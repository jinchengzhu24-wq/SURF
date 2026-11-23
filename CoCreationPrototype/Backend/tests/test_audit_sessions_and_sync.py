"""Regression tests for audited 8010 session, sync, projection, and deployment fixes."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = Path(__file__).resolve().parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from cocreation.api import config_models, session_persistence_helpers  # noqa: E402
from cocreation.api.app_factory import create_app  # noqa: E402
from cocreation.api.config_models import ApiError, MessageRequest, SAMPLE_ROWS, iso_time  # noqa: E402
from cocreation.api.online_sync_and_dashboard import (  # noqa: E402
    _dashboard_node_projection_for_turn,
    _dashboard_node_status,
    _dashboard_turn_kind,
)
from cocreation.api.services import CoCreationSettings, HttpOnlineSyncGateway  # noqa: E402
from cocreation.domain.design_context import empty_design_context, set_active_disagreement  # noqa: E402
from cocreation.domain.level_validation import (  # noqa: E402
    build_entity_bindings,
    format_map_claim_correction,
    summarize_stage_changes,
    validate_and_solve,
)
from cocreation.llm.facade import LLMExecutionResult  # noqa: E402
from cocreation.persistence import repository  # noqa: E402
from cocreation.persistence.database import _public_guidance  # noqa: E402
from cocreation.persistence.session_projection import serialize_session  # noqa: E402
from tests.fakes import FakeClock, RecordedCall, SessionTestCase, create_test_client, create_test_services  # noqa: E402


EDITED_ROWS = list(SAMPLE_ROWS)
EDITED_ROWS[4] = "#..p.......#"

ALT_EDITED_ROWS = list(SAMPLE_ROWS)
ALT_EDITED_ROWS[4] = "#.p........#"


class SessionSyncAndAuditRegressionTests(SessionTestCase):
    """Covers intention sync gating, lock ordering, disagreement clearing, and projections."""

    def _create_online_session(self, key="audit-sess-001", *, match_id="match-audit-1", player_number=1):
        created = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": key,
                "matchId": match_id,
                "playerNumber": player_number,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        session_id = body["sessionId"]
        bootstrap = parse_qs(urlparse(body["launchUrl"]).fragment)["bootstrap"][0]
        access = self.client.post(
            f"/api/sessions/{session_id}/browser-access",
            json={"bootstrapToken": bootstrap},
        )
        self.assertEqual(access.status_code, 200, access.text)
        lang = self.client.patch(
            f"/api/sessions/{session_id}/language",
            json={"language": "en"},
        )
        self.assertEqual(lang.status_code, 200, lang.text)
        return session_id, body["integrationToken"], lang.json()["currentVersionId"]

    def setUp(self):
        self.clock = FakeClock()
        self.open_workspace(clock=self.clock)

    def test_online_intention_sync_gates_integration_status_and_allows_fresh_key_replay(self):
        session_id, integration_token, v1 = self._create_online_session("gate-001")
        fin = self.client.post(
            f"/api/sessions/{session_id}/finalize",
            json={"baseVersionId": v1, "idempotencyKey": "fin-gate-1"},
        )
        self.assertEqual(fin.status_code, 200, fin.text)

        sync_error = ApiError(503, "ONLINE_INTENTION_SYNC_UNAVAILABLE", "8000 offline", retryable=True)
        intention_call = RecordedCall(side_effect=[sync_error, None, None])
        with self.using_dependencies(online_handlers={"intention": intention_call}):
            failed = self.client.post(
                f"/api/sessions/{session_id}/intention",
                json={"content": "Require careful corner avoidance.", "idempotencyKey": "intent-orig-key"},
            )
            self.assertEqual(failed.status_code, 503)

            with self.services.connect() as db:
                submitted = db.execute(
                    "SELECT payload_json FROM audit_events WHERE session_id = ? AND event_type = 'intention_submitted'",
                    (session_id,),
                ).fetchone()
                synced = db.execute(
                    "SELECT id FROM audit_events WHERE session_id = ? AND event_type = 'online_intention_synced'",
                    (session_id,),
                ).fetchone()
            self.assertEqual(json.loads(submitted["payload_json"]), {"intentionSyncRequired": True})
            self.assertIsNone(synced)

            gated = self.client.get(
                f"/api/integrations/sessions/{session_id}",
                headers={"Authorization": f"Bearer {integration_token}"},
            )
            self.assertEqual(gated.status_code, 200, gated.text)
            self.assertEqual(gated.json()["status"], "awaiting_intention")
            self.assertIsNone(gated.json()["finalRows"])
            self.assertIsNone(gated.json()["designerIntention"])

            replayed = self.client.post(
                f"/api/sessions/{session_id}/intention",
                json={"content": "Require careful corner avoidance.", "idempotencyKey": "intent-fresh-retry-key"},
            )
            self.assertEqual(replayed.status_code, 200, replayed.text)
            self.assertEqual(
                intention_call.call_args.kwargs["event_id"],
                f"message:{session_id}:intent-orig-key",
            )

            ungated = self.client.get(
                f"/api/integrations/sessions/{session_id}",
                headers={"Authorization": f"Bearer {integration_token}"},
            )
            self.assertEqual(ungated.status_code, 200, ungated.text)
            self.assertEqual(ungated.json()["status"], "completed")
            self.assertEqual(ungated.json()["finalRows"], SAMPLE_ROWS)
            self.assertEqual(ungated.json()["designerIntention"], "Require careful corner avoidance.")

            with self.services.connect() as db:
                synced_row = db.execute(
                    "SELECT payload_json FROM audit_events WHERE session_id = ? AND event_type = 'online_intention_synced'",
                    (session_id,),
                ).fetchone()
            self.assertEqual(
                json.loads(synced_row["payload_json"]),
                {"idempotencyKey": "intent-fresh-retry-key"},
            )

            conflict_diff_key = self.client.post(
                f"/api/sessions/{session_id}/intention",
                json={"content": "Require careful corner avoidance.", "idempotencyKey": "intent-third-key"},
            )
            self.assertEqual(conflict_diff_key.status_code, 409)
            self.assertEqual(conflict_diff_key.json()["code"], "IDEMPOTENCY_CONFLICT")

            conflict_diff_content = self.client.post(
                f"/api/sessions/{session_id}/intention",
                json={"content": "Different intention text.", "idempotencyKey": "intent-orig-key"},
            )
            self.assertEqual(conflict_diff_content.status_code, 409)
            self.assertEqual(conflict_diff_content.json()["code"], "IDEMPOTENCY_CONFLICT")

            same_key_replay = self.client.post(
                f"/api/sessions/{session_id}/intention",
                json={"content": "Require careful corner avoidance.", "idempotencyKey": "intent-orig-key"},
            )
            self.assertEqual(same_key_replay.status_code, 200, same_key_replay.text)

    def test_legacy_intention_without_sync_flag_and_demo_intention_behave_cleanly(self):
        session_id, integration_token, v1 = self._create_online_session("legacy-gate-001")
        fin = self.client.post(
            f"/api/sessions/{session_id}/finalize",
            json={"baseVersionId": v1, "idempotencyKey": "fin-legacy-1"},
        )
        self.assertEqual(fin.status_code, 200)
        res = self.client.post(
            f"/api/sessions/{session_id}/intention",
            json={"content": "Legacy intention", "idempotencyKey": "legacy-intent-1"},
        )
        self.assertEqual(res.status_code, 200)
        with self.services.connect(immediate=True) as db:
            db.execute("DELETE FROM audit_events WHERE session_id = ? AND event_type = 'online_intention_synced'", (session_id,))
            db.execute(
                "UPDATE audit_events SET payload_json = '{}' WHERE session_id = ? AND event_type = 'intention_submitted'",
                (session_id,),
            )
        legacy_status = self.client.get(
            f"/api/integrations/sessions/{session_id}",
            headers={"Authorization": f"Bearer {integration_token}"},
        )
        self.assertEqual(legacy_status.status_code, 200)
        self.assertEqual(legacy_status.json()["status"], "completed")
        self.assertEqual(legacy_status.json()["finalRows"], SAMPLE_ROWS)
        self.assertEqual(legacy_status.json()["designerIntention"], "Legacy intention")

        demo = self.client.post("/api/demo-sessions", json={"language": "en", "idempotencyKey": "demo-int-1"}).json()
        demo_id = demo["sessionId"]
        demo_boot = parse_qs(urlparse(demo["launchUrl"]).fragment)["bootstrap"][0]
        self.client.post(f"/api/sessions/{demo_id}/browser-access", json={"bootstrapToken": demo_boot})
        demo_v1 = self.client.patch(f"/api/sessions/{demo_id}/language", json={"language": "en"}).json()["currentVersionId"]
        self.client.post(f"/api/sessions/{demo_id}/finalize", json={"baseVersionId": demo_v1, "idempotencyKey": "demo-fin-1"})
        demo_int = self.client.post(
            f"/api/sessions/{demo_id}/intention",
            json={"content": "Demo intent", "idempotencyKey": "demo-int-k1"},
        )
        self.assertEqual(demo_int.status_code, 200)
        demo_diff_key = self.client.post(
            f"/api/sessions/{demo_id}/intention",
            json={"content": "Demo intent", "idempotencyKey": "demo-int-k2"},
        )
        self.assertEqual(demo_diff_key.status_code, 409)
        self.assertEqual(demo_diff_key.json()["code"], "IDEMPOTENCY_CONFLICT")

    def test_create_manual_version_checks_auth_before_solver_and_allows_post_deadline_replay(self):
        session_id, _tok, v1 = self._create_online_session("manual-order-001")
        unauth = create_test_client(self.services)
        self.addCleanup(unauth.close)
        unsolvable_rows = [
            "############",
            "#..........#",
            "#.p...#....#",
            "#.....#....#",
            "#....s#.t..#",
            "#.....#....#",
            "#..........#",
            "#..........#",
            "#..........#",
            "############",
        ]
        denied = unauth.post(
            f"/api/sessions/{session_id}/versions",
            json={"rows": unsolvable_rows, "baseVersionId": v1, "idempotencyKey": "unauth-edit-1"},
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["code"], "SESSION_ACCESS_DENIED")

        sync_error = ApiError(503, "ONLINE_FLOW_SYNC_UNAVAILABLE", "Down", retryable=True)
        stage_sync = RecordedCall(side_effect=[sync_error, None])
        with self.using_dependencies(online_handlers={"version_event": stage_sync}):
            saved = self.client.post(
                f"/api/sessions/{session_id}/versions",
                json={"rows": EDITED_ROWS, "baseVersionId": v1, "idempotencyKey": "edit-replay-1"},
            )
            self.assertEqual(saved.status_code, 503)

            self.clock.advance(1300)
            new_edit_expired = self.client.post(
                f"/api/sessions/{session_id}/versions",
                json={"rows": ALT_EDITED_ROWS, "baseVersionId": v1, "idempotencyKey": "edit-new-expired"},
            )
            self.assertEqual(new_edit_expired.status_code, 409)
            self.assertEqual(new_edit_expired.json()["code"], "SESSION_DEADLINE_EXPIRED")

            replay_after_deadline = self.client.post(
                f"/api/sessions/{session_id}/versions",
                json={"rows": EDITED_ROWS, "baseVersionId": v1, "idempotencyKey": "edit-replay-1"},
            )
            self.assertEqual(replay_after_deadline.status_code, 200, replay_after_deadline.text)
            self.assertEqual(len(replay_after_deadline.json()["versions"]), 2)

            conflict_after_deadline = self.client.post(
                f"/api/sessions/{session_id}/versions",
                json={"rows": ALT_EDITED_ROWS, "baseVersionId": v1, "idempotencyKey": "edit-replay-1"},
            )
            self.assertEqual(conflict_after_deadline.status_code, 409)
            self.assertEqual(conflict_after_deadline.json()["code"], "IDEMPOTENCY_CONFLICT")

    def test_restore_rejects_self_restore_and_clears_disagreement_and_challenges(self):
        session_id, _tok, v1 = self._create_online_session("disagree-001")
        self_restore = self.client.post(
            f"/api/sessions/{session_id}/versions/{v1}/restore",
            json={"baseVersionId": v1, "idempotencyKey": "self-restore-1"},
        )
        self.assertEqual(self_restore.status_code, 409)
        self.assertEqual(self_restore.json()["code"], "UNCHANGED_LEVEL")

        with self.services.connect(immediate=True) as db:
            ctx = empty_design_context()
            ctx = set_active_disagreement(
                ctx,
                {
                    "challengeId": "ch-ai-1",
                    "status": "active",
                    "subject": "ai_revision_challenge",
                    "displayCard": True,
                },
                v1,
                "turn-1",
            )
            db.execute("UPDATE level_versions SET design_context_json = ? WHERE id = ?", (json.dumps(ctx), v1))
            now = self.services.utc_now()
            db.execute(
                """
                INSERT INTO revision_challenges(
                    challenge_id, session_id, base_version_id, source_proposal_turn_id,
                    challenge_turn_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("ch-ai-1", session_id, v1, "turn-1", "ch-turn-1", "choice_pending", now, now),
            )

        v2 = self.client.post(
            f"/api/sessions/{session_id}/versions",
            json={"rows": EDITED_ROWS, "baseVersionId": v1, "idempotencyKey": "manual-clear-1"},
        )
        self.assertEqual(v2.status_code, 200, v2.text)
        v2_id = v2.json()["currentVersionId"]

        with self.services.connect() as db:
            v2_ctx = repository.load_design_context(db, session_id, v2_id)
            ch_row = db.execute("SELECT status FROM revision_challenges WHERE challenge_id = 'ch-ai-1'").fetchone()
        self.assertIsNone(v2_ctx.get("activeDisagreement"))
        self.assertEqual(ch_row["status"], "superseded")

        with self.services.connect(immediate=True) as db:
            v2_ctx = set_active_disagreement(
                empty_design_context(),
                {"challengeId": "ch-ai-2", "status": "active", "subject": "ai_revision_challenge"},
                v2_id,
                "turn-2",
            )
            db.execute("UPDATE level_versions SET design_context_json = ? WHERE id = ?", (json.dumps(v2_ctx), v2_id))
            now = self.services.utc_now()
            db.execute(
                """
                INSERT INTO revision_challenges(
                    challenge_id, session_id, base_version_id, source_proposal_turn_id,
                    challenge_turn_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("ch-ai-2", session_id, v2_id, "turn-2", "ch-turn-2", "awaiting_reason", now, now),
            )

        v3 = self.client.post(
            f"/api/sessions/{session_id}/versions/{v1}/restore",
            json={"baseVersionId": v2_id, "idempotencyKey": "restore-clear-1"},
        )
        self.assertEqual(v3.status_code, 200, v3.text)
        v3_id = v3.json()["currentVersionId"]

        with self.services.connect() as db:
            v3_ctx = repository.load_design_context(db, session_id, v3_id)
            ch2_row = db.execute("SELECT status FROM revision_challenges WHERE challenge_id = 'ch-ai-2'").fetchone()
        self.assertIsNone(v3_ctx.get("activeDisagreement"))
        self.assertEqual(ch2_row["status"], "superseded")

        # Post-deadline restore idempotent replay succeeds cleanly
        self.clock.advance(1300)
        restore_replay = self.client.post(
            f"/api/sessions/{session_id}/versions/{v1}/restore",
            json={"baseVersionId": v2_id, "idempotencyKey": "restore-clear-1"},
        )
        self.assertEqual(restore_replay.status_code, 200, restore_replay.text)

    def test_assess_version_stale_check_and_post_deadline_replay_and_play_gate(self):
        session_id, _tok, v1 = self._create_online_session("assess-gate-001")
        v2 = self.client.post(
            f"/api/sessions/{session_id}/versions",
            json={"rows": EDITED_ROWS, "baseVersionId": v1, "idempotencyKey": "stage2-before-assess"},
        ).json()["currentVersionId"]

        unexpected_llm = RecordedCall(side_effect=AssertionError("LLM should not run for stale version"))
        with self.using_dependencies(llm_handlers={"generate_stage_assessment": unexpected_llm}):
            stale = self.client.post(
                f"/api/sessions/{session_id}/versions/{v1}/assessments",
                json={"idempotencyKey": "stale-assess-1"},
            )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "VERSION_CONFLICT")
        unexpected_llm.assert_not_called()

        opening = LLMExecutionResult(
            "Opening assessment for Stage 2.",
            1,
            "assess-v2-ok",
            model="mock-model",
            guidance={
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
        )
        with self.using_dependencies(llm_handlers={"generate_stage_assessment": opening}):
            assessed = self.client.post(
                f"/api/sessions/{session_id}/versions/{v2}/assessments",
                json={"idempotencyKey": "assess-v2-ok"},
            )
        self.assertEqual(assessed.status_code, 200, assessed.text)

        self.clock.advance(1140)
        issued = self.client.post(
            f"/api/sessions/{session_id}/versions/{v2}/play-attempts",
            json={"idempotencyKey": "play-k1"},
        )
        self.assertEqual(issued.status_code, 200, issued.text)
        query = parse_qs(urlparse(issued.json()["playUrl"]).query)
        attempt_id = query["cocreationAttempt"][0]
        ticket = query["cocreationPlay"][0]

        # Advance past 20m session deadline while 5m play ticket remains valid
        self.clock.advance(120)
        opening_sync = RecordedCall()
        with self.using_dependencies(online_handlers={"event": opening_sync}):
            replay_assess = self.client.post(
                f"/api/sessions/{session_id}/versions/{v2}/assessments",
                json={"idempotencyKey": "assess-v2-ok"},
            )
        self.assertEqual(replay_assess.status_code, 200, replay_assess.text)
        self.assertGreaterEqual(opening_sync.call_count, 1)

        replay_play = self.client.post(
            f"/api/sessions/{session_id}/versions/{v2}/play-attempts",
            json={"idempotencyKey": "play-k1"},
        )
        self.assertEqual(replay_play.status_code, 200, replay_play.text)
        self.assertEqual(replay_play.json()["attemptId"], attempt_id)

        expired_boot = self.client.post(
            f"/api/play-attempts/{attempt_id}/bootstrap",
            json={"ticket": ticket},
        )
        self.assertEqual(expired_boot.status_code, 409)
        self.assertEqual(expired_boot.json()["code"], "SESSION_DEADLINE_EXPIRED")

        # Post-deadline finalize with edited rows works and replay succeeds
        finalized = self.client.post(
            f"/api/sessions/{session_id}/finalize",
            json={"baseVersionId": v2, "rows": ALT_EDITED_ROWS, "idempotencyKey": "fin-post-deadline"},
        )
        self.assertEqual(finalized.status_code, 200, finalized.text)
        self.assertEqual(finalized.json()["status"], "awaiting_intention")

        finalized_replay = self.client.post(
            f"/api/sessions/{session_id}/finalize",
            json={"baseVersionId": v2, "rows": ALT_EDITED_ROWS, "idempotencyKey": "fin-post-deadline"},
        )
        self.assertEqual(finalized_replay.status_code, 200, finalized_replay.text)

        # Locked session bootstrap returns SESSION_LOCKED
        locked_boot = self.client.post(
            f"/api/play-attempts/{attempt_id}/bootstrap",
            json={"ticket": ticket},
        )
        self.assertEqual(locked_boot.status_code, 409)
        self.assertEqual(locked_boot.json()["code"], "SESSION_LOCKED")

    def test_stale_cookie_falls_back_to_session_token_and_language_deadline_and_session_creation_checks(self):
        session_id, _tok, _v1 = self._create_online_session("cookie-fallback-1")
        token = self.services.derive_token("access", session_id)
        stale_client = create_test_client(self.services)
        self.addCleanup(stale_client.close)
        stale_client.cookies.set(config_models.SESSION_COOKIE_NAME, "stale-cookie-from-other-session")
        read_res = stale_client.get(
            f"/api/sessions/{session_id}",
            headers={"X-Session-Token": token},
        )
        self.assertEqual(read_res.status_code, 200, read_res.text)
        self.assertEqual(read_res.json()["sessionId"], session_id)

        # matchId requires playerNumber 1 or 2
        for bad_player in (None, 0, 3):
            bad = self.client.post(
                "/api/sessions",
                json={
                    "rows": SAMPLE_ROWS,
                    "initialDraftMethod": "partial_completion",
                    "language": "en",
                    "idempotencyKey": f"bad-player-{bad_player}",
                    "matchId": "match-invalid",
                    "playerNumber": bad_player,
                },
            )
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(bad.json()["code"], "INVALID_MATCH_PLAYER")
        with self.assertRaises(ApiError) as bool_player_ctx:
            session_persistence_helpers._create_session_record(
                SAMPLE_ROWS,
                "partial_completion",
                "en",
                "bad-player-bool",
                match_id="match-invalid",
                player_number=True,
                services=self.services,
            )
        self.assertEqual(bool_player_ctx.exception.code, "INVALID_MATCH_PLAYER")

        # Session replay compares initialRowsHash even after draft regeneration mutates draft_rows_json
        created = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": "regen-create-replay-1",
                "matchId": "match-regen-1",
                "playerNumber": 1,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        sid = created.json()["sessionId"]
        integ = created.json()["integrationToken"]
        boot = parse_qs(urlparse(created.json()["launchUrl"]).fragment)["bootstrap"][0]
        self.client.post(f"/api/sessions/{sid}/browser-access", json={"bootstrapToken": boot})
        req_regen = self.client.post(
            f"/api/sessions/{sid}/draft-regenerations",
            json={"idempotencyKey": "regen-req-1"},
        ).json()["draftPreview"]["regenerationRequestId"]
        self.client.post(
            f"/api/integrations/sessions/{sid}/draft-regenerations/{req_regen}/claim",
            headers={"Authorization": f"Bearer {integ}"},
        )
        self.client.post(
            f"/api/integrations/sessions/{sid}/draft-regenerations/{req_regen}/complete",
            headers={"Authorization": f"Bearer {integ}"},
            json={"rows": EDITED_ROWS},
        )
        create_replay = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": "regen-create-replay-1",
                "matchId": "match-regen-1",
                "playerNumber": 1,
            },
        )
        self.assertEqual(create_replay.status_code, 200, create_replay.text)
        self.assertEqual(create_replay.json()["sessionId"], sid)
        create_conflict = self.client.post(
            "/api/sessions",
            json={
                "rows": ALT_EDITED_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": "regen-create-replay-1",
                "matchId": "match-regen-1",
                "playerNumber": 1,
            },
        )
        self.assertEqual(create_conflict.status_code, 409)
        self.assertEqual(create_conflict.json()["code"], "IDEMPOTENCY_CONFLICT")

        # Post-deadline language change: non-replay rejects with SESSION_DEADLINE_EXPIRED, locked replay succeeds
        created_lang = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": "lang-deadline-1",
                "matchId": "match-lang-1",
                "playerNumber": 2,
            },
        ).json()
        sid_lang = created_lang["sessionId"]
        boot_lang = parse_qs(urlparse(created_lang["launchUrl"]).fragment)["bootstrap"][0]
        self.client.post(f"/api/sessions/{sid_lang}/browser-access", json={"bootstrapToken": boot_lang})
        with self.services.connect(immediate=True) as db:
            expired = iso_time(self.clock.utc_datetime() - timedelta(seconds=10))
            db.execute("UPDATE design_sessions SET deadline_at = ? WHERE id = ?", (expired, sid_lang))
        expired_lang = self.client.patch(f"/api/sessions/{sid_lang}/language", json={"language": "en"})
        self.assertEqual(expired_lang.status_code, 409)
        self.assertEqual(expired_lang.json()["code"], "SESSION_DEADLINE_EXPIRED")

        self.clock.advance(1300)
        replay_client = create_test_client(self.services)
        self.addCleanup(replay_client.close)
        replay_client.cookies.set(config_models.SESSION_COOKIE_NAME, token)
        locked_replay = replay_client.patch(f"/api/sessions/{session_id}/language", json={"language": "en"})
        self.assertEqual(locked_replay.status_code, 200, locked_replay.text)

        # Demo draft regeneration executes generator cleanly outside lock
        demo_created = self.client.post("/api/demo-sessions", json={"language": "en", "idempotencyKey": "demo-regen-1"}).json()
        demo_sid = demo_created["sessionId"]
        demo_boot = parse_qs(urlparse(demo_created["launchUrl"]).fragment)["bootstrap"][0]
        self.client.post(f"/api/sessions/{demo_sid}/browser-access", json={"bootstrapToken": demo_boot})
        generated_val = validate_and_solve(EDITED_ROWS)
        gen_mock = RecordedCall(
            return_value=SimpleNamespace(
                rows=EDITED_ROWS,
                seed=7,
                attempts=1,
                generation_summary={"source": "mock"},
                validation=generated_val,
            )
        )
        with self.using_dependencies(demo_level_generator=gen_mock):
            regen = self.client.post(
                f"/api/sessions/{demo_sid}/draft-regenerations",
                json={"idempotencyKey": "demo-regen-run-1"},
            )
        self.assertEqual(regen.status_code, 200, regen.text)
        gen_mock.assert_called_once()
        self.assertEqual(regen.json()["draftPreview"]["rows"], EDITED_ROWS)

    def test_play_metrics_validation_minimum_preservation_and_ready_redacts_paths(self):
        session_id, _tok, v1 = self._create_online_session("metrics-ready-1")
        ready = self.client.get("/ready")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["database"], "ready")
        self.assertNotIn("databasePath", ready.json())

        issued = self.client.post(
            f"/api/sessions/{session_id}/versions/{v1}/play-attempts",
            json={"idempotencyKey": "play-metrics-k1"},
        ).json()
        attempt_id = issued["attemptId"]
        ticket = parse_qs(urlparse(issued["playUrl"]).query)["cocreationPlay"][0]
        attempt_token = self.client.post(
            f"/api/play-attempts/{attempt_id}/bootstrap",
            json={"ticket": ticket},
        ).json()["attemptToken"]

        nan_res = self.client.post(
            f"/api/play-attempts/{attempt_id}/complete",
            content=json.dumps({"attemptToken": attempt_token, "durationSeconds": 1.0}).replace("1.0", "NaN"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(nan_res.status_code, 400)
        self.assertEqual(nan_res.json()["code"], "INVALID_METRICS")

        p1 = self.client.post(
            f"/api/play-attempts/{attempt_id}/progress",
            json={
                "attemptToken": attempt_token,
                "durationSeconds": 15.0,
                "moveCount": 12,
                "pushCount": 4,
                "restartCount": 1,
                "minimumMoves": 6,
                "minimumPushes": 2,
            },
        )
        self.assertEqual(p1.status_code, 200)
        p2 = self.client.post(
            f"/api/play-attempts/{attempt_id}/progress",
            json={
                "attemptToken": attempt_token,
                "durationSeconds": 3.0,
                "moveCount": 5,
                "pushCount": 1,
                "restartCount": 0,
                "minimumMoves": -1,
                "minimumPushes": -1,
            },
        )
        self.assertEqual(p2.status_code, 200)
        with self.services.connect() as db:
            row = db.execute("SELECT * FROM play_attempts WHERE id = ?", (attempt_id,)).fetchone()
        self.assertEqual(row["duration_seconds"], 15.0)
        self.assertEqual(row["move_count"], 12)
        self.assertEqual(row["push_count"], 4)
        self.assertEqual(row["restart_count"], 1)
        self.assertEqual(row["minimum_moves"], 6)
        self.assertEqual(row["minimum_pushes"], 2)


class ProjectionDashboardAndDomainTests(SessionTestCase):
    """Covers Policy 2 dashboard, projection filtering, :memory: SQLite, and level validation."""

    def setUp(self):
        self.clock = FakeClock()
        self.open_workspace(clock=self.clock)

    def test_policy2_dashboard_kind_status_parent_and_assessment_guard(self):
        self.assertEqual(
            _dashboard_turn_kind({"proposalQuestion": {"topicId": "topic-p2", "number": 2}}, {}, turn_id="t1"),
            ("proposal", "proposal:topic-p2", None),
        )
        self.assertEqual(
            _dashboard_turn_kind({"proposalOffer": {"summary": "Widen lane"}}, {}, turn_id="turn-fallback"),
            ("proposal", "proposal:turn-fallback", None),
        )
        self.assertEqual(
            _dashboard_turn_kind(
                {"proposalOffer": {"summary": "Widen lane"}, "proposalId": "prop-9"},
                {},
                turn_id="turn-fallback",
            ),
            ("proposal", "proposal:prop-9", None),
        )
        for num in (1, 2, 3):
            self.assertEqual(
                _dashboard_node_status("proposal", {"proposalQuestion": {"topicId": "t", "number": num}}),
                f"clarifying_{num}",
            )

        created = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": "dash-p2-1",
                "matchId": "match-dash-1",
                "playerNumber": 1,
            },
        ).json()
        sid = created["sessionId"]
        boot = parse_qs(urlparse(created["launchUrl"]).fragment)["bootstrap"][0]
        self.client.post(f"/api/sessions/{sid}/browser-access", json={"bootstrapToken": boot})
        v1 = self.client.patch(f"/api/sessions/{sid}/language", json={"language": "en"}).json()["currentVersionId"]

        with self.services.connect(immediate=True) as db:
            session = repository.get_session(db, sid)
            now = self.services.utc_now()
            db.execute(
                """
                INSERT INTO conversation_turns(
                    id, session_id, sequence_number, role, content, language,
                    version_id, request_id, guidance_json, created_at
                ) VALUES (?, ?, ?, 'assistant', ?, 'en', ?, ?, ?, ?)
                """,
                ("turn-open", sid, 1, "Opening turn", v1, "req-open", "{}", now),
            )
            db.execute(
                """
                INSERT INTO llm_assessments(id, session_id, version_id, assistant_turn_id, payload_json, prompt_version, created_at)
                VALUES (?, ?, ?, ?, '{}', 'v1', ?)
                """,
                ("assess-1", sid, v1, "turn-open", now),
            )
            self.assertIsNone(_dashboard_node_projection_for_turn(db, session, "req-open"))

            db.execute(
                """
                INSERT INTO conversation_turns(
                    id, session_id, sequence_number, role, content, language,
                    version_id, request_id, guidance_json, created_at
                ) VALUES (?, ?, ?, 'assistant', ?, 'en', ?, ?, ?, ?)
                """,
                (
                    "turn-prop-offer",
                    sid,
                    2,
                    "Direct proposal",
                    v1,
                    "req-prop",
                    json.dumps({"proposalOffer": {"summary": "Move water"}}),
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO conversation_turns(
                    id, session_id, sequence_number, role, content, language,
                    version_id, request_id, guidance_json, created_at
                ) VALUES (?, ?, ?, 'assistant', ?, 'en', ?, ?, ?, ?)
                """,
                (
                    "turn-challenge",
                    sid,
                    3,
                    "Challenge prompt",
                    v1,
                    "req-chall",
                    json.dumps({"challengeState": {"challengeId": "ch-dash-1", "status": "awaiting_reason"}}),
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO revision_challenges(
                    challenge_id, session_id, base_version_id, source_proposal_turn_id,
                    challenge_turn_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("ch-dash-1", sid, v1, "turn-prop-offer", "turn-challenge", "awaiting_reason", now, now),
            )
            node = _dashboard_node_projection_for_turn(db, session, "req-chall")
            self.assertEqual(node["nodeType"], "player_challenge")
            self.assertEqual(node["nodeParentId"], "proposal:turn-prop-offer")

    def test_public_guidance_and_projection_state_filtering(self):
        filtered = _public_guidance(
            {
                "move": "offer_revision",
                "_internalSecret": "hide",
                "dashboardNode": {"proposalTopicId": "t1"},
                "manualEditReview": {"outcome": "match"},
                "openingRecovery": {"step": 1},
                "proposalOffer": {
                    "summary": "Public summary",
                    "executionBrief": {"secret": True},
                    "revisionWorkflow": {"steps": []},
                    "revisionPlan": {"op": 1},
                    "_privateOffer": 1,
                },
            }
        )
        self.assertEqual(filtered["move"], "offer_revision")
        for key in ("_internalSecret", "dashboardNode", "manualEditReview", "openingRecovery"):
            self.assertNotIn(key, filtered)
        self.assertEqual(filtered["proposalOffer"], {"summary": "Public summary"})

        created = self.client.post(
            "/api/sessions",
            json={
                "rows": SAMPLE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": "proj-filter-1",
                "matchId": "match-proj-1",
                "playerNumber": 1,
            },
        ).json()
        sid = created["sessionId"]
        boot = parse_qs(urlparse(created["launchUrl"]).fragment)["bootstrap"][0]
        self.client.post(f"/api/sessions/{sid}/browser-access", json={"bootstrapToken": boot})
        v1 = self.client.patch(f"/api/sessions/{sid}/language", json={"language": "en"}).json()["currentVersionId"]

        with self.services.connect(immediate=True) as db:
            t_base = self.clock.utc_datetime()
            t_fail = iso_time(t_base + timedelta(seconds=1))
            t_cancel = iso_time(t_base + timedelta(seconds=2))
            self.services.record_event(
                db,
                sid,
                "message_generation_failed",
                {"messageKey": "m-fail", "baseVersionId": v1, "code": "UPSTREAM_TIMEOUT", "requestId": "req-f"},
                t_fail,
            )
            self.services.record_event(
                db,
                sid,
                "binary_discovery_state",
                {
                    "versionId": v1,
                    "topicId": "topic-x",
                    "status": "cancelled",
                    "initialMessageKey": "m-fail",
                    "questionCount": 1,
                },
                t_cancel,
            )
            ctx = {
                **empty_design_context(),
                "intentHypotheses": [
                    {"id": "h-old", "statement": "Keep open lane", "status": "confirmed", "semanticClaims": []},
                    {"id": "h-new", "statement": "Generic fallback statement", "status": "tentative", "semanticClaims": []},
                ],
            }
            db.execute("UPDATE level_versions SET design_context_json = ? WHERE id = ?", (json.dumps(ctx), v1))
            db.execute(
                """
                INSERT INTO conversation_turns(
                    id, session_id, sequence_number, role, content, language,
                    version_id, request_id, guidance_json, created_at
                ) VALUES (?, ?, ?, 'assistant', ?, 'en', ?, ?, ?, ?)
                """,
                (
                    "turn-prop-lock",
                    sid,
                    1,
                    "Conflict & proposal",
                    v1,
                    "req-mix",
                    json.dumps(
                        {
                            "_intentHypothesisId": "h-new",
                            "_intentReviewState": {
                                "interactionMode": "conflict_choice",
                                "existingHypothesisId": "h-old",
                                "candidateText": "Reviewed candidate phrasing",
                            },
                            "proposalOffer": {"summary": "Locked after deadline"},
                        }
                    ),
                    t_fail,
                ),
            )
            projected = serialize_session(db, sid, now=t_base + timedelta(seconds=5))
            self.assertIsNone(projected["lastMessageFailure"])
            turn_guidance = projected["turns"][0]["guidance"]
            self.assertEqual(
                turn_guidance["intentState"]["conflictChoice"]["options"][0]["statement"],
                "Reviewed candidate phrasing",
            )
            projected_expired = serialize_session(db, sid, now=t_base + timedelta(minutes=25))
            self.assertEqual(
                projected_expired["turns"][0]["proposalState"],
                {"status": "locked", "actionable": False},
            )

    def test_memory_sqlite_repository_and_app_lifespan_work_without_sweeper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mem_services = create_test_services(
                Path(temp_dir),
                settings_overrides={"database_path": ":memory:", "sweeper_enabled": False},
            )
            self.addCleanup(mem_services.repository.close)
            with TestClient(create_app(mem_services)) as client:
                ready = client.get("/ready")
                self.assertEqual(ready.status_code, 200)
                self.assertEqual(ready.json()["database"], "ready")
                created = client.post(
                    "/api/sessions",
                    json={
                        "rows": SAMPLE_ROWS,
                        "initialDraftMethod": "partial_completion",
                        "language": "en",
                        "idempotencyKey": "mem-sess-1",
                        "matchId": "match-mem-1",
                        "playerNumber": 1,
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                with mem_services.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM design_sessions").fetchone()[0], 1)

    def test_legacy_chat_settings_deadline_validation_bindings_and_deployment_configs(self):
        self.assertFalse(CoCreationSettings.from_env({}).legacy_chat_enabled)
        self.assertTrue(CoCreationSettings.from_env({"COCREATION_LEGACY_CHAT_ENABLED": "true"}).legacy_chat_enabled)

        with tempfile.TemporaryDirectory() as temp_dir:
            chat_services = create_test_services(
                Path(temp_dir),
                settings_overrides={"legacy_chat_enabled": True},
            )
            chat_services.llm.set_handler(
                "generate_chat_reply",
                LLMExecutionResult("Checked.", 1, "legacy-deadline-req"),
            )
            with create_test_client(chat_services) as chat_client:
                ok = chat_client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hello"}]})
        self.assertEqual(ok.status_code, 200)
        self.assertIsNotNone(chat_services.llm.method_calls["generate_chat_reply"][0]["kwargs"].get("_deadline"))

        # MessageRequest accepts retain_current
        req = MessageRequest(
            content="Retain current stage",
            baseVersionId="v1",
            idempotencyKey="k1",
            challengeChoice="retain_current",
        )
        self.assertEqual(req.challengeChoice, "retain_current")

        # HttpOnlineSyncGateway.sync_turn is a safe no-op making zero HTTP calls
        called = RecordedCall()
        gateway = HttpOnlineSyncGateway(
            CoCreationSettings(
                database_path=Path(":memory:"),
                online_backend_base_url="http://online.invalid:8000",
                intention_sync_secret="secret",
            ),
            http_client_factory=lambda **kwargs: httpx.Client(
                transport=httpx.MockTransport(lambda request: called(request) or httpx.Response(200))
            ),
        )
        self.assertIsNone(
            gateway.sync_turn(
                {"match_id": "m1", "player_number": 1, "demo_mode": 0},
                "turn-1",
                "assistant",
                "hello",
                "en",
                1,
                "2026-10-01T00:00:00Z",
            )
        )
        called.assert_not_called()

        # L-shaped notch boundary walls vs isolated internal wall
        notch_before = [
            "            ",
            "   #########",
            "   #.......#",
            "   #...p...#",
            "   #...s.t.#",
            "   #..#....#",
            "   #.......#",
            "   #########",
            "            ",
            "            ",
        ]
        notch_after = list(notch_before)
        # (3, 1) is an interior coordinate adjacent to exterior void ' ', so it belongs to outerShell;
        # (6, 5) is isolated from both the map perimeter and exterior void, so it belongs to internalWalls.
        notch_after[1] = "    ########"
        notch_after[2] = "   ##......#"
        notch_after[5] = "   #.......#"
        summary = summarize_stage_changes(notch_before, notch_after)
        self.assertEqual(summary["components"], ["outerShell", "internalWalls"])

        # build_entity_bindings accepts transitions with from == "."
        placed_before = list(SAMPLE_ROWS)
        placed_after = list(SAMPLE_ROWS)
        placed_after[4] = "#...p..s...#"
        placed_after[5] = "#.....t....#"
        parent_bindings = build_entity_bindings(placed_before)
        parent_box = next(item for item in parent_bindings["entities"] if item["kind"] == "box")
        updated_bindings = build_entity_bindings(
            placed_after,
            parent_bindings=parent_bindings,
            entity_transitions=[
                {
                    "anchorEntity": parent_box["label"],
                    "from": ".",
                    "to": "s",
                    "row": 5,
                    "column": 8,
                }
            ],
            source="llm_accepted",
        )
        self.assertTrue(any(item["identityConfidence"] == "exact" and item["row"] == 5 and item["column"] == 8 for item in updated_bindings["entities"]))

        # format_map_claim_correction coverage in zh-CN & en
        zh_unknown = format_map_claim_correction(
            [{"kind": "entity", "reason": "entity_identity_unknown", "entity": "B1"}],
            language="zh-CN",
        )
        en_tile = format_map_claim_correction(
            [{"kind": "tile", "row": 2, "column": 3, "actualTile": "@"}],
            language="en",
        )
        self.assertIn("B1", zh_unknown)
        self.assertIn("water", en_tile)

        # Deployment configs
        service_text = (REPOSITORY_ROOT / "CoCreationPrototype" / "Deployment" / "sokoban-cocreation.service").read_text(encoding="utf-8")
        nginx_text = (REPOSITORY_ROOT / "CoCreationPrototype" / "Deployment" / "nginx-sokoban.conf").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=-/root/SURF/CoCreationPrototype/Backend/.env", service_text)
        self.assertIn("--host 127.0.0.1 --port 8010", service_text)
        self.assertIn("ReadWritePaths=/root/SURF/CoCreationPrototype/Backend/data", service_text)
        self.assertIn("return 301 https://sokobanaidemo.top/cocreation/;", nginx_text)
        self.assertIn("proxy_read_timeout 135s;", nginx_text)
        self.assertIn("proxy_send_timeout 135s;", nginx_text)
        self.assertIn("120s budget", nginx_text)


if __name__ == "__main__":
    unittest.main()
