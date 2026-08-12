import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import APITimeoutError


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import llm_client


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))]
        )


class FakeClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=FakeCompletions(outcomes))


class LLMClientTests(unittest.TestCase):
    def execute(self, outcomes):
        client = FakeClient(outcomes)

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_get_client", return_value=client),
            patch.object(llm_client.time, "sleep"),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the level."}],
                ["############"] * 10,
                "request-test",
            )

        return result, client

    def test_valid_response_is_returned(self):
        result, client = self.execute(
            [json.dumps({
                "assistantMessage": "The level has a compact central route.",
                "guidance": {
                    "move": "offer_perspective",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": "Which part would you like to examine first?",
                    "proposalOffer": None,
                },
                "assessment": None,
                "proposedRows": None,
                "modificationSummary": "",
            })]
        )

        self.assertIn("compact central route", result.assistant_message)
        self.assertIn("Which part", result.assistant_message)
        self.assertEqual(result.guidance["move"], "offer_perspective")
        self.assertEqual(result.attempts_used, 1)
        request = client.chat.completions.calls[0]
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertFalse(request["stream"])

    def test_invalid_json_fails_without_automatic_retry(self):
        client = FakeClient(["not-json"])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_get_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Hello."}],
                ["############"] * 10,
                "invalid-test",
            )

        self.assertEqual(raised.exception.code, "MODEL_RESPONSE_INVALID")
        self.assertEqual(raised.exception.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_timeout_uses_one_sixty_second_client_attempt(self):
        timeout = APITimeoutError(
            request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        )
        client = FakeClient([timeout])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_get_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Create a map proposal."}],
                ["############"] * 10,
                "timeout-test",
            )

        self.assertEqual(llm_client.CHAT_TIMEOUT_SECONDS, 60.0)
        self.assertEqual(llm_client.CHAT_MAX_ATTEMPTS, 1)
        self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")
        self.assertEqual(raised.exception.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_missing_api_key_fails_without_model_call(self):
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Hello."}],
                ["############"] * 10,
                "config-test",
            )

        self.assertEqual(raised.exception.code, "CONFIGURATION_ERROR")
        self.assertEqual(raised.exception.attempts_used, 0)

    def test_chinese_response_is_supported(self):
        result = llm_client.validate_chat_response(
            {
                "assistantMessage": "我注意到中央路线比较紧凑。",
                "guidance": {
                    "move": "offer_perspective",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": "你想先讨论哪一部分？",
                    "proposalOffer": None,
                },
                "assessment": None,
                "proposedRows": None,
                "modificationSummary": "",
            }
        )

        self.assertEqual(result[0], "我注意到中央路线比较紧凑。")

    def test_structured_assessment_and_proposal_are_returned(self):
        rows = ["############"] * 10
        payload = {
            "assistantMessage": "Here is a focused alternative.",
            "guidance": {
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
            "assessment": {
                "solutionSummary": "One box route.",
                "difficultyOpinion": "Likely easy.",
                "features": ["Compact"],
                "suggestions": ["Move the player"],
                "satisfactionQuestion": "Does this match your intention?",
            },
            "proposedRows": rows,
            "modificationSummary": "Moved the player.",
        }

        result = llm_client.validate_chat_response(payload)

        self.assertEqual(result[1]["features"], ["Compact"])
        self.assertEqual(result[2], rows)
        self.assertEqual(result[3], "Moved the player.")
        self.assertEqual(result[4]["move"], "deliver_revision")

    def test_invalid_proposal_fails_without_automatic_retry(self):
        payload = json.dumps({
            "assistantMessage": "Here is a map proposal.",
            "guidance": {
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
            "assessment": None,
            "proposedRows": ["############"] * 10,
            "modificationSummary": "Changed the map.",
        })
        client = FakeClient([payload])
        validated_rows = []

        def reject_proposal(rows):
            validated_rows.append(rows)
            raise ValueError("The proposed map is unsolvable.")

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_get_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Please draft that revision."}],
                ["############"] * 10,
                "invalid-proposal-test",
                proposal_validator=reject_proposal,
            )

        self.assertEqual(raised.exception.code, "MODEL_RESPONSE_INVALID")
        self.assertEqual(raised.exception.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertEqual(len(validated_rows), 1)

    def test_unsolicited_revision_offer_cannot_include_map(self):
        payload = {
            "assistantMessage": "A narrower approach lane could add commitment.",
            "guidance": {
                "move": "offer_revision",
                "intentHypothesis": "You may want the first push to feel consequential.",
                "intentConfidence": "low",
                "followUpQuestion": "Would you like me to draft that direction?",
                "proposalOffer": {
                    "summary": "Narrow the first approach lane",
                    "rationale": "It would make the opening choice more deliberate.",
                },
            },
            "assessment": None,
            "proposedRows": ["############"] * 10,
            "modificationSummary": "Narrowed the lane.",
        }

        with self.assertRaisesRegex(ValueError, "cannot include proposedRows"):
            llm_client.validate_chat_response(payload)

    def test_stage_opening_is_neutral_and_requires_one_question(self):
        payload = {
            "assistantMessage": "The box and target share a compact central route.",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "What would you like another player to notice first?",
                "proposalOffer": None,
            },
            "assessment": {
                "solutionSummary": "The solver found a direct route.",
                "difficultyOpinion": "This looks approachable to me.",
                "features": ["Compact route"],
                "suggestions": ["Discuss the opening choice"],
                "satisfactionQuestion": "What would you like another player to notice first?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, assessment_only=True)

        self.assertEqual(result[4]["move"], "observe_stage")
        self.assertIsNone(result[4]["intentHypothesis"])

    def test_stage_opening_rejects_intention_inference(self):
        payload = {
            "assistantMessage": "You want a difficult level.",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": "You want a difficult level.",
                "intentConfidence": "medium",
                "followUpQuestion": "Is that right?",
                "proposalOffer": None,
            },
            "assessment": {
                "solutionSummary": "A route exists.",
                "difficultyOpinion": "It may be difficult.",
                "features": ["One route"],
                "suggestions": ["Review it"],
                "satisfactionQuestion": "Is that right?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        with self.assertRaisesRegex(ValueError, "cannot infer intention"):
            llm_client.validate_chat_response(payload, assessment_only=True)


if __name__ == "__main__":
    unittest.main()
