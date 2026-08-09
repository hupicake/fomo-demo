"use client";

import { CheckCircle2Icon, CircleHelpIcon, LoaderCircleIcon, SendIcon } from "lucide-react";
import { useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { UserInputAnswerInput, UserInputRequest } from "@/lib/contracts";
import { cn } from "@/lib/utils";

export type ClarificationAnswerHandler = (
  requestId: string,
  input: UserInputAnswerInput,
) => Promise<void>;

function newClientMessageId(requestId: string): string {
  return globalThis.crypto?.randomUUID?.()
    || `clarification-${requestId}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const statusLabels: Record<UserInputRequest["status"], string> = {
  answered: "Answered",
  cancelled: "Cancelled",
  expired: "Expired",
  pending: "Needs your input",
};

export function ClarificationCard({
  onAnswer,
  request,
}: {
  onAnswer: ClarificationAnswerHandler;
  request: UserInputRequest;
}) {
  const [selectedChoice, setSelectedChoice] = useState("");
  const [freeform, setFreeform] = useState("");
  const [error, setError] = useState<string>();
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const attemptRef = useRef<{ answer: string; clientMessageId: string } | undefined>(undefined);
  const succeededRef = useRef(false);

  const answer = (freeform.trim() || selectedChoice).trim();
  const canSubmit = request.status === "pending" && Boolean(answer) && !submitting;

  const submit = async () => {
    if (!canSubmit || submittingRef.current || succeededRef.current) return;
    const attempt = attemptRef.current?.answer === answer
      ? attemptRef.current
      : { answer, clientMessageId: newClientMessageId(request.id) };
    attemptRef.current = attempt;
    submittingRef.current = true;
    setSubmitting(true);
    setError(undefined);
    try {
      await onAnswer(request.id, attempt);
      succeededRef.current = true;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not submit your answer. Try again.");
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  const resolved = request.status !== "pending";
  return (
    <article
      aria-labelledby={`clarification-${request.id}-question`}
      className={cn(
        "relative ml-1 rounded-xl border p-3 shadow-sm",
        resolved ? "bg-muted/30" : "border-amber-500/30 bg-amber-500/[0.04]",
      )}
      data-status={request.status}
    >
      <div className="flex items-start gap-2.5">
        <span className={cn(
          "mt-0.5 grid size-7 shrink-0 place-items-center rounded-full",
          resolved ? "bg-muted text-muted-foreground" : "bg-amber-500/15 text-amber-700",
        )}>
          {request.status === "answered"
            ? <CheckCircle2Icon aria-hidden="true" className="size-4" />
            : <CircleHelpIcon aria-hidden="true" className="size-4" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={resolved ? "secondary" : "outline"}>{statusLabels[request.status]}</Badge>
            <span className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">{request.stage}</span>
          </div>
          <h3 className="mt-2 whitespace-pre-wrap break-words text-sm font-medium leading-6" id={`clarification-${request.id}-question`}>
            {request.question}
          </h3>

          {!resolved ? (
            <fieldset className="mt-3 space-y-3" disabled={submitting}>
              <legend className="sr-only">Answer clarification: {request.question}</legend>
              {request.choices.length > 0 ? (
                <div>
                  <p className="mb-1.5 text-xs text-muted-foreground" id={`clarification-${request.id}-choices`}>Choose an answer</p>
                  <div aria-labelledby={`clarification-${request.id}-choices`} className="flex flex-wrap gap-2" role="group">
                    {request.choices.map((choice) => (
                      <Button
                        aria-pressed={selectedChoice === choice}
                        className="h-auto min-h-8 whitespace-normal text-left"
                        key={choice}
                        onClick={() => {
                          setSelectedChoice(choice);
                          setFreeform("");
                          setError(undefined);
                        }}
                        size="sm"
                        type="button"
                        variant={selectedChoice === choice ? "secondary" : "outline"}
                      >
                        {choice}
                      </Button>
                    ))}
                  </div>
                </div>
              ) : null}

              {request.allowFreeform ? (
                <div>
                  <label className="mb-1.5 block text-xs text-muted-foreground" htmlFor={`clarification-${request.id}-answer`}>
                    {request.choices.length > 0 ? "Or write an answer" : "Your answer"}
                  </label>
                  <Textarea
                    aria-describedby={error ? `clarification-${request.id}-error` : undefined}
                    id={`clarification-${request.id}-answer`}
                    maxLength={50_000}
                    onChange={(event) => {
                      setFreeform(event.target.value);
                      if (event.target.value) setSelectedChoice("");
                      setError(undefined);
                    }}
                    placeholder="Add the missing detail…"
                    value={freeform}
                  />
                </div>
              ) : null}

              {error ? (
                <p className="text-xs leading-5 text-destructive" id={`clarification-${request.id}-error`} role="alert">
                  {error}
                </p>
              ) : null}

              <div className="flex items-center justify-between gap-3">
                <p className="text-[11px] leading-4 text-muted-foreground">
                  The Coding Agent will continue this run after your answer.
                </p>
                <Button disabled={!canSubmit} onClick={() => void submit()} size="sm" type="button">
                  {submitting
                    ? <LoaderCircleIcon aria-hidden="true" className="animate-spin" />
                    : <SendIcon aria-hidden="true" />}
                  {submitting ? "Sending…" : "Continue"}
                </Button>
              </div>
            </fieldset>
          ) : (
            <p className="mt-2 text-xs leading-5 text-muted-foreground">
              {request.status === "answered"
                ? "Your answer was submitted and the run can continue."
                : "This clarification request is no longer active."}
            </p>
          )}
        </div>
      </div>
    </article>
  );
}
