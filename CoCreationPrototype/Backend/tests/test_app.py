import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_DIR.parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as backend
from level_validation import summarize_stage_changes
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
        self.assertIn("cocreation-pixel-20260812-2", index_response.text)
        self.assertIn("cocreation-guided-20260812-5", index_response.text)
        self.assertIn('<html lang="zh-CN">', index_response.text)
        self.assertEqual(css_response.status_code, 200)
        self.assertIn("--bg: #6f9d31", css_response.text)
        self.assertIn("--wood: #8b562c", css_response.text)
        self.assertIn("--pixel-shadow: 4px 4px 0", css_response.text)
        self.assertEqual(js_response.status_code, 200)
        self.assertIn("/api/sessions/", js_response.text)
        self.assertIn("play-attempts", js_response.text)
        self.assertIn("guidance-offer", js_response.text)
        self.assertIn('language: "zh-CN"', js_response.text)
        self.assertIn('hash.playReturn', js_response.text)
        self.assertIn('status === "sync_failed"', js_response.text)
        self.assertIn('status === "load_failed"', js_response.text)
        self.assertIn('playSyncFailed', js_response.text)
        self.assertIn('playLoadFailed', js_response.text)
        self.assertIn('pendingMessageKey()', js_response.text)
        self.assertIn('recoverPendingMessage()', js_response.text)
        self.assertIn('chatWaiting', js_response.text)
        self.assertIn('chatRetryPending', js_response.text)
        self.assertIn('selectedStageTurns()', js_response.text)
        self.assertIn('turn.versionId === state.selectedVersionId', js_response.text)
        self.assertIn('version?.openingTurnId', js_response.text)
        self.assertIn('supersededAssessmentTurnIds', js_response.text)
        self.assertIn('return [openingTurn, ...directTurns]', js_response.text)
        self.assertIn('noStageConversation', js_response.text)
        self.assertIn('id="chatRequestStatus"', index_response.text)
        self.assertIn('.chat-request-status', css_response.text)
        self.assertIn('.map-panel .palette-button small', css_response.text)
        self.assertIn('font-size: 10px', css_response.text)
        self.assertNotIn("createAssessmentCard", js_response.text)

    def test_stage_change_summary_classifies_sokoban_components(self):
        before = [
            "############",
            "#..........#",
            "#..#.......#",
            "#...@......#",
            "#...p......#",
            "#...s.t....#",
            "#..........#",
            "#..........#",
            "#..........#",
            "############",
        ]
        after = [
            "#####.######",
            "#..........#",
            "#...#......#",
            "#....@.....#",
            "#....p.....#",
            "#....s.t...#",
            "#..........#",
            "#..........#",
            "#..........#",
            "############",
        ]

        summary = summarize_stage_changes(before, after)

        self.assertEqual(
            summary["components"],
            ["outerShell", "water", "internalWalls", "boxes", "targets", "player"],
        )
        self.assertEqual(summary["changedCellCount"], 11)
        self.assertEqual(summary["componentCellCounts"]["player"], 2)

    def test_cocreation_frontend_uses_active_dashboard_visual_contract(self):
        dashboard_css = (REPOSITORY_ROOT / "Frontend" / "styles.css").read_text(
            encoding="utf-8"
        )
        cocreation_css = (
            REPOSITORY_ROOT / "CoCreationPrototype" / "Frontend" / "styles.css"
        ).read_text(encoding="utf-8")
        shared_tokens = (
            "--bg: #6f9d31",
            "--wood: #8b562c",
            "--stone-dark: #41494d",
            "--purple: #67478c",
            "--pixel-shadow: 4px 4px 0",
            'font-family: Consolas, "Courier New", monospace',
            "border: 3px solid var(--stone-dark)",
            "align-items: stretch",
        )

        for token in shared_tokens:
            self.assertIn(token, dashboard_css)
            self.assertIn(token, cocreation_css)

    def test_new_session_request_defaults_to_chinese(self):
        request = backend.CreateSessionRequest(
            rows=list(backend.SAMPLE_ROWS),
            initialDraftMethod="partial_completion",
            idempotencyKey="default-language-contract",
        )

        self.assertEqual(request.language, "zh-CN")

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
        self.assertIn("at most one central question", system_prompt)
        self.assertIn("tentative, correctable hypothesis", system_prompt)
        self.assertIn("Every difficulty statement must explicitly use", system_prompt)
        self.assertIn("Stage number is a saved-version index", system_prompt)
        self.assertIn("offer_revision", system_prompt)
        self.assertIn("edit the level directly with the tile tools", system_prompt)
        self.assertIn("newly saved human_edit Stage", system_prompt)
        self.assertIn("only turns attached to this saved Stage", system_prompt)
        self.assertIn("accepted LLM proposal may be carried forward", system_prompt)
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
