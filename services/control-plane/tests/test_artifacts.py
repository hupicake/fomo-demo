"""P0-2 visible-artifact contract: display-run selection, newest-per-kind refs,
bounded derived titles, strict detail content, fail-closed 404s and the
lightweight artifact.upserted event.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from fomo.api import create_app
from fomo.persistence.models import ArtifactRecord, SessionRecord
from fomo.schemas import ProjectResponse, RunResponse

PRODUCT_CONTENT: dict[str, object] = {
    "title": "Library product specification",
    "problem": "Readers cannot manage books.",
    "targetUsers": ["librarians", "readers"],
    "userStories": [{"id": "US-1", "story": "Search the catalogue", "priority": "must"}],
    "acceptanceCriteria": [
        {"id": "AC-1", "given": "a query", "when": "submitted", "then": "matches appear"}
    ],
    "pages": [{"route": "/", "purpose": "Catalogue home", "keyElements": ["search"]}],
    "visualDirection": {"tone": "calm", "colors": ["blue"], "references": []},
    "assumptions": ["Readers browse anonymously."],
    "outOfScope": ["Fines."],
}

TECHNICAL_CONTENT: dict[str, object] = {
    "title": "Library technical specification",
    "framework": "Next.js",
    "starterCapabilities": ["crud", "local-persistence"],
    "routes": [{"path": "/books", "rendering": "client", "description": "Catalogue"}],
    "components": [
        {
            "name": "BookTable",
            "responsibility": "List and filter books",
            "children": [],
            "interactionResponsibilities": ["search", "data_table"],
        }
    ],
    "componentDecisions": [
        {"component": "Table", "strategy": "reuse", "source": "radix-ui", "rationale": "Mature primitive"}
    ],
    "featureSurfaces": [
        {
            "componentName": "CatalogSurface",
            "compositionFile": "app/(generated)/composition.tsx",
            "compositionSymbol": "CatalogSurface",
            "compositionResponsibilities": ["compose"],
            "modules": [
                {"role": "data_table", "filePath": "components/features/books.tsx", "publicSymbol": "BooksTable"}
            ],
        }
    ],
    "stateModel": [
        {
            "name": "loanStore",
            "owner": "engineer",
            "persistence": "localStorage",
            "stateClass": "persistent_business",
            "mutableDomains": ["loans"],
        }
    ],
    "persistentStateDomains": [
        {"domain": "loans", "stateModelName": "loanStore", "actionsStoreFile": "lib/loans.ts"}
    ],
    "stateAggregation": {
        "filePath": "app/(generated)/composition.tsx",
        "responsibilities": ["compose persistent state"],
    },
    "dependencies": [{"name": "zod", "reason": "validation"}],
    "filePlan": [{"path": "components/features/books.tsx", "operation": "create", "reason": "Catalogue table"}],
    "testPlan": [{"acceptanceId": "AC-1", "method": "playwright", "steps": ["Search a book"]}],
    "risks": ["Concurrent loans."],
}


async def _project_with_run(
    repository, *, message_id: str
) -> tuple[SessionRecord, ProjectResponse, RunResponse]:
    session = await repository.create_guest_session()
    project = await repository.create_project(session.id, "Library")
    _message, run, _created = await repository.create_message_and_run(
        project.id, session.id, message_id, "Build the library", None
    )
    return session, project, run


async def _seed_artifact(
    repository,
    run_id: str,
    kind: str,
    content: dict[str, object],
    *,
    artifact_id: str,
    created_at: datetime,
) -> str:
    """Direct deterministic seeding for ordering and tie-break assertions."""
    async with repository.database.session_factory() as session:
        session.add(
            ArtifactRecord(
                id=artifact_id,
                run_id=run_id,
                kind=kind,
                schema_version=1,
                content=content,
                created_at=created_at,
            )
        )
        await session.commit()
    return artifact_id


def _long_title() -> str:
    # Collapses to a 719-char string, so the ref title must be truncated to
    # at most 120 chars including the ellipsis.
    return "  ".join(["Alpha"] * 120)


@pytest.mark.asyncio
async def test_snapshot_refs_use_display_run_newest_per_kind_and_canonical_order(
    repository, settings
) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session, project, run = await _project_with_run(repository, message_id="msg-refs")
        headers = {"X-FOMO-Session": session.id}

        _product_old = await _seed_artifact(
            repository,
            run.id,
            "product_spec",
            {"title": "Old product", "problem": "Old problem"},
            artifact_id="artifact-product-old",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        product_new = await _seed_artifact(
            repository,
            run.id,
            "product_spec",
            {"title": _long_title(), "problem": "  Readers\ncannot\nmanage books.  "},
            artifact_id="artifact-product-new",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        technical = await _seed_artifact(
            repository,
            run.id,
            "technical_spec",
            {"title": "Tech", "framework": "Next.js"},
            artifact_id="artifact-technical",
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
        hidden = await _seed_artifact(
            repository,
            run.id,
            "implementation_plan",
            {"batches": []},
            artifact_id="artifact-hidden",
            created_at=datetime(2026, 1, 4, tzinfo=UTC),
        )

        snapshot = (await client.get(f"/v1/projects/{project.id}", headers=headers)).json()
        refs = snapshot["artifactRefs"]

        # Canonical Product then Architect order, newest per kind, never content.
        assert [ref["kind"] for ref in refs] == ["product_spec", "technical_spec"]
        assert [ref["id"] for ref in refs] == [product_new, technical]
        assert hidden not in [ref["id"] for ref in refs]
        for ref in refs:
            assert set(ref) == {
                "id",
                "runId",
                "kind",
                "role",
                "schemaVersion",
                "title",
                "summary",
                "createdAt",
            }

        product_ref = refs[0]
        assert product_ref["runId"] == run.id
        assert product_ref["role"] == "product_manager"
        assert product_ref["schemaVersion"] == 1
        assert product_ref["createdAt"] is not None
        # Deterministic bounded title/summary; the stored content is untouched.
        assert product_ref["title"].endswith("…")
        assert len(product_ref["title"]) <= 120
        assert product_ref["title"] == " ".join(["Alpha"] * 20)
        assert product_ref["summary"] == "Readers cannot manage books."

        assert refs[1]["id"] == technical
        assert refs[1]["role"] == "architect"
        assert refs[1]["title"] == "Tech"
        assert refs[1]["summary"] == "Next.js"

        # The ref never carries content, even when the detail endpoint does.
        detail = (
            await client.get(f"/v1/runs/{run.id}/artifacts/{product_new}", headers=headers)
        ).json()
        assert "content" not in product_ref
        assert detail["content"]["title"] == _long_title()


@pytest.mark.asyncio
async def test_snapshot_newest_per_kind_tie_breaks_by_id_desc(repository, settings) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session, project, run = await _project_with_run(repository, message_id="msg-tie")
        headers = {"X-FOMO-Session": session.id}
        instant = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        for artifact_id in ("artifact-aaa", "artifact-bbb", "artifact-ccc"):
            await _seed_artifact(
                repository,
                run.id,
                "product_spec",
                {"title": artifact_id, "problem": "problem"},
                artifact_id=artifact_id,
                created_at=instant,
            )

        snapshot = (await client.get(f"/v1/projects/{project.id}", headers=headers)).json()
        refs = snapshot["artifactRefs"]

        # One newest record per kind: the same-instant tie is broken by id DESC.
        assert [ref["id"] for ref in refs] == ["artifact-ccc"]


@pytest.mark.asyncio
async def test_snapshot_refs_track_the_display_run_including_terminal(repository, settings) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session, project, run_one = await _project_with_run(repository, message_id="msg-one")
        headers = {"X-FOMO-Session": session.id}
        await _seed_artifact(
            repository,
            run_one.id,
            "product_spec",
            {"title": "Run one product", "problem": "old"},
            artifact_id="artifact-run-one",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        _message, run_two, _created = await repository.create_message_and_run(
            project.id, session.id, "msg-two", "Next request", None
        )
        technical = await _seed_artifact(
            repository,
            run_two.id,
            "technical_spec",
            {"title": "Run two technical", "framework": "Next.js"},
            artifact_id="artifact-run-two",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

        # The active run is the display run; run one's product ref must not leak.
        active_snapshot = (await client.get(f"/v1/projects/{project.id}", headers=headers)).json()
        assert active_snapshot["activeRun"]["id"] == run_two.id
        assert [ref["id"] for ref in active_snapshot["artifactRefs"]] == [technical]

        # A terminal display run (active run cleared) still yields its refs.
        cancelled = await client.post(f"/v1/runs/{run_two.id}/cancel", headers=headers)
        assert cancelled.status_code == 200
        terminal_snapshot = (await client.get(f"/v1/projects/{project.id}", headers=headers)).json()
        assert terminal_snapshot["activeRun"] is None
        assert [ref["id"] for ref in terminal_snapshot["artifactRefs"]] == [technical]
        assert terminal_snapshot["artifactRefs"][0]["runId"] == run_two.id


@pytest.mark.asyncio
async def test_artifact_detail_returns_exact_strict_content(repository, settings) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session, _project, run = await _project_with_run(repository, message_id="msg-detail")
        headers = {"X-FOMO-Session": session.id}
        product_id = await repository.store_artifact(
            run.id, "product_spec", dict(PRODUCT_CONTENT), role="product_manager"
        )
        technical_id = await repository.store_artifact(
            run.id, "technical_spec", dict(TECHNICAL_CONTENT), role="architect"
        )

        product = (
            await client.get(f"/v1/runs/{run.id}/artifacts/{product_id}", headers=headers)
        ).json()
        assert product["id"] == product_id
        assert product["runId"] == run.id
        assert product["kind"] == "product_spec"
        assert product["role"] == "product_manager"
        assert product["schemaVersion"] == 1
        assert product["title"] == PRODUCT_CONTENT["title"]
        assert product["summary"] == PRODUCT_CONTENT["problem"]
        assert product["content"] == PRODUCT_CONTENT
        assert isinstance(product["content"], dict)

        technical = (
            await client.get(f"/v1/runs/{run.id}/artifacts/{technical_id}", headers=headers)
        ).json()
        assert technical["kind"] == "technical_spec"
        assert technical["role"] == "architect"
        assert technical["summary"] == TECHNICAL_CONTENT["framework"]
        assert technical["content"] == TECHNICAL_CONTENT


@pytest.mark.asyncio
async def test_artifact_detail_fails_closed_with_non_disclosing_404(repository, settings) -> None:
    app = create_app(settings, repository)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_a, project_a, run_a = await _project_with_run(repository, message_id="msg-a")
        headers_a = {"X-FOMO-Session": session_a.id}
        artifact_a = await repository.store_artifact(
            run_a.id, "product_spec", dict(PRODUCT_CONTENT), role="product_manager"
        )
        hidden = await repository.store_artifact(
            run_a.id, "implementation_plan", {"batches": []}, role="engineer"
        )
        session_b, project_b, run_b = await _project_with_run(repository, message_id="msg-b")
        headers_b = {"X-FOMO-Session": session_b.id}
        artifact_b = await repository.store_artifact(
            run_b.id, "technical_spec", dict(TECHNICAL_CONTENT), role="architect"
        )
        assert project_a.id != project_b.id

        cases = [
            (f"/v1/runs/{run_a.id}/artifacts/unknown-artifact", headers_a),
            (f"/v1/runs/unknown-run/artifacts/{artifact_a}", headers_a),
            (f"/v1/runs/{run_b.id}/artifacts/{artifact_a}", headers_a),
            (f"/v1/runs/{run_a.id}/artifacts/{artifact_b}", headers_b),
            (f"/v1/runs/{run_a.id}/artifacts/{artifact_a}", headers_b),
            (f"/v1/runs/{run_a.id}/artifacts/{hidden}", headers_a),
        ]
        for path, request_headers in cases:
            response = await client.get(path, headers=request_headers)
            assert response.status_code == 404, path
            body = response.json()
            assert body["title"] == "Not Found"
            assert body["detail"] == "artifact not found"
            assert body["status"] == 404
            assert "content" not in body


@pytest.mark.asyncio
async def test_artifact_upserted_event_stays_lightweight(repository) -> None:
    session, _project, run = await _project_with_run(repository, message_id="msg-event")
    artifact_id = await repository.store_artifact(
        run.id, "product_spec", dict(PRODUCT_CONTENT), role="product_manager"
    )

    events = await repository.list_events(run.id)
    upserted = next(event for event in events if event.kind == "artifact.upserted")
    assert upserted.payload == {"artifactId": artifact_id, "kind": "product_spec"}
    assert upserted.role == "product_manager"
    assert "content" not in upserted.payload
    assert "title" not in upserted.payload
    assert "summary" not in upserted.payload
