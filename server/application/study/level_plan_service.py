"""Async Level Plan service with HA deterministic short-circuit and scoped history."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from server.core.clock import SYSTEM_CLOCK, Clock
from server.domain.blueprints import (
    RECENT_BLUEPRINT_LIMIT,
    apply_selected_ha_plan,
    normalize_generation_preferences,
    resolve_zero_feature_constraints,
)
from server.domain.errors import DomainError, ValidationError


class LevelPlanPipelineProtocol(Protocol):
    """Protocol satisfied by server.llm.pipelines.level_plan.LevelPlanPipeline."""

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
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class LevelPlanExecutionResult:
    """Validated blueprint plan paired with execution telemetry for HTTP headers."""

    plan: dict[str, Any]
    attempts_used: int
    request_id: str = ""
    used_ha_short_circuit: bool = False


def blueprint_key(plan: Mapping[str, Any]) -> str:
    """Build compact diversity-tracking signature for a LevelDesignPlan."""
    return "|".join(
        (
            str(plan.get("archetype", "")),
            str(plan.get("targetLayout", "")),
            str(plan.get("obstacleStyle", "")),
            str(plan.get("waterStyle", "")),
        )
    )


class _ScopedBlueprintHistory:
    """Bounded TTL cache mapping session/participant IDs to recent blueprint signatures."""

    def __init__(
        self,
        *,
        clock: Clock,
        ttl_seconds: float = 3600.0,
        max_scopes: int = 1024,
        per_scope_limit: int = RECENT_BLUEPRINT_LIMIT,
    ) -> None:
        self._clock = clock
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_scopes = max(1, int(max_scopes))
        self._per_scope_limit = max(1, int(per_scope_limit))
        self._entries: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()

    def _prune(self, now: float) -> None:
        expired = [k for k, (exp, _) in self._entries.items() if exp <= now]
        for key in expired:
            self._entries.pop(key, None)

    def get_hint(self, scope_key: str) -> str:
        if not scope_key:
            return "none"
        now = self._clock.monotonic()
        self._prune(now)
        entry = self._entries.get(scope_key)
        if entry is None:
            return "none"
        _, keys = entry
        self._entries.move_to_end(scope_key)
        return "; ".join(keys) if keys else "none"

    def remember(self, scope_key: str, plan: Mapping[str, Any]) -> None:
        if not scope_key:
            return
        now = self._clock.monotonic()
        self._prune(now)
        sig = blueprint_key(plan)
        if scope_key in self._entries:
            _, keys = self._entries[scope_key]
            if sig in keys:
                keys.remove(sig)
            keys.append(sig)
            while len(keys) > self._per_scope_limit:
                keys.pop(0)
            self._entries[scope_key] = (now + self._ttl_seconds, keys)
            self._entries.move_to_end(scope_key)
            return

        while len(self._entries) >= self._max_scopes:
            self._entries.popitem(last=False)
        self._entries[scope_key] = (now + self._ttl_seconds, [sig])


def _resolve_scope_key(context: Mapping[str, Any]) -> str:
    for field in ("sessionId", "participantId", "ideaId"):
        value = str(context.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    return ""


def _extract_plan_and_telemetry(raw: Any, default_request_id: str) -> tuple[Any, int, str]:
    if isinstance(raw, Mapping):
        if "plan" in raw:
            return (
                raw["plan"],
                int(raw.get("attempts_used") or raw.get("attemptsUsed") or 1),
                str(raw.get("request_id") or raw.get("requestId") or default_request_id),
            )
        return raw, 1, default_request_id
    if hasattr(raw, "plan"):
        plan_obj = raw.plan
        attempts = int(getattr(raw, "attempts_used", 1) or 1)
        req_id = str(getattr(raw, "request_id", "") or default_request_id)
        return plan_obj, attempts, req_id
    if hasattr(raw, "value"):
        return (
            raw.value,
            int(getattr(raw, "attempts_used", 1) or 1),
            str(getattr(raw, "request_id", "") or default_request_id),
        )
    return raw, 1, default_request_id


class LevelPlanService:
    """Level blueprint planner with HA deterministic short-circuit and scoped history."""

    def __init__(
        self,
        pipeline: LevelPlanPipelineProtocol | None = None,
        *,
        clock: Clock | None = None,
        history_ttl_seconds: float = 3600.0,
        max_history_scopes: int = 1024,
        default_max_attempts: int = 2,
    ) -> None:
        self._pipeline = pipeline
        self._clock = clock if clock is not None else SYSTEM_CLOCK
        self._default_max_attempts = max(1, min(2, int(default_max_attempts)))
        self._history = _ScopedBlueprintHistory(
            clock=self._clock,
            ttl_seconds=history_ttl_seconds,
            max_scopes=max_history_scopes,
        )

    def get_recent_blueprint_hint(self, scope_key: str) -> str:
        """Return recent blueprint signatures scoped to one session/participant."""
        return self._history.get_hint(scope_key)

    async def create_level_plan(
        self,
        creative_context: Mapping[str, Any] | None = None,
        *,
        request_id: str = "",
        max_attempts: int | None = None,
        deadline: float | None = None,
    ) -> LevelPlanExecutionResult:
        """Produce a validated LevelDesignPlan, short-circuiting HA selections locally."""
        context = dict(creative_context or {})
        context["styleDescription"] = str(context.get("styleDescription") or "").strip()[:420]

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

        scope_key = _resolve_scope_key(context)
        revision_mode = str(context.get("revisionMode") or "").strip().lower()
        selected_ha_plan = str(context.get("selectedHAPlan") or "").strip()

        # Deterministic HA short-circuit: apply selected contract without calling LLM.
        if revision_mode == "ha" and selected_ha_plan:
            try:
                validated_plan = apply_selected_ha_plan(
                    {},
                    context,
                    feature_constraints,
                    generation_preferences,
                )
            except ValueError as exc:
                raise ValidationError(
                    "INVALID_HA_PLAN",
                    "error.validation_failed",
                    message=str(exc),
                ) from exc
            self._history.remember(scope_key, validated_plan)
            return LevelPlanExecutionResult(
                plan=validated_plan,
                attempts_used=0,
                request_id=request_id,
                used_ha_short_circuit=True,
            )

        if self._pipeline is None:
            raise DomainError(
                "LLM_NOT_CONFIGURED",
                "error.configuration",
                retryable=False,
                message="Level plan LLM pipeline is not configured",
            )

        attempts = (
            self._default_max_attempts
            if max_attempts is None
            else max(1, min(2, int(max_attempts)))
        )
        variation_seed = int(self._clock.utc_datetime().timestamp() * 1000)
        recent_hint = self._history.get_hint(scope_key)

        raw_result = await self._pipeline.generate_plan(
            variation_seed=variation_seed,
            recent_blueprint_hint=recent_hint,
            creative_context=context,
            generation_preferences=generation_preferences,
            deadline=deadline,
            request_id=request_id,
            max_attempts=attempts,
        )
        raw_plan, attempts_used, resolved_req_id = _extract_plan_and_telemetry(
            raw_result, request_id
        )
        try:
            validated_plan = apply_selected_ha_plan(
                raw_plan,
                context,
                feature_constraints,
                generation_preferences,
            )
        except ValueError as exc:
            raise ValidationError(
                "INVALID_LEVEL_PLAN",
                "error.validation_failed",
                message=str(exc),
            ) from exc

        self._history.remember(scope_key, validated_plan)
        return LevelPlanExecutionResult(
            plan=validated_plan,
            attempts_used=attempts_used,
            request_id=resolved_req_id,
            used_ha_short_circuit=False,
        )
