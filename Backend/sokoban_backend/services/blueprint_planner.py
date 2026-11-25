import json
import threading
from typing import Any

from ..config import BackendSettings, Clock
from ..llm.prompts import (
    build_creative_idea_expansion_messages,
    build_ha_revision_plan_edit_messages,
    build_ha_revision_plan_messages,
    build_human_adjustment_clarity_messages,
    build_level_plan_messages,
    normalize_prompt_text,
    resolve_zero_feature_constraints,
)
from ..llm.runtime import EventLogger, LLMExecutionResult, LLMGateway, LLMServiceError, new_request_id
from ..schemas import (
    CreativeIdeaExpansionOption,
    DEFAULT_PLAN,
    ENUMS,
    HA_CHANGE_FIELDS,
    LIMITS,
    LevelDesignPlan,
    RECENT_BLUEPRINT_LIMIT,
)


clean_expansion_text = normalize_prompt_text


def validate_human_adjustment_clarity_payload(payload):
    payload = payload if isinstance(payload, dict) else {}

    def score(name):
        try:
            return max(0, min(2, int(payload.get(name, 0))))
        except (TypeError, ValueError):
            return 0

    problem_score = score("problemScore")
    target_score = score("targetScore")
    direction_score = score("directionScore")
    detail_score = score("detailScore")
    total_score = problem_score + target_score + direction_score + detail_score
    is_clear = total_score >= 4 and target_score >= 1 and direction_score >= 1
    reason = str(payload.get("reason") or "").strip()

    if not reason:
        reason = (
            "State which level feature should change and the direction of that change."
            if not is_clear
            else "The instruction contains an actionable user-directed revision."
        )

    return {
        "problemScore": problem_score,
        "targetScore": target_score,
        "directionScore": direction_score,
        "detailScore": detail_score,
        "totalScore": total_score,
        "isClear": is_clear,
        "reason": reason,
    }


def parse_ha_revision_contract(value):
    if isinstance(value, str):
        contract = json.loads(value)
    elif isinstance(value, dict):
        contract = dict(value)
    else:
        raise ValueError("HA promptText must contain a JSON object")

    if not isinstance(contract, dict):
        raise ValueError("HA promptText must contain a JSON object")

    if contract.get("preserveUnlisted") is not True:
        raise ValueError("HA contract preserveUnlisted must be true")

    changes = contract.get("changes")

    if not isinstance(changes, dict) or not changes:
        raise ValueError("HA contract changes must be a non-empty object")

    unknown_fields = set(changes) - HA_CHANGE_FIELDS

    if unknown_fields:
        raise ValueError(
            "HA contract contains unsupported fields: "
            + ", ".join(sorted(unknown_fields))
        )

    normalized_changes = {}

    for field, field_value in changes.items():
        if field in LIMITS:
            if isinstance(field_value, bool) or not isinstance(field_value, int):
                raise ValueError(f"HA contract {field} must be an integer")

            if field == "minWallObstacleBlocks" and field_value == 3:
                field_value = 2

            minimum, maximum = LIMITS[field]

            if field_value < minimum or field_value > maximum:
                raise ValueError(
                    f"HA contract {field}={field_value} is outside {minimum}-{maximum}"
                )

            normalized_changes[field] = field_value
        elif field == "corridorWidth":
            if (
                isinstance(field_value, bool)
                or not isinstance(field_value, int)
                or field_value not in {0, 1, 2}
            ):
                raise ValueError("HA contract corridorWidth must be 0, 1, or 2")

            normalized_changes[field] = field_value
        elif field in ENUMS:
            clean_value = str(field_value or "").strip()

            if clean_value not in ENUMS[field]:
                raise ValueError(
                    f"HA contract {field}={clean_value} is not supported"
                )

            normalized_changes[field] = clean_value
        elif field == "style":
            clean_value = str(field_value or "").strip()

            if not clean_value:
                raise ValueError("HA contract style cannot be empty")

            normalized_changes[field] = clean_value[:80]

    return {
        "changes": normalized_changes,
        "preserveUnlisted": True,
    }


def build_ha_contract_constraints(previous_plan, changes, adjustment_text):
    constraints = resolve_zero_feature_constraints(
        {"latestAdjustmentText": str(adjustment_text or "")}
    )
    previous_plan = previous_plan or {}
    water_fields = {"minWaterAreas", "maxWaterAreas"}
    wall_fields = {"minWallObstacleBlocks", "maxWallObstacleBlocks"}

    if not water_fields.intersection(changes):
        constraints["noWater"] = (
            previous_plan.get("minWaterAreas") == 0
            and previous_plan.get("maxWaterAreas") == 0
        )

    if not wall_fields.intersection(changes):
        constraints["noInternalWalls"] = (
            previous_plan.get("minWallObstacleBlocks") == 0
            and previous_plan.get("maxWallObstacleBlocks") == 0
        )

    return constraints


def validate_ha_revision_contract(contract, previous_plan, adjustment_text):
    if not isinstance(previous_plan, dict) or not previous_plan:
        raise ValueError("previous LevelDesignPlan is required")

    parsed = parse_ha_revision_contract(contract)
    candidate_wall_min = parsed["changes"].get(
        "minWallObstacleBlocks",
        previous_plan.get("minWallObstacleBlocks"),
    )
    candidate_wall_max = parsed["changes"].get(
        "maxWallObstacleBlocks",
        previous_plan.get("maxWallObstacleBlocks"),
    )

    if candidate_wall_min == 0 and candidate_wall_max == 0:
        parsed["changes"].update(
            {
                "corridorPlacement": "none",
                "corridorWidth": 0,
                "corridorOrientation": "any",
                "corridorRole": "visual_only",
                "corridorPriority": "preferred",
            }
        )

    candidate = dict(previous_plan)
    candidate.update(parsed["changes"])
    constraints = build_ha_contract_constraints(
        previous_plan,
        parsed["changes"],
        adjustment_text,
    )
    validated_candidate = validate_plan(candidate, constraints)
    parsed["changes"] = {
        field: validated_candidate[field]
        for field in parsed["changes"]
    }
    return parsed


def validate_ha_revision_plan_options(payload, previous_plan, adjustment_text):
    if payload is None:
        raise ValueError("model returned no HA revision payload")

    data = payload.model_dump() if hasattr(payload, "model_dump") else payload
    if isinstance(data, list):
        raw_options = data
    elif isinstance(data, dict):
        raw_options = data.get("options")
    else:
        raise ValueError("HA revision payload must be a JSON object")

    if not isinstance(raw_options, list) or len(raw_options) != 3:
        raise ValueError("HA revision payload must contain exactly three options")

    validated = []
    canonical_contracts = set()

    for index, raw_option in enumerate(raw_options):
        if hasattr(raw_option, "model_dump"):
            option = raw_option.model_dump()
        elif isinstance(raw_option, dict):
            option = raw_option
        else:
            raise ValueError(f"HA option {index + 1} must be an object")
        option_id = clean_expansion_text(option.get("id")) or "ABC"[index]
        title = clean_expansion_text(option.get("title"))[:64]
        description = clean_expansion_text(option.get("description"))[:420]
        contract = validate_ha_revision_contract(
            option.get("promptText"),
            previous_plan,
            adjustment_text,
        )
        prompt_text = json.dumps(
            contract,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        if not title:
            raise ValueError(f"HA option {index + 1} title is required")

        if not description:
            raise ValueError(f"HA option {index + 1} description is required")

        if prompt_text in canonical_contracts:
            raise ValueError("HA revision options must use distinct change contracts")

        canonical_contracts.add(prompt_text)
        validated.append(
            {
                "id": option_id[:12],
                "title": title,
                "description": description,
                "promptText": prompt_text,
            }
        )

    return validated


def validate_ha_revision_plan_edit(
    payload,
    original_option,
    previous_plan,
    adjustment_text,
    edited_description,
):
    if payload is None:
        raise ValueError("model returned no HA revision-plan edit payload")

    data = payload.model_dump() if hasattr(payload, "model_dump") else payload
    if not isinstance(data, dict):
        raise ValueError("HA revision-plan edit payload must be a JSON object")
    original_option = dict(original_option or {})
    option_id = clean_expansion_text(original_option.get("id"))[:12]
    title = clean_expansion_text(original_option.get("title"))[:64]
    description = clean_expansion_text(data.get("description"))[:420]
    edit_intent = " ".join(
        value
        for value in (
            clean_expansion_text(adjustment_text),
            clean_expansion_text(edited_description),
        )
        if value
    )
    contract = validate_ha_revision_contract(
        data.get("promptText"),
        previous_plan,
        edit_intent,
    )
    original_contract = validate_ha_revision_contract(
        original_option.get("promptText"),
        previous_plan,
        adjustment_text,
    )

    if not option_id:
        raise ValueError("original HA option id is required")

    if not title:
        raise ValueError("original HA option title is required")

    if not description:
        raise ValueError("edited HA option description is required")

    if contract == original_contract:
        raise ValueError("edited HA option must produce a different change contract")

    prompt_text = json.dumps(
        contract,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "id": option_id,
        "title": title,
        "description": description,
        "promptText": prompt_text,
    }


def normalize_generation_preferences(raw_preferences):
    if raw_preferences is None:
        return {}

    data = (
        raw_preferences.model_dump()
        if hasattr(raw_preferences, "model_dump")
        else dict(raw_preferences)
    )
    normalized = {}

    for key, (minimum, maximum) in LIMITS.items():
        value = data.get(key)

        if value is None or value == -1:
            continue

        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"generationPreferences.{key} must be an integer")

        if value < minimum or value > maximum:
            raise ValueError(
                f"generationPreferences.{key}={value} is outside "
                f"{minimum}-{maximum}"
            )

        normalized[key] = value

    range_pairs = (
        ("minSolutionSteps", "maxSolutionSteps"),
        ("minPushes", "maxPushes"),
        ("minWaterAreas", "maxWaterAreas"),
        ("minWallObstacleBlocks", "maxWallObstacleBlocks"),
        ("minReversePulls", "maxReversePulls"),
    )

    for minimum_key, maximum_key in range_pairs:
        has_minimum = minimum_key in normalized
        has_maximum = maximum_key in normalized

        if has_minimum != has_maximum:
            raise ValueError(
                "generationPreferences must provide both "
                f"{minimum_key} and {maximum_key}"
            )

        if has_minimum and normalized[maximum_key] < normalized[minimum_key]:
            raise ValueError(
                f"generationPreferences.{maximum_key} must be >= "
                f"{minimum_key}"
            )

    for key, allowed_values in ENUMS.items():
        value = str(data.get(key) or "").strip()

        if not value:
            continue

        if value not in allowed_values:
            raise ValueError(
                f"generationPreferences.{key}={value} is not supported"
            )

        normalized[key] = value

    corridor_width = data.get("corridorWidth")

    if corridor_width not in (None, -1):
        if isinstance(corridor_width, bool) or not isinstance(
            corridor_width,
            int,
        ):
            raise ValueError(
                "generationPreferences.corridorWidth must be an integer"
            )

        normalized["corridorWidth"] = corridor_width

    corridor_placement = normalized.get("corridorPlacement")

    if corridor_placement:
        if corridor_placement == "none":
            if normalized.get("corridorWidth", 0) != 0:
                raise ValueError(
                    "generationPreferences.corridorWidth must be 0 when "
                    "corridorPlacement is none"
                )
            if normalized.get("corridorPriority") == "required":
                raise ValueError(
                    "generationPreferences.corridorPriority cannot be required when "
                    "corridorPlacement is none"
                )
            if normalized.get("corridorRole") == "required_box_route":
                raise ValueError(
                    "generationPreferences.corridorRole cannot be required_box_route when "
                    "corridorPlacement is none"
                )

            normalized["corridorWidth"] = 0
            normalized.setdefault("corridorOrientation", "any")
            normalized.setdefault("corridorRole", "visual_only")
            normalized.setdefault("corridorPriority", "preferred")
        else:
            if normalized.get("corridorWidth") not in {1, 2}:
                raise ValueError(
                    "generationPreferences.corridorWidth must be 1 or 2 "
                    "when a corridor is requested"
                )

            normalized.setdefault("corridorOrientation", "any")
            normalized.setdefault("corridorRole", "player_route")
            normalized.setdefault("corridorPriority", "preferred")
    elif "corridorWidth" in normalized:
        raise ValueError(
            "generationPreferences.corridorPlacement is required when "
            "corridorWidth is provided"
        )

    if (
        normalized.get("maxWallObstacleBlocks") == 0
        and normalized.get("corridorPlacement") not in (None, "none")
    ):
        raise ValueError(
            "generationPreferences cannot request a corridor with zero "
            "internal wall obstacles"
        )

    return normalized


def apply_generation_preferences(plan, generation_preferences):
    data = plan.model_dump() if isinstance(plan, LevelDesignPlan) else dict(plan)

    for key, value in (generation_preferences or {}).items():
        data[key] = value

    return data


def _parse_ha_client_inputs(creative_context):
    previous_value = creative_context.get("previousLevelPlan")
    if isinstance(previous_value, str):
        try:
            previous_plan = json.loads(previous_value)
        except json.JSONDecodeError as error:
            raise ValueError("previousLevelPlan must be valid JSON") from error
    else:
        previous_plan = previous_value

    selected_text = str(creative_context.get("selectedHAPlan") or "").strip()
    try:
        selected_option = json.loads(selected_text)
    except json.JSONDecodeError as error:
        raise ValueError("selectedHAPlan must be valid JSON") from error
    if not isinstance(selected_option, dict):
        raise ValueError("selectedHAPlan must be a JSON object")

    contract = validate_ha_revision_contract(
        selected_option.get("promptText"),
        previous_plan,
        creative_context.get("latestAdjustmentText"),
    )
    return previous_plan, selected_option, contract


def validate_client_level_plan_inputs(creative_context):
    creative_context = creative_context or {}
    if str(creative_context.get("revisionMode") or "").strip().lower() == "ha":
        selected_text = str(creative_context.get("selectedHAPlan") or "").strip()
        if selected_text:
            _parse_ha_client_inputs(creative_context)


def apply_selected_ha_plan(
    generated_plan,
    creative_context,
    feature_constraints,
    generation_preferences=None,
):
    creative_context = creative_context or {}

    if str(creative_context.get("revisionMode") or "").strip().lower() != "ha":
        candidate = apply_generation_preferences(
            generated_plan,
            generation_preferences,
        )
        effective_constraints = dict(feature_constraints or {})
        prefs = generation_preferences or {}

        if (
            prefs.get("minWaterAreas") == 0
            and prefs.get("maxWaterAreas") == 0
        ):
            effective_constraints["noWater"] = True

        if (
            prefs.get("minWallObstacleBlocks") == 0
            and prefs.get("maxWallObstacleBlocks") == 0
        ):
            effective_constraints["noInternalWalls"] = True

        return validate_plan(
            candidate,
            effective_constraints,
            generation_preferences,
        )

    selected_text = str(creative_context.get("selectedHAPlan") or "").strip()

    if not selected_text:
        candidate = apply_generation_preferences(
            generated_plan,
            generation_preferences,
        )
        return validate_plan(
            candidate,
            feature_constraints,
            generation_preferences,
        )

    previous_plan, selected_option, contract = _parse_ha_client_inputs(creative_context)
    candidate = dict(previous_plan)
    candidate.update(contract["changes"])
    title = clean_expansion_text(selected_option.get("title"))
    description = clean_expansion_text(selected_option.get("description"))
    candidate["designNote"] = (
        "Human-AI revision: "
        + (title or "selected plan")
        + ". "
        + description
    )[:160]
    candidate = apply_generation_preferences(
        candidate,
        generation_preferences,
    )
    effective_constraints = dict(feature_constraints or {})

    if (
        candidate.get("minWaterAreas") == 0
        and candidate.get("maxWaterAreas") == 0
    ):
        effective_constraints["noWater"] = True

    if (
        candidate.get("minWallObstacleBlocks") == 0
        and candidate.get("maxWallObstacleBlocks") == 0
    ):
        effective_constraints["noInternalWalls"] = True

    return validate_plan(
        candidate,
        effective_constraints,
        generation_preferences,
    )


def validate_plan(
    plan,
    feature_constraints=None,
    generation_preferences=None,
):
    if plan is None or not isinstance(plan, (dict, LevelDesignPlan)):
        raise ValueError("model returned no parsed plan")

    data = plan.model_dump() if isinstance(plan, LevelDesignPlan) else dict(plan)
    feature_constraints = feature_constraints or {}
    generation_preferences = generation_preferences or {}
    no_water = bool(feature_constraints.get("noWater"))
    no_internal_walls = bool(feature_constraints.get("noInternalWalls"))
    has_custom_water = "minWaterAreas" in generation_preferences
    has_custom_walls = "minWallObstacleBlocks" in generation_preferences

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

    for key, (minimum, maximum) in LIMITS.items():
        value = data.get(key)

        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")

        if value < minimum or value > maximum:
            raise ValueError(f"{key}={value} is outside {minimum}-{maximum}")

    if not no_water and not has_custom_water and (
        data["minWaterAreas"] < 1 or data["maxWaterAreas"] < 1
    ):
        raise ValueError(
            "water areas can be zero only when the user explicitly requests no water"
        )

    if not no_internal_walls and not has_custom_walls and (
        data["minWallObstacleBlocks"] != 2
        or data["maxWallObstacleBlocks"] < 2
    ):
        raise ValueError(
            "internal wall obstacles can be zero only when the user explicitly "
            "requests no internal walls"
        )

    if data["maxSolutionSteps"] < data["minSolutionSteps"]:
        raise ValueError("maxSolutionSteps must be >= minSolutionSteps")

    if data["maxPushes"] < data["minPushes"]:
        raise ValueError("maxPushes must be >= minPushes")

    if data["maxWaterAreas"] < data["minWaterAreas"]:
        raise ValueError("maxWaterAreas must be >= minWaterAreas")

    if data["maxWallObstacleBlocks"] < data["minWallObstacleBlocks"]:
        raise ValueError("maxWallObstacleBlocks must be >= minWallObstacleBlocks")

    if data["maxReversePulls"] < data["minReversePulls"]:
        raise ValueError("maxReversePulls must be >= minReversePulls")

    for key, allowed_values in ENUMS.items():
        value = str(data.get(key, "")).strip()

        if value not in allowed_values:
            raise ValueError(f"{key}={value} is not supported")

        data[key] = value

    corridor_width = data.get("corridorWidth", 0)

    if isinstance(corridor_width, bool) or not isinstance(corridor_width, int):
        raise ValueError("corridorWidth must be an integer")

    if data["corridorPlacement"] == "none":
        if corridor_width != 0:
            raise ValueError("corridorWidth must be 0 when corridorPlacement is none")
    elif corridor_width not in {1, 2}:
        raise ValueError("corridorWidth must be 1 or 2 when a corridor is requested")

    if data["corridorPriority"] == "required" and data["corridorPlacement"] == "none":
        raise ValueError("required corridorPriority needs a corridor placement")

    if data["corridorRole"] == "required_box_route" and data["corridorPlacement"] == "none":
        raise ValueError("required_box_route needs a corridor placement")

    style = str(data.get("style", "")).strip()
    if not style:
        style = DEFAULT_PLAN["style"]

    design_note = str(data.get("designNote", "")).strip()
    if not design_note:
        design_note = DEFAULT_PLAN["designNote"]

    return {
        "minSolutionSteps": data["minSolutionSteps"],
        "maxSolutionSteps": data["maxSolutionSteps"],
        "minPushes": data["minPushes"],
        "maxPushes": data["maxPushes"],
        "minWaterAreas": data["minWaterAreas"],
        "maxWaterAreas": data["maxWaterAreas"],
        "minWallObstacleBlocks": data["minWallObstacleBlocks"],
        "maxWallObstacleBlocks": data["maxWallObstacleBlocks"],
        "minReversePulls": data["minReversePulls"],
        "maxReversePulls": data["maxReversePulls"],
        "style": style[:80],
        "archetype": data["archetype"],
        "targetLayout": data["targetLayout"],
        "obstacleStyle": data["obstacleStyle"],
        "waterStyle": data["waterStyle"],
        "designNote": design_note[:160],
        "corridorPlacement": data["corridorPlacement"],
        "corridorWidth": corridor_width,
        "corridorOrientation": data["corridorOrientation"],
        "corridorRole": data["corridorRole"],
        "corridorPriority": data["corridorPriority"],
    }


def validate_creative_idea_expansion(payload):
    if payload is None:
        raise ValueError("model returned no expansion payload")

    data = payload.model_dump() if hasattr(payload, "model_dump") else payload

    if isinstance(data, list):
        raw_options = data
    elif isinstance(data, dict):
        raw_options = data.get("options")
    else:
        raise ValueError("expansion payload must be a JSON object")

    if not isinstance(raw_options, list) or len(raw_options) < 3:
        raise ValueError("expansion payload must include at least three options")

    validated = []
    option_ids = ["A", "B", "C"]

    for index, raw_option in enumerate(raw_options[:3]):
        if hasattr(raw_option, "model_dump"):
            option = raw_option.model_dump()
        elif isinstance(raw_option, dict):
            option = raw_option
        else:
            raise ValueError(f"option {index + 1} must be an object")
        option_id = clean_expansion_text(option.get("id")) or option_ids[index]
        title = clean_expansion_text(option.get("title"))[:64]
        description = clean_expansion_text(option.get("description"))[:320]
        prompt_text = clean_expansion_text(option.get("promptText"))[:420]

        if not title:
            raise ValueError(f"option {index + 1} title is required")

        if not description:
            raise ValueError(f"option {index + 1} description is required")

        if not prompt_text:
            prompt_text = description

        validated.append(
            CreativeIdeaExpansionOption(
                id=option_id[:12],
                title=title,
                description=description,
                promptText=prompt_text,
            ).model_dump()
        )

    return validated


def build_contextual_expansion_fallback_options(idea_text):
    tags = classify_expansion_idea(idea_text)

    if "water" in tags:
        return build_water_fallback_options(compact="compact" in tags)

    if "maze" in tags:
        return build_maze_fallback_options()

    if "compact" in tags:
        return build_compact_fallback_options()

    return build_general_fallback_options()


def classify_expansion_idea(idea_text):
    normalized = clean_expansion_text(idea_text).lower()
    tags = set()

    if contains_any(normalized, ["water", "river", "pool", "lake", "pond", "obstacle", "水", "河", "池", "湖", "障碍"]):
        tags.add("water")

    if contains_any(normalized, ["maze", "detour", "corridor", "route", "path", "绕路", "迷宫", "通道", "路线", "走廊"]):
        tags.add("maze")

    if contains_any(normalized, ["hard", "difficult", "compact", "tight", "deadlock", "tricky", "难", "困难", "紧凑", "狭小", "卡死", "精确"]):
        tags.add("compact")

    return tags


def build_water_fallback_options(compact=False):
    if compact:
        return [
            {
                "id": "A",
                "title": "Compact Water Split",
                "description": "In a compact room, place water through the middle so the player must route around it and choose which box to handle first. The water cuts the direct path and compresses standing space.",
                "promptText": "Use the original idea with compact space, a central water split, limited standing positions, detour routing, and two-box order pressure.",
            },
            {
                "id": "B",
                "title": "Tight Waterside Goals",
                "description": "Put the goal area near water and keep the surrounding space tight. The player has to adjust boxes along a narrow waterside edge without trapping a box or themselves.",
                "promptText": "Use the original idea with compact waterside goals, edge-position pressure, and deadlock-aware box placement.",
            },
            {
                "id": "C",
                "title": "Twin Pool Squeeze",
                "description": "Use two small water areas in a tight room to bend the route and make the player switch attention between boxes. The challenge comes from water detours plus limited standing room.",
                "promptText": "Use the original idea with two water pools, compact routing, limited standing space, and box-switching rhythm.",
            },
        ]

    return [
        {
            "id": "A",
            "title": "Water-Split Route",
            "description": "Place water through the middle so the player has to route around it and decide which box to handle first. The water acts as the main blocker, not decoration.",
            "promptText": "Use the original idea with a central water split, detour routing, box-order decisions, and two-box planning pressure.",
        },
        {
            "id": "B",
            "title": "Waterside Goals",
            "description": "Put the goal area near water so pushing space is limited along the edge. The puzzle asks the player to adjust boxes without trapping them against the water.",
            "promptText": "Use the original idea with goals near water, edge-position pressure, and deadlock-aware box placement.",
        },
        {
            "id": "C",
            "title": "Twin Pool Detour",
            "description": "Use two small water areas to bend the route and make the player switch attention between boxes. The challenge comes from timing movement around water, not from a larger map.",
            "promptText": "Use the original idea with two water pools, a bent route, and box-switching rhythm.",
        },
    ]


def build_maze_fallback_options():
    return [
        {
            "id": "A",
            "title": "Loopback Corridor",
            "description": "Shape the route as a corridor the player must loop back through, using one box to open position before returning for the other. The maze feel comes from reused standing space.",
            "promptText": "Use the original idea with a loopback corridor, detour movement, reused positions, and two-box order planning.",
        },
        {
            "id": "B",
            "title": "Offset Hallways",
            "description": "Use staggered corridors so direct pushing is unreliable. The player must move a box to a transfer point, then approach it from another side.",
            "promptText": "Use the original idea with offset corridors, transfer points, side switching, and route-change pressure.",
        },
        {
            "id": "C",
            "title": "Side-Entry Goals",
            "description": "Make the goal area reachable from the side rather than straight ahead. The player has to preserve a path for the second box before closing the entrance.",
            "promptText": "Use the original idea with side-entry goals, preserved access paths, and ordered target delivery.",
        },
    ]


def build_compact_fallback_options():
    return [
        {
            "id": "A",
            "title": "One-Push Pressure",
            "description": "Keep the space tight but give each box one safe transfer spot. The player has to notice which push will close a corridor too early.",
            "promptText": "Use the original idea with compact space, safe transfer spots, corridor-blocking risk, and precise pushes.",
        },
        {
            "id": "B",
            "title": "Crossed Positions",
            "description": "Make the two box routes cross near the center so the player must clear standing space before advancing. The tightness comes from shared positions.",
            "promptText": "Use the original idea with crossed box routes, shared standing space, and ordered movement.",
        },
        {
            "id": "C",
            "title": "Edge Recovery",
            "description": "Let a box approach a risky edge while preserving one recovery route. The player pushes into danger, then uses the other side to bring it back toward the goals.",
            "promptText": "Use the original idea with risky edge pushes, a recovery path, and side-switching position play.",
        },
    ]


def build_general_fallback_options():
    return [
        {
            "id": "A",
            "title": "Order Decision",
            "description": "Turn the idea into a clear two-box order puzzle: solving one box opens standing space, but can also block the other box's route.",
            "promptText": "Use the original idea with two-box order decisions, opened standing space, and preserved routes.",
        },
        {
            "id": "B",
            "title": "Entrance Control",
            "description": "Give the key area only one or two entrances, so the player has to decide when to enter and when to push a box out of the doorway.",
            "promptText": "Use the original idea with limited entrances, temporary blockage, and standing-position judgment.",
        },
        {
            "id": "C",
            "title": "Compressed Goals",
            "description": "Cluster the goal area so the final pushes need careful placement. The first box must not block the second box's finishing route.",
            "promptText": "Use the original idea with clustered goals, endgame route preservation, and two-box placement order.",
        },
    ]


def contains_any(text, keywords):
    return any(keyword in text for keyword in keywords)


def get_blueprint_key(plan):
    return "|".join(
        [
            str(plan.get("archetype", "")),
            str(plan.get("targetLayout", "")),
            str(plan.get("obstacleStyle", "")),
            str(plan.get("waterStyle", "")),
        ]
    )


class BlueprintPlannerService:
    def __init__(
        self,
        llm_gateway: LLMGateway,
        *,
        settings: BackendSettings | None = None,
        clock: Clock | None = None,
        event_logger: EventLogger | None = None,
    ) -> None:
        self.llm_gateway = llm_gateway
        self.settings = settings or llm_gateway.settings
        self.clock = clock or llm_gateway.clock
        self.event_logger = event_logger or llm_gateway.event_logger
        self._recent_blueprints: list[str] = []
        self._plan_history_lock = threading.Lock()

    def remember_blueprint(self, plan: dict[str, Any]) -> None:
        key = get_blueprint_key(plan)
        with self._plan_history_lock:
            if key in self._recent_blueprints:
                self._recent_blueprints.remove(key)
            self._recent_blueprints.append(key)
            while len(self._recent_blueprints) > RECENT_BLUEPRINT_LIMIT:
                self._recent_blueprints.pop(0)

    def get_recent_blueprint_hint(self) -> str:
        with self._plan_history_lock:
            if not self._recent_blueprints:
                return "none"
            return "; ".join(self._recent_blueprints)

    def fallback_creative_idea_expansion(
        self,
        creative_context: dict[str, Any] | None,
        reason: str,
    ) -> dict[str, Any]:
        idea_text = clean_expansion_text((creative_context or {}).get("ideaText"))
        options = build_contextual_expansion_fallback_options(idea_text)
        self.event_logger.log_event(
            "WARNING",
            "creative_expansion_fallback",
            reason=str(reason or "")[:1000],
        )
        return {
            "options": options,
            "usedFallback": True,
            "message": reason,
        }

    def create_creative_idea_expansion(
        self,
        creative_context: dict[str, Any],
        request_id: str = "",
        max_attempts: int | None = None,
    ) -> LLMExecutionResult:
        request_id = new_request_id(request_id)
        attempts = (
            self.settings.llm_max_attempts
            if max_attempts is None
            else max_attempts
        )

        def validate_expansion(payload):
            return {
                "options": validate_creative_idea_expansion(payload),
                "usedFallback": False,
                "message": "",
            }

        try:
            return self.llm_gateway.execute_json_request(
                task="creative_idea_expansion",
                messages=build_creative_idea_expansion_messages(creative_context),
                validator=validate_expansion,
                temperature=self.settings.deepseek_expansion_temperature,
                timeout_seconds=self.settings.llm_timeout_seconds,
                max_attempts=attempts,
                request_id=request_id,
                validation_stage="expansion_validation",
            )
        except LLMServiceError as exception:
            fallback = self.fallback_creative_idea_expansion(
                creative_context,
                exception.safe_message,
            )
            return LLMExecutionResult(
                fallback,
                exception.attempts_used,
                request_id,
            )

    def create_human_adjustment_clarity_check(
        self,
        adjustment_text: str,
        request_id: str = "",
        max_attempts: int | None = None,
    ) -> LLMExecutionResult:
        attempts = (
            self.settings.llm_max_attempts
            if max_attempts is None
            else max_attempts
        )
        required_fields = {
            "problemScore",
            "targetScore",
            "directionScore",
            "detailScore",
            "reason",
        }

        def validate_clarity(payload):
            if not isinstance(payload, dict):
                raise ValueError("Human clarity response must be a JSON object")

            missing_fields = sorted(required_fields - set(payload))
            if missing_fields:
                raise ValueError(
                    "Human clarity response is missing fields: "
                    + ", ".join(missing_fields)
                )

            return validate_human_adjustment_clarity_payload(payload)

        return self.llm_gateway.execute_json_request(
            task="human_adjustment_clarity",
            messages=build_human_adjustment_clarity_messages(adjustment_text),
            validator=validate_clarity,
            temperature=0.0,
            timeout_seconds=self.settings.llm_timeout_seconds,
            max_attempts=attempts,
            request_id=request_id,
            validation_stage="clarity_validation",
        )

    def create_ha_revision_plans(
        self,
        context: dict[str, Any],
        request_id: str = "",
        max_attempts: int | None = None,
    ) -> LLMExecutionResult:
        attempts = (
            self.settings.llm_max_attempts
            if max_attempts is None
            else max_attempts
        )

        def validate_options(payload):
            options = validate_ha_revision_plan_options(
                payload,
                context.get("previousLevelPlan"),
                context.get("adjustmentText"),
            )
            return {"options": options}

        return self.llm_gateway.execute_json_request(
            task="ha_revision_plans",
            messages=build_ha_revision_plan_messages(context),
            validator=validate_options,
            temperature=0.2,
            timeout_seconds=self.settings.plan_llm_timeout_seconds,
            max_attempts=attempts,
            request_id=request_id,
            validation_stage="ha_plan_validation",
        )

    def create_ha_revision_plan_edit(
        self,
        context: dict[str, Any],
        request_id: str = "",
        max_attempts: int | None = None,
    ) -> LLMExecutionResult:
        attempts = (
            self.settings.llm_max_attempts
            if max_attempts is None
            else max_attempts
        )

        def validate_option(payload):
            option = validate_ha_revision_plan_edit(
                payload,
                context.get("originalOption"),
                context.get("previousLevelPlan"),
                context.get("adjustmentText"),
                context.get("editedDescription"),
            )
            return {"option": option}

        return self.llm_gateway.execute_json_request(
            task="ha_revision_plan_edit",
            messages=build_ha_revision_plan_edit_messages(context),
            validator=validate_option,
            temperature=0.1,
            timeout_seconds=self.settings.plan_llm_timeout_seconds,
            max_attempts=attempts,
            request_id=request_id,
            validation_stage="ha_plan_edit_validation",
        )

    def create_level_plan(
        self,
        creative_context: dict[str, Any] | None = None,
        request_id: str = "",
        max_attempts: int | None = None,
    ) -> LLMExecutionResult:
        attempts = (
            self.settings.llm_max_attempts
            if max_attempts is None
            else max_attempts
        )
        context = dict(creative_context or {})
        context["styleDescription"] = str(
            context.get("styleDescription") or ""
        ).strip()[:420]
        generation_preferences = normalize_generation_preferences(
            context.get("generationPreferences")
        )
        context["generationPreferences"] = generation_preferences
        feature_constraints = resolve_zero_feature_constraints(context)

        if "minWaterAreas" in generation_preferences:
            feature_constraints["noWater"] = (
                generation_preferences["minWaterAreas"] == 0
                and generation_preferences["maxWaterAreas"] == 0
            )

        if "minWallObstacleBlocks" in generation_preferences:
            feature_constraints["noInternalWalls"] = (
                generation_preferences["minWallObstacleBlocks"] == 0
                and generation_preferences["maxWallObstacleBlocks"] == 0
            )
        elif generation_preferences.get("corridorPlacement") not in (None, "", "none"):
            feature_constraints["noInternalWalls"] = False

        variation_seed = int(self.clock.now() * 1000)

        def validate_level_plan(payload):
            return apply_selected_ha_plan(
                payload,
                context,
                feature_constraints,
                generation_preferences,
            )

        execution = self.llm_gateway.execute_json_request(
            task="level_plan",
            messages=build_level_plan_messages(
                variation_seed,
                self.get_recent_blueprint_hint(),
                context,
                feature_constraints,
            ),
            validator=validate_level_plan,
            temperature=self.settings.deepseek_level_temperature,
            timeout_seconds=self.settings.plan_llm_timeout_seconds,
            max_attempts=attempts,
            request_id=request_id,
            validation_stage="blueprint_validation",
        )
        self.remember_blueprint(execution.value)
        return execution
