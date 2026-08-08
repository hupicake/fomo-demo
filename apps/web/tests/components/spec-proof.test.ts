// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import type { HTMLAttributes, ReactNode } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { SpecToProof, specSlotsFromArtifacts } from "@/components/workbench/spec-proof";
import type {
  AcceptanceTrace,
  ArtifactDetail,
  ArtifactLoadState,
  ArtifactRef,
} from "@/lib/contracts";

vi.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({ children }: { children?: ReactNode }) =>
    createElement("div", { "data-testid": "markdown" } as HTMLAttributes<HTMLDivElement>, children),
}));

vi.mock("@/components/ai-elements/plan", () => ({
  Plan: ({ children }: { children?: ReactNode }) => createElement("section", null, children),
  PlanContent: ({ children }: { children?: ReactNode }) => createElement("div", null, children),
  PlanDescription: ({ children }: { children?: ReactNode }) => createElement("p", null, children),
  PlanHeader: ({ children }: { children?: ReactNode }) => createElement("header", null, children),
  PlanTitle: ({ children }: { children?: ReactNode }) => createElement("h3", null, children),
  PlanTrigger: () => createElement("button"),
}));

afterEach(cleanup);

const ref: ArtifactRef = {
  id: "artifact-1",
  runId: "run-1",
  kind: "product_spec",
  role: "product_manager",
  schemaVersion: 1,
  title: "Library product spec",
  summary: "Readers can manage books.",
  createdAt: "2026-08-07T12:00:00.000Z",
};

const detail: ArtifactDetail = {
  ...ref,
  kind: "product_spec",
  content: { problem: "Readers cannot manage books." },
};

const trace: AcceptanceTrace[] = [];

function renderSpecToProof(slots: ReturnType<typeof specSlotsFromArtifacts>) {
  return render(createElement(SpecToProof, {
    onFileSelect: () => undefined,
    slots,
    trace,
  }));
}

describe("specSlotsFromArtifacts", () => {
  it("projects refs and loads into canonical Product then Architect slots", () => {
    const slots = specSlotsFromArtifacts([
      { ...ref, id: "artifact-2", kind: "technical_spec", role: "architect" },
      ref,
    ], {});

    expect(slots.map((slot) => slot.kind)).toEqual(["product_spec", "technical_spec"]);
    expect(slots[0]).toEqual({ kind: "product_spec", state: "loading", title: "Library product spec" });
    expect(slots[1]).toEqual({ kind: "technical_spec", state: "loading", title: "Library product spec" });
  });

  it("is absent for a kind with no ref and ignores hidden kinds entirely", () => {
    const slots = specSlotsFromArtifacts([
      { ...ref, id: "artifact-hidden", kind: "implementation_plan", role: "engineer" },
    ], {});

    expect(slots).toEqual([
      { kind: "product_spec", state: "absent" },
      { kind: "technical_spec", state: "absent" },
    ]);
  });

  it("distinguishes loading, error and ready from the load states", () => {
    const loads: Record<string, ArtifactLoadState> = {
      "artifact-1": { status: "ready", detail },
      "artifact-2": { status: "error", message: "fetch failed" },
      "artifact-3": { status: "ready", detail: { ...detail, id: "artifact-3", kind: "technical_spec", title: "Tech" } },
    };
    const slots = specSlotsFromArtifacts([
      ref,
      { ...ref, id: "artifact-2", kind: "technical_spec" },
    ], loads);

    expect(slots[0]).toEqual({
      kind: "product_spec",
      state: "ready",
      title: "Library product spec",
      markdown: expect.stringContaining("# Library product spec"),
    });
    expect(slots[0]).not.toHaveProperty("error");
    expect(slots[1]).toEqual({
      kind: "technical_spec",
      state: "error",
      title: "Library product spec",
      error: "fetch failed",
    });
    expect(slots[1]).not.toHaveProperty("markdown");
  });

  it("never falls back to another ref's ready content", () => {
    const slots = specSlotsFromArtifacts([
      { ...ref, id: "artifact-2", kind: "technical_spec", role: "architect" },
    ], {
      "artifact-1": { status: "ready", detail },
    });

    expect(slots[1]).toEqual({ kind: "technical_spec", state: "loading", title: "Library product spec" });
    expect(slots[1]).not.toHaveProperty("markdown");
  });

  it("uses the last ref in input order when multiple refs share a canonical kind", () => {
    const newer = { ...ref, id: "artifact-newer", title: "Newer product spec" };
    const slots = specSlotsFromArtifacts([ref, newer], {
      "artifact-newer": {
        status: "ready",
        detail: { ...detail, id: "artifact-newer", title: "Newer product spec" },
      },
    });

    expect(slots[0]).toEqual({
      kind: "product_spec",
      state: "ready",
      title: "Newer product spec",
      markdown: expect.stringContaining("# Newer product spec"),
    });

    // Input order is the only tie-break: the last ref wins even when it is
    // the older one.
    const reversed = specSlotsFromArtifacts([newer, ref], {
      "artifact-1": { status: "ready", detail },
    });
    expect(reversed[0]).toEqual({
      kind: "product_spec",
      state: "ready",
      title: "Library product spec",
      markdown: expect.stringContaining("# Library product spec"),
    });
  });
});

describe("SpecToProof rendering", () => {
  it("renders a ready slot with the formatted markdown and no error hint", () => {
    renderSpecToProof(specSlotsFromArtifacts([ref], { "artifact-1": { status: "ready", detail } }));

    expect(screen.getByText("Library product spec")).toBeTruthy();
    const markdown = screen.getByTestId("markdown");
    expect(markdown.textContent).toContain("Readers cannot manage books");
    expect(screen.queryByText("Loading spec content…")).toBeNull();
  });

  it("renders a loading slot without inventing content", () => {
    renderSpecToProof(specSlotsFromArtifacts([ref], {}));

    expect(screen.getByText("Loading spec content…")).toBeTruthy();
    expect(screen.queryByTestId("markdown")).toBeNull();
    expect(screen.queryByText("Readers cannot manage books")).toBeNull();
  });

  it("renders an error slot with the explicit failure and no fallback content", () => {
    renderSpecToProof(specSlotsFromArtifacts([ref], { "artifact-1": { status: "error", message: "fetch failed" } }));

    expect(screen.getByText("fetch failed")).toBeTruthy();
    expect(screen.queryByTestId("markdown")).toBeNull();
    expect(screen.queryByText("Readers cannot manage books")).toBeNull();
    expect(screen.queryByText("No structured spec received yet.")).toBeNull();
  });

  it("renders an absent slot when no ref exists for the kind", () => {
    renderSpecToProof(specSlotsFromArtifacts([], {}));

    expect(screen.getAllByText("No structured spec received yet.")).toHaveLength(2);
  });

  it("renders slots in the canonical Product then Architect order", () => {
    const { container } = renderSpecToProof(specSlotsFromArtifacts([
      { ...ref, id: "artifact-2", kind: "technical_spec", role: "architect", title: "Tech spec" },
      ref,
    ], {
      "artifact-1": { status: "ready", detail },
      "artifact-2": { status: "ready", detail: { ...detail, id: "artifact-2", kind: "technical_spec", title: "Tech spec" } },
    }));

    const headings = Array.from(container.querySelectorAll("h3")).map((node) => node.textContent);
    expect(headings).toEqual(["Library product spec", "Tech spec"]);
  });

  it("opens file evidence through the select handler and ignores non-file evidence", () => {
    const onFileSelect = vi.fn();
    const withTrace: AcceptanceTrace[] = [{
      id: "AC-1",
      title: "Readers can search",
      priority: "must",
      status: "passed",
      evidence: [
        { id: "ev-file", type: "file", label: "components/books.tsx", status: "passed" },
        { id: "ev-test", type: "test", label: "search.spec.ts", status: "passed" },
      ],
    }];
    render(createElement(SpecToProof, {
      onFileSelect,
      slots: specSlotsFromArtifacts([], {}),
      trace: withTrace,
    }));

    fireEvent.click(screen.getByText("components/books.tsx"));
    expect(onFileSelect).toHaveBeenCalledWith("components/books.tsx");

    fireEvent.click(screen.getByText("search.spec.ts"));
    expect(onFileSelect).toHaveBeenCalledTimes(1);
  });
});
