"use client";

import { mutate as revalidateAll } from "swr";
import { create } from "zustand";

import { ApiProblem, auth, type AuthUser } from "@/lib/api/client";

export type AuthMode = "signin" | "register";
export type AuthStatus = "unknown" | "unauthenticated" | "authenticated";

type AuthState = {
  status: AuthStatus;
  user?: AuthUser;
  /** Changes whenever the authenticated browser session changes. */
  cacheEpoch: number;
  /** True while the initial `me()` check is in flight. */
  loading: boolean;
  /** True while a register / login / logout request is in flight. */
  busy: boolean;
  /** Last actionable error from an auth action, shown on the login page. */
  error?: string;
  load: () => Promise<void>;
  register: (input: { email: string; password: string; displayName?: string }) => Promise<boolean>;
  login: (input: { email: string; password: string }) => Promise<boolean>;
  logout: () => Promise<void>;
  invalidate: () => void;
  clearError: () => void;
};

function describeError(failure: unknown): string {
  if (failure instanceof ApiProblem) {
    if (failure.status === 409) return "该邮箱已注册，请直接登录。";
    if (failure.status === 401) return failure.detail || "邮箱或密码不正确。";
    if (failure.status === 422) return failure.detail || "请检查邮箱与密码后重试。";
    return failure.detail || failure.title || "出错了，请重试。";
  }
  return failure instanceof Error ? failure.message : "出错了，请重试。";
}

// Guard the initial `me()` so concurrent mounts (and StrictMode) fetch once.
let loadStarted = false;

async function clearIdentityCache(): Promise<void> {
  await revalidateAll(() => true, undefined, { revalidate: false });
}

export const useAuthStore = create<AuthState>((set) => ({
  status: "unknown",
  cacheEpoch: 0,
  loading: false,
  busy: false,

  load: async () => {
    if (loadStarted) return;
    loadStarted = true;
    set({ loading: true });
    try {
      const user = await auth.me();
      if (user) {
        set((state) => ({
          status: "authenticated",
          user,
          loading: false,
          cacheEpoch: state.cacheEpoch + 1,
        }));
        return;
      }
      set({ status: "unauthenticated", user: undefined, loading: false });
    } catch {
      set({ status: "unauthenticated", user: undefined, loading: false });
    }
  },

  register: async (input) => {
    set({ busy: true, error: undefined });
    try {
      const session = await auth.register(input);
      await clearIdentityCache();
      set((state) => ({
        status: "authenticated",
        user: session.user,
        busy: false,
        error: undefined,
        cacheEpoch: state.cacheEpoch + 1,
      }));
      return true;
    } catch (failure) {
      set({ busy: false, error: describeError(failure) });
      return false;
    }
  },

  login: async (input) => {
    set({ busy: true, error: undefined });
    try {
      const session = await auth.login(input);
      await clearIdentityCache();
      set((state) => ({
        status: "authenticated",
        user: session.user,
        busy: false,
        error: undefined,
        cacheEpoch: state.cacheEpoch + 1,
      }));
      return true;
    } catch (failure) {
      set({ busy: false, error: describeError(failure) });
      return false;
    }
  },

  logout: async () => {
    set({ busy: true, error: undefined });
    try {
      await auth.logout();
    } catch (failure) {
      // The authenticated cookie may still be valid on the server, so keep the
      // local identity and surface the error rather than dropping the user.
      set({ busy: false, error: describeError(failure) });
      return;
    }
    set((state) => ({
      status: "unauthenticated",
      user: undefined,
      busy: false,
      error: undefined,
      cacheEpoch: state.cacheEpoch + 1,
    }));
    await clearIdentityCache();
  },

  invalidate: () => {
    set((state) => ({
      status: "unauthenticated",
      user: undefined,
      busy: false,
      error: undefined,
      cacheEpoch: state.cacheEpoch + 1,
    }));
    void clearIdentityCache();
  },

  clearError: () => set({ error: undefined }),
}));
