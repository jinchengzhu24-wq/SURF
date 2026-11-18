import copy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

import app as backend_module
from sokoban_backend.dependencies import BackendSettings, PCLevelGenerationService
from sokoban_backend.llm.runtime import LLMExecutionResult, LLMServiceError
from sokoban_backend.schemas import PCSolvabilityError
from sokoban_backend.services.pc_generator import (
    build_pc_level_candidate,
    build_pc_solvability_rejection_feedback,
    contains_pc_two_by_two_block,
    diagnose_pc_static_solvability,
    filter_pc_static_wall_coordinates,
    find_pc_enclosed_cells,
    normalize_pc_level_request,
    parse_pc_layout_candidate_selection,
    validate_pc_completed_level_solvability,
    validate_pc_level_candidate,
    validate_pc_open_sketch_feasibility,
    validate_pc_required_features,
    validate_pc_water_rectangles,
)
from tests.fakes import (
    FakeLLMGateway,
    RecordingEventLogger,
    create_test_app,
    create_test_container,
)


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
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.context = {
            "width": 12,
            "height": 10,
            "sketchRows": make_sketch(),
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_candidate_preserves_sketch_contract(self):
        result = validate_pc_level_candidate(
            {"rows": make_candidate()},
            normalize_pc_level_request(self.context),
        )

        self.assertEqual(result["rows"], make_candidate())

    def test_incomplete_inner_space_is_normalized_to_ground(self):
        candidate = make_candidate()
        candidate[2] = " #p... ...# "

        result = validate_pc_level_candidate(
            {"rows": candidate},
            normalize_pc_level_request(self.context),
        )

        self.assertEqual(result["rows"][2], " #p.......# ")

    def test_indexed_layout_builds_complete_rows(self):
        context = normalize_pc_level_request(self.context)
        result = build_pc_level_candidate(
            make_layout(context),
            context,
        )

        self.assertEqual(result["rows"][2][2], "p")
        self.assertEqual(result["rows"][7][5:7], "@@")
        self.assertEqual(result["rows"][8][5:7], "@@")
        self.assertTrue(all(len(row) == 12 for row in result["rows"]))

    def test_indexed_layout_builds_valid_wall_and_water(self):
        context = normalize_pc_level_request(self.context)
        layout = make_layout(
            context,
            walls=((6, 3),),
            water=(5, 7, 2, 2),
        )

        result = build_pc_level_candidate(
            layout,
            context,
        )

        self.assertEqual(result["rows"][2], " #p.......# ")
        self.assertEqual(result["rows"][3], " #.s..#.t.# ")
        self.assertEqual(result["rows"][7], " #...@@...# ")
        self.assertEqual(result["rows"][8], " #...@@...# ")

    def test_water_area_directly_below_fixed_wall_is_not_enumerated(self):
        context = normalize_pc_level_request(self.context)
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
        context = normalize_pc_level_request(self.context)
        layout = make_layout(
            context,
            walls=((5, 6),),
            water=(5, 7, 2, 2),
        )

        with self.assertRaisesRegex(ValueError, "compatibleWallCellIds"):
            build_pc_level_candidate(layout, context)

    def test_candidate_rejects_water_directly_below_wall(self):
        candidate = [list(row) for row in make_candidate()]

        for x, y in ((5, 2), (6, 2), (5, 3), (6, 3)):
            candidate[y][x] = "@"

        with self.assertRaisesRegex(ValueError, "wall directly above"):
            validate_pc_level_candidate(
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
                    validate_pc_level_candidate(
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
        enclosed = find_pc_enclosed_cells(sketch_rows, 12, 10)
        candidate = [
            [
                "." if enclosed[y][x] and sketch_rows[y][x] == " "
                else sketch_rows[y][x]
                for x in range(12)
            ]
            for y in range(10)
        ]
        candidate[2][2] = "p"

        result = validate_pc_level_candidate(
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
        context = normalize_pc_level_request(self.context)

        result = build_pc_level_candidate(
            make_required_layout(context),
            context,
            minimum_internal_walls=5,
            minimum_water_areas=1,
        )

        wall_count, water_count = validate_pc_required_features(
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
        context = normalize_pc_level_request(self.context)

        with self.assertRaisesRegex(ValueError, "at least 5"):
            build_pc_level_candidate(
                make_required_layout(context, wall_count=4),
                context,
                minimum_internal_walls=5,
                minimum_water_areas=1,
            )

    def test_intermediate_candidate_retains_four_walls(self):
        context = normalize_pc_level_request(self.context)

        accepted = build_pc_level_candidate(
            make_required_layout(context, wall_count=4),
            context,
            minimum_internal_walls=3,
            minimum_water_areas=1,
        )
        wall_count, water_count = validate_pc_required_features(
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
        context = normalize_pc_level_request(self.context)

        accepted = build_pc_level_candidate(
            make_required_layout(context, wall_count=3),
            context,
            minimum_internal_walls=3,
            minimum_water_areas=1,
        )
        self.assertIsNotNone(accepted)

        with self.assertRaisesRegex(ValueError, "at least 3"):
            build_pc_level_candidate(
                make_required_layout(context, wall_count=2),
                context,
                minimum_internal_walls=3,
                minimum_water_areas=1,
            )

        invalid_water = make_required_layout(context, wall_count=3)
        invalid_water["waterAreaId"] = 999999

        with self.assertRaisesRegex(ValueError, "not an allowed water area"):
            build_pc_level_candidate(
                invalid_water,
                context,
                minimum_internal_walls=3,
                minimum_water_areas=1,
            )

    def test_indexed_layout_rejects_unknown_player_id(self):
        context = normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["playerCellId"] = 999999

        with self.assertRaisesRegex(ValueError, "not an allowed player cell"):
            build_pc_level_candidate(
                layout,
                context,
            )

    def test_legacy_coordinate_layout_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "indexed layout"):
            build_pc_level_candidate(
                {
                    "player": {"x": 2, "y": 2},
                    "internalWalls": [],
                    "waterAreaIds": [],
                },
                normalize_pc_level_request(self.context),
            )

    def test_indexed_layout_rejects_wall_overlapping_player(self):
        context = normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["internalWallCellIds"] = [layout["playerCellId"]]

        with self.assertRaisesRegex(ValueError, "playerCellId"):
            build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_rejects_duplicate_wall(self):
        context = normalize_pc_level_request(self.context)
        wall_id = find_cell_id(context, 5, 5)
        layout = make_layout(context)
        layout["internalWallCellIds"] = [wall_id, wall_id]

        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_rejects_malformed_wall_id(self):
        context = normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["internalWallCellIds"] = ["5"]

        with self.assertRaisesRegex(ValueError, "must be an integer"):
            build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_rejects_wall_overlapping_water(self):
        context = normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["internalWallCellIds"] = [
            context["allowedWaterAreas"][layout["waterAreaId"]]["cellIds"][0]
        ]

        with self.assertRaisesRegex(ValueError, "overlap the selected water"):
            build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_rejects_wall_touching_box_start(self):
        context = normalize_pc_level_request(self.context)
        layout = make_layout(context)
        layout["internalWallCellIds"] = [find_cell_id(context, 2, 3)]

        with self.assertRaisesRegex(ValueError, "not an allowed wall cell"):
            build_pc_level_candidate(
                layout,
                context,
            )

    def test_indexed_layout_allows_connected_activity_area_below_48(self):
        context = normalize_pc_level_request(self.context)
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

        service = PCLevelGenerationService(
            solvability_validator=lambda _rows, _width, _height: 0
        )
        result = service.build_candidate(layout, context)

        walkable_count = sum(
            tile in {".", "p", "s", "t"}
            for row in result["rows"]
            for tile in row
        )
        self.assertLess(walkable_count, 48)

    def test_layout_candidates_are_limited_stable_and_prioritize_five_walls(self):
        normalized = normalize_pc_level_request(self.context)
        normalized_again = normalize_pc_level_request(self.context)
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

    def test_candidate_validator_accepts_neutral_connected_wall_group(self):
        candidate = [list(row) for row in make_candidate()]

        for x, y in ((5, 5), (6, 5), (7, 5)):
            candidate[y][x] = "#"

        rows = ["".join(row) for row in candidate]
        result = validate_pc_level_candidate(
            {"rows": rows},
            {
                "width": 12,
                "height": 10,
                "sketchRows": make_sketch(),
            },
        )

        self.assertEqual(result["rows"], rows)

    def test_candidate_validator_accepts_neutral_disconnected_walls(self):
        candidate = [list(row) for row in make_candidate()]

        for x, y in ((3, 5), (7, 5), (8, 5)):
            candidate[y][x] = "#"

        rows = ["".join(row) for row in candidate]
        result = validate_pc_level_candidate(
            {"rows": rows},
            {
                "width": 12,
                "height": 10,
                "sketchRows": make_sketch(),
            },
        )

        self.assertEqual(result["rows"], rows)

    def test_legacy_competition_field_is_ignored_during_normalization(self):
        neutral = normalize_pc_level_request(self.context)
        legacy = normalize_pc_level_request(
            {
                **self.context,
                "competitionMode": "legacy-value",
            }
        )

        self.assertEqual(legacy, neutral)
        self.assertNotIn("competitionMode", legacy)

    def test_layout_candidate_walls_keep_outer_shell_clearance(self):
        normalized = normalize_pc_level_request(self.context)
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
        normalized = normalize_pc_level_request(self.context)

        for candidate in normalized["layoutCandidates"]:
            if len(candidate["internalWallCellIds"]) == 5:
                self.assertNotEqual(candidate["wallStyle"], "straight")

    def test_every_layout_candidate_is_complete_and_solvable(self):
        normalized = normalize_pc_level_request(self.context)

        for candidate in normalized["layoutCandidates"]:
            validated = validate_pc_level_candidate(
                {"rows": candidate["rows"]},
                normalized,
            )
            wall_count, water_count = validate_pc_required_features(
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
                contains_pc_two_by_two_block(generated_walls)
            )

            for y in range(1, 10):
                for x in range(12):
                    if validated["rows"][y][x] == "@":
                        self.assertNotEqual(
                            validated["rows"][y - 1][x],
                            "#",
                        )

            validate_pc_completed_level_solvability(
                validated["rows"],
                12,
                10,
            )

    def test_layout_selection_requires_one_known_integer_id(self):
        normalized = normalize_pc_level_request(self.context)

        self.assertEqual(
            parse_pc_layout_candidate_selection(
                {"layoutCandidateId": 0}
            ),
            0,
        )

        with self.assertRaisesRegex(ValueError, "only layoutCandidateId"):
            parse_pc_layout_candidate_selection(
                {"layoutCandidateId": 0, "extra": 1}
            )

        with self.assertRaisesRegex(ValueError, "must be an integer"):
            parse_pc_layout_candidate_selection(
                {"layoutCandidateId": "0"}
            )

        self.assertTrue(normalized["layoutCandidates"])

    def test_static_diagnostic_reports_box_without_target_route(self):
        diagnostic = diagnose_pc_static_solvability(
            {(0, 0), (1, 0), (0, 1), (1, 1)},
            ((0, 0),),
            {(1, 1)},
        )

        self.assertEqual(diagnostic["code"], "BOX_NO_TARGET_ROUTE")
        self.assertEqual(diagnostic["box"], (0, 0))

    def test_static_diagnostic_reports_impossible_target_matching(self):
        diagnostic = diagnose_pc_static_solvability(
            {(x, 0) for x in range(5)} | {(10, 10)},
            ((1, 0), (2, 0)),
            {(4, 0), (10, 10)},
        )

        self.assertEqual(
            diagnostic["code"],
            "TARGET_MATCHING_IMPOSSIBLE",
        )

    def test_static_diagnostic_reports_no_legal_initial_push(self):
        diagnostic = diagnose_pc_static_solvability(
            {(x, 0) for x in range(6)},
            ((2, 0), (3, 0)),
            {(4, 0), (5, 0)},
            [(0, 0)],
        )

        self.assertEqual(diagnostic["code"], "NO_LEGAL_INITIAL_PUSH")

    def test_wall_static_filter_removes_individually_blocking_wall(self):
        filtered = filter_pc_static_wall_coordinates(
            {(x, y) for y in range(5) for x in range(5)},
            [[0, 1]],
            [[0, 0]],
            [[0, 2], [4, 4]],
        )

        self.assertNotIn([0, 2], filtered)
        self.assertIn([4, 4], filtered)

    def test_wall_impact_score_prioritizes_route_obstruction(self):
        normalized = normalize_pc_level_request(self.context)
        normalized_again = normalize_pc_level_request(self.context)
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
        normalized = normalize_pc_level_request(self.context)

        self.assertTrue(normalized["layoutCandidates"])
        self.assertFalse(
            hasattr(backend_module, "find_pc_required_feature_capacity_area")
        )
        self.assertFalse(
            hasattr(backend_module, "prefilter_pc_water_area_candidates")
        )

    def test_safe_layout_prefilter_obeys_shared_state_budget(self):
        service = PCLevelGenerationService(
            settings=BackendSettings(pc_layout_prefilter_max_search_states=1)
        )
        with self.assertRaisesRegex(
            ValueError,
            "no safe completion",
        ):
            service.normalize_request(self.context)

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
        exception = PCSolvabilityError(
            "BOX_NO_TARGET_ROUTE",
            "no route",
            details={"code": "BOX_NO_TARGET_ROUTE", "box": (0, 1)},
        )

        feedback = build_pc_solvability_rejection_feedback(
            layout,
            context,
            rows,
            exception,
        )

        self.assertIn("reasonCode=BOX_NO_TARGET_ROUTE", feedback)
        self.assertIn("blockingWallCellIds=[1]", feedback)

    def test_indexed_candidate_still_requires_backend_solver(self):
        context = normalize_pc_level_request(self.context)

        def fail_solver(_rows, _width, _height):
            raise ValueError("candidate has no Sokoban solution")

        service = PCLevelGenerationService(solvability_validator=fail_solver)
        with self.assertRaisesRegex(ValueError, "no Sokoban solution"):
            service.build_candidate(
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

        pc_service = PCLevelGenerationService(candidate_factory=create_candidate)
        container = create_test_container(self.tmp_path, pc_service=pc_service)
        payload = {
            **self.context,
            "styleDescription": "Must not be consumed.",
            "ideaText": "Must not be consumed.",
            "maxAttempts": 2,
        }

        with TestClient(create_test_app(self.tmp_path, container=container)) as client:
            response = client.post("/generate-pc-level", json=payload)
        container.close()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("styleDescription", captured)
        self.assertNotIn("ideaText", captured)
        self.assertEqual(captured["sketchRows"], make_sketch())
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "1")

    def test_api_returns_rows_after_one_indexed_model_response(self):
        gateway = FakeLLMGateway([{"layoutCandidateId": 0}])
        container = create_test_container(self.tmp_path, llm_gateway=gateway)

        with TestClient(create_test_app(self.tmp_path, container=container)) as client:
            response = client.post(
                "/generate-pc-level",
                json={**self.context, "maxAttempts": 2},
            )
        container.close()

        self.assertEqual(response.status_code, 200)
        rows = response.json()["rows"]
        self.assertEqual(
            validate_pc_water_rectangles(rows, 12, 10),
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
        self.assertEqual(len(gateway.calls), 1)

    def test_api_normalizes_pc_request_only_once(self):
        normalize_calls = 0

        def counting_normalizer(context):
            nonlocal normalize_calls
            normalize_calls += 1
            return normalize_pc_level_request(context)

        gateway = FakeLLMGateway([{"layoutCandidateId": 0}])
        pc_service = PCLevelGenerationService(
            llm_gateway=gateway,
            request_normalizer=counting_normalizer,
        )
        container = create_test_container(
            self.tmp_path,
            llm_gateway=gateway,
            pc_service=pc_service,
        )

        with TestClient(create_test_app(self.tmp_path, container=container)) as client:
            response = client.post(
                "/generate-pc-level",
                json=self.context,
            )
        container.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(normalize_calls, 1)

    def test_invalid_layout_id_uses_highest_ranked_safe_candidate(self):
        gateway = FakeLLMGateway([{"layoutCandidateId": 999}])
        service = PCLevelGenerationService(llm_gateway=gateway)
        execution = service.create_candidate(
            self.context,
            request_id="pc-invalid-id-fallback",
            max_attempts=2,
        )

        normalized = normalize_pc_level_request(self.context)
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
            with self.subTest(code=code):
                gateway = FakeLLMGateway(
                    [
                        LLMServiceError(
                            code,
                            stage,
                            "simulated failure",
                            "pc-safe-fallback",
                            True,
                            1,
                            status_code,
                        )
                    ]
                )
                service = PCLevelGenerationService(llm_gateway=gateway)
                execution = service.create_candidate(
                    self.context,
                    request_id="pc-safe-fallback",
                    max_attempts=2,
                )

                validate_pc_required_features(
                    execution.value["rows"],
                    make_sketch(),
                    12,
                    10,
                    3,
                    1,
                )

    def test_model_timeout_uses_safe_candidate_without_second_call(self):
        gateway = FakeLLMGateway(
            [
                LLMServiceError(
                    "UPSTREAM_TIMEOUT",
                    "deepseek_request",
                    "timeout",
                    "pc-timeout-fallback",
                    True,
                    1,
                    504,
                )
            ]
        )
        service = PCLevelGenerationService(llm_gateway=gateway)
        execution = service.create_candidate(
            self.context,
            request_id="pc-timeout-fallback",
            max_attempts=2,
        )

        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(execution.attempts_used, 1)
        validate_pc_required_features(
            execution.value["rows"],
            make_sketch(),
            12,
            10,
            3,
            1,
        )

    def test_solver_failure_feedback_contains_selection_and_reason_code(self):
        normalized = normalize_pc_level_request(self.context)
        layout = make_required_layout(normalized)
        solver_error = PCSolvabilityError(
            "NO_SOLUTION_AFTER_SEARCH",
            "candidate has no Sokoban solution",
            searched_states=1234,
        )

        def fail_solver(_rows, _width, _height):
            raise solver_error

        logger = RecordingEventLogger()
        service = PCLevelGenerationService(
            solvability_validator=fail_solver,
            event_logger=logger,
        )
        with self.assertRaises(ValueError) as raised:
            service.build_candidate(
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
        rejected_events = logger.find_events("pc_candidate_rejected")
        self.assertEqual(len(rejected_events), 1)
        self.assertEqual(
            rejected_events[0]["reasonCode"],
            "NO_SOLUTION_AFTER_SEARCH",
        )

    def test_invalid_sketch_is_rejected_before_llm(self):
        payload = copy.deepcopy(self.context)
        payload["width"] = 11
        candidate_calls = []

        def record_candidate(*args, **kwargs):
            candidate_calls.append((args, kwargs))
            raise AssertionError("candidate_factory should not be called")

        pc_service = PCLevelGenerationService(candidate_factory=record_candidate)
        container = create_test_container(self.tmp_path, pc_service=pc_service)

        with TestClient(create_test_app(self.tmp_path, container=container)) as client:
            response = client.post("/generate-pc-level", json=payload)
        container.close()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(candidate_calls, [])

    def test_empty_previous_candidate_is_treated_as_first_attempt(self):
        payload = {
            **self.context,
            "previousCandidateRows": [],
        }
        normalized = normalize_pc_level_request(payload)

        self.assertIsNone(normalized["previousCandidateRows"])

    def test_sketch_requires_at_least_56_enclosed_cells(self):
        payload = copy.deepcopy(self.context)
        row = list(payload["sketchRows"][8])
        row[9] = "#"
        payload["sketchRows"][8] = "".join(row)

        with self.assertRaisesRegex(ValueError, "at least 56"):
            normalize_pc_level_request(payload)

    def test_sketch_requires_capacity_for_three_safe_walls_and_water(self):
        normalized = normalize_pc_level_request(self.context)

        self.assertTrue(normalized["layoutCandidates"])
        self.assertTrue(
            all(
                len(candidate["internalWallCellIds"]) >= 3
                for candidate in normalized["layoutCandidates"]
            )
        )
        self.assertTrue(
            all(
                candidate["waterAreaId"] is not None
                for candidate in normalized["layoutCandidates"]
            )
        )

    def test_editable_cells_mark_player_and_wall_permissions(self):
        normalized = normalize_pc_level_request(self.context)
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
            normalize_pc_level_request(payload)

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
        enclosed = find_pc_enclosed_cells(rows, 12, 10)
        enclosed_cells = {
            (x, y)
            for y in range(10)
            for x in range(12)
            if enclosed[y][x]
        }

        with self.assertRaisesRegex(ValueError, "no solvable open completion"):
            validate_pc_open_sketch_feasibility(
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
        enclosed = find_pc_enclosed_cells(rows, 12, 10)
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
            validate_pc_completed_level_solvability(
                completed,
                12,
                10,
            )

    def test_moving_fixed_tile_is_rejected(self):
        candidate = make_candidate()
        candidate[3] = " #..s...t.# "

        with self.assertRaisesRegex(ValueError, "fixed tile"):
            validate_pc_level_candidate(
                {"rows": candidate},
                normalize_pc_level_request(self.context),
            )

    def test_changing_outside_space_is_rejected(self):
        candidate = make_candidate()
        candidate[0] = ".           "

        with self.assertRaisesRegex(ValueError, "outside"):
            validate_pc_level_candidate(
                {"rows": candidate},
                normalize_pc_level_request(self.context),
            )

    def test_extra_target_is_rejected(self):
        candidate = make_candidate()
        candidate[4] = " #t.......# "

        with self.assertRaisesRegex(ValueError, "incomplete tile"):
            validate_pc_level_candidate(
                {"rows": candidate},
                normalize_pc_level_request(self.context),
            )

    def test_invalid_water_shape_is_rejected(self):
        candidate = make_candidate()
        candidate[5] = " #@.......# "

        with self.assertRaisesRegex(ValueError, "water area"):
            validate_pc_level_candidate(
                {"rows": candidate},
                normalize_pc_level_request(self.context),
            )

    def test_candidate_cannot_add_wall_next_to_box_start(self):
        candidate = make_candidate()
        candidate[3] = " ##s....t.# "

        with self.assertRaisesRegex(ValueError, "cannot touch a wall"):
            validate_pc_level_candidate(
                {"rows": candidate},
                normalize_pc_level_request(self.context),
            )

    def test_prompt_only_exposes_complete_layout_candidates(self):
        context = {
            **self.context,
            "previousCandidateRows": make_candidate(),
            "rejectionReason": "Unity solver found no solution.",
        }
        gateway = FakeLLMGateway([{"layoutCandidateId": 0}])
        service = PCLevelGenerationService(llm_gateway=gateway)
        service.create_candidate(
            context,
            request_id="pc-test",
            max_attempts=1,
        )

        prompt_text = "\n".join(
            message["content"] for message in gateway.calls[0]["messages"]
        )
        self.assertIn("layoutCandidateId", prompt_text)
        self.assertIn("layoutCandidates", prompt_text)
        self.assertNotIn("competitionMode", prompt_text)
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
        settings = BackendSettings.from_env(
            {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_PC_LEVEL_TIMEOUT_SECONDS": "15",
                "DEEPSEEK_PC_LEVEL_TEMPERATURE": "0.15",
            }
        )
        gateway = FakeLLMGateway([{"layoutCandidateId": 0}], settings=settings)
        service = PCLevelGenerationService(settings=settings, llm_gateway=gateway)
        service.create_candidate(
            self.context,
            request_id="pc-budget-test",
            max_attempts=2,
        )

        captured = gateway.calls[0]
        self.assertEqual(captured["max_attempts"], 1)
        self.assertEqual(captured["timeout_seconds"], 15.0)
        self.assertEqual(captured["temperature"], 0.15)
        self.assertEqual(captured["thinking_mode"], "disabled")
        self.assertEqual(captured["retry_error_codes"], set())


if __name__ == "__main__":
    unittest.main()
