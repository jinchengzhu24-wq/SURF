import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

try:
    from . import app as backend
    from .llm_runtime import LLMExecutionResult
except ImportError:
    import app as backend
    from llm_runtime import LLMExecutionResult


def make_sketch():
    return [
        "            ",
        " ########## ",
        " #        # ",
        " # s    t # ",
        " #        # ",
        " #        # ",
        " #        # ",
        " #        # ",
        " #        # ",
        " ########## ",
    ]


def make_candidate():
    return [
        "            ",
        " ########## ",
        " #p.......# ",
        " #.s....t.# ",
        " #........# ",
        " #........# ",
        " #........# ",
        " #........# ",
        " #........# ",
        " ########## ",
    ]


def find_cell_id(context, x, y):
    return next(
        cell["id"]
        for cell in context["editableCells"]
        if (cell["x"], cell["y"]) == (x, y)
    )


def find_water_area_id(context, x, y, width, height):
    return next(
        area["id"]
        for area in context["allowedWaterAreas"]
        if (
            area["x"],
            area["y"],
            area["width"],
            area["height"],
        )
        == (x, y, width, height)
    )


def make_layout(
    context,
    player=(2, 2),
    walls=(),
    water=(5, 7, 2, 2),
):
    return {
        "waterAreaId": find_water_area_id(context, *water),
        "playerCellId": find_cell_id(context, *player),
        "internalWallCellIds": [
            find_cell_id(context, x, y)
            for x, y in walls
        ],
    }


def make_required_layout(context, wall_count=5, wall_set_index=0):
    requested_set_size = (
        wall_count
        if wall_count in {3, 4, 5}
        else 3
    )
    matching_layouts = [
        {
            "waterAreaId": candidate["waterAreaId"],
            "playerCellId": candidate["playerCellId"],
            "internalWallCellIds": list(
                candidate["internalWallCellIds"]
            )[:wall_count],
        }
        for candidate in context["layoutCandidates"]
        if len(candidate["internalWallCellIds"]) == requested_set_size
    ]

    if matching_layouts:
        return matching_layouts[wall_set_index]

    raise AssertionError(
        f"fixture has no layout candidate with {requested_set_size} walls"
    )


class PCLevelGenerationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend.app)
        self.context = {
            "width": 12,
            "height": 10,
            "sketchRows": make_sketch(),
        }

    def test_valid_candidate_preserves_sketch_contract(self):
        result = backend.validate_pc_level_candidate(
            {"rows": make_candidate()},
            backend.normalize_pc_level_request(self.context),
        )

        self.assertEqual(result["rows"], make_candidate())

    def test_incomplete_inner_space_is_normalized_to_ground(self):
        candidate = make_candidate()
        candidate[2] = " #p... ...# "

        result = backend.validate_pc_level_candidate(
            {"rows": candidate},
            backend.normalize_pc_level_request(self.context),
        )

        self.assertEqual(result["rows"][2], " #p.......# ")

    def test_indexed_layout_builds_complete_rows(self):
        context = backend.normalize_pc_level_request(self.context)
        result = backend.build_pc_level_candidate(
            make_layout(context),
            context,
        )

        self.assertEqual(result["rows"][2][2], "p")
        self.assertEqual(result["rows"][7][5:7], "@@")
        self.assertEqual(result["rows"][8][5:7], "@@")
        self.assertTrue(all(len(row) == 12 for row in result["rows"]))

    def test_indexed_layout_builds_valid_wall_and_water(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(
            context,
            walls=((6, 3),),
            water=(5, 7, 2, 2),
        )

        result = backend.build_pc_level_candidate(
            layout,
            context,
        )

        self.assertEqual(result["rows"][2], " #p.......# ")
        self.assertEqual(result["rows"][3], " #.s..#.t.# ")
        self.assertEqual(result["rows"][7], " #...@@...# ")
        self.assertEqual(result["rows"][8], " #...@@...# ")

    def test_water_area_directly_below_fixed_wall_is_not_enumerated(self):
        context = backend.normalize_pc_level_request(self.context)
        water_rectangles = {
            (
                area["x"],
                area["y"],
                area["width"],
                area["height"],
            )
            for area in context["allowedWaterAreas"]
        }

        self.assertNotIn((4, 2, 2, 2), water_rectangles)
        self.assertIn((5, 7, 2, 2), water_rectangles)

    def test_indexed_layout_rejects_generated_wall_above_water(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(
            context,
            walls=((5, 6),),
            water=(5, 7, 2, 2),
        )

        with self.assertRaisesRegex(ValueError, "compatibleWallCellIds"):
            backend.build_pc_level_candidate(layout, context)

    def test_candidate_rejects_water_directly_below_wall(self):
        candidate = [list(row) for row in make_candidate()]

        for x, y in ((5, 2), (6, 2), (5, 3), (6, 3)):
            candidate[y][x] = "@"

        with self.assertRaisesRegex(ValueError, "wall directly above"):
            backend.validate_pc_level_candidate(
                {"rows": ["".join(row) for row in candidate]},
                {
                    "width": 12,
                    "height": 10,
                    "sketchRows": make_sketch(),
                },
            )

    def test_candidate_rejects_generated_two_by_two_wall_block(self):
        for extra_wall in (None, (6, 4)):
            with self.subTest(extra_wall=extra_wall):
                candidate = [list(row) for row in make_candidate()]
                walls = [(4, 4), (5, 4), (4, 5), (5, 5)]

                if extra_wall is not None:
                    walls.append(extra_wall)

                for x, y in walls:
                    candidate[y][x] = "#"

                with self.assertRaisesRegex(ValueError, "2x2 block"):
                    backend.validate_pc_level_candidate(
                        {"rows": ["".join(row) for row in candidate]},
                        {
                            "width": 12,
                            "height": 10,
                            "sketchRows": make_sketch(),
                        },
                    )

    def test_candidate_allows_user_authored_two_by_two_wall_block(self):
        sketch = [list(row) for row in make_sketch()]

        for x, y in ((5, 5), (6, 5), (5, 6), (6, 6)):
            sketch[y][x] = "#"

        sketch_rows = ["".join(row) for row in sketch]
        enclosed = backend.find_pc_enclosed_cells(sketch_rows, 12, 10)
        candidate = [
            [
                "." if enclosed[y][x] and sketch_rows[y][x] == " "
                else sketch_rows[y][x]
                for x in range(12)
            ]
            for y in range(10)
        ]
        candidate[2][2] = "p"

        result = backend.validate_pc_level_candidate(
            {"rows": ["".join(row) for row in candidate]},
            {
                "width": 12,
                "height": 10,
                "sketchRows": sketch_rows,
            },
        )

        self.assertEqual(result["rows"][5][5:7], "##")
        self.assertEqual(result["rows"][6][5:7], "##")

    def test_first_candidate_accepts_five_walls_and_one_water_area(self):
        context = backend.normalize_pc_level_request(self.context)

        result = backend.build_pc_level_candidate(
            make_required_layout(context),
            context,
            minimum_internal_walls=5,
            minimum_water_areas=1,
        )

        wall_count, water_count = backend.validate_pc_required_features(
            result["rows"],
            context["sketchRows"],
            12,
            10,
            5,
            1,
        )
        self.assertEqual(wall_count, 5)
        self.assertEqual(water_count, 1)

    def test_first_candidate_fails_below_five_walls(self):
        context = backend.normalize_pc_level_request(self.context)

        with self.assertRaisesRegex(ValueError, "at least 5"):
            backend.build_pc_level_candidate(
                make_required_layout(context, wall_count=4),
                context,
                minimum_internal_walls=5,
                minimum_water_areas=1,
            )

    def test_intermediate_candidate_retains_four_walls(self):
        context = backend.normalize_pc_level_request(self.context)

        accepted = backend.build_pc_level_candidate(
            make_required_layout(context, wall_count=4),
            context,
            minimum_internal_walls=3,
            minimum_water_areas=1,
        )
        wall_count, water_count = backend.validate_pc_required_features(
            accepted["rows"],
            context["sketchRows"],
            12,
            10,
            3,
            1,
        )
        self.assertEqual(wall_count, 4)
        self.assertEqual(water_count, 1)

    def test_fallback_candidate_requires_three_walls_and_one_water_area(self):
        context = backend.normalize_pc_level_request(self.context)

        accepted = backend.build_pc_level_candidate(
            make_required_layout(context, wall_count=3),
            context,
            minimum_internal_walls=3,
            minimum_water_areas=1,
        )
        self.assertIsNotNone(accepted)

        with self.assertRaisesRegex(ValueError, "at least 3"):
            backend.build_pc_level_candidate(
                make_required_layout(context, wall_count=2),
                context,
                minimum_internal_walls=3,
                minimum_water_areas=1,
            )

        invalid_water = make_required_layout(context, wall_count=3)
        invalid_water["waterAreaId"] = 999999

        with self.assertRaisesRegex(ValueError, "not an allowed water area"):
            backend.build_pc_level_candidate(
                invalid_water,
                context,
                minimum_internal_walls=3,
                minimum_water_areas=1,
            )

    def test_indexed_layout_rejects_unknown_player_id(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["playerCellId"] = 999999

        with self.assertRaisesRegex(ValueError, "not an allowed player cell"):
            backend.build_pc_level_candidate(
                layout,
                context,
            )

    def test_legacy_coordinate_layout_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "indexed layout"):
            backend.build_pc_level_candidate(
                {
                    "player": {"x": 2, "y": 2},
                    "internalWalls": [],
                    "waterAreaIds": [],
                },
                backend.normalize_pc_level_request(self.context),
            )

    def test_indexed_layout_rejects_wall_overlapping_player(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["internalWallCellIds"] = [layout["playerCellId"]]

        with self.assertRaisesRegex(ValueError, "playerCellId"):
            backend.build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_rejects_duplicate_wall(self):
        context = backend.normalize_pc_level_request(self.context)
        wall_id = find_cell_id(context, 5, 5)
        layout = make_layout(context)
        layout["internalWallCellIds"] = [wall_id, wall_id]

        with self.assertRaisesRegex(ValueError, "duplicate"):
            backend.build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_rejects_malformed_wall_id(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["internalWallCellIds"] = ["5"]

        with self.assertRaisesRegex(ValueError, "must be an integer"):
            backend.build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_rejects_wall_overlapping_water(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["internalWallCellIds"] = [
            context["allowedWaterAreas"][layout["waterAreaId"]]["cellIds"][0]
        ]

        with self.assertRaisesRegex(ValueError, "overlap the selected water"):
            backend.build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_rejects_wall_touching_box_start(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["internalWallCellIds"] = [find_cell_id(context, 2, 3)]

        with self.assertRaisesRegex(ValueError, "not an allowed wall cell"):
            backend.build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_allows_connected_activity_area_below_48(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(context)
        water_ids = set(
            context["allowedWaterAreas"][layout["waterAreaId"]]["cellIds"]
        )
        layout["internalWallCellIds"] = [
            cell["id"]
            for cell in context["editableCells"]
            if cell["canPlaceWall"]
            and cell["id"] not in water_ids
            and cell["id"] != layout["playerCellId"]
        ][:5]

        with patch.object(
            backend,
            "validate_pc_completed_level_solvability",
        ):
            result = backend.build_pc_level_candidate(
                layout,
                context,
            )

        walkable_count = sum(
            tile in {".", "p", "s", "t"}
            for row in result["rows"]
            for tile in row
        )
        self.assertLess(walkable_count, 48)

    def test_layout_candidates_are_limited_stable_and_prioritize_five_walls(self):
        normalized = backend.normalize_pc_level_request(self.context)
        normalized_again = backend.normalize_pc_level_request(self.context)
        candidates = normalized["layoutCandidates"]

        self.assertTrue(candidates)
        self.assertLessEqual(len(candidates), 6)
        self.assertEqual(
            [candidate["id"] for candidate in candidates],
            list(range(len(candidates))),
        )
        self.assertEqual(
            candidates,
            normalized_again["layoutCandidates"],
        )
        wall_counts = {
            len(candidate["internalWallCellIds"])
            for candidate in candidates
        }
        self.assertEqual(len(candidates[0]["internalWallCellIds"]), 5)
        self.assertEqual(
            wall_counts,
            {3, 4, 5},
        )

    def test_layout_candidate_walls_keep_outer_shell_clearance(self):
        normalized = backend.normalize_pc_level_request(self.context)
        shell = {
            tuple(position)
            for position in normalized["outerShellCells"]
        }

        for candidate in normalized["layoutCandidates"]:
            for wall in candidate["internalWalls"]:
                position = (wall["x"], wall["y"])

                for direction in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    neighbor = (
                        position[0] + direction[0],
                        position[1] + direction[1],
                    )
                    self.assertNotIn(neighbor, shell)

    def test_five_wall_candidates_avoid_straight_lines_when_possible(self):
        normalized = backend.normalize_pc_level_request(self.context)

        for candidate in normalized["layoutCandidates"]:
            if len(candidate["internalWallCellIds"]) == 5:
                self.assertNotEqual(candidate["wallStyle"], "straight")

    def test_every_layout_candidate_is_complete_and_solvable(self):
        normalized = backend.normalize_pc_level_request(self.context)

        for candidate in normalized["layoutCandidates"]:
            validated = backend.validate_pc_level_candidate(
                {"rows": candidate["rows"]},
                normalized,
            )
            wall_count, water_count = backend.validate_pc_required_features(
                validated["rows"],
                normalized["sketchRows"],
                12,
                10,
                3,
                1,
            )
            self.assertGreaterEqual(wall_count, 3)
            self.assertEqual(water_count, 1)
            generated_walls = {
                (x, y)
                for y in range(10)
                for x in range(12)
                if (
                    validated["rows"][y][x] == "#"
                    and normalized["sketchRows"][y][x] != "#"
                )
            }
            self.assertFalse(
                backend.contains_pc_two_by_two_block(generated_walls)
            )

            for y in range(1, 10):
                for x in range(12):
                    if validated["rows"][y][x] == "@":
                        self.assertNotEqual(
                            validated["rows"][y - 1][x],
                            "#",
                        )

            backend.validate_pc_completed_level_solvability(
                validated["rows"],
                12,
                10,
            )

    def test_layout_selection_requires_one_known_integer_id(self):
        normalized = backend.normalize_pc_level_request(self.context)

        self.assertEqual(
            backend.parse_pc_layout_candidate_selection(
                {"layoutCandidateId": 0}
            ),
            0,
        )

        with self.assertRaisesRegex(ValueError, "only layoutCandidateId"):
            backend.parse_pc_layout_candidate_selection(
                {"layoutCandidateId": 0, "extra": 1}
            )

        with self.assertRaisesRegex(ValueError, "must be an integer"):
            backend.parse_pc_layout_candidate_selection(
                {"layoutCandidateId": "0"}
            )

        self.assertTrue(normalized["layoutCandidates"])

    def test_static_diagnostic_reports_box_without_target_route(self):
        diagnostic = backend.diagnose_pc_static_solvability(
            {(0, 0), (1, 0), (0, 1), (1, 1)},
            ((0, 0),),
            {(1, 1)},
        )

        self.assertEqual(diagnostic["code"], "BOX_NO_TARGET_ROUTE")
        self.assertEqual(diagnostic["box"], (0, 0))

    def test_static_diagnostic_reports_impossible_target_matching(self):
        diagnostic = backend.diagnose_pc_static_solvability(
            {(x, 0) for x in range(5)} | {(10, 10)},
            ((1, 0), (2, 0)),
            {(4, 0), (10, 10)},
        )

        self.assertEqual(
            diagnostic["code"],
            "TARGET_MATCHING_IMPOSSIBLE",
        )

    def test_static_diagnostic_reports_no_legal_initial_push(self):
        diagnostic = backend.diagnose_pc_static_solvability(
            {(x, 0) for x in range(6)},
            ((2, 0), (3, 0)),
            {(4, 0), (5, 0)},
            [(0, 0)],
        )

        self.assertEqual(diagnostic["code"], "NO_LEGAL_INITIAL_PUSH")

    def test_wall_static_filter_removes_individually_blocking_wall(self):
        filtered = backend.filter_pc_static_wall_coordinates(
            {(x, y) for y in range(5) for x in range(5)},
            [[0, 1]],
            [[0, 0]],
            [[0, 2], [4, 4]],
        )

        self.assertNotIn([0, 2], filtered)
        self.assertIn([4, 4], filtered)

    def test_wall_impact_score_prioritizes_route_obstruction(self):
        normalized = backend.normalize_pc_level_request(self.context)
        normalized_again = backend.normalize_pc_level_request(self.context)
        scores = {
            (cell["x"], cell["y"]): cell["wallImpactScore"]
            for cell in normalized["editableCells"]
        }
        scores_again = {
            (cell["x"], cell["y"]): cell["wallImpactScore"]
            for cell in normalized_again["editableCells"]
        }

        self.assertEqual(scores, scores_again)
        self.assertGreater(scores[(5, 3)], scores[(4, 4)])
        self.assertEqual(scores[(2, 3)], 0)

    def test_normalization_does_not_use_wall_combination_enumeration(self):
        normalized = backend.normalize_pc_level_request(self.context)

        self.assertTrue(normalized["layoutCandidates"])
        self.assertFalse(
            hasattr(backend, "find_pc_required_feature_capacity_area")
        )
        self.assertFalse(
            hasattr(backend, "prefilter_pc_water_area_candidates")
        )

    def test_safe_layout_prefilter_obeys_shared_state_budget(self):
        with patch.object(
            backend,
            "PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES",
            1,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "no safe completion",
            ):
                backend.normalize_pc_level_request(self.context)

    def test_rejection_feedback_lists_blocking_wall_id(self):
        rows = [
            "t..@@",
            "s..@@",
            "#....",
            ".....",
            "....p",
        ]
        context = {
            "width": 5,
            "height": 5,
            "editableCells": [
                {"id": 1, "x": 0, "y": 2},
                {"id": 2, "x": 3, "y": 0},
                {"id": 3, "x": 4, "y": 0},
                {"id": 4, "x": 3, "y": 1},
                {"id": 5, "x": 4, "y": 1},
                {"id": 6, "x": 4, "y": 4},
            ],
            "allowedWaterAreas": [
                {
                    "id": 0,
                    "x": 3,
                    "y": 0,
                    "width": 2,
                    "height": 2,
                    "cellIds": [2, 3, 4, 5],
                }
            ],
        }
        layout = {
            "waterAreaId": 0,
            "playerCellId": 6,
            "internalWallCellIds": [1],
        }
        exception = backend.PCSolvabilityError(
            "BOX_NO_TARGET_ROUTE",
            "no route",
            details={"code": "BOX_NO_TARGET_ROUTE", "box": (0, 1)},
        )

        feedback = backend.build_pc_solvability_rejection_feedback(
            layout,
            context,
            rows,
            exception,
        )

        self.assertIn("reasonCode=BOX_NO_TARGET_ROUTE", feedback)
        self.assertIn("blockingWallCellIds=[1]", feedback)

    def test_indexed_candidate_still_requires_backend_solver(self):
        context = backend.normalize_pc_level_request(self.context)

        with patch.object(
            backend,
            "validate_pc_completed_level_solvability",
            side_effect=ValueError("candidate has no Sokoban solution"),
        ):
            with self.assertRaisesRegex(ValueError, "no Sokoban solution"):
                backend.build_pc_level_candidate(
                    make_layout(context),
                    context,
                )

    def test_api_uses_only_pc_fields(self):
        captured = {}

        def create_candidate(
            context,
            request_id,
            max_attempts,
            context_is_normalized=False,
        ):
            captured.update(context)
            return LLMExecutionResult({"rows": make_candidate()}, 1, request_id)

        payload = {
            **self.context,
            "styleDescription": "Must not be consumed.",
            "ideaText": "Must not be consumed.",
            "maxAttempts": 2,
        }

        with patch.object(
            backend,
            "create_pc_level_candidate",
            side_effect=create_candidate,
        ):
            response = self.client.post("/generate-pc-level", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("styleDescription", captured)
        self.assertNotIn("ideaText", captured)
        self.assertEqual(captured["sketchRows"], make_sketch())
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "1")

    def test_api_returns_rows_after_one_indexed_model_response(self):
        model_call_count = 0

        def execute_json_request(**kwargs):
            nonlocal model_call_count
            model_call_count += 1
            value = kwargs["validator"]({"layoutCandidateId": 0})
            return LLMExecutionResult(value, 1, kwargs["request_id"])

        with patch.object(
            backend,
            "execute_json_request",
            side_effect=execute_json_request,
        ):
            response = self.client.post(
                "/generate-pc-level",
                json={**self.context, "maxAttempts": 2},
            )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["rows"]
        self.assertEqual(
            backend.validate_pc_water_rectangles(rows, 12, 10),
            1,
        )
        self.assertEqual(
            sum(
                1
                for y in range(10)
                for x in range(12)
                if rows[y][x] == "#" and make_sketch()[y][x] != "#"
            ),
            5,
        )
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "1")
        self.assertEqual(model_call_count, 1)

    def test_api_normalizes_pc_request_only_once(self):
        original_normalize = backend.normalize_pc_level_request

        def execute_json_request(**kwargs):
            value = kwargs["validator"]({"layoutCandidateId": 0})
            return LLMExecutionResult(value, 1, kwargs["request_id"])

        with patch.object(
            backend,
            "normalize_pc_level_request",
            wraps=original_normalize,
        ) as normalize, patch.object(
            backend,
            "execute_json_request",
            side_effect=execute_json_request,
        ):
            response = self.client.post(
                "/generate-pc-level",
                json=self.context,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(normalize.call_count, 1)

    def test_invalid_layout_id_uses_highest_ranked_safe_candidate(self):
        def execute_json_request(**kwargs):
            try:
                kwargs["validator"]({"layoutCandidateId": 999})
            except ValueError as exception:
                raise backend.LLMServiceError(
                    "MODEL_VALIDATION_FAILED",
                    "pc_level_validation",
                    str(exception),
                    kwargs["request_id"],
                    True,
                    1,
                    502,
                ) from exception

        with patch.object(
            backend,
            "execute_json_request",
            side_effect=execute_json_request,
        ):
            execution = backend.create_pc_level_candidate(
                self.context,
                request_id="pc-invalid-id-fallback",
                max_attempts=2,
            )

        normalized = backend.normalize_pc_level_request(self.context)
        self.assertEqual(
            execution.value["rows"],
            normalized["layoutCandidates"][0]["rows"],
        )
        self.assertEqual(execution.attempts_used, 1)

    def test_invalid_json_and_connection_error_use_safe_candidate(self):
        errors = (
            ("MODEL_JSON_INVALID", "json_parse", 502),
            ("UPSTREAM_CONNECTION_ERROR", "deepseek_request", 502),
        )

        for code, stage, status_code in errors:
            with self.subTest(code=code), patch.object(
                backend,
                "execute_json_request",
                side_effect=backend.LLMServiceError(
                    code,
                    stage,
                    "simulated failure",
                    "pc-safe-fallback",
                    True,
                    1,
                    status_code,
                ),
            ):
                execution = backend.create_pc_level_candidate(
                    self.context,
                    request_id="pc-safe-fallback",
                    max_attempts=2,
                )

            backend.validate_pc_required_features(
                execution.value["rows"],
                make_sketch(),
                12,
                10,
                3,
                1,
            )

    def test_model_timeout_uses_safe_candidate_without_second_call(self):
        with patch.object(
            backend,
            "execute_json_request",
            side_effect=backend.LLMServiceError(
                "UPSTREAM_TIMEOUT",
                "deepseek_request",
                "timeout",
                "pc-timeout-fallback",
                True,
                1,
                504,
            ),
        ) as execute:
            execution = backend.create_pc_level_candidate(
                self.context,
                request_id="pc-timeout-fallback",
                max_attempts=2,
            )

        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execution.attempts_used, 1)
        backend.validate_pc_required_features(
            execution.value["rows"],
            make_sketch(),
            12,
            10,
            3,
            1,
        )

    def test_solver_failure_feedback_contains_selection_and_reason_code(self):
        normalized = backend.normalize_pc_level_request(self.context)
        layout = make_required_layout(normalized)
        solver_error = backend.PCSolvabilityError(
            "NO_SOLUTION_AFTER_SEARCH",
            "candidate has no Sokoban solution",
            searched_states=1234,
        )

        with patch.object(
            backend,
            "validate_pc_completed_level_solvability",
            side_effect=solver_error,
        ), patch.object(backend, "log_event") as mocked_log:
            with self.assertRaises(ValueError) as raised:
                backend.build_pc_level_candidate(
                    layout,
                    normalized,
                    request_id="pc-feedback-test",
                    minimum_internal_walls=5,
                    minimum_water_areas=1,
                )

        detail = str(raised.exception)
        self.assertIn("reasonCode=NO_SOLUTION_AFTER_SEARCH", detail)
        self.assertIn(
            f"waterAreaId={layout['waterAreaId']}",
            detail,
        )
        self.assertIn(
            f"playerCellId={layout['playerCellId']}",
            detail,
        )
        self.assertIn("searchedStates=1234", detail)
        self.assertTrue(mocked_log.called)
        self.assertEqual(
            mocked_log.call_args.kwargs["reasonCode"],
            "NO_SOLUTION_AFTER_SEARCH",
        )

    def test_invalid_sketch_is_rejected_before_llm(self):
        payload = copy.deepcopy(self.context)
        payload["width"] = 11

        with patch.object(backend, "create_pc_level_candidate") as create_candidate:
            response = self.client.post("/generate-pc-level", json=payload)

        self.assertEqual(response.status_code, 400)
        create_candidate.assert_not_called()

    def test_empty_previous_candidate_is_treated_as_first_attempt(self):
        payload = {
            **self.context,
            "previousCandidateRows": [],
        }
        normalized = backend.normalize_pc_level_request(payload)

        self.assertIsNone(normalized["previousCandidateRows"])

    def test_sketch_requires_at_least_56_enclosed_cells(self):
        payload = copy.deepcopy(self.context)
        row = list(payload["sketchRows"][8])
        row[9] = "#"
        payload["sketchRows"][8] = "".join(row)

        with self.assertRaisesRegex(ValueError, "at least 56"):
            backend.normalize_pc_level_request(payload)

    def test_sketch_requires_capacity_for_three_safe_walls_and_water(self):
        normalized = backend.normalize_pc_level_request(self.context)

        self.assertTrue(normalized["layoutCandidates"])
        self.assertTrue(
            all(
                len(candidate["internalWallCellIds"]) >= 3
                for candidate in normalized["layoutCandidates"]
            )
        )
        self.assertTrue(
            all(candidate["waterAreaId"] is not None
                for candidate in normalized["layoutCandidates"])
        )

    def test_editable_cells_mark_player_and_wall_permissions(self):
        normalized = backend.normalize_pc_level_request(self.context)
        cells_by_coordinate = {
            (cell["x"], cell["y"]): cell
            for cell in normalized["editableCells"]
        }

        self.assertTrue(cells_by_coordinate[(2, 2)]["canPlacePlayer"])
        self.assertFalse(cells_by_coordinate[(2, 2)]["canPlaceWall"])
        self.assertTrue(cells_by_coordinate[(5, 5)]["canPlaceWall"])
        self.assertNotIn((3, 3), cells_by_coordinate)

        for coordinate in ([2, 3], [4, 3], [3, 2], [3, 4]):
            cell = cells_by_coordinate[tuple(coordinate)]
            self.assertTrue(cell["canPlacePlayer"])
            self.assertFalse(cell["canPlaceWall"])

    def test_box_start_touching_wall_is_rejected(self):
        payload = copy.deepcopy(self.context)
        payload["sketchRows"][3] = " #s     t # "

        with self.assertRaisesRegex(ValueError, "cannot touch a wall"):
            backend.normalize_pc_level_request(payload)

    def test_unsolvable_open_sketch_is_rejected(self):
        rows = [
            "   #######  ",
            "####   s #  ",
            "#        ###",
            "#          #",
            "#s         #",
            "#         t#",
            "##         #",
            " #         #",
            " #   t   ###",
            " #########  ",
        ]
        enclosed = backend.find_pc_enclosed_cells(rows, 12, 10)
        enclosed_cells = {
            (x, y)
            for y in range(10)
            for x in range(12)
            if enclosed[y][x]
        }

        with self.assertRaisesRegex(ValueError, "no solvable open completion"):
            backend.validate_pc_open_sketch_feasibility(
                rows,
                enclosed_cells,
                12,
                10,
            )

    def test_completed_candidate_solver_uses_explicit_player(self):
        rows = [
            "   #######  ",
            "####   s #  ",
            "#        ###",
            "#          #",
            "#s         #",
            "#         t#",
            "##         #",
            " #         #",
            " #   t   ###",
            " #########  ",
        ]
        enclosed = backend.find_pc_enclosed_cells(rows, 12, 10)
        completed = [
            "".join(
                "." if enclosed[y][x] and rows[y][x] == " " else rows[y][x]
                for x in range(12)
            )
            for y in range(10)
        ]
        player_row = list(completed[2])
        player_row[1] = "p"
        completed[2] = "".join(player_row)

        with self.assertRaisesRegex(ValueError, "no Sokoban solution"):
            backend.validate_pc_completed_level_solvability(
                completed,
                12,
                10,
            )

    def test_moving_fixed_tile_is_rejected(self):
        candidate = make_candidate()
        candidate[3] = " #..s...t.# "

        with self.assertRaisesRegex(ValueError, "fixed tile"):
            backend.validate_pc_level_candidate(
                {"rows": candidate},
                backend.normalize_pc_level_request(self.context),
            )

    def test_changing_outside_space_is_rejected(self):
        candidate = make_candidate()
        candidate[0] = ".           "

        with self.assertRaisesRegex(ValueError, "outside"):
            backend.validate_pc_level_candidate(
                {"rows": candidate},
                backend.normalize_pc_level_request(self.context),
            )

    def test_extra_target_is_rejected(self):
        candidate = make_candidate()
        candidate[4] = " #t.......# "

        with self.assertRaisesRegex(ValueError, "incomplete tile"):
            backend.validate_pc_level_candidate(
                {"rows": candidate},
                backend.normalize_pc_level_request(self.context),
            )

    def test_invalid_water_shape_is_rejected(self):
        candidate = make_candidate()
        candidate[5] = " #@.......# "

        with self.assertRaisesRegex(ValueError, "water area"):
            backend.validate_pc_level_candidate(
                {"rows": candidate},
                backend.normalize_pc_level_request(self.context),
            )

    def test_candidate_cannot_add_wall_next_to_box_start(self):
        candidate = make_candidate()
        candidate[3] = " ##s....t.# "

        with self.assertRaisesRegex(ValueError, "cannot touch a wall"):
            backend.validate_pc_level_candidate(
                {"rows": candidate},
                backend.normalize_pc_level_request(self.context),
            )

    def test_prompt_only_exposes_complete_layout_candidates(self):
        context = {
            **self.context,
            "previousCandidateRows": make_candidate(),
            "rejectionReason": "Unity solver found no solution.",
        }
        captured_messages = []

        def execute_json_request(**kwargs):
            captured_messages.extend(kwargs["messages"])
            value = kwargs["validator"]({"layoutCandidateId": 0})
            return LLMExecutionResult(value, 1, kwargs["request_id"])

        with patch.object(
            backend,
            "execute_json_request",
            side_effect=execute_json_request,
        ):
            backend.create_pc_level_candidate(
                context,
                request_id="pc-test",
                max_attempts=1,
            )

        prompt_text = "\n".join(message["content"] for message in captured_messages)
        self.assertIn("layoutCandidateId", prompt_text)
        self.assertIn("layoutCandidates", prompt_text)
        self.assertIn("wallStyle", prompt_text)
        self.assertIn("internalWalls", prompt_text)
        self.assertIn("waterArea", prompt_text)
        self.assertIn("five-wall candidate", prompt_text)
        self.assertIn("four-wall candidate", prompt_text)
        self.assertIn("three-wall candidate", prompt_text)
        self.assertNotIn("effectiveWallSets", prompt_text)
        self.assertNotIn("editableCells", prompt_text)
        self.assertNotIn("previousCandidateRows", prompt_text)
        self.assertNotIn("rejectionReason", prompt_text)

    def test_pc_model_call_is_single_attempt_with_short_timeout(self):
        captured = {}

        def execute_json_request(**kwargs):
            captured.update(kwargs)
            value = kwargs["validator"]({"layoutCandidateId": 0})
            return LLMExecutionResult(value, 1, kwargs["request_id"])

        with (
            patch.dict(
                backend.os.environ,
                {
                    "DEEPSEEK_PC_LEVEL_TIMEOUT_SECONDS": "15",
                    "DEEPSEEK_PC_LEVEL_TEMPERATURE": "0.15",
                },
            ),
            patch.object(
                backend,
                "execute_json_request",
                side_effect=execute_json_request,
            ),
        ):
            backend.create_pc_level_candidate(
                self.context,
                request_id="pc-budget-test",
                max_attempts=2,
            )

        self.assertEqual(captured["max_attempts"], 1)
        self.assertEqual(captured["timeout_seconds"], 15.0)
        self.assertEqual(captured["temperature"], 0.15)
        self.assertEqual(captured["thinking_mode"], "disabled")
        self.assertEqual(captured["retry_error_codes"], set())


if __name__ == "__main__":
    unittest.main()
