from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from fomo.direct_pi import DirectPiOrchestrator
from fomo.fomo_pi_ds import PiBridgeEnvelope, PiBridgeResult, PiTransportResult, RunVirtualKey
from fomo.sandbox.base import ExecResult, FileChange
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.schemas import RunStatus


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

    async def issue(self, **values: Any) -> RunVirtualKey:
        return RunVirtualKey(
            run_id=str(values["run_id"]),
            key_alias=f"fomo-run-{values['run_id']}",
            duration_seconds=int(values["duration_seconds"]),
            secret="sk-test-run-key",
        )

    async def block(self, _key: RunVirtualKey) -> None:
        self.blocked = True


class _Transport:
    def __init__(self, sandbox: FakeSandboxProvider) -> None:
        self.sandbox = sandbox
        self.calls = 0

    async def run(self, ref, invocation, *, on_event=None, on_diagnostic=None, cancel_event=None):
        self.calls += 1
        text = json.dumps(_plan(), ensure_ascii=False, separators=(",", ":"))
        if self.calls == 2:
            await self.sandbox.apply_changes(
                ref,
                [
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
                        content='export type Book = { id: string; title: string };\n',
                    ),
                ],
            )
            text = "Implemented the frozen library plan."
        session_id = invocation.request.session_id
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
            PiBridgeEnvelope(
                seq=3,
                type="pi.event",
                payload={"kind": "turn_end", "role": "assistant", "text": text},
            ),
            PiBridgeEnvelope(seq=4, type="pi.event", payload={"kind": "agent_settled"}),
            PiBridgeEnvelope(seq=5, type="completed", payload={"sessionId": session_id, "stats": stats}),
        ]
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


@pytest.mark.asyncio
async def test_direct_pi_full_loop_publishes_real_contract_evidence(repository, settings) -> None:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id,
        session.id,
        "direct-pi-run",
        "Build a polished library manager with search and durable create.",
    )
    claimed = await repository.claim_next_run("direct-worker", 60)
    assert claimed is not None and claimed.lease_owner

    commands = {
        "pnpm exec playwright test tests/harness/starter.smoke.spec.ts --config=tests/fomo-acceptance/fomo.config.ts --project=chromium --reporter=json": ExecResult(
            0, _playwright_report("starter renders a stable application shell"), ""
        ),
        "pnpm exec playwright test tests/fomo-acceptance/search-books.smoke.spec.ts --config=tests/fomo-acceptance/fomo.config.ts --project=chromium --reporter=json": ExecResult(
            0, _playwright_report("searches books by title"), ""
        ),
        "pnpm exec playwright test tests/fomo-acceptance/create-book.smoke.spec.ts --config=tests/fomo-acceptance/fomo.config.ts --project=chromium --reporter=json": ExecResult(
            0, _playwright_report("creates and persists a book"), ""
        ),
    }
    sandbox = FakeSandboxProvider(commands)
    gateway = _Gateway()
    transport = _Transport(sandbox)
    direct_settings = replace(settings, agent_framework="direct_pi")
    orchestrator = DirectPiOrchestrator(
        repository, sandbox, direct_settings, gateway, transport
    )

    await orchestrator.run(run.id, lease_token=claimed.lease_owner)

    final = await repository.get_run(run.id)
    assert final.status == RunStatus.succeeded
    assert final.preview_url == "http://fake-preview.invalid:8080"
    assert transport.calls == 2
    assert gateway.blocked
    assert await repository.get_latest_artifact(run.id, "build_plan") is not None
    assert await repository.get_latest_artifact(run.id, "acceptance_contract") is not None
    trace = await repository.get_trace(project.id, run.id)
    assert {item["status"] for item in trace["acceptance_trace"]} == {"passed"}
    assert {item["implementationStatus"] for item in trace["acceptance_trace"]} == {"implemented"}
    versions = await repository.list_versions(project.id)
    assert len(versions) == 1 and versions[0].qa_status == "passed"
    _version_id, composition, _digest = await repository.get_version_file_content(
        project.id, "app/(generated)/composition.tsx", versions[0].id
    )
    assert "LibraryDesk" in composition
