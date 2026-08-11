from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from fomo.direct_pi.contracts import AcceptanceTest
from fomo.direct_pi.goalgraph import (
    NAVIGATION_SUITE_VERSION,
    SERVER_QUALITY_BAR,
    GoalGraphDraft,
    GoalGraphPlanningEnvelope,
    GoalStatus,
    GraphStatus,
    InvalidStatusTransition,
    NavigationMode,
    acceptance_persistence_key,
    acceptance_test_path,
    acceptance_test_paths,
    assert_goal_graph_executable,
    can_transition_goal_status,
    derive_navigation_verification_suite,
    materialize_goal_graph,
    parse_goal_graph,
    parse_goal_graph_draft,
    parse_goal_graph_planning_envelope,
    parse_legacy_goal_graph_draft,
    parse_legacy_route_goal_graph_draft,
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


def _route_acceptance(
    index: int,
    path: str,
    title: str,
    *,
    link_from: str | None = None,
    deep_linkable: bool = True,
) -> dict[str, object]:
    direct_id = f"AC-{index}-direct"
    criteria: list[dict[str, object]] = [
        {
            "id": direct_id,
            "title": f"Open {title} directly",
            "priority": "must",
            "given": "The product is available",
            "when": "The route is opened directly",
            "then": "Its exact heading and URL are visible",
        }
    ]
    direct_actions: list[dict[str, object]] = [{"kind": "goto", "path": path}]
    if deep_linkable:
        direct_actions.append({"kind": "reload", "target": None})
    tests: list[dict[str, object]] = [
        {
            "id": f"test-{index}-direct",
            "acceptanceId": direct_id,
            "title": f"opens {title} directly",
            "actions": direct_actions,
            "assertions": [
                {"kind": "url", "path": path},
                {
                    "kind": "visible",
                    "target": {"by": "role", "value": "heading", "name": title},
                },
            ],
        }
    ]
    if link_from is not None:
        link_id = f"AC-{index}-link"
        criteria.append(
            {
                "id": link_id,
                "title": f"Navigate to {title}",
                "priority": "must",
                "given": "A related route is open",
                "when": f"The {title} link is followed",
                "then": "The target route identity is visible",
            }
        )
        tests.append(
            {
                "id": f"test-{index}-link",
                "acceptanceId": link_id,
                "title": f"navigates to {title}",
                "actions": [
                    {"kind": "goto", "path": link_from},
                    {
                        "kind": "click",
                        "target": {
                            "by": "role",
                            "value": "link",
                            "name": title,
                        },
                    },
                ],
                "assertions": [
                    {"kind": "url", "path": path},
                    {
                        "kind": "visible",
                        "target": {
                            "by": "role",
                            "value": "heading",
                            "name": title,
                        },
                    },
                ],
            }
        )
    return {"criteria": criteria, "tests": tests}


def _multi_route_graph() -> dict[str, object]:
    first = _goal(1)
    second = _goal(2, depends_on=["G-1"])
    return {
        "schemaVersion": 3,
        "productOutcome": "Users navigate a durable multi-route mission workspace.",
        "routes": [
            {
                "path": "/",
                "title": "Mission Control",
                "owningGoalId": "G-1",
                "deepLinkable": True,
            },
            {
                "path": "/missions",
                "title": "Missions",
                "owningGoalId": "G-2",
                "deepLinkable": True,
            },
        ],
        "goals": [first, second],
    }


def _legacy_v2_graph() -> dict[str, object]:
    value = _multi_route_graph()
    value["schemaVersion"] = 2
    value["goals"][0]["acceptance"] = _route_acceptance(  # type: ignore[index]
        1, "/", "Mission Control"
    )
    value["goals"][1]["acceptance"] = _route_acceptance(  # type: ignore[index]
        2, "/missions", "Missions", link_from="/"
    )
    return value


def test_graph_accepts_topological_goals_and_has_server_quality_bar() -> None:
    graph = materialize_goal_graph(parse_legacy_goal_graph_draft(_graph()))

    assert graph.schema_version == 1
    assert graph.quality_bar == SERVER_QUALITY_BAR
    assert [goal.goal_id for goal in graph.goals] == ["G-1", "G-2"]
    assert graph.status is GraphStatus.ACTIVE
    assert all(goal.status is GoalStatus.PENDING for goal in graph.goals)


def test_current_planner_transport_is_shallow_and_domain_is_v3_only() -> None:
    schema = GoalGraphDraft.model_json_schema(by_alias=True)
    envelope = GoalGraphPlanningEnvelope.model_json_schema(by_alias=True)

    assert schema["properties"]["schemaVersion"]["const"] == 3
    assert "routes" in schema["required"]
    assert envelope["required"] == ["envelopeVersion", "payloadJson"]
    assert set(envelope["properties"]) == {"envelopeVersion", "payloadJson"}
    route_path = schema["$defs"]["NavigationRoute"]["properties"]["path"]
    assert route_path["maxLength"] == 200
    assert route_path["pattern"].startswith("^/$")
    with pytest.raises(ValidationError, match="schemaVersion"):
        parse_goal_graph_draft(_graph())

    legacy = parse_legacy_goal_graph_draft(_graph())
    assert legacy.schema_version == 1


def test_planning_envelope_is_required_bounded_and_strictly_json() -> None:
    payload_json = json.dumps(
        _multi_route_graph(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    envelope = parse_goal_graph_planning_envelope(
        json.dumps(
            {"envelopeVersion": 1, "payloadJson": payload_json},
            ensure_ascii=False,
        )
    )
    assert envelope.payload_json == payload_json

    invalid_payloads = (
        json.dumps({"payloadJson": payload_json}),
        json.dumps({"envelopeVersion": 1, "payloadJson": {}}),
        json.dumps(
            {"envelopeVersion": 1, "payloadJson": payload_json, "extra": True}
        ),
        '{"envelopeVersion":1,"envelopeVersion":1,"payloadJson":"{}"}',
        '{"envelopeVersion":NaN,"payloadJson":"{}"}',
        f"```json\n{json.dumps({'envelopeVersion': 1, 'payloadJson': payload_json})}\n```",
        json.dumps({"envelopeVersion": 1, "payloadJson": "界" * 32_001}),
    )
    for invalid in invalid_payloads:
        with pytest.raises((ValidationError, ValueError)):
            parse_goal_graph_planning_envelope(invalid)


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
    value = _multi_route_graph()
    mutate(value)  # type: ignore[operator]
    with pytest.raises(ValidationError, match=message):
        parse_goal_graph_draft(value)


def test_planner_draft_rejects_server_owned_policy_and_status() -> None:
    value = _multi_route_graph()
    value["qualityBar"] = SERVER_QUALITY_BAR.model_dump(mode="json", by_alias=True)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_goal_graph_draft(value)

    value = _multi_route_graph()
    value["status"] = "verified"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_goal_graph_draft(value)

    value = _multi_route_graph()
    value["goals"][0]["status"] = "verified"  # type: ignore[index]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_goal_graph_draft(value)


def test_materialize_injects_fixed_initial_policy_and_status() -> None:
    graph = materialize_goal_graph(parse_goal_graph_draft(_multi_route_graph()))

    assert graph.quality_bar == SERVER_QUALITY_BAR
    assert graph.status is GraphStatus.ACTIVE
    assert {goal.status for goal in graph.goals} == {GoalStatus.PENDING}


def test_v3_materializes_a_versioned_server_owned_multi_route_contract() -> None:
    draft = parse_goal_graph_draft(_multi_route_graph())
    graph = materialize_goal_graph(draft)

    assert graph.navigation_mode is NavigationMode.MULTI_ROUTE
    assert graph.navigation_suite_version == NAVIGATION_SUITE_VERSION
    assert [route.path for route in graph.routes] == ["/", "/missions"]
    assert graph.routes[1].owning_goal_id == "G-2"
    assert graph.routes[1].deep_linkable is True

    serialized_draft = serialize_goal_graph_draft(draft)
    assert '"routes":[{"deepLinkable":true,"owningGoalId":"G-1"' in serialized_draft
    assert "navigationMode" not in serialized_draft
    serialized_graph = serialize_goal_graph(graph)
    assert '"navigationMode":"multi_route"' in serialized_graph
    assert serialize_goal_graph(parse_goal_graph(serialized_graph)) == serialized_graph


def test_legacy_v1_graph_round_trips_without_new_hash_fields() -> None:
    draft = parse_legacy_goal_graph_draft(_graph())
    graph = materialize_goal_graph(draft)

    assert graph.navigation_mode is NavigationMode.SINGLE_SURFACE
    assert graph.routes == []
    assert '"routes"' not in serialize_goal_graph_draft(draft)
    assert '"navigationMode"' not in serialize_goal_graph(graph)


def test_legacy_v1_preserves_old_path_hash_but_blocks_protocol_relative_execution() -> None:
    value = _graph()
    for goal in value["goals"]:  # type: ignore[index]
        test = goal["acceptance"]["tests"][0]  # type: ignore[index]
        test["actions"][0]["path"] = "/legacy/"  # type: ignore[index]
        test["assertions"][0]["path"] = "/legacy/"  # type: ignore[index]
    draft = parse_legacy_goal_graph_draft(value)
    canonical = serialize_goal_graph_draft(draft)

    assert hashlib.sha256(canonical.encode()).hexdigest() == (
        "e6dfdb27861dfdfe7d53e0b754020d7abb689ffb9fa6fbbdd57b9d5a60065339"
    )
    assert_goal_graph_executable(materialize_goal_graph(draft))

    value["goals"][0]["acceptance"]["tests"][0]["actions"][0]["path"] = "//evil"  # type: ignore[index]
    unsafe = materialize_goal_graph(parse_legacy_goal_graph_draft(value))
    with pytest.raises(ValueError, match="unsafe protocol-relative goto"):
        assert_goal_graph_executable(unsafe)


def test_v3_rejects_planner_owned_navigation_policy_and_invalid_route_manifests() -> None:
    value = _multi_route_graph()
    value["navigationMode"] = "multi_route"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_goal_graph_draft(value)

    value = _multi_route_graph()
    value["navigationSuiteVersion"] = 1
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_goal_graph_draft(value)

    value = _multi_route_graph()
    value["routes"][1]["path"] = "/missions?tab=all"  # type: ignore[index]
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        parse_goal_graph_draft(value)

    value = _multi_route_graph()
    value["routes"][1]["path"] = "//evil"  # type: ignore[index]
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        parse_goal_graph_draft(value)

    value = _multi_route_graph()
    value["routes"][1]["path"] = "/"  # type: ignore[index]
    with pytest.raises(ValidationError, match="paths must be unique"):
        parse_goal_graph_draft(value)

    value = _multi_route_graph()
    value["routes"][1]["owningGoalId"] = "G-missing"  # type: ignore[index]
    with pytest.raises(ValidationError, match="unknown owning goals"):
        parse_goal_graph_draft(value)

    value = _multi_route_graph()
    value["routes"][0]["path"] = "/home"  # type: ignore[index]
    with pytest.raises(ValidationError, match="must include root path"):
        parse_goal_graph_draft(value)

    value = _multi_route_graph()
    value["routes"][1]["title"] = "mission control"  # type: ignore[index]
    with pytest.raises(ValidationError, match="titles must be unique ignoring case"):
        parse_goal_graph_draft(value)


def test_v3_does_not_require_planner_navigation_samples_but_v2_keeps_old_meaning() -> None:
    assert parse_goal_graph_draft(_multi_route_graph()).schema_version == 3

    legacy = _legacy_v2_graph()
    legacy["goals"][1]["acceptance"]["tests"] = [  # type: ignore[index]
        legacy["goals"][1]["acceptance"]["tests"][0]  # type: ignore[index]
    ]
    legacy["goals"][1]["acceptance"]["criteria"] = [  # type: ignore[index]
        legacy["goals"][1]["acceptance"]["criteria"][0]  # type: ignore[index]
    ]
    with pytest.raises(ValidationError, match="separate link test"):
        parse_legacy_route_goal_graph_draft(legacy)


def test_v3_focused_navigation_allows_a_business_goal_without_an_owned_route() -> None:
    value = _multi_route_graph()
    missions = value["goals"][1]  # type: ignore[index]
    missions["goalId"] = "G-3"  # type: ignore[index]
    missions["dependsOn"] = ["G-2"]  # type: ignore[index]
    value["routes"][1]["owningGoalId"] = "G-3"  # type: ignore[index]
    value["goals"].insert(  # type: ignore[union-attr]
        1,
        {
            "goalId": "G-2",
            "title": "Shared business rules",
            "productOutcome": "Users retain shared mission preferences.",
            "userVisible": True,
            "dependsOn": ["G-1"],
            "acceptance": _acceptance(9),
        },
    )
    graph = materialize_goal_graph(parse_goal_graph_draft(value))

    assert (
        derive_navigation_verification_suite(
            graph,
            goal_ids=("G-2",),
            mode="focused",
        )
        is None
    )


def test_route_semantic_errors_are_aggregated_for_one_correction_turn() -> None:
    value = _multi_route_graph()
    value["routes"][0]["path"] = "/home"  # type: ignore[index]
    value["routes"][1]["title"] = "mission control"  # type: ignore[index]
    value["goals"][1]["acceptance"]["tests"][0]["actions"] = [  # type: ignore[index]
        {"kind": "goto", "path": "/future"}
    ]
    value["goals"][1]["acceptance"]["tests"][0]["assertions"] = [  # type: ignore[index]
        {"kind": "url", "path": "/future"}
    ]

    with pytest.raises(ValidationError) as captured:
        parse_goal_graph_draft(value)

    message = str(captured.value)
    assert "must include root path" in message
    assert "titles must be unique ignoring case" in message
    assert "references undeclared route /future" in message

def test_persisted_v3_graph_rejects_forged_server_navigation_policy() -> None:
    graph = materialize_goal_graph(parse_goal_graph_draft(_multi_route_graph()))
    payload = graph.model_dump(mode="json", by_alias=True)
    payload["navigationMode"] = "single_surface"

    with pytest.raises(ValidationError, match="derived and fixed by the server"):
        parse_goal_graph(payload)

    payload = graph.model_dump(mode="json", by_alias=True)
    payload["navigationSuiteVersion"] = None
    with pytest.raises(ValidationError, match="fixed by the server"):
        parse_goal_graph(payload)


def test_graph_rejects_duplicate_and_non_topological_dependencies() -> None:
    value = _graph()
    value["goals"][1]["dependsOn"] = ["G-1", "G-1"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="dependencies must be unique"):
        parse_legacy_goal_graph_draft(value)

    value = _graph()
    value["goals"][0]["dependsOn"] = ["G-2"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="must reference earlier goals"):
        parse_legacy_goal_graph_draft(value)

    value = _graph()
    value["goals"][1]["goalId"] = "G-1"  # type: ignore[index]
    value["goals"][1]["dependsOn"] = []  # type: ignore[index]
    with pytest.raises(ValidationError, match="goal ids must be unique"):
        parse_legacy_goal_graph_draft(value)


def test_graph_size_follows_product_complexity_without_count_caps() -> None:
    value = _graph()
    value["goals"][0]["acceptance"] = _acceptance(1, criteria_count=8)  # type: ignore[index]
    parsed = parse_legacy_goal_graph_draft(value)
    assert len(parsed.goals[0].acceptance.criteria) == 8

    value["goals"][0]["acceptance"] = _acceptance(1, criteria_count=9)  # type: ignore[index]
    parsed = parse_legacy_goal_graph_draft(value)
    assert len(parsed.goals[0].acceptance.criteria) == 9

    value = {
        "schemaVersion": 1,
        "productOutcome": "A complete user-visible product.",
        "goals": [
            _goal(1, criteria_count=7),
            _goal(2, depends_on=["G-1"], criteria_count=6),
        ],
    }
    parsed = parse_legacy_goal_graph_draft(value)
    assert sum(len(goal.acceptance.criteria) for goal in parsed.goals) == 13


def test_acceptance_scope_has_durable_keys_and_isolated_test_paths() -> None:
    goal = materialize_goal_graph(parse_legacy_goal_graph_draft(_graph())).goals[0]
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
    draft = parse_legacy_goal_graph_draft(json.dumps(_graph()))
    serialized_draft = serialize_goal_graph_draft(draft)
    assert (
        serialize_goal_graph_draft(parse_legacy_goal_graph_draft(serialized_draft))
        == serialized_draft
    )
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
