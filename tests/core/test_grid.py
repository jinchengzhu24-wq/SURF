"""Tests for server.core.grid."""

from __future__ import annotations

import hashlib
import json

import pytest
from server.core.grid import (
    HEIGHT,
    WIDTH,
    Grid,
    GridOperation,
    GridValidationError,
    apply_operations,
    boundary_walls,
    diff_grids,
    exterior_void,
    map_fingerprint,
    outer_shell,
    parse_grid,
    parse_structural_grid,
    replay_solution,
)

BASE_ROWS = (
    "  ########  ",
    " ##.#....## ",
    " #..#.....# ",
    "##........##",
    "#..p..s.t..#",
    "#....#.....#",
    "##...@@...##",
    " #...@@...# ",
    " ##......## ",
    "  ########  ",
)


def test_parse_grid_and_accessors() -> None:
    grid = parse_grid(BASE_ROWS)
    assert isinstance(grid, Grid)
    assert grid.width == WIDTH
    assert grid.height == HEIGHT
    assert grid.to_rows() == list(BASE_ROWS)
    assert grid.find_tile("p") == (3, 4)
    assert grid.find_tiles("s") == ((6, 4),)
    assert grid.find_tiles("t") == ((8, 4),)
    assert grid.tile_at(0, 0) == " "
    assert grid.tile_at(-1, 0) == " "
    assert grid.tile_at(5, 6) == "@"


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (lambda r: r[:-1], "INVALID_HEIGHT"),
        (lambda r: [*r[:-1], "###"], "INVALID_WIDTH"),
        (lambda r: [r[0], f" ##~{r[1][4:]}", *r[2:]], "UNKNOWN_TILE"),
        (lambda r: [row.replace("p", ".") for row in r], "INVALID_PLAYER_COUNT"),
        (lambda r: [row.replace("s", ".") for row in r], "INVALID_BOX_COUNT"),
        (
            lambda r: [row.replace("t", "s") for row in r],
            "MISMATCHED_TARGET_COUNT",
        ),
        (
            lambda r: [f".{r[0][1:]}", *r[1:]],
            "OPEN_OUTER_WALL",
        ),
        (
            lambda r: [r[0], r[1].replace(".", " ", 1), *r[2:]],
            "OPEN_OUTER_WALL",
        ),
        (
            lambda r: [f"  @{r[0][3:]}", *r[1:]],
            "OPEN_OUTER_WALL",
        ),
    ],
)
def test_parse_grid_validation_errors(mutator, expected_code: str) -> None:
    bad_rows = mutator(list(BASE_ROWS))
    with pytest.raises(GridValidationError) as exc_info:
        parse_grid(bad_rows)
    assert exc_info.value.code == expected_code


def test_parse_structural_grid_allows_sketches() -> None:
    sketch = [
        "############",
        "#          #",
        "#          #",
        "#          #",
        "#          #",
        "#          #",
        "#          #",
        "#          #",
        "#          #",
        "############",
    ]
    grid = parse_structural_grid(sketch)
    assert grid.rows == tuple(sketch)


def test_outer_shell_vs_boundary_walls_and_exterior_void() -> None:
    grid = parse_grid(BASE_ROWS)
    ext = exterior_void(grid)
    b_walls = boundary_walls(grid)
    shell = outer_shell(grid)

    # Corner exterior voids are in ext; interior floor is not.
    assert (0, 0) in ext
    assert (11, 9) in ext
    assert (5, 4) not in ext

    # Outer rim '#' cells are in both boundary_walls and outer_shell.
    assert (2, 0) in b_walls
    assert (2, 0) in shell

    # Attached internal wall spur at (4, 1) and (4, 2): connected to border (4, 0),
    # so it belongs to outer_shell but NOT boundary_walls (does not touch void/edge).
    assert (4, 1) in shell and (4, 1) not in b_walls
    assert (4, 2) in shell and (4, 2) not in b_walls

    # Disconnected internal wall island at (5, 5): in neither boundary_walls nor outer_shell.
    assert (5, 5) not in b_walls
    assert (5, 5) not in shell


def test_diff_grids_and_map_fingerprint() -> None:
    before = parse_grid(BASE_ROWS)
    after = apply_operations(before, [{"op": "add_wall", "row": 6, "column": 5}])
    changes = diff_grids(before, after)
    assert len(changes) == 1
    assert changes[0].x == 4
    assert changes[0].y == 5
    assert changes[0].row == 6
    assert changes[0].column == 5
    assert changes[0].before == "."
    assert changes[0].after == "#"

    expected = hashlib.sha256(
        json.dumps(list(BASE_ROWS), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert map_fingerprint(BASE_ROWS) == expected
    assert before.fingerprint() == expected


def test_apply_operations_allows_only_wall_and_water_edits() -> None:
    grid = parse_grid(BASE_ROWS)

    # Valid add_wall, remove_wall (island at (5,5) -> row 6, col 6), add_water, remove_water
    updated = apply_operations(
        grid,
        [
            {"op": "add_wall", "row": 6, "column": 5},
            {"op": "remove_wall", "row": 6, "column": 6},
            {"op": "add_water", "row": 5, "column": 6},
            GridOperation(row=7, column=6, to="."),
        ],
    )
    assert updated.tile_at(4, 5) == "#"
    assert updated.tile_at(5, 5) == "."
    assert updated.tile_at(5, 4) == "@"
    assert updated.tile_at(5, 6) == "."


@pytest.mark.parametrize(
    ("ops", "expected_code"),
    [
        ([], "EMPTY_OPERATIONS"),
        ([{"row": 5, "column": 4, "to": "."}], "PROTECTED_ENTITY_OR_VOID"),
        ([{"row": 5, "column": 5, "to": "p"}], "PROTECTED_ENTITY_OR_VOID"),
        ([{"row": 5, "column": 7, "to": "."}], "PROTECTED_ENTITY_OR_VOID"),
        ([{"row": 5, "column": 9, "to": "."}], "PROTECTED_ENTITY_OR_VOID"),
        ([{"row": 1, "column": 1, "to": "#"}], "PROTECTED_ENTITY_OR_VOID"),
        ([{"row": 1, "column": 3, "to": "."}], "PROTECTED_OUTER_SHELL"),
        # Attached spur (4, 2) -> row 3, col 5 is protected as part of outer_shell
        ([{"op": "remove_wall", "row": 3, "column": 5}], "PROTECTED_OUTER_SHELL"),
        ([{"op": "move_player", "row": 5, "column": 5}], "FORBIDDEN_OPERATION"),
        ([{"row": 7, "column": 6, "to": "#"}], "FORBIDDEN_TRANSITION"),
        ([{"row": True, "column": 5, "to": "#"}], "INVALID_COORDINATE"),
        ([{"row": 99, "column": 5, "to": "#"}], "OUT_OF_BOUNDS"),
        ([{"row": 6, "column": 5, "from": "#", "to": "."}], "SOURCE_TILE_MISMATCH"),
        (
            [
                {"row": 6, "column": 5, "to": "#"},
                {"row": 6, "column": 5, "to": "@"},
            ],
            "CONFLICTING_OPERATION",
        ),
        ([{"row": 6, "column": 5, "to": "."}], "NO_TILE_CHANGES"),
    ],
)
def test_apply_operations_rejects_disallowed_edits(ops, expected_code: str) -> None:
    grid = parse_grid(BASE_ROWS)
    with pytest.raises(GridValidationError) as exc_info:
        apply_operations(grid, ops)
    assert exc_info.value.code == expected_code


def test_replay_solution_success_and_errors() -> None:
    grid = parse_grid(BASE_ROWS)
    # Player at (3,4), box at (6,4), target at (8,4)
    res = replay_solution(grid, "RRRR")
    assert res.solved is True
    assert res.steps == 4
    assert res.pushes == 2
    assert res.player == (7, 4)
    assert res.boxes == ((8, 4),)

    partial = replay_solution(grid, "RRR")
    assert partial.solved is False
    assert partial.pushes == 1

    with pytest.raises(GridValidationError) as bad_char:
        replay_solution(grid, "RX")
    assert bad_char.value.code == "INVALID_MOVE_CHAR"

    with pytest.raises(GridValidationError) as wall_hit:
        replay_solution(grid, "LLLL")
    assert wall_hit.value.code == "ILLEGAL_MOVE"

    # Push box into water at (6,6): move to (6,3), push down twice -> ILLEGAL_PUSH
    with pytest.raises(GridValidationError) as push_blocked:
        replay_solution(grid, "RRURDD")
    assert push_blocked.value.code == "ILLEGAL_PUSH"
