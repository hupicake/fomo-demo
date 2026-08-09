from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from fomo.agent_runtime.sop import ArtifactContractViolation, SOPRunner
from fomo.sandbox.opensandbox import OpenSandboxProvider
from fomo.schemas import TechnicalSpec
from fomo.starter import (
    StarterIntegrityError,
    capability_catalog,
    default_starter_manifest,
    resolve_starter_manifest,
    starter_validation_variants,
)


def _minimal_technical_spec(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "framework": "Next.js",
        "starterCapabilities": [],
        "componentDecisions": [
            {
                "component": "GeneratedComposition",
                "strategy": "reuse",
                "source": "Golden Starter v2",
                "rationale": "Keeps product code inside the generated extension boundary.",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_v2_bare_manifest_is_digest_pinned_and_keeps_a_generic_buildable_boundary() -> None:
    manifest = default_starter_manifest()

    assert manifest.id == "fomo-next-radix-v2"
    assert manifest.version == "2.0.0"
    assert manifest.selected_capabilities == ()
    assert manifest.is_protected_path("app/page.tsx")
    assert manifest.is_protected_path("components/system/app-shell.tsx")
    assert manifest.is_model_owned_path("app/(generated)/composition.tsx")
    assert manifest.is_model_owned_path("components/features/example.tsx")
    assert manifest.is_model_owned_path("lib/domain/example.ts")
    assert manifest.is_model_owned_path("tests/generated/example.smoke.spec.ts")
    assert not manifest.is_model_owned_path("tests/harness/example.ts")
    assert manifest.is_protected_path("tests/harness/starter.smoke.spec.ts")
    assert "@/components/system/app-shell" in manifest.available_imports
    assert "@/components/system/feedback" in manifest.available_imports
    assert "@/components/ui/button" in manifest.available_imports

    files = {entry.path: entry for entry in manifest.files}
    assert "app/(generated)/composition.tsx" in files
    assert "tests/harness/starter.smoke.spec.ts" in files
    assert "tests/harness/runtime.ts" not in files
    canonical_next_env = (
        '/// <reference types="next" />\n'
        '/// <reference types="next/image-types/global" />\n'
        'import "./.next/types/routes.d.ts";\n'
        'import "./.next/types/root-params.d.ts";\n'
        "\n"
        "// NOTE: This file should not be edited\n"
        "// see https://nextjs.org/docs/app/api-reference/config/typescript for more information.\n"
    )
    assert files["next-env.d.ts"]._content == canonical_next_env.encode("utf-8")
    page_source = files["app/page.tsx"]._content.decode("utf-8")
    assert "GeneratedComposition" in page_source
    assert "components/features" not in page_source
    portable_playwright_config = files["playwright.config.ts"]._content.decode("utf-8")
    portable_harness = files["tests/harness/starter.smoke.spec.ts"]._content.decode("utf-8")
    assert 'testDir: "./tests"' in portable_playwright_config
    assert 'reporter: "line"' in portable_playwright_config
    assert 'from "@playwright/test"' in portable_playwright_config
    assert 'from "@playwright/test"' in portable_harness
    assert "/opt/fomo/runtime-cache" not in portable_playwright_config
    assert "/opt/fomo/runtime-cache" not in portable_harness
    tsconfig = json.loads(files["tsconfig.json"]._content.decode("utf-8"))
    assert tsconfig["compilerOptions"]["jsx"] == "react-jsx"
    assert tsconfig["compilerOptions"]["incremental"] is True
    assert tsconfig["compilerOptions"]["tsBuildInfoFile"] == ".next/cache/tsconfig.tsbuildinfo"
    assert tsconfig["include"] == [
        "next-env.d.ts",
        "**/*.ts",
        "**/*.tsx",
        ".next/types/**/*.ts",
        ".next/dev/types/**/*.ts",
    ]
    next_config = files["next.config.ts"]._content.decode("utf-8")
    allowed_dev_origins = re.search(
        r"allowedDevOrigins:\s*\[(?P<origins>[^]]*)\]",
        next_config,
    )
    assert allowed_dev_origins is not None
    origins = allowed_dev_origins.group("origins").strip()
    assert origins == '"127.0.0.1"'
    assert "*" not in origins
    manifest.verify_tree({entry.path: entry._content for entry in manifest.files})


def test_v2_bare_and_capability_assets_fail_closed_when_a_seed_file_is_tampered() -> None:
    base = default_starter_manifest()
    combined = resolve_starter_manifest(("crud", "local-persistence"))

    tampered_base = {entry.path: entry._content for entry in base.files}
    tampered_base["package.json"] = b"{}\n"
    with pytest.raises(StarterIntegrityError, match="starter file verification failed"):
        base.verify_tree(tampered_base)

    tampered_capability = {entry.path: entry._content for entry in combined.files}
    tampered_capability["components/starter/crud-slots.tsx"] = b"export {};\n"
    with pytest.raises(StarterIntegrityError, match="starter file verification failed"):
        combined.verify_tree(tampered_capability)


def test_v2_capability_selection_is_order_independent_and_only_composes_selected_assets() -> None:
    bare = default_starter_manifest()
    crud = resolve_starter_manifest(("crud",))
    persistence = resolve_starter_manifest(("local-persistence",))
    combined = resolve_starter_manifest(("crud", "local-persistence"))
    reversed_combined = resolve_starter_manifest(("local-persistence", "crud"))

    assert combined.tree_sha256 == reversed_combined.tree_sha256
    assert bare.tree_sha256 != crud.tree_sha256
    assert crud.tree_sha256 != combined.tree_sha256
    assert combined.tree_sha256 == _independent_composite_hash(combined)
    assert tuple(capability.id for capability in combined.selected_capabilities) == (
        "crud",
        "local-persistence",
    )
    assert "@/components/starter/crud-slots" in crud.available_imports
    assert "@/lib/starter/local-persistence" not in crud.available_imports
    assert "@/lib/starter/local-persistence" in persistence.available_imports
    assert "@/components/starter/crud-slots" not in persistence.available_imports
    assert "components/starter/crud-slots.tsx" not in {entry.path for entry in bare.files}
    assert "components/starter/crud-slots.tsx" in {entry.path for entry in crud.files}
    assert "lib/starter/local-persistence.ts" in {entry.path for entry in persistence.files}
    assert combined.is_protected_path("components/starter/crud-slots.tsx")
    assert combined.is_protected_path("lib/starter/local-persistence.ts")
    assert not combined.is_model_owned_path("components/starter/crud-slots.tsx")
    assert not combined.is_model_owned_path("lib/starter/local-persistence.ts")
    combined.verify_tree({entry.path: entry._content for entry in combined.files})

    bare_files = {entry.path: entry for entry in bare.files}
    for manifest in (crud, persistence, combined):
        files = {entry.path: entry for entry in manifest.files}
        assert files["package.json"].sha256 == bare_files["package.json"].sha256
        assert files["pnpm-lock.yaml"].sha256 == bare_files["pnpm-lock.yaml"].sha256
    for capability in capability_catalog():
        assert "package.json" not in {entry.path for entry in capability.files}
        assert "pnpm-lock.yaml" not in {entry.path for entry in capability.files}


def test_v2_rejects_unknown_duplicate_and_synthetic_conflicting_capabilities() -> None:
    with pytest.raises(StarterIntegrityError, match="unknown capability"):
        resolve_starter_manifest(("not-approved",))
    with pytest.raises(StarterIntegrityError, match="duplicate capability"):
        resolve_starter_manifest(("crud", "crud"))

    crud = next(capability for capability in capability_catalog() if capability.id == "crud")
    conflicting = replace(crud, id="synthetic-conflict", conflicts=("crud",))
    with pytest.raises(StarterIntegrityError, match="conflicting capabilities"):
        resolve_starter_manifest(("crud", "synthetic-conflict"), catalog=(*capability_catalog(), conflicting))

    colliding = replace(crud, id="synthetic-collision", conflicts=())
    with pytest.raises(StarterIntegrityError, match="overlay path collision"):
        resolve_starter_manifest(("crud", "synthetic-collision"), catalog=(*capability_catalog(), colliding))


def test_technical_spec_exposes_only_the_fixed_capability_enum() -> None:
    technical = TechnicalSpec.model_validate(
        _minimal_technical_spec(starterCapabilities=["crud", "local-persistence"])
    )
    assert [capability.value for capability in technical.starter_capabilities] == [
        "crud",
        "local-persistence",
    ]

    omitted_selection = _minimal_technical_spec()
    omitted_selection.pop("starterCapabilities")
    with pytest.raises(ValidationError):
        TechnicalSpec.model_validate(omitted_selection)
    with pytest.raises(ValidationError):
        TechnicalSpec.model_validate(_minimal_technical_spec(starterCapabilities=["npm-install"]))
    with pytest.raises(ValidationError, match="duplicate"):
        TechnicalSpec.model_validate(_minimal_technical_spec(starterCapabilities=["crud", "crud"]))


def test_v2_provenance_binds_the_resolved_base_capabilities_composite_and_files() -> None:
    manifest = resolve_starter_manifest(("crud", "local-persistence"))
    provenance = manifest.as_provenance("initial-commit")

    assert provenance["base"]["id"] == "fomo-next-radix-v2"
    assert provenance["selectedCapabilities"] == [
        {"id": "crud", "version": "1.0.0", "treeSha256": manifest.selected_capabilities[0].tree_sha256},
        {
            "id": "local-persistence",
            "version": "1.0.0",
            "treeSha256": manifest.selected_capabilities[1].tree_sha256,
        },
    ]
    assert provenance["treeSha256"] == manifest.tree_sha256
    assert provenance["treeSha256"] == _independent_composite_hash(manifest)
    assert provenance["files"] == [entry.as_manifest_entry() for entry in manifest.files]


def _independent_composite_hash(manifest) -> str:
    canonical = (
        f"base\0{manifest.id}\0{manifest.version}\0{manifest.base_tree_sha256}\n"
        + "".join(
            f"capability\0{capability.id}\0{capability.version}\0{capability.tree_sha256}\n"
            for capability in sorted(manifest.selected_capabilities, key=lambda item: item.id)
        )
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def test_v2_architect_context_is_compact_and_binds_selected_capability_imports() -> None:
    bare_context = default_starter_manifest().as_architect_context()
    selected_context = resolve_starter_manifest(("crud",)).as_architect_context()

    assert "files" not in bare_context
    assert bare_context["selectedCapabilities"] == []
    assert bare_context["capabilityCatalog"] == [
        {
            "id": "crud",
            "version": "1.0.0",
            "treeSha256": bare_context["capabilityCatalog"][0]["treeSha256"],
            "availableImports": ["@/components/starter/crud-slots"],
            "protectedPaths": ["components/starter/crud-slots.tsx"],
            "conflicts": [],
            "description": "Reusable client-side collection state and rendering boundaries.",
            "provides": ["collection state", "create/update/remove actions", "render slots"],
        },
        {
            "id": "local-persistence",
            "version": "1.0.0",
            "treeSha256": bare_context["capabilityCatalog"][1]["treeSha256"],
            "availableImports": ["@/lib/starter/local-persistence"],
            "protectedPaths": ["lib/starter/local-persistence.ts"],
            "conflicts": [],
            "description": "A browser-only persistence boundary with explicit validation and migration.",
            "provides": ["SSR-safe localStorage access", "typed versioned envelopes", "migration adapter"],
        },
    ]
    assert selected_context["selectedCapabilities"] == [bare_context["capabilityCatalog"][0]]
    assert "@/components/starter/crud-slots" in selected_context["availableImports"]
    assert "@/lib/starter/local-persistence" not in selected_context["availableImports"]


def test_v2_root_extension_contract_is_compact_and_enforced_before_implementation() -> None:
    runner = object.__new__(SOPRunner)
    runner.settings = SimpleNamespace(engineer_max_batches=1, engineer_max_files_per_batch=4)
    manifest = default_starter_manifest()
    extension = manifest.root_extension_contract

    assert extension.path == "app/(generated)/composition.tsx"
    assert extension.operation == "modify"
    assert extension.export_style == "named"
    assert extension.symbol == "GeneratedComposition"
    assert manifest.as_architect_context()["extensionContracts"] == [extension.as_architect_context()]

    valid = TechnicalSpec.model_validate(
        _minimal_technical_spec(
            filePlan=[
                {
                    "path": extension.path,
                    "operation": extension.operation,
                    "reason": extension.purpose,
                }
            ],
            publicApiContracts=[
                {
                    "filePath": extension.path,
                    "exportStyle": extension.export_style,
                    "symbol": extension.symbol,
                    "props": [],
                    "type": "React.ComponentType",
                }
            ],
        )
    )
    runner._validate_technical_file_plan(valid)

    wrong_operation = TechnicalSpec.model_validate(
        _minimal_technical_spec(
            filePlan=[{"path": extension.path, "operation": "create", "reason": "must fail"}],
            publicApiContracts=[
                {
                    "filePath": extension.path,
                    "exportStyle": extension.export_style,
                    "symbol": extension.symbol,
                    "props": [],
                    "type": "React.ComponentType",
                }
            ],
        )
    )
    with pytest.raises(ArtifactContractViolation) as operation_error:
        runner._validate_technical_file_plan(wrong_operation)
    assert operation_error.value.code == "technical_spec.extension_contract.operation_invalid"

    missing_public_api = TechnicalSpec.model_validate(
        _minimal_technical_spec(
            filePlan=[
                {
                    "path": extension.path,
                    "operation": extension.operation,
                    "reason": extension.purpose,
                }
            ]
        )
    )
    with pytest.raises(ArtifactContractViolation) as public_api_error:
        runner._validate_technical_file_plan(missing_public_api)
    assert public_api_error.value.code == "technical_spec.extension_contract.public_api_required"

    forbidden_page = TechnicalSpec.model_validate(
        _minimal_technical_spec(
            filePlan=[
                {"path": extension.path, "operation": extension.operation, "reason": extension.purpose},
                {"path": "app/(generated)/page.tsx", "operation": "create", "reason": "must fail"},
            ],
            publicApiContracts=[
                {
                    "filePath": extension.path,
                    "exportStyle": extension.export_style,
                    "symbol": extension.symbol,
                    "props": [],
                    "type": "React.ComponentType",
                }
            ],
        )
    )
    with pytest.raises(ArtifactContractViolation) as page_error:
        runner._validate_technical_file_plan(forbidden_page)
    assert page_error.value.code == "technical_spec.extension_contract.page_path_rejected"


def test_v2_sop_validates_model_roots_and_protected_capabilities_from_the_same_selection() -> None:
    runner = object.__new__(SOPRunner)
    runner.settings = SimpleNamespace(engineer_max_batches=1, engineer_max_files_per_batch=4)
    technical = TechnicalSpec.model_validate(
        _minimal_technical_spec(
            starterCapabilities=["crud"],
            filePlan=[
                {
                    "path": "app/(generated)/composition.tsx",
                    "operation": "modify",
                    "reason": "product composition",
                },
                {
                    "path": "tests/generated/app.smoke.spec.ts",
                    "operation": "create",
                    "reason": "smoke coverage",
                },
            ],
        )
    )
    resolved = runner._starter_for_technical(technical)
    assert [capability.id for capability in resolved.selected_capabilities] == ["crud"]
    assert runner._technical_file_plan_paths(technical) == [
        "app/(generated)/composition.tsx",
        "tests/generated/app.smoke.spec.ts",
    ]

    protected_capability = TechnicalSpec.model_validate(
        _minimal_technical_spec(
            starterCapabilities=["crud"],
            filePlan=[
                {
                    "path": "components/starter/crud-slots.tsx",
                    "operation": "modify",
                    "reason": "must fail",
                }
            ],
        )
    )
    with pytest.raises(ArtifactContractViolation) as protected:
        runner._technical_file_plan_paths(protected_capability)
    assert protected.value.code == "technical_spec.file_plan.starter_protected"

    outside_generated_tests = TechnicalSpec.model_validate(
        _minimal_technical_spec(
            starterCapabilities=["crud"],
            filePlan=[
                {
                    "path": "tests/app.smoke.spec.ts",
                    "operation": "create",
                    "reason": "must fail",
                }
            ],
        )
    )
    with pytest.raises(ArtifactContractViolation) as outside_root:
        runner._technical_file_plan_paths(outside_generated_tests)
    assert outside_root.value.code == "technical_spec.file_plan.model_root"


def test_v2_workspace_manifest_rejects_extra_assets_before_file_hash_verification() -> None:
    manifest = default_starter_manifest()
    listed = [{"path": entry.path} for entry in manifest.files]
    listed.append({"path": "unexpected.ts"})

    with pytest.raises(StarterIntegrityError, match="starter workspace file set verification failed"):
        SOPRunner._verify_starter_workspace_file_set(manifest, listed)


def test_v2_opensandbox_copy_command_accepts_only_the_fixed_capability_enum() -> None:
    command = OpenSandboxProvider._starter_copy_command(
        "fomo-next-radix-v2", ("local-persistence", "crud")
    )

    assert command == (
        "cp -R --no-preserve=mode,ownership -- "
        "/opt/fomo/starters/fomo-next-radix-v2/base/. /workspace/ && "
        "test -L /workspace/node_modules && "
        "rm -- /workspace/node_modules && "
        "cp -a --no-preserve=ownership -- "
        "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules "
        "/workspace/node_modules && "
        "chmod -R u+rwX -- /workspace/node_modules && "
        "cp -R --no-preserve=mode,ownership -- "
        "/opt/fomo/starters/fomo-next-radix-v2/capabilities/crud/. /workspace/ && "
        "cp -R --no-preserve=mode,ownership -- "
        "/opt/fomo/starters/fomo-next-radix-v2/capabilities/local-persistence/. /workspace/"
    )
    with pytest.raises(ValueError, match="unsupported immutable starter"):
        OpenSandboxProvider._starter_copy_command("fomo-next-radix-v1", ())
    with pytest.raises(ValueError, match="unsupported starter capability"):
        OpenSandboxProvider._starter_copy_command("fomo-next-radix-v2", ("not-approved",))
    with pytest.raises(ValueError, match="duplicate starter capability"):
        OpenSandboxProvider._starter_copy_command("fomo-next-radix-v2", ("crud", "crud"))


def test_v2_validation_matrix_defines_but_does_not_execute_all_seed_variants() -> None:
    variants = starter_validation_variants()

    assert [(variant.name, variant.capability_ids) for variant in variants] == [
        ("bare", ()),
        ("crud", ("crud",)),
        ("local-persistence", ("local-persistence",)),
        ("crud-local-persistence", ("crud", "local-persistence")),
    ]
    assert all(
        variant.commands == ("pnpm typecheck", "pnpm build", "pnpm test:smoke")
        for variant in variants
    )
    assert all(
        "tests/harness/starter.smoke.spec.ts"
        in {entry.path for entry in resolve_starter_manifest(variant.capability_ids).files}
        for variant in variants
    )
