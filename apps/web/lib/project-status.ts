import type { ProjectSummary, RunStatus } from "@/lib/contracts";

export type DisplayProjectStatus = RunStatus | "idle";

/** Prefer a loaded run state over the coarse project lifecycle. */
export function projectStatusLabel(
  project: Pick<ProjectSummary, "status">,
  latestRunStatus?: RunStatus,
): DisplayProjectStatus {
  return latestRunStatus || project.status || "idle";
}
