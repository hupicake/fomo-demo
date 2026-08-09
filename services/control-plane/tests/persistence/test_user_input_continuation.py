from __future__ import annotations

import pytest

from fomo.persistence import ConflictError
from fomo.schemas import RunStatus, UserInputRequestDraft


async def _running_run(repository, suffix: str):
    owner = await repository.create_guest_session()
    project = await repository.create_project(owner.id, f"Project {suffix}")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        f"initial-{suffix}",
        "Build the requested page",
    )
    claimed = await repository.claim_next_run(f"worker-{suffix}", 120)
    assert claimed is not None and claimed.lease_owner
    await repository.set_sandbox_id(
        run.id,
        f"sandbox-{suffix}",
        lease_token=claimed.lease_owner,
    )
    await repository.register_sandbox_resource(
        run.id,
        f"sandbox-{suffix}",
        "generation",
        lease_token=claimed.lease_owner,
    )
    return owner, project, run, claimed.lease_owner


@pytest.mark.asyncio
async def test_question_answer_requeues_same_run_and_preserves_exact_continuation(
    repository,
) -> None:
    owner, project, run, lease = await _running_run(repository, "answer")
    assert await repository.get_run_continuation(run.id) is None
    request = await repository.wait_for_user_input(
        run.id,
        UserInputRequestDraft(
            question="Which layout should be authoritative?",
            choices=["Grid", "List"],
            allow_freeform=False,
        ),
        continuation_key="goal_graph.goal_build",
        continuation_context={
            "baselineHashes": {"app/page.tsx": "a" * 64},
            "goalStartHashes": {"app/page.tsx": "a" * 64},
        },
        stage="building",
        goal_id="G-1",
        pi_session_id=f"fomo-{run.id}",
        sandbox_id="sandbox-answer",
        lease_token=lease,
    )

    waiting = await repository.get_run(run.id)
    assert waiting.status is RunStatus.waiting_for_user
    assert waiting.pending_input_request == request
    assert await repository.claim_next_run("other-worker", 120) is None
    snapshot = await repository.get_project_snapshot(project.id, owner.id)
    assert snapshot["pending_input_request"] == request

    with pytest.raises(ConflictError, match="exactly match"):
        await repository.answer_user_input(
            run.id,
            request.id,
            owner.id,
            "answer-invalid",
            "Cards",
        )

    message, answered, queued, created = await repository.answer_user_input(
        run.id,
        request.id,
        owner.id,
        "answer-valid",
        "  Grid  ",
    )
    assert created
    assert message.run_id == run.id
    assert message.content == "Grid"
    assert answered.status == "answered"
    assert queued.id == run.id
    assert queued.status is RunStatus.queued

    same_message, _same_request, same_run, duplicate_created = (
        await repository.answer_user_input(
            run.id,
            request.id,
            owner.id,
            "answer-valid",
            "Grid",
        )
    )
    assert not duplicate_created
    assert same_message.id == message.id
    assert same_run.id == run.id

    resumed_claim = await repository.claim_next_run("resume-worker", 120)
    assert resumed_claim is not None and resumed_claim.id == run.id
    continuation = await repository.get_run_continuation(run.id)
    assert continuation is not None
    assert continuation.request_id == request.id
    assert continuation.answer == "Grid"
    assert continuation.pi_session_id == f"fomo-{run.id}"
    assert continuation.sandbox_id == "sandbox-answer"
    assert continuation.continuation_key == "goal_graph.goal_build"

    event_kinds = [event.kind for event in await repository.list_events(run.id)]
    assert "run.input_requested" in event_kinds
    assert "run.input_answered" in event_kinds


@pytest.mark.asyncio
async def test_waiting_run_cancels_immediately_without_a_worker(repository) -> None:
    _owner, _project, run, lease = await _running_run(repository, "cancel")
    await repository.wait_for_user_input(
        run.id,
        UserInputRequestDraft(
            question="Continue?",
            choices=["Yes", "No"],
            allow_freeform=False,
        ),
        continuation_key="goal_graph.goal_build",
        continuation_context={
            "baselineHashes": {"app/page.tsx": "b" * 64},
            "goalStartHashes": {"app/page.tsx": "b" * 64},
        },
        stage="building",
        goal_id="G-1",
        pi_session_id=f"fomo-{run.id}",
        sandbox_id="sandbox-cancel",
        lease_token=lease,
    )

    cancelled = await repository.request_cancel(run.id)

    assert cancelled.status is RunStatus.cancelled
    assert cancelled.pending_input_request is None
    assert (await repository.list_events(run.id))[-1].kind == "run.cancelled"
