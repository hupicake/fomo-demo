"""Deterministic clean-sandbox verification and evidence persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from fomo.agent_runtime.playwright_reporter import parse_playwright_json
from fomo.config import Settings
from fomo.ids import utcnow, uuid7
from fomo.persistence import Repository
from fomo.sandbox.base import Command, ExecResult, PreviewRef, SandboxProvider, SandboxRef
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.schemas import GateDiagnostic, GateResult, GateStatus

from .acceptance import (
    ACCEPTANCE_CONFIG_PATH,
    FOMO_HARNESS_PATH,
    FOMO_PLAYWRIGHT_TEST_MODULE,
    CompiledAcceptance,
)
from .contracts import AcceptanceContract
from .execution import CommandExecutor, assert_run_active, redact
from .goal_manager import (
    RegressionSuite,
    RuntimeValidationMode,
    RuntimeValidationReason,
)
from .goalgraph import (
    NavigationVerificationSuite,
    acceptance_persistence_key,
    navigation_evidence_key,
    navigation_test_ids,
)
from .workspace import (
    FOMO_RUNNER_BIN,
    FOMO_RUNNER_NODE,
    FOMO_RUNNER_PATH,
    fomo_runner_command,
)

# FOMO-owned runner binaries (see fomo_runner_command in .workspace):
# root-owned pnpm shell wrappers at absolute paths in the read-only runtime
# cache, invoked with a PATH containing no node-writable directory. The probe
# also requires the trusted system Node that those wrappers resolve. Gates
# never resolve tooling from the candidate's node_modules/.bin or writable
# image PATH, so a candidate cannot swap or hijack the runner. This is
# container-level hardening only: candidate Next config/app and the tests run
# in the same V user/process boundary, so external QA runner or read-only test
# mounts remain the public-deployment blocker (see the root README).
_RUNNER_PROBE = (
    f"test -x {FOMO_RUNNER_NODE} "
    f"&& test -x {FOMO_RUNNER_BIN}/tsc "
    f"&& test -x {FOMO_RUNNER_BIN}/playwright "
    f"&& test -r {FOMO_PLAYWRIGHT_TEST_MODULE} "
    "&& test -r /opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/next/package.json"
)
_ROOT_NEXT_PACKAGE = (
    "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/next/package.json"
)
_WORKSPACE_NEXT_PACKAGE = "/workspace/node_modules/next/package.json"
_WORKSPACE_NEXT_BIN = "/workspace/node_modules/next/dist/bin/next"
_NEXT_RUNTIME_CHECK_SCRIPT = (
    'const fs=require("node:fs");'
    "try{"
    f'const root=JSON.parse(fs.readFileSync("{_ROOT_NEXT_PACKAGE}","utf8"));'
    f'const workspace=JSON.parse(fs.readFileSync("{_WORKSPACE_NEXT_PACKAGE}","utf8"));'
    f'fs.accessSync("{_WORKSPACE_NEXT_BIN}",fs.constants.R_OK);'
    'if(typeof root.version!=="string"||workspace.version!==root.version)throw new Error();'
    '}catch(error){console.error("workspace Next runtime is missing or version-mismatched");'
    "process.exit(1)}"
)
_NEXT_RUNTIME_PROBE = (
    f"env PATH={shlex.quote(FOMO_RUNNER_PATH)} "
    f"{shlex.quote(FOMO_RUNNER_NODE)} -e {shlex.quote(_NEXT_RUNTIME_CHECK_SCRIPT)}"
)
_WORKSPACE_NEXT = (
    f"env PATH={shlex.quote(FOMO_RUNNER_PATH)} "
    f"{shlex.quote(FOMO_RUNNER_NODE)} {shlex.quote(_WORKSPACE_NEXT_BIN)}"
)
_PLAYWRIGHT = fomo_runner_command(
    bin_name="playwright",
    args="test {path} --config={config} --project=chromium --reporter=json",
)


def _with_preview_base_path(command: str, base_path: str | None) -> str:
    if not base_path:
        return command
    return f"FOMO_PREVIEW_BASE_PATH={shlex.quote(base_path)} {command}"


def _preview_runtime_url(url: str, base_path: str | None) -> str:
    """Attach the build-time Next basePath to one provider endpoint."""

    if not base_path:
        return url
    parsed = urlsplit(url)
    endpoint_path = parsed.path.rstrip("/")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"{endpoint_path}{base_path}", parsed.query, "")
    )


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    passed: bool
    gates: tuple[GateResult, ...]
    diagnostic_artifact_id: str
    preview_url: str | None
    # Elapsed seconds from run start to verification completion. Carried so
    # preview.verified (emitted by the orchestrator only after the final
    # consistency check and version creation) keeps the same event payload.
    preview_elapsed_seconds: float | None = None
    validation_mode: RuntimeValidationMode = RuntimeValidationMode.FULL
    validation_reason: RuntimeValidationReason = RuntimeValidationReason.P0_RELEASE

    @property
    def has_infrastructure_failure(self) -> bool:
        """Return failures that deterministic source repair cannot resolve.

        An ordinary non-zero dependency install is a candidate/package
        problem and stays repairable; only a timed-out install (transport) or
        a missing fixed runner/restore failure is infrastructure.
        """
        return any(
            (
                gate.scope == "project"
                and gate.gate in {"runner", "restore"}
                and gate.status == GateStatus.failed
            )
            or (
                gate.scope == "project"
                and gate.gate == "dependencies"
                and gate.status == GateStatus.failed
                and gate.timed_out
            )
            or (
                gate.scope == "acceptance"
                and gate.outcome == "infrastructure_failed"
            )
            or (
                gate.scope == "navigation"
                and gate.outcome == "infrastructure_failed"
            )
            for gate in self.gates
        )

    def as_repair_context(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "previewUrlAvailable": self.preview_url is not None,
            "diagnosticArtifactId": self.diagnostic_artifact_id,
            "validationMode": self.validation_mode.value,
            "validationReason": self.validation_reason.value,
            "gates": [item.model_dump(mode="json", by_alias=True) for item in self.gates],
        }

    def checkpoint_evidence(
        self,
        goal_id: str,
        *,
        navigation_suite: NavigationVerificationSuite | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Return bounded passed evidence for only the newly claimed goal."""

        prefix = f"{goal_id}:"
        acceptance = tuple(
            {
                "acceptanceKey": gate.acceptance_id,
                "kind": "playwright_smoke",
                "status": "passed",
                "artifactId": self.diagnostic_artifact_id,
                "reference": gate.test_path,
                "summary": gate.summary,
                "payload": {
                    "testName": gate.test_name,
                    "exitCode": gate.exit_code,
                },
            }
            for gate in self.gates
            if gate.scope == "acceptance"
            and gate.status == GateStatus.passed
            and gate.acceptance_id is not None
            and gate.acceptance_id.startswith(prefix)
        )
        navigation_by_id = {
            gate.gate.removeprefix("navigation:"): gate
            for gate in self.gates
            if gate.scope == "navigation"
            and gate.status == GateStatus.passed
            and gate.gate.startswith("navigation:")
        }
        navigation = tuple(
            {
                "acceptanceKey": navigation_evidence_key(
                    navigation_suite.version,
                    test_id,
                ),
                "kind": f"fomo_navigation_v{navigation_suite.version}",
                "status": "passed",
                "artifactId": self.diagnostic_artifact_id,
                "reference": f"navigation:v{navigation_suite.version}:{test_id}",
                "summary": compiled_gate.summary,
                "payload": {"testId": test_id, "suiteMode": navigation_suite.mode},
            }
            for test_id in navigation_test_ids(navigation_suite)
            if (compiled_gate := navigation_by_id.get(test_id)) is not None
        )
        return (*acceptance, *navigation)


class Verifier:
    def __init__(
        self,
        repository: Repository,
        sandbox: SandboxProvider,
        settings: Settings,
        commands: CommandExecutor,
        *,
        run_id: str,
        lease_token: str,
        started_at: float,
    ) -> None:
        self.repository = repository
        self.sandbox = sandbox
        self.settings = settings
        self.commands = commands
        self.run_id = run_id
        self.lease_token = lease_token
        self.started_at = started_at

    def _elapsed_seconds(self) -> float:
        return round(max(0.0, time.monotonic() - self.started_at), 1)

    async def verify(
        self,
        ref: SandboxRef,
        contract: AcceptanceContract,
        compiled: CompiledAcceptance,
        *,
        round_number: int,
        candidate_paths: tuple[str, ...],
    ) -> VerificationOutcome:
        return await self._verify_contracts(
            ref,
            ((None, contract),),
            compiled,
            round_number=round_number,
            candidate_paths=candidate_paths,
            mode=RuntimeValidationMode.FULL,
            reason=RuntimeValidationReason.P0_RELEASE,
        )

    async def verify_regression(
        self,
        ref: SandboxRef,
        suite: RegressionSuite,
        compiled: CompiledAcceptance,
        *,
        round_number: int,
        candidate_paths: tuple[str, ...],
    ) -> VerificationOutcome:
        """Run the server-selected focused or full GoalGraph validation suite."""

        return await self._verify_contracts(
            ref,
            tuple((item.goal_id, item.contract) for item in suite.contracts),
            compiled,
            round_number=round_number,
            candidate_paths=candidate_paths,
            mode=suite.mode,
            reason=suite.reason,
        )

    async def _verify_contracts(
        self,
        ref: SandboxRef,
        contracts: tuple[tuple[str | None, AcceptanceContract], ...],
        compiled: CompiledAcceptance,
        *,
        round_number: int,
        candidate_paths: tuple[str, ...],
        mode: RuntimeValidationMode,
        reason: RuntimeValidationReason,
    ) -> VerificationOutcome:
        gates: list[GateResult] = []
        preview_url: str | None = None
        goal_ids = [goal_id for goal_id, _ in contracts if goal_id is not None]
        await self.repository.append_event(
            self.run_id,
            "verification.suite_started",
            payload={
                "mode": mode.value,
                "reason": reason.value,
                "goalIds": goal_ids,
                "round": round_number,
            },
            lease_token=self.lease_token,
        )
        for goal_id, contract in contracts:
            await self._reset_acceptance(contract, goal_id=goal_id)

        # Fixed runner probe first: a missing FOMO-owned runner is
        # infrastructure, never a candidate defect.
        runner = await self._command_gate(
            ref, "runner", _RUNNER_PROBE, candidate_paths=()
        )
        gates.append(runner)

        if runner.status == GateStatus.passed:
            # --ignore-scripts blocks candidate lifecycle scripts; the install
            # stays offline against FOMO's prefetched store.
            install = await self._command_gate(
                ref,
                "dependencies",
                "pnpm install --offline --frozen-lockfile --ignore-scripts",
                candidate_paths=candidate_paths,
            )
            gates.append(install)

            # The generated project may only use the pinned Next version
            # already present in the immutable runtime cache. Execute the
            # workspace package itself after proving the versions match:
            # using the root-cache Next CLI with workspace runtime imports
            # creates two Next singletons and breaks workStore invariants.
            if all(item.status == GateStatus.passed for item in gates):
                gates.append(
                    await self._command_gate(
                        ref,
                        "next_runtime",
                        _NEXT_RUNTIME_PROBE,
                        candidate_paths=candidate_paths,
                    )
                )

            # Authoritative gates use FOMO-owned absolute runner binaries from
            # the root-owned runtime cache for tsc/Playwright, never scripts or
            # candidate node_modules/.bin resolution. Next is the checked
            # workspace package above, invoked by absolute system Node with a
            # clean PATH. Every mode must finish a production build before the
            # preview starts. `next dev` rewrites next-env.d.ts to reference
            # .next/dev/types in Next 16, which violates the frozen manifest;
            # `next start` serves the production output without that mutation.
            if all(item.status == GateStatus.passed for item in gates):
                gates.append(
                    await self._command_gate(
                        ref, "typecheck", fomo_runner_command(bin_name="tsc", args="--noEmit"),
                        candidate_paths=candidate_paths,
                    )
                )
            if all(item.status == GateStatus.passed for item in gates):
                build_command = _with_preview_base_path(
                    f"{_WORKSPACE_NEXT} build",
                    self.settings.published_preview_base_path(ref.id),
                )
                gates.append(
                    await self._command_gate(
                        ref, "build", build_command,
                        candidate_paths=candidate_paths,
                    )
                )
            # FOCUSED narrows only the acceptance regression set. It cannot
            # weaken the production build or preview runtime contract.
            if all(item.status == GateStatus.passed for item in gates):
                preview = await self._start_preview(ref)
                gates.append(preview[0])
                preview_url = preview[1]

        # Candidate code has now executed (install/typecheck/build/start).
        # Re-inject and re-verify FOMO-owned acceptance tests and restore the
        # trusted harness before any Playwright gate. This is hardening, not a
        # host-level read-only boundary (see the root README: external QA runner /
        # read-only mounts remain a release blocker).
        if all(item.status == GateStatus.passed for item in gates):
            restore = await self._restore_fomo_owned(ref, compiled)
            gates.append(restore)

        if all(item.status == GateStatus.passed for item in gates):
            gates.append(
                await self._playwright_project_gate(ref, FOMO_HARNESS_PATH)
            )

        navigation_paths = compiled.navigation_test_path_by_id or {}
        navigation_names = compiled.navigation_test_name_by_id or {}
        if all(item.status == GateStatus.passed for item in gates):
            for navigation_id, test_path in navigation_paths.items():
                gate = await self._navigation_gate(
                    ref,
                    navigation_id,
                    test_path,
                    navigation_names[navigation_id],
                )
                gates.append(gate)
                if gate.status is not GateStatus.passed:
                    break

        if all(item.status == GateStatus.passed for item in gates):
            for goal_id, contract in contracts:
                for criterion in contract.criteria:
                    acceptance_key = (
                        acceptance_persistence_key(goal_id, criterion.id)
                        if goal_id is not None
                        else criterion.id
                    )
                    gates.append(
                        await self._acceptance_gate(
                            ref,
                            acceptance_key,
                            compiled.test_path_by_acceptance_id[acceptance_key],
                            compiled.test_name_by_acceptance_id[acceptance_key],
                        )
                    )

        criterion_count = sum(len(contract.criteria) for _, contract in contracts)
        project_gate_count = 8 + len(navigation_paths)
        passed = all(item.status == GateStatus.passed for item in gates) and len(gates) == (
            project_gate_count + criterion_count
        )
        diagnostic = {
            "round": round_number,
            "passed": passed,
            "validationMode": mode.value,
            "validationReason": reason.value,
            "goalIds": goal_ids,
            "candidatePaths": list(candidate_paths),
            "gates": [item.model_dump(mode="json", by_alias=True) for item in gates],
        }
        await assert_run_active(self.repository, self.run_id, self.lease_token)
        artifact_id = await self.repository.store_artifact(
            self.run_id,
            "diagnostic_report",
            diagnostic,
            lease_token=self.lease_token,
        )
        await self._record_acceptance_evidence(gates, artifact_id)
        # preview.verified is deliberately NOT emitted here: it may only fire
        # after the orchestrator's final frozen-manifest consistency check and
        # version creation succeed. Carry the measurement instead.
        preview_elapsed_seconds = self._elapsed_seconds() if passed and preview_url else None
        return VerificationOutcome(
            passed=passed,
            gates=tuple(gates),
            diagnostic_artifact_id=artifact_id,
            preview_url=preview_url,
            preview_elapsed_seconds=preview_elapsed_seconds,
            validation_mode=mode,
            validation_reason=reason,
        )

    async def _reset_acceptance(
        self,
        contract: AcceptanceContract,
        *,
        goal_id: str | None = None,
    ) -> None:
        for item in contract.criteria:
            acceptance_key = (
                acceptance_persistence_key(goal_id, item.id)
                if goal_id is not None
                else item.id
            )
            await self.repository.append_event(
                self.run_id,
                "verification.updated",
                payload={
                    "scope": "acceptance",
                    "acceptanceId": item.id,
                    "acceptanceKey": acceptance_key,
                    **({"goalId": goal_id} if goal_id is not None else {}),
                    "status": "unverified",
                },
                lease_token=self.lease_token,
            )

    async def _command_gate(
        self,
        ref: SandboxRef,
        gate: str,
        command: str,
        *,
        candidate_paths: tuple[str, ...],
    ) -> GateResult:
        result = await self.commands.run(
            ref, command, label=gate, stage="verifying"
        )
        status = (
            GateStatus.passed
            if result.exit_code == 0 and not result.timed_out
            else GateStatus.failed
        )
        output = redact(f"{result.stdout}\n{result.stderr}")
        affected = [path for path in candidate_paths if path in output][:8]
        gate_result = GateResult(
            gate=gate,
            status=status,
            summary="passed" if status == GateStatus.passed else self._failure_summary(result),
            evidence=[f"command:{command}"],
            affected_files=affected,
            timed_out=result.timed_out,
        )
        await self._record_project_gate(gate_result)
        return gate_result

    async def _restore_fomo_owned(
        self, ref: SandboxRef, compiled: CompiledAcceptance
    ) -> GateResult:
        """Re-inject acceptance tests and restore the trusted harness after
        candidate code executed, then re-verify hashes before Playwright.

        The compiled set includes the authoritative V-only harness, config,
        and acceptance specs. All import the same root-cache Playwright Test
        module as the fixed CLI, avoiding a second workspace-installed module
        identity. The tests still live in the candidate workspace inside V
        and are not host-level read-only; an external QA runner / read-only
        mount is the public-deployment blocker."""
        try:
            if FOMO_HARNESS_PATH not in compiled.sha256_by_path:
                raise RuntimeError("trusted harness is unavailable")
            await self.sandbox.apply_changes(ref, list(compiled.changes))
            for path, digest in compiled.sha256_by_path.items():
                content = await self.sandbox.read_file(ref, path)
                if hashlib.sha256(content).hexdigest() != digest:
                    raise RuntimeError(f"frozen acceptance test changed after candidate execution: {path}")
        except Exception as exc:
            summary = f"FOMO-owned test restore failed: {redact(str(exc))[:500]}"
            gate = GateResult(
                gate="restore",
                status=GateStatus.failed,
                summary=summary,
                evidence=["restore:fomo-owned-tests"],
            )
            await self._record_project_gate(gate)
            return gate
        gate = GateResult(
            gate="restore",
            status=GateStatus.passed,
            summary="FOMO-owned acceptance tests and harness restored and verified.",
            evidence=["restore:fomo-owned-tests"],
        )
        await self._record_project_gate(gate)
        return gate

    async def _start_preview(self, ref: SandboxRef) -> tuple[GateResult, str | None]:
        base_path = self.settings.published_preview_base_path(ref.id)
        command_text = _with_preview_base_path(
            f"{_WORKSPACE_NEXT} start --hostname 0.0.0.0 --port 8080",
            base_path,
        )
        operation_id = uuid7()
        await self.repository.append_event(
            self.run_id,
            "command.started",
            payload={
                "operationId": operation_id,
                "command": command_text,
                "label": "preview",
                "stage": "verifying",
            },
            lease_token=self.lease_token,
        )
        output: list[str] = []

        async def sink(_stream: str, text: str) -> None:
            output.append(redact(text))

        start_preview = getattr(self.sandbox, "start_preview", None)
        preview: PreviewRef | None = None
        if callable(start_preview):
            try:
                preview = await start_preview(
                    ref,
                    Command(
                        command=command_text,
                        timeout_seconds=self.settings.preview_start_timeout_seconds,
                        max_output_bytes=self.settings.command_output_limit_bytes,
                        operation_id=operation_id,
                    ),
                    8080,
                    sink,
                )
            except Exception:
                preview = None
        if output:
            await self.repository.append_event(
                self.run_id,
                "command.output",
                payload={
                    "operationId": operation_id,
                    "stream": "stdout",
                    "text": "".join(output),
                    "cumulative": False,
                },
                lease_token=self.lease_token,
            )
        endpoint_url = preview.url if preview and preview.status == "ready" else None
        url = _preview_runtime_url(endpoint_url, base_path) if endpoint_url else None
        healthy = bool(url) and await self.preview_is_healthy(str(url))
        await self.repository.append_event(
            self.run_id,
            "command.completed",
            payload={"operationId": operation_id, "exitCode": 0 if healthy else 1},
            lease_token=self.lease_token,
        )
        gate = GateResult(
            gate="preview",
            status=GateStatus.passed if healthy else GateStatus.failed,
            summary=(
                "Preview health check returned 2xx."
                if healthy
                else "Preview did not become browser-reachable."
            ),
            evidence=[f"preview:{url}"] if healthy and url else [],
        )
        await self._record_project_gate(gate)
        if healthy and url:
            await self.repository.set_preview_url(
                self.run_id, url, lease_token=self.lease_token
            )
            await self.repository.append_event(
                self.run_id,
                "preview.available",
                payload={
                    "url": url,
                    "verificationStatus": "unverified",
                    "elapsedSeconds": self._elapsed_seconds(),
                    "sandboxId": ref.id,
                    "routingMode": "base_path_v1" if base_path else "host_root_v1",
                },
                lease_token=self.lease_token,
            )
            return gate, url
        return gate, None

    async def _playwright_project_gate(
        self, ref: SandboxRef, test_path: str
    ) -> GateResult:
        command = _PLAYWRIGHT.format(
            path=shlex.quote(test_path), config=shlex.quote(ACCEPTANCE_CONFIG_PATH)
        )
        command = _with_preview_base_path(
            command, self.settings.published_preview_base_path(ref.id)
        )
        result = await self.commands.run(
            ref, command, label="smoke", stage="verifying"
        )
        report = parse_playwright_json(result.stdout)
        passed = (
            not result.timed_out
            and result.exit_code == 0
            and report is not None
            and report.top_level_errors == 0
            and report.load_errors == 0
            and report.test_count == 1
            and report.status == "passed"
        )
        gate = GateResult(
            gate="smoke",
            status=GateStatus.passed if passed else GateStatus.failed,
            summary=(
                "Fixed starter harness passed."
                if passed
                else "Fixed starter harness did not prove one passing test."
            ),
            evidence=[f"command:{command}"],
        )
        await self._record_project_gate(gate)
        return gate

    async def _acceptance_gate(
        self,
        ref: SandboxRef,
        acceptance_id: str,
        test_path: str,
        test_name: str,
    ) -> GateResult:
        command = _PLAYWRIGHT.format(
            path=shlex.quote(test_path), config=shlex.quote(ACCEPTANCE_CONFIG_PATH)
        )
        command = _with_preview_base_path(
            command, self.settings.published_preview_base_path(ref.id)
        )
        result = await self.commands.run(
            ref, command, label=f"acceptance:{acceptance_id}", stage="verifying"
        )
        report = parse_playwright_json(result.stdout)
        infrastructure_failed = (
            result.timed_out
            or report is None
            or report.top_level_errors > 0
            or report.load_errors > 0
            or report.test_count != 1
            or report.title != test_name
            or report.status == "did_not_run"
            or report.status == "passed"
            and result.exit_code != 0
            or report.status == "failed"
            and result.exit_code == 0
        )
        if infrastructure_failed:
            outcome = "infrastructure_failed"
            summary = "Playwright did not produce a trustworthy one-test result."
        elif report.status == "passed":
            outcome = "passed"
            summary = "Acceptance workflow passed."
        else:
            outcome = "failed"
            summary = "Acceptance workflow assertion failed."
        diagnostic = (
            GateDiagnostic(
                message=report.assertion.message,
                locator=report.assertion.locator,
                test_name=report.assertion.test_name,
                line=report.assertion.line,
            )
            if outcome == "failed"
            and report is not None
            and report.assertion is not None
            else None
        )
        return GateResult(
            gate="acceptance_test",
            scope="acceptance",
            status=GateStatus.passed if outcome == "passed" else GateStatus.failed,
            outcome=outcome,
            summary=summary,
            acceptance_id=acceptance_id,
            test_path=test_path,
            test_name=test_name,
            exit_code=result.exit_code if outcome != "infrastructure_failed" else None,
            evidence=[f"command:{command}"],
            diagnostic=diagnostic,
        )

    async def _navigation_gate(
        self,
        ref: SandboxRef,
        navigation_id: str,
        test_path: str,
        test_name: str,
    ) -> GateResult:
        """Run one server-derived route check as a project-level hard gate."""

        command = _PLAYWRIGHT.format(
            path=shlex.quote(test_path), config=shlex.quote(ACCEPTANCE_CONFIG_PATH)
        )
        command = _with_preview_base_path(
            command, self.settings.published_preview_base_path(ref.id)
        )
        result = await self.commands.run(
            ref,
            command,
            label=f"navigation:{navigation_id}",
            stage="verifying",
        )
        report = parse_playwright_json(result.stdout)
        infrastructure_failed = (
            result.timed_out
            or report is None
            or report.top_level_errors > 0
            or report.load_errors > 0
            or report.test_count != 1
            or report.title != test_name
            or report.status == "did_not_run"
            or report.status == "passed"
            and result.exit_code != 0
            or report.status == "failed"
            and result.exit_code == 0
        )
        if infrastructure_failed:
            outcome = "infrastructure_failed"
            summary = (
                f"Navigation check {navigation_id} ({test_name}) did not produce one "
                f"trustworthy Playwright result for {test_path}."
            )
        elif report.status == "passed":
            outcome = "passed"
            summary = f"Server-owned navigation check passed: {test_name}."
        else:
            outcome = "failed"
            summary = (
                f"Navigation check {navigation_id} ({test_name}) assertion failed "
                f"in {test_path}."
            )
        diagnostic = (
            GateDiagnostic(
                message=report.assertion.message,
                locator=report.assertion.locator,
                test_name=report.assertion.test_name,
                line=report.assertion.line,
            )
            if outcome == "failed"
            and report is not None
            and report.assertion is not None
            else None
        )
        if diagnostic is not None:
            assertion = report.assertion
            detail = redact(assertion.message)[:320]
            locator = (
                f" locator={redact(assertion.locator)[:160]}"
                if assertion.locator
                else ""
            )
            line = f" line={assertion.line}" if assertion.line is not None else ""
            summary = (
                f"Navigation check {navigation_id} ({test_name}) failed in "
                f"{test_path}: "
                f"{detail}{locator}{line}"
            )[:700]
        gate = GateResult(
            gate=f"navigation:{navigation_id}",
            scope="navigation",
            status=GateStatus.passed if outcome == "passed" else GateStatus.failed,
            outcome=outcome,
            summary=summary,
            navigation_id=navigation_id,
            test_path=test_path,
            test_name=test_name,
            exit_code=result.exit_code if outcome != "infrastructure_failed" else None,
            evidence=[f"command:{command}"],
            timed_out=result.timed_out,
            diagnostic=diagnostic,
        )
        await self._record_project_gate(gate)
        return gate

    async def _record_acceptance_evidence(
        self, gates: list[GateResult], artifact_id: str
    ) -> None:
        for gate in gates:
            if (
                gate.scope != "acceptance"
                or gate.outcome not in {"passed", "failed"}
                or gate.acceptance_id is None
                or gate.test_path is None
                or gate.test_name is None
                or gate.exit_code is None
            ):
                continue
            await assert_run_active(self.repository, self.run_id, self.lease_token)
            await self.repository.record_evidence(
                self.run_id,
                gate.acceptance_id,
                "playwright_smoke",
                gate.outcome,
                json.dumps(
                    {
                        "runId": self.run_id,
                        "acceptanceId": gate.acceptance_id,
                        "testPath": gate.test_path,
                        "testName": gate.test_name,
                        "result": gate.outcome,
                        "recordedAt": utcnow().isoformat(),
                        "exitCode": gate.exit_code,
                        "artifactRef": artifact_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                artifact_id=artifact_id,
                lease_token=self.lease_token,
            )

    async def _record_project_gate(self, gate: GateResult) -> None:
        await self.repository.append_event(
            self.run_id,
            "verification.updated",
            payload={
                "scope": "project",
                "gateId": f"project:{gate.gate}",
                "name": gate.gate,
                "status": gate.status.value,
                "summary": gate.summary,
                "affectedFiles": gate.affected_files,
                **({"outcome": gate.outcome} if gate.outcome is not None else {}),
            },
            lease_token=self.lease_token,
        )

    async def preview_is_healthy(self, url: str) -> bool:
        """Point-in-time preview health check.

        Public so publication can re-check the preview immediately before
        tagging/versioning. It remains a point-in-time probe; a same-user
        TOCTOU race is not solved (public-deployment blocker).
        """
        # The synthetic origin exists only for the in-memory test provider.
        # Never trust the hostname alone: a real or compromised provider must
        # still pass an actual HTTP probe even if it returns this URL.
        parsed = urlsplit(url)
        if (
            isinstance(self.sandbox, FakeSandboxProvider)
            and parsed.scheme == "http"
            and parsed.hostname == "fake-preview.invalid"
        ):
            return True
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

    @staticmethod
    def _failure_summary(result: ExecResult) -> str:
        if result.timed_out:
            return "command timed out"
        value = redact(f"{result.stdout}\n{result.stderr}").strip().replace("\n", " ")
        return (value or "command failed")[:1000]
