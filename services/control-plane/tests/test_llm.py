from __future__ import annotations

from typing import Any

import httpx
import pytest

import fomo.agent_runtime.llm as llm_module
from fomo.agent_runtime.llm import ModelError, ModelRequestError, OpenAICompatibleClient
from fomo.config import Settings


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {"choices": [{"message": {"content": '{"status":"ok"}'}}]}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://gateway.invalid/v1/chat/completions")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("gateway status", request=request, response=response)

    def json(self) -> dict[str, Any]:
        return self._payload


class _CapturingAsyncClient:
    def __init__(self, captured: list[dict[str, Any]], **kwargs: Any) -> None:
        captured.append(kwargs)

    async def __aenter__(self) -> _CapturingAsyncClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    async def post(self, *args: Any, **kwargs: Any) -> _Response:
        return _Response()


class _SequencedAsyncClient:
    def __init__(
        self,
        sequence: list[_Response | Exception],
        captured: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        self.sequence = sequence
        captured.append(kwargs)

    async def __aenter__(self) -> _SequencedAsyncClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    async def post(self, *args: Any, **kwargs: Any) -> _Response:
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "expected_trust_env"),
    [
        ("http://localhost:4000/v1", False),
        ("http://127.0.0.1:4000/v1", False),
        ("http://[::1]:4000/v1", False),
        ("https://gateway.example.test/v1", True),
    ],
)
async def test_model_client_bypasses_environment_proxy_only_for_loopback(
    monkeypatch, base_url: str, expected_trust_env: bool
) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _CapturingAsyncClient(captured, **kwargs),
    )

    result = await OpenAICompatibleClient(base_url, api_key="test-gateway-token").complete_json(
        "engineer",
        [{"role": "user", "content": "return JSON"}],
        "ProductSpec",
    )

    assert result == {"status": "ok"}
    assert captured[0]["trust_env"] is expected_trust_env


@pytest.mark.asyncio
async def test_retryable_gateway_responses_follow_retry_after_without_using_sop_budget(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [
        _Response(503, {"Retry-After": "0.3"}),
        _Response(504),
        _Response(),
    ]
    sleeps: list[float] = []
    retry_events = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def record_retry(retry) -> None:
        retry_events.append(retry)

    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )
    monkeypatch.setattr(llm_module.asyncio, "sleep", record_sleep)
    client = OpenAICompatibleClient(
        "http://localhost:4000/v1",
        network_retries=2,
        network_retry_base_delay_seconds=0.1,
        network_retry_max_delay_seconds=0.2,
        retry_after_max_seconds=1.0,
    )

    result = await client.complete_json("architect", [], "TechnicalSpec", on_retry=record_retry)

    assert result == {"status": "ok"}
    assert len(captured) == 3
    assert sleeps == [0.3, 0.2]
    assert [(event.attempt, event.status_code, event.delay_seconds) for event in retry_events] == [
        (1, 503, 0.3),
        (2, 504, 0.2),
    ]
    assert all(event.failure_kind == "gateway_status" for event in retry_events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ConnectError("connect failed"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.RemoteProtocolError("connection reset"),
    ],
)
async def test_retryable_transport_errors_get_an_independent_retry_budget(monkeypatch, transport_error) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [transport_error, _Response()]
    sleeps: list[float] = []
    retry_events = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def record_retry(retry) -> None:
        retry_events.append(retry)

    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )
    monkeypatch.setattr(llm_module.asyncio, "sleep", record_sleep)
    client = OpenAICompatibleClient(
        "http://localhost:4000/v1",
        network_retries=2,
        network_retry_base_delay_seconds=0.1,
        network_retry_max_delay_seconds=0.2,
    )

    assert await client.complete_json("architect", [], "TechnicalSpec", on_retry=record_retry) == {
        "status": "ok"
    }
    assert len(captured) == 2
    assert sleeps == [0.1]
    assert retry_events[0].failure_kind == "transport"
    assert retry_events[0].transport_error == type(transport_error).__name__


@pytest.mark.asyncio
async def test_ordinary_400_is_not_retried(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [_Response(400)]
    sleeps: list[float] = []
    retry_events = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def record_retry(retry) -> None:
        retry_events.append(retry)

    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )
    monkeypatch.setattr(llm_module.asyncio, "sleep", record_sleep)

    with pytest.raises(ModelRequestError, match="HTTPStatusError") as raised:
        await OpenAICompatibleClient("http://localhost:4000/v1", network_retries=2).complete_json(
            "architect", [], "TechnicalSpec", on_retry=record_retry
        )

    assert len(captured) == 1
    assert sleeps == []
    assert retry_events == []
    assert raised.value.attempts == 1
    assert raised.value.status_code == 400


@pytest.mark.asyncio
async def test_exhausted_gateway_retries_return_a_safe_error(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [_Response(504), _Response(504), _Response(504)]
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )
    monkeypatch.setattr(llm_module.asyncio, "sleep", record_sleep)
    token_marker = "test-gateway-token-never-leak"
    client = OpenAICompatibleClient(
        "http://localhost:4000/v1",
        api_key=token_marker,
        network_retries=2,
        network_retry_base_delay_seconds=0.1,
        network_retry_max_delay_seconds=0.2,
    )

    with pytest.raises(ModelRequestError) as raised:
        await client.complete_json("architect", [], "TechnicalSpec")

    assert str(raised.value) == "model request failed after 3 attempts (retryable gateway response)"
    assert token_marker not in str(raised.value)
    assert raised.value.attempts == 3
    assert raised.value.failure_kind == "gateway_status"
    assert raised.value.status_code == 504
    assert len(captured) == 3
    assert sleeps == [0.1, 0.2]


@pytest.mark.asyncio
async def test_malformed_response_body_is_not_kept_as_an_exception_cause(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    response_body_marker = "response-body-never-leak"
    sequence = [_Response(payload={"choices": [{"message": {"content": response_body_marker}}]})]
    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )

    with pytest.raises(ModelError) as raised:
        await OpenAICompatibleClient("http://localhost:4000/v1").complete_json(
            "architect", [], "TechnicalSpec"
        )

    assert str(raised.value) == "model did not return a TechnicalSpec JSON object"
    assert response_body_marker not in str(raised.value)
    assert raised.value.__cause__ is None


def test_default_engineer_batch_budget_is_twenty_four_by_one() -> None:
    settings = Settings()

    assert settings.engineer_max_batches == 24
    assert settings.engineer_max_files_per_batch == 1
    assert settings.engineer_max_batches * settings.engineer_max_files_per_batch == 24


def test_settings_reads_network_retry_environment(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "75")
    monkeypatch.setenv("MODEL_NETWORK_RETRIES", "4")
    monkeypatch.setenv("MODEL_NETWORK_RETRY_BASE_DELAY_SECONDS", "0.25")
    monkeypatch.setenv("MODEL_NETWORK_RETRY_MAX_DELAY_SECONDS", "2.5")
    monkeypatch.setenv("MODEL_RETRY_AFTER_MAX_SECONDS", "15")
    monkeypatch.setenv("ENGINEER_MAX_BATCHES", "6")
    monkeypatch.setenv("ENGINEER_MAX_FILES_PER_BATCH", "2")
    monkeypatch.setenv("ENGINEER_MAX_FILE_CHARACTERS", "8000")

    settings = Settings.from_env()

    assert settings.model_network_retries == 4
    assert settings.model_request_timeout_seconds == 75
    assert settings.model_network_retry_base_delay_seconds == 0.25
    assert settings.model_network_retry_max_delay_seconds == 2.5
    assert settings.model_retry_after_max_seconds == 15.0
    assert settings.engineer_max_batches == 6
    assert settings.engineer_max_files_per_batch == 2
    assert settings.engineer_max_file_characters == 8000
