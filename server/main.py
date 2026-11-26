"""Unified FastAPI application factory and ASGI entrypoint (port 8000)."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.api.deps import REQUEST_ID_PATTERN
from server.api.routers import (
    dashboard,
    dialogue,
    health,
    integrations,
    online,
    play_attempts,
    proposals,
    sessions,
    stage,
    study,
)
from server.container import AppContainer, build_container
from server.core.settings import redact_secrets
from server.domain.errors import (
    AuthError,
    ConflictError,
    DeadlineExceededError,
    DomainError,
    IdempotencyConflictError,
    NotFoundError,
    RateLimitError,
    UpstreamLLMError,
    ValidationError,
)
from server.i18n.messages import get_message
from server.settings import SokobanSettings

logger = logging.getLogger(__name__)

mimetypes.add_type("application/octet-stream", ".data")
mimetypes.add_type("application/wasm", ".wasm")

_NO_CACHE_PATHS = frozenset(
    {
        "/frontend",
        "/frontend/",
        "/frontend/index.html",
        "/cocreation",
        "/cocreation/",
        "/cocreation/index.html",
    }
)


def _domain_error_status(exc: DomainError) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, AuthError):
        return 401
    if isinstance(exc, (IdempotencyConflictError, ConflictError)):
        return 409
    if isinstance(exc, ValidationError):
        return 400
    if isinstance(exc, RateLimitError):
        return 429
    if isinstance(exc, UpstreamLLMError):
        return 502
    if isinstance(exc, DeadlineExceededError):
        return 504
    if exc.code in {"LLM_NOT_CONFIGURED", "DELETE_PASSWORD_NOT_CONFIGURED"}:
        return 503
    return 503 if exc.retryable else 400


def _error_envelope(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any],
    retryable: bool,
    request_id: str,
) -> JSONResponse:
    safe_error = {
        "code": str(code),
        "message": str(message),
        "details": dict(details),
        "retryable": bool(retryable),
    }
    return JSONResponse(
        status_code=status_code,
        content={
            "error": safe_error,
            "code": safe_error["code"],
            "message": safe_error["message"],
            "details": safe_error["details"],
            "retryable": safe_error["retryable"],
            "requestId": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


async def _room_reaper_loop(container: AppContainer, interval_seconds: float) -> None:
    interval = max(1.0, float(interval_seconds))
    secrets_tuple = container.settings.secret_values()
    while True:
        await asyncio.sleep(interval)
        try:
            await container.online_room_service.reap_expired_rooms()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Background room reaper iteration failed: %s",
                redact_secrets(repr(exc), secrets_tuple),
            )


def create_app(
    settings: SokobanSettings | None = None,
    container: AppContainer | None = None,
) -> FastAPI:
    """Create the unified Sokoban FastAPI application with explicit DI and lifespan."""
    resolved_container = (
        container if container is not None else build_container(settings=settings)
    )
    active_settings = resolved_container.settings

    @asynccontextmanager
    async def lifespan(target_app: FastAPI):
        app_container: AppContainer = target_app.state.container
        app_container.db.initialize()
        app_container.settings.warn_if_default_token_secret()
        reaper_task: asyncio.Task[None] | None = None
        if app_container.settings.reaper_enabled:
            reaper_task = asyncio.create_task(
                _room_reaper_loop(
                    app_container, app_container.settings.room_reaper_interval_seconds
                )
            )
        try:
            yield
        finally:
            if reaper_task is not None:
                reaper_task.cancel()
                with suppress(asyncio.CancelledError):
                    await reaper_task
            await app_container.close()

    application = FastAPI(
        title="Sokoban AI Unified Platform",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.container = resolved_container

    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-LLM-Attempts-Used", "X-DG-Guide-Source"],
    )

    @application.middleware("http")
    async def attach_request_context(request: Request, call_next):
        supplied = str(request.headers.get("X-Request-ID") or "").strip()
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else uuid.uuid4().hex
        request.state.request_id = request_id
        started_at = resolved_container.clock.monotonic()
        request.state.llm_started_at = started_at
        request.state.llm_deadline = (
            started_at + active_settings.cocreation_llm_internal_deadline_seconds
        )
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request_id)
        if request.url.path in _NO_CACHE_PATHS:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @application.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        req_id = str(getattr(request.state, "request_id", "") or uuid.uuid4().hex)
        secrets_tuple = active_settings.secret_values()
        raw_message = str(exc)
        if not raw_message or raw_message == exc.message_key:
            raw_message = get_message(exc.message_key, "en")
        safe_message = redact_secrets(raw_message, secrets_tuple)[:400]
        return _error_envelope(
            status_code=_domain_error_status(exc),
            code=exc.code,
            message=safe_message,
            details=dict(exc.details),
            retryable=exc.retryable,
            request_id=req_id,
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        req_id = str(getattr(request.state, "request_id", "") or uuid.uuid4().hex)
        fields = [
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "type": str(err.get("type") or ""),
                "msg": redact_secrets(
                    str(err.get("msg") or ""), active_settings.secret_values()
                )[:200],
            }
            for err in exc.errors()[:10]
        ]
        return _error_envelope(
            status_code=400,
            code="INVALID_REQUEST",
            message="The request payload did not match the API contract.",
            details={"errors": fields},
            retryable=False,
            request_id=req_id,
        )

    @application.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        req_id = str(getattr(request.state, "request_id", "") or uuid.uuid4().hex)
        safe_detail = redact_secrets(
            str(exc.detail or "Request failed."), active_settings.secret_values()
        )[:300]
        return _error_envelope(
            status_code=exc.status_code,
            code=f"HTTP_{exc.status_code}",
            message=safe_detail,
            details={},
            retryable=exc.status_code >= 500 or exc.status_code == 429,
            request_id=req_id,
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        req_id = str(getattr(request.state, "request_id", "") or uuid.uuid4().hex)
        safe_err = redact_secrets(repr(exc), active_settings.secret_values())
        logger.error("Unhandled request error request_id=%s error=%s", req_id, safe_err)
        return _error_envelope(
            status_code=500,
            code="INTERNAL_ERROR",
            message=get_message("error.internal", "en"),
            details={},
            retryable=False,
            request_id=req_id,
        )

    # Health & readiness probes at root and /cocreation prefix
    application.include_router(health.router)
    application.include_router(health.router, prefix="/cocreation")

    # Online matchmaking, study/DG, and dashboard routes
    application.include_router(online.router)
    application.include_router(study.router)
    application.include_router(dashboard.router)

    # Co-creation routes mounted under BOTH /api and /cocreation/api
    for api_prefix in ("/api", "/cocreation/api"):
        application.include_router(sessions.router, prefix=api_prefix)
        application.include_router(stage.router, prefix=api_prefix)
        application.include_router(dialogue.router, prefix=api_prefix)
        application.include_router(proposals.router, prefix=api_prefix)
        application.include_router(integrations.router, prefix=api_prefix)
        application.include_router(play_attempts.router, prefix=api_prefix)

    # Built static frontends & Unity WebGL build
    if active_settings.dashboard_dist_dir.exists():
        application.mount(
            "/frontend",
            StaticFiles(directory=str(active_settings.dashboard_dist_dir), html=True),
            name="frontend",
        )
    if active_settings.cocreation_dist_dir.exists():
        application.mount(
            "/cocreation",
            StaticFiles(directory=str(active_settings.cocreation_dist_dir), html=True),
            name="cocreation",
        )
    if active_settings.webgl_build_dir.exists():
        application.mount(
            "/game",
            StaticFiles(directory=str(active_settings.webgl_build_dir), html=True),
            name="game",
        )

    return application


__all__ = ["create_app"]
