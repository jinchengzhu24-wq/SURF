"""Pydantic v2 contracts for proposals, binary discovery, reviews, translation, and 8000 tasks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.domain.blueprints import (
    BLUEPRINT_LIMITS,
    BlueprintArchetype,
    CorridorOrientation,
    CorridorPlacement,
    CorridorPriority,
    CorridorRole,
    DifficultyLevel,
    LayoutStyle,
    ObstacleStyle,
    TargetLayout,
    WaterStyle,
)

EditableOpName = Literal["add_wall", "remove_wall", "add_water", "remove_water"]
EditableTileChar = Literal[".", "#", "@"]
ProtectedEntityKey = Literal["player", "boxes", "targets", "outer_shell"]
PreserveKey = Literal[
    "outer_shell", "player", "boxes", "targets", "water", "walls", "unrelated_areas"
]
EffectKind = Literal["open_route", "narrow_route", "adjust_internal_walls", "reshape_water"]


class StrictSchema(BaseModel):
    """Base strict Pydantic v2 schema forbidding extra fields and using enum values."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", use_enum_values=True)


class StageAssessmentPayload(StrictSchema):
    """Archival Stage opening assessment (never shown as a standalone card)."""

    solution_summary: str = Field(alias="solutionSummary", max_length=400)
    difficulty_opinion: str = Field(alias="difficultyOpinion", max_length=400)
    features: list[str] = Field(default_factory=list, max_length=6)
    suggestions: list[str] = Field(default_factory=list, max_length=6)
    satisfaction_question: str | None = Field(default=None, alias="satisfactionQuestion")


class RequirementItemPayload(StrictSchema):
    component: Literal["water", "wall", "player", "box", "target", "gameplay"]
    property: Literal[
        "count",
        "positions",
        "distribution",
        "distance",
        "change_scope",
        "solutionSteps",
        "minimumPushes",
        "solutionPushes",
        "boxAlternations",
        "longestPushRun",
        "timing",
        "dependency",
        "experience",
        "appearance",
    ]
    relation: Literal[
        "increase",
        "decrease",
        "equal",
        "preserve",
        "relocate",
        "nearer",
        "farther",
        "early",
        "later",
        "seek",
        "avoid",
    ]
    strength: Literal["invariant", "goal", "preference"]
    entities: list[str] = Field(default_factory=list, max_length=5)
    value: int | None = Field(default=None, ge=0)
    unit: Literal["absolute", "delta", "none"]
    priority_evidence_span: str = Field(default="", alias="priorityEvidenceSpan", max_length=400)
    focused: bool = False
    source_turn_id: str = Field(alias="sourceTurnId", min_length=1, max_length=128)
    evidence_span: str = Field(alias="evidenceSpan", min_length=1, max_length=400)
    statement: str = Field(min_length=1, max_length=400)
    scope: Literal["all", "original_cells"] = "all"


class AutomaticBindingPayload(StrictSchema):
    entity: str = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=300)


class ExactTransitionPayload(StrictSchema):
    row: int = Field(ge=1, le=10)
    column: int = Field(ge=1, le=12)
    before: EditableTileChar = Field(alias="from")
    after: EditableTileChar = Field(alias="to")
    source_turn_id: str = Field(alias="sourceTurnId", min_length=1, max_length=128)
    evidence_span: str = Field(alias="evidenceSpan", min_length=1, max_length=400)


class RequirementRecordPayload(StrictSchema):
    requirements: list[RequirementItemPayload] = Field(default_factory=list, max_length=48)
    automatic_bindings: list[AutomaticBindingPayload] = Field(
        default_factory=list, alias="automaticBindings", max_length=8
    )
    exact_transitions: list[ExactTransitionPayload] = Field(
        default_factory=list, alias="exactTransitions", max_length=12
    )


class FocusAreaPayload(StrictSchema):
    row: int = Field(ge=1, le=10)
    column: int = Field(ge=1, le=12)
    radius: int = Field(ge=1, le=3)


class MetricGoalPayload(StrictSchema):
    metric: Literal["solutionSteps", "solutionPushes", "minimumPushes", "searchedStates"]
    direction: Literal["increase", "decrease", "preserve"]
    minimum_delta: int = Field(default=1, alias="minimumDelta", ge=1, le=1000)


class RequiredTransitionPayload(StrictSchema):
    row: int = Field(ge=1, le=10)
    column: int = Field(ge=1, le=12)
    before: EditableTileChar = Field(alias="from")
    after: EditableTileChar = Field(alias="to")


class RevisionStrategyPayload(StrictSchema):
    effect: EffectKind
    focus: FocusAreaPayload | None = None
    operators: list[EditableOpName] = Field(min_length=1, max_length=4)
    preserve: list[PreserveKey] = Field(default_factory=list, max_length=7)
    edit_budget: int = Field(alias="editBudget", ge=1, le=12)
    metric_goals: list[MetricGoalPayload] = Field(
        default_factory=list, alias="metricGoals", max_length=3
    )
    required_transitions: list[RequiredTransitionPayload] = Field(
        default_factory=list, alias="requiredTransitions", max_length=12
    )
    anchor_entities: list[Literal["P", "B1", "B2", "T1", "T2"]] = Field(
        default_factory=list, alias="anchorEntities", max_length=5
    )
    play_objective: str | None = Field(default=None, alias="playObjective", max_length=120)
    display_reason: str | None = Field(default=None, alias="displayReason", max_length=600)


class RevisionPlanPayload(StrictSchema):
    strategies: list[RevisionStrategyPayload] = Field(min_length=1, max_length=3)


class MapOperationItemPayload(StrictSchema):
    """Single AI-editable map operation (strictly wall/water edits ONLY)."""

    row: int = Field(ge=1, le=10)
    column: int = Field(ge=1, le=12)
    op: EditableOpName
    before: EditableTileChar = Field(alias="from")
    after: EditableTileChar = Field(alias="to")


class MapOperationCandidatePayload(StrictSchema):
    strategy_index: int = Field(default=1, alias="strategyIndex", ge=1, le=3)
    rationale: str = Field(default="", max_length=400)
    operations: list[MapOperationItemPayload] = Field(min_length=1, max_length=12)


class MapOperationsPayload(StrictSchema):
    candidates: list[MapOperationCandidatePayload] = Field(min_length=1, max_length=3)


class BinaryCorridorPayload(StrictSchema):
    row: int = Field(ge=1, le=10)
    column: int = Field(ge=1, le=12)
    axis: Literal["horizontal", "vertical"]


class BinaryPromisePayload(StrictSchema):
    kind: Literal["reduce_obstacles", "widen_corridor", "shorter_solution", "reshape_water"]
    component: Literal["water", "internal_walls", "obstacles"]
    focus: FocusAreaPayload | None = None
    corridor: BinaryCorridorPayload | None = None


class BinaryBranchPayload(StrictSchema):
    label: str = Field(min_length=1, max_length=240)
    strategy: RevisionStrategyPayload
    promise: BinaryPromisePayload


class BinaryDiscoveryPayload(StrictSchema):
    """Policy-2 single-slot binary choice discovery output."""

    action: Literal["ask", "plan", "protected_refusal", "blocked"]
    question: str = Field(default="", max_length=600)
    note: str = Field(default="", max_length=600)
    branches: list[BinaryBranchPayload] = Field(default_factory=list, max_length=2)
    plan_strategies: list[RevisionStrategyPayload] = Field(
        default_factory=list, alias="planStrategies", max_length=3
    )
    protected_entities: list[ProtectedEntityKey] = Field(
        default_factory=list, alias="protectedEntities", max_length=4
    )
    initial_requirements: RequirementRecordPayload | None = Field(
        default=None, alias="initialRequirements"
    )


class ManualEditDirectionComparisonPayload(StrictSchema):
    direction_id: str = Field(alias="directionId", min_length=1, max_length=64)
    relation: Literal["conflict", "aligned", "tradeoff", "unrelated", "unclear"]
    explanation: str = Field(min_length=1, max_length=800)


class ManualEditConflictClassificationPayload(StrictSchema):
    comparisons: list[ManualEditDirectionComparisonPayload] = Field(min_length=1, max_length=24)


class ManualEditConflictCardPayload(StrictSchema):
    user_position: str = Field(alias="userPosition", min_length=1, max_length=400)
    ai_position: str = Field(alias="aiPosition", min_length=1, max_length=400)
    core_disagreement: str = Field(alias="coreDisagreement", min_length=1, max_length=800)
    next_question: str = Field(alias="nextQuestion", min_length=1, max_length=300)


class ManualEditReviewPayload(StrictSchema):
    opening_message: str = Field(alias="openingMessage", min_length=1, max_length=4000)
    assessment: StageAssessmentPayload
    review_message: str = Field(alias="reviewMessage", min_length=1, max_length=4000)
    conflict: ManualEditConflictCardPayload | None = None


class ChallengeReasonReviewPayload(StrictSchema):
    relation: Literal["primary", "secondary", "different", "unclear"]
    merit: Literal["reasonable", "not_yet_reasonable", "unclear"]
    comparison: str = Field(min_length=1, max_length=800)


class ProposalFailureAnalysisPayload(StrictSchema):
    body: str = Field(min_length=1, max_length=2000)


class DGGuideSummaryPayload(StrictSchema):
    summary: str = Field(min_length=1, max_length=480)
    recommended_difficulty: DifficultyLevel = Field(alias="recommendedDifficulty")
    difficulty_rationale: str = Field(alias="difficultyRationale", min_length=1, max_length=320)
    recommended_layout: LayoutStyle = Field(alias="recommendedLayout")
    layout_rationale: str = Field(alias="layoutRationale", min_length=1, max_length=320)


class LevelPlanPayload(StrictSchema):
    """Structured blueprint contract for POST /generate-level-plan."""

    min_solution_steps: int = Field(alias="minSolutionSteps", ge=18, le=30)
    max_solution_steps: int = Field(alias="maxSolutionSteps", ge=32, le=50)
    min_water_areas: int = Field(alias="minWaterAreas", ge=0, le=2)
    max_water_areas: int = Field(alias="maxWaterAreas", ge=0, le=2)
    min_wall_obstacle_blocks: int = Field(alias="minWallObstacleBlocks", ge=0, le=2)
    max_wall_obstacle_blocks: int = Field(alias="maxWallObstacleBlocks", ge=0, le=3)
    min_pushes: int = Field(alias="minPushes", ge=8, le=16)
    max_pushes: int = Field(alias="maxPushes", ge=14, le=28)
    min_reverse_pulls: int = Field(alias="minReversePulls", ge=14, le=24)
    max_reverse_pulls: int = Field(alias="maxReversePulls", ge=24, le=40)
    style: str = Field(min_length=1, max_length=80)
    archetype: BlueprintArchetype
    target_layout: TargetLayout = Field(alias="targetLayout")
    obstacle_style: ObstacleStyle = Field(alias="obstacleStyle")
    water_style: WaterStyle = Field(alias="waterStyle")
    design_note: str = Field(alias="designNote", min_length=1, max_length=160)
    corridor_placement: CorridorPlacement = Field(alias="corridorPlacement")
    corridor_width: int = Field(alias="corridorWidth", ge=0, le=2)
    corridor_orientation: CorridorOrientation = Field(alias="corridorOrientation")
    corridor_role: CorridorRole = Field(alias="corridorRole")
    corridor_priority: CorridorPriority = Field(alias="corridorPriority")

    @model_validator(mode="after")
    def _validate_consistency(self) -> LevelPlanPayload:
        pairs = (
            ("min_solution_steps", "max_solution_steps", "minSolutionSteps", "maxSolutionSteps"),
            ("min_pushes", "max_pushes", "minPushes", "maxPushes"),
            ("min_water_areas", "max_water_areas", "minWaterAreas", "maxWaterAreas"),
            (
                "min_wall_obstacle_blocks",
                "max_wall_obstacle_blocks",
                "minWallObstacleBlocks",
                "maxWallObstacleBlocks",
            ),
            ("min_reverse_pulls", "max_reverse_pulls", "minReversePulls", "maxReversePulls"),
        )
        for lo_attr, hi_attr, lo_key, hi_key in pairs:
            lo, hi = getattr(self, lo_attr), getattr(self, hi_attr)
            min_lo, max_lo = BLUEPRINT_LIMITS[lo_key]
            min_hi, max_hi = BLUEPRINT_LIMITS[hi_key]
            if not (min_lo <= lo <= max_lo) or not (min_hi <= hi <= max_hi):
                raise ValueError(f"{lo_key}/{hi_key} out of generator range.")
            if hi < lo:
                raise ValueError(f"{hi_key} must be >= {lo_key}.")
        if self.corridor_placement == CorridorPlacement.NONE.value:
            if self.corridor_width != 0:
                raise ValueError("corridorWidth must be 0 when corridorPlacement is none.")
            if self.corridor_priority == CorridorPriority.REQUIRED.value:
                raise ValueError("corridorPriority cannot be required when corridorPlacement=none.")
            if self.corridor_role == CorridorRole.REQUIRED_BOX_ROUTE.value:
                raise ValueError("corridorRole cannot be required_box_route without a corridor.")
        elif self.corridor_width not in {1, 2}:
            raise ValueError("corridorWidth must be 1 or 2 when corridorPlacement is center/side.")
        return self
