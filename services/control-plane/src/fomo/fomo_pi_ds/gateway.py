"""Run-scoped LiteLLM virtual-key management for Direct Pi.

Only the control plane may hold the LiteLLM master key. Generation sandbox G
receives the opaque :class:`RunVirtualKey` secret and can call only the two
stage-specific Direct Pi aliases;
provider credentials never cross this module's boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any
from urllib.parse import urlparse

import httpx

from .invocation import IDENTIFIER_PATTERN, MAX_IDENTIFIER_LENGTH, MAX_VIRTUAL_KEY_LENGTH

FOMO_PI_LITELLM_ALIAS = "fomo-pi-flash"
FOMO_PI_BUILD_LITELLM_ALIAS = "fomo-pi-build"
FOMO_PI_LITELLM_ALIASES = (
    FOMO_PI_LITELLM_ALIAS,
    FOMO_PI_BUILD_LITELLM_ALIAS,
)
_KEY_ALIAS_PREFIX = "fomo-run-"


class InferenceGatewayError(RuntimeError):
    """LiteLLM management failed without exposing a credential or response body."""


@dataclass(frozen=True, slots=True)
class RunVirtualKey:
    """One opaque, bounded credential issued for exactly one FOMO run."""

    run_id: str
    key_alias: str
    duration_seconds: int
    secret: str = field(repr=False)
    model_aliases: tuple[str, ...] = FOMO_PI_LITELLM_ALIASES

    def __post_init__(self) -> None:
        if not self.secret or len(self.secret) > MAX_VIRTUAL_KEY_LENGTH:
            raise ValueError("virtual key secret must be non-empty and bounded")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.secret):
            raise ValueError("virtual key secret cannot contain control characters")


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
    ) -> RunVirtualKey:
        self._validate_run_id(run_id)
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than 0")
        if not isfinite(max_budget) or max_budget <= 0:
            raise ValueError("max_budget must be greater than 0")
        if rpm_limit <= 0 or tpm_limit <= 0:
            raise ValueError("rpm_limit and tpm_limit must be greater than 0")

        key_alias = f"{_KEY_ALIAS_PREFIX}{run_id}"
        payload = {
            "key_alias": key_alias,
            "models": list(FOMO_PI_LITELLM_ALIASES),
            "duration": f"{duration_seconds}s",
            "max_budget": max_budget,
            "max_parallel_requests": 1,
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
            )
        except ValueError as exc:
            raise InferenceGatewayError("LiteLLM returned an invalid virtual key") from exc

    async def block(self, virtual_key: RunVirtualKey) -> None:
        # LiteLLM's pinned OpenAPI permits either a token object or JSON null on
        # a successful block. The 2xx status is the authoritative contract.
        await self._post("/key/block", {"key": virtual_key.secret}, expect_json=False)

    async def _post(
        self, path: str, payload: dict[str, Any], *, expect_json: bool = True
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self._master_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._management_url,
                headers=headers,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(path, json=payload)
        except httpx.HTTPError:
            raise InferenceGatewayError("LiteLLM management request failed") from None
        if not 200 <= response.status_code < 300:
            raise InferenceGatewayError(
                f"LiteLLM management request failed with HTTP {response.status_code}"
            )
        if not expect_json:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise InferenceGatewayError("LiteLLM management response was not JSON") from exc

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
