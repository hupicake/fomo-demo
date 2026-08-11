"use client";

import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import type { ChatStatus } from "ai";
import { CornerDownLeftIcon, SquareIcon } from "lucide-react";
import type {
  ComponentProps,
  FormEvent,
  HTMLAttributes,
  KeyboardEventHandler,
  MouseEvent,
} from "react";
import { useCallback, useState } from "react";

export interface PromptInputMessage {
  text: string;
}

export type PromptInputProps = Omit<ComponentProps<"form">, "onSubmit"> & {
  onSubmit: (
    message: PromptInputMessage,
    event: FormEvent<HTMLFormElement>,
  ) => void | Promise<void>;
};

/** Product composer for persisted text prompts; attachments are not part of the API contract. */
export function PromptInput({
  className,
  children,
  onSubmit,
  ...props
}: PromptInputProps) {
  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const form = event.currentTarget;
      const formData = new FormData(form);
      const text = String(formData.get("message") ?? "");

      try {
        await onSubmit({ text }, event);
      } catch {
        // Preserve the prompt so a failed submission can be retried.
        return;
      }

      const message = form.elements.namedItem("message");
      if (message instanceof HTMLTextAreaElement && message.value === text) {
        form.reset();
      }
    },
    [onSubmit],
  );

  return (
    <form
      {...props}
      className={cn("w-full", className)}
      onSubmit={handleSubmit}
    >
      <InputGroup className="overflow-hidden">{children}</InputGroup>
    </form>
  );
}

export type PromptInputTextareaProps = ComponentProps<typeof InputGroupTextarea>;

export function PromptInputTextarea({
  className,
  onCompositionEnd,
  onCompositionStart,
  onKeyDown,
  placeholder = "What would you like to know?",
  ...props
}: PromptInputTextareaProps) {
  const [isComposing, setIsComposing] = useState(false);

  const handleKeyDown: KeyboardEventHandler<HTMLTextAreaElement> = useCallback(
    (event) => {
      onKeyDown?.(event);
      if (event.defaultPrevented || event.key !== "Enter") return;
      if (isComposing || event.nativeEvent.isComposing || event.shiftKey) return;

      event.preventDefault();
      const submitButton = event.currentTarget.form?.querySelector(
        'button[type="submit"]',
      ) as HTMLButtonElement | null;
      if (!submitButton?.disabled) {
        event.currentTarget.form?.requestSubmit();
      }
    },
    [isComposing, onKeyDown],
  );

  return (
    <InputGroupTextarea
      {...props}
      className={cn("field-sizing-content max-h-48 min-h-16", className)}
      name="message"
      onCompositionEnd={(event) => {
        setIsComposing(false);
        onCompositionEnd?.(event);
      }}
      onCompositionStart={(event) => {
        setIsComposing(true);
        onCompositionStart?.(event);
      }}
      onKeyDown={handleKeyDown}
      placeholder={placeholder}
    />
  );
}

export type PromptInputFooterProps = Omit<
  ComponentProps<typeof InputGroupAddon>,
  "align"
>;

export function PromptInputFooter({
  className,
  ...props
}: PromptInputFooterProps) {
  return (
    <InputGroupAddon
      align="block-end"
      className={cn("justify-between gap-1", className)}
      {...props}
    />
  );
}

export type PromptInputToolsProps = HTMLAttributes<HTMLDivElement>;

export function PromptInputTools({ className, ...props }: PromptInputToolsProps) {
  return (
    <div
      className={cn("flex min-w-0 items-center gap-1", className)}
      {...props}
    />
  );
}

export type PromptInputSubmitProps = Omit<
  ComponentProps<typeof InputGroupButton>,
  "type"
> & {
  status?: ChatStatus;
  onStop?: () => void;
};

export function PromptInputSubmit({
  className,
  variant = "default",
  size = "icon-sm",
  status,
  onStop,
  onClick,
  children,
  disabled,
  ...props
}: PromptInputSubmitProps) {
  const isGenerating = status === "submitted" || status === "streaming";
  const isRecoveringFromError = status === "error";

  const handleClick = useCallback(
    (event: MouseEvent<HTMLButtonElement>) => {
      if (isGenerating) {
        event.preventDefault();
        onStop?.();
        return;
      }

      onClick?.(event);
      if (isGenerating || disabled || event.defaultPrevented) return;

      const form = event.currentTarget.form;
      if (form) {
        event.preventDefault();
        form.requestSubmit(event.currentTarget);
      }
    },
    [disabled, isGenerating, onClick, onStop],
  );

  const icon = status === "submitted"
    ? <Spinner />
    : status === "streaming"
      ? <SquareIcon className="size-4" />
      : <CornerDownLeftIcon className="size-4" />;

  return (
    <InputGroupButton
      {...props}
      aria-label={
        isGenerating
          ? onStop ? "Stop" : "Generating"
          : isRecoveringFromError ? "Submit again" : "Submit"
      }
      aria-busy={isGenerating}
      className={cn(className)}
      disabled={disabled}
      onClick={handleClick}
      size={size}
      type={isGenerating ? "button" : "submit"}
      variant={variant}
    >
      {children ?? icon}
    </InputGroupButton>
  );
}

export type PromptInputSelectProps = ComponentProps<typeof Select>;

export function PromptInputSelect(props: PromptInputSelectProps) {
  return <Select {...props} />;
}

export type PromptInputSelectTriggerProps = ComponentProps<typeof SelectTrigger>;

export function PromptInputSelectTrigger({
  className,
  ...props
}: PromptInputSelectTriggerProps) {
  return (
    <SelectTrigger
      className={cn(
        "border-none bg-transparent font-medium text-muted-foreground shadow-none transition-colors",
        "hover:bg-accent hover:text-foreground aria-expanded:bg-accent aria-expanded:text-foreground",
        className,
      )}
      {...props}
    />
  );
}

export type PromptInputSelectContentProps = ComponentProps<typeof SelectContent>;

export function PromptInputSelectContent({
  className,
  ...props
}: PromptInputSelectContentProps) {
  return <SelectContent className={cn(className)} {...props} />;
}

export type PromptInputSelectItemProps = ComponentProps<typeof SelectItem>;

export function PromptInputSelectItem({
  className,
  ...props
}: PromptInputSelectItemProps) {
  return <SelectItem className={cn(className)} {...props} />;
}

export type PromptInputSelectValueProps = ComponentProps<typeof SelectValue>;

export function PromptInputSelectValue({
  className,
  ...props
}: PromptInputSelectValueProps) {
  return <SelectValue className={cn(className)} {...props} />;
}
