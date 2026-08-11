"""Durable repository operations and event transaction boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fomo.agent_framework import DEFAULT_AGENT_FRAMEWORK, AgentFramework
from fomo.auth import hash_password, new_session_id, normalize_email, verify_password
from fomo.direct_pi.failures import public_failure_for_code
from fomo.direct_pi.goalgraph import (
    Goal,
    GoalGraph,
    GoalGraphDraft,
    GoalStatus,
    GraphStatus,
    acceptance_persistence_key,
    materialize_goal_graph,
    parse_goal_graph_draft,
    serialize_goal_graph_draft,
    transition_goal_status,
    transition_graph_status,
)
from fomo.ids import utcnow, uuid7
from fomo.runtime_contract import (
    RuntimeContract,
    resolve_runtime_contract,
    runtime_contract_from_storage,
    validate_agent_framework_runtime,
)
from fomo.schemas import (
    ARTIFACT_KIND_TO_ROLE,
    ARTIFACT_KIND_TO_STAGE,
    VISIBLE_ARTIFACT_KIND_ORDER,
    EventEnvelope,
    MessageResponse,
    ProjectLatestRunResponse,
    ProjectResponse,
    RecoveryMode,
    RunPhase,
    RunResponse,
    RunRuntimeResponse,
    RunStatus,
    RunUsageResponse,
    UserInputRequestDraft,
    UserInputRequestResponse,
    VersionResponse,
)

from .database import Database
from .models import (
    ArtifactRecord,
    CheckpointFileRecord,
    CheckpointRecord,
    GoalEvidenceRecord,
    GoalGraphRecord,
    GoalGraphRevisionRecord,
    GoalNodeRecord,
    MessageRecord,
    ProjectRecord,
    RunEventRecord,
    RunInputRequestRecord,
    RunRecord,
    RunSandboxResourceRecord,
    SessionRecord,
    SpecItemRecord,
    TraceLinkRecord,
    UsageEntryRecord,
    UserRecord,
    VerificationEvidenceRecord,
    VersionFileRecord,
    VersionRecord,
)


def _terminal_event_payload(
    *,
    status: str,
    error_code: str | None,
    summary: str,
) -> dict[str, str]:
    """Build the durable browser payload without forwarding failure text.

    Successful and cancelled summaries are server-authored lifecycle text.
    Failed summaries may originate at many runtime boundaries, so only the
    closed terminal code is allowed to select browser-visible content.
    """

    if status in {RunStatus.failed.value, RunStatus.needs_attention.value}:
        failure = public_failure_for_code(error_code)
        return {
            "status": status,
            "code": failure.code,
            "message": failure.message,
            # Kept for clients replaying the legacy run.failed shape.
            "summary": failure.summary,
        }
    return {"status": status, "summary": summary}


class NotFoundError(LookupError):
    pass


class OwnershipError(PermissionError):
    pass


class AuthenticationError(PermissionError):
    pass


class ConflictError(RuntimeError):
    """The caller tried to write against a stale project/version baseline."""


class FilePathError(ValueError):
    pass


class RunLeaseLost(RuntimeError):
    """A worker attempted a durable write after losing its run lease."""


class ManifestIntegrityError(RuntimeError):
    """A durable checkpoint manifest or file no longer matches its hash."""


TERMINAL_STATUSES = {
    RunStatus.succeeded.value,
    RunStatus.failed.value,
    RunStatus.cancelled.value,
    RunStatus.needs_attention.value,
}

RECOVERABLE_TERMINAL_STATUSES = {
    RunStatus.failed.value,
    RunStatus.cancelled.value,
    RunStatus.needs_attention.value,
}

# Recovery prompts share the same strict bridge envelope as ordinary prompts.
# Bound lineage before a new run is queued so a long recovery chain cannot
# fail minutes later inside a Coding Runtime with an oversized prompt.
MAX_RECOVERY_LINEAGE_RUNS = 8
MAX_RECOVERY_PROMPT_CHARACTERS = 96_000


def _is_business_implementation_path(path: str) -> bool:
    """Keep GoalGraph implementation trace limited to product source files."""
    candidate = PurePosixPath(path)
    parts = tuple(part.lower() for part in candidate.parts)
    name = candidate.name.lower()
    if any(part in {"tests", "test", "__tests__", "e2e"} for part in parts[:-1]):
        return False
    if name in {
        "agents.md",
        "changelog.md",
        "claude.md",
        "contributing.md",
        "license",
        "license.md",
        "readme.md",
    }:
        return False
    if any(marker in name for marker in (".test.", ".spec.", ".stories.")):
        return False
    if parts and parts[0] in {".github", ".fomo", "docs"}:
        return False
    if len(parts) == 1 and (
        name
        in {
            ".gitignore",
            ".npmrc",
            ".nvmrc",
            "components.json",
            "next-env.d.ts",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "yarn.lock",
        }
        or name.startswith(
            (
                "eslint.config.",
                "jest.config.",
                "next.config.",
                "playwright.config.",
                "postcss.config.",
                "prettier.config.",
                "tailwind.config.",
                "tsconfig",
                "vitest.config.",
            )
        )
    ):
        return False
    return True


@dataclass(frozen=True, slots=True)
class SandboxCleanupTarget:
    """A durable sandbox reference that a worker must destroy best-effort."""

    run_id: str
    project_id: str
    sandbox_id: str
    resource_id: str | None = None
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedPreviewTarget:
    """A live sandbox that backs the published preview of a verified run."""

    run_id: str
    project_id: str
    sandbox_id: str
    preview_url: str


@dataclass(frozen=True, slots=True)
class GoalGraphProjection:
    graph_id: str
    project_id: str
    run_id: str
    revision: int
    revision_id: str
    content_hash: str
    graph: GoalGraph


@dataclass(frozen=True, slots=True)
class CheckpointFile:
    path: str
    content_text: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class VerifiedCheckpoint:
    id: str
    graph_id: str
    run_id: str
    goal_id: str
    ordinal: int
    manifest_hash: str
    commit_sha: str | None
    snapshot_id: str | None
    capsule: dict[str, Any]
    files: tuple[CheckpointFile, ...]
    evidence: tuple[dict[str, Any], ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UsageLedgerResult:
    entry_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: int = 0
    cost_micros: int = 0


@dataclass(frozen=True, slots=True)
class RunContinuation:
    request_id: str
    request_status: str
    continuation_key: str
    stage: str
    goal_id: str | None
    context: dict[str, Any]
    question: str
    answer: str | None
    pi_session_id: str
    sandbox_id: str


def _is_expired(value: datetime, *, at: datetime | None = None) -> bool:
    # SQLite does not round-trip timezone info; PostgreSQL does.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    reference = at or utcnow()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return value <= reference


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _project_response(
    record: ProjectRecord,
    latest_run: ProjectLatestRunResponse | None = None,
) -> ProjectResponse:
    return ProjectResponse(
        id=record.id,
        title=record.title,
        status=record.status,
        head_version_id=record.head_version_id,
        active_run_id=record.active_run_id,
        latest_run=latest_run,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _message_response(record: MessageRecord) -> MessageResponse:
    return MessageResponse(
        id=record.id,
        project_id=record.project_id,
        role=record.role,
        content=record.content,
        client_message_id=record.client_message_id,
        run_id=record.run_id,
        created_at=record.created_at,
    )


def _input_request_response(record: RunInputRequestRecord) -> UserInputRequestResponse:
    return UserInputRequestResponse(
        id=record.id,
        run_id=record.run_id,
        question=record.question,
        choices=list(record.choices),
        allow_freeform=record.allow_freeform,
        status=record.status,
        stage=record.stage,
        goal_id=record.goal_id,
        created_at=_as_utc(record.created_at),
        answered_at=_as_utc(record.answered_at) if record.answered_at else None,
    )


def _run_response(
    record: RunRecord,
    last_seq: int = 0,
    pending_input_request: RunInputRequestRecord | None = None,
    *,
    source_checkpoint_available: bool = False,
    usage_totals: UsageTotals | None = None,
) -> RunResponse:
    return RunResponse(
        id=record.id,
        project_id=record.project_id,
        status=RunStatus(record.status),
        phase=RunPhase(record.phase),
        repair_round=record.repair_round,
        last_seq=last_seq,
        base_version_id=record.base_version_id,
        recovered_from_run_id=record.recovered_from_run_id,
        recovered_from_goal_id=record.recovered_from_goal_id,
        recovered_from_checkpoint_id=record.recovered_from_checkpoint_id,
        recovery_mode=record.recovery_mode,
        recovery_available=record.status in RECOVERABLE_TERMINAL_STATUSES,
        source_checkpoint_available=source_checkpoint_available,
        cancel_requested_at=record.cancel_requested_at,
        error_code=record.error_code,
        preview_url=record.preview_url,
        pending_input_request=(
            _input_request_response(pending_input_request)
            if pending_input_request is not None
            else None
        ),
        execution_started_at=(
            _as_utc(record.execution_started_at) if record.execution_started_at else None
        ),
        agent_framework=AgentFramework(record.agent_framework),
        runtime=RunRuntimeResponse(
            profile_id=record.runtime_profile_id,
            thinking=record.runtime_thinking,
            context_window=record.runtime_context_window,
            policy_version=record.runtime_policy_version,
            run_token_budget=record.runtime_run_max_tokens,
            run_token_budget_unlimited=record.runtime_run_max_tokens is None,
            inference_tpm_limit=record.runtime_inference_tpm_limit,
        ),
        usage=_run_usage_response(record.status, usage_totals),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _runtime_contract(record: RunRecord) -> RuntimeContract:
    return runtime_contract_from_storage(
        profile_id=record.runtime_profile_id,
        model_ref=record.runtime_model_ref,
        thinking=record.runtime_thinking,
        context_window=record.runtime_context_window,
        policy_version=record.runtime_policy_version,
        run_max_tokens=record.runtime_run_max_tokens,
        inference_tpm_limit=record.runtime_inference_tpm_limit,
        max_spend_micros=record.runtime_max_spend_micros,
    )


def _project_latest_run_response(
    record: RunRecord,
    *,
    source_checkpoint_available: bool,
    usage_totals: UsageTotals | None,
) -> ProjectLatestRunResponse:
    return ProjectLatestRunResponse(
        id=record.id,
        status=RunStatus(record.status),
        error_code=record.error_code,
        agent_framework=AgentFramework(record.agent_framework),
        profile_id=record.runtime_profile_id,
        thinking=record.runtime_thinking,
        recovery_available=record.status in RECOVERABLE_TERMINAL_STATUSES,
        recovery_mode=record.recovery_mode,
        source_checkpoint_available=source_checkpoint_available,
        usage=_run_usage_response(record.status, usage_totals),
    )


def _run_usage_response(
    run_status: str,
    totals: UsageTotals | None,
) -> RunUsageResponse | None:
    if run_status not in TERMINAL_STATUSES or totals is None:
        return None
    return RunUsageResponse(
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        cache_write_tokens=totals.cache_write_tokens,
        total_tokens=(
            totals.input_tokens
            + totals.output_tokens
            + totals.cache_read_tokens
            + totals.cache_write_tokens
        ),
        tool_calls=totals.tool_calls,
    )


def _version_response(record: VersionRecord) -> VersionResponse:
    return VersionResponse(
        id=record.id,
        project_id=record.project_id,
        number=record.number,
        commit_sha=record.commit_sha,
        parent_version_id=record.parent_version_id,
        qa_status=record.qa_status,
        created_at=record.created_at,
    )


def _bounded_text(value: object, fallback: str, limit: int = 120) -> str:
    """A deterministic, bounded single-line value; never mutates the source."""
    if isinstance(value, str) and value.strip():
        collapsed = " ".join(value.split())
        if len(collapsed) <= limit:
            return collapsed
        # ``limit`` is the final-output hard cap: the ellipsis is part of it,
        # so a long value never exceeds ``limit`` characters.
        return f"{collapsed[: limit - 1].rstrip()}…"
    return fallback


def _artifact_title(kind: str, content: dict[str, Any]) -> str:
    if kind == "run_input":
        return _bounded_text(content.get("title"), "User request")
    if kind == "build_plan":
        return _bounded_text(content.get("title"), "Build plan")
    if kind == "acceptance_contract":
        return "Acceptance contract"
    if kind == "diagnostic_report":
        round_number = content.get("round")
        return (
            f"Verification round {round_number}"
            if isinstance(round_number, int)
            else "Verification report"
        )
    if kind == "product_spec":
        return _bounded_text(content.get("title"), "Product Specification")
    return _bounded_text(content.get("title"), "Technical Specification")


def _artifact_summary(kind: str, content: dict[str, Any]) -> str:
    if kind == "run_input":
        return _bounded_text(content.get("requirement"), "User request")
    if kind == "build_plan":
        return _bounded_text(content.get("summary"), "Direct Pi build plan")
    if kind == "acceptance_contract":
        criteria = content.get("criteria")
        count = len(criteria) if isinstance(criteria, list) else 0
        return f"{count} frozen acceptance workflows"
    if kind == "diagnostic_report":
        return (
            "All deterministic gates passed"
            if content.get("passed") is True
            else "Deterministic verification found blockers"
        )
    if kind == "product_spec":
        return _bounded_text(content.get("problem"), "Product Specification")
    return _bounded_text(content.get("framework"), "Technical Specification")


def _artifact_ref_response(record: ArtifactRecord) -> dict[str, Any]:
    """A visible-artifact reference; title/summary are derived, never content."""
    kind = record.kind
    content = dict(record.content)
    return {
        "id": record.id,
        "runId": record.run_id,
        "kind": kind,
        "role": ARTIFACT_KIND_TO_ROLE[kind],
        "stage": ARTIFACT_KIND_TO_STAGE[kind],
        "schemaVersion": record.schema_version,
        "title": _artifact_title(kind, content),
        "summary": _artifact_summary(kind, content),
        "createdAt": record.created_at,
    }


class Repository:
    """The database is the authoritative event log; no Redis is required for correctness."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def initialize(self) -> None:
        await self.database.upgrade()

    async def register_user(
        self,
        email: str,
        password: str,
        display_name: str | None = None,
        *,
        ttl_hours: int = 24 * 30,
    ) -> tuple[UserRecord, SessionRecord]:
        normalized_email = normalize_email(email)
        password_hash = await asyncio.to_thread(hash_password, password)
        name = display_name.strip() if display_name else normalized_email.partition("@")[0]
        now = utcnow()
        user = UserRecord(
            id=uuid7(),
            email=normalized_email,
            display_name=name,
            password_hash=password_hash,
            created_at=now,
            updated_at=now,
        )
        async with self.database.session_factory() as session:
            session.add(user)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("an account with this email already exists") from exc
            auth_session = await self._issue_user_session_in_session(
                session,
                user.id,
                ttl_hours=ttl_hours,
            )
            await session.commit()
            return user, auth_session

    async def ensure_development_user(
        self,
        email: str,
        password: str,
        display_name: str,
    ) -> UserRecord:
        """Create or repair the configured local-only development account."""

        normalized_email = normalize_email(email)
        normalized_name = display_name.strip() or normalized_email.partition("@")[0]
        async with self.database.session_factory() as session:
            user = await session.scalar(
                select(UserRecord).where(UserRecord.email == normalized_email)
            )
            if user is not None:
                password_matches = await asyncio.to_thread(
                    verify_password,
                    password,
                    user.password_hash,
                )
                if password_matches and user.display_name == normalized_name:
                    return user
                user.password_hash = await asyncio.to_thread(hash_password, password)
                user.display_name = normalized_name
                user.updated_at = utcnow()
                await session.commit()
                return user

            now = utcnow()
            user = UserRecord(
                id=uuid7(),
                email=normalized_email,
                display_name=normalized_name,
                password_hash=await asyncio.to_thread(hash_password, password),
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.commit()
            return user

    async def authenticate_user(
        self,
        email: str,
        password: str,
        *,
        ttl_hours: int = 24 * 30,
    ) -> tuple[UserRecord, SessionRecord]:
        normalized_email = normalize_email(email)
        async with self.database.session_factory() as session:
            user = await session.scalar(
                select(UserRecord).where(UserRecord.email == normalized_email)
            )
            valid = await asyncio.to_thread(
                verify_password,
                password,
                user.password_hash if user is not None else None,
            )
            if user is None or not valid:
                raise AuthenticationError("email or password is incorrect")
            auth_session = await self._issue_user_session_in_session(
                session,
                user.id,
                ttl_hours=ttl_hours,
            )
            await session.commit()
            return user, auth_session

    async def get_current_user(self, session_id: str) -> UserRecord:
        async with self.database.session_factory() as session:
            auth_session = await self._active_session_in_session(session, session_id)
            if auth_session.user_id is None or auth_session.kind != "user":
                raise AuthenticationError("an authenticated account is required")
            user = await session.get(UserRecord, auth_session.user_id)
            if user is None:
                raise AuthenticationError("the session account no longer exists")
            return user

    async def revoke_session(self, session_id: str) -> None:
        async with self.database.session_factory() as session:
            record = await self._active_session_in_session(
                session,
                session_id,
                for_update=True,
            )
            record.revoked_at = utcnow()
            await session.commit()

    async def create_project(self, owner_session_id: str, title: str) -> ProjectResponse:
        async with self.database.session_factory() as session:
            await self._authenticated_session_in_session(session, owner_session_id)
            record = ProjectRecord(
                id=uuid7(),
                owner_session_id=owner_session_id,
                title=title.strip(),
            )
            session.add(record)
            await session.commit()
            return _project_response(record)

    async def list_projects(self, owner_session_id: str) -> list[ProjectResponse]:
        async with self.database.session_factory() as session:
            requester = await self._authenticated_session_in_session(
                session,
                owner_session_id,
            )
            statement = select(ProjectRecord).join(
                SessionRecord,
                ProjectRecord.owner_session_id == SessionRecord.id,
            )
            ownership_filter = and_(
                SessionRecord.kind == "user",
                SessionRecord.user_id == requester.user_id,
            )
            result = await session.scalars(
                statement.where(ownership_filter).order_by(ProjectRecord.updated_at.desc())
            )
            projects = list(result)
            if not projects:
                return []
            runs = list(
                await session.scalars(
                    select(RunRecord)
                    .where(RunRecord.project_id.in_([record.id for record in projects]))
                    .order_by(
                        RunRecord.project_id.asc(),
                        RunRecord.created_at.desc(),
                        RunRecord.id.desc(),
                    )
                )
            )
            latest_by_project: dict[str, RunRecord] = {}
            for run in runs:
                latest_by_project.setdefault(run.project_id, run)
            usage_by_run = await self._usage_totals_for_runs_in_session(
                session,
                [run.id for run in latest_by_project.values()],
            )
            responses: list[ProjectResponse] = []
            for project in projects:
                latest = latest_by_project.get(project.id)
                summary = None
                if latest is not None:
                    checkpoint_available = await self._source_checkpoint_available_in_session(
                        session, latest
                    )
                    summary = _project_latest_run_response(
                        latest,
                        source_checkpoint_available=checkpoint_available,
                        usage_totals=usage_by_run.get(latest.id),
                    )
                responses.append(_project_response(project, summary))
            return responses

    async def require_project(
        self, project_id: str, owner_session_id: str | None = None
    ) -> ProjectRecord:
        async with self.database.session_factory() as session:
            record = await session.get(ProjectRecord, project_id)
            if record is None:
                raise NotFoundError("project not found")
            if owner_session_id is not None and not await self._session_can_access_project(
                session,
                record,
                owner_session_id,
            ):
                raise OwnershipError("project does not belong to this session")
            return record

    async def patch_project(
        self, project_id: str, owner_session_id: str, title: str
    ) -> ProjectResponse:
        async with self.database.session_factory() as session:
            record = await self._require_project_in_session(session, project_id, owner_session_id)
            record.title = title.strip()
            record.updated_at = utcnow()
            await session.commit()
            return _project_response(record)

    async def get_message_run_by_client_id(
        self,
        project_id: str,
        owner_session_id: str,
        client_message_id: str,
    ) -> tuple[MessageResponse, RunResponse] | None:
        """Read an existing idempotent result before consulting mutable runtime policy."""
        async with self.database.session_factory() as session:
            await self._require_project_in_session(session, project_id, owner_session_id)
            message = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.project_id == project_id,
                    MessageRecord.client_message_id == client_message_id,
                )
            )
            if message is None:
                return None
            if message.run_id is None:
                raise RuntimeError("idempotent message is missing its run")
            run = await session.get(RunRecord, message.run_id)
            if run is None:
                raise RuntimeError("idempotent message points to a missing run")
            return _message_response(message), await self._run_with_seq(session, run)

    async def create_message_and_run(
        self,
        project_id: str,
        owner_session_id: str,
        client_message_id: str,
        content: str,
        base_version_id: str | None = None,
        *,
        agent_framework: AgentFramework | str = DEFAULT_AGENT_FRAMEWORK,
        runtime_contract: RuntimeContract | None = None,
        enforce_runtime_match: bool = False,
        enforce_agent_framework_match: bool = False,
    ) -> tuple[MessageResponse, RunResponse, bool]:
        """Save a message and queued run atomically; duplicate client IDs are idempotent."""
        try:
            frozen_agent_framework = AgentFramework(agent_framework).value
        except ValueError:
            raise ValueError(f"unsupported agent framework: {agent_framework}") from None
        async with self.database.session_factory() as session:
            project = await self._require_project_in_session(session, project_id, owner_session_id)
            existing_message = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.project_id == project_id,
                    MessageRecord.client_message_id == client_message_id,
                )
            )
            if existing_message is not None:
                if existing_message.run_id is None:
                    raise RuntimeError("idempotent message is missing its run")
                existing_run = await session.get(RunRecord, existing_message.run_id)
                if existing_run is None:
                    raise RuntimeError("idempotent message points to a missing run")
                if (
                    existing_message.content != content
                    or (
                        base_version_id is not None
                        and existing_run.base_version_id != base_version_id
                    )
                    or (
                        enforce_runtime_match
                        and runtime_contract is not None
                        and _runtime_contract(existing_run) != runtime_contract
                    )
                    or (
                        enforce_agent_framework_match
                        and existing_run.agent_framework != frozen_agent_framework
                    )
                ):
                    raise ConflictError(
                        "Idempotency-Key was already used with a different request"
                    )
                return (
                    _message_response(existing_message),
                    await self._run_with_seq(session, existing_run),
                    False,
                )

            if project.active_run_id is not None:
                active = await session.get(RunRecord, project.active_run_id)
                if active is not None and active.status == RunStatus.waiting_for_user.value:
                    raise ConflictError(
                        "the active run is waiting for an answer; use its input-request endpoint"
                    )

            frozen_runtime = runtime_contract or resolve_runtime_contract()
            validate_agent_framework_runtime(
                frozen_agent_framework,
                frozen_runtime.profile_id,
                frozen_runtime.thinking,
            )
            run_id = uuid7()
            run = RunRecord(
                id=run_id,
                project_id=project_id,
                base_version_id=base_version_id or project.head_version_id,
                status=RunStatus.queued.value,
                phase=RunPhase.queued.value,
                pi_session_id=f"fomo-{run_id}",
                agent_framework=frozen_agent_framework,
                runtime_profile_id=frozen_runtime.profile_id,
                runtime_model_ref=frozen_runtime.model_ref,
                runtime_thinking=frozen_runtime.thinking,
                runtime_context_window=frozen_runtime.context_window,
                runtime_policy_version=frozen_runtime.policy_version,
                runtime_run_max_tokens=frozen_runtime.run_max_tokens,
                runtime_inference_tpm_limit=frozen_runtime.inference_tpm_limit,
                runtime_max_spend_micros=frozen_runtime.max_spend_micros,
            )
            message = MessageRecord(
                id=uuid7(),
                project_id=project_id,
                role="user",
                content=content,
                client_message_id=client_message_id,
                run_id=run.id,
            )
            session.add_all((run, message))
            if project.active_run_id is None:
                project.active_run_id = run.id
            project.status = "queued"
            project.updated_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "run.created",
                payload={
                    "messageId": message.id,
                    "baseVersionId": run.base_version_id,
                    "agentFramework": frozen_agent_framework,
                    "profileId": frozen_runtime.profile_id,
                    "thinking": frozen_runtime.thinking,
                    "contextWindow": frozen_runtime.context_window,
                    "runtimePolicy": frozen_runtime.policy_version,
                },
            )
            await session.commit()
            return _message_response(message), await self._run_with_seq(session, run), True

    async def create_recovery_message_and_run(
        self,
        source_run_id: str,
        owner_session_id: str,
        client_message_id: str,
        content: str,
        *,
        agent_framework: AgentFramework | str | None = None,
        runtime_contract: RuntimeContract | None = None,
        enforce_runtime_match: bool = False,
        enforce_agent_framework_match: bool = False,
    ) -> tuple[MessageResponse, RunResponse, bool, RecoveryMode, bool]:
        """Fork terminal history into a fresh queued run.

        The source run, sandbox and Coding Agent session are never mutated or
        resumed. An integrity-checked goal checkpoint takes priority over a
        verified published source baseline; without either, the new run
        explicitly restarts from the product base.
        """

        async with self.database.session_factory() as session:
            source = await session.get(RunRecord, source_run_id, with_for_update=True)
            if source is None:
                raise NotFoundError("run not found")
            project = await self._require_project_in_session(
                session, source.project_id, owner_session_id
            )
            existing_message = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.project_id == project.id,
                    MessageRecord.client_message_id == client_message_id,
                )
            )
            if existing_message is not None:
                if existing_message.run_id is None:
                    raise RuntimeError("idempotent recovery message is missing its run")
                existing_run = await session.get(RunRecord, existing_message.run_id)
                if existing_run is None:
                    raise RuntimeError("idempotent recovery message points to a missing run")
                if (
                    existing_message.content != content
                    or existing_run.recovered_from_run_id != source.id
                    or (
                        enforce_runtime_match
                        and runtime_contract is not None
                        and _runtime_contract(existing_run) != runtime_contract
                    )
                    or (
                        enforce_agent_framework_match
                        and agent_framework is not None
                        and existing_run.agent_framework
                        != AgentFramework(agent_framework).value
                    )
                    or existing_run.recovery_mode is None
                ):
                    raise ConflictError(
                        "Idempotency-Key was already used with a different request"
                    )
                return (
                    _message_response(existing_message),
                    await self._run_with_seq(session, existing_run),
                    False,
                    existing_run.recovery_mode,
                    existing_run.recovered_from_checkpoint_id is not None,
                )

            if source.status not in RECOVERABLE_TERMINAL_STATUSES:
                raise ConflictError("only a failed, interrupted, or cancelled run can recover")

            try:
                source_lineage = await self._run_prompt_lineage_in_session(
                    session,
                    source.id,
                    expected_project_id=source.project_id,
                )
                self._assemble_run_prompt(
                    [message.content for _run, message in source_lineage] + [content]
                )
            except ManifestIntegrityError as exc:
                raise ConflictError(
                    "recovery history is invalid or too large; start a new project"
                ) from exc

            try:
                checkpoint = await self._recoverable_checkpoint_for_source_in_session(
                    session, source
                )
            except ManifestIntegrityError as exc:
                raise ConflictError(
                    "the source recovery checkpoint failed integrity validation"
                ) from exc
            verified_version = await self._verified_base_version_in_session(
                session, source
            )
            recovery_mode: RecoveryMode
            if checkpoint is not None:
                recovery_mode = "verified_checkpoint"
            elif verified_version is not None:
                recovery_mode = "verified_version"
            else:
                recovery_mode = "base_restart"

            selected_framework = AgentFramework(
                agent_framework if agent_framework is not None else source.agent_framework
            ).value
            selected_runtime = runtime_contract or _runtime_contract(source)
            validate_agent_framework_runtime(
                selected_framework,
                selected_runtime.profile_id,
                selected_runtime.thinking,
            )
            run_id = uuid7()
            run = RunRecord(
                id=run_id,
                project_id=project.id,
                base_version_id=(verified_version.id if verified_version is not None else None),
                recovered_from_run_id=source.id,
                recovered_from_goal_id=(checkpoint.goal_id if checkpoint is not None else None),
                recovered_from_checkpoint_id=(checkpoint.id if checkpoint is not None else None),
                recovery_mode=recovery_mode,
                status=RunStatus.queued.value,
                phase=RunPhase.queued.value,
                # A recovery always starts a fresh framework session. Destroyed
                # source sandboxes and session identifiers are never resumed.
                pi_session_id=f"fomo-{run_id}",
                agent_framework=selected_framework,
                runtime_profile_id=selected_runtime.profile_id,
                runtime_model_ref=selected_runtime.model_ref,
                runtime_thinking=selected_runtime.thinking,
                runtime_context_window=selected_runtime.context_window,
                runtime_policy_version=selected_runtime.policy_version,
                runtime_run_max_tokens=selected_runtime.run_max_tokens,
                runtime_inference_tpm_limit=selected_runtime.inference_tpm_limit,
                runtime_max_spend_micros=selected_runtime.max_spend_micros,
            )
            message = MessageRecord(
                id=uuid7(),
                project_id=project.id,
                role="user",
                content=content,
                client_message_id=client_message_id,
                run_id=run.id,
            )
            session.add_all((run, message))
            if project.active_run_id is None:
                project.active_run_id = run.id
            project.status = "queued"
            project.updated_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "run.created",
                payload={
                    "messageId": message.id,
                    "baseVersionId": run.base_version_id,
                    "agentFramework": selected_framework,
                    "profileId": selected_runtime.profile_id,
                    "thinking": selected_runtime.thinking,
                    "contextWindow": selected_runtime.context_window,
                    "runtimePolicy": selected_runtime.policy_version,
                    "recoveredFromRunId": source.id,
                    "recoveredFromGoalId": run.recovered_from_goal_id,
                    "recoveredFromCheckpointId": run.recovered_from_checkpoint_id,
                    "recoveryMode": recovery_mode,
                    "sourceCheckpointAvailable": checkpoint is not None,
                },
            )
            await session.commit()
            return (
                _message_response(message),
                await self._run_with_seq(session, run),
                True,
                recovery_mode,
                checkpoint is not None,
            )

    async def get_project_snapshot(self, project_id: str, owner_session_id: str) -> dict[str, Any]:
        async with self.database.session_factory() as session:
            project = await self._require_project_in_session(session, project_id, owner_session_id)
            messages = list(
                await session.scalars(
                    select(MessageRecord)
                    .where(MessageRecord.project_id == project_id)
                    .order_by(MessageRecord.created_at.asc())
                )
            )
            runs = list(
                await session.scalars(
                    select(RunRecord)
                    .where(RunRecord.project_id == project_id)
                    .order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
                )
            )
            usage_by_run = await self._usage_totals_for_runs_in_session(
                session,
                [item.id for item in runs],
            )
            run_responses = [
                await self._run_with_seq(
                    session,
                    item,
                    usage_totals=usage_by_run.get(item.id),
                    usage_totals_loaded=True,
                )
                for item in runs
            ]
            active_record = (
                await session.get(RunRecord, project.active_run_id)
                if project.active_run_id is not None
                else None
            )
            active_run = (
                await self._run_with_seq(
                    session,
                    active_record,
                    usage_totals=usage_by_run.get(active_record.id),
                    usage_totals_loaded=True,
                )
                if active_record is not None
                else None
            )
            pending_input_request = (
                await self._pending_input_request_in_session(session, active_record.id)
                if active_record is not None
                else None
            )
            # `active_run` is deliberately null once a run becomes terminal,
            # but refresh still needs the latest completed run's visible trace
            # to reconstruct role progress in the workbench.
            display_record = (
                active_record if active_record is not None else (runs[0] if runs else None)
            )
            display_events: list[EventEnvelope] = []
            if display_record is not None:
                event_records = list(
                    await session.scalars(
                        select(RunEventRecord)
                        .where(RunEventRecord.run_id == display_record.id)
                        .order_by(RunEventRecord.seq.asc())
                    )
                )
                display_events = [
                    self._event_envelope(display_record, item) for item in event_records
                ]
            trace_run_id = display_record.id if display_record is not None else None
            files = await self._list_version_files_in_session(session, project)
            versions = await self._list_versions_in_session(session, project_id)
            trace = await self._get_trace_in_session(session, project_id, trace_run_id)
            preview = await self._get_preview_in_session(session, project)
            artifact_refs = await self._artifact_refs_in_session(
                session, display_record.id if display_record is not None else None
            )
            goal_graph = (
                await self._goal_graph_read_projection_in_session(session, display_record.id)
                if display_record is not None
                else None
            )
            latest_summary = None
            if runs:
                latest_summary = _project_latest_run_response(
                    runs[0],
                    source_checkpoint_available=(
                        await self._source_checkpoint_available_in_session(session, runs[0])
                    ),
                    usage_totals=usage_by_run.get(runs[0].id),
                )
            return {
                "project": _project_response(project, latest_summary),
                "messages": [_message_response(item) for item in messages],
                "runs": run_responses,
                "active_run": active_run,
                "last_seq": (
                    active_run.last_seq
                    if active_run is not None
                    else (run_responses[0].last_seq if run_responses else 0)
                ),
                "events": display_events,
                "files": files,
                "versions": versions,
                "trace": trace,
                "preview": preview,
                "artifact_refs": artifact_refs,
                "goal_graph": goal_graph,
                "pending_input_request": (
                    _input_request_response(pending_input_request)
                    if pending_input_request is not None
                    else None
                ),
            }

    async def get_run(self, run_id: str) -> RunResponse:
        async with self.database.session_factory() as session:
            record = await session.get(RunRecord, run_id)
            if record is None:
                raise NotFoundError("run not found")
            return await self._run_with_seq(session, record)

    async def get_run_runtime_contract(self, run_id: str) -> RuntimeContract:
        """Return the immutable internal runtime tuple without exposing its alias."""
        async with self.database.session_factory() as session:
            record = await session.get(RunRecord, run_id)
            if record is None:
                raise NotFoundError("run not found")
            return _runtime_contract(record)

    async def get_run_agent_framework(self, run_id: str) -> str:
        """Return the run's immutable, public Coding Agent framework."""
        async with self.database.session_factory() as session:
            record = await session.get(RunRecord, run_id)
            if record is None:
                raise NotFoundError("run not found")
            return AgentFramework(record.agent_framework).value

    async def get_run_prompt(self, run_id: str) -> str:
        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise NotFoundError("run not found")
            lineage = await self._run_prompt_lineage_in_session(
                session,
                run.id,
                expected_project_id=run.project_id,
            )
            return self._assemble_run_prompt(
                [message.content for _current, message in lineage]
            )

    @staticmethod
    async def _run_prompt_lineage_in_session(
        session: AsyncSession,
        run_id: str,
        *,
        expected_project_id: str,
    ) -> list[tuple[RunRecord, MessageRecord]]:
        lineage: list[tuple[RunRecord, MessageRecord]] = []
        visited: set[str] = set()
        current_id: str | None = run_id
        while current_id is not None:
            if current_id in visited:
                raise ManifestIntegrityError("recovery run lineage contains a cycle")
            if len(lineage) >= MAX_RECOVERY_LINEAGE_RUNS:
                raise ManifestIntegrityError("recovery run lineage is too deep")
            visited.add(current_id)
            current = await session.get(RunRecord, current_id)
            if current is None:
                raise NotFoundError("run not found")
            if current.project_id != expected_project_id:
                raise ManifestIntegrityError("recovery run lineage crossed project scope")
            message = await session.scalar(
                select(MessageRecord)
                .where(
                    MessageRecord.run_id == current.id,
                    MessageRecord.project_id == expected_project_id,
                    MessageRecord.role == "user",
                )
                .order_by(MessageRecord.created_at.asc(), MessageRecord.id.asc())
                .limit(1)
            )
            if message is None:
                raise NotFoundError("run message not found")
            lineage.append((current, message))
            current_id = current.recovered_from_run_id
        lineage.reverse()
        return lineage

    @staticmethod
    def _assemble_run_prompt(contents: list[str]) -> str:
        if not contents or len(contents) > MAX_RECOVERY_LINEAGE_RUNS:
            raise ManifestIntegrityError("recovery prompt lineage is invalid")
        if len(contents) == 1:
            prompt = contents[0]
        else:
            followups = "\n\n".join(
                f"Recovery follow-up {index}:\n{content}"
                for index, content in enumerate(contents[1:], start=1)
            )
            prompt = f"Original request:\n{contents[0]}\n\n{followups}"
        if len(prompt) > MAX_RECOVERY_PROMPT_CHARACTERS:
            raise ManifestIntegrityError("recovery prompt exceeds the runtime limit")
        return prompt

    async def ensure_pi_session_id(
        self,
        run_id: str,
        proposed_session_id: str,
        *,
        lease_token: str,
    ) -> str:
        if not proposed_session_id or len(proposed_session_id) > 128:
            raise ValueError("Pi session id must be nonempty and bounded")
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if run.pi_session_id is None:
                run.pi_session_id = proposed_session_id
                run.updated_at = utcnow()
                await session.commit()
                return proposed_session_id
            return run.pi_session_id

    async def wait_for_user_input(
        self,
        run_id: str,
        draft: UserInputRequestDraft | Mapping[str, Any],
        *,
        continuation_key: str,
        continuation_context: Mapping[str, Any] | None,
        stage: str,
        goal_id: str | None,
        pi_session_id: str,
        sandbox_id: str,
        lease_token: str,
    ) -> UserInputRequestResponse:
        """Persist a semantic Pi question and relinquish the worker lease.

        The Pi turn has already settled before this transaction begins. The
        worker therefore never blocks on user input and the exact continuation
        identity is durable before the run becomes externally answerable.
        """

        request = (
            draft
            if isinstance(draft, UserInputRequestDraft)
            else UserInputRequestDraft.model_validate(draft)
        )
        if stage not in {"planning", "building", "repairing"}:
            raise ValueError("input request stage is unsupported")
        if not continuation_key or len(continuation_key) > 96:
            raise ValueError("continuation key must be nonempty and bounded")
        if goal_id is not None and (not goal_id or len(goal_id) > 64):
            raise ValueError("continuation goal id must be bounded")
        context = dict(continuation_context or {})
        try:
            encoded_context = json.dumps(
                context,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("continuation context must be valid JSON") from exc
        if len(encoded_context) > 2 * 1024 * 1024:
            raise ValueError("continuation context exceeds its durable limit")

        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if run.sandbox_id != sandbox_id:
                raise ConflictError("continuation sandbox no longer matches the active run")
            if run.pi_session_id is None:
                run.pi_session_id = pi_session_id
            elif run.pi_session_id != pi_session_id:
                raise ConflictError("continuation Pi session does not match the active run")
            pending = await self._pending_input_request_in_session(session, run_id)
            if pending is not None:
                raise ConflictError("run already has a pending input request")

            now = utcnow()
            record = RunInputRequestRecord(
                id=uuid7(),
                run_id=run_id,
                question=request.question,
                choices=list(request.choices),
                allow_freeform=request.allow_freeform,
                status="pending",
                stage=stage,
                goal_id=goal_id,
                created_at=now,
            )
            session.add(record)
            run.continuation_request_id = record.id
            run.continuation_key = continuation_key
            run.continuation_stage = stage
            run.continuation_goal_id = goal_id
            run.continuation_context = context
            run.status = RunStatus.waiting_for_user.value
            run.lease_owner = None
            run.lease_expires_at = None
            run.updated_at = now
            project = await session.get(ProjectRecord, run.project_id, with_for_update=True)
            if project is not None:
                project.active_run_id = run.id
                project.status = RunStatus.waiting_for_user.value
                project.updated_at = now
            await self._append_event_in_session(
                session,
                run,
                "run.input_requested",
                role="pi",
                payload={
                    "requestId": record.id,
                    "question": record.question,
                    "choices": list(record.choices),
                    "allowFreeform": record.allow_freeform,
                    "stage": stage,
                    "goalId": goal_id,
                },
            )
            await self._append_event_in_session(
                session,
                run,
                "run.status_changed",
                payload={"status": run.status, "phase": run.phase},
            )
            await session.commit()
            return _input_request_response(record)

    async def get_pending_input_request(self, run_id: str) -> UserInputRequestResponse | None:
        async with self.database.session_factory() as session:
            if await session.get(RunRecord, run_id) is None:
                raise NotFoundError("run not found")
            record = await self._pending_input_request_in_session(session, run_id)
            return _input_request_response(record) if record is not None else None

    async def answer_user_input(
        self,
        run_id: str,
        request_id: str,
        owner_session_id: str,
        client_message_id: str,
        answer: str,
    ) -> tuple[MessageResponse, UserInputRequestResponse, RunResponse, bool]:
        """Answer one pending request idempotently and requeue the same run."""

        if not answer.strip():
            raise ValueError("answer must be non-blank")
        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id, with_for_update=True)
            if run is None:
                raise NotFoundError("run not found")
            await self._require_project_in_session(session, run.project_id, owner_session_id)
            request = await session.get(RunInputRequestRecord, request_id, with_for_update=True)
            if request is None or request.run_id != run_id:
                raise NotFoundError("input request not found")

            existing_message = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.project_id == run.project_id,
                    MessageRecord.client_message_id == client_message_id,
                )
            )
            if existing_message is not None:
                if (
                    existing_message.run_id != run_id
                    or request.answer_message_id != existing_message.id
                ):
                    raise ConflictError("client message id belongs to another operation")
                return (
                    _message_response(existing_message),
                    _input_request_response(request),
                    await self._run_with_seq(session, run),
                    False,
                )

            if request.status != "pending":
                raise ConflictError("input request is no longer pending")
            if run.status != RunStatus.waiting_for_user.value:
                raise ConflictError("run is not waiting for user input")
            if run.continuation_request_id != request.id:
                raise ConflictError("input request is not the active continuation")

            answer_content = answer
            if not request.allow_freeform:
                normalized_answer = answer.strip()
                if normalized_answer not in request.choices:
                    raise ConflictError(
                        "answer must exactly match one of the input request choices"
                    )
                # Persist the canonical choice value, not transport whitespace.
                answer_content = normalized_answer

            now = utcnow()
            message = MessageRecord(
                id=uuid7(),
                project_id=run.project_id,
                role="user",
                content=answer_content,
                client_message_id=client_message_id,
                run_id=run.id,
                created_at=now,
            )
            session.add(message)
            request.status = "answered"
            request.answer_message_id = message.id
            request.answered_at = now
            run.status = RunStatus.queued.value
            run.lease_owner = None
            run.lease_expires_at = None
            run.updated_at = now
            project = await session.get(ProjectRecord, run.project_id, with_for_update=True)
            if project is not None:
                project.active_run_id = run.id
                project.status = RunStatus.queued.value
                project.updated_at = now
            await self._append_event_in_session(
                session,
                run,
                "run.input_answered",
                role="user",
                payload={"requestId": request.id, "messageId": message.id},
            )
            await self._append_event_in_session(
                session,
                run,
                "run.status_changed",
                payload={"status": run.status, "phase": run.phase},
            )
            await session.commit()
            return (
                _message_response(message),
                _input_request_response(request),
                await self._run_with_seq(session, run),
                True,
            )

    async def get_run_continuation(self, run_id: str) -> RunContinuation | None:
        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise NotFoundError("run not found")
            cursor_fields = (
                run.continuation_key,
                run.continuation_stage,
                run.continuation_goal_id,
                run.continuation_context,
            )
            if run.continuation_request_id is None:
                if any(value is not None for value in cursor_fields):
                    raise ConflictError("run continuation cursor is incomplete")
                return None
            required_identity = (
                run.continuation_request_id,
                run.continuation_key,
                run.continuation_stage,
                run.pi_session_id,
                run.sandbox_id,
            )
            if any(not isinstance(value, str) or not value for value in required_identity):
                raise ConflictError("run continuation identity is incomplete")
            request = await session.get(RunInputRequestRecord, run.continuation_request_id)
            if request is None or request.run_id != run_id:
                raise ConflictError("run continuation request is missing")
            answer: str | None = None
            if request.answer_message_id is not None:
                message = await session.get(MessageRecord, request.answer_message_id)
                if message is None or message.run_id != run_id or message.role != "user":
                    raise ConflictError("run continuation answer is missing")
                answer = message.content
            return RunContinuation(
                request_id=request.id,
                request_status=request.status,
                continuation_key=run.continuation_key,
                stage=run.continuation_stage,
                goal_id=run.continuation_goal_id,
                context=dict(run.continuation_context or {}),
                question=request.question,
                answer=answer,
                pi_session_id=run.pi_session_id,
                sandbox_id=run.sandbox_id,
            )

    async def complete_run_continuation(
        self,
        run_id: str,
        request_id: str,
        *,
        lease_token: str,
    ) -> None:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if run.continuation_request_id != request_id:
                raise ConflictError("run continuation changed before completion")
            request = await session.get(RunInputRequestRecord, request_id)
            if request is None or request.status != "answered":
                raise ConflictError("run continuation has not been answered")
            stage = run.continuation_stage
            goal_id = run.continuation_goal_id
            run.continuation_request_id = None
            run.continuation_key = None
            run.continuation_stage = None
            run.continuation_goal_id = None
            run.continuation_context = None
            run.updated_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "run.resumed",
                payload={"requestId": request_id, "stage": stage, "goalId": goal_id},
            )
            await session.commit()

    async def create_goal_graph(
        self,
        project_id: str,
        run_id: str,
        draft: GoalGraphDraft | Mapping[str, Any] | str | bytes,
        *,
        provenance: Mapping[str, Any] | None = None,
        reason: str | None = None,
        lease_token: str | None = None,
    ) -> GoalGraphProjection:
        """Persist an immutable revision and mutable node lifecycle projection."""
        validated = draft if isinstance(draft, GoalGraphDraft) else parse_goal_graph_draft(draft)
        graph = materialize_goal_graph(validated)
        canonical = serialize_goal_graph_draft(validated)
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now = utcnow()
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if run.project_id != project_id:
                raise ConflictError("goal graph project does not match its run")
            if await session.scalar(
                select(GoalGraphRecord.id).where(GoalGraphRecord.run_id == run_id)
            ):
                raise ConflictError("run already has a goal graph")
            superseded_graph_ids = await self._supersede_terminal_project_graphs(
                session, project_id, excluding_run_id=run_id
            )
            graph_record = GoalGraphRecord(
                id=uuid7(),
                project_id=project_id,
                run_id=run_id,
                schema_version=validated.schema_version,
                current_revision=1,
                status=graph.status.value,
                created_at=now,
                updated_at=now,
            )
            revision = GoalGraphRevisionRecord(
                id=uuid7(),
                graph_id=graph_record.id,
                revision=1,
                product_outcome=validated.product_outcome,
                quality_bar=graph.quality_bar.model_dump(mode="json", by_alias=True),
                content_hash=content_hash,
                reason=reason.strip() if reason and reason.strip() else None,
                provenance=dict(provenance or {"createdBy": f"run:{run_id}"}),
                created_by_run_id=run_id,
                created_at=now,
            )
            session.add_all([graph_record, revision])
            nodes: list[GoalNodeRecord] = []
            for position, goal in enumerate(graph.goals):
                node = GoalNodeRecord(
                    id=uuid7(),
                    graph_id=graph_record.id,
                    revision_id=revision.id,
                    project_id=project_id,
                    run_id=run_id,
                    goal_key=goal.goal_id,
                    position=position,
                    title=goal.title,
                    product_outcome=goal.product_outcome,
                    user_visible=goal.user_visible,
                    depends_on=list(goal.depends_on),
                    acceptance=goal.acceptance.model_dump(mode="json", by_alias=True),
                    status=goal.status.value,
                )
                nodes.append(node)
                session.add(node)
            read_projection = self._goal_graph_read_projection(
                graph_record,
                revision,
                nodes,
                checkpoint_by_node={},
                evidence_count_by_key={},
            )
            await self._append_event_in_session(
                session,
                run,
                "goal_graph.created",
                payload={
                    "graphId": graph_record.id,
                    "revision": 1,
                    "goalCount": len(nodes),
                    "goalGraph": read_projection,
                },
            )
            if superseded_graph_ids:
                await self._append_event_in_session(
                    session,
                    run,
                    "goal_graph.prior_superseded",
                    payload={"graphIds": superseded_graph_ids, "supersededBy": graph_record.id},
                )
            await session.commit()
            return self._goal_graph_projection(graph_record, revision, nodes)

    async def get_goal_graph_for_run(self, run_id: str) -> GoalGraphProjection | None:
        """Return None for a P0 run that has no GoalGraph."""
        async with self.database.session_factory() as session:
            return await self._goal_graph_for_run_in_session(session, run_id)

    async def get_goal_graph(self, run_id: str) -> GoalGraphProjection | None:
        return await self.get_goal_graph_for_run(run_id)

    async def activate_goal(
        self,
        run_id: str,
        goal_id: str,
        *,
        lease_token: str | None = None,
    ) -> GoalGraphProjection:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            graph, revision, nodes, target = await self._goal_write_context(
                session, run_id, goal_id
            )
            current_goal = await session.scalar(
                select(GoalNodeRecord.id).where(
                    GoalNodeRecord.project_id == run.project_id,
                    GoalNodeRecord.status.in_([GoalStatus.ACTIVE.value, GoalStatus.CLAIMED.value]),
                )
            )
            if current_goal is not None:
                raise ConflictError("project already has an active goal")
            status_by_key = {node.goal_key: node.status for node in nodes}
            if any(
                status_by_key.get(key) != GoalStatus.VERIFIED.value for key in target.depends_on
            ):
                raise ConflictError("goal dependencies are not verified")
            target.status = transition_goal_status(
                GoalStatus(target.status), GoalStatus.ACTIVE
            ).value
            await self._append_event_in_session(
                session,
                run,
                "goal.activated",
                payload={"graphId": graph.id, "goalId": goal_id, "revision": revision.revision},
            )
            await session.commit()
            return self._goal_graph_projection(graph, revision, nodes)

    async def claim_goal(
        self,
        run_id: str,
        goal_id: str,
        *,
        lease_token: str | None = None,
    ) -> GoalGraphProjection:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            graph, revision, nodes, target = await self._goal_write_context(
                session, run_id, goal_id
            )
            target.status = transition_goal_status(
                GoalStatus(target.status), GoalStatus.CLAIMED
            ).value
            target.claimed_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "goal.claimed",
                payload={"graphId": graph.id, "goalId": goal_id, "revision": revision.revision},
            )
            await session.commit()
            return self._goal_graph_projection(graph, revision, nodes)

    async def resume_goal(
        self,
        run_id: str,
        goal_id: str,
        *,
        lease_token: str,
    ) -> GoalGraphProjection:
        """Fence a resumed worker and return claimed work to executable active state."""
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            graph, revision, nodes, target = await self._goal_write_context(
                session, run_id, goal_id
            )
            current = GoalStatus(target.status)
            if current == GoalStatus.CLAIMED:
                target.status = transition_goal_status(current, GoalStatus.ACTIVE).value
            elif current != GoalStatus.ACTIVE:
                raise ConflictError("only an active or claimed goal can be resumed")
            await self._append_event_in_session(
                session,
                run,
                "goal.resumed",
                payload={
                    "graphId": graph.id,
                    "goalId": goal_id,
                    "revision": revision.revision,
                },
            )
            await session.commit()
            return self._goal_graph_projection(graph, revision, nodes)

    async def terminalize_goal_graph(
        self,
        run_id: str,
        status: GraphStatus | str,
        *,
        reason: str,
        lease_token: str | None = None,
    ) -> GoalGraphProjection:
        """Close current and pending goals so a later project run cannot be blocked."""
        status = GraphStatus(status)
        if status not in {
            GraphStatus.FAILED,
            GraphStatus.CANCELLED,
            GraphStatus.SUPERSEDED,
        }:
            raise ValueError("terminal graph status must be failed, cancelled, or superseded")
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            graph = await session.scalar(
                select(GoalGraphRecord).where(GoalGraphRecord.run_id == run_id).with_for_update()
            )
            if graph is None:
                raise NotFoundError("goal graph not found")
            revision = await self._current_revision_in_session(session, graph)
            nodes = list(
                await session.scalars(
                    select(GoalNodeRecord)
                    .where(GoalNodeRecord.revision_id == revision.id)
                    .order_by(GoalNodeRecord.position)
                    .with_for_update()
                )
            )
            if graph.status == status.value:
                return self._goal_graph_projection(graph, revision, nodes)
            target_goal_status = (
                GoalStatus.FAILED if status == GraphStatus.FAILED else GoalStatus.SUPERSEDED
            )
            for node in nodes:
                current = GoalStatus(node.status)
                if current in {GoalStatus.ACTIVE, GoalStatus.CLAIMED}:
                    node.status = transition_goal_status(current, target_goal_status).value
                    if target_goal_status == GoalStatus.FAILED:
                        node.failed_at = utcnow()
                elif current == GoalStatus.PENDING:
                    node.status = transition_goal_status(current, GoalStatus.SUPERSEDED).value
            graph.status = transition_graph_status(GraphStatus(graph.status), status).value
            graph.updated_at = utcnow()
            goal_graph = await self._goal_graph_read_projection_in_session(session, run_id)
            await self._append_event_in_session(
                session,
                run,
                f"goal_graph.{status.value}",
                payload={
                    "graphId": graph.id,
                    "status": status.value,
                    "reason": reason,
                    "goalGraph": goal_graph,
                },
            )
            await session.commit()
            return self._goal_graph_projection(graph, revision, nodes)

    async def fail_goal(
        self,
        run_id: str,
        goal_id: str,
        *,
        reason: str,
        lease_token: str | None = None,
    ) -> GoalGraphProjection:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            graph, revision, nodes, target = await self._goal_write_context(
                session, run_id, goal_id
            )
            target.status = transition_goal_status(
                GoalStatus(target.status), GoalStatus.FAILED
            ).value
            target.failed_at = utcnow()
            graph.status = transition_graph_status(
                GraphStatus(graph.status), GraphStatus.FAILED
            ).value
            graph.updated_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "goal.failed",
                payload={"graphId": graph.id, "goalId": goal_id, "reason": reason},
            )
            goal_graph = await self._goal_graph_read_projection_in_session(session, run_id)
            await self._append_event_in_session(
                session,
                run,
                "goal_graph.failed",
                payload={
                    "graphId": graph.id,
                    "status": graph.status,
                    "reason": reason,
                    "goalGraph": goal_graph,
                },
            )
            await session.commit()
            return self._goal_graph_projection(graph, revision, nodes)

    async def record_verified_checkpoint(
        self,
        run_id: str,
        goal_id: str,
        files: Iterable[Mapping[str, Any] | CheckpointFile],
        evidence: Iterable[Mapping[str, Any]],
        *,
        lease_token: str,
        commit_sha: str | None = None,
        snapshot_id: str | None = None,
        capsule: Mapping[str, Any] | None = None,
    ) -> VerifiedCheckpoint:
        """Atomically fence, checkpoint files/evidence, and advance the graph."""
        normalized_files, manifest_hash = self._normalize_checkpoint_files(files)
        normalized_evidence = [dict(item) for item in evidence]
        if not normalized_evidence:
            raise ValueError("verified checkpoint requires passed evidence")
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if run.cancel_requested_at is not None:
                raise RunLeaseLost("run cancellation was requested before checkpoint")
            graph, revision, nodes, target = await self._goal_write_context(
                session, run_id, goal_id
            )
            transition_goal_status(GoalStatus(target.status), GoalStatus.VERIFIED)
            required_keys = {
                acceptance_persistence_key(goal_id, item["id"])
                for item in target.acceptance.get("criteria", [])
            }
            supplied_keys: set[str] = set()
            evidence_keys: set[tuple[str, str]] = set()
            for item in normalized_evidence:
                if item.get("status") != "passed":
                    raise ValueError("verified checkpoint accepts passed evidence only")
                acceptance_key = item.get("acceptanceKey", item.get("acceptance_key"))
                kind = item.get("kind")
                if not isinstance(acceptance_key, str) or not isinstance(kind, str) or not kind:
                    raise ValueError("evidence requires acceptanceKey and kind")
                if acceptance_key not in required_keys:
                    raise ValueError("evidence acceptanceKey is outside the goal scope")
                if (acceptance_key, kind) in evidence_keys:
                    raise ValueError("duplicate checkpoint evidence")
                evidence_keys.add((acceptance_key, kind))
                supplied_keys.add(acceptance_key)
            if supplied_keys != required_keys:
                raise ValueError("every goal acceptance criterion requires passed evidence")

            ordinal = (
                int(
                    await session.scalar(
                        select(func.coalesce(func.max(CheckpointRecord.ordinal), 0)).where(
                            CheckpointRecord.graph_id == graph.id
                        )
                    )
                    or 0
                )
                + 1
            )
            capsule_payload = dict(capsule or {})
            checkpoint = CheckpointRecord(
                id=uuid7(),
                graph_id=graph.id,
                revision_id=revision.id,
                goal_node_id=target.id,
                project_id=run.project_id,
                run_id=run.id,
                ordinal=ordinal,
                manifest_hash=manifest_hash,
                commit_sha=commit_sha,
                snapshot_id=snapshot_id,
                capsule=capsule_payload,
            )
            session.add(checkpoint)
            # IDs are assigned client-side, but without ORM relationships the
            # unit of work cannot infer instance-level ordering. Flush the
            # checkpoint parent before its manifest/evidence children so
            # SQLite foreign-key enforcement matches PostgreSQL.
            await session.flush()
            for item in normalized_files:
                session.add(
                    CheckpointFileRecord(
                        id=uuid7(),
                        checkpoint_id=checkpoint.id,
                        path=item.path,
                        sha256=item.sha256,
                        size=item.size,
                        content_text=item.content_text,
                    )
                )
            for item in normalized_evidence:
                artifact_id = item.get("artifactId", item.get("artifact_id"))
                if artifact_id is not None:
                    artifact = await session.get(ArtifactRecord, artifact_id)
                    if artifact is None or artifact.run_id != run.id:
                        raise ValueError("checkpoint evidence artifact must belong to the same run")
                session.add(
                    GoalEvidenceRecord(
                        id=uuid7(),
                        checkpoint_id=checkpoint.id,
                        graph_id=graph.id,
                        goal_node_id=target.id,
                        project_id=run.project_id,
                        run_id=run.id,
                        acceptance_key=item.get("acceptanceKey", item.get("acceptance_key")),
                        kind=item["kind"],
                        status="passed",
                        artifact_id=artifact_id,
                        reference=item.get("reference", item.get("ref")),
                        summary=str(item.get("summary", "")),
                        payload=dict(item.get("payload") or {}),
                    )
                )

            paths_by_goal = capsule_payload.get("goalChangedPathsByGoal")
            raw_goal_paths = (
                paths_by_goal.get(goal_id, []) if isinstance(paths_by_goal, dict) else []
            )
            snapshot_paths = {item.path for item in normalized_files}
            implementation_paths = sorted(
                {
                    self._validated_file_path(path)
                    for path in raw_goal_paths
                    if isinstance(path, str)
                    and path in snapshot_paths
                    and _is_business_implementation_path(path)
                }
            )
            acceptance_keys = sorted(required_keys)
            existing_pairs = (
                {
                    (source_ref, target_ref)
                    for source_ref, target_ref in (
                        await session.execute(
                            select(
                                TraceLinkRecord.source_ref,
                                TraceLinkRecord.target_ref,
                            ).where(
                                TraceLinkRecord.run_id == run.id,
                                TraceLinkRecord.source_kind == "acceptance_criterion",
                                TraceLinkRecord.source_ref.in_(acceptance_keys),
                                TraceLinkRecord.relation == "implemented_in",
                                TraceLinkRecord.target_kind == "file",
                                TraceLinkRecord.target_ref.in_(implementation_paths),
                            )
                        )
                    ).all()
                }
                if acceptance_keys and implementation_paths
                else set()
            )
            for acceptance_key in acceptance_keys:
                for path in implementation_paths:
                    if (acceptance_key, path) in existing_pairs:
                        continue
                    trace_link = TraceLinkRecord(
                        id=uuid7(),
                        run_id=run.id,
                        source_kind="acceptance_criterion",
                        source_ref=acceptance_key,
                        relation="implemented_in",
                        target_kind="file",
                        target_ref=path,
                        metadata_json={"goalId": goal_id, "source": "goal_checkpoint"},
                    )
                    session.add(trace_link)
                    await self._append_event_in_session(
                        session,
                        run,
                        "trace.updated",
                        payload={"traceLinkId": trace_link.id},
                    )

            target.status = GoalStatus.VERIFIED.value
            target.verified_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "checkpoint.recorded",
                payload={
                    "checkpointId": checkpoint.id,
                    "graphId": graph.id,
                    "goalId": goal_id,
                    "manifestHash": manifest_hash,
                    "fileCount": len(normalized_files),
                },
            )
            next_goal = self._next_eligible_goal(nodes)
            if next_goal is None:
                if not all(node.status == GoalStatus.VERIFIED.value for node in nodes):
                    raise ConflictError(
                        "goal graph has pending nodes with unsatisfied dependencies"
                    )
                graph.status = transition_graph_status(
                    GraphStatus(graph.status), GraphStatus.VERIFIED
                ).value
                graph.updated_at = utcnow()
                await self._append_event_in_session(
                    session,
                    run,
                    "goal_graph.verified",
                    payload={"graphId": graph.id, "revision": revision.revision},
                )
            else:
                next_goal.status = transition_goal_status(
                    GoalStatus(next_goal.status), GoalStatus.ACTIVE
                ).value
                await self._append_event_in_session(
                    session,
                    run,
                    "goal.activated",
                    payload={
                        "graphId": graph.id,
                        "goalId": next_goal.goal_key,
                        "revision": revision.revision,
                    },
                )
            goal_graph = await self._goal_graph_read_projection_in_session(session, run_id)
            await self._append_event_in_session(
                session,
                run,
                "goal.verified",
                payload={
                    "graphId": graph.id,
                    "goalId": goal_id,
                    "checkpointId": checkpoint.id,
                    "goalGraph": goal_graph,
                },
            )
            await session.commit()
            return await self._checkpoint_projection_in_session(
                session, checkpoint, target.goal_key
            )

    async def get_latest_verified_checkpoint(self, run_id: str) -> VerifiedCheckpoint | None:
        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise NotFoundError("run not found")
            return await self._latest_verified_checkpoint_for_run_in_session(session, run)

    async def get_recent_verified_checkpoint(self, run_id: str) -> VerifiedCheckpoint | None:
        return await self.get_latest_verified_checkpoint(run_id)

    async def get_recovery_checkpoint(self, run_id: str) -> VerifiedCheckpoint | None:
        """Return the integrity-checked checkpoint selected for a recovery run.

        This is deliberately a durable cross-run lookup. It never resumes the
        source sandbox or Coding Agent session, and it fails closed if the
        lineage no longer matches the source run and project.
        """

        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise NotFoundError("run not found")
            return await self._recovery_checkpoint_for_run_in_session(session, run)

    async def record_usage_entry(
        self,
        run_id: str,
        request_id: str,
        *,
        lease_token: str,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        tool_calls: int = 0,
        cost_micros: int = 0,
        metadata: Mapping[str, Any] | None = None,
        goal_id: str | None = None,
    ) -> UsageLedgerResult:
        if not request_id.strip() or len(request_id) > 160:
            raise ValueError("request_id must be nonempty and bounded")
        counters = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            tool_calls,
            cost_micros,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("usage counters must be nonnegative integers")
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            existing = await session.scalar(
                select(UsageEntryRecord).where(
                    UsageEntryRecord.run_id == run_id,
                    UsageEntryRecord.request_id == request_id,
                )
            )
            if existing is not None:
                return UsageLedgerResult(entry_id=existing.id, created=False)
            graph = await session.scalar(
                select(GoalGraphRecord).where(GoalGraphRecord.run_id == run_id)
            )
            node = None
            if goal_id is not None:
                if graph is None:
                    raise ConflictError("P0 run has no goal for scoped usage")
                revision = await self._current_revision_in_session(session, graph)
                node = await session.scalar(
                    select(GoalNodeRecord).where(
                        GoalNodeRecord.revision_id == revision.id,
                        GoalNodeRecord.goal_key == goal_id,
                    )
                )
                if node is None:
                    raise NotFoundError("goal not found")
            record = UsageEntryRecord(
                id=uuid7(),
                project_id=run.project_id,
                run_id=run_id,
                graph_id=graph.id if graph else None,
                goal_node_id=node.id if node else None,
                request_id=request_id,
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                tool_calls=tool_calls,
                cost_micros=cost_micros,
                metadata_json=dict(metadata or {}),
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(UsageEntryRecord).where(
                        UsageEntryRecord.run_id == run_id,
                        UsageEntryRecord.request_id == request_id,
                    )
                )
                if existing is None:
                    raise
                return UsageLedgerResult(entry_id=existing.id, created=False)
            return UsageLedgerResult(entry_id=record.id, created=True)

    async def get_usage_totals(self, run_id: str) -> UsageTotals:
        async with self.database.session_factory() as session:
            if await session.get(RunRecord, run_id) is None:
                raise NotFoundError("run not found")
            totals = await self._usage_totals_for_runs_in_session(session, [run_id])
            return totals.get(run_id, UsageTotals())

    @staticmethod
    async def _usage_totals_for_runs_in_session(
        session: AsyncSession,
        run_ids: Iterable[str],
    ) -> dict[str, UsageTotals]:
        unique_run_ids = tuple(dict.fromkeys(run_ids))
        if not unique_run_ids:
            return {}
        rows = (
            await session.execute(
                select(
                    UsageEntryRecord.run_id,
                    func.coalesce(func.sum(UsageEntryRecord.input_tokens), 0),
                    func.coalesce(func.sum(UsageEntryRecord.output_tokens), 0),
                    func.coalesce(func.sum(UsageEntryRecord.cache_read_tokens), 0),
                    func.coalesce(func.sum(UsageEntryRecord.cache_write_tokens), 0),
                    func.coalesce(func.sum(UsageEntryRecord.tool_calls), 0),
                    func.coalesce(func.sum(UsageEntryRecord.cost_micros), 0),
                )
                .where(UsageEntryRecord.run_id.in_(unique_run_ids))
                .group_by(UsageEntryRecord.run_id)
            )
        ).all()
        metadata_rows = (
            await session.execute(
                select(
                    UsageEntryRecord.run_id,
                    UsageEntryRecord.metadata_json,
                ).where(UsageEntryRecord.run_id.in_(unique_run_ids))
            )
        ).all()
        incomplete_run_ids = {
            str(run_id)
            for run_id, metadata in metadata_rows
            if isinstance(metadata, dict) and metadata.get("_usageSettled") is False
        }
        return {
            str(row[0]): UsageTotals(
                input_tokens=int(row[1]),
                output_tokens=int(row[2]),
                cache_read_tokens=int(row[3]),
                cache_write_tokens=int(row[4]),
                tool_calls=int(row[5]),
                cost_micros=int(row[6]),
            )
            for row in rows
            if str(row[0]) not in incomplete_run_ids
        }

    async def record_usage(self, run_id: str, request_id: str, **values: Any) -> UsageLedgerResult:
        return await self.record_usage_entry(run_id, request_id, **values)

    async def reserve_usage_entry(
        self,
        run_id: str,
        request_id: str,
        *,
        lease_token: str,
        provider: str,
        model: str,
        metadata: Mapping[str, Any] | None = None,
        goal_id: str | None = None,
    ) -> str:
        """Reserve one provider request while the caller owns the run lease."""
        if not request_id.strip() or len(request_id) > 160:
            raise ValueError("request_id must be nonempty and bounded")
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            existing = await session.scalar(
                select(UsageEntryRecord).where(
                    UsageEntryRecord.run_id == run_id,
                    UsageEntryRecord.request_id == request_id,
                )
            )
            if existing is not None:
                token = existing.metadata_json.get("_usageReservationToken")
                if (
                    not isinstance(token, str)
                    or existing.provider != provider
                    or existing.model != model
                ):
                    raise ConflictError("usage request id is already bound differently")
                return token

            graph = await session.scalar(
                select(GoalGraphRecord).where(GoalGraphRecord.run_id == run_id)
            )
            node = None
            if goal_id is not None:
                if graph is None:
                    raise ConflictError("P0 run has no goal for scoped usage")
                revision = await self._current_revision_in_session(session, graph)
                node = await session.scalar(
                    select(GoalNodeRecord).where(
                        GoalNodeRecord.revision_id == revision.id,
                        GoalNodeRecord.goal_key == goal_id,
                    )
                )
                if node is None:
                    raise NotFoundError("goal not found")
            reservation_token = uuid7()
            reservation_metadata = dict(metadata or {})
            reservation_metadata.update(
                {
                    "_usageReservationToken": reservation_token,
                    "_usageSettled": False,
                }
            )
            session.add(
                UsageEntryRecord(
                    id=uuid7(),
                    project_id=run.project_id,
                    run_id=run_id,
                    graph_id=graph.id if graph else None,
                    goal_node_id=node.id if node else None,
                    request_id=request_id,
                    provider=provider,
                    model=model,
                    metadata_json=reservation_metadata,
                )
            )
            await session.commit()
            return reservation_token

    async def settle_usage_entry(
        self,
        run_id: str,
        request_id: str,
        *,
        usage_token: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        tool_calls: int = 0,
        cost_micros: int = 0,
    ) -> UsageLedgerResult:
        """Settle actual usage after provider work, even if the run lease was lost."""
        counters = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            tool_calls,
            cost_micros,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("usage counters must be nonnegative integers")
        async with self.database.session_factory() as session:
            record = await session.scalar(
                select(UsageEntryRecord)
                .where(
                    UsageEntryRecord.run_id == run_id,
                    UsageEntryRecord.request_id == request_id,
                )
                .with_for_update()
            )
            if record is None:
                raise NotFoundError("usage reservation not found")
            metadata = dict(record.metadata_json)
            if metadata.get("_usageReservationToken") != usage_token:
                raise ConflictError("usage reservation token does not match")
            recorded = (
                record.input_tokens,
                record.output_tokens,
                record.cache_read_tokens,
                record.cache_write_tokens,
                record.tool_calls,
                record.cost_micros,
            )
            if metadata.get("_usageSettled") is True:
                if recorded != counters:
                    raise ConflictError("usage request was already settled differently")
                return UsageLedgerResult(entry_id=record.id, created=False)
            (
                record.input_tokens,
                record.output_tokens,
                record.cache_read_tokens,
                record.cache_write_tokens,
                record.tool_calls,
                record.cost_micros,
            ) = counters
            metadata["_usageSettled"] = True
            record.metadata_json = metadata
            await session.commit()
            return UsageLedgerResult(entry_id=record.id, created=True)

    async def list_planning_cache_candidates(
        self,
        project_id: str,
        requirement: str,
        base_version_id: str | None,
        starter_fingerprint: dict[str, Any],
        *,
        limit: int = 3,
    ) -> list[dict[str, str]]:
        """Return bounded prior planning outputs for exact-input revalidation.

        Cache entries remain untrusted text: the current Direct Pi contract
        must parse and validate each candidate before it can be reused.

        A prior run qualifies by input fingerprint (project, requirement,
        base version, starter fingerprint) — regardless of its final phase or
        status — so runs that failed later in building/verifying still
        contribute their planning output. The machine truth is the pair of
        already-validated ``build_plan`` and ``acceptance_contract``
        artifacts, never the public (display-capped) ``pi.message.completed``
        text; a run without either artifact is skipped. The combined JSON is
        re-validated by the current PlanningBundle parser before reuse. This
        is a bounded cache lookup, not a fingerprint guarantee; there is no
        separate schemaVersion field on the artifacts.
        """
        async with self.database.session_factory() as session:
            runs = list(
                await session.scalars(
                    select(RunRecord)
                    .join(MessageRecord, MessageRecord.run_id == RunRecord.id)
                    .where(
                        RunRecord.project_id == project_id,
                        RunRecord.base_version_id == base_version_id,
                        MessageRecord.content == requirement,
                    )
                    .order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
                    .limit(max(1, min(limit, 10)))
                )
            )
            candidates: list[dict[str, str]] = []
            for run in runs:
                run_input = await session.scalar(
                    select(ArtifactRecord)
                    .where(
                        ArtifactRecord.run_id == run.id,
                        ArtifactRecord.kind == "run_input",
                    )
                    .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
                    .limit(1)
                )
                if run_input is None or any(
                    run_input.content.get(key) != value
                    for key, value in starter_fingerprint.items()
                ):
                    continue
                # Machine cache truth is the pair of already-validated
                # planning artifacts (never display-capped message text).
                build_plan = await session.scalar(
                    select(ArtifactRecord)
                    .where(
                        ArtifactRecord.run_id == run.id,
                        ArtifactRecord.kind == "build_plan",
                    )
                    .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
                    .limit(1)
                )
                acceptance = await session.scalar(
                    select(ArtifactRecord)
                    .where(
                        ArtifactRecord.run_id == run.id,
                        ArtifactRecord.kind == "acceptance_contract",
                    )
                    .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
                    .limit(1)
                )
                if build_plan is None or acceptance is None:
                    continue
                if not isinstance(build_plan.content, dict) or not isinstance(
                    acceptance.content, dict
                ):
                    continue
                candidates.append(
                    {
                        "runId": run.id,
                        "text": json.dumps(
                            {
                                "buildPlan": build_plan.content,
                                "acceptanceContract": acceptance.content,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
            return candidates

    async def list_goal_graph_cache_candidates(
        self,
        project_id: str,
        requirement: str,
        base_version_id: str | None,
        starter_fingerprint: dict[str, Any],
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Return prior machine GoalGraph drafts for exact-input revalidation.

        A run that planned successfully but failed later in Build is still a
        useful cache source. The current runtime revalidates the stored draft
        before creating a new, independently owned graph for the new run.
        """

        async with self.database.session_factory() as session:
            candidate_limit = max(20, min(limit * 10, 100))
            runs = list(
                await session.scalars(
                    select(RunRecord)
                    .join(MessageRecord, MessageRecord.run_id == RunRecord.id)
                    .where(
                        RunRecord.project_id == project_id,
                        RunRecord.base_version_id == base_version_id,
                        MessageRecord.content == requirement,
                    )
                    .order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
                    # Artifact filtering happens below. Over-fetch a bounded
                    # history so recent failed planning attempts cannot hide
                    # the last valid machine draft.
                    .limit(candidate_limit)
                )
            )
            candidates: list[dict[str, Any]] = []
            for run in runs:
                run_input = await session.scalar(
                    select(ArtifactRecord)
                    .where(
                        ArtifactRecord.run_id == run.id,
                        ArtifactRecord.kind == "run_input",
                    )
                    .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
                    .limit(1)
                )
                if run_input is None or any(
                    run_input.content.get(key) != value
                    for key, value in starter_fingerprint.items()
                ):
                    continue
                graph = await session.scalar(
                    select(ArtifactRecord)
                    .where(
                        ArtifactRecord.run_id == run.id,
                        ArtifactRecord.kind == "goal_graph",
                    )
                    .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
                    .limit(1)
                )
                if graph is None or not isinstance(graph.content, dict):
                    continue
                candidates.append({"runId": run.id, "draft": dict(graph.content)})
                if len(candidates) >= max(1, min(limit, 10)):
                    break
            return candidates

    async def require_run_for_project(
        self, run_id: str, project_id: str, owner_session_id: str | None = None
    ) -> RunRecord:
        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id)
            if run is None or run.project_id != project_id:
                raise NotFoundError("run not found")
            if owner_session_id is not None:
                await self._require_project_in_session(session, project_id, owner_session_id)
            return run

    async def list_events(
        self, run_id: str, after: int = 0, limit: int = 500
    ) -> list[EventEnvelope]:
        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise NotFoundError("run not found")
            records = list(
                await session.scalars(
                    select(RunEventRecord)
                    .where(RunEventRecord.run_id == run_id, RunEventRecord.seq > after)
                    .order_by(RunEventRecord.seq.asc())
                    .limit(limit)
                )
            )
            return [self._event_envelope(run, record) for record in records]

    async def request_cancel(self, run_id: str) -> RunResponse:
        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id, with_for_update=True)
            if run is None:
                raise NotFoundError("run not found")
            if run.status in {
                RunStatus.queued.value,
                RunStatus.waiting_for_user.value,
            }:
                pending = await self._pending_input_request_in_session(session, run_id)
                if pending is not None:
                    pending.status = "cancelled"
                run.status = RunStatus.cancelled.value
                if run.phase == RunPhase.queued.value:
                    run.phase = RunPhase.queued.value
                run.cancel_requested_at = utcnow()
                run.lease_owner = None
                run.lease_expires_at = None
                run.continuation_request_id = None
                run.continuation_key = None
                run.continuation_stage = None
                run.continuation_goal_id = None
                run.continuation_context = None
                await self._append_event_in_session(session, run, "run.cancel_requested")
                await self._append_event_in_session(session, run, "run.cancelled")
                await self._advance_project_after_terminal(session, run)
            elif run.status not in TERMINAL_STATUSES:
                run.cancel_requested_at = utcnow()
                await self._append_event_in_session(session, run, "run.cancel_requested")
            await session.commit()
            return await self._run_with_seq(session, run)

    async def is_cancel_requested(self, run_id: str) -> bool:
        async with self.database.session_factory() as session:
            record = await session.get(RunRecord, run_id)
            return record is None or record.cancel_requested_at is not None

    async def is_user_input_suspended(self, run_id: str) -> bool:
        """True only for the clean wait/answer handoff after a lease release."""

        async with self.database.session_factory() as session:
            record = await session.get(RunRecord, run_id)
            return bool(
                record is not None
                and record.continuation_request_id is not None
                and record.status
                in {
                    RunStatus.waiting_for_user.value,
                    RunStatus.queued.value,
                }
            )

    async def get_active_lease_token(self, run_id: str) -> str:
        """Return the opaque token of an active lease for internal SOP entrypoints."""
        async with self.database.session_factory() as session:
            record = await session.get(RunRecord, run_id)
            if (
                record is None
                or record.status != RunStatus.running.value
                or not record.lease_owner
                or record.lease_expires_at is None
                or _is_expired(record.lease_expires_at)
            ):
                raise RunLeaseLost("run lease is no longer active")
            return record.lease_owner

    async def has_claimable_run(self) -> bool:
        """Return whether the paid runtime preflight can lead to a real claim.

        This read-only hint mirrors ``claim_next_run``'s resource and
        single-writer filters. The later locked claim remains authoritative.
        """

        async with self.database.session_factory() as session:
            live_resources = (
                select(RunSandboxResourceRecord.id)
                .where(
                    RunSandboxResourceRecord.run_id == RunRecord.id,
                    RunSandboxResourceRecord.cleaned_at.is_(None),
                )
                .correlate(RunRecord)
            )
            exact_continuation_resource = (
                select(RunSandboxResourceRecord.id)
                .where(
                    RunSandboxResourceRecord.run_id == RunRecord.id,
                    RunSandboxResourceRecord.cleaned_at.is_(None),
                    RunSandboxResourceRecord.kind == "generation",
                    RunSandboxResourceRecord.sandbox_id == RunRecord.sandbox_id,
                )
                .correlate(RunRecord)
            )
            conflicting_continuation_resource = (
                select(RunSandboxResourceRecord.id)
                .where(
                    RunSandboxResourceRecord.run_id == RunRecord.id,
                    RunSandboxResourceRecord.cleaned_at.is_(None),
                    or_(
                        RunSandboxResourceRecord.kind != "generation",
                        RunSandboxResourceRecord.sandbox_id != RunRecord.sandbox_id,
                    ),
                )
                .correlate(RunRecord)
            )
            resumable_continuation = and_(
                RunRecord.continuation_request_id.is_not(None),
                RunRecord.sandbox_id.is_not(None),
                exact_continuation_resource.exists(),
                ~conflicting_continuation_resource.exists(),
            )
            candidates = list(
                await session.scalars(
                    select(RunRecord)
                    .where(
                        RunRecord.status == RunStatus.queued.value,
                        or_(~live_resources.exists(), resumable_continuation),
                    )
                    .order_by(RunRecord.created_at.asc())
                )
            )
            for run in candidates:
                active_writer_count = await session.scalar(
                    select(func.count())
                    .select_from(RunRecord)
                    .where(
                        RunRecord.project_id == run.project_id,
                        RunRecord.id != run.id,
                        RunRecord.status.in_(
                            [
                                RunStatus.running.value,
                                RunStatus.waiting_for_user.value,
                            ]
                        ),
                    )
                )
                if not active_writer_count:
                    return True
            return False

    async def is_active_lease(self, run_id: str, lease_token: str) -> bool:
        """Read-only fast path used by SOP cancellation checkpoints."""
        now = utcnow()
        async with self.database.session_factory() as session:
            run_id_match = await session.scalar(
                select(RunRecord.id).where(
                    RunRecord.id == run_id,
                    RunRecord.status == RunStatus.running.value,
                    RunRecord.lease_owner == lease_token,
                    RunRecord.lease_expires_at.is_not(None),
                    RunRecord.lease_expires_at > now,
                )
            )
            return run_id_match is not None

    async def claim_next_run(self, worker_id: str, lease_seconds: int) -> RunRecord | None:
        """Claim one queued run without allowing two writers for a project.

        PostgreSQL uses row locks; the extra status check also keeps the SQLite
        test implementation deterministic.
        """
        async with self.database.session_factory() as session:
            live_resources = (
                select(RunSandboxResourceRecord.id)
                .where(
                    RunSandboxResourceRecord.run_id == RunRecord.id,
                    RunSandboxResourceRecord.cleaned_at.is_(None),
                )
                .correlate(RunRecord)
            )
            exact_continuation_resource = (
                select(RunSandboxResourceRecord.id)
                .where(
                    RunSandboxResourceRecord.run_id == RunRecord.id,
                    RunSandboxResourceRecord.cleaned_at.is_(None),
                    RunSandboxResourceRecord.kind == "generation",
                    RunSandboxResourceRecord.sandbox_id == RunRecord.sandbox_id,
                )
                .correlate(RunRecord)
            )
            conflicting_continuation_resource = (
                select(RunSandboxResourceRecord.id)
                .where(
                    RunSandboxResourceRecord.run_id == RunRecord.id,
                    RunSandboxResourceRecord.cleaned_at.is_(None),
                    or_(
                        RunSandboxResourceRecord.kind != "generation",
                        RunSandboxResourceRecord.sandbox_id != RunRecord.sandbox_id,
                    ),
                )
                .correlate(RunRecord)
            )
            resumable_continuation = and_(
                RunRecord.continuation_request_id.is_not(None),
                RunRecord.sandbox_id.is_not(None),
                exact_continuation_resource.exists(),
                ~conflicting_continuation_resource.exists(),
            )
            candidates = list(
                await session.scalars(
                    select(RunRecord)
                    .where(
                        RunRecord.status == RunStatus.queued.value,
                        or_(~live_resources.exists(), resumable_continuation),
                    )
                    .order_by(RunRecord.created_at.asc())
                    .with_for_update(skip_locked=True)
                )
            )
            for run in candidates:
                active_writer_count = await session.scalar(
                    select(func.count())
                    .select_from(RunRecord)
                    .where(
                        RunRecord.project_id == run.project_id,
                        RunRecord.id != run.id,
                        RunRecord.status.in_(
                            [
                                RunStatus.running.value,
                                RunStatus.waiting_for_user.value,
                            ]
                        ),
                    )
                )
                if active_writer_count:
                    continue
                # Worker identity is intentionally not a fence: every claim
                # gets an opaque, per-run token so an old process cannot write
                # through a later worker merely because it shares a hostname.
                lease_token = uuid7()
                run.status = RunStatus.running.value
                run.phase = RunPhase.product_analysis.value
                if run.continuation_request_id is not None:
                    # User wait time is outside the active execution budget;
                    # durable token/tool/spend ledgers still cap repeated turns.
                    run.execution_started_at = utcnow()
                elif run.execution_started_at is None:
                    run.execution_started_at = utcnow()
                run.lease_owner = lease_token
                run.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
                project = await session.get(ProjectRecord, run.project_id, with_for_update=True)
                if project is not None:
                    project.active_run_id = run.id
                    project.status = "running"
                    project.updated_at = utcnow()
                await self._append_event_in_session(
                    session,
                    run,
                    "run.status_changed",
                    payload={"status": run.status, "phase": run.phase},
                )
                await session.commit()
                return run
            return None

    async def renew_lease(self, run_id: str, lease_token: str, lease_seconds: int) -> bool:
        """Extend one claimed run only while this worker still owns it.

        The predicate is deliberately part of the update instead of a
        read-then-write check: an old worker must never revive an expired,
        terminalized, or reassigned lease.
        """
        if lease_seconds <= 0:
            raise ValueError("worker lease must be positive")
        now = utcnow()
        async with self.database.session_factory() as session:
            result = await session.execute(
                update(RunRecord)
                .where(
                    RunRecord.id == run_id,
                    RunRecord.status == RunStatus.running.value,
                    RunRecord.lease_owner == lease_token,
                    RunRecord.lease_expires_at.is_not(None),
                    RunRecord.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
            )
            await session.commit()
            return result.rowcount == 1

    async def recover_expired_running_runs(
        self, *, now: datetime | None = None
    ) -> list[SandboxCleanupTarget]:
        """Recover abandoned runs and return stale sandboxes for destruction.

        P0 runs retain the terminal failure/cancellation behavior. P1 runs
        with an integrity-checked verified checkpoint are requeued so a fresh
        sandbox/session can resume; registered resources fence the next claim
        until cleanup is acknowledged. A cancellation request always wins.
        """
        cutoff = now or utcnow()
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        recovered: list[SandboxCleanupTarget] = []
        async with self.database.session_factory() as session:
            candidates = list(
                await session.scalars(
                    select(RunRecord)
                    .where(
                        RunRecord.status == RunStatus.running.value,
                        or_(
                            RunRecord.lease_expires_at.is_(None),
                            RunRecord.lease_expires_at <= cutoff,
                        ),
                    )
                    .order_by(RunRecord.updated_at.asc(), RunRecord.created_at.asc())
                    .with_for_update(skip_locked=True)
                )
            )
            for run in candidates:
                graph = await session.scalar(
                    select(GoalGraphRecord)
                    .where(
                        GoalGraphRecord.run_id == run.id,
                        GoalGraphRecord.status.in_(
                            [GraphStatus.ACTIVE.value, GraphStatus.VERIFIED.value]
                        ),
                    )
                    .with_for_update()
                )
                current_goal: GoalNodeRecord | None = None
                terminal_nodes: list[GoalNodeRecord] = []
                latest_checkpoint: CheckpointRecord | None = None
                checkpoint_goal: GoalNodeRecord | None = None
                checkpoint_valid = False
                if graph is not None:
                    revision = await self._current_revision_in_session(session, graph)
                    current_goal = await session.scalar(
                        select(GoalNodeRecord)
                        .where(
                            GoalNodeRecord.revision_id == revision.id,
                            GoalNodeRecord.status.in_(
                                [GoalStatus.ACTIVE.value, GoalStatus.CLAIMED.value]
                            ),
                        )
                        .with_for_update()
                    )
                    latest_checkpoint = await session.scalar(
                        select(CheckpointRecord)
                        .where(CheckpointRecord.graph_id == graph.id)
                        .order_by(CheckpointRecord.ordinal.desc())
                        .limit(1)
                    )
                    if latest_checkpoint is not None:
                        checkpoint_goal = await session.get(
                            GoalNodeRecord, latest_checkpoint.goal_node_id
                        )
                        if checkpoint_goal is not None:
                            try:
                                checkpoint_projection = (
                                    await self._checkpoint_projection_in_session(
                                        session,
                                        latest_checkpoint,
                                        checkpoint_goal.goal_key,
                                    )
                                )
                                checkpoint_valid = bool(checkpoint_projection.evidence)
                            except ManifestIntegrityError:
                                checkpoint_valid = False
                if (
                    graph is not None
                    and latest_checkpoint is not None
                    and checkpoint_goal is not None
                    and checkpoint_valid
                    and (graph.status == GraphStatus.VERIFIED.value or current_goal is not None)
                    and run.cancel_requested_at is None
                ):
                    legacy_sandbox_id = run.sandbox_id
                    result = await session.execute(
                        update(RunRecord)
                        .where(
                            RunRecord.id == run.id,
                            RunRecord.status == RunStatus.running.value,
                            RunRecord.cancel_requested_at.is_(None),
                            or_(
                                RunRecord.lease_expires_at.is_(None),
                                RunRecord.lease_expires_at <= cutoff,
                            ),
                        )
                        .values(
                            status=RunStatus.queued.value,
                            phase=RunPhase.queued.value,
                            error_code=None,
                            lease_owner=None,
                            lease_expires_at=None,
                            sandbox_id=None,
                            preview_url=None,
                            updated_at=utcnow(),
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount != 1:
                        continue
                    await session.refresh(run)
                    project = await session.get(ProjectRecord, run.project_id, with_for_update=True)
                    if project is not None:
                        project.active_run_id = run.id
                        project.status = "queued"
                        project.updated_at = utcnow()
                    await self._append_event_in_session(
                        session,
                        run,
                        "goal.resume_scheduled",
                        payload={
                            "graphId": graph.id,
                            "goalId": (
                                current_goal.goal_key
                                if current_goal is not None
                                else checkpoint_goal.goal_key
                            ),
                            "checkpointId": latest_checkpoint.id,
                            "graphStatus": graph.status,
                        },
                    )
                    resources = list(
                        await session.scalars(
                            select(RunSandboxResourceRecord).where(
                                RunSandboxResourceRecord.run_id == run.id,
                                RunSandboxResourceRecord.cleaned_at.is_(None),
                            )
                        )
                    )
                    recovered.extend(
                        SandboxCleanupTarget(
                            run_id=run.id,
                            project_id=run.project_id,
                            sandbox_id=resource.sandbox_id,
                            resource_id=resource.id,
                            kind=resource.kind,
                        )
                        for resource in resources
                    )
                    if legacy_sandbox_id and not any(
                        resource.sandbox_id == legacy_sandbox_id for resource in resources
                    ):
                        recovered.append(
                            SandboxCleanupTarget(
                                run_id=run.id,
                                project_id=run.project_id,
                                sandbox_id=legacy_sandbox_id,
                            )
                        )
                    continue

                if graph is not None and graph.status == GraphStatus.ACTIVE.value:
                    revision = await self._current_revision_in_session(session, graph)
                    terminal_nodes = list(
                        await session.scalars(
                            select(GoalNodeRecord)
                            .where(GoalNodeRecord.revision_id == revision.id)
                            .with_for_update()
                        )
                    )
                # The compare-and-set update is the real recovery claim. It
                # protects SQLite too, where SELECT FOR UPDATE is advisory.
                # Reading cancel_requested inside SQL makes a request that
                # arrived while waiting for the lock win over lease failure.
                cancelled = RunRecord.cancel_requested_at.is_not(None)
                result = await session.execute(
                    update(RunRecord)
                    .where(
                        RunRecord.id == run.id,
                        RunRecord.status == RunStatus.running.value,
                        or_(
                            RunRecord.lease_expires_at.is_(None),
                            RunRecord.lease_expires_at <= cutoff,
                        ),
                    )
                    .values(
                        status=case(
                            (cancelled, RunStatus.cancelled.value),
                            else_=RunStatus.failed.value,
                        ),
                        error_code=case(
                            (cancelled, None),
                            else_="worker_lease_expired",
                        ),
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=utcnow(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    continue
                await session.refresh(run)
                if graph is not None and graph.status == GraphStatus.ACTIVE.value:
                    graph_target = (
                        GraphStatus.CANCELLED
                        if run.status == RunStatus.cancelled.value
                        else GraphStatus.FAILED
                    )
                    for node in terminal_nodes:
                        current = GoalStatus(node.status)
                        if current in {GoalStatus.ACTIVE, GoalStatus.CLAIMED}:
                            node_target = (
                                GoalStatus.SUPERSEDED
                                if graph_target == GraphStatus.CANCELLED
                                else GoalStatus.FAILED
                            )
                            node.status = transition_goal_status(current, node_target).value
                        elif current == GoalStatus.PENDING:
                            node.status = transition_goal_status(
                                current, GoalStatus.SUPERSEDED
                            ).value
                    graph.status = transition_graph_status(
                        GraphStatus(graph.status), graph_target
                    ).value
                    graph.updated_at = utcnow()
                    goal_graph = await self._goal_graph_read_projection_in_session(session, run.id)
                    await self._append_event_in_session(
                        session,
                        run,
                        f"goal_graph.{graph_target.value}",
                        payload={
                            "graphId": graph.id,
                            "status": graph_target.value,
                            "reason": "worker lease expired",
                            "goalGraph": goal_graph,
                        },
                    )
                summary = (
                    "Cancelled after the worker lease expired."
                    if run.status == RunStatus.cancelled.value
                    else "The worker lease expired before the run completed."
                )
                event_kind = (
                    "run.cancelled" if run.status == RunStatus.cancelled.value else "run.failed"
                )
                await self._append_event_in_session(
                    session,
                    run,
                    event_kind,
                    payload=_terminal_event_payload(
                        status=run.status,
                        error_code=run.error_code,
                        summary=summary,
                    ),
                )
                await self._advance_project_after_terminal(session, run)
                # A failed/needs-attention run may deliberately retain its
                # unverified preview for inspection. Cancellation always
                # destroys the sandbox; failures without a preview do too.
                if run.sandbox_id and (
                    run.status == RunStatus.cancelled.value or run.preview_url is None
                ):
                    recovered.append(
                        SandboxCleanupTarget(
                            run_id=run.id,
                            project_id=run.project_id,
                            sandbox_id=run.sandbox_id,
                        )
                    )
            await session.commit()
        return recovered

    async def list_terminal_sandbox_cleanup_targets(self) -> list[SandboxCleanupTarget]:
        """Return failed/cancelled sandboxes left by a worker crash.

        Recovery normally destroys a sandbox immediately. Keeping this small
        durable sweep closes the crash window between terminalization and the
        provider call without ever touching successful preview sandboxes.
        """
        async with self.database.session_factory() as session:
            records = list(
                await session.scalars(
                    select(RunRecord)
                    .where(
                        RunRecord.status.in_(
                            (
                                RunStatus.failed.value,
                                RunStatus.cancelled.value,
                                RunStatus.needs_attention.value,
                            )
                        ),
                        RunRecord.sandbox_id.is_not(None),
                    )
                    .order_by(RunRecord.updated_at.asc())
                )
            )
            return [
                SandboxCleanupTarget(
                    run_id=record.id,
                    project_id=record.project_id,
                    sandbox_id=record.sandbox_id,
                )
                for record in records
                if record.sandbox_id is not None
                and (record.status == RunStatus.cancelled.value or record.preview_url is None)
            ]

    async def clear_sandbox_id(
        self,
        run_id: str,
        sandbox_id: str,
        *,
        lease_token: str | None = None,
    ) -> bool:
        """Forget an exact sandbox reference only after a successful destroy.

        SOP-owned cleanup must prove the same active lease as every other
        durable SOP write. Recovery cleanup intentionally omits the token:
        it operates only on an already-terminal exact sandbox reference.
        """
        async with self.database.session_factory() as session:
            if lease_token is not None:
                await self._run_for_write(session, run_id, lease_token=lease_token)
            result = await session.execute(
                update(RunRecord)
                .where(RunRecord.id == run_id, RunRecord.sandbox_id == sandbox_id)
                .values(sandbox_id=None, updated_at=utcnow())
            )
            await session.commit()
            return result.rowcount == 1

    async def set_run_phase(
        self,
        run_id: str,
        phase: RunPhase,
        *,
        status: RunStatus | None = None,
        lease_token: str | None = None,
    ) -> None:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if status is not None:
                run.status = status.value
            run.phase = phase.value
            run.updated_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "run.status_changed",
                payload={"status": run.status, "phase": run.phase},
            )
            await session.commit()

    async def increment_repair_round(
        self,
        run_id: str,
        *,
        phase: RunPhase = RunPhase.repair,
        lease_token: str | None = None,
    ) -> int:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            run.repair_round += 1
            run.phase = phase.value
            run.updated_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "run.status_changed",
                payload={"status": run.status, "phase": run.phase, "repairRound": run.repair_round},
            )
            await session.commit()
            return run.repair_round

    async def mark_terminal(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error_code: str | None = None,
        summary: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        if status not in {
            RunStatus.succeeded,
            RunStatus.failed,
            RunStatus.cancelled,
            RunStatus.needs_attention,
        }:
            raise ValueError("mark_terminal requires a terminal status")
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if run.cancel_requested_at is not None and status != RunStatus.cancelled:
                status = RunStatus.cancelled
                error_code = None
                summary = "Cancelled safely by request."
            result = await session.execute(
                update(RunRecord)
                .where(
                    RunRecord.id == run_id,
                    RunRecord.status.not_in(TERMINAL_STATUSES),
                )
                .values(
                    status=status.value,
                    error_code=error_code,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=utcnow(),
                )
            )
            if result.rowcount != 1:
                return
            await session.refresh(run)
            event_kind = {
                RunStatus.succeeded: "run.completed",
                RunStatus.failed: "run.failed",
                RunStatus.cancelled: "run.cancelled",
                RunStatus.needs_attention: "run.failed",
            }[status]
            await self._append_event_in_session(
                session,
                run,
                event_kind,
                payload=_terminal_event_payload(
                    status=status.value,
                    error_code=error_code,
                    summary=summary or "",
                ),
            )
            await self._advance_project_after_terminal(session, run)
            await session.commit()

    async def append_event(
        self,
        run_id: str,
        kind: str,
        *,
        role: str | None = None,
        payload: dict[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> EventEnvelope:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            record = await self._append_event_in_session(
                session, run, kind, role=role, payload=payload
            )
            await session.commit()
            return self._event_envelope(run, record)

    async def store_artifact(
        self,
        run_id: str,
        kind: str,
        content: dict[str, Any],
        *,
        role: str | None = None,
        lease_token: str | None = None,
    ) -> str:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            record = ArtifactRecord(
                id=uuid7(), run_id=run_id, kind=kind, schema_version=1, content=content
            )
            session.add(record)
            await self._append_event_in_session(
                session,
                run,
                "artifact.upserted",
                role=role,
                payload={"artifactId": record.id, "kind": kind},
            )
            await session.commit()
            return record.id

    async def get_latest_artifact(self, run_id: str, kind: str) -> dict[str, Any] | None:
        async with self.database.session_factory() as session:
            record = await session.scalar(
                select(ArtifactRecord)
                .where(ArtifactRecord.run_id == run_id, ArtifactRecord.kind == kind)
                .order_by(ArtifactRecord.created_at.desc())
            )
            return None if record is None else dict(record.content)

    async def _artifact_refs_in_session(
        self, session: AsyncSession, run_id: str | None
    ) -> list[dict[str, Any]]:
        """Newest visible artifact per kind in the canonical Product then
        Architect order, for the snapshot's display run.
        """
        if run_id is None:
            return []
        refs: list[dict[str, Any]] = []
        for kind in VISIBLE_ARTIFACT_KIND_ORDER:
            record = await session.scalar(
                select(ArtifactRecord)
                .where(ArtifactRecord.run_id == run_id, ArtifactRecord.kind == kind)
                .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
                .limit(1)
            )
            if record is not None:
                refs.append(_artifact_ref_response(record))
        return refs

    async def get_artifact_detail(
        self, run_id: str, artifact_id: str, owner_session_id: str
    ) -> dict[str, Any]:
        """Return one visible artifact detail, or 404 for every failure.

        Ownership and visibility are checked inside this single operation so a
        cross-user, cross-run, hidden-kind, unknown-run or unknown-artifact
        request all fail closed with the same non-disclosing 404.
        """
        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise NotFoundError("artifact not found")
            project = await session.get(ProjectRecord, run.project_id)
            if project is None:
                raise NotFoundError("artifact not found")
            try:
                owned = await self._session_can_access_project(
                    session,
                    project,
                    owner_session_id,
                )
            except NotFoundError as exc:
                raise NotFoundError("artifact not found") from exc
            if not owned:
                raise NotFoundError("artifact not found")
            record = await session.get(ArtifactRecord, artifact_id)
            if record is None or record.run_id != run_id:
                raise NotFoundError("artifact not found")
            if record.kind not in VISIBLE_ARTIFACT_KIND_ORDER:
                raise NotFoundError("artifact not found")
            detail = _artifact_ref_response(record)
            detail["content"] = dict(record.content)
            return detail

    async def upsert_acceptance_items(
        self,
        project_id: str,
        run_id: str,
        items: Iterable[dict[str, Any]],
        *,
        lease_token: str | None = None,
    ) -> None:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if run.project_id != project_id:
                raise NotFoundError("run not found for project")
            for item in items:
                stable_key = str(item["id"])
                priority = str(item.get("priority", "must"))
                if priority not in {"must", "should", "could"}:
                    raise ValueError("acceptance priority is invalid")
                existing = await session.scalar(
                    select(SpecItemRecord).where(
                        SpecItemRecord.project_id == project_id,
                        SpecItemRecord.stable_key == stable_key,
                    )
                )
                if existing is None:
                    session.add(
                        SpecItemRecord(
                            id=uuid7(),
                            project_id=project_id,
                            stable_key=stable_key,
                            kind="acceptance_criterion",
                            priority=priority,
                            content=dict(item),
                            introduced_run_id=run_id,
                        )
                    )
                else:
                    existing.content = dict(item)
                    existing.priority = priority
                    existing.retired_run_id = None
            await session.commit()

    async def append_trace_link(
        self,
        run_id: str,
        source_kind: str,
        source_ref: str,
        relation: str,
        target_kind: str,
        target_ref: str,
        metadata: dict[str, Any] | None = None,
        *,
        lease_token: str | None = None,
    ) -> str:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            record = TraceLinkRecord(
                id=uuid7(),
                run_id=run_id,
                source_kind=source_kind,
                source_ref=source_ref,
                relation=relation,
                target_kind=target_kind,
                target_ref=target_ref,
                metadata_json=metadata or {},
            )
            session.add(record)
            await self._append_event_in_session(
                session, run, "trace.updated", payload={"traceLinkId": record.id}
            )
            await session.commit()
            return record.id

    async def record_evidence(
        self,
        run_id: str,
        acceptance_key: str,
        kind: str,
        status: str,
        summary: str,
        artifact_id: str | None = None,
        *,
        lease_token: str | None = None,
    ) -> str:
        if kind == "playwright_smoke":
            if status not in {"passed", "failed"}:
                raise ValueError("playwright_smoke evidence status must be passed or failed")
            if not artifact_id or not isinstance(artifact_id, str) or not artifact_id.strip():
                raise ValueError("playwright_smoke evidence requires a nonempty artifactId")
            self._validate_playwright_smoke_summary(
                run_id, acceptance_key, status, summary, artifact_id
            )
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if kind == "playwright_smoke":
                artifact = await session.get(ArtifactRecord, artifact_id)
                if artifact is None or artifact.run_id != run.id:
                    raise ValueError(
                        "playwright_smoke evidence artifactId must belong to the same run"
                    )
            record = VerificationEvidenceRecord(
                id=uuid7(),
                run_id=run_id,
                acceptance_key=acceptance_key,
                kind=kind,
                status=status,
                summary=summary,
                artifact_id=artifact_id,
            )
            session.add(record)
            scoped_goal_id: str | None = None
            local_acceptance_id = acceptance_key
            if ":" in acceptance_key:
                scoped_goal_id, local_acceptance_id = acceptance_key.split(":", 1)
            # Acceptance evidence always carries the closed-set acceptance
            # scope; the frontend Release gate consumes only project scope.
            event_payload: dict[str, Any] = {
                "evidenceId": record.id,
                "acceptanceId": local_acceptance_id,
                "status": status,
                "scope": "acceptance",
            }
            if scoped_goal_id is not None:
                event_payload.update(
                    {
                        "acceptanceKey": acceptance_key,
                        "goalId": scoped_goal_id,
                    }
                )
            await self._append_event_in_session(
                session,
                run,
                "verification.updated",
                role="reviewer",
                payload=event_payload,
            )
            await session.commit()
            return record.id

    @staticmethod
    def _validate_playwright_smoke_summary(
        run_id: str,
        acceptance_key: str,
        status: str,
        summary: str,
        artifact_id: str,
    ) -> None:
        """Bounded structured summary contract for playwright_smoke evidence.
        Never full logs; every field must agree with the record parameters.
        """
        if len(summary.encode("utf-8")) > 4096:
            raise ValueError("playwright_smoke evidence summary exceeds the bounded size")
        try:
            payload = json.loads(summary)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("playwright_smoke evidence summary must be structured JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("playwright_smoke evidence summary must be a JSON object")
        if payload.get("runId") != run_id:
            raise ValueError("playwright_smoke evidence summary runId must match the run")
        if payload.get("acceptanceId") != acceptance_key:
            raise ValueError("playwright_smoke evidence summary acceptanceId must match the record")
        if payload.get("result") != status:
            raise ValueError("playwright_smoke evidence summary result must match the status")
        if payload.get("artifactRef") != artifact_id:
            raise ValueError(
                "playwright_smoke evidence summary artifactRef must match the artifactId"
            )
        for key, limit in (("testPath", 512), ("testName", 300), ("recordedAt", 64)):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(
                    f"playwright_smoke evidence summary {key} must be nonempty and bounded"
                )
        if type(payload.get("exitCode")) is not int:
            raise ValueError("playwright_smoke evidence summary exitCode must be an integer")

    async def create_version(
        self,
        run_id: str,
        commit_sha: str,
        qa_status: str,
        files: Iterable[dict[str, Any]],
        snapshot_id: str | None = None,
        *,
        lease_token: str | None = None,
    ) -> VersionResponse:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            project = await session.get(ProjectRecord, run.project_id, with_for_update=True)
            if project is None:
                raise NotFoundError("project not found")
            number = (
                int(
                    await session.scalar(
                        select(func.coalesce(func.max(VersionRecord.number), 0)).where(
                            VersionRecord.project_id == project.id
                        )
                    )
                    or 0
                )
                + 1
            )
            version = VersionRecord(
                id=uuid7(),
                project_id=project.id,
                number=number,
                commit_sha=commit_sha,
                parent_version_id=run.base_version_id,
                snapshot_id=snapshot_id,
                qa_status=qa_status,
            )
            session.add(version)
            await session.flush()
            for item in files:
                session.add(
                    VersionFileRecord(
                        id=uuid7(),
                        version_id=version.id,
                        path=str(item["path"]),
                        sha256=str(item["sha256"]),
                        size=int(item["size"]),
                        mime=str(item["mime"]),
                        content_text=item.get("content_text"),
                        object_key=item.get("object_key"),
                    )
                )
            project.head_version_id = version.id
            project.updated_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "version.created",
                payload={"versionId": version.id, "number": number, "commitSha": commit_sha},
            )
            await session.commit()
            return _version_response(version)

    async def finalize_verified_publish(
        self,
        run_id: str,
        *,
        commit_sha: str,
        files: Iterable[dict[str, Any]],
        product_title: str,
        acceptance_items: Iterable[tuple[str, str | None]],
        preview_url: str | None,
        preview_elapsed_seconds: float | None,
        lease_token: str,
        snapshot_id: str | None = None,
    ) -> VersionResponse:
        """Atomically publish a verified version and converge the run to success.

        A successful retry for the same run and commit returns the already
        published version. Cancellation and lease loss fail before any version
        or project-head mutation is committed.
        """
        normalized_files = [dict(item) for item in files]
        if not normalized_files:
            raise ValueError("published version requires at least one file")
        normalized_acceptance = tuple(acceptance_items)
        acceptance_keys = [item[0] for item in normalized_acceptance]
        if len(acceptance_keys) != len(set(acceptance_keys)):
            raise ValueError("published acceptance keys must be unique")

        async with self.database.session_factory() as session:
            run = await session.get(RunRecord, run_id, with_for_update=True)
            if run is None:
                raise NotFoundError("run not found")
            if run.status == RunStatus.succeeded.value:
                event = await session.scalar(
                    select(RunEventRecord)
                    .where(
                        RunEventRecord.run_id == run_id,
                        RunEventRecord.kind == "version.created",
                    )
                    .order_by(RunEventRecord.seq.desc())
                    .limit(1)
                )
                version_id = event.payload.get("versionId") if event is not None else None
                version = (
                    await session.get(VersionRecord, version_id)
                    if isinstance(version_id, str)
                    else None
                )
                if (
                    version is None
                    or version.project_id != run.project_id
                    or version.commit_sha != commit_sha
                ):
                    raise ConflictError("run already succeeded with a different publication")
                return _version_response(version)
            if run.status in TERMINAL_STATUSES:
                raise ConflictError("terminal run cannot publish a version")

            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if run.cancel_requested_at is not None:
                raise RunLeaseLost("run cancellation was requested before publication")
            project = await session.get(ProjectRecord, run.project_id, with_for_update=True)
            if project is None:
                raise NotFoundError("project not found")
            number = (
                int(
                    await session.scalar(
                        select(func.coalesce(func.max(VersionRecord.number), 0)).where(
                            VersionRecord.project_id == project.id
                        )
                    )
                    or 0
                )
                + 1
            )
            version = VersionRecord(
                id=uuid7(),
                project_id=project.id,
                number=number,
                commit_sha=commit_sha,
                parent_version_id=run.base_version_id,
                snapshot_id=snapshot_id,
                qa_status="passed",
            )
            session.add(version)
            await session.flush()
            for item in normalized_files:
                session.add(
                    VersionFileRecord(
                        id=uuid7(),
                        version_id=version.id,
                        path=str(item["path"]),
                        sha256=str(item["sha256"]),
                        size=int(item["size"]),
                        mime=str(item["mime"]),
                        content_text=item.get("content_text"),
                        object_key=item.get("object_key"),
                    )
                )
            project.head_version_id = version.id
            project.updated_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "version.created",
                payload={
                    "versionId": version.id,
                    "number": number,
                    "commitSha": commit_sha,
                },
            )
            for acceptance_key, goal_id in normalized_acceptance:
                trace = TraceLinkRecord(
                    id=uuid7(),
                    run_id=run_id,
                    source_kind="acceptance_criterion",
                    source_ref=acceptance_key,
                    relation="verified_in",
                    target_kind="version",
                    target_ref=version.id,
                    metadata_json={"goalId": goal_id} if goal_id is not None else {},
                )
                session.add(trace)
                await self._append_event_in_session(
                    session,
                    run,
                    "trace.updated",
                    payload={"traceLinkId": trace.id},
                )
            run.preview_url = preview_url
            if preview_url is not None:
                await self._append_event_in_session(
                    session,
                    run,
                    "preview.verified",
                    payload={
                        "url": preview_url,
                        "verificationStatus": "verified",
                        "elapsedSeconds": preview_elapsed_seconds,
                    },
                )
            run.phase = RunPhase.ready.value
            await self._append_event_in_session(
                session,
                run,
                "run.status_changed",
                payload={"status": run.status, "phase": run.phase},
            )
            await self._append_event_in_session(
                session,
                run,
                "assistant.summary",
                payload={
                    "summary": (
                        f"{product_title} is ready as version {number}; "
                        "the clean sandbox gates and frozen acceptance workflows passed."
                    )
                },
            )
            run.status = RunStatus.succeeded.value
            run.error_code = None
            run.lease_owner = None
            run.lease_expires_at = None
            run.updated_at = utcnow()
            await self._append_event_in_session(
                session,
                run,
                "run.completed",
                payload={
                    "status": RunStatus.succeeded.value,
                    "summary": f"Version {number} passed deterministic verification.",
                },
            )
            await self._advance_project_after_terminal(session, run)
            await session.commit()
            return _version_response(version)

    async def next_version_number(self, project_id: str) -> int:
        async with self.database.session_factory() as session:
            number = int(
                await session.scalar(
                    select(func.coalesce(func.max(VersionRecord.number), 0)).where(
                        VersionRecord.project_id == project_id
                    )
                )
                or 0
            )
            return number + 1

    async def list_versions(self, project_id: str) -> list[VersionResponse]:
        async with self.database.session_factory() as session:
            return await self._list_versions_in_session(session, project_id)

    async def list_version_files(
        self, project_id: str, version_id: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.database.session_factory() as session:
            project = await session.get(ProjectRecord, project_id)
            if project is None:
                raise NotFoundError("project not found")
            return await self._list_version_files_in_session(session, project, version_id)

    async def get_version_file_content(
        self, project_id: str, path: str, version_id: str | None = None
    ) -> tuple[str, str, str]:
        async with self.database.session_factory() as session:
            project = await session.get(ProjectRecord, project_id)
            if project is None:
                raise NotFoundError("project not found")
            version = await self._selected_version_in_session(session, project, version_id)
            file = await session.scalar(
                select(VersionFileRecord).where(
                    VersionFileRecord.version_id == version.id,
                    VersionFileRecord.path == path,
                )
            )
            if file is None or file.content_text is None:
                raise NotFoundError("file content not available")
            return version.id, file.content_text, file.sha256

    async def save_file_content(
        self,
        project_id: str,
        owner_session_id: str,
        path: str,
        content: str,
        *,
        base_version_id: str | None,
        base_sha256: str | None,
    ) -> tuple[VersionResponse, str, str]:
        """Create an immutable user-edit version after optimistic concurrency checks."""
        normalized_path = self._validated_file_path(path)
        async with self.database.session_factory() as session:
            project = await self._require_project_in_session(session, project_id, owner_session_id)
            self._ensure_no_active_writer(project)
            if project.head_version_id != base_version_id:
                raise ConflictError("The project version changed; reload before saving.")

            previous_files: list[VersionFileRecord] = []
            if project.head_version_id is not None:
                previous_files = list(
                    await session.scalars(
                        select(VersionFileRecord)
                        .where(VersionFileRecord.version_id == project.head_version_id)
                        .order_by(VersionFileRecord.path.asc())
                    )
                )
            current_file = next(
                (item for item in previous_files if item.path == normalized_path), None
            )
            expected_sha = current_file.sha256 if current_file is not None else None
            if (base_sha256 or None) != expected_sha:
                raise ConflictError("The file changed; reload before saving.")

            version = await self._create_derived_version_in_session(
                session,
                project,
                parent_version_id=project.head_version_id,
                commit_sha=f"manual-{uuid7()}",
                qa_status="manual",
            )
            for item in previous_files:
                if item.path != normalized_path:
                    session.add(self._copy_version_file(item, version.id))
            encoded = content.encode("utf-8")
            sha256 = hashlib.sha256(encoded).hexdigest()
            session.add(
                VersionFileRecord(
                    id=uuid7(),
                    version_id=version.id,
                    path=normalized_path,
                    sha256=sha256,
                    size=len(encoded),
                    mime=mimetypes.guess_type(normalized_path)[0] or "text/plain",
                    content_text=content,
                )
            )
            project.head_version_id = version.id
            project.updated_at = utcnow()
            await session.commit()
            return _version_response(version), normalized_path, sha256

    async def restore_version(
        self, project_id: str, owner_session_id: str, version_id: str
    ) -> VersionResponse:
        """Copy a historical version into a new head; never mutate history."""
        async with self.database.session_factory() as session:
            project = await self._require_project_in_session(session, project_id, owner_session_id)
            self._ensure_no_active_writer(project)
            source = await session.get(VersionRecord, version_id)
            if source is None or source.project_id != project_id:
                raise NotFoundError("version not found")
            source_files = list(
                await session.scalars(
                    select(VersionFileRecord)
                    .where(VersionFileRecord.version_id == source.id)
                    .order_by(VersionFileRecord.path.asc())
                )
            )
            version = await self._create_derived_version_in_session(
                session,
                project,
                parent_version_id=project.head_version_id,
                commit_sha=f"restore-{source.commit_sha[:64]}-{uuid7()}",
                qa_status="restored",
            )
            for item in source_files:
                session.add(self._copy_version_file(item, version.id))
            project.head_version_id = version.id
            project.updated_at = utcnow()
            await session.commit()
            return _version_response(version)

    async def build_version_archive(
        self, project_id: str, version_id: str | None = None
    ) -> tuple[VersionResponse, bytes]:
        """Build a source ZIP from durable version files, never from a live sandbox."""
        async with self.database.session_factory() as session:
            project = await session.get(ProjectRecord, project_id)
            if project is None:
                raise NotFoundError("project not found")
            version = await self._selected_version_in_session(session, project, version_id)
            files = list(
                await session.scalars(
                    select(VersionFileRecord)
                    .where(VersionFileRecord.version_id == version.id)
                    .order_by(VersionFileRecord.path.asc())
                )
            )
            archive = BytesIO()
            with ZipFile(archive, mode="w", compression=ZIP_DEFLATED) as zip_file:
                for file in files:
                    if file.content_text is None:
                        raise ConflictError(
                            "The selected version has object-only files and cannot yet be downloaded as a complete archive."
                        )
                    archive_path = self._validated_file_path(file.path)
                    zip_file.writestr(archive_path, file.content_text.encode("utf-8"))
            return _version_response(version), archive.getvalue()

    async def get_trace(self, project_id: str, run_id: str | None = None) -> dict[str, Any]:
        async with self.database.session_factory() as session:
            if run_id is None:
                run_id = await session.scalar(
                    select(RunRecord.id)
                    .where(RunRecord.project_id == project_id)
                    .order_by(RunRecord.created_at.desc())
                    .limit(1)
                )
            return await self._get_trace_in_session(session, project_id, run_id)

    async def set_sandbox_id(
        self, run_id: str, sandbox_id: str, *, lease_token: str | None = None
    ) -> None:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            run.sandbox_id = sandbox_id
            run.updated_at = utcnow()
            await session.commit()

    async def register_sandbox_resource(
        self,
        run_id: str,
        sandbox_id: str,
        kind: str,
        *,
        lease_token: str,
    ) -> str:
        if kind not in {"generation", "verification"}:
            raise ValueError("sandbox resource kind must be generation or verification")
        if not sandbox_id.strip() or len(sandbox_id) > 128:
            raise ValueError("sandbox_id must be nonempty and bounded")
        async with self.database.session_factory() as session:
            await self._run_for_write(session, run_id, lease_token=lease_token)
            record = await session.scalar(
                select(RunSandboxResourceRecord).where(
                    RunSandboxResourceRecord.run_id == run_id,
                    RunSandboxResourceRecord.sandbox_id == sandbox_id,
                    RunSandboxResourceRecord.kind == kind,
                )
            )
            if record is None:
                record = RunSandboxResourceRecord(
                    id=uuid7(),
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    kind=kind,
                )
                session.add(record)
            else:
                record.cleaned_at = None
            await session.commit()
            return record.id

    async def require_live_sandbox_resource(
        self,
        run_id: str,
        sandbox_id: str,
        kind: str,
        *,
        lease_token: str,
    ) -> str:
        """Adopt an already-registered continuation sandbox under a new lease."""

        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            if run.sandbox_id != sandbox_id:
                raise ConflictError("continuation sandbox reference changed")
            record = await session.scalar(
                select(RunSandboxResourceRecord).where(
                    RunSandboxResourceRecord.run_id == run_id,
                    RunSandboxResourceRecord.sandbox_id == sandbox_id,
                    RunSandboxResourceRecord.kind == kind,
                    RunSandboxResourceRecord.cleaned_at.is_(None),
                )
            )
            if record is None:
                raise ConflictError("continuation sandbox resource is unavailable")
            return record.id

    async def list_sandbox_cleanup_targets(
        self, run_id: str | None = None
    ) -> list[SandboxCleanupTarget]:
        async with self.database.session_factory() as session:
            statement = (
                select(RunSandboxResourceRecord, RunRecord)
                .join(RunRecord, RunRecord.id == RunSandboxResourceRecord.run_id)
                .where(
                    RunSandboxResourceRecord.cleaned_at.is_(None),
                    RunRecord.status != RunStatus.running.value,
                    ~and_(
                        RunRecord.status.in_(
                            [
                                RunStatus.waiting_for_user.value,
                                RunStatus.queued.value,
                            ]
                        ),
                        RunRecord.continuation_request_id.is_not(None),
                        RunRecord.sandbox_id == RunSandboxResourceRecord.sandbox_id,
                        RunSandboxResourceRecord.kind == "generation",
                    ),
                    # The current verification sandbox of a successful run is
                    # the live verified preview. Retain it until the preview is
                    # invalidated or the durable sandbox reference changes.
                    or_(
                        RunRecord.status != RunStatus.succeeded.value,
                        RunSandboxResourceRecord.kind != "verification",
                        RunRecord.preview_url.is_(None),
                        RunRecord.sandbox_id.is_(None),
                        RunRecord.sandbox_id != RunSandboxResourceRecord.sandbox_id,
                    ),
                )
                .order_by(RunSandboxResourceRecord.created_at, RunSandboxResourceRecord.id)
            )
            if run_id is not None:
                statement = statement.where(RunSandboxResourceRecord.run_id == run_id)
            rows = (await session.execute(statement)).all()
            return [
                SandboxCleanupTarget(
                    run_id=run.id,
                    project_id=run.project_id,
                    sandbox_id=resource.sandbox_id,
                    resource_id=resource.id,
                    kind=resource.kind,
                )
                for resource, run in rows
            ]

    async def acknowledge_sandbox_cleanup(self, resource_id: str) -> bool:
        async with self.database.session_factory() as session:
            result = await session.execute(
                update(RunSandboxResourceRecord)
                .where(
                    RunSandboxResourceRecord.id == resource_id,
                    RunSandboxResourceRecord.cleaned_at.is_(None),
                )
                .values(cleaned_at=utcnow())
            )
            await session.commit()
            return result.rowcount == 1

    async def set_preview_url(
        self, run_id: str, preview_url: str | None, *, lease_token: str | None = None
    ) -> None:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            run.preview_url = preview_url
            run.updated_at = utcnow()
            await session.commit()

    async def get_preview(self, project_id: str) -> dict[str, Any]:
        async with self.database.session_factory() as session:
            project = await session.get(ProjectRecord, project_id)
            if project is None:
                raise NotFoundError("project not found")
            return await self._get_preview_in_session(session, project)

    async def require_verified_preview_target(self, sandbox_id: str) -> VerifiedPreviewTarget:
        """Resolve only the retained verification sandbox of a published run.

        Preview hostnames are unguessable capability URLs, but the hostname is
        never sufficient authorization by itself.  The gateway must also prove
        that the sandbox is still the current, uncleaned resource behind a
        successfully verified run.
        """

        candidate = sandbox_id.strip()
        if not candidate or len(candidate) > 128:
            raise NotFoundError("verified preview not found")
        async with self.database.session_factory() as session:
            row = (
                await session.execute(
                    select(RunRecord, RunSandboxResourceRecord)
                    .join(
                        RunSandboxResourceRecord,
                        RunSandboxResourceRecord.run_id == RunRecord.id,
                    )
                    .where(
                        RunRecord.sandbox_id == candidate,
                        RunRecord.status == RunStatus.succeeded.value,
                        RunRecord.preview_url.is_not(None),
                        RunSandboxResourceRecord.sandbox_id == candidate,
                        RunSandboxResourceRecord.kind == "verification",
                        RunSandboxResourceRecord.cleaned_at.is_(None),
                    )
                    .order_by(RunRecord.updated_at.desc(), RunRecord.id.desc())
                    .limit(1)
                )
            ).first()
            if row is None:
                raise NotFoundError("verified preview not found")
            run, _resource = row
            return VerifiedPreviewTarget(
                run_id=run.id,
                project_id=run.project_id,
                sandbox_id=candidate,
                preview_url=run.preview_url,
            )

    async def expire_verified_preview_target(
        self,
        sandbox_id: str,
        *,
        expected_preview_url: str | None = None,
    ) -> bool:
        """Converge a confirmed provider-side expiry into durable preview state."""

        candidate = sandbox_id.strip()
        if not candidate or len(candidate) > 128:
            return False
        async with self.database.session_factory() as session:
            conditions = [
                RunRecord.sandbox_id == candidate,
                RunRecord.status == RunStatus.succeeded.value,
                RunRecord.preview_url.is_not(None),
                RunSandboxResourceRecord.sandbox_id == candidate,
                RunSandboxResourceRecord.kind == "verification",
                RunSandboxResourceRecord.cleaned_at.is_(None),
            ]
            if expected_preview_url is not None:
                conditions.append(RunRecord.preview_url == expected_preview_url)
            row = (
                await session.execute(
                    select(RunRecord, RunSandboxResourceRecord)
                    .join(
                        RunSandboxResourceRecord,
                        RunSandboxResourceRecord.run_id == RunRecord.id,
                    )
                    .where(*conditions)
                    .with_for_update()
                    .limit(1)
                )
            ).first()
            if row is None:
                return False
            run, resource = row
            now = utcnow()
            run.preview_url = None
            run.updated_at = now
            resource.cleaned_at = now
            await self._append_event_in_session(
                session,
                run,
                "preview.expired",
                payload={
                    "sandboxId": candidate,
                    "reason": "sandbox_expired",
                },
            )
            await session.commit()
            return True

    async def _list_versions_in_session(
        self, session: AsyncSession, project_id: str
    ) -> list[VersionResponse]:
        records = list(
            await session.scalars(
                select(VersionRecord)
                .where(VersionRecord.project_id == project_id)
                .order_by(VersionRecord.number.desc())
            )
        )
        return [_version_response(record) for record in records]

    async def _selected_version_in_session(
        self, session: AsyncSession, project: ProjectRecord, version_id: str | None
    ) -> VersionRecord:
        selected_id = version_id or project.head_version_id
        if selected_id is None:
            raise NotFoundError("no version available")
        version = await session.get(VersionRecord, selected_id)
        if version is None or version.project_id != project.id:
            raise NotFoundError("version not found")
        return version

    async def _list_version_files_in_session(
        self, session: AsyncSession, project: ProjectRecord, version_id: str | None = None
    ) -> list[dict[str, Any]]:
        if version_id is None and project.head_version_id is None:
            return []
        version = await self._selected_version_in_session(session, project, version_id)
        records = list(
            await session.scalars(
                select(VersionFileRecord)
                .where(VersionFileRecord.version_id == version.id)
                .order_by(VersionFileRecord.path.asc())
            )
        )
        return [
            {"path": item.path, "sha256": item.sha256, "size": item.size, "mime": item.mime}
            for item in records
        ]

    async def _create_derived_version_in_session(
        self,
        session: AsyncSession,
        project: ProjectRecord,
        *,
        parent_version_id: str | None,
        commit_sha: str,
        qa_status: str,
    ) -> VersionRecord:
        number = (
            int(
                await session.scalar(
                    select(func.coalesce(func.max(VersionRecord.number), 0)).where(
                        VersionRecord.project_id == project.id
                    )
                )
                or 0
            )
            + 1
        )
        version = VersionRecord(
            id=uuid7(),
            project_id=project.id,
            number=number,
            commit_sha=commit_sha,
            parent_version_id=parent_version_id,
            qa_status=qa_status,
        )
        session.add(version)
        await session.flush()
        return version

    @staticmethod
    def _copy_version_file(source: VersionFileRecord, version_id: str) -> VersionFileRecord:
        return VersionFileRecord(
            id=uuid7(),
            version_id=version_id,
            path=source.path,
            sha256=source.sha256,
            size=source.size,
            mime=source.mime,
            content_text=source.content_text,
            object_key=source.object_key,
        )

    async def _get_trace_in_session(
        self, session: AsyncSession, project_id: str, run_id: str | None
    ) -> dict[str, Any]:
        links: list[TraceLinkRecord] = []
        evidence: list[VerificationEvidenceRecord] = []
        verification_events: list[RunEventRecord] = []
        if run_id is not None:
            links = list(
                await session.scalars(
                    select(TraceLinkRecord)
                    .where(TraceLinkRecord.run_id == run_id)
                    .order_by(TraceLinkRecord.id.asc())
                )
            )
            evidence = list(
                await session.scalars(
                    select(VerificationEvidenceRecord)
                    .where(VerificationEvidenceRecord.run_id == run_id)
                    .order_by(VerificationEvidenceRecord.id.asc())
                )
            )
            verification_events = list(
                await session.scalars(
                    select(RunEventRecord)
                    .where(
                        RunEventRecord.run_id == run_id,
                        RunEventRecord.kind == "verification.updated",
                    )
                    .order_by(RunEventRecord.seq.asc(), RunEventRecord.id.asc())
                )
            )
        link_payload = [
            {
                "id": item.id,
                "sourceKind": item.source_kind,
                "sourceRef": item.source_ref,
                "relation": item.relation,
                "targetKind": item.target_kind,
                "targetRef": item.target_ref,
                "metadata": item.metadata_json,
            }
            for item in links
        ]
        # Compatibility projection for successful GoalGraph runs created before
        # checkpoint writes materialized implemented_in links. The verified
        # capsule is durable provenance, so this stays read-only and fail-safe.
        if run_id is not None:
            run = await session.get(RunRecord, run_id)
            graph = await session.scalar(
                select(GoalGraphRecord).where(GoalGraphRecord.run_id == run_id)
            )
            if (
                run is not None
                and run.status == RunStatus.succeeded.value
                and graph is not None
                and graph.status == GraphStatus.VERIFIED.value
            ):
                revision = await self._current_revision_in_session(session, graph)
                nodes = list(
                    await session.scalars(
                        select(GoalNodeRecord)
                        .where(GoalNodeRecord.revision_id == revision.id)
                        .order_by(GoalNodeRecord.position)
                    )
                )
                checkpoint = await session.scalar(
                    select(CheckpointRecord)
                    .where(CheckpointRecord.graph_id == graph.id)
                    .order_by(CheckpointRecord.ordinal.desc())
                    .limit(1)
                )
                paths_by_goal = (
                    checkpoint.capsule.get("goalChangedPathsByGoal")
                    if checkpoint is not None and isinstance(checkpoint.capsule, dict)
                    else None
                )
                if isinstance(paths_by_goal, dict) and checkpoint is not None:
                    snapshot_paths = set(
                        await session.scalars(
                            select(CheckpointFileRecord.path).where(
                                CheckpointFileRecord.checkpoint_id == checkpoint.id
                            )
                        )
                    )
                    existing_pairs = {
                        (str(link["sourceRef"]), str(link["targetRef"]))
                        for link in link_payload
                        if link["sourceKind"] == "acceptance_criterion"
                        and link["relation"] == "implemented_in"
                        and link["targetKind"] == "file"
                    }
                    for node in nodes:
                        raw_paths = paths_by_goal.get(node.goal_key, [])
                        if not isinstance(raw_paths, list):
                            continue
                        inferred_paths = sorted(
                            {
                                path
                                for path in raw_paths
                                if isinstance(path, str)
                                and path in snapshot_paths
                                and _is_business_implementation_path(path)
                            }
                        )
                        for criterion in node.acceptance.get("criteria", []):
                            local_id = criterion.get("id")
                            if not isinstance(local_id, str):
                                continue
                            acceptance_key = acceptance_persistence_key(node.goal_key, local_id)
                            for path in inferred_paths:
                                if (acceptance_key, path) in existing_pairs:
                                    continue
                                digest = hashlib.sha256(
                                    f"{run_id}\0{acceptance_key}\0{path}".encode()
                                ).hexdigest()[:32]
                                link_payload.append(
                                    {
                                        "id": f"inferred-{digest}",
                                        "sourceKind": "acceptance_criterion",
                                        "sourceRef": acceptance_key,
                                        "relation": "implemented_in",
                                        "targetKind": "file",
                                        "targetRef": path,
                                        "metadata": {
                                            "goalId": node.goal_key,
                                            "inferred": True,
                                            "source": "goal_checkpoint_capsule",
                                        },
                                    }
                                )
                                existing_pairs.add((acceptance_key, path))
        evidence_payload = [
            {
                "id": item.id,
                "acceptanceId": item.acceptance_key,
                "kind": item.kind,
                "status": item.status,
                "summary": item.summary,
                "artifactId": item.artifact_id,
            }
            for item in evidence
        ]
        specification_items = list(
            await session.scalars(
                select(SpecItemRecord)
                .where(
                    SpecItemRecord.project_id == project_id,
                    SpecItemRecord.kind == "acceptance_criterion",
                    SpecItemRecord.retired_run_id.is_(None),
                )
                .order_by(SpecItemRecord.stable_key.asc())
            )
        )
        acceptance_trace: list[dict[str, Any]] = []
        for item in specification_items:
            acceptance_id = item.stable_key
            # Only deterministic playwright_smoke records surface as AC
            # evidence; historical qa_gates records stay hidden.
            item_evidence = [
                entry
                for entry in evidence_payload
                if entry["acceptanceId"] == acceptance_id and entry["kind"] == "playwright_smoke"
            ]
            item_links = [
                link
                for link in link_payload
                if (
                    link["sourceKind"] == "acceptance_criterion"
                    and link["sourceRef"] == acceptance_id
                )
                or (
                    link["targetKind"] == "acceptance_criterion"
                    and link["targetRef"] == acceptance_id
                )
            ]
            acceptance_trace.append(
                {
                    "acceptanceId": acceptance_id,
                    "criterion": dict(item.content),
                    "status": self._acceptance_status(
                        verification_events, acceptance_id, evidence_payload
                    ),
                    "implementationStatus": (
                        "implemented"
                        if any(
                            link["relation"] == "implemented_in"
                            and link["sourceKind"] == "acceptance_criterion"
                            and link["targetKind"] == "file"
                            and not str(link["targetRef"]).startswith("tests/generated/")
                            for link in item_links
                        )
                        else "not_implemented"
                    ),
                    "links": item_links,
                    "evidence": item_evidence,
                }
            )
        return {
            "run_id": run_id,
            "links": link_payload,
            "evidence": evidence_payload,
            "acceptance_trace": acceptance_trace,
        }

    async def _get_preview_in_session(
        self, session: AsyncSession, project: ProjectRecord
    ) -> dict[str, Any]:
        run = await session.scalar(
            select(RunRecord)
            .where(RunRecord.project_id == project.id, RunRecord.preview_url.is_not(None))
            .order_by(RunRecord.updated_at.desc())
            .limit(1)
        )
        if run is not None and run.preview_url:
            return {
                "status": "ready",
                "url": run.preview_url,
                "runId": run.id,
                "verificationStatus": (
                    "verified" if run.status == RunStatus.succeeded.value else "unverified"
                ),
            }
        if project.head_version_id:
            last_run_id = await session.scalar(
                select(RunRecord.id)
                .where(RunRecord.project_id == project.id)
                .order_by(RunRecord.created_at.desc())
                .limit(1)
            )
            return {
                "status": "expired",
                "url": None,
                "runId": last_run_id,
                "verificationStatus": None,
            }
        return {
            "status": "unavailable",
            "url": None,
            "runId": None,
            "verificationStatus": None,
        }

    @staticmethod
    def _acceptance_status(
        events: list[RunEventRecord],
        acceptance_id: str,
        evidence_payload: list[dict[str, Any]],
    ) -> str:
        """Derive validation status from the latest closed-scope acceptance event.

        Events are ordered by seq with a stable UUIDv7 id tie-break; the newest
        event for the AC wins. A passed/failed event counts only when it points
        at a real playwright_smoke evidence record; reset (unverified), scope-
        less legacy events, and infrastructure outcomes keep the AC unverified.
        """
        latest: RunEventRecord | None = None
        for event in events:
            payload = event.payload or {}
            if payload.get("scope") != "acceptance":
                continue
            if payload.get("acceptanceId") != acceptance_id:
                continue
            latest = event
        if latest is None:
            return "unverified"
        status = str(latest.payload.get("status") or "").lower()
        if status not in {"passed", "failed"}:
            return "unverified"
        evidence_id = latest.payload.get("evidenceId")
        if not isinstance(evidence_id, str):
            return "unverified"
        record = next(
            (entry for entry in evidence_payload if entry["id"] == evidence_id),
            None,
        )
        if (
            record is None
            or record.get("kind") != "playwright_smoke"
            or str(record.get("status")).lower() != status
            or record.get("acceptanceId") != acceptance_id
            or not isinstance(record.get("artifactId"), str)
            or not record["artifactId"]
        ):
            return "unverified"
        return status

    @staticmethod
    def _ensure_no_active_writer(project: ProjectRecord) -> None:
        if project.active_run_id is not None:
            raise ConflictError(
                "An agent run is active; wait for it before changing the published version."
            )

    @staticmethod
    def _goal_graph_projection(
        graph_record: GoalGraphRecord,
        revision: GoalGraphRevisionRecord,
        nodes: list[GoalNodeRecord],
    ) -> GoalGraphProjection:
        draft_payload = {
            "schemaVersion": graph_record.schema_version,
            "productOutcome": revision.product_outcome,
            "goals": [
                {
                    "goalId": node.goal_key,
                    "title": node.title,
                    "productOutcome": node.product_outcome,
                    "userVisible": node.user_visible,
                    "dependsOn": list(node.depends_on),
                    "acceptance": dict(node.acceptance),
                }
                for node in sorted(nodes, key=lambda item: item.position)
            ],
        }
        draft = parse_goal_graph_draft(draft_payload)
        actual_hash = hashlib.sha256(serialize_goal_graph_draft(draft).encode("utf-8")).hexdigest()
        if actual_hash != revision.content_hash:
            raise ManifestIntegrityError("GoalGraph revision content hash mismatch")
        trusted = materialize_goal_graph(draft)
        goal_by_key = {goal.goal_id: goal for goal in trusted.goals}
        projected_goals = [
            Goal.model_validate(
                {
                    **goal_by_key[node.goal_key].model_dump(mode="json", by_alias=True),
                    "status": node.status,
                }
            )
            for node in sorted(nodes, key=lambda item: item.position)
        ]
        projected = GoalGraph.model_validate(
            {
                "schemaVersion": graph_record.schema_version,
                "productOutcome": revision.product_outcome,
                "qualityBar": dict(revision.quality_bar),
                "goals": [item.model_dump(mode="json", by_alias=True) for item in projected_goals],
                "status": graph_record.status,
            }
        )
        return GoalGraphProjection(
            graph_id=graph_record.id,
            project_id=graph_record.project_id,
            run_id=graph_record.run_id,
            revision=revision.revision,
            revision_id=revision.id,
            content_hash=revision.content_hash,
            graph=projected,
        )

    async def _goal_graph_for_run_in_session(
        self, session: AsyncSession, run_id: str
    ) -> GoalGraphProjection | None:
        graph = await session.scalar(
            select(GoalGraphRecord).where(GoalGraphRecord.run_id == run_id)
        )
        if graph is None:
            return None
        revision = await self._current_revision_in_session(session, graph)
        nodes = list(
            await session.scalars(
                select(GoalNodeRecord)
                .where(GoalNodeRecord.revision_id == revision.id)
                .order_by(GoalNodeRecord.position)
            )
        )
        return self._goal_graph_projection(graph, revision, nodes)

    async def _goal_graph_read_projection_in_session(
        self, session: AsyncSession, run_id: str
    ) -> dict[str, Any] | None:
        graph = await session.scalar(
            select(GoalGraphRecord).where(GoalGraphRecord.run_id == run_id)
        )
        if graph is None:
            return None
        revision = await self._current_revision_in_session(session, graph)
        nodes = list(
            await session.scalars(
                select(GoalNodeRecord)
                .where(GoalNodeRecord.revision_id == revision.id)
                .order_by(GoalNodeRecord.position)
            )
        )
        node_ids = [node.id for node in nodes]
        checkpoints = (
            list(
                await session.scalars(
                    select(CheckpointRecord).where(CheckpointRecord.goal_node_id.in_(node_ids))
                )
            )
            if node_ids
            else []
        )
        evidence_rows = (
            (
                await session.execute(
                    select(
                        GoalEvidenceRecord.goal_node_id,
                        GoalEvidenceRecord.acceptance_key,
                        func.count(GoalEvidenceRecord.id),
                    )
                    .where(GoalEvidenceRecord.goal_node_id.in_(node_ids))
                    .group_by(
                        GoalEvidenceRecord.goal_node_id,
                        GoalEvidenceRecord.acceptance_key,
                    )
                )
            ).all()
            if node_ids
            else []
        )
        return self._goal_graph_read_projection(
            graph,
            revision,
            nodes,
            checkpoint_by_node={item.goal_node_id: item.id for item in checkpoints},
            evidence_count_by_key={
                (node_id, acceptance_key): int(count)
                for node_id, acceptance_key, count in evidence_rows
            },
        )

    @staticmethod
    def _goal_graph_read_projection(
        graph: GoalGraphRecord,
        revision: GoalGraphRevisionRecord,
        nodes: list[GoalNodeRecord],
        *,
        checkpoint_by_node: Mapping[str, str],
        evidence_count_by_key: Mapping[tuple[str, str], int],
    ) -> dict[str, Any]:
        active = next(
            (
                node.goal_key
                for node in nodes
                if node.status in {GoalStatus.ACTIVE.value, GoalStatus.CLAIMED.value}
            ),
            None,
        )
        goals: list[dict[str, Any]] = []
        for node in sorted(nodes, key=lambda item: item.position):
            acceptance: list[dict[str, Any]] = []
            goal_evidence_count = 0
            for criterion in node.acceptance.get("criteria", []):
                acceptance_key = acceptance_persistence_key(node.goal_key, criterion["id"])
                count = evidence_count_by_key.get((node.id, acceptance_key), 0)
                goal_evidence_count += count
                acceptance.append(
                    {
                        "acceptanceId": criterion["id"],
                        "title": criterion["title"],
                        "priority": criterion.get("priority", "must"),
                        "status": "passed" if count else "unverified",
                    }
                )
            goals.append(
                {
                    "id": node.goal_key,
                    "title": node.title,
                    "userVisible": node.user_visible,
                    "dependsOn": list(node.depends_on),
                    "status": node.status,
                    "checkpointId": checkpoint_by_node.get(node.id),
                    "claimedAt": (
                        _as_utc(node.claimed_at).isoformat() if node.claimed_at else None
                    ),
                    "verifiedAt": (
                        _as_utc(node.verified_at).isoformat() if node.verified_at else None
                    ),
                    "acceptance": acceptance,
                    "evidenceCount": goal_evidence_count,
                }
            )
        return {
            "graphId": graph.id,
            "runId": graph.run_id,
            "revision": revision.revision,
            "status": graph.status,
            "productOutcome": revision.product_outcome,
            "activeGoalId": active,
            "goals": goals,
        }

    async def _supersede_terminal_project_graphs(
        self,
        session: AsyncSession,
        project_id: str,
        *,
        excluding_run_id: str,
    ) -> list[str]:
        """Explicitly close stale terminal-run graphs before later graph creation."""
        prior_graphs = list(
            await session.scalars(
                select(GoalGraphRecord)
                .where(
                    GoalGraphRecord.project_id == project_id,
                    GoalGraphRecord.run_id != excluding_run_id,
                    GoalGraphRecord.status == GraphStatus.ACTIVE.value,
                )
                .with_for_update()
            )
        )
        superseded: list[str] = []
        for graph in prior_graphs:
            prior_run = await session.get(RunRecord, graph.run_id, with_for_update=True)
            if prior_run is None:
                raise ManifestIntegrityError("GoalGraph owner run is missing")
            if prior_run.status not in TERMINAL_STATUSES:
                raise ConflictError("another nonterminal run owns the project GoalGraph")
            revision = await self._current_revision_in_session(session, graph)
            nodes = list(
                await session.scalars(
                    select(GoalNodeRecord)
                    .where(GoalNodeRecord.revision_id == revision.id)
                    .with_for_update()
                )
            )
            for node in nodes:
                current = GoalStatus(node.status)
                if current in {GoalStatus.PENDING, GoalStatus.ACTIVE, GoalStatus.CLAIMED}:
                    node.status = transition_goal_status(current, GoalStatus.SUPERSEDED).value
            graph.status = transition_graph_status(
                GraphStatus(graph.status), GraphStatus.SUPERSEDED
            ).value
            graph.updated_at = utcnow()
            goal_graph = await self._goal_graph_read_projection_in_session(session, prior_run.id)
            await self._append_event_in_session(
                session,
                prior_run,
                "goal_graph.superseded",
                payload={
                    "graphId": graph.id,
                    "status": GraphStatus.SUPERSEDED.value,
                    "reason": "superseded by a later project run",
                    "goalGraph": goal_graph,
                },
            )
            superseded.append(graph.id)
        return superseded

    @staticmethod
    async def _current_revision_in_session(
        session: AsyncSession, graph: GoalGraphRecord
    ) -> GoalGraphRevisionRecord:
        revision = await session.scalar(
            select(GoalGraphRevisionRecord).where(
                GoalGraphRevisionRecord.graph_id == graph.id,
                GoalGraphRevisionRecord.revision == graph.current_revision,
            )
        )
        if revision is None:
            raise ManifestIntegrityError("GoalGraph current revision is missing")
        return revision

    async def _goal_write_context(
        self,
        session: AsyncSession,
        run_id: str,
        goal_id: str,
    ) -> tuple[GoalGraphRecord, GoalGraphRevisionRecord, list[GoalNodeRecord], GoalNodeRecord]:
        graph = await session.scalar(
            select(GoalGraphRecord).where(GoalGraphRecord.run_id == run_id).with_for_update()
        )
        if graph is None:
            raise NotFoundError("goal graph not found")
        if graph.status != GraphStatus.ACTIVE.value:
            raise ConflictError("goal graph is not active")
        revision = await self._current_revision_in_session(session, graph)
        nodes = list(
            await session.scalars(
                select(GoalNodeRecord)
                .where(GoalNodeRecord.revision_id == revision.id)
                .order_by(GoalNodeRecord.position)
                .with_for_update()
            )
        )
        target = next((node for node in nodes if node.goal_key == goal_id), None)
        if target is None:
            raise NotFoundError("goal not found")
        return graph, revision, nodes, target

    @staticmethod
    def _next_eligible_goal(nodes: list[GoalNodeRecord]) -> GoalNodeRecord | None:
        status_by_key = {node.goal_key: node.status for node in nodes}
        for node in sorted(nodes, key=lambda item: item.position):
            if node.status != GoalStatus.PENDING.value:
                continue
            if all(status_by_key.get(key) == GoalStatus.VERIFIED.value for key in node.depends_on):
                return node
        return None

    @classmethod
    def _normalize_checkpoint_files(
        cls, files: Iterable[Mapping[str, Any] | CheckpointFile]
    ) -> tuple[list[CheckpointFile], str]:
        normalized: list[CheckpointFile] = []
        seen: set[str] = set()
        for value in files:
            if isinstance(value, CheckpointFile):
                path = cls._validated_file_path(value.path)
                content = value.content_text
                expected_hash = value.sha256
                expected_size = value.size
            else:
                path_value = value.get("path")
                content = value.get("contentText", value.get("content_text", value.get("content")))
                if not isinstance(path_value, str):
                    raise ValueError("checkpoint file requires path")
                path = cls._validated_file_path(path_value)
                expected_hash = value.get("sha256")
                expected_size = value.get("size")
            if path in seen:
                raise ValueError("checkpoint file paths must be unique")
            if not isinstance(content, str):
                raise ValueError("checkpoint files must contain complete UTF-8 text")
            encoded = content.encode("utf-8", errors="strict")
            digest = hashlib.sha256(encoded).hexdigest()
            size = len(encoded)
            if expected_hash is not None and expected_hash != digest:
                raise ManifestIntegrityError(f"checkpoint file hash mismatch: {path}")
            if expected_size is not None and expected_size != size:
                raise ManifestIntegrityError(f"checkpoint file size mismatch: {path}")
            seen.add(path)
            normalized.append(
                CheckpointFile(path=path, content_text=content, sha256=digest, size=size)
            )
        if not normalized:
            raise ValueError("checkpoint manifest must contain at least one model-owned file")
        normalized.sort(key=lambda item: item.path.encode("utf-8"))
        manifest = [
            {"path": item.path, "sha256": item.sha256, "size": item.size} for item in normalized
        ]
        canonical = json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return normalized, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _checkpoint_projection_in_session(
        self,
        session: AsyncSession,
        checkpoint: CheckpointRecord,
        goal_id: str,
    ) -> VerifiedCheckpoint:
        records = list(
            await session.scalars(
                select(CheckpointFileRecord)
                .where(CheckpointFileRecord.checkpoint_id == checkpoint.id)
                .order_by(CheckpointFileRecord.path)
            )
        )
        files = [
            CheckpointFile(
                path=record.path,
                content_text=record.content_text,
                sha256=record.sha256,
                size=record.size,
            )
            for record in records
        ]
        _, manifest_hash = self._normalize_checkpoint_files(files)
        if manifest_hash != checkpoint.manifest_hash:
            raise ManifestIntegrityError("checkpoint manifest hash mismatch")
        evidence_records = list(
            await session.scalars(
                select(GoalEvidenceRecord)
                .where(GoalEvidenceRecord.checkpoint_id == checkpoint.id)
                .order_by(GoalEvidenceRecord.acceptance_key, GoalEvidenceRecord.kind)
            )
        )
        return VerifiedCheckpoint(
            id=checkpoint.id,
            graph_id=checkpoint.graph_id,
            run_id=checkpoint.run_id,
            goal_id=goal_id,
            ordinal=checkpoint.ordinal,
            manifest_hash=checkpoint.manifest_hash,
            commit_sha=checkpoint.commit_sha,
            snapshot_id=checkpoint.snapshot_id,
            capsule=dict(checkpoint.capsule),
            files=tuple(files),
            evidence=tuple(
                {
                    "id": item.id,
                    "acceptanceKey": item.acceptance_key,
                    "kind": item.kind,
                    "status": item.status,
                    "artifactId": item.artifact_id,
                    "reference": item.reference,
                    "summary": item.summary,
                    "payload": dict(item.payload),
                }
                for item in evidence_records
            ),
            created_at=_as_utc(checkpoint.created_at),
        )

    @staticmethod
    def _validated_file_path(path: str) -> str:
        candidate = PurePosixPath(path)
        if (
            not path
            or candidate.is_absolute()
            or ".." in candidate.parts
            or str(candidate) in {"", "."}
        ):
            raise FilePathError("path must stay inside the project source tree")
        if ".git" in candidate.parts or any(part.startswith(".env") for part in candidate.parts):
            raise FilePathError("path is not editable through the project API")
        return candidate.as_posix()

    async def _active_session_in_session(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        for_update: bool = False,
    ) -> SessionRecord:
        record = await session.get(
            SessionRecord,
            session_id,
            with_for_update=for_update,
        )
        if record is None or record.revoked_at is not None or _is_expired(record.expires_at):
            raise NotFoundError("session not found or expired")
        return record

    async def _authenticated_session_in_session(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        for_update: bool = False,
    ) -> SessionRecord:
        record = await self._active_session_in_session(
            session,
            session_id,
            for_update=for_update,
        )
        if record.kind != "user" or record.user_id is None:
            raise AuthenticationError("an authenticated account is required")
        if await session.get(UserRecord, record.user_id) is None:
            raise AuthenticationError("the session account no longer exists")
        return record

    async def _issue_user_session_in_session(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        ttl_hours: int,
    ) -> SessionRecord:
        now = utcnow()
        expires_at = now + timedelta(hours=ttl_hours)
        record = SessionRecord(
            id=new_session_id(),
            kind="user",
            user_id=user_id,
            expires_at=expires_at,
        )
        session.add(record)
        return record

    async def _session_can_access_project(
        self,
        session: AsyncSession,
        project: ProjectRecord,
        requester_session_id: str,
    ) -> bool:
        requester = await self._authenticated_session_in_session(
            session,
            requester_session_id,
        )
        owner = await session.get(SessionRecord, project.owner_session_id)
        if owner is None or owner.kind != "user" or owner.user_id != requester.user_id:
            return False
        return await session.get(UserRecord, requester.user_id) is not None

    async def _require_project_in_session(
        self, session: AsyncSession, project_id: str, owner_session_id: str
    ) -> ProjectRecord:
        record = await session.get(ProjectRecord, project_id, with_for_update=True)
        if record is None:
            raise NotFoundError("project not found")
        if not await self._session_can_access_project(session, record, owner_session_id):
            raise OwnershipError("project does not belong to this session")
        return record

    async def _locked_run(self, session: AsyncSession, run_id: str) -> RunRecord:
        record = await session.get(RunRecord, run_id, with_for_update=True)
        if record is None:
            raise NotFoundError("run not found")
        return record

    async def _run_for_write(
        self,
        session: AsyncSession,
        run_id: str,
        *,
        lease_token: str | None,
    ) -> RunRecord:
        """Acquire the run's durable write fence before a SOP side effect.

        API and recovery paths deliberately omit ``lease_token`` and retain
        their existing semantics. A SOP-supplied token uses one conditional
        UPDATE as a cross-database fencing operation: it works on PostgreSQL
        and on SQLite, where ``SELECT FOR UPDATE`` alone is insufficient.
        """
        if lease_token is None:
            return await self._locked_run(session, run_id)
        now = utcnow()
        result = await session.execute(
            update(RunRecord)
            .where(
                RunRecord.id == run_id,
                RunRecord.status == RunStatus.running.value,
                RunRecord.lease_owner == lease_token,
                RunRecord.lease_expires_at.is_not(None),
                RunRecord.lease_expires_at > now,
            )
            # A real UPDATE is the portable row-level lock/fence acquisition;
            # the timestamp is otherwise only bookkeeping for this write.
            .values(updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise RunLeaseLost("run lease is no longer active")
        return await self._locked_run(session, run_id)

    async def _run_with_seq(
        self,
        session: AsyncSession,
        run: RunRecord,
        *,
        usage_totals: UsageTotals | None = None,
        usage_totals_loaded: bool = False,
    ) -> RunResponse:
        last_seq = int(
            await session.scalar(
                select(func.coalesce(func.max(RunEventRecord.seq), 0)).where(
                    RunEventRecord.run_id == run.id
                )
            )
            or 0
        )
        pending = await self._pending_input_request_in_session(session, run.id)
        checkpoint_available = await self._source_checkpoint_available_in_session(
            session, run
        )
        if not usage_totals_loaded:
            usage_by_run = await self._usage_totals_for_runs_in_session(session, [run.id])
            usage_totals = usage_by_run.get(run.id)
        return _run_response(
            run,
            last_seq,
            pending,
            source_checkpoint_available=checkpoint_available,
            usage_totals=usage_totals,
        )

    async def _latest_verified_checkpoint_for_run_in_session(
        self,
        session: AsyncSession,
        run: RunRecord,
    ) -> VerifiedCheckpoint | None:
        checkpoint = await session.scalar(
            select(CheckpointRecord)
            .where(CheckpointRecord.run_id == run.id)
            .order_by(CheckpointRecord.ordinal.desc())
            .limit(1)
        )
        if checkpoint is None:
            return None
        if checkpoint.project_id != run.project_id:
            raise ManifestIntegrityError("checkpoint project lineage is invalid")
        node = await session.get(GoalNodeRecord, checkpoint.goal_node_id)
        if (
            node is None
            or node.run_id != run.id
            or node.project_id != run.project_id
            or node.status != GoalStatus.VERIFIED.value
        ):
            raise ManifestIntegrityError("checkpoint no longer belongs to a verified goal")
        projection = await self._checkpoint_projection_in_session(
            session, checkpoint, node.goal_key
        )
        if not projection.evidence or any(
            item.get("status") != "passed" for item in projection.evidence
        ):
            raise ManifestIntegrityError("checkpoint evidence is not fully verified")
        return projection

    async def _recoverable_checkpoint_for_source_in_session(
        self,
        session: AsyncSession,
        source: RunRecord,
    ) -> VerifiedCheckpoint | None:
        """Select the newest state a fresh recovery may safely inherit.

        A failed recovery run may not have produced a checkpoint of its own.
        In that case its already verified inherited checkpoint remains the
        newest trustworthy state and is revalidated before being selected.
        """

        checkpoint = await self._latest_verified_checkpoint_for_run_in_session(
            session, source
        )
        if checkpoint is not None:
            return checkpoint
        return await self._recovery_checkpoint_for_run_in_session(session, source)

    async def _recovery_checkpoint_for_run_in_session(
        self,
        session: AsyncSession,
        run: RunRecord,
    ) -> VerifiedCheckpoint | None:
        if run.recovery_mode != "verified_checkpoint":
            if (
                run.recovered_from_checkpoint_id is not None
                or run.recovered_from_goal_id is not None
            ):
                raise ManifestIntegrityError(
                    "non-checkpoint recovery has checkpoint lineage"
                )
            return None
        if (
            run.recovered_from_run_id is None
            or run.recovered_from_goal_id is None
            or run.recovered_from_checkpoint_id is None
        ):
            raise ManifestIntegrityError("recovery checkpoint lineage is incomplete")
        checkpoint = await session.get(
            CheckpointRecord, run.recovered_from_checkpoint_id
        )
        if checkpoint is None or checkpoint.project_id != run.project_id:
            raise ManifestIntegrityError("recovery checkpoint lineage is invalid")

        # The run always points to its direct source, while an inherited
        # checkpoint can belong to an older source run. Every intermediate
        # recovery must attest the exact same checkpoint and goal until the
        # checkpoint-owning run is reached.
        ancestor_id: str | None = run.recovered_from_run_id
        visited = {run.id}
        for _ in range(MAX_RECOVERY_LINEAGE_RUNS):
            if ancestor_id is None or ancestor_id in visited:
                raise ManifestIntegrityError("recovery checkpoint lineage is invalid")
            visited.add(ancestor_id)
            ancestor = await session.get(RunRecord, ancestor_id)
            if (
                ancestor is None
                or ancestor.project_id != run.project_id
                or ancestor.status not in RECOVERABLE_TERMINAL_STATUSES
            ):
                raise ManifestIntegrityError("recovery checkpoint lineage is invalid")
            if checkpoint.run_id == ancestor.id:
                break
            if (
                ancestor.recovery_mode != "verified_checkpoint"
                or ancestor.recovered_from_checkpoint_id != checkpoint.id
                or ancestor.recovered_from_goal_id != run.recovered_from_goal_id
            ):
                raise ManifestIntegrityError("recovery checkpoint lineage is invalid")
            ancestor_id = ancestor.recovered_from_run_id
        else:
            raise ManifestIntegrityError("recovery checkpoint lineage is too deep")

        node = await session.get(GoalNodeRecord, checkpoint.goal_node_id)
        if (
            node is None
            or node.run_id != checkpoint.run_id
            or node.project_id != checkpoint.project_id
            or node.goal_key != run.recovered_from_goal_id
            or node.status != GoalStatus.VERIFIED.value
        ):
            raise ManifestIntegrityError("recovery checkpoint goal is invalid")
        projection = await self._checkpoint_projection_in_session(
            session, checkpoint, node.goal_key
        )
        if not projection.evidence or any(
            item.get("status") != "passed" for item in projection.evidence
        ):
            raise ManifestIntegrityError("recovery checkpoint evidence is invalid")
        return projection

    async def _source_checkpoint_available_in_session(
        self,
        session: AsyncSession,
        run: RunRecord,
    ) -> bool:
        if run.status not in RECOVERABLE_TERMINAL_STATUSES:
            return False
        try:
            return (
                await self._recoverable_checkpoint_for_source_in_session(session, run)
            ) is not None
        except ManifestIntegrityError:
            # Availability is a safe projection. Corrupt recovery state is not
            # advertised and the explicit recovery mutation still fails closed.
            return False

    @staticmethod
    async def _verified_base_version_in_session(
        session: AsyncSession,
        source: RunRecord,
    ) -> VersionRecord | None:
        if source.base_version_id is None:
            return None
        version = await session.get(VersionRecord, source.base_version_id)
        if (
            version is None
            or version.project_id != source.project_id
            or version.qa_status != "passed"
        ):
            return None
        return version

    @staticmethod
    async def _pending_input_request_in_session(
        session: AsyncSession, run_id: str
    ) -> RunInputRequestRecord | None:
        return await session.scalar(
            select(RunInputRequestRecord)
            .where(
                RunInputRequestRecord.run_id == run_id,
                RunInputRequestRecord.status == "pending",
            )
            .order_by(
                RunInputRequestRecord.created_at.desc(),
                RunInputRequestRecord.id.desc(),
            )
            .limit(1)
        )

    async def _append_event_in_session(
        self,
        session: AsyncSession,
        run: RunRecord,
        kind: str,
        *,
        role: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RunEventRecord:
        last_seq = int(
            await session.scalar(
                select(func.coalesce(func.max(RunEventRecord.seq), 0)).where(
                    RunEventRecord.run_id == run.id
                )
            )
            or 0
        )
        record = RunEventRecord(
            id=uuid7(),
            run_id=run.id,
            seq=last_seq + 1,
            kind=kind,
            role=role,
            payload=payload or {},
        )
        session.add(record)
        await session.flush()
        return record

    def _event_envelope(self, run: RunRecord, record: RunEventRecord) -> EventEnvelope:
        return EventEnvelope(
            event_id=record.id,
            seq=record.seq,
            project_id=run.project_id,
            run_id=run.id,
            kind=record.kind,
            role=record.role,
            occurred_at=record.created_at,
            payload=dict(record.payload),
        )

    async def _advance_project_after_terminal(self, session: AsyncSession, run: RunRecord) -> None:
        project = await session.get(ProjectRecord, run.project_id, with_for_update=True)
        if project is None:
            return
        next_run = await session.scalar(
            select(RunRecord)
            .where(
                RunRecord.project_id == run.project_id, RunRecord.status == RunStatus.queued.value
            )
            .order_by(RunRecord.created_at.asc())
            .limit(1)
        )
        project.active_run_id = next_run.id if next_run is not None else None
        project.status = "queued" if next_run is not None else "idle"
        project.updated_at = utcnow()
