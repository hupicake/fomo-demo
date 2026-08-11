from __future__ import annotations

import pytest

from fomo.direct_pi.failures import AgentCapabilityUnavailable, AgentNoEffect
from fomo.direct_pi.settlement import (
    RuntimeCapabilityAttestation,
    RuntimeTurnReceipt,
    TurnEffectPolicy,
    settle_workspace_turn,
)


def _receipt(*, tool_calls: int = 1) -> RuntimeTurnReceipt:
    return RuntimeTurnReceipt(
        request_id="request-1",
        framework="opencode",
        stage="building",
        session_id="session-1",
        tool_calls=tool_calls,
        attestation=RuntimeCapabilityAttestation(
            structured_output=False,
            repo_read=True,
            repo_mutate=True,
            command_exec=True,
            session_resume=True,
            session_cancel=True,
        ),
    )


def test_workspace_turn_with_server_observed_delta_is_admitted() -> None:
    result = settle_workspace_turn(
        _receipt(tool_calls=2),
        changed_paths=("app/page.tsx", "app/page.tsx", "lib/store.ts"),
        effect_policy=TurnEffectPolicy.MUST_CHANGE,
    )

    assert result.changed_paths == ("app/page.tsx", "lib/store.ts")
    assert result.tool_calls == 2
    assert result.no_op is False


def test_must_change_turn_without_delta_never_enters_verification() -> None:
    with pytest.raises(AgentNoEffect) as raised:
        settle_workspace_turn(
            _receipt(tool_calls=0),
            changed_paths=(),
            effect_policy=TurnEffectPolicy.MUST_CHANGE,
        )

    diagnostic = raised.value.diagnostic.event_payload()
    assert diagnostic["reasonCode"] == "agent_no_effect"
    assert diagnostic["check"] == "candidate_manifest_delta"
    assert diagnostic["category"] == "runtime_failed"


def test_noop_is_only_accepted_by_an_explicit_effect_policy() -> None:
    result = settle_workspace_turn(
        _receipt(tool_calls=0),
        changed_paths=(),
        effect_policy=TurnEffectPolicy.MAY_NOOP,
    )

    assert result.no_op is True


def test_build_attestation_requires_semantic_repository_capabilities() -> None:
    attestation = RuntimeCapabilityAttestation(
        structured_output=True,
        repo_read=True,
        repo_mutate=False,
        command_exec=True,
        session_resume=True,
        session_cancel=True,
    )

    with pytest.raises(AgentCapabilityUnavailable) as raised:
        attestation.assert_stage_ready(
            framework="opencode",
            stage="building",
            structured_output=False,
        )

    assert raised.value.diagnostic.reason_code == "repository_tools_unavailable"
    assert "repo.mutate" in raised.value.diagnostic.frames[1]


def test_attestation_schema_is_closed() -> None:
    with pytest.raises(ValueError):
        RuntimeCapabilityAttestation.from_started_payload(
            {
                "capabilities": {
                    "structuredOutput": True,
                    "repoRead": True,
                    "repoMutate": True,
                    "commandExec": True,
                    "sessionResume": True,
                    "sessionCancel": True,
                    "unexpected": True,
                }
            }
        )
