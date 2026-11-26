"""Deterministic Human-AI (HA) blueprint revision contract validation and application."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from .models import (
    BLUEPRINT_ENUMS,
    BLUEPRINT_LIMITS,
    HA_CHANGE_FIELDS,
    HARevisionOption,
    LevelDesignPlan,
    _clean_text,
)
from .validation import resolve_zero_feature_constraints, validate_level_plan


def parse_ha_revision_contract(value: str | Mapping[str, Any]) -> dict[str, Any]:
    """Parse and validate an HA revision change contract."""
    if isinstance(value, str):
        contract = json.loads(value)
    elif isinstance(value, Mapping):
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
            "HA contract contains unsupported fields: " + ", ".join(sorted(unknown_fields))
        )

    normalized_changes: dict[str, Any] = {}
    for field, field_value in changes.items():
        if field in BLUEPRINT_LIMITS:
            if isinstance(field_value, bool) or not isinstance(field_value, int):
                raise ValueError(f"HA contract {field} must be an integer")
            if field == "minWallObstacleBlocks" and field_value == 3:
                field_value = 2
            minimum, maximum = BLUEPRINT_LIMITS[field]
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
        elif field in BLUEPRINT_ENUMS:
            clean_value = str(field_value or "").strip()
            if clean_value not in BLUEPRINT_ENUMS[field]:
                raise ValueError(f"HA contract {field}={clean_value} is not supported")
            normalized_changes[field] = clean_value
        elif field == "style":
            clean_value = str(field_value or "").strip()
            if not clean_value:
                raise ValueError("HA contract style cannot be empty")
            normalized_changes[field] = clean_value[:80]

    return {"changes": normalized_changes, "preserveUnlisted": True}


def build_ha_contract_constraints(
    previous_plan: Mapping[str, Any] | None,
    changes: Mapping[str, Any],
    adjustment_text: str | None,
) -> dict[str, bool]:
    constraints = resolve_zero_feature_constraints(
        {"latestAdjustmentText": str(adjustment_text or "")}
    )
    prev = previous_plan or {}
    if not {"minWaterAreas", "maxWaterAreas"}.intersection(changes):
        constraints["noWater"] = (
            prev.get("minWaterAreas") == 0 and prev.get("maxWaterAreas") == 0
        )
    if not {"minWallObstacleBlocks", "maxWallObstacleBlocks"}.intersection(changes):
        constraints["noInternalWalls"] = (
            prev.get("minWallObstacleBlocks") == 0 and prev.get("maxWallObstacleBlocks") == 0
        )
    return constraints


def validate_ha_revision_contract(
    contract: str | Mapping[str, Any],
    previous_plan: Mapping[str, Any] | None,
    adjustment_text: str | None,
) -> dict[str, Any]:
    if not isinstance(previous_plan, Mapping) or not previous_plan:
        raise ValueError("previous LevelDesignPlan is required")

    parsed = parse_ha_revision_contract(contract)
    wall_min = parsed["changes"].get(
        "minWallObstacleBlocks",
        previous_plan.get("minWallObstacleBlocks"),
    )
    wall_max = parsed["changes"].get(
        "maxWallObstacleBlocks",
        previous_plan.get("maxWallObstacleBlocks"),
    )
    if wall_min == 0 and wall_max == 0:
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
    constraints = build_ha_contract_constraints(previous_plan, parsed["changes"], adjustment_text)
    validated_candidate = validate_level_plan(candidate, constraints)
    parsed["changes"] = {field: validated_candidate[field] for field in parsed["changes"]}
    return parsed


def validate_ha_revision_plan_options(
    payload: Any,
    previous_plan: Mapping[str, Any] | None,
    adjustment_text: str | None,
) -> list[dict[str, Any]]:
    if payload is None:
        raise ValueError("model returned no HA revision payload")
    data = payload.model_dump() if isinstance(payload, BaseModel) else payload
    raw_options = (
        data
        if isinstance(data, list)
        else data.get("options")
        if isinstance(data, Mapping)
        else None
    )
    if not isinstance(raw_options, list) or len(raw_options) != 3:
        raise ValueError("HA revision payload must contain exactly three options")

    validated: list[dict[str, Any]] = []
    canonical_contracts: set[str] = set()
    for index, raw_option in enumerate(raw_options):
        option = raw_option.model_dump() if isinstance(raw_option, BaseModel) else raw_option
        if not isinstance(option, Mapping):
            raise ValueError(f"HA option {index + 1} must be an object")
        option_id = _clean_text(option.get("id")) or "ABC"[index]
        title = _clean_text(option.get("title"))[:64]
        description = _clean_text(option.get("description"))[:420]
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
            HARevisionOption(
                id=option_id[:12],
                title=title,
                description=description,
                promptText=prompt_text,
            ).model_dump(by_alias=True)
        )
    return validated


def validate_ha_revision_plan_edit(
    payload: Any,
    original_option: Mapping[str, Any] | None,
    previous_plan: Mapping[str, Any] | None,
    adjustment_text: str | None,
    edited_description: str | None,
) -> dict[str, Any]:
    if payload is None:
        raise ValueError("model returned no HA revision-plan edit payload")
    data = payload.model_dump() if isinstance(payload, BaseModel) else payload
    if not isinstance(data, Mapping):
        raise ValueError("HA revision-plan edit payload must be a JSON object")
    orig = dict(original_option or {})
    option_id = _clean_text(orig.get("id"))[:12]
    title = _clean_text(orig.get("title"))[:64]
    description = _clean_text(data.get("description"))[:420]
    edit_intent = " ".join(
        val for val in (_clean_text(adjustment_text), _clean_text(edited_description)) if val
    )
    contract = validate_ha_revision_contract(data.get("promptText"), previous_plan, edit_intent)
    original_contract = validate_ha_revision_contract(
        orig.get("promptText"), previous_plan, adjustment_text
    )
    if not option_id:
        raise ValueError("original HA option id is required")
    if not title:
        raise ValueError("original HA option title is required")
    if not description:
        raise ValueError("edited HA option description is required")
    if contract == original_contract:
        raise ValueError("edited HA option must produce a different change contract")
    prompt_text = json.dumps(contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return HARevisionOption(
        id=option_id,
        title=title,
        description=description,
        promptText=prompt_text,
    ).model_dump(by_alias=True)


def _apply_generation_preferences(
    plan: LevelDesignPlan | Mapping[str, Any],
    generation_preferences: Mapping[str, Any] | None,
) -> dict[str, Any]:
    data = plan.to_wire_dict() if isinstance(plan, LevelDesignPlan) else dict(plan)
    for key, value in (generation_preferences or {}).items():
        data[key] = value
    return data


def _parse_ha_client_inputs(
    creative_context: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    previous_value = creative_context.get("previousLevelPlan")
    if isinstance(previous_value, str):
        try:
            previous_plan = json.loads(previous_value)
        except json.JSONDecodeError as error:
            raise ValueError("previousLevelPlan must be valid JSON") from error
    else:
        previous_plan = previous_value
    if not isinstance(previous_plan, Mapping):
        raise ValueError("previousLevelPlan must be a JSON object")

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
    return dict(previous_plan), selected_option, contract


def apply_selected_ha_plan(
    generated_plan: LevelDesignPlan | Mapping[str, Any],
    creative_context: Mapping[str, Any] | None,
    feature_constraints: Mapping[str, Any] | None,
    generation_preferences: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministically apply selected HA contract & preferences over base/generated blueprint."""
    ctx = creative_context or {}
    is_ha = str(ctx.get("revisionMode") or "").strip().lower() == "ha"
    selected_text = str(ctx.get("selectedHAPlan") or "").strip()

    if not is_ha or not selected_text:
        candidate = _apply_generation_preferences(generated_plan, generation_preferences)
        effective_constraints = dict(feature_constraints or {})
        prefs = generation_preferences or {}
        if prefs.get("minWaterAreas") == 0 and prefs.get("maxWaterAreas") == 0:
            effective_constraints["noWater"] = True
        if prefs.get("minWallObstacleBlocks") == 0 and prefs.get("maxWallObstacleBlocks") == 0:
            effective_constraints["noInternalWalls"] = True
        return validate_level_plan(candidate, effective_constraints, generation_preferences)

    previous_plan, selected_option, contract = _parse_ha_client_inputs(ctx)
    candidate = dict(previous_plan)
    candidate.update(contract["changes"])
    title = _clean_text(selected_option.get("title"))
    description = _clean_text(selected_option.get("description"))
    candidate["designNote"] = (
        f"Human-AI revision: {title or 'selected plan'}. {description}"
    )[:160]
    candidate = _apply_generation_preferences(candidate, generation_preferences)
    effective_constraints = dict(feature_constraints or {})
    if candidate.get("minWaterAreas") == 0 and candidate.get("maxWaterAreas") == 0:
        effective_constraints["noWater"] = True
    if candidate.get("minWallObstacleBlocks") == 0 and candidate.get("maxWallObstacleBlocks") == 0:
        effective_constraints["noInternalWalls"] = True
    return validate_level_plan(candidate, effective_constraints, generation_preferences)
