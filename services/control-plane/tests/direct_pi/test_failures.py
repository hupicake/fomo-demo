from __future__ import annotations

import json

import pytest

from fomo.direct_pi.failures import (
    AGENT_CAPABILITY_UNAVAILABLE,
    AGENT_NO_EFFECT,
    CODING_AGENT_FAILED,
    CODING_AGENT_RUNTIME_FAILED,
    INFERENCE_GATEWAY_UNAVAILABLE,
    MODEL_RESPONSE_FAILED,
    MODEL_RUNTIME_PROTOCOL_FAILED,
    PLANNING_CONTRACT_FAILED,
    RUN_OUTPUT_BUDGET_EXCEEDED,
    RUN_SPEND_BUDGET_EXCEEDED,
    RUN_TOKEN_BUDGET_EXCEEDED,
    RUN_TOOL_BUDGET_EXCEEDED,
    RUN_WALL_TIME_BUDGET_EXCEEDED,
    WORKSPACE_CONTRACT_FAILED,
    AgentCapabilityUnavailable,
    AgentNoEffect,
    DirectPiOrchestrationError,
    FailureCategory,
    FailureOutcome,
    FailureStage,
    PlanningContractError,
    SafeRunDiagnostic,
    classify_direct_pi_failure,
    public_bridge_failure,
    public_failure_for_code,
)
from fomo.direct_pi.session import DirectPiSessionError
from fomo.direct_pi.workspace import WorkspaceContractError
from fomo.fomo_pi_ds import (
    InferenceGatewayError,
    PiBridgeFailed,
    PiBridgeProtocolError,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    (
        ("Direct Pi exceeded the run token budget", RUN_TOKEN_BUDGET_EXCEEDED),
        ("Direct Pi exceeded the run tool-call budget", RUN_TOOL_BUDGET_EXCEEDED),
        ("Direct Pi run exceeded its wall-clock budget", RUN_WALL_TIME_BUDGET_EXCEEDED),
        ("Direct Pi exceeded the run spend budget", RUN_SPEND_BUDGET_EXCEEDED),
        ("Direct Pi reached its output limit", RUN_OUTPUT_BUDGET_EXCEEDED),
    ),
)
def test_classifies_only_exact_session_budget_sentinels(message, expected) -> None:
    assert classify_direct_pi_failure(DirectPiSessionError(message)) == expected


def test_classifies_gateway_type_without_forwarding_exception_text() -> None:
    secret = "sk-do-not-persist-12345678"
    failure = classify_direct_pi_failure(
        InferenceGatewayError(f"provider response contained {secret}")
    )

    assert failure == INFERENCE_GATEWAY_UNAVAILABLE
    assert secret not in json.dumps(failure.event_payload(), ensure_ascii=False)


def test_classifies_known_cause_through_an_unknown_wrapper() -> None:
    try:
        try:
            raise DirectPiSessionError("Direct Pi exceeded the run token budget")
        except DirectPiSessionError as cause:
            raise RuntimeError("secret wrapper detail") from cause
    except RuntimeError as error:
        assert classify_direct_pi_failure(error) == RUN_TOKEN_BUDGET_EXCEEDED


def test_unknown_or_near_match_failure_is_always_closed_generic() -> None:
    secret = "password=never-persist-this"

    assert classify_direct_pi_failure(RuntimeError(secret)) == CODING_AGENT_FAILED
    assert classify_direct_pi_failure(
        DirectPiSessionError("Direct Pi exceeded the run token budget " + secret)
    ) == CODING_AGENT_FAILED
    assert secret not in json.dumps(CODING_AGENT_FAILED.event_payload(), ensure_ascii=False)


def test_durable_spend_sentinel_and_bridge_codes_use_the_closed_contract() -> None:
    assert classify_direct_pi_failure(
        DirectPiOrchestrationError("Direct Pi exhausted its durable spend budget")
    ) == RUN_SPEND_BUDGET_EXCEEDED
    assert public_bridge_failure("run_output_budget_exceeded") == RUN_OUTPUT_BUDGET_EXCEEDED
    assert public_bridge_failure("timeout") == RUN_WALL_TIME_BUDGET_EXCEEDED
    assert classify_direct_pi_failure(
        PiBridgeFailed({"code": "timeout", "message": "secret provider body"})
    ) == RUN_WALL_TIME_BUDGET_EXCEEDED
    assert public_bridge_failure("provider exploded: sk-private-secret") == CODING_AGENT_FAILED


def test_opencode_failures_use_closed_model_runtime_and_generic_contracts() -> None:
    secret = "provider_body=password=never-persist"

    assert classify_direct_pi_failure(
        PiBridgeFailed({"code": "opencode_model_failed", "message": secret})
    ) == MODEL_RESPONSE_FAILED
    assert public_bridge_failure("opencode_runtime_failed") == CODING_AGENT_RUNTIME_FAILED
    assert public_bridge_failure("opencode_failed") == CODING_AGENT_FAILED
    assert secret not in json.dumps(
        [
            MODEL_RESPONSE_FAILED.event_payload(),
            CODING_AGENT_RUNTIME_FAILED.event_payload(),
            CODING_AGENT_FAILED.event_payload(),
        ],
        ensure_ascii=False,
    )


def test_codex_failures_use_closed_model_runtime_and_planning_contracts() -> None:
    secret = "provider_body=password=never-persist"

    assert classify_direct_pi_failure(
        PiBridgeFailed({"code": "codex_model_failed", "message": secret})
    ) == MODEL_RESPONSE_FAILED
    assert public_bridge_failure("codex_protocol_failed") == MODEL_RUNTIME_PROTOCOL_FAILED
    assert public_bridge_failure("codex_structured_output_invalid") == PLANNING_CONTRACT_FAILED
    assert public_bridge_failure("codex_runtime_failed") == CODING_AGENT_RUNTIME_FAILED
    assert secret not in json.dumps(
        [
            MODEL_RESPONSE_FAILED.event_payload(),
            MODEL_RUNTIME_PROTOCOL_FAILED.event_payload(),
            PLANNING_CONTRACT_FAILED.event_payload(),
            CODING_AGENT_RUNTIME_FAILED.event_payload(),
        ],
        ensure_ascii=False,
    )


def test_model_and_planning_contract_failures_are_specific_without_forwarding_text() -> None:
    secret = "api_key=private-model-body"

    assert classify_direct_pi_failure(
        DirectPiSessionError("Direct Pi completed without a public assistant result")
    ) == MODEL_RESPONSE_FAILED
    assert classify_direct_pi_failure(
        PiBridgeProtocolError(f"malformed provider stream: {secret}")
    ) == MODEL_RUNTIME_PROTOCOL_FAILED
    assert classify_direct_pi_failure(
        PlanningContractError(f"invalid plan: {secret}")
    ) == PLANNING_CONTRACT_FAILED
    assert public_bridge_failure("missing_structured_output") == PLANNING_CONTRACT_FAILED
    assert public_bridge_failure("unexpected_eof") == MODEL_RUNTIME_PROTOCOL_FAILED
    assert secret not in json.dumps(
        [
            MODEL_RESPONSE_FAILED.event_payload(),
            MODEL_RUNTIME_PROTOCOL_FAILED.event_payload(),
            PLANNING_CONTRACT_FAILED.event_payload(),
        ],
        ensure_ascii=False,
    )


def test_workspace_contract_and_terminal_code_lookup_are_closed() -> None:
    secret = "candidate contains /private/path password=never-show"

    assert classify_direct_pi_failure(WorkspaceContractError(secret)) == WORKSPACE_CONTRACT_FAILED
    assert public_failure_for_code("workspace_contract_failed") == WORKSPACE_CONTRACT_FAILED
    assert public_failure_for_code(secret) == CODING_AGENT_FAILED
    assert secret not in json.dumps(WORKSPACE_CONTRACT_FAILED.event_payload(), ensure_ascii=False)


def test_runtime_admission_and_settlement_failures_keep_safe_causal_diagnostics() -> None:
    capability = SafeRunDiagnostic(
        stage=FailureStage.BUILDING,
        component="opencode_adapter",
        check="runtime_capability_binding",
        category=FailureCategory.RUNTIME_FAILED,
        reason_code="repository_tools_unavailable",
        outcome=FailureOutcome.UNAVAILABLE,
        retryable=True,
        frames=("OpenCode repository tools are unavailable.",),
    )
    no_effect = SafeRunDiagnostic(
        stage=FailureStage.BUILDING,
        component="settlement_engine",
        check="candidate_manifest_delta",
        category=FailureCategory.RUNTIME_FAILED,
        reason_code="agent_no_effect",
        outcome=FailureOutcome.REJECTED,
        retryable=True,
        frames=("Server-observed changed files: 0.",),
    )

    assert (
        classify_direct_pi_failure(AgentCapabilityUnavailable(capability))
        == AGENT_CAPABILITY_UNAVAILABLE
    )
    assert classify_direct_pi_failure(AgentNoEffect(no_effect)) == AGENT_NO_EFFECT
    assert public_bridge_failure("opencode_capability_unavailable") == AGENT_CAPABILITY_UNAVAILABLE
    assert capability.event_payload()["reasonCode"] == "repository_tools_unavailable"
