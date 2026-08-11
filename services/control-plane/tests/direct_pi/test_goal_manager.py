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
    parse_legacy_goal_graph_draft,
)
from fomo.direct_pi.prompts import (
    PRODUCT_DESIGN_POLICY,
    PRODUCT_REQUIREMENTS_POLICY,
    _bounded_goal_diagnostic,
    explicit_route_paths,
    goal_build_prompt,
    goal_graph_planning_prompt,
    goal_repair_prompt,
    required_route_count,
    requires_multi_route,
    validate_goal_graph_routing,
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


def _routing_draft(
    *,
    viewport_widths: tuple[int, ...] = (390,),
    missions_deep_linkable: bool = True,
):
    root = _acceptance(1)
    root_test = root["tests"][0]  # type: ignore[index]
    root_test["actions"] = [  # type: ignore[index]
        {"kind": "goto", "path": "/"},
        {"kind": "reload", "target": None},
    ]
    root_test["assertions"] = [  # type: ignore[index]
        {"kind": "url", "path": "/"},
        {
            "kind": "visible",
            "target": {"by": "role", "value": "heading", "name": "Home"},
        },
    ]
    missions = _acceptance(2)
    direct_test = missions["tests"][0]  # type: ignore[index]
    direct_actions: list[dict[str, object]] = [
        {"kind": "goto", "path": "/missions"}
    ]
    if missions_deep_linkable:
        direct_actions.append({"kind": "reload", "target": None})
    direct_test["actions"] = direct_actions  # type: ignore[index]
    direct_test["assertions"] = [  # type: ignore[index]
        {"kind": "url", "path": "/missions"},
        {
            "kind": "visible",
            "target": {"by": "role", "value": "heading", "name": "Missions"},
        },
    ]
    missions["criteria"].extend(  # type: ignore[union-attr]
        [
            {
                "id": "AC-2-link",
                "title": "Navigate to missions",
                "priority": "must",
                "given": "home is open",
                "when": "the Missions link is followed",
                "then": "missions is visible",
            },
            {
                "id": "AC-2-history",
                "title": "Use browser history",
                "priority": "must",
                "given": "home and missions were visited",
                "when": "browser history is traversed",
                "then": "both exact routes are observed",
            },
        ]
    )
    missions["tests"].extend(  # type: ignore[union-attr]
        [
            {
                "id": "test-2-link",
                "acceptanceId": "AC-2-link",
                "title": "navigates to missions on mobile",
                "actions": [
                    *[
                        {"kind": "set_viewport", "width": width, "height": 844}
                        for width in viewport_widths
                    ],
                    {"kind": "goto", "path": "/"},
                    {
                        "kind": "click",
                        "target": {
                            "by": "role",
                            "value": "link",
                            "name": "Missions",
                        },
                    },
                ],
                "assertions": [
                    {"kind": "url", "path": "/missions"},
                    {
                        "kind": "visible",
                        "target": {
                            "by": "role",
                            "value": "heading",
                            "name": "Missions",
                        },
                    },
                ],
            },
            {
                "id": "test-2-history",
                "acceptanceId": "AC-2-history",
                "title": "observes back and forward URLs",
                "actions": [
                    {"kind": "goto", "path": "/"},
                    {"kind": "goto", "path": "/missions"},
                    {
                        "kind": "history_roundtrip",
                        "backPath": "/",
                        "forwardPath": "/missions",
                    },
                ],
                "assertions": [
                    {"kind": "url", "path": "/missions"},
                    {
                        "kind": "visible",
                        "target": {
                            "by": "role",
                            "value": "heading",
                            "name": "Missions",
                        },
                    },
                ],
            },
        ]
    )
    return parse_goal_graph_draft(
        {
            "schemaVersion": 3,
            "productOutcome": "Users navigate a mission workspace.",
            "routes": [
                {
                    "path": "/",
                    "title": "Home",
                    "owningGoalId": "G-1",
                    "deepLinkable": True,
                },
                {
                    "path": "/missions",
                    "title": "Missions",
                    "owningGoalId": "G-2",
                    "deepLinkable": missions_deep_linkable,
                },
            ],
            "goals": [
                {
                    "goalId": "G-1",
                    "title": "Home",
                    "productOutcome": "Users open home.",
                    "userVisible": True,
                    "dependsOn": [],
                    "acceptance": root,
                },
                {
                    "goalId": "G-2",
                    "title": "Missions",
                    "productOutcome": "Users navigate missions.",
                    "userVisible": True,
                    "dependsOn": ["G-1"],
                    "acceptance": missions,
                },
            ],
        }
    )


def _graph() -> GoalGraph:
    return materialize_goal_graph(
        parse_legacy_goal_graph_draft(
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
        parse_legacy_goal_graph_draft(
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


def _three_route_graph() -> GoalGraph:
    return materialize_goal_graph(
        parse_goal_graph_draft(
            {
                "schemaVersion": 3,
                "productOutcome": "Users complete three routed workflows.",
                "routes": [
                    {
                        "path": path,
                        "title": f"Route {index}",
                        "owningGoalId": f"G-{index}",
                        "deepLinkable": True,
                    }
                    for index, path in enumerate(("/", "/two", "/three"), start=1)
                ],
                "goals": [
                    {
                        "goalId": f"G-{index}",
                        "title": f"Goal {index}",
                        "productOutcome": f"Users complete workflow {index}.",
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


def test_v3_navigation_suite_tests_only_owned_route_then_final_complete_graph() -> None:
    graph = materialize_goal_graph(_routing_draft())
    first_claim = claim_active_goal(activate_next_goal(graph))

    focused = build_regression_suite(first_claim)
    assert focused.navigation_suite is not None
    assert focused.navigation_suite.mode == "focused"
    assert [route.path for route in focused.navigation_suite.routes] == ["/"]
    focused_compiled = compile_acceptance_suite(
        focused.contracts,
        navigation_suite=focused.navigation_suite,
    )
    assert list(focused_compiled.navigation_test_name_by_id.values()) == [
        "FOMO navigation direct load: Home"
    ]

    final_claim = claim_active_goal(
        activate_next_goal(verify_claimed_goal(first_claim))
    )
    final = build_regression_suite(final_claim)
    assert final.navigation_suite is not None
    assert final.navigation_suite.mode == "final_full"
    assert [route.path for route in final.navigation_suite.routes] == [
        "/",
        "/missions",
    ]
    final_compiled = compile_acceptance_suite(
        final.contracts,
        navigation_suite=final.navigation_suite,
    )
    final_names = set(final_compiled.navigation_test_name_by_id.values())
    assert final_names == {
        "FOMO navigation direct load: Home",
        "FOMO navigation direct load: Missions",
        "FOMO navigation: root links reach every route",
        "FOMO navigation: 390px shared navigation reaches every route",
        "FOMO navigation: browser back and forward preserve every route identity",
    }


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


def test_v3_early_full_navigation_uses_ready_subgraph_not_pending_route() -> None:
    first = claim_active_goal(activate_next_goal(_three_route_graph()))
    middle = claim_active_goal(activate_next_goal(verify_claimed_goal(first)))

    suite = build_regression_suite(
        middle,
        full_reason=RuntimeValidationReason.PROJECT_CONFIG_CHANGED,
    )

    assert suite.navigation_suite is not None
    assert suite.navigation_suite.mode == "ready_full"
    assert [route.path for route in suite.navigation_suite.routes] == ["/", "/two"]
    compiled = compile_acceptance_suite(
        suite.contracts,
        navigation_suite=suite.navigation_suite,
    )
    navigation_names = set(compiled.navigation_test_name_by_id.values())
    assert "FOMO navigation direct load: Route 3" not in navigation_names
    assert navigation_names == {
        "FOMO navigation direct load: Route 1",
        "FOMO navigation direct load: Route 2",
        "FOMO navigation: root links reach every route",
        "FOMO navigation: 390px shared navigation reaches every route",
        "FOMO navigation: browser back and forward preserve every route identity",
    }


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
    assert (
        '"navigationContract":{"schemaVersion":1,'
        '"navigationSuiteVersion":null,"navigationMode":"single_surface","routes":[]}'
    ) in build
    assert '"navigationContract":{"schemaVersion":1' in repair
    assert '"goalId":"G-2"' in build
    assert "next-app-feature-first@1.0.0 (standard)" in build
    assert '"advisoryOnly":true' in build
    assert "next-app-feature-first@1.0.0 (standard)" in repair
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
    for prompt in (build, repair):
        assert "delegate_subtasks" in prompt
        assert "genuinely independent codebase questions" in prompt
        assert "You remain the only writer and integrator" in prompt


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
        assert "FOMO frontend-only runtime contract" in prompt
        assert "Do not create backend services, API/route handlers" in prompt
        assert "high-fidelity frontend prototype backed by local data" in prompt
        assert "Preserve the requested product breadth across the plan" in prompt
        assert "If the user specifies a visual direction, follow it" in prompt
        assert "Do not force an Apple" in prompt
        assert "Make useful, reversible product-design decisions" in prompt
        assert "giant-heading-plus-a-few-cards" in prompt
        assert "as many or as few files and components" in prompt
        assert "verbatim JSON string" in prompt
        assert "Apple-inspired" not in prompt

    assert "delegate_subtasks" not in planning
    assert "delegate_subtasks" in building

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


def test_goal_planner_promotes_explicit_multi_page_requests_to_route_contracts() -> None:
    multi_route = goal_graph_planning_prompt(
        requirement=(
            "构建一个高难作品展示，至少 5 个真实路由，支持深链接、浏览器前进后退"
            "和移动端导航。"
        ),
        starter={"routes": ["/"]},
    )
    single_surface = goal_graph_planning_prompt(
        requirement="Build one focused calculator surface.",
        starter={"routes": ["/"]},
    )

    assert "authoritative routing contract v3" in multi_route
    assert "MULTI_ROUTE_REQUIRED" in multi_route
    assert "schemaVersion: 3" in multi_route
    assert '"envelopeVersion":1' in multi_route
    assert '"payloadJson"' in multi_route
    assert "complete `routes` manifest" in multi_route
    assert "Do not create criteria or tests merely to prove direct route loading" in multi_route
    assert "FOMO deterministically derives and versions" in multi_route

    assert "ROUTE_SHAPE_PLANNER_SELECTED" in single_surface
    assert "MULTI_ROUTE_REQUIRED" not in single_surface


def test_server_routing_validator_enforces_explicit_route_breadth() -> None:
    draft = parse_goal_graph_draft(
        {
            "schemaVersion": 3,
            "productOutcome": "Users complete one focused workflow.",
            "routes": [
                {
                    "path": "/",
                    "title": "Calculator",
                    "owningGoalId": "G-1",
                    "deepLinkable": True,
                }
            ],
            "goals": [
                {
                    "goalId": "G-1",
                    "title": "Calculator",
                    "productOutcome": "Users calculate a result.",
                    "userVisible": True,
                    "dependsOn": [],
                    "acceptance": {
                        "criteria": [
                            {
                                "id": "AC-1",
                                "title": "Calculator is directly available",
                                "priority": "must",
                                "given": "the product is available",
                                "when": "the route is opened and reloaded",
                                "then": "the calculator remains available",
                            }
                        ],
                        "tests": [
                            {
                                "id": "T-1",
                                "acceptanceId": "AC-1",
                                "title": "opens the calculator directly",
                                "actions": [
                                    {"kind": "goto", "path": "/"},
                                    {"kind": "reload", "target": None},
                                ],
                                "assertions": [
                                    {"kind": "url", "path": "/"},
                                    {
                                        "kind": "visible",
                                        "target": {
                                            "by": "role",
                                            "value": "heading",
                                            "name": "Calculator",
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                }
            ],
        }
    )
    showcase = "构建高难成果展示，至少 5 个真实路由，支持深链接和移动端导航。"

    assert requires_multi_route(showcase)
    assert required_route_count(showcase) == 5
    with pytest.raises(ValueError, match="expected at least 5 real routes"):
        validate_goal_graph_routing(showcase, draft)
    assert validate_goal_graph_routing("Build one focused calculator surface.", draft) is draft


def test_route_intent_classifier_handles_counts_paths_and_negation() -> None:
    assert required_route_count("Build at least seven real routes") == 7
    assert required_route_count("Build at least seven pages.") == 7
    assert required_route_count("至少12个真实页面") == 12
    assert required_route_count("构建至少 12 个真实路由") == 12
    assert explicit_route_paths("Routes: `/`, `/missions`, and `/reports`.") == (
        "/",
        "/missions",
        "/reports",
    )
    assert required_route_count("Routes: `/`, `/missions`, and `/reports`.") == 3
    assert not requires_multi_route("Do not build a showcase, one page only.")
    for single_page_requirement in (
        "No multi-page app; build one page only.",
        "No deep links; this is a single page.",
        "No navigation menu, make one page.",
        "不做多页面，只做单页面。",
    ):
        assert not requires_multi_route(single_page_requirement)
        assert required_route_count(single_page_requirement) == 1
    assert required_route_count(
        "Build a dashboard showing 50 pages of paginated records."
    ) == 1
    assert explicit_route_paths(
        "Only modify /workspace and keep /tmp untouched. Use API endpoint /api/items."
    ) == ()
    assert explicit_route_paths("Create routes /, /missions, and /settings.") == (
        "/",
        "/missions",
        "/settings",
    )
    assert requires_multi_route("构建无需后端的高难成果展示")


def test_route_intent_validator_leaves_mechanical_navigation_to_server_suite() -> None:
    requirement = (
        "Build routes `/` and `/missions` with deep links, mobile navigation, "
        "and browser back and forward."
    )
    valid = _routing_draft()

    assert validate_goal_graph_routing(requirement, valid) is valid
    _activated, plan = plan_goal_execution(
        materialize_goal_graph(valid),
        graph_revision=4,
    )
    build = goal_build_prompt(
        requirement=requirement,
        starter={"routes": ["/", "/missions"]},
        execution_plan=plan,
        advisory_self_check_command=_ADVISORY_SELF_CHECK_COMMAND,
    )
    assert '"navigationMode":"multi_route"' in build
    assert '"path":"/missions","title":"Missions","owningGoalId":"G-2"' in build

    with pytest.raises(ValueError, match="deepLinkable=true"):
        validate_goal_graph_routing(
            requirement,
            _routing_draft(missions_deep_linkable=False),
        )
    assert (
        validate_goal_graph_routing(
            requirement,
            _routing_draft(viewport_widths=(390, 1024)),
        ).schema_version
        == 3
    )

    without_history = valid.model_copy(deep=True)
    history_test = without_history.goals[1].acceptance.tests[-1]
    history_test.actions.pop()
    assert validate_goal_graph_routing(requirement, without_history) is without_history


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
