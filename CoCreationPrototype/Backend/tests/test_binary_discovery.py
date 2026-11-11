"""Offline proofs, HTTP receipts and actual provider schemas for binary discovery."""
import copy
import json
import sys
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as B
import repository
import binary_discovery as binary
import binary_model
import llm_client as llm
from level_validation import validate_and_solve, minimum_pushes
import test_sessions as legacy
import test_wire_contracts as wire_tests
from test_wire_contracts import assert_schema


ROWS = list(B.SAMPLE_ROWS)
ROWS[2] = "#.@.....@..#"


def step():
    return {"action": "ask", "question": "Which area should get more room?", "note": "Keep the other side intact.",
        "initialRequirements": {"requirements": [], "automaticBindings": [], "exactTransitions": []},
        "branches": [{"label": label, "strategy": {"effect": "open_route", "focus": {"row": 3, "column": col, "radius": 1},
            "operators": ["remove_water"], "preserve": ["outer_shell", "player", "boxes", "targets"],
            "editBudget": 2, "metricGoals": []}, "promise": {"kind": "reduce_obstacles", "component": "water",
                "focus": {"row": 3, "column": col, "radius": 1}, "corridor": None}}
            for label, col in [("Open the left side!", 3), ("Open the right side!", 9)]]}


def topic():
    return {"binding": "fixture", "choices": [], "initialRequest": "Make it simpler.", "questionCount": 0, "understanding": {},
        "userTurns": [{"id": "initial", "content": "Make it simpler."}]}


def validate(rows, deadline):
    llm.validate_ai_edit_diff(ROWS, rows)
    return validate_and_solve(rows, deadline=deadline)


class BinaryProofTests(unittest.TestCase):
    def test_both_candidates_really_remove_obstacles_and_remain_solvable(self):
        caches = binary.preflight(binary.parse_step(step(), 0), topic(), ROWS, {}, validate, time.monotonic() + 116)
        self.assertEqual(len(caches), 2)
        self.assertNotEqual(caches[0]["rows"], caches[1]["rows"])
        for cache in caches:
            self.assertTrue(all(r["passed"] for r in cache["promiseResults"]))
            self.assertLess(sum(r.count("@") for r in cache["rows"]), 2)

    def test_solvable_and_operation_name_do_not_prove_reduction(self):
        moved = list(ROWS)
        moved[2] = "#..@....@..#"
        validate_and_solve(moved)
        with self.assertRaises(binary.BinaryError):
            binary.check_promises(ROWS, moved, [step()["branches"][0]["promise"]])

    def test_cumulative_promises_cannot_be_undone(self):
        right_only = list(ROWS)
        right_only[2] = "#.@........#"
        with self.assertRaises(binary.BinaryError):
            binary.check_promises(ROWS, right_only, [b["promise"] for b in step()["branches"]])

    def test_widening_requires_the_named_corridor(self):
        rows = list(ROWS)
        rows[3] = "#...#.#....#"
        rows[4] = "#...#p#....#"
        rows[5] = "#...#s#t...#"
        promise = {"kind": "widen_corridor", "component": "internal_walls", "focus": None,
                   "corridor": {"row": 5, "column": 6, "axis": "horizontal"}}
        unrelated = list(rows); unrelated[2] = "#.......@..#"
        with self.assertRaises(binary.BinaryError): binary.check_promises(rows, unrelated, [promise])
        widened = list(rows); widened[4] = "#....p#....#"
        self.assertTrue(binary.check_promises(rows, widened, [promise])[0]["passed"])

    def test_shorter_solution_uses_actual_solver_metrics(self):
        promise = {"kind": "shorter_solution", "component": "obstacles", "focus": None, "corridor": None}
        baseline = validate_and_solve(ROWS)
        with self.assertRaises(binary.BinaryError): binary.check_promises(ROWS, ROWS, [promise], baseline, baseline)
        with self.assertRaises(binary.BinaryError): binary.check_promises(ROWS, ROWS, [promise])

    def test_reshape_preserves_count_but_changes_positions(self):
        promise = {"kind": "reshape_water", "component": "water", "focus": None, "corridor": None}
        moved = list(ROWS); moved[2] = "#..@....@..#"
        self.assertTrue(binary.check_promises(ROWS, moved, [promise])[0]["passed"])
        with self.assertRaises(binary.BinaryError): binary.check_promises(ROWS, ROWS, [promise])

    def test_deadline_reaches_both_solver_loops(self):
        for solver in (validate_and_solve, minimum_pushes):
            with self.assertRaises(TimeoutError): solver(ROWS, deadline=time.monotonic() - 1)
        with self.assertRaises(binary.BinaryError):
            binary.preflight(step(), topic(), ROWS, {}, validate, time.monotonic() + 19)

    def test_schema_counts_and_protected_operators(self):
        assert_schema(step(), binary.response_format()["json_schema"]["schema"])
        with self.assertRaises(binary.BinaryError): binary.parse_step(step(), 3)
        duplicate = step(); duplicate["branches"][1] = copy.deepcopy(duplicate["branches"][0])
        with self.assertRaises(binary.BinaryError): binary.parse_step(duplicate, 0)
        missing = step(); missing["branches"].pop()
        with self.assertRaises(binary.BinaryError): binary.parse_step(missing, 0)

    def test_missing_choice_compilation_and_hidden_locks_rejected(self):
        choice = {"sourceTurnId": "click", "promise": step()["branches"][0]["promise"]}
        with self.assertRaisesRegex(ValueError, "COVERAGE"):
            binary.require_choice_coverage({"requirements": []}, [choice])
        valid = {"requirements": [{"sourceTurnId": "click", "strength": "goal", "value": None,
            "component": "water", "property": "count", "relation": "decrease"}]}
        binary.require_choice_coverage(valid, [choice])
        valid["requirements"][0]["value"] = 3
        with self.assertRaises(ValueError): binary.require_choice_coverage(valid, [choice])


class BinaryWireTests(unittest.TestCase):
    def test_real_client_generation_and_review_formats(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        def handler(request, number):
            return step() if number == 1 else {"accepted": True, "issues": []}
        with wire.provider(handler):
            result, caches = binary_model.generate(topic(), context, validate, None, "binary-wire", time.monotonic() + 116)
        self.assertEqual(result["action"], "ask")
        self.assertEqual(len(caches), 2)
        self.assertEqual(len(wire.calls), 2)
        self.assertEqual(wire.calls[0]["response_format"]["json_schema"]["name"], "cocreation_binary_discovery")
        self.assertEqual(wire.calls[1]["response_format"]["json_schema"]["name"], "cocreation_binary_review")

    def test_missing_component_is_patched_without_rewriting_question(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        from patch_fixtures import patch_fixture
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        def handler(request, number):
            if number == 1:
                bad = step(); del bad["branches"][1]["promise"]
                return (bad, "stop")
            if number == 2:
                specification = json.loads(request["messages"][-1]["content"])
                self.assertEqual(specification["targets"], ["branch-2"])
                self.assertEqual(specification["frozenCandidate"]["question"], step()["question"])
                return patch_fixture(specification, step())
            return {"accepted": True, "issues": []}
        with wire.provider(handler):
            result, _ = binary_model.generate(topic(), context, validate, None, "binary-patch", time.monotonic() + 116)
        self.assertEqual(result["question"], step()["question"])
        self.assertEqual(len(wire.calls), 3)

    def test_true_semantic_rejection_cannot_be_repaired_into_acceptance(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        with wire.provider(lambda call, number: step() if number == 1 else {"accepted": False, "issues": ["Meaning differs"]}):
            with self.assertRaises(llm.LLMServiceError) as caught:
                binary_model.generate(topic(), context, validate, None, "binary-reject", time.monotonic() + 116)
        self.assertEqual(caught.exception.code, "BINARY_SEMANTIC_REJECTED")
        self.assertEqual(len(wire.calls), 2)

    def test_translation_preserves_whole_group_and_ids(self):
        group = {"questionId": "q", "topicId": "topic", "baseVersionId": "v", "question": "Which side?", "note": "",
            "number": 1, "choices": [{"choiceId": "a", "letter": "A", "label": "Open left"},
                {"choiceId": "b", "letter": "B", "label": "Open right"}], "status": "pending", "selectedChoiceId": None}
        source = {"turnId": "t", "body": "Which side?", "followUpQuestion": None, "intentHypothesis": None,
            "proposalOfferSummary": None, "proposalOfferRationale": None, "proposalSummary": None, "uiCueTexts": [],
            "proposalQuestion": group, "proposalVerifiedActions": ["Open left"], "stageRows": ROWS}
        translated = {k: copy.deepcopy(v) for k, v in source.items() if k != "stageRows"}
        result = llm.validate_translation_response({"translations": [translated]}, [source], "en")
        self.assertEqual(result[0]["proposalQuestion"], group)
        self.assertEqual(result[0]["proposalVerifiedActions"], ["Open left"])
        translated["proposalQuestion"]["choices"][0]["choiceId"] = "invented"
        with self.assertRaises(ValueError): llm.validate_translation_response({"translations": [translated]}, [source], "en")

    def test_locked_record_and_ready_path_use_empty_plan_protocol(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        current = topic(); current["requirements"] = step()["initialRequirements"]
        current["choices"] = [{"question": "Which side?", "note": "", "label": "Open left",
            "promise": step()["branches"][0]["promise"]}]
        current["selectedCandidate"] = {"promiseResults": [{"passed": True, "kind": "reduce_obstacles", "before": 2, "after": 1}]}
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        plan = {"action": "plan", "question": "", "note": "", "branches": [], "initialRequirements": None}
        with wire.provider(lambda call, number: plan if number == 1 else {"accepted": True, "issues": []}):
            result, candidates = binary_model.generate(current, context, validate, None, "binary-ready", time.monotonic()+116)
        self.assertEqual(result["action"], "plan")
        self.assertEqual(candidates, [])
        self.assertEqual(len(wire.calls), 2)
        review = json.loads(wire.calls[1]["messages"][1]["content"])
        self.assertEqual(review["step"]["branches"], [])
        self.assertTrue(review["evidence"]["topic"]["selectedFeasibility"]["allSelectedActionsPassed"])


class BinaryHTTPTests(unittest.TestCase):
    setUpClass = classmethod(legacy.CoCreationSessionTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.CoCreationSessionTests.tearDownClass.__func__)
    tearDown = legacy.CoCreationSessionTests.tearDown
    create_and_open_session = legacy.CoCreationSessionTests.create_and_open_session
    _fake_turn_understanding = staticmethod(legacy.CoCreationSessionTests._fake_turn_understanding)

    def setUp(self):
        legacy.CoCreationSessionTests.setUp(self)
        self.version = self.client.get(f"/api/sessions/{self.session_id}").json()["currentVersionId"]
        # Existing session fixture is an open map; add two legal blocked cells to its immutable test Stage.
        with repository.connect(immediate=True) as db:
            db.execute("UPDATE level_versions SET rows_json=?, validation_json=? WHERE id=?",
                (json.dumps(ROWS), json.dumps(validate_and_solve(ROWS).as_dict()), self.version))
        self.requirements = patch.object(llm, "_review_proposal_requirements", return_value={
            "requirements": [], "automaticBindings": [], "exactTransitions": []})
        self.requirements.start(); self.addCleanup(self.requirements.stop)
        self.start_patch = patch("binary_routes.should_start", return_value=True)
        self.start_patch.start(); self.addCleanup(self.start_patch.stop)
        self.model = patch.object(binary_model, "generate", side_effect=self.generate)
        self.model.start(); self.addCleanup(self.model.stop)

    def generate(self, current, context, validator, strategy_validator, request_id, deadline, budget=20):
        result = step()
        context["stageContext"]["requirementRecord"] = current.get("requirements") or result["initialRequirements"]
        if current.get("requirements") is not None: result["initialRequirements"] = None
        caches = binary.preflight(result, current, context["rows"], context["stageContext"]["stageSnapshot"],
            validator, deadline, strategy_validator, budget)
        return result, caches

    def start(self):
        response = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
            "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "binary-start"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def choice(self, state, key="binary-click"):
        question = state["proposalFlowState"]["pendingQuestion"]
        return {"baseVersionId": self.version, "topicId": question["topicId"], "questionId": question["questionId"],
                "choiceId": question["choices"][0]["choiceId"], "displayLanguage": "en", "idempotencyKey": key}

    def test_question_projection_free_text_rejection_cancel_and_start_receipt(self):
        state = self.start()
        self.assertEqual(state["proposalFlowState"]["clarificationQuestionCount"], 1)
        self.assertNotIn('"candidate"', json.dumps(state))
        replay = self.start()
        self.assertEqual(len(state["turns"]), len(replay["turns"]))
        free = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
            "content": "A", "idempotencyKey": "illegal-text"})
        self.assertEqual(free.status_code, 409, free.text)
        result = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/cancel", json={
            "baseVersionId": self.version, "topicId": state["proposalFlowState"]["topicId"], "idempotencyKey": "cancel"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["proposalFlowState"]["active"])
        self.assertEqual(result.json()["currentVersionId"], self.version)

    def test_failed_continuation_keeps_click_exactly_once_and_rejects_old_buttons(self):
        state = self.start(); payload = self.choice(state)
        failure = llm.LLMServiceError("BINARY_PREFLIGHT_NOT_FOUND", "Retry", "fixture", True, 1, 502)
        with patch.object(binary_model, "generate", side_effect=failure):
            for _ in range(2):
                response = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/choices", json=payload)
                self.assertEqual(response.status_code, 502, response.text)
        latest = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertEqual(latest["proposalFlowState"]["status"], "retry_pending")
        self.assertEqual(latest["proposalFlowState"]["clarificationQuestionCount"], 1)
        self.assertEqual(sum(t["requestId"] == "binary-click" for t in latest["turns"]), 1)
        payload["idempotencyKey"] = "second-tab"
        stale = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/choices", json=payload)
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_final_purple_freezes_selected_candidate_and_preview_replays_it(self):
        state = self.start(); payload = self.choice(state)
        def planner(**kwargs):
            stage = kwargs["stage_context"]
            with repository.connect() as db:
                current = binary.current_topic(db, self.session_id, self.version)
            cache = current["selectedCandidate"]
            from proposal_search import parse_revision_plan
            plan = parse_revision_plan({"strategies": [cache["strategy"]]})
            contract = llm._build_revision_execution_contract(plan, current["initialRequest"], stage)
            import binary_routes
            return llm.LLMExecutionResult("", 1, "fixture-final", model="offline",
                proposed_rows=cache["rows"], revision_contract=contract, revision_plan=plan.as_dict(),
                revision_operations=binary_routes._operations(ROWS, cache["rows"]), proposal_diagnostics={"selectedStrategyIndex": 1})
        with patch.object(binary_model, "generate", return_value=({"action": "plan", "question": "", "note": "", "branches": []}, [])), \
                patch.object(llm, "_generate_revision_search_proposal_sync", side_effect=planner):
            response = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/choices", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        self.assertEqual(state["proposalFlowState"]["status"], "proposal_ready")
        offer_turn = state["turns"][-1]
        offer = offer_turn["guidance"]["proposalOffer"]
        self.assertTrue(offer["proposalPresentation"]["binaryCompact"])
        self.assertEqual(len(offer["proposalPresentation"]["verifiedActions"]), 1)
        response = self.client.post(f"/api/sessions/{self.session_id}/messages", json={
            "baseVersionId": self.version, "content": "Preview the verified proposal.", "idempotencyKey": "binary-preview",
            "action": "execute_revision", "sourceTurnId": offer_turn["turnId"]})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["currentVersionId"], self.version)
        self.assertEqual(len(preview["proposals"]), 1)
        proposal = preview["proposals"][0]
        source = next(t for t in preview["turns"] if t["turnId"] == offer_turn["turnId"])
        self.assertTrue(source["guidance"]["proposalOffer"]["proposalPresentation"]["binaryCompact"])
        adopted = self.client.post(f"/api/sessions/{self.session_id}/proposals/{proposal['proposalId']}/decision", json={
            "baseVersionId": self.version, "decision": "accept", "idempotencyKey": "binary-accept"})
        self.assertEqual(adopted.status_code, 200, adopted.text)
        self.assertNotEqual(adopted.json()["currentVersionId"], self.version)
        self.assertEqual(len(adopted.json()["versions"]), 2)

    def test_cancelled_failed_request_cannot_be_resumed_with_old_key(self):
        with patch.object(binary_model, "generate", side_effect=llm.LLMServiceError("BINARY_PREFLIGHT_NOT_FOUND", "Retry", "x", True, 1, 502)):
            failed = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
                "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "binary-start"})
        self.assertEqual(failed.status_code, 502)
        latest = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/cancel", json={
            "baseVersionId": self.version, "topicId": latest["proposalFlowState"]["topicId"], "idempotencyKey": "cancel-failed"})
        replay = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
            "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "binary-start"})
        self.assertEqual(replay.status_code, 409, replay.text)

    def test_unpublished_question_does_not_consume_count_after_commit_failure(self):
        original = B.insert_turn
        def fail_assistant(database, session, role, *args, **kwargs):
            if role == "assistant": raise RuntimeError("injected commit failure")
            return original(database, session, role, *args, **kwargs)
        with patch.object(B, "insert_turn", side_effect=fail_assistant):
            response = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
                "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "binary-start"})
        self.assertEqual(response.status_code, 502, response.text)
        state = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertEqual(state["proposalFlowState"]["clarificationQuestionCount"], 0)
        self.assertIsNone(state["proposalFlowState"]["pendingQuestion"])
        retried = self.start()
        self.assertEqual(retried["proposalFlowState"]["clarificationQuestionCount"], 1)

    def test_tampered_cached_proof_rolls_back_the_choice_turn(self):
        state = self.start(); payload = self.choice(state)
        with repository.connect(immediate=True) as db:
            current = binary.current_topic(db, self.session_id, self.version)
            current["pendingQuestion"]["choices"][0]["candidate"]["rows"] = list(ROWS)
            repository.record_event(db, self.session_id, "binary_discovery_state", current, B.utc_now())
        response = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/choices", json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        latest = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertFalse(any(t["requestId"] == payload["idempotencyKey"] for t in latest["turns"]))
        self.assertEqual(latest["proposalFlowState"]["clarificationQuestionCount"], 1)
