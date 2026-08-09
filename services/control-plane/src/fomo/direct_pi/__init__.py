"""Direct Pi orchestration contracts and runtime."""

from typing import TYPE_CHECKING, Any

from .acceptance import (
    ACCEPTANCE_CONFIG_PATH,
    ACCEPTANCE_ROOT,
    AcceptanceCompilationError,
    CompiledAcceptance,
    compile_acceptance,
    compile_acceptance_suite,
    compile_goal_acceptance,
)
from .contracts import AcceptanceContract, BuildPlan, PlanningBundle
from .goal_manager import (
    GoalExecutionPlan,
    GoalGraphBlocked,
    GoalManagerError,
    GoalStateConflict,
    RegressionSuite,
    RuntimeValidationMode,
    RuntimeValidationReason,
    VerifiedGoalEvidence,
    activate_next_goal,
    build_regression_suite,
    claim_active_goal,
    early_full_validation_reason,
    plan_goal_execution,
    retry_claimed_goal,
    select_executable_goal,
    verify_claimed_goal,
)
from .goalgraph import (
    Goal,
    GoalDraft,
    GoalGraph,
    GoalGraphDraft,
    GoalGraphQualityBar,
    GoalStatus,
    GraphStatus,
    acceptance_persistence_key,
    acceptance_test_path,
    acceptance_test_paths,
    materialize_goal_graph,
    parse_goal_graph,
    parse_goal_graph_draft,
    parse_persisted_goal_graph,
    scope_acceptance_contract,
    serialize_goal_graph,
    serialize_goal_graph_draft,
    transition_goal_status,
    transition_graph_status,
)

if TYPE_CHECKING:
    from .orchestrator import DirectPiOrchestrator


def __getattr__(name: str) -> Any:
    """Keep the orchestrator export lazy so domain imports stay acyclic."""

    if name == "DirectPiOrchestrator":
        from .orchestrator import DirectPiOrchestrator

        return DirectPiOrchestrator
    raise AttributeError(name)


__all__ = [
    "ACCEPTANCE_CONFIG_PATH",
    "ACCEPTANCE_ROOT",
    "AcceptanceCompilationError",
    "AcceptanceContract",
    "BuildPlan",
    "CompiledAcceptance",
    "DirectPiOrchestrator",
    "Goal",
    "GoalDraft",
    "GoalExecutionPlan",
    "GoalGraph",
    "GoalGraphBlocked",
    "GoalGraphDraft",
    "GoalGraphQualityBar",
    "GoalManagerError",
    "GoalStatus",
    "GoalStateConflict",
    "GraphStatus",
    "PlanningBundle",
    "RegressionSuite",
    "RuntimeValidationMode",
    "RuntimeValidationReason",
    "VerifiedGoalEvidence",
    "activate_next_goal",
    "acceptance_persistence_key",
    "acceptance_test_path",
    "acceptance_test_paths",
    "build_regression_suite",
    "claim_active_goal",
    "compile_acceptance",
    "compile_acceptance_suite",
    "compile_goal_acceptance",
    "early_full_validation_reason",
    "materialize_goal_graph",
    "parse_goal_graph",
    "parse_goal_graph_draft",
    "parse_persisted_goal_graph",
    "plan_goal_execution",
    "retry_claimed_goal",
    "select_executable_goal",
    "scope_acceptance_contract",
    "serialize_goal_graph",
    "serialize_goal_graph_draft",
    "transition_goal_status",
    "transition_graph_status",
    "verify_claimed_goal",
]
