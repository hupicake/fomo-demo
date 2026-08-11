import {
  agentFrameworkIds,
  toProjectStatus,
  toRunStatus,
  userInputRequestStages,
  userInputRequestStatuses,
} from "@/lib/contracts";
import type {
  AgentFrameworkId,
  AgentFrameworkOption,
  DomainEvent,
  FileContent,
  FileManifestEntry,
  PreviewRef,
  ProjectMessage,
  ProjectSnapshot,
  ProjectSummary,
  RecoveryMode,
  RunRuntimeResponse,
  RunSnapshot,
  RunUsage,
  RuntimeOptionsResponse,
  RuntimeProfileOption,
  UserInputAnswerInput,
  UserInputAnswerResponse,
  UserInputRequest,
  VersionSummary,
} from "@/lib/contracts";
import { normalizeGoalGraph } from "@/lib/goal-graph";

const defaultApiOrigin = "http://localhost:8000";
type JsonRecord = Record<string, unknown>;

export class ApiProblem extends Error {
  readonly detail?: string;
  readonly status: number;
  readonly title: string;
  readonly type?: string;

  constructor({
    detail,
    status,
    title,
    type,
  }: {
    detail?: string;
    status: number;
    title: string;
    type?: string;
  }) {
    super(detail || title);
    this.name = "ApiProblem";
    this.detail = detail;
    this.status = status;
    this.title = title;
    this.type = type;
  }
}

export function normalizeApiBase(value?: string): string {
  const origin = (value?.trim() || defaultApiOrigin).replace(/\/+$/, "");
  return origin.endsWith("/v1") ? origin : `${origin}/v1`;
}

export function getApiBase(): string {
  return normalizeApiBase(process.env.NEXT_PUBLIC_API_URL);
}

export function controlPlaneUrl(path: string): string {
  return `${getApiBase()}${path.startsWith("/") ? path : `/${path}`}`;
}

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonRecord) : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nullableNumberValue(value: unknown): number | null {
  return value === null ? null : numberValue(value);
}

function nonnegativeSafeInteger(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : undefined;
}

function toArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function boundedChoices(value: unknown): string[] | undefined {
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

export function normalizeProject(value: unknown): ProjectSummary {
  const source = record(value);
  const latestRunSource = record(source.latestRun || source.latest_run);
  const latestRunId = text(latestRunSource.id);
  const latestAgentFramework = normalizeAgentFrameworkId(
    latestRunSource.agentFramework || latestRunSource.agent_framework,
  );
  const latestRecoveryMode = normalizeRecoveryMode(
    latestRunSource.recoveryMode || latestRunSource.recovery_mode,
  );
  const latestUsage = normalizeRunUsage(latestRunSource.usage);
  return {
    id: text(source.id || source.projectId),
    name: text(source.name || source.title, "Untitled project"),
    activeRunId: text(source.activeRunId || source.active_run_id) || undefined,
    headVersionId: text(source.headVersionId || source.head_version_id) || undefined,
    createdAt: text(source.createdAt || source.created_at) || undefined,
    updatedAt: text(source.updatedAt || source.updated_at) || undefined,
    status: toProjectStatus(source.status),
    ...(latestRunId && latestAgentFramework ? {
      latestRun: {
        id: latestRunId,
        status: toRunStatus(latestRunSource.status),
        errorCode: text(latestRunSource.errorCode || latestRunSource.error_code).slice(0, 80) || undefined,
        agentFramework: latestAgentFramework,
        profileId: text(latestRunSource.profileId || latestRunSource.profile_id),
        thinking: text(latestRunSource.thinking),
        recoveryAvailable: Boolean(
          latestRunSource.recoveryAvailable ?? latestRunSource.recovery_available,
        ),
        ...(latestRecoveryMode ? { recoveryMode: latestRecoveryMode } : {}),
        sourceCheckpointAvailable: Boolean(
          latestRunSource.sourceCheckpointAvailable
            ?? latestRunSource.source_checkpoint_available,
        ),
        ...(latestUsage ? { usage: latestUsage } : {}),
      },
    } : {}),
  };
}

function normalizeRecoveryMode(value: unknown): RecoveryMode | undefined {
  const mode = text(value);
  return ["verified_checkpoint", "verified_version", "base_restart"].includes(mode)
    ? mode as RecoveryMode
    : undefined;
}

/** Explicitly selects the public clarification fields; never spread input. */
export function normalizeUserInputRequest(value: unknown): UserInputRequest | undefined {
  const source = record(value);
  const id = text(source.id || source.requestId || source.request_id);
  const runId = text(source.runId || source.run_id);
  const question = text(source.question).trim();
  const status = text(source.status);
  const stage = text(source.stage);
  const allowFreeformValue = source.allowFreeform ?? source.allow_freeform;
  const choices = boundedChoices(source.choices);
  if (
    !id
    || !runId
    || !question
    || question.length > 2_000
    || choices === undefined
    || typeof allowFreeformValue !== "boolean"
    || !userInputRequestStatuses.includes(status as UserInputRequest["status"])
    || !userInputRequestStages.includes(stage as UserInputRequest["stage"])
    || (status === "pending" && !allowFreeformValue && choices.length === 0)
  ) {
    return undefined;
  }
  return {
    id,
    runId,
    question,
    choices,
    allowFreeform: allowFreeformValue,
    status: status as UserInputRequest["status"],
    stage: stage as UserInputRequest["stage"],
    goalId: text(source.goalId || source.goal_id) || undefined,
    createdAt: text(source.createdAt || source.created_at) || undefined,
    answeredAt: text(source.answeredAt || source.answered_at) || undefined,
  };
}

export function normalizeRunRuntime(value: unknown): RunRuntimeResponse | undefined {
  const source = record(value);
  const profileId = text(source.profileId || source.profile_id);
  const thinking = text(source.thinking);
  if (!profileId || !thinking) {
    return undefined;
  }
  return {
    profileId,
    thinking,
    contextWindow: numberValue(source.contextWindow ?? source.context_window),
    policyVersion: text(source.policyVersion || source.policy_version, "unknown"),
    runTokenBudget: nullableNumberValue(
      source.runTokenBudget !== undefined ? source.runTokenBudget : source.run_token_budget,
    ),
    runTokenBudgetUnlimited: Boolean(
      source.runTokenBudgetUnlimited ?? source.run_token_budget_unlimited,
    ),
    inferenceTpmLimit: numberValue(source.inferenceTpmLimit ?? source.inference_tpm_limit),
  };
}

/** Rejects partial, negative or internally inconsistent usage projections. */
export function normalizeRunUsage(value: unknown): RunUsage | undefined {
  if (value === undefined || value === null) return undefined;
  const source = record(value);
  const inputTokens = nonnegativeSafeInteger(source.inputTokens ?? source.input_tokens);
  const outputTokens = nonnegativeSafeInteger(source.outputTokens ?? source.output_tokens);
  const cacheReadTokens = nonnegativeSafeInteger(
    source.cacheReadTokens ?? source.cache_read_tokens,
  );
  const cacheWriteTokens = nonnegativeSafeInteger(
    source.cacheWriteTokens ?? source.cache_write_tokens,
  );
  const totalTokens = nonnegativeSafeInteger(source.totalTokens ?? source.total_tokens);
  const toolCalls = nonnegativeSafeInteger(source.toolCalls ?? source.tool_calls);
  if (
    inputTokens === undefined
    || outputTokens === undefined
    || cacheReadTokens === undefined
    || cacheWriteTokens === undefined
    || totalTokens === undefined
    || toolCalls === undefined
    || totalTokens !== inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens
  ) {
    return undefined;
  }
  return {
    inputTokens,
    outputTokens,
    cacheReadTokens,
    cacheWriteTokens,
    totalTokens,
    toolCalls,
  };
}

/** Mirrors the public RuntimeProfileOption contract; drops nothing server-sent. */
export function normalizeRuntimeProfile(value: unknown): RuntimeProfileOption | undefined {
  const source = record(value);
  const profileId = text(source.profileId || source.profile_id);
  const label = text(source.label);
  const rawLevels = source.thinkingLevels ?? source.thinking_levels;
  const thinkingLevels = Array.isArray(rawLevels)
    ? rawLevels.map((item) => text(item)).filter(Boolean)
    : [];
  if (!profileId || !label || thinkingLevels.length === 0) {
    return undefined;
  }
  const available = Boolean(source.available ?? source.available);
  const disabledReason = text(source.disabledReason || source.disabled_reason) || undefined;
  return {
    profileId,
    label,
    thinkingLevels,
    defaultThinking: text(source.defaultThinking ?? source.default_thinking, thinkingLevels[0] || "high"),
    contextWindow: numberValue(source.contextWindow ?? source.context_window),
    runTokenBudget: nullableNumberValue(
      source.runTokenBudget !== undefined ? source.runTokenBudget : source.run_token_budget,
    ),
    runTokenBudgetUnlimited: Boolean(
      source.runTokenBudgetUnlimited ?? source.run_token_budget_unlimited,
    ),
    inferenceTpmLimit: numberValue(source.inferenceTpmLimit ?? source.inference_tpm_limit),
    available,
    ...(disabledReason ? { disabledReason } : {}),
  };
}

export function normalizeRun(value: unknown): RunSnapshot | undefined {
  const source = record(value);
  const id = text(source.id || source.runId);
  if (!id) {
    return undefined;
  }
  const pendingInputRequest = normalizeUserInputRequest(source.pendingInputRequest || source.pending_input_request);
  const runtime = normalizeRunRuntime(source.runtime);
  const agentFramework = normalizeAgentFrameworkId(
    source.agentFramework || source.agent_framework,
  );
  const recoveryMode = normalizeRecoveryMode(
    source.recoveryMode || source.recovery_mode,
  );
  const usage = normalizeRunUsage(source.usage);
  return {
    id,
    projectId: text(source.projectId || source.project_id),
    status: toRunStatus(source.status),
    phase: text(source.phase) || undefined,
    errorCode: text(source.errorCode || source.error_code).slice(0, 80) || undefined,
    lastSeq: numberValue(source.lastSeq ?? source.last_seq),
    createdAt: text(source.createdAt || source.created_at) || undefined,
    updatedAt: text(source.updatedAt || source.updated_at) || undefined,
    ...(pendingInputRequest ? { pendingInputRequest } : {}),
    ...(agentFramework ? { agentFramework } : {}),
    ...(runtime ? { runtime } : {}),
    recoveredFromRunId: text(source.recoveredFromRunId || source.recovered_from_run_id) || undefined,
    recoveredFromGoalId: text(source.recoveredFromGoalId || source.recovered_from_goal_id) || undefined,
    recoveredFromCheckpointId: text(
      source.recoveredFromCheckpointId || source.recovered_from_checkpoint_id,
    ) || undefined,
    ...(recoveryMode ? { recoveryMode } : {}),
    recoveryAvailable: Boolean(source.recoveryAvailable ?? source.recovery_available),
    sourceCheckpointAvailable: Boolean(
      source.sourceCheckpointAvailable ?? source.source_checkpoint_available,
    ),
    ...(usage ? { usage } : {}),
  };
}

function normalizeAgentFrameworkId(value: unknown): AgentFrameworkId | undefined {
  const id = text(value);
  return agentFrameworkIds.includes(id as AgentFrameworkId)
    ? id as AgentFrameworkId
    : undefined;
}

function normalizeAgentFramework(value: unknown): AgentFrameworkOption | undefined {
  const source = record(value);
  const id = normalizeAgentFrameworkId(source.id || source.agentFramework || source.agent_framework);
  const label = text(source.label);
  if (!id || !label) return undefined;
  const disabledReason = text(source.disabledReason || source.disabled_reason) || undefined;
  const compatibleProfileIds = toArray(
    source.compatibleProfileIds ?? source.compatible_profile_ids,
  ).flatMap((item) => {
    const value = text(item);
    return value ? [value] : [];
  });
  const rawThinkingLevels = source.compatibleThinkingLevels ?? source.compatible_thinking_levels;
  const compatibleThinkingLevels = rawThinkingLevels == null
    ? null
    : toArray(rawThinkingLevels).flatMap((item) => {
        const value = text(item);
        return value ? [value] : [];
      });
  return {
    id,
    label,
    compatibleProfileIds,
    compatibleThinkingLevels,
    available: Boolean(source.available),
    ...(disabledReason ? { disabledReason } : {}),
  };
}

export function normalizeEvent(value: unknown): DomainEvent | undefined {
  const source = record(value);
  const eventId = text(source.eventId || source.event_id || source.id);
  const runId = text(source.runId || source.run_id);
  const projectId = text(source.projectId || source.project_id);
  const kind = text(source.kind || source.event);
  if (!(eventId && runId && projectId && kind)) {
    return undefined;
  }
  return {
    schemaVersion: numberValue(source.schemaVersion ?? source.schema_version, 1),
    eventId,
    seq: numberValue(source.seq),
    projectId,
    runId,
    kind,
    role: text(source.role) as DomainEvent["role"],
    occurredAt: text(source.occurredAt || source.occurred_at, new Date().toISOString()),
    payload: record(source.payload),
  };
}

function normalizeMessage(value: unknown): ProjectMessage | undefined {
  const source = record(value);
  const id = text(source.id || source.messageId || source.message_id);
  const role = text(source.role);
  const content = text(source.content || source.text);
  if (!(id && (role === "user" || role === "assistant"))) {
    return undefined;
  }
  return {
    id,
    role,
    content,
    createdAt: text(source.createdAt || source.created_at) || undefined,
  };
}

function normalizeFile(value: unknown): FileManifestEntry | undefined {
  const source = record(value);
  const path = text(source.path || source.name);
  return path
    ? {
        path,
        hash: text(source.hash || source.sha256 || source.contentHash || source.content_hash) || undefined,
        language: text(source.language) || undefined,
        size: numberValue(source.size) || undefined,
        binary: Boolean(source.binary),
      }
    : undefined;
}

function normalizeVersion(value: unknown): VersionSummary | undefined {
  const source = record(value);
  const id = text(source.id || source.versionId || source.version_id);
  if (!id) {
    return undefined;
  }
  const qaStatus = text(source.status || source.qaStatus || source.qa_status);
  return {
    id,
    hash: text(source.hash || source.commitHash || source.commitSha || source.commit_hash || source.commit_sha) || undefined,
    message: text(source.message || source.title, source.number ? `Version ${source.number}` : "Generated version"),
    createdAt: text(source.createdAt || source.created_at) || undefined,
    status: qaStatus === "passed" || qaStatus === "ready" || qaStatus === "manual" || qaStatus === "restored"
      ? "ready"
      : qaStatus === "failed"
        ? "failed"
        : qaStatus ? "building" : undefined,
    files: toArray(source.files).flatMap((entry) => {
      const file = record(entry);
      const path = text(file.path);
      return path
        ? [{
            path,
            additions: numberValue(file.additions) || undefined,
            deletions: numberValue(file.deletions) || undefined,
            status: text(file.status) || undefined,
          }]
        : [];
    }),
  };
}

function normalizePreview(value: unknown): PreviewRef | undefined {
  const source = record(value);
  const status = text(source.status);
  if (!status && !source.url) {
    return undefined;
  }
  const verificationStatus = text(source.verificationStatus || source.verification_status);
  const normalizedVerificationStatus = verificationStatus === "verified"
    ? "verified"
    : verificationStatus === "unverified"
      ? "unverified"
      : undefined;
  return {
    status: (status || "pending") as PreviewRef["status"],
    url: text(source.url) || undefined,
    runId: text(source.runId || source.run_id) || undefined,
    error: text(source.error || source.detail) || undefined,
    ...(normalizedVerificationStatus ? { verificationStatus: normalizedVerificationStatus } : {}),
  };
}

async function responseProblem(response: Response): Promise<ApiProblem> {
  let body: JsonRecord = {};
  try {
    body = record(await response.json());
  } catch {
    // RFC 9457 may be absent when a reverse proxy fails before the API.
  }
  return new ApiProblem({
    detail: text(body.detail) || undefined,
    status: response.status,
    title: text(body.title, response.statusText || "Request failed"),
    type: text(body.type) || undefined,
  });
}

async function executeRequest(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(controlPlaneUrl(path), {
    ...init,
    credentials: "include",
    headers,
  });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await executeRequest(path, init);
  if (!response.ok) {
    throw await responseProblem(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function idempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() || `fomo-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export const controlPlane = {
  async getProjects(): Promise<ProjectSummary[]> {
    const response = await request<unknown>("/projects");
    const source = record(response);
    const items = Array.isArray(response) ? response : toArray(source.items || source.projects || source.data);
    return items.map(normalizeProject).filter((project) => project.id);
  },

  async createProject(input: { title: string }): Promise<ProjectSummary> {
    const response = await request<unknown>("/projects", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(input),
    });
    return normalizeProject(record(response).project || response);
  },

  async getProject(projectId: string): Promise<ProjectSnapshot> {
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}`);
    const source = record(response);
    const project = normalizeProject(source.project || source);
    const events = toArray(source.events).flatMap((event) => {
      const normalized = normalizeEvent(event);
      return normalized ? [normalized] : [];
    });
    const messages = toArray(source.messages).flatMap((message) => {
      const normalized = normalizeMessage(message);
      return normalized ? [normalized] : [];
    });
    const runs = toArray(source.runs).flatMap((run) => {
      const normalized = normalizeRun(run);
      return normalized ? [normalized] : [];
    });
    const directActiveRun = normalizeRun(source.activeRun || source.active_run || source.run);
    const activeRun = (project.activeRunId ? runs.find((run) => run.id === project.activeRunId) : undefined)
      || directActiveRun
      || runs[0];
    const pendingInputRequest = normalizeUserInputRequest(
      source.pendingInputRequest || source.pending_input_request || activeRun?.pendingInputRequest,
    );
    return {
      project: { ...project, id: project.id || projectId },
      messages,
      activeRun,
      runs,
      lastSeq: numberValue(source.lastSeq ?? source.last_seq, activeRun?.lastSeq || 0),
      events,
      files: toArray(source.files).flatMap((item) => {
        const normalized = normalizeFile(item);
        return normalized ? [normalized] : [];
      }),
      versions: toArray(source.versions).flatMap((item) => {
        const normalized = normalizeVersion(item);
        return normalized ? [normalized] : [];
      }),
      preview: normalizePreview(source.preview),
      ...(pendingInputRequest ? { pendingInputRequest } : {}),
      goalGraph: normalizeGoalGraph(source.goalGraph || source.goal_graph),
    };
  },

  async startRun(
    projectId: string,
    input: { clientMessageId: string; content: string; baseVersionId?: string; agentFramework?: AgentFrameworkId; profileId?: string; thinking?: string },
  ): Promise<{ runId: string; agentFramework?: AgentFrameworkId; runtime?: RunRuntimeResponse }> {
    // A message always carries attachments: []. The runtime selection is sent
    // only when the caller chose one; when omitted the server applies its own
    // default and the UI still renders the resolved contract from the response.
    const body: JsonRecord = { ...input, attachments: [] };
    if (!input.profileId) delete body.profileId;
    if (input.thinking === undefined) delete body.thinking;
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}/messages`, {
      method: "POST",
      headers: { "Idempotency-Key": input.clientMessageId },
      body: JSON.stringify(body),
    });
    const source = record(response);
    const runId = text(source.runId || source.run_id || record(source.run).id);
    if (!runId) {
      throw new ApiProblem({ status: 502, title: "Control plane did not return a run ID" });
    }
    const runtime = normalizeRunRuntime(record(source.run).runtime);
    const agentFramework = normalizeAgentFrameworkId(
      record(source.run).agentFramework
        || record(source.run).agent_framework
        || source.agentFramework
        || source.agent_framework,
    );
    return {
      runId,
      ...(agentFramework ? { agentFramework } : {}),
      ...(runtime ? { runtime } : {}),
    };
  },

  async recoverRun(
    runId: string,
    input: {
      clientMessageId: string;
      content: string;
      agentFramework?: AgentFrameworkId;
      profileId?: string;
      thinking?: string;
    },
  ): Promise<{
    runId: string;
    recoveryMode?: RecoveryMode;
    sourceCheckpointAvailable: boolean;
    agentFramework?: AgentFrameworkId;
    runtime?: RunRuntimeResponse;
  }> {
    const body: JsonRecord = { ...input, attachments: [] };
    if (!input.profileId) delete body.profileId;
    if (input.thinking === undefined) delete body.thinking;
    const response = await request<unknown>(`/runs/${encodeURIComponent(runId)}/recover`, {
      method: "POST",
      headers: { "Idempotency-Key": input.clientMessageId },
      body: JSON.stringify(body),
    });
    const source = record(response);
    const recoveredRun = record(source.run);
    const recoveredRunId = text(recoveredRun.id || source.runId || source.run_id);
    if (!recoveredRunId) {
      throw new ApiProblem({ status: 502, title: "Control plane did not return a recovery run ID" });
    }
    const runtime = normalizeRunRuntime(recoveredRun.runtime);
    const agentFramework = normalizeAgentFrameworkId(
      recoveredRun.agentFramework || recoveredRun.agent_framework,
    );
    const recoveryMode = normalizeRecoveryMode(
      source.recoveryMode || source.recovery_mode || recoveredRun.recoveryMode || recoveredRun.recovery_mode,
    );
    return {
      runId: recoveredRunId,
      ...(recoveryMode ? { recoveryMode } : {}),
      sourceCheckpointAvailable: Boolean(
        source.sourceCheckpointAvailable
          ?? source.source_checkpoint_available
          ?? recoveredRun.sourceCheckpointAvailable
          ?? recoveredRun.source_checkpoint_available,
      ),
      ...(agentFramework ? { agentFramework } : {}),
      ...(runtime ? { runtime } : {}),
    };
  },

  async getRuntimeOptions(): Promise<RuntimeOptionsResponse> {
    const response = await request<unknown>("/runtime/options");
    const source = record(response);
    const defaultAgentFramework = normalizeAgentFrameworkId(
      source.defaultAgentFramework || source.default_agent_framework,
    ) || null;
    const agentFrameworks = toArray(
      source.agentFrameworks || source.agent_frameworks,
    ).flatMap((item) => {
      const framework = normalizeAgentFramework(item);
      return framework ? [framework] : [];
    });
    const defaultProfileId = text(source.defaultProfileId || source.default_profile_id) || null;
    const profiles = toArray(source.profiles || source.options).flatMap((item) => {
      const profile = normalizeRuntimeProfile(item);
      return profile ? [profile] : [];
    });
    return {
      defaultAgentFramework,
      agentFrameworks,
      defaultProfileId: defaultProfileId || null,
      profiles,
    };
  },

  async answerRunInputRequest(
    runId: string,
    requestId: string,
    input: UserInputAnswerInput,
  ): Promise<UserInputAnswerResponse> {
    const response = await request<unknown>(
      `/runs/${encodeURIComponent(runId)}/input-requests/${encodeURIComponent(requestId)}/answer`,
      {
        method: "POST",
        headers: { "Idempotency-Key": input.clientMessageId },
        body: JSON.stringify(input),
      },
    );
    const source = record(response);
    const message = normalizeMessage(source.message);
    const answeredRequest = normalizeUserInputRequest(source.request);
    const run = normalizeRun(source.run);
    if (
      !message
      || message.role !== "user"
      || !answeredRequest
      || answeredRequest.id !== requestId
      || answeredRequest.runId !== runId
      || answeredRequest.status !== "answered"
      || !run
      || run.id !== runId
    ) {
      throw new ApiProblem({ status: 502, title: "Control plane returned an invalid clarification response" });
    }
    return { message, request: answeredRequest, run };
  },

  async cancelRun(runId: string): Promise<void> {
    await request(`/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
    });
  },

  async getFiles(projectId: string, versionId?: string): Promise<FileManifestEntry[]> {
    const query = versionId ? `?versionId=${encodeURIComponent(versionId)}` : "";
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}/files${query}`);
    const source = record(response);
    const files = Array.isArray(response) ? response : toArray(source.files || source.items || source.data);
    return files.flatMap((item) => {
      const normalized = normalizeFile(item);
      return normalized ? [normalized] : [];
    });
  },

  async getFileContent(projectId: string, path: string, versionId?: string): Promise<FileContent> {
    const query = new URLSearchParams({ path });
    if (versionId) query.set("versionId", versionId);
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}/files/content?${query}`);
    const source = record(response);
    return {
      ...normalizeFile(source),
      path: text(source.path, path),
      content: text(source.content),
    };
  },

  async saveFile(
    projectId: string,
    input: { path: string; content: string; baseVersionId?: string; hash?: string },
  ): Promise<FileContent> {
    const query = new URLSearchParams({ path: input.path });
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}/files/content?${query}`, {
      method: "PUT",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify({
        content: input.content,
        baseVersionId: input.baseVersionId,
        baseSha256: input.hash,
      }),
    });
    const source = record(response);
    return {
      ...normalizeFile(source),
      path: text(source.path, input.path),
      content: text(source.content, input.content),
    };
  },

  async getVersions(projectId: string): Promise<VersionSummary[]> {
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}/versions`);
    const source = record(response);
    const versions = Array.isArray(response) ? response : toArray(source.versions || source.items || source.data);
    return versions.flatMap((item) => {
      const normalized = normalizeVersion(item);
      return normalized ? [normalized] : [];
    });
  },

  async getPreview(projectId: string): Promise<PreviewRef | undefined> {
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}/preview`);
    return normalizePreview(record(response).preview || response);
  },

  async restoreVersion(projectId: string, versionId: string): Promise<VersionSummary | undefined> {
    const response = await request<unknown>(
      `/projects/${encodeURIComponent(projectId)}/versions/${encodeURIComponent(versionId)}/restore`,
      { method: "POST", headers: { "Idempotency-Key": idempotencyKey() } },
    );
    return normalizeVersion(record(response).version || response);
  },
};

export type AuthUser = {
  id: string;
  email: string;
  displayName?: string;
  createdAt: string;
};

export type AuthSession = {
  expiresAt: string;
  user: AuthUser;
};

export type RegisterInput = {
  email: string;
  password: string;
  displayName?: string;
};

export type LoginInput = {
  email: string;
  password: string;
};

function toAuthUser(value: unknown): AuthUser {
  const source = record(value);
  return {
    id: text(source.id),
    email: text(source.email),
    displayName: text(source.displayName) || undefined,
    createdAt: text(source.createdAt),
  };
}

/** Register/login return a nested `user` object and a public expiry hint. */
function toAuthSession(value: unknown): AuthSession {
  const source = record(value);
  return {
    expiresAt: text(source.expiresAt),
    user: toAuthUser(source.user),
  };
}

/**
 * Auth endpoints deliberately bypass the generic `request` wrapper.
 * `auth/me` returns null on 401; `/auth/login` returns 401 for bad credentials;
 * register may return 409 for a duplicate email. The browser relies solely on
 * the HttpOnly `fomo_session` cookie the server sets and clears.
 */
export const auth = {
  async register(input: RegisterInput): Promise<AuthSession> {
    const body: JsonRecord = { email: input.email, password: input.password };
    if (input.displayName) body.displayName = input.displayName;
    const response = await executeRequest("/auth/register", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!response.ok) throw await responseProblem(response);
    return toAuthSession(record(await response.json()));
  },

  async login(input: LoginInput): Promise<AuthSession> {
    const response = await executeRequest("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email: input.email, password: input.password }),
    });
    if (!response.ok) throw await responseProblem(response);
    return toAuthSession(record(await response.json()));
  },

  /** The signed-in user, or null for an expired or logged-out session. */
  async me(): Promise<AuthUser | null> {
    const response = await executeRequest("/auth/me", { method: "GET" });
    if (response.status === 401) return null;
    if (!response.ok) throw await responseProblem(response);
    return toAuthUser(record(await response.json()));
  },

  async logout(): Promise<void> {
    const response = await executeRequest("/auth/logout", { method: "POST" });
    if (!response.ok) throw await responseProblem(response);
    // 204: the server clears the `fomo_session` cookie. Nothing to read.
  },
};
