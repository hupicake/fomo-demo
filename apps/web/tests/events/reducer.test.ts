import { describe, expect, it } from "vitest";

import type { DomainEvent } from "@/lib/contracts";
import { createRunPresentation, domainEventToMessageChunks, hydrateRunPresentationFromSnapshot, reconcileInputAnswer, reconcileInputRequestSnapshot, reduceDomainEvent } from "@/lib/events/reducer";

function event(
  seq: number,
  kind: string,
  payload: Record<string, unknown>,
  role?: DomainEvent["role"],
): DomainEvent {
  return {
    schemaVersion: 1,
    eventId: `event-${seq}`,
    seq,
    projectId: "project-library",
    runId: "run-library",
    kind,
    role,
    occurredAt: "2026-08-07T12:00:00.000Z",
    payload,
  };
}

describe("run presentation reducer", () => {
  it("projects the public clarification lifecycle without leaking continuation metadata", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });
    const requested = event(1, "run.input_requested", {
      requestId: "input-1",
      question: "Which catalogue layout should we use?",
      choices: ["Grid", "List"],
      allowFreeform: true,
      stage: "building",
      goalId: "G-2",
      reason: "private model rationale",
      sessionId: "private-session",
      sandboxId: "private-sandbox",
      context: { hidden: true },
    }, "pi");

    state = reduceDomainEvent(state, requested);
    expect(state.status).toBe("waiting_for_user");
    expect(state.inputRequests).toEqual([{
      id: "input-1",
      runId: "run-library",
      question: "Which catalogue layout should we use?",
      choices: ["Grid", "List"],
      allowFreeform: true,
      status: "pending",
      stage: "building",
      goalId: "G-2",
      createdAt: "2026-08-07T12:00:00.000Z",
      requestedSeq: 1,
    }]);
    expect(JSON.stringify(state.inputRequests)).not.toMatch(/reason|session|sandbox|context/i);
    expect(domainEventToMessageChunks(requested)).toEqual([]);

    state = reduceDomainEvent(state, event(2, "run.status_changed", { status: "waiting_for_user" }));
    state = reduceDomainEvent(state, event(3, "run.input_answered", { requestId: "input-1", messageId: "message-1" }, "user"));
    expect(state.inputRequests[0]).toEqual(expect.objectContaining({
      status: "answered",
      resolvedSeq: 3,
      answerMessageId: "message-1",
    }));
    expect(domainEventToMessageChunks(event(3, "run.input_answered", { requestId: "input-1" }))).toEqual([]);
    expect(domainEventToMessageChunks(event(4, "run.resumed", { requestId: "input-1" }))).toEqual([]);

    state = reduceDomainEvent(state, event(4, "run.status_changed", { status: "queued" }));
    state = reduceDomainEvent(state, event(5, "file.changed", { id: "file-1", path: "app/page.tsx", status: "modified" }));
    expect(state.status).toBe("queued");
    expect(state.inputRequests[0]?.status).toBe("answered");
    expect(state.worklog.at(-1)?.seq).toBe(5);
  });

  it("fails closed on malformed clarification events so they cannot create an unanswerable card", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });
    const malformed = [
      event(1, "run.input_requested", { requestId: "input-1", question: "q".repeat(2_001), choices: ["Grid"], allowFreeform: false, stage: "planning" }),
      event(1, "run.input_requested", { requestId: "input-1", question: "Choose", choices: ["x".repeat(201)], allowFreeform: false, stage: "planning" }),
      event(1, "run.input_requested", { requestId: "input-1", question: "Choose", choices: [], allowFreeform: false, stage: "planning" }),
      event(1, "run.input_requested", { requestId: "input-1", question: "Choose", choices: ["Grid"], allowFreeform: "false", stage: "planning" }),
    ];

    for (const malformedEvent of malformed) {
      const state = reduceDomainEvent(initial, malformedEvent);
      expect(state.inputRequests).toEqual([]);
      expect(state.status).toBe("running");
    }
  });

  it("reconciles an answer locally without advancing the SSE cursor and preserves history on refresh", () => {
    const pending = reduceDomainEvent(createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    }), event(1, "run.input_requested", {
      requestId: "input-1",
      question: "Choose a layout",
      choices: ["Grid"],
      allowFreeform: false,
      stage: "planning",
    }));

    const answered = reconcileInputAnswer(pending, {
      message: { id: "message-1", role: "user", content: "Grid" },
      request: {
        id: "input-1",
        runId: "run-library",
        question: "Choose a layout",
        choices: ["Grid"],
        allowFreeform: false,
        status: "answered",
        stage: "planning",
        answeredAt: "2026-08-07T12:01:00.000Z",
      },
      run: { id: "run-library", projectId: "project-library", status: "queued", lastSeq: 3 },
    });

    expect(answered.lastSeq).toBe(1);
    expect(answered.status).toBe("queued");
    expect(answered.inputRequests[0]).toEqual(expect.objectContaining({
      status: "answered",
      requestedSeq: 1,
      resolvedSeq: 1,
      answerMessageId: "message-1",
    }));
    expect(reconcileInputRequestSnapshot(answered).inputRequests[0]?.status).toBe("answered");
    expect(reconcileInputRequestSnapshot(pending).inputRequests[0]?.status).toBe("expired");
  });

  it("hydrates a pending clarification from the authoritative project snapshot", () => {
    const hydrated = hydrateRunPresentationFromSnapshot({
      events: [],
      lastSeq: 8,
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "waiting_for_user", lastSeq: 8 },
      pendingInputRequest: {
        id: "input-1",
        runId: "run-library",
        question: "Choose a layout",
        choices: ["Grid", "List"],
        allowFreeform: false,
        status: "pending",
        stage: "building",
      },
    });

    expect(hydrated.status).toBe("waiting_for_user");
    expect(hydrated.inputRequests).toEqual([expect.objectContaining({ id: "input-1", status: "pending" })]);
  });

  it("stores only real context usage snapshots emitted at turn boundaries", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    expect(state.contextUsage).toBeUndefined();
    state = reduceDomainEvent(state, event(1, "pi.started", {
      contextTokens: 40_000,
      contextWindow: 200_000,
      stage: "building",
    }));
    expect(state.contextUsage).toEqual({
      contextTokens: 40_000,
      contextWindow: 200_000,
      boundary: "turn_started",
      capturedAt: "2026-08-07T12:00:00.000Z",
    });

    state = reduceDomainEvent(state, event(2, "pi.completed", {
      contextTokens: 62_500,
      contextWindow: 200_000,
      stage: "building",
    }));
    expect(state.contextUsage).toEqual({
      contextTokens: 62_500,
      contextWindow: 200_000,
      boundary: "turn_completed",
      capturedAt: "2026-08-07T12:00:00.000Z",
    });

    state = reduceDomainEvent(state, event(3, "file.changed", { id: "file-1", path: "app/page.tsx", status: "modified" }));
    expect(state.contextUsage?.contextTokens).toBe(62_500);

    state = reduceDomainEvent(state, event(4, "pi.started", { contextWindow: 220_000 }));
    expect(state.contextUsage).toEqual({
      contextTokens: 62_500,
      contextWindow: 220_000,
      boundary: "turn_started",
      capturedAt: "2026-08-07T12:00:00.000Z",
    });
  });

  it("hydrates a GoalGraph snapshot without deriving verified from passed acceptance", () => {
    const snapshot = hydrateRunPresentationFromSnapshot({
      events: [],
      lastSeq: 8,
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 8 },
      goalGraph: {
        graphId: "graph-1",
        runId: "run-library",
        revision: 1,
        status: "active",
        productOutcome: "Readers can borrow books.",
        activeGoalId: "G-1",
        goals: [{
          goalId: "G-1",
          title: "Borrowing flow",
          userVisible: true,
          dependsOn: [],
          status: "claimed",
          acceptance: [{ acceptanceId: "AC-1", title: "Borrowing passes", priority: "must", status: "passed" }],
          evidenceCount: 1,
        }],
      },
    });

    expect(snapshot.goalGraph?.goals[0]?.status).toBe("claimed");
    expect(snapshot.goalGraph?.goals[0]?.acceptance[0]?.status).toBe("passed");
  });

  it("replays every GoalGraph lifecycle event from the server projection", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });
    state = reduceDomainEvent(state, event(1, "goal_graph.created", {
      graphId: "graph-1",
      runId: "run-library",
      revision: 1,
      status: "active",
      productOutcome: "Readers can borrow books.",
      activeGoalId: null,
      goals: [{ goalId: "G-1", title: "Borrowing", userVisible: true, dependsOn: [], status: "pending", acceptance: [], evidenceCount: 0 }],
    }));
    state = reduceDomainEvent(state, event(2, "goal.activated", { goalId: "G-1" }));
    state = reduceDomainEvent(state, event(3, "goal.claimed", { goalId: "G-1", checkpointId: "checkpoint-1", evidenceCount: 1 }));
    expect(state.goalGraph?.goals[0]).toEqual(expect.objectContaining({ status: "claimed", checkpointId: "checkpoint-1", evidenceCount: 1 }));
    state = reduceDomainEvent(state, event(4, "goal.verification_failed", { goalId: "G-1" }));
    expect(state.goalGraph?.goals[0]?.status).toBe("claimed");
    state = reduceDomainEvent(state, event(5, "goal.resume_scheduled", { goalId: "G-1", checkpointId: "checkpoint-1" }));
    state = reduceDomainEvent(state, event(6, "goal.resumed", { goalId: "G-1" }));
    expect(state.goalGraph?.goals[0]?.status).toBe("active");
    state = reduceDomainEvent(state, event(7, "goal.claimed", { goalId: "G-1" }));
    state = reduceDomainEvent(state, event(8, "goal.verified", { goalId: "G-1", evidenceCount: 2 }));
    expect(state.goalGraph?.goals[0]).toEqual(expect.objectContaining({ status: "verified", evidenceCount: 2 }));
    state = reduceDomainEvent(state, event(9, "goal_graph.completed", { revision: 2 }));
    expect(state.goalGraph).toEqual(expect.objectContaining({ status: "verified", activeGoalId: null, revision: 2 }));

    const failed = reduceDomainEvent({ ...state, lastSeq: 9 }, event(10, "goal_graph.failed", {}));
    expect(failed.goalGraph?.status).toBe("failed");
  });

  it("uses authoritative goal projections for evidence and graph terminal states", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
      goalGraph: {
        graphId: "graph-1", runId: "run-library", revision: 1, status: "active",
        productOutcome: "Readers can borrow books.", activeGoalId: "G-1",
        goals: [{
          goalId: "G-1", title: "Borrowing", userVisible: true, dependsOn: [], status: "claimed",
          acceptance: [{ acceptanceId: "AC-1", title: "Borrowing passes", priority: "must", status: "unverified" }],
          evidenceCount: 0,
        }],
      },
    });
    const verifiedProjection = {
      graphId: "graph-1", runId: "run-library", revision: 1, status: "active",
      productOutcome: "Readers can borrow books.", activeGoalId: null,
      goals: [{
        goalId: "G-1", title: "Borrowing", userVisible: true, dependsOn: [], status: "verified",
        acceptance: [{ acceptanceId: "AC-1", title: "Borrowing passes", priority: "must", status: "passed" }],
        evidenceCount: 2,
      }],
    } as const;

    const verified = reduceDomainEvent(initial, event(1, "goal.verified", { goalGraph: verifiedProjection }));
    expect(verified.goalGraph?.goals[0]).toEqual(expect.objectContaining({ status: "verified", evidenceCount: 2 }));
    expect(verified.goalGraph?.goals[0]?.acceptance[0]?.status).toBe("passed");

    const cancelledProjection = {
      ...verifiedProjection,
      status: "cancelled" as const,
      goals: verifiedProjection.goals.map((goal) => ({ ...goal, status: "superseded" as const })),
    };
    const cancelled = reduceDomainEvent(verified, event(2, "goal_graph.cancelled", { goalGraph: cancelledProjection }));
    expect(cancelled.goalGraph).toEqual(expect.objectContaining({ status: "cancelled", activeGoalId: null }));
    expect(cancelled.goalGraph?.goals[0]?.status).toBe("superseded");

    const superseded = reduceDomainEvent(initial, event(1, "goal_graph.superseded", {}));
    expect(superseded.goalGraph).toEqual(expect.objectContaining({ status: "superseded", activeGoalId: null }));
  });

  it("keeps a non-null snapshot GoalGraph authoritative over historical replay", () => {
    const authoritative = {
      graphId: "graph-1", runId: "run-library", revision: 2, status: "verified" as const,
      productOutcome: "Readers can borrow books.", activeGoalId: null,
      goals: [{
        goalId: "G-1", title: "Borrowing", userVisible: true, dependsOn: [], status: "verified" as const,
        acceptance: [{ acceptanceId: "AC-1", title: "Borrowing passes", priority: "must" as const, status: "passed" as const }],
        evidenceCount: 1,
      }],
    };
    const hydrated = hydrateRunPresentationFromSnapshot({
      events: [event(1, "goal_graph.created", {
        ...authoritative,
        revision: 1,
        status: "active",
        activeGoalId: "G-1",
        goals: authoritative.goals.map((goal) => ({ ...goal, status: "pending", evidenceCount: 0 })),
      })],
      lastSeq: 1,
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "completed", lastSeq: 1 },
      goalGraph: authoritative,
    });

    expect(hydrated.goalGraph).toEqual(authoritative);
  });

  it("tolerates stale, out-of-order and unknown GoalGraph events", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 5 },
      goalGraph: {
        graphId: "graph-1", runId: "run-library", revision: 1, status: "active",
        productOutcome: "Readers can borrow books.", activeGoalId: "G-1", goals: [],
      },
    });

    expect(reduceDomainEvent(initial, event(4, "goal.verified", { goalId: "G-1" }))).toBe(initial);
    const unknown = reduceDomainEvent(initial, event(6, "goal.future_event", { goalId: "G-1" }));
    expect(unknown.goalGraph).toBe(initial.goalGraph);
    expect(unknown.lastSeq).toBe(6);
  });

  it("accepts the persisted GoalGraph terminal event aliases", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
      goalGraph: {
        graphId: "graph-1",
        runId: "run-library",
        revision: 1,
        status: "active",
        productOutcome: "Readers can borrow books.",
        activeGoalId: "G-1",
        goals: [{ goalId: "G-1", title: "Borrowing", userVisible: true, dependsOn: [], status: "active", acceptance: [], evidenceCount: 0 }],
      },
    });

    const failedGoal = reduceDomainEvent(initial, event(1, "goal.failed", { goalId: "G-1" }));
    expect(failedGoal.goalGraph?.goals[0]?.status).toBe("failed");
    const verifiedGraph = reduceDomainEvent(failedGoal, event(2, "goal_graph.verified", { revision: 2 }));
    expect(verifiedGraph.goalGraph).toEqual(expect.objectContaining({ status: "verified", activeGoalId: null, revision: 2 }));
  });
  it("projects ordered events into role, command, trace and version evidence", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    state = reduceDomainEvent(state, event(1, "agent.started", { title: "Clarifying lending rules" }, "product_manager"));
    state = reduceDomainEvent(state, event(2, "command.started", { commandId: "build", command: "npm run build" }, "engineer"));
    state = reduceDomainEvent(state, event(3, "command.output", { commandId: "build", chunk: "compiled successfully\n" }, "engineer"));
    state = reduceDomainEvent(state, event(4, "trace.updated", {
      items: [{
        id: "AC-BOOK-SEARCH",
        title: "Readers can search the catalogue",
        priority: "must",
        status: "passed",
        evidence: [{ id: "test-search", type: "test", label: "search.spec.ts", status: "passed" }],
      }],
    }, "reviewer"));
    state = reduceDomainEvent(state, event(5, "version.created", { versionId: "v1", commitHash: "abc1234", message: "Library catalogue ready" }, "reviewer"));

    expect(state.roles.product_manager.status).toBe("working");
    expect(state.commands).toEqual([expect.objectContaining({ id: "build", output: "compiled successfully\n", status: "running" })]);
    expect(state.trace).toEqual([expect.objectContaining({ id: "AC-BOOK-SEARCH", status: "passed" })]);
    expect(state.trace[0]?.evidence[0]).toEqual(expect.objectContaining({ id: "test-search", status: "passed" }));
    expect(state.versions).toEqual([expect.objectContaining({ id: "v1", hash: "abc1234" })]);
  });

  it("coalesces streamed public progress without presenting private reasoning", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    state = reduceDomainEvent(state, event(1, "pi.message.delta", { deltaType: "text_start", contentIndex: 0, thinking: "never public" }));
    state = reduceDomainEvent(state, event(2, "pi.message.delta", {
      deltaType: "text_delta",
      contentIndex: 0,
      delta: "I will inspect the current page. Authorization: Bearer super-secret-token",
      reasoning_content: "never public either",
    }));
    state = reduceDomainEvent(state, event(3, "pi.message.delta", { deltaType: "text_end", contentIndex: 0 }));

    expect(state.worklog).toHaveLength(1);
    expect(state.worklog[0]).toEqual(expect.objectContaining({
      kind: "progress",
      status: "running",
      title: "Agent's current judgment",
    }));
    expect(state.worklog[0]?.detail).toContain("I will inspect the current page.");
    expect(JSON.stringify(state.worklog)).not.toContain("super-secret-token");
    expect(JSON.stringify(state.worklog)).not.toContain("never public");

    state = reduceDomainEvent(state, event(4, "pi.message.completed", {
      role: "assistant",
      text: "I will inspect the current page.",
    }));
    expect(state.worklog).toHaveLength(1);
    expect(state.worklog[0]).toEqual(expect.objectContaining({ status: "completed", title: "Agent progress update" }));
    expect(state.activePublicMessageId).toBeUndefined();
    expect(domainEventToMessageChunks(event(5, "pi.message.delta", { deltaType: "text_delta", delta: "public" }))).toEqual([]);
  });

  it("projects tool, file, goal and QA events as safe, latest-first audit state", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
      goalGraph: {
        graphId: "graph-1",
        runId: "run-library",
        revision: 1,
        status: "active",
        productOutcome: "Ship a verified page",
        activeGoalId: "G-1",
        goals: [{ goalId: "G-1", title: "Build the landing page", userVisible: true, dependsOn: [], status: "active", acceptance: [], evidenceCount: 0 }],
      },
    });

    state = reduceDomainEvent(state, event(1, "pi.tool.started", {
      toolCallId: "tool-1",
      toolName: "bash",
      args: { command: "curl https://example.test -H 'Authorization: Bearer tool-secret'", payload: "x".repeat(4_000) },
      stage: "building",
    }));
    expect(state.worklog.at(-1)).toEqual(expect.objectContaining({
      id: "tool:tool-1",
      kind: "tool",
      status: "running",
      title: "Run sandbox command",
    }));
    expect(JSON.stringify(state.worklog)).not.toContain("curl");
    expect(JSON.stringify(state.commands)).not.toContain("tool-secret");
    expect(state.commands[0]?.command).toBe("Run sandbox command");

    state = reduceDomainEvent(state, event(2, "pi.tool.output", {
      toolCallId: "tool-1",
      toolName: "bash",
      text: "raw tool output must stay out of the public terminal",
    }));
    state = reduceDomainEvent(state, event(3, "pi.tool.completed", { toolCallId: "tool-1", toolName: "bash", isError: false }));
    expect(state.commands[0]?.output).toBe("");
    expect(state.worklog.at(-1)).toEqual(expect.objectContaining({ id: "tool:tool-1", status: "completed" }));

    state = reduceDomainEvent(state, event(4, "file.changed", { id: "file-1", path: "app/page.tsx", status: "modified", additions: 20, deletions: 3 }));
    state = reduceDomainEvent(state, event(5, "goal.verified", { goalId: "G-1", evidenceCount: 2 }));
    state = reduceDomainEvent(state, event(6, "verification.updated", {
      scope: "project",
      gateId: "project:build",
      name: "Production build",
      status: "passed",
      summary: "Build completed",
    }));

    expect(state.worklog.map((item) => item.kind)).toEqual(["tool", "file", "goal", "verification"]);
    expect(state.worklog.at(-1)).toEqual(expect.objectContaining({
      id: "verification:project:build",
      status: "completed",
      title: "QA passed: Production build",
    }));
    expect(domainEventToMessageChunks(event(7, "pi.command.output", { delta: "noisy" }))).toEqual([]);
  });

  it("shows the minimal top-level path emitted by the public SSE projection", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    state = reduceDomainEvent(state, event(1, "pi.tool.started", {
      toolCallId: "tool-read",
      toolName: "read",
      path: "app/page.tsx",
      stage: "building",
    }));

    expect(state.worklog.at(-1)).toEqual(expect.objectContaining({
      title: "Read file",
      detail: "app/page.tsx",
    }));
    expect(state.commands[0]?.command).toBe("Read file · app/page.tsx");
  });

  it("shows the active high-thinking and context-window contract without exposing model internals", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    state = reduceDomainEvent(state, event(1, "pi.started", {
      sessionId: "private-session-id",
      model: "private-provider-route",
      thinkingLevel: "high",
      contextWindow: 200_000,
      stage: "building",
    }));

    expect(state.worklog.at(-1)).toEqual(expect.objectContaining({
      kind: "system",
      status: "running",
      title: "Coding Agent connected",
      detail: "thinkingLevel=high · contextWindow=200000 tokens",
    }));
    expect(JSON.stringify(state.worklog)).not.toContain("private-session-id");
    expect(JSON.stringify(state.worklog)).not.toContain("private-provider-route");

    state = reduceDomainEvent(state, event(2, "pi.completed", { stage: "building" }));
    expect(state.worklog.at(-1)).toEqual(expect.objectContaining({
      status: "completed",
      detail: "thinkingLevel=high · contextWindow=200000 tokens",
    }));
  });

  it("puts only closed-set project scope verification events into the Release gate", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    state = reduceDomainEvent(state, event(1, "verification.updated", {
      scope: "project",
      gateId: "project:typecheck",
      name: "typecheck",
      status: "passed",
      summary: "passed",
    }, "reviewer"));
    // Acceptance evidence, unverified reset events and scope-less legacy
    // events fail closed and never reach the Release gate.
    state = reduceDomainEvent(state, event(2, "verification.updated", {
      scope: "acceptance",
      evidenceId: "evidence-1",
      acceptanceId: "AC-1",
      status: "failed",
    }, "reviewer"));
    state = reduceDomainEvent(state, event(3, "verification.updated", {
      scope: "acceptance",
      acceptanceId: "AC-1",
      status: "unverified",
    }, "reviewer"));
    state = reduceDomainEvent(state, event(4, "verification.updated", {
      evidenceId: "legacy",
      status: "failed",
    }, "reviewer"));

    expect(state.verifications).toEqual([
      expect.objectContaining({ id: "project:typecheck", name: "typecheck", status: "passed", detail: "passed" }),
    ]);
    expect(state.problems).toEqual([]);

    const chunks = domainEventToMessageChunks(event(5, "verification.updated", {
      scope: "acceptance",
      evidenceId: "evidence-2",
      acceptanceId: "AC-1",
      status: "passed",
    }, "reviewer"));
    expect(chunks).toEqual([]);
  });

  it("replaces a project gate result by stable gate id across repair rounds", () => {
    let state = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });
    state = reduceDomainEvent(state, event(1, "verification.updated", {
      scope: "project", gateId: "project:build", name: "build", status: "failed", summary: "build broke",
    }, "reviewer"));
    state = reduceDomainEvent(state, event(2, "verification.updated", {
      scope: "project", gateId: "project:build", name: "build", status: "passed", summary: "passed",
    }, "reviewer"));
    expect(state.verifications).toEqual([
      expect.objectContaining({ id: "project:build", status: "passed" }),
    ]);
  });

  it("drops duplicate and stale sequence numbers before mutating visible evidence", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 2 },
    });
    const stale = event(2, "version.created", { versionId: "duplicate" });

    expect(reduceDomainEvent(initial, stale)).toBe(initial);
  });

  it("maps the server's succeeded status to the completed UI terminal state", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    const next = reduceDomainEvent(initial, event(1, "run.status_changed", { status: "succeeded" }));

    expect(next.status).toBe("completed");
  });

  it("replays terminal snapshot events even when the run carries the final cursor", () => {
    const snapshot = hydrateRunPresentationFromSnapshot({
      events: [
        event(1, "agent.started", {}, "product_manager"),
        event(2, "agent.completed", {}, "product_manager"),
        event(3, "agent.started", {}, "architect"),
        event(4, "agent.completed", {}, "architect"),
        event(5, "run.failed", { summary: "Model route failed" }),
      ],
      lastSeq: 5,
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "failed", lastSeq: 5 },
    });

    expect(snapshot.status).toBe("failed");
    expect(snapshot.lastSeq).toBe(5);
    expect(snapshot.roles.product_manager.status).toBe("completed");
    expect(snapshot.roles.architect.status).toBe("completed");
  });

  it("maps a canonical artifact.upserted to the hyphenated typed AI data part", () => {
    const chunks = domainEventToMessageChunks(event(1, "artifact.upserted", {
      artifactId: "spec-1",
      kind: "product_spec",
    }, "product_manager"));

    expect(chunks).toHaveLength(1);
    expect(chunks[0]).toEqual({
      type: "data-product-spec",
      id: "artifact-spec-1",
      data: {
        id: "spec-1",
        kind: "product_spec",
        runId: "run-library",
        role: "product_manager",
      },
    });
  });

  it("maps a technical_spec artifact to the technical-spec data part", () => {
    const chunks = domainEventToMessageChunks(event(1, "artifact.upserted", {
      artifactId: "spec-2",
      kind: "technical_spec",
    }, "architect"));

    expect(chunks).toEqual([expect.objectContaining({ type: "data-technical-spec", id: "artifact-spec-2" })]);
  });

  it("never maps a legacy artifactType payload or a hidden kind to a data part", () => {
    expect(domainEventToMessageChunks(event(1, "artifact.upserted", {
      artifactType: "product-spec",
      artifactId: "spec-1",
      title: "Library product specification",
      markdown: "# Scope",
    }, "product_manager"))).toEqual([]);
    expect(domainEventToMessageChunks(event(2, "artifact.upserted", {
      artifactId: "spec-2",
      kind: "implementation_plan",
    }, "engineer"))).toEqual([]);
    expect(domainEventToMessageChunks(event(3, "artifact.upserted", {
      kind: "product_spec",
    }, "product_manager"))).toEqual([]);
    expect(domainEventToMessageChunks(event(4, "artifact.upserted", {
      artifactId: "plan-1",
      kind: "build_plan",
    }, "pi"))).toEqual([]);
  });

  it("creates a lightweight loading ref from artifact.upserted without inventing fields", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    const next = reduceDomainEvent(initial, event(1, "artifact.upserted", {
      artifactId: "spec-1",
      kind: "product_spec",
    }, "product_manager"));

    expect(next.artifacts).toEqual([{
      id: "spec-1",
      kind: "product_spec",
      runId: "run-library",
      role: "product_manager",
    }]);
    expect(next.artifacts[0]).not.toHaveProperty("title");
    expect(next.artifacts[0]).not.toHaveProperty("summary");
    expect(next.artifacts[0]).not.toHaveProperty("schemaVersion");
    expect(next.artifacts[0]).not.toHaveProperty("createdAt");
    expect(next.artifacts[0]).not.toHaveProperty("content");
  });

  it("ignores artifact events from an older run after a run switch", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-new", projectId: "project-library", status: "running", lastSeq: 0 },
    });
    const stale: DomainEvent = {
      schemaVersion: 1,
      eventId: "event-stale",
      seq: 9,
      projectId: "project-library",
      runId: "run-old",
      kind: "artifact.upserted",
      role: "product_manager",
      occurredAt: "2026-08-07T12:00:00.000Z",
      payload: { artifactId: "spec-old", kind: "product_spec" },
    };

    const next = reduceDomainEvent(initial, stale);

    expect(next).toBe(initial);
    expect(next.artifacts).toEqual([]);
  });

  it("replaces event-derived loading refs with snapshot refs during hydration", () => {
    const hydrated = hydrateRunPresentationFromSnapshot({
      events: [event(1, "artifact.upserted", { artifactId: "spec-1", kind: "product_spec" }, "product_manager")],
      lastSeq: 1,
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "completed", lastSeq: 1 },
      artifactRefs: [{
        id: "spec-1",
        runId: "run-library",
        kind: "product_spec",
        role: "product_manager",
        schemaVersion: 1,
        title: "Library product specification",
        summary: "Readers can manage books.",
        createdAt: "2026-08-07T12:00:00.000Z",
      }],
    });

    expect(hydrated.artifacts).toEqual([expect.objectContaining({
      id: "spec-1",
      schemaVersion: 1,
      title: "Library product specification",
      summary: "Readers can manage books.",
    })]);
    expect(hydrated.artifacts[0]).not.toHaveProperty("markdown");
    expect(hydrated.artifacts[0]).not.toHaveProperty("content");
  });

  it("keeps event-derived loading refs when the snapshot carried no refs", () => {
    const hydrated = hydrateRunPresentationFromSnapshot({
      events: [event(1, "artifact.upserted", { artifactId: "spec-1", kind: "technical_spec" }, "architect")],
      lastSeq: 1,
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "completed", lastSeq: 1 },
    });

    expect(hydrated.artifacts).toEqual([{
      id: "spec-1",
      kind: "technical_spec",
      runId: "run-library",
      role: "architect",
    }]);
  });

  it("derives preview.ready runId from the trusted event envelope and consumes url and status only", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    const next = reduceDomainEvent(initial, event(1, "preview.ready", {
      url: "https://preview.example.test/app",
      origin: "https://untrusted-origin.example.test",
    }));

    expect(next.preview).toEqual({
      status: "ready",
      url: "https://preview.example.test/app",
      runId: "run-library",
      verificationStatus: "unverified",
    });
    expect(next.preview).not.toHaveProperty("origin");
  });

  it("marks preview.failed without a url and surfaces the worker error", () => {
    const initial = createRunPresentation({
      projectId: "project-library",
      run: { id: "run-library", projectId: "project-library", status: "running", lastSeq: 0 },
    });

    const next = reduceDomainEvent(initial, event(1, "preview.failed", { error: "ingress refused" }));

    expect(next.preview).toEqual({
      status: "failed",
      runId: "run-library",
      error: "ingress refused",
      verificationStatus: "unverified",
    });
  });
});
