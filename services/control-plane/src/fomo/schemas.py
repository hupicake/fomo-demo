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

    @field_validator("acceptance_criteria")
    @classmethod
    def bounded_acceptance_criteria(cls, values: list[AcceptanceCriterion]) -> list[AcceptanceCriterion]:
        if not 1 <= len(values) <= 8:
            raise ValueError("acceptance criteria must contain between 1 and 8 items")
        for item in values:
            if not item.id.strip() or len(item.id) > 64:
                raise ValueError("acceptance criterion ids must be nonempty and bounded")
        return values


class RouteSpec(SchemaModel):
    path: str
    rendering: Literal["client", "server", "static"]
    description: str


InteractionResponsibility = Literal[
    "search",
    "filter",
    "data_table",
    "row_actions",
    "confirmation",
    "form",
    "sort",
    "pagination",
    "selection",
    "bulk_actions",
]
FeatureSurfaceModuleRole = Literal[
    "controller",
    "search",
    "filter",
    "data_table",
    "row_actions",
    "confirmation",
    "form",
    "sort",
    "pagination",
    "selection",
    "bulk_actions",
]
FeatureSurfaceCompositionResponsibility = Literal["compose", "layout", "props"]


class ComponentSpec(SchemaModel):
    name: str
    responsibility: str
    children: list[str] = Field(default_factory=list)
    interaction_responsibilities: list[InteractionResponsibility]


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


class FeatureSurfaceModuleSpec(SchemaModel):
    """One model-owned module of a complex interactive feature surface."""

    role: FeatureSurfaceModuleRole
    file_path: str = Field(min_length=1)
    public_symbol: str = Field(min_length=1)


class FeatureSurfaceSpec(SchemaModel):
    """Explicit UI ownership boundaries for a complex interactive component."""

    component_name: str = Field(min_length=1)
    composition_file: str = Field(min_length=1)
    composition_symbol: str = Field(min_length=1)
    composition_responsibilities: list[FeatureSurfaceCompositionResponsibility] = Field(min_length=1)
    modules: list[FeatureSurfaceModuleSpec] = Field(min_length=1)


class StateModelSpec(SchemaModel):
    name: str
    owner: str
    persistence: str
    state_class: Literal["persistent_business", "transient", "derived"]
    mutable_domains: list[str] = Field(default_factory=list)


class PersistentStateDomainSpec(SchemaModel):
    """One durable business domain and the file that owns its mutations."""

    domain: str = Field(min_length=1)
    state_model_name: str = Field(min_length=1)
    actions_store_file: str = Field(min_length=1)


class StatePersistenceAdapterSpec(SchemaModel):
    """The sole storage boundary shared by composed persistent state slices."""

    file_path: str = Field(min_length=1)
    public_symbol: str = Field(min_length=1)
    storage_key: str = Field(min_length=1)
    # The SOP enforces >= 1 so an invalid structured hand-off gets its closed
    # repair code instead of a generic Pydantic validation failure.
    schema_version: int
    responsibilities: list[str] = Field(min_length=1)


class StateAggregationSpec(SchemaModel):
    """The deliberately narrow composition boundary for domain state slices."""

    file_path: str = Field(min_length=1)
    responsibilities: list[str] = Field(min_length=1)
    persistence_adapter: StatePersistenceAdapterSpec | None = None


class DependencySpec(SchemaModel):
    name: str
    reason: str


class StarterCapabilityId(StrEnum):
    """The only server-owned Golden Starter overlays an Architect may select."""

    crud = "crud"
    local_persistence = "local-persistence"


class FilePlanItem(SchemaModel):
    path: str
    operation: Literal["create", "modify", "delete"]
    reason: str


class TestPlanItem(SchemaModel):
    acceptance_id: str
    method: Literal["playwright", "unit", "manual", "typecheck", "build"]
    steps: list[str] = Field(default_factory=list)
    # Only Playwright items may bind a concrete smoke test. The testPath must
    # be a planned model-owned tests/generated/*.smoke.spec.ts file and the
    # testName must be the exact Playwright title proven by the JSON reporter.
    test_path: str | None = None
    test_name: str | None = None

    @model_validator(mode="after")
    def _enforce_playwright_test_binding(self) -> TestPlanItem:
        if self.method == "playwright":
            if not self.test_path or not self.test_name:
                raise ValueError(
                    "playwright test plan items must declare testPath and testName"
                )
            if not self.test_path.strip() or not self.test_name.strip():
                raise ValueError(
                    "playwright testPath and testName must not be blank"
                )
            if len(self.test_path) > 512 or len(self.test_name) > 300:
                raise ValueError(
                    "playwright testPath and testName must stay within bounded lengths"
                )
        elif self.test_path is not None or self.test_name is not None:
            raise ValueError(
                "only playwright test plan items may declare testPath or testName"
            )
        return self


class TechnicalSpec(SchemaModel):
    framework: str
    starter_capabilities: list[StarterCapabilityId]
    routes: list[RouteSpec] = Field(default_factory=list)
    components: list[ComponentSpec] = Field(default_factory=list)
    component_decisions: list[ComponentDecision] = Field(min_length=1)
    public_api_contracts: list[PublicApiContract] = Field(default_factory=list)
    feature_surfaces: list[FeatureSurfaceSpec] = Field(default_factory=list)
    state_model: list[StateModelSpec] = Field(default_factory=list)
    persistent_state_domains: list[PersistentStateDomainSpec] = Field(default_factory=list)
    state_aggregation: StateAggregationSpec | None = None
    dependencies: list[DependencySpec] = Field(default_factory=list)
    file_plan: list[FilePlanItem] = Field(default_factory=list)
    test_plan: list[TestPlanItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    @field_validator("starter_capabilities")
    @classmethod
    def unique_starter_capabilities(
        cls, values: list[StarterCapabilityId]
    ) -> list[StarterCapabilityId]:
        if len(values) != len(set(values)):
            raise ValueError("starter capabilities must not contain a duplicate")
        return values


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

    @model_validator(mode="after")
    def _enforce_gate_scope_contract(self) -> GateResult:
        acceptance_fields = (
            self.acceptance_id,
            self.test_path,
            self.test_name,
            self.outcome,
        )
        if self.scope == "project":
            if any(value is not None for value in acceptance_fields):
                raise ValueError(
                    "project-scope gates must not carry acceptanceId, testPath, testName, or outcome"
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
        for label, value, limit in (
            ("testPath", self.test_path, 512),
            ("testName", self.test_name, 300),
        ):
            if value is not None and (len(value) > limit or not value.strip()):
                raise ValueError(f"{label} must be a bounded non-blank value")
        return self


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


VisibleArtifactKind = Literal["product_spec", "technical_spec"]

# The fixed role for every visible kind. The persisted artifact record carries
# no role, so the workspace role is derived from the kind alone.
VisibleArtifactRole = Literal["product_manager", "architect"]

# The one closed set of artifact kinds surfaced by the workspace. The order is
# the canonical presentation order (Product then Architect) and the kind->role
# mapping is fixed because the persisted artifact record carries no role.
VISIBLE_ARTIFACT_KIND_ORDER: tuple[VisibleArtifactKind, ...] = (
    "product_spec",
    "technical_spec",
)
ARTIFACT_KIND_TO_ROLE: dict[VisibleArtifactKind, VisibleArtifactRole] = {
    "product_spec": "product_manager",
    "technical_spec": "architect",
}


class ArtifactRefResponse(SchemaModel):
    """A lightweight visible-artifact reference; never carries content."""

    id: str
    run_id: str
    kind: VisibleArtifactKind
    role: VisibleArtifactRole
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
        elif self.url is not None:
            raise ValueError("expired or unavailable preview must have url null")
        return self


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
