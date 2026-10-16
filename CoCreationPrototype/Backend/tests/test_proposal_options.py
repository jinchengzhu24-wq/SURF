"""Direction-first clarification, real validators with offline model fault injection."""
import asyncio
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as backend
import llm_client as llm
import proposal_options as directions
from option_fixtures import options, envelope
from patch_fixtures import patch_fixture
from test_proposal_understanding import ROWS, response, topic, understanding
from level_validation import build_entity_bindings, build_stage_snapshot
from proposal_diagnostics import ProposalValidationError, reason_code


class DirectionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.bindings = build_entity_bindings(ROWS)
        self.snapshot = build_stage_snapshot(ROWS, version_id="v1", entity_bindings=self.bindings)
        self.discovery = {"topicId": "u1", "versionId": "v1", "status": "clarifying",
            "userTurns": [{"id": "u1", "content": "Visual order"}, {"id": "u2", "content": "Water and interior walls"}],
            "understanding": topic(sufficient=False), "clarificationQuestionCount": 0,
            "brief": "Visual order\nWater and interior walls"}
        self.context = {"stageSnapshot": self.snapshot, "entityBindings": self.bindings,
            "proposalDiscovery": self.discovery, "revisionRouting": "needs_clarification",
            "proposalClarification": backend._proposal_clarification_spec(self.discovery, self.snapshot, "en")}

    def generate(self, call):
        with patch.object(llm, "_llm_credentials", return_value=("offline", "offline")), \
                patch.object(llm, "_request_completion", call):
            return llm.generate_chat_reply([{"role": "user", "content": "Water and interior walls"}], ROWS,
                "offline", stage_context=self.context, _deadline=time.monotonic() + 116)

    def test_options_question_and_private_marker_are_delivered_atomically(self):
        call = AsyncMock(side_effect=[response(envelope()), response({"bodyIssues": [], "optionIssues": [], "questionIssue": None})])
        result = self.generate(call)
        marked = backend._mark_proposal_discovery_guidance(result, self.context)
        marker = marked.guidance["proposalDiscovery"]
        self.assertEqual(marker["clarificationQuestionCount"], 1)
        self.assertEqual(len(marker["optionSet"]["options"]), 2)
        self.assertEqual(call.await_count, 2)
        for item in options():
            self.assertIn(item["mechanism"], marked.assistant_message)
        self.assertNotIn("optionSet", backend._design_context_guidance(marked.guidance))

    def test_question_repair_locks_options_and_never_uses_generic_fallback(self):
        first = envelope()
        repaired = {"question": "Would you prefer grouped edges or separated accents?", "comparedOptions": ["1", "2"],
            "discriminant": "grouped edges versus separated accents"}
        async def model(*args, **kwargs):
            if kwargs["task"] == "proposal_clarification":
                return response(first)
            if kwargs["task"] == "proposal_clarification_patch":
                payload = json.loads(args[3][-1]["content"])
                self.assertNotIn("options", payload["targets"])
                return response(patch_fixture(args[3][-1]["content"], repaired))
            return response({"bodyIssues": [], "optionIssues": [], "questionIssue": "Make the actual tradeoff clear." if model.reviews == 0 else None})
        model.reviews = 0
        async def counted(*args, **kwargs):
            value = await model(*args, **kwargs)
            if kwargs["task"] == "proposal_clarification_review":
                model.reviews += 1
            return value
        result = self.generate(AsyncMock(side_effect=counted))
        self.assertIn(repaired["question"], result.assistant_message)
        self.assertIn(first["body"], result.assistant_message)
        self.assertEqual(result.proposal_diagnostics["optionSet"]["options"][0]["summary"], first["options"][0]["summary"])

    def test_unverifiable_review_returns_error_instead_of_question(self):
        async def model(*args, **kwargs):
            return response(envelope() if kwargs["task"] == "proposal_clarification" else {})
        with self.assertRaises(llm.LLMServiceError):
            self.generate(AsyncMock(side_effect=model))

    def test_option_permissions_and_question_binding_are_structural_boundaries(self):
        bad = envelope()
        bad["options"][0]["components"] = ["targets"]
        with self.assertRaises(ProposalValidationError):
            directions.parse_envelope(bad, self.discovery, self.snapshot)
        bad = envelope()
        bad["comparedOptions"] = ["1"]
        with self.assertRaises(ProposalValidationError):
            directions.parse_envelope(bad, self.discovery, self.snapshot)

    def test_existing_options_are_locked_and_rejected_options_cannot_return(self):
        option_set = directions.build_set(options(), self.discovery, self.snapshot)
        discovery = {**self.discovery, "optionSet": option_set, "optionDecision": {
            "selection": "explicit", "preferredOptionIds": [option_set["options"][1]["id"]],
            "rejectedOptionIds": [option_set["options"][0]["id"]], "evidenceSpan": "B"}}
        parsed, _ = directions.parse_envelope({"options": None, "question": "", "comparedOptions": [], "discriminant": ""}, discovery, self.snapshot)
        self.assertEqual([item["id"] for item in parsed["options"]], [option_set["options"][1]["id"]])
        projection = directions.planning_projection(discovery)
        self.assertEqual(projection["candidates"], parsed["options"])
        with self.assertRaises(ProposalValidationError):
            directions.parse_envelope(envelope(), discovery, self.snapshot)
        self.assertIsNone(directions.current_set(discovery, {**self.snapshot, "mapFingerprint": "changed"}))
        self.assertIsNone(directions.current_set({**discovery, "versionId": "v2"}, self.snapshot))

    def test_three_questions_choose_route_and_unclear_answer_cannot_extend_budget(self):
        option_set = directions.build_set(options(), self.discovery, self.snapshot)
        discovery = {**self.discovery, "optionSet": option_set, "clarificationQuestionCount": 3,
            "optionDecision": {"selection": "unclear", "preferredOptionIds": [], "rejectedOptionIds": [], "evidenceSpan": "Either"}}
        record = understanding("Either", topic(sufficient=False))
        record["optionContextSignature"] = directions.context_signature(discovery)
        route = backend._adaptive_revision_routing("Either", {}, self.snapshot,
            proposal_discovery=discovery, turn_understanding=record)
        self.assertIn(route, {"proposal", "proposal_conservative"})
        explanation = {**record, "acts": ["explanation_request"]}
        self.assertEqual(backend._adaptive_revision_routing("Either", {}, self.snapshot,
            proposal_discovery=discovery, turn_understanding=explanation), "none")
        revised = {**discovery, "optionDecision": {**discovery["optionDecision"], "selection": "revise"},
            "understanding": topic(sufficient=True)}
        self.assertEqual(backend._adaptive_revision_routing("Either", {}, self.snapshot,
            proposal_discovery=revised, turn_understanding=record), "proposal_conservative")
        changed = {**discovery, "optionQuestion": {"discriminant": "changed"}}
        with self.assertRaises(backend.ApiError):
            backend._adaptive_revision_routing("Either", {}, self.snapshot, proposal_discovery=changed, turn_understanding=record)

    def test_empty_understanding_or_review_uses_clean_existing_retry_slot(self):
        latest = "Around the first destination"
        context = {**self.discovery, "lastQuestionText": "Which area?"}
        candidate = understanding(latest, topic(sufficient=True))
        for review_timeout in (False, True):
            with self.subTest(review_timeout=review_timeout):
                outputs = ([response(candidate), asyncio.TimeoutError(), response({"accepted": True, "issues": [], "understanding": candidate})]
                    if review_timeout else [asyncio.TimeoutError(), response(candidate)])
                if review_timeout:
                    context = {**context, "understanding": None}
                call = AsyncMock(side_effect=outputs)
                with patch.object(llm, "_llm_credentials", return_value=("offline", "offline")), patch.object(llm, "_request_completion", call):
                    result = llm.classify_turn_understanding([{"role": "user", "content": latest}], self.snapshot,
                        "offline", proposal_context=context, _deadline=time.monotonic() + 116)
                self.assertTrue(result["proposalUnderstanding"])
                self.assertTrue(all(not item.kwargs["task"].endswith("_patch") for item in call.call_args_list))
        self.assertEqual(reason_code(asyncio.TimeoutError()), "BUDGET_OR_ATTEMPT_TIMEOUT")

    def test_missing_choice_repairs_only_choice_then_independent_review(self):
        option_set = directions.build_set(options(), self.discovery, self.snapshot)
        context = {**self.discovery, "optionSet": option_set, "lastQuestionText": envelope()["question"]}
        candidate = understanding("B please", topic(sufficient=True))
        choice = {"selection": "explicit", "preferredOptionIds": [option_set["options"][1]["id"]],
            "rejectedOptionIds": [], "evidenceSpan": "B please"}
        fixed = {**candidate, "optionDecision": choice}
        async def model(*args, **kwargs):
            if kwargs["task"] == "turn_understanding":
                return response(candidate)
            if kwargs["task"] == "turn_understanding_patch":
                spec = json.loads(args[3][-1]["content"])
                self.assertEqual(spec["targets"], ["optionDecision"])
                self.assertEqual(spec["frozenCandidate"]["understanding"]["proposalUnderstanding"], candidate["proposalUnderstanding"])
                return response(patch_fixture(spec, {"understanding": fixed}))
            return response({"accepted": True, "issues": [], "understanding": fixed})
        call = AsyncMock(side_effect=model)
        with patch.object(llm, "_llm_credentials", return_value=("offline", "offline")), patch.object(llm, "_request_completion", call):
            result = llm.classify_turn_understanding([{"role": "user", "content": "B please"}], self.snapshot,
                "offline", proposal_context=context, _deadline=time.monotonic() + 116)
        self.assertEqual(call.await_count, 3)
        self.assertEqual(result["optionDecision"], choice)
        self.assertEqual(result["criticalReview"]["reason"], "proposal_option_choice")

    def test_single_direction_uses_same_deadline_for_planning(self):
        value = envelope()
        value.update(options=value["options"][:1], question="", comparedOptions=[], discriminant="")
        call = AsyncMock(side_effect=[response(value), response({"bodyIssues": [], "optionIssues": [], "questionIssue": None})])
        verified = llm.LLMExecutionResult("A verified direction.", 1, "planned")
        with patch.object(llm, "_generate_revision_search_proposal_sync", return_value=verified) as plan:
            result = self.generate(call)
        self.assertEqual(result.proposal_diagnostics["optionSet"]["options"][0]["summary"], value["options"][0]["summary"])
        self.assertTrue(result.proposal_diagnostics["optionAutoPlan"])
        self.assertEqual(plan.call_args.kwargs["stage_context"]["revisionRouting"], "proposal_conservative")
        self.assertLessEqual(plan.call_args.kwargs["deadline"] - time.monotonic(), 116)

    def test_no_responsible_direction_preserves_topic_without_a_question(self):
        value = {"body": "I cannot find a responsible direction within these restrictions.",
            "options": [], "comparedOptions": [], "discriminant": "", "question": ""}
        result = self.generate(AsyncMock(side_effect=[response(value), response({"bodyIssues": [], "optionIssues": [], "questionIssue": None})]))
        marked = backend._mark_proposal_discovery_guidance(result, self.context)
        self.assertEqual(marked.guidance["proposalDiscovery"]["status"], "revision_needed")
        self.assertEqual(marked.guidance["proposalDiscovery"]["clarificationQuestionCount"], 0)
        self.assertNotIn("?", marked.assistant_message)

    def test_single_direction_failure_analysis_keeps_its_topic_metadata(self):
        value = envelope()
        value.update(options=value["options"][:1], question="", comparedOptions=[], discriminant="")
        call = AsyncMock(side_effect=[response(value), response({"bodyIssues": [], "optionIssues": [], "questionIssue": None})])
        failure = llm.LLMServiceError("PROPOSAL_SEARCH_EXHAUSTED", "No verified candidate", "offline", True, 2, 502)
        analysis = llm.LLMExecutionResult("I could not yet validate this direction.", 1, "analysis")
        with patch.object(llm, "_generate_revision_search_proposal_sync", side_effect=failure), \
                patch.object(llm, "_generate_verified_proposal_failure_analysis", return_value=analysis):
            result = self.generate(call)
        marked = backend._mark_proposal_discovery_guidance(result, self.context)
        self.assertEqual(marked.guidance["proposalDiscovery"]["status"], "revision_needed")
        self.assertEqual(len(marked.guidance["proposalDiscovery"]["optionSet"]["options"]), 1)

    def test_paraphrased_protected_edit_in_an_option_is_not_saved_as_a_choice(self):
        value = envelope()
        value["options"][0]["mechanism"] = "I would give the destination a different home."
        async def model(*args, **kwargs):
            if kwargs["task"] == "proposal_clarification":
                return response(value)
            if kwargs["task"] == "proposal_clarification_review":
                source = json.loads(args[3][-1]["content"])
                bad_id = source["optionSet"]["options"][0]["id"]
                return response({"bodyIssues": [], "optionIssues": [bad_id], "questionIssue": None})
            # A purported repair still relocates the protected entity: reject it
            # at the independent reviewer rather than a keyword shortcut.
            return response(patch_fixture(args[3][-1]["content"], value))
        with self.assertRaises(llm.LLMServiceError):
            self.generate(AsyncMock(side_effect=model))

    def test_option_translation_uses_second_slot_for_review_without_snapshot_leak(self):
        source = {"turnId": "t1", "body": "A. Grouped water. B. Separated water. Which do you prefer?",
            "followUpQuestion": None, "intentHypothesis": None, "proposalOfferSummary": None,
            "proposalOfferRationale": None, "proposalSummary": None, "uiCueTexts": [],
            "guidance": {"proposalDiscovery": {"optionSet": {"private": "not model input"}}},
            "stageRows": ROWS, "entityBindings": self.bindings}
        translated = {key: value for key, value in source.items() if key not in {"guidance", "stageRows", "entityBindings"}}
        translated["body"] = "A. 集中的水域。B. 分散的水域。你更喜欢哪一个？"
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                async def model(*args, **kwargs):
                    if kwargs["task"] == "translation":
                        return response({"translations": [translated]})
                    self.assertEqual(kwargs["task"], "translation_permission_review")
                    prompt = args[3][-1]["content"]
                    self.assertNotIn("stageRows", prompt)
                    self.assertNotIn("entityBindings", prompt)
                    self.assertNotIn("private", prompt)
                    return response({"accepted": accepted})
                call = AsyncMock(side_effect=model)
                with patch.object(llm, "_request_completion", call):
                    request = llm._translate_with_model_fallback(api_key="offline", base_url="offline",
                        models=[llm.KIMI_MODEL] * 2, messages=[], items=[source], target_language="zh-CN",
                        request_id="offline", started_at=time.monotonic(), deadline=time.monotonic() + 116)
                    if accepted:
                        result = asyncio.run(request)
                        self.assertEqual(result.translations[0]["body"], translated["body"])
                    else:
                        with self.assertRaises(llm.LLMServiceError):
                            asyncio.run(request)
                self.assertEqual(call.await_count, 2)
