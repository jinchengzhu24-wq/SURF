"""Online matchmaking room models and challenge board validation."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from server.domain.blueprints import GRID_HEIGHT, GRID_WIDTH
from server.domain.errors import ValidationError

ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_CODE_PATTERN = re.compile(r"^[A-Z0-9]{6}$")
SYNC_ID_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")
CARD_TYPE_PATTERN = re.compile(r"^[a-z_]{1,40}$")
NODE_STATUS_PATTERN = re.compile(r"^[a-z0-9_]{2,64}$")
ALLOWED_CHALLENGE_TILES = frozenset(" #.@pst")


class RoomStatus(StrEnum):
    WAITING_FOR_OPPONENT = "waiting_for_opponent"
    BRIEFING = "briefing"
    WAITING_FOR_CHALLENGES = "waiting_for_challenges"
    CHALLENGES_READY = "challenges_ready"
    WAITING_FOR_RESULTS = "waiting_for_results"
    RESULTS_READY = "results_ready"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


def _reject_bool_int(value: Any, field_name: str) -> Any:
    if isinstance(value, bool):
        raise ValueError(f"Boolean value is not allowed for {field_name}")
    return value


def validate_challenge_rows(rows: Sequence[str]) -> list[str]:
    """Validate structural constraints on a 12x10 online challenge board."""
    if not isinstance(rows, (list, tuple)) or len(rows) != GRID_HEIGHT:
        raise ValidationError("INVALID_ROWS", message=f"rows must contain {GRID_HEIGHT} rows")
    normalized: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, str) or len(row) != GRID_WIDTH:
            raise ValidationError(
                "INVALID_ROWS",
                message=f"Row {index + 1} must contain {GRID_WIDTH} tiles",
            )
        unknown = set(row) - ALLOWED_CHALLENGE_TILES
        if unknown:
            raise ValidationError(
                "INVALID_ROWS",
                message=f"Row {index + 1} contains unsupported tiles",
            )
        normalized.append(row)

    player_count = sum(r.count("p") for r in normalized)
    box_count = sum(r.count("s") for r in normalized)
    target_count = sum(r.count("t") for r in normalized)
    if player_count != 1:
        raise ValidationError(
            "INVALID_PLAYER_COUNT",
            message="Challenge must contain exactly one player start",
        )
    if box_count < 1 or box_count > 2:
        raise ValidationError(
            "INVALID_BOX_COUNT",
            message="Challenge must contain one or two box starts",
        )
    if target_count != box_count:
        raise ValidationError(
            "MISMATCHED_TARGET_COUNT",
            message="Challenge box and target counts must match",
        )
    return normalized


class PlayResultSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    duration_seconds: float = Field(alias="durationSeconds")
    move_count: int = Field(alias="moveCount")
    minimum_moves: int = Field(alias="minimumMoves")
    restart_count: int = Field(default=0, alias="restartCount")
    outcome: Literal["completed", "timed_out"] = "completed"

    @field_validator("move_count", "minimum_moves", "restart_count", mode="before")
    @classmethod
    def _no_bool_ints(cls, value: Any, info: Any) -> Any:
        return _reject_bool_int(value, str(info.field_name))

    @model_validator(mode="after")
    def _validate_bounds(self) -> PlayResultSummary:
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("Result duration must be a finite non-negative number")
        if self.move_count < 0 or self.minimum_moves < 0 or self.restart_count < 0:
            raise ValueError("Result move and restart counts must be non-negative")
        if self.outcome == "completed" and self.move_count < self.minimum_moves:
            raise ValueError("Result move count cannot be lower than the theoretical minimum")
        self.duration_seconds = round(float(self.duration_seconds), 2)
        return self


class RoomPlayer(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    player_number: Literal[1, 2] = Field(alias="playerNumber")
    study_session_id: str = Field(default="", alias="studySessionId")
    menu_started_flow: bool = Field(default=False, alias="menuStartedFlow")
    token: str = ""
    ready: bool = False
    challenge_rows: list[str] | None = Field(default=None, alias="challengeRows")
    ai_assistant_mode: str | None = Field(default=None, alias="aiAssistantMode")
    designer_intention: str | None = Field(default=None, alias="designerIntention")
    result: PlayResultSummary | None = None
    pending_draft: dict[str, Any] | None = Field(default=None, alias="pendingDraft")
    left: bool = False

    @field_validator("study_session_id", mode="before")
    @classmethod
    def _validate_study_session(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if len(clean) > 64:
            raise ValueError("Study session ID must contain at most 64 characters")
        return clean


class OnlineRoom(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    match_id: str = Field(alias="matchId")
    room_code: str = Field(alias="roomCode")
    status: RoomStatus = RoomStatus.WAITING_FOR_OPPONENT
    players: list[RoomPlayer] = Field(default_factory=list)
    last_activity: float = Field(default=0.0, alias="lastActivity")
    created_at: str | None = Field(default=None, alias="createdAt")
    expires_at: str | None = Field(default=None, alias="expiresAt")

    @field_validator("room_code", mode="before")
    @classmethod
    def _validate_room_code(cls, value: Any) -> str:
        normalized = str(value or "").strip().upper()
        if not ROOM_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("Room code must contain exactly 6 letters or digits")
        return normalized
