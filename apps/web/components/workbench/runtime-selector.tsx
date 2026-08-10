"use client";

import { BotIcon, CpuIcon } from "lucide-react";
import { useEffect } from "react";

import {
  PromptInputSelect,
  PromptInputSelectContent,
  PromptInputSelectItem,
  PromptInputSelectTrigger,
  PromptInputSelectValue,
} from "@/components/ai-elements/prompt-input";
import type { AgentFrameworkId, RuntimeOptionsResponse, RuntimeProfileOption, RunRuntimeResponse } from "@/lib/contracts";

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
  selectedAgentFramework,
  selectedProfileId,
  selectedThinking,
  onSelectAgentFramework,
  onSelectProfile,
  onSelectThinking,
}: {
  disabled?: boolean;
  options?: RuntimeOptionsResponse;
  selectedAgentFramework?: AgentFrameworkId;
  selectedProfileId?: string;
  selectedThinking?: string;
  onSelectAgentFramework: (framework: AgentFrameworkId) => void;
  onSelectProfile: (profileId: string) => void;
  onSelectThinking: (thinking: string) => void;
}) {
  const frameworks = options?.agentFrameworks ?? [];
  const profiles = options?.profiles ?? [];
  const selectedFrameworkOption = frameworks.find(
    (framework) => framework.id === selectedAgentFramework,
  );
  const compatibleProfileIds = selectedFrameworkOption?.compatibleProfileIds ?? [];
  const visibleProfiles = compatibleProfileIds.length > 0
    ? profiles.filter((profile) => compatibleProfileIds.includes(profile.profileId))
    : profiles;
  const selectedProfile = findRuntimeProfile(options, selectedProfileId);
  const frameworkThinkingLevels = selectedFrameworkOption?.compatibleThinkingLevels;
  const thinkingLevels = (selectedProfile?.thinkingLevels ?? []).filter(
    (level) => frameworkThinkingLevels == null || frameworkThinkingLevels.includes(level),
  );

  useEffect(() => {
    if (!selectedFrameworkOption?.available) return;
    const allowedProfiles = profiles.filter(
      (profile) => profile.available && (
        selectedFrameworkOption.compatibleProfileIds.length === 0
        || selectedFrameworkOption.compatibleProfileIds.includes(profile.profileId)
      ),
    );
    const profile = allowedProfiles.find((candidate) => candidate.profileId === selectedProfileId)
      ?? allowedProfiles.find((candidate) => candidate.profileId === options?.defaultProfileId)
      ?? allowedProfiles[0];
    if (!profile) return;
    if (profile.profileId !== selectedProfileId) onSelectProfile(profile.profileId);

    const allowedThinking = profile.thinkingLevels.filter(
      (level) => selectedFrameworkOption.compatibleThinkingLevels == null
        || selectedFrameworkOption.compatibleThinkingLevels.includes(level),
    );
    const thinking = allowedThinking.includes(selectedThinking ?? "")
      ? selectedThinking
      : allowedThinking.includes(profile.defaultThinking)
        ? profile.defaultThinking
        : allowedThinking[0];
    if (thinking && thinking !== selectedThinking) onSelectThinking(thinking);
  }, [
    onSelectProfile,
    onSelectThinking,
    options?.defaultProfileId,
    profiles,
    selectedFrameworkOption,
    selectedProfileId,
    selectedThinking,
  ]);

  const selectFramework = (frameworkId: AgentFrameworkId) => {
    const framework = frameworks.find((candidate) => candidate.id === frameworkId);
    onSelectAgentFramework(frameworkId);
    if (!framework) return;

    const allowedProfiles = profiles.filter(
      (profile) => profile.available && (
        framework.compatibleProfileIds.length === 0
        || framework.compatibleProfileIds.includes(profile.profileId)
      ),
    );
    const profile = allowedProfiles.find((candidate) => candidate.profileId === selectedProfileId)
      ?? allowedProfiles.find((candidate) => candidate.profileId === options?.defaultProfileId)
      ?? allowedProfiles[0];
    if (!profile) return;
    if (profile.profileId !== selectedProfileId) onSelectProfile(profile.profileId);

    const allowedThinking = profile.thinkingLevels.filter(
      (level) => framework.compatibleThinkingLevels == null
        || framework.compatibleThinkingLevels.includes(level),
    );
    const thinking = allowedThinking.includes(selectedThinking ?? "")
      ? selectedThinking
      : allowedThinking.includes(profile.defaultThinking)
        ? profile.defaultThinking
        : allowedThinking[0];
    if (thinking && thinking !== selectedThinking) onSelectThinking(thinking);
  };
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1">
      <PromptInputSelect
        disabled={disabled || frameworks.length === 0}
        onValueChange={(value) => selectFramework(value as AgentFrameworkId)}
        value={selectedAgentFramework ?? ""}
      >
        <PromptInputSelectTrigger
          aria-label="Coding Agent 框架"
          className="h-8 max-w-[8rem] shrink-0 gap-1 px-2"
          size="sm"
        >
          <BotIcon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
          <PromptInputSelectValue placeholder="选择 Agent" />
        </PromptInputSelectTrigger>
        <PromptInputSelectContent>
          {frameworks.map((framework) => (
            <PromptInputSelectItem
              disabled={!framework.available}
              key={framework.id}
              value={framework.id}
            >
              <span className="font-medium">{framework.label}</span>
              {!framework.available
                ? <span className="ml-1 text-xs text-muted-foreground">{framework.disabledReason ? ` · ${framework.disabledReason}` : " · 暂不可用"}</span>
                : null}
            </PromptInputSelectItem>
          ))}
        </PromptInputSelectContent>
      </PromptInputSelect>
      <PromptInputSelect
        disabled={disabled || visibleProfiles.length === 0}
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
          {visibleProfiles.map((profile) => (
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
export function RuntimeBadge({ agentFramework, profileLabel, runtime }: { agentFramework?: AgentFrameworkId; profileLabel: string; runtime: RunRuntimeResponse }) {
  const frameworkLabel = agentFramework === "opencode"
    ? "OpenCode"
    : agentFramework === "codex"
      ? "Codex"
      : "Pi";
  const detail = `${frameworkLabel} · ${profileLabel} · 思考 ${thinkingLabel(runtime.thinking)} · 上下文 ${formatTokenBudget(runtime.contextWindow)}`;
  return (
    <span
      className="inline-flex max-w-full min-w-0 items-center gap-1 rounded-full border bg-muted/60 px-2 py-0.5 text-[11px] text-muted-foreground"
      data-runtime={runtime.profileId}
      data-agent-framework={agentFramework ?? "pi"}
      data-thinking={runtime.thinking}
      title={detail}
    >
      <BotIcon aria-hidden="true" className="size-3 shrink-0 text-muted-foreground" />
      <span className="shrink-0 font-medium text-foreground/80">{frameworkLabel}</span>
      <span aria-hidden="true" className="shrink-0 text-border">·</span>
      <span className="truncate font-medium text-foreground/80">{profileLabel}</span>
      <span aria-hidden="true" className="shrink-0 text-border">·</span>
      <span className="shrink-0">{thinkingLabel(runtime.thinking)}</span>
      <span aria-hidden="true" className="shrink-0 text-border">·</span>
      <span className="shrink-0 tabular-nums">{formatTokenBudget(runtime.contextWindow)}</span>
    </span>
  );
}
