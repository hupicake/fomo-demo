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
  type ArtifactRef,
  type CommandLog,
  type DomainEvent,
  type FileChange,
  type PreviewRef,
  type Problem,
  type RoleActivity,
  type RoleStatus,
  type RunPresentation,
  type RunSnapshot,
  type StageActivity,
  type VerificationResult,
  type VersionSummary,
} from "@/lib/contracts";

const maxActivityItems = 80;

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

function defaultRoles(): Record<AgentRole, RoleActivity> {
  return Object.fromEntries(
    agentRoles.map((role) => [
      role,
      { role, status: "idle", title: roleLabels[role] },
    ]),
  ) as Record<AgentRole, RoleActivity>;
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
  trace?: AcceptanceTrace[];
  versions?: VersionSummary[];
  preview?: PreviewRef;
}): RunPresentation {
  const stages = advanceStages(defaultStages(), input.run?.phase || "", input.run?.updatedAt);
  return {
    runId: input.run?.id || "",
    projectId: input.projectId,
    status: input.run?.status || "queued",
    lastSeq: input.run?.lastSeq || 0,
    roles: defaultRoles(),
    stages,
    artifacts: [],
    trace: input.trace || [],
    fileChanges: [],
    commands: [],
    verifications: [],
    problems: [],
    versions: input.versions || [],
    preview: input.preview,
    summaries: [],
  };
}

/** Replays a persisted snapshot from its first event before advancing its cursor. */
export function hydrateRunPresentationFromSnapshot(input: {
  events: DomainEvent[];
  lastSeq: number;
  preview?: PreviewRef;
  projectId: string;
  run?: RunSnapshot;
  trace?: AcceptanceTrace[];
  versions?: VersionSummary[];
  artifactRefs?: ArtifactRef[];
}): RunPresentation {
  const replayRun = input.run ? { ...input.run, lastSeq: 0 } : undefined;
  let presentation = createRunPresentation({
    projectId: input.projectId,
    run: replayRun,
    trace: input.trace,
    versions: input.versions,
    preview: input.preview,
  });
  for (const event of [...input.events].sort((left, right) => left.seq - right.seq)) {
    presentation = reduceDomainEvent(presentation, event);
  }
  return {
    ...presentation,
    // The snapshot's refs are authoritative for the display run; event-derived
    // loading refs only remain when the snapshot carried none.
    artifacts: input.artifactRefs && input.artifactRefs.length > 0 ? input.artifactRefs : presentation.artifacts,
    lastSeq: Math.max(presentation.lastSeq, input.lastSeq, input.run?.lastSeq || 0),
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
  const eventOutput = asText(payload.output || payload.chunk || payload.text);
  const nextOutput = event.kind === "command.output" ? `${previous?.output || ""}${eventOutput}` : eventOutput || previous?.output || "";
  return {
    id,
    command: asText(payload.command || payload.label, previous?.command || "command"),
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
  const toolName = asText(payload.toolName, previous?.command || "Pi tool");
  const args = payload.args && typeof payload.args === "object"
    ? JSON.stringify(payload.args)
    : "";
  return {
    id,
    command: previous?.command || `${toolName}${args ? ` ${args}` : ""}`,
    output: event.kind === "pi.tool.output"
      ? asText(payload.text)
      : previous?.output || "",
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

  if (event.kind.startsWith("agent.")) {
    const activity = activityFromEvent(event);
    if (activity) {
      next.roles = { ...state.roles, [activity.role]: activity };
    }
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
      next.problems = [...state.problems, ...problemsFromEvent(event)].slice(-maxActivityItems);
      break;
    case "run.cancelled":
      next.status = "cancelled";
      break;
    case "run.waiting_for_user":
      next.status = "waiting_for_user";
      break;
    case "artifact.upserted": {
      const ref = artifactRefFromEvent(event);
      if (ref) {
        next.artifacts = replaceById(state.artifacts, ref);
      }
      break;
    }
    case "trace.updated": {
      const updated = traceFromPayload(event.payload);
      next.trace = updated.length > 0 ? updated : state.trace;
      break;
    }
    case "file.changed":
      next.fileChanges = replaceById(state.fileChanges, fileChangeFromEvent(event));
      break;
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
    case "assistant.summary": {
      const summary = asText(event.payload.markdown || event.payload.content || event.payload.summary);
      next.summaries = summary ? [...state.summaries, summary].slice(-12) : state.summaries;
      break;
    }
    default:
      break;
  }
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
    default:
      return [
        dataChunk("notification", {
          level: event.kind.includes("failed") ? "error" : "info",
          message: asText(event.payload.message || event.payload.summary || event.kind),
        }),
      ];
  }
}
