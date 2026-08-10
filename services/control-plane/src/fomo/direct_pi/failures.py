"""Closed-set, browser-safe failure contracts for Direct Pi runs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PublicRunFailure:
    """A stable failure result that is safe to persist and send to browsers."""

    code: str
    message: str

    @property
    def summary(self) -> str:
        return self.message

    def event_payload(self, *, goal_id: str | None = None) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if goal_id is not None:
            payload["goalId"] = goal_id
        return payload


class DirectPiOrchestrationError(RuntimeError):
    """The deterministic orchestration contract could not be completed."""


class PlanningContractError(DirectPiOrchestrationError):
    """The model did not return the server-owned planning contract."""


INFERENCE_GATEWAY_UNAVAILABLE = PublicRunFailure(
    code="inference_gateway_unavailable",
    message="模型服务暂时不可用，请稍后重试。",
)
RUN_TOKEN_BUDGET_EXCEEDED = PublicRunFailure(
    code="run_token_budget_exceeded",
    message="本次任务已达到 Token 使用上限，请缩小任务范围后重试。",
)
RUN_TOOL_BUDGET_EXCEEDED = PublicRunFailure(
    code="run_tool_budget_exceeded",
    message="本次任务已达到工具调用上限，请缩小任务范围后重试。",
)
RUN_WALL_TIME_BUDGET_EXCEEDED = PublicRunFailure(
    code="run_wall_time_budget_exceeded",
    message="本次任务已达到最长运行时间，请缩小任务范围后重试。",
)
RUN_SPEND_BUDGET_EXCEEDED = PublicRunFailure(
    code="run_spend_budget_exceeded",
    message="本次任务已达到费用上限，请缩小任务范围后重试。",
)
RUN_OUTPUT_BUDGET_EXCEEDED = PublicRunFailure(
    code="run_output_budget_exceeded",
    message="模型单次输出已达到上限，请重试或缩小任务范围。",
)
MODEL_RUNTIME_PROTOCOL_FAILED = PublicRunFailure(
    code="model_runtime_protocol_failed",
    message="模型运行协议未能完整结束，未获得可用结果。请重试或切换模型。",
)
MODEL_RESPONSE_FAILED = PublicRunFailure(
    code="model_response_failed",
    message="模型未返回可用的公开结果。请重试、切换模型或缩小任务范围。",
)
CODING_AGENT_RUNTIME_FAILED = PublicRunFailure(
    code="coding_agent_runtime_failed",
    message=(
        "Coding Agent 运行环境暂时不可用，请重试；若问题持续发生，"
        "请检查当前选择的 Agent 框架状态。"
    ),
)
PLANNING_CONTRACT_FAILED = PublicRunFailure(
    code="planning_contract_failed",
    message="模型未能按要求返回有效的产品规划合约。请重试或切换模型。",
)
WORKSPACE_CONTRACT_FAILED = PublicRunFailure(
    code="workspace_contract_failed",
    message=(
        "生成结果未通过工作区安全契约检查，未被发布。请移除密钥或环境文件、"
        "受保护文件改动、符号链接或超出限制的文件后重试。"
    ),
)
DIRECT_PI_VERIFICATION_FAILED = PublicRunFailure(
    code="direct_pi_verification_failed",
    message=(
        "确定性验收仍有阻塞项，任务未发布。请查看失败的类型检查、构建或浏览器验收结果后重试。"
    ),
)
GOAL_VERIFICATION_FAILED = PublicRunFailure(
    code="goal_verification_failed",
    message=(
        "当前开发目标未通过确定性验收，且自动修复次数已用完。请查看失败门禁并缩小需求后重试。"
    ),
)
GOAL_TYPECHECK_FAILED = PublicRunFailure(
    code="goal_typecheck_failed",
    message=(
        "当前开发目标未通过 TypeScript 类型检查，且自动修复未成功。请查看类型检查结果后重试。"
    ),
)
GOAL_GRAPH_FINAL_REVERIFICATION_FAILED = PublicRunFailure(
    code="goal_graph_final_reverification_failed",
    message="最终候选在完整复验中失败，未被发布。请查看发布门禁结果后重试。",
)
DIRECT_PI_INFRASTRUCTURE_FAILED = PublicRunFailure(
    code="direct_pi_infrastructure_failed",
    message=(
        "验收基础设施暂时不可用，无法可信判断生成结果。请稍后重试；这不代表应用代码一定有问题。"
    ),
)
GOAL_VERIFICATION_INFRASTRUCTURE_FAILED = PublicRunFailure(
    code="goal_verification_infrastructure_failed",
    message=(
        "当前目标的验收基础设施未能完成检查。请稍后重试；这不代表当前代码一定有问题。"
    ),
)
WORKER_LEASE_EXPIRED = PublicRunFailure(
    code="worker_lease_expired",
    message=(
        "执行 Worker 在任务完成前失去租约，任务已中止。请重试；若持续发生，请检查 Worker 状态。"
    ),
)
CONTINUATION_ANSWER_MISSING = PublicRunFailure(
    code="continuation_answer_missing",
    message="澄清问题的回答未能持久保存，任务无法继续。请重新提交该需求。",
)
CONTINUATION_CURSOR_INVALID = PublicRunFailure(
    code="continuation_cursor_invalid",
    message="澄清回答对应的运行阶段已失效，任务无法安全继续。请重新提交该需求。",
)
PI_SESSION_RESUME_UNAVAILABLE = PublicRunFailure(
    code="pi_session_resume_unavailable",
    message="原 Coding Agent 会话或沙箱已不可用，无法安全续接。请重新提交该需求。",
)
P0_CONTINUATION_UNSUPPORTED = PublicRunFailure(
    code="p0_continuation_unsupported",
    message="当前兼容运行模式不支持安全续接澄清回答。请重新提交该需求。",
)
REPAIR_NO_PROGRESS = PublicRunFailure(
    code="repair_no_progress",
    message="自动修复后再次出现同一阻塞问题，任务已停止。请查看失败门禁并调整需求后重试。",
)
REPAIR_LIMIT_REACHED = PublicRunFailure(
    code="repair_limit_reached",
    message="自动修复次数已用完，仍有阻塞性验收失败。请查看失败门禁后重试。",
)
SOP_EXECUTION_ERROR = PublicRunFailure(
    code="sop_execution_error",
    message="执行 Worker 在验收完成前异常停止。请重试；若持续发生，请检查 Worker 状态。",
)
CODING_AGENT_FAILED = PublicRunFailure(
    code="coding_agent_failed",
    message="Coding Agent 运行失败，请重试；若问题持续发生，请检查服务状态。",
)

PUBLIC_FAILURES_BY_CODE = {
    failure.code: failure
    for failure in (
        INFERENCE_GATEWAY_UNAVAILABLE,
        RUN_TOKEN_BUDGET_EXCEEDED,
        RUN_TOOL_BUDGET_EXCEEDED,
        RUN_WALL_TIME_BUDGET_EXCEEDED,
        RUN_SPEND_BUDGET_EXCEEDED,
        RUN_OUTPUT_BUDGET_EXCEEDED,
        MODEL_RUNTIME_PROTOCOL_FAILED,
        MODEL_RESPONSE_FAILED,
        CODING_AGENT_RUNTIME_FAILED,
        PLANNING_CONTRACT_FAILED,
        WORKSPACE_CONTRACT_FAILED,
        DIRECT_PI_VERIFICATION_FAILED,
        GOAL_VERIFICATION_FAILED,
        GOAL_TYPECHECK_FAILED,
        GOAL_GRAPH_FINAL_REVERIFICATION_FAILED,
        DIRECT_PI_INFRASTRUCTURE_FAILED,
        GOAL_VERIFICATION_INFRASTRUCTURE_FAILED,
        WORKER_LEASE_EXPIRED,
        CONTINUATION_ANSWER_MISSING,
        CONTINUATION_CURSOR_INVALID,
        PI_SESSION_RESUME_UNAVAILABLE,
        P0_CONTINUATION_UNSUPPORTED,
        REPAIR_NO_PROGRESS,
        REPAIR_LIMIT_REACHED,
        SOP_EXECUTION_ERROR,
        CODING_AGENT_FAILED,
    )
}

_SESSION_FAILURES_BY_EXACT_MESSAGE = {
    "Direct Pi exceeded the run token budget": RUN_TOKEN_BUDGET_EXCEEDED,
    "Direct Pi exceeded the run tool-call budget": RUN_TOOL_BUDGET_EXCEEDED,
    "Direct Pi run exceeded its wall-clock budget": RUN_WALL_TIME_BUDGET_EXCEEDED,
    "Direct Pi exceeded the run spend budget": RUN_SPEND_BUDGET_EXCEEDED,
    "Direct Pi reached its output limit": RUN_OUTPUT_BUDGET_EXCEEDED,
    "Direct Pi completed without a public assistant result": MODEL_RESPONSE_FAILED,
}
_ORCHESTRATION_FAILURES_BY_EXACT_MESSAGE = {
    "Direct Pi exhausted its durable spend budget": RUN_SPEND_BUDGET_EXCEEDED,
}
_BRIDGE_FAILURES_BY_INTERNAL_CODE = {
    # The bridge timeout is configured from the remaining durable run wall
    # budget, so this code has one stable public meaning.
    "timeout": RUN_WALL_TIME_BUDGET_EXCEEDED,
    "agent_inactivity_timeout": RUN_WALL_TIME_BUDGET_EXCEEDED,
    "missing_structured_output": PLANNING_CONTRACT_FAILED,
    "invalid_structured_output": PLANNING_CONTRACT_FAILED,
    "structured_output_too_large": PLANNING_CONTRACT_FAILED,
    "unexpected_eof": MODEL_RUNTIME_PROTOCOL_FAILED,
    "unexpected_exit": MODEL_RUNTIME_PROTOCOL_FAILED,
    "invalid_pi_record": MODEL_RUNTIME_PROTOCOL_FAILED,
    "invalid_utf8": MODEL_RUNTIME_PROTOCOL_FAILED,
    "line_too_large": MODEL_RUNTIME_PROTOCOL_FAILED,
    "truncated_pi_record": MODEL_RUNTIME_PROTOCOL_FAILED,
    "malformed_pi_json": MODEL_RUNTIME_PROTOCOL_FAILED,
    "malformed_pi_record": MODEL_RUNTIME_PROTOCOL_FAILED,
    "invalid_message_delta": MODEL_RUNTIME_PROTOCOL_FAILED,
    "unknown_message_delta": MODEL_RUNTIME_PROTOCOL_FAILED,
    "unknown_pi_event": MODEL_RUNTIME_PROTOCOL_FAILED,
    # OpenCode emits only these stable internal codes. Provider/model response
    # bodies are never copied into the public contract.
    "opencode_model_failed": MODEL_RESPONSE_FAILED,
    "opencode_runtime_failed": CODING_AGENT_RUNTIME_FAILED,
    # Codex CLI failures are reduced to this closed set by the root-owned
    # JSONL bridge; arbitrary stderr/provider text never crosses the boundary.
    "codex_model_failed": MODEL_RESPONSE_FAILED,
    "codex_protocol_failed": MODEL_RUNTIME_PROTOCOL_FAILED,
    "codex_structured_output_invalid": PLANNING_CONTRACT_FAILED,
    "codex_runtime_failed": CODING_AGENT_RUNTIME_FAILED,
    "codex_invalid_environment": CODING_AGENT_RUNTIME_FAILED,
    "codex_spawn_failed": CODING_AGENT_RUNTIME_FAILED,
    "codex_bridge_failed": CODING_AGENT_RUNTIME_FAILED,
    "codex_profile_unsupported": CODING_AGENT_RUNTIME_FAILED,
    "codex_thinking_unsupported": CODING_AGENT_RUNTIME_FAILED,
}


def classify_direct_pi_failure(error: BaseException) -> PublicRunFailure:
    """Classify only known exception types and exact internal sentinel messages.

    Arbitrary exception text is never copied into the returned contract. Causes
    are inspected only so a safe known infrastructure/budget error remains
    visible when an internal wrapper preserves it.
    """

    # Local imports avoid a module cycle: DirectPiSession imports the event
    # writer, and the event writer also uses this module's public constants.
    from fomo.direct_pi.session import DirectPiSessionError
    from fomo.direct_pi.workspace import WorkspaceContractError
    from fomo.fomo_pi_ds import (
        InferenceGatewayError,
        PiBridgeFailed,
        PiBridgeProtocolError,
    )

    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, InferenceGatewayError):
            return INFERENCE_GATEWAY_UNAVAILABLE
        if isinstance(current, WorkspaceContractError):
            return WORKSPACE_CONTRACT_FAILED
        if isinstance(current, PlanningContractError):
            return PLANNING_CONTRACT_FAILED
        if isinstance(current, PiBridgeProtocolError):
            return MODEL_RUNTIME_PROTOCOL_FAILED
        if isinstance(current, PiBridgeFailed):
            failure = _BRIDGE_FAILURES_BY_INTERNAL_CODE.get(
                current.payload.get("code")
            )
            if failure is not None:
                return failure
        exact_message = _single_string_argument(current)
        if isinstance(current, DirectPiSessionError) and exact_message is not None:
            failure = _SESSION_FAILURES_BY_EXACT_MESSAGE.get(exact_message)
            if failure is not None:
                return failure
        if isinstance(current, DirectPiOrchestrationError) and exact_message is not None:
            failure = _ORCHESTRATION_FAILURES_BY_EXACT_MESSAGE.get(exact_message)
            if failure is not None:
                return failure
        current = current.__cause__ or current.__context__
    return CODING_AGENT_FAILED


def public_bridge_failure(code: object) -> PublicRunFailure:
    """Map an untrusted bridge failure code to the same closed public set."""

    if isinstance(code, str):
        known = PUBLIC_FAILURES_BY_CODE.get(code)
        if known is not None:
            return known
        known = _BRIDGE_FAILURES_BY_INTERNAL_CODE.get(code)
        if known is not None:
            return known
    return CODING_AGENT_FAILED


def public_failure_for_code(code: object) -> PublicRunFailure:
    """Return the canonical browser contract for a persisted terminal code.

    Terminal summaries are deliberately not accepted here. Callers may pass
    only the server-owned code; unknown or legacy codes fail closed to the
    generic Coding Agent contract.
    """

    if isinstance(code, str):
        known = PUBLIC_FAILURES_BY_CODE.get(code)
        if known is not None:
            return known
    return CODING_AGENT_FAILED


def _single_string_argument(error: BaseException) -> str | None:
    args = error.args
    if len(args) != 1 or not isinstance(args[0], str):
        return None
    return args[0]
