"use client";

import { ArrowRightIcon, BookOpenCheckIcon, BoxesIcon, CircleAlertIcon, LoaderCircleIcon, MessageSquarePlusIcon, PlusIcon, SparklesIcon } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";

import { PromptInput, PromptInputFooter, PromptInputSubmit, PromptInputTextarea, PromptInputTools } from "@/components/ai-elements/prompt-input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ButtonGroup } from "@/components/ui/button-group";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ThemeToggle } from "@/components/theme/theme-toggle";
import { AccountEntry } from "@/components/workbench/account-entry";
import { RunTokenUsage } from "@/components/workbench/run-token-usage";
import { RuntimeSelector, frameworkProfileThinkingLevels } from "@/components/workbench/runtime-selector";
import { ApiProblem, controlPlane } from "@/lib/api/client";
import type { AgentFrameworkId, ProjectSummary, RuntimeOptionsResponse } from "@/lib/contracts";
import { useAuthStore } from "@/lib/store/auth-store";

const examples = [
  {
    label: "个人阅读清单",
    prompt: `个人阅读清单

为个人用户设计一个完成度高的单页阅读清单。主流程只有：新增书籍（书名、作者）→在“想读 / 在读 / 已读”之间切换→按状态筛选。提供少量真实示例和空状态；可使用 localStorage 保留刷新后的修改。

这是纯前端 UI 任务，不做登录、借阅库存、云同步、后端、API 或数据库。视觉与布局由你自主判断，保证桌面和手机都易用。`,
  },
  {
    label: "咖啡预订卡",
    prompt: `咖啡预订卡

为小型咖啡店设计一个手机优先的单页预订体验。用户选择饮品和取餐时间，填写姓名，提交后在同页显示订单摘要和成功反馈。只覆盖默认、校验错误、提交中和成功状态，使用本地固定菜单与内存状态。

这是纯前端 UI 任务，不做支付、账号、真实库存、订单后台、API 或数据库。视觉由你自主判断，追求轻松、清晰和精品感。`,
  },
  {
    label: "销售机会跟进卡",
    prompt: `销售机会跟进卡

为客户经理设计一个单页商机详情原型。展示客户名称、金额、阶段、下次跟进日期和三条本地活动记录；主流程仅为修改阶段和跟进日期并保存，页面内显示校验或成功反馈。使用固定本地数据，可选 localStorage。

这是纯前端 UI 任务，不做商机列表、仪表盘、图表、CRM 同步、多人协作、后端、API 或数据库。视觉应专业克制，具体排版由你判断。`,
  },
] as const;

type ProjectListTab = "active" | "attention";

const needsAttentionStatuses = new Set(["failed", "needs_attention", "cancelled"]);

function projectListTab(project: ProjectSummary): ProjectListTab {
  const status = project.latestRun?.status ?? project.status;
  return status && needsAttentionStatuses.has(status) ? "attention" : "active";
}

function runStatusPresentation(project: ProjectSummary): { label: string; variant: "secondary" | "destructive"; className?: string } {
  const status = project.latestRun?.status ?? project.status;
  if (status === "completed") {
    return {
      label: "Success",
      variant: "secondary",
      className: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    };
  }
  if (status === "cancelled") return { label: "Interrupted", variant: "destructive" };
  if (status === "needs_attention") return { label: "Needs attention", variant: "destructive" };
  if (status === "failed") return { label: "Failed", variant: "destructive" };
  if (status === "waiting_for_user") return { label: "Waiting", variant: "secondary" };
  if (status === "queued") return { label: "Queued", variant: "secondary" };
  if (status === "running") return { label: "Running", variant: "secondary" };
  return { label: "Ready", variant: "secondary" };
}

function frameworkLabel(framework: AgentFrameworkId): string {
  if (framework === "opencode") return "OpenCode";
  if (framework === "codex") return "Codex";
  return "Pi";
}

function projectLabel(prompt: string): string {
  const firstLine = prompt
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .find(Boolean)
    ?.slice(0, 80);
  return firstLine || "未命名项目";
}

function ProjectLink({ project, onRecover }: { project: ProjectSummary; onRecover: (project: ProjectSummary) => void }) {
  const status = runStatusPresentation(project);
  const runtime = project.latestRun;
  return (
    <article className="rounded-xl border bg-card p-3 transition-colors hover:border-primary/40 hover:bg-accent/30">
      <Link className="group flex min-w-0 items-start justify-between gap-3" href={`/projects/${project.id}`}>
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium">{project.name}</span>
          <span className="mt-2 flex flex-wrap items-center gap-1.5">
            <Badge className={status.className} variant={status.variant}>{status.label}</Badge>
            {runtime ? <span className="text-xs text-muted-foreground">{frameworkLabel(runtime.agentFramework)} · {runtime.profileId} · {runtime.thinking}</span> : null}
          </span>
          {runtime?.usage ? <RunTokenUsage className="mt-2" usage={runtime.usage} /> : null}
          {runtime?.errorCode ? (
            <span className="mt-2 block truncate text-xs text-destructive">
              {runtime.errorCode.replaceAll("_", " ")}
            </span>
          ) : null}
        </span>
        <ArrowRightIcon className="mt-1 size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-primary" />
      </Link>
      {runtime?.recoveryAvailable ? (
        <Button className="mt-3 w-full justify-start" onClick={() => onRecover(project)} size="sm" variant="outline">
          <MessageSquarePlusIcon aria-hidden="true" />
          Continue with a message
        </Button>
      ) : null}
    </article>
  );
}

function ProjectSkeleton() {
  return (
    <div className="flex items-center justify-between rounded-xl border bg-card px-4 py-3">
      <div className="min-w-0 space-y-2">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-3 w-24" />
      </div>
      <Skeleton className="size-4 rounded-full" />
    </div>
  );
}

export function HomeScreen() {
  const router = useRouter();
  const authStatus = useAuthStore((state) => state.status);
  const authLoading = useAuthStore((state) => state.loading);
  const user = useAuthStore((state) => state.user);
  const cacheEpoch = useAuthStore((state) => state.cacheEpoch);
  const invalidateSession = useAuthStore((state) => state.invalidate);
  const userId = authStatus === "authenticated" ? user?.id : undefined;
  const { data: projects, error, isLoading: projectsLoading, mutate } = useSWR(
    userId ? ["projects", userId, cacheEpoch] : null,
    controlPlane.getProjects,
    { revalidateOnFocus: false },
  );
  const [creating, setCreating] = useState(false);
  const [submitError, setSubmitError] = useState<string>();
  const [projectTab, setProjectTab] = useState<ProjectListTab>("active");
  const [recoveryProject, setRecoveryProject] = useState<ProjectSummary>();
  const [recoveryContent, setRecoveryContent] = useState("");
  const [recovering, setRecovering] = useState(false);
  const [recoveryError, setRecoveryError] = useState<string>();
  const [recoveryAgentFramework, setRecoveryAgentFramework] = useState<AgentFrameworkId>();
  const [recoveryProfileId, setRecoveryProfileId] = useState<string>();
  const [recoveryThinking, setRecoveryThinking] = useState<string>();
  const checkingAuth = authStatus === "unknown" || authLoading;

  const { data: runtimeOptions, error: runtimeError } = useSWR<RuntimeOptionsResponse>(
    userId ? ["runtime-options", userId, cacheEpoch] : null,
    controlPlane.getRuntimeOptions,
    { revalidateOnFocus: false },
  );
  const availableProfiles = runtimeOptions?.profiles.filter((profile) => profile.available) ?? [];
  const availableFrameworks = runtimeOptions?.agentFrameworks.filter((framework) => framework.available) ?? [];
  const hasAvailableFramework = availableFrameworks.length > 0;
  const runtimeLoading = !runtimeOptions && !runtimeError;

  const [selectedAgentFramework, setSelectedAgentFramework] = useState<AgentFrameworkId>();
  const [selectedProfileId, setSelectedProfileId] = useState<string>();
  const [selectedThinking, setSelectedThinking] = useState<string>();
  const selectedAvailableFramework = runtimeOptions?.agentFrameworks.find(
    (framework) => framework.id === selectedAgentFramework && framework.available,
  );
  const defaultAvailableFramework = runtimeOptions?.agentFrameworks.find(
    (framework) => framework.id === runtimeOptions.defaultAgentFramework && framework.available,
  );
  const activeAgentFramework = selectedAvailableFramework?.id
    ?? defaultAvailableFramework?.id
    ?? availableFrameworks[0]?.id;
  const activeFramework = runtimeOptions?.agentFrameworks.find(
    (framework) => framework.id === activeAgentFramework,
  );
  const compatibleAvailableProfiles = availableProfiles.filter(
    (profile) => !activeFramework
      || activeFramework.compatibleProfileIds.length === 0
      || activeFramework.compatibleProfileIds.includes(profile.profileId),
  );
  const hasAvailableModel = compatibleAvailableProfiles.length > 0;
  const selectedAvailableProfile = compatibleAvailableProfiles.find(
    (profile) => profile.profileId === selectedProfileId,
  );
  const defaultAvailableProfile = compatibleAvailableProfiles.find(
    (profile) => profile.profileId === runtimeOptions?.defaultProfileId,
  );
  const activeProfile = selectedAvailableProfile
    ?? defaultAvailableProfile
    ?? compatibleAvailableProfiles[0];
  const activeProfileId = activeProfile?.profileId;
  const compatibleThinkingLevels = frameworkProfileThinkingLevels(
    activeFramework,
    activeProfile,
  );
  const activeThinking = compatibleThinkingLevels.includes(selectedThinking ?? "")
    ? selectedThinking
    : compatibleThinkingLevels.includes(activeProfile?.defaultThinking ?? "")
      ? activeProfile?.defaultThinking
      : compatibleThinkingLevels[0];

  const visibleProjects = projects?.filter((project) => projectListTab(project) === projectTab) ?? [];
  const activeProjectCount = projects?.filter((project) => projectListTab(project) === "active").length ?? 0;
  const attentionProjectCount = projects?.filter((project) => projectListTab(project) === "attention").length ?? 0;

  useEffect(() => {
    if (!runtimeOptions || selectedAgentFramework) return;
    const framework = runtimeOptions.agentFrameworks.find(
      (candidate) => candidate.id === runtimeOptions.defaultAgentFramework && candidate.available,
    ) ?? runtimeOptions.agentFrameworks.find((candidate) => candidate.available);
    if (framework) setSelectedAgentFramework(framework.id);
  }, [runtimeOptions, selectedAgentFramework]);

  useEffect(() => {
    if (!runtimeOptions || selectedProfileId) return;
    const profile = compatibleAvailableProfiles.find(
      (candidate) => candidate.profileId === runtimeOptions.defaultProfileId,
    ) ?? compatibleAvailableProfiles[0];
    if (profile) {
      setSelectedProfileId(profile.profileId);
      setSelectedThinking(profile.defaultThinking);
    }
  }, [compatibleAvailableProfiles, runtimeOptions, selectedProfileId]);

  useEffect(() => {
    if (error instanceof ApiProblem && error.status === 401) {
      invalidateSession();
      router.replace("/login?mode=signin&redirect=%2F");
    }
  }, [error, invalidateSession, router]);

  useEffect(() => {
    if (!checkingAuth && !userId) {
      router.replace("/login?mode=signin&redirect=%2F");
    }
  }, [checkingAuth, router, userId]);

  const startProject = useCallback(
    async (prompt: string) => {
      const content = prompt.trim();
      if (!content) {
        setSubmitError("先描述你想构建的产品。");
        return;
      }
      if (!userId) {
        router.push("/login?mode=signin&redirect=%2F");
        return;
      }
      if (!hasAvailableFramework || !activeAgentFramework) {
        setSubmitError("还没有可用的 Coding Agent 框架，请稍后重试。");
        return;
      }
      if (!hasAvailableModel || !activeProfileId || !activeThinking) {
        setSubmitError("还没有可用的模型，请稍后重试。");
        return;
      }
      setCreating(true);
      setSubmitError(undefined);
      try {
        const project = await controlPlane.createProject({ title: projectLabel(content) });
        const run = await controlPlane.startRun(project.id, {
          clientMessageId: globalThis.crypto?.randomUUID?.() || `home-${Date.now()}`,
          content,
          agentFramework: activeAgentFramework,
          profileId: activeProfileId,
          thinking: activeThinking,
        });
        await mutate();
        router.push(`/projects/${project.id}?run=${encodeURIComponent(run.runId)}`);
      } catch (startError) {
        if (startError instanceof ApiProblem && startError.status === 401) {
          invalidateSession();
          router.push("/login?mode=signin&redirect=%2F");
          return;
        }
        setSubmitError(startError instanceof Error ? startError.message : "无法创建项目。");
      } finally {
        setCreating(false);
      }
    },
    [activeAgentFramework, activeProfileId, activeThinking, hasAvailableFramework, hasAvailableModel, invalidateSession, mutate, router, userId],
  );

  const openRecovery = useCallback((project: ProjectSummary) => {
    const source = project.latestRun;
    const framework = runtimeOptions?.agentFrameworks.find(
      (candidate) => candidate.id === source?.agentFramework && candidate.available,
    ) ?? runtimeOptions?.agentFrameworks.find(
      (candidate) => candidate.id === runtimeOptions.defaultAgentFramework && candidate.available,
    ) ?? runtimeOptions?.agentFrameworks.find((candidate) => candidate.available);
    const compatibleProfiles = availableProfiles.filter(
      (profile) => !framework
        || framework.compatibleProfileIds.length === 0
        || framework.compatibleProfileIds.includes(profile.profileId),
    );
    const profile = compatibleProfiles.find((candidate) => candidate.profileId === source?.profileId)
      ?? compatibleProfiles.find((candidate) => candidate.profileId === runtimeOptions?.defaultProfileId)
      ?? compatibleProfiles[0];
    const thinkingLevels = frameworkProfileThinkingLevels(framework, profile);
    const thinking = thinkingLevels.includes(source?.thinking ?? "")
      ? source?.thinking
      : thinkingLevels.includes(profile?.defaultThinking ?? "")
        ? profile?.defaultThinking
        : thinkingLevels[0];
    setRecoveryProject(project);
    setRecoveryContent("");
    setRecoveryError(undefined);
    setRecoveryAgentFramework(framework?.id);
    setRecoveryProfileId(profile?.profileId);
    setRecoveryThinking(thinking);
  }, [availableProfiles, runtimeOptions]);

  const recoverProject = useCallback(async () => {
    const content = recoveryContent.trim();
    const source = recoveryProject?.latestRun;
    if (!recoveryProject || !source) return;
    if (!content) {
      setRecoveryError("请说明希望 Agent 继续修复或完成什么。");
      return;
    }
    if (!recoveryAgentFramework || !recoveryProfileId || !recoveryThinking) {
      setRecoveryError("当前没有兼容的 Coding Agent 与模型组合。");
      return;
    }
    setRecovering(true);
    setRecoveryError(undefined);
    try {
      const recovered = await controlPlane.recoverRun(source.id, {
        clientMessageId: globalThis.crypto?.randomUUID?.() || `recover-${Date.now()}`,
        content,
        agentFramework: recoveryAgentFramework,
        profileId: recoveryProfileId,
        thinking: recoveryThinking,
      });
      await mutate();
      setRecoveryProject(undefined);
      router.push(`/projects/${recoveryProject.id}?run=${encodeURIComponent(recovered.runId)}`);
    } catch (recoverError) {
      if (recoverError instanceof ApiProblem && recoverError.status === 401) {
        invalidateSession();
        router.push("/login?mode=signin&redirect=%2F");
        return;
      }
      setRecoveryError(recoverError instanceof Error ? recoverError.message : "无法创建恢复任务。");
    } finally {
      setRecovering(false);
    }
  }, [invalidateSession, mutate, recoveryAgentFramework, recoveryContent, recoveryProfileId, recoveryProject, recoveryThinking, router]);

  if (checkingAuth || !userId) {
    return (
      <main className="grid min-h-screen place-items-center" role="status">
        <LoaderCircleIcon aria-hidden="true" className="size-5 animate-spin text-muted-foreground" />
        <span className="sr-only">正在检查登录状态……</span>
      </main>
    );
  }

  return (
    <main className="min-h-screen px-5 py-6 sm:px-8 lg:px-12">
      <header className="mx-auto flex max-w-6xl items-center justify-between gap-3">
        <Link className="flex items-center gap-2 font-semibold tracking-tight" href="/">
          <span className="grid size-8 place-items-center rounded-lg bg-foreground font-mono text-sm text-background">F</span>
          FOMO
        </Link>
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <AccountEntry />
        </div>
      </header>

      <section className="mx-auto grid max-w-6xl gap-12 pb-14 pt-16 lg:grid-cols-[minmax(0,1fr)_20rem] lg:pt-24">
        <div>
          <Badge className="rounded-full border-primary/20 bg-primary/10 px-3 py-1 text-primary" variant="secondary">
            <SparklesIcon aria-hidden="true" className="mr-1.5 size-3.5" />
            AI 编程工作台
          </Badge>
          <h1 className="mt-6 max-w-3xl text-4xl font-semibold tracking-[-0.04em] text-balance sm:text-5xl">
            描述一个页面，看它真实跑起来。
          </h1>
          <p className="mt-5 max-w-2xl text-base leading-7 text-muted-foreground sm:text-lg">
            把一个需求交给 FOMO——agent 会规划、构建、验证并修复你的应用，你可以跟着工作日志，随时打开真实预览。
          </p>

          <div className="mt-8 max-w-3xl rounded-2xl border bg-card p-2 shadow-[0_20px_70px_-35px_rgba(15,23,42,0.38)]">
            <PromptInput onSubmit={(message) => startProject(message.text)}>
              <PromptInputTextarea placeholder="描述目标用户、产品目标、核心流程、范围与成功标准……" />
              <PromptInputFooter>
                <RuntimeSelector
                  disabled={creating || runtimeLoading}
                  onSelectAgentFramework={setSelectedAgentFramework}
                  onSelectProfile={(profileId) => {
                    setSelectedProfileId(profileId);
                    const profile = runtimeOptions?.profiles.find((candidate) => candidate.profileId === profileId);
                    if (profile) setSelectedThinking(profile.defaultThinking);
                  }}
                  onSelectThinking={setSelectedThinking}
                  options={runtimeOptions}
                  selectedAgentFramework={activeAgentFramework}
                  selectedProfileId={activeProfileId}
                  selectedThinking={activeThinking}
                />
                <PromptInputTools>
                  <span className="hidden text-xs text-muted-foreground sm:inline">⌘↵ 开始运行</span>
                </PromptInputTools>
                <PromptInputSubmit
                  disabled={creating || !hasAvailableFramework || !hasAvailableModel}
                  status={creating ? "submitted" : "ready"}
                />
              </PromptInputFooter>
            </PromptInput>
          </div>

          {runtimeError ? (
            <div className="mt-3 flex items-start gap-2 rounded-xl border border-destructive/25 bg-destructive/5 p-3 text-sm text-destructive" role="alert">
              <CircleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span>无法加载可用模型：{runtimeError instanceof Error ? runtimeError.message : "请稍后重试。"}</span>
            </div>
          ) : null}

          {!runtimeError && (!hasAvailableFramework || !hasAvailableModel) && !runtimeLoading ? (
            <div className="mt-3 flex items-start gap-2 rounded-xl border border-destructive/25 bg-destructive/5 p-3 text-sm text-destructive" role="alert">
              <CircleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span>当前没有可用的 Coding Agent 或模型，请稍后重试。</span>
            </div>
          ) : null}

          {submitError ? (
            <div className="mt-3 flex items-start gap-2 rounded-xl border border-destructive/25 bg-destructive/5 p-3 text-sm text-destructive" role="alert">
              <CircleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span>{submitError}</span>
            </div>
          ) : null}

          <div className="mt-5 flex flex-wrap gap-2">
            {examples.map((example) => (
              <Button
                className="h-auto rounded-full px-3 py-1.5 text-left text-xs font-normal text-muted-foreground"
                disabled={creating || !hasAvailableFramework || !hasAvailableModel}
                key={example.label}
                onClick={() => startProject(example.prompt)}
                size="sm"
                title={example.prompt}
                variant="outline"
              >
                {example.label}
              </Button>
            ))}
          </div>
        </div>

        <aside className="rounded-2xl border bg-card/70 p-5 shadow-sm">
          <p className="text-sm font-medium">始终可见的过程</p>
          <ul className="mt-5 space-y-5 text-sm text-muted-foreground">
            <li className="flex gap-3"><BoxesIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-primary" /><span>一个 agent 负责规划、构建并修复你的应用，对照实时预览。</span></li>
            <li className="flex gap-3"><BookOpenCheckIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-primary" /><span>每次改动都记录在可追溯的工作日志里。</span></li>
            <li className="flex gap-3"><PlusIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-primary" /><span>每次成功的运行都会产出可恢复的版本。</span></li>
          </ul>
        </aside>
      </section>

      <section className="mx-auto max-w-6xl border-t pt-8">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-medium">最近的项目</h2>
            <p className="mt-1 text-xs text-muted-foreground">失败任务会保留原记录，并从可信断点创建新的恢复任务。</p>
          </div>
          {projectsLoading ? <span className="text-xs text-muted-foreground">正在加载项目……</span> : null}
        </div>
        {projects && projects.length > 0 ? (
          <ButtonGroup aria-label="项目状态分组" className="mb-4" role="tablist">
            <Button
              aria-selected={projectTab === "active"}
              onClick={() => setProjectTab("active")}
              role="tab"
              size="sm"
              variant={projectTab === "active" ? "secondary" : "outline"}
            >
              Active &amp; Success <Badge variant="outline">{activeProjectCount}</Badge>
            </Button>
            <Button
              aria-selected={projectTab === "attention"}
              onClick={() => setProjectTab("attention")}
              role="tab"
              size="sm"
              variant={projectTab === "attention" ? "secondary" : "outline"}
            >
              Needs Attention <Badge variant="outline">{attentionProjectCount}</Badge>
            </Button>
          </ButtonGroup>
        ) : null}
        {error ? (
          <div className="rounded-xl border border-amber-500/25 bg-amber-500/5 p-4 text-sm text-amber-800 dark:text-amber-200">
            <p className="font-medium">FastAPI 控制面不可用。</p>
            <p className="mt-1 text-amber-800/80 dark:text-amber-200/80">请启动本地服务后重试。</p>
            <Button className="mt-3" onClick={() => mutate()} size="sm" variant="outline">重新连接</Button>
          </div>
        ) : projects && projects.length > 0 && visibleProjects.length > 0 ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{visibleProjects.map((project) => <ProjectLink key={project.id} onRecover={openRecovery} project={project} />)}</div>
        ) : projects && projects.length > 0 ? (
          <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
            这个分组暂时没有项目。
          </div>
        ) : projectsLoading ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 3 }).map((_, index) => <ProjectSkeleton key={index} />)}
          </div>
        ) : (
          <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">还没有项目。在上方描述一个产品需求开始吧。</div>
        )}
      </section>

      <Dialog open={Boolean(recoveryProject)} onOpenChange={(open) => {
        if (!open && !recovering) setRecoveryProject(undefined);
      }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Continue from a trusted state</DialogTitle>
            <DialogDescription>
              原失败任务保持不变。FOMO 会创建新任务，并优先恢复最近通过验收的 checkpoint；没有 checkpoint 时从已验证版本或基础模板重新开始。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="rounded-lg border bg-muted/40 p-3 text-xs text-muted-foreground">
              <p className="truncate font-medium text-foreground">{recoveryProject?.name}</p>
              <p className="mt-1">
                {recoveryProject?.latestRun?.sourceCheckpointAvailable
                  ? "Verified checkpoint available"
                  : "Will restart from the safest available base"}
              </p>
            </div>
            <Textarea
              aria-label="恢复任务补充说明"
              disabled={recovering}
              onChange={(event) => setRecoveryContent(event.target.value)}
              placeholder="例如：保留现有页面，修复新增和重置按钮，并确保刷新后数据仍然存在。"
              rows={5}
              value={recoveryContent}
            />
            <RuntimeSelector
              disabled={recovering || runtimeLoading}
              onSelectAgentFramework={setRecoveryAgentFramework}
              onSelectProfile={(profileId) => {
                setRecoveryProfileId(profileId);
                const profile = runtimeOptions?.profiles.find((candidate) => candidate.profileId === profileId);
                if (profile) setRecoveryThinking(profile.defaultThinking);
              }}
              onSelectThinking={setRecoveryThinking}
              options={runtimeOptions}
              selectedAgentFramework={recoveryAgentFramework}
              selectedProfileId={recoveryProfileId}
              selectedThinking={recoveryThinking}
            />
            {recoveryError ? <p className="text-sm text-destructive" role="alert">{recoveryError}</p> : null}
          </div>
          <DialogFooter>
            <Button disabled={recovering} onClick={() => setRecoveryProject(undefined)} variant="outline">Cancel</Button>
            <Button disabled={recovering || runtimeLoading} onClick={recoverProject}>
              {recovering ? <LoaderCircleIcon aria-hidden="true" className="animate-spin" /> : null}
              Create recovery task
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <footer className="mx-auto max-w-6xl border-t pb-10 pt-8 text-center text-xs text-muted-foreground">
        <p>© FOMO 编程工作台 · Next.js · FastAPI · OpenSandbox · LiteLLM</p>
      </footer>
    </main>
  );
}
