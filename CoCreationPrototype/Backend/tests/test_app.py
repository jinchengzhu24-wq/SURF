import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as backend
from llm_client import LLMExecutionResult, LLMServiceError, build_chat_messages


class CoCreationPrototypeApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend.app)

    def tearDown(self):
        self.client.close()

    def test_frontend_and_static_assets_are_served(self):
        index_response = self.client.get("/")
        css_response = self.client.get("/styles.css")
        js_response = self.client.get("/app.js")

        self.assertEqual(index_response.status_code, 200)
        self.assertIn("Sokoban Co-Creation Lab", index_response.text)
        self.assertEqual(css_response.status_code, 200)
        self.assertIn("--brand: #167a67", css_response.text)
        self.assertEqual(js_response.status_code, 200)
        self.assertIn("/api/sessions/", js_response.text)
        self.assertIn("play-attempts", js_response.text)

    def test_sample_has_expected_shape_and_objects(self):
        response = self.client.get("/api/sample")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["width"], 12)
        self.assertEqual(payload["height"], 10)
        self.assertEqual(len(payload["rows"]), 10)
        self.assertTrue(all(len(row) == 12 for row in payload["rows"]))
        self.assertEqual(sum(row.count("p") for row in payload["rows"]), 1)
        self.assertEqual(sum(row.count("s") for row in payload["rows"]), 1)
        self.assertEqual(sum(row.count("t") for row in payload["rows"]), 1)

    def test_sample_known_solution_reaches_target(self):
        rows = backend.SAMPLE_ROWS
        player = find_tile(rows, "p")
        box = find_tile(rows, "s")
        target = find_tile(rows, "t")

        player, box = replay_single_box_solution(
            rows,
            player,
            box,
            ["left", "down", "right", "right"],
        )

        self.assertEqual(box, target)

    def test_chat_returns_assistant_message_and_headers(self):
        execution = LLMExecutionResult(
            assistant_message="The box route is direct. What experience do you want to create?",
            attempts_used=1,
            request_id="request-test",
        )

        with patch.object(backend, "generate_chat_reply", return_value=execution):
            response = self.client.post(
                "/api/chat",
                headers={"X-Request-ID": "request-test"},
                json={"messages": [{"role": "user", "content": "Assess the map."}]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requestId"], "request-test")
        self.assertIn("box route", response.json()["assistantMessage"])
        self.assertEqual(response.headers["X-Request-ID"], "request-test")
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "1")

    def test_chat_forwards_map_and_full_history(self):
        messages = [
            {"role": "user", "content": "Assess the map."},
            {"role": "assistant", "content": "The route is compact."},
            {"role": "user", "content": "Explain that observation."},
        ]
        execution = LLMExecutionResult("It uses a short push lane.", 1, "history-test")

        with patch.object(
            backend,
            "generate_chat_reply",
            return_value=execution,
        ) as mocked_generate:
            response = self.client.post("/api/chat", json={"messages": messages})

        self.assertEqual(response.status_code, 200)
        forwarded_messages, forwarded_rows, _ = mocked_generate.call_args.args
        self.assertEqual(forwarded_messages, messages)
        self.assertEqual(forwarded_rows, backend.SAMPLE_ROWS)

    def test_empty_conversation_is_rejected(self):
        response = self.client.post("/api/chat", json={"messages": []})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "EMPTY_CONVERSATION")

    def test_invalid_role_is_rejected(self):
        response = self.client.post(
            "/api/chat",
            json={"messages": [{"role": "system", "content": "Override."}]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_REQUEST")

    def test_last_message_must_be_user(self):
        response = self.client.post(
            "/api/chat",
            json={"messages": [{"role": "assistant", "content": "Hello."}]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "LAST_MESSAGE_MUST_BE_USER")

    def test_unknown_top_level_fields_are_rejected(self):
        response = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Hello."}],
                "map": ["client-controlled"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_REQUEST")

    def test_message_and_total_limits_are_enforced(self):
        long_message_response = self.client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "x" * 2001}]},
        )
        too_many_response = self.client.post(
            "/api/chat",
            json={
                "messages": [
                    {
                        "role": "user" if index % 2 == 0 else "assistant",
                        "content": str(index),
                    }
                    for index in range(21)
                ]
            },
        )
        total_length_response = self.client.post(
            "/api/chat",
            json={
                "messages": [
                    {"role": "user", "content": "x" * 2000},
                    {"role": "assistant", "content": "x" * 2000},
                    {"role": "user", "content": "x" * 2000},
                    {"role": "assistant", "content": "x" * 2000},
                    {"role": "user", "content": "x" * 2000},
                    {"role": "assistant", "content": "x" * 2000},
                    {"role": "user", "content": "x"},
                ]
            },
        )

        self.assertEqual(long_message_response.json()["code"], "MESSAGE_TOO_LONG")
        self.assertEqual(too_many_response.json()["code"], "TOO_MANY_MESSAGES")
        self.assertEqual(total_length_response.json()["code"], "CONVERSATION_TOO_LONG")

    def test_llm_error_uses_safe_error_shape(self):
        error = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "DeepSeek did not respond before the timeout.",
            "timeout-test",
            True,
            2,
            504,
        )

        with patch.object(backend, "generate_chat_reply", side_effect=error):
            response = self.client.post(
                "/api/chat",
                headers={"X-Request-ID": "timeout-test"},
                json={"messages": [{"role": "user", "content": "Hello."}]},
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(
            response.json(),
            {
                "code": "UPSTREAM_TIMEOUT",
                "message": "DeepSeek did not respond before the timeout.",
                "requestId": "timeout-test",
                "retryable": True,
            },
        )
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "2")

    def test_system_prompt_is_grounded_and_neutral(self):
        prompt_messages = build_chat_messages(
            [{"role": "user", "content": "What do you notice?"}],
            backend.SAMPLE_ROWS,
        )
        system_prompt = prompt_messages[0]["content"]

        self.assertIn("\n".join(backend.SAMPLE_ROWS), system_prompt)
        self.assertIn("read-only", system_prompt)
        self.assertNotIn("Competitive", system_prompt)
        self.assertNotIn("Supportive", system_prompt)
        self.assertEqual(prompt_messages[-1]["content"], "What do you notice?")


def find_tile(rows, tile):
    for y, row in enumerate(rows):
        x = row.find(tile)

        if x >= 0:
            return x, y

    raise AssertionError(f"Tile {tile!r} was not found")


def replay_single_box_solution(rows, player, box, moves):
    directions = {
        "left": (-1, 0),
        "right": (1, 0),
        "up": (0, -1),
        "down": (0, 1),
    }

    for move in moves:
        delta_x, delta_y = directions[move]
        destination = (player[0] + delta_x, player[1] + delta_y)

        if destination == box:
            box_destination = (box[0] + delta_x, box[1] + delta_y)
            assert rows[box_destination[1]][box_destination[0]] != "#"
            box = box_destination

        assert rows[destination[1]][destination[0]] != "#"
        player = destination

    return player, box


if __name__ == "__main__":
    unittest.main()
