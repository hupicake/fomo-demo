"""Deterministic in-memory provider for unit and integration tests."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass, field

from fomo.ids import uuid7

from .base import (
    Command,
    ExecResult,
    FileChange,
    OutputSink,
    PreviewRef,
    SandboxCapabilities,
    SandboxPathError,
    SandboxRef,
    SnapshotRef,
    SourceRef,
    validate_workspace_path,
)


@dataclass(slots=True)
class FakeSandbox:
    files: dict[str, bytes] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)


class FakeSandboxProvider:
    """A contract-test provider, never used for a user-facing preview."""

    def __init__(
        self, command_results: dict[str, ExecResult | list[ExecResult]] | None = None
    ) -> None:
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.command_results = command_results or {}

    async def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(snapshot=True, pause_resume=True, public_preview=True, network_policy=True)

    async def create(self, project_id: str, source: SourceRef | None = None) -> SandboxRef:
        ref = SandboxRef(id=uuid7(), project_id=project_id)
        self.sandboxes[ref.id] = FakeSandbox()
        return ref

    async def connect(self, ref: SandboxRef) -> FakeSandbox:
        return self._sandbox(ref)

    async def exec(self, ref: SandboxRef, command: Command, sink: OutputSink) -> ExecResult:
        sandbox = self._sandbox(ref)
        sandbox.commands.append(command.command)
        configured = self.command_results.get(command.command)
        if isinstance(configured, list):
            result = configured.pop(0) if configured else ExecResult(0, "ok\n", "")
        else:
            result = configured or ExecResult(0, "ok\n", "")
        if result.stdout:
            await sink("stdout", result.stdout)
        if result.stderr:
            await sink("stderr", result.stderr)
        return result

    async def read_file(self, ref: SandboxRef, path: str) -> bytes:
        key = str(validate_workspace_path(path))
        sandbox = self._sandbox(ref)
        if key not in sandbox.files:
            raise FileNotFoundError(key)
        return sandbox.files[key]

    async def apply_changes(self, ref: SandboxRef, changes: list[FileChange]) -> None:
        sandbox = self._sandbox(ref)
        for change in changes:
            path = str(validate_workspace_path(change.path))
            if change.operation == "delete":
                sandbox.files.pop(path, None)
            elif change.operation in {"create", "modify"}:
                sandbox.files[path] = change.content.encode("utf-8")
            else:
                raise SandboxPathError(f"unsupported change operation: {change.operation}")

    async def expose(self, ref: SandboxRef, port: int) -> PreviewRef:
        self._sandbox(ref)
        return PreviewRef(url=f"http://fake-preview.invalid:{port}", status="ready")

    async def start_preview(
        self, ref: SandboxRef, command: Command, port: int, sink: OutputSink
    ) -> PreviewRef:
        self._sandbox(ref).commands.append(command.command)
        await sink("stdout", "fake preview ready\n")
        return await self.expose(ref, port)

    async def renew_preview(self, ref: SandboxRef, lifetime_seconds: int) -> str:
        self._sandbox(ref)
        if lifetime_seconds <= 0:
            raise ValueError("preview lifetime must be positive")
        return "2099-01-01T00:00:00+00:00"

    async def probe_preview(self, ref: SandboxRef) -> bool:
        self._sandbox(ref)
        return True

    async def snapshot(self, ref: SandboxRef) -> SnapshotRef:
        self._sandbox(ref)
        return SnapshotRef(id=uuid7(), location="fake://snapshot")

    async def pause(self, ref: SandboxRef) -> None:
        self._sandbox(ref)

    async def kill(self, ref: SandboxRef) -> None:
        self._sandbox(ref)

    async def list_files(self, ref: SandboxRef) -> list[dict[str, object]]:
        sandbox = self._sandbox(ref)
        return [
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "mime": mimetypes.guess_type(path)[0] or "application/octet-stream",
                "content_text": content.decode("utf-8", errors="replace"),
            }
            for path, content in sorted(sandbox.files.items())
        ]

    def _sandbox(self, ref: SandboxRef) -> FakeSandbox:
        sandbox = self.sandboxes.get(ref.id)
        if sandbox is None:
            raise KeyError(f"unknown fake sandbox {ref.id}")
        return sandbox
