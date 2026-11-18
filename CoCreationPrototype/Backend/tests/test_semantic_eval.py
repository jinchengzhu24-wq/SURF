import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from cocreation.llm.config_and_runtime import LLMConfig, LLMServiceError
from cocreation.tools import semantic_eval_runner as evaluation
from tests.fakes import StubLLMService


class SemanticEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = evaluation.load_suite()

    def case(self, identity="zh_crowded"):
        return next(case for case in self.suite["cases"] if case["id"] == identity)

    def result(self, **overrides):
        return {"acts": ["evaluation"], "elements": ["unknown"], "evidenceSpan": "这里看起来太挤了。",
            "directionSufficient": False, "mapRelated": True, "changes": [], **overrides}

    def captures(self, runs, repeats=1):
        return {"suiteHash": evaluation.suite_hash(self.suite), "repeats": repeats, "runs": runs}

    def test_fixed_suite_has_unique_synthetic_cases_and_two_valid_maps(self):
        self.assertEqual(len(self.suite["cases"]), 36)
        self.assertEqual(len({case["id"] for case in self.suite["cases"]}), 36)
        self.assertEqual({case["origin"] for case in self.suite["cases"]}, {"synthetic"})

    def test_expected_values_and_review_instructions_never_enter_model_input(self):
        case = copy.deepcopy(self.case())
        case["expected"]["sentinel"] = "private-review-expectation"
        case["reviewNotes"] = "private-review-expectation"
        model_input = evaluation.build_model_input(self.suite, case)
        self.assertEqual(set(model_input), {"conversation", "snapshot", "forced_proposal", "proposal_context"})
        self.assertNotIn("private-review-expectation", json.dumps(model_input))

    def test_stage_switch_rebuilds_snapshot_without_old_assistant_map_authority(self):
        case = self.case("zh_stage_switch")
        model_input = evaluation.build_model_input(self.suite, case)
        self.assertEqual(model_input["snapshot"]["versionId"], "eval_stage_2")
        box = next(item for item in model_input["snapshot"]["boxes"] if item["id"] == "B1")
        self.assertEqual((box["row"], box["column"]), (4, 5))
        self.assertNotEqual(model_input["snapshot"]["mapFingerprint"], evaluation.build_model_input(self.suite, self.case())["snapshot"]["mapFingerprint"])

    def test_score_rejects_invented_revision_and_non_contiguous_evidence(self):
        self.assertIn("unexpected_act", evaluation.check_result(self.case(), self.result(acts=["evaluation", "revision_request"])))
        self.assertEqual(evaluation.check_result(self.case(), self.result(evidenceSpan="这里太挤了")), ["invalid_result_contract_or_user_evidence"])

    def test_protected_change_is_not_inferred_from_visual_comment(self):
        result = self.result(changes=[{"component": "boxes", "property": "position", "operation": "change"}])
        self.assertIn("invented_protected_change", evaluation.check_result(self.case(), result))

    def test_forced_button_capture_is_not_silently_fixed_by_scorer(self):
        case = self.case("zh_button_evaluation")
        self.assertIn("missing_required_act", evaluation.check_result(case, self.result()))

    def test_missing_captures_are_failures_not_removed_from_denominator(self):
        report = evaluation.score(self.suite, self.captures([{"caseId": "zh_crowded", "repeat": 1, "result": self.result()}]))
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["total"], 36)
        self.assertEqual(report["missing"], 35)

    def test_repeat_instability_and_regression_are_visible(self):
        baseline = self.captures([{"caseId": "zh_crowded", "repeat": i, "result": self.result()} for i in (1, 2)], 2)
        candidate = copy.deepcopy(baseline)
        candidate["runs"][1] = {"caseId": "zh_crowded", "repeat": 2, "errorCode": "UPSTREAM_TIMEOUT"}
        report = evaluation.compare(self.suite, baseline, candidate)
        self.assertEqual(report["regressedCases"], ["zh_crowded"])
        self.assertEqual(report["candidate"]["inconsistentCases"], 1)

    def test_changed_suite_duplicate_and_unknown_run_are_rejected(self):
        bad = self.captures([])
        bad["suiteHash"] = "wrong"
        with self.assertRaises(ValueError):
            evaluation.score(self.suite, bad)
        run = {"caseId": "zh_crowded", "repeat": 1, "result": self.result()}
        for runs in ([run, run], [{**run, "caseId": "unknown"}], [{**run, "repeat": True}]):
            with self.subTest(runs=runs), self.assertRaises(ValueError):
                evaluation.score(self.suite, self.captures(runs))

    def test_cli_requires_live_flag_before_any_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "runs.json"
            completed = subprocess.run(
                [sys.executable, "-m", "cocreation.tools.semantic_eval_runner", "run", "--output", str(output)],
                cwd=str(BACKEND_DIR),
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("requires --live", completed.stderr)
            self.assertFalse(output.exists())

    def test_isolated_live_adapter_captures_errors_without_database_or_http(self):
        suite = copy.deepcopy(self.suite)
        suite["cases"] = [self.case()]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "runs.json"
            stub_llm = StubLLMService(
                config=LLMConfig(api_key="fake", base_url="https://example.invalid"),
                handlers={
                    "classify_turn_understanding": [
                        self.result(),
                        LLMServiceError("UPSTREAM_TIMEOUT", "test", "r", True, 1, 504),
                    ]
                },
            )
            captures = evaluation.run_live(suite, output, 2, llm_service=stub_llm)
            self.assertEqual(len(stub_llm.method_calls["classify_turn_understanding"]), 2)
            self.assertEqual(captures["runs"][1]["errorCode"], "UPSTREAM_TIMEOUT")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), captures)
            self.assertEqual(evaluation.score(suite, captures)["inconsistentCases"], 1)

            stub_llm_empty = StubLLMService(
                config=LLMConfig(api_key="fake", base_url="https://example.invalid")
            )
            with self.assertRaises(FileExistsError):
                evaluation.run_live(suite, output, llm_service=stub_llm_empty)
            self.assertEqual(len(stub_llm_empty.method_calls["classify_turn_understanding"]), 0)


if __name__ == "__main__":
    unittest.main()
