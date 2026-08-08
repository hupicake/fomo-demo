"""Deterministic clean-sandbox verification and evidence persistence."""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from dataclasses import dataclass

import httpx

from fomo.agent_runtime.playwright_reporter import parse_playwright_json
from fomo.config import Settings
from fomo.ids import utcnow, uuid7
from fomo.persistence import Repository
from fomo.sandbox.base import Command, ExecResult, PreviewRef, SandboxProvider, SandboxRef
from fomo.schemas import GateResult, GateStatus

from .acceptance import ACCEPTANCE_CONFIG_PATH, CompiledAcceptance
from .contracts import AcceptanceContract
from .execution import CommandExecutor, redact

_PLAYWRIGHT = (
    "pnpm exec playwright test {path} --config={config} --project=chromium --reporter=json"
)


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    passed: bool
    gates: tuple[GateResult, ...]
    diagnostic_artifact_id: str
    preview_url: str | None

    def as_repair_context(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "previewUrlAvailable": self.preview_url is not None,
            "diagnosticArtifactId": self.diagnostic_artifact_id,
            "gates": [item.model_dump(mode="json", by_alias=True) for item in self.gates],
        }


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
    ) -> None:
        self.repository = repository
        self.sandbox = sandbox
        self.settings = settings
        self.commands = commands
        self.run_id = run_id
        self.lease_token = lease_token

    async def verify(
        self,
        ref: SandboxRef,
        contract: AcceptanceContract,
        compiled: CompiledAcceptance,
        *,
        round_number: int,
        candidate_paths: tuple[str, ...],
    ) -> VerificationOutcome:
        gates: list[GateResult] = []
        await self._reset_acceptance(contract)

        install = await self._command_gate(
            ref,
            "dependencies",
            "pnpm install --offline --frozen-lockfile",
            candidate_paths=candidate_paths,
        )
        gates.append(install)
        preview_url: str | None = None
        if install.status == GateStatus.passed:
            preview = await self._start_preview(ref)
            gates.append(preview[0])
            preview_url = preview[1]

        # Fail fast across expensive project gates. The unverified preview is
        # already visible whenever dependencies and dev-server health permit.
        if all(item.status == GateStatus.passed for item in gates):
            gates.append(
                await self._command_gate(
                    ref, "typecheck", "pnpm typecheck", candidate_paths=candidate_paths
                )
            )
        if all(item.status == GateStatus.passed for item in gates):
            gates.append(
                await self._command_gate(
                    ref, "build", "pnpm build", candidate_paths=candidate_paths
                )
            )
        if all(item.status == GateStatus.passed for item in gates):
            gates.append(
                await self._playwright_project_gate(
                    ref, "tests/harness/starter.smoke.spec.ts"
                )
            )

        if all(item.status == GateStatus.passed for item in gates):
            for criterion in contract.criteria:
                gates.append(
                    await self._acceptance_gate(
                        ref,
                        criterion.id,
                        compiled.test_path_by_acceptance_id[criterion.id],
                        compiled.test_name_by_acceptance_id[criterion.id],
                    )
                )

        passed = all(item.status == GateStatus.passed for item in gates) and len(gates) == (
            5 + len(contract.criteria)
        )
        diagnostic = {
            "round": round_number,
            "passed": passed,
            "candidatePaths": list(candidate_paths),
            "gates": [item.model_dump(mode="json", by_alias=True) for item in gates],
        }
        artifact_id = await self.repository.store_artifact(
            self.run_id,
            "diagnostic_report",
            diagnostic,
            lease_token=self.lease_token,
        )
        await self._record_acceptance_evidence(gates, artifact_id)
        if passed and preview_url:
            await self.repository.append_event(
                self.run_id,
                "preview.verified",
                payload={"url": preview_url, "verificationStatus": "verified"},
                lease_token=self.lease_token,
            )
        return VerificationOutcome(
            passed=passed,
            gates=tuple(gates),
            diagnostic_artifact_id=artifact_id,
            preview_url=preview_url,
        )

    async def _reset_acceptance(self, contract: AcceptanceContract) -> None:
        for item in contract.criteria:
            await self.repository.append_event(
                self.run_id,
                "verification.updated",
                payload={
                    "scope": "acceptance",
                    "acceptanceId": item.id,
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
        )
        await self._record_project_gate(gate_result)
        return gate_result

    async def _start_preview(self, ref: SandboxRef) -> tuple[GateResult, str | None]:
        command_text = "pnpm dev --hostname 0.0.0.0 --port 8080"
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
        url = preview.url if preview and preview.status == "ready" else None
        healthy = bool(url) and await self._preview_is_healthy(str(url))
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
                payload={"url": url, "verificationStatus": "unverified"},
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
        )

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
            },
            lease_token=self.lease_token,
        )

    async def _preview_is_healthy(self, url: str) -> bool:
        if url.startswith("http://fake-preview.invalid"):
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
        value = redact(f"{result.stdout}\n{result.stderr}").strip().replace("\n", " ")
        return (value or "command failed")[:1000]
