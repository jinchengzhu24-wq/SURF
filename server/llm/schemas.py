"""Pydantic v2 structured output contracts for all active LLM tasks."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ._schemas_proposals import (
    AutomaticBindingPayload,
    BinaryBranchPayload,
    BinaryCorridorPayload,
    BinaryDiscoveryFollowupFinalPayload,
    BinaryDiscoveryFollowupPayload,
    BinaryDiscoveryInitialFinalPayload,
    BinaryDiscoveryInitialPayload,
    BinaryDiscoveryPayload,
    BinaryPreserveKey,
    BinaryPromisePayload,
    BinaryStrategyPayload,
    EntityId,
    ExactTransitionPayload,
    FocusAreaPayload,
    MapOperationCandidatePayload,
    MapOperationItemPayload,
    MapOperationsPayload,
    MetricGoalPayload,
    ProposalFailureAnalysisPayload,
    ReduceObstaclesPromisePayload,
    RequiredTransitionPayload,
    RequirementItemPayload,
    RequirementRecordPayload,
    ReshapeWaterPromisePayload,
    RevisionPlanPayload,
    RevisionStrategyPayload,
    ShorterSolutionPromisePayload,
    WidenCorridorPromisePayload,
    select_binary_discovery_schema,
)
from ._schemas_tasks import (
    ChallengeReasonReviewPayload,
    DGGuideSummaryPayload,
    EditableOpName,
    EditableTileChar,
    EffectKind,
    LevelPlanPayload,
    ManualEditConflictCardPayload,
    ManualEditConflictClassificationPayload,
    ManualEditDirectionComparisonPayload,
    ManualEditReviewNoConflictPayload,
    ManualEditReviewPayload,
    ManualEditReviewWithConflictPayload,
    PreserveKey,
    ProtectedEntityKey,
    StageAssessmentPayload,
    StrictSchema,
    build_constrained_dg_guide_model,
    build_constrained_level_plan_model,
    build_manual_edit_conflict_classification_model,
)

GuidanceMove = Literal[
    "observe_stage",
    "clarify_intent",
    "offer_perspective",
    "challenge_tradeoff",
    "reflect_on_play",
    "offer_revision",
    "deliver_revision",
]

OrdinaryChatMove = Literal[
    "clarify_intent",
    "offer_perspective",
    "challenge_tradeoff",
    "reflect_on_play",
]


class GridCoordinate(StrictSchema):
    """One-based (row 1..10, column 1..12) map coordinate."""

    row: int = Field(ge=1, le=10)
    column: int = Field(ge=1, le=12)


class CoordinateLinkPayload(StrictSchema):
    """Visual route highlight connecting two grounded map coordinates.

    Optional card flaws (e.g. from == to or >220 chars) are dropped safely by
    grounding.py so a verified assistantMessage is never discarded.
    """

    text: str = Field(min_length=1, max_length=240)
    from_coord: GridCoordinate = Field(alias="from")
    to_coord: GridCoordinate = Field(alias="to")


class UICuePayload(StrictSchema):
    type: Literal["manual_edit", "warning", "clarification"]
    text: str = Field(min_length=1, max_length=400)


class IntentClaimPayload(StrictSchema):
    normalized_meaning: str = Field(alias="normalizedMeaning", max_length=320)
    subject_type: Literal[
        "layout",
        "space",
        "route",
        "target",
        "water",
        "wall",
        "box",
        "difficulty",
        "rhythm",
        "other",
    ] = Field(alias="subjectType")
    subject_text: str = Field(alias="subjectText", max_length=120)
    attribute_type: Literal[
        "amount",
        "coverage",
        "density",
        "complexity",
        "length",
        "clarity",
        "concentration",
        "separation",
        "other",
    ] = Field(alias="attributeType")
    attribute_text: str = Field(alias="attributeText", max_length=120)
    direction: Literal["increase", "decrease", "maintain", "avoid", "evaluate", "unspecified"]
    degree: Literal["excessive", "insufficient", "neutral", "unspecified"] = "neutral"
    aspect: Literal["visual", "gameplay", "unspecified"] = "unspecified"
    scope_type: Literal["stage", "region", "entity", "other"] = Field(
        default="stage", alias="scopeType"
    )
    scope_text: str = Field(default="stage", alias="scopeText", max_length=96)
    evidence_span: str = Field(alias="evidenceSpan", max_length=400)


class IntentDecisionPayload(StrictSchema):
    classification: Literal["none", "candidate", "reference_only"] = "none"
    claims: list[IntentClaimPayload] = Field(default_factory=list, max_length=4)
    card_text: str = Field(default="", alias="cardText", max_length=500)

    @model_validator(mode="after")
    def _normalize_classification_consistency(self) -> IntentDecisionPayload:
        """Safely normalize malformed optional intent cards without failing the whole turn."""
        if self.classification == "candidate":
            valid_claims = [
                claim
                for claim in self.claims
                if claim.evidence_span.strip()
                and claim.normalized_meaning.strip()
                and claim.subject_text.strip()
                and claim.attribute_text.strip()
            ]
            if not valid_claims or not self.card_text.strip():
                self.classification = "none"
                self.claims = []
                self.card_text = ""
            else:
                self.claims = valid_claims
        else:
            self.claims = []
            self.card_text = ""
        return self


class RevisionOfferChangePayload(StrictSchema):
    row: int = Field(ge=1, le=10)
    column: int = Field(ge=1, le=12)
    before: EditableTileChar
    after: EditableTileChar
    operation: EditableOpName


class RevisionOfferPayload(StrictSchema):
    summary: str = Field(min_length=1, max_length=240)
    rationale: str = Field(min_length=1, max_length=600)
    objective: str = Field(min_length=1, max_length=200)
    changes: list[RevisionOfferChangePayload] = Field(min_length=1, max_length=12)
    preserve: list[PreserveKey] = Field(default_factory=list)
    expected_effect: str = Field(alias="expectedEffect", max_length=320)


class DisagreementCardPayload(StrictSchema):
    status: Literal["active", "acknowledged", "resolved"] = "active"
    subject: Literal["ai_revision", "human_edit", "user_request"] = "user_request"
    user_position: str = Field(alias="userPosition", max_length=400)
    ai_position: str = Field(alias="aiPosition", max_length=400)
    core_disagreement: str = Field(alias="coreDisagreement", max_length=800)
    next_question: str = Field(alias="nextQuestion", max_length=300)
    resolution: Literal["user", "ai", "compromise", "retain_current"] | None = None


class PatchTextEntry(StrictSchema):
    text: str = Field(min_length=1, max_length=400)
    evidence_text: str = Field(default="", alias="evidenceText", max_length=400)


class PatchQuestionEntry(StrictSchema):
    """Open-question patch entry (missing evidenceText on resolved is dropped in questions.py)."""

    question: str = Field(min_length=1, max_length=400)
    status: Literal["open", "resolved"] = "open"
    evidence_text: str = Field(default="", alias="evidenceText", max_length=400)


class DesignContextPatchPayload(StrictSchema):
    goals: list[PatchTextEntry] = Field(default_factory=list, max_length=4)
    constraints: list[PatchTextEntry] = Field(default_factory=list, max_length=4)
    open_questions: list[PatchQuestionEntry] = Field(
        default_factory=list, alias="openQuestions", max_length=4
    )


class StageOpeningPayload(StrictSchema):
    """Focused structured schema for Stage opening observations."""

    assistant_message: str = Field(alias="assistantMessage", min_length=1, max_length=8000)
    coordinate_links: list[CoordinateLinkPayload] = Field(
        default_factory=list, alias="coordinateLinks", max_length=12
    )
    assessment: StageAssessmentPayload

    @model_validator(mode="after")
    def _validate_non_blank(self) -> StageOpeningPayload:
        if not self.assistant_message.strip():
            raise ValueError("assistantMessage must not be blank.")
        return self


class OrdinaryChatReplyPayload(StrictSchema):
    """Focused structured schema for ordinary co-creation chat without active disagreement."""

    assistant_message: str = Field(alias="assistantMessage", min_length=1, max_length=8000)
    move: OrdinaryChatMove = "offer_perspective"
    intent_decision: IntentDecisionPayload = Field(alias="intentDecision")
    intent_confidence: Literal["low", "medium", "high"] | None = Field(
        default=None, alias="intentConfidence"
    )
    ui_cues: list[UICuePayload] = Field(default_factory=list, alias="uiCues", max_length=2)
    coordinate_links: list[CoordinateLinkPayload] = Field(
        default_factory=list, alias="coordinateLinks", max_length=12
    )
    design_context_patch: DesignContextPatchPayload | None = Field(
        default=None, alias="designContextPatch"
    )

    @model_validator(mode="after")
    def _validate_reply(self) -> OrdinaryChatReplyPayload:
        if not self.assistant_message.strip():
            raise ValueError("assistantMessage must not be blank.")
        if self.intent_decision.classification != "candidate":
            self.intent_confidence = None
        return self


class OrdinaryChatWithDisagreementPayload(OrdinaryChatReplyPayload):
    """Ordinary co-creation chat schema when an active disagreement allows a followUpQuestion."""

    follow_up_question: str | None = Field(default=None, alias="followUpQuestion", max_length=320)


class ChatReplyPayload(StrictSchema):
    """Backward-compatible native structured chat response envelope."""

    assistant_message: str = Field(alias="assistantMessage", min_length=1, max_length=8000)
    move: GuidanceMove = "offer_perspective"
    follow_up_question: str | None = Field(default=None, alias="followUpQuestion", max_length=320)
    intent_decision: IntentDecisionPayload | None = Field(default=None, alias="intentDecision")
    intent_confidence: Literal["low", "medium", "high"] | None = Field(
        default=None, alias="intentConfidence"
    )
    revision_offer: RevisionOfferPayload | None = Field(default=None, alias="revisionOffer")
    disagreement: DisagreementCardPayload | None = None
    ui_cues: list[UICuePayload] = Field(default_factory=list, alias="uiCues", max_length=2)
    coordinate_links: list[CoordinateLinkPayload] = Field(
        default_factory=list, alias="coordinateLinks", max_length=12
    )
    assessment: StageAssessmentPayload | None = None
    design_context_patch: DesignContextPatchPayload | None = Field(
        default=None, alias="designContextPatch"
    )


class RequestedChangeRecord(StrictSchema):
    component: Literal[
        "water",
        "internal_walls",
        "outer_shell",
        "player",
        "boxes",
        "targets",
        "gameplay",
        "unknown",
    ]
    property: Literal[
        "count", "position", "shape", "route", "push_order", "switching", "appearance", "other"
    ]
    operation: Literal["change", "preserve", "mention"]
    evidence_span: str = Field(alias="evidenceSpan", min_length=1, max_length=400)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> RequestedChangeRecord:
        if not self.evidence_span.strip():
            raise ValueError("changes[].evidenceSpan must not be blank.")
        return self


class HumanEditEngagementPayload(StrictSchema):
    decision: Literal["engage", "unchanged", "unclear"]
    evidence_span: str = Field(default="", alias="evidenceSpan", max_length=400)

    @model_validator(mode="after")
    def _validate_engagement(self) -> HumanEditEngagementPayload:
        if self.decision == "engage" and not self.evidence_span.strip():
            raise ValueError(
                "humanEditEngagement.evidenceSpan must not be blank when decision is 'engage'."
            )
        return self


class HumanEditEngagementRecoveryPayload(StrictSchema):
    """Focused schema for recovering human_edit engagement without full turn routing."""

    decision: Literal["engage", "unchanged", "unclear"]
    evidence_span: str = Field(default="", alias="evidenceSpan", max_length=400)

    @model_validator(mode="after")
    def _validate_engagement(self) -> HumanEditEngagementRecoveryPayload:
        if self.decision == "engage" and not self.evidence_span.strip():
            raise ValueError(
                "evidenceSpan must not be blank when decision is 'engage'."
            )
        return self


class TurnRoutingPayload(StrictSchema):
    """Lightweight Policy-2 turn routing classification (no active human-edit conflict)."""

    acts: list[
        Literal[
            "evaluation",
            "intent",
            "explanation_request",
            "idea_request",
            "revision_request",
            "challenge",
            "unclear",
        ]
    ] = Field(min_length=1, max_length=5)
    elements: list[
        Literal[
            "water",
            "internal_walls",
            "outer_shell",
            "player",
            "boxes",
            "targets",
            "gameplay",
            "unknown",
        ]
    ] = Field(default_factory=list, max_length=8)
    map_related: bool = Field(alias="mapRelated")
    direction_sufficient: bool = Field(alias="directionSufficient")
    route_target: Literal["ordinary", "binary_discovery"] = Field(alias="routeTarget")
    evidence_span: str = Field(default="", alias="evidenceSpan", max_length=400)
    changes: list[RequestedChangeRecord] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _validate_evidence_span(self) -> TurnRoutingPayload:
        if self.map_related and not self.evidence_span.strip():
            raise ValueError("evidenceSpan must not be blank when mapRelated is true.")
        return self


class TurnRoutingWithHumanEditPayload(TurnRoutingPayload):
    """Policy-2 turn routing with required human-edit disagreement engagement judgment."""

    human_edit_engagement: HumanEditEngagementPayload = Field(alias="humanEditEngagement")


class TranslatedBinaryChoicePayload(StrictSchema):
    choice_id: str = Field(alias="choiceId")
    letter: str
    label: str = Field(min_length=1, max_length=240)


class TranslatedBinaryChoiceTextPayload(StrictSchema):
    """Slim text-only binary choice translation payload (server preserves immutable metadata)."""

    choice_id: str = Field(alias="choiceId", min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> TranslatedBinaryChoiceTextPayload:
        if not self.choice_id.strip():
            raise ValueError("choices[].choiceId must not be blank.")
        if not self.label.strip():
            raise ValueError("choices[].label must not be blank.")
        return self


class TranslatedBinaryQuestionPayload(StrictSchema):
    question_id: str = Field(alias="questionId")
    topic_id: str = Field(default="", alias="topicId")
    base_version_id: str = Field(default="", alias="baseVersionId")
    question: str = Field(min_length=1, max_length=600)
    note: str = Field(default="", max_length=600)
    number: int = 1
    choices: list[TranslatedBinaryChoicePayload] = Field(min_length=2, max_length=2)
    selected_choice_id: str | None = Field(default=None, alias="selectedChoiceId")
    status: Literal["pending", "selected", "expired"] = "pending"


class TranslatedBinaryQuestionTextPayload(StrictSchema):
    """Slim text-only binary question translation payload (no immutable IDs/status)."""

    question: str = Field(min_length=1, max_length=600)
    note: str = Field(default="", max_length=600)
    choices: list[TranslatedBinaryChoiceTextPayload] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> TranslatedBinaryQuestionTextPayload:
        if not self.question.strip():
            raise ValueError("proposalQuestion.question must not be blank.")
        return self


class TranslatedDisagreementPayload(StrictSchema):
    status: str
    subject: str | None = None
    resolution: str | None = None
    phase: str | None = None
    display_card: bool = Field(default=True, alias="displayCard")
    user_position: str | None = Field(default=None, alias="userPosition")
    ai_position: str | None = Field(default=None, alias="aiPosition")
    core_disagreement: str | None = Field(default=None, alias="coreDisagreement")
    next_question: str | None = Field(default=None, alias="nextQuestion")


class TranslatedDisagreementTextPayload(StrictSchema):
    """Slim text-only disagreement translation payload (server merges immutable metadata)."""

    user_position: str | None = Field(default=None, alias="userPosition", max_length=400)
    ai_position: str | None = Field(default=None, alias="aiPosition", max_length=400)
    core_disagreement: str | None = Field(default=None, alias="coreDisagreement", max_length=800)
    next_question: str | None = Field(default=None, alias="nextQuestion", max_length=300)


class TranslatedPlainTurnItemPayload(StrictSchema):
    """Slim translation payload for plain text turns without cards, cues, or links."""

    turn_id: str = Field(alias="turnId", min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=8000)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> TranslatedPlainTurnItemPayload:
        if not self.turn_id.strip():
            raise ValueError("turnId must not be blank.")
        if not self.body.strip():
            raise ValueError("body must not be blank.")
        return self


class PlainTranslationPayload(StrictSchema):
    translations: list[TranslatedPlainTurnItemPayload] = Field(min_length=1, max_length=32)


class TranslatedTurnItemPayload(StrictSchema):
    turn_id: str = Field(alias="turnId", min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=8000)
    follow_up_question: str | None = Field(default=None, alias="followUpQuestion")
    intent_hypothesis: str | None = Field(default=None, alias="intentHypothesis")
    proposal_offer_summary: str | None = Field(default=None, alias="proposalOfferSummary")
    proposal_offer_rationale: str | None = Field(default=None, alias="proposalOfferRationale")
    proposal_presentation_reason: str | None = Field(
        default=None, alias="proposalPresentationReason"
    )
    ui_cue_texts: list[str] = Field(default_factory=list, alias="uiCueTexts")
    coordinate_link_texts: list[str] | None = Field(default=None, alias="coordinateLinkTexts")
    disagreement: TranslatedDisagreementTextPayload | None = None
    proposal_question: TranslatedBinaryQuestionTextPayload | None = Field(
        default=None, alias="proposalQuestion"
    )
    proposal_verified_actions: list[str] | None = Field(
        default=None, alias="proposalVerifiedActions"
    )

    @model_validator(mode="after")
    def _validate_non_blank(self) -> TranslatedTurnItemPayload:
        if not self.turn_id.strip():
            raise ValueError("turnId must not be blank.")
        if not self.body.strip():
            raise ValueError("body must not be blank.")
        return self


class TranslationPayload(StrictSchema):
    translations: list[TranslatedTurnItemPayload] = Field(min_length=1, max_length=32)


class TranslationVerificationPayload(StrictSchema):
    accepted: bool
    reason: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _validate_rejection_reason(self) -> TranslationVerificationPayload:
        if not self.accepted and not self.reason.strip():
            raise ValueError("reason must not be blank when accepted is false.")
        return self


__all__ = [
    "AutomaticBindingPayload",
    "BinaryBranchPayload",
    "BinaryCorridorPayload",
    "BinaryDiscoveryFollowupFinalPayload",
    "BinaryDiscoveryFollowupPayload",
    "BinaryDiscoveryInitialFinalPayload",
    "BinaryDiscoveryInitialPayload",
    "BinaryDiscoveryPayload",
    "BinaryPreserveKey",
    "BinaryPromisePayload",
    "BinaryStrategyPayload",
    "ChallengeReasonReviewPayload",
    "ChatReplyPayload",
    "CoordinateLinkPayload",
    "DGGuideSummaryPayload",
    "DesignContextPatchPayload",
    "DisagreementCardPayload",
    "EditableOpName",
    "EditableTileChar",
    "EffectKind",
    "EntityId",
    "ExactTransitionPayload",
    "FocusAreaPayload",
    "GridCoordinate",
    "GuidanceMove",
    "HumanEditEngagementPayload",
    "HumanEditEngagementRecoveryPayload",
    "IntentClaimPayload",
    "IntentDecisionPayload",
    "LevelPlanPayload",
    "ManualEditConflictCardPayload",
    "ManualEditConflictClassificationPayload",
    "ManualEditDirectionComparisonPayload",
    "ManualEditReviewNoConflictPayload",
    "ManualEditReviewPayload",
    "ManualEditReviewWithConflictPayload",
    "MapOperationCandidatePayload",
    "MapOperationItemPayload",
    "MapOperationsPayload",
    "MetricGoalPayload",
    "OrdinaryChatMove",
    "OrdinaryChatReplyPayload",
    "OrdinaryChatWithDisagreementPayload",
    "PatchQuestionEntry",
    "PatchTextEntry",
    "PlainTranslationPayload",
    "PreserveKey",
    "ProposalFailureAnalysisPayload",
    "ProtectedEntityKey",
    "ReduceObstaclesPromisePayload",
    "RequestedChangeRecord",
    "RequiredTransitionPayload",
    "RequirementItemPayload",
    "RequirementRecordPayload",
    "ReshapeWaterPromisePayload",
    "RevisionOfferChangePayload",
    "RevisionOfferPayload",
    "RevisionPlanPayload",
    "RevisionStrategyPayload",
    "ShorterSolutionPromisePayload",
    "StageAssessmentPayload",
    "StageOpeningPayload",
    "StrictSchema",
    "TranslatedBinaryChoicePayload",
    "TranslatedBinaryChoiceTextPayload",
    "TranslatedBinaryQuestionPayload",
    "TranslatedBinaryQuestionTextPayload",
    "TranslatedDisagreementPayload",
    "TranslatedDisagreementTextPayload",
    "TranslatedPlainTurnItemPayload",
    "TranslatedTurnItemPayload",
    "TranslationPayload",
    "TranslationVerificationPayload",
    "TurnRoutingPayload",
    "TurnRoutingWithHumanEditPayload",
    "UICuePayload",
    "WidenCorridorPromisePayload",
    "build_constrained_dg_guide_model",
    "build_constrained_level_plan_model",
    "build_manual_edit_conflict_classification_model",
    "select_binary_discovery_schema",
]
