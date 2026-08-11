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
from llm_client import LLMExecutionResult


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
        self.assertEqual(response.json()["turns"][-1]["role"], "assistant")

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


if __name__ == "__main__":
    unittest.main()
