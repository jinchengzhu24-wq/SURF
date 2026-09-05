import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_DIR.parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as backend
from level_validation import summarize_stage_changes, summarize_verified_diff
from llm_client import LLMExecutionResult, LLMServiceError, build_chat_messages


class CoCreationPrototypeApiTests(unittest.TestCase):
    def test_automatic_candidate_is_frozen_into_actionable_purple_offer(self):
        proposed = list(backend.SAMPLE_ROWS)
        proposed[1] = "##.........#"
        execution = LLMExecutionResult(
            assistant_message="candidate",
            attempts_used=2,
            request_id="automatic-offer-test",
            proposed_rows=proposed,
            revision_plan={"strategies": [{}]},
            revision_contract={
                "authorizedBrief": "增加局部路线判断",
                "strategies": [{
                    "strategyIndex": 1,
                    "effect": "adjust_internal_walls",
                    "focus": {"row": 2, "column": 2, "radius": 1},
                    "allowedOperators": ["add_wall"],
                    "preserve": ["outer_shell", "unrelated_areas"],
                    "requiredTransitions": [],
                    "anchorEntities": [],
                    "playObjective": "route_choice",
                }],
            },
            revision_operations=[{"row": 2, "column": 2, "from": ".", "to": "#"}],
            proposal_diagnostics={
                "selectedStrategyIndex": 1,
                "objectivePolicy": {"requiresMechanismEvidence": True},
                "mechanismEvidence": {"passed": True},
            },
            guidance={"move": "deliver_revision", "proposalOffer": None},
        )

        frozen = backend._materialize_verified_automatic_offer(
            execution,
            backend.SAMPLE_ROWS,
            "zh-CN",
            {},
        )

        self.assertIsNone(frozen.proposed_rows)
        offer = frozen.guidance["proposalOffer"]
        self.assertEqual(
            offer["executionBrief"]["requiredTransitions"],
            [{"row": 2, "column": 2, "from": ".", "to": "#"}],
        )
        self.assertEqual(frozen.guidance["move"], "offer_revision")

    def test_candidate_failure_is_not_mislabeled_as_upstream_rejection(self):
        exception = LLMServiceError(
            "HARD_OBJECTIVE_NOT_MET",
            "metric failed",
            "hard-objective-failure-test",
            False,
            3,
            422,
        )
        exception.proposal_diagnostics = {
            "constructedCandidates": 2,
            "rejectionRecords": [{"category": "hard_objective_not_met"}],
        }

        execution = backend._automatic_proposal_failure_execution(
            language="zh-CN",
            request_id=exception.request_id,
            exception=exception,
        )

        self.assertIn("可量化硬指标", execution.assistant_message)
        self.assertIn("不是 Moonshot 接口拒绝", execution.assistant_message)
        self.assertIsNone(execution.guidance["proposalOffer"])

    def setUp(self):
        self.client = TestClient(backend.app)

    def tearDown(self):
        self.client.close()

    def test_adaptive_revision_routing_distinguishes_goal_from_undirected_edit(self):
        snapshot = backend.build_stage_snapshot(backend.SAMPLE_ROWS)
        empty_claims = {"conflicts": []}

        self.assertEqual(
            backend._adaptive_revision_routing(
                "\u8bf7\u7ed9\u6211\u4e00\u4e2a\u65b9\u6848\uff0c\u6211\u60f3\u8ba9\u73a9\u5bb6\u66f4\u660e\u663e\u611f\u53d7\u7ed5\u884c\u538b\u529b",
                empty_claims,
                snapshot,
            ),
            "proposal",
        )
        self.assertEqual(
            backend._adaptive_revision_routing(
                "\u8bf7\u7ed9\u6211\u4e00\u4e2a\u65b9\u6848",
                empty_claims,
                snapshot,
            ),
            "needs_clarification",
        )
        self.assertEqual(
            backend._adaptive_revision_routing(
                "\u5e2e\u6211\u6539\u4e00\u4e0b",
                empty_claims,
                snapshot,
            ),
            "needs_clarification",
        )
        self.assertEqual(
            backend._adaptive_revision_routing(
                "\u6211\u6709\u70b9\u8ff7\u832b\uff0c\u7ed9\u6211\u4e00\u70b9\u601d\u8def",
                empty_claims,
                snapshot,
            ),
            "confused",
        )

    def test_proposal_discovery_keeps_answers_until_a_bound_plan_is_ready(self):
        turns = [
            {
                "id": "request", "role": "user", "content": "先给我个方案吧，如何制造局部死局",
                "sequence_number": 1, "guidance_json": None,
            },
            {
                "id": "ask-1", "role": "assistant", "content": "你希望哪种死局？",
                "sequence_number": 2,
                "guidance_json": '{"proposalDiscovery":{"topicId":"request","clarificationQuestionCount":1,"status":"clarifying"}}',
            },
            {
                "id": "answer-1", "role": "user", "content": "单箱误入死角",
                "sequence_number": 3, "guidance_json": None,
            },
            {
                "id": "ask-2", "role": "assistant", "content": "它应当可逆吗？",
                "sequence_number": 4,
                "guidance_json": '{"proposalDiscovery":{"topicId":"request","clarificationQuestionCount":2,"status":"clarifying"}}',
            },
            {
                "id": "answer-2", "role": "user", "content": "可以重新回到左侧通道",
                "sequence_number": 5, "guidance_json": None,
            },
            {
                "id": "ask-3", "role": "assistant", "content": "谁先开路？",
                "sequence_number": 6,
                "guidance_json": '{"proposalDiscovery":{"topicId":"request","clarificationQuestionCount":3,"status":"clarifying"}}',
            },
            {
                "id": "answer-3", "role": "user",
                "content": "先让 B2 去某个位置打开通路，B1 才能安全进入再被拉回",
                "sequence_number": 7, "guidance_json": None,
            },
        ]
        snapshot = {
            "identityStatus": "exact",
            "entities": [{"kind": "box"}, {"kind": "box"}],
        }

        discovery = backend._proposal_discovery_from_turns(turns, "stage-2")

        self.assertEqual(discovery["clarificationQuestionCount"], 3)
        self.assertIn("B2", discovery["brief"])
        self.assertEqual(
            backend._adaptive_revision_routing(
                turns[-1]["content"], {"conflicts": []}, snapshot,
                proposal_discovery=discovery,
            ),
            "proposal",
        )

    def test_proposal_clarification_continues_topic_and_owns_next_question(self):
        turns = [
            {
                "id": "request", "role": "user",
                "content": "我希望用户花费更多时间来解决这个关卡，可以如何改善",
                "sequence_number": 1, "guidance_json": None,
            },
            {
                "id": "ask-1", "role": "assistant",
                "content": "你希望增加推箱子的总步数，还是增加容易走错的陷阱点？",
                "sequence_number": 2,
                "guidance_json": '{"proposalDiscovery":{"topicId":"request","clarificationQuestionCount":1,"status":"clarifying"}}',
            },
            {
                "id": "answer-1", "role": "user",
                "content": "增加推箱子的总步数",
                "sequence_number": 3, "guidance_json": None,
            },
        ]
        snapshot = {
            "identityStatus": "exact",
            "entities": [
                {"id": "B1", "kind": "box"},
                {"id": "B2", "kind": "box"},
            ],
        }

        discovery = backend._proposal_discovery_from_turns(turns, "stage-1")
        specification = backend._proposal_clarification_spec(
            discovery, snapshot, "zh-CN"
        )

        self.assertEqual(discovery["topicId"], "request")
        self.assertEqual(discovery["askedQuestionKeys"], ["mechanism"])
        self.assertEqual(specification["questionKey"], "binding")
        self.assertIn("B1", specification["allowedEntityLabels"])
        self.assertIn("B2", specification["allowedEntityLabels"])
        self.assertTrue(specification["fallbackQuestion"].endswith(("?", "？")))
        self.assertIn("增加实际推箱次数", specification["fallbackAcknowledgement"])

    def test_server_clarification_replaces_snapshot_fallback_and_advances_count(self):
        execution = LLMExecutionResult(
            assistant_message=(
                "我会以当前保存的 Stage 为准继续分析。当前可确认："
                "B1位于第3行第6列、T1位于第7行第7列。"
            ),
            attempts_used=2,
            request_id="clarification-recovery",
            guidance={"move": "offer_perspective"},
            proposal_diagnostics={
                "groundingSentencesDropped": 2,
                "clarificationRecoveryMode": "deterministic_clarification",
            },
        )
        marked = backend._mark_proposal_discovery_guidance(
            execution,
            {
                "revisionRouting": "needs_clarification",
                "proposalDiscovery": {
                    "topicId": "request",
                    "sourceTurnId": "request",
                    "status": "clarifying",
                    "clarificationQuestionCount": 1,
                    "askedQuestionKeys": ["mechanism"],
                },
                "proposalClarification": {
                    "questionKey": "binding",
                    "fallbackQuestion": "你希望先围绕哪个箱子增加运输长度？",
                    "fallbackAcknowledgement": "明白，你希望通过增加实际推箱次数来延长解题时间。",
                    "countBefore": 1,
                    "askedQuestionKeys": ["mechanism"],
                    "topicContinued": True,
                },
            },
        )

        marker = marked.guidance["proposalDiscovery"]
        self.assertNotIn("以当前保存的 Stage 为准", marked.assistant_message)
        self.assertIn("增加实际推箱次数", marked.assistant_message)
        self.assertEqual(marked.assistant_message.count("？"), 1)
        self.assertEqual(marker["clarificationQuestionKey"], "binding")
        self.assertEqual(marker["clarificationCountBefore"], 1)
        self.assertEqual(marker["clarificationCountAfter"], 2)
        self.assertEqual(marker["clarificationQuestionCount"], 2)
        self.assertEqual(marker["askedQuestionKeys"], ["mechanism", "binding"])
        self.assertTrue(marker["proposalTopicContinued"])

    def test_one_exact_entity_binding_can_converge_before_third_question(self):
        discovery = {
            "topicId": "request",
            "status": "clarifying",
            "clarificationQuestionCount": 1,
            "userEvidence": ["请给方案", "增加推箱子的总步数", "围绕 B1 调整"],
        }
        snapshot = {
            "identityStatus": "exact",
            "entities": [
                {"id": "B1", "kind": "box"},
                {"id": "B2", "kind": "box"},
            ],
        }

        self.assertTrue(
            backend._proposal_discovery_is_sufficient(discovery, snapshot)
        )
        self.assertEqual(
            backend._adaptive_revision_routing(
                "围绕 B1 调整", {"conflicts": []}, snapshot,
                proposal_discovery=discovery,
            ),
            "proposal",
        )

    def test_server_counts_the_kimi_question_that_was_actually_displayed(self):
        message = (
            "我更在意额外运输是否会形成持续的规划压力。\n\n"
            "你希望先让哪个箱子承担这段额外运输？"
        )
        execution = LLMExecutionResult(
            assistant_message=message,
            attempts_used=1,
            request_id="kimi-clarification",
            guidance={"move": "clarify_intent"},
            proposal_diagnostics={
                "clarificationQuestion": "你希望先让哪个箱子承担这段额外运输？",
                "clarificationQuestionValidated": True,
                "clarificationAuthor": "kimi",
                "clarificationRecoveryMode": "kimi_complete",
            },
        )
        marked = backend._mark_proposal_discovery_guidance(
            execution,
            {
                "revisionRouting": "needs_clarification",
                "proposalDiscovery": {
                    "topicId": "request",
                    "sourceTurnId": "request",
                    "status": "clarifying",
                    "clarificationQuestionCount": 1,
                    "askedQuestionKeys": ["mechanism"],
                },
                "proposalClarification": {
                    "questionKey": "binding",
                    "fallbackQuestion": "你希望先围绕哪个箱子增加运输长度？",
                    "fallbackAcknowledgement": "我会继续收敛这个方向。",
                    "countBefore": 1,
                    "askedQuestionKeys": ["mechanism"],
                    "topicContinued": True,
                },
            },
        )

        self.assertEqual(marked.assistant_message, message)
        marker = marked.guidance["proposalDiscovery"]
        self.assertEqual(marker["clarificationQuestionCount"], 2)
        self.assertEqual(marker["clarificationAuthor"], "kimi")
        self.assertEqual(marker["clarificationRecoveryMode"], "kimi_complete")

    def test_proposal_discovery_completes_conservatively_after_three_when_object_is_ambiguous(self):
        discovery = {
            "topicId": "request",
            "status": "clarifying",
            "clarificationQuestionCount": 3,
            "userEvidence": ["请给我一个让箱子更难处理的方案"],
        }
        snapshot = {
            "identityStatus": "exact",
            "entities": [{"kind": "box"}, {"kind": "box"}],
        }

        self.assertEqual(
            backend._adaptive_revision_routing(
                "请继续", {"conflicts": []}, snapshot,
                proposal_discovery=discovery,
            ),
            "proposal_conservative",
        )

    def test_automatic_proposal_failure_never_uses_snapshot_fallback(self):
        exception = LLMServiceError(
            "MODEL_RESPONSE_INVALID",
            "The RevisionPlan response was not valid JSON.",
            "automatic-proposal-failure-test",
            True,
            2,
            422,
        )
        execution = backend._automatic_proposal_failure_execution(
            language="zh-CN",
            request_id="automatic-proposal-failure-test",
            exception=exception,
        )
        marked = backend._mark_proposal_discovery_guidance(
            execution,
            {
                "revisionRouting": "proposal_conservative",
                "proposalDiscovery": {
                    "topicId": "topic", "clarificationQuestionCount": 3,
                },
            },
        )

        self.assertEqual(marked.guidance["proposalDiscovery"]["status"], "failed")
        self.assertNotIn("当前保存的 Stage 为准", marked.assistant_message)
        self.assertIsNone(marked.guidance["proposalOffer"])

    def test_automatic_proposal_failure_exposes_safe_provider_reason(self):
        exception = LLMServiceError(
            "UPSTREAM_REQUEST_REJECTED",
            "Kimi returned HTTP 400: response_format is unsupported.",
            "automatic-provider-failure-test",
            False,
            1,
            502,
            provider_status=400,
            provider_error_type="invalid_request_error",
            provider_error_code="unsupported_parameter",
            provider_param="response_format",
            provider_message="response_format is unsupported",
        )
        execution = backend._automatic_proposal_failure_execution(
            language="zh-CN",
            request_id="automatic-provider-failure-test",
            exception=exception,
        )
        self.assertIn("HTTP 400", execution.assistant_message)
        self.assertIn("response_format is unsupported", execution.assistant_message)
        self.assertIn("providerMessage", execution.proposal_diagnostics)
        self.assertIsNone(execution.guidance["proposalOffer"])

    def test_automatic_proposal_timeout_is_not_described_as_rejection(self):
        exception = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "Kimi did not compile the revision plan before the proposal time limit.",
            "automatic-timeout-test",
            True,
            1,
            504,
        )
        execution = backend._automatic_proposal_failure_execution(
            language="zh-CN",
            request_id="automatic-timeout-test",
            exception=exception,
        )

        self.assertIn("请求超时", execution.assistant_message)
        self.assertNotIn("接口拒绝", execution.assistant_message)
        self.assertEqual(
            execution.proposal_diagnostics["failureClass"],
            "upstream_timeout",
        )
        self.assertIsNone(execution.guidance["proposalOffer"])

    def test_automatic_proposal_reports_truncation_before_retry_timeout(self):
        exception = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "Kimi did not compile the revision plan before the proposal time limit.",
            "automatic-truncated-timeout-test",
            True,
            2,
            504,
        )
        exception.proposal_diagnostics = {
            "failureStage": "revision_plan",
            "attemptFailures": [
                {"attempt": 1, "code": "MODEL_RESPONSE_INVALID", "failureClass": "truncated_output"},
                {"attempt": 2, "code": "UPSTREAM_TIMEOUT", "failureClass": "upstream_timeout"},
            ],
        }

        execution = backend._automatic_proposal_failure_execution(
            language="en",
            request_id="automatic-truncated-timeout-test",
            exception=exception,
        )

        self.assertIn("first RevisionPlan response reached its output limit", execution.assistant_message)
        self.assertIn("corrective retry then timed out", execution.assistant_message)
        self.assertIsNone(execution.guidance["proposalOffer"])

    def test_failed_proposal_topic_reopens_for_a_new_concrete_direction(self):
        turns = [
            {"id": "request", "role": "user", "content": "请给我一个方案", "sequence_number": 1, "guidance_json": None},
            {"id": "failed", "role": "assistant", "content": "候选未通过", "sequence_number": 2,
             "guidance_json": '{"proposalDiscovery":{"topicId":"request","status":"failed","clarificationQuestionCount":3}}'},
            {"id": "retry", "role": "user", "content": "让 B1 阻挡 B2 的通道", "sequence_number": 3, "guidance_json": None},
        ]

        discovery = backend._proposal_discovery_from_turns(turns, "stage")

        self.assertEqual(discovery["status"], "clarifying")
        self.assertIn("B1", discovery["brief"])

    def test_clarification_budget_allows_safe_completion_for_unique_water(self):
        snapshot = {
            "identityStatus": "exact",
            "waterCells": [
                {"row": 2, "column": 2},
                {"row": 2, "column": 3},
            ],
        }
        self.assertFalse(
            backend._allow_adaptive_proposal_completion(
                "\u6c34\u57df\u600e\u4e48\u6539",
                {"conflicts": []},
                snapshot,
                2,
            )
        )
        self.assertTrue(
            backend._allow_adaptive_proposal_completion(
                "\u6c34\u57df\u600e\u4e48\u6539",
                {"conflicts": []},
                snapshot,
                3,
            )
        )
        self.assertFalse(
            backend._allow_adaptive_proposal_completion(
                "\u5e2e\u6211\u6539\u4e00\u4e0b",
                {"conflicts": []},
                snapshot,
                3,
            )
        )

    def test_clarification_budget_allows_completion_for_clear_outcome_goal(self):
        snapshot = backend.build_stage_snapshot(backend.SAMPLE_ROWS)

        self.assertTrue(
            backend._allow_adaptive_proposal_completion(
                "\u8bf7\u7ed9\u6211\u4e00\u4e2a\u65b9\u6848\uff0c\u6211\u60f3\u8ba9\u73a9\u5bb6\u66f4\u660e\u663e\u611f\u53d7\u7ed5\u884c\u538b\u529b",
                {"conflicts": []},
                snapshot,
                3,
            )
        )
        self.assertFalse(
            backend._allow_adaptive_proposal_completion(
                "\u8bf7\u7ed9\u6211\u4e00\u4e2a\u65b9\u6848",
                {"conflicts": []},
                snapshot,
                3,
            )
        )
        self.assertFalse(
            backend._allow_adaptive_proposal_completion(
                "\u8bf7\u8c03\u6574\u7bb1\u5b50\u7684\u8def\u7ebf",
                {"conflicts": []},
                {
                    **snapshot,
                    "entities": [
                        *snapshot["entities"],
                        {"kind": "box", "id": "B2"},
                    ],
                },
                3,
            )
        )

    def test_conservative_map_question_extraction_ignores_generic_question(self):
        self.assertEqual(
            backend._extract_conservative_map_question(
                "The route from B1 to T1 feels narrow. Should we keep this corridor?"
            ),
            "Should we keep this corridor?",
        )
        self.assertIsNone(
            backend._extract_conservative_map_question("Would you like to continue?")
        )
        self.assertIsNone(
            backend._extract_conservative_map_question(
                "B2 goes to (8,7), then to (8,8): is that route reachable?"
            )
        )
        self.assertEqual(
            backend._extract_conservative_map_question(
                "The water changes the push rhythm. Should it create a visible detour or only affect push order?"
            ),
            "Should it create a visible detour or only affect push order?",
        )

    def test_frontend_and_static_assets_are_served(self):
        index_response = self.client.get("/")
        css_response = self.client.get("/styles.css")
        js_response = self.client.get("/app.js")

        self.assertEqual(index_response.status_code, 200)
        self.assertIn("Sokoban Co-Creation Lab", index_response.text)
        self.assertIn("cocreation-kimi-20260904-2", index_response.text)
        self.assertIn('<html lang="zh-CN">', index_response.text)
        self.assertEqual(css_response.status_code, 200)
        self.assertIn("--bg: #6f9d31", css_response.text)
        self.assertIn("--wood: #8b562c", css_response.text)
        self.assertIn("--pixel-shadow: 4px 4px 0", css_response.text)
        self.assertEqual(js_response.status_code, 200)
        self.assertIn("/api/sessions/", js_response.text)
        self.assertIn("play-attempts", js_response.text)
        self.assertIn("guidance-cues", js_response.text)
        self.assertIn("GUIDANCE_CUE_LABELS", js_response.text)
        self.assertIn("assistantBodyWithoutCues", js_response.text)
        self.assertIn("proposalForTurn", js_response.text)
        self.assertNotIn("assessmentForTurn", js_response.text)
        self.assertNotIn("createAssessmentCard", js_response.text)
        self.assertNotIn("assessmentSolution", js_response.text)
        self.assertNotIn("assessmentDifficulty", js_response.text)
        self.assertIn('language: "zh-CN"', js_response.text)
        self.assertIn('apiError.details = payload.details || null;', js_response.text)
        self.assertIn('validationFailed', js_response.text)
        self.assertIn('OPEN_OUTER_WALL', js_response.text)
        self.assertIn('validation-card.invalid', css_response.text)
        self.assertIn('body: { language: "zh-CN" }', js_response.text)
        self.assertIn("ensureVisibleTranslations()", js_response.text)
        self.assertIn("localizedAssistantTurn(turn)", js_response.text)
        self.assertIn("verifiedProposalSummary(proposal.diff)", js_response.text)
        self.assertIn("verifiedProposalMessage()", js_response.text)
        self.assertIn("guidanceForDisplay", js_response.text)
        self.assertIn("createDiscussionFocus", js_response.text)
        self.assertIn('role="log"', index_response.text)
        self.assertIn('tabindex="0"', index_response.text)
        self.assertIn("renderedMessageStageId", js_response.text)
        self.assertIn("scrollbar-gutter", css_response.text)
        self.assertIn("elements.chatForm.requestSubmit()", js_response.text)
        self.assertIn('id="returnUnityButton"', index_response.text)
        self.assertIn('elements.returnUnityButton.addEventListener("click", returnToUnity)', js_response.text)
        self.assertIn('const unityWindow = window.opener;', js_response.text)
        self.assertIn('unityWindow.focus();', js_response.text)
        self.assertIn('window.close();', js_response.text)
        self.assertIn('elements.returnUnityButton.hidden = status !== "completed" || state.session.demoMode;', js_response.text)
        self.assertIn('state.sessionId = hash.session || "";', js_response.text)
        self.assertIn('api("/api/demo-sessions"', js_response.text)
        self.assertIn('id="demoGenerationStatus"', index_response.text)
        self.assertIn('role="status"', index_response.text)
        self.assertIn("demoGenerationStatus", js_response.text)
        self.assertIn("await openCreatedSession(created.launchUrl);", js_response.text)
        self.assertIn("window.history.replaceState", js_response.text)
        self.assertIn("await initialize();", js_response.text)
        self.assertIn("正在使用算法创建示例地图", js_response.text)
        self.assertIn("Creating a sample map with the algorithm", js_response.text)
        self.assertIn('algorithm_demo', js_response.text)
        self.assertNotIn('|| !state.session.matchId', js_response.text)
        self.assertIn('.complete-card .primary-button', css_response.text)

        self.assertIn("LET'S DISCUSS / 一起聊聊", js_response.text)
        self.assertIn(".discussion-focus", css_response.text)
        self.assertIn(".guidance-cue,\n.discussion-focus", css_response.text)
        self.assertIn(".guidance-cue::before,\n.discussion-focus::before", css_response.text)
        self.assertIn(".guidance-cue-label,\n.discussion-focus-label", css_response.text)
        self.assertIn(".discussion-focus {\n    margin-top: 30px;", css_response.text)
        self.assertIn("translation-label", css_response.text)
        self.assertIn('hash.playReturn', js_response.text)
        self.assertIn('status === "sync_failed"', js_response.text)
        self.assertIn('status === "load_failed"', js_response.text)
        self.assertIn('playSyncFailed', js_response.text)
        self.assertIn('playLoadFailed', js_response.text)
        self.assertIn('pendingMessageKey()', js_response.text)
        self.assertIn('recoverPendingMessage()', js_response.text)
        self.assertIn('chatWaitingPrimary', js_response.text)
        self.assertIn('chatWaitingFallback', js_response.text)
        self.assertIn('LLM_REQUEST_TIMEOUT_MS = 120000', js_response.text)
        self.assertIn('MESSAGE_REQUEST_TIMEOUT_MS = 320000', js_response.text)
        self.assertIn('PROPOSAL_DISPLAY_LIMIT_SECONDS = 300', js_response.text)
        self.assertIn('elapsedSeconds < LLM_PRIMARY_WAIT_SECONDS', js_response.text)
        self.assertIn('elapsedSeconds} / ${PROPOSAL_DISPLAY_LIMIT_SECONDS}', js_response.text)
        self.assertIn('timeoutMs: MESSAGE_REQUEST_TIMEOUT_MS', js_response.text)
        self.assertNotIn('elapsedSeconds} / 65', js_response.text)
        self.assertIn('translationFailures', js_response.text)
        self.assertIn('translationUnavailable', js_response.text)
        self.assertIn('void ensureVisibleTranslations()', js_response.text)
        self.assertNotIn('withBusy(ensureVisibleTranslations)', js_response.text)
        self.assertIn('chatRetryPending', js_response.text)
        self.assertIn('selectedStageTurns()', js_response.text)
        self.assertIn('turn.versionId === state.selectedVersionId', js_response.text)
        self.assertIn('version?.openingTurnId', js_response.text)
        self.assertIn('supersededAssessmentTurnIds', js_response.text)
        self.assertIn('return [openingTurn, ...directTurns]', js_response.text)
        self.assertIn('noStageConversation', js_response.text)
        self.assertIn('id="chatRequestStatus"', index_response.text)
        self.assertIn('.chat-request-status', css_response.text)
        self.assertIn('.map-panel .palette-button small', css_response.text)
        self.assertIn('font-size: 10px', css_response.text)
        self.assertIn('font-weight: 700;', css_response.text)
        self.assertIn('font-family: "Segoe UI", Arial, sans-serif;\n    font-weight: 700;', css_response.text)
        self.assertIn('textarea::placeholder {\n    font-weight: 700;', css_response.text)
        self.assertIn('.method-pill {\n    border-radius: 3px;\n    font-weight: 800;', css_response.text)
        self.assertIn('.guidance-cue-manual-edit', css_response.text)
        self.assertIn('.guidance-cue-question', css_response.text)
        self.assertIn('.guidance-cue-revision', css_response.text)
        self.assertIn('.guidance-cue-intent', css_response.text)
        self.assertIn('.guidance-cue-warning', css_response.text)
        self.assertIn('.guidance-cue-tradeoff', css_response.text)
        self.assertIn('font-weight: 800;', css_response.text)
        self.assertIn('manual_edit: "MANUAL EDIT / 手动编辑"', js_response.text)
        self.assertIn('question: "LET\'S DISCUSS / 一起聊聊"', js_response.text)
        self.assertIn("最后一次模型尝试返回了空白内容", js_response.text)
        self.assertIn('if (question) bubble.appendChild(createDiscussionFocus(question));', js_response.text)
        self.assertIn('proposalCompanionManualText', js_response.text)
        self.assertIn('revision: "REVISION / 修改建议"', js_response.text)
        self.assertIn('intent: "TENTATIVE INTENT / 暂定意图"', js_response.text)
        self.assertIn('"execute_revision", "draftSuggestedRevision"', js_response.text)
        self.assertIn('isLatestRevisionOfferTurn', js_response.text)
        self.assertIn('guidance-cue-button-stale', css_response.text)
        self.assertIn('() => prefillProposalConsent(offer)', js_response.text)
        self.assertIn('elements.messageInput.value = t("proposalConsent")', js_response.text)
        self.assertIn('warning: "WARNING / 风险提示"', js_response.text)
        self.assertIn('tradeoff: "WARNING / 风险提示"', js_response.text)
        self.assertNotIn('.guidance-offer {', css_response.text)
        self.assertNotIn("createAssessmentCard", js_response.text)

    def test_relaxed_revision_suggestion_names_the_lowered_requirement_before_generation(self):
        execution = backend._relaxed_revision_suggestion_execution(
            {
                "recentGuidance": {
                    "relaxationOffer": {
                        "status": "awaiting_confirmation",
                        "originalBrief": "重排下半区目标与推进路线",
                        "relaxedBrief": "Preserve the core direction and realize one local effect.",
                        "baseVersionId": "version-1",
                        "briefHash": "brief-hash",
                    }
                }
            },
            "zh-CN",
            "relaxed-suggestion-test",
        )

        self.assertIsNone(execution.proposed_rows)
        self.assertIn("现在还不会直接改图", execution.assistant_message)
        self.assertIn(
            "降低后的修改要求",
            execution.guidance["proposalOffer"]["rationale"],
        )
        self.assertEqual(
            execution.guidance["relaxationOffer"]["status"],
            "suggestion_ready",
        )

    def test_stage_change_summary_classifies_sokoban_components(self):
        before = [
            "############",
            "#..........#",
            "#..#.......#",
            "#...@......#",
            "#...p......#",
            "#...s.t....#",
            "#..........#",
            "#..........#",
            "#..........#",
            "############",
        ]
        after = [
            "#####.######",
            "#..........#",
            "#...#......#",
            "#....@.....#",
            "#....p.....#",
            "#....s.t...#",
            "#..........#",
            "#..........#",
            "#..........#",
            "############",
        ]

        summary = summarize_stage_changes(before, after)

        self.assertEqual(
            summary["components"],
            ["outerShell", "water", "internalWalls", "boxes", "targets", "player"],
        )
        self.assertEqual(summary["changedCellCount"], 11)
        self.assertEqual(summary["componentCellCounts"]["player"], 2)

    def test_verified_proposal_summary_is_derived_from_every_actual_small_diff(self):
        before = list(backend.SAMPLE_ROWS)
        after = list(before)
        after[1] = "#.@@.......#"

        summary = summarize_verified_diff(before, after, "zh-CN")

        self.assertIn("共2格", summary)
        self.assertIn("第2行第3列：地面→水面", summary)
        self.assertIn("第2行第4列：地面→水面", summary)

    def test_cocreation_frontend_uses_active_dashboard_visual_contract(self):
        dashboard_css = (REPOSITORY_ROOT / "Frontend" / "styles.css").read_text(
            encoding="utf-8"
        )
        cocreation_css = (
            REPOSITORY_ROOT / "CoCreationPrototype" / "Frontend" / "styles.css"
        ).read_text(encoding="utf-8")
        shared_tokens = (
            "--bg: #6f9d31",
            "--wood: #8b562c",
            "--stone-dark: #41494d",
            "--purple: #67478c",
            "--pixel-shadow: 4px 4px 0",
            'font-family: Consolas, "Courier New", monospace',
            "border: 3px solid var(--stone-dark)",
            "align-items: stretch",
        )

        for token in shared_tokens:
            self.assertIn(token, dashboard_css)
            self.assertIn(token, cocreation_css)

    def test_new_session_request_defaults_to_chinese(self):
        request = backend.CreateSessionRequest(
            rows=list(backend.SAMPLE_ROWS),
            initialDraftMethod="partial_completion",
            idempotencyKey="default-language-contract",
        )

        self.assertEqual(request.language, "zh-CN")

    def test_sample_has_expected_shape_and_objects(self):
        response = self.client.get("/api/sample")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["width"], 12)
        self.assertEqual(payload["height"], 10)
        self.assertEqual(len(payload["rows"]), 10)
        self.assertTrue(all(len(row) == 12 for row in payload["rows"]))
        self.assertEqual(sum(row.count("p") for row in payload["rows"]), 1)
        self.assertEqual(sum(row.count("s") for row in payload["rows"]), 1)
        self.assertEqual(sum(row.count("t") for row in payload["rows"]), 1)

    def test_sample_known_solution_reaches_target(self):
        rows = backend.SAMPLE_ROWS
        player = find_tile(rows, "p")
        box = find_tile(rows, "s")
        target = find_tile(rows, "t")

        player, box = replay_single_box_solution(
            rows,
            player,
            box,
            ["left", "down", "right", "right"],
        )

        self.assertEqual(box, target)

    def test_chat_returns_assistant_message_and_headers(self):
        execution = LLMExecutionResult(
            assistant_message="The box route is direct. What experience do you want to create?",
            attempts_used=1,
            request_id="request-test",
        )

        with patch.object(backend, "generate_chat_reply", return_value=execution):
            response = self.client.post(
                "/api/chat",
                headers={"X-Request-ID": "request-test"},
                json={"messages": [{"role": "user", "content": "Assess the map."}]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requestId"], "request-test")
        self.assertIn("box route", response.json()["assistantMessage"])
        self.assertEqual(response.headers["X-Request-ID"], "request-test")
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "1")

    def test_chat_forwards_map_and_full_history(self):
        messages = [
            {"role": "user", "content": "Assess the map."},
            {"role": "assistant", "content": "The route is compact."},
            {"role": "user", "content": "Explain that observation."},
        ]
        execution = LLMExecutionResult("It uses a short push lane.", 1, "history-test")

        with patch.object(
            backend,
            "generate_chat_reply",
            return_value=execution,
        ) as mocked_generate:
            response = self.client.post("/api/chat", json={"messages": messages})

        self.assertEqual(response.status_code, 200)
        forwarded_messages, forwarded_rows, _ = mocked_generate.call_args.args
        self.assertEqual(forwarded_messages, messages)
        self.assertEqual(forwarded_rows, backend.SAMPLE_ROWS)

    def test_empty_conversation_is_rejected(self):
        response = self.client.post("/api/chat", json={"messages": []})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "EMPTY_CONVERSATION")

    def test_invalid_role_is_rejected(self):
        response = self.client.post(
            "/api/chat",
            json={"messages": [{"role": "system", "content": "Override."}]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_REQUEST")

    def test_last_message_must_be_user(self):
        response = self.client.post(
            "/api/chat",
            json={"messages": [{"role": "assistant", "content": "Hello."}]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "LAST_MESSAGE_MUST_BE_USER")

    def test_unknown_top_level_fields_are_rejected(self):
        response = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Hello."}],
                "map": ["client-controlled"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_REQUEST")

    def test_message_and_total_limits_are_enforced(self):
        long_message_response = self.client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "x" * 2001}]},
        )
        too_many_response = self.client.post(
            "/api/chat",
            json={
                "messages": [
                    {
                        "role": "user" if index % 2 == 0 else "assistant",
                        "content": str(index),
                    }
                    for index in range(21)
                ]
            },
        )
        total_length_response = self.client.post(
            "/api/chat",
            json={
                "messages": [
                    {"role": "user", "content": "x" * 2000},
                    {"role": "assistant", "content": "x" * 2000},
                    {"role": "user", "content": "x" * 2000},
                    {"role": "assistant", "content": "x" * 2000},
                    {"role": "user", "content": "x" * 2000},
                    {"role": "assistant", "content": "x" * 2000},
                    {"role": "user", "content": "x"},
                ]
            },
        )

        self.assertEqual(long_message_response.json()["code"], "MESSAGE_TOO_LONG")
        self.assertEqual(too_many_response.json()["code"], "TOO_MANY_MESSAGES")
        self.assertEqual(total_length_response.json()["code"], "CONVERSATION_TOO_LONG")

    def test_llm_error_uses_safe_error_shape(self):
        error = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "DeepSeek did not respond before the timeout.",
            "timeout-test",
            True,
            2,
            504,
        )

        with patch.object(backend, "generate_chat_reply", side_effect=error):
            response = self.client.post(
                "/api/chat",
                headers={"X-Request-ID": "timeout-test"},
                json={"messages": [{"role": "user", "content": "Hello."}]},
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(
            response.json(),
            {
                "code": "UPSTREAM_TIMEOUT",
                "message": "DeepSeek did not respond before the timeout.",
                "requestId": "timeout-test",
                "retryable": True,
            },
        )
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "2")

    def test_system_prompt_is_grounded_and_neutral(self):
        prompt_messages = build_chat_messages(
            [{"role": "user", "content": "What do you notice?"}],
            backend.SAMPLE_ROWS,
            solver_metrics={
                "valid": True,
                "solvable": True,
                "searchedStates": 42,
                "solutionSteps": 12,
                "solutionPushes": 4,
                "solution": "UURRDDLL",
            },
        )
        system_prompt = prompt_messages[0]["content"]

        self.assertIn('"rows":["############"', system_prompt)
        self.assertIn('"dimensions":{"rows":10,"columns":12}', system_prompt)
        self.assertEqual(system_prompt.count("Current Stage Snapshot"), 1)
        self.assertNotIn("beforeRows", system_prompt)
        self.assertNotIn("afterRows", system_prompt)
        self.assertIn("authoritative", system_prompt)
        self.assertIn("up to three tightly related clarification questions", system_prompt)
        self.assertIn("tentative", system_prompt)
        self.assertIn("one level with Stages as saved-version indices", system_prompt)
        self.assertIn("offer_revision", system_prompt)
        self.assertIn("Do not claim a map was changed", system_prompt)
        self.assertIn("Draft provenance and attribution", system_prompt)
        self.assertIn("Continuous progress context", system_prompt)
        self.assertIn("Never create a confirmed decision", system_prompt)
        self.assertIn("coordinateLinks", system_prompt)
        self.assertIn('"solutionSteps": 12', system_prompt)
        self.assertIn('"solutionPushes": 4', system_prompt)
        self.assertNotIn("UURRDDLL", system_prompt)
        self.assertNotIn('"solution":', system_prompt)
        self.assertNotIn("Competitive", system_prompt)
        self.assertNotIn("Supportive", system_prompt)
        self.assertEqual(prompt_messages[-1]["content"], "What do you notice?")


def find_tile(rows, tile):
    for y, row in enumerate(rows):
        x = row.find(tile)

        if x >= 0:
            return x, y

    raise AssertionError(f"Tile {tile!r} was not found")


def replay_single_box_solution(rows, player, box, moves):
    directions = {
        "left": (-1, 0),
        "right": (1, 0),
        "up": (0, -1),
        "down": (0, 1),
    }

    for move in moves:
        delta_x, delta_y = directions[move]
        destination = (player[0] + delta_x, player[1] + delta_y)

        if destination == box:
            box_destination = (box[0] + delta_x, box[1] + delta_y)
            assert rows[box_destination[1]][box_destination[0]] != "#"
            box = box_destination

        assert rows[destination[1]][destination[0]] != "#"
        player = destination

    return player, box


if __name__ == "__main__":
    unittest.main()
