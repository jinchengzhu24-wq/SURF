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

    def test_empty_model_failure_saves_one_user_turn_and_no_assistant_turn(self):
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
        self.assertEqual(second.status_code, 502)
        self.assertEqual(mocked.call_count, 2)
        stored = self.read_session()
        matching_turns = [
            turn for turn in stored["turns"]
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
        )

        with patch.object(backend, "generate_chat_reply", return_value=execution):
            proposed = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Move the player one cell left.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "proposal_message_001",
                },
            )

        self.assertEqual(proposed.status_code, 200, proposed.text)
        proposed_session = proposed.json()
        self.assertEqual(len(proposed_session["versions"]), 1)
        self.assertEqual(proposed_session["proposals"][0]["status"], "pending")
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

        with patch.object(
            backend,
            "generate_chat_reply",
            return_value=unchanged_execution,
        ):
            response = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please make that concrete revision.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "unchanged_proposal_message_001",
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

        with patch.object(backend, "generate_chat_reply", return_value=execution):
            proposed = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Please revise the map.",
                    "baseVersionId": version_id,
                    "idempotencyKey": "proposal_to_tamper_001",
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
                "uiCues": [],
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
                    "uiCueTexts": [],
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
        self.assertEqual(mocked.call_count, 1)

        with patch.object(backend, "translate_turns") as cached_mock:
            cached = self.client.post(
                f"/api/sessions/{self.session_id}/translations/zh-CN",
                json={"turnIds": [turn["turnId"]]},
            )

        self.assertEqual(cached.status_code, 200, cached.text)
        cached_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
