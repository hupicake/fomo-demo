"""Compile the frozen acceptance DSL into deterministic FOMO-owned Playwright tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fomo.sandbox.base import FileChange

from .contracts import (
    AcceptanceAction,
    AcceptanceAssertion,
    AcceptanceContract,
    Locator,
    SelectAction,
)
from .goalgraph import (
    NavigationVerificationSuite,
    ScopedAcceptanceContract,
    acceptance_persistence_key,
    acceptance_test_path,
    navigation_test_ids,
    scope_acceptance_contract,
)

ACCEPTANCE_ROOT = "tests/fomo-acceptance"
ACCEPTANCE_CONFIG_PATH = f"{ACCEPTANCE_ROOT}/fomo.config.ts"
ADVISORY_ACCEPTANCE_CONFIG_PATH = f"{ACCEPTANCE_ROOT}/fomo.advisory.config.ts"
FOMO_HARNESS_PATH = "tests/harness/starter.smoke.spec.ts"
# The authoritative verifier invokes the root-owned Playwright CLI from this
# same immutable runtime cache. Every FOMO-injected config/spec imports the
# matching module by absolute path so a candidate-side ``pnpm install`` cannot
# introduce a second Playwright Test singleton through workspace node_modules.
# ``index.js`` is intentional: Playwright transpiles these TypeScript assets as
# CommonJS, while TypeScript can resolve the adjacent ``index.d.ts``.
FOMO_PLAYWRIGHT_TEST_MODULE = (
    "/opt/fomo/runtime-cache/fomo-next-radix-v2/"
    "node_modules/@playwright/test/index.js"
)


@dataclass(frozen=True, slots=True)
class CompiledAcceptance:
    changes: tuple[FileChange, ...]
    sha256_by_path: dict[str, str]
    test_path_by_acceptance_id: dict[str, str]
    test_name_by_acceptance_id: dict[str, str]
    acceptance_key_by_id: dict[str, str] | None = None
    navigation_test_path_by_id: dict[str, str] | None = None
    navigation_test_name_by_id: dict[str, str] | None = None


class AcceptanceCompilationError(ValueError):
    """Scoped acceptance inputs conflict and cannot be verified safely."""


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


_PREVIEW_PATH_HELPERS = """const fomoPreviewBasePath = process.env.FOMO_PREVIEW_BASE_PATH ?? "";
const fomoAppPath = (path: string) =>
  path === "/" ? fomoPreviewBasePath || "/" : `${fomoPreviewBasePath}${path}`;
const fomoAppUrl = (path: string) =>
  new URL(
    fomoAppPath(path),
    process.env.FOMO_PREVIEW_BASE_URL ?? "http://127.0.0.1:8080",
  ).toString();
"""


def _locator(value: Locator) -> str:
    if value.by == "role":
        return (
            f"page.getByRole({_quoted(value.value)}, "
            f"{{ name: {_quoted(value.name or '')}, exact: true }})"
        )
    if value.by == "label":
        return f"page.getByLabel({_quoted(value.value)}, {{ exact: true }})"
    return f"page.getByText({_quoted(value.value)}, {{ exact: true }})"


def _select_action(value: SelectAction) -> str:
    """Select through native and accessible custom controls without guessing.

    The frozen DSL names a control and an option, but generated applications
    may implement that contract with either a native ``select`` or an ARIA
    combobox/listbox (for example, Radix Select). Inspecting the resolved
    control keeps the branch deterministic while preserving strict/exact
    locators for both the control and option.
    """

    target = _locator(value.target)
    option = _quoted(value.value)
    return "\n".join(
        (
            "{",
            f"  const fomoSelectTarget = {target};",
            "  const fomoSelectControl = await fomoSelectTarget.evaluate((element) => ({",
            '    tagName: element.tagName.toLowerCase(),',
            '    role: (element.getAttribute("role") || "").toLowerCase(),',
            '    popup: (element.getAttribute("aria-haspopup") || "").toLowerCase(),',
            "  }));",
            '  if (fomoSelectControl.tagName === "select") {',
            f"    await fomoSelectTarget.selectOption({option});",
            '  } else if (fomoSelectControl.role === "listbox") {',
            "    await fomoSelectTarget",
            f'      .getByRole("option", {{ name: {option}, exact: true }})',
            "      .click();",
            "  } else if (",
            '    fomoSelectControl.role === "combobox" ||',
            '    fomoSelectControl.popup === "listbox"',
            "  ) {",
            "    await fomoSelectTarget.click();",
            f'    await page.getByRole("option", {{ name: {option}, exact: true }}).click();',
            "  } else {",
            '    throw new Error("FOMO select target is not a native select or ARIA listbox");',
            "  }",
            "}",
        )
    )


def _action(value: AcceptanceAction) -> str:
    if value.kind == "goto":
        return f"await page.goto(fomoAppPath({_quoted(value.path)}));"
    if value.kind == "click":
        return f"await {_locator(value.target)}.click();"
    if value.kind == "fill":
        return f"await {_locator(value.target)}.fill({_quoted(value.value)});"
    if value.kind == "select":
        return _select_action(value)
    if value.kind == "reload":
        return "await page.reload();"
    if value.kind == "back":
        return "await page.goBack();"
    if value.kind == "forward":
        return "await page.goForward();"
    if value.kind == "set_viewport":
        return f"await page.setViewportSize({{ width: {value.width}, height: {value.height} }});"
    return "\n".join(
        (
            "await page.goBack();",
            f"await expect(page).toHaveURL(fomoAppUrl({_quoted(value.back_path)}));",
            "await page.goForward();",
            f"await expect(page).toHaveURL(fomoAppUrl({_quoted(value.forward_path)}));",
        )
    )


def _assertion(value: AcceptanceAssertion) -> str:
    if value.kind == "visible":
        locator = _locator(value.target)
        if value.target.by == "text":
            # Text is often repeated legitimately (for example, the same
            # availability label in multiple cards). A visible assertion is
            # existential, so select one currently-visible match instead of
            # turning repeated copy into a Playwright strict-mode failure.
            locator = f"{locator}.filter({{ visible: true }}).first()"
        return f"await expect({locator}).toBeVisible();"
    if value.kind == "not_visible":
        return f"await expect({_locator(value.target)}).not.toBeVisible();"
    if value.kind == "value":
        return f"await expect({_locator(value.target)}).toHaveValue({_quoted(value.expected)});"
    return f"await expect(page).toHaveURL(fomoAppUrl({_quoted(value.path)}));"


def _test_source(
    title: str,
    actions: list[AcceptanceAction],
    assertions: list[AcceptanceAssertion],
    *,
    playwright_test_module: str = FOMO_PLAYWRIGHT_TEST_MODULE,
) -> str:
    body = [*(_action(item) for item in actions), *(_assertion(item) for item in assertions)]
    indented = "\n".join(
        f"  {line}" for statement in body for line in statement.splitlines()
    )
    return (
        f'import {{ expect, test }} from "{playwright_test_module}";\n\n'
        f"{_PREVIEW_PATH_HELPERS}\n"
        f"test({_quoted(title)}, async ({{ page }}) => {{\n{indented}\n}});\n"
    )


def _navigation_test_source(title: str, statements: Iterable[str]) -> str:
    indented = "\n".join(
        f"  {line}" for statement in statements for line in statement.splitlines()
    )
    return (
        f'import {{ expect, test }} from "{FOMO_PLAYWRIGHT_TEST_MODULE}";\n\n'
        f"{_PREVIEW_PATH_HELPERS}\n"
        f"test({_quoted(title)}, async ({{ page }}) => {{\n{indented}\n}});\n"
    )


def _navigation_sources(
    suite: NavigationVerificationSuite | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Compile FOMO-owned route mechanics, never planner-authored samples."""

    if suite is None:
        return {}, {}, {}
    root = next((route for route in suite.routes if route.path == "/"), None)
    sources: dict[str, str] = {}
    paths: dict[str, str] = {}
    names: dict[str, str] = {}

    def register(test_id: str, title: str, statements: Iterable[str]) -> None:
        path = f"{ACCEPTANCE_ROOT}/navigation-v{suite.version}/{test_id}.smoke.spec.ts"
        sources[path] = _navigation_test_source(title, statements)
        paths[test_id] = path
        names[test_id] = title

    direct_ids = navigation_test_ids(suite)[: len(suite.routes)]
    for route, direct_id in zip(suite.routes, direct_ids, strict=True):
        statements = [
            f"await page.goto(fomoAppPath({_quoted(route.path)}));",
            (
                f"await expect(page.getByRole(\"heading\", "
                f"{{ name: {_quoted(route.title)}, exact: true }})).toBeVisible();"
            ),
            f"await expect(page).toHaveURL(fomoAppUrl({_quoted(route.path)}));",
        ]
        if route.deep_linkable:
            statements.extend(
                (
                    "await page.reload();",
                    (
                        f"await expect(page.getByRole(\"heading\", "
                        f"{{ name: {_quoted(route.title)}, exact: true }})).toBeVisible();"
                    ),
                    f"await expect(page).toHaveURL(fomoAppUrl({_quoted(route.path)}));",
                )
            )
        register(
            direct_id,
            f"FOMO navigation direct load: {route.title}",
            statements,
        )

    non_root = tuple(route for route in suite.routes if route.path != "/")
    if not suite.shared_navigation_gate or root is None or not non_root:
        return sources, paths, names

    shared_statements: list[str] = []
    for route in non_root:
        shared_statements.extend(
            (
                'await page.goto(fomoAppPath("/"));',
                (
                    "await page.getByRole(\"navigation\", "
                    "{ name: \"Primary navigation\", exact: true }).filter({ visible: true })"
                    f".getByRole(\"link\", {{ name: {_quoted(route.title)}, exact: true }}).click();"
                ),
                f"await expect(page).toHaveURL(fomoAppUrl({_quoted(route.path)}));",
                (
                    f"await expect(page.getByRole(\"heading\", "
                    f"{{ name: {_quoted(route.title)}, exact: true }})).toBeVisible();"
                ),
            )
        )
    register(
        "shared-navigation",
        "FOMO navigation: root links reach every route",
        shared_statements,
    )

    mobile_statements: list[str] = [
        "await page.setViewportSize({ width: 390, height: 844 });"
    ]
    for index, route in enumerate(non_root):
        variable = f"mobileLink{index}"
        mobile_statements.extend(
            (
                'await page.goto(fomoAppPath("/"));',
                (
                    f"let {variable} = page.getByRole(\"navigation\", "
                    "{ name: \"Primary navigation\", exact: true }).filter({ visible: true })"
                    f".getByRole(\"link\", {{ name: {_quoted(route.title)}, exact: true }});"
                ),
            )
        )
        mobile_statements.extend(
            (
                f"if ((await {variable}.count()) === 0) {{",
                "  await page.getByRole(\"button\", { name: \"Open navigation\", exact: true }).click();",
                "}",
                (
                    f"{variable} = page.getByRole(\"navigation\", "
                    "{ name: \"Primary navigation\", exact: true }).filter({ visible: true })"
                    f".getByRole(\"link\", {{ name: {_quoted(route.title)}, exact: true }});"
                ),
                f"await expect({variable}).toHaveCount(1);",
                f"await expect({variable}).toBeVisible();",
                f"await {variable}.click();",
                f"await expect(page).toHaveURL(fomoAppUrl({_quoted(route.path)}));",
                (
                    f"await expect(page.getByRole(\"heading\", "
                    f"{{ name: {_quoted(route.title)}, exact: true }})).toBeVisible();"
                ),
            )
        )
    register(
        "mobile-navigation-390",
        "FOMO navigation: 390px shared navigation reaches every route",
        mobile_statements,
    )

    history_statements: list[str] = []
    for target in non_root:
        history_statements.extend(
            (
                'await page.goto(fomoAppPath("/"));',
                (
                    "await page.getByRole(\"navigation\", "
                    "{ name: \"Primary navigation\", exact: true }).filter({ visible: true })"
                    f".getByRole(\"link\", {{ name: {_quoted(target.title)}, exact: true }}).click();"
                ),
                f"await expect(page).toHaveURL(fomoAppUrl({_quoted(target.path)}));",
                (
                    f"await expect(page.getByRole(\"heading\", "
                    f"{{ name: {_quoted(target.title)}, exact: true }})).toBeVisible();"
                ),
                "await page.goBack();",
                'await expect(page).toHaveURL(fomoAppUrl("/"));',
                (
                    f"await expect(page.getByRole(\"heading\", "
                    f"{{ name: {_quoted(root.title)}, exact: true }})).toBeVisible();"
                ),
                "await page.goForward();",
                f"await expect(page).toHaveURL(fomoAppUrl({_quoted(target.path)}));",
                (
                    f"await expect(page.getByRole(\"heading\", "
                    f"{{ name: {_quoted(target.title)}, exact: true }})).toBeVisible();"
                ),
            )
        )
    register(
        "history-roundtrip",
        "FOMO navigation: browser back and forward preserve every route identity",
        history_statements,
    )
    return sources, paths, names


_CONFIG_SOURCE = (
    f'import {{ defineConfig, devices }} from "{FOMO_PLAYWRIGHT_TEST_MODULE}";\n\n'
    """export default defineConfig({
  testDir: "../..",
  testMatch: "**/*.smoke.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "json",
  use: {
    baseURL: "http://127.0.0.1:8080",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
"""
)

_ADVISORY_CONFIG_SOURCE = (
    f'import {{ defineConfig, devices }} from "{FOMO_PLAYWRIGHT_TEST_MODULE}";\n\n'
    """export default defineConfig({
  testDir: "../..",
  testMatch: "**/*.smoke.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "line",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:8080",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "/usr/local/bin/node /opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/next/dist/bin/next dev --hostname 0.0.0.0 --port 8080",
    cwd: "../..",
    url: "http://127.0.0.1:8080",
    reuseExistingServer: false,
    timeout: 30_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
"""
)

_HARNESS_SOURCE = (
    f'import {{ expect, test }} from "{FOMO_PLAYWRIGHT_TEST_MODULE}";\n\n'
    f"{_PREVIEW_PATH_HELPERS}\n"
    """test("starter renders a stable application shell", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto(fomoAppPath("/"), { waitUntil: "domcontentloaded" });

  await expect(page.getByRole("main")).toBeVisible();
  expect(pageErrors).toEqual([]);
});
"""
)


def _fomo_verification_sources() -> dict[str, str]:
    """Return fixed V-only assets sharing the trusted runner identity.

    The portable starter keeps its normal package import. Creating V
    overwrites only its protected harness with this authoritative copy and
    adds the isolated acceptance config; application source stays portable.
    """

    return {
        ACCEPTANCE_CONFIG_PATH: _CONFIG_SOURCE,
        FOMO_HARNESS_PATH: _HARNESS_SOURCE,
    }


def _compile_sources(
    scoped_contracts: Iterable[ScopedAcceptanceContract],
    *,
    include_verification_assets: bool = True,
    playwright_test_module: str = FOMO_PLAYWRIGHT_TEST_MODULE,
    additional_sources: Mapping[str, str] | None = None,
    navigation_suite: NavigationVerificationSuite | None = None,
) -> CompiledAcceptance:
    scoped_values = tuple(scoped_contracts)
    single_goal = len(scoped_values) == 1
    sources = _fomo_verification_sources() if include_verification_assets else {}
    for path, content in (additional_sources or {}).items():
        if path in sources:
            raise AcceptanceCompilationError(f"duplicate acceptance source path: {path}")
        sources[path] = content
    navigation_sources, navigation_paths, navigation_names = _navigation_sources(
        navigation_suite
    )
    for path, content in navigation_sources.items():
        if path in sources:
            raise AcceptanceCompilationError(f"duplicate navigation source path: {path}")
        sources[path] = content
    test_paths: dict[str, str] = {}
    test_names: dict[str, str] = {}
    acceptance_keys: dict[str, str] = {}
    seen_goal_ids: set[str] = set()
    for scoped in scoped_values:
        if scoped.goal_id in seen_goal_ids:
            raise AcceptanceCompilationError(f"duplicate scoped acceptance goal: {scoped.goal_id}")
        seen_goal_ids.add(scoped.goal_id)
        for item in scoped.contract.tests:
            key = acceptance_persistence_key(scoped.goal_id, item.acceptance_id)
            path = scoped.test_path_by_test_id.get(item.id)
            expected_path = acceptance_test_path(scoped.goal_id, item.id)
            if path != expected_path:
                raise AcceptanceCompilationError(
                    f"invalid scoped acceptance test path for {scoped.goal_id}:{item.id}"
                )
            if key in test_paths or key in test_names or key in acceptance_keys.values():
                raise AcceptanceCompilationError(f"duplicate scoped acceptance key: {key}")
            if path in sources:
                raise AcceptanceCompilationError(f"duplicate acceptance test path: {path}")
            criterion_key = scoped.acceptance_key_by_id.get(item.acceptance_id)
            if criterion_key != key:
                raise AcceptanceCompilationError(
                    f"invalid scoped acceptance key for {scoped.goal_id}:{item.acceptance_id}"
                )
            sources[path] = _test_source(
                item.title,
                item.actions,
                item.assertions,
                playwright_test_module=playwright_test_module,
            )
            test_paths[key] = path
            test_names[key] = item.title
            acceptance_keys[item.acceptance_id if single_goal else key] = key

    changes = tuple(
        FileChange(path=path, content=content, operation="create")
        for path, content in sorted(sources.items())
    )
    return CompiledAcceptance(
        changes=changes,
        sha256_by_path=dict(
            sorted(
                (
                    path,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
                for path, content in sources.items()
            )
        ),
        test_path_by_acceptance_id=dict(sorted(test_paths.items())),
        test_name_by_acceptance_id=dict(sorted(test_names.items())),
        acceptance_key_by_id=dict(sorted(acceptance_keys.items())),
        navigation_test_path_by_id=dict(sorted(navigation_paths.items())),
        navigation_test_name_by_id=dict(sorted(navigation_names.items())),
    )


def compile_goal_acceptance(
    goal_id: str,
    contract: AcceptanceContract,
) -> CompiledAcceptance:
    """Compile one goal to isolated paths and globally durable criterion keys."""

    return _compile_sources((scope_acceptance_contract(goal_id, contract),))


def compile_goal_advisory_acceptance(
    goal_id: str,
    contract: AcceptanceContract,
    *,
    navigation_suite: NavigationVerificationSuite | None = None,
) -> CompiledAcceptance:
    """Compile one current-goal suite for advisory execution in G.

    Generation sandboxes use the immutable image runner and a protected
    advisory config, so candidate package scripts, Playwright config, and
    ``node_modules/.bin`` cannot redefine this feedback loop. The trusted V
    config and harness remain independent; this slice never constitutes
    release evidence.
    """

    return _compile_sources(
        (scope_acceptance_contract(goal_id, contract),),
        include_verification_assets=False,
        additional_sources={
            ADVISORY_ACCEPTANCE_CONFIG_PATH: _ADVISORY_CONFIG_SOURCE,
        },
        navigation_suite=navigation_suite,
    )


def compile_acceptance_suite(
    contracts: (
        Iterable[ScopedAcceptanceContract]
        | Mapping[str, AcceptanceContract]
        | Iterable[tuple[str, AcceptanceContract]]
    ),
    *,
    navigation_suite: NavigationVerificationSuite | None = None,
) -> CompiledAcceptance:
    """Compile a stable multi-goal FOMO-owned suite, failing closed on conflicts."""

    values: Iterable[ScopedAcceptanceContract]
    if isinstance(contracts, Mapping):
        values = (
            scope_acceptance_contract(goal_id, contract)
            for goal_id, contract in sorted(contracts.items())
        )
    else:
        normalized: list[ScopedAcceptanceContract] = []
        for item in contracts:
            if isinstance(item, ScopedAcceptanceContract):
                normalized.append(item)
            else:
                goal_id, contract = item
                normalized.append(scope_acceptance_contract(goal_id, contract))
        values = sorted(normalized, key=lambda item: item.goal_id)
    return _compile_sources(values, navigation_suite=navigation_suite)


def compile_acceptance(
    contract: AcceptanceContract,
    *,
    goal_id: str | None = None,
) -> CompiledAcceptance:
    """Compile P0 acceptance, or one isolated goal when ``goal_id`` is supplied.

    The no-``goal_id`` call is the P0 contract and intentionally preserves its
    original paths and acceptance-id keyed maps exactly.
    """

    if goal_id is not None:
        return compile_goal_acceptance(goal_id, contract)

    sources = _fomo_verification_sources()
    test_paths: dict[str, str] = {}
    test_names: dict[str, str] = {}
    for item in contract.tests:
        path = f"{ACCEPTANCE_ROOT}/{item.id}.smoke.spec.ts"
        if path in sources or item.acceptance_id in test_paths:
            raise AcceptanceCompilationError("duplicate P0 acceptance path or id")
        sources[path] = _test_source(item.title, item.actions, item.assertions)
        test_paths[item.acceptance_id] = path
        test_names[item.acceptance_id] = item.title
    changes = tuple(
        FileChange(path=path, content=content, operation="create")
        for path, content in sorted(sources.items())
    )
    return CompiledAcceptance(
        changes=changes,
        sha256_by_path={
            path: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for path, content in sources.items()
        },
        test_path_by_acceptance_id=test_paths,
        test_name_by_acceptance_id=test_names,
    )


__all__ = [
    "ACCEPTANCE_CONFIG_PATH",
    "ACCEPTANCE_ROOT",
    "ADVISORY_ACCEPTANCE_CONFIG_PATH",
    "FOMO_HARNESS_PATH",
    "FOMO_PLAYWRIGHT_TEST_MODULE",
    "AcceptanceCompilationError",
    "CompiledAcceptance",
    "compile_acceptance",
    "compile_acceptance_suite",
    "compile_goal_acceptance",
    "compile_goal_advisory_acceptance",
]
