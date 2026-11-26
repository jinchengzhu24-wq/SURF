"""Pydantic v2 base contracts and task schemas for stage, review, DG guide, and level plan."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

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

    solution_summary: str = Field(alias="solutionSummary", min_length=1, max_length=400)
    difficulty_opinion: str = Field(alias="difficultyOpinion", min_length=1, max_length=400)
    features: list[str] = Field(default_factory=list, max_length=6)
    suggestions: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> StageAssessmentPayload:
        if not self.solution_summary.strip():
            raise ValueError("assessment.solutionSummary must not be blank.")
        if not self.difficulty_opinion.strip():
            raise ValueError("assessment.difficultyOpinion must not be blank.")
        return self


class ManualEditDirectionComparisonPayload(StrictSchema):
    direction_id: str = Field(alias="directionId", min_length=1, max_length=64)
    relation: Literal["conflict", "aligned", "tradeoff", "unrelated", "unclear"]
    explanation: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> ManualEditDirectionComparisonPayload:
        if not self.direction_id.strip():
            raise ValueError("directionId must not be blank.")
        if not self.explanation.strip():
            raise ValueError("explanation must not be blank.")
        return self


class ManualEditConflictClassificationPayload(StrictSchema):
    comparisons: list[ManualEditDirectionComparisonPayload] = Field(min_length=1, max_length=24)


def build_manual_edit_conflict_classification_model(
    expected_ids: Sequence[str],
) -> type[StrictSchema]:
    """Build a classification schema constrained to the exact expected directionIds."""
    unique_ids = tuple(dict.fromkeys(str(item) for item in expected_ids if str(item).strip()))
    if not unique_ids:
        return ManualEditConflictClassificationPayload
    direction_literal = Literal[unique_ids]  # type: ignore[valid-type]
    item_model = create_model(
        "ConstrainedManualEditDirectionComparisonPayload",
        __base__=ManualEditDirectionComparisonPayload,
        direction_id=(direction_literal, Field(alias="directionId")),
    )
    count = len(unique_ids)
    return create_model(
        "ConstrainedManualEditConflictClassificationPayload",
        __base__=StrictSchema,
        comparisons=(list[item_model], Field(min_length=count, max_length=count)),  # type: ignore[valid-type]
    )


class ManualEditConflictCardPayload(StrictSchema):
    user_position: str = Field(alias="userPosition", min_length=1, max_length=400)
    ai_position: str = Field(alias="aiPosition", min_length=1, max_length=400)
    core_disagreement: str = Field(alias="coreDisagreement", min_length=1, max_length=800)
    next_question: str = Field(alias="nextQuestion", min_length=1, max_length=300)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> ManualEditConflictCardPayload:
        for attr, alias in (
            ("user_position", "conflict.userPosition"),
            ("ai_position", "conflict.aiPosition"),
            ("core_disagreement", "conflict.coreDisagreement"),
            ("next_question", "conflict.nextQuestion"),
        ):
            if not getattr(self, attr).strip():
                raise ValueError(f"{alias} must not be blank.")
        return self


class ManualEditReviewNoConflictPayload(StrictSchema):
    """Manual-edit review payload when frozenConflictVerdict != 'conflict' (no conflict card)."""

    opening_message: str = Field(alias="openingMessage", min_length=1, max_length=4000)
    assessment: StageAssessmentPayload
    review_message: str = Field(alias="reviewMessage", min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> ManualEditReviewNoConflictPayload:
        if not self.opening_message.strip():
            raise ValueError("openingMessage must not be blank.")
        if not self.review_message.strip():
            raise ValueError("reviewMessage must not be blank.")
        return self


class ManualEditReviewWithConflictPayload(ManualEditReviewNoConflictPayload):
    """Manual-edit review payload when frozenConflictVerdict == 'conflict' (conflict required)."""

    conflict: ManualEditConflictCardPayload


class ManualEditReviewPayload(ManualEditReviewNoConflictPayload):
    """Backward-compatible manual-edit review payload with optional conflict card."""

    conflict: ManualEditConflictCardPayload | None = None


class ChallengeReasonReviewPayload(StrictSchema):
    relation: Literal["primary", "secondary", "different", "unclear"]
    merit: Literal["reasonable", "not_yet_reasonable", "unclear"]
    comparison: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> ChallengeReasonReviewPayload:
        if not self.comparison.strip():
            raise ValueError("comparison must not be blank.")
        return self


class DGGuideSummaryPayload(StrictSchema):
    summary: str = Field(min_length=1, max_length=480)
    recommended_difficulty: DifficultyLevel = Field(alias="recommendedDifficulty")
    difficulty_rationale: str = Field(alias="difficultyRationale", min_length=1, max_length=320)
    recommended_layout: LayoutStyle = Field(alias="recommendedLayout")
    layout_rationale: str = Field(alias="layoutRationale", min_length=1, max_length=320)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> DGGuideSummaryPayload:
        for attr, alias in (
            ("summary", "summary"),
            ("difficulty_rationale", "difficultyRationale"),
            ("layout_rationale", "layoutRationale"),
        ):
            if not getattr(self, attr).strip():
                raise ValueError(f"{alias} must not be blank.")
        return self


def build_constrained_dg_guide_model(
    *,
    allowed_difficulties: Sequence[str],
    allowed_layouts: Sequence[str],
    language: str = "en",
) -> type[DGGuideSummaryPayload]:
    """Build a DG guide schema narrowed to exact allowed difficulty/layout values and locale."""
    diff_vals = tuple(dict.fromkeys(str(v) for v in allowed_difficulties if str(v).strip()))
    layout_vals = tuple(dict.fromkeys(str(v) for v in allowed_layouts if str(v).strip()))
    if not diff_vals:
        diff_vals = tuple(level.value for level in DifficultyLevel)
    if not layout_vals:
        layout_vals = tuple(style.value for style in LayoutStyle)
    is_zh = str(language or "").strip().lower().startswith("zh")
    min_rationale_len = 12 if is_zh else 30
    diff_literal = Literal[diff_vals]  # type: ignore[valid-type]
    layout_literal = Literal[layout_vals]  # type: ignore[valid-type]
    return create_model(
        "ConstrainedDGGuideSummaryPayload",
        __base__=DGGuideSummaryPayload,
        recommended_difficulty=(diff_literal, Field(alias="recommendedDifficulty")),
        difficulty_rationale=(
            str,
            Field(alias="difficultyRationale", min_length=min_rationale_len, max_length=320),
        ),
        recommended_layout=(layout_literal, Field(alias="recommendedLayout")),
        layout_rationale=(
            str,
            Field(alias="layoutRationale", min_length=min_rationale_len, max_length=320),
        ),
    )


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


def build_constrained_level_plan_model(
    *,
    no_water: bool = False,
    no_internal_walls: bool = False,
    generation_preferences: Mapping[str, Any] | None = None,
) -> type[LevelPlanPayload]:
    """Build a LevelPlanPayload model locking water, wall, corridor, and preference bounds."""
    prefs = dict(generation_preferences or {})
    overrides: dict[str, Any] = {}

    if no_water:
        min_w_lo = min_w_hi = max_w_lo = max_w_hi = 0
    elif "minWaterAreas" in prefs and "maxWaterAreas" in prefs:
        min_w_lo = min_w_hi = int(prefs["minWaterAreas"])
        max_w_lo = max_w_hi = int(prefs["maxWaterAreas"])
    else:
        min_w_lo, min_w_hi = 1, 2
        max_w_lo, max_w_hi = 1, 2
    overrides["min_water_areas"] = (
        int,
        Field(alias="minWaterAreas", ge=min_w_lo, le=min_w_hi),
    )
    overrides["max_water_areas"] = (
        int,
        Field(alias="maxWaterAreas", ge=max_w_lo, le=max_w_hi),
    )

    if no_internal_walls:
        min_wall_lo = min_wall_hi = max_wall_lo = max_wall_hi = 0
    elif "minWallObstacleBlocks" in prefs and "maxWallObstacleBlocks" in prefs:
        min_wall_lo = min_wall_hi = int(prefs["minWallObstacleBlocks"])
        max_wall_lo = max_wall_hi = int(prefs["maxWallObstacleBlocks"])
    else:
        min_wall_lo = min_wall_hi = 2
        max_wall_lo, max_wall_hi = 2, 3
    overrides["min_wall_obstacle_blocks"] = (
        int,
        Field(alias="minWallObstacleBlocks", ge=min_wall_lo, le=min_wall_hi),
    )
    overrides["max_wall_obstacle_blocks"] = (
        int,
        Field(alias="maxWallObstacleBlocks", ge=max_wall_lo, le=max_wall_hi),
    )

    numeric_pref_fields = (
        ("min_solution_steps", "minSolutionSteps"),
        ("max_solution_steps", "maxSolutionSteps"),
        ("min_pushes", "minPushes"),
        ("max_pushes", "maxPushes"),
        ("min_reverse_pulls", "minReversePulls"),
        ("max_reverse_pulls", "maxReversePulls"),
    )
    for attr, alias in numeric_pref_fields:
        if alias in prefs:
            val = int(prefs[alias])
            overrides[attr] = (int, Field(alias=alias, ge=val, le=val))

    enum_pref_fields = (
        ("archetype", "archetype"),
        ("target_layout", "targetLayout"),
        ("obstacle_style", "obstacleStyle"),
        ("water_style", "waterStyle"),
    )
    for attr, alias in enum_pref_fields:
        if alias in prefs and str(prefs[alias]).strip():
            enum_lit = Literal[(str(prefs[alias]),)]  # type: ignore[valid-type]
            overrides[attr] = (enum_lit, Field(alias=alias))

    force_no_corridor = (
        no_internal_walls
        or prefs.get("maxWallObstacleBlocks") == 0
        or prefs.get("corridorPlacement") == "none"
    )
    if force_no_corridor:
        pref_orientation = str(prefs.get("corridorOrientation") or "").strip()
        orientation_val = (
            pref_orientation
            if pref_orientation in {e.value for e in CorridorOrientation}
            else CorridorOrientation.ANY.value
        )
        pref_role = str(prefs.get("corridorRole") or "").strip()
        role_val = (
            pref_role
            if pref_role in {CorridorRole.VISUAL_ONLY.value, CorridorRole.PLAYER_ROUTE.value}
            else CorridorRole.VISUAL_ONLY.value
        )
        pref_priority = str(prefs.get("corridorPriority") or "").strip()
        priority_val = (
            pref_priority
            if pref_priority == CorridorPriority.PREFERRED.value
            else CorridorPriority.PREFERRED.value
        )
        overrides["corridor_placement"] = (
            Literal["none"],
            Field(alias="corridorPlacement"),
        )
        overrides["corridor_width"] = (int, Field(alias="corridorWidth", ge=0, le=0))
        overrides["corridor_orientation"] = (
            Literal[(orientation_val,)],  # type: ignore[valid-type]
            Field(alias="corridorOrientation"),
        )
        overrides["corridor_role"] = (
            Literal[(role_val,)],  # type: ignore[valid-type]
            Field(alias="corridorRole"),
        )
        overrides["corridor_priority"] = (
            Literal[(priority_val,)],  # type: ignore[valid-type]
            Field(alias="corridorPriority"),
        )
    elif prefs.get("corridorPlacement") in {"center", "side"}:
        cp_lit = Literal[(str(prefs["corridorPlacement"]),)]  # type: ignore[valid-type]
        overrides["corridor_placement"] = (cp_lit, Field(alias="corridorPlacement"))
        if "corridorWidth" in prefs:
            cw = int(prefs["corridorWidth"])
            overrides["corridor_width"] = (int, Field(alias="corridorWidth", ge=cw, le=cw))
        else:
            overrides["corridor_width"] = (int, Field(alias="corridorWidth", ge=1, le=2))
        for attr, alias in (
            ("corridor_orientation", "corridorOrientation"),
            ("corridor_role", "corridorRole"),
            ("corridor_priority", "corridorPriority"),
        ):
            if alias in prefs and str(prefs[alias]).strip():
                lit = Literal[(str(prefs[alias]),)]  # type: ignore[valid-type]
                overrides[attr] = (lit, Field(alias=alias))

    return create_model(
        "ConstrainedLevelPlanPayload",
        __base__=LevelPlanPayload,
        **overrides,
    )


_PROPOSAL_SCHEMA_EXPORTS = frozenset(
    {
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
        "EntityId",
        "ExactTransitionPayload",
        "FocusAreaPayload",
        "MapOperationCandidatePayload",
        "MapOperationItemPayload",
        "MapOperationsPayload",
        "MetricGoalPayload",
        "ProposalFailureAnalysisPayload",
        "ReduceObstaclesPromisePayload",
        "RequiredTransitionPayload",
        "RequirementItemPayload",
        "RequirementRecordPayload",
        "ReshapeWaterPromisePayload",
        "RevisionPlanPayload",
        "RevisionStrategyPayload",
        "ShorterSolutionPromisePayload",
        "WidenCorridorPromisePayload",
        "select_binary_discovery_schema",
    }
)


def __getattr__(name: str) -> Any:
    """Lazily re-export proposal/binary schemas from ._schemas_proposals without cycles."""
    if name in _PROPOSAL_SCHEMA_EXPORTS:
        from . import _schemas_proposals

        return getattr(_schemas_proposals, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
