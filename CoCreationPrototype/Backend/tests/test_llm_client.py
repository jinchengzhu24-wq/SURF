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


OPERATION_BASE_ROWS = [
    "############",
    "#..........#",
    "#..........#",
    "#..........#",
    "#...p......#",
    "#...s.t....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]


def operation_payload(*operation_sets):
    return json.dumps({
        "candidates": [
            {"operations": operations}
            for operations in operation_sets
        ]
    })


def revision_plan_payload(
    effect="relocate_target",
    operators=None,
    focus=None,
    preserve=None,
    edit_budget=2,
    metric_goals=None,
):
    return json.dumps({
        "strategies": [{
            "effect": effect,
            "focus": focus or {"row": 6, "column": 8, "radius": 1},
            "operators": operators or ["move_target"],
            "preserve": preserve or [
                "outer_shell",
                "player",
                "boxes",
                "water",
                "unrelated_areas",
            ],
            "editBudget": edit_budget,
            "metricGoals": metric_goals or [],
        }]
    })


TARGET_SHIFT_OPERATIONS = [
    {"row": 6, "column": 7, "from": "t", "to": "."},
    {"row": 6, "column": 8, "from": ".", "to": "t"},
]


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
    def execute(self, outcomes, rows=None, **reply_kwargs):
        client = FakeClient(outcomes)
        conversation = reply_kwargs.pop(
            "conversation",
            [{"role": "user", "content": "Assess the level."}],
        )

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                rows or ["############"] * 10,
                "request-test",
                **reply_kwargs,
            )

        return result, client

    def test_valid_response_is_returned(self):
        result, client = self.execute([
            "The level has a compact central route.\n\n"
            "When the box enters the water-side corridor, which route choice should the "
            "player notice first so we can judge its readability?"
        ])

        self.assertIn("compact central route", result.assistant_message)
        self.assertIn("first moves beside the water", result.assistant_message)
        self.assertIn("judge whether the linkage", result.assistant_message)
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

    def test_plain_reply_normalizes_stages_that_are_mistaken_for_separate_levels(self):
        result, _ = self.execute([
            "这种留一点犹豫空间的做法，很符合第二关该有的手感。"
            "这个判断会影响后面关卡里的目标摆放。"
        ], language="zh-CN")

        self.assertIn("这个版本现在呈现出的手感", result.assistant_message)
        self.assertIn("后续版本里的目标摆放", result.assistant_message)
        self.assertNotIn("第二关", result.assistant_message)
        self.assertNotIn("后面关卡", result.assistant_message)

    def test_plain_chat_cannot_claim_an_unsaved_map_change_is_finished(self):
        result, _ = self.execute([
            "我会让右侧水域贴近推动路线。\n\n改好了，你去试试看。"
        ], language="zh-CN")

        self.assertIn("生成可审查的方案后再由你决定是否采用", result.assistant_message)
        self.assertNotIn("改好了", result.assistant_message)
        self.assertNotIn("去试试看", result.assistant_message)

    def test_plain_chat_cannot_describe_an_uncreated_pending_proposal_as_real(self):
        result, _ = self.execute([
            "好，那我把第7行第6到第8列连成一片水，其余格子不动。"
            "这份提案会保持待审查状态。"
        ], language="zh-CN")

        self.assertIn("还没有实际落到地图上", result.assistant_message)
        self.assertNotIn("会保持待审查状态", result.assistant_message)

    def test_generic_change_authorization_is_not_saved_as_design_intent(self):
        visible, intent, _, _ = llm_client._extract_plain_guidance(
            "我先把方向说清楚。\n<GUIDANCE>INTENT: 我暂时把你的方向理解为：你帮我改。"
            "</GUIDANCE>",
            "zh-CN",
            {},
        )

        self.assertEqual(visible, "我先把方向说清楚。")
        self.assertIsNone(intent)

        _, intent, _, _ = llm_client._extract_plain_guidance(
            "我先把方向说清楚。\n<GUIDANCE>INTENT: 我读到的倾向是，你希望后续设计回应“帮我做”。"
            "</GUIDANCE>",
            "zh-CN",
            {},
        )
        self.assertIsNone(intent)

    def test_plain_discuss_card_can_hold_a_first_person_design_insight(self):
        result, _ = self.execute([
            "这个版本的下半区多了一点回旋空间。\n"
            "<GUIDANCE>DISCUSS: 我更喜欢水边这次留下的路线犹豫，它让第一次推动的选择更有分量。</GUIDANCE>"
        ], language="zh-CN")

        focus = result.guidance["followUpQuestion"]
        self.assertIn("不如先看这一点", focus)
        self.assertIn("第一次推动", focus)
        self.assertIn("第一次推箱", focus)
        self.assertNotIn("？", focus)
        self.assertNotIn("GUIDANCE", result.assistant_message)

    def test_repeated_plain_discuss_card_is_omitted_when_no_new_focus_is_needed(self):
        content = (
            "这个版本的下半区多了一点回旋空间。\n"
            "<GUIDANCE>DISCUSS: 我更喜欢水边留下的路线犹豫，它让第一次推动的选择更有分量。</GUIDANCE>"
        )
        result, _ = self.execute(
            [content],
            language="zh-CN",
            stage_context={
                "recentGuidance": {
                    "discussionFocus": (
                        "我更喜欢水边留下的路线犹豫，它让第一次推动的选择更有分量。"
                    )
                }
            },
        )

        focus = result.guidance["followUpQuestion"]
        self.assertIsNone(focus)

    def test_plain_reply_extracts_intent_card(self):
        result, _ = self.execute([
            "The water now reads as part of the route.\n\n"
            "<GUIDANCE>\n"
            "INTENT: The designer wants the water to affect the route.\n"
            "</GUIDANCE>"
        ])

        self.assertEqual(result.guidance["move"], "clarify_intent")
        self.assertEqual(result.guidance["intentConfidence"], "medium")
        self.assertTrue(result.guidance["intentHypothesis"].startswith("I read your preference"))
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
        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertIsNotNone(result.guidance["proposalOffer"])
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertEqual(result.guidance["uiCues"][0]["type"], "manual_edit")

    def test_guidance_card_policy_keeps_only_the_confirmed_families(self):
        discussion = llm_client._apply_guidance_card_policy({
            "intentHypothesis": "I read your preference as preserving the upper route.",
            "intentConfidence": "medium",
            "followUpQuestion": "Which first push should carry the route judgment?",
            "proposalOffer": None,
            "uiCues": [{"type": "warning", "text": "The box may lose its return route."}],
        })
        self.assertIsNotNone(discussion["intentHypothesis"])
        self.assertIsNotNone(discussion["followUpQuestion"])
        self.assertEqual([cue["type"] for cue in discussion["uiCues"]], ["warning"])

        action = llm_client._apply_guidance_card_policy({
            "intentHypothesis": "This must be removed.",
            "intentConfidence": "medium",
            "followUpQuestion": "This must be removed too?",
            "proposalOffer": {"summary": "Move the target", "rationale": "Change the route"},
            "uiCues": [
                {"type": "manual_edit", "text": "Try the same area in the editor."},
                {"type": "warning", "text": "The box may lose its return route."},
            ],
        })
        self.assertIsNone(action["intentHypothesis"])
        self.assertIsNone(action["followUpQuestion"])
        self.assertEqual(
            [cue["type"] for cue in action["uiCues"]],
            ["manual_edit", "warning"],
        )

    def test_non_opening_replies_do_not_force_a_discussion_card(self):
        result, _ = self.execute(["The route remains readable."])
        self.assertIsNone(result.guidance["followUpQuestion"])

        client = FakeClient(["当然，我们可以先聊点别的。"])
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            off_topic = llm_client.generate_chat_reply(
                [{"role": "user", "content": "这个与地图无关，我们换个话题。"}],
                ["############"] * 10,
                "off-topic-card-test",
                language="zh-CN",
            )
        self.assertIsNone(off_topic.guidance["followUpQuestion"])
        self.assertIsNone(off_topic.guidance["intentHypothesis"])
        self.assertEqual(off_topic.guidance["uiCues"], [])

    def test_proposal_card_distills_body_copy_into_title_and_play_rationale(self):
        copied = (
            "这个想法挺自然的——把其中一个目标点挪到下方空旷区域，确实能直接平衡两边的密度。"
        )
        client = FakeClient([
            copied + "\n<GUIDANCE>PROPOSAL_SUMMARY: " + copied
            + " || PROPOSAL_RATIONALE: 你说得对，上下半区的密度落差确实一眼就能看出来。"
            + "</GUIDANCE>"
        ])
        conversation = [
            {"role": "assistant", "content": "你说得对，上下半区的密度落差确实一眼就能看出来。"},
            {"role": "user", "content": "展开讲讲"},
        ]

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                ["############"] * 10,
                "distilled-proposal-card-test",
                language="zh-CN",
            )

        offer = result.guidance["proposalOffer"]
        self.assertIn("目标点", offer["summary"])
        self.assertIn("下方", offer["summary"])
        self.assertIn("唯一改动方向", offer["rationale"])
        self.assertNotEqual(offer["summary"], copied)
        self.assertNotIn("你说得对", offer["rationale"])
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertEqual(result.guidance["uiCues"][0]["type"], "manual_edit")

    def test_proposal_card_rejects_a_bare_confirmation_as_its_title(self):
        content = (
            "好，那就往旁边挪一格。我倾向于挪右边那个箱子，让左边箱子先有可读的下推空间。\n"
            "<GUIDANCE>PROPOSAL_SUMMARY: 好 || "
            "PROPOSAL_RATIONALE: 让两个箱子的推进顺序更清楚。</GUIDANCE>"
        )
        result, _ = self.execute([content], language="zh-CN")

        offer = result.guidance["proposalOffer"]
        self.assertNotEqual(offer["summary"], "好")
        self.assertIn("箱子", offer["summary"])
        self.assertNotIn("“好”", offer["rationale"])

    def test_malformed_guidance_is_hidden_without_failing_reply(self):
        for reply in (
            "Visible reply.\n<GUIDANCE>\nINTENT: You want a tighter route",
            "Visible reply.\n<GUIDANCE>\nUNKNOWN: hidden\n</GUIDANCE>",
            "Visible reply.\n<GUIDANCE>\nPROPOSAL_SUMMARY: Incomplete\n</GUIDANCE>",
        ):
            with self.subTest(reply=reply):
                result, _ = self.execute([reply])
                self.assertTrue(result.assistant_message.startswith("Visible reply."))
                self.assertIsNone(result.guidance["intentHypothesis"])
                self.assertIsNone(result.guidance["proposalOffer"])
                self.assertIsNone(result.guidance["followUpQuestion"])

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

    def test_plain_prompt_asks_at_real_decision_points_without_a_fixed_template(self):
        messages = llm_client.build_plain_chat_messages(
            [{"role": "user", "content": "Make the choice smaller but consequential."}],
            ["############"] * 10,
        )

        self.assertIn("Actively ask at a real decision point", messages[0]["content"])
        self.assertIn("varying their rhythm and opening", messages[0]["content"])
        self.assertIn("never ask the designer to approve the preference", messages[0]["content"])
        self.assertIn("<GUIDANCE>", messages[0]["content"])
        self.assertIn("recentGuidance", messages[0]["content"])
        self.assertIn("every reply must produce at least one card", messages[0]["content"])
        self.assertIn("never produce four cards", messages[0]["content"])
        self.assertIn("two to four compact paragraphs", messages[0]["content"])
        self.assertIn("Give observations room to breathe", messages[0]["content"])

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

        self.assertTrue(result.assistant_message.startswith(reply))
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
        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertEqual(result.guidance["uiCues"], [])

    def test_explicit_direction_can_keep_a_deeper_concrete_question(self):
        client = FakeClient([
            "That makes the first commitment more legible. When the box enters the "
            "water-side corridor, which route should the player notice first so we can "
            "judge the opening's readability?"
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Keep it compact, but make the first push consequential."}],
                ["############"] * 10,
                "deeper-question-test",
            )

        self.assertIn("first moves beside the water", result.guidance["followUpQuestion"])
        self.assertIn("judge whether the linkage", result.guidance["followUpQuestion"])
        self.assertNotIn("water-side corridor", result.assistant_message.split("\n\n")[0])

    def test_plain_guidance_extracts_warning_and_manual_edit(self):
        result, _ = self.execute([
            "The water can shape the first route decision.\n"
            "<GUIDANCE>WARNING: The box may lose its escape route beside the water after "
            "the first push || MANUAL_EDIT: Try a small experiment around the water edge "
            "and watch whether the route choice becomes clearer</GUIDANCE>"
        ], rows=["############", "#p.s @  t  #"] + ["############"] * 8)

        self.assertEqual(
            [cue["type"] for cue in result.guidance["uiCues"]],
            ["manual_edit"],
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

    def test_human_edit_without_play_evidence_does_not_get_automatic_warning(self):
        self.assertIsNone(llm_client._deterministic_play_warning("en", None))

    def test_contextual_manual_edit_avoids_exact_coordinates(self):
        cue = llm_client._contextual_manual_edit(
            ["############", "#p   @     #", "#    @ s t #"] + ["############"] * 7,
            "en",
        )

        self.assertIn("water edge", cue)
        self.assertIn("watch", cue)
        self.assertNotRegex(cue, r"\b(?:row|column)\s+\d+")

    def test_evidence_grounded_warning_is_extracted_but_aesthetic_opinion_is_not(self):
        rows = ["############", "#p.s @  t  #"] + ["############"] * 8
        warning = llm_client._warning_has_strong_evidence(
            "I notice the box may lose its escape route beside the water after the first push.",
            "en",
            rows,
            {},
            None,
        )
        aesthetic = llm_client._warning_has_strong_evidence(
            "The water looks too plain and the colors feel dull.",
            "en",
            rows,
            {},
            None,
        )

        self.assertTrue(warning)
        self.assertFalse(aesthetic)

    def test_general_route_uncertainty_is_not_promoted_to_warning(self):
        strong = llm_client._warning_has_strong_evidence(
            "I am concerned the route may feel unclear after the first push.",
            "en",
            ["############", "#p.s @  t  #"] + ["############"] * 8,
            {},
            None,
        )

        self.assertFalse(strong)

    def test_play_restart_produces_a_first_person_warning(self):
        warning = llm_client._deterministic_play_warning(
            "zh-CN",
            {"restartCount": 2, "moveCount": 30, "minimumMoves": 20},
        )

        self.assertTrue(warning.startswith("我注意到"))
        self.assertIn("重开了 2 次", warning)

    def test_intent_fallback_varies_naturally_with_context(self):
        first = llm_client._natural_intent_candidate(
            "a route with water",
            "en",
            False,
        )
        second = llm_client._natural_intent_candidate(
            "b route with water",
            "en",
            False,
        )

        self.assertNotEqual(first.split(":", 1)[0], second.split(":", 1)[0])
        self.assertNotIn("designer", (first + second).casefold())
        self.assertNotIn("player wants", (first + second).casefold())

    def test_specific_vivid_question_is_kept_as_blue_card(self):
        result, _ = self.execute([
            "The water edge can make the route legible. When the box enters the corridor "
            "beside the water, which route choice should the player notice first so we can "
            "judge its readability?"
        ])

        self.assertIn("water", result.guidance["followUpQuestion"])
        self.assertNotIn("?", result.assistant_message.split("\n\n", 1)[0])

    def test_consecutive_question_repeating_the_same_judgment_is_suppressed(self):
        client = FakeClient([
            "The route remains focused. When the box enters the water-side corridor, "
            "which route choice should the player notice first so we can judge readability?"
        ])
        conversation = [
            {
                "role": "assistant",
                "content": (
                    "I am looking at the same moment. When the box enters the water-side "
                    "corridor, which route choice should the player notice first so we can "
                    "judge readability?"
                ),
            },
            {"role": "user", "content": "The upper route."},
        ]

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                ["############"] * 10,
                "question-dedup-test",
            )

        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_generic_question_is_removed_when_declarative_body_exists(self):
        result, client = self.execute([
            "Moving the target beside the water would create a route decision. "
            "Does this direction work?"
        ])

        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertNotIn("Does this direction work", result.assistant_message)

    def test_clear_first_person_evaluation_gets_an_intent_card_when_model_omits_one(self):
        client = FakeClient(["我更倾向于让水域真正参与路线，而不是只做背景。"])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "我觉得水域现在只是摆设。"}],
                ["############"] * 10,
                "question-frequency-test",
                language="zh-CN",
            )

        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_user_difficulty_reframe_gets_a_tentative_intent_card(self):
        result, _ = self.execute(
            [
                "我会继续看第一次推箱时的路线判断，避免把难度只理解成增加障碍。"
            ],
            language="zh-CN",
            conversation=[
                {
                    "role": "assistant",
                    "content": "在我看来，这张图目前更像顺着流程完成，而不是被很难的谜题卡住。",
                },
                {"role": "user", "content": "我认为还是太简单了。"},
            ],
        )

        intent = result.guidance["intentHypothesis"]
        self.assertEqual(result.guidance["move"], "clarify_intent")
        self.assertEqual(result.guidance["intentConfidence"], "medium")
        self.assertIsNotNone(intent)
        self.assertIn("你", intent)
        self.assertIn("推", intent)

    def test_tentative_intent_rephrases_an_echoed_water_instruction(self):
        user_message = "我倒是认为得改动水域的形状"
        result, _ = self.execute(
            [
                "我同意，水现在更像装饰而不是路线边界。\n"
                "<GUIDANCE>INTENT: 我暂时把你的方向理解为：我倒是认为得改动水域的形状</GUIDANCE>"
            ],
            language="zh-CN",
            conversation=[
                {"role": "assistant", "content": "我原本认为补一个缺口就够。"},
                {"role": "user", "content": user_message},
            ],
        )

        intent = result.guidance["intentHypothesis"]
        self.assertIsNotNone(intent)
        self.assertNotIn(user_message, intent)
        self.assertIn("水", intent)
        self.assertTrue(any(marker in intent for marker in ("路线", "推进", "绕行", "选择")))
        self.assertNotIn("设计者", intent)

    def test_direction_question_goes_deeper_instead_of_asking_for_approval(self):
        question = llm_client._deterministic_key_question(
            [{"role": "user", "content": "我想让箱子贴着水边推进时更有路线判断。"}],
            "zh-CN",
            ["############"] * 10,
        )

        self.assertIn("第一次", question)
        self.assertIn("路线", question)
        self.assertNotIn("方向如何", question)
        self.assertNotIn("可行吗", question)

    def test_chinese_agreement_is_treated_as_an_explicit_direction(self):
        self.assertTrue(llm_client._latest_user_states_direction([
            {"role": "user", "content": "做点联动吧"},
        ]))

    def test_any_first_person_stance_gets_a_correctable_intent_without_a_forced_discussion_card(self):
        guidance = llm_client._ensure_required_guidance_card(
            {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            [{"role": "user", "content": "我认为中间的水域仍然太像装饰。"}],
            "zh-CN",
            OPERATION_BASE_ROWS,
            False,
            {},
        )
        self.assertIsNotNone(guidance["intentHypothesis"])
        self.assertEqual(guidance["intentConfidence"], "medium")
        self.assertIsNone(guidance["followUpQuestion"])

    def test_stage_one_opening_states_the_small_change_boundary(self):
        body = llm_client._ensure_stage_one_orientation(
            "我先看到水域让中间路线有了分隔。",
            OPERATION_BASE_ROWS,
            "zh-CN",
        )
        self.assertIn("小范围更改、提供可审查的改动内容", body)
        self.assertIn("较大的改动我建议由你亲手试一试", body)

    def test_stage_one_opening_keeps_map_observation_but_compacts_process_guidance(self):
        body = llm_client._ensure_stage_one_orientation(
            "我觉得中间水域让箱子第一次靠近目标时需要重新判断路线。\n\n"
            "你可以先试玩，或者直接在右侧面板里做局部调整。"
            "你可以先从直觉说起；想验证时，右侧的试玩和局部编辑都可以随时接上。",
            OPERATION_BASE_ROWS,
            "zh-CN",
        )

        self.assertIn("中间水域让箱子第一次靠近目标", body)
        self.assertEqual(body.count("你可以先说说你的第一反应或试玩当前关卡"), 1)
        self.assertEqual(body.count("右侧面板进行局部编辑"), 1)

    def test_stage_one_opening_removes_model_written_scope_paragraph(self):
        body = llm_client._ensure_stage_one_orientation(
            "我觉得右下角被水和墙夹住的箱子，会让处理顺序变得更敏感。\n\n"
            "你可以先试玩，或者直接告诉我你对这个布局的第一印象；小调整我可以帮你改，"
            "但大改方向还是由你来定。",
            OPERATION_BASE_ROWS,
            "zh-CN",
        )

        self.assertIn("右下角被水和墙夹住的箱子", body)
        self.assertNotIn("小调整我可以帮你改", body)
        self.assertEqual(body.count("你可以先说说你的第一反应或试玩当前关卡"), 1)

    def test_revision_card_uses_the_concrete_plan_not_a_confirmation_word(self):
        visible = (
            "当然，我会先处理右上角贴水的两个箱子。我的想法是：把中间那格水往左挪一格，"
            "让箱子之间多出一条纵向通道。"
        )

        summary = llm_client._revision_direction_sentence(visible)

        self.assertIn("把中间那格水往左挪一格", summary)
        self.assertNotEqual(summary, "当然")

    def test_water_discussion_focus_is_direct_and_explains_the_observation(self):
        focus = llm_client._deterministic_reply_discussion_focus(
            "把中间水格往左挪一格，让右上角的箱子贴着水边推进。",
            "zh-CN",
        )

        self.assertIn("试玩时，请直接看", focus)
        self.assertIn("先向上绕开", focus)
        self.assertIn("如果还是只有一条很显眼的走法", focus)

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
        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertIsNotNone(result.guidance["proposalOffer"])
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertEqual(result.guidance["uiCues"][0]["type"], "manual_edit")
        self.assertNotIn("？", result.assistant_message)
        self.assertNotIn("吗", result.assistant_message)

    def test_invalid_stage_json_falls_back_to_plain_opening(self):
        client = FakeClient([
            "   ",
            "The water narrows the central route in an interesting way. "
            "When the box enters that corridor, which route should read first?",
        ])

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
                {
                    "stageNumber": 1,
                    "source": "initial",
                    "initialDraftMethod": "description_generation",
                },
            )

        self.assertIn("water narrows", result.assistant_message)
        self.assertNotIn("?", result.assistant_message)
        self.assertIn("play the Stage", result.assistant_message)
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertIn("deterministic solver", result.assessment["solutionSummary"])
        self.assertEqual(len(client.chat.completions.calls), 2)
        self.assertEqual(
            client.chat.completions.calls[0]["response_format"],
            {"type": "json_object"},
        )
        self.assertNotIn("response_format", client.chat.completions.calls[1])

    def test_structured_stage_one_opening_receives_rows_for_scope_normalization(self):
        payload = json.dumps({
            "assistantMessage": "The central water area makes the first route split easy to read.",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a valid route.",
                "difficultyOpinion": "I think the opening is readable.",
                "features": ["Central water"],
                "suggestions": ["Try the opening route."],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        })

        client = FakeClient([payload])
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
                "stage-structured-opening-test",
                {"stageNumber": 1, "source": "initial"},
            )

        self.assertIn("small, reviewable edits", result.assistant_message)
        self.assertEqual(result.attempts_used, 1)

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
        response = revision_plan_payload()
        client = FakeClient([response])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            llm_client.generate_chat_reply(
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please create a reviewable map proposal."},
                ],
                OPERATION_BASE_ROWS,
                "proposal-model-test",
            )

        request = client.chat.completions.calls[0]
        self.assertEqual(request["model"], "deepseek-v4-pro")
        self.assertEqual(request["max_tokens"], llm_client.PROPOSAL_PLAN_MAX_TOKENS)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertIn("semantic RevisionPlan", request["messages"][0]["content"])
        self.assertIn("Do not generate map rows", request["messages"][0]["content"])

    def test_natural_chinese_request_to_help_change_uses_proposal_workflow(self):
        response = revision_plan_payload(
            effect="reshape_water",
            operators=["add_water"],
            focus={"row": 3, "column": 7, "radius": 1},
            preserve=["outer_shell", "player", "boxes", "targets", "unrelated_areas"],
            edit_budget=1,
        )
        client = FakeClient([response])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "assistant", "content": "我建议让水域贴近右侧推动路线。"},
                    {"role": "user", "content": "你帮我改"},
                ],
                OPERATION_BASE_ROWS,
                "natural-chinese-proposal-test",
                language="zh-CN",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(client.chat.completions.calls[0]["model"], "deepseek-v4-pro")
        self.assertEqual(
            client.chat.completions.calls[0]["response_format"],
            {"type": "json_object"},
        )

    def test_confirmed_concrete_chinese_plan_immediately_uses_proposal_workflow(self):
        response = revision_plan_payload(
            effect="reshape_water",
            operators=["add_water"],
            focus={"row": 3, "column": 7, "radius": 1},
            preserve=["outer_shell", "player", "boxes", "targets", "unrelated_areas"],
            edit_budget=1,
        )
        client = FakeClient([response])
        conversation = [
            {
                "role": "assistant",
                "content": "好，那就定三格：把第7行第6到第8列连成一片水，其余格子不动。",
            },
            {"role": "user", "content": "三格可以"},
        ]

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                OPERATION_BASE_ROWS,
                "confirmed-chinese-plan-test",
                language="zh-CN",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(client.chat.completions.calls[0]["model"], "deepseek-v4-pro")
        self.assertIn("第7行第6到第8列", client.chat.completions.calls[0]["messages"][1]["content"])

    def test_help_me_do_inherits_a_confirmed_concrete_chinese_plan(self):
        response = revision_plan_payload(
            effect="reshape_water",
            operators=["add_water"],
            focus={"row": 3, "column": 7, "radius": 1},
            preserve=["outer_shell", "player", "boxes", "targets", "unrelated_areas"],
            edit_budget=1,
        )
        client = FakeClient([response])
        conversation = [
            {
                "role": "assistant",
                "content": "好，那就定三格：把第7行第6到第8列连成一片水，其余格子不动。",
            },
            {"role": "user", "content": "三格可以"},
            {"role": "assistant", "content": "这个方向已经说清楚了。"},
            {"role": "user", "content": "帮我做"},
        ]

        state, brief = llm_client._classify_revision_request(conversation)

        self.assertEqual(state, "authorized")
        self.assertIn("第7行第6到第8列", brief)

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                OPERATION_BASE_ROWS,
                "help-me-do-chinese-plan-test",
                language="zh-CN",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(client.chat.completions.calls[0]["model"], "deepseek-v4-pro")

    def test_map_request_phrases_are_detected_without_matching_design_questions(self):
        positives = (
            "你帮我改",
            "帮我做",
            "你来改吧",
            "按这个思路帮我改一下",
            "请你改一下地图",
            "可以帮我修改吗？",
            "修改",
            "Can you change it?",
            "Go ahead and revise the map.",
        )
        negatives = (
            "你觉得应该怎么改？",
            "如果你来改，你会怎么改？",
            "先说说修改思路",
            "How would you change the route?",
            "How would you revise the map?",
        )

        for message in positives:
            with self.subTest(message=message):
                self.assertTrue(llm_client._requests_complete_map([
                    {"role": "assistant", "content": "把右下目标移近水域，让推动路线形成绕行。"},
                    {"role": "user", "content": message},
                ]))
        for message in negatives:
            with self.subTest(message=message):
                self.assertFalse(llm_client._requests_complete_map([
                    {"role": "user", "content": message},
                ]))

        for message in ("可以帮我修改吗？", "修改", "帮我做", "Can you change it?"):
            with self.subTest(message=f"no-basis:{message}"):
                state, brief = llm_client._classify_revision_request([
                    {"role": "user", "content": message},
                ])
                self.assertEqual(state, "needs_direction")
                self.assertIsNone(brief)

        state, brief = llm_client._classify_revision_request([
            {
                "role": "assistant",
                "content": "我觉得水域让下半区的路线更有犹豫感。",
            },
            {"role": "user", "content": "修改"},
        ])
        self.assertEqual(state, "needs_direction")
        self.assertIsNone(brief)

        state, brief = llm_client._classify_revision_request([
            {"role": "user", "content": "换个话题，帮我修改一首与地图无关的诗。"},
        ])
        self.assertEqual(state, "not_request")
        self.assertIsNone(brief)

    def test_unclear_revision_request_is_manual_edit_only(self):
        result, _ = self.execute(
            [
                "I can help, but I still need to understand the area you mean.\n"
                "<GUIDANCE>DISCUSS: Which route should change? || "
                "INTENT: I think you may want a more deliberate route. || "
                "PROPOSAL_SUMMARY: Move the target || "
                "PROPOSAL_RATIONALE: Make the route less direct.</GUIDANCE>"
            ],
            conversation=[{"role": "user", "content": "Can you change it?"}],
        )

        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertEqual(
            [cue["type"] for cue in result.guidance["uiCues"]],
            ["manual_edit"],
        )
        self.assertIn("I would be guessing on your behalf", result.assistant_message)

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
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please create a reviewable map proposal."},
                ],
                ["############"] * 10,
                "proposal-required-test",
            )

        self.assertEqual(raised.exception.code, "MODEL_RESPONSE_INVALID")
        self.assertEqual(raised.exception.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)
        retry_prompt = client.chat.completions.calls[1]["messages"][0]["content"]
        self.assertIn("previous RevisionPlan was rejected", retry_prompt)
        self.assertIn("Do not return map rows or tile operations", retry_prompt)

    def test_structurally_invalid_revision_plan_retries_with_pro_for_correction(self):
        invalid = json.dumps({"strategies": []})
        client = FakeClient([invalid, revision_plan_payload()])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please revise the map."},
                ],
                OPERATION_BASE_ROWS,
                "proposal-pro-correction-test",
            )

        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(
            [call["model"] for call in client.chat.completions.calls],
            ["deepseek-v4-pro", "deepseek-v4-pro"],
        )
        self.assertIn(
            "strategies must contain one to three items",
            client.chat.completions.calls[1]["messages"][0]["content"],
        )

    def test_zero_candidate_revision_plan_retries_with_safe_search_feedback(self):
        no_expandable_cells = revision_plan_payload(
            effect="adjust_internal_walls",
            operators=["remove_wall"],
            focus={"row": 5, "column": 5, "radius": 1},
            preserve=["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
            edit_budget=2,
        )
        client = FakeClient([no_expandable_cells, revision_plan_payload()])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Move the target and revise the map."}],
                OPERATION_BASE_ROWS,
                "zero-candidate-plan-correction-test",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)
        correction_prompt = client.chat.completions.calls[1]["messages"][0]["content"]
        self.assertIn("could not produce any legal local edit", correction_prompt)
        self.assertIn("Do not return map rows or tile operations", correction_prompt)

    def test_revision_plan_search_returns_only_selected_verified_map(self):
        client = FakeClient([revision_plan_payload()])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "user", "content": "Move the target one cell to the right and revise the map."},
                ],
                OPERATION_BASE_ROWS,
                "candidate-selection-test",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(result.revision_plan["strategies"][0]["effect"], "relocate_target")
        self.assertGreater(result.proposal_diagnostics["validCandidates"], 0)
        self.assertEqual(result.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_confirmed_assistant_direction_becomes_a_hard_search_requirement(self):
        conversation = [
            {
                "role": "assistant",
                "content": "我会把左上角的目标点往右下方挪，让它成为中期目标。",
            },
            {
                "role": "user",
                "content": "请根据这个方向生成一份可供审查的地图提案。",
            },
        ]
        requirement = llm_client._authorized_movement_requirement(conversation)

        self.assertEqual(
            requirement,
            {"operator": "move_target", "direction": "lower_right"},
        )
        messages = llm_client._build_revision_plan_messages(
            conversation,
            OPERATION_BASE_ROWS,
            "zh-CN",
            {"authorizedRevisionBrief": "拉开左上角箱子与目标点的距离"},
            requirement,
        )
        self.assertIn(
            "Hard verified movement requirement: the target must move lower right",
            messages[1]["content"],
        )

    def test_direct_user_direction_overrides_an_older_assistant_direction(self):
        requirement = llm_client._authorized_movement_requirement([
            {
                "role": "assistant",
                "content": "Move the target to the left to open the route.",
            },
            {
                "role": "user",
                "content": "Please move the target to the right and generate a reviewable proposal.",
            },
        ])
        self.assertEqual(
            requirement,
            {"operator": "move_target", "direction": "right"},
        )

    def test_revision_plan_protocol_never_asks_model_for_tile_operations(self):
        client = FakeClient([revision_plan_payload()])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "user", "content": "Move the target one cell to the right and revise the map."},
                ],
                OPERATION_BASE_ROWS,
                "server-source-operation-test",
            )

        self.assertIsNotNone(result.proposed_rows)
        system_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
        self.assertIn("Do not generate map rows", system_prompt)
        self.assertIn("Do not generate", system_prompt)
        self.assertNotIn('"to":"#"', system_prompt)

    def test_verbose_brief_with_multiple_regions_does_not_require_every_region(self):
        rows = llm_client._apply_map_operations(
            OPERATION_BASE_ROWS,
            [
                {"row": 6, "column": 7, "to": "."},
                {"row": 6, "column": 8, "to": "t"},
            ],
            "Keep the left side connected to the right side and make one local change.",
        )

        self.assertEqual(rows[5], "#...s..t...#")

    def test_legacy_operation_normalizer_drops_noops_and_identical_duplicates(self):
        rows = llm_client._apply_map_operations(
            OPERATION_BASE_ROWS,
            [
                {"row": 2, "column": 2, "to": "."},
                {"row": 6, "column": 7, "to": "."},
                {"row": 6, "column": 8, "to": "t"},
                {"row": 6, "column": 8, "to": "t"},
            ],
            "Move the target one cell to the right.",
        )
        self.assertEqual(rows[5], "#...s..t...#")

        with self.assertRaisesRegex(ValueError, "real tile change"):
            llm_client._apply_map_operations(
                OPERATION_BASE_ROWS,
                [{"row": 2, "column": 2, "to": "."}],
                "Keep the map unchanged.",
            )

    def test_semantic_search_keeps_outer_shell_immutable(self):
        client = FakeClient([revision_plan_payload(
            effect="narrow_route",
            operators=["add_wall"],
            focus={"row": 1, "column": 1, "radius": 3},
            preserve=["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
            edit_budget=2,
        )])
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Narrow the upper-left route and revise the map."}],
                OPERATION_BASE_ROWS,
                "candidate-safety-test",
            )
        self.assertEqual(result.proposed_rows[0], OPERATION_BASE_ROWS[0])
        self.assertEqual(result.proposed_rows[-1], OPERATION_BASE_ROWS[-1])

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

    def test_revision_plan_timeout_uses_two_attempts_and_reserves_search_time(self):
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
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please create a map proposal."},
                ],
                ["############"] * 10,
                "timeout-test",
            )

        self.assertEqual(llm_client.CHAT_TIMEOUT_SECONDS, 60.0)
        self.assertEqual(llm_client.PRIMARY_ATTEMPT_TIMEOUT_SECONDS, 40.0)
        self.assertEqual(llm_client.CHAT_MAX_ATTEMPTS, 2)
        self.assertEqual(llm_client.PROPOSAL_GENERATION_ATTEMPTS, 2)
        self.assertEqual(llm_client.PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS, 18.0)
        self.assertEqual(llm_client.PROPOSAL_PLAN_RETRY_TIMEOUT_SECONDS, 8.0)
        self.assertEqual(llm_client.PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS, 26.0)
        self.assertEqual(llm_client.PROPOSAL_SEARCH_DEADLINE_SECONDS, 55.0)
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

    def test_discussion_card_is_not_repeated_in_the_saved_assistant_body(self):
        focus = "我会留意水边第一次推进是否真的改变了路线判断。"
        message = (
            "水域现在贴近推进路线。\n\n"
            f"{focus}\n\n{focus}"
        )
        composed = llm_client._compose_assistant_message(
            message,
            {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": focus,
                "proposalOffer": None,
                "uiCues": [],
            },
            "zh-CN",
        )

        self.assertEqual(composed.count(focus), 1)
        self.assertIn("水域现在贴近推进路线", composed)

    def test_recent_discussion_focus_history_suppresses_an_older_repeat(self):
        focus = "我会留意水边第一次推进是否真的改变了路线判断。"
        content = (
            "水域让右侧路线有了新的转折。\n"
            f"<GUIDANCE>DISCUSS: {focus}</GUIDANCE>"
        )

        extracted = llm_client._extract_plain_discussion_focus(
            content,
            "zh-CN",
            stage_context={
                "recentGuidance": {
                    "discussionFocusHistory": [
                        "我会看玩家进入调整区域的第一步会不会重新判断顺序。",
                        focus,
                    ],
                },
            },
        )

        self.assertIsNone(extracted)

    def test_default_discussion_insights_use_play_moments_not_report_lead_ins(self):
        focuses = {
            llm_client._friendly_default_discussion_focus(
                ["############"] * 10,
                "zh-CN",
                f"水边路线 {index}",
                [],
            )
            for index in range(12)
        }

        self.assertGreaterEqual(len(focuses), 4)
        for focus in focuses:
            self.assertNotIn("我在意的是", focus)
            self.assertNotIn("我想把注意力放在", focus)
            self.assertNotIn("比单看格子摆放更能说明", focus)
            self.assertTrue(any(marker in focus for marker in ("第一次", "水边", "调整区域")))

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
        self.assertIn("focus our discussion", result[4]["followUpQuestion"])
        self.assertIn("Would you like to preserve that split?", result[4]["followUpQuestion"])
        self.assertIn("next step", result[4]["followUpQuestion"])

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

    def test_complete_proposal_is_not_described_as_an_already_saved_edit(self):
        payload = {
            "assistantMessage": "改好了，你先去试试看。",
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
            "modificationSummary": "调整路线。",
        }

        result = llm_client.validate_chat_response(payload, language="zh-CN")

        self.assertIn("可审查的修改提案", result[0])
        self.assertIn("决定是否接受", result[0])
        self.assertNotIn("改好了", result[0])

    def test_proposal_language_stays_pending_until_acceptance(self):
        normalized = llm_client._normalize_change_claims_for_proposal(
            "好，我按刚才说的方向改：把水域收拢。改完的版本你可以先看看。",
            "zh-CN",
            True,
        )

        self.assertIn("我按刚才说的方向做了一份修改提案", normalized)
        self.assertIn("这份待审查提案", normalized)
        self.assertNotIn("改完的版本", normalized)

    def test_invalid_proposal_uses_fallback_then_fails(self):
        client = FakeClient([revision_plan_payload()])
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
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please draft that revision."},
                ],
                OPERATION_BASE_ROWS,
                "invalid-proposal-test",
                proposal_validator=reject_proposal,
            )

        self.assertEqual(raised.exception.code, "PROPOSAL_SEARCH_EXHAUSTED")
        self.assertEqual(raised.exception.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertGreater(len(validated_rows), 0)
        self.assertIn("constructedCandidates", raised.exception.proposal_diagnostics)

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
                "It sounds to me like you want to make the route feel risky.",
            "The player seems to want a tighter opening.":
                "I get the sense that you may want a tighter opening.",
            "You want the second push to be surprising.":
                "It sounds to me like you want the second push to be surprising.",
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
            "设计者想要让路线更紧张。": "听起来你更想要让路线更紧张。",
            "玩家希望突出推动顺序。": "听起来你更想要突出推动顺序。",
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
            "I read your preference as wanting a more deliberate opening.",
        )

    def test_structured_discussion_card_accepts_a_concrete_first_person_insight(self):
        payload = {
            "assistantMessage": "The lower route now has more breathing room.",
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": (
                    "I prefer the hesitation beside the water because it gives the first "
                    "route choice more weight."
                ),
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, language="en")

        self.assertIn("I prefer", result[4]["followUpQuestion"])
        self.assertNotIn("?", result[4]["followUpQuestion"])

    def test_structured_fields_normalize_false_level_progression(self):
        payload = {
            "assistantMessage": "This feels like the second level, not the first level.",
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
            "modificationSummary": "Tune the next level.",
        }

        result = llm_client.validate_chat_response(payload, language="en")

        self.assertEqual(result[0], "This feels like this Stage, not this Stage.")
        self.assertEqual(result[3], "Tune the next version.")

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

    def test_stage_one_opening_removes_question_and_adds_natural_orientation(self):
        payload = {
            "assistantMessage": (
                "I notice the water makes the lower route feel more deliberate. "
                "When the box reaches the edge, what should stand out first?"
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "When the box reaches the edge, what should stand out first?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "In my view, the opening is readable.",
                "features": ["Lower route"],
                "suggestions": ["Try the opening"],
                "satisfactionQuestion": "When the box reaches the edge, what should stand out first?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="en",
            stage_context={"stageNumber": 1},
        )

        self.assertNotIn("?", result[0])
        self.assertIn("play", result[0].casefold())
        self.assertIn("edit", result[0].casefold())
        self.assertIsNone(result[4]["followUpQuestion"])
        self.assertIsNone(result[1]["satisfactionQuestion"])

    def test_later_stage_opening_keeps_a_useful_question(self):
        payload = {
            "assistantMessage": "I notice the water makes the lower route feel deliberate.",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "What changes in your reading when the box enters the water-side corridor?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "In my view, the opening is readable.",
                "features": ["Lower route"],
                "suggestions": ["Discuss route readability"],
                "satisfactionQuestion": "What changes in your reading when the box enters the water-side corridor?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="en",
            stage_context={"stageNumber": 2},
        )

        self.assertIn("water-side corridor", result[4]["followUpQuestion"])

    def test_verified_human_edit_opening_adds_a_concrete_discussion_question(self):
        payload = {
            "assistantMessage": "我更喜欢这次水域和通路之间留下的回旋空间。",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "求解器找到了可行路线。",
                "difficultyOpinion": "在我看来，新的路线更有选择感。",
                "features": ["水边通路"],
                "suggestions": ["观察第一次推动"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        }
        context = {
            "stageNumber": 2,
            "source": "human_edit",
            "changeSummary": {"components": ["water", "internalWalls", "targets"]},
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="zh-CN",
            stage_context=context,
        )

        question = result[4]["followUpQuestion"]
        self.assertIn("水域边缘", question)
        self.assertIn("路线选择", question)
        self.assertEqual(result[1]["satisfactionQuestion"], question)

    def test_later_turn_in_stage_one_may_still_ask_a_concrete_question(self):
        client = FakeClient(["我更倾向于让水域真正参与路线，而不是只做背景。"])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "我觉得水域现在只是摆设。"}],
                ["############"] * 10,
                "stage-one-later-turn-question-test",
                language="zh-CN",
                stage_context={"stageNumber": 1, "source": "initial"},
            )

        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["followUpQuestion"])

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

    def test_translation_normalizes_false_multi_level_language(self):
        source = [{
            "turnId": "turn-level",
            "body": "This version has more route choice.",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "uiCueTexts": [],
            "proposalSummary": None,
        }]
        payload = {"translations": [{
            "turnId": "turn-level",
            "body": "这很符合第二关该有的手感，也会影响后面关卡。",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "uiCueTexts": [],
            "proposalSummary": None,
        }]}

        translated = llm_client.validate_translation_response(payload, source)[0]["body"]

        self.assertIn("这个版本现在呈现出的手感", translated)
        self.assertIn("后续版本", translated)
        self.assertNotIn("第二关", translated)


if __name__ == "__main__":
    unittest.main()
