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
)
from .goalgraph import (
    ScopedAcceptanceContract,
    acceptance_persistence_key,
    acceptance_test_path,
    scope_acceptance_contract,
)

ACCEPTANCE_ROOT = "tests/fomo-acceptance"
ACCEPTANCE_CONFIG_PATH = f"{ACCEPTANCE_ROOT}/fomo.config.ts"
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


class AcceptanceCompilationError(ValueError):
    """Scoped acceptance inputs conflict and cannot be verified safely."""


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _locator(value: Locator) -> str:
    if value.by == "role":
        return (
            f"page.getByRole({_quoted(value.value)}, "
            f"{{ name: {_quoted(value.name or '')}, exact: true }})"
        )
    if value.by == "label":
        return f"page.getByLabel({_quoted(value.value)}, {{ exact: true }})"
    return f"page.getByText({_quoted(value.value)}, {{ exact: true }})"


def _action(value: AcceptanceAction) -> str:
    if value.kind == "goto":
        return f"await page.goto({_quoted(value.path)});"
    if value.kind == "click":
        return f"await {_locator(value.target)}.click();"
    if value.kind == "fill":
        return f"await {_locator(value.target)}.fill({_quoted(value.value)});"
    if value.kind == "select":
        return f"await {_locator(value.target)}.selectOption({_quoted(value.value)});"
    return "await page.reload();"


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
    return f"await expect(page).toHaveURL(new RegExp({_quoted(f'{value.path}$')}));"


def _test_source(
    title: str, actions: list[AcceptanceAction], assertions: list[AcceptanceAssertion]
) -> str:
    body = [*(_action(item) for item in actions), *(_assertion(item) for item in assertions)]
    indented = "\n".join(f"  {line}" for line in body)
    return (
        f'import {{ expect, test }} from "{FOMO_PLAYWRIGHT_TEST_MODULE}";\n\n'
        f"test({_quoted(title)}, async ({{ page }}) => {{\n{indented}\n}});\n"
    )


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

_HARNESS_SOURCE = (
    f'import {{ expect, test }} from "{FOMO_PLAYWRIGHT_TEST_MODULE}";\n\n'
    """test("starter renders a stable application shell", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/", { waitUntil: "domcontentloaded" });

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
) -> CompiledAcceptance:
    scoped_values = tuple(scoped_contracts)
    single_goal = len(scoped_values) == 1
    sources = _fomo_verification_sources()
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
            sources[path] = _test_source(item.title, item.actions, item.assertions)
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
    )


def compile_goal_acceptance(
    goal_id: str,
    contract: AcceptanceContract,
) -> CompiledAcceptance:
    """Compile one goal to isolated paths and globally durable criterion keys."""

    return _compile_sources((scope_acceptance_contract(goal_id, contract),))


def compile_acceptance_suite(
    contracts: (
        Iterable[ScopedAcceptanceContract]
        | Mapping[str, AcceptanceContract]
        | Iterable[tuple[str, AcceptanceContract]]
    ),
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
    return _compile_sources(values)


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
    "FOMO_HARNESS_PATH",
    "FOMO_PLAYWRIGHT_TEST_MODULE",
    "AcceptanceCompilationError",
    "CompiledAcceptance",
    "compile_acceptance",
    "compile_acceptance_suite",
    "compile_goal_acceptance",
]
