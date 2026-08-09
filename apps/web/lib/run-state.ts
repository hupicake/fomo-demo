/**
 * The single place that turns run + preview transport vocabulary into the
 * small set of states the workbench is allowed to show. Everything here is
 * pure so the states can be asserted directly, and every state is surfaced in
 * the DOM through a stable `data-run-state` / `data-preview-state` value.
 */

import type { PreviewRef, RunStatus } from "@/lib/contracts";

export const workbenchRunStates = [
  "idle",
  "queued",
  "running",
  "waiting_for_user",
  "needs_attention",
  "failed",
  "cancelled",
  "succeeded",
] as const;

export type WorkbenchRunState = (typeof workbenchRunStates)[number];

/** Semantic tone, never a raw colour. Components map tone to their own palette. */
export type StateTone = "neutral" | "progress" | "attention" | "danger" | "success";

export interface RunStateView {
  state: WorkbenchRunState;
  label: string;
  description: string;
  tone: StateTone;
  /** True while the agent is expected to keep emitting events. */
  live: boolean;
  /** True when the run cannot advance without a human action. */
  blocked: boolean;
}

const runStateViews: Record<WorkbenchRunState, Omit<RunStateView, "state">> = {
  idle: {
    label: "No run yet",
    description: "Describe the page you want and FOMO starts the first run.",
    tone: "neutral",
    live: false,
    blocked: false,
  },
  queued: {
    label: "Queued",
    description: "The run is accepted and waiting for a sandbox.",
    tone: "progress",
    live: true,
    blocked: false,
  },
  running: {
    label: "Running",
    description: "The agent is working. Progress appears in the work log.",
    tone: "progress",
    live: true,
    blocked: false,
  },
  waiting_for_user: {
    label: "Waiting for you",
    description: "Answer the question in the work log to continue this run.",
    tone: "attention",
    live: false,
    blocked: true,
  },
  needs_attention: {
    label: "Needs attention",
    description: "The run stopped on something that needs a decision.",
    tone: "attention",
    live: false,
    blocked: true,
  },
  failed: {
    label: "Run failed",
    description: "The last run did not finish. The work log holds the reason.",
    tone: "danger",
    live: false,
    blocked: false,
  },
  cancelled: {
    label: "Run cancelled",
    description: "You stopped this run. Send a new request to continue.",
    tone: "neutral",
    live: false,
    blocked: false,
  },
  succeeded: {
    label: "Run succeeded",
    description: "The run finished. Open Preview to use the result.",
    tone: "success",
    live: false,
    blocked: false,
  },
};

const runStatusToState: Record<RunStatus, WorkbenchRunState> = {
  cancelled: "cancelled",
  completed: "succeeded",
  failed: "failed",
  needs_attention: "needs_attention",
  queued: "queued",
  running: "running",
  waiting_for_user: "waiting_for_user",
};

/**
 * An unpaired `waiting_for_user` status must never be able to block the UI on
 * its own: only a real pending request is allowed to claim that state.
 */
export function deriveRunState(input: {
  hasRun: boolean;
  isWaitingForUser: boolean;
  status?: RunStatus;
}): RunStateView {
  if (input.isWaitingForUser) {
    return { state: "waiting_for_user", ...runStateViews.waiting_for_user };
  }
  if (!input.hasRun || !input.status) {
    return { state: "idle", ...runStateViews.idle };
  }
  const mapped = runStatusToState[input.status];
  const state: WorkbenchRunState = mapped === "waiting_for_user" ? "running" : mapped || "idle";
  return { state, ...runStateViews[state] };
}

export const previewStates = ["ready", "stale", "updating", "building", "blocked", "unavailable"] as const;
export type WorkbenchPreviewState = (typeof previewStates)[number];

export interface PreviewStateView {
  state: WorkbenchPreviewState;
  label: string;
  description: string;
  tone: StateTone;
  /** Only `true` may ever mount an iframe. */
  renderable: boolean;
}

/**
 * `renderable` is deliberately conservative: the caller still has to validate
 * the URL itself, so an unrenderable preview can never fall back to sample or
 * previous-run content.
 */
export function derivePreviewState(input: {
  activeRunId?: string;
  hasValidUrl: boolean;
  preview?: PreviewRef;
  run: RunStateView;
}): PreviewStateView {
  const status = input.preview?.status;
  if (status === "reconnecting") {
    return {
      state: "updating",
      label: "Updating preview",
      description: "The last verified version stays available while the newest changes are prepared.",
      tone: "progress",
      renderable: false,
    };
  }
  if (status === "ready" && input.preview?.runId && input.hasValidUrl) {
    const stale = Boolean(input.activeRunId) && input.preview.runId !== input.activeRunId;
    return stale
      ? {
        state: "stale",
        label: "Previous version",
        description: "You are looking at the last verified build while a newer run finishes.",
        tone: "attention",
        renderable: true,
      }
      : {
        state: "ready",
        label: "Preview ready",
        description: "This is the real app served from an isolated origin.",
        tone: "success",
        renderable: true,
      };
  }
  if (input.run.state === "failed" || input.run.state === "needs_attention") {
    return {
      state: "blocked",
      label: "No preview from this run",
      description: input.preview?.error || "The run stopped before it produced a runnable preview.",
      tone: "danger",
      renderable: false,
    };
  }
  if (input.run.live) {
    return {
      state: "building",
      label: "Preview is being built",
      description: "Your app appears here as soon as the first build is servable.",
      tone: "progress",
      renderable: false,
    };
  }
  return {
    state: "unavailable",
    label: "No preview yet",
    description: input.preview?.error || "No preview has been produced for this project yet.",
    tone: "neutral",
    renderable: false,
  };
}
