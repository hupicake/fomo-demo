// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { AgentActivityPanel } from "@/components/workbench/agent-activity-panel";
import type { AgentWorklogItem, UserInputRequest } from "@/lib/contracts";

afterEach(cleanup);

function worklogItem(index: number, overrides: Partial<AgentWorklogItem> = {}): AgentWorklogItem {
  return {
    id: `action-${index}`,
    kind: "tool",
    status: "completed",
    title: `Action ${index}`,
    detail: `Detail ${index}`,
    stage: "building",
    occurredAt: `2026-08-09T09:00:${String(index).padStart(2, "0")}.000Z`,
    seq: index,
    ...overrides,
  };
}

function inputRequest(overrides: Partial<UserInputRequest> = {}): UserInputRequest {
  return {
    id: "input-1",
    runId: "run-1",
    question: "Which catalogue layout should we use?",
    choices: ["Grid", "List"],
    allowFreeform: false,
    status: "pending",
    stage: "building",
    requestedSeq: 2,
    ...overrides,
  };
}

describe("AgentActivityPanel", () => {
  it("sorts every action oldest-to-newest in one continuous worklog", () => {
    const items = [
      worklogItem(3, { title: "Running QA", kind: "verification", status: "running" }),
      worklogItem(1, { title: "Agent progress update", kind: "progress" }),
      worklogItem(2, { title: "Edit file", kind: "file" }),
    ];
    render(createElement(AgentActivityPanel, { items }));

    expect(screen.getByRole("heading", { name: "Agent 活动" })).toBeTruthy();
    expect(screen.getByText("3 条")).toBeTruthy();
    const first = screen.getByText("Agent progress update");
    const second = screen.getByText("Edit file");
    const third = screen.getByText("Running QA");
    expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(second.compareDocumentPosition(third) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("进行中")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /full history|recent only/i })).toBeNull();
    expect(screen.queryByText(/private chain-of-thought/i)).toBeNull();
  });

  it("keeps the complete history in the same stream without recent or full modes", () => {
    const items = Array.from({ length: 22 }, (_, index) => worklogItem(index));
    render(createElement(AgentActivityPanel, { items }));

    expect(screen.getByText("22 条")).toBeTruthy();
    expect(screen.getByText("Action 0")).toBeTruthy();
    expect(screen.getByText("Action 12")).toBeTruthy();
    expect(screen.getByText("Action 21")).toBeTruthy();
    expect(screen.getByText("Action 0").compareDocumentPosition(screen.getByText("Action 21")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText(/recent/i)).toBeNull();
    expect(screen.queryByText(/full history/i)).toBeNull();
  });

  it("keeps a failure reason visible without requiring hover", () => {
    render(createElement(AgentActivityPanel, {
      items: [worklogItem(1, {
        status: "failed",
        title: "Production build failed",
        detail: "TypeScript could not resolve the generated route module.",
      })],
    }));

    expect(screen.getByText("Production build failed")).toBeTruthy();
    expect(screen.getByText("TypeScript could not resolve the generated route module.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Read details for Production build failed/ })).toBeNull();
  });

  it("provides a focusable and clickable detail trigger for long non-failure activity", async () => {
    const user = userEvent.setup();
    const detail = "Inspecting the current implementation before making a bounded change. ".repeat(3);
    render(createElement(AgentActivityPanel, {
      items: [worklogItem(1, { kind: "progress", status: "running", title: "Reviewing the page structure", detail })],
    }));

    const trigger = screen.getByRole("button", { name: "Read details for Reviewing the page structure" });
    trigger.focus();
    expect(document.activeElement).toBe(trigger);
    await user.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
  });

  it("shows a concise empty state before the first public event", () => {
    render(createElement(AgentActivityPanel, { items: [] }));

    expect(screen.getByText("当工作开始时，活动将显示在这里。")).toBeTruthy();
    expect(screen.queryByText("进行中")).toBeNull();
  });

  it("keeps a pending clarification at the tail, then lets later activity append after it", () => {
    const onAnswer = async () => undefined;
    const { rerender } = render(createElement(AgentActivityPanel, {
      inputRequests: [inputRequest()],
      items: [worklogItem(1), worklogItem(3)],
      onAnswer,
    }));

    const pendingQuestion = screen.getByRole("heading", { name: "Which catalogue layout should we use?" });
    expect(screen.getByText("Action 3").compareDocumentPosition(pendingQuestion) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("3 条")).toBeTruthy();

    rerender(createElement(AgentActivityPanel, {
      inputRequests: [inputRequest({ status: "answered", resolvedSeq: 2 })],
      items: [worklogItem(1), worklogItem(3)],
      onAnswer,
    }));

    const answeredQuestion = screen.getByRole("heading", { name: "Which catalogue layout should we use?" });
    expect(answeredQuestion.compareDocumentPosition(screen.getByText("Action 3")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("Answered")).toBeTruthy();
  });
});
