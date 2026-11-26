"""Prompt builder for 8000 Level Blueprint Plan generation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from server.domain.blueprints import resolve_zero_feature_constraints

_LEVEL_PLAN_SYSTEM = (
    "You are a classic Sokoban level design director producing a high-level blueprint for an "
    "algorithmic 12x10, 2-box, 2-target Sokoban generator. Return ONLY JSON matching "
    "LevelPlanPayload with every string value in concise English ASCII.\n"
    "Generator capabilities: static 12x10 grid with 1 player, 2 boxes, 2 targets, 0-2 rectangular "
    "water areas, 0 or 2-3 internal wall obstacle blocks, and at most one axis-aligned corridor "
    "(width 1-2 at center/side, or placement='none' with width=0, orientation='any', "
    "role='visual_only', priority='preferred'). No dynamic mechanics, ice, switches, or custom "
    "box types—adapt any unsupported idea into static spatial/push-order pressure.\n"
    "Inclusive ranges: minSolutionSteps 18-30, maxSolutionSteps 32-50, minPushes 8-16, "
    "maxPushes 14-28, minReversePulls 14-24, maxReversePulls 24-40 (each max >= min).\n"
    "Enums: archetype in (goal_room, bottleneck_corridor, split_route, open_workshop); "
    "targetLayout in (clustered, split_pair, edge_cluster); obstacleStyle in (central_baffle, "
    "side_choke, goal_guard); waterStyle in (corner_pool, side_pool, route_divider); "
    "corridorPlacement in (none, center, side); corridorOrientation in (horizontal, vertical, "
    "any); corridorRole in (visual_only, player_route, required_box_route); corridorPriority in "
    "(preferred, required).\n"
    "Constraints: honor featureConstraints strictly (noWater -> min/maxWaterAreas=0, otherwise "
    "1..2; noInternalWalls -> min/maxWallObstacleBlocks=0 and corridorPlacement='none', otherwise "
    "minWallObstacleBlocks=2 and maxWallObstacleBlocks in 2..3). Copy any explicitly supplied "
    "generationPreferences verbatim; they override defaults and recentBlueprintHint."
)


def build_level_plan_messages(
    *,
    variation_seed: int | str,
    recent_blueprint_hint: str = "",
    creative_context: Mapping[str, Any] | None = None,
    feature_constraints: Mapping[str, bool] | None = None,
    generation_preferences: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build level-plan messages with all user text cleanly enclosed inside a JSON payload."""
    ctx = dict(creative_context or {})
    constraints = (
        dict(feature_constraints)
        if feature_constraints is not None
        else resolve_zero_feature_constraints(ctx)
    )
    dg_raw = ctx.get("dgContext")
    safe_dg = (
        {
            "aiSummary": str(dg_raw.get("aiSummary") or "")[:320],
            "aiRecommendedDifficulty": str(dg_raw.get("aiRecommendedDifficulty") or "")[:16],
            "aiRecommendedLayout": str(dg_raw.get("aiRecommendedLayout") or "")[:16],
            "finalDifficulty": str(dg_raw.get("finalDifficulty") or "")[:16],
            "finalLayout": str(dg_raw.get("finalLayout") or "")[:16],
        }
        if isinstance(dg_raw, Mapping) and dg_raw
        else None
    )
    payload = {
        "variationSeed": variation_seed,
        "recentBlueprintHint": str(recent_blueprint_hint or ""),
        "featureConstraints": constraints,
        "generationPreferences": dict(generation_preferences or {}),
        "dgContext": safe_dg,
        "userInputs": {
            "latestAdjustmentText": str(ctx.get("latestAdjustmentText") or "")[:600],
            "styleDescription": str(ctx.get("styleDescription") or "")[:600],
            "originalIdeaText": str(
                ctx.get("originalIdeaText") or ctx.get("ideaText") or ""
            )[:600],
            "adjustmentHistoryText": str(ctx.get("adjustmentHistoryText") or "")[:800],
        },
    }
    return [
        {"role": "system", "content": _LEVEL_PLAN_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]
