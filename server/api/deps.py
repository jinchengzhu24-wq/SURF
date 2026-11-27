"""Shared FastAPI dependencies, cookie/header extractors, and session shape helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Annotated, Any
from urllib.parse import urlparse

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


def _extract_version_assessment(ver: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = ver.get("assessment")
    if raw is None:
        raw = ver.get("assessment_json")
    if raw is None:
        raw = ver.get("assessmentJson")
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return None
        return dict(parsed) if isinstance(parsed, Mapping) else None
    if isinstance(raw, Mapping):
        return dict(raw)
    return None


def ensure_session_shape(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure optional read-model keys expected by the Preact workbench are always populated."""
    versions = payload.get("versions") or []
    turns = payload.get("turns") or []

    first_observe_by_vid: dict[str, str] = {}
    first_assistant_by_vid: dict[str, str] = {}
    for turn in turns:
        if turn.get("role") != "assistant":
            continue
        vid = str(turn.get("versionId") or "")
        tid = str(turn.get("turnId") or "")
        if not vid or not tid:
            continue
        first_assistant_by_vid.setdefault(vid, tid)
        move = (turn.get("guidance") or {}).get("move")
        if move == "observe_stage" and vid not in first_observe_by_vid:
            first_observe_by_vid[vid] = tid

    if "assessments" not in payload:
        assessments: list[dict[str, Any]] = []
        for ver in versions:
            vid = str(ver.get("versionId") or "")
            if not vid:
                continue
            ver_assessment = _extract_version_assessment(ver)
            tid = (
                ver.get("openingTurnId")
                or (ver_assessment.get("assistantTurnId") if ver_assessment else None)
                or first_observe_by_vid.get(vid)
                or (first_assistant_by_vid.get(vid) if ver_assessment is not None else None)
            )
            if tid:
                ver.setdefault("openingTurnId", str(tid))
            if tid or ver_assessment is not None:
                entry: dict[str, Any] = {
                    "versionId": vid,
                    "assistantTurnId": str(tid or ""),
                }
                if ver_assessment is not None:
                    entry["assessment"] = ver_assessment
                assessments.append(entry)
        payload["assessments"] = assessments
    elif isinstance(payload["assessments"], list):
        existing_by_vid = {
            str(item.get("versionId") or ""): item
            for item in payload["assessments"]
            if isinstance(item, dict) and item.get("versionId")
        }
        for ver in versions:
            vid = str(ver.get("versionId") or "")
            if not vid:
                continue
            ver_assessment = _extract_version_assessment(ver)
            if ver_assessment is None:
                continue
            tid = (
                ver.get("openingTurnId")
                or ver_assessment.get("assistantTurnId")
                or first_observe_by_vid.get(vid)
                or first_assistant_by_vid.get(vid)
            )
            if tid:
                ver.setdefault("openingTurnId", str(tid))
            existing = existing_by_vid.get(vid)
            if existing is None:
                payload["assessments"].append(
                    {
                        "versionId": vid,
                        "assistantTurnId": str(tid or ""),
                        "assessment": ver_assessment,
                    }
                )
            else:
                if tid and not existing.get("assistantTurnId"):
                    existing["assistantTurnId"] = str(tid)
                existing.setdefault("assessment", ver_assessment)
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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def rewrite_loopback_url(
    url: str,
    request: Request,
    *,
    configured_base_url: str,
    default_base_url: str,
) -> str:
    """Rewrite default production URLs to the request loopback origin for local dev."""
    hostname = (request.url.hostname or "").casefold()
    if hostname not in _LOOPBACK_HOSTS:
        return url
    if configured_base_url.strip().rstrip("/") != default_base_url.strip().rstrip("/"):
        return url
    parsed_default = urlparse(default_base_url)
    default_origin = f"{parsed_default.scheme}://{parsed_default.netloc}"
    if not default_origin or not url.startswith(default_origin):
        return url
    port = request.url.port
    request_origin = (
        f"{request.url.scheme}://{request.url.hostname}:{port}"
        if port is not None
        else f"{request.url.scheme}://{request.url.hostname}"
    )
    return f"{request_origin}{url[len(default_origin):]}"


def set_session_access_cookie(request: Request, response: Response, token: str) -> None:
    """Attach the HttpOnly session cookie with transport-aware Secure flag."""
    is_https = request.url.scheme == "https"
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
    """Infer matching pending proposal ID when challenge_revision omits proposalId."""
    if proposal_id:
        return proposal_id
    payload = await container.session_read_model_service.get_session_payload(session_id)
    proposals = payload.get("proposals") or []
    if assistant_turn_id:
        for item in reversed(proposals):
            if (
                item.get("assistantTurnId") == assistant_turn_id
                and item.get("status") == "pending"
                and item.get("baseVersionId") == base_version_id
            ):
                return str(item.get("proposalId") or "") or None
        return None
    for item in reversed(proposals):
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
    "rewrite_loopback_url",
    "set_session_access_cookie",
    "validate_fields",
    "validate_identifier",
]
