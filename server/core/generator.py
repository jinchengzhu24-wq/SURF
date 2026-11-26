"""Deterministic/seeded 12x10 Sokoban generator."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ._generator_obstacles import (
    _add_wall_obstacle_blocks,
    _add_water_areas,
    _oriented_template,
    _validate_tile_rules,
)
from ._generator_scoring import (
    BOX_COUNT,
    MAX_REVERSE_PULL_ATTEMPTS,
    _build_rows,
    _count_obstacle_influence,
    _count_water_areas,
    _has_adjacent_box_target_pair,
    _place_fallback_targets,
    _place_player,
    _place_targets,
    _quality_score,
    _reverse_pull_boxes,
)
from ._generator_templates import (
    OUTER_SHELL_TEMPLATES,
    STRUCTURE_TEMPLATES,
    WALL_OBSTACLE_SHAPES,
    StructureTemplate,
)
from .clock import SYSTEM_CLOCK, Clock
from .grid import HEIGHT, WIDTH, GridValidationError
from .solver import (
    DEFAULT_MAX_STATES,
    SolverError,
    SolveResult,
    solve_min_steps,
)

MAX_GENERATION_ATTEMPTS = 300
ALGORITHM_CANDIDATE_SAMPLE_COUNT = 10
MIN_SOLUTION_STEPS = 12
MAX_SOLUTION_STEPS = 35
MIN_REVERSE_PULLS = 8
MAX_TARGET_REVERSE_PULLS = 24
PREFERRED_MIN_SOLUTION_STEPS = 22
PREFERRED_MIN_PUSHES = 8
PREFERRED_MIN_REVERSE_PULLS = 18
MIN_OBSTACLE_INFLUENCE = 2
MIN_QUALITY_SCORE = 220


@dataclass(frozen=True, slots=True)
class GenerationCandidate:
    """Validated candidate produced during demo level generation."""

    rows: tuple[str, ...]
    validation: SolveResult
    reverse_pulls: int
    quality_score: int
    obstacle_influence: int
    water_areas: int
    water_tiles: int
    wall_tiles: int
    archetype: str
    target_layout: str
    obstacle_style: str
    water_style: str


@dataclass(frozen=True, slots=True)
class DemoLevel:
    """Final seeded demo level and generation telemetry."""

    rows: tuple[str, ...]
    validation: SolveResult
    seed: int
    attempts: int
    generation_summary: dict[str, Any]


def _is_qualified(candidate: GenerationCandidate) -> bool:
    return (
        MIN_SOLUTION_STEPS <= candidate.validation.solution_steps <= MAX_SOLUTION_STEPS
        and candidate.validation.solution_pushes >= PREFERRED_MIN_PUSHES
        and candidate.reverse_pulls >= PREFERRED_MIN_REVERSE_PULLS
        and candidate.obstacle_influence >= MIN_OBSTACLE_INFLUENCE
        and candidate.quality_score >= MIN_QUALITY_SCORE
    )


def _candidate_key(candidate: GenerationCandidate) -> tuple[int, int, int, int]:
    return (
        int(_is_qualified(candidate)),
        candidate.quality_score,
        candidate.validation.solution_steps,
        candidate.reverse_pulls,
    )


def _try_solve(
    rows: Sequence[str],
    *,
    deadline_seconds: float | None = 2.5,
    clock: Clock | None = None,
) -> SolveResult | None:
    try:
        return solve_min_steps(rows, deadline_seconds=deadline_seconds, clock=clock)
    except (GridValidationError, SolverError):
        return None


def generate_level_candidate(
    rng: random.Random,
    template: StructureTemplate | None = None,
    *,
    include_optional_obstacles: bool = True,
    deadline: float | None = None,
    clock: Clock | None = None,
) -> GenerationCandidate | None:
    """Build and validate a single candidate demo level from a template."""
    active_clock = clock if clock is not None else SYSTEM_CLOCK
    chosen_template = template if template is not None else rng.choice(STRUCTURE_TEMPLATES)
    shell_rows, oriented = _oriented_template(chosen_template, rng)
    grid = [list(row) for row in shell_rows]

    target_layout = rng.choice(("clustered", "edge_cluster", "split_pair"))
    obstacle_style = rng.choice(("goal_guard", "side_choke", "central_block"))
    water_style = rng.choice(("corner_pool", "route_divider", "side_pool"))

    if include_optional_obstacles:
        if not _add_wall_obstacle_blocks(grid, rng, oriented, obstacle_style, target_layout):
            return None
        if not _add_water_areas(grid, rng, oriented, water_style):
            return None

    targets = _place_targets(grid, rng, oriented, target_layout)
    if targets is None:
        return None
    player = _place_player(grid, rng, targets, oriented)
    if player is None:
        return None
    reverse = _reverse_pull_boxes(
        grid,
        targets,
        player,
        rng,
        rng.randint(PREFERRED_MIN_REVERSE_PULLS, MAX_TARGET_REVERSE_PULLS),
    )
    if reverse is None:
        return None
    boxes, final_player, reverse_pulls = reverse

    if _has_adjacent_box_target_pair(boxes, targets):
        return None
    if not _validate_tile_rules(grid):
        return None

    rows = _build_rows(grid, boxes, targets, final_player)
    solve_budget: float | None = 2.5
    if deadline is not None:
        remaining = deadline - active_clock.monotonic()
        if remaining <= 0:
            return None
        solve_budget = min(2.5, remaining)

    validation = _try_solve(rows, deadline_seconds=solve_budget, clock=active_clock)
    if validation is None:
        return None

    wall_tiles = sum(row.count("#") for row in rows)
    water_tiles = sum(row.count("@") for row in rows)
    water_areas = _count_water_areas(rows)
    obstacle_influence = _count_obstacle_influence(rows, boxes, targets, final_player)
    quality_score = _quality_score(
        validation,
        reverse_pulls,
        water_tiles,
        water_areas,
        obstacle_influence,
        targets,
        rows,
    )
    return GenerationCandidate(
        rows=validation.rows,
        validation=validation,
        reverse_pulls=reverse_pulls,
        quality_score=quality_score,
        obstacle_influence=obstacle_influence,
        water_areas=water_areas,
        water_tiles=water_tiles,
        wall_tiles=wall_tiles,
        archetype=oriented.archetype,
        target_layout=target_layout,
        obstacle_style=obstacle_style,
        water_style=water_style,
    )


def _build_fallback(
    rng: random.Random,
    attempts: int,
    *,
    deadline: float | None = None,
    clock: Clock | None = None,
) -> GenerationCandidate | None:
    active_clock = clock if clock is not None else SYSTEM_CLOCK
    grid = [
        ["#" if x in (0, WIDTH - 1) or y in (0, HEIGHT - 1) else "." for x in range(WIDTH)]
        for y in range(HEIGHT)
    ]
    targets = _place_fallback_targets(grid, rng)
    if targets is None:
        return None
    player = _place_player(grid, rng, targets, None)
    if player is None:
        return None

    for _ in range(min(MAX_REVERSE_PULL_ATTEMPTS, max(40, attempts * 2))):
        if deadline is not None and active_clock.monotonic() >= deadline:
            break
        reverse = _reverse_pull_boxes(
            grid,
            targets,
            player,
            rng,
            rng.randint(PREFERRED_MIN_REVERSE_PULLS, MAX_TARGET_REVERSE_PULLS),
        )
        if reverse is None:
            continue
        boxes, final_player, reverse_pulls = reverse
        if _has_adjacent_box_target_pair(boxes, targets):
            continue
        rows = _build_rows(grid, boxes, targets, final_player)
        validation = _try_solve(rows, deadline_seconds=2.5, clock=active_clock)
        if validation is None:
            continue
        wall_tiles = sum(row.count("#") for row in rows)
        obstacle_influence = _count_obstacle_influence(rows, boxes, targets, final_player)
        return GenerationCandidate(
            rows=validation.rows,
            validation=validation,
            reverse_pulls=reverse_pulls,
            quality_score=_quality_score(
                validation,
                reverse_pulls,
                0,
                0,
                obstacle_influence,
                targets,
                rows,
            ),
            obstacle_influence=obstacle_influence,
            water_areas=0,
            water_tiles=0,
            wall_tiles=wall_tiles,
            archetype="fallback",
            target_layout="split_pair",
            obstacle_style="none",
            water_style="none",
        )
    return None


def _as_demo_level(
    candidate: GenerationCandidate,
    seed: int,
    attempts: int,
    mode: str,
    selectable_count: int,
) -> DemoLevel:
    summary: dict[str, Any] = {
        "mode": mode,
        "archetype": candidate.archetype,
        "targetLayout": candidate.target_layout,
        "obstacleStyle": candidate.obstacle_style,
        "waterStyle": candidate.water_style,
        "candidateAttempts": attempts,
        "selectableCandidates": selectable_count,
        "qualityScore": candidate.quality_score,
        "solutionSteps": candidate.validation.solution_steps,
        "solutionPushes": candidate.validation.solution_pushes,
        "reversePulls": candidate.reverse_pulls,
        "obstacleInfluence": candidate.obstacle_influence,
        "waterAreas": candidate.water_areas,
        "waterTiles": candidate.water_tiles,
        "wallTiles": candidate.wall_tiles,
    }
    return DemoLevel(
        rows=candidate.validation.rows,
        validation=candidate.validation,
        seed=seed,
        attempts=attempts,
        generation_summary=summary,
    )


def generate_demo_level(
    seed: int | None = None,
    max_attempts: int = MAX_GENERATION_ATTEMPTS,
    *,
    deadline_seconds: float | None = 15.0,
    clock: Clock | None = None,
) -> DemoLevel:
    """Generate a reproducible, validated two-box Algorithm_Level demo map."""
    active_clock = clock if clock is not None else SYSTEM_CLOCK
    deadline = (
        None
        if deadline_seconds is None
        else active_clock.monotonic() + max(0.01, float(deadline_seconds))
    )
    actual_seed = int(seed) if seed is not None else random.SystemRandom().randrange(0, 2**63)
    rng = random.Random(actual_seed)
    attempt_limit = max(1, min(int(max_attempts), MAX_GENERATION_ATTEMPTS))
    selectable: list[GenerationCandidate] = []
    best_valid: GenerationCandidate | None = None
    attempts_run = 0

    for attempt in range(1, attempt_limit + 1):
        if deadline is not None and active_clock.monotonic() >= deadline:
            break
        attempts_run = attempt
        template = rng.choice(STRUCTURE_TEMPLATES)
        candidate = generate_level_candidate(
            rng,
            template,
            include_optional_obstacles=True,
            deadline=deadline,
            clock=active_clock,
        )
        if candidate is None:
            continue
        if best_valid is None or _candidate_key(candidate) > _candidate_key(best_valid):
            best_valid = candidate
        if _is_qualified(candidate):
            selectable.append(candidate)
            if len(selectable) >= ALGORITHM_CANDIDATE_SAMPLE_COUNT:
                selected = max(selectable, key=_candidate_key)
                return _as_demo_level(
                    selected, actual_seed, attempt, "algorithm_level", len(selectable)
                )

    if best_valid is not None:
        return _as_demo_level(
            best_valid,
            actual_seed,
            max(1, attempts_run),
            "algorithm_level_relaxed",
            len(selectable),
        )

    fallback = _build_fallback(rng, attempt_limit, deadline=deadline, clock=active_clock)
    if fallback is not None:
        return _as_demo_level(
            fallback,
            actual_seed,
            max(1, attempts_run),
            "fallback",
            len(selectable),
        )
    raise RuntimeError("Unable to generate a validated Algorithm_Level demo level.")


__all__ = [
    "ALGORITHM_CANDIDATE_SAMPLE_COUNT",
    "BOX_COUNT",
    "DEFAULT_MAX_STATES",
    "DemoLevel",
    "GenerationCandidate",
    "MAX_GENERATION_ATTEMPTS",
    "OUTER_SHELL_TEMPLATES",
    "STRUCTURE_TEMPLATES",
    "StructureTemplate",
    "WALL_OBSTACLE_SHAPES",
    "generate_demo_level",
    "generate_level_candidate",
]
