"""Research projections exercise real API transitions with an isolated database."""
import json
import unittest
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import test_sessions as fixtures


backend = fixtures.backend
repository = fixtures.repository


class ResearchProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.CoCreationSessionTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.CoCreationSessionTests.tearDownClass()

    def setUp(self):
        self.fixture = fixtures.CoCreationSessionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.session_id = self.fixture.session_id
        self.events = []
        self.sync = patch.object(
            backend, "synchronize_cocreation_event_with_online_match", side_effect=self.capture,
        )
        self.sync.start()
        self.addCleanup(self.sync.stop)

    def capture(self, session, event):
        self.events.append(json.loads(json.dumps(event)))

    def latest(self, node_type):
        return next(event for event in reversed(self.events) if event.get("nodeType") == node_type)

    def read(self):
        return self.fixture.read_session()

    def test_first_stage_retries_original_promotion_without_resetting_deadline(self):
        created = self.client.post("/api/sessions", json={
            "rows": fixtures.SAMPLE_ROWS, "initialDraftMethod": "partial_completion",
            "language": "en", "idempotencyKey": "first-sync-new", "matchId": "match-new", "playerNumber": 1,
        }).json()
        session_id = created["sessionId"]
        bootstrap = parse_qs(urlparse(created["launchUrl"]).fragment)["bootstrap"][0]
        self.client.post(f"/api/sessions/{session_id}/browser-access", json={"bootstrapToken": bootstrap})
        def fail_once(session, event):
            self.capture(session, event)
            if len(self.events) == 1:
                raise backend.ApiError(503, "ONLINE_FLOW_SYNC_UNAVAILABLE", "Retry", retryable=True)
        with patch.object(backend, "synchronize_cocreation_event_with_online_match", side_effect=fail_once):
            first = self.client.patch(f"/api/sessions/{session_id}/language", json={"language": "en"})
            self.assertEqual(first.status_code, 503)
            with repository.connect() as database:
                before = dict(repository.get_session(database, session_id))
            retry = self.client.patch(f"/api/sessions/{session_id}/language", json={"language": "en"})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(self.events[0], self.events[1])
        self.assertEqual(self.events[0]["eventType"], "first_stage")
        with repository.connect() as database:
            after = repository.get_session(database, session_id)
            self.assertEqual(before["deadline_at"], after["deadline_at"])
            self.assertEqual(database.execute("SELECT COUNT(*) FROM level_versions WHERE session_id = ?", (session_id,)).fetchone()[0], 1)

    def test_restore_sync_retry_keeps_one_version_and_both_stage_references(self):
        original = self.read()["currentVersionId"]
        edited = self.client.post(f"/api/sessions/{self.session_id}/versions", json={
            "rows": fixtures.EDITED_ROWS, "baseVersionId": original, "idempotencyKey": "restore-before",
        }).json()["currentVersionId"]
        self.events.clear()
        def fail_once(session, event):
            self.capture(session, event)
            if len(self.events) == 1:
                raise backend.ApiError(503, "ONLINE_FLOW_SYNC_UNAVAILABLE", "Retry", retryable=True)
        request = {"baseVersionId": edited, "idempotencyKey": "restore-retry"}
        with patch.object(backend, "synchronize_cocreation_event_with_online_match", side_effect=fail_once):
            first = self.client.post(f"/api/sessions/{self.session_id}/versions/{original}/restore", json=request)
            retry = self.client.post(f"/api/sessions/{self.session_id}/versions/{original}/restore", json=request)
        self.assertEqual(first.status_code, 503)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(len(retry.json()["versions"]), 3)
        self.assertEqual(self.events[0], self.events[1])
        self.assertEqual(self.events[1]["source"], "restored")
        self.assertEqual(self.events[1]["restoredFromStageNumber"], 1)
        self.assertEqual(self.events[1]["replacedStageNumber"], 2)
        self.assertEqual(self.events[1]["rows"], fixtures.SAMPLE_ROWS)

    def deadline_retry(self, fail_type):
        original = self.read()["currentVersionId"]
        with repository.connect(immediate=True) as database:
            expired = (backend.parse_time(backend.utc_now()) - timedelta(seconds=1)).isoformat()
            database.execute("UPDATE design_sessions SET deadline_at = ? WHERE id = ?", (expired, self.session_id))
        request = {"baseVersionId": original, "rows": fixtures.EDITED_ROWS, "idempotencyKey": "final-retry"}
        failed = False
        def fail_once(session, event):
            nonlocal failed
            self.capture(session, event)
            if not failed and event["eventType"] == fail_type:
                failed = True
                raise backend.ApiError(503, "ONLINE_FLOW_SYNC_UNAVAILABLE", "Retry", retryable=True)
        with patch.object(backend, "synchronize_cocreation_event_with_online_match", side_effect=fail_once):
            first = self.client.post(f"/api/sessions/{self.session_id}/finalize", json=request)
            retry = self.client.post(f"/api/sessions/{self.session_id}/finalize", json=request)
        self.assertEqual(first.status_code, 503)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(len(retry.json()["versions"]), 2)
        self.assertNotEqual(retry.json()["finalVersionId"], original)
        self.assertEqual([event["eventType"] for event in self.events[-2:]], ["stage", "final"])
        changed = self.client.post(f"/api/sessions/{self.session_id}/finalize", json={**request, "rows": fixtures.SAMPLE_ROWS})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["code"], "IDEMPOTENCY_CONFLICT")

    def test_deadline_stage_sync_retry(self):
        self.deadline_retry("stage")

    def test_deadline_final_sync_retry(self):
        self.deadline_retry("final")

    def seed_manual_review(self, conflict=True):
        original = self.read()["currentVersionId"]
        saved = self.client.post(f"/api/sessions/{self.session_id}/versions", json={
            "rows": fixtures.EDITED_ROWS, "baseVersionId": original, "idempotencyKey": "review-save",
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        version_id = saved.json()["currentVersionId"]
        disagreement = {
            "status": "active", "subject": "human_edit", "userPosition": "Keep this route.",
            "aiPosition": "Consider recovery space.", "coreDisagreement": "Which trade-off matters?",
            "nextQuestion": "Would you like to discuss the route?", "resolution": None, "displayCard": True,
        } if conflict else None
        with repository.connect(immediate=True) as database:
            session = repository.get_session(database, self.session_id)
            opening_execution = fixtures.LLMExecutionResult("I observe the new Stage.", 1, "review-opening", guidance={"uiCues": []})
            review_execution = fixtures.LLMExecutionResult("I compare this edit with the earlier Stage.", 1, "review-comparison", guidance={"disagreement": disagreement, "uiCues": []})
            opening = backend.insert_turn(database, session, "assistant", opening_execution.assistant_message, version_id, "review-opening", opening_execution)
            review = backend.insert_turn(database, session, "assistant", review_execution.assistant_message, version_id, "review-comparison", review_execution)
            backend.record_event(database, self.session_id, "human_edit_reviewed", {
                "versionId": version_id, "openingTurnId": opening, "reviewTurnId": review,
                "changeSummary": {"changedCells": 2}, "disagreement": disagreement,
            }, backend.utc_now())
            if disagreement:
                context = repository.load_design_context(database, self.session_id, version_id)
                context["activeDisagreement"] = disagreement
                repository.save_design_context(database, version_id, context)
                backend._record_disagreement_event(database, self.session_id, version_id, review, {"disagreement": disagreement})
        backend.synchronize_manual_edit_review_nodes(self.session_id, version_id)
        return version_id, disagreement

    def test_manual_review_without_conflict_has_two_messages_and_no_challenge(self):
        self.seed_manual_review(False)
        review = self.latest("manual_edit_review")
        self.assertEqual(sum(entry["kind"] == "llm_message" for entry in review["nodeEntries"]), 2)
        self.assertFalse(any(event.get("nodeType") == "llm_challenge" for event in self.events))

    def test_manual_challenge_ack_keeps_dialogue_and_no_fictitious_agreement(self):
        version_id, disagreement = self.seed_manual_review()
        card_id = self.latest("llm_challenge")["nodeId"]
        for index, (text, status) in enumerate([
            ("I do not want to discuss that yet.", "active"),
            ("Let's discuss it and I can modify it afterward.", "acknowledged"),
        ]):
            execution = fixtures.LLMExecutionResult("I respond to your explanation.", 1, f"manual-followup-{index}",
                guidance={"disagreement": {**disagreement, "status": status}, "uiCues": []})
            with patch.object(backend, "generate_chat_reply", return_value=execution):
                response = self.client.post(f"/api/sessions/{self.session_id}/messages", json={
                    "content": text, "baseVersionId": version_id, "idempotencyKey": f"manual-followup-{index}",
                })
            self.assertEqual(response.status_code, 200, response.text)
        node = self.latest("llm_challenge")
        self.assertEqual(node["nodeId"], card_id)
        self.assertEqual(node["nodeStatus"], "acknowledged")
        self.assertEqual(sum(entry["kind"] == "card" for entry in node["nodeEntries"]), 1)
        self.assertEqual(sum(entry["kind"] == "player_message" for entry in node["nodeEntries"]), 2)
        self.assertFalse(any(event.get("nodeType") == "discussion" for event in self.events))
        with repository.connect() as database:
            context = repository.load_design_context(database, self.session_id, version_id)
        self.assertIsNone(context["activeDisagreement"])
        self.assertEqual(context["confirmedDecisions"], [])
        before = json.loads(json.dumps(node))
        execution = fixtures.LLMExecutionResult("A later ordinary discussion.", 1, "after-ack", guidance={"uiCues": []})
        with patch.object(backend, "generate_chat_reply", return_value=execution):
            response = self.client.post(f"/api/sessions/{self.session_id}/messages", json={
                "content": "What do you think now?", "baseVersionId": version_id, "idempotencyKey": "after-ack",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.latest("llm_challenge"), before)
        self.assertEqual(self.latest("discussion")["nodeType"], "discussion")

    def test_manual_reedit_updates_parent_challenge_without_changing_parent_memory(self):
        version_id, disagreement = self.seed_manual_review()
        saved = self.client.post(f"/api/sessions/{self.session_id}/versions", json={
            "rows": fixtures.SAMPLE_ROWS, "baseVersionId": version_id, "idempotencyKey": "review-reedit",
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        node = self.latest("llm_challenge")
        self.assertEqual(node["nodeStatus"], "manual_reedit")
        self.assertIn("Stage 3", node["nodeEntries"][-1]["text"])
        with repository.connect() as database:
            parent = repository.load_design_context(database, self.session_id, version_id)
            child = repository.load_design_context(database, self.session_id, saved.json()["currentVersionId"])
        self.assertEqual(parent["activeDisagreement"], disagreement)
        self.assertIsNone(child["activeDisagreement"])

    def test_manual_reply_failure_retains_user_and_retries_without_duplicates(self):
        version_id, disagreement = self.seed_manual_review()
        request = {"content": "Let's discuss this route.", "baseVersionId": version_id, "idempotencyKey": "manual-reply-retry"}
        failure = fixtures.LLMServiceError("MODEL_RESPONSE_INVALID", "Retry this message.", "manual-reply-retry", True, 3, 502)
        with patch.object(backend, "generate_chat_reply", side_effect=failure):
            for _ in range(2):
                response = self.client.post(f"/api/sessions/{self.session_id}/messages", json=request)
                self.assertEqual(response.status_code, 502, response.text)
        node = self.latest("llm_challenge")
        self.assertEqual(node["nodeStatus"], "in_progress")
        self.assertEqual(sum(entry["text"] == request["content"] for entry in node["nodeEntries"]), 1)
        self.assertFalse(any(entry["kind"] == "llm_message" for entry in node["nodeEntries"]))
        execution = fixtures.LLMExecutionResult("Let's inspect it together.", 1, "manual-reply-retry",
            guidance={"disagreement": {**disagreement, "status": "acknowledged"}, "uiCues": []})
        with patch.object(backend, "generate_chat_reply", return_value=execution) as model:
            for _ in range(2):
                response = self.client.post(f"/api/sessions/{self.session_id}/messages", json=request)
                self.assertEqual(response.status_code, 200, response.text)
        model.assert_called_once()
        node = self.latest("llm_challenge")
        self.assertEqual(node["nodeStatus"], "acknowledged")
        self.assertEqual(sum(entry["kind"] == "player_message" for entry in node["nodeEntries"]), 1)
        self.assertFalse(any(entry["label"] == "Reply pending retry" for entry in node["nodeEntries"]))

    def accept_pending(self):
        state = self.read()
        proposal = next(item for item in state["proposals"] if item["status"] == "pending")
        with patch.object(backend, "rewrite_intent_progress", return_value={"summaryText": "Accepted map.", "detailedText": None, "model": "mock"}):
            response = self.client.post(f"/api/sessions/{self.session_id}/proposals/{proposal['proposalId']}/decision", json={
                "decision": "accept", "baseVersionId": state["currentVersionId"], "idempotencyKey": "projection-accept",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()["currentVersionId"], state["currentVersionId"])

    def test_player_no_keeps_reason_choice_card_and_separate_proposal(self):
        self.fixture.test_challenge_reason_keeps_or_resolves_structured_disagreement()
        challenge = self.latest("player_challenge")
        self.assertEqual(challenge["nodeStatus"], "replacement_selected")
        self.assertTrue(challenge["nodeParentId"])
        self.assertEqual(sum(entry["kind"] == "card" for entry in challenge["nodeEntries"]), 1)
        text = "\n".join(entry["text"] for entry in challenge["nodeEntries"])
        self.assertIn("player should commit early", text)
        self.assertIn("Keep the original proposal?", text)
        self.assertTrue(any(entry["text"] == "否" for entry in challenge["nodeEntries"]))
        proposal = self.latest("proposal")
        self.assertEqual(proposal["nodeParentId"], challenge["nodeId"])
        self.assertEqual(proposal["nodeStatus"], "pending")
        self.accept_pending()
        self.assertEqual(self.latest("proposal")["nodeStatus"], "accepted")
        self.assertEqual(self.latest("player_challenge"), challenge)

    def test_player_yes_acceptance_does_not_overwrite_challenge_selection(self):
        self.fixture.test_choice_pending_yes_revalidates_challenged_exact_tiles()
        challenge = self.latest("player_challenge")
        self.assertEqual(challenge["nodeStatus"], "original_selected")
        self.assertTrue(challenge["nodeParentId"])
        proposal = self.latest("proposal")
        self.assertEqual(proposal["nodeParentId"], challenge["nodeId"])
        self.assertEqual(proposal["nodeStatus"], "pending")
        self.accept_pending()
        self.assertEqual(self.latest("proposal")["nodeStatus"], "accepted")
        self.assertEqual(self.latest("player_challenge"), challenge)

    def test_old_choice_retry_after_acceptance_republishes_current_proposal(self):
        original = self.read()["currentVersionId"]
        self.fixture.test_choice_pending_yes_revalidates_challenged_exact_tiles()
        challenge = self.latest("player_challenge")
        challenge_id = challenge["nodeId"].removeprefix("player-challenge:")
        self.accept_pending()
        accepted = self.latest("proposal")
        response = self.client.post(f"/api/sessions/{self.session_id}/messages", json={
            "content": "是", "baseVersionId": original, "idempotencyKey": "choice-exact-yes",
            "action": "continue_challenge", "challengeId": challenge_id, "challengeChoice": "ai",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.latest("proposal"), accepted)
        self.assertEqual(self.latest("player_challenge"), challenge)

    def test_rejecting_candidate_is_a_proposal_decision_not_challenge_choice(self):
        self.fixture.test_choice_pending_yes_revalidates_challenged_exact_tiles()
        before = self.read()
        proposal = next(item for item in before["proposals"] if item["status"] == "pending")
        challenge = self.latest("player_challenge")
        response = self.client.post(f"/api/sessions/{self.session_id}/proposals/{proposal['proposalId']}/decision", json={
            "decision": "reject", "reason": "I want to inspect another direction.",
            "baseVersionId": before["currentVersionId"], "idempotencyKey": "projection-reject",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["currentVersionId"], before["currentVersionId"])
        self.assertEqual(self.latest("proposal")["nodeStatus"], "rejected")
        self.assertEqual(self.latest("player_challenge"), challenge)

    def test_player_failed_review_keeps_one_user_reason_without_fake_ai_reply(self):
        version_id = self.read()["currentVersionId"]
        source = self.fixture.offer_bound_revision(fixtures.PLAYER_MOVE_BRIEF, summary="Move player left", message_key="failed-source")
        execution = fixtures.LLMExecutionResult("Initial reply.", 1, "failed-start", guidance={"uiCues": []})
        with patch.object(backend, "generate_chat_reply", return_value=execution):
            started = self.client.post(f"/api/sessions/{self.session_id}/messages", json={
                "content": "Challenge this plan.", "baseVersionId": version_id, "idempotencyKey": "failed-start",
                "action": "challenge_revision", "sourceTurnId": source["turnId"],
            })
        self.assertEqual(started.status_code, 200, started.text)
        failure = fixtures.LLMServiceError("MODEL_RESPONSE_INVALID", "Retry the reason.", "failed-reason", True, 2, 502)
        request = {"content": "The change is too small.", "baseVersionId": version_id, "idempotencyKey": "failed-reason"}
        with patch.object(backend, "classify_challenge_reason", side_effect=failure):
            for _ in range(2):
                response = self.client.post(f"/api/sessions/{self.session_id}/messages", json=request)
                self.assertEqual(response.status_code, 502, response.text)
        node = self.latest("player_challenge")
        self.assertEqual(node["nodeStatus"], "review_pending")
        self.assertEqual(sum(entry["text"] == request["content"] for entry in node["nodeEntries"]), 1)
        self.assertEqual(sum(entry["kind"] == "llm_message" for entry in node["nodeEntries"]), 1)
        self.assertEqual(sum(entry["label"] == "Pending request" for entry in node["nodeEntries"]), 1)

    def test_player_no_failed_generation_keeps_choice_pending(self):
        self.fixture.test_choice_pending_no_without_new_verified_card_keeps_map()
        self.assertEqual(self.latest("player_challenge")["nodeStatus"], "awaiting_player_choice")
        self.assertFalse(any((event.get("nodeParentId") or "").startswith("player-challenge:") for event in self.events if event.get("nodeType") == "proposal"))

    def test_intent_revision_projects_original_input_and_reviewed_statement(self):
        _, card, _ = self.fixture.create_intent_card("I think you want a direct route.", "intent-source")
        guidance = card["guidance"]
        state = guidance["intentState"]
        request = {"action": "revise", "candidateText": "I want a route with recovery space.",
                   "baseVersionId": self.read()["currentVersionId"], "sourceTurnId": card["turnId"],
                   "idempotencyKey": "research-intent-revise"}
        with patch.object(backend, "review_intent_feedback", return_value={
            "verdict": "compatible", "explanation": "This is your revised direction.",
            "displayStatement": "A route with recovery space.", "semanticClaims": [],
        }):
            response = self.client.post(f"/api/sessions/{self.session_id}/intent-hypotheses/{state['hypothesisId']}/feedback", json=request)
        self.assertEqual(response.status_code, 200, response.text)
        node = self.latest("intent")
        self.assertEqual(node["nodeStatus"], "revised")
        text = "\n".join(entry["text"] for entry in node["nodeEntries"])
        self.assertIn("direct route", text)
        self.assertIn(request["candidateText"], text)
        self.assertIn("recovery space", text)
        original = json.loads(json.dumps(node))
        backend.synchronize_dashboard_node_for_turn(self.session_id, "intent-source")
        backend.synchronize_dashboard_intent_node(self.session_id, state["hypothesisId"])
        self.assertEqual(self.latest("intent"), original)

    def test_pending_intent_revision_is_not_confirmed(self):
        _, card, _ = self.fixture.create_intent_card("I think you want a direct route.", "pending-intent-source")
        state = card["guidance"]["intentState"]
        request = {"action": "revise", "candidateText": "I might want a different route feel.",
                   "baseVersionId": self.read()["currentVersionId"], "sourceTurnId": card["turnId"],
                   "idempotencyKey": "pending-intent-revise"}
        with patch.object(backend, "review_intent_feedback", return_value={
            "verdict": "unclear", "explanation": "Please clarify the route effect.", "semanticClaims": [],
        }):
            response = self.client.post(f"/api/sessions/{self.session_id}/intent-hypotheses/{state['hypothesisId']}/feedback", json=request)
        self.assertEqual(response.status_code, 200, response.text)
        node = self.latest("intent")
        self.assertEqual(node["nodeStatus"], "awaiting_player_choice")
        self.assertTrue(any(entry["text"] == request["candidateText"] for entry in node["nodeEntries"]))
        self.assertFalse(any(entry["text"] == "Confirmed tentative intention" for entry in node["nodeEntries"]))
