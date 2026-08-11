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

from fomo.agent_framework import (
    AgentTransportRegistry,
    resolve_run_agent_framework,
)
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
    compile_acceptance_suite,
    compile_goal_advisory_acceptance,
)
from .execution import (
    CommandExecutor,
    DirectPiRunCancelled,
    assert_run_active,
)
from .failures import (
    CODING_AGENT_FAILED,
    GOAL_VERIFICATION_INFRASTRUCTURE_FAILED,
    AgentNoEffect,
    DirectPiOrchestrationError,
    FailureCategory,
    FailureOutcome,
    FailureStage,
    PlanningContractError,
    SafeRunDiagnostic,
    classify_direct_pi_failure,
    safe_diagnostic_for_error,
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
    PRODUCT_DESIGN_POLICY,
    goal_build_prompt,
    goal_graph_planning_correction_prompt,
    goal_graph_planning_prompt,
    goal_repair_prompt,
)
from .session import (
    DirectPiAwaitingUser,
    DirectPiContinuationUnavailable,
    DirectPiSession,
    PiTransport,
)
from .settlement import TurnEffectPolicy, settle_workspace_turn
from .verification import VerificationOutcome, Verifier
from .workspace import (
    AuditedWorkspace,
    CandidateCheckpoint,
    VerificationSnapshot,
    WorkspaceContractError,
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
        model_aliases: tuple[str, ...] | None = None,
    ) -> RunVirtualKey: ...

    async def block(self, virtual_key: RunVirtualKey) -> None: ...


class DirectPiOrchestrator:
    """FOMO owns contracts and proof; Pi owns planning, code, and repairs."""

    def __init__(
        self,
        repository: Repository,
        sandbox: SandboxProvider,
        settings: Settings,
        gateway: RunKeyGateway,
        transport: PiTransport | AgentTransportRegistry[PiTransport],
    ) -> None:
        self.repository = repository
        self.sandbox = sandbox
        self.settings = settings
        self.gateway = gateway
        self.transports = (
            transport
            if isinstance(transport, AgentTransportRegistry)
            else AgentTransportRegistry.pi_only(transport)
        )

    async def run(self, run_id: str, *, lease_token: str | None = None) -> None:
        framework: str | None = None
        try:
            framework = await resolve_run_agent_framework(self.repository, run_id)
            transport = self.transports.require(framework)
        except ValueError:
            # A deployment allowlist may change while an older run is queued.
            # Fail that run explicitly instead of leaving it running until its
            # lease expires, and never silently switch to a different agent.
            try:
                active_lease = lease_token or await self.repository.get_active_lease_token(run_id)
                payload = CODING_AGENT_FAILED.event_payload()
                payload["reason"] = "agent_framework_unavailable"
                if framework is not None:
                    payload["framework"] = framework
                await self.repository.append_event(
                    run_id,
                    "coding_agent.failed",
                    payload=payload,
                    lease_token=active_lease,
                )
                await self.repository.mark_terminal(
                    run_id,
                    RunStatus.failed,
                    error_code=CODING_AGENT_FAILED.code,
                    summary=CODING_AGENT_FAILED.summary,
                    lease_token=active_lease,
                )
            except RunLeaseLost:
                pass
            return
        await self._run_goal_graph(
            run_id,
            transport=transport,
            agent_framework=framework,
            lease_token=lease_token,
        )

    async def _run_goal_graph(
        self,
        run_id: str,
        *,
        transport: PiTransport,
        agent_framework: str,
        lease_token: str | None = None,
    ) -> None:
        """Execute a frozen GoalGraph one server-selected goal at a time."""

        run = await self.repository.get_run(run_id)
        runtime_contract = await self.repository.get_run_runtime_contract(run_id)
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
                        "agentFramework": agent_framework,
                        "planningPolicy": GOAL_GRAPH_PLANNING_POLICY,
                        "productDesignPolicy": PRODUCT_DESIGN_POLICY,
                        **runtime_contract.cache_fingerprint(),
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
            # A recovery run never revives the dead Agent session or sandbox.
            # It may, however, seed a fresh generation sandbox from the source
            # run's integrity-checked verified checkpoint.
            recovery_checkpoint = (
                None
                if latest_checkpoint is not None
                else await self.repository.get_recovery_checkpoint(run_id)
            )
            workspace_checkpoint = latest_checkpoint or recovery_checkpoint
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
            elif workspace_checkpoint is not None:
                generation, baseline = await workspaces.create_generation_from_checkpoint(
                    workspace_checkpoint,
                    base_version_id=run.base_version_id,
                )
            else:
                generation = await workspaces.create_generation(run.base_version_id)
                baseline = await workspaces.snapshot_hashes(generation)
            await assert_run_active(self.repository, run_id, active_lease)

            evidence_summaries = self._checkpoint_evidence_summaries(latest_checkpoint)
            (
                goal_changed_paths_by_id,
                legacy_checkpoint_unknown_paths,
            ) = self._checkpoint_goal_changed_paths(latest_checkpoint)
            if projection is not None and projection.graph.status is GraphStatus.VERIFIED:
                if latest_checkpoint is None:
                    raise DirectPiOrchestrationError("verified GoalGraph has no durable checkpoint")
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
                    await self.repository.set_preview_url(run_id, None, lease_token=active_lease)
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
                    goal_graph=projection,
                )
                keep_verification = True
                return

            await assert_run_active(self.repository, run_id, active_lease)
            virtual_key = await self.gateway.issue(
                run_id=run_id,
                duration_seconds=self.settings.active_run_inference_token_ttl_seconds,
                max_budget=await self._remaining_spend_budget(
                    run_id,
                    runtime_contract.max_spend_micros,
                ),
                rpm_limit=self.settings.run_inference_rpm_limit,
                tpm_limit=runtime_contract.inference_tpm_limit,
                model_aliases=(runtime_contract.litellm_alias,),
            )

            pi_session_id = await self.repository.ensure_pi_session_id(
                run_id,
                f"fomo-{run_id}",
                lease_token=active_lease,
            )
            pi = DirectPiSession(
                self.repository,
                transport,
                self.settings,
                virtual_key,
                runtime_contract=runtime_contract,
                agent_framework=agent_framework,
                run_id=run_id,
                lease_token=active_lease,
                started_at=started_at,
                session_id=pi_session_id,
            )

            if projection is None:
                await self._phase(run_id, RunPhase.planning, active_lease)
                # Recovery may have restored a verified checkpoint after the
                # base snapshot was captured. Planning is read-only relative
                # to the actual workspace it receives, while ``baseline``
                # remains the publication/audit base for the candidate diff.
                before_planning = await workspaces.snapshot_hashes(generation)
                draft: GoalGraphDraft | None = None
                starter_fingerprint = {
                    "starterId": starter.id,
                    "starterVersion": starter.version,
                    "starterCapabilities": list(starter.capability_ids),
                    "goalGraph": True,
                    "planningPolicy": GOAL_GRAPH_PLANNING_POLICY,
                    "productDesignPolicy": PRODUCT_DESIGN_POLICY,
                    **runtime_contract.cache_fingerprint(),
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
                        structured_output_schema=GoalGraphDraft.model_json_schema(by_alias=True),
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
                        structured_output_schema=GoalGraphDraft.model_json_schema(by_alias=True),
                        continuation_key="goal_graph.planning",
                        continuation_context=continuation_context,
                    )
                    await workspaces.assert_unchanged(generation, before_planning)
                    await assert_run_active(self.repository, run_id, active_lease)
                    while True:
                        try:
                            draft = self._parse_goal_graph_draft(plan_text)
                            break
                        except DirectPiOrchestrationError as exc:
                            await self.repository.append_event(
                                run_id,
                                "coding_agent.retrying",
                                payload={
                                    "stage": "planning",
                                    "reason": "goal_graph_contract_validation",
                                    "thinkingLevel": runtime_contract.thinking,
                                },
                                lease_token=active_lease,
                            )
                            plan_text = await pi.invoke(
                                generation,
                                goal_graph_planning_correction_prompt(validation_error=str(exc)),
                                stage="planning",
                                structured_output_schema=GoalGraphDraft.model_json_schema(
                                    by_alias=True
                                ),
                                continuation_key="goal_graph.planning_correction",
                                continuation_context=continuation_context,
                            )
                            await workspaces.assert_unchanged(generation, before_planning)
                            await assert_run_active(self.repository, run_id, active_lease)
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

                advisory = compile_goal_advisory_acceptance(
                    current_goal_id,
                    execution_plan.active_goal.acceptance,
                )
                (
                    baseline,
                    advisory_self_check_command,
                ) = await workspaces.reconcile_generation_advisory(
                    generation,
                    advisory,
                    baseline=baseline,
                )
                turn_start_hashes = await workspaces.snapshot_hashes(generation)
                await assert_run_active(self.repository, run_id, active_lease)

                turn_continuation_context = {
                    "baselineHashes": baseline,
                    "goalStartHashes": self._checkpoint_hashes(goal_start_checkpoint),
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
                    await pi.invoke(
                        generation,
                        self._continuation_prompt(continuation),
                        stage=turn_stage,
                        goal_id=current_goal_id,
                        continuation_key=continuation.continuation_key,
                        continuation_context=turn_continuation_context,
                        resume_request_id=continuation.request_id,
                    )
                    continuation = None
                else:
                    await pi.invoke(
                        generation,
                        goal_build_prompt(
                            requirement=requirement,
                            starter=starter.as_architect_context(),
                            execution_plan=execution_plan,
                            advisory_self_check_command=advisory_self_check_command,
                        ),
                        stage="building",
                        goal_id=current_goal_id,
                        continuation_key="goal_graph.goal_build",
                        continuation_context=turn_continuation_context,
                    )
                await self.repository.append_event(
                    run_id,
                    "runtime.turn.transport_finished",
                    payload={
                        "goalId": current_goal_id,
                        "stage": turn_stage,
                        "framework": agent_framework,
                        "requestId": pi.last_turn_receipt.request_id,
                    },
                    lease_token=active_lease,
                )
                settlement_hashes = await workspaces.snapshot_hashes(generation)
                settlement_paths = self._hash_delta_paths(
                    turn_start_hashes,
                    settlement_hashes,
                )
                try:
                    settlement = settle_workspace_turn(
                        pi.last_turn_receipt,
                        changed_paths=settlement_paths,
                        effect_policy=TurnEffectPolicy.MUST_CHANGE,
                    )
                except AgentNoEffect as first_no_effect:
                    # One deterministic recovery turn is allowed. A second
                    # unchanged manifest is no progress and terminates with a
                    # precise runtime failure instead of verifying the starter.
                    total_repair_round = await self.repository.increment_repair_round(
                        run_id,
                        phase=RunPhase.repairing,
                        lease_token=active_lease,
                    )
                    await self.repository.append_event(
                        run_id,
                        "runtime.turn.repairing",
                        payload={
                            "goalId": current_goal_id,
                            "diagnostic": first_no_effect.diagnostic.event_payload(),
                        },
                        lease_token=active_lease,
                    )
                    await pi.invoke(
                        generation,
                        goal_repair_prompt(
                            execution_plan=execution_plan,
                            diagnostic={
                                "gate": "turn_settlement",
                                "code": "agent_no_effect",
                                "summary": (
                                    "The previous build turn produced no server-observed "
                                    "workspace change. Use the repository tools and implement "
                                    "the active goal before handing off."
                                ),
                                "suggestedActions": [
                                    "Inspect the current workspace with repository tools.",
                                    "Implement the active goal and persist the code changes.",
                                    "Run the focused self-check before handing off.",
                                ],
                            },
                            round_number=total_repair_round,
                            advisory_self_check_command=advisory_self_check_command,
                        ),
                        stage="repairing",
                        goal_id=current_goal_id,
                        continuation_key="goal_graph.settlement_repair",
                        continuation_context=turn_continuation_context,
                        require_existing_session=True,
                    )
                    await self.repository.append_event(
                        run_id,
                        "runtime.turn.transport_finished",
                        payload={
                            "goalId": current_goal_id,
                            "stage": "repairing",
                            "framework": agent_framework,
                            "requestId": pi.last_turn_receipt.request_id,
                        },
                        lease_token=active_lease,
                    )
                    repaired_hashes = await workspaces.snapshot_hashes(generation)
                    settlement_paths = self._hash_delta_paths(
                        settlement_hashes,
                        repaired_hashes,
                    )
                    settlement = settle_workspace_turn(
                        pi.last_turn_receipt,
                        changed_paths=settlement_paths,
                        effect_policy=TurnEffectPolicy.MUST_CHANGE,
                    )
                await self.repository.append_event(
                    run_id,
                    "build.turn.completed",
                    payload={
                        "goalId": current_goal_id,
                        "claimOnly": True,
                        "effectPolicy": settlement.effect_policy.value,
                        "changedFileCount": len(settlement.changed_paths),
                        "toolCalls": settlement.tool_calls,
                    },
                    lease_token=active_lease,
                )
                typecheck = await workspaces.typecheck_workspace(generation)
                while typecheck.exit_code != 0 or typecheck.timed_out:
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
                                "code": ("timeout" if typecheck.timed_out else "nonzero_exit"),
                                "summary": "The fixed direct TypeScript check failed.",
                            },
                            round_number=total_repair_round,
                            advisory_self_check_command=advisory_self_check_command,
                        ),
                        stage="repairing",
                        goal_id=current_goal_id,
                        continuation_key="goal_graph.typecheck_repair",
                        continuation_context=turn_continuation_context,
                    )
                    typecheck = await workspaces.typecheck_workspace(generation)
                projection = await self.repository.claim_goal(
                    run_id,
                    current_goal_id,
                    lease_token=active_lease,
                )
                goal_round = 0

                while True:
                    await assert_run_active(self.repository, run_id, active_lease)
                    while True:
                        try:
                            audited = await workspaces.audit(
                                generation,
                                baseline=baseline,
                            )
                            break
                        except WorkspaceContractError as exc:
                            diagnostic = exc.repair
                            if diagnostic is None:
                                raise
                            if diagnostic.restore_protected_files:
                                restored = await workspaces.restore_generation_protected_files(
                                    generation,
                                    advisory,
                                    baseline=baseline,
                                )
                                if restored:
                                    continue
                                raise

                            total_repair_round = await self.repository.increment_repair_round(
                                run_id,
                                phase=RunPhase.repairing,
                                lease_token=active_lease,
                            )
                            await self.repository.append_event(
                                run_id,
                                "workspace.audit_repairing",
                                payload={
                                    "goalId": current_goal_id,
                                    "code": diagnostic.code.value,
                                    "affectedFileCount": len(diagnostic.affected_files),
                                },
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
                                    diagnostic=diagnostic.as_prompt_context(),
                                    round_number=total_repair_round,
                                    advisory_self_check_command=(advisory_self_check_command),
                                ),
                                stage="repairing",
                                goal_id=current_goal_id,
                                continuation_key="goal_graph.workspace_audit_repair",
                                continuation_context=turn_continuation_context,
                                require_existing_session=True,
                            )
                            projection = await self.repository.claim_goal(
                                run_id,
                                current_goal_id,
                                lease_token=active_lease,
                            )
                            await assert_run_active(
                                self.repository,
                                run_id,
                                active_lease,
                            )
                    await assert_run_active(self.repository, run_id, active_lease)
                    await self._persist_goal_diff(
                        run_id,
                        audited,
                        current_goal_id,
                        active_lease,
                    )
                    await assert_run_active(self.repository, run_id, active_lease)
                    candidate_checkpoint = await workspaces.capture_candidate_checkpoint(generation)
                    await assert_run_active(self.repository, run_id, active_lease)
                    goal_changed_paths = self._candidate_delta_paths(
                        goal_start_checkpoint,
                        candidate_checkpoint,
                    )
                    prior_goal_changed_paths = {
                        path for paths in goal_changed_paths_by_id.values() for path in paths
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
                            goal.goal_id == current_goal_id or goal.status is GoalStatus.VERIFIED
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
                            evidence_refs=(f"artifact:{outcome.diagnostic_artifact_id}",),
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
                    if outcome.has_infrastructure_failure:
                        infrastructure_diagnostic = self._verification_infrastructure_diagnostic(
                            outcome
                        )
                        await self.repository.append_event(
                            run_id,
                            "coding_agent.failed",
                            payload=GOAL_VERIFICATION_INFRASTRUCTURE_FAILED.event_payload(
                                goal_id=current_goal_id,
                                diagnostic=infrastructure_diagnostic,
                            ),
                            lease_token=active_lease,
                        )
                        await self._discard_goal_workspace(workspaces, verification)
                        verification = None
                        await self.repository.set_preview_url(
                            run_id, None, lease_token=active_lease
                        )
                        await self.repository.fail_goal(
                            run_id,
                            current_goal_id,
                            reason="verification infrastructure failed",
                            lease_token=active_lease,
                        )
                        await self.repository.mark_terminal(
                            run_id,
                            RunStatus.needs_attention,
                            error_code="goal_verification_infrastructure_failed",
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
                            advisory_self_check_command=advisory_self_check_command,
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
                        "requestId": (continuation.request_id if continuation is not None else None)
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
                await self.repository.set_preview_url(run_id, None, lease_token=active_lease)
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
                    await self.repository.set_preview_url(run_id, None, lease_token=active_lease)
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
            public_failure = classify_direct_pi_failure(exc)
            safe_diagnostic = safe_diagnostic_for_error(exc)
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
                await self.repository.set_preview_url(run_id, None, lease_token=active_lease)
                if workspaces is not None:
                    await self._discard_goal_workspace(workspaces, verification)
                    await self._discard_goal_workspace(workspaces, generation)
                verification = None
                generation = None
                await self.repository.append_event(
                    run_id,
                    "coding_agent.failed",
                    payload=public_failure.event_payload(
                        goal_id=current_goal_id,
                        diagnostic=safe_diagnostic,
                    ),
                    lease_token=active_lease,
                )
                await self.repository.mark_terminal(
                    run_id,
                    RunStatus.failed,
                    error_code=public_failure.code,
                    summary=public_failure.summary,
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

    @staticmethod
    def _continuation_prompt(continuation: RunContinuation) -> str:
        if not continuation.answer:
            raise DirectPiContinuationUnavailable("the continuation answer is unavailable")
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
    def _continuation_hashes(context: dict[str, object], key: str) -> dict[str, str]:
        value = context.get(key)
        if not isinstance(value, dict) or not value or len(value) > 10_000:
            raise DirectPiContinuationUnavailable(f"the continuation {key} manifest is unavailable")
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
                raise DirectPiContinuationUnavailable(f"the continuation {key} manifest is invalid")
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
    def _continuation_checkpoint(cls, context: dict[str, object]) -> CandidateCheckpoint:
        hashes = cls._continuation_hashes(context, "goalStartHashes")
        return CandidateCheckpoint(
            files=tuple(
                {"path": path, "sha256": digest} for path, digest in sorted(hashes.items())
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

    async def _remaining_spend_budget(
        self, run_id: str, max_spend_micros: int | None = None
    ) -> float:
        getter = getattr(self.repository, "get_usage_totals", None)
        spent = 0.0
        if callable(getter):
            totals = await getter(run_id)
            cost_micros = getattr(totals, "cost_micros", 0)
            if isinstance(cost_micros, int) and cost_micros >= 0:
                spent = cost_micros / 1_000_000
        budget = (
            max_spend_micros / 1_000_000
            if max_spend_micros is not None
            else self.settings.run_max_spend
        )
        remaining = budget - spent
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
            raise DirectPiOrchestrationError("verified checkpoint goal change summary is invalid")
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
                if not isinstance(path, str) or not isinstance(digest, str) or path in result:
                    raise DirectPiOrchestrationError("candidate checkpoint delta input is invalid")
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
    def _hash_delta_paths(
        before: dict[str, str],
        after: dict[str, str],
    ) -> tuple[str, ...]:
        """Compare the provider manifest without reading candidate contents.

        Settlement must not pre-empt the workspace auditor: a model may have
        produced a secret path, binary file, or other repairable violation.
        Content validation remains the later authoritative audit gate.
        """

        return tuple(
            sorted(
                path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
            )
        )

    @staticmethod
    def _verification_infrastructure_diagnostic(
        outcome: VerificationOutcome,
    ) -> SafeRunDiagnostic:
        gate = next(
            item
            for item in outcome.gates
            if (
                item.scope == "project"
                and item.gate in {"runner", "restore"}
                and item.status.value == "failed"
            )
            or (
                item.scope == "project"
                and item.gate == "dependencies"
                and item.status.value == "failed"
                and item.timed_out
            )
            or (item.scope == "acceptance" and item.outcome == "infrastructure_failed")
        )
        if gate.scope == "acceptance":
            reason_code = "playwright_report_untrusted"
            frame = "Playwright did not produce one trustworthy acceptance result."
            check = "playwright_report"
        elif gate.gate == "dependencies":
            reason_code = "verification_dependencies_timeout"
            frame = "The fixed dependency preparation command timed out."
            check = "dependencies"
        elif gate.gate == "restore":
            reason_code = "verification_restore_failed"
            frame = "The immutable candidate could not be restored for verification."
            check = "restore"
        else:
            reason_code = "verification_runner_unavailable"
            frame = "The fixed verification runner could not start reliably."
            check = "runner"
        return SafeRunDiagnostic(
            stage=FailureStage.VERIFYING,
            component="verification_orchestrator",
            check=check,
            category=FailureCategory.INFRASTRUCTURE_FAILED,
            reason_code=reason_code,
            outcome=(FailureOutcome.TIMED_OUT if gate.timed_out else FailureOutcome.UNAVAILABLE),
            retryable=True,
            frames=(
                frame,
                "A recovery run can continue only from the latest verified checkpoint.",
            ),
            exit_code=gate.exit_code,
            timed_out=gate.timed_out,
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
                goal_id: list(paths) for goal_id, paths in sorted(goal_changed_paths_by_id.items())
            },
        }

    async def _persist_goal_diff(
        self,
        run_id: str,
        audited: AuditedWorkspace,
        goal_id: str,
        lease_token: str,
    ) -> None:
        deleted = {change.path for change in audited.model_changes if change.operation == "delete"}
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
        await self.repository.set_sandbox_id(run_id, generation.id, lease_token=lease_token)
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
        await assert_run_active(self.repository, projection.run_id, lease_token)
        audited = await workspaces.audit(generation, baseline=baseline)
        await assert_run_active(self.repository, projection.run_id, lease_token)
        await self._persist_goal_diff(
            projection.run_id,
            audited,
            goals[-1].goal_id,
            lease_token,
        )
        await self._phase(projection.run_id, RunPhase.verifying, lease_token)
        await assert_run_active(self.repository, projection.run_id, lease_token)
        snapshot = await workspaces.create_verification(
            audited,
            compiled,
            base_version_id=getattr(run, "base_version_id", None),
        )
        await assert_run_active(self.repository, projection.run_id, lease_token)
        outcome = await verifier.verify_regression(
            snapshot.ref,
            suite,
            compiled,
            round_number=0,
            candidate_paths=audited.changed_paths,
        )
        await assert_run_active(self.repository, projection.run_id, lease_token)
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
        *,
        goal_graph: GoalGraphProjection,
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
        renew_preview = getattr(self.sandbox, "renew_preview", None)
        if not callable(renew_preview):
            raise DirectPiOrchestrationError(
                "sandbox provider cannot retain the verified preview"
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
            raise DirectPiOrchestrationError("unable to retain the verified preview") from exc
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
        trace_items = tuple(
            (f"{goal.goal_id}:{item.id}", goal.goal_id)
            for goal in goal_graph.graph.goals
            for item in goal.acceptance.criteria
        )
        product_title = goal_graph.graph.product_outcome
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

    async def _phase(self, run_id: str, phase: RunPhase, lease_token: str) -> None:
        await self.repository.set_run_phase(run_id, phase, lease_token=lease_token)

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
            await self.repository.clear_sandbox_id(run_id, ref.id, lease_token=lease_token)

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
            raise PlanningContractError(
                f"Direct Pi returned an invalid GoalGraphDraft: {details}"
            ) from exc
