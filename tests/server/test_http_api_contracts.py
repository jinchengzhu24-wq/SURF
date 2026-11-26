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
    make_translation_json,
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

        # Question & intent-hypothesis feedback
        v3_ctx = next(pc for pc in msg1.json()["progressContexts"] if pc["versionId"] == v3)
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
        prop_id = chosen.json()["proposals"][0]["proposalId"]
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

        # Translations endpoint
        session_snapshot = retried.json()
        turn_ids = [t["turnId"] for t in session_snapshot["turns"] if t["language"] != "zh-CN"]
        bundle.cocreation_transport.queue(
            make_translation_json(session_snapshot, "zh-CN"),
            json.dumps({"accepted": True, "reason": "Verified"}),
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
