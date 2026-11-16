"""Closeout regressions for reviewed semantics and appended quote repair."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_client as llm
from component_patches import PatchRequest, PatchError
from patch_fixtures import patch_fixture
from proposal_diagnostics import ProposalValidationError
from proposal_understanding import reusable_understanding, MEANING_REVIEW_VERSION
from test_proposal_understanding import item
import test_understanding_review as review_tests
from understanding_review import review_targets, review_patch_request


class ReviewQuoteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.f = review_tests.UnderstandingReviewTests()
        self.f.setUp()

    def correction_request(self, *, collection="preserve", values=None, append=False, bad_positions=(0,)):
        base = {**copy.deepcopy(self.f.candidate), "optionDecision": None}
        targets = review_targets(base)
        path = ["proposalUnderstanding", collection] + ([] if append else [0])
        target = next(x for x in targets if x["path"] == path and x["mode"] == ("append" if append else "replace"))
        values = values or [item("保留墙体", span="not exact")]
        value = copy.deepcopy(values if append else values[0])
        verdict = {"decision": "CORRECT", "issues": [], "corrections": [{"targetId": target["targetId"],
            "reason": "Preservation was inverted; retain the explicit requirement.", "value": value, "evidenceRefs": ["user:latest"]}]}
        start = len(base["proposalUnderstanding"][collection]) if append else 0
        issues = [{"fieldPath": f"proposalUnderstanding.{collection}[{start + i}].evidenceSpan",
            "reasonCode": "EVIDENCE_SPAN_NOT_EXACT", "sourceKind": "latest_user"} for i in bad_positions]
        source = {"candidate": base}
        request = review_patch_request(verdict, ProposalValidationError(issues), targets,
            {"latest": self.f.latest}, source)
        return request, verdict, base, targets, source, issues

    def test_repaired_quote_must_not_reverse_reviewed_preservation(self):
        f = self.f
        f.candidate["proposalUnderstanding"]["preserve"][0]["statement"] = "允许删除右侧的墙"
        def model(call, n):
            if n == 1:
                return f.candidate_wire()
            if n == 2:
                return f.correction(call, ["proposalUnderstanding", "preserve", 0], item("保留右侧的墙", span="not exact"))
            spec = json.loads(call["messages"][-1]["content"])
            self.assertEqual(spec["targets"], ["corrections[0].value.evidenceSpan"])
            return {"patches": [{"componentId": spec["componentId"], "field": "corrections[0].value",
                "valueJson": json.dumps(item("可以移除右侧墙体", span="保留右侧的墙"), ensure_ascii=False),
                "evidenceRefs": ["user:latest"]}]}
        with self.assertRaises(llm.LLMServiceError) as caught:
            f.run_classify(model)
        self.assertEqual(caught.exception.details["reasonCode"], "PATCH_TARGET_NOT_ALLOWED")
        self.assertEqual(len(f.wire.calls), 3)

    def test_append_value_repair_must_keep_other_reviewed_added_requirements(self):
        f = self.f
        first = item("玩家位置保持不变", span="not exact")
        second = item("箱子和目标位置保持不变", span="箱子和目标位置保持不变")
        def model(call, n):
            if n == 1:
                return f.candidate_wire()
            if n == 2:
                return f.correction(call, ["proposalUnderstanding", "preserve"], [first, second], "append")
            spec = json.loads(call["messages"][-1]["content"])
            self.assertEqual(spec["targets"], ["corrections[0].value[0].evidenceSpan"])
            return {"patches": [{"componentId": spec["componentId"], "field": "corrections[0].value",
                "valueJson": json.dumps([{**first, "evidenceSpan": "玩家、箱子和目标位置保持不变"}], ensure_ascii=False),
                "evidenceRefs": ["user:latest"]}]}
        with self.assertRaises(llm.LLMServiceError) as caught:
            f.run_classify(model)
        self.assertEqual(caught.exception.details["reasonCode"], "PATCH_TARGET_NOT_ALLOWED")
        self.assertEqual(len(f.wire.calls), 3)

    def test_two_replacements_must_not_swap_preservation_items(self):
        f = self.f
        f.candidate["proposalUnderstanding"]["preserve"].append(item("玩家不变", span="玩家、箱子和目标位置保持不变"))
        before = copy.deepcopy(f.candidate["proposalUnderstanding"]["preserve"])
        for swap in (False, True):
            f.wire.calls.clear()
            def model(call, n):
                if n == 1:
                    return f.candidate_wire()
                verdict = f.correction(call, ["proposalUnderstanding", "preserve", 0], before[1])
                if swap:
                    verdict["corrections"] += f.correction(call, ["proposalUnderstanding", "preserve", 1], before[0])["corrections"]
                return verdict
            with self.subTest(swap=swap), self.assertRaises(llm.LLMServiceError) as caught:
                f.run_classify(model)
            self.assertEqual(caught.exception.details["reasonCode"], "REVIEW_CROSS_ITEM_COPY")
            self.assertEqual(f.candidate["proposalUnderstanding"]["preserve"], before)

    def test_valid_quote_repair_preserves_whole_verdict_except_selected_leaf(self):
        request, verdict, *_ = self.correction_request()
        fixed = copy.deepcopy(verdict)
        fixed["corrections"][0]["value"]["evidenceSpan"] = "保留右侧的墙"
        result = request.merge(patch_fixture(request.prompt(), fixed))
        self.assertEqual(result, fixed)
        result["corrections"][0]["value"]["evidenceSpan"] = verdict["corrections"][0]["value"]["evidenceSpan"]
        self.assertEqual(result, verdict)

    def test_valid_append_first_last_and_multiple_quote_leaves_keep_batch(self):
        for bad in ((0,), (1,), (0, 1)):
            values = [item("玩家不变", span="玩家、箱子和目标位置保持不变"), item("箱子和目标不变", span="箱子和目标位置保持不变")]
            for index in bad:
                values[index]["evidenceSpan"] = "not exact"
            request, verdict, *_ = self.correction_request(values=values, append=True, bad_positions=bad)
            expected = [f"corrections[0].value[{i}].evidenceSpan" for i in bad]
            self.assertEqual(list(request.targets), expected)
            fixed = copy.deepcopy(verdict)
            for index in bad:
                fixed["corrections"][0]["value"][index]["evidenceSpan"] = "玩家、箱子和目标位置保持不变" if index == 0 else "箱子和目标位置保持不变"
            self.assertEqual(request.merge(patch_fixture(request.prompt(), fixed)), fixed)
            self.f.wire.calls.clear()
            def model(call, n):
                if n == 1:
                    return self.f.candidate_wire()
                if n == 2:
                    return self.f.correction(call, ["proposalUnderstanding", "preserve"], values, "append")
                spec = json.loads(call["messages"][-1]["content"])
                self.assertEqual(spec["targets"], expected)
                complete = copy.deepcopy(spec["frozenCandidate"])
                complete["corrections"][0]["value"] = copy.deepcopy(fixed["corrections"][0]["value"])
                return patch_fixture(spec, complete)
            result = self.f.run_classify(model)
            self.assertEqual(result["proposalUnderstanding"]["preserve"],
                [*self.f.candidate["proposalUnderstanding"]["preserve"], *fixed["corrections"][0]["value"]])
            self.assertEqual(len(self.f.wire.calls), 3)

    def test_quote_patch_cannot_modify_source_statement_entity_component_or_decision(self):
        request, verdict, *_ = self.correction_request()
        for field in ("decision", "corrections[0].value", "corrections[0].value.statement",
                "corrections[0].value.sourceTurnId", "corrections[0].value.entities", "corrections[0].value.component"):
            payload = {"patches": [{"componentId": "understanding_review", "field": field,
                "valueJson": '"changed"', "evidenceRefs": ["user:latest"]}]}
            with self.subTest(field=field), self.assertRaises(PatchError):
                request.merge(payload)

    def test_post_merge_assertion_rejects_any_locked_field_or_batch_change(self):
        request, verdict, *_ = self.correction_request(values=[item("A", span="bad"), item("B", span="玩家起点")], append=True)
        mutations = [lambda v: v.update(decision="APPROVE"),
            lambda v: v["corrections"][0]["value"].pop(),
            lambda v: v["corrections"][0]["value"].reverse(),
            lambda v: v["corrections"][0]["value"][1].update(statement="Changed sibling"),
            lambda v: v["corrections"][0]["value"][0].update(sourceTurnId="another")]
        for mutate in mutations:
            tampered = copy.deepcopy(verdict)
            mutate(tampered)
            with self.subTest(mutate=mutate), patch.object(PatchRequest, "merge", return_value=tampered):
                with self.assertRaises((PatchError, IndexError)):
                    request.merge({})

    def test_ambiguous_unknown_or_non_quote_issues_do_not_open_repair(self):
        request, verdict, base, targets, source, issues = self.correction_request(append=True)
        for issue in ({**issues[0], "fieldPath": "proposalUnderstanding.preserve[99].evidenceSpan"},
                {**issues[0], "fieldPath": "proposalUnderstanding.preserve[0].evidenceSpan"},
                {**issues[0], "fieldPath": "proposalUnderstanding.preserve[1].statement"},
                {**issues[0], "reasonCode": "SOURCE_NOT_FOUND"}):
            self.assertIsNone(review_patch_request(verdict, ProposalValidationError([issue]), targets, {"latest": self.f.latest}, source))
        duplicate = copy.deepcopy(verdict)
        duplicate["corrections"].append(copy.deepcopy(duplicate["corrections"][0]))
        self.assertIsNone(review_patch_request(duplicate, ProposalValidationError(issues), targets, {"latest": self.f.latest}, source))

    def test_two_different_append_batches_map_to_their_own_quote_leaves(self):
        _, verdict, base, targets, source, issues = self.correction_request(append=True)
        goal_target = next(x for x in targets if x["path"] == ["proposalUnderstanding", "goals"] and x["mode"] == "append")
        verdict["corrections"].append({"targetId": goal_target["targetId"], "reason": "Missing explicit goal",
            "value": [item("整理水域", span="not exact")], "evidenceRefs": ["user:latest"]})
        issues.append({"fieldPath": "proposalUnderstanding.goals[1].evidenceSpan", "reasonCode": "EVIDENCE_SPAN_NOT_EXACT", "sourceKind": "latest_user"})
        request = review_patch_request(verdict, ProposalValidationError(issues), targets, {"latest": self.f.latest}, source)
        self.assertEqual(list(request.targets), ["corrections[0].value[0].evidenceSpan", "corrections[1].value[0].evidenceSpan"])

    def test_invalid_source_or_incomplete_semantic_value_is_not_repaired(self):
        _, verdict, base, targets, source, issues = self.correction_request()
        for change in ({"sourceTurnId": "unknown"}, {"statement": ""}, {"evidenceSpan": None}):
            invalid = copy.deepcopy(verdict)
            invalid["corrections"][0]["value"].update(change)
            self.assertIsNone(review_patch_request(invalid, ProposalValidationError(issues), targets, {"latest": self.f.latest}, source))

    def test_old_delta_verified_does_not_reuse_new_quote_policy(self):
        value = {"semanticPolicyVersion": 1, "dialogueContextVersion": llm.CONTEXT_VERSION,
            "versionId": "synthetic-v1", "evidenceSignature": "a" * 64,
            "changes": [{"component": "player", "operation": "change", "property": "position"}],
            "criticalReview": {"verified": True, "policyVersion": "meaning-preservation-4-delta"}}
        self.assertFalse(reusable_understanding(value, "synthetic-v1"))
        value["criticalReview"]["policyVersion"] = MEANING_REVIEW_VERSION
        self.assertTrue(reusable_understanding(value, "synthetic-v1"))
