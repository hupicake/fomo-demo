from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fomo.direct_pi.session import DirectPiSession
from fomo.fomo_pi_ds import PiBridgeResult, PiTransportResult, RunVirtualKey
from fomo.runtime_contract import PREVIOUS_RUNTIME_POLICY_VERSION, resolve_runtime_contract


def _session(settings, runtime, repository=None) -> DirectPiSession:
    return DirectPiSession(
        repository or SimpleNamespace(),
        SimpleNamespace(),
        settings,
        RunVirtualKey(
            run_id="run-unlimited",
            key_alias="fomo-run-unlimited",
            duration_seconds=300,
            secret="sk-test-run-key",
            model_aliases=(runtime.litellm_alias,),
        ),
        runtime_contract=runtime,
        run_id="run-unlimited",
        lease_token="lease-unlimited",
        started_at=time.monotonic(),
    )


def _large_usage_result() -> PiTransportResult:
    return PiTransportResult(
        bridge=PiBridgeResult(
            started={},
            events=(),
            completed={
                "stats": {
                    "tokens": {
                        "input": 8_000_000,
                        "output": 2_000_000,
                        "cacheRead": 20_000_000,
                        "cacheWrite": 1_000_000,
                        "total": 31_000_000,
                    },
                    "toolCalls": 50_000,
                    "cost": 0,
                }
            },
        ),
        execution_id="large-token-turn",
        exit_code=0,
        stderr="",
    )


@pytest.mark.asyncio
async def test_completed_turn_never_fails_on_cumulative_token_usage(settings) -> None:
    current = resolve_runtime_contract("gpt-5.6", "high")
    historical = replace(
        current,
        policy_version=PREVIOUS_RUNTIME_POLICY_VERSION,
        run_max_tokens=600_000,
    )

    for runtime in (current, historical):
        _session(settings, runtime)._check_budget(_large_usage_result())


@pytest.mark.asyncio
async def test_durable_ledger_never_blocks_a_turn_on_cumulative_tokens(settings) -> None:
    runtime = resolve_runtime_contract("gpt-5.6", "high")
    repository = SimpleNamespace(
        get_usage_totals=AsyncMock(
            return_value={
                "input_tokens": 8_000_000,
                "output_tokens": 2_000_000,
                "cache_write_tokens": 1_000_000,
                "tool_calls": 50_000,
                "cost_micros": 0,
            }
        )
    )

    assert await _session(settings, runtime, repository)._check_durable_budget(
        for_new_turn=True
    )
