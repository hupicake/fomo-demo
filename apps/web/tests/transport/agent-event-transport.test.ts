import { describe, expect, it, vi } from "vitest";

import { AgentEventTransport } from "@/lib/transport/agent-event-transport";

function terminalEventStream() {
  const encoder = new TextEncoder();
  const event = {
    schemaVersion: 1,
    eventId: "event-terminal",
    seq: 5,
    projectId: "project-1",
    runId: "run-1",
    kind: "run.completed",
    occurredAt: "2026-08-07T12:00:00.000Z",
    payload: { status: "succeeded" },
  };
  return new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
      controller.close();
    },
  }));
}

describe("AgentEventTransport", () => {
  it("does not reopen an event stream after a succeeded terminal event", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(terminalEventStream());
    vi.stubGlobal("fetch", fetchMock);
    const transport = new AgentEventTransport({
      getLastSeq: () => 0,
      onEvent: vi.fn(),
      onRunStarted: vi.fn(),
      projectId: "project-1",
    });
    transport.hydrate("run-1");

    const stream = await transport.reconnectToStream({ abortSignal: undefined } as Parameters<typeof transport.reconnectToStream>[0]);
    expect(stream).not.toBeNull();
    const reader = stream?.getReader();
    while (reader && !(await reader.read()).done) {
      // Consume the stream so the transport observes the terminal event.
    }

    await expect(transport.reconnectToStream({ abortSignal: undefined } as Parameters<typeof transport.reconnectToStream>[0])).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
