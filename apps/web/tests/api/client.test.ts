import { afterEach, describe, expect, it, vi } from "vitest";

import { controlPlane, normalizeApiBase, normalizeUserInputRequest } from "@/lib/api/client";

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

  it("fails closed on an unauthenticated projects request without retrying", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse({ detail: "authentication required" }, 401));

    await expect(controlPlane.getProjects()).rejects.toMatchObject({ status: 401 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(requestUrl(fetchMock, 0).pathname).toBe("/v1/projects");
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).credentials).toBe("include");
  });

  it("normalizes the latest run summary used by task grouping and recovery", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse([{
      id: "project-1",
      title: "Library",
      status: "idle",
      latest_run: {
        id: "run-failed",
        status: "failed",
        error_code: "agent_no_effect",
        agent_framework: "opencode",
        profile_id: "gpt-5.5",
        thinking: "medium",
        recovery_available: true,
        recovery_mode: "verified_checkpoint",
        source_checkpoint_available: true,
      },
    }]));

    await expect(controlPlane.getProjects()).resolves.toEqual([
      expect.objectContaining({
        id: "project-1",
        latestRun: {
          id: "run-failed",
          status: "failed",
          errorCode: "agent_no_effect",
          agentFramework: "opencode",
          profileId: "gpt-5.5",
          thinking: "medium",
          recoveryAvailable: true,
          recoveryMode: "verified_checkpoint",
          sourceCheckpointAvailable: true,
        },
      }),
    ]);
  });

  it("creates a project once with the server's title payload", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse({ id: "project-1", title: "Library" }, 201));

    await controlPlane.createProject({ title: "Library" });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(requestUrl(fetchMock, 0).pathname).toBe("/v1/projects");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.credentials).toBe("include");
    expect(JSON.parse(String(init.body))).toEqual({ title: "Library" });
  });

  it("uses versionId for file requests", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test/v1";
    const fetchMock = installFetch(
      jsonResponse({ files: [] }),
      jsonResponse({ path: "app/page.tsx", content: "export default null" }),
    );

    await controlPlane.getFiles("project 1", "version 2");
    await controlPlane.getFileContent("project 1", "app/page.tsx", "version 2");

    const filesUrl = requestUrl(fetchMock, 0);
    expect(filesUrl.pathname).toBe("/v1/projects/project%201/files");
    expect(filesUrl.searchParams.get("versionId")).toBe("version 2");
    const contentUrl = requestUrl(fetchMock, 1);
    expect(contentUrl.searchParams.get("path")).toBe("app/page.tsx");
    expect(contentUrl.searchParams.get("versionId")).toBe("version 2");
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
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.project.status).toBe("completed");
    expect(snapshot.activeRun).toEqual(expect.objectContaining({ id: "run-active", status: "completed", lastSeq: 19 }));
    expect(snapshot.lastSeq).toBe(19);
    expect(snapshot.runs).toHaveLength(2);
  });

  it("keeps an idle project distinct from its latest failed run", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", status: "idle", title: "Library" },
      messages: [],
      runs: [{
        id: "run-failed",
        projectId: "project-1",
        status: "failed",
        error_code: "model_runtime_protocol_failed",
        lastSeq: 3,
      }],
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.project.status).toBe("idle");
    expect(snapshot.activeRun).toEqual(expect.objectContaining({
      id: "run-failed",
      status: "failed",
      errorCode: "model_runtime_protocol_failed",
    }));
  });

  it("normalizes only the public pending-input fields from project and run snapshots", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const pendingInputRequest = {
      id: "input-1",
      runId: "run-1",
      question: "Which catalogue layout should we use?",
      choices: ["Grid", "List"],
      allowFreeform: true,
      status: "pending",
      stage: "building",
      goalId: "G-2",
      createdAt: "2026-08-09T10:00:00.000Z",
      reason: "private model rationale",
      sessionId: "private-session",
      sandboxId: "private-sandbox",
      context: { hidden: true },
    };
    installFetch(jsonResponse({
      project: { activeRunId: "run-1", id: "project-1", title: "Library" },
      messages: [],
      runs: [{
        id: "run-1",
        projectId: "project-1",
        status: "waiting_for_user",
        lastSeq: 7,
        pendingInputRequest,
      }],
      pendingInputRequest,
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.pendingInputRequest).toEqual({
      id: "input-1",
      runId: "run-1",
      question: "Which catalogue layout should we use?",
      choices: ["Grid", "List"],
      allowFreeform: true,
      status: "pending",
      stage: "building",
      goalId: "G-2",
      createdAt: "2026-08-09T10:00:00.000Z",
      answeredAt: undefined,
    });
    expect(snapshot.activeRun?.pendingInputRequest).toEqual(snapshot.pendingInputRequest);
    expect(JSON.stringify(snapshot.pendingInputRequest)).not.toMatch(/reason|session|sandbox|context/i);
  });

  it("fails closed on malformed or impossible pending-input projections", () => {
    const valid = {
      id: "input-1",
      runId: "run-1",
      question: "Choose a layout",
      choices: ["Grid"],
      allowFreeform: false,
      status: "pending",
      stage: "planning",
    };

    expect(normalizeUserInputRequest(valid)).toBeDefined();
    expect(normalizeUserInputRequest({ ...valid, question: "q".repeat(2_001) })).toBeUndefined();
    expect(normalizeUserInputRequest({ ...valid, choices: ["x".repeat(201)] })).toBeUndefined();
    expect(normalizeUserInputRequest({ ...valid, choices: [] })).toBeUndefined();
    expect(normalizeUserInputRequest({ ...valid, allowFreeform: "false" })).toBeUndefined();
  });

  it("answers the same run idempotently and sends the caller key once", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse({
        message: { id: "message-1", role: "user", content: "Grid" },
        request: {
          id: "input-1",
          runId: "run-1",
          question: "Choose a layout",
          choices: ["Grid", "List"],
          allowFreeform: false,
          status: "answered",
          stage: "planning",
          answeredAt: "2026-08-09T10:01:00.000Z",
          reason: "must stay private",
        },
        run: { id: "run-1", projectId: "project-1", status: "queued", lastSeq: 9 },
      }, 202));

    const result = await controlPlane.answerRunInputRequest("run-1", "input-1", {
      clientMessageId: "client-answer-1",
      answer: "Grid",
    });

    expect(requestUrl(fetchMock, 0).pathname).toBe("/v1/runs/run-1/input-requests/input-1/answer");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("client-answer-1");
    expect(JSON.parse(String(init.body))).toEqual({ clientMessageId: "client-answer-1", answer: "Grid" });
    expect(result.run.status).toBe("queued");
    expect(result.request).toEqual(expect.objectContaining({ id: "input-1", status: "answered" }));
    expect(result.request).not.toHaveProperty("reason");
  });

  it("hydrates the GoalGraph server projection and maps legacy snapshots to null", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(
      jsonResponse({
        project: { id: "project-1", title: "Library" },
        messages: [],
        runs: [{ id: "run-1", projectId: "project-1", status: "running", lastSeq: 4 }],
        goal_graph: {
          graph_id: "graph-1",
          run_id: "run-1",
          revision: 2,
          status: "active",
          product_outcome: "Readers can borrow books.",
          active_goal_id: "G-2",
          goals: [{
            goal_id: "G-2",
            title: "Borrowing flow",
            user_visible: true,
            depends_on: ["G-1"],
            status: "claimed",
            checkpoint_id: "checkpoint-2",
            claimed_at: "2026-08-09T10:00:00.000Z",
            acceptance: [{ acceptance_id: "AC-2", title: "Borrowing persists", priority: "must", status: "pending" }],
            evidence_count: 1,
          }],
        },
      }),
      jsonResponse({ project: { id: "project-p0", title: "Legacy" }, messages: [], runs: [] }),
    );

    const current = await controlPlane.getProject("project-1");
    const legacy = await controlPlane.getProject("project-p0");

    expect(current.goalGraph).toEqual(expect.objectContaining({
      graphId: "graph-1",
      activeGoalId: "G-2",
      goals: [expect.objectContaining({
        goalId: "G-2",
        status: "claimed",
        checkpointId: "checkpoint-2",
        evidenceCount: 1,
        acceptance: [expect.objectContaining({ acceptanceId: "AC-2", status: "pending" })],
      })],
    }));
    expect(legacy.goalGraph).toBeNull();
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
        verificationStatus: "verified",
        origin: "https://untrusted-origin.example.test",
      },
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.preview).toEqual({
      status: "ready",
      url: "https://preview.example.test/app",
      runId: "run-9",
      verificationStatus: "verified",
    });
    expect(snapshot.preview).not.toHaveProperty("origin");
  });

  it("normalizes the preview endpoint response to the typed preview ref", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      status: "ready",
      url: "https://preview.example.test/app",
      runId: "run-9",
      verificationStatus: "unverified",
    }));

    await expect(controlPlane.getPreview("project-1")).resolves.toEqual({
      status: "ready",
      url: "https://preview.example.test/app",
      runId: "run-9",
      verificationStatus: "unverified",
    });
  });

  it("fetches runtime options and normalizes only the public profile fields", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse({
      default_agent_framework: "pi",
      agent_frameworks: [
        { id: "pi", label: "Pi", compatible_profile_ids: ["deepseek-flash", "gpt-5.6"], compatible_thinking_levels: null, available: true },
        { id: "opencode", label: "OpenCode", compatible_profile_ids: ["deepseek-flash", "gpt-5.6"], compatible_thinking_levels: null, available: true },
        { id: "codex", label: "Codex", compatible_profile_ids: ["gpt-5.6"], compatible_thinking_levels: ["low", "medium", "high", "xhigh"], available: true },
      ],
      default_profile_id: "deepseek-flash",
      profiles: [
        { profile_id: "deepseek-flash", label: "DeepSeek Flash", thinking_levels: ["off", "high"], default_thinking: "high", context_window: 1_000_000, run_token_budget: null, run_token_budget_unlimited: true, inference_tpm_limit: 1_250_000, available: true },
        { profile_id: "gpt-5.6", label: "GPT-5.6", thinking_levels: ["off", "low", "high"], default_thinking: "high", context_window: 250_000, run_token_budget: 600_000, inference_tpm_limit: 1_000_000, available: false, disabled_reason: "配额已用尽" },
      ],
    }));

    const options = await controlPlane.getRuntimeOptions();

    expect(requestUrl(fetchMock, 0).pathname).toBe("/v1/runtime/options");
    expect(options.defaultAgentFramework).toBe("pi");
    expect(options.agentFrameworks).toEqual([
      { id: "pi", label: "Pi", compatibleProfileIds: ["deepseek-flash", "gpt-5.6"], compatibleThinkingLevels: null, available: true },
      { id: "opencode", label: "OpenCode", compatibleProfileIds: ["deepseek-flash", "gpt-5.6"], compatibleThinkingLevels: null, available: true },
      { id: "codex", label: "Codex", compatibleProfileIds: ["gpt-5.6"], compatibleThinkingLevels: ["low", "medium", "high", "xhigh"], available: true },
    ]);
    expect(options.defaultProfileId).toBe("deepseek-flash");
    expect(options.profiles).toHaveLength(2);
    expect(options.profiles[0]).toEqual(expect.objectContaining({
      profileId: "deepseek-flash",
      label: "DeepSeek Flash",
      thinkingLevels: ["off", "high"],
      defaultThinking: "high",
      contextWindow: 1_000_000,
      runTokenBudget: null,
      runTokenBudgetUnlimited: true,
      available: true,
    }));
    expect(options.profiles[1]?.available).toBe(false);
    expect(options.profiles[1]?.disabledReason).toBe("配额已用尽");
  });

  it("captures the immutable run runtime from a project snapshot", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({
      project: { id: "project-1", title: "Library" },
      messages: [],
      runs: [{
        id: "run-1",
        projectId: "project-1",
        status: "running",
        lastSeq: 4,
        runtime: {
          profile_id: "deepseek-flash",
          thinking: "high",
          context_window: 1_000_000,
          policy_version: "direct-pi-runtime-v2",
          run_token_budget: null,
          run_token_budget_unlimited: true,
          inference_tpm_limit: 1_250_000,
        },
      }],
    }));

    const snapshot = await controlPlane.getProject("project-1");

    expect(snapshot.activeRun?.runtime).toEqual(expect.objectContaining({
      profileId: "deepseek-flash",
      thinking: "high",
      contextWindow: 1_000_000,
      runTokenBudget: null,
      runTokenBudgetUnlimited: true,
    }));
  });

  it("sends the chosen runtime selection and reads the resolved runtime back", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse({
      message: { id: "message-1", role: "user", content: "Build a thing" },
      run: {
        id: "run-new",
        project_id: "project-1",
        status: "queued",
        last_seq: 1,
        runtime: {
          profile_id: "deepseek-flash",
          thinking: "high",
          context_window: 1_000_000,
          policy_version: "direct-pi-runtime-v2",
          run_token_budget: null,
          run_token_budget_unlimited: true,
          inference_tpm_limit: 1_250_000,
        },
      },
    }, 202));

    const result = await controlPlane.startRun("project-1", {
      clientMessageId: "client-1",
      content: "Build a thing",
      profileId: "deepseek-flash",
      thinking: "high",
    });

    expect(result.runId).toBe("run-new");
    expect(result.runtime).toEqual(expect.objectContaining({
      profileId: "deepseek-flash",
      thinking: "high",
      contextWindow: 1_000_000,
    }));
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("client-1");
    expect(JSON.parse(String(init.body))).toEqual({
      clientMessageId: "client-1",
      content: "Build a thing",
      profileId: "deepseek-flash",
      thinking: "high",
      attachments: [],
    });
  });

  it("creates a recovery run with a user follow-up and frozen runtime selection", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse({
      recovery_mode: "verified_checkpoint",
      source_checkpoint_available: true,
      run: {
        id: "run-recovery",
        agent_framework: "opencode",
        runtime: {
          profile_id: "gpt-5.5",
          thinking: "medium",
          context_window: 250_000,
          policy_version: "direct-pi-runtime-v2",
          run_token_budget: null,
          run_token_budget_unlimited: true,
          inference_tpm_limit: 1_000_000,
        },
      },
    }, 202));

    await expect(controlPlane.recoverRun("run failed", {
      clientMessageId: "recover-1",
      content: "Fix the missing interactions.",
      agentFramework: "opencode",
      profileId: "gpt-5.5",
      thinking: "medium",
    })).resolves.toEqual(expect.objectContaining({
      runId: "run-recovery",
      recoveryMode: "verified_checkpoint",
      sourceCheckpointAvailable: true,
      agentFramework: "opencode",
    }));

    expect(requestUrl(fetchMock).pathname).toBe("/v1/runs/run%20failed/recover");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("recover-1");
    expect(JSON.parse(String(init.body))).toEqual({
      clientMessageId: "recover-1",
      content: "Fix the missing interactions.",
      agentFramework: "opencode",
      profileId: "gpt-5.5",
      thinking: "medium",
      attachments: [],
    });
  });
});
