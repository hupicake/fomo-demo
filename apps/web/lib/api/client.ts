import { artifactKinds, toProjectStatus, toRunStatus } from "@/lib/contracts";
import type {
  AcceptanceTrace,
  ArtifactDetail,
  ArtifactKind,
  DomainEvent,
  FileContent,
  FileManifestEntry,
  PreviewRef,
  ProjectMessage,
  ProjectSnapshot,
  ProjectSummary,
  RunSnapshot,
  VersionSummary,
  VisibleArtifactRef,
} from "@/lib/contracts";

const defaultApiOrigin = "http://localhost:8000";
const guestSessionPath = "/sessions/guest";

let guestSessionBootstrap: Promise<JsonRecord> | undefined;

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

function toArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function normalizeProject(value: unknown): ProjectSummary {
  const source = record(value);
  return {
    id: text(source.id || source.projectId),
    name: text(source.name || source.title, "Untitled project"),
    activeRunId: text(source.activeRunId || source.active_run_id) || undefined,
    headVersionId: text(source.headVersionId || source.head_version_id) || undefined,
    createdAt: text(source.createdAt || source.created_at) || undefined,
    updatedAt: text(source.updatedAt || source.updated_at) || undefined,
    status: toProjectStatus(source.status),
  };
}

export function normalizeRun(value: unknown): RunSnapshot | undefined {
  const source = record(value);
  const id = text(source.id || source.runId);
  if (!id) {
    return undefined;
  }
  return {
    id,
    projectId: text(source.projectId || source.project_id),
    status: toRunStatus(source.status),
    lastSeq: numberValue(source.lastSeq ?? source.last_seq),
    createdAt: text(source.createdAt || source.created_at) || undefined,
    updatedAt: text(source.updatedAt || source.updated_at) || undefined,
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

function traceEvidenceType(value: unknown): AcceptanceTrace["evidence"][number]["type"] {
  const type = text(value);
  return ["design", "file", "test", "screenshot", "version", "command"].includes(type)
    ? (type as AcceptanceTrace["evidence"][number]["type"])
    : "test";
}

function traceEvidenceStatus(value: unknown): AcceptanceTrace["evidence"][number]["status"] {
  const status = text(value);
  return ["passed", "pending", "failed"].includes(status)
    ? (status as AcceptanceTrace["evidence"][number]["status"])
    : "pending";
}

function traceStatus(value: unknown): AcceptanceTrace["status"] {
  const status = text(value);
  if (status === "passed" || status === "failed" || status === "blocked") return status;
  return status === "skipped" ? "blocked" : "pending";
}

function tracePriority(value: unknown): AcceptanceTrace["priority"] {
  const priority = text(value);
  return priority === "should" || priority === "could" ? priority : "must";
}

function traceTitle(source: JsonRecord): string {
  const explicit = text(source.title || source.description);
  if (explicit) return explicit;
  const criterion = record(source.criterion);
  const then = text(criterion.then);
  if (then) return then;
  return text(criterion.title || criterion.description, "Acceptance criterion");
}

function normalizeTraceEvidence(value: unknown, fallbackId: string): AcceptanceTrace["evidence"][number] | undefined {
  const source = record(value);
  const id = text(source.id || source.linkId || source.link_id || source.artifactId || source.artifact_id, fallbackId);
  const label = text(source.label || source.title || source.summary || source.targetRef || source.target_ref, "Evidence");
  return {
    id,
    type: traceEvidenceType(source.type || source.kind || source.targetKind || source.target_kind),
    label,
    href: text(source.href || source.url) || undefined,
    status: traceEvidenceStatus(source.status),
  };
}

function normalizeTrace(value: unknown): AcceptanceTrace | undefined {
  const source = record(value);
  const id = text(source.id || source.acId || source.ac_id || source.acceptanceId || source.acceptance_id);
  if (!id) {
    return undefined;
  }
  const evidence = [...toArray(source.links), ...toArray(source.evidence)].flatMap((item, index) => {
    const normalized = normalizeTraceEvidence(item, `${id}-${index}`);
    return normalized ? [normalized] : [];
  });
  const criterion = record(source.criterion);
  return {
    id,
    title: traceTitle(source),
    priority: tracePriority(source.priority || criterion.priority),
    status: traceStatus(source.status),
    evidence,
  };
}

/** Supports both the V1 graph response and the richer trace items planned by the API. */
function normalizeTraceResponse(value: unknown): AcceptanceTrace[] {
  if (Array.isArray(value)) {
    return value.flatMap((item) => {
      const normalized = normalizeTrace(item);
      return normalized ? [normalized] : [];
    });
  }
  const source = record(value);
  const direct = toArray(source.items || source.trace || source.data || source.acceptanceTrace || source.acceptance_trace)
    .flatMap((item) => {
      const normalized = normalizeTrace(item);
      return normalized ? [normalized] : [];
    });
  if (direct.length > 0) {
    return direct;
  }

  const byAcceptanceId = new Map<string, AcceptanceTrace>();
  const ensureAcceptance = (id: string): AcceptanceTrace => {
    const existing = byAcceptanceId.get(id);
    if (existing) return existing;
    const created: AcceptanceTrace = {
      id,
      title: `Acceptance criterion ${id}`,
      priority: "must",
      status: "pending",
      evidence: [],
    };
    byAcceptanceId.set(id, created);
    return created;
  };

  for (const link of toArray(source.links)) {
    const item = record(link);
    if (text(item.sourceKind || item.source_kind) !== "acceptance_criterion") continue;
    const id = text(item.sourceRef || item.source_ref);
    if (!id) continue;
    const targetRef = text(item.targetRef || item.target_ref);
    const targetKind = text(item.targetKind || item.target_kind);
    if (targetRef) {
      const trace = ensureAcceptance(id);
      trace.evidence.push({
        id: text(item.id, `${id}-${targetRef}`),
        type: traceEvidenceType(targetKind === "file" ? "file" : targetKind),
        label: targetRef,
        status: "pending",
      });
    }
  }

  for (const evidence of toArray(source.evidence)) {
    const item = record(evidence);
    const acceptanceId = text(item.acceptanceId || item.acceptance_id);
    if (!acceptanceId) continue;
    const trace = ensureAcceptance(acceptanceId);
    trace.evidence.push({
      id: text(item.id, `${acceptanceId}-${trace.evidence.length}`),
      type: traceEvidenceType(item.kind),
      label: text(item.summary, "Verification evidence"),
      status: traceEvidenceStatus(item.status),
    });
  }

  return [...byAcceptanceId.values()].map((trace) => ({
    ...trace,
    status: trace.evidence.some((item) => item.status === "failed")
      ? "failed"
      : trace.evidence.length > 0 && trace.evidence.every((item) => item.status === "passed")
        ? "passed"
        : "pending",
  }));
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
  return {
    status: (status || "pending") as PreviewRef["status"],
    url: text(source.url) || undefined,
    runId: text(source.runId || source.run_id) || undefined,
    error: text(source.error || source.detail) || undefined,
  };
}

function normalizeArtifactRef(value: unknown): VisibleArtifactRef | undefined {
  const source = record(value);
  const id = text(source.id || source.artifactId || source.artifact_id);
  const runId = text(source.runId || source.run_id);
  const kind = text(source.kind);
  const role = text(source.role);
  const schemaVersion = numberValue(source.schemaVersion ?? source.schema_version, -1);
  const title = text(source.title);
  const summary = text(source.summary);
  const createdAt = text(source.createdAt || source.created_at);
  if (!id || !runId || !kind || !role || schemaVersion < 0 || !title || !summary || !createdAt) {
    return undefined;
  }
  if (!artifactKinds.includes(kind as ArtifactKind)) {
    return undefined;
  }
  // The kind->role mapping is fixed: a ref whose role does not match its kind
  // is malformed and fails closed.
  const expectedRole = kind === "product_spec" ? "product_manager" : "architect";
  if (role !== expectedRole) {
    return undefined;
  }
  return { id, runId, kind: kind as ArtifactKind, role: expectedRole, schemaVersion, title, summary, createdAt };
}

function normalizeArtifactDetail(value: unknown): ArtifactDetail | undefined {
  const source = record(value);
  const ref = normalizeArtifactRef(source);
  if (!ref) {
    return undefined;
  }
  const content = source.content;
  if (!content || typeof content !== "object" || Array.isArray(content)) {
    return undefined;
  }
  return { ...ref, content: content as Record<string, unknown> };
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

function isGuestSessionRequest(path: string): boolean {
  return path.replace(/\/+$/, "") === guestSessionPath;
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

async function createGuestSessionRequest(): Promise<JsonRecord> {
  const response = await executeRequest(guestSessionPath, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey() },
  });
  if (!response.ok) {
    throw await responseProblem(response);
  }
  return response.status === 204 ? {} : record(await response.json());
}

/**
 * Shares one cookie-creating request among concurrent 401 recoveries. This
 * intentionally uses the raw executor so a failed guest-session request never
 * tries to bootstrap itself recursively.
 */
function bootstrapGuestSession(): Promise<JsonRecord> {
  if (!guestSessionBootstrap) {
    guestSessionBootstrap = createGuestSessionRequest().finally(() => {
      guestSessionBootstrap = undefined;
    });
  }
  return guestSessionBootstrap;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response = await executeRequest(path, init);
  if (response.status === 401 && !isGuestSessionRequest(path)) {
    await bootstrapGuestSession();
    // Retry the original operation exactly once with the newly set cookie.
    response = await executeRequest(path, init);
  }
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
  async createGuestSession() {
    return bootstrapGuestSession();
  },

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
      trace: normalizeTraceResponse(source.trace || source.acceptanceTrace || source.acceptance_trace),
      preview: normalizePreview(source.preview),
      artifactRefs: toArray(source.artifactRefs || source.artifact_refs).flatMap((item) => {
        const normalized = normalizeArtifactRef(item);
        return normalized ? [normalized] : [];
      }),
    };
  },

  async getRun(runId: string): Promise<RunSnapshot | undefined> {
    const response = await request<unknown>(`/runs/${encodeURIComponent(runId)}`);
    return normalizeRun(record(response).run || response);
  },

  async startRun(
    projectId: string,
    input: { clientMessageId: string; content: string; baseVersionId?: string },
  ): Promise<{ runId: string }> {
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}/messages`, {
      method: "POST",
      headers: { "Idempotency-Key": input.clientMessageId },
      body: JSON.stringify({ ...input, attachments: [] }),
    });
    const source = record(response);
    const runId = text(source.runId || source.run_id || record(source.run).id);
    if (!runId) {
      throw new ApiProblem({ status: 502, title: "Control plane did not return a run ID" });
    }
    return { runId };
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

  async getTrace(projectId: string, runId?: string): Promise<AcceptanceTrace[]> {
    const query = runId ? `?runId=${encodeURIComponent(runId)}` : "";
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}/trace${query}`);
    return normalizeTraceResponse(response);
  },

  async getPreview(projectId: string): Promise<PreviewRef | undefined> {
    const response = await request<unknown>(`/projects/${encodeURIComponent(projectId)}/preview`);
    return normalizePreview(record(response).preview || response);
  },

  async getArtifact(runId: string, artifactId: string): Promise<ArtifactDetail> {
    const response = await request<unknown>(
      `/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`,
    );
    const detail = normalizeArtifactDetail(record(response).artifact || response);
    if (!detail || detail.runId !== runId || detail.id !== artifactId) {
      throw new ApiProblem({ status: 502, title: "Artifact detail did not match the requested run or artifact" });
    }
    return detail;
  },

  async restoreVersion(projectId: string, versionId: string): Promise<VersionSummary | undefined> {
    const response = await request<unknown>(
      `/projects/${encodeURIComponent(projectId)}/versions/${encodeURIComponent(versionId)}/restore`,
      { method: "POST", headers: { "Idempotency-Key": idempotencyKey() } },
    );
    return normalizeVersion(record(response).version || response);
  },
};
