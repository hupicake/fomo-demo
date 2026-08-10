// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import type { ReactNode } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ProjectWorkbench } from "@/components/workbench/project-workbench";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ApiProblem } from "@/lib/api/client";
import { useAuthStore } from "@/lib/store/auth-store";
import { useWorkbenchStore } from "@/lib/store/workbench-store";

const h = vi.hoisted(() => ({
  answerRunInputRequest: vi.fn(),
  cancelRun: vi.fn(),
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
  },
  controlPlaneUrl: (path: string) => path,
}));

vi.mock("swr", () => ({
  mutate: vi.fn(),
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
vi.mock("next/navigation", () => ({ usePathname: () => "/projects/test", useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
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
  PromptInputSelect: ({ children, value, onValueChange, disabled }: { children?: ReactNode; value?: string; onValueChange?: (value: string) => void; disabled?: boolean }) => createElement("div", { "data-select": value ?? "", "data-disabled": disabled ? "true" : undefined, onChange: onValueChange }, children),
  PromptInputSelectTrigger: ({ children, ...rest }: { children?: ReactNode } & Record<string, unknown>) => createElement("button", { type: "button", ...rest }, children),
  PromptInputSelectContent: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
  PromptInputSelectItem: ({ children, value }: { children?: ReactNode; value?: string }) => createElement("div", { "data-item": value }, children),
  PromptInputSelectValue: ({ placeholder }: { placeholder?: string }) => createElement("span", null, placeholder),
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

function renderWorkbench(ui: ReactNode = createElement(ProjectWorkbench, { projectId: "project-1" })) {
  return render(createElement(TooltipProvider, null, ui));
}

beforeEach(() => {
  useAuthStore.setState({
    status: "authenticated",
    user: {
      id: "user-1",
      email: "owner@example.test",
      createdAt: "2026-08-07T12:00:00.000Z",
    },
    loading: false,
    busy: false,
    error: undefined,
  });
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
});

function snapshotFixture(lastSeq = 12): Record<string, unknown> {
  return {
    goalGraph: null,
    project: { id: "project-1", name: "Library", status: "idle" },
    activeRun: { id: "run-1", projectId: "project-1", status: "completed", lastSeq },
    lastSeq,
    messages: [],
    events: [],
    files: [],
    versions: [],
    preview: { status: "unavailable" },
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
    ...snapshotFixture(8),
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

describe("ProjectWorkbench runtime overview", () => {
  it("shows the bounded run overview without contract proof or artifact detail loading", () => {
    h.snapshot.project = snapshotFixture();

    renderWorkbench();

    const workLog = screen.getByLabelText("工作日志");
    const activity = screen.getByRole("region", { name: "Agent 活动" });
    const taskSummary = screen.getByRole("region", { name: "当前任务" });
    const composer = screen.getByRole("textbox");
    expect(screen.getByRole("region", { name: "Agent 工作日志" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "运行阶段" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "运行指标" })).toBeTruthy();
    expect(workLog.contains(activity)).toBe(true);
    expect(workLog.contains(taskSummary)).toBe(false);
    expect(workLog.compareDocumentPosition(taskSummary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(taskSummary.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("region", { name: "工作区" })).toBeTruthy();
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

    renderWorkbench();

    const composer = screen.getByRole("textbox", { name: "Project prompt" });
    expect((composer as HTMLTextAreaElement).disabled).toBe(true);
    expect((composer as HTMLTextAreaElement).placeholder).toBe("请在工作日志中回答问题以继续");
    expect(screen.getByText("请回答工作日志中的问题以继续本次运行。")).toBeTruthy();

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

    renderWorkbench();

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

    renderWorkbench();

    const grid = screen.getByRole("button", { name: "Grid" });
    await user.click(grid);
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect((await screen.findByRole("alert")).textContent).toContain("已回答");
    expect(grid.getAttribute("aria-pressed")).toBe("true");
    expect(h.mutate).toHaveBeenCalled();
    expect(h.chat.sendMessage).not.toHaveBeenCalled();
  });

  it("shows the immutable runtime contract from RunResponse.runtime after a run is created", () => {
    h.snapshot.project = {
      ...snapshotFixture(12),
      activeRun: {
        id: "run-1",
        projectId: "project-1",
        status: "completed",
        lastSeq: 12,
        runtime: {
          profileId: "deepseek-flash",
          thinking: "high",
          contextWindow: 1_000_000,
          policyVersion: "direct-pi-runtime-v2",
          runTokenBudget: null,
          runTokenBudgetUnlimited: true,
          inferenceTpmLimit: 1_250_000,
        },
      },
    };

    const { container } = renderWorkbench();

    const badge = container.querySelector('[data-runtime="deepseek-flash"]');
    expect(badge).toBeTruthy();
    expect(badge?.getAttribute("data-thinking")).toBe("high");
    expect(badge?.textContent).toContain("1M");
  });

  it("disables the composer when no runtime model is available", () => {
    h.snapshot.project = snapshotFixture();
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

    renderWorkbench();

    const composer = screen.getByRole("textbox", { name: "Project prompt" }) as HTMLTextAreaElement;
    const submit = screen.getByRole("button", { name: "Submit" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(composer.disabled).toBe(false);
  });
});
