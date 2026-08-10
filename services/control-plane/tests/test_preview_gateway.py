from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from fomo.ids import utcnow, uuid7
from fomo.persistence import NotFoundError, VerifiedPreviewTarget
from fomo.persistence.models import (
    ProjectRecord,
    RunRecord,
    RunSandboxResourceRecord,
    SessionRecord,
)
from fomo.preview_gateway import (
    PreviewEndpointUnavailable,
    PreviewGatewayConfig,
    _endpoint_origin,
    create_preview_gateway,
)


def _config(
    *,
    upstream_host_override: str | None = "host.docker.internal",
    max_request_body_bytes: int = 2 * 1024 * 1024,
    max_response_body_bytes: int = 32 * 1024 * 1024,
) -> PreviewGatewayConfig:
    return PreviewGatewayConfig(
        base_domain="preview.example.test",
        opensandbox_base_url="http://opensandbox.test:8080",
        opensandbox_api_key="server-secret",
        upstream_host_override=upstream_host_override,
        max_request_body_bytes=max_request_body_bytes,
        max_response_body_bytes=max_response_body_bytes,
    )


def _path_config() -> PreviewGatewayConfig:
    return PreviewGatewayConfig(
        base_url="https://app.example.test/preview",
        web_origin="https://app.example.test",
        opensandbox_base_url="http://opensandbox.test:8080",
        opensandbox_api_key="server-secret",
        upstream_host_override="host.docker.internal",
    )


def _target(sandbox_id: str, preview_url: str | None = None) -> VerifiedPreviewTarget:
    return VerifiedPreviewTarget(
        run_id=uuid7(),
        project_id=uuid7(),
        sandbox_id=sandbox_id,
        preview_url=preview_url or f"https://{sandbox_id}.preview.example.test/",
    )


@pytest.mark.asyncio
async def test_path_gateway_strips_prefix_isolates_credentials_and_sandboxes_html(
    repository,
) -> None:
    sandbox_id = str(uuid4())
    public_url = f"https://app.example.test/preview/{sandbox_id}/"
    repository.require_verified_preview_target = AsyncMock(
        return_value=_target(sandbox_id, public_url)
    )

    async def outbound(request: httpx.Request) -> httpx.Response:
        if request.url.host == "opensandbox.test":
            return httpx.Response(200, json={"endpoint": "localhost:55546"})
        assert request.url.path == "/"
        assert request.headers["host"] == "app.example.test"
        assert request.headers["x-forwarded-proto"] == "https"
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            content=(
                b'<link href="/_next/app.css"><script src=\'/_next/app.js\'></script>'
                + f'<script src="/preview/{sandbox_id}/_next/already.js"></script>'.encode()
                + b'<script>const untouched = \'src="/_next/not-an-attribute.js"\';</script>'
            ),
            headers={
                "content-type": "text/html; charset=utf-8",
                "clear-site-data": '"storage"',
                "content-security-policy": "default-src *",
                "content-security-policy-report-only": "default-src *; report-uri /leak",
                "service-worker-allowed": "/",
                "set-cookie": "generated=secret",
            },
        )

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(outbound))
    app = create_preview_gateway(
        repository=repository,
        config=_path_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://app.example.test",
    ) as browser:
        response = await browser.get(
            f"/preview/{sandbox_id}/",
            headers={"Cookie": "fomo_session=private", "Authorization": "Bearer private"},
        )

    assert response.status_code == 200
    assert f'href="/preview/{sandbox_id}/_next/app.css"' in response.text
    assert f"src='/preview/{sandbox_id}/_next/app.js'" in response.text
    assert response.text.count(f"/preview/{sandbox_id}/_next/already.js") == 1
    assert 'src="/_next/not-an-attribute.js"' in response.text
    assert "set-cookie" not in response.headers
    assert "clear-site-data" not in response.headers
    assert "content-security-policy-report-only" not in response.headers
    assert "service-worker-allowed" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "sandbox allow-scripts" in csp
    assert "allow-same-origin" not in csp
    assert f"connect-src {public_url}" in csp
    assert "form-action 'none'" in csp
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"
    repository.require_verified_preview_target.assert_awaited_once_with(sandbox_id)
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_cross_site_path_gateway_allows_interaction_only_inside_exact_preview(
    repository,
) -> None:
    sandbox_id = str(uuid4())
    base_url = "https://preview.203-0-113-10.sslip.io/preview"
    public_url = f"{base_url}/{sandbox_id}/"
    repository.require_verified_preview_target = AsyncMock(
        return_value=_target(sandbox_id, public_url)
    )

    async def outbound(request: httpx.Request) -> httpx.Response:
        if request.url.host == "opensandbox.test":
            return httpx.Response(200, json={"endpoint": "localhost:55546"})
        assert request.headers["host"] == "preview.203-0-113-10.sslip.io"
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            content=b"<html><body>interactive preview</body></html>",
            headers={
                "content-type": "text/html; charset=utf-8",
                "set-cookie": "generated=secret",
            },
        )

    config = PreviewGatewayConfig(
        base_url=base_url,
        web_origin="https://fomo.yieldsum.com/",
        opensandbox_base_url="http://opensandbox.test:8080",
        opensandbox_api_key="server-secret",
        upstream_host_override="host.docker.internal",
    )
    assert config.cross_site_path_mode is True
    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(outbound))
    app = create_preview_gateway(
        repository=repository,
        config=config,
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://preview.203-0-113-10.sslip.io",
    ) as browser:
        rejected = await browser.get(
            f"https://fomo.yieldsum.com/preview/{sandbox_id}/",
        )
        response = await browser.get(
            f"/preview/{sandbox_id}/",
            headers={"Cookie": "preview=private", "Authorization": "Bearer private"},
        )

    assert rejected.status_code == 404
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert "access-control-allow-origin" not in response.headers
    assert "clear-site-data" not in response.headers
    assert "content-security-policy-report-only" not in response.headers
    assert "service-worker-allowed" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = {
        directive.split(" ", 1)[0]: directive.split(" ", 1)[1]
        for directive in response.headers["content-security-policy"].split("; ")
    }
    assert csp["sandbox"] == "allow-scripts allow-same-origin allow-forms"
    assert csp["script-src"] == f"'unsafe-inline' 'unsafe-eval' {public_url} blob:"
    assert csp["style-src"] == f"'unsafe-inline' {public_url}"
    assert csp["img-src"] == f"{public_url} data: blob:"
    assert csp["font-src"] == f"{public_url} data:"
    assert csp["worker-src"] == "'none'"
    assert csp["connect-src"] == public_url
    assert csp["form-action"] == public_url
    assert csp["frame-ancestors"] == "https://fomo.yieldsum.com"
    repository.require_verified_preview_target.assert_awaited_once_with(sandbox_id)
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_path_gateway_sandboxes_non_html_executable_documents_without_rewriting(
    repository,
) -> None:
    sandbox_id = str(uuid4())
    public_url = f"https://app.example.test/preview/{sandbox_id}/"
    repository.require_verified_preview_target = AsyncMock(
        return_value=_target(sandbox_id, public_url)
    )
    documents = {
        "/document.xhtml": (
            "application/xhtml+xml",
            b'<html xmlns="http://www.w3.org/1999/xhtml"><script src="/_next/x.js"/></html>',
        ),
        "/image.svg": (
            "image/svg+xml",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script href="/_next/x.js"/></svg>',
        ),
    }

    async def outbound(request: httpx.Request) -> httpx.Response:
        if request.url.host == "opensandbox.test":
            return httpx.Response(200, json={"endpoint": "localhost:55546"})
        content_type, content = documents[request.url.path]
        return httpx.Response(
            200,
            content=content,
            headers={
                "content-type": content_type,
                "content-security-policy": "default-src *",
                "content-security-policy-report-only": "default-src *; report-uri /leak",
                "x-content-type-options": "sniff",
            },
        )

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(outbound))
    app = create_preview_gateway(
        repository=repository,
        config=_path_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://app.example.test",
    ) as browser:
        responses = {
            path: await browser.get(f"/preview/{sandbox_id}{path}")
            for path in documents
        }

    for path, response in responses.items():
        assert response.status_code == 200
        assert response.content == documents[path][1]
        assert "sandbox allow-scripts" in response.headers["content-security-policy"]
        assert "default-src *" not in response.headers["content-security-policy"]
        assert "content-security-policy-report-only" not in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"
    assert repository.require_verified_preview_target.await_count == 2
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_path_gateway_requires_exact_published_url_and_rewrites_internal_location(
    repository,
) -> None:
    sandbox_id = str(uuid4())
    public_url = f"https://app.example.test/preview/{sandbox_id}/"
    repository.require_verified_preview_target = AsyncMock(
        side_effect=[
            _target(sandbox_id, "https://app.example.test/preview/other/"),
            _target(sandbox_id, public_url),
        ]
    )

    async def outbound(request: httpx.Request) -> httpx.Response:
        if request.url.host == "opensandbox.test":
            return httpx.Response(200, json={"endpoint": "localhost:55546"})
        assert request.url.path == "/account"
        return httpx.Response(
            307,
            headers={"location": "http://host.docker.internal:55546/login?next=%2F"},
        )

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(outbound))
    app = create_preview_gateway(
        repository=repository,
        config=_path_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://app.example.test",
        follow_redirects=False,
    ) as browser:
        rejected = await browser.get(f"/preview/{sandbox_id}/account")
        redirected = await browser.get(f"/preview/{sandbox_id}/account")

    assert rejected.status_code == 404
    assert redirected.status_code == 307
    assert redirected.headers["location"] == (
        f"https://app.example.test/preview/{sandbox_id}/login?next=%2F"
    )
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_path_gateway_drops_external_and_cross_preview_locations(repository) -> None:
    sandbox_id = str(uuid4())
    other_sandbox_id = str(uuid4())
    public_url = f"https://app.example.test/preview/{sandbox_id}/"
    repository.require_verified_preview_target = AsyncMock(
        return_value=_target(sandbox_id, public_url)
    )

    async def outbound(request: httpx.Request) -> httpx.Response:
        if request.url.host == "opensandbox.test":
            return httpx.Response(200, json={"endpoint": "localhost:55546"})
        location = (
            "https://external.example.net/escape"
            if request.url.path == "/external"
            else f"https://app.example.test/preview/{other_sandbox_id}/"
        )
        return httpx.Response(307, headers={"location": location})

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(outbound))
    app = create_preview_gateway(
        repository=repository,
        config=_path_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://app.example.test",
        follow_redirects=False,
    ) as browser:
        external = await browser.get(f"/preview/{sandbox_id}/external")
        cross_preview = await browser.get(f"/preview/{sandbox_id}/cross-preview")

    assert external.status_code == 307
    assert "location" not in external.headers
    assert cross_preview.status_code == 307
    assert "location" not in cross_preview.headers
    assert repository.require_verified_preview_target.await_count == 2
    await outbound_client.aclose()


def test_path_gateway_config_requires_https_and_unambiguous_site_boundary() -> None:
    common = {
        "opensandbox_base_url": "http://opensandbox.test:8080",
        "upstream_host_override": "host.docker.internal",
    }
    with pytest.raises(ValueError, match="HTTPS outside loopback"):
        PreviewGatewayConfig(
            base_url="http://preview.example.net/preview",
            web_origin="https://app.example.test",
            **common,
        )
    with pytest.raises(ValueError, match="WEB_ORIGIN itself or a different registrable site"):
        PreviewGatewayConfig(
            base_url="https://preview.example.test/preview",
            web_origin="https://app.example.test",
            **common,
        )

    loopback = PreviewGatewayConfig(
        base_url="http://localhost:3000/preview",
        web_origin="http://localhost:3000",
        **common,
    )
    assert loopback.cross_site_path_mode is False


@pytest.mark.asyncio
async def test_gateway_proxies_verified_preview_without_forwarding_control_plane_secrets(
    repository,
) -> None:
    sandbox_id = str(uuid4())
    repository.require_verified_preview_target = AsyncMock(return_value=_target(sandbox_id))
    outbound_requests: list[httpx.Request] = []

    async def outbound(request: httpx.Request) -> httpx.Response:
        outbound_requests.append(request)
        if request.url.host == "opensandbox.test":
            assert request.headers["OPEN-SANDBOX-API-KEY"] == "server-secret"
            return httpx.Response(200, json={"endpoint": "localhost:55546"})
        assert request.url.host == "host.docker.internal"
        assert request.url.port == 55546
        assert request.url.path == "/_next/static/app.js"
        assert request.url.query == b"v=1"
        assert request.headers["host"] == f"{sandbox_id}.preview.example.test"
        assert request.headers["x-forwarded-host"] == f"{sandbox_id}.preview.example.test"
        assert request.headers["x-forwarded-proto"] == "https"
        assert request.headers["x-forwarded-port"] == "443"
        assert request.headers["next-action"] == "safe-action-id"
        assert request.headers["accept-language"] == "en-US"
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert "cf-access-jwt-assertion" not in request.headers
        assert "x-fomo-session" not in request.headers
        assert "x-auth-request-user" not in request.headers
        assert "x-api-key" not in request.headers
        assert "x-arbitrary" not in request.headers
        assert "open-sandbox-api-key" not in request.headers
        return httpx.Response(
            200,
            content=b"console.log('ready')",
            headers={
                "content-type": "application/javascript",
                "cache-control": "public, max-age=31536000, immutable",
                "pragma": "upstream-cache",
                "expires": "Tue, 19 Jan 2038 03:14:07 GMT",
                "set-cookie": "must-not-leave-preview=1",
            },
        )

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(outbound))
    app = create_preview_gateway(
        repository=repository,
        config=_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"https://{sandbox_id}.preview.example.test",
    ) as browser:
        response = await browser.get(
            "/_next/static/app.js?v=1",
            headers={
                "Cookie": "fomo_session=private",
                "Authorization": "Bearer private",
                "CF-Access-Jwt-Assertion": "identity-private",
                "X-FOMO-Session": "account-private",
                "X-Auth-Request-User": "identity-private",
                "X-API-Key": "application-private",
                "X-Arbitrary": "not-required",
                "OPEN-SANDBOX-API-KEY": "spoofed",
                "Next-Action": "safe-action-id",
                "Accept-Language": "en-US",
            },
        )

    assert response.status_code == 200
    assert response.content == b"console.log('ready')"
    assert response.headers["content-type"].startswith("application/javascript")
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
    repository.require_verified_preview_target.assert_awaited_once_with(sandbox_id)
    assert len(outbound_requests) == 2
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_gateway_fails_closed_for_unknown_host_or_unpublished_sandbox(repository) -> None:
    sandbox_id = str(uuid4())
    repository.require_verified_preview_target = AsyncMock(
        side_effect=NotFoundError("verified preview not found")
    )

    async def must_not_call_upstream(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unverified preview must not reach OpenSandbox")

    outbound_client = httpx.AsyncClient(
        transport=httpx.MockTransport(must_not_call_upstream)
    )
    app = create_preview_gateway(
        repository=repository,
        config=_config(),
        http_client=outbound_client,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport) as browser:
        invalid_host = await browser.get(
            "https://not-a-sandbox.preview.example.test/",
        )
        unpublished = await browser.get(
            f"https://{sandbox_id}.preview.example.test/",
        )

    assert invalid_host.status_code == 404
    assert unpublished.status_code == 404
    repository.require_verified_preview_target.assert_awaited_once_with(sandbox_id)
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_gateway_maps_missing_opensandbox_endpoint_to_expired(repository) -> None:
    sandbox_id = str(uuid4())
    public_url = f"https://{sandbox_id}.preview.example.test/"
    repository.require_verified_preview_target = AsyncMock(return_value=_target(sandbox_id))
    repository.expire_verified_preview_target = AsyncMock(return_value=True)

    async def missing_endpoint(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "opensandbox.test"
        assert request.headers["OPEN-SANDBOX-API-KEY"] == "server-secret"
        return httpx.Response(404, json={"detail": "not found"})

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(missing_endpoint))
    app = create_preview_gateway(
        repository=repository,
        config=_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"https://{sandbox_id}.preview.example.test",
    ) as browser:
        response = await browser.get("/")

    assert response.status_code == 410
    assert response.json() == {"detail": "preview expired"}
    repository.expire_verified_preview_target.assert_awaited_once_with(
        sandbox_id,
        expected_preview_url=public_url,
    )
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_gateway_does_not_conflate_transient_provider_failure_with_expiry(repository) -> None:
    sandbox_id = str(uuid4())
    repository.require_verified_preview_target = AsyncMock(return_value=_target(sandbox_id))
    repository.expire_verified_preview_target = AsyncMock()

    async def unavailable_endpoint(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "opensandbox.test"
        return httpx.Response(503, json={"detail": "temporarily unavailable"})

    outbound_client = httpx.AsyncClient(
        transport=httpx.MockTransport(unavailable_endpoint)
    )
    app = create_preview_gateway(
        repository=repository,
        config=_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"https://{sandbox_id}.preview.example.test",
    ) as browser:
        response = await browser.get("/")

    assert response.status_code == 502
    assert response.json() == {"detail": "preview unavailable"}
    repository.expire_verified_preview_target.assert_not_awaited()
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_gateway_bounds_generated_application_request_and_response_bodies(repository) -> None:
    sandbox_id = str(uuid4())
    repository.require_verified_preview_target = AsyncMock(return_value=_target(sandbox_id))
    outbound_requests: list[httpx.Request] = []

    async def oversized_response(request: httpx.Request) -> httpx.Response:
        outbound_requests.append(request)
        if request.url.host == "opensandbox.test":
            return httpx.Response(200, json={"endpoint": "localhost:55546"})
        return httpx.Response(200, content=b"12345")

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(oversized_response))
    app = create_preview_gateway(
        repository=repository,
        config=_config(max_request_body_bytes=4, max_response_body_bytes=4),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"https://{sandbox_id}.preview.example.test",
    ) as browser:
        request_too_large = await browser.post("/submit", content=b"12345")
        response_too_large = await browser.get("/")

    assert request_too_large.status_code == 413
    assert request_too_large.json() == {"detail": "preview request too large"}
    assert response_too_large.status_code == 502
    assert response_too_large.json() == {"detail": "preview response too large"}
    assert len(outbound_requests) == 2
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_gateway_rejects_preview_url_not_bound_to_configured_domain(repository) -> None:
    sandbox_id = str(uuid4())
    repository.require_verified_preview_target = AsyncMock(
        return_value=_target(sandbox_id, "http://localhost:55546/")
    )

    async def must_not_call_upstream(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a legacy or other-domain preview must fail before provider lookup")

    outbound_client = httpx.AsyncClient(
        transport=httpx.MockTransport(must_not_call_upstream)
    )
    app = create_preview_gateway(
        repository=repository,
        config=_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"https://{sandbox_id}.preview.example.test",
    ) as browser:
        response = await browser.get("/")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_gateway_does_not_expire_running_sandbox_with_missing_endpoint(repository) -> None:
    sandbox_id = str(uuid4())
    repository.require_verified_preview_target = AsyncMock(return_value=_target(sandbox_id))
    repository.expire_verified_preview_target = AsyncMock()
    requests: list[httpx.Request] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/endpoints/8080"):
            return httpx.Response(404, json={"detail": "endpoint not ready"})
        assert request.url.path == f"/sandboxes/{sandbox_id}"
        return httpx.Response(200, json={"status": {"state": "Running"}})

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    app = create_preview_gateway(
        repository=repository,
        config=_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"https://{sandbox_id}.preview.example.test",
    ) as browser:
        response = await browser.get("/")

    assert response.status_code == 502
    assert len(requests) == 2
    repository.expire_verified_preview_target.assert_not_awaited()
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_gateway_expires_non_running_sandbox_after_missing_endpoint(repository) -> None:
    sandbox_id = str(uuid4())
    public_url = f"https://{sandbox_id}.preview.example.test/"
    repository.require_verified_preview_target = AsyncMock(return_value=_target(sandbox_id))
    repository.expire_verified_preview_target = AsyncMock(return_value=True)

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/endpoints/8080"):
            return httpx.Response(404, json={"detail": "endpoint missing"})
        return httpx.Response(200, json={"status": {"state": "Terminated"}})

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    app = create_preview_gateway(
        repository=repository,
        config=_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"https://{sandbox_id}.preview.example.test",
    ) as browser:
        response = await browser.get("/")

    assert response.status_code == 410
    repository.expire_verified_preview_target.assert_awaited_once_with(
        sandbox_id,
        expected_preview_url=public_url,
    )
    await outbound_client.aclose()


@pytest.mark.asyncio
async def test_gateway_rewrites_internal_location_to_canonical_public_origin(repository) -> None:
    sandbox_id = str(uuid4())
    repository.require_verified_preview_target = AsyncMock(return_value=_target(sandbox_id))

    async def outbound(request: httpx.Request) -> httpx.Response:
        if request.url.host == "opensandbox.test":
            return httpx.Response(200, json={"endpoint": "localhost:55546"})
        return httpx.Response(
            307,
            headers={
                "location": "http://host.docker.internal:55546/login?next=%2F",
                "cache-control": "public, max-age=3600",
            },
        )

    outbound_client = httpx.AsyncClient(transport=httpx.MockTransport(outbound))
    app = create_preview_gateway(
        repository=repository,
        config=_config(),
        http_client=outbound_client,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"https://{sandbox_id}.preview.example.test",
        follow_redirects=False,
    ) as browser:
        response = await browser.get("/")

    assert response.status_code == 307
    assert response.headers["location"] == (
        f"https://{sandbox_id}.preview.example.test/login?next=%2F"
    )
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    await outbound_client.aclose()


def test_endpoint_origin_requires_override_or_loopback_and_sandbox_port_range() -> None:
    assert _endpoint_origin("127.0.0.1:55546", None) == "http://127.0.0.1:55546"
    assert _endpoint_origin("http://provider.invalid:55546", "host.docker.internal") == (
        "http://host.docker.internal:55546"
    )
    with pytest.raises(PreviewEndpointUnavailable, match="host is not allowed"):
        _endpoint_origin("provider.invalid:55546", None)
    with pytest.raises(PreviewEndpointUnavailable, match="outside the sandbox range"):
        _endpoint_origin("localhost:8000", None)


@pytest.mark.asyncio
async def test_repository_allows_only_current_uncleaned_verified_preview(repository) -> None:
    now = utcnow()
    session_id = uuid7()
    project_id = uuid7()
    run_id = uuid7()
    sandbox_id = str(uuid4())
    resource_id = uuid7()
    async with repository.database.session_factory() as session:
        session.add(
            SessionRecord(
                id=session_id,
                kind="guest",
                expires_at=now + timedelta(days=1),
            )
        )
        await session.flush()
        session.add(
            ProjectRecord(
                id=project_id,
                owner_session_id=session_id,
                title="Verified preview",
            )
        )
        await session.flush()
        session.add(
            RunRecord(
                id=run_id,
                project_id=project_id,
                status="succeeded",
                phase="ready",
                sandbox_id=sandbox_id,
                preview_url=f"https://{sandbox_id}.preview.example.test/",
            )
        )
        await session.flush()
        session.add(
            RunSandboxResourceRecord(
                id=resource_id,
                run_id=run_id,
                sandbox_id=sandbox_id,
                kind="verification",
            )
        )
        await session.commit()

    target = await repository.require_verified_preview_target(sandbox_id)
    assert (target.run_id, target.project_id, target.sandbox_id, target.preview_url) == (
        run_id,
        project_id,
        sandbox_id,
        f"https://{sandbox_id}.preview.example.test/",
    )

    assert (
        await repository.expire_verified_preview_target(
            sandbox_id,
            expected_preview_url="https://other.preview.example.test/",
        )
        is False
    )
    assert await repository.require_verified_preview_target(sandbox_id) == target
    assert (
        await repository.expire_verified_preview_target(
            sandbox_id,
            expected_preview_url=target.preview_url,
        )
        is True
    )
    assert await repository.expire_verified_preview_target(sandbox_id) is False

    with pytest.raises(NotFoundError, match="verified preview not found"):
        await repository.require_verified_preview_target(sandbox_id)

    async with repository.database.session_factory() as session:
        run = await session.get(RunRecord, run_id)
        resource = await session.get(RunSandboxResourceRecord, resource_id)
        assert run is not None and run.preview_url is None
        assert resource is not None and resource.cleaned_at is not None
    events = await repository.list_events(run_id)
    assert events[-1].kind == "preview.expired"
    assert events[-1].payload == {
        "sandboxId": sandbox_id,
        "reason": "sandbox_expired",
    }
