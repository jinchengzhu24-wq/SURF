"""Shared card, node, and stage/turn CoCreationFlowEvent models."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    CARD_TYPE_PATTERN,
    SYNC_ID_PATTERN,
    _reject_bool_int,
    validate_challenge_rows,
)


class CoCreationCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    text: str

    @field_validator("type", mode="before")
    @classmethod
    def _valid_type(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not CARD_TYPE_PATTERN.fullmatch(clean):
            raise ValueError("Invalid co-creation card type")
        return clean

    @field_validator("text", mode="before")
    @classmethod
    def _valid_text(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > 4000:
            raise ValueError("Invalid co-creation card text")
        return clean


class DashboardNodeEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    entry_id: str = Field(alias="entryId")
    kind: Literal[
        "player_message",
        "llm_message",
        "card",
        "player_action",
        "map_diff",
        "status",
    ]
    text: str
    label: str = ""
    occurred_at: str = Field(alias="occurredAt")
    language: Literal["en", "zh-CN"] | None = None

    @field_validator("entry_id", mode="before")
    @classmethod
    def _valid_entry_id(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not SYNC_ID_PATTERN.fullmatch(clean):
            raise ValueError("Invalid node entry ID")
        return clean

    @field_validator("text", mode="before")
    @classmethod
    def _valid_text(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > 12000:
            raise ValueError("Invalid dashboard node entry text")
        return clean

    @field_validator("label", mode="before")
    @classmethod
    def _valid_label(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if len(clean) > 160:
            raise ValueError("Invalid dashboard node entry label")
        return clean

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _valid_occurred_at(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise ValueError("occurredAt is required")
        return clean

    @field_validator("language", mode="before")
    @classmethod
    def _norm_lang(cls, value: Any) -> str | None:
        clean = str(value or "").strip()
        return clean or None


class _FlowEventBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    event_id: str = Field(alias="eventId")
    player_number: Literal[1, 2] = Field(alias="playerNumber")
    session_id: str = Field(alias="sessionId")
    match_id: str = Field(default="", alias="matchId")
    room_code: str = Field(default="", alias="roomCode")
    study_session_id: str = Field(default="", alias="studySessionId")
    server_received_at: str | None = Field(default=None, alias="serverReceivedAt")

    @field_validator("player_number", mode="before")
    @classmethod
    def _no_bool_player(cls, value: Any) -> Any:
        return _reject_bool_int(value, "playerNumber")

    @field_validator("event_id", "session_id", mode="before")
    @classmethod
    def _valid_sync_ids(cls, value: Any, info: Any) -> str:
        clean = str(value or "").strip()
        if not SYNC_ID_PATTERN.fullmatch(clean):
            raise ValueError(f"Invalid {info.field_name}")
        return clean


class FirstStageFlowEvent(_FlowEventBase):
    event_type: Literal["first_stage"] = Field(alias="eventType")
    version_id: str = Field(alias="versionId")
    stage_number: Literal[1] = Field(alias="stageNumber")
    initial_draft_method: str = Field(alias="initialDraftMethod")
    rows: list[str]
    diff: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("stage_number", mode="before")
    @classmethod
    def _no_bool_stage(cls, value: Any) -> Any:
        return _reject_bool_int(value, "stageNumber")

    @field_validator("version_id", mode="before")
    @classmethod
    def _valid_vid(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not SYNC_ID_PATTERN.fullmatch(clean):
            raise ValueError("Invalid versionId")
        return clean

    @field_validator("initial_draft_method", mode="before")
    @classmethod
    def _valid_method(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not re.fullmatch(r"[a-z_]{1,64}", clean):
            raise ValueError("Invalid initialDraftMethod")
        return clean

    @field_validator("rows")
    @classmethod
    def _valid_rows(cls, value: list[str]) -> list[str]:
        return validate_challenge_rows(value)

    @field_validator("diff")
    @classmethod
    def _valid_diff(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        json.dumps(value, allow_nan=False)
        return value


class StageFlowEvent(_FlowEventBase):
    event_type: Literal["stage"] = Field(alias="eventType")
    version_id: str = Field(alias="versionId")
    stage_number: int = Field(ge=1, alias="stageNumber")
    source: Literal["manual", "ai", "restored"]
    rows: list[str]
    diff: list[dict[str, Any]] = Field(default_factory=list)
    restored_from_version_id: str | None = Field(default=None, alias="restoredFromVersionId")
    restored_from_stage_number: int | None = Field(
        default=None,
        ge=1,
        alias="restoredFromStageNumber",
    )
    replaced_version_id: str | None = Field(default=None, alias="replacedVersionId")
    replaced_stage_number: int | None = Field(
        default=None,
        ge=1,
        alias="replacedStageNumber",
    )

    @field_validator(
        "stage_number",
        "restored_from_stage_number",
        "replaced_stage_number",
        mode="before",
    )
    @classmethod
    def _no_bool_stages(cls, value: Any, info: Any) -> Any:
        return _reject_bool_int(value, str(info.field_name))

    @field_validator("version_id", mode="before")
    @classmethod
    def _valid_vid(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not SYNC_ID_PATTERN.fullmatch(clean):
            raise ValueError("Invalid versionId")
        return clean

    @field_validator("rows")
    @classmethod
    def _valid_rows(cls, value: list[str]) -> list[str]:
        return validate_challenge_rows(value)

    @field_validator("diff")
    @classmethod
    def _valid_diff(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        json.dumps(value, allow_nan=False)
        return value

    @model_validator(mode="after")
    def _check_restored_fields(self) -> StageFlowEvent:
        if self.source == "restored":
            for vid, num in (
                (self.restored_from_version_id, self.restored_from_stage_number),
                (self.replaced_version_id, self.replaced_stage_number),
            ):
                if not vid or not SYNC_ID_PATTERN.fullmatch(vid) or num is None:
                    raise ValueError("Incomplete restored Stage metadata")
        return self


class OpeningFlowEvent(_FlowEventBase):
    event_type: Literal["opening"] = Field(alias="eventType")
    version_id: str = Field(alias="versionId")
    stage_number: int = Field(ge=1, alias="stageNumber")
    assistant_turn_id: str = Field(alias="assistantTurnId")
    assistant_text: str = Field(alias="assistantText")
    language: Literal["en", "zh-CN"]
    cards: list[CoCreationCard] = Field(default_factory=list, max_length=8)

    @field_validator("stage_number", mode="before")
    @classmethod
    def _no_bool_stage(cls, value: Any) -> Any:
        return _reject_bool_int(value, "stageNumber")

    @field_validator("version_id", "assistant_turn_id", mode="before")
    @classmethod
    def _valid_ids(cls, value: Any, info: Any) -> str:
        clean = str(value or "").strip()
        if not SYNC_ID_PATTERN.fullmatch(clean):
            raise ValueError(f"Invalid {info.field_name}")
        return clean

    @field_validator("assistant_text", mode="before")
    @classmethod
    def _valid_text(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > 12000:
            raise ValueError("Invalid opening assistantText")
        return clean


class TurnFlowEvent(_FlowEventBase):
    event_type: Literal["turn"] = Field(alias="eventType")
    version_id: str = Field(alias="versionId")
    user_text: str = Field(alias="userText")
    assistant_text: str = Field(alias="assistantText")
    language: Literal["en", "zh-CN"]
    cards: list[CoCreationCard] = Field(default_factory=list, max_length=8)
    opening_assistant_turn_id: str | None = Field(default=None, alias="openingAssistantTurnId")
    opening_assistant_text: str | None = Field(default=None, alias="openingAssistantText")
    opening_language: Literal["en", "zh-CN"] | None = Field(default=None, alias="openingLanguage")
    opening_cards: list[CoCreationCard] | None = Field(
        default=None,
        alias="openingCards",
        max_length=8,
    )

    @field_validator("version_id", mode="before")
    @classmethod
    def _valid_vid(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not SYNC_ID_PATTERN.fullmatch(clean):
            raise ValueError("Invalid versionId")
        return clean

    @field_validator("user_text", mode="before")
    @classmethod
    def _valid_user(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > 2000:
            raise ValueError("Invalid userText")
        return clean

    @field_validator("assistant_text", mode="before")
    @classmethod
    def _valid_assistant(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > 12000:
            raise ValueError("Invalid assistantText")
        return clean

    @model_validator(mode="after")
    def _check_optional_opening(self) -> TurnFlowEvent:
        supplied = any(
            x is not None
            for x in (
                self.opening_assistant_turn_id,
                self.opening_assistant_text,
                self.opening_language,
                self.opening_cards,
            )
        )
        if supplied:
            if (
                not self.opening_assistant_turn_id
                or not SYNC_ID_PATTERN.fullmatch(self.opening_assistant_turn_id)
                or not self.opening_assistant_text
                or not self.opening_assistant_text.strip()
                or len(self.opening_assistant_text) > 12000
                or self.opening_language not in {"en", "zh-CN"}
            ):
                raise ValueError("Incomplete opening conversation in turn event")
        return self
