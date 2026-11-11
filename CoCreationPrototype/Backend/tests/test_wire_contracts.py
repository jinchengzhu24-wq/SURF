"""Offline provider boundary: fixtures must obey the actual outbound schema."""
import asyncio
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import APIStatusError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_client as llm
import proposal_options as options
import app as backend
from proposal_diagnostics import ProposalDiagnostic, CURRENT_PROPOSAL_DIAGNOSTIC
from reply_reliability import failure_reason
from option_fixtures import envelope
from patch_fixtures import patch_fixture
import test_proposal_options as option_tests
from test_proposal_understanding import response, ROWS


def assert_schema(value, schema):
    """Check the schema constraints exercised here, independently of parsers."""
    if "anyOf" in schema:
        for variant in schema["anyOf"]:
            try:
                assert_schema(value, variant)
                return
            except AssertionError:
                pass
        raise AssertionError("No schema variant matched")
    types = schema.get("type")
    if types:
        types = types if isinstance(types, list) else [types]
        kinds = {"null": type(None), "boolean": bool, "string": str, "array": list,
                 "object": dict, "integer": int, "number": (int, float)}
        assert any(type(value) in (kinds[t] if isinstance(kinds[t], tuple) else (kinds[t],)) for t in types)
    if "enum" in schema:
        assert value in schema["enum"]
    if isinstance(value, dict):
        assert set(schema.get("required", [])) <= set(value)
        if schema.get("additionalProperties") is False:
            assert set(value) <= set(schema.get("properties", {}))
        for key, item in value.items():
            assert_schema(item, schema.get("properties", {}).get(key, {}))
    if isinstance(value, list):
        assert schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf"))
        for item in value:
            assert_schema(item, schema.get("items", {}))
    if isinstance(value, str):
        assert schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf"))


class WireContracts(unittest.TestCase):
    def setUp(self):
        case = option_tests.DirectionDiscoveryTests()
        case.setUp()
        self.context = case.context
        self.calls = []

    def provider(self, handler):
        owner = self
        class Client:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)
            async def create(self, **kwargs):
                owner.calls.append(kwargs)
                result = handler(kwargs, len(owner.calls))
                if isinstance(result, BaseException):
                    raise result
                if isinstance(result, tuple):
                    # Explicit fault injection for provider violations/truncation.
                    value, finish = result
                    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                        message=SimpleNamespace(content=value if isinstance(value, str) else json.dumps(value)))])
                fmt = kwargs.get("response_format") or {}
                if fmt.get("type") == "json_schema":
                    assert_schema(result, fmt["json_schema"]["schema"])
                return response(result)
            async def close(self):
                pass
        return patch.object(llm, "_create_async_client", side_effect=lambda *args: Client())

    def generate(self, handler, language="en"):
        with self.provider(handler), patch.object(llm, "_llm_credentials", return_value=("offline", "offline")):
            return llm.generate_chat_reply([{"role": "user", "content": "How can this be easier?"}],
                ROWS, "wire-contract", language=language, stage_context=self.context,
                _deadline=time.monotonic() + 116)

    def test_new_options_and_review_use_correct_actual_schema_without_repair(self):
        def model(call, number):
            name = call["response_format"]["json_schema"]["name"]
            return envelope() if name == "cocreation_proposal_options" else {
                "bodyIssues": [], "questionIssue": None, "optionIssues": []}
        result = self.generate(model)
        self.assertIn(envelope()["body"], result.assistant_message)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]["max_completion_tokens"], 1000)
        for call in self.calls:
            self.assertFalse(call["stream"])
            self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}})

    def test_old_schema_reproduces_incident_with_protocol_compliant_provider(self):
        legacy = llm._structured_response_format("proposal_clarification")
        def model(call, number):
            allowed = call["response_format"]["json_schema"]["schema"]["properties"]
            return {key: value for key, value in envelope().items() if key in allowed}
        # Disabling the new selection recreates the old production boundary.
        with patch.object(options, "clarification_response_format", return_value=legacy), \
                patch.object(llm, "_clarification_envelope_patch", return_value=None):
            with self.assertRaises(llm.LLMServiceError) as caught:
                self.generate(model)
        self.assertEqual(caught.exception.code, "MODEL_RESPONSE_INVALID")
        self.assertEqual(len(self.calls), 2)

    def test_missing_or_invalid_fields_patch_only_targets_before_full_review(self):
        faults = [{"options": None}, {"options": "__omit__"}, {"comparedOptions": "wrong"}, {"body": 3},
                  {"comparedOptions": ["wrong-id", "2"]},
                  {"options": [{**envelope()["options"][0], "components": ["targets"]}]}]
        for fault in faults:
            with self.subTest(fault=fault):
                self.setUp()
                expected = set(fault)
                if fault.get("comparedOptions") == ["wrong-id", "2"]:
                    expected = {"question", "comparedOptions", "discriminant"}
                def model(call, number):
                    if number == 1:
                        value = {**envelope(), **fault}
                        if fault.get("options") == "__omit__":
                            value.pop("options")
                        return value, "stop"
                    if number == 2:
                        spec = json.loads(call["messages"][-1]["content"])
                        self.assertEqual(set(spec["targets"]), expected)
                        for key in options.ENVELOPE_FIELDS - expected:
                            self.assertEqual(spec["frozenCandidate"][key], envelope()[key])
                        return patch_fixture(spec, {field: envelope()[field] for field in expected})
                    return {"bodyIssues": [], "questionIssue": None, "optionIssues": []}
                result = self.generate(model)
                self.assertIn(envelope()["body"], result.assistant_message)
                self.assertEqual(len(self.calls), 3)

    def test_locked_and_revised_schemas_preserve_server_mode(self):
        discovery = self.context["proposalDiscovery"]
        saved = options.build_set(envelope()["options"], discovery, self.context["stageSnapshot"])
        discovery["optionSet"] = saved
        for selection in ("unclear", "revise"):
            with self.subTest(selection=selection):
                discovery["optionDecision"] = {"selection": selection}
                value = envelope()
                if selection != "revise":
                    value["options"] = None
                    value["comparedOptions"] = [item["id"] for item in saved["options"]]
                def model(call, number):
                    return value if call["response_format"]["json_schema"]["name"] == "cocreation_proposal_options" else {
                        "bodyIssues": [], "questionIssue": None, "optionIssues": []}
                self.calls = []
                self.generate(model)
                expected = "array" if selection == "revise" else "null"
                self.assertEqual(self.calls[0]["response_format"]["json_schema"]["schema"]["properties"]["options"]["type"], expected)
                self.assertEqual(discovery["optionSet"], saved)

    def test_zero_and_one_options_retain_empty_question_rules(self):
        for count in (0, 1, 3):
            value = envelope()
            value["options"] = value["options"][:count]
            if count == 3:
                value["options"].append({**value["options"][0], "summary": "Try a third composition."})
            else:
                value.update(question="", comparedOptions=[], discriminant="")
            fmt = options.clarification_response_format(self.context["proposalDiscovery"], self.context["stageSnapshot"])
            assert_schema(value, fmt["json_schema"]["schema"])
            parsed, *_ = llm._parse_proposal_clarification_payload(json.dumps(value), "en", self.context)
            self.assertTrue(parsed)

    def test_specific_failure_survives_http_details_without_sensitive_text(self):
        trace = ProposalDiagnostic()
        token = CURRENT_PROPOSAL_DIAGNOSTIC.set(trace)
        try:
            with self.assertRaises(llm.LLMServiceError) as caught:
                self.generate(lambda call, n: ({**envelope(), "options": None}, "stop") if n == 1 else
                    ({"patches": []}, "stop"))
        finally:
            CURRENT_PROPOSAL_DIAGNOSTIC.reset(token)
        details = backend._llm_failure_details(caught.exception)
        self.assertIn("proposalDiagnostic", details)
        self.assertEqual(details["proposalDiagnostic"]["firstFailure"]["reasonCode"], "OPTION_FIELD_INVALID")
        self.assertNotIn("How can this", json.dumps(details))
        from proposal_diagnostics import ProposalValidationError, structural_issue
        self.assertEqual(failure_reason(ProposalValidationError([structural_issue("options", "OPTION_SET_INVALID")])), "OPTION_SET_INVALID")

    def test_schema_rejection_fallback_keeps_same_option_contract_and_review(self):
        def model(call, number):
            if number == 1:
                return APIStatusError("unsupported schema", response=httpx.Response(400,
                    request=httpx.Request("POST", "https://offline.invalid")), body={})
            return envelope() if number == 2 else {"bodyIssues": [], "questionIssue": None, "optionIssues": []}
        self.generate(model)
        self.assertEqual(self.calls[1]["response_format"], {"type": "json_object"})
        self.assertEqual(self.calls[0]["messages"], self.calls[1]["messages"])
        self.assertEqual(len(self.calls), 3)

    def test_translation_review_uses_boolean_schema_and_preserves_rejection(self):
        source = {"turnId": "t1", "body": "A. Grouped water. B. Separated water. Which do you prefer?",
            "followUpQuestion": None, "intentHypothesis": None, "proposalOfferSummary": None,
            "proposalOfferRationale": None, "proposalSummary": None, "uiCueTexts": [],
            "guidance": {"proposalDiscovery": {"optionSet": {"private": "hidden"}}}, "stageRows": ROWS}
        translated = {k: v for k, v in source.items() if k not in {"guidance", "stageRows"}}
        for accepted in (True, False):
            self.calls = []
            def model(call, number):
                return {"translations": [translated]} if number == 1 else {"accepted": accepted}
            with self.provider(model):
                request = llm._translate_with_model_fallback(api_key="offline", base_url="offline",
                    models=[llm.KIMI_MODEL] * 2, messages=[], items=[source], target_language="en",
                    request_id="translation-wire", started_at=time.monotonic(), deadline=time.monotonic() + 116)
                if accepted:
                    self.assertEqual(asyncio.run(request).translations[0]["body"], source["body"])
                else:
                    with self.assertRaises(llm.LLMServiceError):
                        asyncio.run(request)
            self.assertEqual(len(self.calls), 2)
            self.assertNotIn("stageRows", json.dumps(self.calls[1]["messages"]))
            self.assertNotIn("hidden", json.dumps(self.calls[1]["messages"]))

    def test_legacy_clarification_keeps_its_two_field_protocol(self):
        self.context["proposalClarification"] = {"questionKey": "experience_goal", "semanticPolicyVersion": 1}
        result = self.generate(lambda call, n: {"body": "The visual balance can be explored in different ways.",
            "question": "Would you prefer visual balance or open space?"})
        schema = self.calls[0]["response_format"]["json_schema"]["schema"]
        self.assertEqual(set(schema["properties"]), {"body", "question"})
        self.assertEqual(len(self.calls), 1)
        self.assertIn("visual balance", result.assistant_message)

    def test_chinese_option_contract_runs_through_real_language_validation(self):
        value = {"body": "可以从两种局部调整入手。", "question": "你更想减少绕路，还是让空间更宽松？",
            "comparedOptions": ["1", "2"], "discriminant": "绕路与空间的取舍", "options": []}
        for text in ("集中水域", "分散水域"):
            value["options"].append({"summary": text + "可以作为一个方向。", "components": ["water"],
                "focus": "先考虑内部区域。", "mechanism": "可以尝试" + text + "。",
                "effect": "可能让整体更容易读懂。", "tradeoff": "局部空间可能会收紧。",
                "preserve": "保留玩家、箱子、目标和外壳。"})
        result = self.generate(lambda call, n: value if n == 1 else {
            "bodyIssues": [], "questionIssue": None, "optionIssues": []}, language="zh-CN")
        self.assertIn(value["body"], result.assistant_message)
        self.assertEqual(len(self.calls), 2)

    def test_review_technical_repair_is_bounded_and_preserves_valid_fields(self):
        for succeeds in (True, False):
            self.calls = []
            def model(call, number):
                if number == 1:
                    return envelope()
                if number == 2:
                    return ({"bodyIssues": [], "questionIssue": None}, "stop")
                spec = json.loads(call["messages"][-1]["content"])
                self.assertEqual(spec["targets"], ["optionIssues"])
                self.assertEqual(spec["frozenCandidate"], {"bodyIssues": [], "questionIssue": None})
                return patch_fixture(spec, {"optionIssues": []}) if succeeds else {"patches": []}
            if succeeds:
                self.generate(model)
            else:
                with self.assertRaises(llm.LLMServiceError) as caught:
                    self.generate(model)
                self.assertEqual(caught.exception.details["failureStage"], "clarification_review")
            self.assertEqual(len(self.calls), 3)

    def test_empty_truncated_and_unparseable_responses_use_clean_existing_slot(self):
        for content, finish in (("", "stop"), ("{bad json", "stop"), (envelope(), "length")):
            self.calls = []
            def model(call, number):
                if number == 1:
                    return content, finish
                return envelope() if number == 2 else {"bodyIssues": [], "questionIssue": None, "optionIssues": []}
            self.generate(model)
            self.assertEqual(len(self.calls), 3)
            self.assertEqual(set(self.calls[1]["response_format"]["json_schema"]["schema"]["required"]), options.ENVELOPE_FIELDS)
            if content:
                self.assertIn("exactly body, question, options, comparedOptions and discriminant", self.calls[1]["messages"][0]["content"])

    def test_persistently_rejected_options_cannot_be_repaired_into_success(self):
        def model(call, number):
            name = call["response_format"]["json_schema"]["name"]
            if name == "cocreation_proposal_options":
                return envelope()
            if name == "cocreation_component_patch":
                return patch_fixture(json.loads(call["messages"][-1]["content"]), envelope())
            source = json.loads(call["messages"][-1]["content"])
            return {"bodyIssues": [], "questionIssue": None, "optionIssues": [source["optionSet"]["options"][0]["id"]]}
        with self.assertRaises(llm.LLMServiceError):
            self.generate(model)
        self.assertEqual(len(self.calls), 4)
        self.assertNotIn("optionSet", self.context["proposalDiscovery"])

    def test_outbound_task_schemas_cover_their_consumed_root_fields(self):
        contracts = {
            "turn_understanding": {"acts", "elements", "evidenceSpan", "directionSufficient", "mapRelated", "changes", "proposalUnderstanding", "humanEditEngagement", "optionDecision"},
            "turn_understanding_review": {"understanding", "accepted", "issues"},
            "revision_requirements": {"requirements", "automaticBindings", "exactTransitions"},
            "revision_requirement_review": {"accepted", "issues"},
            "revision_plan": {"strategies"}, "operation_candidates": {"candidates"},
            "intent_candidate_review": {"classification", "claims", "cardText", "bodyValid", "cardTextValid", "relation", "issues", "answeredQuestionIds", "conflictingHypothesisIds", "explanation", "reviewedActiveClaims"},
            "intent_body_repair": {"bodyParagraphs"}, "intent_card_repair": {"cardText"},
            "intent_feedback_review": {"verdict", "explanation", "supersedesHypothesisIds", "conflictingHypothesisIds"},
            "manual_edit_assessment_pair_no_conflict": {"openingMessage", "assessment", "reviewMessage", "conflict"},
            "manual_edit_assessment_pair_conflict": {"openingMessage", "assessment", "reviewMessage", "conflict"},
            "challenge_reason_classification": {"relation", "merit", "comparison"},
            "question_answer_review": {"results"}, "intent_progress_rewrite": {"detailedText", "summaryText"},
            "human_edit_engagement_recovery": {"decision", "evidenceSpan"},
            "translation": {"translations"}, "translation_permission_review": {"accepted"},
            "proposal_failure_analysis": {"body"}}
        for task, fields in contracts.items():
            with self.subTest(task=task):
                with self.provider(lambda call, n: ({}, "stop")):
                    asyncio.run(llm._request_completion("offline", "offline", llm.KIMI_MODEL,
                        [{"role": "user", "content": "offline contract audit"}], 100, 2, task=task))
                schema = self.calls[-1]["response_format"]["json_schema"]["schema"]
                self.assertTrue(fields <= set(schema["properties"]))
                self.assertFalse(schema["additionalProperties"])
