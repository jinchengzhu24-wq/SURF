"""Offline faults through production validators and repair slots; no live Kimi."""
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
import app
import llm_client as llm
from component_patches import PatchError
from conversation_evidence import build_dialogue_evidence, with_latest
from level_validation import build_stage_snapshot
from patch_fixtures import patch_fixture
from proposal_diagnostics import (ProposalDiagnostic, ProposalValidationError, CURRENT_PROPOSAL_DIAGNOSTIC,
    proposal_phase, structural_issue)
from proposal_understanding import validate_topic, understanding_patch_request
from proposal_search import parse_revision_plan
from reliability_report import summarize
from reply_reliability import OptionalRepairBudget

ROWS = ["############", "#..........#", "#..p.......#", "#..s..t....#", "#..........#",
        "#..s..t....#", "#..........#", "#..........#", "#..........#", "############"]
LATEST = "怎么把右边的元素分散一下"


def completion(value):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
        content=json.dumps(value, ensure_ascii=False)))])


def topic():
    return {"aspect": "visual", "goals": [
        {"statement": "视觉上不再拥挤", "sourceTurnId": "u2", "evidenceSpan": "挤在一起不好看"},
        {"statement": "分散右侧元素", "sourceTurnId": "latest", "evidenceSpan": "右边的元素分散一下"}],
        "editScope": [], "focus": [], "preserve": [], "sufficient": False, "nextQuestionDimension": "edit_scope"}


def understanding(meaning):
    return {"acts": ["revision_request"], "elements": ["unknown"], "evidenceSpan": LATEST,
        "mapRelated": True, "directionSufficient": False, "changes": [], "proposalUnderstanding": meaning}


class ProposalDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"u1": "地图右边太紧凑了", "u2": "我觉得这样挤在一起不好看", "latest": LATEST}
        self.snapshot = build_stage_snapshot(ROWS, version_id="v1")
        turns = [{"id": key, "role": "user", "content": value, "version_id": "v1"}
            for key, value in self.sources.items() if key != "latest"]
        turns.append({"id": "a1", "role": "assistant", "content": "把两个箱子分散到地图两端", "version_id": "v1"})
        self.context = {"dialogueEvidence": with_latest(build_dialogue_evidence(turns, "v1"), LATEST)}

    def classify(self, request):
        with patch.object(llm, "_llm_credentials", return_value=("offline", "offline")), \
                patch.object(llm, "_request_completion", AsyncMock(side_effect=request)):
            return llm.classify_turn_understanding([{"role": "user", "content": LATEST}], self.snapshot,
                "offline-request", forced_proposal=True, proposal_context=self.context,
                _deadline=time.monotonic() + 116)

    def test_structural_errors_are_specific_bounded_and_contain_no_quotes(self):
        cases = [("sourceTurnId", [], "SOURCE_ID_TYPE_INVALID"),
                 ("sourceTurnId", "missing", "SOURCE_NOT_FOUND"),
                 ("sourceTurnId", "user:latest", "SOURCE_NOT_FOUND"),
                 ("evidenceSpan", "", "EVIDENCE_SPAN_EMPTY"),
                 ("evidenceSpan", "改写的用户原话", "EVIDENCE_SPAN_NOT_EXACT")]
        for field, value, code in cases:
            bad = topic()
            bad["goals"][1][field] = value
            with self.subTest(code=code), self.assertRaises(ProposalValidationError) as caught:
                validate_topic(bad, self.sources, self.snapshot)
            issue = caught.exception.issues[0]
            self.assertEqual(issue["reasonCode"], code)
            self.assertEqual(issue["fieldPath"], "proposalUnderstanding.goals[1]." + field)
            self.assertNotIn(LATEST, json.dumps(issue, ensure_ascii=False))
        bad = topic()
        bad["goals"] = [{"statement": "goal", "sourceTurnId": "unknown", "evidenceSpan": ""}] * 20
        with self.assertRaises(ProposalValidationError) as caught:
            validate_topic(bad, self.sources, self.snapshot)
        self.assertEqual(len(caught.exception.issues), 16)
        self.assertEqual(caught.exception.issue_count, 40)
        self.assertTrue(caught.exception.issues_truncated)

    def test_assistant_server_and_invalid_current_entity_are_distinguished(self):
        bad = topic()
        bad["preserve"] = [{"statement": "固定箱子", "sourceTurnId": "snapshot", "evidenceSpan": "boxes"}]
        bad["goals"][1]["sourceTurnId"] = "a1"
        bad["focus"] = [{"statement": "right", "sourceTurnId": "latest", "evidenceSpan": "右边", "entities": ["B999"]}]
        with self.assertRaises(ProposalValidationError) as caught:
            validate_topic(bad, self.sources, self.snapshot, {"a1": "assistant"})
        self.assertEqual([x["sourceKind"] for x in caught.exception.issues], ["assistant", "unknown", "server"])
        self.assertIn("CURRENT_ENTITY_INVALID", [x["reasonCode"] for x in caught.exception.issues])

    def test_real_entry_targets_only_failed_id_and_preserves_goals_and_source_namespace(self):
        bad = topic()
        bad["goals"][1]["sourceTurnId"] = "user:latest"
        calls = []
        async def request(*args, **kwargs):
            calls.append(kwargs["task"])
            if len(calls) == 1:
                prompt = json.loads(args[3][1]["content"])
                self.assertEqual(prompt["latestUserMessage"], LATEST)
                self.assertIn({"id": "u2", "content": self.sources["u2"]}, prompt["designerTurns"])
                self.assertEqual(prompt["editableComponents"], ["water", "internal_walls"])
                return completion(understanding(bad))
            spec = json.loads(args[3][-1]["content"])
            self.assertEqual(spec["targets"], ["proposalUnderstanding.goals.1.sourceTurnId"])
            self.assertEqual(spec["frozenCandidate"]["understanding"]["proposalUnderstanding"]["goals"][0], topic()["goals"][0])
            self.assertIn("user:latest", spec["evidence"])
            return completion(patch_fixture(spec, {"understanding": {"proposalUnderstanding": topic()}}))
        result = self.classify(request)
        self.assertEqual(result["proposalUnderstanding"], topic())
        self.assertEqual(calls, ["turn_understanding", "turn_understanding_patch"])

    def test_bad_quote_repairs_only_quote_and_cannot_rewrite_valid_goal(self):
        bad = topic()
        bad["goals"][1]["evidenceSpan"] = "paraphrase"
        try:
            validate_topic(bad, self.sources, self.snapshot)
        except ProposalValidationError as error:
            request = understanding_patch_request(understanding(bad), error, self.sources, self.snapshot)
        self.assertEqual(list(request.targets), ["proposalUnderstanding.goals.1.evidenceSpan"])
        payload = patch_fixture(request.prompt(), {"understanding": {"proposalUnderstanding": topic()}})
        payload["patches"][0]["field"] = "proposalUnderstanding.goals.0.statement"
        with self.assertRaises(PatchError):
            request.merge(payload)
        self.assertEqual(bad["goals"][0], topic()["goals"][0])

    def test_merged_but_invalid_patch_preserves_precise_original_and_final_failure(self):
        bad = topic()
        bad["goals"][1]["sourceTurnId"] = "missing"
        calls = []
        async def request(*args, **kwargs):
            calls.append(kwargs["task"])
            return completion(understanding(bad) if len(calls) == 1 else
                patch_fixture(args[3][-1]["content"], {"understanding": {"proposalUnderstanding": bad}}))
        with self.assertRaises(llm.LLMServiceError) as caught:
            self.classify(request)
        diagnostic = caught.exception.details["proposalDiagnostic"]
        self.assertEqual(diagnostic["firstFailure"]["reasonCode"], "SOURCE_NOT_FOUND")
        self.assertEqual(diagnostic["finalFailure"]["reasonCode"], "SOURCE_NOT_FOUND")
        states = [step["status"] for step in diagnostic["steps"] if step["stage"] == "component_repair"]
        self.assertIn("merged", states)
        self.assertIn("exhausted", states)
        self.assertNotIn("revalidated", states)
        self.assertEqual(caught.exception.attempts_used, 2)
        self.assertNotIn(LATEST, json.dumps(diagnostic, ensure_ascii=False))

    def test_new_proposal_contract_and_map_acceptance_cannot_move_protected_entities(self):
        plan = parse_revision_plan({"strategies": [{"effect": "relocate_box", "focus": None,
            "operators": ["move_box"], "preserve": ["outer_shell", "player", "targets"], "editBudget": 2,
            "metricGoals": [], "requiredTransitions": []}]})
        with self.assertRaises(ValueError):
            llm._build_revision_execution_contract(plan, "move", {"aiEditableTilesOnly": True})
        moved = list(ROWS)
        moved[3] = "#...s.t....#"
        with self.assertRaises(ValueError):
            app._validate_changed_proposal(ROWS, moved)
        changed = list(ROWS)
        changed[7] = "#..@.......#"
        llm.validate_ai_edit_diff(ROWS, changed)
        with self.assertRaises(ValueError):
            llm.validate_ai_edit_diff(ROWS, [row.replace("#", ".", 1) for row in ROWS])

    def test_production_prompts_share_permissions_and_do_not_infer_a_specific_scope(self):
        messages = llm.build_plain_chat_messages([{"role": "user", "content": LATEST}], ROWS,
            stage_context={"stageSnapshot": self.snapshot})
        self.assertIn("only water and internal walls", messages[0]["content"])
        self.assertIn("Normal Sokoban pushes", messages[0]["content"])
        operation = llm._build_map_operation_messages({"strategies": []}, ROWS, "en", {"stageSnapshot": self.snapshot})
        self.assertNotIn("A moved player", operation[0]["content"])
        self.assertIn("Allowed destination tiles are #, . and @ only", operation[0]["content"])
        self.assertEqual(validate_topic(topic(), self.sources, self.snapshot)["editScope"], [])

    def test_permission_body_defect_is_required_recovery_even_without_intent_card(self):
        reviewed = {"classification": "none", "claims": [], "cardText": "", "cardTextValid": True,
            "bodyValid": False, "issues": ["protected relocation suggestion"], "relation": "none",
            "conflictingHypothesisIds": []}
        state = {}
        with patch.object(llm, "_review_intent_candidate_async", AsyncMock(return_value=reviewed)):
            with self.assertRaises(llm.LowQualityModelResponse):
                asyncio.run(llm._review_ordinary_intent_with_recovery(optional_budget=OptionalRepairBudget(time.monotonic()+116),
                    component_checks=[], component_state=state, deadline=time.monotonic()+116, body="Move the boxes to opposite corners."))
        self.assertTrue(state["bodyReviewRequired"])
        self.assertEqual(state["lockedIntentReview"]["claims"], [])

    def test_instrumentation_preserves_child_failure_and_never_uses_exception_text(self):
        @proposal_phase("contract", "contract")
        def contract():
            raise ProposalValidationError([structural_issue("strategies", "PROTECTED_EDIT")])
        @proposal_phase("proposal_generation", "proposal")
        def parent():
            try:
                contract()
            except ValueError as error:
                wrapped = llm.LLMServiceError("MODEL_RESPONSE_INVALID", "private original text", "r", True, 1, 502)
                wrapped.details = error.details
                raise wrapped from error
        with self.assertRaises(llm.LLMServiceError) as caught:
            parent()
        diagnostic = caught.exception.details["proposalDiagnostic"]
        self.assertEqual(diagnostic["finalFailure"]["stage"], "contract")
        self.assertNotIn("private original text", json.dumps(diagnostic))

    def test_all_phase_faults_are_traceable_and_bounded(self):
        for stage in ("understanding", "understanding_review", "clarification", "requirements", "requirements_review",
                      "plan", "contract", "operation_candidates", "search", "structure_and_solve", "freeze",
                      "proposal_presentation", "challenge_review", "execution_replay", "acceptance", "commit"):
            @proposal_phase(stage, stage)
            def fail():
                raise ValueError("sensitive exception details")
            with self.subTest(stage=stage), self.assertRaises(ValueError) as caught:
                fail()
            self.assertEqual(caught.exception.details["proposalDiagnostic"]["finalFailure"]["stage"], stage)
        diagnostic = ProposalDiagnostic()
        diagnostic.record("understanding", "understanding", "failed", "SOURCE_NOT_FOUND")
        for index in range(120):
            diagnostic.record("search", "candidate", "passed", attempt=index, modelText="must not persist")
        self.assertEqual(len(diagnostic.steps), 96)
        self.assertEqual(diagnostic.snapshot()["firstFailure"]["reasonCode"], "SOURCE_NOT_FOUND")
        self.assertNotIn("must not persist", json.dumps(diagnostic.snapshot()))

    def test_report_keeps_diagnostic_counts_separate_from_body_requests(self):
        value = ProposalDiagnostic({"requestId": "r", "messageKey": "k"})
        value.repair("understanding", "merged")
        events = [{"sessionId": "s", "eventType": "reply_pipeline_diagnostic",
            "payload": {"requestId": "r", "proposalDiagnostic": value.snapshot()}},
            {"sessionId": "s", "eventType": "reply_pipeline_diagnostic", "payload": {"requestId": "old"}}]
        report = summarize(events)
        self.assertEqual(report["logicalRequests"], 0)
        self.assertEqual(report["proposalDiagnostics"]["httpAttempts"], 1)
        self.assertEqual(report["proposalDiagnostics"]["pipelineRecordsMissingDiagnostic"], 1)
        self.assertEqual(report["proposalDiagnostics"]["repairOutcomes"], {"understanding:merged": 1})


if __name__ == "__main__":
    unittest.main()
