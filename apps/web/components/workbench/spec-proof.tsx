"use client";

import { CheckIcon, CircleAlertIcon, CircleDotIcon, Link2Icon } from "lucide-react";

import { MessageResponse } from "@/components/ai-elements/message";
import { Plan, PlanContent, PlanDescription, PlanHeader, PlanTitle, PlanTrigger } from "@/components/ai-elements/plan";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { AcceptanceTrace, Artifact } from "@/lib/contracts";

function ArtifactPlan({ artifact }: { artifact: Artifact }) {
  return (
    <Plan defaultOpen>
      <PlanHeader className="gap-3 p-3">
        <div><PlanTitle>{artifact.title}</PlanTitle><PlanDescription>{artifact.kind.replaceAll("-", " ")}</PlanDescription></div>
        <PlanTrigger />
      </PlanHeader>
      <PlanContent className="border-t p-3"><MessageResponse className="prose-sm max-w-none text-sm">{artifact.markdown}</MessageResponse></PlanContent>
    </Plan>
  );
}

function TraceStatus({ status }: { status: AcceptanceTrace["status"] }) {
  if (status === "passed") return <CheckIcon className="size-3.5" />;
  if (status === "failed" || status === "blocked") return <CircleAlertIcon className="size-3.5" />;
  return <CircleDotIcon className="size-3.5" />;
}

export function SpecToProof({ artifacts, onFileSelect, trace }: { artifacts: Artifact[]; onFileSelect: (path: string) => void; trace: AcceptanceTrace[] }) {
  const specs = artifacts.filter((artifact) => artifact.kind === "product-spec" || artifact.kind === "technical-spec");
  return (
    <section className="space-y-3" aria-label="Specification to proof graph">
      <div className="flex items-center justify-between"><h2 className="text-sm font-medium">Spec-to-Proof</h2><span className="font-mono text-[11px] text-muted-foreground">AC → evidence</span></div>
      {specs.length > 0 ? <div className="space-y-2">{specs.map((artifact) => <ArtifactPlan artifact={artifact} key={artifact.id} />)}</div> : <div className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">Structured specs will appear after Product and Architect complete their handoff.</div>}
      <div className="space-y-2">
        {trace.map((item) => (
          <article className="rounded-xl border bg-card p-3" key={item.id}>
            <div className="flex gap-2"><span className={cn("mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full", item.status === "passed" ? "bg-emerald-500/10 text-emerald-700" : item.status === "failed" ? "bg-destructive/10 text-destructive" : "bg-muted text-muted-foreground")}><TraceStatus status={item.status} /></span><div className="min-w-0 flex-1"><div className="flex items-center gap-2"><span className="font-mono text-[11px] text-muted-foreground">{item.id}</span><Badge className="h-5 text-[10px]" variant="outline">{item.priority}</Badge></div><p className="mt-1 text-sm leading-5">{item.title}</p></div></div>
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
