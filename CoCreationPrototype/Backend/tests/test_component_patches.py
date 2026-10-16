"""Offline faults at patch boundaries and the real validation entry points."""
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
import app
from component_patches import PatchRequest, PatchError, Target, dependent_components, restore_operation_sources
from level_validation import build_stage_snapshot
from proposal_understanding import critical_review_reason
from patch_fixtures import patch_fixture

ROWS = ["############", "#..........#", "#..p.......#", "#..s..t....#", "#..........#",
        "#..s..t....#", "#..........#", "#..........#", "#..........#", "############"]


def completion(value):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
        message=SimpleNamespace(content=json.dumps(value, ensure_ascii=False)))])


class PatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.value = {"body": "Verified original prose.", "question": None, "legalEmpty": []}
        self.evidence = {"snapshot": {"versionId": "v1"}, "user:u1": {"id": "u1", "content": "Make a proposal"}}
        self.request = PatchRequest("question", self.value, [Target("question", ("question",), (str,))], self.evidence)

    def payload(self):
        return patch_fixture(self.request.prompt(), {"question": "Which experience matters?"})

    def test_only_target_changes_and_input_is_not_mutated(self):
        result = self.request.merge(self.payload())
        self.assertEqual(result["body"], self.value["body"])
        self.assertEqual(result["legalEmpty"], [])
        self.assertIsNone(self.value["question"])

    def test_extra_fields_unknown_targets_and_evidence_are_rejected(self):
        changes = [{"verified": True}, {"field": "body"}, {"componentId": "permission"},
                   {"evidenceRefs": ["invented-turn"]}, {"evidenceRefs": []}, {"valueJson": "3"}]
        for change in changes:
            with self.subTest(change=change):
                payload = self.payload()
                payload["patches"][0].update(change)
                with self.assertRaises(PatchError):
                    self.request.merge(payload)

    def test_duplicate_and_missing_patch_are_rejected(self):
        for patches in ([], self.payload()["patches"] * 2):
            with self.assertRaises(PatchError):
                self.request.merge({"patches": patches})

    def test_stage_or_original_evidence_change_invalidates_patch(self):
        for source, key in ((self.evidence["snapshot"], "versionId"), (self.evidence["user:u1"], "content")):
            before = source[key]
            source[key] = "changed"
            with self.assertRaisesRegex(PatchError, "BINDING_CHANGED"):
                self.request.merge(self.payload())
            source[key] = before

    def test_current_candidate_change_invalidates_patch(self):
        self.value["body"] = "Another body"
        with self.assertRaisesRegex(PatchError, "BINDING_CHANGED"):
            self.request.merge(self.payload())

    def test_boolean_cannot_fill_integer_and_legal_null_can_be_restored(self):
        request = PatchRequest("plan", {"count": None}, [Target("count", ("count",), (int,))], self.evidence)
        with self.assertRaisesRegex(PatchError, "TYPE_INVALID"):
            request.merge(patch_fixture(request.prompt(), {"count": True}))
        request = PatchRequest("plan", {"focus": "bad"}, [Target("focus", ("focus",), (dict, type(None)))], self.evidence)
        self.assertIsNone(request.merge(patch_fixture(request.prompt(), {"focus": None}))["focus"])

    def test_dependency_closure_and_presentation_do_not_invalidate_map(self):
        self.assertEqual(dependent_components("plan"), ["contract", "operations", "validation", "brief", "presentation", "execution"])
        self.assertEqual(dependent_components("presentation"), [])

    def test_restore_current_tile_does_not_choose_destination_or_mutate_input(self):
        operations = [{"row": 8, "column": 4}, {"row": 8, "column": 5, "from": "wrong", "to": "@"}]
        result = restore_operation_sources(operations, ROWS)
        self.assertEqual(result[0], {"row": 8, "column": 4, "from": "."})
        self.assertNotIn("to", result[0])
        self.assertEqual(result[1]["from"], "wrong")
        self.assertNotIn("from", operations[0])
        self.assertEqual(len(build_stage_snapshot(ROWS)["boxes"]), 2)

    def test_recovered_source_does_not_authorize_box_addition_or_movement(self):
        contract = {"strategies": [{"allowedOperators": ["add_water", "remove_water", "add_wall", "remove_wall"],
            "preserve": ["boxes", "targets", "player", "outer_shell"], "requiredTransitions": []}]}
        for operations in ([{"row": 8, "column": 4, "to": "s"}],
                           [{"row": 4, "column": 4, "to": "."}, {"row": 8, "column": 4, "to": "s"}]):
            with self.assertRaises(ValueError):
                llm.execute_revision_operations(ROWS, operations, contract, 1)


class ComponentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_stage_snapshot(ROWS, version_id="v1")

    def test_names_minor_english_and_decimal_model_version_survive_reading(self):
        body = "我是 Kimi K2.6，是 AI 设计伙伴。这个 water 区域可以继续讨论。"
        self.assertEqual(llm.repair_legacy_visible_text(body, "zh-CN"), body)
        self.assertIsNone(llm._visible_chinese_language_issue(body))
        self.assertEqual(llm._content_block_sentences(body)[0], "我是 Kimi K2.6，是 AI 设计伙伴。")

    def test_unknown_internal_identifier_hides_only_its_historical_sentence(self):
        self.assertEqual(llm.repair_legacy_visible_text("routeComplexityScore 需要核实。我们可以讨论水域布局。", "zh-CN"),
                         "我们可以讨论水域布局。")

    def test_bad_optional_metadata_is_omitted_but_required_conflict_is_retained(self):
        guidance = {"intentHypothesis": "routeComplexityScore 体现你的倾向。", "uiCues": [],
                    "coordinateLinks": [{"text": "routeLinkInternal", "row": 1, "column": 1}]}
        result = llm._isolate_optional_language_components(guidance, "zh-CN")
        self.assertIsNone(result["intentHypothesis"])
        self.assertEqual(result["coordinateLinks"], [])
        conflict = llm._isolate_optional_language_components({**guidance, "_intentCandidateReview": {"relation": "conflict"}}, "zh-CN")
        self.assertIsNotNone(conflict["intentHypothesis"])

    def test_missing_review_component_repairs_without_rewriting_body_or_valid_question(self):
        body = "我会先比较水域的分布。\n\n视觉效果还需要你判断。"
        async def request(*args, **kwargs):
            if kwargs["task"] == "proposal_clarification_review":
                return completion({"questionIssue": None})
            spec = json.loads(args[3][-1]["content"])
            self.assertEqual(spec["targets"], ["bodyIssues"])
            self.assertIsNone(spec["frozenCandidate"]["questionIssue"])
            self.assertLessEqual(args[5], 4.0)
            return completion(patch_fixture(spec, {"bodyIssues": []}))
        with patch.object(llm, "_request_completion", AsyncMock(side_effect=request)) as call:
            result = asyncio.run(llm._review_proposal_clarification("offline", "offline", body, "先关注哪个区域？",
                {"stageSnapshot": self.snapshot}, "r", time.monotonic() + 116))
        self.assertEqual(result, (body, ""))
        self.assertEqual(call.await_count, 2)
        self.assertLessEqual(call.call_args_list[0].args[5], 6.0)

    def test_unknown_sentence_reference_is_repaired_and_only_selected_sentence_dropped(self):
        body = "你必须保留27步。我们可以讨论视觉布局。"
        async def request(*args, **kwargs):
            if kwargs["task"] == "proposal_clarification_review":
                return completion({"bodyIssues": ["fabricated-quote"], "questionIssue": None})
            return completion(patch_fixture(args[3][-1]["content"], {"bodyIssues": ["sentence-1"]}))
        with patch.object(llm, "_request_completion", AsyncMock(side_effect=request)):
            result = asyncio.run(llm._review_proposal_clarification("offline", "offline", body, "先关注什么？", {}, "r", time.monotonic() + 116))
        self.assertEqual(result, ("我们可以讨论视觉布局。", ""))

    def test_review_timeout_uses_one_bounded_corrective_slot(self):
        async def request(*args, **kwargs):
            if kwargs["task"] == "proposal_clarification_review":
                raise asyncio.TimeoutError()
            return completion(patch_fixture(args[3][-1]["content"], {"bodyIssues": [], "questionIssue": None}))
        with patch.object(llm, "_request_completion", AsyncMock(side_effect=request)) as call:
            result = asyncio.run(llm._review_proposal_clarification("offline", "offline", "Reliable body.", "Which area?", {}, "r", time.monotonic() + 116))
        self.assertEqual(result[0], "Reliable body.")
        self.assertEqual(call.await_count, 2)

    def test_review_exhaustion_reports_component_and_does_not_forge_valid_prose(self):
        with patch.object(llm, "_request_completion", AsyncMock(return_value=completion({}))) as call:
            with self.assertRaises(llm.LLMServiceError) as caught:
                asyncio.run(llm._review_proposal_clarification("offline", "offline", "A candidate body.", "Which area?", {}, "r", time.monotonic() + 116))
        self.assertEqual(call.await_count, 2)
        self.assertEqual(caught.exception.details["failureStage"], "clarification_review")
        self.assertEqual(caught.exception.details["maximumRepairAttempts"], 1)

    def test_missing_plan_field_patch_preserves_other_strategy_and_empty_transitions(self):
        good = {"effect": "reshape_water", "focus": None, "operators": ["add_water"], "preserve": ["boxes", "targets", "player"],
                "editBudget": 2, "metricGoals": [], "requiredTransitions": []}
        broken = copy.deepcopy(good)
        del broken["operators"]
        responses = [completion({"strategies": [broken, good]})]
        async def request(*args, **kwargs):
            if kwargs["task"] == "revision_plan":
                return responses.pop(0)
            spec = json.loads(args[3][-1]["content"])
            self.assertEqual(spec["targets"], ["strategy-1.operators"])
            self.assertEqual(spec["frozenCandidate"]["strategies"][1], good)
            return completion(patch_fixture(spec, {"strategies": [good, good]}))
        with patch.object(llm, "_request_completion", AsyncMock(side_effect=request)):
            plan, attempts, _ = asyncio.run(llm._compile_revision_plan(api_key="offline", base_url="offline", models=[llm.KIMI_MODEL] * 2,
                messages=[{"role": "user", "content": "Adjust water only"}], request_id="r", started_at=time.monotonic(), deadline=time.monotonic() + 116))
        self.assertEqual(attempts, 2)
        self.assertEqual(plan.strategies[0].required_transitions, ())
        self.assertEqual(plan.strategies[1].focus, None)

    def test_new_preservation_and_same_quote_meaning_change_require_review(self):
        original = {"statement": "Prefer more solving time", "sourceTurnId": "u1", "evidenceSpan": "spend longer"}
        previous = {"goals": [original], "preserve": [], "editScope": []}
        proposed = {**previous, "preserve": [{"statement": "Preserve crowded play", "sourceTurnId": "u2", "evidenceSpan": "crowded"}]}
        self.assertEqual(critical_review_reason({"changes": [], "proposalUnderstanding": proposed}, {"understanding": previous}), "new_preservation_requirement")
        proposed = {**previous, "goals": [{**original, "statement": "Require exactly 27 moves"}]}
        self.assertEqual(critical_review_reason({"changes": [], "proposalUnderstanding": proposed}, {"understanding": previous}), "goal_or_preservation_changed")

    def test_error_counter_is_actual_when_known_and_unknown_is_not_guessed(self):
        error = llm.LLMServiceError("MODEL_RESPONSE_INVALID", "Invalid component", "r", True, 2, 502)
        self.assertNotIn("maximumAttempts", app._llm_failure_details(error))
        error.details = {"maximumAttempts": 2, "component": "clarification_review", "failureStage": "clarification_review"}
        self.assertEqual(app._llm_failure_details(error)["maximumAttempts"], 2)
        self.assertEqual(app._llm_failure_details(error)["failureStage"], "clarification_review")

    def test_requirement_review_repairs_only_missing_metadata_and_reuses_compilation(self):
        from test_reply_reliability import requirement, TURNS
        raw = {"requirements": [requirement()], "automaticBindings": [], "exactTransitions": []}
        tasks = []
        async def request(*args, **kwargs):
            tasks.append(kwargs["task"])
            if kwargs["task"] == "revision_requirements":
                return completion(raw)
            if kwargs["task"] == "revision_requirement_review":
                return completion({"accepted": True})
            spec = json.loads(args[3][-1]["content"])
            self.assertEqual(spec["targets"], ["issues"])
            self.assertTrue(spec["frozenCandidate"]["accepted"])
            return completion(patch_fixture(spec, {"issues": []}))
        with patch.object(llm, "_request_completion", AsyncMock(side_effect=request)):
            result = llm._review_proposal_requirements("offline", "offline",
                {"stageSnapshot": self.snapshot, "requirementUserTurns": TURNS}, "r", time.monotonic() + 116)
        self.assertEqual(len(result["requirements"]), 1)
        self.assertEqual(tasks, ["revision_requirements", "revision_requirement_review", "revision_requirement_review_patch"])

    def test_candidate_missing_destination_is_patched_then_solved_and_frozen(self):
        strategy = {"effect": "reshape_water", "allowedOperators": ["add_water"], "preserve": ["boxes", "targets", "player", "outer_shell"],
                    "minimumChangedCells": 1, "maximumChangedCells": 2, "metricGoals": [], "requiredTransitions": [],
                    "anchorEntities": [], "focus": None, "playObjective": "visual_composition"}
        contract = {"strategies": [strategy], "authorizedBrief": "Adjust water appearance"}
        async def request(*args, **kwargs):
            if kwargs["task"] == "operation_candidates":
                return completion({"candidates": [{"strategyIndex": 1, "operations": [{"row": 8, "column": 4}]}]})
            spec = json.loads(args[3][-1]["content"])
            self.assertEqual(spec["targets"], ["candidate-1.operation-1.to"])
            return completion(patch_fixture(spec, {"candidates": [{"operations": [{"to": "@"}]}]}))
        before = copy.deepcopy(ROWS)
        with patch.object(llm, "_request_completion", AsyncMock(side_effect=request)):
            result = asyncio.run(llm._generate_map_operation_candidates(api_key="offline", base_url="offline", models=[llm.KIMI_MODEL] * 2,
                messages=[{"role": "user", "content": "Use the verified water contract"}], base_rows=ROWS, request_id="r", language="en",
                proposal_validator=None, revision_contract=contract, baseline_metrics={}, started_at=time.monotonic(), deadline=time.monotonic() + 116))
        frozen = app._materialize_verified_automatic_offer(result, ROWS, "en", {"aiEditableTilesOnly": True})
        offer = frozen.guidance["proposalOffer"]
        self.assertTrue(offer["summary"])
        self.assertTrue(offer["rationale"])
        self.assertEqual(offer["executionBrief"]["requiredTransitions"], [{"row": 8, "column": 4, "from": ".", "to": "@"}])
        self.assertIsNone(frozen.proposed_rows)
        self.assertEqual(ROWS, before)

    def test_translation_patch_locks_valid_body_and_restores_source_state(self):
        source = {"turnId": "t1", "body": "The map needs thought.", "followUpQuestion": "Which area?", "intentHypothesis": None,
                  "proposalOfferSummary": None, "proposalOfferRationale": None, "proposalSummary": None, "uiCueTexts": [],
                  "disagreement": None, "stageRows": ROWS, "entityBindings": None}
        initial = {"translations": [{"turnId": "t1", "body": "地图值得仔细考虑。", "followUpQuestion": None}]}
        translated = {"translations": [{"followUpQuestion": "先关注哪个区域？"}]}
        async def request(*args, **kwargs):
            if kwargs["task"] == "translation":
                return completion(initial)
            spec = json.loads(args[3][-1]["content"])
            self.assertEqual(spec["targets"], ["turn-t1.followUpQuestion"])
            self.assertEqual(spec["frozenCandidate"]["translations"][0]["body"], initial["translations"][0]["body"])
            self.assertNotIn("stageRows", spec["evidence"]["turn:t1"])
            return completion(patch_fixture(spec, translated))
        with patch.object(llm, "_request_completion", AsyncMock(side_effect=request)):
            result = asyncio.run(llm._translate_with_model_fallback(api_key="offline", base_url="offline", models=[llm.KIMI_MODEL] * 2,
                messages=[{"role": "user", "content": "Translate the source turn"}], items=[source], target_language="zh-CN",
                request_id="r", started_at=time.monotonic(), deadline=time.monotonic() + 116))
        self.assertEqual(result.translations[0]["body"], "地图值得仔细考虑。")
        self.assertIsNone(result.translations[0].get("disagreement"))
        self.assertEqual(result.attempts_used, 2)

    def test_valid_clarifying_question_without_analysis_has_no_manufactured_body(self):
        from test_proposal_understanding import ProposalUnderstandingTests, topic
        case = ProposalUnderstandingTests()
        case.setUp()
        discovery = {**case.context, "status": "clarifying", "understanding": topic(sufficient=False),
                     "brief": "Visual order\nWater and interior walls"}
        spec = app._proposal_clarification_dimensions_spec(discovery, case.snapshot, "en")
        question = "Which local area should carry the water pattern?"
        with patch.object(llm, "_llm_credentials", return_value=("offline", "offline")), \
                patch.object(llm, "_request_completion", AsyncMock(return_value=completion({"question": question}))) as call:
            result = llm.generate_chat_reply([{"role": "user", "content": "Water and interior walls"}], case.snapshot["rows"], "r",
                language="en", stage_context={"revisionRouting": "needs_clarification", "proposalDiscovery": discovery,
                    "proposalClarification": spec, "stageSnapshot": case.snapshot, "entityBindings": case.bindings},
                _deadline=time.monotonic() + 116)
        self.assertEqual(result.assistant_message, question)
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertEqual(call.await_count, 1)


if __name__ == "__main__":
    unittest.main()
