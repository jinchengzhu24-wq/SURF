"""Blueprint enums, limits, defaults, and Pydantic models."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GRID_WIDTH = 12
GRID_HEIGHT = 10
RECENT_BLUEPRINT_LIMIT = 3

BLUEPRINT_LIMITS: dict[str, tuple[int, int]] = {
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


class DifficultyLevel(StrEnum):
    EASY = "Easy"
    MEDIUM = "Medium"
    HARD = "Hard"
    RANDOM = "Random"


class LayoutStyle(StrEnum):
    COMPACT = "Compact"
    BALANCED = "Balanced"
    OPEN = "Open"
    RANDOM = "Random"


class BlueprintArchetype(StrEnum):
    GOAL_ROOM = "goal_room"
    BOTTLENECK_CORRIDOR = "bottleneck_corridor"
    SPLIT_ROUTE = "split_route"
    OPEN_WORKSHOP = "open_workshop"


class TargetLayout(StrEnum):
    CLUSTERED = "clustered"
    SPLIT_PAIR = "split_pair"
    EDGE_CLUSTER = "edge_cluster"


class ObstacleStyle(StrEnum):
    CENTRAL_BAFFLE = "central_baffle"
    SIDE_CHOKE = "side_choke"
    GOAL_GUARD = "goal_guard"


class WaterStyle(StrEnum):
    CORNER_POOL = "corner_pool"
    SIDE_POOL = "side_pool"
    ROUTE_DIVIDER = "route_divider"


class CorridorPlacement(StrEnum):
    NONE = "none"
    CENTER = "center"
    SIDE = "side"


class CorridorOrientation(StrEnum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"
    ANY = "any"


class CorridorRole(StrEnum):
    VISUAL_ONLY = "visual_only"
    PLAYER_ROUTE = "player_route"
    REQUIRED_BOX_ROUTE = "required_box_route"


class CorridorPriority(StrEnum):
    PREFERRED = "preferred"
    REQUIRED = "required"


BLUEPRINT_ENUMS: dict[str, set[str]] = {
    "archetype": {e.value for e in BlueprintArchetype},
    "targetLayout": {e.value for e in TargetLayout},
    "obstacleStyle": {e.value for e in ObstacleStyle},
    "waterStyle": {e.value for e in WaterStyle},
    "corridorPlacement": {e.value for e in CorridorPlacement},
    "corridorOrientation": {e.value for e in CorridorOrientation},
    "corridorRole": {e.value for e in CorridorRole},
    "corridorPriority": {e.value for e in CorridorPriority},
}

HA_CHANGE_FIELDS: frozenset[str] = frozenset(
    set(BLUEPRINT_LIMITS) | set(BLUEPRINT_ENUMS) | {"corridorWidth", "style"}
)

DEFAULT_LEVEL_PLAN: dict[str, Any] = {
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
    "archetype": BlueprintArchetype.BOTTLENECK_CORRIDOR.value,
    "targetLayout": TargetLayout.SPLIT_PAIR.value,
    "obstacleStyle": ObstacleStyle.SIDE_CHOKE.value,
    "waterStyle": WaterStyle.SIDE_POOL.value,
    "designNote": "Hard two-box route with a forced choke point and separated goals.",
    "corridorPlacement": CorridorPlacement.NONE.value,
    "corridorWidth": 0,
    "corridorOrientation": CorridorOrientation.ANY.value,
    "corridorRole": CorridorRole.VISUAL_ONLY.value,
    "corridorPriority": CorridorPriority.PREFERRED.value,
}


def _reject_bool_int(value: Any, field_name: str) -> Any:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must not be a boolean")
    return value


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


class LevelDesignPlan(BaseModel):
    """Canonical Sokoban blueprint parameters validated against generator limits."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    min_solution_steps: int = Field(alias="minSolutionSteps")
    max_solution_steps: int = Field(alias="maxSolutionSteps")
    min_water_areas: int = Field(alias="minWaterAreas")
    max_water_areas: int = Field(alias="maxWaterAreas")
    min_wall_obstacle_blocks: int = Field(alias="minWallObstacleBlocks")
    max_wall_obstacle_blocks: int = Field(alias="maxWallObstacleBlocks")
    min_pushes: int = Field(alias="minPushes")
    max_pushes: int = Field(alias="maxPushes")
    min_reverse_pulls: int = Field(alias="minReversePulls")
    max_reverse_pulls: int = Field(alias="maxReversePulls")
    style: str = DEFAULT_LEVEL_PLAN["style"]
    archetype: BlueprintArchetype
    target_layout: TargetLayout = Field(alias="targetLayout")
    obstacle_style: ObstacleStyle = Field(alias="obstacleStyle")
    water_style: WaterStyle = Field(alias="waterStyle")
    design_note: str = Field(default=DEFAULT_LEVEL_PLAN["designNote"], alias="designNote")
    corridor_placement: CorridorPlacement = Field(
        default=CorridorPlacement.NONE,
        alias="corridorPlacement",
    )
    corridor_width: int = Field(default=0, alias="corridorWidth")
    corridor_orientation: CorridorOrientation = Field(
        default=CorridorOrientation.ANY,
        alias="corridorOrientation",
    )
    corridor_role: CorridorRole = Field(
        default=CorridorRole.VISUAL_ONLY,
        alias="corridorRole",
    )
    corridor_priority: CorridorPriority = Field(
        default=CorridorPriority.PREFERRED,
        alias="corridorPriority",
    )

    @field_validator(
        "min_solution_steps",
        "max_solution_steps",
        "min_water_areas",
        "max_water_areas",
        "min_wall_obstacle_blocks",
        "max_wall_obstacle_blocks",
        "min_pushes",
        "max_pushes",
        "min_reverse_pulls",
        "max_reverse_pulls",
        "corridor_width",
        mode="before",
    )
    @classmethod
    def _no_bool_ints(cls, value: Any, info: Any) -> Any:
        return _reject_bool_int(value, str(info.field_name))

    @model_validator(mode="after")
    def _check_ranges_and_corridor(self) -> LevelDesignPlan:
        for attr, key in (
            ("min_solution_steps", "minSolutionSteps"),
            ("max_solution_steps", "maxSolutionSteps"),
            ("min_water_areas", "minWaterAreas"),
            ("max_water_areas", "maxWaterAreas"),
            ("min_wall_obstacle_blocks", "minWallObstacleBlocks"),
            ("max_wall_obstacle_blocks", "maxWallObstacleBlocks"),
            ("min_pushes", "minPushes"),
            ("max_pushes", "maxPushes"),
            ("min_reverse_pulls", "minReversePulls"),
            ("max_reverse_pulls", "maxReversePulls"),
        ):
            val = getattr(self, attr)
            minimum, maximum = BLUEPRINT_LIMITS[key]
            if val < minimum or val > maximum:
                raise ValueError(f"{key}={val} is outside {minimum}-{maximum}")

        for min_attr, max_attr, min_key, max_key in (
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
        ):
            if getattr(self, max_attr) < getattr(self, min_attr):
                raise ValueError(f"{max_key} must be >= {min_key}")

        if self.corridor_placement == CorridorPlacement.NONE.value:
            if self.corridor_width != 0:
                raise ValueError("corridorWidth must be 0 when corridorPlacement is none")
            if self.corridor_priority == CorridorPriority.REQUIRED.value:
                raise ValueError("required corridorPriority needs a corridor placement")
            if self.corridor_role == CorridorRole.REQUIRED_BOX_ROUTE.value:
                raise ValueError("required_box_route needs a corridor placement")
        elif self.corridor_width not in {1, 2}:
            raise ValueError("corridorWidth must be 1 or 2 when a corridor is requested")

        self.style = (str(self.style or "").strip() or DEFAULT_LEVEL_PLAN["style"])[:80]
        self.design_note = (
            str(self.design_note or "").strip() or DEFAULT_LEVEL_PLAN["designNote"]
        )[:160]
        return self

    def to_wire_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class GenerationPreferences(BaseModel):
    """Optional user-specified generation preference overrides."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    min_solution_steps: int | None = Field(default=None, alias="minSolutionSteps")
    max_solution_steps: int | None = Field(default=None, alias="maxSolutionSteps")
    min_water_areas: int | None = Field(default=None, alias="minWaterAreas")
    max_water_areas: int | None = Field(default=None, alias="maxWaterAreas")
    min_wall_obstacle_blocks: int | None = Field(default=None, alias="minWallObstacleBlocks")
    max_wall_obstacle_blocks: int | None = Field(default=None, alias="maxWallObstacleBlocks")
    min_pushes: int | None = Field(default=None, alias="minPushes")
    max_pushes: int | None = Field(default=None, alias="maxPushes")
    min_reverse_pulls: int | None = Field(default=None, alias="minReversePulls")
    max_reverse_pulls: int | None = Field(default=None, alias="maxReversePulls")
    archetype: str | None = ""
    target_layout: str | None = Field(default="", alias="targetLayout")
    obstacle_style: str | None = Field(default="", alias="obstacleStyle")
    water_style: str | None = Field(default="", alias="waterStyle")
    corridor_placement: str | None = Field(default="", alias="corridorPlacement")
    corridor_width: int | None = Field(default=None, alias="corridorWidth")
    corridor_orientation: str | None = Field(default="", alias="corridorOrientation")
    corridor_role: str | None = Field(default="", alias="corridorRole")
    corridor_priority: str | None = Field(default="", alias="corridorPriority")


class FeatureConstraints(BaseModel):
    no_water: bool = Field(default=False, alias="noWater")
    no_internal_walls: bool = Field(default=False, alias="noInternalWalls")

    model_config = ConfigDict(populate_by_name=True)


class HARevisionContract(BaseModel):
    changes: dict[str, Any]
    preserve_unlisted: bool = Field(default=True, alias="preserveUnlisted")

    model_config = ConfigDict(populate_by_name=True)


class HARevisionOption(BaseModel):
    id: str
    title: str
    description: str
    prompt_text: str = Field(alias="promptText")

    model_config = ConfigDict(populate_by_name=True)
