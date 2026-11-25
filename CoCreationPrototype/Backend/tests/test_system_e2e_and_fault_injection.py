"""System E2E, cross-service sync, negative, concurrency, and fault-injection tests for 8010."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = Path(__file__).resolve().parents[3]
TESTS_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from cocreation.api import config_models, routes_messages_and_proposals  # noqa: E402
from cocreation.api.app_factory import create_app  # noqa: E402
from cocreation.api.config_models import ApiError, SAMPLE_ROWS  # noqa: E402
from cocreation.api.context_and_discovery import _proposal_feasibility_profile  # noqa: E402
from cocreation.api.services import CoCreationSettings, HttpOnlineSyncGateway, build_default_services  # noqa: E402
from cocreation.domain.design_context import empty_design_context, set_active_disagreement  # noqa: E402
from cocreation.domain.design_requirements import validate_requirement_record  # noqa: E402
from cocreation.domain.level_validation import LevelValidationError, validate_and_solve, validate_rows  # noqa: E402
from cocreation.domain.proposal_options import parse_envelope  # noqa: E402
from cocreation.domain.proposal_search import parse_revision_plan, search_revision_plan  # noqa: E402
from cocreation.llm import facade as llm  # noqa: E402
from cocreation.llm import intent_and_review, translations  # noqa: E402
from cocreation.llm.config_and_runtime import LLMExecutionResult  # noqa: E402
from cocreation.persistence import repository  # noqa: E402
from cocreation.reliability import reliability_audit  # noqa: E402
from cocreation.tools import semantic_eval_runner  # noqa: E402
from tests.fakes import FakeClock, RecordedCall, SessionTestCase  # noqa: E402


def _load_8000_create_app():
    backend8000 = ROOT_DIR / "Backend"
    added = False
    if str(backend8000) not in sys.path:
        sys.path.append(str(backend8000))
        added = True
    try:
        spec = importlib.util.spec_from_file_location("sokoban8000_app_sys_e2e", backend8000 / "app.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        from sokoban_backend.dependencies import BackendSettings, StudyLogPaths, build_container  # noqa: E402
        return module.create_app, BackendSettings, StudyLogPaths, build_container
    finally:
        if added and str(backend8000) in sys.path:
            sys.path.remove(str(backend8000))


class System8010Tests(SessionTestCase):
    def setUp(self):
        self.open_workspace()

    def _create_online_session(self, key="sys-sess-1", *, match_id="a" * 32, player_number=1):
        created = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "description_generation",
                "language": "en",
                "idempotencyKey": key,
                "matchId": match_id,
                "playerNumber": player_number,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        sid = body["sessionId"]
        tok = body["integrationToken"]
        bootstrap = parse_qs(urlparse(body["launchUrl"]).fragment)["bootstrap"][0]
        acc = self.client.post(f"/api/sessions/{sid}/browser-access", json={"bootstrapToken": bootstrap})
        self.assertEqual(acc.status_code, 200, acc.text)
        lang = self.client.patch(f"/api/sessions/{sid}/language", json={"language": "en"})
        self.assertEqual(lang.status_code, 200, lang.text)
        return sid, tok, lang.json()["currentVersionId"]

    def test_cross_service_8000_8010_real_wire_e2e_with_fault_injection(self):
        create_8000_app, Backend8000Settings, StudyLogPaths, build_8000_container = _load_8000_create_app()
        outage = {"fail_intention": False}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            container8000 = build_8000_container(
                settings=Backend8000Settings(
                    dashboard_delete_password="pw",
                    cocreation_intention_sync_secret="sync-secret",
                ),
                paths=StudyLogPaths(tmp_root / "logs"),
            )
            app8000 = create_8000_app(container8000)
            client8000 = TestClient(app8000)
            p1 = client8000.post("/online/rooms", json={"studySessionId": "st1", "menuStartedFlow": True}).json()
            p2 = client8000.post(
                "/online/rooms/join",
                json={"roomCode": p1["roomCode"], "studySessionId": "st2", "menuStartedFlow": True},
            ).json()
            h1 = {"X-Player-Token": p1["playerToken"]}
            h2 = {"X-Player-Token": p2["playerToken"]}
            client8000.post(f"/online/rooms/{p1['matchId']}/ready", headers=h1, json={"ready": True})
            client8000.post(f"/online/rooms/{p1['matchId']}/ready", headers=h2, json={"ready": True})
            client8000.post(
                f"/online/rooms/{p1['matchId']}/draft",
                headers=h1,
                json={"finalDifficulty": "Medium", "finalLayout": "Balanced"},
            )

            def transport_handler(request: httpx.Request) -> httpx.Response:
                if outage["fail_intention"] and request.url.path.endswith("/designer-intention"):
                    return httpx.Response(503, json={"detail": "Injected 8000 outage"})
                res = client8000.request(
                    request.method,
                    str(request.url).replace("http://127.0.0.1:8000", ""),
                    headers=dict(request.headers),
                    content=request.content,
                )
                return httpx.Response(res.status_code, content=res.content, headers=dict(res.headers))

            settings8010 = CoCreationSettings(
                database_path=tmp_root / "cocreation.sqlite3",
                public_base_url="http://127.0.0.1:8010",
                online_backend_base_url="http://127.0.0.1:8000",
                intention_sync_secret="sync-secret",
                token_secret="e2e-secret",
                sweeper_enabled=False,
            )
            services = build_default_services(
                settings=settings8010,
                online_sync=HttpOnlineSyncGateway(
                    settings8010,
                    http_client_factory=lambda **kw: httpx.Client(
                        transport=httpx.MockTransport(transport_handler),
                        base_url="http://127.0.0.1:8000",
                        **kw,
                    ),
                ),
            )
            with TestClient(create_app(services)) as c8010:
                created = c8010.post(
                    "/api/sessions",
                    json={
                        "rows": SAMPLE_ROWS,
                        "initialDraftMethod": "description_generation",
                        "language": "en",
                        "idempotencyKey": "e2e-create",
                        "matchId": p1["matchId"],
                        "playerNumber": 1,
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                sid = created.json()["sessionId"]
                tok = created.json()["integrationToken"]
                boot = parse_qs(urlparse(created.json()["launchUrl"]).fragment)["bootstrap"][0]
                c8010.post(f"/api/sessions/{sid}/browser-access", json={"bootstrapToken": boot})
                lang = c8010.patch(f"/api/sessions/{sid}/language", json={"language": "en"})
                self.assertEqual(lang.status_code, 200)
                v1 = lang.json()["currentVersionId"]

                fin = c8010.post(f"/api/sessions/{sid}/finalize", json={"baseVersionId": v1, "idempotencyKey": "e2e-fin"})
                self.assertEqual(fin.status_code, 200)

                # Fault injection: 8000 down during intention sync
                outage["fail_intention"] = True
                failed_int = c8010.post(
                    f"/api/sessions/{sid}/intention",
                    json={"content": "Make first box clear", "idempotencyKey": "intent-1"},
                )
                self.assertEqual(failed_int.status_code, 503)

                # CRITICAL INVARIANT: BOTH browser GET and integration GET must still be awaiting_intention
                browser_view = c8010.get(f"/api/sessions/{sid}").json()
                unity_view = c8010.get(
                    f"/api/integrations/sessions/{sid}",
                    headers={"Authorization": f"Bearer {tok}"},
                ).json()
                self.assertEqual(browser_view["status"], "awaiting_intention")
                self.assertEqual(unity_view["status"], "awaiting_intention")
                self.assertEqual(browser_view["intention"]["content"], "Make first box clear")

                # Recover 8000 and retry intention (fresh idempotencyKey allowed while unsynced)
                outage["fail_intention"] = False
                ok_int = c8010.post(
                    f"/api/sessions/{sid}/intention",
                    json={"content": "Make first box clear", "idempotencyKey": "intent-2"},
                )
                self.assertEqual(ok_int.status_code, 200)
                self.assertEqual(ok_int.json()["status"], "completed")
                unity_done = c8010.get(
                    f"/api/integrations/sessions/{sid}",
                    headers={"Authorization": f"Bearer {tok}"},
                ).json()
                self.assertEqual(unity_done["status"], "completed")

            # Complete challenges & results on 8000 and check dashboard projection
            client8000.post(f"/online/rooms/{p1['matchId']}/challenge", headers=h1, json={"rows": SAMPLE_ROWS})
            client8000.post(f"/online/rooms/{p1['matchId']}/challenge", headers=h2, json={"rows": SAMPLE_ROWS})
            client8000.post(
                f"/online/rooms/{p1['matchId']}/result",
                headers=h1,
                json={"durationSeconds": 10.0, "moveCount": 5, "minimumMoves": 4, "restartCount": 0, "outcome": "completed"},
            )
            client8000.post(
                f"/online/rooms/{p1['matchId']}/result",
                headers=h2,
                json={"durationSeconds": 30.0, "moveCount": 8, "minimumMoves": 4, "restartCount": 2, "outcome": "timed_out"},
            )
            records = client8000.get("/matchmaking-records-data").json()
            self.assertEqual(len(records["matches"]), 1)
            flow_types = [ev["eventType"] for ev in records["matches"][0]["players"][0]["coCreationFlow"]]
            self.assertEqual(flow_types, ["draft", "first_stage", "final", "message"])

    def test_accepting_second_proposal_supersedes_orphaned_pending_proposal(self):
        sid, _, v1 = self._create_online_session("orphan-prop-1")
        rows_a = list(SAMPLE_ROWS)
        rows_a[2] = "#..@.......#"
        rows_b = list(SAMPLE_ROWS)
        rows_b[2] = "#...@......#"
        with self.services.connect(immediate=True) as db:
            sess = repository.get_session(db, sid)
            t1 = self.services.insert_turn(db, sess, "assistant", "Prop A", v1, "m-a", None)
            t2 = self.services.insert_turn(db, sess, "assistant", "Prop B", v1, "m-b", None)
            val_a = validate_and_solve(rows_a).as_dict()
            val_b = validate_and_solve(rows_b).as_dict()
            now = self.services.utc_now()
            for pid, tid, rows, val in (("prop-a", t1, rows_a, val_a), ("prop-b", t2, rows_b, val_b)):
                db.execute(
                    "INSERT INTO change_proposals(id, session_id, base_version_id, assistant_turn_id, idempotency_key, proposed_rows_json, summary, diff_json, validation_json, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (pid, sid, v1, tid, f"key-{pid}", json.dumps(rows), f"Summary {pid}", "[]", json.dumps(val), now),
                )
        dec = self.client.post(
            f"/api/sessions/{sid}/proposals/prop-b/decision",
            json={"decision": "accept", "baseVersionId": v1, "idempotencyKey": "dec-b"},
        )
        self.assertEqual(dec.status_code, 200, dec.text)
        v2 = dec.json()["currentVersionId"]
        fin = self.client.post(f"/api/sessions/{sid}/finalize", json={"baseVersionId": v2, "idempotencyKey": "fin-v2"})
        self.assertEqual(fin.status_code, 200, fin.text)

    def test_play_metrics_overflow_bool_coercion_and_interior_void_validation(self):
        # Boolean playerNumber rejected at schema validation
        bad_create = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": "bool-p",
                "matchId": "m-bool",
                "playerNumber": True,
            },
        )
        self.assertEqual(bad_create.status_code, 400)
        self.assertEqual(bad_create.json()["code"], "INVALID_REQUEST")

        sid, tok, v1 = self._create_online_session("metrics-test-1")
        att = self.client.post(
            f"/api/sessions/{sid}/versions/{v1}/play-attempts",
            json={"idempotencyKey": "play-att-1"},
        ).json()
        play_ticket = parse_qs(urlparse(att["playUrl"]).query)["cocreationPlay"][0]
        boot = self.client.post(
            f"/api/play-attempts/{att['attemptId']}/bootstrap",
            json={"ticket": play_ticket},
        )
        self.assertEqual(boot.status_code, 200, boot.text)
        attempt_token = boot.json()["attemptToken"]
        # Huge minimumMoves / minimumPushes must return 400 (never 500 OverflowError)
        for bad_payload in (
            {"attemptToken": attempt_token, "minimumMoves": 10**20},
            {"attemptToken": attempt_token, "minimumPushes": -(10**20)},
            {"attemptToken": attempt_token, "durationSeconds": True},
        ):
            r = self.client.post(f"/api/play-attempts/{att['attemptId']}/progress", json=bad_payload)
            self.assertEqual(r.status_code, 400)
            self.assertIn(r.json()["code"], ("INVALID_PLAY_METRICS", "INVALID_REQUEST"))

        # Interior void tile ' ' inside outer wall must fail with OPEN_OUTER_WALL
        interior_void = list(SAMPLE_ROWS)
        interior_void[2] = "#.. ...@...#"
        with self.assertRaises(LevelValidationError) as ctx:
            validate_rows(interior_void)
        self.assertEqual(ctx.exception.code, "OPEN_OUTER_WALL")
        with self.assertRaises(LevelValidationError) as ctx2:
            validate_and_solve(interior_void)
        self.assertEqual(ctx2.exception.code, "OPEN_OUTER_WALL")

    def test_proposal_search_four_transitions_and_bottom_right_beyond_32(self):
        # Case A: 4 required transitions (> BEAM_DEPTH=3)
        plan4 = parse_revision_plan({
            "strategies": [{
                "effect": "adjust_internal_walls",
                "focus": None,
                "operators": ["add_wall"],
                "preserve": [],
                "requiredTransitions": [
                    {"row": 3, "column": 2, "from": ".", "to": "#"},
                    {"row": 3, "column": 4, "from": ".", "to": "#"},
                    {"row": 7, "column": 2, "from": ".", "to": "#"},
                    {"row": 7, "column": 4, "from": ".", "to": "#"},
                ],
                "metricGoals": [],
                "editBudget": 4,
            }]
        })
        res4 = search_revision_plan(SAMPLE_ROWS, plan4, validate_and_solve, {"solutionSteps": 4, "solutionPushes": 1, "minimumPushes": 1})
        for r, c in ((2, 1), (2, 3), (6, 1), (6, 3)):
            self.assertEqual(res4.rows[r][c], "#")

        # Case B: bottom-right required transition at (9, 11) with focus=None (> PRIMITIVE_LIMIT=32)
        plan_br = parse_revision_plan({
            "strategies": [{
                "effect": "adjust_internal_walls",
                "focus": None,
                "operators": ["add_wall"],
                "preserve": [],
                "requiredTransitions": [{"row": 9, "column": 11, "from": ".", "to": "#"}],
                "metricGoals": [],
                "editBudget": 1,
            }]
        })
        res_br = search_revision_plan(SAMPLE_ROWS, plan_br, validate_and_solve, {"solutionSteps": 4, "solutionPushes": 1, "minimumPushes": 1})
        self.assertEqual(res_br.rows[8][10], "#")

    def test_feasibility_profile_constraints_binary_cancel_and_preflight_disagreement(self):
        snap = {"mapFingerprint": "fp", "identityStatus": "exact", "rows": SAMPLE_ROWS, "entities": []}
        dc = {
            **empty_design_context(),
            "designConstraints": [
                {"id": "c1", "constraint": "Keep all water intact", "authority": "confirmed", "status": "active"},
                {"id": "c2", "constraint": "Inferred should exclude", "authority": "inferred", "status": "active"},
            ],
        }
        profile = _proposal_feasibility_profile(snap, SAMPLE_ROWS, dc)
        self.assertEqual(profile["confirmedPreserveItems"], ["Keep all water intact"])

        # Sending requestProposal=True while activeDisagreement is active must fail 409 DISAGREEMENT_ACTIVE BEFORE calling LLM
        sid, _, v1 = self._create_online_session("bin-cancel-disagree")
        with self.services.connect(immediate=True) as db:
            ctx = repository.load_design_context(db, sid, v1)
            ctx = set_active_disagreement(
                ctx,
                {
                    "status": "active",
                    "subject": "human_edit",
                    "userPosition": "keep",
                    "aiPosition": "adjust",
                    "coreDisagreement": "conflict",
                    "nextQuestion": "Discuss?",
                },
                stage_id=v1,
                turn_id="t1",
            )
            repository.save_design_context(db, v1, ctx)

        mock_understand = RecordedCall(return_value={})
        with self.using_dependencies(llm_handlers={"classify_turn_understanding": mock_understand}):
            prop_res = self.client.post(
                f"/api/sessions/{sid}/messages",
                json={"content": "Add wall near B1", "baseVersionId": v1, "idempotencyKey": "m-prop-dis", "requestProposal": True},
            )
        self.assertEqual(prop_res.status_code, 409)
        self.assertEqual(prop_res.json()["code"], "DISAGREEMENT_ACTIVE")
        self.assertEqual(mock_understand.call_count, 0)

        # binary cancel succeeds even while activeDisagreement is set
        with self.services.connect(immediate=True) as db:
            topic = {
                "discoveryPolicyVersion": 2,
                "topicId": "topic-dis",
                "versionId": v1,
                "initialMessageKey": "m0",
                "initialRequest": "Add a wall",
                "understanding": {},
                "userTurns": [],
                "choices": [],
                "questionCount": 1,
                "language": "en",
                "pendingQuestion": None,
                "status": "clarifying",
            }
            self.services.record_event(db, sid, "binary_discovery_state", topic, self.services.utc_now())

        # Cancel binary topic succeeds even while activeDisagreement is active
        cancel_res = self.client.post(
            f"/api/sessions/{sid}/proposal-discovery/cancel",
            json={"baseVersionId": v1, "topicId": "topic-dis", "idempotencyKey": "can-dis-1"},
        )
        self.assertEqual(cancel_res.status_code, 200)
        self.assertEqual(cancel_res.json()["proposalFlowState"]["status"], "cancelled")

    def test_intent_feedback_guards_and_request_lock_isolation(self):
        sid, _, v1 = self._create_online_session("lock-intent-1")
        with self.services.connect(immediate=True) as db:
            sess = repository.get_session(db, sid)
            self.services.insert_turn(db, sess, "user", "Some msg", v1, "turn-key-1", None)
            t_card = self.services.insert_turn(
                db, sess, "assistant", "Maybe reduce water?", v1, "turn-key-card",
                LLMExecutionResult("Maybe reduce water?", 1, "turn-key-card", model="kimi-k2.6", guidance={"move": "clarify_intent", "intentHypothesis": "Reduce water", "intentConfidence": "medium", "followUpQuestion": None, "proposalOffer": None, "disagreement": None, "uiCues": [], "_intentHypothesisId": "h-new"}),
            )
            ctx = repository.load_design_context(db, sid, v1)
            ctx["intentHypotheses"] = [
                {"id": "h-old", "statement": "Keep water large", "status": "confirmed", "topicKey": "water", "sourceStageId": v1},
                {"id": "h-new", "statement": "Reduce water footprint", "status": "tentative", "displayed": True, "topicKey": "water", "sourceStageId": v1},
            ]
            repository.save_design_context(db, v1, ctx)

        # Reusing conversation_turns idempotencyKey in intent feedback must fail 409
        reused = self.client.post(
            f"/api/sessions/{sid}/intent-hypotheses/h-new/feedback",
            json={"action": "confirm", "sourceTurnId": t_card, "baseVersionId": v1, "idempotencyKey": "turn-key-1"},
        )
        self.assertEqual(reused.status_code, 409)

        # Concurrent supersession of h-old while LLM review runs triggers 409 INTENT_CONTEXT_CHANGED
        def mutating_review(*_args, **_kwargs):
            with self.services.connect(immediate=True) as inner_db:
                inner_ctx = repository.load_design_context(inner_db, sid, v1)
                inner_ctx["intentHypotheses"][0]["status"] = "rejected"
                repository.save_design_context(inner_db, v1, inner_ctx)
            return {
                "verdict": "conflict",
                "explanation": "Conflicts with old preference",
                "conflictingHypothesisIds": ["h-old"],
                "supersedesHypothesisIds": [],
            }

        with self.using_dependencies(llm_handlers={"review_intent_feedback": RecordedCall(side_effect=mutating_review)}):
            cas_err = self.client.post(
                f"/api/sessions/{sid}/intent-hypotheses/h-new/feedback",
                json={"action": "confirm", "sourceTurnId": t_card, "baseVersionId": v1, "idempotencyKey": "intent-cas-1"},
            )
        self.assertEqual(cas_err.status_code, 409)
        self.assertEqual(cas_err.json()["code"], "INTENT_CONTEXT_CHANGED")

        # message_request_lock: subsequent request after leader error does NOT inherit stale error; different fingerprint skips leader error
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def leader():
            try:
                with routes_messages_and_proposals.message_request_lock(sid, "k-lock", scope="msg", request_signature={"c": 1}, services=self.services):
                    entered.set()
                    release.wait(timeout=2)
                    raise ApiError(502, "LEADER_ERR", "fail", retryable=True)
            except ApiError:
                pass

        t = threading.Thread(target=leader)
        t.start()
        self.assertTrue(entered.wait(timeout=2))
        release.set()
        t.join(timeout=2)
        with routes_messages_and_proposals.message_request_lock(sid, "k-lock", scope="msg", request_signature={"c": 1}, services=self.services):
            errors.append("ok")
        self.assertEqual(errors, ["ok"])

    def test_prompts_domain_guards_and_offline_tools_resilience(self):
        # Translations patch request includes lockedTranslatedBody & system prompt requires exact substring
        items = [{
            "turnId": "t1",
            "body": "Move B1 from (5,5) to (5,7).",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "proposalSummary": None,
            "uiCueTexts": [],
            "coordinateLinks": [{"text": "from (5,5) to (5,7)", "from": {"row": 5, "column": 5}, "to": {"row": 5, "column": 7}}],
        }]
        bad_payload = {
            "translations": [{
                "turnId": "t1",
                "body": "将 B1 从 (5,5) 移到 (5,7)。",
                "followUpQuestion": None,
                "intentHypothesis": None,
                "proposalOfferSummary": None,
                "proposalOfferRationale": None,
                "proposalSummary": None,
                "uiCueTexts": [],
                "coordinateLinkTexts": ["(5,5) 到 (5,7)"],
            }]
        }
        patch, _ = translations._translation_patch_request(bad_payload, items, "zh-CN", {})
        self.assertIsNotNone(patch)
        self.assertEqual(patch.evidence["turn:t1"]["lockedTranslatedBody"], "将 B1 从 (5,5) 移到 (5,7)。")

        # proposal_options rejects numeric alias self-comparison ["1", opt_id]
        snapshot = {"rows": SAMPLE_ROWS, "entities": [{"id": "B1", "kind": "box", "row": 6, "column": 5}, {"id": "T1", "kind": "target", "row": 6, "column": 7}]}
        valid_opt = lambda oid, label, focus, mech: {
            "id": oid, "label": label, "summary": "Adjust water.", "focus": focus,
            "mechanism": mech, "effect": "Helps readability.", "tradeoff": "Minimal risk.", "preserve": "Keep entities.",
        }
        with self.assertRaises(Exception):
            parse_envelope(
                {
                    "options": [valid_opt("opt-a", "A", "near B1", "widen"), valid_opt("opt-b", "B", "near T1", "open")],
                    "comparedOptions": ["1", "opt-a"],
                    "discriminant": "water placement",
                    "question": "Which?",
                },
                {},
                snapshot,
            )

        # design_requirements rejects delta=0 for increase/decrease
        with self.assertRaisesRegex(ValueError, "positive value"):
            validate_requirement_record(
                {
                    "requirements": [{
                        "strength": "goal",
                        "sourceTurnId": "u1",
                        "evidenceSpan": "减少 0 步",
                        "priorityEvidenceSpan": "",
                        "statement": "Decrease steps by 0",
                        "component": "gameplay",
                        "property": "solutionSteps",
                        "relation": "decrease",
                        "value": 0,
                        "unit": "delta",
                        "entities": [],
                        "focused": False,
                    }],
                    "automaticBindings": [],
                    "exactTransitions": [],
                },
                [{"id": "u1", "content": "减少 0 步"}],
                snapshot,
            )

        # reliability_audit handles non-dict payloads, None step strings, non-int attemptsUsed, unhashable IDs
        summary = reliability_audit.summarize([
            "not-a-dict",
            {"sessionId": ["s1"], "eventType": "reply_pipeline_diagnostic", "payload": {"requestId": ["r1"], "steps": [{"stage": None, "status": None}]}},
            {"sessionId": ["s1"], "eventType": "component_recovery", "payload": {"recoveryId": ["rec1"], "attemptsUsed": None}},
            {"sessionId": "s2", "eventType": "reply_delivery_outcome", "payload": {"messageKey": ["k"], "task": None, "details": ["bad"], "reliableBodyDelivered": True}},
        ])
        self.assertEqual(summary["logicalRequests"], 1)

        # semantic_eval_runner rejects non-dict captures and non-dict runs cleanly with ValueError
        with self.assertRaises(ValueError):
            semantic_eval_runner.score({}, [])
        suite = semantic_eval_runner.load_suite()
        with self.assertRaises(ValueError):
            semantic_eval_runner.score(suite, {"suiteHash": semantic_eval_runner.suite_hash(suite), "repeats": 1, "runs": ["bad"]})


if __name__ == "__main__":
    unittest.main()
