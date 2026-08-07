import { describe, expect, it } from "vitest";

import { consumeDomainEventStream, SseParser } from "@/lib/events/sse";

describe("SSE event handling", () => {
  it("handles comments, CRLF framing and multi-line data", () => {
    const frames: Array<{ data: string; event?: string; id?: string }> = [];
    const parser = new SseParser((frame) => frames.push(frame));

    parser.feed(": heartbeat\r\nid: 7\r\nevent: domain\r\ndata: first line\r\n");
    parser.feed("data: second line\r\n\r\n");
    parser.finish();

    expect(frames).toEqual([{ id: "7", event: "domain", data: "first line\nsecond line" }]);
  });

  it("normalizes a streamed domain event before delivering it", async () => {
    const source = [
      "id: 4\n",
      "data: {\"schemaVersion\":1,\"eventId\":\"evt-4\",\"seq\":4,\"projectId\":\"project-library\",\"runId\":\"run-library\",\"kind\":\"run.completed\",\"occurredAt\":\"2026-08-07T12:00:00Z\",\"payload\":{}}\n\n",
    ];
    const encoder = new TextEncoder();
    const response = new Response(new ReadableStream({
      start(controller) {
        for (const item of source) controller.enqueue(encoder.encode(item));
        controller.close();
      },
    }));
    const received: string[] = [];

    await consumeDomainEventStream(response, (event) => received.push(event.kind));

    expect(received).toEqual(["run.completed"]);
  });
});
