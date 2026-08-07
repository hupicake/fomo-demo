"""LiteLLM OpenAI-compatible client using logical model aliases only."""

from __future__ import annotations

import asyncio
import ipaddress
import json
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


_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


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
        network_retries: int = 2,
        network_retry_base_delay_seconds: float = 0.5,
        network_retry_max_delay_seconds: float = 4.0,
        retry_after_max_seconds: float = 30.0,
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
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {
            "model": model_alias,
            "messages": list(messages),
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        payload = await self._request_json(endpoint, headers, body, on_retry=on_retry)
        try:
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part) for part in content
                )
            if not isinstance(content, str):
                raise TypeError("model content was not text")
            return _parse_json_object(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            # The response content can be included in a JSONDecodeError's
            # diagnostic context. Keep the worker's exception path free of
            # response-body data as well as request credentials.
            raise ModelError(f"model did not return a {schema_name} JSON object") from None

    async def _request_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, Any],
        *,
        on_retry: RetryObserver | None,
    ) -> dict[str, Any]:
        for attempt in range(self.network_retries + 1):
            try:
                # A sourced local dotenv may set HTTP_PROXY for external traffic.
                # Never send localhost LiteLLM through that proxy; external gateway
                # URLs retain normal proxy support from the environment.
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    trust_env=not _is_loopback_url(endpoint),
                ) as client:
                    response = await client.post(endpoint, headers=headers, json=body)
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt < self.network_retries:
                        delay_seconds = self._retry_delay(attempt, response.headers.get("Retry-After"))
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
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("model response was not an object")
                return payload
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                if attempt < self.network_retries:
                    delay_seconds = self._retry_delay(attempt)
                    await self._notify_retry(
                        on_retry,
                        ModelRetry(
                            attempt=attempt + 1,
                            max_attempts=self.network_retries + 1,
                            delay_seconds=delay_seconds,
                            failure_kind="transport",
                            transport_error=type(exc).__name__,
                        ),
                    )
                    await asyncio.sleep(delay_seconds)
                    continue
                raise ModelRequestError(
                    f"model request failed after {attempt + 1} attempts ({type(exc).__name__})",
                    attempts=attempt + 1,
                    failure_kind="transport",
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
    async def _notify_retry(observer: RetryObserver | None, retry: ModelRetry) -> None:
        if observer is not None:
            await observer(retry)

    def _retry_delay(self, retry_index: int, retry_after: str | None = None) -> float:
        retry_after_delay = _parse_retry_after(retry_after)
        if retry_after_delay is not None and retry_after_delay <= self.retry_after_max_seconds:
            return retry_after_delay
        return min(
            self.network_retry_max_delay_seconds,
            self.network_retry_base_delay_seconds * (2**retry_index),
        )


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


def _parse_retry_after(value: str | None) -> float | None:
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
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
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
