import { describe, expect, it } from "vitest";

import { formatArtifactDetail } from "@/lib/artifact-markdown";
import type { ArtifactDetail } from "@/lib/contracts";

function productDetail(overrides?: Partial<ArtifactDetail>): ArtifactDetail {
  return {
    id: "artifact-1",
    runId: "run-1",
    kind: "product_spec",
    role: "product_manager",
    schemaVersion: 1,
    title: "Library product spec",
    summary: "Readers can manage books.",
    createdAt: "2026-08-07T12:00:00.000Z",
    content: {
      title: "Library product spec",
      problem: "Readers cannot manage books.",
      targetUsers: ["librarians", "readers"],
      userStories: [
        { id: "US-1", story: "Search the catalogue", priority: "must" },
        { id: "US-2", story: "", priority: "should" },
      ],
      acceptanceCriteria: [{
        id: "AC-1",
        given: "a query",
        when: "submitted",
        then: "matches appear",
      }],
      pages: [{ route: "/", purpose: "Catalogue home", keyElements: ["search", "filter"] }],
      assumptions: ["Readers browse anonymously."],
      outOfScope: ["Fines."],
      visualDirection: { tone: "calm", colors: ["blue"], references: [] },
    },
    ...overrides,
  };
}

function technicalDetail(overrides?: Partial<ArtifactDetail>): ArtifactDetail {
  return {
    id: "artifact-2",
    runId: "run-1",
    kind: "technical_spec",
    role: "architect",
    schemaVersion: 1,
    title: "Library technical spec",
    summary: "Next.js",
    createdAt: "2026-08-07T12:00:00.000Z",
    content: {
      title: "Library technical spec",
      framework: "Next.js",
      starterCapabilities: ["crud", "local-persistence"],
      routes: [{ path: "/books", rendering: "client", description: "Catalogue" }],
      components: [{
        name: "BookTable",
        responsibility: "List and filter books",
        children: [],
        interactionResponsibilities: ["search", "data_table"],
      }],
      componentDecisions: [{
        component: "Table",
        strategy: "reuse",
        source: "radix-ui",
        rationale: "Mature primitive",
      }],
      featureSurfaces: [{
        componentName: "CatalogSurface",
        compositionFile: "app/(generated)/composition.tsx",
        compositionSymbol: "CatalogSurface",
        compositionResponsibilities: ["compose"],
        modules: [{
          role: "data_table",
          filePath: "components/features/books.tsx",
          publicSymbol: "BooksTable",
        }],
      }],
      stateModel: [{
        name: "loanStore",
        owner: "engineer",
        persistence: "localStorage",
        stateClass: "persistent_business",
        mutableDomains: ["loans"],
      }],
      persistentStateDomains: [{
        domain: "loans",
        stateModelName: "loanStore",
        actionsStoreFile: "lib/loans.ts",
      }],
      stateAggregation: {
        filePath: "app/(generated)/composition.tsx",
        responsibilities: ["compose persistent state"],
      },
      dependencies: [{ name: "zod", reason: "validation" }],
      filePlan: [{
        path: "components/features/books.tsx",
        operation: "create",
        reason: "Catalogue table",
      }],
      testPlan: [{
        acceptanceId: "AC-1",
        method: "playwright",
        steps: ["Search a book", "Open its detail"],
      }],
      risks: ["Concurrent loans."],
    },
    ...overrides,
  };
}

function headingOrder(output: string, headings: string[]): void {
  const indexes = headings.map((heading) => output.indexOf(`## ${heading}`));
  for (const index of indexes) {
    expect(index).toBeGreaterThanOrEqual(0);
  }
  expect([...indexes].sort((a, b) => a - b)).toEqual(indexes);
}

describe("formatArtifactDetail product spec", () => {
  it("renders known fields in the fixed canonical order", () => {
    const output = formatArtifactDetail(productDetail());

    expect(output.startsWith("# Library product spec")).toBe(true);
    headingOrder(output, [
      "Problem",
      "Target users",
      "User stories",
      "Acceptance criteria",
      "Pages",
      "Assumptions",
      "Out of scope",
    ]);
    expect(output).toContain("- librarians");
    expect(output).toContain("- Search the catalogue (must)");
    expect(output).toContain("- US\\-2 (should)");
    expect(output).toContain("- **AC\\-1** — Given a query; when submitted; then matches appear.");
    expect(output).toContain("- / — Catalogue home (search, filter)");
  });

  it("escapes hostile HTML and Markdown so output can never become raw HTML", () => {
    const output = formatArtifactDetail(productDetail({
      title: "Spec <script>alert(1)</script>",
      content: {
        problem: '<img src=x onerror="alert(1)"> **bold** `code` [link](https://evil.example) & more',
        targetUsers: ["<b>admin</b>"],
        userStories: [],
        acceptanceCriteria: [],
        pages: [],
        assumptions: ["# heading <em>emphasis</em>"],
        outOfScope: [],
      },
    }));

    expect(output).not.toContain("<script>");
    expect(output).not.toContain("<img");
    expect(output).not.toContain("<b>");
    expect(output).not.toContain("<em>");
    expect(output).not.toContain("[link](");
    expect(output).not.toContain("**bold**");
    expect(output).toContain("&lt;script&gt;");
    expect(output).toContain("\\*\\*bold\\*\\*");
    expect(output).toContain("\\[link\\]\\(");
    expect(output).toContain("\\# heading");
  });

  it("never renders unknown fields", () => {
    const output = formatArtifactDetail(productDetail({
      content: {
        ...productDetail().content,
        evilHtml: "<iframe src='https://evil.example'></iframe>",
        nestedPayload: { onload: "alert(1)" },
        scriptTag: "<script>alert(2)</script>",
      },
    }));

    expect(output).not.toContain("<iframe");
    expect(output).not.toContain("onload");
    expect(output).not.toContain("evilHtml");
    expect(output).not.toContain("nestedPayload");
    expect(output).not.toContain("scriptTag");
    expect(output).not.toContain("<script>");
  });

  it("is deterministic and does not mutate the source content", () => {
    const detail = productDetail();
    const first = formatArtifactDetail(detail);
    const second = formatArtifactDetail(detail);

    expect(second).toBe(first);
    expect(detail.content.problem).toBe("Readers cannot manage books.");
  });

  it("handles non-string list values without throwing", () => {
    const output = formatArtifactDetail(productDetail({
      content: {
        ...productDetail().content,
        targetUsers: [42, null, true],
        assumptions: [null],
      },
    }));

    expect(output).toContain("- 42");
    expect(output).toContain("- null");
  });
});

describe("formatArtifactDetail technical spec", () => {
  it("renders known fields in the fixed canonical order", () => {
    const output = formatArtifactDetail(technicalDetail());

    expect(output.startsWith("# Library technical spec")).toBe(true);
    headingOrder(output, [
      "Framework",
      "Starter capabilities",
      "Routes",
      "Components",
      "Component decisions",
      "Feature surfaces",
      "State model",
      "Persistent state domains",
      "State aggregation",
      "Dependencies",
      "File plan",
      "Test plan",
      "Risks",
    ]);
    expect(output).toContain("- crud");
    expect(output).toContain("- /books — client: Catalogue");
    expect(output).toContain("- BookTable — List and filter books");
    expect(output).toContain("- Table (reuse) — Mature primitive");
    expect(output).toContain("- CatalogSurface — app/\\(generated\\)/composition\\.tsx");
    expect(output).toContain("- loanStore (engineer) — localStorage");
    expect(output).toContain("- loans → lib/loans\\.ts");
    expect(output).toContain("- app/\\(generated\\)/composition\\.tsx — compose persistent state");
    expect(output).toContain("- create components/features/books\\.tsx — Catalogue table");
    expect(output).toContain("- AC\\-1 (playwright) — Search a book; Open its detail");
  });
});

describe("formatArtifactDetail bounds", () => {
  it("enforces a stable hard length bound and marks truncation", () => {
    const stories = Array.from({ length: 60 }, (_, index) => ({
      id: `US-${index}`,
      story: "S".repeat(2000),
      priority: "must" as const,
    }));
    const output = formatArtifactDetail(productDetail({
      content: { ...productDetail().content, userStories: stories },
    }));

    expect(output.length).toBeLessThanOrEqual(40_000);
    expect(output.endsWith("[spec truncated]")).toBe(true);
  });

  it("returns small documents unchanged", () => {
    const output = formatArtifactDetail(productDetail());

    expect(output).not.toContain("[spec truncated]");
    expect(output.length).toBeGreaterThan(0);
  });

  it("falls back to the kind label for an empty title", () => {
    const output = formatArtifactDetail(productDetail({ title: "" }));

    expect(output.startsWith("# product spec")).toBe(true);
  });
});
