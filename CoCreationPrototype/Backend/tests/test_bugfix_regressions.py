"""Regression tests covering verified Phase 4 bug fixes on CoCreationPrototype/Backend."""
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from openai import APITimeoutError

BACKEND_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
    import cocreation  # noqa: F401
from tests.fakes import RecordedAsyncCall, RecordedCall, SessionTestCase, llm_scope
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from cocreation.api import config_models
from cocreation.api import context_and_discovery
from cocreation.api import proposal_and_challenge
from cocreation.api import session_persistence_helpers
from cocreation.domain import binary_discovery as binary
from cocreation.llm import facade as llm
from cocreation.persistence import repository
from cocreation.api.context_and_discovery import _binary_policy_turns_and_requests
from cocreation.llm.snapshot_grounding import (
    _entity_claim_is_non_current,
    _server_fact_text,
    _validate_coordinate_claims,
)
from cocreation.domain.design_requirements import validate_requirement_record
from cocreation.domain.level_validation import analyze_user_map_claims, build_stage_snapshot, validate_and_solve
from cocreation.domain.proposal_search import parse_revision_plan, search_revision_plan
from cocreation.domain.understanding_review import merge_review
import test_sessions as legacy


BASE_ROWS = list(legacy.SAMPLE_ROWS)
WATER_ROWS = list(BASE_ROWS)
WATER_ROWS[2] = "#.@.....@..#"


class DomainAndLLMBugfixTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_stage_snapshot(WATER_ROWS, version_id="v1", stage_number=1)

    def test_duplicate_requirement_preserves_source_turn_ids_and_guards_types(self):
        sources = [{"id": "u1", "content": "减少水域"}, {"id": "u2", "content": "减少水域"}]
        raw = {
            "requirements": [
                {
                    "component": "water",
                    "property": "count",
                    "relation": "decrease",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "strength": "goal",
                    "focused": False,
                    "statement": "Reduce water count",
                    "sourceTurnId": "u1",
                    "evidenceSpan": "减少水域",
                    "priorityEvidenceSpan": "",
                    "scope": "all",
                },
                {
                    "component": "water",
                    "property": "count",
                    "relation": "decrease",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "strength": "goal",
                    "focused": True,
                    "statement": "Reduce water count",
                    "sourceTurnId": "u2",
                    "evidenceSpan": "减少水域",
                    "priorityEvidenceSpan": "",
                    "scope": "all",
                },
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }
        record = validate_requirement_record(raw, sources, self.snapshot)
        self.assertEqual(len(record["requirements"]), 1)
        self.assertEqual(record["requirements"][0]["sourceTurnIds"], ["u1", "u2"])
        self.assertTrue(record["requirements"][0]["focused"])

        promise = {
            "kind": "reduce_obstacles",
            "component": "water",
            "focus": {"row": 3, "column": 3, "radius": 1},
            "corridor": None,
        }
        binary.require_choice_coverage(
            record,
            [{"sourceTurnId": "u1", "promise": promise}, {"sourceTurnId": "u2", "promise": promise}],
        )

        bad_source = copy.deepcopy(raw)
        bad_source["requirements"] = [bad_source["requirements"][0]]
        bad_source["requirements"][0]["sourceTurnId"] = ["u1"]
        with self.assertRaises(ValueError):
            validate_requirement_record(bad_source, sources, self.snapshot)

        bad_binding = {
            "requirements": [raw["requirements"][0]],
            "automaticBindings": [{"entity": 123, "reason": "bad type"}],
            "exactTransitions": [],
        }
        with self.assertRaises(ValueError):
            validate_requirement_record(bad_binding, sources, self.snapshot)

    def test_binary_preflight_avoids_reusing_branch_a_candidate_for_branch_b(self):
        both_valid_rows = list(WATER_ROWS)
        both_valid_rows[2] = "#.......@..#"
        validation = validate_and_solve(both_valid_rows)
        promise = {
            "kind": "reduce_obstacles",
            "component": "water",
            "focus": None,
            "corridor": None,
        }
        step = {
            "action": "ask",
            "question": "Which obstacle would you rather open?",
            "note": "",
            "initialRequirements": {"requirements": [], "automaticBindings": [], "exactTransitions": []},
            "branches": [
                {
                    "label": "Open left water",
                    "strategy": {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_water"],
                        "preserve": ["outer_shell", "player", "boxes", "targets"],
                        "editBudget": 1,
                        "metricGoals": [],
                    },
                    "promise": promise,
                },
                {
                    "label": "Open right water",
                    "strategy": {
                        "effect": "open_route",
                        "focus": {"row": 3, "column": 9, "radius": 1},
                        "operators": ["remove_water"],
                        "preserve": ["outer_shell", "player", "boxes", "targets"],
                        "editBudget": 1,
                        "metricGoals": [],
                    },
                    "promise": {
                        "kind": "reduce_obstacles",
                        "component": "water",
                        "focus": {"row": 3, "column": 9, "radius": 1},
                        "corridor": None,
                    },
                },
            ],
        }
        topic = {
            "binding": "fixture-bind",
            "choices": [],
            "initialRequest": "Reduce water obstacles.",
            "questionCount": 0,
            "understanding": {},
            "userTurns": [{"id": "u1", "content": "Reduce water obstacles."}],
            "selectedCandidate": {
                "rows": both_valid_rows,
                "topicBinding": "fixture-bind",
                "validation": validation.as_dict(),
            },
        }
        right_or_both = list(WATER_ROWS)
        right_or_both[2] = "#.@........#"

        def fake_validate(rows, deadline=None):
            return validate_and_solve(rows, deadline=deadline)

        # selectedCandidate removes cell (3,3) which satisfies Branch A (`focus=None`).
        # Make selectedCandidate also satisfy Branch B's promise (`focus=(3,9,1)` via (2->1)?):
        # If both Branch A & B share the cached candidate unless excluded: test with same focus!
        step["branches"][1]["strategy"]["focus"] = None
        step["branches"][1]["promise"] = copy.deepcopy(promise)
        step["branches"][1]["promise"]["component"] = "obstacles"
        caches = binary.preflight(step, topic, WATER_ROWS, {}, fake_validate, time.monotonic() + 30.0)
        self.assertEqual(len(caches), 2)
        self.assertNotEqual(caches[0]["rows"], caches[1]["rows"])

    def test_search_seen_maps_resets_per_strategy_and_minimum_pushes_respects_deadline(self):
        plan = parse_revision_plan(
            {
                "strategies": [
                    {
                        "effect": "reshape_water",
                        "focus": {"row": 3, "column": 3, "radius": 1},
                        "operators": ["remove_water"],
                        "preserve": ["outer_shell", "player", "boxes", "targets"],
                        "editBudget": 1,
                        "metricGoals": [{"metric": "searchedStates", "direction": "increase", "minimumDelta": 500}],
                    },
                    {
                        "effect": "reshape_water",
                        "focus": {"row": 3, "column": 3, "radius": 1},
                        "operators": ["remove_water"],
                        "preserve": ["outer_shell", "player", "boxes", "targets"],
                        "editBudget": 1,
                        "metricGoals": [],
                    },
                ]
            }
        )
        result = search_revision_plan(
            WATER_ROWS,
            plan,
            lambda rows: validate_and_solve(rows),
            baseline_metrics={"searchedStates": 10},
        )
        self.assertEqual(result.strategy_index, 2)

        from cocreation.domain.proposal_search import MetricGoal, _metric_matches

        baseline = validate_and_solve(WATER_ROWS)
        with self.assertRaises(TimeoutError):
            _metric_matches(
                (MetricGoal("minimumPushes", "decrease", 1),),
                {"minimumPushes": 10},
                baseline,
                deadline=time.monotonic() - 1.0,
            )

    def test_server_fact_water_neighbor_preposition_and_clause_lookbehind(self):
        text = _server_fact_text({"factType": "tile_state", "row": 3, "column": 3}, WATER_ROWS, None, "en", {})
        self.assertEqual(text, "Row 3, column 3 is water.")
        text_zh = _server_fact_text({"factType": "tile_state", "row": 3, "column": 3}, WATER_ROWS, None, "zh-CN", {})
        self.assertEqual(text_zh, "第3行第3列是水域。")

        # "adjacent to (2, 2) is a wall" should not false-positive when (2, 2) is actually floor '.'.
        _validate_coordinate_claims("The open tile adjacent to (2, 2) is floor in the corridor.", WATER_ROWS)
        _validate_coordinate_claims("adjacent to (2, 2) is a wall", WATER_ROWS)
        # "If (2, 2) is water" -> non-current lookbehind skips it.
        _validate_coordinate_claims("If (2, 2) is water, the route changes.", WATER_ROWS)
        self.assertTrue(_entity_claim_is_non_current("If (2, 2) is water, adjust it", 3, 18))
        # Clause delimiter stops lookbehind from spilling across comma.
        with self.assertRaises(ValueError):
            _validate_coordinate_claims("If P moves left, (2, 2) is water right now.", WATER_ROWS)

    def test_translations_out_of_order_turn_id_and_card_grounding(self):
        t0 = {
            "turnId": "t0",
            "body": "The player starts near the center.",
            "followUpQuestion": "Which corridor should open first?",
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "proposalSummary": None,
            "uiCueTexts": [],
            "stageRows": BASE_ROWS,
        }
        t1 = {
            "turnId": "t1",
            "body": "Keep this option ready.",
            "followUpQuestion": None,
            "intentHypothesis": "The player prefers a shorter route.",
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "proposalSummary": None,
            "uiCueTexts": [],
            "coordinateLinks": [{"text": "(2, 2) floor"}],
            "stageRows": BASE_ROWS,
        }
        out_of_order = [
            {
                "turnId": "t1",
                "body": "保留此选项，参见 (2, 2) 地板。",
                "followUpQuestion": None,
                "intentHypothesis": "玩家更希望路线更短。",
                "proposalOfferSummary": None,
                "proposalOfferRationale": None,
                "proposalSummary": None,
                "uiCueTexts": [],
                "coordinateLinkTexts": ["(2, 2) 地板"],
            },
            {
                "turnId": "t0",
                "body": "玩家从中央附近出发。",
                "followUpQuestion": "你想先打开哪条走廊？",
                "intentHypothesis": None,
                "proposalOfferSummary": None,
                "proposalOfferRationale": None,
                "proposalSummary": None,
                "uiCueTexts": [],
            },
        ]
        result = llm.validate_translation_response({"translations": out_of_order}, [t0, t1], target_language="zh-CN")
        self.assertEqual([item["turnId"] for item in result], ["t0", "t1"])

        bad_card = copy.deepcopy(out_of_order)
        bad_card[0]["intentHypothesis"] = "把(2, 2)变成墙，实际上(2, 2)是墙。"
        with self.assertRaises(ValueError):
            llm.validate_translation_response({"translations": bad_card}, [t0, t1], target_language="zh-CN")

    def test_merge_review_rejects_component_or_entities_on_goals_and_preserve(self):
        candidate = {
            "acts": ["revision_request"],
            "elements": ["water"],
            "evidenceSpan": "减少水域并保留墙",
            "directionSufficient": True,
            "mapRelated": True,
            "changes": [],
            "proposalUnderstanding": {
                "summary": "Reduce water",
                "aspect": "mechanism",
                "goals": [{"statement": "Reduce water", "sourceTurnId": "latest", "evidenceSpan": "减少水域"}],
                "editScope": [{"statement": "Edit water", "sourceTurnId": "latest", "evidenceSpan": "减少水域", "component": "water"}],
                "focus": [],
                "preserve": [{"statement": "Preserve wall", "sourceTurnId": "latest", "evidenceSpan": "保留墙"}],
                "sufficient": True,
                "nextQuestionDimension": "none",
            },
        }
        targets = [
            {
                "targetId": "append_goals",
                "path": ["proposalUnderstanding", "goals"],
                "mode": "append",
                "currentValue": [],
            }
        ]
        verdict = {
            "decision": "CORRECT",
            "issues": [],
            "corrections": [
                {
                    "targetId": "append_goals",
                    "reason": "Add goal item",
                    "value": [{"statement": "Reduce water", "component": "water", "sourceTurnId": "latest", "evidenceSpan": "减少水域"}],
                    "evidenceRefs": ["user:latest"],
                }
            ],
        }
        with self.assertRaises(ValueError):
            merge_review(verdict, candidate, targets, {"latest": "减少水域并保留墙"})

    def test_intent_and_challenge_preserve_upstream_timeout_and_empty_choices(self):
        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=APITimeoutError(request=None))):
            with self.assertRaises(llm.LLMServiceError) as caught:
                llm.classify_turn_understanding(
                    [{"role": "user", "content": "Open the left corridor."}],
                    self.snapshot,
                    "req-timeout",
                    _deadline=time.monotonic() + 30.0,
                )
        self.assertEqual(caught.exception.code, "UPSTREAM_TIMEOUT")

        empty_choice_response = SimpleNamespace(choices=[])
        with llm_scope(completion_requester=RecordedAsyncCall(return_value=empty_choice_response)):
            with self.assertRaises(llm.LLMServiceError) as caught2:
                llm.classify_challenge_reason(
                    "I think this detour helps",
                    {"primary": "p", "secondary": "s"},
                    "Open left",
                    "req-empty",
                    _deadline=time.monotonic() + 30.0,
                )
        self.assertEqual(caught2.exception.code, "MODEL_EMPTY_RESPONSE")

    def test_chat_generation_shares_reliability_state_with_caller(self):
        stage_context = {"_reliabilityState": {}, "stageSnapshot": self.snapshot, "designContext": {}}
        expected = llm.LLMExecutionResult("Ok, let's adjust water.", 1, "req-rel")

        def fake_plain(**kwargs):
            kwargs["stage_context"]["_reliabilityState"]["modelGenerationSucceeded"] = True
            return expected

        with llm_scope(handlers={'generate_plain_chat': RecordedCall(side_effect=fake_plain)}):
            llm.generate_chat_reply([{"role": "user", "content": "What do you think?"}], BASE_ROWS, "req-rel", stage_context=stage_context)
        self.assertTrue(stage_context["_reliabilityState"].get("modelGenerationSucceeded"))

    def test_analyze_user_map_claims_empty_snapshot_does_not_crash(self):
        claims = analyze_user_map_claims("(2, 2) is water. P is located in row 2, column 2.", {"entities": [{"id": "P", "row": 2, "column": 2, "identityConfidence": "ambiguous"}], "rows": []})
        self.assertEqual(claims["conflicts"][0]["reason"], "entity_identity_unknown")


class HttpAndPersistenceBugfixTests(SessionTestCase):
    setUp = legacy.CoCreationSessionTests.setUp
    create_and_open_session = legacy.CoCreationSessionTests.create_and_open_session
    read_session = legacy.CoCreationSessionTests.read_session
    _fake_turn_understanding = staticmethod(legacy.CoCreationSessionTests._fake_turn_understanding)

    def test_policy1_excludes_policy2_turns_and_ancestor_evidence_resolves(self):
        session_id, _ = self.create_and_open_session("policy-isolation")
        self.session_id = session_id
        v1 = self.read_session()["currentVersionId"]
        with self.services.connect(immediate=True) as db:
            session = repository.get_session(db, session_id)
            u_hist = self.services.insert_turn(db, session, "user", "Make the first room brighter.", v1, "hist-key", None)
            config_models.record_event(
                db,
                session_id,
                "binary_discovery_state",
                {"versionId": v1, "topicId": "binary-t1", "status": "cancelled", "userTurns": [{"id": u_hist, "content": "Make the first room brighter."}], "choices": []},
                self.services.utc_now(),
            )
            turns = db.execute("SELECT id, role, content, request_id, sequence_number, guidance_json FROM conversation_turns WHERE session_id=? AND version_id=?", (session_id, v1)).fetchall()
            turn_ids, req_ids = _binary_policy_turns_and_requests(db, session_id, turns)
            self.assertIn(u_hist, turn_ids)
            self.assertEqual(req_ids, set())

            # Test ancestor-Stage evidence DB lookup in build_llm_context
            v2_validation = validate_and_solve(legacy.WATER_EDITED_ROWS)
            v2 = session_persistence_helpers._insert_version(db, session, v2_validation, "human_edit", "Edited", "edit-k1", session_persistence_helpers.get_version(db, session_id, v1), services=self.services)
            ctx_v2 = repository.load_design_context(db, session_id, v2)
            ctx_v2["intentHypotheses"] = [
                {
                    "id": "h1",
                    "statement": "Brighter first room",
                    "status": "confirmed",
                    "semanticClaims": [
                        {
                            "subject": "layout",
                            "attribute": "clarity",
                            "direction": "increase",
                            "sourceUserTurnId": u_hist,
                        }
                    ],
                }
            ]
            repository.save_design_context(db, v2, ctx_v2)
            built = context_and_discovery.build_llm_context(db, session_id, session_persistence_helpers.get_version(db, session_id, v2), services=self.services)
            hyp = built["stageContext"]["designContext"]["intentHypotheses"][0]
            self.assertEqual(hyp["evidenceTurnId"], u_hist)
            self.assertIn("brighter", hyp["evidenceText"])

    def test_proposal_binding_preserved_idempotency_conflicts_and_finalize_guards(self):
        session_id, _ = self.create_and_open_session("binding-preserve")
        self.session_id = session_id
        v1 = self.read_session()["currentVersionId"]

        # Create an offer turn and execute it, verify proposal_binding_json survives _proposal_review_guidance
        with self.services.connect(immediate=True) as db:
            session = repository.get_session(db, session_id)
            offer_exec = llm.LLMExecutionResult(
                "Offer ready.",
                1,
                "offer-k1",
                guidance={
                    "move": "offer_revision",
                    "proposalOffer": {
                        "summary": "Add water",
                        "rationale": "Reshape flow",
                        "executionBrief": legacy.WATER_RESHAPE_BRIEF,
                        "revisionWorkflow": {"mode": "enforced", "requirementPolicyVersion": 1, "requirementRecord": None},
                    },
                },
            )
            bound = proposal_and_challenge._bind_execution_to_stage(
                offer_exec,
                v1,
                BASE_ROWS,
                language="en",
                entity_bindings=repository.load_entity_bindings(db, session_id, v1),
            )
            offer_turn_id = self.services.insert_turn(db, session, "assistant", bound.assistant_message, v1, "offer-k1", bound)
            db.execute(
                "INSERT INTO designer_decisions(id, session_id, version_id, proposal_id, decision_type, reason, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("dec0", session_id, None, "dummy-prop", "reject", "", "reused-key", self.services.utc_now()),
            )

        with self.using_dependencies(
            llm_handlers={
                "generate_chat_reply": llm.LLMExecutionResult('Verified', 1, 'exec-k1', proposed_rows=legacy.WATER_EDITED_ROWS, revision_contract=legacy.WATER_RESHAPE_CONTRACT, revision_plan=legacy.WATER_RESHAPE_CONTRACT['revisionPlan'], revision_operations=legacy.WATER_RESHAPE_OPERATIONS, proposal_diagnostics={'selectedStrategyIndex': 1}),
            },
        ):
            res = self.client.post(
                f"/api/sessions/{session_id}/messages",
                json={
                    "baseVersionId": v1,
                    "content": "Apply offer",
                    "idempotencyKey": "exec-k1",
                    "action": "execute_revision",
                    "sourceTurnId": offer_turn_id,
                },
            )
        self.assertEqual(res.status_code, 200, res.text)
        proposal = res.json()["proposals"][0]

        with self.services.connect() as db:
            pturn = db.execute("SELECT proposal_binding_json FROM conversation_turns WHERE id = ?", (proposal["assistantTurnId"],)).fetchone()
            pbinding = json.loads(pturn["proposal_binding_json"])
            self.assertEqual(pbinding["sourceOfferTurnId"], offer_turn_id)
            self.assertEqual(len(pbinding["executionBrief"]["requiredTransitions"]), 2)

        # Idempotency conflict check on _decide_proposal_impl (reusing 'reused-key' on different proposal_id)
        conflict = self.client.post(
            f"/api/sessions/{session_id}/proposals/{proposal['proposalId']}/decision",
            json={"baseVersionId": v1, "decision": "accept", "idempotencyKey": "reused-key"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "IDEMPOTENCY_CONFLICT")

        # Tamper stored proposed_rows_json to verify accept enforces frozen EXECUTION_REPLAY_MISMATCH from proposal_binding_json
        with self.services.connect(immediate=True) as db:
            db.execute("UPDATE change_proposals SET proposed_rows_json = ? WHERE id = ?", (json.dumps(legacy.WATER_RIGHT_ROWS), proposal["proposalId"]))
        tampered = self.client.post(
            f"/api/sessions/{session_id}/proposals/{proposal['proposalId']}/decision",
            json={"baseVersionId": v1, "decision": "accept", "idempotencyKey": "accept-tampered"},
        )
        self.assertEqual(tampered.status_code, 409)
        self.assertEqual(tampered.json()["code"], "EXECUTION_REPLAY_MISMATCH")

        # Human edit supersedes pending proposal
        edit_res = self.client.post(
            f"/api/sessions/{session_id}/versions",
            json={"baseVersionId": v1, "rows": legacy.WATER_EDITED_ROWS, "idempotencyKey": "human-supersede"},
        )
        self.assertEqual(edit_res.status_code, 200, edit_res.text)
        self.assertEqual(edit_res.json()["proposals"][0]["status"], "superseded")

        # Pre-deadline finalize with modified rows -> 409 UNSAVED_DRAFT_ROWS
        v2 = edit_res.json()["currentVersionId"]
        unsaved = self.client.post(
            f"/api/sessions/{session_id}/finalize",
            json={"baseVersionId": v2, "idempotencyKey": "fin-unsaved", "rows": BASE_ROWS},
        )
        self.assertEqual(unsaved.status_code, 409)
        self.assertEqual(unsaved.json()["code"], "UNSAVED_DRAFT_ROWS")

        # Finalize and verify /intention idempotency conflict check
        fin_res = self.client.post(
            f"/api/sessions/{session_id}/finalize",
            json={"baseVersionId": v2, "idempotencyKey": "fin-ok"},
        )
        self.assertEqual(fin_res.status_code, 200, fin_res.text)
        i1 = self.client.post(
            f"/api/sessions/{session_id}/intention",
            json={"content": "Open left route.", "idempotencyKey": "intent-k1"},
        )
        self.assertEqual(i1.status_code, 200, i1.text)
        i2 = self.client.post(
            f"/api/sessions/{session_id}/intention",
            json={"content": "Different text.", "idempotencyKey": "intent-k1"},
        )
        self.assertEqual(i2.status_code, 409)
        self.assertEqual(i2.json()["code"], "IDEMPOTENCY_CONFLICT")

    def test_binary_routes_bind_latest_records_outcome_and_clears_retry_action_on_non_retryable(self):
        session_id, _ = self.create_and_open_session("binary-details")
        self.session_id = session_id
        v1 = self.read_session()["currentVersionId"]
        with self.services.connect(immediate=True) as db:
            db.execute("UPDATE level_versions SET rows_json=?, validation_json=? WHERE id=?", (json.dumps(WATER_ROWS), json.dumps(validate_and_solve(WATER_ROWS).as_dict()), v1))
        step = {
            "action": "ask",
            "question": "Left or right?",
            "note": "",
            "initialRequirements": {"requirements": [], "automaticBindings": [], "exactTransitions": []},
            "branches": [
                {"label": "Left", "strategy": {"effect": "open_route", "focus": {"row": 3, "column": 3, "radius": 1}, "operators": ["remove_water"], "preserve": ["outer_shell", "player", "boxes", "targets"], "editBudget": 1, "metricGoals": []}, "promise": {"kind": "reduce_obstacles", "component": "water", "focus": {"row": 3, "column": 3, "radius": 1}, "corridor": None}},
                {"label": "Right", "strategy": {"effect": "open_route", "focus": {"row": 3, "column": 9, "radius": 1}, "operators": ["remove_water"], "preserve": ["outer_shell", "player", "boxes", "targets"], "editBudget": 1, "metricGoals": []}, "promise": {"kind": "reduce_obstacles", "component": "water", "focus": {"row": 3, "column": 9, "radius": 1}, "corridor": None}},
            ],
        }
        understanding = {
            "acts": ["revision_request"],
            "elements": ["water"],
            "evidenceSpan": "Make it simpler.",
            "directionSufficient": False,
            "mapRelated": True,
            "changes": [],
            "proposalUnderstanding": {
                "summary": "Simplify water",
                "aspect": "mechanism",
                "goals": [{"statement": "Make it simpler", "sourceTurnId": "latest", "evidenceSpan": "Make it simpler."}],
                "editScope": [],
                "focus": [],
                "preserve": [],
                "sufficient": False,
                "nextQuestionDimension": "visual_direction",
            },
        }
        with self.using_dependencies(
            should_start_binary_fn=RecordedCall(return_value=True),
            llm_handlers={
                "classify_turn_understanding": understanding,
                "generate_binary_step": lambda *a, **kw: (copy.deepcopy(step), binary.preflight(binary.parse_step(copy.deepcopy(step), 0), a[0], WATER_ROWS, {}, lambda r, d=None: validate_and_solve(r, deadline=d), time.monotonic() + 30)),
            },
        ):
            res = self.client.post(f"/api/sessions/{session_id}/messages", json={"baseVersionId": v1, "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "bin-k1"})
        self.assertEqual(res.status_code, 200, res.text)

        with self.services.connect() as db:
            tu = db.execute("SELECT payload_json FROM audit_events WHERE session_id=? AND event_type='turn_understanding' ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
            tu_payload = json.loads(tu["payload_json"])
            self.assertNotEqual(tu_payload["result"]["proposalUnderstanding"]["goals"][0]["sourceTurnId"], "latest")
            rdo = db.execute("SELECT payload_json FROM audit_events WHERE session_id=? AND event_type='reply_delivery_outcome'", (session_id,)).fetchone()
            self.assertIsNotNone(rdo)

        # Non-retryable failure on next choice must clear retryAction and nest details on message_generation_failed
        q = res.json()["proposalFlowState"]["pendingQuestion"]
        err = llm.LLMServiceError("BINARY_PROMISE_NOT_REALIZED", "Cannot realize", "req-nr", False, 1, 422)
        err.details = {"task": "revision_plan", "failureStage": "binary_preflight", "reasonCode": "BINARY_PROMISE_NOT_REALIZED"}
        with self.using_dependencies(llm_handlers={"generate_binary_step": err}):
            fail_res = self.client.post(
                f"/api/sessions/{session_id}/proposal-discovery/choices",
                json={"baseVersionId": v1, "topicId": q["topicId"], "questionId": q["questionId"], "choiceId": q["choices"][0]["choiceId"], "displayLanguage": "en", "idempotencyKey": "bin-c1"},
            )
        self.assertEqual(fail_res.status_code, 422)
        latest = self.read_session()
        self.assertIsNone(latest["proposalFlowState"]["retryAction"])
        self.assertEqual(latest["lastMessageFailure"]["details"]["failureStage"], "binary_preflight")


if __name__ == "__main__":
    unittest.main()
