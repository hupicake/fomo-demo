from __future__ import annotations

from fomo.direct_pi.acceptance import compile_acceptance, compile_acceptance_suite
from fomo.direct_pi.contracts import AcceptanceContract
from fomo.direct_pi.goalgraph import NavigationRoute, NavigationVerificationSuite


def test_navigation_actions_compile_to_real_browser_history_and_viewport_steps() -> None:
    contract = AcceptanceContract.model_validate(
        {
            "criteria": [
                {
                    "id": "AC-navigation",
                    "title": "Navigate responsively",
                    "priority": "must",
                    "given": "The multi-route product is open",
                    "when": "The user navigates on a narrow screen and uses browser history",
                    "then": "The exact route remains browser-addressable",
                }
            ],
            "tests": [
                {
                    "id": "responsive-history",
                    "acceptanceId": "AC-navigation",
                    "title": "uses mobile navigation and browser history",
                    "actions": [
                        {"kind": "set_viewport", "width": 390, "height": 844},
                        {"kind": "goto", "path": "/"},
                        {"kind": "goto", "path": "/missions"},
                        {
                            "kind": "history_roundtrip",
                            "backPath": "/",
                            "forwardPath": "/missions",
                        },
                    ],
                    "assertions": [{"kind": "url", "path": "/missions"}],
                }
            ],
        }
    )

    compiled = compile_acceptance(contract)
    source = next(
        change.content
        for change in compiled.changes
        if change.path.endswith("responsive-history.smoke.spec.ts")
    )

    assert "await page.setViewportSize({ width: 390, height: 844 });" in source
    assert "await page.goBack();" in source
    assert 'await expect(page).toHaveURL(fomoAppUrl("/"));' in source
    assert "await page.goForward();" in source
    assert 'await expect(page).toHaveURL(fomoAppUrl("/missions"));' in source


def test_server_navigation_suite_compiles_exact_direct_shared_mobile_and_history_gates() -> None:
    suite = NavigationVerificationSuite(
        version=1,
        routes=(
            NavigationRoute(
                path="/",
                title="Home",
                owningGoalId="G-1",
                deepLinkable=True,
            ),
            NavigationRoute(
                path="/missions",
                title="Missions",
                owningGoalId="G-2",
                deepLinkable=True,
            ),
            NavigationRoute(
                path="/settings",
                title="Settings",
                owningGoalId="G-3",
                deepLinkable=True,
            ),
        ),
        mode="final_full",
    )

    compiled = compile_acceptance_suite((), navigation_suite=suite)
    sources = {change.path: change.content for change in compiled.changes}
    missions_direct_id = next(
        test_id
        for test_id, name in compiled.navigation_test_name_by_id.items()
        if name == "FOMO navigation direct load: Missions"
    )
    direct = sources[compiled.navigation_test_path_by_id[missions_direct_id]]
    shared = sources[
        "tests/fomo-acceptance/navigation-v1/shared-navigation.smoke.spec.ts"
    ]
    mobile = sources[
        "tests/fomo-acceptance/navigation-v1/mobile-navigation-390.smoke.spec.ts"
    ]
    history = sources[
        "tests/fomo-acceptance/navigation-v1/history-roundtrip.smoke.spec.ts"
    ]

    assert 'await page.goto(fomoAppPath("/missions"));' in direct
    assert 'name: "Missions", exact: true' in direct
    assert 'await expect(page).toHaveURL(fomoAppUrl("/missions"));' in direct
    assert "await page.reload();" in direct
    assert 'name: "Primary navigation", exact: true' in shared
    assert ".filter({ visible: true })" in shared
    assert '.getByRole("link", { name: "Missions", exact: true }).click();' in shared
    assert "await page.setViewportSize({ width: 390, height: 844 });" in mobile
    assert 'name: "Open navigation", exact: true' in mobile
    assert 'name: "Primary navigation", exact: true' in mobile
    assert "if ((await mobileLink0.count()) === 0)" in mobile
    assert mobile.count('name: "Primary navigation", exact: true') == 4
    assert "await expect(mobileLink0).toHaveCount(1);" in mobile
    assert "await page.goBack();" in history
    assert "await page.goForward();" in history
    assert history.count("await page.goBack();") == 2
    assert 'await expect(page).toHaveURL(fomoAppUrl("/settings"));' in history
