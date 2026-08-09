"use client";

import { CircleAlertIcon, LoaderCircleIcon, LogInIcon, UserPlusIcon } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useAuthStore } from "@/lib/store/auth-store";

type FieldErrors = {
  email?: string;
  password?: string;
  displayName?: string;
};

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PASSWORD_MIN = 8;
const PASSWORD_MAX = 128;
const DISPLAY_NAME_MAX = 100;

function validate(email: string, password: string, displayName: string, isRegister: boolean): FieldErrors {
  const errors: FieldErrors = {};
  const trimmedEmail = email.trim();
  if (!trimmedEmail) errors.email = "Enter your email address.";
  else if (!EMAIL_PATTERN.test(trimmedEmail)) errors.email = "Enter a valid email address.";

  if (!password) errors.password = "Enter a password.";
  else if (password.length < PASSWORD_MIN || password.length > PASSWORD_MAX) {
    errors.password = `Password must be ${PASSWORD_MIN}–${PASSWORD_MAX} characters.`;
  }

  if (isRegister && displayName.length > DISPLAY_NAME_MAX) {
    errors.displayName = `Display name must be ${DISPLAY_NAME_MAX} characters or fewer.`;
  }
  return errors;
}

export function AuthDialog() {
  const dialogMode = useAuthStore((state) => state.dialogMode);
  const busy = useAuthStore((state) => state.busy);
  const error = useAuthStore((state) => state.error);
  const register = useAuthStore((state) => state.register);
  const login = useAuthStore((state) => state.login);
  const closeDialog = useAuthStore((state) => state.closeDialog);

  const isRegister = dialogMode === "register";
  const open = dialogMode !== null;

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const passwordRef = useRef<HTMLInputElement>(null);

  const emailErrorId = useId();
  const passwordErrorId = useId();
  const passwordHintId = useId();
  const displayErrorId = useId();
  const formErrorId = useId();

  // Reset the form each time the dialog opens so a previous session's values
  // and errors never leak into the next attempt.
  useEffect(() => {
    if (open) {
      setEmail("");
      setPassword("");
      setDisplayName("");
      setFieldErrors({});
    }
  }, [open]);

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const errors = validate(email, password, displayName, isRegister);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) {
      if (errors.email) return;
      passwordRef.current?.focus();
      return;
    }
    if (isRegister) {
      await register({ email: email.trim(), password, displayName: displayName.trim() || undefined });
    } else {
      await login({ email: email.trim(), password });
    }
  };

  return (
    <Dialog
      onOpenChange={(next) => {
        if (!next) closeDialog();
      }}
      open={open}
    >
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{isRegister ? "Create your account" : "Sign in"}</DialogTitle>
          <DialogDescription>
            {isRegister
              ? "Register once to keep your projects across sessions. FOMO never sees your password."
              : "Welcome back. Sign in to pick up your projects."}
          </DialogDescription>
        </DialogHeader>

        <form className="space-y-4" noValidate onSubmit={onSubmit}>
          {error ? (
            <div
              aria-live="polite"
              className="flex items-start gap-2 rounded-lg border border-destructive/25 bg-destructive/5 p-3 text-sm text-destructive"
              id={formErrorId}
              role="alert"
            >
              <CircleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span>{error}</span>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <label className="text-xs font-medium" htmlFor="auth-email">Email</label>
            <Input
              aria-describedby={fieldErrors.email ? emailErrorId : undefined}
              aria-invalid={Boolean(fieldErrors.email)}
              autoComplete="email"
              disabled={busy}
              id="auth-email"
              inputMode="email"
              onChange={(event) => setEmail(event.target.value)}
              placeholder="you@example.com"
              type="email"
              value={email}
            />
            {fieldErrors.email ? (
              <p className="text-xs text-destructive" id={emailErrorId} role="alert">{fieldErrors.email}</p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium" htmlFor="auth-password">Password</label>
            <Input
              ref={passwordRef}
              aria-describedby={[
                fieldErrors.password ? passwordErrorId : null,
                !fieldErrors.password ? passwordHintId : null,
              ].filter(Boolean).join(" ") || undefined}
              aria-invalid={Boolean(fieldErrors.password)}
              autoComplete={isRegister ? "new-password" : "current-password"}
              disabled={busy}
              id="auth-password"
              onChange={(event) => setPassword(event.target.value)}
              placeholder="••••••••"
              type="password"
              value={password}
            />
            {fieldErrors.password ? (
              <p className="text-xs text-destructive" id={passwordErrorId} role="alert">{fieldErrors.password}</p>
            ) : (
              <p className="text-xs text-muted-foreground" id={passwordHintId}>
                {PASSWORD_MIN}–{PASSWORD_MAX} characters.
              </p>
            )}
          </div>

          {isRegister ? (
            <div className="space-y-1.5">
              <label className="text-xs font-medium" htmlFor="auth-display-name">
                Display name <span className="font-normal text-muted-foreground">(optional)</span>
              </label>
              <Input
                aria-describedby={fieldErrors.displayName ? displayErrorId : undefined}
                aria-invalid={Boolean(fieldErrors.displayName)}
                autoComplete="name"
                disabled={busy}
                id="auth-display-name"
                maxLength={DISPLAY_NAME_MAX}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="How FOMO should greet you"
                value={displayName}
              />
              {fieldErrors.displayName ? (
                <p className="text-xs text-destructive" id={displayErrorId} role="alert">{fieldErrors.displayName}</p>
              ) : null}
            </div>
          ) : null}

          <DialogFooter>
            <Button
              aria-busy={busy}
              className="w-full"
              disabled={busy}
              type="submit"
            >
              {busy ? (
                <LoaderCircleIcon aria-hidden="true" className="size-4 animate-spin" />
              ) : isRegister ? (
                <UserPlusIcon aria-hidden="true" className="size-4" />
              ) : (
                <LogInIcon aria-hidden="true" className="size-4" />
              )}
              {isRegister ? "Create account" : "Sign in"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
