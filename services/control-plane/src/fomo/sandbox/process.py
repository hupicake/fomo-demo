"""Unsafe local process sandbox for trusted development and CI only.

It intentionally requires an explicit opt-in. It must never be selected for a
public demo: it is a convenient local adapter, not a security boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import signal
import socket
import tarfile
from dataclasses import dataclass
from pathlib import Path

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

_IGNORED_MANIFEST_DIRECTORIES = {
    ".git",
    ".next",
    "blob-report",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
}


@dataclass(slots=True)
class _ProcessSandbox:
    workspace: Path
    preview_process: asyncio.subprocess.Process | None = None


class ProcessSandboxProvider:
    """A narrowly-scoped local adapter, guarded by ``ALLOW_UNSAFE_PROCESS_SANDBOX``."""

    def __init__(self, root: Path, *, enabled: bool, default_timeout_seconds: int = 300) -> None:
        if not enabled:
            raise RuntimeError(
                "ProcessSandboxProvider is dev/test only; set ALLOW_UNSAFE_PROCESS_SANDBOX=true explicitly"
            )
        self.root = root.resolve()
        self.default_timeout_seconds = default_timeout_seconds
        self._sandboxes: dict[str, _ProcessSandbox] = {}

    async def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(snapshot=True, pause_resume=False, public_preview=False, network_policy=False)

    async def create(self, project_id: str, source: SourceRef | None = None) -> SandboxRef:
        sandbox_id = uuid7()
        workspace = (self.root / project_id / sandbox_id / "workspace").resolve()
        workspace.mkdir(parents=True, exist_ok=False)
        ref = SandboxRef(id=sandbox_id, project_id=project_id)
        self._sandboxes[ref.id] = _ProcessSandbox(workspace=workspace)
        return ref

    async def connect(self, ref: SandboxRef) -> _ProcessSandbox:
        return self._sandbox(ref)

    async def exec(self, ref: SandboxRef, command: Command, sink: OutputSink) -> ExecResult:
        sandbox = self._sandbox(ref)
        timeout = command.timeout_seconds or self.default_timeout_seconds
        process = await asyncio.create_subprocess_shell(
            command.command,
            cwd=sandbox.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        emitted = 0
        capped = False

        async def pump(stream: asyncio.StreamReader | None, label: str, target: list[bytes]) -> None:
            nonlocal emitted, capped
            if stream is None:
                return
            while chunk := await stream.read(4096):
                if emitted < command.max_output_bytes:
                    remaining = command.max_output_bytes - emitted
                    visible = chunk[:remaining]
                    emitted += len(visible)
                    target.append(visible)
                    if visible:
                        await sink(label, visible.decode("utf-8", errors="replace"))
                    if len(visible) < len(chunk):
                        capped = True
                else:
                    capped = True

        pumps = [asyncio.create_task(pump(process.stdout, "stdout", stdout_parts)), asyncio.create_task(pump(process.stderr, "stderr", stderr_parts))]
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await self._terminate_process_group(process)
        finally:
            await asyncio.gather(*pumps)
        if capped:
            marker = "\n[output truncated]\n"
            stderr_parts.append(marker.encode())
            await sink("stderr", marker)
        return ExecResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=b"".join(stdout_parts).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_parts).decode("utf-8", errors="replace"),
            timed_out=timed_out,
        )

    async def read_file(self, ref: SandboxRef, path: str) -> bytes:
        file_path = self._resolve(ref, path)
        if file_path.is_symlink() or not file_path.is_file():
            raise FileNotFoundError(path)
        return file_path.read_bytes()

    async def apply_changes(self, ref: SandboxRef, changes: list[FileChange]) -> None:
        for change in changes:
            file_path = self._resolve(ref, change.path)
            if change.operation == "delete":
                if file_path.exists():
                    if file_path.is_symlink():
                        raise SandboxPathError("refusing to delete a symlink")
                    file_path.unlink()
                continue
            if change.operation not in {"create", "modify"}:
                raise SandboxPathError(f"unsupported change operation: {change.operation}")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            if file_path.exists() and file_path.is_symlink():
                raise SandboxPathError("refusing to write through a symlink")
            temporary = file_path.with_suffix(f"{file_path.suffix}.fomo-tmp")
            temporary.write_text(change.content, encoding="utf-8")
            temporary.replace(file_path)

    async def expose(self, ref: SandboxRef, port: int) -> PreviewRef:
        self._sandbox(ref)
        return PreviewRef(url=f"http://127.0.0.1:{port}", status="pending")

    async def start_preview(
        self, ref: SandboxRef, command: Command, port: int, sink: OutputSink
    ) -> PreviewRef:
        sandbox = self._sandbox(ref)
        if sandbox.preview_process is not None and sandbox.preview_process.returncode is None:
            # A repair may have changed files; retaining an old dev server would
            # make the health gate validate stale code.
            await self._terminate_process_group(sandbox.preview_process)
        process = await asyncio.create_subprocess_shell(
            command.command,
            cwd=sandbox.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        sandbox.preview_process = process

        async def forward(stream: asyncio.StreamReader | None, label: str) -> None:
            if stream is None:
                return
            while chunk := await stream.read(4096):
                await sink(label, chunk.decode("utf-8", errors="replace"))

        asyncio.create_task(forward(process.stdout, "stdout"))
        asyncio.create_task(forward(process.stderr, "stderr"))
        return PreviewRef(url=f"http://127.0.0.1:{port}", status="ready")

    async def snapshot(self, ref: SandboxRef) -> SnapshotRef:
        sandbox = self._sandbox(ref)
        archive_dir = (sandbox.workspace.parent / "snapshots").resolve()
        archive_dir.mkdir(parents=True, exist_ok=True)
        snapshot_id = uuid7()
        archive = archive_dir / f"{snapshot_id}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(sandbox.workspace, arcname="workspace", recursive=True)
        return SnapshotRef(id=snapshot_id, location=str(archive))

    async def pause(self, ref: SandboxRef) -> None:
        # A regular host process has no safe pause/resume semantic.
        self._sandbox(ref)

    async def kill(self, ref: SandboxRef) -> None:
        sandbox = self._sandbox(ref)
        if sandbox.preview_process is not None and sandbox.preview_process.returncode is None:
            await self._terminate_process_group(sandbox.preview_process)

    async def list_files(self, ref: SandboxRef) -> list[dict[str, object]]:
        sandbox = self._sandbox(ref)
        entries: list[dict[str, object]] = []
        for path in sorted(sandbox.workspace.rglob("*")):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.name.endswith(".log")
                or _IGNORED_MANIFEST_DIRECTORIES.intersection(path.parts)
            ):
                continue
            relative = path.relative_to(sandbox.workspace).as_posix()
            data = path.read_bytes()
            mime = mimetypes.guess_type(relative)[0] or "application/octet-stream"
            entries.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "mime": mime,
                    "content_text": data.decode("utf-8", errors="replace")
                    if len(data) <= 512 * 1024 and b"\x00" not in data
                    else None,
                }
            )
        return entries

    def available_port(self, start: int) -> int:
        for port in range(start, start + 100):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    return port
        raise RuntimeError("no available preview port")

    def _sandbox(self, ref: SandboxRef) -> _ProcessSandbox:
        sandbox = self._sandboxes.get(ref.id)
        if sandbox is None:
            raise KeyError(f"unknown process sandbox {ref.id}")
        return sandbox

    def _resolve(self, ref: SandboxRef, path: str) -> Path:
        relative = validate_workspace_path(path)
        workspace = self._sandbox(ref).workspace
        candidate = (workspace / relative).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise SandboxPathError("path escaped the workspace") from exc
        return candidate

    async def _terminate_process_group(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            await process.wait()
