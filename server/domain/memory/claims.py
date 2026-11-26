"""Typed semantic claims and intent hypothesis models."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalization import (
    _clamp_confidence,
    _clean_text,
    _opt_source,
    _stable_id,
    sanitize_user_design_text,
)


class HypothesisStatus(StrEnum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    LEGACY_UNVERIFIED = "legacy_unverified"


INTENT_TOPICS: frozenset[str] = frozenset(
    {
        "difficulty",
        "route_readability",
        "push_dependency",
        "space_distribution",
        "route_rhythm",
        "water_function",
        "entity_placement",
        "preservation",
        "other",
    }
)
INTENT_CLAIM_SUBJECTS: frozenset[str] = frozenset(
    {"layout", "space", "route", "target", "water", "wall", "box", "difficulty", "rhythm", "other"}
)
INTENT_CLAIM_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "amount",
        "coverage",
        "density",
        "complexity",
        "length",
        "clarity",
        "concentration",
        "separation",
        "other",
    }
)
INTENT_CLAIM_DIRECTIONS: frozenset[str] = frozenset(
    {"increase", "decrease", "maintain", "avoid", "evaluate", "unspecified"}
)
INTENT_CLAIM_DEGREES: frozenset[str] = frozenset(
    {"excessive", "insufficient", "neutral", "unspecified"}
)
INTENT_CLAIM_ASPECTS: frozenset[str] = frozenset({"visual", "gameplay", "unspecified"})


class SemanticClaim(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    subject: str
    subject_type: str = Field(alias="subjectType")
    subject_text: str = Field(alias="subjectText")
    attribute: str
    attribute_type: str = Field(alias="attributeType")
    attribute_text: str = Field(alias="attributeText")
    direction: str
    degree: str = "neutral"
    aspect: str = "unspecified"
    scope: str = "stage"
    scope_type: str = Field(default="stage", alias="scopeType")
    scope_text: str = Field(default="stage", alias="scopeText")
    normalized_meaning: str = Field(default="", alias="normalizedMeaning")
    evidence_span: str = Field(default="", alias="evidenceSpan")
    semantic_source: Literal["kimi_reviewed", "legacy_unverified"] = Field(
        default="legacy_unverified",
        alias="semanticSource",
    )
    review_version: str | None = Field(default=None, alias="reviewVersion")
    source_user_turn_id: str | None = Field(default=None, alias="sourceUserTurnId")
    confidence: float = 1.0

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> SemanticClaim | None:
        subject = _clean_text(raw.get("subjectType") or raw.get("subject"), 32)
        attribute = _clean_text(raw.get("attributeType") or raw.get("attribute"), 32)
        direction = _clean_text(raw.get("direction"), 32)
        degree = _clean_text(raw.get("degree"), 32) or "neutral"
        aspect = _clean_text(raw.get("aspect"), 32) or "unspecified"
        scope = _clean_text(raw.get("scopeText") or raw.get("scope"), 96) or "stage"
        if (
            subject not in INTENT_CLAIM_SUBJECTS
            or attribute not in INTENT_CLAIM_ATTRIBUTES
            or direction not in INTENT_CLAIM_DIRECTIONS
            or degree not in INTENT_CLAIM_DEGREES
            or aspect not in INTENT_CLAIM_ASPECTS
        ):
            return None
        semantic_source = _clean_text(raw.get("semanticSource"), 32)
        if semantic_source not in {"kimi_reviewed", "legacy_unverified"}:
            semantic_source = "legacy_unverified"
        return cls(
            subject=subject,
            subjectType=subject,
            subjectText=_clean_text(raw.get("subjectText"), 160) or subject,
            attribute=attribute,
            attributeType=attribute,
            attributeText=_clean_text(raw.get("attributeText"), 160) or attribute,
            direction=direction,
            degree=degree,
            aspect=aspect,
            scope=scope,
            scopeType=_clean_text(raw.get("scopeType"), 32)
            or ("stage" if scope == "stage" else "region"),
            scopeText=scope,
            normalizedMeaning=_clean_text(raw.get("normalizedMeaning"), 500),
            evidenceSpan=_clean_text(raw.get("evidenceSpan"), 500),
            semanticSource=semantic_source,  # type: ignore[arg-type]
            reviewVersion=_clean_text(raw.get("reviewVersion"), 32) or None,
            sourceUserTurnId=_opt_source(raw.get("sourceUserTurnId")),
            confidence=_clamp_confidence(raw.get("confidence"), 1.0),
        )


class IntentHypothesis(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    id: str = ""
    topic_key: str = Field(default="other", alias="topicKey")
    statement: str
    status: HypothesisStatus = HypothesisStatus.TENTATIVE
    confidence: float = 0.35
    supporting_evidence_ids: list[str] = Field(default_factory=list, alias="supportingEvidenceIds")
    contradicting_evidence_ids: list[str] = Field(
        default_factory=list,
        alias="contradictingEvidenceIds",
    )
    source_stage_id: str | None = Field(default=None, alias="sourceStageId")
    source_turn_id: str | None = Field(default=None, alias="sourceTurnId")
    last_updated_stage_id: str | None = Field(default=None, alias="lastUpdatedStageId")
    origin: str = "model"
    displayed: bool = False
    original_statement: str | None = Field(default=None, alias="originalStatement")
    confirmed_at_stage_id: str | None = Field(default=None, alias="confirmedAtStageId")
    feedback_action: Literal["confirm", "revise"] | None = Field(
        default=None,
        alias="feedbackAction",
    )
    display_statement: str | None = Field(default=None, alias="displayStatement")
    display_language: str | None = Field(default=None, alias="displayLanguage")
    semantic_claims: list[SemanticClaim] = Field(default_factory=list, alias="semanticClaims")

    @field_validator("statement", mode="before")
    @classmethod
    def _sanitize_statement(cls, value: Any) -> str:
        clean = sanitize_user_design_text(value)
        if not clean:
            raise ValueError("Hypothesis statement cannot be empty")
        return clean

    @field_validator("topic_key", mode="before")
    @classmethod
    def _norm_topic(cls, value: Any) -> str:
        topic = _clean_text(value, 64)
        return topic if topic in INTENT_TOPICS else "other"

    @field_validator("status", mode="before")
    @classmethod
    def _norm_status(cls, value: Any) -> str:
        allowed = {s.value for s in HypothesisStatus}
        return value if value in allowed else HypothesisStatus.TENTATIVE.value

    @field_validator("semantic_claims", mode="before")
    @classmethod
    def _norm_claims(cls, value: Any) -> list[SemanticClaim]:
        if not isinstance(value, list):
            return []
        deduped: dict[tuple[str, ...], SemanticClaim] = {}
        for raw in value:
            if isinstance(raw, SemanticClaim):
                claim = raw
            elif isinstance(raw, Mapping):
                parsed = SemanticClaim.from_raw(raw)
                if parsed is None:
                    continue
                claim = parsed
            else:
                continue
            key = (
                claim.subject,
                claim.attribute,
                claim.direction,
                claim.degree,
                claim.aspect,
                claim.scope,
                claim.normalized_meaning,
                claim.evidence_span,
            )
            deduped[key] = claim
            if len(deduped) >= 4:
                break
        return list(deduped.values())

    @model_validator(mode="after")
    def _normalize_fields(self) -> IntentHypothesis:
        self.id = _clean_text(self.id, 96) or _stable_id(
            "hypothesis", self.topic_key, self.statement
        )
        self.source_stage_id = _opt_source(self.source_stage_id)
        self.source_turn_id = _opt_source(self.source_turn_id)
        self.last_updated_stage_id = _opt_source(self.last_updated_stage_id or self.source_stage_id)
        self.confirmed_at_stage_id = _opt_source(self.confirmed_at_stage_id)
        self.origin = _clean_text(self.origin, 32) or "model"
        self.original_statement = sanitize_user_design_text(self.original_statement) or None
        self.display_statement = sanitize_user_design_text(self.display_statement) or None
        self.display_language = _clean_text(self.display_language, 16) or None
        self.supporting_evidence_ids = list(
            dict.fromkeys(v for v in (_opt_source(x) for x in self.supporting_evidence_ids) if v)
        )[:32]
        self.contradicting_evidence_ids = list(
            dict.fromkeys(v for v in (_opt_source(x) for x in self.contradicting_evidence_ids) if v)
        )[:32]
        if self.status in {
            HypothesisStatus.TENTATIVE.value,
            HypothesisStatus.LEGACY_UNVERIFIED.value,
        }:
            self.confidence = min(_clamp_confidence(self.confidence, 0.35), 0.75)
        elif self.status == HypothesisStatus.CONFIRMED.value:
            self.confidence = _clamp_confidence(self.confidence, 1.0)
        else:
            self.confidence = _clamp_confidence(self.confidence, 0.0)
        return self
