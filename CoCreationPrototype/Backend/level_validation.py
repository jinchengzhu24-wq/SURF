from collections import deque
from dataclasses import dataclass
import hashlib
import json
import re


WIDTH = 12
HEIGHT = 10
ALLOWED_TILES = frozenset(" #.@pst")
WALKABLE_TILES = frozenset(".pst")
MAX_SEARCH_STATES = 300_000
ENTITY_BINDINGS_SCHEMA_VERSION = 1


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


def minimum_pushes(rows, maximum_search_states=MAX_SEARCH_STATES):
    """Return the fewest pushes needed to solve a valid Stage.

    The ordinary validator optimizes total player moves and reports the pushes
    on that witness route.  Proposal contracts that explicitly constrain push
    count need a different metric, so this bounded 0-1 search minimizes pushes
    directly without changing the public validator result.
    """
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
    pending = deque([(player, boxes)])
    costs = {(player, boxes): 0}
    directions = ((0, -1), (0, 1), (-1, 0), (1, 0))

    while pending:
        current_player, current_boxes = pending.popleft()
        current_state = (current_player, current_boxes)
        current_cost = costs[current_state]
        if set(current_boxes) == set(targets):
            return current_cost
        if len(costs) >= maximum_search_states:
            raise LevelValidationError(
                "MIN_PUSH_SEARCH_BUDGET_EXCEEDED",
                "Minimum-push validation exceeded the search budget.",
                {"searchedStates": len(costs)},
            )

        box_set = set(current_boxes)
        for dx, dy in directions:
            destination = (current_player[0] + dx, current_player[1] + dy)
            if destination not in walkable:
                continue
            next_boxes = current_boxes
            push_cost = 0
            if destination in box_set:
                box_destination = (destination[0] + dx, destination[1] + dy)
                if box_destination not in walkable or box_destination in box_set:
                    continue
                moved = list(current_boxes)
                moved[moved.index(destination)] = box_destination
                next_boxes = tuple(sorted(moved))
                push_cost = 1
            next_state = (destination, next_boxes)
            next_cost = current_cost + push_cost
            if next_cost >= costs.get(next_state, float("inf")):
                continue
            costs[next_state] = next_cost
            if push_cost:
                pending.append(next_state)
            else:
                pending.appendleft(next_state)

    raise LevelValidationError(
        "UNSOLVABLE_LEVEL",
        "The level has no Sokoban solution.",
        {"searchedStates": len(costs)},
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

    _validate_outer_wall_closure(normalized)

    return tuple(normalized)


def _validate_outer_wall_closure(rows):
    """Ensure exterior void cannot enter the level through a non-wall tile.

    A level may have an irregular outline, so spaces on the canvas perimeter are
    valid exterior void. Only walls can stop that exterior region: water and
    every other map tile are a breach when reachable from it.
    """
    exterior_void = deque()
    visited = set()

    for y in range(HEIGHT):
        for x in range(WIDTH):
            if x not in (0, WIDTH - 1) and y not in (0, HEIGHT - 1):
                continue

            tile = rows[y][x]

            if tile == "#":
                continue

            if tile != " ":
                _raise_open_outer_wall(x, y, tile)

            exterior_void.append((x, y))
            visited.add((x, y))

    while exterior_void:
        x, y = exterior_void.popleft()

        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            next_x, next_y = x + dx, y + dy

            if not (0 <= next_x < WIDTH and 0 <= next_y < HEIGHT):
                continue

            if (next_x, next_y) in visited:
                continue

            next_tile = rows[next_y][next_x]

            if next_tile == "#":
                continue

            if next_tile != " ":
                _raise_open_outer_wall(next_x, next_y, next_tile)

            visited.add((next_x, next_y))
            exterior_void.append((next_x, next_y))


def _raise_open_outer_wall(x, y, tile):
    raise LevelValidationError(
        "OPEN_OUTER_WALL",
        "The outer wall must be closed with # wall tiles; water cannot close the boundary.",
        {"row": y + 1, "column": x + 1, "tile": tile},
    )


def describe_diff(before_rows, after_rows):
    changes = []

    for y in range(HEIGHT):
        for x in range(WIDTH):
            before = before_rows[y][x]
            after = after_rows[y][x]

            if before != after:
                changes.append({"x": x, "y": y, "before": before, "after": after})

    return changes


def summarize_verified_diff(before_rows, after_rows, language="en"):
    """Describe only the tile changes proven by the before/after rows."""
    changes = describe_diff(before_rows, after_rows)

    if not changes:
        return "未检测到实际格子改动。" if language == "zh-CN" else "No tile changes detected."

    tile_names = (
        {
            " ": "边界外区域",
            "#": "墙",
            ".": "地面",
            "@": "水面",
            "p": "玩家",
            "s": "箱子",
            "t": "目标点",
        }
        if language == "zh-CN"
        else {
            " ": "void",
            "#": "wall",
            ".": "floor",
            "@": "water",
            "p": "player",
            "s": "box",
            "t": "target",
        }
    )

    if len(changes) <= 8:
        if language == "zh-CN":
            details = "；".join(
                f"第{change['y'] + 1}行第{change['x'] + 1}列："
                f"{tile_names[change['before']]}→{tile_names[change['after']]}"
                for change in changes
            )
            return f"已核对实际改动（共{len(changes)}格）：{details}。"

        details = "; ".join(
            f"row {change['y'] + 1}, column {change['x'] + 1}: "
            f"{tile_names[change['before']]} → {tile_names[change['after']]}"
            for change in changes
        )
        return f"Verified tile changes ({len(changes)} total): {details}."

    transition_counts = {}

    for change in changes:
        transition = (change["before"], change["after"])
        transition_counts[transition] = transition_counts.get(transition, 0) + 1

    if language == "zh-CN":
        details = "；".join(
            f"{tile_names[before]}→{tile_names[after]} {count}格"
            for (before, after), count in transition_counts.items()
        )
        return f"已核对实际改动（共{len(changes)}格）：{details}。具体位置以地图高亮为准。"

    details = "; ".join(
        f"{tile_names[before]} → {tile_names[after]}: {count}"
        for (before, after), count in transition_counts.items()
    )
    return (
        f"Verified tile changes ({len(changes)} total): {details}. "
        "See the highlighted map cells for every location."
    )


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


def build_entity_bindings(
    rows,
    parent_bindings=None,
    entity_transitions=None,
    source="initial",
):
    """Create conservative, persistent logical identities for one Stage.

    Entity labels are presentation-friendly (B1/B2/T1/T2), while entityId is
    the identity that is carried through child Stages.  A parent identity is
    only inherited when the unchanged coordinate, an explicit tagged move, or
    one unambiguous remove/add pair proves the mapping.  Ambiguous edits are
    intentionally marked unknown instead of being guessed from distance.
    """
    normalized = validate_rows(rows)
    current_positions = {
        "player": _find_all(normalized, "p"),
        "box": _find_all(normalized, "s"),
        "target": _find_all(normalized, "t"),
    }
    tile_by_kind = {"player": "p", "box": "s", "target": "t"}
    label_prefix = {"player": "P", "box": "B", "target": "T"}
    parent_entities = _binding_entities(parent_bindings)
    transition_items = [
        item for item in (entity_transitions or ()) if isinstance(item, dict)
    ]
    ambiguous_kinds = set()
    for kind, tile in tile_by_kind.items():
        source_positions = {
            (item.get("column"), item.get("row"))
            for item in transition_items
            if item.get("from") == tile and item.get("to") != tile
        }
        destination_positions = {
            (item.get("column"), item.get("row"))
            for item in transition_items
            if item.get("to") == tile and item.get("from") != tile
        }
        # Multiple untagged entities moving in one operation cannot be mapped
        # back to identities from the resulting grid alone.  Do not let the
        # unchanged-coordinate shortcut silently turn a swap into certainty.
        if len(source_positions) > 1 and len(destination_positions) > 1:
            if any(
                not item.get("anchorEntity")
                for item in transition_items
                if item.get("from") == tile or item.get("to") == tile
            ):
                ambiguous_kinds.add(kind)
    assignments = {}
    used_parent_ids = set()

    def assign(entity, position, confidence):
        key = (entity["kind"], tuple(position))
        if key in assignments:
            return False
        if entity.get("entityId") in used_parent_ids:
            return False
        assignments[key] = {
            "entityId": entity.get("entityId") or _new_entity_id(
                entity["kind"], position
            ),
            "label": entity.get("label"),
            "kind": entity["kind"],
            "row": position[1] + 1,
            "column": position[0] + 1,
            "identityConfidence": confidence,
        }
        if entity.get("entityId"):
            used_parent_ids.add(entity["entityId"])
        return True

    # The player is unique, so an unchanged coordinate is conclusive.  An
    # already-unknown binding, however, must stay unknown: a later snapshot
    # cannot retroactively prove which member of an earlier ambiguous group it
    # was merely because it still occupies the same cell.
    for entity in parent_entities:
        kind = entity.get("kind")
        if kind not in current_positions or kind in ambiguous_kinds:
            continue
        position = (entity.get("column", 0) - 1, entity.get("row", 0) - 1)
        if position in current_positions[kind]:
            assign(
                entity,
                position,
                "exact" if entity.get("identityConfidence") == "exact" else "unknown",
            )

    # Explicit entity-tagged transitions are stronger than generic grid diffs.
    for transition in transition_items:
        label = transition.get("anchorEntity")
        if not isinstance(label, str):
            continue
        entity = next(
            (item for item in parent_entities if item.get("label") == label),
            None,
        )
        if entity is None:
            continue
        kind = entity.get("kind")
        if kind not in current_positions:
            continue
        if transition.get("from") not in {"p", "s", "t"}:
            continue
        if transition.get("to") not in {".", "p", "s", "t"}:
            continue
        position = (
            int(transition.get("column", 0)) - 1,
            int(transition.get("row", 0)) - 1,
        )
        if position in current_positions[kind] and transition.get("to") == tile_by_kind[kind]:
            assign(entity, position, "exact")

    # A single removed entity and a single new position is provable.  Anything
    # involving two possible entities remains unknown.
    for kind, positions in current_positions.items():
        assigned_positions = {
            position for (assigned_kind, position) in assignments
            if assigned_kind == kind
        }
        unmatched_current = [position for position in positions if position not in assigned_positions]
        unmatched_parent = [
            entity for entity in parent_entities
            if entity.get("kind") == kind
            and entity.get("entityId") not in used_parent_ids
        ]
        if len(unmatched_current) == 1 and len(unmatched_parent) == 1:
            assign(unmatched_parent[0], unmatched_current[0], "exact")

    records = []
    for kind in ("player", "box", "target"):
        positions = sorted(current_positions[kind], key=lambda item: (item[1], item[0]))
        used_labels = {
            item.get("label") for item in assignments.values()
            if item.get("kind") == kind and item.get("label")
        }
        available_labels = [
            "P" if kind == "player" else f"{label_prefix[kind]}{index}"
            for index in range(1, len(positions) + 1)
        ]
        for position in positions:
            record = assignments.get((kind, position))
            if record is None:
                label = next(
                    (candidate for candidate in available_labels if candidate not in used_labels),
                    f"{label_prefix[kind]}{len(records) + 1}",
                )
                record = {
                    "entityId": _new_entity_id(kind, position),
                    "label": label,
                    "kind": kind,
                    "row": position[1] + 1,
                    "column": position[0] + 1,
                    "identityConfidence": "unknown" if parent_entities else "exact",
                }
                assignments[(kind, position)] = record
            used_labels.add(record["label"])
            records.append(record)

    status = (
        "exact"
        if all(item["identityConfidence"] == "exact" for item in records)
        else "partial"
        if any(item["identityConfidence"] == "exact" for item in records)
        else "unknown"
    )
    binding = {
        "schemaVersion": ENTITY_BINDINGS_SCHEMA_VERSION,
        "mapFingerprint": _rows_fingerprint(normalized),
        "identityStatus": status,
        "source": source,
        "entities": records,
    }
    binding["bindingFingerprint"] = entity_binding_fingerprint(binding)
    return binding


def entity_binding_fingerprint(bindings):
    """Return a stable hash for the logical identity snapshot."""
    payload = {
        "schemaVersion": (bindings or {}).get("schemaVersion", ENTITY_BINDINGS_SCHEMA_VERSION),
        "identityStatus": (bindings or {}).get("identityStatus", "unknown"),
        "entities": [
            {
                key: item.get(key)
                for key in (
                    "entityId", "label", "kind", "row", "column", "identityConfidence"
                )
            }
            for item in _binding_entities(bindings)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _new_entity_id(kind, position):
    seed = f"{kind}:{position[1] + 1}:{position[0] + 1}".encode("utf-8")
    opaque_id = hashlib.sha256(seed).hexdigest()[:16]
    return f"{kind}:{opaque_id}"


def _binding_entities(bindings):
    if not isinstance(bindings, dict) or not isinstance(bindings.get("entities"), list):
        return []
    return [item for item in bindings["entities"] if isinstance(item, dict)]


def _binding_matches_rows(bindings, normalized):
    entities = _binding_entities(bindings)
    if not entities:
        return False
    if bindings.get("schemaVersion") != ENTITY_BINDINGS_SCHEMA_VERSION:
        return False
    if bindings.get("mapFingerprint") != _rows_fingerprint(normalized):
        return False
    if bindings.get("bindingFingerprint") != entity_binding_fingerprint(bindings):
        return False
    expected = {
        "player": _find_all(normalized, "p"),
        "box": _find_all(normalized, "s"),
        "target": _find_all(normalized, "t"),
    }
    actual = {kind: [] for kind in expected}
    tile_by_kind = {"player": "p", "box": "s", "target": "t"}
    seen = set()
    seen_ids = set()
    seen_labels = set()
    for entity in entities:
        kind = entity.get("kind")
        row = entity.get("row")
        column = entity.get("column")
        entity_id = entity.get("entityId")
        label = entity.get("label")
        confidence = entity.get("identityConfidence")
        if (
            kind not in expected
            or not isinstance(row, int)
            or not isinstance(column, int)
            or not isinstance(entity_id, str)
            or not isinstance(label, str)
            or confidence not in {"exact", "unknown"}
            or entity_id in seen_ids
            or label in seen_labels
        ):
            return False
        position = (column - 1, row - 1)
        if (
            position in seen
            or position not in expected[kind]
            or not 1 <= row <= HEIGHT
            or not 1 <= column <= WIDTH
        ):
            return False
        seen.add(position)
        seen_ids.add(entity_id)
        seen_labels.add(label)
        actual[kind].append(position)
        if normalized[row - 1][column - 1] != tile_by_kind[kind]:
            return False
    if not all(sorted(actual[kind]) == sorted(expected[kind]) for kind in expected):
        return False
    expected_labels = {
        "player": {"P"},
        "box": {f"B{index}" for index in range(1, len(expected["box"]) + 1)},
        "target": {f"T{index}" for index in range(1, len(expected["target"]) + 1)},
    }
    return all(
        {entity.get("label") for entity in entities if entity.get("kind") == kind}
        == expected_labels[kind]
        for kind in expected
    )


def entity_bindings_match_rows(bindings, rows):
    """Public integrity check for a server-owned identity snapshot."""
    try:
        normalized = _normalize_rows_for_structural_comparison(rows)
    except (TypeError, ValueError):
        return False
    return _binding_matches_rows(bindings, normalized)


def _normalize_rows_for_structural_comparison(rows):
    """Validate only grid shape and tile alphabet for legacy diffing."""
    if not isinstance(rows, (list, tuple)) or len(rows) != HEIGHT:
        raise ValueError(f"The level must contain exactly {HEIGHT} rows.")

    normalized = []
    for index, row in enumerate(rows):
        if not isinstance(row, str) or len(row) != WIDTH:
            raise ValueError(
                f"Row {index + 1} must contain exactly {WIDTH} tiles."
            )
        unknown = sorted(set(row) - ALLOWED_TILES)
        if unknown:
            raise ValueError(
                f"Row {index + 1} contains unknown tiles: {unknown}."
            )
        normalized.append(row)
    return tuple(normalized)


def build_untrusted_entity_bindings(rows, source="legacy_untrusted"):
    """Build coordinate-only bindings for structurally valid legacy maps.

    Labels are deterministic for display, but every identity is explicitly
    unknown. This is the safe migration representation when a historical map
    cannot pass today's semantic level validator.
    """
    normalized = _normalize_rows_for_structural_comparison(rows)
    positions = {
        "player": _find_all(normalized, "p"),
        "box": _find_all(normalized, "s"),
        "target": _find_all(normalized, "t"),
    }
    prefixes = {"player": "P", "box": "B", "target": "T"}
    records = []
    for kind in ("player", "box", "target"):
        for index, (column, row) in enumerate(
            sorted(positions[kind], key=lambda item: (item[1], item[0])),
            start=1,
        ):
            records.append({
                "entityId": _new_entity_id(kind, (column, row)),
                "label": (
                    prefixes[kind]
                    if kind == "player"
                    else f"{prefixes[kind]}{index}"
                ),
                "kind": kind,
                "row": row + 1,
                "column": column + 1,
                "identityConfidence": "unknown",
            })
    binding = {
        "schemaVersion": ENTITY_BINDINGS_SCHEMA_VERSION,
        "mapFingerprint": _rows_fingerprint(normalized),
        "identityStatus": "unknown",
        "source": source,
        "entities": records,
    }
    binding["bindingFingerprint"] = entity_binding_fingerprint(binding)
    return binding


def derive_entity_transitions(before_rows, after_rows):
    """Return changed cells as untagged transitions for conservative mapping.

    This is intentionally only a diff observation.  It never assigns an
    entity label; callers pass explicit anchorEntity values when the operation
    contract proves identity.  The diff lets the binding layer detect edits
    with multiple possible sources/destinations and downgrade them.
    """
    before = _normalize_rows_for_structural_comparison(before_rows)
    after = _normalize_rows_for_structural_comparison(after_rows)
    if len(before) != len(after) or any(
        len(before[row]) != len(after[row]) for row in range(len(before))
    ):
        raise ValueError("before and after maps must have identical dimensions")
    return [
        {
            "row": row_index + 1,
            "column": column_index + 1,
            "from": before[row_index][column_index],
            "to": after[row_index][column_index],
        }
        for row_index in range(len(before))
        for column_index in range(len(before[row_index]))
        if before[row_index][column_index] != after[row_index][column_index]
    ]


def _rows_fingerprint(rows):
    return hashlib.sha256(
        json.dumps(list(rows), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _build_untrusted_bindings(normalized):
    """Represent a supplied-but-invalid snapshot without inventing certainty."""
    return build_untrusted_entity_bindings(
        normalized,
        source="untrusted_runtime_fallback",
    )


def build_map_facts(rows, before_rows=None, entity_bindings=None):
    """Return compact, deterministic facts for language-model grounding.

    Coordinates in this payload are deliberately one-based so they agree with the
    numbered maps shown in prompts and with the UI's verified-diff wording.  These
    are descriptive facts only: a ``gridDistance`` is Manhattan distance, never a
    claim that a push route is available.
    """
    semantic_validation_error = None
    try:
        normalized = validate_rows(rows)
    except LevelValidationError as error:
        if error.code in {
            "INVALID_PLAYER_COUNT",
            "INVALID_BOX_COUNT",
            "MISMATCHED_TARGET_COUNT",
        }:
            raise
        # Keep historical, structurally readable maps available for grounding
        # and display. Strict validation is still required before saving or
        # executing a new map; this branch only prevents old data from taking
        # down the service while it is being reviewed or migrated.
        normalized = _normalize_rows_for_structural_comparison(rows)
        semantic_validation_error = {
            "code": error.code,
            "message": str(error),
        }
    positions = {
        tile: [(x + 1, y + 1) for y, row in enumerate(normalized)
               for x, value in enumerate(row) if value == tile]
        for tile in ("p", "s", "t", "@")
    }
    water = set(positions["@"])

    def point(position):
        column, row = position
        return {"row": row, "column": column}

    def adjacent_to_water(position):
        column, row = position
        return any(
            (column + dx, row + dy) in water
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0))
        )

    if entity_bindings is None:
        if semantic_validation_error is None:
            bindings = build_entity_bindings(normalized, source="legacy_derived")
        else:
            bindings = build_untrusted_entity_bindings(
                normalized,
                source="legacy_untrusted",
            )
    elif _binding_matches_rows(entity_bindings, normalized):
        bindings = entity_bindings
    else:
        # A malformed or stale binding must never silently become a new exact
        # B1/B2 numbering.  Keep the current coordinates usable, but make the
        # entity labels non-authoritative until the Stage is backfilled/fixed.
        bindings = _build_untrusted_bindings(normalized)
    bound_entities = _binding_entities(bindings)
    # Bind by the actual current coordinate, never by the order in which the
    # grid happens to enumerate entities.  Row-major re-numbering here was a
    # subtle source of B1/B2 and T1/T2 swaps after a child Stage was created.
    bound_by_position = {
        (item.get("kind"), item.get("row"), item.get("column")): item
        for item in bound_entities
        if isinstance(item, dict)
    }

    def bound_entity(kind, position):
        column, row = position
        return bound_by_position.get((kind, row, column), {})

    targets = [point(position) for position in positions["t"]]
    boxes = []
    for index, position in enumerate(positions["s"], start=1):
        column, row = position
        bound = bound_entity("box", position)
        boxes.append({
            "id": bound.get("label") or f"B{index}",
            **point(position),
            "entityId": bound.get("entityId"),
            "identityConfidence": bound.get("identityConfidence", "unknown"),
            "orthogonallyAdjacentToWater": adjacent_to_water(position),
            "gridDistancesToTargets": [
                {
                    "targetId": (
                        bound_entity("target", (target_column, target_row)).get("label")
                        or f"T{target_index}"
                    ),
                    "gridDistance": abs(column - target_column) + abs(row - target_row),
                }
                for target_index, (target_column, target_row) in enumerate(
                    positions["t"], start=1
                )
            ],
        })

    facts = {
        "schemaVersion": 1,
        "dimensions": {"rows": HEIGHT, "columns": WIDTH},
        "mapFingerprint": _rows_fingerprint(normalized),
        "entityBindingFingerprint": entity_binding_fingerprint(bindings),
        "identityStatus": bindings.get("identityStatus", "unknown"),
        "coordinateSystem": "one-based row,column; row 1 is top and column 1 is left",
        "player": (
            {
                "id": bound_entity("player", positions["p"][0]).get("label", "P"),
                **point(positions["p"][0]),
            }
            if positions["p"]
            else None
        ),
        "boxes": boxes,
        "targets": [
            {
                "id": (
                    bound_entity(
                        "target", (target["column"], target["row"])
                    ).get("label")
                ) or f"T{index}",
                **target,
                "entityId": (
                    bound_entity(
                        "target", (target["column"], target["row"])
                    ).get("entityId")
                ),
                "identityConfidence": (
                    bound_entity(
                        "target", (target["column"], target["row"])
                    ).get("identityConfidence", "unknown")
                ),
            }
            for index, target in enumerate(targets, start=1)
        ],
        "waterCells": [point(position) for position in positions["@"]],
        # Keep exact tile facts alongside the compact entity facts.  The chat
        # model may discuss a coordinate, but only this authoritative map
        # snapshot may decide whether that coordinate is actually editable.
        "tileAt": {
            f"{y + 1},{x + 1}": tile
            for y, row in enumerate(normalized)
            for x, tile in enumerate(row)
            if tile != " "
        },
    }
    if semantic_validation_error is not None:
        facts["semanticValidation"] = {
            "valid": False,
            **semantic_validation_error,
        }

    facts["entities"] = [
        {
            "id": item.get("label") or "P",
            "entityId": item.get("entityId"),
            "kind": item.get("kind"),
            "identityConfidence": item.get("identityConfidence", "unknown"),
            **point((item["column"], item["row"])),
        }
        for item in bound_entities
    ]

    if before_rows is not None:
        try:
            before = validate_rows(before_rows)
        except LevelValidationError:
            before = _normalize_rows_for_structural_comparison(before_rows)
        entity_changes = {}
        for tile, label in (("p", "player"), ("s", "boxes"), ("t", "targets")):
            before_positions = set(_find_all(before, tile))
            after_positions = set(_find_all(normalized, tile))
            removed = sorted(before_positions - after_positions)
            added = sorted(after_positions - before_positions)
            if removed or added:
                entity_changes[label] = {
                    "removed": [point((x + 1, y + 1)) for x, y in removed],
                    "added": [point((x + 1, y + 1)) for x, y in added],
                }
        facts["verifiedEntityChangesFromParent"] = entity_changes

    return facts


def build_stage_snapshot(
    rows,
    *,
    version_id=None,
    stage_number=None,
    entity_bindings=None,
):
    """Build the one authoritative, current-Stage fact object.

    This deliberately contains the grid and the server-owned identity facts in
    one object.  Callers may add presentation metadata around it, but must not
    send a second map representation to an LLM.
    """
    normalized = _normalize_rows_for_structural_comparison(rows)
    facts = build_map_facts(normalized, entity_bindings=entity_bindings)
    player = dict(facts.get("player") or {})
    player_binding = next(
        (
            item for item in facts.get("entities") or []
            if item.get("kind") == "player"
        ),
        {},
    )
    player["entityId"] = player_binding.get("entityId")
    player["identityConfidence"] = player_binding.get(
        "identityConfidence", "unknown"
    )
    snapshot = {
        "schemaVersion": 1,
        "versionId": version_id,
        "stageNumber": stage_number,
        "rows": list(normalized),
        "dimensions": {"rows": HEIGHT, "columns": WIDTH},
        "coordinateSystem": facts["coordinateSystem"],
        "mapFingerprint": facts["mapFingerprint"],
        "entityBindingFingerprint": facts["entityBindingFingerprint"],
        "identityStatus": facts.get("identityStatus", "unknown"),
        "entities": facts.get("entities", []),
        "player": player,
        "boxes": facts.get("boxes", []),
        "targets": facts.get("targets", []),
        "waterCells": facts.get("waterCells", []),
        "tileAt": facts.get("tileAt", {}),
    }
    return snapshot


def _claim_is_non_current(text, start, end):
    """Do not treat future, hypothetical, or route wording as current fact."""
    # Only look before the claimed fact.  A valid fact followed by a design
    # intention such as “B1 is at (7,4), and I want...” must still be checked.
    # Include the claim itself: future wording often sits between the entity
    # label and its coordinate (for example, “B1 will be at (4,4)”).
    window = str(text or "")[max(0, start - 28):end]
    # Keep this expression ASCII-escaped because this module is also deployed
    # on Windows environments with a non-UTF-8 console/code page.
    return bool(re.search(
        r"(?:\u5982\u679c|\u82e5|\u5047\u8bbe|\u5c06|\u4f1a|\u51c6\u5907|\u5e0c\u671b|\u60f3\u8981|\u60f3\u628a|\u60f3\u8ba9|\u79fb\u52a8\u5230|\u63a8\u5230|\u4ece[^\u3002\uff01\uff1f?]{0,18}(?:\u5230|\u5411)|"
        r"\b(?:if|when|would|will|move|push|from|toward|through|via)\b)",
        window,
        flags=re.IGNORECASE,
    ))
def analyze_user_map_claims(text, snapshot):
    """Compare only present-tense user map claims with one StageSnapshot.

    The raw user turn is retained for audit, while callers use this result to
    keep incorrect coordinates out of semantic memory and execution contracts.
    """
    value = str(text or "")
    snapshot = snapshot or {}
    entities = {
        str(item.get("id")): item
        for item in snapshot.get("entities", [])
        if isinstance(item, dict) and item.get("id")
    }
    tile_at = snapshot.get("tileAt") or {}
    claims = []
    conflicts = []
    seen = set()
    entity_pattern = re.compile(
        r"(?P<label>\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))\s*"
        r"(?:\u5728|\u4f4d\u4e8e|\u5750\u843d\u4e8e|occupies|is\s+at|is\s+located\s+(?:at|in)|sits\s+on)\s*"
        r"(?:\u7b2c\s*)?(?P<row>\d{1,2})\s*(?:\u884c\s*[,，]?\s*\u7b2c\s*|[,，])\s*"
        r"(?P<column>\d{1,2})\s*(?:\u5217)?|"
        r"(?P<label_after>\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))\s*"
        r"(?:\u5728|\u4f4d\u4e8e|\u5750\u843d\u4e8e|occupies|is\s+at|is\s+located\s+(?:at|in)|sits\s+on)\s*"
        r"[（(]\s*(?P<row_paren>\d{1,2})\s*[,，]\s*(?P<column_paren>\d{1,2})\s*[）)]|"
        r"[（(]\s*(?P<row_reverse>\d{1,2})\s*[,，]\s*(?P<column_reverse>\d{1,2})\s*[）)]\s*"
        r"(?:\u662f|\u4e3a|belongs\s+to)\s*(?P<label_reverse>\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))",
        flags=re.IGNORECASE,
    )
    tile_pattern = re.compile(
        r"[（(]\s*(?P<row>\d{1,2})\s*[,，]\s*(?P<column>\d{1,2})\s*[）)]\s*"
        r"(?:\u662f|\u4e3a|\u5c5e\u4e8e|is|contains)\s*"
        r"(?P<tile>\u6c34\u57df|\u6c34|\u5899|\u5899\u4f53|\u5730\u9762|\u7a7a\u5730|\u901a\u9053|water|wall|floor|ground|corridor)",
        flags=re.IGNORECASE,
    )

    # Re-declare with only ASCII source escapes.  This is intentionally kept
    # local so the parser remains independent of the host console encoding.
    entity_pattern = re.compile(
        r"(?P<label>\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))\s*"
        r"(?:\u5728|\u4f4d\u4e8e|\u5750\u843d\u4e8e|occupies|is\s+at|is\s+located\s+at|sits\s+on)\s*"
        r"(?:\u7b2c\s*)?(?P<row>\d{1,2})\s*(?:\u884c\s*[,\uFF0C]?\s*\u7b2c\s*|[,\uFF0C])\s*"
        r"(?P<column>\d{1,2})\s*(?:\u5217)?|"
        r"(?P<label_after>\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))\s*"
        r"(?:\u5728|\u4f4d\u4e8e|\u5750\u843d\u4e8e|occupies|is\s+at|is\s+located\s+at|sits\s+on)\s*"
        r"[\uFF08(]\s*(?P<row_paren>\d{1,2})\s*[,\uFF0C]\s*(?P<column_paren>\d{1,2})\s*[\uFF09)]|"
        r"[\uFF08(]\s*(?P<row_reverse>\d{1,2})\s*[,\uFF0C]\s*(?P<column_reverse>\d{1,2})\s*[\uFF09)]\s*"
        r"(?:\u662f|\u4e3a|belongs\s+to)\s*(?P<label_reverse>\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))",
        flags=re.IGNORECASE,
    )
    tile_pattern = re.compile(
        r"[\uFF08(]\s*(?P<row>\d{1,2})\s*[,\uFF0C]\s*(?P<column>\d{1,2})\s*[\uFF09)]\s*"
        r"(?:\u662f|\u4e3a|\u5c5e\u4e8e|is|contains)\s*"
        r"(?P<tile>\u6c34\u57df|\u6c34|\u5899|\u5899\u4f53|\u5730\u9762|\u7a7a\u5730|\u901a\u9053|water|wall|floor|ground|corridor)",
        flags=re.IGNORECASE,
    )
    for match in entity_pattern.finditer(value):
        if _claim_is_non_current(value, match.start(), match.end()):
            continue
        label = (
            match.group("label")
            or match.group("label_after")
            or match.group("label_reverse")
        )
        row = (
            match.group("row") or match.group("row_paren")
            or match.group("row_reverse")
        )
        column = (
            match.group("column") or match.group("column_paren")
            or match.group("column_reverse")
        )
        if not label or row is None or column is None:
            continue
        key = ("entity", label.upper(), int(row), int(column))
        if key in seen:
            continue
        seen.add(key)
        expected = entities.get(label.upper())
        claim = {
            "kind": "entity",
            "entity": label.upper(),
            "row": int(row),
            "column": int(column),
            "sourceText": match.group(0),
            "current": expected is not None and (
                expected.get("row"), expected.get("column")
            ) == (int(row), int(column)),
        }
        if expected is None:
            claim["reason"] = "entity_not_bound_to_current_stage"
            conflicts.append(claim)
        elif expected.get("identityConfidence") != "exact":
            claim["reason"] = "entity_identity_unknown"
            claim["expected"] = {
                "row": expected.get("row"),
                "column": expected.get("column"),
            }
            conflicts.append(claim)
        if (
            expected is not None
            and expected.get("identityConfidence") == "exact"
            and not claim["current"]
        ):
            claim["expected"] = {
                "row": expected.get("row"),
                "column": expected.get("column"),
            }
            conflicts.append(claim)
        claims.append(claim)

    row_entity_pattern = re.compile(
        r"(?P<label>\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))\s*"
        r"(?:is\s+located\s+(?:in|at)|is\s+sitting\s+in|sits\s+(?:in|on)|occupies)\s+row\s*"
        r"(?P<row>\d{1,2})\s*[,\s]+(?:column\s*)?(?P<column>\d{1,2})"
        r"|row\s*(?P<row_reverse>\d{1,2})\s*[,\s]+column\s*"
        r"(?P<column_reverse>\d{1,2})\s*(?:is|contains)\s*"
        r"(?P<label_reverse>\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))",
        flags=re.IGNORECASE,
    )
    for match in row_entity_pattern.finditer(value):
        if _claim_is_non_current(value, match.start(), match.end()):
            continue
        label = match.group("label") or match.group("label_reverse")
        row = match.group("row") or match.group("row_reverse")
        column = match.group("column") or match.group("column_reverse")
        if not label or row is None or column is None:
            continue
        key = ("entity", label.upper(), int(row), int(column))
        if key in seen:
            continue
        seen.add(key)
        expected = entities.get(label.upper())
        claim = {
            "kind": "entity",
            "entity": label.upper(),
            "row": int(row),
            "column": int(column),
            "sourceText": match.group(0),
            "current": expected is not None and (
                expected.get("row"), expected.get("column")
            ) == (int(row), int(column)),
        }
        if expected is None:
            claim["reason"] = "entity_not_bound_to_current_stage"
            conflicts.append(claim)
        if expected is not None and not claim["current"]:
            claim["expected"] = {
                "row": expected.get("row"),
                "column": expected.get("column"),
            }
            conflicts.append(claim)
        claims.append(claim)

    chinese_row_entity_pattern = re.compile(
        r"(?:\u7b2c\s*)?(?P<row>\d{1,2})\s*\u884c\s*"
        r"(?:\u7b2c\s*)?(?P<column>\d{1,2})\s*\u5217\s*"
        r"(?:\u662f|\u4e3a)\s*"
        r"(?P<label>\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))",
        flags=re.IGNORECASE,
    )
    for match in chinese_row_entity_pattern.finditer(value):
        if _claim_is_non_current(value, match.start(), match.end()):
            continue
        label = match.group("label").upper()
        row, column = int(match.group("row")), int(match.group("column"))
        key = ("entity", label, row, column)
        if key in seen:
            continue
        seen.add(key)
        expected = entities.get(label)
        claim = {
            "kind": "entity",
            "entity": label,
            "row": row,
            "column": column,
            "sourceText": match.group(0),
            "current": expected is not None
            and (expected.get("row"), expected.get("column")) == (row, column),
        }
        if expected is None:
            claim["reason"] = "entity_not_bound_to_current_stage"
            conflicts.append(claim)
        elif expected.get("identityConfidence") != "exact":
            claim["reason"] = "entity_identity_unknown"
            claim["expected"] = {
                "row": expected.get("row"),
                "column": expected.get("column"),
            }
            conflicts.append(claim)
        elif not claim["current"]:
            claim["expected"] = {
                "row": expected.get("row"),
                "column": expected.get("column"),
            }
            conflicts.append(claim)
        claims.append(claim)

    tile_names = {
        "水域": "@", "水": "@", "water": "@",
        "墙": "#", "墙体": "#", "wall": "#",
        "地面": ".", "空地": ".", "通道": ".", "floor": ".",
        "ground": ".", "corridor": ".",
    }
    tile_names = {
        "\u6c34\u57df": "@", "\u6c34": "@", "water": "@",
        "\u5899": "#", "\u5899\u4f53": "#", "wall": "#",
        "\u5730\u9762": ".", "\u7a7a\u5730": ".", "\u901a\u9053": ".", "floor": ".",
        "ground": ".", "corridor": ".",
    }
    for match in tile_pattern.finditer(value):
        if _claim_is_non_current(value, match.start(), match.end()):
            continue
        row, column = int(match.group("row")), int(match.group("column"))
        key = ("tile", row, column, match.group("tile").casefold())
        if key in seen:
            continue
        seen.add(key)
        actual = tile_at.get(f"{row},{column}")
        if actual is None:
            snapshot_rows = snapshot.get("rows") or []
            if 1 <= row <= HEIGHT and 1 <= column <= WIDTH:
                actual = snapshot_rows[row - 1][column - 1]
        expected_tile = tile_names.get(match.group("tile").casefold())
        claim = {
            "kind": "tile",
            "row": row,
            "column": column,
            "claimedTile": expected_tile,
            "sourceText": match.group(0),
            "current": actual == expected_tile,
        }
        if not claim["current"]:
            claim["actualTile"] = actual
            conflicts.append(claim)
        claims.append(claim)

    row_tile_pattern = re.compile(
        r"(?:\u7b2c\s*)?(?P<row>\d{1,2})\s*\u884c\s*"
        r"(?:\u7b2c\s*)?(?P<column>\d{1,2})\s*\u5217\s*"
        r"(?:\u662f|\u4e3a|\u5c5e\u4e8e)\s*"
        r"(?P<tile>\u6c34\u57df|\u6c34|\u5899|\u5899\u4f53|\u5730\u9762|\u7a7a\u5730|\u901a\u9053)",
        flags=re.IGNORECASE,
    )
    for match in row_tile_pattern.finditer(value):
        if _claim_is_non_current(value, match.start(), match.end()):
            continue
        row, column = int(match.group("row")), int(match.group("column"))
        key = ("tile", row, column, match.group("tile").casefold())
        if key in seen:
            continue
        seen.add(key)
        actual = tile_at.get(f"{row},{column}")
        if actual is None:
            snapshot_rows = snapshot.get("rows") or []
            if 1 <= row <= HEIGHT and 1 <= column <= WIDTH:
                actual = snapshot_rows[row - 1][column - 1]
        expected_tile = tile_names.get(match.group("tile").casefold())
        claim = {
            "kind": "tile",
            "row": row,
            "column": column,
            "claimedTile": expected_tile,
            "sourceText": match.group(0),
            "current": actual == expected_tile,
        }
        if not claim["current"]:
            claim["actualTile"] = actual
            conflicts.append(claim)
        claims.append(claim)

    return {"claims": claims, "conflicts": conflicts}


def format_map_claim_correction(conflicts, language="en"):
    """Return a short deterministic correction for the visible chat body."""
    if not conflicts:
        return ""
    first = conflicts[0]
    if language == "zh-CN" and first.get("kind") == "entity":
        if first.get("reason") == "entity_identity_unknown":
            return (
                f"\u5f53\u524d Stage \u53ea\u80fd\u786e\u8ba4\u76f8\u5173\u683c\u5b50\u4e0a\u7684\u5b9e\u4f53\uff0c\u6682\u65f6\u65e0\u6cd5\u628a\u5b83\u4e0e\u5386\u53f2 {first.get('entity')} \u8eab\u4efd\u53ef\u9760\u5bf9\u5e94\u3002"
                "\u8bf7\u7528\u5f53\u524d\u5730\u56fe\u7684\u884c\u5217\u4f4d\u7f6e\u63cf\u8ff0\u5b83\uff0c\u6211\u4f1a\u6309\u8fd9\u4e2a\u4e2d\u6027\u4f4d\u7f6e\u7ee7\u7eed\u5206\u6790\u3002"
            )
        if not first.get("expected"):
            return (
                f"\u5f53\u524d Stage \u4e2d\u6ca1\u6709\u53ef\u9a8c\u8bc1\u7684 {first.get('entity')} \u8eab\u4efd\u3002"
                "\u8bf7\u6539\u7528\u5f53\u524d\u5730\u56fe\u7684\u884c\u5217\u4f4d\u7f6e\u63cf\u8ff0\u8981\u8ba8\u8bba\u7684\u5b9e\u4f53\u3002"
            )
    if language == "zh-CN":
        if first.get("kind") == "entity":
            if first.get("reason") == "entity_identity_unknown":
                return (
                    f"当前 Stage 只能确认相关格子上的实体，暂时无法把它与历史 {first.get('entity')} 身份可靠对应。"
                    "请用当前地图的行列位置描述它，我会按这个中性位置继续分析。"
                )
            if not first.get("expected"):
                return (
                    f"当前 Stage 中没有可验证的 {first.get('entity')} 身份。"
                    "请改用当前地图的行列位置描述要讨论的实体。"
                )
            expected = first.get("expected") or {}
            return (
                f"\u4f60\u63d0\u5230\u7684 {first.get('entity')} \u4f4d\u7f6e\u4e0e\u5f53\u524d Stage \u4e0d\u4e00\u81f4\u3002"
                f"\u5f53\u524d\u4fdd\u5b58\u5730\u56fe\u4e2d {first.get('entity')} \u4f4d\u4e8e\u7b2c {expected.get('row')} \u884c\u7b2c {expected.get('column')} \u5217\uff0c\u6211\u4f1a\u4ee5\u8fd9\u4e2a\u4f4d\u7f6e\u7ee7\u7eed\u5206\u6790\u3002"
            )
        tile_names = {"@": "\u6c34\u57df", "#": "\u5899\u4f53", ".": "\u5730\u9762"}
        actual_name = tile_names.get(first.get("actualTile"), "\u5f53\u524d\u683c\u5b50")
        return (
            f"\u4f60\u63d0\u5230\u7684\u7b2c {first.get('row')} \u884c\u7b2c {first.get('column')} \u5217\u4e0e\u5f53\u524d Stage \u4e0d\u4e00\u81f4\uff1b"
            f"\u5f53\u524d\u4fdd\u5b58\u5730\u56fe\u4e2d\u8fd9\u91cc\u662f{actual_name}\uff0c\u6211\u4f1a\u4ee5\u8fd9\u4e2a\u683c\u5b50\u72b6\u6001\u7ee7\u7eed\u5206\u6790\u3002"
        )
    if language == "zh-CN":
        if first.get("kind") == "entity":
            label = first.get("entity")
            expected = first.get("expected") or {}
            return (
                f"你提到的 {label} 位置与当前 Stage 不一致。当前保存地图中 {label} "
                f"位于第 {expected.get('row')} 行第 {expected.get('column')} 列，我会以这个位置继续分析。"
            )
        tile_names = {"@": "水域", "#": "墙体", ".": "地面"}
        actual = tile_names.get(first.get("actualTile"), "当前格子")
        return (
            f"你提到的第 {first.get('row')} 行第 {first.get('column')} 列与当前 Stage 不一致；"
            f"当前保存地图中这里是{actual}，我会以这个格子状态继续分析。"
        )
    if first.get("kind") == "entity":
        expected = first.get("expected") or {}
        if first.get("reason") == "entity_identity_unknown":
            return (
                f"The current Stage confirms an entity at the claimed cell, but cannot reliably map it to historical {first.get('entity')} identity. "
                "Please describe the entity by its current row and column so I can continue neutrally."
            )
        if not expected:
            return (
                f"The current Stage has no verifiable binding for {first.get('entity')}. "
                "Please describe the entity by its current row and column."
            )
        return (
            f"The position you gave for {first.get('entity')} does not match the current Stage. "
            f"The saved map places it at row {expected.get('row')}, column {expected.get('column')}; "
            "I will use that position for the analysis."
        )
    tile_names = {"@": "water", "#": "a wall", ".": "floor"}
    return (
        f"The tile you gave at row {first.get('row')}, column {first.get('column')} "
        f"does not match the current Stage; the saved map has {tile_names.get(first.get('actualTile'), 'a different tile')} there. "
        "I will use the saved tile for the analysis."
    )


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
