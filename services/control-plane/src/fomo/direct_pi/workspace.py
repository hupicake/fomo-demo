"""Immutable starter seeding, candidate audit, and G-to-V workspace transfer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fomo.config import Settings
from fomo.persistence import Repository
from fomo.sandbox.base import FileChange, SandboxProvider, SandboxRef
from fomo.starter import StarterIntegrityError, StarterManifest

from .acceptance import CompiledAcceptance
from .execution import CommandExecutor

SYSTEM_GITIGNORE_PATH = ".gitignore"
SYSTEM_GITIGNORE = """# FOMO system safety baseline
node_modules/
.next/
dist/
build/
coverage/
playwright-report/
test-results/
blob-report/
*.log
.env
.env.*
"""
_REGULAR_SOURCE_TREE = (
    "if find . -path './.git' -prune -o -path './node_modules' -prune "
    "-o -path './.next' -prune -o \\( ! -type f ! -type d \\) -print -quit | grep -q .; "
    "then exit 1; fi"
)


class WorkspaceContractError(RuntimeError):
    """The candidate violated a server-owned workspace boundary."""


@dataclass(frozen=True, slots=True)
class AuditedWorkspace:
    files: tuple[dict[str, object], ...]
    model_changes: tuple[FileChange, ...]
    changed_paths: tuple[str, ...]


class WorkspaceManager:
    def __init__(
        self,
        repository: Repository,
        sandbox: SandboxProvider,
        settings: Settings,
        commands: CommandExecutor,
        starter: StarterManifest,
        *,
        run_id: str,
        project_id: str,
        lease_token: str,
    ) -> None:
        self.repository = repository
        self.sandbox = sandbox
        self.settings = settings
        self.commands = commands
        self.starter = starter
        self.run_id = run_id
        self.project_id = project_id
        self.lease_token = lease_token

    async def create_generation(self, base_version_id: str | None) -> SandboxRef:
        ref = await self.sandbox.create(self.project_id)
        await self.repository.set_sandbox_id(
            self.run_id, ref.id, lease_token=self.lease_token
        )
        await self._seed(ref, base_version_id=base_version_id)
        commit = await self._initialize_git(ref, "chore(starter): prepare Direct Pi workspace")
        await self.repository.store_artifact(
            self.run_id,
            "starter_provenance",
            self.starter.as_provenance(commit),
            lease_token=self.lease_token,
        )
        return ref

    async def create_verification(
        self,
        audited: AuditedWorkspace,
        compiled: CompiledAcceptance,
    ) -> tuple[SandboxRef, str]:
        ref = await self.sandbox.create(self.project_id)
        await self._seed(ref, base_version_id=None)
        await self.sandbox.apply_changes(ref, list(audited.model_changes))
        await self.sandbox.apply_changes(ref, list(compiled.changes))
        await self._verify_protected(ref, compiled)
        commit = await self._initialize_git(
            ref, f"feat(agent): run {self.run_id} candidate implementation"
        )
        await self.repository.set_sandbox_id(
            self.run_id, ref.id, lease_token=self.lease_token
        )
        return ref, commit

    async def freeze_acceptance(
        self,
        ref: SandboxRef,
        compiled: CompiledAcceptance,
    ) -> None:
        await self.sandbox.apply_changes(ref, list(compiled.changes))
        await self._verify_protected(ref, compiled)
        result = await self.commands.run(
            ref,
            "git add -A && git commit -m 'test(fomo): freeze acceptance contract'",
            label="Freeze acceptance contract",
            stage="planning",
        )
        if result.exit_code != 0 or result.timed_out:
            raise WorkspaceContractError("unable to commit the frozen acceptance contract")

    async def snapshot_hashes(self, ref: SandboxRef) -> dict[str, str]:
        return {
            str(item["path"]): str(item["sha256"])
            for item in await self._list_files(ref)
        }

    async def assert_unchanged(
        self, ref: SandboxRef, before: dict[str, str]
    ) -> None:
        if await self.snapshot_hashes(ref) != before:
            raise WorkspaceContractError("Pi changed files during the read-only planning turn")

    async def audit(
        self,
        ref: SandboxRef,
        compiled: CompiledAcceptance,
        *,
        planned_paths: set[str],
    ) -> AuditedWorkspace:
        await self._verify_protected(ref, compiled)
        symlinks = await self.commands.run(
            ref,
            _REGULAR_SOURCE_TREE,
            label="Audit source file types",
            stage="building",
            timeout_seconds=30,
        )
        if symlinks.exit_code != 0 or symlinks.timed_out:
            raise WorkspaceContractError("candidate source contains a symlink")

        listed = await self._list_files(ref)
        starter_paths = {item.path for item in self.starter.files}
        acceptance_paths = set(compiled.sha256_by_path)
        model_entries: list[dict[str, object]] = []
        model_changes: list[FileChange] = []
        paths = {str(item["path"]) for item in listed}
        missing_plan = sorted(planned_paths - paths)
        if missing_plan:
            raise WorkspaceContractError("candidate is missing one or more planned files")

        for item in listed:
            path = str(item["path"])
            if path in acceptance_paths or path == SYSTEM_GITIGNORE_PATH:
                continue
            # The starter's declared extension entry is intentionally both a
            # starter file and model-owned. Its generated replacement must be
            # audited and transferred to V; all other starter files stay
            # immutable and are omitted from the candidate diff.
            if path in starter_paths and not self.starter.is_model_owned_path(path):
                continue
            if not self.starter.is_model_owned_path(path):
                raise WorkspaceContractError(f"candidate added a file outside model-owned roots: {path}")
            if self.starter.is_forbidden_model_owned_path(path):
                raise WorkspaceContractError(f"candidate wrote a forbidden model path: {path}")
            try:
                content = (await self.sandbox.read_file(ref, path)).decode("utf-8", errors="strict")
            except (FileNotFoundError, UnicodeDecodeError) as exc:
                raise WorkspaceContractError("model-owned source must be regular UTF-8 text") from exc
            if "\x00" in content:
                raise WorkspaceContractError("model-owned source must not contain NUL bytes")
            if len(content) > self.settings.pi_max_file_characters:
                raise WorkspaceContractError(
                    f"model-owned source exceeds the file limit: {path}"
                )
            model_entries.append(item)
            model_changes.append(FileChange(path=path, content=content, operation="create"))

        if len(model_entries) > self.settings.pi_max_changed_files:
            raise WorkspaceContractError("candidate exceeds the model-owned file limit")
        actual_model_paths = {str(item["path"]) for item in model_entries}
        unplanned = sorted(actual_model_paths - planned_paths)
        if unplanned:
            raise WorkspaceContractError("candidate contains one or more unplanned source files")
        return AuditedWorkspace(
            files=tuple(listed),
            model_changes=tuple(model_changes),
            # G is untrusted, including its .git directory. The audited file
            # manifest—not model-authored Git metadata—is the candidate truth.
            changed_paths=tuple(sorted(str(item["path"]) for item in model_entries)),
        )

    async def _seed(self, ref: SandboxRef, *, base_version_id: str | None) -> None:
        copy_starter = getattr(self.sandbox, "copy_starter", None)
        if callable(copy_starter):
            result = await copy_starter(ref, self.starter.id, self.starter.capability_ids)
            if result.exit_code != 0 or result.timed_out:
                raise WorkspaceContractError("unable to copy the immutable starter")
        else:
            await self.sandbox.apply_changes(ref, self.starter.file_changes)
        await self._verify_starter(ref)

        if base_version_id is not None:
            changes: list[FileChange] = []
            for item in await self.repository.list_version_files(
                self.project_id, base_version_id
            ):
                path = str(item["path"])
                if not self.starter.is_model_owned_path(path):
                    continue
                _version_id, content, digest = await self.repository.get_version_file_content(
                    self.project_id, path, base_version_id
                )
                if digest != item["sha256"] or hashlib.sha256(content.encode()).hexdigest() != digest:
                    raise WorkspaceContractError("base version source hash mismatch")
                changes.append(FileChange(path=path, content=content, operation="create"))
            if changes:
                await self.sandbox.apply_changes(ref, changes)

        await self.sandbox.apply_changes(
            ref,
            [FileChange(path=SYSTEM_GITIGNORE_PATH, content=SYSTEM_GITIGNORE)],
        )

    async def _initialize_git(self, ref: SandboxRef, message: str) -> str:
        command = (
            "git init && git config user.email fomo@local.invalid && "
            "git config user.name 'FOMO Agent' && git add -A && "
            f"git commit -m {self._shell_single_quote(message)} && git rev-parse HEAD"
        )
        result = await self.commands.run(
            ref,
            command,
            label="Create immutable Git baseline",
            stage="preparing",
        )
        if result.exit_code != 0 or result.timed_out or not result.stdout.strip():
            raise WorkspaceContractError("unable to create the Git baseline")
        return result.stdout.strip().splitlines()[-1]

    async def _verify_starter(self, ref: SandboxRef) -> None:
        try:
            for entry in self.starter.files:
                self.starter.verify_file(
                    entry.path, await self.sandbox.read_file(ref, entry.path)
                )
        except (FileNotFoundError, StarterIntegrityError) as exc:
            raise WorkspaceContractError("immutable starter verification failed") from exc

    async def _verify_protected(
        self, ref: SandboxRef, compiled: CompiledAcceptance
    ) -> None:
        try:
            for entry in self.starter.files:
                if self.starter.is_protected_path(entry.path):
                    self.starter.verify_file(
                        entry.path, await self.sandbox.read_file(ref, entry.path)
                    )
            if await self.sandbox.read_file(ref, SYSTEM_GITIGNORE_PATH) != SYSTEM_GITIGNORE.encode():
                raise WorkspaceContractError("system .gitignore changed")
            for path, digest in compiled.sha256_by_path.items():
                content = await self.sandbox.read_file(ref, path)
                if hashlib.sha256(content).hexdigest() != digest:
                    raise WorkspaceContractError("frozen acceptance test changed")
        except (FileNotFoundError, StarterIntegrityError) as exc:
            raise WorkspaceContractError("protected workspace verification failed") from exc

    async def _list_files(self, ref: SandboxRef) -> list[dict[str, object]]:
        list_files = getattr(self.sandbox, "list_files", None)
        if not callable(list_files):
            raise WorkspaceContractError("sandbox provider cannot list source files")
        files = list(await list_files(ref))
        if not files:
            raise WorkspaceContractError("sandbox workspace is empty")
        return files

    @staticmethod
    def _shell_single_quote(value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"
