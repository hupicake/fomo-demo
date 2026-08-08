import { describe, expect, it } from "vitest";

import type { DomainEvent } from "@/lib/contracts";
import { createRunPresentation, domainEventToMessageChunks, hydrateRunPresentationFromSnapshot, reduceDomainEvent } from "@/lib/events/reducer";

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
    });
  });
});
