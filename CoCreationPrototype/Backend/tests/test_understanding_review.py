"""Synthetic provider-boundary regressions; no credentials or network required."""
import copy
import json
import sys
import time
import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_client as llm
import test_wire_contracts as wire
from test_proposal_understanding import ROWS, topic, item, understanding
from patch_fixtures import patch_fixture
from level_validation import build_stage_snapshot
from understanding_review import review_targets, merge_review
from proposal_diagnostics import ProposalValidationError, ProposalDiagnostic, CURRENT_PROPOSAL_DIAGNOSTIC


APPROVE = {"decision": "APPROVE", "issues": [], "corrections": []}


class UnderstandingReviewTests(unittest.TestCase):
    def setUp(self):
        self.wire = wire.WireContracts()
        self.wire.setUp()
        self.snapshot = build_stage_snapshot(ROWS, version_id="synthetic")
        self.latest = "请整理水域；保留右侧的墙。\n玩家、箱子和目标位置保持不变。"
        self.context = {}
        self.candidate = understanding(self.latest, {
            "aspect": "visual", "goals": [item("整理水域", span="请整理水域")],
            "editScope": [item("整理水域", span="请整理水域", component="water")],
            "preserve": [item("保留右侧的墙", span="保留右侧的墙")], "focus": [],
            "sufficient": True, "nextQuestionDimension": "none"}, "water", "change", ["revision_request"])

    def run_classify(self, model, deadline=None):
        with self.wire.provider(model), patch.object(llm, "_llm_credentials", return_value=("offline", "offline")):
            return llm.classify_turn_understanding([{"role": "user", "content": self.latest}],
                self.snapshot, "synthetic-review", forced_proposal=True,
                proposal_context=self.context, _deadline=deadline or time.monotonic() + 116)

    def candidate_wire(self):
        return {**copy.deepcopy(self.candidate), "humanEditEngagement": None, "optionDecision": None}

    def correction(self, call, path, value, mode="replace"):
        source = json.loads(call["messages"][1]["content"])
        target = next(x for x in source["correctionTargets"] if x["path"] == path and x["mode"] == mode)
        return {"decision": "CORRECT", "issues": [], "corrections": [{"targetId": target["targetId"],
            "reason": "The candidate reverses the explicit preservation request.",
            "value": copy.deepcopy(value), "evidenceRefs": ["user:latest"]}]}

    def test_three_empty_quotes_repaired_then_approve_keeps_all_fields(self):
        self.candidate["proposalUnderstanding"]["editScope"].append(
            item("保留右侧的墙", span="保留右侧的墙", component="internal_walls"))
        def model(call, number):
            if number == 1:
                value = self.candidate_wire()
                for entry in value["proposalUnderstanding"]["editScope"] + value["proposalUnderstanding"]["preserve"]:
                    entry["evidenceSpan"] = ""
                return value
            if number == 2:
                spec = json.loads(call["messages"][-1]["content"])
                self.assertEqual(len(spec["targets"]), 3)
                return patch_fixture(spec, {"understanding": self.candidate})
            self.assertNotIn("understanding", call["response_format"]["json_schema"]["schema"]["properties"])
            return APPROVE
        result = self.run_classify(model)
        self.assertEqual(result["proposalUnderstanding"], self.candidate["proposalUnderstanding"])
        self.assertFalse(result["criticalReview"]["corrected"])
        self.assertEqual(len(self.wire.calls), 3)

    def test_direct_approve_uses_two_calls_and_preserves_base(self):
        result = self.run_classify(lambda call, n: self.candidate_wire() if n == 1 else APPROVE)
        self.assertEqual(len(self.wire.calls), 2)
        self.assertEqual(result["proposalUnderstanding"], self.candidate["proposalUnderstanding"])
        for call in self.wire.calls:
            self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}})
            self.assertEqual(call["temperature"], 0.6)
        self.assertEqual(self.wire.calls[0]["max_completion_tokens"], 1800)
        self.assertEqual(self.wire.calls[1]["max_completion_tokens"], 2000)

    def test_no_review_needed_still_one_call(self):
        self.candidate["proposalUnderstanding"]["preserve"] = []
        self.run_classify(lambda call, n: self.candidate_wire())
        self.assertEqual(len(self.wire.calls), 1)

    def test_legacy_full_rewrite_cannot_override_base(self):
        before = copy.deepcopy(self.candidate)
        def model(call, n):
            if n == 1:
                return self.candidate_wire()
            bad = copy.deepcopy(self.candidate)
            bad["proposalUnderstanding"]["preserve"] = [item("invented quote") for _ in range(4)]
            return {"accepted": True, "issues": [], "understanding": bad}, "stop"
        with self.assertRaises(llm.LLMServiceError) as caught:
            self.run_classify(model)
        self.assertEqual(caught.exception.details["reasonCode"], "REVIEW_COMPONENT_INVALID")
        self.assertEqual(self.candidate, before)
        self.assertEqual(len(self.wire.calls), 2)

    def test_valid_semantic_correction_replaces_only_one_item(self):
        self.candidate["proposalUnderstanding"]["preserve"][0]["statement"] = "允许删除右侧的墙"
        desired = item("保留右侧的墙", span="保留右侧的墙")
        def model(call, n):
            return self.candidate_wire() if n == 1 else self.correction(call, ["proposalUnderstanding", "preserve", 0], desired)
        result = self.run_classify(model)
        expected = copy.deepcopy(self.candidate["proposalUnderstanding"])
        expected["preserve"][0] = desired
        self.assertEqual(result["proposalUnderstanding"], expected)
        self.assertTrue(result["criticalReview"]["corrected"])
        self.assertEqual(len(self.wire.calls), 2)

    def test_invalid_necessary_correction_repairs_only_its_quote_never_old_candidate(self):
        self.candidate["proposalUnderstanding"]["preserve"][0]["statement"] = "允许删除右侧的墙"
        desired = item("保留右侧的墙", span="保留右侧的墙")
        verdict = None
        def model(call, n):
            nonlocal verdict
            if n == 1:
                return self.candidate_wire()
            if n == 2:
                verdict = self.correction(call, ["proposalUnderstanding", "preserve", 0], {**desired, "evidenceSpan": "保留右边墙体"})
                return verdict
            spec = json.loads(call["messages"][-1]["content"])
            self.assertEqual(spec["targets"], ["corrections[0].value.evidenceSpan"])
            self.assertEqual(spec["frozenCandidate"]["decision"], "CORRECT")
            fixed = copy.deepcopy(verdict)
            fixed["corrections"][0]["value"] = desired
            return patch_fixture(spec, fixed)
        result = self.run_classify(model)
        self.assertEqual(result["proposalUnderstanding"]["preserve"][0], desired)
        self.assertEqual(len(self.wire.calls), 3)

    def test_bad_correction_after_initial_repair_has_no_extra_call_or_fallback(self):
        def model(call, n):
            if n == 1:
                candidate = self.candidate_wire()
                candidate["proposalUnderstanding"]["preserve"][0]["evidenceSpan"] = ""
                return candidate
            if n == 2:
                return patch_fixture(call["messages"][-1]["content"], {"understanding": self.candidate})
            return self.correction(call, ["proposalUnderstanding", "preserve", 0], item("nonexistent user evidence"))
        with self.assertRaises(llm.LLMServiceError) as caught:
            self.run_classify(model)
        self.assertEqual(len(self.wire.calls), 3)
        self.assertEqual(caught.exception.details["reasonCode"], "EVIDENCE_SPAN_NOT_EXACT")
        self.assertEqual(caught.exception.details["failureStage"], "understanding_review")
        self.assertEqual(caught.exception.details["maximumAttempts"], 2)

    def test_reject_cannot_be_repaired_into_consent(self):
        with self.assertRaises(llm.LLMServiceError) as caught:
            self.run_classify(lambda call, n: self.candidate_wire() if n == 1 else {
                "decision": "REJECT", "issues": ["The quoted request does not authorize that change."], "corrections": []})
        self.assertEqual(len(self.wire.calls), 2)
        self.assertEqual(caught.exception.details["reasonCode"], "SEMANTIC_REVIEW_REJECTED")

    def test_unknown_targets_deletion_and_bulk_replacement_are_rejected(self):
        for target_id in ("proposalUnderstanding.preserve", "humanEditEngagement", "arbitrary", "changes"):
            self.wire.calls.clear()
            def model(call, n):
                return self.candidate_wire() if n == 1 else {"decision": "CORRECT", "issues": [], "corrections": [{
                    "targetId": target_id, "reason": "remove constraints", "value": [], "evidenceRefs": ["user:latest"]}]}
            with self.subTest(target=target_id), self.assertRaises(llm.LLMServiceError):
                self.run_classify(model)
            self.assertEqual(len(self.wire.calls), 2)

    def test_append_missing_preservation_keeps_existing_items(self):
        desired = item("玩家、箱子和目标位置保持不变", span="玩家、箱子和目标位置保持不变")
        def model(call, n):
            return self.candidate_wire() if n == 1 else self.correction(call, ["proposalUnderstanding", "preserve"], [desired], "append")
        result = self.run_classify(model)
        self.assertEqual(result["proposalUnderstanding"]["preserve"], [*self.candidate["proposalUnderstanding"]["preserve"], desired])

    def test_exact_unicode_newline_source_binding_remains_strict(self):
        sources = {"latest": self.latest, "u1": "保留右侧的墙"}
        base = {**self.candidate, "optionDecision": None}
        for quote, source, valid in (("墙。\n玩家", "latest", True), ("墙。 玩家", "latest", False),
                ("保留右侧的墙", "u1", True), ("保留右侧的墙", "missing", False),
                ("玩家、箱子", "latest", True), ("玩家,箱子", "latest", False),
                ("保留右侧的墙玩家", "latest", False)):
            candidate = copy.deepcopy(base)
            candidate["proposalUnderstanding"]["preserve"] = [item("Preserve", source=source, span=quote)]
            from proposal_understanding import validate_topic
            if valid:
                validate_topic(candidate["proposalUnderstanding"], sources, self.snapshot)
            else:
                with self.assertRaises(ProposalValidationError):
                    validate_topic(candidate["proposalUnderstanding"], sources, self.snapshot)

    def test_targets_are_bound_and_never_mutate_base(self):
        base = {**self.candidate, "optionDecision": None}
        targets = review_targets(base)
        changed = copy.deepcopy(base)
        changed["proposalUnderstanding"]["preserve"][0]["statement"] = "Changed after request"
        with self.assertRaises(ProposalValidationError):
            merge_review(APPROVE, changed, targets, {"latest": self.latest})
        result = merge_review(APPROVE, base, targets, {"latest": self.latest})
        result["proposalUnderstanding"]["preserve"].clear()
        self.assertTrue(base["proposalUnderstanding"]["preserve"])

    def test_missing_review_field_only_uses_existing_corrective_slot(self):
        def model(call, n):
            if n == 1:
                return self.candidate_wire()
            if n == 2:
                return {"decision": "APPROVE", "issues": []}, "stop"
            spec = json.loads(call["messages"][-1]["content"])
            self.assertEqual(spec["targets"], ["corrections"])
            return patch_fixture(spec, APPROVE)
        self.run_classify(model)
        self.assertEqual(len(self.wire.calls), 3)

    def test_original_deadline_exhaustion_does_not_start_a_call(self):
        with self.assertRaises(llm.LLMServiceError):
            self.run_classify(lambda call, n: self.candidate_wire(), time.monotonic() - 1)
        self.assertEqual(len(self.wire.calls), 0)

    def test_diagnostics_contain_hashes_and_target_ids_without_prose(self):
        diagnostic = ProposalDiagnostic()
        token = CURRENT_PROPOSAL_DIAGNOSTIC.set(diagnostic)
        try:
            self.run_classify(lambda call, n: self.candidate_wire() if n == 1 else APPROVE)
        finally:
            CURRENT_PROPOSAL_DIAGNOSTIC.reset(token)
        step = next(x for x in diagnostic.steps if x.get("reviewDecision") == "APPROVE")
        self.assertEqual(step["correctionCount"], 0)
        self.assertEqual(len(step["bindingSignature"]), 64)
        self.assertNotIn(self.latest, json.dumps(diagnostic.snapshot(), ensure_ascii=False))

    def test_probe_cap_counts_actual_transport_calls_including_errors(self):
        from understanding_review_probe import bounded_factory
        transport = AsyncMock(side_effect=[RuntimeError("provider error"), SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))])])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=transport)))
        calls = []
        wrapped = bounded_factory(lambda: client, calls, limit=2)()
        request = {"model": "kimi-k2.6", "max_completion_tokens": 1800}
        with self.assertRaises(RuntimeError):
            asyncio.run(wrapped.chat.completions.create(**request))
        asyncio.run(wrapped.chat.completions.create(**request))
        with self.assertRaisesRegex(RuntimeError, "SYNTHETIC_PROBE_CALL_LIMIT"):
            asyncio.run(wrapped.chat.completions.create(**request))
        self.assertEqual(len(calls), 2)
        self.assertEqual(transport.await_count, 2)

    def test_correction_cannot_repair_into_approval_or_remove_preservations(self):
        def model(call, n):
            if n == 1:
                return self.candidate_wire()
            if n == 2:
                return self.correction(call, ["proposalUnderstanding", "preserve", 0], item("invalid quote"))
            spec = json.loads(call["messages"][-1]["content"])
            return {"patches": [{"componentId": spec["componentId"], "field": "decision", "valueJson": '"APPROVE"',
                "evidenceRefs": ["review_source"]}]}
        with self.assertRaises(llm.LLMServiceError):
            self.run_classify(model)
        self.assertEqual(len(self.wire.calls), 3)

    def test_unstructured_correction_cannot_reopen_semantic_value_for_repair(self):
        self.candidate["proposalUnderstanding"]["preserve"][0]["statement"] = "允许删除右侧的墙"
        desired = item("保留右侧的墙", span="保留右侧的墙")
        verdict = None
        def model(call, n):
            nonlocal verdict
            if n == 1:
                return self.candidate_wire()
            if n == 2:
                verdict = self.correction(call, ["proposalUnderstanding", "preserve", 0], "保留右侧的墙")
                return verdict
            raise AssertionError("An unstructured semantic correction must fail without a repair call.")
        with self.assertRaises(llm.LLMServiceError) as caught:
            self.run_classify(model)
        self.assertEqual(caught.exception.details["reasonCode"], "REVIEW_VALUE_INVALID")
        self.assertEqual(len(self.wire.calls), 2)

    def test_failed_existing_item_does_not_unlock_unrelated_append_correction(self):
        from understanding_review import review_patch_request
        base = {**self.candidate, "optionDecision": None}
        targets = review_targets(base)
        corrections = []
        for path, mode, value in ((["proposalUnderstanding", "preserve", 0], "replace", item("bad quote")),
                (["proposalUnderstanding", "preserve"], "append", [item("玩家起点", span="玩家起点")])):
            target = next(x for x in targets if x["path"] == path and x["mode"] == mode)
            corrections.append({"targetId": target["targetId"], "reason": "Synthetic defect", "value": value, "evidenceRefs": ["user:latest"]})
        error = ProposalValidationError([{"fieldPath": "proposalUnderstanding.preserve[0].evidenceSpan",
            "reasonCode": "EVIDENCE_SPAN_NOT_EXACT", "sourceKind": "latest_user"}])
        request = review_patch_request({"decision": "CORRECT", "issues": [], "corrections": corrections},
            error, targets, {"latest": self.latest}, {"candidate": base})
        self.assertEqual(list(request.targets), ["corrections[0].value.evidenceSpan"])

    def test_probe_cumulative_cap_includes_previous_run(self):
        from understanding_review_probe import bounded_factory
        transport = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=transport)))
        calls = []
        wrapped = bounded_factory(lambda: client, calls, initial_count=14)()
        args = {"model": "kimi-k2.6", "max_completion_tokens": 1800}
        asyncio.run(wrapped.chat.completions.create(**args))
        self.assertEqual(calls[0]["call"], 15)
        with self.assertRaisesRegex(RuntimeError, "SYNTHETIC_PROBE_CALL_LIMIT"):
            asyncio.run(wrapped.chat.completions.create(**args))
        self.assertEqual(transport.await_count, 1)
