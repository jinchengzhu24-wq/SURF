"""CoCreationFlowEvent discriminated union and remaining flow event variants."""

from __future__ import annotations

import json
import math
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from server.domain.blueprints import (
    DG_ANSWER_CHOICES,
    ONLINE_DRAFT_METADATA_KEYS,
    ONLINE_DRAFT_RECOMMENDATION_SOURCES,
)

from ._event_models import (
    CoCreationCard,
    DashboardNodeEntry,
    FirstStageFlowEvent,
    OpeningFlowEvent,
    StageFlowEvent,
    TurnFlowEvent,
    _FlowEventBase,
)
from .models import (
    NODE_STATUS_PATTERN,
    SYNC_ID_PATTERN,
    _reject_bool_int,
    validate_challenge_rows,
)


class FinalFlowEvent(_FlowEventBase):
    event_type: Literal["final"] = Field(alias="eventType")
    version_id: str = Field(alias="versionId")
    stage_number: int = Field(ge=1, alias="stageNumber")
    rows: list[str]
    diff: list[dict[str, Any]] = Field(default_factory=list)
    co_creation_duration_seconds: float | None = Field(
        default=None,
        alias="coCreationDurationSeconds",
    )

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

    @field_validator("rows")
    @classmethod
    def _valid_rows(cls, value: list[str]) -> list[str]:
        return validate_challenge_rows(value)

    @field_validator("diff")
    @classmethod
    def _valid_diff(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        json.dumps(value, allow_nan=False)
        return value

    @field_validator("co_creation_duration_seconds")
    @classmethod
    def _valid_duration(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value < 0 or value > 86400:
            raise ValueError("Invalid coCreationDurationSeconds")
        return round(float(value), 2)


class NodeFlowEvent(_FlowEventBase):
    event_type: Literal["node"] = Field(alias="eventType")
    node_id: str = Field(alias="nodeId")
    node_type: Literal[
        "discussion",
        "intent",
        "intent_conflict",
        "proposal",
        "player_challenge",
        "manual_edit_review",
        "llm_challenge",
    ] = Field(alias="nodeType")
    node_status: str = Field(alias="nodeStatus")
    node_parent_id: str | None = Field(default=None, alias="nodeParentId")
    node_entries: list[DashboardNodeEntry] = Field(
        min_length=1,
        max_length=96,
        alias="nodeEntries",
    )
    version_id: str | None = Field(default=None, alias="versionId")
    stage_number: int | None = Field(default=None, ge=1, alias="stageNumber")

    @field_validator("stage_number", mode="before")
    @classmethod
    def _no_bool_stage(cls, value: Any) -> Any:
        return _reject_bool_int(value, "stageNumber")

    @field_validator("node_id", mode="before")
    @classmethod
    def _valid_nid(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not SYNC_ID_PATTERN.fullmatch(clean):
            raise ValueError("Invalid nodeId")
        return clean

    @field_validator("node_status", mode="before")
    @classmethod
    def _valid_status(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if not NODE_STATUS_PATTERN.fullmatch(clean):
            raise ValueError("Invalid nodeStatus")
        return clean

    @field_validator("node_parent_id", "version_id", mode="before")
    @classmethod
    def _valid_optional_ids(cls, value: Any, info: Any) -> str | None:
        clean = str(value or "").strip()
        if not clean:
            return None
        if not SYNC_ID_PATTERN.fullmatch(clean):
            raise ValueError(f"Invalid {info.field_name}")
        return clean


class DraftFlowEvent(_FlowEventBase):
    event_type: Literal["draft"] = Field(alias="eventType")
    final_difficulty: Literal["Easy", "Medium", "Hard"] = Field(alias="finalDifficulty")
    final_layout: Literal["Compact", "Balanced", "Open"] = Field(alias="finalLayout")
    q1_answer: str = Field(default="", alias="q1Answer")
    q2_answer: str = Field(default="", alias="q2Answer")
    q3_answer: str = Field(default="", alias="q3Answer")
    q4_answer: str = Field(default="", alias="q4Answer")
    ai_reflection: str = Field(default="", alias="aiReflection")
    ai_difficulty_rationale: str = Field(default="", alias="aiDifficultyRationale")
    ai_layout_rationale: str = Field(default="", alias="aiLayoutRationale")
    ai_recommended_difficulty: str = Field(default="", alias="aiRecommendedDifficulty")
    ai_recommended_layout: str = Field(default="", alias="aiRecommendedLayout")
    ai_recommendation_source: str = Field(default="", alias="aiRecommendationSource")
    draft_metadata_complete: bool = Field(default=False, alias="draftMetadataComplete")

    @model_validator(mode="after")
    def _validate_draft_metadata(self) -> DraftFlowEvent:
        for attr, q_key in (
            ("q1_answer", "q1Answer"),
            ("q2_answer", "q2Answer"),
            ("q3_answer", "q3Answer"),
            ("q4_answer", "q4Answer"),
        ):
            val = str(getattr(self, attr) or "").strip()
            setattr(self, attr, val)
            if val and val not in DG_ANSWER_CHOICES[q_key]:
                raise ValueError(f"Invalid Draft {q_key}")

        self.ai_reflection = self.ai_reflection.strip()
        self.ai_difficulty_rationale = self.ai_difficulty_rationale.strip()
        self.ai_layout_rationale = self.ai_layout_rationale.strip()
        if len(self.ai_reflection) > 480:
            raise ValueError("Draft aiReflection is too long")
        if len(self.ai_difficulty_rationale) > 320:
            raise ValueError("Draft aiDifficultyRationale is too long")
        if len(self.ai_layout_rationale) > 320:
            raise ValueError("Draft aiLayoutRationale is too long")

        if self.ai_recommended_difficulty and self.ai_recommended_difficulty not in {
            "Easy",
            "Medium",
            "Hard",
            "Random",
        }:
            raise ValueError("Invalid Draft AI difficulty")
        if self.ai_recommended_layout and self.ai_recommended_layout not in {
            "Compact",
            "Balanced",
            "Open",
            "Random",
        }:
            raise ValueError("Invalid Draft AI layout")
        if (
            self.ai_recommendation_source
            and self.ai_recommendation_source not in ONLINE_DRAFT_RECOMMENDATION_SOURCES
        ):
            raise ValueError("Invalid Draft AI recommendation source")

        data = self.model_dump(by_alias=True)
        self.draft_metadata_complete = all(bool(data.get(k)) for k in ONLINE_DRAFT_METADATA_KEYS)
        return self


class MessageFlowEvent(_FlowEventBase):
    event_type: Literal["message"] = Field(alias="eventType")
    message: str

    @field_validator("message", mode="before")
    @classmethod
    def _valid_message(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if len(clean) > 4000:
            raise ValueError("Designer intention must contain at most 4000 characters")
        return clean


CoCreationFlowEvent = Annotated[
    FirstStageFlowEvent
    | StageFlowEvent
    | OpeningFlowEvent
    | TurnFlowEvent
    | FinalFlowEvent
    | NodeFlowEvent
    | DraftFlowEvent
    | MessageFlowEvent,
    Field(discriminator="event_type"),
]

__all__ = [
    "CoCreationCard",
    "CoCreationFlowEvent",
    "DashboardNodeEntry",
    "DraftFlowEvent",
    "FinalFlowEvent",
    "FirstStageFlowEvent",
    "MessageFlowEvent",
    "NodeFlowEvent",
    "OpeningFlowEvent",
    "StageFlowEvent",
    "TurnFlowEvent",
]
