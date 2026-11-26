"""Canonical 12x10 Sokoban grid value object, shell geometry, ops, and replay."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

WIDTH = 12
HEIGHT = 10

TILE_VOID = " "
TILE_WALL = "#"
TILE_FLOOR = "."
TILE_WATER = "@"
TILE_PLAYER = "p"
TILE_BOX = "s"
TILE_TARGET = "t"

ALLOWED_TILES = frozenset(" #.@pst")
WALKABLE_TILES = frozenset(".pst")
ENTITY_TILES = frozenset("pst")
AI_EDITABLE_TILES = frozenset("#.@")

AI_ALLOWED_OPERATIONS = frozenset({"add_wall", "remove_wall", "add_water", "remove_water"})
_TRANSITION_TO_OP: dict[tuple[str, str], str] = {
    (".", "#"): "add_wall",
    ("#", "."): "remove_wall",
    (".", "@"): "add_water",
    ("@", "."): "remove_water",
}
_OP_TO_TRANSITION: dict[str, tuple[str, str]] = {
    op: transition for transition, op in _TRANSITION_TO_OP.items()
}

DIRECTIONS: tuple[tuple[int, int], ...] = ((0, -1), (0, 1), (-1, 0), (1, 0))
MOVE_DELTAS: dict[str, tuple[int, int]] = {
    "U": (0, -1),
    "D": (0, 1),
    "L": (-1, 0),
    "R": (1, 0),
}

TILE_LEGEND: dict[str, str] = {
    " ": "void",
    "#": "wall",
    ".": "floor",
    "@": "water",
    "p": "player",
    "s": "box",
    "t": "target",
}

Coord = tuple[int, int]


class GridValidationError(ValueError):
    """Domain validation error carrying a stable error code and metadata."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details: dict[str, Any] = dict(details or {})


@dataclass(frozen=True, slots=True)
class TileChange:
    """Single cell difference between two 12x10 grids (0-based x, y)."""

    x: int
    y: int
    before: str
    after: str

    @property
    def row(self) -> int:
        return self.y + 1

    @property
    def column(self) -> int:
        return self.x + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "row": self.row,
            "column": self.column,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True, slots=True)
class GridOperation:
    """Single 1-based AI map edit operation restricted to wall/water changes."""

    row: int
    column: int
    to: str
    op: str | None = None
    expected_from: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Deterministic result of replaying a U/D/L/R move sequence."""

    solved: bool
    steps: int
    pushes: int
    player: Coord
    boxes: tuple[Coord, ...]
    trace: str


def _extract_rows(grid_or_rows: Grid | Sequence[str]) -> tuple[str, ...]:
    if isinstance(grid_or_rows, Grid):
        return grid_or_rows.rows
    return _normalize_shape_and_tiles(grid_or_rows)


def _normalize_shape_and_tiles(rows: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(rows, (list, tuple)) or len(rows) != HEIGHT:
        raise GridValidationError(
            "INVALID_HEIGHT",
            f"The level must contain exactly {HEIGHT} rows.",
        )

    normalized: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, str) or len(row) != WIDTH:
            raise GridValidationError(
                "INVALID_WIDTH",
                f"Row {index + 1} must contain exactly {WIDTH} tiles.",
                {"row": index},
            )
        unknown = sorted(set(row) - ALLOWED_TILES)
        if unknown:
            raise GridValidationError(
                "UNKNOWN_TILE",
                f"Row {index + 1} contains an unknown tile.",
                {"row": index, "tiles": unknown},
            )
        normalized.append(row)
    return tuple(normalized)


def _flood_exterior_void(rows: tuple[str, ...], *, strict_closure: bool) -> frozenset[Coord]:
    """Flood-fill exterior void `' '` from canvas perimeter with enqueue-time visited tracking."""
    queue: deque[Coord] = deque()
    visited: set[Coord] = set()

    def _check_breach(x: int, y: int, tile: str) -> None:
        if strict_closure:
            raise GridValidationError(
                "OPEN_OUTER_WALL",
                "The outer wall must be closed with # wall tiles; water cannot close the boundary.",
                {"row": y + 1, "column": x + 1, "tile": tile},
            )

    for y in range(HEIGHT):
        for x in range(WIDTH):
            if x not in (0, WIDTH - 1) and y not in (0, HEIGHT - 1):
                continue
            tile = rows[y][x]
            if tile == TILE_WALL:
                continue
            if tile != TILE_VOID:
                _check_breach(x, y, tile)
                continue
            coord = (x, y)
            if coord not in visited:
                visited.add(coord)
                queue.append(coord)

    while queue:
        x, y = queue.popleft()
        for dx, dy in DIRECTIONS:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
                continue
            coord = (nx, ny)
            if coord in visited:
                continue
            next_tile = rows[ny][nx]
            if next_tile == TILE_WALL:
                continue
            if next_tile != TILE_VOID:
                _check_breach(nx, ny, next_tile)
                continue
            visited.add(coord)
            queue.append(coord)

    if strict_closure:
        for y in range(HEIGHT):
            for x in range(WIDTH):
                if rows[y][x] == TILE_VOID and (x, y) not in visited:
                    _check_breach(x, y, TILE_VOID)

    return frozenset(visited)


@dataclass(frozen=True, slots=True)
class Grid:
    """Immutable canonical 12x10 Sokoban level grid."""

    rows: tuple[str, ...]

    def __post_init__(self) -> None:
        normalized = _normalize_shape_and_tiles(self.rows)
        object.__setattr__(self, "rows", normalized)

    @property
    def width(self) -> int:
        return WIDTH

    @property
    def height(self) -> int:
        return HEIGHT

    def to_rows(self) -> list[str]:
        return list(self.rows)

    def tile_at(self, x: int, y: int) -> str:
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            return TILE_VOID
        return self.rows[y][x]

    def find_tiles(self, tile: str) -> tuple[Coord, ...]:
        if len(tile) != 1 or tile not in ALLOWED_TILES:
            raise GridValidationError("UNKNOWN_TILE", f"Unsupported tile {tile!r}.")
        return tuple(
            (x, y)
            for y, row in enumerate(self.rows)
            for x, value in enumerate(row)
            if value == tile
        )

    def find_tile(self, tile: str) -> Coord:
        matches = self.find_tiles(tile)
        if len(matches) != 1:
            raise GridValidationError(
                "INVALID_TILE_COUNT",
                f"Expected exactly one {tile!r} tile, found {len(matches)}.",
                {"tile": tile, "count": len(matches)},
            )
        return matches[0]

    def exterior_void(self) -> frozenset[Coord]:
        return _flood_exterior_void(self.rows, strict_closure=False)

    def boundary_walls(self) -> frozenset[Coord]:
        return boundary_walls(self.rows)

    def outer_shell(self) -> frozenset[Coord]:
        return outer_shell(self.rows)

    def fingerprint(self) -> str:
        return map_fingerprint(self.rows)


def parse_structural_grid(rows: Grid | Sequence[str]) -> Grid:
    """Validate 12x10 dimensions and tile alphabet without playable entity rules."""
    if isinstance(rows, Grid):
        return rows
    return Grid(_normalize_shape_and_tiles(rows))


def parse_grid(
    rows: Grid | Sequence[str],
    *,
    validate_playable: bool = True,
    max_boxes: int | None = 2,
) -> Grid:
    """Parse and validate a canonical 12x10 Sokoban grid."""
    grid = parse_structural_grid(rows)
    if not validate_playable:
        return grid

    normalized = grid.rows
    player_count = sum(row.count(TILE_PLAYER) for row in normalized)
    box_count = sum(row.count(TILE_BOX) for row in normalized)
    target_count = sum(row.count(TILE_TARGET) for row in normalized)

    if player_count != 1:
        raise GridValidationError(
            "INVALID_PLAYER_COUNT",
            "The level must contain exactly one player.",
            {"count": player_count},
        )
    if box_count < 1 or (max_boxes is not None and box_count > max_boxes):
        limit_desc = f"between 1 and {max_boxes}" if max_boxes is not None else "at least 1"
        raise GridValidationError(
            "INVALID_BOX_COUNT",
            f"The level must contain {limit_desc} boxes.",
            {"count": box_count},
        )
    if target_count != box_count:
        raise GridValidationError(
            "MISMATCHED_TARGET_COUNT",
            "The number of targets must match the number of boxes.",
            {"boxes": box_count, "targets": target_count},
        )

    _flood_exterior_void(normalized, strict_closure=True)
    return grid


def exterior_void(grid_or_rows: Grid | Sequence[str]) -> frozenset[Coord]:
    """Return all `' '` cells connected to the 12x10 canvas perimeter."""
    rows = _extract_rows(grid_or_rows)
    return _flood_exterior_void(rows, strict_closure=False)


def boundary_walls(grid_or_rows: Grid | Sequence[str]) -> frozenset[Coord]:
    """Return immediate `'#'` cells on the perimeter or 4-adjacent to exterior void."""
    rows = _extract_rows(grid_or_rows)
    ext_void = _flood_exterior_void(rows, strict_closure=False)
    walls: set[Coord] = set()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if rows[y][x] != TILE_WALL:
                continue
            if x in (0, WIDTH - 1) or y in (0, HEIGHT - 1):
                walls.add((x, y))
                continue
            if any((x + dx, y + dy) in ext_void for dx, dy in DIRECTIONS):
                walls.add((x, y))
    return frozenset(walls)


def outer_shell(grid_or_rows: Grid | Sequence[str]) -> frozenset[Coord]:
    """Return all `'#'` cells connected to the outer border or exterior void.

    Uses a single canonical flood-fill with enqueue-time visited tracking.
    """
    rows = _extract_rows(grid_or_rows)
    seeds = boundary_walls(rows)
    shell: set[Coord] = set(seeds)
    queue: deque[Coord] = deque(seeds)

    while queue:
        x, y = queue.popleft()
        for dx, dy in DIRECTIONS:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
                continue
            coord = (nx, ny)
            if coord in shell or rows[ny][nx] != TILE_WALL:
                continue
            shell.add(coord)
            queue.append(coord)

    return frozenset(shell)


def diff_grids(before: Grid | Sequence[str], after: Grid | Sequence[str]) -> list[TileChange]:
    """Return row-major list of cell differences between two 12x10 grids."""
    before_rows = _extract_rows(before)
    after_rows = _extract_rows(after)
    return [
        TileChange(x=x, y=y, before=before_tile, after=after_tile)
        for y, (left, right) in enumerate(zip(before_rows, after_rows, strict=True))
        for x, (before_tile, after_tile) in enumerate(zip(left, right, strict=True))
        if before_tile != after_tile
    ]


def _parse_operation(raw_op: Mapping[str, Any] | GridOperation) -> GridOperation:
    if isinstance(raw_op, GridOperation):
        return raw_op
    if not isinstance(raw_op, Mapping):
        raise GridValidationError(
            "INVALID_OPERATION",
            "Operation must be a mapping or GridOperation.",
        )

    allowed_keys = {"row", "column", "to", "from", "op"}
    unknown = set(raw_op) - allowed_keys
    if unknown:
        raise GridValidationError(
            "INVALID_OPERATION",
            f"Operation contains unsupported keys: {sorted(unknown)}.",
        )
    if "row" not in raw_op or "column" not in raw_op:
        raise GridValidationError("INVALID_OPERATION", "Operation requires 'row' and 'column'.")

    row = raw_op["row"]
    column = raw_op["column"]
    op_name = raw_op.get("op")
    to_tile = raw_op.get("to")
    expected_from = raw_op.get("from")

    if op_name is not None:
        if not isinstance(op_name, str) or op_name not in AI_ALLOWED_OPERATIONS:
            raise GridValidationError(
                "FORBIDDEN_OPERATION",
                f"Operation {op_name!r} is not allowed; only wall/water edits are permitted.",
            )
        op_before, op_after = _OP_TO_TRANSITION[op_name]
        if to_tile is None:
            to_tile = op_after
        elif to_tile != op_after:
            raise GridValidationError(
                "CONFLICTING_OPERATION",
                f"Operation {op_name!r} requires target tile {op_after!r}, got {to_tile!r}.",
            )
        if expected_from is not None and expected_from != op_before:
            raise GridValidationError(
                "CONFLICTING_OPERATION",
                f"Operation {op_name!r} requires source tile {op_before!r}, got {expected_from!r}.",
            )
        expected_from = expected_from if expected_from is not None else op_before

    if not isinstance(to_tile, str) or len(to_tile) != 1:
        raise GridValidationError(
            "INVALID_OPERATION",
            "Operation requires a single-character 'to' tile.",
        )

    return GridOperation(
        row=row,
        column=column,
        to=to_tile,
        op=op_name,
        expected_from=expected_from,
    )


def apply_operations(
    grid: Grid | Sequence[str],
    ops: Sequence[Mapping[str, Any] | GridOperation],
    *,
    allow_water_wall_swap: bool = False,
    validate_result: bool = True,
) -> Grid:
    """Apply AI-safe terrain edits (`add_wall`, `remove_wall`, `add_water`, `remove_water`).

    Entities (`p`, `s`, `t`), void (` `), and the connected `outer_shell` are
    strictly immutable and cannot be added, removed, or relocated.
    """
    base = parse_structural_grid(grid)
    if not isinstance(ops, (list, tuple)) or not ops:
        raise GridValidationError("EMPTY_OPERATIONS", "At least one operation is required.")

    shell = base.outer_shell()
    mutable = [list(row) for row in base.rows]
    seen: dict[Coord, str] = {}
    changed = 0

    for raw_op in ops:
        op = _parse_operation(raw_op)
        if (
            isinstance(op.row, bool)
            or isinstance(op.column, bool)
            or not isinstance(op.row, int)
            or not isinstance(op.column, int)
        ):
            raise GridValidationError(
                "INVALID_COORDINATE",
                "Operation row and column must be ints.",
            )

        x = op.column - 1
        y = op.row - 1
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            raise GridValidationError(
                "OUT_OF_BOUNDS",
                f"Coordinate row {op.row}, column {op.column} is outside {HEIGHT}x{WIDTH}.",
            )

        before = base.rows[y][x]
        after = op.to
        if op.expected_from is not None and op.expected_from != before:
            raise GridValidationError(
                "SOURCE_TILE_MISMATCH",
                f"Row {op.row}, column {op.column} has {before!r}, expected {op.expected_from!r}.",
            )

        if before == after:
            if (x, y) in seen and seen[(x, y)] != after:
                raise GridValidationError(
                    "CONFLICTING_OPERATION",
                    "Duplicate coordinates conflict.",
                )
            seen[(x, y)] = after
            continue

        if (x, y) in shell:
            raise GridValidationError(
                "PROTECTED_OUTER_SHELL",
                "The connected outer shell cannot be modified.",
                {"row": op.row, "column": op.column},
            )

        if before not in AI_EDITABLE_TILES or after not in AI_EDITABLE_TILES:
            raise GridValidationError(
                "PROTECTED_ENTITY_OR_VOID",
                "Only internal walls (#), water (@), and floor (.) may be modified.",
                {"row": op.row, "column": op.column, "before": before, "after": after},
            )

        transition = (before, after)
        is_basic = transition in _TRANSITION_TO_OP
        is_swap = transition in {("@", "#"), ("#", "@")}
        if not is_basic and not (allow_water_wall_swap and is_swap):
            raise GridValidationError(
                "FORBIDDEN_TRANSITION",
                f"Tile transition {before!r} -> {after!r} is not permitted.",
                {"row": op.row, "column": op.column, "before": before, "after": after},
            )

        if (x, y) in seen:
            if seen[(x, y)] == after:
                continue
            raise GridValidationError("CONFLICTING_OPERATION", "Duplicate coordinates conflict.")

        seen[(x, y)] = after
        mutable[y][x] = after
        changed += 1

    if changed == 0:
        raise GridValidationError("NO_TILE_CHANGES", "Operations produced no actual tile changes.")

    candidate_rows = tuple("".join(row) for row in mutable)
    return parse_grid(candidate_rows, validate_playable=validate_result)


def replay_solution(grid: Grid | Sequence[str], moves: str) -> ReplayResult:
    """Replay a U/D/L/R move sequence deterministically on a valid Sokoban grid."""
    if not isinstance(moves, str):
        raise GridValidationError("INVALID_MOVES", "Moves must be a string of U, D, L, R.")

    parsed = parse_grid(grid, validate_playable=True)
    walkable = {
        (x, y)
        for y, row in enumerate(parsed.rows)
        for x, tile in enumerate(row)
        if tile in WALKABLE_TILES
    }
    player = parsed.find_tile(TILE_PLAYER)
    boxes = set(parsed.find_tiles(TILE_BOX))
    targets = frozenset(parsed.find_tiles(TILE_TARGET))
    pushes = 0

    for step_index, move in enumerate(moves, start=1):
        delta = MOVE_DELTAS.get(move)
        if delta is None:
            raise GridValidationError(
                "INVALID_MOVE_CHAR",
                f"Invalid move character {move!r} at step {step_index}.",
                {"step": step_index, "move": move},
            )
        dx, dy = delta
        next_player = (player[0] + dx, player[1] + dy)
        if next_player not in walkable:
            raise GridValidationError(
                "ILLEGAL_MOVE",
                f"Move {move!r} at step {step_index} hits a non-walkable tile.",
                {"step": step_index, "move": move},
            )
        if next_player in boxes:
            next_box = (next_player[0] + dx, next_player[1] + dy)
            if next_box not in walkable or next_box in boxes:
                raise GridValidationError(
                    "ILLEGAL_PUSH",
                    f"Push {move!r} at step {step_index} is blocked.",
                    {"step": step_index, "move": move},
                )
            boxes.remove(next_player)
            boxes.add(next_box)
            pushes += 1
        player = next_player

    return ReplayResult(
        solved=(frozenset(boxes) == targets),
        steps=len(moves),
        pushes=pushes,
        player=player,
        boxes=tuple(sorted(boxes)),
        trace=moves,
    )


def map_fingerprint(rows: Grid | Sequence[str]) -> str:
    """Return the canonical SHA-256 hex digest for a Stage grid."""
    row_list = list(rows.rows) if isinstance(rows, Grid) else list(rows or [])
    canonical = json.dumps(row_list, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
