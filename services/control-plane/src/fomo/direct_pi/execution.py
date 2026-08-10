"""Durable, redacted event adapters for trusted commands and Direct Pi output."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from fomo.config import Settings
from fomo.fomo_pi_ds import PiBridgeEnvelope
from fomo.ids import uuid7
from fomo.persistence import Repository, RunLeaseLost
from fomo.sandbox.base import Command, ExecResult, SandboxProvider, SandboxRef
from fomo.text_safety import redact

from .failures import public_bridge_failure

_PUBLIC_MESSAGE_CHARACTERS = 8_192
_PUBLIC_DETAIL_CHARACTERS = 4_096
_PUBLIC_PATH_CHARACTERS = 512
_PUBLIC_NUMBER_MAX = 2**53 - 1
_PUBLIC_PATH_TOOLS = frozenset({"read", "write", "edit", "ls", "find", "grep"})
_PUBLIC_PATH_KEYS = ("path", "file", "filePath", "file_path", "directory", "cwd")
_PUBLIC_TEXT_DELTA_TYPES = frozenset({"text_start", "text_delta", "text_end"})


class DirectPiRunCancelled(RuntimeError):
    """Cancellation fence shared by Pi turns and deterministic commands."""


async def assert_run_active(
    repository: Repository,
    run_id: str,
    lease_token: str,
) -> None:
    """Fail before another side effect, with cancellation taking precedence."""

    cancelled, active = await asyncio.gather(
        repository.is_cancel_requested(run_id),
        repository.is_active_lease(run_id, lease_token),
    )
    if cancelled:
        raise DirectPiRunCancelled("Direct Pi run was cancelled")
    if not active:
        raise RunLeaseLost("run lease is no longer active")


class CommandExecutor:
    """Execute one trusted command and persist a compact terminal transcript."""

    def __init__(
        self,
        repository: Repository,
        sandbox: SandboxProvider,
        settings: Settings,
        *,
        run_id: str,
        lease_token: str,
    ) -> None:
        self.repository = repository
        self.sandbox = sandbox
        self.settings = settings
        self.run_id = run_id
        self.lease_token = lease_token

    async def run(
        self,
        ref: SandboxRef,
        command_text: str,
        *,
        label: str,
        stage: str,
        timeout_seconds: int | None = None,
    ) -> ExecResult:
        await assert_run_active(self.repository, self.run_id, self.lease_token)
        operation_id = uuid7()
        await self.repository.append_event(
            self.run_id,
            "command.started",
            payload={
                "operationId": operation_id,
                "command": command_text,
                "label": label,
                "stage": stage,
            },
            lease_token=self.lease_token,
        )
        # Fence again immediately before the external side effect. A cancel
        # request may race with the durable command.started projection.
        await assert_run_active(self.repository, self.run_id, self.lease_token)
        chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}

        async def sink(stream: str, text: str) -> None:
            if stream in chunks and text:
                chunks[stream].append(redact(text))

        result = await self.sandbox.exec(
            ref,
            Command(
                command=command_text,
                timeout_seconds=timeout_seconds or self.settings.command_timeout_seconds,
                max_output_bytes=self.settings.command_output_limit_bytes,
                operation_id=operation_id,
            ),
            sink,
        )
        # Never continue deterministic verification/checkpoint work after a
        # command that completed under a cancelled or superseded lease.
        await assert_run_active(self.repository, self.run_id, self.lease_token)
        for stream in ("stdout", "stderr"):
            text = "".join(chunks[stream])
            if text:
                await self.repository.append_event(
                    self.run_id,
                    "command.output",
                    payload={
                        "operationId": operation_id,
                        "stream": stream,
                        "text": text,
                        "cumulative": False,
                    },
                    lease_token=self.lease_token,
                )
        await self.repository.append_event(
            self.run_id,
            "command.completed",
            payload={
                "operationId": operation_id,
                "exitCode": result.exit_code,
                "timedOut": result.timed_out,
                "stage": stage,
            },
            lease_token=self.lease_token,
        )
        await assert_run_active(self.repository, self.run_id, self.lease_token)
        return result


class PiEventWriter:
    """Project public Pi activity into the durable FOMO event vocabulary."""

    def __init__(self, repository: Repository, *, run_id: str, lease_token: str) -> None:
        self.repository = repository
        self.run_id = run_id
        self.lease_token = lease_token

    async def __call__(self, envelope: PiBridgeEnvelope, *, stage: str | None = None) -> None:
        payload = dict(envelope.payload)
        if envelope.type == "started":
            kind = "pi.started"
        elif envelope.type == "completed":
            kind = "pi.completed"
        elif envelope.type == "failed":
            kind = "pi.failed"
        else:
            pi_kind = str(payload.pop("kind", "activity"))
            kind = {
                "message_delta": "pi.message.delta",
                "turn_end": "pi.message.completed",
                "tool_start": "pi.tool.started",
                "tool_output": "pi.tool.output",
                "tool_end": "pi.tool.completed",
                "bash_output": "pi.command.output",
            }.get(pi_kind, "pi.activity")
            payload["activity"] = pi_kind
        payload["bridgeSeq"] = envelope.seq
        if stage:
            payload["stage"] = stage
        await self.repository.append_event(
            self.run_id,
            kind,
            payload=_public_payload(kind, payload),
            lease_token=self.lease_token,
        )


def _bounded_public_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = redact(value).translate(
        {character: None for character in range(32) if character not in {9, 10, 13}}
    )
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: max(0, limit - 1)]}…"


def _public_string(
    output: dict[str, Any],
    source: dict[str, Any],
    key: str,
    *,
    limit: int = _PUBLIC_DETAIL_CHARACTERS,
) -> None:
    value = _bounded_public_text(source.get(key), limit)
    if value is not None:
        output[key] = value


def _public_boolean(output: dict[str, Any], source: dict[str, Any], key: str) -> None:
    value = source.get(key)
    if isinstance(value, bool):
        output[key] = value


def _public_number(output: dict[str, Any], source: dict[str, Any], key: str) -> None:
    value = source.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= _PUBLIC_NUMBER_MAX:
        output[key] = value
    elif isinstance(value, float) and math.isfinite(value) and abs(value) <= _PUBLIC_NUMBER_MAX:
        output[key] = value


def _public_nonnegative_number(
    output: dict[str, Any],
    source: dict[str, Any],
    source_key: str,
    output_key: str | None = None,
) -> bool:
    value = source.get(source_key)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
        or value > _PUBLIC_NUMBER_MAX
    ):
        return False
    output[output_key or source_key] = value
    return True


def _public_context_usage(
    output: dict[str, Any], source: dict[str, Any], stats_key: str
) -> bool:
    stats = source.get(stats_key)
    usage = stats.get("contextUsage") if isinstance(stats, dict) else None
    if not isinstance(usage, dict):
        return False
    _public_nonnegative_number(output, usage, "tokens", "contextTokens")
    return _public_nonnegative_number(output, usage, "contextWindow")


def _public_event_context(source: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    _public_string(output, source, "activity", limit=80)
    _public_string(output, source, "stage", limit=80)
    _public_number(output, source, "bridgeSeq")
    return output


def _safe_tool_path(tool_name: str | None, args: Any) -> str | None:
    if tool_name not in _PUBLIC_PATH_TOOLS or not isinstance(args, dict):
        return None
    for key in _PUBLIC_PATH_KEYS:
        value = args.get(key)
        if not isinstance(value, str):
            continue
        path = _bounded_public_text(
            value.replace("\r", " ").replace("\n", " ").replace("\t", " "),
            _PUBLIC_PATH_CHARACTERS,
        )
        if path and path.strip():
            return path.strip()
    return None


def _public_tool_results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    results: list[dict[str, Any]] = []
    for item in value[:128]:
        if not isinstance(item, dict):
            continue
        result: dict[str, Any] = {}
        _public_string(result, item, "toolCallId", limit=256)
        _public_string(result, item, "toolName", limit=80)
        _public_boolean(result, item, "isError")
        if result:
            results.append(result)
    return results


_ACTIVITY_FIELDS: dict[str, tuple[str, ...]] = {
    "agent_end": ("willRetry",),
    "message_start": ("role",),
    "message_end": ("role", "stopReason"),
    "compaction_start": ("reason",),
    "compaction_end": ("reason", "aborted", "willRetry"),
    "auto_retry_start": ("attempt", "maxAttempts"),
    "auto_retry_end": ("success", "attempt"),
    "summarization_retry_scheduled": ("attempt", "maxAttempts"),
    "summarization_retry_attempt_start": ("source",),
    "extension_error": ("extensionPath", "event"),
}


def _public_payload(kind: str, source: dict[str, Any]) -> dict[str, Any]:
    """Project one bridge event onto its minimal durable, browser-safe payload."""

    output = _public_event_context(source)

    if kind == "pi.started":
        # The Pi session id is an internal continuation capability. Keep it in
        # the bridge/runtime result and durable RunRecord, never in browser/SSE
        # telemetry where it has no user-facing value.
        # Provider/model aliases remain server-owned. The immutable public
        # runtime profile is projected through RunResponse.runtime instead.
        _public_string(output, source, "thinkingLevel", limit=32)
        if not _public_context_usage(output, source, "initialStats"):
            _public_nonnegative_number(output, source, "contextWindow")
        _public_boolean(output, source, "resumed")
        return output

    if kind == "pi.completed":
        _public_context_usage(output, source, "stats")
        return output

    if kind == "pi.failed":
        failure = public_bridge_failure(source.get("code"))
        output.update(failure.event_payload())
        return output

    if kind == "pi.message.delta":
        delta_type = source.get("deltaType")
        if delta_type not in _PUBLIC_TEXT_DELTA_TYPES:
            output["suppressed"] = True
            return output
        _public_string(output, source, "deltaType", limit=80)
        _public_number(output, source, "contentIndex")
        if delta_type == "text_delta":
            _public_string(output, source, "delta", limit=_PUBLIC_MESSAGE_CHARACTERS)
        return output

    if kind == "pi.message.completed":
        _public_string(output, source, "role", limit=80)
        _public_string(output, source, "stopReason", limit=256)
        _public_string(output, source, "text", limit=_PUBLIC_MESSAGE_CHARACTERS)
        tool_results = _public_tool_results(source.get("toolResults"))
        if tool_results:
            output["toolResults"] = tool_results
        return output

    if kind == "pi.tool.started":
        _public_string(output, source, "toolCallId", limit=256)
        _public_string(output, source, "toolName", limit=80)
        _public_number(output, source, "elapsedMs")
        path = _safe_tool_path(output.get("toolName"), source.get("args"))
        if path is not None:
            output["path"] = path
        return output

    if kind == "pi.tool.output":
        _public_string(output, source, "toolCallId", limit=256)
        _public_string(output, source, "toolName", limit=80)
        _public_boolean(output, source, "cumulative")
        _public_number(output, source, "elapsedMs")
        output["suppressed"] = True
        return output

    if kind == "pi.tool.completed":
        _public_string(output, source, "toolCallId", limit=256)
        _public_string(output, source, "toolName", limit=80)
        _public_boolean(output, source, "isError")
        _public_number(output, source, "elapsedMs")
        return output

    if kind == "pi.command.output":
        output["suppressed"] = True
        return output

    if kind == "pi.activity":
        activity = output.get("activity")
        for key in _ACTIVITY_FIELDS.get(activity, ()):
            if key in {"willRetry", "aborted", "success"}:
                _public_boolean(output, source, key)
            elif key in {"attempt", "maxAttempts"}:
                _public_number(output, source, key)
            else:
                _public_string(output, source, key)
        return output

    return output
