"""Server-owned model catalog and immutable per-run inference contract.

Browsers select a public profile id and a profile-supported thinking level.
Provider routes, LiteLLM aliases, context limits, and policy versions remain
server-owned so a queued or resumed run cannot drift when defaults change.
"""

from __future__ import annotations

from dataclasses import dataclass

from fomo.agent_framework import AgentFramework, normalize_agent_framework

RUNTIME_POLICY_VERSION = "direct-pi-runtime-v2"
PREVIOUS_RUNTIME_POLICY_VERSION = "direct-pi-runtime-v1"
LEGACY_RUNTIME_POLICY_VERSION = "direct-pi-legacy-v0"
DEFAULT_PROFILE_ID = "deepseek-flash"
DEFAULT_THINKING = "high"
LEGACY_PROFILE_ID = "deepseek-flash"
LEGACY_MODEL_REF = "fomo-litellm/fomo-pi-flash"
LEGACY_LITELLM_ALIAS = "fomo-pi-flash"
LEGACY_THINKING = "high"
LEGACY_CONTEXT_WINDOW = 200_000
MAX_CONTEXT_WINDOW = 1_000_000
ENABLED_PROFILES_ENV = "FOMO_RUNTIME_ENABLED_PROFILES"
DEFAULT_PROFILE_ENV = "FOMO_RUNTIME_DEFAULT_PROFILE"
DEFAULT_ENABLED_PROFILE_IDS = frozenset({DEFAULT_PROFILE_ID})
LEGACY_RUN_MAX_TOKENS = 400_000
LEGACY_INFERENCE_TPM_LIMIT = 1_000_000
LEGACY_MAX_SPEND_MICROS = 2_000_000
DEFAULT_MAX_SPEND_MICROS = 5_000_000


class RuntimeContractError(ValueError):
    """A public selection or persisted runtime snapshot is invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    profile_id: str
    label: str
    model_ref: str
    litellm_alias: str
    context_window: int
    thinking_levels: tuple[str, ...]
    inference_tpm_limit: int
    default_thinking: str = DEFAULT_THINKING

    def __post_init__(self) -> None:
        if not self.profile_id or not self.label:
            raise RuntimeContractError("runtime profile identity must be non-empty")
        if self.model_ref != f"fomo-litellm/{self.litellm_alias}":
            raise RuntimeContractError("runtime profile model ref and alias must match")
        if not 1 <= self.context_window <= MAX_CONTEXT_WINDOW:
            raise RuntimeContractError("runtime profile context window is out of range")
        if self.inference_tpm_limit <= self.context_window:
            raise RuntimeContractError("runtime TPM budget must include output headroom")
        if not self.thinking_levels or len(set(self.thinking_levels)) != len(
            self.thinking_levels
        ):
            raise RuntimeContractError("runtime profile thinking levels must be unique")
        if self.default_thinking not in self.thinking_levels:
            raise RuntimeContractError("runtime profile default thinking is unsupported")


RUNTIME_PROFILES: tuple[RuntimeProfile, ...] = (
    RuntimeProfile(
        profile_id="gpt-5.6",
        label="GPT-5.6",
        model_ref="fomo-litellm/fomo-pi-gpt-5.6",
        litellm_alias="fomo-pi-gpt-5.6",
        context_window=250_000,
        thinking_levels=("off", "low", "medium", "high", "xhigh", "max"),
        inference_tpm_limit=1_000_000,
    ),
    RuntimeProfile(
        profile_id="gpt-5.5",
        label="GPT-5.5",
        model_ref="fomo-litellm/fomo-pi-gpt-5.5",
        litellm_alias="fomo-pi-gpt-5.5",
        context_window=250_000,
        thinking_levels=("off", "low", "medium", "high", "xhigh"),
        inference_tpm_limit=1_000_000,
    ),
    RuntimeProfile(
        profile_id="deepseek-flash",
        label="DeepSeek Flash",
        model_ref="fomo-litellm/fomo-pi-deepseek-flash",
        litellm_alias="fomo-pi-deepseek-flash",
        context_window=1_000_000,
        thinking_levels=("off", "high"),
        inference_tpm_limit=1_250_000,
    ),
    RuntimeProfile(
        profile_id="grok-4.5",
        label="Grok 4.5",
        model_ref="fomo-litellm/fomo-pi-grok-4.5",
        litellm_alias="fomo-pi-grok-4.5",
        context_window=500_000,
        thinking_levels=("low", "medium", "high"),
        inference_tpm_limit=1_000_000,
    ),
    RuntimeProfile(
        profile_id="kimi-k2.7-code",
        label="Kimi K2.7 Code",
        model_ref="fomo-litellm/fomo-pi-kimi-k2.7-code",
        litellm_alias="fomo-pi-kimi-k2.7-code",
        context_window=262_144,
        thinking_levels=("default",),
        inference_tpm_limit=1_000_000,
        default_thinking="default",
    ),
    RuntimeProfile(
        profile_id="gemini-3.6-flash",
        label="Gemini 3.6 Flash",
        model_ref="fomo-litellm/fomo-pi-gemini-3.6-flash",
        litellm_alias="fomo-pi-gemini-3.6-flash",
        context_window=250_000,
        thinking_levels=("minimal", "low", "medium", "high"),
        inference_tpm_limit=1_000_000,
    ),
    RuntimeProfile(
        profile_id="gemini-3.1-pro",
        label="Gemini 3.1 Pro",
        model_ref="fomo-litellm/fomo-pi-gemini-3.1-pro",
        litellm_alias="fomo-pi-gemini-3.1-pro",
        context_window=250_000,
        thinking_levels=("low", "medium", "high"),
        inference_tpm_limit=1_000_000,
    ),
)

_PROFILE_BY_ID = {profile.profile_id: profile for profile in RUNTIME_PROFILES}
_PROFILE_BY_MODEL_REF = {profile.model_ref: profile for profile in RUNTIME_PROFILES}
_PROFILE_BY_ALIAS = {profile.litellm_alias: profile for profile in RUNTIME_PROFILES}

# Codex transport is intentionally restricted to the audited GPT routes. Keep
# this as a closed profile-id allowlist: adding a future profile named "gpt-*"
# must not silently make it eligible before its transport contract is checked.
CODEX_COMPATIBLE_PROFILE_IDS = frozenset({"gpt-5.5", "gpt-5.6"})
CODEX_COMPATIBLE_THINKING_LEVELS = (
    "low",
    "medium",
    "high",
    "xhigh",
)

# OpenCode 1.18.x implements JSON-schema output as a required synthetic tool
# call. DeepSeek V4 rejects forced tool choice while thinking is enabled, so a
# run-wide OpenCode + DeepSeek contract can only admit non-thinking mode: every
# run must pass the structured planner before reaching the workspace stage.
_FRAMEWORK_PROFILE_THINKING_OVERRIDES: dict[
    tuple[str, str], tuple[str, ...]
] = {
    (AgentFramework.opencode.value, "deepseek-flash"): ("off",),
}

# Runtime v1 froze a cumulative token ceiling. It remains accepted only when
# reading historical runs; runtime v2 stores ``NULL`` to mean that cumulative
# usage is unlimited. Context windows, per-minute throughput, provider output
# limits, tool-call budgets, wall time, and spend limits remain independent.
_V1_RUN_MAX_TOKENS_BY_PROFILE_ID: dict[str, int] = {
    "gpt-5.6": 600_000,
    "gpt-5.5": 600_000,
    "deepseek-flash": 2_000_000,
    "grok-4.5": 600_000,
    "kimi-k2.7-code": 600_000,
    "gemini-3.6-flash": 600_000,
    "gemini-3.1-pro": 600_000,
}

# Existing queued/waiting runs were persisted before a runtime snapshot existed.
# Their historical route is accepted only as an immutable legacy tuple.
LEGACY_MODEL_THINKING_LEVELS: dict[str, frozenset[str]] = {
    LEGACY_MODEL_REF: frozenset({"off", "high", "max"}),
}
LEGACY_MODEL_CONTEXT_LIMITS: dict[str, int] = {
    LEGACY_MODEL_REF: 1_000_000,
}


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    profile_id: str
    model_ref: str
    litellm_alias: str
    thinking: str
    context_window: int
    policy_version: str
    # ``None`` is the explicit runtime-v2 unlimited cumulative-token contract.
    # Historical runtime-v0/v1 rows retain their frozen integer for replay.
    run_max_tokens: int | None
    inference_tpm_limit: int
    max_spend_micros: int

    def cache_fingerprint(self) -> dict[str, str]:
        return {
            "runtimeProfile": self.profile_id,
            "runtimeThinking": self.thinking,
            "runtimePolicy": self.policy_version,
        }


def runtime_profile(profile_id: str) -> RuntimeProfile:
    try:
        return _PROFILE_BY_ID[profile_id]
    except KeyError as exc:
        raise RuntimeContractError("unknown runtime profile") from exc


def compatible_profile_ids_for_agent_framework(
    agent_framework: object,
) -> frozenset[str]:
    """Return the server-owned profile allowlist for one public framework."""

    try:
        framework = normalize_agent_framework(agent_framework)
    except ValueError as exc:
        raise RuntimeContractError(str(exc)) from exc
    if framework == AgentFramework.codex.value:
        return CODEX_COMPATIBLE_PROFILE_IDS
    return frozenset(_PROFILE_BY_ID)


def compatible_thinking_levels_for_agent_framework_profile(
    agent_framework: object,
    profile_id: str,
) -> tuple[str, ...]:
    """Return thinking levels proven compatible with one framework/model pair."""

    validate_agent_framework_profile(agent_framework, profile_id)
    framework = normalize_agent_framework(agent_framework)
    profile = runtime_profile(profile_id)
    override = _FRAMEWORK_PROFILE_THINKING_OVERRIDES.get(
        (framework, profile_id)
    )
    if override is not None:
        return override
    if framework == AgentFramework.codex.value:
        return tuple(
            level
            for level in profile.thinking_levels
            if level in CODEX_COMPATIBLE_THINKING_LEVELS
        )
    return profile.thinking_levels


def validate_agent_framework_profile(
    agent_framework: object,
    profile_id: str,
) -> None:
    """Reject unsupported framework/profile pairs before a run is persisted."""

    runtime_profile(profile_id)
    if profile_id not in compatible_profile_ids_for_agent_framework(agent_framework):
        raise RuntimeContractError(
            "codex agent framework requires a GPT runtime profile"
        )


def validate_agent_framework_runtime(
    agent_framework: object,
    profile_id: str,
    thinking: str,
) -> None:
    """Validate the complete framework-owned inference boundary."""

    framework = normalize_agent_framework(agent_framework)
    compatible_levels = compatible_thinking_levels_for_agent_framework_profile(
        framework,
        profile_id,
    )
    if thinking not in compatible_levels:
        raise RuntimeContractError(
            f"{framework} agent framework does not support this thinking level "
            f"for {profile_id}"
        )


def runtime_profile_for_model_ref(model_ref: str) -> RuntimeProfile | None:
    return _PROFILE_BY_MODEL_REF.get(model_ref)


def selectable_litellm_aliases() -> tuple[str, ...]:
    return tuple(profile.litellm_alias for profile in RUNTIME_PROFILES)


def parse_enabled_profile_ids(value: str | None) -> frozenset[str]:
    raw = value or ""
    if not raw.strip():
        return DEFAULT_ENABLED_PROFILE_IDS
    requested = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if not requested:
        return DEFAULT_ENABLED_PROFILE_IDS
    unknown = requested.difference(_PROFILE_BY_ID)
    if unknown:
        raise RuntimeContractError("enabled runtime profiles contain an unknown profile")
    return requested


def validated_default_profile_id(
    value: str | None, enabled_profiles: frozenset[str]
) -> str:
    candidate = (value or DEFAULT_PROFILE_ID).strip()
    if candidate not in _PROFILE_BY_ID:
        raise RuntimeContractError("default runtime profile is unknown")
    if candidate not in enabled_profiles:
        raise RuntimeContractError("default runtime profile must be enabled")
    return candidate


def allowed_litellm_aliases() -> frozenset[str]:
    return frozenset(
        {
            *selectable_litellm_aliases(),
            LEGACY_LITELLM_ALIAS,
        }
    )


def allowed_model_refs() -> frozenset[str]:
    return frozenset(
        {
            *(profile.model_ref for profile in RUNTIME_PROFILES),
            *LEGACY_MODEL_THINKING_LEVELS,
        }
    )


def thinking_levels_for_model_ref(model_ref: str) -> frozenset[str]:
    profile = _PROFILE_BY_MODEL_REF.get(model_ref)
    if profile is not None:
        return frozenset(profile.thinking_levels)
    try:
        return LEGACY_MODEL_THINKING_LEVELS[model_ref]
    except KeyError as exc:
        raise RuntimeContractError("unknown runtime model ref") from exc


def context_limit_for_model_ref(model_ref: str) -> int:
    profile = _PROFILE_BY_MODEL_REF.get(model_ref)
    if profile is not None:
        return profile.context_window
    try:
        return LEGACY_MODEL_CONTEXT_LIMITS[model_ref]
    except KeyError as exc:
        raise RuntimeContractError("unknown runtime model ref") from exc


def resolve_runtime_contract(
    profile_id: str = DEFAULT_PROFILE_ID,
    thinking: str | None = None,
    *,
    inference_tpm_limit: int | None = None,
    max_spend_micros: int | None = None,
) -> RuntimeContract:
    profile = runtime_profile(profile_id)
    resolved_thinking = thinking or profile.default_thinking
    if resolved_thinking not in profile.thinking_levels:
        raise RuntimeContractError("thinking level is unsupported by runtime profile")
    resolved_inference_tpm_limit = min(
        profile.inference_tpm_limit,
        inference_tpm_limit
        if inference_tpm_limit is not None
        else profile.inference_tpm_limit,
    )
    resolved_max_spend_micros = (
        max_spend_micros
        if max_spend_micros is not None
        else DEFAULT_MAX_SPEND_MICROS
    )
    if resolved_inference_tpm_limit <= profile.context_window:
        raise RuntimeContractError("runtime TPM limit lacks output headroom")
    if resolved_max_spend_micros <= 0:
        raise RuntimeContractError("runtime spend budget must be positive")
    return RuntimeContract(
        profile_id=profile.profile_id,
        model_ref=profile.model_ref,
        litellm_alias=profile.litellm_alias,
        thinking=resolved_thinking,
        context_window=profile.context_window,
        policy_version=RUNTIME_POLICY_VERSION,
        run_max_tokens=None,
        inference_tpm_limit=resolved_inference_tpm_limit,
        max_spend_micros=resolved_max_spend_micros,
    )


def legacy_runtime_contract() -> RuntimeContract:
    return RuntimeContract(
        profile_id=LEGACY_PROFILE_ID,
        model_ref=LEGACY_MODEL_REF,
        litellm_alias=LEGACY_LITELLM_ALIAS,
        thinking=LEGACY_THINKING,
        context_window=LEGACY_CONTEXT_WINDOW,
        policy_version=LEGACY_RUNTIME_POLICY_VERSION,
        run_max_tokens=LEGACY_RUN_MAX_TOKENS,
        inference_tpm_limit=LEGACY_INFERENCE_TPM_LIMIT,
        max_spend_micros=LEGACY_MAX_SPEND_MICROS,
    )


def runtime_contract_from_storage(
    *,
    profile_id: str,
    model_ref: str,
    thinking: str,
    context_window: int,
    policy_version: str,
    run_max_tokens: int | None,
    inference_tpm_limit: int,
    max_spend_micros: int,
) -> RuntimeContract:
    if policy_version == LEGACY_RUNTIME_POLICY_VERSION:
        expected = legacy_runtime_contract()
        candidate = RuntimeContract(
            profile_id=profile_id,
            model_ref=model_ref,
            litellm_alias=model_ref.partition("/")[2],
            thinking=thinking,
            context_window=context_window,
            policy_version=policy_version,
            run_max_tokens=run_max_tokens,
            inference_tpm_limit=inference_tpm_limit,
            max_spend_micros=max_spend_micros,
        )
        if candidate != expected:
            raise RuntimeContractError("legacy runtime contract does not match its frozen tuple")
        return candidate

    if policy_version not in {
        PREVIOUS_RUNTIME_POLICY_VERSION,
        RUNTIME_POLICY_VERSION,
    }:
        raise RuntimeContractError("unknown runtime policy version")
    candidate = RuntimeContract(
        profile_id=profile_id,
        model_ref=model_ref,
        litellm_alias=model_ref.partition("/")[2],
        thinking=thinking,
        context_window=context_window,
        policy_version=policy_version,
        run_max_tokens=run_max_tokens,
        inference_tpm_limit=inference_tpm_limit,
        max_spend_micros=max_spend_micros,
    )
    expected = resolve_runtime_contract(profile_id, thinking)
    token_contract_is_valid = (
        run_max_tokens is None
        if policy_version == RUNTIME_POLICY_VERSION
        else isinstance(run_max_tokens, int)
        and not isinstance(run_max_tokens, bool)
        and context_window <= run_max_tokens <= _V1_RUN_MAX_TOKENS_BY_PROFILE_ID[profile_id]
    )
    if (
        expected.model_ref != model_ref
        or expected.context_window != context_window
        or not token_contract_is_valid
        or not context_window < inference_tpm_limit <= expected.inference_tpm_limit
        or max_spend_micros <= 0
    ):
        raise RuntimeContractError("persisted runtime contract does not match its profile")
    return candidate


def validate_invocation_contract(
    *, model_ref: str, thinking: str, context_window: int
) -> None:
    if model_ref not in allowed_model_refs():
        raise RuntimeContractError("unknown runtime model ref")
    if thinking not in thinking_levels_for_model_ref(model_ref):
        raise RuntimeContractError("thinking level is unsupported by runtime model")
    if not 1 <= context_window <= context_limit_for_model_ref(model_ref):
        raise RuntimeContractError("context window exceeds the runtime model limit")


__all__ = [
    "CODEX_COMPATIBLE_PROFILE_IDS",
    "CODEX_COMPATIBLE_THINKING_LEVELS",
    "DEFAULT_PROFILE_ID",
    "DEFAULT_MAX_SPEND_MICROS",
    "DEFAULT_THINKING",
    "DEFAULT_ENABLED_PROFILE_IDS",
    "DEFAULT_PROFILE_ENV",
    "ENABLED_PROFILES_ENV",
    "LEGACY_CONTEXT_WINDOW",
    "LEGACY_INFERENCE_TPM_LIMIT",
    "LEGACY_LITELLM_ALIAS",
    "LEGACY_MODEL_REF",
    "LEGACY_MAX_SPEND_MICROS",
    "LEGACY_RUN_MAX_TOKENS",
    "LEGACY_RUNTIME_POLICY_VERSION",
    "MAX_CONTEXT_WINDOW",
    "PREVIOUS_RUNTIME_POLICY_VERSION",
    "RUNTIME_POLICY_VERSION",
    "RUNTIME_PROFILES",
    "RuntimeContract",
    "RuntimeContractError",
    "RuntimeProfile",
    "allowed_litellm_aliases",
    "allowed_model_refs",
    "compatible_profile_ids_for_agent_framework",
    "compatible_thinking_levels_for_agent_framework_profile",
    "context_limit_for_model_ref",
    "parse_enabled_profile_ids",
    "legacy_runtime_contract",
    "resolve_runtime_contract",
    "runtime_contract_from_storage",
    "runtime_profile",
    "runtime_profile_for_model_ref",
    "selectable_litellm_aliases",
    "thinking_levels_for_model_ref",
    "validated_default_profile_id",
    "validate_agent_framework_profile",
    "validate_agent_framework_runtime",
    "validate_invocation_contract",
]
