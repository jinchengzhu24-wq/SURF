"""Ordinary delivery faults; isolated fixtures only, no live model or production DB."""
import asyncio
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cocreation  # noqa: F401
from tests.fakes import FakeClock, RecordedAsyncCall, RecordedCall, SessionTestCase, llm_scope
from cocreation.llm import facade as llm
from cocreation.persistence import repository
import test_sessions
import test_llm_client
OPERATION_BASE_ROWS = test_llm_client.OPERATION_BASE_ROWS
from cocreation.reliability.reply_reliability import CURRENT_REPLY_TRACE, ReplyTrace, OptionalRepairBudget, REPLY_PIPELINE_VERSION
from reliability_report import summarize

USER = "The right side looks too cramped; I prefer more open spacing."
BODY = "I would focus on visual spacing while keeping the play effects uncertain."
CARD = "My provisional interpretation is that you prefer more open visual spacing on the right. You can correct me."
CLAIM = {"normalizedMeaning": "more open visual spacing on the right", "subjectType": "space",
         "subjectText": "right side", "attributeType": "density", "attributeText": "visual spacing",
         "direction": "decrease", "degree": "excessive", "aspect": "visual", "scopeType": "region",
         "scopeText": "right side", "evidenceSpan": USER}


def verdict(**changes):
    return {"classification": "candidate", "claims": [copy.deepcopy(CLAIM)], "cardText": CARD,
            "cardTextValid": True, "bodyValid": True, "relation": "none", "conflictingHypothesisIds": [],
            "explanation": None, "issues": [], "reviewedActiveClaims": [], "answeredQuestionIds": ["q1"], **changes}


def completion(value, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=value if isinstance(value, str) else json.dumps(value)))])


class JointReviewTests(unittest.TestCase):
    def review(self, payload, state=None, finish="stop"):
        state = state if state is not None else {}
        call = RecordedAsyncCall(return_value=completion(payload, finish))
        with llm_scope(completion_requester=call):
            result = asyncio.run(llm._review_intent_candidate_async(api_key="offline", base_url="offline",
                user_text=USER, body=BODY, decision={"classification": "none", "claims": [], "cardText": ""},
                active_inclinations=[], language="en", request_id="r", deadline=time.monotonic() + 30,
                visible_questions=[{"questionId": "q1", "question": "Which spacing would you prefer?"}],
                component_state=state))
        return result, state, call

    def test_one_call_returns_intent_and_question_independently(self):
        result, state, call = self.review(verdict())
        self.assertEqual(call.await_count, 1)
        self.assertEqual(result["classification"], "candidate")
        self.assertEqual(state["questionAnswerReview"]["answeredQuestionIds"], ["q1"])

    def test_bad_question_ids_preserve_intent(self):
        for identities in (["foreign"], ["q1", "q1"], "q1", [True]):
            result, state, _ = self.review(verdict(answeredQuestionIds=identities))
            self.assertEqual(result["classification"], "candidate")
            self.assertNotIn("questionAnswerReview", state)
            self.assertEqual(state["questionReviewIssue"], "QUESTION_RESULTS_INVALID")

    def test_bad_intent_evidence_preserves_answer(self):
        state = {}
        payload = verdict(claims=[{**CLAIM, "evidenceSpan": "invented quote"}])
        with self.assertRaises(ValueError):
            self.review(payload, state)
        self.assertEqual(state["questionAnswerReview"]["answeredQuestionIds"], ["q1"])
        self.assertNotIn("intentReviewAudit", state)

    def test_missing_card_preserves_verified_semantics(self):
        payload = verdict()
        payload.pop("cardText")
        result, state, _ = self.review(payload)
        self.assertEqual(result["classification"], "candidate")
        self.assertEqual(result["cardText"], "")
        self.assertEqual(state["intentReviewAudit"]["claims"], [CLAIM])

    def test_truncated_and_invalid_json_lock_neither_component(self):
        for payload, finish in (("{", "stop"), (verdict(), "length")):
            state = {}
            with self.assertRaises(ValueError):
                self.review(payload, state, finish)
            self.assertEqual(state, {})

    def test_questions_are_bounded_and_deduplicated(self):
        questions = [{"id": str(i), "question": "Which spacing?"} for i in range(30)]
        self.assertEqual(len(llm._ordinary_review_questions(questions + questions)), 24)

    def test_presentation_synonyms_are_advisory_only_after_semantic_review(self):
        self.assertIsNone(llm._intent_hypothesis_detail_issue(CARD, "en", semantic_reviewed=True))
        self.assertIsNotNone(llm._intent_hypothesis_detail_issue("This proves you definitely prefer it.", "en", semantic_reviewed=True))

    def test_single_field_card_repair_preserves_body_and_model_settings(self):
        call = RecordedAsyncCall(return_value=completion({'cardText': CARD}))
        with llm_scope(completion_requester=call):
            body, card = asyncio.run(llm._repair_intent_components_async(api_key="offline", base_url="offline",
                user_text=USER, body=BODY, card_text="bad", claims=[CLAIM], issues=[], repair_body=False,
                repair_card=True, map_facts="{}", language="en", request_id="r", deadline=time.monotonic() + 30))
        self.assertEqual((body, card), (BODY, CARD))
        self.assertEqual(call.call_args.kwargs["task"], "intent_card_repair")
        self.assertEqual(llm._structured_response_format("intent_card_repair")["json_schema"]["schema"]["required"], ["cardText"])

    def test_budget_exhaustion_makes_no_repair_call_and_has_specific_reason(self):
        trace = ReplyTrace("r", "m", "v", lambda *a, **k: None)
        token = CURRENT_REPLY_TRACE.set(trace)
        budget = OptionalRepairBudget(time.monotonic() + 60)
        budget.spent = 10
        try:
            repair = RecordedAsyncCall()
            with llm_scope(intent_component_repairer=repair):
                result = asyncio.run(llm._repair_reviewed_intent_presentation(repair_body=False, repair_card=True,
                    body=BODY, card_text="bad", deadline=budget.deadline, optional_budget=budget,
                    card_required=False, component_checks=[]))
            self.assertEqual(result, (BODY, ""))
            self.assertEqual(repair.await_count, 0)
            self.assertEqual(trace.steps[-1]["code"], "OPTIONAL_BUDGET_EXHAUSTED")
        finally:
            CURRENT_REPLY_TRACE.reset(token)

    def test_report_missing_metrics_stay_unknown_and_trace_is_not_a_retry(self):
        report = summarize([{"sessionId": "s", "eventType": "reply_delivery_outcome", "payload": {
            "messageKey": "m", "reliableBodyDelivered": True}}, {"sessionId": "s",
            "eventType": "reply_pipeline_diagnostic", "payload": {"messageKey": "m", "requestId": "r",
            "pipelineVersion": REPLY_PIPELINE_VERSION, "steps": [{"stage": "card_repair", "status": "omitted"}]}}])
        self.assertEqual(report["logicalRequests"], 1)
        self.assertEqual(report["recordedOutcomes"], 1)
        self.assertEqual(report["afterRetries"]["intentCardCommitted"]["missing"], 1)
        self.assertEqual(report["pipelineDiagnostics"]["httpAttempts"], 1)


class DeliveryTests(unittest.TestCase):
    setUp = test_llm_client.LLMClientTests.setUp
    execute = test_llm_client.LLMClientTests.execute

    def test_answered_question_does_not_veto_card_or_invent_keyword_card(self):
        payload = verdict(answeredQuestionIds=[])
        review = RecordedAsyncCall(return_value={**payload, 'reviewVersion': 'test'})
        guidance_helper = RecordedCall(side_effect=llm._apply_deterministic_guidance_fallback)
        with llm_scope(intent_candidate_reviewer=review, handlers={'apply_deterministic_guidance_fallback': guidance_helper}):
            result, _ = self.execute([BODY], rows=OPERATION_BASE_ROWS, conversation=[{"role": "user", "content": USER}],
                stage_context={"answeredVisibleQuestion": True, "conversationBranch": "ordinary"})
        self.assertEqual(review.await_count, 1)
        self.assertTrue(guidance_helper.call_args.kwargs["semantic_intent"])
        self.assertEqual(result.guidance["intentHypothesis"], CARD)

    def test_card_failure_retains_reviewed_candidate_and_body(self):
        review = verdict(cardText="This proves you definitely want this.", cardTextValid=False, answeredQuestionIds=[], reviewVersion="test")
        with llm_scope(intent_candidate_reviewer=RecordedAsyncCall(return_value=review), intent_component_repairer=RecordedAsyncCall(side_effect=ValueError('bad expression'))):
            result, _ = self.execute([BODY], rows=OPERATION_BASE_ROWS, conversation=[{"role": "user", "content": USER}])
        self.assertEqual(result.assistant_message, BODY)
        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertEqual(result.guidance["_intentCandidateReview"]["classification"], "candidate")
        self.assertEqual(result.guidance["_intentCandidateReview"]["claims"], [CLAIM])

    def test_map_memo_reuses_only_identical_inputs_and_resets(self):
        inputs = ["I would consider visual balance."]
        memo = {"passed": set(), "hits": 0, "checks": 0}
        token = llm._GROUNDING_MEMO.set(memo)
        try:
            llm._validate_map_grounding_texts(inputs, OPERATION_BASE_ROWS)
            llm._validate_map_grounding_texts(inputs, OPERATION_BASE_ROWS)
            llm._validate_map_grounding_texts(["Changed text."], OPERATION_BASE_ROWS)
            llm._validate_map_grounding_texts(inputs, OPERATION_BASE_ROWS, entity_bindings={"changed": True})
            self.assertEqual(memo["checks"], 3)
            self.assertEqual(memo["hits"], 1)
        finally:
            llm._GROUNDING_MEMO.reset(token)
        self.assertIsNone(llm._GROUNDING_MEMO.get())

    def test_body_retry_reuses_semantics_and_card_without_optional_budget_reset(self):
        state = {}
        tasks = []
        clock = FakeClock(time.monotonic())
        async def model(*args, **kwargs):
            tasks.append(kwargs["task"])
            if kwargs["task"] == "plain_chat":
                return completion(BODY)
            if kwargs["task"] == "intent_body_repair":
                # Technical fault in the original corrective slot.
                clock.advance(10)
                raise ValueError("PATCH_TYPE_INVALID")
            if kwargs.get("response_format_override"):
                return completion({"bodyValid": True, "issues": []})
            # The first review is verified, but identifies a mandatory body defect.
            return completion(verdict(bodyValid=False, issues=["The body does not address the spatial judgment."]))
        with llm_scope(completion_requester=model, clock=clock):
            result = llm.generate_chat_reply([{"role": "user", "content": USER}], OPERATION_BASE_ROWS, "r",
                stage_context={"_reliabilityState": state, "ordinaryOpenQuestions": [{"questionId": "q1", "question": "Which spacing?"}]})
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(result.guidance["intentHypothesis"], CARD)
        self.assertEqual(state["questionAnswerReview"]["answeredQuestionIds"], ["q1"])
        self.assertEqual(tasks, ["plain_chat", "intent_candidate_review", "intent_body_repair", "plain_chat", "intent_candidate_review"])
        self.assertEqual(state["lockedIntentReview"]["claims"], [CLAIM])


class JointHttpTests(SessionTestCase):
    def setUp(self):
        self.fixture = test_sessions.CoCreationSessionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_joint_results_commit_once_and_hidden_review_stays_private(self):
        fixture = self.fixture
        version = fixture.read_session()["currentVersionId"]
        with self.fixture.services.connect(immediate=True) as database:
            session = repository.get_session(database, fixture.session_id)
            self.fixture.services.insert_turn(database, session, "assistant", "Which spacing would you prefer?", version, "opening", None)
            context = repository.load_design_context(database, fixture.session_id, version)
            context["openQuestions"] = [{"id": "q1", "question": "Which spacing would you prefer?", "status": "open",
                "sourceKind": "visible_output", "sourceStageId": version}]
            repository.save_design_context(database, version, context)
        calls = []
        async def model(*args, **kwargs):
            calls.append(kwargs["task"])
            return completion(BODY if kwargs["task"] == "plain_chat" else verdict())
        payload = {"content": USER, "baseVersionId": version, "idempotencyKey": "joint-http"}
        with self.fixture.using_dependencies(
            llm_handlers={"review_question_answers": AssertionError('extra question call')},
            llm_options={"completion_requester": RecordedAsyncCall(side_effect=model)},
        ):
            response = fixture.client.post(f"/api/sessions/{fixture.session_id}/messages", json=payload)
            replay = fixture.client.post(f"/api/sessions/{fixture.session_id}/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        turn = response.json()["turns"][-1]
        self.assertEqual(turn["guidance"]["intentHypothesis"], CARD)
        self.assertFalse(any(key.startswith("_intent") for key in turn["guidance"]))
        self.assertEqual(calls, ["plain_chat", "intent_candidate_review"])
        self.assertEqual(response.json()["turns"], replay.json()["turns"])
        self.assertEqual(response.headers["X-Reply-Pipeline-Version"], REPLY_PIPELINE_VERSION)
        with self.fixture.services.connect() as database:
            context = repository.load_design_context(database, fixture.session_id, version)
            self.assertEqual(context["openQuestions"][0]["status"], "answered")
            events = database.execute("SELECT payload_json FROM audit_events WHERE event_type='reply_pipeline_diagnostic'").fetchall()
        self.assertEqual(len(events), 2)  # HTTP attempts, not extra model calls.

