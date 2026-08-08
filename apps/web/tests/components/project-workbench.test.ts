// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, StrictMode } from "react";
import type { HTMLAttributes, ReactNode } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";

import {
  ProjectWorkbench,
  clearArtifactDetailCache,
  useArtifactDetailLoader,
} from "@/components/workbench/project-workbench";
import { useWorkbenchStore } from "@/lib/store/workbench-store";
import type { ArtifactDetail, ArtifactKind, ArtifactRef } from "@/lib/contracts";

const h = vi.hoisted(() => ({
  getArtifact: vi.fn(),
  snapshot: { project: undefined as unknown },
  chat: {
    clearError: vi.fn(),
    error: undefined,
    messages: [],
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
  controlPlane: { getArtifact: h.getArtifact },
  controlPlaneUrl: (path: string) => path,
}));

vi.mock("swr", () => ({
  default: (key: unknown) => {
    const first = Array.isArray(key) ? key[0] : key;
    if (first === "project") {
      return { data: h.snapshot.project, error: undefined, isLoading: false, mutate: vi.fn() };
    }
    if (first === "projects") return { data: [], error: undefined, isLoading: false, mutate: vi.fn() };
    if (first === "versions") return { data: [], error: undefined, isLoading: false, mutate: vi.fn() };
    if (first === "trace") return { data: [], error: undefined, isLoading: false, mutate: vi.fn() };
    if (first === "preview") return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
    if (first === "files") return { data: [], error: undefined, isLoading: false, mutate: vi.fn() };
    if (first === "file") return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
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
  PromptInputSubmit: () => createElement("button"),
  PromptInputTextarea: () => createElement("textarea"),
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
  h.getArtifact.mockReset();
  h.chat.resumeStream.mockClear();
  h.snapshot.project = undefined;
  clearArtifactDetailCache();
});

function refFixture(id: string, kind: ArtifactKind, runId = "run-1"): ArtifactRef {
  return {
    id,
    runId,
    kind,
    role: kind === "product_spec" ? "product_manager" : "architect",
    schemaVersion: 1,
    title: `${kind.replaceAll("_", " ")} ${id}`,
    summary: `Summary ${id}`,
    createdAt: "2026-08-07T12:00:00.000Z",
  };
}

function detailFixture(ref: ArtifactRef, problem: string): ArtifactDetail {
  return { ...ref, kind: ref.kind as ArtifactKind, content: { problem } };
}

function snapshotFixture(refs: ArtifactRef[], lastSeq = 12): Record<string, unknown> {
  return {
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

describe("ProjectWorkbench artifact loading", () => {
  it("hydrates snapshot refs and loads each detail exactly once across StrictMode, rerender and refresh", async () => {
    const product = refFixture("product-1", "product_spec");
    const technical = refFixture("technical-1", "technical_spec");
    h.snapshot.project = snapshotFixture([product, technical], 12);
    h.getArtifact.mockImplementation(async (runId: string, artifactId: string) => {
      const kind: ArtifactKind = artifactId.startsWith("technical") ? "technical_spec" : "product_spec";
      return detailFixture(refFixture(artifactId, kind, runId), "Readers cannot manage books.");
    });

    const { rerender } = render(createElement(StrictMode, null, createElement(ProjectWorkbench, { projectId: "project-1" })));
    await waitFor(() => expect(h.getArtifact).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText(/Readers cannot manage books/)).toBeTruthy());

    // A plain rerender with the same snapshot must not refetch.
    rerender(createElement(StrictMode, null, createElement(ProjectWorkbench, { projectId: "project-1" })));
    expect(h.getArtifact).toHaveBeenCalledTimes(2);

    // A terminal refresh with a new snapshot signature must not refetch known refs.
    h.snapshot.project = snapshotFixture([product, technical], 99);
    rerender(createElement(StrictMode, null, createElement(ProjectWorkbench, { projectId: "project-1" })));
    await waitFor(() => expect(h.getArtifact).toHaveBeenCalledTimes(2));
    expect(screen.getByText(/Readers cannot manage books/)).toBeTruthy();
  });

  it("fetches a ref that appears only after a terminal refresh exactly once", async () => {
    const product = refFixture("product-1", "product_spec");
    h.snapshot.project = snapshotFixture([product], 12);
    h.getArtifact.mockImplementation(async (runId: string, artifactId: string) => {
      const kind: ArtifactKind = artifactId.startsWith("technical") ? "technical_spec" : "product_spec";
      return detailFixture(refFixture(artifactId, kind, runId), "Readers cannot manage books.");
    });

    const { rerender } = render(createElement(ProjectWorkbench, { projectId: "project-1" }));
    await waitFor(() => expect(h.getArtifact).toHaveBeenCalledTimes(1));

    const technical = refFixture("technical-1", "technical_spec");
    h.snapshot.project = snapshotFixture([product, technical], 13);
    rerender(createElement(ProjectWorkbench, { projectId: "project-1" }));

    await waitFor(() => expect(h.getArtifact).toHaveBeenCalledTimes(2));
    expect(h.getArtifact).toHaveBeenCalledWith("run-1", "technical-1");
  });

  it("shows an explicit failure in the spec UI without retrying or falling back", async () => {
    h.snapshot.project = snapshotFixture([refFixture("product-1", "product_spec")], 12);
    h.getArtifact.mockRejectedValue(new Error("artifact fetch failed"));

    const { rerender } = render(createElement(ProjectWorkbench, { projectId: "project-1" }));
    await waitFor(() => expect(screen.getByText("artifact fetch failed")).toBeTruthy());
    expect(screen.queryByText(/Readers cannot manage books/)).toBeNull();
    expect(screen.queryByText("Loading spec content…")).toBeNull();

    rerender(createElement(ProjectWorkbench, { projectId: "project-1" }));
    expect(h.getArtifact).toHaveBeenCalledTimes(1);
  });
});
