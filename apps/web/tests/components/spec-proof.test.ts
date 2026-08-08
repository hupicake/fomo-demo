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
  VisibleArtifactRef,
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

const ref = {
  id: "artifact-1",
  runId: "run-1",
  kind: "product_spec",
  role: "product_manager",
  stage: "product",
  schemaVersion: 1,
  title: "Library product spec",
  summary: "Readers can manage books.",
  createdAt: "2026-08-07T12:00:00.000Z",
} satisfies VisibleArtifactRef;

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
  it("projects refs and loads into the canonical Direct Pi artifact order", () => {
    const slots = specSlotsFromArtifacts([
      { ...ref, id: "artifact-2", kind: "technical_spec", role: "architect", stage: "architecture" },
      ref,
    ], {});

    expect(slots.map((slot) => slot.kind)).toEqual([
      "run_input",
      "build_plan",
      "acceptance_contract",
      "diagnostic_report",
      "product_spec",
      "technical_spec",
    ]);
    expect(slots[4]).toEqual({ kind: "product_spec", state: "loading", title: "Library product spec" });
    expect(slots[5]).toEqual({ kind: "technical_spec", state: "loading", title: "Library product spec" });
  });

  it("is absent for a kind with no ref and ignores hidden kinds entirely", () => {
    const slots = specSlotsFromArtifacts([
      { ...ref, id: "artifact-hidden", kind: "implementation_plan", role: "engineer" },
    ], {});

    expect(slots).toHaveLength(6);
    expect(slots.every((slot) => slot.state === "absent")).toBe(true);
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

    expect(slots[4]).toEqual({
      kind: "product_spec",
      state: "ready",
      title: "Library product spec",
      markdown: expect.stringContaining("# Library product spec"),
    });
    expect(slots[4]).not.toHaveProperty("error");
    expect(slots[5]).toEqual({
      kind: "technical_spec",
      state: "error",
      title: "Library product spec",
      error: "fetch failed",
    });
    expect(slots[5]).not.toHaveProperty("markdown");
  });

  it("never falls back to another ref's ready content", () => {
    const slots = specSlotsFromArtifacts([
      { ...ref, id: "artifact-2", kind: "technical_spec", role: "architect", stage: "architecture" },
    ], {
      "artifact-1": { status: "ready", detail },
    });

    expect(slots[5]).toEqual({ kind: "technical_spec", state: "loading", title: "Library product spec" });
    expect(slots[5]).not.toHaveProperty("markdown");
  });

  it("uses the last ref in input order when multiple refs share a canonical kind", () => {
    const newer = { ...ref, id: "artifact-newer", title: "Newer product spec" };
    const slots = specSlotsFromArtifacts([ref, newer], {
      "artifact-newer": {
        status: "ready",
        detail: { ...detail, id: "artifact-newer", title: "Newer product spec" },
      },
    });

    expect(slots[4]).toEqual({
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
    expect(reversed[4]).toEqual({
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

  it("does not render empty placeholder cards when no artifact exists", () => {
    renderSpecToProof(specSlotsFromArtifacts([], {}));

    expect(screen.queryByText("No structured spec received yet.")).toBeNull();
  });

  it("renders slots in the canonical Product then Architect order", () => {
    const { container } = renderSpecToProof(specSlotsFromArtifacts([
      { ...ref, id: "artifact-2", kind: "technical_spec", role: "architect", stage: "architecture", title: "Tech spec" },
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

  it("shows an independent implemented badge and an unverified validation state", () => {
    const withTrace: AcceptanceTrace[] = [
      {
        id: "AC-1",
        title: "Readers can search",
        priority: "must",
        status: "unverified",
        implementationStatus: "implemented",
        evidence: [],
      },
      {
        id: "AC-2",
        title: "Readers can borrow",
        priority: "must",
        status: "unverified",
        implementationStatus: "not_implemented",
        evidence: [],
      },
    ];
    render(createElement(SpecToProof, {
      onFileSelect: () => undefined,
      slots: specSlotsFromArtifacts([], {}),
      trace: withTrace,
    }));

    // implemented · unverified is displayed as two independent signals.
    expect(screen.getAllByText("implemented")).toHaveLength(1);
    expect(screen.getAllByText("not implemented")).toHaveLength(1);
    expect(screen.getAllByText("unverified · no deterministic playwright evidence yet")).toHaveLength(2);
  });
});
