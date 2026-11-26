"""Thin async routes for co-creation Stage versions and draft regeneration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from server.api.deps import (
    AccessCookie,
    ContainerDep,
    ensure_session_shape,
    get_llm_deadline,
    get_request_id,
    rewrite_loopback_url,
    validate_identifier,
)
from server.api.schemas import (
    AssessmentRequest,
    DraftRegenerationRequest,
    PlayAttemptRequest,
    RestoreRequest,
    VersionRequest,
)
from server.settings import DEFAULT_PUBLIC_BASE_URL, DEFAULT_WEBGL_BASE_URL

router = APIRouter()


@router.post("/sessions/{session_id}/versions")
async def save_version(
    session_id: str,
    payload: VersionRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.stage_service.save_version(
        session_id,
        access_cookie,
        base_version_id=payload.baseVersionId,
        rows=payload.rows,
        idempotency_key=payload.idempotencyKey,
        summary=payload.summary,
    )
    return ensure_session_shape(result)


@router.post("/sessions/{session_id}/versions/{version_id}/restore")
async def restore_version(
    session_id: str,
    version_id: str,
    payload: RestoreRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.stage_service.restore_version(
        session_id,
        version_id,
        access_cookie,
        base_version_id=payload.baseVersionId,
        idempotency_key=payload.idempotencyKey,
    )
    return ensure_session_shape(result)


@router.post("/sessions/{session_id}/versions/{version_id}/assessments")
async def assess_version(
    session_id: str,
    version_id: str,
    payload: AssessmentRequest,
    request: Request,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.stage_service.assess_version(
        session_id,
        version_id,
        access_cookie,
        idempotency_key=payload.idempotencyKey,
        request_id=get_request_id(request),
        deadline=get_llm_deadline(request),
    )
    return ensure_session_shape(result)


@router.post("/sessions/{session_id}/versions/{version_id}/play-attempts")
async def create_play_attempt(
    session_id: str,
    version_id: str,
    payload: PlayAttemptRequest,
    request: Request,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.stage_service.create_play_attempt(
        session_id, version_id, access_cookie, idempotency_key=payload.idempotencyKey
    )
    if "playUrl" in result and isinstance(result["playUrl"], str):
        result["playUrl"] = rewrite_loopback_url(
            result["playUrl"],
            request,
            configured_base_url=container.settings.webgl_base_url,
            default_base_url=DEFAULT_WEBGL_BASE_URL,
        )
    if "returnUrl" in result and isinstance(result["returnUrl"], str):
        result["returnUrl"] = rewrite_loopback_url(
            result["returnUrl"],
            request,
            configured_base_url=container.settings.public_base_url,
            default_base_url=DEFAULT_PUBLIC_BASE_URL,
        )
    return result


@router.post("/sessions/{session_id}/draft-regenerations")
async def request_draft_regeneration(
    session_id: str,
    payload: DraftRegenerationRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.stage_service.request_draft_regeneration(
        session_id, access_cookie, idempotency_key=payload.idempotencyKey
    )
    return ensure_session_shape(result)


@router.post("/sessions/{session_id}/draft-regenerations/{request_id}/cancel")
async def cancel_draft_regeneration(
    session_id: str,
    request_id: str,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    result = await container.stage_service.cancel_draft_regeneration(
        session_id, request_id, access_cookie
    )
    return ensure_session_shape(result)
