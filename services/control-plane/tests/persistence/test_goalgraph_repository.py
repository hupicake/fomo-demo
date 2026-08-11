from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from fomo.direct_pi.architecture_profile import derive_architecture_profile
from fomo.direct_pi.goalgraph import (
    GoalStatus,
    GraphStatus,
    NavigationMode,
    parse_goal_graph_draft,
    parse_legacy_goal_graph_draft,
)
from fomo.ids import utcnow
from fomo.persistence import ConflictError, ManifestIntegrityError, RunLeaseLost
from fomo.persistence.models import (
    CheckpointFileRecord,
    CheckpointRecord,
    GoalGraphRevisionRecord,
    GoalNodeRecord,
    ProjectRecord,
    RunEventRecord,
    RunRecord,
    TraceLinkRecord,
    VersionRecord,
)
from fomo.schemas import RunStatus
from tests.helpers import create_user_session


def _acceptance(identifier: str) -> dict[str, Any]:
    return {
        "criteria": [
            {
                "id": identifier,
                "title": f"Verify {identifier}",
                "priority": "must",
                "given": "the application is open",
                "when": "the workflow runs",
                "then": "the outcome is visible",
            }
        ],
        "tests": [
            {
                "id": f"T-{identifier}",
                "acceptanceId": identifier,
                "title": f"Test {identifier}",
                "actions": [{"kind": "goto", "path": "/"}],
                "assertions": [
                    {
                        "kind": "visible",
                        "target": {"by": "role", "value": "main", "name": "Application"},
                    }
                ],
            }
        ],
    }


def _draft():
    return parse_legacy_goal_graph_draft(
        {
            "schemaVersion": 1,
            "productOutcome": "A verified two-step product",
            "goals": [
                {
                    "goalId": "G-1",
                    "title": "Foundation",
                    "productOutcome": "The foundation works",
                    "userVisible": True,
                    "dependsOn": [],
                    "acceptance": _acceptance("AC-1"),
                },
                {
                    "goalId": "G-2",
                    "title": "Experience",
                    "productOutcome": "The experience works",
                    "userVisible": True,
                    "dependsOn": ["G-1"],
                    "acceptance": _acceptance("AC-2"),
                },
            ],
        }
    )


def _v2_draft():
    first_acceptance = _acceptance("AC-route-root")
    first_acceptance["tests"][0] = {
        "id": "T-route-root",
        "acceptanceId": "AC-route-root",
        "title": "opens the home route directly",
        "actions": [
            {"kind": "goto", "path": "/"},
            {"kind": "reload", "target": None},
        ],
        "assertions": [
            {"kind": "url", "path": "/"},
            {
                "kind": "visible",
                "target": {"by": "role", "value": "heading", "name": "Home"},
            },
        ],
    }
    second_acceptance = {
        "criteria": [
            {
                "id": "AC-route-missions-direct",
                "title": "Open missions directly",
                "priority": "must",
                "given": "the product is available",
                "when": "missions is opened directly",
                "then": "the route identity is visible",
            },
            {
                "id": "AC-route-missions-link",
                "title": "Navigate to missions",
                "priority": "must",
                "given": "home is open",
                "when": "the Missions link is followed",
                "then": "the target route identity is visible",
            },
        ],
        "tests": [
            {
                "id": "T-route-missions-direct",
                "acceptanceId": "AC-route-missions-direct",
                "title": "opens missions directly",
                "actions": [
                    {"kind": "goto", "path": "/missions"},
                    {"kind": "reload", "target": None},
                ],
                "assertions": [
                    {"kind": "url", "path": "/missions"},
                    {
                        "kind": "visible",
                        "target": {
                            "by": "role",
                            "value": "heading",
                            "name": "Missions",
                        },
                    },
                ],
            },
            {
                "id": "T-route-missions-link",
                "acceptanceId": "AC-route-missions-link",
                "title": "navigates to missions",
                "actions": [
                    {"kind": "goto", "path": "/"},
                    {
                        "kind": "click",
                        "target": {
                            "by": "role",
                            "value": "link",
                            "name": "Missions",
                        },
                    },
                ],
                "assertions": [
                    {"kind": "url", "path": "/missions"},
                    {
                        "kind": "visible",
                        "target": {
                            "by": "role",
                            "value": "heading",
                            "name": "Missions",
                        },
                    },
                ],
            },
        ],
    }
    return parse_goal_graph_draft(
        {
            "schemaVersion": 2,
            "productOutcome": "A durable routed mission product",
            "routes": [
                {
                    "path": "/",
                    "title": "Home",
                    "owningGoalId": "G-1",
                    "deepLinkable": True,
                },
                {
                    "path": "/missions",
                    "title": "Missions",
                    "owningGoalId": "G-2",
                    "deepLinkable": True,
                },
            ],
            "goals": [
                {
                    "goalId": "G-1",
                    "title": "Home route",
                    "productOutcome": "Users open mission control.",
                    "userVisible": True,
                    "dependsOn": [],
                    "acceptance": first_acceptance,
                },
                {
                    "goalId": "G-2",
                    "title": "Mission route",
                    "productOutcome": "Users navigate to missions.",
                    "userVisible": True,
                    "dependsOn": ["G-1"],
                    "acceptance": second_acceptance,
                },
            ],
        }
    )


async def _running_context(repository, suffix: str = "one"):
    owner = await create_user_session(repository)
    project = await repository.create_project(owner.id, f"Project {suffix}")
    _message, run, _created = await repository.create_message_and_run(
        project.id, owner.id, f"message-{suffix}", "Build it"
    )
    claimed = await repository.claim_next_run(f"worker-{suffix}", 120)
    assert claimed is not None and claimed.id == run.id and claimed.lease_owner
    return project, run, claimed.lease_owner


@pytest.mark.asyncio
async def test_dag_projection_and_p0_none(repository) -> None:
    project, run, lease = await _running_context(repository)
    assert await repository.get_goal_graph_for_run(run.id) is None
    created = await repository._create_legacy_goal_graph(
        project.id, run.id, _draft(), lease_token=lease
    )
    assert created.graph.status is GraphStatus.ACTIVE
    assert [goal.status for goal in created.graph.goals] == [
        GoalStatus.PENDING,
        GoalStatus.PENDING,
    ]
    activated = await repository.activate_goal(run.id, "G-1", lease_token=lease)
    assert activated.graph.goals[0].status is GoalStatus.ACTIVE
    claimed = await repository.claim_goal(run.id, "G-1", lease_token=lease)
    assert claimed.graph.goals[0].status is GoalStatus.CLAIMED
    restored = await repository.get_goal_graph(run.id)
    assert restored is not None and restored.graph == claimed.graph


@pytest.mark.asyncio
async def test_legacy_write_requires_private_admission_and_preserves_safe_old_paths(
    repository,
) -> None:
    project, run, lease = await _running_context(repository, "legacy-admission")
    draft = _draft()
    with pytest.raises(ValueError, match="only accepts current schema v2"):
        await repository.create_goal_graph(project.id, run.id, draft, lease_token=lease)

    payload = draft.model_dump(mode="json", by_alias=True)
    for goal in payload["goals"]:
        test = goal["acceptance"]["tests"][0]
        test["actions"][0]["path"] = "/legacy/"
    legacy = parse_legacy_goal_graph_draft(payload)
    created = await repository._create_legacy_goal_graph(
        project.id, run.id, legacy, lease_token=lease
    )
    restored = await repository.get_goal_graph(run.id)

    assert created.graph.schema_version == 1
    assert restored is not None
    assert restored.graph.goals[0].acceptance.tests[0].actions[0].path == "/legacy/"


@pytest.mark.asyncio
async def test_v2_route_manifest_round_trips_and_is_integrity_checked(repository) -> None:
    project, run, lease = await _running_context(repository, "routes")
    profile = derive_architecture_profile(
        route_count=2,
        goal_count=2,
        shared_state_across_routes=True,
    )
    with pytest.raises(ValueError, match="requires an architecture profile"):
        await repository.create_goal_graph(
            project.id,
            run.id,
            _v2_draft(),
            lease_token=lease,
        )
    created = await repository.create_goal_graph(
        project.id,
        run.id,
        _v2_draft(),
        architecture_profile=profile,
        provenance={
            "createdBy": "routing-test",
            "_fomoGoalGraphRouting": {
                "navigationMode": "single_surface",
                "routes": [],
            },
            "_fomoArchitectureProfile": {"id": "caller-controlled"},
        },
        lease_token=lease,
    )

    assert created.graph.navigation_mode is NavigationMode.MULTI_ROUTE
    assert created.architecture_profile == profile
    assert [route.path for route in created.graph.routes] == ["/", "/missions"]
    created_event = next(
        event
        for event in await repository.list_events(run.id)
        if event.kind == "goal_graph.created"
    )
    read_projection = created_event.payload["goalGraph"]
    assert read_projection["schemaVersion"] == 2
    assert read_projection["navigationMode"] == "multi_route"
    assert read_projection["routes"] == [
        {
            "path": "/",
            "title": "Home",
            "owningGoalId": "G-1",
            "deepLinkable": True,
        },
        {
            "path": "/missions",
            "title": "Missions",
            "owningGoalId": "G-2",
            "deepLinkable": True,
        },
    ]
    restored = await repository.get_goal_graph(run.id)
    assert restored is not None and restored.graph == created.graph
    assert restored.architecture_profile == profile

    async with repository.database.session_factory() as session:
        revision = await session.scalar(
            select(GoalGraphRevisionRecord).where(
                GoalGraphRevisionRecord.graph_id == created.graph_id
            )
        )
        assert revision is not None
        provenance = dict(revision.provenance)
        routing = dict(provenance["_fomoGoalGraphRouting"])
        assert routing["navigationMode"] == "multi_route"
        routes = [dict(route) for route in routing["routes"]]
        routes.reverse()
        routing["routes"] = routes
        provenance["_fomoGoalGraphRouting"] = routing
        revision.provenance = provenance
        await session.commit()

    with pytest.raises(ManifestIntegrityError, match="content hash mismatch"):
        await repository.get_goal_graph(run.id)


@pytest.mark.asyncio
async def test_v2_architecture_profile_provenance_rejects_tampering(repository) -> None:
    project, run, lease = await _running_context(repository, "architecture-profile")
    profile = derive_architecture_profile(route_count=2, goal_count=2)
    created = await repository.create_goal_graph(
        project.id,
        run.id,
        _v2_draft(),
        architecture_profile=profile,
        lease_token=lease,
    )

    async with repository.database.session_factory() as session:
        revision = await session.scalar(
            select(GoalGraphRevisionRecord).where(
                GoalGraphRevisionRecord.graph_id == created.graph_id
            )
        )
        assert revision is not None
        provenance = dict(revision.provenance)
        replacement = derive_architecture_profile(
            route_count=2,
            goal_count=2,
            shared_state_across_routes=True,
        )
        provenance["_fomoArchitectureProfile"] = replacement.as_prompt_context()
        revision.provenance = provenance
        await session.commit()

    with pytest.raises(ManifestIntegrityError, match="content hash mismatch"):
        await repository.get_goal_graph(run.id)


@pytest.mark.asyncio
async def test_verified_checkpoint_advances_next_goal_and_detects_tampering(repository) -> None:
    project, run, lease = await _running_context(repository)
    await repository._create_legacy_goal_graph(
        project.id, run.id, _draft(), lease_token=lease
    )
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    checkpoint = await repository.record_verified_checkpoint(
        run.id,
        "G-1",
        [{"path": "app/page.tsx", "content": "export default () => <main>你好</main>\n"}],
        [
            {
                "acceptanceKey": "G-1:AC-1",
                "kind": "fomo_qa_test",
                "status": "passed",
                "summary": "passed",
            }
        ],
        lease_token=lease,
        capsule={"nextGoal": "G-2"},
    )
    assert checkpoint.files[0].size == len(checkpoint.files[0].content_text.encode("utf-8"))
    projection = await repository.get_goal_graph(run.id)
    assert projection is not None
    assert [goal.status for goal in projection.graph.goals] == [
        GoalStatus.VERIFIED,
        GoalStatus.ACTIVE,
    ]
    restored = await repository.get_latest_verified_checkpoint(run.id)
    assert restored == checkpoint
    verified_event = (await repository.list_events(run.id))[-1]
    assert verified_event.kind == "goal.verified"
    authoritative = verified_event.payload["goalGraph"]
    assert authoritative["activeGoalId"] == "G-2"
    assert authoritative["goals"][0]["checkpointId"] == checkpoint.id
    assert authoritative["goals"][0]["evidenceCount"] == 1
    assert authoritative["goals"][0]["acceptance"][0]["status"] == "passed"
    assert authoritative["goals"][1]["acceptance"][0]["status"] == "unverified"

    async with repository.database.session_factory() as session:
        await session.execute(
            update(CheckpointFileRecord)
            .where(CheckpointFileRecord.checkpoint_id == checkpoint.id)
            .values(content_text="tampered")
        )
        await session.commit()
    with pytest.raises(ManifestIntegrityError):
        await repository.get_latest_verified_checkpoint(run.id)


@pytest.mark.asyncio
async def test_legacy_verified_goal_trace_is_inferred_read_only_from_checkpoint_capsule(
    repository,
) -> None:
    project, run, lease = await _running_context(repository, "legacy-trace")
    await repository._create_legacy_goal_graph(
        project.id, run.id, _draft(), lease_token=lease
    )
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    first_files = [
        {"path": "components/library.tsx", "content": "export const Library = 1\n"},
        {"path": "tests/library.spec.ts", "content": "test('library', () => {})\n"},
        {"path": "AGENTS.md", "content": "agent instructions\n"},
        {"path": "README.md", "content": "project documentation\n"},
        {"path": "docs/architecture.md", "content": "architecture documentation\n"},
        {"path": "next.config.ts", "content": "export default {}\n"},
    ]
    await repository.record_verified_checkpoint(
        run.id,
        "G-1",
        first_files,
        [{"acceptanceKey": "G-1:AC-1", "kind": "fomo_qa_test", "status": "passed"}],
        lease_token=lease,
        capsule={"goalChangedPathsByGoal": {"G-1": [item["path"] for item in first_files]}},
    )
    await repository.claim_goal(run.id, "G-2", lease_token=lease)
    final_files = [
        *first_files,
        {"path": "lib/books.ts", "content": "export const books = []\n"},
        {"path": "CLAUDE.md", "content": "agent instructions\n"},
    ]
    await repository.record_verified_checkpoint(
        run.id,
        "G-2",
        final_files,
        [{"acceptanceKey": "G-2:AC-2", "kind": "fomo_qa_test", "status": "passed"}],
        lease_token=lease,
        capsule={
            "goalChangedPathsByGoal": {
                "G-1": [item["path"] for item in first_files],
                "G-2": ["lib/books.ts", "CLAUDE.md"],
            }
        },
    )
    await repository.upsert_acceptance_items(
        project.id,
        run.id,
        [
            {"id": "G-1:AC-1", "title": "Foundation", "priority": "must"},
            {"id": "G-2:AC-2", "title": "Experience", "priority": "must"},
        ],
        lease_token=lease,
    )
    await repository.mark_terminal(run.id, RunStatus.succeeded, lease_token=lease)
    async with repository.database.session_factory() as session:
        await session.execute(delete(TraceLinkRecord).where(TraceLinkRecord.run_id == run.id))
        await session.commit()

    trace = await repository.get_trace(project.id, run.id)
    assert {
        (link["sourceRef"], link["targetRef"])
        for link in trace["links"]
        if link["relation"] == "implemented_in"
    } == {
        ("G-1:AC-1", "components/library.tsx"),
        ("G-2:AC-2", "lib/books.ts"),
    }
    assert all(item["implementationStatus"] == "implemented" for item in trace["acceptance_trace"])
    async with repository.database.session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(TraceLinkRecord).where(
                    TraceLinkRecord.run_id == run.id
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_lost_lease_rolls_back_checkpoint_state_and_events(repository) -> None:
    project, run, lease = await _running_context(repository)
    await repository._create_legacy_goal_graph(
        project.id, run.id, _draft(), lease_token=lease
    )
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)
    async with repository.database.session_factory() as session:
        await session.execute(
            update(RunRecord)
            .where(RunRecord.id == run.id)
            .values(lease_expires_at=utcnow() - timedelta(seconds=1))
        )
        before_events = await session.scalar(
            select(func.count()).select_from(RunEventRecord).where(RunEventRecord.run_id == run.id)
        )
        await session.commit()
    with pytest.raises(RunLeaseLost):
        await repository.record_verified_checkpoint(
            run.id,
            "G-1",
            [{"path": "app/page.tsx", "content": "complete"}],
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
    async with repository.database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CheckpointRecord)) == 0
        node = await session.scalar(
            select(GoalNodeRecord).where(GoalNodeRecord.run_id == run.id, GoalNodeRecord.goal_key == "G-1")
        )
        assert node is not None and node.status == GoalStatus.CLAIMED.value
        after_events = await session.scalar(
            select(func.count()).select_from(RunEventRecord).where(RunEventRecord.run_id == run.id)
        )
        assert after_events == before_events


@pytest.mark.asyncio
async def test_usage_request_id_is_idempotent_for_p0_run(repository) -> None:
    _project, run, lease = await _running_context(repository)
    first = await repository.record_usage_entry(
        run.id,
        "provider-request-1",
        lease_token=lease,
        provider="test",
        model="model",
        input_tokens=10,
        output_tokens=4,
        cost_micros=25,
    )
    second = await repository.record_usage_entry(
        run.id,
        "provider-request-1",
        lease_token=lease,
        provider="test",
        model="model",
        input_tokens=999,
    )
    assert first.created is True
    assert second.created is False
    assert first.entry_id == second.entry_id
    with pytest.raises(RunLeaseLost):
        await repository.record_usage_entry(
            run.id,
            "provider-request-stale",
            lease_token=f"{lease}-stale",
            provider="test",
            model="model",
            input_tokens=500,
        )
    totals = await repository.get_usage_totals(run.id)
    assert totals.input_tokens == 10
    assert totals.output_tokens == 4
    assert totals.cost_micros == 25


@pytest.mark.asyncio
async def test_partial_unique_index_allows_only_one_project_current_goal(repository) -> None:
    project, run, lease = await _running_context(repository, "first")
    await repository._create_legacy_goal_graph(
        project.id, run.id, _draft(), lease_token=lease
    )
    await repository.activate_goal(run.id, "G-1", lease_token=lease)

    with pytest.raises(ConflictError, match="active goal"):
        await repository.activate_goal(run.id, "G-2", lease_token=lease)

    async with repository.database.session_factory() as session:
        second_node = await session.scalar(
            select(GoalNodeRecord).where(
                GoalNodeRecord.run_id == run.id,
                GoalNodeRecord.goal_key == "G-2",
            )
        )
        assert second_node is not None
        second_node.status = GoalStatus.ACTIVE.value
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


def test_manifest_hash_is_stable_across_file_order() -> None:
    from fomo.persistence import Repository

    first, first_hash = Repository._normalize_checkpoint_files(
        [
            {"path": "b.ts", "content": "β"},
            {"path": "a.ts", "content": "alpha"},
        ]
    )
    second, second_hash = Repository._normalize_checkpoint_files(
        [
            {"path": "a.ts", "content": "alpha"},
            {"path": "b.ts", "content": "β"},
        ]
    )
    assert first_hash == second_hash
    assert [item.path for item in first] == [item.path for item in second] == ["a.ts", "b.ts"]


@pytest.mark.asyncio
async def test_graph_terminalization_releases_current_goal_and_emits_ui_event(repository) -> None:
    project, run, lease = await _running_context(repository, "terminalize")
    await repository._create_legacy_goal_graph(
        project.id, run.id, _draft(), lease_token=lease
    )
    await repository.activate_goal(run.id, "G-1", lease_token=lease)
    await repository.claim_goal(run.id, "G-1", lease_token=lease)

    projection = await repository.terminalize_goal_graph(
        run.id,
        GraphStatus.CANCELLED,
        reason="user cancelled",
        lease_token=lease,
    )
    assert projection.graph.status is GraphStatus.CANCELLED
    assert [goal.status for goal in projection.graph.goals] == [
        GoalStatus.SUPERSEDED,
        GoalStatus.SUPERSEDED,
    ]
    event = (await repository.list_events(run.id))[-1]
    assert event.kind == "goal_graph.cancelled"
    assert event.payload["goalGraph"]["status"] == "cancelled"
    assert [item["status"] for item in event.payload["goalGraph"]["goals"]] == [
        "superseded",
        "superseded",
    ]


@pytest.mark.asyncio
async def test_scoped_legacy_evidence_event_exposes_local_and_durable_keys(repository) -> None:
    _project, run, lease = await _running_context(repository, "scoped-evidence")
    await repository.record_evidence(
        run.id,
        "G-1:AC-1",
        "fomo_qa_test",
        "passed",
        "passed",
        lease_token=lease,
    )
    event = (await repository.list_events(run.id))[-1]
    assert event.payload["acceptanceId"] == "AC-1"
    assert event.payload["acceptanceKey"] == "G-1:AC-1"
    assert event.payload["goalId"] == "G-1"


@pytest.mark.asyncio
async def test_finalize_verified_publish_is_atomic_idempotent_and_cancel_fenced(
    repository,
) -> None:
    project, run, lease = await _running_context(repository, "atomic-publish")
    content = "export default function Page() { return <main>ready</main> }\n"
    files = [
        {
            "path": "app/page.tsx",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "size": len(content.encode()),
            "mime": "text/typescript",
            "content_text": content,
        }
    ]
    version = await repository.finalize_verified_publish(
        run.id,
        commit_sha="a" * 40,
        files=files,
        product_title="Atomic product",
        acceptance_items=(("G-1:AC-1", "G-1"),),
        preview_url="https://preview.invalid",
        preview_elapsed_seconds=1.2,
        lease_token=lease,
    )
    retry = await repository.finalize_verified_publish(
        run.id,
        commit_sha="a" * 40,
        files=files,
        product_title="Atomic product",
        acceptance_items=(("G-1:AC-1", "G-1"),),
        preview_url="https://preview.invalid",
        preview_elapsed_seconds=1.2,
        lease_token=lease,
    )
    assert retry.id == version.id
    assert (await repository.get_run(run.id)).status.value == "succeeded"
    async with repository.database.session_factory() as session:
        durable_project = await session.get(ProjectRecord, project.id)
        assert durable_project is not None and durable_project.head_version_id == version.id
        assert (
            await session.scalar(
                select(func.count()).select_from(VersionRecord).where(
                    VersionRecord.project_id == project.id
                )
            )
            == 1
        )
        trace = await session.scalar(
            select(TraceLinkRecord).where(TraceLinkRecord.run_id == run.id)
        )
        assert trace is not None
        assert trace.source_ref == "G-1:AC-1"
        assert trace.target_ref == version.id
        assert trace.metadata_json == {"goalId": "G-1"}

    cancelled_project, cancelled_run, cancelled_lease = await _running_context(
        repository, "cancelled-publish"
    )
    await repository.request_cancel(cancelled_run.id)
    with pytest.raises(RunLeaseLost):
        await repository.finalize_verified_publish(
            cancelled_run.id,
            commit_sha="b" * 40,
            files=files,
            product_title="Must not publish",
            acceptance_items=(),
            preview_url=None,
            preview_elapsed_seconds=None,
            lease_token=cancelled_lease,
        )
    async with repository.database.session_factory() as session:
        durable_project = await session.get(ProjectRecord, cancelled_project.id)
        assert durable_project is not None and durable_project.head_version_id is None
        assert (
            await session.scalar(
                select(func.count()).select_from(VersionRecord).where(
                    VersionRecord.project_id == cancelled_project.id
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_usage_reservation_settles_after_lease_loss_idempotently(repository) -> None:
    _project, run, lease = await _running_context(repository, "usage-settlement")
    token = await repository.reserve_usage_entry(
        run.id,
        "provider-request-reserved",
        lease_token=lease,
        provider="provider",
        model="model",
        metadata={"stage": "building"},
    )
    async with repository.database.session_factory() as session:
        await session.execute(
            update(RunRecord)
            .where(RunRecord.id == run.id)
            .values(lease_expires_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()
    first = await repository.settle_usage_entry(
        run.id,
        "provider-request-reserved",
        usage_token=token,
        input_tokens=11,
        output_tokens=7,
        tool_calls=2,
        cost_micros=50,
    )
    retry = await repository.settle_usage_entry(
        run.id,
        "provider-request-reserved",
        usage_token=token,
        input_tokens=11,
        output_tokens=7,
        tool_calls=2,
        cost_micros=50,
    )
    assert first.created is True
    assert retry.created is False
    totals = await repository.get_usage_totals(run.id)
    assert totals.input_tokens == 11
    assert totals.output_tokens == 7
    assert totals.tool_calls == 2
    assert totals.cost_micros == 50
