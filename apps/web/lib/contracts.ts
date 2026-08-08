import type { UIMessage } from "ai";

export const agentRoles = [
  "product_manager",
  "architect",
  "engineer",
  "reviewer",
] as const;

export type AgentRole = (typeof agentRoles)[number];
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

export interface RunSnapshot {
  id: string;
  projectId: string;
  status: RunStatus;
  lastSeq: number;
  createdAt?: string;
  updatedAt?: string;
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
  status: "pending" | "passed" | "failed" | "blocked";
  evidence: TraceEvidence[];
}

export interface Artifact {
  id: string;
  kind: "product-spec" | "technical-spec" | "implementation-report" | "diagnostic-report" | string;
  title: string;
  markdown: string;
  role?: AgentRole;
  updatedAt?: string;
}

export interface RoleActivity {
  role: AgentRole;
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

export interface DomainEvent {
  schemaVersion: number;
  eventId: string;
  seq: number;
  projectId: string;
  runId: string;
  kind: string;
  role?: AgentRole;
  occurredAt: string;
  payload: Record<string, unknown>;
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
}

export interface RunPresentation {
  runId: string;
  projectId: string;
  status: RunStatus;
  lastSeq: number;
  roles: Record<AgentRole, RoleActivity>;
  artifacts: Artifact[];
  trace: AcceptanceTrace[];
  fileChanges: FileChange[];
  commands: CommandLog[];
  verifications: VerificationResult[];
  problems: Problem[];
  versions: VersionSummary[];
  preview?: PreviewRef;
  summaries: string[];
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
  "product-spec": Artifact;
  "technical-spec": Artifact;
  "acceptance-trace": AcceptanceTrace[];
  "file-change": FileChange;
  command: CommandLog;
  verification: VerificationResult;
  preview: PreviewRef;
  version: VersionSummary;
  notification: { level: "info" | "warning" | "error"; message: string };
};

export type AgentUIMessage = UIMessage<AgentMessageMetadata, AgentDataParts>;
