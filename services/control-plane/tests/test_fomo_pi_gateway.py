from __future__ import annotations

import json

import httpx
import pytest

from fomo.fomo_pi_ds import InferenceGatewayError, LiteLLMRunKeyClient


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
    )
    await client.block(virtual_key)

    generate = json.loads(requests[0].content)
    assert generate == {
        "key_alias": "fomo-run-run-123",
        "models": ["fomo-pi-flash"],
        "duration": "4200s",
        "max_budget": 2.0,
        "max_parallel_requests": 1,
        "rpm_limit": 60,
        "tpm_limit": 1_000_000,
        "metadata": {"fomo_run_id": "run-123", "scope": "fomo-pi-ds"},
    }
    assert json.loads(requests[1].content) == {"key": "sk-run-secret"}
    assert all(request.headers["authorization"] == "Bearer master-secret" for request in requests)
    assert "sk-run-secret" not in repr(virtual_key)
    assert "master-secret" not in repr(client)


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
