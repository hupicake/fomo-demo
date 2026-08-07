from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from fomo.agent_runtime import SOPRunner
from fomo.ids import utcnow
from fomo.persistence import RunLeaseLost
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.schemas import RunStatus
from fomo.worker.runner import WorkerRunner


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


async def _running_run(repository, *, message_id: str, lease_seconds: int = 60):
    session = await repository.create_guest_session()
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
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        session.id,
        message_id,
        "Create a book management system",
    )
    return project, run


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
