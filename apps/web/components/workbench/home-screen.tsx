"use client";

import { ArrowRightIcon, BookOpenCheckIcon, BoxesIcon, CircleAlertIcon, LoaderCircleIcon, PlusIcon, SparklesIcon } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";

import { PromptInput, PromptInputFooter, PromptInputSubmit, PromptInputTextarea, PromptInputTools } from "@/components/ai-elements/prompt-input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ThemeToggle } from "@/components/theme/theme-toggle";
import { AccountEntry } from "@/components/workbench/account-entry";
import { RuntimeSelector } from "@/components/workbench/runtime-selector";
import { ApiProblem, controlPlane } from "@/lib/api/client";
import type { AgentFrameworkId, ProjectSummary, RuntimeOptionsResponse } from "@/lib/contracts";
import { projectStatusLabel } from "@/lib/project-status";
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

const statusLabels: Record<string, string> = {
  idle: "空闲",
  queued: "排队中",
  running: "运行中",
  waiting_for_user: "等待回答",
  needs_attention: "需关注",
  completed: "已完成",
  planning: "规划中",
  building: "构建中",
  verifying: "验证中",
  repairing: "修复中",
  streaming: "生成中",
  submitted: "已提交",
  ready: "已完成",
  error: "出错",
  cancelled: "已取消",
  failed: "失败",
};

function projectStatusText(status: string): string {
  return statusLabels[status] || status || "空闲";
}

function projectLabel(prompt: string): string {
  const firstLine = prompt
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .find(Boolean)
    ?.slice(0, 80);
  return firstLine || "未命名项目";
}

function ProjectLink({ project }: { project: ProjectSummary }) {
  return (
    <Link className="group flex items-center justify-between rounded-xl border bg-card px-4 py-3 transition-colors hover:border-primary/40 hover:bg-accent/50" href={`/projects/${project.id}`}>
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium">{project.name}</span>
        <span className="mt-1 block text-xs text-muted-foreground">{projectStatusText(projectStatusLabel(project))}</span>
      </span>
      <ArrowRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-primary" />
    </Link>
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
  const checkingAuth = authStatus === "unknown" || authLoading;

  const { data: runtimeOptions, error: runtimeError } = useSWR<RuntimeOptionsResponse>(
    userId ? ["runtime-options", userId, cacheEpoch] : null,
    controlPlane.getRuntimeOptions,
    { revalidateOnFocus: false },
  );
  const availableProfiles = runtimeOptions?.profiles.filter((profile) => profile.available) ?? [];
  const availableFrameworks = runtimeOptions?.agentFrameworks.filter((framework) => framework.available) ?? [];
  const hasAvailableModel = availableProfiles.length > 0;
  const hasAvailableFramework = availableFrameworks.length > 0;
  const runtimeLoading = !runtimeOptions && !runtimeError;

  const [selectedAgentFramework, setSelectedAgentFramework] = useState<AgentFrameworkId>();
  const [selectedProfileId, setSelectedProfileId] = useState<string>();
  const [selectedThinking, setSelectedThinking] = useState<string>();
  // Resolve the effective selection: the user's pick, else the first available
  // profile (and its default thinking) so a submit is always well-formed.
  const selectedAvailableProfile = runtimeOptions?.profiles.find(
    (profile) => profile.profileId === selectedProfileId && profile.available,
  );
  const defaultAvailableProfile = runtimeOptions?.profiles.find(
    (profile) => profile.profileId === runtimeOptions.defaultProfileId && profile.available,
  );
  const activeProfileId = selectedAvailableProfile?.profileId
    ?? defaultAvailableProfile?.profileId
    ?? availableProfiles[0]?.profileId;
  const activeThinking = selectedThinking
    ?? runtimeOptions?.profiles.find((profile) => profile.profileId === activeProfileId)?.defaultThinking;
  const selectedAvailableFramework = runtimeOptions?.agentFrameworks.find(
    (framework) => framework.id === selectedAgentFramework && framework.available,
  );
  const defaultAvailableFramework = runtimeOptions?.agentFrameworks.find(
    (framework) => framework.id === runtimeOptions.defaultAgentFramework && framework.available,
  );
  const activeAgentFramework = selectedAvailableFramework?.id
    ?? defaultAvailableFramework?.id
    ?? availableFrameworks[0]?.id;

  useEffect(() => {
    if (!runtimeOptions || selectedAgentFramework) return;
    const framework = runtimeOptions.agentFrameworks.find(
      (candidate) => candidate.id === runtimeOptions.defaultAgentFramework && candidate.available,
    ) ?? runtimeOptions.agentFrameworks.find((candidate) => candidate.available);
    if (framework) setSelectedAgentFramework(framework.id);
  }, [runtimeOptions, selectedAgentFramework]);

  useEffect(() => {
    if (!runtimeOptions || selectedProfileId) return;
    const profile = runtimeOptions.profiles.find((candidate) => candidate.profileId === runtimeOptions.defaultProfileId && candidate.available)
      ?? runtimeOptions.profiles.find((candidate) => candidate.available);
    if (profile) {
      setSelectedProfileId(profile.profileId);
      setSelectedThinking(profile.defaultThinking);
    }
  }, [runtimeOptions, selectedProfileId]);

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
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-medium">最近的项目</h2>
          {projectsLoading ? <span className="text-xs text-muted-foreground">正在加载项目……</span> : null}
        </div>
        {error ? (
          <div className="rounded-xl border border-amber-500/25 bg-amber-500/5 p-4 text-sm text-amber-800 dark:text-amber-200">
            <p className="font-medium">FastAPI 控制面不可用。</p>
            <p className="mt-1 text-amber-800/80 dark:text-amber-200/80">请启动本地服务后重试。</p>
            <Button className="mt-3" onClick={() => mutate()} size="sm" variant="outline">重新连接</Button>
          </div>
        ) : projects && projects.length > 0 ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{projects.map((project) => <ProjectLink key={project.id} project={project} />)}</div>
        ) : projectsLoading ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 3 }).map((_, index) => <ProjectSkeleton key={index} />)}
          </div>
        ) : (
          <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">还没有项目。在上方描述一个产品需求开始吧。</div>
        )}
      </section>

      <footer className="mx-auto max-w-6xl border-t pb-10 pt-8 text-center text-xs text-muted-foreground">
        <p>© FOMO 编程工作台 · Next.js · FastAPI · OpenSandbox · LiteLLM</p>
      </footer>
    </main>
  );
}
