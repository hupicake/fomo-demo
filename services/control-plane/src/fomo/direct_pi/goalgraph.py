"""Strict, engine-neutral GoalGraph domain contracts.

The graph is deliberately limited to frozen product outcomes, dependency
ordering, and the existing FOMO-owned acceptance DSL.  Execution and
checkpoint policy belong to the control plane, not to this schema.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from fomo.schemas import SchemaModel

from .contracts import AcceptanceContract, AcceptanceTest, Identifier

SCHEMA_VERSION = 1
ACCEPTANCE_ROOT = "tests/fomo-acceptance"

ProductOutcome = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
GoalProductOutcome = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
GoalTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]


class GraphStatus(StrEnum):
    ACTIVE = "active"
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class GoalStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    CLAIMED = "claimed"
    VERIFIED = "verified"
    FAILED = "failed"
    SUPERSEDED = "superseded"


QualityGate = Literal[
    "deps",
    "typecheck",
    "build",
    "harness-smoke",
    "per-goal-acceptance",
    "preview-health",
]
QUALITY_GATES: tuple[QualityGate, ...] = (
    "deps",
    "typecheck",
    "build",
    "harness-smoke",
    "per-goal-acceptance",
    "preview-health",
)


class GoalGraphQualityBar(SchemaModel):
    """The server-owned quality policy serialized with every graph.

    Every member is a literal and the complete gate sequence is validated, so
    untrusted planner output can neither weaken nor extend this policy.
    """

    model_config = ConfigDict(frozen=True)

    gates: tuple[QualityGate, ...] = QUALITY_GATES
    must_acceptance: Literal["all"] = "all"
    release_evidence: Literal["fomo_qa_only"] = "fomo_qa_only"

    @model_validator(mode="after")
    def fixed_server_policy(self) -> GoalGraphQualityBar:
        if self.gates != QUALITY_GATES:
            raise ValueError("qualityBar gates are fixed by the server")
        return self


SERVER_QUALITY_BAR = GoalGraphQualityBar()


class GoalDraft(SchemaModel):
    """Planner-owned goal fields; lifecycle state is intentionally absent."""

    goal_id: Identifier
    title: GoalTitle
    product_outcome: GoalProductOutcome
    user_visible: StrictBool
    depends_on: list[Identifier] = Field(default_factory=list, max_length=5)
    acceptance: AcceptanceContract

    @model_validator(mode="after")
    def bounded_unique_acceptance(self) -> GoalDraft:
        criterion_count = len(self.acceptance.criteria)
        if not 1 <= criterion_count <= 8:
            raise ValueError("a goal must contain between 1 and 8 acceptance criteria")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("goal dependencies must be unique")
        if self.goal_id in self.depends_on:
            raise ValueError("a goal cannot depend on itself")
        return self


class Goal(GoalDraft):
    """Trusted persisted goal projection with server-managed lifecycle state."""

    status: GoalStatus


class GoalGraphDraft(SchemaModel):
    """Strict planner payload with no server-owned policy or lifecycle fields."""

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    product_outcome: ProductOutcome
    goals: list[GoalDraft] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def valid_topological_graph(self) -> GoalGraphDraft:
        goal_ids = [goal.goal_id for goal in self.goals]
        if len(goal_ids) != len(set(goal_ids)):
            raise ValueError("goal ids must be unique")

        seen: set[str] = set()
        for goal in self.goals:
            unavailable = [dependency for dependency in goal.depends_on if dependency not in seen]
            if unavailable:
                joined = ", ".join(unavailable)
                raise ValueError(
                    f"goal {goal.goal_id} dependencies must reference earlier goals: {joined}"
                )
            seen.add(goal.goal_id)

        criterion_count = sum(len(goal.acceptance.criteria) for goal in self.goals)
        if criterion_count > 12:
            raise ValueError("a GoalGraph may contain at most 12 acceptance criteria")
        return self


class GoalGraph(GoalGraphDraft):
    """Trusted persisted graph projection with server-owned policy and state."""

    quality_bar: GoalGraphQualityBar
    goals: list[Goal] = Field(min_length=1, max_length=6)
    status: GraphStatus

    @model_validator(mode="after")
    def fixed_quality_bar(self) -> GoalGraph:
        if self.quality_bar != SERVER_QUALITY_BAR:
            raise ValueError("qualityBar is fixed by the server and cannot be overridden")
        return self


class ScopedAcceptanceContract(SchemaModel):
    """One goal's DSL plus its durable keys and isolated test paths."""

    goal_id: Identifier
    contract: AcceptanceContract
    acceptance_key_by_id: dict[Identifier, str]
    test_path_by_test_id: dict[Identifier, str]


class InvalidStatusTransition(ValueError):
    """Raised when a caller attempts a forbidden domain-state transition."""


GRAPH_STATUS_TRANSITIONS: Mapping[GraphStatus, frozenset[GraphStatus]] = MappingProxyType({
    GraphStatus.ACTIVE: frozenset(
        {
            GraphStatus.VERIFIED,
            GraphStatus.FAILED,
            GraphStatus.CANCELLED,
            GraphStatus.SUPERSEDED,
        }
    ),
    GraphStatus.VERIFIED: frozenset(),
    GraphStatus.FAILED: frozenset(),
    GraphStatus.CANCELLED: frozenset(),
    GraphStatus.SUPERSEDED: frozenset(),
})

GOAL_STATUS_TRANSITIONS: Mapping[GoalStatus, frozenset[GoalStatus]] = MappingProxyType({
    GoalStatus.PENDING: frozenset({GoalStatus.ACTIVE, GoalStatus.SUPERSEDED}),
    GoalStatus.ACTIVE: frozenset(
        {GoalStatus.CLAIMED, GoalStatus.FAILED, GoalStatus.SUPERSEDED}
    ),
    GoalStatus.CLAIMED: frozenset(
        {
            GoalStatus.ACTIVE,
            GoalStatus.VERIFIED,
            GoalStatus.FAILED,
            GoalStatus.SUPERSEDED,
        }
    ),
    GoalStatus.VERIFIED: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.SUPERSEDED: frozenset(),
})

_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)


def _identifier(value: str, label: str) -> str:
    try:
        return _IDENTIFIER_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"{label} must be a safe identifier") from exc


def acceptance_persistence_key(goal_id: str, acceptance_id: str) -> str:
    """Return the globally unique durable key for one goal-local criterion."""

    safe_goal_id = _identifier(goal_id, "goal_id")
    safe_acceptance_id = _identifier(acceptance_id, "acceptance_id")
    return f"{safe_goal_id}:{safe_acceptance_id}"


def acceptance_test_path(goal_id: str, test_id: str) -> str:
    """Return the isolated FOMO-owned Playwright path for a goal-local test."""

    safe_goal_id = _identifier(goal_id, "goal_id")
    safe_test_id = _identifier(test_id, "test_id")
    return f"{ACCEPTANCE_ROOT}/{safe_goal_id}/{safe_test_id}.smoke.spec.ts"


def acceptance_test_paths(
    goal_id: str,
    tests: Sequence[AcceptanceTest],
) -> dict[str, str]:
    """Map every test id to a path without collapsing shared criteria."""

    paths = {item.id: acceptance_test_path(goal_id, item.id) for item in tests}
    if len(paths) != len(tests):
        raise ValueError("acceptance test ids must be unique")
    return paths


def scope_acceptance_contract(
    goal_or_id: GoalDraft | str,
    contract: AcceptanceContract | None = None,
) -> ScopedAcceptanceContract:
    """Attach durable keys and test paths without mutating the frozen DSL.

    Passing a ``Goal`` is the normal domain path.  The two-argument form keeps
    the helper useful to adapters that already hold a validated contract.
    """

    if isinstance(goal_or_id, GoalDraft):
        if contract is not None:
            raise ValueError("contract must be omitted when scoping a GoalDraft")
        goal_id = goal_or_id.goal_id
        contract = goal_or_id.acceptance
    else:
        goal_id = _identifier(goal_or_id, "goal_id")
        if contract is None:
            raise ValueError("contract is required when scoping by goal_id")

    return ScopedAcceptanceContract(
        goal_id=goal_id,
        contract=contract,
        acceptance_key_by_id={
            item.id: acceptance_persistence_key(goal_id, item.id)
            for item in contract.criteria
        },
        test_path_by_test_id=acceptance_test_paths(goal_id, contract.tests),
    )


def can_transition_graph_status(current: GraphStatus, target: GraphStatus) -> bool:
    return target in GRAPH_STATUS_TRANSITIONS.get(current, frozenset())


def transition_graph_status(current: GraphStatus, target: GraphStatus) -> GraphStatus:
    if not can_transition_graph_status(current, target):
        raise InvalidStatusTransition(f"illegal graph status transition: {current} -> {target}")
    return target


def can_transition_goal_status(current: GoalStatus, target: GoalStatus) -> bool:
    return target in GOAL_STATUS_TRANSITIONS.get(current, frozenset())


def transition_goal_status(current: GoalStatus, target: GoalStatus) -> GoalStatus:
    if not can_transition_goal_status(current, target):
        raise InvalidStatusTransition(f"illegal goal status transition: {current} -> {target}")
    return target


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _parse_json_object(
    payload: str | bytes | bytearray | Mapping[str, Any],
    *,
    label: str,
) -> Mapping[str, Any]:
    value: Any = payload
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            value = json.loads(
                payload,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} payload must be a JSON object")
    return value


def parse_goal_graph_draft(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> GoalGraphDraft:
    """Parse untrusted planner output; server-owned fields are forbidden."""

    return GoalGraphDraft.model_validate(_parse_json_object(payload, label="GoalGraphDraft"))


def materialize_goal_graph(draft: GoalGraphDraft) -> GoalGraph:
    """Create the initial trusted projection with server-owned policy/state."""

    return GoalGraph(
        schema_version=draft.schema_version,
        product_outcome=draft.product_outcome,
        quality_bar=SERVER_QUALITY_BAR,
        goals=[
            Goal.model_validate({**goal.model_dump(), "status": GoalStatus.PENDING})
            for goal in draft.goals
        ],
        status=GraphStatus.ACTIVE,
    )


def parse_persisted_goal_graph(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> GoalGraph:
    """Parse a trusted persisted projection, never untrusted planner output."""

    return GoalGraph.model_validate(_parse_json_object(payload, label="GoalGraph"))


def parse_goal_graph(payload: str | bytes | bytearray | Mapping[str, Any]) -> GoalGraph:
    """Compatibility alias for trusted persisted GoalGraph projections only.

    Planner/model output must always go through :func:`parse_goal_graph_draft`
    followed by :func:`materialize_goal_graph`.
    """

    return parse_persisted_goal_graph(payload)


def serialize_goal_graph(graph: GoalGraph | Mapping[str, Any]) -> str:
    """Return deterministic canonical JSON suitable for hashing/persistence."""

    validated = graph if isinstance(graph, GoalGraph) else parse_persisted_goal_graph(graph)
    return _serialize_model(validated)


def serialize_goal_graph_draft(
    draft: GoalGraphDraft | Mapping[str, Any],
) -> str:
    """Return deterministic canonical JSON for a strict planner draft."""

    validated = draft if isinstance(draft, GoalGraphDraft) else parse_goal_graph_draft(draft)
    return _serialize_model(validated)


def _serialize_model(graph: GoalGraph | GoalGraphDraft) -> str:
    return json.dumps(
        graph.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# Explicit aliases keep call sites readable without creating a second contract.
GoalNode = Goal
scoped_acceptance_key = acceptance_persistence_key
scoped_acceptance_test_path = acceptance_test_path


__all__ = [
    "ACCEPTANCE_ROOT",
    "GOAL_STATUS_TRANSITIONS",
    "GRAPH_STATUS_TRANSITIONS",
    "QUALITY_GATES",
    "SCHEMA_VERSION",
    "SERVER_QUALITY_BAR",
    "Goal",
    "GoalDraft",
    "GoalGraph",
    "GoalGraphDraft",
    "GoalGraphQualityBar",
    "GoalNode",
    "GoalStatus",
    "GraphStatus",
    "InvalidStatusTransition",
    "ScopedAcceptanceContract",
    "acceptance_persistence_key",
    "acceptance_test_path",
    "acceptance_test_paths",
    "can_transition_goal_status",
    "can_transition_graph_status",
    "materialize_goal_graph",
    "parse_goal_graph",
    "parse_goal_graph_draft",
    "parse_persisted_goal_graph",
    "scope_acceptance_contract",
    "scoped_acceptance_key",
    "scoped_acceptance_test_path",
    "serialize_goal_graph",
    "serialize_goal_graph_draft",
    "transition_goal_status",
    "transition_graph_status",
]
