from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fomo.fomo_pi_ds import (
    FOMO_PI_MODEL,
    FOMO_PI_THINKING,
    OpenSandboxPiTransport,
    PiBridgeProtocolError,
    PiInvocation,
    PiRequest,
    PiTransportCancelled,
    PiTransportError,
)
from fomo.sandbox.base import SandboxRef


def _stats(session_id: str) -> dict[str, object]:
    return {
        "sessionId": session_id,
        "userMessages": 1,
        "assistantMessages": 1,
        "toolCalls": 1,
        "toolResults": 1,
        "totalMessages": 4,
        "tokens": {
            "input": 10,
            "output": 5,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": 15,
        },
        "cost": 0.001,
    }


def _line(seq: int, event_type: str, payload: dict[str, object]) -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "requestId": "request-1",
            "correlationId": "run-1",
            "seq": seq,
            "type": event_type,
            "payload": payload,
        },
        separators=(",", ":"),
    )


def _successful_lines() -> list[str]:
    session_id = "session-1"
    return [
        _line(
            1,
            "started",
            {
                "sessionId": session_id,
                "model": FOMO_PI_MODEL,
                "thinkingLevel": FOMO_PI_THINKING,
                "resumed": False,
                "initialStats": _stats(session_id),
            },
        ),
        _line(2, "pi.event", {"kind": "agent_start"}),
        _line(3, "pi.event", {"kind": "agent_settled"}),
        _line(
            4,
            "completed",
            {
                "sessionId": session_id,
                "state": {
                    "sessionId": session_id,
                    "messageCount": 4,
                    "pendingMessageCount": 0,
                    "isStreaming": False,
                    "isCompacting": False,
                },
                "stats": _stats(session_id),
            },
        ),
    ]


class _FakeCommands:
    def __init__(
        self,
        *,
        stdout: list[str] | None = None,
        stderr: list[str] | None = None,
        wait_for_interrupt: bool = False,
        exit_code: int = 0,
    ) -> None:
        self.stdout = stdout or []
        self.stderr = stderr or []
        self.wait_for_interrupt = wait_for_interrupt
        self.exit_code = exit_code
        self.started = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.interrupt_ids: list[str] = []
        self.command: str | None = None
        self.opts: Any = None

    async def run(self, command: str, *, opts: Any, handlers: Any) -> Any:
        self.command = command
        self.opts = opts
        await handlers.on_init(SimpleNamespace(id="execution-1"))
        self.started.set()
        if self.wait_for_interrupt:
            await self.interrupted.wait()
            return self._execution(exit_code=1)
        for value in self.stderr:
            await handlers.on_stderr(SimpleNamespace(text=value))
        for value in self.stdout:
            await handlers.on_stdout(SimpleNamespace(text=value))
        return self._execution(exit_code=self.exit_code)

    async def interrupt(self, execution_id: str) -> None:
        self.interrupt_ids.append(execution_id)
        self.interrupted.set()

    @staticmethod
    def _execution(*, exit_code: int) -> Any:
        return SimpleNamespace(
            id="execution-1",
            exit_code=exit_code,
            error=None,
            logs=SimpleNamespace(stdout=[], stderr=[]),
        )


class _FakeProvider:
    def __init__(self, commands: _FakeCommands) -> None:
        self.sandbox = SimpleNamespace(commands=commands)
        self.refs: list[SandboxRef] = []

    async def connect(self, ref: SandboxRef) -> Any:
        self.refs.append(ref)
        return self.sandbox


def _invocation(tmp_path: Path) -> PiInvocation:
    return PiInvocation(
        PiRequest(
            request_id="request-1",
            correlation_id="run-1",
            session_id="session-1",
            provider_base_url="http://litellm:4000/v1",
            prompt="private prompt",
            virtual_key="sk-run-secret",
            state_dir=str(tmp_path / "state"),
            timeout_seconds=120,
        )
    )


@pytest.mark.asyncio
async def test_transport_streams_protocol_and_keeps_secrets_out_of_diagnostics(
    tmp_path: Path,
) -> None:
    commands = _FakeCommands(
        stdout=_successful_lines(),
        stderr=["warning private prompt sk-run-secret"],
    )
    provider = _FakeProvider(commands)
    events = []
    diagnostics: list[str] = []
    transport = OpenSandboxPiTransport(provider, default_timeout_seconds=300)  # type: ignore[arg-type]

    async def collect_event(event: Any) -> None:
        events.append(event)

    async def collect_diagnostic(value: str) -> None:
        diagnostics.append(value)

    result = await transport.run(
        SandboxRef(id="sandbox-1", project_id="project-1"),
        _invocation(tmp_path),
        on_event=collect_event,
        on_diagnostic=collect_diagnostic,
    )

    assert [event.type for event in events] == [
        "started",
        "pi.event",
        "pi.event",
        "completed",
    ]
    assert result.bridge.completed["stats"]["tokens"]["total"] == 15
    assert result.execution_id == "execution-1"
    assert commands.command == "/opt/fomo/bin/fomo-pi-rpc-bridge.mjs"
    assert commands.opts.background is False
    assert commands.opts.working_directory == "/workspace"
    # Outer sandbox timeout must exceed the bridge's own timeout by the grace
    # period plus a small finalization margin so the bridge cleans up first.
    assert int(commands.opts.timeout.total_seconds()) == 120 + 10 + 5
    assert commands.opts.envs["FOMO_PI_VIRTUAL_KEY"] == "sk-run-secret"
    assert commands.opts.envs["FOMO_PI_CONTEXT_WINDOW"] == "200000"
    assert "FOMO_PI_TOOL_POLICY_B64" not in commands.opts.envs
    assert "private prompt" not in result.stderr
    assert "sk-run-secret" not in result.stderr
    assert all("private prompt" not in value for value in diagnostics)
    assert all("sk-run-secret" not in value for value in diagnostics)


@pytest.mark.asyncio
async def test_transport_interrupts_the_foreground_execution_on_cancel(tmp_path: Path) -> None:
    commands = _FakeCommands(wait_for_interrupt=True)
    provider = _FakeProvider(commands)
    cancel_event = asyncio.Event()
    transport = OpenSandboxPiTransport(provider, default_timeout_seconds=300)  # type: ignore[arg-type]

    task = asyncio.create_task(
        transport.run(
            SandboxRef(id="sandbox-1", project_id="project-1"),
            _invocation(tmp_path),
            cancel_event=cancel_event,
        )
    )
    await commands.started.wait()
    cancel_event.set()

    with pytest.raises(PiTransportCancelled):
        await task
    assert commands.interrupt_ids == ["execution-1"]


@pytest.mark.asyncio
async def test_transport_fails_closed_and_interrupts_after_bad_jsonl(tmp_path: Path) -> None:
    commands = _FakeCommands(stdout=["{not-json}"])
    provider = _FakeProvider(commands)
    transport = OpenSandboxPiTransport(provider, default_timeout_seconds=300)  # type: ignore[arg-type]

    with pytest.raises(PiBridgeProtocolError, match="invalid JSONL"):
        await transport.run(
            SandboxRef(id="sandbox-1", project_id="project-1"),
            _invocation(tmp_path),
        )
    assert commands.interrupt_ids == ["execution-1"]


@pytest.mark.asyncio
async def test_transport_rejects_nonzero_exit_after_completed_protocol(tmp_path: Path) -> None:
    commands = _FakeCommands(stdout=_successful_lines(), exit_code=7)
    provider = _FakeProvider(commands)
    transport = OpenSandboxPiTransport(provider, default_timeout_seconds=300)  # type: ignore[arg-type]

    with pytest.raises(PiTransportError, match="exited unsuccessfully"):
        await transport.run(
            SandboxRef(id="sandbox-1", project_id="project-1"),
            _invocation(tmp_path),
        )
