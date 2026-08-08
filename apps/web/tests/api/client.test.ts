import { afterEach, describe, expect, it, vi } from "vitest";

import { controlPlane, normalizeApiBase } from "@/lib/api/client";

const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
const originalFetch = globalThis.fetch;

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}

function installFetch(...responses: Response[]) {
  const mockedFetch = vi.fn<typeof fetch>();
  for (const response of responses) mockedFetch.mockResolvedValueOnce(response);
  vi.stubGlobal("fetch", mockedFetch);
  return mockedFetch;
}

function requestUrl(fetchMock: ReturnType<typeof installFetch>, call = 0): URL {
  return new URL(String(fetchMock.mock.calls[call]?.[0]));
}

afterEach(() => {
  vi.unstubAllGlobals();
  globalThis.fetch = originalFetch;
  if (originalApiUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL;
  else process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
});

describe("control plane client contract", () => {
  it("normalizes an origin or a versioned origin to the same v1 API base", () => {
    expect(normalizeApiBase("https://api.example.test")).toBe("https://api.example.test/v1");
    expect(normalizeApiBase("https://api.example.test/v1/")).toBe("https://api.example.test/v1");
  });

  it("bootstraps a guest session once before retrying the first projects request", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(
      jsonResponse({ detail: "guest session required" }, 401),
      jsonResponse({ id: "guest-1" }, 201),
      jsonResponse([]),
    );

    await expect(controlPlane.getProjects()).resolves.toEqual([]);

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(requestUrl(fetchMock, 0).pathname).toBe("/v1/projects");
    expect(requestUrl(fetchMock, 1).pathname).toBe("/v1/sessions/guest");
    expect(requestUrl(fetchMock, 2).pathname).toBe("/v1/projects");
    for (const [, init] of fetchMock.mock.calls) {
      expect((init as RequestInit).credentials).toBe("include");
    }
  });

  it("shares a single guest bootstrap across concurrent first project reads", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(
      jsonResponse({ detail: "guest session required" }, 401),
      jsonResponse({ detail: "guest session required" }, 401),
      jsonResponse({ id: "guest-1" }, 201),
      jsonResponse([]),
      jsonResponse([]),
    );

    await expect(Promise.all([controlPlane.getProjects(), controlPlane.getProjects()])).resolves.toEqual([[], []]);

    expect(fetchMock.mock.calls.filter(([url]) => new URL(String(url)).pathname === "/v1/sessions/guest")).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it("retries a project creation once with the server's title payload", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(
      jsonResponse({ detail: "guest session required" }, 401),
      jsonResponse({ id: "guest-1" }, 201),
      jsonResponse({ id: "project-1", title: "Library" }, 201),
    );

    await controlPlane.createProject({ title: "Library" });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(requestUrl(fetchMock, 0).pathname).toBe("/v1/projects");
    expect(requestUrl(fetchMock, 1).pathname).toBe("/v1/sessions/guest");
    expect(requestUrl(fetchMock, 2).pathname).toBe("/v1/projects");
    const init = fetchMock.mock.calls[2]?.[1] as RequestInit;
    expect(init.credentials).toBe("include");
    expect(JSON.parse(String(init.body))).toEqual({ title: "Library" });
  });

  it("does not recursively retry after the post-bootstrap request is still unauthorized", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(
      jsonResponse({ detail: "guest session required" }, 401),
      jsonResponse({ id: "guest-1" }, 201),
      jsonResponse({ detail: "still unauthorized" }, 401),
    );

    await expect(controlPlane.getProjects()).rejects.toMatchObject({ status: 401 });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(requestUrl(fetchMock, 0).pathname).toBe("/v1/projects");
    expect(requestUrl(fetchMock, 1).pathname).toBe("/v1/sessions/guest");
    expect(requestUrl(fetchMock, 2).pathname).toBe("/v1/projects");
  });

  it("uses versionId for files and runId for trace requests", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test/v1";
    const fetchMock = installFetch(
      jsonResponse({ files: [] }),
      jsonResponse({ path: "app/page.tsx", content: "export default null" }),
      jsonResponse({ runId: "run-9", links: [], evidence: [] }),
    );

    await controlPlane.getFiles("project 1", "version 2");
    await controlPlane.getFileContent("project 1", "app/page.tsx", "version 2");
    await controlPlane.getTrace("project 1", "run 9");

    const filesUrl = requestUrl(fetchMock, 0);
    expect(filesUrl.pathname).toBe("/v1/projects/project%201/files");
    expect(filesUrl.searchParams.get("versionId")).toBe("version 2");
    const contentUrl = requestUrl(fetchMock, 1);
    expect(contentUrl.searchParams.get("path")).toBe("app/page.tsx");
    expect(contentUrl.searchParams.get("versionId")).toBe("version 2");
    const traceUrl = requestUrl(fetchMock, 2);
    expect(traceUrl.pathname).toBe("/v1/projects/project%201/trace");
    expect(traceUrl.searchParams.get("runId")).toBe("run 9");
  });

  it("uses the file query and optimistic fields expected by the current API", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse({
      versionId: "version-next",
      path: "app/page.tsx",
      content: "export default null",
      sha256: "sha-next",
    }));

    const saved = await controlPlane.saveFile("project 1", {
      path: "app/page.tsx",
      content: "export default null",
      baseVersionId: "version-current",
      hash: "sha-current",
    });

    const url = requestUrl(fetchMock);
    expect(url.pathname).toBe("/v1/projects/project%201/files/content");
    expect(url.searchParams.get("path")).toBe("app/page.tsx");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      content: "export default null",
      baseVersionId: "version-current",
      baseSha256: "sha-current",
    });
    expect(saved.hash).toBe("sha-next");
  });

  it("selects the active run from a current project snapshot and maps succeeded", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: {
        activeRunId: "run-active",
        id: "project-1",
        status: "succeeded",
        title: "Library",
      },
      messages: [],
      runs: [
        { id: "run-old", projectId: "project-1", status: "failed", lastSeq: 4 },
        { id: "run-active", projectId: "project-1", status: "succeeded", lastSeq: 19 },
      ],
      trace: {
        acceptanceTrace: [{
          acceptanceId: "AC-LIBRARY-1",
          criterion: { then: "Readers can borrow an available book." },
          status: "passed",
          links: [{ id: "link-1", targetKind: "file", targetRef: "lib/loans.ts" }],
          evidence: [{ id: "evidence-1", kind: "test", status: "passed", summary: "loan flow passes" }],
        }],
      },
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.project.status).toBe("completed");
    expect(snapshot.activeRun).toEqual(expect.objectContaining({ id: "run-active", status: "completed", lastSeq: 19 }));
    expect(snapshot.lastSeq).toBe(19);
    expect(snapshot.runs).toHaveLength(2);
    expect(snapshot.trace).toEqual([expect.objectContaining({
      id: "AC-LIBRARY-1",
      status: "passed",
      title: "Readers can borrow an available book.",
      evidence: expect.arrayContaining([
        expect.objectContaining({ id: "link-1", type: "file" }),
        expect.objectContaining({ id: "evidence-1", type: "test", status: "passed" }),
      ]),
    })]);
  });

  it("keeps an idle project distinct from its latest failed run", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", status: "idle", title: "Library" },
      messages: [],
      runs: [{ id: "run-failed", projectId: "project-1", status: "failed", lastSeq: 3 }],
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.project.status).toBe("idle");
    expect(snapshot.activeRun).toEqual(expect.objectContaining({ id: "run-failed", status: "failed" }));
  });

  it("maps unverified validation and link-derived implementation status from the trace", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      trace: {
        acceptanceTrace: [{
          acceptanceId: "AC-1",
          criterion: { then: "Readers can search." },
          status: "unverified",
          implementationStatus: "implemented",
          links: [{ id: "link-has-test", relation: "has_test", targetKind: "file", targetRef: "tests/generated/library.smoke.spec.ts" }],
          evidence: [{
            id: "evidence-1",
            kind: "playwright_smoke",
            status: "passed",
            summary: '{"runId":"run-9","acceptanceId":"AC-1","testPath":"tests/generated/library.smoke.spec.ts","testName":"library keeps a searchable catalog","result":"passed","recordedAt":"2026-08-07T10:00:00Z","exitCode":0,"artifactRef":null}',
          }],
        }],
      },
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.trace).toEqual([expect.objectContaining({
      id: "AC-1",
      status: "unverified",
      implementationStatus: "implemented",
      evidence: expect.arrayContaining([
        // The bounded structured summary renders as a compact human label.
        expect.objectContaining({ id: "evidence-1", type: "test", status: "passed", label: "library keeps a searchable catalog · passed" }),
      ]),
    })]);
  });

  it("derives not_implemented from the absence of implemented_in links in the graph fallback", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      trace: {
        links: [{ id: "link-1", sourceKind: "acceptance_criterion", sourceRef: "AC-1", relation: "has_test", targetKind: "file", targetRef: "tests/generated/library.smoke.spec.ts" }],
        evidence: [],
      },
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.trace).toEqual([expect.objectContaining({
      id: "AC-1",
      implementationStatus: "not_implemented",
    })]);
  });

  it("does not mark implemented from a test-file implemented_in link in the graph fallback", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      trace: {
        links: [{ id: "link-test", sourceKind: "acceptance_criterion", sourceRef: "AC-1", relation: "implemented_in", targetKind: "file", targetRef: "tests/generated/library.smoke.spec.ts" }],
        evidence: [],
      },
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.trace).toEqual([expect.objectContaining({
      id: "AC-1",
      implementationStatus: "not_implemented",
    })]);
  });

  it("does not mark implemented from a malformed implemented_in link in the graph fallback", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      trace: {
        links: [
          // implemented_in with a non-file target kind: not a business link.
          { id: "link-bad", sourceKind: "acceptance_criterion", sourceRef: "AC-1", relation: "implemented_in", targetKind: "artifact", targetRef: "artifact-1" },
        ],
        evidence: [],
      },
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.trace).toEqual([expect.objectContaining({
      id: "AC-1",
      implementationStatus: "not_implemented",
    })]);
  });

  it("marks implemented only from a well-formed business-file implemented_in link", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      trace: {
        links: [{ id: "link-real", sourceKind: "acceptance_criterion", sourceRef: "AC-1", relation: "implemented_in", targetKind: "file", targetRef: "components/features/library.tsx" }],
        evidence: [],
      },
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.trace).toEqual([expect.objectContaining({
      id: "AC-1",
      implementationStatus: "implemented",
    })]);
  });

  it("applies the business-file predicate to acceptanceTrace links and keeps explicit status authoritative", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      trace: {
        acceptanceTrace: [
          {
            acceptanceId: "AC-1",
            criterion: { then: "Readers can search." },
            status: "unverified",
            links: [{ id: "link-snake", source_kind: "acceptance_criterion", source_ref: "AC-1", relation: "implemented_in", target_kind: "file", target_ref: "tests/generated/library.smoke.spec.ts" }],
          },
          {
            acceptanceId: "AC-2",
            criterion: { then: "Readers can borrow." },
            status: "unverified",
            implementationStatus: "implemented",
            links: [{ id: "link-test-only", source_kind: "acceptance_criterion", source_ref: "AC-2", relation: "implemented_in", target_kind: "file", target_ref: "tests/generated/library-2.smoke.spec.ts" }],
          },
        ],
      },
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.trace).toEqual([
      expect.objectContaining({ id: "AC-1", implementationStatus: "not_implemented" }),
      // Explicit backend implementationStatus wins over link-derived fallback.
      expect.objectContaining({ id: "AC-2", implementationStatus: "implemented" }),
    ]);
  });

  it("normalizes a ready preview from the snapshot without preserving a second origin field", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      preview: {
        status: "ready",
        url: "https://preview.example.test/app",
        runId: "run-9",
        origin: "https://untrusted-origin.example.test",
      },
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.preview).toEqual({
      status: "ready",
      url: "https://preview.example.test/app",
      runId: "run-9",
    });
    expect(snapshot.preview).not.toHaveProperty("origin");
  });

  it("normalizes the preview endpoint response to the typed preview ref", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({ status: "ready", url: "https://preview.example.test/app", runId: "run-9" }));

    await expect(controlPlane.getPreview("project-1")).resolves.toEqual({
      status: "ready",
      url: "https://preview.example.test/app",
      runId: "run-9",
    });
  });
});

describe("artifact ref and detail contract", () => {
  const ref = {
    id: "artifact-1",
    runId: "run-1",
    kind: "product_spec",
    role: "product_manager",
    schemaVersion: 1,
    title: "Library product spec",
    summary: "Readers can manage books.",
    createdAt: "2026-08-07T12:00:00.000Z",
  };

  it("normalizes valid snapshot refs and fails closed on hidden kinds and malformed required fields", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      artifactRefs: [
        ref,
        { ...ref, id: "artifact-2", kind: "technical_spec", role: "architect" },
        { ...ref, id: "artifact-3", kind: "diagnostic_report", role: "reviewer" },
        { ...ref, id: "artifact-4", summary: "" },
        { ...ref, id: "artifact-5", schemaVersion: "one" },
        { ...ref, id: "artifact-6", runId: undefined },
      ],
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.artifactRefs).toEqual([
      ref,
      { ...ref, id: "artifact-2", kind: "technical_spec", role: "architect" },
    ]);
    const refs = snapshot.artifactRefs;
    expect(refs).toBeDefined();
    expect(refs).toHaveLength(2);
    if (!refs) {
      throw new Error("snapshot.artifactRefs must be present");
    }
    expect(refs[0]).not.toHaveProperty("content");
  });

  it("normalizes snake_case refs identically to camelCase refs", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      artifact_refs: [{
        id: "artifact-1",
        run_id: "run-1",
        kind: "technical_spec",
        role: "architect",
        schema_version: 2,
        title: "Tech spec",
        summary: "Next.js",
        created_at: "2026-08-07T12:00:00.000Z",
      }],
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.artifactRefs).toEqual([{
      id: "artifact-1",
      runId: "run-1",
      kind: "technical_spec",
      role: "architect",
      schemaVersion: 2,
      title: "Tech spec",
      summary: "Next.js",
      createdAt: "2026-08-07T12:00:00.000Z",
    }]);
  });

  it("returns detail content as a strict JSON object", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const content = { problem: "Readers cannot manage books.", visualDirection: { tone: "calm" } };
    installFetch(jsonResponse({ ...ref, content }));

    await expect(controlPlane.getArtifact("run-1", "artifact-1")).resolves.toEqual({
      ...ref,
      content,
    });
  });

  it("fails closed when detail content is not a JSON object", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({ ...ref, content: JSON.stringify({ problem: "x" }) }));

    await expect(controlPlane.getArtifact("run-1", "artifact-1")).rejects.toMatchObject({ status: 502 });
  });

  it("fails closed on null or array detail content", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({ ...ref, content: null }));
    await expect(controlPlane.getArtifact("run-1", "artifact-1")).rejects.toMatchObject({ status: 502 });

    installFetch(jsonResponse({ ...ref, content: ["not", "an", "object"] }));
    await expect(controlPlane.getArtifact("run-1", "artifact-1")).rejects.toMatchObject({ status: 502 });
  });

  it("fails closed when the response run or artifact id does not match the request", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({ ...ref, runId: "run-other" }));
    await expect(controlPlane.getArtifact("run-1", "artifact-1")).rejects.toMatchObject({ status: 502 });

    installFetch(jsonResponse({ ...ref, id: "artifact-other" }));
    await expect(controlPlane.getArtifact("run-1", "artifact-1")).rejects.toMatchObject({ status: 502 });
  });

  it("fails closed on hidden kinds and malformed refs in detail responses", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({ ...ref, kind: "implementation_plan", role: "engineer" }));
    await expect(controlPlane.getArtifact("run-1", "artifact-1")).rejects.toMatchObject({ status: 502 });

    installFetch(jsonResponse({ ...ref, summary: "" }));
    await expect(controlPlane.getArtifact("run-1", "artifact-1")).rejects.toMatchObject({ status: 502 });
  });

  it("fails closed on snapshot refs whose role mismatches the fixed kind mapping", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [],
      artifactRefs: [
        ref,
        { ...ref, id: "artifact-2", kind: "technical_spec", role: "product_manager" },
        { ...ref, id: "artifact-3", kind: "product_spec", role: "architect" },
        { ...ref, id: "artifact-4", kind: "product_spec", role: "engineer" },
      ],
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.artifactRefs).toEqual([ref]);
  });

  it("fails closed on detail responses whose role mismatches the fixed kind mapping", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({ ...ref, role: "architect" }));
    await expect(controlPlane.getArtifact("run-1", "artifact-1")).rejects.toMatchObject({ status: 502 });

    installFetch(jsonResponse({ ...ref, kind: "technical_spec", role: "product_manager" }));
    await expect(controlPlane.getArtifact("run-1", "artifact-1")).rejects.toMatchObject({ status: 502 });
  });
});
