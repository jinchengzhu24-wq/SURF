from collections import deque
from dataclasses import dataclass


WIDTH = 12
HEIGHT = 10
ALLOWED_TILES = frozenset(" #.@pst")
WALKABLE_TILES = frozenset(".pst")
MAX_SEARCH_STATES = 300_000


class LevelValidationError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = str(code)
        self.details = details or {}


@dataclass(frozen=True)
class LevelValidationResult:
    rows: tuple[str, ...]
    searched_states: int
    solution_steps: int
    solution_pushes: int
    solution: str

    def as_dict(self):
        return {
            "valid": True,
            "solvable": True,
            "searchedStates": self.searched_states,
            "solutionSteps": self.solution_steps,
            "solutionPushes": self.solution_pushes,
            "solution": self.solution,
        }


def validate_and_solve(rows, maximum_search_states=MAX_SEARCH_STATES):
    normalized = validate_rows(rows)
    walkable = {
        (x, y)
        for y, row in enumerate(normalized)
        for x, tile in enumerate(row)
        if tile in WALKABLE_TILES
    }
    player = _find_one(normalized, "p")
    boxes = tuple(sorted(_find_all(normalized, "s")))
    targets = frozenset(_find_all(normalized, "t"))
    queue = deque([(player, boxes, "", 0)])
    visited = {(player, boxes)}
    directions = (
        (0, -1, "U"),
        (0, 1, "D"),
        (-1, 0, "L"),
        (1, 0, "R"),
    )

    while queue:
        current_player, current_boxes, trace, pushes = queue.popleft()

        if set(current_boxes) == set(targets):
            return LevelValidationResult(
                normalized,
                len(visited),
                len(trace),
                pushes,
                trace,
            )

        if len(visited) >= maximum_search_states:
            raise LevelValidationError(
                "SEARCH_BUDGET_EXCEEDED",
                "Level validation exceeded the search budget.",
                {"searchedStates": len(visited)},
            )

        box_set = set(current_boxes)

        for dx, dy, move in directions:
            destination = (current_player[0] + dx, current_player[1] + dy)

            if destination not in walkable:
                continue

            next_boxes = current_boxes
            next_pushes = pushes

            if destination in box_set:
                box_destination = (destination[0] + dx, destination[1] + dy)

                if box_destination not in walkable or box_destination in box_set:
                    continue

                moved_boxes = list(current_boxes)
                moved_boxes[moved_boxes.index(destination)] = box_destination
                next_boxes = tuple(sorted(moved_boxes))
                next_pushes += 1

            state = (destination, next_boxes)

            if state in visited:
                continue

            visited.add(state)
            queue.append((destination, next_boxes, trace + move, next_pushes))

    raise LevelValidationError(
        "UNSOLVABLE_LEVEL",
        "The level has no Sokoban solution.",
        {"searchedStates": len(visited)},
    )


def validate_rows(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) != HEIGHT:
        raise LevelValidationError(
            "INVALID_HEIGHT",
            f"The level must contain exactly {HEIGHT} rows.",
        )

    normalized = []

    for index, row in enumerate(rows):
        if not isinstance(row, str) or len(row) != WIDTH:
            raise LevelValidationError(
                "INVALID_WIDTH",
                f"Row {index + 1} must contain exactly {WIDTH} tiles.",
                {"row": index},
            )

        unknown = sorted(set(row) - ALLOWED_TILES)

        if unknown:
            raise LevelValidationError(
                "UNKNOWN_TILE",
                f"Row {index + 1} contains an unknown tile.",
                {"row": index, "tiles": unknown},
            )

        normalized.append(row)

    player_count = sum(row.count("p") for row in normalized)
    box_count = sum(row.count("s") for row in normalized)
    target_count = sum(row.count("t") for row in normalized)

    if player_count != 1:
        raise LevelValidationError(
            "INVALID_PLAYER_COUNT",
            "The level must contain exactly one player.",
            {"count": player_count},
        )

    if box_count < 1 or box_count > 2:
        raise LevelValidationError(
            "INVALID_BOX_COUNT",
            "The level must contain one or two boxes.",
            {"count": box_count},
        )

    if target_count != box_count:
        raise LevelValidationError(
            "MISMATCHED_TARGET_COUNT",
            "The number of targets must match the number of boxes.",
            {"boxes": box_count, "targets": target_count},
        )

    return tuple(normalized)


def describe_diff(before_rows, after_rows):
    changes = []

    for y in range(HEIGHT):
        for x in range(WIDTH):
            before = before_rows[y][x]
            after = after_rows[y][x]

            if before != after:
                changes.append({"x": x, "y": y, "before": before, "after": after})

    return changes


def summarize_stage_changes(before_rows, after_rows):
    before_shell = _boundary_wall_cells(before_rows)
    after_shell = _boundary_wall_cells(after_rows)
    changed_cells = describe_diff(before_rows, after_rows)
    component_cells = {
        "outerShell": set(),
        "water": set(),
        "internalWalls": set(),
        "boxes": set(),
        "targets": set(),
        "player": set(),
        "floorArea": set(),
    }

    for change in changed_cells:
        position = (change["x"], change["y"])
        before = change["before"]
        after = change["after"]

        if "#" in {before, after}:
            component = (
                "outerShell"
                if position in before_shell or position in after_shell
                else "internalWalls"
            )
            component_cells[component].add(position)

        for tile, component in (
            ("@", "water"),
            ("s", "boxes"),
            ("t", "targets"),
            ("p", "player"),
        ):
            if tile in {before, after}:
                component_cells[component].add(position)

        if {before, after}.issubset({".", " "}):
            component_cells["floorArea"].add(position)

    order = (
        "outerShell",
        "water",
        "internalWalls",
        "boxes",
        "targets",
        "player",
        "floorArea",
    )
    components = [name for name in order if component_cells[name]]
    return {
        "components": components,
        "changedCellCount": len(changed_cells),
        "componentCellCounts": {
            name: len(component_cells[name]) for name in components
        },
    }


def _boundary_wall_cells(rows):
    pending = []
    visited = set()

    for y in range(HEIGHT):
        for x in range(WIDTH):
            if x not in {0, WIDTH - 1} and y not in {0, HEIGHT - 1}:
                continue
            if rows[y][x] == "#":
                pending.append((x, y))

    while pending:
        position = pending.pop()

        if position in visited:
            continue

        visited.add(position)
        x, y = position

        for next_x, next_y in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if not (0 <= next_x < WIDTH and 0 <= next_y < HEIGHT):
                continue
            if rows[next_y][next_x] == "#" and (next_x, next_y) not in visited:
                pending.append((next_x, next_y))

    return visited


def _find_all(rows, tile):
    return [
        (x, y)
        for y, row in enumerate(rows)
        for x, value in enumerate(row)
        if value == tile
    ]


def _find_one(rows, tile):
    values = _find_all(rows, tile)
    return values[0]
