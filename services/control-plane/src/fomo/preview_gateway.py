"""Gateway for verified generated-application previews.

The preferred security boundary remains an isolated wildcard origin. A
same-origin ``/preview/<sandbox-id>/`` route is also supported as a deployment-
light compromise: HTML receives a CSP sandbox without ``allow-same-origin`` and
cannot submit forms or connect back to the control plane. In both modes the
gateway resolves the short-lived Docker endpoint only after persistence proves
that it still backs a successfully verified run. Account cookies and control-
plane credentials never cross into generated applications.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
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
_PREVIEW_PATH = re.compile(
    r"^/(?:[A-Za-z0-9][A-Za-z0-9._~-]*)(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*$"
)
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
    opensandbox_base_url: str
    base_domain: str | None = None
    base_url: str | None = None
    opensandbox_api_key: str | None = None
    upstream_host_override: str | None = None
    application_port: int = _APPLICATION_PORT
    request_timeout_seconds: float = 30.0
    max_request_body_bytes: int = 2 * 1024 * 1024
    max_response_body_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        base_domain = _normalize_domain(self.base_domain) if self.base_domain else None
        base_url = _normalize_base_url(self.base_url) if self.base_url else None
        if bool(base_domain) == bool(base_url):
            raise ValueError(
                "exactly one of PUBLIC_PREVIEW_BASE_URL or PUBLIC_PREVIEW_BASE_DOMAIN is required"
            )
        object.__setattr__(self, "base_domain", base_domain)
        object.__setattr__(self, "base_url", base_url)
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

    @property
    def path_prefix(self) -> str | None:
        if not self.base_url:
            return None
        return urlsplit(self.base_url).path.rstrip("/")


def _normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    labels = domain.split(".")
    if not domain or len(domain) > 253 or len(labels) < 2:
        raise ValueError("PUBLIC_PREVIEW_BASE_DOMAIN must be a DNS domain")
    if any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ValueError("PUBLIC_PREVIEW_BASE_DOMAIN must be a DNS domain")
    return domain


def _normalize_base_url(value: str) -> str:
    candidate = value.strip()
    if candidate != value or "\\" in candidate:
        raise ValueError("PUBLIC_PREVIEW_BASE_URL must be an absolute http(s) URL")
    parsed = urlsplit(candidate)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("PUBLIC_PREVIEW_BASE_URL must be an absolute http(s) URL") from exc
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (path and not _PREVIEW_PATH.fullmatch(path))
    ):
        raise ValueError("PUBLIC_PREVIEW_BASE_URL must be an absolute http(s) URL")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


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


def _request_headers(headers: Mapping[str, str], public_origin: str) -> dict[str, str]:
    forwarded = {
        name: value
        for name, value in headers.items()
        if name.lower() in _FORWARDED_REQUEST_HEADERS
    }
    # Never trust forwarding metadata supplied by the browser or ingress.  The
    # generated Next server needs one coherent public origin for Server Action
    # origin checks and absolute URL construction, so the gateway writes it.
    public = urlsplit(public_origin)
    port = public.port or (443 if public.scheme == "https" else 80)
    forwarded.update(
        {
            "Host": public.netloc,
            "X-Forwarded-Host": public.netloc,
            "X-Forwarded-Proto": public.scheme,
            "X-Forwarded-Port": str(port),
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


def _rewrite_path_location(
    location: str,
    *,
    upstream_url: str,
    upstream_origin: str,
    public_url: str,
) -> str:
    """Keep every internal redirect inside one verified preview path."""

    resolved = urlsplit(urljoin(upstream_url, location))
    upstream = urlsplit(upstream_origin)
    if resolved.username is not None or resolved.password is not None:
        return location
    try:
        resolved_port = resolved.port or (443 if resolved.scheme == "https" else 80)
        upstream_port = upstream.port or (443 if upstream.scheme == "https" else 80)
    except ValueError:
        return location
    if (
        resolved.scheme.casefold() != upstream.scheme.casefold()
        or not resolved.hostname
        or not upstream.hostname
        or resolved.hostname.casefold() != upstream.hostname.casefold()
        or resolved_port != upstream_port
    ):
        return location
    public = urlsplit(public_url)
    preview_root = public.path.rstrip("/")
    path = resolved.path if resolved.path.startswith("/") else f"/{resolved.path}"
    return urlunsplit(
        SplitResult(
            public.scheme,
            public.netloc,
            f"{preview_root}{path}",
            resolved.query,
            resolved.fragment,
        )
    )


def _path_mode_csp(public_url: str) -> str:
    # A path cannot provide origin isolation. The response sandbox deliberately
    # omits allow-same-origin/forms and prevents generated code from fetching or
    # submitting data back to the control plane. Scripts and styles are limited
    # to this preview's exact public path so ordinary client-side UI still runs.
    source = public_url.rstrip("/") + "/"
    return "; ".join(
        (
            "sandbox allow-scripts",
            "default-src 'none'",
            f"script-src 'unsafe-inline' 'unsafe-eval' {source} blob:",
            f"style-src 'unsafe-inline' {source}",
            f"img-src {source} data: blob:",
            f"font-src {source} data:",
            f"worker-src {source} blob:",
            f"connect-src {source}",
            "form-action 'none'",
            "base-uri 'none'",
            "frame-ancestors 'self'",
        )
    )


def _rewrite_legacy_next_assets(body: bytes, public_url: str) -> bytes:
    """Narrow compatibility for retained builds created before assetPrefix."""

    prefix = urlsplit(public_url).path.rstrip("/").encode("ascii")
    output = bytearray()
    cursor = 0
    while True:
        opening = body.find(b"<", cursor)
        if opening < 0:
            output.extend(body[cursor:])
            break
        output.extend(body[cursor:opening])
        quote: int | None = None
        closing = opening + 1
        while closing < len(body):
            value = body[closing]
            if quote is None and value in {ord('"'), ord("'")}:
                quote = value
            elif quote == value:
                quote = None
            elif quote is None and value == ord(">"):
                break
            closing += 1
        if closing >= len(body):
            output.extend(body[opening:])
            break
        tag = body[opening : closing + 1]
        for attribute in (b"src", b"href"):
            for delimiter in (b'"', b"'"):
                needle = attribute + b"=" + delimiter + b"/_next/"
                replacement = attribute + b"=" + delimiter + prefix + b"/_next/"
                tag = tag.replace(needle, replacement)
        output.extend(tag)
        cursor = closing + 1
    return bytes(output)


def _canonical_sandbox_id(value: str) -> str | None:
    try:
        canonical = str(UUID(value))
    except ValueError:
        return None
    return canonical if canonical == value else None


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
        base_url = settings.public_preview_base_url
        if not base_domain and not base_url:
            raise ValueError(
                "PUBLIC_PREVIEW_BASE_URL or PUBLIC_PREVIEW_BASE_DOMAIN is required "
                "for the preview gateway"
            )
        config = PreviewGatewayConfig(
            opensandbox_base_url=settings.opensandbox_base_url,
            base_domain=base_domain,
            base_url=base_url,
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

    async def proxy_verified(
        request: Request,
        *,
        sandbox_id: str,
        public_url: str,
        upstream_path: str,
        path_mode: bool,
    ) -> Response:
        public = urlsplit(public_url)
        public_origin = urlunsplit(SplitResult(public.scheme, public.netloc, "", "", ""))
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

        upstream_url = f"{upstream_origin}{upstream_path}"
        try:
            async with http_client.stream(
                request.method,
                upstream_url,
                params=list(request.query_params.multi_items()),
                content=request_body,
                headers=_request_headers(
                    request.headers,
                    public_origin,
                ),
            ) as upstream:
                response_headers = _response_headers(upstream.headers)
                if "location" in response_headers:
                    response_headers["location"] = (
                        _rewrite_path_location(
                            response_headers["location"],
                            upstream_url=upstream_url,
                            upstream_origin=upstream_origin,
                            public_url=public_url,
                        )
                        if path_mode
                        else _rewrite_location(
                            response_headers["location"],
                            upstream_origin,
                            public_origin,
                        )
                    )
                response_body = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(response_body) + len(chunk) > config.max_response_body_bytes:
                        raise PreviewBodyTooLarge("preview response body is too large")
                    response_body.extend(chunk)
                response_status = upstream.status_code
                response_content_type = upstream.headers.get("content-type", "")
        except httpx.HTTPError:
            return gateway_error(502, "preview unavailable")
        except PreviewBodyTooLarge:
            return gateway_error(502, "preview response too large")
        body = bytes(response_body)
        if path_mode and response_content_type.casefold().startswith("text/html"):
            body = _rewrite_legacy_next_assets(body, public_url)
            for name in tuple(response_headers):
                if name.casefold() == "content-security-policy":
                    response_headers.pop(name)
            response_headers["Content-Security-Policy"] = _path_mode_csp(public_url)
        return Response(
            content=body,
            status_code=response_status,
            headers=response_headers,
            media_type=None,
        )

    if config.base_domain:
        async def proxy_host(request: Request, full_path: str = "") -> Response:
            sandbox_id = _sandbox_id_from_host(request.url.hostname, config.base_domain)
            if sandbox_id is None:
                return gateway_error(404, "preview not found")
            public_origin = _canonical_public_origin(sandbox_id, config.base_domain)
            raw_path = request.scope.get(
                "raw_path", request.url.path.encode("ascii", "ignore")
            )
            upstream_path = bytes(raw_path).decode("ascii", "surrogateescape") or "/"
            return await proxy_verified(
                request,
                sandbox_id=sandbox_id,
                public_url=f"{public_origin}/",
                upstream_path=upstream_path,
                path_mode=False,
            )

        app.add_api_route("/", proxy_host, methods=_REQUEST_METHODS)
        app.add_api_route("/{full_path:path}", proxy_host, methods=_REQUEST_METHODS)
    else:
        assert config.base_url is not None
        route_prefix = config.path_prefix or ""

        async def proxy_path(
            request: Request,
            sandbox_id: str,
            full_path: str = "",
        ) -> Response:
            canonical_id = _canonical_sandbox_id(sandbox_id)
            if canonical_id is None:
                return gateway_error(404, "preview not found")
            raw_path = request.scope.get(
                "raw_path", request.url.path.encode("ascii", "ignore")
            )
            path = bytes(raw_path).decode("ascii", "surrogateescape")
            public_root = f"{route_prefix}/{canonical_id}"
            if path == public_root:
                upstream_path = "/"
            elif path.startswith(f"{public_root}/"):
                upstream_path = path[len(public_root):] or "/"
            else:
                return gateway_error(404, "preview not found")
            return await proxy_verified(
                request,
                sandbox_id=canonical_id,
                public_url=f"{config.base_url}/{canonical_id}/",
                upstream_path=upstream_path,
                path_mode=True,
            )

        app.add_api_route(
            f"{route_prefix}/{{sandbox_id}}",
            proxy_path,
            methods=_REQUEST_METHODS,
        )
        app.add_api_route(
            f"{route_prefix}/{{sandbox_id}}/{{full_path:path}}",
            proxy_path,
            methods=_REQUEST_METHODS,
        )
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
