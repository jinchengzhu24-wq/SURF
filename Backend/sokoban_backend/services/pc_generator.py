from collections import deque

from ..config import (
    DEFAULT_LLM_MAX_ATTEMPTS,
    PC_DESIGN_MIN_ACTIVITY_AREA,
    PC_FALLBACK_MIN_INTERNAL_WALLS,
    PC_FEASIBILITY_MAX_SEARCH_STATES,
    PC_INTERMEDIATE_INTERNAL_WALLS,
    PC_LAYOUT_MAX_WATER_CHECKS,
    PC_LAYOUT_PREFILTER_SECONDS,
    PC_LEVEL_LLM_TIMEOUT_SECONDS,
    PC_MAX_LAYOUT_CANDIDATES,
    PC_MAX_WATER_AREA_CANDIDATES,
    PC_PRIMARY_INTERNAL_WALLS,
    env_float,
)
from ..llm.prompts import build_pc_level_generation_messages
from ..llm.runtime import LLMExecutionResult, LLMServiceError, safe_log_text
from ..runtime_state import app_attr, call_app_hook, current_perf_counter
from ..schemas import PCSolvabilityError


def create_pc_level_candidate(
    context,
    request_id="",
    max_attempts=DEFAULT_LLM_MAX_ATTEMPTS,
    context_is_normalized=False,
):
    if not context_is_normalized:
        context = call_app_hook("normalize_pc_level_request", context)

    def validate_candidate(payload):
        layout_candidate_id = parse_pc_layout_candidate_selection(payload)
        candidate = next(
            (
                item
                for item in context.get("layoutCandidates", [])
                if item["id"] == layout_candidate_id
            ),
            None,
        )

        if candidate is None:
            raise ValueError(
                f"layoutCandidateId {layout_candidate_id} is not available"
            )

        return {
            "rows": list(candidate["rows"]),
        }

    fallback_candidate = context["layoutCandidates"][0]

    try:
        return call_app_hook(
            "execute_json_request",
            task="pc_level_generation",
            messages=build_pc_level_generation_messages(context),
            validator=validate_candidate,
            temperature=env_float("DEEPSEEK_PC_LEVEL_TEMPERATURE", 0.15, 0.0, 2.0),
            timeout_seconds=env_float(
                "DEEPSEEK_PC_LEVEL_TIMEOUT_SECONDS",
                PC_LEVEL_LLM_TIMEOUT_SECONDS,
                0.5,
                120.0,
            ),
            max_attempts=1,
            thinking_mode="disabled",
            retry_error_codes=set(),
            request_id=request_id,
            validation_stage="pc_level_validation",
        )
    except LLMServiceError as exception:
        call_app_hook(
            "log_event",
            "INFO",
            "pc_layout_fallback_used",
            requestId=request_id or exception.request_id,
            reasonCode=exception.code,
            stage=exception.stage,
            layoutCandidateId=fallback_candidate["id"],
            internalWallCount=len(
                fallback_candidate["internalWallCellIds"]
            ),
        )
        return LLMExecutionResult(
            {"rows": list(fallback_candidate["rows"])},
            max(1, exception.attempts_used),
            request_id or exception.request_id,
        )


def parse_pc_layout_candidate_selection(payload):
    if not isinstance(payload, dict) or set(payload) != {"layoutCandidateId"}:
        raise ValueError(
            "PC layout selection must contain only layoutCandidateId"
        )

    layout_candidate_id = payload.get("layoutCandidateId")

    if isinstance(layout_candidate_id, bool) or not isinstance(
        layout_candidate_id,
        int,
    ):
        raise ValueError("layoutCandidateId must be an integer")

    return layout_candidate_id


def build_pc_level_candidate(
    layout,
    context,
    request_id="",
    minimum_internal_walls=0,
    minimum_water_areas=0,
):
    water_area_id, player_cell_id, internal_wall_cell_ids = (
        parse_pc_indexed_layout(layout)
    )

    width = context["width"]
    height = context["height"]
    sketch_rows = context["sketchRows"]
    enclosed = find_pc_enclosed_cells(sketch_rows, width, height)
    rows = [
        [
            "." if enclosed[y][x] and sketch_rows[y][x] == " " else sketch_rows[y][x]
            for x in range(width)
        ]
        for y in range(height)
    ]
    editable_cells = {
        cell["id"]: cell
        for cell in context.get("editableCells", [])
    }
    allowed_water_areas = {
        area["id"]: area
        for area in context.get("allowedWaterAreas", [])
    }
    water_area = allowed_water_areas.get(water_area_id)

    if water_area is None:
        raise ValueError(
            f"waterAreaId {water_area_id} is not an allowed water area ID"
        )

    water_cell_ids = set(water_area.get("cellIds") or [])
    player_cell = editable_cells.get(player_cell_id)

    if player_cell is None or not player_cell.get("canPlacePlayer"):
        raise ValueError(
            f"playerCellId {player_cell_id} is not an allowed player cell ID"
        )

    if player_cell_id in water_cell_ids:
        raise ValueError(
            f"playerCellId {player_cell_id} overlaps waterAreaId {water_area_id}"
        )

    if len(set(internal_wall_cell_ids)) != len(internal_wall_cell_ids):
        raise ValueError("internalWallCellIds must not contain duplicate IDs")

    if player_cell_id in internal_wall_cell_ids:
        raise ValueError(
            f"internalWallCellIds cannot contain playerCellId {player_cell_id}"
        )

    overlapping_water_ids = sorted(
        set(internal_wall_cell_ids) & water_cell_ids
    )

    if overlapping_water_ids:
        raise ValueError(
            "internalWallCellIds overlap the selected water area at cell IDs "
            + ",".join(str(value) for value in overlapping_water_ids)
        )

    wall_cells = []

    for cell_id in internal_wall_cell_ids:
        cell = editable_cells.get(cell_id)

        if cell is None or not cell.get("canPlaceWall"):
            raise ValueError(
                f"internalWallCellId {cell_id} is not an allowed wall cell ID"
            )

        wall_cells.append(cell)

    compatible_wall_cell_ids = set(
        water_area.get("compatibleWallCellIds") or []
    )
    incompatible_wall_ids = sorted(
        set(internal_wall_cell_ids) - compatible_wall_cell_ids
    )

    if incompatible_wall_ids:
        raise ValueError(
            "internalWallCellIds must come from compatibleWallCellIds for "
            f"waterAreaId {water_area_id}; incompatible IDs "
            + ",".join(str(value) for value in incompatible_wall_ids)
        )

    for cell_id in water_cell_ids:
        cell = editable_cells.get(cell_id)

        if cell is None:
            raise ValueError(
                f"waterAreaId {water_area_id} references unknown cell ID {cell_id}"
            )

        rows[cell["y"]][cell["x"]] = "@"

    rows[player_cell["y"]][player_cell["x"]] = "p"

    for cell in wall_cells:
        rows[cell["y"]][cell["x"]] = "#"

    candidate = validate_pc_level_candidate(
        {"rows": ["".join(row) for row in rows]},
        context,
    )
    validate_pc_required_features(
        candidate["rows"],
        sketch_rows,
        width,
        height,
        minimum_internal_walls,
        minimum_water_areas,
    )
    try:
        call_app_hook(
            "validate_pc_completed_level_solvability",
            candidate["rows"],
            width,
            height,
        )
    except PCSolvabilityError as exception:
        feedback = build_pc_solvability_rejection_feedback(
            layout,
            context,
            candidate["rows"],
            exception,
        )
        fields = {
            "reasonCode": exception.reason_code,
            "waterAreaId": water_area_id,
            "playerCellId": player_cell_id,
            "internalWallCellIds": internal_wall_cell_ids,
            "searchedStates": exception.searched_states,
            "detail": safe_log_text(feedback),
        }

        if request_id:
            fields["requestId"] = request_id

        call_app_hook("log_event", "INFO", "pc_candidate_rejected", **fields)
        raise ValueError(feedback) from exception

    return candidate


def build_pc_solvability_rejection_feedback(
    layout,
    context,
    rows,
    exception,
):
    water_area_id, player_cell_id, internal_wall_cell_ids = (
        parse_pc_indexed_layout(layout)
    )
    static_codes = {
        "BOX_NO_TARGET_ROUTE",
        "TARGET_MATCHING_IMPOSSIBLE",
        "NO_LEGAL_INITIAL_PUSH",
    }
    blocking_wall_cell_ids = []
    water_blocks_static_route = False

    if exception.reason_code in static_codes:
        walkable = {
            (x, y)
            for y in range(context["height"])
            for x in range(context["width"])
            if rows[y][x] in {".", "p", "s", "t"}
        }
        start_boxes = tuple(
            sorted(
                (x, y)
                for y in range(context["height"])
                for x in range(context["width"])
                if rows[y][x] == "s"
            )
        )
        targets = {
            (x, y)
            for y in range(context["height"])
            for x in range(context["width"])
            if rows[y][x] == "t"
        }
        players = [
            (x, y)
            for y in range(context["height"])
            for x in range(context["width"])
            if rows[y][x] == "p"
        ]
        editable_cells = {
            cell["id"]: cell
            for cell in context["editableCells"]
        }

        for cell_id in internal_wall_cell_ids:
            cell = editable_cells[cell_id]
            diagnostic = diagnose_pc_static_solvability(
                walkable | {(cell["x"], cell["y"])},
                start_boxes,
                targets,
                players,
            )

            if diagnostic is None:
                blocking_wall_cell_ids.append(cell_id)

        water_area = next(
            area
            for area in context["allowedWaterAreas"]
            if area["id"] == water_area_id
        )
        water_cells = {
            (
                editable_cells[cell_id]["x"],
                editable_cells[cell_id]["y"],
            )
            for cell_id in water_area["cellIds"]
        }
        water_blocks_static_route = diagnose_pc_static_solvability(
            walkable | water_cells,
            start_boxes,
            targets,
            players,
        ) is None

    detail = describe_pc_solvability_diagnostic(
        {
            "code": exception.reason_code,
            **exception.details,
        }
    )

    if water_blocks_static_route:
        correction = "choose a different waterAreaId"
    elif blocking_wall_cell_ids:
        correction = "replace the listed blocking internal wall cell IDs"
    elif exception.reason_code == "NO_LEGAL_INITIAL_PUSH":
        correction = "change playerCellId or remove obstacles near a box"
    elif exception.reason_code == "SEARCH_BUDGET_EXCEEDED":
        correction = "choose a simpler water and wall arrangement"
    else:
        correction = "change waterAreaId, playerCellId, or internalWallCellIds"

    parts = [
        "candidate has no Sokoban solution",
        f"reasonCode={exception.reason_code}",
        f"waterAreaId={water_area_id}",
        f"playerCellId={player_cell_id}",
        "internalWallCellIds=["
        + ",".join(str(value) for value in internal_wall_cell_ids)
        + "]",
        f"detail={detail}",
    ]

    if exception.searched_states:
        parts.append(f"searchedStates={exception.searched_states}")

    if blocking_wall_cell_ids:
        parts.append(
            "blockingWallCellIds=["
            + ",".join(str(value) for value in blocking_wall_cell_ids)
            + "]"
        )

    if water_blocks_static_route:
        parts.append("selectedWaterBlocksStaticRoute=true")

    parts.append(f"correction={correction}")
    return "; ".join(parts)


def parse_pc_indexed_layout(layout):
    if layout is None or not isinstance(layout, dict):
        raise ValueError("model returned no PC indexed layout")

    required_fields = {
        "waterAreaId",
        "playerCellId",
        "internalWallCellIds",
    }

    if set(layout) != required_fields:
        raise ValueError(
            "PC indexed layout must contain only waterAreaId, playerCellId, "
            "and internalWallCellIds"
        )

    internal_wall_cell_ids = layout.get("internalWallCellIds")

    if not isinstance(internal_wall_cell_ids, list):
        raise ValueError("internalWallCellIds must be an array")

    return (
        parse_pc_integer(layout.get("waterAreaId"), "waterAreaId"),
        parse_pc_integer(layout.get("playerCellId"), "playerCellId"),
        [
            parse_pc_integer(value, f"internalWallCellIds[{index}]")
            for index, value in enumerate(internal_wall_cell_ids)
        ],
    )


def parse_pc_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")

    return value


def normalize_pc_level_request(context):
    context = dict(context or {})
    width = context.get("width")
    height = context.get("height")
    rows = context.get("sketchRows")

    if width != 12 or height != 10:
        raise ValueError("PC sketch size must be exactly 12x10")

    validate_pc_rows(rows, width, height, {" ", "#", "s", "t"}, "sketchRows")
    box_count = sum(row.count("s") for row in rows)
    target_count = sum(row.count("t") for row in rows)

    if box_count < 1 or box_count > 2 or box_count != target_count:
        raise ValueError("PC sketch must contain one or two matching s/t pairs")

    enclosed = find_pc_enclosed_cells(rows, width, height)
    enclosed_cells = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if enclosed[y][x]
    }

    if len(enclosed_cells) < PC_DESIGN_MIN_ACTIVITY_AREA:
        raise ValueError(
            "PC sketch enclosed activity area must contain at least "
            f"{PC_DESIGN_MIN_ACTIVITY_AREA} cells"
        )

    if any(
        rows[y][x] in {"s", "t"} and (x, y) not in enclosed_cells
        for y in range(height)
        for x in range(width)
    ):
        raise ValueError("Every s and t must be inside the enclosed activity area")

    if count_pc_components(enclosed_cells) != 1:
        raise ValueError("PC sketch must contain exactly one enclosed activity area")

    validate_pc_start_clearance(rows, width, height)

    previous_rows = context.get("previousCandidateRows")

    if previous_rows == []:
        previous_rows = None

    if previous_rows is not None:
        validate_pc_rows(
            previous_rows,
            width,
            height,
            {" ", "#", ".", "@", "p", "s", "t"},
            "previousCandidateRows",
        )

    box_starts = [
        [x, y]
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "s"
    ]
    targets = [
        [x, y]
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "t"
    ]
    editable_coordinates = [
        [x, y]
        for y in range(height)
        for x in range(width)
        if enclosed[y][x] and rows[y][x] == " "
    ]
    cells_next_to_box_starts = {
        (neighbor_x, neighbor_y)
        for start_x, start_y in box_starts
        for neighbor_x, neighbor_y in (
            (start_x + 1, start_y),
            (start_x - 1, start_y),
            (start_x, start_y + 1),
            (start_x, start_y - 1),
        )
    }
    outer_shell_cells = find_pc_outer_shell_cells(
        rows,
        width,
        height,
    )
    cells_next_to_outer_shell = {
        (wall_x + direction_x, wall_y + direction_y)
        for wall_x, wall_y in outer_shell_cells
        for direction_x, direction_y in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
        )
    }
    allowed_wall_coordinates = [
        coordinate
        for coordinate in editable_coordinates
        if (
            tuple(coordinate) not in cells_next_to_box_starts
            and tuple(coordinate) not in cells_next_to_outer_shell
        )
    ]
    allowed_wall_coordinates = filter_pc_static_wall_coordinates(
        enclosed_cells,
        box_starts,
        targets,
        allowed_wall_coordinates,
    )
    allowed_wall_coordinate_set = {
        tuple(coordinate)
        for coordinate in allowed_wall_coordinates
    }
    wall_impact_scores = calculate_pc_wall_impact_scores(
        enclosed_cells,
        box_starts,
        targets,
        allowed_wall_coordinates,
    )
    editable_cells = [
        {
            "id": index,
            "x": coordinate[0],
            "y": coordinate[1],
            "canPlacePlayer": True,
            "canPlaceWall": tuple(coordinate) in allowed_wall_coordinate_set,
            "wallImpactScore": wall_impact_scores.get(tuple(coordinate), 0),
        }
        for index, coordinate in enumerate(editable_coordinates)
    ]
    all_allowed_water_areas = enumerate_pc_allowed_water_areas(
        rows,
        enclosed_cells,
        width,
        height,
    )

    ranked_water_areas = select_pc_water_area_candidates(
        all_allowed_water_areas,
        editable_cells,
        box_starts,
        targets,
        -1,
        len(all_allowed_water_areas),
    )
    ranked_water_areas = attach_pc_compatible_wall_ids(
        ranked_water_areas,
        editable_cells,
    )
    layout_candidates, checked_water_areas, searched_states = (
        build_pc_safe_layout_candidates(
            rows,
            enclosed_cells,
            ranked_water_areas[:PC_LAYOUT_MAX_WATER_CHECKS],
            editable_cells,
            box_starts,
            targets,
            previous_rows,
            width,
            height,
        )
    )

    if not layout_candidates:
        raise ValueError(
            "PC sketch has no safe completion with at least three internal "
            "walls and one water area"
        )

    call_app_hook(
        "log_event",
        "INFO",
        "pc_layout_prefilter_completed",
        checkedWaterAreas=checked_water_areas,
        layoutCandidateCount=len(layout_candidates),
        searchedStates=searched_states,
        preferredWallCount=len(layout_candidates[0]["internalWallCellIds"]),
    )

    return {
        "width": width,
        "height": height,
        "sketchRows": list(rows),
        "boxStarts": box_starts,
        "targets": targets,
        "editableCells": editable_cells,
        "editableCoordinates": editable_coordinates,
        "allowedWallCoordinates": allowed_wall_coordinates,
        "outerShellCells": [
            [x, y]
            for x, y in sorted(
                outer_shell_cells,
                key=lambda position: (position[1], position[0]),
            )
        ],
        "allowedWaterAreas": ranked_water_areas,
        "layoutCandidates": layout_candidates,
        "previousCandidateRows": (
            list(previous_rows) if previous_rows is not None else None
        ),
        "rejectionReason": str(context.get("rejectionReason") or "").strip()[:500],
    }


def validate_pc_level_candidate(payload, context):
    if payload is None or not isinstance(payload, dict):
        raise ValueError("model returned no PC level candidate")

    if set(payload) != {"rows"}:
        raise ValueError("PC level response must contain only rows")

    width = context["width"]
    height = context["height"]
    sketch_rows = context["sketchRows"]
    rows = payload.get("rows")
    validate_pc_rows(
        rows,
        width,
        height,
        {" ", "#", ".", "@", "p", "s", "t"},
        "rows",
    )
    enclosed = find_pc_enclosed_cells(sketch_rows, width, height)
    normalized_rows = [list(row) for row in rows]

    for y in range(height):
        for x in range(width):
            if (
                enclosed[y][x]
                and sketch_rows[y][x] == " "
                and normalized_rows[y][x] == " "
            ):
                normalized_rows[y][x] = "."

    rows = ["".join(row) for row in normalized_rows]

    for y in range(height):
        for x in range(width):
            source = sketch_rows[y][x]
            candidate = rows[y][x]

            if source in {"#", "s", "t"} and candidate != source:
                raise ValueError(f"candidate changed fixed tile at ({x},{y})")

            if not enclosed[y][x] and candidate != source:
                raise ValueError(
                    f"candidate changed a tile outside the activity area at ({x},{y})"
                )

            if enclosed[y][x] and source == " " and candidate not in {".", "#", "@", "p"}:
                raise ValueError(f"candidate left incomplete tile at ({x},{y})")

    for tile, label in (("s", "box starts"), ("t", "targets")):
        if sum(row.count(tile) for row in rows) != sum(
            row.count(tile) for row in sketch_rows
        ):
            raise ValueError(f"candidate changed the number of {label}")

    if sum(row.count("p") for row in rows) != 1:
        raise ValueError("candidate must contain exactly one p")

    validate_pc_water_wall_clearance(rows, width, height)
    validate_pc_generated_wall_shapes(
        rows,
        sketch_rows,
        width,
        height,
    )
    validate_pc_start_clearance(rows, width, height)
    validate_pc_candidate_activity(rows, width, height)

    return {"rows": list(rows)}


def validate_pc_water_wall_clearance(rows, width, height):
    for y in range(1, height):
        for x in range(width):
            if rows[y][x] == "@" and rows[y - 1][x] == "#":
                raise ValueError(
                    "water tile at "
                    f"({x},{y}) cannot have a wall directly above it"
                )


def validate_pc_generated_wall_shapes(
    rows,
    sketch_rows,
    width,
    height,
):
    generated_walls = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "#" and sketch_rows[y][x] != "#"
    }

    if contains_pc_two_by_two_block(generated_walls):
        raise ValueError(
            "generated internal walls must not contain a complete 2x2 block"
        )


def validate_pc_candidate_activity(rows, width, height):
    validate_pc_water_rectangles(rows, width, height)
    walkable = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] in {".", "p", "s", "t"}
    }

    if count_pc_components(walkable) != 1:
        raise ValueError("candidate walkable cells must form one connected component")


def validate_pc_required_features(
    rows,
    sketch_rows,
    width,
    height,
    minimum_internal_walls,
    minimum_water_areas,
):
    internal_wall_count = sum(
        1
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "#" and sketch_rows[y][x] != "#"
    )
    water_area_count = validate_pc_water_rectangles(rows, width, height)

    if internal_wall_count < minimum_internal_walls:
        raise ValueError(
            "candidate retained "
            f"{internal_wall_count} internal wall tiles; at least "
            f"{minimum_internal_walls} are required"
        )

    if water_area_count < minimum_water_areas:
        raise ValueError(
            "candidate retained "
            f"{water_area_count} water areas; at least "
            f"{minimum_water_areas} are required"
        )

    return internal_wall_count, water_area_count


def enumerate_pc_allowed_water_areas(
    sketch_rows,
    enclosed_cells,
    width,
    height,
):
    editable_cells = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if sketch_rows[y][x] == " " and (x, y) in enclosed_cells
    }
    allowed_areas = []

    for y in range(height):
        for x in range(width):
            for area_height in range(2, 5):
                for area_width in range(2, 5):
                    positions = {
                        (cell_x, cell_y)
                        for cell_y in range(y, y + area_height)
                        for cell_x in range(x, x + area_width)
                    }

                    if not positions.issubset(editable_cells):
                        continue

                    if any(
                        cell_y > 0
                        and sketch_rows[cell_y - 1][cell_x] == "#"
                        for cell_x, cell_y in positions
                    ):
                        continue

                    remaining_walkable = enclosed_cells - positions

                    if count_pc_components(remaining_walkable) != 1:
                        continue

                    allowed_areas.append(
                        {
                            "id": len(allowed_areas),
                            "x": x,
                            "y": y,
                            "width": area_width,
                            "height": area_height,
                        }
                    )

    return allowed_areas


def calculate_pc_wall_impact_scores(
    enclosed_cells,
    box_starts,
    targets,
    allowed_wall_coordinates,
):
    walkable = set(enclosed_cells)
    starts = [tuple(position) for position in box_starts]
    target_cells = [tuple(position) for position in targets]
    baseline_distances = {
        (start, target): find_pc_shortest_grid_distance(
            start,
            target,
            walkable,
        )
        for start in starts
        for target in target_cells
    }
    unreachable_penalty = max(1, len(walkable))
    scores = {}

    for coordinate in allowed_wall_coordinates:
        wall = tuple(coordinate)
        walkable_without_wall = walkable - {wall}
        score = 0

        for pair, baseline_distance in baseline_distances.items():
            if baseline_distance is None:
                continue

            changed_distance = find_pc_shortest_grid_distance(
                pair[0],
                pair[1],
                walkable_without_wall,
            )

            if changed_distance is None:
                score += unreachable_penalty
            else:
                score += max(0, changed_distance - baseline_distance)

        scores[wall] = score

    return scores


def find_pc_shortest_grid_distance(start, target, walkable):
    if start not in walkable or target not in walkable:
        return None

    open_cells = deque([(start, 0)])
    visited = {start}

    while open_cells:
        current, distance = open_cells.popleft()

        if current == target:
            return distance

        x, y = current

        for neighbor in (
            (x + 1, y),
            (x - 1, y),
            (x, y + 1),
            (x, y - 1),
        ):
            if neighbor in walkable and neighbor not in visited:
                visited.add(neighbor)
                open_cells.append((neighbor, distance + 1))

    return None


def filter_pc_static_wall_coordinates(
    enclosed_cells,
    box_starts,
    targets,
    allowed_wall_coordinates,
):
    start_boxes = tuple(sorted(tuple(position) for position in box_starts))
    target_set = {tuple(position) for position in targets}
    filtered = []

    for coordinate in allowed_wall_coordinates:
        wall = tuple(coordinate)
        remaining_walkable = set(enclosed_cells) - {wall}

        if count_pc_components(remaining_walkable) != 1:
            continue

        diagnostic = diagnose_pc_static_solvability(
            remaining_walkable,
            start_boxes,
            target_set,
        )

        if diagnostic is None:
            filtered.append(list(coordinate))

    return filtered


def select_pc_water_area_candidates(
    allowed_water_areas,
    editable_cells,
    box_starts,
    targets,
    preferred_area_id,
    limit=PC_MAX_WATER_AREA_CANDIDATES,
):
    limit = max(1, int(limit))
    coordinate_to_cell_id = {
        (cell["x"], cell["y"]): cell["id"]
        for cell in editable_cells
    }
    wall_cell_ids = {
        cell["id"]
        for cell in editable_cells
        if cell.get("canPlaceWall")
    }
    fixed_positions = [
        tuple(position)
        for position in list(box_starts) + list(targets)
    ]
    enriched_areas = []

    for area in allowed_water_areas:
        cell_ids = [
            coordinate_to_cell_id[(cell_x, cell_y)]
            for cell_y in range(area["y"], area["y"] + area["height"])
            for cell_x in range(area["x"], area["x"] + area["width"])
        ]
        water_positions = [
            (cell_x, cell_y)
            for cell_y in range(area["y"], area["y"] + area["height"])
            for cell_x in range(area["x"], area["x"] + area["width"])
        ]
        minimum_fixed_distance = min(
            (
                abs(cell_x - fixed_x) + abs(cell_y - fixed_y)
                for cell_x, cell_y in water_positions
                for fixed_x, fixed_y in fixed_positions
            ),
            default=999,
        )
        enriched_areas.append(
            {
                **area,
                "cellIds": cell_ids,
                "_areaSize": len(cell_ids),
                "_minimumFixedDistance": minimum_fixed_distance,
                "_remainingWallOptions": len(wall_cell_ids - set(cell_ids)),
            }
        )

    groups = {}

    for area in enriched_areas:
        groups.setdefault(area["_areaSize"], []).append(area)

    for group in groups.values():
        group.sort(
            key=lambda area: (
                -area["_minimumFixedDistance"],
                -area["_remainingWallOptions"],
                area["y"],
                area["x"],
                area["height"],
                area["width"],
            )
        )

    selected = []
    preferred_area = next(
        (
            area
            for area in enriched_areas
            if area["id"] == preferred_area_id
        ),
        None,
    )

    if preferred_area is not None:
        selected.append(preferred_area)

    group_offsets = {
        area_size: 0
        for area_size in groups
    }
    area_sizes = sorted(groups)

    while len(selected) < min(limit, len(enriched_areas)):
        added = False

        for area_size in area_sizes:
            group = groups[area_size]
            offset = group_offsets[area_size]

            while offset < len(group) and group[offset] in selected:
                offset += 1

            group_offsets[area_size] = offset

            if offset >= len(group):
                continue

            selected.append(group[offset])
            group_offsets[area_size] += 1
            added = True

            if len(selected) >= min(limit, len(enriched_areas)):
                break

        if not added:
            break

    return [
        {
            "id": index,
            "x": area["x"],
            "y": area["y"],
            "width": area["width"],
            "height": area["height"],
            "cellIds": list(area["cellIds"]),
        }
        for index, area in enumerate(selected)
    ]


def attach_pc_compatible_wall_ids(allowed_water_areas, editable_cells):
    allowed_wall_ids = {
        cell["id"]
        for cell in editable_cells
        if cell.get("canPlaceWall")
    }
    editable_by_position = {
        (cell["x"], cell["y"]): cell["id"]
        for cell in editable_cells
    }
    result = []

    for area in allowed_water_areas:
        water_cell_ids = set(area.get("cellIds") or [])
        water_positions = {
            (cell["x"], cell["y"])
            for cell in editable_cells
            if cell["id"] in water_cell_ids
        }
        wall_ids_above_water = {
            editable_by_position[(water_x, water_y - 1)]
            for water_x, water_y in water_positions
            if (water_x, water_y - 1) in editable_by_position
        }
        result.append(
            {
                **area,
                "compatibleWallCellIds": sorted(
                    allowed_wall_ids
                    - water_cell_ids
                    - wall_ids_above_water
                ),
            }
        )

    return result


def build_pc_safe_layout_candidates(
    sketch_rows,
    enclosed_cells,
    allowed_water_areas,
    editable_cells,
    box_starts,
    targets,
    previous_rows,
    width,
    height,
):
    started = current_perf_counter()
    max_states = int(
        app_attr("PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES")
        or PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES
    )
    budget = {
        "deadline": started + PC_LAYOUT_PREFILTER_SECONDS,
        "remainingStates": max_states,
        "searchedStates": 0,
    }
    editable_by_id = {
        cell["id"]: cell
        for cell in editable_cells
    }
    start_boxes = tuple(sorted(tuple(position) for position in box_starts))
    target_cells = {tuple(position) for position in targets}
    candidates = []
    checked_water_areas = 0

    for area in allowed_water_areas[:PC_LAYOUT_MAX_WATER_CHECKS]:
        if (
            len(candidates) >= PC_MAX_LAYOUT_CANDIDATES
            or current_perf_counter() >= budget["deadline"]
            or budget["remainingStates"] <= 0
        ):
            break

        checked_water_areas += 1
        water_cell_ids = set(area.get("cellIds") or [])
        water_positions = {
            (
                editable_by_id[cell_id]["x"],
                editable_by_id[cell_id]["y"],
            )
            for cell_id in water_cell_ids
            if cell_id in editable_by_id
        }
        walkable = set(enclosed_cells) - water_positions

        if count_pc_components(walkable) != 1:
            continue

        if diagnose_pc_static_solvability(
            walkable,
            start_boxes,
            target_cells,
        ) is not None:
            continue

        player_and_trace = find_pc_safe_player_and_solution_trace(
            walkable,
            start_boxes,
            target_cells,
            editable_cells,
            water_cell_ids,
            budget,
        )

        if player_and_trace is None:
            continue

        player_cell_id, protected_positions = player_and_trace
        compatible_wall_cell_ids = set(
            area.get("compatibleWallCellIds") or []
        )
        safe_wall_cells = [
            cell
            for cell in editable_cells
            if (
                cell.get("canPlaceWall")
                and cell["id"] in compatible_wall_cell_ids
                and cell["id"] not in water_cell_ids
                and cell["id"] != player_cell_id
                and (cell["x"], cell["y"]) not in protected_positions
            )
        ]
        wall_groups = build_pc_greedy_wall_groups(
            safe_wall_cells,
            walkable,
        )

        for wall_group in wall_groups:
            if len(candidates) >= PC_MAX_LAYOUT_CANDIDATES:
                break

            rows = assemble_pc_layout_rows(
                sketch_rows,
                enclosed_cells,
                editable_by_id,
                water_cell_ids,
                player_cell_id,
                wall_group["internalWallCellIds"],
                width,
                height,
            )
            candidate = validate_pc_level_candidate(
                {"rows": rows},
                {
                    "width": width,
                    "height": height,
                    "sketchRows": sketch_rows,
                },
            )
            player_cell = editable_by_id[player_cell_id]
            wall_cells = [
                editable_by_id[cell_id]
                for cell_id in wall_group["internalWallCellIds"]
            ]
            candidates.append(
                {
                    "id": len(candidates),
                    "waterAreaId": area["id"],
                    "playerCellId": player_cell_id,
                    "internalWallCellIds": list(
                        wall_group["internalWallCellIds"]
                    ),
                    "wallStyle": wall_group["wallStyle"],
                    "waterArea": {
                        "x": area["x"],
                        "y": area["y"],
                        "width": area["width"],
                        "height": area["height"],
                    },
                    "player": {
                        "x": player_cell["x"],
                        "y": player_cell["y"],
                    },
                    "internalWalls": [
                        {"x": cell["x"], "y": cell["y"]}
                        for cell in wall_cells
                    ],
                    "rows": list(candidate["rows"]),
                }
            )

    if previous_rows:
        different_candidates = [
            candidate
            for candidate in candidates
            if candidate["rows"] != list(previous_rows)
        ]

        if different_candidates:
            candidates = different_candidates

    style_rank = {
        "bent": 0,
        "split": 1,
        "dispersed": 2,
        "straight": 3,
    }
    candidates.sort(
        key=lambda candidate: (
            -len(candidate["internalWallCellIds"]),
            style_rank.get(candidate["wallStyle"], 99),
            candidate["waterAreaId"],
            candidate["internalWallCellIds"],
        )
    )

    for index, candidate in enumerate(candidates):
        candidate["id"] = index

    elapsed_ms = round((current_perf_counter() - started) * 1000)
    call_app_hook(
        "log_event",
        "INFO",
        "pc_layout_prefilter_timing",
        elapsedMs=elapsed_ms,
        checkedWaterAreas=checked_water_areas,
        layoutCandidateCount=len(candidates),
        searchedStates=budget["searchedStates"],
        budgetExhausted=(
            current_perf_counter() >= budget["deadline"]
            or budget["remainingStates"] <= 0
        ),
    )
    return candidates, checked_water_areas, budget["searchedStates"]


def find_pc_safe_player_and_solution_trace(
    walkable,
    start_boxes,
    targets,
    editable_cells,
    water_cell_ids,
    budget,
):
    box_set = set(start_boxes)
    remaining_player_ids = {
        cell["id"]
        for cell in editable_cells
        if (
            cell["id"] not in water_cell_ids
            and (cell["x"], cell["y"]) in walkable
            and (cell["x"], cell["y"]) not in box_set
        )
    }

    for representative in find_pc_region_representatives(
        walkable,
        box_set,
    ):
        reachable = find_pc_reachable_cells(
            representative,
            walkable,
            box_set,
        )
        region_player_cells = sorted(
            (
                cell
                for cell in editable_cells
                if (
                    cell["id"] in remaining_player_ids
                    and (cell["x"], cell["y"]) in reachable
                )
            ),
            key=lambda cell: cell["id"],
        )

        if not region_player_cells:
            continue

        player_cell = region_player_cells[0]

        try:
            protected_positions = search_pc_solution_trace(
                walkable,
                start_boxes,
                targets,
                (player_cell["x"], player_cell["y"]),
                budget,
            )
        except PCSolvabilityError:
            continue

        return player_cell["id"], protected_positions

    return None


def search_pc_solution_trace(
    walkable,
    start_boxes,
    targets,
    player,
    budget,
):
    start_state = (player, tuple(sorted(start_boxes)))
    open_states = deque([start_state])
    parents = {start_state: None}

    while open_states:
        if (
            budget["remainingStates"] <= 0
            or current_perf_counter() >= budget["deadline"]
        ):
            raise PCSolvabilityError(
                "SEARCH_BUDGET_EXCEEDED",
                "PC safe-layout search exceeded its shared preprocessing budget",
                searched_states=budget["searchedStates"],
            )

        current_player, boxes = open_states.popleft()
        budget["remainingStates"] -= 1
        budget["searchedStates"] += 1
        state = (current_player, boxes)

        if set(boxes) == targets:
            protected = set()
            current = state

            while current is not None:
                current_state_player, current_state_boxes = current
                protected.add(current_state_player)
                protected.update(current_state_boxes)
                current = parents[current]

            return protected

        box_set = set(boxes)

        for direction_x, direction_y in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
        ):
            next_player = (
                current_player[0] + direction_x,
                current_player[1] + direction_y,
            )

            if next_player not in walkable:
                continue

            next_boxes = boxes

            if next_player in box_set:
                next_box = (
                    next_player[0] + direction_x,
                    next_player[1] + direction_y,
                )

                if next_box not in walkable or next_box in box_set:
                    continue

                moved_boxes = list(boxes)
                moved_boxes[moved_boxes.index(next_player)] = next_box
                next_boxes = tuple(sorted(moved_boxes))

            next_state = (next_player, next_boxes)

            if next_state not in parents:
                parents[next_state] = state
                open_states.append(next_state)

    raise PCSolvabilityError(
        "NO_SOLUTION_AFTER_SEARCH",
        "PC water-only candidate has no Sokoban solution",
        searched_states=budget["searchedStates"],
    )


def build_pc_greedy_wall_groups(
    safe_wall_cells,
    walkable,
):
    groups = []

    for required_count in (
        PC_PRIMARY_INTERNAL_WALLS,
        PC_INTERMEDIATE_INTERNAL_WALLS,
        PC_FALLBACK_MIN_INTERNAL_WALLS,
    ):
        group = choose_pc_greedy_wall_group(
            safe_wall_cells,
            walkable,
            required_count,
        )

        if group:
            groups.append(group)

    return groups


def choose_pc_greedy_wall_group(
    safe_wall_cells,
    walkable,
    required_count,
):
    if len(safe_wall_cells) < required_count:
        return None

    ordered_cells = sorted(
        safe_wall_cells,
        key=lambda cell: (
            -int(cell.get("wallImpactScore") or 0),
            cell["id"],
        ),
    )
    variants = []

    for seed in ordered_cells:
        selected = [seed]

        while len(selected) < required_count:
            best_cell = None
            best_score = None

            for candidate in ordered_cells:
                if candidate in selected:
                    continue

                proposed = selected + [candidate]
                proposed_positions = {
                    (cell["x"], cell["y"])
                    for cell in proposed
                }

                if (
                    contains_pc_two_by_two_block(proposed_positions)
                    or count_pc_components(
                        set(walkable) - proposed_positions
                    ) != 1
                ):
                    continue

                style = classify_pc_wall_style(proposed)
                pair_distances = [
                    abs(left["x"] - right["x"])
                    + abs(left["y"] - right["y"])
                    for index, left in enumerate(proposed)
                    for right in proposed[index + 1:]
                ]
                adjacency_count = sum(
                    distance == 1
                    for distance in pair_distances
                )
                style_bonus = {
                    "bent": 80,
                    "split": 70,
                    "dispersed": 60,
                    "straight": 0,
                }.get(style, 0)
                score = (
                    style_bonus,
                    adjacency_count,
                    min(pair_distances, default=0),
                    int(candidate.get("wallImpactScore") or 0),
                    -candidate["id"],
                )

                if best_score is None or score > best_score:
                    best_score = score
                    best_cell = candidate

            if best_cell is None:
                break

            selected.append(best_cell)

        if len(selected) != required_count:
            continue

        style = classify_pc_wall_style(selected)
        variants.append(
            {
                "internalWallCellIds": sorted(
                    cell["id"]
                    for cell in selected
                ),
                "wallStyle": style,
            }
        )

    if not variants:
        return None

    style_rank = {
        "bent": 0,
        "split": 1,
        "dispersed": 2,
        "straight": 3,
    }
    variants.sort(
        key=lambda variant: (
            style_rank.get(variant["wallStyle"], 99),
            variant["internalWallCellIds"],
        )
    )
    return variants[0]


def contains_pc_two_by_two_block(positions):
    positions = set(positions)

    return any(
        {
            (x, y),
            (x + 1, y),
            (x, y + 1),
            (x + 1, y + 1),
        }.issubset(positions)
        for x, y in positions
    )


def classify_pc_wall_style(wall_cells):
    positions = {
        (cell["x"], cell["y"])
        for cell in wall_cells
    }

    if len(positions) >= 3 and (
        len({x for x, _ in positions}) == 1
        or len({y for _, y in positions}) == 1
    ):
        return "straight"

    component_count = count_pc_components(positions)

    if component_count == 1:
        return "bent"

    if component_count == 2:
        return "split"

    return "dispersed"


def assemble_pc_layout_rows(
    sketch_rows,
    enclosed_cells,
    editable_by_id,
    water_cell_ids,
    player_cell_id,
    internal_wall_cell_ids,
    width,
    height,
):
    rows = [
        [
            "." if (x, y) in enclosed_cells and sketch_rows[y][x] == " "
            else sketch_rows[y][x]
            for x in range(width)
        ]
        for y in range(height)
    ]

    for cell_id in water_cell_ids:
        cell = editable_by_id[cell_id]
        rows[cell["y"]][cell["x"]] = "@"

    player_cell = editable_by_id[player_cell_id]
    rows[player_cell["y"]][player_cell["x"]] = "p"

    for cell_id in internal_wall_cell_ids:
        cell = editable_by_id[cell_id]
        rows[cell["y"]][cell["x"]] = "#"

    return ["".join(row) for row in rows]


def validate_pc_rows(rows, width, height, allowed_tiles, field_name):
    if not isinstance(rows, list) or len(rows) != height:
        raise ValueError(f"{field_name} must contain exactly {height} rows")

    for row_index, row in enumerate(rows):
        if not isinstance(row, str) or len(row) != width:
            raise ValueError(
                f"{field_name}[{row_index}] must contain exactly {width} characters"
            )

        invalid = next((tile for tile in row if tile not in allowed_tiles), None)

        if invalid is not None:
            raise ValueError(f"{field_name} contains unsupported tile {invalid!r}")


def find_pc_enclosed_cells(rows, width, height):
    outside = [[False for _ in range(width)] for _ in range(height)]
    open_cells = []

    def enqueue(x, y):
        if (
            x < 0
            or x >= width
            or y < 0
            or y >= height
            or outside[y][x]
            or rows[y][x] == "#"
        ):
            return

        outside[y][x] = True
        open_cells.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)

    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    index = 0

    while index < len(open_cells):
        x, y = open_cells[index]
        index += 1
        enqueue(x + 1, y)
        enqueue(x - 1, y)
        enqueue(x, y + 1)
        enqueue(x, y - 1)

    return [
        [rows[y][x] != "#" and not outside[y][x] for x in range(width)]
        for y in range(height)
    ]


def find_pc_outer_shell_cells(rows, width, height):
    outside = set()
    open_cells = deque()

    def enqueue(x, y):
        position = (x, y)

        if (
            x < 0
            or x >= width
            or y < 0
            or y >= height
            or position in outside
            or rows[y][x] == "#"
        ):
            return

        outside.add(position)
        open_cells.append(position)

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)

    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    while open_cells:
        x, y = open_cells.popleft()

        for neighbor_x, neighbor_y in (
            (x + 1, y),
            (x - 1, y),
            (x, y + 1),
            (x, y - 1),
        ):
            enqueue(neighbor_x, neighbor_y)

    shell_cells = set()

    for y in range(height):
        for x in range(width):
            if rows[y][x] != "#":
                continue

            for neighbor_x, neighbor_y in (
                (x + 1, y),
                (x - 1, y),
                (x, y + 1),
                (x, y - 1),
            ):
                if (
                    neighbor_x < 0
                    or neighbor_x >= width
                    or neighbor_y < 0
                    or neighbor_y >= height
                    or (neighbor_x, neighbor_y) in outside
                ):
                    shell_cells.add((x, y))
                    break

    return shell_cells


def count_pc_components(cells):
    remaining = set(cells)
    component_count = 0

    while remaining:
        component_count += 1
        open_cells = [remaining.pop()]

        while open_cells:
            x, y = open_cells.pop()

            for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    open_cells.append(neighbor)

    return component_count


def validate_pc_start_clearance(rows, width, height):
    for y in range(height):
        for x in range(width):
            if rows[y][x] != "s":
                continue

            for neighbor_x, neighbor_y in (
                (x + 1, y),
                (x - 1, y),
                (x, y + 1),
                (x, y - 1),
            ):
                if (
                    0 <= neighbor_x < width
                    and 0 <= neighbor_y < height
                    and rows[neighbor_y][neighbor_x] == "#"
                ):
                    raise ValueError(
                        "box start at row "
                        f"{y + 1}, column {x + 1} cannot touch a wall"
                    )


def find_pc_reverse_box_reachable_cells(target, walkable):
    reachable = {target}
    open_cells = [target]

    while open_cells:
        current_x, current_y = open_cells.pop()

        for direction_x, direction_y in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
        ):
            previous = (
                current_x - direction_x,
                current_y - direction_y,
            )
            standing = (
                previous[0] - direction_x,
                previous[1] - direction_y,
            )

            if (
                previous in walkable
                and standing in walkable
                and previous not in reachable
            ):
                reachable.add(previous)
                open_cells.append(previous)

    return reachable


def has_pc_box_target_matching(box_target_options, box_index=0, used_targets=None):
    if box_index >= len(box_target_options):
        return True

    used_targets = set(used_targets or ())

    for target in box_target_options[box_index]:
        if target in used_targets:
            continue

        if has_pc_box_target_matching(
            box_target_options,
            box_index + 1,
            used_targets | {target},
        ):
            return True

    return False


def has_pc_legal_initial_push(walkable, start_boxes, initial_players):
    box_set = set(start_boxes)

    for player in initial_players:
        reachable = find_pc_reachable_cells(player, walkable, box_set)

        for box_x, box_y in start_boxes:
            for direction_x, direction_y in (
                (1, 0),
                (-1, 0),
                (0, 1),
                (0, -1),
            ):
                standing = (
                    box_x - direction_x,
                    box_y - direction_y,
                )
                destination = (
                    box_x + direction_x,
                    box_y + direction_y,
                )

                if (
                    standing in reachable
                    and destination in walkable
                    and destination not in box_set
                ):
                    return True

    return False


def diagnose_pc_static_solvability(
    walkable,
    start_boxes,
    targets,
    initial_players=None,
):
    walkable = set(walkable)
    start_boxes = tuple(sorted(start_boxes))
    targets = set(targets)
    reverse_reachable = {
        target: find_pc_reverse_box_reachable_cells(target, walkable)
        for target in targets
    }
    box_target_options = []

    for box in start_boxes:
        options = sorted(
            target
            for target, reachable in reverse_reachable.items()
            if box in reachable
        )

        if not options:
            return {
                "code": "BOX_NO_TARGET_ROUTE",
                "box": box,
            }

        box_target_options.append(options)

    if not has_pc_box_target_matching(box_target_options):
        return {
            "code": "TARGET_MATCHING_IMPOSSIBLE",
            "boxTargetOptions": [
                {
                    "box": start_boxes[index],
                    "targets": list(options),
                }
                for index, options in enumerate(box_target_options)
            ],
        }

    if (
        initial_players is not None
        and set(start_boxes) != targets
        and not has_pc_legal_initial_push(
            walkable,
            start_boxes,
            initial_players,
        )
    ):
        return {
            "code": "NO_LEGAL_INITIAL_PUSH",
        }

    return None


def describe_pc_solvability_diagnostic(diagnostic):
    code = diagnostic.get("code", "NO_SOLUTION_AFTER_SEARCH")

    if code == "BOX_NO_TARGET_ROUTE":
        box_x, box_y = diagnostic["box"]
        return (
            f"box start ({box_x},{box_y}) has no static push route to any target"
        )

    if code == "TARGET_MATCHING_IMPOSSIBLE":
        return "box-to-target routes cannot form a complete matching"

    if code == "NO_LEGAL_INITIAL_PUSH":
        return "the selected player position has no legal initial box push"

    if code == "SEARCH_BUDGET_EXCEEDED":
        return "candidate solvability search exceeded its state budget"

    return "complete search found no Sokoban solution"


def validate_pc_open_sketch_feasibility(
    rows,
    enclosed_cells,
    width,
    height,
    maximum_search_states=PC_FEASIBILITY_MAX_SEARCH_STATES,
):
    walkable = set(enclosed_cells)
    start_boxes = tuple(
        sorted(
            (x, y)
            for y in range(height)
            for x in range(width)
            if rows[y][x] == "s"
        )
    )
    targets = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "t"
    }
    initial_regions = find_pc_region_representatives(
        walkable,
        set(start_boxes),
    )

    if not initial_regions:
        raise ValueError("PC sketch has no possible player start")

    return validate_pc_push_solvability(
        walkable,
        start_boxes,
        targets,
        initial_regions,
        maximum_search_states,
        "PC sketch open-map solvability check exceeded its search budget",
        (
            "PC sketch has no solvable open completion; "
            "move box starts, targets, or walls"
        ),
    )


def validate_pc_completed_level_solvability(
    rows,
    width,
    height,
    maximum_search_states=PC_FEASIBILITY_MAX_SEARCH_STATES,
):
    walkable = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] in {".", "p", "s", "t"}
    }
    start_boxes = tuple(
        sorted(
            (x, y)
            for y in range(height)
            for x in range(width)
            if rows[y][x] == "s"
        )
    )
    targets = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "t"
    }
    players = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "p"
    ]

    if len(players) != 1:
        raise ValueError("candidate must contain exactly one player start")

    diagnostic = diagnose_pc_static_solvability(
        walkable,
        start_boxes,
        targets,
        players,
    )

    if diagnostic is not None:
        raise PCSolvabilityError(
            diagnostic["code"],
            "candidate has no Sokoban solution: "
            + describe_pc_solvability_diagnostic(diagnostic),
            details=diagnostic,
        )

    return validate_pc_push_solvability(
        walkable,
        start_boxes,
        targets,
        players,
        maximum_search_states,
        "candidate solvability check exceeded its search budget",
        "candidate has no Sokoban solution",
    )


def validate_pc_push_solvability(
    walkable,
    start_boxes,
    targets,
    initial_players,
    maximum_search_states,
    budget_error,
    unsolvable_error,
):
    _, searched_states = search_pc_minimum_pushes(
        walkable,
        start_boxes,
        targets,
        initial_players,
        maximum_search_states,
        budget_error,
        unsolvable_error,
    )
    return searched_states


def search_pc_minimum_pushes(
    walkable,
    start_boxes,
    targets,
    initial_players,
    maximum_search_states,
    budget_error,
    unsolvable_error,
):
    open_states = deque()
    visited_states = set()

    for player in initial_players:
        key = build_pc_push_state_key(player, start_boxes, walkable)

        if key not in visited_states:
            visited_states.add(key)
            open_states.append((player, start_boxes, 0))

    searched_states = 0
    search_limit = max(1, int(maximum_search_states))

    while open_states and searched_states < search_limit:
        player, boxes, pushes = open_states.popleft()
        searched_states += 1

        if set(boxes) == targets:
            return pushes, searched_states

        box_set = set(boxes)
        reachable = find_pc_reachable_cells(player, walkable, box_set)

        for box_index, box in enumerate(boxes):
            for direction_x, direction_y in (
                (1, 0),
                (-1, 0),
                (0, 1),
                (0, -1),
            ):
                standing_cell = (
                    box[0] - direction_x,
                    box[1] - direction_y,
                )
                destination = (
                    box[0] + direction_x,
                    box[1] + direction_y,
                )

                if (
                    standing_cell not in reachable
                    or destination not in walkable
                    or destination in box_set
                ):
                    continue

                next_boxes = list(boxes)
                next_boxes[box_index] = destination
                next_boxes = tuple(sorted(next_boxes))
                next_player = box
                key = build_pc_push_state_key(
                    next_player,
                    next_boxes,
                    walkable,
                )

                if key not in visited_states:
                    visited_states.add(key)
                    open_states.append((next_player, next_boxes, pushes + 1))

    if open_states:
        raise PCSolvabilityError(
            "SEARCH_BUDGET_EXCEEDED",
            budget_error,
            searched_states=searched_states,
        )

    raise PCSolvabilityError(
        "NO_SOLUTION_AFTER_SEARCH",
        unsolvable_error,
        searched_states=searched_states,
    )


def find_pc_region_representatives(walkable, boxes):
    remaining = set(walkable) - set(boxes)
    representatives = []

    while remaining:
        representative = min(remaining, key=lambda position: (position[1], position[0]))
        representatives.append(representative)
        open_cells = [representative]
        remaining.remove(representative)

        while open_cells:
            x, y = open_cells.pop()

            for neighbor in (
                (x + 1, y),
                (x - 1, y),
                (x, y + 1),
                (x, y - 1),
            ):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    open_cells.append(neighbor)

    return representatives


def find_pc_reachable_cells(player, walkable, boxes):
    if player not in walkable or player in boxes:
        return set()

    reachable = {player}
    open_cells = [player]

    while open_cells:
        x, y = open_cells.pop()

        for neighbor in (
            (x + 1, y),
            (x - 1, y),
            (x, y + 1),
            (x, y - 1),
        ):
            if (
                neighbor in walkable
                and neighbor not in boxes
                and neighbor not in reachable
            ):
                reachable.add(neighbor)
                open_cells.append(neighbor)

    return reachable


def build_pc_push_state_key(player, boxes, walkable):
    reachable = find_pc_reachable_cells(player, walkable, set(boxes))
    representative = min(
        reachable,
        key=lambda position: (position[1], position[0]),
    )
    return boxes, representative


def validate_pc_water_rectangles(rows, width, height):
    remaining = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "@"
    }
    water_area_count = 0

    while remaining:
        water_area_count += 1
        start = remaining.pop()
        component = {start}
        open_cells = [start]

        while open_cells:
            x, y = open_cells.pop()

            for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    open_cells.append(neighbor)

        xs = [position[0] for position in component]
        ys = [position[1] for position in component]
        area_width = max(xs) - min(xs) + 1
        area_height = max(ys) - min(ys) + 1

        if (
            area_width < 2
            or area_width > 4
            or area_height < 2
            or area_height > 4
            or len(component) != area_width * area_height
        ):
            raise ValueError("every water area must be a complete 2-4 by 2-4 rectangle")

    return water_area_count
