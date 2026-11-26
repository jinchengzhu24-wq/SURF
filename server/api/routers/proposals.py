"""Thin async routes for accepting or rejecting pending co-creation proposals."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from server.api.deps import (
    AccessCookie,
    ContainerDep,
    ensure_session_shape,
    validate_identifier,
)
from server.api.schemas import ProposalDecisionRequest

router = APIRouter()


@router.post("/sessions/{session_id}/proposals/{proposal_id}/decision")
async def decide_proposal(
    session_id: str,
    proposal_id: str,
    payload: ProposalDecisionRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    result = await container.proposal_service.decide_proposal(
        session_id,
        proposal_id,
        access_cookie,
        decision=payload.decision,
        base_version_id=payload.baseVersionId,
        idempotency_key=payload.idempotencyKey,
        reason=payload.reason,
    )
    return ensure_session_shape(result)
