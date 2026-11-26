"""Pydantic v2 structured output contracts for all active LLM tasks."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ._schemas_tasks import (
    AutomaticBindingPayload,
    BinaryBranchPayload,
    BinaryCorridorPayload,
    BinaryDiscoveryPayload,
    BinaryPromisePayload,
    ChallengeReasonReviewPayload,
    DGGuideSummaryPayload,
    EditableOpName,
    EditableTileChar,
    EffectKind,
    ExactTransitionPayload,
    FocusAreaPayload,
    LevelPlanPayload,
    ManualEditConflictCardPayload,
    ManualEditConflictClassificationPayload,
    ManualEditDirectionComparisonPayload,
    ManualEditReviewPayload,
    MapOperationCandidatePayload,
    MapOperationItemPayload,
    MapOperationsPayload,
    MetricGoalPayload,
    PreserveKey,
    ProposalFailureAnalysisPayload,
    ProtectedEntityKey,
    RequiredTransitionPayload,
    RequirementItemPayload,
    RequirementRecordPayload,
    RevisionPlanPayload,
    RevisionStrategyPayload,
    StageAssessmentPayload,
    StrictSchema,
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


class GridCoordinate(StrictSchema):
    """One-based (row 1..10, column 1..12) map coordinate."""

    row: int = Field(ge=1, le=10)
    column: int = Field(ge=1, le=12)


class CoordinateLinkPayload(StrictSchema):
    """Visual route highlight connecting two grounded map coordinates."""

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
    question: str = Field(min_length=1, max_length=400)
    status: Literal["open", "resolved"] = "open"
    evidence_text: str = Field(default="", alias="evidenceText", max_length=400)


class DesignContextPatchPayload(StrictSchema):
    goals: list[PatchTextEntry] = Field(default_factory=list, max_length=4)
    constraints: list[PatchTextEntry] = Field(default_factory=list, max_length=4)
    open_questions: list[PatchQuestionEntry] = Field(
        default_factory=list, alias="openQuestions", max_length=4
    )


class ChatReplyPayload(StrictSchema):
    """Native structured chat response replacing legacy GUIDANCE text scraping."""

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
    evidence_span: str = Field(alias="evidenceSpan", max_length=400)


class HumanEditEngagementPayload(StrictSchema):
    decision: Literal["engage", "unchanged", "unclear"]
    evidence_span: str = Field(default="", alias="evidenceSpan", max_length=400)


class TurnRoutingPayload(StrictSchema):
    """Lightweight Policy-2 turn routing classification."""

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
        Literal["water", "internal_walls", "outer_shell", "player", "boxes", "targets", "unknown"]
    ] = Field(default_factory=list, max_length=7)
    map_related: bool = Field(alias="mapRelated")
    direction_sufficient: bool = Field(alias="directionSufficient")
    route_target: Literal["ordinary", "binary_discovery", "challenge", "revision"] = Field(
        alias="routeTarget"
    )
    evidence_span: str = Field(alias="evidenceSpan", max_length=400)
    changes: list[RequestedChangeRecord] = Field(default_factory=list, max_length=8)
    human_edit_engagement: HumanEditEngagementPayload | None = Field(
        default=None, alias="humanEditEngagement"
    )


class TranslatedBinaryChoicePayload(StrictSchema):
    choice_id: str = Field(alias="choiceId")
    letter: str
    label: str = Field(min_length=1, max_length=240)


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


class TranslatedTurnItemPayload(StrictSchema):
    turn_id: str = Field(alias="turnId", min_length=1, max_length=128)
    body: str = Field(max_length=8000)
    follow_up_question: str | None = Field(default=None, alias="followUpQuestion")
    intent_hypothesis: str | None = Field(default=None, alias="intentHypothesis")
    proposal_offer_summary: str | None = Field(default=None, alias="proposalOfferSummary")
    proposal_offer_rationale: str | None = Field(default=None, alias="proposalOfferRationale")
    proposal_presentation_reason: str | None = Field(
        default=None, alias="proposalPresentationReason"
    )
    proposal_summary: str | None = Field(default=None, alias="proposalSummary")
    ui_cue_texts: list[str] = Field(default_factory=list, alias="uiCueTexts")
    coordinate_link_texts: list[str] | None = Field(default=None, alias="coordinateLinkTexts")
    disagreement: TranslatedDisagreementPayload | None = None
    proposal_question: TranslatedBinaryQuestionPayload | None = Field(
        default=None, alias="proposalQuestion"
    )
    proposal_verified_actions: list[str] | None = Field(
        default=None, alias="proposalVerifiedActions"
    )


class TranslationPayload(StrictSchema):
    translations: list[TranslatedTurnItemPayload] = Field(min_length=1, max_length=32)


class TranslationVerificationPayload(StrictSchema):
    accepted: bool
    reason: str = Field(default="", max_length=400)


__all__ = [
    "AutomaticBindingPayload",
    "BinaryBranchPayload",
    "BinaryCorridorPayload",
    "BinaryDiscoveryPayload",
    "BinaryPromisePayload",
    "ChallengeReasonReviewPayload",
    "ChatReplyPayload",
    "CoordinateLinkPayload",
    "DGGuideSummaryPayload",
    "DesignContextPatchPayload",
    "DisagreementCardPayload",
    "EditableOpName",
    "EditableTileChar",
    "EffectKind",
    "ExactTransitionPayload",
    "FocusAreaPayload",
    "GridCoordinate",
    "GuidanceMove",
    "HumanEditEngagementPayload",
    "IntentClaimPayload",
    "IntentDecisionPayload",
    "LevelPlanPayload",
    "ManualEditConflictCardPayload",
    "ManualEditConflictClassificationPayload",
    "ManualEditDirectionComparisonPayload",
    "ManualEditReviewPayload",
    "MapOperationCandidatePayload",
    "MapOperationItemPayload",
    "MapOperationsPayload",
    "MetricGoalPayload",
    "PatchQuestionEntry",
    "PatchTextEntry",
    "PreserveKey",
    "ProposalFailureAnalysisPayload",
    "ProtectedEntityKey",
    "RequestedChangeRecord",
    "RequiredTransitionPayload",
    "RequirementItemPayload",
    "RequirementRecordPayload",
    "RevisionOfferChangePayload",
    "RevisionOfferPayload",
    "RevisionPlanPayload",
    "RevisionStrategyPayload",
    "StageAssessmentPayload",
    "StrictSchema",
    "TranslatedBinaryChoicePayload",
    "TranslatedBinaryQuestionPayload",
    "TranslatedDisagreementPayload",
    "TranslatedTurnItemPayload",
    "TranslationPayload",
    "TranslationVerificationPayload",
    "TurnRoutingPayload",
    "UICuePayload",
]
