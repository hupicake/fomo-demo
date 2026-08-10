"use client";

import { ArrowLeftIcon, CircleAlertIcon, LoaderCircleIcon, LogInIcon, SparklesIcon, UserPlusIcon } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuthStore, type AuthMode } from "@/lib/store/auth-store";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PASSWORD_MIN = 8;
const PASSWORD_MAX = 128;
const DISPLAY_NAME_MAX = 100;
const REDIRECT_BASE = new URL("https://fomo.invalid");
const NEXT_DEVELOPMENT = process.env.NODE_ENV === "development";
const REGISTRATION_ENABLED = NEXT_DEVELOPMENT;
const DEVELOPMENT_EMAIL = process.env.NEXT_PUBLIC_DEV_ACCOUNT_EMAIL
  || (NEXT_DEVELOPMENT ? "dev@fomo.local" : "");
const DEVELOPMENT_PASSWORD = process.env.NEXT_PUBLIC_DEV_ACCOUNT_PASSWORD
  || (NEXT_DEVELOPMENT ? "fomo-dev-password" : "");
const PREFILL_DEVELOPMENT_ACCOUNT = Boolean(DEVELOPMENT_EMAIL && DEVELOPMENT_PASSWORD);

type FieldErrors = {
  email?: string;
  password?: string;
  displayName?: string;
};

function safeRedirectPath(rawRedirect: string | null): string {
  if (!rawRedirect?.startsWith("/") || rawRedirect.includes("\\")) return "/";
  try {
    const target = new URL(rawRedirect, REDIRECT_BASE);
    if (target.origin !== REDIRECT_BASE.origin) return "/";
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return "/";
  }
}

function validate(email: string, password: string, displayName: string, isRegister: boolean): FieldErrors {
  const errors: FieldErrors = {};
  const trimmedEmail = email.trim();
  if (!trimmedEmail) errors.email = "请输入邮箱地址。";
  else if (!EMAIL_PATTERN.test(trimmedEmail)) errors.email = "请输入有效的邮箱地址。";

  if (!password) errors.password = "请输入密码。";
  else if (password.length < PASSWORD_MIN || password.length > PASSWORD_MAX) {
    errors.password = `密码长度需为 ${PASSWORD_MIN}–${PASSWORD_MAX} 个字符。`;
  }

  if (isRegister && displayName.length > DISPLAY_NAME_MAX) {
    errors.displayName = `显示名称不超过 ${DISPLAY_NAME_MAX} 个字符。`;
  }
  return errors;
}

function LoginForm({ initialMode, redirectTo }: { initialMode: AuthMode; redirectTo: string }) {
  const router = useRouter();
  const status = useAuthStore((state) => state.status);
  const loading = useAuthStore((state) => state.loading);
  const busy = useAuthStore((state) => state.busy);
  const error = useAuthStore((state) => state.error);
  const register = useAuthStore((state) => state.register);
  const login = useAuthStore((state) => state.login);
  const clearError = useAuthStore((state) => state.clearError);

  const [mode, setMode] = useState<AuthMode>(initialMode);
  const [email, setEmail] = useState(PREFILL_DEVELOPMENT_ACCOUNT ? DEVELOPMENT_EMAIL : "");
  const [password, setPassword] = useState(PREFILL_DEVELOPMENT_ACCOUNT ? DEVELOPMENT_PASSWORD : "");
  const [displayName, setDisplayName] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const passwordRef = useRef<HTMLInputElement>(null);

  const emailErrorId = useId();
  const passwordErrorId = useId();
  const passwordHintId = useId();
  const displayErrorId = useId();
  const formErrorId = useId();

  const isRegister = REGISTRATION_ENABLED && mode === "register";

  // Already authenticated (e.g. opened /login in a second tab) — go home.
  useEffect(() => {
    if (!loading && status === "authenticated") {
      router.replace(redirectTo || "/");
    }
  }, [loading, status, router, redirectTo]);

  const switchMode = (next: AuthMode) => {
    setMode(next);
    setFieldErrors({});
    clearError();
  };

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const errors = validate(email, password, displayName, isRegister);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) {
      if (errors.email) return;
      passwordRef.current?.focus();
      return;
    }
    const ok = isRegister
      ? await register({ email: email.trim(), password, displayName: displayName.trim() || undefined })
      : await login({ email: email.trim(), password });
    if (ok) {
      router.replace(redirectTo || "/");
    }
  };

  return (
    <div className="w-full max-w-sm">
      <div className="mb-8 flex items-center gap-2">
        <Link className="flex items-center gap-2 font-semibold tracking-tight" href="/">
          <span className="grid size-8 place-items-center rounded-lg bg-foreground font-mono text-sm text-background">F</span>
          FOMO
        </Link>
      </div>

      <div className="rounded-2xl border bg-card p-7 shadow-[0_20px_70px_-35px_rgba(15,23,42,0.38)]">
        <h1 className="text-xl font-semibold tracking-tight">
          {isRegister ? "创建账号" : "欢迎回来"}
        </h1>
        <p className="mt-1.5 text-sm text-muted-foreground">
          {isRegister
            ? "注册一次，跨会话保留你的项目。密码仅用于校验，不会被存储在客户端。"
            : "登录后继续你的项目。密码仅用于校验，不会被存储在客户端。"}
        </p>

        <form className="mt-6 space-y-4" noValidate onSubmit={onSubmit}>
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
            <label className="text-xs font-medium" htmlFor="auth-email">邮箱</label>
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
            <label className="text-xs font-medium" htmlFor="auth-password">密码</label>
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
                {PASSWORD_MIN}–{PASSWORD_MAX} 个字符。
              </p>
            )}
          </div>

          {isRegister ? (
            <div className="space-y-1.5">
              <label className="text-xs font-medium" htmlFor="auth-display-name">
                显示名称 <span className="font-normal text-muted-foreground">（可选）</span>
              </label>
              <Input
                aria-describedby={fieldErrors.displayName ? displayErrorId : undefined}
                aria-invalid={Boolean(fieldErrors.displayName)}
                autoComplete="name"
                disabled={busy}
                id="auth-display-name"
                maxLength={DISPLAY_NAME_MAX}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="希望我们怎么称呼你"
                value={displayName}
              />
              {fieldErrors.displayName ? (
                <p className="text-xs text-destructive" id={displayErrorId} role="alert">{fieldErrors.displayName}</p>
              ) : null}
            </div>
          ) : null}

          <Button
            aria-busy={busy}
            className="w-full"
            disabled={busy || loading}
            type="submit"
          >
            {busy ? (
              <LoaderCircleIcon aria-hidden="true" className="size-4 animate-spin" />
            ) : isRegister ? (
              <UserPlusIcon aria-hidden="true" className="size-4" />
            ) : (
              <LogInIcon aria-hidden="true" className="size-4" />
            )}
            {isRegister ? "创建账号" : "登录"}
          </Button>
        </form>

        <div className="mt-5 border-t pt-4 text-center text-sm text-muted-foreground">
          {!REGISTRATION_ENABLED ? (
            <span>当前仅开放受邀账号登录。</span>
          ) : isRegister ? (
            <>
              已有账号？
              <button
                className="ml-1 font-medium text-foreground underline-offset-4 hover:underline"
                onClick={() => switchMode("signin")}
                type="button"
              >
                直接登录
              </button>
            </>
          ) : (
            <>
              还没有账号？
              <button
                className="ml-1 font-medium text-foreground underline-offset-4 hover:underline"
                onClick={() => switchMode("register")}
                type="button"
              >
                注册一个
              </button>
            </>
          )}
        </div>
      </div>

      <div className="mt-6 flex items-center justify-center">
        <Link
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
          href={redirectTo || "/"}
        >
          <ArrowLeftIcon aria-hidden="true" className="size-3.5" />
          返回首页
        </Link>
      </div>
    </div>
  );
}

function LoginContent() {
  const searchParams = useSearchParams();
  const redirectTo = safeRedirectPath(searchParams.get("redirect"));
  const initialMode: AuthMode = REGISTRATION_ENABLED && searchParams.get("mode") === "register"
    ? "register"
    : "signin";

  return (
    <main className="relative grid min-h-screen lg:grid-cols-2">
      {/* Brand / marketing panel */}
      <aside className="relative hidden flex-col justify-between overflow-hidden border-r bg-foreground p-12 text-background lg:flex">
        <div className="bg-grid pointer-events-none absolute inset-0 opacity-[0.07]" />
        <div className="relative">
          <Link className="flex items-center gap-2 font-semibold tracking-tight" href="/">
            <span className="grid size-8 place-items-center rounded-lg bg-background font-mono text-sm text-foreground">F</span>
            FOMO
          </Link>
        </div>
        <div className="relative max-w-md">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-background/20 bg-background/10 px-3 py-1 text-xs">
            <SparklesIcon aria-hidden="true" className="size-3.5" />
            AI 编程工作台
          </span>
          <h2 className="mt-5 text-3xl font-semibold leading-tight tracking-tight text-balance">
            描述一个页面，跟着工作日志，打开真实预览。
          </h2>
          <p className="mt-4 text-sm leading-7 text-background/70">
            一个 agent 负责规划、构建与修复你的应用，每一步都记录在可追溯的工作日志里，每次成功的运行都会产出可恢复的版本。
          </p>
        </div>
        <p className="relative text-xs text-background/50">© FOMO 编程工作台</p>
      </aside>

      {/* Form panel */}
      <section className="flex items-center justify-center p-6 sm:p-10">
        <LoginForm initialMode={initialMode} redirectTo={redirectTo} />
      </section>
    </main>
  );
}

function LoginFallback() {
  return (
    <main className="relative grid min-h-screen lg:grid-cols-2">
      <aside className="relative hidden flex-col justify-between overflow-hidden border-r bg-foreground p-12 lg:flex">
        <div className="bg-grid pointer-events-none absolute inset-0 opacity-[0.07]" />
        <div className="relative">
          <div className="flex items-center gap-2 font-semibold tracking-tight text-background">
            <span className="grid size-8 place-items-center rounded-lg bg-background font-mono text-sm text-foreground">F</span>
            FOMO
          </div>
        </div>
        <div className="relative max-w-md space-y-4">
          <Skeleton className="h-6 w-32 rounded-full" />
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-4 w-56" />
          <Skeleton className="h-4 w-48" />
        </div>
        <Skeleton className="relative h-3 w-40" />
      </aside>
      <section className="flex items-center justify-center p-6 sm:p-10">
        <Skeleton className="h-96 w-full max-w-sm rounded-2xl" />
      </section>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<LoginFallback />}>
      <LoginContent />
    </Suspense>
  );
}
