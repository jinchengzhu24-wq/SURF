import copy
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as backend
import llm_client
from design_requirements import (
    evaluate_requirements, goal_rank, preflight_requirements, protected_change_requested,
    tradeoff_message, validate_requirement_record,
)
from level_validation import build_entity_bindings, build_stage_snapshot, validate_and_solve
from revision_workflow import build_revision_workflow, validate_semantic_constraints, SemanticConstraintError

BASE = ["############", "#..........#", "#.......@@.#", "#..........#",
    "#...p......#", "#...s.t....#", "#..........#", "#..........#", "#..........#", "############"]
SOURCES = [
    {"id": "u1", "content": "给我一个方案想想怎么让玩家花费更多时间"},
    {"id": "u2", "content": "增加箱子之间的切换频率来制造碎片化思考"},
    {"id": "u3", "content": "策略性必须"},
    {"id": "u4", "content": "打断B1的连续推动"},
    {"id": "u5", "content": "早期吧"},
]


def requirement(prop="boxAlternations", relation="increase", **kwargs):
    item = {"component": "gameplay", "property": prop, "relation": relation, "strength": "goal",
        "entities": [], "value": None, "unit": "none", "priorityEvidenceSpan": "", "focused": True,
        "sourceTurnId": "u2", "evidenceSpan": SOURCES[1]["content"], "statement": "增加箱子切换频率"}
    item.update(kwargs)
    return item


def changed(*updates):
    rows = [list(row) for row in BASE]
    for r, c, tile in updates:
        rows[r - 1][c - 1] = tile
    return ["".join(row) for row in rows]


def response(payload):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])


class DesignRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.bindings = build_entity_bindings(BASE)
        self.snapshot = build_stage_snapshot(BASE, version_id="v1", entity_bindings=self.bindings)

    def record(self, *requirements, sources=None, bindings=None, transitions=None):
        return validate_requirement_record({"requirements": list(requirements), "automaticBindings": bindings or [],
            "exactTransitions": transitions or []}, sources or SOURCES, self.snapshot)

    def test_screenshot_switching_does_not_compile_entity_counts(self):
        record = self.record(requirement(), requirement("timing", "early", sourceTurnId="u5",
            evidenceSpan="早期吧", entities=["B1"], statement="较早切换处理其他箱子"))
        workflow = build_revision_workflow("\n".join(x["content"] for x in SOURCES),
            {"requirementPolicyVersion": 1, "requirementRecord": record, "versionId": "v1"})
        self.assertEqual(workflow["semanticConstraints"], [])
        self.assertFalse(preflight_requirements(BASE, record))
        policy = llm_client._proposal_objective_policy([], {"requirementRecord": record})
        self.assertEqual(policy["hardMetricGoals"], [])
        self.assertFalse(any(x["property"] == "count" for x in record["requirements"]))

    def test_b1_is_not_a_metric_value(self):
        with self.assertRaisesRegex(ValueError, "Numeric requirement"):
            self.record(requirement("minimumPushes", sourceTurnId="u4", evidenceSpan="打断B1的连续推动", value=1, unit="delta"))

    def test_exact_span_and_entity_are_validated(self):
        for changes in [{"evidenceSpan": "AI invented this"}, {"entities": ["B9"]}, {"sourceTurnId": "assistant"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.record(requirement(**changes))

    def test_explicit_number_and_units(self):
        source = [{"id": "u2", "content": "增加3次推动"}]
        for unit in ("delta", "absolute"):
            record = self.record(requirement("solutionPushes", evidenceSpan=source[0]["content"], value=3, unit=unit), sources=source)
            self.assertEqual(record["requirements"][0]["value"], 3)
        with self.assertRaises(ValueError):
            self.record(requirement("solutionPushes", evidenceSpan=source[0]["content"], value=4, unit="delta"), sources=source)

    def test_chinese_explicit_number(self):
        source = [{"id": "u2", "content": "增加两次推动"}]
        self.record(requirement("solutionPushes", evidenceSpan=source[0]["content"], value=2, unit="delta"), sources=source)

    def test_directional_goal_cannot_become_invariant(self):
        with self.assertRaisesRegex(ValueError, "directional goal"):
            self.record(requirement(strength="invariant"))

    def test_real_box_quantity_request_conflicts_with_fixed_entities(self):
        source = [{"id": "u2", "content": "增加箱子数量"}]
        record = self.record(requirement("count", component="box", evidenceSpan=source[0]["content"]), sources=source)
        self.assertEqual(preflight_requirements(BASE, record)[0]["reason"], "protected_entity_change")

    def test_unchanged_partial_and_reverse(self):
        record = self.record(requirement(value=3, unit="delta", evidenceSpan="增加3次切换"),
            sources=[{"id": "u2", "content": "增加3次切换"}])
        for after, status, passed in [(3, "deferred", True), (4, "partial", True), (6, "fulfilled", True), (2, "deferred", False)]:
            with self.subTest(after=after):
                result = evaluate_requirements(BASE, BASE, record, {"boxAlternations": 3}, {"boxAlternations": after})[0]
                self.assertEqual((result["status"], result["passed"]), (status, passed))

    def test_decrease_direction_is_preserved(self):
        record = self.record(requirement("solutionSteps", "decrease"))
        self.assertTrue(evaluate_requirements(BASE, BASE, record, {"solutionSteps": 20}, {"solutionSteps": 20})[0]["passed"])
        self.assertFalse(evaluate_requirements(BASE, BASE, record, {"solutionSteps": 20}, {"solutionSteps": 21})[0]["passed"])

    def test_timing_is_a_relative_route_fact_not_fixed_coordinates(self):
        record = self.record(requirement("timing", "early", sourceTurnId="u5", evidenceSpan="早期吧", entities=["B1"]))
        for count, passed in [(7, True), (3, True), (9, False)]:
            result = evaluate_requirements(BASE, BASE, record, {"firstRuns": {"B1": 7}}, {"firstRuns": {"B1": count}})[0]
            self.assertEqual(result["passed"], passed)
        self.assertEqual(record["exactTransitions"], [])

    def test_shorter_transport_does_not_prove_an_interruption(self):
        record = self.record(requirement("timing", "early", sourceTurnId="u5", evidenceSpan="早期吧", entities=["B1"]))
        before = {"firstRuns": {"B1": 7}, "interruptions": {"B1": False}}
        after = {"firstRuns": {"B1": 3}, "interruptions": {"B1": False}}
        outcome = evaluate_requirements(BASE, BASE, record, before, after)[0]
        self.assertEqual(outcome["status"], "deferred")
        self.assertTrue(outcome["passed"])
        before["interruptions"]["B1"] = True
        self.assertFalse(evaluate_requirements(BASE, BASE, record, before, after)[0]["passed"])

    def test_fixed_counts_and_positions_enforced_even_in_shadow(self):
        source = [{"id": "u2", "content": "水域数量不变，不移动水域"}]
        for prop in ("count", "positions"):
            record = self.record(requirement(prop, "preserve", component="water", strength="invariant", evidenceSpan=source[0]["content"]), sources=source)
            workflow = {"mode": "shadow", "requirementPolicyVersion": 1, "requirementRecord": record}
            with self.assertRaises(SemanticConstraintError):
                validate_semantic_constraints(BASE, changed((3, 9, ".")), workflow)

    def test_fixed_edit_scope_is_not_negotiable(self):
        source = [{"id": "u2", "content": "至少修改3格"}]
        record = self.record(requirement("change_scope", component="wall", strength="invariant", value=3,
            unit="delta", evidenceSpan=source[0]["content"]), sources=source)
        self.assertFalse(evaluate_requirements(BASE, changed((2, 2, "#")), record)[0]["passed"])

    def test_distance_nonreverse(self):
        source = [{"id": "u2", "content": "水域靠近B1"}]
        record = self.record(requirement("distance", "nearer", component="water", entities=["B1"], evidenceSpan=source[0]["content"]), sources=source)
        before = {"entityPositions": {"B1": (6, 5)}}
        self.assertFalse(evaluate_requirements(BASE, changed((3, 9, "."), (3, 10, "."), (2, 10, "@")), record, before, before)[0]["passed"])

    def test_preference_does_not_block_candidate(self):
        record = self.record(requirement(strength="preference"))
        result = evaluate_requirements(BASE, BASE, record, {"boxAlternations": 3}, {"boxAlternations": 1})[0]
        self.assertTrue(result["passed"])

    def test_auto_binding_is_assumption_and_cannot_replace_bound_goal(self):
        record = self.record(requirement(), bindings=[{"entity": "B1", "reason": "smallest feasible local area"}])
        self.assertEqual(record["automaticBindings"][0]["authority"], "proposal_assumption")
        self.assertEqual(record["requirements"][0]["entities"], [])
        with self.assertRaises(ValueError):
            self.record(requirement(entities=["B1"]), bindings=[{"entity": "T1", "reason": "another"}])

    def test_exact_transition_evidence_preflight_and_replay(self):
        source = [{"id": "u2", "content": "把(2,2)的地板改为墙"}]
        transition = {"row": 2, "column": 2, "from": ".", "to": "#", "sourceTurnId": "u2", "evidenceSpan": source[0]["content"]}
        record = self.record(sources=source, transitions=[transition])
        self.assertFalse(preflight_requirements(BASE, record))
        self.assertTrue(evaluate_requirements(BASE, changed((2, 2, "#")), record)[0]["passed"])
        self.assertFalse(evaluate_requirements(BASE, changed((2, 3, "#")), record)[0]["passed"])
        conflict = {**record, "exactTransitions": [transition, dict(transition, to="@") ]}
        self.assertTrue(preflight_requirements(BASE, conflict))

    def test_priority_focus_then_completion_ranking(self):
        def outcome(status, prioritized=False, focused=False):
            return {"strength": "goal", "status": status, "prioritized": prioritized, "focused": focused}
        self.assertGreater(goal_rank([outcome("fulfilled", True)]), goal_rank([outcome("fulfilled", focused=True)] * 3))
        self.assertGreater(goal_rank([outcome("fulfilled", focused=True)]), goal_rank([outcome("fulfilled")] * 3))

    def test_tradeoff_message_keeps_deferred_goals_and_qualifies_route(self):
        record = self.record(requirement())
        outcomes = evaluate_requirements(BASE, BASE, record, {"boxAlternations": 3}, {"boxAlternations": 3})
        self.assertIn("仍保留", tradeoff_message(record, outcomes, "zh-CN"))
        outcomes = evaluate_requirements(BASE, BASE, record, {"boxAlternations": 3}, {"boxAlternations": 4})
        self.assertIn("一条求解路线", tradeoff_message(record, outcomes, "zh-CN"))

    def test_independent_review_repairs_semantic_misread(self):
        payload = {"requirements": [requirement()], "automaticBindings": [], "exactTransitions": []}
        returns = [response(payload), response({"accepted": False, "issues": ["attribute misread"]}),
            response(payload), response({"accepted": True, "issues": []})]
        tasks = []
        async def stub(*args, **kwargs):
            tasks.append(kwargs["task"])
            return returns.pop(0)
        with patch.object(llm_client, "_request_completion", side_effect=stub):
            record = llm_client._review_proposal_requirements("offline", "offline", {"stageSnapshot": self.snapshot,
                "proposalDiscovery": {"topicId": "u1", "userTurns": SOURCES, "answers": []}}, "request", time.monotonic() + 116)
        self.assertEqual(tasks, ["revision_requirements", "revision_requirement_review"] * 2)
        self.assertEqual(record["topicId"], "u1")

    def test_unreliable_interpretation_is_retryable_not_user_conflict(self):
        async def stub(*args, **kwargs):
            return response({})
        with patch.object(llm_client, "_request_completion", side_effect=stub), self.assertRaises(llm_client.LLMServiceError) as caught:
            llm_client._review_proposal_requirements("offline", "offline", {"stageSnapshot": self.snapshot,
                "requirementUserTurns": SOURCES}, "request", time.monotonic() + 116)
        self.assertEqual(caught.exception.code, "REQUIREMENT_INTERPRETATION_INVALID")
        self.assertTrue(caught.exception.retryable)

    def test_protected_mentions_and_preservation_do_not_request_entity_edits(self):
        self.assertFalse(protected_change_requested({"elements": ["boxes"]}, active_topic=True))
        self.assertFalse(protected_change_requested({"changes": [{"component": "boxes", "property": "position", "operation": "preserve"}]}))
        self.assertFalse(protected_change_requested({"changes": [{"component": "gameplay", "property": "switching"}]}))
        self.assertTrue(protected_change_requested({"changes": [{"component": "boxes", "property": "count"}]}))

    def test_failure_has_no_warning_and_no_false_impossibility_claim(self):
        error = llm_client.LLMServiceError("SEMANTIC_CONSTRAINT_NOT_MET", "failed", "request", False, 2, 422)
        error.revision_contract = {"revisionWorkflow": {"requirementRecord": self.record(requirement())}}
        result = backend._classified_proposal_failure_execution(language="zh-CN", request_id="request", exception=error)
        self.assertEqual(result.guidance["uiCues"], [])
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertIsNone(result.proposed_rows)
        self.assertIn("不能证明", result.assistant_message)

    def test_four_question_screenshot_rebuild_repairs_missing_counts(self):
        turns = []
        for index in range(4):
            turns.append({"id": f"u{index + 1}", "role": "user", "content": SOURCES[index]["content"],
                "request_id": f"r{index}", "sequence_number": len(turns) + 1, "guidance_json": None})
            turns.append({"id": f"a{index}", "role": "assistant", "content": f"讨论方向。第{index + 1}个偏好是什么？",
                "request_id": f"a{index}", "sequence_number": len(turns) + 1,
                "guidance_json": json.dumps({"proposalDiscovery": {"topicId": "u1", "status": "clarifying", "clarificationQuestionCount": [1, 1, 2, 2][index]}})})
        for i in range(30):
            turns.append({"id": f"later{i}", "role": "user", "content": "不知道", "request_id": f"later{i}",
                "sequence_number": len(turns) + 1, "guidance_json": None})
        annotated = {"r0": {"acts": ["revision_request"], "elements": ["unknown"]},
            "r1": {"acts": ["revision_request"], "elements": ["boxes"]},
            "r3": {"acts": ["revision_request"], "elements": ["boxes"]}}
        first = backend._proposal_discovery_from_turns(turns, "v1", turn_understandings=annotated)
        second = backend._proposal_discovery_from_turns(copy.deepcopy(turns), "v1", turn_understandings=annotated)
        self.assertEqual(first, second)
        self.assertEqual(first["clarificationQuestionCount"], 3)
        self.assertEqual(first["observedQuestionCount"], 4)
        self.assertEqual(first["answers"][0]["answerTurnId"], "u2")
        self.assertEqual(first["answers"][0]["answerText"], SOURCES[1]["content"])
        self.assertEqual(first["userTurns"][0], SOURCES[0])
        self.assertIn(backend._adaptive_revision_routing("不知道", {}, self.snapshot, proposal_discovery=first,
            turn_understanding={"acts": ["evaluation"], "elements": ["unknown"], "mapRelated": True}), {"proposal", "proposal_conservative"})

    def test_multiple_questions_counted_and_fourth_cannot_display(self):
        discovery = {"topicId": "u1", "clarificationQuestionCount": 1, "askedQuestionKeys": []}
        result = llm_client.LLMExecutionResult("偏好甲？偏好乙？偏好丙？", 0, "request", guidance={"followUpQuestion": "隐藏的第四问？"})
        guarded = backend._mark_proposal_discovery_guidance(result, {"proposalDiscovery": discovery, "revisionRouting": "needs_clarification"})
        self.assertEqual(len(backend._visible_proposal_questions(guarded.assistant_message)), 2)
        self.assertEqual(guarded.guidance["proposalDiscovery"]["clarificationQuestionCount"], 3)
        self.assertIsNone(guarded.guidance["followUpQuestion"])
        exhausted = backend._mark_proposal_discovery_guidance(result, {"proposalDiscovery": dict(discovery, clarificationQuestionCount=3), "revisionRouting": "needs_clarification"})
        self.assertEqual(backend._visible_proposal_questions(exhausted.assistant_message), [])

    def test_early_sufficient_request_does_not_force_first_question(self):
        discovery = {"topicId": "u1", "status": "clarifying", "clarificationQuestionCount": 0, "userEvidence": ["增加水域"]}
        route = backend._adaptive_revision_routing("增加水域", {}, self.snapshot, proposal_discovery=discovery,
            turn_understanding={"acts": ["revision_request"], "elements": ["water"], "mapRelated": True,
                "directionSufficient": True, "changes": [{"component": "water", "property": "count", "operation": "change"}]})
        self.assertIn(route, {"proposal", "proposal_conservative"})

    def test_frozen_execution_rechecks_goal_claim(self):
        record = self.record(requirement("count", component="water"))
        workflow = {"requirementRecord": record, "goalOutcomes": evaluate_requirements(BASE, changed((2, 4, "@")), record)}
        unchanged = validate_and_solve(BASE)
        with self.assertRaises(SemanticConstraintError):
            llm_client.validate_frozen_requirements(BASE, BASE, workflow, unchanged, unchanged, self.bindings)

    def test_new_two_role_pipeline_freezes_goals_and_modifier_receives_no_chat(self):
        from test_llm_client import revision_plan_payload, operation_payload, FakeClient
        source = [{"id": "u2", "content": "增加水域数量"}]
        typed = {"requirements": [requirement("count", component="water", evidenceSpan=source[0]["content"])],
            "automaticBindings": [], "exactTransitions": []}
        client = FakeClient([json.dumps(typed, ensure_ascii=False), json.dumps({"accepted": True, "issues": []}),
            revision_plan_payload(effect="reshape_water", operators=["add_water"],
                focus={"row": 2, "column": 4, "radius": 1}, preserve=["outer_shell", "player", "boxes", "targets", "unrelated_areas"],
                edit_budget=1, required_transitions=[]), operation_payload([{"row": 2, "column": 4, "to": "@"}])])
        context = {"versionId": "v1", "stageNumber": 1, "stageSnapshot": self.snapshot, "entityBindings": self.bindings,
            "requirementPolicyVersion": 1, "requirementUserTurns": source, "aiEditableTilesOnly": True,
            "authorizedRevisionBrief": source[0]["content"], "responseLanguage": "zh-CN"}
        with patch.object(llm_client, "_create_async_client", return_value=client):
            execution = llm_client._generate_revision_search_proposal_sync(api_key="offline", base_url="offline",
                conversation=[{"role": "user", "content": source[0]["content"]}], rows=BASE, request_id="new-policy",
                language="zh-CN", proposal_validator=validate_and_solve, stage_context=context, deadline=time.monotonic() + 116)
        self.assertIsNotNone(execution.proposed_rows)
        workflow = execution.revision_contract["revisionWorkflow"]
        self.assertEqual(workflow["requirementPolicyVersion"], 1)
        self.assertEqual(workflow["goalOutcomes"][0]["status"], "fulfilled")
        modifier = client.chat.completions.calls[-1]["messages"]
        modifier_json = " ".join(x["content"] for x in modifier)
        self.assertIn("requirements", modifier_json)
        self.assertNotIn("designerTurns", modifier_json)
        self.assertNotIn("dgContext", modifier_json)
        frozen = backend._materialize_verified_automatic_offer(execution, BASE, "zh-CN", context)
        self.assertIsNone(frozen.proposed_rows)
        self.assertTrue(frozen.guidance["proposalOffer"]["executionBrief"]["requiredTransitions"])
        marked = backend._mark_proposal_discovery_guidance(frozen, {"proposalDiscovery": {"topicId": "u2", "clarificationQuestionCount": 0}, "revisionRouting": "proposal"})
        self.assertTrue(marked.guidance["proposalDiscovery"]["hasValidatedCandidate"])

    def test_new_proposal_budget_is_bounded_by_120_seconds(self):
        expected = llm_client.LLMExecutionResult("candidate", 0, "budget")
        with patch.object(llm_client, "_llm_credentials", return_value=("offline", "offline")), patch.object(
            llm_client, "_generate_revision_search_proposal_sync", return_value=expected) as generate:
            llm_client.generate_chat_reply([{"role": "user", "content": "Please give me a plan."}], BASE, "budget",
                stage_context={"revisionRouting": "proposal_conservative", "requirementPolicyVersion": 1})
        remaining = generate.call_args.kwargs["deadline"] - time.monotonic()
        self.assertGreater(remaining, 100)
        self.assertLessEqual(remaining, 116)


if __name__ == "__main__":
    unittest.main()
