"use client";

import { useChat } from "@ai-sdk/react";
import {
  ArrowLeftIcon,
  CircleAlertIcon,
  CircleDotIcon,
  FolderKanbanIcon,
  GitBranchIcon,
  LayoutPanelTopIcon,
  LoaderCircleIcon,
  MessageSquareTextIcon,
  PanelLeftCloseIcon,
  PlayIcon,
  RotateCcwIcon,
  SquareIcon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";

import { Conversation, ConversationContent, ConversationScrollButton } from "@/components/ai-elements/conversation";
import { Message, MessageContent, MessageResponse } from "@/components/ai-elements/message";
import { PromptInput, PromptInputFooter, PromptInputSubmit, PromptInputTextarea, PromptInputTools } from "@/components/ai-elements/prompt-input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ThemeToggle } from "@/components/theme/theme-toggle";
import { AgentActivityPanel } from "@/components/workbench/agent-activity-panel";
import { AccountEntry } from "@/components/workbench/account-entry";
import { TaskSummary } from "@/components/workbench/goal-graph-panel";
import { RunMetrics } from "@/components/workbench/run-metrics";
import { RunTimeline } from "@/components/workbench/role-timeline";
import { RuntimeBadge, RuntimeSelector, findRuntimeProfile } from "@/components/workbench/runtime-selector";
import { Workspace } from "@/components/workbench/workspace";
import { derivePreviewState, deriveRunState, type PreviewStateView, type RunStateView } from "@/lib/run-state";
import { ApiProblem, controlPlane, controlPlaneUrl } from "@/lib/api/client";
import { type AgentFrameworkId, type AgentUIMessage, type DomainEvent, type FileContent, type FileManifestEntry, type ProjectMessage, type ProjectSnapshot, type ProjectSummary, type RunPresentation, type RuntimeOptionsResponse, type UserInputAnswerInput, type UserInputAnswerResponse, type VersionSummary } from "@/lib/contracts";
import { submitChatMessage } from "@/lib/chat/submit-message";
import { createRunPresentation, hydrateRunPresentationFromSnapshot, reconcileInputAnswer, reconcileInputRequestSnapshot, reduceDomainEvent } from "@/lib/events/reducer";
import { projectStatusLabel } from "@/lib/project-status";
import { validatePreviewUrl } from "@/lib/preview";
import { useAuthStore } from "@/lib/store/auth-store";
import { AgentEventTransport } from "@/lib/transport/agent-event-transport";
import { useWorkbenchStore } from "@/lib/store/workbench-store";
import { cn } from "@/lib/utils";

type MobileSurface = "chat" | "workspace";

function isTerminal(status: RunPresentation["status"]): boolean {
  return status === "completed" || status === "failed" || status === "cancelled" || status === "needs_attention";
}

function isTerminalEvent(event: DomainEvent): boolean {
  return ["run.completed", "run.failed", "run.cancelled"].includes(event.kind)
    || ["succeeded", "completed", "failed", "cancelled", "needs_attention"].includes(String(event.payload.status));
}

function projectMessages(messages: ProjectMessage[], projectId: string): AgentUIMessage[] {
  return messages.map((message) => ({
    id: message.id,
    role: message.role,
    metadata: {
      projectId,
      createdAt: message.createdAt || new Date().toISOString(),
    },
    parts: [{ type: "text", text: message.content }],
  }));
}

function ReadableMessage({ message }: { message: AgentUIMessage }) {
  const textParts = message.parts.filter((part): part is Extract<typeof part, { type: "text" }> => part.type === "text");
  if (textParts.length === 0) return null;
  return (
    <Message className="workbench-message" from={message.role}>
      <MessageContent>
        {textParts.map((part, index) => message.role === "assistant" ? <MessageResponse key={`${message.id}-${index}`}>{part.text}</MessageResponse> : <p className="whitespace-pre-wrap leading-6" key={`${message.id}-${index}`}>{part.text}</p>)}
      </MessageContent>
    </Message>
  );
}

const runStatusLabels: Record<string, string> = {
  idle: "空闲",
  queued: "排队中",
  running: "运行中",
  planning: "规划中",
  building: "构建中",
  verifying: "验证中",
  publishing: "发布中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  needs_attention: "需关注",
  waiting_for_user: "等待回答",
};

function RunStatusBadge({ status }: { status: RunPresentation["status"] | "idle" }) {
  const color = status === "completed"
    ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
    : status === "failed" || status === "needs_attention"
      ? "bg-destructive/10 text-destructive"
      : status === "waiting_for_user"
        ? "bg-amber-500/10 text-amber-800 dark:text-amber-200"
        : status === "running"
          ? "bg-primary/10 text-primary"
          : "bg-muted text-muted-foreground";
  return (
    <Badge className={cn("gap-1 rounded-full border-0 px-2 py-0.5 text-[11px]", color)} variant="secondary">
      <CircleDotIcon aria-hidden="true" className={cn("size-3", status === "running" && "animate-pulse")} />
      {runStatusLabels[status] || status.replaceAll("_", " ")}
    </Badge>
  );
}

function WorkbenchBoot({ label }: { label: string }) {
  return (
    <main aria-busy="true" aria-label={label} className="flex h-dvh flex-col overflow-hidden bg-muted/30">
      <div className="flex h-12 items-center justify-between border-b bg-card px-4">
        <div className="flex items-center gap-2">
          <Skeleton className="size-7 rounded-md" />
          <Skeleton className="h-4 w-16" />
        </div>
        <Skeleton className="h-8 w-24 rounded-full" />
      </div>
      <div className="grid min-h-0 flex-1 overflow-hidden lg:grid-cols-[25rem_minmax(0,1fr)]">
        <section className="hidden h-full min-h-0 flex-col border-r bg-background lg:flex">
          <div className="flex h-12 items-center gap-2 border-b px-3">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-5 w-16 rounded-full" />
          </div>
          <div className="space-y-3 p-3">
            <Skeleton className="h-16 w-full rounded-xl" />
            <Skeleton className="h-12 w-5/6 rounded-xl" />
            <Skeleton className="h-20 w-full rounded-xl" />
          </div>
        </section>
        <div className="flex h-full min-h-0 items-center justify-center p-6">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircleIcon aria-hidden="true" className="size-4 animate-spin" />
            {label}
          </div>
        </div>
      </div>
    </main>
  );
}

function formatProjectActivity(iso?: string): string {
  if (!iso) return "暂无活动";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "暂无活动";
  const diffMs = Date.now() - date.getTime();
  if (diffMs < 0) {
    return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
  }
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

function projectStatusText(status: string): string {
  return runStatusLabels[status] || status.replaceAll("_", " ");
}

function errorNotice(prefix: string, failure: unknown): string {
  return failure instanceof Error && failure.message
    ? `${prefix}：${failure.message}`
    : prefix;
}

function ProjectRail({ currentProjectId, currentProjectName, projects, run }: { currentProjectId: string; currentProjectName: string; projects: ProjectSummary[]; run: RunPresentation }) {
  const visibleProjects = projects.map((project) => {
    const status = projectStatusLabel(project, project.id === currentProjectId && run.runId ? run.status : undefined);
    return {
      id: project.id,
      name: project.name,
      status,
      statusText: projectStatusText(status),
      activity: formatProjectActivity(project.updatedAt || project.createdAt),
      activityIso: project.updatedAt || project.createdAt,
    };
  });
  const runHint = run.runId ? `run ${run.runId.slice(0, 8)}` : "尚未开始运行";
  return (
    <aside aria-label="项目导航" className="hidden min-h-0 w-56 flex-col border-r bg-card/90 lg:flex">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b px-3">
        <Link
          aria-label="FOMO 首页"
          className="grid size-8 shrink-0 place-items-center rounded-lg bg-foreground font-mono text-xs font-semibold text-background shadow-sm"
          href="/"
        >
          F
        </Link>
        <span className="truncate text-sm font-semibold tracking-tight">FOMO</span>
      </div>
      <div className="flex h-11 shrink-0 items-center justify-between gap-2 border-b px-3">
        <span className="text-xs font-medium text-muted-foreground">项目</span>
        <Button asChild size="icon-sm" title="新建项目" variant="secondary">
          <Link href="/">
            <LayoutPanelTopIcon aria-hidden="true" className="size-4" />
            <span className="sr-only">新建项目</span>
          </Link>
        </Button>
      </div>
      <nav aria-label="项目列表" className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto overscroll-contain p-2 [scrollbar-gutter:stable]">
        {visibleProjects.length === 0 ? (
          <p className="px-2 py-3 text-xs leading-5 text-muted-foreground">还没有其他项目。</p>
        ) : null}
        {visibleProjects.map((project) => (
          <Link
            aria-current={project.id === currentProjectId ? "page" : undefined}
            aria-label={`${project.name} · ${project.statusText} · ${project.activity}`}
            className={cn(
              "flex min-w-0 flex-col gap-0.5 rounded-lg border border-transparent px-2.5 py-2 outline-none transition-colors",
              "text-muted-foreground hover:bg-muted/70 hover:text-foreground",
              "focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
              project.id === currentProjectId && "border-border bg-muted text-foreground shadow-sm",
            )}
            href={`/projects/${project.id}`}
            key={project.id}
            title={`${project.name} · ${project.statusText} · ${project.activity}`}
          >
            <span className="truncate text-sm font-medium text-foreground">{project.name}</span>
            <span className="flex min-w-0 items-center gap-1.5 text-[11px] leading-4 text-muted-foreground">
              <span className="truncate">{project.statusText}</span>
              <span aria-hidden="true" className="shrink-0 text-border">·</span>
              {project.activityIso ? (
                <time className="shrink-0 tabular-nums" dateTime={project.activityIso}>{project.activity}</time>
              ) : (
                <span className="shrink-0">{project.activity}</span>
              )}
            </span>
          </Link>
        ))}
      </nav>
      <div className="flex shrink-0 flex-col gap-2 border-t p-2">
        <div
          aria-label={`${currentProjectName} · ${runHint}`}
          className="flex min-w-0 items-start gap-2 rounded-lg bg-muted/70 px-2.5 py-2 text-muted-foreground"
          title={runHint}
        >
          <GitBranchIcon aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          <span className="min-w-0">
            <span className="block truncate text-xs font-medium text-foreground">{currentProjectName}</span>
            <span className="mt-0.5 block truncate font-mono text-[10px]">{runHint}</span>
          </span>
        </div>
        <Button asChild className="w-full justify-start" size="sm" title="返回项目列表" variant="ghost">
          <Link href="/">
            <PanelLeftCloseIcon aria-hidden="true" className="size-4" />
            项目列表
          </Link>
        </Button>
      </div>
    </aside>
  );
}

function EmptyWorkspace({ onRetry }: { onRetry: () => void }) {
  return <main className="grid min-h-screen place-items-center p-6"><section className="max-w-md rounded-2xl border bg-card p-6 shadow-sm"><CircleAlertIcon className="size-5 text-amber-600" /><h1 className="mt-3 text-lg font-semibold">项目暂时不可用</h1><p className="mt-2 text-sm leading-6 text-muted-foreground">该项目不存在、你无权访问，或控制面暂时不可达。</p><div className="mt-5 flex gap-2"><Button asChild variant="outline"><Link href="/"><ArrowLeftIcon className="mr-1.5 size-3.5" />首页</Link></Button><Button onClick={onRetry} variant="secondary">重试</Button></div></section></main>;
}

export function ProjectWorkbench({ initialRunId, projectId }: { initialRunId?: string; projectId: string }) {
  const router = useRouter();
  const authStatus = useAuthStore((state) => state.status);
  const authLoading = useAuthStore((state) => state.loading);
  const user = useAuthStore((state) => state.user);
  const cacheEpoch = useAuthStore((state) => state.cacheEpoch);
  const invalidateSession = useAuthStore((state) => state.invalidate);
  const userId = authStatus === "authenticated" ? user?.id : undefined;
  const redirectPath = `/projects/${encodeURIComponent(projectId)}${initialRunId ? `?run=${encodeURIComponent(initialRunId)}` : ""}`;

  useEffect(() => {
    if (authStatus === "unauthenticated" && !authLoading) {
      router.replace(`/login?redirect=${encodeURIComponent(redirectPath)}`);
    }
  }, [authLoading, authStatus, redirectPath, router]);

  const { data: snapshot, error: fetchError, isLoading, mutate: mutateProject } = useSWR(
    userId ? ["project", userId, cacheEpoch, projectId] : null,
    () => controlPlane.getProject(projectId),
    { revalidateOnFocus: false },
  );
  const { data: projects = [], error: projectsError, mutate: mutateProjects } = useSWR(
    userId ? ["projects", userId, cacheEpoch] : null,
    controlPlane.getProjects,
    { revalidateOnFocus: false },
  );
  const { data: runtimeOptions, error: runtimeOptionsError, mutate: mutateRuntimeOptions } = useSWR<RuntimeOptionsResponse>(
    userId ? ["runtime-options", userId, cacheEpoch] : null,
    controlPlane.getRuntimeOptions,
    { revalidateOnFocus: false },
  );
  const availableProfiles = runtimeOptions?.profiles.filter((profile) => profile.available) ?? [];
  const availableFrameworks = runtimeOptions?.agentFrameworks.filter((framework) => framework.available) ?? [];
  const hasAvailableFramework = availableFrameworks.length > 0;
  const runtimeOptionsLoading = !runtimeOptions && !runtimeOptionsError;

  // The user's choice for the next run. Defaults to the server's suggested
  // profile (and that profile's default thinking) once options load.
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
  const activeProfile = compatibleAvailableProfiles.find(
    (profile) => profile.profileId === selectedProfileId,
  ) ?? compatibleAvailableProfiles.find(
    (profile) => profile.profileId === runtimeOptions?.defaultProfileId,
  ) ?? compatibleAvailableProfiles[0];
  const activeProfileId = activeProfile?.profileId;
  const compatibleThinkingLevels = activeProfile?.thinkingLevels.filter(
    (level) => activeFramework?.compatibleThinkingLevels == null
      || activeFramework.compatibleThinkingLevels.includes(level),
  ) ?? [];
  const activeThinking = compatibleThinkingLevels.includes(selectedThinking ?? "")
    ? selectedThinking
    : compatibleThinkingLevels.includes(activeProfile?.defaultThinking ?? "")
      ? activeProfile?.defaultThinking
      : compatibleThinkingLevels[0];
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

  // Retries reuse the first selection for a given clientMessageId. useChat may
  // resend the same message id on reconnect; capture it once so the backend's
  // idempotency check never sees a conflicting runtime pair.
  const runtimeSelectionByMessageId = useRef(new Map<string, { agentFramework: AgentFrameworkId; profileId: string; thinking: string }>());
  const resolveRuntimeSelection = useCallback(
    (clientMessageId: string) => {
      const cached = runtimeSelectionByMessageId.current.get(clientMessageId);
      if (cached) return cached;
      const selection = {
        agentFramework: activeAgentFramework ?? "pi",
        profileId: activeProfileId ?? "",
        thinking: activeThinking ?? "",
      };
      if (selection.agentFramework && selection.profileId) runtimeSelectionByMessageId.current.set(clientMessageId, selection);
      return selection;
    },
    [activeAgentFramework, activeProfileId, activeThinking],
  );
  const resolveRuntimeSelectionRef = useRef(resolveRuntimeSelection);
  resolveRuntimeSelectionRef.current = resolveRuntimeSelection;

  useEffect(() => {
    if (fetchError instanceof ApiProblem && fetchError.status === 401) {
      invalidateSession();
      router.replace(`/login?redirect=${encodeURIComponent(redirectPath)}`);
    }
  }, [fetchError, invalidateSession, redirectPath, router]);

  const activeRunId = snapshot?.activeRun?.id || initialRunId;
  const { data: fetchedVersions, mutate: mutateVersions } = useSWR(
    userId && snapshot ? ["versions", userId, cacheEpoch, projectId] : null,
    () => controlPlane.getVersions(projectId),
    { revalidateOnFocus: false },
  );
  const { data: fetchedPreview, mutate: mutatePreview } = useSWR(
    userId && snapshot ? ["preview", userId, cacheEpoch, projectId] : null,
    () => controlPlane.getPreview(projectId),
    { revalidateOnFocus: false },
  );
  const [presentation, setPresentation] = useState<RunPresentation>(() => createRunPresentation({ projectId }));
  const presentationRef = useRef(presentation);
  const hydratedSnapshotRef = useRef<string | undefined>(undefined);
  const resumedRunsRef = useRef(new Set<string>());
  const [connectionMessage, setConnectionMessage] = useState<string>();
  const [mobileSurface, setMobileSurface] = useState<MobileSurface>("chat");
  const [saveError, setSaveError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const [selectedVersionId, setSelectedVersionId] = useState<string>();
  const selectedTab = useWorkbenchStore((state) => state.selectedTab);
  const setSelectedTab = useWorkbenchStore((state) => state.setSelectedTab);
  const selectedFile = useWorkbenchStore((state) => state.selectedFile);
  const setSelectedFile = useWorkbenchStore((state) => state.setSelectedFile);
  const device = useWorkbenchStore((state) => state.device);
  const setDevice = useWorkbenchStore((state) => state.setDevice);
  const setLastSeq = useWorkbenchStore((state) => state.setLastSeq);

  useEffect(() => {
    presentationRef.current = presentation;
    if (presentation.runId) setLastSeq(presentation.runId, presentation.lastSeq);
  }, [presentation, setLastSeq]);

  const transport = useMemo(() => new AgentEventTransport({
    getLastSeq: () => presentationRef.current.lastSeq,
    getRuntimeSelection: (clientMessageId) => resolveRuntimeSelectionRef.current(clientMessageId),
    onConnectionChange: (connected, message) => setConnectionMessage(connected ? undefined : message),
    onEvent: (event) => {
      setPresentation((current) => reduceDomainEvent(current, event));
      if (isTerminalEvent(event)) {
        void Promise.all([mutateProject(), mutatePreview(), mutateVersions()]).catch((refreshFailure) => {
          setConnectionMessage(errorNotice("运行已结束，但刷新项目结果失败", refreshFailure));
        });
      }
    },
    onRunStarted: (runId, agentFramework, runtime) => setPresentation((current) => ({ ...createRunPresentation({ projectId, run: { id: runId, projectId, status: "running", lastSeq: 0, agentFramework }, runtime }), versions: current.versions, preview: current.preview })),
    projectId,
  }), [mutatePreview, mutateProject, mutateVersions, projectId]);

  const { clearError, error: chatError, messages, resumeStream, sendMessage, setMessages, status: chatStatus, stop } = useChat<AgentUIMessage>({
    id: `project-${projectId}`,
    transport,
    onError: (chatFailure) => setConnectionMessage(chatFailure.message),
  });

  useEffect(() => {
    if (!snapshot) return;
    const run = snapshot.activeRun;
    // GoalGraph is deliberately bounded (six goals / twelve criteria), so a
    // full projection signature is cheap and ensures evidence or acceptance
    // updates hydrate even when graph revision/status do not change.
    const goalGraphSignature = snapshot.goalGraph ? JSON.stringify(snapshot.goalGraph) : "p0";
    const inputRequestSignature = snapshot.pendingInputRequest
      ? JSON.stringify(snapshot.pendingInputRequest)
      : "none";
    const signature = `${run?.id || initialRunId || "none"}:${snapshot.lastSeq}:${snapshot.messages.length}:${goalGraphSignature}:${inputRequestSignature}`;
    if (hydratedSnapshotRef.current !== signature) {
      hydratedSnapshotRef.current = signature;
      setMessages(projectMessages(snapshot.messages, projectId));
      setPresentation((current) => {
        const sameRun = Boolean(run?.id && current.runId === run.id);
        let next = sameRun
          ? current
          : hydrateRunPresentationFromSnapshot({
            events: snapshot.events,
            lastSeq: snapshot.lastSeq,
            preview: snapshot.preview,
            projectId,
            run,
            versions: snapshot.versions,
            goalGraph: snapshot.goalGraph,
            pendingInputRequest: snapshot.pendingInputRequest,
          });
        if (sameRun) {
          for (const event of [...snapshot.events].sort((left, right) => left.seq - right.seq)) next = reduceDomainEvent(next, event);
          next = reconcileInputRequestSnapshot(next, snapshot.pendingInputRequest);
        }
        return {
          ...next,
          lastSeq: Math.max(next.lastSeq, snapshot.lastSeq, run?.lastSeq || 0),
          versions: snapshot.versions && snapshot.versions.length > 0 ? snapshot.versions : next.versions,
          preview: snapshot.preview || next.preview,
          goalGraph: snapshot.goalGraph || next.goalGraph,
        };
      });
    }
  }, [initialRunId, projectId, setMessages, snapshot]);

  useEffect(() => {
    if (!snapshot || !(fetchedVersions || fetchedPreview)) return;
    setPresentation((current) => ({
      ...current,
      versions: fetchedVersions || current.versions,
      preview: fetchedPreview || current.preview,
    }));
  }, [fetchedPreview, fetchedVersions, snapshot]);

  useEffect(() => {
    const run = snapshot?.activeRun;
    const runId = run?.id || initialRunId;
    if (!runId) return;
    transport.hydrate(runId);
    if (run && isTerminal(run.status)) return;
    if (resumedRunsRef.current.has(runId)) return;
    resumedRunsRef.current.add(runId);
    void resumeStream();
  }, [initialRunId, resumeStream, snapshot?.activeRun, transport]);

  const manifestKey = userId && snapshot ? ["files", userId, cacheEpoch, projectId, selectedVersionId || "current"] : null;
  const { data: fetchedFiles } = useSWR(manifestKey, () => controlPlane.getFiles(projectId, selectedVersionId), { revalidateOnFocus: false });
  const files: FileManifestEntry[] = fetchedFiles || snapshot?.files || [];

  useEffect(() => {
    if ((!selectedFile || !files.some((candidate) => candidate.path === selectedFile)) && files[0]) {
      setSelectedFile(files[0].path);
    }
  }, [files, selectedFile, setSelectedFile]);

  const selectedManifest = files.find((file) => file.path === selectedFile);
  const fileKey = userId && selectedTab === "code" && selectedFile ? ["file", userId, cacheEpoch, projectId, selectedFile, selectedVersionId || "current"] : null;
  const { data: fetchedFile, mutate: mutateFile } = useSWR(fileKey, () => controlPlane.getFileContent(projectId, selectedFile || "", selectedVersionId), { revalidateOnFocus: false });
  const file: FileContent | undefined = fetchedFile?.path === selectedManifest?.path ? fetchedFile : undefined;

  const visibleMessages = messages.length > 0 ? messages : snapshot ? projectMessages(snapshot.messages, projectId) : [];

  const selectFile = useCallback((requestedPath: string) => {
    const found = files.find((file) => file.path === requestedPath || requestedPath.endsWith(file.path) || file.path.endsWith(requestedPath));
    if (found) {
      setSelectedFile(found.path);
      setSelectedTab("code");
      setMobileSurface("workspace");
    }
  }, [files, setSelectedFile, setSelectedTab]);

  const submitPrompt = useCallback(async (text: string) => {
    if (presentationRef.current.inputRequests.some((request) => request.status === "pending")) {
      throw new Error("请先回答工作日志中等待处理的问题。");
    }
    if (!text.trim()) return;
    if (!hasAvailableFramework || !activeAgentFramework) {
      const failure = new Error("当前没有可用的 Coding Agent 框架，无法开始运行。");
      setConnectionMessage(failure.message);
      throw failure;
    }
    if (!hasAvailableModel || !activeProfileId || !activeThinking) {
      const failure = new Error("当前没有可用的模型，无法开始运行。");
      setConnectionMessage(failure.message);
      throw failure;
    }
    setConnectionMessage(undefined);
    try {
      await submitChatMessage({ clearError, sendMessage, text });
    } catch (sendFailure) {
      const failure = sendFailure instanceof Error ? sendFailure : new Error("无法启动运行。");
      setConnectionMessage(failure.message);
      throw failure;
    }
  }, [activeAgentFramework, activeProfileId, activeThinking, clearError, hasAvailableFramework, hasAvailableModel, sendMessage]);

  const answerClarification = useCallback(async (requestId: string, input: UserInputAnswerInput) => {
    const current = presentationRef.current;
    const request = current.inputRequests.find((candidate) => candidate.id === requestId);
    if (!current.runId || request?.status !== "pending") {
      await mutateProject();
      throw new Error("该问题已不再等待回答。项目已刷新。");
    }

    let response: UserInputAnswerResponse;
    try {
      response = await controlPlane.answerRunInputRequest(current.runId, requestId, input);
    } catch (failure) {
      if (failure instanceof ApiProblem && (failure.status === 404 || failure.status === 409)) {
        try {
          await mutateProject();
        } catch {
          // Preserve the authoritative API error; reconnect remains available.
        }
        throw new Error("问题已变更或已回答。项目已刷新。");
      }
      throw failure;
    }

    setConnectionMessage(undefined);
    setPresentation((state) => reconcileInputAnswer(state, response));
    try {
      await mutateProject();
    } catch (refreshFailure) {
      setConnectionMessage(refreshFailure instanceof Error
        ? `回答已接受，但刷新失败：${refreshFailure.message}`
        : "回答已接受，但项目快照无法刷新。");
    }
  }, [mutateProject]);

  const stopRun = useCallback(async () => {
    const runId = presentationRef.current.runId;
    if (!runId) return;
    try {
      await controlPlane.cancelRun(runId);
      stop();
      setPresentation((current) => ({
        ...current,
        status: "cancelled",
        inputRequests: current.inputRequests.map((request) => request.status === "pending"
          ? { ...request, status: "cancelled", resolvedSeq: current.lastSeq }
          : request),
      }));
    } catch (cancelFailure) {
      setConnectionMessage(cancelFailure instanceof Error ? cancelFailure.message : "无法取消运行。");
    }
  }, [stop]);

  const reconnect = useCallback(async () => {
    setConnectionMessage(undefined);
    try {
      await Promise.all([
        mutateProject(),
        mutateProjects(),
        mutateVersions(),
        mutatePreview(),
        mutateRuntimeOptions(),
      ]);
      await resumeStream();
    } catch (reconnectFailure) {
      setConnectionMessage(reconnectFailure instanceof Error ? reconnectFailure.message : "重连失败。");
    }
  }, [mutatePreview, mutateProject, mutateProjects, mutateRuntimeOptions, mutateVersions, resumeStream]);

  const saveFile = useCallback(async (path: string, content: string, hash?: string) => {
    setSaving(true);
    setSaveError(undefined);
    try {
      const saved = await controlPlane.saveFile(projectId, {
        path,
        content,
        baseVersionId: selectedVersionId || snapshot?.project.headVersionId,
        hash,
      });
      await mutateFile(saved, { revalidate: false });
      await Promise.all([mutateProject(), mutateVersions()]);
    } catch (saveFailure) {
      const message = saveFailure instanceof ApiProblem && saveFailure.status === 409
        ? "此文件在较新的 Agent 版本中已变更。保存前请重新加载或选择该版本；FOMO 未覆盖它。"
        : saveFailure instanceof Error ? saveFailure.message : "无法保存文件。";
      setSaveError(message);
    } finally {
      setSaving(false);
    }
  }, [mutateFile, mutateProject, mutateVersions, projectId, selectedVersionId, snapshot?.project.headVersionId]);

  const restoreVersion = useCallback(async (version: VersionSummary) => {
    try {
      const restored = await controlPlane.restoreVersion(projectId, version.id);
      if (restored) setSelectedVersionId(restored.id);
      await Promise.all([mutateProject(), mutateVersions(), mutatePreview()]);
    } catch (restoreFailure) {
      setConnectionMessage(restoreFailure instanceof Error ? restoreFailure.message : "无法恢复版本。");
    }
  }, [mutatePreview, mutateProject, mutateVersions, projectId]);

  if (authStatus !== "authenticated" || authLoading || !userId) {
    return <WorkbenchBoot label="正在检查登录状态……" />;
  }
  if (fetchError && !snapshot) return <EmptyWorkspace onRetry={() => void mutateProject()} />;
  if (!snapshot && isLoading) return <WorkbenchBoot label="正在加载项目……" />;
  if (!snapshot) return null;

  const currentProjectName = snapshot.project.name;
  const runtimeProfileLabel = presentation.runtime
    ? (findRuntimeProfile(runtimeOptions, presentation.runtime.profileId)?.label ?? presentation.runtime.profileId)
    : undefined;
  const currentStatus = presentation.runId ? presentation.status : projectStatusLabel(snapshot.project);
  // The actionable request, rather than an unpaired status string, owns the
  // composer lock. Malformed or partial history must never dead-end the UI.
  const isWaitingForUser = presentation.inputRequests.some((request) => request.status === "pending");

  // Single source of truth for the handful of run/preview states the workbench
  // is allowed to show. Both are pure so the UI never invents an intermediate
  // state, and both are mirrored into `data-*` attributes for assertions.
  const runStateView: RunStateView = deriveRunState({
    hasRun: Boolean(presentation.runId),
    isWaitingForUser,
    status: presentation.runId ? presentation.status : undefined,
  });
  const previewValidation = presentation.preview ? validatePreviewUrl(presentation.preview.url) : undefined;
  const previewStateView: PreviewStateView = derivePreviewState({
    activeRunId,
    hasValidUrl: Boolean(previewValidation),
    preview: presentation.preview,
    run: runStateView,
  });
  const runActive = runStateView.live
    || isWaitingForUser
    || chatStatus === "streaming"
    || chatStatus === "submitted";
  const displayedAgentFramework = runActive
    ? presentation.agentFramework ?? activeAgentFramework
    : activeAgentFramework;
  const displayedProfileId = runActive
    ? presentation.runtime?.profileId ?? activeProfileId
    : activeProfileId;
  const displayedThinking = runActive
    ? presentation.runtime?.thinking ?? activeThinking
    : activeThinking;
  const canStopRun = runActive;
  const connectionNotice = connectionMessage
    || chatError?.message
    || (runtimeOptionsError ? errorNotice("无法加载运行时模型", runtimeOptionsError) : undefined)
    || (projectsError ? errorNotice("无法刷新项目列表", projectsError) : undefined)
    || (fetchError && snapshot ? errorNotice("无法刷新当前项目", fetchError) : undefined);
  const downloadHref = controlPlaneUrl(`/projects/${encodeURIComponent(projectId)}/download${selectedVersionId ? `?versionId=${encodeURIComponent(selectedVersionId)}` : ""}`);
  return (
    <main className="flex h-dvh flex-col overflow-hidden bg-muted/30" data-run-state={runStateView.state}>
      <div className="flex h-12 items-center justify-between border-b bg-card px-4 lg:hidden">
        <Link className="flex items-center gap-2 font-semibold" href="/">
          <span className="grid size-7 place-items-center rounded-md bg-foreground font-mono text-xs text-background">F</span>
          FOMO
        </Link>
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <AccountEntry />
          <RunStatusBadge status={currentStatus} />
        </div>
      </div>
      {connectionNotice ? (
        <div className="flex items-center justify-between gap-3 border-b border-amber-500/25 bg-amber-500/5 px-4 py-2 text-xs text-amber-900 dark:text-amber-200" role="alert">
          <span className="flex min-w-0 items-center gap-2">
            <CircleAlertIcon aria-hidden="true" className="size-3.5 shrink-0" />
            {connectionNotice}
          </span>
          <Button className="shrink-0" onClick={reconnect} size="sm" variant="outline">
            <RotateCcwIcon aria-hidden="true" className="mr-1 size-3" />
            重新连接
          </Button>
        </div>
      ) : null}
      <div className="border-b bg-card px-3 py-2 lg:hidden">
        <div aria-label="视图切换" className="grid grid-cols-2 rounded-lg bg-muted p-1" role="group">
          <button
            aria-pressed={mobileSurface === "chat"}
            className={cn("rounded-md px-3 py-1.5 text-xs font-medium transition-colors", mobileSurface === "chat" && "bg-background shadow-sm")}
            onClick={() => setMobileSurface("chat")}
            type="button"
          >
            <MessageSquareTextIcon aria-hidden="true" className="mr-1 inline size-3.5" />
            工作日志
          </button>
          <button
            aria-pressed={mobileSurface === "workspace"}
            className={cn("rounded-md px-3 py-1.5 text-xs font-medium transition-colors", mobileSurface === "workspace" && "bg-background shadow-sm")}
            onClick={() => setMobileSurface("workspace")}
            type="button"
          >
            <FolderKanbanIcon aria-hidden="true" className="mr-1 inline size-3.5" />
            工作区
          </button>
        </div>
      </div>
      <div className="grid min-h-0 flex-1 overflow-hidden lg:grid-cols-[14rem_minmax(0,24rem)_minmax(0,1fr)]">
        <ProjectRail currentProjectId={projectId} currentProjectName={currentProjectName} projects={projects} run={presentation} />
        <section
          aria-label="Agent 工作日志"
          className={cn("h-full min-h-0 overflow-hidden border-r bg-background lg:flex lg:flex-col", mobileSurface === "chat" ? "flex flex-col" : "hidden")}
          data-run-state={runStateView.state}
        >
          <header className="flex h-12 shrink-0 items-center gap-2 overflow-hidden border-b bg-card px-3">
            <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden">
              {/* Project name lives in the rail at lg+; keep it only for mobile/tablet chat. */}
              <p className="truncate text-sm font-semibold lg:hidden">{currentProjectName}</p>
              <RunStatusBadge status={presentation.status} />
              {presentation.runtime ? <RuntimeBadge agentFramework={presentation.agentFramework} profileLabel={runtimeProfileLabel ?? presentation.runtime.profileId} runtime={presentation.runtime} /> : null}
            </div>
            <div className="hidden shrink-0 items-center gap-1 lg:flex">
              <ThemeToggle />
              <AccountEntry />
            </div>
          </header>
          <div aria-label="运行状态条" className="grid h-11 shrink-0 grid-cols-[minmax(0,1fr)_11.5rem] items-center gap-2 border-b bg-card/70 px-3"><RunTimeline stages={presentation.stages} /><RunMetrics contextUsage={presentation.contextUsage} goalGraph={presentation.goalGraph} /></div>
          <div className="relative min-h-0 flex-1 overflow-hidden" aria-label="工作日志">
            <Conversation aria-label="工作日志流" className="h-full min-h-0 overscroll-contain [scrollbar-gutter:stable]"><ConversationContent className="min-h-full gap-3 p-3 pb-5">{visibleMessages.length > 0 ? visibleMessages.map((message) => <ReadableMessage key={message.id} message={message} />) : <div className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">提交一个需求以开始首次运行。</div>}<AgentActivityPanel inputRequests={presentation.inputRequests} items={presentation.worklog} onAnswer={answerClarification} /></ConversationContent><ConversationScrollButton aria-label="跳转到最新活动" /></Conversation>
          </div>
          <div className="shrink-0 border-t bg-card">
            <TaskSummary graph={presentation.goalGraph} />
            <div className="border-t p-3">
              <PromptInput onSubmit={(message) => submitPrompt(message.text)}>
                <PromptInputTextarea
                  disabled={runActive}
                  placeholder={isWaitingForUser
                    ? "请在工作日志中回答问题以继续"
                    : runActive ? "Agent 正在处理当前任务……" : "描述一个改动、修复或下一步能力……"}
                />
                <PromptInputFooter className="flex-wrap items-center gap-x-2 gap-y-1.5 !justify-start">
                  <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1.5">
                    <RuntimeSelector
                      disabled={runActive || !hasAvailableFramework || !hasAvailableModel || runtimeOptionsLoading}
                      onSelectAgentFramework={setSelectedAgentFramework}
                      onSelectProfile={(profileId) => {
                        setSelectedProfileId(profileId);
                        const profile = runtimeOptions?.profiles.find((candidate) => candidate.profileId === profileId);
                        if (profile) setSelectedThinking(profile.defaultThinking);
                      }}
                      onSelectThinking={setSelectedThinking}
                      options={runtimeOptions}
                      selectedAgentFramework={displayedAgentFramework}
                      selectedProfileId={displayedProfileId}
                      selectedThinking={displayedThinking}
                    />
                    {isWaitingForUser ? (
                      <PromptInputTools className="basis-full sm:basis-auto">
                        <span className="text-[11px] leading-4 text-muted-foreground">
                          请回答工作日志中的问题以继续本次运行。
                        </span>
                      </PromptInputTools>
                    ) : null}
                  </div>
                  <PromptInputSubmit
                    className="ml-auto shrink-0"
                    disabled={!canStopRun && (!hasAvailableFramework || !hasAvailableModel)}
                    onStop={canStopRun ? stopRun : undefined}
                    status={runActive ? "streaming" : chatStatus}
                  />
                </PromptInputFooter>
              </PromptInput>
            </div>
          </div>
        </section>
        <div className={cn("h-full min-h-0 p-1.5 lg:flex", mobileSurface === "workspace" ? "flex" : "hidden")}>
          <div className="h-full min-h-0 min-w-0 flex-1">
            <Workspace
              device={device}
              downloadHref={downloadHref}
              file={file}
              files={files}
              onDeviceChange={setDevice}
              onRestore={restoreVersion}
              onSave={saveFile}
              onSelectFile={selectFile}
              onVersionChange={setSelectedVersionId}
              previewState={previewStateView}
              presentation={presentation}
              runState={runStateView}
              saveError={saveError}
              saving={saving}
              selectedFile={selectedFile}
              selectedTab={selectedTab}
              selectedVersionId={selectedVersionId}
              setSelectedTab={setSelectedTab}
            />
          </div>
        </div>
      </div>
    </main>
  );
}
