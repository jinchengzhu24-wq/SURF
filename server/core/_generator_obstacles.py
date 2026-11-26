"""Geometry, wall obstacle, and water placement helpers for Sokoban level generation."""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Sequence

from ._generator_templates import (
    OUTER_SHELL_TEMPLATES,
    WALL_OBSTACLE_SHAPES,
    StructureTemplate,
)
from .grid import DIRECTIONS, HEIGHT, WIDTH, Coord

MIN_WATER_AREAS = 1
MAX_WATER_AREAS = 2
MIN_WATER_SIZE = 2
MAX_WATER_SIZE = 4
MIN_WALL_OBSTACLE_BLOCKS = 1
MAX_WALL_OBSTACLE_BLOCKS = 3


def _inside(x: int, y: int) -> bool:
    return 0 <= x < WIDTH and 0 <= y < HEIGHT


def _manhattan(first: Coord, second: Coord) -> int:
    return abs(first[0] - second[0]) + abs(first[1] - second[1])


def _nearest_distance(point: Coord, positions: Sequence[Coord]) -> int:
    return min((_manhattan(point, other) for other in positions), default=999)


def _tile_at(grid: Sequence[Sequence[str]], x: int, y: int) -> str:
    if not _inside(x, y):
        return " "
    return grid[y][x]


def _is_wall_at(grid: Sequence[Sequence[str]], x: int, y: int) -> bool:
    return _tile_at(grid, x, y) == "#"


def _is_ground_at(grid: Sequence[Sequence[str]], position: Coord) -> bool:
    x, y = position
    return _inside(x, y) and grid[y][x] == "."


def _ground_cells(grid: Sequence[Sequence[str]]) -> list[Coord]:
    return [
        (x, y)
        for y, row in enumerate(grid)
        for x, tile in enumerate(row)
        if tile == "."
    ]


def _ground_cells_connected(grid: Sequence[Sequence[str]]) -> bool:
    cells = set(_ground_cells(grid))
    if not cells:
        return False
    start = next(iter(cells))
    pending: deque[Coord] = deque([start])
    visited = {start}
    while pending:
        x, y = pending.popleft()
        for dx, dy in DIRECTIONS:
            next_cell = (x + dx, y + dy)
            if next_cell in cells and next_cell not in visited:
                visited.add(next_cell)
                pending.append(next_cell)
    return visited == cells


def _can_reach(
    grid: Sequence[Sequence[str]],
    start: Coord,
    destination: Coord,
    boxes: Sequence[Coord],
) -> bool:
    if start == destination:
        return True
    if not _is_ground_at(grid, start) or not _is_ground_at(grid, destination):
        return False
    blocked = set(boxes)
    queue: deque[Coord] = deque([start])
    visited = {start}
    while queue:
        cx, cy = queue.popleft()
        for dx, dy in DIRECTIONS:
            next_position = (cx + dx, cy + dy)
            if next_position in visited or next_position in blocked:
                continue
            if not _is_ground_at(grid, next_position):
                continue
            if next_position == destination:
                return True
            visited.add(next_position)
            queue.append(next_position)
    return False


def _oriented_template(
    template: StructureTemplate,
    rng: random.Random,
) -> tuple[tuple[str, ...], StructureTemplate]:
    rows = OUTER_SHELL_TEMPLATES[template.outer_shell_index]
    obstacle_anchors = list(template.obstacle_anchors)
    target_anchors = list(template.target_anchors)
    water_anchors = list(template.water_anchors)

    if rng.random() < 0.5:
        rows = tuple(row[::-1] for row in rows)
        obstacle_anchors = [(WIDTH - 1 - x, y) for x, y in obstacle_anchors]
        target_anchors = [(WIDTH - 1 - x, y) for x, y in target_anchors]
        water_anchors = [(WIDTH - 1 - x, y) for x, y in water_anchors]
    if rng.random() < 0.35:
        rows = tuple(reversed(rows))
        obstacle_anchors = [(x, HEIGHT - 1 - y) for x, y in obstacle_anchors]
        target_anchors = [(x, HEIGHT - 1 - y) for x, y in target_anchors]
        water_anchors = [(x, HEIGHT - 1 - y) for x, y in water_anchors]

    return rows, StructureTemplate(
        template.archetype,
        template.outer_shell_index,
        tuple(obstacle_anchors),
        template.wall_shape_indices,
        tuple(target_anchors),
        tuple(water_anchors),
    )


def _wall_origins(
    template: StructureTemplate,
    shape: Sequence[str],
    rng: random.Random,
) -> list[Coord]:
    height = len(shape)
    width = max(len(row) for row in shape)
    origins: list[Coord] = []
    seen: set[Coord] = set()
    for anchor_x, anchor_y in template.obstacle_anchors:
        for y_offset in (-1, 0, 1):
            for x_offset in (-1, 0, 1):
                origin = (
                    anchor_x - width // 2 + x_offset,
                    anchor_y - height // 2 + y_offset,
                )
                if origin not in seen:
                    seen.add(origin)
                    origins.append(origin)
    for y in range(2, HEIGHT - height - 1):
        for x in range(2, WIDTH - width - 1):
            origin = (x, y)
            if origin not in seen:
                seen.add(origin)
                origins.append(origin)
    rng.shuffle(origins)
    return origins


def _can_place_wall_shape(
    grid: list[list[str]],
    origin: Coord,
    shape: Sequence[str],
) -> bool:
    origin_x, origin_y = origin
    shape_height = len(shape)
    shape_width = max(len(row) for row in shape)
    for y in range(origin_y - 1, origin_y + shape_height + 1):
        for x in range(origin_x - 1, origin_x + shape_width + 1):
            if not _inside(x, y) or grid[y][x] != ".":
                return False
    return True


def _set_wall_shape(
    grid: list[list[str]],
    origin: Coord,
    shape: Sequence[str],
    tile: str,
) -> None:
    origin_x, origin_y = origin
    for y_offset, row in enumerate(shape):
        for x_offset, value in enumerate(row):
            if value == "#":
                grid[origin_y + y_offset][origin_x + x_offset] = tile


def _wall_origin_score(
    origin: Coord,
    shape: Sequence[str],
    style: str,
    template: StructureTemplate,
    target_layout: str,
) -> int:
    width = max(len(row) for row in shape)
    height = len(shape)
    center = (origin[0] + width // 2, origin[1] + height // 2)
    map_center = (WIDTH // 2, HEIGHT // 2)
    if style == "side_choke":
        score = 120 - min(center[0] - 1, WIDTH - 2 - center[0]) * 10
    elif style == "goal_guard":
        score = 120 - min(
            center[0] - 1,
            WIDTH - 2 - center[0],
            center[1] - 1,
            HEIGHT - 2 - center[1],
        ) * 8
    else:
        score = 120 - _manhattan(center, map_center) * 8
    score += 80 - _nearest_distance(center, template.obstacle_anchors) * 8
    if target_layout == "split_pair":
        score += abs(center[0] - map_center[0]) * 2
    return score


def _allowed_outer_shell_join(
    grid: Sequence[Sequence[str]],
    start_x: int,
    start_y: int,
    length: int,
) -> bool:
    if length != 2:
        return False
    for y in (start_y, start_y + 1):
        for x in range(start_x, start_x + length):
            for dx, dy in DIRECTIONS:
                nx, ny = x + dx, y + dy
                if not _inside(nx, ny) or grid[ny][nx] == " ":
                    return True
    return False


def _validate_wall_rules(grid: Sequence[Sequence[str]]) -> bool:
    for y, row in enumerate(grid):
        for x, tile in enumerate(row):
            if tile != "#":
                continue
            up = _is_wall_at(grid, x, y - 1)
            down = _is_wall_at(grid, x, y + 1)
            left = _is_wall_at(grid, x - 1, y)
            right = _is_wall_at(grid, x + 1, y)
            right_down = _is_wall_at(grid, x + 1, y + 1)
            surrounded = (
                all(
                    _tile_at(grid, x + dx, y + dy) != " "
                    for dx in (-1, 0, 1)
                    for dy in (-1, 0, 1)
                    if (dx, dy) != (0, 0)
                )
                and _tile_at(grid, x, y + 1) != "@"
            )
            if not (
                surrounded
                or (up and (left or right))
                or (right and right_down)
                or ((up and down) or (down and (left or right)))
                or (left and right)
                or sum((up, down, left, right)) == 1
            ):
                return False
    for y in range(HEIGHT - 1):
        run_start: int | None = None
        run_length = 0
        for x in range(WIDTH):
            parallel = grid[y][x] == "#" and grid[y + 1][x] == "#"
            if parallel:
                run_start = x if run_start is None else run_start
                run_length += 1
            else:
                if (
                    run_length >= 2
                    and run_start is not None
                    and not _allowed_outer_shell_join(grid, run_start, y, run_length)
                ):
                    return False
                run_start = None
                run_length = 0
        if (
            run_length >= 2
            and run_start is not None
            and not _allowed_outer_shell_join(grid, run_start, y, run_length)
        ):
            return False
    return True


def _validate_tile_rules(grid: Sequence[Sequence[str]]) -> bool:
    for y, row in enumerate(grid):
        for x, tile in enumerate(row):
            if tile == "@" and y > 0 and grid[y - 1][x] == "#":
                return False
    return _validate_wall_rules(grid)


def _add_wall_obstacle_blocks(
    grid: list[list[str]],
    rng: random.Random,
    template: StructureTemplate,
    style: str,
    target_layout: str,
) -> bool:
    count = rng.randint(MIN_WALL_OBSTACLE_BLOCKS, MAX_WALL_OBSTACLE_BLOCKS)
    placed = 0
    shape_indices = list(template.wall_shape_indices) + list(range(len(WALL_OBSTACLE_SHAPES)))
    rng.shuffle(shape_indices)
    for _ in range(count):
        placed_this = False
        for shape_index in shape_indices:
            shape = WALL_OBSTACLE_SHAPES[shape_index % len(WALL_OBSTACLE_SHAPES)]
            origins = _wall_origins(template, shape, rng)
            origins.sort(
                key=lambda origin: _wall_origin_score(
                    origin, shape, style, template, target_layout
                ),
                reverse=True,
            )
            for origin in origins:
                if not _can_place_wall_shape(grid, origin, shape):
                    continue
                _set_wall_shape(grid, origin, shape, "#")
                if _ground_cells_connected(grid) and _validate_wall_rules(grid):
                    placed_this = True
                    break
                _set_wall_shape(grid, origin, shape, ".")
            if placed_this:
                break
        if not placed_this:
            return False
        placed += 1
    return placed == count


def _can_place_water_rect(
    grid: Sequence[Sequence[str]],
    origin: Coord,
    width: int,
    height: int,
) -> bool:
    x, y = origin
    if x < 1 or y < 2 or x + width > WIDTH - 1 or y + height > HEIGHT - 1:
        return False
    for yy in range(y, y + height):
        for xx in range(x, x + width):
            if grid[yy][xx] != ".":
                return False
            if yy > 0 and grid[yy - 1][xx] == "#":
                return False
            for dx, dy in DIRECTIONS:
                nx, ny = xx + dx, yy + dy
                if (
                    _inside(nx, ny)
                    and not (x <= nx < x + width and y <= ny < y + height)
                    and grid[ny][nx] == "@"
                ):
                    return False
    return True


def _water_origin_score(
    origin: Coord,
    width: int,
    height: int,
    template: StructureTemplate,
    style: str,
) -> int:
    center = (origin[0] + width // 2, origin[1] + height // 2)
    map_center = (WIDTH // 2, HEIGHT // 2)
    if style == "corner_pool":
        score = 100 - min(
            _manhattan(center, (1, 1)),
            _manhattan(center, (WIDTH - 2, 1)),
            _manhattan(center, (1, HEIGHT - 2)),
            _manhattan(center, (WIDTH - 2, HEIGHT - 2)),
        ) * 8
    elif style == "route_divider":
        score = 100 - _manhattan(center, map_center) * 6
    else:
        score = 100 - min(center[0] - 1, WIDTH - 2 - center[0]) * 8
    return score + 80 - _nearest_distance(center, template.water_anchors) * 8


def _add_water_areas(
    grid: list[list[str]],
    rng: random.Random,
    template: StructureTemplate,
    style: str,
) -> bool:
    area_count = rng.randint(MIN_WATER_AREAS, MAX_WATER_AREAS)
    placed = 0
    attempts = 0
    while placed < area_count and attempts < 80:
        attempts += 1
        width = rng.randint(MIN_WATER_SIZE, MAX_WATER_SIZE)
        height = rng.randint(MIN_WATER_SIZE, MAX_WATER_SIZE)
        origins = [
            (x, y)
            for y in range(2, HEIGHT - height - 1)
            for x in range(2, WIDTH - width - 1)
            if _can_place_water_rect(grid, (x, y), width, height)
        ]
        rng.shuffle(origins)
        origins.sort(
            key=lambda origin: _water_origin_score(origin, width, height, template, style),
            reverse=True,
        )
        committed = False
        for x, y in origins:
            if not _can_place_water_rect(grid, (x, y), width, height):
                continue
            for yy in range(y, y + height):
                for xx in range(x, x + width):
                    grid[yy][xx] = "@"
            if _ground_cells_connected(grid):
                committed = True
                break
            for yy in range(y, y + height):
                for xx in range(x, x + width):
                    grid[yy][xx] = "."
        if committed:
            placed += 1
    return placed == area_count and _ground_cells_connected(grid)
