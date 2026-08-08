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
