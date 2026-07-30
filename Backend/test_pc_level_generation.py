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
        player=(2, 2),
        walls=((5, 3), (6, 3), (7, 3), (5, 4))[:wall_count],
        water=(5, 7, 2, 2),
    )


def build_candidate_for_wall_impact_test(context, layout):
    with patch.object(backend, "validate_pc_internal_wall_impact"):
        candidate = backend.build_pc_level_candidate(layout, context)

    editable_cells = {
        cell["id"]: cell
        for cell in context["editableCells"]
    }
    wall_cells = [
        editable_cells[cell_id]
        for cell_id in layout["internalWallCellIds"]
    ]
    return candidate["rows"], wall_cells


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
        ), patch.object(
            backend,
            "validate_pc_internal_wall_impact",
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
        allowed_wall_ids = {
            cell["id"]
            for cell in normalized["editableCells"]
            if cell["canPlaceWall"]
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
            self.assertEqual(
                area["compatibleWallCellIds"],
                sorted(allowed_wall_ids - set(area["cellIds"])),
            )

    def test_selected_walls_must_use_water_compatible_ids(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_required_layout(context)
        selected_area = next(
            area
            for area in context["allowedWaterAreas"]
            if area["id"] == layout["waterAreaId"]
        )
        removed_id = layout["internalWallCellIds"][0]
        selected_area["compatibleWallCellIds"].remove(removed_id)

        with self.assertRaisesRegex(
            ValueError,
            "compatibleWallCellIds",
        ):
            backend.build_pc_level_candidate(
                layout,
                context,
                minimum_internal_walls=4,
                minimum_water_areas=1,
            )

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
            self.assertEqual(backend.count_pc_components(remaining), 1)
            backend.validate_pc_open_sketch_feasibility(
                make_sketch(),
                remaining,
                12,
                10,
                maximum_search_states=backend.PC_PREFILTER_MAX_SEARCH_STATES,
            )

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

    def test_decorative_wall_set_is_rejected_for_low_impact(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_layout(
            context,
            player=(2, 2),
            walls=((4, 4), (5, 4), (6, 4), (7, 4)),
            water=(5, 7, 2, 2),
        )

        with self.assertRaises(backend.PCWallImpactError) as raised:
            backend.build_pc_level_candidate(
                layout,
                context,
                minimum_internal_walls=4,
                minimum_water_areas=1,
            )

        self.assertEqual(
            raised.exception.reason_code,
            "WALL_IMPACT_TOO_LOW",
        )
        self.assertIn("stepDelta=0", str(raised.exception))
        self.assertIn("pushDelta=0", str(raised.exception))

    def test_wall_impact_accepts_four_extra_shortest_steps(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_required_layout(context, wall_count=2)
        rows, wall_cells = build_candidate_for_wall_impact_test(
            context,
            layout,
        )
        metric_results = [
            {
                "shortestSteps": 14,
                "minimumPushes": 5,
                "pushSearchedStates": 10,
                "stepSearchedStates": 20,
            },
            {
                "shortestSteps": 10,
                "minimumPushes": 5,
                "pushSearchedStates": 10,
                "stepSearchedStates": 20,
            },
        ]

        with patch.object(
            backend,
            "calculate_pc_level_solution_metrics",
            side_effect=metric_results,
        ):
            result = backend.validate_pc_internal_wall_impact(
                rows,
                context["sketchRows"],
                wall_cells,
                12,
                10,
                internal_wall_cell_ids=layout["internalWallCellIds"],
            )

        self.assertEqual(result["stepDelta"], 4)
        self.assertEqual(result["pushDelta"], 0)

    def test_wall_impact_accepts_one_extra_minimum_push(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_required_layout(context, wall_count=2)
        rows, wall_cells = build_candidate_for_wall_impact_test(
            context,
            layout,
        )
        metric_results = [
            {
                "shortestSteps": 12,
                "minimumPushes": 6,
                "pushSearchedStates": 10,
                "stepSearchedStates": 20,
            },
            {
                "shortestSteps": 10,
                "minimumPushes": 5,
                "pushSearchedStates": 10,
                "stepSearchedStates": 20,
            },
        ]

        with patch.object(
            backend,
            "calculate_pc_level_solution_metrics",
            side_effect=metric_results,
        ):
            result = backend.validate_pc_internal_wall_impact(
                rows,
                context["sketchRows"],
                wall_cells,
                12,
                10,
                internal_wall_cell_ids=layout["internalWallCellIds"],
            )

        self.assertEqual(result["stepDelta"], 2)
        self.assertEqual(result["pushDelta"], 1)

    def test_wall_counterfactual_removes_only_generated_walls(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_required_layout(context, wall_count=2)
        rows, wall_cells = build_candidate_for_wall_impact_test(
            context,
            layout,
        )

        result = backend.validate_pc_internal_wall_impact(
            rows,
            context["sketchRows"],
            wall_cells,
            12,
            10,
            internal_wall_cell_ids=layout["internalWallCellIds"],
        )
        counterfactual = result["counterfactualRows"]
        generated_wall_positions = {
            (cell["x"], cell["y"])
            for cell in wall_cells
        }

        for y in range(10):
            for x in range(12):
                if (x, y) in generated_wall_positions:
                    self.assertEqual(counterfactual[y][x], ".")
                else:
                    self.assertEqual(counterfactual[y][x], rows[y][x])

        self.assertEqual(counterfactual[1][1], "#")
        self.assertEqual(counterfactual[7][5:7], "@@")
        self.assertEqual(counterfactual[2][2], "p")
        self.assertEqual(counterfactual[3][3], "s")
        self.assertEqual(counterfactual[3][8], "t")

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

    def test_wall_impact_search_budget_has_explicit_reason(self):
        context = backend.normalize_pc_level_request(self.context)
        layout = make_required_layout(context, wall_count=2)
        rows, wall_cells = build_candidate_for_wall_impact_test(
            context,
            layout,
        )

        with patch.object(
            backend,
            "PC_WALL_IMPACT_MAX_SEARCH_STATES",
            1,
        ):
            with self.assertRaises(backend.PCWallImpactError) as raised:
                backend.validate_pc_internal_wall_impact(
                    rows,
                    context["sketchRows"],
                    wall_cells,
                    12,
                    10,
                    internal_wall_cell_ids=layout["internalWallCellIds"],
                )

        self.assertEqual(
            raised.exception.reason_code,
            "WALL_IMPACT_SEARCH_BUDGET_EXCEEDED",
        )

    def test_water_prefilter_drops_proven_unsolvable_candidate(self):
        normalized = backend.normalize_pc_level_request(self.context)
        enclosed = backend.find_pc_enclosed_cells(make_sketch(), 12, 10)
        enclosed_cells = {
            (x, y)
            for y in range(10)
            for x in range(12)
            if enclosed[y][x]
        }
        calls = 0

        def validate(*args, **kwargs):
            nonlocal calls
            calls += 1

            if calls == 1:
                raise backend.PCSolvabilityError(
                    "NO_SOLUTION_AFTER_SEARCH",
                    "no solution",
                )

            return 1

        with patch.object(
            backend,
            "validate_pc_open_sketch_feasibility",
            side_effect=validate,
        ):
            accepted = backend.prefilter_pc_water_area_candidates(
                make_sketch(),
                enclosed_cells,
                normalized["allowedWaterAreas"],
                12,
                10,
                12,
            )

        self.assertEqual(len(accepted), 11)
        self.assertEqual(
            [area["id"] for area in accepted],
            list(range(11)),
        )

    def test_water_prefilter_keeps_search_budget_unknown(self):
        normalized = backend.normalize_pc_level_request(self.context)
        enclosed = backend.find_pc_enclosed_cells(make_sketch(), 12, 10)
        enclosed_cells = {
            (x, y)
            for y in range(10)
            for x in range(12)
            if enclosed[y][x]
        }

        with patch.object(
            backend,
            "validate_pc_open_sketch_feasibility",
            side_effect=backend.PCSolvabilityError(
                "SEARCH_BUDGET_EXCEEDED",
                "budget",
                searched_states=20000,
            ),
        ):
            accepted = backend.prefilter_pc_water_area_candidates(
                make_sketch(),
                enclosed_cells,
                normalized["allowedWaterAreas"],
                12,
                10,
                12,
            )

        self.assertEqual(len(accepted), 12)

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

            corrected_layout = make_layout(
                normalized,
                player=(2, 2),
                walls=((6, 3), (7, 3)),
                water=(5, 7, 2, 2),
            )
            value = kwargs["validator"](corrected_layout)
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

    def test_corrected_candidate_must_change_the_rejected_selection(self):
        def execute_json_request(**kwargs):
            normalized = backend.normalize_pc_level_request(self.context)
            rejected_layout = make_required_layout(
                normalized,
                wall_count=2,
            )

            with self.assertRaisesRegex(ValueError, "at least 4"):
                kwargs["validator"](rejected_layout)

            with self.assertRaisesRegex(ValueError, "must change waterAreaId"):
                kwargs["validator"](rejected_layout)

            corrected_layout = make_layout(
                normalized,
                player=(2, 2),
                walls=((6, 3), (7, 3)),
                water=(5, 7, 2, 2),
            )
            value = kwargs["validator"](corrected_layout)
            return LLMExecutionResult(value, 2, kwargs["request_id"])

        with patch.object(
            backend,
            "execute_json_request",
            side_effect=execute_json_request,
        ):
            execution = backend.create_pc_level_candidate(
                self.context,
                request_id="pc-change-selection-test",
                max_attempts=2,
            )

        self.assertEqual(execution.attempts_used, 2)

    def test_low_impact_correction_must_change_wall_ids(self):
        def execute_json_request(**kwargs):
            normalized = backend.normalize_pc_level_request(self.context)
            low_impact_layout = make_layout(
                normalized,
                player=(2, 2),
                walls=((4, 4), (5, 4), (6, 4), (7, 4)),
                water=(5, 7, 2, 2),
            )

            with self.assertRaises(backend.PCWallImpactError):
                kwargs["validator"](low_impact_layout)

            same_walls_new_player = make_layout(
                normalized,
                player=(3, 2),
                walls=((4, 4), (5, 4), (6, 4), (7, 4)),
                water=(5, 7, 2, 2),
            )

            with self.assertRaisesRegex(
                ValueError,
                "must change internalWallCellIds",
            ):
                kwargs["validator"](same_walls_new_player)

            corrected_layout = make_layout(
                normalized,
                player=(2, 2),
                walls=((6, 3), (7, 3)),
                water=(5, 7, 2, 2),
            )
            value = kwargs["validator"](corrected_layout)
            return LLMExecutionResult(value, 2, kwargs["request_id"])

        with patch.object(
            backend,
            "execute_json_request",
            side_effect=execute_json_request,
        ):
            execution = backend.create_pc_level_candidate(
                self.context,
                request_id="pc-wall-impact-correction-test",
                max_attempts=2,
            )

        self.assertEqual(execution.attempts_used, 2)

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
                    minimum_internal_walls=4,
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
                0,
            )
        )
        self.assertFalse(
            backend.has_pc_required_feature_capacity(
                enclosed_cells,
                normalized["allowedWallCoordinates"],
                [],
                4,
                0,
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
        self.assertIn("compatibleWallCellIds", prompt_text)
        self.assertIn("wallImpactScore", prompt_text)
        self.assertIn("at least four shortest-path movement steps", prompt_text)
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
