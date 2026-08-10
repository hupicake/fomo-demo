// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";

import { PromptInput, PromptInputSubmit, PromptInputTextarea, type PromptInputMessage } from "@/components/ai-elements/prompt-input";
import { submitChatMessage } from "@/lib/chat/submit-message";

afterEach(() => cleanup());

describe("error-state chat submission", () => {
  it("submits the PromptInput once through a real pointer click", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();

    render(createElement(
      PromptInput,
      { onSubmit },
      createElement(PromptInputTextarea, { placeholder: "Retry the request" }),
      createElement(PromptInputSubmit, { status: "error" }),
    ));

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    await user.type(textarea, "Add reader search and loans");
    const submit = screen.getByRole("button", { name: "Submit again" });

    await user.click(submit);

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0]?.[0]?.text).toBe("Add reader search and loans");
    expect(textarea.value).toBe("");
  });

  it("recovers the chat error, submits the captured text, and keeps a submit button in error state", async () => {
    const calls: string[] = [];
    const clearError = vi.fn(() => calls.push("clear"));
    const sendMessage = vi.fn(async ({ text }: { text: string }) => {
      calls.push(`send:${text}`);
    });
    const onSubmit = async (message: PromptInputMessage) => {
      await submitChatMessage({ clearError, sendMessage, text: message.text });
    };

    render(createElement(
      PromptInput,
      { onSubmit },
      createElement(PromptInputTextarea, { placeholder: "Retry the request" }),
      createElement(PromptInputSubmit, { status: "error" }),
    ));

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "  Add reader search and loans  " } });
    const submit = screen.getByRole("button", { name: "Submit again" });
    expect(submit.getAttribute("type")).toBe("submit");

    fireEvent.click(submit);

    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith({ text: "Add reader search and loans" }));
    expect(clearError).toHaveBeenCalledTimes(1);
    expect(calls).toEqual(["clear", "send:Add reader search and loans"]);
    expect(textarea.value).toBe("");
  });

  it("keeps the submitted text when an async submission fails", async () => {
    const onSubmit = vi.fn(async () => {
      throw new Error("Coding Agent is unavailable");
    });

    render(createElement(
      PromptInput,
      { onSubmit },
      createElement(PromptInputTextarea, { placeholder: "Retry the request" }),
      createElement(PromptInputSubmit, { status: "ready" }),
    ));

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "Keep this product brief" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(textarea.value).toBe("Keep this product brief");
  });
});
