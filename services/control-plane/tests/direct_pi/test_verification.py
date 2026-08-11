"""Verifier contract tests: fixed runner invocations, gate fail-closed
classification, FOMO-owned restore/hash protection, and preview health."""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import fomo.direct_pi.verification as verification_module
from fomo.direct_pi.acceptance import (
    ACCEPTANCE_CONFIG_PATH,
    FOMO_HARNESS_PATH,
    FOMO_PLAYWRIGHT_TEST_MODULE,
    compile_acceptance,
    compile_acceptance_suite,
)
from fomo.direct_pi.contracts import AcceptanceContract
from fomo.direct_pi.execution import CommandExecutor, DirectPiRunCancelled
from fomo.direct_pi.goal_manager import (
    RegressionSuite,
    RuntimeValidationMode,
    RuntimeValidationReason,
)
from fomo.direct_pi.goalgraph import (
    NavigationRoute,
    NavigationVerificationSuite,
    navigation_test_ids,
    scope_acceptance_contract,
)
from fomo.direct_pi.verification import VerificationOutcome, Verifier
from fomo.direct_pi.workspace import (
    FOMO_RUNNER_BIN,
    FOMO_RUNNER_NODE,
    FOMO_RUNNER_PATH,
    fomo_runner_command,
)
from fomo.sandbox.base import ExecResult
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.schemas import GateStatus
from tests.helpers import create_user_session

_HARNESS_PATH = FOMO_HARNESS_PATH
_RUNNER_PROBE = (
    f"test -x {FOMO_RUNNER_NODE} "
    f"&& test -x {FOMO_RUNNER_BIN}/tsc "
    f"&& test -x {FOMO_RUNNER_BIN}/playwright "
    f"&& test -r {FOMO_PLAYWRIGHT_TEST_MODULE} "
    "&& test -r /opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/next/package.json"
)
_NEXT_RUNTIME_PROBE = verification_module._NEXT_RUNTIME_PROBE
_with_preview_base_path = verification_module._with_preview_base_path
_WORKSPACE_NEXT = (
    "env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
    "/usr/local/bin/node /workspace/node_modules/next/dist/bin/next"
)
_WORKSPACE_NEXT_BUILD = f"{_WORKSPACE_NEXT} build"
_WORKSPACE_NEXT_START = f"{_WORKSPACE_NEXT} start --hostname 0.0.0.0 --port 8080"


def _contract() -> AcceptanceContract:
    return AcceptanceContract.model_validate(
        {
            "criteria": [
                {
                    "id": "AC-1",
                    "title": "Search books",
                    "priority": "must",
                    "given": "The library is open",
                    "when": "A search is submitted",
                    "then": "Matches appear",
                }
            ],
            "tests": [
                {
                    "id": "search-books",
                    "acceptanceId": "AC-1",
                    "title": "searches books",
                    "actions": [{"kind": "goto", "path": "/"}],
                    "assertions": [
                        {
                            "kind": "visible",
                            "target": {"by": "role", "value": "heading", "name": "Library"},
                        }
                    ],
                }
            ],
        }
    )


def _playwright_command(path: str, *, base_path: str | None = None) -> str:
    command = fomo_runner_command(
        bin_name="playwright",
        args=f"test {path} --config={ACCEPTANCE_CONFIG_PATH} --project=chromium --reporter=json",
    )
    return _with_preview_base_path(command, base_path)


def _playwright_report(title: str) -> str:
    import json

    return json.dumps(
        {
            "errors": [],
            "suites": [
                {
                    "specs": [
                        {
                            "title": title,
                            "errors": [],
                            "tests": [
                                {"status": "expected", "results": [{"status": "passed"}]}
                            ],
                        }
                    ]
                }
            ],
        }
    )


def _playwright_failure_report(title: str) -> str:
    import json

    return json.dumps(
        {
            "errors": [],
            "suites": [
                {
                    "specs": [
                        {
                            "title": title,
                            "errors": [],
                            "tests": [
                                {
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "error": {
                                                "message": (
                                                    "Error: \u001b[31mexpect(locator).toBeVisible()\u001b[39m failed\n"
                                                    "Locator: getByText('张三', { exact: true }).first()\n"
                                                    "Expected: visible\n"
                                                    "PASSWORD=artifact-secret\n"
                                                    "Call log:\n"
                                                    + "x" * 20_000
                                                ),
                                                "stack": "trace-secret-must-not-persist",
                                                "location": {
                                                    "file": "/workspace/tests/fomo-acceptance/private.spec.ts",
                                                    "line": 10,
                                                    "column": 89,
                                                },
                                            },
                                            "attachments": [
                                                {
                                                    "name": "trace",
                                                    "body": "A" * 20_000,
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ],
        }
    )


def _playwright_timeout_report(title: str) -> str:
    import json

    return json.dumps(
        {
            "errors": [],
            "suites": [
                {
                    "specs": [
                        {
                            "title": title,
                            "errors": [],
                            "tests": [
                                {
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "timedOut",
                                            "error": {
                                                "message": (
                                                    "TimeoutError: locator.click: Timeout 5000ms exceeded.\n"
                                                    "Call log:\n"
                                                    "  - waiting for getByRole('button', { name: 'Increment' })\n"
                                                    "TOKEN=timeout-diagnostic-secret"
                                                ),
                                                "stack": "raw-timeout-stack-must-not-persist",
                                                "location": {
                                                    "file": "/workspace/tests/fomo-acceptance/private.spec.ts",
                                                    "line": 14,
                                                    "column": 9,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ],
        }
    )


def _playwright_results(
    *, goal_id: str | None = None, base_path: str | None = None
) -> dict[str, ExecResult]:
    acceptance_path = "tests/fomo-acceptance/search-books.smoke.spec.ts"
    if goal_id is not None:
        acceptance_path = f"tests/fomo-acceptance/{goal_id}/search-books.smoke.spec.ts"
    return {
        _playwright_command(_HARNESS_PATH, base_path=base_path): ExecResult(
            0, _playwright_report("starter renders a stable application shell"), ""
        ),
        _playwright_command(acceptance_path, base_path=base_path): ExecResult(
            0, _playwright_report("searches books"), ""
        ),
    }


def _regression_suite(mode: RuntimeValidationMode) -> RegressionSuite:
    scoped = scope_acceptance_contract("G-1", _contract())
    reason = (
        RuntimeValidationReason.GOAL_FOCUSED
        if mode is RuntimeValidationMode.FOCUSED
        else RuntimeValidationReason.FINAL_GOAL
    )
    return RegressionSuite(
        claimed_goal_id="G-1",
        goal_ids=("G-1",),
        contracts=(scoped,),
        mode=mode,
        reason=reason,
    )


def test_fixed_runner_contract_pins_absolute_wrappers_and_trusted_path() -> None:
    assert FOMO_RUNNER_NODE == "/usr/local/bin/node"
    assert FOMO_RUNNER_PATH == (
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    assert FOMO_RUNNER_BIN == (
        "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/.bin"
    )
    assert fomo_runner_command(bin_name="tsc", args="--noEmit") == (
        "env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
        "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/.bin/tsc --noEmit"
    )

    playwright = fomo_runner_command(
        bin_name="playwright",
        args="test {path} --config={config} --project=chromium --reporter=json",
    )
    assert playwright == (
        "env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
        "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/.bin/playwright "
        "test {path} --config={config} --project=chromium --reporter=json"
    )
    assert f"{FOMO_RUNNER_NODE} {FOMO_RUNNER_BIN}/playwright" not in playwright
    assert verification_module._RUNNER_PROBE == _RUNNER_PROBE
    assert verification_module._WORKSPACE_NEXT == _WORKSPACE_NEXT
    assert _NEXT_RUNTIME_PROBE.startswith(
        "env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
        "/usr/local/bin/node -e "
    )
    assert "/workspace/node_modules/next/package.json" in _NEXT_RUNTIME_PROBE
    assert "/workspace/node_modules/next/dist/bin/next" in _NEXT_RUNTIME_PROBE
    assert (
        "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/next/package.json"
        in _NEXT_RUNTIME_PROBE
    )
    for command in (_WORKSPACE_NEXT_BUILD, _WORKSPACE_NEXT_START):
        assert "pnpm" not in command
        assert "npx" not in command
        assert "node_modules/.bin" not in command
        assert "/workspace/node_modules/next/dist/bin/next" in command
        assert (
            "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/next/dist/bin/next"
            not in command
        )


def test_preview_base_path_wraps_only_path_mode_commands() -> None:
    assert _with_preview_base_path(_WORKSPACE_NEXT_BUILD, None) == _WORKSPACE_NEXT_BUILD
    assert _with_preview_base_path(
        _WORKSPACE_NEXT_BUILD, "/preview/sandbox-id"
    ) == f"FOMO_PREVIEW_BASE_PATH=/preview/sandbox-id {_WORKSPACE_NEXT_BUILD}"


async def _run_context(repository, message_id: str = "verify-test"):
    session = await create_user_session(repository)
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, message_id, "Build a library manager."
    )
    claimed = await repository.claim_next_run(f"verify-{message_id}", 60)
    assert claimed is not None and claimed.lease_owner
    return project, run, claimed.lease_owner


def _verifier(repository, sandbox, settings, run_id: str, lease_token: str) -> Verifier:
    commands = CommandExecutor(
        repository,
        sandbox,
        settings,
        run_id=run_id,
        lease_token=lease_token,
    )
    return Verifier(
        repository,
        sandbox,
        settings,
        commands,
        run_id=run_id,
        lease_token=lease_token,
        started_at=time.monotonic(),
    )


@pytest.mark.asyncio
async def test_verify_full_gates_use_fixed_runner_and_never_scripts(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "gates-pass")
    sandbox = FakeSandboxProvider()
    ref = await sandbox.create(_project.id)
    preview_base_path = f"/preview/{ref.id}"
    sandbox.command_results = _playwright_results(base_path=preview_base_path)
    path_settings = replace(
        settings,
        public_preview_base_url="http://localhost:3000/preview",
    )
    verifier = _verifier(repository, sandbox, path_settings, run.id, lease)
    contract = _contract()
    compiled = compile_acceptance(contract)

    outcome = await verifier.verify(
        ref,
        contract,
        compiled,
        round_number=0,
        candidate_paths=("lib/domain/books.ts",),
    )

    assert outcome.passed
    assert outcome.preview_url == (
        f"http://fake-preview.invalid:8080{preview_base_path}"
    )
    assert outcome.preview_elapsed_seconds is not None
    assert not outcome.has_infrastructure_failure
    recorded = sandbox.sandboxes[ref.id].commands
    assert _RUNNER_PROBE in recorded
    assert _NEXT_RUNTIME_PROBE in recorded
    assert fomo_runner_command(bin_name="tsc", args="--noEmit") in recorded
    preview_build = _with_preview_base_path(_WORKSPACE_NEXT_BUILD, preview_base_path)
    preview_start = _with_preview_base_path(_WORKSPACE_NEXT_START, preview_base_path)
    assert preview_build in recorded
    assert preview_start in recorded
    assert not any(f"{FOMO_RUNNER_BIN}/next" in command for command in recorded)
    # No model-editable scripts are ever invoked; the only .bin entries come
    # from the fixed root-owned runtime cache path.
    assert not any(
        command.startswith("pnpm typecheck")
        or command.startswith("pnpm build")
        or command.startswith("pnpm dev")
        for command in recorded
    )
    gate_names = [gate.gate for gate in outcome.gates]
    assert gate_names == [
        "runner",
        "dependencies",
        "next_runtime",
        "typecheck",
        "build",
        "preview",
        "restore",
        "smoke",
        "acceptance_test",
    ]
    assert recorded[:6] == [
        _RUNNER_PROBE,
        "pnpm install --offline --frozen-lockfile --ignore-scripts",
        _NEXT_RUNTIME_PROBE,
        fomo_runner_command(bin_name="tsc", args="--noEmit"),
        preview_build,
        preview_start,
    ]
    preview_available = next(
        event for event in await repository.list_events(run.id)
        if event.kind == "preview.available"
    )
    assert preview_available.payload["sandboxId"] == ref.id
    assert preview_available.payload["routingMode"] == "base_path_v1"
    assert preview_available.payload["url"] == outcome.preview_url
    assert _playwright_command(_HARNESS_PATH, base_path=preview_base_path) in recorded
    assert _playwright_command(
        "tests/fomo-acceptance/search-books.smoke.spec.ts",
        base_path=preview_base_path,
    ) in recorded


@pytest.mark.asyncio
async def test_failed_acceptance_persists_safe_assertion_diagnostic(
    repository, settings
) -> None:
    project, run, lease = await _run_context(repository, "assertion-diagnostic")
    contract = _contract()
    results = _playwright_results()
    acceptance_command = _playwright_command(
        "tests/fomo-acceptance/search-books.smoke.spec.ts"
    )
    results[acceptance_command] = ExecResult(
        1,
        _playwright_failure_report("searches books"),
        "",
    )
    sandbox = FakeSandboxProvider(results)
    ref = await sandbox.create(project.id)

    outcome = await _verifier(repository, sandbox, settings, run.id, lease).verify(
        ref,
        contract,
        compile_acceptance(contract),
        round_number=0,
        candidate_paths=("app/page.tsx",),
    )

    assert not outcome.passed
    failed = next(gate for gate in outcome.gates if gate.outcome == "failed")
    assert failed.diagnostic is not None
    assert failed.diagnostic.locator == "getByText('张三', { exact: true }).first()"
    assert failed.diagnostic.test_name == "searches books"
    assert failed.diagnostic.line == 10
    assert "[REDACTED]" in failed.diagnostic.message
    artifact = await repository.get_latest_artifact(run.id, "diagnostic_report")
    assert artifact is not None
    serialized = __import__("json").dumps(artifact, ensure_ascii=False)
    assert "张三" in serialized
    assert '"line": 10' in serialized
    for forbidden in (
        "artifact-secret",
        "trace-secret-must-not-persist",
        "/workspace/tests/fomo-acceptance/private.spec.ts",
        "A" * 1_000,
        "x" * 1_000,
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_outcome", "expected_infrastructure_failure"),
    [
        (
            ExecResult(1, _playwright_timeout_report("searches books"), ""),
            "failed",
            False,
        ),
        (
            ExecResult(
                1,
                '{"errors":[{"message":"browser startup failed"}],"suites":[]}',
                "",
            ),
            "infrastructure_failed",
            True,
        ),
        (ExecResult(1, "not-json", ""), "infrastructure_failed", True),
        (
            ExecResult(
                -1,
                _playwright_timeout_report("searches books"),
                "",
                timed_out=True,
            ),
            "infrastructure_failed",
            True,
        ),
    ],
    ids=(
        "locator-timeout-is-repairable",
        "browser-startup-error-is-infrastructure",
        "malformed-report-is-infrastructure",
        "outer-command-timeout-is-infrastructure",
    ),
)
async def test_acceptance_timeout_classification_replays_trusted_runner_results(
    repository,
    settings,
    result: ExecResult,
    expected_outcome: str,
    expected_infrastructure_failure: bool,
) -> None:
    project, run, lease = await _run_context(
        repository, f"timeout-classification-{expected_outcome}-{result.timed_out}"
    )
    contract = _contract()
    results = _playwright_results()
    acceptance_command = _playwright_command(
        "tests/fomo-acceptance/search-books.smoke.spec.ts"
    )
    results[acceptance_command] = result
    sandbox = FakeSandboxProvider(results)
    ref = await sandbox.create(project.id)

    outcome = await _verifier(repository, sandbox, settings, run.id, lease).verify(
        ref,
        contract,
        compile_acceptance(contract),
        round_number=0,
        candidate_paths=("app/page.tsx",),
    )

    acceptance_gate = next(
        gate for gate in outcome.gates if gate.gate == "acceptance_test"
    )
    assert acceptance_gate.outcome == expected_outcome
    assert outcome.has_infrastructure_failure is expected_infrastructure_failure

    artifact = await repository.get_latest_artifact(run.id, "diagnostic_report")
    assert artifact is not None
    serialized = __import__("json").dumps(artifact, ensure_ascii=False)
    for forbidden in (
        "timeout-diagnostic-secret",
        "raw-timeout-stack-must-not-persist",
        "/workspace/tests/fomo-acceptance/private.spec.ts",
        "browser startup failed",
    ):
        assert forbidden not in serialized

    if expected_outcome == "failed":
        assert acceptance_gate.diagnostic is not None
        assert acceptance_gate.diagnostic.locator == (
            "getByRole('button', { name: 'Increment' })"
        )
        assert acceptance_gate.diagnostic.line == 14
        assert acceptance_gate.exit_code == 1
    else:
        assert acceptance_gate.diagnostic is None
        assert acceptance_gate.exit_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_factory", "expected_outcome", "expected_infrastructure"),
    [
        (
            lambda title: ExecResult(1, _playwright_timeout_report(title), ""),
            "failed",
            False,
        ),
        (
            lambda _title: ExecResult(1, "not-json", ""),
            "infrastructure_failed",
            True,
        ),
    ],
    ids=("locator-timeout-is-repairable", "malformed-report-is-infrastructure"),
)
async def test_navigation_gate_preserves_assertion_vs_infrastructure_failure_domain(
    repository,
    settings,
    result_factory,
    expected_outcome: str,
    expected_infrastructure: bool,
) -> None:
    project, run, lease = await _run_context(
        repository,
        f"navigation-classification-{expected_outcome}",
    )
    suite = NavigationVerificationSuite(
        version=1,
        routes=(
            NavigationRoute(
                path="/",
                title="Home",
                owningGoalId="G-1",
                deepLinkable=True,
            ),
        ),
        mode="focused",
    )
    navigation_id = navigation_test_ids(suite)[0]
    test_path = (
        f"tests/fomo-acceptance/navigation-v1/{navigation_id}.smoke.spec.ts"
    )
    test_name = "FOMO navigation direct load: Home"
    command = _playwright_command(test_path)
    sandbox = FakeSandboxProvider(
        {command: result_factory(test_name)}
    )
    ref = await sandbox.create(project.id)

    gate = await _verifier(
        repository,
        sandbox,
        settings,
        run.id,
        lease,
    )._navigation_gate(ref, navigation_id, test_path, test_name)
    outcome = VerificationOutcome(
        passed=False,
        gates=(gate,),
        diagnostic_artifact_id="diagnostic-artifact",
        preview_url=None,
    )

    assert gate.scope == "navigation"
    assert gate.navigation_id == navigation_id
    assert gate.outcome == expected_outcome
    assert outcome.has_infrastructure_failure is expected_infrastructure
    if expected_outcome == "failed":
        assert gate.diagnostic is not None
        assert gate.diagnostic.locator == (
            "getByRole('button', { name: 'Increment' })"
        )
        assert "timeout-diagnostic-secret" not in gate.summary
    else:
        assert gate.diagnostic is None
        assert gate.exit_code is None


@pytest.mark.asyncio
async def test_cancel_after_last_gate_writes_no_passed_diagnostic_or_evidence(
    repository, settings, monkeypatch
) -> None:
    _project, run, lease = await _run_context(repository, "cancel-before-diagnostic")
    sandbox = FakeSandboxProvider(_playwright_results())
    ref = await sandbox.create(_project.id)
    verifier = _verifier(repository, sandbox, settings, run.id, lease)
    contract = _contract()
    original = verifier._acceptance_gate

    async def cancel_after_gate(*args, **kwargs):
        gate = await original(*args, **kwargs)
        await repository.request_cancel(run.id)
        return gate

    monkeypatch.setattr(verifier, "_acceptance_gate", cancel_after_gate)

    with pytest.raises(DirectPiRunCancelled):
        await verifier.verify(
            ref,
            contract,
            compile_acceptance(contract),
            round_number=0,
            candidate_paths=(),
        )

    assert await repository.get_latest_artifact(run.id, "diagnostic_report") is None
    events = await repository.list_events(run.id)
    assert not any(
        event.kind == "verification.updated"
        and event.payload.get("scope") == "acceptance"
        and event.payload.get("status") == "passed"
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_gates"),
    [
        (
            RuntimeValidationMode.FOCUSED,
            [
                "runner",
                "dependencies",
                "next_runtime",
                "typecheck",
                "build",
                "preview",
                "restore",
                "smoke",
                "acceptance_test",
            ],
        ),
        (
            RuntimeValidationMode.FULL,
            [
                "runner",
                "dependencies",
                "next_runtime",
                "typecheck",
                "build",
                "preview",
                "restore",
                "smoke",
                "acceptance_test",
            ],
        ),
    ],
)
async def test_regression_validation_modes_always_build_before_preview(
    repository,
    settings,
    mode: RuntimeValidationMode,
    expected_gates: list[str],
) -> None:
    project, run, lease = await _run_context(repository, f"regression-{mode.value}")
    sandbox = FakeSandboxProvider(_playwright_results(goal_id="G-1"))
    ref = await sandbox.create(project.id)
    suite = _regression_suite(mode)
    compiled = compile_acceptance_suite(suite.contracts)

    outcome = await _verifier(repository, sandbox, settings, run.id, lease).verify_regression(
        ref,
        suite,
        compiled,
        round_number=0,
        candidate_paths=("app/page.tsx",),
    )

    assert outcome.passed
    assert outcome.validation_mode is mode
    assert outcome.validation_reason is suite.reason
    assert [gate.gate for gate in outcome.gates] == expected_gates
    build_command = _WORKSPACE_NEXT_BUILD
    commands = sandbox.sandboxes[ref.id].commands
    assert build_command in commands
    preview_command = _WORKSPACE_NEXT_START
    expected_prefix = [
        _RUNNER_PROBE,
        "pnpm install --offline --frozen-lockfile --ignore-scripts",
        _NEXT_RUNTIME_PROBE,
        fomo_runner_command(bin_name="tsc", args="--noEmit"),
        build_command,
    ]
    expected_prefix.append(preview_command)
    assert commands[: len(expected_prefix)] == expected_prefix


@pytest.mark.asyncio
async def test_runner_probe_failure_is_infrastructure_and_skips_gates(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "runner-probe")
    sandbox = FakeSandboxProvider({_RUNNER_PROBE: ExecResult(1, "", "missing runner")})
    ref = await sandbox.create(_project.id)
    verifier = _verifier(repository, sandbox, settings, run.id, lease)
    contract = _contract()

    outcome = await verifier.verify(
        ref, contract, compile_acceptance(contract), round_number=0, candidate_paths=()
    )

    assert not outcome.passed
    assert outcome.has_infrastructure_failure
    assert [gate.gate for gate in outcome.gates] == ["runner"]
    assert outcome.preview_url is None


@pytest.mark.asyncio
async def test_dependency_install_failure_is_repairable_unless_timed_out(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "deps-repairable")
    contract = _contract()
    compiled = compile_acceptance(contract)

    sandbox = FakeSandboxProvider(
        {
            "pnpm install --offline --frozen-lockfile --ignore-scripts": ExecResult(
                1, "", "store missing package"
            )
        }
    )
    ref = await sandbox.create(_project.id)
    outcome = await _verifier(repository, sandbox, settings, run.id, lease).verify(
        ref, contract, compiled, round_number=0, candidate_paths=()
    )
    assert not outcome.passed
    assert not outcome.has_infrastructure_failure
    assert outcome.preview_url is None

    sandbox = FakeSandboxProvider(
        {
            "pnpm install --offline --frozen-lockfile --ignore-scripts": ExecResult(
                -1, "", "", timed_out=True
            )
        }
    )
    ref = await sandbox.create(_project.id)
    outcome = await _verifier(repository, sandbox, settings, run.id, lease).verify(
        ref, contract, compiled, round_number=0, candidate_paths=()
    )
    assert not outcome.passed
    assert outcome.has_infrastructure_failure


@pytest.mark.asyncio
async def test_workspace_next_version_mismatch_fails_closed_before_code_execution(
    repository, settings
) -> None:
    project, run, lease = await _run_context(repository, "next-runtime-mismatch")
    sandbox = FakeSandboxProvider(
        {
            _NEXT_RUNTIME_PROBE: ExecResult(
                1, "", "workspace Next runtime is missing or version-mismatched"
            )
        }
    )
    ref = await sandbox.create(project.id)
    contract = _contract()

    outcome = await _verifier(repository, sandbox, settings, run.id, lease).verify(
        ref,
        contract,
        compile_acceptance(contract),
        round_number=0,
        candidate_paths=("package.json", "pnpm-lock.yaml"),
    )

    assert not outcome.passed
    assert outcome.preview_url is None
    assert [gate.gate for gate in outcome.gates] == [
        "runner",
        "dependencies",
        "next_runtime",
    ]
    commands = sandbox.sandboxes[ref.id].commands
    assert fomo_runner_command(bin_name="tsc", args="--noEmit") not in commands
    assert _WORKSPACE_NEXT_BUILD not in commands
    assert _WORKSPACE_NEXT_START not in commands


@pytest.mark.asyncio
async def test_restore_reinjects_acceptance_and_guards_trusted_harness(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "restore")
    contract = _contract()
    compiled = compile_acceptance(contract)

    # The sandbox starts WITHOUT acceptance files; restore must re-inject them
    # before Playwright and leave hashes intact.
    sandbox = FakeSandboxProvider(_playwright_results())
    ref = await sandbox.create(_project.id)
    outcome = await _verifier(repository, sandbox, settings, run.id, lease).verify(
        ref, contract, compiled, round_number=0, candidate_paths=()
    )
    assert outcome.passed
    for path, digest in compiled.sha256_by_path.items():
        content = await sandbox.read_file(ref, path)
        assert __import__("hashlib").sha256(content).hexdigest() == digest

    # A compiled set without the authoritative harness fails closed.
    missing_harness = replace(
        compiled,
        changes=tuple(item for item in compiled.changes if item.path != _HARNESS_PATH),
        sha256_by_path={
            path: digest
            for path, digest in compiled.sha256_by_path.items()
            if path != _HARNESS_PATH
        },
    )
    sandbox = FakeSandboxProvider(_playwright_results())
    ref = await sandbox.create(_project.id)
    outcome = await _verifier(repository, sandbox, settings, run.id, lease).verify(
        ref, contract, missing_harness, round_number=0, candidate_paths=()
    )
    assert not outcome.passed
    assert outcome.has_infrastructure_failure
    restore_gate = next(gate for gate in outcome.gates if gate.gate == "restore")
    assert restore_gate.status == GateStatus.failed


@pytest.mark.asyncio
async def test_restore_hash_mismatch_fails_closed_before_playwright(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "restore-hash-mismatch")
    contract = _contract()
    compiled = compile_acceptance(contract)

    class _TamperingSandbox(FakeSandboxProvider):
        async def apply_changes(self, ref, changes):
            await super().apply_changes(ref, changes)
            for change in changes:
                if change.path.startswith("tests/fomo-acceptance/"):
                    self.sandboxes[ref.id].files[change.path] += b"\n// tampered"

    sandbox = _TamperingSandbox(_playwright_results())
    ref = await sandbox.create(_project.id)
    outcome = await _verifier(repository, sandbox, settings, run.id, lease).verify(
        ref, contract, compiled, round_number=0, candidate_paths=()
    )

    assert not outcome.passed
    assert outcome.has_infrastructure_failure
    restore_gate = next(gate for gate in outcome.gates if gate.gate == "restore")
    assert restore_gate.status == GateStatus.failed
    assert "changed after candidate execution" in restore_gate.summary
    assert _playwright_command(_HARNESS_PATH) not in sandbox.sandboxes[ref.id].commands


@pytest.mark.asyncio
async def test_preview_is_healthy_shortcuts_fake_and_fails_closed_on_dead_port(
    repository, settings
) -> None:
    _project, run, lease = await _run_context(repository, "preview-health")
    sandbox = FakeSandboxProvider()
    verifier = _verifier(
        repository,
        sandbox,
        replace(settings, preview_start_timeout_seconds=1),
        run.id,
        lease,
    )

    assert await verifier.preview_is_healthy("http://fake-preview.invalid:8080")
    # Local closed port: connection refused, no external network involved.
    assert not await verifier.preview_is_healthy("http://127.0.0.1:9")


@pytest.mark.asyncio
async def test_non_fake_provider_must_probe_the_synthetic_preview_host(
    repository, settings, monkeypatch
) -> None:
    _project, run, lease = await _run_context(repository, "preview-non-fake")
    requested_urls: list[str] = []

    class _ProbingClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            return None

        async def get(self, url: str):
            requested_urls.append(url)
            return SimpleNamespace(status_code=204)

    monkeypatch.setattr(verification_module.httpx, "AsyncClient", _ProbingClient)
    non_fake_provider = SimpleNamespace()
    verifier = _verifier(
        repository,
        non_fake_provider,
        settings,
        run.id,
        lease,
    )
    url = "http://fake-preview.invalid:8080"

    assert await verifier.preview_is_healthy(url)
    assert requested_urls == [url]
