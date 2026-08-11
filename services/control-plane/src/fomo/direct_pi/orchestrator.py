"""Thin production orchestrator for one Direct Pi session and clean verification."""

from __future__ import annotations

import asyncio
import hashlib
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
from .architecture_profile import (
    ARCHITECTURE_PROFILE_ID,
    ARCHITECTURE_PROFILE_VERSION,
    ArchitectureProfile,
    derive_product_architecture_profile,
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
    PlanningViolationDiagnostic,
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
    GoalGraphPlanningEnvelope,
    GoalStatus,
    GraphStatus,
    derive_navigation_verification_suite,
    parse_goal_graph_draft,
    parse_goal_graph_planning_envelope,
    scope_acceptance_contract,
)
from .prompts import (
    GOAL_GRAPH_PLANNING_POLICY,
    PRODUCT_DESIGN_POLICY,
    goal_build_prompt,
    goal_graph_planning_correction_prompt,
    goal_graph_planning_prompt,
    goal_repair_prompt,
    validate_goal_graph_routing,
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

_MAX_GOAL_GRAPH_CORRECTIONS = 12


def _planning_failure_fingerprint(plan_text: str, error: Exception) -> str:
    """Hash one invalid semantic submission without persisting its contents."""

    try:
        normalized_plan = json.dumps(
            json.loads(plan_text),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        normalized_plan = " ".join(plan_text.split())
    normalized_error = " ".join(str(error)[:4_000].split())
    return hashlib.sha256(
        f"{normalized_plan}\n{normalized_error}".encode()
    ).hexdigest()


def _planning_entity_ids(plan_text: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Extract only bounded domain identifiers; never return planner content."""

    try:
        value = json.loads(plan_text)
        if isinstance(value, dict) and isinstance(value.get("payloadJson"), str):
            value = json.loads(value["payloadJson"])
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return (), (), ()
    if not isinstance(value, dict):
        return (), (), ()
    routes = value.get("routes")
    goals = value.get("goals")
    route_ids: list[str] = []
    goal_ids: list[str] = []
    test_ids: list[str] = []
    if isinstance(routes, list):
        for route in routes[:32]:
            path = route.get("path") if isinstance(route, dict) else None
            if (
                isinstance(path, str)
                and len(path) <= 200
                and re.fullmatch(r"^/$|^/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$", path)
                and path not in route_ids
                and len(route_ids) < 8
            ):
                route_ids.append(path)
    if isinstance(goals, list):
        for goal in goals[:32]:
            if not isinstance(goal, dict):
                continue
            goal_id = goal.get("goalId")
            if (
                isinstance(goal_id, str)
                and re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", goal_id)
                and goal_id not in goal_ids
                and len(goal_ids) < 8
            ):
                goal_ids.append(goal_id)
            acceptance = goal.get("acceptance")
            tests = acceptance.get("tests") if isinstance(acceptance, dict) else None
            if not isinstance(tests, list):
                continue
            for test in tests[:16]:
                test_id = test.get("id") if isinstance(test, dict) else None
                if (
                    isinstance(test_id, str)
                    and re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", test_id)
                    and test_id not in test_ids
                    and len(test_ids) < 8
                ):
                    test_ids.append(test_id)
    return tuple(route_ids), tuple(goal_ids), tuple(test_ids)


def _planning_violation_diagnostic(
    plan_text: str,
    error: Exception,
    *,
    fingerprint: str,
    repeated_from: str | None = None,
    violation_code: str | None = None,
) -> PlanningViolationDiagnostic:
    routes, goals, tests = _planning_entity_ids(plan_text)
    code = violation_code or (
        error.violation_code
        if isinstance(error, PlanningContractError)
        else "planning_contract_invalid"
    )
    return PlanningViolationDiagnostic(
        violation_code=code,
        route_ids=routes,
        goal_ids=goals,
        test_ids=tests,
        fingerprint=fingerprint,
        repeated_from=repeated_from,
    )


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
        # Kept for narrow compatibility with code that inspects the original
        # Pi-only orchestrator. All turns use the immutable registry instead.
        self.transport = self.transports.transports.get("pi")

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
                active_lease = lease_token or await self.repository.get_active_lease_token(
                    run_id
                )
                payload = CODING_AGENT_FAILED.event_payload()
                payload["reason"] = "agent_framework_unavailable"
                if framework is not None:
                    payload["framework"] = framework
                await self.repository.append_event(
                    run_id,
                    "pi.failed",
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
                        "architectureProfilePolicy": (
                            f"{ARCHITECTURE_PROFILE_ID}@{ARCHITECTURE_PROFILE_VERSION}"
                        ),
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

            evidence_summaries = self._checkpoint_evidence_summaries(
                workspace_checkpoint
            )
            (
                goal_changed_paths_by_id,
                legacy_checkpoint_unknown_paths,
            ) = self._checkpoint_goal_changed_paths(workspace_checkpoint)
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
                    projection,
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

            architecture_profile: ArchitectureProfile | None = None
            if projection is None:
                await self._phase(run_id, RunPhase.planning, active_lease)
                # Recovery may have restored a verified checkpoint after the
                # base snapshot was captured. Planning is read-only relative
                # to the actual workspace it receives, while ``baseline``
                # remains the publication/audit base for the candidate diff.
                before_planning = await workspaces.snapshot_hashes(generation)
                draft: GoalGraphDraft | None = None
                plan_text: str | None = None
                starter_fingerprint = {
                    "starterId": starter.id,
                    "starterVersion": starter.version,
                    "starterCapabilities": list(starter.capability_ids),
                    "goalGraph": True,
                    "planningPolicy": GOAL_GRAPH_PLANNING_POLICY,
                    "productDesignPolicy": PRODUCT_DESIGN_POLICY,
                    "architectureProfilePolicy": (
                        f"{ARCHITECTURE_PROFILE_ID}@{ARCHITECTURE_PROFILE_VERSION}"
                    ),
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
                        structured_output_schema=GoalGraphPlanningEnvelope.model_json_schema(
                            by_alias=True
                        ),
                        continuation_key=continuation.continuation_key,
                        continuation_context=continuation.context,
                        resume_request_id=continuation.request_id,
                    )
                    await workspaces.assert_unchanged(generation, before_planning)
                    await assert_run_active(self.repository, run_id, active_lease)
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
                            draft = self._parse_goal_graph_draft(
                                candidate_text,
                                requirement=requirement,
                                allow_raw_domain=True,
                            )
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
                    if plan_text is None:
                        plan_text = await pi.invoke(
                            generation,
                            goal_graph_planning_prompt(
                                requirement=requirement,
                                starter=starter.as_build_context(),
                            ),
                            stage="planning",
                            structured_output_schema=(
                                GoalGraphPlanningEnvelope.model_json_schema(
                                    by_alias=True
                                )
                            ),
                            continuation_key="goal_graph.planning",
                            continuation_context=continuation_context,
                        )
                        await workspaces.assert_unchanged(
                            generation,
                            before_planning,
                        )
                        await assert_run_active(
                            self.repository,
                            run_id,
                            active_lease,
                        )
                    correction_attempts = 0
                    invalid_submission_fingerprints: set[str] = set()
                    while True:
                        try:
                            draft = self._parse_goal_graph_draft(
                                plan_text,
                                requirement=requirement,
                            )
                            break
                        except DirectPiOrchestrationError as exc:
                            failure_fingerprint = _planning_failure_fingerprint(
                                plan_text,
                                exc,
                            )
                            diagnostic = _planning_violation_diagnostic(
                                plan_text,
                                exc,
                                fingerprint=failure_fingerprint,
                            )
                            if failure_fingerprint in invalid_submission_fingerprints:
                                raise PlanningContractError(
                                    "GoalGraph planning repeated the same invalid semantic "
                                    "submission without progress",
                                    violation_code="planning_no_progress",
                                    planning_diagnostic=_planning_violation_diagnostic(
                                        plan_text,
                                        exc,
                                        fingerprint=failure_fingerprint,
                                        repeated_from=failure_fingerprint,
                                        violation_code="planning_no_progress",
                                    ),
                                ) from exc
                            invalid_submission_fingerprints.add(failure_fingerprint)
                            if correction_attempts >= _MAX_GOAL_GRAPH_CORRECTIONS:
                                raise PlanningContractError(
                                    "GoalGraph planning did not converge within the "
                                    f"server correction budget ({_MAX_GOAL_GRAPH_CORRECTIONS})",
                                    violation_code="planning_budget_exhausted",
                                    planning_diagnostic=_planning_violation_diagnostic(
                                        plan_text,
                                        exc,
                                        fingerprint=failure_fingerprint,
                                        violation_code="planning_budget_exhausted",
                                    ),
                                ) from exc
                            correction_attempts += 1
                            await self.repository.append_event(
                                run_id,
                                "pi.retrying",
                                payload={
                                    "stage": "planning",
                                    "reason": "goal_graph_contract_validation",
                                    "attempt": correction_attempts,
                                    "maxAttempts": _MAX_GOAL_GRAPH_CORRECTIONS,
                                    "thinkingLevel": runtime_contract.thinking,
                                    "diagnostic": diagnostic.event_payload(),
                                },
                                lease_token=active_lease,
                            )
                            plan_text = await pi.invoke(
                                generation,
                                goal_graph_planning_correction_prompt(
                                    validation_error=str(exc)
                                ),
                                stage="planning",
                                structured_output_schema=GoalGraphPlanningEnvelope.model_json_schema(
                                    by_alias=True
                                ),
                                continuation_key="goal_graph.planning_correction",
                                continuation_context=continuation_context,
                            )
                            await workspaces.assert_unchanged(generation, before_planning)
                            await assert_run_active(
                                self.repository, run_id, active_lease
                            )
                else:
                    await workspaces.assert_unchanged(generation, before_planning)
                    await assert_run_active(self.repository, run_id, active_lease)
                architecture_profile = derive_product_architecture_profile(
                    requirement=requirement,
                    route_count=max(1, len(draft.routes)),
                    goal_count=len(draft.goals),
                )
                projection = await self.repository.create_goal_graph(
                    run.project_id,
                    run_id,
                    draft,
                    architecture_profile=architecture_profile,
                    provenance={"createdBy": f"run:{run_id}", "planner": "direct_pi"},
                    lease_token=active_lease,
                )
                await self.repository.store_artifact(
                    run_id,
                    "goal_graph",
                    draft.model_dump(mode="json", by_alias=True),
                    lease_token=active_lease,
                )

            if architecture_profile is None:
                architecture_profile = projection.architecture_profile
            if architecture_profile is None:
                if projection.graph.schema_version == 1:
                    # Historical v1 graphs predate the frozen profile policy.
                    # Their compatibility path remains deterministic, while
                    # every current v2 graph must carry durable provenance.
                    architecture_profile = derive_product_architecture_profile(
                        requirement=requirement,
                        route_count=1,
                        goal_count=len(projection.graph.goals),
                    )
                else:
                    raise DirectPiOrchestrationError(
                        "GoalGraph is missing its frozen architecture profile"
                    )
            evidence_summaries = self._projection_evidence_summaries(
                projection,
                evidence_summaries,
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
                goal_round = 0
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
                    self._assert_continuation_architecture_profile(
                        continuation.context,
                        architecture_profile,
                    )
                    goal_start_checkpoint = self._continuation_checkpoint(
                        continuation.context,
                    )
                    logical_turn_start_hashes = self._continuation_hashes(
                        continuation.context,
                        "logicalTurnStartHashes",
                    )
                    goal_effect_policy = self._continuation_effect_policy(
                        continuation.context,
                        "goalEffectPolicy",
                    )
                    turn_effect_policy = self._continuation_effect_policy(
                        continuation.context,
                        "turnEffectPolicy",
                    )
                    turn_kind = self._continuation_turn_kind(continuation.context)
                    build_settled = self._continuation_boolean(
                        continuation.context,
                        "buildSettled",
                    )
                    goal_round = self._continuation_nonnegative_integer(
                        continuation.context,
                        "goalRound",
                    )
                    repair_candidate_start_checkpoint = self._continuation_optional_checkpoint(
                        continuation.context,
                        hashes_key="repairCandidateStartHashes",
                        manifest_key="repairCandidateStartManifestHash",
                    )
                    checkpoint_bound = (
                        workspace_checkpoint is not None
                        and goal_start_checkpoint.manifest_hash
                        == workspace_checkpoint.manifest_hash
                    )
                    if goal_effect_policy is TurnEffectPolicy.MAY_NOOP and (
                        not checkpoint_bound
                        or not any(
                            goal.status is GoalStatus.VERIFIED for goal in projection.graph.goals
                        )
                    ):
                        raise DirectPiContinuationUnavailable(
                            "the no-op continuation is not bound to the verified checkpoint"
                        )
                    if turn_kind == "build":
                        if continuation.stage != "building":
                            raise DirectPiContinuationUnavailable(
                                "the build continuation stage is invalid"
                            )
                    elif (
                        continuation.stage != "repairing"
                        or turn_effect_policy is not TurnEffectPolicy.MUST_CHANGE
                    ):
                        raise DirectPiContinuationUnavailable(
                            "the repair continuation effect contract is invalid"
                        )
                    # A question may be asked after arbitrary edits in the Pi
                    # turn. Resume conservatively with full regression breadth.
                    legacy_checkpoint_unknown_paths = True
                else:
                    goal_start_checkpoint = await workspaces.capture_candidate_checkpoint(
                        generation
                    )
                    checkpoint_bound = (
                        workspace_checkpoint is not None
                        and goal_start_checkpoint.manifest_hash
                        == workspace_checkpoint.manifest_hash
                    )
                    if workspace_checkpoint is not None and not checkpoint_bound:
                        # G is retained between goals and may have drifted after
                        # the last durable checkpoint. Never grant no-op authority
                        # to that state, and re-check every verified goal.
                        legacy_checkpoint_unknown_paths = True
                    goal_effect_policy = self._initial_goal_effect_policy(
                        projection,
                        fresh_build=True,
                        checkpoint_bound=checkpoint_bound,
                    )
                    turn_effect_policy = goal_effect_policy
                    turn_kind = "build"
                    build_settled = False
                    repair_candidate_start_checkpoint = None
                await assert_run_active(self.repository, run_id, active_lease)

                advisory = compile_goal_advisory_acceptance(
                    current_goal_id,
                    execution_plan.active_goal.acceptance,
                    navigation_suite=derive_navigation_verification_suite(
                        projection.graph,
                        goal_ids=(current_goal_id,),
                        mode="focused",
                    ),
                )
                baseline, advisory_self_check_command = (
                    await workspaces.reconcile_generation_advisory(
                        generation,
                        advisory,
                        baseline=baseline,
                    )
                )
                turn_start_hashes = await workspaces.snapshot_hashes(generation)
                await assert_run_active(self.repository, run_id, active_lease)

                if continuation is None:
                    logical_turn_start_hashes = turn_start_hashes
                turn_continuation_context = self._goal_turn_continuation_context(
                    baseline=baseline,
                    goal_start=goal_start_checkpoint,
                    logical_turn_start_hashes=logical_turn_start_hashes,
                    goal_effect_policy=goal_effect_policy,
                    turn_effect_policy=turn_effect_policy,
                    turn_kind=turn_kind,
                    build_settled=build_settled,
                    goal_round=goal_round,
                    repair_candidate_start=repair_candidate_start_checkpoint,
                    architecture_profile=architecture_profile,
                )

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
                        "architectureProfile": architecture_profile.as_prompt_context(),
                    },
                    lease_token=active_lease,
                )
                if continuation is not None:
                    await pi.invoke(
                        generation,
                        self._continuation_prompt(
                            continuation,
                            architecture_profile=architecture_profile,
                        ),
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
                            starter=starter.as_build_context(),
                            execution_plan=execution_plan,
                            advisory_self_check_command=advisory_self_check_command,
                            architecture_profile=architecture_profile,
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
                    logical_turn_start_hashes,
                    settlement_hashes,
                )
                try:
                    settle_workspace_turn(
                        pi.last_turn_receipt,
                        changed_paths=settlement_paths,
                        effect_policy=turn_effect_policy,
                    )
                except AgentNoEffect as first_no_effect:
                    if turn_kind != "build":
                        raise
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
                    logical_turn_start_hashes = settlement_hashes
                    turn_effect_policy = TurnEffectPolicy.MUST_CHANGE
                    turn_kind = "settlement_repair"
                    if repair_candidate_start_checkpoint is None:
                        with suppress(WorkspaceContractError):
                            repair_candidate_start_checkpoint = (
                                await workspaces.capture_candidate_checkpoint(generation)
                            )
                    turn_continuation_context = self._goal_turn_continuation_context(
                        baseline=baseline,
                        goal_start=goal_start_checkpoint,
                        logical_turn_start_hashes=logical_turn_start_hashes,
                        goal_effect_policy=goal_effect_policy,
                        turn_effect_policy=turn_effect_policy,
                        turn_kind=turn_kind,
                        build_settled=build_settled,
                        goal_round=goal_round,
                        repair_candidate_start=(repair_candidate_start_checkpoint),
                        architecture_profile=architecture_profile,
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
                            architecture_profile=architecture_profile,
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
                        logical_turn_start_hashes,
                        repaired_hashes,
                    )
                    settle_workspace_turn(
                        pi.last_turn_receipt,
                        changed_paths=settlement_paths,
                        effect_policy=turn_effect_policy,
                    )
                typecheck = await workspaces.typecheck_workspace(generation)
                while typecheck.exit_code != 0 or typecheck.timed_out:
                    total_repair_round = await self.repository.increment_repair_round(
                        run_id,
                        phase=RunPhase.repairing,
                        lease_token=active_lease,
                    )
                    logical_turn_start_hashes = await workspaces.snapshot_hashes(generation)
                    turn_effect_policy = TurnEffectPolicy.MUST_CHANGE
                    turn_kind = "typecheck_repair"
                    if repair_candidate_start_checkpoint is None:
                        with suppress(WorkspaceContractError):
                            repair_candidate_start_checkpoint = (
                                await workspaces.capture_candidate_checkpoint(generation)
                            )
                    turn_continuation_context = self._goal_turn_continuation_context(
                        baseline=baseline,
                        goal_start=goal_start_checkpoint,
                        logical_turn_start_hashes=logical_turn_start_hashes,
                        goal_effect_policy=goal_effect_policy,
                        turn_effect_policy=turn_effect_policy,
                        turn_kind=turn_kind,
                        build_settled=build_settled,
                        goal_round=goal_round,
                        repair_candidate_start=(repair_candidate_start_checkpoint),
                        architecture_profile=architecture_profile,
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
                            advisory_self_check_command=advisory_self_check_command,
                            architecture_profile=architecture_profile,
                        ),
                        stage="repairing",
                        goal_id=current_goal_id,
                        continuation_key="goal_graph.typecheck_repair",
                        continuation_context=turn_continuation_context,
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
                    typecheck_repair_hashes = await workspaces.snapshot_hashes(generation)
                    settle_workspace_turn(
                        pi.last_turn_receipt,
                        changed_paths=self._hash_delta_paths(
                            logical_turn_start_hashes,
                            typecheck_repair_hashes,
                        ),
                        effect_policy=turn_effect_policy,
                    )
                    typecheck = await workspaces.typecheck_workspace(generation)
                projection = await self.repository.claim_goal(
                    run_id,
                    current_goal_id,
                    lease_token=active_lease,
                )
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
                                restored = (
                                    await workspaces.restore_generation_protected_files(
                                        generation,
                                        advisory,
                                        baseline=baseline,
                                    )
                                )
                                if restored:
                                    continue
                                raise

                            total_repair_round = (
                                await self.repository.increment_repair_round(
                                    run_id,
                                    phase=RunPhase.repairing,
                                    lease_token=active_lease,
                                )
                            )
                            await self.repository.append_event(
                                run_id,
                                "workspace.audit_repairing",
                                payload={
                                    "goalId": current_goal_id,
                                    "code": diagnostic.code.value,
                                    "affectedFileCount": len(
                                        diagnostic.affected_files
                                    ),
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
                            logical_turn_start_hashes = await workspaces.snapshot_hashes(generation)
                            turn_effect_policy = TurnEffectPolicy.MUST_CHANGE
                            turn_kind = "workspace_audit_repair"
                            if repair_candidate_start_checkpoint is None:
                                with suppress(WorkspaceContractError):
                                    repair_candidate_start_checkpoint = (
                                        await workspaces.capture_candidate_checkpoint(generation)
                                    )
                            turn_continuation_context = self._goal_turn_continuation_context(
                                baseline=baseline,
                                goal_start=goal_start_checkpoint,
                                logical_turn_start_hashes=(logical_turn_start_hashes),
                                goal_effect_policy=goal_effect_policy,
                                turn_effect_policy=turn_effect_policy,
                                turn_kind=turn_kind,
                                build_settled=build_settled,
                                goal_round=goal_round,
                                repair_candidate_start=(repair_candidate_start_checkpoint),
                                architecture_profile=architecture_profile,
                            )
                            await pi.invoke(
                                generation,
                                goal_repair_prompt(
                                    execution_plan=repair_plan,
                                    diagnostic=diagnostic.as_prompt_context(),
                                    round_number=total_repair_round,
                                    advisory_self_check_command=(
                                        advisory_self_check_command
                                    ),
                                    architecture_profile=architecture_profile,
                                ),
                                stage="repairing",
                                goal_id=current_goal_id,
                                continuation_key="goal_graph.workspace_audit_repair",
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
                            audit_repair_hashes = await workspaces.snapshot_hashes(generation)
                            settle_workspace_turn(
                                pi.last_turn_receipt,
                                changed_paths=self._hash_delta_paths(
                                    logical_turn_start_hashes,
                                    audit_repair_hashes,
                                ),
                                effect_policy=turn_effect_policy,
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
                    if turn_kind != "build":
                        # A repair is admitted provisionally from its raw turn
                        # delta, then settled again only after the workspace
                        # auditor has restored every protected file. This keeps
                        # protected-only edits from satisfying MUST_CHANGE for a
                        # later Goal whose goal-level policy permits a no-op.
                        repaired_candidate_hashes = await workspaces.snapshot_hashes(
                            generation
                        )
                        settle_workspace_turn(
                            pi.last_turn_receipt,
                            changed_paths=self._hash_delta_paths(
                                logical_turn_start_hashes,
                                repaired_candidate_hashes,
                            ),
                            effect_policy=TurnEffectPolicy.MUST_CHANGE,
                        )
                    candidate_checkpoint = await workspaces.capture_candidate_checkpoint(
                        generation
                    )
                    await assert_run_active(self.repository, run_id, active_lease)
                    goal_changed_paths = self._candidate_delta_paths(
                        goal_start_checkpoint,
                        candidate_checkpoint,
                    )
                    candidate_paths = frozenset(self._checkpoint_hashes(candidate_checkpoint))
                    goal_settlement = settle_workspace_turn(
                        pi.last_turn_receipt,
                        changed_paths=goal_changed_paths,
                        effect_policy=goal_effect_policy,
                    )
                    if repair_candidate_start_checkpoint is not None:
                        settle_workspace_turn(
                            pi.last_turn_receipt,
                            changed_paths=self._candidate_delta_paths(
                                repair_candidate_start_checkpoint,
                                candidate_checkpoint,
                            ),
                            effect_policy=TurnEffectPolicy.MUST_CHANGE,
                        )
                        repair_candidate_start_checkpoint = None
                    if not build_settled:
                        await self.repository.append_event(
                            run_id,
                            "build.turn.completed",
                            payload={
                                "goalId": current_goal_id,
                                "claimOnly": True,
                                "effectPolicy": goal_settlement.effect_policy.value,
                                "changedFileCount": len(goal_settlement.changed_paths),
                                "toolCalls": goal_settlement.tool_calls,
                                "noOp": goal_settlement.no_op,
                            },
                            lease_token=active_lease,
                        )
                        build_settled = True
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
                    compiled = compile_acceptance_suite(
                        suite.contracts,
                        navigation_suite=suite.navigation_suite,
                    )
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
                        await self._persist_goal_diff(
                            run_id,
                            audited,
                            current_goal_id,
                            active_lease,
                            changed_paths=goal_changed_paths,
                            deleted_paths=tuple(
                                path
                                for path in goal_changed_paths
                                if path not in candidate_paths
                            ),
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
                        workspace_checkpoint = await self.repository.record_verified_checkpoint(
                            run_id,
                            current_goal_id,
                            candidate_checkpoint.files,
                            outcome.checkpoint_evidence(
                                current_goal_id,
                                navigation_suite=suite.navigation_suite,
                            ),
                            lease_token=active_lease,
                            commit_sha=snapshot.commit_sha,
                            capsule=self._checkpoint_capsule(
                                evidence_summaries,
                                goal_changed_paths_by_id,
                            ),
                            navigation_suite=suite.navigation_suite,
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
                                projection,
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
                        infrastructure_diagnostic = (
                            self._verification_infrastructure_diagnostic(outcome)
                        )
                        await self.repository.append_event(
                            run_id,
                            "pi.failed",
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
                    repair_candidate_start_checkpoint = candidate_checkpoint
                    logical_turn_start_hashes = await workspaces.snapshot_hashes(generation)
                    turn_effect_policy = TurnEffectPolicy.MUST_CHANGE
                    turn_kind = "verification_repair"
                    turn_continuation_context = self._goal_turn_continuation_context(
                        baseline=baseline,
                        goal_start=goal_start_checkpoint,
                        logical_turn_start_hashes=logical_turn_start_hashes,
                        goal_effect_policy=goal_effect_policy,
                        turn_effect_policy=turn_effect_policy,
                        turn_kind=turn_kind,
                        build_settled=build_settled,
                        goal_round=goal_round,
                        repair_candidate_start=(repair_candidate_start_checkpoint),
                        architecture_profile=architecture_profile,
                    )
                    await assert_run_active(self.repository, run_id, active_lease)
                    await pi.invoke(
                        generation,
                        goal_repair_prompt(
                            execution_plan=repair_plan,
                            diagnostic=outcome.as_repair_context(),
                            round_number=total_repair_round,
                            advisory_self_check_command=advisory_self_check_command,
                            architecture_profile=architecture_profile,
                        ),
                        stage="repairing",
                        goal_id=current_goal_id,
                        continuation_key="goal_graph.verification_repair",
                        continuation_context=turn_continuation_context,
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
                    repair_end_hashes = await workspaces.snapshot_hashes(generation)
                    settle_workspace_turn(
                        pi.last_turn_receipt,
                        changed_paths=self._hash_delta_paths(
                            logical_turn_start_hashes,
                            repair_end_hashes,
                        ),
                        effect_policy=turn_effect_policy,
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
                    payload={
                        **public_failure.event_payload(
                            goal_id=current_goal_id,
                            diagnostic=safe_diagnostic,
                        ),
                        **(
                            {
                                "planningDiagnostic": exc.planning_diagnostic.event_payload()
                            }
                            if isinstance(exc, PlanningContractError)
                            and exc.planning_diagnostic is not None
                            else {}
                        ),
                    },
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
    def _continuation_prompt(
        continuation: RunContinuation,
        *,
        architecture_profile: ArchitectureProfile | None = None,
    ) -> str:
        if not continuation.answer:
            raise DirectPiContinuationUnavailable(
                "the continuation answer is unavailable"
            )
        planning_instruction = (
            " Complete the required submit_structured_output form when planning is now resolved."
            if continuation.stage == "planning"
            else " Continue implementation with the native coding tools and finish the prior turn."
        )
        architecture_instruction = (
            "\nContinue to follow this exact frozen architecture profile:\n"
            + architecture_profile.render_brief()
            + "\n"
            + json.dumps(
                architecture_profile.as_prompt_context(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if architecture_profile is not None
            else ""
        )
        return (
            "USER CLARIFICATION CONTINUATION. The prior request_user_input turn ended "
            "cleanly and the user has now answered. Continue the exact prior task in "
            "this same Pi session and existing workspace.\n"
            f"Requested question: {json.dumps(continuation.question, ensure_ascii=False)}\n"
            f"User answer: {json.dumps(continuation.answer, ensure_ascii=False)}\n"
            "Apply the answer as authoritative task context. Do not repeat the same question."
            + planning_instruction
            + architecture_instruction
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
        cls,
        context: dict[str, object],
        *,
        hashes_key: str = "goalStartHashes",
        manifest_key: str = "goalStartManifestHash",
    ) -> CandidateCheckpoint:
        hashes = cls._continuation_hashes(context, hashes_key)
        manifest_hash = context.get(manifest_key)
        if (
            not isinstance(manifest_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_hash) is None
        ):
            raise DirectPiContinuationUnavailable(
                f"the continuation {manifest_key} is invalid"
            )
        return CandidateCheckpoint(
            files=tuple(
                {"path": path, "sha256": digest}
                for path, digest in sorted(hashes.items())
            ),
            manifest_hash=manifest_hash,
        )

    @classmethod
    def _continuation_optional_checkpoint(
        cls,
        context: dict[str, object],
        *,
        hashes_key: str,
        manifest_key: str,
    ) -> CandidateCheckpoint | None:
        if hashes_key not in context and manifest_key not in context:
            return None
        if hashes_key not in context or manifest_key not in context:
            raise DirectPiContinuationUnavailable(
                "the continuation repair checkpoint is incomplete"
            )
        return cls._continuation_checkpoint(
            context,
            hashes_key=hashes_key,
            manifest_key=manifest_key,
        )

    @staticmethod
    def _continuation_effect_policy(
        context: dict[str, object],
        key: str,
    ) -> TurnEffectPolicy:
        value = context.get(key)
        try:
            policy = TurnEffectPolicy(value)
        except (TypeError, ValueError) as exc:
            raise DirectPiContinuationUnavailable(
                f"the continuation {key} is invalid"
            ) from exc
        if policy is TurnEffectPolicy.VERIFY_ONLY:
            raise DirectPiContinuationUnavailable(
                f"the continuation {key} cannot be verify-only"
            )
        return policy

    @staticmethod
    def _continuation_turn_kind(context: dict[str, object]) -> str:
        value = context.get("turnKind")
        if value not in {
            "build",
            "settlement_repair",
            "typecheck_repair",
            "workspace_audit_repair",
            "verification_repair",
        }:
            raise DirectPiContinuationUnavailable("the continuation turn kind is invalid")
        return str(value)

    @staticmethod
    def _continuation_boolean(context: dict[str, object], key: str) -> bool:
        value = context.get(key)
        if type(value) is not bool:
            raise DirectPiContinuationUnavailable(f"the continuation {key} is invalid")
        return value

    @staticmethod
    def _continuation_nonnegative_integer(
        context: dict[str, object], key: str
    ) -> int:
        value = context.get(key)
        if type(value) is not int or value < 0:
            raise DirectPiContinuationUnavailable(f"the continuation {key} is invalid")
        return value

    @classmethod
    def _goal_turn_continuation_context(
        cls,
        *,
        baseline: dict[str, str],
        goal_start: CandidateCheckpoint,
        logical_turn_start_hashes: dict[str, str],
        goal_effect_policy: TurnEffectPolicy,
        turn_effect_policy: TurnEffectPolicy,
        turn_kind: str,
        build_settled: bool,
        goal_round: int,
        repair_candidate_start: CandidateCheckpoint | None,
        architecture_profile: ArchitectureProfile,
    ) -> dict[str, object]:
        context: dict[str, object] = {
            "baselineHashes": baseline,
            "goalStartHashes": cls._checkpoint_hashes(goal_start),
            "goalStartManifestHash": goal_start.manifest_hash,
            "logicalTurnStartHashes": logical_turn_start_hashes,
            "goalEffectPolicy": goal_effect_policy.value,
            "turnEffectPolicy": turn_effect_policy.value,
            "turnKind": turn_kind,
            "buildSettled": build_settled,
            "goalRound": goal_round,
            "architectureProfile": architecture_profile.as_prompt_context(),
            "architectureProfileHash": architecture_profile.fingerprint(),
        }
        if repair_candidate_start is not None:
            context["repairCandidateStartHashes"] = cls._checkpoint_hashes(
                repair_candidate_start
            )
            context["repairCandidateStartManifestHash"] = repair_candidate_start.manifest_hash
        return context

    @staticmethod
    def _assert_continuation_architecture_profile(
        context: dict[str, object],
        expected: ArchitectureProfile,
    ) -> None:
        raw_profile = context.get("architectureProfile")
        raw_hash = context.get("architectureProfileHash")
        if not isinstance(raw_profile, dict) or not isinstance(raw_hash, str):
            raise DirectPiContinuationUnavailable(
                "the continuation architecture profile is unavailable"
            )
        try:
            restored = ArchitectureProfile.from_prompt_context(raw_profile)
        except ValueError as exc:
            raise DirectPiContinuationUnavailable(
                "the continuation architecture profile is invalid"
            ) from exc
        if (
            restored.fingerprint() != raw_hash
            or raw_hash != expected.fingerprint()
        ):
            raise DirectPiContinuationUnavailable(
                "the continuation architecture profile changed"
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
    def _initial_goal_effect_policy(
        projection: GoalGraphProjection,
        *,
        fresh_build: bool,
        checkpoint_bound: bool,
    ) -> TurnEffectPolicy:
        """Allow a later goal to prove work already integrated by an earlier goal.

        The persisted GoalGraph is the authority: a model cannot opt itself into
        no-op verification.  A fresh graph still requires a real workspace delta,
        while any graph with verified work may submit an unchanged candidate for
        the next goal's independent acceptance suite.
        """

        if (
            fresh_build
            and checkpoint_bound
            and any(goal.status is GoalStatus.VERIFIED for goal in projection.graph.goals)
        ):
            return TurnEffectPolicy.MAY_NOOP
        return TurnEffectPolicy.MUST_CHANGE

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
    def _projection_evidence_summaries(
        projection: GoalGraphProjection,
        summaries: tuple[VerifiedGoalEvidence, ...],
    ) -> tuple[VerifiedGoalEvidence, ...]:
        """Bind checkpoint evidence only to verified goals in this graph.

        A cross-run recovery checkpoint may seed the workspace while the new run
        deliberately plans a fresh graph. Its capsule still owns path-history and
        legacy breadth, but source-run goal evidence cannot be transplanted onto
        unrelated pending goals in the new graph.
        """

        verified_goal_ids = {
            goal.goal_id
            for goal in projection.graph.goals
            if goal.status is GoalStatus.VERIFIED
        }
        selected = tuple(
            summary for summary in summaries if summary.goal_id in verified_goal_ids
        )
        if {summary.goal_id for summary in selected} != verified_goal_ids:
            raise DirectPiOrchestrationError(
                "verified checkpoint evidence does not cover the active GoalGraph"
            )
        return selected

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
                path
                for path in before.keys() | after.keys()
                if before.get(path) != after.get(path)
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
            or (
                item.scope in {"acceptance", "navigation"}
                and item.outcome == "infrastructure_failed"
            )
        )
        if gate.scope == "navigation":
            reason_code = "navigation_playwright_report_untrusted"
            frame = (
                f"Navigation test {gate.navigation_id} ({gate.test_name}) did not "
                "produce one trustworthy Playwright result."
            )[:240]
            check = "navigation_playwright_report"
        elif gate.scope == "acceptance":
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
            outcome=(
                FailureOutcome.TIMED_OUT
                if gate.timed_out
                else FailureOutcome.UNAVAILABLE
            ),
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
        *,
        changed_paths: tuple[str, ...] | None = None,
        deleted_paths: tuple[str, ...] | None = None,
    ) -> None:
        deleted = (
            {
                change.path
                for change in audited.model_changes
                if change.operation == "delete"
            }
            if deleted_paths is None
            else set(deleted_paths)
        )
        for path in audited.changed_paths if changed_paths is None else changed_paths:
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
            navigation_suite=derive_navigation_verification_suite(
                projection.graph,
                goal_ids=tuple(goal.goal_id for goal in goals),
                mode="final_full",
            ),
        )
        compiled = compile_acceptance_suite(
            suite.contracts,
            navigation_suite=suite.navigation_suite,
        )
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
        await self.repository.set_run_phase(
            run_id, phase, lease_token=lease_token
        )

    @staticmethod
    def _parse_goal_graph_draft(
        text: str,
        *,
        requirement: str,
        allow_raw_domain: bool = False,
    ) -> GoalGraphDraft:
        value = text.strip()

        def validation_details(exc: ValidationError) -> str:
            details: list[str] = []
            for item in exc.errors(include_input=False)[:5]:
                pointer = "/" + "/".join(str(part) for part in item["loc"])
                details.append(f"{pointer}: {str(item['msg'])[:500]}")
            return "; ".join(details)[:2_500]

        try:
            envelope = parse_goal_graph_planning_envelope(value)
        except ValidationError as exc:
            if not allow_raw_domain:
                raise PlanningContractError(
                    "Direct Pi returned an invalid planning envelope: "
                    + validation_details(exc),
                    violation_code="planning_envelope_invalid",
                ) from exc
            payload: object = value
        except (RecursionError, TypeError, ValueError) as exc:
            if not allow_raw_domain:
                raise PlanningContractError(
                    "Direct Pi returned malformed planning-envelope JSON.",
                    violation_code="planning_envelope_json_invalid",
                ) from exc
            payload = value
        else:
            payload = envelope.payload_json

        try:
            draft = parse_goal_graph_draft(payload)
        except ValidationError as exc:
            locations = [item["loc"] for item in exc.errors(include_input=False)[:20]]
            violation_code = "goal_graph_domain_invalid"
            if any(location and location[0] == "routes" for location in locations):
                violation_code = "route_manifest_invalid"
            elif any(location and location[0] == "goals" for location in locations):
                violation_code = "goal_contract_invalid"
            raise PlanningContractError(
                "Direct Pi returned an invalid GoalGraphDraft: "
                + validation_details(exc),
                violation_code=violation_code,
            ) from exc
        except (RecursionError, TypeError, ValueError) as exc:
            raise PlanningContractError(
                "Direct Pi returned malformed payloadJson; expected one strict JSON object.",
                violation_code="planning_payload_json_invalid",
            ) from exc

        try:
            return validate_goal_graph_routing(
                requirement,
                draft,
            )
        except ValueError as exc:
            raise PlanningContractError(
                "Direct Pi returned a GoalGraphDraft that conflicts with the request: "
                + str(exc)[:2_500],
                violation_code="route_intent_invalid",
            ) from exc
