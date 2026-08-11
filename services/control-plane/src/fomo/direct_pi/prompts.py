"""Prompts for one persistent Direct Pi product-development session.

The BuildPlan and GoalGraph organize delivery; they do not cap product ambition,
architecture, or file topology. Pi keeps its official builtin tools with full
``/workspace`` permission. The source request and user-visible outcomes remain
authoritative.  The generation sandbox receives a protected current-goal
advisory mirror for early self-checks; only the independently recompiled suite
in the clean verification sandbox counts as release evidence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from fomo.text_safety import bounded_diagnostic_text

from .architecture_profile import (
    ArchitectureProfile,
    derive_product_architecture_profile,
)
from .contracts import LOCAL_PATH_PATTERN
from .goal_manager import GoalExecutionPlan
from .goalgraph import GoalGraphDraft, route_link_evidence_tests

GOAL_GRAPH_PLANNING_POLICY = "frontend-ui-v6"
PRODUCT_REQUIREMENTS_POLICY = "frontend-product-v3"
PRODUCT_DESIGN_POLICY = "frontend-design-v4"
_MAX_REPAIR_DIAGNOSTIC_JSON_CHARACTERS = 12_000


_FRONTEND_ONLY_BRIEF = """FOMO frontend-only runtime contract:
- Build only a polished frontend web application inside the existing Next.js workspace. Product behavior must run in the browser; concentrate effort on UI quality, information architecture, responsive interaction, accessibility, and convincing product states.
- Do not create backend services, API/route handlers, Server Actions, databases, ORM models, queues, cron jobs, server-side authentication, email delivery, external service integrations, or infrastructure. Do not add server frameworks or read/write environment files.
- Implement requested data and workflows with typed browser state plus deterministic local fixtures. Use versioned localStorage only for non-sensitive product data when persistence across refresh is required; use in-memory state when persistence is not required. Never store passwords, access tokens, API keys, or other secrets. Simulate loading, success, error, permissions, and other product states locally and visibly.
- If the source request mentions a backend-dependent capability, preserve its user-facing journey as a high-fidelity frontend prototype backed by local data rather than inventing a server. Never claim that a real remote side effect occurred.
- Implement every top-level destination declared by the frozen route contract as a real Next.js App Router location. Tabs may organize subordinate content inside one route, but must never impersonate a required route through component state, a URL hash, or query-only state; business logic and mutable product data must remain client-side. Keep the result self-contained and runnable without credentials or network APIs."""


_HARD_MULTI_ROUTE_INTENT = re.compile(
    r"(?:multi[ -]?(?:page|route)|deep[ -]?links?|"
    r"browser[^,.;，。；\n]{0,16}(?:back|forward)|"
    r"(?:sidebar|mobile|primary)\s+navigation|navigation\s+(?:menu|drawer)|"
    r"多页(?:面)?|多路由|深链接|浏览器[^，。；;\n]{0,16}(?:前进|后退)|"
    r"(?:移动端|侧边栏|主)(?:菜单|导航)|导航(?:菜单|抽屉))",
    re.IGNORECASE,
)
_SHOWCASE_INTENT = re.compile(
    r"high[ -]?difficulty|showcase|高难|(?:作品|成果)展示",
    re.IGNORECASE,
)
_SINGLE_SURFACE_INTENT = re.compile(
    r"\b(?:one|single)[ -]?(?:page|route|surface)\b|单页(?:面)?|单路由",
    re.IGNORECASE,
)
_NEGATED_ROUTE_INTENT = re.compile(
    r"(?:(?:do\s+not|don't|without|no|not)\s+"
    r"(?:(?:build|create|use|include|need)\s+)?(?:an?\s+)?"
    r"(?:showcase|high[ -]?difficulty|multi[ -]?(?:page|route)|"
    r"deep[ -]?links?|navigation)|"
    r"(?:不要|无需|不需要|禁止|不做)(?:构建|创建|使用|包含|做)?"
    r"(?:成果?展示|作品展示|高难|多页(?:面)?|多路由|深链接|导航))",
    re.IGNORECASE,
)
_DEEP_LINK_INTENT = re.compile(r"deep[ -]?links?|深链接", re.IGNORECASE)
_MOBILE_NAVIGATION_INTENT = re.compile(
    r"(?:mobile|responsive|narrow[- ]screen)[ -]?(?:menu|navigation|nav|drawer)|"
    r"(?:移动端|窄屏|响应式)(?:菜单|导航|抽屉)",
    re.IGNORECASE,
)
_HISTORY_INTENT = re.compile(
    r"browser[^,.;，。；\n]{0,24}(?:back[^,.;，。；\n]{0,12}forward|"
    r"forward[^,.;，。；\n]{0,12}back)|"
    r"浏览器[^,.;，。；\n]{0,24}(?:前进[^,.;，。；\n]{0,12}后退|"
    r"后退[^,.;，。；\n]{0,12}前进)",
    re.IGNORECASE,
)

_ARABIC_ROUTE_COUNT = re.compile(
    r"(?:at\s+least\s+)?(?P<count>[2-9]|[1-9][0-9]+)\s+"
    r"(?:(?:(?:real|top[ -]?level|distinct)\s+)?pages?\b"
    r"(?!\s+of\s+(?:(?:paginated|paged)\s+)?(?:records|results|items|data|rows)\b)|"
    r"(?:real\s+)?routes?\b)",
    re.IGNORECASE,
)
_ENGLISH_ROUTE_COUNT = re.compile(
    r"(?:at\s+least\s+)?(?P<count>two|three|four|five|six|seven|eight|nine|"
    r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty)\s+"
    r"(?:(?:(?:real|top[ -]?level|distinct)\s+)?pages?\b"
    r"(?!\s+of\s+(?:(?:paginated|paged)\s+)?(?:records|results|items|data|rows)\b)|"
    r"(?:real\s+)?routes?\b)",
    re.IGNORECASE,
)
_CHINESE_ROUTE_COUNT = re.compile(
    r"至少\s*(?P<count>[1-9][0-9]+|[2-9]|[一二两三四五六七八九十百千零]+)\s*个?\s*"
    r"(?:(?:真实)?路由|(?:真实|顶级|独立)页面)"
)
_ENGLISH_COUNT = {
    word: value
    for value, word in enumerate(
        (
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        )
    )
}
_CHINESE_DIGIT = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNIT = {"十": 10, "百": 100, "千": 1000}
_LOCAL_PATH = re.compile(LOCAL_PATH_PATTERN)
_EXPLICIT_PATH = re.compile(
    r"(?<![A-Za-z0-9_:/])/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)?"
    r"(?=$|[\s`'\"),，.;；。\]])"
)
_ROUTE_PATH_CLAUSE = re.compile(
    r"(?:create|build|implement)\s+(?:the\s+)?routes?\s+(?=/)|"
    r"(?:exact\s+)?(?:product\s+)?routes?\s*"
    r"(?:(?:are|include|including)\s*|[:：]\s*)|"
    r"(?:exact\s+)?route\s+paths?\s*"
    r"(?:(?:are|include|including)\s*|[:：]\s*)|"
    r"(?:构建|创建|实现)\s*(?:真实)?路由\s*(?=/)|"
    r"(?:真实)?路由(?:路径)?\s*(?:为|包括|包含|[:：])\s*",
    re.IGNORECASE,
)


def _positive_requirement(requirement: str) -> str:
    return _NEGATED_ROUTE_INTENT.sub("", requirement)


def _parse_chinese_count(raw: str) -> int:
    if raw.isascii():
        return int(raw)
    total = 0
    current = 0
    for character in raw:
        if character in _CHINESE_DIGIT:
            current = _CHINESE_DIGIT[character]
            continue
        unit = _CHINESE_UNIT[character]
        total += (current or 1) * unit
        current = 0
    return total + current


def explicit_route_paths(requirement: str) -> tuple[str, ...]:
    """Extract canonical paths only from explicit product-route clauses."""

    positive = _positive_requirement(requirement)
    paths: list[str] = []
    for clause in _ROUTE_PATH_CLAUSE.finditer(positive):
        tail = positive[clause.end() : clause.end() + 600]
        boundary = re.search(r"(?:\n\s*\n|[.;。；])", tail)
        body = tail[: boundary.start()] if boundary is not None else tail
        for match in _EXPLICIT_PATH.finditer(body):
            path = match.group(0)
            if _LOCAL_PATH.fullmatch(path) and path not in paths:
                paths.append(path)
    return tuple(paths)


def requires_multi_route(requirement: str) -> bool:
    """Classify source requests whose route shape cannot be planner-selected."""

    positive = _positive_requirement(requirement)
    counted = any(
        pattern.search(positive) is not None
        for pattern in (_ARABIC_ROUTE_COUNT, _ENGLISH_ROUTE_COUNT, _CHINESE_ROUTE_COUNT)
    )
    listed = len(explicit_route_paths(positive)) >= 2
    hard = _HARD_MULTI_ROUTE_INTENT.search(positive) is not None
    if counted or listed or hard:
        return True
    if _SINGLE_SURFACE_INTENT.search(positive):
        return False
    return _SHOWCASE_INTENT.search(positive) is not None


def required_route_count(requirement: str) -> int:
    """Return the minimum authoritative route count implied by the source."""

    positive = _positive_requirement(requirement)
    counts = [
        int(match.group("count"))
        for match in _ARABIC_ROUTE_COUNT.finditer(positive)
    ]
    counts.extend(
        _ENGLISH_COUNT[match.group("count").lower()]
        for match in _ENGLISH_ROUTE_COUNT.finditer(positive)
    )
    counts.extend(
        _parse_chinese_count(match.group("count"))
        for match in _CHINESE_ROUTE_COUNT.finditer(positive)
    )
    listed_count = len(explicit_route_paths(positive))
    if listed_count:
        counts.append(listed_count)
    if counts:
        return max(counts)
    return 2 if requires_multi_route(requirement) else 1


def _asserts_route_identity(test: object, *, path: str, title: str) -> bool:
    assertions = getattr(test, "assertions", ())
    return any(
        item.kind == "url" and item.path == path for item in assertions
    ) and any(
        item.kind == "visible"
        and item.target.by == "role"
        and item.target.value == "heading"
        and item.target.name == title
        for item in assertions
    )


def _has_observable_history_roundtrip(draft: GoalGraphDraft) -> bool:
    routes_by_path = {route.path: route for route in draft.routes}
    for goal in draft.goals:
        for test in goal.acceptance.tests:
            for index, action in enumerate(test.actions):
                if action.kind != "history_roundtrip" or index != len(test.actions) - 1:
                    continue
                back_route = routes_by_path.get(action.back_path)
                forward_route = routes_by_path.get(action.forward_path)
                if (
                    back_route is None
                    or forward_route is None
                    or not _asserts_route_identity(
                        test,
                        path=forward_route.path,
                        title=forward_route.title,
                    )
                ):
                    continue
                setup = test.actions[:index]
                navigation_setup = [
                    item
                    for item in setup
                    if item.kind in {"goto", "back", "forward", "history_roundtrip"}
                    or (
                        item.kind == "click"
                        and item.target.by == "role"
                        and item.target.value == "link"
                    )
                ]
                if (
                    len(navigation_setup) >= 2
                    and navigation_setup[-2].kind == "goto"
                    and navigation_setup[-2].path == back_route.path
                    and navigation_setup[-1].kind == "goto"
                    and navigation_setup[-1].path == forward_route.path
                ):
                    return True
                if len(navigation_setup) < 2:
                    continue
                source = navigation_setup[-2]
                link = navigation_setup[-1]
                if (
                    source.kind == "goto"
                    and source.path == back_route.path
                    and link.kind == "click"
                    and link.target.by == "role"
                    and link.target.value == "link"
                    and link.target.name == forward_route.title
                ):
                    return True
    return False


def _has_mobile_link_evidence(draft: GoalGraphDraft) -> bool:
    for route in draft.routes:
        if route.path == "/":
            continue
        for test in route_link_evidence_tests(draft, route.path):
            last_viewport = next(
                (
                    action
                    for action in reversed(test.actions[:-1])
                    if action.kind == "set_viewport"
                ),
                None,
            )
            if last_viewport is not None and last_viewport.width <= 480:
                return True
    return False


def validate_goal_graph_routing(
    requirement: str,
    draft: GoalGraphDraft,
) -> GoalGraphDraft:
    """Reject a valid v2 graph that still weakens explicit route requirements."""

    violations: list[str] = []
    minimum = required_route_count(requirement)
    if len(draft.routes) < minimum:
        violations.append(
            f"expected at least {minimum} real routes, received {len(draft.routes)}"
        )
    required_paths = set(explicit_route_paths(requirement))
    missing_paths = sorted(required_paths.difference(route.path for route in draft.routes))
    if missing_paths:
        violations.append("missing explicitly requested routes: " + ", ".join(missing_paths))

    positive = _positive_requirement(requirement)
    if _DEEP_LINK_INTENT.search(positive):
        not_deep = [route.path for route in draft.routes if not route.deep_linkable]
        if not_deep:
            violations.append(
                "explicit deep-link support requires deepLinkable=true for: "
                + ", ".join(not_deep)
            )
    if _MOBILE_NAVIGATION_INTENT.search(positive):
        if not _has_mobile_link_evidence(draft):
            violations.append(
                "mobile navigation requires a non-root link test with set_viewport width<=480"
            )
    if _HISTORY_INTENT.search(positive) and not _has_observable_history_roundtrip(draft):
        violations.append(
            "browser back/forward requires a terminal history_roundtrip with exact "
            "back and forward URL observations"
        )
    if violations:
        raise ValueError(
            "GoalGraph route intent violations:\n- " + "\n- ".join(violations)
        )
    return draft


def _routing_planning_brief(requirement: str) -> str:
    classified = requires_multi_route(requirement)
    minimum = required_route_count(requirement)
    classification = (
        "MULTI_ROUTE_REQUIRED: the source request explicitly requires a navigation menu, "
        "multiple pages/routes, deep-link/history behavior, or a high-difficulty showcase. "
        f"Declare at least {minimum} routes."
        if classified
        else "ROUTE_SHAPE_PLANNER_SELECTED: choose one route only for a genuinely single-surface "
        "product; otherwise declare every top-level destination as a separate route."
    )
    listed_paths = explicit_route_paths(requirement)
    listed = (
        " Preserve these explicitly requested paths: " + ", ".join(listed_paths) + "."
        if listed_paths
        else ""
    )
    return f"""FOMO authoritative routing contract v2:
- Output `schemaVersion: 2` and a complete `routes` manifest. FOMO derives `navigationMode`; never submit that server-owned field.
- {classification}{listed}
- The manifest must include `/`. Every path is one canonical same-origin local path with no `//`, query, hash, dynamic bracket, or trailing-slash alias. Titles must be unique ignoring case. Each route declares `path`, `title`, `owningGoalId`, and `deepLinkable`; explicit deep-link requirements make every applicable route deep-linkable.
- Every route owner needs an independent direct test asserting exact `url(path)` and exact visible role=`heading` name=`route.title`. A normal route's final action is `goto(path)`; a deep-linkable route's final two actions are exactly `goto(path)`, `reload`, with nothing after them.
- With multiple routes, every non-root route needs a separate link test in its owner. Its final action is role=`link` with accessible name exactly equal to the target route title; it asserts the target's exact URL and exact heading. The last preceding goto is another declared source whose owner is either the same goal or a transitive dependency of the target owner.
- Mobile navigation evidence puts `set_viewport` width <= 480 before a qualifying non-root link. Browser back/forward evidence uses a separate terminal `history_roundtrip` action with distinct declared `backPath`/`forwardPath`; FOMO asserts both URLs inside that action. State tabs, hashes, query panels, raw `back`/`forward`, or a later goto never prove routing.
- Cross-route persistence must be proven through a visible mutation, real route navigation, reload, and visible resulting state rather than an implementation-only assertion."""


_ROUTING_DELIVERY_BRIEF = """FOMO route-delivery policy:
- The embedded `navigationContract` is complete, frozen, server-owned, and read-only. Implement it exactly; never rename, add, remove, or collapse its routes in the active Goal.
- Treat route paths and their acceptance journeys as frozen product behavior. Implement each top-level destination as a real Next.js App Router page reachable at its exact path.
- Render one exact accessible heading matching each route title and use accessible links with those exact route-title names for cross-route movement. Route-level destinations must not be implemented as state tabs, hashes, or query-only panels; tabs are allowed only for subordinate content that remains within one route.
- Preserve direct loading, reload, browser history and narrow-screen navigation whenever the active acceptance contract exercises them. Shared browser state must remain consistent across real route transitions and reloads."""


_PRODUCT_MANAGER_BRIEF = f"""FOMO product-requirements policy {PRODUCT_REQUIREMENTS_POLICY}:
- Act as a product manager translating the user's source request into an implementation-ready product contract. Preserve every explicit requirement and priority; do not replace the requested product with a generic dashboard, landing page, or component demo.
- Make the product outcome concrete where relevant: intended users and use context, the problem and product objective, the primary end-to-end journey, in-scope capabilities and boundaries, key data or persistence expectations, meaningful states and failure recovery, and the observable result that makes the product useful. Do not mechanically add a category that has no bearing on this product.
- Describe workflows as user intent -> action -> system feedback -> completed outcome. Acceptance criteria should prove the complete primary journey and the highest-risk supporting behavior, not merely that headings, cards, or controls render.
- When the source request is underspecified, use product judgment to make reasonable, reversible assumptions that produce a coherent and useful product. Choose their depth according to the product's complexity, record consequential assumptions in the outcome or goal that depends on them, and avoid unrelated major workflows or irreversible business rules.
- Distinguish product requirements from implementation and visual recommendations. Freeze user-visible outcomes, content and state behavior; leave file topology, exact component composition, and other reversible implementation choices to the coding turn.
- Use realistic domain language, sample content and labels so the result can be evaluated as a product. Avoid filler copy, vanity metrics, vague promises, and feature lists that do not participate in a workflow.

For GoalGraph planning, `productOutcome` is the compact product brief rather than a slogan: include the relevant audience, objective, primary journey, scope, critical states, and success outcome. Each goal's `productOutcome` must describe a complete user-visible vertical result and its important feedback or recovery behavior. For legacy PlanningBundle planning, carry the same information through the build-plan summary, routes, criteria, and tests."""


_PRODUCT_DELIVERY_BRIEF = f"""FOMO product-delivery policy {PRODUCT_REQUIREMENTS_POLICY}:
- Treat the original user request plus the frozen product outcome, active goal, and acceptance behavior as one product-manager contract. Deliver the complete active outcome and all supporting architecture, integration, content, and states needed to make that outcome genuinely usable; do not reduce it to the easiest visible controls or isolated test fixtures.
- Resolve unspecified details autonomously with product judgment consistent with the intended users and use context. Make reversible adjacent improvements when they strengthen the requested experience, while avoiding unrelated major workflows or irreversible business rules.
- Use realistic domain content and explicit system feedback. Loading, empty, validation, error, success, disabled, and persisted/reloaded states are required only where the workflow can actually encounter them, and must help the user understand what happened and what to do next.
- The acceptance contract is the verification floor, not the full product description. Preserve user-visible requirements from the source request and frozen outcome even when a detail is not directly asserted by a test."""


_PRODUCT_DESIGN_BRIEF = f"""FOMO product-design policy {PRODUCT_DESIGN_POLICY}:
- Preserve the requested product breadth across the plan. Planning must represent it all; a goal build must completely deliver its active outcome and protect verified capabilities. GoalGraph is delivery ordering, never a limit on product ambition, information architecture, workflows, content, states, responsive behavior, shared foundations, or integration work needed for a coherent result.
- Build a coherent product surface, not a component showcase or acceptance-test prop. During planning, rely on the embedded verified Base Snapshot; during implementation, read the relevant existing source and capabilities before choosing a clear hierarchy, realistic content, appropriate information density, and obvious primary actions.
- If the user specifies a visual direction, follow it. Otherwise infer a fitting direction from the product, audience, task frequency, and content density, then make one coherent original choice for hierarchy, typography, composition, color, depth, and motion. Do not force an Apple, SaaS-dashboard, glass, gradient, or marketing-site treatment onto every product.
- Make useful, reversible product-design decisions wherever the requirement is silent: grouping, navigation, content examples, density, affordances, state presentation, hierarchy, and interaction polish. Let the product's complexity determine their depth. Exercise independent visual judgment during implementation without stopping for approval on these decisions.
- Cover the complete primary journey and the meaningful loading, empty, error, success, disabled, focus, and narrow-screen states. Reuse accessible shadcn/ui Radix primitives and Lucide icons when available; keep keyboard use, labels, contrast, and responsive behavior intact.
- Prefer one strong composition and deliberate visual rhythm. Avoid the generic giant-heading-plus-a-few-cards result, decorative gradients by default, excessive glass or nested cards, filler metrics, repeated icon tiles, placeholder copy, and ornamental UI that has no product purpose. Subtract weak decoration or redundant chrome when that produces a more premium result.
- Let code topology follow coherent domain, state, and UI responsibilities. Use as many or as few files and components as the product architecture warrants; neither add filler nor compress the product merely to minimize changed paths."""


_READ_ONLY_DELEGATION_BRIEF = """Optional read-only parallel research:
- `delegate_subtasks` may run up to three isolated read-only children in parallel. Use it only when genuinely independent codebase questions can be investigated concurrently and doing so is likely to save meaningful wall time. Skip it for small tasks, tightly coupled questions, or work you can answer with one direct inspection.
- Give each child one bounded, non-overlapping investigation. Children can only read/search/list; they cannot write, run commands, load repository instructions/extensions/skills, keep a session, request input, or delegate again.
- You remain the only writer and integrator. Treat child findings as bounded evidence, inspect anything consequential yourself, make all edits, resolve conflicts, and run the required advisory checks in the foreground session. Never delegate implementation, integration, or QA ownership."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _architecture_prompt_section(profile: ArchitectureProfile) -> str:
    """Render one engine-neutral profile as prose plus auditable JSON."""

    return (
        f"{profile.render_brief()}\n\n"
        "Frozen ArchitectureProfile (advisory responsibilities, not a file allowlist):\n"
        f"{_json(profile.as_prompt_context())}"
    )


def _goal_architecture_profile(
    requirement: str,
    plan: GoalExecutionPlan,
) -> ArchitectureProfile:
    return derive_product_architecture_profile(
        requirement=requirement,
        route_count=max(1, len(plan.routes)),
        goal_count=max(1, len(plan.verified_evidence) + 1),
    )


def _legacy_architecture_profile(
    requirement: str,
    planning_bundle: Mapping[str, object],
) -> ArchitectureProfile:
    build_plan = planning_bundle.get("buildPlan")
    routes = build_plan.get("routes") if isinstance(build_plan, Mapping) else None
    route_count = len(routes) if isinstance(routes, list) and routes else 1
    return derive_product_architecture_profile(
        requirement=requirement,
        route_count=route_count,
        goal_count=1,
    )


def _goal_execution_context(plan: GoalExecutionPlan) -> dict[str, object]:
    """Return only frozen goal data and durable evidence identifiers."""

    return {
        "graphRevision": plan.graph_revision,
        "navigationContract": {
            "schemaVersion": plan.graph_schema_version,
            "navigationMode": plan.navigation_mode.value,
            "routes": [
                route.model_dump(mode="json", by_alias=True) for route in plan.routes
            ],
        },
        "activeGoal": plan.active_goal.model_dump(mode="json", by_alias=True),
        "verifiedEvidence": [
            {
                "goalId": item.goal_id,
                "passedAcceptanceIds": list(item.passed_acceptance_ids),
                "evidenceRefs": list(item.evidence_refs),
            }
            for item in plan.verified_evidence
        ],
    }


def _bounded_goal_diagnostic(diagnostic: Mapping[str, object]) -> dict[str, object]:
    """Whitelist failed gates under a hard serialized-size budget."""

    bounded: dict[str, object] = {}
    for key in ("passed", "previewUrlAvailable"):
        value = diagnostic.get(key)
        if isinstance(value, bool):
            candidate = {**bounded, key: value}
            if len(_json(candidate)) <= _MAX_REPAIR_DIAGNOSTIC_JSON_CHARACTERS:
                bounded = candidate
    for key, limit in (
        ("diagnosticArtifactId", 100),
        ("validationMode", 50),
        ("validationReason", 100),
    ):
        value = diagnostic.get(key)
        if isinstance(value, str):
            candidate = {
                **bounded,
                key: bounded_diagnostic_text(value, limit=limit),
            }
            if len(_json(candidate)) <= _MAX_REPAIR_DIAGNOSTIC_JSON_CHARACTERS:
                bounded = candidate
    for key in ("gate", "code", "summary", "affectedFiles", "suggestedActions"):
        value = diagnostic.get(key)
        if isinstance(value, str):
            projected: object = bounded_diagnostic_text(value, limit=1_000)
        elif isinstance(value, (list, tuple)):
            projected = [
                bounded_diagnostic_text(item, limit=300)
                for item in value[:12]
                if isinstance(item, str)
            ]
        else:
            continue
        candidate = {**bounded, key: projected}
        if len(_json(candidate)) <= _MAX_REPAIR_DIAGNOSTIC_JSON_CHARACTERS:
            bounded = candidate

    raw_gates = diagnostic.get("gates")
    if not isinstance(raw_gates, (list, tuple)):
        return bounded
    failed_gates = [
        gate
        for gate in raw_gates[:20]
        if isinstance(gate, Mapping) and gate.get("status") == "failed"
    ]
    projected_gates: list[dict[str, object]] = []
    for gate in failed_gates:
        projected_gate: dict[str, object] = {}
        for key, limit in (
            ("gate", 100),
            ("scope", 50),
            ("status", 50),
            ("outcome", 50),
            ("summary", 500),
            ("acceptanceId", 200),
            ("testPath", 512),
            ("testName", 300),
        ):
            value = gate.get(key)
            if isinstance(value, str):
                projected_gate[key] = bounded_diagnostic_text(value, limit=limit)
        for key in ("exitCode", "line"):
            value = gate.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                projected_gate[key] = value
        timed_out = gate.get("timedOut")
        if isinstance(timed_out, bool):
            projected_gate["timedOut"] = timed_out
        affected = gate.get("affectedFiles")
        if isinstance(affected, (list, tuple)):
            projected_gate["affectedFiles"] = [
                bounded_diagnostic_text(item, limit=256)
                for item in affected[:8]
                if isinstance(item, str)
            ]
        raw_assertion = gate.get("diagnostic")
        if isinstance(raw_assertion, Mapping):
            assertion: dict[str, object] = {}
            for key, limit in (
                ("message", 800),
                ("locator", 400),
                ("testName", 300),
            ):
                value = raw_assertion.get(key)
                if isinstance(value, str):
                    assertion[key] = bounded_diagnostic_text(value, limit=limit)
            line = raw_assertion.get("line")
            if isinstance(line, int) and not isinstance(line, bool):
                assertion["line"] = line
            if assertion:
                projected_gate["diagnostic"] = assertion
        candidate = {**bounded, "gates": [*projected_gates, projected_gate]}
        if len(_json(candidate)) > _MAX_REPAIR_DIAGNOSTIC_JSON_CHARACTERS:
            break
        projected_gates.append(projected_gate)
    if projected_gates:
        bounded["gates"] = projected_gates
    omitted = len(failed_gates) - len(projected_gates)
    if omitted:
        candidate = {**bounded, "omittedFailedGateCount": omitted}
        if len(_json(candidate)) <= _MAX_REPAIR_DIAGNOSTIC_JSON_CHARACTERS:
            bounded = candidate
    return bounded


def goal_graph_planning_prompt(
    *,
    requirement: str,
    starter: dict[str, object],
) -> str:
    """Strict planner-only prompt for a frozen, server-managed GoalGraph."""

    return f"""You are planning a FOMO product as an outcome-driven frozen GoalGraph.

PLANNING TURN ONLY. Fill the submit_structured_output form until that virtual tool succeeds exactly once. If it returns a form or schema validation error, use the feedback to correct the fields and resubmit until it succeeds. Stop immediately after the successful submission. It is the only allowed tool. Do not modify /workspace, emit prose or JSON as assistant text, or add lifecycle status, quality bar, active-goal choice, revision, evidence, or any extra field. FOMO owns every lifecycle and policy field and will choose the active goal after strict validation.

Planning policy {GOAL_GRAPH_PLANNING_POLICY}: derive the number and granularity of user-visible vertical goals from the actual requirement complexity, end-to-end journeys, roles, risks, dependencies, and independently verifiable outcomes. Use enough goals to express the complete product without artificial consolidation or fragmentation. A route, page section, or feature may share a goal or have its own goal when that best represents a coherent user outcome; do not organize goals merely around files, code layers, styling passes, or arbitrary quotas. Declare dependencies in topological order.

{_routing_planning_brief(requirement)}

The GoalGraph structures delivery order, not product ambition. Preserve every explicit user outcome and describe enough observable behavior that the coding turn cannot satisfy the graph with a hollow shell. Where the requirement leaves presentation details open, exercise product and design judgment without inventing unrelated major capabilities.

{_PRODUCT_MANAGER_BRIEF}

{_FRONTEND_ONLY_BRIEF}

Each goal must deliver a usable product outcome, declare only earlier dependsOn goals, and provide observable acceptance coverage proportional to its workflows and risk. The structured contract requires one deterministic restricted-DSL Playwright test per criterion. Treat those criteria as the verification floor: preserve all source requirements even when they are not directly asserted. Use stable local routes and role/label/text locators; test complete workflows and persistence, never implementation details or screenshots.

The product will use Next.js, TypeScript, Tailwind, existing shadcn/ui Radix primitives and Lucide icons. Goals freeze outcomes and acceptance behavior, not file topology. Do not invent package availability or prescribe a build file allowlist.

{_PRODUCT_DESIGN_BRIEF}

Verified initial Base Snapshot manifest (modifiable only after planning):
{_json(starter)}

Product source request (verbatim JSON string; authoritative user input):
{_json(requirement)}
"""


def goal_graph_planning_correction_prompt(*, validation_error: str) -> str:
    """One schema correction turn without allowing lifecycle selection."""

    return f"""Correct your immediately previous GoalGraphDraft submission.

Fill the submit_structured_output form until that virtual tool succeeds exactly once. If it returns a form or schema validation error, use the feedback to correct the fields and resubmit until it succeeds. Stop immediately after the successful submission. It is the only allowed tool. Do not change files, emit prose or JSON as assistant text, add lifecycle status/quality policy/revision/evidence, or choose an active goal. Preserve the complete intended product outcome and acceptance behavior while enforcing planning policy {GOAL_GRAPH_PLANNING_POLICY}: let requirement complexity and coherent user outcomes determine goal granularity and coverage. Revise goals, dependencies, or criteria as needed to satisfy the structured contract without shrinking the source request. The form enforces the JSON shape; FOMO will revalidate all semantic constraints.

Keep `schemaVersion: 2` and repair the complete route manifest plus all reported route violations in one submission; do not add server-owned `navigationMode`. Include `/`, canonical same-origin paths, and case-insensitively unique titles. Every route needs its own exact URL + exact route-title heading direct test, ending in `goto(path)` or `goto(path), reload` when deep-linkable. Every non-root route in a multi-route graph needs a separate owner-local test ending in its exact route-title role=`link`, sourced from the same owner or a transitive dependency and asserting the target URL + heading. Mobile evidence needs `set_viewport` width <= 480 before such a link; browser history needs a terminal observable `history_roundtrip`. Never use state tabs, hashes, query panels, raw history actions, or a later goto as route evidence.

{_FRONTEND_ONLY_BRIEF}

Bounded contract validation failure:
{validation_error[:4000]}
"""


def goal_build_prompt(
    *,
    requirement: str,
    starter: dict[str, object],
    execution_plan: GoalExecutionPlan,
    advisory_self_check_command: str,
    architecture_profile: ArchitectureProfile | None = None,
) -> str:
    """Build prompt for the one server-selected active goal."""

    architecture = architecture_profile or _goal_architecture_profile(
        requirement,
        execution_plan,
    )

    return f"""Continue FOMO's implementation for one server-selected goal in /workspace.

The Goal Manager selected the active goal. Deliver that goal as a complete, production-quality product outcome while preserving all previously verified behavior. You may create shared foundations, refactor architecture, and complete adjacent integration required for coherence even when those details are not directly asserted by acceptance tests. Do not change GoalGraph lifecycle or select a different goal. A completion statement is only a claim; only FOMO-owned QA evidence can mark the goal verified.

You may create, edit, move, and delete project files as implementation evidence requires. Use the official builtin tools. Prefer existing offline-installable dependencies. Keep the frozen graph revision, active goal outcome, dependencies, and acceptance contract unchanged.

{_READ_ONLY_DELEGATION_BRIEF}

FOMO has placed a current-goal advisory mirror under `tests/fomo-acceptance/**`. It is generated from the frozen acceptance DSL and is protected system input: inspect and run it, but never edit, delete, replace, bypass, or duplicate it. Before claiming completion, run the exact command below. If typecheck or any Playwright workflow fails, fix the product source and rerun the same command until it passes. The Playwright config manages the app server on port 8080; do not leave a competing manual dev server running.

Advisory self-check command:
{advisory_self_check_command}

This self-check is fast implementation feedback only. It never verifies the goal, creates evidence, or replaces FOMO's independently recompiled tests in the clean verification sandbox.

Keep the user informed through public progress text that FOMO can stream over SSE. Before the first tool batch and at each important decision or new related group of tool calls, write 1-2 concise sentences stating what you will do and the practical reason. Do not reveal hidden chain-of-thought, narrate every minor operation, or make any extra tool call solely to report progress.

{_PRODUCT_DELIVERY_BRIEF}

{_FRONTEND_ONLY_BRIEF}

{_ROUTING_DELIVERY_BRIEF}

{_architecture_prompt_section(architecture)}

{_PRODUCT_DESIGN_BRIEF}

The design policy is a quality baseline, not a request to redesign unrelated previously verified work. Use the active goal and existing product as evidence, exercise visual judgment, and make necessary subtraction when it improves hierarchy or usability.

Previously verified evidence is provided only as bounded acceptance IDs and durable references; it is regression context, not raw logs. Preserve those verified outcomes. When implementation is complete, reply with a concise claim and integration summary. FOMO will verify the candidate separately.

Verified initial Base Snapshot manifest (modifiable during BUILDING):
{_json(starter)}

Original product source request (verbatim JSON string):
{_json(requirement)}

Frozen GoalExecutionPlan:
{_json(_goal_execution_context(execution_plan))}
"""


def goal_repair_prompt(
    *,
    execution_plan: GoalExecutionPlan,
    diagnostic: Mapping[str, object],
    round_number: int,
    advisory_self_check_command: str,
    architecture_profile: ArchitectureProfile | None = None,
) -> str:
    """Repair one goal using a whitelisted diagnostic summary, never raw logs."""

    if round_number < 1:
        raise ValueError("round_number must be positive")
    architecture = architecture_profile or _goal_architecture_profile(
        "",
        execution_plan,
    )
    return f"""Continue the same FOMO session and repair the server-selected active goal after deterministic verification round {round_number}.

Do not select or switch goals, weaken acceptance behavior, or edit FOMO-owned acceptance tests. The candidate remains only a claim until FOMO-owned QA verifies the current goal and conservatively reruns all previously verified goals. Make every root-cause, architectural, state, and product-integrity edit needed for a durable repair, while preserving verified outcomes.

{_READ_ONLY_DELEGATION_BRIEF}

{_FRONTEND_ONLY_BRIEF}

{_ROUTING_DELIVERY_BRIEF}

{_architecture_prompt_section(architecture)}

The protected `tests/fomo-acceptance/**` files are the current goal's advisory mirror. After repairing product source, run the exact command below. If it fails, continue repairing and rerun it until it passes; never modify, delete, replace, bypass, or duplicate the advisory tests. Ensure no competing manual dev server remains on port 8080.

Advisory self-check command:
{advisory_self_check_command}

Passing this command is advisory only. Release evidence still comes exclusively from FOMO's independently recompiled suite in a clean verification sandbox.

Preserve the coherent hierarchy, accessibility, responsive behavior, and purposeful visual decisions established under {PRODUCT_DESIGN_POLICY}. Do not make a failed locator pass by flattening the product into a stub or replacing intentional UI with generic cards.

Keep the user informed through public progress text that FOMO can stream over SSE. Before the first repair tool batch and at each important diagnostic decision or new related group of tool calls, write 1-2 concise sentences stating what you will check or change and the practical reason. Do not reveal hidden chain-of-thought, narrate every minor operation, or make any extra tool call solely to report progress.

Frozen GoalExecutionPlan:
{_json(_goal_execution_context(execution_plan))}

Bounded structured diagnostic (raw terminal output intentionally excluded):
{_json(_bounded_goal_diagnostic(diagnostic))}
"""


def planning_prompt(*, requirement: str, starter: dict[str, object]) -> str:
    return f"""You are FOMO's single Direct Pi coding agent.

PLANNING TURN ONLY. Fill the submit_structured_output form until that virtual tool succeeds exactly once. If it returns a form or schema validation error, use the feedback to correct the fields and resubmit until it succeeds. Stop immediately after the successful submission. It is the only allowed tool. Do not change any workspace file or emit prose or JSON as assistant text. The verified initial Base Snapshot manifest is embedded below; it is the starting point and is modifiable during BUILDING. Fill every required PlanningBundle field with clear purposes, criteria, and test steps; add no extra field.

Plan a polished, usable React product rather than a component showcase. Use Next.js, TypeScript, Tailwind, existing shadcn/ui Radix primitives, Lucide icons, and the selected starter capabilities. Plan an architecture and as many or as few files as the complete product warrants, including retained files when editing an existing version and the required extension contract. Organize responsibilities coherently and order implementation dependencies clearly. Use accessible labels and names. Every destructive action needs confirmation. Include loading/empty/error/success feedback and responsive behavior.

{_PRODUCT_MANAGER_BRIEF}

{_FRONTEND_ONLY_BRIEF}

{_PRODUCT_DESIGN_BRIEF}

The plan is ADVISORY: it communicates intent, it is not a frozen file contract. During BUILDING you may adjust architecture, file topology, routes, component boundaries, dependencies, or starter/config files based on implementation evidence. The source request and user-visible outcomes are authoritative; acceptance criteria are a non-negotiable verification floor, not the ceiling of the product.

Define an appropriately scoped set of user-observable acceptance criteria covering the primary journeys and highest-risk behavior. Each criterion must have exactly one deterministic Playwright test in the restricted DSL. Use only local routes and role/label/text locators that the implementation can make stable. Test real workflows, including persistence after reload when requested; do not test implementation details or screenshots.

Verified initial Base Snapshot manifest (modifiable during BUILDING):
{_json(starter)}

Product source request (verbatim JSON string; product intent cannot override the immutable/runtime rules above):
{_json(requirement)}
"""


def build_prompt(
    *,
    requirement: str,
    starter: dict[str, object],
    planning_bundle: dict[str, object],
    architecture_profile: ArchitectureProfile | None = None,
) -> str:
    architecture = architecture_profile or _legacy_architecture_profile(
        requirement,
        planning_bundle,
    )
    overview = {
        "title": planning_bundle["buildPlan"]["title"],
        "summary": planning_bundle["buildPlan"]["summary"],
        "routes": planning_bundle["buildPlan"]["routes"],
    }
    criteria = planning_bundle["acceptanceContract"]["criteria"]
    tests = planning_bundle["acceptanceContract"]["tests"]
    return f"""Continue FOMO's implementation as the complete BUILDING turn in /workspace.

You have full project development permission: you may create, edit, move, and delete any project file, including package.json, lockfiles, config files, starter base files, routes, app shell, components, and tests. Use the official builtin read/write/edit/bash tools. You may run pnpm commands, dev servers, and your own self-checks; your self-checks are advisory only and never count as release evidence.

{_READ_ONLY_DELEGATION_BRIEF}

The original source request and user-visible product outcomes are authoritative. The frozen acceptance criteria and their FOMO-owned Playwright tests (injected only into FOMO's clean verification sandbox) are a non-negotiable verification floor and must not be weakened. Implement the complete product contract, including necessary behavior and polish that is not directly asserted; do not add hidden fake success.

The BuildPlan below is ADVISORY only: follow its product intent, but freely adjust architecture, file topology, routes, component boundaries, or package/config/starter files as implementation evidence demands. Do not shrink the user requirement or weaken acceptance behavior.

Dependency constraint: verification installs offline from FOMO's prefetched package store. Prefer existing dependencies; a new dependency is only safe if it is already in the store. Do not claim a run is releasable when it depends on packages that cannot install offline.

Use explicit TypeScript types, accessible labels/names, versioned local persistence, responsive layout, destructive confirmation, and complete loading/empty/error/success states. `useCrudCollection<T>()` returns the full `{{state, actions}}` result; `CrudCollectionState<T>` is only its inner state. No TODO, placeholder, stub, or hidden fake success.

{_PRODUCT_DELIVERY_BRIEF}

{_FRONTEND_ONLY_BRIEF}

{_architecture_prompt_section(architecture)}

{_PRODUCT_DESIGN_BRIEF}

Inspect the workspace as broadly as needed and run any useful local self-checks that the sandbox supports, including typecheck, build, or focused interaction tests. FOMO will independently repeat verification from a clean sandbox. When complete, provide a concise integration handoff; the filesystem is authoritative.

Verified initial Base Snapshot manifest (modifiable during BUILDING):
{_json(starter)}

Original product source request (verbatim JSON string):
{_json(requirement)}

Advisory BuildPlan (product overview):
{_json(overview)}

Frozen acceptance criteria:
{_json(criteria)}

Frozen acceptance tests (must pass in FOMO's clean verification sandbox):
{_json(tests)}
"""


def build_repair_prompt(
    *,
    diagnostic: str,
    architecture_profile: ArchitectureProfile | None = None,
) -> str:
    architecture = architecture_profile or derive_product_architecture_profile(
        requirement="",
        route_count=1,
        goal_count=1,
    )
    return f"""Repair the immediately preceding BUILDING turn after FOMO's direct typecheck.

You may inspect and edit any project file in /workspace; you do not need permission lists. Do not weaken behavior, delete required acceptance coverage, or touch FOMO-owned acceptance tests (they live only in FOMO's verification sandbox). Use the bounded compiler output as evidence, then make every implementation, architecture, or integration change needed for a durable fix. Run any useful sandbox-supported self-checks and provide a concise handoff.

{_READ_ONLY_DELEGATION_BRIEF}

{_FRONTEND_ONLY_BRIEF}

{_architecture_prompt_section(architecture)}

Bounded typecheck diagnostic:
{diagnostic[:12000]}
"""


def planning_correction_prompt(*, validation_error: str) -> str:
    return f"""Correct your immediately previous PlanningBundle submission.

It did not satisfy FOMO's structured planning contract. Fill the submit_structured_output form until that virtual tool succeeds exactly once. If it returns a form or schema validation error, use the feedback to correct the fields and resubmit until it succeeds. Stop immediately after the successful submission; it is the only allowed tool. Do not change files or emit prose or JSON as assistant text. Preserve the complete product intent, but revise routes, architecture, files, criteria, and deterministic tests as needed to satisfy the contract without reducing the source request. The form enforces the JSON shape; FOMO will revalidate all semantic constraints.

{_FRONTEND_ONLY_BRIEF}

Bounded contract validation failure:
{validation_error[:4000]}
"""


def repair_prompt(
    *,
    planning_bundle: dict[str, object],
    diagnostic: dict[str, object],
    round_number: int,
    architecture_profile: ArchitectureProfile | None = None,
) -> str:
    architecture = architecture_profile or _legacy_architecture_profile(
        "",
        planning_bundle,
    )
    return f"""Continue the same FOMO session. Deterministic verification round {round_number} failed.

Repair the implementation using the supplied bounded evidence as a starting point, then inspect any relevant project source needed to understand the root cause. You may edit any project file in /workspace, including package/config/starter files; the BuildPlan is advisory, so refactor architecture and topology as needed. Keep the frozen acceptance criteria and FOMO-owned acceptance tests unchanged (they live only in FOMO's verification sandbox). Fix root causes without deleting behavior, weakening assertions, hiding errors, or replacing the product with a stub. Verification installs offline from FOMO's prefetched package store, so added dependencies must be available there. Run any useful sandbox-supported self-checks; FOMO will independently re-verify from a new clean sandbox. Reply with a concise summary when the edits are complete.

{_READ_ONLY_DELEGATION_BRIEF}

{_FRONTEND_ONLY_BRIEF}

{_architecture_prompt_section(architecture)}

Preserve the coherent hierarchy, accessibility, responsive behavior, and purposeful visual decisions established under {PRODUCT_DESIGN_POLICY}. Repair the root cause without degrading the product into generic test-oriented UI.

Advisory planning bundle:
{_json(planning_bundle)}

Deterministic diagnostic:
{_json(_bounded_goal_diagnostic(diagnostic))}
"""
