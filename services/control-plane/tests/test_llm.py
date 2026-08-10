from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
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
        lines: list[str | Exception] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {"choices": [{"delta": {"content": '{"status":"ok"}'}}]}
        self._lines = lines

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://gateway.invalid/v1/chat/completions")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("gateway status", request=request, response=response)

    def json(self) -> dict[str, Any]:
        return self._payload

    async def aiter_lines(self):
        lines = self._lines
        if lines is None:
            lines = [f"data: {json.dumps(self._payload)}", "", "data: [DONE]", ""]
        for item in lines:
            if isinstance(item, Exception):
                raise item
            yield item


class _StreamContext:
    def __init__(self, item: _Response | Exception) -> None:
        self._item = item

    async def __aenter__(self) -> _Response:
        if isinstance(self._item, Exception):
            raise self._item
        return self._item

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class _CapturingAsyncClient:
    def __init__(
        self,
        captured: list[dict[str, Any]],
        *,
        requests: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        captured.append(kwargs)
        self._requests = requests

    async def __aenter__(self) -> _CapturingAsyncClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def stream(self, method: str, endpoint: str, **kwargs: Any) -> _StreamContext:
        if self._requests is not None:
            self._requests.append({"method": method, "endpoint": endpoint, **kwargs})
        return _StreamContext(_Response())


class _SequencedAsyncClient:
    def __init__(
        self,
        sequence: list[_Response | Exception],
        captured: list[dict[str, Any]],
        *,
        requests: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        self.sequence = sequence
        captured.append(kwargs)
        self._requests = requests

    async def __aenter__(self) -> _SequencedAsyncClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def stream(self, method: str, endpoint: str, **kwargs: Any) -> _StreamContext:
        item = self.sequence.pop(0)
        if self._requests is not None:
            self._requests.append({"method": method, "endpoint": endpoint, **kwargs})
        return _StreamContext(item)


def _sse_data(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}"


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
async def test_structured_chat_completion_requests_streaming_json(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _CapturingAsyncClient(captured, requests=requests, **kwargs),
    )

    result = await OpenAICompatibleClient("http://localhost:4000/v1").complete_json(
        "any-logical-model-alias",
        [{"role": "user", "content": "return JSON"}],
        "TechnicalSpec",
    )

    assert result == {"status": "ok"}
    assert requests == [
        {
            "method": "POST",
            "endpoint": "http://localhost:4000/v1/chat/completions",
            "headers": {"Content-Type": "application/json", "Accept": "text/event-stream"},
            "json": {
                "model": "any-logical-model-alias",
                "messages": [{"role": "user", "content": "return JSON"}],
                "temperature": 0.2,
                "stream": True,
                "response_format": {"type": "json_object"},
            },
        }
    ]


@pytest.mark.asyncio
async def test_streamed_delta_content_is_assembled_after_a_single_done_marker(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [
        _Response(
            lines=[
                "event: message",
                _sse_data({"choices": [{"delta": {"role": "assistant"}}]}),
                _sse_data({"choices": [{"delta": {"content": '{"status":'}}]}),
                _sse_data({"choices": [{"delta": {"content": '"streamed"}'}}]}),
                _sse_data({"choices": [{"delta": {}}]}),
                "data: [DONE]",
                "",
            ]
        )
    ]
    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )

    assert await OpenAICompatibleClient("http://localhost:4000/v1").complete_json(
        "engineer", [], "ImplementationPlan"
    ) == {"status": "streamed"}


@pytest.mark.asyncio
async def test_missing_done_retries_stream_and_discards_partial_content(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [
        _Response(lines=[_sse_data({"choices": [{"delta": {"content": '{"discarded":'}}]})]),
        _Response(
            lines=[
                _sse_data({"choices": [{"delta": {"content": '{"status":"recovered"}'}}]}),
                "data: [DONE]",
            ]
        ),
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

    assert await OpenAICompatibleClient(
        "http://localhost:4000/v1",
        network_retries=1,
        network_retry_base_delay_seconds=0.1,
        network_retry_max_delay_seconds=0.2,
        random_source=lambda: 1.0,
    ).complete_json("architect", [], "TechnicalSpec", on_retry=record_retry) == {"status": "recovered"}

    assert len(captured) == 2
    assert sleeps == [0.1]
    assert retry_events[0].failure_kind == "stream"
    assert retry_events[0].transport_error == "_SSEStreamIncompleteError"


@pytest.mark.asyncio
async def test_midstream_read_timeout_retries_and_discards_partial_content(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [
        _Response(
            lines=[
                _sse_data({"choices": [{"delta": {"content": '{"discarded":'}}]}),
                httpx.ReadTimeout("read timed out"),
            ]
        ),
        _Response(
            lines=[
                _sse_data({"choices": [{"delta": {"content": '{"status":"recovered"}'}}]}),
                "data: [DONE]",
            ]
        ),
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

    assert await OpenAICompatibleClient(
        "http://localhost:4000/v1",
        network_retries=1,
        network_retry_base_delay_seconds=0.1,
        network_retry_max_delay_seconds=0.2,
        random_source=lambda: 1.0,
    ).complete_json("architect", [], "TechnicalSpec", on_retry=record_retry) == {"status": "recovered"}

    assert len(captured) == 2
    assert sleeps == [0.1]
    assert retry_events[0].failure_kind == "transport"
    assert retry_events[0].transport_error == "ReadTimeout"


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
        random_source=lambda: 1.0,
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
async def test_default_retry_budget_recovers_after_five_429_responses(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [_Response(429) for _ in range(5)] + [_Response()]
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )
    monkeypatch.setattr(llm_module.asyncio, "sleep", record_sleep)

    assert await OpenAICompatibleClient(
        "http://localhost:4000/v1", random_source=lambda: 1.0
    ).complete_json("engineer", [], "ImplementationPlan") == {"status": "ok"}

    assert len(captured) == 6
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


@pytest.mark.asyncio
async def test_429_honors_numeric_retry_after_with_a_bounded_cap(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [_Response(429, {"Retry-After": "9"}), _Response()]
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )
    monkeypatch.setattr(llm_module.asyncio, "sleep", record_sleep)

    assert await OpenAICompatibleClient(
        "http://localhost:4000/v1",
        network_retries=1,
        network_retry_base_delay_seconds=0.1,
        network_retry_max_delay_seconds=0.2,
        retry_after_max_seconds=1.5,
        random_source=lambda: 1.0,
    ).complete_json("engineer", [], "ImplementationPlan") == {"status": "ok"}

    assert len(captured) == 2
    assert sleeps == [1.5]


@pytest.mark.asyncio
async def test_429_honors_http_date_retry_after_with_an_injected_clock(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    fixed_now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    sequence = [
        _Response(429, {"Retry-After": format_datetime(fixed_now + timedelta(seconds=3), usegmt=True)}),
        _Response(),
    ]
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )
    monkeypatch.setattr(llm_module.asyncio, "sleep", record_sleep)

    assert await OpenAICompatibleClient(
        "http://localhost:4000/v1",
        network_retries=1,
        network_retry_base_delay_seconds=0.1,
        network_retry_max_delay_seconds=0.2,
        retry_after_max_seconds=5.0,
        random_source=lambda: 1.0,
        now=lambda: fixed_now,
    ).complete_json("reviewer", [], "DiagnosticReport") == {"status": "ok"}

    assert len(captured) == 2
    assert sleeps == [3.0]


@pytest.mark.asyncio
async def test_retry_backoff_uses_injected_bounded_jitter(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    sequence = [_Response(504), _Response(504), _Response()]
    samples = iter((0.0, 1.0))
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )
    monkeypatch.setattr(llm_module.asyncio, "sleep", record_sleep)

    assert await OpenAICompatibleClient(
        "http://localhost:4000/v1",
        network_retries=2,
        network_retry_base_delay_seconds=0.2,
        network_retry_max_delay_seconds=0.3,
        random_source=lambda: next(samples),
    ).complete_json("architect", [], "TechnicalSpec") == {"status": "ok"}

    assert len(captured) == 3
    assert sleeps == [0.1, 0.3]


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
        random_source=lambda: 1.0,
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
        random_source=lambda: 1.0,
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
    sequence = [
        _Response(
            lines=[
                _sse_data({"choices": [{"delta": {"content": response_body_marker}}]}),
                "data: [DONE]",
            ]
        )
    ]
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


@pytest.mark.asyncio
async def test_malformed_sse_does_not_leak_response_body(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    response_body_marker = "malformed-sse-body-never-leak"
    sequence = [_Response(lines=[f"data: {response_body_marker}", "data: [DONE]"])]
    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedAsyncClient(sequence, captured, **kwargs),
    )

    with pytest.raises(ModelRequestError) as raised:
        await OpenAICompatibleClient(
            "http://localhost:4000/v1", network_retries=0
        ).complete_json("architect", [], "TechnicalSpec")

    assert str(raised.value) == "model request failed after 1 attempts (_SSEStreamProtocolError)"
    assert response_body_marker not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.failure_kind == "stream"


def test_default_engineer_batch_budget_is_twenty_four_by_one() -> None:
    settings = Settings()

    assert settings.engineer_max_batches == 24
    assert settings.engineer_max_files_per_batch == 1
    assert settings.engineer_max_batches * settings.engineer_max_files_per_batch == 24
    assert settings.engineer_target_file_characters == 12_000
    assert settings.engineer_max_file_characters == 20_000


def test_settings_reads_network_retry_environment(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "75")
    monkeypatch.setenv("MODEL_NETWORK_RETRIES", "4")
    monkeypatch.setenv("MODEL_NETWORK_RETRY_BASE_DELAY_SECONDS", "0.25")
    monkeypatch.setenv("MODEL_NETWORK_RETRY_MAX_DELAY_SECONDS", "2.5")
    monkeypatch.setenv("MODEL_RETRY_AFTER_MAX_SECONDS", "15")
    monkeypatch.setenv("ENGINEER_MAX_BATCHES", "6")
    monkeypatch.setenv("ENGINEER_MAX_FILES_PER_BATCH", "2")
    monkeypatch.setenv("ENGINEER_TARGET_FILE_CHARACTERS", "6000")
    monkeypatch.setenv("ENGINEER_MAX_FILE_CHARACTERS", "8000")

    settings = Settings.from_env()

    assert settings.model_network_retries == 4
    assert settings.model_request_timeout_seconds == 75
    assert settings.model_network_retry_base_delay_seconds == 0.25
    assert settings.model_network_retry_max_delay_seconds == 2.5
    assert settings.model_retry_after_max_seconds == 15.0
    assert settings.engineer_max_batches == 6
    assert settings.engineer_max_files_per_batch == 2
    assert settings.engineer_target_file_characters == 6000
    assert settings.engineer_max_file_characters == 8000


@pytest.mark.parametrize(
    ("target", "hard", "message"),
    [
        pytest.param(
            "20001",
            "20000",
            "ENGINEER_TARGET_FILE_CHARACTERS must be less than or equal to ENGINEER_MAX_FILE_CHARACTERS",
            id="target-exceeds-hard",
        ),
        pytest.param(
            "12000",
            "24001",
            "ENGINEER_MAX_FILE_CHARACTERS must be at most 24000",
            id="hard-exceeds-maximum",
        ),
        pytest.param(
            "0",
            "20000",
            "ENGINEER_TARGET_FILE_CHARACTERS must be greater than 0",
            id="target-is-not-positive",
        ),
        pytest.param(
            "12000",
            "0",
            "ENGINEER_MAX_FILE_CHARACTERS must be greater than 0",
            id="hard-is-not-positive",
        ),
        pytest.param(
            "not-an-integer",
            "20000",
            "ENGINEER_TARGET_FILE_CHARACTERS must be an integer",
            id="target-is-not-an-integer",
        ),
    ],
)
def test_settings_rejects_invalid_engineer_file_character_limits(
    monkeypatch, target: str, hard: str, message: str
) -> None:
    monkeypatch.setenv("ENGINEER_TARGET_FILE_CHARACTERS", target)
    monkeypatch.setenv("ENGINEER_MAX_FILE_CHARACTERS", hard)

    with pytest.raises(ValueError, match=re.escape(message)):
        Settings.from_env()


def test_gpt55_role_routes_use_xhigh_reasoning_effort_without_pro_models() -> None:
    config = (Path(__file__).resolve().parents[3] / "infra" / "litellm" / "config.yaml").read_text(
        encoding="utf-8"
    )
    for alias in ("pm-fallback", "architect-fallback", "engineer", "reviewer-fallback"):
        match = re.search(
            rf"(?ms)^  - model_name: {re.escape(alias)}\n(?P<body>.*?)(?=^  - model_name:|\Z)",
            config,
        )
        assert match is not None
        assert "model: openai/gpt-5.5" in match.group("body")
        assert "reasoning_effort: xhigh" in match.group("body")

    assert "model: openai/gpt-5.5-pro" not in config


def test_direct_pi_catalog_aliases_are_unique_scoped_and_documented() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    config = (repository_root / "infra" / "litellm" / "config.yaml").read_text(encoding="utf-8")
    model_aliases = list(
        re.finditer(
            r"(?ms)^  - model_name: (?P<name>[^\n]+)\n(?P<body>.*?)(?=^  - model_name:|\Z)",
            config,
        )
    )
    aliases_by_name = {match.group("name"): match.group("body") for match in model_aliases}
    assert len(aliases_by_name) == len(model_aliases)

    legacy_flash = aliases_by_name["deepseek-flash"]
    assert "model: deepseek/deepseek-v4-flash" in legacy_flash
    assert "api_key: os.environ/DEEPSEEK_API_KEY" in legacy_flash
    assert "api_base: https://api.deepseek.com" in legacy_flash
    assert "thinking:\n        type: disabled" in legacy_flash
    assert legacy_flash.count("thinking:") == 1

    canonical_gpt56 = aliases_by_name["fomo-pi-gpt-5.6"]
    assert "model: openai/gpt-5.6-sol" in canonical_gpt56
    assert "api_key: os.environ/OPENAI_API_KEY" in canonical_gpt56
    assert "api_base: os.environ/OPENAI_API_BASE" in canonical_gpt56
    assert "use_chat_completions_api: true" in canonical_gpt56

    canonical_gpt55 = aliases_by_name["fomo-pi-gpt-5.5"]
    assert "model: openai/gpt-5.5" in canonical_gpt55
    assert "api_key: os.environ/OPENAI_API_KEY" in canonical_gpt55
    assert "api_base: os.environ/OPENAI_API_BASE" in canonical_gpt55
    assert "use_chat_completions_api: true" in canonical_gpt55

    canonical_flash = aliases_by_name["fomo-pi-deepseek-flash"]
    assert "model: deepseek/deepseek-v4-flash" in canonical_flash
    assert "api_key: os.environ/DEEPSEEK_API_KEY" in canonical_flash
    assert "api_base: https://api.deepseek.com" in canonical_flash
    assert "thinking:" not in canonical_flash

    direct_flash = aliases_by_name["fomo-pi-flash"]
    assert "model: deepseek/deepseek-v4-flash" in direct_flash
    assert "api_key: os.environ/DEEPSEEK_API_KEY" in direct_flash
    assert "api_base: https://api.deepseek.com" in direct_flash
    assert "thinking:\n        type: enabled" in direct_flash
    assert direct_flash.count("thinking:") == 1

    direct_build = aliases_by_name["fomo-pi-build"]
    assert "model: openai/gpt-5.5" in direct_build
    assert "api_key: os.environ/OPENAI_API_KEY" in direct_build
    assert "api_base: os.environ/OPENAI_API_BASE" in direct_build
    # Build/repair thinking is selected explicitly by the Pi RPC contract;
    # this GPT route must not inherit DeepSeek's static `thinking` request body.
    assert "thinking:" not in direct_build

    grok_route = aliases_by_name["fomo-pi-grok-4.5"]
    assert "model: openai/grok-4.5" in grok_route
    assert "api_key: os.environ/GROK_API_KEY" in grok_route
    assert "api_base: https://sub.yieldsum.com/v1" in grok_route
    assert "input_cost_per_token: 0.000004" in grok_route
    assert "output_cost_per_token: 0.000012" in grok_route

    opencode_routes = {
        "fomo-pi-kimi-k2.7-code": (
            "model: openai/kimi-k2.7-code",
            "input_cost_per_token: 0.00000095",
            "output_cost_per_token: 0.000004",
        ),
        "fomo-pi-gemini-3.6-flash": (
            "model: gemini/gemini-3.6-flash",
            "input_cost_per_token: 0.0000015",
            "output_cost_per_token: 0.0000075",
        ),
        "fomo-pi-gemini-3.1-pro": (
            "model: gemini/gemini-3.1-pro",
            "input_cost_per_token: 0.000004",
            "output_cost_per_token: 0.000018",
        ),
    }
    for alias, expected_fragments in opencode_routes.items():
        route = aliases_by_name[alias]
        assert "api_key: os.environ/OPENCODE_API_KEY" in route
        assert "api_base: https://opencode.ai/zen/v1" in route
        assert all(fragment in route for fragment in expected_fragments)

    deepseek_routes = {
        name
        for name, body in aliases_by_name.items()
        if "model: deepseek/deepseek-v4-flash" in body
    }
    assert deepseek_routes == {
        "deepseek-flash",
        "fomo-pi-deepseek-flash",
        "fomo-pi-flash",
    }
    assert "deepseek-v4-pro" not in config.lower()

    compose = (repository_root / "compose.yaml").read_text(encoding="utf-8")
    services = compose.split("\nservices:\n", 1)[1]
    litellm_service = re.search(
        r"(?ms)^  litellm:\n(?P<body>.*?)(?=^  [a-z0-9-]+:|\Z)", services
    )
    assert litellm_service is not None
    assert "DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY:-}" in litellm_service.group("body")
    assert len(re.findall(r"(?m)^\s+DEEPSEEK_API_KEY:", compose)) == 1
    assert "GROK_API_KEY: ${GROK_API_KEY:-}" in litellm_service.group("body")
    assert len(re.findall(r"(?m)^\s+GROK_API_KEY:", compose)) == 1
    assert "OPENCODE_API_KEY: ${OPENCODE_API_KEY:-}" in litellm_service.group("body")
    assert len(re.findall(r"(?m)^\s+OPENCODE_API_KEY:", compose)) == 1
    assert (
        "FOMO_RUNTIME_ENABLED_PROFILES: "
        "${FOMO_RUNTIME_ENABLED_PROFILES:-deepseek-flash}"
    ) in compose
    assert (
        "FOMO_RUNTIME_DEFAULT_PROFILE: "
        "${FOMO_RUNTIME_DEFAULT_PROFILE:-deepseek-flash}"
    ) in compose

    env_example = (repository_root / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"(?m)^DEEPSEEK_API_KEY=$", env_example)
    assert re.search(r"(?m)^GROK_API_KEY=$", env_example)
    assert re.search(r"(?m)^OPENCODE_API_KEY=$", env_example)
    assert re.search(
        r"(?m)^FOMO_RUNTIME_ENABLED_PROFILES=deepseek-flash$", env_example
    )
    assert re.search(
        r"(?m)^FOMO_RUNTIME_DEFAULT_PROFILE=deepseek-flash$", env_example
    )
    runtime_readme = (repository_root / "services" / "control-plane" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "`fomo-pi-flash` (planning)" in runtime_readme
    assert "`fomo-pi-build` (building/repairing)" in runtime_readme
    assert "explicit thinking compatibility" in runtime_readme
