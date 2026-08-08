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
import { RoleTimeline } from "@/components/workbench/role-timeline";
import { SpecToProof, specSlotsFromArtifacts, type SpecSlot } from "@/components/workbench/spec-proof";
import { Workspace } from "@/components/workbench/workspace";
import { ApiProblem, controlPlane, controlPlaneUrl } from "@/lib/api/client";
import type { AgentUIMessage, ArtifactDetail, ArtifactLoadState, ArtifactRef, DomainEvent, FileContent, FileManifestEntry, ProjectMessage, ProjectSnapshot, ProjectSummary, RunPresentation, VersionSummary } from "@/lib/contracts";
import { createDemoRunPresentation, demoFiles, demoProjectId, demoProjectSnapshot } from "@/lib/demo/library-project";
import { submitChatMessage } from "@/lib/chat/submit-message";
import { createRunPresentation, hydrateRunPresentationFromSnapshot, reduceDomainEvent } from "@/lib/events/reducer";
import { projectStatusLabel } from "@/lib/project-status";
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

export function artifactDetailKey(runId: string, artifactId: string): string {
  return `${runId}/${artifactId}`;
}

/**
 * Module-level immutable promise cache keyed by runId + artifactId so each
 * visible artifact detail is fetched exactly once across rerenders, React
 * StrictMode remounts and snapshot refreshes. Rejected promises stay cached:
 * failures are surfaced once and never auto-retried in this change.
 */
const artifactDetailPromises = new Map<string, Promise<ArtifactDetail>>();

export function clearArtifactDetailCache(): void {
  artifactDetailPromises.clear();
}

export function useArtifactDetailLoader(
  artifacts: ArtifactRef[],
  currentRunId?: string,
): Record<string, ArtifactLoadState> {
  const [loads, setLoads] = useState<Record<string, ArtifactLoadState>>({});
  const currentRunRef = useRef(currentRunId);
  currentRunRef.current = currentRunId;

  useEffect(() => {
    for (const ref of artifacts) {
      if (ref.kind !== "product_spec" && ref.kind !== "technical_spec") continue;
      const runId = ref.runId;
      if (!runId) continue;
      const key = artifactDetailKey(runId, ref.id);
      const promise = artifactDetailPromises.get(key) ?? controlPlane.getArtifact(runId, ref.id);
      artifactDetailPromises.set(key, promise);
      setLoads((previous) => (previous[ref.id] ? previous : { ...previous, [ref.id]: { status: "loading" } }));
      promise.then(
        (detail) => {
          if (currentRunRef.current !== runId) return;
          setLoads((previous) => ({ ...previous, [ref.id]: { status: "ready", detail } }));
        },
        (failure) => {
          if (currentRunRef.current !== runId) return;
          setLoads((previous) => ({
            ...previous,
            [ref.id]: { status: "error", message: failure instanceof Error ? failure.message : "Could not load this spec." },
          }));
        },
      );
    }
  }, [artifacts]);

  return loads;
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

function RunStatusBadge({ status }: { status: RunPresentation["status"] | "idle" }) {
  const color = status === "completed" ? "bg-emerald-500/10 text-emerald-700" : status === "failed" || status === "needs_attention" ? "bg-destructive/10 text-destructive" : status === "running" ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground";
  return <Badge className={cn("gap-1 rounded-full border-0 px-2 py-0.5 text-[11px]", color)} variant="secondary"><CircleDotIcon className={cn("size-3", status === "running" && "animate-pulse")} />{status.replaceAll("_", " ")}</Badge>;
}

function ProjectSidebar({ currentProjectId, currentProjectName, currentStatus, isDemo, projects, run }: { currentProjectId: string; currentProjectName: string; currentStatus: RunPresentation["status"] | "idle"; isDemo: boolean; projects: ProjectSummary[]; run: RunPresentation }) {
  return (
    <aside className="hidden min-h-0 flex-col border-r bg-card/75 lg:flex">
      <div className="flex h-14 items-center justify-between border-b px-4"><Link className="flex items-center gap-2 font-semibold tracking-tight" href="/"><span className="grid size-7 place-items-center rounded-md bg-slate-950 font-mono text-xs text-white">F</span>FOMO</Link><Button asChild size="icon-sm" title="Back to projects" variant="ghost"><Link href="/"><PanelLeftCloseIcon className="size-4" /></Link></Button></div>
      <div className="border-b p-3"><Button asChild className="w-full justify-start" size="sm" variant="secondary"><Link href="/"><LayoutPanelTopIcon className="mr-2 size-3.5" />New project</Link></Button></div>
      <div className="min-h-0 flex-1 overflow-auto p-3"><p className="mb-2 px-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">Projects</p><div className="space-y-1">{isDemo ? <Link className="block rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-sm" href={`/projects/${demoProjectId}`}><span className="block truncate font-medium">图书管理系统</span><span className="mt-1 block text-[11px] text-amber-800">explicit demo fixture</span></Link> : projects.map((project) => <Link className={cn("block rounded-lg px-3 py-2 text-sm transition-colors hover:bg-muted", project.id === currentProjectId && "bg-muted")} href={`/projects/${project.id}`} key={project.id}><span className="block truncate font-medium">{project.name}</span><span className="mt-1 block text-[11px] text-muted-foreground">{projectStatusLabel(project, project.id === currentProjectId && run.runId ? run.status : undefined)}</span></Link>)}</div></div>
      <div className="border-t p-3"><div className="rounded-xl border bg-background p-3"><div className="flex items-center gap-2"><span className="grid size-7 place-items-center rounded-md bg-muted"><GitBranchIcon className="size-3.5" /></span><div className="min-w-0"><p className="truncate text-xs font-medium">{currentProjectName}</p><p className="mt-0.5 text-[11px] text-muted-foreground">{run.runId ? `run ${run.runId.slice(0, 8)}` : "No run started"}</p></div></div><div className="mt-2"><RunStatusBadge status={currentStatus} /></div></div></div>
    </aside>
  );
}

function EmptyWorkspace({ onDemo }: { onDemo: () => void }) {
  return <main className="grid min-h-screen place-items-center p-6"><section className="max-w-md rounded-2xl border bg-card p-6 shadow-sm"><CircleAlertIcon className="size-5 text-amber-600" /><h1 className="mt-3 text-lg font-semibold">Control plane is unavailable</h1><p className="mt-2 text-sm leading-6 text-muted-foreground">The workbench defaults to the real FastAPI API and has not substituted fabricated data. Start the local service, then retry.</p><div className="mt-5 flex gap-2"><Button asChild variant="outline"><Link href="/"><ArrowLeftIcon className="mr-1.5 size-3.5" />Home</Link></Button><Button onClick={onDemo} variant="secondary">Open explicit demo</Button></div></section></main>;
}

export function ProjectWorkbench({ initialRunId, projectId }: { initialRunId?: string; projectId: string }) {
  const router = useRouter();
  const isDemo = projectId === demoProjectId;
  const { data: fetchedSnapshot, error: fetchError, isLoading, mutate: mutateProject } = useSWR(isDemo ? null : ["project", projectId], () => controlPlane.getProject(projectId), { revalidateOnFocus: false });
  const { data: projects = [] } = useSWR(isDemo ? null : "projects", controlPlane.getProjects, { revalidateOnFocus: false });
  const snapshot = isDemo ? demoProjectSnapshot : fetchedSnapshot;
  const activeRunId = snapshot?.activeRun?.id || initialRunId;
  const { data: fetchedVersions, mutate: mutateVersions } = useSWR(
    !isDemo && snapshot ? ["versions", projectId] : null,
    () => controlPlane.getVersions(projectId),
    { revalidateOnFocus: false },
  );
  const { data: fetchedTrace, mutate: mutateTrace } = useSWR(
    !isDemo && snapshot && activeRunId ? ["trace", projectId, activeRunId] : null,
    () => controlPlane.getTrace(projectId, activeRunId),
    { revalidateOnFocus: false },
  );
  const { data: fetchedPreview, mutate: mutatePreview } = useSWR(
    !isDemo && snapshot ? ["preview", projectId] : null,
    () => controlPlane.getPreview(projectId),
    { revalidateOnFocus: false },
  );
  const [presentation, setPresentation] = useState<RunPresentation>(() => isDemo ? createDemoRunPresentation() : createRunPresentation({ projectId }));
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

  const artifactLoads = useArtifactDetailLoader(isDemo ? [] : presentation.artifacts, presentation.runId || undefined);
  const specSlots = useMemo<SpecSlot[]>(
    () => specSlotsFromArtifacts(presentation.artifacts, artifactLoads),
    [artifactLoads, presentation.artifacts],
  );

  useEffect(() => {
    presentationRef.current = presentation;
    if (presentation.runId) setLastSeq(presentation.runId, presentation.lastSeq);
  }, [presentation, setLastSeq]);

  const transport = useMemo(() => new AgentEventTransport({
    getLastSeq: () => presentationRef.current.lastSeq,
    onConnectionChange: (connected, message) => setConnectionMessage(connected ? undefined : message),
    onEvent: (event) => {
      setPresentation((current) => reduceDomainEvent(current, event));
      if (isTerminalEvent(event)) {
        void Promise.all([mutateProject(), mutatePreview(), mutateTrace(), mutateVersions()]);
      }
    },
    onRunStarted: (runId) => setPresentation((current) => ({ ...createRunPresentation({ projectId, run: { id: runId, projectId, status: "running", lastSeq: 0 } }), versions: current.versions, trace: current.trace, preview: current.preview })),
    projectId,
  }), [mutatePreview, mutateProject, mutateTrace, mutateVersions, projectId]);

  const { clearError, error: chatError, messages, resumeStream, sendMessage, setMessages, status: chatStatus, stop } = useChat<AgentUIMessage>({
    id: `project-${projectId}`,
    transport,
    onError: (chatFailure) => setConnectionMessage(chatFailure.message),
  });

  useEffect(() => {
    if (!snapshot) return;
    const run = snapshot.activeRun;
    const artifactSignature = (snapshot.artifactRefs || []).map((ref) => `${ref.kind}:${ref.id}`).join(",");
    const signature = `${run?.id || initialRunId || "none"}:${snapshot.lastSeq}:${snapshot.messages.length}:${artifactSignature}`;
    if (hydratedSnapshotRef.current !== signature) {
      hydratedSnapshotRef.current = signature;
      setMessages(projectMessages(snapshot.messages, projectId));
      setPresentation((current) => {
        if (isDemo) return createDemoRunPresentation();
        const sameRun = Boolean(run?.id && current.runId === run.id);
        let next = sameRun
          ? current
          : hydrateRunPresentationFromSnapshot({
            events: snapshot.events,
            lastSeq: snapshot.lastSeq,
            preview: snapshot.preview,
            projectId,
            run,
            trace: snapshot.trace,
            versions: snapshot.versions,
            artifactRefs: snapshot.artifactRefs,
          });
        if (sameRun) {
          for (const event of [...snapshot.events].sort((left, right) => left.seq - right.seq)) next = reduceDomainEvent(next, event);
        }
        return {
          ...next,
          lastSeq: Math.max(next.lastSeq, snapshot.lastSeq, run?.lastSeq || 0),
          trace: snapshot.trace && snapshot.trace.length > 0 ? snapshot.trace : next.trace,
          versions: snapshot.versions && snapshot.versions.length > 0 ? snapshot.versions : next.versions,
          preview: snapshot.preview || next.preview,
          artifacts: snapshot.artifactRefs && snapshot.artifactRefs.length > 0 ? snapshot.artifactRefs : next.artifacts,
        };
      });
    }
  }, [initialRunId, isDemo, projectId, setMessages, snapshot]);

  useEffect(() => {
    if (isDemo || !snapshot || !(fetchedVersions || fetchedTrace || fetchedPreview)) return;
    setPresentation((current) => ({
      ...current,
      trace: fetchedTrace || current.trace,
      versions: fetchedVersions || current.versions,
      preview: fetchedPreview || current.preview,
    }));
  }, [fetchedPreview, fetchedTrace, fetchedVersions, isDemo, snapshot]);

  useEffect(() => {
    if (isDemo) return;
    const run = snapshot?.activeRun;
    const runId = run?.id || initialRunId;
    if (!runId) return;
    transport.hydrate(runId);
    if (run && isTerminal(run.status)) return;
    if (resumedRunsRef.current.has(runId)) return;
    resumedRunsRef.current.add(runId);
    void resumeStream();
  }, [initialRunId, isDemo, resumeStream, snapshot?.activeRun, transport]);

  const manifestKey = !isDemo && snapshot ? ["files", projectId, selectedVersionId || "current"] : null;
  const { data: fetchedFiles } = useSWR(manifestKey, () => controlPlane.getFiles(projectId, selectedVersionId), { revalidateOnFocus: false });
  const files: FileManifestEntry[] = isDemo ? demoProjectSnapshot.files || [] : fetchedFiles || snapshot?.files || [];

  useEffect(() => {
    if (!selectedFile && files[0]) setSelectedFile(files[0].path);
  }, [files, selectedFile, setSelectedFile]);

  const selectedManifest = files.find((file) => file.path === selectedFile);
  const fileKey = !isDemo && selectedTab === "code" && selectedFile ? ["file", projectId, selectedFile, selectedVersionId || "current"] : null;
  const { data: fetchedFile, mutate: mutateFile } = useSWR(fileKey, () => controlPlane.getFileContent(projectId, selectedFile || "", selectedVersionId), { revalidateOnFocus: false });
  const file: FileContent | undefined = isDemo && selectedFile && demoFiles[selectedFile]
    ? { path: selectedFile, ...demoFiles[selectedFile] }
    : fetchedFile || (selectedManifest && selectedTab === "code" ? { ...selectedManifest, content: "" } : undefined);

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
    if (isDemo) {
      setConnectionMessage("The demo fixture is read-only. Create a real project to run the agent.");
      return;
    }
    if (!text.trim()) return;
    setConnectionMessage(undefined);
    try {
      await submitChatMessage({ clearError, sendMessage, text });
    } catch (sendFailure) {
      setConnectionMessage(sendFailure instanceof Error ? sendFailure.message : "Could not start the run.");
    }
  }, [clearError, isDemo, sendMessage]);

  const stopRun = useCallback(async () => {
    const runId = presentationRef.current.runId;
    if (!runId || isDemo) return;
    try {
      await controlPlane.cancelRun(runId);
      stop();
      setPresentation((current) => ({ ...current, status: "cancelled" }));
    } catch (cancelFailure) {
      setConnectionMessage(cancelFailure instanceof Error ? cancelFailure.message : "Could not cancel the run.");
    }
  }, [isDemo, stop]);

  const reconnect = useCallback(async () => {
    if (isDemo) return;
    setConnectionMessage(undefined);
    try {
      await Promise.all([mutateProject(), mutateVersions(), mutateTrace(), mutatePreview()]);
      await resumeStream();
    } catch (reconnectFailure) {
      setConnectionMessage(reconnectFailure instanceof Error ? reconnectFailure.message : "Reconnect failed.");
    }
  }, [isDemo, mutatePreview, mutateProject, mutateTrace, mutateVersions, resumeStream]);

  const saveFile = useCallback(async (path: string, content: string, hash?: string) => {
    if (isDemo) return;
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
        ? "This file changed in a newer Agent version. Reload or select that version before saving; FOMO did not overwrite it."
        : saveFailure instanceof Error ? saveFailure.message : "Could not save the file.";
      setSaveError(message);
    } finally {
      setSaving(false);
    }
  }, [isDemo, mutateFile, mutateProject, mutateVersions, projectId, selectedVersionId, snapshot?.project.headVersionId]);

  const restoreVersion = useCallback(async (version: VersionSummary) => {
    if (isDemo) return;
    try {
      const restored = await controlPlane.restoreVersion(projectId, version.id);
      if (restored) setSelectedVersionId(restored.id);
      await Promise.all([mutateProject(), mutateVersions(), mutateTrace(), mutatePreview()]);
    } catch (restoreFailure) {
      setConnectionMessage(restoreFailure instanceof Error ? restoreFailure.message : "Could not restore the version.");
    }
  }, [isDemo, mutatePreview, mutateProject, mutateTrace, mutateVersions, projectId]);

  if (!isDemo && fetchError && !snapshot) return <EmptyWorkspace onDemo={() => router.push(`/projects/${demoProjectId}`)} />;
  if (!snapshot && isLoading) return <main className="grid min-h-screen place-items-center"><div className="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircleIcon className="size-4 animate-spin" />Hydrating project snapshot…</div></main>;
  if (!snapshot) return null;

  const currentProjectName = snapshot.project.name;
  const currentStatus = presentation.runId ? presentation.status : projectStatusLabel(snapshot.project);
  const connectionNotice = connectionMessage || chatError?.message;
  const downloadHref = isDemo
    ? undefined
    : controlPlaneUrl(`/projects/${encodeURIComponent(projectId)}/download${selectedVersionId ? `?versionId=${encodeURIComponent(selectedVersionId)}` : ""}`);
  return (
    <main className="flex min-h-screen flex-col bg-background">
      <div className="flex h-14 items-center justify-between border-b bg-card px-4 lg:hidden"><Link className="flex items-center gap-2 font-semibold" href="/"><span className="grid size-7 place-items-center rounded-md bg-slate-950 font-mono text-xs text-white">F</span>FOMO</Link><RunStatusBadge status={currentStatus} /></div>
      {isDemo ? <div className="border-b border-amber-500/25 bg-amber-500/5 px-4 py-2 text-center text-xs text-amber-900">Explicit demo fixture · all results are local sample data, not a real model, sandbox, or QA result.</div> : null}
      {connectionNotice ? <div className="flex items-center justify-between gap-3 border-b border-amber-500/25 bg-amber-500/5 px-4 py-2 text-xs text-amber-900"><span className="flex min-w-0 items-center gap-2"><CircleAlertIcon className="size-3.5 shrink-0" />{connectionNotice}</span><Button className="shrink-0" onClick={reconnect} size="sm" variant="outline"><RotateCcwIcon className="mr-1 size-3" />Reconnect</Button></div> : null}
      <div className="border-b bg-card px-3 py-2 lg:hidden"><div className="grid grid-cols-2 rounded-lg bg-muted p-1"><button className={cn("rounded-md px-3 py-1.5 text-xs font-medium", mobileSurface === "chat" && "bg-background shadow-sm")} onClick={() => setMobileSurface("chat")} type="button"><MessageSquareTextIcon className="mr-1 inline size-3.5" />Chat</button><button className={cn("rounded-md px-3 py-1.5 text-xs font-medium", mobileSurface === "workspace" && "bg-background shadow-sm")} onClick={() => setMobileSurface("workspace")} type="button"><FolderKanbanIcon className="mr-1 inline size-3.5" />Workspace</button></div></div>
      <div className="grid min-h-0 flex-1 lg:grid-cols-[16rem_minmax(22rem,0.9fr)_minmax(32rem,1.25fr)]">
        <ProjectSidebar currentProjectId={projectId} currentProjectName={currentProjectName} currentStatus={currentStatus} isDemo={isDemo} projects={projects} run={presentation} />
        <section className={cn("min-h-0 border-r bg-background lg:flex lg:flex-col", mobileSurface === "chat" ? "flex flex-col" : "hidden")} aria-label="Agent conversation">
          <header className="flex shrink-0 items-center justify-between border-b bg-card px-4 py-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{currentProjectName}</p><p className="mt-0.5 text-xs text-muted-foreground">Product → Architecture → Implementation → Proof</p></div><RunStatusBadge status={presentation.status} /></header>
          <Conversation className="min-h-0 flex-1"><ConversationContent className="gap-5 p-4">{visibleMessages.length > 0 ? visibleMessages.map((message) => <ReadableMessage key={message.id} message={message} />) : <div className="rounded-xl border border-dashed p-5 text-sm text-muted-foreground">Submit a request to start the first run.</div>}<RoleTimeline roles={presentation.roles} /><SpecToProof onFileSelect={selectFile} slots={specSlots} trace={presentation.trace} /></ConversationContent><ConversationScrollButton /></Conversation>
          <div className="shrink-0 border-t bg-card p-3">
            <PromptInput onSubmit={(message) => submitPrompt(message.text)}>
              <PromptInputTextarea disabled={isDemo} placeholder={isDemo ? "Demo fixture is read-only" : "Describe a change, bug fix, or next capability…"} />
              <PromptInputFooter>
                <PromptInputTools><span className="text-[11px] text-muted-foreground">SSE resumes from the last committed event</span></PromptInputTools>
                <PromptInputSubmit disabled={isDemo} onStop={chatStatus === "streaming" || chatStatus === "submitted" ? stopRun : undefined} status={isDemo ? "ready" : chatStatus} />
              </PromptInputFooter>
            </PromptInput>
          </div>
        </section>
        <div className={cn("min-h-0 p-3 lg:flex lg:min-h-0", mobileSurface === "workspace" ? "flex" : "hidden")}><div className="min-h-[34rem] min-w-0 flex-1 lg:min-h-0"><Workspace device={device} downloadHref={downloadHref} file={file} files={files} isDemo={isDemo} onDeviceChange={setDevice} onRestore={restoreVersion} onSave={saveFile} onSelectFile={selectFile} onVersionChange={setSelectedVersionId} presentation={presentation} saveError={saveError} saving={saving} selectedFile={selectedFile} selectedTab={selectedTab} selectedVersionId={selectedVersionId} setSelectedTab={setSelectedTab} /></div></div>
      </div>
    </main>
  );
}
