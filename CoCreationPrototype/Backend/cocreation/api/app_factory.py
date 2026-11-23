"""FastAPI application factory for the 8010 co-creation service."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from cocreation.llm.facade import LLMServiceError
from cocreation.reliability.reply_reliability import REPLY_PIPELINE_VERSION

from . import routes_messages_and_proposals, routes_sessions
from .config_models import (
    ApiError,
    FRONTEND_DIR,
    REQUEST_ID_PATTERN,
    _llm_failure_details,
    error_response,
)
from .services import CoCreationServices, build_default_services
from .session_persistence_helpers import _draft_regeneration_sweeper


def create_app(services: CoCreationServices | None = None) -> FastAPI:
    """Build an isolated FastAPI application backed by explicit CoCreationServices."""
    active_services = services if services is not None else build_default_services()

    @asynccontextmanager
    async def app_lifespan(target_app: FastAPI):
        app_services: CoCreationServices = target_app.state.services
        app_services.repository.initialize()
        if not app_services.settings.sweeper_enabled:
            yield
            return
        sweeper = asyncio.create_task(_draft_regeneration_sweeper(app_services))
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper

    created_app = FastAPI(
        title="Sokoban Co-Creation Prototype",
        version="1.0.0",
        lifespan=app_lifespan,
    )
    created_app.state.services = active_services
    created_app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_services.settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Request-ID", "X-Session-Token"],
    )

    @created_app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request_services: CoCreationServices = request.app.state.services
        supplied_request_id = request.headers.get("X-Request-ID", "").strip()
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else uuid.uuid4().hex
        )
        request.state.request_id = request_id
        request.state.llm_started_at = request_services.monotonic()
        request.state.llm_deadline = (
            request.state.llm_started_at + request_services.settings.llm_internal_deadline_seconds
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Reply-Pipeline-Version"] = REPLY_PIPELINE_VERSION
        if request.url.path in {"/", "/index.html", "/cocreation/", "/cocreation/index.html"}:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @created_app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(request: Request, exception):
        return error_response(
            400,
            "INVALID_REQUEST",
            "The request body does not match the API contract.",
            request.state.request_id,
            False,
        )

    @created_app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exception: ApiError):
        return error_response(
            exception.status_code,
            exception.code,
            exception.message,
            request.state.request_id,
            exception.retryable,
            exception.details,
        )

    @created_app.exception_handler(LLMServiceError)
    async def handle_llm_service_error(request: Request, exception: LLMServiceError):
        response = error_response(
            exception.status_code,
            exception.code,
            exception.safe_message,
            request.state.request_id,
            exception.retryable,
            _llm_failure_details(exception),
        )
        response.headers["X-LLM-Attempts-Used"] = str(exception.attempts_used)
        return response

    created_app.include_router(routes_sessions.router)
    created_app.include_router(routes_messages_and_proposals.router)
    created_app.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIR, html=True, check_dir=False),
        name="prototype-frontend",
    )
    return created_app
