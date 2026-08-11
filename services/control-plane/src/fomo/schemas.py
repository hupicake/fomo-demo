"""Public API and role hand-off schemas.

All role artifacts are strict Pydantic models so a later role cannot consume an
unvalidated free-form answer from an earlier role.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from fomo.agent_framework import AgentFramework
from fomo.runtime_contract import (
    DEFAULT_PROFILE_ID,
    DEFAULT_THINKING,
    LEGACY_CONTEXT_WINDOW,
    LEGACY_INFERENCE_TPM_LIMIT,
    LEGACY_RUN_MAX_TOKENS,
    LEGACY_RUNTIME_POLICY_VERSION,
    RuntimeContractError,
    resolve_runtime_contract,
)


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class SchemaModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra="forbid")


class GateStatus(StrEnum):
    passed = "passed"
    failed = "failed"
    skipped = "skipped"


class GateDiagnostic(SchemaModel):
    """Bounded assertion evidence safe to persist and forward for repair."""

    message: str = Field(min_length=1, max_length=1_200)
    locator: str | None = Field(default=None, max_length=500)
    test_name: str = Field(min_length=1, max_length=300)
    line: int | None = Field(default=None, ge=1, le=10_000_000)


class GateResult(SchemaModel):
    gate: str
    status: GateStatus
    summary: str
    evidence: list[str] = Field(default_factory=list)
    # Deterministic QA may expose only normalized workspace paths, never the
    # command output used to derive them.
    affected_files: list[str] = Field(default_factory=list)
    # Project gates (dependencies/typecheck/build/smoke/preview) are closed-set
    # project scope; acceptance gates bind one Playwright item to one AC.
    scope: Literal["project", "acceptance"] = "project"
    acceptance_id: str | None = None
    test_path: str | None = None
    test_name: str | None = None
    # acceptance-only: assertion outcomes write AC evidence; an
    # infrastructure_failed gate blocks the run without touching AC validation.
    outcome: Literal["passed", "failed", "infrastructure_failed"] | None = None
    # Bounded command exit code; required for a passed/failed acceptance gate
    # so evidence can carry it without persisting full logs.
    exit_code: int | None = None
    # Command-level timeout flag. Project gates record it so a timed-out
    # dependency install is classified as infrastructure while an ordinary
    # non-zero install stays repairable.
    timed_out: bool = False
    # Failed acceptance assertions may carry only this closed, bounded
    # diagnostic projection. Raw reporter output, stack, trace and attachments
    # are never valid gate fields.
    diagnostic: GateDiagnostic | None = None

    @model_validator(mode="after")
    def _enforce_gate_scope_contract(self) -> GateResult:
        acceptance_fields = (
            self.acceptance_id,
            self.test_path,
            self.test_name,
            self.outcome,
            self.diagnostic,
        )
        if self.scope == "project":
            if any(value is not None for value in acceptance_fields):
                raise ValueError(
                    "project-scope gates must not carry acceptance fields or diagnostics"
                )
            return self
        if not self.acceptance_id or not self.outcome:
            raise ValueError(
                "acceptance-scope gates require a nonempty acceptanceId and outcome"
            )
        if self.outcome in {"passed", "failed"}:
            if (
                not self.test_path
                or not self.test_name
                or self.exit_code is None
            ):
                raise ValueError(
                    "passed or failed acceptance gates require testPath, testName, and exitCode"
                )
        if self.diagnostic is not None and self.outcome != "failed":
            raise ValueError("only failed acceptance assertions may carry diagnostics")
        for label, value, limit in (
            ("testPath", self.test_path, 512),
            ("testName", self.test_name, 300),
        ):
            if value is not None and (len(value) > limit or not value.strip()):
                raise ValueError(f"{label} must be a bounded non-blank value")
        return self


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    waiting_for_user = "waiting_for_user"
    needs_attention = "needs_attention"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class RunPhase(StrEnum):
    queued = "queued"
    preparing = "preparing"
    planning = "planning"
    building = "building"
    verifying = "verifying"
    repairing = "repairing"
    ready = "ready"
    # Legacy phases remain readable until historical runs are retired.
    product_analysis = "product_analysis"
    architecture = "architecture"
    implementation = "implementation"
    verification = "verification"
    repair = "repair"
    publishing = "publishing"


class EventEnvelope(SchemaModel):
    schema_version: int = 1
    event_id: str
    seq: int
    project_id: str
    run_id: str
    kind: str
    role: str | None = None
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class AccountCredentials(SchemaModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        local, separator, domain = normalized.partition("@")
        if (
            not separator
            or not local
            or not domain
            or "@" in domain
            or "." not in domain
            or any(character.isspace() for character in normalized)
        ):
            raise ValueError("email must be a valid address")
        return normalized


class UserRegister(AccountCredentials):
    display_name: str | None = Field(default=None, max_length=100)

    @field_validator("display_name")
    @classmethod
    def non_blank_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("displayName must be non-blank")
        return normalized


class UserLogin(AccountCredentials):
    pass


class UserResponse(SchemaModel):
    id: str
    email: str
    display_name: str
    created_at: datetime


class AuthSessionResponse(SchemaModel):
    expires_at: datetime
    user: UserResponse


class ProjectCreate(SchemaModel):
    title: str = Field(min_length=1, max_length=200)


class ProjectPatch(SchemaModel):
    title: str = Field(min_length=1, max_length=200)


class RuntimeSelection(SchemaModel):
    profile_id: str = Field(default=DEFAULT_PROFILE_ID, min_length=1, max_length=64)
    thinking: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def _supported_selection(self) -> RuntimeSelection:
        try:
            resolve_runtime_contract(self.profile_id, self.thinking)
        except RuntimeContractError as exc:
            raise ValueError(str(exc)) from exc
        return self


class MessageCreate(SchemaModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=50_000)
    base_version_id: str | None = None
    # Binary/file attachments are not part of the current persisted run input
    # contract. Accept the existing client's empty array but reject silent data loss.
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=0)
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    thinking: str | None = Field(default=None, min_length=1, max_length=32)
    agent_framework: AgentFramework | None = None

    @model_validator(mode="after")
    def _supported_runtime(self) -> MessageCreate:
        if self.profile_id is not None:
            RuntimeSelection(profile_id=self.profile_id, thinking=self.thinking)
        return self


class UserInputRequestDraft(SchemaModel):
    """Strict public form emitted only by Pi's trusted virtual tool."""

    question: str = Field(min_length=1, max_length=2_000)
    choices: list[str] = Field(default_factory=list, max_length=8)
    allow_freeform: bool
    # A short public rationale may help the control plane audit why execution
    # paused, but it is never projected to clients or SSE.
    reason: str | None = Field(default=None, max_length=1_000)

    @field_validator("question")
    @classmethod
    def _bounded_question(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("question must be non-blank")
        return normalized

    @field_validator("choices")
    @classmethod
    def _bounded_choices(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()) for value in values]
        if any(not value or len(value) > 200 for value in normalized):
            raise ValueError("choices must be bounded non-blank values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("choices must be unique")
        return normalized

    @model_validator(mode="after")
    def _answerable(self) -> UserInputRequestDraft:
        if not self.allow_freeform and not self.choices:
            raise ValueError("a request must allow freeform input or provide choices")
        return self


class UserInputRequestResponse(SchemaModel):
    id: str
    run_id: str
    question: str
    choices: list[str] = Field(default_factory=list)
    allow_freeform: bool
    status: Literal["pending", "answered", "cancelled", "expired"]
    stage: Literal["planning", "building", "repairing"]
    goal_id: str | None = None
    created_at: datetime
    answered_at: datetime | None = None


class UserInputAnswerCreate(SchemaModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(min_length=1, max_length=50_000)

    @field_validator("answer")
    @classmethod
    def _non_blank_answer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer must be non-blank")
        return value


class RecoveryRunCreate(SchemaModel):
    """User-authored follow-up that forks a new run from terminal history."""

    client_message_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=50_000)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=0)
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    thinking: str | None = Field(default=None, min_length=1, max_length=32)
    agent_framework: AgentFramework | None = None

    @field_validator("content")
    @classmethod
    def _non_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must be non-blank")
        return value

    @model_validator(mode="after")
    def _supported_runtime(self) -> RecoveryRunCreate:
        if self.profile_id is not None:
            RuntimeSelection(profile_id=self.profile_id, thinking=self.thinking)
        return self


RecoveryMode = Literal["verified_checkpoint", "verified_version", "base_restart"]


class RunUsageResponse(SchemaModel):
    """Final durable usage aggregated across every provider turn in one run."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistent_total(self) -> RunUsageResponse:
        expected = (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )
        if self.total_tokens != expected:
            raise ValueError("run usage total is inconsistent")
        return self


class ProjectLatestRunResponse(SchemaModel):
    id: str
    status: RunStatus
    error_code: str | None = None
    agent_framework: AgentFramework
    profile_id: str
    thinking: str
    recovery_available: bool = False
    recovery_mode: RecoveryMode | None = None
    source_checkpoint_available: bool = False
    usage: RunUsageResponse | None = None


class ProjectResponse(SchemaModel):
    id: str
    title: str
    status: str
    head_version_id: str | None = None
    active_run_id: str | None = None
    latest_run: ProjectLatestRunResponse | None = None
    created_at: datetime
    updated_at: datetime


class MessageResponse(SchemaModel):
    id: str
    project_id: str
    role: str
    content: str
    client_message_id: str
    run_id: str | None = None
    created_at: datetime


class RunRuntimeResponse(SchemaModel):
    profile_id: str
    thinking: str
    context_window: int
    policy_version: str
    # ``None`` plus the explicit flag is the public unlimited contract. An
    # integer is retained only when replaying a historical runtime-v0/v1 run.
    run_token_budget: int | None
    run_token_budget_unlimited: bool
    inference_tpm_limit: int

    @model_validator(mode="after")
    def _consistent_token_budget(self) -> RunRuntimeResponse:
        if self.run_token_budget_unlimited != (self.run_token_budget is None):
            raise ValueError("run token budget unlimited flag is inconsistent")
        return self


def _legacy_run_runtime_response() -> RunRuntimeResponse:
    return RunRuntimeResponse(
        profile_id=DEFAULT_PROFILE_ID,
        thinking=DEFAULT_THINKING,
        context_window=LEGACY_CONTEXT_WINDOW,
        policy_version=LEGACY_RUNTIME_POLICY_VERSION,
        run_token_budget=LEGACY_RUN_MAX_TOKENS,
        run_token_budget_unlimited=False,
        inference_tpm_limit=LEGACY_INFERENCE_TPM_LIMIT,
    )


class RuntimeProfileOption(SchemaModel):
    profile_id: str
    label: str
    thinking_levels: list[str]
    default_thinking: str
    context_window: int
    run_token_budget: int | None
    run_token_budget_unlimited: bool
    inference_tpm_limit: int
    available: bool
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def _consistent_token_budget(self) -> RuntimeProfileOption:
        if self.run_token_budget_unlimited != (self.run_token_budget is None):
            raise ValueError("run token budget unlimited flag is inconsistent")
        return self


class AgentFrameworkOption(SchemaModel):
    id: AgentFramework
    label: str
    compatible_profile_ids: list[str]
    compatible_thinking_levels: list[str] | None = None
    available: bool
    disabled_reason: str | None = None


class RuntimeOptionsResponse(SchemaModel):
    default_profile_id: str | None
    profiles: list[RuntimeProfileOption]
    default_agent_framework: AgentFramework
    agent_frameworks: list[AgentFrameworkOption]


class RunResponse(SchemaModel):
    id: str
    project_id: str
    status: RunStatus
    phase: RunPhase
    repair_round: int
    last_seq: int = 0
    base_version_id: str | None = None
    recovered_from_run_id: str | None = None
    recovered_from_goal_id: str | None = None
    recovered_from_checkpoint_id: str | None = None
    recovery_mode: RecoveryMode | None = None
    recovery_available: bool = False
    source_checkpoint_available: bool = False
    cancel_requested_at: datetime | None = None
    error_code: str | None = None
    preview_url: str | None = None
    pending_input_request: UserInputRequestResponse | None = None
    execution_started_at: datetime | None = None
    agent_framework: AgentFramework = AgentFramework.pi
    runtime: RunRuntimeResponse = Field(default_factory=_legacy_run_runtime_response)
    usage: RunUsageResponse | None = None
    created_at: datetime
    updated_at: datetime


class MessageRunResponse(SchemaModel):
    message: MessageResponse
    run: RunResponse


class RecoveryRunResponse(MessageRunResponse):
    recovery_mode: RecoveryMode
    source_checkpoint_available: bool


class UserInputAnswerResponse(SchemaModel):
    message: MessageResponse
    request: UserInputRequestResponse
    run: RunResponse


class VersionResponse(SchemaModel):
    id: str
    project_id: str
    number: int
    commit_sha: str
    parent_version_id: str | None = None
    qa_status: str
    created_at: datetime


class FileEntry(SchemaModel):
    path: str
    sha256: str
    size: int
    mime: str


class FileContentResponse(SchemaModel):
    version_id: str
    path: str
    content: str
    sha256: str


class FileContentUpdate(SchemaModel):
    """Optimistic single-file edit against the currently published version."""

    base_version_id: str | None = None
    base_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    content: str = Field(max_length=1_048_576)


class AcceptanceTraceItem(SchemaModel):
    """Stable acceptance-criterion projection for the trace UI."""

    acceptance_id: str
    criterion: dict[str, Any]
    # Derived only from the latest deterministic playwright_smoke evidence.
    status: Literal["unverified", "passed", "failed", "skipped"] = "unverified"
    # Derived only from real implemented_in trace links.
    implementation_status: Literal["implemented", "not_implemented"] = "not_implemented"
    links: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class TraceResponse(SchemaModel):
    run_id: str | None = None
    links: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    acceptance_trace: list[AcceptanceTraceItem] = Field(default_factory=list)


VisibleArtifactKind = Literal[
    "run_input",
    "build_plan",
    "acceptance_contract",
    "diagnostic_report",
    "product_spec",
    "technical_spec",
]

# The fixed role for every visible kind. The persisted artifact record carries
# no role, so the workspace role is derived from the kind alone.
VisibleArtifactRole = Literal["user", "pi", "fomo", "product_manager", "architect"]
VisibleArtifactStage = Literal[
    "input",
    "planning",
    "acceptance",
    "verification",
    "product",
    "architecture",
]

# The one closed set of artifact kinds surfaced by the workspace. The order is
# the canonical presentation order (Product then Architect) and the kind->role
# mapping is fixed because the persisted artifact record carries no role.
VISIBLE_ARTIFACT_KIND_ORDER: tuple[VisibleArtifactKind, ...] = (
    "run_input",
    "build_plan",
    "acceptance_contract",
    "diagnostic_report",
    "product_spec",
    "technical_spec",
)
ARTIFACT_KIND_TO_ROLE: dict[VisibleArtifactKind, VisibleArtifactRole] = {
    "run_input": "user",
    "build_plan": "pi",
    "acceptance_contract": "fomo",
    "diagnostic_report": "fomo",
    "product_spec": "product_manager",
    "technical_spec": "architect",
}
ARTIFACT_KIND_TO_STAGE: dict[VisibleArtifactKind, VisibleArtifactStage] = {
    "run_input": "input",
    "build_plan": "planning",
    "acceptance_contract": "acceptance",
    "diagnostic_report": "verification",
    "product_spec": "product",
    "technical_spec": "architecture",
}


class ArtifactRefResponse(SchemaModel):
    """A lightweight visible-artifact reference; never carries content."""

    id: str
    run_id: str
    kind: VisibleArtifactKind
    role: VisibleArtifactRole
    stage: VisibleArtifactStage
    schema_version: int
    title: str
    summary: str
    created_at: datetime


class ArtifactDetailResponse(ArtifactRefResponse):
    """A visible artifact plus the original JSON content object."""

    content: dict[str, JsonValue]


class PreviewResponse(SchemaModel):
    """The sole preview-location contract: a ready status must carry an
    absolute http(s) URL and a run id, and any non-ready status must not carry
    a URL at all.
    """

    status: Literal["ready", "expired", "unavailable"]
    url: str | None = None
    run_id: str | None = None
    verification_status: Literal["unverified", "verified"] | None = None

    @model_validator(mode="after")
    def _enforce_preview_contract(self) -> PreviewResponse:
        if self.status == "ready":
            if not self.run_id:
                raise ValueError("ready preview requires a nonempty runId")
            if not self.url:
                raise ValueError("ready preview requires a url")
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("preview url must be an absolute http(s) URL")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("preview url must not contain userinfo")
            if self.verification_status is None:
                raise ValueError("ready preview requires verificationStatus")
        elif self.url is not None:
            raise ValueError("expired or unavailable preview must have url null")
        elif self.verification_status is not None:
            raise ValueError("expired or unavailable preview must have verificationStatus null")
        return self


class GoalAcceptanceProjection(SchemaModel):
    acceptance_id: str
    title: str
    priority: Literal["must", "should"]
    status: Literal["unverified", "passed"]


class GoalNodeProjection(SchemaModel):
    id: str
    title: str
    user_visible: bool
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["pending", "active", "claimed", "verified", "failed", "superseded"]
    checkpoint_id: str | None = None
    claimed_at: datetime | None = None
    verified_at: datetime | None = None
    acceptance: list[GoalAcceptanceProjection] = Field(default_factory=list)
    evidence_count: int = 0


class GoalGraphReadProjection(SchemaModel):
    graph_id: str
    run_id: str
    revision: int
    status: Literal["active", "verified", "failed", "cancelled", "superseded"]
    product_outcome: str
    active_goal_id: str | None = None
    goals: list[GoalNodeProjection] = Field(default_factory=list)


class ProjectSnapshotResponse(SchemaModel):
    """Refresh-safe project baseline; events continue from ``lastSeq``."""

    project: ProjectResponse
    messages: list[MessageResponse] = Field(default_factory=list)
    runs: list[RunResponse] = Field(default_factory=list)
    active_run: RunResponse | None = None
    last_seq: int = 0
    events: list[EventEnvelope] = Field(default_factory=list)
    files: list[FileEntry] = Field(default_factory=list)
    versions: list[VersionResponse] = Field(default_factory=list)
    trace: TraceResponse
    preview: PreviewResponse
    artifact_refs: list[ArtifactRefResponse] = Field(default_factory=list)
    goal_graph: GoalGraphReadProjection | None = None
    pending_input_request: UserInputRequestResponse | None = None
