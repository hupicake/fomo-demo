"""WorkspaceManager contract tests: mutable Base Snapshot, real diff audit,
cross-version deletion persistence, frozen verification snapshot binding, and
fail-safe sandbox cleanup."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock

import pytest

from fomo.direct_pi.acceptance import (
    ACCEPTANCE_ROOT,
    ADVISORY_ACCEPTANCE_CONFIG_PATH,
    compile_acceptance,
    compile_goal_advisory_acceptance,
)
from fomo.direct_pi.contracts import AcceptanceContract
from fomo.direct_pi.execution import CommandExecutor
from fomo.direct_pi.workspace import (
    FOMO_RUNNER_BIN,
    AuditedWorkspace,
    WorkspaceContractError,
    WorkspaceManager,
)
from fomo.sandbox.base import ExecResult, FileChange
from fomo.starter import resolve_starter_manifest
from tests.helpers import create_user_session

from ._git_sandbox import CANDIDATE_SHA, GitAwareSandbox, persisted_sandbox_id

_DELETED_STARTER_FILE = "components/ui/badge.tsx"


def _contract() -> AcceptanceContract:
    return AcceptanceContract.model_validate(
        {
            "criteria": [
                {
                    "id": "AC-1",
                    "title": "Search books",
                    "priority": "must",
                    "given": "The library is open",
                    "when": "A search is submitted",
                    "then": "Matches appear",
                }
            ],
            "tests": [
                {
                    "id": "search-books",
                    "acceptanceId": "AC-1",
                    "title": "searches books",
                    "actions": [{"kind": "goto", "path": "/"}],
                    "assertions": [
                        {
                            "kind": "visible",
                            "target": {"by": "role", "value": "heading", "name": "Library"},
                        }
                    ],
                }
            ],
        }
    )


class _RecordingKillSandbox(GitAwareSandbox):
    """GitAwareSandbox that records killed sandbox ids for cleanup asserts."""

    def __init__(self, command_results=None) -> None:
        super().__init__(command_results)
        self.killed: list[str] = []

    async def kill(self, ref) -> None:
        self.killed.append(ref.id)
        await super().kill(ref)


async def _run_context(repository, message_id: str = "workspace-test"):
    session = await create_user_session(repository)
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, message_id, "Build a library manager."
    )
    claimed = await repository.claim_next_run(f"workspace-{message_id}", 60)
    assert claimed is not None and claimed.lease_owner
    return project, run, claimed.lease_owner


def _manager(repository, sandbox, settings, run_id: str, project_id: str, lease_token: str):
    commands = CommandExecutor(
        repository,
        sandbox,
        settings,
        run_id=run_id,
        lease_token=lease_token,
    )
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    return WorkspaceManager(
        repository,
        sandbox,
        settings,
        commands,
        starter,
        run_id=run_id,
        project_id=project_id,
        lease_token=lease_token,
    )


@pytest.mark.asyncio
async def test_audit_transfers_only_real_changed_new_and_deleted_files(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "audit-diff")
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)

    await sandbox.apply_changes(
        generation,
        [
            FileChange(path="package.json", content='{"name":"modified"}', operation="modify"),
            FileChange(path="lib/domain/new.ts", content="export const fresh = 1;\n"),
            FileChange(path=_DELETED_STARTER_FILE, operation="delete"),
        ],
    )
    audited = await workspaces.audit(generation, baseline=baseline)

    assert set(audited.changed_paths) == {
        "package.json",
        "lib/domain/new.ts",
        _DELETED_STARTER_FILE,
    }
    by_path = {change.path: change for change in audited.model_changes}
    assert by_path["package.json"].operation == "create"
    assert by_path["lib/domain/new.ts"].operation == "create"
    assert by_path[_DELETED_STARTER_FILE].operation == "delete"
    # Unchanged files never enter the candidate diff.
    assert "app/layout.tsx" not in by_path
    assert len(audited.model_changes) == 3


@pytest.mark.asyncio
async def test_next_env_runtime_rewrite_is_not_candidate_or_checkpoint_truth(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "next-env-generated")
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)
    dev_next_env = (
        '/// <reference types="next" />\n'
        '/// <reference types="next/image-types/global" />\n'
        'import "./.next/dev/types/routes.d.ts";\n'
        'import "./.next/dev/types/root-params.d.ts";\n'
    )

    # Next 16 rewrites this file when a candidate starts `next dev` in G.
    await sandbox.apply_changes(
        generation,
        [FileChange(path="next-env.d.ts", content=dev_next_env)],
    )

    audited = await workspaces.audit(generation, baseline=baseline)
    assert "next-env.d.ts" not in audited.changed_paths
    assert "next-env.d.ts" not in {change.path for change in audited.model_changes}

    checkpoint = await workspaces.capture_candidate_checkpoint(generation)
    assert "next-env.d.ts" not in {str(item["path"]) for item in checkpoint.files}

    snapshot = await workspaces.create_verification(
        audited,
        compile_acceptance(_contract()),
        base_version_id=run.base_version_id,
    )
    starter_next_env = next(
        item for item in workspaces.starter.files if item.path == "next-env.d.ts"
    )
    assert await sandbox.read_file(snapshot.ref, "next-env.d.ts") == starter_next_env._content
    assert snapshot.initial_hashes["next-env.d.ts"] == starter_next_env.sha256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret_path",
    [
        ".env",
        "config/.env.local",
        ".envrc",
        "config/.env.local/secret",
    ],
)
async def test_audit_rejects_env_secret_files_even_when_present(
    repository, settings, secret_path
) -> None:
    _project, run, lease = await _run_context(
        repository, f"audit-secret-{secret_path.replace('/', '-')}"
    )
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)
    # Poison the workspace directly: the sandbox API refuses .env* paths, so
    # the settle audit is the last line of defense.
    sandbox.sandboxes[generation.id].files[secret_path] = b"SECRET=1"

    with pytest.raises(WorkspaceContractError, match="rejected secret file") as caught:
        await workspaces.audit(generation, baseline=baseline)
    assert caught.value.repair is not None
    assert caught.value.repair.code == "rejected_secret_file"
    assert caught.value.repair.affected_files == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "operation", "content"),
    [
        (
            "tests/harness/starter.smoke.spec.ts",
            "modify",
            "test('tampered', async () => {});\n",
        ),
        (
            "tests/fomo-acceptance/injected.smoke.spec.ts",
            "create",
            "test('injected', async () => {});\n",
        ),
        ("tests/harness/starter.smoke.spec.ts", "delete", ""),
    ],
    ids=("modify-harness", "add-acceptance", "delete-harness"),
)
async def test_audit_rejects_fomo_owned_changes_and_keeps_unchanged_out_of_diff(
    repository, settings, path, operation, content
) -> None:
    _project, run, lease = await _run_context(
        repository, f"audit-fomo-owned-{operation}-{path.rsplit('/', 1)[-1]}"
    )
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)

    audited = await workspaces.audit(generation, baseline=baseline)
    assert "tests/harness/starter.smoke.spec.ts" not in audited.changed_paths

    await sandbox.apply_changes(
        generation,
        [FileChange(path=path, content=content, operation=operation)],
    )
    with pytest.raises(WorkspaceContractError, match="FOMO-owned"):
        await workspaces.audit(generation, baseline=baseline)


@pytest.mark.asyncio
async def test_generation_advisory_is_protected_and_excluded_from_candidate_truth(
    repository, settings
) -> None:
    project, run, lease = await _run_context(repository, "goal-advisory")
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)
    compiled = compile_goal_advisory_acceptance("G-1", _contract())

    refreshed, command = await workspaces.reconcile_generation_advisory(
        generation,
        compiled,
        baseline=baseline,
    )

    expected_paths = set(compiled.sha256_by_path)
    assert expected_paths == {
        ADVISORY_ACCEPTANCE_CONFIG_PATH,
        "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts",
    }
    assert {
        path for path in refreshed if path.startswith(f"{ACCEPTANCE_ROOT}/")
    } == expected_paths
    assert f"{FOMO_RUNNER_BIN}/tsc --noEmit" in command
    assert f"{FOMO_RUNNER_BIN}/playwright test" in command
    assert "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts" in command
    assert f"--config={ADVISORY_ACCEPTANCE_CONFIG_PATH}" in command
    assert "--project=chromium --workers=1 --retries=0 --reporter=line" in command
    advisory_config = next(
        change.content
        for change in compiled.changes
        if change.path == ADVISORY_ACCEPTANCE_CONFIG_PATH
    )
    assert 'cwd: "../.."' in advisory_config
    for path, digest in compiled.sha256_by_path.items():
        assert hashlib.sha256(await sandbox.read_file(generation, path)).hexdigest() == digest

    audited = await workspaces.audit(generation, baseline=refreshed)
    assert not any(path.startswith(f"{ACCEPTANCE_ROOT}/") for path in audited.changed_paths)
    assert not any(
        change.path.startswith(f"{ACCEPTANCE_ROOT}/")
        for change in audited.model_changes
    )
    checkpoint = await workspaces.capture_candidate_checkpoint(generation)
    assert not any(
        str(item["path"]).startswith(f"{ACCEPTANCE_ROOT}/")
        for item in checkpoint.files
    )


@pytest.mark.asyncio
async def test_generation_advisory_replaces_prior_goal_and_fails_closed_on_tamper(
    repository, settings
) -> None:
    project, run, lease = await _run_context(repository, "goal-advisory-transition")
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)
    first = compile_goal_advisory_acceptance("G-1", _contract())
    baseline, _command = await workspaces.reconcile_generation_advisory(
        generation,
        first,
        baseline=baseline,
    )
    second = compile_goal_advisory_acceptance("G-2", _contract())
    baseline, _command = await workspaces.reconcile_generation_advisory(
        generation,
        second,
        baseline=baseline,
    )

    first_path = next(
        path for path in first.sha256_by_path if path != ADVISORY_ACCEPTANCE_CONFIG_PATH
    )
    second_path = next(
        path for path in second.sha256_by_path if path != ADVISORY_ACCEPTANCE_CONFIG_PATH
    )
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file(generation, first_path)
    assert await sandbox.read_file(generation, second_path)
    assert first_path not in baseline
    assert baseline[second_path] == second.sha256_by_path[second_path]

    await sandbox.apply_changes(
        generation,
        [FileChange(path=second_path, content="// tampered\n", operation="modify")],
    )
    baseline, _command = await workspaces.reconcile_generation_advisory(
        generation,
        second,
        baseline=baseline,
    )
    assert hashlib.sha256(await sandbox.read_file(generation, second_path)).hexdigest() == (
        second.sha256_by_path[second_path]
    )

    await sandbox.apply_changes(
        generation,
        [FileChange(path=second_path, content="// tampered again\n", operation="modify")],
    )

    with pytest.raises(WorkspaceContractError) as caught:
        await workspaces.audit(generation, baseline=baseline)
    assert caught.value.repair is not None
    assert caught.value.repair.restore_protected_files
    assert await workspaces.restore_generation_protected_files(
        generation,
        second,
        baseline=baseline,
    )
    await workspaces.audit(generation, baseline=baseline)


@pytest.mark.asyncio
async def test_audit_does_not_impose_file_size_or_changed_source_quotas(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "audit-limits")
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)

    large_lockfile = "lockfileVersion: 9.0\n" + ("x" * 30_000)
    await sandbox.apply_changes(
        generation,
        [FileChange(path="pnpm-lock.yaml", content=large_lockfile, operation="modify")],
    )
    audited = await workspaces.audit(generation, baseline=baseline)
    assert "pnpm-lock.yaml" in audited.changed_paths

    oversized_lockfile = "lockfileVersion: 9.0\n" + ("x" * (512 * 1024))
    await sandbox.apply_changes(
        generation,
        [FileChange(path="pnpm-lock.yaml", content=oversized_lockfile, operation="modify")],
    )
    audited = await workspaces.audit(generation, baseline=baseline)
    assert "pnpm-lock.yaml" in audited.changed_paths

    # Character count alone would accept this value, but its UTF-8 encoding
    # exceeds the 512 KiB persistence boundary.
    multibyte_lockfile = "lockfileVersion: 9.0\n" + ("锁" * 180_000)
    assert len(multibyte_lockfile) < 512 * 1024
    assert len(multibyte_lockfile.encode("utf-8")) > 512 * 1024
    await sandbox.apply_changes(
        generation,
        [
            FileChange(
                path="pnpm-lock.yaml",
                content=multibyte_lockfile,
                operation="modify",
            )
        ],
    )
    audited = await workspaces.audit(generation, baseline=baseline)
    assert "pnpm-lock.yaml" in audited.changed_paths

    # Restore the smaller lockfile before checking an ordinary source file.
    await sandbox.apply_changes(
        generation,
        [FileChange(path="pnpm-lock.yaml", content=large_lockfile, operation="modify")],
    )

    await sandbox.apply_changes(
        generation,
        [FileChange(path="app/layout.tsx", content="x" * 25_000, operation="modify")],
    )
    audited = await workspaces.audit(generation, baseline=baseline)
    assert {"app/layout.tsx", "pnpm-lock.yaml"}.issubset(audited.changed_paths)


@pytest.mark.asyncio
async def test_audit_timeout_is_not_sent_to_the_model_for_repair(
    repository, settings
) -> None:
    project, run, lease = await _run_context(repository, "audit-timeout")
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)
    workspaces.commands.run = AsyncMock(
        return_value=ExecResult(exit_code=-1, stdout="", stderr="", timed_out=True)
    )

    with pytest.raises(WorkspaceContractError, match="audit timed out") as caught:
        await workspaces.audit(generation, baseline=baseline)
    assert caught.value.repair is None


@pytest.mark.asyncio
async def test_seed_restores_version_files_and_persists_deletions_across_runs(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "seed-delete")
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)

    original = await sandbox.create(_project.id)
    await workspaces._seed(original, base_version_id=None)
    listed = await workspaces._list_files(original)
    assert any(str(item["path"]) == _DELETED_STARTER_FILE for item in listed)

    modified_package = '{"name":"version-two"}'
    files: list[dict[str, object]] = []
    for item in listed:
        path = str(item["path"])
        if path == _DELETED_STARTER_FILE:
            continue
        entry = dict(item)
        if path == "package.json":
            entry["content_text"] = modified_package
            entry["sha256"] = hashlib.sha256(modified_package.encode()).hexdigest()
            entry["size"] = len(modified_package)
        files.append(entry)
    version = await repository.create_version(
        run.id,
        commit_sha=CANDIDATE_SHA,
        qa_status="passed",
        files=files,
        lease_token=lease,
    )

    restored = await sandbox.create(_project.id)
    await workspaces._seed(restored, base_version_id=version.id)

    restored_files = {str(item["path"]) for item in await workspaces._list_files(restored)}
    assert _DELETED_STARTER_FILE not in restored_files
    package = await sandbox.read_file(restored, "package.json")
    assert package == modified_package.encode()
    # FOMO-owned harness survives from the trusted starter seed.
    assert "tests/harness/starter.smoke.spec.ts" in restored_files


@pytest.mark.asyncio
async def test_create_verification_freezes_manifest_bound_to_clean_head(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "snapshot")
    sandbox = GitAwareSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)
    await sandbox.apply_changes(
        generation,
        [FileChange(path="lib/domain/books.ts", content="export type Book = string;\n")],
    )
    audited = await workspaces.audit(generation, baseline=baseline)
    compiled = compile_acceptance(_contract())

    snapshot = await workspaces.create_verification(
        audited, compiled, base_version_id=run.base_version_id
    )

    assert snapshot.commit_sha == CANDIDATE_SHA
    assert snapshot.initial_hashes == {
        str(item["path"]): str(item["sha256"]) for item in snapshot.initial_files
    }
    live = await workspaces.snapshot_hashes(snapshot.ref)
    assert live == snapshot.initial_hashes
    assert any(
        str(item["path"]) == "tests/fomo-acceptance/fomo.config.ts"
        for item in snapshot.initial_files
    )
    binding_commands = [
        command
        for command in sandbox.sandboxes[snapshot.ref.id].commands
        if command.startswith("test ") and "git status --porcelain" in command
    ]
    assert len(binding_commands) == 1
    assert CANDIDATE_SHA in binding_commands[0]
    assert "--untracked-files=all" in binding_commands[0]

    await sandbox.apply_changes(
        snapshot.ref,
        [FileChange(path="lib/domain/books.ts", content="export type Book = number;\n")],
    )
    with pytest.raises(WorkspaceContractError, match="snapshot drift"):
        await workspaces.assert_unchanged(
            snapshot.ref,
            snapshot.initial_hashes,
            context="verification snapshot drift detected",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_failure", ["dirty worktree", "HEAD mismatch"])
async def test_create_verification_fail_safe_on_invalid_binding(
    repository, settings, binding_failure
) -> None:
    _project, run, lease = await _run_context(
        repository, f"snapshot-{binding_failure.replace(' ', '-')}"
    )

    class _UnboundWorktreeSandbox(_RecordingKillSandbox):
        async def exec(self, ref, command, sink):
            text = command.command
            if text.startswith("test ") and '"$(git rev-parse HEAD)"' in text:
                self._sandbox(ref).commands.append(text)
                return ExecResult(1, "", binding_failure)
            return await super().exec(ref, command, sink)

    sandbox = _UnboundWorktreeSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)
    generation = await workspaces.create_generation(run.base_version_id)
    baseline = await workspaces.snapshot_hashes(generation)
    await sandbox.apply_changes(
        generation,
        [FileChange(path="lib/domain/books.ts", content="export type Book = string;\n")],
    )
    audited = await workspaces.audit(generation, baseline=baseline)
    compiled = compile_acceptance(_contract())

    with pytest.raises(WorkspaceContractError, match="not bound"):
        await workspaces.create_verification(
            audited, compiled, base_version_id=run.base_version_id
        )
    # The fresh verification sandbox was killed while the live generation
    # reference remained authoritative.
    sandbox_ids = list(sandbox.sandboxes)
    assert len(sandbox_ids) == 2  # generation + failed verification
    assert sandbox.killed == [sandbox_ids[-1]]
    assert await persisted_sandbox_id(repository, run.id) == generation.id


@pytest.mark.asyncio
async def test_create_generation_fail_safe_cleans_up_on_persist_failure(
    repository, settings, monkeypatch
) -> None:
    _project, run, lease = await _run_context(repository, "generation-cleanup")
    sandbox = _RecordingKillSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)
    monkeypatch.setattr(
        repository,
        "store_artifact",
        AsyncMock(side_effect=RuntimeError("persist exploded")),
    )

    with pytest.raises(RuntimeError, match="persist exploded"):
        await workspaces.create_generation(run.base_version_id)

    assert len(sandbox.killed) == 1
    assert await persisted_sandbox_id(repository, run.id) is None


@pytest.mark.asyncio
async def test_create_verification_fail_safe_cleans_up_on_seed_failure(
    repository, settings, monkeypatch
) -> None:
    _project, run, lease = await _run_context(repository, "verification-cleanup")
    sandbox = _RecordingKillSandbox()
    workspaces = _manager(repository, sandbox, settings, run.id, _project.id, lease)
    compiled = compile_acceptance(_contract())
    audited = AuditedWorkspace(
        files=(),
        model_changes=(),
        changed_paths=(),
    )
    monkeypatch.setattr(
        workspaces,
        "_seed",
        AsyncMock(side_effect=WorkspaceContractError("seed exploded")),
    )

    with pytest.raises(WorkspaceContractError, match="seed exploded"):
        await workspaces.create_verification(audited, compiled, base_version_id=None)

    assert len(sandbox.killed) == 1
    assert await persisted_sandbox_id(repository, run.id) is None
