"""One budgeted Direct Pi session reused across planning, build, and repair turns."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from fomo.config import Settings
from fomo.fomo_pi_ds import (
    PiBridgeEnvelope,
    PiInvocation,
    PiRequest,
    PiTransportCancelled,
    PiTransportResult,
    RunVirtualKey,
)
from fomo.ids import uuid7
from fomo.persistence import Repository, RunLeaseLost
from fomo.sandbox.base import SandboxRef

from .execution import PiEventWriter, redact


class PiTransport(Protocol):
    async def run(
        self,
        ref: SandboxRef,
        invocation: PiInvocation,
        *,
        on_event: Callable[[PiBridgeEnvelope], Awaitable[None]] | None = None,
        on_diagnostic: Callable[[str], Awaitable[None]] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> PiTransportResult: ...


class DirectPiSessionError(RuntimeError):
    pass


class DirectPiCancelled(DirectPiSessionError):
    pass


class DirectPiSession:
    def __init__(
        self,
        repository: Repository,
        transport: PiTransport,
        settings: Settings,
        virtual_key: RunVirtualKey,
        *,
        run_id: str,
        lease_token: str,
        started_at: float,
    ) -> None:
        self.repository = repository
        self.transport = transport
        self.settings = settings
        self.virtual_key = virtual_key
        self.run_id = run_id
        self.lease_token = lease_token
        self.started_at = started_at
        self.session_id = f"fomo-{run_id}"
        self._writer = PiEventWriter(
            repository, run_id=run_id, lease_token=lease_token
        )

    async def invoke(self, ref: SandboxRef, prompt: str, *, stage: str) -> str:
        await self._check_active()
        remaining = self.settings.run_max_wall_seconds - (time.monotonic() - self.started_at)
        if remaining <= 0:
            raise DirectPiSessionError("Direct Pi run exceeded its wall-clock budget")
        request = PiRequest(
            request_id=uuid7(),
            correlation_id=self.run_id,
            session_id=self.session_id,
            provider_base_url=self.settings.pi_provider_base_url,
            prompt=prompt,
            virtual_key=self.virtual_key.secret,
            timeout_seconds=max(1, math.floor(remaining)),
        )
        cancel_event = asyncio.Event()
        diagnostic: list[str] = []

        async def on_diagnostic(value: str) -> None:
            if sum(len(item) for item in diagnostic) < 16_000:
                diagnostic.append(redact(value)[:4096])

        watcher = asyncio.create_task(self._watch_cancel(cancel_event))
        try:
            result = await self.transport.run(
                ref,
                PiInvocation(request),
                on_event=self._writer,
                on_diagnostic=on_diagnostic,
                cancel_event=cancel_event,
            )
        except PiTransportCancelled:
            if await self.repository.is_cancel_requested(self.run_id):
                raise DirectPiCancelled("Direct Pi run was cancelled") from None
            raise RunLeaseLost("run lease was lost while Direct Pi was active") from None
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await self._check_active()
        self._check_budget(result)
        if diagnostic:
            await self.repository.append_event(
                self.run_id,
                "pi.diagnostic",
                payload={"stage": stage, "message": "".join(diagnostic)[:16_000]},
                lease_token=self.lease_token,
            )
        text = self._last_assistant_text(result)
        if not text:
            raise DirectPiSessionError("Direct Pi completed without a public assistant result")
        return text

    async def _watch_cancel(self, cancel_event: asyncio.Event) -> None:
        while not cancel_event.is_set():
            active, cancelled = await asyncio.gather(
                self.repository.is_active_lease(self.run_id, self.lease_token),
                self.repository.is_cancel_requested(self.run_id),
            )
            if not active or cancelled:
                cancel_event.set()
                return
            await asyncio.sleep(0.5)

    async def _check_active(self) -> None:
        if not await self.repository.is_active_lease(self.run_id, self.lease_token):
            raise RunLeaseLost("run lease is no longer active")
        if await self.repository.is_cancel_requested(self.run_id):
            raise DirectPiCancelled("Direct Pi run was cancelled")

    def _check_budget(self, result: PiTransportResult) -> None:
        stats = result.bridge.completed.get("stats")
        if not isinstance(stats, dict):
            raise DirectPiSessionError("Direct Pi did not report final usage")
        tokens = stats.get("tokens")
        token_total = tokens.get("total") if isinstance(tokens, dict) else None
        tool_calls = stats.get("toolCalls")
        cost = stats.get("cost")
        if not isinstance(token_total, (int, float)) or token_total < 0:
            raise DirectPiSessionError("Direct Pi token usage is invalid")
        if not isinstance(tool_calls, int) or tool_calls < 0:
            raise DirectPiSessionError("Direct Pi tool usage is invalid")
        if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
            raise DirectPiSessionError("Direct Pi cost usage is invalid")
        if token_total > self.settings.run_max_tokens:
            raise DirectPiSessionError("Direct Pi exceeded the run token budget")
        if tool_calls > self.settings.run_max_tool_calls:
            raise DirectPiSessionError("Direct Pi exceeded the run tool-call budget")
        if cost > self.settings.run_max_spend:
            raise DirectPiSessionError("Direct Pi exceeded the run spend budget")

    @staticmethod
    def _last_assistant_text(result: PiTransportResult) -> str:
        texts = [
            str(event.payload.get("text", "")).strip()
            for event in result.bridge.events
            if event.type == "pi.event"
            and event.payload.get("kind") == "turn_end"
            and event.payload.get("role") == "assistant"
        ]
        return next((value for value in reversed(texts) if value), "")
