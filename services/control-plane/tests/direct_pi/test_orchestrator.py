from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from fomo.direct_pi import DirectPiOrchestrator
from fomo.direct_pi.acceptance import ACCEPTANCE_CONFIG_PATH
from fomo.direct_pi.contracts import PlanningBundle
from fomo.direct_pi.goalgraph import parse_goal_graph_draft
from fomo.direct_pi.orchestrator import DirectPiOrchestrationError
from fomo.direct_pi.prompts import (
    GOAL_GRAPH_PLANNING_POLICY,
    goal_graph_planning_correction_prompt,
    goal_graph_planning_prompt,
    planning_correction_prompt,
    planning_prompt,
)
from fomo.direct_pi.session import (
    DirectPiAwaitingUser,
    DirectPiSession,
    DirectPiSessionError,
)
from fomo.direct_pi.verification import VerificationOutcome
from fomo.direct_pi.workspace import (
    CandidateCheckpoint,
    VerificationSnapshot,
    WorkspaceContractError,
    WorkspaceManager,
    fomo_runner_command,
)
from fomo.fomo_pi_ds import (
    FOMO_PI_PLANNING_MODEL,
    PiBridgeEnvelope,
    PiBridgeFailed,
    PiBridgeResult,
    PiTransportResult,
    RunVirtualKey,
)
from fomo.persistence.models import TraceLinkRecord
from fomo.sandbox.base import ExecResult, FileChange, SandboxRef
from fomo.schemas import RunStatus
from fomo.starter import resolve_starter_manifest

from ._git_sandbox import CANDIDATE_SHA, GitAwareSandbox, persisted_sandbox_id

_HARNESS_PATH = "tests/harness/starter.smoke.spec.ts"


def _playwright_command(path: str) -> str:
    return fomo_runner_command(
        bin_name="playwright",
        args=f"test {path} --config={ACCEPTANCE_CONFIG_PATH} --project=chromium --reporter=json",
    )


def _playwright_report(title: str) -> str:
    return json.dumps(
        {
            "errors": [],
            "suites": [
                {
                    "specs": [
                        {
                            "title": title,
                            "errors": [],
                            "tests": [
                                {
                                    "status": "expected",
                                    "results": [{"status": "passed"}],
                                }
                            ],
                        }
                    ]
                }
            ],
        }
    )


def _playwright_results() -> dict[str, ExecResult]:
    return {
        _playwright_command(_HARNESS_PATH): ExecResult(
            0, _playwright_report("starter renders a stable application shell"), ""
        ),
        _playwright_command("tests/fomo-acceptance/search-books.smoke.spec.ts"): ExecResult(
            0, _playwright_report("searches books by title"), ""
        ),
        _playwright_command("tests/fomo-acceptance/create-book.smoke.spec.ts"): ExecResult(
            0, _playwright_report("creates and persists a book"), ""
        ),
    }


def _plan() -> dict[str, Any]:
    return {
        "buildPlan": {
            "title": "Library desk",
            "summary": "Search and maintain a persistent book collection.",
            "visualPreset": "indigo",
            "routes": ["/"],
            "files": [
                {
                    "path": "app/(generated)/composition.tsx",
                    "purpose": "Compose the product page.",
                    "acceptanceIds": ["AC-1", "AC-2"],
                },
                {
                    "path": "components/features/library-desk.tsx",
                    "purpose": "Accessible CRUD workflow.",
                    "acceptanceIds": ["AC-1", "AC-2"],
                },
                {
                    "path": "lib/domain/books.ts",
                    "purpose": "Typed book state.",
                    "acceptanceIds": ["AC-1", "AC-2"],
                },
            ],
        },
        "acceptanceContract": {
            "criteria": [
                {
                    "id": "AC-1",
                    "title": "Search books",
                    "priority": "must",
                    "given": "Books exist",
                    "when": "The user searches by title",
                    "then": "Only matching books remain",
                },
                {
                    "id": "AC-2",
                    "title": "Create a durable book",
                    "priority": "must",
                    "given": "The library is open",
                    "when": "The user creates a book and reloads",
                    "then": "The book remains visible",
                },
            ],
            "tests": [
                {
                    "id": "search-books",
                    "acceptanceId": "AC-1",
                    "title": "searches books by title",
                    "actions": [
                        {"kind": "goto", "path": "/"},
                        {
                            "kind": "fill",
                            "target": {"by": "label", "value": "Search books"},
                            "value": "Dune",
                        },
                    ],
                    "assertions": [
                        {"kind": "visible", "target": {"by": "text", "value": "Dune"}}
                    ],
                },
                {
                    "id": "create-book",
                    "acceptanceId": "AC-2",
                    "title": "creates and persists a book",
                    "actions": [
                        {"kind": "goto", "path": "/"},
                        {
                            "kind": "click",
                            "target": {"by": "role", "value": "button", "name": "Add book"},
                        },
                        {
                            "kind": "fill",
                            "target": {"by": "label", "value": "Title"},
                            "value": "Dune",
                        },
                        {"kind": "reload"},
                    ],
                    "assertions": [
                        {"kind": "visible", "target": {"by": "text", "value": "Dune"}}
                    ],
                },
            ],
        },
    }


class _Gateway:
    def __init__(self) -> None:
        self.blocked = False
        self.issued: list[dict[str, Any]] = []

    async def issue(self, **values: Any) -> RunVirtualKey:
        self.issued.append(dict(values))
        return RunVirtualKey(
            run_id=str(values["run_id"]),
            key_alias=f"fomo-run-{values['run_id']}",
            duration_seconds=int(values["duration_seconds"]),
            secret="sk-test-run-key",
        )

    async def block(self, _key: RunVirtualKey) -> None:
        self.blocked = True


class _Transport:
    """Plays the planning turn and the single full-project build turn.

    BuildPlan is advisory: the build turn applies the candidate files and
    reports a handoff; there is no batch/allowlist structure anywhere.
    """

    def __init__(
        self,
        sandbox: GitAwareSandbox,
        *,
        build_only: bool = False,
        extra_helper: bool = False,
        fail_planning: Exception | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.calls = 0
        self.build_call = 1 if build_only else 2
        self.extra_helper = extra_helper
        self.fail_planning = fail_planning

    async def run(self, ref, invocation, *, on_event=None, on_diagnostic=None, cancel_event=None):
        self.calls += 1
        text = json.dumps(_plan(), ensure_ascii=False, separators=(",", ":"))
        if self.calls == self.build_call:
            changes = [
                FileChange(
                    path="app/(generated)/composition.tsx",
                    content='import { LibraryDesk } from "@/components/features/library-desk";\nexport function GeneratedComposition() { return <LibraryDesk />; }\n',
                    operation="modify",
                ),
                FileChange(
                    path="components/features/library-desk.tsx",
                    content='"use client";\nexport function LibraryDesk() { return <main><label>Search books<input aria-label="Search books" /></label><button>Add book</button><span>Dune</span></main>; }\n',
                ),
                FileChange(
                    path="lib/domain/books.ts",
                    content="export type Book = { id: string; title: string };\n",
                ),
            ]
            if self.extra_helper:
                changes.append(
                    FileChange(
                        path="components/features/native-select.tsx",
                        content=(
                            "export function NativeSelect() { return "
                            '<select aria-label="Inventory" />; }\n'
                        ),
                    )
                )
            await self.sandbox.apply_changes(
                ref,
                changes,
            )
            text = "Implemented the library project."
        elif self.fail_planning is not None and self.calls == 1:
            raise self.fail_planning
        session_id = invocation.request.session_id
        structured = invocation.request.structured_output_schema is not None
        stats = {
            "sessionId": session_id,
            "userMessages": self.calls,
            "assistantMessages": self.calls,
            "toolCalls": 3,
            "toolResults": 3,
            "totalMessages": 8,
            "tokens": {"input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0, "total": 150},
            "cost": 0.01,
        }
        envelopes = [
            PiBridgeEnvelope(seq=1, type="started", payload={"sessionId": session_id}),
            PiBridgeEnvelope(seq=2, type="pi.event", payload={"kind": "agent_start"}),
        ]
        if structured:
            envelopes.extend(
                [
                    PiBridgeEnvelope(
                        seq=3,
                        type="pi.event",
                        payload={
                            "kind": "tool_start",
                            "toolCallId": "structured-1",
                            "toolName": "submit_structured_output",
                            "args": _plan(),
                        },
                    ),
                    PiBridgeEnvelope(
                        seq=4,
                        type="pi.event",
                        payload={
                            "kind": "tool_end",
                            "toolCallId": "structured-1",
                            "toolName": "submit_structured_output",
                            "isError": False,
                        },
                    ),
                    PiBridgeEnvelope(
                        seq=5,
                        type="pi.event",
                        payload={
                            "kind": "turn_end",
                            "role": "assistant",
                            "stopReason": "toolUse",
                            "text": "",
                        },
                    ),
                    PiBridgeEnvelope(
                        seq=6,
                        type="pi.event",
                        payload={"kind": "agent_settled"},
                    ),
                    PiBridgeEnvelope(
                        seq=7,
                        type="completed",
                        payload={"sessionId": session_id, "stats": stats},
                    ),
                ]
            )
        else:
            envelopes.extend(
                [
                    PiBridgeEnvelope(
                        seq=3,
                        type="pi.event",
                        payload={
                            "kind": "turn_end",
                            "role": "assistant",
                            "text": text,
                        },
                    ),
                    PiBridgeEnvelope(
                        seq=4,
                        type="pi.event",
                        payload={"kind": "agent_settled"},
                    ),
                    PiBridgeEnvelope(
                        seq=5,
                        type="completed",
                        payload={"sessionId": session_id, "stats": stats},
                    ),
                ]
            )
        if on_event is not None:
            for envelope in envelopes:
                await on_event(envelope)
        return PiTransportResult(
            bridge=PiBridgeResult(
                started={"sessionId": session_id},
                events=tuple(envelopes),
                completed={"sessionId": session_id, "stats": stats},
            ),
            execution_id=f"fake-{self.calls}",
            exit_code=0,
            stderr="",
        )


def _goal_graph_plan() -> dict[str, object]:
    first = _plan()["acceptanceContract"]
    second = json.loads(json.dumps(first))
    second["criteria"] = [
        {
            **second["criteria"][1],
            "id": "AC-2",
            "title": "Create a durable book",
        }
    ]
    second["tests"] = [second["tests"][1]]
    first = json.loads(json.dumps(first))
    first["criteria"] = [first["criteria"][0]]
    first["tests"] = [first["tests"][0]]
    return {
        "schemaVersion": 1,
        "productOutcome": "Users can search and maintain a durable library.",
        "goals": [
            {
                "goalId": "G-1",
                "title": "Search the library",
                "productOutcome": "Users can search books by title.",
                "userVisible": True,
                "dependsOn": [],
                "acceptance": first,
            },
            {
                "goalId": "G-2",
                "title": "Create durable books",
                "productOutcome": "Users can create books that survive reload.",
                "userVisible": True,
                "dependsOn": ["G-1"],
                "acceptance": second,
            },
        ],
    }


def test_goal_graph_planning_prompts_enforce_coarse_vertical_policy() -> None:
    prompt = goal_graph_planning_prompt(
        requirement="Build one responsive landing page.",
        starter={"routes": ["/"]},
    )
    correction = goal_graph_planning_correction_prompt(validation_error="invalid graph")

    assert GOAL_GRAPH_PLANNING_POLICY == "coarse-v2"
    assert "define 1-3 coarse-grained" in prompt
    assert "single-route, frontend-only page" in prompt
    assert "prefer exactly one goal" in prompt
    assert "hero, features, pricing, FAQ, and footer" in prompt
    assert "1-8 concise observable acceptance criteria" in prompt
    assert "at most 12 criteria total" in prompt
    assert GOAL_GRAPH_PLANNING_POLICY in correction
    assert "collapse a single-route frontend page into one complete goal" in correction


def test_structured_planning_prompts_allow_only_bounded_schema_refills() -> None:
    prompts = (
        goal_graph_planning_prompt(requirement="Build a page.", starter={}),
        goal_graph_planning_correction_prompt(validation_error="invalid graph"),
        planning_prompt(requirement="Build a page.", starter={}),
        planning_correction_prompt(validation_error="invalid bundle"),
    )

    for prompt in prompts:
        assert "succeeds exactly once" in prompt
        assert "at most 3 total attempts" in prompt
        assert "Stop immediately after the successful submission" in prompt
        assert "emit prose or JSON as assistant text" in prompt


class _GoalGraphTransport:
    def __init__(self, sandbox: GitAwareSandbox) -> None:
        self.sandbox = sandbox
        self.calls = 0
        self.session_ids: list[str] = []

    async def run(self, ref, invocation, *, on_event=None, **_kwargs):
        self.calls += 1
        self.session_ids.append(invocation.request.session_id)
        structured = invocation.request.structured_output_schema is not None
        if self.calls == 1:
            text = json.dumps(_goal_graph_plan(), separators=(",", ":"))
        elif self.calls == 2:
            await self.sandbox.apply_changes(
                ref,
                [
                    FileChange(
                        path="components/features/library-desk.tsx",
                        content=(
                            '"use client";\nexport function LibraryDesk() { '
                            'return <main><label>Search books<input aria-label="Search books" />'
                            "</label><span>Dune</span></main>; }\n"
                        ),
                    )
                ],
            )
            text = "Claimed G-1 implementation."
        else:
            await self.sandbox.apply_changes(
                ref,
                [
                    FileChange(
                        path="lib/domain/books.ts",
                        content="export type Book = { id: string; title: string };\n",
                    )
                ],
            )
            text = "Claimed G-2 implementation."
        initial = {
            "tokens": {
                "input": (self.calls - 1) * 100,
                "output": (self.calls - 1) * 50,
                "cacheRead": 0,
                "cacheWrite": 0,
            },
            "toolCalls": self.calls - 1,
            "cost": (self.calls - 1) * 0.01,
        }
        final = {
            "tokens": {
                "input": self.calls * 100,
                "output": self.calls * 50,
                "cacheRead": 0,
                "cacheWrite": 0,
                "total": self.calls * 150,
            },
            "toolCalls": self.calls,
            "cost": self.calls * 0.01,
        }
        if structured:
            events = (
                PiBridgeEnvelope(
                    seq=1,
                    type="pi.event",
                    payload={"kind": "turn_start"},
                ),
                PiBridgeEnvelope(
                    seq=2,
                    type="pi.event",
                    payload={
                        "kind": "tool_start",
                        "toolCallId": "structured-1",
                        "toolName": "submit_structured_output",
                        "args": _goal_graph_plan(),
                    },
                ),
                PiBridgeEnvelope(
                    seq=3,
                    type="pi.event",
                    payload={
                        "kind": "tool_end",
                        "toolCallId": "structured-1",
                        "toolName": "submit_structured_output",
                        "isError": False,
                    },
                ),
                PiBridgeEnvelope(
                    seq=4,
                    type="pi.event",
                    payload={
                        "kind": "turn_end",
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "text": "",
                    },
                ),
                PiBridgeEnvelope(
                    seq=5,
                    type="pi.event",
                    payload={"kind": "agent_settled"},
                ),
            )
        else:
            events = (
                PiBridgeEnvelope(
                    seq=1,
                    type="pi.event",
                    payload={"kind": "turn_start"},
                ),
                PiBridgeEnvelope(
                    seq=2,
                    type="pi.event",
                    payload={
                        "kind": "message_delta",
                        "deltaType": "text_delta",
                        "delta": text,
                    },
                ),
                PiBridgeEnvelope(
                    seq=3,
                    type="pi.event",
                    payload={
                        "kind": "turn_end",
                        "role": "assistant",
                        "text": text,
                    },
                ),
                PiBridgeEnvelope(
                    seq=4,
                    type="pi.event",
                    payload={"kind": "agent_settled"},
                ),
            )
        if on_event is not None:
            for event in events:
                await on_event(event)
        return PiTransportResult(
            bridge=PiBridgeResult(
                started={"initialStats": initial},
                events=events,
                completed={"stats": final},
            ),
            execution_id=f"goal-turn-{self.calls}",
            exit_code=0,
            stderr="",
        )


def _one_goal_graph_plan() -> dict[str, object]:
    plan = json.loads(json.dumps(_goal_graph_plan()))
    first_goal = plan["goals"][0]
    first_goal["acceptance"]["criteria"] = first_goal["acceptance"]["criteria"][:1]
    first_goal["acceptance"]["tests"] = first_goal["acceptance"]["tests"][:1]
    plan["goals"] = [first_goal]
    return plan


def _cumulative_stats(turn: int) -> dict[str, object]:
    return {
        "tokens": {
            "input": turn * 100,
            "output": turn * 50,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": turn * 150,
        },
        "toolCalls": turn,
        "cost": turn * 0.01,
    }


class _PlanningThenQuestionTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.session_id: str | None = None
        self.sandbox_id: str | None = None

    async def run(self, ref, invocation, *, on_event=None, **_kwargs):
        self.calls += 1
        self.session_id = invocation.request.session_id
        self.sandbox_id = ref.id
        initial = _cumulative_stats(self.calls - 1)
        final = _cumulative_stats(self.calls)
        completed: dict[str, object] = {"stats": final}
        if self.calls == 1:
            plan = _one_goal_graph_plan()
            events = (
                PiBridgeEnvelope(
                    seq=1,
                    type="pi.event",
                    payload={
                        "kind": "tool_start",
                        "toolCallId": "structured-plan",
                        "toolName": "submit_structured_output",
                        "args": plan,
                    },
                ),
                PiBridgeEnvelope(
                    seq=2,
                    type="pi.event",
                    payload={
                        "kind": "tool_end",
                        "toolCallId": "structured-plan",
                        "toolName": "submit_structured_output",
                        "isError": False,
                    },
                ),
                PiBridgeEnvelope(
                    seq=3,
                    type="pi.event",
                    payload={"kind": "agent_settled"},
                ),
            )
        else:
            assert invocation.request.user_input_enabled
            input_request = {
                "requestId": "input-layout",
                "question": "Which layout should I implement?",
                "choices": ["Grid", "List"],
                "allowFreeform": False,
            }
            events = (
                PiBridgeEnvelope(
                    seq=1,
                    type="pi.event",
                    payload={
                        "kind": "input_request",
                        "inputRequest": input_request,
                    },
                ),
                PiBridgeEnvelope(
                    seq=2,
                    type="pi.event",
                    payload={"kind": "agent_settled"},
                ),
            )
            completed["inputRequest"] = input_request
        if on_event is not None:
            for event in events:
                await on_event(event)
        return PiTransportResult(
            bridge=PiBridgeResult(
                started={"initialStats": initial},
                events=events,
                completed=completed,
            ),
            execution_id=f"clarification-{self.calls}",
            exit_code=0,
            stderr="",
        )


class _AnswerContinuationTransport:
    def __init__(
        self,
        sandbox: GitAwareSandbox,
        *,
        expected_session_id: str,
        expected_sandbox_id: str,
        unavailable: bool = False,
    ) -> None:
        self.sandbox = sandbox
        self.expected_session_id = expected_session_id
        self.expected_sandbox_id = expected_sandbox_id
        self.unavailable = unavailable
        self.calls = 0

    async def run(self, ref, invocation, *, on_event=None, **_kwargs):
        self.calls += 1
        assert invocation.request.require_resume
        assert invocation.request.session_id == self.expected_session_id
        assert ref.id == self.expected_sandbox_id
        assert "Grid" in invocation.request.prompt
        if self.unavailable:
            raise PiBridgeFailed(
                {
                    "code": "session_resume_unavailable",
                    "message": "session cache is missing",
                    "phase": "boot",
                }
            )
        await self.sandbox.apply_changes(
            ref,
            [
                FileChange(
                    path="components/features/library-desk.tsx",
                    content=(
                        '"use client";\nexport function LibraryDesk() { '
                        'return <main><label>Search books<input aria-label="Search books" />'
                        "</label><span>Dune</span></main>; }\n"
                    ),
                )
            ],
        )
        text = "Implemented the clarified Grid layout."
        events = (
            PiBridgeEnvelope(
                seq=1,
                type="pi.event",
                payload={"kind": "turn_start"},
            ),
            PiBridgeEnvelope(
                seq=2,
                type="pi.event",
                payload={
                    "kind": "message_delta",
                    "deltaType": "text_delta",
                    "delta": text,
                },
            ),
            PiBridgeEnvelope(
                seq=3,
                type="pi.event",
                payload={
                    "kind": "turn_end",
                    "role": "assistant",
                    "text": text,
                },
            ),
            PiBridgeEnvelope(
                seq=4,
                type="pi.event",
                payload={"kind": "agent_settled"},
            ),
        )
        if on_event is not None:
            for event in events:
                await on_event(event)
        return PiTransportResult(
            bridge=PiBridgeResult(
                started={"initialStats": _cumulative_stats(2)},
                events=events,
                completed={"stats": _cumulative_stats(3)},
            ),
            execution_id="clarification-answer",
            exit_code=0,
            stderr="",
        )


def test_direct_pi_machine_result_reconstructs_text_beyond_public_event_limit() -> None:
    full_text = '{"plan":"' + ("x" * 9_000) + '"}'
    events = (
        PiBridgeEnvelope(seq=1, type="pi.event", payload={"kind": "turn_start"}),
        PiBridgeEnvelope(
            seq=2,
            type="pi.event",
            payload={
                "kind": "message_delta",
                "deltaType": "text_delta",
                "delta": full_text[:5_000],
            },
        ),
        PiBridgeEnvelope(
            seq=3,
            type="pi.event",
            payload={
                "kind": "message_delta",
                "deltaType": "text_delta",
                "delta": full_text[5_000:],
            },
        ),
        PiBridgeEnvelope(
            seq=4,
            type="pi.event",
            payload={
                "kind": "turn_end",
                "role": "assistant",
                "text": full_text[:8_192] + "…[truncated]",
            },
        ),
    )
    result = PiTransportResult(
        bridge=PiBridgeResult(started={}, events=events, completed={}),
        execution_id="long-result",
        exit_code=0,
        stderr="",
    )

    assert DirectPiSession._last_assistant_text(result) == full_text


def _structured_pi_result(
    events: tuple[PiBridgeEnvelope, ...],
) -> PiTransportResult:
    return PiTransportResult(
        bridge=PiBridgeResult(started={}, events=events, completed={}),
        execution_id="structured-result",
        exit_code=0,
        stderr="",
    )


def _structured_start(
    seq: int,
    tool_call_id: str,
    args: dict[str, object],
    *,
    tool_name: str = "submit_structured_output",
) -> PiBridgeEnvelope:
    return PiBridgeEnvelope(
        seq=seq,
        type="pi.event",
        payload={
            "kind": "tool_start",
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "args": args,
        },
    )


def _structured_end(
    seq: int,
    tool_call_id: str,
    is_error: bool,
    *,
    tool_name: str = "submit_structured_output",
) -> PiBridgeEnvelope:
    return PiBridgeEnvelope(
        seq=seq,
        type="pi.event",
        payload={
            "kind": "tool_end",
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "isError": is_error,
        },
    )


def test_direct_pi_structured_output_accepts_failed_attempt_then_valid_refill() -> None:
    events = (
        _structured_start(1, "structured-1", {"answer": 42}),
        _structured_end(2, "structured-1", True),
        _structured_start(3, "structured-2", {"answer": "valid"}),
        _structured_end(4, "structured-2", False),
    )

    result = DirectPiSession._structured_output_text(_structured_pi_result(events))

    assert json.loads(result) == {"answer": "valid"}


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ((), "successfully exactly once"),
        (
            (
                PiBridgeEnvelope(
                    seq=1,
                    type="pi.event",
                    payload={
                        "kind": "turn_end",
                        "role": "assistant",
                        "text": '{"answer":"prose-is-not-machine-output"}',
                    },
                ),
            ),
            "successfully exactly once",
        ),
        (
            (
                _structured_start(1, "structured-1", {"answer": 1}),
                _structured_end(2, "structured-1", True),
                _structured_start(3, "structured-2", {"answer": 2}),
                _structured_end(4, "structured-2", True),
                _structured_start(5, "structured-3", {"answer": 3}),
                _structured_end(6, "structured-3", True),
            ),
            "successfully exactly once",
        ),
        (
            (
                _structured_start(1, "structured-1", {"answer": "first"}),
                _structured_end(2, "structured-1", False),
                _structured_start(3, "structured-2", {"answer": "second"}),
                _structured_end(4, "structured-2", False),
            ),
            "stop after structured output succeeds",
        ),
        (
            (
                _structured_start(
                    1,
                    "native-1",
                    {"path": "package.json"},
                    tool_name="read",
                ),
                _structured_end(2, "native-1", False, tool_name="read"),
            ),
            "native tool",
        ),
        (
            (_structured_end(1, "structured-missing", False),),
            "unmatched structured tool progress",
        ),
        (
            (_structured_start(1, "structured-1", {"answer": "ok"}),),
            "did not complete",
        ),
        (
            (
                _structured_start(1, "structured-1", {"answer": 1}),
                _structured_end(2, "structured-1", True),
                _structured_start(3, "structured-2", {"answer": 2}),
                _structured_end(4, "structured-2", True),
                _structured_start(5, "structured-3", {"answer": 3}),
                _structured_end(6, "structured-3", True),
                _structured_start(7, "structured-4", {"answer": "late"}),
            ),
            "attempt limit",
        ),
    ],
)
def test_direct_pi_structured_output_fails_closed_for_invalid_tool_lifecycle(
    events: tuple[PiBridgeEnvelope, ...],
    message: str,
) -> None:
    with pytest.raises(DirectPiSessionError, match=message):
        DirectPiSession._structured_output_text(_structured_pi_result(events))


def test_direct_pi_token_budget_excludes_cache_reads_but_counts_cache_writes() -> None:
    assert DirectPiSession._budgeted_token_total(
        {
            "input": 53_542,
            "output": 104_726,
            "cacheRead": 2_683_136,
            "cacheWrite": 7,
            "total": 2_841_411,
        }
    ) == 158_275


def _session_repository() -> SimpleNamespace:
    return SimpleNamespace(
        append_event=AsyncMock(return_value=None),
        is_active_lease=AsyncMock(return_value=True),
        is_cancel_requested=AsyncMock(return_value=False),
    )


def _session(settings, repository=None) -> DirectPiSession:
    return DirectPiSession(
        repository or _session_repository(),
        _RecordingTransport(),
        settings,
        RunVirtualKey(
            run_id="run-1",
            key_alias="fomo-run-1",
            duration_seconds=300,
            secret="sk-test-run-key",
        ),
        run_id="run-1",
        lease_token="lease-1",
        started_at=time.monotonic(),
    )


class _RecordingTransport:
    """Captures per-turn PiRequest fields and returns a clean turn result."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.stop_reasons: list[str] = []

    async def run(self, _ref, invocation, *, on_event=None, **_kwargs):
        self.requests.append(invocation.request)
        reason = self.stop_reasons.pop(0) if self.stop_reasons else "stop"
        text = "ok" if reason == "stop" else ""
        if invocation.request.structured_output_schema is not None:
            events = (
                PiBridgeEnvelope(
                    seq=1,
                    type="pi.event",
                    payload={"kind": "turn_start"},
                ),
                PiBridgeEnvelope(
                    seq=2,
                    type="pi.event",
                    payload={
                        "kind": "tool_start",
                        "toolCallId": "structured-1",
                        "toolName": "submit_structured_output",
                        "args": {"answer": "ok"},
                    },
                ),
                PiBridgeEnvelope(
                    seq=3,
                    type="pi.event",
                    payload={
                        "kind": "tool_end",
                        "toolCallId": "structured-1",
                        "toolName": "submit_structured_output",
                        "isError": False,
                    },
                ),
                PiBridgeEnvelope(
                    seq=4,
                    type="pi.event",
                    payload={
                        "kind": "turn_end",
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "text": "ignored prose",
                    },
                ),
                PiBridgeEnvelope(
                    seq=5,
                    type="pi.event",
                    payload={"kind": "agent_settled"},
                ),
            )
        else:
            events = (
                PiBridgeEnvelope(
                    seq=1,
                    type="pi.event",
                    payload={"kind": "turn_start"},
                ),
                PiBridgeEnvelope(
                    seq=2,
                    type="pi.event",
                    payload={
                        "kind": "message_delta",
                        "deltaType": "text_delta",
                        "delta": text,
                    },
                ),
                PiBridgeEnvelope(
                    seq=3,
                    type="pi.event",
                    payload={
                        "kind": "turn_end",
                        "role": "assistant",
                        "stopReason": reason,
                        "text": text,
                    },
                ),
                PiBridgeEnvelope(
                    seq=4,
                    type="pi.event",
                    payload={"kind": "agent_settled"},
                ),
            )
        stats = {
            "tokens": {"input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0, "total": 150},
            "toolCalls": int(invocation.request.structured_output_schema is not None),
            "cost": 0,
        }
        if on_event is not None:
            for event in events:
                await on_event(event)
        return PiTransportResult(
            bridge=PiBridgeResult(
                started={},
                events=events,
                completed={"stats": stats},
            ),
            execution_id=f"turn-{len(self.requests)}",
            exit_code=0,
            stderr="",
        )


class _InputRequestTransport(_RecordingTransport):
    async def run(self, _ref, invocation, *, on_event=None, **_kwargs):
        self.requests.append(invocation.request)
        input_request = {
            "requestId": "input-123",
            "question": "Which layout?",
            "choices": ["Grid", "List"],
            "allowFreeform": False,
        }
        events = (
            PiBridgeEnvelope(
                seq=1,
                type="pi.event",
                payload={
                    "kind": "input_request",
                    "inputRequest": input_request,
                },
            ),
            PiBridgeEnvelope(
                seq=2,
                type="pi.event",
                payload={"kind": "agent_settled"},
            ),
        )
        if on_event is not None:
            for event in events:
                await on_event(event)
        return PiTransportResult(
            bridge=PiBridgeResult(
                started={},
                events=events,
                completed={
                    "inputRequest": input_request,
                    "stats": {
                        "tokens": {
                            "input": 10,
                            "output": 5,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "total": 15,
                        },
                        "toolCalls": 1,
                        "cost": 0,
                    },
                },
            ),
            execution_id="turn-input",
            exit_code=0,
            stderr="",
        )


@pytest.mark.asyncio
async def test_direct_pi_stage_contract_maps_model_thinking_and_inactivity_budget(settings) -> None:
    transport = _RecordingTransport()
    session = DirectPiSession(
        _session_repository(),
        transport,
        settings,
        RunVirtualKey(
            run_id="run-1",
            key_alias="fomo-run-1",
            duration_seconds=300,
            secret="sk-test-run-key",
        ),
        run_id="run-1",
        lease_token="lease-1",
        started_at=time.monotonic(),
    )
    ref = SandboxRef(id="sandbox-1", project_id="project-1")

    assert await session.invoke(ref, "plan", stage="planning") == "ok"
    assert await session.invoke(ref, "build it", stage="building") == "ok"
    assert await session.invoke(ref, "repair it", stage="repairing") == "ok"

    planning, building, repairing = transport.requests
    assert planning.model == FOMO_PI_PLANNING_MODEL
    assert planning.thinking == "high"
    assert planning.activity_silence_seconds == settings.model_request_timeout_seconds
    assert building.model == FOMO_PI_PLANNING_MODEL
    assert building.thinking == "high"
    assert building.activity_silence_seconds == settings.model_request_timeout_seconds
    assert repairing.model == FOMO_PI_PLANNING_MODEL
    assert repairing.thinking == "high"
    assert repairing.activity_silence_seconds == settings.model_request_timeout_seconds
    assert all(request.context_window == settings.pi_context_window for request in transport.requests)


@pytest.mark.asyncio
async def test_direct_pi_structured_planning_returns_only_virtual_tool_arguments(
    settings,
) -> None:
    transport = _RecordingTransport()
    session = DirectPiSession(
        _session_repository(),
        transport,
        settings,
        RunVirtualKey(
            run_id="run-1",
            key_alias="fomo-run-1",
            duration_seconds=300,
            secret="sk-test-run-key",
        ),
        run_id="run-1",
        lease_token="lease-1",
        started_at=time.monotonic(),
    )
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }

    result = await session.invoke(
        SandboxRef(id="sandbox-1", project_id="project-1"),
        "fill the form",
        stage="planning",
        structured_output_schema=schema,
    )

    assert json.loads(result) == {"answer": "ok"}
    assert transport.requests[0].structured_output_schema == schema
    with pytest.raises(DirectPiSessionError, match="only available during planning"):
        await session.invoke(
            SandboxRef(id="sandbox-1", project_id="project-1"),
            "build",
            stage="building",
            structured_output_schema=schema,
        )


@pytest.mark.asyncio
async def test_direct_pi_persists_only_structured_user_input_requests(settings) -> None:
    repository = _session_repository()
    repository.wait_for_user_input = AsyncMock(return_value=None)
    transport = _InputRequestTransport()
    session = DirectPiSession(
        repository,
        transport,
        settings,
        RunVirtualKey(
            run_id="run-1",
            key_alias="fomo-run-1",
            duration_seconds=300,
            secret="sk-test-run-key",
        ),
        run_id="run-1",
        lease_token="lease-1",
        started_at=time.monotonic(),
    )
    ref = SandboxRef(id="sandbox-1", project_id="project-1")

    with pytest.raises(DirectPiAwaitingUser):
        await session.invoke(
            ref,
            "build",
            stage="building",
            goal_id="G-1",
            continuation_key="goal_graph.goal_build",
            continuation_context={"baselineHashes": {"app/page.tsx": "a" * 64}},
        )

    assert transport.requests[0].user_input_enabled
    repository.wait_for_user_input.assert_awaited_once()
    _run_id, persisted = repository.wait_for_user_input.await_args.args
    assert persisted.question == "Which layout?"
    assert persisted.choices == ["Grid", "List"]
    assert repository.wait_for_user_input.await_args.kwargs["sandbox_id"] == ref.id


@pytest.mark.asyncio
async def test_direct_pi_answer_turn_requires_existing_session_and_completes_cursor(
    settings,
) -> None:
    repository = _session_repository()
    repository.complete_run_continuation = AsyncMock(return_value=None)
    transport = _RecordingTransport()
    session = DirectPiSession(
        repository,
        transport,
        settings,
        RunVirtualKey(
            run_id="run-1",
            key_alias="fomo-run-1",
            duration_seconds=300,
            secret="sk-test-run-key",
        ),
        run_id="run-1",
        lease_token="lease-1",
        started_at=time.monotonic(),
        session_id="fomo-run-1",
    )

    assert (
        await session.invoke(
            SandboxRef(id="sandbox-1", project_id="project-1"),
            "the user chose Grid",
            stage="building",
            resume_request_id="request-1",
        )
        == "ok"
    )
    assert transport.requests[0].require_resume
    repository.complete_run_continuation.assert_awaited_once_with(
        "run-1",
        "request-1",
        lease_token="lease-1",
    )


@pytest.mark.asyncio
async def test_direct_pi_length_stop_fails_closed_without_an_invoke_retry(settings) -> None:
    transport = _RecordingTransport()
    transport.stop_reasons.append("length")
    session = DirectPiSession(
        _session_repository(),
        transport,
        settings,
        RunVirtualKey(
            run_id="run-1",
            key_alias="fomo-run-1",
            duration_seconds=300,
            secret="sk-test-run-key",
        ),
        run_id="run-1",
        lease_token="lease-1",
        started_at=time.monotonic(),
    )

    with pytest.raises(DirectPiSessionError, match="output limit"):
        await session.invoke(
            SandboxRef(id="sandbox-1", project_id="project-1"),
            "Implement the plan.",
            stage="repairing",
        )
    # No in-session retry exists: the failure surfaces to the control layer.
    assert len(transport.requests) == 1


def test_planning_parser_normalizes_one_unambiguous_deepseek_nesting_error() -> None:
    valid = _plan()
    malformed = {
        "buildPlan": valid["buildPlan"],
        "acceptanceContract": {"criteria": valid["acceptanceContract"]["criteria"]},
        "tests": valid["acceptanceContract"]["tests"],
    }

    bundle = DirectPiOrchestrator._parse_planning_bundle(
        json.dumps(malformed, separators=(",", ":")) + "}"
    )

    assert len(bundle.acceptance_contract.tests) == 2


def test_planning_parser_rejects_arbitrary_trailing_content() -> None:
    with pytest.raises(DirectPiOrchestrationError, match="invalid planning contract"):
        DirectPiOrchestrator._parse_planning_bundle(
            json.dumps(_plan(), separators=(",", ":")) + " trailing"
        )


async def _new_project_run(repository, requirement: str, message_id: str):
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, message_id, requirement
    )
    claimed = await repository.claim_next_run(f"direct-worker-{message_id}", 60)
    assert claimed is not None and claimed.lease_owner
    return project, run, claimed.lease_owner


def _direct_orchestrator(
    repository,
    settings,
    sandbox: GitAwareSandbox,
    gateway: _Gateway,
    transport: _Transport,
) -> DirectPiOrchestrator:
    return DirectPiOrchestrator(
        repository,
        sandbox,
        replace(
            settings,
            agent_framework="direct_pi",
            direct_pi_goal_graph_enabled=False,
        ),
        gateway,
        transport,
    )


@pytest.mark.asyncio
async def test_goal_graph_runs_two_goals_with_scoped_full_regression_and_checkpoints(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a library with search and durable create.",
        "goal-graph-two-goals",
    )
    harness = _playwright_report("starter renders a stable application shell")
    search = _playwright_report("searches books by title")
    create = _playwright_report("creates and persists a book")
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): [
                ExecResult(0, harness, ""),
                ExecResult(0, harness, ""),
            ],
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): [ExecResult(0, search, ""), ExecResult(0, search, "")],
            _playwright_command(
                "tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"
            ): ExecResult(0, create, ""),
        }
    )
    gateway = _Gateway()
    transport = _GoalGraphTransport(sandbox)
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        replace(
            settings,
            agent_framework="direct_pi",
            direct_pi_goal_graph_enabled=True,
        ),
        gateway,
        transport,
    )

    await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    projection = await repository.get_goal_graph_for_run(run.id)
    checkpoint = await repository.get_latest_verified_checkpoint(run.id)
    usage = await repository.get_usage_totals(run.id)
    assert final.status is RunStatus.succeeded
    assert projection is not None and projection.graph.status.value == "verified"
    assert [goal.status.value for goal in projection.graph.goals] == [
        "verified",
        "verified",
    ]
    assert checkpoint is not None and checkpoint.ordinal == 2
    checkpoint_paths = {item.path for item in checkpoint.files}
    assert "components/features/library-desk.tsx" in checkpoint_paths
    assert "lib/domain/books.ts" in checkpoint_paths
    assert usage.input_tokens == 300
    assert usage.output_tokens == 150
    assert usage.cost_micros == 30_000
    assert transport.calls == 3
    assert len(set(transport.session_ids)) == 1

    events = await repository.list_events(run.id)
    kinds = [event.kind for event in events]
    assert kinds.count("goal.claimed") == 2
    assert kinds.count("goal.verified") == 2
    assert kinds.index("goal.claimed") < kinds.index("goal.verified")
    assert any(
        event.kind == "preview.expired"
        and event.payload.get("reason") == "goal_advanced"
        for event in events
    )
    suites = [
        event.payload for event in events if event.kind == "verification.suite_started"
    ]
    assert [item["mode"] for item in suites] == ["focused", "full"]
    assert [item["reason"] for item in suites] == ["goal_focused", "final_goal"]
    assert [item["goalIds"] for item in suites] == [["G-1"], ["G-1", "G-2"]]
    async with repository.database.session_factory() as session:
        implementation_links = list(
            await session.scalars(
                select(TraceLinkRecord).where(
                    TraceLinkRecord.run_id == run.id,
                    TraceLinkRecord.relation == "implemented_in",
                    TraceLinkRecord.target_kind == "file",
                )
            )
        )
    assert {
        (link.source_ref, link.target_ref) for link in implementation_links
    } == {
        ("G-1:AC-1", "components/features/library-desk.tsx"),
        ("G-2:AC-2", "lib/domain/books.ts"),
    }


async def _run_until_goal_graph_question(repository, settings, suffix: str):
    owner = await repository.create_guest_session()
    project = await repository.create_project(owner.id, f"Clarification {suffix}")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        f"clarification-{suffix}",
        "Build a searchable library",
    )
    claimed = await repository.claim_next_run(f"question-worker-{suffix}", 120)
    assert claimed is not None and claimed.lease_owner
    harness = _playwright_report("starter renders a stable application shell")
    search = _playwright_report("searches books by title")
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): [
                ExecResult(0, harness, ""),
                ExecResult(0, harness, ""),
            ],
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): [ExecResult(0, search, ""), ExecResult(0, search, "")],
        }
    )
    transport = _PlanningThenQuestionTransport()
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        replace(
            settings,
            agent_framework="direct_pi",
            direct_pi_goal_graph_enabled=True,
        ),
        _Gateway(),
        transport,
    )

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    waiting = await repository.get_run(run.id)
    assert waiting.status is RunStatus.waiting_for_user
    assert waiting.pending_input_request is not None
    assert transport.session_id == f"fomo-{run.id}"
    assert transport.sandbox_id is not None
    assert await persisted_sandbox_id(repository, run.id) == transport.sandbox_id
    assert transport.sandbox_id in sandbox.sandboxes
    return owner, project, run, waiting.pending_input_request, sandbox, transport


@pytest.mark.asyncio
async def test_goal_graph_wait_retains_generation_and_answer_resumes_same_session(
    repository, settings
) -> None:
    owner, _project, run, request, sandbox, first_transport = (
        await _run_until_goal_graph_question(repository, settings, "success")
    )
    _message, _answered, queued, _created = await repository.answer_user_input(
        run.id,
        request.id,
        owner.id,
        "clarification-answer-success",
        "Grid",
    )
    assert queued.status is RunStatus.queued
    claimed = await repository.claim_next_run("answer-worker-success", 120)
    assert claimed is not None and claimed.id == run.id and claimed.lease_owner
    answer_transport = _AnswerContinuationTransport(
        sandbox,
        expected_session_id=first_transport.session_id or "",
        expected_sandbox_id=first_transport.sandbox_id or "",
    )
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        replace(
            settings,
            agent_framework="direct_pi",
            direct_pi_goal_graph_enabled=True,
        ),
        _Gateway(),
        answer_transport,
    )

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    assert answer_transport.calls == 1
    assert (await repository.get_run(run.id)).status is RunStatus.succeeded
    assert await repository.get_run_continuation(run.id) is None
    events = await repository.list_events(run.id)
    assert any(event.kind == "run.resumed" for event in events)
    assert not any(event.kind == "run.continuation_unavailable" for event in events)


@pytest.mark.asyncio
async def test_goal_graph_missing_pi_session_fails_closed_without_replaying_answer(
    repository, settings
) -> None:
    owner, _project, run, request, sandbox, first_transport = (
        await _run_until_goal_graph_question(repository, settings, "unavailable")
    )
    await repository.answer_user_input(
        run.id,
        request.id,
        owner.id,
        "clarification-answer-unavailable",
        "Grid",
    )
    claimed = await repository.claim_next_run("answer-worker-unavailable", 120)
    assert claimed is not None and claimed.id == run.id and claimed.lease_owner
    answer_transport = _AnswerContinuationTransport(
        sandbox,
        expected_session_id=first_transport.session_id or "",
        expected_sandbox_id=first_transport.sandbox_id or "",
        unavailable=True,
    )
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        replace(
            settings,
            agent_framework="direct_pi",
            direct_pi_goal_graph_enabled=True,
        ),
        _Gateway(),
        answer_transport,
    )

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    failed = await repository.get_run(run.id)
    assert failed.status is RunStatus.needs_attention
    assert failed.error_code == "pi_session_resume_unavailable"
    events = await repository.list_events(run.id)
    assert any(event.kind == "run.continuation_unavailable" for event in events)
    assert not any(event.kind == "run.resumed" for event in events)


@pytest.mark.asyncio
async def test_goal_graph_reuses_failed_build_planning_artifact_and_starts_with_build(
    repository, settings
) -> None:
    requirement = "Build a library with search and durable create."
    owner = await repository.create_guest_session()
    project = await repository.create_project(owner.id, "Library")
    _message, prior, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "goal-graph-cache-source",
        requirement,
    )
    prior_claim = await repository.claim_next_run("goal-graph-cache-source-worker", 60)
    assert prior_claim is not None and prior_claim.lease_owner
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    await repository.store_artifact(
        prior.id,
        "run_input",
        {
            "starterId": starter.id,
            "starterVersion": starter.version,
            "starterCapabilities": list(starter.capability_ids),
            "goalGraph": True,
            "planningPolicy": GOAL_GRAPH_PLANNING_POLICY,
        },
        lease_token=prior_claim.lease_owner,
    )
    draft = parse_goal_graph_draft(_goal_graph_plan())
    prior_graph = await repository.create_goal_graph(
        project.id,
        prior.id,
        draft,
        lease_token=prior_claim.lease_owner,
    )
    await repository.store_artifact(
        prior.id,
        "goal_graph",
        draft.model_dump(mode="json", by_alias=True),
        lease_token=prior_claim.lease_owner,
    )
    await repository.mark_terminal(
        prior.id,
        RunStatus.failed,
        error_code="goal_graph_execution_error",
        lease_token=prior_claim.lease_owner,
    )

    # A newer draft from the retired fine-grained policy must not shadow the
    # exact coarse-v2 cache source.
    _message, legacy, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "goal-graph-cache-legacy-source",
        requirement,
    )
    legacy_claim = await repository.claim_next_run(
        "goal-graph-cache-legacy-source-worker", 60
    )
    assert legacy_claim is not None and legacy_claim.lease_owner
    await repository.store_artifact(
        legacy.id,
        "run_input",
        {
            "starterId": starter.id,
            "starterVersion": starter.version,
            "starterCapabilities": list(starter.capability_ids),
            "goalGraph": True,
        },
        lease_token=legacy_claim.lease_owner,
    )
    await repository.store_artifact(
        legacy.id,
        "goal_graph",
        draft.model_dump(mode="json", by_alias=True),
        lease_token=legacy_claim.lease_owner,
    )
    await repository.mark_terminal(
        legacy.id,
        RunStatus.failed,
        error_code="goal_graph_execution_error",
        lease_token=legacy_claim.lease_owner,
    )

    _message, run, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "goal-graph-cache-target",
        requirement,
    )
    claimed = await repository.claim_next_run("goal-graph-cache-target-worker", 60)
    assert claimed is not None and claimed.lease_owner
    harness = _playwright_report("starter renders a stable application shell")
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): [
                ExecResult(0, harness, ""),
                ExecResult(0, harness, ""),
            ],
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): [
                ExecResult(0, _playwright_report("searches books by title"), ""),
                ExecResult(0, _playwright_report("searches books by title"), ""),
            ],
            _playwright_command(
                "tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"
            ): ExecResult(0, _playwright_report("creates and persists a book"), ""),
        }
    )

    class _BuildOnlyGoalGraphTransport(_GoalGraphTransport):
        def __init__(self, sandbox: GitAwareSandbox) -> None:
            super().__init__(sandbox)
            self.calls = 1  # Skip the helper's synthetic planning response.
            self.prompts: list[str] = []

        async def run(self, ref, invocation, **kwargs):
            self.prompts.append(invocation.request.prompt)
            return await super().run(ref, invocation, **kwargs)

    transport = _BuildOnlyGoalGraphTransport(sandbox)
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        replace(
            settings,
            agent_framework="direct_pi",
            direct_pi_goal_graph_enabled=True,
        ),
        _Gateway(),
        transport,
    )

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    final = await repository.get_run(run.id)
    current_graph = await repository.get_goal_graph_for_run(run.id)
    events = await repository.list_events(run.id, limit=500)
    cache_hit = next(event for event in events if event.kind == "planning.cache_hit")
    assert final.status is RunStatus.succeeded
    assert len(transport.prompts) == 2
    assert all("Frozen GoalExecutionPlan:" in prompt for prompt in transport.prompts)
    assert all("PLANNING TURN ONLY" not in prompt for prompt in transport.prompts)
    assert cache_hit.payload["sourceRunId"] == prior.id
    assert current_graph is not None and current_graph.graph_id != prior_graph.graph_id
    run_input = await repository.get_latest_artifact(run.id, "run_input")
    assert run_input is not None
    assert run_input["planningPolicy"] == GOAL_GRAPH_PLANNING_POLICY


@pytest.mark.asyncio
async def test_goal_graph_recovers_from_verified_checkpoint_with_durable_session_id_and_budget(
    repository, settings
) -> None:
    project, run, lease = await _new_project_run(
        repository,
        "Build a library with search and durable create.",
        "goal-graph-recovery",
    )
    draft = parse_goal_graph_draft(_goal_graph_plan())
    await repository.create_goal_graph(project.id, run.id, draft, lease_token=lease)
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    checkpoint_files = [
        {"path": item.path, "content": item.as_change().content}
        for item in starter.files
        if not item.path.startswith("tests/harness/")
        and not item.path.startswith("tests/fomo-acceptance/")
        and item.path != ".gitignore"
    ]
    await repository.record_verified_checkpoint(
        run.id,
        "G-1",
        checkpoint_files,
        [
            {
                "acceptanceKey": "G-1:AC-1",
                "kind": "playwright_smoke",
                "status": "passed",
            }
        ],
        lease_token=lease,
        commit_sha="b" * 40,
        capsule={
            "verifiedEvidence": [
                {
                    "goalId": "G-1",
                    "passedAcceptanceIds": ["AC-1"],
                    "evidenceRefs": ["checkpoint:seed-g1"],
                }
            ]
        },
    )
    await repository.record_usage_entry(
        run.id,
        "prior-request",
        lease_token=lease,
        provider="fomo-litellm",
        model="fomo-pi-build",
        input_tokens=100,
        output_tokens=50,
        cost_micros=500_000,
        goal_id="G-1",
    )

    harness = _playwright_report("starter renders a stable application shell")
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): ExecResult(0, harness, ""),
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): ExecResult(0, _playwright_report("searches books by title"), ""),
            _playwright_command(
                "tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"
            ): ExecResult(0, _playwright_report("creates and persists a book"), ""),
        }
    )
    transport = _GoalGraphTransport(sandbox)
    transport.calls = 1  # Recovery starts directly at the active G-2 build turn.
    gateway = _Gateway()
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        replace(
            settings,
            agent_framework="direct_pi",
            direct_pi_goal_graph_enabled=True,
        ),
        gateway,
        transport,
    )

    await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    latest = await repository.get_latest_verified_checkpoint(run.id)
    assert final.status is RunStatus.succeeded
    assert latest is not None and latest.ordinal == 2
    assert len(transport.session_ids) == 1
    assert transport.session_ids[0] == f"fomo-{run.id}"
    assert gateway.issued[0]["max_budget"] == pytest.approx(1.5)
    events = await repository.list_events(run.id)
    assert any(event.kind == "goal.resumed" for event in events)


@pytest.mark.asyncio
async def test_verified_graph_publish_recovery_rebuilds_and_reverifies_without_pi(
    repository, settings
) -> None:
    project, run, lease = await _new_project_run(
        repository,
        "Build a library with search and durable create.",
        "goal-graph-publish-recovery",
    )
    await repository.create_goal_graph(
        project.id,
        run.id,
        parse_goal_graph_draft(_goal_graph_plan()),
        lease_token=lease,
    )
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    files = [
        {"path": item.path, "content": item.as_change().content}
        for item in starter.files
        if not item.path.startswith("tests/harness/")
        and not item.path.startswith("tests/fomo-acceptance/")
        and item.path != ".gitignore"
    ]
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    await repository.record_verified_checkpoint(
        run.id,
        "G-1",
        files,
        [{"acceptanceKey": "G-1:AC-1", "kind": "playwright_smoke", "status": "passed"}],
        lease_token=lease,
        capsule={
            "verifiedEvidence": [
                {
                    "goalId": "G-1",
                    "passedAcceptanceIds": ["AC-1"],
                    "evidenceRefs": ["checkpoint:publish-g1"],
                }
            ]
        },
    )
    await repository.claim_goal(run.id, "G-2", lease_token=lease)
    await repository.record_verified_checkpoint(
        run.id,
        "G-2",
        files,
        [{"acceptanceKey": "G-2:AC-2", "kind": "playwright_smoke", "status": "passed"}],
        lease_token=lease,
        capsule={
            "verifiedEvidence": [
                {
                    "goalId": "G-1",
                    "passedAcceptanceIds": ["AC-1"],
                    "evidenceRefs": ["checkpoint:publish-g1"],
                },
                {
                    "goalId": "G-2",
                    "passedAcceptanceIds": ["AC-2"],
                    "evidenceRefs": ["checkpoint:publish-g2"],
                },
            ]
        },
    )

    harness = _playwright_report("starter renders a stable application shell")
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): ExecResult(0, harness, ""),
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): ExecResult(0, _playwright_report("searches books by title"), ""),
            _playwright_command(
                "tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"
            ): ExecResult(0, _playwright_report("creates and persists a book"), ""),
        }
    )

    class _NoPiTransport:
        async def run(self, *_args, **_kwargs):
            raise AssertionError("verified publish recovery must not invoke Pi")

    gateway = _Gateway()
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        replace(
            settings,
            agent_framework="direct_pi",
            direct_pi_goal_graph_enabled=True,
        ),
        gateway,
        _NoPiTransport(),
    )

    await orchestrator.run(run.id, lease_token=lease)

    assert (await repository.get_run(run.id)).status is RunStatus.succeeded
    assert (await repository.get_latest_verified_checkpoint(run.id)).ordinal == 2  # type: ignore[union-attr]
    assert gateway.issued == []
    events = await repository.list_events(run.id)
    recovery_suite = next(
        event
        for event in events
        if event.kind == "verification.suite_started"
    )
    assert recovery_suite.payload["mode"] == "full"
    assert recovery_suite.payload["reason"] == "verified_graph_recovery"


def test_goal_delta_excludes_unchanged_cumulative_files_and_detects_repeated_edits() -> None:
    before = CandidateCheckpoint(
        files=(
            {"path": "app/prior.tsx", "sha256": "a" * 64},
            {"path": "app/current.tsx", "sha256": "b" * 64},
        ),
        manifest_hash="before",
    )
    after = CandidateCheckpoint(
        files=(
            {"path": "app/prior.tsx", "sha256": "a" * 64},
            {"path": "app/current.tsx", "sha256": "c" * 64},
        ),
        manifest_hash="after",
    )
    repeated = CandidateCheckpoint(
        files=(
            {"path": "app/prior.tsx", "sha256": "d" * 64},
            {"path": "app/current.tsx", "sha256": "c" * 64},
        ),
        manifest_hash="repeated",
    )

    assert DirectPiOrchestrator._candidate_delta_paths(before, after) == (
        "app/current.tsx",
    )
    assert DirectPiOrchestrator._candidate_delta_paths(after, repeated) == (
        "app/prior.tsx",
    )


def test_legacy_checkpoint_without_goal_paths_is_fail_safe_until_rewritten() -> None:
    legacy = SimpleNamespace(capsule={"verifiedEvidence": []})
    modern = SimpleNamespace(
        capsule={"verifiedEvidence": [], "goalChangedPathsByGoal": {}}
    )

    assert DirectPiOrchestrator._checkpoint_goal_changed_paths(legacy) == ({}, True)
    assert DirectPiOrchestrator._checkpoint_goal_changed_paths(modern) == ({}, False)


@pytest.mark.asyncio
async def test_direct_pi_full_loop_publishes_frozen_manifest_evidence(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a polished library manager with search and durable create.",
        "direct-pi-run",
    )
    sandbox = GitAwareSandbox(_playwright_results())
    gateway = _Gateway()
    transport = _Transport(sandbox)
    orchestrator = _direct_orchestrator(repository, settings, sandbox, gateway, transport)

    await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    assert final.status == RunStatus.succeeded
    assert final.preview_url == "http://fake-preview.invalid:8080"
    assert transport.calls == 2
    assert gateway.blocked
    assert await repository.get_latest_artifact(run.id, "build_plan") is not None
    assert await repository.get_latest_artifact(run.id, "acceptance_contract") is not None
    trace = await repository.get_trace(_project.id, run.id)
    assert {item["status"] for item in trace["acceptance_trace"]} == {"passed"}
    assert {item["implementationStatus"] for item in trace["acceptance_trace"]} == {"implemented"}
    versions = await repository.list_versions(_project.id)
    assert len(versions) == 1 and versions[0].qa_status == "passed"
    assert versions[0].commit_sha == CANDIDATE_SHA
    _version_id, composition, _digest = await repository.get_version_file_content(
        _project.id, "app/(generated)/composition.tsx", versions[0].id
    )
    assert "LibraryDesk" in composition
    # preview.verified only after the frozen manifest + health recheck passed.
    events = await repository.list_events(run.id, limit=500)
    assert any(event.kind == "preview.verified" for event in events)
    assert not any(event.kind == "build.batch.started" for event in events)


@pytest.mark.asyncio
async def test_direct_pi_dependency_timeout_is_infrastructure_and_not_repaired(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a polished library manager with search and durable create.",
        "direct-pi-infra-failure",
    )
    sandbox = GitAwareSandbox(
        {
            "pnpm install --offline --frozen-lockfile --ignore-scripts": ExecResult(
                -1, "", "", timed_out=True
            )
        }
    )
    gateway = _Gateway()
    transport = _Transport(sandbox)
    orchestrator = _direct_orchestrator(repository, settings, sandbox, gateway, transport)

    await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    assert final.status == RunStatus.needs_attention
    assert final.error_code == "direct_pi_infrastructure_failed"
    assert final.repair_round == 0
    assert final.preview_url is None
    assert transport.calls == 2
    assert gateway.blocked


@pytest.mark.asyncio
async def test_direct_pi_reuses_exact_validated_planning_artifacts(repository, settings) -> None:
    requirement = "Build a polished library manager with search and durable create."
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, prior, _created = await repository.create_message_and_run(
        project.id, session.id, "prior-planning", requirement
    )
    prior_claim = await repository.claim_next_run("prior-worker", 60)
    assert prior_claim is not None and prior_claim.lease_owner
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    await repository.store_artifact(
        prior.id,
        "run_input",
        {
            "starterId": starter.id,
            "starterVersion": starter.version,
            "starterCapabilities": list(starter.capability_ids),
        },
        lease_token=prior_claim.lease_owner,
    )
    bundle = PlanningBundle.model_validate(_plan())
    await repository.store_artifact(
        prior.id,
        "build_plan",
        bundle.build_plan.model_dump(mode="json", by_alias=True),
        lease_token=prior_claim.lease_owner,
    )
    await repository.store_artifact(
        prior.id,
        "acceptance_contract",
        bundle.acceptance_contract.model_dump(mode="json", by_alias=True),
        lease_token=prior_claim.lease_owner,
    )
    await repository.mark_terminal(
        prior.id,
        RunStatus.failed,
        error_code="direct_pi_execution_error",
        lease_token=prior_claim.lease_owner,
    )

    # A newer cache candidate has the exact input fingerprint but an invalid
    # current contract. The orchestrator must re-validate and skip it, then
    # reuse the older valid machine artifacts.
    _message, invalid, _created = await repository.create_message_and_run(
        project.id, session.id, "invalid-cached-planning", requirement
    )
    invalid_claim = await repository.claim_next_run("invalid-cache-worker", 60)
    assert invalid_claim is not None and invalid_claim.lease_owner
    await repository.store_artifact(
        invalid.id,
        "run_input",
        {
            "starterId": starter.id,
            "starterVersion": starter.version,
            "starterCapabilities": list(starter.capability_ids),
        },
        lease_token=invalid_claim.lease_owner,
    )
    invalid_plan = bundle.build_plan.model_dump(mode="json", by_alias=True)
    invalid_plan["routes"] = ["https://invalid.example"]
    await repository.store_artifact(
        invalid.id,
        "build_plan",
        invalid_plan,
        lease_token=invalid_claim.lease_owner,
    )
    await repository.store_artifact(
        invalid.id,
        "acceptance_contract",
        bundle.acceptance_contract.model_dump(mode="json", by_alias=True),
        lease_token=invalid_claim.lease_owner,
    )
    await repository.mark_terminal(
        invalid.id,
        RunStatus.failed,
        error_code="direct_pi_execution_error",
        lease_token=invalid_claim.lease_owner,
    )

    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, "cached-planning", requirement
    )
    claimed = await repository.claim_next_run("direct-worker", 60)
    assert claimed is not None and claimed.lease_owner
    sandbox = GitAwareSandbox(_playwright_results())
    gateway = _Gateway()
    transport = _Transport(sandbox, build_only=True, extra_helper=True)
    orchestrator = _direct_orchestrator(repository, settings, sandbox, gateway, transport)

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id, limit=500)
    assert final.status == RunStatus.succeeded
    assert transport.calls == 1
    cache_hits = [event for event in events if event.kind == "planning.cache_hit"]
    assert len(cache_hits) == 1
    assert cache_hits[0].payload["sourceRunId"] == prior.id
    # The advisory plan allowed an unplanned helper: no amendment artifact is
    # produced, and the helper is a normal part of the audited diff.
    assert await repository.get_latest_artifact(run.id, "build_plan_amendment") is None
    assert any(
        event.kind == "file.changed"
        and event.payload.get("path") == "components/features/native-select.tsx"
        for event in events
    )


@pytest.mark.asyncio
async def test_direct_pi_repair_recreates_a_clean_verification_sandbox(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a polished library manager with search and durable create.",
        "repair-loop",
    )
    results = _playwright_results()
    # First smoke gate fails once, then passes on the re-verified round.
    results[_playwright_command(_HARNESS_PATH)] = [
        ExecResult(1, "", "smoke failed"),
        ExecResult(0, _playwright_report("starter renders a stable application shell"), ""),
    ]
    sandbox = GitAwareSandbox(results)
    gateway = _Gateway()
    transport = _Transport(sandbox)
    orchestrator = _direct_orchestrator(repository, settings, sandbox, gateway, transport)

    await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id, limit=500)
    assert final.status == RunStatus.succeeded
    assert final.repair_round == 1
    # The repair turn reuses the same session (planning + build + repair).
    assert transport.calls == 3
    assert any(event.kind == "preview.expired" for event in events)
    assert any(event.kind == "preview.verified" for event in events)
    sandbox_ids = list(sandbox.sandboxes)
    assert len(sandbox_ids) == 3  # generation, failed V, clean replacement V
    durable_sandbox_id = await persisted_sandbox_id(repository, run.id)
    assert durable_sandbox_id == sandbox_ids[-1]
    assert durable_sandbox_id != sandbox_ids[-2]


@pytest.mark.asyncio
async def test_direct_pi_planning_failure_destroys_generation_and_blocks_key(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a polished library manager with search and durable create.",
        "planning-failure",
    )
    sandbox = GitAwareSandbox()
    gateway = _Gateway()
    transport = _Transport(sandbox, fail_planning=RuntimeError("provider exploded"))
    orchestrator = _direct_orchestrator(repository, settings, sandbox, gateway, transport)

    with pytest.raises(RuntimeError, match="provider exploded"):
        await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    assert final.status == RunStatus.failed
    assert final.error_code == "direct_pi_execution_error"
    assert gateway.blocked
    # The generation sandbox was destroyed and its durable reference cleared
    # by the orchestrator's cleanup.
    assert await persisted_sandbox_id(repository, run.id) is None


class _RetainingGitAwareSandbox(GitAwareSandbox):
    def __init__(self, *, renewal_error: Exception | None = None) -> None:
        super().__init__()
        self.renewal_error = renewal_error
        self.renewal_calls: list[tuple[SandboxRef, int]] = []

    async def renew_preview(self, ref: SandboxRef, lifetime_seconds: int) -> str:
        self.renewal_calls.append((ref, lifetime_seconds))
        if self.renewal_error is not None:
            raise self.renewal_error
        return "2026-08-17T01:02:03+00:00"


@pytest.mark.asyncio
async def test_publish_uses_frozen_snapshot_and_emits_preview_verified_after(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a polished library manager.",
        "publish-frozen",
    )
    sandbox = _RetainingGitAwareSandbox()
    settings = replace(
        settings,
        agent_framework="direct_pi",
        sandbox_provider="opensandbox",
        verified_preview_lifetime_seconds=123_456,
        public_preview_base_domain="preview.example.test",
    )
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    commands = _commands(repository, sandbox, settings, run.id, lease)
    workspaces = _workspaces(repository, sandbox, settings, commands, starter, run.id, lease)
    ref = await sandbox.create(_project.id)
    await workspaces._seed(ref, base_version_id=None)
    initial_files = await workspaces._list_files(ref)
    snapshot = VerificationSnapshot(
        ref=ref,
        commit_sha=CANDIDATE_SHA,
        initial_files=tuple(initial_files),
        initial_hashes={str(item["path"]): str(item["sha256"]) for item in initial_files},
    )
    verifier = SimpleNamespace(preview_is_healthy=AsyncMock(return_value=True))
    outcome = VerificationOutcome(
        passed=True,
        gates=(),
        diagnostic_artifact_id="diagnostic-1",
        preview_url="http://fake-preview.invalid:8080",
        preview_elapsed_seconds=1.5,
    )
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        _Gateway(),
        _Transport(sandbox),
    )

    await orchestrator._publish(
        run.id,
        _project.id,
        lease,
        workspaces,
        verifier,  # type: ignore[arg-type]
        snapshot,
        outcome,
        PlanningBundle.model_validate(_plan()),
    )

    versions = await repository.list_versions(_project.id)
    assert len(versions) == 1
    assert versions[0].commit_sha == CANDIDATE_SHA
    published_files = await repository.list_version_files(
        _project.id, versions[0].id
    )
    assert {
        str(item["path"]): str(item["sha256"]) for item in published_files
    } == snapshot.initial_hashes
    events = await repository.list_events(run.id, limit=500)
    verified = [event for event in events if event.kind == "preview.verified"]
    retained = [event for event in events if event.kind == "preview.retention_extended"]
    assert len(verified) == 1
    assert len(retained) == 1
    assert retained[0].seq < verified[0].seq
    assert retained[0].payload == {
        "sandboxId": ref.id,
        "expiresAt": "2026-08-17T01:02:03+00:00",
        "lifetimeSeconds": 123_456,
    }
    assert sandbox.renewal_calls == [(ref, 123_456)]
    assert verified[0].payload["elapsedSeconds"] == 1.5
    assert verified[0].payload["verificationStatus"] == "verified"
    assert verified[0].payload["url"] == f"https://{ref.id}.preview.example.test/"
    final = await repository.get_run(run.id)
    assert final.preview_url == f"https://{ref.id}.preview.example.test/"
    # Internal readiness is checked before publication; the public gateway is
    # authorized only by the atomic succeeded-run write below.
    verifier.preview_is_healthy.assert_awaited_once_with(outcome.preview_url)
    assert (
        f"git tag version/1 {CANDIDATE_SHA}"
        in sandbox.sandboxes[ref.id].commands
    )


@pytest.mark.asyncio
async def test_publish_fails_closed_when_verified_preview_cannot_be_renewed(
    repository, settings
) -> None:
    project, run, lease = await _new_project_run(
        repository,
        "Build a polished library manager.",
        "publish-renewal-failure",
    )
    sandbox = _RetainingGitAwareSandbox(
        renewal_error=RuntimeError("provider metadata must stay private")
    )
    settings = replace(settings, sandbox_provider="opensandbox")
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    commands = _commands(repository, sandbox, settings, run.id, lease)
    workspaces = _workspaces(repository, sandbox, settings, commands, starter, run.id, lease)
    ref = await sandbox.create(project.id)
    await workspaces._seed(ref, base_version_id=None)
    initial_files = await workspaces._list_files(ref)
    snapshot = VerificationSnapshot(
        ref=ref,
        commit_sha=CANDIDATE_SHA,
        initial_files=tuple(initial_files),
        initial_hashes={
            str(item["path"]): str(item["sha256"]) for item in initial_files
        },
    )
    verifier = SimpleNamespace(preview_is_healthy=AsyncMock(return_value=True))
    outcome = VerificationOutcome(
        passed=True,
        gates=(),
        diagnostic_artifact_id="diagnostic-1",
        preview_url="http://fake-preview.invalid:8080",
        preview_elapsed_seconds=1.5,
    )
    orchestrator = _direct_orchestrator(
        repository, settings, sandbox, _Gateway(), _Transport(sandbox)
    )

    with pytest.raises(
        DirectPiOrchestrationError,
        match="unable to retain the verified preview",
    ):
        await orchestrator._publish(
            run.id,
            project.id,
            lease,
            workspaces,
            verifier,  # type: ignore[arg-type]
            snapshot,
            outcome,
            PlanningBundle.model_validate(_plan()),
        )

    assert await repository.list_versions(project.id) == []
    events = await repository.list_events(run.id, limit=500)
    assert not any(event.kind == "preview.verified" for event in events)
    assert not any(event.kind == "preview.retention_extended" for event in events)
    assert not any(
        command.startswith("git tag ") for command in sandbox.sandboxes[ref.id].commands
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "preview_url", "healthy", "error_type", "error_match"),
    [
        (
            "drift",
            "http://fake-preview.invalid:8080",
            True,
            WorkspaceContractError,
            "drifted from the frozen",
        ),
        (
            "missing-url",
            None,
            True,
            DirectPiOrchestrationError,
            "no preview URL",
        ),
        (
            "unhealthy-preview",
            "http://fake-preview.invalid:8080",
            False,
            DirectPiOrchestrationError,
            "health recheck failed",
        ),
    ],
)
async def test_publish_fails_closed_on_drift_missing_url_or_dead_preview(
    repository, settings, failure, preview_url, healthy, error_type, error_match
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a polished library manager.",
        f"publish-fail-closed-{failure}",
    )
    sandbox = GitAwareSandbox()
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    commands = _commands(repository, sandbox, settings, run.id, lease)
    workspaces = _workspaces(repository, sandbox, settings, commands, starter, run.id, lease)
    ref = await sandbox.create(_project.id)
    await workspaces._seed(ref, base_version_id=None)
    initial_files = await workspaces._list_files(ref)
    snapshot = VerificationSnapshot(
        ref=ref,
        commit_sha=CANDIDATE_SHA,
        initial_files=tuple(initial_files),
        initial_hashes={
            str(item["path"]): str(item["sha256"]) for item in initial_files
        },
    )
    if failure == "drift":
        await sandbox.apply_changes(ref, [FileChange(path="drift.txt", content="drifted")])
    verifier = SimpleNamespace(preview_is_healthy=AsyncMock(return_value=healthy))
    outcome = VerificationOutcome(
        passed=True,
        gates=(),
        diagnostic_artifact_id="diagnostic-1",
        preview_url=preview_url,
        preview_elapsed_seconds=None,
    )
    orchestrator = _direct_orchestrator(repository, settings, sandbox, _Gateway(), _Transport(sandbox))

    with pytest.raises(error_type, match=error_match):
        await orchestrator._publish(
            run.id,
            _project.id,
            lease,
            workspaces,
            verifier,  # type: ignore[arg-type]
            snapshot,
            outcome,
            PlanningBundle.model_validate(_plan()),
        )

    assert await repository.list_versions(_project.id) == []
    events = await repository.list_events(run.id, limit=500)
    assert not any(event.kind == "preview.verified" for event in events)
    assert not any(command.startswith("git tag ") for command in sandbox.sandboxes[ref.id].commands)
    if failure == "unhealthy-preview":
        verifier.preview_is_healthy.assert_awaited_once_with(preview_url)
    else:
        verifier.preview_is_healthy.assert_not_awaited()


def _commands(repository, sandbox, settings, run_id: str, lease_token: str):
    from fomo.direct_pi.execution import CommandExecutor

    return CommandExecutor(
        repository,
        sandbox,
        settings,
        run_id=run_id,
        lease_token=lease_token,
    )


def _workspaces(repository, sandbox, settings, commands, starter, run_id: str, lease_token: str):
    return WorkspaceManager(
        repository,
        sandbox,
        settings,
        commands,
        starter,
        run_id=run_id,
        project_id="project-placeholder",
        lease_token=lease_token,
    )
