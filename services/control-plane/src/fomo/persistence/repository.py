"""Durable repository operations and event transaction boundaries."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fomo.ids import utcnow, uuid7
from fomo.schemas import (
    ARTIFACT_KIND_TO_ROLE,
    VISIBLE_ARTIFACT_KIND_ORDER,
    EventEnvelope,
    MessageResponse,
    ProjectResponse,
    RunPhase,
    RunResponse,
    RunStatus,
    VersionResponse,
)

from .database import Database
from .models import (
    ArtifactRecord,
    MessageRecord,
    ProjectRecord,
    RunEventRecord,
    RunRecord,
    SessionRecord,
    SpecItemRecord,
    TraceLinkRecord,
    VerificationEvidenceRecord,
    VersionFileRecord,
    VersionRecord,
)


class NotFoundError(LookupError):
    pass


class OwnershipError(PermissionError):
    pass


class ConflictError(RuntimeError):
    """The caller tried to write against a stale project/version baseline."""


class FilePathError(ValueError):
    pass


class RunLeaseLost(RuntimeError):
    """A worker attempted a durable write after losing its run lease."""


TERMINAL_STATUSES = {
    RunStatus.succeeded.value,
    RunStatus.failed.value,
    RunStatus.cancelled.value,
    RunStatus.needs_attention.value,
}


@dataclass(frozen=True, slots=True)
class SandboxCleanupTarget:
    """A durable sandbox reference that a worker must destroy best-effort."""

    run_id: str
    project_id: str
    sandbox_id: str


def _is_expired(value: datetime, *, at: datetime | None = None) -> bool:
    # SQLite does not round-trip timezone info; PostgreSQL does.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    reference = at or utcnow()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return value <= reference


def _project_response(record: ProjectRecord) -> ProjectResponse:
    return ProjectResponse(
        id=record.id,
        title=record.title,
        status=record.status,
        head_version_id=record.head_version_id,
        active_run_id=record.active_run_id,
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


def _run_response(record: RunRecord, last_seq: int = 0) -> RunResponse:
    return RunResponse(
        id=record.id,
        project_id=record.project_id,
        status=RunStatus(record.status),
        phase=RunPhase(record.phase),
        repair_round=record.repair_round,
        last_seq=last_seq,
        base_version_id=record.base_version_id,
        cancel_requested_at=record.cancel_requested_at,
        error_code=record.error_code,
        preview_url=record.preview_url,
        created_at=record.created_at,
        updated_at=record.updated_at,
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
        return f"{collapsed[:limit - 1].rstrip()}…"
    return fallback


def _artifact_title(kind: str, content: dict[str, Any]) -> str:
    if kind == "product_spec":
        return _bounded_text(content.get("title"), "Product Specification")
    return _bounded_text(content.get("title"), "Technical Specification")


def _artifact_summary(kind: str, content: dict[str, Any]) -> str:
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
        await self.database.create_all()

    async def create_guest_session(self, ttl_hours: int = 24 * 14) -> SessionRecord:
        record = SessionRecord(
            id=uuid7(),
            kind="guest",
            expires_at=utcnow() + timedelta(hours=ttl_hours),
        )
        async with self.database.session_factory() as session:
            session.add(record)
            await session.commit()
        return record

    async def get_session(self, session_id: str) -> SessionRecord:
        async with self.database.session_factory() as session:
            record = await session.get(SessionRecord, session_id)
            if record is None or _is_expired(record.expires_at):
                raise NotFoundError("session not found or expired")
            return record

    async def create_project(self, owner_session_id: str, title: str) -> ProjectResponse:
        await self.get_session(owner_session_id)
        record = ProjectRecord(id=uuid7(), owner_session_id=owner_session_id, title=title.strip())
        async with self.database.session_factory() as session:
            session.add(record)
            await session.commit()
            return _project_response(record)

    async def list_projects(self, owner_session_id: str) -> list[ProjectResponse]:
        async with self.database.session_factory() as session:
            result = await session.scalars(
                select(ProjectRecord)
                .where(ProjectRecord.owner_session_id == owner_session_id)
                .order_by(ProjectRecord.updated_at.desc())
            )
            return [_project_response(record) for record in result]

    async def require_project(self, project_id: str, owner_session_id: str | None = None) -> ProjectRecord:
        async with self.database.session_factory() as session:
            record = await session.get(ProjectRecord, project_id)
            if record is None:
                raise NotFoundError("project not found")
            if owner_session_id is not None and record.owner_session_id != owner_session_id:
                raise OwnershipError("project does not belong to this session")
            return record

    async def patch_project(self, project_id: str, owner_session_id: str, title: str) -> ProjectResponse:
        async with self.database.session_factory() as session:
            record = await self._require_project_in_session(session, project_id, owner_session_id)
            record.title = title.strip()
            record.updated_at = utcnow()
            await session.commit()
            return _project_response(record)

    async def create_message_and_run(
        self,
        project_id: str,
        owner_session_id: str,
        client_message_id: str,
        content: str,
        base_version_id: str | None = None,
    ) -> tuple[MessageResponse, RunResponse, bool]:
        """Save a message and queued run atomically; duplicate client IDs are idempotent."""
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
                return _message_response(existing_message), await self._run_with_seq(session, existing_run), False

            run = RunRecord(
                id=uuid7(),
                project_id=project_id,
                base_version_id=base_version_id or project.head_version_id,
                status=RunStatus.queued.value,
                phase=RunPhase.queued.value,
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
                payload={"messageId": message.id, "baseVersionId": run.base_version_id},
            )
            await session.commit()
            return _message_response(message), await self._run_with_seq(session, run), True

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
            run_responses = [await self._run_with_seq(session, item) for item in runs]
            active_record = (
                await session.get(RunRecord, project.active_run_id)
                if project.active_run_id is not None
                else None
            )
            active_run = (
                await self._run_with_seq(session, active_record) if active_record is not None else None
            )
            # `active_run` is deliberately null once a run becomes terminal,
            # but refresh still needs the latest completed run's visible trace
            # to reconstruct role progress in the workbench.
            display_record = active_record if active_record is not None else (runs[0] if runs else None)
            display_events: list[EventEnvelope] = []
            if display_record is not None:
                event_records = list(
                    await session.scalars(
                        select(RunEventRecord)
                        .where(RunEventRecord.run_id == display_record.id)
                        .order_by(RunEventRecord.seq.asc())
                    )
                )
                display_events = [self._event_envelope(display_record, item) for item in event_records]
            trace_run_id = display_record.id if display_record is not None else None
            files = await self._list_version_files_in_session(session, project)
            versions = await self._list_versions_in_session(session, project_id)
            trace = await self._get_trace_in_session(session, project_id, trace_run_id)
            preview = await self._get_preview_in_session(session, project)
            artifact_refs = await self._artifact_refs_in_session(
                session, display_record.id if display_record is not None else None
            )
            return {
                "project": _project_response(project),
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
            }

    async def get_run(self, run_id: str) -> RunResponse:
        async with self.database.session_factory() as session:
            record = await session.get(RunRecord, run_id)
            if record is None:
                raise NotFoundError("run not found")
            return await self._run_with_seq(session, record)

    async def get_run_prompt(self, run_id: str) -> str:
        async with self.database.session_factory() as session:
            message = await session.scalar(select(MessageRecord).where(MessageRecord.run_id == run_id))
            if message is None:
                raise NotFoundError("run message not found")
            return message.content

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

    async def list_events(self, run_id: str, after: int = 0, limit: int = 500) -> list[EventEnvelope]:
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
            if run.status == RunStatus.queued.value:
                run.status = RunStatus.cancelled.value
                run.phase = RunPhase.queued.value
                run.cancel_requested_at = utcnow()
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
            candidates = list(
                await session.scalars(
                    select(RunRecord)
                    .where(RunRecord.status == RunStatus.queued.value)
                    .order_by(RunRecord.created_at.asc())
                    .with_for_update(skip_locked=True)
                )
            )
            for run in candidates:
                running_count = await session.scalar(
                    select(func.count())
                    .select_from(RunRecord)
                    .where(
                        RunRecord.project_id == run.project_id,
                        RunRecord.status == RunStatus.running.value,
                    )
                )
                if running_count:
                    continue
                # Worker identity is intentionally not a fence: every claim
                # gets an opaque, per-run token so an old process cannot write
                # through a later worker merely because it shares a hostname.
                lease_token = uuid7()
                run.status = RunStatus.running.value
                run.phase = RunPhase.product_analysis.value
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
        """Terminalize abandoned running runs and return their stale sandboxes.

        This is intentionally idempotent. Rows are selected and rechecked
        while locked, then transitioned exactly once; later recovery passes
        cannot append another terminal event. A cancellation request wins over
        a lease-expiry failure so an interrupted user cancellation converges to
        the outcome the user asked for.
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
                    payload={"status": run.status, "summary": summary},
                )
                await self._advance_project_after_terminal(session, run)
                if run.sandbox_id:
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

    async def increment_repair_round(self, run_id: str, *, lease_token: str | None = None) -> int:
        async with self.database.session_factory() as session:
            run = await self._run_for_write(session, run_id, lease_token=lease_token)
            run.repair_round += 1
            run.phase = RunPhase.repair.value
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
                payload={"status": status.value, "summary": summary or ""},
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
            record = await self._append_event_in_session(session, run, kind, role=role, payload=payload)
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
            record = ArtifactRecord(id=uuid7(), run_id=run_id, kind=kind, schema_version=1, content=content)
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
            if project is None or project.owner_session_id != owner_session_id:
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
                            priority="must",
                            content=dict(item),
                            introduced_run_id=run_id,
                        )
                    )
                else:
                    existing.content = dict(item)
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
            # Acceptance evidence always carries the closed-set acceptance
            # scope; the frontend Release gate consumes only project scope.
            await self._append_event_in_session(
                session,
                run,
                "verification.updated",
                role="reviewer",
                payload={
                    "evidenceId": record.id,
                    "acceptanceId": acceptance_key,
                    "status": status,
                    "scope": "acceptance",
                },
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
            raise ValueError(
                "playwright_smoke evidence summary must be structured JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("playwright_smoke evidence summary must be a JSON object")
        if payload.get("runId") != run_id:
            raise ValueError("playwright_smoke evidence summary runId must match the run")
        if payload.get("acceptanceId") != acceptance_key:
            raise ValueError(
                "playwright_smoke evidence summary acceptanceId must match the record"
            )
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
            number = int(
                await session.scalar(
                    select(func.coalesce(func.max(VersionRecord.number), 0)).where(
                        VersionRecord.project_id == project.id
                    )
                )
                or 0
            ) + 1
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

    async def list_version_files(self, project_id: str, version_id: str | None = None) -> list[dict[str, Any]]:
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
            current_file = next((item for item in previous_files if item.path == normalized_path), None)
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
        number = int(
            await session.scalar(
                select(func.coalesce(func.max(VersionRecord.number), 0)).where(
                    VersionRecord.project_id == project.id
                )
            )
            or 0
        ) + 1
        version = VersionRecord(
            id=uuid7(),
            project_id=project.id,
            number=number,
            commit_sha=commit_sha,
            parent_version_id=parent_version_id,
            qa_status=qa_status,
        )
        session.add(version)
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
                if entry["acceptanceId"] == acceptance_id
                and entry["kind"] == "playwright_smoke"
            ]
            item_links = [
                link
                for link in link_payload
                if (link["sourceKind"] == "acceptance_criterion" and link["sourceRef"] == acceptance_id)
                or (link["targetKind"] == "acceptance_criterion" and link["targetRef"] == acceptance_id)
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
            return {"status": "ready", "url": run.preview_url, "runId": run.id}
        if project.head_version_id:
            last_run_id = await session.scalar(
                select(RunRecord.id)
                .where(RunRecord.project_id == project.id)
                .order_by(RunRecord.created_at.desc())
                .limit(1)
            )
            return {"status": "expired", "url": None, "runId": last_run_id}
        return {"status": "unavailable", "url": None, "runId": None}

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
            raise ConflictError("An agent run is active; wait for it before changing the published version.")

    @staticmethod
    def _validated_file_path(path: str) -> str:
        candidate = PurePosixPath(path)
        if not path or candidate.is_absolute() or ".." in candidate.parts or str(candidate) in {"", "."}:
            raise FilePathError("path must stay inside the project source tree")
        if ".git" in candidate.parts or any(part.startswith(".env") for part in candidate.parts):
            raise FilePathError("path is not editable through the project API")
        return candidate.as_posix()

    async def _require_project_in_session(
        self, session: AsyncSession, project_id: str, owner_session_id: str
    ) -> ProjectRecord:
        record = await session.get(ProjectRecord, project_id, with_for_update=True)
        if record is None:
            raise NotFoundError("project not found")
        if record.owner_session_id != owner_session_id:
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

    async def _run_with_seq(self, session: AsyncSession, run: RunRecord) -> RunResponse:
        last_seq = int(
            await session.scalar(
                select(func.coalesce(func.max(RunEventRecord.seq), 0)).where(RunEventRecord.run_id == run.id)
            )
            or 0
        )
        return _run_response(run, last_seq)

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
                select(func.coalesce(func.max(RunEventRecord.seq), 0)).where(RunEventRecord.run_id == run.id)
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
            .where(RunRecord.project_id == run.project_id, RunRecord.status == RunStatus.queued.value)
            .order_by(RunRecord.created_at.asc())
            .limit(1)
        )
        project.active_run_id = next_run.id if next_run is not None else None
        project.status = "queued" if next_run is not None else "idle"
        project.updated_at = utcnow()
