import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_sessions as session_fixtures
from cocreation.api.message_request_identity import conflicts_with_saved_user_turn, message_request_signature
from cocreation.llm.config_and_runtime import LLMExecutionResult, LLMServiceError
from cocreation.persistence.database import load_json
from tests.fakes import RecordedCall, SessionTestCase


class MessageRequestIdentityTests(unittest.TestCase):
    def test_signature_matches_the_reference_commit(self):
        cases = (
            (
                "Keep the route readable.", "stage-1", False, "none", None,
                "f72dd61b4d69d9904da8cae13c472c282d94f6d76d3178e226a3fda060c87b59",
            ),
            (
                '请比较水域\n"这块" 🧩', "stage-2", True, "none", None,
                "cb0bcdc6cda1b5acda3941a8ace766ea9fad665efe726f13f65d63687ca77258",
            ),
            (
                "Apply the change.", "stage-1", False, "execute_revision", "turn-5",
                "d20fdbafdd6907f4751a43a45d6735686a2b0dc798837ea5af50720901bc941d",
            ),
        )
        for content, stage, mode, action, source, expected in cases:
            with self.subTest(content=content, action=action):
                self.assertEqual(
                    message_request_signature(
                        content,
                        base_version_id=stage,
                        request_proposal=mode,
                        action=action,
                        source_turn_id=source,
                    ),
                    expected,
                )

    def test_saved_user_turn_must_match_content_and_stage(self):
        saved = {"content": "Keep the route readable.", "version_id": "stage-1"}
        self.assertFalse(conflicts_with_saved_user_turn(None, saved["content"], "stage-1"))
        self.assertFalse(conflicts_with_saved_user_turn(saved, saved["content"], "stage-1"))
        self.assertTrue(conflicts_with_saved_user_turn(saved, "Keep the route readable. ", "stage-1"))
        self.assertTrue(conflicts_with_saved_user_turn(saved, saved["content"], "stage-2"))
        self.assertTrue(conflicts_with_saved_user_turn({**saved, "version_id": None}, saved["content"], "stage-1"))


class MessageRequestIdentityHttpTests(SessionTestCase):
    create_and_open_session = session_fixtures.CoCreationSessionTests.create_and_open_session
    read_session = session_fixtures.CoCreationSessionTests.read_session
    offer_bound_revision = session_fixtures.CoCreationSessionTests.offer_bound_revision
    classify = staticmethod(session_fixtures.CoCreationSessionTests._fake_turn_understanding)

    def setUp(self):
        self.open_workspace(
            settings_overrides={"binary_discovery_enabled": False},
            llm_handlers={"classify_turn_understanding": self.classify},
        )
        self.session_id, _ = self.create_and_open_session()
        self.version_id = self.read_session()["currentVersionId"]
        self.messages_url = f"/api/sessions/{self.session_id}/messages"

    def test_understanding_failure_binds_same_key_before_a_user_turn_exists(self):
        content = "这个布局怎么样？"
        payload = {
            "content": f"  {content}\n",
            "baseVersionId": self.version_id,
            "idempotencyKey": "signed-failure",
        }
        failure = LLMServiceError("UPSTREAM_TIMEOUT", "Fixture timeout.", "offline", True, 2, 504)
        prior_turns = self.read_session()["turns"]
        with self.using_dependencies(llm_handlers={"classify_turn_understanding": failure}):
            failed = self.client.post(self.messages_url, json=payload)
        self.assertEqual(failed.status_code, 504, failed.text)
        self.assertEqual(self.read_session()["turns"], prior_turns)

        with self.services.connect() as database:
            event = database.execute(
                "SELECT payload_json FROM audit_events WHERE session_id = ? "
                "AND event_type = 'message_generation_failed' "
                "AND json_extract(payload_json, '$.messageKey') = ?",
                (self.session_id, payload["idempotencyKey"]),
            ).fetchone()
        expected_json = (
            f'{{"content":"{content}","baseVersionId":"{self.version_id}",'
            '"requestProposal":false,"action":"none","sourceTurnId":null}'
        )
        self.assertEqual(
            load_json(event["payload_json"])["requestSignature"],
            hashlib.sha256(expected_json.encode("utf-8")).hexdigest(),
        )

        classify = RecordedCall(side_effect=self.classify)
        generate = RecordedCall(return_value=LLMExecutionResult("The opening is spacious.", 1, "offline"))
        with self.using_dependencies(llm_handlers={
            "classify_turn_understanding": classify,
            "generate_chat_reply": generate,
        }):
            for change in ({"requestProposal": True}, {"sourceTurnId": ""}):
                with self.subTest(change=change):
                    conflict = self.client.post(self.messages_url, json={**payload, **change})
                    self.assertEqual(conflict.status_code, 409, conflict.text)
                    self.assertEqual(conflict.json()["code"], "IDEMPOTENCY_CONFLICT")
                    self.assertEqual(
                        conflict.json()["message"], "The failed message key belongs to a different request."
                    )
            classify.assert_not_called()
            generate.assert_not_called()
            retry = self.client.post(self.messages_url, json={**payload, "content": content})
            self.assertEqual(retry.status_code, 200, retry.text)
            replay = self.client.post(self.messages_url, json=payload)
            self.assertEqual(replay.status_code, 200, replay.text)
        classify.assert_called_once()
        generate.assert_called_once()
        self.assertEqual(replay.json()["turns"], retry.json()["turns"])
        self.assertEqual(
            [turn["role"] for turn in replay.json()["turns"] if turn["requestId"] == payload["idempotencyKey"]],
            ["user", "assistant"],
        )

    def test_replayed_card_action_rejects_a_changed_action_or_source(self):
        offer = self.offer_bound_revision(
            session_fixtures.WATER_RESHAPE_BRIEF,
            summary="Adjust the water distribution",
            message_key="identity-offer",
        )
        payload = {
            "content": "Execute this frozen water proposal.",
            "baseVersionId": self.version_id,
            "idempotencyKey": "identity-execute",
            "action": "execute_revision",
            "sourceTurnId": offer["turnId"],
        }
        provider = RecordedCall(side_effect=AssertionError("Frozen execution must not call the provider"))
        with self.using_dependencies(llm_options={"client_factory": provider}):
            executed = self.client.post(self.messages_url, json=payload)
        self.assertEqual(executed.status_code, 200, executed.text)
        provider.assert_not_called()

        generate = RecordedCall(side_effect=AssertionError("Reusing a saved card action must not generate a reply"))
        with self.using_dependencies(llm_handlers={"generate_chat_reply": generate}):
            for change in ({"action": "challenge_revision"}, {"sourceTurnId": "another-offer"}):
                with self.subTest(change=change):
                    conflict = self.client.post(self.messages_url, json={**payload, **change})
                    self.assertEqual(conflict.status_code, 409, conflict.text)
                    self.assertEqual(conflict.json()["code"], "IDEMPOTENCY_CONFLICT")
                    self.assertEqual(
                        conflict.json()["message"], "The message key was already used for a different card action."
                    )
            replay = self.client.post(self.messages_url, json=payload)
            self.assertEqual(replay.status_code, 200, replay.text)
        generate.assert_not_called()
        self.assertEqual(replay.json()["turns"], executed.json()["turns"])
        self.assertEqual(replay.json()["proposals"], executed.json()["proposals"])
