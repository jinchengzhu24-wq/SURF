import asyncio
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

    async def create(self, **kwargs):
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

    async def close(self):
        return None


class SlowCompletions:
    async def create(self, **kwargs):
        await asyncio.sleep(10)


class SlowClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SlowCompletions())

    async def close(self):
        return None


class LLMClientTests(unittest.TestCase):
    def execute(self, outcomes):
        client = FakeClient(outcomes)

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
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
                    "uiCues": [],
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
        self.assertEqual(request["model"], "deepseek-v4-flash")
        self.assertEqual(request["max_tokens"], llm_client.CHAT_MAX_TOKENS)

    def test_explicit_map_proposal_uses_pro_model_and_larger_output_limit(self):
        response = json.dumps({
            "assistantMessage": "I will prepare the requested map.",
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        })
        client = FakeClient([response])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Please create a reviewable map proposal."}],
                ["############"] * 10,
                "proposal-model-test",
            )

        request = client.chat.completions.calls[0]
        self.assertEqual(request["model"], "deepseek-v4-pro")
        self.assertEqual(request["max_tokens"], llm_client.PROPOSAL_MAX_TOKENS)

    def test_flash_invalid_response_falls_back_to_pro(self):
        valid = json.dumps({
            "assistantMessage": "A grounded fallback response.",
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        })
        client = FakeClient(["not-json", valid])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the route."}],
                ["############"] * 10,
                "fallback-model-test",
            )

        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(result.model, "deepseek-v4-pro")
        self.assertEqual(
            [call["model"] for call in client.chat.completions.calls],
            ["deepseek-v4-flash", "deepseek-v4-pro"],
        )

    def test_wall_clock_limit_cancels_slow_models(self):
        started_at = llm_client.time.monotonic()

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "CHAT_TIMEOUT_SECONDS", 0.06),
            patch.object(llm_client, "PRIMARY_ATTEMPT_TIMEOUT_SECONDS", 0.02),
            patch.object(llm_client, "_create_async_client", return_value=SlowClient()),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the route."}],
                ["############"] * 10,
                "wall-clock-timeout-test",
            )

        self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")
        self.assertLess(llm_client.time.monotonic() - started_at, 1.0)

    def test_invalid_json_uses_fallback_then_fails(self):
        client = FakeClient(["not-json", "still-not-json"])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Hello."}],
                ["############"] * 10,
                "invalid-test",
            )

        self.assertEqual(raised.exception.code, "MODEL_RESPONSE_INVALID")
        self.assertEqual(raised.exception.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)

    def test_timeout_uses_two_models_with_one_sixty_second_total_limit(self):
        timeout = APITimeoutError(
            request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        )
        client = FakeClient([timeout, timeout])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Create a map proposal."}],
                ["############"] * 10,
                "timeout-test",
            )

        self.assertEqual(llm_client.CHAT_TIMEOUT_SECONDS, 60.0)
        self.assertEqual(llm_client.PRIMARY_ATTEMPT_TIMEOUT_SECONDS, 40.0)
        self.assertEqual(llm_client.CHAT_MAX_ATTEMPTS, 2)
        self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")
        self.assertEqual(raised.exception.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)

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
                    "uiCues": [],
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
                "uiCues": [],
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

    def test_invalid_proposal_uses_fallback_then_fails(self):
        payload = json.dumps({
            "assistantMessage": "Here is a map proposal.",
            "guidance": {
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": ["############"] * 10,
            "modificationSummary": "Changed the map.",
        })
        client = FakeClient([payload, payload])
        validated_rows = []

        def reject_proposal(rows):
            validated_rows.append(rows)
            raise ValueError("The proposed map is unsolvable.")

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Please draft that revision."}],
                ["############"] * 10,
                "invalid-proposal-test",
                proposal_validator=reject_proposal,
            )

        self.assertEqual(raised.exception.code, "MODEL_RESPONSE_INVALID")
        self.assertEqual(raised.exception.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)
        self.assertEqual(len(validated_rows), 2)

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
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": ["############"] * 10,
            "modificationSummary": "Narrowed the lane.",
        }

        with self.assertRaisesRegex(ValueError, "cannot include proposedRows"):
            llm_client.validate_chat_response(payload)

    def test_manual_edit_and_warning_ui_cues_are_validated(self):
        payload = {
            "assistantMessage": "You can compare both routing choices directly.",
            "guidance": {
                "move": "challenge_tradeoff",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "Which compromise fits your intention better?",
                "proposalOffer": None,
                "uiCues": [
                    {
                        "type": "warning",
                        "text": "A wider route improves freedom but reduces commitment.",
                    },
                    {
                        "type": "manual_edit",
                        "text": "Try the right-side tile editor and save the result as a new Stage.",
                    },
                ],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload)

        self.assertEqual(
            [cue["type"] for cue in result[4]["uiCues"]],
            ["warning", "manual_edit"],
        )

    def test_legacy_guidance_without_ui_cues_defaults_to_empty(self):
        payload = {
            "assistantMessage": "A concise perspective.",
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload)

        self.assertEqual(result[4]["uiCues"], [])

    def test_composed_message_keeps_cues_for_future_llm_context(self):
        guidance = {
            "move": "challenge_tradeoff",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": "Which direction do you prefer?",
            "proposalOffer": None,
            "uiCues": [
                {"type": "tradeoff", "text": "The open route reduces commitment."},
                {"type": "manual_edit", "text": "Compare it with the tile editor."},
            ],
        }

        message = llm_client._compose_assistant_message(
            "Both routes remain solvable.",
            guidance,
        )

        self.assertEqual(
            message,
            "Both routes remain solvable.\n\n"
            "The open route reduces commitment.\n\n"
            "Compare it with the tile editor.\n\n"
            "Which direction do you prefer?",
        )

    def test_ui_cues_reject_invalid_duplicate_and_missing_tradeoff(self):
        base_guidance = {
            "move": "offer_perspective",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": None,
        }
        cases = (
            (
                [{"type": "unknown", "text": "Unknown cue."}],
                "type is invalid",
                "offer_perspective",
            ),
            (
                [
                    {"type": "manual_edit", "text": "First."},
                    {"type": "manual_edit", "text": "Second."},
                ],
                "cannot repeat a type",
                "offer_perspective",
            ),
            ([], "requires a warning uiCue", "challenge_tradeoff"),
        )

        for ui_cues, expected_error, move in cases:
            with self.subTest(expected_error=expected_error):
                payload = {
                    "assistantMessage": "A grounded response.",
                    "guidance": {
                        **base_guidance,
                        "move": move,
                        "uiCues": ui_cues,
                    },
                    "assessment": None,
                    "proposedRows": None,
                    "modificationSummary": "",
                }

                with self.assertRaisesRegex(ValueError, expected_error):
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
                "uiCues": [],
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
                "uiCues": [],
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

    def test_human_edit_opening_acknowledges_verified_changes_in_chinese(self):
        message = llm_client._compose_assistant_message(
            "在我看来，这会让路线选择更集中。",
            {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "这符合你想强调的体验吗？",
                "proposalOffer": None,
            },
            language="zh-CN",
            assessment_only=True,
            stage_context={
                "source": "human_edit",
                "changeSummary": {
                    "components": ["water", "internalWalls", "player"],
                    "changedCellCount": 6,
                },
            },
        )

        self.assertIn("我注意到你对水域、内部墙体、玩家位置进行了修改", message)
        self.assertIn("已通过确定性检查并确认可解", message)
        self.assertIn("在我看来", message)
        self.assertTrue(message.endswith("这符合你想强调的体验吗？"))


if __name__ == "__main__":
    unittest.main()
