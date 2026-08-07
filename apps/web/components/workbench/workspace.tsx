"use client";

import {
  CheckCircle2Icon,
  ChevronRightIcon,
  CircleAlertIcon,
  Code2Icon,
  DownloadIcon,
  ExternalLinkIcon,
  FileCode2Icon,
  FileWarningIcon,
  HistoryIcon,
  MonitorIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  SmartphoneIcon,
  TerminalSquareIcon,
  TabletIcon,
} from "lucide-react";
import { startTransition, useEffect, useMemo, useState } from "react";

import {
  CodeBlock,
  CodeBlockActions,
  CodeBlockCopyButton,
  CodeBlockFilename,
  CodeBlockHeader,
  CodeBlockTitle,
} from "@/components/ai-elements/code-block";
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
import { LazyMonacoEditor } from "@/components/workbench/monaco-editor";
import type { DeviceViewport, WorkspaceTab } from "@/lib/store/workbench-store";
import type { FileContent, FileManifestEntry, PreviewRef, RunPresentation, VersionSummary } from "@/lib/contracts";
import { cn } from "@/lib/utils";

const tabs: Array<{ icon: typeof MonitorIcon; id: WorkspaceTab; label: string }> = [
  { id: "preview", label: "Preview", icon: MonitorIcon },
  { id: "code", label: "Code", icon: Code2Icon },
  { id: "terminal", label: "Terminal", icon: TerminalSquareIcon },
  { id: "problems", label: "Problems", icon: FileWarningIcon },
  { id: "versions", label: "Versions", icon: HistoryIcon },
];

const folderOrder = ["app", "components", "lib", "tests"];
const defaultFolders = new Set(folderOrder);

function sourceLanguage(file?: FileManifestEntry): string {
  if (file?.language) return file.language;
  const extension = file?.path.split(".").pop();
  return extension === "tsx" || extension === "jsx" ? "typescript" : extension === "ts" ? "typescript" : extension || "plaintext";
}

function previewUrl(preview?: PreviewRef): string | undefined {
  if (!(preview?.url && preview.origin && preview.status === "ready")) return undefined;
  try {
    const url = new URL(preview.url);
    const origin = new URL(preview.origin);
    const localhost = url.hostname === "localhost" || url.hostname === "127.0.0.1";
    if (url.origin !== origin.origin || !(url.protocol === "https:" || (localhost && url.protocol === "http:"))) return undefined;
    return url.toString();
  } catch {
    return undefined;
  }
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

function PreviewDemo() {
  return (
    <div className="grid min-h-[32rem] place-items-center bg-[linear-gradient(135deg,#f8fafc,#e7eefb)] p-7">
      <div className="w-full max-w-lg overflow-hidden rounded-2xl border bg-white shadow-xl">
        <div className="flex items-center justify-between border-b px-5 py-4"><div><p className="text-xs text-slate-500">Northstar Library</p><h3 className="text-lg font-semibold text-slate-900">图书目录</h3></div><span className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white">新增图书</span></div>
        <div className="p-5"><div className="h-9 rounded-lg border bg-slate-50 px-3 py-2 text-xs text-slate-400">搜索书名、作者或 ISBN…</div><div className="mt-4 overflow-hidden rounded-lg border"><div className="grid grid-cols-[1.4fr_1fr_.5fr] bg-slate-50 px-3 py-2 text-[10px] font-medium uppercase tracking-wider text-slate-500"><span>图书</span><span>作者</span><span>库存</span></div>{["人类简史", "百年孤独", "The Design of Everyday Things"].map((book, index) => <div className="grid grid-cols-[1.4fr_1fr_.5fr] border-t px-3 py-3 text-xs text-slate-700" key={book}><span className="font-medium">{book}</span><span>{["尤瓦尔·赫拉利", "加西亚·马尔克斯", "Don Norman"][index]}</span><span className="text-emerald-700">{[3, 1, 6][index]} 可借</span></div>)}</div></div>
      </div>
      <p className="mt-4 text-center text-xs text-slate-500">Demo fixture preview — not an OpenSandbox runtime.</p>
    </div>
  );
}

function PreviewPanel({ device, isDemo, onDeviceChange, preview }: { device: DeviceViewport; isDemo: boolean; onDeviceChange: (value: DeviceViewport) => void; preview?: PreviewRef }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const [logs, setLogs] = useState<Array<{ level: "log" | "warn" | "error"; message: string; timestamp: Date }>>([]);
  const url = previewUrl(preview);

  useEffect(() => {
    if (!(url && preview?.origin && preview.runId)) return;
    const expectedOrigin = preview.origin;
    const runId = preview.runId;
    const onMessage = (event: MessageEvent<unknown>) => {
      if (event.origin !== expectedOrigin || !event.data || typeof event.data !== "object") return;
      const message = event.data as Record<string, unknown>;
      const type = message.type;
      const eventRunId = message.runId;
      const level = message.level;
      if (eventRunId !== runId || !["preview.console", "preview.error", "preview.unhandledrejection"].includes(String(type))) return;
      const normalizedLevel: "log" | "warn" | "error" = level === "warn" ? "warn" : type === "preview.console" && level === "log" ? "log" : "error";
      const content = typeof message.message === "string" ? message.message : "Preview emitted an invalid event.";
      startTransition(() => setLogs((current) => [...current, { level: normalizedLevel, message: content, timestamp: new Date() }].slice(-40)));
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [preview?.origin, preview?.runId, url]);

  const viewportClass = device === "desktop" ? "w-full" : device === "tablet" ? "mx-auto max-w-[768px]" : "mx-auto max-w-[390px]";
  if (isDemo) {
    return <div className="h-full overflow-auto"><div className="border-b bg-amber-500/5 px-4 py-2 text-xs text-amber-800">Demo fixture: this is not a sandbox health check or a successful production preview.</div><PreviewDemo /></div>;
  }
  if (preview?.status === "reconnecting") {
    return <div className="grid h-full place-items-center p-8 text-center"><RefreshCwIcon className="size-5 animate-spin text-primary" /><p className="mt-3 text-sm font-medium">Reconnecting preview sandbox</p><p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">FOMO retains the last version and waits for the runtime to recover; it does not display an empty success state.</p></div>;
  }
  if (!url) {
    return <div className="grid h-full place-items-center p-8 text-center"><MonitorIcon className="size-6 text-muted-foreground" /><p className="mt-3 text-sm font-medium">Preview is not ready</p><p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">A verified OpenSandbox ingress URL appears here only after the worker reports it. {preview?.error || "No preview URL was received yet."}</p></div>;
  }
  return (
    <div className="flex h-full min-h-0 flex-col bg-muted/30">
      <div className="flex items-center justify-between border-b bg-card px-3 py-2"><div className="flex items-center gap-1">{([ ["desktop", MonitorIcon], ["tablet", TabletIcon], ["mobile", SmartphoneIcon] ] as const).map(([id, Icon]) => <button aria-label={`${id} viewport`} className={cn("grid size-7 place-items-center rounded-md", device === id ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")} key={id} onClick={() => onDeviceChange(id)} type="button"><Icon className="size-3.5" /></button>)}</div><div className="flex items-center gap-2"><Badge className="font-mono text-[10px]" variant="outline">isolated origin</Badge><Button onClick={() => window.open(url, "_blank", "noopener,noreferrer")} size="icon-sm" title="Open preview in new window" variant="ghost"><ExternalLinkIcon className="size-3.5" /></Button></div></div>
      <div className="min-h-0 flex-1 overflow-auto p-4"><div className={cn("h-full min-h-[32rem] overflow-hidden rounded-xl border bg-white shadow-sm transition-[max-width]", viewportClass)}><WebPreview defaultUrl={url} key={`${url}-${refreshKey}`}><WebPreviewNavigation><WebPreviewNavigationButton onClick={() => setRefreshKey((key) => key + 1)} tooltip="Reload preview"><RefreshCwIcon className="size-3.5" /></WebPreviewNavigationButton><WebPreviewUrl readOnly /><Button asChild size="icon-sm" variant="ghost"><a href={url} rel="noreferrer" target="_blank"><ExternalLinkIcon className="size-3.5" /></a></Button></WebPreviewNavigation><WebPreviewBody referrerPolicy="no-referrer" /><WebPreviewConsole logs={logs} /></WebPreview></div></div>
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

function CodePanel({ file, files, isDemo, onSave, onSelect, saveError, saving, selectedPath }: { file?: FileContent; files: FileManifestEntry[]; isDemo: boolean; onSave: (path: string, content: string, hash?: string) => void; onSelect: (path: string) => void; saveError?: string; saving: boolean; selectedPath?: string }) {
  const [draft, setDraft] = useState(file?.content || "");
  const [draftPath, setDraftPath] = useState(file?.path);
  if (file?.path !== draftPath) {
    setDraftPath(file?.path);
    setDraft(file?.content || "");
  }
  return (
    <div className="grid h-full min-h-0 grid-cols-[13rem_minmax(0,1fr)]">
      <aside className="min-h-0 overflow-auto border-r bg-card p-2"><p className="px-2 pb-2 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">Files</p><FileBrowser files={files} onSelect={onSelect} selectedPath={selectedPath} /></aside>
      <section className="flex min-w-0 flex-col bg-[#0d1117]">
        {file ? <><div className="flex items-center justify-between border-b border-white/10 px-3 py-2 text-xs text-slate-300"><span className="font-mono">{file.path}</span><div className="flex items-center gap-2">{isDemo ? <Badge className="border-amber-300/30 bg-amber-400/10 text-amber-200" variant="outline">read-only fixture</Badge> : <Button disabled={saving} onClick={() => onSave(file.path, draft, file.hash)} size="sm" variant="secondary">{saving ? "Saving…" : "Save"}</Button>}</div></div><div className="min-h-0 flex-1"><LazyMonacoEditor language={sourceLanguage(file)} onChange={setDraft} value={draft} /></div>{saveError ? <div className="border-t border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-red-200">{saveError}</div> : null}</> : <div className="grid h-full place-items-center p-8 text-center text-sm text-slate-400">Select a file to load its content on demand.</div>}
      </section>
    </div>
  );
}

function TerminalPanel({ presentation }: { presentation: RunPresentation }) {
  const output = presentation.commands.map((command) => `$ ${command.command}\n${command.output}${command.exitCode === undefined ? "" : `\nexit ${command.exitCode}`}\n`).join("\n");
  return <div className="h-full overflow-auto p-4">{presentation.commands.length > 0 ? <Terminal isStreaming={presentation.commands.some((command) => command.status === "running")} output={output} /> : <div className="grid h-full place-items-center rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">Commands will appear when the Engineer or Reviewer executes work in the sandbox.</div>}</div>;
}

function ProblemsPanel({ presentation, onSelectFile }: { presentation: RunPresentation; onSelectFile: (path: string) => void }) {
  const summary = useMemo(() => ({ passed: presentation.verifications.filter((item) => item.status === "passed").length, failed: presentation.verifications.filter((item) => item.status === "failed").length, skipped: presentation.verifications.filter((item) => item.status === "skipped").length, total: presentation.verifications.length, duration: presentation.verifications.reduce((total, item) => total + (item.duration || 0), 0) }), [presentation.verifications]);
  const suiteStatus = summary.failed > 0 ? "failed" : summary.total > 0 ? "passed" : "running";
  return (
    <div className="h-full overflow-auto p-4">
      {summary.total > 0 ? <TestResults summary={summary}><div className="flex items-center justify-between border-b px-4 py-3"><TestResultsSummary /><span className="font-mono text-xs text-muted-foreground">{summary.duration}ms</span></div><TestResultsContent><TestResultsProgress /><TestSuite defaultOpen name="Release gate" status={suiteStatus}><TestSuiteName><TestSuiteStats failed={summary.failed} passed={summary.passed} /></TestSuiteName><TestSuiteContent>{presentation.verifications.map((check) => <Test duration={check.duration} key={check.id} name={check.name} status={check.status} />)}</TestSuiteContent></TestSuite></TestResultsContent></TestResults> : <div className="rounded-xl border border-dashed p-5 text-sm text-muted-foreground">No QA result has been received. A run cannot be shown as successful until the verified gate is reported.</div>}
      <div className="mt-4 space-y-2">{presentation.problems.map((problem) => <StackTrace defaultOpen key={problem.id} onFilePathClick={(path) => onSelectFile(path)} trace={problem.stack || `${problem.title}\n    at ${problem.file || "unknown"}:${problem.line || 1}:1`}><StackTraceHeader><StackTraceError><StackTraceErrorType>{problem.severity}</StackTraceErrorType><StackTraceErrorMessage>{problem.title}</StackTraceErrorMessage></StackTraceError><StackTraceExpandButton /></StackTraceHeader><StackTraceContent><StackTraceFrames showInternalFrames={false} /></StackTraceContent></StackTrace>)}</div>
      {presentation.problems.length === 0 && summary.total > 0 ? <div className="mt-4 flex items-center gap-2 rounded-xl border border-emerald-500/20 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-800"><CheckCircle2Icon className="size-4" /> No blocking problem reported by Reviewer.</div> : null}
    </div>
  );
}

function VersionPanel({ isDemo, onRestore, selectedVersionId, versions }: { isDemo: boolean; onRestore: (version: VersionSummary) => void; selectedVersionId?: string; versions: VersionSummary[] }) {
  return (
    <div className="h-full overflow-auto p-4"><div className="mb-3 flex items-center justify-between"><p className="text-sm font-medium">Version history</p>{isDemo ? <Badge className="text-[10px]" variant="outline">fixture</Badge> : null}</div><div className="space-y-2">{versions.map((version) => <Commit className={cn(selectedVersionId === version.id && "ring-1 ring-primary/35")} defaultOpen={false} key={version.id}><CommitHeader><CommitInfo><CommitMessage>{version.message}</CommitMessage><CommitMetadata><CommitHash>{version.hash || version.id.slice(0, 7)}</CommitHash>{version.createdAt ? <CommitTimestamp date={new Date(version.createdAt)} /> : null}</CommitMetadata></CommitInfo><CommitAuthor><CommitAuthorAvatar initials="FM" /></CommitAuthor></CommitHeader><CommitContent><CommitFiles>{version.files?.map((file) => <CommitFile key={file.path}><CommitFileInfo><CommitFileStatus status={(file.status as "added" | "modified" | "deleted" | "renamed") || "modified"} /><CommitFileIcon /><CommitFilePath>{file.path}</CommitFilePath></CommitFileInfo><CommitFileChanges><CommitFileAdditions count={file.additions || 0} /><CommitFileDeletions count={file.deletions || 0} /></CommitFileChanges></CommitFile>) || <p className="px-2 py-1 text-xs text-muted-foreground">Version metadata available; file diff loads on demand.</p>}</CommitFiles><Button className="mt-3" disabled={isDemo} onClick={() => onRestore(version)} size="sm" variant="outline"><RotateCcwIcon className="mr-1.5 size-3.5" />{isDemo ? "Restore unavailable in fixture" : "Restore as new version"}</Button></CommitContent></Commit>)}</div>{versions.length === 0 ? <div className="rounded-xl border border-dashed p-5 text-sm text-muted-foreground">A successful Agent commit will be listed here.</div> : null}</div>
  );
}

export function Workspace({ device, downloadHref, file, files, isDemo, onDeviceChange, onRestore, onSave, onSelectFile, onVersionChange, presentation, saveError, saving, selectedFile, selectedTab, selectedVersionId, setSelectedTab }: { device: DeviceViewport; downloadHref?: string; file?: FileContent; files: FileManifestEntry[]; isDemo: boolean; onDeviceChange: (value: DeviceViewport) => void; onRestore: (version: VersionSummary) => void; onSave: (path: string, content: string, hash?: string) => void; onSelectFile: (path: string) => void; onVersionChange: (versionId?: string) => void; presentation: RunPresentation; saveError?: string; saving: boolean; selectedFile?: string; selectedTab: WorkspaceTab; selectedVersionId?: string; setSelectedTab: (tab: WorkspaceTab) => void }) {
  return (
    <section className="flex min-h-0 flex-col overflow-hidden rounded-2xl border bg-card shadow-sm" aria-label="Workspace">
      <nav className="flex shrink-0 items-center gap-1 overflow-x-auto border-b bg-card px-2" aria-label="Workspace tabs"><div className="flex">{tabs.map((tab) => { const Icon = tab.icon; return <button className={cn("inline-flex h-11 items-center gap-1.5 border-b-2 px-3 text-xs font-medium transition-colors", selectedTab === tab.id ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground")} key={tab.id} onClick={() => setSelectedTab(tab.id)} type="button"><Icon className="size-3.5" />{tab.label}</button>; })}</div><div className="ml-auto flex shrink-0 items-center gap-1.5 py-1"><label className="sr-only" htmlFor="workspace-version">Version</label><select className="h-7 max-w-32 rounded-md border bg-background px-2 text-[11px] outline-none focus:ring-2 focus:ring-primary/30" id="workspace-version" onChange={(event) => onVersionChange(event.target.value || undefined)} value={selectedVersionId || ""}><option value="">Current workspace</option>{presentation.versions.map((version) => <option key={version.id} value={version.id}>{version.hash?.slice(0, 7) || version.id.slice(0, 7)}</option>)}</select>{downloadHref ? <Button asChild size="icon-sm" title="Download selected source version" variant="ghost"><a href={downloadHref}><DownloadIcon className="size-3.5" /><span className="sr-only">Download source</span></a></Button> : null}</div></nav>
      <div className="min-h-0 flex-1">{selectedTab === "preview" ? <PreviewPanel device={device} isDemo={isDemo} onDeviceChange={onDeviceChange} preview={presentation.preview} /> : null}{selectedTab === "code" ? <CodePanel file={file} files={files} isDemo={isDemo} onSave={onSave} onSelect={onSelectFile} saveError={saveError} saving={saving} selectedPath={selectedFile} /> : null}{selectedTab === "terminal" ? <TerminalPanel presentation={presentation} /> : null}{selectedTab === "problems" ? <ProblemsPanel onSelectFile={onSelectFile} presentation={presentation} /> : null}{selectedTab === "versions" ? <VersionPanel isDemo={isDemo} onRestore={onRestore} selectedVersionId={selectedVersionId} versions={presentation.versions} /> : null}</div>
    </section>
  );
}

export function CodeFallback({ file }: { file?: FileContent }) {
  return file ? <CodeBlock code={file.content} language={sourceLanguage(file) as never}><CodeBlockHeader><CodeBlockTitle><CodeBlockFilename>{file.path}</CodeBlockFilename></CodeBlockTitle><CodeBlockActions><CodeBlockCopyButton /></CodeBlockActions></CodeBlockHeader></CodeBlock> : null;
}
