"use client";

import { CheckIcon, CircleAlertIcon, CircleDotIcon, Link2Icon, LoaderCircleIcon } from "lucide-react";

import { MessageResponse } from "@/components/ai-elements/message";
import { Plan, PlanContent, PlanDescription, PlanHeader, PlanTitle, PlanTrigger } from "@/components/ai-elements/plan";
import { Badge } from "@/components/ui/badge";
import { formatArtifactDetail } from "@/lib/artifact-markdown";
import {
  artifactKinds,
  type AcceptanceTrace,
  type ArtifactKind,
  type ArtifactLoadState,
  type ArtifactRef,
} from "@/lib/contracts";
import { cn } from "@/lib/utils";

export interface SpecSlot {
  kind: ArtifactKind;
  state: "absent" | "loading" | "error" | "ready";
  title?: string;
  markdown?: string;
  error?: string;
}

/**
 * Pure refs + load states -> canonical SpecSlot projection. Kinds outside the
 * closed set are never surfaced, slots follow the canonical run-artifact
 * order, and a ref can only ever be absent, loading, error or ready
 * — there is no fallback to demo, old-run or other-run content.
 */
export function specSlotsFromArtifacts(
  artifacts: ArtifactRef[],
  loads: Record<string, ArtifactLoadState>,
): SpecSlot[] {
  const byKind = new Map<ArtifactKind, ArtifactRef>();
  for (const ref of artifacts) {
    if (!artifactKinds.includes(ref.kind as ArtifactKind)) {
      continue;
    }
    // Multiple refs of the same canonical kind resolve to the last one in
    // input order, which is deterministically the newest.
    byKind.set(ref.kind as ArtifactKind, ref);
  }
  return artifactKinds.map((kind) => {
    const ref = byKind.get(kind);
    if (!ref) {
      return { kind, state: "absent" };
    }
    if (ref.markdown) {
      return { kind, state: "ready", title: ref.title, markdown: ref.markdown };
    }
    const load = loads[ref.id];
    if (load?.status === "ready") {
      return {
        kind,
        state: "ready",
        title: load.detail.title,
        markdown: formatArtifactDetail(load.detail),
      };
    }
    if (load?.status === "error") {
      return { kind, state: "error", title: ref.title, error: load.message };
    }
    return { kind, state: "loading", title: ref.title };
  });
}

function SpecSlotCard({ slot }: { slot: SpecSlot }) {
  const label = slot.title || slot.kind.replaceAll("_", " ");
  if (slot.state === "ready") {
    return (
      <Plan defaultOpen>
        <PlanHeader className="gap-3 p-3">
          <div><PlanTitle>{label}</PlanTitle><PlanDescription>{slot.kind.replaceAll("_", " ")}</PlanDescription></div>
          <PlanTrigger />
        </PlanHeader>
        <PlanContent className="border-t p-3"><MessageResponse className="prose-sm max-w-none text-sm">{slot.markdown || ""}</MessageResponse></PlanContent>
      </Plan>
    );
  }
  const icon = slot.state === "loading"
    ? <LoaderCircleIcon className="size-4 animate-spin text-muted-foreground" />
    : slot.state === "error"
      ? <CircleAlertIcon className="size-4 text-destructive" />
      : <CircleDotIcon className="size-4 text-muted-foreground" />;
  const hint = slot.state === "loading"
    ? "Loading spec content…"
    : slot.state === "error"
      ? slot.error || "Could not load this spec."
      : "No structured spec received yet.";
  return (
    <article className="rounded-xl border bg-card p-4">
      <div className="flex items-center gap-2">
        {icon}
        <div className="min-w-0">
          <p className="text-sm font-medium">{label}</p>
          <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p>
        </div>
      </div>
    </article>
  );
}

function TraceStatus({ status }: { status: AcceptanceTrace["status"] }) {
  if (status === "passed") return <CheckIcon className="size-3.5" />;
  if (status === "failed" || status === "blocked") return <CircleAlertIcon className="size-3.5" />;
  return <CircleDotIcon className="size-3.5" />;
}

function ImplementationBadge({ status }: { status?: AcceptanceTrace["implementationStatus"] }) {
  if (status === "implemented") {
    return <Badge className="h-5 text-[10px]" variant="outline">implemented</Badge>;
  }
  if (status === "not_implemented") {
    return <Badge className="h-5 text-[10px] text-muted-foreground" variant="outline">not implemented</Badge>;
  }
  return <Badge className="h-5 border-amber-600/20 bg-amber-500/5 text-[10px] text-amber-800" variant="outline">implementation unlinked</Badge>;
}

export function SpecToProof({ slots, onFileSelect, trace }: { slots: SpecSlot[]; onFileSelect: (path: string) => void; trace: AcceptanceTrace[] }) {
  const specs = slots.filter((slot) => slot.state !== "absent");
  return (
    <section className="space-y-3" aria-label="Specification to proof graph">
      <div className="flex items-center justify-between"><h2 className="text-sm font-medium">Contract-to-Proof</h2><span className="font-mono text-[11px] text-muted-foreground">plan → AC → evidence</span></div>
      <div className="space-y-2">{specs.map((slot) => <SpecSlotCard key={slot.kind} slot={slot} />)}</div>
      <div className="space-y-2">
        {trace.map((item) => (
          <article className="rounded-xl border bg-card p-3" key={item.id}>
            <div className="flex gap-2"><span aria-label={`Acceptance status: ${item.status}`} className={cn("mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full", item.status === "passed" ? "bg-emerald-500/10 text-emerald-700" : item.status === "failed" ? "bg-destructive/10 text-destructive" : "bg-muted text-muted-foreground")}><TraceStatus status={item.status} /></span><div className="min-w-0 flex-1"><div className="flex items-center gap-2"><span className="font-mono text-[11px] text-muted-foreground">{item.id}</span><ImplementationBadge status={item.implementationStatus} /><Badge className="h-5 text-[10px]" variant="outline">{item.priority}</Badge></div><p className="mt-1 text-sm leading-5">{item.title}</p>{item.status === "unverified" ? <p className="mt-1 text-[11px] text-muted-foreground">unverified · no deterministic playwright evidence yet</p> : null}</div></div>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {item.evidence.map((evidence) => (
                <button className={cn("inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors hover:bg-accent", evidence.status === "passed" && "border-emerald-600/20 bg-emerald-500/5 text-emerald-800")} key={evidence.id} onClick={() => evidence.type === "file" ? onFileSelect(evidence.label) : undefined} type="button">
                  <Link2Icon className="size-3" /> {evidence.label}
                </button>
              ))}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
