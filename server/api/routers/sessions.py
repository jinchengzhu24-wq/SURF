"""Thin async routes for co-creation session lifecycle, language, translation, and finalize."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request, Response

from server.api.deps import (
    AccessCookie,
    ContainerDep,
    SessionTokenHeader,
    ensure_session_shape,
    get_llm_deadline,
    get_request_id,
    rewrite_loopback_url,
    set_session_access_cookie,
    validate_identifier,
)
from server.api.schemas import (
    BrowserAccessRequest,
    CreateSessionRequest,
    DemoSessionRequest,
    FinalizeRequest,
    IntentionRequest,
    LanguageRequest,
    TranslationRequest,
)
from server.domain.errors import AuthError
from server.settings import DEFAULT_PUBLIC_BASE_URL

router = APIRouter()


@router.post("/sessions")
async def create_session(
    payload: CreateSessionRequest, request: Request, container: ContainerDep
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.session_lifecycle_service.create_formal_session(
        rows=payload.rows,
        initial_draft_method=payload.initialDraftMethod,
        language=payload.language,
        idempotency_key=payload.idempotencyKey,
        match_id=payload.matchId,
        player_number=payload.playerNumber,
    )
    if "launchUrl" in result and isinstance(result["launchUrl"], str):
        result["launchUrl"] = rewrite_loopback_url(
            result["launchUrl"],
            request,
            configured_base_url=container.settings.public_base_url,
            default_base_url=DEFAULT_PUBLIC_BASE_URL,
        )
    return result


@router.post("/demo-sessions")
async def create_demo_session(
    payload: DemoSessionRequest, request: Request, container: ContainerDep
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.session_lifecycle_service.create_demo_session(
        language=payload.language, idempotency_key=payload.idempotencyKey
    )
    if "launchUrl" in result and isinstance(result["launchUrl"], str):
        result["launchUrl"] = rewrite_loopback_url(
            result["launchUrl"],
            request,
            configured_base_url=container.settings.public_base_url,
            default_base_url=DEFAULT_PUBLIC_BASE_URL,
        )
    return result


@router.post("/sessions/{session_id}/browser-access")
async def authorize_browser_access(
    session_id: str,
    payload: BrowserAccessRequest,
    request: Request,
    response: Response,
    container: ContainerDep,
) -> dict[str, str]:
    granted = await container.session_lifecycle_service.authorize_browser_access(
        session_id, payload.bootstrapToken
    )
    set_session_access_cookie(request, response, granted["accessToken"])
    return {"sessionId": granted["sessionId"]}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
    session_token: SessionTokenHeader = None,
) -> dict[str, Any]:
    try:
        container.session_lifecycle_service.require_browser_session(session_id, access_cookie)
    except AuthError:
        if not session_token:
            raise
        container.session_lifecycle_service.require_browser_session(session_id, session_token)
    return ensure_session_shape(
        await container.session_read_model_service.get_session_payload(session_id)
    )


@router.patch("/sessions/{session_id}/language")
async def update_language(
    session_id: str,
    payload: LanguageRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    return ensure_session_shape(
        await container.session_lifecycle_service.update_language(
            session_id, access_cookie, language=payload.language
        )
    )


@router.post("/sessions/{session_id}/translations/{lang}")
async def translate_session_turns(
    session_id: str,
    lang: Literal["en", "zh-CN"],
    payload: TranslationRequest,
    request: Request,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    result = await container.translation_service.translate_turns(
        session_id,
        access_cookie,
        target_language=lang,
        turn_ids=payload.turnIds,
        request_id=get_request_id(request),
        deadline=get_llm_deadline(request),
    )
    return ensure_session_shape(result)


@router.post("/sessions/{session_id}/finalize")
async def finalize_session(
    session_id: str,
    payload: FinalizeRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.session_lifecycle_service.finalize_session(
        session_id,
        access_cookie,
        base_version_id=payload.baseVersionId,
        idempotency_key=payload.idempotencyKey,
        rows=payload.rows,
    )
    return ensure_session_shape(result)


@router.post("/sessions/{session_id}/intention")
async def submit_intention(
    session_id: str,
    payload: IntentionRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.session_lifecycle_service.submit_intention(
        session_id, access_cookie, content=payload.content, idempotency_key=payload.idempotencyKey
    )
    return ensure_session_shape(result)
