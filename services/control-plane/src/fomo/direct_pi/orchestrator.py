"""Thin production orchestrator for one Direct Pi session and clean verification."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Protocol

from pydantic import ValidationError

from fomo.config import Settings
from fomo.fomo_pi_ds import RunVirtualKey
from fomo.persistence import Repository, RunLeaseLost
from fomo.sandbox.base import SandboxProvider, SandboxRef
from fomo.schemas import RunPhase, RunStatus
from fomo.starter import resolve_starter_manifest

from .acceptance import CompiledAcceptance, compile_acceptance
from .contracts import PlanningBundle, validate_plan_write_scope
from .execution import CommandExecutor
from .prompts import build_prompt, planning_prompt, repair_prompt
from .session import DirectPiCancelled, DirectPiSession, PiTransport
from .verification import VerificationOutcome, Verifier
from .workspace import AuditedWorkspace, WorkspaceManager

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
        run = await self.repository.get_run(run_id)
        if run.status in {
            RunStatus.cancelled,
            RunStatus.succeeded,
            RunStatus.failed,
            RunStatus.needs_attention,
        }:
            return
        try:
            active_lease = lease_token or await self.repository.get_active_lease_token(run_id)
        except RunLeaseLost:
            return

        started_at = time.monotonic()
        generation: SandboxRef | None = None
        verification: SandboxRef | None = None
        keep_verification = False
        virtual_key: RunVirtualKey | None = None
        try:
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
            pi = DirectPiSession(
                self.repository,
                self.transport,
                self.settings,
                virtual_key,
                run_id=run_id,
                lease_token=active_lease,
                started_at=started_at,
            )
            verifier = Verifier(
                self.repository,
                self.sandbox,
                self.settings,
                commands,
                run_id=run_id,
                lease_token=active_lease,
            )

            generation = await workspaces.create_generation(run.base_version_id)
            before_planning = await workspaces.snapshot_hashes(generation)
            await self._phase(run_id, RunPhase.planning, active_lease)
            plan_text = await pi.invoke(
                generation,
                planning_prompt(
                    requirement=requirement,
                    starter=starter.as_architect_context(),
                ),
                stage="planning",
            )
            await workspaces.assert_unchanged(generation, before_planning)
            bundle = self._parse_planning_bundle(plan_text)
            validate_plan_write_scope(
                bundle.build_plan, model_owned=starter.is_model_owned_path
            )
            if starter.root_extension_contract.path not in {
                item.path for item in bundle.build_plan.files
            }:
                raise DirectPiOrchestrationError("build plan omitted the starter extension contract")

            build_plan_id = await self.repository.store_artifact(
                run_id,
                "build_plan",
                bundle.build_plan.model_dump(mode="json", by_alias=True),
                lease_token=active_lease,
            )
            acceptance_id = await self.repository.store_artifact(
                run_id,
                "acceptance_contract",
                bundle.acceptance_contract.model_dump(mode="json", by_alias=True),
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
            await workspaces.freeze_acceptance(generation, compiled)

            await self._phase(run_id, RunPhase.building, active_lease)
            await pi.invoke(
                generation,
                build_prompt(
                    requirement=requirement,
                    starter=starter.as_architect_context(),
                    planning_bundle=bundle.model_dump(mode="json", by_alias=True),
                ),
                stage="building",
            )
            audited = await workspaces.audit(
                generation,
                compiled,
                planned_paths={item.path for item in bundle.build_plan.files},
            )
            await self._persist_actual_diff(run_id, audited, bundle, active_lease)

            round_number = 0
            while True:
                await self._phase(run_id, RunPhase.verifying, active_lease)
                verification, commit_sha = await workspaces.create_verification(
                    audited, compiled
                )
                outcome = await verifier.verify(
                    verification,
                    bundle.acceptance_contract,
                    compiled,
                    round_number=round_number,
                    candidate_paths=audited.changed_paths,
                )
                if outcome.passed:
                    await self._publish(
                        run_id,
                        run.project_id,
                        active_lease,
                        verification,
                        commit_sha,
                        outcome,
                        bundle,
                    )
                    keep_verification = True
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
                )
                audited = await workspaces.audit(
                    generation,
                    compiled,
                    planned_paths={item.path for item in bundle.build_plan.files},
                )
                await self._persist_actual_diff(
                    run_id, audited, bundle, active_lease
                )
        except DirectPiCancelled:
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

    async def _publish(
        self,
        run_id: str,
        project_id: str,
        lease_token: str,
        verification: SandboxRef,
        commit_sha: str,
        outcome: VerificationOutcome,
        bundle: PlanningBundle,
    ) -> None:
        list_files = getattr(self.sandbox, "list_files", None)
        if not callable(list_files):
            raise DirectPiOrchestrationError("sandbox provider cannot persist the verified source")
        files = list(await list_files(verification))
        number = await self.repository.next_version_number(project_id)
        commands = CommandExecutor(
            self.repository,
            self.sandbox,
            self.settings,
            run_id=run_id,
            lease_token=lease_token,
        )
        tag = await commands.run(
            verification,
            f"git tag version/{number}",
            label="Tag verified version",
            stage="ready",
            timeout_seconds=30,
        )
        if tag.exit_code != 0 or tag.timed_out:
            raise DirectPiOrchestrationError("unable to tag the verified version")
        version = await self.repository.create_version(
            run_id,
            commit_sha=commit_sha,
            qa_status="passed",
            files=files,
            lease_token=lease_token,
        )
        if version.number != number:
            raise DirectPiOrchestrationError("version number changed during publication")
        for item in bundle.acceptance_contract.criteria:
            await self.repository.append_trace_link(
                run_id,
                "acceptance_criterion",
                item.id,
                "verified_in",
                "version",
                version.id,
                lease_token=lease_token,
            )
        await self._phase(run_id, RunPhase.ready, lease_token)
        await self.repository.append_event(
            run_id,
            "assistant.summary",
            payload={
                "summary": (
                    f"{bundle.build_plan.title} is ready as version {version.number}; "
                    "the clean sandbox gates and frozen acceptance workflows passed."
                )
            },
            lease_token=lease_token,
        )
        await self.repository.mark_terminal(
            run_id,
            RunStatus.succeeded,
            summary=f"Version {version.number} passed deterministic verification.",
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
        for path in audited.changed_paths:
            await self.repository.append_event(
                run_id,
                "file.changed",
                payload={"path": path, "status": "modified"},
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
            payload = json.loads(value)
            return PlanningBundle.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            raise DirectPiOrchestrationError("Direct Pi returned an invalid planning contract") from exc
