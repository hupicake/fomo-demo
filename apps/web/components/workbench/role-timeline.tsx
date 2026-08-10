"use client";

import { CheckIcon, CircleDashedIcon, CircleXIcon, LoaderCircleIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { cn } from "@/lib/utils";
import { agentStages, type AgentStage, type StageActivity } from "@/lib/contracts";

const stageDisplay: Record<AgentStage, { label: string }> = {
  planning: { label: "Plan" },
  building: { label: "Build" },
  verifying: { label: "Verify" },
  repairing: { label: "Repair" },
};

function StatusMark({ status }: { status: StageActivity["status"] }) {
  if (status === "completed") return <CheckIcon aria-hidden="true" className="size-3" />;
  if (status === "failed") return <CircleXIcon aria-hidden="true" className="size-3" />;
  if (status === "working") return <LoaderCircleIcon aria-hidden="true" className="size-3 animate-spin" />;
  return <CircleDashedIcon aria-hidden="true" className="size-3" />;
}

function stableTime(iso: string): string {
  const match = iso.match(/T(\d{2}:\d{2})/);
  return match ? `${match[1]} UTC` : "Time unavailable";
}

const railTone: Record<StageActivity["status"], string> = {
  completed: "bg-emerald-500",
  failed: "bg-destructive",
  working: "bg-primary",
  queued: "bg-primary/30",
  idle: "bg-border",
};

/**
 * The phase rail is the run's coarsest signal, so it stays four fixed
 * segments: the shape never reflows as stages change, only the fill does.
 */
export function RunTimeline({ stages }: { stages: Record<AgentStage, StageActivity> }) {
  const current = agentStages.find((stage) => stages[stage].status === "working")
    || agentStages.find((stage) => stages[stage].status === "failed");
  return (
    <section aria-label="运行阶段" data-current-stage={current || "none"}>
      <ol className="grid grid-cols-4 gap-0.5">
        {agentStages.map((stage) => {
          const activity = stages[stage];
          const display = stageDisplay[stage];
          const active = stage === current;
          return (
            <li className="min-w-0" key={stage} data-stage={stage} data-stage-status={activity.status}>
              <HoverCard openDelay={200}>
                <HoverCardTrigger asChild>
                  <Button
                    aria-label={`${display.label}: ${activity.status}`}
                    className="h-7 w-full min-w-0 gap-1 rounded-md px-1 text-left hover:bg-muted/60"
                    variant="ghost"
                  >
                    <span className={cn("size-1.5 shrink-0 rounded-full transition-colors", railTone[activity.status], activity.status === "working" && "animate-pulse")} />
                    <span className={cn(
                      "truncate text-[10px] leading-4",
                      active ? "font-semibold text-foreground" : "font-medium text-muted-foreground",
                    )}>
                      {display.label}
                    </span>
                    <span className={cn(
                      "ml-auto shrink-0",
                      activity.status === "completed" && "text-emerald-600 dark:text-emerald-400",
                      activity.status === "failed" && "text-destructive",
                      activity.status === "working" && "text-primary",
                      (activity.status === "idle" || activity.status === "queued") && "text-muted-foreground/60",
                    )}><StatusMark status={activity.status} /></span>
                  </Button>
                </HoverCardTrigger>
                <HoverCardContent align="center" className="w-72 space-y-2">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-medium">{activity.title}</p>
                    <span className="text-xs capitalize text-muted-foreground">{activity.status}</span>
                  </div>
                  {activity.detail ? <p className="text-xs leading-5 text-muted-foreground">{activity.detail}</p> : null}
                  {activity.updatedAt ? <time className="block font-mono text-[10px] text-muted-foreground" dateTime={activity.updatedAt}>{stableTime(activity.updatedAt)}</time> : null}
                </HoverCardContent>
              </HoverCard>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
