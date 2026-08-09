// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClarificationCard } from "@/components/workbench/clarification-card";
import type { UserInputRequest } from "@/lib/contracts";

afterEach(cleanup);

function request(overrides: Partial<UserInputRequest> = {}): UserInputRequest {
  return {
    id: "input-1",
    runId: "run-1",
    question: "Which catalogue layout should we use?",
    choices: ["Grid", "List"],
    allowFreeform: true,
    status: "pending",
    stage: "building",
    ...overrides,
  };
}

describe("ClarificationCard", () => {
  it("offers keyboard-accessible choices and an explicitly labelled freeform answer", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn().mockResolvedValue(undefined);
    render(createElement(ClarificationCard, { onAnswer, request: request() }));

    expect(screen.getByRole("group", { name: "Choose an answer" })).toBeTruthy();
    const grid = screen.getByRole("button", { name: "Grid" });
    await user.click(grid);
    expect(grid.getAttribute("aria-pressed")).toBe("true");

    const freeform = screen.getByRole("textbox", { name: "Or write an answer" });
    await user.type(freeform, "Use a compact grid on desktop");
    expect(grid.getAttribute("aria-pressed")).toBe("false");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(1));
    expect(onAnswer).toHaveBeenCalledWith("input-1", {
      clientMessageId: expect.any(String),
      answer: "Use a compact grid on desktop",
    });
  });

  it("retains input and reuses the client message id when a failed answer is retried", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn()
      .mockRejectedValueOnce(new Error("Network unavailable"))
      .mockResolvedValueOnce(undefined);
    render(createElement(ClarificationCard, { onAnswer, request: request({ choices: [] }) }));

    const freeform = screen.getByRole("textbox", { name: "Your answer" });
    await user.type(freeform, "Keep the current navigation");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Network unavailable");
    expect((freeform as HTMLTextAreaElement).value).toBe("Keep the current navigation");

    await user.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2));
    expect(onAnswer.mock.calls[1]?.[1].clientMessageId).toBe(onAnswer.mock.calls[0]?.[1].clientMessageId);
  });

  it("deduplicates repeated submit clicks while an answer is in flight", () => {
    let resolveAnswer: (() => void) | undefined;
    const onAnswer = vi.fn(() => new Promise<void>((resolve) => {
      resolveAnswer = resolve;
    }));
    render(createElement(ClarificationCard, { onAnswer, request: request({ allowFreeform: false }) }));

    fireEvent.click(screen.getByRole("button", { name: "Grid" }));
    const submit = screen.getByRole("button", { name: "Continue" });
    fireEvent.click(submit);
    fireEvent.click(submit);

    expect(onAnswer).toHaveBeenCalledTimes(1);
    resolveAnswer?.();
  });
});
