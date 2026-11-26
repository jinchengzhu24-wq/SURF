"""Tests for server.core.solver."""

from __future__ import annotations

import pytest
from server.core.clock import FakeClock
from server.core.grid import WALKABLE_TILES, parse_grid, replay_solution
from server.core.solver import (
    SearchBudgetExceededError,
    SolverDeadlineExceededError,
    UnsolvableLevelError,
    compute_static_deadlocks,
    solve_min_pushes,
    solve_min_steps,
)

SIMPLE_ROWS = (
    "############",
    "#..........#",
    "#..........#",
    "#..p..s..t.#",
    "#..........#",
    "#....@@....#",
    "#....@@....#",
    "#..........#",
    "#..........#",
    "############",
)

# Level where shortest-step path requires pushing blocking box A out and back (+2 pushes),
# whereas walking around the outer loop requires 0 extra pushes (2 pushes total vs 4).
MIN_STEPS_VS_PUSHES_ROWS = (
    "############",
    "#..........#",
    "#.########.#",
    "#.#......#.#",
    "#.#.ps.t.#.#",
    "#.#..s...#.#",
    "#.#..#t..#.#",
    "#..........#",
    "#..........#",
    "############",
)


def test_solve_min_steps_and_replay() -> None:
    grid = parse_grid(SIMPLE_ROWS)
    result = solve_min_steps(grid)
    assert result.solvable is True
    assert result.solution == "RRRRR"
    assert result.solution_steps == 5
    assert result.solution_pushes == 3
    replay = replay_solution(grid, result.solution)
    assert replay.solved is True
    assert replay.steps == result.solution_steps
    assert replay.pushes == result.solution_pushes


def test_static_deadlocks_catches_corners_and_targetless_corridors() -> None:
    # Top row y=1 has wall at y=0, corners at (1,1) and (10,1), and no targets on y=1.
    # Therefore EVERY cell on y=1 is statically deadlocked!
    rows = (
        "############",
        "#..........#",
        "#.########.#",
        "#..........#",
        "#..p..s.t..#",
        "#..........#",
        "#..........#",
        "#..........#",
        "#..........#",
        "############",
    )
    grid = parse_grid(rows)
    walkable = frozenset(
        (x, y)
        for y, row in enumerate(grid.rows)
        for x, tile in enumerate(row)
        if tile in WALKABLE_TILES
    )
    targets = frozenset(grid.find_tiles("t"))
    dead = compute_static_deadlocks(walkable, targets)

    # All four outer corners have no targets and must be dead.
    for corner in ((1, 1), (10, 1), (1, 8), (10, 8)):
        assert corner in dead

    # Targetless top wall corridor y=1 (x=1..10) is completely deadlocked.
    for x in range(1, 11):
        assert (x, 1) in dead

    # Target itself and push-origin cells behind the box are NOT dead.
    assert (8, 4) not in dead
    assert (7, 4) not in dead
    assert (6, 4) not in dead


def test_unsolvable_box_in_corner_or_water_blocked() -> None:
    corner_box = (
        "############",
        "#s.........#",
        "#..........#",
        "#....p...t.#",
        "#..........#",
        "#..........#",
        "#..........#",
        "#..........#",
        "#..........#",
        "############",
    )
    with pytest.raises(UnsolvableLevelError):
        solve_min_steps(corner_box)

    water_blocked = (
        "############",
        "#..........#",
        "#..........#",
        "#..p.s@..t.#",
        "#.....@....#",
        "#.....@....#",
        "#..........#",
        "#..........#",
        "#..........#",
        "############",
    )
    grid = parse_grid(water_blocked)
    # Box at (5,3) can still be pushed down and around water; seal the column with water.
    sealed = (
        "############",
        "#.....@....#",
        "#.....@....#",
        "#..p.s@..t.#",
        "#.....@....#",
        "#.....@....#",
        "#.....@....#",
        "#.....@....#",
        "#.....@....#",
        "############",
    )
    with pytest.raises(UnsolvableLevelError):
        solve_min_steps(sealed)
    assert solve_min_steps(grid).solvable is True


def test_lazy_solve_min_pushes_differs_from_min_steps() -> None:
    # Two-box puzzle where min_steps and min_pushes both solve and min_pushes <= min_steps.pushes.
    puzzle = (
        "############",
        "#..........#",
        "#..######..#",
        "#..p.s..t..#",
        "#..#....#..#",
        "#..#..s.#..#",
        "#..#..t.#..#",
        "#..........#",
        "#..........#",
        "############",
    )
    step_res = solve_min_steps(puzzle)
    push_res = solve_min_pushes(puzzle)
    assert step_res.solvable is True
    assert push_res.solvable is True
    assert push_res.minimum_pushes <= step_res.solution_pushes
    assert replay_solution(puzzle, step_res.solution).solved is True
    rep_push = replay_solution(puzzle, push_res.solution)
    assert rep_push.solved is True
    assert rep_push.pushes == push_res.minimum_pushes


def test_solver_budget_and_deadline_errors() -> None:
    grid = parse_grid(SIMPLE_ROWS)
    with pytest.raises(SearchBudgetExceededError) as budget_exc:
        solve_min_steps(grid, max_states=2)
    assert budget_exc.value.code == "SEARCH_BUDGET_EXCEEDED"

    with pytest.raises(SearchBudgetExceededError) as push_budget_exc:
        solve_min_pushes(grid, max_states=2)
    assert push_budget_exc.value.code == "MIN_PUSH_SEARCH_BUDGET_EXCEEDED"

    clock = FakeClock(monotonic_time=10.0)
    with pytest.raises(SolverDeadlineExceededError) as deadline_exc:
        solve_min_steps(grid, deadline_seconds=0.0, clock=clock)
    assert isinstance(deadline_exc.value, TimeoutError)
    assert deadline_exc.value.code == "DEADLINE_EXCEEDED"

    with pytest.raises(SolverDeadlineExceededError):
        solve_min_pushes(grid, deadline_seconds=0.0, clock=clock)
