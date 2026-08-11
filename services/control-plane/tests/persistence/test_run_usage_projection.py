from __future__ import annotations

import httpx
import pytest

from fomo.api import create_app
from fomo.schemas import RunStatus
from tests.helpers import create_user_session


def _headers(settings, session_id: str) -> dict[str, str]:
    return {"Cookie": f"{settings.session_cookie_key}={session_id}"}


@pytest.mark.asyncio
async def test_terminal_usage_is_aggregated_in_owned_run_and_project_views(
    repository,
    settings,
) -> None:
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, "Usage projection")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "usage-message",
        "Build a frontend",
    )
    claimed = await repository.claim_next_run("usage-worker", 120)
    assert claimed is not None and claimed.id == run.id and claimed.lease_owner

    await repository.record_usage_entry(
        run.id,
        "provider-request-1",
        lease_token=claimed.lease_owner,
        provider="test",
        model="model",
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=300,
        cache_write_tokens=10,
        tool_calls=2,
    )
    await repository.record_usage_entry(
        run.id,
        "provider-request-2",
        lease_token=claimed.lease_owner,
        provider="test",
        model="model",
        input_tokens=50,
        output_tokens=5,
        cache_read_tokens=20,
        tool_calls=1,
    )
    await repository.mark_terminal(
        run.id,
        RunStatus.succeeded,
        lease_token=claimed.lease_owner,
    )

    expected = {
        "inputTokens": 150,
        "outputTokens": 25,
        "cacheReadTokens": 320,
        "cacheWriteTokens": 10,
        "totalTokens": 505,
        "toolCalls": 3,
    }
    app = create_app(settings, repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        owner_headers = _headers(settings, owner.id)

        run_response = await client.get(f"/v1/runs/{run.id}", headers=owner_headers)
        assert run_response.status_code == 200
        assert run_response.json()["usage"] == expected

        project_response = await client.get(f"/v1/projects/{project.id}", headers=owner_headers)
        assert project_response.status_code == 200
        snapshot = project_response.json()
        assert snapshot["activeRun"] is None
        assert snapshot["runs"][0]["usage"] == expected
        assert snapshot["project"]["latestRun"]["usage"] == expected

        projects_response = await client.get("/v1/projects", headers=owner_headers)
        assert projects_response.status_code == 200
        assert projects_response.json()[0]["latestRun"]["usage"] == expected

        stranger = await create_user_session(repository)
        forbidden = await client.get(
            f"/v1/runs/{run.id}",
            headers=_headers(settings, stranger.id),
        )
        assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_usage_is_hidden_until_terminal_and_missing_ledger_remains_unknown(
    repository,
    settings,
) -> None:
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, "Historical usage")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "historical-message",
        "Build a frontend",
    )
    claimed = await repository.claim_next_run("historical-worker", 120)
    assert claimed is not None and claimed.id == run.id and claimed.lease_owner

    app = create_app(settings, repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        headers = _headers(settings, owner.id)
        running = await client.get(f"/v1/runs/{run.id}", headers=headers)
        assert running.status_code == 200
        assert running.json()["usage"] is None

        await repository.mark_terminal(
            run.id,
            RunStatus.failed,
            error_code="runtime_unavailable",
            lease_token=claimed.lease_owner,
        )
        terminal = await client.get(f"/v1/runs/{run.id}", headers=headers)
        assert terminal.status_code == 200
        assert terminal.json()["usage"] is None
