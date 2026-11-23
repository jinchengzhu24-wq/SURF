"""Focused regression tests for the audit fixes across cocreation.llm."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cocreation  # noqa: F401
from cocreation.domain import binary_discovery as binary
from cocreation.domain.level_validation import build_stage_snapshot
from cocreation.domain.proposal_search import ProposalSearchExhausted
from cocreation.llm import (
    binary_model,
    chat_generation,
    config_and_runtime,
    intent_and_review,
    proposal_planning,
    service,
    snapshot_grounding,
    translations,
)
from cocreation.llm.config_and_runtime import (
    APIConnectionError,
    APIStatusError,
    LLMConfig,
    LLMServiceError,
)
from tests.fakes import FakeClock, RecordedAsyncCall, RecordedCall, llm_scope
import test_llm_client


AUDIT_ROWS = [
    "############",  # 1
    "#p.........#",  # 2: p=(2,2)
    "#..s.......#",  # 3: s=(3,4)
    "#...@#t....#",  # 4: @=(4,5), #=(4,6), t=(4,7)
    "#..........#",  # 5
    "#..........#",  # 6
    "#.....@....#",  # 7: @=(7,7)
    "#..........#",  # 8
    "#..........#",  # 9
    "############",  # 10
]

VOID_SHELL_ROWS = [
    "  ########  ",  # row 0: (2..9, 0) touches void (1,0)/(10,0)/(0,0)
    "###......###",  # row 1: (0..2, 1), (9..11, 1)
    "#p..s...t..#",  # row 2
    "#....#.....#",  # row 3: disconnected interior '#' at (5, 3)
    "#..........#",  # row 4
    "#..........#",  # row 5
    "#..........#",  # row 6
    "#..........#",  # row 7
    "#..........#",  # row 8
    "############",  # row 9
]


def completion(value, finish="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                message=SimpleNamespace(
                    content=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _api_status(status_code):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return APIStatusError(f"HTTP {status_code}", response=response, body={"error": {"message": f"status {status_code}"}})


class AuditConfigAndServiceTests(unittest.TestCase):
    def test_revision_plan_schema_omits_schema_version_and_strict_fields_exist(self):
        plan_schema = config_and_runtime._structured_response_format("revision_plan")["json_schema"]["schema"]
        self.assertNotIn("schemaVersion", plan_schema["properties"])
        self.assertEqual(plan_schema["required"], ["strategies"])

        chat_schema = config_and_runtime._structured_response_format("chat")["json_schema"]["schema"]
        self.assertEqual(set(chat_schema["required"]), set(chat_schema["properties"]))
        guidance_schema = chat_schema["properties"]["guidance"]
        self.assertEqual(set(guidance_schema["required"]), set(guidance_schema["properties"]))
        assessment_schema = chat_schema["properties"]["assessment"]
        self.assertEqual(set(assessment_schema["required"]), set(assessment_schema["properties"]))

    def test_strip_unreferenced_current_map_claims_uses_ascii_boundaries(self):
        prose = "TOP at (2,2) stays quiet. P is at (2,2)."
        stripped = config_and_runtime._strip_unreferenced_current_map_claims(prose)
        self.assertEqual(stripped, "TOP at (2,2) stays quiet.")

    def test_service_wrappers_activate_instance(self):
        def fake_solver(rows, *args, **kwargs):
            return SimpleNamespace(
                valid=True,
                solvable=True,
                solution_steps=10,
                solution_pushes=3,
                searched_states=5,
                solution="RR",
                rows=rows,
            )

        custom_cfg = LLMConfig(api_key="test-key", model="custom-audit-model")
        svc = service.CoCreationLLMService(config=custom_cfg, clock=FakeClock(100.0), solver_fn=fake_solver)
        self.assertEqual(config_and_runtime._current_service().config.model, "kimi-k2.6")

        msg = svc.server_snapshot_fallback_message(
            AUDIT_ROWS, "en", stage_context={"stageSnapshot": build_stage_snapshot(AUDIT_ROWS)}, stage_opening=True
        )
        self.assertTrue(msg)
        safe = svc.safe_grounding_chat_reply(AUDIT_ROWS, "en")
        self.assertIn("Stage", safe)
        guidance = svc.apply_deterministic_guidance_fallback(
            [{"role": "user", "content": "Hello"}],
            "Make the stage simpler.",
            "en",
            {"stageSnapshot": build_stage_snapshot(AUDIT_ROWS)},
            False,
            None,
            None,
            [],
            AUDIT_ROWS,
            {"solvable": True, "solutionSteps": 8, "solutionPushes": 2},
        )
        self.assertIsInstance(guidance, tuple)
        self.assertEqual(len(guidance), 4)

        solver_metrics = {"solvable": True, "solutionSteps": 8, "solutionPushes": 2}
        evidence = {"directionCount": 0, "directions": [], "effects": [], "solver": solver_metrics}
        decision = {
            "verdict": "no_conflict",
            "decisionSource": "deterministic_no_comparable_evidence",
            "confirmedDirectionIds": [],
            "effectEvidenceIds": [],
            "relation": "none",
            "solverDelta": {},
            "comparisons": [],
        }
        payload = {
            "openingMessage": "The Stage stays solvable and B1 remains near the center while P starts near the corner. I would watch the first push order carefully.",
            "assessment": {
                "solutionSummary": "The solver finds a valid route.",
                "difficultyOpinion": "The opening asks for careful alignment.",
                "features": ["Open center"],
                "suggestions": ["Test the first push"],
                "satisfactionQuestion": None,
            },
            "reviewMessage": "Your Stage keeps a clear route while leaving alternative paths open.",
            "conflict": None,
        }
        res = svc.validate_manual_edit_pair(
            payload,
            AUDIT_ROWS,
            "en",
            solver_metrics,
            {"stageNumber": 2, "source": "human_edit"},
            evidence,
            decision,
            "req-svc",
            1,
            "custom-audit-model",
            10,
        )
        self.assertEqual(res.model, "custom-audit-model")


class AuditSnapshotGroundingTests(unittest.TestCase):
    def test_validate_coordinate_claims_water_parens_and_past_tense(self):
        # Must NOT match 't' from 'the' in "(4, 5) is the water."
        snapshot_grounding._validate_coordinate_claims("(4, 5) is the water.", AUDIT_ROWS)
        snapshot_grounding._validate_coordinate_claims("（4，5）是水。", AUDIT_ROWS)
        snapshot_grounding._validate_coordinate_claims("(4, 7) is a target.", AUDIT_ROWS)
        with self.assertRaises(ValueError):
            snapshot_grounding._validate_coordinate_claims("(4, 6) is a target.", AUDIT_ROWS)
        with self.assertRaises(ValueError):
            snapshot_grounding._validate_coordinate_claims("（2，2）是墙。", AUDIT_ROWS)

        # Historical / past-tense skipped cleanly; active false transition still rejected.
        snapshot_grounding._validate_coordinate_claims("(2, 2) was water in the previous draft.", AUDIT_ROWS)
        snapshot_grounding._validate_coordinate_claims("Previously, (2, 2) changed from wall to floor.", AUDIT_ROWS)
        snapshot_grounding._validate_coordinate_claims("之前(2, 2)从墙变成了地板。", AUDIT_ROWS)
        with self.assertRaises(ValueError):
            snapshot_grounding._validate_coordinate_claims("把(2,2)从墙变成地板。", AUDIT_ROWS)

    def test_adjacency_checks_skip_negations_and_boundaries(self):
        snapshot_grounding._validate_map_grounding_texts(
            [
                "The box is not next to water.",
                "No box is in the upper-right.",
                "并没有箱子靠近目标点。",
                "箱子不在右上角。",
                "如果箱子靠近目标点，推箱就会更轻松。",
            ],
            AUDIT_ROWS,
        )
        with self.assertRaises(ValueError):
            snapshot_grounding._validate_map_grounding_texts(["The box is next to water."], AUDIT_ROWS)

        bindings = {
            "identityStatus": "exact",
            "entities": [
                {"id": "P", "kind": "player", "row": 2, "column": 2, "identityConfidence": "exact"},
                {"id": "B1", "kind": "box", "row": 3, "column": 4, "identityConfidence": "exact"},
                {"id": "T1", "kind": "target", "row": 4, "column": 7, "identityConfidence": "exact"},
            ],
        }
        snapshot_grounding._validate_map_grounding_texts(["B10 is beside water."], AUDIT_ROWS, entity_bindings=bindings)

    def test_recover_coordinate_links_enforces_span_non_overlap(self):
        links = snapshot_grounding._recover_coordinate_links(
            "B1 -> (3,6) -> T1", test_llm_client.OPERATION_BASE_ROWS
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["text"], "B1 -> (3,6)")


class AuditBinaryModelTests(unittest.TestCase):
    def _topic(self):
        return {
            "topicId": "topic-1",
            "questionCount": 0,
            "initialRequest": "Make it simpler",
            "userTurns": [{"id": "u1", "content": "Make it simpler"}],
            "understanding": {"sufficient": False, "nextQuestionDimension": "obstacles"},
            "choices": [],
            "requirements": None,
        }

    def _context(self):
        snap = build_stage_snapshot(AUDIT_ROWS)
        return {
            "language": "en",
            "rows": AUDIT_ROWS,
            "stageContext": {
                "stageSnapshot": snap,
                "entityBindings": {"identityStatus": "exact", "entities": snap["entities"]},
            },
        }

    def _valid_candidate(self, branch2_label="Remove internal wall"):
        req = {
            "sourceTurnId": "u1",
            "evidenceSpan": "Make it simpler",
            "component": "gameplay",
            "property": "experience",
            "relation": "seek",
            "strength": "goal",
            "entities": [],
            "value": None,
            "unit": "none",
            "focused": True,
            "priorityEvidenceSpan": "",
            "statement": "Make the stage simpler.",
        }
        return {
            "action": "ask",
            "question": "Which area should change first?",
            "note": "Water sits near the middle while the interior wall sits beside the target.",
            "branches": [
                {
                    "label": "Reduce water",
                    "strategy": {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_water"],
                        "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
                        "editBudget": 1,
                        "metricGoals": [],
                    },
                    "promise": {"kind": "reduce_obstacles", "component": "water", "focus": None, "corridor": None},
                },
                {
                    "label": branch2_label,
                    "strategy": {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_wall"],
                        "preserve": ["outer_shell", "player", "boxes", "targets", "water"],
                        "editBudget": 1,
                        "metricGoals": [],
                    },
                    "promise": {"kind": "reduce_obstacles", "component": "internal_walls", "focus": None, "corridor": None},
                },
            ],
            "initialRequirements": {
                "requirements": [req],
                "automaticBindings": [],
                "exactTransitions": [],
            },
        }

    def test_upstream_timeout_retries_cleanly_without_patch_and_classifies_upstream(self):
        clock = FakeClock(100.0)
        calls = []

        async def mock_req(*args, **kwargs):
            calls.append(kwargs.get("task"))
            raise asyncio.TimeoutError()

        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=mock_req), clock=clock):
            with self.assertRaises(LLMServiceError) as raised:
                binary_model.generate(
                    self._topic(),
                    self._context(),
                    lambda rows: SimpleNamespace(valid=True, solvable=True, solution_steps=8, solution_pushes=2, rows=rows),
                    lambda *a, **kw: None,
                    "bin-timeout",
                    clock.monotonic() + 100.0,
                )
        self.assertEqual(calls, ["proposal_clarification", "proposal_clarification"])
        self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")
        self.assertEqual(raised.exception.details["failureStage"], "upstream")

    def test_branch_text_error_targets_branch2_and_accepted_false_without_issues_retries_full_review(self):
        clock = FakeClock(100.0)
        seen_prompts = []
        valid_cand = self._valid_candidate(branch2_label="B1 is at (1, 1).")
        fixed_cand = self._valid_candidate()
        review_calls = []

        async def mock_req(*args, **kwargs):
            task = kwargs.get("task")
            msgs = args[3]
            seen_prompts.append((task, msgs[-1]["content"]))
            if task == "proposal_clarification":
                return completion(valid_cand)
            if task == "proposal_clarification_patch":
                patch_prompt = json.loads(msgs[-1]["content"])
                return completion(
                    {
                        "patches": [
                            {
                                "componentId": patch_prompt["componentId"],
                                "field": "branch-2",
                                "valueJson": json.dumps(fixed_cand["branches"][1]),
                                "evidenceRefs": ["snapshot"],
                            }
                        ]
                    }
                )
            if task == "proposal_clarification_review":
                review_calls.append(task)
                if len(review_calls) == 1:
                    return completion({"accepted": False})  # Missing issues -> MUST NOT lock accepted=False in PatchRequest
                return completion({"accepted": True, "issues": []})
            raise AssertionError(f"Unexpected task {task}")

        orig_preflight = binary.preflight
        try:
            binary.preflight = lambda step, *a, **kw: [
                {"rows": AUDIT_ROWS, "promiseResults": [{"passed": True}]},
                {"rows": AUDIT_ROWS, "promiseResults": [{"passed": True}]},
            ]
            with llm_scope(completion_requester=RecordedAsyncCall(side_effect=mock_req), clock=clock):
                step, _ = binary_model.generate(
                    self._topic(),
                    self._context(),
                    lambda rows: None,
                    lambda *a, **kw: None,
                    "bin-text",
                    clock.monotonic() + 100.0,
                )
        finally:
            binary.preflight = orig_preflight

        self.assertEqual(step["branches"][1]["label"], "Remove internal wall")
        patch_calls = [msg for task, msg in seen_prompts if task == "proposal_clarification_patch"]
        self.assertEqual(len(patch_calls), 1)
        self.assertIn("branch-2", patch_calls[0])
        self.assertEqual(len(review_calls), 2)
        self.assertNotIn("proposal_clarification_review_patch", [task for task, _ in seen_prompts])


class AuditTranslationTests(unittest.TestCase):
    def _source_items(self):
        return [
            {
                "turnId": "turn-1",
                "body": "Move B1 toward (3,5) to inspect the route.",
                "followUpQuestion": "Should water change?",
                "intentHypothesis": "You may want cleaner spacing.",
                "proposalOfferSummary": "Open space",
                "proposalOfferRationale": "Simplifies push order.",
                "proposalPresentationReason": "Fits the visual focus.",
                "proposalSummary": "Updated route",
                "uiCueTexts": ["Water note"],
                "coordinateLinks": [{"text": "B1 toward (3,5)", "from": {"row": 3, "column": 4}, "to": {"row": 3, "column": 5}}],
                "proposalQuestion": {
                    "questionId": "q1",
                    "question": "Which way first?",
                    "note": "Water vs wall.",
                    "status": "open",
                    "choices": [
                        {"choiceId": "c1", "letter": "A", "label": "Water"},
                        {"choiceId": "c2", "letter": "B", "label": "Wall"},
                    ],
                },
                "stageRows": AUDIT_ROWS,
                "entityBindings": {
                    "identityStatus": "exact",
                    "entities": [
                        {"id": "P", "kind": "player", "row": 2, "column": 2, "identityConfidence": "exact"},
                        {"id": "B1", "kind": "box", "row": 3, "column": 4, "identityConfidence": "exact"},
                        {"id": "T1", "kind": "target", "row": 4, "column": 7, "identityConfidence": "exact"},
                    ],
                },
            }
        ]

    def test_prompt_sources_include_proposal_presentation_reason_and_patch_preserves_question(self):
        items = self._source_items()
        projected = translations.prompt_translation_sources(items)
        self.assertEqual(projected[0]["proposalPresentationReason"], "Fits the visual focus.")

        bad_payload = {
            "translations": [
                {
                    "turnId": "turn-1",
                    "body": 123,  # non-string body must not crash coordinateLinkTexts check
                    "followUpQuestion": "水域是否需要调整？",
                    "intentHypothesis": "你可能希望空间更清爽。",
                    "proposalOfferSummary": "打开空间",
                    "proposalOfferRationale": "理顺推动顺序。",
                    "proposalPresentationReason": "符合视觉聚焦方向。",
                    "proposalSummary": "更新后的路线",
                    "uiCueTexts": ["水域提示"],
                    "coordinateLinkTexts": ["B1 到 (3,5)"],
                    "proposalQuestion": {"questionId": "q1"},
                }
            ]
        }
        patch, _ = translations._translation_patch_request(bad_payload, items, "zh-CN", {})
        self.assertIsNotNone(patch)
        self.assertIn("turn-turn-1.proposalQuestion", patch.targets)
        self.assertIn("turn-turn-1.body", patch.targets)

        good_pq = {
            "questionId": "q1",
            "question": "先调哪一侧？",
            "note": "水或墙。",
            "status": "open",
            "choices": [
                {"choiceId": "c1", "letter": "A", "label": "调水"},
                {"choiceId": "c2", "letter": "B", "label": "调墙"},
            ],
        }
        locked = translations._lock_valid_translation_fields(
            {"translations": [{"turnId": "turn-1", "proposalQuestion": good_pq}]}, items, "zh-CN"
        )
        self.assertIn("proposalQuestion", locked["turn-1"])

    def test_validate_translation_rejects_ungrounded_coordinate_link_text(self):
        items = self._source_items()
        bad = {
            "translations": [
                {
                    "turnId": "turn-1",
                    "body": "将 B1 移向 (4,6) 来观察路线。",
                    "followUpQuestion": "水域是否需要调整？",
                    "intentHypothesis": "你可能希望空间更清爽。",
                    "proposalOfferSummary": "打开空间",
                    "proposalOfferRationale": "理顺推动顺序。",
                    "proposalPresentationReason": "符合视觉聚焦方向。",
                    "proposalSummary": "更新后的路线",
                    "uiCueTexts": ["水域提示"],
                    "coordinateLinkTexts": ["B1 移向 (4,6)"],  # original to is (3,5), not (4,6)
                    "proposalQuestion": {
                        "questionId": "q1",
                        "question": "先调哪一侧？",
                        "note": "水或墙。",
                        "status": "open",
                        "choices": [
                            {"choiceId": "c1", "letter": "A", "label": "调水"},
                            {"choiceId": "c2", "letter": "B", "label": "调墙"},
                        ],
                    },
                }
            ]
        }
        with self.assertRaises(ValueError):
            translations.validate_translation_response(bad, items, target_language="zh-CN")

    def test_option_translation_fails_fast_without_wasting_attempt_two(self):
        items = self._source_items()
        clock = FakeClock(100.0)
        tasks = []

        async def mock_req(*args, **kwargs):
            tasks.append(kwargs.get("task"))
            return completion("not json")

        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=mock_req), clock=clock):
            with self.assertRaises(LLMServiceError):
                asyncio.run(
                    translations._translate_with_model_fallback(
                        api_key="fake",
                        base_url="fake",
                        models=["kimi-k2.6", "kimi-k2.6"],
                        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                        items=items,
                        target_language="zh-CN",
                        request_id="tr-failfast",
                        started_at=clock.monotonic(),
                        deadline=clock.monotonic() + 60.0,
                    )
                )
        self.assertEqual(tasks, ["translation"])


class AuditChatAndIntentTests(unittest.TestCase):
    def test_guidance_extracted_before_clarification_review_and_sanitization(self):
        clock = FakeClock(100.0)
        review_inputs = []

        async def mock_clarification(*args, **kwargs):
            # Capture the body passed into _review_proposal_clarification.
            review_inputs.append(args[2] if len(args) > 2 else kwargs.get("body"))
            return ("Visible discussion text only.", "")

        mock_intent_review = RecordedAsyncCall(
            return_value={
                "classification": "none",
                "claims": [],
                "cardText": "",
                "cardTextValid": True,
                "bodyValid": True,
                "relation": "none",
                "conflictingHypothesisIds": [],
                "explanation": None,
                "issues": [],
                "reviewedActiveClaims": [],
                "reviewVersion": "test",
            }
        )
        raw_reply = (
            "Visible discussion text.\n"
            "<GUIDANCE>DISCUSS: I would compare how the first push enters the wide corridor."
            ' || INTENT_DECISION: {"classification":"none","claims":[],"cardText":""}'
            "</GUIDANCE>"
        )
        orig_clar = service.CoCreationLLMService.review_proposal_clarification
        try:
            service.CoCreationLLMService.review_proposal_clarification = lambda self, *a, **kw: mock_clarification(*a, **kw)
            with llm_scope(
                completion_requester=RecordedAsyncCall(return_value=completion(raw_reply)),
                intent_candidate_reviewer=mock_intent_review,
                clock=clock,
            ) as svc:
                result = svc.generate_plain_chat(
                    conversation=[{"role": "user", "content": "What do you think?"}],
                    rows=AUDIT_ROWS,
                    request_id="chat-guidance-extract",
                    language="en",
                    solver_metrics=None,
                    play_summary=None,
                    stage_context={"conversationOnly": True, "stageSnapshot": build_stage_snapshot(AUDIT_ROWS)},
                )
        finally:
            service.CoCreationLLMService.review_proposal_clarification = orig_clar

        self.assertEqual(review_inputs, ["Visible discussion text."])
        self.assertNotIn("<GUIDANCE>", result.assistant_message)

    def test_body_review_required_rechecks_body_before_clearing_and_map_facts_is_dict(self):
        clock = FakeClock(100.0)
        state = {}
        repair_map_facts = []
        review_calls = []

        async def mock_req(*args, **kwargs):
            task = kwargs.get("task")
            if task == "plain_chat":
                return completion(
                    'Here is the layout note.\n<GUIDANCE>INTENT_DECISION: {"classification":"candidate","claims":[{"normalizedMeaning":"clearer spacing","subjectType":"space","subjectText":"center","attributeType":"density","attributeText":"spacing","direction":"decrease","degree":"excessive","aspect":"visual","scopeType":"region","scopeText":"center","evidenceSpan":"clearer spacing"}],"cardText":"My tentative view is that you prefer clearer spacing in the center. You can adjust this."}</GUIDANCE>'
                )
            if task == "intent_candidate_review":
                review_calls.append(kwargs.get("response_format_override"))
                if len(review_calls) == 1:
                    return completion(
                        {
                            "classification": "candidate",
                            "claims": [
                                {
                                    "normalizedMeaning": "clearer spacing",
                                    "subjectType": "space",
                                    "subjectText": "center",
                                    "attributeType": "density",
                                    "attributeText": "spacing",
                                    "direction": "decrease",
                                    "degree": "excessive",
                                    "aspect": "visual",
                                    "scopeType": "region",
                                    "scopeText": "center",
                                    "evidenceSpan": "clearer spacing",
                                }
                            ],
                            "cardText": "My tentative view is that you prefer clearer spacing in the center. You can adjust this.",
                            "cardTextValid": True,
                            "bodyValid": False,
                            "relation": "none",
                            "conflictingHypothesisIds": [],
                            "explanation": None,
                            "issues": ["Needs clearer focus on spacing consequence"],
                            "reviewedActiveClaims": [],
                            "answeredQuestionIds": [],
                        }
                    )
                return completion({"bodyValid": True, "issues": []})
            if task == "intent_body_repair":
                return completion(
                    {
                        "bodyParagraphs": [
                            "The center currently groups tiles tightly, which affects how clearly the opening space reads.",
                            "Whether a sparser center makes the opening push feel easier or thinner remains uncertain.",
                        ]
                    }
                )
            raise AssertionError(f"Unexpected task {task}")

        orig_repair = intent_and_review._repair_intent_components_async

        async def spy_repair(**kwargs):
            repair_map_facts.append(kwargs.get("map_facts"))
            return await orig_repair(**kwargs)

        orig_method = service.CoCreationLLMService.repair_intent_components_async
        try:
            service.CoCreationLLMService.repair_intent_components_async = lambda self, *a, **kw: spy_repair(**kw)
            with llm_scope(completion_requester=RecordedAsyncCall(side_effect=mock_req), clock=clock) as svc:
                svc.generate_plain_chat(
                    conversation=[{"role": "user", "content": "I want clearer spacing in the center."}],
                    rows=AUDIT_ROWS,
                    request_id="chat-body-recheck",
                    language="en",
                    solver_metrics=None,
                    play_summary=None,
                    stage_context={"_reliabilityState": state, "stageSnapshot": build_stage_snapshot(AUDIT_ROWS)},
                )
        finally:
            service.CoCreationLLMService.repair_intent_components_async = orig_method

        self.assertIsInstance(repair_map_facts[0], dict)
        self.assertEqual(len(review_calls), 2)
        self.assertIsNotNone(review_calls[1])
        self.assertFalse(state["bodyReviewRequired"])

    def test_intent_and_review_4xx_empty_active_conflict_and_unclear_challenge(self):
        clock = FakeClock(100.0)
        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=_api_status(400)), clock=clock):
            with self.assertRaises(LLMServiceError) as raised:
                intent_and_review.review_intent_feedback(
                    "More spacing", [], "No prior turns", "en", "req-400"
                )
        self.assertFalse(raised.exception.retryable)

        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=_api_status(400)), clock=clock):
            with self.assertRaises(LLMServiceError) as raised_q:
                intent_and_review.review_question_answers(
                    [{"questionId": "q1", "question": "Which?"}], "Water", "en", "req-q-400"
                )
        self.assertFalse(raised_q.exception.retryable)

        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=_api_status(400)), clock=clock):
            with self.assertRaises(LLMServiceError) as raised_rw:
                intent_and_review.rewrite_intent_progress("inclination", "Less water", ["Less water"], "en", "req-rw-400")
        self.assertFalse(raised_rw.exception.retryable)

        conflict_payload = {
            "verdict": "conflict",
            "explanation": "Direction one pushes more obstacles. Direction two reduces obstacles simultaneously.",
            "supersedesHypothesisIds": [],
            "conflictingHypothesisIds": [],
        }
        with llm_scope(completion_requester=RecordedAsyncCall(return_value=completion(conflict_payload)), clock=clock):
            review = intent_and_review.review_intent_feedback(
                "Self-contradictory preference", [], "Evidence text", "en", "req-conflict-empty-active"
            )
        self.assertEqual(review["verdict"], "conflict")

        unclear_payload = {"relation": "different", "merit": "unclear", "comparison": ""}
        with llm_scope(completion_requester=RecordedAsyncCall(return_value=completion(unclear_payload)), clock=clock):
            challenge = intent_and_review.classify_challenge_reason(
                "Maybe?",
                {"primary": "Less water", "secondary": "Keep walls"},
                "Add wall",
                "req-unclear",
                comparison_validator=lambda text: text,
            )
        self.assertIsNone(challenge["comparison"])

        # Post-composition coordinateLinks re-filtering drops orphaned links when _strip_unverified_state_claims runs.
        orig_clar = service.CoCreationLLMService.review_proposal_clarification
        try:
            async def fake_clar(self, api_key, base_url, body, *a, **kw):
                return (body, "")
            service.CoCreationLLMService.review_proposal_clarification = fake_clar
            with llm_scope(
                completion_requester=RecordedAsyncCall(
                    return_value=completion(
                        "B1 -> (3,6). We already edited the stage.\n"
                        '<GUIDANCE>COORDINATE_LINKS: [{"text":"B1 -> (3,6)","from":{"row":3,"column":5},"to":{"row":3,"column":6}},'
                        '{"text":"We already edited the stage","from":{"row":3,"column":5},"to":{"row":3,"column":6}}]</GUIDANCE>'
                    )
                ),
                intent_candidate_reviewer=RecordedAsyncCall(
                    return_value={
                        "classification": "none",
                        "claims": [],
                        "cardText": "",
                        "cardTextValid": True,
                        "bodyValid": True,
                        "relation": "none",
                        "conflictingHypothesisIds": [],
                        "explanation": None,
                        "issues": [],
                        "reviewedActiveClaims": [],
                        "reviewVersion": "test",
                    }
                ),
                clock=clock,
            ) as svc:
                chat_res = svc.generate_plain_chat(
                    conversation=[{"role": "user", "content": "Review"}],
                    rows=test_llm_client.OPERATION_BASE_ROWS,
                    request_id="orphan-links",
                    language="en",
                    solver_metrics=None,
                    play_summary=None,
                    stage_context={"conversationOnly": True},
                )
        finally:
            service.CoCreationLLMService.review_proposal_clarification = orig_clar
        self.assertTrue(
            all(link["text"] in chat_res.assistant_message for link in chat_res.guidance.get("coordinateLinks") or [])
        )

        # Attempt 1 of _classify_manual_edit_conflict runs even when remaining < min_retry_budget_seconds.
        conflict_payload = {
            "comparisons": {
                "direction_1": {
                    "relation": "aligned",
                    "explanation": "The manual edit opens the same water corridor requested by the designer.",
                }
            }
        }
        tight_clock = FakeClock(100.0)
        with llm_scope(
            config={"min_retry_budget_seconds": 20.0},
            completion_requester=RecordedAsyncCall(return_value=completion(conflict_payload)),
            clock=tight_clock,
        ):
            conflict_res = intent_and_review._classify_manual_edit_conflict(
                [
                    {"id": "d1", "kind": "design_goal", "fact": "Open water"},
                    {"id": "e1", "kind": "verified_change_summary", "fact": "Removed water at (4,5)"},
                ],
                "en",
                "req-tight-conflict",
                tight_clock.monotonic() + 10.0,
            )
        self.assertEqual(conflict_res["verdict"], "no_conflict")

        # classify_turn_understanding must NOT emit phantom 'proposed' repair trace when parse fails (payload={}).
        from cocreation.domain.proposal_diagnostics import CURRENT_PROPOSAL_DIAGNOSTIC, ProposalDiagnostic

        diag = ProposalDiagnostic({"requestId": "req-no-phantom"})
        diag_token = CURRENT_PROPOSAL_DIAGNOSTIC.set(diag)
        try:
            attempt_n = 0

            async def mock_understanding(*args, **kwargs):
                nonlocal attempt_n
                attempt_n += 1
                if attempt_n == 1:
                    return completion("not valid json")
                return completion(
                    {
                        "acts": ["explanation_request"],
                        "elements": ["water"],
                        "evidenceSpan": "What does water do?",
                        "directionSufficient": False,
                        "mapRelated": True,
                        "changes": [],
                        "proposalUnderstanding": None,
                        "optionDecision": None,
                        "humanEditEngagement": None,
                    }
                )

            with llm_scope(completion_requester=RecordedAsyncCall(side_effect=mock_understanding), clock=clock):
                intent_and_review.classify_turn_understanding(
                    [{"role": "user", "content": "What does water do?"}],
                    build_stage_snapshot(AUDIT_ROWS),
                    "req-no-phantom",
                    forced_proposal=False,
                    _deadline=clock.monotonic() + 30.0,
                )
        finally:
            CURRENT_PROPOSAL_DIAGNOSTIC.reset(diag_token)
        self.assertEqual(diag.pending_repairs, set())
        self.assertEqual(
            [step for step in diag.steps if step.get("stage") == "component_repair" and step.get("status") == "proposed"],
            [],
        )


class AuditProposalPlanningTests(unittest.TestCase):
    def test_compile_revision_plan_extra_key_falls_back_to_full_retry(self):
        clock = FakeClock(100.0)
        tasks = []
        valid_plan = {
            "strategies": [
                {
                    "effect": "open_route",
                    "focus": None,
                    "operators": ["remove_water"],
                    "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
                    "editBudget": 1,
                    "metricGoals": [],
                    "requiredTransitions": [],
                    "anchorEntities": [],
                    "playObjective": None,
                }
            ]
        }

        async def mock_req(*args, **kwargs):
            tasks.append(kwargs.get("task"))
            if len(tasks) == 1:
                return completion({**valid_plan, "schemaVersion": 1})
            return completion(valid_plan)

        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=mock_req), clock=clock):
            plan, attempts, _ = asyncio.run(
                proposal_planning._compile_revision_plan(
                    api_key="fake",
                    base_url="fake",
                    models=["kimi-k2.6", "kimi-k2.6"],
                    messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}],
                    request_id="plan-retry",
                    started_at=clock.monotonic(),
                    deadline=clock.monotonic() + 60.0,
                )
            )
        self.assertEqual(tasks, ["revision_plan", "revision_plan"])
        self.assertEqual(attempts, 2)
        self.assertEqual(len(plan.strategies), 1)

    def test_water_wall_transitions_supported_across_contract_and_brief(self):
        self.assertEqual(proposal_planning._operation_kind("@", "#"), ("remove_water", "add_wall"))
        self.assertEqual(proposal_planning._operation_kind("#", "@"), ("remove_wall", "add_water"))

        brief = {
            "schemaVersion": 1,
            "effect": "open_route",
            "anchors": [],
            "focus": None,
            "requiredTransitions": [{"row": 4, "column": 5, "from": "@", "to": "#"}],
            "allowedOperators": ["remove_water", "add_wall"],
            "preserve": ["outer_shell", "player", "boxes", "targets"],
            "playObjective": None,
        }
        validated = proposal_planning._validate_execution_brief(brief, AUDIT_ROWS)
        self.assertEqual(validated["requiredTransitions"][0]["to"], "#")

        with self.assertRaises(ValueError):
            proposal_planning._validate_execution_brief({**brief, "preserve": ["outer_shell", "walls"]}, AUDIT_ROWS)
        with self.assertRaises(ValueError):
            proposal_planning._validate_execution_brief({**brief, "allowedOperators": ["remove_water"]}, AUDIT_ROWS)

        # Contract execution and _validate_operation_contract accept '@' <-> '#' when both operators are permitted
        proposal_planning._validate_operation_contract(
            [(4, 3, "@", "#")],
            {
                "effect": "open_route",
                "focus": None,
                "allowedOperators": ["remove_water", "add_wall"],
                "preserve": ["outer_shell", "player", "boxes", "targets"],
                "requiredTransitions": [{"row": 4, "column": 5, "from": "@", "to": "#"}],
            },
        )
        # Note: proposal_search.validate_revision_plan_against_map lives on sibling worker backend-pipeline;
        # stubbing it here verifies _deterministic_exact_revision's operator derivation and replay end-to-end.
        orig_validate = proposal_planning.validate_revision_plan_against_map
        try:
            proposal_planning.validate_revision_plan_against_map = lambda *a, **kw: None
            exact = proposal_planning._deterministic_exact_revision(
                AUDIT_ROWS, brief, "exact-ww", "en", lambda rows: SimpleNamespace(valid=True, solvable=True, rows=rows)
            )
        finally:
            proposal_planning.validate_revision_plan_against_map = orig_validate
        self.assertEqual(exact.proposed_rows[3][4], "#")

    def test_operation_patch_request_and_requirements_review_and_bindings(self):
        contract = {"strategies": [{"strategyIndex": 1}]}
        patch = proposal_planning._operation_patch_request(
            {
                "candidates": [
                    {"strategyIndex": 1, "operations": [{"row": 2, "column": 2, "to": "#"}], "extra": True},
                    {"strategyIndex": 1, "operations": [{"row": "bad", "column": 2}, {"row": 3, "column": "x", "to": "."}]},
                ]
            },
            contract,
            AUDIT_ROWS,
        )
        self.assertIn("candidate-1", patch.targets)
        self.assertIn("candidate-2.operation-1.row", patch.targets)
        self.assertIn("candidate-2.operation-1.to", patch.targets)
        self.assertIn("candidate-2.operation-2.column", patch.targets)

        turns = [{"id": "t1", "content": "Make it simpler"}]
        snap = build_stage_snapshot(AUDIT_ROWS)
        record = {"requirements": [{"requirementId": "req-1"}]}
        valid_false, failed_false = proposal_planning._requirement_review_components(
            {"accepted": False, "issues": []}, record, turns
        )
        self.assertNotIn("issues", valid_false)
        self.assertIn("issues", failed_false)

        valid_true, failed_true = proposal_planning._requirement_review_components(
            {
                "accepted": True,
                "issues": [
                    {
                        "requirementIds": ["req-1"],
                        "kind": "evidence",
                        "evidenceSpans": ["Make it simpler"],
                        "repairInstruction": "Fix",
                    }
                ],
            },
            record,
            turns,
        )
        self.assertNotIn("accepted", valid_true)
        self.assertIn("accepted", failed_true)

        req_patch = proposal_planning._requirements_patch_request(
            {
                "requirements": [
                    {
                        "sourceTurnId": "t1",
                        "evidenceSpan": "Make it simpler",
                        "component": "gameplay",
                        "property": "experience",
                        "relation": "seek",
                        "strength": "goal",
                        "entities": [],
                        "value": None,
                        "unit": "none",
                        "focused": True,
                        "priorityEvidenceSpan": "",
                        "statement": "Make the stage simpler.",
                    }
                ],
                "automaticBindings": [{"entity": "B1", "reason": "Closest active box"}],
                "exactTransitions": ["invalid"],
            },
            {},
            turns,
            snap,
        )
        self.assertNotIn("automaticBindings", req_patch.targets)
        self.assertIn("exactTransitions", req_patch.targets)

    def test_connected_outer_shell_and_clarification_zh_mode_and_budget_retryable(self):
        shell = proposal_planning._connected_outer_shell(VOID_SHELL_ROWS)
        self.assertIn((2, 0), shell)
        self.assertNotIn((5, 3), shell)

        obs = proposal_planning._proposal_clarification_observability(
            "Only one option.",
            "Choose?",
            {"proposalClarification": {"routeEvidence": {"路线证据状态": "已核对的路线概况"}}},
        )
        self.assertEqual(obs["clarificationRouteEvidenceMode"], "verified_route_summary")

        clock = FakeClock(100.0)
        with llm_scope(
            completion_requester=RecordedAsyncCall(side_effect=APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))),
            clock=clock,
        ):
            with self.assertRaises(LLMServiceError) as raised:
                asyncio.run(
                    proposal_planning._review_proposal_clarification(
                        "fake",
                        "fake",
                        "Valid analysis sentence.",
                        "Which option?",
                        {"stageSnapshot": build_stage_snapshot(AUDIT_ROWS)},
                        "clar-conn",
                        clock.monotonic() + 30.0,
                    )
                )
        self.assertTrue(raised.exception.retryable)

        valid_plan = {
            "strategies": [
                {
                    "effect": "open_route",
                    "focus": None,
                    "operators": ["remove_wall"],
                    "preserve": ["outer_shell", "player", "boxes", "targets", "water"],
                    "editBudget": 1,
                    "metricGoals": [],
                    "requiredTransitions": [],
                    "anchorEntities": [],
                    "playObjective": None,
                }
            ]
        }
        empty_candidates = {"candidates": [{"strategyIndex": 1, "operations": [{"row": 4, "column": 6, "to": "."}]}]}

        async def mock_prop(*args, **kwargs):
            if kwargs.get("task") == "revision_plan":
                return completion(valid_plan)
            if kwargs.get("task") in {"operation_candidates", "operation_candidates_patch"}:
                return completion(
                    empty_candidates
                    if kwargs.get("task") == "operation_candidates"
                    else {
                        "patches": [
                            {
                                "componentId": "operations",
                                "field": "candidate-1.operations",
                                "valueJson": json.dumps(empty_candidates["candidates"][0]["operations"]),
                                "evidenceRefs": ["snapshot"],
                            }
                        ]
                    }
                )
            return completion(valid_plan)

        orig_search = proposal_planning.search_revision_plan
        try:
            proposal_planning.search_revision_plan = lambda *a, **kw: (_ for _ in ()).throw(
                ProposalSearchExhausted({"deadlineReached": True, "constructedCandidates": 1, "failureReasons": {"SEARCH_BUDGET_EXCEEDED": 1}})
            )
            with llm_scope(
                completion_requester=RecordedAsyncCall(side_effect=mock_prop),
                solver_fn=RecordedCall(return_value={"valid": True, "solvable": True, "solutionSteps": 8, "solutionPushes": 2, "searchedStates": 3, "solution": "RR"}),
                clock=clock,
            ):
                with self.assertRaises(LLMServiceError) as raised_budget:
                    proposal_planning._generate_revision_search_proposal_sync(
                        api_key="fake",
                        base_url="fake",
                        conversation=[{"role": "user", "content": "Go ahead and revise."}],
                        rows=AUDIT_ROWS,
                        request_id="budget-prop",
                        language="en",
                        proposal_validator=lambda rows: (_ for _ in ()).throw(ValueError("The proposed map is unsolvable.")),
                        stage_context={"authorizedRevisionBrief": "Open the center wall", "stageSnapshot": build_stage_snapshot(AUDIT_ROWS)},
                    )
        finally:
            proposal_planning.search_revision_plan = orig_search

        self.assertNotEqual(raised_budget.exception.code, "DETERMINISTIC_SEARCH_EXHAUSTED")
        self.assertTrue(raised_budget.exception.retryable)


if __name__ == "__main__":
    unittest.main()
