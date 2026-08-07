"""LiteLLM OpenAI-compatible client using logical model aliases only."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx


class ModelError(RuntimeError):
    """A model response could not satisfy the requested structured contract."""


class ModelRequestError(ModelError):
    """The model gateway could not complete a request after transport recovery.

    It is deliberately distinct from ``ModelError`` so the SOP never spends a
    schema-repair attempt on an unavailable or rejected gateway request.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        failure_kind: str = "request",
        status_code: int | None = None,
        transport_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.failure_kind = failure_kind
        self.status_code = status_code
        self.transport_error = transport_error


@dataclass(frozen=True, slots=True)
class ModelRetry:
    """Safe telemetry for one retryable model transport failure."""

    attempt: int
    max_attempts: int
    delay_seconds: float
    failure_kind: str
    status_code: int | None = None
    transport_error: str | None = None


RetryObserver = Callable[[ModelRetry], Awaitable[None]]


class _SSEStreamError(ValueError):
    """A streamed completion ended before it became a trustworthy response."""


class _SSEStreamIncompleteError(_SSEStreamError):
    pass


class _SSEStreamProtocolError(_SSEStreamError):
    pass


_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)
_RETRYABLE_STREAM_ERRORS = (_SSEStreamIncompleteError, _SSEStreamProtocolError)
_RETRYABLE_REQUEST_ERRORS = _RETRYABLE_TRANSPORT_ERRORS + _RETRYABLE_STREAM_ERRORS


class ModelClient(Protocol):
    async def complete_json(
        self,
        model_alias: str,
        messages: Sequence[dict[str, str]],
        schema_name: str,
        *,
        on_retry: RetryObserver | None = None,
    ) -> dict[str, Any]: ...


class OpenAICompatibleClient:
    """Call LiteLLM's OpenAI-compatible API without handling provider credentials."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: int = 300,
        *,
        network_retries: int = 5,
        network_retry_base_delay_seconds: float = 1.0,
        network_retry_max_delay_seconds: float = 16.0,
        retry_after_max_seconds: float = 60.0,
        random_source: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        # This budget is intentionally independent of SOP structured-output
        # retries: transport recovery must not consume schema-repair attempts.
        self.network_retries = max(0, network_retries)
        self.network_retry_base_delay_seconds = max(0.0, network_retry_base_delay_seconds)
        self.network_retry_max_delay_seconds = max(
            self.network_retry_base_delay_seconds,
            network_retry_max_delay_seconds,
        )
        self.retry_after_max_seconds = max(0.0, retry_after_max_seconds)
        # Both sources are injectable so retry timing stays deterministic in
        # tests without weakening production's jittered recovery behavior.
        self._random_source = random_source or random.random
        self._now = now or (lambda: datetime.now(UTC))

    async def complete_json(
        self,
        model_alias: str,
        messages: Sequence[dict[str, str]],
        schema_name: str,
        *,
        on_retry: RetryObserver | None = None,
    ) -> dict[str, Any]:
        if self.base_url.endswith("/chat/completions"):
            endpoint = self.base_url
        elif self.base_url.endswith("/v1"):
            endpoint = f"{self.base_url}/chat/completions"
        else:
            endpoint = f"{self.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {
            "model": model_alias,
            "messages": list(messages),
            "temperature": 0.2,
            "stream": True,
            "response_format": {"type": "json_object"},
        }
        content = await self._request_streamed_content(endpoint, headers, body, on_retry=on_retry)
        try:
            return _parse_json_object(content)
        except (TypeError, json.JSONDecodeError):
            # The response content can be included in a JSONDecodeError's
            # diagnostic context. Keep the worker's exception path free of
            # response-body data as well as request credentials.
            raise ModelError(f"model did not return a {schema_name} JSON object") from None

    async def _request_streamed_content(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, Any],
        *,
        on_retry: RetryObserver | None,
    ) -> str:
        for attempt in range(self.network_retries + 1):
            try:
                # A sourced local dotenv may set HTTP_PROXY for external traffic.
                # Never send localhost LiteLLM through that proxy; external gateway
                # URLs retain normal proxy support from the environment.
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    trust_env=not _is_loopback_url(endpoint),
                ) as client:
                    async with client.stream("POST", endpoint, headers=headers, json=body) as response:
                        if response.status_code in _RETRYABLE_STATUS_CODES:
                            if attempt < self.network_retries:
                                delay_seconds = self._retry_delay(
                                    attempt, response.headers.get("Retry-After")
                                )
                                await self._notify_retry(
                                    on_retry,
                                    ModelRetry(
                                        attempt=attempt + 1,
                                        max_attempts=self.network_retries + 1,
                                        delay_seconds=delay_seconds,
                                        failure_kind="gateway_status",
                                        status_code=response.status_code,
                                    ),
                                )
                                await asyncio.sleep(delay_seconds)
                                continue
                            raise ModelRequestError(
                                "model request failed after "
                                f"{attempt + 1} attempts (retryable gateway response)",
                                attempts=attempt + 1,
                                failure_kind="gateway_status",
                                status_code=response.status_code,
                            )
                        response.raise_for_status()
                        return await self._consume_sse_content(response)
            except _RETRYABLE_REQUEST_ERRORS as exc:
                failure_kind = "stream" if isinstance(exc, _SSEStreamError) else "transport"
                if attempt < self.network_retries:
                    delay_seconds = self._retry_delay(attempt)
                    await self._notify_retry(
                        on_retry,
                        ModelRetry(
                            attempt=attempt + 1,
                            max_attempts=self.network_retries + 1,
                            delay_seconds=delay_seconds,
                            failure_kind=failure_kind,
                            transport_error=type(exc).__name__,
                        ),
                    )
                    await asyncio.sleep(delay_seconds)
                    continue
                raise ModelRequestError(
                    f"model request failed after {attempt + 1} attempts ({type(exc).__name__})",
                    attempts=attempt + 1,
                    failure_kind=failure_kind,
                    transport_error=type(exc).__name__,
                ) from None
            except ModelRequestError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                # Do not include HTTP bodies, headers, URLs, or exception chains:
                # gateways can expose credentials in any of those values.
                status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                raise ModelRequestError(
                    f"model request failed ({type(exc).__name__})",
                    attempts=attempt + 1,
                    failure_kind="http_status" if status_code is not None else "gateway_response",
                    status_code=status_code,
                ) from None
        raise AssertionError("unreachable")

    @staticmethod
    async def _consume_sse_content(response: Any) -> str:
        parts: list[str] = []
        done_seen = False
        async for line in response.aiter_lines():
            if not isinstance(line, str):
                raise _SSEStreamProtocolError("invalid SSE line")
            if not line or line.startswith((":", "event:", "id:", "retry:")):
                continue
            if not line.startswith("data:"):
                raise _SSEStreamProtocolError("invalid SSE field")
            data = line[5:].lstrip()
            if data == "[DONE]":
                if done_seen:
                    raise _SSEStreamProtocolError("duplicate SSE completion marker")
                done_seen = True
                continue
            if done_seen:
                raise _SSEStreamProtocolError("SSE data followed completion marker")
            try:
                event = json.loads(data)
                if not isinstance(event, dict):
                    raise TypeError("SSE event was not an object")
                choices = event["choices"]
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    raise TypeError("SSE event choices were invalid")
                delta = choices[0]["delta"]
                if not isinstance(delta, dict):
                    raise TypeError("SSE event delta was invalid")
                content = delta.get("content")
                if content is not None and not isinstance(content, str):
                    raise TypeError("SSE delta content was not text")
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                raise _SSEStreamProtocolError("invalid SSE completion event") from None
            if content is not None:
                parts.append(content)
        if not done_seen:
            raise _SSEStreamIncompleteError("SSE completion marker was missing")
        return "".join(parts)

    @staticmethod
    async def _notify_retry(observer: RetryObserver | None, retry: ModelRetry) -> None:
        if observer is not None:
            await observer(retry)

    def _retry_delay(self, retry_index: int, retry_after: str | None = None) -> float:
        exponential_delay = min(
            self.network_retry_max_delay_seconds,
            self.network_retry_base_delay_seconds * (2**retry_index),
        )
        # Equal jitter keeps retries from synchronizing while preserving at
        # least half of the bounded exponential delay.
        jitter = min(1.0, max(0.0, float(self._random_source())))
        backoff_delay = exponential_delay * (0.5 + 0.5 * jitter)
        retry_after_delay = _parse_retry_after(retry_after, now=self._now)
        if retry_after_delay is None:
            return backoff_delay
        # A valid gateway hint is a lower bound for the retry. Cap it so a
        # malformed or excessive date cannot hold a worker indefinitely.
        return max(backoff_delay, min(retry_after_delay, self.retry_after_max_seconds))


def _is_loopback_url(url: str) -> bool:
    hostname = urlparse(url).hostname
    if hostname is None:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _parse_retry_after(
    value: str | None,
    *,
    now: Callable[[], datetime] | None = None,
) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current = (now or (lambda: datetime.now(UTC)))()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        seconds = (retry_at - current).total_seconds()
    return seconds if seconds >= 0 else None


def _parse_json_object(content: str) -> dict[str, Any]:
    value = content.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else value
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise json.JSONDecodeError("no JSON object", value, 0)
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("JSON value was not object", value, start)
    return parsed


ScriptedResponse = (
    dict[str, Any]
    | Exception
    | Callable[[str, Sequence[dict[str, str]], str], dict[str, Any]]
)


class ScriptedModelClient:
    """Test-only model fake that proves each role gets a separate invocation."""

    def __init__(self, responses: dict[str, ScriptedResponse | list[ScriptedResponse]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.requests: list[tuple[str, list[dict[str, str]], str]] = []

    async def complete_json(
        self,
        model_alias: str,
        messages: Sequence[dict[str, str]],
        schema_name: str,
        *,
        on_retry: RetryObserver | None = None,
    ) -> dict[str, Any]:
        self.calls.append((model_alias, schema_name))
        self.requests.append((model_alias, [dict(message) for message in messages], schema_name))
        response = self.responses.get(model_alias)
        if response is None:
            raise ModelError(f"no scripted response for {model_alias}")
        if isinstance(response, list):
            if not response:
                raise ModelError(f"no scripted response remaining for {model_alias}")
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(model_alias, messages, schema_name)
        return dict(response)
