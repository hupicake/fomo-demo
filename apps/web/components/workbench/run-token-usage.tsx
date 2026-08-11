import type { RunUsage } from "@/lib/contracts";
import { cn } from "@/lib/utils";

export function formatTokenCount(tokens: number): string {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 1,
    notation: "compact",
  }).format(tokens);
}

export function RunTokenUsage({ className, usage }: { className?: string; usage: RunUsage }) {
  const detail = [
    `Total ${usage.totalTokens.toLocaleString("en-US")} tokens`,
    `Input ${usage.inputTokens.toLocaleString("en-US")}`,
    `Output ${usage.outputTokens.toLocaleString("en-US")}`,
    `Cache read ${usage.cacheReadTokens.toLocaleString("en-US")}`,
    `Cache write ${usage.cacheWriteTokens.toLocaleString("en-US")}`,
    `${usage.toolCalls.toLocaleString("en-US")} tool calls`,
  ].join(" · ");

  return (
    <span
      aria-label={detail}
      className={cn(
        "inline-flex h-6 items-center gap-1 rounded-full border bg-muted/50 px-2 font-mono text-[10px] tabular-nums text-muted-foreground",
        className,
      )}
      title={detail}
    >
      <span className="font-sans uppercase tracking-wide">Tokens</span>
      <span className="font-semibold text-foreground">{formatTokenCount(usage.totalTokens)}</span>
    </span>
  );
}
