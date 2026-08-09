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

export interface RunSnapshot {
  id: string;
  projectId: string;
  status: RunStatus;
  phase?: string;
  lastSeq: number;
  createdAt?: string;
  updatedAt?: string;
  pendingInputRequest?: UserInputRequest;
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
  status: "ready" | "reconnecting" | "failed" | "pending" | "demo" | "expired" | "unavailable";
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
  /** Demo fixture only; real refs never carry pre-rendered markdown. */
  markdown?: string;
}

export interface VisibleArtifactRef extends ArtifactRef {
  runId: string;
  kind: ArtifactKind;
  role: "user" | "pi" | "fomo" | "product_manager" | "architect";
  stage: "input" | "planning" | "acceptance" | "verification" | "product" | "architecture";
  schemaVersion: number;
  title: string;
  summary: string;
  createdAt: string;
}

export interface ArtifactDetail extends VisibleArtifactRef {
  content: Record<string, unknown>;
}

export type ArtifactLoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; detail: ArtifactDetail };

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

/** Read-only server projection. Lifecycle and acceptance states are never inferred by the UI. */
export interface GoalGraphProjection {
  graphId: string;
  runId: string;
  revision: number;
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
  trace?: AcceptanceTrace[];
  preview?: PreviewRef;
  artifactRefs?: VisibleArtifactRef[];
  pendingInputRequest?: UserInputRequest;
  /** Null for legacy P0 runs and responses that predate GoalGraph. */
  goalGraph: GoalGraphProjection | null;
}

export interface RunPresentation {
  runId: string;
  projectId: string;
  status: RunStatus;
  lastSeq: number;
  roles: Record<AgentRole, RoleActivity>;
  stages: Record<AgentStage, StageActivity>;
  artifacts: ArtifactRef[];
  trace: AcceptanceTrace[];
  fileChanges: FileChange[];
  commands: CommandLog[];
  verifications: VerificationResult[];
  problems: Problem[];
  versions: VersionSummary[];
  preview?: PreviewRef;
  summaries: string[];
  worklog: AgentWorklogItem[];
  inputRequests: UserInputRequest[];
  /** Reducer cursor used only to coalesce one streamed public message. */
  activePublicMessageId?: string;
  goalGraph: GoalGraphProjection | null;
  contextUsage?: ContextUsageSnapshot;
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
