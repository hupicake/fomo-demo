"""OpenSandbox Server v0.2.2 adapter, backed by Python SDK v0.1.15.

This provider is the production sandbox path.  It deliberately has no host
process fallback: generated code is written and run only inside an
OpenSandbox-managed container.  ``44772`` is the SDK's internal execd port;
the only browser-facing endpoint this adapter returns is the generated app on
port ``8080``.
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.filesystem import SearchEntry, WriteEntry
from opensandbox.models.sandboxes import PlatformSpec, SandboxState

from fomo.config import (
    DEFAULT_OPENSANDBOX_IMAGE,
    DEFAULT_OPENSANDBOX_LIFETIME_SECONDS,
    DEFAULT_OPENSANDBOX_READY_TIMEOUT_SECONDS,
)

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

_APPLICATION_PORT = 8080
_WORKSPACE = "/workspace"
_STARTER_ROOT = "/opt/fomo/starters"
_RUNTIME_CACHE_ROOT = "/opt/fomo/runtime-cache"
_SUPPORTED_STARTER_ID = "fomo-next-radix-v2"
_SUPPORTED_STARTER_CAPABILITIES = {
    "crud": "crud",
    "local-persistence": "local-persistence",
}
# OpenSandbox serializes permission modes as octal-looking JSON integers
# (for example, 644), not Python ``0o`` integer literals (which become 420).
_OPENSANDBOX_DIRECTORY_WIRE_MODE = 755
_OPENSANDBOX_FILE_WIRE_MODE = 644
# The local image is deliberately curated by infrastructure: it includes Node,
# pnpm and Git, unlike a generic Node slim image. It remains env-overridable.
_DEFAULT_IMAGE = DEFAULT_OPENSANDBOX_IMAGE
_MAX_PERSISTED_TEXT_BYTES = 512 * 1024
_SANDBOX_PROXY_ENV_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"})
# Keep enough headroom for Docker Desktop and OpenSandbox execd overhead while
# copying the baked starter and capability layers.
_STARTER_COPY_TIMEOUT_SECONDS = 120
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


class _OutputCollector:
    """Forward streamed execd output while keeping a bounded result copy."""

    def __init__(self, sink: OutputSink, limit_bytes: int) -> None:
        self._sink = sink
        self._remaining = max(0, limit_bytes)
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._saw_stream_event = False
        self._truncated = False

    @property
    def saw_stream_event(self) -> bool:
        return self._saw_stream_event

    @property
    def stdout(self) -> str:
        return self._joined_output(self._stdout)

    @property
    def stderr(self) -> str:
        return self._joined_output(self._stderr)

    @staticmethod
    def _joined_output(chunks: list[str]) -> str:
        """Match the pinned SDK's line-safe aggregation of output messages."""
        return "\n".join(chunk.rstrip("\n") for chunk in chunks)

    async def emit(self, stream: str, value: Any) -> None:
        text = getattr(value, "text", value)
        if not isinstance(text, str) or not text:
            return
        self._saw_stream_event = True
        encoded = text.encode("utf-8", errors="replace")
        visible = encoded[: self._remaining]
        self._remaining -= len(visible)
        if len(visible) < len(encoded):
            self._truncated = True
        if not visible:
            return
        safe_text = visible.decode("utf-8", errors="replace")
        if stream == "stdout":
            self._stdout.append(safe_text)
        else:
            self._stderr.append(safe_text)
        await self._sink(stream, safe_text)

    async def finish(self) -> None:
        if not self._truncated:
            return
        marker = "\n[output truncated]\n"
        self._stderr.append(marker)
        await self._sink("stderr", marker)


def _validated_proxy_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    """Permit only explicitly configured proxy variables inside generated-code sandboxes."""
    if environment is None:
        return {}
    unexpected = set(environment).difference(_SANDBOX_PROXY_ENV_NAMES)
    if unexpected:
        raise ValueError("sandbox environment may only contain HTTP_PROXY, HTTPS_PROXY, and NO_PROXY")
    validated: dict[str, str] = {}
    for name, value in environment.items():
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if name == "NO_PROXY":
            if any(character in value for character in "\r\n\x00"):
                raise ValueError("NO_PROXY cannot contain control characters")
            validated[name] = value
            continue
        parsed = urlparse(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"{name} must contain a valid proxy port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or port is not None and not 1 <= port <= 65_535
        ):
            raise ValueError(f"{name} must be an http(s) proxy URL without userinfo")
        validated[name] = value
    return validated


class OpenSandboxProvider:
    """Real OpenSandbox implementation with durable server sandbox IDs."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        image: str | None = None,
        *,
        sandbox_class: Any | None = None,
        lifetime_seconds: int = DEFAULT_OPENSANDBOX_LIFETIME_SECONDS,
        ready_timeout_seconds: int = DEFAULT_OPENSANDBOX_READY_TIMEOUT_SECONDS,
        proxy_environment: Mapping[str, str] | None = None,
    ) -> None:
        if lifetime_seconds <= 0:
            raise ValueError("OpenSandbox lifetime must be positive")
        if ready_timeout_seconds <= 0:
            raise ValueError("OpenSandbox ready timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._image = image or _DEFAULT_IMAGE
        self._preview_scheme = urlparse(self.base_url).scheme or "http"
        self._sandbox_class = sandbox_class or Sandbox
        self._lifetime_seconds = lifetime_seconds
        self._ready_timeout_seconds = ready_timeout_seconds
        self._proxy_environment = _validated_proxy_environment(proxy_environment)
        self._sandboxes: dict[str, Any] = {}
        self._preview_execution_ids: dict[str, str] = {}

    async def capabilities(self) -> SandboxCapabilities:
        # OpenSandbox v0.2.2 supports pause/kill. Snapshot persistence is left
        # disabled intentionally until the version rollback workflow consumes
        # server snapshots instead of its authoritative Git/file manifest.
        # Egress policy stays disabled: the local config.toml has no
        # authenticated dns+nft sidecar, so no fail-closed network policy can
        # be claimed (see DESIGN: public untrusted deployment is blocked).
        return SandboxCapabilities(
            snapshot=False,
            pause_resume=True,
            public_preview=True,
            network_policy=False,
        )

    async def create(self, project_id: str, source: SourceRef | None = None) -> SandboxRef:
        metadata = {"fomo.project_id": project_id}
        if source and source.version_id:
            metadata["fomo.source_version_id"] = source.version_id
        if source and source.bundle_key:
            metadata["fomo.source_bundle_key"] = source.bundle_key

        create_kwargs: dict[str, Any] = {
            "timeout": timedelta(seconds=self._lifetime_seconds),
            "ready_timeout": timedelta(seconds=self._ready_timeout_seconds),
            "metadata": metadata,
            "platform": PlatformSpec(os="linux", arch="arm64"),
            "connection_config": self._connection_config(),
        }
        if self._proxy_environment:
            # Do not inherit generic process proxy settings or any credentials;
            # only the settings-curated proxy variables cross this boundary.
            create_kwargs["env"] = dict(self._proxy_environment)
        sandbox = await self._sandbox_class.create(self._image, **create_kwargs)
        try:
            await sandbox.files.create_directories(
                [WriteEntry(path=_WORKSPACE, mode=_OPENSANDBOX_DIRECTORY_WIRE_MODE)]
            )
        except BaseException:
            await self._destroy_handle(sandbox)
            raise

        sandbox_id = str(sandbox.id)
        ref = SandboxRef(id=sandbox_id, project_id=project_id)
        self._sandboxes[sandbox_id] = sandbox
        return ref

    async def connect(self, ref: SandboxRef) -> Any:
        sandbox = self._sandboxes.get(ref.id)
        if sandbox is None:
            sandbox = await self._sandbox_class.connect(
                ref.id,
                connection_config=self._connection_config(),
            )
            self._sandboxes[ref.id] = sandbox
        return sandbox

    async def exec(self, ref: SandboxRef, command: Command, sink: OutputSink) -> ExecResult:
        sandbox = await self.connect(ref)
        collector = _OutputCollector(sink, command.max_output_bytes)

        async def on_stdout(message: Any) -> None:
            await collector.emit("stdout", message)

        async def on_stderr(message: Any) -> None:
            await collector.emit("stderr", message)

        execution = await sandbox.commands.run(
            command.command,
            opts=RunCommandOpts(
                working_directory=_WORKSPACE,
                timeout=timedelta(seconds=command.timeout_seconds),
            ),
            handlers=ExecutionHandlers(
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                skip_accumulation=True,
            ),
        )
        if not collector.saw_stream_event:
            for message in getattr(getattr(execution, "logs", None), "stdout", []):
                await collector.emit("stdout", message)
            for message in getattr(getattr(execution, "logs", None), "stderr", []):
                await collector.emit("stderr", message)
        error = getattr(execution, "error", None)
        if error is not None:
            error_text = getattr(error, "value", None) or str(error)
            await collector.emit("stderr", error_text)
        await collector.finish()

        exit_code = getattr(execution, "exit_code", None)
        if exit_code is None:
            exit_code = 1 if error is not None else 0
        return ExecResult(
            exit_code=int(exit_code),
            stdout=collector.stdout,
            stderr=collector.stderr,
            # OpenSandbox reports a command deadline as exit_code=-1 even
            # when the SDK omits an error object. Preserve that provider
            # convention instead of misclassifying the result as a normal
            # source-code failure.
            timed_out=self._execution_timed_out(error, exit_code=int(exit_code)),
        )

    async def read_file(self, ref: SandboxRef, path: str) -> bytes:
        workspace_path = self._workspace_path(path)
        try:
            return await (await self.connect(ref)).files.read_bytes(workspace_path)
        except SandboxApiException as exc:
            # The SDK exposes HTTP 404 as SandboxApiException while the FOMO
            # provider contract uses FileNotFoundError. Normalize only this
            # semantic absence; other provider failures must remain visible.
            if exc.status_code == 404:
                raise FileNotFoundError(path) from None
            raise

    async def apply_changes(self, ref: SandboxRef, changes: list[FileChange]) -> None:
        sandbox = await self.connect(ref)
        write_entries: list[WriteEntry] = []
        delete_paths: list[str] = []
        for change in changes:
            path = self._workspace_path(change.path)
            if change.operation == "delete":
                delete_paths.append(path)
            elif change.operation in {"create", "modify"}:
                write_entries.append(
                    WriteEntry(path=path, data=change.content, mode=_OPENSANDBOX_FILE_WIRE_MODE)
                )
            else:
                raise SandboxPathError(f"unsupported change operation: {change.operation}")
        if write_entries:
            await sandbox.files.write_files(write_entries)
        if delete_paths:
            await sandbox.files.delete_files(delete_paths)

    @staticmethod
    def _starter_copy_command(starter_id: str, capability_ids: tuple[str, ...]) -> str:
        """Build a command entirely from trusted starter/capability enum values."""
        if starter_id != _SUPPORTED_STARTER_ID:
            raise ValueError("unsupported immutable starter")
        requested = tuple(str(capability_id) for capability_id in capability_ids)
        if len(requested) != len(set(requested)):
            raise ValueError("duplicate starter capability")
        unknown = sorted(set(requested) - set(_SUPPORTED_STARTER_CAPABILITIES))
        if unknown:
            raise ValueError("unsupported starter capability")
        base_directory = f"{_STARTER_ROOT}/{_SUPPORTED_STARTER_ID}/base"
        runtime_node_modules = (
            f"{_RUNTIME_CACHE_ROOT}/{_SUPPORTED_STARTER_ID}/node_modules"
        )
        commands = [
            f"cp -R --no-preserve=mode,ownership -- {base_directory}/. {_WORKSPACE}/",
            # The image-level starter uses a symlink only to avoid duplicating
            # its cache layer. A generated workspace must be self-contained:
            # preserve pnpm's internal symlink/hardlink graph and executable
            # modes without leaving node_modules pointed outside the project.
            f"test -L {_WORKSPACE}/node_modules",
            f"rm -- {_WORKSPACE}/node_modules",
            (
                "cp -a --no-preserve=ownership -- "
                f"{runtime_node_modules} {_WORKSPACE}/node_modules"
            ),
            f"chmod -R u+rwX -- {_WORKSPACE}/node_modules",
        ]
        commands.extend(
            "cp -R --no-preserve=mode,ownership -- "
            f"{_STARTER_ROOT}/{_SUPPORTED_STARTER_ID}/capabilities/"
            f"{_SUPPORTED_STARTER_CAPABILITIES[capability_id]}/. {_WORKSPACE}/"
            for capability_id in sorted(requested)
        )
        return " && ".join(commands)

    async def copy_starter(
        self,
        ref: SandboxRef,
        starter_id: str,
        capability_ids: tuple[str, ...] = (),
    ) -> ExecResult:
        """Copy only the resolved baked seed into a writable generated workspace."""
        command = self._starter_copy_command(starter_id, capability_ids)

        async def discard_output(_stream: str, _text: str) -> None:
            return None

        # The starter source is image-owned and read-only. Do not preserve its
        # mode or ownership: the copy in /workspace must be writable by the
        # sandbox's unprivileged Node user.
        return await self.exec(
            ref,
            Command(
                command=command,
                timeout_seconds=_STARTER_COPY_TIMEOUT_SECONDS,
            ),
            discard_output,
        )

    async def expose(self, ref: SandboxRef, port: int) -> PreviewRef:
        self._validate_preview_port(port)
        endpoint = await (await self.connect(ref)).get_endpoint(port)
        headers = getattr(endpoint, "headers", {}) or {}
        if headers:
            raise RuntimeError(
                "OpenSandbox preview endpoint requires request headers; configure browser-reachable direct ingress"
            )
        return PreviewRef(url=self._endpoint_url(endpoint), status="ready")

    async def renew_preview(self, ref: SandboxRef, lifetime_seconds: int) -> str:
        """Extend one verified preview and return its authoritative expiry."""
        if lifetime_seconds <= 0:
            raise ValueError("verified preview lifetime must be positive")
        sandbox = None
        try:
            sandbox = await self.connect(ref)
            info = await sandbox.get_info()
            state = str(getattr(getattr(info, "status", None), "state", ""))
            if state != SandboxState.RUNNING:
                raise RuntimeError("OpenSandbox verified preview is not running")
            response = await sandbox.renew(timedelta(seconds=lifetime_seconds))
        except SandboxApiException as exc:
            if self._is_missing_sandbox(exc):
                await self._evict_cached_sandbox(ref.id, sandbox)
            raise
        expires_at = getattr(response, "expires_at", None)
        if not isinstance(expires_at, datetime):
            raise RuntimeError("OpenSandbox renew response did not include an expiry")
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at.astimezone(UTC).isoformat()

    async def probe_preview(self, ref: SandboxRef) -> bool:
        """Return False only for a confirmed missing or non-running resource.

        ``Sandbox.is_healthy`` deliberately swallows transport errors, so it
        cannot distinguish an expired preview from a transient control-plane
        outage. ``get_info`` preserves that distinction for callers.
        """
        sandbox = None
        try:
            sandbox = await self.connect(ref)
            info = await sandbox.get_info()
        except SandboxApiException as exc:
            if not self._is_missing_sandbox(exc):
                raise
            await self._evict_cached_sandbox(ref.id, sandbox)
            return False
        state = str(getattr(getattr(info, "status", None), "state", ""))
        return state == SandboxState.RUNNING

    async def start_preview(
        self, ref: SandboxRef, command: Command, port: int, sink: OutputSink
    ) -> PreviewRef:
        self._validate_preview_port(port)
        sandbox = await self.connect(ref)
        previous_execution_id = self._preview_execution_ids.pop(ref.id, None)
        if previous_execution_id:
            # Re-verification after a repair must not leave a stale dev server
            # listening on the fixed application port.
            with suppress(Exception):
                await sandbox.commands.interrupt(previous_execution_id)

        collector = _OutputCollector(sink, command.max_output_bytes)

        async def on_stdout(message: Any) -> None:
            await collector.emit("stdout", message)

        async def on_stderr(message: Any) -> None:
            await collector.emit("stderr", message)

        execution = await sandbox.commands.run(
            command.command,
            opts=RunCommandOpts(background=True, working_directory=_WORKSPACE),
            handlers=ExecutionHandlers(
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                skip_accumulation=True,
            ),
        )
        if getattr(execution, "error", None) is not None:
            await collector.emit("stderr", getattr(execution.error, "value", execution.error))
            await collector.finish()
            raise RuntimeError("OpenSandbox preview command could not be started")
        execution_id = getattr(execution, "id", None)
        if execution_id:
            self._preview_execution_ids[ref.id] = str(execution_id)
        await collector.finish()
        return await self.expose(ref, port)

    async def snapshot(self, ref: SandboxRef) -> SnapshotRef:
        self._sandboxes.get(ref.id)  # keep the API error deterministic for unknown refs
        raise NotImplementedError(
            "OpenSandbox snapshots are intentionally disabled; FOMO versions use Git commits and file manifests."
        )

    async def pause(self, ref: SandboxRef) -> None:
        await (await self.connect(ref)).pause()

    async def kill(self, ref: SandboxRef) -> None:
        try:
            sandbox = await self.connect(ref)
        except SandboxApiException as exc:
            if not self._is_missing_sandbox(exc):
                raise
            # A recovery worker may be holding a durable reference after the
            # OpenSandbox server already expired/destroyed the container. The
            # desired end state has been reached, so make delete idempotent.
            self._preview_execution_ids.pop(ref.id, None)
            self._sandboxes.pop(ref.id, None)
            return
        try:
            await self._destroy_handle(sandbox)
        except SandboxApiException as exc:
            if not self._is_missing_sandbox(exc):
                raise
        finally:
            self._preview_execution_ids.pop(ref.id, None)
            self._sandboxes.pop(ref.id, None)

    async def list_files(self, ref: SandboxRef) -> list[dict[str, object]]:
        """Return source-file manifest entries without persisting dependencies/build output."""
        sandbox = await self.connect(ref)
        entries = await sandbox.files.search(SearchEntry(path=_WORKSPACE, pattern="**/*"))
        result: list[dict[str, object]] = []
        prefix = f"{_WORKSPACE}/"
        for entry in entries:
            absolute_path = str(getattr(entry, "path", ""))
            if not absolute_path.startswith(prefix):
                continue
            relative = absolute_path.removeprefix(prefix)
            relative_path = PurePosixPath(relative)
            if (
                not relative
                or relative_path.name.endswith(".log")
                or _IGNORED_MANIFEST_DIRECTORIES.intersection(relative_path.parts)
            ):
                continue
            entry_type = getattr(entry, "entry_type", None)
            if entry_type not in {None, "file"}:
                continue
            data = await sandbox.files.read_bytes(absolute_path)
            result.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "mime": mimetypes.guess_type(relative)[0] or "application/octet-stream",
                    "content_text": data.decode("utf-8", errors="replace")
                    if len(data) <= _MAX_PERSISTED_TEXT_BYTES and b"\x00" not in data
                    else None,
                }
            )
        return sorted(result, key=lambda item: str(item["path"]))

    def _connection_config(self) -> ConnectionConfig:
        # The SDK also supports OPEN_SANDBOX_API_KEY. Passing the configured
        # value explicitly keeps FOMO's OPENSANDBOX_* configuration namespace
        # self-contained without reading any dotenv file.
        return ConnectionConfig(
            api_key=self._api_key,
            domain=self.base_url,
            protocol="http",
            request_timeout=timedelta(seconds=self._ready_timeout_seconds),
        )

    @staticmethod
    def _execution_timed_out(error: Any, *, exit_code: int | None = None) -> bool:
        if exit_code == -1:
            return True
        if error is None:
            return False
        text = " ".join(
            str(value) for value in (getattr(error, "name", ""), getattr(error, "value", ""))
        ).lower()
        return "timeout" in text or "timed out" in text

    @staticmethod
    def _is_missing_sandbox(exc: SandboxApiException) -> bool:
        """Recognize only the server's explicit missing-resource outcomes."""
        if exc.status_code == 404:
            return True
        code = str(getattr(getattr(exc, "error", None), "code", "")).upper()
        return code == "SANDBOX_NOT_FOUND" or code.endswith("::SANDBOX_NOT_FOUND")

    @staticmethod
    async def _destroy_handle(sandbox: Any) -> None:
        destroy = getattr(sandbox, "destroy", None)
        if destroy is not None:
            await destroy()
            return
        await sandbox.kill()
        close = getattr(sandbox, "close", None)
        if close is not None:
            await close()

    async def _evict_cached_sandbox(self, sandbox_id: str, sandbox: Any | None) -> None:
        """Forget a server-confirmed missing sandbox without issuing destroy."""
        self._preview_execution_ids.pop(sandbox_id, None)
        self._sandboxes.pop(sandbox_id, None)
        close = getattr(sandbox, "close", None) if sandbox is not None else None
        if close is not None:
            with suppress(Exception):
                await close()

    @staticmethod
    def _workspace_path(path: str) -> str:
        return f"{_WORKSPACE}/{validate_workspace_path(path)}"

    @staticmethod
    def _validate_preview_port(port: int) -> None:
        if port != _APPLICATION_PORT:
            raise ValueError("OpenSandbox previews must use application port 8080, never execd")

    def _endpoint_url(self, endpoint: Any) -> str:
        raw = getattr(endpoint, "endpoint", None) or getattr(endpoint, "url", None) or str(endpoint)
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"}:
            return raw
        if raw.startswith("/"):
            raise RuntimeError("OpenSandbox returned a relative preview endpoint without a browser base URL")
        return f"{self._preview_scheme}://{raw}"
