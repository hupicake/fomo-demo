from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fomo.fomo_pi_ds import (
    FOMO_PI_MODEL,
    FOMO_PI_REQUIRE_RESUME,
    FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64,
    FOMO_PI_THINKING,
    FOMO_PI_USER_INPUT_ENABLED,
    PiBridgeFailed,
    PiBridgeProtocolError,
    PiBridgeStreamReducer,
    PiInvocation,
    PiRequest,
)
from fomo.runtime_contract import context_limit_for_model_ref, runtime_profile

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BRIDGE = REPOSITORY_ROOT / "infra" / "opensandbox" / "fomo-pi-rpc-bridge.mjs"


def _stats(session_id: str, *, total: int = 0) -> dict[str, object]:
    return {
        "sessionId": session_id,
        "userMessages": 0,
        "assistantMessages": 0,
        "toolCalls": 0,
        "toolResults": 0,
        "totalMessages": 0,
        "tokens": {
            "input": total,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": total,
        },
        "cost": 0,
    }


def _state(session_id: str) -> dict[str, object]:
    return {
        "sessionId": session_id,
        "messageCount": 0,
        "pendingMessageCount": 0,
        "isStreaming": False,
        "isCompacting": False,
    }


def _line(seq: int, event_type: str, payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            {
                "schemaVersion": 1,
                "requestId": "request-1",
                "correlationId": "run-1",
                "seq": seq,
                "type": event_type,
                "payload": payload,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _started(session_id: str) -> dict[str, object]:
    return {
        "sessionId": session_id,
        "model": FOMO_PI_MODEL,
        "thinkingLevel": FOMO_PI_THINKING,
        "resumed": False,
        "initialStats": _stats(session_id),
    }


def _completed(session_id: str) -> dict[str, object]:
    return {
        "sessionId": session_id,
        "state": _state(session_id),
        "stats": _stats(session_id, total=10),
    }


def test_invocation_keeps_prompt_and_key_out_of_argv_and_repr(tmp_path: Path) -> None:
    request = PiRequest(
        request_id="request-1",
        correlation_id="run-1",
        session_id="session-1",
        provider_base_url="http://litellm:4000/v1",
        prompt="classified prompt",
        virtual_key="dummy-virtual-key",
        workspace=str(tmp_path),
        state_dir=str(tmp_path / "state"),
        bridge_bin=str(BRIDGE),
        pi_bin="/opt/fomo/pi/bin/pi",
        thinking="high",
    )
    invocation = PiInvocation(request)

    argv = invocation.command_line()
    environment = invocation.fomo_environment()

    assert argv == (str(BRIDGE),)
    assert "classified prompt" not in repr(request)
    assert "dummy-virtual-key" not in repr(request)
    assert "classified prompt" not in repr(invocation)
    assert "dummy-virtual-key" not in repr(invocation)
    assert all("classified prompt" not in part for part in argv)
    assert all("dummy-virtual-key" not in part for part in argv)
    assert base64.b64decode(environment["FOMO_PI_PROMPT_B64"]).decode() == "classified prompt"
    assert environment["FOMO_PI_VIRTUAL_KEY"] == "dummy-virtual-key"
    assert environment["FOMO_PI_THINKING_LEVEL"] == "high"
    # Explicit context window travels to the bridge; no business tool policy
    # exists anymore, and an inactivity budget is absent unless requested.
    assert environment["FOMO_PI_CONTEXT_WINDOW"] == "1000000"
    assert "FOMO_PI_TOOL_POLICY_B64" not in environment
    assert "FOMO_PI_ACTIVITY_SILENCE_SECONDS" not in environment
    assert FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64 not in environment


def test_invocation_passes_a_copied_bounded_structured_output_schema() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    request = PiRequest(
        request_id="request-1",
        correlation_id="run-1",
        session_id="session-1",
        provider_base_url="http://litellm:4000/v1",
        prompt="plan",
        virtual_key="dummy-virtual-key",
        structured_output_schema=schema,
    )
    schema["type"] = "array"

    encoded = PiInvocation(request).fomo_environment()[FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64]
    decoded = json.loads(base64.b64decode(encoded))

    assert decoded["type"] == "object"
    assert decoded["required"] == ["answer"]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array"},
        {"type": "object", "const": float("nan")},
        {"type": "object", "description": "x" * (64 * 1024)},
    ],
)
def test_invocation_rejects_invalid_or_oversized_structured_output_schema(
    schema: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="structured_output_schema"):
        PiRequest(
            request_id="request-1",
            correlation_id="run-1",
            session_id="session-1",
            provider_base_url="http://litellm:4000/v1",
            prompt="plan",
            virtual_key="dummy-virtual-key",
            structured_output_schema=schema,
        )


def test_invocation_passes_activity_silence_budget_when_requested(tmp_path: Path) -> None:
    request = PiRequest(
        request_id="request-1",
        correlation_id="run-1",
        session_id="session-1",
        provider_base_url="http://litellm:4000/v1",
        prompt="build",
        virtual_key="dummy-virtual-key",
        activity_silence_seconds=90,
    )
    environment = PiInvocation(request).fomo_environment()
    assert environment["FOMO_PI_ACTIVITY_SILENCE_SECONDS"] == "90"


def test_invocation_explicitly_enables_user_input_and_fail_closed_resume() -> None:
    request = PiRequest(
        request_id="request-1",
        correlation_id="run-1",
        session_id="session-1",
        provider_base_url="http://litellm:4000/v1",
        prompt="continue with the answer",
        virtual_key="dummy-virtual-key",
        user_input_enabled=True,
        require_resume=True,
    )

    environment = PiInvocation(request).fomo_environment()

    assert environment[FOMO_PI_USER_INPUT_ENABLED] == "1"
    assert environment[FOMO_PI_REQUIRE_RESUME] == "1"


def test_reducer_binds_input_request_event_to_completed_payload() -> None:
    reducer = PiBridgeStreamReducer(
        request_id="request-1",
        correlation_id="run-1",
        session_id="session-1",
    )
    input_request = {
        "requestId": "input-123",
        "question": "Which layout?",
        "choices": ["Grid", "List"],
        "allowFreeform": False,
    }
    completed = _completed("session-1")
    completed["inputRequest"] = input_request

    reducer.feed(_line(1, "started", _started("session-1")))
    reducer.feed(
        _line(
            2,
            "pi.event",
            {"kind": "input_request", "inputRequest": input_request},
        )
    )
    reducer.feed(_line(3, "pi.event", {"kind": "agent_settled"}))
    reducer.feed(_line(4, "completed", completed))

    assert reducer.finish().completed["inputRequest"] == input_request


@pytest.mark.parametrize(
    ("model", "thinking"),
    [
        (FOMO_PI_MODEL, "max"),
        (runtime_profile("gpt-5.6").model_ref, "default"),
    ],
)
def test_invocation_rejects_unsupported_model_thinking_pairs(model: str, thinking: str) -> None:
    with pytest.raises(ValueError, match="not supported by"):
        PiRequest(
            request_id="request-1",
            correlation_id="run-1",
            session_id="session-1",
            provider_base_url="http://litellm:4000/v1",
            prompt="build",
            virtual_key="dummy-key",
            model=model,
            thinking=thinking,
        )


@pytest.mark.parametrize(
    ("model", "thinking"),
    [
        (FOMO_PI_MODEL, "high"),
        (FOMO_PI_MODEL, "off"),
        (runtime_profile("gpt-5.6").model_ref, "xhigh"),
    ],
)
def test_invocation_accepts_supported_model_thinking_pairs(model: str, thinking: str) -> None:
    request = PiRequest(
        request_id="request-1",
        correlation_id="run-1",
        session_id="session-1",
        provider_base_url="http://litellm:4000/v1",
        prompt="build",
        virtual_key="dummy-key",
        model=model,
        thinking=thinking,
        context_window=context_limit_for_model_ref(model),
    )
    assert request.model == model
    assert request.thinking == thinking


@pytest.mark.parametrize(
    "url",
    [
        "http://litellm:4000",
        "http://litellm:4000/v1?token=bad",
        "http://user:pass@litellm:4000/v1",
    ],
)
def test_invocation_rejects_gateway_urls_outside_the_v1_contract(url: str) -> None:
    with pytest.raises(ValueError, match="ending in /v1"):
        PiRequest(
            request_id="request-1",
            correlation_id="run-1",
            session_id="session-1",
            provider_base_url=url,
            prompt="build",
            virtual_key="dummy-key",
        )


def test_reducer_accepts_chunked_happy_path() -> None:
    session_id = "session-1"
    stream = b"".join(
        [
            _line(1, "started", _started(session_id)),
            _line(2, "pi.event", {"kind": "agent_start"}),
            _line(3, "pi.event", {"kind": "message_delta", "delta": "hello"}),
            _line(4, "pi.event", {"kind": "inference_heartbeat"}),
            _line(5, "pi.event", {"kind": "agent_settled"}),
            _line(6, "completed", _completed(session_id)),
        ]
    )
    reducer = PiBridgeStreamReducer(
        request_id="request-1", correlation_id="run-1", session_id=session_id
    )

    for start in range(0, len(stream), 7):
        reducer.feed(stream[start : start + 7])
    result = reducer.finish()

    assert result.started["model"] == FOMO_PI_MODEL
    assert result.completed["stats"]["tokens"]["total"] == 10
    assert [event.type for event in result.events] == [
        "started",
        "pi.event",
        "pi.event",
        "pi.event",
        "pi.event",
        "completed",
    ]


def test_reducer_accepts_the_requested_high_thinking_level() -> None:
    session_id = "session-high"
    started = _started(session_id)
    started["thinkingLevel"] = "high"
    reducer = PiBridgeStreamReducer(
        request_id="request-1",
        correlation_id="run-1",
        session_id=session_id,
        thinking_level="high",
    )

    reducer.feed(_line(1, "started", started))
    reducer.feed(_line(2, "pi.event", {"kind": "agent_settled"}))
    reducer.feed(_line(3, "completed", _completed(session_id)))

    assert reducer.finish().started["thinkingLevel"] == "high"


def test_reducer_accepts_the_requested_off_thinking_level() -> None:
    session_id = "session-off"
    started = _started(session_id)
    started["thinkingLevel"] = "off"
    reducer = PiBridgeStreamReducer(
        request_id="request-1",
        correlation_id="run-1",
        session_id=session_id,
        thinking_level="off",
    )

    reducer.feed(_line(1, "started", started))
    reducer.feed(_line(2, "pi.event", {"kind": "agent_settled"}))
    reducer.feed(_line(3, "completed", _completed(session_id)))

    assert reducer.finish().started["thinkingLevel"] == "off"


def test_reducer_rejects_sequence_gaps_and_hidden_reasoning() -> None:
    reducer = PiBridgeStreamReducer(
        request_id="request-1", correlation_id="run-1", session_id="session-1"
    )
    reducer.feed(_line(1, "started", _started("session-1")))
    with pytest.raises(PiBridgeProtocolError, match="sequence"):
        reducer.feed(_line(3, "pi.event", {"kind": "agent_start"}))

    reducer = PiBridgeStreamReducer(
        request_id="request-1", correlation_id="run-1", session_id="session-1"
    )
    reducer.feed(_line(1, "started", _started("session-1")))
    with pytest.raises(PiBridgeProtocolError, match="hidden reasoning"):
        reducer.feed(
            _line(
                2,
                "pi.event",
                {"kind": "message_delta", "content": {"type": "thinking", "thinking": "secret"}},
            )
        )


def test_reducer_fails_closed_for_terminal_failure_and_unterminated_eof() -> None:
    reducer = PiBridgeStreamReducer(
        request_id="request-1", correlation_id="run-1", session_id="session-1"
    )
    reducer.feed(
        _line(1, "failed", {"code": "unexpected_eof", "message": "stopped", "phase": "running"})
    )
    with pytest.raises(PiBridgeFailed) as failure:
        reducer.finish()
    assert failure.value.payload["code"] == "unexpected_eof"

    reducer = PiBridgeStreamReducer(
        request_id="request-1", correlation_id="run-1", session_id="session-1"
    )
    reducer.feed(_line(1, "started", _started("session-1"))[:-1])
    with pytest.raises(PiBridgeProtocolError, match="unterminated"):
        reducer.finish()


def test_reducer_rejects_malformed_duplicate_and_invalid_utf8_records() -> None:
    cases = [
        b"{not-json}\n",
        b'{"schemaVersion":1,"schemaVersion":1}\n',
        b'{"schemaVersion":"\xff"}\n',
    ]
    for record in cases:
        reducer = PiBridgeStreamReducer(
            request_id="request-1", correlation_id="run-1", session_id="session-1"
        )
        with pytest.raises(PiBridgeProtocolError, match="invalid JSONL"):
            reducer.feed(record)


def _write_fake_pi(path: Path) -> None:
    source = f"""#!{sys.executable}
import json
import os
import sys
import time

args = sys.argv[1:]
session_id = args[args.index("--session-id") + 1]
mode = os.environ.get("FAKE_PI_MODE", "ok")
pid_file = os.environ.get("FAKE_PI_PID_FILE")
if pid_file:
    with open(pid_file, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\\n")
    sys.stdout.flush()

def state():
    return {{
        "model": {{"provider": "fomo-litellm", "id": "fomo-pi-deepseek-flash"}},
        "thinkingLevel": "high",
        "sessionId": session_id,
        "messageCount": 0,
        "pendingMessageCount": 0,
        "isStreaming": False,
        "isCompacting": False,
    }}

def stats():
    return {{
        "sessionId": session_id,
        "userMessages": 0,
        "assistantMessages": 0,
        "toolCalls": 0,
        "toolResults": 0,
        "totalMessages": 0,
        "tokens": {{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
        "cost": 0,
    }}

for raw in sys.stdin:
    command = json.loads(raw)
    kind = command["type"]
    if kind in {{"set_model", "set_thinking_level"}}:
        send({{"type": "response", "id": command["id"], "command": kind, "success": True}})
    elif kind == "get_state":
        send({{"type": "response", "id": command["id"], "command": kind, "success": True, "data": state()}})
    elif kind == "get_session_stats":
        send({{"type": "response", "id": command["id"], "command": kind, "success": True, "data": stats()}})
    elif kind == "prompt":
        print(os.environ.get("FOMO_PI_VIRTUAL_KEY", ""), command.get("message", ""), file=sys.stderr, flush=True)
        send({{"type": "response", "id": command["id"], "command": kind, "success": True}})
        if mode == "malformed":
            sys.stdout.write("not-json\\n")
            sys.stdout.flush()
            sys.exit(0)
        if mode == "early-eof":
            sys.exit(0)
        if mode == "timeout":
            while True:
                time.sleep(60)
        send({{"type": "agent_start"}})
        send({{"type": "message_update", "assistantMessageEvent": {{"type": "thinking_delta", "delta": "hidden thought"}}}})
        send({{"type": "message_update", "assistantMessageEvent": {{"type": "text_delta", "contentIndex": 0, "delta": "visible"}}}})
        send({{"type": "agent_settled"}})
    elif kind == "abort":
        sys.exit(0)
    else:
        raise RuntimeError(f"unexpected fake Pi command: {{kind}}")
    """
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def _run_bridge(tmp_path: Path, mode: str = "ok", *, timeout_seconds: int | None = None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the bridge integration smoke")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_pi = tmp_path / "fake-pi"
    _write_fake_pi(fake_pi)
    pid_file = tmp_path / "fake-pi.pid"
    request = PiRequest(
        request_id="request-1",
        correlation_id="run-1",
        session_id="session-1",
        provider_base_url="http://litellm:4000/v1",
        prompt="classified prompt",
        virtual_key="dummy-virtual-key",
        workspace=str(workspace),
        state_dir=str(tmp_path / "state"),
        bridge_bin=str(BRIDGE),
        pi_bin=str(fake_pi),
        # The canned fake Pi reports the canonical default runtime.
        model=FOMO_PI_MODEL,
        thinking="high",
        timeout_seconds=timeout_seconds,
        grace_seconds=1,
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "FAKE_PI_MODE": mode,
        "FAKE_PI_PID_FILE": str(pid_file),
        **PiInvocation(request).fomo_environment(),
    }
    completed = subprocess.run(
        [node, str(BRIDGE)],
        cwd=workspace,
        env=environment,
        capture_output=True,
        check=False,
        timeout=15,
    )
    return completed, pid_file


def _fake_bridge_reducer() -> PiBridgeStreamReducer:
    """Match the default runtime contract reported by the canned fake Pi."""
    return PiBridgeStreamReducer(
        request_id="request-1",
        correlation_id="run-1",
        session_id="session-1",
        thinking_level="high",
        model_ref=FOMO_PI_MODEL,
    )


def test_bridge_runs_fake_pi_to_clean_completion_without_leaking_secrets(tmp_path: Path) -> None:
    completed, _ = _run_bridge(tmp_path)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert b"dummy-virtual-key" not in completed.stdout
    assert b"dummy-virtual-key" not in completed.stderr
    assert b"classified prompt" not in completed.stdout
    assert b"classified prompt" not in completed.stderr
    assert b"hidden thought" not in completed.stdout

    reducer = _fake_bridge_reducer()
    reducer.feed(completed.stdout)
    result = reducer.finish()
    assert result.completed["sessionId"] == "session-1"
    assert any(event.payload.get("kind") == "message_delta" for event in result.events)


@pytest.mark.parametrize("mode", ["malformed", "early-eof"])
def test_bridge_fails_closed_for_bad_fake_pi_protocol(tmp_path: Path, mode: str) -> None:
    completed, _ = _run_bridge(tmp_path, mode)
    assert completed.returncode != 0
    reducer = _fake_bridge_reducer()
    reducer.feed(completed.stdout)
    with pytest.raises(PiBridgeFailed):
        reducer.finish()


def test_bridge_timeout_kills_the_fake_pi_process_group(tmp_path: Path) -> None:
    completed, pid_file = _run_bridge(tmp_path, "timeout", timeout_seconds=1)
    assert completed.returncode == 124
    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 1
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= deadline:
            pytest.fail("fake Pi process survived bridge timeout cleanup")
        time.sleep(0.02)
