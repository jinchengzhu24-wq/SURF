"""Pure validation, auth, and serialization helpers for OnlineRoomService."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from typing import Any, NoReturn

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from server.core.settings import safe_compare_digest
from server.domain.blueprints import (
    DG_ANSWER_CHOICES,
    ONLINE_DRAFT_METADATA_KEYS,
    ONLINE_DRAFT_RECOMMENDATION_SOURCES,
)
from server.domain.errors import AuthError, ConflictError, NotFoundError, ValidationError
from server.domain.rooms import (
    ROOM_CODE_ALPHABET,
    ROOM_CODE_PATTERN,
    CoCreationFlowEvent,
    OnlineRoom,
    serialize_public_room,
)
from server.persistence.errors import (
    ConflictError as PersistenceConflictError,
)
from server.persistence.errors import (
    NotFoundError as PersistenceNotFoundError,
)
from server.persistence.errors import (
    PersistenceError,
)
from server.persistence.models import OnlineRoomRecord

ONLINE_AI_ASSISTANT_MODES: frozenset[str] = frozenset({"description_generation"})
_FLOW_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(CoCreationFlowEvent)


def hash_room_token(raw_token: str) -> str:
    """Return canonical SHA-256 hex digest for a room player token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_room_code() -> str:
    """Generate a 6-character uppercase room code from the unambiguous alphabet."""
    return "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(6))


def normalize_room_code(room_code: str | None) -> str:
    """Normalize and validate a 6-character matchmaking room code."""
    normalized = str(room_code or "").strip().upper()
    if not ROOM_CODE_PATTERN.fullmatch(normalized):
        raise ValidationError(
            "INVALID_ROOM_CODE",
            "error.validation_failed",
            message="Room code must contain exactly 6 letters or digits",
        )
    return normalized


def normalize_study_session_id(study_session_id: str | None) -> str:
    """Normalize and bound an optional Unity studySessionId."""
    normalized = str(study_session_id or "").strip()
    if len(normalized) > 64:
        raise ValidationError(
            "INVALID_STUDY_SESSION_ID",
            "error.validation_failed",
            message="Study session ID must contain at most 64 characters",
        )
    return normalized


def normalize_ai_assistant_mode(mode: str | None) -> str:
    """Validate the online challenge AI assistant mode."""
    clean = str(mode or "").strip()
    if clean not in ONLINE_AI_ASSISTANT_MODES:
        raise ValidationError(
            "INVALID_AI_ASSISTANT_MODE",
            "error.validation_failed",
            message="Unknown AI assistant mode",
        )
    return clean


def authenticate_room_record(
    record: OnlineRoomRecord,
    supplied_token: str | None,
) -> dict[str, Any]:
    """Authenticate a player token against hashed (or legacy raw) room player credentials."""
    token = str(supplied_token or "").strip()
    if not token:
        raise AuthError("INVALID_PLAYER_TOKEN", "error.room_invalid_token")
    token_hash = hash_room_token(token)
    for player in record.players:
        stored_hash = str(player.get("tokenHash") or "").strip()
        if stored_hash and safe_compare_digest(stored_hash, token_hash):
            return player
        stored_raw = str(player.get("token") or "").strip()
        if stored_raw and safe_compare_digest(stored_raw, token):
            return player
    raise AuthError("INVALID_PLAYER_TOKEN", "error.room_invalid_token")


def to_public_room_payload(
    record: OnlineRoomRecord,
    *,
    player_number: int | None = None,
    raw_token: str | None = None,
) -> dict[str, Any]:
    """Serialize a persisted OnlineRoomRecord into the public Unity/WebGL room DTO."""
    room = OnlineRoom.model_validate(
        {
            "matchId": record.id,
            "roomCode": record.room_code,
            "status": record.status,
            "players": record.players,
            "createdAt": record.created_at,
            "expiresAt": record.expires_at,
        }
    )
    payload = serialize_public_room(
        room,
        player_number=player_number,
        include_token=False,
    )
    if raw_token is not None and player_number is not None:
        payload["playerToken"] = raw_token
    return payload


def validate_draft_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Validate an online Starting Draft attachment payload."""
    final_difficulty = str(payload.get("finalDifficulty") or "").strip()
    final_layout = str(payload.get("finalLayout") or "").strip()
    if final_difficulty not in {"Easy", "Medium", "Hard"}:
        raise ValidationError(
            "INVALID_FINAL_DIFFICULTY",
            "error.validation_failed",
            message="Invalid final difficulty",
        )
    if final_layout not in {"Compact", "Balanced", "Open"}:
        raise ValidationError(
            "INVALID_FINAL_LAYOUT",
            "error.validation_failed",
            message="Invalid final layout",
        )

    metadata: dict[str, str] = {}
    for field in ONLINE_DRAFT_METADATA_KEYS:
        value = str(payload.get(field) or "").strip()
        if field in {"aiReflection", "aiDifficultyRationale", "aiLayoutRationale"}:
            max_len = 480 if field == "aiReflection" else 320
            if len(value) > max_len:
                raise ValidationError(
                    "DRAFT_METADATA_TOO_LONG",
                    "error.validation_failed",
                    message=f"Draft {field} is too long",
                )
        metadata[field] = value

    for field, choices in DG_ANSWER_CHOICES.items():
        if metadata[field] and metadata[field] not in choices:
            raise ValidationError(
                "INVALID_DRAFT_ANSWER",
                "error.validation_failed",
                message=f"Invalid Draft {field}",
            )

    if metadata["aiRecommendedDifficulty"] and metadata["aiRecommendedDifficulty"] not in {
        "Easy",
        "Medium",
        "Hard",
        "Random",
    }:
        raise ValidationError(
            "INVALID_DRAFT_AI_DIFFICULTY",
            "error.validation_failed",
            message="Invalid Draft AI difficulty",
        )
    if metadata["aiRecommendedLayout"] and metadata["aiRecommendedLayout"] not in {
        "Compact",
        "Balanced",
        "Open",
        "Random",
    }:
        raise ValidationError(
            "INVALID_DRAFT_AI_LAYOUT",
            "error.validation_failed",
            message="Invalid Draft AI layout",
        )
    if (
        metadata["aiRecommendationSource"]
        and metadata["aiRecommendationSource"] not in ONLINE_DRAFT_RECOMMENDATION_SOURCES
    ):
        raise ValidationError(
            "INVALID_DRAFT_AI_SOURCE",
            "error.validation_failed",
            message="Invalid Draft AI recommendation source",
        )

    metadata_complete = all(bool(metadata[field]) for field in ONLINE_DRAFT_METADATA_KEYS)
    requested_draft = {
        "finalDifficulty": final_difficulty,
        "finalLayout": final_layout,
        **metadata,
        "draftMetadataComplete": metadata_complete,
    }
    return requested_draft, metadata_complete


def draft_events_match(existing: Mapping[str, Any], requested: Mapping[str, Any]) -> bool:
    """Compare existing persisted draft event against a retried draft payload."""
    keys = ("finalDifficulty", "finalLayout", *ONLINE_DRAFT_METADATA_KEYS)
    for key in keys:
        if str(existing.get(key) or "").strip() != str(requested.get(key) or "").strip():
            return False
    return bool(existing.get("draftMetadataComplete", False)) == bool(
        requested.get("draftMetadataComplete", False)
    )


def build_validated_flow_event(
    *,
    match_id: str,
    room_code: str,
    player: Mapping[str, Any],
    event_payload: Mapping[str, Any],
    now_iso: str,
) -> dict[str, Any]:
    """Validate and normalize a co-creation flow event via CoCreationFlowEvent."""
    candidate = dict(event_payload)
    candidate["matchId"] = match_id
    candidate["roomCode"] = room_code
    candidate["playerNumber"] = int(player["playerNumber"])
    candidate["studySessionId"] = str(player.get("studySessionId") or "").strip()
    candidate["serverReceivedAt"] = now_iso
    try:
        validated = _FLOW_EVENT_ADAPTER.validate_python(candidate)
    except PydanticValidationError as exc:
        raise ValidationError(
            "INVALID_COCREATION_EVENT",
            "error.validation_failed",
            message=f"Invalid co-creation event: {exc}",
        ) from exc
    return validated.model_dump(by_alias=True, exclude_none=True)


def map_persistence_error(exc: PersistenceError) -> NoReturn:
    """Translate persistence exceptions into domain errors."""
    if isinstance(exc, PersistenceNotFoundError):
        key = (
            "error.player_not_found"
            if exc.code == "PLAYER_NOT_FOUND"
            else "error.room_not_found"
        )
        raise NotFoundError(exc.code, key, message=exc.message) from exc
    if isinstance(exc, PersistenceConflictError):
        key = (
            "error.room_cancelled"
            if exc.code == "ROOM_CANCELLED"
            else "error.room_full"
            if exc.code == "ROOM_FULL"
            else "error.idempotency_conflict"
            if exc.code in {"CHALLENGE_FROZEN", "RESULT_FROZEN", "DRAFT_FROZEN", "INTENTION_FROZEN"}
            else "error.room_not_ready"
        )
        raise ConflictError(exc.code, key, message=exc.message) from exc
    raise ConflictError(exc.code, "error.internal", message=exc.message) from exc
