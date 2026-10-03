import asyncio
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_client as llm
from design_requirements import validate_requirement_record, validate_requirement_components
from level_validation import build_stage_snapshot
from reply_reliability import OptionalRepairBudget, parse_json_object, requirement_cache_key
from reliability_report import summarize

ROWS = ["############", "#..........#", "#..........#", "#..........#", "#...p......#",
        "#...s.t....#", "#..........#", "#..........#", "#..........#", "############"]
TURNS = [{"id": "u1", "content": "Increase switching between boxes."},
         {"id": "u2", "content": "Make the player spend longer solving."}]


def requirement(index=0):
    return {"component": "gameplay", "property": "boxAlternations" if index == 0 else "experience",
        "relation": "increase" if index == 0 else "seek", "strength": "goal", "entities": [],
        "value": None, "unit": "none", "priorityEvidenceSpan": "", "focused": True,
        "sourceTurnId": TURNS[index]["id"], "evidenceSpan": TURNS[index]["content"],
        "statement": TURNS[index]["content"]}


def completion(payload):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])


class ReplyReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_stage_snapshot(ROWS, version_id="v1")
        self.context = {"stageSnapshot": self.snapshot, "requirementUserTurns": copy.deepcopy(TURNS),
            "requirementPolicyVersion": 1, "_reliabilityState": {}}

    def test_only_unambiguous_json_wrapping_is_recovered(self):
        self.assertEqual(parse_json_object('```json\n{"body":"Useful analysis"}\n```')["body"], "Useful analysis")
        for value in ('{"body":', '{} {}', '[]', 'Explanation: {"body":"text"}'):
            with self.subTest(value=value), self.assertRaises((ValueError, json.JSONDecodeError)):
                parse_json_object(value)

    def test_optional_work_keeps_reserve_and_total_cap(self):
        with patch("reply_reliability.time.monotonic", return_value=100):
            budget = OptionalRepairBudget(150)
            self.assertEqual(budget.timeout(), 10)
            budget.record(94)
            self.assertEqual(budget.timeout(), 4)
        with patch("reply_reliability.time.monotonic", return_value=132):
            self.assertEqual(budget.timeout(), 0)

    def repair(self, payload, *, body=True, card=False):
        async def request(*args, **kwargs):
            return completion(payload)
        with patch.object(llm, "_request_completion", side_effect=request) as mocked:
            result = asyncio.run(llm._repair_intent_components_async(api_key="fake", base_url="fake",
                user_text="Make the route clearer.", body="Verified body.", card_text="Verified card.",
                claims=[], issues=[], repair_body=body, repair_card=card, map_facts="{}",
                language="en", request_id="r", deadline=time.monotonic() + 30))
        return result, mocked

    def test_body_repair_does_not_require_or_validate_card(self):
        for payload in ({"bodyParagraphs": ["The first route choice creates a meaningful planning commitment."]},
                        {"bodyParagraphs": ["The route choice affects planning."], "cardSentences": []}):
            result, mocked = self.repair(payload)
            self.assertEqual(result[1], "Verified card.")
            self.assertEqual(mocked.call_args.kwargs["task"], "intent_body_repair")

    def test_card_repair_does_not_require_or_validate_body(self):
        card = ["For now, I understand that you may prefer a clearer route choice.",
                "This interpretation remains tentative and you can correct me."]
        result, mocked = self.repair({"cardSentences": card, "bodyParagraphs": []}, body=False, card=True)
        self.assertEqual(result[0], "Verified body.")
        self.assertEqual(mocked.call_args.kwargs["task"], "intent_card_repair")

    def test_failed_optional_card_keeps_verified_body(self):
        checks = []
        async def invalid(**kwargs):
            raise ValueError("bad card")
        with patch.object(llm, "_repair_intent_components_async", side_effect=invalid):
            result = asyncio.run(llm._repair_reviewed_intent_presentation(
                repair_body=False, repair_card=True, body="Verified body.", card_text="bad",
                deadline=time.monotonic() + 60, optional_budget=OptionalRepairBudget(time.monotonic() + 60),
                card_required=False, component_checks=checks))
        self.assertEqual(result, ("Verified body.", ""))
        self.assertEqual(checks[0].status, "omitted")

    def test_required_conflict_card_failure_is_not_hidden(self):
        async def invalid(**kwargs):
            raise ValueError("bad required card")
        with patch.object(llm, "_repair_intent_components_async", side_effect=invalid), self.assertRaises(ValueError):
            asyncio.run(llm._repair_reviewed_intent_presentation(
                repair_body=False, repair_card=True, body="Verified body.", card_text="bad",
                deadline=time.monotonic() + 60, optional_budget=OptionalRepairBudget(time.monotonic() + 60),
                card_required=True, component_checks=[]))

    def test_component_validation_locates_invalid_requirement(self):
        invalid = requirement(1)
        invalid["value"] = 99
        valid, issues = validate_requirement_components({"requirements": [requirement(), invalid],
            "automaticBindings": [], "exactTransitions": []}, TURNS, self.snapshot)
        self.assertEqual(len(valid), 1)
        self.assertEqual(issues[0]["itemIndex"], 1)

    def test_requirement_repair_preserves_other_reviewed_items(self):
        initial = {"requirements": [requirement(), requirement(1)], "automaticBindings": [], "exactTransitions": []}
        initial["requirements"][1]["relation"] = "avoid"
        record = validate_requirement_record(initial, TURNS, self.snapshot)
        responses = iter([initial, {"accepted": False, "issues": [{
            "requirementIds": [record["requirements"][1]["requirementId"]], "kind": "meaning",
            "evidenceSpans": [TURNS[1]["content"]], "repairInstruction": "Restore the expressed direction."}]},
            {**initial, "requirements": [requirement(), requirement(1)]}, {"accepted": True, "issues": []}])
        async def request(*args, **kwargs):
            return completion(next(responses))
        with patch.object(llm, "_request_completion", side_effect=request) as mocked:
            result = llm._review_proposal_requirements("fake", "fake", self.context, "r", time.monotonic() + 100)
        self.assertEqual(len(result["requirements"]), 2)
        repair = json.loads(mocked.call_args_list[2].args[3][-1]["content"])
        self.assertEqual(repair["lockedRequirements"], [requirement()])
        self.assertEqual(mocked.call_count, 4)

    def test_verified_requirements_reuse_exact_cache_and_invalidate_changes(self):
        raw = {"requirements": [requirement()], "automaticBindings": [], "exactTransitions": []}
        async def request(*args, **kwargs):
            return completion(raw if kwargs["task"] == "revision_requirements" else {"accepted": True, "issues": []})
        with patch.object(llm, "_request_completion", side_effect=request) as mocked:
            llm._review_proposal_requirements("fake", "fake", self.context, "r", time.monotonic() + 100)
            self.context["verifiedRequirementCache"] = [self.context["_reliabilityState"]["verifiedRequirements"]]
            llm._review_proposal_requirements("fake", "fake", self.context, "retry", time.monotonic() + 100)
            self.assertEqual(mocked.call_count, 2)
            self.context["stageSnapshot"] = {**self.snapshot, "versionId": "v2"}
            llm._review_proposal_requirements("fake", "fake", self.context, "new-stage", time.monotonic() + 100)
            self.assertEqual(mocked.call_count, 4)
        key = requirement_cache_key(self.context, TURNS)
        self.assertNotEqual(key, requirement_cache_key(self.context, TURNS + [{"id": "u3", "content": "Also preserve the water."}]))

    def test_challenge_grounding_uses_second_existing_attempt(self):
        responses = iter([{"relation": "different", "merit": "reasonable", "comparison": "B1 is at (99,99), so this change is sufficient."},
            {"relation": "different", "merit": "reasonable", "comparison": "I agree that a single change may not address your concern about the route's planning demands."}])
        async def request(*args, **kwargs):
            return completion(next(responses))
        with patch.object(llm, "_llm_credentials", return_value=("fake", "fake")), patch.object(llm, "_request_completion", side_effect=request):
            result = llm.classify_challenge_reason("One tile is insufficient.", {}, "A local wall change.", "r", simple=True,
                stage_snapshot=self.snapshot,
                comparison_validator=lambda text: llm._strip_invalid_grounding_sentences(text, ROWS)[0])
        self.assertEqual(result["attemptsUsed"], 2)
        self.assertNotIn("99", result["comparison"])

    def test_invalid_premise_removes_dependent_conclusion_only(self):
        body, removed = llm._strip_invalid_grounding_sentences(
            "B1 is at (99,99). Therefore the opening is forced. I would consider the visual balance separately.", ROWS)
        self.assertNotIn("forced", body)
        self.assertIn("visual balance", body)
        self.assertEqual(len(removed), 2)

    def test_failure_analysis_requires_verified_understanding_and_no_fake_proposal(self):
        error = llm.LLMServiceError("DETERMINISTIC_SEARCH_EXHAUSTED", "No admissible solvable candidate was found.", "r", True, 2, 502)
        args = ("fake", "fake", self.context, ROWS, "en", "r", time.monotonic() + 30, error)
        self.assertIsNone(llm._generate_verified_proposal_failure_analysis(*args))
        self.context["_reliabilityState"]["verifiedRequirements"] = {"record": {"requirements": [requirement()]}}
        async def request(*a, **kw):
            return completion({"body": "I could not verify a solvable candidate for this direction. I would reconsider which local route relationship should change, while retaining your original goal and preserving the current Stage."})
        with patch.object(llm, "_request_completion", side_effect=request):
            result = llm._generate_verified_proposal_failure_analysis(*args)
        self.assertTrue(result.proposal_diagnostics["verifiedFailureAnalysis"])
        self.assertIsNone(result.proposed_rows)
        self.assertIsNone(result.guidance["proposalOffer"])

    def test_translation_locks_valid_body_without_snapshot_in_prompt(self):
        source = {"turnId": "t1", "body": "The route requires planning.", "followUpQuestion": None,
            "intentHypothesis": None, "proposalOfferSummary": None, "proposalOfferRationale": None,
            "uiCueTexts": [], "proposalSummary": None, "stageRows": ROWS}
        locked = llm._lock_valid_translation_fields({"translations": [{"turnId": "t1", "body": "The route requires careful planning.",
            "intentHypothesis": "An invented inclination"}]}, [source], "en")
        self.assertIn("body", locked["t1"])
        self.assertNotIn("intentHypothesis", locked["t1"])

    def test_metrics_deduplicate_retries_and_separate_quality_from_delivery(self):
        events = [
            {"sessionId": "s", "eventType": "message_generation_failed", "payload": {"messageKey": "m", "modelGenerationSucceeded": True}},
            {"sessionId": "s", "eventType": "reply_delivery_outcome", "payload": {"messageKey": "m", "modelGenerationSucceeded": True,
                "reliableBodyDelivered": True, "fullQualityPassed": False}},
            {"sessionId": "s", "eventType": "reply_delivery_outcome", "payload": {"messageKey": "opening", "snapshotFallback": True,
                "reliableBodyDelivered": True, "fullQualityPassed": False}},
        ]
        report = summarize(events)
        self.assertEqual(report["logicalRequests"], 2)
        self.assertEqual(report["snapshotFallbackCount"], 1)
        self.assertEqual(report["firstAttempt"]["reliableBodyDelivered"]["passed"], 0)
        self.assertEqual(report["afterRetries"]["reliableBodyDelivered"]["passed"], 1)
        self.assertEqual(report["afterRetries"]["fullQualityPassed"]["passed"], 0)

    def test_optional_intent_discovery_failure_keeps_body_but_explicit_defect_blocks(self):
        kwargs = dict(api_key="fake", base_url="fake", user_text="Discuss the route.",
            body="The route asks the player to inspect the next pushing position before committing. I would compare that choice with the available walking space.",
            decision={"classification": "none"}, active_inclinations=[], language="en",
            request_id="r", deadline=time.monotonic() + 60)
        with patch.object(llm, "_review_intent_candidate_async", side_effect=ValueError("bad auxiliary JSON")):
            result = asyncio.run(llm._review_ordinary_intent_with_recovery(
                optional_budget=OptionalRepairBudget(kwargs["deadline"]), component_checks=[], **kwargs))
        self.assertTrue(result["omitted"])
        invalid = {"classification": "none", "bodyValid": False, "issues": ["The body does not answer the user's route question."]}
        with patch.object(llm, "_review_intent_candidate_async", return_value=invalid), self.assertRaises(llm.LowQualityModelResponse):
            asyncio.run(llm._review_ordinary_intent_with_recovery(
                optional_budget=OptionalRepairBudget(kwargs["deadline"]), component_checks=[], **kwargs))

    def test_expired_auxiliary_deadline_does_not_start_a_new_budget(self):
        with patch.object(llm, "_review_intent_candidate_async") as review:
            result = asyncio.run(llm._review_ordinary_intent_with_recovery(
                optional_budget=OptionalRepairBudget(time.monotonic() - 1), component_checks=[],
                api_key="fake", base_url="fake", user_text="Discuss the route.",
                body="The first route choice can create a planning commitment. I would examine where the player must stand before pushing, while treating the map as unchanged.",
                decision={"classification": "none"}, active_inclinations=[], language="en",
                request_id="r", deadline=time.monotonic() - 1))
        review.assert_not_called()
        self.assertTrue(result["omitted"])

    def test_manual_edit_pair_preserves_opening_when_comparison_needs_repair(self):
        stage_context = {"stageNumber": 2, "source": "human_edit", "diff": [], "evaluatorDesignContext": {}}
        solver = {"solvable": True, "solutionSteps": 24, "solutionPushes": 6}
        payload = {"openingMessage": "The saved Stage remains solvable. The central space is open. The first walking route reaches a pushing position. I would inspect that position before committing to a push.",
            "assessment": {"solutionSummary": "The solver found a route.", "difficultyOpinion": "The pushing position requires care.",
                "features": ["Open space"], "suggestions": ["Inspect the first push"], "satisfactionQuestion": None},
            "reviewMessage": "bad", "conflict": None}
        evidence = llm._manual_edit_review_evidence(stage_context, solver, {})
        decision = {"verdict": "no_conflict", "decisionSource": "deterministic_no_comparable_evidence",
            "confirmedDirectionIds": [], "effectEvidenceIds": [], "relation": "no comparable direction", "solverDelta": {}, "comparisons": []}
        locked = {}
        with self.assertRaises(ValueError):
            llm._validate_manual_edit_pair_payload(payload, ROWS, "en", solver, stage_context, evidence,
                decision, "r", 1, "fake", 0, _component_state=locked)
        self.assertEqual(locked["openingMessage"], payload["openingMessage"])
        self.assertNotIn("reviewMessage", locked)
        payload["reviewMessage"] = "There is no confirmed design direction available for comparison. I can verify that the saved Stage remains solvable and keep this observation separate from any assumed designer intention."
        result = llm._validate_manual_edit_pair_payload(payload, ROWS, "en", solver, stage_context, evidence,
            decision, "r", 2, "fake", 0, _component_state=locked)
        self.assertIsNotNone(result.secondary_execution)
        self.assertIn("saved Stage", result.assistant_message)

    def test_server_receipt_and_snapshot_opening_are_not_model_generation(self):
        import app as backend
        for model, fallback in (("server", False), ("kimi-k2.6-safe-opening", True), ("kimi-k2.6", False)):
            with patch.object(backend, "record_event") as event:
                backend._record_reply_outcome(None, "s", "key", "v", llm.LLMExecutionResult(
                    "A verified reply.", 1, "r", model=model, guidance={}), "message")
            payload = event.call_args.args[3]
            self.assertEqual(payload["snapshotFallback"], fallback)
            self.assertEqual(payload["modelGenerationSucceeded"], model == "kimi-k2.6")
