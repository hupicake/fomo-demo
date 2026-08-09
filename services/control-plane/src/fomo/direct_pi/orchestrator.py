"""Thin production orchestrator for one Direct Pi session and clean verification."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError

from fomo.config import Settings
from fomo.fomo_pi_ds import RunVirtualKey
from fomo.ids import utcnow
from fomo.persistence import (
    GoalGraphProjection,
    Repository,
    RunContinuation,
    RunLeaseLost,
    VerifiedCheckpoint,
)
from fomo.sandbox.base import SandboxProvider, SandboxRef
from fomo.schemas import RunPhase, RunStatus
from fomo.starter import resolve_starter_manifest

from .acceptance import (
    CompiledAcceptance,
    compile_acceptance,
    compile_acceptance_suite,
)
from .contracts import PlanningBundle
from .execution import (
    CommandExecutor,
    DirectPiRunCancelled,
    assert_run_active,
)
from .goal_manager import (
    RegressionSuite,
    RuntimeValidationMode,
    RuntimeValidationReason,
    VerifiedGoalEvidence,
    build_regression_suite,
    early_full_validation_reason,
    plan_goal_execution,
    select_executable_goal,
)
from .goalgraph import (
    GoalGraphDraft,
    GoalStatus,
    GraphStatus,
    parse_goal_graph_draft,
    scope_acceptance_contract,
)
from .prompts import (
    GOAL_GRAPH_PLANNING_POLICY,
    build_prompt,
    build_repair_prompt,
    goal_build_prompt,
    goal_graph_planning_correction_prompt,
    goal_graph_planning_prompt,
    goal_repair_prompt,
    planning_correction_prompt,
    planning_prompt,
    repair_prompt,
)
from .session import (
    DirectPiAwaitingUser,
    DirectPiContinuationUnavailable,
    DirectPiSession,
    PiTransport,
)
from .verification import VerificationOutcome, Verifier
from .workspace import (
    AuditedWorkspace,
    CandidateCheckpoint,
    VerificationSnapshot,
    WorkspaceManager,
)

logger = logging.getLogger(__name__)


class RunKeyGateway(Protocol):
    async def issue(
        self,
        *,
        run_id: str,
        duration_seconds: int,
        max_budget: float,
        rpm_limit: int,
        tpm_limit: int,
    ) -> RunVirtualKey: ...

    async def block(self, virtual_key: RunVirtualKey) -> None: ...


class DirectPiOrchestrationError(RuntimeError):
    pass


class DirectPiOrchestrator:
    """FOMO owns contracts and proof; Pi owns planning, code, and repairs."""

    def __init__(
        self,
        repository: Repository,
        sandbox: SandboxProvider,
        settings: Settings,
        gateway: RunKeyGateway,
        transport: PiTransport,
    ) -> None:
        self.repository = repository
        self.sandbox = sandbox
        self.settings = settings
        self.gateway = gateway
        self.transport = transport

    async def run(self, run_id: str, *, lease_token: str | None = None) -> None:
        if (
            self.settings.direct_pi_goal_graph_enabled
            and self.settings.agent_framework == "direct_pi"
        ):
            await self._run_goal_graph(run_id, lease_token=lease_token)
            return
        await self._run_p0(run_id, lease_token=lease_token)

    async def _run_goal_graph(
        self,
        run_id: str,
        *,
        lease_token: str | None = None,
    ) -> None:
        """Execute a frozen GoalGraph one server-selected goal at a time."""

        run = await self.repository.get_run(run_id)
        if run.status in {
            RunStatus.cancelled,
            RunStatus.succeeded,
            RunStatus.failed,
            RunStatus.needs_attention,
            RunStatus.waiting_for_user,
        }:
            return
        try:
            active_lease = lease_token or await self.repository.get_active_lease_token(run_id)
        except RunLeaseLost:
            return

        projection = await self.repository.get_goal_graph_for_run(run_id)
        continuation = await self.repository.get_run_continuation(run_id)
        if continuation is not None and (
            continuation.request_status != "answered" or not continuation.answer
        ):
            await self.repository.mark_terminal(
                run_id,
                RunStatus.needs_attention,
                error_code="continuation_answer_missing",
                summary="The queued clarification continuation has no durable answer.",
                lease_token=active_lease,
            )
            return
        if (
            continuation is not None
            and projection is not None
            and projection.graph.status is GraphStatus.VERIFIED
        ):
            await self.repository.mark_terminal(
                run_id,
                RunStatus.needs_attention,
                error_code="continuation_cursor_invalid",
                summary="A clarification cursor cannot target an already verified graph.",
                lease_token=active_lease,
            )
            return
        recovered = projection is not None and continuation is None
        started_at = self._durable_started_at(run, recovered=recovered)
        generation: SandboxRef | None = None
        verification: SandboxRef | None = None
        keep_verification = False
        virtual_key: RunVirtualKey | None = None
        workspaces: WorkspaceManager | None = None
        current_goal_id: str | None = None
        total_repair_round = run.repair_round
        try:
            await assert_run_active(self.repository, run_id, active_lease)
            await self._phase(run_id, RunPhase.preparing, active_lease)
            requirement = await self.repository.get_run_prompt(run_id)
            starter = resolve_starter_manifest(("crud", "local-persistence"))
            if continuation is None:
                await self.repository.store_artifact(
                    run_id,
                    "run_input",
                    {
                        "title": "User request",
                        "requirement": requirement,
                        "starterId": starter.id,
                        "starterVersion": starter.version,
                        "starterCapabilities": list(starter.capability_ids),
                        "goalGraph": True,
                        "planningPolicy": GOAL_GRAPH_PLANNING_POLICY,
                    },
                    lease_token=active_lease,
                )
            commands = CommandExecutor(
                self.repository,
                self.sandbox,
                self.settings,
                run_id=run_id,
                lease_token=active_lease,
            )
            workspaces = WorkspaceManager(
                self.repository,
                self.sandbox,
                self.settings,
                commands,
                starter,
                run_id=run_id,
                project_id=run.project_id,
                lease_token=active_lease,
            )
            verifier = Verifier(
                self.repository,
                self.sandbox,
                self.settings,
                commands,
                run_id=run_id,
                lease_token=active_lease,
                started_at=started_at,
            )

            latest_checkpoint = await self.repository.get_latest_verified_checkpoint(run_id)
            await assert_run_active(self.repository, run_id, active_lease)
            if continuation is not None:
                generation = SandboxRef(
                    id=continuation.sandbox_id,
                    project_id=run.project_id,
                )
                try:
                    await workspaces.adopt_generation(generation)
                except Exception as exc:
                    raise DirectPiContinuationUnavailable(
                        "the retained continuation sandbox is unavailable"
                    ) from exc
                baseline = self._continuation_hashes(
                    continuation.context,
                    "baselineHashes",
                )
            elif latest_checkpoint is not None:
                generation, baseline = await workspaces.create_generation_from_checkpoint(
                    latest_checkpoint,
                    base_version_id=run.base_version_id,
                )
            else:
                generation = await workspaces.create_generation(run.base_version_id)
                baseline = await workspaces.snapshot_hashes(generation)
            await assert_run_active(self.repository, run_id, active_lease)

            evidence_summaries = self._checkpoint_evidence_summaries(
                latest_checkpoint
            )
            (
                goal_changed_paths_by_id,
                legacy_checkpoint_unknown_paths,
            ) = self._checkpoint_goal_changed_paths(latest_checkpoint)
            if projection is not None and projection.graph.status is GraphStatus.VERIFIED:
                if latest_checkpoint is None:
                    raise DirectPiOrchestrationError(
                        "verified GoalGraph has no durable checkpoint"
                    )
                snapshot, outcome = await self._verify_final_checkpoint(
                    run=run,
                    projection=projection,
                    workspaces=workspaces,
                    verifier=verifier,
                    generation=generation,
                    baseline=baseline,
                    lease_token=active_lease,
                )
                verification = snapshot.ref
                if not outcome.passed:
                    await self._discard_goal_workspace(workspaces, verification)
                    verification = None
                    await self.repository.set_preview_url(
                        run_id, None, lease_token=active_lease
                    )
                    await self.repository.mark_terminal(
                        run_id,
                        RunStatus.needs_attention,
                        error_code="goal_graph_final_reverification_failed",
                        summary="The final verified checkpoint no longer passes full clean verification.",
                        lease_token=active_lease,
                    )
                    return
                await self._publish(
                    run_id,
                    run.project_id,
                    active_lease,
                    workspaces,
                    verifier,
                    snapshot,
                    outcome,
                    None,
                    goal_graph=projection,
                )
                keep_verification = True
                return

            await assert_run_active(self.repository, run_id, active_lease)
            virtual_key = await self.gateway.issue(
                run_id=run_id,
                duration_seconds=self.settings.inference_token_ttl_seconds,
                max_budget=await self._remaining_spend_budget(run_id),
                rpm_limit=self.settings.run_inference_rpm_limit,
                tpm_limit=self.settings.run_inference_tpm_limit,
            )

            pi_session_id = await self.repository.ensure_pi_session_id(
                run_id,
                f"fomo-{run_id}",
                lease_token=active_lease,
            )
            pi = DirectPiSession(
                self.repository,
                self.transport,
                self.settings,
                virtual_key,
                run_id=run_id,
                lease_token=active_lease,
                started_at=started_at,
                session_id=pi_session_id,
            )

            if projection is None:
                await self._phase(run_id, RunPhase.planning, active_lease)
                before_planning = baseline
                draft: GoalGraphDraft | None = None
                starter_fingerprint = {
                    "starterId": starter.id,
                    "starterVersion": starter.version,
                    "starterCapabilities": list(starter.capability_ids),
                    "goalGraph": True,
                    "planningPolicy": GOAL_GRAPH_PLANNING_POLICY,
                }
                continuation_context = {"baselineHashes": before_planning}
                if continuation is not None:
                    if continuation.stage != "planning" or continuation.goal_id is not None:
                        raise DirectPiContinuationUnavailable(
                            "the planning continuation cursor is invalid"
                        )
                    plan_text = await pi.invoke(
                        generation,
                        self._continuation_prompt(continuation),
                        stage="planning",
                        structured_output_schema=GoalGraphDraft.model_json_schema(
                            by_alias=True
                        ),
                        continuation_key=continuation.continuation_key,
                        continuation_context=continuation.context,
                        resume_request_id=continuation.request_id,
                    )
                    await workspaces.assert_unchanged(generation, before_planning)
                    await assert_run_active(self.repository, run_id, active_lease)
                    draft = self._parse_goal_graph_draft(plan_text)
                    continuation = None
                else:
                    candidates = await self.repository.list_goal_graph_cache_candidates(
                        run.project_id,
                        requirement,
                        run.base_version_id,
                        starter_fingerprint,
                    )
                    for candidate in candidates:
                        try:
                            candidate_text = json.dumps(
                                candidate["draft"],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            draft = self._parse_goal_graph_draft(candidate_text)
                        except (DirectPiOrchestrationError, KeyError, TypeError):
                            continue
                        await self.repository.append_event(
                            run_id,
                            "planning.cache_hit",
                            payload={
                                "sourceRunId": candidate["runId"],
                                "contract": "goal_graph",
                            },
                            lease_token=active_lease,
                        )
                        break

                if draft is None:
                    plan_text = await pi.invoke(
                        generation,
                        goal_graph_planning_prompt(
                            requirement=requirement,
                            starter=starter.as_architect_context(),
                        ),
                        stage="planning",
                        structured_output_schema=GoalGraphDraft.model_json_schema(
                            by_alias=True
                        ),
                        continuation_key="goal_graph.planning",
                        continuation_context=continuation_context,
                    )
                    await workspaces.assert_unchanged(generation, before_planning)
                    await assert_run_active(self.repository, run_id, active_lease)
                    try:
                        draft = self._parse_goal_graph_draft(plan_text)
                    except DirectPiOrchestrationError as exc:
                        await self.repository.append_event(
                            run_id,
                            "pi.retrying",
                            payload={
                                "stage": "planning",
                                "reason": "goal_graph_contract_validation",
                                "thinkingLevel": "high",
                            },
                            lease_token=active_lease,
                        )
                        corrected = await pi.invoke(
                            generation,
                            goal_graph_planning_correction_prompt(
                                validation_error=str(exc)
                            ),
                            stage="planning",
                            structured_output_schema=GoalGraphDraft.model_json_schema(
                                by_alias=True
                            ),
                            continuation_key="goal_graph.planning_correction",
                            continuation_context=continuation_context,
                        )
                        await workspaces.assert_unchanged(generation, before_planning)
                        await assert_run_active(self.repository, run_id, active_lease)
                        draft = self._parse_goal_graph_draft(corrected)
                else:
                    await workspaces.assert_unchanged(generation, before_planning)
                    await assert_run_active(self.repository, run_id, active_lease)
                projection = await self.repository.create_goal_graph(
                    run.project_id,
                    run_id,
                    draft,
                    provenance={"createdBy": f"run:{run_id}", "planner": "direct_pi"},
                    lease_token=active_lease,
                )
                await self.repository.store_artifact(
                    run_id,
                    "goal_graph",
                    draft.model_dump(mode="json", by_alias=True),
                    lease_token=active_lease,
                )

            while True:
                current = self._current_goal(projection)
                if current is None:
                    selected = select_executable_goal(projection.graph)
                    if selected is None:
                        raise DirectPiOrchestrationError(
                            "GoalGraph has pending work but no executable goal"
                        )
                    projection = await self.repository.activate_goal(
                        run_id,
                        selected.goal_id,
                        lease_token=active_lease,
                    )
                    current = self._current_goal(projection)
                elif current.status in {GoalStatus.CLAIMED, GoalStatus.ACTIVE} and recovered:
                    projection = await self.repository.resume_goal(
                        run_id,
                        current.goal_id,
                        lease_token=active_lease,
                    )
                    current = self._current_goal(projection)
                    recovered = False
                if current is None or current.status is not GoalStatus.ACTIVE:
                    raise DirectPiOrchestrationError(
                        "Goal Manager did not produce exactly one active goal"
                    )
                current_goal_id = current.goal_id
                _, execution_plan = plan_goal_execution(
                    projection.graph,
                    graph_revision=projection.revision,
                    verified_evidence=evidence_summaries,
                )
                # Freeze the candidate-owned state at this goal boundary. QA
                # breadth must be based on this goal's actual delta, never the
                # cumulative diff from the starter baseline.
                await assert_run_active(self.repository, run_id, active_lease)
                if continuation is not None:
                    if (
                        continuation.stage not in {"building", "repairing"}
                        or continuation.goal_id != current_goal_id
                    ):
                        raise DirectPiContinuationUnavailable(
                            "the code continuation cursor does not match the active goal"
                        )
                    goal_start_checkpoint = self._continuation_checkpoint(
                        continuation.context,
                    )
                    # A question may be asked after arbitrary edits in the Pi
                    # turn. Resume conservatively with full regression breadth.
                    legacy_checkpoint_unknown_paths = True
                else:
                    goal_start_checkpoint = await workspaces.capture_candidate_checkpoint(
                        generation
                    )
                await assert_run_active(self.repository, run_id, active_lease)

                turn_continuation_context = {
                    "baselineHashes": baseline,
                    "goalStartHashes": self._checkpoint_hashes(
                        goal_start_checkpoint
                    ),
                }

                turn_stage = continuation.stage if continuation is not None else "building"
                await self._phase(
                    run_id,
                    RunPhase.repairing if turn_stage == "repairing" else RunPhase.building,
                    active_lease,
                )
                await self.repository.append_event(
                    run_id,
                    "build.turn.started",
                    payload={
                        "stage": turn_stage,
                        "goalId": current_goal_id,
                        "graphRevision": projection.revision,
                        "resumed": continuation is not None,
                    },
                    lease_token=active_lease,
                )
                if continuation is not None:
                    handoff = await pi.invoke(
                        generation,
                        self._continuation_prompt(continuation),
                        stage=turn_stage,
                        goal_id=current_goal_id,
                        continuation_key=continuation.continuation_key,
                        continuation_context=continuation.context,
                        resume_request_id=continuation.request_id,
                    )
                    continuation = None
                else:
                    handoff = await pi.invoke(
                        generation,
                        goal_build_prompt(
                            requirement=requirement,
                            starter=starter.as_architect_context(),
                            execution_plan=execution_plan,
                        ),
                        stage="building",
                        goal_id=current_goal_id,
                        continuation_key="goal_graph.goal_build",
                        continuation_context=turn_continuation_context,
                    )
                await self.repository.append_event(
                    run_id,
                    "build.turn.completed",
                    payload={
                        "goalId": current_goal_id,
                        "claimOnly": True,
                        "handoff": handoff[:1000],
                    },
                    lease_token=active_lease,
                )
                typecheck = await workspaces.typecheck_workspace(generation)
                if typecheck.exit_code != 0 or typecheck.timed_out:
                    if total_repair_round >= self.settings.max_repair_rounds:
                        await self.repository.fail_goal(
                            run_id,
                            current_goal_id,
                            reason="run-total repair rounds exhausted during typecheck",
                            lease_token=active_lease,
                        )
                        await self.repository.mark_terminal(
                            run_id,
                            RunStatus.needs_attention,
                            error_code="goal_typecheck_failed",
                            summary="The current goal failed direct typecheck and the run repair budget is exhausted.",
                            lease_token=active_lease,
                        )
                        return
                    total_repair_round = await self.repository.increment_repair_round(
                        run_id,
                        phase=RunPhase.repairing,
                        lease_token=active_lease,
                    )
                    await pi.invoke(
                        generation,
                        goal_repair_prompt(
                            execution_plan=execution_plan,
                            diagnostic={
                                "gate": "typecheck",
                                "code": (
                                    "timeout" if typecheck.timed_out else "nonzero_exit"
                                ),
                                "summary": "The fixed direct TypeScript check failed.",
                            },
                            round_number=total_repair_round,
                        ),
                        stage="repairing",
                        goal_id=current_goal_id,
                        continuation_key="goal_graph.typecheck_repair",
                        continuation_context=turn_continuation_context,
                    )
                    typecheck = await workspaces.typecheck_workspace(generation)
                    if typecheck.exit_code != 0 or typecheck.timed_out:
                        await self.repository.fail_goal(
                            run_id,
                            current_goal_id,
                            reason="direct typecheck failed after same-goal repair",
                            lease_token=active_lease,
                        )
                        await self.repository.mark_terminal(
                            run_id,
                            RunStatus.needs_attention,
                            error_code="goal_typecheck_failed",
                            summary="The current goal did not pass direct typecheck after repair.",
                            lease_token=active_lease,
                        )
                        return
                projection = await self.repository.claim_goal(
                    run_id,
                    current_goal_id,
                    lease_token=active_lease,
                )
                goal_round = 0

                while True:
                    await assert_run_active(self.repository, run_id, active_lease)
                    audited = await workspaces.audit(generation, baseline=baseline)
                    await assert_run_active(self.repository, run_id, active_lease)
                    await self._persist_goal_diff(
                        run_id,
                        audited,
                        current_goal_id,
                        active_lease,
                    )
                    await assert_run_active(self.repository, run_id, active_lease)
                    candidate_checkpoint = await workspaces.capture_candidate_checkpoint(
                        generation
                    )
                    await assert_run_active(self.repository, run_id, active_lease)
                    goal_changed_paths = self._candidate_delta_paths(
                        goal_start_checkpoint,
                        candidate_checkpoint,
                    )
                    prior_goal_changed_paths = {
                        path
                        for paths in goal_changed_paths_by_id.values()
                        for path in paths
                    }
                    full_reason = (
                        RuntimeValidationReason.LEGACY_CHECKPOINT_UNKNOWN_PATHS
                        if legacy_checkpoint_unknown_paths
                        else early_full_validation_reason(
                            goal_changed_paths,
                            prior_goal_changed_paths=prior_goal_changed_paths,
                        )
                    )
                    suite = build_regression_suite(
                        projection.graph,
                        full_reason=full_reason,
                    )
                    compiled = compile_acceptance_suite(suite.contracts)
                    await self._phase(run_id, RunPhase.verifying, active_lease)
                    await assert_run_active(self.repository, run_id, active_lease)
                    snapshot = await workspaces.create_verification(
                        audited,
                        compiled,
                        base_version_id=run.base_version_id,
                    )
                    verification = snapshot.ref
                    await assert_run_active(self.repository, run_id, active_lease)
                    outcome = await verifier.verify_regression(
                        verification,
                        suite,
                        compiled,
                        round_number=goal_round,
                        candidate_paths=audited.changed_paths,
                    )
                    await assert_run_active(self.repository, run_id, active_lease)
                    if outcome.passed:
                        await assert_run_active(self.repository, run_id, active_lease)
                        await self._assert_verification_stable(
                            workspaces,
                            verifier,
                            snapshot,
                            outcome,
                        )
                        await assert_run_active(self.repository, run_id, active_lease)
                        final_claim = all(
                            goal.goal_id == current_goal_id
                            or goal.status is GoalStatus.VERIFIED
                            for goal in projection.graph.goals
                        )
                        if (
                            final_claim
                            and outcome.validation_mode is not RuntimeValidationMode.FULL
                        ):
                            raise DirectPiOrchestrationError(
                                "final GoalGraph checkpoint requires a full validation suite"
                            )
                        next_summary = VerifiedGoalEvidence(
                            goal_id=current_goal_id,
                            passed_acceptance_ids=tuple(
                                item.id for item in execution_plan.active_goal.acceptance.criteria
                            ),
                            evidence_refs=(
                                f"artifact:{outcome.diagnostic_artifact_id}",
                            ),
                        )
                        evidence_summaries = (*evidence_summaries, next_summary)
                        persisted_goal_paths = (
                            tuple(sorted(set(audited.changed_paths) | set(goal_changed_paths)))
                            if legacy_checkpoint_unknown_paths
                            else goal_changed_paths
                        )
                        goal_changed_paths_by_id = {
                            **goal_changed_paths_by_id,
                            current_goal_id: persisted_goal_paths,
                        }
                        legacy_checkpoint_unknown_paths = False
                        await assert_run_active(self.repository, run_id, active_lease)
                        await self.repository.record_verified_checkpoint(
                            run_id,
                            current_goal_id,
                            candidate_checkpoint.files,
                            outcome.checkpoint_evidence(current_goal_id),
                            lease_token=active_lease,
                            commit_sha=snapshot.commit_sha,
                            capsule=self._checkpoint_capsule(
                                evidence_summaries,
                                goal_changed_paths_by_id,
                            ),
                        )
                        await assert_run_active(self.repository, run_id, active_lease)
                        projection = await self.repository.get_goal_graph_for_run(run_id)
                        if projection is None:
                            raise DirectPiOrchestrationError(
                                "GoalGraph disappeared after checkpoint"
                            )
                        if projection.graph.status is GraphStatus.VERIFIED:
                            await self._publish(
                                run_id,
                                run.project_id,
                                active_lease,
                                workspaces,
                                verifier,
                                snapshot,
                                outcome,
                                None,
                                goal_graph=projection,
                            )
                            keep_verification = True
                            return
                        await self._retire_goal_verification(
                            workspaces,
                            run_id,
                            verification,
                            generation,
                            active_lease,
                            reason="goal_advanced",
                        )
                        verification = None
                        current_goal_id = None
                        break

                    await self.repository.append_event(
                        run_id,
                        "goal.verification_failed",
                        payload={
                            "goalId": current_goal_id,
                            "graphRevision": projection.revision,
                            "round": goal_round,
                            "diagnosticArtifactId": outcome.diagnostic_artifact_id,
                            "infrastructureFailure": outcome.has_infrastructure_failure,
                        },
                        lease_token=active_lease,
                    )
                    terminal = (
                        outcome.has_infrastructure_failure
                        or total_repair_round >= self.settings.max_repair_rounds
                    )
                    if terminal:
                        await self._discard_goal_workspace(workspaces, verification)
                        verification = None
                        await self.repository.set_preview_url(
                            run_id, None, lease_token=active_lease
                        )
                        await self.repository.fail_goal(
                            run_id,
                            current_goal_id,
                            reason=(
                                "verification infrastructure failed"
                                if outcome.has_infrastructure_failure
                                else "goal repair rounds exhausted"
                            ),
                            lease_token=active_lease,
                        )
                        await self.repository.mark_terminal(
                            run_id,
                            RunStatus.needs_attention,
                            error_code=(
                                "goal_verification_infrastructure_failed"
                                if outcome.has_infrastructure_failure
                                else "goal_verification_failed"
                            ),
                            summary="The current goal could not be verified; no claim was promoted.",
                            lease_token=active_lease,
                        )
                        return

                    await self._retire_goal_verification(
                        workspaces,
                        run_id,
                        verification,
                        generation,
                        active_lease,
                        reason="repairing",
                    )
                    verification = None
                    goal_round += 1
                    total_repair_round = await self.repository.increment_repair_round(
                        run_id,
                        phase=RunPhase.repairing,
                        lease_token=active_lease,
                    )
                    projection = await self.repository.resume_goal(
                        run_id,
                        current_goal_id,
                        lease_token=active_lease,
                    )
                    _, repair_plan = plan_goal_execution(
                        projection.graph,
                        graph_revision=projection.revision,
                        verified_evidence=evidence_summaries,
                    )
                    await pi.invoke(
                        generation,
                        goal_repair_prompt(
                            execution_plan=repair_plan,
                            diagnostic=outcome.as_repair_context(),
                            round_number=total_repair_round,
                        ),
                        stage="repairing",
                        goal_id=current_goal_id,
                        continuation_key="goal_graph.verification_repair",
                        continuation_context=turn_continuation_context,
                    )
                    projection = await self.repository.claim_goal(
                        run_id,
                        current_goal_id,
                        lease_token=active_lease,
                    )
        except DirectPiAwaitingUser:
            # wait_for_user_input() already committed the durable cursor and
            # released this worker's lease. Preserve G exactly as-is; the
            # answer path will requeue this same run and adopt the same ref.
            if workspaces is not None and verification is not None:
                await self._discard_goal_workspace(workspaces, verification)
            generation = None
            verification = None
            return
        except DirectPiContinuationUnavailable:
            with suppress(RunLeaseLost):
                await self.repository.append_event(
                    run_id,
                    "run.continuation_unavailable",
                    payload={
                        "requestId": (
                            continuation.request_id
                            if continuation is not None
                            else None
                        )
                    },
                    lease_token=active_lease,
                )
                await self.repository.mark_terminal(
                    run_id,
                    RunStatus.needs_attention,
                    error_code="pi_session_resume_unavailable",
                    summary=(
                        "The exact Pi session or retained sandbox is unavailable; "
                        "the answer was not replayed in a replacement session."
                    ),
                    lease_token=active_lease,
                )
            return
        except DirectPiRunCancelled:
            if workspaces is not None:
                await self._discard_goal_workspace(workspaces, verification)
                await self._discard_goal_workspace(workspaces, generation)
            verification = None
            generation = None
            with suppress(RunLeaseLost):
                with suppress(Exception):
                    await self.repository.terminalize_goal_graph(
                        run_id,
                        GraphStatus.CANCELLED,
                        reason="cancelled by request",
                        lease_token=active_lease,
                    )
                await self.repository.set_preview_url(
                    run_id, None, lease_token=active_lease
                )
                await self.repository.mark_terminal(
                    run_id,
                    RunStatus.cancelled,
                    summary="Cancelled safely by request.",
                    lease_token=active_lease,
                )
        except RunLeaseLost:
            if workspaces is not None:
                await self._discard_goal_workspace(workspaces, generation)
                await self._discard_goal_workspace(workspaces, verification)
            generation = None
            verification = None
            if await self.repository.is_cancel_requested(run_id):
                with suppress(RunLeaseLost):
                    with suppress(Exception):
                        await self.repository.terminalize_goal_graph(
                            run_id,
                            GraphStatus.CANCELLED,
                            reason="cancelled by request",
                            lease_token=active_lease,
                        )
                    await self.repository.set_preview_url(
                        run_id, None, lease_token=active_lease
                    )
                    await self.repository.mark_terminal(
                        run_id,
                        RunStatus.cancelled,
                        summary="Cancelled safely by request.",
                        lease_token=active_lease,
                    )
        except asyncio.CancelledError:
            if workspaces is not None:
                await self._discard_goal_workspace(workspaces, generation)
                if not keep_verification:
                    await self._discard_goal_workspace(workspaces, verification)
            raise
        except Exception as exc:
            logger.error("GoalGraph Direct Pi run failed", extra={"run_id": run_id})
            try:
                if current_goal_id is not None:
                    latest = await self.repository.get_goal_graph_for_run(run_id)
                    current = self._current_goal(latest) if latest is not None else None
                    if current is not None and current.status in {
                        GoalStatus.ACTIVE,
                        GoalStatus.CLAIMED,
                    }:
                        await self.repository.fail_goal(
                            run_id,
                            current.goal_id,
                            reason=type(exc).__name__,
                            lease_token=active_lease,
                        )
                with suppress(Exception):
                    await self.repository.terminalize_goal_graph(
                        run_id,
                        GraphStatus.FAILED,
                        reason=type(exc).__name__,
                        lease_token=active_lease,
                    )
                await self.repository.set_preview_url(
                    run_id, None, lease_token=active_lease
                )
                if workspaces is not None:
                    await self._discard_goal_workspace(workspaces, verification)
                    await self._discard_goal_workspace(workspaces, generation)
                verification = None
                generation = None
                await self.repository.append_event(
                    run_id,
                    "pi.failed",
                    payload={"errorType": type(exc).__name__, "goalId": current_goal_id},
                    lease_token=active_lease,
                )
                await self.repository.mark_terminal(
                    run_id,
                    RunStatus.failed,
                    error_code="goal_graph_execution_error",
                    summary="GoalGraph execution stopped before a verified release was created.",
                    lease_token=active_lease,
                )
            except RunLeaseLost:
                pass
            raise
        finally:
            if workspaces is not None:
                if generation is not None:
                    await self._discard_goal_workspace(workspaces, generation)
                if verification is not None and not keep_verification:
                    await self._discard_goal_workspace(workspaces, verification)
            if virtual_key is not None:
                try:
                    await self.gateway.block(virtual_key)
                except Exception:
                    logger.warning(
                        "Direct Pi run key revocation failed; TTL remains active",
                        extra={"run_id": run_id},
                    )

    async def _run_p0(self, run_id: str, *, lease_token: str | None = None) -> None:
        run = await self.repository.get_run(run_id)
        if run.status in {
            RunStatus.cancelled,
            RunStatus.succeeded,
            RunStatus.failed,
            RunStatus.needs_attention,
            RunStatus.waiting_for_user,
        }:
            return
        try:
            active_lease = lease_token or await self.repository.get_active_lease_token(run_id)
        except RunLeaseLost:
            return

        continuation = await self.repository.get_run_continuation(run_id)
        if continuation is not None:
            await self.repository.append_event(
                run_id,
                "run.continuation_unavailable",
                payload={"requestId": continuation.request_id, "runtime": "p0"},
                lease_token=active_lease,
            )
            await self.repository.mark_terminal(
                run_id,
                RunStatus.needs_attention,
                error_code="p0_continuation_unsupported",
                summary=(
                    "This legacy execution mode cannot reconstruct the exact continuation; "
                    "the answer was not replayed in a replacement session."
                ),
                lease_token=active_lease,
            )
            return

        started_at = time.monotonic()
        generation: SandboxRef | None = None
        verification: SandboxRef | None = None
        keep_verification = False
        virtual_key: RunVirtualKey | None = None
        try:
            await assert_run_active(self.repository, run_id, active_lease)
            await self._phase(run_id, RunPhase.preparing, active_lease)
            requirement = await self.repository.get_run_prompt(run_id)
            starter = resolve_starter_manifest(("crud", "local-persistence"))
            run_input_id = await self.repository.store_artifact(
                run_id,
                "run_input",
                {
                    "title": "User request",
                    "requirement": requirement,
                    "starterId": starter.id,
                    "starterVersion": starter.version,
                    "starterCapabilities": list(starter.capability_ids),
                },
                lease_token=active_lease,
            )
            await assert_run_active(self.repository, run_id, active_lease)
            virtual_key = await self.gateway.issue(
                run_id=run_id,
                duration_seconds=self.settings.inference_token_ttl_seconds,
                max_budget=self.settings.run_max_spend,
                rpm_limit=self.settings.run_inference_rpm_limit,
                tpm_limit=self.settings.run_inference_tpm_limit,
            )
            commands = CommandExecutor(
                self.repository,
                self.sandbox,
                self.settings,
                run_id=run_id,
                lease_token=active_lease,
            )
            workspaces = WorkspaceManager(
                self.repository,
                self.sandbox,
                self.settings,
                commands,
                starter,
                run_id=run_id,
                project_id=run.project_id,
                lease_token=active_lease,
            )
            pi_session_id = await self.repository.ensure_pi_session_id(
                run_id,
                f"fomo-{run_id}",
                lease_token=active_lease,
            )
            pi = DirectPiSession(
                self.repository,
                self.transport,
                self.settings,
                virtual_key,
                run_id=run_id,
                lease_token=active_lease,
                started_at=started_at,
                session_id=pi_session_id,
            )
            verifier = Verifier(
                self.repository,
                self.sandbox,
                self.settings,
                commands,
                run_id=run_id,
                lease_token=active_lease,
                started_at=started_at,
            )

            await assert_run_active(self.repository, run_id, active_lease)
            generation = await workspaces.create_generation(run.base_version_id)
            before_planning = await workspaces.snapshot_hashes(generation)
            await assert_run_active(self.repository, run_id, active_lease)
            await self._phase(run_id, RunPhase.planning, active_lease)
            bundle: PlanningBundle | None = None
            starter_fingerprint = {
                "starterId": starter.id,
                "starterVersion": starter.version,
                "starterCapabilities": list(starter.capability_ids),
            }
            candidates = await self.repository.list_planning_cache_candidates(
                run.project_id,
                requirement,
                run.base_version_id,
                starter_fingerprint,
            )
            for candidate in candidates:
                try:
                    candidate_bundle = self._parse_planning_bundle(candidate["text"])
                except (DirectPiOrchestrationError, KeyError):
                    continue
                bundle = candidate_bundle
                await self.repository.append_event(
                    run_id,
                    "planning.cache_hit",
                    payload={"sourceRunId": candidate["runId"]},
                    lease_token=active_lease,
                )
                break

            if bundle is None:
                plan_text = await pi.invoke(
                    generation,
                    planning_prompt(
                        requirement=requirement,
                        starter=starter.as_architect_context(),
                    ),
                    stage="planning",
                    structured_output_schema=PlanningBundle.model_json_schema(
                        by_alias=True
                    ),
                    continuation_key="p0.planning",
                    continuation_context={"baselineHashes": before_planning},
                )
                await workspaces.assert_unchanged(generation, before_planning)
                await assert_run_active(self.repository, run_id, active_lease)
                try:
                    bundle = self._parse_planning_bundle(plan_text)
                except DirectPiOrchestrationError as exc:
                    await self.repository.append_event(
                        run_id,
                        "pi.retrying",
                        payload={
                            "stage": "planning",
                            "reason": "contract_validation",
                            "thinkingLevel": "high",
                        },
                        lease_token=active_lease,
                    )
                    corrected_plan_text = await pi.invoke(
                        generation,
                        planning_correction_prompt(validation_error=str(exc)),
                        stage="planning",
                        structured_output_schema=PlanningBundle.model_json_schema(
                            by_alias=True
                        ),
                        continuation_key="p0.planning_correction",
                        continuation_context={"baselineHashes": before_planning},
                    )
                    await workspaces.assert_unchanged(generation, before_planning)
                    await assert_run_active(self.repository, run_id, active_lease)
                    bundle = self._parse_planning_bundle(corrected_plan_text)
            else:
                await workspaces.assert_unchanged(generation, before_planning)
                await assert_run_active(self.repository, run_id, active_lease)

            build_plan_content = bundle.build_plan.model_dump(
                mode="json", by_alias=True
            )
            acceptance_content = bundle.acceptance_contract.model_dump(
                mode="json", by_alias=True
            )
            build_plan_id = await self.repository.store_artifact(
                run_id,
                "build_plan",
                build_plan_content,
                lease_token=active_lease,
            )
            acceptance_id = await self.repository.store_artifact(
                run_id,
                "acceptance_contract",
                acceptance_content,
                lease_token=active_lease,
            )
            await self.repository.append_trace_link(
                run_id,
                "artifact",
                run_input_id,
                "planned_by",
                "artifact",
                build_plan_id,
                lease_token=active_lease,
            )
            await self.repository.append_trace_link(
                run_id,
                "artifact",
                build_plan_id,
                "verified_by",
                "artifact",
                acceptance_id,
                lease_token=active_lease,
            )
            await self.repository.upsert_acceptance_items(
                run.project_id,
                run_id,
                [
                    item.model_dump(mode="json", by_alias=True)
                    for item in bundle.acceptance_contract.criteria
                ],
                lease_token=active_lease,
            )
            compiled = compile_acceptance(bundle.acceptance_contract)
            await self._persist_plan_trace(
                run_id, bundle, compiled, active_lease
            )

            await self._phase(run_id, RunPhase.building, active_lease)
            planning_payload = bundle.model_dump(mode="json", by_alias=True)
            await self.repository.append_event(
                run_id,
                "build.turn.started",
                payload={"stage": "building", "advisoryPlan": True},
                lease_token=active_lease,
            )
            handoff = await pi.invoke(
                generation,
                build_prompt(
                    requirement=requirement,
                    starter=starter.as_architect_context(),
                    planning_bundle=planning_payload,
                ),
                stage="building",
                continuation_key="p0.building",
                continuation_context={"baselineHashes": before_planning},
            )
            typecheck = await workspaces.typecheck_workspace(generation)
            repaired = False
            if typecheck.exit_code != 0 or typecheck.timed_out:
                repaired = True
                diagnostic = (typecheck.stdout + "\n" + typecheck.stderr).strip()
                handoff = await pi.invoke(
                    generation,
                    build_repair_prompt(diagnostic=diagnostic),
                    stage="repairing",
                    continuation_key="p0.typecheck_repair",
                    continuation_context={"baselineHashes": before_planning},
                )
                typecheck = await workspaces.typecheck_workspace(generation)
            if typecheck.exit_code != 0 or typecheck.timed_out:
                raise DirectPiOrchestrationError(
                    "candidate did not pass the direct typecheck"
                )
            checkpoint = await workspaces.checkpoint_workspace(generation)
            await assert_run_active(self.repository, run_id, active_lease)
            await self.repository.store_artifact(
                run_id,
                "build_handoff",
                {
                    "stage": "building",
                    "checkpointSha": checkpoint,
                    "repaired": repaired,
                    "handoff": handoff[:2_000],
                },
                lease_token=active_lease,
            )
            await self.repository.append_event(
                run_id,
                "build.turn.completed",
                payload={
                    "checkpointSha": checkpoint,
                    "repaired": repaired,
                },
                lease_token=active_lease,
            )
            audited = await workspaces.audit(
                generation,
                baseline=before_planning,
            )
            await assert_run_active(self.repository, run_id, active_lease)
            await self._persist_actual_diff(run_id, audited, bundle, active_lease)

            round_number = 0
            while True:
                await self._phase(run_id, RunPhase.verifying, active_lease)
                await assert_run_active(self.repository, run_id, active_lease)
                snapshot = await workspaces.create_verification(
                    audited, compiled, base_version_id=run.base_version_id
                )
                verification = snapshot.ref
                await assert_run_active(self.repository, run_id, active_lease)
                outcome = await verifier.verify(
                    verification,
                    bundle.acceptance_contract,
                    compiled,
                    round_number=round_number,
                    candidate_paths=audited.changed_paths,
                )
                await assert_run_active(self.repository, run_id, active_lease)
                if outcome.passed:
                    await self._publish(
                        run_id,
                        run.project_id,
                        active_lease,
                        workspaces,
                        verifier,
                        snapshot,
                        outcome,
                        bundle,
                    )
                    keep_verification = True
                    return
                if outcome.has_infrastructure_failure:
                    await self._discard_workspace(
                        run_id, verification, active_lease
                    )
                    verification = None
                    await self.repository.set_preview_url(
                        run_id, None, lease_token=active_lease
                    )
                    await self.repository.mark_terminal(
                        run_id,
                        RunStatus.needs_attention,
                        error_code="direct_pi_infrastructure_failed",
                        summary=(
                            "Deterministic verification infrastructure failed before "
                            "a trustworthy source repair could be attempted."
                        ),
                        lease_token=active_lease,
                    )
                    return
                if round_number >= self.settings.max_repair_rounds:
                    keep_verification = outcome.preview_url is not None
                    if not keep_verification:
                        await self._discard_workspace(
                            run_id, verification, active_lease
                        )
                        verification = None
                    await self.repository.mark_terminal(
                        run_id,
                        RunStatus.needs_attention,
                        error_code="direct_pi_verification_failed",
                        summary=(
                            "The preview is available but deterministic verification still has blockers."
                            if keep_verification
                            else "Deterministic verification did not produce a browser-reachable preview."
                        ),
                        lease_token=active_lease,
                    )
                    return

                await self._retire_verification(
                    run_id, verification, generation, active_lease
                )
                verification = None
                round_number = await self.repository.increment_repair_round(
                    run_id,
                    phase=RunPhase.repairing,
                    lease_token=active_lease,
                )
                await pi.invoke(
                    generation,
                    repair_prompt(
                        planning_bundle=bundle.model_dump(mode="json", by_alias=True),
                        diagnostic=outcome.as_repair_context(),
                        round_number=round_number,
                    ),
                    stage="repairing",
                    continuation_key="p0.verification_repair",
                    continuation_context={"baselineHashes": before_planning},
                )
                audited = await workspaces.audit(
                    generation,
                    baseline=before_planning,
                )
                await assert_run_active(self.repository, run_id, active_lease)
                await self._persist_actual_diff(
                    run_id, audited, bundle, active_lease
                )
        except DirectPiAwaitingUser:
            # The exact legacy sandbox/session is retained, but P0 deliberately
            # refuses to fake a resume after the answer; see the early guard.
            generation = None
            verification = None
            return
        except DirectPiRunCancelled:
            with suppress(RunLeaseLost):
                await self.repository.set_preview_url(
                    run_id, None, lease_token=active_lease
                )
            await self._discard_workspace(run_id, verification, active_lease)
            await self._discard_workspace(run_id, generation, active_lease)
            generation = None
            verification = None
            with suppress(RunLeaseLost):
                await self.repository.mark_terminal(
                    run_id,
                    RunStatus.cancelled,
                    summary="Cancelled safely by request.",
                    lease_token=active_lease,
                )
        except RunLeaseLost:
            await self._destroy(generation)
            await self._destroy(verification)
            generation = None
            verification = None
            if await self.repository.is_cancel_requested(run_id):
                with suppress(RunLeaseLost):
                    await self.repository.set_preview_url(
                        run_id, None, lease_token=active_lease
                    )
                    await self.repository.mark_terminal(
                        run_id,
                        RunStatus.cancelled,
                        summary="Cancelled safely by request.",
                        lease_token=active_lease,
                    )
        except asyncio.CancelledError:
            await self._destroy(generation)
            if not keep_verification:
                await self._destroy(verification)
            raise
        except Exception as exc:
            logger.error("Direct Pi run failed", extra={"run_id": run_id})
            try:
                await self.repository.set_preview_url(
                    run_id, None, lease_token=active_lease
                )
                await self._discard_workspace(run_id, verification, active_lease)
                await self._discard_workspace(run_id, generation, active_lease)
                generation = None
                verification = None
                await self.repository.append_event(
                    run_id,
                    "pi.failed",
                    payload={"errorType": type(exc).__name__},
                    lease_token=active_lease,
                )
                await self.repository.mark_terminal(
                    run_id,
                    RunStatus.failed,
                    error_code="direct_pi_execution_error",
                    summary="The Direct Pi run stopped before a verified version was created.",
                    lease_token=active_lease,
                )
            except RunLeaseLost:
                pass
            raise
        finally:
            if generation is not None:
                await self._destroy(generation)
            if verification is not None and not keep_verification:
                await self._destroy(verification)
            if virtual_key is not None:
                try:
                    await self.gateway.block(virtual_key)
                except Exception:
                    logger.warning(
                        "Direct Pi run key revocation failed; TTL remains active",
                        extra={"run_id": run_id},
                    )

    @staticmethod
    def _continuation_prompt(continuation: RunContinuation) -> str:
        if not continuation.answer:
            raise DirectPiContinuationUnavailable(
                "the continuation answer is unavailable"
            )
        planning_instruction = (
            " Complete the required submit_structured_output form when planning is now resolved."
            if continuation.stage == "planning"
            else " Continue implementation with the native coding tools and finish the prior turn."
        )
        return (
            "USER CLARIFICATION CONTINUATION. The prior request_user_input turn ended "
            "cleanly and the user has now answered. Continue the exact prior task in "
            "this same Pi session and existing workspace.\n"
            f"Requested question: {json.dumps(continuation.question, ensure_ascii=False)}\n"
            f"User answer: {json.dumps(continuation.answer, ensure_ascii=False)}\n"
            "Apply the answer as authoritative task context. Do not repeat the same question."
            + planning_instruction
        )

    @staticmethod
    def _continuation_hashes(
        context: dict[str, object], key: str
    ) -> dict[str, str]:
        value = context.get(key)
        if not isinstance(value, dict) or not value or len(value) > 10_000:
            raise DirectPiContinuationUnavailable(
                f"the continuation {key} manifest is unavailable"
            )
        hashes: dict[str, str] = {}
        for path, digest in value.items():
            if (
                not isinstance(path, str)
                or not path
                or len(path) > 1_024
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or path in hashes
            ):
                raise DirectPiContinuationUnavailable(
                    f"the continuation {key} manifest is invalid"
                )
            hashes[path] = digest
        return hashes

    @staticmethod
    def _checkpoint_hashes(checkpoint: CandidateCheckpoint) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in checkpoint.files:
            path = item.get("path")
            digest = item.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str) or path in result:
                raise DirectPiOrchestrationError(
                    "candidate checkpoint cannot form a continuation cursor"
                )
            result[path] = digest
        return result

    @classmethod
    def _continuation_checkpoint(
        cls, context: dict[str, object]
    ) -> CandidateCheckpoint:
        hashes = cls._continuation_hashes(context, "goalStartHashes")
        return CandidateCheckpoint(
            files=tuple(
                {"path": path, "sha256": digest}
                for path, digest in sorted(hashes.items())
            ),
            manifest_hash="continuation-cursor",
        )

    @staticmethod
    def _durable_started_at(run: object, *, recovered: bool) -> float:
        value = getattr(run, "execution_started_at", None)
        if value is None and recovered:
            value = getattr(run, "created_at", None)
        if not isinstance(value, datetime):
            return time.monotonic()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        elapsed = max(0.0, (utcnow() - value.astimezone(UTC)).total_seconds())
        return time.monotonic() - elapsed

    async def _remaining_spend_budget(self, run_id: str) -> float:
        getter = getattr(self.repository, "get_usage_totals", None)
        spent = 0.0
        if callable(getter):
            totals = await getter(run_id)
            cost_micros = getattr(totals, "cost_micros", 0)
            if isinstance(cost_micros, int) and cost_micros >= 0:
                spent = cost_micros / 1_000_000
        remaining = self.settings.run_max_spend - spent
        if remaining <= 0:
            raise DirectPiOrchestrationError("Direct Pi exhausted its durable spend budget")
        return remaining

    @staticmethod
    def _current_goal(projection: GoalGraphProjection | None):
        if projection is None:
            return None
        current = [
            goal
            for goal in projection.graph.goals
            if goal.status in {GoalStatus.ACTIVE, GoalStatus.CLAIMED}
        ]
        if len(current) > 1:
            raise DirectPiOrchestrationError(
                "GoalGraph projection contains multiple active/claimed goals"
            )
        return current[0] if current else None

    @staticmethod
    def _checkpoint_evidence_summaries(
        checkpoint: VerifiedCheckpoint | None,
    ) -> tuple[VerifiedGoalEvidence, ...]:
        if checkpoint is None:
            return ()
        values = checkpoint.capsule.get("verifiedEvidence")
        if not isinstance(values, list):
            raise DirectPiOrchestrationError(
                "verified checkpoint is missing bounded evidence summaries"
            )
        summaries: list[VerifiedGoalEvidence] = []
        try:
            for value in values:
                if not isinstance(value, dict):
                    raise ValueError("summary must be an object")
                summaries.append(
                    VerifiedGoalEvidence(
                        goal_id=str(value["goalId"]),
                        passed_acceptance_ids=tuple(value["passedAcceptanceIds"]),
                        evidence_refs=tuple(value.get("evidenceRefs", ())),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise DirectPiOrchestrationError(
                "verified checkpoint evidence summary is invalid"
            ) from exc
        return tuple(summaries)

    @staticmethod
    def _checkpoint_goal_changed_paths(
        checkpoint: VerifiedCheckpoint | None,
    ) -> tuple[dict[str, tuple[str, ...]], bool]:
        """Load per-goal deltas and explicitly identify legacy unknown history."""

        if checkpoint is None:
            return {}, False
        if "goalChangedPathsByGoal" not in checkpoint.capsule:
            return {}, True
        values = checkpoint.capsule["goalChangedPathsByGoal"]
        if not isinstance(values, dict) or len(values) > 6:
            raise DirectPiOrchestrationError(
                "verified checkpoint goal change summary is invalid"
            )
        result: dict[str, tuple[str, ...]] = {}
        for goal_id, paths in values.items():
            if (
                not isinstance(goal_id, str)
                or not isinstance(paths, list)
                or len(paths) > 24
                or any(not isinstance(path, str) or not path for path in paths)
                or len(paths) != len(set(paths))
            ):
                raise DirectPiOrchestrationError(
                    "verified checkpoint goal change summary is invalid"
                )
            result[goal_id] = tuple(sorted(paths))
        return result, False

    @staticmethod
    def _candidate_delta_paths(
        before: CandidateCheckpoint,
        after: CandidateCheckpoint,
    ) -> tuple[str, ...]:
        """Return only paths changed by the current Goal boundary."""

        def hashes(checkpoint: CandidateCheckpoint) -> dict[str, str]:
            result: dict[str, str] = {}
            for item in checkpoint.files:
                path = item.get("path")
                digest = item.get("sha256")
                if (
                    not isinstance(path, str)
                    or not isinstance(digest, str)
                    or path in result
                ):
                    raise DirectPiOrchestrationError(
                        "candidate checkpoint delta input is invalid"
                    )
                result[path] = digest
            return result

        prior = hashes(before)
        current = hashes(after)
        return tuple(
            sorted(
                path
                for path in prior.keys() | current.keys()
                if prior.get(path) != current.get(path)
            )
        )

    @staticmethod
    def _checkpoint_capsule(
        summaries: tuple[VerifiedGoalEvidence, ...],
        goal_changed_paths_by_id: dict[str, tuple[str, ...]],
    ) -> dict[str, object]:
        return {
            "verifiedEvidence": [
                {
                    "goalId": item.goal_id,
                    "passedAcceptanceIds": list(item.passed_acceptance_ids),
                    "evidenceRefs": list(item.evidence_refs),
                }
                for item in summaries
            ],
            "goalChangedPathsByGoal": {
                goal_id: list(paths)
                for goal_id, paths in sorted(goal_changed_paths_by_id.items())
            },
        }

    async def _persist_goal_diff(
        self,
        run_id: str,
        audited: AuditedWorkspace,
        goal_id: str,
        lease_token: str,
    ) -> None:
        deleted = {
            change.path for change in audited.model_changes if change.operation == "delete"
        }
        for path in audited.changed_paths:
            await self.repository.append_event(
                run_id,
                "file.changed",
                payload={
                    "path": path,
                    "status": "deleted" if path in deleted else "modified",
                    "goalId": goal_id,
                },
                lease_token=lease_token,
            )

    async def _retire_goal_verification(
        self,
        workspaces: WorkspaceManager,
        run_id: str,
        verification: SandboxRef,
        generation: SandboxRef,
        lease_token: str,
        *,
        reason: str,
    ) -> None:
        await workspaces.destroy(verification)
        await self.repository.set_preview_url(run_id, None, lease_token=lease_token)
        await self.repository.set_sandbox_id(
            run_id, generation.id, lease_token=lease_token
        )
        await self.repository.append_event(
            run_id,
            "preview.expired",
            payload={"reason": reason},
            lease_token=lease_token,
        )

    @staticmethod
    async def _discard_goal_workspace(
        workspaces: WorkspaceManager,
        ref: SandboxRef | None,
    ) -> None:
        if ref is None:
            return
        with suppress(Exception):
            await workspaces.destroy(ref)

    async def _verify_final_checkpoint(
        self,
        *,
        run: object,
        projection: GoalGraphProjection,
        workspaces: WorkspaceManager,
        verifier: Verifier,
        generation: SandboxRef,
        baseline: dict[str, str],
        lease_token: str,
    ) -> tuple[VerificationSnapshot, VerificationOutcome]:
        goals = tuple(projection.graph.goals)
        if not goals or any(goal.status is not GoalStatus.VERIFIED for goal in goals):
            raise DirectPiOrchestrationError(
                "final checkpoint verification requires an all-verified GoalGraph"
            )
        suite = RegressionSuite(
            claimed_goal_id=goals[-1].goal_id,
            goal_ids=tuple(goal.goal_id for goal in goals),
            contracts=tuple(scope_acceptance_contract(goal) for goal in goals),
            mode=RuntimeValidationMode.FULL,
            reason=RuntimeValidationReason.VERIFIED_GRAPH_RECOVERY,
        )
        compiled = compile_acceptance_suite(suite.contracts)
        await assert_run_active(
            self.repository, projection.run_id, lease_token
        )
        audited = await workspaces.audit(generation, baseline=baseline)
        await assert_run_active(
            self.repository, projection.run_id, lease_token
        )
        await self._persist_goal_diff(
            projection.run_id,
            audited,
            goals[-1].goal_id,
            lease_token,
        )
        await self._phase(projection.run_id, RunPhase.verifying, lease_token)
        await assert_run_active(
            self.repository, projection.run_id, lease_token
        )
        snapshot = await workspaces.create_verification(
            audited,
            compiled,
            base_version_id=getattr(run, "base_version_id", None),
        )
        await assert_run_active(
            self.repository, projection.run_id, lease_token
        )
        outcome = await verifier.verify_regression(
            snapshot.ref,
            suite,
            compiled,
            round_number=0,
            candidate_paths=audited.changed_paths,
        )
        await assert_run_active(
            self.repository, projection.run_id, lease_token
        )
        return snapshot, outcome

    async def _assert_verification_stable(
        self,
        workspaces: WorkspaceManager,
        verifier: Verifier,
        snapshot: VerificationSnapshot,
        outcome: VerificationOutcome,
    ) -> None:
        """Recheck frozen V source and live preview before trusting evidence."""

        await workspaces.assert_unchanged(
            snapshot.ref,
            snapshot.initial_hashes,
            context=(
                "verification sandbox source files drifted from the frozen "
                "initial snapshot; refusing to trust verification"
            ),
        )
        if outcome.preview_url is None:
            raise DirectPiOrchestrationError(
                "no preview URL after verification; refusing to trust evidence"
            )
        if not await verifier.preview_is_healthy(outcome.preview_url):
            raise DirectPiOrchestrationError(
                "preview health recheck failed; refusing to trust verification"
            )

    async def _publish(
        self,
        run_id: str,
        project_id: str,
        lease_token: str,
        workspaces: WorkspaceManager,
        verifier: Verifier,
        snapshot: VerificationSnapshot,
        outcome: VerificationOutcome,
        bundle: PlanningBundle | None,
        *,
        goal_graph: GoalGraphProjection | None = None,
    ) -> None:
        verification = snapshot.ref
        await assert_run_active(self.repository, run_id, lease_token)
        # Repeat the pre-checkpoint stability gate at publication. The live V
        # may have drifted or exited after durable checkpoint advancement.
        await self._assert_verification_stable(
            workspaces,
            verifier,
            snapshot,
            outcome,
        )
        await assert_run_active(self.repository, run_id, lease_token)
        commit_sha = snapshot.commit_sha
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise DirectPiOrchestrationError("verification commit sha is invalid")
        if self.settings.sandbox_provider == "opensandbox":
            renew_preview = getattr(self.sandbox, "renew_preview", None)
            if not callable(renew_preview):
                raise DirectPiOrchestrationError(
                    "OpenSandbox provider cannot retain the verified preview"
                )
            try:
                expires_at = await renew_preview(
                    verification,
                    self.settings.verified_preview_lifetime_seconds,
                )
            except Exception as exc:
                # Do not interpolate the provider exception: SDK errors may
                # include request metadata that does not belong in user-facing
                # run summaries or events.
                logger.warning(
                    "verified preview renewal failed for run=%s sandbox=%s error_type=%s",
                    run_id,
                    verification.id,
                    type(exc).__name__,
                )
                raise DirectPiOrchestrationError(
                    "unable to retain the verified preview"
                ) from exc
            await self.repository.append_event(
                run_id,
                "preview.retention_extended",
                payload={
                    "sandboxId": verification.id,
                    "expiresAt": expires_at,
                    "lifetimeSeconds": self.settings.verified_preview_lifetime_seconds,
                },
                lease_token=lease_token,
            )
            await assert_run_active(self.repository, run_id, lease_token)
        number = await self.repository.next_version_number(project_id)
        commands = CommandExecutor(
            self.repository,
            self.sandbox,
            self.settings,
            run_id=run_id,
            lease_token=lease_token,
        )
        # The tag must point explicitly at the frozen commit; HEAD may have
        # moved while gates were running.
        tag = await commands.run(
            verification,
            f"git tag version/{number} {commit_sha}",
            label="Tag verified version",
            stage="ready",
            timeout_seconds=30,
        )
        if tag.exit_code != 0 or tag.timed_out:
            raise DirectPiOrchestrationError("unable to tag the verified version")
        await assert_run_active(self.repository, run_id, lease_token)
        if bundle is not None:
            trace_items = tuple(
                (item.id, None) for item in bundle.acceptance_contract.criteria
            )
            product_title = bundle.build_plan.title
        elif goal_graph is not None:
            trace_items = tuple(
                (f"{goal.goal_id}:{item.id}", goal.goal_id)
                for goal in goal_graph.graph.goals
                for item in goal.acceptance.criteria
            )
            product_title = goal_graph.graph.product_outcome
        else:
            raise DirectPiOrchestrationError(
                "publication requires a P0 bundle or GoalGraph"
            )
        # This is the sole durable publication/terminal write. The repository
        # atomically rechecks lease + cancellation, creates the version and
        # frozen files, advances project head, emits trace/preview/summary,
        # and marks the run succeeded. Cancellation therefore wins without a
        # partial version or a split terminal state.
        published_preview_url = (
            self.settings.published_preview_url(verification.id) or outcome.preview_url
        )
        await self.repository.finalize_verified_publish(
            run_id,
            commit_sha=commit_sha,
            files=snapshot.initial_files,
            product_title=product_title,
            acceptance_items=trace_items,
            preview_url=published_preview_url,
            preview_elapsed_seconds=outcome.preview_elapsed_seconds,
            lease_token=lease_token,
        )

    async def _persist_plan_trace(
        self,
        run_id: str,
        bundle: PlanningBundle,
        compiled: CompiledAcceptance,
        lease_token: str,
    ) -> None:
        for item in bundle.acceptance_contract.criteria:
            await self.repository.append_trace_link(
                run_id,
                "acceptance_criterion",
                item.id,
                "has_test",
                "file",
                compiled.test_path_by_acceptance_id[item.id],
                metadata={"testName": compiled.test_name_by_acceptance_id[item.id]},
                lease_token=lease_token,
            )

    async def _persist_actual_diff(
        self,
        run_id: str,
        audited: AuditedWorkspace,
        bundle: PlanningBundle,
        lease_token: str,
    ) -> None:
        changed = set(audited.changed_paths)
        deleted = {
            change.path for change in audited.model_changes if change.operation == "delete"
        }
        for path in audited.changed_paths:
            await self.repository.append_event(
                run_id,
                "file.changed",
                payload={
                    "path": path,
                    "status": "deleted" if path in deleted else "modified",
                },
                lease_token=lease_token,
            )
        for item in bundle.build_plan.files:
            if item.path not in changed:
                continue
            for acceptance_id in item.acceptance_ids:
                await self.repository.append_trace_link(
                    run_id,
                    "acceptance_criterion",
                    acceptance_id,
                    "implemented_in",
                    "file",
                    item.path,
                    lease_token=lease_token,
                )

    async def _retire_verification(
        self,
        run_id: str,
        verification: SandboxRef,
        generation: SandboxRef,
        lease_token: str,
    ) -> None:
        await self._destroy(verification)
        await self.repository.set_preview_url(run_id, None, lease_token=lease_token)
        await self.repository.set_sandbox_id(
            run_id, generation.id, lease_token=lease_token
        )
        await self.repository.append_event(
            run_id,
            "preview.expired",
            payload={"reason": "repairing"},
            lease_token=lease_token,
        )

    async def _phase(self, run_id: str, phase: RunPhase, lease_token: str) -> None:
        await self.repository.set_run_phase(
            run_id, phase, lease_token=lease_token
        )

    async def _destroy(self, ref: SandboxRef | None) -> None:
        if ref is None:
            return
        with suppress(Exception):
            await self.sandbox.kill(ref)

    async def _discard_workspace(
        self,
        run_id: str,
        ref: SandboxRef | None,
        lease_token: str,
    ) -> None:
        if ref is None:
            return
        try:
            await self.sandbox.kill(ref)
        except Exception:
            return
        with suppress(RunLeaseLost):
            await self.repository.clear_sandbox_id(
                run_id, ref.id, lease_token=lease_token
            )

    @staticmethod
    def _parse_planning_bundle(text: str) -> PlanningBundle:
        value = text.strip()
        if value.startswith("```json") and value.endswith("```"):
            value = value[7:-3].strip()
        elif value.startswith("```") and value.endswith("```"):
            value = value[3:-3].strip()
        try:
            decoder = json.JSONDecoder()
            payload, end = decoder.raw_decode(value)
            remainder = value[end:].strip()
            if remainder:
                # DeepSeek occasionally closes ``acceptanceContract`` before
                # ``tests`` and then balances the intended shape with one
                # final brace. This exact shape is unambiguous and contains no
                # ignored content, so normalize it before strict validation.
                if remainder != "}" or not isinstance(payload, dict):
                    raise ValueError("planning contract has trailing content")
                acceptance = payload.get("acceptanceContract")
                if (
                    set(payload) != {"buildPlan", "acceptanceContract", "tests"}
                    or not isinstance(acceptance, dict)
                    or set(acceptance) != {"criteria"}
                ):
                    raise ValueError("planning contract has an unsupported JSON shape")
                payload = {
                    "buildPlan": payload["buildPlan"],
                    "acceptanceContract": {
                        "criteria": acceptance["criteria"],
                        "tests": payload["tests"],
                    },
                }
            return PlanningBundle.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                details = "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                    for item in exc.errors()[:5]
                )
            elif isinstance(exc, json.JSONDecodeError):
                details = f"JSON syntax error at character {exc.pos}"
            else:
                details = str(exc) or type(exc).__name__
            raise DirectPiOrchestrationError(
                f"Direct Pi returned an invalid planning contract: {details}"
            ) from exc

    @staticmethod
    def _parse_goal_graph_draft(text: str) -> GoalGraphDraft:
        value = text.strip()
        if value.startswith("```json") and value.endswith("```"):
            value = value[7:-3].strip()
        elif value.startswith("```") and value.endswith("```"):
            value = value[3:-3].strip()
        try:
            decoder = json.JSONDecoder()
            payload, end = decoder.raw_decode(value)
            if value[end:].strip():
                raise ValueError("GoalGraphDraft has trailing content")
            return parse_goal_graph_draft(payload)
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                details = "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                    for item in exc.errors()[:5]
                )
            elif isinstance(exc, json.JSONDecodeError):
                details = f"JSON syntax error at character {exc.pos}"
            else:
                details = str(exc) or type(exc).__name__
            raise DirectPiOrchestrationError(
                f"Direct Pi returned an invalid GoalGraphDraft: {details}"
            ) from exc
