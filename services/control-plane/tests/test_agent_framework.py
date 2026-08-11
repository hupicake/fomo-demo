from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fomo.agent_framework import (
    AgentTransportRegistry,
    normalize_agent_framework,
    resolve_run_agent_framework,
)
from fomo.direct_pi import DirectPiOrchestrator
from fomo.direct_pi.session import DirectPiSession
from fomo.fomo_pi_ds import RunVirtualKey
from fomo.runtime_contract import resolve_runtime_contract
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.schemas import RunStatus
from fomo.worker.runner import WorkerRunner


class _FrameworkRepository:
    def __init__(self, framework: str) -> None:
        self.framework = framework

    async def get_run_agent_framework(self, _run_id: str) -> str:
        return self.framework


class _TerminalFrameworkRepository(_FrameworkRepository):
    def __init__(self, framework: str) -> None:
        super().__init__(framework)
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.terminal: tuple[str, RunStatus, str | None] | None = None

    async def append_event(
        self,
        run_id: str,
        kind: str,
        *,
        payload: dict[str, object],
        lease_token: str,
    ) -> None:
        self.events.append((run_id, kind, payload))

    async def mark_terminal(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error_code: str | None = None,
        summary: str | None = None,
        lease_token: str,
    ) -> None:
        self.terminal = (run_id, status, error_code)


class _DispatchProbe(DirectPiOrchestrator):
    selected: tuple[object, str] | None = None

    async def _run_goal_graph(
        self,
        _run_id: str,
        *,
        transport,
        agent_framework: str,
        lease_token: str | None = None,
    ) -> None:
        self.selected = (transport, agent_framework)


class _WorkerRepository(_FrameworkRepository):
    def __init__(self, framework: str) -> None:
        super().__init__(framework)
        self.claimed = False
        self.terminal: tuple[str, RunStatus, str | None] | None = None

    async def recover_expired_running_runs(self):
        return []

    async def list_sandbox_cleanup_targets(self):
        return []

    async def list_terminal_sandbox_cleanup_targets(self):
        return []

    async def claim_next_run(self, _worker_id: str, _lease_seconds: int):
        if self.claimed:
            return None
        self.claimed = True
        return SimpleNamespace(id="run-opencode", lease_owner="lease-token")

    async def mark_terminal(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error_code: str | None = None,
        summary: str | None = None,
        lease_token: str,
    ) -> None:
        self.terminal = (run_id, status, error_code)


class _TaskNameProbe:
    def __init__(self) -> None:
        self.task_name: str | None = None

    async def run(self, _run_id: str, *, lease_token: str | None = None) -> None:
        self.task_name = asyncio.current_task().get_name()  # type: ignore[union-attr]


class _ActiveSessionRepository:
    async def is_cancel_requested(self, _run_id: str) -> bool:
        return False

    async def is_active_lease(self, _run_id: str, _lease_token: str) -> bool:
        return True


class _RequestProbeTransport:
    def __init__(self) -> None:
        self.request = None

    async def run(self, _ref, invocation, **_kwargs):
        self.request = invocation.request
        raise RuntimeError("request captured")


def test_agent_transport_registry_is_closed_and_framework_specific() -> None:
    pi_transport = object()
    opencode_transport = object()
    registry = AgentTransportRegistry(
        {"pi": pi_transport, "opencode": opencode_transport}
    )

    assert normalize_agent_framework(" OpenCode ") == "opencode"
    assert registry.require("pi") is pi_transport
    assert registry.require("opencode") is opencode_transport
    with pytest.raises(ValueError, match="pi, opencode, or codex"):
        normalize_agent_framework("unknown")


@pytest.mark.asyncio
async def test_orchestrator_reuses_one_goal_graph_with_the_selected_transport(settings) -> None:
    pi_transport = object()
    opencode_transport = object()
    repository = _FrameworkRepository("opencode")
    orchestrator = _DispatchProbe(
        repository,  # type: ignore[arg-type]
        FakeSandboxProvider(),
        settings,
        object(),  # type: ignore[arg-type]
        AgentTransportRegistry(
            {"pi": pi_transport, "opencode": opencode_transport}
        ),
    )

    await orchestrator.run("run-opencode", lease_token="lease-token")

    assert orchestrator.selected == (opencode_transport, "opencode")


@pytest.mark.asyncio
async def test_disabled_framework_reaches_a_safe_terminal_boundary(settings) -> None:
    repository = _TerminalFrameworkRepository("opencode")
    orchestrator = DirectPiOrchestrator(
        repository,  # type: ignore[arg-type]
        FakeSandboxProvider(),
        settings,
        object(),  # type: ignore[arg-type]
        AgentTransportRegistry.pi_only(object()),  # type: ignore[arg-type]
    )

    await orchestrator.run("run-opencode", lease_token="lease-token")

    assert repository.terminal == (
        "run-opencode",
        RunStatus.failed,
        "coding_agent_failed",
    )
    assert repository.events[0][2]["reason"] == "agent_framework_unavailable"


@pytest.mark.asyncio
async def test_worker_task_name_exposes_the_frozen_framework(settings) -> None:
    repository = _WorkerRepository("opencode")
    orchestrator = _TaskNameProbe()
    worker = WorkerRunner(
        repository,  # type: ignore[arg-type]
        settings,
        sandbox=FakeSandboxProvider(),
        direct_orchestrator=orchestrator,
        worker_id="dispatch-worker",
    )

    assert await worker.run_once()
    assert orchestrator.task_name == "fomo-opencode-agent:run-opencode"


@pytest.mark.asyncio
async def test_opencode_turn_disables_unsupported_user_input_tool(settings) -> None:
    transport = _RequestProbeTransport()
    contract = resolve_runtime_contract("deepseek-flash", "off")
    session = DirectPiSession(
        _ActiveSessionRepository(),  # type: ignore[arg-type]
        transport,  # type: ignore[arg-type]
        settings,
        RunVirtualKey(
            run_id="run-opencode",
            key_alias="fomo-run-opencode",
            duration_seconds=300,
            secret="sk-test-run-key",
        ),
        runtime_contract=contract,
        agent_framework="opencode",
        run_id="run-opencode",
        lease_token="lease-token",
        started_at=0.0,
    )

    with pytest.raises(RuntimeError, match="request captured"):
        await session.invoke(
            SimpleNamespace(id="sandbox", project_id="project"),  # type: ignore[arg-type]
            "Build the interface",
            stage="building",
        )

    assert transport.request is not None
    assert transport.request.user_input_enabled is False
    assert transport.request.thinking == "off"


@pytest.mark.asyncio
async def test_codex_turn_keeps_resume_independent_from_user_input(settings) -> None:
    transport = _RequestProbeTransport()
    contract = resolve_runtime_contract("gpt-5.6", "xhigh")
    session = DirectPiSession(
        _ActiveSessionRepository(),  # type: ignore[arg-type]
        transport,  # type: ignore[arg-type]
        settings,
        RunVirtualKey(
            run_id="run-codex",
            key_alias="fomo-run-codex",
            duration_seconds=300,
            secret="sk-test-run-key",
            model_aliases=(contract.litellm_alias,),
        ),
        runtime_contract=contract,
        agent_framework="codex",
        run_id="run-codex",
        lease_token="lease-token",
        started_at=0.0,
    )

    with pytest.raises(RuntimeError, match="request captured"):
        await session.invoke(
            SimpleNamespace(id="sandbox", project_id="project"),  # type: ignore[arg-type]
            "Continue the implementation",
            stage="building",
            require_existing_session=True,
        )

    assert transport.request is not None
    assert transport.request.user_input_enabled is False
    assert transport.request.require_resume is True
    assert transport.request.model == contract.model_ref
    assert transport.request.thinking == "xhigh"


@pytest.mark.asyncio
async def test_framework_resolution_uses_the_authoritative_repository_value() -> None:
    assert await resolve_run_agent_framework(
        _FrameworkRepository("opencode"),
        "run-opencode",
    ) == "opencode"
