"use client";

import {
  CheckCircle2Icon,
  Code2Icon,
  DownloadIcon,
  EllipsisIcon,
  ExternalLinkIcon,
  FileCode2Icon,
  FileWarningIcon,
  HistoryIcon,
  LoaderCircleIcon,
  MonitorIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  SmartphoneIcon,
  TerminalSquareIcon,
  TabletIcon,
} from "lucide-react";
import { startTransition, type KeyboardEvent, useEffect, useMemo, useState } from "react";

import {
  Commit,
  CommitAuthor,
  CommitAuthorAvatar,
  CommitContent,
  CommitFile,
  CommitFileAdditions,
  CommitFileChanges,
  CommitFileDeletions,
  CommitFileIcon,
  CommitFileInfo,
  CommitFilePath,
  CommitFiles,
  CommitFileStatus,
  CommitHash,
  CommitHeader,
  CommitInfo,
  CommitMessage,
  CommitMetadata,
  CommitTimestamp,
} from "@/components/ai-elements/commit";
import { FileTree, FileTreeFile, FileTreeFolder } from "@/components/ai-elements/file-tree";
import {
  StackTrace,
  StackTraceContent,
  StackTraceError,
  StackTraceErrorMessage,
  StackTraceErrorType,
  StackTraceExpandButton,
  StackTraceFrames,
  StackTraceHeader,
} from "@/components/ai-elements/stack-trace";
import { Terminal } from "@/components/ai-elements/terminal";
import {
  Test,
  TestResults,
  TestResultsContent,
  TestResultsProgress,
  TestResultsSummary,
  TestSuite,
  TestSuiteContent,
  TestSuiteName,
  TestSuiteStats,
} from "@/components/ai-elements/test-results";
import {
  WebPreview,
  WebPreviewBody,
  WebPreviewConsole,
  WebPreviewNavigation,
  WebPreviewNavigationButton,
  WebPreviewUrl,
} from "@/components/ai-elements/web-preview";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { LazyMonacoEditor } from "@/components/workbench/monaco-editor";
import type { DeviceViewport, WorkspaceTab } from "@/lib/store/workbench-store";
import type { FileContent, FileManifestEntry, PreviewRef, RunPresentation, VersionSummary } from "@/lib/contracts";
import { validatePreviewUrl } from "@/lib/preview";
import { derivePreviewState, deriveRunState, type PreviewStateView, type RunStateView } from "@/lib/run-state";
import { cn } from "@/lib/utils";

const primaryTabs: Array<{ icon: typeof MonitorIcon; id: Exclude<WorkspaceTab, "problems">; label: string }> = [
  { id: "preview", label: "预览", icon: MonitorIcon },
  { id: "code", label: "代码", icon: Code2Icon },
  { id: "terminal", label: "终端", icon: TerminalSquareIcon },
  { id: "versions", label: "版本", icon: HistoryIcon },
];

const previewTone: Record<PreviewStateView["state"], string> = {
  blocked: "border-destructive/30 bg-destructive/10 text-destructive",
  building: "border-primary/25 bg-primary/10 text-primary",
  ready: "border-emerald-600/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  stale: "border-amber-500/25 bg-amber-500/10 text-amber-800 dark:text-amber-200",
  unavailable: "border-border bg-muted text-muted-foreground",
  updating: "border-primary/25 bg-primary/10 text-primary",
};

const deviceLabels: Record<DeviceViewport, string> = {
  desktop: "桌面视口",
  tablet: "平板视口",
  mobile: "手机视口",
};

const folderOrder = ["app", "components", "lib", "tests"];
const defaultFolders = new Set(folderOrder);

function sourceLanguage(file?: FileManifestEntry): string {
  if (file?.language) return file.language;
  const extension = file?.path.split(".").pop();
  return extension === "tsx" || extension === "jsx" ? "typescript" : extension === "ts" ? "typescript" : extension || "plaintext";
}

function groupFiles(files: FileManifestEntry[]): Array<{ name: string; files: FileManifestEntry[] }> {
  const groups = new Map<string, FileManifestEntry[]>();
  for (const file of files) {
    const root = file.path.split("/")[0] || "root";
    groups.set(root, [...(groups.get(root) || []), file]);
  }
  return [...groups.entries()]
    .sort(([left], [right]) => (folderOrder.indexOf(left) + 10).toString().localeCompare((folderOrder.indexOf(right) + 10).toString()) || left.localeCompare(right))
    .map(([name, entries]) => ({ name, files: entries }));
}

function PreviewPanel({ activeRunId, device, onDeviceChange, preview, previewState }: { activeRunId?: string; device: DeviceViewport; onDeviceChange: (value: DeviceViewport) => void; preview?: PreviewRef; previewState: PreviewStateView }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const [logs, setLogs] = useState<Array<{ level: "log" | "warn" | "error"; message: string; timestamp: Date }>>([]);
  const valid = useMemo(() => validatePreviewUrl(preview?.url), [preview?.url]);

  useEffect(() => {
    if (!(preview?.status === "ready" && preview?.runId && valid)) return;
    const expectedOrigin = valid.expectedOrigin;
    const runId = preview.runId;
    const onMessage = (event: MessageEvent<unknown>) => {
      if (event.origin !== expectedOrigin || !event.data || typeof event.data !== "object") return;
      const message = event.data as Record<string, unknown>;
      const type = message.type;
      const eventRunId = message.runId;
      const level = message.level;
      if (eventRunId !== runId || !["preview.console", "preview.error", "preview.unhandledrejection"].includes(String(type))) return;
      const normalizedLevel: "log" | "warn" | "error" = level === "warn" ? "warn" : type === "preview.console" && level === "log" ? "log" : "error";
      const content = typeof message.message === "string" ? message.message : "预览发出了无效事件。";
      startTransition(() => setLogs((current) => [...current, { level: normalizedLevel, message: content, timestamp: new Date() }].slice(-40)));
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [preview?.runId, preview?.status, valid]);

  const viewportClass = device === "desktop" ? "w-full" : device === "tablet" ? "mx-auto max-w-[768px]" : "mx-auto max-w-[390px]";
  const isPreviousVersion = preview?.status === "ready" && preview.runId !== activeRunId;
  if (preview?.status === "reconnecting") {
    return (
      <div className="grid h-full place-items-center p-6 text-center" data-preview-state={previewState.state}>
        <RefreshCwIcon aria-hidden="true" className="size-5 animate-spin text-primary" />
        <p className="mt-3 text-sm font-medium">正在更新预览</p>
        <p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">在准备最新改动期间，上一个已验证版本仍然可用。</p>
      </div>
    );
  }
  const target = preview?.status === "ready" && Boolean(preview.runId) ? valid : undefined;
  if (!target) {
    const unavailableReason = preview?.error || (previewState.state === "blocked"
      ? "最近一次运行在发布已验证预览前就停止了。"
      : "agent 仍在准备首个可预览的构建。");
    return (
      <div className="grid h-full place-items-center p-6 text-center" data-preview-state={previewState.state}>
        <MonitorIcon aria-hidden="true" className="size-6 text-muted-foreground" />
        <p className="mt-3 text-sm font-medium">等待预览</p>
        <p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">{unavailableReason}</p>
      </div>
    );
  }
  const url = target.href;
  return (
    <div className="flex h-full min-h-0 flex-col bg-muted/30" data-preview-state={previewState.state}>
      <div className="flex items-center justify-between border-b bg-card px-3 py-1.5">
        <div className="flex items-center gap-2">
          <div aria-label="预览设备" className="flex items-center gap-1" role="group">
            {([ ["desktop", MonitorIcon], ["tablet", TabletIcon], ["mobile", SmartphoneIcon] ] as const).map(([id, Icon]) => (
              <button
                aria-label={deviceLabels[id]}
                aria-pressed={device === id}
                className={cn(
                  "grid size-7 place-items-center rounded-md transition-colors",
                  device === id ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted",
                )}
                key={id}
                onClick={() => onDeviceChange(id)}
                type="button"
              >
                <Icon aria-hidden="true" className="size-3.5" />
              </button>
            ))}
          </div>
          {isPreviousVersion ? (
            <span className="text-[11px] font-medium text-amber-700 dark:text-amber-300">旧版本 · 更新中</span>
          ) : null}
        </div>
        <div className="flex items-center gap-1.5">
          <Badge className={cn("font-mono text-[10px]", previewTone[previewState.state])} variant="outline">
            {previewState.label}
          </Badge>
          {preview?.verificationStatus === "verified" ? (
            <Badge className="hidden border-emerald-600/25 bg-emerald-500/5 font-mono text-[10px] text-emerald-800 dark:text-emerald-300 sm:inline-flex" variant="outline">
              已验证
            </Badge>
          ) : null}
          <Button
            aria-label="在新窗口打开预览"
            onClick={() => window.open(url, "_blank", "noopener,noreferrer")}
            size="icon-sm"
            title="在新窗口打开预览"
            variant="ghost"
          >
            <ExternalLinkIcon aria-hidden="true" className="size-3.5" />
          </Button>
        </div>
      </div>
      <div className="min-h-0 flex-1 p-2">
        <div className={cn("h-full min-h-0 overflow-hidden rounded-lg border bg-background shadow-sm transition-[max-width]", viewportClass)}>
          <WebPreview defaultUrl={url} key={`${url}-${refreshKey}`}>
            <WebPreviewNavigation>
              <WebPreviewNavigationButton onClick={() => setRefreshKey((key) => key + 1)} tooltip="重新加载预览">
                <RefreshCwIcon aria-hidden="true" className="size-3.5" />
              </WebPreviewNavigationButton>
              <WebPreviewUrl readOnly />
              <Button asChild size="icon-sm" variant="ghost">
                <a aria-label="在新标签页打开预览" href={url} rel="noreferrer" target="_blank">
                  <ExternalLinkIcon aria-hidden="true" className="size-3.5" />
                </a>
              </Button>
            </WebPreviewNavigation>
            <WebPreviewBody referrerPolicy="no-referrer" />
            <WebPreviewConsole logs={logs} />
          </WebPreview>
        </div>
      </div>
    </div>
  );
}

function FileBrowser({ files, onSelect, selectedPath }: { files: FileManifestEntry[]; onSelect: (path: string) => void; selectedPath?: string }) {
  return (
    <FileTree defaultExpanded={defaultFolders} onSelect={onSelect} selectedPath={selectedPath}>
      {groupFiles(files).map((group) => <FileTreeFolder key={group.name} name={group.name} path={group.name}>{group.files.map((file) => <FileTreeFile icon={<FileCode2Icon className="size-3.5 text-sky-600" />} key={file.path} name={file.path.slice(group.name.length + 1) || file.path} path={file.path} />)}</FileTreeFolder>)}
    </FileTree>
  );
}

function CodePanel({ file, files, onSave, onSelect, saveError, saving, selectedPath }: { file?: FileContent; files: FileManifestEntry[]; onSave: (path: string, content: string, hash?: string) => void; onSelect: (path: string) => void; saveError?: string; saving: boolean; selectedPath?: string }) {
  const activeFile = file?.path === selectedPath ? file : undefined;
  const [draft, setDraft] = useState(activeFile?.content || "");
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setDraft(activeFile?.content || "");
    setDirty(false);
  }, [activeFile?.content, activeFile?.hash, activeFile?.path]);

  return (
    <div className="grid h-full min-h-0 grid-cols-[9.5rem_minmax(0,1fr)] sm:grid-cols-[13rem_minmax(0,1fr)]">
      <aside className="min-h-0 overflow-auto border-r bg-card p-2"><p className="px-2 pb-2 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">文件</p><FileBrowser files={files} onSelect={onSelect} selectedPath={selectedPath} /></aside>
      <section className="flex min-w-0 flex-col bg-[#0d1117]">
        {activeFile ? <><div className="flex items-center justify-between border-b border-white/10 px-3 py-2 text-xs text-slate-300"><span className="truncate font-mono">{activeFile.path}</span><Button disabled={saving || !dirty} onClick={() => onSave(activeFile.path, draft, activeFile.hash)} size="sm" variant="secondary">{saving ? "保存中…" : dirty ? "保存" : "已保存"}</Button></div><div className="min-h-0 flex-1"><LazyMonacoEditor language={sourceLanguage(activeFile)} onChange={(value) => { setDraft(value); setDirty(value !== activeFile.content); }} value={draft} /></div>{saveError ? <div className="border-t border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-red-200">{saveError}</div> : null}</> : selectedPath ? <div className="grid h-full place-items-center p-8 text-center text-sm text-slate-400"><span className="flex items-center gap-2"><LoaderCircleIcon className="size-4 animate-spin" />正在加载文件……</span></div> : <div className="grid h-full place-items-center p-8 text-center text-sm text-slate-400">选择一个文件以按需加载其内容。</div>}
      </section>
    </div>
  );
}

function TerminalPanel({ presentation }: { presentation: RunPresentation }) {
  const output = presentation.commands.map((command) => `$ ${command.command}\n${command.output}${command.exitCode === undefined ? "" : `\nexit ${command.exitCode}`}\n`).join("\n");
  return <div className="h-full overflow-auto p-4">{presentation.commands.length > 0 ? <Terminal isStreaming={presentation.commands.some((command) => command.status === "running")} output={output} /> : <div className="grid h-full place-items-center rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">agent 在准备和检查你的应用时，构建活动会显示在这里。</div>}</div>;
}

function ProblemsPanel({ presentation, onSelectFile }: { presentation: RunPresentation; onSelectFile: (path: string) => void }) {
  const summary = useMemo(() => ({ passed: presentation.verifications.filter((item) => item.status === "passed").length, failed: presentation.verifications.filter((item) => item.status === "failed").length, skipped: presentation.verifications.filter((item) => item.status === "skipped").length, total: presentation.verifications.length, duration: presentation.verifications.reduce((total, item) => total + (item.duration || 0), 0) }), [presentation.verifications]);
  const suiteStatus = summary.failed > 0 ? "failed" : summary.total > 0 ? "passed" : "running";
  return (
    <div className="h-full overflow-auto p-4">
      {summary.total > 0 ? <TestResults summary={summary}><div className="flex items-center justify-between border-b px-4 py-3"><TestResultsSummary /><span className="font-mono text-xs text-muted-foreground">{summary.duration}ms</span></div><TestResultsContent><TestResultsProgress /><TestSuite defaultOpen name="发布门禁" status={suiteStatus}><TestSuiteName><TestSuiteStats failed={summary.failed} passed={summary.passed} /></TestSuiteName><TestSuiteContent>{presentation.verifications.map((check) => <Test duration={check.duration} key={check.id} name={check.name} status={check.status} />)}</TestSuiteContent></TestSuite></TestResultsContent></TestResults> : <div className="rounded-xl border border-dashed p-5 text-sm text-muted-foreground">agent 审查你的应用后，质量检查结果会显示在这里。</div>}
      <div className="mt-4 space-y-2">{presentation.problems.map((problem) => <StackTrace defaultOpen key={problem.id} onFilePathClick={(path) => onSelectFile(path)} trace={problem.stack || `${problem.title}\n    at ${problem.file || "unknown"}:${problem.line || 1}:1`}><StackTraceHeader><StackTraceError><StackTraceErrorType>{problem.severity}</StackTraceErrorType><StackTraceErrorMessage>{problem.title}</StackTraceErrorMessage></StackTraceError><StackTraceExpandButton /></StackTraceHeader><StackTraceContent><StackTraceFrames showInternalFrames={false} /></StackTraceContent></StackTrace>)}</div>
      {presentation.problems.length === 0 && summary.total > 0 ? (
        <div className="mt-4 flex items-center gap-2 rounded-xl border border-emerald-500/20 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-800 dark:text-emerald-200">
          <CheckCircle2Icon aria-hidden="true" className="size-4" />
          没有剩余的阻塞性确定性门禁。
        </div>
      ) : null}
    </div>
  );
}

function VersionPanel({ onRestore, selectedVersionId, versions }: { onRestore: (version: VersionSummary) => void; selectedVersionId?: string; versions: VersionSummary[] }) {
  return (
    <div className="h-full overflow-auto p-4"><div className="mb-3 flex items-center justify-between"><p className="text-sm font-medium">版本历史</p></div><div className="space-y-2">{versions.map((version) => <Commit className={cn(selectedVersionId === version.id && "ring-1 ring-primary/35")} defaultOpen={false} key={version.id}><CommitHeader><CommitInfo><CommitMessage>{version.message}</CommitMessage><CommitMetadata><CommitHash>{version.hash || version.id.slice(0, 7)}</CommitHash>{version.createdAt ? <CommitTimestamp date={new Date(version.createdAt)} /> : null}</CommitMetadata></CommitInfo><CommitAuthor><CommitAuthorAvatar initials="FM" /></CommitAuthor></CommitHeader><CommitContent><CommitFiles>{version.files?.map((file) => <CommitFile key={file.path}><CommitFileInfo><CommitFileStatus status={(file.status as "added" | "modified" | "deleted" | "renamed") || "modified"} /><CommitFileIcon /><CommitFilePath>{file.path}</CommitFilePath></CommitFileInfo><CommitFileChanges><CommitFileAdditions count={file.additions || 0} /><CommitFileDeletions count={file.deletions || 0} /></CommitFileChanges></CommitFile>) || <p className="px-2 py-1 text-xs text-muted-foreground">版本元数据可用；文件差异按需加载。</p>}</CommitFiles><Button className="mt-3" onClick={() => onRestore(version)} size="sm" variant="outline"><RotateCcwIcon className="mr-1.5 size-3.5" />恢复为新版本</Button></CommitContent></Commit>)}</div>{versions.length === 0 ? <div className="rounded-xl border border-dashed p-5 text-sm text-muted-foreground">一次成功的 Agent 提交将列在这里。</div> : null}</div>
  );
}

export function Workspace({ device, downloadHref, file, files, onDeviceChange, onRestore, onSave, onSelectFile, onVersionChange, presentation, previewState, runState, saveError, saving, selectedFile, selectedTab, selectedVersionId, setSelectedTab }: { device: DeviceViewport; downloadHref?: string; file?: FileContent; files: FileManifestEntry[]; onDeviceChange: (value: DeviceViewport) => void; onRestore: (version: VersionSummary) => void; onSave: (path: string, content: string, hash?: string) => void; onSelectFile: (path: string) => void; onVersionChange: (versionId?: string) => void; presentation: RunPresentation; previewState?: PreviewStateView; runState?: RunStateView; saveError?: string; saving: boolean; selectedFile?: string; selectedTab: WorkspaceTab; selectedVersionId?: string; setSelectedTab: (tab: WorkspaceTab) => void }) {
  // The shell normally passes explicit run/preview views; fall back to deriving
  // them from the presentation so the component stays usable on its own.
  const runStateView = runState ?? deriveRunState({
    hasRun: Boolean(presentation.runId),
    isWaitingForUser: presentation.inputRequests.some((request) => request.status === "pending"),
    status: presentation.runId ? presentation.status : undefined,
  });
  const previewValidation = presentation.preview ? validatePreviewUrl(presentation.preview.url) : undefined;
  const previewStateView = previewState ?? derivePreviewState({
    activeRunId: presentation.runId,
    hasValidUrl: Boolean(previewValidation),
    preview: presentation.preview,
    run: runStateView,
  });
  const problemsCount = presentation.problems.length + presentation.verifications.filter((verification) => verification.status === "failed").length;
  const selectedPrimaryIndex = primaryTabs.findIndex((tab) => tab.id === selectedTab);
  const handlePrimaryTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | undefined;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % primaryTabs.length;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + primaryTabs.length) % primaryTabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = primaryTabs.length - 1;
    if (nextIndex === undefined) return;
    event.preventDefault();
    const nextTab = primaryTabs[nextIndex];
    setSelectedTab(nextTab.id);
    document.getElementById(`workspace-tab-${nextTab.id}`)?.focus();
  };
  return (
    <section className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border bg-card shadow-sm" aria-label="工作区" data-run-state={runStateView.state}>
      <nav className="flex shrink-0 items-center gap-1 overflow-x-auto border-b bg-card px-2" aria-label="工作区标签页">
        <div aria-label="主要工作区视图" className="flex" role="tablist">
          {primaryTabs.map((tab, index) => {
            const Icon = tab.icon;
            const selected = selectedTab === tab.id;
            const isFallbackTabStop = selectedPrimaryIndex === -1 && index === 0;
            return (
              <button
                aria-controls="workspace-panel"
                aria-selected={selected}
                className={cn(
                  "inline-flex h-10 items-center gap-1.5 border-b-2 px-3 text-xs font-medium transition-colors",
                  selected ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground",
                )}
                id={`workspace-tab-${tab.id}`}
                key={tab.id}
                onClick={() => setSelectedTab(tab.id)}
                onKeyDown={(event) => handlePrimaryTabKeyDown(event, index)}
                role="tab"
                tabIndex={selected || isFallbackTabStop ? 0 : -1}
                type="button"
              >
                <Icon aria-hidden="true" className="size-3.5" />
                {tab.label}
              </button>
            );
          })}
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              aria-label="更多工作区视图"
              className={cn("h-8 gap-1 px-2 text-xs", selectedTab === "problems" && "bg-muted text-foreground")}
              id="workspace-tab-more"
              variant="ghost"
            >
              <EllipsisIcon aria-hidden="true" className="size-3.5" />
              <span className="hidden md:inline">更多</span>
              {problemsCount > 0 ? <span className="rounded-full bg-destructive/15 px-1.5 text-[9px] font-semibold text-destructive">{problemsCount}</span> : null}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-44">
            <DropdownMenuItem onSelect={() => setSelectedTab("problems")}>
              <FileWarningIcon aria-hidden="true" className="size-3.5" />
              问题
              {problemsCount > 0 ? <span className="ml-auto text-xs text-destructive">{problemsCount}</span> : null}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <div className="ml-auto flex shrink-0 items-center gap-1.5 py-1">
          <label className="sr-only" htmlFor="workspace-version">版本</label>
          <select
            className="h-7 max-w-32 rounded-md border bg-background px-2 text-[11px] outline-none focus:ring-2 focus:ring-primary/30"
            id="workspace-version"
            onChange={(event) => onVersionChange(event.target.value || undefined)}
            value={selectedVersionId || ""}
          >
            <option value="">当前工作版本</option>
            {presentation.versions.map((version) => (
              <option key={version.id} value={version.id}>{version.hash?.slice(0, 7) || version.id.slice(0, 7)}</option>
            ))}
          </select>
          {downloadHref ? (
            <Button asChild size="icon-sm" title="下载源码" variant="ghost">
              <a href={downloadHref}><DownloadIcon aria-hidden="true" className="size-3.5" /><span className="sr-only">下载源码</span></a>
            </Button>
          ) : null}
        </div>
      </nav>
      <div aria-labelledby={selectedTab === "problems" ? "workspace-tab-more" : `workspace-tab-${selectedTab}`} className="min-h-0 flex-1" id="workspace-panel" role="tabpanel">{selectedTab === "preview" ? <PreviewPanel activeRunId={presentation.runId} device={device} onDeviceChange={onDeviceChange} preview={presentation.preview} previewState={previewStateView} /> : null}{selectedTab === "code" ? <CodePanel file={file} files={files} onSave={onSave} onSelect={onSelectFile} saveError={saveError} saving={saving} selectedPath={selectedFile} /> : null}{selectedTab === "terminal" ? <TerminalPanel presentation={presentation} /> : null}{selectedTab === "problems" ? <ProblemsPanel onSelectFile={onSelectFile} presentation={presentation} /> : null}{selectedTab === "versions" ? <VersionPanel onRestore={onRestore} selectedVersionId={selectedVersionId} versions={presentation.versions} /> : null}</div>
    </section>
  );
}
