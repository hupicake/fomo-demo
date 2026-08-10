import type { ChatTransport, UIMessageChunk } from "ai";

import { ApiProblem, controlPlane, controlPlaneUrl } from "@/lib/api/client";
import type { AgentDataParts, AgentMessageMetadata, AgentUIMessage, DomainEvent, RunRuntimeResponse } from "@/lib/contracts";
import { domainEventToMessageChunks } from "@/lib/events/reducer";
import { consumeDomainEventStream } from "@/lib/events/sse";

type RuntimeSelection = { profileId?: string; thinking?: string };

type TransportOptions = {
  getLastSeq: () => number;
  /** Resolves the caller's current model/thinking choice for a message id. */
  getRuntimeSelection?: (clientMessageId: string) => RuntimeSelection;
  onConnectionChange?: (connected: boolean, message?: string) => void;
  onEvent: (event: DomainEvent) => void;
  onRunStarted: (runId: string, runtime?: RunRuntimeResponse) => void;
  projectId: string;
};

function latestUserText(messages: AgentUIMessage[]): { id: string; text: string } {
  const message = [...messages].reverse().find((item) => item.role === "user");
  if (!message) {
    throw new Error("No user message was available to start the run.");
  }
  const text = message.parts
    .filter((part): part is Extract<typeof part, { type: "text" }> => part.type === "text")
    .map((part) => part.text)
    .join("\n")
    .trim();
  if (!text) {
    throw new Error("Enter a request before starting a run.");
  }
  return { id: message.id, text };
}

function streamError(error: unknown): Error {
  if (error instanceof ApiProblem) {
    return new Error(error.detail || error.title);
  }
  if (error instanceof Error) {
    return error;
  }
  return new Error("The event stream could not be opened.");
}

function isTerminalEvent(event: DomainEvent): boolean {
  if (["run.completed", "run.failed", "run.cancelled"].includes(event.kind)) {
    return true;
  }
  return ["succeeded", "completed", "failed", "cancelled", "needs_attention"].includes(String(event.payload.status));
}

export class AgentEventTransport implements ChatTransport<AgentUIMessage> {
  private activeRunId?: string;
  private readonly terminalRunIds = new Set<string>();

  constructor(private readonly options: TransportOptions) {}

  hydrate(runId: string | undefined): void {
    this.activeRunId = runId;
  }

  async sendMessages({
    messages,
    abortSignal,
  }: Parameters<ChatTransport<AgentUIMessage>["sendMessages"]>[0]): Promise<ReadableStream<UIMessageChunk>> {
    const message = latestUserText(messages);
    const selection = this.options.getRuntimeSelection?.(message.id) || {};
    const run = await controlPlane.startRun(this.options.projectId, {
      clientMessageId: message.id,
      content: message.text,
      ...(selection.profileId ? { profileId: selection.profileId, thinking: selection.thinking } : {}),
    });
    this.activeRunId = run.runId;
    this.terminalRunIds.delete(run.runId);
    this.options.onRunStarted(run.runId, run.runtime);
    // Sequence numbers are scoped to a run. A new run always starts at cursor zero.
    return this.open(run.runId, 0, abortSignal);
  }

  async reconnectToStream({
    abortSignal,
  }: Parameters<ChatTransport<AgentUIMessage>["reconnectToStream"]>[0]): Promise<ReadableStream<UIMessageChunk> | null> {
    if (!this.activeRunId || this.terminalRunIds.has(this.activeRunId)) {
      return null;
    }
    return this.open(this.activeRunId, this.options.getLastSeq(), abortSignal);
  }

  private open(
    runId: string,
    after: number,
    abortSignal?: AbortSignal,
  ): ReadableStream<UIMessageChunk<AgentMessageMetadata, AgentDataParts>> {
    return new ReadableStream<UIMessageChunk<AgentMessageMetadata, AgentDataParts>>({
      start: async (controller) => {
        controller.enqueue({ type: "start", messageId: `run-${runId}` });
        let reachedTerminalState = false;
        try {
          const url = new URL(controlPlaneUrl(`/runs/${encodeURIComponent(runId)}/events`));
          url.searchParams.set("after", String(after));
          const response = await fetch(url, {
            headers: {
              Accept: "text/event-stream",
              "Last-Event-ID": String(after),
            },
            credentials: "include",
            signal: abortSignal,
          });
          if (!response.ok) {
            throw new ApiProblem({ status: response.status, title: "Unable to reconnect to the event stream" });
          }
          this.options.onConnectionChange?.(true);
          await consumeDomainEventStream(response, (event) => {
            if (isTerminalEvent(event)) {
              reachedTerminalState = true;
              this.terminalRunIds.add(event.runId);
            }
            this.options.onEvent(event);
            for (const chunk of domainEventToMessageChunks(event)) {
              controller.enqueue(chunk);
            }
          });
          if (reachedTerminalState) {
            this.options.onConnectionChange?.(true);
          } else {
            this.options.onConnectionChange?.(false, "The event stream closed. You can reconnect safely from the last event.");
          }
        } catch (error) {
          const readableError = streamError(error);
          this.options.onConnectionChange?.(false, readableError.message);
          if (!abortSignal?.aborted) {
            controller.enqueue({ type: "error", errorText: readableError.message });
          }
        } finally {
          controller.enqueue({ type: "finish" });
          controller.close();
        }
      },
    });
  }
}
