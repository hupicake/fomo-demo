"""Base Snapshot seeding, settle safety audit, and G-to-V candidate transfer.

P0 semantics: the Base Snapshot is mutable. Pi has full project development
permission in G (/workspace read/write, package/config/starter included), so
settle audit enforces only real safety invariants:

- normalized in-workspace paths; `.env`/`.env.*` files are rejected outright;
- `.git/**` (G-internal checkpoint) is excluded, never part of the candidate;
- FOMO-owned roots (tests/fomo-acceptance/**, tests/harness/**) and the system
  `.gitignore` stay immutable: present-and-unchanged files are excluded from
  the candidate diff; any add/modify/delete fails the audit;
- the candidate diff contains only real changes: create/modify for files whose
  hash differs from the seed baseline, delete for removed files; unchanged
  files are not transferred;
- changed/new files are not subject to FOMO development quotas; the audit
  still requires complete regular UTF-8 text without NUL bytes.

FOMO-owned acceptance tests are never seeded into G; they are injected by
FOMO into the clean verification sandbox V after the audited candidate diff
is applied.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from fomo.config import Settings
from fomo.persistence import Repository
from fomo.sandbox.base import ExecResult, FileChange, SandboxProvider, SandboxRef
from fomo.starter import StarterIntegrityError, StarterManifest

from .acceptance import (
    ACCEPTANCE_ROOT,
    ADVISORY_ACCEPTANCE_CONFIG_PATH,
    CompiledAcceptance,
)
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
# FOMO-owned roots: never part of the model's candidate diff. Acceptance tests
# are injected into V per run; the harness smoke is part of the immutable seed.
_FOMO_OWNED_ROOTS = (ACCEPTANCE_ROOT, "tests/harness")
# Next owns this declaration file and rewrites its generated type imports when
# switching between `next dev` and `next build`. It is reproducible from the
# trusted starter/runtime, so a G-side rewrite must never become candidate or
# durable checkpoint truth.
_RUNNER_GENERATED_PATHS = frozenset({"next-env.d.ts"})
# FOMO-owned verification runner: fixed absolute binaries in the root-owned,
# read-only runtime cache baked into the sandbox image (self-contained; it
# shares no inode with the candidate-writable pnpm store). The runner entries
# are pnpm-generated ``#!/bin/sh`` wrappers. FOMO invokes their absolute,
# root-owned paths directly with a PATH that contains no node-writable
# directory, so the wrappers resolve the trusted system Node rather than
# anything under PNPM_HOME or the candidate workspace. Both the G-side quick
# typecheck and the authoritative V gates use this single helper, never
# candidate node_modules/.bin resolution. This is container-level hardening:
# candidate Next config/app and tests run inside the same V user/process
# boundary, so it is not host-level/cryptographic anti-tamper (external QA
# runner or read-only test mounts remain the public-deployment blocker).
FOMO_RUNNER_NODE = "/usr/local/bin/node"
FOMO_RUNNER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
FOMO_RUNNER_BIN = "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/.bin"


def fomo_runner_command(*, bin_name: str, args: str) -> str:
    """One fixed prefix for every FOMO-owned runner invocation.

    ``env PATH=<trusted> <absolute .bin wrapper> ...`` defeats PATH hijacking
    through writable tool directories while allowing pnpm's shell wrappers
    to resolve the trusted system Node. ``args`` remains a trusted command
    fragment so verifier placeholders keep their existing contract; the PATH
    and absolute wrapper path are shell-quoted here. The single helper
    prevents command-string drift between the G-side typecheck and the
    authoritative V gates.
    """
    runner_path = f"{FOMO_RUNNER_BIN}/{bin_name}"
    return f"env PATH={shlex.quote(FOMO_RUNNER_PATH)} {shlex.quote(runner_path)} {args}"


class WorkspaceContractError(RuntimeError):
    """The candidate violated a server-owned workspace boundary."""


@dataclass(frozen=True, slots=True)
class AuditedWorkspace:
    files: tuple[dict[str, object], ...]
    model_changes: tuple[FileChange, ...]
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationSnapshot:
    """Frozen V state bound to the initial Git commit.

    ``initial_files`` is the complete manifest the gates run against and the
    only manifest ever persisted for the version; ``initial_hashes`` is
    derived from that same listing. The binding is verified, not atomic: the
    manifest is frozen right after the initial commit and a HEAD + clean
    worktree check runs before any candidate process starts. Publication is
    refused unless the live V still matches ``initial_hashes`` after all
    gates.
    """

    ref: SandboxRef
    commit_sha: str
    initial_files: tuple[dict[str, object], ...]
    initial_hashes: dict[str, str]


@dataclass(frozen=True, slots=True)
class CandidateCheckpoint:
    """Complete model-owned UTF-8 candidate truth for durable recovery."""

    files: tuple[dict[str, object], ...]
    manifest_hash: str


class VerifiedCheckpointLike(Protocol):
    manifest_hash: str
    files: tuple[object, ...]


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
        self._resource_ids: dict[str, str] = {}

    async def create_generation(self, base_version_id: str | None) -> SandboxRef:
        ref = await self.sandbox.create(self.project_id)
        try:
            await self._register_sandbox(ref, "generation")
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
        except BaseException:
            # Fail-safe: the orchestrator never received this ref, so any
            # error/cancellation during seed/commit/persist must not leak the
            # new sandbox. Best-effort kill + reference clear; the original
            # exception is re-raised untouched.
            try:
                await self.sandbox.kill(ref)
            except Exception:
                pass
            else:
                with suppress(Exception):
                    await self._acknowledge_sandbox(ref)
            with suppress(Exception):
                await self.repository.clear_sandbox_id(
                    self.run_id, ref.id, lease_token=self.lease_token
                )
            raise

    async def create_generation_from_checkpoint(
        self,
        checkpoint: VerifiedCheckpointLike,
        *,
        base_version_id: str | None,
    ) -> tuple[SandboxRef, dict[str, str]]:
        """Rebuild a fresh G from durable files, never an orphan sandbox."""

        ref = await self.sandbox.create(self.project_id)
        try:
            await self._register_sandbox(ref, "generation")
            await self.repository.set_sandbox_id(
                self.run_id, ref.id, lease_token=self.lease_token
            )
            await self._seed(ref, base_version_id=base_version_id)
            baseline = await self.snapshot_hashes(ref)
            await self._restore_checkpoint(ref, checkpoint)
            commit = await self._initialize_git(
                ref,
                "chore(checkpoint): restore last verified GoalGraph candidate",
            )
            await self.repository.store_artifact(
                self.run_id,
                "checkpoint_restore",
                {
                    "manifestHash": checkpoint.manifest_hash,
                    "commitSha": commit,
                    "fileCount": len(checkpoint.files),
                },
                lease_token=self.lease_token,
            )
            return ref, baseline
        except BaseException:
            try:
                await self.sandbox.kill(ref)
            except Exception:
                pass
            else:
                with suppress(Exception):
                    await self._acknowledge_sandbox(ref)
            with suppress(Exception):
                await self.repository.clear_sandbox_id(
                    self.run_id, ref.id, lease_token=self.lease_token
                )
            raise

    async def adopt_generation(self, ref: SandboxRef) -> SandboxRef:
        """Reconnect the exact generation sandbox retained for user input."""

        resource_id = await self.repository.require_live_sandbox_resource(
            self.run_id,
            ref.id,
            "generation",
            lease_token=self.lease_token,
        )
        # Connecting is the provider-level existence check. No new sandbox or
        # session directory may be substituted for an answered continuation.
        await self.sandbox.connect(ref)
        self._resource_ids[ref.id] = resource_id
        return ref

    async def create_verification(
        self,
        audited: AuditedWorkspace,
        compiled: CompiledAcceptance,
        *,
        base_version_id: str | None,
    ) -> VerificationSnapshot:
        ref = await self.sandbox.create(self.project_id)
        try:
            await self._register_sandbox(ref, "verification")
            # V is seeded from the same mutable Base Snapshot baseline as G, then
            # receives the complete audited candidate diff (including deletions
            # and changes to starter/config/package/lockfile files), then the
            # FOMO-injected acceptance tests.
            await self._seed(ref, base_version_id=base_version_id)
            await self.sandbox.apply_changes(ref, list(audited.model_changes))
            await self.sandbox.apply_changes(ref, list(compiled.changes))
            await self._verify_protected(ref, compiled)
            commit = await self._initialize_git(
                ref, f"feat(agent): run {self.run_id} candidate implementation"
            )
            # Freeze the initial manifest from this single listing; initial_hashes
            # is derived from the same capture (never a second list).
            initial_files = await self._list_files(ref)
            initial_hashes = {
                str(item["path"]): str(item["sha256"]) for item in initial_files
            }
            # Bind the frozen manifest to the initial commit before any candidate
            # process can start: HEAD must equal the commit and the worktree must
            # be clean. This is a verified binding, not an atomic snapshot.
            binding = await self.commands.run(
                ref,
                (
                    "test \"$(git rev-parse HEAD)\" = "
                    + self._shell_single_quote(commit)
                    + " && test -z \"$(git status --porcelain=v1 --untracked-files=all)\""
                ),
                label="Bind verification manifest to commit",
                stage="verifying",
                timeout_seconds=30,
            )
            if binding.exit_code != 0 or binding.timed_out:
                raise WorkspaceContractError(
                    "verification sandbox is not bound to the frozen commit "
                    "(HEAD mismatch or dirty worktree)"
                )
            await self.repository.set_sandbox_id(
                self.run_id, ref.id, lease_token=self.lease_token
            )
            return VerificationSnapshot(
                ref=ref,
                commit_sha=commit,
                initial_files=tuple(initial_files),
                initial_hashes=initial_hashes,
            )
        except BaseException:
            # Fail-safe: the orchestrator never received this ref, so any
            # error/cancellation during seed/apply/commit/binding must not
            # leak the new sandbox. Best-effort kill + reference clear; the
            # original exception is re-raised untouched.
            try:
                await self.sandbox.kill(ref)
            except Exception:
                pass
            else:
                with suppress(Exception):
                    await self._acknowledge_sandbox(ref)
            with suppress(Exception):
                await self.repository.clear_sandbox_id(
                    self.run_id, ref.id, lease_token=self.lease_token
                )
            raise

    async def destroy(self, ref: SandboxRef | None) -> None:
        """Kill a registered G/V and acknowledge cleanup only after success."""

        if ref is None:
            return
        await self.sandbox.kill(ref)
        await self._acknowledge_sandbox(ref)
        with suppress(Exception):
            await self.repository.clear_sandbox_id(
                self.run_id, ref.id, lease_token=self.lease_token
            )

    async def _register_sandbox(self, ref: SandboxRef, kind: str) -> None:
        if not (
            self.settings.direct_pi_goal_graph_enabled
            and self.settings.agent_framework == "direct_pi"
        ):
            return
        resource_id = await self.repository.register_sandbox_resource(
            self.run_id,
            ref.id,
            kind,
            lease_token=self.lease_token,
        )
        self._resource_ids[ref.id] = resource_id

    async def _acknowledge_sandbox(self, ref: SandboxRef) -> None:
        resource_id = self._resource_ids.pop(ref.id, None)
        if resource_id is not None:
            await self.repository.acknowledge_sandbox_cleanup(resource_id)

    async def typecheck_workspace(self, ref: SandboxRef) -> ExecResult:
        return await self.commands.run(
            ref,
            fomo_runner_command(bin_name="tsc", args="--noEmit"),
            label="Typecheck candidate",
            stage="building",
            timeout_seconds=120,
        )

    async def reconcile_generation_advisory(
        self,
        ref: SandboxRef,
        compiled: CompiledAcceptance,
        *,
        baseline: dict[str, str],
    ) -> tuple[dict[str, str], str]:
        """Install one current-goal self-check without changing candidate truth.

        The advisory specs reuse the frozen acceptance compiler but remain
        untrusted release evidence.  They live under the already protected
        acceptance root, are bound into the settle-audit baseline, and are
        replaced at each goal boundary.  The clean verification sandbox still
        recompiles its own authoritative suite from the persisted GoalGraph.
        """

        expected_hashes = dict(compiled.sha256_by_path)
        expected_paths = set(expected_hashes)
        change_paths = {change.path for change in compiled.changes}
        test_paths = sorted(set(compiled.test_path_by_acceptance_id.values()))
        allowed_paths = {ADVISORY_ACCEPTANCE_CONFIG_PATH, *test_paths}
        if (
            not expected_paths
            or change_paths != expected_paths
            or not test_paths
            or expected_paths != allowed_paths
            or any(not path.startswith(f"{ACCEPTANCE_ROOT}/") for path in expected_paths)
        ):
            raise WorkspaceContractError(
                "generation advisory must contain only its config and current-goal specs"
            )

        previous_hashes = {
            path: digest
            for path, digest in baseline.items()
            if path.startswith(f"{ACCEPTANCE_ROOT}/")
        }
        current_hashes = {
            str(item["path"]): str(item["sha256"])
            for item in await self._list_files(ref)
            if str(item["path"]).startswith(f"{ACCEPTANCE_ROOT}/")
        }
        if current_hashes != previous_hashes:
            raise WorkspaceContractError(
                "generation advisory acceptance specs changed outside FOMO"
            )

        changes = [
            FileChange(path=path, operation="delete")
            for path in sorted(set(previous_hashes) - expected_paths)
        ]
        changes.extend(compiled.changes)
        await self.sandbox.apply_changes(ref, changes)

        installed_hashes: dict[str, str] = {}
        for path in sorted(expected_paths):
            try:
                content = await self.sandbox.read_file(ref, path)
            except FileNotFoundError as exc:
                raise WorkspaceContractError(
                    "generation advisory acceptance spec is missing"
                ) from exc
            digest = hashlib.sha256(content).hexdigest()
            if digest != expected_hashes[path]:
                raise WorkspaceContractError(
                    "generation advisory acceptance spec hash mismatch"
                )
            installed_hashes[path] = digest

        refreshed_baseline = {
            path: digest
            for path, digest in baseline.items()
            if not path.startswith(f"{ACCEPTANCE_ROOT}/")
        }
        refreshed_baseline.update(installed_hashes)
        quoted_tests = " ".join(shlex.quote(path) for path in test_paths)
        typecheck = fomo_runner_command(bin_name="tsc", args="--noEmit")
        playwright = fomo_runner_command(
            bin_name="playwright",
            args=(
                f"test {quoted_tests} "
                f"--config={shlex.quote(ADVISORY_ACCEPTANCE_CONFIG_PATH)} "
                "--project=chromium --workers=1 --retries=0 --reporter=line"
            ),
        )
        return refreshed_baseline, f"{typecheck} && {playwright}"

    async def checkpoint_workspace(self, ref: SandboxRef) -> str:
        result = await self.commands.run(
            ref,
            (
                "git add -A && "
                "git commit -m 'feat(fomo): checkpoint candidate implementation' && "
                "git rev-parse HEAD"
            ),
            label="Checkpoint candidate",
            stage="building",
        )
        if result.exit_code != 0 or result.timed_out or not result.stdout.strip():
            raise WorkspaceContractError("unable to checkpoint the candidate")
        return result.stdout.strip().splitlines()[-1]

    async def capture_candidate_checkpoint(self, ref: SandboxRef) -> CandidateCheckpoint:
        """Capture every candidate-owned file with strict UTF-8 bytes and hashes."""

        listed = await self._list_files(ref)
        files: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in listed:
            path = str(item["path"])
            if self._is_excluded_path(path) or self._is_fomo_owned_path(path):
                continue
            if self._is_secret_path(path):
                raise WorkspaceContractError(
                    f"checkpoint contains a rejected secret file: {path}"
                )
            if path in seen:
                raise WorkspaceContractError("checkpoint file paths must be unique")
            try:
                raw = await self.sandbox.read_file(ref, path)
                content = raw.decode("utf-8", errors="strict")
            except (FileNotFoundError, UnicodeDecodeError) as exc:
                raise WorkspaceContractError(
                    "checkpoint files must be complete UTF-8 text"
                ) from exc
            if b"\x00" in raw:
                raise WorkspaceContractError("checkpoint files must not contain NUL bytes")
            digest = hashlib.sha256(raw).hexdigest()
            listed_digest = item.get("sha256")
            listed_size = item.get("size")
            if listed_digest != digest or listed_size != len(raw):
                raise WorkspaceContractError("checkpoint source changed during capture")
            seen.add(path)
            files.append(
                {
                    "path": path,
                    "contentText": content,
                    "sha256": digest,
                    "size": len(raw),
                }
            )
        if not files:
            raise WorkspaceContractError("checkpoint candidate is empty")
        files.sort(key=lambda item: str(item["path"]).encode("utf-8"))
        manifest_hash = self._candidate_manifest_hash(files)
        return CandidateCheckpoint(files=tuple(files), manifest_hash=manifest_hash)

    async def snapshot_hashes(self, ref: SandboxRef) -> dict[str, str]:
        return {
            str(item["path"]): str(item["sha256"])
            for item in await self._list_files(ref)
        }

    async def assert_unchanged(
        self,
        ref: SandboxRef,
        before: dict[str, str],
        *,
        context: str = "Pi changed files during the read-only planning turn",
    ) -> None:
        """Fail closed when the sandbox's visible source manifest drifts from a
        frozen hash snapshot. Planning uses the default message; publication
        passes a drift-specific context."""
        if await self.snapshot_hashes(ref) != before:
            raise WorkspaceContractError(context)

    async def audit(
        self,
        ref: SandboxRef,
        *,
        baseline: dict[str, str],
    ) -> AuditedWorkspace:
        """Settle audit: safety invariants and the real full-project diff."""
        try:
            if await self.sandbox.read_file(ref, SYSTEM_GITIGNORE_PATH) != SYSTEM_GITIGNORE.encode():
                raise WorkspaceContractError("system .gitignore changed")
        except FileNotFoundError as exc:
            raise WorkspaceContractError("system .gitignore is missing") from exc
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
        current_hashes: dict[str, str] = {
            str(item["path"]): str(item["sha256"]) for item in listed
        }
        changes: list[FileChange] = []
        changed_paths: list[str] = []

        for item in listed:
            path = str(item["path"])
            if self._is_excluded_path(path):
                continue
            if self._is_secret_path(path):
                raise WorkspaceContractError(
                    f"candidate contains a rejected secret file: {path}"
                )
            if self._is_fomo_owned_path(path):
                # FOMO-owned files are allowed only when present in the seed
                # baseline with an unchanged hash; any add/modify/delete is
                # rejected and never enters the candidate diff.
                if baseline.get(path) != current_hashes.get(path):
                    raise WorkspaceContractError(
                        f"candidate added, modified, or deleted a FOMO-owned file: {path}"
                    )
                continue
            if baseline.get(path) == current_hashes.get(path):
                # Unchanged file: not part of the candidate diff.
                continue
            try:
                content = (await self.sandbox.read_file(ref, path)).decode("utf-8", errors="strict")
            except (FileNotFoundError, UnicodeDecodeError) as exc:
                raise WorkspaceContractError(
                    "changed candidate files must be regular UTF-8 text"
                ) from exc
            if "\x00" in content:
                raise WorkspaceContractError("changed candidate files must not contain NUL bytes")
            changes.append(FileChange(path=path, content=content, operation="create"))
            changed_paths.append(path)

        for path in baseline:
            if self._is_excluded_path(path):
                continue
            if self._is_fomo_owned_path(path):
                if path not in current_hashes:
                    raise WorkspaceContractError(
                        f"candidate deleted a FOMO-owned file: {path}"
                    )
                continue
            if path not in current_hashes:
                changes.append(FileChange(path=path, operation="delete"))
                changed_paths.append(path)

        changed_paths.sort()
        return AuditedWorkspace(
            files=tuple(listed),
            model_changes=tuple(changes),
            changed_paths=tuple(changed_paths),
        )

    async def _seed(self, ref: SandboxRef, *, base_version_id: str | None) -> None:
        copy_starter = getattr(self.sandbox, "copy_starter", None)
        if callable(copy_starter):
            result = await copy_starter(ref, self.starter.id, self.starter.capability_ids)
            if result.exit_code != 0 or result.timed_out:
                raise WorkspaceContractError("unable to copy the starter base")
        else:
            await self.sandbox.apply_changes(ref, self.starter.file_changes)
        await self._verify_starter(ref)

        if base_version_id is not None:
            # The version manifest is the complete candidate truth: restore its
            # files over the starter copy, then delete every starter file that
            # the prior verified version removed (explicit normalized
            # FileChange deletes, no tombstones) so deletions survive across
            # runs. FOMO-owned roots are restored from the trusted starter;
            # the system .gitignore is restored by the control plane below.
            manifest = await self.repository.list_version_files(
                self.project_id, base_version_id
            )
            manifest_paths = {str(item["path"]) for item in manifest}
            changes: list[FileChange] = []
            for item in manifest:
                path = str(item["path"])
                if self._is_fomo_owned_path(path) or self._is_excluded_path(path):
                    continue
                _version_id, content, digest = await self.repository.get_version_file_content(
                    self.project_id, path, base_version_id
                )
                if digest != str(item["sha256"]) or hashlib.sha256(content.encode()).hexdigest() != digest:
                    raise WorkspaceContractError("base version source hash mismatch")
                changes.append(FileChange(path=path, content=content, operation="create"))
            for entry in self.starter.files:
                path = entry.path
                if self._is_fomo_owned_path(path) or self._is_excluded_path(path):
                    continue
                if path not in manifest_paths:
                    changes.append(FileChange(path=path, operation="delete"))
            if changes:
                await self.sandbox.apply_changes(ref, changes)

        await self.sandbox.apply_changes(
            ref,
            [FileChange(path=SYSTEM_GITIGNORE_PATH, content=SYSTEM_GITIGNORE)],
        )

    async def _restore_checkpoint(
        self,
        ref: SandboxRef,
        checkpoint: VerifiedCheckpointLike,
    ) -> None:
        expected_files: list[dict[str, object]] = []
        expected_paths: set[str] = set()
        changes: list[FileChange] = []
        for value in checkpoint.files:
            path = getattr(value, "path", None)
            content = getattr(value, "content_text", None)
            digest = getattr(value, "sha256", None)
            size = getattr(value, "size", None)
            if not isinstance(path, str) or not isinstance(content, str):
                raise WorkspaceContractError("durable checkpoint file is invalid")
            if self._is_excluded_path(path) or self._is_fomo_owned_path(path):
                raise WorkspaceContractError("durable checkpoint contains a protected path")
            if self._is_secret_path(path) or path in expected_paths:
                raise WorkspaceContractError("durable checkpoint path is invalid or duplicated")
            raw = content.encode("utf-8", errors="strict")
            actual_digest = hashlib.sha256(raw).hexdigest()
            if digest != actual_digest or size != len(raw):
                raise WorkspaceContractError("durable checkpoint file hash mismatch")
            expected_paths.add(path)
            expected_files.append(
                {
                    "path": path,
                    "sha256": actual_digest,
                    "size": len(raw),
                }
            )
            changes.append(FileChange(path=path, content=content, operation="create"))
        expected_files.sort(key=lambda item: str(item["path"]).encode("utf-8"))
        if not expected_files or self._candidate_manifest_hash(expected_files) != checkpoint.manifest_hash:
            raise WorkspaceContractError("durable checkpoint manifest hash mismatch")

        for item in await self._list_files(ref):
            path = str(item["path"])
            if (
                path not in expected_paths
                and not self._is_excluded_path(path)
                and not self._is_fomo_owned_path(path)
            ):
                changes.append(FileChange(path=path, operation="delete"))
        await self.sandbox.apply_changes(ref, changes)

        restored = await self.capture_candidate_checkpoint(ref)
        if restored.manifest_hash != checkpoint.manifest_hash:
            raise WorkspaceContractError("restored checkpoint does not match durable truth")

    @staticmethod
    def _candidate_manifest_hash(files: list[dict[str, object]]) -> str:
        manifest = [
            {
                "path": str(item["path"]),
                "sha256": str(item["sha256"]),
                "size": int(item["size"]),
            }
            for item in sorted(files, key=lambda item: str(item["path"]).encode("utf-8"))
        ]
        canonical = json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
            raise WorkspaceContractError("starter base verification failed") from exc

    async def _verify_protected(
        self, ref: SandboxRef, compiled: CompiledAcceptance
    ) -> None:
        """Verify FOMO-owned files inside V: injected acceptance tests and the
        system .gitignore. Starter/base files are intentionally mutable."""
        try:
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
    def _is_fomo_owned_path(path: str) -> bool:
        return any(path == root or path.startswith(root + "/") for root in _FOMO_OWNED_ROOTS)

    @staticmethod
    def _is_secret_path(path: str) -> bool:
        # Keep the settle audit at least as strict as provider-side path
        # validation. Pi can create files through bash, bypassing provider
        # write helpers, so every path segment matching `.env*` must be
        # rejected here as well (for example `.envrc` and `.env.local/key`).
        return any(part.startswith(".env") for part in PurePosixPath(path).parts)

    @staticmethod
    def _is_excluded_path(path: str) -> bool:
        """VCS-internal, generated, and FOMO-system exclusions."""
        if path == SYSTEM_GITIGNORE_PATH or path in _RUNNER_GENERATED_PATHS:
            return True
        return path == ".git" or path.startswith(".git/")

    @staticmethod
    def _shell_single_quote(value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"
