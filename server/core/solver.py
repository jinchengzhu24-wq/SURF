"""Fast Sokoban solvers: minimum-step BFS and lazy minimum-push 0-1 BFS."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .clock import SYSTEM_CLOCK, Clock
from .grid import (
    DIRECTIONS,
    TILE_BOX,
    TILE_PLAYER,
    TILE_TARGET,
    WALKABLE_TILES,
    Coord,
    Grid,
    parse_grid,
)

DEFAULT_MAX_STATES = 300_000
DEFAULT_DEADLINE_SECONDS = 2.5
_DEADLINE_CHECK_MASK = 1023  # Check clock.monotonic() every 1,024 pops.
_MOVE_DIRS: tuple[tuple[int, int, str], ...] = (
    (0, -1, "U"),
    (0, 1, "D"),
    (-1, 0, "L"),
    (1, 0, "R"),
)

State = tuple[Coord, tuple[Coord, ...]]


class SolverError(ValueError):
    """Base solver failure carrying a machine-readable code and search count."""

    def __init__(self, code: str, message: str, *, searched_states: int = 0) -> None:
        super().__init__(message)
        self.code = str(code)
        self.searched_states = int(searched_states)


class UnsolvableLevelError(SolverError):
    """Raised when the search exhausts all reachable states without solving."""


class SearchBudgetExceededError(SolverError):
    """Raised when explored states reach `max_states`."""


class SolverDeadlineExceededError(TimeoutError, SolverError):
    """Raised when the monotonic deadline is reached during search."""

    def __init__(self, message: str, *, searched_states: int = 0) -> None:
        TimeoutError.__init__(self, message)
        SolverError.__init__(self, "DEADLINE_EXCEEDED", message, searched_states=searched_states)


@dataclass(frozen=True, slots=True)
class SolveResult:
    """Result of minimum-step BFS."""

    solvable: bool
    rows: tuple[str, ...]
    searched_states: int
    solution_steps: int
    solution_pushes: int
    solution: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": True,
            "solvable": self.solvable,
            "searchedStates": self.searched_states,
            "solutionSteps": self.solution_steps,
            "solutionPushes": self.solution_pushes,
            "solution": self.solution,
        }


@dataclass(frozen=True, slots=True)
class PushSolveResult:
    """Result of lazy minimum-push 0-1 BFS."""

    solvable: bool
    rows: tuple[str, ...]
    searched_states: int
    minimum_pushes: int
    solution_steps: int
    solution: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": True,
            "solvable": self.solvable,
            "searchedStates": self.searched_states,
            "minimumPushes": self.minimum_pushes,
            "solutionSteps": self.solution_steps,
            "solution": self.solution,
        }


def compute_static_deadlocks(
    walkable: frozenset[Coord],
    targets: frozenset[Coord],
) -> frozenset[Coord]:
    """Compute cells from which no box can ever be pushed to any target.

    Reverse-push reachability from every target catches:
    - Simple non-target corners (two orthogonal axes blocked).
    - Targetless wall/water corridors and dead alcoves.
    Because every valid forward push `prev -> cur` (player at `prev - d`)
    corresponds to a valid reverse pull `cur -> prev` (standing at `prev - d`),
    any walkable cell outside the union of reverse-reachable sets is 100% dead.
    """
    live: set[Coord] = set()
    for target in targets:
        if target not in walkable or target in live:
            continue
        live.add(target)
        queue: deque[Coord] = deque([target])
        while queue:
            cx, cy = queue.popleft()
            for dx, dy in DIRECTIONS:
                prev_box = (cx - dx, cy - dy)
                if prev_box in live or prev_box not in walkable:
                    continue
                standing = (prev_box[0] - dx, prev_box[1] - dy)
                if standing not in walkable:
                    continue
                live.add(prev_box)
                queue.append(prev_box)
    return walkable - live


def _prepare_search(
    grid: Grid | Sequence[str],
) -> tuple[Grid, frozenset[Coord], Coord, tuple[Coord, ...], tuple[Coord, ...], frozenset[Coord]]:
    parsed = parse_grid(grid, validate_playable=True)
    walkable = frozenset(
        (x, y)
        for y, row in enumerate(parsed.rows)
        for x, tile in enumerate(row)
        if tile in WALKABLE_TILES
    )
    player = parsed.find_tile(TILE_PLAYER)
    start_boxes = tuple(sorted(parsed.find_tiles(TILE_BOX)))
    targets = frozenset(parsed.find_tiles(TILE_TARGET))
    sorted_targets = tuple(sorted(targets))
    dead_cells = compute_static_deadlocks(walkable, targets)
    return parsed, walkable, player, start_boxes, sorted_targets, dead_cells


def _reconstruct_trace(
    parents: dict[State, tuple[State | None, str, bool]],
    goal_state: State,
) -> tuple[str, int]:
    moves: list[str] = []
    pushes = 0
    cursor: State | None = goal_state
    while cursor is not None:
        parent, move_char, was_push = parents[cursor]
        if move_char:
            moves.append(move_char)
        if was_push:
            pushes += 1
        cursor = parent
    moves.reverse()
    return "".join(moves), pushes


def solve_min_steps(
    grid: Grid | Sequence[str],
    *,
    max_states: int = DEFAULT_MAX_STATES,
    deadline_seconds: float | None = DEFAULT_DEADLINE_SECONDS,
    clock: Clock | None = None,
) -> SolveResult:
    """Find a minimum-step Sokoban solution using BFS with static deadlock pruning."""
    active_clock = clock if clock is not None else SYSTEM_CLOCK
    deadline = (
        None
        if deadline_seconds is None
        else active_clock.monotonic() + max(0.0, float(deadline_seconds))
    )
    budget = max(1, int(max_states))
    parsed, walkable, player, start_boxes, sorted_targets, dead_cells = _prepare_search(grid)

    if any(box in dead_cells for box in start_boxes):
        raise UnsolvableLevelError(
            "UNSOLVABLE_LEVEL",
            "The level has a box on a statically deadlocked cell.",
            searched_states=1,
        )

    start_state: State = (player, start_boxes)
    queue: deque[State] = deque([start_state])
    parents: dict[State, tuple[State | None, str, bool]] = {start_state: (None, "", False)}
    pop_count = 0

    while queue:
        if deadline is not None and (pop_count & _DEADLINE_CHECK_MASK) == 0:
            if active_clock.monotonic() >= deadline:
                raise SolverDeadlineExceededError(
                    "The shared solver deadline was exhausted.",
                    searched_states=len(parents),
                )
        pop_count += 1
        current_state = queue.popleft()
        current_player, current_boxes = current_state

        if current_boxes == sorted_targets:
            trace, pushes = _reconstruct_trace(parents, current_state)
            return SolveResult(
                solvable=True,
                rows=parsed.rows,
                searched_states=len(parents),
                solution_steps=len(trace),
                solution_pushes=pushes,
                solution=trace,
            )

        if len(parents) >= budget:
            raise SearchBudgetExceededError(
                "SEARCH_BUDGET_EXCEEDED",
                "Level validation exceeded the search budget.",
                searched_states=len(parents),
            )

        px, py = current_player
        box_set = set(current_boxes)

        for dx, dy, move in _MOVE_DIRS:
            dest = (px + dx, py + dy)
            if dest not in walkable:
                continue

            next_boxes = current_boxes
            was_push = False
            if dest in box_set:
                box_dest = (dest[0] + dx, dest[1] + dy)
                if box_dest not in walkable or box_dest in box_set or box_dest in dead_cells:
                    continue
                moved = list(current_boxes)
                moved[moved.index(dest)] = box_dest
                next_boxes = tuple(sorted(moved))
                was_push = True

            next_state = (dest, next_boxes)
            if next_state in parents:
                continue

            parents[next_state] = (current_state, move, was_push)
            queue.append(next_state)

    raise UnsolvableLevelError(
        "UNSOLVABLE_LEVEL",
        "The level has no Sokoban solution.",
        searched_states=len(parents),
    )


def solve_min_pushes(
    grid: Grid | Sequence[str],
    *,
    max_states: int = DEFAULT_MAX_STATES,
    deadline_seconds: float | None = DEFAULT_DEADLINE_SECONDS,
    clock: Clock | None = None,
) -> PushSolveResult:
    """Find a minimum-push Sokoban solution using lazy 0-1 BFS."""
    active_clock = clock if clock is not None else SYSTEM_CLOCK
    deadline = (
        None
        if deadline_seconds is None
        else active_clock.monotonic() + max(0.0, float(deadline_seconds))
    )
    budget = max(1, int(max_states))
    parsed, walkable, player, start_boxes, sorted_targets, dead_cells = _prepare_search(grid)

    if any(box in dead_cells for box in start_boxes):
        raise UnsolvableLevelError(
            "UNSOLVABLE_LEVEL",
            "The level has a box on a statically deadlocked cell.",
            searched_states=1,
        )

    start_state: State = (player, start_boxes)
    pending: deque[tuple[int, State]] = deque([(0, start_state)])
    costs: dict[State, int] = {start_state: 0}
    parents: dict[State, tuple[State | None, str, bool]] = {start_state: (None, "", False)}
    pop_count = 0

    while pending:
        if deadline is not None and (pop_count & _DEADLINE_CHECK_MASK) == 0:
            if active_clock.monotonic() >= deadline:
                raise SolverDeadlineExceededError(
                    "The shared minimum-push deadline was exhausted.",
                    searched_states=len(costs),
                )
        pop_count += 1
        current_cost, current_state = pending.popleft()
        if current_cost > costs[current_state]:
            continue

        current_player, current_boxes = current_state
        if current_boxes == sorted_targets:
            trace, _ = _reconstruct_trace(parents, current_state)
            return PushSolveResult(
                solvable=True,
                rows=parsed.rows,
                searched_states=len(costs),
                minimum_pushes=current_cost,
                solution_steps=len(trace),
                solution=trace,
            )

        if len(costs) >= budget:
            raise SearchBudgetExceededError(
                "MIN_PUSH_SEARCH_BUDGET_EXCEEDED",
                "Minimum-push validation exceeded the search budget.",
                searched_states=len(costs),
            )

        px, py = current_player
        box_set = set(current_boxes)

        for dx, dy, move in _MOVE_DIRS:
            dest = (px + dx, py + dy)
            if dest not in walkable:
                continue

            next_boxes = current_boxes
            push_cost = 0
            if dest in box_set:
                box_dest = (dest[0] + dx, dest[1] + dy)
                if box_dest not in walkable or box_dest in box_set or box_dest in dead_cells:
                    continue
                moved = list(current_boxes)
                moved[moved.index(dest)] = box_dest
                next_boxes = tuple(sorted(moved))
                push_cost = 1

            next_state = (dest, next_boxes)
            next_cost = current_cost + push_cost
            if next_cost >= costs.get(next_state, budget + 1):
                continue

            costs[next_state] = next_cost
            parents[next_state] = (current_state, move, bool(push_cost))
            if push_cost:
                pending.append((next_cost, next_state))
            else:
                pending.appendleft((next_cost, next_state))

    raise UnsolvableLevelError(
        "UNSOLVABLE_LEVEL",
        "The level has no Sokoban solution.",
        searched_states=len(costs),
    )
