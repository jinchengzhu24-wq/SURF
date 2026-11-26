"""Typed semantic memory state models and single-pass bounding validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .claims import HypothesisStatus, IntentHypothesis
from .normalization import (
    MAX_TEXT,
    _clamp_confidence,
    _clean_text,
    _opt_source,
    _stable_id,
    is_design_level_question,
    sanitize_user_design_text,
)

SCHEMA_VERSION = 6
MAX_ACTIVE_INFERRED = 16
MAX_INACTIVE_ITEMS = 16
MAX_PATCH_ITEMS = 8


class Authority(StrEnum):
    EXPLICIT = "explicit"
    CONFIRMED = "confirmed"
    INFERRED = "inferred"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class DecisionStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    IGNORED = "ignored"


class DisagreementStatus(StrEnum):
    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class Goal(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    id: str = ""
    goal: str
    authority: Authority = Authority.INFERRED
    status: GoalStatus = GoalStatus.ACTIVE
    source_stage_id: str | None = Field(default=None, alias="sourceStageId")
    source_turn_id: str | None = Field(default=None, alias="sourceTurnId")
    confidence: float = 0.5
    supersedes_id: str | None = Field(default=None, alias="supersedesId")

    @field_validator("goal", mode="before")
    @classmethod
    def _sanitize_goal(cls, value: Any) -> str:
        clean = sanitize_user_design_text(value)
        if not clean:
            raise ValueError("Goal text cannot be empty")
        return clean

    @field_validator("authority", mode="before")
    @classmethod
    def _norm_authority(cls, value: Any) -> str:
        return value if value in {a.value for a in Authority} else Authority.INFERRED.value

    @field_validator("status", mode="before")
    @classmethod
    def _norm_status(cls, value: Any) -> str:
        return value if value in {s.value for s in GoalStatus} else GoalStatus.ACTIVE.value

    @field_validator("confidence", mode="before")
    @classmethod
    def _norm_confidence(cls, value: Any) -> float:
        return _clamp_confidence(value, 0.5)

    @model_validator(mode="after")
    def _fill_id_and_sources(self) -> Goal:
        self.id = _clean_text(self.id, 96) or _stable_id("goal", self.goal)
        self.source_stage_id = _opt_source(self.source_stage_id)
        self.source_turn_id = _opt_source(self.source_turn_id)
        self.supersedes_id = _opt_source(self.supersedes_id)
        return self


class Constraint(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    id: str = ""
    constraint: str
    authority: Authority = Authority.INFERRED
    status: GoalStatus = GoalStatus.ACTIVE
    source_stage_id: str | None = Field(default=None, alias="sourceStageId")
    source_turn_id: str | None = Field(default=None, alias="sourceTurnId")
    confidence: float = 0.5
    supersedes_id: str | None = Field(default=None, alias="supersedesId")

    @field_validator("constraint", mode="before")
    @classmethod
    def _sanitize_constraint(cls, value: Any) -> str:
        clean = sanitize_user_design_text(value)
        if not clean:
            raise ValueError("Constraint text cannot be empty")
        return clean

    @field_validator("authority", mode="before")
    @classmethod
    def _norm_authority(cls, value: Any) -> str:
        return value if value in {a.value for a in Authority} else Authority.INFERRED.value

    @field_validator("status", mode="before")
    @classmethod
    def _norm_status(cls, value: Any) -> str:
        return value if value in {s.value for s in GoalStatus} else GoalStatus.ACTIVE.value

    @field_validator("confidence", mode="before")
    @classmethod
    def _norm_confidence(cls, value: Any) -> float:
        return _clamp_confidence(value, 0.5)

    @model_validator(mode="after")
    def _fill_id_and_sources(self) -> Constraint:
        self.id = _clean_text(self.id, 96) or _stable_id("constraint", self.constraint)
        self.source_stage_id = _opt_source(self.source_stage_id)
        self.source_turn_id = _opt_source(self.source_turn_id)
        self.supersedes_id = _opt_source(self.supersedes_id)
        return self


class Decision(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    id: str = ""
    decision: str
    reason: str = ""
    status: DecisionStatus | None = DecisionStatus.ACTIVE
    source_stage_id: str | None = Field(default=None, alias="sourceStageId")
    source_turn_id: str | None = Field(default=None, alias="sourceTurnId")
    proposal_id: str | None = Field(default=None, alias="proposalId")

    @field_validator("decision", mode="before")
    @classmethod
    def _clean_decision(cls, value: Any) -> str:
        clean = _clean_text(value)
        if not clean:
            raise ValueError("Decision text cannot be empty")
        return clean

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("status", mode="before")
    @classmethod
    def _norm_status(cls, value: Any) -> str | None:
        if value is None:
            return None
        return value if value in {s.value for s in DecisionStatus} else DecisionStatus.ACTIVE.value

    @model_validator(mode="after")
    def _fill_id_and_sources(self) -> Decision:
        self.source_stage_id = _opt_source(self.source_stage_id)
        self.source_turn_id = _opt_source(self.source_turn_id)
        self.proposal_id = _opt_source(self.proposal_id)
        self.id = _clean_text(self.id, 96) or _stable_id(
            "decision", self.decision, self.source_turn_id
        )
        return self


class OpenQuestion(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    id: str = ""
    question: str
    status: QuestionStatus = QuestionStatus.OPEN
    source_stage_id: str | None = Field(default=None, alias="sourceStageId")
    source_turn_id: str | None = Field(default=None, alias="sourceTurnId")
    updated_from_turn_id: str | None = Field(default=None, alias="updatedFromTurnId")
    resolved_by_turn_id: str | None = Field(default=None, alias="resolvedByTurnId")
    answered_at_stage_id: str | None = Field(default=None, alias="answeredAtStageId")
    ignored_at_stage_id: str | None = Field(default=None, alias="ignoredAtStageId")
    ignored_at: str | None = Field(default=None, alias="ignoredAt")
    source_kind: str = Field(default="legacy", alias="sourceKind")
    source_key: str | None = Field(default=None, alias="sourceKey")

    @field_validator("question", mode="before")
    @classmethod
    def _sanitize_question(cls, value: Any) -> str:
        clean = sanitize_user_design_text(value)
        if not clean:
            raise ValueError("Question text cannot be empty")
        return clean

    @field_validator("status", mode="before")
    @classmethod
    def _norm_status(cls, value: Any) -> str:
        if value == "resolved":
            return QuestionStatus.ANSWERED.value
        return value if value in {s.value for s in QuestionStatus} else QuestionStatus.OPEN.value

    @model_validator(mode="after")
    def _fill_defaults(self) -> OpenQuestion:
        self.id = _clean_text(self.id, 96) or _stable_id("question", self.question)
        self.source_stage_id = _opt_source(self.source_stage_id)
        self.source_turn_id = _opt_source(self.source_turn_id)
        self.updated_from_turn_id = _opt_source(self.updated_from_turn_id or self.source_turn_id)
        self.resolved_by_turn_id = _opt_source(self.resolved_by_turn_id)
        self.answered_at_stage_id = _opt_source(self.answered_at_stage_id)
        self.ignored_at_stage_id = _opt_source(self.ignored_at_stage_id)
        self.ignored_at = _clean_text(self.ignored_at, 64) or None
        self.source_kind = _clean_text(self.source_kind, 32) or "legacy"
        self.source_key = _clean_text(self.source_key, 192) or None
        return self


class Disagreement(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    status: DisagreementStatus
    subject: str | None = None
    user_position: str | None = Field(default=None, alias="userPosition")
    ai_position: str | None = Field(default=None, alias="aiPosition")
    core_disagreement: str | None = Field(default=None, alias="coreDisagreement")
    next_question: str | None = Field(default=None, alias="nextQuestion")
    resolution: str | None = None
    phase: Literal["reason_pending", "reason_review", "choice_pending"] | None = None
    display_card: bool = Field(default=True, alias="displayCard")
    challenge_id: str | None = Field(default=None, alias="challengeId")
    disagreement_id: str | None = Field(default=None, alias="disagreementId")
    primary_hypothesis: str | None = Field(default=None, alias="primaryHypothesis")
    secondary_hypothesis: str | None = Field(default=None, alias="secondaryHypothesis")
    proposal_summary: str | None = Field(default=None, alias="proposalSummary")
    accepted_reason: str | None = Field(default=None, alias="acceptedReason")

    @model_validator(mode="after")
    def _clean_fields(self) -> Disagreement:
        for attr in (
            "subject",
            "user_position",
            "ai_position",
            "core_disagreement",
            "next_question",
            "resolution",
            "challenge_id",
            "disagreement_id",
            "primary_hypothesis",
            "secondary_hypothesis",
            "proposal_summary",
            "accepted_reason",
        ):
            setattr(self, attr, _clean_text(getattr(self, attr), MAX_TEXT) or None)
        if self.status in {
            DisagreementStatus.ACTIVE.value,
            DisagreementStatus.ACKNOWLEDGED.value,
        }:
            self.resolution = None
        if self.status == DisagreementStatus.ACKNOWLEDGED.value and self.subject != "human_edit":
            raise ValueError("Only human_edit disagreements can remain acknowledged")
        return self


def _bounded_semantic_items(items: Sequence[Any]) -> list[Any]:
    authoritative = [
        item
        for item in items
        if item.status == GoalStatus.ACTIVE.value
        and item.authority in {Authority.EXPLICIT.value, Authority.CONFIRMED.value}
    ]
    inferred = [
        item
        for item in items
        if item.status == GoalStatus.ACTIVE.value and item.authority == Authority.INFERRED.value
    ][-MAX_ACTIVE_INFERRED:]
    inactive = [item for item in items if item.status != GoalStatus.ACTIVE.value][
        -MAX_INACTIVE_ITEMS:
    ]
    return [*authoritative, *inferred, *inactive]


def _bounded_decisions(items: Sequence[Decision]) -> list[Decision]:
    active = [item for item in items if item.status == DecisionStatus.ACTIVE.value]
    inactive = [item for item in items if item.status != DecisionStatus.ACTIVE.value][
        -MAX_INACTIVE_ITEMS:
    ]
    return [*active, *inactive]


def _bounded_hypotheses(items: Sequence[IntentHypothesis]) -> list[IntentHypothesis]:
    confirmed = [item for item in items if item.status == HypothesisStatus.CONFIRMED.value]
    tentative = [item for item in items if item.status == HypothesisStatus.TENTATIVE.value][
        -MAX_ACTIVE_INFERRED:
    ]
    inactive = [
        item
        for item in items
        if item.status
        not in {HypothesisStatus.CONFIRMED.value, HypothesisStatus.TENTATIVE.value}
    ][-MAX_INACTIVE_ITEMS:]
    return [*confirmed, *tentative, *inactive]


def _parse_safe_list(items: Any, model_cls: type[BaseModel]) -> list[Any]:
    if not isinstance(items, list):
        return []
    parsed: list[Any] = []
    for raw in items:
        if isinstance(raw, model_cls):
            parsed.append(raw)
        elif isinstance(raw, Mapping):
            try:
                parsed.append(model_cls.model_validate(raw))
            except ValueError:
                continue
    return parsed


class DesignContextState(BaseModel):
    """Server-owned semantic memory for one co-created Stage."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    schema_version: int = Field(default=SCHEMA_VERSION, alias="schemaVersion")
    user_goals: list[Goal] = Field(default_factory=list, alias="userGoals")
    design_constraints: list[Constraint] = Field(default_factory=list, alias="designConstraints")
    confirmed_decisions: list[Decision] = Field(default_factory=list, alias="confirmedDecisions")
    rejected_decisions: list[Decision] = Field(default_factory=list, alias="rejectedDecisions")
    open_questions: list[OpenQuestion] = Field(default_factory=list, alias="openQuestions")
    intent_hypotheses: list[IntentHypothesis] = Field(
        default_factory=list,
        alias="intentHypotheses",
    )
    processed_evidence_ids: list[str] = Field(default_factory=list, alias="processedEvidenceIds")
    active_disagreement: Disagreement | None = Field(default=None, alias="activeDisagreement")
    updated_from_stage_id: str | None = Field(default=None, alias="updatedFromStageId")
    updated_from_turn_id: str | None = Field(default=None, alias="updatedFromTurnId")

    @field_validator("user_goals", mode="before")
    @classmethod
    def _parse_goals(cls, value: Any) -> list[Goal]:
        return _parse_safe_list(value, Goal)

    @field_validator("design_constraints", mode="before")
    @classmethod
    def _parse_constraints(cls, value: Any) -> list[Constraint]:
        return _parse_safe_list(value, Constraint)

    @field_validator("confirmed_decisions", mode="before")
    @classmethod
    def _parse_confirmed(cls, value: Any) -> list[Decision]:
        return _parse_safe_list(value, Decision)

    @field_validator("rejected_decisions", mode="before")
    @classmethod
    def _parse_rejected(cls, value: Any) -> list[Decision]:
        items = _parse_safe_list(value, Decision)
        for item in items:
            item.status = None
        return items

    @field_validator("open_questions", mode="before")
    @classmethod
    def _parse_questions(cls, value: Any) -> list[OpenQuestion]:
        return _parse_safe_list(value, OpenQuestion)

    @field_validator("intent_hypotheses", mode="before")
    @classmethod
    def _parse_hypotheses(cls, value: Any) -> list[IntentHypothesis]:
        return _parse_safe_list(value, IntentHypothesis)

    @field_validator("active_disagreement", mode="before")
    @classmethod
    def _parse_disagreement(cls, value: Any) -> Disagreement | None:
        if value is None:
            return None
        if isinstance(value, Disagreement):
            return value if value.status == DisagreementStatus.ACTIVE.value else None
        if isinstance(value, Mapping):
            try:
                parsed = Disagreement.model_validate(value)
                return parsed if parsed.status == DisagreementStatus.ACTIVE.value else None
            except ValueError:
                return None
        return None

    @model_validator(mode="after")
    def _apply_bounds_single_pass(self) -> DesignContextState:
        self.schema_version = SCHEMA_VERSION
        self.user_goals = _bounded_semantic_items(self.user_goals)
        self.design_constraints = _bounded_semantic_items(self.design_constraints)
        self.confirmed_decisions = _bounded_decisions(self.confirmed_decisions)
        self.rejected_decisions = self.rejected_decisions[-MAX_INACTIVE_ITEMS:]
        self.intent_hypotheses = _bounded_hypotheses(self.intent_hypotheses)
        self.processed_evidence_ids = list(
            dict.fromkeys(
                v for v in (_opt_source(x) for x in self.processed_evidence_ids) if v
            )
        )[-128:]
        self.updated_from_stage_id = _opt_source(self.updated_from_stage_id)
        self.updated_from_turn_id = _opt_source(self.updated_from_turn_id)
        if (
            self.active_disagreement is not None
            and self.active_disagreement.status != DisagreementStatus.ACTIVE.value
        ):
            self.active_disagreement = None
        return self

    def to_wire_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=False)

    def design_level_open_questions(self) -> list[OpenQuestion]:
        return [
            item
            for item in self.open_questions
            if item.status == QuestionStatus.OPEN.value
            and is_design_level_question(item.question)
        ]


DesignContext = DesignContextState


def ensure_design_context(
    value: DesignContextState | Mapping[str, Any] | None,
) -> DesignContextState:
    """Validate and coerce raw dictionary or state into DesignContextState in one pass."""
    if isinstance(value, DesignContextState):
        return value.model_copy(deep=True)
    if isinstance(value, Mapping):
        return DesignContextState.model_validate(value)
    return DesignContextState()
