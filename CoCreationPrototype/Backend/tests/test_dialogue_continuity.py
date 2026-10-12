"""Synthetic conversation and component failures; no live model requests."""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from openai import APIStatusError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_client as llm
from patch_fixtures import patch_fixture
import test_llm_client as llm_tests
from conversation_evidence import build_dialogue_evidence, with_latest
from level_validation import build_stage_snapshot
from proposal_understanding import designer_sources, validate_topic
from reply_reliability import OptionalRepairBudget

ROWS = llm_tests.OPERATION_BASE_ROWS


def completion(value):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
        content=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)))])


def interpretation(latest, topic=None):
    return {"acts": ["revision_request"], "elements": ["unknown"], "evidenceSpan": latest,
            "directionSufficient": False, "mapRelated": True, "changes": [],
            "proposalUnderstanding": topic}


def visual_topic():
    return {"aspect": "visual", "goals": [{"statement": "改善外观", "sourceTurnId": "u1", "evidenceSpan": "不好看"}],
            "editScope": [], "focus": [], "preserve": [], "sufficient": False, "nextQuestionDimension": "edit_scope"}


class DialogueEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_stage_snapshot(ROWS, version_id="v1")

    def test_window_pins_topic_sources_without_other_stages(self):
        turns = [{"id": f"u{i}", "role": "user", "content": f"wording {i}",
                  "version_id": "v1", "sequence_number": i} for i in range(35)]
        turns.append({"id": "other", "role": "user", "content": "other map", "version_id": "v2"})
        evidence = build_dialogue_evidence(turns, "v1", {"userTurns": [{"id": "u0"}]})
        sources = designer_sources({"dialogueEvidence": evidence})
        self.assertEqual(len(sources), 25)
        self.assertIn("u0", sources)
        self.assertNotIn("other", sources)
        self.assertNotIn("u1", sources)

    def test_earlier_evaluation_is_real_evidence_before_topic_exists(self):
        latest = "那你觉得可以怎么改"
        evidence = build_dialogue_evidence([{"id": "u1", "role": "user", "content": "不好看"}], "v1")
        context = {"dialogueEvidence": with_latest(evidence, latest)}
        request = AsyncMock(return_value=completion(interpretation(latest, visual_topic())))
        with patch.object(llm, "_llm_credentials", return_value=("fake", "fake")), patch.object(llm, "_request_completion", request):
            result = llm.classify_turn_understanding([
                {"role": "user", "content": "不好看"}, {"role": "user", "content": latest}], self.snapshot, "r", proposal_context=context)
        self.assertEqual(result["proposalUnderstanding"]["goals"][0]["sourceTurnId"], "u1")
        sent = json.loads(request.call_args.args[3][1]["content"])
        self.assertIn({"id": "u1", "content": "不好看"}, sent["designerTurns"])
        self.assertEqual(request.await_count, 1)

    def test_bad_optional_topic_does_not_block_ordinary_understanding(self):
        latest = "不好看"
        candidate = {**interpretation(latest, {"broken": True}), "acts": ["evaluation"]}
        request = AsyncMock(return_value=completion(candidate))
        with patch.object(llm, "_llm_credentials", return_value=("fake", "fake")), patch.object(llm, "_request_completion", request):
            result = llm.classify_turn_understanding([{"role": "user", "content": latest}], self.snapshot, "r")
        self.assertIsNone(result["proposalUnderstanding"])
        self.assertEqual(result["acts"], ["evaluation"])

    def test_topic_repair_locks_valid_basic_understanding(self):
        latest = "那你觉得可以怎么改"
        context = {"dialogueEvidence": with_latest(build_dialogue_evidence([
            {"id": "u1", "role": "user", "content": "不好看"}], "v1"), latest)}
        wrong = visual_topic()
        wrong["goals"][0]["sourceTurnId"] = "unknown"
        async def request_value(*args, **kwargs):
            if kwargs["task"] == "turn_understanding":
                return completion(interpretation(latest, wrong))
            return completion(patch_fixture(args[3][-1]["content"], {"understanding": {"proposalUnderstanding": visual_topic()}}))
        request = AsyncMock(side_effect=request_value)
        with patch.object(llm, "_llm_credentials", return_value=("fake", "fake")), patch.object(llm, "_request_completion", request):
            result = llm.classify_turn_understanding([{"role": "user", "content": latest}], self.snapshot, "r", proposal_context=context)
        self.assertEqual(result["acts"], ["revision_request"])
        self.assertEqual(request.await_count, 2)

    def test_assistant_and_missing_turn_cannot_be_goal_evidence(self):
        topic = visual_topic()
        topic["goals"][0]["sourceTurnId"] = "a1"
        evidence = build_dialogue_evidence([{"id": "a1", "role": "assistant", "content": "不好看"}], "v1")
        with self.assertRaises(ValueError):
            validate_topic(topic, designer_sources({"dialogueEvidence": evidence}), self.snapshot)

    def test_chat_background_preserves_suggestions_and_excludes_historical_spatial_facts(self):
        context = {"stageSnapshot": self.snapshot, "dialogueEvidence": build_dialogue_evidence([
            {"id": "u1", "role": "user", "content": "不好看"},
            {"id": "a1", "role": "assistant", "content": "B1 is at (9,9). I would try a less regular water boundary."}], "v1")}
        prompt = llm.build_plain_chat_messages([{"role": "user", "content": "按你刚才说的呢"}], ROWS, stage_context=context)[0]["content"]
        self.assertIn("不好看", prompt)
        self.assertIn("less regular water boundary", prompt)
        self.assertNotIn("(9,9)", prompt)
        self.assertNotIn('"dialogueEvidence"', prompt)

    def test_conceptual_entity_suggestion_is_background_not_a_position_fact(self):
        context = {"stageSnapshot": self.snapshot, "dialogueEvidence": build_dialogue_evidence([
            {"id": "a1", "role": "assistant", "content": "T1 is next to B1. I would try less regular water edges around T1."}], "v1")}
        background = llm._dialogue_background_prompt(context)
        self.assertIn("less regular water edges around T1", background)
        self.assertNotIn("T1 is next to B1", background)


class ConversationalDeliveryTests(unittest.TestCase):
    setUp = llm_tests.LLMClientTests.setUp
    execute = llm_tests.LLMClientTests.execute

    def test_short_relevant_reply_and_targeted_question_are_delivered(self):
        for body in ("我更倾向先改善观感。", "你更想改善观感，还是游玩难度？"):
            with self.subTest(body=body):
                result, client = self.execute([body], language="zh-CN", conversation=[{"role": "user", "content": "你觉得该关注什么"}])
                self.assertEqual(result.attempts_used, 1)
                self.assertIn(body, result.assistant_message)

    def test_intent_review_failure_with_confirmed_memory_keeps_short_body(self):
        review = AsyncMock(side_effect=ValueError("bad optional metadata"))
        checks = []
        with patch.object(llm, "_review_intent_candidate_async", review):
            result = asyncio.run(llm._review_ordinary_intent_with_recovery(
                optional_budget=OptionalRepairBudget(time.monotonic() + 60), component_checks=checks,
                api_key="fake", base_url="fake", user_text="少一些", body="我倾向减少水面。",
                decision={"classification": "candidate", "claims": [], "cardText": "bad"},
                active_inclinations=[{"hypothesisId": "h1"}], language="zh-CN", request_id="r",
                deadline=time.monotonic() + 60))
        self.assertTrue(result["omitted"])
        self.assertEqual(result["classification"], "none")

    def test_verified_semantic_body_defect_still_requires_repair(self):
        review = {"classification": "none", "bodyValid": False, "issues": ["The reply does not answer the question."]}
        with patch.object(llm, "_review_intent_candidate_async", AsyncMock(return_value=review)), self.assertRaises(llm.LowQualityModelResponse):
            asyncio.run(llm._review_ordinary_intent_with_recovery(
                optional_budget=OptionalRepairBudget(time.monotonic() + 60), component_checks=[],
                api_key="fake", base_url="fake", user_text="What is T?", body="Hello there.",
                decision={"classification": "none"}, active_inclinations=[], language="en",
                request_id="r", deadline=time.monotonic() + 60))

    def test_optional_review_provider_failure_preserves_body(self):
        error = APIStatusError("upstream unavailable", response=httpx.Response(502,
            request=httpx.Request("POST", "https://example.invalid")), body={})
        with patch.object(llm, "_review_intent_candidate_async", AsyncMock(side_effect=error)):
            result = asyncio.run(llm._review_ordinary_intent_with_recovery(
                optional_budget=OptionalRepairBudget(time.monotonic() + 60), component_checks=[],
                api_key="fake", base_url="fake", user_text="What is T?", body="T marks a target.",
                decision={"classification": "none"}, active_inclinations=[], language="en",
                request_id="r", deadline=time.monotonic() + 60))
        self.assertTrue(result["omitted"])

    def test_unverified_routing_never_delivers_cards_or_applied_state_claims(self):
        result, _ = self.execute(["我可以先解释两个方向的区别。\n<GUIDANCE>INTENT: 我目前理解为你想增加水面。</GUIDANCE>"],
            language="zh-CN", conversation=[{"role": "user", "content": "那你觉得呢"}],
            stage_context={"conversationOnly": True})
        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertNotIn("_intentDecision", result.guidance)

    def test_unverified_state_claim_is_removed_without_discarding_other_prose(self):
        body = "The disagreement is resolved. I would compare the two visual directions first."
        self.assertEqual(llm._strip_unverified_state_claims(body), "I would compare the two visual directions first.")
