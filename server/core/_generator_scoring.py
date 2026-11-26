"""Entity placement, reverse-pull generation, and quality scoring helpers."""

from __future__ import annotations

import random
from collections.abc import Sequence

from ._generator_obstacles import (
    _can_reach,
    _ground_cells,
    _inside,
    _is_ground_at,
    _manhattan,
    _nearest_distance,
    _tile_at,
)
from ._generator_templates import StructureTemplate
from .grid import DIRECTIONS, HEIGHT, WIDTH, Coord
from .solver import SolveResult

BOX_COUNT = 2
MAX_REVERSE_PULL_ATTEMPTS = 200


def _near_playable_edge(position: Coord) -> bool:
    x, y = position
    return x <= 2 or x >= WIDTH - 3 or y <= 2 or y >= HEIGHT - 3


def _place_targets(
    grid: Sequence[Sequence[str]],
    rng: random.Random,
    template: StructureTemplate,
    layout: str,
) -> tuple[Coord, ...] | None:
    candidates = _ground_cells(grid)
    if len(candidates) < BOX_COUNT:
        return None
    rng.shuffle(candidates)

    if layout == "clustered":
        for first in candidates:
            pairs = [
                second
                for second in candidates
                if second != first and _manhattan(first, second) <= 2
            ]
            rng.shuffle(pairs)
            if pairs:
                return (first, pairs[0])
    elif layout == "edge_cluster":
        edge = [cell for cell in candidates if _near_playable_edge(cell)]
        rng.shuffle(edge)
        for first in edge:
            pairs = [
                second
                for second in edge
                if second != first and _manhattan(first, second) <= 3
            ]
            rng.shuffle(pairs)
            if pairs:
                return (first, pairs[0])
    else:
        for first in candidates:
            farthest = max(
                (cell for cell in candidates if cell != first),
                key=lambda cell: _manhattan(first, cell),
                default=None,
            )
            if farthest is not None and _manhattan(first, farthest) >= max(4, WIDTH // 3):
                return (first, farthest)

    selected: list[Coord] = []
    for anchor in template.target_anchors:
        nearby = sorted(
            (cell for cell in candidates if cell not in selected),
            key=lambda cell: _manhattan(cell, anchor),
        )
        if nearby and _manhattan(nearby[0], anchor) <= 4:
            selected.append(nearby[0])
        if len(selected) == BOX_COUNT:
            return tuple(selected)
    if len(selected) < BOX_COUNT:
        return tuple(candidates[:BOX_COUNT])
    return tuple(selected)


def _place_fallback_targets(
    grid: Sequence[Sequence[str]],
    rng: random.Random,
) -> tuple[Coord, ...] | None:
    candidates = _ground_cells(grid)
    pairs = [
        (first, second)
        for first in candidates
        for second in candidates
        if first != second and _manhattan(first, second) >= max(4, WIDTH // 3)
    ]
    if not pairs:
        return None
    return rng.choice(pairs)


def _place_player(
    grid: Sequence[Sequence[str]],
    rng: random.Random,
    targets: Sequence[Coord],
    template: StructureTemplate | None,
) -> Coord | None:
    candidates = [cell for cell in _ground_cells(grid) if cell not in targets]
    if not candidates:
        return None
    rng.shuffle(candidates)
    return max(
        candidates,
        key=lambda cell: (
            min(_manhattan(cell, target) for target in targets) * 4,
            _nearest_distance(cell, template.target_anchors) if template is not None else 0,
        ),
    )


def _reverse_pull_boxes(
    grid: Sequence[Sequence[str]],
    initial_boxes: Sequence[Coord],
    initial_player: Coord,
    rng: random.Random,
    target_pulls: int,
) -> tuple[list[Coord], Coord, int] | None:
    boxes = list(initial_boxes)
    targets = set(initial_boxes)
    player = initial_player
    moved = [False] * len(boxes)
    reverse_pulls = 0

    for _ in range(MAX_REVERSE_PULL_ATTEMPTS):
        if reverse_pulls >= target_pulls:
            break
        legal: list[tuple[int, Coord]] = []
        for index, box in enumerate(boxes):
            for direction in DIRECTIONS:
                next_box = (box[0] - direction[0], box[1] - direction[1])
                next_player = (box[0] - direction[0] * 2, box[1] - direction[1] * 2)
                if next_box in targets or next_player in targets:
                    continue
                if not _is_ground_at(grid, next_box) or not _is_ground_at(grid, next_player):
                    continue
                if any(
                    other != index and position in {next_box, next_player}
                    for other, position in enumerate(boxes)
                ):
                    continue
                if not _can_reach(grid, player, next_box, boxes):
                    continue
                legal.append((index, direction))
        if not legal:
            break
        index, direction = rng.choice(legal)
        box = boxes[index]
        boxes[index] = (box[0] - direction[0], box[1] - direction[1])
        player = (box[0] - direction[0] * 2, box[1] - direction[1] * 2)
        moved[index] = True
        reverse_pulls += 1

    if reverse_pulls < target_pulls or not all(moved):
        return None
    if any(box in targets for box in boxes) or player in targets:
        return None
    return boxes, player, reverse_pulls


def _has_adjacent_box_target_pair(
    boxes: Sequence[Coord],
    targets: Sequence[Coord],
) -> bool:
    return any(_manhattan(box, target) == 1 for box in boxes for target in targets)


def _build_rows(
    grid: Sequence[Sequence[str]],
    boxes: Sequence[Coord],
    targets: Sequence[Coord],
    player: Coord,
) -> tuple[str, ...]:
    rendered = [list(row) for row in grid]
    for x, y in targets:
        rendered[y][x] = "t"
    for x, y in boxes:
        rendered[y][x] = "s"
    px, py = player
    rendered[py][px] = "p"
    return tuple("".join(row) for row in rendered)


def _count_water_areas(rows: Sequence[str]) -> int:
    water = {
        (x, y)
        for y, row in enumerate(rows)
        for x, tile in enumerate(row)
        if tile == "@"
    }
    areas = 0
    while water:
        areas += 1
        pending = [water.pop()]
        while pending:
            x, y = pending.pop()
            for dx, dy in DIRECTIONS:
                cell = (x + dx, y + dy)
                if cell in water:
                    water.remove(cell)
                    pending.append(cell)
    return areas


def _near_corridor(position: Coord, start: Coord, end: Coord, width: int) -> bool:
    x, y = position
    min_x = min(start[0], end[0]) - width
    max_x = max(start[0], end[0]) + width
    min_y = min(start[1], end[1]) - width
    max_y = max(start[1], end[1]) + width
    near_horizontal = min_x <= x <= max_x and (
        abs(y - start[1]) <= width or abs(y - end[1]) <= width
    )
    near_vertical = min_y <= y <= max_y and (
        abs(x - start[0]) <= width or abs(x - end[0]) <= width
    )
    return near_horizontal or near_vertical


def _near_axis_aligned_corridor(
    position: Coord,
    boxes: Sequence[Coord],
    targets: Sequence[Coord],
) -> bool:
    return any(
        _near_corridor(position, box, target, 2)
        for box in boxes
        for target in targets
    )


def _is_choke_influence_wall(rows: Sequence[str], x: int, y: int) -> bool:
    left = _inside(x - 1, y) and rows[y][x - 1] in ".pst"
    right = _inside(x + 1, y) and rows[y][x + 1] in ".pst"
    up = _inside(x, y - 1) and rows[y - 1][x] in ".pst"
    down = _inside(x, y + 1) and rows[y + 1][x] in ".pst"
    return (left and right and not up and not down) or (
        up and down and not left and not right
    )


def _count_obstacle_influence(
    rows: Sequence[str],
    boxes: Sequence[Coord],
    targets: Sequence[Coord],
    player: Coord,
) -> int:
    influence = 0
    for y in range(1, HEIGHT - 1):
        for x in range(1, WIDTH - 1):
            if rows[y][x] != "#":
                continue
            score = 0
            nearest_target = _nearest_distance((x, y), targets)
            nearest_box = _nearest_distance((x, y), boxes)
            if nearest_target <= 2:
                score += 2
            elif nearest_target <= 3:
                score += 1
            if nearest_box <= 2:
                score += 2
            elif nearest_box <= 3:
                score += 1
            if _near_axis_aligned_corridor((x, y), boxes, targets):
                score += 2
            if _nearest_distance((x, y), [player]) <= 4:
                score += 1
            if _is_choke_influence_wall(rows, x, y):
                score += 1
            influence += min(score, 5)
    return influence


def _quality_score(
    validation: SolveResult,
    reverse_pulls: int,
    water_tiles: int,
    water_areas: int,
    obstacle_influence: int,
    targets: Sequence[Coord],
    rows: Sequence[str],
) -> int:
    score = validation.solution_steps * 3
    score += validation.solution_pushes * 10
    score += reverse_pulls * 2
    score += min(validation.searched_states // 100, 40)
    score += min(water_tiles, 12) * 3
    score += water_areas * 20
    surrounded_walls = sum(
        1
        for y in range(1, HEIGHT - 1)
        for x in range(1, WIDTH - 1)
        if rows[y][x] == "#"
        and all(
            _tile_at(rows, x + dx, y + dy) != " "
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (dx, dy) != (0, 0)
        )
    )
    score += min(surrounded_walls, 3) * 45
    score += min(obstacle_influence, 8) * 18
    target_distance = sum(
        _manhattan(targets[index], targets[other_index])
        for index in range(len(targets))
        for other_index in range(index + 1, len(targets))
    )
    score += target_distance * 6
    return score
