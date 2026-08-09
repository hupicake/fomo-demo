"use client";

import { useEffect } from "react";

import { AuthDialog } from "@/components/auth/auth-dialog";
import { useAuthStore } from "@/lib/store/auth-store";

/**
 * Loads the signed-in identity once on the client and mounts the single auth
 * dialog. Keeping the dialog at the root means every `AccountEntry` drives the
 * same modal instead of each rendering its own.
 */
export function AuthBootstrap({ children }: { children: React.ReactNode }) {
  const load = useAuthStore((state) => state.load);
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <>
      {children}
      <AuthDialog />
    </>
  );
}
