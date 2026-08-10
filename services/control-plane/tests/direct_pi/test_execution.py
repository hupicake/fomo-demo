"""Public event projection tests for Direct Pi execution telemetry."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fomo.direct_pi.execution import PiEventWriter
from fomo.fomo_pi_ds import PiBridgeEnvelope
from tests.helpers import create_user_session


def _repository() -> SimpleNamespace:
    return SimpleNamespace(append_event=AsyncMock(return_value=None))


def _persisted(repository: SimpleNamespace, index: int = -1) -> tuple[str, dict[str, object]]:
    call = repository.append_event.await_args_list[index]
    return call.args[1], call.kwargs["payload"]


@pytest.mark.asyncio
async def test_pi_lifecycle_projects_only_safe_flat_context_usage() -> None:
    repository = _repository()
    writer = PiEventWriter(repository, run_id="run-1", lease_token="lease-1")

    await writer(
        PiBridgeEnvelope(
            seq=1,
            type="started",
            payload={
                "sessionId": "pi-session",
                "model": "fomo-litellm/private-provider-alias",
                "contextWindow": 200_000,
                "initialStats": {
                    "contextUsage": {"tokens": 12_345, "contextWindow": 180_000},
                    "tokens": {"total": 999_999},
                    "cost": 42,
                },
                "privateMessages": ["do not persist"],
            },
        )
    )
    await writer(
        PiBridgeEnvelope(
            seq=2,
            type="completed",
            payload={
                "sessionId": "pi-session",
                "stats": {
                    "contextUsage": {"tokens": 80_000, "contextWindow": 200_000},
                    "tokens": {"total": 1_000_000},
                    "cost": 84,
                },
                "reasoning": "do not persist",
            },
        )
    )

    assert _persisted(repository, 0) == (
        "pi.started",
        {
            "bridgeSeq": 1,
            "contextTokens": 12_345,
            "contextWindow": 180_000,
        },
    )
    assert _persisted(repository, 1) == (
        "pi.completed",
        {
            "bridgeSeq": 2,
            "contextTokens": 80_000,
            "contextWindow": 200_000,
        },
    )
    assert "sessionId" not in _persisted(repository, 0)[1]
    assert "sessionId" not in _persisted(repository, 1)[1]

    await writer(
        PiBridgeEnvelope(
            seq=3,
            type="started",
            payload={
                "contextWindow": 200_000,
                "initialStats": {
                    "contextUsage": {
                        "tokens": -1,
                        "contextWindow": float("nan"),
                    }
                },
            },
        )
    )
    assert _persisted(repository) == (
        "pi.started",
        {"bridgeSeq": 3, "contextWindow": 200_000},
    )


@pytest.mark.asyncio
async def test_pi_failed_projects_only_a_closed_public_failure_contract() -> None:
    repository = _repository()
    writer = PiEventWriter(repository, run_id="run-1", lease_token="lease-1")

    await writer(
        PiBridgeEnvelope(
            seq=4,
            type="failed",
            payload={
                "code": "bridge_error",
                "phase": "running",
                "message": "provider leaked password=private-value",
            },
        ),
        stage="building",
    )

    kind, payload = _persisted(repository)
    assert kind == "pi.failed"
    assert payload == {
        "stage": "building",
        "bridgeSeq": 4,
        "code": "coding_agent_failed",
        "message": "Coding Agent 运行失败，请重试；若问题持续发生，请检查服务状态。",
    }
    assert "private-value" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_pi_event_writer_projects_tool_events_without_commands_or_output() -> None:
    repository = _repository()
    writer = PiEventWriter(repository, run_id="run-1", lease_token="lease-1")
    source = "export PASSWORD=source-password && printf '<main>private source</main>'"

    await writer(
        PiBridgeEnvelope(
            seq=1,
            type="pi.event",
            payload={
                "kind": "tool_start",
                "toolCallId": "write-1",
                "toolName": "write",
                "elapsedMs": 42,
                "args": {
                    "path": "app/page.tsx",
                    "content": source,
                    "reasoning_content": "hidden plan",
                },
            },
        ),
        stage="building",
    )
    kind, payload = _persisted(repository)
    assert kind == "pi.tool.started"
    assert payload == {
        "activity": "tool_start",
        "stage": "building",
        "bridgeSeq": 1,
        "toolCallId": "write-1",
        "toolName": "write",
        "elapsedMs": 42,
        "path": "app/page.tsx",
    }

    await writer(
        PiBridgeEnvelope(
            seq=2,
            type="pi.event",
            payload={
                "kind": "tool_start",
                "toolCallId": "bash-1",
                "toolName": "bash",
                "args": {
                    "command": "env && curl -H 'Cookie: session=cookie-value' example.test",
                    "cwd": "/workspace",
                    "payload": "x" * 100_000,
                },
            },
        ),
        stage="building",
    )
    _, bash_started = _persisted(repository)
    assert bash_started == {
        "activity": "tool_start",
        "stage": "building",
        "bridgeSeq": 2,
        "toolCallId": "bash-1",
        "toolName": "bash",
    }

    await writer(
        PiBridgeEnvelope(
            seq=3,
            type="pi.event",
            payload={
                "kind": "tool_output",
                "toolCallId": "bash-1",
                "toolName": "bash",
                "text": "TOKEN=tool-token\nCookie: session=tool-cookie\n" + source,
                "cumulative": True,
                "elapsedMs": 50,
            },
        ),
        stage="building",
    )
    _, tool_output = _persisted(repository)
    assert tool_output == {
        "activity": "tool_output",
        "stage": "building",
        "bridgeSeq": 3,
        "toolCallId": "bash-1",
        "toolName": "bash",
        "cumulative": True,
        "elapsedMs": 50,
        "suppressed": True,
    }

    await writer(
        PiBridgeEnvelope(
            seq=4,
            type="pi.event",
            payload={
                "kind": "bash_output",
                "delta": "FOMO_PI_VIRTUAL_KEY=virtual-secret\n" + source,
            },
        ),
        stage="building",
    )
    kind, command_output = _persisted(repository)
    assert kind == "pi.command.output"
    assert command_output == {
        "activity": "bash_output",
        "stage": "building",
        "bridgeSeq": 4,
        "suppressed": True,
    }

    persisted = json.dumps(
        [call.kwargs["payload"] for call in repository.append_event.await_args_list]
    )
    for private_value in (
        "source-password",
        "private source",
        "cookie-value",
        "tool-token",
        "tool-cookie",
        "virtual-secret",
        "hidden plan",
        "x" * 1_000,
    ):
        assert private_value not in persisted


@pytest.mark.asyncio
async def test_pi_event_writer_never_persists_structured_form_arguments() -> None:
    repository = _repository()
    writer = PiEventWriter(repository, run_id="run-1", lease_token="lease-1")
    machine_form = {
        "productOutcome": "A complete delivery plan",
        "goals": [{"id": "G-1", "description": "large form " + "x" * 100_000}],
    }
    envelope = PiBridgeEnvelope(
        seq=9,
        type="pi.event",
        payload={
            "kind": "tool_start",
            "toolCallId": "structured-1",
            "toolName": "submit_structured_output",
            "args": machine_form,
        },
    )

    await writer(envelope, stage="planning")

    kind, payload = _persisted(repository)
    assert kind == "pi.tool.started"
    assert payload == {
        "activity": "tool_start",
        "stage": "planning",
        "bridgeSeq": 9,
        "toolCallId": "structured-1",
        "toolName": "submit_structured_output",
    }
    # The writer is only a durable projection. The in-memory bridge envelope
    # remains intact for DirectPiSession._structured_output_text().
    assert envelope.payload["args"] is machine_form
    assert envelope.payload["args"]["goals"][0]["description"].endswith("x" * 100_000)


@pytest.mark.asyncio
async def test_pi_event_writer_redacts_public_text_and_drops_hidden_reasoning() -> None:
    repository = _repository()
    writer = PiEventWriter(repository, run_id="run-1", lease_token="lease-1")
    public_text = "\n".join(
        (
            "Continuing implementation.",
            "Authorization: Bearer bearer-value",
            'API_KEY="api-value"',
            "ACCESS_TOKEN=access-value",
            "PASSWORD: password-value",
            "BUILD_SECRET=secret-value",
            "Cookie: session=cookie-value; refresh=refresh-value",
            "fallback sk-1234567890abcdef",
        )
    )

    await writer(
        PiBridgeEnvelope(
            seq=1,
            type="pi.event",
            payload={
                "kind": "message_delta",
                "deltaType": "text_delta",
                "contentIndex": 0,
                "delta": public_text,
                "thinking": "hidden thought",
                "reasoning_content": "hidden reasoning",
            },
        ),
        stage="building",
    )
    kind, payload = _persisted(repository)
    assert kind == "pi.message.delta"
    assert payload["delta"].startswith("Continuing implementation.")
    serialized = json.dumps(payload)
    for private_value in (
        "bearer-value",
        "api-value",
        "access-value",
        "password-value",
        "secret-value",
        "cookie-value",
        "refresh-value",
        "sk-1234567890abcdef",
        "hidden thought",
        "hidden reasoning",
    ):
        assert private_value not in serialized
    assert serialized.count("[REDACTED]") >= 7

    await writer(
        PiBridgeEnvelope(
            seq=2,
            type="pi.event",
            payload={
                "kind": "turn_end",
                "role": "assistant",
                "stopReason": "stop",
                "text": "Done. TOKEN=final-token SECRET=final-secret",
                "reasoning_content": "final hidden reasoning",
                "toolResults": [
                    {
                        "toolCallId": "tool-1",
                        "toolName": "read",
                        "isError": False,
                        "thinking": "nested hidden thought",
                        "output": "PASSWORD=nested-password",
                    }
                ],
            },
        ),
        stage="building",
    )
    kind, completed = _persisted(repository)
    assert kind == "pi.message.completed"
    assert completed["toolResults"] == [
        {"toolCallId": "tool-1", "toolName": "read", "isError": False}
    ]
    serialized = json.dumps(completed)
    for private_value in (
        "final-token",
        "final-secret",
        "final hidden reasoning",
        "nested hidden thought",
        "nested-password",
    ):
        assert private_value not in serialized


@pytest.mark.asyncio
async def test_pi_event_writer_suppresses_non_text_message_deltas() -> None:
    repository = _repository()
    writer = PiEventWriter(repository, run_id="run-1", lease_token="lease-1")

    await writer(
        PiBridgeEnvelope(
            seq=7,
            type="pi.event",
            payload={
                "kind": "message_delta",
                "deltaType": "thinking_delta",
                "contentIndex": 0,
                "delta": "private chain of thought",
            },
        ),
        stage="building",
    )

    kind, payload = _persisted(repository)
    assert kind == "pi.message.delta"
    assert payload == {
        "activity": "message_delta",
        "stage": "building",
        "bridgeSeq": 7,
        "suppressed": True,
    }
    assert "private chain of thought" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_repository_sse_source_contains_only_the_public_projection(repository) -> None:
    session = await create_user_session(repository)
    project = await repository.create_project(session.id, "Public event projection")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        session.id,
        "public-event-message",
        "Build the page",
    )
    claimed = await repository.claim_next_run("public-event-worker", 60)
    assert claimed is not None and claimed.lease_owner
    writer = PiEventWriter(
        repository,
        run_id=run.id,
        lease_token=claimed.lease_owner,
    )

    await writer(
        PiBridgeEnvelope(
            seq=1,
            type="pi.event",
            payload={
                "kind": "tool_start",
                "toolCallId": "bash-1",
                "toolName": "bash",
                "args": {
                    "command": "env && printf private-source",
                    "environment": "PASSWORD=repository-password",
                },
            },
        ),
        stage="building",
    )
    await writer(
        PiBridgeEnvelope(
            seq=2,
            type="pi.event",
            payload={
                "kind": "bash_output",
                "delta": "Cookie: session=repository-cookie\nTOKEN=repository-token",
            },
        ),
        stage="building",
    )

    events = [event for event in await repository.list_events(run.id) if event.kind.startswith("pi.")]
    assert [event.kind for event in events] == ["pi.tool.started", "pi.command.output"]
    assert events[1].payload["suppressed"] is True
    serialized = "\n".join(event.model_dump_json(by_alias=True) for event in events)
    for private_value in (
        "env && printf",
        "private-source",
        "repository-password",
        "repository-cookie",
        "repository-token",
    ):
        assert private_value not in serialized
