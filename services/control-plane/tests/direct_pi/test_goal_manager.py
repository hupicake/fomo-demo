from __future__ import annotations

import inspect
import json

import pytest

from fomo.direct_pi.acceptance import (
    ACCEPTANCE_CONFIG_PATH,
    FOMO_HARNESS_PATH,
    AcceptanceCompilationError,
    compile_acceptance,
    compile_acceptance_suite,
    compile_goal_acceptance,
)
from fomo.direct_pi.goal_manager import (
    GoalGraphBlocked,
    GoalStateConflict,
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
from fomo.direct_pi.goalgraph import (
    GoalGraph,
    GoalStatus,
    materialize_goal_graph,
    parse_goal_graph_draft,
)
from fomo.direct_pi.prompts import (
    PRODUCT_DESIGN_POLICY,
    PRODUCT_REQUIREMENTS_POLICY,
    _bounded_goal_diagnostic,
    goal_build_prompt,
    goal_graph_planning_prompt,
    goal_repair_prompt,
)

_ADVISORY_SELF_CHECK_COMMAND = (
    "pnpm typecheck && pnpm exec playwright test "
    "tests/fomo-acceptance/G-2/test-2.smoke.spec.ts "
    "--config=playwright.config.ts --project=chromium "
    "--workers=1 --retries=0 --reporter=line"
)


def _acceptance(index: int) -> dict[str, object]:
    return {
        "criteria": [
            {
                "id": f"AC-{index}",
                "title": f"Outcome {index}",
                "priority": "must",
                "given": "The product is open",
                "when": "The workflow is completed",
                "then": f"Outcome {index} is visible",
            }
        ],
        "tests": [
            {
                "id": f"test-{index}",
                "acceptanceId": f"AC-{index}",
                "title": f"verifies outcome {index}",
                "actions": [{"kind": "goto", "path": "/"}],
                "assertions": [{"kind": "url", "path": "/"}],
            }
        ],
    }


def _graph() -> GoalGraph:
    return materialize_goal_graph(
        parse_goal_graph_draft(
            {
                "schemaVersion": 1,
                "productOutcome": "Users can complete both workflows.",
                "goals": [
                    {
                        "goalId": "G-1",
                        "title": "First goal",
                        "productOutcome": "Users can complete the first workflow.",
                        "userVisible": True,
                        "dependsOn": [],
                        "acceptance": _acceptance(1),
                    },
                    {
                        "goalId": "G-2",
                        "title": "Second goal",
                        "productOutcome": "Users can complete the second workflow.",
                        "userVisible": True,
                        "dependsOn": ["G-1"],
                        "acceptance": _acceptance(2),
                    },
                ],
            }
        )
    )


def _three_goal_graph() -> GoalGraph:
    return materialize_goal_graph(
        parse_goal_graph_draft(
            {
                "schemaVersion": 1,
                "productOutcome": "Users can complete all three workflows.",
                "goals": [
                    {
                        "goalId": f"G-{index}",
                        "title": f"Goal {index}",
                        "productOutcome": f"Users can complete workflow {index}.",
                        "userVisible": True,
                        "dependsOn": [] if index == 1 else [f"G-{index - 1}"],
                        "acceptance": _acceptance(index),
                    }
                    for index in range(1, 4)
                ],
            }
        )
    )


def _with_statuses(graph: GoalGraph, *statuses: GoalStatus) -> GoalGraph:
    payload = graph.model_dump(mode="python")
    for goal, status in zip(payload["goals"], statuses, strict=True):
        goal["status"] = status
    return GoalGraph.model_validate(payload)


def test_selects_first_topological_goal_without_planner_choice() -> None:
    graph = _graph()

    selected = select_executable_goal(graph)
    activated, plan = plan_goal_execution(graph, graph_revision=3)

    assert selected is not None and selected.goal_id == "G-1"
    assert plan.active_goal.goal_id == "G-1"
    assert plan.graph_revision == 3
    assert activated.goals[0].status is GoalStatus.ACTIVE
    assert "goal_id" not in inspect.signature(plan_goal_execution).parameters


def test_blocked_dependencies_and_multiple_current_goals_fail_closed() -> None:
    blocked = _with_statuses(_graph(), GoalStatus.FAILED, GoalStatus.PENDING)
    assert select_executable_goal(blocked) is None
    with pytest.raises(GoalGraphBlocked, match="blocked by unverified dependencies"):
        activate_next_goal(blocked)

    conflicting = _with_statuses(_graph(), GoalStatus.ACTIVE, GoalStatus.CLAIMED)
    with pytest.raises(GoalStateConflict, match="multiple active/claimed"):
        select_executable_goal(conflicting)


def test_claim_is_only_candidate_and_retry_returns_same_goal_to_active() -> None:
    active = activate_next_goal(_graph())
    claimed = claim_active_goal(active)

    assert claimed.goals[0].status is GoalStatus.CLAIMED
    assert claimed.goals[0].status is not GoalStatus.VERIFIED
    assert retry_claimed_goal(claimed).goals[0].status is GoalStatus.ACTIVE


def test_second_goal_regression_contains_verified_g1_and_claimed_g2() -> None:
    first = claim_active_goal(activate_next_goal(_graph()))
    after_first = verify_claimed_goal(first)
    second = claim_active_goal(activate_next_goal(after_first))

    suite = build_regression_suite(second)

    assert suite.claimed_goal_id == "G-2"
    assert suite.goal_ids == ("G-1", "G-2")
    assert [item.goal_id for item in suite.contracts] == ["G-1", "G-2"]


def test_middle_goal_uses_focused_suite_for_only_the_current_claim() -> None:
    first = claim_active_goal(activate_next_goal(_three_goal_graph()))
    after_first = verify_claimed_goal(first)
    middle = claim_active_goal(activate_next_goal(after_first))

    suite = build_regression_suite(middle)

    assert suite.mode is RuntimeValidationMode.FOCUSED
    assert suite.reason is RuntimeValidationReason.GOAL_FOCUSED
    assert suite.claimed_goal_id == "G-2"
    assert suite.goal_ids == ("G-2",)
    assert [item.goal_id for item in suite.contracts] == ["G-2"]


def test_final_goal_forces_full_suite_across_every_implemented_goal() -> None:
    first = claim_active_goal(activate_next_goal(_three_goal_graph()))
    after_first = verify_claimed_goal(first)
    middle = claim_active_goal(activate_next_goal(after_first))
    after_middle = verify_claimed_goal(middle)
    final = claim_active_goal(activate_next_goal(after_middle))

    suite = build_regression_suite(final)

    assert suite.mode is RuntimeValidationMode.FULL
    assert suite.reason is RuntimeValidationReason.FINAL_GOAL
    assert suite.claimed_goal_id == "G-3"
    assert suite.goal_ids == ("G-1", "G-2", "G-3")


@pytest.mark.parametrize(
    ("goal_paths", "prior_paths", "expected_reason"),
    [
        (
            ("package.json", "app/goal-two/page.tsx"),
            (),
            RuntimeValidationReason.PROJECT_CONFIG_CHANGED,
        ),
        (
            ("app/shared-shell.tsx", "app/goal-two/page.tsx"),
            ("app/shared-shell.tsx", "app/goal-one/page.tsx"),
            RuntimeValidationReason.PRIOR_GOAL_FILE_CHANGED,
        ),
    ],
)
def test_shared_change_escalates_middle_goal_to_full_suite(
    goal_paths: tuple[str, ...],
    prior_paths: tuple[str, ...],
    expected_reason: RuntimeValidationReason,
) -> None:
    first = claim_active_goal(activate_next_goal(_three_goal_graph()))
    after_first = verify_claimed_goal(first)
    middle = claim_active_goal(activate_next_goal(after_first))
    reason = early_full_validation_reason(
        goal_paths,
        prior_goal_changed_paths=prior_paths,
    )

    suite = build_regression_suite(middle, full_reason=reason)

    assert reason is expected_reason
    assert suite.mode is RuntimeValidationMode.FULL
    assert suite.reason is expected_reason
    assert suite.goal_ids == ("G-1", "G-2")


def test_legacy_checkpoint_unknown_paths_escalates_middle_goal_to_full() -> None:
    first = claim_active_goal(activate_next_goal(_three_goal_graph()))
    after_first = verify_claimed_goal(first)
    middle = claim_active_goal(activate_next_goal(after_first))

    suite = build_regression_suite(
        middle,
        full_reason=RuntimeValidationReason.LEGACY_CHECKPOINT_UNKNOWN_PATHS,
    )

    assert suite.mode is RuntimeValidationMode.FULL
    assert suite.reason is RuntimeValidationReason.LEGACY_CHECKPOINT_UNKNOWN_PATHS
    assert suite.goal_ids == ("G-1", "G-2")


def test_goal_acceptance_has_isolated_paths_and_scoped_persistent_keys() -> None:
    graph = _graph()
    compiled = compile_goal_acceptance("G-1", graph.goals[0].acceptance)

    assert compiled.test_path_by_acceptance_id == {
        "G-1:AC-1": "tests/fomo-acceptance/G-1/test-1.smoke.spec.ts"
    }
    assert compiled.acceptance_key_by_id == {"AC-1": "G-1:AC-1"}
    assert [item.path for item in compiled.changes] == sorted(
        item.path for item in compiled.changes
    )


def test_multi_goal_suite_is_stable_and_duplicate_scope_fails_closed() -> None:
    graph = _graph()
    compiled = compile_acceptance_suite(
        {"G-2": graph.goals[1].acceptance, "G-1": graph.goals[0].acceptance}
    )

    assert tuple(compiled.test_path_by_acceptance_id) == ("G-1:AC-1", "G-2:AC-2")
    assert [item.path for item in compiled.changes] == [
        "tests/fomo-acceptance/G-1/test-1.smoke.spec.ts",
        "tests/fomo-acceptance/G-2/test-2.smoke.spec.ts",
        ACCEPTANCE_CONFIG_PATH,
        FOMO_HARNESS_PATH,
    ]
    with pytest.raises(AcceptanceCompilationError, match="duplicate scoped acceptance goal"):
        compile_acceptance_suite(
            [("G-1", graph.goals[0].acceptance), ("G-1", graph.goals[0].acceptance)]
        )


def test_p0_compiler_contract_remains_unscoped() -> None:
    contract = _graph().goals[0].acceptance
    compiled = compile_acceptance(contract)

    assert compiled.test_path_by_acceptance_id == {
        "AC-1": "tests/fomo-acceptance/test-1.smoke.spec.ts"
    }
    assert compiled.acceptance_key_by_id is None


def test_goal_prompts_bind_revision_and_exclude_raw_diagnostics() -> None:
    verified_g1 = verify_claimed_goal(claim_active_goal(activate_next_goal(_graph())))
    with pytest.raises(ValueError, match="cover every verified goal"):
        plan_goal_execution(verified_g1, graph_revision=7)
    active_g2, plan = plan_goal_execution(
        verified_g1,
        graph_revision=7,
        verified_evidence=(
            VerifiedGoalEvidence(
                goal_id="G-1",
                passed_acceptance_ids=("AC-1",),
                evidence_refs=("verification_evidence:ev-1",),
            ),
        ),
    )
    assert active_g2.goals[1].status is GoalStatus.ACTIVE

    build = goal_build_prompt(
        requirement="Build both workflows.",
        starter={"manifestHash": "safe-hash"},
        execution_plan=plan,
        advisory_self_check_command=_ADVISORY_SELF_CHECK_COMMAND,
    )
    repair = goal_repair_prompt(
        execution_plan=plan,
        round_number=1,
        advisory_self_check_command=_ADVISORY_SELF_CHECK_COMMAND,
        diagnostic={
            "gate": "typecheck",
            "summary": "A typed interface does not match.",
            "affectedFiles": ["app/page.tsx"],
            "gates": [
                {
                    "gate": "acceptance_test",
                    "scope": "acceptance",
                    "status": "failed",
                    "outcome": "failed",
                    "summary": "Acceptance workflow assertion failed.",
                    "acceptanceId": "G-2:AC-1",
                    "testPath": "tests/fomo-acceptance/G-2/outcome.smoke.spec.ts",
                    "testName": "shows the registered attendee",
                    "exitCode": 1,
                    "diagnostic": {
                        "message": (
                            "expect(locator).toBeVisible() failed; "
                            "PASSWORD=prompt-secret"
                        ),
                        "locator": "getByText('张三', { exact: true }).first()",
                        "testName": "shows the registered attendee",
                        "line": 10,
                        "trace": "DO-NOT-LEAK-trace",
                    },
                    "evidence": ["DO-NOT-LEAK-command-output"],
                }
            ],
            "rawLog": "DO-NOT-LEAK-sensitive-terminal-output",
            "stderr": "DO-NOT-LEAK-sensitive-stderr",
        },
    )

    assert '"graphRevision":7' in build
    assert '"goalId":"G-2"' in build
    assert "Goal Manager selected the active goal" in build
    assert "shared foundations" in build
    assert "verification_evidence:ev-1" in build
    assert "public progress text" in build
    assert "Before the first tool batch" in build
    assert "Do not reveal hidden chain-of-thought" in build
    assert _ADVISORY_SELF_CHECK_COMMAND in build
    assert "advisory mirror" in build
    assert "never edit, delete, replace, bypass, or duplicate it" in build
    assert "independently recompiled tests in the clean verification sandbox" in build
    assert "public progress text" in repair
    assert "Before the first repair tool batch" in repair
    assert "make any extra tool call solely to report progress" in repair
    assert _ADVISORY_SELF_CHECK_COMMAND in repair
    assert "advisory only" in repair
    assert "never modify, delete, replace, bypass, or duplicate" in repair
    assert "clean verification sandbox" in repair
    assert "DO-NOT-LEAK" not in repair
    assert "prompt-secret" not in repair
    assert "typed interface" in repair
    assert "getByText('张三', { exact: true }).first()" in repair
    assert '"line":10' in repair
    assert '"testName":"shows the registered attendee"' in repair
    assert "Make the smallest root-cause edits" not in repair
    assert "every root-cause, architectural, state, and product-integrity edit" in repair


def test_goal_prompts_preserve_product_scope_and_apply_design_baseline() -> None:
    planning = goal_graph_planning_prompt(
        requirement="Build a useful event product.",
        starter={"routes": ["/"]},
    )
    _active, plan = plan_goal_execution(_graph(), graph_revision=1)
    building = goal_build_prompt(
        requirement="Build a useful event product.",
        starter={"routes": ["/"]},
        execution_plan=plan,
        advisory_self_check_command=_ADVISORY_SELF_CHECK_COMMAND,
    )

    for prompt in (planning, building):
        assert PRODUCT_DESIGN_POLICY in prompt
        assert PRODUCT_REQUIREMENTS_POLICY in prompt
        assert "Preserve the requested product breadth across the plan" in prompt
        assert "If the user specifies a visual direction, follow it" in prompt
        assert "Do not force an Apple" in prompt
        assert "Make useful, reversible product-design decisions" in prompt
        assert "giant-heading-plus-a-few-cards" in prompt
        assert "as many or as few files and components" in prompt
        assert "verbatim JSON string" in prompt
        assert "Apple-inspired" not in prompt

    assert "GoalGraph structures delivery order, not product ambition" in planning
    assert "derive the number and granularity" in planning
    assert "artificial consolidation or fragmentation" in planning
    assert "Act as a product manager" in planning
    assert "intended users and use context" in planning
    assert "user intent -> action -> system feedback -> completed outcome" in planning
    assert "use product judgment to make reasonable, reversible assumptions" in planning
    assert "`productOutcome` is the compact product brief rather than a slogan" in planning
    assert "During planning, rely on the embedded verified Base Snapshot" in planning
    assert "define 1-3" not in planning
    assert "prefer exactly one goal" not in planning
    assert "acceptance contract is the verification floor" in building
    assert "complete active outcome and all supporting architecture" in building
    assert "make necessary subtraction" in building


def test_goal_repair_diagnostic_has_a_hard_json_cap() -> None:
    diagnostic = {
        "passed": False,
        "gates": [
            {
                "gate": "acceptance_test",
                "scope": "acceptance",
                "status": "failed",
                "outcome": "failed",
                "summary": "failure " + "summary-detail " * 2_000,
                "acceptanceId": f"G-1:AC-{index}",
                "testPath": f"tests/fomo-acceptance/failure-{index}.smoke.spec.ts",
                "testName": "test " + "name-detail " * 2_000,
                "diagnostic": {
                    "message": "assertion " + "message-detail " * 20_000,
                    "locator": "getByText('missing') " + "locator-detail " * 20_000,
                    "testName": "test " + "title-detail " * 20_000,
                    "line": index + 1,
                    "body": "A" * 1_000_000,
                    "trace": "private-trace",
                },
            }
            for index in range(20)
        ],
        "rawLog": "raw-terminal-output",
    }

    bounded = _bounded_goal_diagnostic(diagnostic)
    rendered = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))

    assert len(rendered) <= 12_000
    assert "omittedFailedGateCount" in bounded
    assert "raw-terminal-output" not in rendered
    assert "private-trace" not in rendered
    assert "A" * 1_000 not in rendered
