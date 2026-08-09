// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, StrictMode } from "react";
import type { HTMLAttributes, ReactNode } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  ProjectWorkbench,
  clearArtifactDetailCache,
  useArtifactDetailLoader,
} from "@/components/workbench/project-workbench";
import { ApiProblem } from "@/lib/api/client";
import { useWorkbenchStore } from "@/lib/store/workbench-store";
import type { ArtifactDetail, ArtifactKind, ArtifactRef, VisibleArtifactRef } from "@/lib/contracts";

const h = vi.hoisted(() => ({
  answerRunInputRequest: vi.fn(),
  cancelRun: vi.fn(),
  getArtifact: vi.fn(),
  mutate: vi.fn(),
  emptyArray: Object.freeze([]),
  emptyObject: Object.freeze({}),
  snapshot: { project: undefined as unknown },
  chat: {
    clearError: vi.fn(),
    error: undefined,
    messages: Object.freeze([]),
    resumeStream: vi.fn(),
    sendMessage: vi.fn(),
    setMessages: vi.fn(),
    status: "ready",
    stop: vi.fn(),
  },
}));

vi.mock("@/lib/api/client", () => ({
  ApiProblem: class ApiProblem extends Error {
    readonly status: number;
    readonly title: string;

    constructor(input: { status: number; title: string; detail?: string }) {
      super(input.detail || input.title);
      this.name = "ApiProblem";
      this.status = input.status;
      this.title = input.title;
    }
  },
  controlPlane: {
    answerRunInputRequest: h.answerRunInputRequest,
    cancelRun: h.cancelRun,
    getArtifact: h.getArtifact,
  },
  controlPlaneUrl: (path: string) => path,
}));

vi.mock("swr", () => ({
  default: (key: unknown) => {
    const first = Array.isArray(key) ? key[0] : key;
    if (first === "project") {
      return { data: h.snapshot.project, error: undefined, isLoading: false, mutate: h.mutate };
    }
    if (first === "projects") return { data: h.emptyArray, error: undefined, isLoading: false, mutate: h.mutate };
    if (first === "versions") return { data: h.emptyArray, error: undefined, isLoading: false, mutate: h.mutate };
    if (first === "trace") return { data: h.emptyArray, error: undefined, isLoading: false, mutate: h.mutate };
    if (first === "preview") return { data: undefined, error: undefined, isLoading: false, mutate: h.mutate };
    if (first === "files") return { data: h.emptyArray, error: undefined, isLoading: false, mutate: h.mutate };
    if (first === "file") return { data: undefined, error: undefined, isLoading: false, mutate: h.mutate };
    return { data: undefined, error: undefined, isLoading: false, mutate: h.mutate };
  },
}));

vi.mock("@ai-sdk/react", () => ({ useChat: () => h.chat }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("next/link", () => ({
  default: ({ children }: { children?: ReactNode }) => createElement("a", null, children),
}));
vi.mock("@/components/workbench/monaco-editor", () => ({ LazyMonacoEditor: () => null }));
vi.mock("@/components/ai-elements/conversation", () => ({
  Conversation: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
  ConversationContent: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
  ConversationScrollButton: () => null,
}));
vi.mock("@/components/ai-elements/message", () => ({
  Message: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
  MessageContent: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
  MessageResponse: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
}));
vi.mock("@/components/ai-elements/plan", () => ({
  Plan: ({ children }: { children?: ReactNode }) => createElement("section", null, children),
  PlanContent: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
  PlanDescription: ({ children }: { children?: ReactNode }) => createElement("p", null, children),
  PlanHeader: ({ children }: { children?: ReactNode }) => createElement("header", null, children),
  PlanTitle: ({ children }: { children?: ReactNode }) => createElement("h3", null, children),
  PlanTrigger: () => createElement("button"),
}));
vi.mock("@/components/ai-elements/prompt-input", () => ({
  PromptInput: ({ children }: { children?: ReactNode }) => createElement("form", null, children),
  PromptInputFooter: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
  PromptInputSubmit: ({ disabled, onStop, status }: { disabled?: boolean; onStop?: () => void; status?: string }) => createElement("button", {
    "aria-label": status === "streaming" || status === "submitted" ? "Stop" : "Submit",
    disabled,
    onClick: onStop,
    type: "button",
  }),
  PromptInputTextarea: ({ disabled, placeholder }: { disabled?: boolean; placeholder?: string }) => createElement("textarea", {
    "aria-label": "Project prompt",
    disabled,
    placeholder,
  }),
  PromptInputTools: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
}));

if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

afterEach(cleanup);

beforeEach(() => {
  useWorkbenchStore.setState({
    device: "desktop",
    lastSeqByRun: {},
    selectedFile: undefined,
    selectedTab: "preview",
  });
  vi.clearAllMocks();
  h.answerRunInputRequest.mockReset();
  h.cancelRun.mockReset().mockResolvedValue(undefined);
  h.mutate.mockReset().mockResolvedValue(undefined);
  h.chat.status = "ready";
  clearArtifactDetailCache();
});

function refFixture(id: string, kind: ArtifactKind, runId = "run-1"): VisibleArtifactRef {
  const ownership = {
    run_input: ["user", "input"],
    build_plan: ["pi", "planning"],
    acceptance_contract: ["fomo", "acceptance"],
    diagnostic_report: ["fomo", "verification"],
    product_spec: ["product_manager", "product"],
    technical_spec: ["architect", "architecture"],
  } as const;
  const [role, stage] = ownership[kind];
  return {
    id,
    runId,
    kind,
    role,
    stage,
    schemaVersion: 1,
    title: `${kind.replaceAll("_", " ")} ${id}`,
    summary: `Summary ${id}`,
    createdAt: "2026-08-07T12:00:00.000Z",
  };
}

function detailFixture(ref: VisibleArtifactRef, problem: string): ArtifactDetail {
  return { ...ref, content: { problem } };
}

function snapshotFixture(refs: ArtifactRef[], lastSeq = 12): Record<string, unknown> {
  return {
    goalGraph: null,
    project: { id: "project-1", name: "Library", status: "idle" },
    activeRun: { id: "run-1", projectId: "project-1", status: "completed", lastSeq },
    lastSeq,
    messages: [],
    events: [],
    files: [],
    versions: [],
    trace: [],
    preview: { status: "unavailable" },
    artifactRefs: refs,
  };
}

function waitingSnapshotFixture(): Record<string, unknown> {
  const pendingInputRequest = {
    id: "input-1",
    runId: "run-1",
    question: "Which catalogue layout should we use?",
    choices: ["Grid", "List"],
    allowFreeform: false,
    status: "pending",
    stage: "building",
    createdAt: "2026-08-09T10:00:00.000Z",
  };
  return {
    ...snapshotFixture([], 8),
    activeRun: {
      id: "run-1",
      projectId: "project-1",
      status: "waiting_for_user",
      lastSeq: 8,
      pendingInputRequest,
    },
    pendingInputRequest,
  };
}

function LoaderHarness({ refs, runId }: { refs: ArtifactRef[]; runId?: string }) {
  const loads = useArtifactDetailLoader(refs, runId);
  return createElement("pre", { "data-testid": "loads" } as HTMLAttributes<HTMLPreElement>, JSON.stringify(loads));
}

function loadsFromDom(): Record<string, unknown> {
  return JSON.parse(screen.getByTestId("loads").textContent || "{}");
}

describe("useArtifactDetailLoader", () => {
  it("fetches each ref exactly once across rerenders with fresh ref arrays", async () => {
    const refA = refFixture("product-1", "product_spec", "run-a");
    h.getArtifact.mockResolvedValue(detailFixture(refA, "readers can search"));

    const { rerender } = render(createElement(LoaderHarness, { refs: [refA], runId: "run-a" }));
    rerender(createElement(LoaderHarness, { refs: [{ ...refA }], runId: "run-a" }));

    await waitFor(() => {
      expect(loadsFromDom()["product-1"]).toEqual({ status: "ready", detail: expect.anything() });
    });
    expect(h.getArtifact).toHaveBeenCalledTimes(1);
  });

  it("loads exactly once per ref under StrictMode double effects", async () => {
    const refA = refFixture("product-1", "product_spec", "run-a");
    const refB = refFixture("technical-1", "technical_spec", "run-a");
    h.getArtifact.mockResolvedValue(detailFixture(refA, "readers can search"));

    render(createElement(StrictMode, null, createElement(LoaderHarness, { refs: [refA, refB], runId: "run-a" })));

    await waitFor(() => expect(h.getArtifact).toHaveBeenCalledTimes(2));
  });

  it("skips hidden kinds and refs without a run id without fetching", () => {
    render(createElement(LoaderHarness, {
      refs: [
        { ...refFixture("hidden-1", "product_spec"), kind: "implementation_plan" },
        { ...refFixture("norun-1", "product_spec"), runId: undefined },
      ],
      runId: "run-a",
    }));

    expect(h.getArtifact).not.toHaveBeenCalled();
    expect(loadsFromDom()).toEqual({});
  });

  it("never applies a late response from a previous run to the current run", async () => {
    const refA = refFixture("product-1", "product_spec", "run-a");
    const refB = refFixture("product-1", "product_spec", "run-b");
    let resolveOld: ((detail: ArtifactDetail) => void) | undefined;
    h.getArtifact.mockImplementation((runId: string, artifactId: string) => {
      if (runId === "run-a") {
        return new Promise<ArtifactDetail>((resolve) => {
          resolveOld = resolve;
        });
      }
      return Promise.resolve(detailFixture(refFixture(artifactId, "product_spec", runId), "run-b content"));
    });

    const { rerender } = render(createElement(LoaderHarness, { refs: [refA], runId: "run-a" }));
    expect(h.getArtifact).toHaveBeenCalledTimes(1);

    rerender(createElement(LoaderHarness, { refs: [refB], runId: "run-b" }));
    await waitFor(() => {
      expect(loadsFromDom()["product-1"]).toEqual({
        status: "ready",
        detail: expect.objectContaining({ runId: "run-b" }),
      });
    });

    // The stale run-a response resolves after the run switch and must be dropped.
    await act(async () => {
      resolveOld?.(detailFixture(refA, "stale run-a content"));
    });

    expect(loadsFromDom()["product-1"]).toEqual({
      status: "ready",
      detail: expect.objectContaining({ runId: "run-b", content: { problem: "run-b content" } }),
    });
    expect(JSON.stringify(loadsFromDom())).not.toContain("stale");
  });

  it("surfaces failures explicitly and never retries on rerender", async () => {
    const refA = refFixture("product-1", "product_spec", "run-a");
    h.getArtifact.mockRejectedValue(new Error("artifact fetch failed"));

    const { rerender } = render(createElement(LoaderHarness, { refs: [refA], runId: "run-a" }));
    await waitFor(() => {
      expect(loadsFromDom()["product-1"]).toEqual({ status: "error", message: "artifact fetch failed" });
    });

    rerender(createElement(LoaderHarness, { refs: [{ ...refA }], runId: "run-a" }));
    expect(h.getArtifact).toHaveBeenCalledTimes(1);
  });
});

describe("ProjectWorkbench runtime overview", () => {
  it("shows the bounded run overview without contract proof or artifact detail loading", () => {
    h.snapshot.project = snapshotFixture([]);

    render(createElement(ProjectWorkbench, { projectId: "project-1" }));

    const workLog = screen.getByLabelText("Work log");
    const activity = screen.getByRole("region", { name: "Agent activity" });
    const taskSummary = screen.getByRole("region", { name: "Current task" });
    const composer = screen.getByRole("textbox");
    expect(screen.getByRole("region", { name: "Agent work log" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Run stages" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Run metrics" })).toBeTruthy();
    expect(workLog.contains(activity)).toBe(true);
    expect(workLog.contains(taskSummary)).toBe(false);
    expect(workLog.compareDocumentPosition(taskSummary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(taskSummary.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("region", { name: "Workspace" })).toBeTruthy();
    expect(screen.queryByText("Contract-to-Proof")).toBeNull();
    expect(h.getArtifact).not.toHaveBeenCalled();
  });

  it("keeps spec artifacts out of the main UI across hydration and refresh", () => {
    const product = refFixture("product-1", "product_spec");
    const technical = refFixture("technical-1", "technical_spec");
    h.snapshot.project = snapshotFixture([product, technical], 12);

    const { rerender } = render(createElement(StrictMode, null, createElement(ProjectWorkbench, { projectId: "project-1" })));
    expect(h.getArtifact).not.toHaveBeenCalled();
    expect(screen.queryByText("Contract-to-Proof")).toBeNull();

    rerender(createElement(StrictMode, null, createElement(ProjectWorkbench, { projectId: "project-1" })));
    expect(h.getArtifact).not.toHaveBeenCalled();

    h.snapshot.project = snapshotFixture([product, technical], 99);
    rerender(createElement(StrictMode, null, createElement(ProjectWorkbench, { projectId: "project-1" })));
    expect(h.getArtifact).not.toHaveBeenCalled();
    expect(screen.queryByText("Contract-to-Proof")).toBeNull();
  });

  it("disables ordinary prompting while answering through the idempotent clarification endpoint", async () => {
    const user = userEvent.setup();
    h.snapshot.project = waitingSnapshotFixture();
    h.answerRunInputRequest.mockResolvedValue({
      message: { id: "message-1", role: "user", content: "Grid" },
      request: {
        id: "input-1",
        runId: "run-1",
        question: "Which catalogue layout should we use?",
        choices: ["Grid", "List"],
        allowFreeform: false,
        status: "answered",
        stage: "building",
        answeredAt: "2026-08-09T10:01:00.000Z",
      },
      run: { id: "run-1", projectId: "project-1", status: "queued", lastSeq: 10 },
    });

    render(createElement(ProjectWorkbench, { projectId: "project-1" }));

    const composer = screen.getByRole("textbox", { name: "Project prompt" });
    expect((composer as HTMLTextAreaElement).disabled).toBe(true);
    expect((composer as HTMLTextAreaElement).placeholder).toBe("Answer the question in the work log to continue");
    expect(screen.getByText("Answer the question in the work log to continue this run.")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Grid" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(h.answerRunInputRequest).toHaveBeenCalledTimes(1));
    expect(h.answerRunInputRequest).toHaveBeenCalledWith("run-1", "input-1", {
      clientMessageId: expect.any(String),
      answer: "Grid",
    });
    expect(h.chat.sendMessage).not.toHaveBeenCalled();
    expect(h.chat.resumeStream).toHaveBeenCalledTimes(1);
    expect(h.mutate).toHaveBeenCalled();
    expect(await screen.findByText("Answered")).toBeTruthy();
  });

  it("keeps cancel available for a refreshed waiting run before useChat reports streaming", async () => {
    const user = userEvent.setup();
    h.snapshot.project = waitingSnapshotFixture();
    h.chat.status = "ready";

    render(createElement(ProjectWorkbench, { projectId: "project-1" }));

    const stopButton = screen.getByRole("button", { name: "Stop" });
    expect((stopButton as HTMLButtonElement).disabled).toBe(false);
    await user.click(stopButton);

    await waitFor(() => expect(h.cancelRun).toHaveBeenCalledWith("run-1"));
    expect(h.chat.stop).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Cancelled")).toBeTruthy();
  });

  it("refreshes a stale clarification after a conflict and leaves the selected answer retryable", async () => {
    const user = userEvent.setup();
    h.snapshot.project = waitingSnapshotFixture();
    h.answerRunInputRequest.mockRejectedValue(new ApiProblem({ status: 409, title: "Conflict" }));

    render(createElement(ProjectWorkbench, { projectId: "project-1" }));

    const grid = screen.getByRole("button", { name: "Grid" });
    await user.click(grid);
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect((await screen.findByRole("alert")).textContent).toContain("already answered");
    expect(grid.getAttribute("aria-pressed")).toBe("true");
    expect(h.mutate).toHaveBeenCalled();
    expect(h.chat.sendMessage).not.toHaveBeenCalled();
  });
});
