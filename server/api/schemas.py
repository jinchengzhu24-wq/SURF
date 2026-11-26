"""Pydantic v2 request contracts for online matchmaking, study, and co-creation APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


def _reject_bool_number(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("Boolean value is not allowed for a numeric field.")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Online matchmaking & study schemas
# ---------------------------------------------------------------------------


class OnlineRoomCreateRequest(BaseModel):
    studySessionId: str | None = ""
    menuStartedFlow: bool = False


class OnlineRoomJoinRequest(BaseModel):
    roomCode: str | None = ""
    studySessionId: str | None = ""
    menuStartedFlow: bool = False


class OnlineReadyRequest(BaseModel):
    ready: bool = True


class OnlineDraftRequest(BaseModel):
    finalDifficulty: str
    finalLayout: str
    q1Answer: str | None = ""
    q2Answer: str | None = ""
    q3Answer: str | None = ""
    q4Answer: str | None = ""
    aiReflection: str | None = ""
    aiDifficultyRationale: str | None = ""
    aiLayoutRationale: str | None = ""
    aiRecommendedDifficulty: str | None = ""
    aiRecommendedLayout: str | None = ""
    aiRecommendationSource: str | None = ""


class OnlineChallengeRequest(BaseModel):
    rows: list[str]
    aiAssistantMode: str = "description_generation"


class OnlineResultRequest(BaseModel):
    durationSeconds: float
    moveCount: int
    minimumMoves: int
    restartCount: int = 0
    outcome: str = "completed"

    @field_validator("durationSeconds", "moveCount", "minimumMoves", "restartCount", mode="before")
    @classmethod
    def _reject_bools(cls, value: Any) -> Any:
        return _reject_bool_number(value)


class DGGuideSummaryRequest(BaseModel):
    firstMovePreference: str
    pushPlanningPreference: str
    spacePreference: str
    routeRhythmPreference: str
    language: str | None = "en"


class LevelPlanRequest(BaseModel):
    ideaText: str | None = ""
    ideaId: str | None = ""
    sessionId: str | None = ""
    sceneName: str | None = ""
    originalIdeaText: str | None = ""
    selectedDirectionText: str | None = ""
    refinementFeedbackText: str | None = ""
    adjustmentHistoryText: str | None = ""
    latestAdjustmentText: str | None = ""
    revisionMode: str | None = ""
    previousLevelPlan: str | dict[str, Any] | None = ""
    previousLevelMetrics: str | dict[str, Any] | None = ""
    selectedHAPlan: str | None = ""
    styleDescription: str | None = ""
    generationPreferences: dict[str, Any] | None = None
    dgContext: dict[str, Any] | None = None
    maxAttempts: int | None = 2


class DeleteOnlineMatchRequest(BaseModel):
    matchId: str | None = ""


# ---------------------------------------------------------------------------
# Co-creation workbench & integration schemas
# ---------------------------------------------------------------------------


class CreateSessionRequest(StrictModel):
    rows: list[str]
    initialDraftMethod: Literal["partial_completion", "description_generation"]
    language: Literal["en", "zh-CN"] = "zh-CN"
    idempotencyKey: str
    matchId: str | None = None
    playerNumber: int | None = None

    @field_validator("playerNumber", mode="before")
    @classmethod
    def _reject_bool_player_number(cls, value: Any) -> Any:
        return _reject_bool_number(value)


class DemoSessionRequest(StrictModel):
    language: Literal["en", "zh-CN"] = "zh-CN"
    idempotencyKey: str


class BrowserAccessRequest(StrictModel):
    bootstrapToken: str


class LanguageRequest(StrictModel):
    language: Literal["en", "zh-CN"]


class TranslationRequest(StrictModel):
    turnIds: list[str]


class VersionRequest(StrictModel):
    rows: list[str]
    baseVersionId: str
    idempotencyKey: str
    summary: str = ""


class RestoreRequest(StrictModel):
    baseVersionId: str
    idempotencyKey: str


class AssessmentRequest(StrictModel):
    idempotencyKey: str


class PlayAttemptRequest(StrictModel):
    idempotencyKey: str


class DraftRegenerationRequest(StrictModel):
    idempotencyKey: str


class DraftRegenerationCompleteRequest(StrictModel):
    rows: list[str]


class DraftRegenerationFailRequest(StrictModel):
    failureCode: Literal[
        "generation_failed",
        "llm_failed",
        "validation_failed",
        "unity_unavailable",
        "blueprint_unavailable",
        "generation_timed_out",
    ]


class FinalizeRequest(StrictModel):
    baseVersionId: str
    idempotencyKey: str
    rows: list[str] | None = None


class IntentionRequest(StrictModel):
    content: str
    idempotencyKey: str


class MessageRequest(StrictModel):
    content: str = ""
    baseVersionId: str
    idempotencyKey: str
    requestProposal: bool = False
    action: Literal[
        "none",
        "send",
        "execute_revision",
        "challenge_revision",
        "alternative_revision",
        "continue_challenge",
    ] = "none"
    sourceTurnId: str | None = None
    assistantTurnId: str | None = None
    proposalId: str | None = None
    challengeId: str | None = None
    challengeChoice: Literal[
        "ai",
        "user",
        "retain_current",
        "keep_proposal",
        "revise_proposal",
        "cancel_challenge",
    ] | None = None
    exitChallenge: bool = False


class BinaryCancelRequest(StrictModel):
    baseVersionId: str
    topicId: str
    idempotencyKey: str


class BinaryChoiceRequest(BinaryCancelRequest):
    questionId: str
    choiceId: str
    displayLanguage: Literal["en", "zh-CN"] = "zh-CN"


class QuestionFeedbackRequest(StrictModel):
    action: Literal["ignore", "restore"]
    baseVersionId: str
    idempotencyKey: str


class IntentFeedbackRequest(StrictModel):
    action: Literal["confirm", "reject", "revise", "keep"]
    candidateText: str | None = None
    selectedHypothesisId: str | None = None
    sourceTurnId: str | None = None
    baseVersionId: str
    idempotencyKey: str


class ComponentRecoveryRequest(StrictModel):
    baseVersionId: str
    idempotencyKey: str


class ProposalDecisionRequest(StrictModel):
    decision: Literal["accept", "reject"]
    baseVersionId: str
    idempotencyKey: str
    reason: str = ""


class PlayBootstrapRequest(StrictModel):
    ticket: str


class PlayMetricsRequest(StrictModel):
    attemptToken: str
    durationSeconds: float = 0
    moveCount: int = 0
    pushCount: int = 0
    restartCount: int = 0
    minimumMoves: int = -1
    minimumPushes: int = -1

    @field_validator(
        "durationSeconds",
        "moveCount",
        "pushCount",
        "restartCount",
        "minimumMoves",
        "minimumPushes",
        mode="before",
    )
    @classmethod
    def _reject_bool_metrics(cls, value: Any) -> Any:
        return _reject_bool_number(value)
