import { normalizeEvent } from "@/lib/api/client";
import type { DomainEvent } from "@/lib/contracts";

export type SseFrame = {
  data: string;
  event?: string;
  id?: string;
};

type FrameBuilder = { data: string[]; event?: string; id?: string };

function emptyBuilder(): FrameBuilder {
  return { data: [] };
}

/** Small standards-compliant SSE parser that does not depend on EventSource. */
export class SseParser {
  private buffer = "";
  private frame: FrameBuilder = emptyBuilder();

  constructor(private readonly onFrame: (frame: SseFrame) => void) {}

  feed(chunk: string): void {
    this.buffer += chunk.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const lines = this.buffer.split("\n");
    this.buffer = lines.pop() || "";
    for (const line of lines) {
      this.consumeLine(line);
    }
  }

  finish(): void {
    if (this.buffer) {
      this.consumeLine(this.buffer);
    }
    this.emit();
    this.buffer = "";
  }

  private consumeLine(line: string): void {
    if (!line) {
      this.emit();
      return;
    }
    if (line.startsWith(":")) {
      return;
    }
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "data") {
      this.frame.data.push(value);
    } else if (field === "event") {
      this.frame.event = value;
    } else if (field === "id") {
      this.frame.id = value;
    }
  }

  private emit(): void {
    if (this.frame.data.length > 0) {
      this.onFrame({
        data: this.frame.data.join("\n"),
        event: this.frame.event,
        id: this.frame.id,
      });
    }
    this.frame = emptyBuilder();
  }
}

export async function consumeDomainEventStream(
  response: Response,
  onEvent: (event: DomainEvent) => void,
): Promise<void> {
  if (!response.body) {
    throw new Error("SSE response did not contain a body");
  }
  const decoder = new TextDecoder();
  const parser = new SseParser((frame) => {
    const parsed = normalizeEvent(JSON.parse(frame.data));
    if (parsed) {
      onEvent(parsed);
    }
  });
  const reader = response.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    parser.feed(decoder.decode(value, { stream: true }));
  }
  parser.feed(decoder.decode());
  parser.finish();
}
