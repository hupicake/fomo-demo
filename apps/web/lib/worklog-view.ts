/**
 * Presentation-only projection of the bounded worklog.
 *
 * The reducer already refuses to publish private reasoning or raw tool
 * arguments. This module is the second, purely visual filter: it keeps what a
 * person needs to follow the run — phases, the agent's own progress summaries,
 * tool actions, verifications, goals, clarification-adjacent failures — and
 * pushes routine runtime chatter out of the way without discarding it.
 */

import type { AgentWorklogItem } from "@/lib/contracts";

export interface WorklogView {
  entries: AgentWorklogItem[];
  /** Routine entries removed from `entries`; never silently dropped from the UI count. */
  routineHidden: number;
}

/** Fewer than this many adjacent file writes stay individually addressable. */
const fileGroupThreshold = 3;
const groupedPathPreview = 3;

/**
 * Runtime lifecycle events (agent connected, turn completed, context
 * compaction, transport retries) explain the machine, not the work. They are
 * only promoted when they failed, because a failure is always actionable.
 */
export function isRoutineNoise(item: AgentWorklogItem): boolean {
  if (item.status === "failed") return false;
  return item.kind === "system";
}

function byOccurrence(left: AgentWorklogItem, right: AgentWorklogItem): number {
  if (left.seq !== right.seq) return left.seq - right.seq;
  return left.id.localeCompare(right.id);
}

function detailPath(item: AgentWorklogItem): string {
  const detail = item.detail || "";
  const [path] = detail.split(" · ");
  return path || item.title;
}

function groupFileRun(run: AgentWorklogItem[]): AgentWorklogItem {
  const last = run[run.length - 1];
  const paths = run.map(detailPath);
  const shown = paths.slice(0, groupedPathPreview).join("\n");
  const remaining = paths.length - groupedPathPreview;
  return {
    id: `file-group:${run[0].id}:${last.id}`,
    kind: "file",
    status: run.some((item) => item.status === "failed") ? "failed" : "completed",
    title: `${run.length} files changed`,
    detail: remaining > 0 ? `${shown}\n+${remaining} more` : shown,
    stage: last.stage,
    occurredAt: last.occurredAt,
    seq: last.seq,
  };
}

/**
 * Collapses only *adjacent* completed file writes. A verification, tool call
 * or progress note between two writes always breaks the group, so the log
 * never implies work happened in an order it did not.
 */
function collapseFileRuns(items: AgentWorklogItem[]): AgentWorklogItem[] {
  const collapsed: AgentWorklogItem[] = [];
  let run: AgentWorklogItem[] = [];
  const flush = () => {
    if (run.length >= fileGroupThreshold) collapsed.push(groupFileRun(run));
    else collapsed.push(...run);
    run = [];
  };
  for (const item of items) {
    if (item.kind === "file" && item.status !== "running") {
      run.push(item);
      continue;
    }
    flush();
    collapsed.push(item);
  }
  flush();
  return collapsed;
}

export function projectWorklogView(
  items: AgentWorklogItem[],
  options: { includeRoutine?: boolean } = {},
): WorklogView {
  const ordered = [...items].sort(byOccurrence);
  if (options.includeRoutine) {
    return { entries: collapseFileRuns(ordered), routineHidden: 0 };
  }
  const signal = ordered.filter((item) => !isRoutineNoise(item));
  return {
    entries: collapseFileRuns(signal),
    routineHidden: ordered.length - signal.length,
  };
}
