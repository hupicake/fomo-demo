"""GoalGraph prompts for one persistent Direct Pi product-development session.

The GoalGraph organizes delivery without capping product ambition, architecture,
or file topology. Pi keeps its official builtin tools with full ``/workspace``
permission. The source request and user-visible outcomes remain authoritative.
The generation sandbox receives a protected current-goal advisory mirror for
early self-checks; only the independently recompiled suite in the clean
verification sandbox counts as release evidence.
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
from .goalgraph import GoalGraphDraft

GOAL_GRAPH_PLANNING_POLICY = "frontend-ui-v7"
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


def validate_goal_graph_routing(
    requirement: str,
    draft: GoalGraphDraft,
) -> GoalGraphDraft:
    """Reject a valid v3 manifest that weakens explicit source requirements."""

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
    return f"""FOMO authoritative routing contract v3:
- Inside the envelope payload, output `schemaVersion: 3` and a complete `routes` manifest. FOMO derives `navigationMode` and `navigationSuiteVersion`; never submit those server-owned fields.
- {classification}{listed}
- The manifest must include `/`. Every path is one canonical same-origin local path with no `//`, query, hash, dynamic bracket, or trailing-slash alias. Titles must be unique ignoring case. Each route declares `path`, `title`, `owningGoalId`, and `deepLinkable`; explicit deep-link requirements make every applicable route deep-linkable.
- Every route owner is user-visible. The root owner must be the same goal as, or a transitive dependency of, every non-root owner. A goal's business tests may reference only routes owned by itself or its dependencies, never a future goal.
- Do not create criteria or tests merely to prove direct route loading, route-title headings, shared navigation links, mobile navigation, reload, or browser history. FOMO deterministically derives and versions that mechanical navigation suite from the frozen manifest.
- Cross-route persistence must be proven through a visible mutation, real route navigation, reload, and visible resulting state rather than an implementation-only assertion."""


_ROUTING_DELIVERY_BRIEF = """FOMO route-delivery policy:
- The embedded `navigationContract` is complete, frozen, server-owned, and read-only. Implement it exactly; never rename, add, remove, or collapse its routes in the active Goal.
- Treat route paths and their acceptance journeys as frozen product behavior. Implement each top-level destination as a real Next.js App Router page reachable at its exact path.
- Render one exact accessible heading matching each route title and use accessible links with those exact route-title names for cross-route movement. Route-level destinations must not be implemented as state tabs, hashes, or query-only panels; tabs are allowed only for subordinate content that remains within one route.
- FOMO always derives exact direct-load/heading checks for ready routes. At the final graph gate it clicks every non-root exact-title link from `/`, exercises navigation at 390px (opening a button named `Open navigation` when links are hidden), and observes browser back/forward for every non-root route plus deep-link reload. Implement these mechanics even though the model-authored acceptance omits their repetitive tests.
- Expose the shared navigation as an accessible `navigation` named `Primary navigation`. Shared browser state must remain consistent across real route transitions and reloads."""


_PRODUCT_MANAGER_BRIEF = f"""FOMO product-requirements policy {PRODUCT_REQUIREMENTS_POLICY}:
- Act as a product manager translating the user's source request into an implementation-ready product contract. Preserve every explicit requirement and priority; do not replace the requested product with a generic dashboard, landing page, or component demo.
- Make the product outcome concrete where relevant: intended users and use context, the problem and product objective, the primary end-to-end journey, in-scope capabilities and boundaries, key data or persistence expectations, meaningful states and failure recovery, and the observable result that makes the product useful. Do not mechanically add a category that has no bearing on this product.
- Describe workflows as user intent -> action -> system feedback -> completed outcome. Acceptance criteria should prove the complete primary journey and the highest-risk supporting behavior, not merely that headings, cards, or controls render.
- When the source request is underspecified, use product judgment to make reasonable, reversible assumptions that produce a coherent and useful product. Choose their depth according to the product's complexity, record consequential assumptions in the outcome or goal that depends on them, and avoid unrelated major workflows or irreversible business rules.
- Distinguish product requirements from implementation and visual recommendations. Freeze user-visible outcomes, content and state behavior; leave file topology, exact component composition, and other reversible implementation choices to the coding turn.
- Use realistic domain language, sample content and labels so the result can be evaluated as a product. Avoid filler copy, vanity metrics, vague promises, and feature lists that do not participate in a workflow.

`productOutcome` is the compact product brief rather than a slogan: include the relevant audience, objective, primary journey, scope, critical states, and success outcome. Each goal's `productOutcome` must describe a complete user-visible vertical result and its important feedback or recovery behavior."""


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


def _goal_execution_context(plan: GoalExecutionPlan) -> dict[str, object]:
    """Return only frozen goal data and durable evidence identifiers."""

    return {
        "graphRevision": plan.graph_revision,
        "navigationContract": {
            "schemaVersion": plan.graph_schema_version,
            "navigationSuiteVersion": plan.navigation_suite_version,
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

Provider transport envelope (the form is intentionally shallow): submit exactly `{{"envelopeVersion":1,"payloadJson":"<one JSON-encoded GoalGraphDraft>"}}`. `payloadJson` is a string, not a nested object or Markdown. Its decoded object has exactly `schemaVersion`, `productOutcome`, `routes`, and `goals`. A route has `path`, `title`, `owningGoalId`, `deepLinkable`. A goal has `goalId`, `title`, `productOutcome`, `userVisible`, `dependsOn`, and `acceptance`. Acceptance has equal one-to-one `criteria` and `tests`; a criterion has `id`, `title`, `priority`, `given`, `when`, `then`; a test has `id`, `acceptanceId`, `title`, nonempty `actions`, nonempty `assertions`. Actions are `goto(path)`, `click(target)`, `fill(target,value)`, `select(target,value)`, `reload`, `back`, `forward`, `set_viewport(width,height)`, or `history_roundtrip(backPath,forwardPath)`. Assertions are `visible(target)`, `not_visible(target)`, `value(target,expected)`, or `url(path)`. A target is exact `role(value,name)`, `label(value)`, or `text(value)`. Use these tests for business workflows only; FOMO owns mechanical navigation tests.

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

Submit the same exact `{{"envelopeVersion":1,"payloadJson":"..."}}` envelope and keep inner `schemaVersion: 3`. Repair the complete route manifest and reported business-contract violations in one submission; do not add server-owned `navigationMode` or `navigationSuiteVersion`. Include `/`, canonical same-origin paths, case-insensitively unique titles, user-visible owners, and root-owner dependency reachability. Business tests may reference only routes owned by their goal or dependencies. Do not add repetitive direct/link/mobile/history criteria: FOMO owns and derives them from the manifest.

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
