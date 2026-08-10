// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { Workspace } from "@/components/workbench/workspace";
import { createRunPresentation } from "@/lib/events/reducer";
import type { PreviewRef, RunPresentation } from "@/lib/contracts";
import type { DeviceViewport, WorkspaceTab } from "@/lib/store/workbench-store";

vi.mock("@/components/workbench/monaco-editor", () => ({
  LazyMonacoEditor: () => null,
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

function presentationWithPreview(preview?: PreviewRef): RunPresentation {
  return createRunPresentation({
    projectId: "project-1",
    run: { id: "run-1", projectId: "project-1", status: "completed", lastSeq: 0 },
    preview,
  });
}

function renderWorkspace(preview?: PreviewRef) {
  return render(createElement(Workspace, {
    device: "desktop" as DeviceViewport,
    files: [],
    onDeviceChange: () => undefined,
    onRestore: () => undefined,
    onSave: () => undefined,
    onSelectFile: () => undefined,
    onVersionChange: () => undefined,
    presentation: presentationWithPreview(preview),
    saving: false,
    selectedTab: "preview" as WorkspaceTab,
    setSelectedTab: () => undefined,
  }));
}

describe("Workspace preview iframe", () => {
  it("mounts exactly one iframe for a valid ready URL", () => {
    const { container } = renderWorkspace({
      status: "ready",
      url: "https://preview.example.test/app",
      runId: "run-1",
    });

    const iframes = container.querySelectorAll("iframe");
    expect(iframes).toHaveLength(1);
    expect(iframes[0]?.getAttribute("src")).toBe("https://preview.example.test/app");
  });

  it("mounts no iframe when runId is missing", () => {
    const { container } = renderWorkspace({
      status: "ready",
      url: "https://preview.example.test/app",
    });

    expect(container.querySelectorAll("iframe")).toHaveLength(0);
  });

  it("mounts no iframe for a non-ready status", () => {
    const { container } = renderWorkspace({
      status: "expired",
      url: "https://preview.example.test/app",
      runId: "run-1",
    });

    expect(container.querySelectorAll("iframe")).toHaveLength(0);
  });

  it("mounts no iframe for an unsafe URL and has no fallback", () => {
    const { container } = renderWorkspace({
      status: "ready",
      url: "http://preview.example.test/app",
      runId: "run-1",
    });

    expect(container.querySelectorAll("iframe")).toHaveLength(0);
    expect(screen.queryByText("Northstar Library")).toBeNull();
    expect(screen.queryByText("图书目录")).toBeNull();
  });

  it("accepts console messages from the derived origin and rejects a wrong origin", async () => {
    renderWorkspace({
      status: "ready",
      url: "https://preview.example.test/app",
      runId: "run-1",
    });

    fireEvent.click(screen.getByText("Console"));

    act(() => {
      window.dispatchEvent(new MessageEvent("message", {
        data: { type: "preview.console", runId: "run-1", level: "log", message: "trusted log line" },
        origin: "https://preview.example.test",
      }));
    });
    await waitFor(() => expect(screen.getByText("trusted log line")).toBeTruthy());

    act(() => {
      window.dispatchEvent(new MessageEvent("message", {
        data: { type: "preview.console", runId: "run-1", level: "log", message: "forged log line" },
        origin: "https://evil.example.test",
      }));
    });
    await waitFor(() => expect(screen.queryByText("forged log line")).toBeNull());
  });
});

describe("Workspace tabs", () => {
  it("keeps a main tab reachable from Problems and supports arrow-key navigation", () => {
    const setSelectedTab = vi.fn();
    render(createElement(Workspace, {
      device: "desktop" as DeviceViewport,
      files: [],
      onDeviceChange: () => undefined,
      onRestore: () => undefined,
      onSave: () => undefined,
      onSelectFile: () => undefined,
      onVersionChange: () => undefined,
      presentation: presentationWithPreview(),
      saving: false,
      selectedTab: "problems" as WorkspaceTab,
      setSelectedTab,
    }));

    const preview = screen.getByRole("tab", { name: "预览" });
    const code = screen.getByRole("tab", { name: "代码" });
    expect(preview.tabIndex).toBe(0);

    preview.focus();
    fireEvent.keyDown(preview, { key: "ArrowRight" });

    expect(setSelectedTab).toHaveBeenCalledWith("code");
    expect(document.activeElement).toBe(code);
  });
});
