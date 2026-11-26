"""Tests for server.core.generator."""

from __future__ import annotations

import random

from server.core.clock import FakeClock
from server.core.generator import (
    OUTER_SHELL_TEMPLATES,
    STRUCTURE_TEMPLATES,
    WALL_OBSTACLE_SHAPES,
    generate_demo_level,
    generate_level_candidate,
)
from server.core.grid import parse_grid, replay_solution


def test_templates_are_immutable_tuples() -> None:
    assert isinstance(WALL_OBSTACLE_SHAPES, tuple)
    assert all(isinstance(shape, tuple) for shape in WALL_OBSTACLE_SHAPES)
    assert isinstance(OUTER_SHELL_TEMPLATES, tuple)
    assert all(isinstance(shell, tuple) for shell in OUTER_SHELL_TEMPLATES)
    assert isinstance(STRUCTURE_TEMPLATES, tuple)
    for template in STRUCTURE_TEMPLATES:
        assert isinstance(template.obstacle_anchors, tuple)
        assert isinstance(template.wall_shape_indices, tuple)
        assert isinstance(template.target_anchors, tuple)
        assert isinstance(template.water_anchors, tuple)


def test_generate_demo_level_is_deterministic_and_solvable() -> None:
    level_a = generate_demo_level(seed=42, max_attempts=30)
    level_b = generate_demo_level(seed=42, max_attempts=30)

    assert level_a.rows == level_b.rows
    assert level_a.seed == 42
    assert level_a.validation.solvable is True
    assert level_a.generation_summary["solutionSteps"] == level_a.validation.solution_steps

    parsed = parse_grid(level_a.rows)
    assert len(parsed.find_tiles("s")) == 2
    assert len(parsed.find_tiles("t")) == 2
    replay = replay_solution(parsed, level_a.validation.solution)
    assert replay.solved is True


def test_generate_level_candidate_respects_deadline() -> None:
    clock = FakeClock(monotonic_time=100.0)
    # Expired deadline returns None immediately instead of hanging.
    candidate = generate_level_candidate(
        random.Random(1),
        STRUCTURE_TEMPLATES[0],
        deadline=99.0,
        clock=clock,
    )
    assert candidate is None
