"""Focused regression tests for backend audit pipeline, challenges, Policy 2, search, and requirements."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
    import cocreation  # noqa: F401
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from cocreation.api import binary_routes, context_and_discovery, message_response_policy, proposal_and_challenge
from cocreation.domain import binary_discovery as binary
from cocreation.domain.design_requirements import (
    evaluate_requirements,
    preflight_requirements,
    requirement_model_view,
    validate_requirement_record,
)
from cocreation.domain.level_validation import build_stage_snapshot, validate_and_solve
from cocreation.domain.proposal_search import (
    ProposalSearchExhausted,
    RevisionPlanError,
    _connected_outer_shell,
    parse_revision_plan,
    search_revision_plan,
    validate_revision_plan_against_map,
)
from cocreation.domain.proposal_understanding import (
    CONTEXT_VERSION,
    MEANING_REVIEW_VERSION,
    POLICY_VERSION,
    reusable_understanding,
    topic_signature,
)
from cocreation.llm import facade as llm
from cocreation.llm.facade import LLMExecutionResult, LLMServiceError
from cocreation.persistence import repository
from cocreation.reliability import component_recovery, reliability_audit
from tests.fakes import FakeClock, RecordedCall, SessionTestCase
import test_binary_discovery as binary_fixtures
import test_sessions as legacy


class DomainAuditPipelineTests(unittest.TestCase):
    """Domain-level regression tests for search, requirements, policy, recovery, understanding, and audit."""

    def test_water_to_wall_respects_strategy_preserve_walls(self):
        rows = list(legacy.SAMPLE_ROWS)
        rows[2] = "#.@........#"
        plan = parse_revision_plan({
            "strategies": [{
                "effect": "reshape_water",
                "focus": {"row": 3, "column": 3, "radius": 1},
                "operators": ["remove_water", "add_wall"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
                "editBudget": 2,
                "metricGoals": [],
            }]
        })
        baseline = validate_and_solve(rows).as_dict()
        result = search_revision_plan(rows, plan, validate_and_solve, baseline_metrics=baseline)
        self.assertNotIn("add_wall", result.operators)
        self.assertFalse(any(
            before == "@" and after == "#"
            for before_row, after_row in zip(rows, result.rows)
            for before, after in zip(before_row, after_row)
        ))

    def test_composite_water_wall_transitions_supported_and_checked_against_preserve(self):
        rows = list(legacy.SAMPLE_ROWS)
        rows[2] = "#.@#.......#"
        water_to_wall_plan = parse_revision_plan({
            "strategies": [{
                "effect": "reshape_water",
                "focus": None,
                "operators": ["remove_water", "add_wall"],
                "preserve": ["outer_shell", "player", "boxes", "targets"],
                "requiredTransitions": [{"row": 3, "column": 3, "from": "@", "to": "#"}],
                "editBudget": 2,
                "metricGoals": [],
            }]
        })
        self.assertTrue(validate_revision_plan_against_map(rows, water_to_wall_plan))

        wall_to_water_plan = parse_revision_plan({
            "strategies": [{
                "effect": "reshape_water",
                "focus": None,
                "operators": ["remove_wall", "add_water"],
                "preserve": ["outer_shell", "player", "boxes", "targets"],
                "requiredTransitions": [{"row": 3, "column": 4, "from": "#", "to": "@"}],
                "editBudget": 2,
                "metricGoals": [],
            }]
        })
        self.assertTrue(validate_revision_plan_against_map(rows, wall_to_water_plan))

        blocked_plan = parse_revision_plan({
            "strategies": [{
                "effect": "open_route",
                "focus": None,
                "operators": ["remove_wall", "add_water"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "water"],
                "requiredTransitions": [{"row": 3, "column": 4, "from": "#", "to": "@"}],
                "editBudget": 2,
                "metricGoals": [],
            }]
        })
        with self.assertRaises(RevisionPlanError):
            validate_revision_plan_against_map(rows, blocked_plan)

    def test_search_catches_timeout_in_metric_evaluation_and_breaks_cleanly(self):
        rows = list(legacy.SAMPLE_ROWS)
        rows[2] = "#.#........#"
        plan = parse_revision_plan({
            "strategies": [{
                "effect": "open_route",
                "focus": None,
                "operators": ["remove_wall"],
                "preserve": ["outer_shell", "player", "boxes", "targets"],
                "editBudget": 2,
                "metricGoals": [{"metric": "minimumPushes", "direction": "preserve"}],
            }]
        })
        clock = FakeClock()

        def expiring_validator(candidate_rows):
            clock.advance(10.0)
            return validate_and_solve(candidate_rows)

        with self.assertRaises(ProposalSearchExhausted) as caught:
            search_revision_plan(
                rows,
                plan,
                expiring_validator,
                baseline_metrics={"minimumPushes": 1},
                deadline=clock.monotonic() + 5.0,
                clock=clock,
            )
        diagnostics = caught.exception.diagnostics
        self.assertTrue(diagnostics["deadlineReached"])
        self.assertGreaterEqual(diagnostics["failureReasons"].get("TimeoutError", 0), 1)

    def test_connected_outer_shell_seeds_void_adjacent_walls_and_preflight_blocks_them(self):
        rows = [
            "  ########  ",
            "  #......#  ",
            "###..p...###",
            "#....s.t...#",
            "#..........#",
            "#..........#",
            "#..........#",
            "#..........#",
            "#..........#",
            "############",
        ]
        shell = _connected_outer_shell(rows)
        # (2, 1) is row 2, column 3 (`#` adjacent to exterior `' '` void at (1, 1)), interior-reachable border.
        self.assertIn((2, 1), shell)
        # (4, 1) is floor `.`, not part of shell.
        self.assertNotIn((4, 1), shell)
        sources = [{"id": "u1", "content": "把(2,3)改成水"}]
        snapshot = build_stage_snapshot(rows, version_id="v1")
        record = validate_requirement_record({
            "requirements": [],
            "automaticBindings": [],
            "exactTransitions": [{
                "row": 2, "column": 3, "from": "#", "to": "@",
                "sourceTurnId": "u1", "evidenceSpan": "把(2,3)改成水",
            }],
        }, sources, snapshot)
        issues = preflight_requirements(rows, record)
        self.assertTrue(any(item["reason"] == "exact_transition_conflicts_with_snapshot" for item in issues))

    def test_evaluate_requirements_absolute_targets_and_change_scope_cap(self):
        rows = list(legacy.SAMPLE_ROWS)
        snapshot = build_stage_snapshot(rows, version_id="v1")
        sources = [{"id": "u1", "content": "最多改2格，水域保持不超过3格"}]
        record = validate_requirement_record({
            "requirements": [
                {
                    "component": "wall",
                    "property": "change_scope",
                    "relation": "decrease",
                    "entities": [],
                    "value": 2,
                    "unit": "absolute",
                    "strength": "invariant",
                    "focused": False,
                    "statement": "Change at most 2 cells",
                    "sourceTurnId": "u1",
                    "evidenceSpan": "最多改2格",
                    "priorityEvidenceSpan": "",
                },
                {
                    "component": "water",
                    "property": "count",
                    "relation": "decrease",
                    "entities": [],
                    "value": 3,
                    "unit": "absolute",
                    "strength": "goal",
                    "focused": False,
                    "statement": "Keep water count at most 3",
                    "sourceTurnId": "u1",
                    "evidenceSpan": "不超过3格",
                    "priorityEvidenceSpan": "",
                },
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }, sources, snapshot)
        within_cap = copy.deepcopy(rows)
        within_cap[2] = "#.@........#"  # 1 changed cell, 1 water <= 3
        outcomes = evaluate_requirements(rows, within_cap, record)
        self.assertTrue(all(item["passed"] and item["status"] == "fulfilled" for item in outcomes), outcomes)

        over_cap = copy.deepcopy(rows)
        over_cap[2] = "#.@@@@.....#"  # 4 changed cells (> 2) and 4 water (> 3)
        over_outcomes = evaluate_requirements(rows, over_cap, record)
        self.assertFalse(any(item["passed"] for item in over_outcomes), over_outcomes)

    def test_requirement_model_view_preserves_merged_source_turn_ids(self):
        rows = list(legacy.SAMPLE_ROWS)
        snapshot = build_stage_snapshot(rows, version_id="v1")
        sources = [{"id": "u1", "content": "减少水域"}, {"id": "u2", "content": "少放一点水"}]
        record = validate_requirement_record({
            "requirements": [
                {
                    "component": "water", "property": "count", "relation": "decrease",
                    "entities": [], "value": None, "unit": "none", "strength": "goal",
                    "focused": False, "statement": "Reduce water",
                    "sourceTurnId": "u1", "evidenceSpan": "减少水域", "priorityEvidenceSpan": "",
                },
                {
                    "component": "water", "property": "count", "relation": "decrease",
                    "entities": [], "value": None, "unit": "none", "strength": "goal",
                    "focused": True, "statement": "Reduce water",
                    "sourceTurnId": "u2", "evidenceSpan": "少放一点水", "priorityEvidenceSpan": "",
                },
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }, sources, snapshot)
        self.assertEqual(record["requirements"][0]["sourceTurnIds"], ["u1", "u2"])
        projected = requirement_model_view(record)
        replayed = validate_requirement_record(projected, sources, snapshot)
        self.assertEqual(replayed, record)

    def test_deterministic_intent_conflict_skips_other_and_formats_english_subject(self):
        generic_inc = [{
            "subjectType": "other", "attributeType": "other", "direction": "increase",
            "semanticSource": "kimi_reviewed", "evidenceSpan": "something",
        }]
        generic_dec = [{
            "subjectType": "other", "attributeType": "other", "direction": "decrease",
            "semanticSource": "kimi_reviewed", "evidenceSpan": "another",
        }]
        self.assertIsNone(context_and_discovery._deterministic_intent_conflict(generic_inc, generic_dec))
        pair = {
            "new": {"subjectType": "water", "attributeType": "coverage", "direction": "increase"},
            "existing": {"subjectType": "water", "attributeType": "coverage", "direction": "decrease"},
        }
        explanation = context_and_discovery._deterministic_conflict_explanation(pair, "en")
        self.assertIn("increase water coverage", explanation)
        self.assertIn("reduce water coverage", explanation)

    def test_reusable_understanding_checks_signature_context_and_review_version(self):
        snapshot = build_stage_snapshot(legacy.SAMPLE_ROWS, version_id="v1")
        context = {"topicId": "t1", "versionId": "v1", "initialRequest": "Reduce water"}
        expected_sig = topic_signature(snapshot, context, "Reduce water")
        valid = {
            "semanticPolicyVersion": POLICY_VERSION,
            "dialogueContextVersion": CONTEXT_VERSION,
            "versionId": "v1",
            "evidenceSignature": expected_sig,
            "acts": ["revision_request"],
            "elements": ["water"],
            "evidenceSpan": "Reduce water",
            "directionSufficient": False,
            "mapRelated": True,
            "changes": [],
            "criticalReview": {"verified": True, "policyVersion": MEANING_REVIEW_VERSION},
        }
        self.assertTrue(reusable_understanding(valid, "v1", expected_signature=expected_sig, context=context))
        self.assertFalse(reusable_understanding(valid, "v1", expected_signature="b" * 64, context=context))
        stale_review = {**valid, "criticalReview": {"verified": True, "policyVersion": "stale-review-version"}}
        self.assertFalse(reusable_understanding(stale_review, "v1", expected_signature=expected_sig, context=context))
        wrong_topic = {
            **valid,
            "proposalUnderstanding": {
                "aspect": "gameplay",
                "editScope": [{"component": "water", "sourceTurnId": "u1", "evidenceSpan": "x", "statement": "s"}],
                "focus": [],
                "goals": [{"sourceTurnId": "u1", "evidenceSpan": "x", "statement": "s"}],
                "preserve": [],
                "sufficient": True,
                "nextQuestionDimension": "none",
            },
        }
        wrong_topic.pop("criticalReview", None)
        self.assertFalse(
            reusable_understanding(wrong_topic, "v1", expected_signature=expected_sig, context={**context, "topicId": "t1"})
        )

    def test_conversation_only_policy_preserves_state_transition_deferred(self):
        execution = LLMExecutionResult(
            "Safe response.",
            1,
            "req-1",
            model="mock",
            guidance={"move": "offer_perspective", "_stateTransitionDeferred": True, "discussionCardMode": "disagreement_only"},
        )
        result = message_response_policy.apply_conversation_only_policy(execution, active_disagreement=None)
        self.assertTrue(result.guidance.get("_stateTransitionDeferred"))
        self.assertNotIn("discussionCardMode", result.guidance)

    def test_reliability_audit_maps_proposal_clarification_to_message(self):
        self.assertEqual(
            reliability_audit._task({"eventType": "reply_delivery_outcome", "payload": {"task": "proposal_clarification"}}),
            "message",
        )


class ChallengeAndRecoveryHttpTests(SessionTestCase):
    """HTTP regression tests for challenge deadlocks, retain_current, ai workflow, recovery, and deadlines."""

    create_and_open_session = legacy.CoCreationSessionTests.create_and_open_session
    read_session = legacy.CoCreationSessionTests.read_session
    _fake_turn_understanding = staticmethod(legacy.CoCreationSessionTests._fake_turn_understanding)

    def setUp(self):
        self.clock = FakeClock()
        self.open_workspace(
            clock=self.clock,
            settings_overrides={"binary_discovery_enabled": False},
            llm_handlers={"classify_turn_understanding": self._fake_turn_understanding},
        )
        self.session_id, self.integration_token = self.create_and_open_session()
        self.version_id = self.read_session()["currentVersionId"]

    def _seed_source_card_and_challenge(self, *, execution_brief=None):
        offer_execution = LLMExecutionResult(
            "Here is one reversible proposal.",
            1,
            "offer-001",
            model="mock-model",
            guidance={
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Move player start one cell left",
                    "rationale": "Changes the opening distance cleanly.",
                    "executionBrief": execution_brief or legacy.PLAYER_MOVE_BRIEF,
                    "revisionWorkflow": {
                        "revisionWorkflowId": "wf-challenge-001",
                        "mode": "shadow",
                        "objective": "Give the opening move more room.",
                        "requirementPolicyVersion": 1,
                        "semanticConstraints": [],
                        "softObjectives": [],
                        "requirementRecord": {
                            "policyVersion": 1,
                            "requirements": [{
                                "requirementId": "req-wf-001",
                                "component": "walls",
                                "property": "count",
                                "relation": "preserve",
                                "entities": [],
                                "value": None,
                                "unit": "none",
                                "strength": "invariant",
                                "focused": False,
                                "statement": "Preserve existing wall count",
                                "sourceTurnId": "u-src-1",
                                "evidenceSpan": "keep walls",
                                "priorityEvidenceSpan": "",
                            }],
                            "automaticBindings": [],
                            "exactTransitions": [],
                        },
                    },
                },
                "uiCues": [],
            },
        )
        with self.services.connect(immediate=True) as db:
            session = db.execute("SELECT * FROM design_sessions WHERE id = ?", (self.session_id,)).fetchone()
            self.services.insert_turn(db, session, "user", "Adjust the opening start.", self.version_id, "seed-offer-user", None)
            source_turn_id = self.services.insert_turn(
                db, session, "assistant", "Here is one reversible proposal.", self.version_id, "seed-offer", offer_execution
            )

        challenge_resp = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={
                "content": "I want to challenge this proposal.",
                "baseVersionId": self.version_id,
                "idempotencyKey": "start-challenge",
                "action": "challenge_revision",
                "sourceTurnId": source_turn_id,
            },
        )
        self.assertEqual(challenge_resp.status_code, 200, challenge_resp.text)
        challenge_id = challenge_resp.json()["turns"][-1]["guidance"]["challengeState"]["challengeId"]
        return source_turn_id, challenge_id

    def test_unclear_challenge_reason_resets_reviewing_status_and_allows_retry_and_follow_up(self):
        _, challenge_id = self._seed_source_card_and_challenge()
        unclear_classification = {
            "relation": "unclear",
            "merit": "unclear",
            "comparison": "Please clarify which gameplay consequence concerns you.",
            "attemptsUsed": 1,
        }
        unclear_reply = LLMExecutionResult(
            "Could you clarify which gameplay consequence concerns you?",
            1,
            "unclear-reply",
            model="mock-model",
            guidance={"move": "offer_perspective", "proposalOffer": None, "disagreement": None, "uiCues": []},
        )
        with self.using_dependencies(llm_handlers={
            "classify_challenge_reason": unclear_classification,
            "generate_chat_reply": unclear_reply,
        }):
            first = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Not sure.",
                    "baseVersionId": self.version_id,
                    "idempotencyKey": "unclear-reason-1",
                    "action": "continue_challenge",
                    "challengeId": challenge_id,
                },
            )
        self.assertEqual(first.status_code, 200, first.text)
        state = first.json()["turns"][-1]["guidance"]["challengeState"]
        self.assertEqual(state["status"], "awaiting_reason")
        self.assertEqual(state["challengeId"], challenge_id)
        with self.services.connect() as db:
            row = db.execute(
                "SELECT status FROM revision_challenges WHERE session_id = ? AND challenge_id = ?",
                (self.session_id, challenge_id),
            ).fetchone()
            self.assertEqual(row["status"], "awaiting_reason")
            ctx = context_and_discovery.build_llm_context(
                db, self.session_id, repository.get_version(db, self.session_id, self.version_id), services=self.services
            )
            self.assertEqual((ctx["stageContext"].get("challengeContext") or {}).get("challengeId"), challenge_id)

        # Follow-up concrete reason succeeds (did not stay deadlocked in reviewing).
        reason_accepted = {
            "relation": "different",
            "merit": "reasonable",
            "comparison": "Your concern focuses on early box pacing rather than corridor width.",
            "attemptsUsed": 1,
        }
        choice_reply = LLMExecutionResult(
            "Understood, let's decide which direction to use.",
            1,
            "reason-accepted",
            model="mock-model",
            guidance={"move": "offer_perspective", "uiCues": []},
        )
        with self.using_dependencies(llm_handlers={
            "classify_challenge_reason": reason_accepted,
            "generate_chat_reply": choice_reply,
        }):
            follow_up = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Changing the start breaks the initial box push pacing.",
                    "baseVersionId": self.version_id,
                    "idempotencyKey": "clear-reason-2",
                },
            )
        self.assertEqual(follow_up.status_code, 200, follow_up.text)
        self.assertEqual(follow_up.json()["turns"][-1]["guidance"]["challengeState"]["status"], "choice_pending")

        # Same-key retry after unclear works cleanly via natural action routing.
        with self.using_dependencies(llm_handlers={"classify_challenge_reason": RecordedCall(side_effect=AssertionError("no replay llm"))}):
            retry_unclear = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Not sure.",
                    "baseVersionId": self.version_id,
                    "idempotencyKey": "unclear-reason-1",
                },
            )
        self.assertEqual(retry_unclear.status_code, 200, retry_unclear.text)

        # Active challenge + action='none' + requestProposal=True -> 409 DISAGREEMENT_ACTIVE.
        conflict = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={
                "content": "Give me another proposal instead.",
                "baseVersionId": self.version_id,
                "idempotencyKey": "req-prop-during-challenge",
                "requestProposal": True,
            },
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["code"], "DISAGREEMENT_ACTIVE")

    def test_stale_challenge_and_exception_cleanup_and_125s_review_expiry(self):
        # 1. Missing active challenge on continue_challenge returns 409 without inserting orphaned user turn.
        stale = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={
                "content": "Continue missing challenge.",
                "baseVersionId": self.version_id,
                "idempotencyKey": "stale-challenge-key",
                "action": "continue_challenge",
                "challengeId": "missing-challenge-id",
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "CHALLENGE_STALE")
        with self.services.connect() as db:
            orphans = db.execute(
                "SELECT 1 FROM conversation_turns WHERE session_id = ? AND request_id = ?",
                (self.session_id, "stale-challenge-key"),
            ).fetchone()
            self.assertIsNone(orphans)

        # 2. Unhandled exception inside classify_challenge_reason cleans up 'reviewing' rows back to 'review_pending'.
        _, challenge_id = self._seed_source_card_and_challenge()
        from tests.fakes import create_test_client
        with self.using_dependencies(llm_handlers={"classify_challenge_reason": RecordedCall(side_effect=RuntimeError("boom"))}):
            no_raise_client = create_test_client(self.services, raise_server_exceptions=False)
            no_raise_client.cookies = self.client.cookies
            crashed = no_raise_client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "My reason triggers crash.",
                    "baseVersionId": self.version_id,
                    "idempotencyKey": "reason-crash-1",
                    "action": "continue_challenge",
                    "challengeId": challenge_id,
                },
            )
            self.assertEqual(crashed.status_code, 500)
        with self.services.connect() as db:
            rev = db.execute(
                "SELECT status FROM challenge_reason_reviews WHERE session_id = ? AND challenge_id = ?",
                (self.session_id, challenge_id),
            ).fetchone()
            chal = db.execute(
                "SELECT status FROM revision_challenges WHERE session_id = ? AND challenge_id = ?",
                (self.session_id, challenge_id),
            ).fetchone()
            self.assertEqual(rev["status"], "review_pending")
            self.assertEqual(chal["status"], "review_pending")

            # Force stale 'reviewing' >125s and verify routing expires it cleanly back to 'review_pending'.
            stale_ts = "2026-10-01T11:57:00Z"  # > 125s before 12:00:00Z
            db.execute(
                "UPDATE challenge_reason_reviews SET status = 'reviewing', updated_at = ? WHERE session_id = ?",
                (stale_ts, self.session_id),
            )
            db.execute(
                "UPDATE revision_challenges SET status = 'reviewing', updated_at = ? WHERE session_id = ?",
                (stale_ts, self.session_id),
            )
            db.commit()

        with self.using_dependencies(llm_handlers={
            "classify_challenge_reason": {"relation": "different", "merit": "reasonable", "comparison": "Clear difference.", "attemptsUsed": 1},
            "generate_chat_reply": LLMExecutionResult("Let's choose.", 1, "expired-ok", model="mock", guidance={"move": "offer_perspective", "uiCues": []}),
        }):
            recovered = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "A completely new follow-up reason.",
                    "baseVersionId": self.version_id,
                    "idempotencyKey": "after-125s-expiry",
                    "action": "continue_challenge",
                    "challengeId": challenge_id,
                },
            )
        self.assertEqual(recovered.status_code, 200, recovered.text)

    def test_retain_current_challenge_resolution_and_validation(self):
        self.assertEqual(proposal_and_challenge._challenge_choice("保留现状", "zh-CN"), "retain_current")
        self.assertEqual(proposal_and_challenge._challenge_choice("Keep current", "en"), "retain_current")
        proposal_and_challenge._validate_message_action_payload(
            SimpleNamespace(
                action="continue_challenge",
                sourceTurnId=None,
                requestProposal=False,
                exitChallenge=False,
                challengeChoice="retain_current",
                content="保留现状",
                challengeId="chal-1",
            )
        )
        _, challenge_id = self._seed_source_card_and_challenge()
        with self.using_dependencies(llm_handlers={
            "classify_challenge_reason": {"relation": "different", "merit": "reasonable", "comparison": "Both are valid.", "attemptsUsed": 1},
            "generate_chat_reply": LLMExecutionResult("Choose how to proceed.", 1, "c1", model="mock", guidance={"move": "offer_perspective", "uiCues": []}),
        }):
            r1 = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={"content": "The opening should stay readable.", "baseVersionId": self.version_id, "idempotencyKey": "r1"},
            )
            self.assertEqual(r1.status_code, 200, r1.text)

        retain_reply = LLMExecutionResult(
            "Understood, we will keep the current Stage unchanged.",
            1,
            "retain-ok",
            model="mock",
            proposed_rows=legacy.EDITED_ROWS,  # should be stripped!
            guidance={"move": "offer_revision", "proposalOffer": {"summary": "Should be stripped"}, "uiCues": []},
        )
        with self.using_dependencies(llm_handlers={"generate_chat_reply": retain_reply}):
            r2 = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "保留现状",
                    "baseVersionId": self.version_id,
                    "idempotencyKey": "r2-retain",
                    "action": "continue_challenge",
                    "challengeId": challenge_id,
                },
            )
        self.assertEqual(r2.status_code, 200, r2.text)
        body = r2.json()
        self.assertEqual(body["proposals"], [])
        assistant_turn = body["turns"][-1]
        self.assertEqual(assistant_turn["guidance"]["challengeState"]["status"], "resolved")
        self.assertEqual(assistant_turn["guidance"]["disagreement"]["resolution"], "retain_current")
        self.assertIsNone(assistant_turn["guidance"]["proposalOffer"])

    def test_challenge_choice_ai_preserves_revision_workflow_and_confirmed_memory(self):
        water_brief = {
            "schemaVersion": 1,
            "effect": "reshape_water",
            "anchors": [],
            "focus": {"row": 3, "column": 3, "radius": 1},
            "requiredTransitions": [{"row": 3, "column": 3, "from": ".", "to": "@"}],
            "allowedOperators": ["add_water"],
            "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
            "playObjective": "route_choice",
        }
        source_turn_id, challenge_id = self._seed_source_card_and_challenge(execution_brief=water_brief)
        with self.using_dependencies(llm_handlers={
            "classify_challenge_reason": {"relation": "different", "merit": "reasonable", "comparison": "Different trade-off.", "attemptsUsed": 1},
            "generate_chat_reply": LLMExecutionResult("Pick approach.", 1, "c1", model="mock", guidance={"move": "offer_perspective", "uiCues": []}),
        }):
            r1 = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={"content": "Concerned about pace.", "baseVersionId": self.version_id, "idempotencyKey": "ai-choice-r1"},
            )
            self.assertEqual(r1.status_code, 200, r1.text)

        water_proposal_rows = list(legacy.SAMPLE_ROWS)
        water_proposal_rows[2] = "#.@........#"
        water_contract = {
            "schemaVersion": 1,
            "authorizedBrief": "Add a water cell near the north corridor.",
            "aiEditableTilesOnly": True,
            "revisionPlan": {
                "strategies": [{
                    "effect": "reshape_water",
                    "focus": None,
                    "operators": ["add_water"],
                    "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
                    "editBudget": 1,
                    "metricGoals": [],
                    "requiredTransitions": [],
                    "anchorEntities": [],
                    "playObjective": "route_choice",
                }]
            },
            "strategies": [{
                "strategyIndex": 1,
                "effect": "reshape_water",
                "focus": None,
                "allowedOperators": ["add_water"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
                "minimumChangedCells": 1,
                "maximumChangedCells": 1,
                "metricGoals": [],
                "requiredTransitions": [],
                "anchorEntities": [],
                "playObjective": "route_choice",
            }],
            "explicitlyRelaxedByDesigner": False,
        }
        ai_reply = LLMExecutionResult(
            "Executing your chosen original approach.",
            1,
            "ai-choice-exec",
            model="mock",
            proposed_rows=water_proposal_rows,
            revision_plan=water_contract["revisionPlan"],
            revision_contract=water_contract,
            revision_operations=[{"row": 3, "column": 3, "to": "@"}],
            proposal_diagnostics={"selectedStrategyIndex": 1},
            guidance={"move": "deliver_revision", "proposalOffer": None, "uiCues": []},
        )
        with self.using_dependencies(llm_handlers={"generate_chat_reply": ai_reply}):
            r2 = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "content": "Yes",
                    "baseVersionId": self.version_id,
                    "idempotencyKey": "ai-choice-yes",
                    "action": "continue_challenge",
                    "challengeId": challenge_id,
                    "challengeChoice": "ai",
                },
            )
        self.assertEqual(r2.status_code, 200, r2.text)
        proposal_id = r2.json()["proposals"][0]["proposalId"]
        proposal_turn_id = r2.json()["proposals"][0]["assistantTurnId"]
        with self.services.connect() as db:
            binding = json.loads(
                db.execute("SELECT proposal_binding_json FROM conversation_turns WHERE id = ?", (proposal_turn_id,)).fetchone()[0]
            )
            self.assertEqual(binding.get("sourceOfferTurnId"), source_turn_id)
            self.assertEqual(binding.get("revisionWorkflow", {}).get("revisionWorkflowId"), "wf-challenge-001")

        decision = self.client.post(
            f"/api/sessions/{self.session_id}/proposals/{proposal_id}/decision",
            json={"baseVersionId": self.version_id, "decision": "accept", "idempotencyKey": "accept-ai-choice"},
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        new_version_id = decision.json()["currentVersionId"]
        with self.services.connect() as db:
            child_ctx = repository.load_design_context(db, self.session_id, new_version_id)
            self.assertTrue(any(item.get("id") == "req-wf-001" for item in child_ctx.get("designConstraints", [])))

        # Post-deadline replay succeeds on _route_message_to_active_challenge and _decide_proposal_impl.
        self.clock.advance(self.services.settings.cocreation_deadline_seconds + 100.0)
        replay_msg = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={
                "content": "Yes",
                "baseVersionId": self.version_id,
                "idempotencyKey": "ai-choice-yes",
                "action": "continue_challenge",
                "challengeId": challenge_id,
                "challengeChoice": "ai",
            },
        )
        self.assertEqual(replay_msg.status_code, 200, replay_msg.text)
        replay_decision = self.client.post(
            f"/api/sessions/{self.session_id}/proposals/{proposal_id}/decision",
            json={"baseVersionId": self.version_id, "decision": "accept", "idempotencyKey": "accept-ai-choice"},
        )
        self.assertEqual(replay_decision.status_code, 200, replay_decision.text)

    def test_alternative_duplicate_rejection_and_recovery_selected_strategy_index(self):
        # 1. _ensure_alternative_revision_execution raises retryable PROPOSAL_SEARCH_EXHAUSTED on duplicate transitions.
        dup_exec = LLMExecutionResult(
            "Here is another card.",
            1,
            "alt-dup",
            model="mock",
            guidance={
                "move": "offer_revision",
                "proposalOffer": {
                    "summary": "Different wording",
                    "rationale": "Different rationale",
                    "executionBrief": legacy.PLAYER_MOVE_BRIEF,
                },
                "uiCues": [],
            },
        )
        with self.assertRaises(LLMServiceError) as caught:
            proposal_and_challenge._ensure_alternative_revision_execution(
                dup_exec,
                {"summary": "Move P left", "rationale": "Original rationale"},
                "en",
                original_binding={"executionBrief": legacy.PLAYER_MOVE_BRIEF},
            )
        self.assertEqual(caught.exception.code, "PROPOSAL_SEARCH_EXHAUSTED")
        self.assertTrue(caught.exception.retryable)

        # Missing offer replaces card-ready prose with safe clarification and removes proposalOffer.
        missing_exec = LLMExecutionResult(
            "See the card below.",
            1,
            "alt-missing",
            model="mock",
            guidance={"move": "offer_revision", "proposalOffer": None, "uiCues": []},
        )
        sanitized = proposal_and_challenge._ensure_alternative_revision_execution(
            missing_exec,
            {"summary": "Move P left", "rationale": "Original rationale"},
            "en",
            original_binding={"executionBrief": legacy.PLAYER_MOVE_BRIEF},
        )
        self.assertIsNone(sanitized.guidance["proposalOffer"])
        self.assertEqual(sanitized.guidance["move"], "clarify_intent")
        self.assertNotIn("See the card below", sanitized.assistant_message)

        # 2. _verified_recovery_suggestion_execution records selectedStrategyIndex.
        from cocreation.persistence.repository import map_fingerprint
        water_rows = list(legacy.SAMPLE_ROWS)
        water_rows[2] = "#.@.....@..#"
        candidate_rows = list(water_rows)
        candidate_rows[2] = "#.......@..#"
        water_contract = {
            "schemaVersion": 1,
            "policyVersion": 1,
            "strategies": [{
                "strategyIndex": 1,
                "effect": "open_route",
                "focus": None,
                "allowedOperators": ["remove_water"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
                "minimumChangedCells": 1,
                "maximumChangedCells": 2,
                "metricGoals": [],
            }],
            "objectivePolicy": {"hardMetricGoals": []},
        }
        discovery = {
            "failureEnvelope": {
                "recoverySuggestions": [{
                    "suggestionId": "rec-1",
                    "requiresDesignerChoice": False,
                }],
                "rejections": [{
                    "recoverySuggestionId": "rec-1",
                    "hiddenCandidate": {
                        "baseMapFingerprint": map_fingerprint(water_rows),
                        "candidateMapFingerprint": map_fingerprint(candidate_rows),
                        "revisionContract": water_contract,
                        "strategyIndex": 1,
                        "operations": [{"row": 3, "column": 3, "to": "."}],
                        "mechanismEvidence": {},
                    },
                }],
            }
        }
        replayed = proposal_and_challenge._verified_recovery_suggestion_execution(
            discovery, "suggestion 1", water_rows, "en", "req-rec", services=self.services
        )
        self.assertIsNotNone(replayed)
        self.assertEqual(replayed.proposal_diagnostics["selectedStrategyIndex"], 1)

    def test_human_edit_active_disagreement_suppresses_policy1_and_strips_card_even_without_disagreement_field(self):
        disagreement = {
            "status": "active",
            "subject": "human_edit",
            "userPosition": "Narrower opening.",
            "aiPosition": "Keep breathing room.",
            "coreDisagreement": "Width of opening.",
            "nextQuestion": "Can we discuss the trade-off?",
            "resolution": None,
            "displayCard": False,
        }
        with self.services.connect(immediate=True) as db:
            ctx = repository.load_design_context(db, self.session_id, self.version_id)
            ctx["activeDisagreement"] = disagreement
            repository.save_design_context(db, self.version_id, ctx)

        contexts = []

        def capture_reply(*args, **kwargs):
            contexts.append(kwargs["stage_context"])
            # Model ignores disagreement and emits proposalOffer + proposed_rows + disagreement=None
            return LLMExecutionResult(
                "Let's discuss this first.",
                1,
                "he-followup",
                model="mock",
                proposed_rows=legacy.EDITED_ROWS,
                revision_plan=legacy.PLAYER_MOVE_CONTRACT["revisionPlan"],
                revision_contract=legacy.PLAYER_MOVE_CONTRACT,
                revision_operations=legacy.PLAYER_MOVE_OPERATIONS,
                guidance={"move": "offer_revision", "proposalOffer": {"summary": "Forbidden", "executionBrief": legacy.PLAYER_MOVE_BRIEF}, "disagreement": None, "uiCues": []},
            )

        with self.using_dependencies(llm_handlers={"generate_chat_reply": capture_reply}):
            resp = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={"content": "Make the corridor wider instead.", "baseVersionId": self.version_id, "idempotencyKey": "he-chat-1"},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(contexts[0].get("proposalDiscovery"), {})
        self.assertEqual(contexts[0].get("revisionRouting"), "none")
        self.assertIsNone(contexts[0].get("proposalClarification"))
        self.assertEqual(resp.json()["proposals"], [])
        self.assertIsNone(resp.json()["turns"][-1]["guidance"]["proposalOffer"])

    def test_retry_component_recovery_timeout_and_applied_sync(self):
        with self.services.connect(immediate=True) as db:
            session = db.execute("SELECT * FROM design_sessions WHERE id = ?", (self.session_id,)).fetchone()
            ctx = repository.load_design_context(db, self.session_id, self.version_id)
            ctx["activeDisagreement"] = {"status": "active", "subject": "human_edit", "coreDisagreement": "Focus", "disagreementId": "d-rec"}
            repository.save_design_context(db, self.version_id, ctx)
            u1 = self.services.insert_turn(db, session, "user", "Let's discuss focus.", self.version_id, "u-rec-1", None)
            a1 = self.services.insert_turn(db, session, "assistant", "Deferred.", self.version_id, "u-rec-1", LLMExecutionResult("Deferred.", 1, "r1", model="m", guidance={"move": "offer_perspective"}))
            task = component_recovery.enqueue(db, self.session_id, self.version_id, u1, a1, "u-rec-1", clock=self.clock, record_event_fn=self.services.record_event)
            self.assertIsNotNone(task)

            # Enqueue refuses when a newer user turn already exists for a fresh source turn.
            u_old = self.services.insert_turn(db, session, "user", "Older unqueued turn.", self.version_id, "u-rec-old", None)
            u_newer = self.services.insert_turn(db, session, "user", "Newer user message.", self.version_id, "u-rec-2", None)
            a_old = self.services.insert_turn(db, session, "assistant", "Deferred old.", self.version_id, "u-rec-old", LLMExecutionResult("Deferred old.", 1, "r-old", model="m", guidance={"move": "offer_perspective"}))
            self.assertIsNone(component_recovery.enqueue(db, self.session_id, self.version_id, u_old, a_old, "u-rec-old", clock=self.clock, record_event_fn=self.services.record_event))
            self.assertIsNotNone(u_newer)

        with self.using_dependencies(llm_handlers={"recover_human_edit_engagement": RecordedCall(side_effect=TimeoutError("timed out"))}):
            r_timeout = self.client.post(
                f"/api/sessions/{self.session_id}/component-recoveries/{task['recoveryId']}/retry",
                json={"baseVersionId": self.version_id, "idempotencyKey": task["recoveryId"] + "-1"},
            )
        self.assertEqual(r_timeout.status_code, 200, r_timeout.text)
        with self.services.connect() as db:
            tasks_map = component_recovery.tasks(db, self.session_id)
            self.assertEqual(tasks_map[task["recoveryId"]]["failureCode"], "RECOVERY_TIMEOUT")

    def test_submit_intent_feedback_revise_conflict_persists_revised_candidate_text(self):
        with self.services.connect(immediate=True) as db:
            session = db.execute("SELECT * FROM design_sessions WHERE id = ?", (self.session_id,)).fetchone()
            ctx = repository.load_design_context(db, self.session_id, self.version_id)
            ctx["intentHypotheses"] = [
                {"id": "h-old", "statement": "Reduce water coverage.", "status": "confirmed", "displayed": True, "confidence": 1.0},
                {"id": "h-tent", "statement": "Adjust pacing slightly.", "status": "tentative", "displayed": True, "confidence": 0.7},
            ]
            repository.save_design_context(db, self.version_id, ctx)
            turn_id = self.services.insert_turn(
                db, session, "assistant", "Check this inclination.", self.version_id, "intent-seed-turn",
                LLMExecutionResult("Check this inclination.", 1, "ir1", model="m", guidance={"move": "offer_perspective", "intentHypothesis": "Adjust pacing slightly.", "_intentHypothesisId": "h-tent"}),
            )

        with self.using_dependencies(llm_handlers={
            "review_intent_feedback": {
                "verdict": "conflict",
                "explanation": "Increasing water conflicts with reducing water.",
                "conflictingHypothesisIds": ["h-old"],
                "rewrite": "Increase water coverage cleanly",
            }
        }):
            revise_resp = self.client.post(
                f"/api/sessions/{self.session_id}/intent-hypotheses/h-tent/feedback",
                json={
                    "baseVersionId": self.version_id,
                    "action": "revise",
                    "sourceTurnId": turn_id,
                    "candidateText": "Increase water coverage cleanly",
                    "idempotencyKey": "intent-rev-1",
                },
            )
        self.assertEqual(revise_resp.status_code, 200, revise_resp.text)
        conflict_turn_id = revise_resp.json()["session"]["turns"][-1]["turnId"]

        # While conflict is pending, Policy 1/chat & Policy 2 are blocked with 409 INTENT_CONFLICT_PENDING BEFORE classify_turn_understanding.
        classify_spy = RecordedCall(side_effect=AssertionError("Should not run classify_turn_understanding"))
        with self.using_dependencies(llm_handlers={"classify_turn_understanding": classify_spy}):
            blocked = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={"content": "Hello", "baseVersionId": self.version_id, "idempotencyKey": "blocked-by-conflict"},
            )
        classify_spy.assert_not_called()
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["code"], "INTENT_CONFLICT_PENDING")

        # Keep new revised statement and verify revised candidateText is saved instead of old 'Adjust pacing slightly.'.
        keep_resp = self.client.post(
            f"/api/sessions/{self.session_id}/intent-hypotheses/h-tent/feedback",
            json={
                "baseVersionId": self.version_id,
                "action": "keep",
                "sourceTurnId": conflict_turn_id,
                "selectedHypothesisId": "h-tent",
                "idempotencyKey": "intent-keep-new",
            },
        )
        self.assertEqual(keep_resp.status_code, 200, keep_resp.text)
        with self.services.connect() as db:
            updated = repository.load_design_context(db, self.session_id, self.version_id)
            kept = next(item for item in updated["intentHypotheses"] if item["id"] == "h-tent")
            self.assertEqual(kept["status"], "confirmed")
            self.assertEqual(kept["statement"], "Increase water coverage cleanly")


class BinaryPolicyAndIdempotencyHttpTests(SessionTestCase):
    """HTTP tests for Policy 2 guards, clock leases, 409 cleanup, dashboard nodes, and cross-endpoint idempotency."""

    create_and_open_session = legacy.CoCreationSessionTests.create_and_open_session
    _fake_turn_understanding = staticmethod(legacy.CoCreationSessionTests._fake_turn_understanding)

    def setUp(self):
        self.clock = FakeClock()
        self.open_workspace(
            clock=self.clock,
            settings_overrides={"binary_discovery_enabled": True},
            llm_handlers={
                "classify_turn_understanding": self._fake_turn_understanding,
                "review_proposal_requirements": {"requirements": [], "automaticBindings": [], "exactTransitions": []},
                "generate_binary_step": self._generate,
            },
        )
        self.session_id, _ = self.create_and_open_session()
        self.version = self.client.get(f"/api/sessions/{self.session_id}").json()["currentVersionId"]
        with self.services.connect(immediate=True) as db:
            db.execute(
                "UPDATE level_versions SET rows_json=?, validation_json=? WHERE id=?",
                (json.dumps(binary_fixtures.ROWS), json.dumps(validate_and_solve(binary_fixtures.ROWS).as_dict()), self.version),
            )

    def _generate(self, current, context, validator, strategy_validator, request_id, deadline, budget=20):
        result = binary_fixtures.step()
        context["stageContext"]["requirementRecord"] = current.get("requirements") or result["initialRequirements"]
        if current.get("requirements") is not None:
            result["initialRequirements"] = None
        caches = binary.preflight(
            result, current, context["rows"], context["stageContext"]["stageSnapshot"],
            validator, deadline, strategy_validator, budget, clock=self.services.clock,
        )
        return result, caches

    def test_409_during_advance_releases_planning_topic_and_revision_needed_blocks_retry(self):
        # Induce 409 DISAGREEMENT_ACTIVE inside _advance commit/guard while topic is 'planning'.
        def mutate_constraints_mid_step(current, context, validator, strategy_validator, request_id, deadline, budget=20):
            res, caches = self._generate(current, context, validator, strategy_validator, request_id, deadline, budget)
            with self.services.connect(immediate=True) as db:
                design_ctx = repository.load_design_context(db, self.session_id, self.version)
                design_ctx["activeDisagreement"] = {"status": "active", "subject": "human_edit"}
                repository.save_design_context(db, self.version, design_ctx)
            return res, caches

        with self.using_dependencies(llm_handlers={"generate_binary_step": mutate_constraints_mid_step}):
            resp = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={"baseVersionId": self.version, "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "p2-409"},
            )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "DISAGREEMENT_ACTIVE")
        with self.services.connect() as db:
            design_ctx = repository.load_design_context(db, self.session_id, self.version)
            design_ctx["activeDisagreement"] = None
            repository.save_design_context(db, self.version, design_ctx)
            current = binary.current_topic(db, self.session_id, self.version, clock=self.clock)
            self.assertNotEqual(current["status"], "planning")
            self.assertEqual(current["status"], "cancelled")

            # Also verify clock injection expires 'planning' lease cleanly and revision_needed rejects retry with 409 PROPOSAL_STALE.
            current["status"] = "planning"
            current["leaseUntil"] = self.clock.utc_datetime().timestamp() + 10
            repository.record_event(db, self.session_id, "binary_discovery_state", current, self.services.utc_now())
        self.clock.advance(20)
        with self.services.connect(immediate=True) as db:
            expired = binary.current_topic(db, self.session_id, self.version, clock=self.clock)
            self.assertEqual(expired["status"], "retry_pending")
            expired["status"] = "revision_needed"
            repository.record_event(db, self.session_id, "binary_discovery_state", expired, self.services.utc_now())
            binary_routes._put_receipt(
                self.services, db, self.session_id, "p2-rev-needed",
                {"kind": "start", "baseVersionId": self.version, "content": "Make it simpler.", "requestProposal": True},
                "pending",
            )
            expired["initialMessageKey"] = "p2-rev-needed"
            repository.record_event(db, self.session_id, "binary_discovery_state", expired, self.services.utc_now())

        blocked = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={"baseVersionId": self.version, "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "p2-rev-needed"},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["code"], "PROPOSAL_STALE")

    def test_cross_endpoint_idempotency_dashboard_node_and_sync_replay(self):
        with self.services.connect(immediate=True) as db:
            db.execute(
                "UPDATE design_sessions SET match_id = 'match-p2', player_number = 1, demo_mode = 0 WHERE id = ?",
                (self.session_id,),
            )
        # 1. Start Policy 2 clarification -> verify dashboardNode on clarification turn & replay syncs turn.
        start = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={"baseVersionId": self.version, "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "p2-flow-1"},
        )
        self.assertEqual(start.status_code, 200, start.text)
        state = start.json()
        topic_id = state["proposalFlowState"]["topicId"]
        clar_turn = state["turns"][-1]
        self.assertNotIn("dashboardNode", clar_turn["guidance"])
        with self.services.connect() as db:
            row = db.execute("SELECT guidance_json FROM conversation_turns WHERE id = ?", (clar_turn["turnId"],)).fetchone()
            stored_guidance = json.loads(row["guidance_json"])
        self.assertEqual(stored_guidance["dashboardNode"]["proposalTopicId"], topic_id)
        self.assertTrue(any((ev.get("metadata") or {}).get("nodeId") == f"proposal:{topic_id}" for ev in self.services.online_sync.events))

        before_sync_count = len(self.services.online_sync.events)
        replay_start = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={"baseVersionId": self.version, "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "p2-flow-1"},
        )
        self.assertEqual(replay_start.status_code, 200)
        self.assertGreater(len(self.services.online_sync.events), before_sync_count)

        # 2. Cannot reuse a binary choice key for ordinary chat -> 409 IDEMPOTENCY_CONFLICT.
        with self.services.connect(immediate=True) as db:
            binary_routes._put_receipt(
                self.services, db, self.session_id, "p2-choice-receipt",
                {"kind": "choice", "baseVersionId": self.version}, "done",
            )
        cross = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={"baseVersionId": self.version, "content": "Hijack choice key", "idempotencyKey": "p2-choice-receipt"},
        )
        self.assertEqual(cross.status_code, 409)
        self.assertEqual(cross.json()["code"], "IDEMPOTENCY_CONFLICT")

        # 3. Cannot reuse ordinary chat/failure key for binary cancel -> 409 IDEMPOTENCY_CONFLICT.
        with self.services.connect(immediate=True) as db:
            self.services.record_event(
                db, self.session_id, "message_generation_failed",
                {"messageKey": "chat-failed-key", "baseVersionId": self.version, "requestSignature": "sig-1"},
                self.services.utc_now(),
            )
        bad_cancel = self.client.post(
            f"/api/sessions/{self.session_id}/proposal-discovery/cancel",
            json={"baseVersionId": self.version, "topicId": topic_id, "idempotencyKey": "chat-failed-key"},
        )
        self.assertEqual(bad_cancel.status_code, 409)
        self.assertEqual(bad_cancel.json()["code"], "IDEMPOTENCY_CONFLICT")

        # 4. Pending 'start' receipt for an ended/cancelled topic raises 409 PROPOSAL_STALE.
        cancel_resp = self.client.post(
            f"/api/sessions/{self.session_id}/proposal-discovery/cancel",
            json={"baseVersionId": self.version, "topicId": topic_id, "idempotencyKey": "p2-cancel-1"},
        )
        self.assertEqual(cancel_resp.status_code, 200, cancel_resp.text)
        with self.services.connect(immediate=True) as db:
            binary_routes._put_receipt(
                self.services, db, self.session_id, "p2-cancelled-pending",
                {"kind": "start", "baseVersionId": self.version, "content": "Make it simpler.", "requestProposal": True},
                "pending",
            )
        stale_pending = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={"baseVersionId": self.version, "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "p2-cancelled-pending"},
        )
        self.assertEqual(stale_pending.status_code, 409)
        self.assertEqual(stale_pending.json()["code"], "PROPOSAL_STALE")

    def test_final_commit_version_changed_raises_proposal_stale_with_reason(self):
        with self.services.connect(immediate=True) as db:
            session = db.execute("SELECT * FROM design_sessions WHERE id = ?", (self.session_id,)).fetchone()
            db.execute("UPDATE level_versions SET rows_json=? WHERE id=?", (json.dumps(legacy.SAMPLE_ROWS), self.version))
            offer_turn_id = self.services.insert_turn(
                db, session, "assistant", "Offer ready.", self.version, "seed-ver-race-offer",
                LLMExecutionResult(
                    "Offer ready.", 1, "ver-offer", model="mock",
                    guidance={
                        "move": "offer_revision",
                        "proposalOffer": {
                            "summary": "Move player start one cell left",
                            "rationale": "Moving the player left gives a cleaner opening approach.",
                            "executionBrief": legacy.PLAYER_MOVE_BRIEF,
                        },
                        "uiCues": [],
                    },
                ),
            )

        def mutate_version_during_reply(*args, **kwargs):
            with self.services.connect(immediate=True) as db:
                db.execute(
                    "UPDATE design_sessions SET current_version_id = 'newer-version-id' WHERE id = ?",
                    (self.session_id,),
                )
            return LLMExecutionResult(
                "Proposal ready.",
                1,
                "ver-race",
                model="mock",
                proposed_rows=legacy.EDITED_ROWS,
                revision_plan=legacy.PLAYER_MOVE_CONTRACT["revisionPlan"],
                revision_contract=legacy.PLAYER_MOVE_CONTRACT,
                revision_operations=legacy.PLAYER_MOVE_OPERATIONS,
                proposal_diagnostics={"selectedStrategyIndex": 1, "changedCellCount": 2},
                guidance={"move": "deliver_revision", "proposalOffer": None, "uiCues": []},
            )

        with self.using_dependencies(
            settings={"binary_discovery_enabled": False},
            llm_handlers={"generate_chat_reply": mutate_version_during_reply},
        ):
            raced = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "baseVersionId": self.version,
                    "content": "Execute the player move.",
                    "idempotencyKey": "version-race-key",
                    "action": "execute_revision",
                    "sourceTurnId": offer_turn_id,
                },
            )
        self.assertEqual(raced.status_code, 409, raced.text)
        self.assertEqual(raced.json()["code"], "PROPOSAL_STALE")
        self.assertEqual(raced.json()["details"]["reason"], "version_changed")

    def test_require_choice_coverage_allows_merged_numeric_turn_and_rejects_choice_numeric_lock(self):
        choice = {"sourceTurnId": "turn-choice", "promise": {"kind": "reduce_obstacles"}}
        merged_record = {
            "requirements": [{
                "sourceTurnId": "turn-user",
                "sourceTurnIds": ["turn-user", "turn-choice"],
                "component": "water",
                "property": "count",
                "relation": "decrease",
                "value": 2,
                "unit": "absolute",
            }],
            "exactTransitions": [],
        }
        binary.require_choice_coverage(merged_record, [choice])

        locked_choice_record = {
            "requirements": [{
                "sourceTurnId": "turn-choice",
                "sourceTurnIds": ["turn-choice"],
                "component": "water",
                "property": "count",
                "relation": "decrease",
                "value": 2,
                "unit": "absolute",
            }],
            "exactTransitions": [],
        }
        with self.assertRaisesRegex(ValueError, "BINARY_CHOICE_COVERAGE"):
            binary.require_choice_coverage(locked_choice_record, [choice])

    def test_advance_question_count_three_uses_full_deadline_for_baseline(self):
        start = self.client.post(
            f"/api/sessions/{self.session_id}/messages",
            json={"baseVersionId": self.version, "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "p2-q3-start"},
        )
        self.assertEqual(start.status_code, 200, start.text)
        state = start.json()
        question = state["proposalFlowState"]["pendingQuestion"]
        with self.services.connect(immediate=True) as db:
            current = binary.current_topic(db, self.session_id, self.version, clock=self.clock)
            current["questionCount"] = 3
            repository.record_event(db, self.session_id, "binary_discovery_state", current, self.services.utc_now())

        started_mono = self.services.monotonic()
        seen_deadlines = []

        def record_features(*args, **kwargs):
            seen_deadlines.append(kwargs.get("deadline"))
            return {"minimumPushes": 1}

        def plan_from_selected(**kwargs):
            with self.services.connect() as db:
                current = binary.current_topic(db, self.session_id, self.version, clock=self.clock)
            cache = current["selectedCandidate"]
            plan = parse_revision_plan({"strategies": [cache["strategy"]]})
            contract = llm._build_revision_execution_contract(plan, current["initialRequest"], kwargs["stage_context"])
            return LLMExecutionResult(
                "", 1, "q3-final", model="offline",
                proposed_rows=cache["rows"], revision_contract=contract, revision_plan=plan.as_dict(),
                revision_operations=binary_routes._operations(binary_fixtures.ROWS, cache["rows"]),
                proposal_diagnostics={"selectedStrategyIndex": 1},
            )

        with self.using_dependencies(
            settings={"llm_internal_deadline_seconds": 10.0},
            llm_handlers={
                "proposal_route_features": RecordedCall(side_effect=record_features),
                "generate_binary_step": ({"action": "plan", "question": "", "note": "", "branches": []}, []),
            },
            llm_options={"revision_search_generator": RecordedCall(side_effect=plan_from_selected)},
        ):
            resp = self.client.post(
                f"/api/sessions/{self.session_id}/proposal-discovery/choices",
                json={
                    "baseVersionId": self.version,
                    "topicId": state["proposalFlowState"]["topicId"],
                    "questionId": question["questionId"],
                    "choiceId": question["choices"][0]["choiceId"],
                    "displayLanguage": "en",
                    "idempotencyKey": "p2-q3-choice",
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(seen_deadlines)
        self.assertGreater(seen_deadlines[0], started_mono + 5.0)


if __name__ == "__main__":
    unittest.main()
