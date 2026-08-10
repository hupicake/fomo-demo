"""FastAPI control-plane API with persistent SSE replay."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import (
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

from fomo.agent_framework import AgentFramework
from fomo.config import Settings
from fomo.fomo_pi_ds.gateway import InferenceGatewayError, LiteLLMRunKeyClient
from fomo.persistence import (
    AuthenticationError,
    ConflictError,
    Database,
    FilePathError,
    NotFoundError,
    OwnershipError,
    Repository,
)
from fomo.runtime_contract import (
    DEFAULT_PROFILE_ID,
    RUNTIME_PROFILES,
    RuntimeContractError,
    legacy_runtime_contract,
    resolve_runtime_contract,
)
from fomo.schemas import (
    AgentFrameworkOption,
    ArtifactDetailResponse,
    AuthSessionResponse,
    FileContentResponse,
    FileContentUpdate,
    MessageCreate,
    MessageRunResponse,
    PreviewResponse,
    ProjectCreate,
    ProjectPatch,
    ProjectResponse,
    ProjectSnapshotResponse,
    RunResponse,
    RuntimeOptionsResponse,
    RuntimeProfileOption,
    TraceResponse,
    UserInputAnswerCreate,
    UserInputAnswerResponse,
    UserLogin,
    UserRegister,
    UserResponse,
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


async def _stream_run_events(
    request: Request,
    repository: Repository,
    run_id: str,
    owner_session_id: str,
    cursor: int,
    *,
    poll_interval_seconds: float = 0.5,
) -> AsyncIterator[dict[str, str]]:
    """Replay events while continuously enforcing the original read grant."""
    while not await request.is_disconnected():
        try:
            run = await repository.get_run(run_id)
            await repository.require_project(run.project_id, owner_session_id)
        except (NotFoundError, OwnershipError):
            # Headers have already been sent for an SSE response. Ending the
            # stream is the only non-leaking response to a revoked/expired
            # session or ownership change.
            return
        events = await repository.list_events(run_id, after=cursor)
        for event in events:
            cursor = event.seq
            yield {
                "id": str(event.seq),
                "event": "run.event",
                "data": event.model_dump_json(by_alias=True),
            }
        if run.status.value in {"succeeded", "failed", "cancelled", "needs_attention"}:
            return
        await asyncio.sleep(poll_interval_seconds)


def create_app(settings: Settings | None = None, repository: Repository | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_database = repository is None
    database = repository.database if repository is not None else Database(settings.database_url)
    repository = repository or Repository(database)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await repository.initialize()
        if (
            settings.app_env == "development"
            and settings.dev_account_email
            and settings.dev_account_password
        ):
            await repository.ensure_development_user(
                settings.dev_account_email,
                settings.dev_account_password,
                settings.dev_account_display_name,
            )
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
        allow_headers=["Content-Type", "Idempotency-Key", "Last-Event-ID"],
    )

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return problem(404, "Not Found", str(exc))

    @app.exception_handler(OwnershipError)
    async def _forbidden(_: Request, exc: OwnershipError) -> JSONResponse:
        return problem(403, "Forbidden", str(exc))

    @app.exception_handler(AuthenticationError)
    async def _unauthorized(_: Request, exc: AuthenticationError) -> JSONResponse:
        return problem(401, "Unauthorized", str(exc))

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

    async def session_id(request: Request) -> str:
        candidate = request.cookies.get(settings.session_cookie_key)
        if not candidate:
            raise HTTPException(status_code=401, detail="authenticated session is required")
        try:
            await repository.get_current_user(candidate)
        except (AuthenticationError, NotFoundError) as exc:
            raise HTTPException(
                status_code=401,
                detail="authenticated session is invalid or expired",
            ) from exc
        return candidate

    SessionId = Annotated[str, Depends(session_id)]

    def set_session_cookie(response: Response, session_value: str, max_age: int) -> None:
        response.set_cookie(
            key=settings.session_cookie_key,
            value=session_value,
            max_age=max_age,
            path="/",
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="lax",
        )

    def auth_session_response(user, auth_session) -> AuthSessionResponse:
        return AuthSessionResponse(
            expires_at=auth_session.expires_at,
            user=UserResponse(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                created_at=user.created_at,
            ),
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def runtime_availability() -> tuple[set[str], bool]:
        """Resolve public availability without exposing provider routing details."""
        enabled = set(settings.runtime_enabled_profiles)
        if settings.agent_framework != "direct_pi":
            return set(), True
        if not settings.litellm_api_key:
            return set(), False
        gateway = LiteLLMRunKeyClient(
            management_url=settings.litellm_management_url,
            master_key=settings.litellm_api_key,
            timeout_seconds=settings.inference_management_timeout_seconds,
        )
        try:
            discovered = await gateway.discover_model_aliases()
        except InferenceGatewayError:
            return set(), False
        return {
            profile.profile_id
            for profile in RUNTIME_PROFILES
            if profile.profile_id in enabled and profile.litellm_alias in discovered
        }, True

    @app.get("/v1/runtime/options", response_model=RuntimeOptionsResponse)
    async def runtime_options() -> RuntimeOptionsResponse:
        available_profiles, discovery_succeeded = await runtime_availability()
        configured_default = settings.runtime_default_profile
        if configured_default in available_profiles:
            default_profile_id: str | None = configured_default
        elif DEFAULT_PROFILE_ID in available_profiles:
            default_profile_id = DEFAULT_PROFILE_ID
        else:
            default_profile_id = min(available_profiles, default=None)
        enabled = set(settings.runtime_enabled_profiles)
        options: list[RuntimeProfileOption] = []
        for profile in RUNTIME_PROFILES:
            available = profile.profile_id in available_profiles
            disabled_reason: str | None = None
            if not available:
                if profile.profile_id not in enabled:
                    disabled_reason = "Not enabled by the server."
                elif not discovery_succeeded:
                    disabled_reason = "Model availability could not be verified."
                else:
                    disabled_reason = "The configured model route is unavailable."
            options.append(
                RuntimeProfileOption(
                    profile_id=profile.profile_id,
                    label=profile.label,
                    thinking_levels=list(profile.thinking_levels),
                    default_thinking=profile.default_thinking,
                    context_window=profile.context_window,
                    run_token_budget=None,
                    run_token_budget_unlimited=True,
                    inference_tpm_limit=min(
                        profile.inference_tpm_limit,
                        settings.run_inference_tpm_limit,
                    ),
                    available=available,
                    disabled_reason=disabled_reason,
                )
            )
        return RuntimeOptionsResponse(
            default_profile_id=default_profile_id,
            profiles=options,
            default_agent_framework=AgentFramework(settings.agent_default_framework),
            agent_frameworks=[
                AgentFrameworkOption(
                    id=framework,
                    label="Pi" if framework is AgentFramework.pi else "OpenCode",
                    available=(
                        settings.agent_framework == "direct_pi"
                        and framework.value in settings.agent_enabled_frameworks
                    ),
                    disabled_reason=(
                        None
                        if (
                            settings.agent_framework == "direct_pi"
                            and framework.value in settings.agent_enabled_frameworks
                        )
                        else "Not enabled by the server."
                    ),
                )
                for framework in AgentFramework
            ],
        )

    @app.post(
        "/v1/auth/register",
        response_model=AuthSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def register(
        payload: UserRegister,
        response: Response,
    ) -> AuthSessionResponse:
        if settings.app_env == "production":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Registration is disabled.",
            )
        user, auth_session = await repository.register_user(
            payload.email,
            payload.password,
            payload.display_name,
        )
        set_session_cookie(response, auth_session.id, 30 * 24 * 60 * 60)
        return auth_session_response(user, auth_session)

    @app.post("/v1/auth/login", response_model=AuthSessionResponse)
    async def login(
        payload: UserLogin,
        response: Response,
    ) -> AuthSessionResponse:
        user, auth_session = await repository.authenticate_user(
            payload.email,
            payload.password,
        )
        set_session_cookie(response, auth_session.id, 30 * 24 * 60 * 60)
        return auth_session_response(user, auth_session)

    @app.get("/v1/auth/me", response_model=UserResponse)
    async def current_user(owner_session_id: SessionId) -> UserResponse:
        user = await repository.get_current_user(owner_session_id)
        return UserResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
        )

    @app.post("/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(response: Response, owner_session_id: SessionId) -> None:
        await repository.get_current_user(owner_session_id)
        await repository.revoke_session(owner_session_id)
        response.delete_cookie(
            key=settings.session_cookie_key,
            path="/",
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="lax",
        )

    @app.get("/v1/projects", response_model=list[ProjectResponse])
    async def list_projects(owner_session_id: SessionId) -> list[ProjectResponse]:
        return await repository.list_projects(owner_session_id)

    @app.post("/v1/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
    async def create_project(
        payload: ProjectCreate, owner_session_id: SessionId
    ) -> ProjectResponse:
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
            goal_graph=snapshot["goal_graph"],
            pending_input_request=snapshot["pending_input_request"],
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
            raise HTTPException(
                status_code=422, detail="Idempotency-Key must match clientMessageId"
            )
        existing = await repository.get_message_run_by_client_id(
            project_id,
            owner_session_id,
            payload.client_message_id,
        )
        if existing is not None:
            existing_message, existing_run = existing
            if (
                payload.content != existing_message.content
                or (
                    payload.base_version_id is not None
                    and payload.base_version_id != existing_run.base_version_id
                )
                or (
                    payload.profile_id is not None
                    and payload.profile_id != existing_run.runtime.profile_id
                )
                or (
                    payload.thinking is not None
                    and payload.thinking != existing_run.runtime.thinking
                )
                or (
                    payload.agent_framework is not None
                    and payload.agent_framework != existing_run.agent_framework
                )
            ):
                raise ConflictError(
                    "Idempotency-Key was already used with a different request"
                )
            response.status_code = status.HTTP_200_OK
            return MessageRunResponse(message=existing_message, run=existing_run)
        selected_agent_framework = (
            payload.agent_framework.value
            if payload.agent_framework is not None
            else settings.agent_default_framework
        )
        if selected_agent_framework not in settings.agent_enabled_frameworks:
            raise HTTPException(status_code=422, detail="agent framework is not enabled")
        if settings.agent_framework != "direct_pi":
            if (
                payload.agent_framework is not None
                or payload.profile_id is not None
                or payload.thinking is not None
            ):
                raise HTTPException(
                    status_code=422,
                    detail="runtime selection requires the Direct Pi framework",
                )
            message, run, created = await repository.create_message_and_run(
                project_id,
                owner_session_id,
                payload.client_message_id,
                payload.content,
                payload.base_version_id,
                agent_framework=selected_agent_framework,
                runtime_contract=legacy_runtime_contract(),
                enforce_agent_framework_match=(
                    "agent_framework" in payload.model_fields_set
                ),
            )
            response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
            return MessageRunResponse(message=message, run=run)
        available_profiles, discovery_succeeded = await runtime_availability()
        selected_profile = payload.profile_id
        if selected_profile is None:
            if settings.runtime_default_profile in available_profiles:
                selected_profile = settings.runtime_default_profile
            elif DEFAULT_PROFILE_ID in available_profiles:
                selected_profile = DEFAULT_PROFILE_ID
            else:
                selected_profile = min(available_profiles, default=None)
        if selected_profile is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "runtime model availability could not be verified"
                    if not discovery_succeeded
                    else "no runtime profile is available"
                ),
            )
        if selected_profile not in settings.runtime_enabled_profiles:
            raise HTTPException(status_code=422, detail="runtime profile is not enabled")
        if not discovery_succeeded:
            raise HTTPException(
                status_code=503,
                detail="runtime model availability could not be verified",
            )
        if selected_profile not in available_profiles:
            raise HTTPException(status_code=422, detail="runtime profile is unavailable")
        try:
            runtime_contract = resolve_runtime_contract(
                selected_profile,
                payload.thinking,
                inference_tpm_limit=settings.run_inference_tpm_limit,
                max_spend_micros=int(settings.run_max_spend * 1_000_000),
            )
        except RuntimeContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        message, run, created = await repository.create_message_and_run(
            project_id,
            owner_session_id,
            payload.client_message_id,
            payload.content,
            payload.base_version_id,
            agent_framework=selected_agent_framework,
            runtime_contract=runtime_contract,
            enforce_runtime_match=bool(
                {"profile_id", "thinking"}.intersection(payload.model_fields_set)
            ),
            enforce_agent_framework_match=(
                "agent_framework" in payload.model_fields_set
            ),
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
            raise HTTPException(
                status_code=422, detail="Last-Event-ID must be an integer sequence"
            ) from exc

        return EventSourceResponse(
            _stream_run_events(
                request,
                repository,
                run_id,
                owner_session_id,
                cursor,
            ),
            ping=15,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunResponse)
    async def cancel_run(run_id: str, owner_session_id: SessionId) -> RunResponse:
        await _owned_run(run_id, owner_session_id)
        return await repository.request_cancel(run_id)

    @app.post(
        "/v1/runs/{run_id}/input-requests/{request_id}/answer",
        response_model=UserInputAnswerResponse,
    )
    async def answer_run_input_request(
        run_id: str,
        request_id: str,
        payload: UserInputAnswerCreate,
        response: Response,
        owner_session_id: SessionId,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> UserInputAnswerResponse:
        if idempotency_key and idempotency_key != payload.client_message_id:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key must match clientMessageId",
            )
        await _owned_run(run_id, owner_session_id)
        message, input_request, run, created = await repository.answer_user_input(
            run_id,
            request_id,
            owner_session_id,
            payload.client_message_id,
            payload.answer,
        )
        response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return UserInputAnswerResponse(
            message=message,
            request=input_request,
            run=run,
        )

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
