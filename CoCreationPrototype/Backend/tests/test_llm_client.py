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

        if hasattr(outcome, "choices"):
            return outcome

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
        result, client = self.execute([
            "The level has a compact central route.\n\n"
            "When the box enters the water-side corridor, which route choice should the "
            "player notice first so we can judge its readability?"
        ])

        self.assertIn("compact central route", result.assistant_message)
        self.assertIn("water-side corridor", result.assistant_message)
        self.assertEqual(result.guidance["move"], "offer_perspective")
        self.assertEqual(result.attempts_used, 1)
        request = client.chat.completions.calls[0]
        self.assertNotIn("response_format", request)
        self.assertFalse(request["stream"])
        self.assertEqual(request["model"], "deepseek-v4-flash")
        self.assertEqual(request["max_tokens"], llm_client.PLAIN_CHAT_MAX_TOKENS)
        self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})

    def test_non_json_natural_language_is_accepted_immediately(self):
        result, client = self.execute(["This is useful prose, not a JSON object."])

        self.assertEqual(result.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertIn("useful prose", result.assistant_message)

    def test_plain_reply_extracts_intent_card(self):
        result, _ = self.execute([
            "The water now reads as part of the route.\n\n"
            "<GUIDANCE>\n"
            "INTENT: The designer wants the water to affect the route.\n"
            "</GUIDANCE>"
        ])

        self.assertEqual(result.guidance["move"], "clarify_intent")
        self.assertEqual(result.guidance["intentConfidence"], "medium")
        self.assertTrue(result.guidance["intentHypothesis"].startswith("I think you may"))
        self.assertNotIn("GUIDANCE", result.assistant_message)

    def test_plain_reply_extracts_proposal_card(self):
        result, _ = self.execute([
            "A small route linkage would make the water consequential.\n\n"
            "<GUIDANCE>\n"
            "PROPOSAL_SUMMARY: Link the lower target to the water edge\n"
            "PROPOSAL_RATIONALE: Make the first push depend on reading the water route\n"
            "</GUIDANCE>"
        ])

        self.assertEqual(result.guidance["move"], "offer_revision")
        self.assertEqual(
            result.guidance["proposalOffer"]["summary"],
            "Link the lower target to the water edge",
        )
        self.assertNotIn("PROPOSAL_SUMMARY", result.assistant_message)

    def test_plain_reply_can_extract_intent_and_proposal_cards_together(self):
        result, _ = self.execute([
            "That gives us a focused next move.\n\n"
            "<GUIDANCE>\n"
            "INTENT: You want the water to shape the route without dominating it\n"
            "PROPOSAL_SUMMARY: Move the target beneath the water edge\n"
            "PROPOSAL_RATIONALE: Tie one push decision to the water while preserving the main route\n"
            "</GUIDANCE>"
        ])

        self.assertEqual(result.guidance["move"], "offer_revision")
        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertIsNotNone(result.guidance["proposalOffer"])

    def test_malformed_guidance_is_hidden_without_failing_reply(self):
        for reply in (
            "Visible reply.\n<GUIDANCE>\nINTENT: You want a tighter route",
            "Visible reply.\n<GUIDANCE>\nUNKNOWN: hidden\n</GUIDANCE>",
            "Visible reply.\n<GUIDANCE>\nPROPOSAL_SUMMARY: Incomplete\n</GUIDANCE>",
        ):
            with self.subTest(reply=reply):
                result, _ = self.execute([reply])
                self.assertEqual(result.assistant_message, "Visible reply.")
                self.assertIsNone(result.guidance["intentHypothesis"])
                self.assertIsNone(result.guidance["proposalOffer"])

    def test_repeated_guidance_cards_are_suppressed_but_changed_intent_remains(self):
        content = (
            "This direction is now concrete.\n"
            "<GUIDANCE>\n"
            "INTENT: I think you may want water to shape the route\n"
            "PROPOSAL_SUMMARY: Link the target to the water edge\n"
            "PROPOSAL_RATIONALE: Make water influence the first push\n"
            "</GUIDANCE>"
        )
        client = FakeClient([content])
        stage_context = {
            "recentGuidance": {
                "intentHypothesis": "I think you may want water to shape the route.",
                "proposalOffer": {
                    "summary": "Link the target to the water edge",
                    "rationale": "Make water influence the first push.",
                },
            }
        }

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Yes, do that."}],
                ["############"] * 10,
                "guidance-dedup-test",
                stage_context=stage_context,
            )

        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["proposalOffer"])

        _, changed_intent, _, _ = llm_client._extract_plain_guidance(
            "Reply.\n<GUIDANCE>\nINTENT: I think you may want water to control two route decisions\n</GUIDANCE>",
            "en",
            stage_context,
        )
        self.assertIsNotNone(changed_intent)

    def test_plain_prompt_defaults_to_no_question(self):
        messages = llm_client.build_plain_chat_messages(
            [{"role": "user", "content": "Make the choice smaller but consequential."}],
            ["############"] * 10,
        )

        self.assertIn("default to no question", messages[0]["content"])
        self.assertIn("your reply must contain no question", messages[0]["content"])
        self.assertIn("Never ask for a preference the designer has already stated", messages[0]["content"])
        self.assertIn("<GUIDANCE>", messages[0]["content"])
        self.assertIn("recentGuidance", messages[0]["content"])
        self.assertIn("you MUST output INTENT", messages[0]["content"])

    def test_pure_generic_question_uses_fallback_model(self):
        result, client = self.execute([
            "What do you think?",
            "The water-side route could carry more of the decision.",
        ])

        self.assertIn("water-side route", result.assistant_message)
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(client.chat.completions.calls[1]["model"], "deepseek-v4-pro")
        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_two_pure_generic_questions_return_low_quality_error(self):
        client = FakeClient(["What do you think?", "Is this direction okay?"])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            with self.assertRaises(llm_client.LLMServiceError) as raised:
                llm_client.generate_chat_reply(
                    [{"role": "user", "content": "Assess the level."}],
                    ["############"] * 10,
                    "low-quality-test",
                )

        self.assertEqual(raised.exception.code, "MODEL_LOW_QUALITY_RESPONSE")
        self.assertEqual(raised.exception.attempts_used, 2)

    def test_multiple_questions_stay_in_body_without_failing(self):
        reply = "What should stay? What should change?"
        result, _ = self.execute([reply])

        self.assertEqual(result.assistant_message, reply)
        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_redundant_question_is_removed_after_an_explicit_direction(self):
        client = FakeClient([
            "That would make the opening commitment more legible. What else do you prefer?"
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Keep it compact, but make the first push consequential."}],
                ["############"] * 10,
                "no-forced-question-test",
            )

        self.assertTrue(result.assistant_message.startswith(
            "That would make the opening commitment more legible."
        ))
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertEqual(result.guidance["uiCues"][0]["type"], "manual_edit")

    def test_plain_guidance_extracts_warning_and_manual_edit(self):
        result, _ = self.execute([
            "The water can shape the first route decision.\n"
            "<GUIDANCE>WARNING: The box may lose its escape route beside the water after "
            "the first push || MANUAL_EDIT: Try a small experiment around the water edge "
            "and watch whether the route choice becomes clearer</GUIDANCE>"
        ])

        self.assertEqual(
            [cue["type"] for cue in result.guidance["uiCues"]],
            ["warning", "manual_edit"],
        )
        self.assertNotIn("GUIDANCE", result.assistant_message)
        self.assertLessEqual(len(result.guidance["uiCues"]), 2)

    def test_invalid_or_duplicate_ui_cue_fields_do_not_break_visible_reply(self):
        visible, _, _, cues = llm_client._extract_plain_guidance(
            "Visible reply.\n<GUIDANCE>WARNING: The water colors look dull || "
            "WARNING: The box may be stuck beside the water || UNKNOWN: hidden || "
            "MANUAL_EDIT: Move the wall at row 3 and observe it</GUIDANCE>",
            "en",
            {},
        )

        self.assertEqual(visible, "Visible reply.")
        self.assertEqual(cues, [])

    def test_ui_cue_dedup_allows_same_warning_after_evidence_changes(self):
        content = (
            "Visible reply.\n<GUIDANCE>WARNING: The box may lose its escape route "
            "beside the water after the first push</GUIDANCE>"
        )
        recent = {
            "recentGuidance": {
                "uiCues": {
                    "warning": {
                        "text": "The box may lose its escape route beside the water after the first push.",
                        "evidenceSignature": "same",
                    }
                }
            },
            "guidanceEvidenceSignature": "same",
        }
        _, _, _, same_cues = llm_client._extract_plain_guidance(content, "en", recent)
        recent["guidanceEvidenceSignature"] = "changed"
        _, _, _, changed_cues = llm_client._extract_plain_guidance(content, "en", recent)

        self.assertEqual(same_cues, [])
        self.assertEqual(changed_cues[0]["type"], "warning")

    def test_human_edit_without_play_evidence_gets_tentative_warning(self):
        warning = llm_client._deterministic_warning(
            "The revision is saved.",
            "en",
            {
                "source": "human_edit",
                "changeSummary": {"components": ["water", "boxes"]},
            },
            None,
        )

        self.assertIn("may alter", warning)
        self.assertIn("first push", warning)

    def test_contextual_manual_edit_avoids_exact_coordinates(self):
        cue = llm_client._contextual_manual_edit(
            ["############", "#p   @     #", "#    @ s t #"] + ["############"] * 7,
            "en",
        )

        self.assertIn("water edge", cue)
        self.assertIn("watch", cue)
        self.assertNotRegex(cue, r"\b(?:row|column)\s+\d+")

    def test_evidence_grounded_warning_is_extracted_but_aesthetic_opinion_is_not(self):
        warning = llm_client._deterministic_warning(
            "I am concerned the box may lose its escape route beside the water after the first push.",
            "en",
            {},
            None,
        )
        aesthetic = llm_client._deterministic_warning(
            "The water looks too plain and the colors feel dull.",
            "en",
            {},
            None,
        )

        self.assertIsNotNone(warning)
        self.assertIsNone(aesthetic)

    def test_specific_vivid_question_is_kept_as_blue_card(self):
        result, _ = self.execute([
            "The water edge can make the route legible. When the box enters the corridor "
            "beside the water, which route choice should the player notice first so we can "
            "judge its readability?"
        ])

        self.assertIn("water", result.guidance["followUpQuestion"])
        self.assertNotIn("?", result.assistant_message.split("\n\n", 1)[0])

    def test_generic_question_is_removed_when_declarative_body_exists(self):
        result, client = self.execute([
            "Moving the target beside the water would create a route decision. "
            "Does this direction work?"
        ])

        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertNotIn("Does this direction work", result.assistant_message)

    def test_chinese_agreement_is_treated_as_an_explicit_direction(self):
        self.assertTrue(llm_client._latest_user_states_direction([
            {"role": "user", "content": "做点联动吧"},
        ]))

    def test_explicit_agreement_gets_deterministic_cards_and_no_questions(self):
        client = FakeClient([
            "我会把右下目标与水塘做局部联动。这样能让水域影响第一次推动。"
            "你还想改别的区域吗？要不要扩大水域？"
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "user", "content": "我认为水域纯摆设"},
                    {"role": "assistant", "content": "可以让水塘与目标点形成一个小联动。"},
                    {"role": "user", "content": "做点联动吧"},
                ],
                ["############"] * 10,
                "deterministic-guidance-test",
                language="zh-CN",
                stage_context={"recentGuidance": {}},
            )

        self.assertEqual(result.guidance["move"], "offer_revision")
        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertIsNotNone(result.guidance["proposalOffer"])
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertNotIn("？", result.assistant_message)
        self.assertNotIn("吗", result.assistant_message)

    def test_invalid_stage_json_falls_back_to_plain_opening(self):
        client = FakeClient(["   ", "The water narrows the central route in an interesting way."])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_stage_assessment(
                [],
                ["############"] * 10,
                "en",
                {"solvable": True, "solutionSteps": 24, "solutionPushes": 6},
                {},
                "stage-fallback-test",
                {"source": "initial", "initialDraftMethod": "description_generation"},
            )

        self.assertIn("water narrows", result.assistant_message)
        self.assertIn("deterministic solver", result.assessment["solutionSummary"])
        self.assertEqual(len(client.chat.completions.calls), 2)
        self.assertEqual(
            client.chat.completions.calls[0]["response_format"],
            {"type": "json_object"},
        )
        self.assertNotIn("response_format", client.chat.completions.calls[1])

    def test_length_truncation_uses_fallback_model(self):
        truncated = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content=""),
            )]
        )
        valid = "A grounded fallback response."
        client = FakeClient([truncated, valid])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the route."}],
                ["############"] * 10,
                "length-fallback-test",
            )

        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(result.model, "deepseek-v4-pro")

    def test_explicit_map_proposal_uses_pro_model_and_larger_output_limit(self):
        proposed_rows = ["############"] * 10
        response = json.dumps({
            "assistantMessage": "I will prepare the requested map.",
            "guidance": {
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": proposed_rows,
            "modificationSummary": "Changed the route.",
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
        self.assertEqual(request["response_format"], {"type": "json_object"})

    def test_explicit_map_proposal_rejects_text_only_result(self):
        text_only = json.dumps({
            "assistantMessage": "I would narrow the route.",
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
        client = FakeClient([text_only, text_only])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Please create a reviewable map proposal."}],
                ["############"] * 10,
                "proposal-required-test",
            )

        self.assertEqual(raised.exception.code, "MODEL_RESPONSE_INVALID")
        self.assertEqual(len(client.chat.completions.calls), 2)
        retry_prompt = client.chat.completions.calls[1]["messages"][0]["content"]
        self.assertIn("explicitly authorized a complete map proposal", retry_prompt)

    def test_flash_empty_response_falls_back_to_pro(self):
        client = FakeClient(["   \n ", "A grounded fallback response."])

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
        self.assertNotIn("response_format", client.chat.completions.calls[1])

    def test_corrective_fallback_does_not_mutate_original_messages(self):
        messages = [{"role": "system", "content": "Original contract."}]

        corrected = llm_client._messages_with_validation_feedback(
            messages,
            "guidance is required.",
        )

        self.assertEqual(messages[0]["content"], "Original contract.")
        self.assertIn("guidance is required", corrected[0]["content"])

    def test_safe_validation_reason_does_not_include_invalid_json_content(self):
        try:
            json.loads('{"assistantMessage":')
        except json.JSONDecodeError as exception:
            reason = llm_client._safe_validation_reason(exception)

        self.assertEqual(reason, "The response was not a complete valid JSON object.")
        self.assertNotIn("assistantMessage", reason)

    def test_wall_clock_limit_cancels_slow_models(self):
        started_at = llm_client.time.monotonic()

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "PLAIN_CHAT_TIMEOUT_SECONDS", 0.06),
            patch.object(llm_client, "PLAIN_PRIMARY_TIMEOUT_SECONDS", 0.02),
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

    def test_two_empty_plain_responses_use_fallback_then_fail(self):
        client = FakeClient(["   ", "\n\t"])

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

        self.assertEqual(raised.exception.code, "MODEL_EMPTY_RESPONSE")
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

    def test_structured_question_is_removed_from_assistant_body(self):
        result = llm_client.validate_chat_response(
            {
                "assistantMessage": (
                    "The two targets create distinct routes.\n\n"
                    "Would you like to preserve that split?"
                ),
                "guidance": {
                    "move": "offer_perspective",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": "Which route should feel more important?",
                    "proposalOffer": None,
                    "uiCues": [],
                },
                "assessment": None,
                "proposedRows": None,
                "modificationSummary": "",
            }
        )

        self.assertEqual(result[0], "The two targets create distinct routes.")

    def test_question_without_structured_follow_up_is_extracted(self):
        payload = {
            "assistantMessage": (
                "In my view, a tighter route would make the push order more visible. "
                "Would you like to preserve that split?"
            ),
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
        }

        result = llm_client.validate_chat_response(payload)

        self.assertEqual(
            result[0],
            "In my view, a tighter route would make the push order more visible.",
        )
        self.assertEqual(
            result[4]["followUpQuestion"],
            "Would you like to preserve that split?",
        )

    def test_question_only_message_is_rejected(self):
        payload = {
            "assistantMessage": "Would you like to preserve that split?",
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
        }

        with self.assertRaisesRegex(ValueError, "declarative response"):
            llm_client.validate_chat_response(payload)

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
        fallback_system = client.chat.completions.calls[1]["messages"][0]["content"]
        self.assertIn("guidance.move as deliver_revision", fallback_system)
        self.assertIn("do not downgrade", fallback_system)

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

    def test_english_intent_hypothesis_uses_direct_tentative_voice(self):
        cases = {
            "The designer wants to make the route feel risky.":
                "I think you may want to make the route feel risky.",
            "The player seems to want a tighter opening.":
                "I get the sense that you may want a tighter opening.",
            "You want the second push to be surprising.":
                "I think you may want the second push to be surprising.",
            "I think you may be emphasizing push order.":
                "I think you may be emphasizing push order.",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    llm_client._normalize_intent_hypothesis(source, "en"),
                    expected,
                )

    def test_chinese_intent_hypothesis_uses_direct_tentative_voice(self):
        cases = {
            "设计者想要让路线更紧张。": "我猜你可能想要让路线更紧张。",
            "玩家希望突出推动顺序。": "我猜你可能想要突出推动顺序。",
            "我猜你可能更在意路线辨识度。": "我猜你可能更在意路线辨识度。",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    llm_client._normalize_intent_hypothesis(source, "zh-CN"),
                    expected,
                )

    def test_validated_guidance_normalizes_report_style_intent(self):
        payload = {
            "assistantMessage": "The tighter route could make the opening more deliberate.",
            "guidance": {
                "move": "clarify_intent",
                "intentHypothesis": "The designer wants a more deliberate opening.",
                "intentConfidence": "low",
                "followUpQuestion": "What part of that direction matters most to you?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, language="en")

        self.assertEqual(
            result[4]["intentHypothesis"],
            "I think you may want a more deliberate opening.",
        )

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

    def test_stage_opening_is_neutral_and_may_include_one_question(self):
        payload = {
            "assistantMessage": (
                "The box and target share a compact central route. "
                "In my view, that makes the opening relationship easy to notice."
            ),
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

    def test_stage_opening_allows_no_question(self):
        payload = {
            "assistantMessage": (
                "The water turns the lower route into a deliberate detour. "
                "To me, that gives the small room a surprisingly clear identity."
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "This looks deliberate to me.",
                "features": ["Lower detour"],
                "suggestions": ["Playtest the route"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, assessment_only=True)

        self.assertIsNone(result[4]["followUpQuestion"])
        self.assertIsNone(result[1]["satisfactionQuestion"])

    def test_stage_opening_requires_archival_question_to_match_discussion(self):
        payload = {
            "assistantMessage": (
                "The water shapes the opening route. "
                "In my view, it makes the first push worth discussing."
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "What were you exploring with the central water?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "In my view, the opening requires attention.",
                "features": ["Central water"],
                "suggestions": ["Discuss the opening push"],
                "satisfactionQuestion": "What other part should we discuss next?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        with self.assertRaisesRegex(ValueError, "must match"):
            llm_client.validate_chat_response(payload, assessment_only=True)

    def test_stage_opening_prompt_is_concrete_and_non_anchoring(self):
        messages = llm_client.build_chat_messages(
            [],
            ["############"] * 10,
            assessment_only=True,
        )
        prompt = messages[0]["content"]

        self.assertIn("one to three short paragraphs", prompt)
        self.assertIn("one or two concrete map choices", prompt)
        self.assertIn("grounded personal perspective", prompt)
        self.assertIn("Do not force a question", prompt)
        self.assertIn("Do not say Welcome to Stage", prompt)
        self.assertIn("either-or choice", prompt)
        self.assertIn("还是/或者/或是", prompt)
        self.assertIn("do not ask a yes/no question", prompt)
        self.assertIn("not as the prose style", prompt)

    def test_dg_initial_provenance_does_not_attribute_exact_tiles(self):
        guidance = llm_client._build_draft_provenance_guidance(
            {
                "source": "initial",
                "initialDraftMethod": "description_generation",
            }
        )

        self.assertIn("generator produced every exact tile placement", guidance)
        self.assertIn("Never ask why the designer placed", guidance)
        self.assertIn("Do not invent or quote parameter values", guidance)
        self.assertIn("Never say or imply that the designer intended", guidance)
        self.assertIn("How does this generated result compare", guidance)
        self.assertIn("what surprised them", guidance)

    def test_pc_initial_provenance_separates_sketch_from_completion(self):
        guidance = llm_client._build_draft_provenance_guidance(
            {
                "source": "initial",
                "initialDraftMethod": "partial_completion",
            }
        )

        self.assertIn("box starts, targets, and broad room/wall constraints", guidance)
        self.assertIn("added the exact water, generated internal walls", guidance)
        self.assertIn("never attribute a particular internal wall", guidance)
        self.assertIn("never claim that the completion system produced or filled in all walls", guidance)
        self.assertIn("box-target relationship", guidance)

    def test_later_stage_provenance_uses_actual_stage_source(self):
        human = llm_client._build_draft_provenance_guidance(
            {"source": "human_edit", "initialDraftMethod": "description_generation"}
        )
        accepted = llm_client._build_draft_provenance_guidance(
            {"source": "llm_accepted", "initialDraftMethod": "partial_completion"}
        )
        restored = llm_client._build_draft_provenance_guidance(
            {"source": "restored", "initialDraftMethod": "partial_completion"}
        )

        self.assertIn("directly edited by the designer", human)
        self.assertIn("explicitly accepted", accepted)
        self.assertIn("restores an earlier saved version", restored)

    def test_stage_opening_removes_recoverable_english_choice_anchor(self):
        question = (
            "What drew you to place the target beside the box—were you aiming for "
            "a quick solve, or a longer sequence?"
        )

        self.assertEqual(
            llm_client._normalize_opening_question(question),
            "What drew you to place the target beside the box?",
        )

    def test_stage_opening_removes_recoverable_chinese_choice_anchor(self):
        question = "你把箱子和目标放在紧邻的位置，是希望快速完成，还是继续扩展？"

        self.assertEqual(
            llm_client._normalize_opening_question(question),
            "你把箱子和目标放在紧邻的位置时，你最先考虑的是什么？",
        )

    def test_stage_opening_chinese_anchor_does_not_duplicate_time_suffix(self):
        question = "你设计这个初始布局时，是想保持开放，还是增加墙体？"

        self.assertEqual(
            llm_client._normalize_opening_question(question),
            "你设计这个初始布局时，你最先考虑的是什么？",
        )

    def test_stage_opening_converts_recoverable_chinese_yes_no_question(self):
        question = "你选择把箱子和目标放在同一行，是刻意强调绕行吗？"

        self.assertEqual(
            llm_client._normalize_opening_question(question),
            "你选择把箱子和目标放在同一行时，你最先考虑的是什么？",
        )

    def test_stage_opening_rejects_english_yes_no_question(self):
        with self.assertRaisesRegex(ValueError, "cannot anchor"):
            llm_client._normalize_opening_question(
                "Did you intend the central water to control the first push?"
            )

    def test_stage_opening_rejects_unrecoverable_choice_anchor(self):
        with self.assertRaisesRegex(ValueError, "cannot anchor"):
            llm_client._normalize_opening_question(
                "Would you prefer a quick solve or a longer sequence?"
            )

    def test_stage_opening_single_block_remains_natural(self):
        message = (
            "The water divides the room. In my view, that makes the first push clearer. "
            "The open lower area may still invite experimentation."
        )

        result = llm_client._format_stage_opening_paragraphs(message)

        self.assertEqual(len(result.split("\n\n")), 1)
        self.assertIn("In my view", result)

    def test_stage_opening_rejects_intention_inference(self):
        payload = {
            "assistantMessage": "You want a difficult level.",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": "You want a difficult level.",
                "intentConfidence": "medium",
                "followUpQuestion": "What shaped this placement choice?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "A route exists.",
                "difficultyOpinion": "It may be difficult.",
                "features": ["One route"],
                "suggestions": ["Review it"],
                "satisfactionQuestion": "What shaped this placement choice?",
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

    def test_translation_validation_preserves_ids_nulls_and_cue_order(self):
        source = [
            {
                "turnId": "turn-1",
                "body": "A compact route.",
                "followUpQuestion": "What should stand out?",
                "intentHypothesis": None,
                "proposalOfferSummary": None,
                "proposalOfferRationale": None,
                "uiCueTexts": ["The corner may be tight."],
                "proposalSummary": None,
            }
        ]
        payload = {
            "translations": [
                {
                    "turnId": "turn-1",
                    "body": "一条紧凑的路线。",
                    "followUpQuestion": "你希望什么最突出？",
                    "intentHypothesis": None,
                    "proposalOfferSummary": None,
                    "proposalOfferRationale": None,
                    "uiCueTexts": ["这个角落可能较紧。"],
                    "proposalSummary": None,
                }
            ]
        }

        self.assertEqual(
            llm_client.validate_translation_response(payload, source),
            payload["translations"],
        )

        payload["translations"][0]["intentHypothesis"] = "新增的意图"

        with self.assertRaisesRegex(ValueError, "must remain null"):
            llm_client.validate_translation_response(payload, source)


if __name__ == "__main__":
    unittest.main()
