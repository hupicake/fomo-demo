"""Server-owned admission and settlement for Coding Runtime turns.

Transport completion only proves that a framework stopped producing events.
This module decides whether the turn produced an artifact that may enter the
workspace audit and verification pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .failures import (
    AgentCapabilityUnavailable,
    AgentNoEffect,
    FailureCategory,
    FailureOutcome,
    FailureStage,
    SafeRunDiagnostic,
)


class TurnEffectPolicy(StrEnum):
    MUST_CHANGE = "must_change"
    MAY_NOOP = "may_noop"
    VERIFY_ONLY = "verify_only"


_CAPABILITY_KEYS = frozenset(
    {
        "structuredOutput",
        "repoRead",
        "repoMutate",
        "commandExec",
        "sessionResume",
        "sessionCancel",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityAttestation:
    structured_output: bool
    repo_read: bool
    repo_mutate: bool
    command_exec: bool
    session_resume: bool
    session_cancel: bool

    @classmethod
    def from_started_payload(
        cls,
        payload: dict[str, Any],
    ) -> RuntimeCapabilityAttestation | None:
        value = payload.get("capabilities")
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != _CAPABILITY_KEYS:
            raise ValueError("runtime capability attestation is malformed")
        if any(type(value[key]) is not bool for key in _CAPABILITY_KEYS):
            raise ValueError("runtime capability attestation must contain booleans")
        return cls(
            structured_output=value["structuredOutput"],
            repo_read=value["repoRead"],
            repo_mutate=value["repoMutate"],
            command_exec=value["commandExec"],
            session_resume=value["sessionResume"],
            session_cancel=value["sessionCancel"],
        )

    def assert_stage_ready(
        self,
        *,
        framework: str,
        stage: str,
        structured_output: bool,
    ) -> None:
        if structured_output:
            missing = () if self.structured_output else ("structured_output",)
        elif stage in {"building", "repairing"}:
            missing = tuple(
                name
                for name, available in (
                    ("repo.read", self.repo_read),
                    ("repo.mutate", self.repo_mutate),
                    ("command.exec", self.command_exec),
                )
                if not available
            )
        else:
            missing = ()
        if not missing:
            return
        stage_value = _failure_stage(stage)
        raise AgentCapabilityUnavailable(
            SafeRunDiagnostic(
                stage=stage_value,
                component=f"{framework}_adapter",
                check="runtime_capability_binding",
                category=FailureCategory.RUNTIME_FAILED,
                reason_code="repository_tools_unavailable",
                outcome=FailureOutcome.UNAVAILABLE,
                retryable=True,
                frames=(
                    f"{framework} {stage_value.value} session is missing required capabilities.",
                    f"Missing capability: {', '.join(missing)}.",
                    "The model turn was not admitted to workspace verification.",
                ),
            )
        )


@dataclass(frozen=True, slots=True)
class RuntimeTurnReceipt:
    request_id: str
    framework: str
    stage: str
    session_id: str
    tool_calls: int
    attestation: RuntimeCapabilityAttestation | None


@dataclass(frozen=True, slots=True)
class TurnSettlement:
    effect_policy: TurnEffectPolicy
    changed_paths: tuple[str, ...]
    tool_calls: int
    no_op: bool


def settle_workspace_turn(
    receipt: RuntimeTurnReceipt,
    *,
    changed_paths: tuple[str, ...],
    effect_policy: TurnEffectPolicy,
) -> TurnSettlement:
    """Admit a workspace turn using only server-observed candidate changes."""

    unique_paths = tuple(sorted(set(changed_paths)))
    if effect_policy is TurnEffectPolicy.MUST_CHANGE and not unique_paths:
        raise AgentNoEffect(
            SafeRunDiagnostic(
                stage=_failure_stage(receipt.stage),
                component="settlement_engine",
                check="candidate_manifest_delta",
                category=FailureCategory.RUNTIME_FAILED,
                reason_code="agent_no_effect",
                outcome=FailureOutcome.REJECTED,
                retryable=True,
                frames=(
                    "The Coding Runtime transport finished without a candidate change.",
                    f"Server-observed changed files: 0; tool calls: {receipt.tool_calls}.",
                    "Verification admission was rejected before typecheck or browser QA.",
                ),
            )
        )
    return TurnSettlement(
        effect_policy=effect_policy,
        changed_paths=unique_paths,
        tool_calls=receipt.tool_calls,
        no_op=not unique_paths,
    )


def _failure_stage(stage: str) -> FailureStage:
    try:
        return FailureStage(stage)
    except ValueError:
        return FailureStage.BUILDING
