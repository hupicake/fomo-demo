import type { UIMessage } from "ai";

export const agentRoles = [
  "product_manager",
  "architect",
  "engineer",
  "reviewer",
] as const;

export type AgentRole = (typeof agentRoles)[number];
export const agentStages = ["planning", "building", "verifying", "repairing"] as const;
export type AgentStage = (typeof agentStages)[number];
export type RunStatus =
  | "queued"
  | "running"
  | "waiting_for_user"
  | "needs_attention"
  | "completed"
  | "failed"
  | "cancelled";

const runStatusAliases: Record<string, RunStatus> = {
  cancelled: "cancelled",
  completed: "completed",
  failed: "failed",
  needs_attention: "needs_attention",
  queued: "queued",
  running: "running",
  succeeded: "completed",
  waiting_for_user: "waiting_for_user",
};

/** Maps control-plane vocabulary into the UI's stable, user-facing statuses. */
export function toRunStatus(value: unknown, fallback: RunStatus = "queued"): RunStatus {
  return typeof value === "string" ? (runStatusAliases[value] || fallback) : fallback;
}

/** Project lifecycle is distinct from the latest run lifecycle. */
export type ProjectStatus = "idle" | RunStatus;

export function toProjectStatus(value: unknown): ProjectStatus | undefined {
  if (typeof value !== "string") return undefined;
  return value === "idle" ? "idle" : runStatusAliases[value];
}

export type RoleStatus = "idle" | "queued" | "working" | "completed" | "failed";

export interface ProjectSummary {
  id: string;
  name: string;
  activeRunId?: string;
  headVersionId?: string;
  createdAt?: string;
  updatedAt?: string;
  status?: ProjectStatus;
  latestRun?: ProjectLatestRun;
}

export type RecoveryMode = "verified_checkpoint" | "verified_version" | "base_restart";

export interface ProjectLatestRun {
  id: string;
  status: RunStatus;
  errorCode?: string;
  agentFramework: AgentFrameworkId;
  profileId: string;
  thinking: string;
  recoveryAvailable: boolean;
  recoveryMode?: RecoveryMode;
  sourceCheckpointAvailable: boolean;
  usage?: RunUsage;
}

export interface ProjectMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt?: string;
}

export const userInputRequestStatuses = ["pending", "answered", "cancelled", "expired"] as const;
export type UserInputRequestStatus = (typeof userInputRequestStatuses)[number];
export const userInputRequestStages = ["planning", "building", "repairing"] as const;
export type UserInputRequestStage = (typeof userInputRequestStages)[number];

/**
 * Deliberately small public projection of a clarification request. Private
 * agent continuation data (including reasons, sessions and sandbox/context
 * state) is never part of the browser contract.
 */
export interface UserInputRequest {
  id: string;
  runId: string;
  question: string;
  choices: string[];
  allowFreeform: boolean;
  status: UserInputRequestStatus;
  stage: UserInputRequestStage;
  goalId?: string;
  createdAt?: string;
  answeredAt?: string;
  /** Client-only ordering cursor copied from run.input_requested. */
  requestedSeq?: number;
  /** Client-only ordering cursor copied from the resolution event. */
  resolvedSeq?: number;
  /** Public message reference returned by the answer endpoint. */
  answerMessageId?: string;
}

export interface UserInputAnswerInput {
  clientMessageId: string;
  answer: string;
}

export interface UserInputAnswerResponse {
  message: ProjectMessage;
  request: UserInputRequest;
  run: RunSnapshot;
}

/**
 * A single model the user may pick for a run. Provider routing, LiteLLM
 * aliases and context limits stay server-owned; the browser
 * only sees a public label, the thinking levels that model supports, and why
 * an otherwise-valid profile might be unavailable right now.
 */
export interface RuntimeProfileOption {
  profileId: string;
  label: string;
  thinkingLevels: string[];
  defaultThinking: string;
  contextWindow: number;
  runTokenBudget: number | null;
  runTokenBudgetUnlimited: boolean;
  inferenceTpmLimit: number;
  available: boolean;
  disabledReason?: string | null;
}

export const agentFrameworkIds = ["pi", "opencode", "codex"] as const;
export type AgentFrameworkId = (typeof agentFrameworkIds)[number];

export interface AgentFrameworkOption {
  id: AgentFrameworkId;
  label: string;
  compatibleProfileIds: string[];
  compatibleThinkingLevels: string[] | null;
  compatibleThinkingLevelsByProfile: Record<string, string[]>;
  available: boolean;
  disabledReason?: string | null;
}

export interface RuntimeOptionsResponse {
  defaultAgentFramework: AgentFrameworkId | null;
  agentFrameworks: AgentFrameworkOption[];
  defaultProfileId: string | null;
  profiles: RuntimeProfileOption[];
}

/**
 * The immutable per-run inference contract resolved by the server. After a run
 * is created the UI shows *only* these fields; it never lets the user edit
 * provider routing or context limits.
 */
export interface RunRuntimeResponse {
  profileId: string;
  thinking: string;
  contextWindow: number;
  policyVersion: string;
  runTokenBudget: number | null;
  runTokenBudgetUnlimited: boolean;
  inferenceTpmLimit: number;
}

/** Final, durable usage aggregated across every model turn in one terminal run. */
export interface RunUsage {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  totalTokens: number;
  toolCalls: number;
}

export interface RunSnapshot {
  id: string;
  projectId: string;
  status: RunStatus;
  phase?: string;
  /** Server-owned terminal category; never an exception or provider body. */
  errorCode?: string;
  lastSeq: number;
  createdAt?: string;
  updatedAt?: string;
  pendingInputRequest?: UserInputRequest;
  agentFramework?: AgentFrameworkId;
  runtime?: RunRuntimeResponse;
  recoveredFromRunId?: string;
  recoveredFromGoalId?: string;
  recoveredFromCheckpointId?: string;
  recoveryMode?: RecoveryMode;
  recoveryAvailable?: boolean;
  sourceCheckpointAvailable?: boolean;
  usage?: RunUsage;
}

export interface FileManifestEntry {
  path: string;
  hash?: string;
  language?: string;
  size?: number;
  binary?: boolean;
}

export interface FileContent extends FileManifestEntry {
  content: string;
}

export interface VersionSummary {
  id: string;
  hash?: string;
  message: string;
  createdAt?: string;
  status?: "ready" | "failed" | "building";
  files?: Array<{ path: string; additions?: number; deletions?: number; status?: string }>;
}

export interface PreviewRef {
  status: "ready" | "reconnecting" | "failed" | "pending" | "expired" | "unavailable";
  url?: string;
  runId?: string;
  error?: string;
  verificationStatus?: "unverified" | "verified";
}

export interface TraceEvidence {
  id: string;
  type: "design" | "file" | "test" | "screenshot" | "version" | "command";
  label: string;
  href?: string;
  status?: "passed" | "pending" | "failed";
}

export interface AcceptanceTrace {
  id: string;
  title: string;
  priority: "must" | "should" | "could";
  /** Derived only from the latest deterministic playwright_smoke evidence. */
  status: "unverified" | "pending" | "passed" | "failed" | "blocked";
  /**
   * "implemented" requires an explicit server projection or a real
   * implemented_in business-file link. Undefined means the trace is unlinked;
   * absence of a link is not proof that implementation is absent.
   */
  implementationStatus?: "implemented" | "not_implemented";
  evidence: TraceEvidence[];
}

export const artifactKinds = [
  "run_input",
  "build_plan",
  "acceptance_contract",
  "diagnostic_report",
  "product_spec",
  "technical_spec",
] as const;
export type ArtifactKind = (typeof artifactKinds)[number];

export interface ArtifactRef {
  id: string;
  runId?: string;
  kind: string;
  role?: string;
  stage?: string;
  schemaVersion?: number;
  title?: string;
  summary?: string;
  createdAt?: string;
}

export interface RoleActivity {
  role: AgentRole;
  status: RoleStatus;
  title: string;
  detail?: string;
  updatedAt?: string;
}

export interface StageActivity {
  stage: AgentStage;
  status: RoleStatus;
  title: string;
  detail?: string;
  updatedAt?: string;
}

export interface FileChange {
  id: string;
  path: string;
  status: "added" | "modified" | "deleted" | "renamed";
  additions?: number;
  deletions?: number;
}

export interface CommandLog {
  id: string;
  command: string;
  output: string;
  status: "running" | "completed" | "failed";
  exitCode?: number;
}

export interface VerificationResult {
  id: string;
  name: string;
  status: "passed" | "failed" | "running" | "skipped";
  duration?: number;
  detail?: string;
  stack?: string;
}

export interface Problem {
  id: string;
  title: string;
  severity: "error" | "major" | "minor";
  file?: string;
  line?: number;
  stack?: string;
}

export type AgentWorklogKind = "progress" | "tool" | "file" | "verification" | "goal" | "system";
export type AgentWorklogStatus = "running" | "completed" | "failed" | "info";

/**
 * A deliberately public, bounded projection of agent activity. It contains
 * model-authored progress messages and safe action summaries, never private
 * chain-of-thought or raw tool arguments.
 */
export interface AgentWorklogItem {
  id: string;
  kind: AgentWorklogKind;
  status: AgentWorklogStatus;
  title: string;
  detail?: string;
  stage?: AgentStage;
  occurredAt: string;
  seq: number;
}

/** Latest real context-usage snapshot emitted at a Coding Agent turn boundary. */
export interface ContextUsageSnapshot {
  contextTokens?: number;
  contextWindow?: number;
  boundary: "turn_started" | "turn_completed";
  capturedAt: string;
}

export interface DomainEvent {
  schemaVersion: number;
  eventId: string;
  seq: number;
  projectId: string;
  runId: string;
  kind: string;
  role?: string;
  occurredAt: string;
  payload: Record<string, unknown>;
}

export type GoalGraphStatus = "active" | "verified" | "completed" | "failed" | "cancelled" | "superseded";
export type GoalStatus = "pending" | "active" | "claimed" | "verified" | "failed" | "superseded";
export type GoalAcceptanceStatus = "pending" | "passed" | "failed" | "blocked" | "unverified";
export type GoalAcceptancePriority = "must" | "should" | "could";

export interface GoalAcceptanceProjection {
  acceptanceId: string;
  title: string;
  priority: GoalAcceptancePriority;
  status: GoalAcceptanceStatus;
}

export interface GoalProjection {
  goalId: string;
  title: string;
  userVisible: boolean;
  dependsOn: string[];
  status: GoalStatus;
  checkpointId?: string;
  claimedAt?: string;
  verifiedAt?: string;
  acceptance: GoalAcceptanceProjection[];
  evidenceCount: number;
}

export type GoalNavigationMode = "single_surface" | "multi_route";

export interface GoalRouteProjection {
  path: string;
  title: string;
  owningGoalId: string;
  deepLinkable: boolean;
}

/** Read-only server projection. Lifecycle and acceptance states are never inferred by the UI. */
export interface GoalGraphProjection {
  graphId: string;
  runId: string;
  revision: number;
  schemaVersion: 1 | 2;
  navigationMode: GoalNavigationMode;
  routes: GoalRouteProjection[];
  status: GoalGraphStatus;
  productOutcome: string;
  activeGoalId: string | null;
  goals: GoalProjection[];
}

export interface ProjectSnapshot {
  project: ProjectSummary;
  messages: ProjectMessage[];
  activeRun?: RunSnapshot;
  runs?: RunSnapshot[];
  lastSeq: number;
  events: DomainEvent[];
  files?: FileManifestEntry[];
  versions?: VersionSummary[];
  preview?: PreviewRef;
  pendingInputRequest?: UserInputRequest;
  /** Null for legacy P0 runs and responses that predate GoalGraph. */
  goalGraph: GoalGraphProjection | null;
}

export interface RunPresentation {
  runId: string;
  projectId: string;
  status: RunStatus;
  lastSeq: number;
  stages: Record<AgentStage, StageActivity>;
  commands: CommandLog[];
  verifications: VerificationResult[];
  problems: Problem[];
  versions: VersionSummary[];
  preview?: PreviewRef;
  worklog: AgentWorklogItem[];
  inputRequests: UserInputRequest[];
  /** Reducer cursor used only to coalesce one streamed public message. */
  activePublicMessageId?: string;
  goalGraph: GoalGraphProjection | null;
  contextUsage?: ContextUsageSnapshot;
  /** Immutable actual configuration, shown read-only after the run is created. */
  agentFramework?: AgentFrameworkId;
  runtime?: RunRuntimeResponse;
  /** Present only after the server has finalized the run ledger. */
  usage?: RunUsage;
  disconnected?: boolean;
}

export type AgentMessageMetadata = {
  projectId: string;
  runId?: string;
  createdAt: string;
  status?: RunStatus;
};

export type AgentDataParts = {
  "agent-role": RoleActivity;
  "agent-stage": StageActivity;
  "product-spec": ArtifactRef;
  "technical-spec": ArtifactRef;
  "acceptance-trace": AcceptanceTrace[];
  "file-change": FileChange;
  command: CommandLog;
  verification: VerificationResult;
  preview: PreviewRef;
  version: VersionSummary;
  notification: { level: "info" | "warning" | "error"; message: string };
};

export type AgentUIMessage = UIMessage<AgentMessageMetadata, AgentDataParts>;
