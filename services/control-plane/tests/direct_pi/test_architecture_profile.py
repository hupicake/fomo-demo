from __future__ import annotations

import pytest

from fomo.direct_pi.architecture_profile import (
    ARCHITECTURE_PROFILE_ID,
    ARCHITECTURE_PROFILE_VERSION,
    ArchitectureProfile,
    ArchitectureScale,
    derive_architecture_profile,
    derive_product_architecture_profile,
)


def test_profile_scales_with_routes_goals_and_shared_cross_route_state() -> None:
    simple = derive_architecture_profile(route_count=1, goal_count=1)
    standard = derive_architecture_profile(route_count=3, goal_count=2)
    complex_routes = derive_architecture_profile(route_count=5, goal_count=2)
    complex_state = derive_architecture_profile(
        route_count=3,
        goal_count=2,
        shared_state_across_routes=True,
    )

    assert simple.scale is ArchitectureScale.SIMPLE
    assert standard.scale is ArchitectureScale.STANDARD
    assert complex_routes.scale is ArchitectureScale.COMPLEX
    assert complex_state.scale is ArchitectureScale.COMPLEX


def test_profile_is_versioned_advisory_guidance_not_a_file_contract() -> None:
    profile = derive_architecture_profile(
        route_count=4,
        goal_count=3,
        shared_state_across_routes=True,
    )
    context = profile.as_prompt_context()
    brief = profile.render_brief()

    assert context["id"] == ARCHITECTURE_PROFILE_ID
    assert context["version"] == ARCHITECTURE_PROFILE_VERSION
    assert context["scale"] == "complex"
    assert context["signals"] == {
        "routeCount": 4,
        "goalCount": 3,
        "sharedStateAcrossRoutes": True,
    }
    assert context["advisoryOnly"] is True
    assert "nearest common layout" in brief
    assert "bounded vertical feature slices" in brief
    assert "not a fixed file plan, path allowlist" in brief
    assert "app/(product)" not in brief


def test_profile_context_round_trips_with_a_stable_fingerprint() -> None:
    profile = derive_architecture_profile(
        route_count=5,
        goal_count=4,
        shared_state_across_routes=True,
    )

    restored = ArchitectureProfile.from_prompt_context(profile.as_prompt_context())

    assert restored == profile
    assert restored.fingerprint() == profile.fingerprint()


def test_profile_context_rejects_policy_drift() -> None:
    profile = derive_architecture_profile(route_count=3, goal_count=2)
    context = profile.as_prompt_context()
    context["guidance"] = ["invent an unrelated service layer"]

    with pytest.raises(ValueError, match="guidance is inconsistent"):
        ArchitectureProfile.from_prompt_context(context)


def test_product_profile_detects_explicit_cross_route_browser_state() -> None:
    persisted = derive_product_architecture_profile(
        requirement="Persist the workflow in localStorage across routes and reloads.",
        route_count=3,
        goal_count=2,
    )
    single_route = derive_product_architecture_profile(
        requirement="使用本地存储持久化，刷新后恢复。",
        route_count=1,
        goal_count=1,
    )

    assert persisted.scale is ArchitectureScale.COMPLEX
    assert persisted.shared_state_across_routes is True
    assert single_route.scale is ArchitectureScale.SIMPLE
    assert single_route.shared_state_across_routes is False


@pytest.mark.parametrize(
    ("route_count", "goal_count"),
    ((0, 1), (1, 0), (-1, 1), (1, -1)),
)
def test_profile_rejects_non_positive_graph_signals(
    route_count: int,
    goal_count: int,
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        derive_architecture_profile(route_count=route_count, goal_count=goal_count)
