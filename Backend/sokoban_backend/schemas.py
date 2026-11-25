from pydantic import BaseModel, field_validator

from .config import DEFAULT_LLM_MAX_ATTEMPTS


def _reject_bool_int(value):
    if isinstance(value, bool):
        raise ValueError("Boolean value is not allowed for an integer field")
    return value


class PCSolvabilityError(ValueError):
    def __init__(self, reason_code, message, searched_states=0, details=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.searched_states = int(searched_states or 0)
        self.details = dict(details or {})


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


ONLINE_DRAFT_ANSWER_CHOICES = {
    "q1Answer": {"quick_start", "observe_then_decide", "plan_ahead", "no_preference"},
    "q2Answer": {"easy_to_adjust", "consider_order", "connected_pushes", "no_preference"},
    "q3Answer": {"focused_area", "connected_areas", "wide_area", "no_preference"},
    "q4Answer": {"short_routes", "occasional_detours", "long_routes", "no_preference"},
}
ONLINE_DRAFT_METADATA_KEYS = (
    "q1Answer",
    "q2Answer",
    "q3Answer",
    "q4Answer",
    "aiReflection",
    "aiDifficultyRationale",
    "aiLayoutRationale",
    "aiRecommendedDifficulty",
    "aiRecommendedLayout",
    "aiRecommendationSource",
)
ONLINE_DRAFT_RECOMMENDATION_SOURCES = {"llm", "deterministic_fallback"}


class OnlineChallengeRequest(BaseModel):
    rows: list[str]
    aiAssistantMode: str = "description_generation"


class OnlineDesignerIntentionSyncRequest(BaseModel):
    playerNumber: int
    designerIntention: str
    eventId: str | None = ""
    sessionId: str | None = ""

    @field_validator("playerNumber", mode="before")
    @classmethod
    def reject_bool_player_number(cls, value):
        return _reject_bool_int(value)


class OnlineCoCreationFlowEventRequest(BaseModel):
    eventId: str
    playerNumber: int
    eventType: str
    sessionId: str
    versionId: str | None = ""
    stageNumber: int | None = None
    source: str | None = ""
    initialDraftMethod: str | None = ""
    rows: list[str] | None = None
    diff: list[dict] | None = None
    userText: str | None = ""
    assistantText: str | None = ""
    language: str | None = ""
    cards: list[dict] | None = None
    assistantTurnId: str | None = ""
    openingAssistantTurnId: str | None = ""
    openingAssistantText: str | None = ""
    openingLanguage: str | None = ""
    openingCards: list[dict] | None = None
    coCreationDurationSeconds: float | None = None
    nodeId: str | None = ""
    nodeType: str | None = ""
    nodeStatus: str | None = ""
    nodeParentId: str | None = ""
    nodeEntries: list[dict] | None = None
    restoredFromVersionId: str | None = None
    restoredFromStageNumber: int | None = None
    replacedVersionId: str | None = None
    replacedStageNumber: int | None = None

    @field_validator(
        "playerNumber",
        "stageNumber",
        "restoredFromStageNumber",
        "replacedStageNumber",
        mode="before",
    )
    @classmethod
    def reject_bool_integers(cls, value):
        return _reject_bool_int(value)


class OnlineResultRequest(BaseModel):
    durationSeconds: float
    moveCount: int
    minimumMoves: int
    restartCount: int = 0
    outcome: str = "completed"


class LevelDesignPlan(BaseModel):
    minSolutionSteps: int
    maxSolutionSteps: int
    minWaterAreas: int
    maxWaterAreas: int
    minWallObstacleBlocks: int
    maxWallObstacleBlocks: int
    minPushes: int
    maxPushes: int
    minReversePulls: int
    maxReversePulls: int
    style: str
    archetype: str
    targetLayout: str
    obstacleStyle: str
    waterStyle: str
    designNote: str
    corridorPlacement: str = "none"
    corridorWidth: int = 0
    corridorOrientation: str = "any"
    corridorRole: str = "visual_only"
    corridorPriority: str = "preferred"


class CreativeIdeaExpansionRequest(BaseModel):
    ideaId: str | None = ""
    sessionId: str | None = ""
    ideaText: str | None = ""
    sceneName: str | None = ""
    regenerationAttempt: int | None = 0
    previousOptions: list[dict] | None = None


class CreativeIdeaExpansionOption(BaseModel):
    id: str
    title: str
    description: str
    promptText: str


class HARevisionPlanRequest(BaseModel):
    ideaId: str | None = ""
    sessionId: str | None = ""
    gameRoundId: str | None = ""
    gameRoundIndex: int | None = 0
    adjustmentText: str
    sceneName: str | None = ""
    officialRound: bool | None = False
    previousLevelPlan: dict
    corridorValidation: dict | None = None
    regenerationAttempt: int | None = 0
    previousOptions: list[dict] | None = None


class HARevisionPlanEditRequest(BaseModel):
    ideaId: str | None = ""
    sessionId: str | None = ""
    adjustmentText: str
    sceneName: str | None = ""
    previousLevelPlan: dict
    corridorValidation: dict | None = None
    originalOption: dict
    editedDescription: str


class HumanAdjustmentValidationRequest(BaseModel):
    adjustmentText: str


class GenerationPreferences(BaseModel):
    minSolutionSteps: int | None = None
    maxSolutionSteps: int | None = None
    minWaterAreas: int | None = None
    maxWaterAreas: int | None = None
    minWallObstacleBlocks: int | None = None
    maxWallObstacleBlocks: int | None = None
    minPushes: int | None = None
    maxPushes: int | None = None
    minReversePulls: int | None = None
    maxReversePulls: int | None = None
    archetype: str | None = ""
    targetLayout: str | None = ""
    obstacleStyle: str | None = ""
    waterStyle: str | None = ""
    corridorPlacement: str | None = ""
    corridorWidth: int | None = None
    corridorOrientation: str | None = ""
    corridorRole: str | None = ""
    corridorPriority: str | None = ""


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
    previousLevelPlan: str | None = ""
    previousLevelMetrics: str | None = ""
    selectedHAPlan: str | None = ""
    styleDescription: str | None = ""
    generationPreferences: GenerationPreferences | None = None
    dgContext: dict | None = None
    maxAttempts: int | None = DEFAULT_LLM_MAX_ATTEMPTS


class DGGuideSummaryRequest(BaseModel):
    firstMovePreference: str
    pushPlanningPreference: str
    spacePreference: str
    routeRhythmPreference: str
    language: str | None = "en"


class PCLevelGenerationRequest(BaseModel):
    width: int
    height: int
    sketchRows: list[str]
    previousCandidateRows: list[str] | None = None
    rejectionReason: str | None = ""
    maxAttempts: int | None = DEFAULT_LLM_MAX_ATTEMPTS


class RenameRoundRequest(BaseModel):
    roundId: str
    displayName: str


class RenameLevelRunRequest(BaseModel):
    levelRunId: str
    displayName: str


class RoundRequest(BaseModel):
    roundId: str


class DeleteLevelRunRequest(BaseModel):
    levelRunId: str | None = ""


class DeleteSurveyResponseRequest(BaseModel):
    responseId: str | None = ""
    playerNickname: str | None = ""


class DeleteCreativeIdeaRequest(BaseModel):
    ideaId: str | None = ""


class DeleteExpansionChoiceRequest(BaseModel):
    choiceId: str | None = ""


class DeleteHAPlanEventRequest(BaseModel):
    haEventId: str | None = ""


class DeleteJourneyEventRequest(BaseModel):
    journeyEventId: str | None = ""


class DeleteIdeaRecordsRequest(BaseModel):
    ideaId: str | None = ""


class DeleteOnlineMatchRequest(BaseModel):
    matchId: str | None = ""


DEFAULT_PLAN = {
    "minSolutionSteps": 22,
    "maxSolutionSteps": 42,
    "minWaterAreas": 1,
    "maxWaterAreas": 2,
    "minWallObstacleBlocks": 2,
    "maxWallObstacleBlocks": 3,
    "minPushes": 10,
    "maxPushes": 22,
    "minReversePulls": 18,
    "maxReversePulls": 34,
    "style": "hard classic choke route",
    "archetype": "bottleneck_corridor",
    "targetLayout": "split_pair",
    "obstacleStyle": "side_choke",
    "waterStyle": "side_pool",
    "designNote": "Hard two-box route with a forced choke point and separated goals.",
    "corridorPlacement": "none",
    "corridorWidth": 0,
    "corridorOrientation": "any",
    "corridorRole": "visual_only",
    "corridorPriority": "preferred",
}

RECENT_BLUEPRINT_LIMIT = 3

LIMITS = {
    "minSolutionSteps": (18, 30),
    "maxSolutionSteps": (32, 50),
    "minWaterAreas": (0, 2),
    "maxWaterAreas": (0, 2),
    "minWallObstacleBlocks": (0, 2),
    "maxWallObstacleBlocks": (0, 3),
    "minPushes": (8, 16),
    "maxPushes": (14, 28),
    "minReversePulls": (14, 24),
    "maxReversePulls": (24, 40),
}

ENUMS = {
    "archetype": {"goal_room", "bottleneck_corridor", "split_route", "open_workshop"},
    "targetLayout": {"clustered", "split_pair", "edge_cluster"},
    "obstacleStyle": {"central_baffle", "side_choke", "goal_guard"},
    "waterStyle": {"corner_pool", "side_pool", "route_divider"},
    "corridorPlacement": {"none", "center", "side"},
    "corridorOrientation": {"horizontal", "vertical", "any"},
    "corridorRole": {"visual_only", "player_route", "required_box_route"},
    "corridorPriority": {"preferred", "required"},
}

HA_CHANGE_FIELDS = set(LIMITS) | set(ENUMS) | {"corridorWidth", "style"}
