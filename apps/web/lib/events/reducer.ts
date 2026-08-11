import type { UIMessageChunk } from "ai";

import {
  agentRoles,
  agentStages,
  artifactKinds,
  toRunStatus,
  type AcceptanceTrace,
  type AgentDataParts,
  type AgentMessageMetadata,
  type AgentRole,
  type AgentStage,
  type AgentWorklogItem,
  type ArtifactRef,
  type CommandLog,
  type ContextUsageSnapshot,
  type DomainEvent,
  type FileChange,
  type GoalGraphProjection,
  type GoalProjection,
  type PreviewRef,
  type Problem,
  type RoleActivity,
  type RoleStatus,
  type RunPresentation,
  type RunRuntimeResponse,
  type RunSnapshot,
  type StageActivity,
  type UserInputAnswerResponse,
  type UserInputRequest,
  type VerificationResult,
  type VersionSummary,
} from "@/lib/contracts";
import { goalGraphRecord, goalGraphText, normalizeGoalGraph, normalizeGoalProjection } from "@/lib/goal-graph";

const maxActivityItems = 80;
const maxInputRequests = maxActivityItems;
const maxPublicProgressCharacters = 1_200;
const maxPublicDetailCharacters = 280;
const maxPublicCommandOutputCharacters = 8_000;

const publicFailureContracts = {
  inference_gateway_unavailable: {
    title: "模型运行问题：服务不可用",
    message: "模型服务暂时不可用，请稍后重试。",
  },
  run_token_budget_exceeded: {
    title: "模型运行问题：Token 上限",
    message: "本次任务已达到 Token 使用上限，请缩小任务范围后重试。",
  },
  run_tool_budget_exceeded: {
    title: "Agent 运行问题：工具调用上限",
    message: "本次任务已达到工具调用上限，请缩小任务范围后重试。",
  },
  run_wall_time_budget_exceeded: {
    title: "模型运行问题：运行超时",
    message: "本次任务已达到最长运行时间，请缩小任务范围后重试。",
  },
  run_spend_budget_exceeded: {
    title: "模型运行问题：费用上限",
    message: "本次任务已达到费用上限，请缩小任务范围后重试。",
  },
  run_output_budget_exceeded: {
    title: "模型运行问题：输出上限",
    message: "模型单次输出已达到上限，请重试或缩小任务范围。",
  },
  model_runtime_protocol_failed: {
    title: "模型运行问题：响应协议失败",
    message: "模型运行协议未能完整结束，未获得可用结果。请重试或切换模型。",
  },
  model_response_failed: {
    title: "模型运行问题：无可用结果",
    message: "模型未返回可用的公开结果。请重试、切换模型或缩小任务范围。",
  },
  coding_agent_runtime_failed: {
    title: "Coding Agent 运行环境问题",
    message: "Coding Agent 运行环境暂时不可用，请重试；若问题持续发生，请检查所选 Agent 框架状态。",
  },
  agent_capability_unavailable: {
    title: "Coding Agent 工具不可用",
    message: "当前 Coding Agent 未获得完成此阶段所需的仓库或命令工具，本轮没有进入代码验收。",
  },
  agent_no_effect: {
    title: "Coding Agent 未产生代码变更",
    message: "Coding Agent 已结束本轮开发，但工作区没有产生可验收的代码变更。",
  },
  planning_contract_failed: {
    title: "模型运行问题：规划输出无效",
    message: "模型未能按要求返回有效的产品规划合约。请重试或切换模型。",
  },
  workspace_contract_failed: {
    title: "工作区契约失败",
    message: "生成结果未通过工作区安全契约检查，未被发布。请移除密钥或环境文件、受保护文件改动、符号链接或超出限制的文件后重试。",
  },
  direct_pi_verification_failed: {
    title: "确定性验收失败",
    message: "确定性验收仍有阻塞项，任务未发布。请查看失败的类型检查、构建或浏览器验收结果后重试。",
  },
  goal_verification_failed: {
    title: "目标验收失败",
    message: "当前开发目标未通过确定性验收，且自动修复次数已用完。请查看失败门禁并缩小需求后重试。",
  },
  goal_typecheck_failed: {
    title: "类型检查失败",
    message: "当前开发目标未通过 TypeScript 类型检查，且自动修复未成功。请查看类型检查结果后重试。",
  },
  goal_graph_final_reverification_failed: {
    title: "发布复验失败",
    message: "最终候选在完整复验中失败，未被发布。请查看发布门禁结果后重试。",
  },
  direct_pi_infrastructure_failed: {
    title: "验收基础设施异常",
    message: "验收基础设施暂时不可用，无法可信判断生成结果。请稍后重试；这不代表应用代码一定有问题。",
  },
  goal_verification_infrastructure_failed: {
    title: "目标验收基础设施异常",
    message: "当前目标的验收基础设施未能完成检查。请稍后重试；这不代表当前代码一定有问题。",
  },
  worker_lease_expired: {
    title: "执行 Worker 中断",
    message: "执行 Worker 在任务完成前失去租约，任务已中止。请重试；若持续发生，请检查 Worker 状态。",
  },
  continuation_answer_missing: {
    title: "澄清回答未保存",
    message: "澄清问题的回答未能持久保存，任务无法继续。请重新提交该需求。",
  },
  continuation_cursor_invalid: {
    title: "澄清阶段已失效",
    message: "澄清回答对应的运行阶段已失效，任务无法安全继续。请重新提交该需求。",
  },
  pi_session_resume_unavailable: {
    title: "Agent 会话无法续接",
    message: "原 Coding Agent 会话或沙箱已不可用，无法安全续接。请重新提交该需求。",
  },
  p0_continuation_unsupported: {
    title: "当前模式无法续接",
    message: "当前兼容运行模式不支持安全续接澄清回答。请重新提交该需求。",
  },
  repair_no_progress: {
    title: "自动修复没有进展",
    message: "自动修复后再次出现同一阻塞问题，任务已停止。请查看失败门禁并调整需求后重试。",
  },
  repair_limit_reached: {
    title: "自动修复次数已用完",
    message: "自动修复次数已用完，仍有阻塞性验收失败。请查看失败门禁后重试。",
  },
  sop_execution_error: {
    title: "执行 Worker 异常停止",
    message: "执行 Worker 在验收完成前异常停止。请重试；若持续发生，请检查 Worker 状态。",
  },
  coding_agent_failed: {
    title: "Coding Agent 运行失败",
    message: "Coding Agent 运行失败，请重试；若问题持续发生，请检查服务状态。",
  },
} as const satisfies Record<string, { message: string; title: string }>;

const genericFailureContract = publicFailureContracts.coding_agent_failed;

const roleLabels: Record<AgentRole, string> = {
  product_manager: "Product manager",
  architect: "Architect",
  engineer: "Engineer",
  reviewer: "Reviewer",
};

const stageLabels: Record<AgentStage, string> = {
  planning: "Plan",
  building: "Build",
  verifying: "Verify",
  repairing: "Repair",
};

const phaseStages: Record<string, AgentStage | undefined> = {
  preparing: "planning",
  planning: "planning",
  product_analysis: "planning",
  architecture: "planning",
  building: "building",
  implementation: "building",
  verifying: "verifying",
  verification: "verifying",
  publishing: "verifying",
  repairing: "repairing",
  repair: "repairing",
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asText(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asRole(value: unknown): AgentRole | undefined {
  return agentRoles.includes(value as AgentRole) ? (value as AgentRole) : undefined;
}

function asStage(value: unknown): AgentStage | undefined {
  return agentStages.includes(value as AgentStage) ? (value as AgentStage) : undefined;
}

function asInputStage(value: unknown): UserInputRequest["stage"] | undefined {
  return ["planning", "building", "repairing"].includes(value as string)
    ? value as UserInputRequest["stage"]
    : undefined;
}

function publicChoices(value: unknown): string[] | undefined {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.length > 8) return undefined;
  const choices: string[] = [];
  for (const candidate of value) {
    if (typeof candidate !== "string") return undefined;
    const choice = candidate.trim();
    if (!choice || choice.length > 200 || choices.includes(choice)) return undefined;
    choices.push(choice);
  }
  return choices;
}

function asThinkingLevel(value: unknown): "off" | "medium" | "high" | "max" | undefined {
  return ["off", "medium", "high", "max"].includes(value as string)
    ? value as "off" | "medium" | "high" | "max"
    : undefined;
}

function asRoleStatus(value: unknown, fallback: RoleStatus): RoleStatus {
  const statuses = new Set<RoleStatus>(["idle", "queued", "working", "completed", "failed"]);
  return statuses.has(value as RoleStatus) ? (value as RoleStatus) : fallback;
}

function replaceById<T extends { id: string }>(items: T[], incoming: T, max = maxActivityItems): T[] {
  const index = items.findIndex((item) => item.id === incoming.id);
  if (index === -1) {
    return [...items, incoming].slice(-max);
  }
  const next = [...items];
  next[index] = { ...next[index], ...incoming };
  return next;
}

function redactPublicText(value: string): string {
  return value
    .replace(/(authorization\s*:\s*bearer\s+)[^\s,;]+/gi, "$1[REDACTED]")
    .replace(/((?:api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*)[^\s,;]+/gi, "$1[REDACTED]")
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, "[REDACTED]")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "");
}

function boundedPublicText(value: string, limit: number): string {
  const redacted = redactPublicText(value);
  return redacted.length <= limit ? redacted : `${redacted.slice(0, Math.max(0, limit - 1))}…`;
}

function publicFailureContract(payload: Record<string, unknown>): { message: string; title: string } {
  const codeValue = payload.code ?? payload.errorCode ?? payload.error_code;
  const code = typeof codeValue === "string" && codeValue.length <= 80
    ? codeValue
    : "";
  return publicFailureContracts[code as keyof typeof publicFailureContracts]
    || genericFailureContract;
}

function publicFailureDetail(payload: Record<string, unknown>): string {
  // The server-provided message/summary is intentionally ignored. Only its
  // stable code can select browser text, so provider bodies and exception
  // strings remain private even if an upstream boundary regresses.
  const diagnostic = asRecord(payload.diagnostic);
  const safeReasonCodes = new Set([
    "repository_tools_unavailable",
    "runtime_capability_attestation_invalid",
    "runtime_capability_attestation_missing",
    "agent_no_effect",
    "protected_file_changed",
    "protected_file_missing",
    "rejected_secret_file",
    "unsupported_source_type",
    "invalid_source_encoding",
    "workspace_contract_rejected",
    "playwright_report_untrusted",
    "verification_dependencies_timeout",
    "verification_restore_failed",
    "verification_runner_unavailable",
  ]);
  const safeStages = new Set(["initializing", "planning", "building", "repairing", "verifying", "publishing"]);
  const safeCategories = new Set(["runtime_failed", "product_failed", "infrastructure_failed", "workspace_rejected"]);
  const trustedDiagnostic = diagnostic.version === 1
    && safeReasonCodes.has(asText(diagnostic.reasonCode))
    && safeStages.has(asText(diagnostic.stage))
    && safeCategories.has(asText(diagnostic.category));
  const frames = trustedDiagnostic && Array.isArray(diagnostic.frames) && diagnostic.frames.length <= 4
    ? diagnostic.frames.flatMap((value) => {
        if (typeof value !== "string") return [];
        const frame = boundedPublicText(value, 240).trim();
        return frame ? [frame] : [];
      })
    : [];
  return frames.length > 0
    ? frames.join(" → ")
    : publicFailureContract(payload).message;
}

const legacyFailureCodesByExactReason = {
  InferenceGatewayError: "inference_gateway_unavailable",
  PiBridgeProtocolError: "model_runtime_protocol_failed",
  PlanningContractError: "planning_contract_failed",
  WorkspaceContractError: "workspace_contract_failed",
  "direct typecheck failed after same-goal repair": "goal_typecheck_failed",
  "goal repair rounds exhausted": "goal_verification_failed",
  "run-total repair rounds exhausted during typecheck": "goal_typecheck_failed",
  "verification infrastructure failed": "goal_verification_infrastructure_failed",
} as const satisfies Record<string, keyof typeof publicFailureContracts>;

function legacyFailureCode(payload: Record<string, unknown>): keyof typeof publicFailureContracts | undefined {
  const reason = payload.reason ?? payload.errorType ?? payload.error_type;
  if (typeof reason !== "string") return undefined;
  return legacyFailureCodesByExactReason[reason as keyof typeof legacyFailureCodesByExactReason];
}

function inferredLegacyFailureCode(events: DomainEvent[]): keyof typeof publicFailureContracts | undefined {
  for (const event of [...events].reverse()) {
    const directCode = event.payload.code ?? event.payload.errorCode ?? event.payload.error_code;
    if (
      ["pi.failed", "run.failed"].includes(event.kind)
      && typeof directCode === "string"
      && directCode !== "coding_agent_failed"
      && directCode in publicFailureContracts
    ) {
      return directCode as keyof typeof publicFailureContracts;
    }
    const code = ["goal.failed", "goal_graph.failed", "pi.failed"].includes(event.kind)
      ? legacyFailureCode(event.payload)
      : undefined;
    if (code) return code;
  }
  return undefined;
}

function safePathFromToolArgs(value: unknown): string | undefined {
  const args = asRecord(value);
  const path = asText(
    args.path || args.file || args.filePath || args.file_path || args.directory || args.cwd,
  ).replace(/[\r\n\t]/g, " ").trim();
  return path ? boundedPublicText(path, 180) : undefined;
}

function safeToolAction(toolNameValue: unknown, args: unknown): { title: string; detail?: string } {
  const toolName = asText(toolNameValue).toLowerCase();
  const path = safePathFromToolArgs(args);
  const known: Record<string, string> = {
    bash: "Run sandbox command",
    edit: "Edit file",
    find: "Find files",
    grep: "Search source",
    ls: "List directory",
    read: "Read file",
    submit_structured_output: "Submit structured plan",
    write: "Write file",
  };
  if (known[toolName]) {
    return {
      title: known[toolName],
      ...(!["bash", "submit_structured_output"].includes(toolName) && path ? { detail: path } : {}),
    };
  }
  const safeName = toolName.replace(/[^a-z0-9._-]/g, "").slice(0, 40);
  return { title: safeName ? `Use ${safeName} tool` : "Use agent tool" };
}

function upsertWorklog(items: AgentWorklogItem[], incoming: AgentWorklogItem): AgentWorklogItem[] {
  const previous = items.find((item) => item.id === incoming.id);
  const withoutPrevious = items.filter((item) => item.id !== incoming.id);
  return [...withoutPrevious, { ...previous, ...incoming }].slice(-maxActivityItems);
}

function defaultStages(): Record<AgentStage, StageActivity> {
  return Object.fromEntries(
    agentStages.map((stage) => [stage, { stage, status: "idle", title: stageLabels[stage] }]),
  ) as Record<AgentStage, StageActivity>;
}

function advanceStages(
  current: Record<AgentStage, StageActivity>,
  phase: string,
  updatedAt?: string,
): Record<AgentStage, StageActivity> {
  const next = Object.fromEntries(
    agentStages.map((stage) => [stage, { ...current[stage] }]),
  ) as Record<AgentStage, StageActivity>;
  if (phase === "ready") {
    for (const stage of agentStages) {
      if (stage !== "repairing" || next[stage].status !== "idle") {
        next[stage] = { ...next[stage], status: "completed", updatedAt };
      }
    }
    return next;
  }
  const target = phaseStages[phase];
  if (!target) return next;
  for (const stage of agentStages) {
    if (next[stage].status === "working") {
      next[stage] = { ...next[stage], status: "completed", updatedAt };
    }
  }
  const targetIndex = agentStages.indexOf(target);
  for (let index = 0; index < targetIndex; index += 1) {
    const stage = agentStages[index];
    if (stage !== "repairing") {
      next[stage] = { ...next[stage], status: "completed", updatedAt };
    }
  }
  next[target] = {
    ...next[target],
    status: "working",
    detail: phase.replaceAll("_", " "),
    updatedAt,
  };
  return next;
}

export function createRunPresentation(input: {
  projectId: string;
  run?: RunSnapshot;
  runtime?: RunRuntimeResponse;
  versions?: VersionSummary[];
  preview?: PreviewRef;
  goalGraph?: GoalGraphProjection | null;
  pendingInputRequest?: UserInputRequest;
}): RunPresentation {
  const stages = advanceStages(defaultStages(), input.run?.phase || "", input.run?.updatedAt);
  const pendingInputRequest = input.pendingInputRequest || input.run?.pendingInputRequest;
  return {
    runId: input.run?.id || "",
    projectId: input.projectId,
    status: input.run?.status || "queued",
    lastSeq: input.run?.lastSeq || 0,
    stages,
    commands: [],
    verifications: [],
    problems: [],
    versions: input.versions || [],
    preview: input.preview,
    worklog: [],
    inputRequests: pendingInputRequest ? [pendingInputRequest] : [],
    goalGraph: input.goalGraph ?? null,
    contextUsage: undefined,
    agentFramework: input.run?.agentFramework,
    runtime: input.runtime ?? input.run?.runtime,
  };
}

function contextUsageFromEvent(event: DomainEvent): ContextUsageSnapshot | undefined {
  if (event.kind !== "pi.started" && event.kind !== "pi.completed") return undefined;
  const contextTokens = asNumber(event.payload.contextTokens ?? event.payload.context_tokens);
  const contextWindow = asNumber(event.payload.contextWindow ?? event.payload.context_window);
  if (contextTokens === undefined && contextWindow === undefined) return undefined;
  return {
    contextTokens,
    contextWindow,
    boundary: event.kind === "pi.started" ? "turn_started" : "turn_completed",
    capturedAt: event.occurredAt,
  };
}

/** Replays a persisted snapshot from its first event before advancing its cursor. */
export function hydrateRunPresentationFromSnapshot(input: {
  events: DomainEvent[];
  lastSeq: number;
  preview?: PreviewRef;
  projectId: string;
  run?: RunSnapshot;
  versions?: VersionSummary[];
  goalGraph?: GoalGraphProjection | null;
  pendingInputRequest?: UserInputRequest;
}): RunPresentation {
  const replayRun = input.run ? { ...input.run, lastSeq: 0 } : undefined;
  let presentation = createRunPresentation({
    projectId: input.projectId,
    run: replayRun,
    versions: input.versions,
    preview: input.preview,
    goalGraph: input.goalGraph,
    pendingInputRequest: input.pendingInputRequest || input.run?.pendingInputRequest,
  });
  for (const event of [...input.events].sort((left, right) => left.seq - right.seq)) {
    presentation = reduceDomainEvent(presentation, event);
  }
  const inferredFailureCode = inferredLegacyFailureCode(input.events);
  const snapshotFailureCode = input.run?.errorCode;
  const specificSnapshotCode = snapshotFailureCode
    && snapshotFailureCode !== "coding_agent_failed"
    && snapshotFailureCode in publicFailureContracts
    ? snapshotFailureCode as keyof typeof publicFailureContracts
    : undefined;
  const terminalFailureCode = specificSnapshotCode || inferredFailureCode || snapshotFailureCode;
  const terminalFailure = input.run
    && ["failed", "needs_attention"].includes(input.run.status)
    && terminalFailureCode
    ? publicFailureContract({ code: terminalFailureCode })
    : undefined;
  let worklog = presentation.worklog;
  let problems = presentation.problems;
  if (terminalFailure && input.run) {
    worklog = upsertWorklog(worklog, {
      id: "system:run:failure",
      kind: "system",
      status: "failed",
      title: terminalFailure.title,
      detail: terminalFailure.message,
      occurredAt: input.run.updatedAt || input.run.createdAt || "",
      seq: Math.max(presentation.lastSeq, input.lastSeq, input.run.lastSeq),
    });
    const piFailure = worklog.find((item) => item.id === "system:pi:failure");
    if (piFailure) {
      worklog = upsertWorklog(worklog, {
        ...piFailure,
        title: terminalFailure.title,
        detail: terminalFailure.message,
      });
    }
    const terminalProblemIndexes = problems
      .map((problem, index) => problem.id.startsWith("failure:") ? index : -1)
      .filter((index) => index >= 0);
    if (terminalProblemIndexes.length > 0) {
      const lastTerminalIndex = terminalProblemIndexes.at(-1)!;
      problems = problems.map((problem, index) => index === lastTerminalIndex
        ? { ...problem, title: terminalFailure.message }
        : problem);
    } else {
      problems = [...problems, {
        id: "failure:snapshot",
        title: terminalFailure.message,
        severity: "error" as const,
      }].slice(-maxActivityItems);
    }
  }
  return {
    ...presentation,
    // A non-null snapshot projection is the server's final read model. Replay
    // remains useful for the rest of the presentation, but an older
    // goal_graph.created event must not replace the authoritative graph.
    goalGraph: input.goalGraph ?? presentation.goalGraph,
    inputRequests: reconcileInputRequestSnapshot(
      presentation,
      input.pendingInputRequest || input.run?.pendingInputRequest,
    ).inputRequests,
    worklog,
    problems,
    lastSeq: Math.max(presentation.lastSeq, input.lastSeq, input.run?.lastSeq || 0),
  };
}

function inputRequestFromEvent(event: DomainEvent): UserInputRequest | undefined {
  if (event.kind !== "run.input_requested") return undefined;
  const id = asText(event.payload.requestId || event.payload.request_id);
  const question = asText(event.payload.question).trim();
  const stage = asInputStage(event.payload.stage);
  const allowFreeformValue = event.payload.allowFreeform ?? event.payload.allow_freeform;
  const choices = publicChoices(event.payload.choices);
  if (
    !id
    || !question
    || question.length > 2_000
    || !stage
    || choices === undefined
    || typeof allowFreeformValue !== "boolean"
    || (!allowFreeformValue && choices.length === 0)
  ) return undefined;
  return {
    id,
    runId: event.runId,
    question,
    choices,
    allowFreeform: allowFreeformValue,
    status: "pending",
    stage,
    goalId: asText(event.payload.goalId || event.payload.goal_id) || undefined,
    createdAt: event.occurredAt,
    requestedSeq: event.seq,
  };
}

/** Applies an answer response immediately without advancing the SSE cursor. */
export function reconcileInputAnswer(
  state: RunPresentation,
  response: UserInputAnswerResponse,
): RunPresentation {
  if (response.run.id !== state.runId || response.request.runId !== state.runId) return state;
  const previous = state.inputRequests.find((request) => request.id === response.request.id);
  const answered: UserInputRequest = {
    ...response.request,
    requestedSeq: previous?.requestedSeq,
    resolvedSeq: previous?.resolvedSeq ?? state.lastSeq,
    answerMessageId: response.message.id,
  };
  return {
    ...state,
    // SSE may already have resumed the run before the POST response arrives.
    // Never regress that newer state back to the response's queued snapshot.
    status: previous?.status === "pending" ? response.run.status : state.status,
    inputRequests: replaceById(state.inputRequests, answered, maxInputRequests),
  };
}

/** Reconciles the server's single-pending-request read model while preserving history. */
export function reconcileInputRequestSnapshot(
  state: RunPresentation,
  pendingInputRequest?: UserInputRequest,
): RunPresentation {
  if (pendingInputRequest && pendingInputRequest.runId === state.runId) {
    const previous = state.inputRequests.find((request) => request.id === pendingInputRequest.id);
    const withoutCompetingPending = state.inputRequests.map((request): UserInputRequest => (
      request.id !== pendingInputRequest.id && request.status === "pending"
        ? { ...request, status: "expired", resolvedSeq: request.resolvedSeq ?? state.lastSeq }
        : request
    ));
    return {
      ...state,
      inputRequests: replaceById(withoutCompetingPending, {
        ...pendingInputRequest,
        requestedSeq: previous?.requestedSeq,
      }, maxInputRequests),
    };
  }
  if (!state.inputRequests.some((request) => request.status === "pending")) return state;
  return {
    ...state,
    inputRequests: state.inputRequests.map((request): UserInputRequest => request.status === "pending"
      ? { ...request, status: "expired", resolvedSeq: request.resolvedSeq ?? state.lastSeq }
      : request),
  };
}

const goalEventStatuses: Partial<Record<string, GoalProjection["status"]>> = {
  "goal.activated": "active",
  "goal.claimed": "claimed",
  "goal.failed": "failed",
  "goal.verified": "verified",
  "goal.resumed": "active",
};

const terminalGraphStatuses: Partial<Record<string, GoalGraphProjection["status"]>> = {
  "goal_graph.completed": "verified",
  "goal_graph.verified": "verified",
  "goal_graph.failed": "failed",
  "goal_graph.cancelled": "cancelled",
  "goal_graph.superseded": "superseded",
};

function goalGraphFromEvent(
  current: GoalGraphProjection | null,
  event: DomainEvent,
): GoalGraphProjection | null {
  const payload = event.payload;
  const suppliedGraph = payload.goalGraph || payload.goal_graph || payload.graph || payload.projection;
  const normalizedGraph = normalizeGoalGraph(suppliedGraph || payload);
  if (normalizedGraph) return normalizedGraph;
  if (!current) return null;

  const terminalStatus = terminalGraphStatuses[event.kind];
  if (terminalStatus) {
    return {
      ...current,
      activeGoalId: null,
      revision: asNumber(payload.revision) ?? current.revision,
      status: terminalStatus,
    };
  }

  const goalSource = payload.goal || payload.goalProjection || payload.goal_projection || payload.projection;
  const projectedGoal = normalizeGoalProjection(goalSource);
  const goalRecord = goalGraphRecord(goalSource);
  const goalId = projectedGoal?.goalId || goalGraphText(
    goalRecord.goalId || goalRecord.goal_id || payload.goalId || payload.goal_id,
  );
  if (!goalId) return current;
  const existing = current.goals.find((goal) => goal.goalId === goalId);
  if (!existing) return current;

  const eventStatus = goalEventStatuses[event.kind];
  const nextGoal: GoalProjection = projectedGoal || {
    ...existing,
    status: (["pending", "active", "claimed", "verified", "failed", "superseded"] as const).includes(
      asText(payload.status) as GoalProjection["status"],
    ) ? asText(payload.status) as GoalProjection["status"] : eventStatus || existing.status,
    checkpointId: asText(payload.checkpointId || payload.checkpoint_id) || existing.checkpointId,
    claimedAt: event.kind === "goal.claimed"
      ? asText(payload.claimedAt || payload.claimed_at, event.occurredAt)
      : existing.claimedAt,
    verifiedAt: event.kind === "goal.verified"
      ? asText(payload.verifiedAt || payload.verified_at, event.occurredAt)
      : existing.verifiedAt,
    evidenceCount: asNumber(payload.evidenceCount ?? payload.evidence_count) ?? existing.evidenceCount,
  };
  return {
    ...current,
    activeGoalId: event.kind === "goal.activated" || event.kind === "goal.resumed"
      ? goalId
      : event.kind === "goal.verified" ? null : current.activeGoalId,
    revision: asNumber(payload.revision) ?? current.revision,
    goals: current.goals.map((goal) => goal.goalId === goalId ? nextGoal : goal),
  };
}

export function activityFromEvent(event: DomainEvent): RoleActivity | undefined {
  const payload = event.payload;
  const role = asRole(event.role) || asRole(payload.role);
  if (!role) {
    return undefined;
  }
  const status: RoleStatus = event.kind.endsWith(".started")
    ? "working"
    : event.kind.endsWith(".completed")
      ? "completed"
      : event.kind.endsWith(".failed")
        ? "failed"
        : asRoleStatus(payload.status, "working");
  return {
    role,
    status,
    title: asText(payload.title || payload.action, roleLabels[role]),
    detail: asText(payload.detail || payload.summary || payload.message) || undefined,
    updatedAt: event.occurredAt,
  };
}

function artifactRefFromEvent(event: DomainEvent): ArtifactRef | undefined {
  const payload = event.payload;
  const kind = asText(payload.kind);
  if (!artifactKinds.includes(kind as (typeof artifactKinds)[number])) {
    return undefined;
  }
  const id = asText(payload.artifactId || payload.artifact_id);
  if (!id) {
    return undefined;
  }
  // A deliberately lightweight loading ref: runId comes only from the trusted
  // event envelope, and no title/summary/schemaVersion/createdAt/content is
  // invented here; the workspace loads the real detail on demand.
  return { id, kind, runId: event.runId, role: event.role || asRole(payload.role) };
}

function traceFromPayload(payload: Record<string, unknown>): AcceptanceTrace[] {
  const candidates = Array.isArray(payload.items)
    ? payload.items
    : Array.isArray(payload.trace)
      ? payload.trace
      : [];
  return candidates.flatMap((candidate) => {
    const item = asRecord(candidate);
    const id = asText(item.id || item.acId || item.ac_id);
    if (!id) {
      return [];
    }
    const evidence = Array.isArray(item.evidence) ? item.evidence : [];
    return [{
      id,
      title: asText(item.title || item.description, "Acceptance criterion"),
      priority: asText(item.priority, "must") as AcceptanceTrace["priority"],
      status: asText(item.status, "pending") as AcceptanceTrace["status"],
      evidence: evidence.flatMap((itemEvidence) => {
        const source = asRecord(itemEvidence);
        const evidenceId = asText(source.id || source.linkId || source.link_id);
        if (!evidenceId) {
          return [];
        }
        return [{
          id: evidenceId,
          type: asText(source.type, "test") as AcceptanceTrace["evidence"][number]["type"],
          label: asText(source.label || source.title, "Evidence"),
          href: asText(source.href || source.url) || undefined,
          status: asText(source.status) as AcceptanceTrace["evidence"][number]["status"],
        }];
      }),
    }];
  });
}

function fileChangeFromEvent(event: DomainEvent): FileChange {
  const payload = event.payload;
  const status = asText(payload.status || payload.change, "modified");
  return {
    id: asText(payload.id || payload.fileChangeId || payload.file_change_id, event.eventId),
    path: asText(payload.path || payload.file, "unknown file"),
    status: ["added", "modified", "deleted", "renamed"].includes(status)
      ? (status as FileChange["status"])
      : "modified",
    additions: asNumber(payload.additions || payload.addedLines || payload.added_lines),
    deletions: asNumber(payload.deletions || payload.deletedLines || payload.deleted_lines),
  };
}

function commandFromEvent(event: DomainEvent, previous?: CommandLog): CommandLog {
  const payload = event.payload;
  const id = asText(payload.operationId || payload.operation_id || payload.commandId || payload.command_id || payload.id, event.eventId);
  const eventOutput = boundedPublicText(
    asText(payload.output || payload.chunk || payload.text),
    maxPublicCommandOutputCharacters,
  );
  const nextOutput = event.kind === "command.output"
    ? boundedPublicText(`${previous?.output || ""}${eventOutput}`, maxPublicCommandOutputCharacters)
    : eventOutput || previous?.output || "";
  const label = boundedPublicText(asText(payload.label), 120).trim();
  return {
    id,
    // Deterministic control-plane commands are auditable by their trusted
    // label; the shell text itself may contain credentials or oversized args.
    command: previous?.command || label || "Run trusted sandbox command",
    output: nextOutput,
    status: event.kind === "command.started"
      ? "running"
      : event.kind === "command.completed" && Number(payload.exitCode ?? payload.exit_code) !== 0
        ? "failed"
        : event.kind === "command.completed"
          ? "completed"
          : previous?.status || "running",
    exitCode: asNumber(payload.exitCode ?? payload.exit_code) ?? previous?.exitCode,
  };
}

function piToolFromEvent(event: DomainEvent, previous?: CommandLog): CommandLog {
  const payload = event.payload;
  const id = `pi:${asText(payload.toolCallId, event.eventId)}`;
  const action = safeToolAction(payload.toolName, payload.args ?? payload);
  return {
    id,
    // Pi arguments can contain complete commands, generated source, or other
    // large/sensitive values. The public terminal keeps only the same safe
    // action label used by the worklog and never serializes those arguments.
    command: previous?.command || [action.title, action.detail].filter(Boolean).join(" · "),
    output: previous?.output || "",
    status: event.kind === "pi.tool.started"
      ? "running"
      : event.kind === "pi.tool.completed" && payload.isError === true
        ? "failed"
        : event.kind === "pi.tool.completed"
          ? "completed"
          : previous?.status || "running",
    exitCode: event.kind === "pi.tool.completed" ? (payload.isError === true ? 1 : 0) : previous?.exitCode,
  };
}

function verificationFromEvent(event: DomainEvent): VerificationResult | undefined {
  const payload = event.payload;
  // The Release gate consumes only closed-set project scope events. Acceptance
  // evidence lives in the AC trace; scope-less legacy events fail closed and
  // never reach the gate.
  if (payload.scope !== "project") {
    return undefined;
  }
  const status = asText(payload.status, event.kind.includes("failed") ? "failed" : "running");
  return {
    id: asText(payload.gateId || payload.id || payload.verificationId || payload.verification_id, event.eventId),
    name: asText(payload.name || payload.check || payload.title, "Verification"),
    status: ["passed", "failed", "running", "skipped"].includes(status)
      ? (status as VerificationResult["status"])
      : "running",
    duration: asNumber(payload.duration || payload.durationMs || payload.duration_ms),
    detail: asText(payload.summary || payload.detail || payload.message) || undefined,
    stack: asText(payload.stack || payload.error) || undefined,
  };
}

function previewFromEvent(event: DomainEvent): PreviewRef {
  const payload = event.payload;
  return {
    status: event.kind === "preview.expired"
      ? "expired"
      : event.kind === "preview.failed"
        ? "failed"
        : "ready",
    url: asText(payload.url) || undefined,
    runId: event.runId,
    error: asText(payload.error || payload.detail) || undefined,
    verificationStatus: event.kind === "preview.verified"
      ? "verified"
      : asText(payload.verificationStatus) === "verified"
        ? "verified"
        : "unverified",
  };
}

function versionFromEvent(event: DomainEvent): VersionSummary {
  const payload = event.payload;
  return {
    id: asText(payload.id || payload.versionId || payload.version_id, event.eventId),
    hash: asText(payload.hash || payload.commitHash || payload.commit_hash) || undefined,
    message: asText(payload.message || payload.title, "Generated version"),
    createdAt: event.occurredAt,
    status: asText(payload.status, "ready") as VersionSummary["status"],
  };
}

function problemsFromEvent(event: DomainEvent): Problem[] {
  const payload = event.payload;
  const candidates = Array.isArray(payload.problems) ? payload.problems : [payload];
  return candidates.flatMap((candidate, index) => {
    const item = asRecord(candidate);
    const title = asText(item.title || item.message || item.error);
    if (!title) {
      return [];
    }
    return [{
      id: asText(item.id || item.problemId || item.problem_id, `${event.eventId}-${index}`),
      title,
      severity: ["error", "major", "minor"].includes(asText(item.severity))
        ? (asText(item.severity) as Problem["severity"])
        : "error",
      file: asText(item.file || item.path) || undefined,
      line: asNumber(item.line),
      stack: asText(item.stack) || undefined,
    }];
  });
}

function worklogItem(
  event: DomainEvent,
  input: Omit<AgentWorklogItem, "occurredAt" | "seq" | "stage"> & { stage?: AgentStage },
): AgentWorklogItem {
  return {
    ...input,
    occurredAt: event.occurredAt,
    seq: event.seq,
    stage: input.stage || asStage(event.payload.stage),
  };
}

function goalWorklogItem(
  event: DomainEvent,
  graph: GoalGraphProjection | null,
): AgentWorklogItem | undefined {
  const payload = event.payload;
  const goalRecord = goalGraphRecord(payload.goal || payload.goalProjection || payload.goal_projection || payload.projection);
  const goalId = goalGraphText(goalRecord.goalId || goalRecord.goal_id || payload.goalId || payload.goal_id);
  const goal = graph?.goals.find((candidate) => candidate.goalId === goalId);
  const detail = boundedPublicText(goal?.title || goalId, maxPublicDetailCharacters) || undefined;
  const lifecycle: Partial<Record<string, { title: string; status: AgentWorklogItem["status"] }>> = {
    "goal_graph.created": { title: "Delivery goals are ready", status: "completed" },
    "goal.activated": { title: "Working on delivery goal", status: "running" },
    "goal.claimed": { title: "Goal checkpoint saved", status: "completed" },
    "goal.verification_failed": { title: "Goal needs repair", status: "failed" },
    "goal.resume_scheduled": { title: "Goal repair scheduled", status: "info" },
    "goal.resumed": { title: "Repairing delivery goal", status: "running" },
    "goal.verified": { title: "Delivery goal verified", status: "completed" },
    "goal.failed": { title: "Delivery goal failed", status: "failed" },
    "goal_graph.completed": { title: "All delivery goals completed", status: "completed" },
    "goal_graph.verified": { title: "All delivery goals verified", status: "completed" },
    "goal_graph.failed": { title: "Delivery goal graph failed", status: "failed" },
    "goal_graph.cancelled": { title: "Delivery goals cancelled", status: "info" },
    "goal_graph.superseded": { title: "Delivery goals superseded", status: "info" },
  };
  const display = lifecycle[event.kind];
  if (!display) return undefined;
  const legacyCode = display.status === "failed" ? legacyFailureCode(payload) : undefined;
  const legacyFailure = legacyCode ? publicFailureContract({ code: legacyCode }) : undefined;
  const graphDetail = legacyFailure?.message || (event.kind === "goal_graph.created"
    ? boundedPublicText(graph?.productOutcome || "", maxPublicDetailCharacters) || undefined
    : detail);
  return worklogItem(event, {
    id: `goal:${goalId || graph?.graphId || "graph"}`,
    kind: "goal",
    status: display.status,
    title: legacyFailure?.title || display.title,
    detail: graphDetail,
  });
}

function projectWorklogEvent(
  state: RunPresentation,
  next: RunPresentation,
  event: DomainEvent,
): Pick<RunPresentation, "worklog" | "activePublicMessageId"> {
  let worklog = state.worklog;
  let activePublicMessageId = state.activePublicMessageId;
  const add = (item: AgentWorklogItem) => {
    worklog = upsertWorklog(worklog, item);
  };

  if (event.kind === "pi.message.delta") {
    const deltaType = asText(event.payload.deltaType || event.payload.delta_type);
    if (!activePublicMessageId) activePublicMessageId = `progress:${event.eventId}`;
    const previous = worklog.find((item) => item.id === activePublicMessageId);
    const delta = deltaType === "text_delta" ? asText(event.payload.delta) : "";
    const detail = boundedPublicText(`${previous?.detail || ""}${delta}`, maxPublicProgressCharacters);
    add(worklogItem(event, {
      id: activePublicMessageId,
      kind: "progress",
      status: "running",
      title: "Agent's current judgment",
      detail: detail || previous?.detail,
      stage: previous?.stage,
    }));
    return { worklog, activePublicMessageId };
  }

  if (event.kind === "pi.message.completed") {
    if (asText(event.payload.role, "assistant") !== "assistant") {
      return { worklog, activePublicMessageId };
    }
    const publicText = boundedPublicText(asText(event.payload.text), maxPublicProgressCharacters).trim();
    if (activePublicMessageId) {
      const previous = worklog.find((item) => item.id === activePublicMessageId);
      add(worklogItem(event, {
        id: activePublicMessageId,
        kind: "progress",
        status: "completed",
        title: "Agent progress update",
        detail: previous?.detail?.trim() || publicText || undefined,
        stage: previous?.stage,
      }));
    } else if (publicText) {
      add(worklogItem(event, {
        id: `progress:${event.eventId}`,
        kind: "progress",
        status: "completed",
        title: "Agent progress update",
        detail: publicText,
      }));
    }
    return { worklog, activePublicMessageId: undefined };
  }

  if (event.kind === "pi.tool.started" || event.kind === "pi.tool.completed") {
    const id = `tool:${asText(event.payload.toolCallId, event.eventId)}`;
    const previous = worklog.find((item) => item.id === id);
    const action = safeToolAction(event.payload.toolName, event.payload.args ?? event.payload);
    const failed = event.kind === "pi.tool.completed" && event.payload.isError === true;
    add(worklogItem(event, {
      id,
      kind: "tool",
      status: event.kind === "pi.tool.started" ? "running" : failed ? "failed" : "completed",
      title: previous?.title || action.title,
      detail: previous?.detail || action.detail,
      stage: previous?.stage,
    }));
    return { worklog, activePublicMessageId };
  }

  if (event.kind === "file.changed") {
    const file = fileChangeFromEvent(event);
    const changeLabel = file.status === "added" ? "File added" : file.status === "deleted" ? "File deleted" : file.status === "renamed" ? "File renamed" : "File modified";
    const counts = [
      file.additions === undefined ? "" : `+${file.additions}`,
      file.deletions === undefined ? "" : `−${file.deletions}`,
    ].filter(Boolean).join(" ");
    add(worklogItem(event, {
      id: `file:${file.id}`,
      kind: "file",
      status: "completed",
      title: changeLabel,
      detail: boundedPublicText(`${file.path}${counts ? ` · ${counts}` : ""}`, maxPublicDetailCharacters),
    }));
    return { worklog, activePublicMessageId };
  }

  if (event.kind === "verification.updated") {
    const verification = verificationFromEvent(event);
    if (!verification) return { worklog, activePublicMessageId };
    const status: AgentWorklogItem["status"] = verification.status === "running"
      ? "running"
      : verification.status === "failed" ? "failed" : "completed";
    add(worklogItem(event, {
      id: `verification:${verification.id}`,
      kind: "verification",
      status,
      title: `${verification.status === "running" ? "Running QA" : verification.status === "failed" ? "QA failed" : verification.status === "skipped" ? "QA skipped" : "QA passed"}: ${boundedPublicText(verification.name, 100)}`,
      detail: verification.detail ? boundedPublicText(verification.detail, maxPublicDetailCharacters) : undefined,
    }));
    return { worklog, activePublicMessageId };
  }

  if (event.kind.startsWith("goal.") || event.kind.startsWith("goal_graph.")) {
    const item = goalWorklogItem(event, next.goalGraph);
    if (item) add(item);
    return { worklog, activePublicMessageId };
  }

  if (event.kind === "run.failed") {
    const failure = publicFailureContract(event.payload);
    const causalDetail = [...state.worklog].reverse().find(
      (item) => item.id === "system:pi:failure" && item.status === "failed" && item.detail,
    )?.detail;
    add(worklogItem(event, {
      id: "system:run:failure",
      kind: "system",
      status: "failed",
      title: failure.title,
      detail: causalDetail || failure.message,
    }));
    return { worklog, activePublicMessageId: undefined };
  }

  const piLifecycle: Partial<Record<string, { title: string; status: AgentWorklogItem["status"]; detail?: string }>> = {
    "pi.started": { title: "Coding Agent connected", status: "running" },
    "pi.completed": { title: "Coding Agent turn completed", status: "completed" },
    "pi.failed": { title: "Coding Agent 运行失败", status: "failed" },
  };
  const piDisplay = piLifecycle[event.kind];
  if (piDisplay) {
    const failure = event.kind === "pi.failed"
      ? publicFailureContract(event.payload)
      : undefined;
    const stage = asStage(event.payload.stage);
    const thinkingLevel = event.kind === "pi.started" ? asThinkingLevel(event.payload.thinkingLevel) : undefined;
    const contextWindow = event.kind === "pi.started" ? asNumber(event.payload.contextWindow) : undefined;
    const runtimeDetail = [
      thinkingLevel ? `thinkingLevel=${thinkingLevel}` : "",
      contextWindow === undefined ? "" : `contextWindow=${Math.round(contextWindow)} tokens`,
    ].filter(Boolean).join(" · ") || undefined;
    const lifecycleDetail = event.kind === "pi.failed"
      ? publicFailureDetail(event.payload)
      : runtimeDetail;
    add(worklogItem(event, {
      id: event.kind === "pi.failed" ? "system:pi:failure" : `system:pi:${stage || "turn"}`,
      kind: "system",
      status: piDisplay.status,
      title: failure?.title || piDisplay.title,
      ...(lifecycleDetail ? { detail: lifecycleDetail } : {}),
      stage,
    }));
    if (event.kind === "pi.failed") activePublicMessageId = undefined;
    return { worklog, activePublicMessageId };
  }

  if (event.kind === "pi.activity") {
    const activity = asText(event.payload.activity);
    const attempt = asNumber(event.payload.attempt);
    const activities: Partial<Record<string, { title: string; status: AgentWorklogItem["status"]; detail?: string }>> = {
      compaction_start: { title: "Compressing working context", status: "running" },
      compaction_end: { title: "Working context compressed", status: "completed" },
      auto_retry_start: { title: "Retrying the model request", status: "running", detail: attempt === undefined ? undefined : `Attempt ${attempt}` },
      auto_retry_end: { title: event.payload.success === true ? "Model retry succeeded" : "Model retry ended", status: event.payload.success === true ? "completed" : "failed" },
      extension_error: { title: "Agent extension reported an error", status: "failed" },
    };
    const display = activities[activity];
    if (display) {
      const stage = asStage(event.payload.stage);
      const lifecycleId = activity.startsWith("compaction_")
        ? "compaction"
        : activity.startsWith("auto_retry_") ? `retry:${attempt || "current"}` : activity;
      add(worklogItem(event, {
        id: `system:${stage || "turn"}:${lifecycleId}`,
        kind: "system",
        status: display.status,
        title: display.title,
        detail: display.detail,
        stage,
      }));
    }
  }

  return { worklog, activePublicMessageId };
}

export function reduceDomainEvent(state: RunPresentation, event: DomainEvent): RunPresentation {
  if (event.runId !== state.runId && state.runId) {
    return state;
  }
  if (event.seq <= state.lastSeq) {
    return state;
  }

  const next: RunPresentation = {
    ...state,
    projectId: event.projectId,
    runId: event.runId,
    lastSeq: event.seq,
    disconnected: false,
  };
  const contextUsage = contextUsageFromEvent(event);
  if (contextUsage) {
    next.contextUsage = {
      ...contextUsage,
      contextTokens: contextUsage.contextTokens ?? state.contextUsage?.contextTokens,
      contextWindow: contextUsage.contextWindow ?? state.contextUsage?.contextWindow,
    };
  }

  switch (event.kind) {
    case "run.created":
    case "run.status_changed":
      next.status = toRunStatus(event.payload.status, state.status);
      next.stages = advanceStages(state.stages, asText(event.payload.phase), event.occurredAt);
      break;
    case "run.completed":
      next.status = "completed";
      next.stages = advanceStages(state.stages, "ready", event.occurredAt);
      break;
    case "run.failed":
      next.status = toRunStatus(event.payload.status, "failed");
      next.stages = Object.fromEntries(agentStages.map((stage) => [
        stage,
        state.stages[stage].status === "working"
          ? { ...state.stages[stage], status: "failed", updatedAt: event.occurredAt }
          : state.stages[stage],
      ])) as Record<AgentStage, StageActivity>;
      next.problems = [...state.problems, {
        id: `failure:${event.eventId}`,
        title: [...state.worklog].reverse().find(
          (item) => item.id === "system:pi:failure" && item.status === "failed" && item.detail,
        )?.detail || publicFailureDetail(event.payload),
        severity: "error" as const,
      }].slice(-maxActivityItems);
      break;
    case "run.cancelled":
      next.status = "cancelled";
      next.inputRequests = state.inputRequests.map((request): UserInputRequest => request.status === "pending"
        ? { ...request, status: "cancelled", resolvedSeq: event.seq }
        : request);
      break;
    case "run.waiting_for_user":
      next.status = "waiting_for_user";
      break;
    case "run.input_requested": {
      const request = inputRequestFromEvent(event);
      if (request) {
        next.status = "waiting_for_user";
        const withoutCompetingPending = state.inputRequests.map((existing): UserInputRequest => (
          existing.id !== request.id && existing.status === "pending"
            ? { ...existing, status: "expired", resolvedSeq: event.seq }
            : existing
        ));
        next.inputRequests = replaceById(withoutCompetingPending, request, maxInputRequests);
      }
      break;
    }
    case "run.input_answered": {
      const requestId = asText(event.payload.requestId || event.payload.request_id);
      const messageId = asText(event.payload.messageId || event.payload.message_id);
      next.inputRequests = state.inputRequests.map((request): UserInputRequest => request.id === requestId
        ? {
            ...request,
            status: "answered",
            answeredAt: event.occurredAt,
            resolvedSeq: event.seq,
            answerMessageId: messageId || request.answerMessageId,
          }
        : request);
      break;
    }
    case "command.started":
    case "command.output":
    case "command.completed": {
      const id = asText(event.payload.operationId || event.payload.operation_id || event.payload.commandId || event.payload.command_id || event.payload.id, event.eventId);
      const previous = state.commands.find((command) => command.id === id);
      next.commands = replaceById(state.commands, commandFromEvent(event, previous));
      break;
    }
    case "pi.tool.started":
    case "pi.tool.output":
    case "pi.tool.completed": {
      const id = `pi:${asText(event.payload.toolCallId, event.eventId)}`;
      const previous = state.commands.find((command) => command.id === id);
      next.commands = replaceById(state.commands, piToolFromEvent(event, previous));
      break;
    }
    case "verification.updated": {
      const verification = verificationFromEvent(event);
      if (!verification) {
        break;
      }
      next.verifications = replaceById(state.verifications, verification);
      if (verification.status === "failed") {
        next.problems = [...state.problems, ...problemsFromEvent(event)].slice(-maxActivityItems);
      }
      break;
    }
    case "preview.ready":
    case "preview.available":
    case "preview.verified":
    case "preview.expired":
    case "preview.failed":
      next.preview = previewFromEvent(event);
      break;
    case "version.created":
    case "version.restored":
      next.versions = replaceById(state.versions, versionFromEvent(event));
      break;
    case "goal_graph.created":
    case "goal.activated":
    case "goal.claimed":
    case "goal.failed":
    case "goal.verification_failed":
    case "goal.verified":
    case "goal.resume_scheduled":
    case "goal.resumed":
    case "goal_graph.completed":
    case "goal_graph.verified":
    case "goal_graph.failed":
    case "goal_graph.cancelled":
    case "goal_graph.superseded":
      next.goalGraph = goalGraphFromEvent(state.goalGraph, event);
      break;
    default:
      break;
  }
  const worklogProjection = projectWorklogEvent(state, next, event);
  next.worklog = worklogProjection.worklog;
  next.activePublicMessageId = worklogProjection.activePublicMessageId;
  return next;
}

type DataName = keyof AgentDataParts;

function dataChunk<Name extends DataName>(
  name: Name,
  data: AgentDataParts[Name],
  id?: string,
): UIMessageChunk<AgentMessageMetadata, AgentDataParts> {
  return {
    type: `data-${name}`,
    data,
    ...(id ? { id } : {}),
  } as UIMessageChunk<AgentMessageMetadata, AgentDataParts>;
}

export function domainEventToMessageChunks(
  event: DomainEvent,
): UIMessageChunk<AgentMessageMetadata, AgentDataParts>[] {
  if (event.kind.startsWith("agent.")) {
    const activity = activityFromEvent(event);
    return activity ? [dataChunk("agent-role", activity, `role-${event.runId}-${activity.role}`)] : [];
  }

  switch (event.kind) {
    case "artifact.upserted": {
      const ref = artifactRefFromEvent(event);
      if (!ref || (ref.kind !== "product_spec" && ref.kind !== "technical_spec")) {
        return [];
      }
      // The only place snake_case backend kinds convert to hyphenated AI
      // data-part names.
      const dataName: "product-spec" | "technical-spec" = ref.kind === "product_spec"
        ? "product-spec"
        : "technical-spec";
      return [dataChunk(dataName, ref, `artifact-${ref.id}`)];
    }
    case "trace.updated":
      return [dataChunk("acceptance-trace", traceFromPayload(event.payload), `trace-${event.runId}`)];
    case "file.changed": {
      const file = fileChangeFromEvent(event);
      return [dataChunk("file-change", file, `file-${file.id}`)];
    }
    case "command.started":
    case "command.output":
    case "command.completed": {
      const command = commandFromEvent(event);
      return [dataChunk("command", command, `command-${command.id}`)];
    }
    case "verification.updated": {
      const verification = verificationFromEvent(event);
      return verification
        ? [dataChunk("verification", verification, `verification-${verification.id}`)]
        : [];
    }
    case "preview.ready":
    case "preview.failed":
      return [dataChunk("preview", previewFromEvent(event), `preview-${event.runId}`)];
    case "version.created":
    case "version.restored": {
      const version = versionFromEvent(event);
      return [dataChunk("version", version, `version-${version.id}`)];
    }
    case "assistant.summary": {
      const summary = asText(event.payload.markdown || event.payload.content || event.payload.summary);
      if (!summary) {
        return [];
      }
      const id = `summary-${event.eventId}`;
      return [
        { type: "text-start", id },
        { type: "text-delta", id, delta: summary },
        { type: "text-end", id },
      ];
    }
    case "pi.started":
    case "pi.completed":
    case "pi.failed":
    case "pi.activity":
    case "pi.message.delta":
    case "pi.message.completed":
    case "pi.tool.started":
    case "pi.tool.output":
    case "pi.tool.completed":
    case "pi.command.output":
    case "run.input_requested":
    case "run.input_answered":
    case "run.resumed":
    case "goal_graph.created":
    case "goal.activated":
    case "goal.claimed":
    case "goal.failed":
    case "goal.verification_failed":
    case "goal.verified":
    case "goal.resume_scheduled":
    case "goal.resumed":
    case "goal_graph.completed":
    case "goal_graph.verified":
    case "goal_graph.failed":
    case "goal_graph.cancelled":
    case "goal_graph.superseded":
      // These are rendered by the bounded worklog projection. Feeding every
      // delta into useChat would duplicate activity and retain noisy payloads.
      return [];
    default:
      return [
        dataChunk("notification", {
          level: event.kind.includes("failed") ? "error" : "info",
          message: asText(event.payload.message || event.payload.summary || event.kind),
        }),
      ];
  }
}
