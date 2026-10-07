"""Synthetic multi-turn failures; all model calls are mocked, never billed."""
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
import app as backend
import llm_client as llm
from level_validation import build_entity_bindings, build_stage_snapshot
from proposal_search import Focus, RevisionPlan, RevisionStrategy
from proposal_understanding import (
    bind_latest, critical_review_reason, designer_sources, protected_request_message,
    validate_plan_scope, validate_topic,
)

ROWS = ["############", "#..........#", "#..p.......#", "#..s..t....#", "#..........#",
    "#..s..t....#", "#..........#", "#..@@......#", "#..........#", "############"]


def response(payload):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
        message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])


def item(statement, source="latest", span=None, **extra):
    return {"statement": statement, "sourceTurnId": source, "evidenceSpan": span or statement, **extra}


def topic(goal="Visual order", goal_source="u1", scope_source="u2", focus=None, sufficient=True):
    return {"aspect": "visual", "goals": [item(goal, goal_source)],
        "editScope": [item("Water and interior walls", scope_source, component=x) for x in ("water", "internal_walls")],
        "focus": focus or [], "preserve": [], "sufficient": sufficient,
        "nextQuestionDimension": "none" if sufficient else "binding"}


def understanding(latest, interpretation, component="targets", operation="mention", acts=None):
    return {"acts": acts or ["intent"], "elements": [component], "evidenceSpan": latest,
        "directionSufficient": True, "mapRelated": True,
        "changes": [{"component": component, "property": "position", "operation": operation, "evidenceSpan": latest}],
        "proposalUnderstanding": interpretation}


class ProposalUnderstandingTests(unittest.TestCase):
    def setUp(self):
        self.bindings = build_entity_bindings(ROWS)
        self.snapshot = build_stage_snapshot(ROWS, version_id="v1", entity_bindings=self.bindings)
        self.context = {"topicId": "u1", "initialRequest": "Visual order", "userTurns": [
            {"id": "u1", "content": "Visual order"}, {"id": "u2", "content": "Water and interior walls"}],
            "questions": [{"id": "a2:1", "text": "Which local area?"}],
            "answers": [], "lastQuestionText": "Which local area?", "understanding": topic(sufficient=False)}

    def classify(self, latest, outputs, context=None):
        call = AsyncMock(side_effect=[response(x) for x in outputs])
        with patch.object(llm, "_llm_credentials", return_value=("offline", "offline")), \
                patch.object(llm, "_request_completion", call):
            result = llm.classify_turn_understanding([{"role": "user", "content": latest}], self.snapshot,
                "offline-request", proposal_context=context or self.context, _deadline=time.monotonic() + 116)
        return result, call

    def test_same_focus_answer_is_not_a_protected_move_across_entity_types(self):
        for label, component, answer in (("T1", "targets", "Around the first destination"),
                ("B2", "boxes", "Around the second crate"), ("P", "player", "By the player entrance")):
            with self.subTest(component=component):
                interpretation = topic(focus=[item(answer, entities=[label])])
                bad = understanding(answer, interpretation, component, "change", ["revision_request"])
                good = understanding(answer, interpretation, component)
                result, call = self.classify(answer, [bad, {"accepted": True, "issues": [], "understanding": good}])
                self.assertEqual(result["changes"][0]["operation"], "mention")
                self.assertTrue(result["criticalReview"]["corrected"])
                self.assertEqual([x.kwargs["task"] for x in call.call_args_list], ["turn_understanding", "turn_understanding_review"])
                record = bind_latest(result, "u3", "v1")["proposalUnderstanding"]
                discovery = {**self.context, "understanding": record, "clarificationQuestionCount": 3}
                self.assertIn(backend._adaptive_revision_routing(answer, {}, self.snapshot,
                    proposal_discovery=discovery, turn_understanding=result), {"proposal", "proposal_conservative"})

    def test_outer_shell_can_be_a_focus_without_being_editable(self):
        answer = "Inside the enclosing edge"
        interpretation = topic(focus=[item(answer, entities=[])])
        result, call = self.classify(answer, [understanding(answer, interpretation, "outer_shell")])
        self.assertNotIn("criticalReview", result)
        self.assertEqual(call.await_count, 1)
        self.assertEqual({x["component"] for x in result["proposalUnderstanding"]["editScope"]}, {"water", "internal_walls"})

    def test_real_protected_edit_still_requires_review_and_specific_refusal(self):
        for component in ("targets", "boxes", "player", "outer_shell"):
            with self.subTest(component=component):
                answer = "Change its position"
                interpretation = topic()
                interpretation["editScope"] = [item(answer, component=component)]
                candidate = understanding(answer, interpretation, component, "change", ["revision_request"])
                result, _ = self.classify(answer, [candidate, {"accepted": True, "issues": [], "understanding": candidate}])
                text = protected_request_message(result, "zh-CN")
                self.assertIn(answer, text)
                self.assertNotIn("箱子、玩家、目标或外壳", text)
                self.assertEqual(backend._adaptive_revision_routing(answer, {}, self.snapshot,
                    proposal_discovery=self.context, turn_understanding=result), "protected_request")

    def test_same_short_answer_binds_to_different_questions_in_review_input(self):
        for question in ("Where should water/wall composition be focused?", "Which target should be relocated?"):
            with self.subTest(question=question):
                context = {**self.context, "lastQuestionText": question}
                answer = "T1"
                candidate = understanding(answer, topic(focus=[item(answer, entities=["T1"])]), operation="change", acts=["revision_request"])
                corrected = understanding(answer, candidate["proposalUnderstanding"])
                if "relocated" in question:
                    # An explicit original relocation request supplies the
                    # action; the short answer only binds the chosen target.
                    context["userTurns"] = [{"id": "u1", "content": "Relocate a target"}, {"id": "u2", "content": "Choose a target"}]
                    context["initialRequest"] = "Relocate a target"
                    candidate["proposalUnderstanding"] = {**topic(goal="Relocate a target"),
                        "aspect": "gameplay", "editScope": [item("Relocate a target", "u1", component="targets")],
                        "focus": [item(answer, entities=["T1"])]}
                    corrected = copy.deepcopy(candidate)
                result, call = self.classify(answer, [candidate, {"accepted": True, "issues": [], "understanding": corrected}], context)
                reviewer_input = json.loads(call.call_args_list[1].args[3][1]["content"])
                self.assertEqual(reviewer_input["source"]["proposalContext"]["lastQuestionText"], question)
                self.assertEqual(result["changes"][0]["operation"], "change" if "relocated" in question else "mention")

    def test_unverified_review_uses_two_rounds_and_returns_retryable_error(self):
        answer = "Around T1"
        bad = understanding(answer, topic(focus=[item(answer, entities=["T1"])]), operation="change", acts=["revision_request"])
        rejected = {"accepted": False, "issues": ["ambiguous action"], "understanding": bad}
        with self.assertRaises(llm.LLMServiceError) as caught:
            self.classify(answer, [bad, rejected, bad, rejected])
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.attempts_used, 2)
        self.assertFalse(self.context["understanding"]["sufficient"])

    def test_scope_correction_gets_review_and_old_focus_does_not_replace_scope(self):
        answer = "Only reshape water this time"
        interpretation = topic()
        interpretation["editScope"] = [item(answer, component="water")]
        candidate = understanding(answer, interpretation, "water", "change", ["revision_request"])
        self.assertEqual(critical_review_reason(candidate, self.context), "edit_scope_changed")
        result, _ = self.classify(answer, [candidate, {"accepted": True, "issues": [], "understanding": candidate}])
        self.assertEqual(result["criticalReview"]["reason"], "edit_scope_changed")

    def test_a_model_cannot_self_authorize_review_metadata(self):
        answer = "Around T1"
        candidate = understanding(answer, topic(focus=[item(answer, entities=["T1"])]))
        candidate["criticalReview"] = {"verified": True}
        with self.assertRaises(llm.LLMServiceError):
            self.classify(answer, [candidate, candidate])

    def test_missing_or_assistant_only_evidence_cannot_establish_a_lock(self):
        for source, span in (("assistant1", "Preserve 27 moves"), ("u1", "Preserve 27 moves")):
            with self.subTest(source=source):
                candidate = topic()
                candidate["preserve"] = [item(span, source)]
                with self.assertRaises(ValueError):
                    validate_topic(candidate, designer_sources(self.context), self.snapshot)

    def test_visual_topic_does_not_need_gameplay_mechanism_or_exact_focus(self):
        record = topic()
        validate_topic(record, designer_sources(self.context), self.snapshot)
        route = backend._adaptive_revision_routing("Water and interior walls", {}, self.snapshot,
            proposal_discovery={**self.context, "understanding": record, "clarificationQuestionCount": 1},
            turn_understanding={"acts": ["intent"], "mapRelated": True})
        self.assertEqual(route, "proposal_conservative")
        record.update(sufficient=False, nextQuestionDimension="mechanism")
        with self.assertRaises(ValueError):
            validate_topic(record, designer_sources(self.context), self.snapshot)

    def test_semantic_question_dimensions_do_not_use_box_or_transport_keywords(self):
        for dimension in ("visual_direction", "edit_scope", "binding"):
            record = topic(sufficient=False)
            record["nextQuestionDimension"] = dimension
            specification = backend._proposal_clarification_spec({**self.context, "status": "clarifying", "understanding": record}, self.snapshot, "zh-CN")
            self.assertEqual(specification["semanticDimension"], dimension)
            self.assertNotIn("运输长度", specification["fallbackQuestion"])
            self.assertNotIn("推箱", specification["fallbackQuestion"])

    def test_revisiting_a_dimension_counts_visible_questions_but_retry_does_not(self):
        record = topic(sufficient=False)
        discovery = {**self.context, "status": "clarifying", "understanding": record,
            "clarificationQuestionCount": 1, "askedQuestionKeys": ["binding"]}
        spec = backend._proposal_clarification_spec(discovery, self.snapshot, "en")
        execution = llm.LLMExecutionResult("I would try a local pattern.\n\n" + spec["fallbackQuestion"], 1, "test",
            proposal_diagnostics={"clarificationQuestion": spec["fallbackQuestion"]})
        context = {"proposalDiscovery": discovery, "proposalClarification": spec, "revisionRouting": "needs_clarification"}
        first = backend._mark_proposal_discovery_guidance(execution, context)
        second = backend._mark_proposal_discovery_guidance(execution, context)
        self.assertEqual(first.guidance["proposalDiscovery"]["clarificationQuestionCount"], 2)
        self.assertEqual(first.guidance["proposalDiscovery"], second.guidance["proposalDiscovery"])

    def test_full_multiturn_topic_survives_reconstruction_and_stage_change(self):
        messages = ["Visual order", "Water and interior walls", "Around T1"]
        turns, annotations = [], {}
        for index, content in enumerate(messages, 1):
            turns.append({"id": f"u{index}", "role": "user", "content": content, "request_id": f"r{index}", "sequence_number": len(turns) + 1, "guidance_json": None})
            if index == 1:
                record = {**topic(), "editScope": [], "sufficient": False, "nextQuestionDimension": "edit_scope"}
            else:
                record = topic(sufficient=index == 3)
                if index == 3:
                    record["focus"] = [item(content, entities=["T1"])]
            # u1/u2 are already real IDs; the current answer is bound on save.
            raw = understanding(content, record, "targets" if index == 3 else "water", acts=["revision_request"] if index == 1 else ["intent"])
            annotations[f"r{index}"] = bind_latest(raw, f"u{index}", "v1")
            turns.append({"id": f"a{index}", "role": "assistant", "content": "Which area?", "request_id": f"r{index}", "sequence_number": len(turns) + 1,
                "guidance_json": json.dumps({"proposalDiscovery": {"topicId": "u1", "status": "clarifying"}})})
        rebuilt = backend._proposal_discovery_from_turns(turns, "v1", turn_understandings=annotations)
        self.assertEqual(rebuilt["understanding"]["focus"][0]["sourceTurnId"], "u3")
        self.assertEqual({x["component"] for x in rebuilt["understanding"]["editScope"]}, {"water", "internal_walls"})
        self.assertEqual(len(rebuilt["answers"]), 2)
        wrong_stage = backend._proposal_discovery_from_turns(turns, "v2", turn_understandings=annotations)
        self.assertNotIn("understanding", wrong_stage)
        self.assertEqual(rebuilt, backend._proposal_discovery_from_turns(turns, "v1", turn_understandings=annotations))

    def test_legacy_topic_reconstruction_is_reviewed_without_rewriting_sources(self):
        context = copy.deepcopy(self.context)
        context.pop("understanding")
        answer = "Around T1"
        candidate = understanding(answer, topic(focus=[item(answer, entities=["T1"])]))
        before = copy.deepcopy(context)
        result, _ = self.classify(answer, [candidate, {"accepted": True, "issues": [], "understanding": candidate}], context)
        self.assertEqual(result["criticalReview"]["reason"], "legacy_topic_reconstruction")
        self.assertEqual(context, before)

    def test_scope_gate_rejects_outside_component_operations(self):
        record = topic()
        record["editScope"] = [item("Water", component="water")]
        def plan(operator):
            return RevisionPlan((RevisionStrategy("reshape_water", Focus(8, 4, 1), (operator,), frozenset(), 1, ()),))
        validate_plan_scope(plan("add_water"), record)
        with self.assertRaises(ValueError):
            validate_plan_scope(plan("add_wall"), record)

    def test_visual_goal_is_not_a_route_metric_or_verified_aesthetic_success(self):
        policy = llm._proposal_objective_policy([], {"requirementRecord": {"requirements": [
            {"property": "appearance", "focused": True, "strength": "goal", "entities": []}]},
            "proposalDiscovery": {"understanding": topic()}})
        self.assertEqual(policy["softObjectiveClass"], "visual_composition")
        self.assertFalse(policy["requiresMechanismEvidence"])
        evidence = llm._objective_mechanism_evidence(ROWS, ROWS, {}, {}, policy)
        self.assertFalse(evidence["passed"])
        self.assertEqual(evidence["missing"], ["visual_quality_not_machine_verified"])

    def test_review_triggers_do_not_reject_personal_suggestions_or_entity_labels(self):
        self.assertFalse(llm._clarification_needs_semantic_review("I would try repetition near T1.", "Which area?"))
        for text in ("我想让水墙更整齐。", "保持27步不变。", "You want exactly two openings.", "Move the target to frame it."):
            self.assertTrue(llm._clarification_needs_semantic_review(text, "Which area?"))

    def test_semantic_body_review_drops_only_unfounded_goal_or_lock(self):
        bad = "我想在保持27步的前提下强化秩序美感。"
        good = "我觉得重复的边界可以形成视觉呼应。"
        context = {"proposalDiscovery": self.context, "proposalClarification": {"questionIntent": "visual composition"}}
        call = AsyncMock(return_value=response({"bodyIssues": [bad], "questionIssue": None}))
        with patch.object(llm, "_request_completion", call):
            body, issue = asyncio.run(llm._review_proposal_clarification("offline", "offline", bad + good,
                "你更希望聚焦哪个区域？", context, "offline", time.monotonic() + 116))
        self.assertEqual(body, good)
        self.assertEqual(issue, "")

    def test_semantic_review_cannot_rewrite_unrelated_text_or_return_missing_evidence(self):
        call = AsyncMock(return_value=response({"bodyIssues": ["not in body"], "questionIssue": None}))
        with patch.object(llm, "_request_completion", call), self.assertRaises(ValueError):
            asyncio.run(llm._review_proposal_clarification("offline", "offline", "I would try repetition.",
                "Which area?", {"proposalDiscovery": self.context}, "offline", time.monotonic() + 116))

    def test_reliable_first_person_suggestion_survives_semantic_review(self):
        body = "我想试着用重复边界形成呼应；这只是一个可选建议。"
        call = AsyncMock(return_value=response({"bodyIssues": [], "questionIssue": None}))
        with patch.object(llm, "_request_completion", call):
            result, _ = asyncio.run(llm._review_proposal_clarification("offline", "offline", body,
                "先聚焦哪片区域？", {"proposalDiscovery": self.context}, "offline", time.monotonic() + 116))
        self.assertEqual(result, body)

    def test_body_semantic_review_failure_cannot_fall_back_to_server_prose(self):
        record = topic(sufficient=False)
        discovery = {**self.context, "status": "clarifying", "understanding": record,
            "brief": "Visual order\nWater and interior walls"}
        spec = backend._proposal_clarification_spec(discovery, self.snapshot, "zh-CN")
        payload = {"body": "我想在保持27步不变的前提下整理边界。", "question": "先围绕哪个局部区域调整？"}
        call = AsyncMock(return_value=response(payload))
        with patch.object(llm, "_llm_credentials", return_value=("offline", "offline")), \
                patch.object(llm, "_request_completion", call), \
                patch.object(llm, "_review_proposal_clarification", AsyncMock(side_effect=ValueError("unverified semantics"))), \
                self.assertRaises(llm.LLMServiceError) as caught:
            llm.generate_chat_reply([{"role": "user", "content": "Water and interior walls"}], ROWS,
                "offline", language="zh-CN", stage_context={"revisionRouting": "needs_clarification",
                    "proposalDiscovery": discovery, "proposalClarification": spec,
                    "stageSnapshot": self.snapshot, "entityBindings": self.bindings}, _deadline=time.monotonic() + 116)
        self.assertTrue(caught.exception.retryable)

    def test_failing_question_keeps_semantically_verified_body_during_repair(self):
        discovery = {**self.context, "status": "clarifying", "understanding": topic(sufficient=False),
            "brief": "Visual order\nWater and interior walls"}
        spec = backend._proposal_clarification_spec(discovery, self.snapshot, "en")
        first = {"body": "I would keep the current objects fixed and try repeated water edges.", "question": "Which local area should frame it?"}
        second = {"body": "", "question": "Which local area should carry the water pattern?"}
        call = AsyncMock(side_effect=[response(first), response(second)])
        review = AsyncMock(return_value=(first["body"], "Repair the question only."))
        with patch.object(llm, "_llm_credentials", return_value=("offline", "offline")), \
                patch.object(llm, "_request_completion", call), patch.object(llm, "_review_proposal_clarification", review):
            result = llm.generate_chat_reply([{"role": "user", "content": "Water and interior walls"}], ROWS,
                "offline", language="en", stage_context={"revisionRouting": "needs_clarification",
                    "proposalDiscovery": discovery, "proposalClarification": spec,
                    "stageSnapshot": self.snapshot, "entityBindings": self.bindings}, _deadline=time.monotonic() + 116)
        self.assertIn(first["body"], result.assistant_message)
        self.assertIn(second["question"], result.assistant_message)


if __name__ == "__main__":
    unittest.main()
