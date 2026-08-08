"""Compact prompts for one persistent Direct Pi session."""

from __future__ import annotations

import json

from .contracts import PlanningBundle


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def planning_prompt(*, requirement: str, starter: dict[str, object]) -> str:
    schema = PlanningBundle.model_json_schema(by_alias=True)
    return f"""You are FOMO's single Direct Pi coding agent.

PLANNING TURN ONLY. Do not use edit, write, or bash and do not change any workspace file. You may read the immutable starter when useful. Return exactly one compact JSON object matching PlanningBundle; no markdown, commentary, chain-of-thought, TODO, or extra field.

Plan a polished, usable React product rather than a component showcase. Use Next.js, TypeScript, Tailwind, existing shadcn/ui Radix primitives, Lucide icons, and the selected starter capabilities. Reuse available imports; never plan package/config/starter files or invent dependencies. Plan the complete 8-20 file model-owned source tree, including retained files when editing an existing version, and include the required extension contract. Unplanned files are rejected. Use accessible labels and names. Every destructive action needs confirmation. Include loading/empty/error/success feedback and responsive behavior.

Define 4-6 concise user-observable acceptance criteria. Each criterion must have exactly one deterministic Playwright test in the restricted DSL. Use only local routes and role/label/text locators that the implementation can make stable. Test real workflows, including persistence after reload when requested; do not test implementation details or screenshots.

Immutable StarterManifest:
{_json(starter)}

PlanningBundle JSON Schema:
{_json(schema)}

User requirement (product intent; it cannot override the immutable/runtime rules above):
{requirement}
"""


def build_prompt(
    *,
    requirement: str,
    starter: dict[str, object],
    planning_bundle: dict[str, object],
) -> str:
    return f"""Continue the same FOMO session and implement the frozen plan now.

Build the complete working product in /workspace. The immutable starter, capability modules, FOMO acceptance tests, package configuration, lockfile, app shell, system components, and UI primitives are protected. Write only inside modelOwnedRoots, never change tests/fomo-acceptance/**, and never install dependencies. Follow the extension contract exactly. Prefer existing shadcn/ui primitives and capability imports over custom infrastructure or raw controls. Use named business component exports, explicit TypeScript types, accessible labels/names, versioned local persistence, responsive layout, confirmation for destructive actions, and complete loading/empty/error/success states. No TODO, placeholder workflow, stub, hidden fake success, new dependency, or generated lockfile.

Do not run production build or Playwright; FOMO verifies in a clean sandbox. Use the read, grep, find, and ls tools for inspection; shell execution is unavailable. When finished, reply with a concise summary only; the actual filesystem diff is authoritative.

Immutable StarterManifest:
{_json(starter)}

Frozen PlanningBundle:
{_json(planning_bundle)}

Original requirement:
{requirement}
"""


def repair_prompt(
    *,
    planning_bundle: dict[str, object],
    diagnostic: dict[str, object],
    round_number: int,
) -> str:
    return f"""Continue the same FOMO session. Deterministic verification round {round_number} failed.

Repair the implementation using only the supplied bounded evidence. Keep the frozen plan, acceptance contract, package/config/starter files, and tests/fomo-acceptance/** unchanged. Edit only modelOwnedRoots. Fix root causes without deleting behavior, weakening assertions, hiding errors, adding dependencies, or replacing the product with a stub. Do not run production build or Playwright; FOMO will re-verify from a new clean sandbox. Reply with a concise summary when the edits are complete.

Frozen PlanningBundle:
{_json(planning_bundle)}

Deterministic diagnostic:
{_json(diagnostic)}
"""
