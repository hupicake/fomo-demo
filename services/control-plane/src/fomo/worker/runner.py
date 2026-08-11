"""Standalone worker process; the API process never executes generated code."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import urlparse

from fomo.agent_framework import (
    AgentTransportRegistry,
    normalize_agent_framework,
    resolve_run_agent_framework,
)
from fomo.config import Settings
from fomo.direct_pi import DirectPiOrchestrator
from fomo.fomo_pi_ds import (
    LiteLLMRunKeyClient,
    OpenSandboxCodexTransport,
    OpenSandboxOpenCodeTransport,
    OpenSandboxPiTransport,
)
from fomo.persistence import Database, Repository, SandboxCleanupTarget
from fomo.runtime_contract import runtime_profile
from fomo.runtime_preflight import DirectPiRuntimePreflight
from fomo.sandbox import (
    OpenSandboxProvider,
    SandboxProvider,
    SandboxRef,
    create_opensandbox_provider,
)

logger = logging.getLogger(__name__)
_PREFLIGHT_SUCCESS_TTL_SECONDS = 60.0
_PREFLIGHT_RETRY_BASE_SECONDS = 1.0
_PREFLIGHT_RETRY_MAX_SECONDS = 30.0


class RunOrchestrator(Protocol):
    async def run(self, run_id: str, *, lease_token: str | None = None) -> None: ...


def _is_loopback_host(hostname: str | None) -> bool:
    """Return whether a provider host resolves only to the caller's namespace."""
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if (
        normalized == "localhost"
        or normalized.endswith(".localhost")
        or normalized == "localhost.localdomain"
    ):
        return True
    # ``ipaddress`` intentionally rejects abbreviated IPv4 forms such as
    # 127.1 even though URL clients may normalize them to loopback.
    if normalized.startswith("127.") and all(
        part.isdigit() for part in normalized.split(".")
    ):
        return True
    try:
        address = ipaddress.ip_address(normalized.split("%", 1)[0])
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


def _validate_opensandbox_pi_provider_url(settings: Settings) -> None:
    hostname = urlparse(settings.pi_provider_base_url).hostname
    if _is_loopback_host(hostname):
        raise ValueError(
            "OpenSandbox cannot reach the Direct Pi provider through a loopback host; "
            "set SANDBOX_LITELLM_BASE_URL to an http(s) address reachable from the "
            "sandbox (for local Docker, http://host.docker.internal:4000)"
        )


class WorkerRunner:
    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        *,
        sandbox: SandboxProvider | None = None,
        direct_orchestrator: RunOrchestrator | None = None,
        runtime_preflight: Callable[[], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        worker_id: str | None = None,
    ) -> None:
        if settings.worker_lease_seconds <= 0:
            raise ValueError("WORKER_LEASE_SECONDS must be positive")
        if sandbox is None:
            _validate_opensandbox_pi_provider_url(settings)
        self.repository = repository
        self.settings = settings
        self.sandbox = sandbox or create_opensandbox_provider(settings)
        self._runtime_preflight = runtime_preflight
        self._monotonic = monotonic
        self._preflight_ready_until = 0.0
        self._preflight_next_attempt_at = 0.0
        self._preflight_retry_seconds = _PREFLIGHT_RETRY_BASE_SECONDS
        if direct_orchestrator is not None:
            self.direct_orchestrator = direct_orchestrator
        else:
            if not settings.opensandbox_api_key:
                raise ValueError("coding-agent worker requires OPENSANDBOX_API_KEY")
            if not isinstance(self.sandbox, OpenSandboxProvider):
                raise ValueError("coding-agent production requires OpenSandbox")
            if not settings.litellm_api_key:
                raise ValueError("coding-agent worker requires a LiteLLM master key")
            gateway = LiteLLMRunKeyClient(
                management_url=settings.litellm_management_url,
                master_key=settings.litellm_api_key,
                timeout_seconds=settings.inference_management_timeout_seconds,
            )
            enabled_frameworks = tuple(
                normalize_agent_framework(framework)
                for framework in settings.agent_enabled_frameworks
            )
            transports = {}
            pi_transport = OpenSandboxPiTransport(
                self.sandbox,
                # This is the provider resource lifetime, not a FOMO run
                # budget. Individual turns have no bridge wall timer.
                default_timeout_seconds=settings.opensandbox_lifetime_seconds,
                stderr_limit_bytes=settings.command_output_limit_bytes,
            )
            if "pi" in enabled_frameworks:
                transports["pi"] = pi_transport
            if "opencode" in enabled_frameworks:
                transports["opencode"] = OpenSandboxOpenCodeTransport(
                    self.sandbox,
                    default_timeout_seconds=settings.opensandbox_lifetime_seconds,
                    stderr_limit_bytes=settings.command_output_limit_bytes,
                )
            if "codex" in enabled_frameworks:
                transports["codex"] = OpenSandboxCodexTransport(
                    self.sandbox,
                    default_timeout_seconds=settings.opensandbox_lifetime_seconds,
                    stderr_limit_bytes=settings.command_output_limit_bytes,
                )
            transport_registry = AgentTransportRegistry(transports)
            # The startup probe validates the shared run-scoped LiteLLM route
            # before a queued run is claimed.
            if self._runtime_preflight is None:
                self._runtime_preflight = DirectPiRuntimePreflight(
                    gateway=gateway,
                    sandbox=self.sandbox,
                    transport=pi_transport,
                    provider_base_url=settings.pi_provider_base_url,
                    sandbox_ready_timeout_seconds=settings.opensandbox_ready_timeout_seconds,
                    sandbox_lifetime_seconds=settings.opensandbox_lifetime_seconds,
                    management_timeout_seconds=settings.inference_management_timeout_seconds,
                    model_aliases=tuple(
                        runtime_profile(profile_id).litellm_alias
                        for profile_id in settings.runtime_enabled_profiles
                    ),
                )
            self.direct_orchestrator = DirectPiOrchestrator(
                repository,
                self.sandbox,
                settings,
                gateway,
                transport_registry,
            )
        self.worker_id = worker_id or f"{socket.gethostname()}:{id(self)}"

    async def run_once(self) -> bool:
        await self._recover_expired_runs()
        if self._runtime_preflight is not None:
            if not await self.repository.has_claimable_run():
                return False
            if not await self._ensure_runtime_ready():
                return False
        run = await self.repository.claim_next_run(self.worker_id, self.settings.worker_lease_seconds)
        if run is None:
            return False
        lease_token = run.lease_owner
        if not lease_token:
            raise RuntimeError("claimed run is missing its lease token")
        claimed_framework = getattr(run, "agent_framework", None)
        framework = (
            normalize_agent_framework(claimed_framework)
            if claimed_framework is not None
            else await resolve_run_agent_framework(self.repository, run.id)
        )
        task_name = f"fomo-{framework}-agent:{run.id}"
        lease_lost = asyncio.Event()
        run_task = asyncio.create_task(
            self.direct_orchestrator.run(run.id, lease_token=lease_token),
            name=task_name,
        )
        heartbeat = asyncio.create_task(
            self._renew_lease_forever(run.id, lease_token, run_task, lease_lost),
            name=f"fomo-lease-heartbeat:{run.id}",
        )
        try:
            await run_task
        except asyncio.CancelledError:
            # A heartbeat-induced cancel means the durable fence/recovery now
            # owns this run. A real process/task cancellation must still reach
            # the caller so shutdown semantics stay intact.
            current = asyncio.current_task()
            externally_cancelling = current is not None and current.cancelling() > 0
            if not lease_lost.is_set() or externally_cancelling:
                raise
        except Exception:
            # The selected orchestrator persisted a safe terminal status. Avoid raw
            # exception data because model/provider failures may contain it.
            logger.error("FOMO run failed", extra={"run_id": run.id})
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True

    async def _ensure_runtime_ready(self) -> bool:
        """Probe before claim, caching success and backing off without touching runs."""

        assert self._runtime_preflight is not None
        now = self._monotonic()
        if now < self._preflight_ready_until:
            return True
        if now < self._preflight_next_attempt_at:
            return False
        try:
            await self._runtime_preflight()
        except Exception:
            self._preflight_ready_until = 0.0
            self._preflight_next_attempt_at = self._monotonic() + self._preflight_retry_seconds
            self._preflight_retry_seconds = min(
                _PREFLIGHT_RETRY_MAX_SECONDS,
                self._preflight_retry_seconds * 2,
            )
            # Never attach the exception: HTTP clients may retain credentials or
            # provider response bodies even when their string form looks safe.
            logger.warning("FOMO runtime preflight failed; worker will retry before claiming work")
            return False
        self._preflight_ready_until = self._monotonic() + _PREFLIGHT_SUCCESS_TTL_SECONDS
        self._preflight_next_attempt_at = 0.0
        self._preflight_retry_seconds = _PREFLIGHT_RETRY_BASE_SECONDS
        return True

    async def _renew_lease_forever(
        self,
        run_id: str,
        lease_token: str,
        run_task: asyncio.Task[None],
        lease_lost: asyncio.Event,
    ) -> None:
        """Keep one claimed run recoverable without allowing it to go stale."""
        interval = max(0.1, min(5.0, self.settings.worker_lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.repository.renew_lease(
                    run_id,
                    lease_token,
                    self.settings.worker_lease_seconds,
                )
            except Exception:
                # Do not expose provider/database request details in worker logs.
                # Fail closed: the local SOP must stop because it can no
                # longer prove it owns the durable write fence.
                logger.error("FOMO lease heartbeat failed", extra={"run_id": run_id})
                lease_lost.set()
                if not run_task.done():
                    run_task.cancel()
                return
            if not renewed:
                if await self.repository.is_user_input_suspended(run_id):
                    # The Pi turn settled and atomically released its lease at
                    # a durable clarification boundary. Let the orchestrator
                    # unwind without treating this expected handoff as loss.
                    return
                lease_lost.set()
                if not run_task.done():
                    run_task.cancel()
                return

    async def _recover_expired_runs(self) -> None:
        """Converge abandoned work before accepting new work for the project."""
        recovered, registered_cleanup, pending_cleanup = await asyncio.gather(
            self.repository.recover_expired_running_runs(),
            self.repository.list_sandbox_cleanup_targets(),
            self.repository.list_terminal_sandbox_cleanup_targets(),
        )
        attempted: set[tuple[str, str]] = set()
        for target in [*recovered, *registered_cleanup, *pending_cleanup]:
            key = (target.run_id, target.sandbox_id)
            if key in attempted:
                continue
            attempted.add(key)
            await self._destroy_stale_sandbox(target)

    async def _destroy_stale_sandbox(self, target: SandboxCleanupTarget) -> bool:
        """Destroy through the provider so remote handles are reconnected if needed."""
        ref = SandboxRef(id=target.sandbox_id, project_id=target.project_id)
        try:
            # OpenSandbox.kill() reconnects from this durable ref when this
            # worker did not create the sandbox itself.
            await self.sandbox.kill(ref)
        except Exception:
            # Leave sandbox_id durable so a later worker can retry cleanup.
            logger.warning(
                "FOMO stale sandbox cleanup failed",
                extra={"run_id": target.run_id, "sandbox_id": target.sandbox_id},
            )
            return False
        try:
            if target.resource_id is not None:
                await self.repository.acknowledge_sandbox_cleanup(target.resource_id)
            await self.repository.clear_sandbox_id(target.run_id, target.sandbox_id)
        except Exception:
            # The provider resource is already gone. Retaining the reference
            # causes a harmless later idempotent cleanup attempt.
            logger.warning(
                "FOMO stale sandbox cleanup acknowledgement failed",
                extra={"run_id": target.run_id, "sandbox_id": target.sandbox_id},
            )
            return False
        return True

    async def run_forever(self) -> None:
        await self.repository.initialize()
        while True:
            worked = await self.run_once()
            if not worked:
                await asyncio.sleep(self.settings.worker_poll_interval_seconds)


def run() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_url)
    repository = Repository(database)
    try:
        asyncio.run(WorkerRunner(repository, settings).run_forever())
    finally:
        # asyncio.run has already cleaned the loop; engine cleanup is best-effort on process shutdown.
        pass
