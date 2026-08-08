"""Compile the frozen acceptance DSL into deterministic FOMO-owned Playwright tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from fomo.sandbox.base import FileChange

from .contracts import (
    AcceptanceAction,
    AcceptanceAssertion,
    AcceptanceContract,
    Locator,
)

ACCEPTANCE_ROOT = "tests/fomo-acceptance"
ACCEPTANCE_CONFIG_PATH = f"{ACCEPTANCE_ROOT}/fomo.config.ts"


@dataclass(frozen=True, slots=True)
class CompiledAcceptance:
    changes: tuple[FileChange, ...]
    sha256_by_path: dict[str, str]
    test_path_by_acceptance_id: dict[str, str]
    test_name_by_acceptance_id: dict[str, str]


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
        return f"await expect({_locator(value.target)}).toBeVisible();"
    if value.kind == "not_visible":
        return f"await expect({_locator(value.target)}).not.toBeVisible();"
    if value.kind == "value":
        return f"await expect({_locator(value.target)}).toHaveValue({_quoted(value.expected)});"
    return f"await expect(page).toHaveURL(new RegExp({_quoted(f'{value.path}$')}));"


def _test_source(title: str, actions: list[AcceptanceAction], assertions: list[AcceptanceAssertion]) -> str:
    body = [*(_action(item) for item in actions), *(_assertion(item) for item in assertions)]
    indented = "\n".join(f"  {line}" for line in body)
    return (
        'import { expect, test } from "@playwright/test";\n\n'
        f"test({_quoted(title)}, async ({{ page }}) => {{\n{indented}\n}});\n"
    )


def compile_acceptance(contract: AcceptanceContract) -> CompiledAcceptance:
    """Produce one exactly-one-test file per AC plus a fixed isolated config."""
    config = """import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
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
    sources: dict[str, str] = {ACCEPTANCE_CONFIG_PATH: config}
    test_paths: dict[str, str] = {}
    test_names: dict[str, str] = {}
    for item in contract.tests:
        path = f"{ACCEPTANCE_ROOT}/{item.id}.smoke.spec.ts"
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
