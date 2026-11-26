"""Shared FastAPI dependencies, cookie/header extractors, and session shape helpers."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import Cookie, Depends, Header, Request, Response

from server.application.dialogue.orchestrator import MessageAction
from server.container import AppContainer
from server.domain.errors import ValidationError

SESSION_COOKIE_NAME = "sokoban_cocreation_access"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def get_container(request: Request) -> AppContainer:
    """Return the AppContainer bound to request.app.state."""
    container = getattr(getattr(request, "app", None), "state", None)
    container = getattr(container, "container", None)
    if container is None:
        raise RuntimeError("AppContainer is not initialized on request.app.state")
    return container


ContainerDep = Annotated[AppContainer, Depends(get_container)]
AccessCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)]
SessionTokenHeader = Annotated[str | None, Header(alias="X-Session-Token")]
PlayerTokenHeader = Annotated[str | None, Header(alias="X-Player-Token")]
DeletePasswordHeader = Annotated[str | None, Header(alias="X-Delete-Password")]
AuthorizationHeader = Annotated[str | None, Header(alias="Authorization")]
IntegrationTokenHeader = Annotated[str | None, Header(alias="X-Integration-Token")]


def get_request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def get_llm_deadline(request: Request) -> float | None:
    value = getattr(request.state, "llm_deadline", None)
    return float(value) if isinstance(value, (int, float)) else None


def validate_identifier(value: str | None, field_name: str) -> str:
    """Enforce safe bounded identifier characters on path/idempotency tokens."""
    clean = str(value or "")
    if not REQUEST_ID_PATTERN.fullmatch(clean):
        raise ValidationError(
            "INVALID_IDENTIFIER",
            "error.invalid_identifier",
            message=f"{field_name} is invalid.",
        )
    return clean


def extract_bearer_token(
    authorization: str | None,
    x_integration_token: str | None = None,
) -> str | None:
    """Extract Bearer token from Authorization header (with optional X-Integration-Token)."""
    if authorization:
        prefix, _, token = authorization.partition(" ")
        if prefix.casefold() == "bearer" and token.strip():
            return token.strip()
    if x_integration_token and x_integration_token.strip():
        return x_integration_token.strip()
    return None


def ensure_session_shape(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure optional read-model keys expected by the Preact workbench are always populated."""
    versions = payload.get("versions") or []
    turns = payload.get("turns") or []
    if "assessments" not in payload:
        first_assistant_by_vid: dict[str, str] = {}
        for turn in turns:
            vid = str(turn.get("versionId") or "")
            if not vid or vid in first_assistant_by_vid:
                continue
            move = (turn.get("guidance") or {}).get("move")
            if turn.get("role") == "assistant" and move == "observe_stage":
                first_assistant_by_vid[vid] = str(turn.get("turnId") or "")
        assessments: list[dict[str, str]] = []
        for ver in versions:
            vid = str(ver.get("versionId") or "")
            tid = ver.get("openingTurnId") or first_assistant_by_vid.get(vid)
            if tid:
                ver.setdefault("openingTurnId", tid)
                assessments.append({"versionId": vid, "assistantTurnId": str(tid)})
        payload["assessments"] = assessments
    return payload


ACTION_MAP: dict[str, MessageAction] = {
    "none": "send",
    "send": "send",
    "execute_revision": "execute_revision",
    "challenge_revision": "challenge_revision",
    "alternative_revision": "alternative_revision",
    "continue_challenge": "continue_challenge",
}
CHALLENGE_CHOICE_MAP: dict[str, str] = {
    "ai": "keep_proposal",
    "user": "revise_proposal",
    "retain_current": "cancel_challenge",
    "keep_proposal": "keep_proposal",
    "revise_proposal": "revise_proposal",
    "cancel_challenge": "cancel_challenge",
}


def validate_fields(payload: Any, *field_names: str) -> None:
    """Validate one or more identifier fields on a Pydantic request model."""
    for field_name in field_names:
        validate_identifier(getattr(payload, field_name), field_name)


def set_session_access_cookie(request: Request, response: Response, token: str) -> None:
    """Attach the HttpOnly session cookie with transport-aware Secure flag."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    is_https = request.url.scheme == "https" or forwarded_proto == "https"
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=is_https,
        max_age=60 * 60 * 24 * 30,
        path="/",
    )


async def resolve_challenge_proposal_id(
    container: AppContainer,
    session_id: str,
    *,
    proposal_id: str | None,
    assistant_turn_id: str | None,
    base_version_id: str,
) -> str | None:
    """Infer pending proposal ID when challenge_revision omits proposalId."""
    if proposal_id:
        return proposal_id
    payload = await container.session_read_model_service.get_session_payload(session_id)
    for item in reversed(payload.get("proposals") or []):
        if assistant_turn_id and item.get("assistantTurnId") == assistant_turn_id:
            return str(item.get("proposalId") or "") or None
        if item.get("status") == "pending" and item.get("baseVersionId") == base_version_id:
            return str(item.get("proposalId") or "") or None
    return None


__all__ = [
    "ACTION_MAP",
    "AccessCookie",
    "AuthorizationHeader",
    "CHALLENGE_CHOICE_MAP",
    "ContainerDep",
    "DeletePasswordHeader",
    "IntegrationTokenHeader",
    "PlayerTokenHeader",
    "REQUEST_ID_PATTERN",
    "SESSION_COOKIE_NAME",
    "SessionTokenHeader",
    "ensure_session_shape",
    "extract_bearer_token",
    "get_container",
    "get_llm_deadline",
    "get_request_id",
    "resolve_challenge_proposal_id",
    "set_session_access_cookie",
    "validate_fields",
    "validate_identifier",
]
