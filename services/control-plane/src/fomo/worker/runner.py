"""Standalone worker process; the API process never executes generated code."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Protocol

from fomo.agent_runtime import MetaGPTAdapter, ModelClient, OpenAICompatibleClient, SOPRunner
from fomo.config import Settings
from fomo.direct_pi import DirectPiOrchestrator
from fomo.fomo_pi_ds import LiteLLMRunKeyClient, OpenSandboxPiTransport
from fomo.persistence import Database, Repository, SandboxCleanupTarget
from fomo.sandbox import OpenSandboxProvider, SandboxProvider, SandboxRef, create_sandbox_provider

logger = logging.getLogger(__name__)


class RunOrchestrator(Protocol):
    async def run(self, run_id: str, *, lease_token: str | None = None) -> None: ...


class WorkerRunner:
    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        *,
        model: ModelClient | None = None,
        sandbox: SandboxProvider | None = None,
        agent_adapter: MetaGPTAdapter | None = None,
        direct_orchestrator: RunOrchestrator | None = None,
        worker_id: str | None = None,
    ) -> None:
        if settings.worker_lease_seconds <= 0:
            raise ValueError("WORKER_LEASE_SECONDS must be positive")
        self.repository = repository
        self.settings = settings
        self.sandbox = sandbox or create_sandbox_provider(settings)
        self.direct_orchestrator: RunOrchestrator | None = None
        self.model: ModelClient | None = None
        if settings.agent_framework == "direct_pi":
            if agent_adapter is not None or model is not None:
                raise ValueError("direct_pi does not accept a legacy model or MetaGPT adapter")
            if direct_orchestrator is not None:
                self.direct_orchestrator = direct_orchestrator
            else:
                if not isinstance(self.sandbox, OpenSandboxProvider):
                    raise ValueError("direct_pi production requires OpenSandbox")
                if not settings.litellm_api_key:
                    raise ValueError("direct_pi requires a LiteLLM master key")
                gateway = LiteLLMRunKeyClient(
                    management_url=settings.litellm_management_url,
                    master_key=settings.litellm_api_key,
                    timeout_seconds=settings.inference_management_timeout_seconds,
                )
                transport = OpenSandboxPiTransport(
                    self.sandbox,
                    default_timeout_seconds=settings.run_max_wall_seconds,
                    stderr_limit_bytes=settings.command_output_limit_bytes,
                )
                self.direct_orchestrator = DirectPiOrchestrator(
                    repository, self.sandbox, settings, gateway, transport
                )
            self.agent_adapter = None
        elif settings.agent_framework in {"metagpt", "native"}:
            if direct_orchestrator is not None:
                raise ValueError("legacy agent framework cannot receive a Direct Pi orchestrator")
            self.model = model or OpenAICompatibleClient(
                settings.litellm_base_url,
                api_key=settings.litellm_api_key,
                timeout_seconds=settings.model_request_timeout_seconds,
                network_retries=settings.model_network_retries,
                network_retry_base_delay_seconds=settings.model_network_retry_base_delay_seconds,
                network_retry_max_delay_seconds=settings.model_network_retry_max_delay_seconds,
                retry_after_max_seconds=settings.model_retry_after_max_seconds,
            )
        else:
            raise ValueError("AGENT_FRAMEWORK must be direct_pi, metagpt, or native")
        if settings.agent_framework == "metagpt":
            assert self.model is not None
            # Fail at worker construction when the explicitly selected default
            # is unavailable; never run a fake native fallback in production.
            self.agent_adapter = agent_adapter or MetaGPTAdapter(self.model)
        elif settings.agent_framework == "native":
            if agent_adapter is not None:
                raise ValueError("native agent framework cannot receive a MetaGPT adapter")
            self.agent_adapter = None
        self.worker_id = worker_id or f"{socket.gethostname()}:{id(self)}"

    async def run_once(self) -> bool:
        await self._recover_expired_runs()
        run = await self.repository.claim_next_run(self.worker_id, self.settings.worker_lease_seconds)
        if run is None:
            return False
        if self.direct_orchestrator is not None:
            orchestrator = self.direct_orchestrator
            task_name = f"fomo-direct-pi:{run.id}"
        else:
            assert self.model is not None
            orchestrator = SOPRunner(
                self.repository,
                self.model,
                self.sandbox,
                self.settings,
                agent_adapter=self.agent_adapter,
            )
            task_name = f"fomo-legacy-sop:{run.id}"
        lease_token = run.lease_owner
        if not lease_token:
            raise RuntimeError("claimed run is missing its lease token")
        lease_lost = asyncio.Event()
        run_task = asyncio.create_task(
            orchestrator.run(run.id, lease_token=lease_token),
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
                lease_lost.set()
                if not run_task.done():
                    run_task.cancel()
                return

    async def _recover_expired_runs(self) -> None:
        """Converge abandoned work before accepting new work for the project."""
        recovered = await self.repository.recover_expired_running_runs()
        pending_cleanup = await self.repository.list_terminal_sandbox_cleanup_targets()
        seen: set[tuple[str, str]] = set()
        for target in [*recovered, *pending_cleanup]:
            key = (target.run_id, target.sandbox_id)
            if key in seen:
                continue
            seen.add(key)
            await self._destroy_stale_sandbox(target)

    async def _destroy_stale_sandbox(self, target: SandboxCleanupTarget) -> None:
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
            return
        try:
            await self.repository.clear_sandbox_id(target.run_id, target.sandbox_id)
        except Exception:
            # The provider resource is already gone. Retaining the reference
            # causes a harmless later idempotent cleanup attempt.
            logger.warning(
                "FOMO stale sandbox cleanup acknowledgement failed",
                extra={"run_id": target.run_id, "sandbox_id": target.sandbox_id},
            )

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
