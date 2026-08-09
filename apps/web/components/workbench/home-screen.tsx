"use client";

import { ArrowRightIcon, BookOpenCheckIcon, BoxesIcon, CircleAlertIcon, PlusIcon, SparklesIcon } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import useSWR from "swr";

import { PromptInput, PromptInputFooter, PromptInputSubmit, PromptInputTextarea, PromptInputTools } from "@/components/ai-elements/prompt-input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AccountEntry } from "@/components/workbench/account-entry";
import { controlPlane } from "@/lib/api/client";
import type { ProjectSummary } from "@/lib/contracts";
import { demoProjectId } from "@/lib/demo/library-project";
import { projectStatusLabel } from "@/lib/project-status";

const examples = [
  "构建一个图书管理系统，包含检索、借阅、归还、读者管理和库存状态。",
  "创建一个深色 SaaS 销售仪表盘，支持筛选、趋势图与手机端布局。",
  "设计一个活动报名系统，支持票种、候补队列和确认邮件。",
];

function projectLabel(prompt: string): string {
  const firstLine = prompt.replace(/\s+/g, " ").trim().slice(0, 44);
  return firstLine || "Untitled project";
}

function ProjectLink({ project }: { project: ProjectSummary }) {
  return (
    <Link className="group flex items-center justify-between rounded-xl border bg-card px-4 py-3 transition-colors hover:border-primary/40 hover:bg-accent/50" href={`/projects/${project.id}`}>
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium">{project.name}</span>
        <span className="mt-1 block text-xs text-muted-foreground">{projectStatusLabel(project)}</span>
      </span>
      <ArrowRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-primary" />
    </Link>
  );
}

export function HomeScreen() {
  const router = useRouter();
  const { data: projects, error, isLoading, mutate } = useSWR("projects", controlPlane.getProjects, { revalidateOnFocus: false });
  const [creating, setCreating] = useState(false);
  const [submitError, setSubmitError] = useState<string>();

  const startProject = useCallback(
    async (prompt: string) => {
      const content = prompt.trim();
      if (!content) {
        setSubmitError("Describe the product you want to build first.");
        return;
      }
      setCreating(true);
      setSubmitError(undefined);
      try {
        const project = await controlPlane.createProject({ title: projectLabel(content) });
        const run = await controlPlane.startRun(project.id, {
          clientMessageId: globalThis.crypto?.randomUUID?.() || `home-${Date.now()}`,
          content,
        });
        await mutate();
        router.push(`/projects/${project.id}?run=${encodeURIComponent(run.runId)}`);
      } catch (startError) {
        setSubmitError(startError instanceof Error ? startError.message : "Could not create the project.");
      } finally {
        setCreating(false);
      }
    },
    [mutate, router],
  );

  return (
    <main className="min-h-screen px-5 py-6 sm:px-8 lg:px-12">
      <header className="mx-auto flex max-w-6xl items-center justify-between gap-3">
        <Link className="flex items-center gap-2 font-semibold tracking-tight" href="/">
          <span className="grid size-8 place-items-center rounded-lg bg-slate-950 font-mono text-sm text-white">F</span>
          FOMO
        </Link>
        <div className="flex items-center gap-3">
          <AccountEntry connection={error ? "degraded" : "online"} />
          <Link className="text-sm text-muted-foreground transition-colors hover:text-foreground" href={`/projects/${demoProjectId}`}>
            Open explicit demo
          </Link>
        </div>
      </header>

      <section className="mx-auto grid max-w-6xl gap-12 pb-14 pt-16 lg:grid-cols-[minmax(0,1fr)_20rem] lg:pt-24">
        <div>
          <Badge className="rounded-full border-primary/20 bg-primary/10 px-3 py-1 text-primary" variant="secondary">
            <SparklesIcon className="mr-1.5 size-3.5" />
            AI coding-agent workbench
          </Badge>
          <h1 className="mt-6 max-w-3xl text-4xl font-semibold tracking-[-0.04em] text-balance sm:text-5xl">
            Build software with a team you can inspect.
          </h1>
          <p className="mt-5 max-w-2xl text-base leading-7 text-muted-foreground sm:text-lg">
            FOMO turns one request into a running app — an agent plans, builds, verifies, and repairs while you follow the work log and open a real preview.
          </p>

          <div className="mt-8 max-w-3xl rounded-2xl border bg-card p-2 shadow-[0_20px_70px_-35px_rgba(15,23,42,0.38)]">
            <PromptInput onSubmit={(message) => startProject(message.text)}>
              <PromptInputTextarea placeholder="Describe the product you want to build…" />
              <PromptInputFooter>
                <PromptInputTools>
                  <span className="hidden text-xs text-muted-foreground sm:inline">⌘↵ to start a run</span>
                </PromptInputTools>
                <PromptInputSubmit disabled={creating} status={creating ? "submitted" : "ready"} />
              </PromptInputFooter>
            </PromptInput>
          </div>

          {submitError ? (
            <div className="mt-3 flex items-start gap-2 rounded-xl border border-destructive/25 bg-destructive/5 p-3 text-sm text-destructive" role="alert">
              <CircleAlertIcon className="mt-0.5 size-4 shrink-0" />
              <span>{submitError}</span>
            </div>
          ) : null}

          <div className="mt-5 flex flex-wrap gap-2">
            {examples.map((example) => (
              <button className="rounded-full border bg-card px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:border-primary/30 hover:text-foreground" key={example} onClick={() => startProject(example)} type="button">
                {example.slice(0, 26)}…
              </button>
            ))}
          </div>
        </div>

        <aside className="rounded-2xl border bg-card/70 p-5 shadow-sm">
          <p className="text-sm font-medium">What stays visible</p>
          <ul className="mt-5 space-y-5 text-sm text-muted-foreground">
            <li className="flex gap-3"><BoxesIcon className="mt-0.5 size-4 shrink-0 text-primary" /><span>One agent plans, builds, and repairs your app against a live preview.</span></li>
            <li className="flex gap-3"><BookOpenCheckIcon className="mt-0.5 size-4 shrink-0 text-primary" /><span>Every change is recorded in a work log you can follow.</span></li>
            <li className="flex gap-3"><PlusIcon className="mt-0.5 size-4 shrink-0 text-primary" /><span>Each successful run produces a recoverable version.</span></li>
          </ul>
        </aside>
      </section>

      <section className="mx-auto max-w-6xl border-t pt-8">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-medium">Recent projects</h2>
          {isLoading ? <span className="text-xs text-muted-foreground">Connecting to control plane…</span> : null}
        </div>
        {error ? (
          <div className="rounded-xl border border-amber-500/25 bg-amber-500/5 p-4 text-sm text-amber-800">
            <p className="font-medium">FastAPI control plane is unavailable.</p>
            <p className="mt-1 text-amber-800/80">Start the local services, then retry. The demo below is a clearly labelled fixture and does not represent a real generated app.</p>
            <div className="mt-3 flex gap-2">
              <Button onClick={() => mutate()} size="sm" variant="outline">Retry connection</Button>
              <Button asChild size="sm" variant="secondary"><Link href={`/projects/${demoProjectId}`}>Open demo fixture</Link></Button>
            </div>
          </div>
        ) : projects && projects.length > 0 ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{projects.map((project) => <ProjectLink key={project.id} project={project} />)}</div>
        ) : !isLoading ? (
          <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">No projects yet. Start with a product request above.</div>
        ) : null}
      </section>
    </main>
  );
}
