from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from fomo.api import create_app
from fomo.direct_pi.goalgraph import parse_goal_graph_draft
from fomo.persistence.models import RunRecord
from fomo.schemas import RunStatus
from tests.helpers import create_user_session


def _headers(settings, session_id: str) -> dict[str, str]:
    return {"Cookie": f"{settings.session_cookie_key}={session_id}"}


def _one_goal_draft():
    return parse_goal_graph_draft(
        {
            "schemaVersion": 1,
            "productOutcome": "A visible recovered product",
            "goals": [
                {
                    "goalId": "G-1",
                    "title": "Recovered workflow",
                    "productOutcome": "The recovered workflow is usable",
                    "userVisible": True,
                    "dependsOn": [],
                    "acceptance": {
                        "criteria": [
                            {
                                "id": "AC-1",
                                "title": "Workflow is visible",
                                "priority": "must",
                                "given": "the app is open",
                                "when": "the page loads",
                                "then": "the workflow appears",
                            }
                        ],
                        "tests": [
                            {
                                "id": "T-1",
                                "acceptanceId": "AC-1",
                                "title": "Recovered workflow smoke",
                                "actions": [{"kind": "goto", "path": "/"}],
                                "assertions": [
                                    {
                                        "kind": "visible",
                                        "target": {
                                            "by": "role",
                                            "value": "main",
                                            "name": "Application",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        }
    )


async def _terminal_source_with_verified_state(repository):
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, "Recovery project")

    _message, seed, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "seed-message",
        "Create the initial application",
    )
    claimed_seed = await repository.claim_next_run("seed-worker", 120)
    assert claimed_seed is not None and claimed_seed.lease_owner
    version = await repository.create_version(
        seed.id,
        "verified-seed",
        "passed",
        [
            {
                "path": "app/page.tsx",
                "sha256": "7d793037a0760186574b0282f2f435e7",
                "size": 5,
                "mime": "text/plain",
                "content_text": "hello",
            }
        ],
        lease_token=claimed_seed.lease_owner,
    )
    await repository.mark_terminal(
        seed.id,
        RunStatus.succeeded,
        lease_token=claimed_seed.lease_owner,
    )

    _message, source, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "source-message",
        "Add a visible counter and preserve the existing application",
    )
    assert source.base_version_id == version.id
    claimed_source = await repository.claim_next_run("source-worker", 120)
    assert claimed_source is not None and claimed_source.lease_owner
    await repository.create_goal_graph(
        project.id,
        source.id,
        _one_goal_draft(),
        lease_token=claimed_source.lease_owner,
    )
    await repository.activate_goal(
        source.id, "G-1", lease_token=claimed_source.lease_owner
    )
    await repository.claim_goal(
        source.id, "G-1", lease_token=claimed_source.lease_owner
    )
    checkpoint = await repository.record_verified_checkpoint(
        source.id,
        "G-1",
        [
            {
                "path": "app/page.tsx",
                "content": "export default () => <main>Counter</main>\n",
            }
        ],
        [
            {
                "acceptanceKey": "G-1:AC-1",
                "kind": "playwright_smoke",
                "status": "passed",
                "summary": "passed",
            }
        ],
        lease_token=claimed_source.lease_owner,
    )
    await repository.mark_terminal(
        source.id,
        RunStatus.failed,
        error_code="agent_protocol_failed",
        lease_token=claimed_source.lease_owner,
    )
    return owner, project, source, version, checkpoint


@pytest.mark.asyncio
async def test_terminal_recovery_forks_verified_history_and_preserves_source(
    repository, settings
) -> None:
    owner, project, source, version, checkpoint = (
        await _terminal_source_with_verified_state(repository)
    )
    source_before = await repository.get_run(source.id)
    source_events_before = await repository.list_events(source.id)
    app = create_app(settings, repository)
    headers = _headers(settings, owner.id)
    body = {
        "clientMessageId": "recovery-message",
        "content": "Keep the verified counter and repair the failed publication.",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            f"/v1/runs/{source.id}/recover",
            headers={**headers, "Idempotency-Key": "recovery-message"},
            json=body,
        )
        assert created.status_code == 202
        payload = created.json()
        recovered = payload["run"]
        assert payload["recoveryMode"] == "verified_checkpoint"
        assert payload["sourceCheckpointAvailable"] is True
        assert recovered["baseVersionId"] == version.id
        assert recovered["recoveredFromRunId"] == source.id
        assert recovered["recoveredFromGoalId"] == "G-1"
        assert recovered["recoveredFromCheckpointId"] == checkpoint.id
        assert recovered["recoveryMode"] == "verified_checkpoint"
        assert recovered["status"] == "queued"

        replay = await client.post(
            f"/v1/runs/{source.id}/recover",
            headers={**headers, "Idempotency-Key": "recovery-message"},
            json=body,
        )
        assert replay.status_code == 200
        assert replay.json()["run"]["id"] == recovered["id"]
        conflict = await client.post(
            f"/v1/runs/{source.id}/recover",
            headers={**headers, "Idempotency-Key": "recovery-message"},
            json={**body, "content": "A different follow-up"},
        )
        assert conflict.status_code == 409

        listed = await client.get("/v1/projects", headers=headers)
        latest = listed.json()[0]["latestRun"]
        assert latest == {
            "id": recovered["id"],
            "status": "queued",
            "errorCode": None,
            "agentFramework": "pi",
            "profileId": "deepseek-flash",
            "thinking": "high",
            "recoveryAvailable": False,
            "recoveryMode": "verified_checkpoint",
            "sourceCheckpointAvailable": False,
        }

        other = await create_user_session(repository)
        forbidden = await client.post(
            f"/v1/runs/{source.id}/recover",
            headers=_headers(settings, other.id),
            json={
                "clientMessageId": "foreign-recovery",
                "content": "Do not disclose this run",
            },
        )
        assert forbidden.status_code == 403

    source_after = await repository.get_run(source.id)
    assert source_after.status is RunStatus.failed
    assert source_after.updated_at == source_before.updated_at
    assert await repository.list_events(source.id) == source_events_before
    selected_checkpoint = await repository.get_recovery_checkpoint(recovered["id"])
    assert selected_checkpoint is not None and selected_checkpoint.id == checkpoint.id
    prompt = await repository.get_run_prompt(recovered["id"])
    assert "Original request:\nAdd a visible counter" in prompt
    assert "Recovery follow-up 1:\nKeep the verified counter" in prompt
    async with repository.database.session_factory() as session:
        source_record = await session.get(RunRecord, source.id)
        recovery_record = await session.get(RunRecord, recovered["id"])
        assert source_record is not None and recovery_record is not None
        assert recovery_record.pi_session_id != source_record.pi_session_id
        assert recovery_record.sandbox_id is None


@pytest.mark.asyncio
async def test_failed_recovery_without_a_new_checkpoint_inherits_verified_source_state(
    repository,
) -> None:
    owner, _project, source, _version, checkpoint = (
        await _terminal_source_with_verified_state(repository)
    )
    _message, first_recovery, created, mode, checkpoint_available = (
        await repository.create_recovery_message_and_run(
            source.id,
            owner.id,
            "first-recovery",
            "Continue from the verified counter checkpoint.",
        )
    )
    assert created and mode == "verified_checkpoint" and checkpoint_available

    claimed = await repository.claim_next_run("first-recovery-worker", 120)
    assert claimed is not None and claimed.id == first_recovery.id
    assert claimed.lease_owner
    assert await repository.get_latest_verified_checkpoint(first_recovery.id) is None
    await repository.mark_terminal(
        first_recovery.id,
        RunStatus.failed,
        error_code="coding_agent_runtime_failed",
        lease_token=claimed.lease_owner,
    )

    _message, second_recovery, created, mode, checkpoint_available = (
        await repository.create_recovery_message_and_run(
            first_recovery.id,
            owner.id,
            "second-recovery",
            "Retry without losing the already verified counter.",
        )
    )

    assert created and mode == "verified_checkpoint" and checkpoint_available
    assert second_recovery.recovered_from_run_id == first_recovery.id
    assert second_recovery.recovered_from_goal_id == checkpoint.goal_id
    assert second_recovery.recovered_from_checkpoint_id == checkpoint.id
    inherited = await repository.get_recovery_checkpoint(second_recovery.id)
    assert inherited is not None and inherited.id == checkpoint.id


@pytest.mark.asyncio
async def test_verified_version_is_used_when_source_has_no_checkpoint(
    repository,
) -> None:
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, "Verified version recovery")
    _message, seed, _created = await repository.create_message_and_run(
        project.id, owner.id, "version-seed", "Create the verified baseline"
    )
    claimed = await repository.claim_next_run("version-worker", 120)
    assert claimed is not None and claimed.lease_owner
    version = await repository.create_version(
        seed.id,
        "verified-baseline",
        "passed",
        [
            {
                "path": "app/page.tsx",
                "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
                "size": 5,
                "mime": "text/plain",
                "content_text": "hello",
            }
        ],
        lease_token=claimed.lease_owner,
    )
    await repository.mark_terminal(
        seed.id, RunStatus.succeeded, lease_token=claimed.lease_owner
    )
    _message, source, _created = await repository.create_message_and_run(
        project.id, owner.id, "version-source", "Extend the verified baseline"
    )
    await repository.request_cancel(source.id)

    _message, recovered, created, mode, checkpoint_available = (
        await repository.create_recovery_message_and_run(
            source.id,
            owner.id,
            "version-recovery",
            "Retry from the verified baseline",
        )
    )

    assert created is True
    assert mode == "verified_version"
    assert checkpoint_available is False
    assert recovered.base_version_id == version.id


@pytest.mark.asyncio
async def test_recovery_explicitly_restarts_from_base_and_freezes_selected_runtime(
    repository, settings, monkeypatch
) -> None:
    async def discovered(_self) -> set[str]:
        return {"fomo-pi-gpt-5.6"}

    monkeypatch.setattr(
        "fomo.api.app.LiteLLMRunKeyClient.discover_model_aliases", discovered
    )
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, "Base restart")
    _message, source, _created = await repository.create_message_and_run(
        project.id, owner.id, "failed-source", "Build a dashboard"
    )
    await repository.request_cancel(source.id)
    configured = replace(
        settings,
        agent_framework="direct_pi",
        agent_enabled_frameworks=("pi", "opencode"),
        litellm_api_key="sk-test-management",
        runtime_enabled_profiles=("gpt-5.6",),
        runtime_default_profile="gpt-5.6",
    )
    app = create_app(configured, repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/runs/{source.id}/recover",
            headers={
                **_headers(configured, owner.id),
                "Idempotency-Key": "runtime-recovery",
            },
            json={
                "clientMessageId": "runtime-recovery",
                "content": "Retry with the selected Coding Agent.",
                "agentFramework": "opencode",
                "profileId": "gpt-5.6",
                "thinking": "medium",
            },
        )
        assert response.status_code == 202
        payload = response.json()
        assert payload["recoveryMode"] == "base_restart"
        assert payload["sourceCheckpointAvailable"] is False
        assert payload["run"]["baseVersionId"] is None
        assert payload["run"]["agentFramework"] == "opencode"
        assert payload["run"]["runtime"]["profileId"] == "gpt-5.6"
        assert payload["run"]["runtime"]["thinking"] == "medium"

        _message, live, _created = await repository.create_message_and_run(
            project.id, owner.id, "live-source", "A live run"
        )
        rejected = await client.post(
            f"/v1/runs/{live.id}/recover",
            headers=_headers(configured, owner.id),
            json={
                "clientMessageId": "live-recovery",
                "content": "Must not fork mutable history",
            },
        )
        assert rejected.status_code == 409

    async with repository.database.session_factory() as session:
        source_record = await session.scalar(
            select(RunRecord).where(RunRecord.id == source.id)
        )
        assert source_record is not None and source_record.status == "cancelled"
