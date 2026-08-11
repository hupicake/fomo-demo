"""Versioned, engine-neutral guidance for generated frontend code topology.

The profile is deliberately advisory.  It describes responsibility boundaries
that scale with the frozen product graph without becoming a file plan, path
allowlist, or release gate.  Pi, OpenCode, and Codex can therefore consume the
same compact brief even though their native skill systems differ.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

ARCHITECTURE_PROFILE_ID = "next-app-feature-first"
ARCHITECTURE_PROFILE_VERSION = "1.0.0"

_SHARED_BROWSER_STATE_INTENT = re.compile(
    r"(?:local|session)storage|persist(?:ed|ence|ent|ing)?|survive\s+(?:a\s+)?reload|"
    r"cross[ -]?(?:route|page)|shared\s+state|"
    r"本地存储|持久化|刷新后?(?:保留|恢复)|跨(?:路由|页面)|共享状态",
    re.IGNORECASE,
)


class ArchitectureScale(StrEnum):
    """Coarse product scale used only to choose proportional guidance."""

    SIMPLE = "simple"
    STANDARD = "standard"
    COMPLEX = "complex"


_COMMON_GUIDANCE = (
    "Treat Next.js App Router files as route and layout entry points; keep substantial "
    "product behavior in coherent feature boundaries.",
    "Place a shared shell or browser-state provider in the nearest common layout instead "
    "of wrapping the same shell around every page.",
    "Keep domain-specific components, state, fixtures, and helpers together by feature; "
    "reserve shared UI and utility locations for code genuinely reused across features.",
    "Reuse the starter's shadcn/ui primitives and existing aliases before creating another "
    "primitive or parallel design-system directory.",
    "Choose the smallest coherent topology for the product. Do not create placeholder or "
    "single-file folders, and do not compress unrelated responsibilities into one large file.",
)

_SCALE_GUIDANCE = {
    ArchitectureScale.SIMPLE: (
        "For one small route and workflow, prefer route-local colocation, including private "
        "_components or _lib folders only when each groups multiple related files.",
        "Introduce a feature boundary only when it owns enough UI, state, or behavior to make "
        "that boundary clearer than direct colocation.",
    ),
    ArchitectureScale.STANDARD: (
        "Use thin route entries plus one cohesive product or feature boundary for shared "
        "components, model, state, persistence, and fixtures.",
        "Keep cross-route navigation, shell, and providers in a common App Router layout; do "
        "not duplicate them across destination pages.",
    ),
    ArchitectureScale.COMPLEX: (
        "Split the product into bounded vertical feature slices, each owning its related UI, "
        "state, model, persistence, and fixtures rather than scattering one domain by file type.",
        "Keep only cross-feature layout, primitives, and utilities in shared locations; avoid "
        "inventing backend, repository, service, or monorepo layers for a frontend-only product.",
    ),
}


@dataclass(frozen=True, slots=True)
class ArchitectureProfile:
    """A deterministic prompt context derived from server-known graph signals."""

    scale: ArchitectureScale
    route_count: int
    goal_count: int
    shared_state_across_routes: bool
    profile_id: str = ARCHITECTURE_PROFILE_ID
    version: str = ARCHITECTURE_PROFILE_VERSION

    @property
    def guidance(self) -> tuple[str, ...]:
        """Return the immutable common-plus-scale guidance sequence."""

        return (*_COMMON_GUIDANCE, *_SCALE_GUIDANCE[self.scale])

    def as_prompt_context(self) -> dict[str, object]:
        """Return a compact contract that remains explicitly non-prescriptive."""

        return {
            "id": self.profile_id,
            "version": self.version,
            "scale": self.scale.value,
            "signals": {
                "routeCount": self.route_count,
                "goalCount": self.goal_count,
                "sharedStateAcrossRoutes": self.shared_state_across_routes,
            },
            "guidance": list(self.guidance),
            "advisoryOnly": True,
        }

    def render_brief(self) -> str:
        """Render the common natural-language brief used by coding turns."""

        bullets = "\n".join(f"- {item}" for item in self.guidance)
        return (
            f"FOMO generated-app architecture profile {self.profile_id}@{self.version} "
            f"({self.scale.value}):\n"
            f"{bullets}\n"
            "This is proportional architecture guidance, not a fixed file plan, path "
            "allowlist, or reason to add unused layers."
        )

    def fingerprint(self) -> str:
        """Bind continuations and recovery turns to this exact frozen profile."""

        canonical = json.dumps(
            self.as_prompt_context(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def from_prompt_context(cls, value: Mapping[str, object]) -> ArchitectureProfile:
        """Restore a server-authored profile without accepting policy drift.

        Profile versions are compatibility contracts. When a future policy is
        introduced, its decoder must be added alongside this one instead of
        reinterpreting an in-flight run with newer guidance.
        """

        if value.get("id") != ARCHITECTURE_PROFILE_ID:
            raise ValueError("architecture profile id is unsupported")
        if value.get("version") != ARCHITECTURE_PROFILE_VERSION:
            raise ValueError("architecture profile version is unsupported")
        raw_signals = value.get("signals")
        if not isinstance(raw_signals, Mapping):
            raise ValueError("architecture profile signals are missing")
        route_count = raw_signals.get("routeCount")
        goal_count = raw_signals.get("goalCount")
        shared_state = raw_signals.get("sharedStateAcrossRoutes")
        if (
            type(route_count) is not int
            or route_count < 1
            or type(goal_count) is not int
            or goal_count < 1
            or type(shared_state) is not bool
        ):
            raise ValueError("architecture profile signals are invalid")
        restored = derive_architecture_profile(
            route_count=route_count,
            goal_count=goal_count,
            shared_state_across_routes=shared_state,
        )
        if value.get("scale") != restored.scale.value:
            raise ValueError("architecture profile scale is inconsistent")
        if value.get("guidance") != list(restored.guidance):
            raise ValueError("architecture profile guidance is inconsistent")
        if value.get("advisoryOnly") is not True:
            raise ValueError("architecture profile must remain advisory")
        return restored


def derive_architecture_profile(
    *,
    route_count: int,
    goal_count: int,
    shared_state_across_routes: bool = False,
) -> ArchitectureProfile:
    """Choose proportional guidance from durable, framework-independent signals.

    Route and goal counts are intentionally coarse.  They prevent a single-page
    toy topology from being prescribed to a broad product without pretending
    that FOMO can infer an exact file tree before the coding agent inspects the
    workspace.
    """

    if route_count < 1:
        raise ValueError("route_count must be positive")
    if goal_count < 1:
        raise ValueError("goal_count must be positive")

    if (
        route_count >= 5
        or goal_count >= 5
        or (route_count >= 3 and shared_state_across_routes)
    ):
        scale = ArchitectureScale.COMPLEX
    elif route_count == 1 and goal_count == 1 and not shared_state_across_routes:
        scale = ArchitectureScale.SIMPLE
    else:
        scale = ArchitectureScale.STANDARD

    return ArchitectureProfile(
        scale=scale,
        route_count=route_count,
        goal_count=goal_count,
        shared_state_across_routes=shared_state_across_routes,
    )


def derive_product_architecture_profile(
    *,
    requirement: str,
    route_count: int,
    goal_count: int,
) -> ArchitectureProfile:
    """Derive one profile from the frozen product contract and graph shape.

    Shared browser state matters only when more than one route exists.  The
    source request is authoritative, while this bounded classifier merely
    prevents a multi-route persisted workflow from receiving a single-surface
    topology recommendation.
    """

    return derive_architecture_profile(
        route_count=route_count,
        goal_count=goal_count,
        shared_state_across_routes=(
            route_count > 1 and _SHARED_BROWSER_STATE_INTENT.search(requirement) is not None
        ),
    )


__all__ = [
    "ARCHITECTURE_PROFILE_ID",
    "ARCHITECTURE_PROFILE_VERSION",
    "ArchitectureProfile",
    "ArchitectureScale",
    "derive_architecture_profile",
    "derive_product_architecture_profile",
]
