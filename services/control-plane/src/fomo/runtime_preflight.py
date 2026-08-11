"""Secret-safe runtime readiness probe for the complete Direct Pi path."""

from __future__ import annotations

import asyncio
import sys
from uuid import uuid4

from fomo.config import Settings
from fomo.fomo_pi_ds import (
    FOMO_PI_DEFAULT_PREFLIGHT_ALIASES,
    InferenceGatewayError,
    LiteLLMRunKeyClient,
    OpenSandboxPiTransport,
    RunVirtualKey,
)
from fomo.runtime_contract import runtime_profile
from fomo.sandbox import OpenSandboxProvider, SandboxRef, create_opensandbox_provider

_PREFLIGHT_CREATE_MARGIN_SECONDS = 15
_PREFLIGHT_COMMAND_TIMEOUT_SECONDS = 195
_PREFLIGHT_COMMAND_HOST_MARGIN_SECONDS = 15
_PREFLIGHT_CLEANUP_TIMEOUT_SECONDS = 30
_PREFLIGHT_MANAGEMENT_CALL_BUDGET = 5
_PREFLIGHT_MANAGEMENT_RETRY_DELAY_BUDGET_SECONDS = 1
_PREFLIGHT_LIFETIME_HEADROOM_SECONDS = 15


class RuntimePreflightError(RuntimeError):
    """A synthesized readiness failure that contains no secret or response body."""


class DirectPiRuntimePreflight:
    """Prove OpenSandbox, execd, and every enabled sandbox-side model route."""

    def __init__(
        self,
        *,
        gateway: LiteLLMRunKeyClient,
        sandbox: OpenSandboxProvider,
        transport: OpenSandboxPiTransport,
        provider_base_url: str,
        sandbox_ready_timeout_seconds: int,
        sandbox_lifetime_seconds: int,
        management_timeout_seconds: int,
        model_aliases: tuple[str, ...] = FOMO_PI_DEFAULT_PREFLIGHT_ALIASES,
    ) -> None:
        if (
            sandbox_ready_timeout_seconds <= 0
            or sandbox_lifetime_seconds <= 0
            or management_timeout_seconds <= 0
        ):
            raise ValueError("runtime preflight sandbox timeouts must be positive")
        self._gateway = gateway
        self._sandbox = sandbox
        self._transport = transport
        self._provider_base_url = provider_base_url
        if not model_aliases or len(set(model_aliases)) != len(model_aliases):
            raise ValueError("runtime preflight model aliases must be non-empty and unique")
        self._model_aliases = model_aliases
        self._create_timeout_seconds = (
            sandbox_ready_timeout_seconds + _PREFLIGHT_CREATE_MARGIN_SECONDS
        )
        required_lifetime_seconds = (
            self._create_timeout_seconds
            + _PREFLIGHT_MANAGEMENT_CALL_BUDGET * management_timeout_seconds
            + _PREFLIGHT_MANAGEMENT_RETRY_DELAY_BUDGET_SECONDS
            + _PREFLIGHT_COMMAND_TIMEOUT_SECONDS
            + _PREFLIGHT_COMMAND_HOST_MARGIN_SECONDS
            + _PREFLIGHT_CLEANUP_TIMEOUT_SECONDS
            + _PREFLIGHT_LIFETIME_HEADROOM_SECONDS
        )
        if sandbox_lifetime_seconds < required_lifetime_seconds:
            raise ValueError(
                "OPENSANDBOX_LIFETIME_SECONDS must be at least "
                f"{required_lifetime_seconds} seconds for the complete runtime preflight"
            )
        self._temporary_lifetime_seconds = required_lifetime_seconds

    async def __call__(self) -> None:
        ref: SandboxRef | None = None
        primary_error: RuntimePreflightError | InferenceGatewayError | None = None
        cleanup_error: RuntimePreflightError | None = None
        cancellation: asyncio.CancelledError | None = None

        try:
            ref = await self._create_sandbox()

            async def probe(virtual_key: RunVirtualKey) -> None:
                assert ref is not None
                try:
                    async with asyncio.timeout(
                        _PREFLIGHT_COMMAND_TIMEOUT_SECONDS
                        + _PREFLIGHT_COMMAND_HOST_MARGIN_SECONDS
                    ):
                        await self._transport.preflight_gateway(
                            ref,
                            virtual_key,
                            provider_base_url=self._provider_base_url,
                            timeout_seconds=_PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise RuntimePreflightError(
                        "OpenSandbox runtime preflight gateway command failed"
                    ) from None

            await self._gateway.preflight(
                probe,
                model_aliases=self._model_aliases,
            )
        except asyncio.CancelledError as exc:
            cancellation = exc
        except (InferenceGatewayError, RuntimePreflightError) as exc:
            primary_error = exc
        except Exception:
            primary_error = RuntimePreflightError(
                "Direct Pi runtime preflight failed unexpectedly"
            )

        if ref is not None:
            try:
                await self._cleanup_sandbox(ref)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except RuntimePreflightError as exc:
                cleanup_error = exc

        if cancellation is not None:
            raise cancellation
        if cleanup_error is not None:
            if primary_error is not None:
                raise RuntimePreflightError(
                    "Direct Pi runtime preflight failed and temporary sandbox cleanup also failed"
                ) from None
            raise cleanup_error
        if primary_error is not None:
            raise primary_error

    async def _create_sandbox(self) -> SandboxRef:
        try:
            async with asyncio.timeout(self._create_timeout_seconds):
                return await self._sandbox.create(
                    f"runtime-preflight-{uuid4().hex}",
                    lifetime_seconds=self._temporary_lifetime_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # OpenSandboxProvider.create() destroys a partially created handle
            # before raising. Do not retain or stringify the SDK exception.
            raise RuntimePreflightError(
                "OpenSandbox runtime preflight could not create a temporary sandbox"
            ) from None

    async def _cleanup_sandbox(self, ref: SandboxRef) -> None:
        async def kill() -> None:
            async with asyncio.timeout(_PREFLIGHT_CLEANUP_TIMEOUT_SECONDS):
                await self._sandbox.kill(ref)

        task = asyncio.create_task(kill())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Finish the bounded destroy even when shutdown arrives during
                # cleanup, then restore cancellation to the caller.
                cancelled = True
            except Exception:
                # Normalize the completed SDK/timeout failure below without
                # ever stringifying it.
                pass
        try:
            task.result()
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise RuntimePreflightError(
                "OpenSandbox runtime preflight temporary sandbox cleanup failed"
            ) from None
        if cancelled:
            raise asyncio.CancelledError


async def preflight(settings: Settings) -> None:
    """Run the same complete readiness gate used before worker claims."""

    if not settings.litellm_api_key:
        raise InferenceGatewayError("LiteLLM runtime preflight requires a master key")
    if not settings.opensandbox_api_key:
        raise RuntimePreflightError("OpenSandbox runtime preflight requires an API key")
    sandbox = create_opensandbox_provider(settings)
    gateway = LiteLLMRunKeyClient(
        management_url=settings.litellm_management_url,
        master_key=settings.litellm_api_key,
        timeout_seconds=settings.inference_management_timeout_seconds,
    )
    transport = OpenSandboxPiTransport(
        sandbox,
        default_timeout_seconds=settings.opensandbox_lifetime_seconds,
        stderr_limit_bytes=settings.command_output_limit_bytes,
    )
    await DirectPiRuntimePreflight(
        gateway=gateway,
        sandbox=sandbox,
        transport=transport,
        provider_base_url=settings.pi_provider_base_url,
        sandbox_ready_timeout_seconds=settings.opensandbox_ready_timeout_seconds,
        sandbox_lifetime_seconds=settings.opensandbox_lifetime_seconds,
        management_timeout_seconds=settings.inference_management_timeout_seconds,
        model_aliases=tuple(
            runtime_profile(profile_id).litellm_alias
            for profile_id in settings.runtime_enabled_profiles
        ),
    )()


def run() -> None:
    try:
        settings = Settings.from_env()
        asyncio.run(preflight(settings))
    except (InferenceGatewayError, RuntimePreflightError, ValueError) as exc:
        # These exception messages are synthesized locally and deliberately
        # contain neither credentials nor response bodies.
        print(f"FOMO runtime preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        # Never stringify an unknown exception: third-party clients may attach
        # request headers or response bodies to it.
        print(
            f"FOMO runtime preflight failed with {type(exc).__name__}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(
        "FOMO runtime preflight passed: OpenSandbox and all enabled sandbox-side model aliases are ready."
    )
