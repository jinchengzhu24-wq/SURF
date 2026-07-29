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
            value = kwargs["validator"]({"rows": make_candidate()})
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

    def test_pc_model_call_uses_single_attempt_and_dedicated_timeout(self):
        captured = {}

        def execute_json_request(**kwargs):
            captured.update(kwargs)
            value = kwargs["validator"]({"rows": make_candidate()})
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

        self.assertEqual(captured["max_attempts"], 1)
        self.assertEqual(captured["timeout_seconds"], 60.0)
        self.assertEqual(captured["thinking_mode"], "disabled")


if __name__ == "__main__":
    unittest.main()
