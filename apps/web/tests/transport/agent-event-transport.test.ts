import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentEventTransport } from "@/lib/transport/agent-event-transport";

afterEach(() => vi.unstubAllGlobals());

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

  it("forwards the resolved runtime selection and surfaces the immutable runtime", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      JSON.stringify({
        message: { id: "message-2", role: "user", content: "Refactor" },
        run: {
          id: "run-2",
          project_id: "project-2",
          status: "queued",
          last_seq: 1,
          runtime: {
            profile_id: "gpt-5.6",
            thinking: "low",
            context_window: 250_000,
            policy_version: "direct-pi-runtime-v1",
            run_token_budget: 600_000,
            inference_tpm_limit: 1_000_000,
          },
        },
      }),
      { headers: { "Content-Type": "application/json" }, status: 202 },
    ));
    vi.stubGlobal("fetch", fetchMock);
    const onRunStarted = vi.fn();
    const getRuntimeSelection = vi.fn((clientMessageId: string) => ({ profileId: "gpt-5.6", thinking: "low" }));

    const transport = new AgentEventTransport({
      getLastSeq: () => 0,
      getRuntimeSelection,
      onEvent: vi.fn(),
      onRunStarted,
      projectId: "project-2",
    });

    await transport.sendMessages({
      abortSignal: undefined,
      messages: [{ id: "message-2", role: "user", parts: [{ type: "text", text: "Refactor" }] }],
    } as Parameters<typeof transport.sendMessages>[0]);

    expect(getRuntimeSelection).toHaveBeenCalledWith("message-2");
    const body = JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body));
    expect(body).toMatchObject({ profileId: "gpt-5.6", thinking: "low" });
    expect(onRunStarted).toHaveBeenCalledWith("run-2", expect.objectContaining({ profileId: "gpt-5.6", thinking: "low" }));
  });
});
