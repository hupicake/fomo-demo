"use client";

import {
  ActivityIcon,
  CheckCircle2Icon,
  CircleXIcon,
  FilePenLineIcon,
  LoaderCircleIcon,
  MessageSquareTextIcon,
  ShieldCheckIcon,
  TargetIcon,
  WrenchIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { ClarificationCard, type ClarificationAnswerHandler } from "@/components/workbench/clarification-card";
import type { AgentWorklogItem, UserInputRequest } from "@/lib/contracts";
import { projectWorklogView } from "@/lib/worklog-view";
import { cn } from "@/lib/utils";

const kindIcons = {
  file: FilePenLineIcon,
  goal: TargetIcon,
  progress: MessageSquareTextIcon,
  system: ActivityIcon,
  tool: WrenchIcon,
  verification: ShieldCheckIcon,
} satisfies Record<AgentWorklogItem["kind"], typeof ActivityIcon>;

function stableTime(iso: string): string {
  const match = iso.match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? `${match[1]} UTC` : "Time unavailable";
}

function StatusIcon({ status }: { status: AgentWorklogItem["status"] }) {
  if (status === "running") return <LoaderCircleIcon aria-hidden="true" className="size-3.5 animate-spin" />;
  if (status === "failed") return <CircleXIcon aria-hidden="true" className="size-3.5" />;
  if (status === "info") return <ActivityIcon aria-hidden="true" className="size-3.5" />;
  return <CheckCircle2Icon aria-hidden="true" className="size-3.5" />;
}

function DetailPreview({ current, item }: { current: boolean; item: AgentWorklogItem }) {
  const [open, setOpen] = useState(false);
  if (!item.detail) return null;
  if (item.status === "failed" || item.detail.length <= 120) {
    return <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-5 text-muted-foreground">{item.detail}</p>;
  }
  return (
    <HoverCard onOpenChange={setOpen} open={open} openDelay={250}>
      <HoverCardTrigger asChild>
        <Button
          aria-expanded={open}
          aria-label={`Read details for ${item.title}`}
          className={cn("mt-1 h-auto w-full justify-start whitespace-normal px-0 py-0 text-left text-xs font-normal leading-5 text-muted-foreground hover:bg-transparent", current ? "line-clamp-2" : "line-clamp-1")}
          onClick={() => setOpen(true)}
          variant="ghost"
        >
          {item.detail}
        </Button>
      </HoverCardTrigger>
      <HoverCardContent align="start" className="max-h-72 w-80 overflow-y-auto">
        <p className="whitespace-pre-wrap break-words text-xs leading-5">{item.detail}</p>
      </HoverCardContent>
    </HoverCard>
  );
}

function WorklogRow({ current = false, item }: { current?: boolean; item: AgentWorklogItem }) {
  const Icon = kindIcons[item.kind];
  return (
    <div className={cn("group relative flex min-w-0 shrink-0 gap-2 py-1.5 pl-7 pr-1 [content-visibility:auto] [contain-intrinsic-size:auto_4.5rem]", current && "rounded-lg bg-primary/[0.04] py-2 ring-1 ring-inset ring-primary/15")} data-status={item.status}>
      {!current ? <span aria-hidden="true" className="absolute bottom-0 left-[0.84rem] top-0 w-px bg-border last:hidden" /> : null}
      <span className={cn(
        "absolute left-1 top-2 z-10 grid size-5 place-items-center rounded-full border bg-background text-muted-foreground",
        current && "left-1.5 top-2.5 size-6",
        item.status === "running" && "border-primary/30 bg-primary/10 text-primary",
        item.status === "completed" && "border-emerald-600/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        item.status === "failed" && "border-destructive/30 bg-destructive/10 text-destructive",
      )}>
        {current ? <Icon aria-hidden="true" className="size-3.5" /> : <StatusIcon status={item.status} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-start gap-2">
          <p className="min-w-0 flex-1 text-xs font-medium leading-5">{item.title}</p>
          <span className="sr-only">状态：{item.status}</span>
          {current ? <Badge className="h-5 shrink-0 px-1.5 text-[9px] uppercase" variant={item.status === "failed" ? "destructive" : "secondary"}>{item.status === "running" ? "进行中" : item.status}</Badge> : null}
        </div>
        <DetailPreview current={current} item={item} />
        <div className={cn("mt-0.5 hidden flex-wrap items-center gap-1.5 font-mono text-[9px] uppercase tracking-wide text-muted-foreground/75 group-focus-within:flex group-hover:flex", (current || item.status === "failed") && "flex")}>
          <span>{item.kind}</span>
          {item.stage ? <><span aria-hidden="true">·</span><span>{item.stage}</span></> : null}
          <time className="ml-auto normal-case tracking-normal" dateTime={item.occurredAt}>{stableTime(item.occurredAt)}</time>
        </div>
      </div>
    </div>
  );
}

type ActivityEntry = { kind: "activity"; item: AgentWorklogItem; seq: number };
type ClarificationEntry = { kind: "clarification"; request: UserInputRequest; seq: number };
type LogEntry = ActivityEntry | ClarificationEntry;

const unavailableAnswer: ClarificationAnswerHandler = async () => {
  throw new Error("Answering is not available in this view.");
};

function orderEntries(left: LogEntry, right: LogEntry): number {
  if (left.seq !== right.seq) return left.seq - right.seq;
  if (left.kind !== right.kind) return left.kind === "activity" ? -1 : 1;
  const leftId = left.kind === "activity" ? left.item.id : left.request.id;
  const rightId = right.kind === "activity" ? right.item.id : right.request.id;
  return leftId.localeCompare(rightId);
}

export function AgentActivityPanel({
  inputRequests = [],
  items,
  onAnswer,
}: {
  inputRequests?: UserInputRequest[];
  items: AgentWorklogItem[];
  onAnswer?: ClarificationAnswerHandler;
}) {
  // The bounded reducer already strips private reasoning; this second pass
  // removes routine runtime chatter and groups adjacent file writes so the
  // log reads as phases and outcomes, not transport noise.
  const { entries, routineHidden } = useMemo(() => projectWorklogView(items), [items]);

  const orderedEntries = useMemo(() => {
    const activityEntries: ActivityEntry[] = entries.map((item): ActivityEntry => ({
      kind: "activity",
      item,
      seq: item.seq,
    }));
    const clarificationEntries: ClarificationEntry[] = inputRequests.map((request): ClarificationEntry => ({
      kind: "clarification",
      request,
      // An unresolved question is always the active tail of the log.
      seq: request.status === "pending"
        ? Number.POSITIVE_INFINITY
        : request.resolvedSeq ?? request.requestedSeq ?? 0,
    }));
    return [...activityEntries, ...clarificationEntries].sort(orderEntries);
  }, [entries, inputRequests]);

  return (
    <section aria-label="Agent 活动" data-routine-hidden={routineHidden}>
      <div className="sr-only">
        <h2>Agent 活动</h2>
        <span>
          <span>{orderedEntries.length} 条</span>
          {routineHidden > 0 ? <><span aria-hidden="true">·</span><span className="text-muted-foreground/70">{routineHidden} 条常规已隐藏</span></> : null}
        </span>
      </div>
      <div aria-live="polite" aria-relevant="additions text">
        {orderedEntries.length > 0
          ? orderedEntries.map((entry, index) => entry.kind === "activity"
            ? <WorklogRow current={index === orderedEntries.length - 1} item={entry.item} key={`activity:${entry.item.id}`} />
            : <ClarificationCard key={`clarification:${entry.request.id}`} onAnswer={onAnswer || unavailableAnswer} request={entry.request} />)
          : <p className="rounded-lg border border-dashed px-3 py-3 text-xs leading-5 text-muted-foreground">当工作开始时，活动将显示在这里。</p>}
      </div>
    </section>
  );
}
