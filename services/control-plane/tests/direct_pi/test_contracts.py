from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from fomo.direct_pi import PlanningBundle, compile_acceptance


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


def test_contract_rejects_unmapped_acceptance_and_external_navigation() -> None:
    value = _bundle()
    value["buildPlan"]["files"][0]["acceptanceIds"] = ["AC-unknown"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="unknown acceptance"):
        PlanningBundle.model_validate(value)

    value = _bundle()
    value["acceptanceContract"]["tests"][0]["actions"][0]["path"] = "https://example.com"  # type: ignore[index]
    with pytest.raises(ValidationError, match="local path"):
        PlanningBundle.model_validate(value)
