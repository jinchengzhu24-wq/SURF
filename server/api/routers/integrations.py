"""Thin async routes for Unity integration polling and draft regeneration callbacks."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from server.api.deps import (
    AuthorizationHeader,
    ContainerDep,
    IntegrationTokenHeader,
    extract_bearer_token,
)
from server.api.schemas import DraftRegenerationCompleteRequest, DraftRegenerationFailRequest

router = APIRouter()


@router.get("/integrations/sessions/{session_id}")
async def integration_status(
    session_id: str,
    container: ContainerDep,
    authorization: AuthorizationHeader = None,
    x_integration_token: IntegrationTokenHeader = None,
) -> dict[str, Any]:
    token = extract_bearer_token(authorization, x_integration_token)
    return await container.session_read_model_service.get_integration_status(session_id, token)


@router.post("/integrations/sessions/{session_id}/draft-regenerations/{request_id}/claim")
async def claim_draft_regeneration(
    session_id: str,
    request_id: str,
    container: ContainerDep,
    authorization: AuthorizationHeader = None,
    x_integration_token: IntegrationTokenHeader = None,
) -> dict[str, Any]:
    token = extract_bearer_token(authorization, x_integration_token)
    return await container.stage_service.claim_draft_regeneration(session_id, request_id, token)


@router.post("/integrations/sessions/{session_id}/draft-regenerations/{request_id}/complete")
async def complete_draft_regeneration(
    session_id: str,
    request_id: str,
    payload: DraftRegenerationCompleteRequest,
    container: ContainerDep,
    authorization: AuthorizationHeader = None,
    x_integration_token: IntegrationTokenHeader = None,
) -> dict[str, Any]:
    token = extract_bearer_token(authorization, x_integration_token)
    return await container.stage_service.complete_draft_regeneration(
        session_id, request_id, token, rows=payload.rows
    )


@router.post("/integrations/sessions/{session_id}/draft-regenerations/{request_id}/fail")
async def fail_draft_regeneration(
    session_id: str,
    request_id: str,
    payload: DraftRegenerationFailRequest,
    container: ContainerDep,
    authorization: AuthorizationHeader = None,
    x_integration_token: IntegrationTokenHeader = None,
) -> dict[str, Any]:
    token = extract_bearer_token(authorization, x_integration_token)
    return await container.stage_service.fail_draft_regeneration(
        session_id, request_id, token, failure_code=payload.failureCode
    )
