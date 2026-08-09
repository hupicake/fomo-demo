from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from fomo.direct_pi.contracts import AcceptanceTest
from fomo.direct_pi.goalgraph import (
    SERVER_QUALITY_BAR,
    GoalStatus,
    GraphStatus,
    InvalidStatusTransition,
    acceptance_persistence_key,
    acceptance_test_path,
    acceptance_test_paths,
    can_transition_goal_status,
    materialize_goal_graph,
    parse_goal_graph,
    parse_goal_graph_draft,
    scope_acceptance_contract,
    serialize_goal_graph,
    serialize_goal_graph_draft,
    transition_goal_status,
    transition_graph_status,
)


def _acceptance(index: int, *, criteria_count: int = 1) -> dict[str, object]:
    criteria: list[dict[str, object]] = []
    tests: list[dict[str, object]] = []
    for offset in range(criteria_count):
        acceptance_id = f"AC-{index}-{offset}"
        criteria.append(
            {
                "id": acceptance_id,
                "title": f"Observable outcome {index}-{offset}",
                "priority": "must",
                "given": "The product is open",
                "when": "The user completes the workflow",
                "then": "The expected result is visible",
            }
        )
        tests.append(
            {
                "id": f"test-{index}-{offset}",
                "acceptanceId": acceptance_id,
                "title": f"verifies outcome {index}-{offset}",
                "actions": [{"kind": "goto", "path": "/"}],
                "assertions": [{"kind": "url", "path": "/"}],
            }
        )
    return {"criteria": criteria, "tests": tests}


def _goal(
    index: int,
    *,
    depends_on: list[str] | None = None,
    criteria_count: int = 1,
) -> dict[str, object]:
    return {
        "goalId": f"G-{index}",
        "title": f"Goal {index}",
        "productOutcome": f"Users can complete outcome {index}",
        "userVisible": True,
        "dependsOn": depends_on or [],
        "acceptance": _acceptance(index, criteria_count=criteria_count),
    }


def _graph() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "productOutcome": "Users can manage their work from one reliable product.",
        "goals": [_goal(1), _goal(2, depends_on=["G-1"])],
    }


def test_graph_accepts_topological_goals_and_has_server_quality_bar() -> None:
    graph = materialize_goal_graph(parse_goal_graph_draft(_graph()))

    assert graph.schema_version == 1
    assert graph.quality_bar == SERVER_QUALITY_BAR
    assert [goal.goal_id for goal in graph.goals] == ["G-1", "G-2"]
    assert graph.status is GraphStatus.ACTIVE
    assert all(goal.status is GoalStatus.PENDING for goal in graph.goals)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.update(schemaVersion=2),
            "schemaVersion",
        ),
        (
            lambda value: value.update(productOutcome=""),
            "at least 1 character",
        ),
        (
            lambda value: value.update(unknown=True),
            "Extra inputs are not permitted",
        ),
        (
            lambda value: value["goals"][0].update(userVisible="true"),  # type: ignore[index,union-attr]
            "valid boolean",
        ),
        (
            lambda value: value["goals"][0].update(goalId="unsafe/id"),  # type: ignore[index,union-attr]
            "string_pattern_mismatch",
        ),
    ],
)
def test_graph_fails_closed_for_invalid_model_input(mutate: object, message: str) -> None:
    value = _graph()
    mutate(value)  # type: ignore[operator]
    with pytest.raises(ValidationError, match=message):
        parse_goal_graph_draft(value)


def test_planner_draft_rejects_server_owned_policy_and_status() -> None:
    value = _graph()
    value["qualityBar"] = SERVER_QUALITY_BAR.model_dump(mode="json", by_alias=True)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_goal_graph_draft(value)

    value = _graph()
    value["status"] = "verified"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_goal_graph_draft(value)

    value = _graph()
    value["goals"][0]["status"] = "verified"  # type: ignore[index]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_goal_graph_draft(value)


def test_materialize_injects_fixed_initial_policy_and_status() -> None:
    graph = materialize_goal_graph(parse_goal_graph_draft(_graph()))

    assert graph.quality_bar == SERVER_QUALITY_BAR
    assert graph.status is GraphStatus.ACTIVE
    assert {goal.status for goal in graph.goals} == {GoalStatus.PENDING}


def test_graph_rejects_duplicate_and_non_topological_dependencies() -> None:
    value = _graph()
    value["goals"][1]["dependsOn"] = ["G-1", "G-1"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="dependencies must be unique"):
        parse_goal_graph_draft(value)

    value = _graph()
    value["goals"][0]["dependsOn"] = ["G-2"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="must reference earlier goals"):
        parse_goal_graph_draft(value)

    value = _graph()
    value["goals"][1]["goalId"] = "G-1"  # type: ignore[index]
    value["goals"][1]["dependsOn"] = []  # type: ignore[index]
    with pytest.raises(ValidationError, match="goal ids must be unique"):
        parse_goal_graph_draft(value)


def test_graph_enforces_goal_and_total_acceptance_bounds() -> None:
    value = _graph()
    value["goals"][0]["acceptance"] = _acceptance(1, criteria_count=8)  # type: ignore[index]
    parsed = parse_goal_graph_draft(value)
    assert len(parsed.goals[0].acceptance.criteria) == 8

    value["goals"][0]["acceptance"] = _acceptance(1, criteria_count=9)  # type: ignore[index]
    with pytest.raises(ValidationError, match="at most 8 items"):
        parse_goal_graph_draft(value)

    value = {
        "schemaVersion": 1,
        "productOutcome": "A complete user-visible product.",
        "goals": [
            _goal(1, criteria_count=7),
            _goal(2, depends_on=["G-1"], criteria_count=6),
        ],
    }
    with pytest.raises(ValidationError, match="at most 12 acceptance criteria"):
        parse_goal_graph_draft(value)


def test_acceptance_scope_has_durable_keys_and_isolated_test_paths() -> None:
    goal = materialize_goal_graph(parse_goal_graph_draft(_graph())).goals[0]
    scoped = scope_acceptance_contract(goal)

    assert acceptance_persistence_key("G-1", "AC-1-0") == "G-1:AC-1-0"
    assert acceptance_test_path("G-1", "test-1-0") == (
        "tests/fomo-acceptance/G-1/test-1-0.smoke.spec.ts"
    )
    assert scoped.contract is goal.acceptance
    assert scoped.acceptance_key_by_id == {"AC-1-0": "G-1:AC-1-0"}
    assert scoped.test_path_by_test_id == {
        "test-1-0": "tests/fomo-acceptance/G-1/test-1-0.smoke.spec.ts"
    }

    with pytest.raises(ValueError, match="safe identifier"):
        acceptance_test_path("../escape", "test")


def test_test_path_mapping_does_not_collapse_tests_for_the_same_acceptance() -> None:
    base = _acceptance(1)
    test_value = base["tests"][0]  # type: ignore[index]
    first = AcceptanceTest.model_validate(test_value)
    second = AcceptanceTest.model_validate({**test_value, "id": "test-1-secondary"})  # type: ignore[arg-type]

    assert acceptance_test_paths("G-1", [first, second]) == {
        "test-1-0": "tests/fomo-acceptance/G-1/test-1-0.smoke.spec.ts",
        "test-1-secondary": (
            "tests/fomo-acceptance/G-1/test-1-secondary.smoke.spec.ts"
        ),
    }


def test_status_transitions_preserve_claim_vs_verified_and_terminal_states() -> None:
    assert transition_graph_status(GraphStatus.ACTIVE, GraphStatus.VERIFIED) is GraphStatus.VERIFIED
    with pytest.raises(InvalidStatusTransition):
        transition_graph_status(GraphStatus.VERIFIED, GraphStatus.ACTIVE)

    assert transition_goal_status(GoalStatus.PENDING, GoalStatus.ACTIVE) is GoalStatus.ACTIVE
    assert transition_goal_status(GoalStatus.ACTIVE, GoalStatus.CLAIMED) is GoalStatus.CLAIMED
    assert transition_goal_status(GoalStatus.CLAIMED, GoalStatus.ACTIVE) is GoalStatus.ACTIVE
    assert transition_goal_status(GoalStatus.CLAIMED, GoalStatus.VERIFIED) is GoalStatus.VERIFIED
    assert can_transition_goal_status(GoalStatus.ACTIVE, GoalStatus.FAILED)
    assert can_transition_goal_status(GoalStatus.PENDING, GoalStatus.SUPERSEDED)
    with pytest.raises(InvalidStatusTransition):
        transition_goal_status(GoalStatus.VERIFIED, GoalStatus.ACTIVE)


def test_parse_and_serialization_are_fail_closed_and_deterministic() -> None:
    draft = parse_goal_graph_draft(json.dumps(_graph()))
    serialized_draft = serialize_goal_graph_draft(draft)
    assert serialize_goal_graph_draft(parse_goal_graph_draft(serialized_draft)) == serialized_draft
    assert "qualityBar" not in serialized_draft
    assert '"status"' not in serialized_draft

    serialized = serialize_goal_graph(materialize_goal_graph(draft))

    assert serialize_goal_graph(parse_goal_graph(serialized)) == serialized
    assert serialized.startswith('{"goals":')
    assert '"qualityBar":{"gates":["deps","typecheck","build"' in serialized

    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_goal_graph_draft('{"schemaVersion":1,"schemaVersion":1}')
    with pytest.raises(ValueError, match="JSON object"):
        parse_goal_graph_draft("[]")
