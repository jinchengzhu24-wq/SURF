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
    def setUp(self):
        self.client = TestClient(backend.app)

    def tearDown(self):
        self.client.close()

    def test_frontend_and_static_assets_are_served(self):
        index_response = self.client.get("/")
        css_response = self.client.get("/styles.css")
        js_response = self.client.get("/app.js")

        self.assertEqual(index_response.status_code, 200)
        self.assertIn("Sokoban Co-Creation Lab", index_response.text)
        self.assertIn("cocreation-translation-parallel-20260830-6", index_response.text)
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
        self.assertIn('timeoutMs: 65000', js_response.text)
        self.assertIn('elapsedSeconds < 40', js_response.text)
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
        self.assertIn('t("draftSuggestedRevision")', js_response.text)
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

        self.assertIn("\n".join(backend.SAMPLE_ROWS), system_prompt)
        self.assertIn("read-only", system_prompt)
        self.assertIn("at most one central question", system_prompt)
        self.assertIn("tentative, correctable hypothesis", system_prompt)
        self.assertIn("Every difficulty statement must explicitly use", system_prompt)
        self.assertIn("exactly one level", system_prompt)
        self.assertIn("Every Stage number is only a saved-version index", system_prompt)
        self.assertIn("offer_revision", system_prompt)
        self.assertIn("edit the level directly with the tile tools", system_prompt)
        self.assertIn("newly saved human_edit Stage", system_prompt)
        self.assertIn("only turns attached to this saved Stage", system_prompt)
        self.assertIn("accepted LLM proposal may be carried forward", system_prompt)
        self.assertIn("manual_edit uiCue", system_prompt)
        self.assertIn("warning uiCue", system_prompt)
        self.assertIn("only when strong", system_prompt)
        self.assertIn("Ordinary uncertainty, route trade-offs", system_prompt)
        self.assertIn("thoughtful, equal design peer", system_prompt)
        self.assertIn("do not end with a question by habit", system_prompt)
        self.assertIn('"followUpQuestion":null', system_prompt)
        self.assertIn("do not recite metrics or spell out a move sequence", system_prompt)
        self.assertIn("differ from the current saved Stage by at least one tile", system_prompt)
        self.assertIn("unless the before/after rows prove it", system_prompt)
        self.assertIn('"uiCues":[]', system_prompt)
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
