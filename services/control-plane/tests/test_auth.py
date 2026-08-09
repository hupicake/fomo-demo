from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from fomo.api import create_app
from fomo.api.app import _stream_run_events
from fomo.persistence.models import ProjectRecord, SessionRecord, UserRecord


def _transport(repository, settings) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=create_app(settings, repository))


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_register_login_me_wrong_credentials_and_logout(repository, settings) -> None:
    transport = _transport(repository, settings)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        registered = await client.post(
            "/v1/auth/register",
            json={
                "email": "  DEMO@Example.com ",
                "password": "correct horse battery staple",
                "displayName": "Demo User",
            },
        )
        assert registered.status_code == 201
        payload = registered.json()
        assert payload["user"]["email"] == "demo@example.com"
        assert payload["user"]["displayName"] == "Demo User"
        assert payload["sessionId"]
        assert "HttpOnly" in registered.headers["set-cookie"]
        first_session_id = payload["sessionId"]

        me = await client.get(
            "/v1/auth/me",
            headers={"X-FOMO-Session": first_session_id},
        )
        assert me.status_code == 200
        assert me.json()["id"] == payload["user"]["id"]

        duplicate = await client.post(
            "/v1/auth/register",
            json={
                "email": "demo@example.com",
                "password": "another safe password",
            },
        )
        assert duplicate.status_code == 409

        missing_account = await client.post(
            "/v1/auth/login",
            json={"email": "missing@example.com", "password": "wrong password"},
        )
        wrong_password = await client.post(
            "/v1/auth/login",
            json={"email": "demo@example.com", "password": "wrong password"},
        )
        assert missing_account.status_code == wrong_password.status_code == 401
        assert missing_account.json()["detail"] == wrong_password.json()["detail"]

        logged_out = await client.post(
            "/v1/auth/logout",
            headers={"X-FOMO-Session": first_session_id},
        )
        assert logged_out.status_code == 204
        rejected = await client.get(
            "/v1/auth/me",
            headers={"X-FOMO-Session": first_session_id},
        )
        assert rejected.status_code == 401

        guest = await client.post("/v1/sessions/guest")
        guest_session_id = guest.json()["id"]
        guest_project = await client.post(
            "/v1/projects",
            headers={"X-FOMO-Session": guest_session_id},
            json={"title": "Login claim"},
        )
        assert guest_project.status_code == 201

        logged_in = await client.post(
            "/v1/auth/login",
            json={
                "email": "demo@example.com",
                "password": "correct horse battery staple",
            },
        )
        assert logged_in.status_code == 200
        logged_in_session_id = logged_in.json()["sessionId"]
        assert logged_in_session_id not in {first_session_id, guest_session_id}
        assert (
            await client.get(
                "/v1/projects",
                headers={"X-FOMO-Session": logged_in_session_id},
            )
        ).json()[0]["id"] == guest_project.json()["id"]
        assert (
            await client.get(
                "/v1/projects",
                headers={"X-FOMO-Session": guest_session_id},
            )
        ).status_code == 401

    async with repository.database.session_factory() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.email == "demo@example.com")
        )
        assert user is not None
        assert user.password_hash.startswith("scrypt$")
        assert user.password_hash != "correct horse battery staple"


@pytest.mark.asyncio
async def test_production_session_cookie_is_host_only_secure_and_deleted_symmetrically(
    repository,
    settings,
) -> None:
    production = replace(
        settings,
        app_env="production",
        session_cookie_name="fomo_session",
    )
    transport = _transport(repository, production)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.example.test",
    ) as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"email": "secure@example.com", "password": "secure password"},
        )
        assert registered.status_code == 201
        session_id = registered.json()["sessionId"]
        set_cookie = registered.headers["set-cookie"]
        normalized_set = set_cookie.lower()
        assert set_cookie.startswith("__Host-fomo_session=")
        assert "path=/" in normalized_set
        assert "httponly" in normalized_set
        assert "secure" in normalized_set
        assert "samesite=lax" in normalized_set
        assert "domain=" not in normalized_set

        logged_out = await client.post(
            "/v1/auth/logout",
            headers={"X-FOMO-Session": session_id},
        )
        assert logged_out.status_code == 204
        delete_cookie = logged_out.headers["set-cookie"]
        normalized_delete = delete_cookie.lower()
        assert delete_cookie.startswith('__Host-fomo_session=""')
        assert "max-age=0" in normalized_delete
        assert "path=/" in normalized_delete
        assert "httponly" in normalized_delete
        assert "secure" in normalized_delete
        assert "samesite=lax" in normalized_delete
        assert "domain=" not in normalized_delete


@pytest.mark.asyncio
async def test_development_session_cookie_keeps_compatible_name_without_secure(
    repository,
    settings,
) -> None:
    transport = _transport(repository, settings)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        guest = await client.post("/v1/sessions/guest")

    cookie = guest.headers["set-cookie"]
    normalized = cookie.lower()
    assert cookie.startswith("fomo_session=")
    assert "path=/" in normalized
    assert "httponly" in normalized
    assert "samesite=lax" in normalized
    assert "secure" not in normalized
    assert "domain=" not in normalized


@pytest.mark.asyncio
async def test_same_user_sessions_share_projects_but_other_users_and_guests_cannot(
    repository,
    settings,
) -> None:
    transport = _transport(repository, settings)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as first_client,
        httpx.AsyncClient(transport=transport, base_url="http://test") as second_client,
        httpx.AsyncClient(transport=transport, base_url="http://test") as other_client,
    ):
        first_session = (
            await first_client.post(
                "/v1/auth/register",
                json={"email": "owner@example.com", "password": "owner password"},
            )
        ).json()["sessionId"]
        first_headers = {"X-FOMO-Session": first_session}
        project = await first_client.post(
            "/v1/projects",
            headers=first_headers,
            json={"title": "Shared account project"},
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        second_session = (
            await second_client.post(
                "/v1/auth/login",
                json={"email": "owner@example.com", "password": "owner password"},
            )
        ).json()["sessionId"]
        assert second_session != first_session
        second_headers = {"X-FOMO-Session": second_session}
        listed = await second_client.get("/v1/projects", headers=second_headers)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [project_id]
        assert (
            await second_client.get(f"/v1/projects/{project_id}", headers=second_headers)
        ).status_code == 200

        other_session = (
            await other_client.post(
                "/v1/auth/register",
                json={"email": "other@example.com", "password": "other password"},
            )
        ).json()["sessionId"]
        other_headers = {"X-FOMO-Session": other_session}
        assert (await other_client.get("/v1/projects", headers=other_headers)).json() == []
        assert (
            await other_client.get(f"/v1/projects/{project_id}", headers=other_headers)
        ).status_code == 403

        guest_session = (await other_client.post("/v1/sessions/guest")).json()["id"]
        assert (
            await other_client.get(
                f"/v1/projects/{project_id}",
                headers={"X-FOMO-Session": guest_session},
            )
        ).status_code == 403


@pytest.mark.parametrize(
    ("path_template", "foreign_status"),
    [
        ("/v1/runs/{run_id}", 403),
        ("/v1/runs/{run_id}/artifacts/{artifact_id}", 404),
        ("/v1/projects/{project_id}/preview", 403),
    ],
)
@pytest.mark.asyncio
async def test_account_authorization_matrix_for_nested_surfaces(
    repository,
    settings,
    path_template: str,
    foreign_status: int,
) -> None:
    transport = _transport(repository, settings)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        owner_session = (
            await client.post(
                "/v1/auth/register",
                json={"email": "matrix-owner@example.com", "password": "owner password"},
            )
        ).json()["sessionId"]
        owner_headers = {"X-FOMO-Session": owner_session}
        project_id = (
            await client.post(
                "/v1/projects",
                headers=owner_headers,
                json={"title": "Authorization matrix"},
            )
        ).json()["id"]
        run_id = (
            await client.post(
                f"/v1/projects/{project_id}/messages",
                headers={**owner_headers, "Idempotency-Key": "matrix-message"},
                json={"clientMessageId": "matrix-message", "content": "Build it"},
            )
        ).json()["run"]["id"]
        artifact_id = await repository.store_artifact(
            run_id,
            "product_spec",
            {"title": "Matrix spec", "problem": "Prove ownership"},
        )

        same_user_session = (
            await client.post(
                "/v1/auth/login",
                json={"email": "matrix-owner@example.com", "password": "owner password"},
            )
        ).json()["sessionId"]
        other_user_session = (
            await client.post(
                "/v1/auth/register",
                json={"email": "matrix-other@example.com", "password": "other password"},
            )
        ).json()["sessionId"]
        guest_session = (await client.post("/v1/sessions/guest")).json()["id"]
        path = path_template.format(
            project_id=project_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )

        assert (
            await client.get(path, headers={"X-FOMO-Session": same_user_session})
        ).status_code == 200
        assert (
            await client.get(path, headers={"X-FOMO-Session": other_user_session})
        ).status_code == foreign_status
        assert (
            await client.get(path, headers={"X-FOMO-Session": guest_session})
        ).status_code == foreign_status


@pytest.mark.parametrize("corruption", ["owner_kind", "requester_kind", "missing_user"])
@pytest.mark.asyncio
async def test_account_sharing_fails_closed_for_invalid_identity_links(
    repository,
    settings,
    corruption: str,
) -> None:
    transport = _transport(repository, settings)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"email": "linked@example.com", "password": "linked password"},
        )
        user_id = registered.json()["user"]["id"]
        owner_session_id = registered.json()["sessionId"]
        project_id = (
            await client.post(
                "/v1/projects",
                headers={"X-FOMO-Session": owner_session_id},
                json={"title": "Identity link"},
            )
        ).json()["id"]
        requester_session_id = (
            await client.post(
                "/v1/auth/login",
                json={"email": "linked@example.com", "password": "linked password"},
            )
        ).json()["sessionId"]

        async with repository.database.session_factory() as session:
            if corruption == "missing_user":
                user = await session.get(UserRecord, user_id)
                assert user is not None
                await session.delete(user)
            else:
                target_id = (
                    owner_session_id if corruption == "owner_kind" else requester_session_id
                )
                target = await session.get(SessionRecord, target_id)
                assert target is not None
                target.kind = "guest"
            await session.commit()

        denied = await client.get(
            f"/v1/projects/{project_id}",
            headers={"X-FOMO-Session": requester_session_id},
        )
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_sse_stream_stops_after_the_authenticated_session_is_revoked(
    repository,
    settings,
) -> None:
    transport = _transport(repository, settings)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = (
            await client.post(
                "/v1/auth/register",
                json={"email": "stream@example.com", "password": "stream password"},
            )
        ).json()["sessionId"]
        headers = {"X-FOMO-Session": session_id}
        project_id = (
            await client.post(
                "/v1/projects",
                headers=headers,
                json={"title": "Revocable stream"},
            )
        ).json()["id"]
        run_id = (
            await client.post(
                f"/v1/projects/{project_id}/messages",
                headers={**headers, "Idempotency-Key": "stream-message"},
                json={"clientMessageId": "stream-message", "content": "Build it"},
            )
        ).json()["run"]["id"]
        stream = _stream_run_events(
            _ConnectedRequest(),  # type: ignore[arg-type]
            repository,
            run_id,
            session_id,
            0,
            poll_interval_seconds=0,
        )
        first_event = await anext(stream)
        assert first_event["event"] == "run.event"

        assert (await client.post("/v1/auth/logout", headers=headers)).status_code == 204
        with pytest.raises(StopAsyncIteration):
            await anext(stream)


@pytest.mark.asyncio
async def test_guest_projects_survive_registration_logout_and_a_new_login(
    repository,
    settings,
) -> None:
    transport = _transport(repository, settings)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as guest_client:
        guest = await guest_client.post("/v1/sessions/guest")
        guest_session_id = guest.json()["id"]
        guest_project = await guest_client.post(
            "/v1/projects",
            headers={"X-FOMO-Session": guest_session_id},
            json={"title": "Guest draft"},
        )
        assert guest_project.status_code == 201
        project_id = guest_project.json()["id"]
        assert (await guest_client.get("/v1/auth/me")).status_code == 401

        registered = await guest_client.post(
            "/v1/auth/register",
            json={"email": "claimed@example.com", "password": "claimed password"},
        )
        assert registered.status_code == 201
        user_session_id = registered.json()["sessionId"]
        assert user_session_id != guest_session_id
        listed = await guest_client.get("/v1/projects")
        assert [item["id"] for item in listed.json()] == [project_id]
        assert (
            await guest_client.get(
                "/v1/projects",
                headers={"X-FOMO-Session": guest_session_id},
            )
        ).status_code == 401
        async with repository.database.session_factory() as session:
            project = await session.get(ProjectRecord, project_id)
            old_guest = await session.get(SessionRecord, guest_session_id)
            assert project is not None and project.owner_session_id == user_session_id
            assert old_guest is not None and old_guest.revoked_at is not None
        assert (await guest_client.post("/v1/auth/logout")).status_code == 204

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as new_client:
        logged_in = await new_client.post(
            "/v1/auth/login",
            json={"email": "claimed@example.com", "password": "claimed password"},
        )
        assert logged_in.status_code == 200
        listed = await new_client.get("/v1/projects")
        assert [item["id"] for item in listed.json()] == [project_id]
