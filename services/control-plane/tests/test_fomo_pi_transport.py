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
    OpenSandboxCodexTransport,
    OpenSandboxOpenCodeTransport,
    OpenSandboxPiTransport,
    PiBridgeFailed,
    PiBridgeProtocolError,
    PiInvocation,
    PiRequest,
    PiTransportCancelled,
    PiTransportError,
    RunVirtualKey,
)
from fomo.runtime_contract import runtime_profile
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


def _successful_lines(
    *, model: str = FOMO_PI_MODEL, thinking: str = FOMO_PI_THINKING
) -> list[str]:
    session_id = "session-1"
    return [
        _line(
            1,
            "started",
            {
                "sessionId": session_id,
                "model": model,
                "thinkingLevel": thinking,
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
        exit_code: int | None = 0,
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
    def _execution(*, exit_code: int | None) -> Any:
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


def _preflight_key() -> RunVirtualKey:
    return RunVirtualKey(
        run_id="preflight-1",
        key_alias="fomo-run-preflight-1",
        duration_seconds=300,
        secret="sk-preflight-secret",
        model_aliases=(runtime_profile("deepseek-flash").litellm_alias,),
    )


@pytest.mark.asyncio
async def test_transport_runs_silent_runtime_probe_with_secret_only_in_environment() -> None:
    commands = _FakeCommands(
        stdout=["private provider response sk-preflight-secret"],
        stderr=["private provider error sk-preflight-secret"],
    )
    provider = _FakeProvider(commands)
    transport = OpenSandboxPiTransport(provider, default_timeout_seconds=300)  # type: ignore[arg-type]

    await transport.preflight_gateway(
        SandboxRef(id="sandbox-1", project_id="runtime-preflight-1"),
        _preflight_key(),
        provider_base_url="http://host.docker.internal:4000/v1",
        timeout_seconds=195,
    )

    assert commands.command == "/opt/fomo/bin/fomo-runtime-preflight.mjs"
    assert "sk-preflight-secret" not in commands.command
    assert commands.opts.working_directory == "/workspace"
    assert commands.opts.timeout.total_seconds() == 195
    assert commands.opts.envs == {
        "FOMO_PREFLIGHT_PROVIDER_BASE_URL": "http://host.docker.internal:4000/v1",
        "FOMO_PREFLIGHT_VIRTUAL_KEY": "sk-preflight-secret",
        "FOMO_PREFLIGHT_ALIASES_JSON": json.dumps(
            _preflight_key().model_aliases,
            separators=(",", ":"),
        ),
    }


@pytest.mark.asyncio
async def test_transport_runtime_probe_failure_never_exposes_output_or_key() -> None:
    commands = _FakeCommands(
        stderr=["private provider error sk-preflight-secret"],
        exit_code=7,
    )
    provider = _FakeProvider(commands)
    transport = OpenSandboxPiTransport(provider, default_timeout_seconds=300)  # type: ignore[arg-type]

    with pytest.raises(PiTransportError) as failure:
        await transport.preflight_gateway(
            SandboxRef(id="sandbox-1", project_id="runtime-preflight-1"),
            _preflight_key(),
            provider_base_url="http://host.docker.internal:4000/v1",
            timeout_seconds=195,
        )

    rendered = str(failure.value)
    assert rendered == "OpenSandbox runtime preflight command failed"
    assert "sk-preflight-secret" not in rendered
    assert "private provider" not in rendered


@pytest.mark.asyncio
async def test_transport_runtime_probe_requires_explicit_zero_exit_code() -> None:
    commands = _FakeCommands(exit_code=None)
    provider = _FakeProvider(commands)
    transport = OpenSandboxPiTransport(provider, default_timeout_seconds=300)  # type: ignore[arg-type]

    with pytest.raises(PiTransportError, match="runtime preflight command failed"):
        await transport.preflight_gateway(
            SandboxRef(id="sandbox-1", project_id="runtime-preflight-1"),
            _preflight_key(),
            provider_base_url="http://host.docker.internal:4000/v1",
            timeout_seconds=195,
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
    assert commands.opts.envs["FOMO_PI_CONTEXT_WINDOW"] == "1000000"
    assert "FOMO_PI_TOOL_POLICY_B64" not in commands.opts.envs
    assert "private prompt" not in result.stderr
    assert "sk-run-secret" not in result.stderr
    assert all("private prompt" not in value for value in diagnostics)
    assert all("sk-run-secret" not in value for value in diagnostics)


@pytest.mark.asyncio
async def test_opencode_transport_selects_root_owned_bridge_and_isolated_state(
    tmp_path: Path,
) -> None:
    commands = _FakeCommands(stdout=_successful_lines())
    provider = _FakeProvider(commands)
    transport = OpenSandboxOpenCodeTransport(  # type: ignore[arg-type]
        provider,
        default_timeout_seconds=300,
    )
    original = _invocation(tmp_path)

    result = await transport.run(
        SandboxRef(id="sandbox-1", project_id="project-1"),
        original,
    )

    assert result.bridge.completed["sessionId"] == "session-1"
    assert commands.command == "/opt/fomo/bin/fomo-opencode-rpc-bridge.mjs"
    assert commands.opts.envs["FOMO_PI_STATE_DIR"] == "/var/lib/fomo-opencode"
    assert commands.opts.envs["FOMO_PI_BIN"] == "/opt/fomo/pi/bin/opencode"
    assert commands.opts.envs["FOMO_PI_VIRTUAL_KEY"] == "sk-run-secret"
    # The caller's immutable Pi invocation is unchanged and can still be used
    # by the Pi runtime after framework dispatch.
    assert original.command_line() == ("/opt/fomo/bin/fomo-pi-rpc-bridge.mjs",)
    assert original.request.state_dir == str(tmp_path / "state")


@pytest.mark.asyncio
async def test_opencode_transport_projects_protocol_failure_as_runtime_failure(
    tmp_path: Path,
) -> None:
    commands = _FakeCommands(stdout=["{provider_body: password=private-value}"])
    provider = _FakeProvider(commands)
    transport = OpenSandboxOpenCodeTransport(  # type: ignore[arg-type]
        provider,
        default_timeout_seconds=300,
    )

    with pytest.raises(PiBridgeFailed) as failure:
        await transport.run(
            SandboxRef(id="sandbox-1", project_id="project-1"),
            _invocation(tmp_path),
        )

    assert failure.value.payload == {
        "code": "opencode_runtime_failed",
        "message": "OpenCode runtime could not complete the request.",
        "phase": "transport",
    }
    assert "private-value" not in str(failure.value)


@pytest.mark.asyncio
async def test_codex_transport_selects_isolated_runtime_and_strict_resume(
    tmp_path: Path,
) -> None:
    profile = runtime_profile("gpt-5.6")
    invocation = PiInvocation(
        PiRequest(
            request_id="request-1",
            correlation_id="run-1",
            session_id="session-1",
            provider_base_url="http://litellm:4000/v1",
            prompt="private prompt",
            virtual_key="sk-run-secret",
            model=profile.model_ref,
            thinking="xhigh",
            context_window=profile.context_window,
            user_input_enabled=False,
            require_resume=True,
        )
    )
    commands = _FakeCommands(
        stdout=_successful_lines(model=profile.model_ref, thinking="xhigh")
    )
    provider = _FakeProvider(commands)
    transport = OpenSandboxCodexTransport(  # type: ignore[arg-type]
        provider,
        default_timeout_seconds=300,
    )

    result = await transport.run(
        SandboxRef(id="sandbox-1", project_id="project-1"), invocation
    )

    assert result.bridge.completed["sessionId"] == "session-1"
    assert commands.command == "/opt/fomo/bin/fomo-codex-rpc-bridge.mjs"
    assert commands.opts.envs["FOMO_PI_STATE_DIR"] == "/var/lib/fomo-codex"
    assert commands.opts.envs["FOMO_PI_BIN"] == "/opt/fomo/pi/bin/codex"
    assert commands.opts.envs["FOMO_PI_REQUIRE_RESUME"] == "1"
    assert "FOMO_PI_USER_INPUT_ENABLED" not in commands.opts.envs
    assert invocation.command_line() == ("/opt/fomo/bin/fomo-pi-rpc-bridge.mjs",)


@pytest.mark.asyncio
async def test_codex_transport_fails_closed_without_framework_fallback(
    tmp_path: Path,
) -> None:
    commands = _FakeCommands(stdout=["{private-provider-body}"])
    provider = _FakeProvider(commands)
    transport = OpenSandboxCodexTransport(  # type: ignore[arg-type]
        provider,
        default_timeout_seconds=300,
    )

    with pytest.raises(PiBridgeFailed) as unsupported:
        await transport.run(
            SandboxRef(id="sandbox-1", project_id="project-1"),
            _invocation(tmp_path),
        )
    assert unsupported.value.payload["code"] == "codex_profile_unsupported"
    assert provider.refs == []

    profile = runtime_profile("gpt-5.5")
    supported = PiInvocation(
        PiRequest(
            request_id="request-1",
            correlation_id="run-1",
            session_id="session-1",
            provider_base_url="http://litellm:4000/v1",
            prompt="private prompt",
            virtual_key="sk-run-secret",
            model=profile.model_ref,
            thinking="high",
            context_window=profile.context_window,
        )
    )
    with pytest.raises(PiBridgeFailed) as malformed:
        await transport.run(
            SandboxRef(id="sandbox-1", project_id="project-1"), supported
        )
    assert malformed.value.payload == {
        "code": "codex_runtime_failed",
        "message": "Codex runtime could not complete the request.",
        "phase": "transport",
    }
    assert "private-provider-body" not in str(malformed.value)


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
