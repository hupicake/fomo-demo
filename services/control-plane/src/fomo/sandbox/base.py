"""Vendor-neutral sandbox contract and safety helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol


class SandboxPathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    snapshot: bool
    pause_resume: bool
    public_preview: bool
    network_policy: bool


@dataclass(frozen=True, slots=True)
class SandboxRef:
    id: str
    project_id: str


@dataclass(frozen=True, slots=True)
class SourceRef:
    version_id: str | None = None
    bundle_key: str | None = None


@dataclass(frozen=True, slots=True)
class Command:
    command: str
    timeout_seconds: int = 300
    max_output_bytes: int = 64 * 1024
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    content: str = ""
    operation: str = "create"


@dataclass(frozen=True, slots=True)
class PreviewRef:
    url: str | None
    status: str
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    id: str
    location: str | None = None


OutputSink = Callable[[str, str], Awaitable[None]]


class SandboxSession(Protocol):
    """Opaque provider-specific connection marker."""


class SandboxProvider(Protocol):
    async def capabilities(self) -> SandboxCapabilities: ...

    async def create(self, project_id: str, source: SourceRef | None = None) -> SandboxRef: ...

    async def connect(self, ref: SandboxRef) -> SandboxSession: ...

    async def exec(self, ref: SandboxRef, command: Command, sink: OutputSink) -> ExecResult: ...

    async def read_file(self, ref: SandboxRef, path: str) -> bytes: ...

    async def apply_changes(self, ref: SandboxRef, changes: list[FileChange]) -> None: ...

    async def expose(self, ref: SandboxRef, port: int) -> PreviewRef: ...

    async def snapshot(self, ref: SandboxRef) -> SnapshotRef: ...

    async def pause(self, ref: SandboxRef) -> None: ...

    async def kill(self, ref: SandboxRef) -> None: ...


class PreviewSandboxProvider(SandboxProvider, Protocol):
    """Optional extension; no core domain code depends on provider-specific types."""

    async def start_preview(
        self, ref: SandboxRef, command: Command, port: int, sink: OutputSink
    ) -> PreviewRef: ...


SENSITIVE_NAMES = {".env", ".env.local", ".env.production", ".git/hooks", ".gitconfig"}


def validate_workspace_path(path: str) -> PurePosixPath:
    """Reject traversal, absolute paths and configuration targets before any provider call."""
    candidate = PurePosixPath(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        raise SandboxPathError("path must stay inside the sandbox workspace")
    normalized = str(candidate)
    if normalized in {".", ""} or normalized.startswith(".git/") or normalized in SENSITIVE_NAMES:
        raise SandboxPathError("path is not writable by an agent")
    if any(part.startswith(".env") for part in candidate.parts):
        raise SandboxPathError("environment files are not writable by an agent")
    return candidate
