"use client";

import { CheckIcon, CircleDashedIcon, CircleXIcon, LoaderCircleIcon } from "lucide-react";

import { Agent, AgentContent, AgentHeader } from "@/components/ai-elements/agent";
import { MessageResponse } from "@/components/ai-elements/message";
import { cn } from "@/lib/utils";
import type { AgentRole, RoleActivity } from "@/lib/contracts";

const roleOrder: AgentRole[] = ["product_manager", "architect", "engineer", "reviewer"];

const roleDisplay: Record<AgentRole, { label: string; marker: string }> = {
  product_manager: { label: "Product", marker: "PM" },
  architect: { label: "Architect", marker: "AR" },
  engineer: { label: "Engineer", marker: "EN" },
  reviewer: { label: "Reviewer", marker: "QA" },
};

function StatusMark({ status }: { status: RoleActivity["status"] }) {
  if (status === "completed") return <CheckIcon className="size-3.5" />;
  if (status === "failed") return <CircleXIcon className="size-3.5" />;
  if (status === "working") return <LoaderCircleIcon className="size-3.5 animate-spin" />;
  return <CircleDashedIcon className="size-3.5" />;
}

function stableTime(iso: string): string {
  const match = iso.match(/T(\d{2}:\d{2})/);
  return match ? `${match[1]} UTC` : "Time unavailable";
}

export function RoleTimeline({ roles }: { roles: Record<AgentRole, RoleActivity> }) {
  return (
    <section aria-label="Four role timeline" className="space-y-2">
      <div className="flex items-center justify-between"><h2 className="text-sm font-medium">Role timeline</h2><span className="font-mono text-[11px] text-muted-foreground">visible SOP</span></div>
      <div className="space-y-2">
        {roleOrder.map((role) => {
          const activity = roles[role];
          const display = roleDisplay[role];
          return (
            <Agent className="overflow-hidden border-border/80 bg-card shadow-none" key={role}>
              <AgentHeader className="p-3" name={display.label} />
              <AgentContent className="space-y-2 px-3 pb-3">
                <div className="flex items-center gap-2">
                  <span className="grid size-6 place-items-center rounded-md bg-muted font-mono text-[10px] font-semibold">{display.marker}</span>
                  <span className={cn("inline-flex items-center gap-1 text-xs", activity.status === "completed" && "text-emerald-700", activity.status === "failed" && "text-destructive", activity.status === "working" && "text-primary", activity.status === "idle" && "text-muted-foreground")}>
                    <StatusMark status={activity.status} /> {activity.status === "working" ? "Working" : activity.status}
                  </span>
                  {activity.updatedAt ? <time className="ml-auto text-[11px] text-muted-foreground" dateTime={activity.updatedAt}>{stableTime(activity.updatedAt)}</time> : null}
                </div>
                <p className="text-sm font-medium">{activity.title}</p>
                {activity.detail ? <MessageResponse className="text-xs leading-5 text-muted-foreground">{activity.detail}</MessageResponse> : null}
              </AgentContent>
            </Agent>
          );
        })}
      </div>
    </section>
  );
}
