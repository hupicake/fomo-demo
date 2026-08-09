"use client";

import {
  CircleUserRoundIcon,
  ExternalLinkIcon,
  LogInIcon,
  LogOutIcon,
  UserPlusIcon,
} from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuthStore } from "@/lib/store/auth-store";
import { cn } from "@/lib/utils";

export type ControlPlaneConnection = "online" | "degraded" | "demo";

const connectionCopy: Record<ControlPlaneConnection, { label: string; detail: string; dot: string }> = {
  online: {
    label: "Control plane connected",
    detail: "Runs, files and previews are live.",
    dot: "bg-emerald-500",
  },
  degraded: {
    label: "Control plane unreachable",
    detail: "Reconnect from the banner above to resume the event stream.",
    dot: "bg-amber-500",
  },
  demo: {
    label: "Demo fixture",
    detail: "Local sample data only — no model, sandbox or QA result.",
    dot: "bg-amber-500",
  },
};

function initials(name?: string, email?: string): string {
  const source = (name || email || "?").trim();
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return source.slice(0, 2).toUpperCase();
}

/**
 * The reserved account surface, now backed by the official auth contract.
 * When signed out it offers Sign in / Create account (which open the auth
 * dialog); when signed in it shows the identity and a Sign out action. The
 * control-plane connection the workbench already tracks is kept as a separate
 * status dot. No password is ever stored in the client — the browser relies on
 * the server-managed `fomo_session` cookie.
 */
export function AccountEntry({
  className,
  connection,
}: {
  className?: string;
  connection: ControlPlaneConnection;
}) {
  const status = connectionCopy[connection];
  const authStatus = useAuthStore((state) => state.status);
  const user = useAuthStore((state) => state.user);
  const busy = useAuthStore((state) => state.busy);
  const openDialog = useAuthStore((state) => state.openDialog);
  const logout = useAuthStore((state) => state.logout);
  const isSignedIn = authStatus === "authenticated" && Boolean(user);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={isSignedIn ? `Account: ${user?.displayName || user?.email}` : "Account and workspace status"}
          className={cn("h-8 gap-2 rounded-full border pl-1.5 pr-2.5", className)}
          data-auth-state={isSignedIn ? "signed-in" : "signed-out"}
          data-connection={connection}
          size="sm"
          variant="ghost"
        >
          <span className="relative grid size-5 place-items-center rounded-full bg-muted text-muted-foreground">
            {isSignedIn ? (
              <span aria-hidden="true" className="text-[9px] font-semibold text-foreground">{initials(user?.displayName, user?.email)}</span>
            ) : (
              <CircleUserRoundIcon aria-hidden="true" className="size-3.5" />
            )}
            <span aria-hidden="true" className={cn("absolute -bottom-0.5 -right-0.5 size-2 rounded-full ring-2 ring-card", status.dot)} />
          </span>
          <span className="hidden max-w-28 truncate text-[11px] font-medium sm:inline">
            {isSignedIn ? (user?.displayName || user?.email) : "Guest"}
          </span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="flex items-start gap-2 py-2">
          <span aria-hidden="true" className={cn("mt-1.5 size-2 shrink-0 rounded-full", status.dot)} />
          <span className="min-w-0">
            <span className="block text-xs font-medium">{status.label}</span>
            <span className="mt-0.5 block text-[11px] font-normal leading-4 text-muted-foreground">{status.detail}</span>
          </span>
        </DropdownMenuLabel>

        {isSignedIn ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel className="flex flex-col gap-0.5 py-2">
              <span className="block truncate text-xs font-semibold">{user?.displayName || "Account"}</span>
              {user?.displayName ? (
                <span className="block truncate text-[11px] font-normal text-muted-foreground">{user?.email}</span>
              ) : null}
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className="items-center gap-2 py-2"
              disabled={busy}
              onSelect={() => void logout()}
            >
              <LogOutIcon aria-hidden="true" className="size-3.5" />
              <span className="text-xs">{busy ? "Signing out…" : "Sign out"}</span>
            </DropdownMenuItem>
          </>
        ) : (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className="items-center gap-2 py-2"
              onSelect={() => openDialog("signin")}
            >
              <LogInIcon aria-hidden="true" className="size-3.5" />
              <span className="text-xs">Sign in</span>
            </DropdownMenuItem>
            <DropdownMenuItem
              className="items-center gap-2 py-2"
              onSelect={() => openDialog("register")}
            >
              <UserPlusIcon aria-hidden="true" className="size-3.5" />
              <span className="text-xs">Create account</span>
            </DropdownMenuItem>
          </>
        )}

        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link className="flex items-center gap-2 text-xs" href="/">
            <ExternalLinkIcon aria-hidden="true" className="size-3.5" />
            All projects
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
