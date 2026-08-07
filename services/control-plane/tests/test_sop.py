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
from fomo.agent_runtime.sop import SOPExecutionError, SOPRunner
from fomo.agent_runtime.state import FailureRouter, SOPStateMachine
from fomo.sandbox.base import ExecResult
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.schemas import (
    DiagnosticReport,
    GateResult,
    GateStatus,
    ImplementationPlan,
    RunPhase,
    TechnicalSpec,
)
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
                    "id": "package-scaffold",
                    "purpose": "package configuration",
                    "paths": ["package.json"],
                    "acceptanceIds": ["AC-1"],
                },
                {
                    "id": "library-ui",
                    "purpose": "library interaction",
                    "paths": ["app.js"],
                    "acceptanceIds": ["AC-1"],
                },
            ],
            "designDecisionIds": ["DD-1"],
            "knownLimitations": [],
        },
        {
            "batchId": "package-scaffold",
            "implementedAcceptanceIds": ["AC-1"],
            "designDecisionIds": ["DD-1"],
            "changedFiles": ["package.json"],
            "knownLimitations": [],
            "fileChanges": [
                {
                    "path": "package.json",
                    "operation": "create",
                    "content": '{"scripts":{"typecheck":"node --check app.js","build":"node --check app.js","dev":"node server.js"}}',
                }
            ],
        },
        {
            "batchId": "library-ui",
            "implementedAcceptanceIds": ["AC-1"],
            "designDecisionIds": ["DD-1"],
            "changedFiles": ["app.js"],
            "knownLimitations": [],
            "fileChanges": [
                {"path": "app.js", "operation": "create", "content": "console.log('library')"}
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
                    "paths": ["app.js"],
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
            "changedFiles": ["app.js"],
            "knownLimitations": [],
            "fileChanges": [
                {"path": "app.js", "operation": "modify", "content": "console.log('library repair')"}
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
            "components": [{"name": "Library", "responsibility": "books", "children": []}],
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
                    "filePath": "app.js",
                    "exportStyle": "named",
                    "symbol": "Library",
                    "props": [],
                    "type": "React.ComponentType",
                }
            ],
            "stateModel": [{"name": "books", "owner": "client", "persistence": "local"}],
            "dependencies": [],
            "filePlan": [
                {"path": "package.json", "operation": "create", "reason": "scripts"},
                {"path": "app.js", "operation": "create", "reason": "library interaction"},
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


def _architect_response_with_file_plan_count(count: int) -> dict[str, Any]:
    response = dict(_responses()["architect"])
    response["filePlan"] = [
        {
            "path": f"src/generated/file-{index}.ts",
            "operation": "create",
            "reason": "test-only file-plan capacity fixture",
        }
        for index in range(count)
    ]
    response["publicApiContracts"] = [
        {
            "filePath": "src/generated/file-0.ts",
            "exportStyle": "named",
            "symbol": "GeneratedFile",
            "props": [],
            "type": "unknown",
        }
    ]
    return response


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


def _repair_responses() -> dict[str, Any]:
    responses = _responses()
    responses["engineer"] = [*responses["engineer"], *_single_file_repair_engineer_cycle()]
    initial_reviewer = dict(responses["reviewer"])
    initial_reviewer["locationFiles"] = ["app.js"]
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
        "app.js",
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
    assert "Never plan system-managed .gitignore files" in architect_system_prompt
    assert "pnpm-lock.yaml, package-lock.json, yarn.lock, bun.lock, or bun.lockb" in architect_system_prompt
    assert "0.0.0.0" in architect_system_prompt
    assert "http://127.0.0.1:<port>" in architect_system_prompt
    assert "dynamic hostname" in architect_system_prompt
    assert "React + TypeScript + Tailwind CSS + shadcn/ui + Lucide React" in architect_system_prompt
    assert "componentDecisions" in architect_system_prompt
    assert "publicApiContracts" in architect_system_prompt
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
    engineer_batch_requests = [request for request in model.requests if request[2] == "FileBatchReport"]
    assert len(engineer_batch_requests) == 2
    engineer_batch_system_prompt = engineer_batch_requests[0][1][0]["content"]
    assert "0.0.0.0" in engineer_batch_system_prompt
    assert "http://127.0.0.1:<port>" in engineer_batch_system_prompt
    assert "dynamic hostname" in engineer_batch_system_prompt
    assert "React + TypeScript + Tailwind CSS + shadcn/ui + Lucide React" in engineer_batch_system_prompt
    for engineer_batch_request in engineer_batch_requests:
        assert "Shared public API contracts" in engineer_batch_request[1][1]["content"]
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
                    "id": "package",
                    "purpose": "initial package file",
                    "paths": ["package.json"],
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
                    "paths": ["package.json", "app.js"],
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
                    "id": "scaffold",
                    "purpose": "initial files",
                    "paths": ["package.json"],
                },
                {
                    "id": "app",
                    "purpose": "library application",
                    "paths": ["app.js"],
                },
                {
                    "id": "extra",
                    "purpose": "not authorized by the Architect",
                    "paths": ["server.js"],
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
    diagnostic = DiagnosticReport(location_files=["app.js"])

    scoped_technical = runner._repair_technical(technical, diagnostic)

    assert [item.path for item in scoped_technical.file_plan] == ["app.js"]
    with pytest.raises(ValueError, match="exactly match the architect TechnicalSpec file plan"):
        runner._validate_implementation_plan(
            ImplementationPlan.model_validate(_two_batch_engineer_cycle()[0]),
            scoped_technical,
        )
    with pytest.raises(SOPExecutionError, match="did not identify an approved file scope"):
        runner._repair_technical(technical, DiagnosticReport())


@pytest.mark.asyncio
async def test_repair_scope_includes_only_the_package_manifest_for_dependency_gates(repository, settings) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_responses()["architect"])
    diagnostic = DiagnosticReport(
        gates=[GateResult(gate="dependencies", status=GateStatus.failed, summary="lockfile mismatch")]
    )

    scoped_technical = runner._repair_technical(technical, diagnostic)

    assert [item.path for item in scoped_technical.file_plan] == ["package.json"]


@pytest.mark.asyncio
async def test_architect_contracts_bind_component_decisions_and_public_api_files(repository, settings) -> None:
    runner = SOPRunner(repository, ScriptedModelClient({}), FakeSandboxProvider(), settings)
    technical = TechnicalSpec.model_validate(_responses()["architect"])

    runner._validate_technical_file_plan(technical)
    with pytest.raises(ValueError, match="explain every planned component decision"):
        runner._validate_technical_file_plan(technical.model_copy(update={"component_decisions": []}))
    invalid_contract = technical.public_api_contracts[0].model_copy(update={"file_path": "missing.ts"})
    with pytest.raises(ValueError, match="must reference an Architect-planned file"):
        runner._validate_technical_file_plan(
            technical.model_copy(update={"public_api_contracts": [invalid_contract]})
        )


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

    with pytest.raises(ValueError, match="system-managed path"):
        runner._validate_technical_file_plan(technical)


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
    assert {item["path"] for item in technical["filePlan"]} == {"package.json", "app.js"}
    events = await repository.list_events(run.id)
    assert any(
        event.kind == "agent.activity"
        and event.role == "architect"
        and event.payload.get("action") == "structured_retry"
        for event in events
    )


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
    assert any(
        event.kind == "agent.activity" and event.payload.get("action") == "structured_retry"
        for event in events
    )
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
                ExecResult(1, "", "Type error in app.js"),
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
