"""Offline prompt coverage and real validation boundaries; no model-quality claims."""
import asyncio
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_client as llm
from level_validation import build_stage_snapshot
from reply_reliability import OptionalRepairBudget


ROWS = ["############", "#..........#", "#..........#", "#..........#", "#...p......#",
        "#...s.t....#", "#..........#", "#..........#", "#..........#", "############"]
BODY = ("I would separate a possible route from a compulsory route. One verified solution "
        "does not earn a certificate saying every solution must follow it. That stronger "
        "claim needs more evidence, so I would keep it tentative.")


def completion(payload):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])


class ReplyStyleTests(unittest.TestCase):
    def test_effective_prompts_cover_languages_routes_openings_and_clarification_returns(self):
        contexts = [({}, False), ({}, True), ({"stageNumber": 2, "source": "human_edit"}, True),
            ({"revisionRouting": "needs_clarification", "proposalClarification": {"questionKey": "focus"}}, False),
            ({"revisionRouting": "needs_clarification", "proposalClarification": {"questionKey": "focus", "optionPolicyVersion": llm.proposal_options.VERSION}}, False)]
        for language in ("en", "zh-CN"):
            for context, opening in contexts:
                for structured in (False, True):
                    with self.subTest(language=language, context=context, structured=structured):
                        kwargs = dict(language=language, stage_context=copy.deepcopy(context))
                        if structured:
                            messages = llm.build_chat_messages([], ROWS, assessment_only=opening, **kwargs)
                        else:
                            messages = llm.build_plain_chat_messages([], ROWS, stage_opening=opening,
                                validation_mode="route_discussion", **kwargs)
                        prompt = messages[0]["content"]
                        self.assertEqual(prompt.count("Visible expression:"), 1)
                        self.assertIn("Humor is optional", prompt)
                        self.assertIn("humor never exempts a claim from evidence checks", prompt)
                        self.assertIn("A simple difficulty question needs a judgment and its strongest reason", prompt)
                        self.assertIn("a complex question may need depth", prompt)
                        self.assertIn("without Markdown emphasis markers or headings", prompt)
                        self.assertNotIn("plus one or two concrete first-person reflection sentences", prompt)
                        self.assertNotIn("plus a concrete first-person reflection", prompt)
                        if context.get("proposalClarification"):
                            self.assertIn("Three questions are a ceiling, not a required sequence", prompt)
                            self.assertIn("An explanation request is not a choice", prompt)
                            self.assertIn("options carry the treatments, effects and trade-offs", prompt)
                        self.assertNotIn("seven to ten declarative sentences", prompt)
                        self.assertNotIn("2-4 paragraphs with 2-4 sentences", prompt)
                        if opening:
                            self.assertIn("keep any humor subtle", prompt)
                            self.assertIn("Stage 1 closing", prompt)
                        elif context:
                            self.assertIn("only the body may be lightly playful", prompt)
                            self.assertIn("neutral and equally respectful", prompt)
                            if not structured and context["proposalClarification"].get("optionPolicyVersion"):
                                self.assertIn("at most THREE", prompt)
                                self.assertIn("at most 240 characters", prompt)

    def test_modifier_and_planner_do_not_receive_the_visible_persona(self):
        for messages in (
            llm._build_map_operation_messages({}, ROWS, "en", {}),
            llm._build_revision_plan_messages([], ROWS, "en", {}),
        ):
            prompt = messages[0]["content"]
            self.assertNotIn("Visible expression:", prompt)
            self.assertNotIn("self-directed wit", prompt)

    def test_manual_edit_pair_uses_restrained_voice_without_changing_frozen_verdict(self):
        result = SimpleNamespace(secondary_execution=SimpleNamespace(guidance={}))
        with patch.object(llm, "_llm_credentials", return_value=("fake", "fake")), \
                patch.object(llm, "_classify_manual_edit_conflict", return_value={"verdict": "no_conflict"}), \
                patch.object(llm, "_request_completion", new_callable=AsyncMock, return_value=completion({})) as request, \
                patch.object(llm, "_validate_manual_edit_pair_payload", return_value=result):
            self.assertIs(llm._generate_manual_edit_assessment_pair([], ROWS, "en",
                {"solvable": True}, {}, "style", {"source": "human_edit", "stageNumber": 2}), result)
        prompt = request.call_args.args[3][0]["content"]
        self.assertIn("use restrained warmth", prompt)
        self.assertIn("do not use humor to persuade", prompt)
        self.assertIn("conflict must be null", prompt)
        self.assertIn("never produce WARNING", prompt)

    def test_translation_preserves_existing_tone_without_adding_jokes_or_snapshot(self):
        for language in ("en", "zh-CN"):
            with self.subTest(language=language), \
                    patch.object(llm, "_llm_credentials", return_value=("fake", "fake")), \
                    patch.object(llm, "_translate_with_model_fallback", new_callable=AsyncMock,
                        return_value="translated") as translate:
                llm.translate_turns([{"turnId": "t1", "body": BODY, "stageRows": ROWS,
                    "entityBindings": {}, "guidance": {}}], language, "style")
            messages = translate.call_args.kwargs["messages"]
            self.assertIn("do not add jokes, intensify conclusions", messages[0]["content"])
            self.assertIn("relative appeal of options", messages[0]["content"])
            source = json.loads(messages[1]["content"])["items"][0]
            self.assertEqual(source["body"], BODY)
            self.assertNotIn("stageRows", source)
            self.assertNotIn("entityBindings", source)
            self.assertNotIn("Visible expression:", messages[0]["content"])

    def test_body_fault_repair_keeps_locked_card_claims_and_natural_voice(self):
        claims = [{"normalizedMeaning": "The route should be clearer."}]
        original = copy.deepcopy(claims)
        with patch.object(llm, "_request_completion", new_callable=AsyncMock,
                return_value=completion({"bodyParagraphs": [BODY]})) as request:
            body, card = asyncio.run(llm._repair_intent_components_async(api_key="fake", base_url="fake",
                user_text="Make the route clearer.", body="Unsupported claim.", card_text="Locked card.",
                claims=claims, issues=["Remove the unsupported claim."], repair_body=True, repair_card=False,
                map_facts="{}", language="en", request_id="style", deadline=time.monotonic() + 30))
        self.assertEqual(body, BODY)
        self.assertEqual(card, "Locked card.")
        self.assertEqual(claims, original)
        request.assert_awaited_once()
        prompt = request.call_args.args[3][0]["content"]
        self.assertIn("including harmless existing wit", prompt)
        self.assertIn("Repair only the specified factual or semantic defect", prompt)
        self.assertIn('"normalizedMeaning": "The route should be clearer."', prompt)
        self.assertEqual(request.call_args.kwargs["task"], "intent_body_repair")

    def test_optional_card_failure_does_not_discard_reliable_wit(self):
        checks = []
        with patch.object(llm, "_repair_intent_components_async", new_callable=AsyncMock,
                side_effect=ValueError("Injected card failure")):
            result = asyncio.run(llm._repair_reviewed_intent_presentation(
                repair_body=False, repair_card=True, body=BODY, card_text="bad",
                deadline=time.monotonic() + 60, optional_budget=OptionalRepairBudget(time.monotonic() + 60),
                card_required=False, component_checks=checks))
        self.assertEqual(result, (BODY, ""))
        self.assertEqual(checks[0].status, "omitted")

    def test_quality_only_review_does_not_force_another_generation(self):
        decision = {"classification": "none", "claims": [], "cardText": ""}
        text = BODY + "\n<GUIDANCE>INTENT_DECISION: " + json.dumps(decision) + "</GUIDANCE>"
        context = {"stageSnapshot": build_stage_snapshot(ROWS, version_id="v1")}
        review = {**decision, "bodyValid": True, "cardTextValid": True, "relation": "none",
            "issues": ["The reply could be longer and more playful."], "reviewedActiveClaims": [],
            "conflictingHypothesisIds": [], "explanation": None}
        messages = llm.build_plain_chat_messages([{"role": "user",
            "content": "Does one solution prove all solutions follow that route?"}], ROWS, stage_context=context)
        with patch.object(llm, "_request_completion", new_callable=AsyncMock,
                return_value=completion(text)) as request, \
                patch.object(llm, "_review_ordinary_intent_with_recovery", new_callable=AsyncMock,
                    return_value=review):
            result = asyncio.run(llm._generate_plain_with_model_fallback(api_key="fake", base_url="fake",
                models=[llm.KIMI_MODEL] * 3, messages=messages, request_id="style", task="chat", language="en",
                solver_metrics={}, rows=ROWS, play_summary={}, stage_opening=False, stage_context=context,
                guidance_mode="none", started_at=time.monotonic()))
        self.assertIn("certificate", result.assistant_message)
        self.assertIsNone(result.proposed_rows)
        request.assert_awaited_once()

    def test_figurative_coordinates_and_route_links_have_no_validation_exemption(self):
        reliable = "I would keep that judgment tentative; the route has not earned a compulsory-route certificate."
        for false_claim in ("B1 is at (2, 2), enjoying its holiday.", "B1 is beside water, taking a bath."):
            with self.subTest(claim=false_claim):
                body, removed = llm._strip_invalid_grounding_sentences(false_claim + " " + reliable, ROWS)
                self.assertTrue(removed)
                self.assertNotIn(false_claim, body)
                self.assertIn("certificate", body)
        text = "B1 to T1 is the red-carpet route."
        links = llm._filter_coordinate_links([{"text": text, "from": [2, 2], "to": [6, 7]}], text, ROWS)
        self.assertEqual(links, [])

    def test_unsaved_success_claim_is_still_corrected_even_with_a_joke(self):
        body = llm._normalize_unsaved_change_claims("I have changed the map. The confetti can wait.", "en")
        self.assertNotIn("I have changed the map", body)
        self.assertIn("I have not saved a map change", body)
        self.assertIn("confetti", body)

    def test_stage_one_fixed_witty_closing_is_stable_and_read_time_compatible(self):
        cases = [
            ("zh-CN", "我会先观察这张地图的推箱顺序。", "大改由你掌舵",
                "你可以先说说你的第一反应或试玩当前关卡，之后不满意的话，你可以指出来，我们一起探讨商量然后制定方案，"
                "或者你自己在右侧面板进行局部编辑；我只能协助进行小范围更改、提供可审查的改动内容以及帮助你梳理思路，"
                "较大的改动我建议由你亲手试一试。"),
            ("en", "I would inspect the push order first.", "you steer any broad rebuild",
                "You can share a first reaction or play the Stage; I support only small, "
                "reviewable edits and thinking through them, while a broad rebuild stays designer-led."),
        ]
        for language, observation, marker, legacy in cases:
            with self.subTest(language=language):
                body = llm._ensure_stage_one_orientation(observation, ROWS, language)
                closing = body.split("\n\n")[-1]
                self.assertIn(marker, closing)
                self.assertIn(observation, body)
                self.assertEqual(llm._ensure_stage_one_orientation(body, ROWS, language), body)
                repaired = llm._ensure_stage_one_orientation(observation + "\n\n" + legacy, ROWS, language)
                self.assertEqual(repaired, body)
                displayed = llm._repair_stage_one_opening_display(repaired, ROWS, language)
                self.assertEqual(displayed.count(closing), 1)
                self.assertTrue(displayed.endswith(closing))

    def test_distinct_reflections_preserve_all_views_and_paragraphs(self):
        cases = {
            "en": "I feel the opening is calm.\n\nI prefer room to experiment. My worry is the late commitment.\n\nI notice a useful visual contrast.",
            "zh-CN": "我觉得开头很轻松。\n\n我喜欢留出试错的余地。我会担心最后一步的承诺。\n\n对我来说，这种视觉变化很有趣。",
        }
        for language, body in cases.items():
            with self.subTest(language=language):
                self.assertGreater(len(llm._personal_reflection_sentences(body)), 2)
                recovered, diagnostics = llm._recover_overlong_personal_reflection(body, language=language)
                self.assertEqual(recovered, body)
                self.assertFalse(diagnostics["changed"])
                self.assertEqual(diagnostics["reflectionDroppedSentenceCount"], 0)

    def test_duplicate_reflections_are_removed_without_truncating_later_views(self):
        repeated = "I feel the opening is calm."
        later = "My worry is the late commitment."
        body = f"{repeated} I prefer room to experiment. {repeated} {later}"
        recovered, diagnostics = llm._recover_overlong_personal_reflection(body)
        self.assertEqual(recovered.count(repeated), 1)
        self.assertIn(later, recovered)
        self.assertEqual(diagnostics["reflectionDroppedSentenceCount"], 1)

    def test_short_manual_review_uses_real_validation_and_rejects_invalid_content(self):
        context = {"stageNumber": 2, "source": "human_edit", "evaluatorDesignContext": {}}
        solver = {"solvable": True, "solutionSteps": 24, "solutionPushes": 6}
        evidence = llm._manual_edit_review_evidence(context, solver, {})
        payload = {"openingMessage": "The saved Stage remains solvable. I would inspect the first push.",
            "assessment": {"solutionSummary": "The Stage is solvable.", "difficultyOpinion": "A calm opening.",
                "features": [], "suggestions": [], "satisfactionQuestion": None},
            "reviewMessage": "Still solvable.", "conflict": None}

        def validate(candidate, language="en"):
            return llm._validate_manual_edit_pair_payload(candidate, ROWS, language, solver,
                context, evidence, {"verdict": "no_conflict"}, "short-review", 1, "offline", 0)

        result = validate(payload)
        self.assertEqual(result.secondary_execution.assistant_message, "Still solvable.")
        chinese = {**payload, "openingMessage": "已保存的地图可解，我会先观察推箱顺序。",
            "assessment": {"solutionSummary": "地图可解。", "difficultyOpinion": "开头较温和。",
                "features": [], "suggestions": [], "satisfactionQuestion": None},
            "reviewMessage": "修改后的地图仍然可解。"}
        self.assertEqual(validate(chinese, "zh-CN").secondary_execution.assistant_message, chinese["reviewMessage"])
        for invalid in ("", "   ", "B1 is at (2, 2).", "Your hidden intention is speed."):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate({**payload, "reviewMessage": invalid})

    def test_retry_prompts_preserve_relevance_without_requiring_detailed_analysis(self):
        for feedback in ("token limit", "route reasoning is too detailed"):
            messages = [{"role": "system", "content": "Original task."}]
            repaired = llm._plain_messages_with_validation_feedback(messages, feedback)
            self.assertIn("needed to answer the question", repaired[0]["content"])
            self.assertNotIn("keeps the detailed design analysis", repaired[0]["content"])
            self.assertEqual(messages[0]["content"], "Original task.")
        structured = llm._messages_with_validation_feedback(messages, "token limit", task="chat")
        self.assertNotIn("Preserve the detailed map/design analysis", structured[0]["content"])


if __name__ == "__main__":
    unittest.main()
