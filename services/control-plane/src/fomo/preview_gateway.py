"""Host-based gateway for verified generated-application previews.

The browser sees a stable, isolated origin such as
``<sandbox-id>.preview.example.com``.  The gateway resolves the sandbox's
short-lived Docker endpoint server-side, after persistence confirms that the
sandbox still backs a successfully verified run.  Control-plane credentials
and account cookies never cross into generated applications.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from fomo.config import Settings
from fomo.persistence import Database, NotFoundError, Repository

_APPLICATION_PORT = 8080
_MIN_UPSTREAM_PORT = 40_000
_MAX_UPSTREAM_PORT = 60_000
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_REQUEST_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_FORWARDED_REQUEST_HEADERS = {
    "accept",
    "accept-language",
    "access-control-request-headers",
    "access-control-request-method",
    "cache-control",
    "content-encoding",
    "content-language",
    "content-type",
    "dnt",
    "idempotency-key",
    "if-match",
    "if-modified-since",
    "if-none-match",
    "if-range",
    "if-unmodified-since",
    "last-event-id",
    "next-action",
    "next-router-prefetch",
    "next-router-state-tree",
    "next-url",
    "origin",
    "priority",
    "purpose",
    "range",
    "referer",
    "rsc",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "sec-purpose",
    "user-agent",
    "x-nextjs-data",
}
_REWRITTEN_RESPONSE_HEADERS = {
    "cache-control",
    "content-encoding",
    "content-length",
    "expires",
    "pragma",
    "set-cookie",
}
_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class PreviewEndpointUnavailable(RuntimeError):
    """OpenSandbox no longer exposes the requested generated application."""


class PreviewEndpointExpired(PreviewEndpointUnavailable):
    """OpenSandbox authoritatively reports that the sandbox no longer exists."""


class PreviewBodyTooLarge(RuntimeError):
    """A generated application request or response exceeded the gateway bound."""


@dataclass(frozen=True, slots=True)
class PreviewGatewayConfig:
    base_domain: str
    opensandbox_base_url: str
    opensandbox_api_key: str | None = None
    upstream_host_override: str | None = None
    application_port: int = _APPLICATION_PORT
    request_timeout_seconds: float = 30.0
    max_request_body_bytes: int = 2 * 1024 * 1024
    max_response_body_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_domain", _normalize_domain(self.base_domain))
        parsed = urlsplit(self.opensandbox_base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OpenSandbox base URL must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("OpenSandbox base URL must not contain userinfo")
        if not 1 <= self.application_port <= 65_535:
            raise ValueError("preview application port is invalid")
        if self.request_timeout_seconds <= 0:
            raise ValueError("preview gateway timeout must be positive")
        if self.max_request_body_bytes <= 0 or self.max_response_body_bytes <= 0:
            raise ValueError("preview gateway body limits must be positive")
        if self.upstream_host_override:
            override = urlsplit(f"//{self.upstream_host_override}")
            if (
                not override.hostname
                or override.port is not None
                or override.username is not None
                or override.password is not None
                or override.path
            ):
                raise ValueError("preview upstream host override must be a hostname without a port")


def _normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    labels = domain.split(".")
    if not domain or len(domain) > 253 or len(labels) < 2:
        raise ValueError("PUBLIC_PREVIEW_BASE_DOMAIN must be a DNS domain")
    if any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ValueError("PUBLIC_PREVIEW_BASE_DOMAIN must be a DNS domain")
    return domain


def _sandbox_id_from_host(host: str | None, base_domain: str) -> str | None:
    if host is None:
        return None
    hostname = host.lower().rstrip(".")
    suffix = f".{base_domain}"
    if not hostname.endswith(suffix):
        return None
    label = hostname[: -len(suffix)]
    if not label or "." in label:
        return None
    try:
        sandbox_id = str(UUID(label))
    except ValueError:
        return None
    return sandbox_id if sandbox_id == label else None


def _canonical_public_origin(sandbox_id: str, base_domain: str) -> str:
    return f"https://{sandbox_id}.{base_domain}"


def _request_headers(headers: Mapping[str, str], public_host: str) -> dict[str, str]:
    forwarded = {
        name: value
        for name, value in headers.items()
        if name.lower() in _FORWARDED_REQUEST_HEADERS
    }
    # Never trust forwarding metadata supplied by the browser or ingress.  The
    # generated Next server needs one coherent public origin for Server Action
    # origin checks and absolute URL construction, so the gateway writes it.
    forwarded.update(
        {
            "Host": public_host,
            "X-Forwarded-Host": public_host,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Port": "443",
        }
    )
    return forwarded


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    forwarded = {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
        and name.lower() not in _REWRITTEN_RESPONSE_HEADERS
    }
    forwarded.update(_NO_STORE_HEADERS)
    return forwarded


def _is_loopback_host(host: str) -> bool:
    candidate = host.casefold().rstrip(".")
    if candidate == "localhost":
        return True
    try:
        return ip_address(candidate).is_loopback
    except ValueError:
        return False


def _endpoint_origin(endpoint: str, host_override: str | None) -> str:
    candidate = endpoint.strip()
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    scheme = parsed.scheme or "http"
    try:
        port = parsed.port
    except ValueError as exc:
        raise PreviewEndpointUnavailable("preview endpoint has an invalid port") from exc
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PreviewEndpointUnavailable("preview endpoint is invalid")
    if not _MIN_UPSTREAM_PORT <= port <= _MAX_UPSTREAM_PORT:
        raise PreviewEndpointUnavailable("preview endpoint port is outside the sandbox range")
    if host_override is None and not _is_loopback_host(parsed.hostname):
        raise PreviewEndpointUnavailable("preview endpoint host is not allowed")
    host = host_override or parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return urlunsplit(SplitResult(scheme, f"{host}:{port}", "", "", ""))


def _rewrite_location(location: str, upstream_origin: str, public_origin: str) -> str:
    parsed = urlsplit(location)
    if not parsed.scheme and not parsed.netloc:
        return location
    upstream = urlsplit(upstream_origin)
    if parsed.username is not None or parsed.password is not None:
        return location
    candidate_scheme = (parsed.scheme or upstream.scheme).casefold()
    if candidate_scheme not in {"http", "https"}:
        return location
    try:
        candidate_port = parsed.port or (443 if candidate_scheme == "https" else 80)
        upstream_port = upstream.port or (443 if upstream.scheme == "https" else 80)
    except ValueError:
        return location
    if (
        parsed.hostname is None
        or upstream.hostname is None
        or parsed.hostname.casefold() != upstream.hostname.casefold()
        or candidate_scheme != upstream.scheme.casefold()
        or candidate_port != upstream_port
    ):
        return location
    public = urlsplit(public_origin)
    return urlunsplit(
        SplitResult(
            public.scheme,
            public.netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


async def _bounded_request_body(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise PreviewBodyTooLarge("preview request body is too large")
        body.extend(chunk)
    return bytes(body)


class OpenSandboxEndpointResolver:
    def __init__(self, config: PreviewGatewayConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client

    def _headers(self) -> dict[str, str]:
        if not self._config.opensandbox_api_key:
            return {}
        return {"OPEN-SANDBOX-API-KEY": self._config.opensandbox_api_key}

    async def _sandbox_is_expired(self, sandbox_id: str) -> bool:
        try:
            response = await self._client.get(
                f"{self._config.opensandbox_base_url.rstrip('/')}/sandboxes/{sandbox_id}",
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise PreviewEndpointUnavailable("preview sandbox lookup failed") from exc
        if response.status_code in {404, 410}:
            return True
        try:
            response.raise_for_status()
            payload = response.json()
            status = payload["status"]
            state = status["state"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise PreviewEndpointUnavailable("preview sandbox lookup failed") from exc
        if not isinstance(state, str) or not state.strip():
            raise PreviewEndpointUnavailable("preview sandbox lookup failed")
        return state.casefold() != "running"

    async def resolve(self, sandbox_id: str) -> str:
        try:
            response = await self._client.get(
                f"{self._config.opensandbox_base_url.rstrip('/')}/sandboxes/"
                f"{sandbox_id}/endpoints/{self._config.application_port}",
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise PreviewEndpointUnavailable("preview endpoint lookup failed") from exc
        if response.status_code == 410:
            raise PreviewEndpointExpired("preview sandbox expired")
        if response.status_code == 404:
            if await self._sandbox_is_expired(sandbox_id):
                raise PreviewEndpointExpired("preview sandbox expired")
            raise PreviewEndpointUnavailable("preview endpoint is unavailable")
        try:
            response.raise_for_status()
            endpoint = response.json()["endpoint"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise PreviewEndpointUnavailable("preview endpoint lookup failed") from exc
        if not isinstance(endpoint, str):
            raise PreviewEndpointUnavailable("preview endpoint lookup failed")
        return _endpoint_origin(endpoint, self._config.upstream_host_override)


def create_preview_gateway(
    settings: Settings | None = None,
    repository: Repository | None = None,
    *,
    config: PreviewGatewayConfig | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if config is None:
        base_domain = settings.public_preview_base_domain
        if not base_domain:
            raise ValueError("PUBLIC_PREVIEW_BASE_DOMAIN is required for the preview gateway")
        config = PreviewGatewayConfig(
            base_domain=base_domain,
            opensandbox_base_url=settings.opensandbox_base_url,
            opensandbox_api_key=settings.opensandbox_api_key,
            upstream_host_override=(
                os.getenv("PREVIEW_UPSTREAM_HOST_OVERRIDE", "").strip() or None
            ),
        )

    owns_database = repository is None
    database = repository.database if repository is not None else Database(settings.database_url)
    repository = repository or Repository(database)
    owns_http_client = http_client is None
    http_client = http_client or httpx.AsyncClient(
        timeout=config.request_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    )
    resolver = OpenSandboxEndpointResolver(config, http_client)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await repository.initialize()
        yield
        if owns_http_client:
            await http_client.aclose()
        if owns_database:
            await database.dispose()

    app = FastAPI(
        title="FOMO Preview Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.config = config

    @app.get("/_fomo_gateway/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def gateway_error(status_code: int, detail: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={"detail": detail},
            headers=_NO_STORE_HEADERS,
        )

    async def proxy(request: Request, full_path: str = "") -> Response:
        sandbox_id = _sandbox_id_from_host(request.url.hostname, config.base_domain)
        if sandbox_id is None:
            return gateway_error(404, "preview not found")
        public_origin = _canonical_public_origin(sandbox_id, config.base_domain)
        public_url = f"{public_origin}/"
        try:
            target = await repository.require_verified_preview_target(sandbox_id)
        except NotFoundError:
            return gateway_error(404, "preview not found")
        if target.preview_url != public_url:
            return gateway_error(404, "preview not found")
        try:
            request_body = await _bounded_request_body(
                request,
                config.max_request_body_bytes,
            )
        except PreviewBodyTooLarge:
            return gateway_error(413, "preview request too large")
        try:
            upstream_origin = await resolver.resolve(sandbox_id)
        except PreviewEndpointExpired:
            await repository.expire_verified_preview_target(
                sandbox_id,
                expected_preview_url=public_url,
            )
            return gateway_error(410, "preview expired")
        except PreviewEndpointUnavailable:
            return gateway_error(502, "preview unavailable")

        raw_path = request.scope.get("raw_path", request.url.path.encode("ascii", "ignore"))
        path = bytes(raw_path).decode("ascii", "surrogateescape") or "/"
        upstream_url = f"{upstream_origin}{path}"
        try:
            async with http_client.stream(
                request.method,
                upstream_url,
                params=list(request.query_params.multi_items()),
                content=request_body,
                headers=_request_headers(
                    request.headers,
                    f"{sandbox_id}.{config.base_domain}",
                ),
            ) as upstream:
                response_headers = _response_headers(upstream.headers)
                if "location" in response_headers:
                    response_headers["location"] = _rewrite_location(
                        response_headers["location"],
                        upstream_origin,
                        public_origin,
                    )
                response_body = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(response_body) + len(chunk) > config.max_response_body_bytes:
                        raise PreviewBodyTooLarge("preview response body is too large")
                    response_body.extend(chunk)
                response_status = upstream.status_code
        except httpx.HTTPError:
            return gateway_error(502, "preview unavailable")
        except PreviewBodyTooLarge:
            return gateway_error(502, "preview response too large")
        return Response(
            content=bytes(response_body),
            status_code=response_status,
            headers=response_headers,
            media_type=None,
        )

    app.add_api_route("/", proxy, methods=_REQUEST_METHODS)
    app.add_api_route("/{full_path:path}", proxy, methods=_REQUEST_METHODS)
    return app


def run() -> None:
    port = int(os.getenv("PREVIEW_GATEWAY_PORT", "8001"))
    uvicorn.run(
        "fomo.preview_gateway:create_preview_gateway",
        factory=True,
        host="0.0.0.0",
        port=port,
        reload=False,
    )
