from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from fomo.direct_pi import PlanningBundle, compile_acceptance
from fomo.direct_pi.acceptance import (
    ACCEPTANCE_CONFIG_PATH,
    FOMO_HARNESS_PATH,
    FOMO_PLAYWRIGHT_TEST_MODULE,
)


def _bundle() -> dict[str, object]:
    return {
        "buildPlan": {
            "title": "Library desk",
            "summary": "Manage a durable book collection.",
            "visualPreset": "indigo",
            "routes": ["/"],
            "files": [
                {
                    "path": "app/(generated)/composition.tsx",
                    "purpose": "Compose the library workspace.",
                    "acceptanceIds": ["AC-1"],
                }
            ],
        },
        "acceptanceContract": {
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
        },
    }


def test_contract_compiles_one_immutable_test_per_acceptance() -> None:
    bundle = PlanningBundle.model_validate(_bundle())
    compiled = compile_acceptance(bundle.acceptance_contract)

    path = "tests/fomo-acceptance/create-book.smoke.spec.ts"
    source = next(item.content for item in compiled.changes if item.path == path)
    assert 'test("creates a book"' in source
    assert 'page.getByRole("button", { name: "Add book", exact: true }).click()' in source
    assert compiled.test_path_by_acceptance_id == {"AC-1": path}
    assert compiled.sha256_by_path[path] == hashlib.sha256(source.encode()).hexdigest()


def test_text_visibility_is_existential_without_relaxing_unique_operations() -> None:
    value = _bundle()
    assertions = value["acceptanceContract"]["tests"][0]["assertions"]  # type: ignore[index]
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
    bundle = PlanningBundle.model_validate(value)
    compiled = compile_acceptance(bundle.acceptance_contract)
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


def test_compiled_verification_assets_share_the_root_owned_playwright_module() -> None:
    bundle = PlanningBundle.model_validate(_bundle())
    compiled = compile_acceptance(bundle.acceptance_contract)
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


def test_contract_rejects_unmapped_acceptance_and_external_navigation() -> None:
    value = _bundle()
    value["buildPlan"]["files"][0]["acceptanceIds"] = ["AC-unknown"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="unknown acceptance"):
        PlanningBundle.model_validate(value)

    value = _bundle()
    value["acceptanceContract"]["tests"][0]["actions"][0]["path"] = "https://example.com"  # type: ignore[index]
    with pytest.raises(ValidationError, match="local path"):
        PlanningBundle.model_validate(value)


def test_plan_is_advisory_and_does_not_enforce_model_owned_write_scopes() -> None:
    # BuildPlan is display/consultation only: it may name any workspace path,
    # including package/config/starter files, without a business write-scope
    # validation (validate_plan_write_scope was removed with the frozen plan).
    value = _bundle()
    value["buildPlan"]["files"][0]["path"] = "package.json"  # type: ignore[index]
    value["buildPlan"]["files"].append(  # type: ignore[attr-defined]
        {
            "path": "lib/domain/books.ts",
            "purpose": "Typed state.",
            "acceptanceIds": ["AC-1"],
        }
    )
    bundle = PlanningBundle.model_validate(value)
    assert bundle.build_plan.files[0].path == "package.json"


def test_contract_accepts_next_dynamic_routes_and_explicit_null_reload_target() -> None:
    value = _bundle()
    value["buildPlan"]["routes"] = ["/", "/books/[id]"]  # type: ignore[index]
    value["acceptanceContract"]["tests"][0]["actions"].append(  # type: ignore[index]
        {"kind": "reload", "target": None}
    )
    value["acceptanceContract"]["tests"][0]["actions"].insert(  # type: ignore[index]
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
    value["acceptanceContract"]["tests"][0]["assertions"].append(  # type: ignore[index]
        {
            "kind": "visible",
            "target": {
                "by": "role",
                "value": "alertdialog",
                "name": "Confirm deletion",
            },
        }
    )

    bundle = PlanningBundle.model_validate(value)
    compiled = compile_acceptance(bundle.acceptance_contract)

    source = next(
        item.content
        for item in compiled.changes
        if item.path == "tests/fomo-acceptance/create-book.smoke.spec.ts"
    )
    assert 'page.getByRole("spinbutton", { name: "Inventory", exact: true }).fill("3")' in source
    assert "await page.reload();" in source
    assert 'page.getByRole("alertdialog", { name: "Confirm deletion", exact: true })' in source
