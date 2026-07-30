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
    water=(4, 2, 2, 2),
):
    return {
        "waterAreaId": find_water_area_id(context, *water),
        "playerCellId": find_cell_id(context, *player),
        "internalWallCellIds": [
            find_cell_id(context, x, y)
            for x, y in walls
        ],
    }


def make_required_layout(context, wall_count=4):
    return make_layout(
        context,
        player=(3, 2),
        walls=((2, 2), (6, 2), (7, 2), (8, 2))[:wall_count],
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
        self.assertEqual(result["rows"][2][4:6], "@@")
        self.assertEqual(result["rows"][3][4:6], "@@")
        self.assertTrue(all(len(row) == 12 for row in result["rows"]))

    def test_indexed_layout_builds_valid_wall_and_water(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(context, walls=((6, 2),))

        result = backend.build_pc_level_candidate(
            layout,
            context,
        )

        self.assertEqual(result["rows"][2], " #p.@@#...# ")
        self.assertEqual(result["rows"][3], " #.s@@..t.# ")

    def test_first_candidate_accepts_four_walls_and_one_water_area(self):
        context = backend.normalize_pc_level_request(self.context)

        result = backend.build_pc_level_candidate(
            make_required_layout(context),
            context,
            minimum_internal_walls=4,
            minimum_water_areas=1,
        )

        wall_count, water_count = backend.validate_pc_required_features(
            result["rows"],
            context["sketchRows"],
            12,
            10,
            4,
            1,
        )
        self.assertEqual(wall_count, 4)
        self.assertEqual(water_count, 1)

    def test_first_candidate_fails_below_four_walls(self):
        context = backend.normalize_pc_level_request(self.context)

        with self.assertRaisesRegex(ValueError, "at least 4"):
            backend.build_pc_level_candidate(
                make_required_layout(context, wall_count=3),
                context,
                minimum_internal_walls=4,
                minimum_water_areas=1,
            )

    def test_fallback_candidate_requires_two_walls_and_one_water_area(self):
        context = backend.normalize_pc_level_request(self.context)

        accepted = backend.build_pc_level_candidate(
            make_required_layout(context, wall_count=2),
            context,
            minimum_internal_walls=2,
            minimum_water_areas=1,
        )
        self.assertIsNotNone(accepted)

        with self.assertRaisesRegex(ValueError, "at least 2"):
            backend.build_pc_level_candidate(
                make_required_layout(context, wall_count=1),
                context,
                minimum_internal_walls=2,
                minimum_water_areas=1,
            )

        invalid_water = make_required_layout(context, wall_count=2)
        invalid_water["waterAreaId"] = 999999

        with self.assertRaisesRegex(ValueError, "not an allowed water area"):
            backend.build_pc_level_candidate(
                invalid_water,
                context,
                minimum_internal_walls=2,
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

    def test_indexed_layout_rejects_activity_area_below_48(self):
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

        with (
            self.assertRaisesRegex(ValueError, "at least 48"),
            patch.object(
                backend,
                "validate_pc_completed_level_solvability",
            ),
        ):
            backend.build_pc_level_candidate(
                layout,
                context,
            )

    def test_water_candidates_are_limited_stable_and_diverse(self):
        normalized = backend.normalize_pc_level_request(self.context)
        normalized_again = backend.normalize_pc_level_request(self.context)
        areas = normalized["allowedWaterAreas"]

        self.assertLessEqual(len(areas), 12)
        self.assertEqual(
            [area["id"] for area in areas],
            list(range(len(areas))),
        )
        self.assertEqual(areas, normalized_again["allowedWaterAreas"])
        self.assertGreaterEqual(
            len({area["width"] * area["height"] for area in areas}),
            2,
        )

        editable_cells = {
            cell["id"]: (cell["x"], cell["y"])
            for cell in normalized["editableCells"]
        }

        for area in areas:
            expected_positions = {
                (x, y)
                for y in range(area["y"], area["y"] + area["height"])
                for x in range(area["x"], area["x"] + area["width"])
            }
            actual_positions = {
                editable_cells[cell_id]
                for cell_id in area["cellIds"]
            }
            self.assertEqual(actual_positions, expected_positions)

    def test_every_water_candidate_is_individually_safe(self):
        normalized = backend.normalize_pc_level_request(self.context)
        enclosed = backend.find_pc_enclosed_cells(make_sketch(), 12, 10)
        enclosed_cells = {
            (x, y)
            for y in range(10)
            for x in range(12)
            if enclosed[y][x]
        }

        for area in normalized["allowedWaterAreas"]:
            positions = {
                (x, y)
                for y in range(area["y"], area["y"] + area["height"])
                for x in range(area["x"], area["x"] + area["width"])
            }
            remaining = enclosed_cells - positions
            self.assertGreaterEqual(len(remaining), 48)
            self.assertEqual(backend.count_pc_components(remaining), 1)

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

        def create_candidate(context, request_id, max_attempts):
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
            context = backend.normalize_pc_level_request(self.context)
            value = kwargs["validator"](make_required_layout(context))
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
            4,
        )
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "1")
        self.assertEqual(model_call_count, 1)

    def test_second_structural_candidate_uses_two_wall_fallback(self):
        def execute_json_request(**kwargs):
            normalized = backend.normalize_pc_level_request(self.context)
            fallback_layout = make_required_layout(
                normalized,
                wall_count=2,
            )

            with self.assertRaisesRegex(ValueError, "at least 4"):
                kwargs["validator"](fallback_layout)

            value = kwargs["validator"](fallback_layout)
            return LLMExecutionResult(value, 2, kwargs["request_id"])

        with patch.object(
            backend,
            "execute_json_request",
            side_effect=execute_json_request,
        ):
            execution = backend.create_pc_level_candidate(
                self.context,
                request_id="pc-fallback-test",
                max_attempts=2,
            )

        self.assertEqual(execution.attempts_used, 2)
        backend.validate_pc_required_features(
            execution.value["rows"],
            make_sketch(),
            12,
            10,
            2,
            1,
        )

    def test_conflicting_first_candidate_is_rejected_before_correction(self):
        def execute_json_request(**kwargs):
            normalized = backend.normalize_pc_level_request(self.context)
            conflicting_layout = make_required_layout(normalized)
            water_area = normalized["allowedWaterAreas"][
                conflicting_layout["waterAreaId"]
            ]
            conflicting_layout["internalWallCellIds"][0] = (
                water_area["cellIds"][0]
            )

            with self.assertRaisesRegex(
                ValueError,
                "overlap the selected water",
            ):
                kwargs["validator"](conflicting_layout)

            value = kwargs["validator"](
                make_required_layout(normalized, wall_count=2)
            )
            return LLMExecutionResult(value, 2, kwargs["request_id"])

        with patch.object(
            backend,
            "execute_json_request",
            side_effect=execute_json_request,
        ):
            execution = backend.create_pc_level_candidate(
                self.context,
                request_id="pc-conflict-correction-test",
                max_attempts=2,
            )

        self.assertEqual(execution.attempts_used, 2)

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

    def test_sketch_requires_capacity_for_four_walls_and_water(self):
        normalized = backend.normalize_pc_level_request(self.context)
        enclosed = backend.find_pc_enclosed_cells(make_sketch(), 12, 10)
        enclosed_cells = {
            (x, y)
            for y in range(10)
            for x in range(12)
            if enclosed[y][x]
        }

        self.assertTrue(
            backend.has_pc_required_feature_capacity(
                enclosed_cells,
                normalized["allowedWallCoordinates"],
                normalized["allowedWaterAreas"],
                4,
                48,
            )
        )
        self.assertFalse(
            backend.has_pc_required_feature_capacity(
                enclosed_cells,
                normalized["allowedWallCoordinates"],
                [],
                4,
                48,
            )
        )

    def test_editable_cells_mark_player_and_wall_permissions(self):
        normalized = backend.normalize_pc_level_request(self.context)
        cells_by_coordinate = {
            (cell["x"], cell["y"]): cell
            for cell in normalized["editableCells"]
        }

        self.assertTrue(cells_by_coordinate[(2, 2)]["canPlacePlayer"])
        self.assertTrue(cells_by_coordinate[(2, 2)]["canPlaceWall"])
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

    def test_previous_rejection_is_added_to_prompt(self):
        context = {
            **self.context,
            "previousCandidateRows": make_candidate(),
            "rejectionReason": "Unity solver found no solution.",
        }
        captured_messages = []

        def execute_json_request(**kwargs):
            captured_messages.extend(kwargs["messages"])
            normalized = backend.normalize_pc_level_request(self.context)
            value = kwargs["validator"](make_required_layout(normalized))
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
        self.assertIn("previousCandidateRows", prompt_text)
        self.assertIn("Unity solver found no solution.", prompt_text)
        self.assertIn("internalWallCellIds", prompt_text)
        self.assertIn("Do not return map rows", prompt_text)
        self.assertIn("editableCells", prompt_text)
        self.assertIn("allowedWaterAreas", prompt_text)
        self.assertIn("waterAreaId", prompt_text)
        self.assertIn("playerCellId", prompt_text)
        self.assertIn("cellIds", prompt_text)
        self.assertIn("at least four", prompt_text)
        self.assertIn("fallbackMinimumInternalWalls", prompt_text)
        self.assertNotIn("editableCoordinates", prompt_text)
        self.assertNotIn("allowedWallCoordinates", prompt_text)
        self.assertNotIn("waterAreaIds", prompt_text)
        self.assertNotIn('"player":', prompt_text)

    def test_pc_model_call_retries_only_model_output_failures(self):
        captured = {}

        def execute_json_request(**kwargs):
            captured.update(kwargs)
            normalized = backend.normalize_pc_level_request(self.context)
            value = kwargs["validator"](make_required_layout(normalized))
            return LLMExecutionResult(value, 1, kwargs["request_id"])

        with (
            patch.dict(
                backend.os.environ,
                {
                    "DEEPSEEK_PC_LEVEL_TIMEOUT_SECONDS": "60",
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

        self.assertEqual(captured["max_attempts"], 2)
        self.assertEqual(captured["timeout_seconds"], 60.0)
        self.assertEqual(captured["temperature"], 0.15)
        self.assertEqual(captured["thinking_mode"], "disabled")
        self.assertEqual(
            captured["retry_error_codes"],
            {"MODEL_JSON_INVALID", "MODEL_VALIDATION_FAILED"},
        )


if __name__ == "__main__":
    unittest.main()
