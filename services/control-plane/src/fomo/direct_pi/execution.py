"""Durable, redacted event adapters for trusted commands and Direct Pi output."""

from __future__ import annotations

import re
from typing import Any

from fomo.config import Settings
from fomo.fomo_pi_ds import PiBridgeEnvelope
from fomo.ids import uuid7
from fomo.persistence import Repository
from fomo.sandbox.base import Command, ExecResult, SandboxProvider, SandboxRef


def redact(value: str) -> str:
    value = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s]+", r"\1[REDACTED]", value)
    return re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}\b", "[REDACTED]", value)


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
        return result


class PiEventWriter:
    """Project public Pi activity into the durable FOMO event vocabulary."""

    def __init__(self, repository: Repository, *, run_id: str, lease_token: str) -> None:
        self.repository = repository
        self.run_id = run_id
        self.lease_token = lease_token

    async def __call__(self, envelope: PiBridgeEnvelope) -> None:
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
        await self.repository.append_event(
            self.run_id,
            kind,
            payload=_public_payload(payload),
            lease_token=self.lease_token,
        )


def _public_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact public bridge text without inventing reasoning fields."""
    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return redact(item)
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, dict):
            return {str(key): clean(child) for key, child in item.items()}
        return item

    return clean(value)
