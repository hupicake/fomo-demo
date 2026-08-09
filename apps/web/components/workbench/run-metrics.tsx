"use client";

import { Button } from "@/components/ui/button";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import type { ContextUsageSnapshot, GoalGraphProjection } from "@/lib/contracts";
import { cn } from "@/lib/utils";

export type DevelopmentProgress = {
  completed: number;
  total: number;
  blocked: number;
  source: "acceptance criteria" | "user-visible goals";
  percent?: number;
};

export function developmentProgressFromGoalGraph(graph: GoalGraphProjection | null): DevelopmentProgress {
  const userGoals = graph?.goals.filter((goal) => goal.userVisible) || [];
  const acceptance = userGoals.flatMap((goal) => goal.acceptance);
  if (acceptance.length > 0) {
    const completed = acceptance.filter((item) => item.status === "passed").length;
    return {
      completed,
      total: acceptance.length,
      blocked: acceptance.filter((item) => item.status === "blocked" || item.status === "failed").length,
      source: "acceptance criteria",
      percent: Math.round((completed / acceptance.length) * 100),
    };
  }
  const completed = userGoals.filter((goal) => goal.status === "verified").length;
  return {
    completed,
    total: userGoals.length,
    blocked: userGoals.filter((goal) => goal.status === "failed").length,
    source: "user-visible goals",
    percent: userGoals.length > 0 ? Math.round((completed / userGoals.length) * 100) : undefined,
  };
}

function Meter({ label, percent, tone }: { label: string; percent?: number; tone: string }) {
  const bounded = percent === undefined ? 0 : Math.min(100, Math.max(0, percent));
  return (
    <div
      aria-label={label}
      aria-valuemax={100}
      aria-valuemin={0}
      aria-valuetext={percent === undefined ? "Unknown" : `${percent}%`}
      {...(percent === undefined ? {} : { "aria-valuenow": bounded })}
      className="h-1 overflow-hidden rounded-full bg-border/70"
      role="progressbar"
    >
      <div className={cn("h-full rounded-full transition-[width] duration-500", tone)} style={{ width: `${bounded}%` }} />
    </div>
  );
}

function Metric({ detail, label, percent, sublabel, tone }: { detail: string; label: string; percent?: number; sublabel: string; tone: string }) {
  return (
    <HoverCard openDelay={200}>
      <HoverCardTrigger asChild>
        <Button className="h-auto w-full flex-col items-stretch gap-1 rounded-md px-2 py-1.5 text-left hover:bg-muted/60" variant="ghost">
          <span className="flex items-baseline justify-between gap-2">
            <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">{label}</span>
            <span className={cn("font-mono text-xs tabular-nums", percent === undefined ? "text-muted-foreground" : "font-semibold text-foreground")}>
              {percent === undefined ? "—" : `${percent}%`}
            </span>
          </span>
          <Meter label={`${label} progress`} percent={percent} tone={tone} />
          <span className="truncate text-[10px] font-normal leading-4 text-muted-foreground">{sublabel}</span>
        </Button>
      </HoverCardTrigger>
      <HoverCardContent align="start" className="w-72">
        <p className="text-xs leading-5">{detail}</p>
      </HoverCardContent>
    </HoverCard>
  );
}

/**
 * Two honest, compact indicators. Neither is a live counter: context is the
 * last turn-boundary snapshot and development is deterministic acceptance
 * state, so both render an em dash rather than guessing.
 */
export function RunMetrics({ contextUsage, goalGraph }: { contextUsage?: ContextUsageSnapshot; goalGraph: GoalGraphProjection | null }) {
  const contextPercent = contextUsage?.contextTokens !== undefined && contextUsage.contextWindow !== undefined && contextUsage.contextWindow > 0
    ? Math.round((contextUsage.contextTokens / contextUsage.contextWindow) * 100)
    : undefined;
  const contextDetail = contextUsage
    ? `${contextUsage.contextTokens === undefined ? "Unknown" : contextUsage.contextTokens.toLocaleString("en-US")} of ${contextUsage.contextWindow === undefined ? "unknown" : contextUsage.contextWindow.toLocaleString("en-US")} tokens at ${contextUsage.boundary === "turn_started" ? "turn start" : "turn completion"}. This is a turn-boundary snapshot, not a live counter.`
    : "Context usage is unknown until the Coding Agent emits a turn-boundary snapshot.";
  const development = developmentProgressFromGoalGraph(goalGraph);
  const developmentDetail = development.total > 0
    ? `${development.completed} of ${development.total} ${development.source} are deterministically complete${development.blocked > 0 ? `; ${development.blocked} blocked or failed` : ""}.`
    : "Development progress is unknown until user-visible goals or acceptance criteria are available.";
  const contextTone = contextPercent !== undefined && contextPercent >= 85
    ? "bg-destructive"
    : contextPercent !== undefined && contextPercent >= 65 ? "bg-amber-500" : "bg-sky-500";

  return (
    <section
      aria-label="Run metrics"
      className="grid grid-cols-2 gap-1"
      data-context-percent={contextPercent ?? "unknown"}
      data-development-percent={development.percent ?? "unknown"}
    >
      <Metric detail={contextDetail} label="Context" percent={contextPercent} sublabel="turn-boundary snapshot" tone={contextTone} />
      <Metric
        detail={developmentDetail}
        label="Development"
        percent={development.percent}
        sublabel={development.total > 0 ? `${development.completed}/${development.total} ${development.source}` : "waiting for goals"}
        tone={development.blocked > 0 ? "bg-amber-500" : "bg-emerald-500"}
      />
    </section>
  );
}
