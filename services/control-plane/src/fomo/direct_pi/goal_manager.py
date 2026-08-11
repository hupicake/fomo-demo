"""Repository-independent decisions for executing a frozen GoalGraph.

The manager deliberately owns no persistence, sessions, sandboxes, or retry
loop.  It validates the graph's current projection and returns a new validated
projection plus deterministic execution/verification inputs for adapters to
persist and execute.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .goalgraph import (
    LEGACY_ROUTE_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    NAVIGATION_SUITE_VERSION,
    SCHEMA_VERSION,
    Goal,
    GoalGraph,
    GoalStatus,
    GraphStatus,
    NavigationMode,
    NavigationRoute,
    NavigationVerificationSuite,
    ScopedAcceptanceContract,
    derive_navigation_verification_suite,
    scope_acceptance_contract,
    transition_goal_status,
    transition_graph_status,
)


class GoalManagerError(ValueError):
    """Base error for fail-closed Goal Manager decisions."""


class GoalStateConflict(GoalManagerError):
    """The persisted projection violates the single-current-goal invariant."""


class GoalGraphBlocked(GoalManagerError):
    """Pending work exists but no goal can run under the frozen dependencies."""


class RuntimeValidationMode(StrEnum):
    """Server-selected deterministic validation breadth."""

    FOCUSED = "focused"
    FULL = "full"


class RuntimeValidationReason(StrEnum):
    """Auditable reason for the server-selected validation breadth."""

    P0_RELEASE = "p0_release"
    GOAL_FOCUSED = "goal_focused"
    FINAL_GOAL = "final_goal"
    PROJECT_CONFIG_CHANGED = "project_config_changed"
    PRIOR_GOAL_FILE_CHANGED = "prior_goal_file_changed"
    LEGACY_CHECKPOINT_UNKNOWN_PATHS = "legacy_checkpoint_unknown_paths"
    VERIFIED_GRAPH_RECOVERY = "verified_graph_recovery"


@dataclass(frozen=True, slots=True)
class VerifiedGoalEvidence:
    """Bounded, prompt-safe references to FOMO-owned evidence.

    Raw command output and model transcripts are intentionally not representable
    here.  Adapters should resolve durable evidence records into these identifiers.
    """

    goal_id: str
    passed_acceptance_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        safe_identifier = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
        safe_reference = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_./-]{0,255}$")
        if not safe_identifier.fullmatch(self.goal_id):
            raise ValueError("evidence goal_id must be a safe identifier")
        if not self.passed_acceptance_ids:
            raise ValueError("verified evidence requires passed acceptance ids")
        if len(self.passed_acceptance_ids) != len(set(self.passed_acceptance_ids)):
            raise ValueError("passed acceptance ids must be unique")
        if any(not safe_identifier.fullmatch(item) for item in self.passed_acceptance_ids):
            raise ValueError("passed acceptance ids must be safe identifiers")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence refs must be unique")
        if any(not safe_reference.fullmatch(item) for item in self.evidence_refs):
            raise ValueError("evidence refs must be bounded durable references")


@dataclass(frozen=True, slots=True)
class RegressionSuite:
    """The server-selected validation set for one claimed candidate."""

    claimed_goal_id: str
    goal_ids: tuple[str, ...]
    contracts: tuple[ScopedAcceptanceContract, ...]
    mode: RuntimeValidationMode
    reason: RuntimeValidationReason
    navigation_suite: NavigationVerificationSuite | None = None

    def __post_init__(self) -> None:
        if not self.goal_ids or self.goal_ids[-1] != self.claimed_goal_id:
            raise ValueError("regression suite must end with the claimed goal")
        if self.goal_ids != tuple(item.goal_id for item in self.contracts):
            raise ValueError("regression suite goal ids and contracts must match")
        if len(self.goal_ids) != len(set(self.goal_ids)):
            raise ValueError("regression suite goal ids must be unique")
        if self.mode is RuntimeValidationMode.FOCUSED:
            if self.goal_ids != (self.claimed_goal_id,):
                raise ValueError("focused validation must contain only the claimed goal")
            if self.reason is not RuntimeValidationReason.GOAL_FOCUSED:
                raise ValueError("focused validation requires the goal-focused reason")
        elif self.reason in {
            RuntimeValidationReason.GOAL_FOCUSED,
        }:
            raise ValueError("full validation requires a full-suite reason")


@dataclass(frozen=True, slots=True)
class GoalExecutionPlan:
    """One server-selected build target bound to a frozen graph revision."""

    graph_revision: int
    graph_schema_version: int
    navigation_mode: NavigationMode
    routes: tuple[NavigationRoute, ...]
    navigation_suite_version: int | None
    active_goal: Goal
    verified_evidence: tuple[VerifiedGoalEvidence, ...]

    def __post_init__(self) -> None:
        if self.graph_revision < 1:
            raise ValueError("graph_revision must be positive")
        if self.graph_schema_version not in {
            LEGACY_SCHEMA_VERSION,
            LEGACY_ROUTE_SCHEMA_VERSION,
            SCHEMA_VERSION,
        }:
            raise ValueError("graph_schema_version must be supported")
        expected_mode = (
            NavigationMode.MULTI_ROUTE
            if self.graph_schema_version in {LEGACY_ROUTE_SCHEMA_VERSION, SCHEMA_VERSION}
            and len(self.routes) >= 2
            else NavigationMode.SINGLE_SURFACE
        )
        if self.navigation_mode is not expected_mode:
            raise ValueError("execution plan navigation contract is inconsistent")
        expected_suite_version = (
            NAVIGATION_SUITE_VERSION
            if self.graph_schema_version == SCHEMA_VERSION
            else None
        )
        if self.navigation_suite_version != expected_suite_version:
            raise ValueError("execution plan navigation suite version is inconsistent")
        if self.active_goal.status is not GoalStatus.ACTIVE:
            raise ValueError("an execution plan requires exactly one active goal")


_CURRENT_STATUSES = frozenset({GoalStatus.ACTIVE, GoalStatus.CLAIMED})


def _current_goals(graph: GoalGraph) -> tuple[Goal, ...]:
    return tuple(goal for goal in graph.goals if goal.status in _CURRENT_STATUSES)


def _assert_single_current_goal(graph: GoalGraph) -> Goal | None:
    current = _current_goals(graph)
    if len(current) > 1:
        goal_ids = ", ".join(goal.goal_id for goal in current)
        raise GoalStateConflict(f"multiple active/claimed goals are forbidden: {goal_ids}")
    return current[0] if current else None


def _require_active_graph(graph: GoalGraph) -> None:
    if graph.status is not GraphStatus.ACTIVE:
        raise GoalManagerError(f"goal graph is not active: {graph.status}")


def _goal_by_id(graph: GoalGraph, goal_id: str) -> Goal:
    matches = [goal for goal in graph.goals if goal.goal_id == goal_id]
    if len(matches) != 1:
        raise GoalManagerError(f"unknown goal: {goal_id}")
    return matches[0]


def _replace_goal_status(
    graph: GoalGraph,
    *,
    goal_id: str,
    target: GoalStatus,
    graph_status: GraphStatus | None = None,
) -> GoalGraph:
    goal = _goal_by_id(graph, goal_id)
    transition_goal_status(goal.status, target)
    payload = graph.model_dump(mode="python")
    for item in payload["goals"]:
        if item["goal_id"] == goal_id:
            item["status"] = target
            break
    if graph_status is not None:
        payload["status"] = graph_status
    updated = GoalGraph.model_validate(payload)
    _assert_single_current_goal(updated)
    return updated


def select_executable_goal(graph: GoalGraph) -> Goal | None:
    """Select the sole current goal or the first runnable pending goal.

    Selection is entirely server-side and follows the graph's frozen order.
    Callers cannot nominate a goal.  ``None`` means either all work is terminal
    or pending goals are blocked by non-verified dependencies.
    """

    if graph.status is not GraphStatus.ACTIVE:
        return None
    current = _assert_single_current_goal(graph)
    if current is not None:
        if current.status is GoalStatus.ACTIVE:
            verified = {goal.goal_id for goal in graph.goals if goal.status is GoalStatus.VERIFIED}
            if not set(current.depends_on).issubset(verified):
                raise GoalStateConflict(
                    f"active goal {current.goal_id} has unverified dependencies"
                )
        return current

    verified = {goal.goal_id for goal in graph.goals if goal.status is GoalStatus.VERIFIED}
    return next(
        (
            goal
            for goal in graph.goals
            if goal.status is GoalStatus.PENDING and set(goal.depends_on).issubset(verified)
        ),
        None,
    )


def activate_next_goal(graph: GoalGraph) -> GoalGraph:
    """Return a projection with the deterministic next goal activated."""

    selected = select_executable_goal(graph)
    if selected is None:
        pending = [goal.goal_id for goal in graph.goals if goal.status is GoalStatus.PENDING]
        if pending:
            raise GoalGraphBlocked(
                "pending goals are blocked by unverified dependencies: " + ", ".join(pending)
            )
        raise GoalGraphBlocked("the graph has no executable goal")
    if selected.status in _CURRENT_STATUSES:
        return graph
    return _replace_goal_status(
        graph,
        goal_id=selected.goal_id,
        target=GoalStatus.ACTIVE,
    )


def claim_active_goal(graph: GoalGraph) -> GoalGraph:
    """Record the current model claim without treating it as verification."""

    _require_active_graph(graph)
    current = _assert_single_current_goal(graph)
    if current is None or current.status is not GoalStatus.ACTIVE:
        raise GoalManagerError("exactly one active goal is required before claim")
    return _replace_goal_status(
        graph,
        goal_id=current.goal_id,
        target=GoalStatus.CLAIMED,
    )


def retry_claimed_goal(graph: GoalGraph) -> GoalGraph:
    """Return the sole claimed goal to active for a same-goal repair turn."""

    _require_active_graph(graph)
    current = _assert_single_current_goal(graph)
    if current is None or current.status is not GoalStatus.CLAIMED:
        raise GoalManagerError("exactly one claimed goal is required before retry")
    return _replace_goal_status(
        graph,
        goal_id=current.goal_id,
        target=GoalStatus.ACTIVE,
    )


def verify_claimed_goal(graph: GoalGraph) -> GoalGraph:
    """Promote a claim only after the caller has durable FOMO QA evidence."""

    _require_active_graph(graph)
    current = _assert_single_current_goal(graph)
    if current is None or current.status is not GoalStatus.CLAIMED:
        raise GoalManagerError("exactly one claimed goal is required before verification")
    is_last = all(
        goal.goal_id == current.goal_id or goal.status is GoalStatus.VERIFIED
        for goal in graph.goals
    )
    graph_status = None
    if is_last:
        graph_status = transition_graph_status(graph.status, GraphStatus.VERIFIED)
    return _replace_goal_status(
        graph,
        goal_id=current.goal_id,
        target=GoalStatus.VERIFIED,
        graph_status=graph_status,
    )


def build_regression_suite(
    graph: GoalGraph,
    *,
    full_reason: RuntimeValidationReason | None = None,
) -> RegressionSuite:
    """Select focused-by-default QA, escalating final or shared changes to full."""

    _require_active_graph(graph)
    current = _assert_single_current_goal(graph)
    if current is None or current.status is not GoalStatus.CLAIMED:
        raise GoalManagerError("a claimed goal is required to build a regression suite")
    is_final = all(
        goal.goal_id == current.goal_id or goal.status is GoalStatus.VERIFIED
        for goal in graph.goals
    )
    if is_final:
        mode = RuntimeValidationMode.FULL
        reason = RuntimeValidationReason.FINAL_GOAL
    elif full_reason is not None:
        if full_reason not in {
            RuntimeValidationReason.PROJECT_CONFIG_CHANGED,
            RuntimeValidationReason.PRIOR_GOAL_FILE_CHANGED,
            RuntimeValidationReason.LEGACY_CHECKPOINT_UNKNOWN_PATHS,
        }:
            raise GoalManagerError("invalid early full-validation reason")
        mode = RuntimeValidationMode.FULL
        reason = full_reason
    else:
        mode = RuntimeValidationMode.FOCUSED
        reason = RuntimeValidationReason.GOAL_FOCUSED

    selected = (
        tuple(
            goal
            for goal in graph.goals
            if goal.status is GoalStatus.VERIFIED or goal.goal_id == current.goal_id
        )
        if mode is RuntimeValidationMode.FULL
        else (current,)
    )
    goal_ids = tuple(goal.goal_id for goal in selected)
    return RegressionSuite(
        claimed_goal_id=current.goal_id,
        goal_ids=goal_ids,
        contracts=tuple(scope_acceptance_contract(goal) for goal in selected),
        mode=mode,
        reason=reason,
        navigation_suite=derive_navigation_verification_suite(
            graph,
            goal_ids=goal_ids,
            mode=(
                "final_full"
                if is_final
                else "ready_full"
                if mode is RuntimeValidationMode.FULL
                else "focused"
            ),
        ),
    )


_PROJECT_CONFIG_PATHS = frozenset(
    {
        "components.json",
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.ts",
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "postcss.config.js",
        "postcss.config.mjs",
        "postcss.config.ts",
        "tailwind.config.js",
        "tailwind.config.mjs",
        "tailwind.config.ts",
        "tsconfig.json",
    }
)


def early_full_validation_reason(
    goal_changed_paths: Iterable[str],
    *,
    prior_goal_changed_paths: Iterable[str] = (),
) -> RuntimeValidationReason | None:
    """Return a deterministic escalation reason for one goal's actual delta."""

    changed = frozenset(goal_changed_paths)
    if any(
        path in _PROJECT_CONFIG_PATHS
        or ("/" not in path and path.startswith("tsconfig") and path.endswith(".json"))
        for path in changed
    ):
        return RuntimeValidationReason.PROJECT_CONFIG_CHANGED
    if changed.intersection(prior_goal_changed_paths):
        return RuntimeValidationReason.PRIOR_GOAL_FILE_CHANGED
    return None


def plan_goal_execution(
    graph: GoalGraph,
    *,
    graph_revision: int,
    verified_evidence: Iterable[VerifiedGoalEvidence] = (),
) -> tuple[GoalGraph, GoalExecutionPlan]:
    """Activate and describe the next deterministic goal build turn."""

    activated = activate_next_goal(graph)
    current = _assert_single_current_goal(activated)
    if current is None or current.status is not GoalStatus.ACTIVE:
        raise GoalManagerError("a claimed goal must be retried before it can be planned")

    summaries = tuple(verified_evidence)
    verified_goal_ids = {
        goal.goal_id for goal in activated.goals if goal.status is GoalStatus.VERIFIED
    }
    summary_ids = [item.goal_id for item in summaries]
    if len(summary_ids) != len(set(summary_ids)):
        raise GoalManagerError("verified evidence summaries must be unique by goal")
    if set(summary_ids) != verified_goal_ids:
        raise GoalManagerError("evidence summaries must cover every verified goal exactly once")
    summary_by_goal_id = {item.goal_id: item for item in summaries}
    for goal in activated.goals:
        if goal.status is not GoalStatus.VERIFIED:
            continue
        expected_ids = {item.id for item in goal.acceptance.criteria}
        actual_ids = set(summary_by_goal_id[goal.goal_id].passed_acceptance_ids)
        if actual_ids != expected_ids:
            raise GoalManagerError(
                f"verified evidence does not cover frozen acceptance for {goal.goal_id}"
            )

    ordered = tuple(
        summary_by_goal_id[goal.goal_id]
        for goal in activated.goals
        if goal.goal_id in summary_by_goal_id
    )
    return activated, GoalExecutionPlan(
        graph_revision=graph_revision,
        graph_schema_version=activated.schema_version,
        navigation_mode=activated.navigation_mode,
        routes=tuple(activated.routes),
        navigation_suite_version=activated.navigation_suite_version,
        active_goal=current,
        verified_evidence=ordered,
    )


__all__ = [
    "GoalExecutionPlan",
    "GoalGraphBlocked",
    "GoalManagerError",
    "GoalStateConflict",
    "RegressionSuite",
    "RuntimeValidationMode",
    "RuntimeValidationReason",
    "VerifiedGoalEvidence",
    "activate_next_goal",
    "build_regression_suite",
    "claim_active_goal",
    "early_full_validation_reason",
    "plan_goal_execution",
    "retry_claimed_goal",
    "select_executable_goal",
    "verify_claimed_goal",
]
