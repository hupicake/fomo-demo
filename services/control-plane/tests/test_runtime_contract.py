from __future__ import annotations

import pytest

from fomo.runtime_contract import (
    CODEX_COMPATIBLE_PROFILE_IDS,
    PREVIOUS_RUNTIME_POLICY_VERSION,
    RuntimeContractError,
    compatible_profile_ids_for_agent_framework,
    resolve_runtime_contract,
    runtime_contract_from_storage,
    validate_agent_framework_runtime,
)


def _restore_gpt(*, policy_version: str, run_max_tokens: int | None):
    return runtime_contract_from_storage(
        profile_id="gpt-5.6",
        model_ref="fomo-litellm/fomo-pi-gpt-5.6",
        thinking="high",
        context_window=250_000,
        policy_version=policy_version,
        run_max_tokens=run_max_tokens,
        inference_tpm_limit=1_000_000,
        max_spend_micros=5_000_000,
    )


def test_new_runtime_freezes_explicit_unlimited_cumulative_tokens() -> None:
    runtime = resolve_runtime_contract("gpt-5.6", "high")

    assert runtime.policy_version == "direct-pi-runtime-v2"
    assert runtime.run_max_tokens is None
    assert _restore_gpt(
        policy_version=runtime.policy_version,
        run_max_tokens=None,
    ) == runtime


def test_operator_spend_cap_is_not_silently_reduced_by_profile() -> None:
    runtime = resolve_runtime_contract(
        "gpt-5.6",
        "high",
        max_spend_micros=100_000_000,
    )

    assert runtime.max_spend_micros == 100_000_000


def test_historical_runtime_v1_integer_budget_remains_readable() -> None:
    runtime = _restore_gpt(
        policy_version=PREVIOUS_RUNTIME_POLICY_VERSION,
        run_max_tokens=600_000,
    )

    assert runtime.policy_version == PREVIOUS_RUNTIME_POLICY_VERSION
    assert runtime.run_max_tokens == 600_000

    with pytest.raises(RuntimeContractError, match="does not match"):
        _restore_gpt(
            policy_version=PREVIOUS_RUNTIME_POLICY_VERSION,
            run_max_tokens=None,
        )
    with pytest.raises(RuntimeContractError, match="does not match"):
        _restore_gpt(
            policy_version="direct-pi-runtime-v2",
            run_max_tokens=600_000,
        )


def test_codex_runtime_compatibility_is_a_closed_gpt_only_contract() -> None:
    assert compatible_profile_ids_for_agent_framework("codex") == (
        CODEX_COMPATIBLE_PROFILE_IDS
    )
    validate_agent_framework_runtime("codex", "gpt-5.6", "xhigh")

    with pytest.raises(RuntimeContractError, match="requires a GPT"):
        validate_agent_framework_runtime("codex", "deepseek-flash", "high")
    with pytest.raises(RuntimeContractError, match="thinking level"):
        validate_agent_framework_runtime("codex", "gpt-5.6", "off")
    with pytest.raises(RuntimeContractError, match="pi, opencode, or codex"):
        compatible_profile_ids_for_agent_framework("unknown")
