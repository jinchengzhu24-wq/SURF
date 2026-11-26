"""End-to-end 2-player formal study flow test across matchmaking, DG, co-creation, and sync."""

from __future__ import annotations

import json

import pytest

from tests.conftest import (
    ALT_ROWS,
    BASE_ROWS,
    extract_hash_param,
    extract_query_param,
    make_binary_ask_json,
    make_binary_plan_json,
    make_dg_summary_json,
    make_map_ops_json,
    make_opening_json,
)


@pytest.mark.asyncio
async def test_full_two_player_formal_study_journey_and_in_process_acid_sync(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory(deepseek_responses=[make_dg_summary_json("en")])
    async with bundle.client() as client:
        # 1. Player 1 creates online room; Player 2 joins; both ready up
        p1_room = (
            await client.post(
                "/online/rooms", json={"studySessionId": "study-p1", "menuStartedFlow": True}
            )
        ).json()
        match_id = p1_room["matchId"]
        room_code = p1_room["roomCode"]
        p1_token = p1_room["playerToken"]

        p2_room = (
            await client.post(
                "/online/rooms/join",
                json={"roomCode": room_code, "studySessionId": "study-p2", "menuStartedFlow": True},
            )
        ).json()
        p2_token = p2_room["playerToken"]

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

        # 2. Pre-study survey & DG summary (DeepSeek only)
        assert (
            await client.post(
                "/record-survey-response",
                json={"matchId": match_id, "playerNumber": 1, "phase": "pre", "score": 5},
            )
        ).status_code == 200

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
        assert dg.status_code == 200 and dg.headers["X-DG-Guide-Source"] == "llm"
        deepseek_calls_after_dg = len(bundle.deepseek_transport.calls)
        assert deepseek_calls_after_dg == 1

        # 3. Record DG drafts for both players
        draft_payload = {
            "finalDifficulty": "Medium",
            "finalLayout": "Balanced",
            "q1Answer": "observe_then_decide",
            "q2Answer": "consider_order",
            "q3Answer": "connected_areas",
            "q4Answer": "occasional_detours",
            "aiReflection": dg.json()["summary"],
            "aiRecommendedDifficulty": dg.json()["recommendedDifficulty"],
            "aiRecommendedLayout": dg.json()["recommendedLayout"],
            "aiRecommendationSource": "llm",
        }
        assert (
            await client.post(
                f"/online/rooms/{match_id}/draft",
                json=draft_payload,
                headers={"X-Player-Token": p1_token},
            )
        ).status_code == 200
        assert (
            await client.post(
                f"/online/rooms/{match_id}/draft",
                json=draft_payload,
                headers={"X-Player-Token": p2_token},
            )
        ).status_code == 200

        # 4. Player 1 enters formal co-creation session & authorizes browser access
        created = (
            await client.post(
                "/api/sessions",
                json={
                    "rows": BASE_ROWS,
                    "initialDraftMethod": "partial_completion",
                    "language": "en",
                    "idempotencyKey": "e2e-formal-p1",
                    "matchId": match_id,
                    "playerNumber": 1,
                },
            )
        ).json()
        sid = created["sessionId"]
        assert (
            await client.post(
                f"/api/sessions/{sid}/browser-access",
                json={"bootstrapToken": extract_hash_param(created["launchUrl"], "bootstrap")},
            )
        ).status_code == 200

        # 5. Language lock -> Stage 1, then assessment & manual Stage 2 save
        locked = (
            await client.patch(f"/api/sessions/{sid}/language", json={"language": "en"})
        ).json()
        v1 = locked["currentVersionId"]

        bundle.cocreation_transport.queue(make_opening_json("Stage 1 looks solid."))
        assessed = (
            await client.post(
                f"/api/sessions/{sid}/versions/{v1}/assessments",
                json={"idempotencyKey": "e2e-assess-1"},
            )
        ).json()
        assert assessed["assessments"]

        manual_rows = list(BASE_ROWS)
        manual_rows[7] = "#....#.....#"
        saved_resp = await client.post(
            f"/api/sessions/{sid}/versions",
            json={
                "rows": manual_rows,
                "baseVersionId": v1,
                "idempotencyKey": "e2e-save-2",
                "summary": "Manual edit before binary discovery",
            },
        )
        assert saved_resp.status_code == 200, saved_resp.text
        saved = saved_resp.json()
        v2 = saved["currentVersionId"]
        assert v2 != v1

        # 6. Policy-2 binary discovery -> choice -> accept proposal -> Stage 3
        bundle.cocreation_transport.queue(
            make_binary_ask_json,
            make_binary_plan_json(),
            make_map_ops_json(),
        )
        asked = (
            await client.post(
                f"/api/sessions/{sid}/messages",
                json={
                    "content": "Make center simpler",
                    "baseVersionId": v2,
                    "idempotencyKey": "e2e-bin-msg",
                    "requestProposal": True,
                    "action": "send",
                },
            )
        ).json()
        pq = next(
            t["guidance"]["proposalQuestion"]
            for t in reversed(asked["turns"])
            if t["guidance"].get("proposalQuestion")
        )
        planned = (
            await client.post(
                f"/api/sessions/{sid}/proposal-discovery/choices",
                json={
                    "topicId": pq["topicId"],
                    "questionId": pq["questionId"],
                    "choiceId": pq["choices"][0]["choiceId"],
                    "baseVersionId": v2,
                    "idempotencyKey": "e2e-bin-choice",
                },
            )
        ).json()
        prop_id = planned["proposals"][0]["proposalId"]
        accepted = (
            await client.post(
                f"/api/sessions/{sid}/proposals/{prop_id}/decision",
                json={"decision": "accept", "baseVersionId": v2, "idempotencyKey": "e2e-prop-acc"},
            )
        ).json()
        v3 = accepted["currentVersionId"]
        assert v3 not in {v1, v2}

        # 7. Playtest Stage 3 before finalizing
        pa = (
            await client.post(
                f"/api/sessions/{sid}/versions/{v3}/play-attempts",
                json={"idempotencyKey": "e2e-pa"},
            )
        ).json()
        boot = (
            await client.post(
                f"/api/play-attempts/{pa['attemptId']}/bootstrap",
                json={"ticket": extract_query_param(pa["playUrl"], "cocreationPlay")},
            )
        ).json()
        att_tok = boot["attemptToken"]
        assert (
            await client.post(
                f"/api/play-attempts/{pa['attemptId']}/start", json={"attemptToken": att_tok}
            )
        ).status_code == 200
        assert (
            await client.post(
                f"/api/play-attempts/{pa['attemptId']}/progress",
                json={"attemptToken": att_tok, "moveCount": 5, "pushCount": 1},
            )
        ).status_code == 200
        assert (
            await client.post(
                f"/api/play-attempts/{pa['attemptId']}/complete",
                json={
                    "attemptToken": att_tok,
                    "durationSeconds": 7.0,
                    "moveCount": 9,
                    "pushCount": 2,
                },
            )
        ).status_code == 200

        # 8. Finalize Stage & submit Designer Intention
        finalized = (
            await client.post(
                f"/api/sessions/{sid}/finalize",
                json={"baseVersionId": v3, "idempotencyKey": "e2e-finalize"},
            )
        ).json()
        assert finalized["status"] == "awaiting_intention"
        final_rows = next(
            v["rows"]
            for v in finalized["versions"]
            if v["versionId"] == finalized["finalVersionId"]
        )

        intention = (
            await client.post(
                f"/api/sessions/{sid}/intention",
                json={
                    "content": "Opened the center wall to keep pushing rhythmic.",
                    "idempotencyKey": "e2e-intention",
                },
            )
        ).json()
        assert intention["status"] == "completed"
        assert intention["intention"]["content"].startswith("Opened the center wall")
        assert len(bundle.deepseek_transport.calls) == deepseek_calls_after_dg

        # 9. Verify in-process ACID sync immediately in GET /matchmaking-records-data!
        records_data = (await client.get("/matchmaking-records-data")).json()
        match_record = next(m for m in records_data["matches"] if m["matchId"] == match_id)
        p1_record = match_record["players"][0]
        p1_flow = p1_record["coCreationFlow"]
        assert p1_record["designerIntention"] == "Opened the center wall to keep pushing rhythmic."
        assert any(ev.get("eventType") == "first_stage" for ev in p1_flow)
        assert any(
            ev.get("eventType") == "final" and ev.get("rows") == final_rows for ev in p1_flow
        )
        assert any(ev.get("eventType") == "turn" for ev in p1_flow)

        # 10. Study telemetry + JSONL export + Cross-player Challenge & Result
        assert (
            await client.post(
                "/record-level-start",
                json={"matchId": match_id, "playerNumber": 2, "levelVersionId": v3},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/record-level-end",
                json={"matchId": match_id, "playerNumber": 2, "durationSeconds": 12.4},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/record-journey-event",
                json={"matchId": match_id, "ideaId": sid, "phase": "challenge_complete"},
            )
        ).status_code == 200

        jsonl_lines = [
            json.loads(line)
            for line in (await client.get("/export/study-logs.jsonl")).text.strip().splitlines()
        ]
        assert len(jsonl_lines) == 4

        assert (
            await client.post(
                f"/online/rooms/{match_id}/challenge",
                json={"rows": final_rows, "aiAssistantMode": "description_generation"},
                headers={"X-Player-Token": p1_token},
            )
        ).status_code == 200
        assert (
            await client.post(
                f"/online/rooms/{match_id}/challenge",
                json={"rows": ALT_ROWS, "aiAssistantMode": "description_generation"},
                headers={"X-Player-Token": p2_token},
            )
        ).status_code == 200

        assert (
            await client.post(
                f"/online/rooms/{match_id}/result",
                json={
                    "durationSeconds": 10.2,
                    "moveCount": 14,
                    "minimumMoves": 12,
                    "restartCount": 0,
                },
                headers={"X-Player-Token": p1_token},
            )
        ).status_code == 200
        p2_res = await client.post(
            f"/online/rooms/{match_id}/result",
            json={"durationSeconds": 12.4, "moveCount": 16, "minimumMoves": 12, "restartCount": 1},
            headers={"X-Player-Token": p2_token},
        )
        assert p2_res.status_code == 200
        assert p2_res.json()["status"] == "results_ready"

        final_dashboard = (await client.get("/matchmaking-records-data")).json()
        completed_match = next(m for m in final_dashboard["matches"] if m["matchId"] == match_id)
        assert completed_match["status"] == "completed"
