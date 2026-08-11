from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from fomo.agent_runtime import SOPRunner
from fomo.direct_pi.goalgraph import GoalStatus, parse_legacy_goal_graph_draft
from fomo.ids import utcnow
from fomo.persistence import RunLeaseLost
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.schemas import RunStatus
from fomo.worker.runner import WorkerRunner
from tests.helpers import create_user_session


def _acceptance(identifier: str):
    return {
        "criteria": [
            {
                "id": identifier,
                "title": identifier,
                "priority": "must",
                "given": "the app is open",
                "when": "the flow runs",
                "then": "the result appears",
            }
        ],
        "tests": [
            {
                "id": f"T-{identifier}",
                "acceptanceId": identifier,
                "title": identifier,
                "actions": [{"kind": "goto", "path": "/"}],
                "assertions": [
                    {
                        "kind": "visible",
                        "target": {"by": "role", "value": "main", "name": "App"},
                    }
                ],
            }
        ],
    }


def _goal_draft():
    return parse_legacy_goal_graph_draft(
        {
            "schemaVersion": 1,
            "productOutcome": "A recoverable app",
            "goals": [
                {
                    "goalId": "G-1",
                    "title": "Foundation",
                    "productOutcome": "Foundation works",
                    "userVisible": True,
                    "dependsOn": [],
                    "acceptance": _acceptance("AC-1"),
                },
                {
                    "goalId": "G-2",
                    "title": "Experience",
                    "productOutcome": "Experience works",
                    "userVisible": True,
                    "dependsOn": ["G-1"],
                    "acceptance": _acceptance("AC-2"),
                },
            ],
        }
    )


class _BlockingModel:
    """A model call that only finishes when the SOP cancels its task."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._never = asyncio.Event()

    async def complete_json(self, _model_alias, _messages, _schema_name, *, on_retry=None):
        self.started.set()
        try:
            await self._never.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("blocking model unexpectedly completed")


class _TrackingSandbox(FakeSandboxProvider):
    def __init__(self) -> None:
        super().__init__()
        self.killed_ids: list[str] = []

    async def kill(self, ref) -> None:
        self.killed_ids.append(ref.id)
        await super().kill(ref)


class _FailingCleanupSandbox(FakeSandboxProvider):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def kill(self, ref) -> None:
        self.attempts += 1
        raise RuntimeError("cleanup unavailable")


class _RecordingOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, run_id: str, *, lease_token: str | None = None) -> None:
        self.calls.append((run_id, lease_token))


async def _running_run(repository, *, message_id: str, lease_seconds: int = 60):
    session = await create_user_session(repository)
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        session.id,
        message_id,
        "Create a book management system",
    )
    claimed = await repository.claim_next_run("stale-worker", lease_seconds)
    assert claimed is not None and claimed.id == run.id
    return project, run


async def _queued_run(repository, *, message_id: str):
    session = await create_user_session(repository)
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        session.id,
        message_id,
        "Create a book management system",
    )
    return project, run


@pytest.mark.asyncio
async def test_worker_preflight_failure_does_not_claim_and_recovery_claims(
    repository,
    settings,
    caplog,
) -> None:
    _project, run = await _queued_run(repository, message_id="preflight-recovery")
    attempts = 0

    async def runtime_preflight() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("master-secret private provider response")

    clock = [100.0]
    orchestrator = _RecordingOrchestrator()
    worker = WorkerRunner(
        repository,
        replace(settings, agent_framework="direct_pi"),
        sandbox=FakeSandboxProvider(),
        direct_orchestrator=orchestrator,
        runtime_preflight=runtime_preflight,
        monotonic=lambda: clock[0],
        worker_id="preflight-worker",
    )
    caplog.set_level(logging.WARNING)

    assert not await worker.run_once()
    assert attempts == 1
    assert (await repository.get_run(run.id)).status == RunStatus.queued
    assert orchestrator.calls == []
    assert "master-secret" not in caplog.text
    assert "private provider response" not in caplog.text

    clock[0] += 1.1
    assert await worker.run_once()
    assert attempts == 2
    assert (await repository.get_run(run.id)).status == RunStatus.running
    assert len(orchestrator.calls) == 1
    assert orchestrator.calls[0][0] == run.id
    assert orchestrator.calls[0][1]


@pytest.mark.asyncio
async def test_worker_does_not_spend_on_preflight_while_queue_is_empty(
    repository,
    settings,
) -> None:
    attempts = 0

    async def runtime_preflight() -> None:
        nonlocal attempts
        attempts += 1

    worker = WorkerRunner(
        repository,
        replace(settings, agent_framework="direct_pi"),
        sandbox=FakeSandboxProvider(),
        direct_orchestrator=_RecordingOrchestrator(),
        runtime_preflight=runtime_preflight,
        worker_id="idle-preflight-worker",
    )

    assert not await worker.run_once()
    assert attempts == 0


@pytest.mark.asyncio
async def test_model_cancel_stops_the_underlying_task_and_marks_run_cancelled(repository, settings) -> None:
    _project, run = await _running_run(repository, message_id="cancel-model")
    model = _BlockingModel()
    task = asyncio.create_task(SOPRunner(repository, model, FakeSandboxProvider(), settings).run(run.id))

    await asyncio.wait_for(model.started.wait(), timeout=1)
    await repository.request_cancel(run.id)
    await asyncio.wait_for(task, timeout=2)

    assert model.cancelled.is_set()
    assert (await repository.get_run(run.id)).status == RunStatus.cancelled


@pytest.mark.asyncio
async def test_worker_heartbeat_prevents_live_run_recovery(repository, settings) -> None:
    _project, run = await _queued_run(repository, message_id="heartbeat")
    model = _BlockingModel()
    worker = WorkerRunner(
        repository,
        replace(settings, worker_lease_seconds=1),
        model=model,
        sandbox=FakeSandboxProvider(),
        worker_id="heartbeat-worker",
    )
    task = asyncio.create_task(worker.run_once())

    await asyncio.wait_for(model.started.wait(), timeout=1)
    await asyncio.sleep(1.1)
    assert await repository.recover_expired_running_runs() == []
    assert (await repository.get_run(run.id)).status == RunStatus.running

    await repository.request_cancel(run.id)
    assert await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_expired_lease_cannot_be_renewed(repository) -> None:
    _project, run = await _queued_run(repository, message_id="expired-renewal")
    claimed = await repository.claim_next_run("lease-worker", lease_seconds=0)

    assert claimed is not None and claimed.id == run.id
    lease_token = claimed.lease_owner
    assert lease_token is not None
    assert lease_token != "lease-worker"
    assert not await repository.renew_lease(run.id, lease_token, lease_seconds=60)


@pytest.mark.asyncio
async def test_recovered_lease_cancels_blocking_model_without_post_terminal_events(repository, settings) -> None:
    _project, run = await _queued_run(repository, message_id="recovered-lease")
    model = _BlockingModel()
    worker = WorkerRunner(
        repository,
        replace(settings, worker_lease_seconds=1),
        model=model,
        sandbox=FakeSandboxProvider(),
        worker_id="recovery-worker",
    )
    task = asyncio.create_task(worker.run_once())

    await asyncio.wait_for(model.started.wait(), timeout=1)
    assert await repository.recover_expired_running_runs(now=utcnow() + timedelta(seconds=2)) == []
    terminal_events = await repository.list_events(run.id)
    terminal_seq = terminal_events[-1].seq
    assert terminal_events[-1].kind == "run.failed"
    assert terminal_events[-1].payload["code"] == "worker_lease_expired"
    assert terminal_events[-1].payload["message"].startswith("执行 Worker")

    assert await asyncio.wait_for(task, timeout=2)
    assert model.cancelled.is_set()
    assert (await repository.get_run(run.id)).status == RunStatus.failed
    assert all(event.seq <= terminal_seq for event in await repository.list_events(run.id))


@pytest.mark.asyncio
async def test_stale_lease_token_cannot_append_event_or_artifact(repository) -> None:
    _project, run = await _queued_run(repository, message_id="stale-token")
    claimed = await repository.claim_next_run("current-worker", lease_seconds=60)

    assert claimed is not None and claimed.id == run.id
    lease_token = claimed.lease_owner
    assert lease_token is not None
    events_before = await repository.list_events(run.id)

    with pytest.raises(RunLeaseLost):
        await repository.append_event(
            run.id,
            "agent.completed",
            role="engineer",
            lease_token=f"{lease_token}-stale",
        )
    with pytest.raises(RunLeaseLost):
        await repository.store_artifact(
            run.id,
            "product_spec",
            {"title": "stale output"},
            role="product_manager",
            lease_token=f"{lease_token}-stale",
        )

    assert [event.seq for event in await repository.list_events(run.id)] == [
        event.seq for event in events_before
    ]
    assert await repository.get_latest_artifact(run.id, "product_spec") is None


@pytest.mark.asyncio
async def test_expired_cancelled_run_is_terminal_once_releases_active_project_and_destroys_sandbox(
    repository, settings
) -> None:
    project, run = await _running_run(repository, message_id="expired-cancel", lease_seconds=0)
    sandbox = _TrackingSandbox()
    ref = await sandbox.create(project.id)
    await repository.set_sandbox_id(run.id, ref.id)
    await repository.request_cancel(run.id)

    worker = WorkerRunner(
        repository,
        settings,
        sandbox=sandbox,
        worker_id="recovery-worker",
    )
    assert not await worker.run_once()

    final = await repository.get_run(run.id)
    assert final.status == RunStatus.cancelled
    assert (await repository.require_project(project.id)).active_run_id is None
    assert sandbox.killed_ids == [ref.id]
    terminal_events = [
        event for event in await repository.list_events(run.id) if event.kind == "run.cancelled"
    ]
    assert len(terminal_events) == 1


@pytest.mark.asyncio
async def test_expired_run_fails_releases_active_project_and_destroys_sandbox(repository, settings) -> None:
    project, run = await _running_run(repository, message_id="expired-failed", lease_seconds=0)
    sandbox = _TrackingSandbox()
    ref = await sandbox.create(project.id)
    await repository.set_sandbox_id(run.id, ref.id)

    worker = WorkerRunner(
        repository,
        settings,
        sandbox=sandbox,
        worker_id="recovery-worker",
    )
    assert not await worker.run_once()

    final = await repository.get_run(run.id)
    assert final.status == RunStatus.failed
    assert final.error_code == "worker_lease_expired"
    assert (await repository.require_project(project.id)).active_run_id is None
    assert sandbox.killed_ids == [ref.id]


@pytest.mark.asyncio
async def test_expired_p1_run_requeues_after_all_sandboxes_are_acknowledged(repository) -> None:
    project, run = await _running_run(repository, message_id="p1-resume", lease_seconds=60)
    lease = await repository.get_active_lease_token(run.id)
    first_claim = await repository.get_run(run.id)
    assert first_claim.execution_started_at is not None
    await repository._create_legacy_goal_graph(
        project.id, run.id, _goal_draft(), lease_token=lease
    )
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    await repository.record_verified_checkpoint(
        run.id,
        "G-1",
        [{"path": "app/page.tsx", "content": "export default function Page() {}\n"}],
        [
            {
                "acceptanceKey": "G-1:AC-1",
                "kind": "fomo_qa_test",
                "status": "passed",
                "summary": "passed",
            }
        ],
        lease_token=lease,
    )
    await repository.claim_goal(run.id, "G-2", lease_token=lease)
    generation_id = await repository.register_sandbox_resource(
        run.id, "sandbox-g", "generation", lease_token=lease
    )
    verification_id = await repository.register_sandbox_resource(
        run.id, "sandbox-v", "verification", lease_token=lease
    )
    await repository.set_sandbox_id(run.id, "sandbox-v", lease_token=lease)
    await repository.set_preview_url(run.id, "http://preview.test", lease_token=lease)

    cleanup = await repository.recover_expired_running_runs(
        now=utcnow() + timedelta(seconds=120)
    )
    assert {item.resource_id for item in cleanup} == {generation_id, verification_id}
    requeued = await repository.get_run(run.id)
    assert requeued.status == RunStatus.queued
    assert requeued.execution_started_at == first_claim.execution_started_at
    project_record = await repository.require_project(project.id)
    assert project_record.active_run_id == run.id
    assert project_record.status == "queued"
    assert await repository.claim_next_run("new-worker", 60) is None

    assert await repository.acknowledge_sandbox_cleanup(generation_id)
    assert await repository.claim_next_run("new-worker", 60) is None
    assert await repository.acknowledge_sandbox_cleanup(verification_id)
    reclaimed = await repository.claim_next_run("new-worker", 60)
    assert reclaimed is not None and reclaimed.id == run.id and reclaimed.lease_owner != lease
    assert reclaimed.execution_started_at == first_claim.execution_started_at.replace(tzinfo=None)
    resumed = await repository.resume_goal(
        run.id, "G-2", lease_token=reclaimed.lease_owner
    )
    assert resumed.graph.goals[1].status is GoalStatus.ACTIVE
    assert (await repository.list_events(run.id))[-1].kind == "goal.resumed"


@pytest.mark.asyncio
async def test_expired_verified_graph_requeues_for_reverification_and_publish(repository) -> None:
    project, run = await _running_run(repository, message_id="verified-resume", lease_seconds=60)
    lease = await repository.get_active_lease_token(run.id)
    await repository._create_legacy_goal_graph(
        project.id, run.id, _goal_draft(), lease_token=lease
    )
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    for goal_id, acceptance_id in (("G-1", "AC-1"), ("G-2", "AC-2")):
        if goal_id == "G-2":
            await repository.claim_goal(run.id, goal_id, lease_token=lease)
        await repository.record_verified_checkpoint(
            run.id,
            goal_id,
            [{"path": "app/page.tsx", "content": f"// {goal_id}\n"}],
            [
                {
                    "acceptanceKey": f"{goal_id}:{acceptance_id}",
                    "kind": "fomo_qa_test",
                    "status": "passed",
                    "summary": "passed",
                }
            ],
            lease_token=lease,
        )
    graph = await repository.get_goal_graph(run.id)
    assert graph is not None and graph.graph.status.value == "verified"

    assert await repository.recover_expired_running_runs(
        now=utcnow() + timedelta(seconds=120)
    ) == []
    assert (await repository.get_run(run.id)).status == RunStatus.queued
    event = (await repository.list_events(run.id))[-1]
    assert event.kind == "goal.resume_scheduled"
    assert event.payload["graphStatus"] == "verified"
    assert event.payload["goalId"] == "G-2"
    assert await repository.claim_next_run("publish-worker", 60) is not None


@pytest.mark.asyncio
async def test_healthy_running_registry_resource_is_not_cleaned(repository, settings) -> None:
    project, run = await _running_run(repository, message_id="healthy-resource", lease_seconds=60)
    lease = await repository.get_active_lease_token(run.id)
    sandbox = _TrackingSandbox()
    ref = await sandbox.create(project.id)
    await repository.register_sandbox_resource(
        run.id, ref.id, "generation", lease_token=lease
    )
    worker = WorkerRunner(repository, settings, sandbox=sandbox, worker_id="other-worker")

    assert not await worker.run_once()
    assert sandbox.killed_ids == []
    assert (await repository.get_run(run.id)).status == RunStatus.running


@pytest.mark.asyncio
async def test_successful_verified_preview_is_retained_until_invalidated(
    repository, settings
) -> None:
    project, run = await _running_run(
        repository, message_id="retained-preview", lease_seconds=60
    )
    lease = await repository.get_active_lease_token(run.id)
    sandbox = _TrackingSandbox()
    ref = await sandbox.create(project.id)
    resource_id = await repository.register_sandbox_resource(
        run.id, ref.id, "verification", lease_token=lease
    )
    await repository.set_sandbox_id(run.id, ref.id, lease_token=lease)
    await repository.set_preview_url(
        run.id, "https://preview.invalid", lease_token=lease
    )
    await repository.mark_terminal(
        run.id, RunStatus.succeeded, lease_token=lease
    )
    worker = WorkerRunner(repository, settings, sandbox=sandbox, worker_id="preview-worker")

    assert not await worker.run_once()
    assert sandbox.killed_ids == []
    assert await repository.list_sandbox_cleanup_targets(run.id) == []

    # Clearing the durable preview makes the retained verification resource
    # explicitly reclaimable on the next cleanup sweep.
    await repository.set_preview_url(run.id, None)
    assert not await worker.run_once()
    assert sandbox.killed_ids == [ref.id]
    assert await repository.list_sandbox_cleanup_targets(run.id) == []
    assert await repository.acknowledge_sandbox_cleanup(resource_id) is False


@pytest.mark.asyncio
async def test_duplicate_cleanup_does_not_ack_when_first_kill_fails(repository, settings) -> None:
    project, run = await _running_run(repository, message_id="cleanup-failure", lease_seconds=60)
    lease = await repository.get_active_lease_token(run.id)
    await repository._create_legacy_goal_graph(
        project.id, run.id, _goal_draft(), lease_token=lease
    )
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    await repository.record_verified_checkpoint(
        run.id,
        "G-1",
        [{"path": "app/page.tsx", "content": "// checkpoint\n"}],
        [
            {
                "acceptanceKey": "G-1:AC-1",
                "kind": "fomo_qa_test",
                "status": "passed",
                "summary": "passed",
            }
        ],
        lease_token=lease,
    )
    resource_id = await repository.register_sandbox_resource(
        run.id, "sandbox-duplicate", "generation", lease_token=lease
    )
    cleanup = await repository.recover_expired_running_runs(
        now=utcnow() + timedelta(seconds=120)
    )
    assert cleanup[0].resource_id == resource_id
    repository.recover_expired_running_runs = AsyncMock(return_value=cleanup)
    sandbox = _FailingCleanupSandbox()
    worker = WorkerRunner(repository, settings, sandbox=sandbox, worker_id="cleanup-worker")

    assert not await worker.run_once()
    assert sandbox.attempts == 1
    pending = await repository.list_sandbox_cleanup_targets(run.id)
    assert [item.resource_id for item in pending] == [resource_id]
    assert await repository.claim_next_run("blocked-worker", 60) is None
