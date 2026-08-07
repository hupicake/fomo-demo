"""Executable, durable four-role Product → Architect → Engineer → Reviewer SOP."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeVar

import httpx
from pydantic import ValidationError

from fomo.config import Settings
from fomo.ids import uuid7
from fomo.persistence import Repository, RunLeaseLost
from fomo.sandbox.base import (
    Command,
    FileChange,
    PreviewRef,
    SandboxProvider,
    SandboxRef,
    validate_workspace_path,
)
from fomo.schemas import (
    DiagnosticReport,
    FileBatchReport,
    GateResult,
    GateStatus,
    ImplementationBatchPlan,
    ImplementationPlan,
    ImplementationReport,
    ProductSpec,
    RunPhase,
    RunStatus,
    TechnicalSpec,
)
from fomo.starter import StarterIntegrityError, StarterManifest, default_starter_manifest

from .llm import ModelClient, ModelError, ModelRequestError, ModelRetry
from .metagpt_adapter import MetaGPTAdapter
from .state import FailureRouter, SOPStateMachine

Artifact = TypeVar(
    "Artifact",
    ProductSpec,
    TechnicalSpec,
    ImplementationPlan,
    FileBatchReport,
    ImplementationReport,
    DiagnosticReport,
)
Awaited = TypeVar("Awaited")

# This file is system-owned rather than model-owned. It makes the candidate
# commit and persisted source manifest independent of whether an Engineer
# remembered to author a .gitignore.
_SYSTEM_GITIGNORE = """# FOMO system safety baseline
node_modules/
.next/
dist/
build/
coverage/
playwright-report/
test-results/
blob-report/
*.log
.env
.env.*
"""
_SYSTEM_GITIGNORE_PATH = ".gitignore"
# This is intentionally a fixed command over a fixed system-owned path. Never
# interpolate generated or user file content into a sandbox shell command.
_SYSTEM_GITIGNORE_RESET_COMMAND = "chmod u+rw -- .gitignore 2>/dev/null || true; rm -f -- .gitignore"
_SYSTEM_MANAGED_FILE_PLAN_NAMES = frozenset(
    {
        ".gitignore",
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
    }
)
_MAX_REPAIR_SCOPE_FILES = 8
_STATE_AGGREGATION_RESPONSIBILITIES = frozenset({"compose", "persist", "re_export"})
_FEATURE_SURFACE_COMPOSITION_RESPONSIBILITIES = frozenset({"compose", "layout", "props"})
_DEFAULT_FRONTEND_STACK_CONTRACT = (
    "Use FOMO's default Next.js + React + TypeScript + Tailwind CSS + shadcn/ui + Lucide React "
    "frontend stack. Prefer existing components and mature shadcn/ui primitives with Lucide icons. "
    "CRUD uses Table + DropdownMenu, Sheet/Dialog editing, AlertDialog deletion, and Badge status; "
    "forms use Label + Input/Textarea/Select/Checkbox/Switch with validation and pending submission. "
    "When a shadcn primitive exists, do not use raw button/input/textarea/select or an unjustified custom "
    "Dialog/Menu/Select/Tooltip; every custom decision must document the missing capability in "
    "componentDecisions."
)
_PLAYWRIGHT_NETWORK_CONTRACT = (
    "Any Playwright webServer command must bind to 0.0.0.0; its baseURL and readiness probe must use "
    "http://127.0.0.1:<port>. Never use a container hostname, os.hostname(), process.env.HOSTNAME, "
    "or any other dynamic hostname."
)
_TECHNICAL_SPEC_REPAIR_INSTRUCTIONS = MappingProxyType(
    {
        "technical_spec.component_decisions.duplicate": "Use each componentDecisions.component value at most once.",
        "technical_spec.public_api_contracts.path_invalid": (
            "Set every publicApiContracts.filePath to a valid relative workspace file path."
        ),
        "technical_spec.public_api_contracts.file_unplanned": (
            "Reference a file that exists in TechnicalSpec.filePlan."
        ),
        "technical_spec.public_api_contracts.file_deleted": (
            "Remove public API contracts for filePlan entries scheduled for deletion."
        ),
        "technical_spec.public_api_contracts.symbol_duplicate": (
            "Use each public API filePath and symbol pair at most once."
        ),
        "technical_spec.file_plan.empty": "Provide at least one model-writable TechnicalSpec.filePlan entry.",
        "technical_spec.file_plan.path_invalid": (
            "Use only valid relative workspace paths in TechnicalSpec.filePlan."
        ),
        "technical_spec.file_plan.system_managed": "Remove system-managed files from TechnicalSpec.filePlan.",
        "technical_spec.file_plan.starter_protected": (
            "Remove immutable starter-owned paths from TechnicalSpec.filePlan."
        ),
        "technical_spec.file_plan.model_root": (
            "Plan files only inside the StarterManifest modelOwnedRoots."
        ),
        "technical_spec.file_plan.path_duplicate": "Use each TechnicalSpec.filePlan path at most once.",
        "technical_spec.file_plan.capacity_exceeded": (
            "Reduce TechnicalSpec.filePlan to the configured Engineer capacity."
        ),
        "technical_spec.dependencies.starter_only": (
            "Do not declare additional dependencies; use the immutable starter baseline or record a risk."
        ),
        "technical_spec.feature_surfaces.component_mapping_invalid": (
            "Give every ComponentSpec an explicit interactionResponsibilities list and declare exactly one "
            "featureSurfaces entry for each component with three or more concerns."
        ),
        "technical_spec.feature_surfaces.module_mapping_invalid": (
            "For each feature surface, map every declared interaction responsibility to exactly one module and "
            "use each module role at most once."
        ),
        "technical_spec.feature_surfaces.controller_missing": (
            "Give every feature surface exactly one additional controller module."
        ),
        "technical_spec.feature_surfaces.composition_responsibilities_invalid": (
            "Limit feature surface compositionResponsibilities to compose, layout, and props without duplicates."
        ),
        "technical_spec.feature_surfaces.file_invalid": (
            "Use valid relative workspace paths for feature surface composition and module files."
        ),
        "technical_spec.feature_surfaces.file_unplanned": (
            "Reference model-owned create or modify TechnicalSpec.filePlan files for every feature surface composition "
            "and module."
        ),
        "technical_spec.feature_surfaces.file_conflict": (
            "Use different model-owned create or modify TechnicalSpec.filePlan files for every feature surface "
            "composition and module."
        ),
        "technical_spec.feature_surfaces.public_api_unbound": (
            "Bind every feature surface composition and module filePath plus publicSymbol to publicApiContracts."
        ),
        "technical_spec.persistent_state_domains.mapping_invalid": (
            "For every independently mutable persistent business domain declared in stateModel.mutableDomains, "
            "provide exactly one matching persistentStateDomains entry with stateModelName, domain, and "
            "actionsStoreFile."
        ),
        "technical_spec.persistent_state_domains.mutable_domains_invalid": (
            "Give every persistent_business stateModel a nonempty unique mutableDomains list, and leave "
            "mutableDomains empty for transient or derived state."
        ),
        "technical_spec.persistent_state_domains.file_invalid": (
            "Use valid relative workspace paths for persistentStateDomains.actionsStoreFile."
        ),
        "technical_spec.persistent_state_domains.file_unplanned": (
            "Reference model-owned create or modify files from TechnicalSpec.filePlan in "
            "persistentStateDomains.actionsStoreFile."
        ),
        "technical_spec.persistent_state_domains.file_shared": (
            "For three or more persistentStateDomains, give every domain a different actionsStoreFile."
        ),
        "technical_spec.persistent_state_domains.aggregation_missing": (
            "For three or more persistentStateDomains, declare a separate stateAggregation file."
        ),
        "technical_spec.persistent_state_domains.aggregation_file_invalid": (
            "Use a valid relative workspace path for stateAggregation.filePath."
        ),
        "technical_spec.persistent_state_domains.aggregation_file_unplanned": (
            "Reference a model-owned create or modify TechnicalSpec.filePlan file from stateAggregation.filePath."
        ),
        "technical_spec.persistent_state_domains.aggregation_file_conflict": (
            "Use a stateAggregation.filePath different from every persistentStateDomains.actionsStoreFile."
        ),
        "technical_spec.persistent_state_domains.aggregation_responsibilities_invalid": (
            "Limit stateAggregation.responsibilities to compose, persist, and re_export; do not put cross-domain "
            "CRUD there."
        ),
    }
)
_FILE_BATCH_REPAIR_INSTRUCTIONS = MappingProxyType(
    {
        "file_batch_report.batch_id_mismatch": "Set batchId to the requested batch id.",
        "file_batch_report.file_changes_empty": (
            "Provide one complete fileChanges entry for every requested path."
        ),
        "file_batch_report.paths_duplicate": "Use each fileChanges path at most once.",
        "file_batch_report.paths_mismatch": (
            "Make fileChanges paths exactly match the requested batch paths."
        ),
        "file_batch_report.workspace_path_invalid": (
            "Use only valid relative workspace paths in fileChanges."
        ),
        "file_batch_report.starter_protected": (
            "Do not write immutable starter-owned or system-owned paths in fileChanges."
        ),
        "file_batch_report.model_root": (
            "Write fileChanges only inside the StarterManifest modelOwnedRoots."
        ),
        "file_batch_report.file_size_exceeded": (
            "Keep every create or modify fileChanges content within the configured character limit stated in the prompt."
        ),
    }
)


class RunCancelled(RuntimeError):
    pass


class SOPExecutionError(RuntimeError):
    pass


class ArtifactContractViolation(ValueError):
    """A trusted, safe-to-return correction for an internal artifact contract."""

    __slots__ = ("code", "repair_instruction")

    def __init__(self, *, code: str) -> None:
        try:
            repair_instruction = _TECHNICAL_SPEC_REPAIR_INSTRUCTIONS[code]
        except KeyError:
            raise ValueError("unknown artifact contract violation code") from None
        super().__init__(code)
        self.code = code
        self.repair_instruction = repair_instruction


class FileBatchContractViolation(ValueError):
    """A trusted, safe-to-return correction for a FileBatchReport contract."""

    __slots__ = ("code", "repair_instruction")

    def __init__(self, *, code: str) -> None:
        try:
            repair_instruction = _FILE_BATCH_REPAIR_INSTRUCTIONS[code]
        except KeyError:
            raise ValueError("unknown file batch contract violation code") from None
        super().__init__(code)
        self.code = code
        self.repair_instruction = repair_instruction


class EngineerPlanCapacityError(SOPExecutionError):
    """The Architect plan cannot fit in this run's bounded Engineer protocol."""


@dataclass(slots=True)
class _Context:
    run_id: str
    project_id: str
    base_version_id: str | None
    phase: RunPhase
    prompt: str
    lease_token: str
    sandbox: SandboxRef | None = None
    product: ProductSpec | None = None
    technical: TechnicalSpec | None = None
    product_artifact_id: str | None = None
    technical_artifact_id: str | None = None
    implementation: ImplementationReport | None = None
    implementation_plan_artifact_id: str | None = None
    implementation_batch_artifact_ids: list[str] = field(default_factory=list)
    candidate_commit: str | None = None


class SOPRunner:
    """Only this runner changes SOP phases and creates product versions."""

    def __init__(
        self,
        repository: Repository,
        model: ModelClient,
        sandbox: SandboxProvider,
        settings: Settings,
        *,
        agent_adapter: MetaGPTAdapter | None = None,
    ) -> None:
        self.repository = repository
        self.model = model
        self.sandbox = sandbox
        self.settings = settings
        self.starter: StarterManifest = default_starter_manifest()
        if settings.agent_framework == "metagpt":
            self.agent_adapter = agent_adapter or MetaGPTAdapter(model)
        elif settings.agent_framework == "native":
            if agent_adapter is not None:
                raise ValueError("native agent framework cannot receive a MetaGPT adapter")
            self.agent_adapter = None
        else:
            raise ValueError("AGENT_FRAMEWORK must be either 'metagpt' or 'native'")
        self.state_machine = SOPStateMachine()
        self.failure_router = FailureRouter()

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
            active_lease_token = lease_token or await self.repository.get_active_lease_token(run.id)
        except RunLeaseLost:
            # Direct/internal callers without a current claim follow the same
            # stale-worker rule as the main SOP body: no terminal side effect.
            return
        context = _Context(
            run_id=run.id,
            project_id=run.project_id,
            base_version_id=run.base_version_id,
            phase=run.phase,
            prompt=await self.repository.get_run_prompt(run.id),
            lease_token=active_lease_token,
        )
        keep_preview = False
        try:
            await self._check_cancelled(context)
            product = await self._produce_product(context)
            technical = await self._produce_technical(context)
            await self._create_sandbox(context)
            await self._implement(context, product, technical, diagnostic=None)
            diagnostic = await self._verify(context, product, technical)
            await self._repair_until_done(context, product, technical, diagnostic)
            keep_preview = (await self.repository.get_run(run_id)).status == RunStatus.succeeded
        except RunLeaseLost:
            # Recovery owns the terminal outcome. A stale worker must not
            # append a failure event or overwrite that durable decision.
            pass
        except RunCancelled:
            # The cancellation event may already exist; the repository makes
            # the terminal transition/event immutable under recovery races.
            try:
                await self._cleanup_sandbox(context)
                await self.repository.mark_terminal(
                    run_id,
                    RunStatus.cancelled,
                    summary="Cancelled safely by request",
                    lease_token=context.lease_token,
                )
            except RunLeaseLost:
                pass
        except asyncio.CancelledError:
            # A worker shutdown is allowed to leave a non-cancelled run for
            # lease recovery. If the user already requested cancellation,
            # converge it now instead of waiting for the old lease to expire.
            if await self.repository.is_cancel_requested(run_id):
                try:
                    await self._cleanup_sandbox(context)
                    await self.repository.mark_terminal(
                        run_id,
                        RunStatus.cancelled,
                        summary="Cancelled safely during worker shutdown.",
                        lease_token=context.lease_token,
                    )
                except RunLeaseLost:
                    pass
            raise
        except Exception as exc:
            try:
                await self.repository.append_event(
                    run_id,
                    "agent.failed",
                    payload={"stage": context.phase.value, "errorType": type(exc).__name__},
                    lease_token=context.lease_token,
                )
                await self._cleanup_sandbox(context)
                await self.repository.mark_terminal(
                    run_id,
                    RunStatus.failed,
                    error_code="sop_execution_error",
                    summary="The worker stopped before verification completed.",
                    lease_token=context.lease_token,
                )
            except RunLeaseLost:
                pass
            raise
        finally:
            if context.sandbox is not None and not keep_preview:
                # Any exceptional BaseException (including a worker SIGINT)
                # reaches this finalizer. Successful preview sandboxes are the
                # only ones retained; every other durable ref is cleared after
                # a best-effort provider destroy.
                await self._cleanup_sandbox(context)

    async def _produce_product(
        self, context: _Context, diagnostic: DiagnosticReport | None = None
    ) -> ProductSpec:
        await self._transition(context, RunPhase.product_analysis)
        repair_context = "" if diagnostic is None else self._json(diagnostic)
        product = await self._role(
            context,
            role="product_manager",
            model_alias=self.settings.model_pm,
            schema=ProductSpec,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are FOMO's Product Manager. Turn the user request into a concise ProductSpec JSON. "
                        "Use stable AC-1, AC-2 acceptance IDs. Do not provide chain-of-thought, markdown, source "
                        "code, secrets, or unsupported fields."
                    ),
                },
                {
                    "role": "user",
                    "content": f"User request:\n{context.prompt}\nRepair evidence (if any):\n{repair_context}",
                },
            ],
        )
        context.product = product
        artifact_id = await self.repository.store_artifact(
            context.run_id,
            "product_spec",
            product.model_dump(mode="json", by_alias=True),
            role="product_manager",
            lease_token=context.lease_token,
        )
        context.product_artifact_id = artifact_id
        if self.agent_adapter is not None:
            self.agent_adapter.register_artifact(
                run_id=context.run_id,
                role="product_manager",
                artifact_id=artifact_id,
                artifact=product,
            )
        acceptance_items = [item.model_dump(mode="json", by_alias=True) for item in product.acceptance_criteria]
        await self.repository.upsert_acceptance_items(
            context.project_id,
            context.run_id,
            acceptance_items,
            lease_token=context.lease_token,
        )
        for acceptance in product.acceptance_criteria:
            await self.repository.append_trace_link(
                context.run_id,
                "acceptance_criterion",
                acceptance.id,
                "specified_by",
                "artifact",
                artifact_id,
                lease_token=context.lease_token,
            )
        return product

    async def _produce_technical(
        self,
        context: _Context,
        diagnostic: DiagnosticReport | None = None,
    ) -> TechnicalSpec:
        if context.product is None:
            raise SOPExecutionError("architect was invoked without ProductSpec")
        await self._transition(context, RunPhase.architecture)
        max_batches = self._engineer_max_batches()
        max_files_per_batch = self._engineer_max_files_per_batch()
        max_planned_files = max_batches * max_files_per_batch
        max_file_characters = self._engineer_max_file_characters()
        path_label = "path" if max_files_per_batch == 1 else "paths"
        technical = await self._role(
            context,
            role="architect",
            model_alias=self.settings.model_architect,
            schema=TechnicalSpec,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are FOMO's Architect. Consume the supplied ProductSpec and produce only TechnicalSpec JSON. "
                        f"{_DEFAULT_FRONTEND_STACK_CONTRACT} Use the starter's typecheck/build/dev scripts in the test plan. "
                        f"{_PLAYWRIGHT_NETWORK_CONTRACT} "
                        "An immutable starter already exists. Do not create, list in filePlan, modify, or delete any "
                        "StarterManifest protectedPaths. Plan only StarterManifest modelOwnedRoots. Reuse listed "
                        "availableImports directly; do not generate components/ui primitives, package configuration, "
                        "or dependencies. If a required capability is absent, record it in risks instead of installing "
                        "or scaffolding it. Put normal generated routes under app/(generated)/** (or app/page.tsx for "
                        "the root route), business compositions under components/features/**, domain state/types under "
                        "lib/domain/**, and smoke tests under tests/**. "
                        "Return concise componentDecisions only for decision-bearing UI primitives, composed feature "
                        "surfaces, or custom strategies—not a component inventory. Leave ordinary business composition "
                        "components in components, filePlan, and publicApiContracts without requiring a same-name decision. "
                        "A reuse decision may group maintained building blocks; a custom decision must identify a concrete "
                        "capability gap. Avoid per-control Button/Card/Input decisions; include source and rationale. "
                        "Return publicApiContracts only for actual cross-file public symbols, not an inventory of internal "
                        "or same-file symbols; include file path, export style, symbol, props, and type. "
                        "Every ComponentSpec must explicitly set interactionResponsibilities from the schema; use [] for "
                        "leaf or presentational components. For each component with three or more declared concerns, provide "
                        "exactly one featureSurfaces entry. Give every declared concern its own module with filePath and "
                        "publicSymbol, plus one separate controller module. The compositionFile may only compose children, "
                        "arrange layout, and pass props. All composition and module files must be different model-owned create "
                        "or modify filePlan files, and every compositionFile/compositionSymbol and module filePath/publicSymbol "
                        "pair must appear in publicApiContracts. Do not infer or merge UI responsibilities from product-domain "
                        "names. "
                        "Classify every stateModel as persistent_business, transient, or derived. For each "
                        "persistent_business stateModel, mutableDomains must enumerate every independently mutable business "
                        "aggregate it owns; each aggregate with an independent CRUD lifecycle must use its own domain name "
                        "and must not be collapsed into an application, library, or other umbrella domain. Do not include "
                        "component/form/filter state or "
                        "derived stats. When the total declared "
                        "persistent business mutableDomains reaches three or more, map each domain exactly once in "
                        "persistentStateDomains with stateModelName and a model-owned actionsStoreFile. Every such "
                        "actionsStoreFile must be a different create or modify filePlan file. Declare a separate "
                        "stateAggregation filePlan entry whose "
                        "responsibilities are limited to compose, persist, and re_export; it must not contain cross-domain "
                        "CRUD or share a file with a domain actionsStoreFile. "
                        "Strictly use the JSON Schema's literal enum values (never descriptive "
                        f"variants), fit TechnicalSpec.filePlan within {max_batches} Engineer batches of at most "
                        f"{max_files_per_batch} unique valid relative workspace {path_label} each (no more than "
                        f"{max_planned_files} paths total). Each create/modify file must be completable within the "
                        f"Engineer {max_file_characters}-character source limit; split complex state or features across "
                        "files. Keep every list and description concise, and do not include "
                        "chain-of-thought, secrets, or fields outside the schema."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"ProductSpec:\n{self._json(context.product)}\n"
                        f"StarterManifest:\n{self._json(self.starter.as_architect_context())}\n"
                        f"Repair evidence (if any):\n{self._json(diagnostic) if diagnostic else ''}"
                    ),
                },
            ],
            validate_artifact=self._validate_technical_file_plan,
        )
        context.technical = technical
        artifact_id = await self.repository.store_artifact(
            context.run_id,
            "technical_spec",
            technical.model_dump(mode="json", by_alias=True),
            role="architect",
            lease_token=context.lease_token,
        )
        context.technical_artifact_id = artifact_id
        if self.agent_adapter is not None:
            self.agent_adapter.register_artifact(
                run_id=context.run_id,
                role="architect",
                artifact_id=artifact_id,
                artifact=technical,
            )
        if context.product_artifact_id:
            await self.repository.append_trace_link(
                context.run_id,
                "artifact",
                context.product_artifact_id,
                "designed_by",
                "artifact",
                artifact_id,
                lease_token=context.lease_token,
            )
        return technical

    async def _create_sandbox(self, context: _Context) -> None:
        await self._check_cancelled(context)
        if context.sandbox is None:
            context.sandbox = await self.sandbox.create(context.project_id)
            await self.repository.set_sandbox_id(
                context.run_id,
                context.sandbox.id,
                lease_token=context.lease_token,
            )
            await self._copy_and_verify_starter(context)
            await self._ensure_system_gitignore(context)
            result = await self._command(
                context,
                "git init && git config user.email fomo@local.invalid && git config user.name 'FOMO Agent'",
                role="engineer",
            )
            if result.exit_code != 0:
                raise SOPExecutionError("unable to initialize Git in sandbox")
            result = await self._command(
                context,
                "git add -A && git commit -m 'chore(starter): fomo-next-radix-v1'",
                role="engineer",
            )
            if result.exit_code != 0:
                raise SOPExecutionError("unable to create immutable starter Git commit")
            starter_commit = await self._command(context, "git rev-parse HEAD", role="engineer")
            if starter_commit.exit_code != 0 or not starter_commit.stdout.strip():
                raise SOPExecutionError("unable to read immutable starter Git commit")
            initial_commit_sha = starter_commit.stdout.strip().splitlines()[-1]
            await self.repository.store_artifact(
                context.run_id,
                "starter_provenance",
                self.starter.as_provenance(initial_commit_sha),
                role="engineer",
                lease_token=context.lease_token,
            )
            await self.repository.append_event(
                context.run_id,
                "agent.activity",
                role="engineer",
                payload={
                    "action": "sandbox_created",
                    "summary": "Created an isolated workspace from the immutable starter.",
                    "starterId": self.starter.id,
                    "starterVersion": self.starter.version,
                    "starterTreeSha256": self.starter.tree_sha256,
                    "starterCommitSha": initial_commit_sha,
                },
                lease_token=context.lease_token,
            )

    async def _copy_and_verify_starter(self, context: _Context) -> None:
        """Copy the fixed baseline before any model can write to the sandbox."""
        if context.sandbox is None:
            raise SOPExecutionError("cannot bootstrap a starter without a sandbox")
        image_copy = getattr(self.sandbox, "copy_starter", None)
        if callable(image_copy):
            result = await image_copy(context.sandbox, self.starter.id)
            if result.exit_code != 0:
                raise SOPExecutionError("unable to copy immutable starter from sandbox image")
        else:
            await self.sandbox.apply_changes(context.sandbox, self.starter.file_changes)
        copied_files: dict[str, bytes] = {}
        try:
            for entry in self.starter.files:
                copied_files[entry.path] = await self.sandbox.read_file(context.sandbox, entry.path)
            self.starter.verify_tree(copied_files)
        except (FileNotFoundError, StarterIntegrityError) as exc:
            raise SOPExecutionError("immutable starter verification failed") from exc

    async def _implement(
        self,
        context: _Context,
        product: ProductSpec,
        technical: TechnicalSpec,
        diagnostic: DiagnosticReport | None,
    ) -> ImplementationReport:
        await self._check_cancelled(context)
        await self._transition(context, RunPhase.implementation)
        if context.sandbox is None:
            raise SOPExecutionError("engineer was invoked without a sandbox")
        run = await self.repository.get_run(context.run_id)
        if run.repair_round > 0:
            if diagnostic is None:
                raise SOPExecutionError("repair implementation requires a diagnostic scope")
            implementation_technical = self._repair_technical(technical, diagnostic)
            repair_scope_instruction = (
                "This is a repair. TechnicalSpec.filePlan is the complete diagnosis-derived repair scope: plan "
                "exactly those files and never rewrite unrelated files. "
            )
        else:
            implementation_technical = technical
            repair_scope_instruction = ""
        try:
            technical_file_paths = self._technical_file_plan_paths(implementation_technical)
        except ValueError:
            # Architect output normally cannot reach this point without first
            # passing its own structured validator. Retain a safe defense for
            # direct callers and future execution paths.
            raise SOPExecutionError("architect TechnicalSpec failed the Engineer file-plan contract") from None
        max_planned_files = self._engineer_max_planned_files()
        if len(technical_file_paths) > max_planned_files:
            await self.repository.append_event(
                context.run_id,
                "agent.failed",
                role="engineer",
                payload={
                    "role": "engineer",
                    "errorType": "EngineerPlanCapacityError",
                    "plannedFileCount": len(technical_file_paths),
                    "maxPlannedFiles": max_planned_files,
                },
                lease_token=context.lease_token,
            )
            raise EngineerPlanCapacityError(
                "architect file plan exceeds the configured Engineer capacity"
            )
        repair_context = self._json(diagnostic) if diagnostic else ""
        max_files_per_batch = self._engineer_max_files_per_batch()
        file_label = "file" if max_files_per_batch == 1 else "files"
        plan = await self._role(
            context,
            role="engineer",
            model_alias=self.settings.model_engineer,
            schema=ImplementationPlan,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are FOMO's Engineer. Consume both upstream specs and return only ImplementationPlan JSON, "
                        "without source code. Split the working Next.js TypeScript app into ordered, independently "
                        f"writable batches of at most {max_files_per_batch} relative {file_label}, with at "
                        f"most {self._engineer_max_batches()} batches and no more than {max_planned_files} files total. "
                        "The immutable starter already provides package configuration, scripts, layout, globals, and "
                        "components/ui primitives. Do not plan, create, modify, or delete protected paths; plan only "
                        "model-owned business files. Every TechnicalSpec.filePlan path must appear exactly once, with no "
                        "additional paths. "
                        f"{_DEFAULT_FRONTEND_STACK_CONTRACT} {_PLAYWRIGHT_NETWORK_CONTRACT} "
                        "Honor TechnicalSpec.componentDecisions and TechnicalSpec.publicApiContracts. "
                        f"{repair_scope_instruction}"
                        "Never plan .env files, Git hooks, or files outside the workspace. Do not include chain-of-thought."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"ProductSpec:\n{self._json(product)}\nTechnicalSpec:\n{self._json(implementation_technical)}\n"
                        f"StarterManifest:\n{self._json(self.starter.as_architect_context())}\n"
                        "Shared public API contracts (preserve across every batch):\n"
                        f"{self._json(technical.public_api_contracts)}\n"
                        f"DiagnosticReport for repair (if any):\n{repair_context}"
                    ),
                },
            ],
            validate_artifact=lambda candidate: self._validate_implementation_plan(
                candidate, implementation_technical
            ),
            persist_handoff=False,
        )
        context.implementation_plan_artifact_id = await self.repository.store_artifact(
            context.run_id,
            "implementation_plan",
            plan.model_dump(mode="json", by_alias=True),
            role="engineer",
            lease_token=context.lease_token,
        )
        context.implementation_batch_artifact_ids = []
        if context.technical_artifact_id:
            await self.repository.append_trace_link(
                context.run_id,
                "artifact",
                context.technical_artifact_id,
                "implementation_planned_by",
                "artifact",
                context.implementation_plan_artifact_id,
                lease_token=context.lease_token,
            )

        batch_manifests: list[dict[str, Any]] = []
        applied_paths: list[str] = []
        for index, batch in enumerate(plan.batches, start=1):
            await self._check_cancelled(context)
            await self.repository.append_event(
                context.run_id,
                "agent.activity",
                role="engineer",
                payload={
                    "action": "implementation_batch_started",
                    "batchId": batch.id,
                    "batchIndex": index,
                    "batchCount": len(plan.batches),
                    "paths": batch.paths,
                },
                lease_token=context.lease_token,
            )
            batch_report = await self._role(
                context,
                role="engineer",
                model_alias=self.settings.model_engineer,
                schema=FileBatchReport,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are FOMO's Engineer. Return only FileBatchReport JSON for the requested batch. "
                            "Generate complete source for exactly the requested relative paths and no other paths. "
                            "Do not repeat source from earlier or later batches. Each create/modify file must be at most "
                            f"{self._engineer_max_file_characters()} characters. {_DEFAULT_FRONTEND_STACK_CONTRACT} "
                            f"{_PLAYWRIGHT_NETWORK_CONTRACT} Honor every shared public API contract supplied below. "
                            "The immutable starter already exists. Write only StarterManifest modelOwnedRoots and never "
                            "write protected paths, package configuration, or components/ui primitives. "
                            "Never write .env files, Git hooks, "
                            "or files outside the workspace. Do not include chain-of-thought."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"ProductSpec:\n{self._json(product)}\nTechnicalSpec:\n{self._json(implementation_technical)}\n"
                            f"StarterManifest:\n{self._json(self.starter.as_architect_context())}\n"
                            "Shared public API contracts (preserve across every batch):\n"
                            f"{self._json(technical.public_api_contracts)}\n"
                            f"ImplementationPlan:\n{self._json(plan)}\n"
                            f"Requested batch {index} of {len(plan.batches)}:\n{self._json(batch)}\n"
                            f"DiagnosticReport for repair (if any):\n{repair_context}"
                        ),
                    },
                ],
                validate_artifact=lambda report, expected=batch: self._validate_file_batch_report(
                    report, expected
                ),
                persist_handoff=False,
            )
            changes = [
                FileChange(path=item.path, content=item.content, operation=item.operation)
                for item in batch_report.file_changes
            ]
            await self.sandbox.apply_changes(context.sandbox, changes)
            # The model may have omitted, altered, or deleted .gitignore.
            # Restore the system baseline after each durable batch.
            await self._ensure_system_gitignore(context)
            batch_artifact_id = await self.repository.store_artifact(
                context.run_id,
                "implementation_batch",
                batch_report.model_dump(mode="json", by_alias=True),
                role="engineer",
                lease_token=context.lease_token,
            )
            context.implementation_batch_artifact_ids.append(batch_artifact_id)
            await self.repository.append_trace_link(
                context.run_id,
                "artifact",
                context.implementation_plan_artifact_id,
                "implemented_by",
                "artifact",
                batch_artifact_id,
                lease_token=context.lease_token,
            )
            for change in changes:
                applied_paths.append(change.path)
                await self.repository.append_event(
                    context.run_id,
                    "file.changed",
                    role="engineer",
                    payload={"path": change.path, "operation": change.operation, "batchId": batch.id},
                    lease_token=context.lease_token,
                )
                await self.repository.append_trace_link(
                    context.run_id,
                    "artifact",
                    batch_artifact_id,
                    "applied_to",
                    "file",
                    change.path,
                    lease_token=context.lease_token,
                )
                for acceptance_id in batch_report.implemented_acceptance_ids:
                    await self.repository.append_trace_link(
                        context.run_id,
                        "acceptance_criterion",
                        acceptance_id,
                        "implemented_in",
                        "file",
                        change.path,
                        lease_token=context.lease_token,
                    )
            batch_manifests.append(
                {
                    "artifactId": batch_artifact_id,
                    "batchId": batch_report.batch_id,
                    "paths": [change.path for change in changes],
                    "acceptanceIds": batch_report.implemented_acceptance_ids,
                }
            )
            await self.repository.append_event(
                context.run_id,
                "agent.activity",
                role="engineer",
                payload={
                    "action": "implementation_batch_persisted",
                    "batchId": batch.id,
                    "batchIndex": index,
                    "batchCount": len(plan.batches),
                    "artifactId": batch_artifact_id,
                },
                lease_token=context.lease_token,
            )

        # This final compact report is still produced by a real Engineer
        # Role/Action. It carries only the manifest of already persisted files,
        # so candidate-commit handoff never requires the model to repeat a
        # complete project response.
        report = await self._role(
            context,
            role="engineer",
            model_alias=self.settings.model_engineer,
            schema=ImplementationReport,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are FOMO's Engineer. Return only a compact final ImplementationReport JSON for files "
                        "already generated in durable batches. Set fileChanges to [] and never repeat source code. "
                        "Copy batchArtifactIds exactly from the supplied manifest and summarize only its changed files, "
                        "acceptance IDs, design decisions, and known limitations. Do not include chain-of-thought."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Baseline version: {context.base_version_id or ''}\n"
                        f"Implementation batch manifest:\n{self._json(batch_manifests)}"
                    ),
                },
            ],
            validate_artifact=lambda final_report: self._validate_final_implementation_report(
                final_report,
                context.implementation_batch_artifact_ids,
                applied_paths,
            ),
        )
        context.implementation = report
        return report

    def _engineer_max_batches(self) -> int:
        return max(1, self.settings.engineer_max_batches)

    def _engineer_max_files_per_batch(self) -> int:
        return max(1, self.settings.engineer_max_files_per_batch)

    def _engineer_max_planned_files(self) -> int:
        return self._engineer_max_batches() * self._engineer_max_files_per_batch()

    def _engineer_max_file_characters(self) -> int:
        return max(1, self.settings.engineer_max_file_characters)

    def _validate_technical_file_plan(self, technical: TechnicalSpec) -> None:
        if technical.dependencies:
            raise ArtifactContractViolation(code="technical_spec.dependencies.starter_only")
        self._technical_file_plan_paths(technical)
        decision_names = [decision.component for decision in technical.component_decisions]
        if len(decision_names) != len(set(decision_names)):
            raise ArtifactContractViolation(
                code="technical_spec.component_decisions.duplicate",
            )
        planned_files = {
            str(validate_workspace_path(item.path)): item for item in technical.file_plan
        }
        self._validate_persistent_state_domain_slices(technical, planned_files)
        contract_keys: list[tuple[str, str]] = []
        for contract in technical.public_api_contracts:
            try:
                contract_path = str(validate_workspace_path(contract.file_path))
            except ValueError:
                raise ArtifactContractViolation(
                    code="technical_spec.public_api_contracts.path_invalid",
                ) from None
            planned_file = planned_files.get(contract_path)
            if planned_file is None:
                raise ArtifactContractViolation(
                    code="technical_spec.public_api_contracts.file_unplanned",
                )
            if planned_file.operation == "delete":
                raise ArtifactContractViolation(
                    code="technical_spec.public_api_contracts.file_deleted",
                )
            contract_keys.append((contract_path, contract.symbol))
        if len(contract_keys) != len(set(contract_keys)):
            raise ArtifactContractViolation(
                code="technical_spec.public_api_contracts.symbol_duplicate",
            )
        self._validate_feature_surface_slices(technical, planned_files, set(contract_keys))

    def _validate_persistent_state_domain_slices(
        self,
        technical: TechnicalSpec,
        planned_files: dict[str, Any],
    ) -> None:
        """Bind durable domain state to independently writable model-owned files."""
        state_model_names = [state.name for state in technical.state_model]
        if len(state_model_names) != len(set(state_model_names)):
            raise ArtifactContractViolation(
                code="technical_spec.persistent_state_domains.mapping_invalid",
            )

        persistent_domain_owners: list[tuple[str, str]] = []
        for state in technical.state_model:
            mutable_domains = state.mutable_domains
            if state.state_class == "persistent_business":
                if (
                    not mutable_domains
                    or len(mutable_domains) != len(set(mutable_domains))
                    or any(not domain.strip() for domain in mutable_domains)
                ):
                    raise ArtifactContractViolation(
                        code="technical_spec.persistent_state_domains.mutable_domains_invalid",
                    )
                persistent_domain_owners.extend((state.name, domain) for domain in mutable_domains)
            elif mutable_domains:
                raise ArtifactContractViolation(
                    code="technical_spec.persistent_state_domains.mutable_domains_invalid",
                )

        persistent_domain_names = [domain for _state_model_name, domain in persistent_domain_owners]
        if len(persistent_domain_names) != len(set(persistent_domain_names)):
            raise ArtifactContractViolation(
                code="technical_spec.persistent_state_domains.mutable_domains_invalid",
            )
        if len(persistent_domain_owners) < 3:
            return

        domains = technical.persistent_state_domains
        declared_domain_owners = [(domain.state_model_name, domain.domain) for domain in domains]
        if (
            len(declared_domain_owners) != len(set(declared_domain_owners))
            or set(declared_domain_owners) != set(persistent_domain_owners)
        ):
            raise ArtifactContractViolation(
                code="technical_spec.persistent_state_domains.mapping_invalid",
            )

        actions_store_paths: list[str] = []
        for domain in domains:
            try:
                actions_store_path = str(validate_workspace_path(domain.actions_store_file))
            except ValueError:
                raise ArtifactContractViolation(
                    code="technical_spec.persistent_state_domains.file_invalid",
                ) from None
            planned_file = planned_files.get(actions_store_path)
            if planned_file is None or planned_file.operation == "delete":
                raise ArtifactContractViolation(
                    code="technical_spec.persistent_state_domains.file_unplanned",
                )
            actions_store_paths.append(actions_store_path)

        aggregation = technical.state_aggregation
        if len(actions_store_paths) != len(set(actions_store_paths)):
            raise ArtifactContractViolation(
                code="technical_spec.persistent_state_domains.file_shared",
            )
        if aggregation is None:
            raise ArtifactContractViolation(
                code="technical_spec.persistent_state_domains.aggregation_missing",
            )
        try:
            aggregation_path = str(validate_workspace_path(aggregation.file_path))
        except ValueError:
            raise ArtifactContractViolation(
                code="technical_spec.persistent_state_domains.aggregation_file_invalid",
            ) from None
        planned_aggregation_file = planned_files.get(aggregation_path)
        if planned_aggregation_file is None or planned_aggregation_file.operation == "delete":
            raise ArtifactContractViolation(
                code="technical_spec.persistent_state_domains.aggregation_file_unplanned",
            )
        if aggregation_path in actions_store_paths:
            raise ArtifactContractViolation(
                code="technical_spec.persistent_state_domains.aggregation_file_conflict",
            )
        responsibilities = aggregation.responsibilities
        if (
            len(responsibilities) != len(set(responsibilities))
            or not set(responsibilities).issubset(_STATE_AGGREGATION_RESPONSIBILITIES)
        ):
            raise ArtifactContractViolation(
                code="technical_spec.persistent_state_domains.aggregation_responsibilities_invalid",
            )

    def _validate_feature_surface_slices(
        self,
        technical: TechnicalSpec,
        planned_files: dict[str, Any],
        public_api_keys: set[tuple[str, str]],
    ) -> None:
        """Bind explicitly declared complex UI concerns to small public modules."""
        component_names = [component.name for component in technical.components]
        if len(component_names) != len(set(component_names)):
            raise ArtifactContractViolation(
                code="technical_spec.feature_surfaces.component_mapping_invalid",
            )

        components_by_name = {component.name: component for component in technical.components}
        complex_component_names: set[str] = set()
        for component in technical.components:
            concerns = component.interaction_responsibilities
            if len(concerns) != len(set(concerns)):
                raise ArtifactContractViolation(
                    code="technical_spec.feature_surfaces.component_mapping_invalid",
                )
            if len(concerns) >= 3:
                complex_component_names.add(component.name)

        surfaces = technical.feature_surfaces
        surface_component_names = [surface.component_name for surface in surfaces]
        if (
            len(surface_component_names) != len(set(surface_component_names))
            or set(surface_component_names) != complex_component_names
        ):
            raise ArtifactContractViolation(
                code="technical_spec.feature_surfaces.component_mapping_invalid",
            )

        surface_paths: set[str] = set()
        for surface in surfaces:
            component = components_by_name.get(surface.component_name)
            if component is None:
                raise ArtifactContractViolation(
                    code="technical_spec.feature_surfaces.component_mapping_invalid",
                )

            roles = [module.role for module in surface.modules]
            controller_count = roles.count("controller")
            if controller_count == 0:
                raise ArtifactContractViolation(
                    code="technical_spec.feature_surfaces.controller_missing",
                )
            concern_roles = [role for role in roles if role != "controller"]
            if (
                controller_count != 1
                or len(concern_roles) != len(set(concern_roles))
                or set(concern_roles) != set(component.interaction_responsibilities)
            ):
                raise ArtifactContractViolation(
                    code="technical_spec.feature_surfaces.module_mapping_invalid",
                )

            composition_responsibilities = surface.composition_responsibilities
            if (
                len(composition_responsibilities) != len(set(composition_responsibilities))
                or not set(composition_responsibilities).issubset(
                    _FEATURE_SURFACE_COMPOSITION_RESPONSIBILITIES
                )
            ):
                raise ArtifactContractViolation(
                    code="technical_spec.feature_surfaces.composition_responsibilities_invalid",
                )

            try:
                composition_path = str(validate_workspace_path(surface.composition_file))
            except ValueError:
                raise ArtifactContractViolation(
                    code="technical_spec.feature_surfaces.file_invalid",
                ) from None
            module_paths: list[str] = []
            for module in surface.modules:
                try:
                    module_path = str(validate_workspace_path(module.file_path))
                except ValueError:
                    raise ArtifactContractViolation(
                        code="technical_spec.feature_surfaces.file_invalid",
                    ) from None
                module_paths.append(module_path)

            owned_paths = [composition_path, *module_paths]
            if len(owned_paths) != len(set(owned_paths)) or any(
                path in surface_paths for path in owned_paths
            ):
                raise ArtifactContractViolation(
                    code="technical_spec.feature_surfaces.file_conflict",
                )
            for path in owned_paths:
                planned_file = planned_files.get(path)
                if planned_file is None or planned_file.operation == "delete":
                    raise ArtifactContractViolation(
                        code="technical_spec.feature_surfaces.file_unplanned",
                    )
            surface_paths.update(owned_paths)

            if (composition_path, surface.composition_symbol) not in public_api_keys:
                raise ArtifactContractViolation(
                    code="technical_spec.feature_surfaces.public_api_unbound",
                )
            for module, module_path in zip(surface.modules, module_paths, strict=True):
                if (module_path, module.public_symbol) not in public_api_keys:
                    raise ArtifactContractViolation(
                        code="technical_spec.feature_surfaces.public_api_unbound",
                    )

    def _technical_file_plan_paths(self, technical: TechnicalSpec) -> list[str]:
        paths = [item.path for item in technical.file_plan]
        if not paths:
            raise ArtifactContractViolation(
                code="technical_spec.file_plan.empty",
            )
        workspace_paths = []
        try:
            for path in paths:
                workspace_paths.append(validate_workspace_path(path))
        except ValueError:
            raise ArtifactContractViolation(
                code="technical_spec.file_plan.path_invalid",
            ) from None
        if any(path.name in _SYSTEM_MANAGED_FILE_PLAN_NAMES for path in workspace_paths):
            raise ArtifactContractViolation(
                code="technical_spec.file_plan.system_managed",
            )
        if any(self.starter.is_protected_path(str(path)) for path in workspace_paths):
            raise ArtifactContractViolation(
                code="technical_spec.file_plan.starter_protected",
            )
        if any(not self.starter.is_model_owned_path(str(path)) for path in workspace_paths):
            raise ArtifactContractViolation(
                code="technical_spec.file_plan.model_root",
            )
        if len(paths) != len(set(paths)):
            raise ArtifactContractViolation(
                code="technical_spec.file_plan.path_duplicate",
            )
        if len(paths) > self._engineer_max_planned_files():
            raise ArtifactContractViolation(
                code="technical_spec.file_plan.capacity_exceeded",
            )
        return paths

    def _repair_technical(self, technical: TechnicalSpec, diagnostic: DiagnosticReport) -> TechnicalSpec:
        """Restrict repair plans to structured diagnostic evidence, never a full rewrite."""
        self._technical_file_plan_paths(technical)
        planned_paths = {
            str(validate_workspace_path(item.path)): item for item in technical.file_plan
        }
        scope_candidates = set(diagnostic.location_files)
        scope_candidates.update(finding.file for finding in diagnostic.findings if finding.file)

        scoped_paths: set[str] = set()
        for candidate in scope_candidates:
            try:
                normalized = str(validate_workspace_path(candidate))
            except ValueError:
                continue
            if normalized in planned_paths:
                scoped_paths.add(normalized)
        if not scoped_paths:
            raise SOPExecutionError("repair diagnostic did not identify an approved file scope")
        if len(scoped_paths) > _MAX_REPAIR_SCOPE_FILES:
            raise SOPExecutionError(
                f"repair diagnostic scope exceeds the maximum of {_MAX_REPAIR_SCOPE_FILES} approved files"
            )

        return technical.model_copy(
            update={
                "file_plan": [
                    item
                    for item in technical.file_plan
                    if str(validate_workspace_path(item.path)) in scoped_paths
                ]
            }
        )

    def _validate_implementation_plan(self, plan: ImplementationPlan, technical: TechnicalSpec) -> None:
        technical_file_paths = self._technical_file_plan_paths(technical)
        if not plan.batches:
            raise ValueError("implementation plan must contain at least one batch")
        if len(plan.batches) > self._engineer_max_batches():
            raise ValueError("implementation plan exceeds the configured batch limit")
        batch_ids = [batch.id for batch in plan.batches]
        if any(not batch_id.strip() for batch_id in batch_ids) or len(batch_ids) != len(set(batch_ids)):
            raise ValueError("implementation plan batch ids must be nonempty and unique")
        planned_paths: list[str] = []
        for batch in plan.batches:
            if not batch.paths:
                raise ValueError("implementation plan batch must contain at least one path")
            if len(batch.paths) > self._engineer_max_files_per_batch():
                raise ValueError("implementation plan batch exceeds the configured file limit")
            for path in batch.paths:
                workspace_path = str(validate_workspace_path(path))
                if self.starter.is_protected_path(workspace_path):
                    raise ValueError("implementation plan includes an immutable starter path")
                if not self.starter.is_model_owned_path(workspace_path):
                    raise ValueError("implementation plan includes a non-model-owned path")
                planned_paths.append(workspace_path)
        if len(planned_paths) != len(set(planned_paths)):
            raise ValueError("implementation plan must not repeat a path across batches")
        if set(planned_paths) != set(technical_file_paths):
            raise ValueError("implementation plan paths must exactly match the architect TechnicalSpec file plan")

    def _validate_file_batch_report(
        self,
        report: FileBatchReport,
        expected: ImplementationBatchPlan,
    ) -> None:
        if report.batch_id != expected.id:
            raise FileBatchContractViolation(code="file_batch_report.batch_id_mismatch")
        if not report.file_changes:
            raise FileBatchContractViolation(code="file_batch_report.file_changes_empty")
        paths = [change.path for change in report.file_changes]
        for change in report.file_changes:
            try:
                workspace_path = str(validate_workspace_path(change.path))
            except ValueError:
                raise FileBatchContractViolation(
                    code="file_batch_report.workspace_path_invalid"
                ) from None
            if self.starter.is_protected_path(workspace_path):
                raise FileBatchContractViolation(code="file_batch_report.starter_protected")
            if not self.starter.is_model_owned_path(workspace_path):
                raise FileBatchContractViolation(code="file_batch_report.model_root")
        if len(paths) != len(set(paths)):
            raise FileBatchContractViolation(code="file_batch_report.paths_duplicate")
        if set(paths) != set(expected.paths):
            raise FileBatchContractViolation(code="file_batch_report.paths_mismatch")
        for change in report.file_changes:
            if change.operation != "delete" and len(change.content) > self._engineer_max_file_characters():
                raise FileBatchContractViolation(code="file_batch_report.file_size_exceeded")

    def _validate_final_implementation_report(
        self,
        report: ImplementationReport,
        batch_artifact_ids: list[str],
        applied_paths: list[str],
    ) -> None:
        if report.file_changes:
            raise ValueError("final implementation report must reference persisted batches, not repeat source files")
        if report.batch_artifact_ids != batch_artifact_ids:
            raise ValueError("final implementation report must reference every persisted batch in order")
        if len(report.changed_files) != len(set(report.changed_files)):
            raise ValueError("final implementation report must not repeat a changed file")
        if set(report.changed_files) != set(applied_paths):
            raise ValueError("final implementation report changed files must match persisted batches")

    async def _ensure_system_gitignore(self, context: _Context) -> None:
        if context.sandbox is None:
            raise SOPExecutionError("cannot write system Git exclusions without a sandbox")
        try:
            current = await self.sandbox.read_file(context.sandbox, _SYSTEM_GITIGNORE_PATH)
        except FileNotFoundError:
            await self._write_system_gitignore(context)
            return
        if current == _SYSTEM_GITIGNORE.encode("utf-8"):
            return

        # A generated batch may have changed the system-owned file, or its
        # ownership/mode may reject an SDK overwrite. Resetting uses no model
        # content and makes the subsequent provider write deterministic.
        await self._reset_system_gitignore(context)
        await self._write_system_gitignore(context)

    async def _write_system_gitignore(self, context: _Context) -> None:
        assert context.sandbox is not None
        change = FileChange(path=_SYSTEM_GITIGNORE_PATH, content=_SYSTEM_GITIGNORE, operation="create")
        try:
            await self.sandbox.apply_changes(context.sandbox, [change])
        except Exception:
            # OpenSandbox can report an overwrite as a provider-specific 5xx
            # permission error. Recover once using the fixed command, then
            # surface a controlled SOP error rather than provider internals.
            await self._reset_system_gitignore(context)
            try:
                await self.sandbox.apply_changes(context.sandbox, [change])
            except Exception:
                raise SOPExecutionError("unable to restore FOMO system Git exclusions") from None

    async def _reset_system_gitignore(self, context: _Context) -> None:
        result = await self._command(
            context,
            _SYSTEM_GITIGNORE_RESET_COMMAND,
            role="engineer",
        )
        if result.exit_code != 0:
            raise SOPExecutionError("unable to reset FOMO system Git exclusions")

    async def _verify(
        self, context: _Context, product: ProductSpec, technical: TechnicalSpec
    ) -> DiagnosticReport:
        await self._check_cancelled(context)
        await self._transition(context, RunPhase.verification)
        if context.sandbox is None:
            raise SOPExecutionError("reviewer was invoked without a sandbox")
        gates = await self._run_quality_gates(context)
        candidate_commit = await self._create_candidate_commit(context)
        draft = await self._role(
            context,
            role="reviewer",
            model_alias=self.settings.model_reviewer,
            schema=DiagnosticReport,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are FOMO's independent Reviewer. Consume ProductSpec, TechnicalSpec, and actual "
                        "deterministic command results. Return only DiagnosticReport JSON. Never claim an artifact, "
                        "test, screenshot, or gate that is absent from the supplied evidence. When blockers remain, "
                        "populate locationFiles with affected workspace files and only direct dependency files necessary "
                        "for repair; FOMO will reject an unscoped rewrite. Do not include chain-of-thought."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"ProductSpec:\n{self._json(product)}\nTechnicalSpec:\n{self._json(technical)}\n"
                        f"Candidate Git commit:\n{candidate_commit}\n"
                        f"Actual QA gates:\n{self._json(gates)}"
                    ),
                },
            ],
        )
        failed_gates = [gate for gate in gates if gate.status == GateStatus.failed]
        model_blockers = list(draft.blocking_issues)
        model_blockers.extend(
            finding.message for finding in draft.findings if finding.severity in {"major", "error"}
        )
        model_blockers.extend(f"{gate.gate}: {gate.summary}" for gate in failed_gates)
        report = DiagnosticReport(
            gates=gates,
            acceptance_ids=[item.id for item in product.acceptance_criteria],
            issue_fingerprint=None,
            responsible_role=draft.responsible_role,
            blocking_issues=list(dict.fromkeys(item for item in model_blockers if item)),
            evidence=[item for gate in gates for item in gate.evidence],
            location_files=draft.location_files,
            suggested_fix=draft.suggested_fix,
            screenshot_references=[],
            findings=draft.findings,
        )
        report = report.model_copy(
            update={
                "issue_fingerprint": self.failure_router.fingerprint(report),
                "responsible_role": self.failure_router.route(report),
            }
        )
        artifact_id = await self.repository.store_artifact(
            context.run_id,
            "diagnostic_report",
            report.model_dump(mode="json", by_alias=True),
            role="reviewer",
            lease_token=context.lease_token,
        )
        if self.agent_adapter is not None:
            self.agent_adapter.register_artifact(
                run_id=context.run_id,
                role="reviewer",
                artifact_id=artifact_id,
                artifact=report,
            )
        status = "failed" if report.blocking_issues else "passed"
        for acceptance in product.acceptance_criteria:
            await self.repository.record_evidence(
                context.run_id,
                acceptance.id,
                "qa_gates",
                status,
                "All deterministic gates passed." if status == "passed" else "; ".join(report.blocking_issues),
                artifact_id=artifact_id,
                lease_token=context.lease_token,
            )
        return report

    async def _run_quality_gates(self, context: _Context) -> list[GateResult]:
        assert context.sandbox is not None
        gates: list[GateResult] = []
        try:
            package = json.loads((await self.sandbox.read_file(context.sandbox, "package.json")).decode("utf-8"))
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
            return [
                GateResult(
                    gate="package_manifest",
                    status=GateStatus.failed,
                    summary="package.json is missing or invalid.",
                )
            ]

        run = await self.repository.get_run(context.run_id)
        if run.repair_round > 0:
            install_command = "pnpm install --no-frozen-lockfile"
        else:
            install_command = "pnpm install --frozen-lockfile"
            try:
                await self.sandbox.read_file(context.sandbox, "pnpm-lock.yaml")
            except FileNotFoundError:
                install_command = "pnpm install"
        gates.append(await self._gate_command(context, "dependencies", install_command))
        for gate_name, script_name in (("typecheck", "typecheck"), ("build", "build")):
            if not isinstance(scripts, dict) or script_name not in scripts:
                gates.append(
                    GateResult(
                        gate=gate_name,
                        status=GateStatus.failed,
                        summary=f"package.json is missing the required {script_name} script.",
                    )
                )
            else:
                gates.append(await self._gate_command(context, gate_name, f"pnpm {script_name}"))
        if isinstance(scripts, dict) and "test:smoke" in scripts:
            gates.append(await self._gate_command(context, "smoke", "pnpm test:smoke"))
        else:
            gates.append(
                GateResult(
                    gate="smoke",
                    status=GateStatus.skipped,
                    summary="No test:smoke script was supplied; core QA remains typecheck/build/preview.",
                )
            )
        gates.append(await self._start_preview_gate(context, scripts))
        return gates

    async def _gate_command(self, context: _Context, gate: str, command: str) -> GateResult:
        result = await self._command(context, command, role="reviewer")
        status = GateStatus.passed if result.exit_code == 0 and not result.timed_out else GateStatus.failed
        summary = "passed" if status == GateStatus.passed else self._failure_summary(result)
        return GateResult(
            gate=gate,
            status=status,
            summary=summary,
            evidence=[f"command:{command}"],
        )

    async def _start_preview_gate(self, context: _Context, scripts: dict[str, Any]) -> GateResult:
        assert context.sandbox is not None
        if not isinstance(scripts, dict) or "dev" not in scripts:
            return GateResult(
                gate="preview",
                status=GateStatus.failed,
                summary="package.json is missing the required dev script.",
            )
        start_preview = getattr(self.sandbox, "start_preview", None)
        if start_preview is None:
            return GateResult(
                gate="preview",
                status=GateStatus.failed,
                summary="sandbox provider cannot start a preview process.",
            )
        operation_id = uuid7()
        command_text = "pnpm dev --hostname 0.0.0.0 --port 8080"
        await self.repository.append_event(
            context.run_id,
            "command.started",
            role="reviewer",
            payload={"operationId": operation_id, "command": command_text},
            lease_token=context.lease_token,
        )
        try:
            sink = self._output_sink(context, operation_id, "reviewer")
            preview: PreviewRef = await start_preview(
                context.sandbox,
                Command(
                    command=command_text,
                    timeout_seconds=self.settings.preview_start_timeout_seconds,
                    max_output_bytes=self.settings.command_output_limit_bytes,
                    operation_id=operation_id,
                ),
                8080,
                sink,
            )
            await sink.flush()
            passed = preview.url is not None and preview.status == "ready"
            if passed and preview.url and not preview.url.startswith("http://fake-preview.invalid"):
                passed = await self._preview_is_healthy(preview.url)
            await self.repository.append_event(
                context.run_id,
                "command.completed",
                role="reviewer",
                payload={"operationId": operation_id, "exitCode": 0 if passed else 1},
                lease_token=context.lease_token,
            )
            if passed:
                await self.repository.set_preview_url(
                    context.run_id,
                    preview.url,
                    lease_token=context.lease_token,
                )
                await self.repository.append_event(
                    context.run_id,
                    "preview.ready",
                    role="reviewer",
                    payload={"url": preview.url},
                    lease_token=context.lease_token,
                )
                return GateResult(
                    gate="preview",
                    status=GateStatus.passed,
                    summary="Preview health check returned 2xx.",
                    evidence=[f"preview:{preview.url}"],
                )
            return GateResult(
                gate="preview",
                status=GateStatus.failed,
                summary="Preview did not become healthy.",
            )
        except Exception as exc:
            await self.repository.append_event(
                context.run_id,
                "command.completed",
                role="reviewer",
                payload={"operationId": operation_id, "exitCode": 1, "errorType": type(exc).__name__},
                lease_token=context.lease_token,
            )
            return GateResult(
                gate="preview",
                status=GateStatus.failed,
                summary="Preview startup failed.",
            )

    async def _preview_is_healthy(self, url: str) -> bool:
        deadline = time.monotonic() + self.settings.preview_start_timeout_seconds
        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    response = await client.get(url)
                if 200 <= response.status_code < 300:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        return False

    async def _repair_until_done(
        self,
        context: _Context,
        product: ProductSpec,
        technical: TechnicalSpec,
        diagnostic: DiagnosticReport,
    ) -> None:
        previous_fingerprint: str | None = None
        while diagnostic.blocking_issues:
            await self._check_cancelled(context)
            fingerprint = diagnostic.issue_fingerprint
            if previous_fingerprint is not None and fingerprint == previous_fingerprint:
                await self._cleanup_sandbox(context)
                await self.repository.mark_terminal(
                    context.run_id,
                    RunStatus.needs_attention,
                    error_code="repair_no_progress",
                    summary="The same blocking diagnostic recurred after a repair attempt.",
                    lease_token=context.lease_token,
                )
                return
            run = await self.repository.get_run(context.run_id)
            if run.repair_round >= self.settings.max_repair_rounds:
                await self._cleanup_sandbox(context)
                await self.repository.mark_terminal(
                    context.run_id,
                    RunStatus.needs_attention,
                    error_code="repair_limit_reached",
                    summary="The repair limit was reached with blocking QA evidence remaining.",
                    lease_token=context.lease_token,
                )
                return
            previous_fingerprint = fingerprint
            await self._transition(context, RunPhase.repair)
            repair_round = await self.repository.increment_repair_round(
                context.run_id,
                lease_token=context.lease_token,
            )
            target = self.failure_router.route(diagnostic)
            await self.repository.append_event(
                context.run_id,
                "agent.activity",
                role=target,
                payload={
                    "action": "repair_routed",
                    "summary": f"Repair round {repair_round} is routed to {target} from verified evidence.",
                    "fingerprint": fingerprint,
                },
                lease_token=context.lease_token,
            )
            if target == "product_manager":
                product = await self._produce_product(context, diagnostic)
                technical = await self._produce_technical(context, diagnostic)
            elif target == "architect":
                technical = await self._produce_technical(context, diagnostic)
            await self._implement(context, product, technical, diagnostic)
            diagnostic = await self._verify(context, product, technical)
        await self._publish(context)

    async def _publish(self, context: _Context) -> None:
        await self._transition(context, RunPhase.publishing)
        if context.sandbox is None:
            raise SOPExecutionError("cannot publish without sandbox")
        if context.candidate_commit is None:
            raise SOPExecutionError("cannot publish without a candidate Git commit")
        snapshot_id: str | None = None
        capabilities = await self.sandbox.capabilities()
        if capabilities.snapshot:
            snapshot = await self.sandbox.snapshot(context.sandbox)
            snapshot_id = snapshot.id
        files = await self._snapshot_files(context)
        expected_version_number = await self.repository.next_version_number(context.project_id)
        tag = await self._command(
            context,
            f"git tag version/{expected_version_number}",
            role="engineer",
        )
        if tag.exit_code != 0:
            raise SOPExecutionError("Git version tag failed")
        version = await self.repository.create_version(
            context.run_id,
            commit_sha=context.candidate_commit,
            qa_status="passed",
            files=files,
            snapshot_id=snapshot_id,
            lease_token=context.lease_token,
        )
        if version.number != expected_version_number:
            raise SOPExecutionError("version number changed while creating the Git tag")
        await self.repository.mark_terminal(
            context.run_id,
            RunStatus.succeeded,
            summary=f"Version {version.number} passed deterministic QA and reviewer gates.",
            lease_token=context.lease_token,
        )

    async def _create_candidate_commit(self, context: _Context) -> str:
        """Commit the exact workspace reviewed by QA before asking the Reviewer to decide."""
        commit = await self._command(
            context,
            f"git add -A && git commit -m 'feat(agent): run {context.run_id} candidate implementation'",
            role="engineer",
        )
        if commit.exit_code != 0:
            raise SOPExecutionError("Git candidate commit failed")
        sha_result = await self._command(context, "git rev-parse HEAD", role="engineer")
        if sha_result.exit_code != 0 or not sha_result.stdout.strip():
            raise SOPExecutionError("unable to read candidate Git commit")
        context.candidate_commit = sha_result.stdout.strip().splitlines()[-1]
        if context.implementation is not None:
            context.implementation = context.implementation.model_copy(
                update={"candidate_commit": context.candidate_commit}
            )
            artifact_id = await self.repository.store_artifact(
                context.run_id,
                "implementation_report",
                context.implementation.model_dump(mode="json", by_alias=True),
                role="engineer",
                lease_token=context.lease_token,
            )
            if self.agent_adapter is not None:
                self.agent_adapter.register_artifact(
                    run_id=context.run_id,
                    role="engineer",
                    artifact_id=artifact_id,
                    artifact=context.implementation,
                )
            if context.technical_artifact_id:
                await self.repository.append_trace_link(
                    context.run_id,
                    "artifact",
                    context.technical_artifact_id,
                    "implemented_by",
                    "artifact",
                    artifact_id,
                    lease_token=context.lease_token,
                )
            if context.implementation_plan_artifact_id:
                await self.repository.append_trace_link(
                    context.run_id,
                    "artifact",
                    context.implementation_plan_artifact_id,
                    "summarized_by",
                    "artifact",
                    artifact_id,
                    lease_token=context.lease_token,
                )
            for batch_artifact_id in context.implementation_batch_artifact_ids:
                await self.repository.append_trace_link(
                    context.run_id,
                    "artifact",
                    batch_artifact_id,
                    "summarized_by",
                    "artifact",
                    artifact_id,
                    lease_token=context.lease_token,
                )
        return context.candidate_commit

    async def _snapshot_files(self, context: _Context) -> list[dict[str, Any]]:
        assert context.sandbox is not None
        list_files = getattr(self.sandbox, "list_files", None)
        if list_files is None:
            raise SOPExecutionError("sandbox provider cannot persist a file manifest")
        files = await list_files(context.sandbox)
        if not files:
            raise SOPExecutionError("sandbox workspace is empty after implementation")
        return list(files)

    async def _role(
        self,
        context: _Context,
        *,
        role: str,
        model_alias: str,
        schema: type[Artifact],
        messages: list[dict[str, str]],
        validate_artifact: Callable[[Artifact], None] | None = None,
        persist_handoff: bool = True,
    ) -> Artifact:
        await self._check_cancelled(context)
        schema_contract = json.dumps(
            schema.model_json_schema(by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        messages = [
            *messages,
            {
                "role": "user",
                "content": f"Required JSON Schema for {schema.__name__}:\n{schema_contract}",
            },
        ]
        await self.repository.append_event(
            context.run_id,
            "agent.started",
            role=role,
            payload={"role": role, "modelAlias": model_alias},
            lease_token=context.lease_token,
        )

        async def record_transport_retry(retry: ModelRetry) -> None:
            # This deliberately contains only a bounded category/status and
            # never request URLs, headers, response content, or credentials.
            payload: dict[str, Any] = {
                "action": "model_transport_retry",
                "attempt": retry.attempt,
                "maxAttempts": retry.max_attempts,
                "delaySeconds": retry.delay_seconds,
                "failureKind": retry.failure_kind,
            }
            if retry.status_code is not None:
                payload["statusCode"] = retry.status_code
            if retry.transport_error is not None:
                payload["transportError"] = retry.transport_error
            await self.repository.append_event(
                context.run_id,
                "agent.activity",
                role=role,
                payload=payload,
                lease_token=context.lease_token,
            )

        attempts = self.settings.structured_output_retries + 1
        for attempt in range(attempts):
            try:
                if self.agent_adapter is None:
                    payload = await self._await_cancellable(
                        context,
                        self.model.complete_json(
                            model_alias,
                            messages,
                            schema.__name__,
                            on_retry=record_transport_retry,
                        ),
                    )
                    artifact = schema.model_validate(payload)
                else:
                    artifact = await self._await_cancellable(
                        context,
                        self.agent_adapter.run_action(
                            run_id=context.run_id,
                            role=role,
                            model_alias=model_alias,
                            schema=schema,
                            messages=messages,
                            persist_handoff=persist_handoff,
                            on_retry=record_transport_retry,
                        ),
                    )
                    if not isinstance(artifact, schema):
                        raise TypeError(f"MetaGPT {role} returned the wrong artifact type")
                if validate_artifact is not None:
                    validate_artifact(artifact)
                await self.repository.append_event(
                    context.run_id,
                    "agent.completed",
                    role=role,
                    payload={"role": role, "artifact": schema.__name__, "attempt": attempt + 1},
                    lease_token=context.lease_token,
                )
                return artifact
            except ModelRequestError as exc:
                # Gateway recovery has already used its own bounded retry
                # budget. Do not turn a transport/configuration failure into a
                # second model generation that consumes schema-repair budget.
                await self.repository.append_event(
                    context.run_id,
                    "agent.failed",
                    role=role,
                    payload={
                        "role": role,
                        "errorType": type(exc).__name__,
                        "attempts": exc.attempts,
                        "failureKind": exc.failure_kind,
                        **({"statusCode": exc.status_code} if exc.status_code is not None else {}),
                        **(
                            {"transportError": exc.transport_error}
                            if exc.transport_error is not None
                            else {}
                        ),
                    },
                    lease_token=context.lease_token,
                )
                raise SOPExecutionError(f"{role} model request failed") from None
            except (ModelError, ValidationError, ValueError, TypeError) as exc:
                trusted_contract_violation: (
                    ArtifactContractViolation | FileBatchContractViolation | None
                ) = None
                if (
                    role == "architect"
                    and schema is TechnicalSpec
                    and type(exc) is ArtifactContractViolation
                ):
                    trusted_contract_violation = exc
                elif (
                    role == "engineer"
                    and schema is FileBatchReport
                    and type(exc) is FileBatchContractViolation
                ):
                    trusted_contract_violation = exc
                reason_code = (
                    trusted_contract_violation.code
                    if trusted_contract_violation is not None
                    else None
                )
                repair_instruction = (
                    trusted_contract_violation.repair_instruction
                    if trusted_contract_violation is not None
                    else None
                )
                if attempt + 1 < attempts:
                    retry_payload: dict[str, Any] = {
                        "action": "structured_retry",
                        "summary": "The structured hand-off was invalid; requesting a schema-correct response.",
                    }
                    if reason_code is not None:
                        retry_payload["reasonCode"] = reason_code
                    await self.repository.append_event(
                        context.run_id,
                        "agent.activity",
                        role=role,
                        payload=retry_payload,
                        lease_token=context.lease_token,
                    )
                    correction_message = (
                        f"Return only a valid {schema.__name__} JSON object matching the declared schema."
                    )
                    if repair_instruction is not None:
                        correction_message = f"{correction_message}\n{repair_instruction}"
                    messages = [
                        *messages,
                        {
                            "role": "user",
                            "content": correction_message,
                        },
                    ]
                    continue
                failure_payload: dict[str, Any] = {"role": role, "errorType": type(exc).__name__}
                if reason_code is not None:
                    failure_payload["reasonCode"] = reason_code
                await self.repository.append_event(
                    context.run_id,
                    "agent.failed",
                    role=role,
                    payload=failure_payload,
                    lease_token=context.lease_token,
                )
                raise SOPExecutionError(f"{role} failed to produce a valid {schema.__name__}") from exc
        raise AssertionError("unreachable")

    async def _await_cancellable(self, context: _Context, operation: Awaitable[Awaited]) -> Awaited:
        """Await a model/MetaGPT call while polling the durable cancel flag.

        Transport timeouts are intentionally generous for normal generations.
        A cancellation request must not inherit the configured model timeout: cancel
        the underlying task, let its HTTP client unwind, and surface the SOP's
        controlled ``RunCancelled`` path instead.
        """
        task = asyncio.ensure_future(operation)
        try:
            while True:
                await self._check_cancelled(context)
                done, _pending = await asyncio.wait((task,), timeout=0.25)
                if task in done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _cleanup_sandbox(self, context: _Context) -> None:
        """Release an unsuccessful sandbox without masking the primary outcome."""
        sandbox = context.sandbox
        if sandbox is None:
            return
        try:
            await self.sandbox.kill(sandbox)
        except Exception:
            # Keep the durable ref for the worker's startup recovery sweep.
            # Do not surface provider details while unwinding an earlier error.
            return
        try:
            await self.repository.clear_sandbox_id(
                context.run_id,
                sandbox.id,
                lease_token=context.lease_token,
            )
        except Exception:
            # A lost lease must never acknowledge durable state after recovery
            # terminalized the run. Other acknowledgement failures leave the
            # exact ref for worker recovery to retry safely.
            pass
        finally:
            # Provider cleanup completed, so do not invoke it twice from the
            # outer finalizer. A retained durable reference is intentional
            # when the fenced acknowledgement could not be committed.
            context.sandbox = None

    async def _command(self, context: _Context, command_text: str, *, role: str) -> Any:
        assert context.sandbox is not None
        await self._check_cancelled(context)
        operation_id = uuid7()
        await self.repository.append_event(
            context.run_id,
            "command.started",
            role=role,
            payload={"operationId": operation_id, "command": command_text},
            lease_token=context.lease_token,
        )
        sink = self._output_sink(context, operation_id, role)
        result = await self.sandbox.exec(
            context.sandbox,
            Command(
                command=command_text,
                timeout_seconds=self.settings.command_timeout_seconds,
                max_output_bytes=self.settings.command_output_limit_bytes,
                operation_id=operation_id,
            ),
            sink,
        )
        await sink.flush()
        await self.repository.append_event(
            context.run_id,
            "command.completed",
            role=role,
            payload={
                "operationId": operation_id,
                "exitCode": result.exit_code,
                "timedOut": result.timed_out,
            },
            lease_token=context.lease_token,
        )
        await self._check_cancelled(context)
        return result

    def _output_sink(self, context: _Context, operation_id: str, role: str):
        buffers: dict[str, str] = {"stdout": "", "stderr": ""}
        timers: dict[str, asyncio.Task[None]] = {}

        async def emit(stream: str) -> None:
            text = buffers.get(stream, "")
            if not text:
                return
            buffers[stream] = ""
            try:
                await self.repository.append_event(
                    context.run_id,
                    "command.output",
                    role=role,
                    payload={"operationId": operation_id, "stream": stream, "text": text},
                    lease_token=context.lease_token,
                )
            except RunLeaseLost:
                # Timed output callbacks can outlive the SOP task by one
                # scheduling turn. The fence rejects the write; swallowing
                # this expected outcome prevents an unobserved task warning.
                return

        async def delayed_emit(stream: str) -> None:
            try:
                await asyncio.sleep(0.05)
                await emit(stream)
            finally:
                timers.pop(stream, None)

        async def sink(stream: str, text: str) -> None:
            buffers[stream] = buffers.get(stream, "") + self._redact(text)
            if len(buffers[stream].encode("utf-8")) >= 4096:
                timer = timers.pop(stream, None)
                if timer is not None:
                    timer.cancel()
                await emit(stream)
            elif stream not in timers:
                timers[stream] = asyncio.create_task(delayed_emit(stream))

        async def flush_all() -> None:
            pending = list(timers.values())
            timers.clear()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for stream in list(buffers):
                await emit(stream)

        return _FlushOnCompletionSink(sink, flush_all)

    async def _transition(self, context: _Context, target: RunPhase) -> None:
        self.state_machine.transition(context.phase, target)
        if context.phase != target:
            await self.repository.set_run_phase(
                context.run_id,
                target,
                lease_token=context.lease_token,
            )
            context.phase = target

    async def _check_cancelled(self, context: _Context) -> None:
        if not await self.repository.is_active_lease(context.run_id, context.lease_token):
            raise RunLeaseLost("run lease is no longer active")
        if await self.repository.is_cancel_requested(context.run_id):
            raise RunCancelled()

    @staticmethod
    def _json(value: Any) -> str:
        def jsonable(item: Any) -> Any:
            if hasattr(item, "model_dump"):
                return item.model_dump(mode="json", by_alias=True)
            if isinstance(item, list):
                return [jsonable(child) for child in item]
            if isinstance(item, tuple):
                return [jsonable(child) for child in item]
            if isinstance(item, dict):
                return {str(key): jsonable(child) for key, child in item.items()}
            return item

        return json.dumps(jsonable(value), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _failure_summary(result: Any) -> str:
        text = (result.stderr or result.stdout or "command failed").strip().replace("\n", " ")
        return SOPRunner._redact(text[:1000])

    @staticmethod
    def _redact(value: str) -> str:
        value = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s]+", r"\1[REDACTED]", value)
        return re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}\b", "[REDACTED]", value)


class _FlushOnCompletionSink:
    """Callable sink plus a finalizer invoked by ``_command`` after provider execution."""

    def __init__(self, sink: Any, flush_callback: Any) -> None:
        self._sink = sink
        self._flush_callback = flush_callback

    async def __call__(self, stream: str, text: str) -> None:
        await self._sink(stream, text)

    async def flush(self) -> None:
        await self._flush_callback()
