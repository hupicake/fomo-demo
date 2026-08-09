from __future__ import annotations

import json
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest

from fomo.api import create_app
from fomo.direct_pi.goalgraph import parse_goal_graph_draft
from fomo.schemas import UserInputRequestDraft


def _single_goal_draft():
    return parse_goal_graph_draft(
        {
            "schemaVersion": 1,
            "productOutcome": "A visible goal panel",
            "goals": [
                {
                    "goalId": "G-1",
                    "title": "Visible workflow",
                    "productOutcome": "The workflow works",
                    "userVisible": True,
                    "dependsOn": [],
                    "acceptance": {
                        "criteria": [
                            {
                                "id": "AC-1",
                                "title": "Workflow is visible",
                                "priority": "must",
                                "given": "the app is open",
                                "when": "the page loads",
                                "then": "the workflow appears",
                            }
                        ],
                        "tests": [
                            {
                                "id": "T-1",
                                "acceptanceId": "AC-1",
                                "title": "Workflow smoke",
                                "actions": [{"kind": "goto", "path": "/"}],
                                "assertions": [
                                    {
                                        "kind": "visible",
                                        "target": {
                                            "by": "role",
                                            "value": "main",
                                            "name": "Application",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_guest_project_idempotent_message_and_persistent_event_replay(repository, settings) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_response = await client.post("/v1/sessions/guest")
        assert session_response.status_code == 201
        session_id = session_response.json()["id"]
        headers = {"X-FOMO-Session": session_id}
        project_response = await client.post("/v1/projects", headers=headers, json={"title": "Library"})
        assert project_response.status_code == 201
        project_id = project_response.json()["id"]

        body = {"clientMessageId": "msg-1", "content": "Build a library manager"}
        first = await client.post(
            f"/v1/projects/{project_id}/messages",
            headers={**headers, "Idempotency-Key": "msg-1"},
            json=body,
        )
        assert first.status_code == 202
        run_id = first.json()["run"]["id"]
        project_snapshot = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert project_snapshot.status_code == 200
        snapshot_payload = project_snapshot.json()
        assert snapshot_payload["activeRun"]["id"] == run_id
        assert snapshot_payload["lastSeq"] == 1
        assert [event["seq"] for event in snapshot_payload["events"]] == [1]
        assert snapshot_payload["files"] == []
        assert snapshot_payload["versions"] == []
        assert snapshot_payload["goalGraph"] is None
        assert snapshot_payload["trace"]["acceptanceTrace"] == []
        assert snapshot_payload["preview"] == {
            "status": "unavailable",
            "url": None,
            "runId": None,
            "verificationStatus": None,
        }
        duplicate = await client.post(
            f"/v1/projects/{project_id}/messages",
            headers={**headers, "Idempotency-Key": "msg-1"},
            json=body,
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["run"]["id"] == run_id

        await repository.append_event(
            run_id,
            "agent.started",
            role="architect",
            payload={"role": "architect"},
        )
        await repository.append_event(
            run_id,
            "command.completed",
            role="engineer",
            payload={"operationId": "operation-test", "exitCode": 0},
        )

        cancelled = await client.post(f"/v1/runs/{run_id}/cancel", headers=headers)
        assert cancelled.status_code == 200
        events = await client.get(f"/v1/runs/{run_id}/events", headers=headers)
        assert events.status_code == 200
        assert "event: run.event" in events.text
        assert '"seq":1' in events.text
        snapshot = await client.get(f"/v1/runs/{run_id}", headers=headers)
        assert snapshot.json()["status"] == "cancelled"

        # A terminal run is not active, but a refresh must replay its visible
        # role and command history so the workbench does not reset to idle.
        terminal_project_snapshot = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert terminal_project_snapshot.status_code == 200
        terminal_payload = terminal_project_snapshot.json()
        assert terminal_payload["activeRun"] is None
        assert terminal_payload["lastSeq"] == 5
        assert [event["seq"] for event in terminal_payload["events"]] == [1, 2, 3, 4, 5]
        assert any(
            event["kind"] == "agent.started" and event["role"] == "architect"
            for event in terminal_payload["events"]
        )
        assert any(
            event["kind"] == "command.completed" and event["role"] == "engineer"
            for event in terminal_payload["events"]
        )


@pytest.mark.asyncio
async def test_answer_endpoint_is_owned_idempotent_and_requeues_the_same_run(
    repository, settings
) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        owner_id = (await client.post("/v1/sessions/guest")).json()["id"]
        other_id = (await client.post("/v1/sessions/guest")).json()["id"]
        owner_headers = {"X-FOMO-Session": owner_id}
        project_id = (
            await client.post(
                "/v1/projects",
                headers=owner_headers,
                json={"title": "Clarification"},
            )
        ).json()["id"]
        run_id = (
            await client.post(
                f"/v1/projects/{project_id}/messages",
                headers={**owner_headers, "Idempotency-Key": "initial-question"},
                json={
                    "clientMessageId": "initial-question",
                    "content": "Build a library page",
                },
            )
        ).json()["run"]["id"]
        claimed = await repository.claim_next_run("question-worker", 120)
        assert claimed is not None and claimed.lease_owner
        await repository.set_sandbox_id(
            run_id,
            "sandbox-api-answer",
            lease_token=claimed.lease_owner,
        )
        await repository.register_sandbox_resource(
            run_id,
            "sandbox-api-answer",
            "generation",
            lease_token=claimed.lease_owner,
        )
        request = await repository.wait_for_user_input(
            run_id,
            UserInputRequestDraft(
                question="Choose a layout",
                choices=["Grid", "List"],
                allow_freeform=False,
            ),
            continuation_key="goal_graph.goal_build",
            continuation_context={
                "baselineHashes": {"app/page.tsx": "a" * 64},
                "goalStartHashes": {"app/page.tsx": "a" * 64},
            },
            stage="building",
            goal_id="G-1",
            pi_session_id=f"fomo-{run_id}",
            sandbox_id="sandbox-api-answer",
            lease_token=claimed.lease_owner,
        )

        snapshot = await client.get(f"/v1/projects/{project_id}", headers=owner_headers)
        assert snapshot.status_code == 200
        assert snapshot.json()["pendingInputRequest"]["id"] == request.id
        assert (
            snapshot.json()["activeRun"]["pendingInputRequest"]["question"]
            == "Choose a layout"
        )

        answer_url = f"/v1/runs/{run_id}/input-requests/{request.id}/answer"
        forbidden = await client.post(
            answer_url,
            headers={"X-FOMO-Session": other_id},
            json={"clientMessageId": "answer-1", "answer": "Grid"},
        )
        assert forbidden.status_code == 403

        mismatched_key = await client.post(
            answer_url,
            headers={**owner_headers, "Idempotency-Key": "different"},
            json={"clientMessageId": "answer-1", "answer": "Grid"},
        )
        assert mismatched_key.status_code == 422

        invalid_choice = await client.post(
            answer_url,
            headers={**owner_headers, "Idempotency-Key": "answer-invalid"},
            json={"clientMessageId": "answer-invalid", "answer": "Cards"},
        )
        assert invalid_choice.status_code == 409

        accepted = await client.post(
            answer_url,
            headers={**owner_headers, "Idempotency-Key": "answer-1"},
            json={"clientMessageId": "answer-1", "answer": " Grid "},
        )
        assert accepted.status_code == 202
        assert accepted.json()["run"]["id"] == run_id
        assert accepted.json()["run"]["status"] == "queued"
        assert accepted.json()["message"]["content"] == "Grid"

        duplicate = await client.post(
            answer_url,
            headers={**owner_headers, "Idempotency-Key": "answer-1"},
            json={"clientMessageId": "answer-1", "answer": "Grid"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["message"]["id"] == accepted.json()["message"]["id"]


@pytest.mark.asyncio
async def test_project_snapshot_exposes_authoritative_goal_graph_projection(
    repository, settings
) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = (await client.post("/v1/sessions/guest")).json()["id"]
        headers = {"X-FOMO-Session": session_id}
        project_id = (
            await client.post("/v1/projects", headers=headers, json={"title": "Goals"})
        ).json()["id"]
        run_id = (
            await client.post(
                f"/v1/projects/{project_id}/messages",
                headers={**headers, "Idempotency-Key": "goal-message"},
                json={"clientMessageId": "goal-message", "content": "Build the workflow"},
            )
        ).json()["run"]["id"]
        claimed = await repository.claim_next_run("goal-worker", 60)
        assert claimed is not None and claimed.lease_owner
        lease = claimed.lease_owner
        await repository.create_goal_graph(
            project_id, run_id, _single_goal_draft(), lease_token=lease
        )
        created_event = next(
            event
            for event in await repository.list_events(run_id)
            if event.kind == "goal_graph.created"
        )
        assert created_event.payload["goalGraph"]["goals"][0]["acceptance"][0] == {
            "acceptanceId": "AC-1",
            "title": "Workflow is visible",
            "priority": "must",
            "status": "unverified",
        }
        await repository.activate_goal(run_id, "G-1", lease_token=lease)
        await repository.claim_goal(run_id, "G-1", lease_token=lease)
        await repository.record_verified_checkpoint(
            run_id,
            "G-1",
            [{"path": "app/page.tsx", "content": "export default function Page() {}\n"}],
            [
                {
                    "acceptanceKey": "G-1:AC-1",
                    "kind": "fomo_qa_test",
                    "status": "passed",
                    "summary": "passed",
                }
            ],
            lease_token=lease,
        )

        response = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert response.status_code == 200
        goal_graph = response.json()["goalGraph"]
        assert goal_graph["graphId"] == created_event.payload["goalGraph"]["graphId"]
        assert goal_graph["runId"] == run_id
        assert goal_graph["status"] == "verified"
        assert goal_graph["activeGoalId"] is None
        assert goal_graph["goals"][0]["status"] == "verified"
        assert goal_graph["goals"][0]["checkpointId"] is not None
        assert goal_graph["goals"][0]["evidenceCount"] == 1
        assert goal_graph["goals"][0]["acceptance"][0]["status"] == "passed"


@pytest.mark.asyncio
async def test_versioned_file_edits_restore_download_and_acceptance_projection(repository, settings) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = (await client.post("/v1/sessions/guest")).json()["id"]
        headers = {"X-FOMO-Session": session_id}
        project_id = (
            await client.post("/v1/projects", headers=headers, json={"title": "Library"})
        ).json()["id"]

        first = await client.put(
            f"/v1/projects/{project_id}/files/content",
            headers=headers,
            params={"path": "src/books.ts"},
            json={"baseVersionId": None, "baseSha256": None, "content": "export const books = []\n"},
        )
        assert first.status_code == 200
        first_file = first.json()
        version_one_id = first_file["versionId"]
        assert len(first_file["sha256"]) == 64

        stale_version = await client.put(
            f"/v1/projects/{project_id}/files/content",
            headers=headers,
            params={"path": "src/books.ts"},
            json={"baseVersionId": None, "baseSha256": None, "content": "stale"},
        )
        assert stale_version.status_code == 409

        second = await client.put(
            f"/v1/projects/{project_id}/files/content",
            headers=headers,
            params={"path": "src/books.ts"},
            json={
                "baseVersionId": version_one_id,
                "baseSha256": first_file["sha256"],
                "content": "export const books = ['Dune']\n",
            },
        )
        assert second.status_code == 200
        version_two_id = second.json()["versionId"]

        stale_hash = await client.put(
            f"/v1/projects/{project_id}/files/content",
            headers=headers,
            params={"path": "src/books.ts"},
            json={
                "baseVersionId": version_two_id,
                "baseSha256": "0" * 64,
                "content": "stale hash",
            },
        )
        assert stale_hash.status_code == 409

        restored = await client.post(
            f"/v1/projects/{project_id}/versions/{version_one_id}/restore", headers=headers
        )
        assert restored.status_code == 201
        assert restored.json()["parentVersionId"] == version_two_id
        restored_id = restored.json()["id"]
        restored_content = await client.get(
            f"/v1/projects/{project_id}/files/content",
            headers=headers,
            params={"path": "src/books.ts"},
        )
        assert restored_content.json()["versionId"] == restored_id
        assert restored_content.json()["content"] == "export const books = []\n"

        download = await client.get(
            f"/v1/projects/{project_id}/download",
            headers=headers,
            params={"versionId": version_one_id},
        )
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("application/zip")
        with ZipFile(BytesIO(download.content)) as archive:
            assert archive.namelist() == ["src/books.ts"]
            assert archive.read("src/books.ts") == b"export const books = []\n"

        queued = await client.post(
            f"/v1/projects/{project_id}/messages",
            headers={**headers, "Idempotency-Key": "trace-message"},
            json={"clientMessageId": "trace-message", "content": "Show my library"},
        )
        run_id = queued.json()["run"]["id"]
        await repository.upsert_acceptance_items(
            project_id,
            run_id,
            [{"id": "AC-BOOKS", "given": "a collection", "when": "opened", "then": "books appear"}],
        )
        await repository.append_trace_link(
            run_id,
            "acceptance_criterion",
            "AC-BOOKS",
            "implemented_in",
            "file",
            "src/books.ts",
        )
        diagnostic_artifact_id = await repository.store_artifact(
            run_id,
            "diagnostic_report",
            {"gates": []},
        )
        await repository.record_evidence(
            run_id,
            "AC-BOOKS",
            "playwright_smoke",
            "passed",
            json.dumps(
                {
                    "runId": run_id,
                    "acceptanceId": "AC-BOOKS",
                    "testPath": "tests/generated/books.smoke.spec.ts",
                    "testName": "books appear",
                    "result": "passed",
                    "recordedAt": "2026-08-07T10:00:00Z",
                    "exitCode": 0,
                    "artifactRef": diagnostic_artifact_id,
                },
                separators=(",", ":"),
            ),
            artifact_id=diagnostic_artifact_id,
        )

        trace = await client.get(f"/v1/projects/{project_id}/trace", headers=headers)
        assert trace.status_code == 200
        acceptance_trace = trace.json()["acceptanceTrace"]
        assert acceptance_trace[0]["acceptanceId"] == "AC-BOOKS"
        assert acceptance_trace[0]["status"] == "passed"
        assert acceptance_trace[0]["implementationStatus"] == "implemented"
        assert acceptance_trace[0]["evidence"][0]["kind"] == "playwright_smoke"
        assert acceptance_trace[0]["links"][0]["targetRef"] == "src/books.ts"

        project_snapshot = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert project_snapshot.status_code == 200
        snapshot = project_snapshot.json()
        assert snapshot["activeRun"]["id"] == run_id
        assert snapshot["lastSeq"] == snapshot["events"][-1]["seq"]
        assert snapshot["files"][0]["path"] == "src/books.ts"
        assert [item["number"] for item in snapshot["versions"]] == [3, 2, 1]
        assert snapshot["trace"]["acceptanceTrace"][0]["status"] == "passed"
        assert snapshot["preview"] == {
            "status": "expired",
            "url": None,
            "runId": run_id,
            "verificationStatus": None,
        }


@pytest.mark.asyncio
async def test_preview_endpoint_returns_typed_ready_url_and_requires_project_ownership(
    repository, settings
) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = (await client.post("/v1/sessions/guest")).json()["id"]
        headers = {"X-FOMO-Session": session_id}
        project_id = (
            await client.post("/v1/projects", headers=headers, json={"title": "Library"})
        ).json()["id"]
        run_id = (
            await client.post(
                f"/v1/projects/{project_id}/messages",
                headers={**headers, "Idempotency-Key": "preview-message"},
                json={"clientMessageId": "preview-message", "content": "Serve the library"},
            )
        ).json()["run"]["id"]

        await repository.set_preview_url(run_id, "https://preview.example.test/app")

        preview = await client.get(f"/v1/projects/{project_id}/preview", headers=headers)
        assert preview.status_code == 200
        assert preview.json() == {
            "status": "ready",
            "url": "https://preview.example.test/app",
            "runId": run_id,
            "verificationStatus": "unverified",
        }
        assert preview.headers["content-type"].startswith("application/json")

        # The project snapshot keeps the same typed preview contract.
        snapshot = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert snapshot.json()["preview"] == {
            "status": "ready",
            "url": "https://preview.example.test/app",
            "runId": run_id,
            "verificationStatus": "unverified",
        }

        # Ownership is enforced before any preview data is disclosed.
        other_session_id = (await client.post("/v1/sessions/guest")).json()["id"]
        other_headers = {"X-FOMO-Session": other_session_id}
        forbidden = await client.get(f"/v1/projects/{project_id}/preview", headers=other_headers)
        assert forbidden.status_code == 403
