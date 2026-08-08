"""FastAPI control-plane API with persistent SSE replay."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from fomo.config import Settings
from fomo.persistence import (
    ConflictError,
    Database,
    FilePathError,
    NotFoundError,
    OwnershipError,
    Repository,
)
from fomo.schemas import (
    ArtifactDetailResponse,
    FileContentResponse,
    FileContentUpdate,
    GuestSessionResponse,
    MessageCreate,
    MessageRunResponse,
    PreviewResponse,
    ProjectCreate,
    ProjectPatch,
    ProjectResponse,
    ProjectSnapshotResponse,
    RunResponse,
    TraceResponse,
    VersionResponse,
)


def problem(status_code: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "type": f"https://fomo.local/problems/{title.lower().replace(' ', '-')}",
            "title": title,
            "status": status_code,
            "detail": detail,
        },
        media_type="application/problem+json",
    )


def create_app(settings: Settings | None = None, repository: Repository | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_database = repository is None
    database = repository.database if repository is not None else Database(settings.database_url)
    repository = repository or Repository(database)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await repository.initialize()
        yield
        if owns_database:
            await database.dispose()

    app = FastAPI(title="FOMO Control Plane", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.repository = repository
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-FOMO-Session", "Last-Event-ID"],
    )

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return problem(404, "Not Found", str(exc))

    @app.exception_handler(OwnershipError)
    async def _forbidden(_: Request, exc: OwnershipError) -> JSONResponse:
        return problem(403, "Forbidden", str(exc))

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return problem(409, "Conflict", str(exc))

    @app.exception_handler(FilePathError)
    async def _invalid_file_path(_: Request, exc: FilePathError) -> JSONResponse:
        return problem(422, "Validation Error", str(exc))

    @app.exception_handler(HTTPException)
    async def _http_exception(_: Request, exc: HTTPException) -> JSONResponse:
        return problem(exc.status_code, "Request Error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, _exc: RequestValidationError) -> JSONResponse:
        return problem(422, "Validation Error", "The request did not match the API contract.")

    async def session_id(
        request: Request,
        x_fomo_session: Annotated[str | None, Header()] = None,
        fomo_session: Annotated[str | None, Cookie()] = None,
    ) -> str:
        candidate = x_fomo_session or request.cookies.get(settings.session_cookie_name) or fomo_session
        if not candidate:
            raise HTTPException(status_code=401, detail="guest session is required")
        try:
            await repository.get_session(candidate)
        except NotFoundError as exc:
            raise HTTPException(status_code=401, detail="guest session is invalid or expired") from exc
        return candidate

    SessionId = Annotated[str, Depends(session_id)]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sessions/guest", response_model=GuestSessionResponse, status_code=status.HTTP_201_CREATED)
    async def create_guest(response: Response) -> GuestSessionResponse:
        record = await repository.create_guest_session()
        response.set_cookie(
            key=settings.session_cookie_name,
            value=record.id,
            max_age=14 * 24 * 60 * 60,
            httponly=True,
            secure=settings.app_env not in {"development", "test"},
            samesite="lax",
        )
        return GuestSessionResponse(id=record.id, expires_at=record.expires_at)

    @app.get("/v1/projects", response_model=list[ProjectResponse])
    async def list_projects(owner_session_id: SessionId) -> list[ProjectResponse]:
        return await repository.list_projects(owner_session_id)

    @app.post("/v1/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
    async def create_project(payload: ProjectCreate, owner_session_id: SessionId) -> ProjectResponse:
        return await repository.create_project(owner_session_id, payload.title)

    @app.get("/v1/projects/{project_id}", response_model=ProjectSnapshotResponse)
    async def get_project(project_id: str, owner_session_id: SessionId) -> ProjectSnapshotResponse:
        snapshot = await repository.get_project_snapshot(project_id, owner_session_id)
        trace = snapshot["trace"]
        return ProjectSnapshotResponse(
            project=snapshot["project"],
            messages=snapshot["messages"],
            runs=snapshot["runs"],
            active_run=snapshot["active_run"],
            last_seq=snapshot["last_seq"],
            events=snapshot["events"],
            files=snapshot["files"],
            versions=snapshot["versions"],
            trace=TraceResponse(
                run_id=trace["run_id"],
                links=trace["links"],
                evidence=trace["evidence"],
                acceptance_trace=trace["acceptance_trace"],
            ),
            preview=snapshot["preview"],
            artifact_refs=snapshot["artifact_refs"],
        )

    @app.patch("/v1/projects/{project_id}", response_model=ProjectResponse)
    async def patch_project(
        project_id: str, payload: ProjectPatch, owner_session_id: SessionId
    ) -> ProjectResponse:
        return await repository.patch_project(project_id, owner_session_id, payload.title)

    @app.post("/v1/projects/{project_id}/messages", response_model=MessageRunResponse)
    async def create_message(
        project_id: str,
        payload: MessageCreate,
        response: Response,
        owner_session_id: SessionId,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> MessageRunResponse:
        if idempotency_key and idempotency_key != payload.client_message_id:
            raise HTTPException(status_code=422, detail="Idempotency-Key must match clientMessageId")
        message, run, created = await repository.create_message_and_run(
            project_id,
            owner_session_id,
            payload.client_message_id,
            payload.content,
            payload.base_version_id,
        )
        response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return MessageRunResponse(message=message, run=run)

    async def _owned_run(run_id: str, owner_session_id: str) -> RunResponse:
        run = await repository.get_run(run_id)
        await repository.require_project(run.project_id, owner_session_id)
        return run

    @app.get("/v1/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str, owner_session_id: SessionId) -> RunResponse:
        return await _owned_run(run_id, owner_session_id)

    @app.get(
        "/v1/runs/{run_id}/artifacts/{artifact_id}",
        response_model=ArtifactDetailResponse,
    )
    async def get_artifact_detail(
        run_id: str, artifact_id: str, owner_session_id: SessionId
    ) -> ArtifactDetailResponse:
        return await repository.get_artifact_detail(run_id, artifact_id, owner_session_id)

    @app.get("/v1/runs/{run_id}/events")
    async def run_events(
        request: Request,
        run_id: str,
        owner_session_id: SessionId,
        after: int = Query(default=0, ge=0),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> EventSourceResponse:
        await _owned_run(run_id, owner_session_id)
        try:
            cursor = max(after, int(last_event_id or "0"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Last-Event-ID must be an integer sequence") from exc

        async def stream() -> AsyncIterator[dict[str, str]]:
            nonlocal cursor
            while not await request.is_disconnected():
                events = await repository.list_events(run_id, after=cursor)
                for event in events:
                    cursor = event.seq
                    yield {
                        "id": str(event.seq),
                        "event": "run.event",
                        "data": event.model_dump_json(by_alias=True),
                    }
                run = await repository.get_run(run_id)
                if run.status.value in {"succeeded", "failed", "cancelled", "needs_attention"}:
                    return
                await asyncio.sleep(0.5)

        return EventSourceResponse(
            stream(),
            ping=15,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunResponse)
    async def cancel_run(run_id: str, owner_session_id: SessionId) -> RunResponse:
        await _owned_run(run_id, owner_session_id)
        return await repository.request_cancel(run_id)

    @app.get("/v1/projects/{project_id}/files")
    async def list_files(
        project_id: str,
        owner_session_id: SessionId,
        version_id: str | None = Query(default=None, alias="versionId"),
    ) -> dict[str, Any]:
        await repository.require_project(project_id, owner_session_id)
        return {"files": await repository.list_version_files(project_id, version_id)}

    @app.get("/v1/projects/{project_id}/files/content", response_model=FileContentResponse)
    async def get_file_content(
        project_id: str,
        path: str,
        owner_session_id: SessionId,
        version_id: str | None = Query(default=None, alias="versionId"),
    ) -> FileContentResponse:
        await repository.require_project(project_id, owner_session_id)
        selected_version_id, content, sha256 = await repository.get_version_file_content(
            project_id, path, version_id
        )
        return FileContentResponse(
            version_id=selected_version_id,
            path=path,
            content=content,
            sha256=sha256,
        )

    @app.put("/v1/projects/{project_id}/files/content", response_model=FileContentResponse)
    async def put_file_content(
        project_id: str,
        path: str,
        payload: FileContentUpdate,
        owner_session_id: SessionId,
    ) -> FileContentResponse:
        version, normalized_path, sha256 = await repository.save_file_content(
            project_id,
            owner_session_id,
            path,
            payload.content,
            base_version_id=payload.base_version_id,
            base_sha256=payload.base_sha256,
        )
        return FileContentResponse(
            version_id=version.id,
            path=normalized_path,
            content=payload.content,
            sha256=sha256,
        )

    @app.get("/v1/projects/{project_id}/versions", response_model=list[VersionResponse])
    async def list_versions(project_id: str, owner_session_id: SessionId) -> list[VersionResponse]:
        await repository.require_project(project_id, owner_session_id)
        return await repository.list_versions(project_id)

    @app.post(
        "/v1/projects/{project_id}/versions/{version_id}/restore",
        response_model=VersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def restore_version(
        project_id: str, version_id: str, owner_session_id: SessionId
    ) -> VersionResponse:
        return await repository.restore_version(project_id, owner_session_id, version_id)

    @app.get("/v1/projects/{project_id}/download")
    async def download_version(
        project_id: str,
        owner_session_id: SessionId,
        version_id: str | None = Query(default=None, alias="versionId"),
    ) -> Response:
        await repository.require_project(project_id, owner_session_id)
        version, archive = await repository.build_version_archive(project_id, version_id)
        return Response(
            content=archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="fomo-project-v{version.number}.zip"',
                "Content-Length": str(len(archive)),
            },
        )

    @app.get("/v1/projects/{project_id}/trace", response_model=TraceResponse)
    async def get_trace(
        project_id: str,
        owner_session_id: SessionId,
        run_id: str | None = Query(default=None, alias="runId"),
    ) -> TraceResponse:
        if run_id is None:
            await repository.require_project(project_id, owner_session_id)
        else:
            await repository.require_run_for_project(run_id, project_id, owner_session_id)
        trace = await repository.get_trace(project_id, run_id)
        return TraceResponse(
            run_id=trace["run_id"],
            links=trace["links"],
            evidence=trace["evidence"],
            acceptance_trace=trace["acceptance_trace"],
        )

    @app.get("/v1/projects/{project_id}/preview", response_model=PreviewResponse)
    async def get_preview(project_id: str, owner_session_id: SessionId) -> PreviewResponse:
        await repository.require_project(project_id, owner_session_id)
        return await repository.get_preview(project_id)

    return app


def run() -> None:
    uvicorn.run("fomo.api.app:create_app", factory=True, host="0.0.0.0", port=8000, reload=False)


# Conventional ASGI export for deployment tooling. It does not open a database
# connection until FastAPI lifespan starts.
app = create_app()
