from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from fomo.api import create_app
from fomo.api.app import _stream_run_events
from fomo.ids import utcnow, uuid7
from fomo.persistence.models import ProjectRecord, SessionRecord, UserRecord


def _transport(repository, settings) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=create_app(settings, repository))


def _session_cookie(response: httpx.Response, settings) -> str:
    value = response.cookies.get(settings.session_cookie_key)
    assert value is not None
    return value


def _session_headers(settings, session_id: str) -> dict[str, str]:
    return {"Cookie": f"{settings.session_cookie_key}={session_id}"}


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_development_lifespan_seeds_login_account_without_a_session(
    repository,
    settings,
) -> None:
    development = replace(
        settings,
        app_env="development",
        dev_account_email="dev@fomo.local",
        dev_account_password="fomo-dev-password",
        dev_account_display_name="Dev",
    )
    app = create_app(development, repository)

    async with app.router.lifespan_context(app):
        async with repository.database.session_factory() as session:
            user = await session.scalar(
                select(UserRecord).where(UserRecord.email == "dev@fomo.local")
            )
            sessions = list(await session.scalars(select(SessionRecord)))

        assert user is not None
        assert user.display_name == "Dev"
        assert sessions == []

        _, auth_session = await repository.authenticate_user(
            "dev@fomo.local",
            "fomo-dev-password",
        )
        assert auth_session.user_id == user.id


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
        assert "sessionId" not in payload
        assert "HttpOnly" in registered.headers["set-cookie"]
        first_session_id = _session_cookie(registered, settings)

        me = await client.get(
            "/v1/auth/me",
            headers=_session_headers(settings, first_session_id),
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
            headers=_session_headers(settings, first_session_id),
        )
        assert logged_out.status_code == 204
        rejected = await client.get(
            "/v1/auth/me",
            headers=_session_headers(settings, first_session_id),
        )
        assert rejected.status_code == 401

        logged_in = await client.post(
            "/v1/auth/login",
            json={
                "email": "demo@example.com",
                "password": "correct horse battery staple",
            },
        )
        assert logged_in.status_code == 200
        assert "sessionId" not in logged_in.json()
        logged_in_session_id = _session_cookie(logged_in, settings)
        assert logged_in_session_id != first_session_id
        assert (await client.get("/v1/projects")).json() == []

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
    await repository.register_user("secure@example.com", "secure password")
    transport = _transport(repository, production)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.example.test",
    ) as client:
        logged_in = await client.post(
            "/v1/auth/login",
            json={"email": "secure@example.com", "password": "secure password"},
        )
        assert logged_in.status_code == 200
        set_cookie = logged_in.headers["set-cookie"]
        normalized_set = set_cookie.lower()
        assert set_cookie.startswith("__Host-fomo_session=")
        assert "path=/" in normalized_set
        assert "httponly" in normalized_set
        assert "secure" in normalized_set
        assert "samesite=lax" in normalized_set
        assert "domain=" not in normalized_set

        logged_out = await client.post("/v1/auth/logout")
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
        registered = await client.post(
            "/v1/auth/register",
            json={"email": "cookie@example.com", "password": "cookie password"},
        )

    cookie = registered.headers["set-cookie"]
    normalized = cookie.lower()
    assert cookie.startswith("fomo_session=")
    assert "path=/" in normalized
    assert "httponly" in normalized
    assert "samesite=lax" in normalized
    assert "secure" not in normalized
    assert "domain=" not in normalized


@pytest.mark.asyncio
async def test_same_user_sessions_share_projects_but_other_users_cannot(
    repository,
    settings,
) -> None:
    transport = _transport(repository, settings)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as first_client,
        httpx.AsyncClient(transport=transport, base_url="http://test") as second_client,
        httpx.AsyncClient(transport=transport, base_url="http://test") as other_client,
    ):
        first_registered = await first_client.post(
            "/v1/auth/register",
            json={"email": "owner@example.com", "password": "owner password"},
        )
        first_session = _session_cookie(first_registered, settings)
        first_headers = _session_headers(settings, first_session)
        project = await first_client.post(
            "/v1/projects",
            headers=first_headers,
            json={"title": "Shared account project"},
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        second_logged_in = await second_client.post(
            "/v1/auth/login",
            json={"email": "owner@example.com", "password": "owner password"},
        )
        second_session = _session_cookie(second_logged_in, settings)
        assert second_session != first_session
        second_headers = _session_headers(settings, second_session)
        listed = await second_client.get("/v1/projects", headers=second_headers)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [project_id]
        assert (
            await second_client.get(f"/v1/projects/{project_id}", headers=second_headers)
        ).status_code == 200

        other_registered = await other_client.post(
            "/v1/auth/register",
            json={"email": "other@example.com", "password": "other password"},
        )
        other_session = _session_cookie(other_registered, settings)
        other_headers = _session_headers(settings, other_session)
        assert (await other_client.get("/v1/projects", headers=other_headers)).json() == []
        assert (
            await other_client.get(f"/v1/projects/{project_id}", headers=other_headers)
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
        owner_registered = await client.post(
            "/v1/auth/register",
            json={"email": "matrix-owner@example.com", "password": "owner password"},
        )
        owner_session = _session_cookie(owner_registered, settings)
        owner_headers = _session_headers(settings, owner_session)
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

        same_user_login = await client.post(
            "/v1/auth/login",
            json={"email": "matrix-owner@example.com", "password": "owner password"},
        )
        same_user_session = _session_cookie(same_user_login, settings)
        other_user_registered = await client.post(
            "/v1/auth/register",
            json={"email": "matrix-other@example.com", "password": "other password"},
        )
        other_user_session = _session_cookie(other_user_registered, settings)
        path = path_template.format(
            project_id=project_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )

        assert (
            await client.get(path, headers=_session_headers(settings, same_user_session))
        ).status_code == 200
        assert (
            await client.get(path, headers=_session_headers(settings, other_user_session))
        ).status_code == foreign_status


@pytest.mark.parametrize(
    ("corruption", "expected_status"),
    [("owner_kind", 403), ("requester_kind", 401), ("missing_user", 401)],
)
@pytest.mark.asyncio
async def test_account_sharing_fails_closed_for_invalid_identity_links(
    repository,
    settings,
    corruption: str,
    expected_status: int,
) -> None:
    transport = _transport(repository, settings)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"email": "linked@example.com", "password": "linked password"},
        )
        user_id = registered.json()["user"]["id"]
        owner_session_id = _session_cookie(registered, settings)
        project_id = (
            await client.post(
                "/v1/projects",
                headers=_session_headers(settings, owner_session_id),
                json={"title": "Identity link"},
            )
        ).json()["id"]
        requester_login = await client.post(
            "/v1/auth/login",
            json={"email": "linked@example.com", "password": "linked password"},
        )
        requester_session_id = _session_cookie(requester_login, settings)

        async with repository.database.session_factory() as session:
            if corruption == "missing_user":
                user = await session.get(UserRecord, user_id)
                assert user is not None
                await session.delete(user)
            else:
                target_id = owner_session_id if corruption == "owner_kind" else requester_session_id
                target = await session.get(SessionRecord, target_id)
                assert target is not None
                target.kind = "guest"
            await session.commit()

        denied = await client.get(
            f"/v1/projects/{project_id}",
            headers=_session_headers(settings, requester_session_id),
        )
        assert denied.status_code == expected_status


@pytest.mark.asyncio
async def test_sse_stream_stops_after_the_authenticated_session_is_revoked(
    repository,
    settings,
) -> None:
    transport = _transport(repository, settings)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"email": "stream@example.com", "password": "stream password"},
        )
        session_id = _session_cookie(registered, settings)
        headers = _session_headers(settings, session_id)
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
async def test_guest_endpoint_is_removed_and_legacy_guest_projects_are_not_claimed(
    repository,
    settings,
) -> None:
    guest_session_id = uuid7()
    project_id = uuid7()
    async with repository.database.session_factory() as session:
        session.add(
            SessionRecord(
                id=guest_session_id,
                kind="guest",
                expires_at=utcnow() + timedelta(days=1),
            )
        )
        await session.flush()
        session.add(
            ProjectRecord(
                id=project_id,
                owner_session_id=guest_session_id,
                title="Legacy guest draft",
            )
        )
        await session.commit()

    transport = _transport(repository, settings)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/v1/sessions/guest")).status_code == 404
        registered = await client.post(
            "/v1/auth/register",
            headers={"X-FOMO-Session": guest_session_id},
            json={"email": "new@example.com", "password": "new account password"},
        )
        assert registered.status_code == 201
        assert "sessionId" not in registered.json()
        user_session_id = _session_cookie(registered, settings)
        assert user_session_id != guest_session_id
        assert (await client.get("/v1/projects")).json() == []

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anonymous:
        preflight = await anonymous.options(
            "/v1/projects",
            headers={
                "Origin": settings.web_origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-FOMO-Session",
            },
        )
        assert preflight.status_code == 400
        assert "x-fomo-session" not in preflight.headers["access-control-allow-headers"].lower()
        assert (
            await anonymous.get(
                "/v1/projects",
                headers={"X-FOMO-Session": user_session_id},
            )
        ).status_code == 401
        assert (
            await anonymous.get(
                "/v1/projects",
                headers=_session_headers(settings, guest_session_id),
            )
        ).status_code == 401

    async with repository.database.session_factory() as session:
        project = await session.get(ProjectRecord, project_id)
        old_guest = await session.get(SessionRecord, guest_session_id)
        assert project is not None and project.owner_session_id == guest_session_id
        assert old_guest is not None and old_guest.revoked_at is None
