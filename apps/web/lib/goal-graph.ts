import type {
  GoalAcceptancePriority,
  GoalAcceptanceStatus,
  GoalNavigationMode,
  GoalGraphProjection,
  GoalGraphStatus,
  GoalProjection,
  GoalRouteProjection,
} from "@/lib/contracts";

type JsonRecord = Record<string, unknown>;

const graphStatuses = new Set<GoalGraphStatus>(["active", "verified", "completed", "failed", "cancelled", "superseded"]);
const goalStatuses = new Set<GoalProjection["status"]>(["pending", "active", "claimed", "verified", "failed", "superseded"]);
const acceptanceStatuses = new Set<GoalAcceptanceStatus>(["pending", "passed", "failed", "blocked", "unverified"]);
const acceptancePriorities = new Set<GoalAcceptancePriority>(["must", "should", "could"]);
const navigationModes = new Set<GoalNavigationMode>(["single_surface", "multi_route"]);

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function normalizeAcceptance(value: unknown): GoalProjection["acceptance"][number] | undefined {
  const source = record(value);
  const acceptanceId = text(source.acceptanceId || source.acceptance_id || source.id);
  const title = text(source.title || source.description);
  if (!acceptanceId || !title) return undefined;
  const priority = text(source.priority, "must") as GoalAcceptancePriority;
  const status = text(source.status, "pending") as GoalAcceptanceStatus;
  return {
    acceptanceId,
    title,
    priority: acceptancePriorities.has(priority) ? priority : "must",
    status: acceptanceStatuses.has(status) ? status : "pending",
  };
}

export function normalizeGoalProjection(value: unknown): GoalProjection | undefined {
  const source = record(value);
  const goalId = text(source.goalId || source.goal_id || source.id);
  const title = text(source.title);
  if (!goalId || !title) return undefined;
  const status = text(source.status, "pending") as GoalProjection["status"];
  const acceptanceSource = source.acceptance;
  const acceptance = array(
    Array.isArray(acceptanceSource) ? acceptanceSource : record(acceptanceSource).criteria,
  ).flatMap((item) => {
    const normalized = normalizeAcceptance(item);
    return normalized ? [normalized] : [];
  });
  return {
    goalId,
    title,
    userVisible: source.userVisible === true || source.user_visible === true,
    dependsOn: array(source.dependsOn || source.depends_on).flatMap((dependency) => {
      const id = text(dependency);
      return id ? [id] : [];
    }),
    status: goalStatuses.has(status) ? status : "pending",
    checkpointId: text(source.checkpointId || source.checkpoint_id) || undefined,
    claimedAt: text(source.claimedAt || source.claimed_at) || undefined,
    verifiedAt: text(source.verifiedAt || source.verified_at) || undefined,
    acceptance,
    evidenceCount: Math.max(0, Math.floor(numberValue(source.evidenceCount ?? source.evidence_count))),
  };
}

function normalizeRoute(value: unknown): GoalRouteProjection | undefined {
  const source = record(value);
  const path = text(source.path);
  const title = text(source.title);
  const owningGoalId = text(source.owningGoalId || source.owning_goal_id);
  if (
    !path
    || !title
    || !owningGoalId
    || (
      typeof source.deepLinkable !== "boolean"
      && typeof source.deep_linkable !== "boolean"
    )
  ) {
    return undefined;
  }
  return {
    path,
    title,
    owningGoalId,
    deepLinkable: source.deepLinkable === true || source.deep_linkable === true,
  };
}

/** Normalizes the server-owned read projection; malformed or legacy values fail closed to null. */
export function normalizeGoalGraph(value: unknown): GoalGraphProjection | null {
  const source = record(value);
  const graphId = text(source.graphId || source.graph_id || source.id);
  const runId = text(source.runId || source.run_id);
  const productOutcome = text(source.productOutcome || source.product_outcome);
  const status = text(source.status) as GoalGraphStatus;
  if (!graphId || !runId || !productOutcome || !graphStatuses.has(status)) return null;
  const rawSchemaVersion = numberValue(source.schemaVersion ?? source.schema_version, 1);
  const schemaVersion = rawSchemaVersion === 3 ? 3 : rawSchemaVersion === 2 ? 2 : 1;
  const rawNavigationSuiteVersion = numberValue(
    source.navigationSuiteVersion ?? source.navigation_suite_version,
  );
  const navigationSuiteVersion = rawNavigationSuiteVersion === 1 ? 1 : null;
  const rawNavigationMode = text(
    source.navigationMode || source.navigation_mode,
    "single_surface",
  ) as GoalNavigationMode;
  const navigationMode = navigationModes.has(rawNavigationMode)
    ? rawNavigationMode
    : "single_surface";
  return {
    graphId,
    runId,
    revision: Math.max(0, Math.floor(numberValue(source.revision))),
    schemaVersion,
    navigationMode,
    navigationSuiteVersion,
    routes: array(source.routes).flatMap((item) => {
      const normalized = normalizeRoute(item);
      return normalized ? [normalized] : [];
    }),
    status,
    productOutcome,
    activeGoalId: text(source.activeGoalId || source.active_goal_id) || null,
    goals: array(source.goals).flatMap((item) => {
      const normalized = normalizeGoalProjection(item);
      return normalized ? [normalized] : [];
    }),
  };
}

export function goalGraphRecord(value: unknown): JsonRecord {
  return record(value);
}

export function goalGraphText(value: unknown): string {
  return text(value);
}
