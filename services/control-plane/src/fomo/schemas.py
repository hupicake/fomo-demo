"""Public API and role hand-off schemas.

All role artifacts are strict Pydantic models so a later role cannot consume an
unvalidated free-form answer from an earlier role.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class SchemaModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra="forbid")


class UserStory(SchemaModel):
    id: str
    story: str
    priority: Literal["must", "should", "could"] = "must"


class AcceptanceCriterion(SchemaModel):
    id: str
    given: str
    when: str
    then: str


class PageSpec(SchemaModel):
    route: str
    purpose: str
    key_elements: list[str] = Field(default_factory=list)


class VisualDirection(SchemaModel):
    tone: str
    colors: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)


class ProductSpec(SchemaModel):
    title: str
    problem: str
    target_users: list[str] = Field(default_factory=list)
    user_stories: list[UserStory] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    pages: list[PageSpec] = Field(default_factory=list)
    visual_direction: VisualDirection
    assumptions: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)

    @field_validator("acceptance_criteria")
    @classmethod
    def unique_acceptance_ids(cls, values: list[AcceptanceCriterion]) -> list[AcceptanceCriterion]:
        ids = [item.id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance criteria ids must be unique")
        return values


class RouteSpec(SchemaModel):
    path: str
    rendering: Literal["client", "server", "static"]
    description: str


class ComponentSpec(SchemaModel):
    name: str
    responsibility: str
    children: list[str] = Field(default_factory=list)


class ComponentDecision(SchemaModel):
    """Why a UI component reuses a mature primitive or is built in-app."""

    component: str = Field(min_length=1)
    strategy: Literal["reuse", "custom"]
    source: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class PublicApiProp(SchemaModel):
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    required: bool = True


class PublicApiContract(SchemaModel):
    """A cross-file symbol contract that every Engineer batch must preserve."""

    file_path: str = Field(min_length=1)
    export_style: Literal["default", "named"]
    symbol: str = Field(min_length=1)
    props: list[PublicApiProp] = Field(default_factory=list)
    type: str = Field(min_length=1)


class StateModelSpec(SchemaModel):
    name: str
    owner: str
    persistence: str


class DependencySpec(SchemaModel):
    name: str
    reason: str


class FilePlanItem(SchemaModel):
    path: str
    operation: Literal["create", "modify", "delete"]
    reason: str


class TestPlanItem(SchemaModel):
    acceptance_id: str
    method: Literal["playwright", "unit", "manual", "typecheck", "build"]
    steps: list[str] = Field(default_factory=list)


class TechnicalSpec(SchemaModel):
    framework: str
    routes: list[RouteSpec] = Field(default_factory=list)
    components: list[ComponentSpec] = Field(default_factory=list)
    component_decisions: list[ComponentDecision] = Field(min_length=1)
    public_api_contracts: list[PublicApiContract] = Field(default_factory=list)
    state_model: list[StateModelSpec] = Field(default_factory=list)
    dependencies: list[DependencySpec] = Field(default_factory=list)
    file_plan: list[FilePlanItem] = Field(default_factory=list)
    test_plan: list[TestPlanItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class GeneratedFile(SchemaModel):
    path: str
    content: str = ""
    operation: Literal["create", "modify", "delete"] = "create"


class ImplementationBatchPlan(SchemaModel):
    id: str
    purpose: str
    paths: list[str] = Field(default_factory=list)
    acceptance_ids: list[str] = Field(default_factory=list)


class ImplementationPlan(SchemaModel):
    """A compact Engineer-owned plan that bounds each source-generation call."""

    baseline_version_id: str | None = None
    batches: list[ImplementationBatchPlan] = Field(default_factory=list)
    design_decision_ids: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)


class FileBatchReport(SchemaModel):
    """One bounded, immediately persisted group of complete workspace files."""

    batch_id: str
    implemented_acceptance_ids: list[str] = Field(default_factory=list)
    design_decision_ids: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    file_changes: list[GeneratedFile] = Field(default_factory=list)

    @field_validator("file_changes")
    @classmethod
    def nonempty_paths(cls, values: list[GeneratedFile]) -> list[GeneratedFile]:
        if any(not item.path.strip() for item in values):
            raise ValueError("file path cannot be empty")
        return values


class ImplementationReport(SchemaModel):
    baseline_version_id: str | None = None
    implemented_acceptance_ids: list[str] = Field(default_factory=list)
    design_decision_ids: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    candidate_commit: str | None = None
    # The final Engineer action is intentionally compact: complete file bodies
    # live in the independently persisted implementation_batch artifacts.
    batch_artifact_ids: list[str] = Field(default_factory=list)
    file_changes: list[GeneratedFile] = Field(default_factory=list)

    @field_validator("file_changes")
    @classmethod
    def nonempty_paths(cls, values: list[GeneratedFile]) -> list[GeneratedFile]:
        if any(not item.path.strip() for item in values):
            raise ValueError("file path cannot be empty")
        return values


class GateStatus(StrEnum):
    passed = "passed"
    failed = "failed"
    skipped = "skipped"


class GateResult(SchemaModel):
    gate: str
    status: GateStatus
    summary: str
    evidence: list[str] = Field(default_factory=list)


class DiagnosticFinding(SchemaModel):
    severity: Literal["minor", "major", "error"]
    message: str
    file: str | None = None
    acceptance_id: str | None = None


class DiagnosticReport(SchemaModel):
    gates: list[GateResult] = Field(default_factory=list)
    acceptance_ids: list[str] = Field(default_factory=list)
    issue_fingerprint: str | None = None
    responsible_role: Literal["product_manager", "architect", "engineer", "reviewer"] = "engineer"
    blocking_issues: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    location_files: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    screenshot_references: list[str] = Field(default_factory=list)
    findings: list[DiagnosticFinding] = Field(default_factory=list)


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


class GuestSessionResponse(SchemaModel):
    id: str
    expires_at: datetime


class ProjectCreate(SchemaModel):
    title: str = Field(min_length=1, max_length=200)


class ProjectPatch(SchemaModel):
    title: str = Field(min_length=1, max_length=200)


class MessageCreate(SchemaModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=50_000)
    base_version_id: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class ProjectResponse(SchemaModel):
    id: str
    title: str
    status: str
    head_version_id: str | None = None
    active_run_id: str | None = None
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


class RunResponse(SchemaModel):
    id: str
    project_id: str
    status: RunStatus
    phase: RunPhase
    repair_round: int
    last_seq: int = 0
    base_version_id: str | None = None
    cancel_requested_at: datetime | None = None
    error_code: str | None = None
    preview_url: str | None = None
    created_at: datetime
    updated_at: datetime


class MessageRunResponse(SchemaModel):
    message: MessageResponse
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
    status: Literal["unverified", "passed", "failed", "skipped"] = "unverified"
    links: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class TraceResponse(SchemaModel):
    run_id: str | None = None
    links: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    acceptance_trace: list[AcceptanceTraceItem] = Field(default_factory=list)


class PreviewResponse(SchemaModel):
    status: Literal["ready", "expired", "unavailable"]
    url: str | None = None
    run_id: str | None = None


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
