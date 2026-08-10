"use client";

import { useEffect } from "react";

import { useAuthStore } from "@/lib/store/auth-store";

/**
 * Loads the signed-in identity once on the client. Auth now lives on the
 * dedicated `/login` route instead of a global dialog, so this component
 * only triggers the initial `me()` check.
 */
export function AuthBootstrap({ children }: { children: React.ReactNode }) {
  const load = useAuthStore((state) => state.load);
  useEffect(() => {
    void load();
  }, [load]);
  return <>{children}</>;
}
