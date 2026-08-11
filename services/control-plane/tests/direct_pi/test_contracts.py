from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from fomo.direct_pi import AcceptanceContract, compile_acceptance
from fomo.direct_pi.acceptance import (
    ACCEPTANCE_CONFIG_PATH,
    ADVISORY_ACCEPTANCE_CONFIG_PATH,
    FOMO_HARNESS_PATH,
    FOMO_PLAYWRIGHT_TEST_MODULE,
    compile_goal_acceptance,
    compile_goal_advisory_acceptance,
)


def _contract() -> dict[str, object]:
    return {
        "criteria": [
            {
                "id": "AC-1",
                "title": "Create a book",
                "priority": "must",
                "given": "The library is open",
                "when": "A book is added",
                "then": "The book appears in the table",
            }
        ],
        "tests": [
            {
                "id": "create-book",
                "acceptanceId": "AC-1",
                "title": "creates a book",
                "actions": [
                    {"kind": "goto", "path": "/"},
                    {
                        "kind": "click",
                        "target": {"by": "role", "value": "button", "name": "Add book"},
                    },
                    {
                        "kind": "fill",
                        "target": {"by": "label", "value": "Title"},
                        "value": "Dune",
                    },
                ],
                "assertions": [
                    {
                        "kind": "visible",
                        "target": {"by": "text", "value": "Dune"},
                    }
                ],
            }
        ],
    }


def test_contract_compiles_one_immutable_test_per_acceptance() -> None:
    contract = AcceptanceContract.model_validate(_contract())
    compiled = compile_acceptance(contract)

    path = "tests/fomo-acceptance/create-book.smoke.spec.ts"
    source = next(item.content for item in compiled.changes if item.path == path)
    assert 'test("creates a book"' in source
    assert 'page.getByRole("button", { name: "Add book", exact: true }).click()' in source
    assert compiled.test_path_by_acceptance_id == {"AC-1": path}
    assert compiled.sha256_by_path[path] == hashlib.sha256(source.encode()).hexdigest()


def test_compiled_navigation_uses_the_server_owned_preview_base_path() -> None:
    value = _contract()
    test = value["tests"][0]  # type: ignore[index]
    test["actions"][0]["path"] = "/catalog"  # type: ignore[index]
    test["assertions"].append({"kind": "url", "path": "/catalog"})  # type: ignore[index]

    compiled = compile_acceptance(AcceptanceContract.model_validate(value))
    sources = {item.path: item.content for item in compiled.changes}
    source = sources["tests/fomo-acceptance/create-book.smoke.spec.ts"]
    harness = sources[FOMO_HARNESS_PATH]

    assert 'const fomoPreviewBasePath = process.env.FOMO_PREVIEW_BASE_PATH ?? "";' in source
    assert 'process.env.FOMO_PREVIEW_BASE_URL ?? "http://127.0.0.1:8080"' in source
    assert 'await page.goto(fomoAppPath("/catalog"));' in source
    assert 'await expect(page).toHaveURL(fomoAppUrl("/catalog"));' in source
    assert "new RegExp" not in source
    assert 'await page.goto(fomoAppPath("/"), { waitUntil: "domcontentloaded" });' in harness


@pytest.mark.parametrize("path", ["//evil.test", "/catalog/", "/catalog?tab=all", "/#x"])
def test_navigation_paths_reject_external_or_noncanonical_aliases(path: str) -> None:
    value = _contract()
    test = value["tests"][0]  # type: ignore[index]
    test["actions"][0]["path"] = path  # type: ignore[index]
    test["assertions"].append({"kind": "url", "path": path})  # type: ignore[index]

    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        AcceptanceContract.model_validate(value)


def test_text_visibility_is_existential_without_relaxing_unique_operations() -> None:
    value = _contract()
    assertions = value["tests"][0]["assertions"]  # type: ignore[index]
    assertions.extend(  # type: ignore[union-attr]
        [
            {
                "kind": "visible",
                "target": {"by": "role", "value": "status", "name": "Ready"},
            },
            {
                "kind": "not_visible",
                "target": {"by": "text", "value": "Archived"},
            },
            {
                "kind": "value",
                "target": {"by": "label", "value": "Title"},
                "expected": "Dune",
            },
        ]
    )
    compiled = compile_acceptance(AcceptanceContract.model_validate(value))
    source = next(
        item.content
        for item in compiled.changes
        if item.path == "tests/fomo-acceptance/create-book.smoke.spec.ts"
    )

    assert (
        'expect(page.getByText("Dune", { exact: true }).filter({ visible: true }).first())'
        ".toBeVisible();"
    ) in source
    assert source.count(".first()") == 1
    assert 'page.getByRole("button", { name: "Add book", exact: true }).click()' in source
    assert 'page.getByLabel("Title", { exact: true }).fill("Dune")' in source
    assert (
        'expect(page.getByRole("status", { name: "Ready", exact: true })).toBeVisible()'
        in source
    )
    assert (
        'expect(page.getByText("Archived", { exact: true })).not.toBeVisible()'
        in source
    )
    assert (
        'expect(page.getByLabel("Title", { exact: true })).toHaveValue("Dune")'
        in source
    )


def test_select_adapts_to_native_select_and_exact_aria_options() -> None:
    value = _contract()
    actions = value["tests"][0]["actions"]  # type: ignore[index]
    actions.append(  # type: ignore[union-attr]
        {
            "kind": "select",
            "target": {"by": "label", "value": "票种"},
            "value": "VIP Pass",
        }
    )

    compiled = compile_acceptance(AcceptanceContract.model_validate(value))
    source = next(
        item.content
        for item in compiled.changes
        if item.path == "tests/fomo-acceptance/create-book.smoke.spec.ts"
    )

    assert (
        'const fomoSelectTarget = page.getByLabel("票种", { exact: true });'
        in source
    )
    assert 'fomoSelectControl.tagName === "select"' in source
    assert 'await fomoSelectTarget.selectOption("VIP Pass");' in source
    assert 'fomoSelectControl.role === "listbox"' in source
    assert (
        'fomoSelectTarget\n        .getByRole("option", { name: "VIP Pass", exact: true })'
        in source
    )
    assert 'fomoSelectControl.role === "combobox"' in source
    assert 'fomoSelectControl.popup === "listbox"' in source
    assert (
        'page.getByRole("option", { name: "VIP Pass", exact: true }).click()'
        in source
    )


def test_select_values_remain_quoted_inside_frozen_test_source() -> None:
    value = _contract()
    target = '票种");\nawait page.reload();\n//'
    option = 'VIP Pass");\nawait page.goto("/pwned");\n//'
    actions = value["tests"][0]["actions"]  # type: ignore[index]
    actions.append(  # type: ignore[union-attr]
        {
            "kind": "select",
            "target": {"by": "label", "value": target},
            "value": option,
        }
    )

    compiled = compile_acceptance(AcceptanceContract.model_validate(value))
    source = next(
        item.content
        for item in compiled.changes
        if item.path == "tests/fomo-acceptance/create-book.smoke.spec.ts"
    )
    quoted_target = json.dumps(target, ensure_ascii=False)
    quoted_option = json.dumps(option, ensure_ascii=False)

    assert (
        f"const fomoSelectTarget = page.getByLabel({quoted_target}, {{ exact: true }});"
        in source
    )
    assert f"selectOption({quoted_option})" in source
    assert f'{{ name: {quoted_option}, exact: true }}' in source
    assert '\nawait page.goto("/pwned")' not in source
    assert "\nawait page.reload();\n//" not in source


def test_compiled_verification_assets_share_the_root_owned_playwright_module() -> None:
    compiled = compile_acceptance(AcceptanceContract.model_validate(_contract()))
    sources = {item.path: item.content for item in compiled.changes}

    assert set(sources) == {
        ACCEPTANCE_CONFIG_PATH,
        FOMO_HARNESS_PATH,
        "tests/fomo-acceptance/create-book.smoke.spec.ts",
    }
    trusted_import = f'from "{FOMO_PLAYWRIGHT_TEST_MODULE}";'
    for source in sources.values():
        assert trusted_import in source
        assert 'from "@playwright/test"' not in source


def test_goal_advisory_compiler_emits_only_workspace_playwright_specs() -> None:
    contract = AcceptanceContract.model_validate(_contract())
    authoritative = compile_goal_acceptance("G-1", contract)
    advisory = compile_goal_advisory_acceptance("G-1", contract)

    path = "tests/fomo-acceptance/G-1/create-book.smoke.spec.ts"
    advisory_sources = {item.path: item.content for item in advisory.changes}
    authoritative_sources = {item.path: item.content for item in authoritative.changes}

    assert set(advisory_sources) == {ADVISORY_ACCEPTANCE_CONFIG_PATH, path}
    assert ACCEPTANCE_CONFIG_PATH not in advisory_sources
    assert FOMO_HARNESS_PATH not in advisory_sources
    assert advisory.test_path_by_acceptance_id == {"G-1:AC-1": path}
    assert advisory.test_name_by_acceptance_id == {"G-1:AC-1": "creates a book"}
    assert advisory.acceptance_key_by_id == {"AC-1": "G-1:AC-1"}

    advisory_source = advisory_sources[path]
    authoritative_source = authoritative_sources[path]
    assert advisory_source.startswith(
        f'import {{ expect, test }} from "{FOMO_PLAYWRIGHT_TEST_MODULE}";\n\n'
    )
    assert authoritative_source.startswith(
        f'import {{ expect, test }} from "{FOMO_PLAYWRIGHT_TEST_MODULE}";\n\n'
    )
    assert advisory_source.split("\n\n", 1)[1] == authoritative_source.split("\n\n", 1)[1]
    assert advisory.sha256_by_path[path] == hashlib.sha256(advisory_source.encode()).hexdigest()
    advisory_config = advisory_sources[ADVISORY_ACCEPTANCE_CONFIG_PATH]
    assert FOMO_PLAYWRIGHT_TEST_MODULE in advisory_config
    assert "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/next" in advisory_config
    assert "webServer" in advisory_config


def test_contract_accepts_supported_roles_and_explicit_null_reload_target() -> None:
    value = _contract()
    value["tests"][0]["actions"].append(  # type: ignore[index]
        {"kind": "reload", "target": None}
    )
    value["tests"][0]["actions"].insert(  # type: ignore[index]
        -1,
        {
            "kind": "fill",
            "target": {
                "by": "role",
                "value": "spinbutton",
                "name": "Inventory",
            },
            "value": "3",
        },
    )
    value["tests"][0]["assertions"].append(  # type: ignore[index]
        {
            "kind": "visible",
            "target": {
                "by": "role",
                "value": "alertdialog",
                "name": "Confirm deletion",
            },
        }
    )

    compiled = compile_acceptance(AcceptanceContract.model_validate(value))

    source = next(
        item.content
        for item in compiled.changes
        if item.path == "tests/fomo-acceptance/create-book.smoke.spec.ts"
    )
    assert 'page.getByRole("spinbutton", { name: "Inventory", exact: true }).fill("3")' in source
    assert "await page.reload();" in source
    assert 'page.getByRole("alertdialog", { name: "Confirm deletion", exact: true })' in source
