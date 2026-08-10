"use client";

import { MoonIcon, SunIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

type Theme = "light" | "dark";

const STORAGE_KEY = "fomo:theme:v1";
type StoredTheme = { version: 1; data: { theme: Theme } };

function getStoredTheme(): Theme {
  if (typeof window === "undefined") return "light";
  try {
    const stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "null") as Partial<StoredTheme> | null;
    const theme = stored?.version === 1 ? stored.data?.theme : undefined;
    if (theme === "dark" || theme === "light") return theme;
  } catch {
    // Ignore malformed or inaccessible preferences and use the OS preference.
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
}

/**
 * Toggles between light and dark themes. The initial theme is resolved before
 * paint by the no-FOUC script in the root layout; this component only syncs
 * its icon and persists user choices to localStorage.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("light");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const resolved = getStoredTheme();
    setTheme(resolved);
    applyTheme(resolved);
    setMounted(true);
  }, []);

  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    applyTheme(next);
    try {
      const stored: StoredTheme = { version: 1, data: { theme: next } };
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
    } catch {
      // Storage may be unavailable (private mode); the class is already set.
    }
  };

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
          data-theme={mounted ? theme : undefined}
          onClick={toggle}
          size="icon-sm"
          variant="ghost"
        >
          {mounted && theme === "dark" ? (
            <SunIcon aria-hidden="true" className="size-4" />
          ) : (
            <MoonIcon aria-hidden="true" className="size-4" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {mounted && theme === "dark" ? "浅色模式" : "深色模式"}
      </TooltipContent>
    </Tooltip>
  );
}
