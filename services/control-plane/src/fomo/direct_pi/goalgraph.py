"""Strict, engine-neutral GoalGraph domain contracts.

The graph contains frozen product outcomes, dependency ordering, and the
existing FOMO-owned acceptance DSL. Execution and checkpoint policy belong to
the control plane, not to this schema. Product complexity determines graph
size; the schema does not impose arbitrary goal or acceptance-count ceilings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    field_validator,
    model_validator,
)

from fomo.schemas import SchemaModel

from .contracts import (
    AcceptanceContract,
    AcceptanceItem,
    AcceptanceTest,
    ClickAction,
    FillAction,
    GotoAction,
    Identifier,
    LocalPath,
    ReloadAction,
    SelectAction,
    UrlAssertion,
    ValueAssertion,
    VisibleAssertion,
)

LEGACY_SCHEMA_VERSION = 1
LEGACY_ROUTE_SCHEMA_VERSION = 2
SCHEMA_VERSION = 3
NAVIGATION_SUITE_VERSION = 1
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
PlanningPayload = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=96_000),
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


class NavigationMode(StrEnum):
    SINGLE_SURFACE = "single_surface"
    MULTI_ROUTE = "multi_route"


@dataclass(frozen=True, slots=True)
class NavigationVerificationSuite:
    """One deterministic slice of FOMO's versioned navigation policy."""

    version: int
    routes: tuple[NavigationRoute, ...]
    mode: Literal["focused", "ready_full", "final_full"]

    def __post_init__(self) -> None:
        if self.version != NAVIGATION_SUITE_VERSION:
            raise ValueError("navigation verification suite version is unsupported")
        if not self.routes:
            raise ValueError("navigation verification suite requires routes")
        if len({route.path for route in self.routes}) != len(self.routes):
            raise ValueError("navigation verification suite routes must be unique")

    @property
    def shared_navigation_gate(self) -> bool:
        return self.mode in {"ready_full", "final_full"}


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


class NavigationRoute(SchemaModel):
    """Planner-declared route data that becomes frozen server-owned policy.

    Paths describe concrete browser locations, not Next.js file patterns. Query
    strings, fragments, dynamic brackets and trailing-slash aliases are
    intentionally excluded so exact URL evidence has one canonical meaning.
    """

    path: LocalPath
    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
    ]
    owning_goal_id: Identifier
    deep_linkable: StrictBool


LegacyLocalPath = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=200,
        pattern=r"^/[A-Za-z0-9/_-]*$",
    ),
]


class LegacyGotoAction(GotoAction):
    """Exact v1 decoder shape; never admitted to current planner output."""

    path: LegacyLocalPath


class LegacyUrlAssertion(UrlAssertion):
    """Exact v1 URL assertion shape retained for historical hash checks."""

    path: LegacyLocalPath


LegacyAcceptanceAction = Annotated[
    LegacyGotoAction | ClickAction | FillAction | SelectAction | ReloadAction,
    Field(discriminator="kind"),
]
LegacyAcceptanceAssertion = Annotated[
    VisibleAssertion | ValueAssertion | LegacyUrlAssertion,
    Field(discriminator="kind"),
]


class LegacyAcceptanceTest(AcceptanceTest):
    actions: list[LegacyAcceptanceAction] = Field(min_length=1)
    assertions: list[LegacyAcceptanceAssertion] = Field(min_length=1)


class LegacyAcceptanceContract(AcceptanceContract):
    criteria: list[AcceptanceItem] = Field(min_length=1)
    tests: list[LegacyAcceptanceTest] = Field(min_length=1)


class GoalDraft(SchemaModel):
    """Planner-owned goal fields; lifecycle state is intentionally absent."""

    goal_id: Identifier
    title: GoalTitle
    product_outcome: GoalProductOutcome
    user_visible: StrictBool
    depends_on: list[Identifier] = Field(default_factory=list)
    acceptance: AcceptanceContract

    @model_validator(mode="after")
    def unique_dependencies(self) -> GoalDraft:
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("goal dependencies must be unique")
        if self.goal_id in self.depends_on:
            raise ValueError("a goal cannot depend on itself")
        return self


class Goal(GoalDraft):
    """Trusted persisted goal projection with server-managed lifecycle state."""

    status: GoalStatus


class LegacyGoalDraft(GoalDraft):
    """Historical goal decoder using the exact v1 acceptance path grammar."""

    acceptance: LegacyAcceptanceContract


class LegacyGoal(Goal):
    """Historical lifecycle projection; unsafe paths remain read-only."""

    acceptance: LegacyAcceptanceContract


def _validate_goal_topology(goals: Sequence[GoalDraft]) -> None:
    goal_ids = [goal.goal_id for goal in goals]
    if len(goal_ids) != len(set(goal_ids)):
        raise ValueError("goal ids must be unique")

    seen: set[str] = set()
    for goal in goals:
        unavailable = [
            dependency for dependency in goal.depends_on if dependency not in seen
        ]
        if unavailable:
            joined = ", ".join(unavailable)
            raise ValueError(
                f"goal {goal.goal_id} dependencies must reference earlier goals: {joined}"
            )
        seen.add(goal.goal_id)


def _validate_route_manifest(
    *,
    schema_version: int,
    routes: Sequence[NavigationRoute],
    goals: Sequence[GoalDraft],
) -> None:
    if schema_version == LEGACY_SCHEMA_VERSION:
        if routes:
            raise ValueError("legacy GoalGraph schema cannot declare routes")
        return

    violations: list[str] = []
    if not routes:
        raise ValueError(
            "route contract violations: routed GoalGraph requires a route manifest"
        )
    paths = [route.path for route in routes]
    if "/" not in paths:
        violations.append("the route manifest must include root path /")
    if len(paths) != len(set(paths)):
        violations.append("route paths must be unique")
    normalized_titles = [route.title.casefold() for route in routes]
    if len(normalized_titles) != len(set(normalized_titles)):
        violations.append("route titles must be unique ignoring case")

    goals_by_id = {goal.goal_id: goal for goal in goals}
    unknown_owners = sorted(
        {
            route.owning_goal_id
            for route in routes
            if route.owning_goal_id not in goals_by_id
        }
    )
    if unknown_owners:
        violations.append(
            "navigation routes reference unknown owning goals: "
            + ", ".join(unknown_owners)
        )

    if schema_version == SCHEMA_VERSION:
        root = next((route for route in routes if route.path == "/"), None)
        routes_by_path = {route.path: route for route in routes}
        for route in routes:
            owner = goals_by_id.get(route.owning_goal_id)
            if owner is None:
                continue
            if not owner.user_visible:
                violations.append(
                    f"route {route.path} owner {owner.goal_id} must be user-visible"
                )
            if root is not None and route.path != "/":
                allowed = {
                    owner.goal_id,
                    *_transitive_dependencies(owner.goal_id, goals_by_id),
                }
                if root.owning_goal_id not in allowed:
                    violations.append(
                        f"route {route.path} owner {owner.goal_id} must depend on root "
                        f"owner {root.owning_goal_id}"
                    )

        for goal in goals:
            allowed_route_owners = {
                goal.goal_id,
                *_transitive_dependencies(goal.goal_id, goals_by_id),
            }
            for test in goal.acceptance.tests:
                referenced_paths = {
                    action.path
                    for action in test.actions
                    if action.kind == "goto"
                }
                referenced_paths.update(
                    path
                    for action in test.actions
                    if action.kind == "history_roundtrip"
                    for path in (action.back_path, action.forward_path)
                )
                referenced_paths.update(
                    assertion.path
                    for assertion in test.assertions
                    if assertion.kind == "url"
                )
                for path in sorted(referenced_paths):
                    referenced = routes_by_path.get(path)
                    if referenced is None:
                        violations.append(
                            f"goal {goal.goal_id} test {test.id} references undeclared "
                            f"route {path}"
                        )
                    elif referenced.owning_goal_id not in allowed_route_owners:
                        violations.append(
                            f"goal {goal.goal_id} test {test.id} references future route "
                            f"{path}"
                        )

    if violations:
        raise ValueError(
            "route manifest violations:\n- " + "\n- ".join(violations)
        )


def _validate_legacy_v2_routing_contract(
    *,
    routes: Sequence[NavigationRoute],
    goals: Sequence[GoalDraft],
) -> None:
    """Validate the exact v2 planner-owned navigation evidence contract.

    New planning never enters this path. It exists solely so persisted v2
    revisions keep their original hash and meaning instead of being silently
    reinterpreted under the server-derived v3 navigation suite.
    """

    _validate_route_manifest(
        schema_version=LEGACY_ROUTE_SCHEMA_VERSION,
        routes=routes,
        goals=goals,
    )
    violations: list[str] = []
    goals_by_id = {goal.goal_id: goal for goal in goals}
    for route in routes:
        if route.owning_goal_id not in goals_by_id:
            continue
        owner = goals_by_id[route.owning_goal_id]
        if not _route_direct_evidence_tests(route, owner):
            suffix = "goto(path), reload" if route.deep_linkable else "goto(path)"
            violations.append(
                f"route {route.path} requires an independent direct test in owner "
                f"{route.owning_goal_id} ending with {suffix} and asserting exact URL "
                f"plus heading {route.title!r}"
            )
        if len(routes) >= 2 and route.path != "/" and not _route_link_evidence_tests(
            route,
            owner,
            routes=routes,
            goals_by_id=goals_by_id,
        ):
            violations.append(
                f"route {route.path} requires a separate link test in owner "
                f"{route.owning_goal_id} from an allowed declared source, ending with "
                f"role=link name={route.title!r} and asserting exact URL plus heading"
            )

    if violations:
        raise ValueError("route contract violations:\n- " + "\n- ".join(violations))


class LegacyGoalGraphDraft(SchemaModel):
    """Read-only schema for revisions created before route manifests existed."""

    schema_version: Literal[LEGACY_SCHEMA_VERSION] = LEGACY_SCHEMA_VERSION
    product_outcome: ProductOutcome
    goals: list[LegacyGoalDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_topological_graph(self) -> LegacyGoalGraphDraft:
        _validate_goal_topology(self.goals)
        return self


class LegacyRouteGoalGraphDraft(SchemaModel):
    """Read-only v2 graph whose planner supplied mechanical navigation tests."""

    schema_version: Literal[LEGACY_ROUTE_SCHEMA_VERSION] = LEGACY_ROUTE_SCHEMA_VERSION
    product_outcome: ProductOutcome
    routes: list[NavigationRoute] = Field(min_length=1)
    goals: list[GoalDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_legacy_route_graph(self) -> LegacyRouteGoalGraphDraft:
        _validate_goal_topology(self.goals)
        _validate_legacy_v2_routing_contract(routes=self.routes, goals=self.goals)
        return self


class GoalGraphDraft(SchemaModel):
    """Current planner payload: business acceptance plus a route manifest.

    Mechanical direct-load, shared-navigation, mobile and history checks are
    deliberately absent. FOMO derives those from the frozen manifest after
    admission, so the provider cannot weaken them and large products do not
    spend most of their planning budget repeating server policy.
    """

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    product_outcome: ProductOutcome
    routes: list[NavigationRoute] = Field(min_length=1)
    goals: list[GoalDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_graph_contract(self) -> GoalGraphDraft:
        _validate_goal_topology(self.goals)
        _validate_route_manifest(
            schema_version=self.schema_version,
            routes=self.routes,
            goals=self.goals,
        )
        return self


class GoalGraphPlanningEnvelope(SchemaModel):
    """Shallow provider transport, intentionally decoupled from the domain.

    Provider adapters only need to submit one bounded JSON string. The server
    remains the single parser for Pydantic shape, topology and semantic route
    validation across Pi, OpenCode and Codex.
    """

    envelope_version: Literal[1]
    payload_json: PlanningPayload

    @field_validator("payload_json")
    @classmethod
    def bounded_utf8_payload(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 96_000:
            raise ValueError("payloadJson exceeds the UTF-8 byte limit")
        return value


GoalGraphDraftLike = (
    GoalGraphDraft | LegacyRouteGoalGraphDraft | LegacyGoalGraphDraft
)


class GoalGraph(SchemaModel):
    """Trusted persisted graph projection with server-owned policy and state."""

    schema_version: Literal[
        LEGACY_SCHEMA_VERSION,
        LEGACY_ROUTE_SCHEMA_VERSION,
        SCHEMA_VERSION,
    ] = SCHEMA_VERSION
    product_outcome: ProductOutcome
    routes: list[NavigationRoute] = Field(default_factory=list)
    quality_bar: GoalGraphQualityBar
    navigation_mode: NavigationMode = NavigationMode.SINGLE_SURFACE
    navigation_suite_version: Literal[NAVIGATION_SUITE_VERSION] | None = None
    goals: list[Goal | LegacyGoal] = Field(min_length=1)
    status: GraphStatus

    @model_validator(mode="after")
    def fixed_server_contract(self) -> GoalGraph:
        if self.schema_version != LEGACY_SCHEMA_VERSION and any(
            isinstance(goal, LegacyGoal) for goal in self.goals
        ):
            raise ValueError("routed GoalGraph cannot contain legacy acceptance paths")
        _validate_goal_topology(self.goals)
        if self.schema_version == LEGACY_ROUTE_SCHEMA_VERSION:
            _validate_legacy_v2_routing_contract(
                routes=self.routes,
                goals=self.goals,
            )
        else:
            _validate_route_manifest(
                schema_version=self.schema_version,
                routes=self.routes,
                goals=self.goals,
            )
        if self.schema_version == SCHEMA_VERSION:
            if self.navigation_suite_version != NAVIGATION_SUITE_VERSION:
                raise ValueError("navigationSuiteVersion is fixed by the server")
        elif self.navigation_suite_version is not None:
            raise ValueError(
                "historical GoalGraph cannot declare a navigation suite version"
            )
        if self.quality_bar != SERVER_QUALITY_BAR:
            raise ValueError("qualityBar is fixed by the server and cannot be overridden")
        expected_navigation = _navigation_mode(self.schema_version, self.routes)
        if self.navigation_mode is not expected_navigation:
            raise ValueError("navigationMode is derived and fixed by the server")
        return self


def assert_goal_graph_executable(graph: GoalGraph) -> None:
    """Reject historical protocol-relative paths before acceptance compilation."""

    if graph.schema_version != LEGACY_SCHEMA_VERSION:
        return
    for goal in graph.goals:
        for test in goal.acceptance.tests:
            for action in test.actions:
                if action.kind == "goto" and action.path.startswith("//"):
                    raise ValueError(
                        "legacy GoalGraph contains an unsafe protocol-relative goto path"
                    )
            for assertion in test.assertions:
                if assertion.kind == "url" and assertion.path.startswith("//"):
                    raise ValueError(
                        "legacy GoalGraph contains an unsafe protocol-relative URL path"
                    )


def _asserts_route_identity(test: AcceptanceTest, route: NavigationRoute) -> bool:
    exact_url = any(
        assertion.kind == "url" and assertion.path == route.path
        for assertion in test.assertions
    )
    exact_heading = any(
        assertion.kind == "visible"
        and assertion.target.by == "role"
        and assertion.target.value == "heading"
        and assertion.target.name == route.title
        for assertion in test.assertions
    )
    return exact_url and exact_heading


def _route_direct_evidence_tests(
    route: NavigationRoute,
    owner: GoalDraft,
) -> tuple[AcceptanceTest, ...]:
    matches: list[AcceptanceTest] = []
    for test in owner.acceptance.tests:
        actions = test.actions
        if route.deep_linkable:
            has_terminal_direct_load = (
                len(actions) >= 2
                and actions[-2].kind == "goto"
                and actions[-2].path == route.path
                and actions[-1].kind == "reload"
            )
        else:
            has_terminal_direct_load = (
                bool(actions)
                and actions[-1].kind == "goto"
                and actions[-1].path == route.path
            )
        if has_terminal_direct_load and _asserts_route_identity(test, route):
            matches.append(test)
    return tuple(matches)


def _transitive_dependencies(
    goal_id: str,
    goals_by_id: Mapping[str, GoalDraft],
) -> frozenset[str]:
    discovered: set[str] = set()
    pending = list(goals_by_id[goal_id].depends_on)
    while pending:
        dependency = pending.pop()
        if dependency in discovered:
            continue
        discovered.add(dependency)
        ancestor = goals_by_id.get(dependency)
        if ancestor is not None:
            pending.extend(ancestor.depends_on)
    return frozenset(discovered)


def _route_link_evidence_tests(
    route: NavigationRoute,
    owner: GoalDraft,
    *,
    routes: Sequence[NavigationRoute],
    goals_by_id: Mapping[str, GoalDraft],
) -> tuple[AcceptanceTest, ...]:
    routes_by_path = {item.path: item for item in routes}
    allowed_source_owners = {
        route.owning_goal_id,
        *_transitive_dependencies(route.owning_goal_id, goals_by_id),
    }
    matches: list[AcceptanceTest] = []
    for test in owner.acceptance.tests:
        actions = test.actions
        if not actions or not _asserts_route_identity(test, route):
            continue
        link = actions[-1]
        if (
            link.kind != "click"
            or link.target.by != "role"
            or link.target.value != "link"
            or link.target.name != route.title
        ):
            continue
        source_index = next(
            (
                index
                for index in range(len(actions) - 2, -1, -1)
                if actions[index].kind == "goto"
            ),
            None,
        )
        if source_index is None:
            continue
        source_path = actions[source_index].path
        obscuring_navigation = any(
            action.kind in {"goto", "back", "forward", "history_roundtrip"}
            or (
                action.kind == "click"
                and action.target.by == "role"
                and action.target.value == "link"
            )
            for action in actions[source_index + 1 : -1]
        )
        if obscuring_navigation:
            continue
        source_route = routes_by_path.get(source_path or "")
        if (
            source_route is None
            or source_route.path == route.path
            or source_route.owning_goal_id not in allowed_source_owners
        ):
            continue
        matches.append(test)
    return tuple(matches)


def route_link_evidence_tests(
    graph: GoalGraphDraft | GoalGraph,
    target_path: str,
) -> tuple[AcceptanceTest, ...]:
    """Return trusted link tests for one non-root route."""

    route = next((item for item in graph.routes if item.path == target_path), None)
    if route is None:
        return ()
    goals_by_id = {goal.goal_id: goal for goal in graph.goals}
    owner = goals_by_id.get(route.owning_goal_id)
    if owner is None:
        return ()
    return _route_link_evidence_tests(
        route,
        owner,
        routes=graph.routes,
        goals_by_id=goals_by_id,
    )


def _navigation_mode(
    schema_version: int,
    routes: Sequence[NavigationRoute],
) -> NavigationMode:
    if schema_version == LEGACY_SCHEMA_VERSION or len(routes) < 2:
        return NavigationMode.SINGLE_SURFACE
    return NavigationMode.MULTI_ROUTE


def derive_navigation_verification_suite(
    graph: GoalGraph,
    *,
    goal_ids: Sequence[str],
    mode: Literal["focused", "ready_full", "final_full"],
) -> NavigationVerificationSuite | None:
    """Derive a current-schema suite without reinterpreting v1/v2 history."""

    if graph.schema_version != SCHEMA_VERSION:
        return None
    selected = frozenset(goal_ids)
    known = {goal.goal_id for goal in graph.goals}
    if not selected or not selected.issubset(known):
        raise ValueError("navigation suite goal scope is invalid")
    routes = tuple(
        route for route in graph.routes if route.owning_goal_id in selected
    )
    if not routes:
        return None
    if mode == "final_full" and (
        selected != known or len(routes) != len(graph.routes)
    ):
        raise ValueError("final navigation gate requires the complete GoalGraph")
    if mode == "ready_full" and "/" not in {route.path for route in routes}:
        raise ValueError("full ready navigation suite requires the root route")
    return NavigationVerificationSuite(
        version=graph.navigation_suite_version or 0,
        routes=routes,
        mode=mode,
    )


def navigation_test_ids(
    suite: NavigationVerificationSuite | None,
) -> tuple[str, ...]:
    """Return stable internal IDs shared by compilation and checkpoints."""

    if suite is None:
        return ()
    values = [
        f"direct-{hashlib.sha256(route.path.encode()).hexdigest()[:12]}"
        for route in suite.routes
    ]
    non_root = [route for route in suite.routes if route.path != "/"]
    if suite.shared_navigation_gate and non_root:
        values.extend(
            ("shared-navigation", "mobile-navigation-390", "history-roundtrip")
        )
    return tuple(values)


def navigation_evidence_key(version: int, test_id: str) -> str:
    if version != NAVIGATION_SUITE_VERSION:
        raise ValueError("navigation evidence suite version is unsupported")
    safe_id = _identifier(test_id, "navigation test id")
    return f"__fomo_navigation_v{version}:{safe_id}"


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
    """Parse new planner output; only current schema v3 is accepted."""

    return GoalGraphDraft.model_validate(_parse_json_object(payload, label="GoalGraphDraft"))


def parse_goal_graph_planning_envelope(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> GoalGraphPlanningEnvelope:
    """Parse the provider envelope with duplicate/non-finite rejection."""

    return GoalGraphPlanningEnvelope.model_validate(
        _parse_json_object(payload, label="GoalGraphPlanningEnvelope")
    )


def parse_legacy_goal_graph_draft(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> LegacyGoalGraphDraft:
    """Parse a historical v1 draft for compatibility reads, never planning."""

    return LegacyGoalGraphDraft.model_validate(
        _parse_json_object(payload, label="LegacyGoalGraphDraft")
    )


def parse_legacy_route_goal_graph_draft(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> LegacyRouteGoalGraphDraft:
    """Parse a historical v2 draft without applying the v3 suite policy."""

    return LegacyRouteGoalGraphDraft.model_validate(
        _parse_json_object(payload, label="LegacyRouteGoalGraphDraft")
    )


def parse_persisted_goal_graph_draft(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> GoalGraphDraftLike:
    """Parse a stored draft using its explicit schema-version discriminator."""

    value = _parse_json_object(payload, label="PersistedGoalGraphDraft")
    schema_version = value.get("schemaVersion", value.get("schema_version"))
    if schema_version is None:
        raise ValueError("persisted GoalGraph draft requires schemaVersion")
    if schema_version == LEGACY_SCHEMA_VERSION:
        return LegacyGoalGraphDraft.model_validate(value)
    if schema_version == LEGACY_ROUTE_SCHEMA_VERSION:
        return LegacyRouteGoalGraphDraft.model_validate(value)
    if schema_version == SCHEMA_VERSION:
        return GoalGraphDraft.model_validate(value)
    raise ValueError("persisted GoalGraph schemaVersion is unsupported")


def materialize_goal_graph(draft: GoalGraphDraftLike) -> GoalGraph:
    """Create the initial trusted projection with server-owned policy/state."""

    routes = (
        draft.routes
        if isinstance(draft, (GoalGraphDraft, LegacyRouteGoalGraphDraft))
        else []
    )
    goal_type = LegacyGoal if isinstance(draft, LegacyGoalGraphDraft) else Goal
    return GoalGraph(
        schema_version=draft.schema_version,
        product_outcome=draft.product_outcome,
        routes=routes,
        quality_bar=SERVER_QUALITY_BAR,
        navigation_mode=_navigation_mode(draft.schema_version, routes),
        navigation_suite_version=(
            NAVIGATION_SUITE_VERSION
            if isinstance(draft, GoalGraphDraft)
            else None
        ),
        goals=[
            goal_type.model_validate({**goal.model_dump(), "status": GoalStatus.PENDING})
            for goal in draft.goals
        ],
        status=GraphStatus.ACTIVE,
    )


def parse_persisted_goal_graph(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> GoalGraph:
    """Parse a trusted persisted projection, never untrusted planner output."""

    value = _parse_json_object(payload, label="GoalGraph")
    if "schemaVersion" not in value and "schema_version" not in value:
        raise ValueError("persisted GoalGraph requires schemaVersion")
    return GoalGraph.model_validate(value)


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
    draft: GoalGraphDraftLike | Mapping[str, Any],
) -> str:
    """Return deterministic canonical JSON for a current or historical draft."""

    validated = (
        draft
        if isinstance(
            draft,
            (GoalGraphDraft, LegacyRouteGoalGraphDraft, LegacyGoalGraphDraft),
        )
        else parse_persisted_goal_graph_draft(draft)
    )
    return _serialize_model(validated)


def _serialize_model(graph: GoalGraph | GoalGraphDraftLike) -> str:
    payload = graph.model_dump(mode="json", by_alias=True)
    # Schema v1 predates the routing contract. Omitting the new default fields
    # preserves historical revision hashes and canonical persisted payloads.
    if graph.schema_version in {
        LEGACY_SCHEMA_VERSION,
        LEGACY_ROUTE_SCHEMA_VERSION,
    }:
        payload.pop("navigationSuiteVersion", None)
    if graph.schema_version == LEGACY_SCHEMA_VERSION:
        payload.pop("routes", None)
        payload.pop("navigationMode", None)
    return json.dumps(
        payload,
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
    "LEGACY_SCHEMA_VERSION",
    "LEGACY_ROUTE_SCHEMA_VERSION",
    "NAVIGATION_SUITE_VERSION",
    "SCHEMA_VERSION",
    "SERVER_QUALITY_BAR",
    "Goal",
    "GoalDraft",
    "GoalGraph",
    "GoalGraphDraft",
    "GoalGraphDraftLike",
    "GoalGraphPlanningEnvelope",
    "GoalGraphQualityBar",
    "GoalNode",
    "GoalStatus",
    "GraphStatus",
    "InvalidStatusTransition",
    "LegacyGoal",
    "LegacyGoalDraft",
    "LegacyGoalGraphDraft",
    "LegacyRouteGoalGraphDraft",
    "NavigationMode",
    "NavigationRoute",
    "NavigationVerificationSuite",
    "ScopedAcceptanceContract",
    "acceptance_persistence_key",
    "acceptance_test_path",
    "acceptance_test_paths",
    "can_transition_goal_status",
    "can_transition_graph_status",
    "assert_goal_graph_executable",
    "derive_navigation_verification_suite",
    "materialize_goal_graph",
    "navigation_evidence_key",
    "navigation_test_ids",
    "parse_goal_graph",
    "parse_goal_graph_draft",
    "parse_goal_graph_planning_envelope",
    "parse_legacy_goal_graph_draft",
    "parse_legacy_route_goal_graph_draft",
    "parse_persisted_goal_graph",
    "parse_persisted_goal_graph_draft",
    "route_link_evidence_tests",
    "scope_acceptance_contract",
    "scoped_acceptance_key",
    "scoped_acceptance_test_path",
    "serialize_goal_graph",
    "serialize_goal_graph_draft",
    "transition_goal_status",
    "transition_graph_status",
]
