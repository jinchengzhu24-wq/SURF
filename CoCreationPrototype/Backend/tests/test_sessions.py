import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as backend
import repository
from llm_client import (
    LLMExecutionResult,
    LLMServiceError,
    TranslationExecutionResult,
)


SAMPLE_ROWS = list(backend.SAMPLE_ROWS)
EDITED_ROWS = list(SAMPLE_ROWS)
EDITED_ROWS[4] = "#..p.......#"
TARGET_SHIFT_ROWS = list(SAMPLE_ROWS)
TARGET_SHIFT_ROWS[5] = "#...s..t...#"
PLAYER_MOVE_OPERATIONS = [
    {"row": 5, "column": 5, "to": "."},
    {"row": 5, "column": 4, "to": "p"},
]
PLAYER_MOVE_CONTRACT = {
    "schemaVersion": 1,
    "authorizedBrief": "Move the player start left.",
    "revisionPlan": {
        "strategies": [{
            "effect": "relocate_start",
            "focus": None,
            "operators": ["move_player"],
            "preserve": ["outer_shell", "unrelated_areas"],
            "editBudget": 2,
            "metricGoals": [],
        }],
    },
    "strategies": [{
        "strategyIndex": 1,
        "effect": "relocate_start",
        "focus": None,
        "allowedOperators": ["move_player"],
        "preserve": ["outer_shell", "unrelated_areas"],
        "minimumChangedCells": 2,
        "maximumChangedCells": 2,
        "metricGoals": [],
    }],
    "explicitlyRelaxedByDesigner": False,
}


class CoCreationSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        repository.DATABASE_PATH = Path(cls.temp_directory.name) / "test.sqlite3"
        repository.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.temp_directory.cleanup()

    def setUp(self):
        with repository.connect(immediate=True) as database:
            for table in (
                "audit_events",
                "designer_intentions",
                "play_attempts",
                "designer_decisions",
                "change_proposals",
                "llm_assessments",
                "turn_translations",
                "conversation_turns",
                "level_versions",
                "design_sessions",
            ):
                database.execute(f"DELETE FROM {table}")

        self.client = TestClient(backend.app)
        self.session_id, self.integration_token = self.create_and_open_session()

    def tearDown(self):
        self.client.close()

    def create_and_open_session(self, creation_key="unity_test_001"):
        response = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": creation_key,
                "matchId": "match-test",
                "playerNumber": 1,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        fragment = parse_qs(urlparse(payload["launchUrl"]).fragment)
        exchange = self.client.post(
            f"/api/sessions/{payload['sessionId']}/browser-access",
            json={"bootstrapToken": fragment["bootstrap"][0]},
        )
        self.assertEqual(exchange.status_code, 200, exchange.text)
        return payload["sessionId"], payload["integrationToken"]

    def create_and_open_demo_session(self, creation_key):
        response = self.client.post(
            "/api/demo-sessions",
            json={
                "language": "en",
                "idempotencyKey": creation_key,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        fragment = parse_qs(urlparse(payload["launchUrl"]).fragment)
        self.assertEqual(fragment["mode"][0], "demo")
        exchange = self.client.post(
            f"/api/sessions/{payload['sessionId']}/browser-access",
            json={"bootstrapToken": fragment["bootstrap"][0]},
        )
        self.assertEqual(exchange.status_code, 200, exchange.text)
        return payload["sessionId"]

    def read_session(self):
        response = self.client.get(f"/api/sessions/{self.session_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_initial_stage_matches_unity_rows_and_bootstrap_is_single_use(self):
        session = self.read_session()

        self.assertEqual(session["initialDraftMethod"], "partial_completion")
        self.assertEqual(session["versions"][0]["stageNumber"], 1)
        self.assertEqual(session["versions"][0]["rows"], SAMPLE_ROWS)

        second = self.client.post(
            f"/api/sessions/{self.session_id}/browser-access",
            json={"bootstrapToken": backend.derive_token("bootstrap", self.session_id)},
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "BOOTSTRAP_TOKEN_USED")

    def test_formal_session_records_blueprint_to_cocreation_handoff(self):
        with repository.connect() as database:
            event = database.execute(
                """
                SELECT payload_json FROM audit_events
                WHERE session_id = ? AND event_type = 'agent_handoff'
                ORDER BY id
                """,
                (self.session_id,),
            ).fetchone()

        self.assertIsNotNone(event)
        payload = repository.load_json(event["payload_json"])
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["fromAgent"], "blueprint_planning")
        self.assertEqual(payload["toAgent"], "co_creation_chat")
        self.assertEqual(payload["artifactType"], "validated_initial_stage")
        self.assertEqual(payload["status"], "confirmed")
        self.assertEqual(
            payload["artifact"]["versionId"],
            self.read_session()["currentVersionId"],
        )
        self.assertEqual(payload["evidence"][0]["type"], "deterministic_solver")

    def test_demo_session_has_no_deadline_and_skips_online_sync(self):
        with patch.object(backend, "synchronize_version_with_online_match") as sync:
            demo_session_id = self.create_and_open_demo_session("demo_no_deadline_001")

        sync.assert_not_called()
        response = self.client.get(f"/api/sessions/{demo_session_id}")
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()

        self.assertTrue(session["demoMode"])
        self.assertEqual(session["initialDraftMethod"], "algorithm_demo")
        self.assertIsNone(session["matchId"])
        self.assertIsNone(session["playerNumber"])
        self.assertIsNone(session["deadlineStartedAt"])
        self.assertIsNone(session["deadlineAt"])
        self.assertFalse(session["deadlineExpired"])
        self.assertIsNone(session["remainingSeconds"])
        self.assertEqual(len(session["versions"]), 1)
        self.assertEqual(session["versions"][0]["source"], "initial")

        with repository.connect() as database:
            events = [
                row
                for row in database.execute(
                    "SELECT event_type, payload_json FROM audit_events WHERE session_id = ?",
                    (demo_session_id,),
                ).fetchall()
            ]
        self.assertNotIn("deadline_started", [event["event_type"] for event in events])
        created_event = next(
            event for event in events if event["event_type"] == "session_created"
        )
        audit_payload = json.loads(created_event["payload_json"])
        self.assertIsInstance(audit_payload["generationSeed"], int)
        self.assertGreaterEqual(audit_payload["generationAttempts"], 1)
        self.assertIn("qualityScore", audit_payload["generationSummary"])
        self.assertIn("waterAreas", audit_payload["generationSummary"])

    def test_new_demo_session_removes_only_previous_demo_data(self):
        first_demo_id = self.create_and_open_demo_session("demo_cleanup_001")
        with repository.connect(immediate=True) as database:
            repository.record_event(
                database,
                first_demo_id,
                "test_marker",
                {"value": "old"},
                "2026-08-30T00:00:00Z",
            )

        second_demo_id = self.create_and_open_demo_session("demo_cleanup_002")
        self.assertNotEqual(first_demo_id, second_demo_id)

        with repository.connect() as database:
            old_session = database.execute(
                "SELECT id FROM design_sessions WHERE id = ?",
                (first_demo_id,),
            ).fetchone()
            demo_count = database.execute(
                "SELECT COUNT(*) FROM design_sessions WHERE demo_mode = 1"
            ).fetchone()[0]
            old_versions = database.execute(
                "SELECT COUNT(*) FROM level_versions WHERE session_id = ?",
                (first_demo_id,),
            ).fetchone()[0]
            formal_session = database.execute(
                "SELECT id FROM design_sessions WHERE id = ? AND demo_mode = 0",
                (self.session_id,),
            ).fetchone()

        self.assertIsNone(old_session)
        self.assertEqual(demo_count, 1)
        self.assertEqual(old_versions, 0)
        self.assertIsNotNone(formal_session)

    def test_demo_generation_failure_keeps_previous_demo_session(self):
        first_demo_id = self.create_and_open_demo_session("demo_failure_001")

        with patch.object(backend, "generate_demo_level", side_effect=RuntimeError("generation failed")):
            with self.assertRaises(RuntimeError):
                backend.create_demo_session(
                    backend.DemoSessionRequest(
                        language="en",
                        idempotencyKey="demo_failure_002",
                    )
                )

        with repository.connect() as database:
            retained = database.execute(
                "SELECT id FROM design_sessions WHERE id = ? AND demo_mode = 1",
                (first_demo_id,),
            ).fetchone()
        self.assertIsNotNone(retained)

    def test_session_creation_rejects_removed_dg_context_field(self):
        response = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "description_generation",
                "language": "en",
                "idempotencyKey": "unity_no_dg_context",
                "dgContextJson": '{"finalDifficulty":"Hard"}',
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_new_stage_opening_persists_guidance(self):
        version_id = self.read_session()["currentVersionId"]
        execution = LLMExecutionResult(
            "The box and target share a compact central route.\n\nWhat would you like another player to notice first?",
            1,
            "opening-request",
            assessment={
                "solutionSummary": "The solver found a direct route.",
                "difficultyOpinion": "This looks approachable to me.",
                "features": ["Compact route"],
                "suggestions": ["Discuss the opening choice"],
                "satisfactionQuestion": "What would you like another player to notice first?",
            },
            model="mock-model",
            guidance={
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "What would you like another player to notice first?",
                "proposalOffer": None,
            },
        )

        with patch.object(
            backend,
            "generate_stage_assessment",
            return_value=execution,
        ) as mocked:
            response = self.client.post(
                f"/api/sessions/{self.session_id}/versions/{version_id}/assessments",
                json={"idempotencyKey": "opening_001"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["turns"][-1]["guidance"]["move"], "observe_stage")
        self.assertEqual(mocked.call_args.kwargs["stage_context"]["stageNumber"], 1)
        self.assertEqual(
            mocked.call_args.kwargs["stage_context"]["initialDraftMethod"],
            "partial_completion",
        )

    def test_opening_and_first_user_turn_sync_as_three_part_record(self):
        version_id = self.read_session()["currentVersionId"]
        opening = LLMExecutionResult(
            "I notice a clear central route.",
            1,
            "opening-flow-request",
            assessment={},
            model="mock-model",
            guidance={
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "What should stand out first?",
                "proposalOffer": None,
            },
        )
        reply = LLMExecutionResult(
            "I would keep that route visible.",
            1,
            "reply-flow-request",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
        )

        with patch.object(
            backend,
            "synchronize_cocreation_event_with_online_match",
        ) as sync, patch.object(
            backend,
            "generate_stage_assessment",
            return_value=opening,
        ), patch.object(
            backend,
            "generate_chat_reply",
            return_value=reply,
        ):
            assessed = self.client.post(
                f"/api/sessions/{self.session_id}/versions/{version_id}/assessments",
                json={"idempotencyKey": "opening_flow_001"},
            )
            self.assertEqual(assessed.status_code, 200, assessed.text)
            messaged = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Could the route feel fair?",
                    "baseVersionId": version_id,
                    "idempotencyKey": "opening_flow_message_001",
                },
            )
            second_message = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "What should I adjust next?",
                    "baseVersionId": version_id,
                    "idempotencyKey": "opening_flow_message_002",
                },
            )

        self.assertEqual(messaged.status_code, 200, messaged.text)
        self.assertEqual(second_message.status_code, 200, second_message.text)
        events = [call.args[1] for call in sync.call_args_list]
        self.assertEqual(events[0]["eventType"], "opening")
        turn = next(event for event in events if event["eventType"] == "turn")
        self.assertEqual(turn["openingAssistantText"], opening.assistant_message)
        self.assertEqual(turn["userText"], "Could the route feel fair?")
        self.assertEqual(turn["assistantText"], reply.assistant_message)
        regular_turn = [event for event in events if event["eventType"] == "turn"][1]
        self.assertNotIn("openingAssistantText", regular_turn)

    def test_legacy_database_receives_nullable_guidance_column(self):
        original_path = repository.DATABASE_PATH

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            legacy_path = Path(directory) / "legacy.sqlite3"
            database = sqlite3.connect(legacy_path)
            database.executescript(
                repository.SCHEMA.replace("    guidance_json TEXT,\n", "", 1)
            )
            database.close()

            try:
                repository.DATABASE_PATH = legacy_path
                repository.initialize_database()
                database = sqlite3.connect(legacy_path)
                columns = {
                    row[1]
                    for row in database.execute(
                        "PRAGMA table_info(conversation_turns)"
                    ).fetchall()
                }
                database.close()
            finally:
                repository.DATABASE_PATH = original_path

        self.assertIn("guidance_json", columns)

    def test_manual_stage_requires_current_base_and_is_idempotent(self):
        stage_one = self.read_session()["currentVersionId"]
        request = {
            "rows": EDITED_ROWS,
            "baseVersionId": stage_one,
            "idempotencyKey": "manual_edit_001",
            "summary": "Move player left",
        }
        response = self.client.post(
            f"/api/sessions/{self.session_id}/versions",
            json=request,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["versions"]), 2)

        retry = self.client.post(
            f"/api/sessions/{self.session_id}/versions",
            json=request,
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(len(retry.json()["versions"]), 2)

        conflict = self.client.post(
            f"/api/sessions/{self.session_id}/versions",
            json={**request, "idempotencyKey": "manual_edit_002"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "VERSION_CONFLICT")

    def test_manual_stage_validation_failure_returns_details_without_creating_version(self):
        stage_one = self.read_session()["currentVersionId"]
        open_wall_rows = list(SAMPLE_ROWS)
        open_wall_rows[0] = "####.#######"

        response = self.client.post(
            f"/api/sessions/{self.session_id}/versions",
            json={
                "rows": open_wall_rows,
                "baseVersionId": stage_one,
                "idempotencyKey": "manual_invalid_outer_wall_001",
                "summary": "Open the outer wall",
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "OPEN_OUTER_WALL")
        self.assertEqual(
            response.json()["details"],
            {"row": 1, "column": 5, "tile": "."},
        )
        session = self.read_session()
        self.assertEqual(session["currentVersionId"], stage_one)
        self.assertEqual(len(session["versions"]), 1)

    def test_manual_stage_validation_errors_cover_shape_counts_and_solvability(self):
        stage_one = self.read_session()["currentVersionId"]
        invalid_player_rows = list(SAMPLE_ROWS)
        invalid_player_rows[4] = "#..........#"
        invalid_width_rows = list(SAMPLE_ROWS)
        invalid_width_rows[0] = "#############"
        unsolvable_rows = list(SAMPLE_ROWS)
        unsolvable_rows[1] = "#s.........#"
        unsolvable_rows[5] = "#.....t....#"

        cases = (
            ("invalid_player", invalid_player_rows, "INVALID_PLAYER_COUNT"),
            ("invalid_width", invalid_width_rows, "INVALID_WIDTH"),
            ("unsolvable", unsolvable_rows, "UNSOLVABLE_LEVEL"),
        )

        for suffix, rows, code in cases:
            with self.subTest(code=code):
                response = self.client.post(
                    f"/api/sessions/{self.session_id}/versions",
                    json={
                        "rows": rows,
                        "baseVersionId": stage_one,
                        "idempotencyKey": f"manual_invalid_{suffix}_001",
                        "summary": "Invalid manual map",
                    },
                )

                self.assertEqual(response.status_code, 400, response.text)
                payload = response.json()
                self.assertEqual(payload["code"], code)
                self.assertIsInstance(payload.get("details"), dict)

        session = self.read_session()
        self.assertEqual(session["currentVersionId"], stage_one)
        self.assertEqual(len(session["versions"]), 1)

    def test_manual_stage_assessment_receives_deterministic_change_summary(self):
        stage_one = self.read_session()["currentVersionId"]
        saved = self.client.post(
            f"/api/sessions/{self.session_id}/versions",
            json={
                "rows": EDITED_ROWS,
                "baseVersionId": stage_one,
                "idempotencyKey": "manual_context_001",
                "summary": "Move player left",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        stage_two = saved.json()["currentVersionId"]
        execution = LLMExecutionResult(
            "I noticed the player-position change and would like to discuss its effect.",
            1,
            "manual-opening-request",
            assessment={
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "In my view, the opening is more direct.",
                "features": ["Changed player start"],
                "suggestions": ["Consider the first route choice"],
                "satisfactionQuestion": "Does this opening match your intention?",
            },
            model="mock-model",
            guidance={
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "Does this opening match your intention?",
                "proposalOffer": None,
            },
        )

        with patch.object(
            backend,
            "generate_stage_assessment",
            return_value=execution,
        ) as mocked:
            assessed = self.client.post(
                f"/api/sessions/{self.session_id}/versions/{stage_two}/assessments",
                json={"idempotencyKey": "manual_assessment_001"},
            )

        self.assertEqual(assessed.status_code, 200, assessed.text)
        context = mocked.call_args.kwargs["stage_context"]
        self.assertEqual(context["source"], "human_edit")
        self.assertEqual(context["changeSummary"]["components"], ["player"])
        self.assertEqual(context["changeSummary"]["componentCellCounts"]["player"], 2)

    def test_llm_receives_current_stage_history_and_latest_play_evidence(self):
        session = self.read_session()
        version_id = session["currentVersionId"]
        issued = self.client.post(
            f"/api/sessions/{self.session_id}/versions/{version_id}/play-attempts",
            json={"idempotencyKey": "play_before_chat"},
        ).json()
        query = parse_qs(urlparse(issued["playUrl"]).query)
        attempt_id = query["cocreationAttempt"][0]
        play_data = self.client.post(
            f"/api/play-attempts/{attempt_id}/bootstrap",
            json={"ticket": query["cocreationPlay"][0]},
        ).json()
        self.client.post(
            f"/api/play-attempts/{attempt_id}/complete",
            json={
                "attemptToken": play_data["attemptToken"],
                "durationSeconds": 9.5,
                "moveCount": 6,
                "pushCount": 2,
                "restartCount": 0,
                "minimumMoves": 4,
                "minimumPushes": 2,
            },
        )
        execution = LLMExecutionResult(
            "The route is compact. What experience are you aiming for?",
            1,
            "chat-request",
            assessment={
                "solutionSummary": "Direct route",
                "difficultyOpinion": "Likely easy",
                "features": ["Compact"],
                "suggestions": ["Play-test it"],
                "satisfactionQuestion": "Is this good enough for your intention?",
            },
            model="mock-model",
            guidance={
                "move": "reflect_on_play",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "Did the short route match your expectation?",
                "proposalOffer": None,
                "uiCues": [
                    {
                        "type": "manual_edit",
                        "text": "You can test an alternative with the right-side editor.",
                    }
                ],
            },
        )

        with patch.object(backend, "generate_chat_reply", return_value=execution) as mocked:
            response = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "What stands out?",
                    "baseVersionId": version_id,
                    "idempotencyKey": "chat_001",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        conversation, rows, _ = mocked.call_args.args
        self.assertEqual(rows, SAMPLE_ROWS)
        self.assertEqual(conversation[-1], {"role": "user", "content": "What stands out?"})
        self.assertEqual(mocked.call_args.kwargs["play_summary"]["moveCount"], 6)
        self.assertEqual(mocked.call_args.kwargs["play_summary"]["status"], "completed")
        self.assertEqual(mocked.call_args.kwargs["stage_context"]["stageNumber"], 1)
        self.assertEqual(mocked.call_args.kwargs["stage_context"]["source"], "initial")
        self.assertEqual(response.json()["turns"][-1]["role"], "assistant")
        self.assertEqual(
            response.json()["turns"][-1]["guidance"]["move"],
            "reflect_on_play",
        )
        self.assertEqual(
            response.json()["turns"][-1]["guidance"]["uiCues"][0]["type"],
            "manual_edit",
        )

    def test_llm_conversation_is_scoped_to_current_stage(self):
        stage_one = self.read_session()["currentVersionId"]
        first_execution = LLMExecutionResult(
            "Stage one response.",
            1,
            "stage-one-response",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
        )
        second_execution = LLMExecutionResult(
            "Stage two response.",
            1,
            "stage-two-response",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
        )

        with patch.object(
            backend,
            "generate_chat_reply",
            side_effect=[first_execution, second_execution],
        ) as mocked:
            first = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Discuss Stage one.",
                    "baseVersionId": stage_one,
                    "idempotencyKey": "stage_one_chat",
                },
            )
            self.assertEqual(first.status_code, 200, first.text)
            saved = self.client.post(
                f"/api/sessions/{self.session_id}/versions",
                json={
                    "rows": EDITED_ROWS,
                    "baseVersionId": stage_one,
                    "idempotencyKey": "stage_two_save",
                    "summary": "Move player left",
                },
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            stage_two = saved.json()["currentVersionId"]
            second = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Discuss Stage two.",
                    "baseVersionId": stage_two,
                    "idempotencyKey": "stage_two_chat",
                },
            )

        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(mocked.call_count, 2)
        stage_two_conversation = mocked.call_args_list[1].args[0]
        self.assertEqual(
            stage_two_conversation,
            [{"role": "user", "content": "Discuss Stage two."}],
        )
        turns = second.json()["turns"]
        self.assertEqual(
            [turn["role"] for turn in turns if turn["versionId"] == stage_one],
            ["user", "assistant"],
        )
        self.assertEqual(
            [turn["role"] for turn in turns if turn["versionId"] == stage_two],
            ["user", "assistant"],
        )

    def test_llm_context_includes_latest_stage_guidance_for_card_deduplication(self):
        version_id = self.read_session()["currentVersionId"]
        first_execution = LLMExecutionResult(
            "The water can become part of the route.",
            1,
            "first-guidance",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": "I think you may want the water to shape the route.",
                "intentConfidence": "medium",
                "followUpQuestion": (
                    "I prefer the water-side hesitation because it gives the route choice weight."
                ),
                "proposalOffer": {
                    "summary": "Link the lower target to the water edge",
                    "rationale": "Make the first push depend on reading the water route",
                },
                "uiCues": [
                    {
                        "type": "warning",
                        "text": "The box may lose its escape route beside the water.",
                    },
                    {
                        "type": "manual_edit",
                        "text": "Try the water edge and observe the first route choice.",
                    },
                ],
                "evidenceSignature": "saved-evidence",
            },
        )
        second_execution = LLMExecutionResult(
            "That direction is now established.",
            1,
            "second-guidance",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
        )

        with patch.object(
            backend,
            "generate_chat_reply",
            side_effect=[first_execution, second_execution],
        ) as mocked:
            for content, request_key in (
                ("The water feels decorative.", "guidance_context_1"),
                ("Make it part of the route.", "guidance_context_2"),
            ):
                response = self.client.post(
                    f"/api/sessions/{self.session_id}/messages",
                    json={
                        "content": content,
                        "baseVersionId": version_id,
                        "idempotencyKey": request_key,
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)

        recent = mocked.call_args_list[1].kwargs["stage_context"]["recentGuidance"]
        self.assertIn("water-side hesitation", recent["discussionFocus"])
        self.assertEqual(
            recent["intentHypothesis"],
            "I think you may want the water to shape the route.",
        )
        self.assertEqual(
            recent["proposalOffer"]["summary"],
            "Link the lower target to the water edge",
        )
        self.assertEqual(
            recent["uiCues"]["warning"]["evidenceSignature"],
            "saved-evidence",
        )
        self.assertIn("manual_edit", recent["uiCues"])
        self.assertTrue(
            mocked.call_args_list[1].kwargs["stage_context"]["guidanceEvidenceSignature"]
        )

    def test_generic_continuation_does_not_replace_substantive_design_direction(self):
        turns = [
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "我再展开一点。"},
            {"role": "user", "content": "我希望水域参与箱子的推进路线。"},
        ]

        self.assertEqual(
            backend._latest_substantive_design_direction(turns),
            "我希望水域参与箱子的推进路线。",
        )

    def test_failed_message_retries_with_one_user_turn_and_one_assistant_turn(self):
        version_id = self.read_session()["currentVersionId"]
        request_payload = {
            "content": "Create a reviewable map proposal.",
            "baseVersionId": version_id,
            "idempotencyKey": "retry_message_001",
        }
        timeout = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "DeepSeek did not respond before the timeout.",
            "timeout-request",
            True,
            1,
            504,
        )
        execution = LLMExecutionResult(
            "Here is a direction to review.",
            1,
            "retry-request",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
        )

        with patch.object(
            backend,
            "generate_chat_reply",
            side_effect=[timeout, execution],
        ) as mocked:
            failed = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=request_payload,
            )
            retried = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=request_payload,
            )
            repeated_success = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=request_payload,
            )

        self.assertEqual(failed.status_code, 504)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(repeated_success.status_code, 200)
        self.assertEqual(mocked.call_count, 2)
        matching_turns = [
            turn for turn in retried.json()["turns"]
            if turn["requestId"] == request_payload["idempotencyKey"]
        ]
        self.assertEqual([turn["role"] for turn in matching_turns], ["user", "assistant"])
        self.assertEqual(len(retried.json()["versions"]), 1)
        self.assertEqual(retried.json()["proposals"], [])

    def test_exhausted_deterministic_search_returns_guidance_without_relaxation_offer(self):
        version_id = self.read_session()["currentVersionId"]
        request_payload = {
            "content": "Please revise the map by moving the target one cell to the right.",
            "baseVersionId": version_id,
            "idempotencyKey": "strict_search_failure_message_001",
        }
        invalid = LLMServiceError(
            "PROPOSAL_SEARCH_EXHAUSTED",
            "Deterministic search found no solvable map.",
            "invalid-proposal-request",
            False,
            1,
            502,
        )
        invalid.revision_plan = {
            "strategies": [{"effect": "relocate_target"}],
        }
        invalid.proposal_diagnostics = {
            "constructedCandidates": 64,
            "validCandidates": 0,
            "failureReasons": {"UNSOLVABLE_LEVEL": 64},
        }

        with patch.object(
            backend,
            "generate_chat_reply",
            side_effect=invalid,
        ) as mocked:
            failed_generation = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=request_payload,
            )

            self.assertEqual(failed_generation.status_code, 200, failed_generation.text)
            matching = [
                turn for turn in failed_generation.json()["turns"]
                if turn["requestId"] == request_payload["idempotencyKey"]
            ]
            self.assertEqual([turn["role"] for turn in matching], ["user", "assistant"])
            warning_turn = matching[-1]
            self.assertEqual(
                [cue["type"] for cue in warning_turn["guidance"]["uiCues"]],
                ["warning"],
            )
            self.assertIsNone(warning_turn["guidance"]["proposalOffer"])
            self.assertIsNone(warning_turn["guidance"]["relaxationOffer"])
            self.assertIn("adjust the map yourself", warning_turn["content"].lower())
            self.assertIn("discuss", warning_turn["content"].lower())
            self.assertEqual(failed_generation.json()["proposals"], [])

            repeated = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=request_payload,
            )

        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["proposals"], [])
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(repeated.json()["turns"], failed_generation.json()["turns"])
        with repository.connect() as database:
            event_types = [
                row[0]
                for row in database.execute(
                    "SELECT event_type FROM audit_events WHERE session_id = ?",
                    (self.session_id,),
                ).fetchall()
            ]
        self.assertEqual(event_types.count("proposal_search_failed"), 1)
        self.assertEqual(event_types.count("proposal_relaxation_offered"), 0)

    def test_transport_failures_never_trigger_relaxation_offer(self):
        version_id = self.read_session()["currentVersionId"]
        request_payload = {
            "content": "Please revise the map by moving the target one cell to the right.",
            "baseVersionId": version_id,
            "idempotencyKey": "transport_failure_message_001",
        }
        timeout = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "DeepSeek did not respond before the timeout.",
            "timeout-proposal-request",
            True,
            3,
            504,
        )
        with patch.object(
            backend,
            "generate_chat_reply",
            side_effect=timeout,
        ):
            response = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=request_payload,
            )

        self.assertEqual(response.status_code, 504)
        matching = [
            turn for turn in self.read_session()["turns"]
            if turn["requestId"] == request_payload["idempotencyKey"]
        ]
        self.assertEqual([turn["role"] for turn in matching], ["user"])
        with repository.connect() as database:
            count = database.execute(
                """
                SELECT COUNT(*) FROM audit_events
                WHERE session_id = ? AND event_type LIKE 'proposal_%'
                """,
                (self.session_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_retry_after_model_failure_saves_a_self_edit_guidance_reply(self):
        version_id = self.read_session()["currentVersionId"]
        request_payload = {
            "content": "I think the route is too easy.",
            "baseVersionId": version_id,
            "idempotencyKey": "empty_message_001",
        }
        empty_response = LLMServiceError(
            "MODEL_EMPTY_RESPONSE",
            "The LLM returned an empty response.",
            request_payload["idempotencyKey"],
            True,
            2,
            502,
        )

        with patch.object(
            backend,
            "generate_chat_reply",
            side_effect=empty_response,
        ) as mocked:
            first = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=request_payload,
            )
            second = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=request_payload,
            )

        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.json()["code"], "MODEL_EMPTY_RESPONSE")
        self.assertTrue(first.json()["retryable"])
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(mocked.call_count, 2)
        stored = self.read_session()
        matching_turns = [
            turn for turn in stored["turns"]
            if turn["requestId"] == request_payload["idempotencyKey"]
        ]
        self.assertEqual([turn["role"] for turn in matching_turns], ["user", "assistant"])
        self.assertIn("tried generating the revision again", matching_turns[1]["content"])
        self.assertIn("make the change yourself", matching_turns[1]["content"])
        with repository.connect() as database:
            retry_event = database.execute(
                """
                SELECT payload_json FROM audit_events
                WHERE session_id = ? AND event_type = 'message_retry_exhausted'
                """,
                (self.session_id,),
            ).fetchone()
        self.assertIsNotNone(retry_event)

    def test_retry_exhausted_reply_uses_the_requested_chinese_copy(self):
        failure = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "Timed out.",
            "retry-copy-test",
            True,
            2,
            504,
        )

        execution = backend._retry_exhausted_execution("zh-CN", failure)

        self.assertEqual(
            execution.assistant_message,
            "我注意到你尝试了一次重新生成，非常抱歉，由于我的能力不足，我可能无法帮助你进行这个修改，"
            "请你根据我们商量好的方案进行自主修改。",
        )

    def test_low_quality_model_failure_saves_no_assistant_turn(self):
        version_id = self.read_session()["currentVersionId"]
        request_payload = {
            "content": "展开讲讲。",
            "baseVersionId": version_id,
            "idempotencyKey": "low_quality_message_001",
        }
        error = LLMServiceError(
            "MODEL_LOW_QUALITY_RESPONSE",
            "The LLM returned only a low-information question.",
            request_payload["idempotencyKey"],
            True,
            2,
            502,
        )

        with patch.object(backend, "generate_chat_reply", side_effect=error):
            response = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=request_payload,
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "MODEL_LOW_QUALITY_RESPONSE")
        matching_turns = [
            turn for turn in self.read_session()["turns"]
            if turn["requestId"] == request_payload["idempotencyKey"]
        ]
        self.assertEqual([turn["role"] for turn in matching_turns], ["user"])

    def test_message_idempotency_key_rejects_changed_content_or_stage(self):
        version_id = self.read_session()["currentVersionId"]
        timeout = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "DeepSeek did not respond before the timeout.",
            "timeout-request",
            True,
            1,
            504,
        )
        original = {
            "content": "Create a reviewable map proposal.",
            "baseVersionId": version_id,
            "idempotencyKey": "conflict_message_001",
        }

        with patch.object(backend, "generate_chat_reply", side_effect=timeout) as mocked:
            failed = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json=original,
            )
            changed_content = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={**original, "content": "Create a different proposal."},
            )

        self.assertEqual(failed.status_code, 504)
        self.assertEqual(changed_content.status_code, 409)
        self.assertEqual(changed_content.json()["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(mocked.call_count, 1)

        saved = self.client.post(
            f"/api/sessions/{self.session_id}/versions",
            json={
                "rows": EDITED_ROWS,
                "baseVersionId": version_id,
                "idempotencyKey": "manual_after_timeout",
                "summary": "Create a second Stage.",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        changed_stage = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={**original, "baseVersionId": saved.json()["currentVersionId"]},
        )
        self.assertEqual(changed_stage.status_code, 409)
        self.assertEqual(changed_stage.json()["code"], "IDEMPOTENCY_CONFLICT")

    def test_play_ticket_is_single_use_and_metrics_do_not_create_stage(self):
        version_id = self.read_session()["currentVersionId"]
        issued = self.client.post(
            f"/api/sessions/{self.session_id}/versions/{version_id}/play-attempts",
            json={"idempotencyKey": "play_001"},
        )
        self.assertEqual(issued.status_code, 200, issued.text)
        play_url = urlparse(issued.json()["playUrl"])
        query = parse_qs(play_url.query)
        attempt_id = query["cocreationAttempt"][0]
        ticket = query["cocreationPlay"][0]

        bootstrap = self.client.post(
            f"/api/play-attempts/{attempt_id}/bootstrap",
            json={"ticket": ticket},
        )
        self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
        play_data = bootstrap.json()
        self.assertEqual(play_data["rows"], SAMPLE_ROWS)
        self.assertEqual(play_data["initialDraftMethod"], "partial_completion")

        repeated = self.client.post(
            f"/api/play-attempts/{attempt_id}/bootstrap",
            json={"ticket": ticket},
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(repeated.json()["code"], "PLAY_TICKET_USED")

        metrics = {
            "attemptToken": play_data["attemptToken"],
            "durationSeconds": 12.5,
            "moveCount": 8,
            "pushCount": 2,
            "restartCount": 1,
            "minimumMoves": 4,
            "minimumPushes": 2,
        }
        started = self.client.post(
            f"/api/play-attempts/{attempt_id}/start",
            json={**metrics, "durationSeconds": 0, "moveCount": 0, "pushCount": 0},
        )
        finished = self.client.post(
            f"/api/play-attempts/{attempt_id}/complete",
            json=metrics,
        )
        retried = self.client.post(
            f"/api/play-attempts/{attempt_id}/complete",
            json=metrics,
        )

        self.assertEqual(started.json()["status"], "started")
        self.assertEqual(finished.json()["status"], "completed")
        self.assertEqual(retried.json()["status"], "completed")
        session = self.read_session()
        self.assertEqual(len(session["versions"]), 1)
        self.assertEqual(session["versions"][0]["playAttempts"][0]["moveCount"], 8)

    def test_llm_proposal_only_creates_stage_after_designer_acceptance(self):
        version_id = self.read_session()["currentVersionId"]
        execution = LLMExecutionResult(
            "I prepared a focused map proposal for your review.",
            1,
            "proposal-request",
            assessment={
                "solutionSummary": "One-box route",
                "difficultyOpinion": "Likely easy",
                "features": ["Compact"],
                "suggestions": ["Review the player start"],
                "satisfactionQuestion": "Does this match your intention?",
            },
            proposed_rows=EDITED_ROWS,
            modification_summary="Moved the player start left.",
            model="mock-model",
            guidance={
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
            revision_plan={
                "strategies": [{
                    "effect": "relocate_start",
                    "operators": ["move_player"],
                }],
            },
            proposal_diagnostics={
                "constructedCandidates": 12,
                "validCandidates": 4,
                "selectedOperators": ["move_player"],
                "selectedStrategyIndex": 1,
                "changedCellCount": 2,
            },
            revision_contract=PLAYER_MOVE_CONTRACT,
            revision_operations=PLAYER_MOVE_OPERATIONS,
        )

        offer_execution = LLMExecutionResult(
            "I see a focused revision direction.",
            1,
            "proposal-offer-request",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Move the player start left",
                    "rationale": "This changes the first route choice while preserving the box-target relationship.",
                },
                "uiCues": [],
            },
        )

        with patch.object(backend, "generate_chat_reply", side_effect=[offer_execution, execution]):
            offered = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Move the player one cell left.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "proposal_offer_001",
                },
            )
            source_turn = offered.json()["turns"][-1]
            proposed = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please generate this plan.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "proposal_message_001",
                    "action": "execute_revision",
                    "sourceTurnId": source_turn["turnId"],
                },
            )

        self.assertEqual(proposed.status_code, 200, proposed.text)
        proposed_session = proposed.json()
        self.assertEqual(len(proposed_session["versions"]), 1)
        self.assertEqual(proposed_session["proposals"][0]["status"], "pending")
        self.assertEqual(
            proposed_session["proposals"][0]["summary"],
            "Verified tile changes (2 total): row 5, column 4: floor → player; "
            "row 5, column 5: player → floor.",
        )
        proposal_turn_before_acceptance = next(
            turn
            for turn in proposed_session["turns"]
            if turn["turnId"] == proposed_session["proposals"][0]["assistantTurnId"]
        )
        self.assertIn(
            "checked its real before/after tile changes",
            proposal_turn_before_acceptance["content"],
        )
        self.assertNotIn(
            "Moved the player start left",
            proposal_turn_before_acceptance["content"],
        )
        self.assertEqual(
            [cue["type"] for cue in proposal_turn_before_acceptance["guidance"]["uiCues"]],
            ["manual_edit"],
        )
        with repository.connect() as database:
            search_event = database.execute(
                """
                SELECT payload_json FROM audit_events
                WHERE session_id = ? AND event_type = 'proposal_search_completed'
                """,
                (self.session_id,),
            ).fetchone()
        self.assertIsNotNone(search_event)
        search_payload = repository.load_json(search_event["payload_json"])
        self.assertEqual(search_payload["search"]["validCandidates"], 4)
        self.assertEqual(
            search_payload["revisionPlan"]["strategies"][0]["effect"],
            "relocate_start",
        )
        repeated = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={
                "content": "Please generate this plan.",
                "baseVersionId": version_id,
                "idempotencyKey": "proposal_message_001",
                "action": "execute_revision",
                "sourceTurnId": source_turn["turnId"],
            },
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(len(repeated.json()["proposals"]), 1)
        with repository.connect() as database:
            search_event_count = database.execute(
                """
                SELECT COUNT(*) AS count FROM audit_events
                WHERE session_id = ? AND event_type = 'proposal_search_completed'
                """,
                (self.session_id,),
            ).fetchone()["count"]
        self.assertEqual(search_event_count, 1)
        proposal_id = proposed_session["proposals"][0]["proposalId"]

        accepted = self.client.post(
            f"/api/sessions/{self.session_id}/proposals/{proposal_id}/decision",
            json={
                "decision": "accept",
                "baseVersionId": version_id,
                "idempotencyKey": "accept_proposal_001",
                "reason": "",
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(len(accepted.json()["versions"]), 2)
        self.assertEqual(accepted.json()["versions"][1]["rows"], EDITED_ROWS)
        self.assertEqual(accepted.json()["versions"][1]["source"], "llm_accepted")
        stage_two = accepted.json()["versions"][1]
        proposal_turn = next(
            turn
            for turn in accepted.json()["turns"]
            if turn["turnId"] == proposed_session["proposals"][0]["assistantTurnId"]
        )
        self.assertEqual(stage_two["openingTurnId"], proposal_turn["turnId"])
        self.assertEqual(stage_two["openingProposalId"], proposal_id)

        with patch.object(backend, "generate_stage_assessment") as assessment_mock:
            opening = self.client.post(
                f"/api/sessions/{self.session_id}/versions/{stage_two['versionId']}/assessments",
                json={"idempotencyKey": "accepted_opening_001"},
            )

        self.assertEqual(opening.status_code, 200, opening.text)
        assessment_mock.assert_not_called()
        self.assertEqual(
            [
                item
                for item in opening.json()["assessments"]
                if item["versionId"] == stage_two["versionId"]
            ],
            [],
        )

        legacy_assessment = LLMExecutionResult(
            "A superseded first assessment.",
            1,
            "legacy-accepted-assessment",
            assessment={
                "solutionSummary": "Legacy",
                "difficultyOpinion": "In my view, moderate.",
                "features": ["Legacy"],
                "suggestions": ["Legacy"],
                "satisfactionQuestion": "Legacy?",
            },
            model="mock-model",
            guidance={
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "Legacy?",
                "proposalOffer": None,
            },
        )
        with repository.connect(immediate=True) as database:
            session_row = repository.get_session(database, self.session_id)
            legacy_turn_id = backend.insert_turn(
                database,
                session_row,
                "assistant",
                legacy_assessment.assistant_message,
                stage_two["versionId"],
                legacy_assessment.request_id,
                legacy_assessment,
            )
            database.execute(
                """
                INSERT INTO llm_assessments(
                    id, session_id, version_id, assistant_turn_id,
                    payload_json, prompt_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy_accepted_assessment",
                    self.session_id,
                    stage_two["versionId"],
                    legacy_turn_id,
                    repository.dump_json(legacy_assessment.assessment),
                    "legacy-prompt",
                    backend.utc_now(),
                ),
            )

        follow_up_execution = LLMExecutionResult(
            "Let's continue from the accepted proposal.",
            1,
            "accepted-follow-up",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
        )
        with patch.object(
            backend,
            "generate_chat_reply",
            return_value=follow_up_execution,
        ) as chat_mock:
            continued = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "What should we inspect next?",
                    "baseVersionId": stage_two["versionId"],
                    "idempotencyKey": "accepted_follow_up_001",
                },
            )

        self.assertEqual(continued.status_code, 200, continued.text)
        self.assertEqual(
            chat_mock.call_args.args[0],
            [
                {"role": "assistant", "content": proposal_turn["content"]},
                {"role": "user", "content": "What should we inspect next?"},
            ],
        )
        self.assertNotIn(
            legacy_assessment.assistant_message,
            [turn["content"] for turn in chat_mock.call_args.args[0]],
        )
        self.assertEqual(
            chat_mock.call_args.kwargs["stage_context"]["openingTurnId"],
            proposal_turn["turnId"],
        )

    def test_intent_hypothesis_and_validator_handoff_keep_player_control(self):
        version_id = self.read_session()["currentVersionId"]
        execution = LLMExecutionResult(
            "I prepared a focused map proposal for your review.",
            1,
            "hypothesis-proposal-request",
            proposed_rows=EDITED_ROWS,
            modification_summary="Move the player start left.",
            model="mock-model",
            guidance={
                "move": "deliver_revision",
                "intentHypothesis": "I think you want the first route choice to feel more deliberate.",
                "intentConfidence": "medium",
                "followUpQuestion": None,
                "proposalOffer": None,
            },
            revision_plan={
                "strategies": [{"effect": "relocate_start", "operators": ["move_player"]}]
            },
            revision_contract=PLAYER_MOVE_CONTRACT,
            revision_operations=PLAYER_MOVE_OPERATIONS,
            proposal_diagnostics={
                "selectedStrategyIndex": 1,
                "changedCellCount": 2,
            },
        )

        offer_execution = LLMExecutionResult(
            "I see a focused revision direction.",
            1,
            "hypothesis-offer-request",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Move the player start left",
                    "rationale": "This changes the first route choice while preserving the box-target relationship.",
                },
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", side_effect=[offer_execution, execution]):
            offered = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Move the player one cell left.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "hypothesis_offer_001",
                },
            )
            source_turn = offered.json()["turns"][-1]
            proposed = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please generate this plan.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "hypothesis_proposal_001",
                    "action": "execute_revision",
                    "sourceTurnId": source_turn["turnId"],
                },
            )

        self.assertEqual(proposed.status_code, 200, proposed.text)
        proposal_id = proposed.json()["proposals"][0]["proposalId"]
        rejected = self.client.post(
            f"/api/sessions/{self.session_id}/proposals/{proposal_id}/decision",
            json={
                "decision": "reject",
                "baseVersionId": version_id,
                "idempotencyKey": "hypothesis_reject_001",
                "reason": "This does not reflect what I meant.",
            },
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)

        with repository.connect() as database:
            chat_handoff = database.execute(
                """
                SELECT payload_json FROM audit_events
                WHERE session_id = ? AND event_type = 'agent_handoff'
                  AND json_extract(payload_json, '$.artifactType') = 'revision_plan'
                """,
                (self.session_id,),
            ).fetchone()
            handoff = database.execute(
                """
                SELECT payload_json FROM audit_events
                WHERE session_id = ? AND event_type = 'agent_handoff'
                  AND json_extract(payload_json, '$.artifactType') = 'revision_operations'
                """,
                (self.session_id,),
            ).fetchone()
            hypotheses = database.execute(
                """
                SELECT payload_json FROM audit_events
                WHERE session_id = ? AND event_type = 'intent_hypothesis'
                ORDER BY id
                """,
                (self.session_id,),
            ).fetchall()
            current_version = repository.get_current_version(
                database,
                repository.get_session(database, self.session_id),
            )
            llm_context = backend.build_llm_context(
                database,
                self.session_id,
                current_version,
            )

        self.assertIsNotNone(handoff)
        self.assertIsNotNone(chat_handoff)
        chat_handoff_payload = repository.load_json(chat_handoff["payload_json"])
        self.assertEqual(chat_handoff_payload["fromAgent"], "co_creation_chat")
        self.assertEqual(chat_handoff_payload["toAgent"], "co_creation_revision")
        self.assertEqual(
            chat_handoff_payload["artifact"]["executionContract"]["schemaVersion"],
            1,
        )
        handoff_payload = repository.load_json(handoff["payload_json"])
        self.assertEqual(handoff_payload["fromAgent"], "co_creation_revision")
        self.assertEqual(handoff_payload["toAgent"], "deterministic_validator")
        self.assertEqual(handoff_payload["status"], "confirmed")
        self.assertEqual(handoff_payload["artifact"]["proposalId"], proposal_id)
        self.assertEqual(
            handoff_payload["evidence"][0]["validation"]["solvable"],
            True,
        )

        self.assertEqual(len(hypotheses), 2)
        proposed_payload = repository.load_json(hypotheses[0]["payload_json"])
        rejected_payload = repository.load_json(hypotheses[1]["payload_json"])
        self.assertEqual(proposed_payload["status"], "proposed")
        self.assertEqual(proposed_payload["artifact"]["confidence"], "medium")
        self.assertEqual(
            proposed_payload["artifact"]["hypothesis"],
            "I think you want the first route choice to feel more deliberate.",
        )
        self.assertEqual(rejected_payload["status"], "rejected")
        self.assertEqual(rejected_payload["proposalId"], proposal_id)
        self.assertIsNone(
            llm_context["stageContext"]["recentGuidance"]["intentHypothesis"]
        )

    def test_unchanged_llm_proposal_is_rejected_before_it_can_be_saved(self):
        version_id = self.read_session()["currentVersionId"]
        unchanged_execution = LLMExecutionResult(
            "I drafted that revision.",
            1,
            "unchanged-proposal-request",
            proposed_rows=SAMPLE_ROWS,
            modification_summary="Claimed changes that are not present.",
            model="mock-model",
            guidance={
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
        )

        offer_execution = LLMExecutionResult(
            "I have a concrete plan to review.",
            1,
            "unchanged-offer-request",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Review the current player start",
                    "rationale": "The plan should make a small, testable change to the opening route.",
                },
                "uiCues": [],
            },
        )
        with patch.object(
            backend,
            "generate_chat_reply",
            side_effect=[offer_execution, unchanged_execution],
        ):
            offered = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please propose a concrete revision.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "unchanged_offer_001",
                },
            )
            source_turn = offered.json()["turns"][-1]
            response = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please make that concrete revision.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "unchanged_proposal_message_001",
                    "action": "execute_revision",
                    "sourceTurnId": source_turn["turnId"],
                },
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "UNCHANGED_PROPOSAL")
        session = self.read_session()
        self.assertEqual(len(session["versions"]), 1)
        self.assertEqual(session["proposals"], [])
        matching_turns = [
            turn
            for turn in session["turns"]
            if turn["requestId"] == "unchanged_proposal_message_001"
        ]
        self.assertEqual([turn["role"] for turn in matching_turns], ["user"])

    def test_unchanged_pending_proposal_cannot_be_accepted(self):
        version_id = self.read_session()["currentVersionId"]
        execution = LLMExecutionResult(
            "Here is a real revision for review.",
            1,
            "proposal-before-tamper",
            proposed_rows=EDITED_ROWS,
            modification_summary="Moved the player start left.",
            model="mock-model",
            guidance={
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
        )

        offer_execution = LLMExecutionResult(
            "I have a concrete plan to review.",
            1,
            "tamper-offer-request",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Move the player start left",
                    "rationale": "This changes the opening route while keeping the puzzle solvable.",
                },
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", side_effect=[offer_execution, execution]):
            offered = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please propose a concrete revision.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "tamper_offer_001",
                },
            )
            source_turn = offered.json()["turns"][-1]
            proposed = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please revise the map.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "proposal_to_tamper_001",
                    "action": "execute_revision",
                    "sourceTurnId": source_turn["turnId"],
                },
            )

        self.assertEqual(proposed.status_code, 200, proposed.text)
        proposal_id = proposed.json()["proposals"][0]["proposalId"]

        with repository.connect(immediate=True) as database:
            database.execute(
                """
                UPDATE change_proposals
                SET proposed_rows_json = ?, diff_json = '[]'
                WHERE id = ?
                """,
                (repository.dump_json(SAMPLE_ROWS), proposal_id),
            )

        accepted = self.client.post(
            f"/api/sessions/{self.session_id}/proposals/{proposal_id}/decision",
            json={
                "decision": "accept",
                "baseVersionId": version_id,
                "idempotencyKey": "reject_unchanged_accept_001",
                "reason": "",
            },
        )

        self.assertEqual(accepted.status_code, 400, accepted.text)
        self.assertEqual(accepted.json()["code"], "UNCHANGED_PROPOSAL")
        session = self.read_session()
        self.assertEqual(len(session["versions"]), 1)
        self.assertEqual(session["proposals"][0]["status"], "pending")

    def test_finalize_hides_rows_until_intention_is_submitted(self):
        version_id = self.read_session()["currentVersionId"]
        finalized = self.client.post(
            f"/api/sessions/{self.session_id}/finalize",
            json={
                "baseVersionId": version_id,
                "idempotencyKey": "finalize_001",
            },
        )
        self.assertEqual(finalized.status_code, 200, finalized.text)
        self.assertEqual(finalized.json()["status"], "awaiting_intention")

        integration = self.client.get(
            f"/api/integrations/sessions/{self.session_id}",
            headers={"Authorization": f"Bearer {self.integration_token}"},
        )
        self.assertIsNone(integration.json()["finalRows"])

        intention = self.client.post(
            f"/api/sessions/{self.session_id}/intention",
            json={
                "content": "I wanted a compact introductory puzzle.",
                "idempotencyKey": "intention_001",
            },
        )
        self.assertEqual(intention.status_code, 200, intention.text)
        self.assertEqual(intention.json()["status"], "completed")

        integration = self.client.get(
            f"/api/integrations/sessions/{self.session_id}",
            headers={"Authorization": f"Bearer {self.integration_token}"},
        )
        self.assertEqual(integration.json()["finalRows"], SAMPLE_ROWS)
        self.assertEqual(
            integration.json()["designerIntention"],
            "I wanted a compact introductory puzzle.",
        )

    def test_manual_stage_and_final_are_synchronized_as_compact_flow_events(self):
        initial_version_id = self.read_session()["currentVersionId"]
        with patch.object(backend, "synchronize_cocreation_event_with_online_match") as sync:
            saved = self.client.post(
                f"/api/sessions/{self.session_id}/versions",
                json={
                    "rows": EDITED_ROWS,
                    "baseVersionId": initial_version_id,
                    "idempotencyKey": "sync_manual_stage_001",
                    "summary": "Move the player left.",
                },
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            version_id = saved.json()["currentVersionId"]
            finalized = self.client.post(
                f"/api/sessions/{self.session_id}/finalize",
                json={
                    "baseVersionId": version_id,
                    "idempotencyKey": "sync_final_stage_001",
                },
            )

        self.assertEqual(finalized.status_code, 200, finalized.text)
        events = [call.args[1] for call in sync.call_args_list]
        self.assertEqual(events[0]["eventType"], "stage")
        self.assertEqual(events[0]["source"], "manual")
        self.assertEqual(events[0]["rows"], EDITED_ROWS)
        self.assertEqual(events[1]["eventType"], "final")
        self.assertEqual(events[1]["versionId"], version_id)
        self.assertIsInstance(events[1]["coCreationDurationSeconds"], int)
        self.assertGreaterEqual(events[1]["coCreationDurationSeconds"], 0)
        self.assertLessEqual(events[1]["coCreationDurationSeconds"], 600)

    def test_cocreation_duration_uses_remaining_deadline_and_caps_at_timeout(self):
        session = {
            "deadline_at": "2026-08-26T10:10:00Z",
            "finalized_at": "2026-08-26T10:06:37Z",
        }
        self.assertEqual(
            backend.calculate_cocreation_duration_seconds(session),
            397,
        )

        timed_out = {
            "deadline_at": "2026-08-26T10:10:00Z",
            "finalized_at": "2026-08-26T10:18:00Z",
        }
        self.assertEqual(
            backend.calculate_cocreation_duration_seconds(timed_out),
            600,
        )

    def test_message_sync_uses_a_stable_message_event_id(self):
        version_id = self.read_session()["currentVersionId"]
        self.client.post(
            f"/api/sessions/{self.session_id}/finalize",
            json={"baseVersionId": version_id, "idempotencyKey": "message_final_001"},
        )
        with patch.object(backend, "synchronize_final_intention_with_online_match") as sync:
            response = self.client.post(
                f"/api/sessions/{self.session_id}/intention",
                json={
                    "content": "Notice the route before moving the box.",
                    "idempotencyKey": "message_send_001",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sync.call_args.args[3], "message:" + self.session_id + ":message_send_001")
        self.assertEqual(sync.call_args.args[4], self.session_id)

    def test_expired_deadline_locks_edits_and_finalizes_the_current_draft(self):
        with repository.connect(immediate=True) as database:
            database.execute(
                "UPDATE design_sessions SET deadline_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", self.session_id),
            )

        session = self.read_session()
        self.assertTrue(session["deadlineExpired"])
        self.assertEqual(session["remainingSeconds"], 0)

        locked = self.client.patch(
            f"/api/sessions/{self.session_id}/language",
            json={"language": "zh-CN"},
        )
        self.assertEqual(locked.status_code, 409)
        self.assertEqual(locked.json()["code"], "SESSION_DEADLINE_EXPIRED")

        finalized = self.client.post(
            f"/api/sessions/{self.session_id}/finalize",
            json={
                "baseVersionId": session["currentVersionId"],
                "idempotencyKey": "deadline_finalize_001",
                "rows": EDITED_ROWS,
            },
        )
        self.assertEqual(finalized.status_code, 200, finalized.text)
        self.assertEqual(finalized.json()["status"], "awaiting_intention")
        self.assertEqual(finalized.json()["finalVersionId"], finalized.json()["currentVersionId"])
        self.assertEqual(finalized.json()["versions"][-1]["source"], "human_edit")

    def test_language_switch_is_persisted(self):
        response = self.client.patch(
            f"/api/sessions/{self.session_id}/language",
            json={"language": "zh-CN"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["language"], "zh-CN")

    def test_assistant_translation_is_cached_without_changing_original_turn(self):
        version_id = self.read_session()["currentVersionId"]
        opening = LLMExecutionResult(
            "The central route may feel direct.\n\nWhat should stand out first?",
            1,
            "translation-opening",
            assessment={
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "This looks approachable to me.",
                "features": ["Central route"],
                "suggestions": ["Discuss the focal point"],
                "satisfactionQuestion": "What should stand out first?",
            },
            model="mock-model",
            guidance={
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "What should stand out first?",
                "proposalOffer": None,
                "uiCues": [
                    {"type": "warning", "text": "The box may be trapped by the water."},
                    {"type": "manual_edit", "text": "Try a small route experiment."},
                ],
            },
        )

        with patch.object(backend, "generate_stage_assessment", return_value=opening):
            assessed = self.client.post(
                f"/api/sessions/{self.session_id}/versions/{version_id}/assessments",
                json={"idempotencyKey": "translation_opening_001"},
            )

        turn = assessed.json()["turns"][-1]
        translated = TranslationExecutionResult(
            translations=[
                {
                    "turnId": turn["turnId"],
                    "body": "中央路线可能显得较为直接。",
                    "followUpQuestion": "你希望玩家首先注意到什么？",
                    "intentHypothesis": None,
                    "proposalOfferSummary": None,
                    "proposalOfferRationale": None,
                    "uiCueTexts": ["箱子可能被水域卡住。", "尝试一次小范围路线实验。"],
                    "proposalSummary": None,
                }
            ],
            attempts_used=1,
            request_id="translation-request",
            model="translation-model",
            latency_ms=25,
        )

        with patch.object(backend, "translate_turns", return_value=translated) as mocked:
            response = self.client.post(
                f"/api/sessions/{self.session_id}/translations/zh-CN",
                json={"turnIds": [turn["turnId"]]},
            )

        self.assertEqual(response.status_code, 200, response.text)
        translated_turn = response.json()["turns"][-1]
        self.assertEqual(translated_turn["content"], turn["content"])
        self.assertEqual(translated_turn["language"], "en")
        self.assertEqual(
            translated_turn["translations"]["zh-CN"]["body"],
            "中央路线可能显得较为直接。",
        )
        self.assertEqual(
            translated_turn["translations"]["zh-CN"]["guidance"]["followUpQuestion"],
            "你希望玩家首先注意到什么？",
        )
        self.assertEqual(
            [
                cue["type"]
                for cue in translated_turn["translations"]["zh-CN"]["guidance"]["uiCues"]
            ],
            ["warning", "manual_edit"],
        )
        self.assertEqual(
            translated_turn["translations"]["zh-CN"]["guidance"]["uiCues"][0]["text"],
            "箱子可能被水域卡住。",
        )
        self.assertEqual(mocked.call_count, 1)

        with patch.object(backend, "translate_turns") as cached_mock:
            cached = self.client.post(
                f"/api/sessions/{self.session_id}/translations/zh-CN",
                json={"turnIds": [turn["turnId"]]},
            )

        self.assertEqual(cached.status_code, 200, cached.text)
        cached_mock.assert_not_called()

    def test_translated_coordinate_guidance_keeps_saved_endpoints(self):
        source = {
            "move": "offer_perspective",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": None,
            "disagreement": None,
            "uiCues": [],
            "coordinateLinks": [{
                "text": "from (5,5) to (5,7)",
                "from": {"row": 5, "column": 5},
                "to": {"row": 5, "column": 7},
            }],
        }
        translated = {
            "body": "from (5,5) to (5,7).",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "uiCueTexts": [],
            "proposalSummary": None,
            "coordinateLinkTexts": ["from (5,5) to (5,7)"],
        }

        result = backend._translated_guidance(source, translated)

        self.assertEqual(
            result["coordinateLinks"][0]["from"],
            {"row": 5, "column": 5},
        )
        self.assertEqual(
            result["coordinateLinks"][0]["to"],
            {"row": 5, "column": 7},
        )

    def test_bidirectional_revision_actions_are_visible_and_audited(self):
        version_id = self.read_session()["currentVersionId"]
        offer_execution = LLMExecutionResult(
            "I see one focused direction worth comparing.",
            1,
            "revision-offer-001",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Make the first push create a detour choice",
                    "rationale": "The player should compare the central route with a local detour.",
                },
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=offer_execution):
            offered = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please suggest a concrete route revision.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "bidirectional-offer-001",
                },
            )
        self.assertEqual(offered.status_code, 200, offered.text)
        source_turn = offered.json()["turns"][-1]
        self.assertEqual(source_turn["guidance"]["proposalOffer"]["summary"], "Make the first push create a detour choice")
        self.assertEqual(offered.json()["proposals"], [])

        challenge_execution = LLMExecutionResult(
            "ignored by the deterministic challenge wrapper",
            1,
            "challenge-001",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": source_turn["guidance"]["proposalOffer"],
                "uiCues": [],
            },
            proposed_rows=EDITED_ROWS,
        )
        with patch.object(backend, "generate_chat_reply", return_value=challenge_execution):
            challenged = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Challenge this plan: Make the first push create a detour choice",
                    "baseVersionId": version_id,
                    "idempotencyKey": "bidirectional-challenge-001",
                    "action": "challenge_revision",
                    "sourceTurnId": source_turn["turnId"],
                },
            )
        self.assertEqual(challenged.status_code, 200, challenged.text)
        challenge_turn = challenged.json()["turns"][-1]
        self.assertEqual(challenge_turn["role"], "assistant")
        self.assertIsNone(challenge_turn["guidance"]["proposalOffer"])
        self.assertIsNone(challenge_turn["guidance"]["disagreement"])
        self.assertEqual(challenged.json()["proposals"], [])

        alternative_execution = LLMExecutionResult(
            "I would try a different local treatment.",
            1,
            "alternative-001",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Change the box approach order instead",
                    "rationale": "This changes the decision before the first push without copying the detour treatment.",
                },
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=alternative_execution):
            alternative = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Try another plan: Make the first push create a detour choice",
                    "baseVersionId": version_id,
                    "idempotencyKey": "bidirectional-alternative-001",
                    "action": "alternative_revision",
                    "sourceTurnId": source_turn["turnId"],
                },
            )
        self.assertEqual(alternative.status_code, 200, alternative.text)
        self.assertEqual(
            alternative.json()["turns"][-1]["guidance"]["proposalOffer"]["summary"],
            "Change the box approach order instead",
        )
        self.assertEqual(alternative.json()["proposals"], [])
        latest_source_turn = alternative.json()["turns"][-1]

        execute_execution = LLMExecutionResult(
            "ignored because the proposal wrapper replaces this text",
            1,
            "execute-001",
            model="mock-model",
            proposed_rows=EDITED_ROWS,
            revision_plan={"strategies": [{"effect": "relocate_start"}]},
            revision_contract=PLAYER_MOVE_CONTRACT,
            revision_operations=PLAYER_MOVE_OPERATIONS,
            proposal_diagnostics={"selectedStrategyIndex": 1, "changedCellCount": 2},
            guidance={
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=execute_execution):
            executed = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please generate the first purple plan.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "bidirectional-execute-001",
                    "action": "execute_revision",
                    "sourceTurnId": latest_source_turn["turnId"],
                },
            )
        self.assertEqual(executed.status_code, 200, executed.text)
        self.assertEqual(len(executed.json()["proposals"]), 1)
        self.assertEqual(executed.json()["versions"][0]["versionId"], version_id)

        with repository.connect() as database:
            event_types = [
                row["event_type"]
                for row in database.execute(
                    "SELECT event_type FROM audit_events WHERE session_id = ?",
                    (self.session_id,),
                ).fetchall()
            ]
        self.assertIn("card_action_requested", event_types)
        self.assertIn("proposal_challenge_started", event_types)
        self.assertIn("alternative_revision_requested", event_types)
        self.assertIn("revision_execution_requested", event_types)

    def test_only_latest_revision_offer_can_trigger_card_actions(self):
        version_id = self.read_session()["currentVersionId"]
        older_offer = LLMExecutionResult(
            "An older direction.",
            1,
            "latest-card-old-001",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Use the left route as the main choice",
                    "rationale": "It creates an earlier route decision.",
                },
                "uiCues": [],
            },
        )
        latest_offer = LLMExecutionResult(
            "A newer direction.",
            1,
            "latest-card-new-001",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Use the central route as the main choice",
                    "rationale": "It creates a different route decision.",
                },
                "uiCues": [],
            },
        )
        with repository.connect(immediate=True) as database:
            session = repository.get_session(database, self.session_id)
            older_turn_id = backend.insert_turn(
                database,
                session,
                "assistant",
                older_offer.assistant_message,
                version_id,
                older_offer.request_id,
                older_offer,
            )
            latest_turn_id = backend.insert_turn(
                database,
                session,
                "assistant",
                latest_offer.assistant_message,
                version_id,
                latest_offer.request_id,
                latest_offer,
            )

        before = self.read_session()
        before_turn_count = len(before["turns"])
        before_event_count = None
        with repository.connect() as database:
            before_event_count = database.execute(
                "SELECT COUNT(*) FROM audit_events WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()[0]

        for index, action in enumerate(
            ("execute_revision", "challenge_revision", "alternative_revision"),
            start=1,
        ):
            with patch.object(backend, "generate_chat_reply") as generate_reply:
                rejected = self.client.post(
                    f"/api/sessions/{self.session_id}/messages",
                    json={
                        "content": f"Use the older card ({action}).",
                        "baseVersionId": version_id,
                        "idempotencyKey": f"stale-card-action-{index}",
                        "action": action,
                        "sourceTurnId": older_turn_id,
                    },
                )
            self.assertEqual(rejected.status_code, 409, rejected.text)
            self.assertEqual(rejected.json()["code"], "INVALID_CARD_SOURCE")
            generate_reply.assert_not_called()

        after = self.read_session()
        self.assertEqual(len(after["turns"]), before_turn_count)
        with repository.connect() as database:
            after_event_count = database.execute(
                "SELECT COUNT(*) FROM audit_events WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()[0]
        self.assertEqual(after_event_count, before_event_count)

        with repository.connect() as database:
            source, offer = backend._source_revision_offer(
                database,
                self.session_id,
                version_id,
                latest_turn_id,
            )
        self.assertEqual(source["id"], latest_turn_id)
        self.assertEqual(offer["summary"], "Use the central route as the main choice")

    def test_normal_assistant_turn_does_not_make_latest_revision_offer_stale(self):
        version_id = self.read_session()["currentVersionId"]
        offer = LLMExecutionResult(
            "A concrete direction.",
            1,
            "latest-card-normal-001",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Keep the first route readable",
                    "rationale": "This preserves the intended first decision.",
                },
                "uiCues": [],
            },
        )
        ordinary = LLMExecutionResult(
            "The current route is readable.",
            1,
            "latest-card-normal-reply-001",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", side_effect=[offer, ordinary]):
            offered = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Suggest a route revision.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "latest-card-normal-offer-001",
                },
            )
            source_turn_id = offered.json()["turns"][-1]["turnId"]
            ordinary_reply = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Why is that route readable?",
                    "baseVersionId": version_id,
                    "idempotencyKey": "latest-card-normal-reply-001",
                },
            )
        self.assertEqual(ordinary_reply.status_code, 200, ordinary_reply.text)
        with repository.connect() as database:
            source, offer = backend._source_revision_offer(
                database,
                self.session_id,
                version_id,
                source_turn_id,
            )
        self.assertEqual(source["id"], source_turn_id)
        self.assertEqual(offer["summary"], "Keep the first route readable")

    def test_card_action_rejects_stale_or_non_revision_source_turn(self):
        version_id = self.read_session()["currentVersionId"]
        execution = LLMExecutionResult(
            "A normal reply.",
            1,
            "normal-001",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=execution):
            ordinary = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Tell me what stands out.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "non-card-source-001",
                },
            )
        self.assertEqual(ordinary.status_code, 200, ordinary.text)
        user_turn = ordinary.json()["turns"][0]["turnId"]
        rejected = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={
                "content": "Challenge this plan.",
                "baseVersionId": version_id,
                "idempotencyKey": "invalid-card-source-001",
                "action": "challenge_revision",
                "sourceTurnId": user_turn,
            },
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["code"], "INVALID_CARD_SOURCE")

    def test_challenge_reason_keeps_or_resolves_structured_disagreement(self):
        version_id = self.read_session()["currentVersionId"]
        offer = {
            "summary": "Make the first push create a detour choice",
            "rationale": "The player should compare the central route with a local detour.",
        }
        offer_execution = LLMExecutionResult(
            "I see one focused direction worth comparing.",
            1,
            "challenge-reason-offer-001",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": offer,
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=offer_execution):
            offered = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please suggest a concrete route revision.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "challenge-reason-offer-001",
                },
            )
        source_turn = offered.json()["turns"][-1]

        challenge_execution = LLMExecutionResult(
            "I want to hear your specific concern.",
            1,
            "challenge-reason-start-001",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=challenge_execution):
            challenged = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Challenge this plan.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "challenge-reason-start-001",
                    "action": "challenge_revision",
                    "sourceTurnId": source_turn["turnId"],
                },
            )
        self.assertEqual(challenged.status_code, 200, challenged.text)

        active = {
            "status": "active",
            "subject": "ai_revision",
            "userPosition": "Do not add a detour; the direct opening is intentional.",
            "aiPosition": "The direct opening may hide the first route judgment.",
            "coreDisagreement": "Whether to prioritize a direct opening or a visible detour decision.",
            "nextQuestion": "Which first-push judgment should the player have to make?",
            "resolution": None,
        }
        active_execution = LLMExecutionResult(
            "I still see a disagreement about the opening decision.",
            1,
            "challenge-reason-active-001",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "disagreement": active,
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=active_execution) as active_mock:
            still_active = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "I want the opening to stay direct because the player should commit early.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "challenge-reason-active-001",
                },
            )
        self.assertEqual(still_active.status_code, 200, still_active.text)
        self.assertIsNotNone(active_mock.call_args.kwargs["stage_context"]["challengeContext"])
        self.assertEqual(still_active.json()["turns"][-1]["guidance"]["disagreement"]["status"], "active")

        resolved = dict(active)
        resolved["status"] = "resolved"
        resolved["resolution"] = "user"
        resolved_execution = LLMExecutionResult(
            "I accept that the direct opening is the design priority.",
            1,
            "challenge-reason-resolved-001",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Keep the direct opening and sharpen its first commitment",
                    "rationale": "This follows your reason while making the early commitment easier to read.",
                },
                "disagreement": resolved,
                "uiCues": [],
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=resolved_execution):
            resolved_response = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Keep the direct opening, but make the commitment easier to read.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "challenge-reason-resolved-001",
                },
            )
        self.assertEqual(resolved_response.status_code, 200, resolved_response.text)
        latest = resolved_response.json()["turns"][-1]["guidance"]
        self.assertEqual(latest["disagreement"]["resolution"], "user")
        self.assertIsNotNone(latest["proposalOffer"])

    def test_active_disagreement_cards_keep_warning_and_four_summaries(self):
        disagreement = {
            "status": "active",
            "subject": "human_edit",
            "userPosition": "Keep B2 one cell lower to create a detour.",
            "aiPosition": "That position may close B1's only entry route.",
            "coreDisagreement": "Whether the extra detour is worth risking the entry route.",
            "nextQuestion": "Which play effect should the compromise preserve?",
            "resolution": None,
        }
        cards = backend._displayed_cards({
            "move": "offer_perspective",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": "legacy question",
            "proposalOffer": None,
            "uiCues": [{"type": "warning", "text": "B1 may lose its only entry route."}],
            "disagreement": disagreement,
            "discussionCardMode": "disagreement_only",
        })
        self.assertEqual([card["type"] for card in cards], ["warning", "discussion"])
        self.assertEqual(cards[-1]["disagreement"], disagreement)

    def test_manual_edit_warning_gets_discussion_without_replacing_stage(self):
        version_id = self.read_session()["currentVersionId"]
        saved = self.client.post(
            f"/api/sessions/{self.session_id}/versions",
            json={
                "rows": EDITED_ROWS,
                "baseVersionId": version_id,
                "idempotencyKey": "manual-risk-stage-001",
                "summary": "Move player left",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        stage_id = saved.json()["currentVersionId"]
        execution = LLMExecutionResult(
            "I notice the change may close the box's escape route.",
            1,
            "manual-risk-review-001",
            model="mock-model",
            assessment={
                "solutionSummary": "The map remains solvable.",
                "difficultyOpinion": "In my view, the opening is tighter.",
                "features": ["Changed player start"],
                "suggestions": ["Discuss the first route"],
                "satisfactionQuestion": None,
            },
            guidance={
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [{
                    "type": "warning",
                    "text": "The moved player can close the box's only escape route beside the wall.",
                }],
            },
        )
        with patch.object(backend, "generate_stage_assessment", return_value=execution):
            assessed = self.client.post(
                f"/api/sessions/{self.session_id}/versions/{stage_id}/assessments",
                json={"idempotencyKey": "manual-risk-assessment-001"},
            )
        self.assertEqual(assessed.status_code, 200, assessed.text)
        opening = assessed.json()["turns"][-1]
        self.assertEqual(opening["guidance"]["disagreement"]["status"], "active")
        self.assertEqual(
            [card["type"] for card in backend._displayed_cards(opening["guidance"])],
            ["warning", "discussion"],
        )
        self.assertEqual(assessed.json()["currentVersionId"], stage_id)

    def test_chat_patch_updates_public_progress_without_exposing_internal_patch(self):
        version_id = self.read_session()["currentVersionId"]
        question = "Can the player read the B2 to T1 detour?"
        first_execution = LLMExecutionResult(
            "The new corridor gives us a concrete detour to evaluate.",
            1,
            "progress-question-001",
            model="mock-model",
            guidance={
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
                "designContextPatch": {
                    "openQuestions": [{"question": question, "status": "open"}],
                },
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=first_execution):
            first = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "We still need to test whether the player can read the B2 to T1 detour.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "progress-question-001",
                },
            )
        self.assertEqual(first.status_code, 200, first.text)
        payload = first.json()
        self.assertNotIn("designContextPatch", payload["turns"][-1]["guidance"])
        progress = next(
            item for item in payload["progressContexts"]
            if item["versionId"] == version_id
        )
        self.assertEqual(progress["unresolvedQuestions"][0]["question"], question)

        second_execution = LLMExecutionResult(
            "That question is settled by the latest play observation.",
            1,
            "progress-question-002",
            model="mock-model",
            guidance={
                "move": "reflect_on_play",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
                "designContextPatch": {
                    "openQuestions": [{
                        "question": question,
                        "status": "resolved",
                        "evidenceText": "The detour is clear now",
                    }],
                },
            },
        )
        with patch.object(backend, "generate_chat_reply", return_value=second_execution):
            second = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "The detour is clear now, so we can move on.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "progress-question-002",
                },
            )
        self.assertEqual(second.status_code, 200, second.text)
        progress = next(
            item for item in second.json()["progressContexts"]
            if item["versionId"] == version_id
        )
        self.assertEqual(progress["unresolvedQuestions"], [])


if __name__ == "__main__":
    unittest.main()
