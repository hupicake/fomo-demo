from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest

from fomo.api import create_app


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
        assert snapshot_payload["trace"]["acceptanceTrace"] == []
        assert snapshot_payload["preview"] == {"status": "unavailable", "url": None, "runId": None}
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
        await repository.record_evidence(
            run_id,
            "AC-BOOKS",
            "qa_gates",
            "passed",
            "smoke test passed",
        )

        trace = await client.get(f"/v1/projects/{project_id}/trace", headers=headers)
        assert trace.status_code == 200
        acceptance_trace = trace.json()["acceptanceTrace"]
        assert acceptance_trace[0]["acceptanceId"] == "AC-BOOKS"
        assert acceptance_trace[0]["status"] == "passed"
        assert acceptance_trace[0]["links"][0]["targetRef"] == "src/books.ts"

        project_snapshot = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert project_snapshot.status_code == 200
        snapshot = project_snapshot.json()
        assert snapshot["activeRun"]["id"] == run_id
        assert snapshot["lastSeq"] == snapshot["events"][-1]["seq"]
        assert snapshot["files"][0]["path"] == "src/books.ts"
        assert [item["number"] for item in snapshot["versions"]] == [3, 2, 1]
        assert snapshot["trace"]["acceptanceTrace"][0]["status"] == "passed"
        assert snapshot["preview"] == {"status": "expired", "url": None, "runId": run_id}


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
        }
        assert preview.headers["content-type"].startswith("application/json")

        # The project snapshot keeps the same typed preview contract.
        snapshot = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert snapshot.json()["preview"] == {
            "status": "ready",
            "url": "https://preview.example.test/app",
            "runId": run_id,
        }

        # Ownership is enforced before any preview data is disclosed.
        other_session_id = (await client.post("/v1/sessions/guest")).json()["id"]
        other_headers = {"X-FOMO-Session": other_session_id}
        forbidden = await client.get(f"/v1/projects/{project_id}/preview", headers=other_headers)
        assert forbidden.status_code == 403
