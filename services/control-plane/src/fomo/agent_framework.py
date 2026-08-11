"""Per-run Coding Agent framework selection without duplicating orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Protocol, TypeVar


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
    repository: RunFrameworkRepository,
    run_id: str,
) -> str:
    """Resolve the immutable run choice from the authoritative repository."""

    return normalize_agent_framework(await repository.get_run_agent_framework(run_id))


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
