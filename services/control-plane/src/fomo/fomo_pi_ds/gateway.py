"""Run-scoped LiteLLM virtual-key management for Direct Pi.

Only the control plane may hold the LiteLLM master key. Generation sandbox G
receives the opaque :class:`RunVirtualKey` secret and can call only the two
stage-specific Direct Pi aliases;
provider credentials never cross this module's boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from math import isfinite
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from fomo.runtime_contract import (
    DEFAULT_PROFILE_ID,
    allowed_litellm_aliases,
    runtime_profile,
    selectable_litellm_aliases,
)

from .invocation import IDENTIFIER_PATTERN, MAX_IDENTIFIER_LENGTH, MAX_VIRTUAL_KEY_LENGTH

FOMO_PI_LITELLM_ALIAS = "fomo-pi-flash"
FOMO_PI_BUILD_LITELLM_ALIAS = "fomo-pi-build"
FOMO_PI_LITELLM_ALIASES = (
    FOMO_PI_LITELLM_ALIAS,
    FOMO_PI_BUILD_LITELLM_ALIAS,
)
FOMO_PI_SELECTABLE_LITELLM_ALIASES = selectable_litellm_aliases()
FOMO_PI_ALLOWED_LITELLM_ALIASES = allowed_litellm_aliases()
FOMO_PI_DEFAULT_PREFLIGHT_ALIASES = (
    runtime_profile(DEFAULT_PROFILE_ID).litellm_alias,
)
_KEY_ALIAS_PREFIX = "fomo-run-"
_PREFLIGHT_KEY_DURATION_SECONDS = 300
_PREFLIGHT_MAX_BUDGET = 0.10
_KEY_REVOCATION_ATTEMPTS = 3
_KEY_REVOCATION_RETRY_BASE_SECONDS = 0.25


class InferenceGatewayError(RuntimeError):
    """LiteLLM management failed without exposing a credential or response body."""


@dataclass(frozen=True, slots=True)
class RunVirtualKey:
    """One opaque, bounded credential issued for exactly one FOMO run."""

    run_id: str
    key_alias: str
    duration_seconds: int
    secret: str = field(repr=False)
    model_aliases: tuple[str, ...] = (FOMO_PI_LITELLM_ALIAS,)

    def __post_init__(self) -> None:
        if not self.secret or len(self.secret) > MAX_VIRTUAL_KEY_LENGTH:
            raise ValueError("virtual key secret must be non-empty and bounded")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.secret):
            raise ValueError("virtual key secret cannot contain control characters")
        if (
            not self.model_aliases
            or len(set(self.model_aliases)) != len(self.model_aliases)
            or any(
                alias not in FOMO_PI_ALLOWED_LITELLM_ALIASES
                for alias in self.model_aliases
            )
        ):
            raise ValueError("virtual key model aliases must be registered and unique")


class LiteLLMRunKeyClient:
    """Issue and block least-privilege virtual keys through LiteLLM management APIs."""

    def __init__(
        self,
        *,
        management_url: str,
        master_key: str,
        timeout_seconds: int = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._management_url = management_url.rstrip("/")
        self._validate_management_url(self._management_url)
        if not master_key or any(
            ord(character) < 32 or ord(character) == 127 for character in master_key
        ):
            raise ValueError("LiteLLM master key is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        self._master_key = master_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def issue(
        self,
        *,
        run_id: str,
        duration_seconds: int,
        max_budget: float,
        rpm_limit: int,
        tpm_limit: int,
        model_aliases: tuple[str, ...] | None = None,
    ) -> RunVirtualKey:
        self._validate_run_id(run_id)
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than 0")
        if not isfinite(max_budget) or max_budget <= 0:
            raise ValueError("max_budget must be greater than 0")
        if rpm_limit <= 0 or tpm_limit <= 0:
            raise ValueError("rpm_limit and tpm_limit must be greater than 0")
        resolved_aliases = model_aliases or (FOMO_PI_LITELLM_ALIAS,)
        if (
            not resolved_aliases
            or len(set(resolved_aliases)) != len(resolved_aliases)
            or any(
                alias not in FOMO_PI_ALLOWED_LITELLM_ALIASES
                for alias in resolved_aliases
            )
        ):
            raise ValueError("model_aliases must contain registered unique aliases")

        key_alias = f"{_KEY_ALIAS_PREFIX}{run_id}"
        payload = {
            "key_alias": key_alias,
            "models": list(resolved_aliases),
            "duration": f"{duration_seconds}s",
            "max_budget": max_budget,
            # The foreground model is idle while delegate_subtasks runs up to
            # three read-only child Pi processes against this same run key.
            "max_parallel_requests": 3,
            "rpm_limit": rpm_limit,
            "tpm_limit": tpm_limit,
            "metadata": {"fomo_run_id": run_id, "scope": "fomo-pi-ds"},
        }
        response = await self._post("/key/generate", payload)
        secret = response.get("key") if isinstance(response, dict) else None
        if not isinstance(secret, str) or not secret:
            raise InferenceGatewayError("LiteLLM key generation returned an invalid contract")
        try:
            return RunVirtualKey(
                run_id=run_id,
                key_alias=key_alias,
                duration_seconds=duration_seconds,
                secret=secret,
                model_aliases=resolved_aliases,
            )
        except ValueError as exc:
            raise InferenceGatewayError("LiteLLM returned an invalid virtual key") from exc

    async def block(self, virtual_key: RunVirtualKey) -> None:
        """Block a key with bounded retries while deferring caller cancellation.

        A revocation request is a cleanup fence, so cancellation must not abort
        an in-flight attempt and leave a still-valid credential behind. The
        caller's cancellation is re-raised after this bounded cleanup finishes.
        """

        task = asyncio.create_task(self._block_with_retries(virtual_key))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except InferenceGatewayError:
                # Read and normalize the completed task below so cancellation
                # and cleanup failure have one deterministic precedence rule.
                pass
        try:
            task.result()
        except InferenceGatewayError:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        if cancelled:
            raise asyncio.CancelledError

    async def _block_with_retries(self, virtual_key: RunVirtualKey) -> None:
        for attempt in range(_KEY_REVOCATION_ATTEMPTS):
            try:
                # LiteLLM's pinned OpenAPI permits either a token object or
                # JSON null on success. The 2xx status is authoritative.
                await self._post(
                    "/key/block",
                    {"key": virtual_key.secret},
                    expect_json=False,
                )
                return
            except InferenceGatewayError:
                if attempt + 1 == _KEY_REVOCATION_ATTEMPTS:
                    break
                await asyncio.sleep(_KEY_REVOCATION_RETRY_BASE_SECONDS * (2**attempt))
        raise InferenceGatewayError(
            "LiteLLM virtual key revocation failed after bounded retries"
        ) from None

    async def preflight(
        self,
        probe: Callable[[RunVirtualKey], Awaitable[None]],
        *,
        model_aliases: tuple[str, ...] = FOMO_PI_DEFAULT_PREFLIGHT_ALIASES,
    ) -> None:
        """Prove the complete Direct Pi gateway path without exposing credentials.

        The probe intentionally uses a short-lived, least-privilege virtual key
        and delegates the actual model calls to the supplied sandbox probe.
        Every error emitted here is synthesized locally; provider and gateway
        response bodies are never included.
        """

        if (
            not model_aliases
            or len(set(model_aliases)) != len(model_aliases)
            or any(alias not in FOMO_PI_SELECTABLE_LITELLM_ALIASES for alias in model_aliases)
        ):
            raise ValueError("preflight model aliases must be selectable and unique")
        aliases = await self._model_aliases()
        missing = sorted(set(model_aliases) - aliases)
        if missing:
            raise InferenceGatewayError(
                "LiteLLM preflight is missing required model aliases: " + ", ".join(missing)
            )

        virtual_key: RunVirtualKey | None = None
        primary_error: InferenceGatewayError | None = None
        revocation_error: InferenceGatewayError | None = None
        try:
            virtual_key = await self.issue(
                run_id=f"preflight-{uuid4().hex}",
                duration_seconds=_PREFLIGHT_KEY_DURATION_SECONDS,
                max_budget=_PREFLIGHT_MAX_BUDGET,
                rpm_limit=len(model_aliases) + 1,
                tpm_limit=1_250_000,
                model_aliases=model_aliases,
            )
            await probe(virtual_key)
        except InferenceGatewayError as exc:
            primary_error = exc
        except asyncio.CancelledError:
            raise
        except Exception:
            primary_error = InferenceGatewayError(
                "Direct Pi sandbox gateway probe failed"
            )
        finally:
            if virtual_key is not None:
                try:
                    await self.block(virtual_key)
                except InferenceGatewayError as exc:
                    revocation_error = exc

        if revocation_error is not None:
            if primary_error is not None:
                raise InferenceGatewayError(
                    "LiteLLM preflight failed and virtual key revocation also failed"
                ) from None
            raise InferenceGatewayError("LiteLLM preflight virtual key revocation failed") from None
        if primary_error is not None:
            raise primary_error

    async def _model_aliases(self) -> set[str]:
        response = await self._request(
            "GET",
            "/v1/models",
            bearer_token=self._master_key,
            error_prefix="LiteLLM preflight model discovery",
        )
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list):
            raise InferenceGatewayError(
                "LiteLLM preflight model discovery returned an invalid contract"
            )
        return {
            identifier
            for item in data
            if isinstance(item, dict) and isinstance((identifier := item.get("id")), str)
        }

    async def discover_model_aliases(self) -> set[str]:
        """Return gateway model ids without provider configuration or credentials."""
        return await self._model_aliases()

    async def _post(
        self, path: str, payload: dict[str, Any], *, expect_json: bool = True
    ) -> Any:
        return await self._request(
            "POST",
            path,
            bearer_token=self._master_key,
            json=payload,
            expect_json=expect_json,
            error_prefix="LiteLLM management",
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        bearer_token: str,
        error_prefix: str,
        json: dict[str, Any] | None = None,
        expect_json: bool = True,
        timeout_seconds: int | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {bearer_token}"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(
                base_url=self._management_url,
                headers=headers,
                timeout=timeout_seconds or self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.request(method, path, json=json)
        except httpx.HTTPError:
            raise InferenceGatewayError(f"{error_prefix} request failed") from None
        if not 200 <= response.status_code < 300:
            raise InferenceGatewayError(
                f"{error_prefix} request failed with HTTP {response.status_code}"
            )
        if not expect_json:
            return None
        try:
            return response.json()
        except ValueError:
            # JSON decoder errors can embed response excerpts in their repr.
            # Preserve only our locally synthesized, credential-free message.
            raise InferenceGatewayError(f"{error_prefix} response was not JSON") from None

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if (
            not run_id
            or len(run_id) + len(_KEY_ALIAS_PREFIX) > MAX_IDENTIFIER_LENGTH
            or not IDENTIFIER_PATTERN.fullmatch(run_id)
        ):
            raise ValueError("run_id is not a valid bounded identifier")

    @staticmethod
    def _validate_management_url(value: str) -> None:
        parsed = urlparse(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("management_url must contain a valid port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or port is not None
            and not 1 <= port <= 65_535
        ):
            raise ValueError("management_url must be an http(s) origin without userinfo")

    def __repr__(self) -> str:
        return (
            f"LiteLLMRunKeyClient(management_url={self._management_url!r}, "
            f"timeout_seconds={self._timeout_seconds!r})"
        )
