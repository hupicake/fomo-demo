"""Foreground OpenSandbox transport for the root-owned Direct Pi bridge."""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts

from fomo.sandbox.base import SandboxRef
from fomo.sandbox.opensandbox import OpenSandboxProvider

from .gateway import RunVirtualKey
from .invocation import PiInvocation
from .rpc import PiBridgeEnvelope, PiBridgeResult, PiBridgeStreamReducer

PiEventSink = Callable[[PiBridgeEnvelope], Awaitable[None]]
PiDiagnosticSink = Callable[[str], Awaitable[None]]


class PiTransportError(RuntimeError):
    """The sandbox command transport failed without exposing bridge data."""


class PiTransportCancelled(PiTransportError):
    """The caller requested cancellation and the foreground bridge was interrupted."""


@dataclass(frozen=True, slots=True)
class PiTransportResult:
    bridge: PiBridgeResult
    execution_id: str
    exit_code: int
    stderr: str


# Extra headroom beyond bridge grace so the bridge can clean up and emit its
# terminal envelope before the sandbox command deadline fires.
_OUTER_TIMEOUT_MARGIN_SECONDS = 5
_RUNTIME_PREFLIGHT_BIN = "/opt/fomo/bin/fomo-runtime-preflight.mjs"
_RUNTIME_PREFLIGHT_PROVIDER_BASE_URL = "FOMO_PREFLIGHT_PROVIDER_BASE_URL"
_RUNTIME_PREFLIGHT_VIRTUAL_KEY = "FOMO_PREFLIGHT_VIRTUAL_KEY"
_RUNTIME_PREFLIGHT_ALIASES_JSON = "FOMO_PREFLIGHT_ALIASES_JSON"


class OpenSandboxPiTransport:
    """Run one Pi turn in generation sandbox G and reduce its strict JSONL stream."""

    def __init__(
        self,
        provider: OpenSandboxProvider,
        *,
        default_timeout_seconds: int,
        stderr_limit_bytes: int = 64 * 1024,
    ) -> None:
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be greater than 0")
        if stderr_limit_bytes <= 0:
            raise ValueError("stderr_limit_bytes must be greater than 0")
        self._provider = provider
        self._default_timeout_seconds = default_timeout_seconds
        self._stderr_limit_bytes = stderr_limit_bytes

    async def preflight_gateway(
        self,
        ref: SandboxRef,
        virtual_key: RunVirtualKey,
        *,
        provider_base_url: str,
        timeout_seconds: int,
    ) -> None:
        """Run the fixed, silent LiteLLM canary from inside OpenSandbox.

        The virtual key crosses the same execd environment boundary as a real
        Pi turn. It is never placed in argv, a workspace file, output, or an
        exception message.
        """

        if timeout_seconds <= 0:
            raise ValueError("runtime preflight timeout must be greater than 0")
        sandbox = await self._provider.connect(ref)
        execution_id: str | None = None

        async def discard_output(_message: Any) -> None:
            # The root-owned probe intentionally emits nothing. Never inspect
            # unexpected output because it could contain a provider response.
            return None

        async def on_init(message: Any) -> None:
            nonlocal execution_id
            value = getattr(message, "id", None)
            if isinstance(value, str) and value:
                execution_id = value

        try:
            execution = await sandbox.commands.run(
                _RUNTIME_PREFLIGHT_BIN,
                opts=RunCommandOpts(
                    background=False,
                    working_directory="/workspace",
                    timeout=timedelta(seconds=timeout_seconds),
                    envs={
                        _RUNTIME_PREFLIGHT_PROVIDER_BASE_URL: provider_base_url,
                        _RUNTIME_PREFLIGHT_VIRTUAL_KEY: virtual_key.secret,
                        _RUNTIME_PREFLIGHT_ALIASES_JSON: json.dumps(
                            virtual_key.model_aliases,
                            separators=(",", ":"),
                        ),
                    },
                ),
                handlers=ExecutionHandlers(
                    on_stdout=discard_output,
                    on_stderr=discard_output,
                    on_init=on_init,
                    skip_accumulation=True,
                ),
            )
        except asyncio.CancelledError:
            if execution_id is not None:
                with suppress(Exception):
                    await asyncio.shield(sandbox.commands.interrupt(execution_id))
            raise
        except Exception:
            if execution_id is not None:
                with suppress(Exception):
                    await sandbox.commands.interrupt(execution_id)
            raise PiTransportError("OpenSandbox runtime preflight command failed") from None

        exit_code = getattr(execution, "exit_code", None)
        error = getattr(execution, "error", None)
        # An absent exit code means execd never proved command completion (for
        # example, its SSE stream ended early). Only an explicit zero is ready.
        if exit_code != 0 or error is not None:
            raise PiTransportError("OpenSandbox runtime preflight command failed") from None

    async def run(
        self,
        ref: SandboxRef,
        invocation: PiInvocation,
        *,
        on_event: PiEventSink | None = None,
        on_diagnostic: PiDiagnosticSink | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> PiTransportResult:
        sandbox = await self._provider.connect(ref)
        request = invocation.request
        reducer = PiBridgeStreamReducer(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            thinking_level=request.thinking,
            model_ref=request.model,
        )
        execution_id: str | None = None
        init_ready = asyncio.Event()
        run_finished = asyncio.Event()
        callback_error: Exception | None = None
        interrupt_error: Exception | None = None
        saw_stdout = False
        stderr_chunks: list[str] = []
        stderr_bytes = 0
        stderr_truncated = False

        async def emit_stderr(value: Any) -> None:
            nonlocal stderr_bytes, stderr_truncated
            text = getattr(value, "text", value)
            if not isinstance(text, str) or not text:
                return
            safe_text = invocation.redact(text)
            encoded = safe_text.encode("utf-8", errors="replace")
            remaining = max(0, self._stderr_limit_bytes - stderr_bytes)
            visible = encoded[:remaining]
            stderr_bytes += len(visible)
            if len(visible) < len(encoded):
                stderr_truncated = True
            if not visible:
                return
            rendered = visible.decode("utf-8", errors="replace")
            stderr_chunks.append(rendered.rstrip("\n"))
            if on_diagnostic is not None:
                await on_diagnostic(rendered)

        async def on_stdout(message: Any) -> None:
            nonlocal callback_error, saw_stdout
            try:
                text = getattr(message, "text", message)
                if not isinstance(text, str) or not text:
                    return
                saw_stdout = True
                # OpenSandbox execd dispatches stdout as line-safe OutputMessage
                # values. Restore the LF removed by its SSE framing before the
                # strict byte-level JSONL reducer sees the record.
                chunk = text.encode("utf-8", errors="strict")
                if not chunk.endswith(b"\n"):
                    chunk += b"\n"
                for envelope in reducer.feed(chunk):
                    if on_event is not None:
                        await on_event(envelope)
            except Exception as exc:
                callback_error = exc
                raise

        async def on_stderr(message: Any) -> None:
            nonlocal callback_error
            try:
                await emit_stderr(message)
            except Exception as exc:
                callback_error = exc
                raise

        async def on_init(message: Any) -> None:
            nonlocal execution_id
            value = getattr(message, "id", None)
            if isinstance(value, str) and value:
                execution_id = value
            init_ready.set()

        async def interrupt_when_cancelled() -> None:
            nonlocal interrupt_error
            assert cancel_event is not None
            await cancel_event.wait()
            await init_ready.wait()
            if run_finished.is_set() or execution_id is None:
                return
            try:
                await sandbox.commands.interrupt(execution_id)
            except Exception as exc:
                interrupt_error = exc

        cancel_task = (
            asyncio.create_task(interrupt_when_cancelled()) if cancel_event is not None else None
        )
        # The outer OpenSandbox command timeout must exceed the bridge's own
        # timeout by the bridge grace period plus a small finalization margin,
        # so the bridge (not the sandbox) performs cleanup first and reports a
        # terminal envelope. This is best-effort: the orchestrator still
        # destroys G and blocks the virtual key on any cancellation/exception.
        timeout_seconds = self._default_timeout_seconds
        if request.timeout_seconds is not None:
            timeout_seconds = (
                request.timeout_seconds + request.grace_seconds + _OUTER_TIMEOUT_MARGIN_SECONDS
            )
        command = shlex.join(invocation.command_line())
        try:
            execution = await sandbox.commands.run(
                command,
                opts=RunCommandOpts(
                    background=False,
                    working_directory=request.workspace,
                    timeout=timedelta(seconds=timeout_seconds),
                    envs=invocation.fomo_environment(),
                ),
                handlers=ExecutionHandlers(
                    on_stdout=on_stdout,
                    on_stderr=on_stderr,
                    on_init=on_init,
                    skip_accumulation=True,
                ),
            )
        except asyncio.CancelledError:
            if execution_id is not None:
                with suppress(Exception):
                    await asyncio.shield(sandbox.commands.interrupt(execution_id))
            raise
        except Exception as exc:
            if execution_id is not None:
                with suppress(Exception):
                    await sandbox.commands.interrupt(execution_id)
            if callback_error is not None:
                raise callback_error from exc
            raise PiTransportError("OpenSandbox failed while running the Pi bridge") from exc
        finally:
            run_finished.set()
            init_ready.set()
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cancel_task

        if interrupt_error is not None:
            raise PiTransportError("OpenSandbox could not interrupt the Pi bridge") from interrupt_error
        if cancel_event is not None and cancel_event.is_set():
            raise PiTransportCancelled("Direct Pi invocation was cancelled")

        if not saw_stdout:
            for message in getattr(getattr(execution, "logs", None), "stdout", []):
                await on_stdout(message)
        error = getattr(execution, "error", None)
        if error is not None:
            await emit_stderr(getattr(error, "value", None) or str(error))
        if stderr_truncated:
            stderr_chunks.append("[stderr truncated]")

        bridge_result = reducer.finish()
        exit_code = getattr(execution, "exit_code", None)
        if exit_code is None:
            exit_code = 1 if error is not None else 0
        if int(exit_code) != 0 or error is not None:
            raise PiTransportError("Pi bridge exited unsuccessfully after a terminal envelope")
        if execution_id is None:
            execution_id = str(getattr(execution, "id", "") or "")
        if not execution_id:
            raise PiTransportError("OpenSandbox did not identify the Pi bridge execution")
        return PiTransportResult(
            bridge=bridge_result,
            execution_id=execution_id,
            exit_code=int(exit_code),
            stderr="\n".join(chunk for chunk in stderr_chunks if chunk),
        )
