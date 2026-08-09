"use client";

import type { ReactNode } from "react";
import { Menu } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";

export type AppShellProps = {
  children: ReactNode;
  header?: ReactNode;
  navigation?: ReactNode;
};

export function AppShell({ children, header, navigation }: AppShellProps) {
  return (
    <div className="min-h-screen bg-muted/20 text-foreground">
      {navigation ? (
        <aside className="fixed inset-y-0 left-0 z-20 hidden w-64 border-r bg-background md:block">
          <nav className="h-full overflow-y-auto p-4" aria-label="Primary navigation">
            {navigation}
          </nav>
        </aside>
      ) : null}
      <div className={navigation ? "md:pl-64" : undefined}>
        <header className="sticky top-0 z-10 flex min-h-14 items-center gap-3 border-b bg-background/95 px-4 backdrop-blur">
          {navigation ? (
            <Sheet>
              <SheetTrigger asChild>
                <Button variant="ghost" size="icon" className="md:hidden" aria-label="Open navigation">
                  <Menu className="size-5" />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-72 p-0">
                <SheetTitle className="sr-only">Navigation</SheetTitle>
                <nav className="h-full overflow-y-auto p-4" aria-label="Primary navigation">
                  {navigation}
                </nav>
              </SheetContent>
            </Sheet>
          ) : null}
          <div className="min-w-0 flex-1">{header}</div>
        </header>
        <Separator />
        <main className="mx-auto w-full max-w-7xl p-4 sm:p-6 lg:p-8">{children}</main>
      </div>
    </div>
  );
}
