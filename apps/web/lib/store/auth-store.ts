"use client";

import { mutate as revalidateAll } from "swr";
import { create } from "zustand";

import { ApiProblem, auth, controlPlane, type AuthUser } from "@/lib/api/client";

export type AuthMode = "signin" | "register";
export type AuthStatus = "unknown" | "guest" | "authenticated";

type AuthState = {
  status: AuthStatus;
  user?: AuthUser;
  /** True while the initial `me()` check is in flight. */
  loading: boolean;
  /** True while a register / login / logout request is in flight. */
  busy: boolean;
  /** Last actionable error from an auth action, shown in the dialog. */
  error?: string;
  /** The open dialog mode, or null when closed. */
  dialogMode: AuthMode | null;
  load: () => Promise<void>;
  register: (input: { email: string; password: string; displayName?: string }) => Promise<boolean>;
  login: (input: { email: string; password: string }) => Promise<boolean>;
  logout: () => Promise<void>;
  openDialog: (mode: AuthMode) => void;
  closeDialog: () => void;
  clearError: () => void;
};

function describeError(failure: unknown): string {
  if (failure instanceof ApiProblem) {
    if (failure.status === 409) return "An account with this email already exists. Try signing in instead.";
    if (failure.status === 401) return failure.detail || "Incorrect email or password.";
    if (failure.status === 422) return failure.detail || "Check your email and password, then try again.";
    return failure.detail || failure.title || "Something went wrong. Please try again.";
  }
  return failure instanceof Error ? failure.message : "Something went wrong. Please try again.";
}

// Guard the initial `me()` so concurrent mounts (and StrictMode) fetch once.
let loadStarted = false;

export const useAuthStore = create<AuthState>((set, get) => ({
  status: "unknown",
  loading: false,
  busy: false,
  dialogMode: null,

  load: async () => {
    if (loadStarted) return;
    loadStarted = true;
    set({ loading: true });
    try {
      const user = await auth.me();
      if (user) {
        set({ status: "authenticated", user, loading: false });
        return;
      }
      set({ status: "guest", loading: false });
    } catch {
      // A failed identity check must never dead-end the UI: fall back to guest
      // and let the next guarded request bootstrap a session if needed.
      set({ status: "guest", loading: false });
    }
  },

  register: async (input) => {
    set({ busy: true, error: undefined });
    try {
      const session = await auth.register(input);
      set({ status: "authenticated", user: session.user, busy: false, error: undefined, dialogMode: null });
      // Projects owned by the prior guest token are transferred to this
      // account; refresh the caches so they appear immediately.
      void revalidateAll(() => true);
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
      set({ status: "authenticated", user: session.user, busy: false, error: undefined, dialogMode: null });
      void revalidateAll(() => true);
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
    // Only after a confirmed server logout do we re-establish a guest session
    // and refresh the project caches under the new guest token.
    try {
      await controlPlane.createGuestSession();
    } catch {
      // If the guest bootstrap fails now, the next guarded request retries it.
    }
    set({ status: "guest", user: undefined, busy: false, error: undefined, dialogMode: null });
    void revalidateAll(() => true);
  },

  openDialog: (mode) => set({ dialogMode: mode, error: undefined }),
  closeDialog: () => set({ dialogMode: null, error: undefined }),
  clearError: () => set({ error: undefined }),
}));

export const isAuthenticated = (state: AuthState): boolean => state.status === "authenticated";
