"""HTTP API contract tests covering all unified FastAPI endpoints, parity, and security."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from tests.conftest import (
    ALT_ROWS,
    BASE_ROWS,
    extract_hash_param,
    extract_query_param,
    make_binary_ask_json,
    make_binary_ask_water_json,
    make_binary_plan_json,
    make_chat_json,
    make_dg_summary_json,
    make_level_plan_json,
    make_map_ops_json,
    make_opening_json,
    make_routing_json,
)


def test_health_readiness_default_secret_warning_and_static_mounts(
    app_bundle_factory, caplog: pytest.LogCaptureFixture
) -> None:
    import server.main as server_main

    assert not hasattr(server_main, "app")
    assert callable(server_main.create_app)
    bundle = app_bundle_factory(token_secret="development-only-change-before-deployment")
    with caplog.at_level(logging.WARNING), TestClient(bundle.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/cocreation/health").json() == {"status": "ok"}

        ready = client.get("/ready")
        assert ready.status_code == 200
        ready_json = ready.json()
        assert ready_json["database"] == "ready"
        assert ready_json["tokenSecretConfigured"] is False
        assert ready_json["deepseekConfigured"] is False
        assert ready_json["cocreationLlmConfigured"] is False
        assert ready.headers.get("X-Request-ID")

        co_ready = client.get("/cocreation/ready")
        assert co_ready.status_code == 200 and co_ready.json()["database"] == "ready"

        if bundle.settings.dashboard_dist_dir.exists():
            assert client.get("/frontend/").status_code == 200
            pdf = client.get("/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf")
            assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        if bundle.settings.cocreation_dist_dir.exists():
            assert client.get("/cocreation/").status_code == 200

    assert any("TOKEN_SECRET" in rec.message for rec in caplog.records)
    assert bundle.deepseek_transport.closed and bundle.cocreation_transport.closed

    strong_bundle = app_bundle_factory()
    with TestClient(strong_bundle.app) as strong_client:
        assert strong_client.get("/ready").json()["tokenSecretConfigured"] is True


@pytest.mark.asyncio
async def test_dead_legacy_routes_return_404_and_validation_envelope(app_bundle_factory) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        for method, path in (
            ("POST", "/api/chat"),
            ("GET", "/api/sample"),
            ("GET", "/survey-records"),
            ("GET", "/level-records"),
            ("POST", "/generate-pc-level"),
            ("POST", "/validate-human-adjustment"),
        ):
            resp = await client.request(method, path)
            assert resp.status_code == 404, (method, path, resp.status_code)
            body = resp.json()
            assert body["error"]["code"] == "HTTP_404"
            assert resp.headers.get("X-Request-ID")

        invalid = await client.post("/api/demo-sessions", json={"language": "fr"})
        assert invalid.status_code == 400
        err_body = invalid.json()
        assert err_body["error"]["code"] == "INVALID_REQUEST"
        assert err_body["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_online_rooms_study_records_dg_level_plan_and_dashboard_decision_5b(
    app_bundle_factory,
) -> None:
    base_plan = json.loads(make_level_plan_json())
    ha_contract = json.dumps(
        {"changes": {"archetype": "open_workshop"}, "preserveUnlisted": True},
        separators=(",", ":"),
        sort_keys=True,
    )
    selected_ha = json.dumps(
        {
            "id": "opt_open",
            "title": "Open center",
            "description": "Remove internal walls.",
            "promptText": ha_contract,
        }
    )
    bundle = app_bundle_factory(
        deepseek_responses=[make_dg_summary_json("en"), make_level_plan_json()]
    )

    async with bundle.client() as client:
        # 1. Online room lifecycle + X-Player-Token checks
        c_resp = await client.post(
            "/online/rooms", json={"studySessionId": "study-p1", "menuStartedFlow": True}
        )
        assert c_resp.status_code == 200
        room1 = c_resp.json()
        match_id = room1["matchId"]
        p1_token = room1["playerToken"]

        no_auth = await client.get(f"/online/rooms/{match_id}")
        assert no_auth.status_code == 401
        assert no_auth.json()["error"]["code"]

        j_resp = await client.post(
            "/online/rooms/join",
            json={"roomCode": room1["roomCode"], "studySessionId": "study-p2"},
        )
        assert j_resp.status_code == 200
        p2_token = j_resp.json()["playerToken"]

        assert (
            await client.post(
                f"/online/rooms/{match_id}/ready",
                json={"ready": True},
                headers={"X-Player-Token": p1_token},
            )
        ).status_code == 200
        assert (
            await client.post(
                f"/online/rooms/{match_id}/ready",
                json={"ready": True},
                headers={"X-Player-Token": p2_token},
            )
        ).status_code == 200

        draft_body = {
            "finalDifficulty": "Medium",
            "finalLayout": "Balanced",
            "q1Answer": "observe_then_decide",
            "q2Answer": "consider_order",
            "q3Answer": "connected_areas",
            "q4Answer": "occasional_detours",
            "aiReflection": "Balanced draft.",
        }
        assert (
            await client.post(
                f"/online/rooms/{match_id}/draft",
                json=draft_body,
                headers={"X-Player-Token": p1_token},
            )
        ).status_code == 200

        chal = await client.post(
            f"/online/rooms/{match_id}/challenge",
            json={"rows": BASE_ROWS, "aiAssistantMode": "description_generation"},
            headers={"X-Player-Token": p1_token},
        )
        assert chal.status_code == 200
        chal2 = await client.post(
            f"/online/rooms/{match_id}/challenge",
            json={"rows": ALT_ROWS, "aiAssistantMode": "description_generation"},
            headers={"X-Player-Token": p2_token},
        )
        assert chal2.status_code == 200
        res = await client.post(
            f"/online/rooms/{match_id}/result",
            json={"durationSeconds": 11.5, "moveCount": 18, "minimumMoves": 14, "restartCount": 0},
            headers={"X-Player-Token": p2_token},
        )
        assert res.status_code == 200

        # Separate room testing leave flow
        r2 = (await client.post("/online/rooms", json={"studySessionId": "study-leave"})).json()
        left = await client.post(
            f"/online/rooms/{r2['matchId']}/leave",
            headers={"X-Player-Token": r2["playerToken"]},
        )
        assert left.status_code == 200 and left.json()["status"] == "cancelled"

        # 2. DG Guide Summary + Level Plan (LLM + HA 0-LLM short-circuit)
        dg = await client.post(
            "/dg/guide/summary",
            json={
                "firstMovePreference": "observe_then_decide",
                "pushPlanningPreference": "consider_order",
                "spacePreference": "connected_areas",
                "routeRhythmPreference": "occasional_detours",
                "language": "en",
            },
        )
        assert dg.status_code == 200
        assert dg.headers["X-DG-Guide-Source"] == "llm"
        assert dg.headers["X-LLM-Attempts-Used"] == "1"
        assert len(bundle.deepseek_transport.calls) == 1
        assert len(bundle.cocreation_transport.calls) == 0

        lp = await client.post(
            "/generate-level-plan",
            json={"ideaText": "Balanced central corridor", "sessionId": "lp-s1"},
        )
        assert lp.status_code == 200
        assert lp.headers["X-LLM-Attempts-Used"] == "1"
        assert len(bundle.deepseek_transport.calls) == 2

        ha_lp = await client.post(
            "/generate-level-plan",
            json={
                "revisionMode": "ha",
                "previousLevelPlan": base_plan,
                "selectedHAPlan": selected_ha,
                "sessionId": "lp-s1",
            },
        )
        assert ha_lp.status_code == 200
        assert ha_lp.headers["X-LLM-Attempts-Used"] == "0"
        assert len(bundle.deepseek_transport.calls) == 2  # Zero extra LLM calls!
        assert ha_lp.json()["archetype"] == "open_workshop"

        # 3. Study telemetry endpoints + JSONL export
        assert (
            await client.post(
                "/record-survey-response",
                json={"participantId": "p1", "phase": "pre", "answers": {"q1": "yes"}},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/record-level-start",
                json={"participantId": "p1", "levelId": "lvl-1"},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/record-level-end",
                json={"participantId": "p1", "levelId": "lvl-1", "durationSeconds": 9.2},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/record-journey-event",
                json={"participantId": "p1", "ideaId": "idea-1", "phase": "menu"},
            )
        ).status_code == 200

        export_all = await client.get("/export/study-logs.jsonl")
        assert export_all.status_code == 200
        lines = [json.loads(line) for line in export_all.text.strip().splitlines()]
        assert len(lines) == 4

        # Sync a formal co-creation Stage 1 event so match_id appears in matchmaking projection
        co_created = (
            await client.post(
                "/api/sessions",
                json={
                    "rows": BASE_ROWS,
                    "initialDraftMethod": "partial_completion",
                    "language": "en",
                    "idempotencyKey": "dash-co-1",
                    "matchId": match_id,
                    "playerNumber": 1,
                },
            )
        ).json()
        co_sid = co_created["sessionId"]
        assert (
            await client.post(
                f"/api/sessions/{co_sid}/browser-access",
                json={"bootstrapToken": extract_hash_param(co_created["launchUrl"], "bootstrap")},
            )
        ).status_code == 200
        assert (
            await client.patch(f"/api/sessions/{co_sid}/language", json={"language": "en"})
        ).status_code == 200

        # 4. Dashboard Decision 5B: open reads, X-Delete-Password required on deletes
        open_data = await client.get("/matchmaking-records-data")
        assert open_data.status_code == 200
        assert open_data.json()["summary"]["matchCount"] == 1
        raw_txt = await client.get("/matchmaking-records")
        assert raw_txt.status_code == 200 and match_id in raw_txt.text

        bad_pw = await client.post(
            "/verify-dashboard-password", headers={"X-Delete-Password": "wrong"}
        )
        assert bad_pw.status_code == 401
        ok_pw = await client.post(
            "/verify-dashboard-password", headers={"X-Delete-Password": "delete-pass-123"}
        )
        assert ok_pw.status_code == 200 and ok_pw.json()["ok"] is True

        denied_del = await client.post("/delete-online-match", json={"matchId": match_id})
        assert denied_del.status_code == 401
        deleted = await client.post(
            "/delete-online-match",
            json={"matchId": match_id},
            headers={"X-Delete-Password": "delete-pass-123"},
        )
        assert deleted.status_code == 200 and deleted.json()["matchId"] == match_id

        cleared = await client.post(
            "/clear-matchmaking-records",
            headers={"X-Delete-Password": "delete-pass-123"},
        )
        assert cleared.status_code == 200
        assert (await client.get("/matchmaking-records-data")).json()["summary"]["matchCount"] == 0


@pytest.mark.asyncio
async def test_cocreation_api_parity_demo_ttl_and_full_dialogue_stage_contracts(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        # 1. /api vs /cocreation/api parity + demo TTL isolation
        d1 = await client.post(
            "/api/demo-sessions", json={"language": "en", "idempotencyKey": "demo-1"}
        )
        assert d1.status_code == 200
        d1_json = d1.json()
        d1_sid = d1_json["sessionId"]

        d2 = await client.post(
            "/cocreation/api/demo-sessions",
            json={"language": "zh-CN", "idempotencyKey": "demo-2"},
        )
        assert d2.status_code == 200
        d2_json = d2.json()
        d2_sid = d2_json["sessionId"]

        # Auth d1 via /api and d2 via /cocreation/api -> d1 must still exist (TTL isolation!)
        assert (
            await client.post(
                f"/api/sessions/{d1_sid}/browser-access",
                json={"bootstrapToken": extract_hash_param(d1_json["launchUrl"], "bootstrap")},
            )
        ).status_code == 200
        assert (await client.get(f"/api/sessions/{d1_sid}")).status_code == 200

        # Advance past 24h demo TTL -> creating d3 prunes expired d1 & d2
        bundle.clock.advance(86401.0)
        d3 = await client.post(
            "/api/demo-sessions", json={"language": "en", "idempotencyKey": "demo-3"}
        )
        assert d3.status_code == 200
        assert (await client.get(f"/api/sessions/{d1_sid}")).status_code == 404

        assert (
            await client.post(
                f"/cocreation/api/sessions/{d2_sid}/browser-access",
                json={"bootstrapToken": extract_hash_param(d2_json["launchUrl"], "bootstrap")},
            )
        ).status_code == 401
        assert (await client.get(f"/cocreation/api/sessions/{d2_sid}")).status_code == 404

        # 2. Formal co-creation session: browser-access, session-token header, draft regen, stage
        created = await client.post(
            "/api/sessions",
            json={
                "rows": BASE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": "formal-s1",
            },
        )
        assert created.status_code == 200
        cjson = created.json()
        sid = cjson["sessionId"]
        int_token = cjson["integrationToken"]

        auth = await client.post(
            f"/api/sessions/{sid}/browser-access",
            json={"bootstrapToken": extract_hash_param(cjson["launchUrl"], "bootstrap")},
        )
        assert auth.status_code == 200

        # Draft regeneration + Unity integration endpoints (claim -> complete, then request -> fail)
        regen1 = await client.post(
            f"/api/sessions/{sid}/draft-regenerations",
            json={"idempotencyKey": "regen-req-1"},
        )
        assert regen1.status_code == 200
        rid1 = regen1.json()["draftPreview"]["regenerationRequestId"]

        integ_status = await client.get(
            f"/api/integrations/sessions/{sid}",
            headers={"Authorization": f"Bearer {int_token}"},
        )
        assert integ_status.status_code == 200
        assert (
            await client.post(
                f"/api/integrations/sessions/{sid}/draft-regenerations/{rid1}/claim",
                headers={"X-Integration-Token": int_token},
            )
        ).status_code == 200
        regen_rows = list(BASE_ROWS)
        regen_rows[6] = "#....#.....#"
        assert (
            await client.post(
                f"/api/integrations/sessions/{sid}/draft-regenerations/{rid1}/complete",
                json={"rows": regen_rows},
                headers={"Authorization": f"Bearer {int_token}"},
            )
        ).status_code == 200

        bundle.clock.advance(1.0)
        regen2 = await client.post(
            f"/api/sessions/{sid}/draft-regenerations",
            json={"idempotencyKey": "regen-req-2"},
        )
        rid2 = regen2.json()["draftPreview"]["regenerationRequestId"]
        assert (
            await client.post(
                f"/api/integrations/sessions/{sid}/draft-regenerations/{rid2}/claim",
                headers={"Authorization": f"Bearer {int_token}"},
            )
        ).status_code == 200
        assert (
            await client.post(
                f"/api/integrations/sessions/{sid}/draft-regenerations/{rid2}/fail",
                json={"failureCode": "generation_failed"},
                headers={"Authorization": f"Bearer {int_token}"},
            )
        ).status_code == 200

        bundle.clock.advance(1.0)
        regen3 = await client.post(
            f"/api/sessions/{sid}/draft-regenerations",
            json={"idempotencyKey": "regen-req-3"},
        )
        rid3 = regen3.json()["draftPreview"]["regenerationRequestId"]
        assert (
            await client.post(
                f"/api/sessions/{sid}/draft-regenerations/{rid3}/cancel"
            )
        ).status_code == 200

        # Lock language -> Stage 1, then assess, save Stage 2, restore Stage 1 -> Stage 3
        bundle.cocreation_transport.queue(make_opening_json("Stage 1 opening."))
        locked = await client.patch(f"/api/sessions/{sid}/language", json={"language": "en"})
        assert locked.status_code == 200
        v1 = locked.json()["currentVersionId"]

        assessed = await client.post(
            f"/api/sessions/{sid}/versions/{v1}/assessments",
            json={"idempotencyKey": "assess-v1"},
        )
        assert assessed.status_code == 200 and assessed.json()["assessments"]

        manual_rows = list(BASE_ROWS)
        manual_rows[7] = "#....#.....#"
        saved = await client.post(
            f"/api/sessions/{sid}/versions",
            json={
                "rows": manual_rows,
                "baseVersionId": v1,
                "idempotencyKey": "save-v2",
                "summary": "Add lower wall",
            },
        )
        assert saved.status_code == 200, saved.text
        v2 = saved.json()["currentVersionId"]

        restored = await client.post(
            f"/api/sessions/{sid}/versions/{v1}/restore",
            json={"baseVersionId": v2, "idempotencyKey": "restore-v1"},
        )
        assert restored.status_code == 200
        v3 = restored.json()["currentVersionId"]

        # Play attempts (complete + abandon)
        pa = (
            await client.post(
                f"/api/sessions/{sid}/versions/{v3}/play-attempts",
                json={"idempotencyKey": "pa-1"},
            )
        ).json()
        aid = pa["attemptId"]
        boot = (
            await client.post(
                f"/api/play-attempts/{aid}/bootstrap",
                json={"ticket": extract_query_param(pa["playUrl"], "cocreationPlay")},
            )
        ).json()
        att_tok = boot["attemptToken"]
        assert (
            await client.post(f"/api/play-attempts/{aid}/start", json={"attemptToken": att_tok})
        ).status_code == 200
        assert (
            await client.post(
                f"/api/play-attempts/{aid}/progress",
                json={"attemptToken": att_tok, "moveCount": 3, "pushCount": 1},
            )
        ).status_code == 200
        comp = await client.post(
            f"/api/play-attempts/{aid}/complete",
            json={"attemptToken": att_tok, "durationSeconds": 4.2, "moveCount": 7, "pushCount": 2},
        )
        assert comp.status_code == 200 and comp.json()["status"] == "completed"

        pa2 = (
            await client.post(
                f"/api/sessions/{sid}/versions/{v3}/play-attempts",
                json={"idempotencyKey": "pa-2"},
            )
        ).json()
        boot2 = (
            await client.post(
                f"/api/play-attempts/{pa2['attemptId']}/bootstrap",
                json={"ticket": extract_query_param(pa2["playUrl"], "cocreationPlay")},
            )
        ).json()
        ab = await client.post(
            f"/api/play-attempts/{pa2['attemptId']}/abandon",
            json={"attemptToken": boot2["attemptToken"]},
        )
        assert ab.status_code == 200 and ab.json()["status"] == "abandoned"

        # Ordinary chat + DB idempotency replay & conflict
        bundle.cocreation_transport.queue(
            make_routing_json("ordinary"),
            make_chat_json("Center route flows well.", hyp="Designer likes open center."),
        )
        msg1 = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "What about the center?",
                "baseVersionId": v3,
                "idempotencyKey": "chat-1",
                "action": "none",
            },
        )
        assert msg1.status_code == 200
        calls_now = len(bundle.cocreation_transport.calls)

        msg1_replay = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "What about the center?",
                "baseVersionId": v3,
                "idempotencyKey": "chat-1",
                "action": "send",
            },
        )
        assert msg1_replay.status_code == 200
        assert len(bundle.cocreation_transport.calls) == calls_now  # DB idempotency replay!

        msg1_conflict = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Completely different prompt",
                "baseVersionId": v3,
                "idempotencyKey": "chat-1",
                "action": "send",
            },
        )
        assert msg1_conflict.status_code == 409
        assert msg1_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        # Question & intent-hypothesis feedback (seed a visible_output open question as ordinary
        # chat omits followUpQuestion unless resolving an active disagreement)
        from server.domain.memory import add_open_question, ensure_design_context
        from server.persistence.database import dump_json

        with bundle.container.db.write_tx() as conn:
            v3_rec = bundle.container.sessions_repo.get_version(sid, v3)
            assert v3_rec is not None
            q_ctx = add_open_question(
                ensure_design_context(v3_rec.design_context),
                "Do you prefer a wider center?",
                stage_id=v3,
                turn_id=msg1.json()["turns"][-1]["turnId"],
                source_kind="visible_output",
            )
            conn.execute(
                "UPDATE level_versions SET design_context_json = ? "
                "WHERE session_id = ? AND version_id = ?",
                (dump_json(q_ctx.to_wire_dict()), sid, v3),
            )
        refreshed_v3 = (await client.get(f"/api/sessions/{sid}")).json()
        v3_ctx = next(pc for pc in refreshed_v3["progressContexts"] if pc["versionId"] == v3)
        q_id = v3_ctx["questionRecords"][0]["questionId"]
        qf = await client.post(
            f"/api/sessions/{sid}/questions/{q_id}/feedback",
            json={"action": "ignore", "baseVersionId": v3, "idempotencyKey": "qf-1"},
        )
        assert qf.status_code == 200 and qf.json()["outcome"] == "applied"

        hyp_turn = next(
            t for t in qf.json()["session"]["turns"] if t["guidance"].get("intentState")
        )
        hyp_id = hyp_turn["guidance"]["intentState"]["hypothesisId"]
        hf = await client.post(
            f"/api/sessions/{sid}/intent-hypotheses/{hyp_id}/feedback",
            json={
                "action": "keep",
                "selectedHypothesisId": hyp_id,
                "baseVersionId": v3,
                "idempotencyKey": "hf-1",
            },
        )
        assert hf.status_code == 200 and hf.json()["outcome"] == "applied"

        # Policy-2 binary discovery -> choice -> accept proposal -> cancel topic
        bundle.cocreation_transport.queue(
            make_binary_ask_json,
            make_binary_plan_json(),
            make_map_ops_json(),
        )
        asked = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Make center simpler",
                "baseVersionId": v3,
                "idempotencyKey": "bin-msg-1",
                "requestProposal": True,
                "action": "send",
            },
        )
        assert asked.status_code == 200
        pq = next(
            t["guidance"]["proposalQuestion"]
            for t in reversed(asked.json()["turns"])
            if t["guidance"].get("proposalQuestion")
        )
        chosen = await client.post(
            f"/api/sessions/{sid}/proposal-discovery/choices",
            json={
                "topicId": pq["topicId"],
                "questionId": pq["questionId"],
                "choiceId": pq["choices"][0]["choiceId"],
                "baseVersionId": v3,
                "idempotencyKey": "bin-ch-1",
            },
        )
        assert chosen.status_code == 200
        chosen_json = chosen.json()
        assert chosen_json["proposals"] == []
        offer_turn = chosen_json["turns"][-1]
        assert isinstance(offer_turn["guidance"].get("proposalOffer"), dict)

        executed = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Draft this suggestion",
                "baseVersionId": v3,
                "idempotencyKey": "bin-exec-1",
                "action": "execute_revision",
                "sourceTurnId": offer_turn["turnId"],
            },
        )
        assert executed.status_code == 200
        executed_json = executed.json()
        assert not executed_json["turns"][-1]["guidance"].get("proposalOffer")
        prop_id = executed_json["proposals"][0]["proposalId"]
        accepted = await client.post(
            f"/api/sessions/{sid}/proposals/{prop_id}/decision",
            json={"decision": "accept", "baseVersionId": v3, "idempotencyKey": "prop-dec-1"},
        )
        assert accepted.status_code == 200
        v4 = accepted.json()["currentVersionId"]
        assert v4 != v3

        # Binary discovery cancel path on v4
        bundle.cocreation_transport.queue(make_binary_ask_water_json)
        asked2 = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Adjust water",
                "baseVersionId": v4,
                "idempotencyKey": "bin-msg-2",
                "requestProposal": True,
                "action": "send",
            },
        )
        assert asked2.status_code == 200
        pq2 = next(
            t["guidance"]["proposalQuestion"]
            for t in reversed(asked2.json()["turns"])
            if t["guidance"].get("proposalQuestion")
        )
        cancelled = await client.post(
            f"/api/sessions/{sid}/proposal-discovery/cancel",
            json={"topicId": pq2["topicId"], "baseVersionId": v4, "idempotencyKey": "bin-can-1"},
        )
        assert cancelled.status_code == 200

        # Component recovery retry via HTTP
        bundle.container.operational_repo.upsert_task(
            "rec-http-1",
            sid,
            "stage_opening",
            v4,
            {
                "component": "stage_opening",
                "baseVersionId": v4,
                "status": "retry_pending",
                "attemptsUsed": 0,
                "maxAttempts": 3,
            },
            updated_at=bundle.clock.utc_now(),
        )
        bundle.cocreation_transport.queue(make_opening_json("Recovered stage 4 opening."))
        retried = await client.post(
            f"/api/sessions/{sid}/component-recoveries/rec-http-1/retry",
            json={"baseVersionId": v4, "idempotencyKey": "rec-try-1"},
        )
        assert retried.status_code == 200
        assert any(
            t["versionId"] == v4 and t["role"] == "assistant" for t in retried.json()["turns"]
        )

        # Translations endpoint (per-turn persistence + verification)
        session_snapshot = retried.json()
        pending_turns = [t for t in session_snapshot["turns"] if t["language"] != "zh-CN"]
        turn_ids = [t["turnId"] for t in pending_turns]
        for turn in pending_turns:
            g = turn["guidance"]
            raw_cues = [
                f"提示:{i}"
                for i, cue in enumerate(g.get("uiCues") or [])
                if isinstance(cue, dict) and str(cue.get("text") or "").strip()
            ]
            has_structured = bool(
                raw_cues
                or str(g.get("followUpQuestion") or "").strip()
                or g.get("intentHypothesis")
                or isinstance(g.get("proposalOffer"), dict)
                or isinstance(g.get("proposalQuestion"), dict)
                or isinstance(g.get("disagreement"), dict)
                or g.get("coordinateLinks")
            )
            tr_item: dict[str, object] = {
                "turnId": turn["turnId"],
                "body": f"中文:{turn['content'][:40]}",
            }
            if has_structured:
                tr_item["uiCueTexts"] = raw_cues
            if str(g.get("followUpQuestion") or "").strip():
                tr_item["followUpQuestion"] = "中文追问？"
            if g.get("intentHypothesis"):
                tr_item["intentHypothesis"] = "中文意图"
            offer = g.get("proposalOffer")
            needs_verify = False
            if isinstance(offer, dict):
                if offer.get("summary"):
                    tr_item["proposalOfferSummary"] = "中文提案摘要"
                if offer.get("rationale"):
                    tr_item["proposalOfferRationale"] = "中文理由"
                pres = offer.get("proposalPresentation")
                if isinstance(pres, dict):
                    if pres.get("schemaVersion") == 2 and pres.get("reason"):
                        tr_item["proposalPresentationReason"] = "中文说明"
                    if pres.get("binaryCompact") and pres.get("verifiedActions"):
                        tr_item["proposalVerifiedActions"] = [
                            f"动作:{i}"
                            for i, a in enumerate(pres.get("verifiedActions") or [])
                            if str(a).strip()
                        ]
                        needs_verify = True
            if isinstance(g.get("proposalQuestion"), dict):
                pq_src = g["proposalQuestion"]
                tr_item["proposalQuestion"] = {
                    "question": "中文问题？",
                    "note": "说明" if pq_src.get("note") else "",
                    "choices": [
                        {
                            "choiceId": ch["choiceId"],
                            "label": f"选项{ch['letter']}",
                        }
                        for ch in (pq_src.get("choices") or [])
                    ],
                }
                needs_verify = True
            bundle.cocreation_transport.queue(
                json.dumps({"translations": [tr_item]}, ensure_ascii=False)
            )
            if needs_verify:
                bundle.cocreation_transport.queue(
                    json.dumps({"accepted": True, "reason": "Verified"})
                )
        tr = await client.post(
            f"/api/sessions/{sid}/translations/zh-CN",
            json={"turnIds": turn_ids},
        )
        assert tr.status_code == 200
        assert any("zh-CN" in t["translations"] for t in tr.json()["turns"])

        # Finalize + Intention
        fin = await client.post(
            f"/api/sessions/{sid}/finalize",
            json={"baseVersionId": v4, "idempotencyKey": "fin-1"},
        )
        assert fin.status_code == 200 and fin.json()["status"] == "awaiting_intention"

        intent = await client.post(
            f"/api/sessions/{sid}/intention",
            json={"content": "Focused on clear central push timing.", "idempotencyKey": "int-1"},
        )
        assert intent.status_code == 200
        assert intent.json()["status"] == "completed"
        assert (
            intent.json()["intention"]["content"]
            == "Focused on clear central push timing."
        )
        assert len(bundle.deepseek_transport.calls) == 0  # Strict Moonshot isolation!


@pytest.mark.asyncio
async def test_container_provider_transports_and_core_llm_error_mapping(
    app_bundle_factory, tmp_path
) -> None:
    from server.container import build_container
    from server.core.llm_client import LLMValidationError, UpstreamLLMError
    from server.settings import SokobanSettings

    settings = SokobanSettings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "provider.sqlite3",
        deepseek_api_key="ds-test-key-1234567890",
        cocreation_llm_api_key="sk-kimi-test-1234567890",
        cocreation_llm_base_url="https://api.moonshot.cn/v1",
        cocreation_llm_model="kimi-k2.6",
        reaper_enabled=False,
    )
    container = build_container(settings=settings)
    try:
        assert container.deepseek_transport.response_format_mode == "prompt_only"
        assert container.deepseek_transport.default_extra_body == {
            "thinking": {"type": "disabled"}
        }
        assert container.cocreation_transport.response_format_mode == "json_object"
        assert container.cocreation_transport.default_extra_body == {
            "thinking": {"type": "disabled"}
        }
        assert container.cocreation_transport.fixed_temperature == 0.6
        assert container.cocreation_transport.use_max_completion_tokens is True
    finally:
        await container.close()

    bundle = app_bundle_factory(
        cocreation_responses=[
            UpstreamLLMError(
                "UPSTREAM_REQUEST_REJECTED",
                "bad param sk-secret-token-123456",
                retryable=False,
                provider_status=400,
            )
        ]
    )
    async with bundle.client() as client:
        demo = await client.post(
            "/cocreation/api/demo-sessions",
            json={"language": "en", "idempotencyKey": "demo-err-1"},
        )
        assert demo.status_code == 200
        demo_payload = demo.json()
        sid = demo_payload["sessionId"]

        access_resp = await client.post(
            f"/cocreation/api/sessions/{sid}/browser-access",
            json={"bootstrapToken": extract_hash_param(demo_payload["launchUrl"], "bootstrap")},
        )
        assert access_resp.status_code == 200

        vid = demo_payload.get("currentVersionId")
        if not vid:
            locked = await client.patch(
                f"/cocreation/api/sessions/{sid}/language",
                json={"language": "en"},
            )
            assert locked.status_code == 200
            vid = locked.json()["currentVersionId"]

        assess = await client.post(
            f"/cocreation/api/sessions/{sid}/versions/{vid}/assessments",
            json={"idempotencyKey": "assess-err-1"},
        )
        assert assess.status_code == 503
        assert assess.headers.get("X-LLM-Attempts-Used") == "1"
        err_body = assess.json()
        assert err_body["detail"] == err_body["error"]["message"]
        assert err_body["error"]["code"] == "UPSTREAM_REQUEST_REJECTED"
        assert err_body["error"]["retryable"] is False
        assert "sk-secret-token-123456" not in err_body["error"]["message"]
        assert "[redacted]" in err_body["error"]["message"]

        # CoreLLMValidationError maps to HTTP 422 (never 502) so proxies preserve JSON payload
        bundle.cocreation_transport.queue(
            LLMValidationError(
                "BINARY_PREFLIGHT_NOT_FOUND",
                "no verified candidate after repair",
                attempts_used=2,
            ),
            LLMValidationError(
                "BINARY_PREFLIGHT_NOT_FOUND",
                "no verified candidate after repair",
                attempts_used=2,
            ),
        )
        val_err = await client.post(
            f"/cocreation/api/sessions/{sid}/messages",
            json={
                "content": "Discuss the wall layout",
                "baseVersionId": vid,
                "idempotencyKey": "val-err-422-1",
                "action": "send",
            },
        )
        assert val_err.status_code == 422
        assert val_err.headers.get("X-LLM-Attempts-Used") == "2"
        val_body = val_err.json()
        assert val_body["code"] == "BINARY_PREFLIGHT_NOT_FOUND"
        assert val_body["error"]["code"] == "BINARY_PREFLIGHT_NOT_FOUND"
        assert val_body["error"]["retryable"] is True
        assert val_body["requestId"] == val_err.headers.get("X-Request-ID")


@pytest.mark.asyncio
async def test_unity_detail_contract_attempts_header_no_cache_and_placeholder_secret(
    app_bundle_factory, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    import httpx
    from server.api.deps import ensure_session_shape
    from server.main import _room_reaper_loop, create_app
    from server.settings import SokobanSettings

    # 1. Placeholder TOKEN_SECRET from .env.example is flagged as default/insecure.
    webgl_dir = tmp_path / "webgl"
    (webgl_dir / "TemplateData").mkdir(parents=True)
    (webgl_dir / "index.html").write_text("<html>game</html>", encoding="utf-8")
    (webgl_dir / "TemplateData" / "sokoban-bridge.js").write_text("// bridge", encoding="utf-8")

    placeholder_settings = SokobanSettings(
        data_dir=tmp_path / "ph_data",
        db_path=tmp_path / "ph_data" / "ph.sqlite3",
        webgl_build_dir=webgl_dir,
        token_secret="replace-with-a-long-random-production-secret",
        reaper_enabled=False,
    )
    assert placeholder_settings.is_default_token_secret is True
    placeholder_app = create_app(settings=placeholder_settings)
    with caplog.at_level(logging.WARNING), TestClient(placeholder_app) as sync_client:
        ready_resp = sync_client.get("/ready")
        assert ready_resp.status_code == 200
        assert ready_resp.json()["tokenSecretConfigured"] is False

        for no_cache_path in (
            "/game",
            "/game/",
            "/game/index.html",
            "/game/TemplateData/sokoban-bridge.js",
        ):
            r = sync_client.get(no_cache_path, follow_redirects=False)
            assert r.status_code in (200, 307), (no_cache_path, r.status_code)
            assert r.headers.get("Cache-Control") == "no-cache, must-revalidate", no_cache_path

        not_found = sync_client.get("/does-not-exist")
        assert not_found.status_code == 404
        assert isinstance(not_found.json()["detail"], str)
        assert not_found.json()["detail"] == not_found.json()["message"]

        bad_req = sync_client.post("/api/demo-sessions", json={"language": "fr"})
        assert bad_req.status_code == 400
        assert isinstance(bad_req.json()["detail"], str)
        assert bad_req.json()["detail"] == bad_req.json()["message"]

    assert any("TOKEN_SECRET" in rec.message for rec in caplog.records)

    # 2. CoreLLMError exposes X-LLM-Attempts-Used when attempts_used > 0 and string `detail`.
    bundle = app_bundle_factory(
        deepseek_responses=[
            httpx.ReadTimeout("timeout 1"),
            httpx.ReadTimeout("timeout 2"),
        ]
    )
    async with bundle.client() as client:
        lp_err = await client.post(
            "/generate-level-plan",
            json={"ideaText": "Balanced central corridor", "sessionId": "lp-err-1"},
        )
        assert lp_err.status_code == 504
        assert lp_err.headers.get("X-LLM-Attempts-Used") == "2"
        body = lp_err.json()
        assert isinstance(body["detail"], str) and body["detail"] == body["message"]
        assert body["error"]["code"] == "UPSTREAM_TIMEOUT"

    # 3. ensure_session_shape incorporates level_versions.assessment_json when present.
    shaped = ensure_session_shape(
        {
            "versions": [
                {"versionId": "v1", "assessment": {"assistantTurnId": "t-migrated", "ok": True}},
                {"versionId": "v2", "assessment": {"solutionSummary": "manual review"}},
            ],
            "turns": [
                {
                    "turnId": "t-manual",
                    "versionId": "v2",
                    "role": "assistant",
                    "guidance": {"move": "clarify_intent"},
                }
            ],
        }
    )
    assert shaped["versions"][0]["openingTurnId"] == "t-migrated"
    assert shaped["versions"][1]["openingTurnId"] == "t-manual"
    assert [a["versionId"] for a in shaped["assessments"]] == ["v1", "v2"]
    assert shaped["assessments"][0]["assistantTurnId"] == "t-migrated"
    assert shaped["assessments"][1]["assistantTurnId"] == "t-manual"
    assert callable(_room_reaper_loop)


@pytest.mark.asyncio
async def test_router_passthrough_kwargs_and_reaper_calls_stage_sweeper(
    app_bundle_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from server.main import _room_reaper_loop

    bundle = app_bundle_factory()
    recorded: dict[str, dict[str, object]] = {}

    async def fake_send_message(*args, **kwargs):
        recorded["send_message"] = {"args": args, "kwargs": kwargs}
        return {"versions": [], "turns": []}

    async def fake_choose_binary(*args, **kwargs):
        recorded["choose_binary"] = {"args": args, "kwargs": kwargs}
        return {"versions": [], "turns": []}

    async def fake_cancel_binary(*args, **kwargs):
        recorded["cancel_binary"] = {"args": args, "kwargs": kwargs}
        return {"versions": [], "turns": []}

    async def fake_intent_feedback(*args, **kwargs):
        recorded["intent_feedback"] = {"args": args, "kwargs": kwargs}
        return {"versions": [], "turns": []}

    async def fake_retry_recovery(*args, **kwargs):
        recorded["retry_recovery"] = {"args": args, "kwargs": kwargs}
        return {"versions": [], "turns": []}

    async def fake_decide_proposal(*args, **kwargs):
        recorded["decide_proposal"] = {"args": args, "kwargs": kwargs}
        return {"versions": [], "turns": []}

    monkeypatch.setattr(bundle.container.message_orchestrator, "send_message", fake_send_message)
    monkeypatch.setattr(
        bundle.container.proposal_service, "choose_binary_choice", fake_choose_binary
    )
    monkeypatch.setattr(
        bundle.container.proposal_service, "cancel_binary_topic", fake_cancel_binary
    )
    monkeypatch.setattr(
        bundle.container.intent_service, "submit_intent_feedback", fake_intent_feedback
    )
    monkeypatch.setattr(bundle.container.recovery_service, "retry_recovery", fake_retry_recovery)
    monkeypatch.setattr(bundle.container.proposal_service, "decide_proposal", fake_decide_proposal)

    async with bundle.client() as client:
        r1 = await client.post(
            "/api/sessions/s1/messages",
            json={
                "content": "",
                "baseVersionId": "v1",
                "idempotencyKey": "k-msg",
                "action": "continue_challenge",
                "exitChallenge": True,
            },
        )
        assert r1.status_code == 200
        assert recorded["send_message"]["kwargs"]["exit_challenge"] is True

        r2 = await client.post(
            "/api/sessions/s1/proposal-discovery/choices",
            json={
                "topicId": "t1",
                "questionId": "q1",
                "choiceId": "c1",
                "baseVersionId": "v1",
                "displayLanguage": "en",
                "idempotencyKey": "k-ch",
            },
        )
        assert r2.status_code == 200
        assert recorded["choose_binary"]["kwargs"]["base_version_id"] == "v1"
        assert recorded["choose_binary"]["kwargs"]["display_language"] == "en"

        r3 = await client.post(
            "/api/sessions/s1/proposal-discovery/cancel",
            json={"topicId": "t1", "baseVersionId": "v1", "idempotencyKey": "k-can"},
        )
        assert r3.status_code == 200
        assert recorded["cancel_binary"]["kwargs"]["base_version_id"] == "v1"

        r4 = await client.post(
            "/api/sessions/s1/intent-hypotheses/h1/feedback",
            json={
                "action": "confirm",
                "sourceTurnId": "turn-9",
                "baseVersionId": "v1",
                "idempotencyKey": "k-int",
            },
        )
        assert r4.status_code == 200
        assert recorded["intent_feedback"]["kwargs"]["source_turn_id"] == "turn-9"

        r5 = await client.post(
            "/api/sessions/s1/component-recoveries/rec1/retry",
            json={"baseVersionId": "v1", "idempotencyKey": "k-rec"},
        )
        assert r5.status_code == 200
        assert recorded["retry_recovery"]["kwargs"]["base_version_id"] == "v1"
        assert recorded["retry_recovery"]["kwargs"]["deadline"] == pytest.approx(10.0)

        r6 = await client.post(
            "/api/sessions/s1/proposals/p1/decision",
            json={
                "decision": "reject",
                "baseVersionId": "v1",
                "idempotencyKey": "k-prop",
                "reason": "Too narrow",
            },
        )
        assert r6.status_code == 200
        assert recorded["decide_proposal"]["kwargs"]["reason"] == "Too narrow"

    # Verify _room_reaper_loop invokes stage_service.sweep_expired_operations()
    calls: list[str] = []

    async def fake_reap_rooms() -> int:
        calls.append("rooms")
        return 0

    async def fake_sweep_ops() -> None:
        calls.append("stage_sweep")

    sleep_count = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(bundle.container.online_room_service, "reap_expired_rooms", fake_reap_rooms)
    monkeypatch.setattr(
        bundle.container.stage_service, "sweep_expired_operations", fake_sweep_ops, raising=False
    )
    monkeypatch.setattr("server.main.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await _room_reaper_loop(bundle.container, 1.0)
    assert calls == ["rooms", "stage_sweep"]


@pytest.mark.asyncio
async def test_placeholders_secret_redaction_cookie_scheme_and_loopback_urls(
    tmp_path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from server.container import build_container
    from server.domain.errors import DomainError
    from server.main import create_app
    from server.settings import SokobanSettings

    # 1. Placeholder API keys & delete password + env alias secret collection
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-alias-secret-999999")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moonshot-alias-secret-888888")
    monkeypatch.setenv("COCREATION_LLM_API_KEY", "your_kimi_api_key_here")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "your_deepseek_api_key_here")
    monkeypatch.setenv("DASHBOARD_DELETE_PASSWORD", "replace-with-dashboard-delete-password")
    monkeypatch.setenv("COCREATION_TOKEN_SECRET", "tok-alias-secret-777777")

    loaded = SokobanSettings.from_env(load_dotenv_file=False)
    assert loaded.has_deepseek_config is False
    assert loaded.has_cocreation_llm_config is False
    assert (
        SokobanSettings(cocreation_llm_api_key="your_llm_api_key_here").has_cocreation_llm_config
        is False
    )
    secrets_collected = loaded.secret_values()
    assert "sk-kimi-alias-secret-999999" in secrets_collected
    assert "sk-moonshot-alias-secret-888888" in secrets_collected
    assert "tok-alias-secret-777777" in secrets_collected

    ph_settings = SokobanSettings(
        data_dir=tmp_path / "ph2_data",
        db_path=tmp_path / "ph2_data" / "ph2.sqlite3",
        deepseek_api_key="your_deepseek_api_key_here",
        cocreation_llm_api_key="your_kimi_api_key_here",
        dashboard_delete_password="replace-with-dashboard-delete-password",
        reaper_enabled=False,
    )
    container = build_container(settings=ph_settings)
    app = create_app(settings=ph_settings, container=container)

    with caplog.at_level(logging.WARNING), TestClient(app) as sync_client:
        ready = sync_client.get("/ready").json()
        assert ready["deepseekConfigured"] is False
        assert ready["cocreationLlmConfigured"] is False

        del_resp = sync_client.post(
            "/verify-dashboard-password",
            headers={"X-Delete-Password": "replace-with-dashboard-delete-password"},
        )
        assert del_resp.status_code == 503
        assert del_resp.json()["error"]["code"] == "DELETE_PASSWORD_NOT_CONFIGURED"

    assert any("DASHBOARD_DELETE_PASSWORD" in rec.message for rec in caplog.records)

    # 2. DomainError.details recursive redaction
    @app.get("/_test_domain_error_redaction")
    async def _raise_secret_domain_error() -> dict[str, str]:
        raise DomainError(
            "INTERNAL_TEST_FAILURE",
            "error.internal",
            retryable=True,
            details={
                "nested": {"hint": "leak sk-kimi-alias-secret-999999 here"},
                "items": ["bearer tok-alias-secret-777777", 42],
                "attemptsUsed": 1,
            },
            message="Failed with sk-moonshot-alias-secret-888888",
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as loopback_client:
        err_resp = await loopback_client.get("/_test_domain_error_redaction")
        assert err_resp.status_code == 503
        err_json = err_resp.json()
        dumped = json.dumps(err_json)
        assert "sk-kimi-alias-secret-999999" not in dumped
        assert "sk-moonshot-alias-secret-888888" not in dumped
        assert "tok-alias-secret-777777" not in dumped
        assert "[redacted]" in err_json["details"]["nested"]["hint"]
        assert err_json["details"]["attemptsUsed"] == 1

        # 3. Loopback launchUrl/playUrl rewrite + cookie ignores raw X-Forwarded-Proto
        demo = await loopback_client.post(
            "/api/demo-sessions", json={"language": "en", "idempotencyKey": "loop-demo-1"}
        )
        assert demo.status_code == 200
        demo_json = demo.json()
        assert demo_json["launchUrl"].startswith("http://127.0.0.1:8000/cocreation/#")

        access = await loopback_client.post(
            f"/api/sessions/{demo_json['sessionId']}/browser-access",
            json={"bootstrapToken": extract_hash_param(demo_json["launchUrl"], "bootstrap")},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert access.status_code == 200
        set_cookie = access.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie
        assert "; Secure" not in set_cookie and "; secure" not in set_cookie

        locked = await loopback_client.patch(
            f"/api/sessions/{demo_json['sessionId']}/language", json={"language": "en"}
        )
        assert locked.status_code == 200
        vid = locked.json()["currentVersionId"]

        pa = await loopback_client.post(
            f"/api/sessions/{demo_json['sessionId']}/versions/{vid}/play-attempts",
            json={"idempotencyKey": "loop-pa-1"},
        )
        assert pa.status_code == 200
        assert pa.json()["playUrl"].startswith("http://127.0.0.1:8000/game/?")

    # 4. HTTPS scheme sets Secure cookie
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://sokobanaidemo.top"
    ) as https_client:
        demo_https = await https_client.post(
            "/api/demo-sessions", json={"language": "en", "idempotencyKey": "https-demo-1"}
        )
        assert demo_https.status_code == 200
        assert demo_https.json()["launchUrl"].startswith("https://sokobanaidemo.top/cocreation/#")
        demo_https_json = demo_https.json()
        access_https = await https_client.post(
            f"/api/sessions/{demo_https_json['sessionId']}/browser-access",
            json={"bootstrapToken": extract_hash_param(demo_https_json["launchUrl"], "bootstrap")},
        )
        assert access_https.status_code == 200
        assert "Secure" in access_https.headers.get("set-cookie", "")
