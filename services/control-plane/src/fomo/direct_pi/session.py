"""One policy-bounded Direct Pi session reused across planning, build, and repair turns."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fomo.config import Settings
from fomo.fomo_pi_ds import (
    PiBridgeEnvelope,
    PiBridgeFailed,
    PiInvocation,
    PiRequest,
    PiTransportCancelled,
    PiTransportResult,
    RunVirtualKey,
)
from fomo.ids import uuid7
from fomo.persistence import Repository, RunLeaseLost
from fomo.runtime_contract import RuntimeContract, legacy_runtime_contract
from fomo.sandbox.base import SandboxRef
from fomo.schemas import UserInputRequestDraft

from .execution import DirectPiRunCancelled, PiEventWriter, redact

_STRUCTURED_OUTPUT_TOOL_NAME = "submit_structured_output"
_USER_INPUT_TOOL_NAME = "request_user_input"


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


class DirectPiCancelled(DirectPiSessionError, DirectPiRunCancelled):
    pass


class DirectPiAwaitingUser(DirectPiSessionError):
    """The current Pi turn ended cleanly at a durable user-input boundary."""


class DirectPiContinuationUnavailable(DirectPiSessionError):
    """The exact persisted Pi session cannot accept the continuation turn."""


class DirectPiSession:
    def __init__(
        self,
        repository: Repository,
        transport: PiTransport,
        settings: Settings,
        virtual_key: RunVirtualKey,
        *,
        runtime_contract: RuntimeContract | None = None,
        run_id: str,
        lease_token: str,
        started_at: float,
        session_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.transport = transport
        self.settings = settings
        self.virtual_key = virtual_key
        self.runtime_contract = runtime_contract or legacy_runtime_contract()
        if self.runtime_contract.litellm_alias not in virtual_key.model_aliases:
            raise ValueError("virtual key does not authorize the run runtime profile")
        self.run_id = run_id
        self.lease_token = lease_token
        self.started_at = started_at
        self.session_id = session_id or f"fomo-{run_id}"
        self._writer = PiEventWriter(
            repository, run_id=run_id, lease_token=lease_token
        )

    async def invoke(
        self,
        ref: SandboxRef,
        prompt: str,
        *,
        stage: str,
        goal_id: str | None = None,
        structured_output_schema: dict[str, object] | None = None,
        continuation_key: str | None = None,
        continuation_context: dict[str, object] | None = None,
        resume_request_id: str | None = None,
        require_existing_session: bool = False,
    ) -> str:
        if structured_output_schema is not None and stage != "planning":
            raise DirectPiSessionError(
                "structured output is only available during planning"
        )
        await self._check_active()
        await self._check_durable_budget(for_new_turn=True)
        request = PiRequest(
            request_id=uuid7(),
            correlation_id=self.run_id,
            session_id=self.session_id,
            provider_base_url=self.settings.pi_provider_base_url,
            prompt=prompt,
            virtual_key=self.virtual_key.secret,
            # One immutable run contract spans planning, implementation,
            # bounded repair, and clarification resume.
            thinking=self.runtime_contract.thinking,
            model=self.runtime_contract.model_ref,
            context_window=self.runtime_contract.context_window,
            # This value controls liveness heartbeat cadence only. The bridge
            # never turns protocol silence into a run failure.
            activity_silence_seconds=self.settings.model_request_timeout_seconds,
            # The sandbox resource lifetime, lease, cancellation, provider
            # connection, and spend boundary own termination—not a FOMO wall.
            timeout_seconds=None,
            structured_output_schema=structured_output_schema,
            user_input_enabled=True,
            require_resume=resume_request_id is not None or require_existing_session,
        )
        usage_token = await self._reserve_usage(
            request=request,
            stage=stage,
            goal_id=goal_id,
        )
        # Reservation is durable but zero-valued. Re-check immediately before
        # invoking the paid transport so a concurrent cancel cannot start a
        # new turn merely because the request id was reserved.
        await self._check_active()
        cancel_event = asyncio.Event()
        diagnostic: list[str] = []
        watcher_failures: list[BaseException] = []

        async def on_diagnostic(value: str) -> None:
            if sum(len(item) for item in diagnostic) < 16_000:
                diagnostic.append(redact(value)[:4096])

        watcher = asyncio.create_task(
            self._watch_cancel(cancel_event, failures=watcher_failures)
        )

        async def write_event(envelope: PiBridgeEnvelope) -> None:
            await self._writer(envelope, stage=stage)

        try:
            result = await self.transport.run(
                ref,
                PiInvocation(request),
                on_event=write_event,
                on_diagnostic=on_diagnostic,
                cancel_event=cancel_event,
            )
        except PiBridgeFailed as exc:
            if exc.payload.get("code") == "session_resume_unavailable":
                raise DirectPiContinuationUnavailable(
                    "the persisted Pi session is unavailable"
                ) from None
            raise
        except PiTransportCancelled:
            if await self.repository.is_cancel_requested(self.run_id):
                raise DirectPiCancelled("Direct Pi run was cancelled") from None
            raise RunLeaseLost("run lease was lost while Direct Pi was active") from None
        except RunLeaseLost:
            raise
        except Exception as exc:
            if resume_request_id is not None:
                raise DirectPiContinuationUnavailable(
                    "the exact Pi continuation transport is unavailable"
                ) from exc
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        # A completed provider turn has already incurred usage. Settle it by
        # request id before observing cancellation or lease loss; the opaque
        # reservation token makes this safe and idempotent after fencing.
        await self._record_usage(
            request=request,
            result=result,
            stage=stage,
            goal_id=goal_id,
            usage_token=usage_token,
        )
        await self._check_active()
        if watcher_failures:
            raise RunLeaseLost(
                "run active guard failed while Direct Pi was active"
            ) from watcher_failures[0]
        if diagnostic:
            await self.repository.append_event(
                self.run_id,
                "pi.diagnostic",
                payload={"stage": stage, "message": "".join(diagnostic)[:16_000]},
                lease_token=self.lease_token,
            )
        input_request = self._input_request(result)
        if input_request is not None:
            key = continuation_key or f"{stage}.turn"
            await self.repository.wait_for_user_input(
                self.run_id,
                input_request,
                continuation_key=key,
                continuation_context=continuation_context,
                stage=stage,
                goal_id=goal_id,
                pi_session_id=self.session_id,
                sandbox_id=ref.id,
                lease_token=self.lease_token,
            )
            raise DirectPiAwaitingUser("Direct Pi is waiting for user input")
        stop_reason = self._last_assistant_stop_reason(result)
        if stop_reason == "length":
            self._check_budget(result)
            raise DirectPiSessionError("Direct Pi reached its output limit")
        if not await self._check_durable_budget(for_new_turn=False):
            self._check_budget(result)
        text = (
            self._structured_output_text(result)
            if structured_output_schema is not None
            else self._last_assistant_text(result)
        )
        if not text:
            raise DirectPiSessionError("Direct Pi completed without a public assistant result")
        if resume_request_id is not None:
            await self.repository.complete_run_continuation(
                self.run_id,
                resume_request_id,
                lease_token=self.lease_token,
            )
        return text

    @staticmethod
    def _input_request(result: PiTransportResult) -> UserInputRequestDraft | None:
        value = result.bridge.completed.get("inputRequest")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise DirectPiSessionError("Direct Pi input request is invalid")
        if set(value) - {
            "requestId",
            "question",
            "choices",
            "allowFreeform",
            "reason",
        }:
            raise DirectPiSessionError("Direct Pi input request has unknown fields")
        request_id = value.get("requestId")
        if not isinstance(request_id, str) or not request_id.startswith("input-"):
            raise DirectPiSessionError("Direct Pi input request id is invalid")
        try:
            return UserInputRequestDraft.model_validate(
                {key: item for key, item in value.items() if key != "requestId"}
            )
        except Exception as exc:
            raise DirectPiSessionError("Direct Pi input request is invalid") from exc

    async def _record_usage(
        self,
        *,
        request: PiRequest,
        result: PiTransportResult,
        stage: str,
        goal_id: str | None,
        usage_token: str | None,
    ) -> None:
        usage = self._usage_delta(result)
        settler = getattr(self.repository, "settle_usage_entry", None)
        if usage_token is not None:
            if not callable(settler):
                raise DirectPiSessionError(
                    "Direct Pi usage reservation cannot be settled"
                )
            await settler(
                self.run_id,
                request.request_id,
                usage_token=usage_token,
                **usage,
            )
            return

        # Compatibility for repositories/fakes predating two-phase usage.
        recorder = getattr(self.repository, "record_usage_entry", None)
        if not callable(recorder):
            return
        provider, separator, model = request.model.partition("/")
        if not separator:
            provider, model = "unknown", request.model
        await recorder(
            self.run_id,
            request.request_id,
            provider=provider,
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_tokens"],
            cache_write_tokens=usage["cache_write_tokens"],
            tool_calls=usage["tool_calls"],
            cost_micros=usage["cost_micros"],
            metadata={
                "stage": stage,
                "sessionId": self.session_id,
                "executionId": result.execution_id,
            },
            goal_id=goal_id,
            lease_token=self.lease_token,
        )

    async def _reserve_usage(
        self,
        *,
        request: PiRequest,
        stage: str,
        goal_id: str | None,
    ) -> str | None:
        reserver = getattr(self.repository, "reserve_usage_entry", None)
        if not callable(reserver):
            return None
        provider, separator, model = request.model.partition("/")
        if not separator:
            provider, model = "unknown", request.model
        token = await reserver(
            self.run_id,
            request.request_id,
            provider=provider,
            model=model,
            metadata={
                "stage": stage,
                "sessionId": self.session_id,
            },
            goal_id=goal_id,
            lease_token=self.lease_token,
        )
        if not isinstance(token, str) or not token:
            raise DirectPiSessionError("Direct Pi usage reservation token is invalid")
        return token

    async def _check_durable_budget(self, *, for_new_turn: bool) -> bool:
        """Check ledger-backed run totals when the repository supports them."""

        getter = getattr(self.repository, "get_usage_totals", None)
        if not callable(getter):
            return False
        totals = await getter(self.run_id)

        def value(*names: str) -> float:
            for name in names:
                if isinstance(totals, dict) and name in totals:
                    candidate = totals[name]
                else:
                    candidate = getattr(totals, name, None)
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                    return float(candidate)
            return 0.0

        cost_micros = value("cost_micros", "costMicros")
        spend_exhausted = (
            cost_micros >= self.runtime_contract.max_spend_micros
            if for_new_turn
            else cost_micros > self.runtime_contract.max_spend_micros
        )
        if spend_exhausted:
            raise DirectPiSessionError("Direct Pi exceeded the run spend budget")
        return True

    @classmethod
    def _usage_delta(cls, result: PiTransportResult) -> dict[str, int]:
        final = result.bridge.completed.get("stats")
        if not isinstance(final, dict):
            raise DirectPiSessionError("Direct Pi did not report final usage")
        initial = result.bridge.started.get("initialStats")
        if not isinstance(initial, dict):
            # Compatibility for older in-process/fake transports. Production
            # bridge protocol always supplies started.initialStats.
            initial = {
                "tokens": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "toolCalls": 0,
                "cost": 0,
            }

        def counter(stats: dict[str, Any], name: str) -> int:
            value = stats.get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise DirectPiSessionError("Direct Pi usage counter is invalid")
            if not math.isfinite(value) or value < 0 or not float(value).is_integer():
                raise DirectPiSessionError("Direct Pi usage counter is invalid")
            return int(value)

        def token(stats: dict[str, Any], name: str) -> int:
            tokens = stats.get("tokens")
            if not isinstance(tokens, dict):
                raise DirectPiSessionError("Direct Pi token usage is invalid")
            return counter(tokens, name)

        def cost(stats: dict[str, Any]) -> float:
            value = stats.get("cost")
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise DirectPiSessionError("Direct Pi cost usage is invalid")
            return float(value)

        deltas = {
            "input_tokens": token(final, "input") - token(initial, "input"),
            "output_tokens": token(final, "output") - token(initial, "output"),
            "cache_read_tokens": token(final, "cacheRead") - token(initial, "cacheRead"),
            "cache_write_tokens": token(final, "cacheWrite") - token(initial, "cacheWrite"),
            "tool_calls": counter(final, "toolCalls") - counter(initial, "toolCalls"),
            "cost_micros": round((cost(final) - cost(initial)) * 1_000_000),
        }
        if any(value < 0 for value in deltas.values()):
            raise DirectPiSessionError("Direct Pi cumulative usage moved backwards")
        return deltas

    async def _watch_cancel(
        self,
        cancel_event: asyncio.Event,
        *,
        failures: list[BaseException],
    ) -> None:
        try:
            while not cancel_event.is_set():
                active, cancelled = await asyncio.gather(
                    self.repository.is_active_lease(self.run_id, self.lease_token),
                    self.repository.is_cancel_requested(self.run_id),
                )
                if not active or cancelled:
                    cancel_event.set()
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failures.append(exc)
            if not cancel_event.is_set():
                cancel_event.set()

    async def _check_active(self) -> None:
        cancelled, active = await asyncio.gather(
            self.repository.is_cancel_requested(self.run_id),
            self.repository.is_active_lease(self.run_id, self.lease_token),
        )
        if cancelled:
            raise DirectPiCancelled("Direct Pi run was cancelled")
        if not active:
            raise RunLeaseLost("run lease is no longer active")

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
        # Keep token accounting structurally valid for telemetry and context
        # management, but runtime v2 intentionally has no cumulative per-run
        # token ceiling. Provider/context/output limits remain enforced at the
        # invocation boundary.
        self._budgeted_token_total(tokens)
        if cost * 1_000_000 > self.runtime_contract.max_spend_micros:
            raise DirectPiSessionError("Direct Pi exceeded the run spend budget")

    @staticmethod
    def _budgeted_token_total(tokens: object) -> float:
        """Validate and derive newly processed tokens for usage telemetry.

        Pi's cumulative ``total`` includes ``cacheRead`` again for every tool
        turn. Cache reads are tracked separately and excluded from this derived
        value. The value is not compared with a cumulative run ceiling.
        """
        if not isinstance(tokens, dict):
            raise DirectPiSessionError("Direct Pi token usage is invalid")
        values: list[float] = []
        for key in ("input", "output", "cacheWrite", "cacheRead"):
            value = tokens.get(key)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise DirectPiSessionError("Direct Pi token usage is invalid")
            if key != "cacheRead":
                values.append(float(value))
        return sum(values)

    @staticmethod
    def _last_assistant_text(result: PiTransportResult) -> str:
        # ``turn_end.text`` is intentionally capped for durable UI events.
        # Machine contracts must instead be reconstructed from the complete
        # ordered text-delta stream, otherwise valid plans above the display
        # limit are parsed as the literal ``…[truncated]`` marker.
        texts: list[str] = []
        current: list[str] = []
        for event in result.bridge.events:
            if event.type != "pi.event":
                continue
            kind = event.payload.get("kind")
            if kind == "turn_start":
                current = []
            elif (
                kind == "message_delta"
                and event.payload.get("deltaType") == "text_delta"
            ):
                delta = event.payload.get("delta")
                if isinstance(delta, str):
                    current.append(delta)
            elif kind == "turn_end" and event.payload.get("role") == "assistant":
                reconstructed = "".join(current).strip()
                public_fallback = str(event.payload.get("text", "")).strip()
                texts.append(reconstructed or public_fallback)
        return next((value for value in reversed(texts) if value), "")

    @staticmethod
    def _structured_output_text(result: PiTransportResult) -> str:
        """Extract the sole successful virtual-tool call as canonical JSON.

        Structured planning never falls back to assistant prose. The bridge
        permits up to three schema-correction attempts and disables Pi's native
        tools. The control plane independently checks strict ordered lifecycle
        matching so a protocol/configuration regression fails closed before
        Pydantic sees the successful payload.
        """

        active: dict[str, dict[str, Any]] = {}
        seen_ids: set[str] = set()
        successful_arguments: dict[str, Any] | None = None
        for event in result.bridge.events:
            if event.type != "pi.event":
                continue
            kind = event.payload.get("kind")
            if kind == "tool_start":
                if event.payload.get("toolName") != _STRUCTURED_OUTPUT_TOOL_NAME:
                    raise DirectPiSessionError(
                        "Direct Pi used a native tool during structured planning"
                    )
                if successful_arguments is not None:
                    raise DirectPiSessionError(
                        "Direct Pi must stop after structured output succeeds"
                    )
                tool_call_id = event.payload.get("toolCallId")
                if (
                    not isinstance(tool_call_id, str)
                    or not tool_call_id
                    or tool_call_id in seen_ids
                ):
                    raise DirectPiSessionError(
                        "Direct Pi emitted an invalid or duplicate structured tool start"
                    )
                if active:
                    raise DirectPiSessionError(
                        "Direct Pi emitted concurrent structured tool attempts"
                    )
                arguments = event.payload.get("args")
                if not isinstance(arguments, dict):
                    raise DirectPiSessionError(
                        "Direct Pi structured output arguments must be an object"
                    )
                seen_ids.add(tool_call_id)
                active[tool_call_id] = arguments
            elif kind in {"tool_output", "tool_end"}:
                if event.payload.get("toolName") != _STRUCTURED_OUTPUT_TOOL_NAME:
                    raise DirectPiSessionError(
                        "Direct Pi used a native tool during structured planning"
                    )
                tool_call_id = event.payload.get("toolCallId")
                if not isinstance(tool_call_id, str) or tool_call_id not in active:
                    raise DirectPiSessionError(
                        "Direct Pi emitted unmatched structured tool progress"
                    )
                if kind == "tool_end":
                    is_error = event.payload.get("isError")
                    if not isinstance(is_error, bool):
                        raise DirectPiSessionError(
                            "Direct Pi structured tool result is missing error status"
                        )
                    arguments = active.pop(tool_call_id)
                    if not is_error:
                        if successful_arguments is not None:
                            raise DirectPiSessionError(
                                "Direct Pi completed structured output successfully more than once"
                            )
                        successful_arguments = arguments

        if active:
            raise DirectPiSessionError(
                "Direct Pi structured output tool did not complete"
            )
        if successful_arguments is None:
            raise DirectPiSessionError(
                "Direct Pi must complete submit_structured_output successfully exactly once"
            )
        try:
            return json.dumps(
                successful_arguments,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise DirectPiSessionError(
                "Direct Pi structured output arguments are not valid JSON"
            ) from exc

    @staticmethod
    def _last_assistant_stop_reason(result: PiTransportResult) -> str | None:
        for event in reversed(result.bridge.events):
            if event.type != "pi.event":
                continue
            if (
                event.payload.get("kind") in {"turn_end", "message_end"}
                and event.payload.get("role") == "assistant"
            ):
                value = event.payload.get("stopReason")
                return value if isinstance(value, str) and value else None
        return None
