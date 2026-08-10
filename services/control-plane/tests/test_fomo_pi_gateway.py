from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fomo.fomo_pi_ds import (
    FOMO_PI_DEFAULT_PREFLIGHT_ALIASES,
    FOMO_PI_LITELLM_ALIASES,
    FOMO_PI_SELECTABLE_LITELLM_ALIASES,
    InferenceGatewayError,
    LiteLLMRunKeyClient,
    RunVirtualKey,
)


def _models_response(*aliases: str) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": alias} for alias in aliases]})


async def _noop_probe(_virtual_key: RunVirtualKey) -> None:
    return None


@pytest.mark.asyncio
async def test_run_key_is_least_privilege_and_blocked_by_exact_secret() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/key/generate":
            return httpx.Response(200, json={"key": "sk-run-secret", "expires": None})
        if request.url.path == "/key/block":
            return httpx.Response(200, json=None)
        return httpx.Response(404)

    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(handler),
    )
    virtual_key = await client.issue(
        run_id="run-123",
        duration_seconds=4_200,
        max_budget=2.0,
        rpm_limit=60,
        tpm_limit=1_000_000,
        model_aliases=(FOMO_PI_SELECTABLE_LITELLM_ALIASES[0],),
    )
    await client.block(virtual_key)

    generate = json.loads(requests[0].content)
    assert generate == {
        "key_alias": "fomo-run-run-123",
        "models": [FOMO_PI_SELECTABLE_LITELLM_ALIASES[0]],
        "duration": "4200s",
        "max_budget": 2.0,
        "max_parallel_requests": 1,
        "rpm_limit": 60,
        "tpm_limit": 1_000_000,
        "metadata": {"fomo_run_id": "run-123", "scope": "fomo-pi-ds"},
    }
    assert json.loads(requests[1].content) == {"key": "sk-run-secret"}
    assert FOMO_PI_LITELLM_ALIASES == ("fomo-pi-flash", "fomo-pi-build")
    assert virtual_key.model_aliases == (FOMO_PI_SELECTABLE_LITELLM_ALIASES[0],)
    assert all(request.headers["authorization"] == "Bearer master-secret" for request in requests)
    assert "sk-run-secret" not in repr(virtual_key)
    assert "master-secret" not in repr(client)


@pytest.mark.asyncio
async def test_runtime_preflight_delegates_scoped_key_to_sandbox_and_revokes_it() -> None:
    requests: list[httpx.Request] = []
    probed: list[RunVirtualKey] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/models":
            return _models_response(*FOMO_PI_SELECTABLE_LITELLM_ALIASES[:2])
        if request.url.path == "/key/generate":
            return httpx.Response(200, json={"key": "sk-preflight-secret"})
        if request.url.path == "/key/block":
            return httpx.Response(200, json=None)
        return httpx.Response(404)

    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(handler),
    )
    async def probe(virtual_key: RunVirtualKey) -> None:
        probed.append(virtual_key)

    selected = FOMO_PI_SELECTABLE_LITELLM_ALIASES[:2]
    await client.preflight(probe, model_aliases=selected)

    assert [request.url.path for request in requests] == [
        "/v1/models",
        "/key/generate",
        "/key/block",
    ]
    assert len(probed) == 1
    assert probed[0].secret == "sk-preflight-secret"
    assert probed[0].model_aliases == selected
    assert json.loads(requests[-1].content) == {"key": "sk-preflight-secret"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 503])
async def test_runtime_preflight_management_errors_are_status_only(status_code: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text="master-secret sk-preflight-secret private response body",
        )

    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(InferenceGatewayError) as failure:
        await client.preflight(_noop_probe)

    rendered = str(failure.value)
    assert f"HTTP {status_code}" in rendered
    assert "master-secret" not in rendered
    assert "sk-preflight-secret" not in rendered
    assert "private response body" not in rendered


@pytest.mark.asyncio
async def test_runtime_preflight_connection_error_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "master-secret sk-preflight-secret private response body",
            request=request,
        )

    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(InferenceGatewayError) as failure:
        await client.preflight(_noop_probe)

    rendered = str(failure.value)
    assert rendered == "LiteLLM preflight model discovery request failed"
    assert "master-secret" not in rendered
    assert "sk-preflight-secret" not in rendered
    assert "private response body" not in rendered


@pytest.mark.asyncio
async def test_runtime_preflight_rejects_missing_alias_without_creating_a_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _models_response()

    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(
        InferenceGatewayError,
        match=FOMO_PI_DEFAULT_PREFLIGHT_ALIASES[0],
    ):
        await client.preflight(_noop_probe)

    assert [request.url.path for request in requests] == ["/v1/models"]


@pytest.mark.asyncio
async def test_runtime_preflight_revokes_key_after_redacted_sandbox_probe_failure() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/models":
            return _models_response(*FOMO_PI_DEFAULT_PREFLIGHT_ALIASES)
        if request.url.path == "/key/generate":
            return httpx.Response(200, json={"key": "sk-preflight-secret"})
        if request.url.path == "/key/block":
            return httpx.Response(200, json=None)
        return httpx.Response(404)

    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(handler),
    )
    async def failed_probe(_virtual_key: RunVirtualKey) -> None:
        raise RuntimeError("master-secret sk-preflight-secret private response body")

    with pytest.raises(InferenceGatewayError) as failure:
        await client.preflight(failed_probe)

    rendered = str(failure.value)
    assert rendered == "Direct Pi sandbox gateway probe failed"
    assert "master-secret" not in rendered
    assert "sk-preflight-secret" not in rendered
    assert "private response body" not in rendered
    assert requests[-1].url.path == "/key/block"


@pytest.mark.asyncio
async def test_key_revocation_retries_transient_failures_without_exposing_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/key/generate":
            return httpx.Response(200, json={"key": "sk-retry-secret"})
        if request.url.path == "/key/block" and len(requests) < 4:
            return httpx.Response(503, text="sk-retry-secret private response")
        if request.url.path == "/key/block":
            return httpx.Response(200, json=None)
        return httpx.Response(404)

    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(handler),
    )
    virtual_key = await client.issue(
        run_id="retry-run",
        duration_seconds=300,
        max_budget=0.1,
        rpm_limit=3,
        tpm_limit=4096,
    )

    await client.block(virtual_key)

    assert [request.url.path for request in requests].count("/key/block") == 3


@pytest.mark.asyncio
async def test_key_revocation_defers_cancellation_until_cleanup_finishes(monkeypatch) -> None:
    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
    )
    virtual_key = RunVirtualKey(
        run_id="cancel-run",
        key_alias="fomo-run-cancel-run",
        duration_seconds=300,
        secret="sk-cancel-secret",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_revoke(_virtual_key: RunVirtualKey) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(client, "_block_with_retries", delayed_revoke)
    task = asyncio.create_task(client.block(virtual_key))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_gateway_errors_do_not_echo_response_or_credentials() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="master-secret sk-run-secret private body")

    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(InferenceGatewayError) as failure:
        await client.issue(
            run_id="run-123",
            duration_seconds=4_200,
            max_budget=2.0,
            rpm_limit=60,
            tpm_limit=1_000_000,
        )

    rendered = str(failure.value)
    assert "HTTP 500" in rendered
    assert "master-secret" not in rendered
    assert "sk-run-secret" not in rendered
    assert "private body" not in rendered


@pytest.mark.asyncio
async def test_gateway_rejects_missing_or_oversized_key_contract() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"token": "wrong field"}),
            httpx.Response(200, json={"key": "x" * 4097}),
        ]
    )
    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000",
        master_key="master-secret",
        transport=httpx.MockTransport(lambda _request: next(responses)),
    )

    for _ in range(2):
        with pytest.raises(InferenceGatewayError):
            await client.issue(
                run_id="run-123",
                duration_seconds=4_200,
                max_budget=2.0,
                rpm_limit=60,
                tpm_limit=1_000_000,
            )


@pytest.mark.asyncio
async def test_gateway_rejects_unbounded_or_unsafe_inputs() -> None:
    with pytest.raises(ValueError, match="master key"):
        LiteLLMRunKeyClient(management_url="http://litellm:4000", master_key="")

    client = LiteLLMRunKeyClient(
        management_url="http://litellm:4000", master_key="master-secret"
    )
    with pytest.raises(ValueError, match="run_id"):
        # Validation occurs before a network call.
        await client.issue(
            run_id="../escape",
            duration_seconds=4_200,
            max_budget=2.0,
            rpm_limit=60,
            tpm_limit=1_000_000,
        )
