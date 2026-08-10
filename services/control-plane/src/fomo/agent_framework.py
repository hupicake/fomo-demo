"""Per-run Coding Agent framework selection without duplicating orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Protocol, TypeVar, cast


class AgentFramework(StrEnum):
    pi = "pi"
    opencode = "opencode"
    codex = "codex"


SUPPORTED_AGENT_FRAMEWORKS = frozenset(framework.value for framework in AgentFramework)
DEFAULT_AGENT_FRAMEWORK = AgentFramework.pi.value
DEFAULT_ENABLED_AGENT_FRAMEWORKS = (
    AgentFramework.pi.value,
    AgentFramework.opencode.value,
)


def parse_enabled_agent_frameworks(
    values: str | Iterable[str] | None,
) -> tuple[str, ...]:
    """Parse a deployment allowlist and reject empty or unknown identifiers."""

    if values is None:
        candidates = list(DEFAULT_ENABLED_AGENT_FRAMEWORKS)
    elif isinstance(values, str):
        candidates = [part.strip().lower() for part in values.split(",")]
    else:
        candidates = [str(value).strip().lower() for value in values]
    if not candidates or any(not candidate for candidate in candidates):
        raise ValueError("enabled agent frameworks must be a non-empty comma-separated allowlist")
    unknown = sorted(set(candidates).difference(SUPPORTED_AGENT_FRAMEWORKS))
    if unknown:
        raise ValueError(f"unsupported agent framework: {', '.join(unknown)}")
    selected = set(candidates)
    return tuple(
        framework.value for framework in AgentFramework if framework.value in selected
    )


def validated_default_agent_framework(value: str, enabled: Iterable[str]) -> str:
    candidate = normalize_agent_framework(value)
    if candidate not in set(enabled):
        raise ValueError("default agent framework must be present in the enabled allowlist")
    return candidate


def public_framework_from_legacy(value: str) -> str:
    """Map the retired process-wide switch without silently accepting typos."""

    candidate = value.strip().lower()
    if candidate in {"direct_pi", "native", AgentFramework.pi.value}:
        return AgentFramework.pi.value
    if candidate == AgentFramework.opencode.value:
        return AgentFramework.opencode.value
    raise ValueError("AGENT_FRAMEWORK must be direct_pi, native, pi, or opencode")


def legacy_framework_mode(value: str) -> str:
    """Keep old config consumers functional while runs become authoritative."""

    candidate = value.strip().lower()
    public_framework_from_legacy(candidate)
    return "native" if candidate == "native" else "direct_pi"


class RunFrameworkRepository(Protocol):
    async def get_run_agent_framework(self, run_id: str) -> str: ...


def normalize_agent_framework(value: object) -> str:
    """Return one persisted public framework id or fail closed."""

    if not isinstance(value, str):
        raise ValueError("run agent framework must be a string")
    framework = value.strip().lower()
    if framework not in SUPPORTED_AGENT_FRAMEWORKS:
        raise ValueError("run agent framework must be pi, opencode, or codex")
    return framework


async def resolve_run_agent_framework(
    repository: object,
    run_id: str,
    *,
    fallback: str = "pi",
) -> str:
    """Resolve the immutable run choice, with a narrow legacy-fake fallback.

    Production repositories expose ``get_run_agent_framework``. The fallback
    exists only so legacy repositories and focused test doubles created before
    the column was introduced keep selecting Pi deterministically.
    """

    getter = getattr(repository, "get_run_agent_framework", None)
    if getter is None:
        return normalize_agent_framework(fallback)
    value = await cast(RunFrameworkRepository, repository).get_run_agent_framework(
        run_id
    )
    return normalize_agent_framework(value)


TransportT = TypeVar("TransportT")


@dataclass(frozen=True, slots=True)
class AgentTransportRegistry(Generic[TransportT]):
    """Immutable framework-to-transport binding shared by one worker."""

    transports: Mapping[str, TransportT]

    def __post_init__(self) -> None:
        normalized: dict[str, TransportT] = {}
        for framework, transport in self.transports.items():
            key = normalize_agent_framework(framework)
            if key in normalized:
                raise ValueError(f"duplicate transport for {key}")
            normalized[key] = transport
        if not normalized:
            raise ValueError("at least one agent transport is required")
        object.__setattr__(self, "transports", normalized)

    def require(self, framework: str) -> TransportT:
        key = normalize_agent_framework(framework)
        try:
            return self.transports[key]
        except KeyError:
            raise ValueError(f"agent framework {key} is not enabled on this worker") from None

    @classmethod
    def pi_only(cls, transport: TransportT) -> AgentTransportRegistry[TransportT]:
        return cls({"pi": transport})
