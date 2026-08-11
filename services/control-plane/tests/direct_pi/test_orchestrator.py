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
from fomo.direct_pi.architecture_profile import (
    ARCHITECTURE_PROFILE_ID,
    ARCHITECTURE_PROFILE_VERSION,
    derive_product_architecture_profile,
)
from fomo.direct_pi.failures import AgentNoEffect, PlanningContractError
from fomo.direct_pi.goalgraph import (
    GoalStatus,
    derive_navigation_verification_suite,
    navigation_evidence_key,
    navigation_test_ids,
    parse_goal_graph_draft,
)
from fomo.direct_pi.orchestrator import DirectPiOrchestrationError
from fomo.direct_pi.prompts import (
    GOAL_GRAPH_PLANNING_POLICY,
    PRODUCT_DESIGN_POLICY,
    goal_graph_planning_correction_prompt,
    goal_graph_planning_prompt,
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
    FOMO_PI_MODEL,
    InferenceGatewayError,
    PiBridgeEnvelope,
    PiBridgeFailed,
    PiBridgeResult,
    PiTransportResult,
    RunVirtualKey,
)
from fomo.persistence.models import RunRecord, TraceLinkRecord
from fomo.runtime_contract import resolve_runtime_contract
from fomo.sandbox.base import ExecResult, FileChange, SandboxRef
from fomo.schemas import GateResult, GateStatus, RunStatus
from fomo.starter import resolve_starter_manifest
from tests.helpers import create_user_session

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


def _playwright_failure_report(title: str) -> str:
    report = json.loads(_playwright_report(title))
    test = report["suites"][0]["specs"][0]["tests"][0]
    test["status"] = "unexpected"
    test["results"] = [
        {
            "status": "failed",
            "error": {"message": "Expected the workflow outcome to be visible."},
        }
    ]
    return json.dumps(report)


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
            model_aliases=tuple(values.get("model_aliases") or ("fomo-pi-flash",)),
        )

    async def block(self, _key: RunVirtualKey) -> None:
        self.blocked = True


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
    first["tests"][0]["actions"] = [
        {"kind": "goto", "path": "/"},
        {"kind": "reload"},
    ]
    first["tests"][0]["assertions"] = [
        {"kind": "url", "path": "/"},
        {
            "kind": "visible",
            "target": {
                "by": "role",
                "value": "heading",
                "name": "Library desk",
            },
        },
    ]
    return {
        "schemaVersion": 3,
        "productOutcome": "Users can search and maintain a durable library.",
        "routes": [
            {
                "path": "/",
                "title": "Library desk",
                "owningGoalId": "G-1",
                "deepLinkable": True,
            }
        ],
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


def _two_route_goal_graph_plan() -> dict[str, object]:
    def criterion(identifier: str, title: str) -> dict[str, object]:
        return {
            "id": identifier,
            "title": title,
            "priority": "must",
            "given": "The application is available",
            "when": "The user follows the declared route workflow",
            "then": "The exact destination is visible",
        }

    def identity(path: str, title: str) -> list[dict[str, object]]:
        return [
            {"kind": "url", "path": path},
            {
                "kind": "visible",
                "target": {
                    "by": "role",
                    "value": "heading",
                    "name": title,
                },
            },
        ]

    return {
        "schemaVersion": 3,
        "productOutcome": "Users navigate a two-route local library.",
        "routes": [
            {
                "path": "/",
                "title": "Library desk",
                "owningGoalId": "G-1",
                "deepLinkable": True,
            },
            {
                "path": "/books",
                "title": "Books",
                "owningGoalId": "G-1",
                "deepLinkable": True,
            },
        ],
        "goals": [
            {
                "goalId": "G-1",
                "title": "Navigate the library",
                "productOutcome": "Users can load and navigate both library routes.",
                "userVisible": True,
                "dependsOn": [],
                "acceptance": {
                    "criteria": [
                        criterion("AC-root", "Load the library root"),
                        criterion("AC-books", "Load the books route"),
                        criterion("AC-link", "Navigate with the Books link"),
                    ],
                    "tests": [
                        {
                            "id": "root",
                            "acceptanceId": "AC-root",
                            "title": "loads the library root",
                            "actions": [
                                {"kind": "goto", "path": "/"},
                                {"kind": "reload"},
                            ],
                            "assertions": identity("/", "Library desk"),
                        },
                        {
                            "id": "books-direct",
                            "acceptanceId": "AC-books",
                            "title": "loads the books route",
                            "actions": [
                                {"kind": "goto", "path": "/books"},
                                {"kind": "reload"},
                            ],
                            "assertions": identity("/books", "Books"),
                        },
                        {
                            "id": "books-link",
                            "acceptanceId": "AC-link",
                            "title": "navigates with the Books link",
                            "actions": [
                                {"kind": "goto", "path": "/"},
                                {
                                    "kind": "click",
                                    "target": {
                                        "by": "role",
                                        "value": "link",
                                        "name": "Books",
                                    },
                                },
                            ],
                            "assertions": identity("/books", "Books"),
                        },
                    ],
                },
            }
        ],
    }


async def _navigation_checkpoint_fixture(
    repository,
    run_id: str,
    goal_id: str,
    *,
    mode: str = "focused",
):
    projection = await repository.get_goal_graph_for_run(run_id)
    assert projection is not None
    selected = (
        (goal_id,)
        if mode == "focused"
        else tuple(
            goal.goal_id
            for goal in projection.graph.goals
            if goal.status is GoalStatus.VERIFIED or goal.goal_id == goal_id
        )
    )
    suite = derive_navigation_verification_suite(
        projection.graph,
        goal_ids=selected,
        mode=mode,
    )
    evidence = (
        [
            {
                "acceptanceKey": navigation_evidence_key(suite.version, test_id),
                "kind": f"fomo_navigation_v{suite.version}",
                "status": "passed",
            }
            for test_id in navigation_test_ids(suite)
        ]
        if suite is not None
        else []
    )
    return suite, evidence


def test_goal_graph_planning_prompts_use_complexity_driven_product_scope() -> None:
    prompt = goal_graph_planning_prompt(
        requirement="Build one responsive landing page.",
        starter={"routes": ["/"]},
    )
    correction = goal_graph_planning_correction_prompt(validation_error="invalid graph")

    assert GOAL_GRAPH_PLANNING_POLICY == "frontend-ui-v7"
    assert "derive the number and granularity" in prompt
    assert "actual requirement complexity" in prompt
    assert "Use enough goals to express the complete product" in prompt
    assert "artificial consolidation or fragmentation" in prompt
    assert "verification floor" in prompt
    assert "define 1-3 coarse-grained" not in prompt
    assert "single-route, frontend-only page" not in prompt
    assert "prefer exactly one goal" not in prompt
    assert "at most 12 criteria total" not in prompt
    assert GOAL_GRAPH_PLANNING_POLICY in correction
    assert "FOMO frontend-only runtime contract" in correction
    assert "Do not create backend services, API/route handlers" in correction
    assert "requirement complexity and coherent user outcomes" in correction
    assert "without shrinking the source request" in correction


def test_structured_planning_prompts_allow_schema_refills_until_success() -> None:
    prompts = (
        goal_graph_planning_prompt(requirement="Build a page.", starter={}),
        goal_graph_planning_correction_prompt(validation_error="invalid graph"),
    )

    for prompt in prompts:
        assert "succeeds exactly once" in prompt
        assert "resubmit until it succeeds" in prompt
        assert "FOMO frontend-only runtime contract" in prompt
        assert "at most 3 total attempts" not in prompt
        assert "Stop immediately after the successful submission" in prompt
        assert "emit prose or JSON as assistant text" in prompt


class _GoalGraphTransport:
    def __init__(
        self,
        sandbox: GitAwareSandbox,
        *,
        workspace_audit_repair: bool = False,
        plan: dict[str, object] | None = None,
        noop_calls: set[int] | None = None,
        protected_only_calls: set[int] | None = None,
        question_calls: set[int] | None = None,
        structured_question_calls: set[int] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.calls = 0
        self.session_ids: list[str] = []
        self.require_resumes: list[bool] = []
        self.workspace_audit_repair = workspace_audit_repair
        self.plan = plan or _goal_graph_plan()
        self.noop_calls = noop_calls or set()
        self.protected_only_calls = protected_only_calls or set()
        self.question_calls = question_calls or set()
        self.structured_question_calls = structured_question_calls or set()

    async def run(self, ref, invocation, *, on_event=None, **_kwargs):
        self.calls += 1
        self.session_ids.append(invocation.request.session_id)
        self.require_resumes.append(invocation.request.require_resume)
        structured = invocation.request.structured_output_schema is not None
        input_request: dict[str, object] | None = None
        if structured and self.calls in self.structured_question_calls:
            assert invocation.request.user_input_enabled
            input_request = {
                "requestId": f"input-planning-{self.calls}",
                "question": "Should the library use a focused route?",
                "choices": ["Yes", "No"],
                "allowFreeform": False,
            }
            text = "Waiting for the route decision before submitting GoalGraph."
        elif structured:
            envelope = {
                "envelopeVersion": 1,
                "payloadJson": json.dumps(self.plan, separators=(",", ":")),
            }
            text = json.dumps(envelope, separators=(",", ":"))
        elif self.calls in self.question_calls:
            assert invocation.request.user_input_enabled
            input_request = {
                "requestId": f"input-layout-{self.calls}",
                "question": "Which layout should the repair preserve?",
                "choices": ["Grid", "List"],
                "allowFreeform": False,
            }
            text = "Waiting for the layout decision before repairing."
        elif self.calls in self.protected_only_calls:
            await self.sandbox.apply_changes(
                ref,
                [
                    FileChange(
                        path="tests/fomo-acceptance/G-1/search-books.smoke.spec.ts",
                        content="test('bypass', () => {});\n",
                        operation="modify",
                    )
                ],
            )
            text = "Changed only the protected acceptance mirror."
        elif self.calls in self.noop_calls:
            text = "The active goal is already integrated; ready for independent verification."
        elif self.calls == 2:
            await self.sandbox.apply_changes(
                ref,
                [
                    FileChange(
                        path="components/features/library-desk.tsx",
                        content=(
                            '"use client";\nexport function LibraryDesk() { '
                            'return <main><h1>Library desk</h1><label>Search books<input aria-label="Search books" />'
                            "</label><span>Dune</span></main>; }\n"
                        ),
                    )
                ],
            )
            if self.workspace_audit_repair:
                self.sandbox.sandboxes[ref.id].files["lib/domain/broken.ts"] = b"bad\x00source"
            text = "Claimed G-1 implementation."
        elif self.workspace_audit_repair and self.calls == 3:
            self.sandbox.sandboxes[ref.id].files.pop("lib/domain/broken.ts", None)
            text = "Removed the invalid source file."
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
        if structured and input_request is None:
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
                        "args": envelope,
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
        elif input_request is not None:
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
                        "kind": "input_request",
                        "inputRequest": input_request,
                    },
                ),
                PiBridgeEnvelope(
                    seq=3,
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
        completed: dict[str, object] = {"stats": final}
        if input_request is not None:
            completed["inputRequest"] = input_request
        return PiTransportResult(
            bridge=PiBridgeResult(
                started={"initialStats": initial},
                events=events,
                completed=completed,
            ),
            execution_id=f"goal-turn-{self.calls}",
            exit_code=0,
            stderr="",
        )


class _GoalGraphPlanningSequenceTransport(_GoalGraphTransport):
    def __init__(self, sandbox: GitAwareSandbox, plans: list[dict[str, object]]) -> None:
        if not plans:
            raise ValueError("planning sequence must not be empty")
        super().__init__(sandbox, plan=plans[0])
        self.plans = list(plans)
        self.structured_calls = 0

    async def run(self, ref, invocation, *, on_event=None, **kwargs):
        if invocation.request.structured_output_schema is not None:
            index = min(self.structured_calls, len(self.plans) - 1)
            self.plan = self.plans[index]
            self.structured_calls += 1
        return await super().run(ref, invocation, on_event=on_event, **kwargs)


class _DriftingGoalSandbox(GitAwareSandbox):
    """Mutate retained G immediately after the first verified V is retired."""

    def __init__(self, command_results=None) -> None:
        super().__init__(command_results)
        self.generation_id: str | None = None
        self.drifted = False

    async def create(self, project_id, source=None):
        ref = await super().create(project_id, source)
        if self.generation_id is None:
            self.generation_id = ref.id
        return ref

    async def kill(self, ref) -> None:
        await super().kill(ref)
        if not self.drifted and self.generation_id is not None and ref.id != self.generation_id:
            self.sandboxes[self.generation_id].files["components/features/library-desk.tsx"] = (
                b"export function LibraryDesk() { return <main>drifted</main>; }\n"
            )
            self.drifted = True


def _one_goal_graph_plan() -> dict[str, object]:
    plan = json.loads(json.dumps(_goal_graph_plan()))
    first_goal = plan["goals"][0]
    first_goal["acceptance"]["criteria"] = first_goal["acceptance"]["criteria"][:1]
    first_goal["acceptance"]["tests"] = first_goal["acceptance"]["tests"][:1]
    plan["goals"] = [first_goal]
    return plan


def _three_goal_graph_plan() -> dict[str, object]:
    plan = json.loads(json.dumps(_goal_graph_plan()))
    plan["productOutcome"] = "Users can search, maintain, and reset a durable library."
    third = json.loads(json.dumps(plan["goals"][1]))
    third.update(
        goalId="G-3",
        title="Reset the library",
        productOutcome="Users can reset the library to an empty state.",
        dependsOn=["G-2"],
    )
    criterion = third["acceptance"]["criteria"][0]
    criterion.update(
        id="AC-3",
        title="Reset all books",
        given="The library contains books",
        when="The user resets the library",
        then="The empty library state is visible",
    )
    test = third["acceptance"]["tests"][0]
    test.update(
        id="reset-library",
        acceptanceId="AC-3",
        title="resets the library",
        actions=[
            {"kind": "goto", "path": "/"},
            {
                "kind": "click",
                "target": {"by": "role", "value": "button", "name": "Reset library"},
            },
        ],
        assertions=[{"kind": "visible", "target": {"by": "text", "value": "No books"}}],
    )
    plan["goals"].append(third)
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
    def __init__(
        self,
        sandbox: GitAwareSandbox | None = None,
        *,
        change_before_question: bool = False,
    ) -> None:
        self.sandbox = sandbox
        self.calls = 0
        self.session_id: str | None = None
        self.sandbox_id: str | None = None
        self.change_before_question = change_before_question

    async def run(self, ref, invocation, *, on_event=None, **_kwargs):
        self.calls += 1
        self.session_id = invocation.request.session_id
        self.sandbox_id = ref.id
        initial = _cumulative_stats(self.calls - 1)
        final = _cumulative_stats(self.calls)
        completed: dict[str, object] = {"stats": final}
        if self.calls == 1:
            plan = _one_goal_graph_plan()
            envelope = {
                "envelopeVersion": 1,
                "payloadJson": json.dumps(plan, separators=(",", ":")),
            }
            events = (
                PiBridgeEnvelope(
                    seq=1,
                    type="pi.event",
                    payload={
                        "kind": "tool_start",
                        "toolCallId": "structured-plan",
                        "toolName": "submit_structured_output",
                        "args": envelope,
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
            if self.change_before_question:
                assert self.sandbox is not None
                await self.sandbox.apply_changes(
                    ref,
                    [
                        FileChange(
                            path="components/features/library-desk.tsx",
                            content=(
                                '"use client";\nexport function LibraryDesk() { '
                                "return <main><h1>Library desk</h1><span>Dune</span></main>; }\n"
                            ),
                        )
                    ],
                )
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
        apply_change: bool = True,
        change_path: str = "components/features/library-desk.tsx",
        change_content: str | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.expected_session_id = expected_session_id
        self.expected_sandbox_id = expected_sandbox_id
        self.unavailable = unavailable
        self.apply_change = apply_change
        self.change_path = change_path
        self.change_content = change_content
        self.calls = 0

    async def run(self, ref, invocation, *, on_event=None, **_kwargs):
        self.calls += 1
        assert invocation.request.require_resume
        assert invocation.request.session_id == self.expected_session_id
        assert ref.id == self.expected_sandbox_id
        assert "Grid" in invocation.request.prompt
        assert "next-app-feature-first@1.0.0" in invocation.request.prompt
        assert "exact frozen architecture profile" in invocation.request.prompt
        if self.unavailable:
            raise PiBridgeFailed(
                {
                    "code": "session_resume_unavailable",
                    "message": "session cache is missing",
                    "phase": "boot",
                }
            )
        if self.apply_change:
            await self.sandbox.apply_changes(
                ref,
                [
                    FileChange(
                        path=self.change_path,
                        content=self.change_content
                        or (
                            '"use client";\nexport function LibraryDesk() { '
                            'return <main><h1>Library desk</h1><label>Search books<input aria-label="Search books" />'
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
        _structured_start(3, "structured-2", {"answer": 43}),
        _structured_end(4, "structured-2", True),
        _structured_start(5, "structured-3", {"answer": 44}),
        _structured_end(6, "structured-3", True),
        _structured_start(7, "structured-4", {"answer": "valid"}),
        _structured_end(8, "structured-4", False),
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
            "did not complete",
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
    assert planning.model == FOMO_PI_MODEL
    assert planning.thinking == "high"
    assert planning.activity_silence_seconds == settings.model_request_timeout_seconds
    assert planning.timeout_seconds is None
    assert building.model == FOMO_PI_MODEL
    assert building.thinking == "high"
    assert building.activity_silence_seconds == settings.model_request_timeout_seconds
    assert building.timeout_seconds is None
    assert repairing.model == FOMO_PI_MODEL
    assert repairing.thinking == "high"
    assert repairing.activity_silence_seconds == settings.model_request_timeout_seconds
    assert repairing.timeout_seconds is None
    assert all(
        request.context_window == resolve_runtime_contract().context_window
        for request in transport.requests
    )


@pytest.mark.asyncio
async def test_selected_runtime_is_frozen_across_every_direct_pi_stage(settings) -> None:
    transport = _RecordingTransport()
    runtime = resolve_runtime_contract("gpt-5.6", "xhigh")
    session = DirectPiSession(
        _session_repository(),
        transport,
        settings,
        RunVirtualKey(
            run_id="run-selected",
            key_alias="fomo-run-selected",
            duration_seconds=300,
            secret="sk-test-selected-key",
            model_aliases=(runtime.litellm_alias,),
        ),
        runtime_contract=runtime,
        run_id="run-selected",
        lease_token="lease-selected",
        started_at=time.monotonic(),
    )
    ref = SandboxRef(id="sandbox-selected", project_id="project-selected")

    for stage in ("planning", "building", "repairing"):
        assert await session.invoke(ref, stage, stage=stage) == "ok"

    assert len(transport.requests) == 3
    assert all(request.model == runtime.model_ref for request in transport.requests)
    assert all(request.thinking == "xhigh" for request in transport.requests)
    assert all(request.context_window == 250_000 for request in transport.requests)


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


def test_goal_graph_provider_parser_requires_envelope_but_cache_can_read_raw_v3() -> None:
    plan = _one_goal_graph_plan()
    raw = json.dumps(plan, separators=(",", ":"))
    requirement = "Build one focused library workflow."

    with pytest.raises(PlanningContractError, match="invalid planning envelope"):
        DirectPiOrchestrator._parse_goal_graph_draft(
            raw,
            requirement=requirement,
        )

    malformed_cases = (
        ("{not-json", "planning_envelope_json_invalid"),
        (
            json.dumps({"envelopeVersion": 1, "payloadJson": "{not-json"}),
            "planning_payload_json_invalid",
        ),
        (
            json.dumps({"envelopeVersion": 1, "payloadJson": {}}),
            "planning_envelope_invalid",
        ),
    )
    for malformed, expected_code in malformed_cases:
        with pytest.raises(PlanningContractError) as captured:
            DirectPiOrchestrator._parse_goal_graph_draft(
                malformed,
                requirement=requirement,
            )
        assert captured.value.violation_code == expected_code

    cached = DirectPiOrchestrator._parse_goal_graph_draft(
        raw,
        requirement=requirement,
        allow_raw_domain=True,
    )
    provider = DirectPiOrchestrator._parse_goal_graph_draft(
        json.dumps(
            {"envelopeVersion": 1, "payloadJson": raw},
            separators=(",", ":"),
        ),
        requirement=requirement,
    )
    assert cached == provider


def test_navigation_report_infrastructure_diagnostic_names_the_server_owned_check() -> None:
    outcome = VerificationOutcome(
        passed=False,
        gates=(
            GateResult(
                gate="navigation:history-roundtrip",
                scope="navigation",
                status=GateStatus.failed,
                outcome="infrastructure_failed",
                summary="Playwright report was missing.",
                navigationId="history-roundtrip",
                testPath=(
                    "tests/fomo-acceptance/navigation-v1/"
                    "history-roundtrip.smoke.spec.ts"
                ),
                testName=(
                    "FOMO navigation: browser back and forward preserve every "
                    "route identity"
                ),
            ),
        ),
        diagnostic_artifact_id="diagnostic-artifact",
        preview_url=None,
    )

    diagnostic = DirectPiOrchestrator._verification_infrastructure_diagnostic(outcome)

    assert diagnostic.reason_code == "navigation_playwright_report_untrusted"
    assert diagnostic.check == "navigation_playwright_report"
    assert "history-roundtrip" in diagnostic.frames[0]
    assert "browser back and forward" in diagnostic.frames[0]


async def _new_project_run(repository, requirement: str, message_id: str):
    session = await create_user_session(repository)
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, message_id, requirement
    )
    claimed = await repository.claim_next_run(f"direct-worker-{message_id}", 60)
    assert claimed is not None and claimed.lease_owner
    return project, run, claimed.lease_owner


def _goal_graph_orchestrator(
    repository,
    settings,
    sandbox: GitAwareSandbox,
    transport: _GoalGraphTransport,
) -> DirectPiOrchestrator:
    return DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        _Gateway(),
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
        settings,
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


@pytest.mark.asyncio
async def test_goal_graph_repeated_invalid_planning_stops_after_one_correction(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build exact routes: / and /books.",
        "goal-graph-repeated-invalid-plan",
    )
    sandbox = GitAwareSandbox()
    invalid = _goal_graph_plan()
    invalid["productOutcome"] = "PRIVATE-PLANNING-DRAFT-MARKER"
    transport = _GoalGraphPlanningSequenceTransport(sandbox, [invalid, invalid])

    with pytest.raises(
        PlanningContractError,
        match="repeated the same invalid semantic submission",
    ):
        await _goal_graph_orchestrator(
            repository,
            settings,
            sandbox,
            transport,
        ).run(run.id, lease_token=lease)

    events = await repository.list_events(run.id)
    assert await repository.get_goal_graph_for_run(run.id) is None
    assert transport.structured_calls == 2
    retries = [event for event in events if event.kind == "pi.retrying"]
    assert len(retries) == 1
    assert retries[0].payload["reason"] == "goal_graph_contract_validation"
    retry_diagnostic = retries[0].payload["diagnostic"]
    assert retry_diagnostic["violationCode"] == "route_intent_invalid"
    assert retry_diagnostic["routeIds"] == ["/"]
    assert retry_diagnostic["goalIds"] == ["G-1", "G-2"]
    assert retry_diagnostic["testIds"]
    failed = next(event for event in reversed(events) if event.kind == "pi.failed")
    failure_diagnostic = failed.payload["planningDiagnostic"]
    assert failure_diagnostic["violationCode"] == "planning_no_progress"
    assert failure_diagnostic["fingerprint"] == retry_diagnostic["fingerprint"]
    assert failure_diagnostic["repeatedFrom"] == retry_diagnostic["fingerprint"]
    serialized_events = json.dumps(
        [event.payload for event in events],
        ensure_ascii=False,
    )
    assert "PRIVATE-PLANNING-DRAFT-MARKER" not in serialized_events


@pytest.mark.asyncio
async def test_goal_graph_changed_planning_correction_can_converge(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build exact routes: / and /books.",
        "goal-graph-changed-plan",
    )
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): ExecResult(
                0,
                _playwright_report("starter renders a stable application shell"),
                "",
            ),
            _playwright_command(
                "tests/fomo-acceptance/G-1/root.smoke.spec.ts"
            ): ExecResult(0, _playwright_report("loads the library root"), ""),
            _playwright_command(
                "tests/fomo-acceptance/G-1/books-direct.smoke.spec.ts"
            ): ExecResult(0, _playwright_report("loads the books route"), ""),
            _playwright_command(
                "tests/fomo-acceptance/G-1/books-link.smoke.spec.ts"
            ): ExecResult(0, _playwright_report("navigates with the Books link"), ""),
        }
    )
    transport = _GoalGraphPlanningSequenceTransport(
        sandbox,
        [_goal_graph_plan(), _two_route_goal_graph_plan()],
    )

    await _goal_graph_orchestrator(
        repository,
        settings,
        sandbox,
        transport,
    ).run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    projection = await repository.get_goal_graph_for_run(run.id)
    assert final.status is RunStatus.succeeded
    assert projection is not None
    assert [route.path for route in projection.graph.routes] == ["/", "/books"]
    assert transport.structured_calls == 2


@pytest.mark.asyncio
async def test_answered_planning_continuation_uses_same_semantic_correction_loop(
    repository,
    settings,
) -> None:
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, "Planning continuation")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "planning-continuation-correction",
        "Build one focused searchable library workflow.",
    )
    claimed = await repository.claim_next_run("planning-question-worker", 120)
    assert claimed is not None and claimed.lease_owner
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): ExecResult(
                0,
                _playwright_report("starter renders a stable application shell"),
                "",
            ),
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): ExecResult(0, _playwright_report("searches books by title"), ""),
        }
    )
    question_transport = _GoalGraphTransport(
        sandbox,
        plan=_one_goal_graph_plan(),
        structured_question_calls={1},
    )
    await _goal_graph_orchestrator(
        repository,
        settings,
        sandbox,
        question_transport,
    ).run(run.id, lease_token=claimed.lease_owner)

    waiting = await repository.get_run(run.id)
    assert waiting.status is RunStatus.waiting_for_user
    assert waiting.pending_input_request is not None
    _message, _request, queued, _created = await repository.answer_user_input(
        run.id,
        waiting.pending_input_request.id,
        owner.id,
        "planning-continuation-answer",
        "Yes",
    )
    assert queued.status is RunStatus.queued
    resumed = await repository.claim_next_run("planning-answer-worker", 120)
    assert resumed is not None and resumed.lease_owner

    invalid = _one_goal_graph_plan()
    invalid["routes"][0]["path"] = "/home"  # type: ignore[index]
    answer_transport = _GoalGraphPlanningSequenceTransport(
        sandbox,
        [invalid, _one_goal_graph_plan()],
    )
    await _goal_graph_orchestrator(
        repository,
        settings,
        sandbox,
        answer_transport,
    ).run(run.id, lease_token=resumed.lease_owner)

    final = await repository.get_run(run.id)
    assert final.status is RunStatus.succeeded
    assert answer_transport.structured_calls == 2
    assert answer_transport.require_resumes[0] is True
    retries = [
        event
        for event in await repository.list_events(run.id)
        if event.kind == "pi.retrying"
    ]
    assert len(retries) == 1
    assert retries[0].payload["diagnostic"]["violationCode"] == (
        "goal_graph_domain_invalid"
    )


@pytest.mark.asyncio
async def test_first_goal_noop_cannot_enter_verification(repository, settings) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a searchable library.",
        "goal-first-noop",
    )
    sandbox = GitAwareSandbox()
    transport = _GoalGraphTransport(
        sandbox,
        plan=_one_goal_graph_plan(),
        noop_calls={2, 3},
    )
    orchestrator = _goal_graph_orchestrator(repository, settings, sandbox, transport)

    with pytest.raises(AgentNoEffect):
        await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id)
    assert final.status is RunStatus.failed
    assert final.error_code == "agent_no_effect"
    assert final.repair_round == 1
    assert transport.calls == 3  # planning, build, bounded settlement repair
    assert any(event.kind == "runtime.turn.repairing" for event in events)
    assert not any(event.kind == "verification.suite_started" for event in events)


@pytest.mark.asyncio
async def test_first_goal_protected_only_delta_is_noop_after_audit_restore(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a searchable library.",
        "goal-first-protected-only",
    )
    sandbox = GitAwareSandbox()
    transport = _GoalGraphTransport(
        sandbox,
        plan=_one_goal_graph_plan(),
        protected_only_calls={2},
    )
    orchestrator = _goal_graph_orchestrator(repository, settings, sandbox, transport)

    with pytest.raises(AgentNoEffect):
        await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id)
    assert final.status is RunStatus.failed
    assert final.error_code == "agent_no_effect"
    assert final.repair_round == 0
    assert transport.calls == 2
    assert not any(event.kind == "build.turn.completed" for event in events)
    assert not any(event.kind == "verification.suite_started" for event in events)


@pytest.mark.asyncio
async def test_later_typecheck_repair_requires_net_candidate_change_after_audit_restore(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a library with search and durable create.",
        "goal-later-typecheck-protected-only",
    )
    harness = _playwright_report("starter renders a stable application shell")
    search = _playwright_report("searches books by title")
    typecheck_command = fomo_runner_command(bin_name="tsc", args="--noEmit")
    sandbox = GitAwareSandbox(
        {
            typecheck_command: [
                ExecResult(0, "", ""),
                ExecResult(0, "", ""),
                ExecResult(1, "", "type error"),
                ExecResult(0, "", ""),
            ],
            _playwright_command(_HARNESS_PATH): ExecResult(0, harness, ""),
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): ExecResult(0, search, ""),
        }
    )
    transport = _GoalGraphTransport(
        sandbox,
        noop_calls={3},
        protected_only_calls={4},
    )
    orchestrator = _goal_graph_orchestrator(repository, settings, sandbox, transport)

    with pytest.raises(AgentNoEffect):
        await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id)
    settlements = [event.payload for event in events if event.kind == "build.turn.completed"]
    suites = [event.payload for event in events if event.kind == "verification.suite_started"]
    assert final.status is RunStatus.failed
    assert final.error_code == "agent_no_effect"
    assert final.repair_round == 1
    assert transport.calls == 4
    assert [item["goalId"] for item in settlements] == ["G-1"]
    assert [(item["mode"], item["goalIds"]) for item in suites] == [
        ("focused", ["G-1"]),
    ]


@pytest.mark.asyncio
async def test_later_noop_is_denied_when_generation_drifted_from_checkpoint(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a library with search, durable create, and reset.",
        "goal-later-drift-noop",
    )
    harness = _playwright_report("starter renders a stable application shell")
    search = _playwright_report("searches books by title")
    sandbox = _DriftingGoalSandbox(
        {
            _playwright_command(_HARNESS_PATH): ExecResult(0, harness, ""),
            _playwright_command("tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"): ExecResult(
                0, search, ""
            ),
        }
    )
    transport = _GoalGraphTransport(
        sandbox,
        plan=_three_goal_graph_plan(),
        noop_calls={3, 4},
    )
    orchestrator = _goal_graph_orchestrator(repository, settings, sandbox, transport)

    with pytest.raises(AgentNoEffect):
        await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id)
    settlements = [event.payload for event in events if event.kind == "build.turn.completed"]
    suites = [event.payload for event in events if event.kind == "verification.suite_started"]
    assert sandbox.drifted
    assert final.status is RunStatus.failed
    assert final.error_code == "agent_no_effect"
    assert transport.calls == 4
    assert [item["effectPolicy"] for item in settlements] == ["must_change"]
    assert [(item["mode"], item["goalIds"]) for item in suites] == [
        ("focused", ["G-1"]),
    ]


@pytest.mark.asyncio
async def test_later_preimplemented_goals_can_noop_into_authoritative_verification(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a library with search, durable create, and reset.",
        "goal-later-preimplemented",
    )
    harness = _playwright_report("starter renders a stable application shell")
    search = _playwright_report("searches books by title")
    create = _playwright_report("creates and persists a book")
    reset = _playwright_report("resets the library")
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): [
                ExecResult(0, harness, ""),
                ExecResult(0, harness, ""),
                ExecResult(0, harness, ""),
            ],
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): [
                ExecResult(0, search, ""),
                ExecResult(0, search, ""),
            ],
            _playwright_command("tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"): [
                ExecResult(0, create, ""),
                ExecResult(0, create, ""),
            ],
            _playwright_command(
                "tests/fomo-acceptance/G-3/reset-library.smoke.spec.ts"
            ): ExecResult(0, reset, ""),
        }
    )
    transport = _GoalGraphTransport(
        sandbox,
        plan=_three_goal_graph_plan(),
        noop_calls={3, 4},
    )
    orchestrator = _goal_graph_orchestrator(repository, settings, sandbox, transport)

    await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    projection = await repository.get_goal_graph_for_run(run.id)
    events = await repository.list_events(run.id)
    settlements = [event.payload for event in events if event.kind == "build.turn.completed"]
    suites = [event.payload for event in events if event.kind == "verification.suite_started"]
    changed_files = [event.payload for event in events if event.kind == "file.changed"]
    assert final.status is RunStatus.succeeded
    assert projection is not None
    assert [goal.status.value for goal in projection.graph.goals] == [
        "verified",
        "verified",
        "verified",
    ]
    assert transport.calls == 4  # planning plus one turn per goal
    assert [item["effectPolicy"] for item in settlements] == [
        "must_change",
        "may_noop",
        "may_noop",
    ]
    assert [item["noOp"] for item in settlements] == [False, True, True]
    assert [item["changedFileCount"] for item in settlements] == [1, 0, 0]
    assert [(item["mode"], item["goalIds"]) for item in suites] == [
        ("focused", ["G-1"]),
        ("focused", ["G-2"]),
        ("full", ["G-1", "G-2", "G-3"]),
    ]
    assert [(item["goalId"], item["path"]) for item in changed_files] == [
        ("G-1", "components/features/library-desk.tsx")
    ]


@pytest.mark.asyncio
async def test_failed_later_noop_requires_repair_to_change_the_workspace(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a library with search, durable create, and reset.",
        "goal-later-noop-repair-noop",
    )
    harness = _playwright_report("starter renders a stable application shell")
    search = _playwright_report("searches books by title")
    create_failure = _playwright_failure_report("creates and persists a book")
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): [
                ExecResult(0, harness, ""),
                ExecResult(0, harness, ""),
            ],
            _playwright_command("tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"): ExecResult(
                0, search, ""
            ),
            _playwright_command("tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"): ExecResult(
                1, create_failure, ""
            ),
        }
    )
    transport = _GoalGraphTransport(
        sandbox,
        plan=_three_goal_graph_plan(),
        noop_calls={3, 4},
    )
    orchestrator = _goal_graph_orchestrator(repository, settings, sandbox, transport)

    with pytest.raises(AgentNoEffect):
        await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id)
    settlements = [event.payload for event in events if event.kind == "build.turn.completed"]
    suites = [event.payload for event in events if event.kind == "verification.suite_started"]
    assert final.status is RunStatus.failed
    assert final.error_code == "agent_no_effect"
    assert final.repair_round == 1
    assert transport.calls == 4  # planning, G-1, G-2 no-op, rejected repair no-op
    assert [item["effectPolicy"] for item in settlements] == [
        "must_change",
        "may_noop",
    ]
    assert [item["noOp"] for item in settlements] == [False, True]
    assert [(item["mode"], item["goalIds"]) for item in suites] == [
        ("focused", ["G-1"]),
        ("focused", ["G-2"]),
    ]
    assert sum(event.kind == "goal.verification_failed" for event in events) == 1


@pytest.mark.asyncio
async def test_verification_repair_continuation_preserves_must_change(repository, settings) -> None:
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, "Repair continuation")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "verification-repair-question",
        "Build a library with search and durable create.",
    )
    claimed = await repository.claim_next_run("verification-repair-question-worker", 120)
    assert claimed is not None and claimed.lease_owner

    harness = _playwright_report("starter renders a stable application shell")
    search = _playwright_report("searches books by title")
    create_failure = _playwright_failure_report("creates and persists a book")
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): [
                ExecResult(0, harness, ""),
                ExecResult(0, harness, ""),
            ],
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): [
                ExecResult(0, search, ""),
                ExecResult(0, search, ""),
            ],
            _playwright_command("tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"): ExecResult(
                1, create_failure, ""
            ),
        }
    )
    first_transport = _GoalGraphTransport(
        sandbox,
        noop_calls={3},
        question_calls={4},
    )
    orchestrator = _goal_graph_orchestrator(
        repository,
        settings,
        sandbox,
        first_transport,
    )

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    waiting = await repository.get_run(run.id)
    assert waiting.status is RunStatus.waiting_for_user
    assert waiting.pending_input_request is not None
    await repository.answer_user_input(
        run.id,
        waiting.pending_input_request.id,
        owner.id,
        "verification-repair-answer",
        "Grid",
    )
    resumed = await repository.claim_next_run("verification-repair-answer-worker", 120)
    assert resumed is not None and resumed.id == run.id and resumed.lease_owner
    answer_transport = _AnswerContinuationTransport(
        sandbox,
        expected_session_id=f"fomo-{run.id}",
        expected_sandbox_id=await persisted_sandbox_id(repository, run.id) or "",
        apply_change=False,
    )
    resumed_orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        _Gateway(),
        answer_transport,
    )

    with pytest.raises(AgentNoEffect):
        await resumed_orchestrator.run(run.id, lease_token=resumed.lease_owner)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id)
    assert final.status is RunStatus.failed
    assert final.error_code == "agent_no_effect"
    assert final.repair_round == 1
    assert answer_transport.calls == 1
    assert sum(event.kind == "goal.verification_failed" for event in events) == 1


@pytest.mark.asyncio
async def test_verification_repair_continuation_preserves_goal_round(
    repository, settings
) -> None:
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, "Repair round continuation")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "verification-repair-round-question",
        "Build a library with search and durable create.",
    )
    claimed = await repository.claim_next_run(
        "verification-repair-round-question-worker", 120
    )
    assert claimed is not None and claimed.lease_owner

    harness = _playwright_report("starter renders a stable application shell")
    search = _playwright_report("searches books by title")
    create = _playwright_report("creates and persists a book")
    create_failure = _playwright_failure_report("creates and persists a book")
    sandbox = GitAwareSandbox(
        {
            _playwright_command(_HARNESS_PATH): [
                ExecResult(0, harness, ""),
                ExecResult(0, harness, ""),
                ExecResult(0, harness, ""),
            ],
            _playwright_command(
                "tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"
            ): [
                ExecResult(0, search, ""),
                ExecResult(0, search, ""),
                ExecResult(0, search, ""),
            ],
            _playwright_command("tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"): [
                ExecResult(1, create_failure, ""),
                ExecResult(0, create, ""),
            ],
        }
    )
    first_transport = _GoalGraphTransport(
        sandbox,
        noop_calls={3},
        question_calls={4},
    )
    orchestrator = _goal_graph_orchestrator(
        repository,
        settings,
        sandbox,
        first_transport,
    )

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    waiting = await repository.get_run(run.id)
    assert waiting.status is RunStatus.waiting_for_user
    assert waiting.pending_input_request is not None
    await repository.answer_user_input(
        run.id,
        waiting.pending_input_request.id,
        owner.id,
        "verification-repair-round-answer",
        "Grid",
    )
    resumed = await repository.claim_next_run(
        "verification-repair-round-answer-worker", 120
    )
    assert resumed is not None and resumed.id == run.id and resumed.lease_owner
    answer_transport = _AnswerContinuationTransport(
        sandbox,
        expected_session_id=f"fomo-{run.id}",
        expected_sandbox_id=await persisted_sandbox_id(repository, run.id) or "",
        change_path="lib/domain/books.ts",
        change_content="export type Book = { id: string; title: string; saved: boolean };\n",
    )
    resumed_orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        _Gateway(),
        answer_transport,
    )

    await resumed_orchestrator.run(run.id, lease_token=resumed.lease_owner)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id)
    suites = [event.payload for event in events if event.kind == "verification.suite_started"]
    assert final.status is RunStatus.succeeded
    assert final.repair_round == 1
    assert answer_transport.calls == 1
    assert [item["round"] for item in suites] == [0, 0, 1]


@pytest.mark.asyncio
async def test_recovery_run_plans_against_the_restored_verified_checkpoint(
    repository, settings
) -> None:
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, "Recovery library")
    requirement = "Build a library with search and durable create."
    _message, source, _created = await repository.create_message_and_run(
        project.id,
        owner.id,
        "recovery-source",
        requirement,
    )
    claimed_source = await repository.claim_next_run("recovery-source-worker", 60)
    assert claimed_source is not None and claimed_source.lease_owner
    source_lease = claimed_source.lease_owner
    source_draft = parse_goal_graph_draft(_goal_graph_plan())
    await repository.create_goal_graph(
        project.id,
        source.id,
        source_draft,
        architecture_profile=derive_product_architecture_profile(
            requirement=requirement,
            route_count=len(source_draft.routes),
            goal_count=len(source_draft.goals),
        ),
        lease_token=source_lease,
    )
    await repository.activate_goal(source.id, "G-1", lease_token=source_lease)
    await repository.claim_goal(source.id, "G-1", lease_token=source_lease)
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    checkpoint_files = [
        {"path": item.path, "content": item.as_change().content}
        for item in starter.files
        if not item.path.startswith("tests/harness/")
        and not item.path.startswith("tests/fomo-acceptance/")
        and item.path != "next-env.d.ts"
        and item.path != ".gitignore"
    ]
    checkpoint_files.append(
        {
            "path": "lib/domain/recovered.ts",
            "content": "export const recoveredCheckpoint = true;\n",
        }
    )
    claimed_projection = await repository.get_goal_graph_for_run(source.id)
    assert claimed_projection is not None
    navigation_suite = derive_navigation_verification_suite(
        claimed_projection.graph,
        goal_ids=("G-1",),
        mode="focused",
    )
    assert navigation_suite is not None
    navigation_evidence = [
        {
            "acceptanceKey": navigation_evidence_key(
                navigation_suite.version,
                test_id,
            ),
            "kind": f"fomo_navigation_v{navigation_suite.version}",
            "status": "passed",
        }
        for test_id in navigation_test_ids(navigation_suite)
    ]
    await repository.record_verified_checkpoint(
        source.id,
        "G-1",
        checkpoint_files,
        [
            {
                "acceptanceKey": "G-1:AC-1",
                "kind": "playwright_smoke",
                "status": "passed",
            },
            *navigation_evidence,
        ],
        lease_token=source_lease,
        commit_sha="c" * 40,
        capsule={
            "verifiedEvidence": [
                {
                    "goalId": "G-1",
                    "passedAcceptanceIds": ["AC-1"],
                    "evidenceRefs": ["checkpoint:source-g1"],
                }
            ]
        },
        navigation_suite=navigation_suite,
    )
    await repository.mark_terminal(
        source.id,
        RunStatus.needs_attention,
        error_code="goal_verification_failed",
        lease_token=source_lease,
    )
    _message, recovered, created, mode, checkpoint_available = (
        await repository.create_recovery_message_and_run(
            source.id,
            owner.id,
            "recovery-follow-up",
            "Keep the verified work and finish the remaining interactions.",
        )
    )
    assert created and mode == "verified_checkpoint" and checkpoint_available
    claimed_recovery = await repository.claim_next_run("recovery-worker", 60)
    assert claimed_recovery is not None and claimed_recovery.id == recovered.id
    assert claimed_recovery.lease_owner

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
            ): [
                ExecResult(0, search, ""),
                ExecResult(0, search, ""),
            ],
            _playwright_command("tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"): ExecResult(
                0, create, ""
            ),
        }
    )
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        _Gateway(),
        _GoalGraphTransport(sandbox),
    )

    await orchestrator.run(recovered.id, lease_token=claimed_recovery.lease_owner)

    final = await repository.get_run(recovered.id)
    checkpoint = await repository.get_latest_verified_checkpoint(recovered.id)
    events = await repository.list_events(recovered.id)
    suites = [event.payload for event in events if event.kind == "verification.suite_started"]
    assert final.status is RunStatus.succeeded
    assert checkpoint is not None
    assert "lib/domain/recovered.ts" in {item.path for item in checkpoint.files}
    assert [item["reason"] for item in suites] == [
        "legacy_checkpoint_unknown_paths",
        "final_goal",
    ]


@pytest.mark.asyncio
async def test_goal_graph_repairs_workspace_audit_in_same_session(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a library with search and durable create.",
        "goal-graph-workspace-audit-repair",
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
            _playwright_command("tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"): [
                ExecResult(0, search, ""),
                ExecResult(0, search, ""),
            ],
            _playwright_command("tests/fomo-acceptance/G-2/create-book.smoke.spec.ts"): ExecResult(
                0, create, ""
            ),
        }
    )
    transport = _GoalGraphTransport(sandbox, workspace_audit_repair=True)
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        _Gateway(),
        transport,
    )

    await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    assert final.status is RunStatus.succeeded
    assert transport.calls == 4
    assert len(set(transport.session_ids)) == 1
    assert transport.require_resumes == [False, False, True, False]
    events = await repository.list_events(run.id)
    repair_event = next(
        event for event in events if event.kind == "workspace.audit_repairing"
    )
    assert repair_event.payload == {
        "goalId": "G-1",
        "code": "invalid_source_encoding",
        "affectedFileCount": 1,
    }


async def _run_until_goal_graph_question(
    repository,
    settings,
    suffix: str,
    *,
    change_before_question: bool = False,
):
    owner = await create_user_session(repository)
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
            _playwright_command("tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"): [
                ExecResult(0, search, ""),
                ExecResult(0, search, ""),
            ],
        }
    )
    transport = _PlanningThenQuestionTransport(
        sandbox,
        change_before_question=change_before_question,
    )
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
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
        settings,
        _Gateway(),
        answer_transport,
    )

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    assert answer_transport.calls == 1
    completed = await repository.get_run(run.id)
    assert (completed.status, completed.error_code) == (RunStatus.succeeded, None), (
        sandbox.sandboxes[answer_transport.expected_sandbox_id].commands
    )
    assert await repository.get_run_continuation(run.id) is None
    events = await repository.list_events(run.id)
    assert any(event.kind == "run.resumed" for event in events)
    assert not any(event.kind == "run.continuation_unavailable" for event in events)


@pytest.mark.asyncio
async def test_build_continuation_settles_from_pre_question_logical_start(
    repository, settings
) -> None:
    owner, _project, run, request, sandbox, first_transport = await _run_until_goal_graph_question(
        repository,
        settings,
        "pre-question-change",
        change_before_question=True,
    )
    await repository.answer_user_input(
        run.id,
        request.id,
        owner.id,
        "clarification-answer-pre-question-change",
        "Grid",
    )
    claimed = await repository.claim_next_run("answer-worker-pre-question-change", 120)
    assert claimed is not None and claimed.id == run.id and claimed.lease_owner
    answer_transport = _AnswerContinuationTransport(
        sandbox,
        expected_session_id=first_transport.session_id or "",
        expected_sandbox_id=first_transport.sandbox_id or "",
        apply_change=False,
    )
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        _Gateway(),
        answer_transport,
    )

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id)
    settlements = [event.payload for event in events if event.kind == "build.turn.completed"]
    assert final.status is RunStatus.succeeded
    assert final.repair_round == 0
    assert answer_transport.calls == 1
    assert [(item["effectPolicy"], item["changedFileCount"]) for item in settlements] == [
        ("must_change", 1),
    ]


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
        settings,
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
async def test_goal_graph_continuation_rejects_architecture_profile_tampering(
    repository, settings
) -> None:
    owner, _project, run, request, sandbox, first_transport = (
        await _run_until_goal_graph_question(repository, settings, "profile-tamper")
    )
    async with repository.database.session_factory() as session:
        record = await session.get(RunRecord, run.id)
        assert record is not None and isinstance(record.continuation_context, dict)
        context = dict(record.continuation_context)
        context["architectureProfileHash"] = "0" * 64
        record.continuation_context = context
        await session.commit()
    await repository.answer_user_input(
        run.id,
        request.id,
        owner.id,
        "clarification-answer-profile-tamper",
        "Grid",
    )
    claimed = await repository.claim_next_run("answer-worker-profile-tamper", 120)
    assert claimed is not None and claimed.id == run.id and claimed.lease_owner
    answer_transport = _AnswerContinuationTransport(
        sandbox,
        expected_session_id=first_transport.session_id or "",
        expected_sandbox_id=first_transport.sandbox_id or "",
    )

    await _goal_graph_orchestrator(
        repository,
        settings,
        sandbox,
        answer_transport,
    ).run(run.id, lease_token=claimed.lease_owner)

    failed = await repository.get_run(run.id)
    assert failed.status is RunStatus.needs_attention
    assert failed.error_code == "pi_session_resume_unavailable"
    assert answer_transport.calls == 0


@pytest.mark.asyncio
async def test_goal_graph_reuses_failed_build_planning_artifact_and_starts_with_build(
    repository, settings
) -> None:
    requirement = "Build a library with search and durable create."
    owner = await create_user_session(repository)
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
            "productDesignPolicy": PRODUCT_DESIGN_POLICY,
            "architectureProfilePolicy": (
                f"{ARCHITECTURE_PROFILE_ID}@{ARCHITECTURE_PROFILE_VERSION}"
            ),
            **resolve_runtime_contract().cache_fingerprint(),
        },
        lease_token=prior_claim.lease_owner,
    )
    draft = parse_goal_graph_draft(_goal_graph_plan())
    prior_graph = await repository.create_goal_graph(
        project.id,
        prior.id,
        draft,
        architecture_profile=derive_product_architecture_profile(
            requirement=requirement,
            route_count=len(draft.routes),
            goal_count=len(draft.goals),
        ),
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
            "productDesignPolicy": PRODUCT_DESIGN_POLICY,
            **resolve_runtime_contract().cache_fingerprint(),
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
            _playwright_command("tests/fomo-acceptance/G-1/search-books.smoke.spec.ts"): [
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
        settings,
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
    await repository.create_goal_graph(
        project.id,
        run.id,
        draft,
        architecture_profile=derive_product_architecture_profile(
            requirement="Build a library with search and durable create.",
            route_count=len(draft.routes),
            goal_count=len(draft.goals),
        ),
        lease_token=lease,
    )
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    checkpoint_files = [
        {"path": item.path, "content": item.as_change().content}
        for item in starter.files
        if not item.path.startswith("tests/harness/")
        and not item.path.startswith("tests/fomo-acceptance/")
        and item.path != "next-env.d.ts"
        and item.path != ".gitignore"
    ]
    navigation_suite, navigation_evidence = await _navigation_checkpoint_fixture(
        repository,
        run.id,
        "G-1",
    )
    await repository.record_verified_checkpoint(
        run.id,
        "G-1",
        checkpoint_files,
        [
            {
                "acceptanceKey": "G-1:AC-1",
                "kind": "playwright_smoke",
                "status": "passed",
            },
            *navigation_evidence,
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
        navigation_suite=navigation_suite,
    )
    await repository.record_usage_entry(
        run.id,
        "prior-request",
        lease_token=lease,
        provider="fomo-litellm",
        model=resolve_runtime_contract().model_ref,
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
        settings,
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
    runtime = resolve_runtime_contract()
    assert gateway.issued[0]["max_budget"] == pytest.approx(
        (runtime.max_spend_micros - 500_000) / 1_000_000
    )
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
    publish_draft = parse_goal_graph_draft(_goal_graph_plan())
    await repository.create_goal_graph(
        project.id,
        run.id,
        publish_draft,
        architecture_profile=derive_product_architecture_profile(
            requirement="Build a library with search and durable create.",
            route_count=len(publish_draft.routes),
            goal_count=len(publish_draft.goals),
        ),
        lease_token=lease,
    )
    starter = resolve_starter_manifest(("crud", "local-persistence"))
    files = [
        {"path": item.path, "content": item.as_change().content}
        for item in starter.files
        if not item.path.startswith("tests/harness/")
        and not item.path.startswith("tests/fomo-acceptance/")
        and item.path != "next-env.d.ts"
        and item.path != ".gitignore"
    ]
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    first_navigation, first_navigation_evidence = (
        await _navigation_checkpoint_fixture(repository, run.id, "G-1")
    )
    await repository.record_verified_checkpoint(
        run.id,
        "G-1",
        files,
        [
            {
                "acceptanceKey": "G-1:AC-1",
                "kind": "playwright_smoke",
                "status": "passed",
            },
            *first_navigation_evidence,
        ],
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
        navigation_suite=first_navigation,
    )
    await repository.claim_goal(run.id, "G-2", lease_token=lease)
    final_navigation, final_navigation_evidence = (
        await _navigation_checkpoint_fixture(
            repository,
            run.id,
            "G-2",
            mode="final_full",
        )
    )
    await repository.record_verified_checkpoint(
        run.id,
        "G-2",
        files,
        [
            {
                "acceptanceKey": "G-2:AC-2",
                "kind": "playwright_smoke",
                "status": "passed",
            },
            *final_navigation_evidence,
        ],
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
        navigation_suite=final_navigation,
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
        settings,
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
async def test_goal_graph_gateway_failure_is_specific_and_does_not_leak(
    repository, settings
) -> None:
    _project, run, lease = await _new_project_run(
        repository,
        "Build a polished library manager.",
        "goal-gateway-failure",
    )
    sandbox = GitAwareSandbox()

    class FailingGateway(_Gateway):
        async def issue(self, **values: Any) -> RunVirtualKey:
            self.issued.append(dict(values))
            raise InferenceGatewayError(
                "gateway returned Authorization: Bearer private-token"
            )

    gateway = FailingGateway()
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        gateway,
        _GoalGraphTransport(sandbox),
    )

    with pytest.raises(InferenceGatewayError):
        await orchestrator.run(run.id, lease_token=lease)

    final = await repository.get_run(run.id)
    events = await repository.list_events(run.id)
    failure = next(event for event in reversed(events) if event.kind == "pi.failed")
    run_failed = next(event for event in reversed(events) if event.kind == "run.failed")
    serialized = json.dumps(failure.payload, ensure_ascii=False)
    assert final.error_code == "inference_gateway_unavailable"
    assert run_failed.payload["summary"] == "模型服务暂时不可用，请稍后重试。"
    assert failure.payload == {
        "code": "inference_gateway_unavailable",
        "message": "模型服务暂时不可用，请稍后重试。",
    }
    assert "private-token" not in serialized


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


async def _publish_projection(repository, project_id: str, run_id: str, lease: str):
    draft = parse_goal_graph_draft(_one_goal_graph_plan())
    return await repository.create_goal_graph(
        project_id,
        run_id,
        draft,
        architecture_profile=derive_product_architecture_profile(
            requirement="Build a polished library manager.",
            route_count=len(draft.routes),
            goal_count=len(draft.goals),
        ),
        lease_token=lease,
    )


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
        _GoalGraphTransport(sandbox),
    )
    projection = await _publish_projection(repository, _project.id, run.id, lease)

    await orchestrator._publish(
        run.id,
        _project.id,
        lease,
        workspaces,
        verifier,  # type: ignore[arg-type]
        snapshot,
        outcome,
        projection,
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
    assert f"git tag version/1 {CANDIDATE_SHA}" in sandbox.sandboxes[ref.id].commands


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
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        _Gateway(),
        _GoalGraphTransport(sandbox),
    )
    projection = await _publish_projection(repository, project.id, run.id, lease)

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
            projection,
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
    orchestrator = DirectPiOrchestrator(
        repository,
        sandbox,
        settings,
        _Gateway(),
        _GoalGraphTransport(sandbox),
    )
    projection = await _publish_projection(repository, _project.id, run.id, lease)

    with pytest.raises(error_type, match=error_match):
        await orchestrator._publish(
            run.id,
            _project.id,
            lease,
            workspaces,
            verifier,  # type: ignore[arg-type]
            snapshot,
            outcome,
            projection,
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
