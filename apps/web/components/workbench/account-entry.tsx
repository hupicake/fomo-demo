"use client";

import { CircleUserRoundIcon, ExternalLinkIcon, LogOutIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuthStore } from "@/lib/store/auth-store";
import { cn } from "@/lib/utils";

function initials(name?: string, email?: string): string {
  const source = (name || email || "?").trim();
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return source.slice(0, 2).toUpperCase();
}

function loginHref(mode: "signin" | "register", currentPath: string): string {
  const params = new URLSearchParams({ mode });
  if (currentPath && currentPath !== "/") params.set("redirect", currentPath);
  return `/login?${params.toString()}`;
}

export function AccountEntry({ className }: { className?: string }) {
  const authStatus = useAuthStore((state) => state.status);
  const user = useAuthStore((state) => state.user);
  const busy = useAuthStore((state) => state.busy);
  const logout = useAuthStore((state) => state.logout);
  const pathname = usePathname();

  if (authStatus === "unknown") {
    return <Skeleton aria-label="正在检查登录状态" className={cn("h-8 w-28 rounded-full", className)} />;
  }

  if (authStatus !== "authenticated" || !user) {
    return (
      <div className={cn("flex items-center gap-1.5", className)} data-auth-state="signed-out">
        <Button asChild size="sm" variant="ghost">
          <Link href={loginHref("signin", pathname)}>登录</Link>
        </Button>
        <Button asChild size="sm">
          <Link href={loginHref("register", pathname)}>注册</Link>
        </Button>
      </div>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={`账号：${user.displayName || user.email}`}
          className={cn("h-8 gap-2 rounded-full border pl-1.5 pr-2.5", className)}
          data-auth-state="signed-in"
          size="sm"
          variant="ghost"
        >
          <span className="grid size-5 place-items-center rounded-full bg-muted text-[9px] font-semibold text-foreground">
            {initials(user.displayName, user.email)}
          </span>
          <span className="hidden max-w-28 truncate text-[11px] font-medium sm:inline">
            {user.displayName || user.email}
          </span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="flex items-center gap-2 py-2">
          <CircleUserRoundIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
          <span className="min-w-0">
            <span className="block truncate text-xs font-semibold">{user.displayName || "账号"}</span>
            <span className="block truncate text-[11px] font-normal text-muted-foreground">{user.email}</span>
          </span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link className="flex items-center gap-2 text-xs" href="/">
            <ExternalLinkIcon aria-hidden="true" className="size-3.5" />
            全部项目
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem className="items-center gap-2 py-2" disabled={busy} onSelect={() => void logout()}>
          <LogOutIcon aria-hidden="true" className="size-3.5" />
          <span className="text-xs">{busy ? "正在退出…" : "退出登录"}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
