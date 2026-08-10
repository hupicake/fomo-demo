"use client";

import { CpuIcon } from "lucide-react";

import {
  PromptInputSelect,
  PromptInputSelectContent,
  PromptInputSelectItem,
  PromptInputSelectTrigger,
  PromptInputSelectValue,
} from "@/components/ai-elements/prompt-input";
import type { RuntimeOptionsResponse, RuntimeProfileOption, RunRuntimeResponse } from "@/lib/contracts";

const thinkingLevelLabels: Record<string, string> = {
  off: "关闭",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "最高",
  max: "最大",
  default: "默认",
  minimal: "极少",
};

export function thinkingLabel(level: string): string {
  return thinkingLevelLabels[level] ?? level;
}

export function formatTokenBudget(tokens: number): string {
  if (!tokens) return "—";
  if (tokens >= 1_000_000 && tokens % 1_000_000 === 0) return `${tokens / 1_000_000}M`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}K`;
  return String(tokens);
}

export function findRuntimeProfile(options: RuntimeOptionsResponse | undefined, profileId: string | undefined): RuntimeProfileOption | undefined {
  if (!options || !profileId) return undefined;
  return options.profiles.find((profile) => profile.profileId === profileId);
}

export function RuntimeSelector({
  disabled,
  options,
  selectedProfileId,
  selectedThinking,
  onSelectProfile,
  onSelectThinking,
}: {
  disabled?: boolean;
  options?: RuntimeOptionsResponse;
  selectedProfileId?: string;
  selectedThinking?: string;
  onSelectProfile: (profileId: string) => void;
  onSelectThinking: (thinking: string) => void;
}) {
  const profiles = options?.profiles ?? [];
  const selectedProfile = findRuntimeProfile(options, selectedProfileId);
  const thinkingLevels = selectedProfile?.thinkingLevels ?? [];
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1">
      <PromptInputSelect
        disabled={disabled || profiles.length === 0}
        onValueChange={onSelectProfile}
        value={selectedProfileId ?? ""}
      >
        <PromptInputSelectTrigger
          aria-label="运行时模型"
          className="h-8 max-w-[11rem] shrink-0 gap-1 px-2"
          size="sm"
        >
          <CpuIcon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
          <PromptInputSelectValue placeholder="选择模型" />
        </PromptInputSelectTrigger>
        <PromptInputSelectContent>
          {profiles.map((profile) => (
            <PromptInputSelectItem
              disabled={!profile.available}
              key={profile.profileId}
              value={profile.profileId}
            >
              <span className="font-medium">{profile.label}</span>
              {!profile.available
                ? <span className="ml-1 text-xs text-muted-foreground">{profile.disabledReason ? ` · ${profile.disabledReason}` : " · 暂不可用"}</span>
                : null}
            </PromptInputSelectItem>
          ))}
        </PromptInputSelectContent>
      </PromptInputSelect>
      <PromptInputSelect
        disabled={disabled || thinkingLevels.length === 0}
        onValueChange={onSelectThinking}
        value={selectedThinking ?? ""}
      >
        <PromptInputSelectTrigger
          aria-label="思考级别"
          className="h-8 max-w-[6.5rem] shrink-0 px-2"
          size="sm"
        >
          <PromptInputSelectValue placeholder="思考" />
        </PromptInputSelectTrigger>
        <PromptInputSelectContent>
          {thinkingLevels.map((level) => (
            <PromptInputSelectItem key={level} value={level}>
              {thinkingLabel(level)}
            </PromptInputSelectItem>
          ))}
        </PromptInputSelectContent>
      </PromptInputSelect>
    </div>
  );
}

/** Read-only presentation of the resolved run contract, shown after creation. */
export function RuntimeBadge({ profileLabel, runtime }: { profileLabel: string; runtime: RunRuntimeResponse }) {
  const detail = `${profileLabel} · 思考 ${thinkingLabel(runtime.thinking)} · 上下文 ${formatTokenBudget(runtime.contextWindow)}`;
  return (
    <span
      className="inline-flex max-w-full min-w-0 items-center gap-1 rounded-full border bg-muted/60 px-2 py-0.5 text-[11px] text-muted-foreground"
      data-runtime={runtime.profileId}
      data-thinking={runtime.thinking}
      title={detail}
    >
      <CpuIcon aria-hidden="true" className="size-3 shrink-0 text-muted-foreground" />
      <span className="truncate font-medium text-foreground/80">{profileLabel}</span>
      <span aria-hidden="true" className="shrink-0 text-border">·</span>
      <span className="shrink-0">{thinkingLabel(runtime.thinking)}</span>
      <span aria-hidden="true" className="shrink-0 text-border">·</span>
      <span className="shrink-0 tabular-nums">{formatTokenBudget(runtime.contextWindow)}</span>
    </span>
  );
}
