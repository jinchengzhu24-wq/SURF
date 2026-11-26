"""Zero-feature constraints, generation preferences, and blueprint validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from server.domain.errors import ValidationError

from .models import (
    BLUEPRINT_ENUMS,
    BLUEPRINT_LIMITS,
    GenerationPreferences,
    LevelDesignPlan,
    _clean_text,
)

ZERO_WATER_PHRASES: tuple[str, ...] = (
    "no water",
    "no-water",
    "without water",
    "without any water",
    "water-free",
    "water free",
    "zero water",
    "remove water",
    "remove the water",
    "don't use water",
    "do not use water",
    "不要水",
    "不要有水",
    "不要任何水",
    "不需要水",
    "不用水",
    "不使用水",
    "不放水",
    "没有水",
    "无水",
    "零水",
    "去掉水",
    "移除水",
    "删除水",
)

ZERO_INTERNAL_WALL_PHRASES: tuple[str, ...] = (
    "no internal wall",
    "no-internal-wall",
    "without internal wall",
    "without any internal wall",
    "zero internal wall",
    "no interior wall",
    "without interior wall",
    "no wall obstacle",
    "without wall obstacle",
    "without water or internal wall",
    "without any water or any internal wall",
    "no water or internal wall",
    "no water and no internal wall",
    "remove internal wall",
    "remove interior wall",
    "remove wall obstacle",
    "don't use internal wall",
    "do not use internal wall",
    "不要内部墙",
    "不要有内部墙",
    "不要任何内部墙",
    "不需要内部墙",
    "不使用内部墙",
    "不放内部墙",
    "没有内部墙",
    "无内部墙",
    "零内部墙",
    "不要墙障碍",
    "无墙障碍",
    "零墙障碍",
    "无水域和内部墙",
    "无水和内部墙",
    "去掉内部墙",
    "移除内部墙",
    "删除内部墙",
)

WATER_FEATURE_TERMS: tuple[str, ...] = (
    "water",
    "river",
    "pool",
    "lake",
    "pond",
    "水",
    "河",
    "湖",
    "池",
)

INTERNAL_WALL_FEATURE_TERMS: tuple[str, ...] = (
    "wall",
    "internal wall",
    "interior wall",
    "wall obstacle",
    "墙",
    "墙体",
    "内部墙",
    "墙障碍",
)


def _constraint_snippets(creative_context: Mapping[str, Any] | None) -> list[str]:
    ctx = creative_context or {}
    snippets: list[str] = []
    for key in ("latestAdjustmentText", "styleDescription"):
        text = _clean_text(ctx.get(key))
        if text:
            snippets.append(text)
    raw_history = ctx.get("adjustmentHistoryText")
    if raw_history is not None:
        entries = [_clean_text(line) for line in str(raw_history).splitlines() if _clean_text(line)]
        snippets.extend(reversed(entries))
    original = _clean_text(ctx.get("originalIdeaText")) or _clean_text(ctx.get("ideaText"))
    if original:
        snippets.append(original)
    return snippets


def _resolve_zero_flag(
    snippets: Sequence[str],
    zero_phrases: tuple[str, ...],
    feature_terms: tuple[str, ...],
) -> bool:
    for snippet in snippets:
        normalized = _clean_text(snippet).casefold()
        if any(phrase in normalized for phrase in zero_phrases):
            return True
        if any(term in normalized for term in feature_terms):
            return False
    return False


def resolve_zero_feature_constraints(
    creative_context: Mapping[str, Any] | None = None,
) -> dict[str, bool]:
    """Determine explicit zero-water / zero-internal-wall overrides from prompt context."""
    snippets = _constraint_snippets(creative_context)
    return {
        "noWater": _resolve_zero_flag(snippets, ZERO_WATER_PHRASES, WATER_FEATURE_TERMS),
        "noInternalWalls": _resolve_zero_flag(
            snippets, ZERO_INTERNAL_WALL_PHRASES, INTERNAL_WALL_FEATURE_TERMS
        ),
    }


def normalize_generation_preferences(
    raw_preferences: GenerationPreferences | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize and validate optional generation preferences."""
    if raw_preferences is None:
        return {}
    data = (
        raw_preferences.model_dump(by_alias=True)
        if isinstance(raw_preferences, BaseModel)
        else dict(raw_preferences)
    )
    normalized: dict[str, Any] = {}

    for key, (minimum, maximum) in BLUEPRINT_LIMITS.items():
        value = data.get(key)
        if value is None or value == -1:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(
                "INVALID_GENERATION_PREFERENCE",
                message=f"generationPreferences.{key} must be an integer",
            )
        if value < minimum or value > maximum:
            raise ValidationError(
                "INVALID_GENERATION_PREFERENCE",
                message=f"generationPreferences.{key}={value} is outside {minimum}-{maximum}",
            )
        normalized[key] = value

    for min_key, max_key in (
        ("minSolutionSteps", "maxSolutionSteps"),
        ("minPushes", "maxPushes"),
        ("minWaterAreas", "maxWaterAreas"),
        ("minWallObstacleBlocks", "maxWallObstacleBlocks"),
        ("minReversePulls", "maxReversePulls"),
    ):
        if (min_key in normalized) != (max_key in normalized):
            raise ValidationError(
                "INVALID_GENERATION_PREFERENCE",
                message=f"generationPreferences must provide both {min_key} and {max_key}",
            )
        if min_key in normalized and normalized[max_key] < normalized[min_key]:
            raise ValidationError(
                "INVALID_GENERATION_PREFERENCE",
                message=f"generationPreferences.{max_key} must be >= {min_key}",
            )

    for key, allowed in BLUEPRINT_ENUMS.items():
        value = str(data.get(key) or "").strip()
        if not value:
            continue
        if value not in allowed:
            raise ValidationError(
                "INVALID_GENERATION_PREFERENCE",
                message=f"generationPreferences.{key}={value} is not supported",
            )
        normalized[key] = value

    corridor_width = data.get("corridorWidth")
    if corridor_width not in (None, -1):
        if isinstance(corridor_width, bool) or not isinstance(corridor_width, int):
            raise ValidationError(
                "INVALID_GENERATION_PREFERENCE",
                message="generationPreferences.corridorWidth must be an integer",
            )
        normalized["corridorWidth"] = corridor_width

    corridor_placement = normalized.get("corridorPlacement")
    if corridor_placement:
        if corridor_placement == "none":
            if normalized.get("corridorWidth", 0) != 0:
                raise ValidationError(
                    "INVALID_GENERATION_PREFERENCE",
                    message=(
                        "generationPreferences.corridorWidth must be 0 "
                        "when corridorPlacement is none"
                    ),
                )
            if normalized.get("corridorPriority") == "required":
                raise ValidationError(
                    "INVALID_GENERATION_PREFERENCE",
                    message=(
                        "generationPreferences.corridorPriority cannot be required "
                        "when corridorPlacement is none"
                    ),
                )
            if normalized.get("corridorRole") == "required_box_route":
                raise ValidationError(
                    "INVALID_GENERATION_PREFERENCE",
                    message=(
                        "generationPreferences.corridorRole cannot be required_box_route "
                        "when corridorPlacement is none"
                    ),
                )
            normalized["corridorWidth"] = 0
            normalized.setdefault("corridorOrientation", "any")
            normalized.setdefault("corridorRole", "visual_only")
            normalized.setdefault("corridorPriority", "preferred")
        else:
            if normalized.get("corridorWidth") not in {1, 2}:
                raise ValidationError(
                    "INVALID_GENERATION_PREFERENCE",
                    message=(
                        "generationPreferences.corridorWidth must be 1 or 2 "
                        "when a corridor is requested"
                    ),
                )
            normalized.setdefault("corridorOrientation", "any")
            normalized.setdefault("corridorRole", "player_route")
            normalized.setdefault("corridorPriority", "preferred")
    elif "corridorWidth" in normalized:
        raise ValidationError(
            "INVALID_GENERATION_PREFERENCE",
            message=(
                "generationPreferences.corridorPlacement is required "
                "when corridorWidth is provided"
            ),
        )

    if (
        normalized.get("maxWallObstacleBlocks") == 0
        and normalized.get("corridorPlacement") not in (None, "none")
    ):
        raise ValidationError(
            "INVALID_GENERATION_PREFERENCE",
            message=(
                "generationPreferences cannot request a corridor "
                "with zero internal wall obstacles"
            ),
        )

    return normalized


def validate_level_plan(
    plan: LevelDesignPlan | Mapping[str, Any] | None,
    feature_constraints: Mapping[str, Any] | None = None,
    generation_preferences: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and normalize a blueprint dictionary against constraints and preferences."""
    if plan is None or not isinstance(plan, (dict, LevelDesignPlan)):
        raise ValueError("model returned no parsed plan")

    data = plan.to_wire_dict() if isinstance(plan, LevelDesignPlan) else dict(plan)
    constraints = feature_constraints or {}
    prefs = generation_preferences or {}
    no_water = bool(constraints.get("noWater"))
    no_internal_walls = bool(constraints.get("noInternalWalls"))
    has_custom_water = "minWaterAreas" in prefs
    has_custom_walls = "minWallObstacleBlocks" in prefs

    if no_water:
        data["minWaterAreas"] = 0
        data["maxWaterAreas"] = 0

    if no_internal_walls:
        data["minWallObstacleBlocks"] = 0
        data["maxWallObstacleBlocks"] = 0
        data["corridorPlacement"] = "none"
        data["corridorWidth"] = 0
        data["corridorOrientation"] = "any"
        data["corridorRole"] = "visual_only"
        data["corridorPriority"] = "preferred"

    validated = LevelDesignPlan.model_validate(data).to_wire_dict()

    if (
        not no_water
        and not has_custom_water
        and (validated["minWaterAreas"] < 1 or validated["maxWaterAreas"] < 1)
    ):
        raise ValueError(
            "minWaterAreas and maxWaterAreas must be 1 or 2 unless no water or custom "
            "generationPreferences are requested "
            f"(got minWaterAreas={validated['minWaterAreas']}, "
            f"maxWaterAreas={validated['maxWaterAreas']})."
        )

    if (
        not no_internal_walls
        and not has_custom_walls
        and (validated["minWallObstacleBlocks"] != 2 or validated["maxWallObstacleBlocks"] < 2)
    ):
        raise ValueError(
            "minWallObstacleBlocks must be 2 and maxWallObstacleBlocks must be 2 or 3 "
            "unless no internal walls or custom generationPreferences are requested "
            f"(got minWallObstacleBlocks={validated['minWallObstacleBlocks']}, "
            f"maxWallObstacleBlocks={validated['maxWallObstacleBlocks']})."
        )

    return validated
