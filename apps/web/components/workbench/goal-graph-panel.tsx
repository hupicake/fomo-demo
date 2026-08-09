"use client";

import { CheckCircle2Icon, CircleDashedIcon, CircleDotIcon, CircleXIcon, ListTreeIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { developmentProgressFromGoalGraph } from "@/components/workbench/run-metrics";
import type { GoalGraphProjection, GoalProjection } from "@/lib/contracts";
import { cn } from "@/lib/utils";

export type TaskSummaryProps = {
  graph: GoalGraphProjection | null;
};

export type GoalGraphPanelProps = TaskSummaryProps;

type GoalDisplayStatus = GoalProjection["status"] | "ready" | "waiting";

function GoalStatusIcon({ status }: { status: GoalDisplayStatus }) {
  if (status === "verified") return <CheckCircle2Icon aria-hidden="true" />;
  if (status === "failed") return <CircleXIcon aria-hidden="true" />;
  if (status === "active" || status === "claimed" || status === "ready") return <CircleDotIcon aria-hidden="true" />;
  return <CircleDashedIcon aria-hidden="true" />;
}

function statusLabel(status: GoalDisplayStatus): string {
  if (status === "claimed") return "Awaiting verification";
  if (status === "active") return "In progress";
  if (status === "ready") return "Ready";
  if (status === "waiting") return "Waiting";
  return status.replaceAll("_", " ");
}

function statusTone(status: GoalDisplayStatus): string {
  if (status === "verified") return "text-emerald-600";
  if (status === "failed") return "text-destructive";
  if (status === "active" || status === "claimed") return "text-primary";
  if (status === "ready") return "text-sky-600";
  return "text-muted-foreground";
}

/**
 * Deliberately narrow: the current goal, the current step inside it, and one
 * overall progress figure. Everything else about the graph stays behind the
 * plan disclosure so the always-visible surface never competes with the log.
 */
export function TaskSummary({ graph }: TaskSummaryProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const readyGoalIds = useMemo(() => {
    if (!graph) return new Set<string>();
    const statusById = new Map(graph.goals.map((goal) => [goal.goalId, goal.status]));
    return new Set(
      graph.goals
        .filter((goal) => goal.status === "pending" && goal.dependsOn.every((dependency) => statusById.get(dependency) === "verified"))
        .map((goal) => goal.goalId),
    );
  }, [graph]);

  if (!graph) {
    return (
      <section aria-label="Current task" className="px-3 py-2.5" data-goal-state="unavailable">
        <p className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Current task</p>
        <p className="line-clamp-1 text-xs leading-5 text-muted-foreground">The delivery plan will appear when it is ready.</p>
      </section>
    );
  }

  const progress = developmentProgressFromGoalGraph(graph);
  const readyGoals = graph.goals.filter((goal) => readyGoalIds.has(goal.goalId));
  const activeGoal = graph.goals.find((goal) => goal.goalId === graph.activeGoalId)
    || graph.goals.find((goal) => goal.status === "active" || goal.status === "claimed")
    || graph.goals.find((goal) => goal.status === "failed")
    || readyGoals[0]
    || graph.goals.at(-1);
  const displayStatus = (goal: GoalProjection): GoalDisplayStatus => {
    if (readyGoalIds.has(goal.goalId)) return "ready";
    if (goal.status === "pending") return "waiting";
    return goal.status;
  };
  const activeStatus = activeGoal ? displayStatus(activeGoal) : "waiting";
  const planComplete = graph.status === "verified" || graph.status === "completed";
  const executionSummary = planComplete
    ? "All planned work verified"
    : readyGoals.length > 0
      ? `${readyGoals.length} Ready · currently executed sequentially`
      : "Current plan executes goals sequentially";
  const executionDetail = planComplete
    ? "All planned goals are verified. This run executed goals sequentially."
    : `${readyGoals.length} ${readyGoals.length === 1 ? "goal is" : "goals are"} Ready. Goals are currently executed sequentially in plan order; Ready marks eligibility, not concurrent execution.`;
  const currentStep = activeGoal?.acceptance.find((item) => item.status !== "passed")
    || activeGoal?.acceptance.at(-1);
  const percent = progress.percent;

  return (
    <section
      aria-label="Current task"
      className="space-y-2 px-3 py-2.5"
      data-goal-state={activeStatus}
      data-goal-progress={percent ?? "unknown"}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        <span className={cn("shrink-0 [&>svg]:size-4", statusTone(activeStatus))}>
          <GoalStatusIcon status={activeStatus} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2 text-[10px] leading-4 text-muted-foreground">
            <span className="shrink-0 font-medium uppercase tracking-wide">Current task</span>
            <span aria-hidden="true">·</span>
            <span className="truncate">{executionSummary}</span>
          </div>
          <p className="line-clamp-1 text-xs font-medium leading-5" title={activeGoal?.title}>{activeGoal?.title || "No active goal"}</p>
        </div>
        {activeGoal ? <Badge className="h-5 shrink-0 text-[10px] capitalize" variant={activeGoal.status === "failed" ? "destructive" : "outline"}>{statusLabel(activeStatus)}</Badge> : null}
        <HoverCard onOpenChange={setDetailsOpen} open={detailsOpen} openDelay={250}>
          <HoverCardTrigger asChild>
            <Button
              aria-expanded={detailsOpen}
              aria-label="Plan details"
              className="h-7 shrink-0 gap-1 px-2 text-[11px]"
              onClick={() => setDetailsOpen((open) => !open)}
              variant="ghost"
            >
              <ListTreeIcon aria-hidden="true" className="size-3.5" />
              <span className="hidden sm:inline">Plan</span>
            </Button>
          </HoverCardTrigger>
          <HoverCardContent align="end" className="max-h-[min(24rem,70vh)] w-[min(24rem,calc(100vw-2rem))] overflow-y-auto p-3">
            <div className="mb-2 space-y-1">
              <p className="text-xs font-medium">Full delivery plan</p>
              <p className="text-xs leading-5 text-muted-foreground">{graph.productOutcome}</p>
              <p className="text-[11px] leading-4 text-muted-foreground">{executionDetail}</p>
            </div>
            <ol className="divide-y">
              {graph.goals.map((goal) => {
                const goalStatus = displayStatus(goal);
                return (
                  <li className="space-y-1.5 py-2.5" key={goal.goalId}>
                    <div className="flex items-start gap-2">
                      <span className={cn("mt-0.5 [&>svg]:size-3.5", statusTone(goalStatus))}><GoalStatusIcon status={goalStatus} /></span>
                      <p className="min-w-0 flex-1 text-xs font-medium leading-5">{goal.title}</p>
                      <span className="shrink-0 text-[10px] capitalize text-muted-foreground">{statusLabel(goalStatus)}</span>
                    </div>
                    {goal.acceptance.length > 0 ? (
                      <ul className="space-y-1 pl-5">
                        {goal.acceptance.map((item) => <li className="flex items-start gap-2 text-[11px] leading-4 text-muted-foreground" key={item.acceptanceId}><span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full bg-muted-foreground/40", item.status === "passed" && "bg-emerald-600", (item.status === "failed" || item.status === "blocked") && "bg-destructive")} /><span className="min-w-0 flex-1">{item.title}</span><span className="capitalize">{item.status}</span></li>)}
                      </ul>
                    ) : null}
                  </li>
                );
              })}
            </ol>
            <div className="mt-2 flex items-center justify-between gap-3 border-t pt-2 text-[11px] text-muted-foreground">
              <span>{progress.total > 0 ? `${progress.completed}/${progress.total} ${progress.source} complete` : "Completion not available"}</span>
              {progress.blocked > 0 ? <span className="font-medium text-destructive">{progress.blocked} blocked</span> : <span>No deterministic blockers</span>}
            </div>
          </HoverCardContent>
        </HoverCard>
      </div>

      {activeGoal ? (
        <div className="flex min-w-0 items-center gap-3 pl-[1.625rem]">
          <p className="min-w-0 flex-1 truncate text-[11px] leading-4 text-muted-foreground">
            {currentStep
              ? <><span className="font-medium text-foreground/70">Step</span> · {currentStep.title}</>
              : "No acceptance criteria linked to this goal yet."}
          </p>
          <div className="flex shrink-0 items-center gap-1.5">
            <div
              aria-label="Overall goal progress"
              aria-valuemax={100}
              aria-valuemin={0}
              aria-valuetext={percent === undefined ? "Unknown" : `${percent}%`}
              {...(percent === undefined ? {} : { "aria-valuenow": percent })}
              className="h-1 w-16 overflow-hidden rounded-full bg-border/70"
              role="progressbar"
            >
              <div
                className={cn("h-full rounded-full transition-[width] duration-500", progress.blocked > 0 ? "bg-amber-500" : "bg-emerald-500")}
                style={{ width: `${percent === undefined ? 0 : Math.min(100, Math.max(0, percent))}%` }}
              />
            </div>
            <span className="w-8 text-right font-mono text-[10px] tabular-nums text-muted-foreground">
              {percent === undefined ? "—" : `${percent}%`}
            </span>
          </div>
        </div>
      ) : null}
    </section>
  );
}

// Compatibility export for callers that still use the former component name.
export const GoalGraphPanel = TaskSummary;
