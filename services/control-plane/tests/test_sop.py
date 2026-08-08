from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from fomo.agent_runtime.llm import ModelError, ModelRequestError, ModelRetry, ScriptedModelClient
from fomo.agent_runtime.metagpt_adapter import (
    MetaGPTAdapter,
    MetaGPTAvailability,
    MetaGPTUnavailable,
)
from fomo.agent_runtime.sop import (
    ArtifactContractViolation,
    FileBatchContractViolation,
    SOPExecutionError,
    SOPRunner,
    _Context,
)
from fomo.agent_runtime.state import FailureRouter, SOPStateMachine
from fomo.sandbox.base import ExecResult, FileChange
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.sandbox.opensandbox import _OutputCollector
from fomo.schemas import (
    DiagnosticReport,
    FileBatchReport,
    GateResult,
    GateStatus,
    ImplementationBatchPlan,
    ImplementationPlan,
    ProductSpec,
    RunPhase,
    TechnicalSpec,
)
from fomo.starter import default_starter_manifest
from fomo.worker.runner import WorkerRunner


def _final_implementation_response(
    _model_alias: str,
    messages: list[dict[str, str]],
    _schema_name: str,
) -> dict[str, Any]:
    manifest_message = next(
        message["content"]
        for message in messages
        if message["content"].startswith("Baseline version:")
    )
    manifest = json.loads(manifest_message.split("Implementation batch manifest:\n", 1)[1])
    return {
        "baselineVersionId": None,
        "implementedAcceptanceIds": ["AC-1"],
        "designDecisionIds": ["DD-1"],
        "changedFiles": [path for batch in manifest for path in batch["paths"]],
        "commands": [],
        "knownLimitations": [],
        "candidateCommit": None,
        "batchArtifactIds": [batch["artifactId"] for batch in manifest],
        "fileChanges": [],
    }


def _engineer_cycle() -> list[dict[str, Any] | object]:
    return _two_batch_engineer_cycle()


def _two_batch_engineer_cycle() -> list[dict[str, Any] | object]:
    return [
        {
            "baselineVersionId": None,
            "batches": [
                {
                    "id": "library-page",
                    "purpose": "root route composition",
                    "paths": ["app/(generated)/composition.tsx"],
                    "acceptanceIds": ["AC-1"],
                },
                {
                    "id": "library-feature",
                    "purpose": "library interaction",
                    "paths": ["components/features/library.tsx"],
                    "acceptanceIds": ["AC-1"],
                },
            ],
            "designDecisionIds": ["DD-1"],
            "knownLimitations": [],
        },
        {
            "batchId": "library-page",
            "implementedAcceptanceIds": ["AC-1"],
            "designDecisionIds": ["DD-1"],
            "changedFiles": ["app/(generated)/composition.tsx"],
            "knownLimitations": [],
            "fileChanges": [
                {
                    "path": "app/(generated)/composition.tsx",
                    "operation": "modify",
                    "content": 'import { Library } from "@/components/features/library";\n\nexport function GeneratedComposition() {\n  return <Library />;\n}\n',
                }
            ],
        },
        {
            "batchId": "library-feature",
            "implementedAcceptanceIds": ["AC-1"],
            "designDecisionIds": ["DD-1"],
            "changedFiles": ["components/features/library.tsx"],
            "knownLimitations": [],
            "fileChanges": [
                {
                    "path": "components/features/library.tsx",
                    "operation": "create",
                    "content": 'export function Library() {\n  return <main>Library</main>;\n}\n',
                }
            ],
        },
        _final_implementation_response,
    ]


def _single_file_repair_engineer_cycle() -> list[dict[str, Any] | object]:
    return [
        {
            "baselineVersionId": None,
            "batches": [
                {
                    "id": "library-ui-repair",
                    "purpose": "repair the diagnosed library UI file",
                    "paths": ["components/features/library.tsx"],
                    "acceptanceIds": ["AC-1"],
                }
            ],
            "designDecisionIds": ["DD-1"],
            "knownLimitations": [],
        },
        {
            "batchId": "library-ui-repair",
            "implementedAcceptanceIds": ["AC-1"],
            "designDecisionIds": ["DD-1"],
            "changedFiles": ["components/features/library.tsx"],
            "knownLimitations": [],
            "fileChanges": [
                {
                    "path": "components/features/library.tsx",
                    "operation": "modify",
                    "content": 'export function Library() {\n  return <main>Library repaired</main>;\n}\n',
                }
            ],
        },
        _final_implementation_response,
    ]


def _responses() -> dict[str, Any]:
    return {
        "pm": {
            "title": "Library Manager",
            "problem": "Readers need to manage books.",
            "targetUsers": ["Librarian"],
            "userStories": [{"id": "US-1", "story": "Manage books", "priority": "must"}],
            "acceptanceCriteria": [
                {"id": "AC-1", "given": "books", "when": "adding", "then": "the list updates"}
            ],
            "pages": [{"route": "/", "purpose": "book management", "keyElements": ["form", "list"]}],
            "visualDirection": {"tone": "clean", "colors": ["blue"], "references": []},
            "assumptions": [],
            "outOfScope": [],
        },
        "architect": {
            "framework": "nextjs",
            "routes": [{"path": "/", "rendering": "client", "description": "library"}],
            "components": [
                {
                    "name": "Library",
                    "responsibility": "books",
                    "children": [],
                    "interactionResponsibilities": [],
                }
            ],
            "componentDecisions": [
                {
                    "component": "Button",
                    "strategy": "reuse",
                    "source": "shadcn/ui",
                    "rationale": "Use the maintained accessible primitive for common actions.",
                },
                {
                    "component": "Library",
                    "strategy": "custom",
                    "source": "application",
                    "rationale": "Its book-domain composition is specific to this product.",
                },
            ],
            "publicApiContracts": [
                {
                    "filePath": "app/(generated)/composition.tsx",
                    "exportStyle": "named",
                    "symbol": "GeneratedComposition",
                    "props": [],
                    "type": "React.ComponentType",
                },
                {
                    "filePath": "components/features/library.tsx",
                    "exportStyle": "named",
                    "symbol": "Library",
                    "props": [],
                    "type": "React.ComponentType",
                }
            ],
            "stateModel": [
                {
                    "name": "books",
                    "owner": "client",
                    "persistence": "local",
                    "stateClass": "persistent_business",
                    "mutableDomains": ["books"],
                }
            ],
            "dependencies": [],
            "filePlan": [
                {
                    "path": "app/(generated)/composition.tsx",
                    "operation": "modify",
                    "reason": "root route composition",
                },
                {
                    "path": "components/features/library.tsx",
                    "operation": "create",
                    "reason": "library interaction",
                },
            ],
            "testPlan": [{"acceptanceId": "AC-1", "method": "unit", "steps": ["add book"]}],
            "risks": [],
        },
        "engineer": _engineer_cycle(),
        "reviewer": {
            "gates": [],
            "acceptanceIds": ["AC-1"],
            "responsibleRole": "engineer",
            "blockingIssues": [],
            "evidence": [],
            "locationFiles": [],
            "suggestedFix": "",
            "screenshotReferences": [],
            "findings": [],
        },
    }


def _architect_response_with_playwright_smoke_path(
    path: str = "tests/generated/library.smoke.spec.ts",
    operation: str = "create",
) -> dict[str, Any]:
    response = _responses()["architect"]
    response["filePlan"].append(
        {
            "path": path,
            "operation": operation,
            "reason": "Playwright smoke coverage",
        }
    )
    response["testPlan"] = [{"acceptanceId": "AC-1", "method": "playwright", "steps": ["smoke"]}]
    return response


def _architect_response_with_file_plan_count(count: int) -> dict[str, Any]:
    response = dict(_responses()["architect"])
    response["filePlan"] = [
        {
            "path": f"components/features/generated/file-{index}.tsx",
            "operation": "create",
            "reason": "test-only file-plan capacity fixture",
        }
        for index in range(count)
    ]
    response["publicApiContracts"] = [
        {
            "filePath": "components/features/generated/file-0.tsx",
            "exportStyle": "named",
            "symbol": "GeneratedFile",
            "props": [],
            "type": "unknown",
        }
    ]
    return response


def _technical_for_reviewer_dependency_scope(
    paths: list[str],
    *,
    operations: dict[str, str] | None = None,
    public_api_contracts: list[dict[str, Any]] | None = None,
    feature_surfaces: list[dict[str, Any]] | None = None,
    persistent_state_domains: list[dict[str, Any]] | None = None,
    state_aggregation: dict[str, Any] | None = None,
) -> TechnicalSpec:
    response = _responses()["architect"]
    operations = operations or {}
    response["filePlan"] = [
        {
            "path": path,
            "operation": operations.get(path, "create"),
            "reason": "reviewer dependency-scope fixture",
        }
        for path in paths
    ]
    response["publicApiContracts"] = public_api_contracts or []
    response["featureSurfaces"] = feature_surfaces or []
    response["persistentStateDomains"] = persistent_state_domains or []
    response["stateAggregation"] = state_aggregation
    response["stateModel"] = []
    return TechnicalSpec.model_validate(response)


async def _derive_reviewer_dependency_scope(
    repository,
    settings,
    technical: TechnicalSpec,
    raw_paths: list[str],
    source_files: dict[str, str],
) -> tuple[SOPRunner, list[GateResult], Any]:
    sandbox = FakeSandboxProvider()
    ref = await sandbox.create("reviewer-dependency-scope")
    await sandbox.apply_changes(
        ref,
        [FileChange(path=path, content=content, operation="create") for path, content in source_files.items()],
    )
    runner = SOPRunner(repository, ScriptedModelClient({}), sandbox, settings)
    compiler_output = "\n".join(f"{path}(1,1): error" for path in raw_paths)
    gates = [
        GateResult(
            gate="typecheck",
            status=GateStatus.failed,
            summary="typecheck failed",
            affected_files=SOPRunner._affected_workspace_paths(compiler_output, ""),
        )
    ]
    scope = await runner._derive_reviewer_repair_scope(
        SimpleNamespace(sandbox=ref),
        technical,
        gates,
    )
    return runner, gates, scope


def _architect_response_with_system_managed_path(path: str) -> dict[str, Any]:
    response = dict(_responses()["architect"])
    response["filePlan"] = [
        {
            "path": path,
            "operation": "create",
            "reason": "test-only invalid system-managed file-plan fixture",
        }
    ]
    response["publicApiContracts"] = [
        {
            "filePath": path,
            "exportStyle": "named",
            "symbol": "SystemManagedFixture",
            "props": [],
            "type": "unknown",
        }
    ]
    return response


def _architect_response_with_domain_state_slices() -> dict[str, Any]:
    response = dict(_responses()["architect"])
    response["stateModel"] = [
        {
            "name": "LibraryData",
            "owner": "useLibraryStore",
            "persistence": "localStorage",
            "stateClass": "persistent_business",
            "mutableDomains": ["books", "readers", "loans"],
        },
        {
            "name": "BookFilters",
            "owner": "BookList",
            "persistence": "component state",
            "stateClass": "transient",
            "mutableDomains": [],
        },
        {
            "name": "LoanFormState",
            "owner": "LoanForm",
            "persistence": "component state",
            "stateClass": "transient",
            "mutableDomains": [],
        },
        {
            "name": "DerivedStatsAndStatuses",
            "owner": "selectors",
            "persistence": "computed",
            "stateClass": "derived",
            "mutableDomains": [],
        },
    ]
    response["persistentStateDomains"] = [
        {
            "domain": "books",
            "stateModelName": "LibraryData",
            "actionsStoreFile": "lib/domain/state/books-store.ts",
        },
        {
            "domain": "readers",
            "stateModelName": "LibraryData",
            "actionsStoreFile": "lib/domain/state/readers-store.ts",
        },
        {
            "domain": "loans",
            "stateModelName": "LibraryData",
            "actionsStoreFile": "lib/domain/state/loans-store.ts",
        },
    ]
    response["stateAggregation"] = {
        "filePath": "lib/domain/use-library-store.ts",
        "responsibilities": ["compose", "re_export"],
        "persistenceAdapter": {
            "filePath": "lib/domain/state/library-persistence-adapter.ts",
            "publicSymbol": "libraryPersistenceAdapter",
            "storageKey": "fomo.library",
            "schemaVersion": 1,
            "responsibilities": ["load", "save", "migrate"],
        },
    }
    response["publicApiContracts"] = [
        *response["publicApiContracts"],
        {
            "filePath": "lib/domain/state/library-persistence-adapter.ts",
            "exportStyle": "named",
            "symbol": "libraryPersistenceAdapter",
            "props": [],
            "type": "StatePersistenceAdapter",
        },
    ]
    response["filePlan"] = [
        *response["filePlan"],
        {"path": "lib/domain/state/books-store.ts", "operation": "create", "reason": "book mutations"},
        {"path": "lib/domain/state/readers-store.ts", "operation": "create", "reason": "reader mutations"},
        {"path": "lib/domain/state/loans-store.ts", "operation": "create", "reason": "loan and return mutations"},
        {
            "path": "lib/domain/use-library-store.ts",
            "operation": "create",
            "reason": "state composition and re-exports only",
        },
        {
            "path": "lib/domain/state/library-persistence-adapter.ts",
            "operation": "create",
            "reason": "durable storage loading, saving, and migration",
        },
    ]
    return response


def _architect_response_with_feature_surface_slices() -> dict[str, Any]:
    response = dict(_responses()["architect"])
    response["components"] = [
        {
            "name": "CatalogSurface",
            "responsibility": "compose a complex catalog management surface",
            "children": ["CatalogSearch", "CatalogFilters", "CatalogTable"],
            "interactionResponsibilities": ["search", "filter"],
        },
        {
            "name": "CatalogSearch",
            "responsibility": "render catalog query controls",
            "children": [],
            "interactionResponsibilities": [],
        },
        {
            "name": "CatalogFilters",
            "responsibility": "render catalog filter controls",
            "children": [],
            "interactionResponsibilities": [],
        },
        {
            "name": "CatalogTable",
            "responsibility": "render catalog data rows",
            "children": [],
            "interactionResponsibilities": [],
        },
    ]
    response["featureSurfaces"] = [
        {
            "componentName": "CatalogSurface",
            "compositionFile": "components/features/catalog/catalog-surface.tsx",
            "compositionSymbol": "CatalogSurface",
            "compositionResponsibilities": ["compose", "layout", "props"],
            "modules": [
                {
                    "role": "controller",
                    "filePath": "components/features/catalog/use-catalog-controller.ts",
                    "publicSymbol": "useCatalogController",
                },
                {
                    "role": "search",
                    "filePath": "components/features/catalog/catalog-search.tsx",
                    "publicSymbol": "CatalogSearch",
                },
                {
                    "role": "filter",
                    "filePath": "components/features/catalog/catalog-filters.tsx",
                    "publicSymbol": "CatalogFilters",
                },
            ],
        }
    ]
    response["filePlan"] = [
        *response["filePlan"],
        {
            "path": "components/features/catalog/catalog-surface.tsx",
            "operation": "create",
            "reason": "compose catalog surface only",
        },
        {
            "path": "components/features/catalog/use-catalog-controller.ts",
            "operation": "create",
            "reason": "catalog interaction state and callbacks",
        },
        {
            "path": "components/features/catalog/catalog-search.tsx",
            "operation": "create",
            "reason": "catalog query controls",
        },
        {
            "path": "components/features/catalog/catalog-filters.tsx",
            "operation": "create",
            "reason": "catalog filter controls",
        },
        {
            "path": "components/features/catalog/catalog-table.tsx",
            "operation": "create",
            "reason": "catalog data table",
        },
    ]
    response["publicApiContracts"] = [
        *response["publicApiContracts"],
        {
            "filePath": "components/features/catalog/catalog-surface.tsx",
            "exportStyle": "named",
            "symbol": "CatalogSurface",
            "props": [],
            "type": "React.ComponentType",
        },
        {
            "filePath": "components/features/catalog/use-catalog-controller.ts",
            "exportStyle": "named",
            "symbol": "useCatalogController",
            "props": [],
            "type": "() => CatalogController",
        },
        {
            "filePath": "components/features/catalog/catalog-search.tsx",
            "exportStyle": "named",
            "symbol": "CatalogSearch",
            "props": [],
            "type": "React.ComponentType",
        },
        {
            "filePath": "components/features/catalog/catalog-filters.tsx",
            "exportStyle": "named",
            "symbol": "CatalogFilters",
            "props": [],
            "type": "React.ComponentType",
        },
        {
            "filePath": "components/features/catalog/catalog-table.tsx",
            "exportStyle": "named",
            "symbol": "CatalogTable",
            "props": [],
            "type": "React.ComponentType",
        },
    ]
    return response


def _architect_response_with_three_concern_feature_surface_slices() -> dict[str, Any]:
    response = _architect_response_with_feature_surface_slices()
    response["components"][0]["interactionResponsibilities"].append("data_table")
    response["featureSurfaces"][0]["modules"].append(
        {
            "role": "data_table",
            "filePath": "components/features/catalog/catalog-table.tsx",
            "publicSymbol": "CatalogTable",
        }
    )
    return response


def _architect_response_with_four_concern_feature_surface() -> dict[str, Any]:
    response = _architect_response_with_three_concern_feature_surface_slices()
    response["components"][0]["children"].append("CatalogRowActions")
    response["components"][0]["interactionResponsibilities"].append("row_actions")
    response["components"].append(
        {
            "name": "CatalogRowActions",
            "responsibility": "render catalog row action controls",
            "children": [],
            "interactionResponsibilities": [],
        }
    )
    response["featureSurfaces"][0]["modules"].append(
        {
            "role": "row_actions",
            "filePath": "components/features/catalog/catalog-row-actions.tsx",
            "publicSymbol": "CatalogRowActions",
        }
    )
    response["filePlan"].append(
        {
            "path": "components/features/catalog/catalog-row-actions.tsx",
            "operation": "create",
            "reason": "catalog row action controls",
        }
    )
    response["publicApiContracts"].append(
        {
            "filePath": "components/features/catalog/catalog-row-actions.tsx",
            "exportStyle": "named",
            "symbol": "CatalogRowActions",
            "props": [],
            "type": "React.ComponentType",
        }
    )
    return response


def _architect_response_with_split_feature_surface_slices() -> dict[str, Any]:
    response = _architect_response_with_feature_surface_slices()
    response["components"].insert(
        0,
        {
            "name": "CatalogShell",
            "responsibility": "compose catalog discovery and operations surfaces only",
            "children": ["CatalogSurface", "CatalogOperationsSurface"],
            "interactionResponsibilities": [],
        },
    )
    response["components"].extend(
        [
            {
                "name": "CatalogOperationsSurface",
                "responsibility": "compose bounded catalog workflow controls",
                "children": ["CatalogRowActions", "CatalogConfirmation"],
                "interactionResponsibilities": ["row_actions", "confirmation"],
            },
            {
                "name": "CatalogRowActions",
                "responsibility": "render catalog row action controls",
                "children": [],
                "interactionResponsibilities": [],
            },
            {
                "name": "CatalogConfirmation",
                "responsibility": "render catalog confirmation controls",
                "children": [],
                "interactionResponsibilities": [],
            },
        ]
    )
    response["featureSurfaces"].append(
        {
            "componentName": "CatalogOperationsSurface",
            "compositionFile": "components/features/catalog/catalog-operations-surface.tsx",
            "compositionSymbol": "CatalogOperationsSurface",
            "compositionResponsibilities": ["compose", "layout", "props"],
            "modules": [
                {
                    "role": "controller",
                    "filePath": "components/features/catalog/use-catalog-operations-controller.ts",
                    "publicSymbol": "useCatalogOperationsController",
                },
                {
                    "role": "row_actions",
                    "filePath": "components/features/catalog/catalog-row-actions.tsx",
                    "publicSymbol": "CatalogRowActions",
                },
                {
                    "role": "confirmation",
                    "filePath": "components/features/catalog/catalog-confirmation.tsx",
                    "publicSymbol": "CatalogConfirmation",
                },
            ],
        }
    )
    response["filePlan"].extend(
        [
            {
                "path": "components/features/catalog/catalog-shell.tsx",
                "operation": "create",
                "reason": "top-level catalog composition only",
            },
            {
                "path": "components/features/catalog/catalog-operations-surface.tsx",
                "operation": "create",
                "reason": "compose bounded catalog workflow controls",
            },
            {
                "path": "components/features/catalog/use-catalog-operations-controller.ts",
                "operation": "create",
                "reason": "catalog workflow coordination only",
            },
            {
                "path": "components/features/catalog/catalog-row-actions.tsx",
                "operation": "create",
                "reason": "catalog row action controls",
            },
            {
                "path": "components/features/catalog/catalog-confirmation.tsx",
                "operation": "create",
                "reason": "catalog confirmation controls",
            },
        ]
    )
    response["publicApiContracts"].extend(
        [
            {
                "filePath": "components/features/catalog/catalog-shell.tsx",
                "exportStyle": "named",
                "symbol": "CatalogShell",
                "props": [],
                "type": "React.ComponentType",
            },
            {
                "filePath": "components/features/catalog/catalog-operations-surface.tsx",
                "exportStyle": "named",
                "symbol": "CatalogOperationsSurface",
                "props": [],
                "type": "React.ComponentType",
            },
            {
                "filePath": "components/features/catalog/use-catalog-operations-controller.ts",
                "exportStyle": "named",
                "symbol": "useCatalogOperationsController",
                "props": [],
                "type": "() => CatalogOperationsController",
            },
            {
                "filePath": "components/features/catalog/catalog-row-actions.tsx",
                "exportStyle": "named",
                "symbol": "CatalogRowActions",
                "props": [],
                "type": "React.ComponentType",
            },
            {
                "filePath": "components/features/catalog/catalog-confirmation.tsx",
                "exportStyle": "named",
                "symbol": "CatalogConfirmation",
                "props": [],
                "type": "React.ComponentType",
            },
        ]
    )
    return response


def test_artifact_contract_violation_uses_a_closed_repair_instruction_map() -> None:
    violation = ArtifactContractViolation(code="technical_spec.file_plan.system_managed")

    assert violation.repair_instruction == "Remove system-managed files from TechnicalSpec.filePlan."
    with pytest.raises(ValueError, match="unknown artifact contract violation code") as unknown_code:
        ArtifactContractViolation(code="untrusted.contract.code")
    assert "untrusted.contract.code" not in str(unknown_code.value)
    with pytest.raises(TypeError):
        ArtifactContractViolation(
            code="technical_spec.file_plan.system_managed",
            repair_instruction="untrusted instruction",  # type: ignore[call-arg]
        )


def test_file_batch_contract_violation_uses_a_closed_repair_instruction_map() -> None:
    violation = FileBatchContractViolation(
        code="file_batch_report.file_size_exceeded",
        hard_limit_characters=20_000,
        max_observed_characters=20_001,
        oversized_file_count=1,
    )

    assert violation.repair_instruction == (
        "Keep every create or modify fileChanges content at or below the hard character limit stated in the prompt."
    )
    assert violation.hard_limit_characters == 20_000
    assert violation.max_observed_characters == 20_001
    assert violation.oversized_file_count == 1
    with pytest.raises(AttributeError):
        violation.hard_limit_characters = 1  # type: ignore[misc]
    with pytest.raises(ValueError, match="unknown file batch contract violation code") as unknown_code:
        FileBatchContractViolation(code="untrusted.contract.code")
    assert "untrusted.contract.code" not in str(unknown_code.value)
    with pytest.raises(TypeError):
        FileBatchContractViolation(
            code="file_batch_report.file_size_exceeded",
            repair_instruction="untrusted instruction",  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        FileBatchContractViolation(
            code="file_batch_report.file_size_exceeded",
            hard_limit_characters=20_000,
            max_observed_characters=20_001,
            oversized_file_count=1,
            details={"untrusted": "metadata"},  # type: ignore[call-arg]
        )


def test_default_starter_manifest_is_digest_pinned_and_exposes_approved_primitives() -> None:
    manifest = default_starter_manifest()

    assert manifest.id == "fomo-next-radix-v2"
    assert manifest.version == "2.0.0"
    assert manifest.tree_sha256 == "acae2845e79415b6c75a5aeee6057a63333ceddfd7f55921c32a02615832302a"
    assert "@/components/ui/button" in manifest.available_imports
    assert "@/components/ui/card" in manifest.available_imports
    assert "package.json" in manifest.protected_paths
    assert "playwright.config.ts" in manifest.protected_paths
    assert "components/features/**" in manifest.model_owned_roots
    assert "app/page.tsx" in manifest.protected_paths
    assert "tests/generated/**" in manifest.model_owned_roots
    assert manifest.base_scripts["test:smoke"] == "playwright test --project=chromium"
    manifest.verify_tree(
        {change.path: change.content.encode("utf-8") for change in manifest.file_changes}
    )


@pytest.mark.asyncio
async def test_sandbox_bootstrap_copies_verified_starter_before_initial_commit(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Starter bootstrap")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "starter-bootstrap", "Create a book management system"
    )
    claimed = await repository.claim_next_run("test-worker", 60)
    assert claimed is not None and claimed.id == run.id
    sandbox = FakeSandboxProvider()
    runner = SOPRunner(repository, ScriptedModelClient({}), sandbox, settings)
    context = SimpleNamespace(
        run_id=run.id,
        project_id=project.id,
        lease_token=await repository.get_active_lease_token(run.id),
        sandbox=None,
    )

    await runner._create_sandbox(context)

    assert context.sandbox is not None
    workspace = sandbox.sandboxes[context.sandbox.id]
    expected_files = {change.path: change.content.encode("utf-8") for change in runner.starter.file_changes}
    assert {path: workspace.files[path] for path in expected_files} == expected_files
    assert workspace.files[".gitignore"] == (
        b"# FOMO system safety baseline\nnode_modules/\n.next/\ndist/\nbuild/\ncoverage/\n"
        b"playwright-report/\ntest-results/\nblob-report/\n*.log\n.env\n.env.*\n"
    )
    assert workspace.commands == [
        "git init && git config user.email fomo@local.invalid && git config user.name 'FOMO Agent'",
        "git add -A && git commit -m 'chore(starter): fomo-next-radix-v2@2.0.0'",
        "git rev-parse HEAD",
    ]
    provenance = await repository.get_latest_artifact(run.id, "starter_provenance")
    assert provenance is not None
    assert provenance["treeSha256"] == runner.starter.tree_sha256
    assert provenance["initialCommitSha"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report_payload", "expected_payload", "settings_overrides", "code"),
    [
        pytest.param(
            {
                "batchId": "wrong-batch",
                "fileChanges": [{"path": "components/features/book.tsx", "content": "export {}"}],
            },
            {"id": "requested-batch", "purpose": "test", "paths": ["components/features/book.tsx"]},
            {},
            "file_batch_report.batch_id_mismatch",
            id="batch-id-mismatch",
        ),
        pytest.param(
            {"batchId": "requested-batch", "fileChanges": []},
            {"id": "requested-batch", "purpose": "test", "paths": ["components/features/book.tsx"]},
            {},
            "file_batch_report.file_changes_empty",
            id="empty-file-changes",
        ),
        pytest.param(
            {
                "batchId": "requested-batch",
                "fileChanges": [
                    {"path": "components/features/book.tsx", "content": "first"},
                    {"path": "components/features/book.tsx", "content": "second"},
                ],
            },
            {"id": "requested-batch", "purpose": "test", "paths": ["components/features/book.tsx"]},
            {},
            "file_batch_report.paths_duplicate",
            id="duplicate-paths",
        ),
        pytest.param(
            {
                "batchId": "requested-batch",
                "fileChanges": [{"path": "components/features/other.tsx", "content": "export {}"}],
            },
            {"id": "requested-batch", "purpose": "test", "paths": ["components/features/book.tsx"]},
            {},
            "file_batch_report.paths_mismatch",
            id="paths-mismatch",
        ),
        pytest.param(
            {
                "batchId": "requested-batch",
                "fileChanges": [{"path": "../outside.ts", "content": "export {}"}],
            },
            {"id": "requested-batch", "purpose": "test", "paths": ["components/features/book.tsx"]},
            {},
            "file_batch_report.workspace_path_invalid",
            id="invalid-workspace-path-before-path-mismatch",
        ),
        pytest.param(
            {
                "batchId": "requested-batch",
                "fileChanges": [{"path": "components/features/book.tsx", "content": "too-long"}],
            },
            {"id": "requested-batch", "purpose": "test", "paths": ["components/features/book.tsx"]},
            {"engineer_max_file_characters": 2},
            "file_batch_report.file_size_exceeded",
            id="file-size-exceeded",
        ),
    ],
)
async def test_file_batch_contract_validation_uses_stable_codes(
    repository,
    settings,
    report_payload,
    expected_payload,
    settings_overrides,
    code,
) -> None:
    runner = SOPRunner(
        repository,
        ScriptedModelClient({}),
        FakeSandboxProvider(),
        replace(settings, **settings_overrides),
    )
    report = FileBatchReport.model_validate(report_payload)
    expected = ImplementationBatchPlan.model_validate(expected_payload)

    with pytest.raises(FileBatchContractViolation) as violation:
        runner._validate_file_batch_report(report, expected)
    assert violation.value.code == code


@pytest.mark.asyncio
async def test_file_batch_character_limit_accepts_hard_limit_and_rejects_only_larger_content(
    repository, settings
) -> None:
    runner = SOPRunner(
        repository,
        ScriptedModelClient({}),
        FakeSandboxProvider(),
        replace(
            settings,
            engineer_target_file_characters=12_000,
            engineer_max_file_characters=20_000,
        ),
    )
    expected = ImplementationBatchPlan.model_validate(
        {"id": "requested-batch", "purpose": "test", "paths": ["components/features/book.tsx"]}
    )
    at_hard_limit = FileBatchReport.model_validate(
        {
            "batchId": "requested-batch",
            "fileChanges": [{"path": "components/features/book.tsx", "content": "x" * 20_000}],
        }
    )

    runner._validate_file_batch_report(at_hard_limit, expected)

    over_hard_limit = FileBatchReport.model_validate(
        {
            "batchId": "requested-batch",
            "fileChanges": [{"path": "components/features/book.tsx", "content": "x" * 20_001}],
        }
    )
    with pytest.raises(FileBatchContractViolation) as violation:
        runner._validate_file_batch_report(over_hard_limit, expected)

    assert violation.value.code == "file_batch_report.file_size_exceeded"
    assert violation.value.hard_limit_characters == 20_000
    assert violation.value.max_observed_characters == 20_001
    assert violation.value.oversized_file_count == 1


def _repair_responses() -> dict[str, Any]:
    responses = _responses()
    responses["engineer"] = [*responses["engineer"], *_single_file_repair_engineer_cycle()]
    initial_reviewer = dict(responses["reviewer"])
    initial_reviewer["locationFiles"] = ["components/features/library.tsx"]
    responses["reviewer"] = [initial_reviewer, responses["reviewer"]]
    return responses


class _RetryReportingScriptedModelClient(ScriptedModelClient):
    def __init__(self, responses) -> None:
        super().__init__(responses)
        self._reported_retry = False

    async def complete_json(self, model_alias, messages, schema_name, *, on_retry=None):
        if not self._reported_retry and on_retry is not None:
            self._reported_retry = True
            await on_retry(
                ModelRetry(
                    attempt=1,
                    max_attempts=3,
                    delay_seconds=0.5,
                    failure_kind="gateway_status",
                    status_code=504,
                )
            )
        return await super().complete_json(model_alias, messages, schema_name, on_retry=on_retry)


class _PermissionAwareGitignoreSandbox(FakeSandboxProvider):
    """Fake shell behavior for a provider that rejects an in-place overwrite."""

    def __init__(self) -> None:
        super().__init__()
        self.gitignore_write_attempts = 0
        self.recovery_commands: list[str] = []
        self._overwrite_requires_reset = False

    async def apply_changes(self, ref, changes) -> None:
        if any(change.path == ".gitignore" and change.operation != "delete" for change in changes):
            self.gitignore_write_attempts += 1
            if self._overwrite_requires_reset:
                raise PermissionError("simulated overwrite permission denied")
        await super().apply_changes(ref, changes)

    async def exec(self, ref, command, sink) -> ExecResult:
        if "rm -f -- .gitignore" in command.command:
            self.recovery_commands.append(command.command)
            self._sandbox(ref).files.pop(".gitignore", None)
            self._overwrite_requires_reset = False
            return ExecResult(0, "", "")
        return await super().exec(ref, command, sink)

    def tamper_gitignore(self, ref) -> None:
        self._sandbox(ref).files[".gitignore"] = b"model-supplied-content\n"
        self._overwrite_requires_reset = True


def test_metagpt_selected_without_extra_fails_instead_of_using_native(monkeypatch) -> None:
    monkeypatch.setattr(
        MetaGPTAdapter,
        "availability",
        staticmethod(
            lambda: MetaGPTAvailability(
                available=False,
                reason="AGENT_FRAMEWORK=metagpt requires the optional MetaGPT extra. "
                "Install it with: uv sync --extra metagpt --extra dev.",
            )
        ),
    )
    with pytest.raises(MetaGPTUnavailable, match="uv sync --extra metagpt --extra dev"):
        MetaGPTAdapter(ScriptedModelClient({}))


def test_pinned_metagpt_classes_import_with_a_temporary_safe_config(tmp_path) -> None:
    """Prove bare MetaGPT class imports work before FOMO builds a worker."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config2.yaml").write_text(
        """llm:
  api_type: openai
  model: fomo-test-coordination-placeholder
  base_url: http://127.0.0.1:9/v1
  api_key: fomo-test-placeholder-not-used
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "ANTHROPIC_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment.pop(name, None)
    environment["METAGPT_PROJECT_ROOT"] = str(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from metagpt.actions import Action; from metagpt.roles import Role; "
            "from metagpt.schema import Message; "
            "print(Action.__module__, Role.__module__, Message.__module__)",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "metagpt.actions.action metagpt.roles.role metagpt.schema"


@pytest.mark.asyncio
async def test_four_role_sop_creates_version_and_trace(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-1", "Create a book management system"
    )
    claimed = await repository.claim_next_run("test-worker", 60)
    assert claimed is not None and claimed.id == run.id
    responses = _responses()
    responses["engineer"] = _two_batch_engineer_cycle()
    model = ScriptedModelClient(responses)
    runner = SOPRunner(repository, model, FakeSandboxProvider(), settings)

    await runner.run(run.id)

    final = await repository.get_run(run.id)
    assert final.status.value == "succeeded"
    assert [alias for alias, _schema in model.calls] == [
        "pm",
        "architect",
        "engineer",
        "engineer",
        "engineer",
        "engineer",
        "reviewer",
    ]
    versions = await repository.list_versions(project.id)
    assert len(versions) == 1
    assert {item["path"] for item in await repository.list_version_files(project.id)} >= {
        ".gitignore",
        "app/(generated)/composition.tsx",
        "components/features/library.tsx",
    }
    _version_id, gitignore, _sha256 = await repository.get_version_file_content(project.id, ".gitignore")
    assert "node_modules/" in gitignore
    assert "playwright-report/" in gitignore
    assert "unsafe-generated-ignore" not in gitignore
    implementation = await repository.get_latest_artifact(run.id, "implementation_report")
    assert implementation is not None and implementation["candidateCommit"] == "ok"
    assert implementation["fileChanges"] == []
    assert len(implementation["batchArtifactIds"]) == 2
    assert await repository.get_latest_artifact(run.id, "implementation_plan") is not None
    assert await repository.get_latest_artifact(run.id, "implementation_batch") is not None
    starter_provenance = await repository.get_latest_artifact(run.id, "starter_provenance")
    assert starter_provenance is not None
    assert starter_provenance["id"] == "fomo-next-radix-v2"
    assert starter_provenance["version"] == "2.0.0"
    assert starter_provenance["initialCommitSha"] == "ok"
    assert {entry["path"] for entry in starter_provenance["files"]} >= {
        "components/ui/button.tsx",
        "components/ui/card.tsx",
        "playwright.config.ts",
        "pnpm-lock.yaml",
    }
    trace = await repository.get_trace(project.id, run.id)
    assert trace["links"]
    events = await repository.list_events(run.id)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert any(event.kind == "version.created" for event in events)
    assert any(
        event.kind == "agent.activity" and event.payload.get("action") == "implementation_batch_persisted"
        for event in events
    )
    architect_request = next(request for request in model.requests if request[0] == "architect")
    architect_system_prompt = architect_request[1][0]["content"]
    assert "literal enum values" in architect_system_prompt
    assert "concise" in architect_system_prompt
    assert "24 Engineer batches of at most 1 unique valid relative workspace path each" in architect_system_prompt
    assert "no more than 24 paths total" in architect_system_prompt
    assert "An immutable starter already exists" in architect_system_prompt
    assert "StarterManifest protectedPaths" in architect_system_prompt
    assert "app/(generated)/**" in architect_system_prompt
    assert "components/features/**" in architect_system_prompt
    assert "lib/domain/**" in architect_system_prompt
    assert "0.0.0.0" in architect_system_prompt
    assert "http://127.0.0.1:<port>" in architect_system_prompt
    assert "dynamic hostname" in architect_system_prompt
    assert "React + TypeScript + Tailwind CSS + shadcn/ui + Lucide React" in architect_system_prompt
    assert "componentDecisions" in architect_system_prompt
    assert "decision-bearing UI primitives" in architect_system_prompt
    assert "not a component inventory" in architect_system_prompt
    assert "without requiring a same-name decision" in architect_system_prompt
    assert "Avoid per-control Button/Card/Input decisions" in architect_system_prompt
    assert "only for actual cross-file public symbols" in architect_system_prompt
    assert "12000-character target" in architect_system_prompt
    assert "20000-character hard limit" in architect_system_prompt
    assert "only rejection threshold" in architect_system_prompt
    assert "split complex features across files" in architect_system_prompt
    assert "publicApiContracts" in architect_system_prompt
    assert "interactionResponsibilities" in architect_system_prompt
    assert "featureSurfaces" in architect_system_prompt
    assert "two or more declared concerns" in architect_system_prompt
    assert "separate controller module" in architect_system_prompt
    assert "at most 3 declared concerns" in architect_system_prompt
    assert "top-level aggregate must use []" in architect_system_prompt
    assert "without duplicating CRUD logic or UI markup" in architect_system_prompt
    assert "Do not infer or merge UI responsibilities from product-domain names" in architect_system_prompt
    assert "persistent_business, transient, or derived" in architect_system_prompt
    assert "mutableDomains" in architect_system_prompt
    assert "persistentStateDomains" in architect_system_prompt
    assert "independent CRUD lifecycle" in architect_system_prompt
    assert "other umbrella domain" in architect_system_prompt
    assert "exactly compose and re_export" in architect_system_prompt
    assert "storage I/O" in architect_system_prompt
    assert "persistenceAdapter" in architect_system_prompt
    assert "load, save, and migrate" in architect_system_prompt
    assert "must not perform CRUD or UI work" in architect_system_prompt
    assert "pairwise different" in architect_system_prompt
    engineer_plan_request = next(request for request in model.requests if request[2] == "ImplementationPlan")
    engineer_plan_system_prompt = engineer_plan_request[1][0]["content"]
    assert "at most 1 relative file, with at most 24 batches" in engineer_plan_system_prompt
    assert "no more than 24 files total" in engineer_plan_system_prompt
    assert "TechnicalSpec.filePlan path must appear exactly once" in engineer_plan_system_prompt
    assert "0.0.0.0" in engineer_plan_system_prompt
    assert "http://127.0.0.1:<port>" in engineer_plan_system_prompt
    assert "dynamic hostname" in engineer_plan_system_prompt
    assert "React + TypeScript + Tailwind CSS + shadcn/ui + Lucide React" in engineer_plan_system_prompt
    assert "publicApiContracts" in engineer_plan_system_prompt
    assert "immutable starter already provides package configuration" in engineer_plan_system_prompt
    assert "12000-character target" in engineer_plan_system_prompt
    assert "20000-character hard limit" in engineer_plan_system_prompt
    assert "only rejection threshold" in engineer_plan_system_prompt
    assert "each controller coordinates only its own at-most-three declared concerns" in engineer_plan_system_prompt
    assert "without duplicating CRUD logic or UI markup" in engineer_plan_system_prompt
    engineer_batch_requests = [request for request in model.requests if request[2] == "FileBatchReport"]
    assert len(engineer_batch_requests) == 2
    engineer_batch_system_prompt = engineer_batch_requests[0][1][0]["content"]
    assert "0.0.0.0" in engineer_batch_system_prompt
    assert "http://127.0.0.1:<port>" in engineer_batch_system_prompt
    assert "dynamic hostname" in engineer_batch_system_prompt
    assert "React + TypeScript + Tailwind CSS + shadcn/ui + Lucide React" in engineer_batch_system_prompt
    assert "Write only StarterManifest modelOwnedRoots" in engineer_batch_system_prompt
    assert "12000-character target" in engineer_batch_system_prompt
    assert "20000-character hard limit" in engineer_batch_system_prompt
    assert "only rejection threshold" in engineer_batch_system_prompt
    assert "each controller coordinates only its own at-most-three declared concerns" in engineer_batch_system_prompt
    assert "without duplicating CRUD logic or UI markup" in engineer_batch_system_prompt
    for engineer_batch_request in engineer_batch_requests:
        assert "Shared public API contracts" in engineer_batch_request[1][1]["content"]
        assert "StarterManifest:" in engineer_batch_request[1][1]["content"]
        assert '"symbol":"Library"' in engineer_batch_request[1][1]["content"]

    # On refresh a finished run remains non-active, but its whole visible
    # workbench trace is still available for the client to reconstruct.
    terminal_snapshot = await repository.get_project_snapshot(project.id, session.id)
    assert terminal_snapshot["active_run"] is None
    assert terminal_snapshot["last_seq"] == events[-1].seq
    assert [event.seq for event in terminal_snapshot["events"]] == [event.seq for event in events]
    assert {
        event.role for event in terminal_snapshot["events"] if event.kind == "agent.started"
    } == {"product_manager", "architect", "engineer", "reviewer"}
    assert any(event.kind == "command.completed" and event.role == "engineer" for event in terminal_snapshot["events"])


@pytest.mark.asyncio
async def test_architect_prompt_uses_configured_file_character_thresholds(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-file-character-limit", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    model = ScriptedModelClient(_responses())

    await SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(
            settings,
            engineer_target_file_characters=1234,
            engineer_max_file_characters=4321,
        ),
    ).run(run.id)

    architect_request = next(request for request in model.requests if request[0] == "architect")
    architect_system_prompt = architect_request[1][0]["content"]
    assert "1234-character target" in architect_system_prompt
    assert "4321-character hard limit" in architect_system_prompt


@pytest.mark.asyncio
async def test_system_gitignore_is_idempotent_and_recovers_a_permission_protected_tamper(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-gitignore", "Create a book management system"
    )
    claimed = await repository.claim_next_run("test-worker", 60)
    assert claimed is not None and claimed.id == run.id
    sandbox = _PermissionAwareGitignoreSandbox()
    ref = await sandbox.create(project.id)
    context = SimpleNamespace(
        run_id=run.id,
        sandbox=ref,
        lease_token=await repository.get_active_lease_token(run.id),
    )
    runner = SOPRunner(repository, ScriptedModelClient({}), sandbox, settings)

    await runner._ensure_system_gitignore(context)
    baseline = await sandbox.read_file(ref, ".gitignore")
    assert sandbox.gitignore_write_attempts == 1

    await runner._ensure_system_gitignore(context)
    assert sandbox.gitignore_write_attempts == 1
    assert sandbox.recovery_commands == []

    sandbox.tamper_gitignore(ref)
    await runner._ensure_system_gitignore(context)
    assert await sandbox.read_file(ref, ".gitignore") == baseline
    assert sandbox.gitignore_write_attempts == 2
    assert len(sandbox.recovery_commands) == 1
    assert "model-supplied-content" not in sandbox.recovery_commands[0]


@pytest.mark.asyncio
async def test_engineer_plan_must_exactly_cover_architect_file_plan(repository, settings) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_responses()["architect"])

    missing_path_plan = ImplementationPlan.model_validate(
        {
            "batches": [
                {
                    "id": "page",
                    "purpose": "only one planned model-owned file",
                    "paths": ["app/(generated)/composition.tsx"],
                },
            ]
        }
    )
    with pytest.raises(ValueError, match="exactly match the architect TechnicalSpec file plan"):
        runner._validate_implementation_plan(missing_path_plan, technical)

    overfull_batch_plan = ImplementationPlan.model_validate(
        {
            "batches": [
                {
                    "id": "overfull",
                    "purpose": "violates the bounded batch contract",
                    "paths": ["app/(generated)/composition.tsx", "components/features/library.tsx"],
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="batch exceeds the configured file limit"):
        runner._validate_implementation_plan(overfull_batch_plan, technical)

    extra_path_plan = ImplementationPlan.model_validate(
        {
            "batches": [
                {
                    "id": "page",
                    "purpose": "initial files",
                    "paths": ["app/(generated)/composition.tsx"],
                },
                {
                    "id": "feature",
                    "purpose": "library application",
                    "paths": ["components/features/library.tsx"],
                },
                {
                    "id": "extra",
                    "purpose": "not authorized by the Architect",
                    "paths": ["components/features/extra.tsx"],
                },
            ]
        }
    )
    with pytest.raises(ValueError, match="exactly match the architect TechnicalSpec file plan"):
        runner._validate_implementation_plan(extra_path_plan, technical)


@pytest.mark.asyncio
async def test_repair_scope_rejects_an_unrelated_full_rewrite(repository, settings) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_responses()["architect"])
    diagnostic = DiagnosticReport(location_files=["components/features/library.tsx"])

    scoped_technical = runner._repair_technical(technical, diagnostic)

    assert [item.path for item in scoped_technical.file_plan] == ["components/features/library.tsx"]
    with pytest.raises(ValueError, match="exactly match the architect TechnicalSpec file plan"):
        runner._validate_implementation_plan(
            ImplementationPlan.model_validate(_two_batch_engineer_cycle()[0]),
            scoped_technical,
        )
    with pytest.raises(SOPExecutionError, match="did not identify an approved file scope"):
        runner._repair_technical(technical, DiagnosticReport())
    with pytest.raises(SOPExecutionError, match="invalid file scope"):
        runner._repair_technical(technical, DiagnosticReport(location_files=["../outside.tsx"]))
    with pytest.raises(SOPExecutionError, match="unapproved file scope"):
        runner._repair_technical(
            technical,
            DiagnosticReport(location_files=["components/features/unplanned.tsx"]),
        )
    finding_expansion = runner._repair_technical(
        technical,
        DiagnosticReport.model_validate(
            {
                "locationFiles": ["components/features/library.tsx"],
                "findings": [
                    {
                        "severity": "error",
                        "message": "untrusted finding cannot expand repair scope",
                        "file": "app/(generated)/composition.tsx",
                    }
                ],
            }
        ),
    )
    assert [item.path for item in finding_expansion.file_plan] == ["components/features/library.tsx"]


@pytest.mark.asyncio
async def test_repair_scope_does_not_offer_immutable_package_files_for_dependency_gates(
    repository, settings
) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_responses()["architect"])
    diagnostic = DiagnosticReport(
        gates=[GateResult(gate="dependencies", status=GateStatus.failed, summary="lockfile mismatch")]
    )

    with pytest.raises(SOPExecutionError, match="did not identify an approved file scope"):
        runner._repair_technical(technical, diagnostic)


@pytest.mark.asyncio
async def test_repair_scope_caps_full_diagnostic_scope_and_preserves_file_plan_order(
    repository, settings
) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_architect_response_with_file_plan_count(9))

    small_scope = runner._repair_technical(
        technical,
        DiagnosticReport(
            location_files=[
                "components/features/generated/file-7.tsx",
                "components/features/generated/file-2.tsx",
            ]
        ),
    )

    assert [item.path for item in small_scope.file_plan] == [
        "components/features/generated/file-2.tsx",
        "components/features/generated/file-7.tsx",
    ]
    with pytest.raises(SOPExecutionError, match="exceeds the maximum of 8 approved files"):
        runner._repair_technical(
            technical,
            DiagnosticReport(location_files=[item.path for item in technical.file_plan]),
        )


@pytest.mark.asyncio
async def test_reviewer_scope_fails_closed_when_deterministic_union_exceeds_capacity(
    repository, settings
) -> None:
    root = "lib/domain/root.ts"
    dependencies = [f"lib/domain/dependency-{index}.ts" for index in range(8)]
    technical = _technical_for_reviewer_dependency_scope([root, *dependencies])
    source_files = {
        root: "\n".join(
            f'import {{ dependency{index} }} from "./dependency-{index}";'
            for index in range(8)
        ),
        **{
            path: f"export const dependency{index} = {index};"
            for index, path in enumerate(dependencies)
        },
    }

    with pytest.raises(SOPExecutionError, match="exceeds the maximum of 8"):
        await _derive_reviewer_dependency_scope(
            repository,
            settings,
            technical,
            [root],
            source_files,
        )


@pytest.mark.asyncio
async def test_verify_reviewer_scope_contract_retries_without_detail_leak(
    repository, settings, monkeypatch
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "reviewer-scope-verify", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    sandbox = FakeSandboxProvider()
    ref = await sandbox.create(project.id)
    context = _Context(
        run_id=run.id,
        project_id=project.id,
        base_version_id=None,
        phase=RunPhase.implementation,
        prompt="Create a book management system",
        lease_token=await repository.get_active_lease_token(run.id),
        sandbox=ref,
    )
    technical = TechnicalSpec.model_validate(_architect_response_with_file_plan_count(9))
    planned_paths = [item.path for item in technical.file_plan]
    source_marker = "reviewer-verify-untrusted-marker"
    invalid_report = dict(_responses()["reviewer"])
    invalid_report.update(
        {
            "blockingIssues": [source_marker],
            "locationFiles": planned_paths,
        }
    )
    corrected_report = dict(_responses()["reviewer"])
    corrected_report.update(
        {
            "blockingIssues": ["typecheck failed"],
            "locationFiles": [planned_paths[0]],
        }
    )
    model = ScriptedModelClient({"reviewer": [invalid_report, corrected_report]})
    runner = SOPRunner(
        repository,
        model,
        sandbox,
        replace(settings, structured_output_retries=1),
    )

    async def failed_quality_gates(_context) -> list[GateResult]:
        return [
            GateResult(
                gate="typecheck",
                status=GateStatus.failed,
                summary="failed",
                affected_files=[planned_paths[0]],
            )
        ]

    monkeypatch.setattr(runner, "_run_quality_gates", failed_quality_gates)

    report = await runner._verify(
        context,
        ProductSpec.model_validate(_responses()["pm"]),
        technical,
    )

    assert report.location_files == [planned_paths[0]]
    assert [gate.gate for gate in report.gates] == ["typecheck"]
    retry_event = next(
        event
        for event in await repository.list_events(run.id)
        if event.kind == "agent.activity" and event.payload.get("action") == "structured_retry"
    )
    assert retry_event.payload["reasonCode"] == "diagnostic_report.location_files.capacity_exceeded"
    correction = next(
        message["content"]
        for message in model.requests[1][1]
        if message["content"].startswith("Return only a valid DiagnosticReport JSON object")
    )
    assert source_marker not in correction
    assert planned_paths[0] not in correction
    assert source_marker not in json.dumps(
        [event.payload for event in await repository.list_events(run.id)]
    )


@pytest.mark.asyncio
async def test_reviewer_scope_contract_variants_retry_closed_without_detail_leak(
    repository, settings
) -> None:
    technical = TechnicalSpec.model_validate(_architect_response_with_file_plan_count(9))
    planned_paths = [item.path for item in technical.file_plan]
    source_marker = "reviewer-contract-untrusted-marker"
    invalid_cases = [
        (
            "capacity",
            {"locationFiles": planned_paths},
            "diagnostic_report.location_files.capacity_exceeded",
        ),
        (
            "duplicate",
            {"locationFiles": [planned_paths[0], planned_paths[0]]},
            "diagnostic_report.location_files.duplicate",
        ),
        (
            "finding-extension",
            {
                "locationFiles": [planned_paths[0]],
                "findings": [
                    {
                        "severity": "error",
                        "message": source_marker,
                        "file": planned_paths[1],
                    }
                ],
            },
            "diagnostic_report.findings.file_out_of_scope",
        ),
        (
            "invalid-path",
            {"locationFiles": [f"../{source_marker}.tsx"]},
            "diagnostic_report.location_files.path_invalid",
        ),
        (
            "unplanned-path",
            {"locationFiles": [f"components/features/{source_marker}.tsx"]},
            "diagnostic_report.location_files.file_unplanned",
        ),
    ]
    failed_gate = GateResult(
        gate="typecheck",
        status=GateStatus.failed,
        summary="failed",
        affected_files=[planned_paths[0]],
    )

    for case, invalid_update, expected_code in invalid_cases:
        session = await repository.create_guest_session()
        project = await repository.create_project(session.id, f"Library {case}")
        _message, run, _created = await repository.create_message_and_run(
            project.id,
            session.id,
            f"reviewer-contract-{case}",
            "Create a book management system",
        )
        assert await repository.claim_next_run(f"test-worker-{case}", 60)
        context = SimpleNamespace(
            run_id=run.id,
            lease_token=await repository.get_active_lease_token(run.id),
        )
        invalid_report = dict(_responses()["reviewer"])
        invalid_report.update({"blockingIssues": [source_marker], **invalid_update})
        corrected_report = dict(_responses()["reviewer"])
        corrected_report.update(
            {
                "blockingIssues": ["typecheck failed"],
                "locationFiles": [planned_paths[0]],
            }
        )
        model = ScriptedModelClient({"reviewer": [invalid_report, corrected_report]})
        runner = SOPRunner(
            repository,
            model,
            FakeSandboxProvider(),
            replace(settings, structured_output_retries=1),
        )

        diagnostic = await runner._role(
            context,
            role="reviewer",
            model_alias="reviewer",
            schema=DiagnosticReport,
            messages=[{"role": "system", "content": "test reviewer contract"}],
            validate_artifact=lambda report, runner=runner: runner._validate_diagnostic_repair_scope(
                report,
                technical,
                [failed_gate],
            ),
        )

        assert diagnostic.location_files == [planned_paths[0]]
        events = await repository.list_events(run.id)
        retry_event = next(
            event
            for event in events
            if event.kind == "agent.activity" and event.payload.get("action") == "structured_retry"
        )
        assert retry_event.payload["reasonCode"] == expected_code
        correction = next(
            message["content"]
            for message in model.requests[1][1]
            if message["content"].startswith("Return only a valid DiagnosticReport JSON object")
        )
        assert source_marker not in correction
        assert source_marker not in json.dumps([event.payload for event in events])


@pytest.mark.asyncio
async def test_reviewer_scope_rejects_planned_guess_without_affected_file_evidence(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "reviewer-evidence-missing", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    context = SimpleNamespace(
        run_id=run.id,
        lease_token=await repository.get_active_lease_token(run.id),
    )
    technical = TechnicalSpec.model_validate(_responses()["architect"])
    planned_path = technical.file_plan[0].path
    source_marker = "reviewer-evidence-missing-source-marker"
    command_marker = "reviewer-evidence-missing-command-marker"
    output_marker = "reviewer-evidence-missing-output-marker"
    invalid_report = dict(_responses()["reviewer"])
    invalid_report.update(
        {
            "blockingIssues": [source_marker],
            "locationFiles": [planned_path],
        }
    )
    model = ScriptedModelClient({"reviewer": [invalid_report, dict(invalid_report)]})
    runner = SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(settings, structured_output_retries=1),
    )
    failed_gate = GateResult(
        gate="typecheck",
        status=GateStatus.failed,
        summary=output_marker,
        evidence=[f"command:{command_marker}"],
    )

    with pytest.raises(SOPExecutionError, match="reviewer failed to produce a valid DiagnosticReport"):
        await runner._role(
            context,
            role="reviewer",
            model_alias="reviewer",
            schema=DiagnosticReport,
            messages=[{"role": "system", "content": "test missing diagnostic evidence"}],
            validate_artifact=lambda report: runner._validate_diagnostic_repair_scope(
                report,
                technical,
                [failed_gate],
            ),
        )

    events = await repository.list_events(run.id)
    retry_event = next(
        event
        for event in events
        if event.kind == "agent.activity" and event.payload.get("action") == "structured_retry"
    )
    failed_event = next(event for event in events if event.kind == "agent.failed")
    assert retry_event.payload["reasonCode"] == "diagnostic_report.location_files.evidence_missing"
    assert failed_event.payload["reasonCode"] == "diagnostic_report.location_files.evidence_missing"
    correction = next(
        message["content"]
        for message in model.requests[1][1]
        if message["content"].startswith("Return only a valid DiagnosticReport JSON object")
    )
    serialized_events = json.dumps([event.payload for event in events])
    for marker in (planned_path, source_marker, command_marker, output_marker):
        assert marker not in correction
        assert marker not in serialized_events


@pytest.mark.asyncio
async def test_gate_command_extracts_concatenated_paths_and_bounds_reviewer_scope(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "gate-path-extraction", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    paths = [
        "lib/domain/library-persistence.ts",
        "lib/domain/library-actions.ts",
        "lib/domain/library-types.ts",
    ]
    stdout = "".join(f"?{path}({index},28):" for index, path in enumerate(paths, start=1))
    sandbox = FakeSandboxProvider(
        {"pnpm typecheck": ExecResult(exit_code=1, stdout=stdout, stderr="error")}
    )
    ref = await sandbox.create(project.id)
    context = SimpleNamespace(
        run_id=run.id,
        lease_token=await repository.get_active_lease_token(run.id),
        sandbox=ref,
    )
    runner = SOPRunner(repository, ScriptedModelClient({}), sandbox, settings)

    gate = await runner._gate_command(context, "typecheck", "pnpm typecheck")

    assert gate.affected_files == paths
    assert paths[0] in gate.summary
    assert gate.summary.endswith("error")
    technical = TechnicalSpec.model_validate(_architect_response_with_file_plan_count(4))
    technical_paths = [item.path for item in technical.file_plan]
    gate = gate.model_copy(update={"affected_files": technical_paths[:3]})
    runner._validate_diagnostic_repair_scope(
        DiagnosticReport(
            blocking_issues=["typecheck failed"],
            location_files=technical_paths[:3],
        ),
        technical,
        [gate],
    )
    with pytest.raises(ArtifactContractViolation) as violation:
        runner._validate_diagnostic_repair_scope(
            DiagnosticReport(
                blocking_issues=["typecheck failed"],
                location_files=[technical_paths[3]],
            ),
            technical,
            [gate],
        )
    assert violation.value.code == "diagnostic_report.location_files.not_affected"


def test_affected_workspace_paths_keep_hidden_and_dot_relative_files() -> None:
    assert SOPRunner._affected_workspace_paths(
        "./components/features/library.tsx(1,2): error\n.hidden/keep.ts(3,4): error",
        "",
    ) == ["components/features/library.tsx", ".hidden/keep.ts"]


@pytest.mark.asyncio
async def test_collector_newlines_keep_typecheck_locations_unprefixed() -> None:
    async def sink(_stream: str, _text: str) -> None:
        return None

    collector = _OutputCollector(sink, limit_bytes=4_096)
    await collector.emit(
        "stdout",
        "components/features/dashboard/dashboard-page.tsx(326,12): error TS2769: invalid aria-hidden.",
    )
    await collector.emit("stdout", "Type 'undefined'.")
    await collector.emit(
        "stdout",
        "components/features/readers/readers-controller.tsx(104,58): error TS2741: pending missing.",
    )
    await collector.emit(
        "stdout",
        "lib/domain/books-store.ts(60,18): error TS2352: invalid cast.",
    )
    await collector.emit(
        "stdout",
        "lib/domain/readers-store.ts(35,18): error TS2352: invalid cast.",
    )

    assert SOPRunner._affected_workspace_paths(collector.stdout, "") == [
        "components/features/dashboard/dashboard-page.tsx",
        "components/features/readers/readers-controller.tsx",
        "lib/domain/books-store.ts",
        "lib/domain/readers-store.ts",
    ]


def test_playwright_test_plan_requires_matching_model_owned_smoke_file_plan(repository, settings) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)

    runner._validate_technical_file_plan(TechnicalSpec.model_validate(_responses()["architect"]))
    accepted = TechnicalSpec.model_validate(_architect_response_with_playwright_smoke_path())
    runner._validate_technical_file_plan(accepted)

    for response in (
        _architect_response_with_playwright_smoke_path("tests/generated/library-smoke.spec.ts"),
        _architect_response_with_playwright_smoke_path(operation="delete"),
    ):
        with pytest.raises(ArtifactContractViolation) as violation:
            runner._validate_technical_file_plan(TechnicalSpec.model_validate(response))
        assert violation.value.code == "technical_spec.playwright_smoke.file_plan_required"


def test_no_tests_smoke_gate_uses_exact_evidence_and_bounded_reviewer_scope(repository, settings) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_architect_response_with_playwright_smoke_path())
    runner._validate_technical_file_plan(technical)
    smoke_path = "tests/generated/library.smoke.spec.ts"
    assert SOPRunner._is_exact_playwright_no_tests_failure("\nError: No tests found\n", "1")
    assert not SOPRunner._is_exact_playwright_no_tests_failure(
        "Error: No tests found after an unrelated failure",
        "",
    )

    gates = runner._add_no_tests_smoke_affected_files(
        [
            GateResult(
                gate="smoke",
                status=GateStatus.failed,
                summary="Error: No tests found",
                affected_files=["components/features/library.tsx"],
            )
        ],
        technical,
    )
    assert gates[0].affected_files == [smoke_path]
    runner._validate_diagnostic_repair_scope(
        DiagnosticReport(blocking_issues=["smoke failed"], location_files=[smoke_path]),
        technical,
        gates,
    )

    extended = runner._add_no_tests_smoke_affected_files(
        [
            GateResult(
                gate="smoke",
                status=GateStatus.failed,
                summary="Error: No tests found after an unrelated failure",
            )
        ],
        technical,
    )
    assert extended[0].affected_files == []


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_adds_direct_provider_consumer_and_contract_edges(
    repository, settings
) -> None:
    books_path = "lib/domain/books.ts"
    persistence_path = "lib/domain/library-persistence.ts"
    catalog_path = "lib/domain/catalog.ts"
    unrelated_path = "lib/domain/unrelated.ts"
    technical = _technical_for_reviewer_dependency_scope(
        [books_path, persistence_path, catalog_path, unrelated_path],
        public_api_contracts=[
            {
                "filePath": persistence_path,
                "exportStyle": "named",
                "symbol": "loadLibraryState",
                "props": [],
                "type": "() => LibraryState",
            }
        ],
    )
    runner, gates, scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [books_path],
        {
            books_path: 'import { loadLibraryState } from "./library-persistence";\nexport const books = loadLibraryState;',
            persistence_path: "export const loadLibraryState = () => ({ books: [] });",
            catalog_path: 'import { books } from "./books";\nexport const catalog = books;',
            unrelated_path: "export const unrelated = true;",
        },
    )

    assert scope.raw_paths == (books_path,)
    assert scope.derived_paths == (persistence_path, catalog_path)
    assert scope.eligible_paths == (books_path, persistence_path, catalog_path)
    diagnostic = DiagnosticReport(location_files=[books_path, persistence_path])
    runner._validate_diagnostic_repair_scope(diagnostic, technical, gates, scope)
    scoped_technical = runner._repair_technical(technical, diagnostic, scope)
    assert [item.path for item in scoped_technical.file_plan] == [books_path, persistence_path]
    with pytest.raises(ArtifactContractViolation) as out_of_scope:
        runner._validate_diagnostic_repair_scope(
            DiagnosticReport(location_files=[unrelated_path]),
            technical,
            gates,
            scope,
        )
    assert out_of_scope.value.code == "diagnostic_report.location_files.not_affected"
    with pytest.raises(SOPExecutionError, match="outside the verified repair scope"):
        runner._repair_technical(
            technical,
            DiagnosticReport(location_files=[unrelated_path]),
            scope,
        )


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_adds_declared_public_contract_feature_edges(
    repository, settings
) -> None:
    composition_path = "components/features/library/library-surface.tsx"
    controller_path = "components/features/library/use-library-controller.ts"
    technical = _technical_for_reviewer_dependency_scope(
        [composition_path, controller_path],
        public_api_contracts=[
            {
                "filePath": composition_path,
                "exportStyle": "named",
                "symbol": "LibrarySurface",
                "props": [],
                "type": "React.ComponentType",
            },
            {
                "filePath": controller_path,
                "exportStyle": "named",
                "symbol": "useLibraryController",
                "props": [],
                "type": "() => void",
            },
        ],
        feature_surfaces=[
            {
                "componentName": "LibrarySurface",
                "compositionFile": composition_path,
                "compositionSymbol": "LibrarySurface",
                "compositionResponsibilities": ["compose"],
                "modules": [
                    {
                        "role": "controller",
                        "filePath": controller_path,
                        "publicSymbol": "useLibraryController",
                    }
                ],
            }
        ],
    )

    _runner, _gates, scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [composition_path],
        {
            composition_path: "export const LibrarySurface = () => null;",
            controller_path: "export const useLibraryController = () => undefined;",
        },
    )

    assert scope.raw_paths == (composition_path,)
    assert scope.derived_paths == (controller_path,)
    assert scope.eligible_paths == (composition_path, controller_path)


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_uses_declared_state_aggregation_edges_bidirectionally(
    repository, settings
) -> None:
    aggregation_path = "lib/domain/use-library-state.ts"
    books_store_path = "lib/domain/state/books-store.ts"
    readers_store_path = "lib/domain/state/readers-store.ts"
    loans_store_path = "lib/domain/state/loans-store.ts"
    adapter_path = "lib/domain/state/library-persistence-adapter.ts"
    planned_paths = [
        aggregation_path,
        books_store_path,
        readers_store_path,
        loans_store_path,
        adapter_path,
    ]
    state_aggregation = {
        "filePath": aggregation_path,
        "responsibilities": ["compose", "re_export"],
        "persistenceAdapter": {
            "filePath": adapter_path,
            "publicSymbol": "loadLibraryState",
            "storageKey": "library-state",
            "schemaVersion": 1,
            "responsibilities": ["load", "save", "migrate"],
        },
    }
    persistent_state_domains = [
        {
            "domain": "books",
            "stateModelName": "library",
            "actionsStoreFile": books_store_path,
        },
        {
            "domain": "readers",
            "stateModelName": "library",
            "actionsStoreFile": readers_store_path,
        },
        {
            "domain": "loans",
            "stateModelName": "library",
            "actionsStoreFile": loans_store_path,
        },
    ]
    technical = _technical_for_reviewer_dependency_scope(
        planned_paths,
        persistent_state_domains=persistent_state_domains,
        state_aggregation=state_aggregation,
    )
    source_files = {path: "export const value = true;" for path in planned_paths}

    _runner, _gates, aggregation_scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [aggregation_path],
        source_files,
    )
    assert aggregation_scope.raw_paths == (aggregation_path,)
    assert aggregation_scope.derived_paths == (
        books_store_path,
        readers_store_path,
        loans_store_path,
        adapter_path,
    )

    _runner, _gates, domain_scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [books_store_path],
        source_files,
    )
    assert domain_scope.raw_paths == (books_store_path,)
    assert domain_scope.derived_paths == (aggregation_path,)
    assert domain_scope.eligible_paths == (books_store_path, aggregation_path)

    _runner, _gates, adapter_scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [adapter_path],
        source_files,
    )
    assert adapter_scope.raw_paths == (adapter_path,)
    assert adapter_scope.derived_paths == (aggregation_path,)
    assert adapter_scope.eligible_paths == (adapter_path, aggregation_path)


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_excludes_unplanned_or_protected_contract_endpoints(
    repository, settings
) -> None:
    aggregation_path = "lib/domain/use-library-state.ts"
    protected_store_path = "components/ui/button.tsx"
    unplanned_adapter_path = "lib/domain/state/unplanned-persistence-adapter.ts"
    technical = _technical_for_reviewer_dependency_scope(
        [aggregation_path, protected_store_path],
        persistent_state_domains=[
            {
                "domain": "books",
                "stateModelName": "library",
                "actionsStoreFile": protected_store_path,
            }
        ],
        state_aggregation={
            "filePath": aggregation_path,
            "responsibilities": ["compose", "re_export"],
            "persistenceAdapter": {
                "filePath": unplanned_adapter_path,
                "publicSymbol": "loadLibraryState",
                "storageKey": "library-state",
                "schemaVersion": 1,
                "responsibilities": ["load", "save", "migrate"],
            },
        },
    )

    _runner, _gates, scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [aggregation_path],
        {
            aggregation_path: "export const libraryState = true;",
            protected_store_path: "export const Button = () => null;",
        },
    )

    assert scope.raw_paths == (aggregation_path,)
    assert scope.derived_paths == ()
    assert scope.eligible_paths == (aggregation_path,)


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_resolves_barrels_aliases_indexes_and_tsx(
    repository, settings
) -> None:
    consumer_path = "components/features/consumer.tsx"
    provider_path = "components/features/provider.tsx"
    barrel_path = "lib/domain/index.ts"
    persistence_path = "lib/domain/library-persistence/index.ts"
    technical = _technical_for_reviewer_dependency_scope(
        [consumer_path, provider_path, barrel_path, persistence_path]
    )

    _runner, _gates, scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [consumer_path, barrel_path],
        {
            consumer_path: (
                'import { Provider } from "@/components/features/provider";\n'
                'import { loadLibraryState } from "@/lib/domain/library-persistence";\n'
                "export const Consumer = Provider;"
            ),
            provider_path: "export const Provider = () => null;",
            barrel_path: 'export { loadLibraryState } from "./library-persistence";',
            persistence_path: "export const loadLibraryState = () => ({ books: [] });",
        },
    )

    assert scope.raw_paths == (consumer_path, barrel_path)
    assert scope.derived_paths == (provider_path, persistence_path)
    assert scope.eligible_paths == (consumer_path, barrel_path, provider_path, persistence_path)


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_terminates_cycles_and_ignores_dynamic_or_unknown_targets(
    repository, settings
) -> None:
    first_path = "lib/domain/first.ts"
    second_path = "lib/domain/second.ts"
    protected_path = "components/ui/button.tsx"
    technical = _technical_for_reviewer_dependency_scope(
        [first_path, second_path, protected_path],
    )

    _runner, _gates, scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [first_path],
        {
            first_path: (
                'import { second } from "./second";\n'
                'const dynamic = import("./unknown");\n'
                'import { missing } from "./missing";\n'
                'import { Button } from "@/components/ui/button";\n'
                "export const first = second;"
            ),
            second_path: 'import { first } from "./first";\nexport const second = first;',
            protected_path: "export const Button = () => null;",
        },
    )

    assert scope.raw_paths == (first_path,)
    assert scope.derived_paths == (second_path,)
    assert scope.eligible_paths == (first_path, second_path)


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_ignores_comment_and_literal_module_text(
    repository, settings
) -> None:
    books_path = "lib/domain/books.ts"
    persistence_path = "lib/domain/library-persistence.ts"
    technical = _technical_for_reviewer_dependency_scope([books_path, persistence_path])

    _runner, _gates, scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [books_path],
        {
            books_path: (
                '// import { loadLibraryState } from "./library-persistence";\n'
                "/*\n"
                'import { loadLibraryState } from "./library-persistence";\n'
                "*/\n"
                'const quoted = \'export { loadLibraryState } from "./library-persistence";\';\n'
                'const template = `example: ${`import { loadLibraryState } from "./library-persistence";`}`;\n'
                "export const books = [];"
            ),
            persistence_path: "export const loadLibraryState = () => ({ books: [] });",
        },
    )

    assert scope.raw_paths == (books_path,)
    assert scope.derived_paths == ()
    assert scope.eligible_paths == (books_path,)


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_drops_unplanned_and_protected_raw_paths(
    repository, settings
) -> None:
    books_path = "lib/domain/books.ts"
    protected_path = "components/ui/button.tsx"
    technical = _technical_for_reviewer_dependency_scope([books_path, protected_path])

    _runner, _gates, scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        ["package.json", protected_path, books_path],
        {books_path: "export const books = [];", protected_path: "export const Button = () => null;"},
    )

    assert scope.raw_paths == (books_path,)
    assert scope.derived_paths == ()
    assert scope.eligible_paths == (books_path,)


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_keeps_no_evidence_fail_closed_without_expansion(
    repository, settings
) -> None:
    books_path = "lib/domain/books.ts"
    persistence_path = "lib/domain/library-persistence.ts"
    technical = _technical_for_reviewer_dependency_scope([books_path, persistence_path])
    runner, gates, scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [],
        {
            books_path: 'import { loadLibraryState } from "./library-persistence";\nexport const books = [];',
            persistence_path: "export const loadLibraryState = () => ({ books: [] });",
        },
    )

    assert scope.raw_paths == ()
    assert scope.derived_paths == ()
    assert scope.eligible_paths == ()
    with pytest.raises(ArtifactContractViolation) as evidence_missing:
        runner._validate_diagnostic_repair_scope(
            DiagnosticReport(location_files=[books_path]),
            technical,
            gates,
            scope,
        )
    assert evidence_missing.value.code == "diagnostic_report.location_files.evidence_missing"


@pytest.mark.asyncio
async def test_reviewer_dependency_scope_allows_books_to_persistence_cross_batch_repair(
    repository, settings
) -> None:
    books_path = "lib/domain/books.ts"
    persistence_path = "lib/domain/library-persistence.ts"
    readers_path = "lib/domain/readers.ts"
    technical = _technical_for_reviewer_dependency_scope(
        [books_path, persistence_path, readers_path],
        public_api_contracts=[
            {
                "filePath": persistence_path,
                "exportStyle": "named",
                "symbol": "loadLibraryState",
                "props": [],
                "type": "() => LibraryState",
            }
        ],
    )
    runner, gates, scope = await _derive_reviewer_dependency_scope(
        repository,
        settings,
        technical,
        [books_path],
        {
            books_path: (
                'import { loadLibraryState } from "./library-persistence";\n'
                "export const listBooks = () => loadLibraryState().books;"
            ),
            persistence_path: "export const loadLibraryState = () => ({ books: [] });",
            readers_path: "export const readers = [];",
        },
    )

    assert scope.eligible_paths == (books_path, persistence_path)
    diagnostic = DiagnosticReport(location_files=[books_path, persistence_path])
    runner._validate_diagnostic_repair_scope(diagnostic, technical, gates, scope)
    scoped_technical = runner._repair_technical(technical, diagnostic, scope)
    assert [item.path for item in scoped_technical.file_plan] == [books_path, persistence_path]


@pytest.mark.asyncio
async def test_architect_contracts_bind_component_decisions_and_public_api_files(repository, settings) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_responses()["architect"])

    runner._validate_technical_file_plan(technical)
    empty_decisions_response = dict(_responses()["architect"])
    empty_decisions_response["componentDecisions"] = []
    with pytest.raises(ValueError):
        TechnicalSpec.model_validate(empty_decisions_response)
    unmatched_component_response = dict(_responses()["architect"])
    unmatched_component_response["components"] = [
        {
            "name": "BookRow",
            "responsibility": "render a book",
            "children": [],
            "interactionResponsibilities": [],
        }
    ]
    runner._validate_technical_file_plan(TechnicalSpec.model_validate(unmatched_component_response))
    with pytest.raises(ArtifactContractViolation) as duplicate_decisions:
        runner._validate_technical_file_plan(
            technical.model_copy(
                update={"component_decisions": [technical.component_decisions[0], technical.component_decisions[0]]}
            )
        )
    assert duplicate_decisions.value.code == "technical_spec.component_decisions.duplicate"
    invalid_contract = technical.public_api_contracts[0].model_copy(update={"file_path": "missing.ts"})
    with pytest.raises(ArtifactContractViolation) as unplanned_contract:
        runner._validate_technical_file_plan(
            technical.model_copy(update={"public_api_contracts": [invalid_contract]})
        )
    assert unplanned_contract.value.code == "technical_spec.public_api_contracts.file_unplanned"
    no_contract_response = dict(_responses()["architect"])
    no_contract_response.pop("publicApiContracts")
    no_contract_technical = TechnicalSpec.model_validate(no_contract_response)
    assert no_contract_technical.public_api_contracts == []
    with pytest.raises(ArtifactContractViolation) as missing_root_contract:
        runner._validate_technical_file_plan(no_contract_technical)
    assert missing_root_contract.value.code == "technical_spec.extension_contract.public_api_required"
    deleted_file_plan = [
        item.model_copy(update={"operation": "delete"})
        if item.path == "components/features/library.tsx"
        else item
        for item in technical.file_plan
    ]
    with pytest.raises(ArtifactContractViolation) as deleted_contract:
        runner._validate_technical_file_plan(
            technical.model_copy(update={"file_plan": deleted_file_plan})
        )
    assert deleted_contract.value.code == "technical_spec.public_api_contracts.file_deleted"


@pytest.mark.asyncio
async def test_architect_persistent_domain_slices_use_explicit_state_declarations(repository, settings) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_architect_response_with_domain_state_slices())

    runner._validate_technical_file_plan(technical)

    shared_store = technical.persistent_state_domains[1].model_copy(
        update={"actions_store_file": technical.persistent_state_domains[0].actions_store_file}
    )
    with pytest.raises(ArtifactContractViolation) as shared_store_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(
                update={
                    "persistent_state_domains": [
                        technical.persistent_state_domains[0],
                        shared_store,
                        technical.persistent_state_domains[2],
                    ]
                }
            )
        )
    assert shared_store_violation.value.code == "technical_spec.persistent_state_domains.file_shared"

    missing_domain_mapping = technical.model_copy(update={"persistent_state_domains": []})
    with pytest.raises(ArtifactContractViolation) as missing_mapping_violation:
        runner._validate_technical_file_plan(missing_domain_mapping)
    assert missing_mapping_violation.value.code == "technical_spec.persistent_state_domains.mapping_invalid"

    assert technical.state_aggregation is not None
    aggregation = technical.state_aggregation
    assert aggregation.persistence_adapter is not None
    adapter = aggregation.persistence_adapter

    missing_adapter = aggregation.model_copy(update={"persistence_adapter": None})
    with pytest.raises(ArtifactContractViolation) as missing_adapter_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(update={"state_aggregation": missing_adapter})
        )
    assert (
        missing_adapter_violation.value.code
        == "technical_spec.persistent_state_domains.persistence_adapter_missing"
    )

    invalid_adapter_responsibilities = adapter.model_copy(
        update={"responsibilities": ["load", "save", "migrate", "load"]}
    )
    with pytest.raises(ArtifactContractViolation) as adapter_responsibilities_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(
                update={
                    "state_aggregation": aggregation.model_copy(
                        update={"persistence_adapter": invalid_adapter_responsibilities}
                    )
                }
            )
        )
    assert (
        adapter_responsibilities_violation.value.code
        == "technical_spec.persistent_state_domains.persistence_adapter_responsibilities_invalid"
    )

    unplanned_adapter = adapter.model_copy(update={"file_path": "lib/domain/state/missing-adapter.ts"})
    with pytest.raises(ArtifactContractViolation) as unplanned_adapter_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(
                update={
                    "state_aggregation": aggregation.model_copy(
                        update={"persistence_adapter": unplanned_adapter}
                    )
                }
            )
        )
    assert (
        unplanned_adapter_violation.value.code
        == "technical_spec.persistent_state_domains.persistence_adapter_file_unplanned"
    )

    conflicting_adapter = adapter.model_copy(update={"file_path": aggregation.file_path})
    with pytest.raises(ArtifactContractViolation) as adapter_conflict_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(
                update={
                    "state_aggregation": aggregation.model_copy(
                        update={"persistence_adapter": conflicting_adapter}
                    )
                }
            )
        )
    assert (
        adapter_conflict_violation.value.code
        == "technical_spec.persistent_state_domains.persistence_adapter_file_conflict"
    )

    unbound_adapter = adapter.model_copy(update={"public_symbol": "missingPersistenceAdapter"})
    with pytest.raises(ArtifactContractViolation) as adapter_unbound_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(
                update={
                    "state_aggregation": aggregation.model_copy(
                        update={"persistence_adapter": unbound_adapter}
                    )
                }
            )
        )
    assert (
        adapter_unbound_violation.value.code
        == "technical_spec.persistent_state_domains.persistence_adapter_public_api_unbound"
    )

    invalid_aggregation = technical.state_aggregation.model_copy(
        update={"responsibilities": ["compose", "cross_domain_crud"]}
    )
    with pytest.raises(ArtifactContractViolation) as aggregation_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(update={"state_aggregation": invalid_aggregation})
        )
    assert (
        aggregation_violation.value.code
        == "technical_spec.persistent_state_domains.aggregation_responsibilities_invalid"
    )

    persist_aggregation = aggregation.model_copy(
        update={"responsibilities": ["compose", "persist", "re_export"]}
    )
    with pytest.raises(ArtifactContractViolation) as persist_rejected_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(update={"state_aggregation": persist_aggregation})
        )
    assert (
        persist_rejected_violation.value.code
        == "technical_spec.persistent_state_domains.aggregation_persist_rejected"
    )

    invalid_state_class = _architect_response_with_domain_state_slices()
    invalid_state_class["stateModel"][0]["stateClass"] = "persistent"
    with pytest.raises(ValueError):
        TechnicalSpec.model_validate(invalid_state_class)


@pytest.mark.asyncio
async def test_architect_two_concern_feature_surface_slices_bind_explicit_concerns(repository, settings) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_architect_response_with_feature_surface_slices())

    runner._validate_technical_file_plan(technical)

    three_concern_technical = TechnicalSpec.model_validate(
        _architect_response_with_three_concern_feature_surface_slices()
    )
    runner._validate_technical_file_plan(three_concern_technical)

    over_budget_technical = TechnicalSpec.model_validate(
        _architect_response_with_four_concern_feature_surface()
    )
    with pytest.raises(ArtifactContractViolation) as over_budget_violation:
        runner._validate_technical_file_plan(over_budget_technical)
    assert over_budget_violation.value.code == "technical_spec.feature_surfaces.controller_scope_exceeded"

    split_technical = TechnicalSpec.model_validate(_architect_response_with_split_feature_surface_slices())
    runner._validate_technical_file_plan(split_technical)
    split_batches = [
        {
            "id": f"split-{index}",
            "purpose": "bounded feature surface file",
            "paths": [item.path],
        }
        for index, item in enumerate(split_technical.file_plan, start=1)
    ]
    runner._validate_implementation_plan(
        ImplementationPlan.model_validate({"batches": split_batches}),
        split_technical,
    )
    missing_and_extra_batches = [
        *split_batches[:-1],
        {
            **split_batches[-1],
            "paths": ["components/features/catalog/unplanned.tsx"],
        },
    ]
    with pytest.raises(ValueError, match="paths must exactly match"):
        runner._validate_implementation_plan(
            ImplementationPlan.model_validate({"batches": missing_and_extra_batches}),
            split_technical,
        )

    missing_surface = technical.model_copy(update={"feature_surfaces": []})
    with pytest.raises(ArtifactContractViolation) as missing_surface_violation:
        runner._validate_technical_file_plan(missing_surface)
    assert missing_surface_violation.value.code == "technical_spec.feature_surfaces.component_mapping_invalid"

    surface = technical.feature_surfaces[0]
    shared_module = surface.modules[2].model_copy(update={"file_path": surface.modules[1].file_path})
    shared_surface = surface.model_copy(
        update={"modules": [surface.modules[0], surface.modules[1], shared_module]}
    )
    with pytest.raises(ArtifactContractViolation) as shared_file_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(update={"feature_surfaces": [shared_surface]})
        )
    assert shared_file_violation.value.code == "technical_spec.feature_surfaces.file_conflict"

    without_controller = surface.model_copy(
        update={"modules": [module for module in surface.modules if module.role != "controller"]}
    )
    with pytest.raises(ArtifactContractViolation) as controller_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(update={"feature_surfaces": [without_controller]})
        )
    assert controller_violation.value.code == "technical_spec.feature_surfaces.controller_missing"

    unbound_module = surface.modules[1].model_copy(update={"public_symbol": "MissingCatalogSearch"})
    unbound_surface = surface.model_copy(
        update={"modules": [surface.modules[0], unbound_module, *surface.modules[2:]]}
    )
    with pytest.raises(ArtifactContractViolation) as unbound_api_violation:
        runner._validate_technical_file_plan(
            technical.model_copy(update={"feature_surfaces": [unbound_surface]})
        )
    assert unbound_api_violation.value.code == "technical_spec.feature_surfaces.public_api_unbound"

    missing_interaction_responsibilities = _architect_response_with_feature_surface_slices()
    missing_interaction_responsibilities["components"][0].pop("interactionResponsibilities")
    with pytest.raises(ValueError):
        TechnicalSpec.model_validate(missing_interaction_responsibilities)


@pytest.mark.asyncio
async def test_architect_file_plan_over_capacity_retries_before_sandbox_creation(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-capacity", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    responses = _responses()
    responses["architect"] = [
        _architect_response_with_file_plan_count(25),
        responses["architect"],
    ]
    model = ScriptedModelClient(responses)
    sandbox = FakeSandboxProvider()
    await SOPRunner(
        repository,
        model,
        sandbox,
        replace(settings, structured_output_retries=1),
    ).run(run.id)

    assert model.calls == [
        ("pm", "ProductSpec"),
        ("architect", "TechnicalSpec"),
        ("architect", "TechnicalSpec"),
        ("engineer", "ImplementationPlan"),
        ("engineer", "FileBatchReport"),
        ("engineer", "FileBatchReport"),
        ("engineer", "ImplementationReport"),
        ("reviewer", "DiagnosticReport"),
    ]
    final = await repository.get_run(run.id)
    assert final.status.value == "succeeded"
    assert len(sandbox.sandboxes) == 1
    events = await repository.list_events(run.id)
    assert any(
        event.kind == "agent.activity"
        and event.role == "architect"
        and event.payload.get("action") == "structured_retry"
        for event in events
    )
    assert any(
        event.kind == "agent.activity" and event.payload.get("action") == "sandbox_created"
        for event in events
    )


@pytest.mark.asyncio
async def test_repeated_architect_file_plan_over_capacity_fails_without_sandbox(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-capacity-failure", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    responses = _responses()
    responses["architect"] = [
        _architect_response_with_file_plan_count(25),
        _architect_response_with_file_plan_count(25),
    ]
    model = ScriptedModelClient(responses)
    sandbox = FakeSandboxProvider()

    with pytest.raises(SOPExecutionError, match="architect failed to produce a valid TechnicalSpec"):
        await SOPRunner(
            repository,
            model,
            sandbox,
            replace(settings, structured_output_retries=1),
        ).run(run.id)

    assert model.calls == [
        ("pm", "ProductSpec"),
        ("architect", "TechnicalSpec"),
        ("architect", "TechnicalSpec"),
    ]
    final = await repository.get_run(run.id)
    assert final.status.value == "failed"
    assert sandbox.sandboxes == {}
    events = await repository.list_events(run.id)
    assert not any(
        event.kind == "agent.activity" and event.payload.get("action") == "sandbox_created"
        for event in events
    )
    assert sum(
        1
        for event in events
        if event.kind == "agent.activity"
        and event.role == "architect"
        and event.payload.get("action") == "structured_retry"
    ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "nested/.gitignore",
    ],
)
async def test_architect_file_plan_rejects_system_managed_paths(repository, settings, path) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_architect_response_with_system_managed_path(path))

    with pytest.raises(ArtifactContractViolation) as violation:
        runner._validate_technical_file_plan(technical)
    assert violation.value.code == "technical_spec.file_plan.system_managed"
    assert violation.value.repair_instruction == "Remove system-managed files from TechnicalSpec.filePlan."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("package.json", "technical_spec.file_plan.starter_protected"),
        ("components/ui/button.tsx", "technical_spec.file_plan.starter_protected"),
        ("app/layout.tsx", "technical_spec.file_plan.starter_protected"),
        ("playwright.config.ts", "technical_spec.file_plan.starter_protected"),
        ("src/not-allowed.ts", "technical_spec.file_plan.model_root"),
    ],
)
async def test_architect_file_plan_rejects_immutable_starter_and_non_model_roots(
    repository, settings, path, code
) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_architect_response_with_system_managed_path(path))

    with pytest.raises(ArtifactContractViolation) as violation:
        runner._validate_technical_file_plan(technical)
    assert violation.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("components/ui/button.tsx", "file_batch_report.starter_protected"),
        ("package.json", "file_batch_report.starter_protected"),
        ("src/not-allowed.ts", "file_batch_report.model_root"),
    ],
)
async def test_engineer_file_batch_rejects_immutable_starter_and_non_model_roots(
    repository, settings, path, code
) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    expected = ImplementationBatchPlan.model_validate(
        {"id": "requested-batch", "purpose": "test", "paths": [path]}
    )
    report = FileBatchReport.model_validate(
        {
            "batchId": "requested-batch",
            "fileChanges": [{"path": path, "content": "export {}"}],
        }
    )

    with pytest.raises(FileBatchContractViolation) as violation:
        runner._validate_file_batch_report(report, expected)
    assert violation.value.code == code


@pytest.mark.asyncio
async def test_system_managed_file_plan_retries_before_sandbox_creation(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-system-path-retry", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    responses = _responses()
    responses["architect"] = [
        _architect_response_with_system_managed_path("pnpm-lock.yaml"),
        responses["architect"],
    ]
    model = ScriptedModelClient(responses)
    sandbox = FakeSandboxProvider()

    await SOPRunner(
        repository,
        model,
        sandbox,
        replace(settings, structured_output_retries=1),
    ).run(run.id)

    assert model.calls == [
        ("pm", "ProductSpec"),
        ("architect", "TechnicalSpec"),
        ("architect", "TechnicalSpec"),
        ("engineer", "ImplementationPlan"),
        ("engineer", "FileBatchReport"),
        ("engineer", "FileBatchReport"),
        ("engineer", "ImplementationReport"),
        ("reviewer", "DiagnosticReport"),
    ]
    assert len(sandbox.sandboxes) == 1
    technical = await repository.get_latest_artifact(run.id, "technical_spec")
    assert technical is not None
    assert {item["path"] for item in technical["filePlan"]} == {
        "app/(generated)/composition.tsx",
        "components/features/library.tsx",
    }
    events = await repository.list_events(run.id)
    retry_event = next(
        event
        for event in events
        if event.kind == "agent.activity"
        and event.role == "architect"
        and event.payload.get("action") == "structured_retry"
    )
    assert retry_event.payload == {
        "action": "structured_retry",
        "summary": "The structured hand-off was invalid; requesting a schema-correct response.",
        "reasonCode": "technical_spec.file_plan.system_managed",
    }
    repair_instruction = "Remove system-managed files from TechnicalSpec.filePlan."
    assert repair_instruction not in json.dumps([event.payload for event in events])
    architect_requests = [request for request in model.requests if request[0] == "architect"]
    correction_message = next(
        message["content"]
        for message in architect_requests[1][1]
        if message["content"].startswith("Return only a valid TechnicalSpec JSON object")
    )
    assert correction_message == (
        "Return only a valid TechnicalSpec JSON object matching the declared schema.\n"
        + repair_instruction
    )


@pytest.mark.asyncio
async def test_architect_persistence_adapter_retry_uses_closed_code_without_event_leak(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-domain-slice-retry", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    context = SimpleNamespace(
        run_id=run.id,
        lease_token=await repository.get_active_lease_token(run.id),
    )
    source_marker = "model-body-never-leak"
    invalid_response = _architect_response_with_domain_state_slices()
    invalid_response["stateModel"][0]["owner"] = source_marker
    invalid_response["stateAggregation"]["persistenceAdapter"]["schemaVersion"] = 0
    model = ScriptedModelClient(
        {
            "architect": [
                invalid_response,
                _architect_response_with_domain_state_slices(),
            ]
        }
    )
    runner = SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(settings, structured_output_retries=1),
    )

    artifact = await runner._role(
        context,
        role="architect",
        model_alias="architect",
        schema=TechnicalSpec,
        messages=[{"role": "system", "content": "test architect domain state correction"}],
        validate_artifact=runner._validate_technical_file_plan,
    )

    assert isinstance(artifact, TechnicalSpec)
    events = await repository.list_events(run.id)
    retry_event = next(
        event
        for event in events
        if event.kind == "agent.activity"
        and event.role == "architect"
        and event.payload.get("action") == "structured_retry"
    )
    assert retry_event.payload == {
        "action": "structured_retry",
        "summary": "The structured hand-off was invalid; requesting a schema-correct response.",
        "reasonCode": "technical_spec.persistent_state_domains.persistence_adapter_schema_version_invalid",
    }
    repair_instruction = "Set persistenceAdapter.schemaVersion to an integer greater than or equal to 1."
    serialized_events = json.dumps([event.payload for event in events])
    assert repair_instruction not in serialized_events
    assert source_marker not in serialized_events
    correction_message = next(
        message["content"]
        for message in model.requests[1][1]
        if message["content"].startswith("Return only a valid TechnicalSpec JSON object")
    )
    assert correction_message == (
        "Return only a valid TechnicalSpec JSON object matching the declared schema.\n"
        + repair_instruction
    )


@pytest.mark.asyncio
async def test_architect_feature_surface_retry_uses_closed_component_mapping_code_without_event_leak(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Catalog")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-feature-surface-retry", "Create a catalog management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    context = SimpleNamespace(
        run_id=run.id,
        lease_token=await repository.get_active_lease_token(run.id),
    )
    source_marker = "model-body-never-leak"
    invalid_response = _architect_response_with_feature_surface_slices()
    invalid_response["components"][0]["responsibility"] = source_marker
    invalid_response["featureSurfaces"] = []
    model = ScriptedModelClient(
        {
            "architect": [
                invalid_response,
                _architect_response_with_feature_surface_slices(),
            ]
        }
    )
    runner = SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(settings, structured_output_retries=1),
    )

    artifact = await runner._role(
        context,
        role="architect",
        model_alias="architect",
        schema=TechnicalSpec,
        messages=[{"role": "system", "content": "test architect feature surface correction"}],
        validate_artifact=runner._validate_technical_file_plan,
    )

    assert isinstance(artifact, TechnicalSpec)
    events = await repository.list_events(run.id)
    retry_event = next(
        event
        for event in events
        if event.kind == "agent.activity"
        and event.role == "architect"
        and event.payload.get("action") == "structured_retry"
    )
    assert retry_event.payload == {
        "action": "structured_retry",
        "summary": "The structured hand-off was invalid; requesting a schema-correct response.",
        "reasonCode": "technical_spec.feature_surfaces.component_mapping_invalid",
    }
    repair_instruction = (
        "Give every ComponentSpec an explicit interactionResponsibilities list and declare exactly one "
        "featureSurfaces entry for each component with two or more concerns."
    )
    serialized_events = json.dumps([event.payload for event in events])
    assert repair_instruction not in serialized_events
    assert source_marker not in serialized_events
    correction_message = next(
        message["content"]
        for message in model.requests[1][1]
        if message["content"].startswith("Return only a valid TechnicalSpec JSON object")
    )
    assert correction_message == (
        "Return only a valid TechnicalSpec JSON object matching the declared schema.\n"
        + repair_instruction
    )


@pytest.mark.asyncio
async def test_architect_feature_surface_controller_budget_retries_to_split_surfaces_without_event_leak(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Catalog")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-feature-surface-budget-retry", "Create a catalog management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    context = SimpleNamespace(
        run_id=run.id,
        lease_token=await repository.get_active_lease_token(run.id),
    )
    source_marker = "model-body-never-leak"
    invalid_response = _architect_response_with_four_concern_feature_surface()
    invalid_response["components"][0]["responsibility"] = source_marker
    model = ScriptedModelClient(
        {
            "architect": [
                invalid_response,
                _architect_response_with_split_feature_surface_slices(),
            ]
        }
    )
    runner = SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(settings, structured_output_retries=1),
    )

    artifact = await runner._role(
        context,
        role="architect",
        model_alias="architect",
        schema=TechnicalSpec,
        messages=[{"role": "system", "content": "test architect controller budget correction"}],
        validate_artifact=runner._validate_technical_file_plan,
    )

    assert isinstance(artifact, TechnicalSpec)
    assert [surface.component_name for surface in artifact.feature_surfaces] == [
        "CatalogSurface",
        "CatalogOperationsSurface",
    ]
    events = await repository.list_events(run.id)
    retry_event = next(
        event
        for event in events
        if event.kind == "agent.activity"
        and event.role == "architect"
        and event.payload.get("action") == "structured_retry"
    )
    assert retry_event.payload == {
        "action": "structured_retry",
        "summary": "The structured hand-off was invalid; requesting a schema-correct response.",
        "reasonCode": "technical_spec.feature_surfaces.controller_scope_exceeded",
    }
    repair_instruction = (
        "Split any ComponentSpec with more than three interactionResponsibilities into multiple featureSurfaces "
        "of at most three concerns, each with its own controller; keep the top-level aggregate composition-only "
        "with an empty interactionResponsibilities list."
    )
    serialized_events = json.dumps([event.payload for event in events])
    assert repair_instruction not in serialized_events
    assert source_marker not in serialized_events
    correction_message = next(
        message["content"]
        for message in model.requests[1][1]
        if message["content"].startswith("Return only a valid TechnicalSpec JSON object")
    )
    assert correction_message == (
        "Return only a valid TechnicalSpec JSON object matching the declared schema.\n"
        + repair_instruction
    )


@pytest.mark.asyncio
async def test_non_architect_contract_violation_uses_generic_schema_correction(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-non-architect-contract", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    context = SimpleNamespace(
        run_id=run.id,
        lease_token=await repository.get_active_lease_token(run.id),
    )
    model = ScriptedModelClient(
        {"engineer": [_responses()["architect"], _responses()["architect"]]}
    )
    validation_attempts = 0

    def reject_once(_artifact: TechnicalSpec) -> None:
        nonlocal validation_attempts
        validation_attempts += 1
        if validation_attempts == 1:
            raise ArtifactContractViolation(code="technical_spec.file_plan.system_managed")

    artifact = await SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(settings, structured_output_retries=1),
    )._role(
        context,
        role="engineer",
        model_alias="engineer",
        schema=TechnicalSpec,
        messages=[{"role": "system", "content": "test generic correction"}],
        validate_artifact=reject_once,
    )

    assert isinstance(artifact, TechnicalSpec)
    retry_event = next(
        event
        for event in await repository.list_events(run.id)
        if event.kind == "agent.activity" and event.payload.get("action") == "structured_retry"
    )
    assert retry_event.payload == {
        "action": "structured_retry",
        "summary": "The structured hand-off was invalid; requesting a schema-correct response.",
    }
    correction_message = next(
        message["content"]
        for message in model.requests[1][1]
        if message["content"].startswith("Return only a valid TechnicalSpec JSON object")
    )
    assert correction_message == "Return only a valid TechnicalSpec JSON object matching the declared schema."


@pytest.mark.asyncio
async def test_file_batch_contract_violation_adds_targeted_schema_correction(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-file-batch-contract", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    context = SimpleNamespace(
        run_id=run.id,
        lease_token=await repository.get_active_lease_token(run.id),
    )
    expected = ImplementationBatchPlan.model_validate(
        {"id": "requested-batch", "purpose": "test", "paths": ["components/features/book.tsx"]}
    )
    source_marker = "model-body-never-leak"
    model = ScriptedModelClient(
        {
            "engineer": [
                {
                    "batchId": "wrong-batch",
                    "fileChanges": [{"path": "components/features/book.tsx", "content": source_marker}],
                },
                {
                    "batchId": "requested-batch",
                    "fileChanges": [{"path": "components/features/book.tsx", "content": "export {}"}],
                },
            ]
        }
    )
    runner = SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(settings, structured_output_retries=1),
    )

    artifact = await runner._role(
        context,
        role="engineer",
        model_alias="engineer",
        schema=FileBatchReport,
        messages=[{"role": "system", "content": "test file batch correction"}],
        validate_artifact=lambda report: runner._validate_file_batch_report(report, expected),
    )

    assert isinstance(artifact, FileBatchReport)
    events = await repository.list_events(run.id)
    retry_event = next(
        event
        for event in events
        if event.kind == "agent.activity"
        and event.role == "engineer"
        and event.payload.get("action") == "structured_retry"
    )
    assert retry_event.payload == {
        "action": "structured_retry",
        "summary": "The structured hand-off was invalid; requesting a schema-correct response.",
        "reasonCode": "file_batch_report.batch_id_mismatch",
    }
    repair_instruction = "Set batchId to the requested batch id."
    assert repair_instruction not in json.dumps([event.payload for event in events])
    assert source_marker not in json.dumps([event.payload for event in events])
    correction_message = next(
        message["content"]
        for message in model.requests[1][1]
        if message["content"].startswith("Return only a valid FileBatchReport JSON object")
    )
    assert correction_message == (
        "Return only a valid FileBatchReport JSON object matching the declared schema.\n"
        + repair_instruction
    )


@pytest.mark.asyncio
async def test_file_batch_size_retry_and_failure_include_only_safe_metrics(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-file-batch-size-contract", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    context = SimpleNamespace(
        run_id=run.id,
        lease_token=await repository.get_active_lease_token(run.id),
    )
    expected = ImplementationBatchPlan.model_validate(
        {"id": "requested-batch", "purpose": "test", "paths": ["components/features/book.tsx"]}
    )
    source_marker = "oversized-source-never-leak"
    prompt_marker = "oversized-prompt-never-leak"
    oversized_content = source_marker + "x" * (20_001 - len(source_marker))
    model = ScriptedModelClient(
        {
            "engineer": [
                {
                    "batchId": "requested-batch",
                    "fileChanges": [
                        {"path": "components/features/book.tsx", "content": oversized_content}
                    ],
                },
                {
                    "batchId": "requested-batch",
                    "fileChanges": [
                        {"path": "components/features/book.tsx", "content": oversized_content}
                    ],
                },
            ]
        }
    )
    runner = SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(
            settings,
            engineer_target_file_characters=12_000,
            engineer_max_file_characters=20_000,
            structured_output_retries=1,
        ),
    )

    with pytest.raises(SOPExecutionError, match="engineer failed to produce a valid FileBatchReport"):
        await runner._role(
            context,
            role="engineer",
            model_alias="engineer",
            schema=FileBatchReport,
            messages=[{"role": "system", "content": prompt_marker}],
            validate_artifact=lambda report: runner._validate_file_batch_report(report, expected),
        )

    events = await repository.list_events(run.id)
    retry_event = next(
        event
        for event in events
        if event.kind == "agent.activity" and event.payload.get("action") == "structured_retry"
    )
    failed_event = next(
        event for event in events if event.kind == "agent.failed" and event.role == "engineer"
    )
    safe_metrics = {
        "hardLimitCharacters": 20_000,
        "maxObservedCharacters": 20_001,
        "oversizedFileCount": 1,
    }
    assert retry_event.payload == {
        "action": "structured_retry",
        "summary": "The structured hand-off was invalid; requesting a schema-correct response.",
        "reasonCode": "file_batch_report.file_size_exceeded",
        **safe_metrics,
    }
    assert failed_event.payload == {
        "role": "engineer",
        "errorType": "FileBatchContractViolation",
        "reasonCode": "file_batch_report.file_size_exceeded",
        **safe_metrics,
    }
    event_payloads = json.dumps([event.payload for event in events])
    assert source_marker not in event_payloads
    assert prompt_marker not in event_payloads
    correction_message = next(
        message["content"]
        for message in model.requests[1][1]
        if message["content"].startswith("Return only a valid FileBatchReport JSON object")
    )
    assert correction_message == (
        "Return only a valid FileBatchReport JSON object matching the declared schema.\n"
        "The prior FileBatchReport exceeded the 20000-character hard limit; its maximum observed "
        "file size was 20001 characters. Regenerate a complete FileBatchReport for exactly the "
        "same requested path set. For every create or modify file, rewrite the entire file from "
        "scratch; do not trim or continue the rejected response. Do not add, remove, rename, or "
        "substitute requested paths. Preserve the requested batch's acceptance IDs, all shared public "
        "API contracts, and required business behavior, side effects, and imports. Do not use stubs, "
        "TODOs, no-ops, or error-path-only implementations to shorten the result. Reuse planned domain "
        "stores and modules instead of duplicating them. Keep each create or modify file at or below "
        "the 12000-character soft target, leaving 8000 characters of headroom below the "
        "20000-character hard limit."
    )
    assert source_marker not in correction_message
    assert prompt_marker not in correction_message


@pytest.mark.asyncio
async def test_file_batch_size_retry_persists_only_the_compact_requested_path(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-file-batch-size-retry", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    source_marker = "oversized-batch-never-persist"
    engineer_cycle = _two_batch_engineer_cycle()
    oversized_first_batch = {
        **engineer_cycle[1],
        "fileChanges": [
            {
                **engineer_cycle[1]["fileChanges"][0],
                "content": source_marker + "x" * (20_001 - len(source_marker)),
            }
        ],
    }
    responses = _responses()
    responses["engineer"] = [
        engineer_cycle[0],
        oversized_first_batch,
        engineer_cycle[1],
        engineer_cycle[2],
        engineer_cycle[3],
    ]

    await SOPRunner(
        repository,
        ScriptedModelClient(responses),
        FakeSandboxProvider(),
        replace(settings, structured_output_retries=1),
    ).run(run.id)

    final = await repository.get_run(run.id)
    assert final.status.value == "succeeded"
    events = await repository.list_events(run.id)
    first_batch_changes = [
        event.payload
        for event in events
        if event.kind == "file.changed" and event.payload.get("batchId") == "library-page"
    ]
    assert first_batch_changes == [
        {"path": "app/(generated)/composition.tsx", "operation": "modify", "batchId": "library-page"}
    ]
    _version_id, page_source, _sha256 = await repository.get_version_file_content(
        project.id, "app/(generated)/composition.tsx"
    )
    assert page_source == engineer_cycle[1]["fileChanges"][0]["content"]
    assert source_marker not in page_source
    assert source_marker not in json.dumps([event.payload for event in events])


@pytest.mark.asyncio
async def test_over_target_file_batch_persists_then_emits_one_safe_warning(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-file-batch-over-target", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    source_marker = "over-target-source-never-leak"
    responses = _responses()
    engineer_cycle = _two_batch_engineer_cycle()
    engineer_cycle[1]["fileChanges"][0]["content"] = source_marker + "x" * (
        12_001 - len(source_marker)
    )
    responses["engineer"] = engineer_cycle

    await SOPRunner(repository, ScriptedModelClient(responses), FakeSandboxProvider(), settings).run(run.id)

    final = await repository.get_run(run.id)
    assert final.status.value == "succeeded"
    events = await repository.list_events(run.id)
    warnings = [
        event
        for event in events
        if event.kind == "agent.activity" and event.payload.get("action") == "file_batch_over_target"
    ]
    assert len(warnings) == 1
    assert warnings[0].payload == {
        "action": "file_batch_over_target",
        "targetCharacters": 12_000,
        "maxObservedCharacters": 12_001,
        "overTargetFileCount": 1,
    }
    warning_index = events.index(warnings[0])
    assert events[warning_index - 1].kind == "agent.activity"
    assert events[warning_index - 1].payload.get("action") == "implementation_batch_persisted"
    assert source_marker not in json.dumps([event.payload for event in events])


@pytest.mark.asyncio
async def test_engineer_non_file_batch_contract_violation_uses_generic_schema_correction(
    repository, settings
) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-engineer-generic-contract", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    context = SimpleNamespace(
        run_id=run.id,
        lease_token=await repository.get_active_lease_token(run.id),
    )
    plan_payload = {
        "batches": [
            {
                "id": "requested-batch",
                "purpose": "test",
                "paths": ["components/features/book.tsx"],
            }
        ]
    }
    model = ScriptedModelClient({"engineer": [plan_payload, plan_payload]})
    validation_attempts = 0

    def reject_once(_artifact: ImplementationPlan) -> None:
        nonlocal validation_attempts
        validation_attempts += 1
        if validation_attempts == 1:
            raise FileBatchContractViolation(code="file_batch_report.batch_id_mismatch")

    artifact = await SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(settings, structured_output_retries=1),
    )._role(
        context,
        role="engineer",
        model_alias="engineer",
        schema=ImplementationPlan,
        messages=[{"role": "system", "content": "test generic correction"}],
        validate_artifact=reject_once,
    )

    assert isinstance(artifact, ImplementationPlan)
    retry_event = next(
        event
        for event in await repository.list_events(run.id)
        if event.kind == "agent.activity" and event.payload.get("action") == "structured_retry"
    )
    assert retry_event.payload == {
        "action": "structured_retry",
        "summary": "The structured hand-off was invalid; requesting a schema-correct response.",
    }
    correction_message = next(
        message["content"]
        for message in model.requests[1][1]
        if message["content"].startswith("Return only a valid ImplementationPlan JSON object")
    )
    assert correction_message == "Return only a valid ImplementationPlan JSON object matching the declared schema."


@pytest.mark.asyncio
async def test_repeated_system_managed_file_plan_fails_without_sandbox(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-system-path-failure", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    responses = _responses()
    responses["architect"] = [
        _architect_response_with_system_managed_path("nested/.gitignore"),
        _architect_response_with_system_managed_path("nested/.gitignore"),
    ]
    model = ScriptedModelClient(responses)
    sandbox = FakeSandboxProvider()

    with pytest.raises(SOPExecutionError, match="architect failed to produce a valid TechnicalSpec"):
        await SOPRunner(
            repository,
            model,
            sandbox,
            replace(settings, structured_output_retries=1),
        ).run(run.id)

    assert model.calls == [
        ("pm", "ProductSpec"),
        ("architect", "TechnicalSpec"),
        ("architect", "TechnicalSpec"),
    ]
    assert sandbox.sandboxes == {}
    events = await repository.list_events(run.id)
    assert not any(
        event.kind == "agent.activity" and event.payload.get("action") == "sandbox_created"
        for event in events
    )
    assert sum(
        1
        for event in events
        if event.kind == "agent.activity"
        and event.role == "architect"
        and event.payload.get("action") == "structured_retry"
    ) == 1


@pytest.mark.asyncio
async def test_real_metagpt_roles_exchange_persisted_artifact_references(repository, settings) -> None:
    """Exercise installed MetaGPT classes; no Role/Action/Message is mocked."""
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-metagpt", "Create a book management system"
    )
    responses = _responses()
    responses["engineer"] = _two_batch_engineer_cycle()
    model = ScriptedModelClient(responses)
    metagpt_settings = replace(settings, agent_framework="metagpt")
    worker = WorkerRunner(
        repository,
        metagpt_settings,
        model=model,
        sandbox=FakeSandboxProvider(),
        worker_id="test-worker",
    )
    assert isinstance(worker.agent_adapter, MetaGPTAdapter)
    adapter = worker.agent_adapter

    assert await worker.run_once()

    final = await repository.get_run(run.id)
    assert final.status.value == "succeeded"
    assert [invocation.role for invocation in adapter.invocations] == [
        "product_manager",
        "architect",
        "engineer",
        "engineer",
        "engineer",
        "engineer",
        "reviewer",
    ]
    for invocation in adapter.invocations:
        assert isinstance(invocation.role_instance, adapter.runtime_types.role_base)
        assert isinstance(invocation.action, adapter.runtime_types.action_base)
        assert isinstance(invocation.output_message, adapter.runtime_types.message_type)

    product_handoff = adapter.handoff(run.id, "product_manager")
    assert product_handoff is not None
    product_reference = product_handoff.metadata["fomo"]["artifact"]
    assert set(product_reference) == {"artifactId", "artifactKind", "role", "summary"}
    assert product_reference["artifactKind"] == "product_spec"
    assert "Readers need to manage books." not in product_handoff.content
    assert "acceptanceCriteria" not in product_handoff.content

    architect_invocation = next(
        invocation for invocation in adapter.invocations if invocation.role == "architect"
    )
    assert product_reference["artifactId"] in architect_invocation.upstream_artifact_ids
    assert len(architect_invocation.input_messages) == 1
    assert architect_invocation.input_messages[0].metadata["fomo"]["artifact"] == product_reference

    architect_request = next(request for request in model.requests if request[0] == "architect")
    coordination_messages = [
        message
        for message in architect_request[1]
        if message["content"].startswith("MetaGPT coordination envelope")
    ]
    assert len(coordination_messages) == 1
    assert product_reference["artifactId"] in coordination_messages[0]["content"]
    assert "Readers need to manage books." not in coordination_messages[0]["content"]

    engineer_invocations = [invocation for invocation in adapter.invocations if invocation.role == "engineer"]
    assert [invocation.action._schema.__name__ for invocation in engineer_invocations] == [
        "ImplementationPlan",
        "FileBatchReport",
        "FileBatchReport",
        "ImplementationReport",
    ]
    assert engineer_invocations[0].output_message.metadata["fomo"]["kind"] == "intermediate_artifact"
    assert engineer_invocations[1].output_message.metadata["fomo"]["kind"] == "intermediate_artifact"
    assert engineer_invocations[2].output_message.metadata["fomo"]["kind"] == "intermediate_artifact"
    engineer_handoff = adapter.handoff(run.id, "engineer")
    assert engineer_handoff is engineer_invocations[3].output_message
    assert engineer_handoff.metadata["fomo"]["artifact"]["artifactKind"] == "implementation_report"


@pytest.mark.asyncio
async def test_metagpt_model_failure_uses_controlled_message_and_fomo_retry(
    repository, settings, monkeypatch
) -> None:
    secret_marker = "test-secret-never-log-this"
    responses = _responses()
    responses["pm"] = [ModelError(secret_marker), responses["pm"]]
    model = ScriptedModelClient(responses)
    metagpt_settings = replace(
        settings,
        agent_framework="metagpt",
        structured_output_retries=1,
    )
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-metagpt-retry", "Create a book management system"
    )
    worker = WorkerRunner(
        repository,
        metagpt_settings,
        model=model,
        sandbox=FakeSandboxProvider(),
        worker_id="test-worker",
    )
    adapter = worker.agent_adapter

    class _GuardLogger:
        exception_calls = 0

        def exception(self, *args, **kwargs) -> None:
            self.exception_calls += 1

        def __getattr__(self, _name: str):
            return lambda *args, **kwargs: None

    guard_logger = _GuardLogger()
    role_module = sys.modules[adapter.runtime_types.role_base.__module__]
    monkeypatch.setattr(role_module, "logger", guard_logger)
    controlled_messages = []
    original_run = adapter.runtime_types.role_base.run

    async def capture_run(role, *args, **kwargs):
        message = await original_run(role, *args, **kwargs)
        if message is not None:
            controlled_messages.append(message)
        return message

    monkeypatch.setattr(adapter.runtime_types.role_base, "run", capture_run)

    assert await worker.run_once()

    final = await repository.get_run(run.id)
    assert final.status.value == "succeeded"
    assert [alias for alias, _schema in model.calls] == [
        "pm",
        "pm",
        "architect",
        "engineer",
        "engineer",
        "engineer",
        "engineer",
        "reviewer",
    ]
    assert guard_logger.exception_calls == 0
    assert controlled_messages
    controlled_failure = controlled_messages[0]
    assert controlled_failure.metadata["fomo"] == {
        "kind": "artifact_error",
        "role": "product_manager",
    }
    assert secret_marker not in controlled_failure.content
    assert secret_marker not in controlled_failure.model_dump_json()
    events = await repository.list_events(run.id)
    retry_event = next(
        event
        for event in events
        if event.kind == "agent.activity" and event.payload.get("action") == "structured_retry"
    )
    assert "reasonCode" not in retry_event.payload
    pm_retry_request = [request for request in model.requests if request[0] == "pm"][1]
    correction_message = next(
        message["content"]
        for message in pm_retry_request[1]
        if message["content"].startswith("Return only a valid ProductSpec JSON object")
    )
    assert correction_message == "Return only a valid ProductSpec JSON object matching the declared schema."
    assert secret_marker not in correction_message
    assert secret_marker not in json.dumps([event.payload for event in events])


@pytest.mark.asyncio
async def test_gateway_failure_does_not_spend_structured_output_retry(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-gateway-failure", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    model = ScriptedModelClient(
        {
            "pm": [
                ModelRequestError("model request failed after 3 attempts (retryable gateway response)"),
                _responses()["pm"],
            ]
        }
    )
    runner = SOPRunner(
        repository,
        model,
        FakeSandboxProvider(),
        replace(settings, structured_output_retries=2),
    )

    with pytest.raises(SOPExecutionError, match="product_manager model request failed"):
        await runner.run(run.id)

    assert [alias for alias, _schema in model.calls] == ["pm"]
    events = await repository.list_events(run.id)
    assert not any(
        event.kind == "agent.activity" and event.payload.get("action") == "structured_retry"
        for event in events
    )


@pytest.mark.asyncio
async def test_sop_persists_safe_model_transport_retry_telemetry(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-retry-telemetry", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)

    await SOPRunner(
        repository,
        _RetryReportingScriptedModelClient(_responses()),
        FakeSandboxProvider(),
        settings,
    ).run(run.id)

    retry_event = next(
        event
        for event in await repository.list_events(run.id)
        if event.kind == "agent.activity" and event.payload.get("action") == "model_transport_retry"
    )
    assert retry_event.payload == {
        "action": "model_transport_retry",
        "attempt": 1,
        "maxAttempts": 3,
        "delaySeconds": 0.5,
        "failureKind": "gateway_status",
        "statusCode": 504,
    }


@pytest.mark.asyncio
async def test_engineer_batch_failure_keeps_prior_batch_durable_in_the_current_run(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-batch-failure", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    responses = _responses()
    engineer_cycle = _two_batch_engineer_cycle()
    responses["engineer"] = [
        engineer_cycle[0],
        engineer_cycle[1],
        ModelRequestError(
            "model request failed after 3 attempts (retryable gateway response)",
            attempts=3,
            failure_kind="gateway_status",
            status_code=504,
        ),
    ]
    runner = SOPRunner(repository, ScriptedModelClient(responses), FakeSandboxProvider(), settings)

    with pytest.raises(SOPExecutionError, match="engineer model request failed"):
        await runner.run(run.id)

    final = await repository.get_run(run.id)
    assert final.status.value == "failed"
    events = await repository.list_events(run.id)
    persisted = [
        event
        for event in events
        if event.kind == "agent.activity" and event.payload.get("action") == "implementation_batch_persisted"
    ]
    assert len(persisted) == 1
    assert await repository.get_latest_artifact(run.id, "implementation_batch") is not None
    failed = next(event for event in events if event.kind == "agent.failed" and event.role == "engineer")
    assert failed.payload == {
        "role": "engineer",
        "errorType": "ModelRequestError",
        "attempts": 3,
        "failureKind": "gateway_status",
        "statusCode": 504,
    }


@pytest.mark.asyncio
async def test_blocking_typecheck_routes_one_repair_to_engineer(repository, settings) -> None:
    class LockfileSandbox(FakeSandboxProvider):
        async def create(self, project_id, source=None):
            ref = await super().create(project_id, source)
            self._sandbox(ref).files["pnpm-lock.yaml"] = b"lockfileVersion: '9.0'\n"
            return ref

    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "message-repair", "Create a book management system"
    )
    assert await repository.claim_next_run("test-worker", 60)
    model = ScriptedModelClient(_repair_responses())
    sandbox = LockfileSandbox(
        {
            "pnpm typecheck": [
                ExecResult(1, "", "Type error in components/features/library.tsx"),
                ExecResult(0, "ok\n", ""),
            ]
        }
    )
    await SOPRunner(repository, model, sandbox, settings).run(run.id)

    final = await repository.get_run(run.id)
    assert final.status.value == "succeeded"
    assert final.repair_round == 1
    assert [alias for alias, _schema in model.calls] == [
        "pm",
        "architect",
        "engineer",
        "engineer",
        "engineer",
        "engineer",
        "reviewer",
        "engineer",
        "engineer",
        "engineer",
        "reviewer",
    ]
    commands = next(iter(sandbox.sandboxes.values())).commands
    assert commands.count("pnpm install --frozen-lockfile") == 1
    assert commands.count("pnpm install --no-frozen-lockfile") == 1
    assert "pnpm install --frozen-lockfile" not in commands[
        commands.index("pnpm install --no-frozen-lockfile") + 1 :
    ]


def test_state_machine_and_failure_router() -> None:
    state = SOPStateMachine()
    assert state.transition(RunPhase.queued, RunPhase.product_analysis) == RunPhase.product_analysis
    report = DiagnosticReport(
        blocking_issues=["Missing acceptance requirement for search"],
        suggested_fix="define it",
    )
    assert FailureRouter().route(report) == "product_manager"
