"""Native-async 8000 Level Blueprint Plan pipeline (for POST /generate-level-plan)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.llm_client import LLMTransport, LLMValidationError, call_structured
from server.domain.blueprints import (
    normalize_generation_preferences,
    resolve_zero_feature_constraints,
    validate_level_plan,
)
from server.llm.prompts.level_plan import build_level_plan_messages
from server.llm.schemas import LevelPlanPayload, build_constrained_level_plan_model


@dataclass(frozen=True, slots=True)
class LevelPlanResult:
    """Validated level blueprint plan ready for Unity/algorithmic level generation."""

    plan: dict[str, Any]
    feature_constraints: dict[str, bool]
    model: str
    attempts_used: int
    request_id: str
    latency_ms: int
    usage: dict[str, int | None] = field(default_factory=dict)


class LevelPlanPipeline:
    """Native-async structured generator for LevelPlanPayload.

    HA short-circuiting is handled deterministically upstream in LevelPlanService.
    """

    def __init__(
        self,
        transport: LLMTransport,
        *,
        model: str,
        clock: Clock | None = None,
        extra_body: Mapping[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.transport = transport
        self.model = model
        self.clock = clock if clock is not None else SYSTEM_CLOCK
        self.extra_body = dict(extra_body) if extra_body is not None else None
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def generate_plan(
        self,
        *,
        variation_seed: int | str,
        recent_blueprint_hint: str = "",
        creative_context: Mapping[str, Any] | None = None,
        generation_preferences: Mapping[str, Any] | None = None,
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 2,
    ) -> LevelPlanResult:
        """Call the LLM to generate and validate a LevelPlanPayload."""
        ctx = dict(creative_context or {})
        raw_prefs = (
            generation_preferences
            if generation_preferences is not None
            else ctx.get("generationPreferences")
        )
        normalized_prefs = normalize_generation_preferences(raw_prefs)
        constraints = resolve_zero_feature_constraints(ctx)
        if "minWaterAreas" in normalized_prefs:
            constraints["noWater"] = (
                normalized_prefs["minWaterAreas"] == 0
                and normalized_prefs["maxWaterAreas"] == 0
            )
        if "minWallObstacleBlocks" in normalized_prefs:
            constraints["noInternalWalls"] = (
                normalized_prefs["minWallObstacleBlocks"] == 0
                and normalized_prefs["maxWallObstacleBlocks"] == 0
            )
        elif normalized_prefs.get("corridorPlacement") not in (None, "", "none"):
            constraints["noInternalWalls"] = False

        response_model = build_constrained_level_plan_model(
            no_water=constraints["noWater"],
            no_internal_walls=constraints["noInternalWalls"],
            generation_preferences=normalized_prefs,
        )

        messages = build_level_plan_messages(
            variation_seed=variation_seed,
            recent_blueprint_hint=recent_blueprint_hint,
            creative_context=ctx,
            feature_constraints=constraints,
            generation_preferences=normalized_prefs,
        )
        validated_holder: dict[str, dict[str, Any]] = {}

        def _validate(parsed: LevelPlanPayload) -> LevelPlanPayload:
            merged = parsed.model_dump(by_alias=True)
            merged.update(normalized_prefs)
            try:
                validated_holder["plan"] = validate_level_plan(
                    merged,
                    feature_constraints=constraints,
                    generation_preferences=normalized_prefs,
                )
            except ValueError as exc:
                raise LLMValidationError("LEVEL_PLAN_INVALID", str(exc)) from exc
            return parsed

        result = await call_structured(
            self.transport,
            task="level_plan",
            model=self.model,
            messages=messages,
            response_model=response_model,
            validator=_validate,
            deadline=deadline,
            max_attempts=max_attempts,
            temperature=0.6,
            max_tokens=4096,
            attempt_timeout_seconds=self.timeout_seconds,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        return LevelPlanResult(
            plan=validated_holder["plan"],
            feature_constraints=constraints,
            model=result.model,
            attempts_used=result.attempts_used,
            request_id=result.request_id,
            latency_ms=result.latency_ms,
            usage=result.usage,
        )
