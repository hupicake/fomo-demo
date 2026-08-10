from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from fomo.fomo_pi_ds import (
    FOMO_PI_DEFAULT_PREFLIGHT_ALIASES,
    LiteLLMRunKeyClient,
    RunVirtualKey,
)
from fomo.runtime_preflight import DirectPiRuntimePreflight, RuntimePreflightError
from fomo.sandbox import SandboxRef


class _Sandbox:
    def __init__(self, *, create_error: Exception | None = None) -> None:
        self.create_error = create_error
        self.kill_error: Exception | None = None
        self.events: list[str] = []
        self.lifetimes: list[int] = []
        self.kill_started = asyncio.Event()
        self.kill_release: asyncio.Event | None = None

    async def create(
        self,
        project_id: str,
        _source: Any = None,
        *,
        lifetime_seconds: int | None = None,
    ) -> SandboxRef:
        self.events.append("create")
        assert project_id.startswith("runtime-preflight-")
        assert lifetime_seconds is not None
        self.lifetimes.append(lifetime_seconds)
        if self.create_error is not None:
            raise self.create_error
        return SandboxRef(id="sandbox-preflight-1", project_id=project_id)

    async def kill(self, ref: SandboxRef) -> None:
        self.events.append("kill")
        self.kill_started.set()
        assert ref.id == "sandbox-preflight-1"
        if self.kill_release is not None:
            await self.kill_release.wait()
        if self.kill_error is not None:
            raise self.kill_error


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[SandboxRef, RunVirtualKey, str, int]] = []
        self.error: Exception | None = None
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def preflight_gateway(
        self,
        ref: SandboxRef,
        virtual_key: RunVirtualKey,
        *,
        provider_base_url: str,
        timeout_seconds: int,
    ) -> None:
        self.calls.append((ref, virtual_key, provider_base_url, timeout_seconds))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error


def _gateway(
    requests: list[httpx.Request],
    *,
    generate_status: int = 200,
    block_status: int = 200,
) -> LiteLLMRunKeyClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": alias}
                        for alias in FOMO_PI_DEFAULT_PREFLIGHT_ALIASES
                    ]
                },
            )
        if request.url.path == "/key/generate":
            if generate_status != 200:
                return httpx.Response(
                    generate_status,
                    text="master-secret sk-preflight-secret private body",
                )
            return httpx.Response(200, json={"key": "sk-preflight-secret"})
        if request.url.path == "/key/block":
            return httpx.Response(
                block_status,
                text="master-secret sk-preflight-secret private body",
            )
        return httpx.Response(404)

    return LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(handler),
    )


def _preflight(
    gateway: LiteLLMRunKeyClient,
    sandbox: _Sandbox,
    transport: _Transport,
) -> DirectPiRuntimePreflight:
    return DirectPiRuntimePreflight(
        gateway=gateway,
        sandbox=sandbox,  # type: ignore[arg-type]
        transport=transport,  # type: ignore[arg-type]
        provider_base_url="http://host.docker.internal:4000/v1",
        sandbox_ready_timeout_seconds=120,
        sandbox_lifetime_seconds=21_600,
        management_timeout_seconds=15,
    )


@pytest.mark.asyncio
async def test_complete_runtime_preflight_uses_short_sandbox_and_cleans_everything() -> None:
    requests: list[httpx.Request] = []
    sandbox = _Sandbox()
    transport = _Transport()

    await _preflight(_gateway(requests), sandbox, transport)()

    assert sandbox.events == ["create", "kill"]
    # 135s create + 5*15s management + 1s retry delay + 210s command
    # + 30s cleanup + 15s scheduling headroom.
    assert sandbox.lifetimes == [466]
    assert len(transport.calls) == 1
    ref, virtual_key, provider_url, timeout_seconds = transport.calls[0]
    assert ref.id == "sandbox-preflight-1"
    assert virtual_key.secret == "sk-preflight-secret"
    assert provider_url == "http://host.docker.internal:4000/v1"
    assert timeout_seconds == 195
    assert [request.url.path for request in requests] == [
        "/v1/models",
        "/key/generate",
        "/key/block",
    ]


def test_runtime_preflight_rejects_sandbox_lifetime_below_complete_budget() -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(ValueError, match="at least 466 seconds"):
        DirectPiRuntimePreflight(
            gateway=_gateway(requests),
            sandbox=_Sandbox(),  # type: ignore[arg-type]
            transport=_Transport(),  # type: ignore[arg-type]
            provider_base_url="http://host.docker.internal:4000/v1",
            sandbox_ready_timeout_seconds=120,
            sandbox_lifetime_seconds=465,
            management_timeout_seconds=15,
        )


@pytest.mark.asyncio
async def test_sandbox_create_failure_never_issues_a_virtual_key() -> None:
    requests: list[httpx.Request] = []
    sandbox = _Sandbox(
        create_error=RuntimeError("OpenSandbox key private response body"),
    )

    with pytest.raises(RuntimePreflightError) as failure:
        await _preflight(_gateway(requests), sandbox, _Transport())()

    assert str(failure.value) == (
        "OpenSandbox runtime preflight could not create a temporary sandbox"
    )
    assert requests == []
    assert "private response" not in str(failure.value)


@pytest.mark.asyncio
async def test_gateway_issue_failure_still_kills_temporary_sandbox_and_is_redacted() -> None:
    requests: list[httpx.Request] = []
    sandbox = _Sandbox()

    with pytest.raises(Exception) as failure:
        await _preflight(
            _gateway(requests, generate_status=401),
            sandbox,
            _Transport(),
        )()

    rendered = str(failure.value)
    assert "HTTP 401" in rendered
    assert "master-secret" not in rendered
    assert "sk-preflight-secret" not in rendered
    assert "private body" not in rendered
    assert sandbox.events == ["create", "kill"]
    assert [request.url.path for request in requests] == ["/v1/models", "/key/generate"]


@pytest.mark.asyncio
async def test_sandbox_command_failure_revokes_key_and_kills_sandbox_without_leaking() -> None:
    requests: list[httpx.Request] = []
    sandbox = _Sandbox()
    transport = _Transport()
    transport.error = RuntimeError(
        "master-secret sk-preflight-secret private provider response"
    )

    with pytest.raises(Exception) as failure:
        await _preflight(_gateway(requests), sandbox, transport)()

    rendered = str(failure.value)
    assert rendered == "Direct Pi sandbox gateway probe failed"
    assert "master-secret" not in rendered
    assert "sk-preflight-secret" not in rendered
    assert "private provider response" not in rendered
    assert requests[-1].url.path == "/key/block"
    assert sandbox.events[-1] == "kill"


@pytest.mark.asyncio
async def test_key_block_failure_does_not_skip_sandbox_cleanup() -> None:
    requests: list[httpx.Request] = []
    sandbox = _Sandbox()

    with pytest.raises(Exception) as failure:
        await _preflight(
            _gateway(requests, block_status=503),
            sandbox,
            _Transport(),
        )()

    rendered = str(failure.value)
    assert rendered == "LiteLLM preflight virtual key revocation failed"
    assert [request.url.path for request in requests].count("/key/block") == 3
    assert sandbox.events[-1] == "kill"


@pytest.mark.asyncio
async def test_sandbox_cleanup_failure_occurs_after_key_revocation_and_fails_preflight() -> None:
    requests: list[httpx.Request] = []
    sandbox = _Sandbox()
    sandbox.kill_error = RuntimeError("OpenSandbox key private response")

    with pytest.raises(RuntimePreflightError) as failure:
        await _preflight(_gateway(requests), sandbox, _Transport())()

    assert str(failure.value) == (
        "OpenSandbox runtime preflight temporary sandbox cleanup failed"
    )
    assert requests[-1].url.path == "/key/block"
    assert "private response" not in str(failure.value)


@pytest.mark.asyncio
async def test_cancellation_waits_for_key_revocation_and_sandbox_cleanup() -> None:
    requests: list[httpx.Request] = []
    sandbox = _Sandbox()
    sandbox.kill_release = asyncio.Event()
    transport = _Transport()
    transport.release = asyncio.Event()
    task = asyncio.create_task(_preflight(_gateway(requests), sandbox, transport)())
    await transport.started.wait()

    task.cancel()
    await sandbox.kill_started.wait()
    await asyncio.sleep(0)
    assert not task.done()
    assert requests[-1].url.path == "/key/block"

    sandbox.kill_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
