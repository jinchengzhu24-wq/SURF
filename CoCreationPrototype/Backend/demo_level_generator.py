"""Algorithm_Level-inspired deterministic generator for standalone 8010 demos.

The Unity Algorithm_Level scene is intentionally mirrored here instead of being
called remotely.  Templates provide the structural vocabulary, while every
candidate
is independently validated by the same Python Sokoban solver used by 8010.
"""

import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from level_validation import (
    HEIGHT,
    WIDTH,
    LevelValidationError,
    LevelValidationResult,
    validate_and_solve,
)


# These values mirror the defaults serialized in LevelData.prefab and used by
# Algorithm_Level.  The effective minimums are the algorithm quality gate.
BOX_COUNT = 2
MIN_WATER_AREAS = 1
MAX_WATER_AREAS = 2
MIN_WATER_SIZE = 2
MAX_WATER_SIZE = 4
MIN_WALL_OBSTACLE_BLOCKS = 1
MAX_WALL_OBSTACLE_BLOCKS = 3
MAX_GENERATION_ATTEMPTS = 300
MAX_REVERSE_PULL_ATTEMPTS = 200
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

DIRECTIONS = ((0, -1), (0, 1), (-1, 0), (1, 0))


@dataclass(frozen=True)
class StructureTemplate:
    archetype: str
    outer_shell_index: int
    obstacle_anchors: Tuple[Tuple[int, int], ...]
    wall_shape_indices: Tuple[int, ...]
    target_anchors: Tuple[Tuple[int, int], ...]
    water_anchors: Tuple[Tuple[int, int], ...]


@dataclass(frozen=True)
class GenerationCandidate:
    rows: Tuple[str, ...]
    validation: LevelValidationResult
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


@dataclass(frozen=True)
class DemoLevel:
    rows: Tuple[str, ...]
    validation: LevelValidationResult
    seed: int
    attempts: int
    generation_summary: Dict[str, object]


# Direct data port of LevelGenerationTemplates.cs.
WALL_OBSTACLE_SHAPES = [
    [
        "##",
        "#."
    ],
    [
        "##",
        "#.",
        "#."
    ],
    [
        "###",
        ".#."
    ],
    [
        "##.",
        ".##"
    ],
    [
        "#.#",
        "###"
    ],
    [
        "##.",
        "#..",
        "##."
    ],
    [
        "###",
        "#.."
    ],
    [
        "##",
        ".#",
        ".#"
    ],
    [
        "#"
    ],
    [
        "#.",
        ".#"
    ],
    [
        ".#",
        "#."
    ],
    [
        "##"
    ],
    [
        "#",
        "#"
    ],
    [
        "###"
    ],
    [
        "#",
        "#",
        "#"
    ],
    [
        "#.",
        "##"
    ],
    [
        ".#",
        "##"
    ],
    [
        "###",
        "..#"
    ],
    [
        "#..",
        "###"
    ]
]

OUTER_SHELL_TEMPLATES = [
    [
        "  ########  ",
        " ##......## ",
        " #........# ",
        "##........##",
        "#..........#",
        "#..........#",
        "##........##",
        " #........# ",
        " ##......## ",
        "  ########  "
    ],
    [
        " ###########",
        "##.........#",
        "#..........#",
        "#..........#",
        "#.........##",
        "#........## ",
        "##.......#  ",
        " ##......#  ",
        "  #......#  ",
        "  ########  "
    ],
    [
        "   #######  ",
        "  ##.....## ",
        " ##.......##",
        " #.........#",
        "##.........#",
        "#..........#",
        "#.........##",
        "##.......## ",
        " ##.....##  ",
        "  #######   "
    ],
    [
        " #########  ",
        " #.......#  ",
        "##.......#  ",
        "#........## ",
        "#.........# ",
        "##........# ",
        " #........##",
        " #.........#",
        " ##.......##",
        "  ######### "
    ],
    [
        "  ######### ",
        " ##.......# ",
        " #........# ",
        " #........##",
        "##.........#",
        "#..........#",
        "#.........##",
        "##........# ",
        " #.......## ",
        " #########  "
    ],
    [
        " ########   ",
        "##......##  ",
        "#........## ",
        "#.........# ",
        "##........##",
        " #.........#",
        " #.........#",
        " ##.......##",
        "  ##.....## ",
        "   #######  "
    ],
    [
        "  ########  ",
        " ##......## ",
        "##........# ",
        "#.........# ",
        "#........## ",
        "##.......#  ",
        " #.......## ",
        " #........# ",
        " ##......## ",
        "  ########  "
    ],
    [
        " #########  ",
        " #.......## ",
        "##........# ",
        "#.........##",
        "#..........#",
        "##.........#",
        " #........##",
        " #........# ",
        " ##.......# ",
        "  ######### "
    ],
    [
        "  ########  ",
        " ##......## ",
        " #........# ",
        " #.......## ",
        "##.......#  ",
        "#........## ",
        "#.........# ",
        "##........# ",
        " ##......## ",
        "  ########  "
    ],
    [
        "   #######  ",
        "  ##.....## ",
        " ##.......##",
        " #.........#",
        " #.........#",
        "##........##",
        "#.........# ",
        "#........## ",
        "##......##  ",
        " ########   "
    ]
]

STRUCTURE_TEMPLATES = (
    StructureTemplate("goal_room", 0, [[6,4],[6,5],[5,4],[7,5]], [1,6,7,8,9,11,17,18], [[8,4],[8,5],[9,3],[9,6]], [[2,6],[3,6],[2,4],[3,7]]),
    StructureTemplate("bottleneck_corridor", 1, [[4,4],[5,5],[6,4],[7,5]], [0,1,3,7,8,10,11,12,13,14], [[8,3],[8,6],[9,4],[4,7]], [[2,5],[3,6],[8,5]]),
    StructureTemplate("split_route", 2, [[5,4],[6,5],[4,5],[7,4]], [2,3,4,5,8,9,10,13,15,16,18], [[3,3],[8,6],[3,6],[8,3]], [[5,6],[6,6],[2,5],[9,5]]),
    StructureTemplate("open_workshop", 0, [[5,4],[6,4],[4,6],[7,6]], [2,3,6,8,9,11,12,15,16], [[4,4],[7,5],[3,6],[8,3]], [[2,6],[9,6]]),
    StructureTemplate("goal_room", 3, [[6,3],[7,4],[5,6],[8,6]], [0,2,6,8,9,10,11,17,18], [[8,3],[9,4],[8,6],[9,7]], [[2,5],[3,6],[4,7]]),
    StructureTemplate("bottleneck_corridor", 4, [[3,4],[4,5],[6,4],[7,5]], [1,3,5,7,8,10,11,12,13,14], [[8,2],[9,5],[7,7],[3,7]], [[2,4],[3,5],[8,6]]),
    StructureTemplate("split_route", 5, [[5,3],[6,4],[5,6],[7,6]], [2,3,4,5,8,9,10,13,15,16,18], [[2,3],[3,6],[8,3],[9,6]], [[5,5],[6,5],[7,6],[2,6]]),
    StructureTemplate("open_workshop", 8, [[4,3],[7,3],[5,5],[8,6]], [0,2,3,6,8,9,11,12,15,16], [[3,4],[4,6],[7,3],[8,6]], [[2,5],[8,5],[3,7],[9,6]]),
    StructureTemplate("goal_room", 2, [[5,4],[6,4],[6,5],[7,5]], [0,2,6,8,9,11,17,18], [[8,3],[9,4],[8,5],[9,6]], [[2,5],[3,6],[4,7]]),
    StructureTemplate("goal_room", 7, [[5,4],[6,4],[5,5],[6,6]], [1,3,5,7,8,10,11,17,18], [[2,3],[3,4],[2,6],[3,7]], [[8,3],[9,5],[8,6]]),
    StructureTemplate("bottleneck_corridor", 0, [[5,3],[6,3],[5,6],[6,6]], [1,2,4,7,8,10,11,12,13,14], [[3,4],[3,6],[8,4],[8,6]], [[2,4],[9,5],[5,7]]),
    StructureTemplate("bottleneck_corridor", 8, [[4,3],[5,4],[6,5],[7,6]], [0,1,3,5,8,9,11,12,13,14], [[8,2],[8,3],[3,7],[4,8]], [[3,4],[8,5],[6,7]]),
    StructureTemplate("split_route", 1, [[5,4],[6,4],[5,5],[6,6]], [2,3,4,5,8,9,10,13,15,16,18], [[2,2],[3,3],[8,6],[8,7]], [[3,6],[7,3],[4,7]]),
    StructureTemplate("split_route", 9, [[5,4],[6,4],[5,5],[6,5]], [0,2,3,4,6,8,10,13,15,16,18], [[4,2],[7,2],[4,7],[7,7]], [[2,4],[8,5],[6,6]]),
    StructureTemplate("open_workshop", 3, [[4,3],[7,3],[4,6],[7,6]], [0,2,3,6,8,9,11,12,15,16], [[3,4],[8,4],[3,7],[8,7]], [[5,5],[6,5],[2,6],[9,6]]),
    StructureTemplate("open_workshop", 0, [[3,4],[8,4],[4,6],[7,6]], [1,2,3,6,8,10,11,12,15,16], [[4,3],[7,3],[4,7],[7,7]], [[2,5],[9,5],[5,6],[6,4]]),
)


def generate_demo_level(seed=None, max_attempts=MAX_GENERATION_ATTEMPTS):
    """Generate a reproducible, validated two-box Algorithm_Level demo map."""
    actual_seed = (
        int(seed)
        if seed is not None
        else random.SystemRandom().randrange(0, 2**63)
    )
    rng = random.Random(actual_seed)
    attempt_limit = max(1, min(int(max_attempts), MAX_GENERATION_ATTEMPTS))
    selectable: List[GenerationCandidate] = []
    best_valid: Optional[GenerationCandidate] = None

    for attempt in range(1, attempt_limit + 1):
        template = rng.choice(STRUCTURE_TEMPLATES)
        candidate = _build_candidate(rng, template, include_optional_obstacles=True)
        if candidate is None:
            continue
        if best_valid is None or _candidate_key(candidate) > _candidate_key(best_valid):
            best_valid = candidate
        if _is_qualified(candidate):
            selectable.append(candidate)
            if len(selectable) >= ALGORITHM_CANDIDATE_SAMPLE_COUNT:
                selected = max(selectable, key=_candidate_key)
                return _as_demo_level(selected, actual_seed, attempt, "algorithm_level", len(selectable))
    
    if best_valid is not None:
        return _as_demo_level(
            best_valid,
            actual_seed,
            attempt_limit,
            "algorithm_level_relaxed",
            len(selectable),
        )

    # The fallback still starts boxes on targets and uses the same legal
    # reverse-pull constructor.  It only removes optional walls and water when
    # the full blueprint cannot fit a valid candidate.
    fallback = _build_fallback(rng, attempt_limit)
    if fallback is not None:
        return _as_demo_level(
            fallback,
            actual_seed,
            attempt_limit,
            "fallback",
            len(selectable),
        )
    raise RuntimeError("Unable to generate a validated Algorithm_Level demo level.")


def _as_demo_level(
    candidate: GenerationCandidate,
    seed: int,
    attempts: int,
    mode: str,
    selectable_count: int,
) -> DemoLevel:
    summary = {
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
        tuple(candidate.validation.rows),
        candidate.validation,
        seed,
        attempts,
        summary,
    )


def _build_candidate(
    rng: random.Random,
    template: StructureTemplate,
    include_optional_obstacles: bool,
) -> Optional[GenerationCandidate]:
    shell = _oriented_template(template, rng)
    grid = [list(row) for row in shell[0]]
    oriented = shell[1]

    target_layout = rng.choice(("clustered", "edge_cluster", "split_pair"))
    obstacle_style = rng.choice(("goal_guard", "side_choke", "central_block"))
    water_style = rng.choice(("corner_pool", "route_divider", "side_pool"))

    if include_optional_obstacles:
        if not _add_wall_obstacle_blocks(
            grid, rng, oriented, obstacle_style, target_layout
        ):
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
        list(targets),
        player,
        rng,
        rng.randint(PREFERRED_MIN_REVERSE_PULLS, MAX_TARGET_REVERSE_PULLS),
    )
    if reverse is None:
        return None
    boxes, player, reverse_pulls = reverse

    if _has_adjacent_box_target_pair(boxes, targets):
        return None
    if not _validate_tile_rules(grid):
        return None

    rows = _build_rows(grid, boxes, targets, player)
    validation = _try_validate(rows)
    if validation is None:
        return None

    wall_tiles = sum(row.count("#") for row in rows)
    water_tiles = sum(row.count("@") for row in rows)
    water_areas = _count_water_areas(rows)
    obstacle_influence = _count_obstacle_influence(
        rows, boxes, targets, player
    )
    quality_score = _quality_score(
        validation,
        reverse_pulls,
        water_tiles,
        water_areas,
        wall_tiles,
        obstacle_influence,
        boxes,
        targets,
        rows,
    )
    return GenerationCandidate(
        tuple(validation.rows),
        validation,
        reverse_pulls,
        quality_score,
        obstacle_influence,
        water_areas,
        water_tiles,
        wall_tiles,
        oriented.archetype,
        target_layout,
        obstacle_style,
        water_style,
    )


def _build_fallback(rng: random.Random, attempts: int) -> Optional[GenerationCandidate]:
    grid = [
        ["#" if x in (0, WIDTH - 1) or y in (0, HEIGHT - 1) else "."
         for x in range(WIDTH)]
        for y in range(HEIGHT)
    ]
    targets = _place_fallback_targets(grid, rng)
    if targets is None:
        return None
    player = _place_player(grid, rng, targets, None)
    if player is None:
        return None
    for _ in range(min(MAX_REVERSE_PULL_ATTEMPTS, max(40, attempts * 2))):
        reverse = _reverse_pull_boxes(
            grid,
            list(targets),
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
        validation = _try_validate(rows)
        if validation is None:
            continue
        water_tiles = 0
        wall_tiles = sum(row.count("#") for row in rows)
        obstacle_influence = _count_obstacle_influence(
            rows, boxes, targets, final_player
        )
        return GenerationCandidate(
            tuple(validation.rows),
            validation,
            reverse_pulls,
            _quality_score(
                validation, reverse_pulls, 0, 0, wall_tiles,
                obstacle_influence, boxes, targets, rows
            ),
            obstacle_influence,
            0,
            water_tiles,
            wall_tiles,
            "fallback",
            "split_pair",
            "none",
            "none",
        )
    return None


def _oriented_template(
    template: StructureTemplate,
    rng: random.Random,
) -> Tuple[Tuple[str, ...], StructureTemplate]:
    rows = tuple(OUTER_SHELL_TEMPLATES[template.outer_shell_index])
    anchors = {
        "obstacle": list(template.obstacle_anchors),
        "target": list(template.target_anchors),
        "water": list(template.water_anchors),
    }
    if rng.random() < 0.5:
        rows = tuple(row[::-1] for row in rows)
        for key in anchors:
            anchors[key] = [(WIDTH - 1 - x, y) for x, y in anchors[key]]
    if rng.random() < 0.35:
        rows = tuple(reversed(rows))
        for key in anchors:
            anchors[key] = [(x, HEIGHT - 1 - y) for x, y in anchors[key]]
    return rows, StructureTemplate(
        template.archetype,
        template.outer_shell_index,
        tuple(anchors["obstacle"]),
        template.wall_shape_indices,
        tuple(anchors["target"]),
        tuple(anchors["water"]),
    )


def _add_wall_obstacle_blocks(
    grid: List[List[str]],
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
                if (
                    _ground_cells_connected(grid)
                    and _validate_wall_rules(grid)
                ):
                    placed_this = True
                    break
                _set_wall_shape(grid, origin, shape, ".")
            if placed_this:
                break
        if not placed_this:
            return False
        placed += 1
    return placed == count


def _wall_origins(
    template: StructureTemplate,
    shape: Sequence[str],
    rng: random.Random,
) -> List[Tuple[int, int]]:
    height = len(shape)
    width = max(len(row) for row in shape)
    origins = []
    for anchor_x, anchor_y in template.obstacle_anchors:
        for y_offset in (-1, 0, 1):
            for x_offset in (-1, 0, 1):
                origin = (
                    anchor_x - width // 2 + x_offset,
                    anchor_y - height // 2 + y_offset,
                )
                if origin not in origins:
                    origins.append(origin)
    for y in range(2, HEIGHT - height - 1):
        for x in range(2, WIDTH - width - 1):
            origins.append((x, y))
    rng.shuffle(origins)
    return origins


def _can_place_wall_shape(
    grid: List[List[str]],
    origin: Tuple[int, int],
    shape: Sequence[str],
) -> bool:
    origin_x, origin_y = origin
    shape_height = len(shape)
    shape_width = max(len(row) for row in shape)
    for y in range(origin_y - 1, origin_y + shape_height + 1):
        for x in range(origin_x - 1, origin_x + shape_width + 1):
            if not _inside(x, y) or grid[y][x] != ".":
                return False
    for y_offset, row in enumerate(shape):
        for x_offset, value in enumerate(row):
            if value == "#" and grid[origin_y + y_offset][origin_x + x_offset] != ".":
                return False
    return True


def _set_wall_shape(
    grid: List[List[str]],
    origin: Tuple[int, int],
    shape: Sequence[str],
    tile: str,
) -> None:
    origin_x, origin_y = origin
    for y_offset, row in enumerate(shape):
        for x_offset, value in enumerate(row):
            if value == "#":
                grid[origin_y + y_offset][origin_x + x_offset] = tile


def _wall_origin_score(
    origin: Tuple[int, int],
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
            center[0] - 1, WIDTH - 2 - center[0],
            center[1] - 1, HEIGHT - 2 - center[1],
        ) * 8
    else:
        score = 120 - _manhattan(center, map_center) * 8
    score += 80 - _nearest_distance(center, template.obstacle_anchors) * 8
    if target_layout == "split_pair":
        score += abs(center[0] - map_center[0]) * 2
    return score


def _add_water_areas(
    grid: List[List[str]],
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
        origins = _water_origins(grid, width, height, template, style)
        rng.shuffle(origins)
        origins.sort(
            key=lambda origin: _water_origin_score(
                origin, width, height, template, style
            ),
            reverse=True,
        )
        committed = False
        for origin in origins:
            if not _can_place_water_rect(grid, origin, width, height):
                continue
            x, y = origin
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


def _water_origins(
    grid: List[List[str]],
    width: int,
    height: int,
    template: StructureTemplate,
    style: str,
) -> List[Tuple[int, int]]:
    origins = []
    for y in range(2, HEIGHT - height - 1):
        for x in range(2, WIDTH - width - 1):
            if _can_place_water_rect(grid, (x, y), width, height):
                origins.append((x, y))
    return origins


def _can_place_water_rect(
    grid: List[List[str]],
    origin: Tuple[int, int],
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
                neighbor = (xx + dx, yy + dy)
                if (
                    _inside(*neighbor)
                    and not (x <= neighbor[0] < x + width and y <= neighbor[1] < y + height)
                    and grid[neighbor[1]][neighbor[0]] == "@"
                ):
                    return False
    return True


def _water_origin_score(
    origin: Tuple[int, int],
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


def _place_targets(
    grid: List[List[str]],
    rng: random.Random,
    template: StructureTemplate,
    layout: str,
) -> Optional[Tuple[Tuple[int, int], ...]]:
    candidates = _ground_cells(grid)
    if len(candidates) < BOX_COUNT:
        return None
    rng.shuffle(candidates)

    if layout == "clustered":
        for first in candidates:
            pairs = [
                second for second in candidates
                if second != first and _manhattan(first, second) <= 2
            ]
            rng.shuffle(pairs)
            if pairs:
                return (first, pairs[0])
    elif layout == "edge_cluster":
        edge = [
            cell for cell in candidates
            if _near_playable_edge(cell)
        ]
        rng.shuffle(edge)
        for first in edge:
            pairs = [
                second for second in edge
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

    # Template-anchor fallback follows TryPlaceTargetsNearTemplateAnchors.
    selected = []
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


def _near_playable_edge(position: Tuple[int, int]) -> bool:
    x, y = position
    return x <= 2 or x >= WIDTH - 3 or y <= 2 or y >= HEIGHT - 3


def _place_fallback_targets(
    grid: List[List[str]],
    rng: random.Random,
) -> Optional[Tuple[Tuple[int, int], ...]]:
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
    grid: List[List[str]],
    rng: random.Random,
    targets: Sequence[Tuple[int, int]],
    template: Optional[StructureTemplate],
) -> Optional[Tuple[int, int]]:
    candidates = [
        cell for cell in _ground_cells(grid)
        if cell not in targets
    ]
    if not candidates:
        return None
    rng.shuffle(candidates)
    return max(
        candidates,
        key=lambda cell: (
            min(_manhattan(cell, target) for target in targets) * 4,
            _nearest_distance(cell, template.target_anchors)
            if template is not None else 0,
        ),
    )


def _reverse_pull_boxes(
    grid: List[List[str]],
    initial_boxes: List[Tuple[int, int]],
    initial_player: Tuple[int, int],
    rng: random.Random,
    target_pulls: int,
) -> Optional[Tuple[List[Tuple[int, int]], Tuple[int, int], int]]:
    boxes = list(initial_boxes)
    targets = set(initial_boxes)
    player = initial_player
    moved = [False] * len(boxes)
    reverse_pulls = 0

    for _ in range(MAX_REVERSE_PULL_ATTEMPTS):
        if reverse_pulls >= target_pulls:
            break
        legal = []
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
        boxes[index] = (
            box[0] - direction[0],
            box[1] - direction[1],
        )
        player = (
            box[0] - direction[0] * 2,
            box[1] - direction[1] * 2,
        )
        moved[index] = True
        reverse_pulls += 1

    if reverse_pulls < target_pulls or not all(moved):
        return None
    if any(box in targets for box in boxes) or player in targets:
        return None
    return boxes, player, reverse_pulls


def _can_reach(
    grid: List[List[str]],
    start: Tuple[int, int],
    destination: Tuple[int, int],
    boxes: Sequence[Tuple[int, int]],
) -> bool:
    if start == destination:
        return True
    if not _is_ground_at(grid, start) or not _is_ground_at(grid, destination):
        return False
    blocked = set(boxes)
    queue = [start]
    visited = {start}
    while queue:
        current = queue.pop(0)
        for direction in DIRECTIONS:
            next_position = (
                current[0] + direction[0],
                current[1] + direction[1],
            )
            if next_position in visited or next_position in blocked:
                continue
            if not _is_ground_at(grid, next_position):
                continue
            if next_position == destination:
                return True
            visited.add(next_position)
            queue.append(next_position)
    return False


def _has_adjacent_box_target_pair(
    boxes: Sequence[Tuple[int, int]],
    targets: Sequence[Tuple[int, int]],
) -> bool:
    return any(
        _manhattan(box, target) == 1
        for box in boxes
        for target in targets
    )


def _build_rows(
    grid: List[List[str]],
    boxes: Sequence[Tuple[int, int]],
    targets: Sequence[Tuple[int, int]],
    player: Tuple[int, int],
) -> List[str]:
    rendered = [row[:] for row in grid]
    for x, y in targets:
        rendered[y][x] = "t"
    for x, y in boxes:
        rendered[y][x] = "s"
    x, y = player
    rendered[y][x] = "p"
    return ["".join(row) for row in rendered]


def _ground_cells(grid: Sequence[Sequence[str]]) -> List[Tuple[int, int]]:
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
    pending = [next(iter(cells))]
    visited = set(pending)
    while pending:
        x, y = pending.pop()
        for dx, dy in DIRECTIONS:
            next_cell = (x + dx, y + dy)
            if next_cell in cells and next_cell not in visited:
                visited.add(next_cell)
                pending.append(next_cell)
    return visited == cells


def _validate_tile_rules(grid: Sequence[Sequence[str]]) -> bool:
    for y, row in enumerate(grid):
        for x, tile in enumerate(row):
            if tile == "@" and y > 0 and grid[y - 1][x] == "#":
                return False
    return _validate_wall_rules(grid)


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
        run_start = None
        run_length = 0
        for x in range(WIDTH):
            parallel = grid[y][x] == "#" and grid[y + 1][x] == "#"
            if parallel:
                run_start = x if run_start is None else run_start
                run_length += 1
            else:
                if run_length >= 2 and not _allowed_outer_shell_join(
                    grid, run_start, y, run_length
                ):
                    return False
                run_start = None
                run_length = 0
        if run_length >= 2 and not _allowed_outer_shell_join(
            grid, run_start, y, run_length
        ):
            return False
    return True


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


def _count_obstacle_influence(
    rows: Sequence[str],
    boxes: Sequence[Tuple[int, int]],
    targets: Sequence[Tuple[int, int]],
    player: Tuple[int, int],
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


def _near_axis_aligned_corridor(
    position: Tuple[int, int],
    boxes: Sequence[Tuple[int, int]],
    targets: Sequence[Tuple[int, int]],
) -> bool:
    x, y = position
    for box in boxes:
        for target in targets:
            if _near_corridor(position, box, target, 2):
                return True
    return False


def _near_corridor(
    position: Tuple[int, int],
    start: Tuple[int, int],
    end: Tuple[int, int],
    width: int,
) -> bool:
    x, y = position
    min_x = min(start[0], end[0]) - width
    max_x = max(start[0], end[0]) + width
    min_y = min(start[1], end[1]) - width
    max_y = max(start[1], end[1]) + width
    inside_horizontal_range = min_x <= x <= max_x
    inside_vertical_range = min_y <= y <= max_y
    near_horizontal_corridor = inside_horizontal_range and (
        abs(y - start[1]) <= width or abs(y - end[1]) <= width
    )
    near_vertical_corridor = inside_vertical_range and (
        abs(x - start[0]) <= width or abs(x - end[0]) <= width
    )
    return near_horizontal_corridor or near_vertical_corridor


def _is_choke_influence_wall(rows: Sequence[str], x: int, y: int) -> bool:
    left = _walkable_for_influence(rows, x - 1, y)
    right = _walkable_for_influence(rows, x + 1, y)
    up = _walkable_for_influence(rows, x, y - 1)
    down = _walkable_for_influence(rows, x, y + 1)
    return (left and right and not up and not down) or (
        up and down and not left and not right
    )


def _walkable_for_influence(rows: Sequence[str], x: int, y: int) -> bool:
    return _inside(x, y) and rows[y][x] in ".pst"


def _quality_score(
    validation: LevelValidationResult,
    reverse_pulls: int,
    water_tiles: int,
    water_areas: int,
    wall_tiles: int,
    obstacle_influence: int,
    boxes: Sequence[Tuple[int, int]],
    targets: Sequence[Tuple[int, int]],
    rows: Sequence[str],
) -> int:
    score = validation.solution_steps * 3
    score += validation.solution_pushes * 10
    score += reverse_pulls * 2
    score += min(validation.searched_states // 100, 40)
    score += min(water_tiles, 12) * 3
    score += water_areas * 20
    surrounded_walls = sum(
        1 for y in range(1, HEIGHT - 1)
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
    score -= _structure_similarity(rows)
    return score


def _structure_similarity(rows: Sequence[str]) -> int:
    # Standalone demo candidates have no parent design; retain the Unity
    # similarity term as zero rather than inventing a researcher condition.
    return 0


def _candidate_key(candidate: GenerationCandidate) -> Tuple[int, int, int, int]:
    return (
        int(_is_qualified(candidate)),
        candidate.quality_score,
        candidate.validation.solution_steps,
        candidate.reverse_pulls,
    )


def _is_qualified(candidate: GenerationCandidate) -> bool:
    return (
        MIN_SOLUTION_STEPS <= candidate.validation.solution_steps <= MAX_SOLUTION_STEPS
        and candidate.validation.solution_pushes >= PREFERRED_MIN_PUSHES
        and candidate.reverse_pulls >= PREFERRED_MIN_REVERSE_PULLS
        and candidate.obstacle_influence >= MIN_OBSTACLE_INFLUENCE
        and candidate.quality_score >= MIN_QUALITY_SCORE
    )


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


def _nearest_distance(
    point: Tuple[int, int],
    positions: Sequence[Tuple[int, int]],
) -> int:
    return min((_manhattan(point, other) for other in positions), default=999)


def _manhattan(first: Tuple[int, int], second: Tuple[int, int]) -> int:
    return abs(first[0] - second[0]) + abs(first[1] - second[1])


def _ground_cells_from_rows(rows: Sequence[str]) -> Set[Tuple[int, int]]:
    return {
        (x, y)
        for y, row in enumerate(rows)
        for x, tile in enumerate(row)
        if tile in ".pst"
    }


def _tile_at(grid, x: int, y: int) -> str:
    if not _inside(x, y):
        return " "
    return grid[y][x]


def _is_wall_at(grid, x: int, y: int) -> bool:
    return _tile_at(grid, x, y) == "#"


def _is_ground_at(grid: Sequence[Sequence[str]], position: Tuple[int, int]) -> bool:
    x, y = position
    return _inside(x, y) and grid[y][x] == "."


def _inside(x: int, y: int) -> bool:
    return 0 <= x < WIDTH and 0 <= y < HEIGHT


def _try_validate(rows: Sequence[str]) -> Optional[LevelValidationResult]:
    try:
        return validate_and_solve(list(rows))
    except LevelValidationError:
        return None
