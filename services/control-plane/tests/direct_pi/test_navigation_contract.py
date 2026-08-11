from __future__ import annotations

from fomo.direct_pi.acceptance import compile_acceptance
from fomo.direct_pi.contracts import AcceptanceContract


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
