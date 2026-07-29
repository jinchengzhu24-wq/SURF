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


def make_layout():
    return {
        "player": {"x": 2, "y": 2},
        "internalWalls": [],
        "waterAreas": [],
    }


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

    def test_coordinate_layout_builds_complete_rows(self):
        result = backend.build_pc_level_candidate(
            make_layout(),
            backend.normalize_pc_level_request(self.context),
        )

        self.assertEqual(result, {"rows": make_candidate()})
        self.assertTrue(all(len(row) == 12 for row in result["rows"]))

    def test_coordinate_layout_builds_valid_wall_and_water(self):
        layout = make_layout()
        layout["internalWalls"] = [{"x": 6, "y": 2}]
        layout["waterAreas"] = [
            {"x": 4, "y": 2, "width": 2, "height": 2},
        ]

        result = backend.build_pc_level_candidate(
            layout,
            backend.normalize_pc_level_request(self.context),
        )

        self.assertEqual(result["rows"][2], " #p.@@#...# ")
        self.assertEqual(result["rows"][3], " #.s@@..t.# ")

    def test_coordinate_layout_rejects_out_of_bounds_player(self):
        layout = make_layout()
        layout["player"] = {"x": 12, "y": 2}

        with self.assertRaisesRegex(ValueError, "outside the map"):
            backend.build_pc_level_candidate(
                layout,
                backend.normalize_pc_level_request(self.context),
            )

    def test_coordinate_layout_rejects_fixed_tile_overlap(self):
        layout = make_layout()
        layout["player"] = {"x": 3, "y": 3}

        with self.assertRaisesRegex(ValueError, "fixed sketch tile"):
            backend.build_pc_level_candidate(
                layout,
                backend.normalize_pc_level_request(self.context),
            )

    def test_coordinate_layout_rejects_element_overlap(self):
        layout = make_layout()
        layout["internalWalls"] = [{"x": 2, "y": 2}]

        with self.assertRaisesRegex(ValueError, "overlaps"):
            backend.build_pc_level_candidate(
                layout,
                backend.normalize_pc_level_request(self.context),
            )

    def test_coordinate_layout_rejects_duplicate_wall(self):
        layout = make_layout()
        layout["internalWalls"] = [
            {"x": 5, "y": 5},
            {"x": 5, "y": 5},
        ]

        with self.assertRaisesRegex(ValueError, "overlaps"):
            backend.build_pc_level_candidate(
                layout,
                backend.normalize_pc_level_request(self.context),
            )

    def test_coordinate_layout_rejects_invalid_water_size(self):
        layout = make_layout()
        layout["waterAreas"] = [
            {"x": 5, "y": 5, "width": 1, "height": 2},
        ]

        with self.assertRaisesRegex(ValueError, "2-4 by 2-4"):
            backend.build_pc_level_candidate(
                layout,
                backend.normalize_pc_level_request(self.context),
            )

    def test_coordinate_layout_rejects_water_over_fixed_tile(self):
        layout = make_layout()
        layout["waterAreas"] = [
            {"x": 2, "y": 2, "width": 2, "height": 2},
        ]

        with self.assertRaisesRegex(ValueError, "fixed sketch tile"):
            backend.build_pc_level_candidate(
                layout,
                backend.normalize_pc_level_request(self.context),
            )

    def test_coordinate_layout_rejects_new_wall_touching_box_start(self):
        layout = make_layout()
        layout["internalWalls"] = [{"x": 2, "y": 3}]

        with self.assertRaisesRegex(ValueError, "cannot touch a wall"):
            backend.build_pc_level_candidate(
                layout,
                backend.normalize_pc_level_request(self.context),
            )

    def test_coordinate_layout_rejects_disconnected_walkable_area(self):
        layout = make_layout()
        layout["internalWalls"] = [
            {"x": 6, "y": y}
            for y in range(2, 9)
        ]

        with self.assertRaisesRegex(ValueError, "connected component"):
            backend.build_pc_level_candidate(
                layout,
                backend.normalize_pc_level_request(self.context),
            )

    def test_coordinate_layout_rejects_activity_area_below_48(self):
        layout = make_layout()
        layout["waterAreas"] = [
            {"x": 4, "y": 5, "width": 3, "height": 3},
        ]

        with self.assertRaisesRegex(ValueError, "at least 48"):
            backend.build_pc_level_candidate(
                layout,
                backend.normalize_pc_level_request(self.context),
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

    def test_api_returns_rows_after_internal_coordinate_generation(self):
        def execute_json_request(**kwargs):
            value = kwargs["validator"](make_layout())
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
        self.assertEqual(response.json(), {"rows": make_candidate()})
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "1")

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
            value = kwargs["validator"](make_layout())
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
        self.assertIn("internalWalls", prompt_text)
        self.assertIn("Do not return map rows", prompt_text)

    def test_pc_model_call_retries_only_model_output_failures(self):
        captured = {}

        def execute_json_request(**kwargs):
            captured.update(kwargs)
            value = kwargs["validator"](make_layout())
            return LLMExecutionResult(value, 1, kwargs["request_id"])

        with (
            patch.dict(
                backend.os.environ,
                {"DEEPSEEK_PC_LEVEL_TIMEOUT_SECONDS": "60"},
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
        self.assertEqual(captured["thinking_mode"], "disabled")
        self.assertEqual(
            captured["retry_error_codes"],
            {"MODEL_JSON_INVALID", "MODEL_VALIDATION_FAILED"},
        )


if __name__ == "__main__":
    unittest.main()
