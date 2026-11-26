"""Zero-paid-LLM unit and integration tests for rooms, study, and analytics services."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from server.application import (
    DashboardService,
    DGGuideService,
    LevelPlanService,
    OnlineRoomService,
    StudyRecordsService,
)
from server.core.clock import FakeClock
from server.core.llm_client import TransportResponse, UpstreamLLMError
from server.domain.errors import (
    AuthError,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from server.llm.pipelines.dg_guide import DGGuidePipeline
from server.llm.pipelines.level_plan import LevelPlanPipeline
from server.persistence import RoomsRepository, SQLiteDatabase, StudyRepository

VALID_ROWS: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

UNSOLVABLE_ROWS: list[str] = [
    "############",
    "#p.........#",
    "#..........#",
    "#..##......#",
    "#..#s#.....#",
    "#..###...t.#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]


def _base_plan() -> dict[str, Any]:
    return {
        "minSolutionSteps": 22,
        "maxSolutionSteps": 42,
        "minWaterAreas": 1,
        "maxWaterAreas": 2,
        "minWallObstacleBlocks": 2,
        "maxWallObstacleBlocks": 3,
        "minPushes": 10,
        "maxPushes": 22,
        "minReversePulls": 18,
        "maxReversePulls": 34,
        "style": "hard classic choke route",
        "archetype": "bottleneck_corridor",
        "targetLayout": "split_pair",
        "obstacleStyle": "side_choke",
        "waterStyle": "side_pool",
        "designNote": "Base two-box route.",
        "corridorPlacement": "none",
        "corridorWidth": 0,
        "corridorOrientation": "any",
        "corridorRole": "visual_only",
        "corridorPriority": "preferred",
    }


def _draft_payload() -> dict[str, Any]:
    return {
        "finalDifficulty": "Medium",
        "finalLayout": "Balanced",
        "q1Answer": "quick_start",
        "q2Answer": "easy_to_adjust",
        "q3Answer": "focused_area",
        "q4Answer": "short_routes",
        "aiReflection": "Got it, quick start and focused spots.",
        "aiDifficultyRationale": "I map quick start to Easy.",
        "aiLayoutRationale": "I map focused spots to Compact.",
        "aiRecommendedDifficulty": "Easy",
        "aiRecommendedLayout": "Compact",
        "aiRecommendationSource": "llm",
    }


class _FakeTransport:
    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> TransportResponse:
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return TransportResponse(content=item, finish_reason="stop")


# ---------------------------------------------------------------------------
# OnlineRoomService tests
# ---------------------------------------------------------------------------


async def test_online_room_lifecycle_hashing_solver_and_in_tx_sync(tmp_path: Path) -> None:
    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "rooms.sqlite3")
    repo = RoomsRepository(db)
    svc = OnlineRoomService(repo, clock=clock, room_ttl_seconds=120.0)

    p1_room = await svc.create_room(study_session_id="study-1", menu_started_flow=True)
    match_id = p1_room["matchId"]
    room_code = p1_room["roomCode"]
    p1_token = p1_room["playerToken"]
    assert p1_room["playerNumber"] == 1
    assert p1_room["status"] == "waiting_for_opponent"

    # Tokens are stored hashed (SHA-256), never plaintext in SQLite.
    stored = repo.get_room(match_id)
    assert stored is not None
    assert "token" not in stored.players[0]
    assert stored.players[0]["tokenHash"] != p1_token

    p2_room = await svc.join_room(
        room_code=room_code.lower(), study_session_id="study-2", menu_started_flow=True
    )
    p2_token = p2_room["playerToken"]
    assert p2_room["playerNumber"] == 2
    assert p2_room["status"] == "briefing"

    with pytest.raises(ConflictError):
        await svc.join_room(room_code=room_code, study_session_id="study-3")

    with pytest.raises(AuthError):
        await svc.get_room(match_id, "wrong-token")

    fetched = await svc.get_room(match_id, p1_token)
    assert fetched["matchId"] == match_id
    assert "playerToken" not in fetched

    await svc.set_ready(match_id, p1_token, ready=True)
    ready_state = await svc.set_ready(match_id, p2_token, ready=True)
    assert ready_state["status"] == "waiting_for_challenges"

    draft_res = await svc.record_draft(match_id, p1_token, _draft_payload())
    assert draft_res["pending"] is True

    # Unsolvable challenge is rejected before persisting.
    with pytest.raises(ValidationError):
        await svc.submit_challenge(match_id, p1_token, rows=UNSOLVABLE_ROWS)

    # Co-creation sync inside caller's write_tx (with solver NOT inside write_tx).
    with db.write_tx() as conn:
        first_ev, created = svc.record_cocreation_event_tx(
            conn,
            match_id=match_id,
            player_slot=1,
            event_payload={
                "eventId": "ev-first-1",
                "eventType": "first_stage",
                "sessionId": "cocreation-1",
                "versionId": "v1",
                "stageNumber": 1,
                "initialDraftMethod": "description_generation",
                "rows": VALID_ROWS,
            },
        )
        assert created is True
        assert first_ev["eventType"] == "first_stage"

        # Idempotent duplicate inside tx
        _, dup = svc.record_cocreation_event_tx(
            conn,
            match_id=match_id,
            player_slot=1,
            event_payload={
                "eventId": "ev-first-1",
                "eventType": "first_stage",
                "sessionId": "cocreation-1",
                "versionId": "v1",
                "stageNumber": 1,
                "initialDraftMethod": "description_generation",
                "rows": VALID_ROWS,
            },
        )
        assert dup is False

        # Node snapshot updates + intention sync
        svc.record_cocreation_event_tx(
            conn,
            match_id=match_id,
            player_slot=1,
            event_payload={
                "eventId": "ev-node-1",
                "eventType": "node",
                "sessionId": "cocreation-1",
                "nodeId": "node-p1",
                "nodeType": "proposal",
                "nodeStatus": "pending",
                "nodeEntries": [
                    {
                        "entryId": "e1",
                        "kind": "llm_message",
                        "text": "Initial proposal",
                        "occurredAt": "2026-01-01T00:00:05Z",
                    }
                ],
            },
        )
        synced = svc.record_designer_intention_tx(
            conn,
            match_id=match_id,
            player_slot=1,
            designer_intention="Keep the opening tight.",
            event_id="ev-intent-msg",
            session_id="cocreation-1",
        )
        assert synced["matchId"] == match_id
        with pytest.raises(ConflictError):
            svc.record_designer_intention_tx(
                conn,
                match_id=match_id,
                player_slot=1,
                designer_intention="Different intention",
            )
        svc.record_cocreation_event_tx(
            conn,
            match_id=match_id,
            player_slot=1,
            event_payload={
                "eventId": "ev-final-1",
                "eventType": "final",
                "sessionId": "cocreation-1",
                "versionId": "v1",
                "stageNumber": 1,
                "rows": VALID_ROWS,
                "coCreationDurationSeconds": 35.25,
            },
        )

    # Verify draft materialized and frozen check works.
    retry_draft = await svc.record_draft(match_id, p1_token, _draft_payload())
    assert retry_draft["event"]["finalDifficulty"] == "Medium"
    bad_draft = _draft_payload()
    bad_draft["finalDifficulty"] = "Hard"
    with pytest.raises(ConflictError):
        await svc.record_draft(match_id, p1_token, bad_draft)

    # Submit valid challenges & results.
    c1 = await svc.submit_challenge(match_id, p1_token, rows=VALID_ROWS)
    assert c1["status"] == "waiting_for_challenges"
    c2 = await svc.submit_challenge(match_id, p2_token, rows=VALID_ROWS)
    assert c2["status"] == "challenges_ready"
    assert c2["opponentChallengeMetadata"]["designerIntention"] == "Keep the opening tight."

    with pytest.raises(ValidationError):
        await svc.submit_result(
            match_id, p1_token, {"durationSeconds": 5.0, "moveCount": 2, "minimumMoves": 5}
        )
    r1 = await svc.submit_result(
        match_id,
        p1_token,
        {"durationSeconds": 10.5, "moveCount": 6, "minimumMoves": 5, "restartCount": 1},
    )
    assert r1["status"] == "waiting_for_results"
    r2 = await svc.submit_result(
        match_id,
        p2_token,
        {"durationSeconds": 14.5, "moveCount": 7, "minimumMoves": 5, "restartCount": 3},
    )
    assert r2["status"] == "results_ready"

    # Leaving after results_ready does not cancel the match.
    after_leave = await svc.leave_room(match_id, p1_token)
    assert after_leave["status"] == "results_ready"


async def test_online_room_leave_and_reaper(tmp_path: Path) -> None:
    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "reap.sqlite3")
    repo = RoomsRepository(db)
    svc = OnlineRoomService(repo, clock=clock, room_ttl_seconds=60.0)

    r1 = await svc.create_room(study_session_id="s1", menu_started_flow=True)
    r2 = await svc.join_room(room_code=r1["roomCode"], study_session_id="s2")
    cancelled = await svc.leave_room(r1["matchId"], r2["playerToken"])
    assert cancelled["status"] == "cancelled"

    r3 = await svc.create_room(study_session_id="s3", menu_started_flow=True)
    clock.advance(61.0)
    reaped = await svc.reap_expired_rooms()
    assert reaped == 2
    assert repo.get_room(r3["matchId"]) is None
    events = repo.list_room_events(r3["matchId"])
    assert any(ev.event_type == "room_expired" for ev in events)


async def test_submit_challenge_runs_solver_outside_write_tx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "tx_guard.sqlite3")
    repo = RoomsRepository(db)
    svc = OnlineRoomService(repo, clock=clock)

    r1 = await svc.create_room()
    r2 = await svc.join_room(room_code=r1["roomCode"])
    await svc.set_ready(r1["matchId"], r1["playerToken"], ready=True)
    await svc.set_ready(r1["matchId"], r2["playerToken"], ready=True)

    in_write_tx = False
    orig_write_tx = db.write_tx

    def tracked_write_tx():
        cm = orig_write_tx()

        class _Ctx:
            def __enter__(self) -> sqlite3.Connection:
                nonlocal in_write_tx
                in_write_tx = True
                return cm.__enter__()

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
                nonlocal in_write_tx
                try:
                    return cm.__exit__(exc_type, exc, tb)
                finally:
                    in_write_tx = False

        return _Ctx()

    monkeypatch.setattr(db, "write_tx", tracked_write_tx)
    import server.application.rooms.service as rooms_service_mod

    orig_solve = rooms_service_mod.solve_min_steps
    solve_saw_write_tx: list[bool] = []

    def spy_solve(*args: Any, **kwargs: Any) -> Any:
        solve_saw_write_tx.append(in_write_tx)
        return orig_solve(*args, **kwargs)

    monkeypatch.setattr(rooms_service_mod, "solve_min_steps", spy_solve)
    await svc.submit_challenge(r1["matchId"], r1["playerToken"], rows=VALID_ROWS)
    assert solve_saw_write_tx == [False]


# ---------------------------------------------------------------------------
# DGGuideService tests
# ---------------------------------------------------------------------------


async def test_dg_guide_service_en_zh_and_deterministic_fallback() -> None:
    clock = FakeClock()
    en_json = json.dumps(
        {
            "summary": (
                "Got it! I'd try a quick start with independent pushes. "
                "How about focused spots and short paths?"
            ),
            "recommendedDifficulty": "Easy",
            "difficultyRationale": (
                "I connect quick_start and easy_to_adjust to Easy planning complexity."
            ),
            "recommendedLayout": "Compact",
            "layoutRationale": "I connect focused_area and short_routes to a Compact layout.",
        }
    )
    en_pipe = DGGuidePipeline(_FakeTransport([en_json]), model="fake", clock=clock)
    en_svc = DGGuideService(en_pipe)
    en_res = await en_svc.create_summary(
        {
            "firstMovePreference": "quick_start",
            "pushPlanningPreference": "easy_to_adjust",
            "spacePreference": "focused_area",
            "routeRhythmPreference": "short_routes",
            "language": "en",
        },
        request_id="req-en",
    )
    assert en_res.source == "llm"
    assert en_res.value["summary"].startswith("Got it!")
    assert en_res.request_id == "req-en"

    # zh-CN LLM success (note space between sentences until dg_guide regex allows unspaced CJK)
    zh_json = json.dumps(
        {
            "summary": "明白，我先试试快速开局和独立推动。 咱们可以把关键位置集中在一个区域吗？",
            "recommendedDifficulty": "Easy",
            "difficultyRationale": "我将首步与推动偏好对应到简单规划复杂度。",
            "recommendedLayout": "Compact",
            "layoutRationale": "我将集中空间与短路径对应到紧凑布局。",
        },
        ensure_ascii=False,
    )
    zh_pipe = DGGuidePipeline(_FakeTransport([zh_json]), model="fake", clock=clock)
    zh_res = await DGGuideService(zh_pipe).create_summary(
        {
            "firstMovePreference": "quick_start",
            "pushPlanningPreference": "easy_to_adjust",
            "spacePreference": "focused_area",
            "routeRhythmPreference": "short_routes",
            "language": "zh-CN",
        }
    )
    assert zh_res.source == "llm"
    assert zh_res.value["summary"].startswith("明白")

    # Deterministic fallback with secret redaction & zh-CN support
    secret_err = UpstreamLLMError(
        "BAD", "boom api_key=sk-secret123456789", retryable=False
    )
    failing_pipe = DGGuidePipeline(
        _FakeTransport([secret_err]),
        model="fake",
        clock=clock,
    )
    fb_svc = DGGuideService(failing_pipe, extra_secrets=("sk-secret123456789",))
    fb_res = await fb_svc.create_summary(
        {
            "firstMovePreference": "no_preference",
            "pushPlanningPreference": "no_preference",
            "spacePreference": "no_preference",
            "routeRhythmPreference": "no_preference",
            "language": "zh-CN",
        }
    )
    assert fb_res.source == "deterministic_fallback"
    assert fb_res.value["recommendedDifficulty"] == "Random"
    assert fb_res.value["recommendedLayout"] == "Random"
    assert "明白" in fb_res.value["summary"]
    assert "sk-secret123456789" not in fb_res.value.get("fallbackReason", "")

    with pytest.raises(ValidationError):
        await en_svc.create_summary(
            {
                "firstMovePreference": "invalid_choice",
                "pushPlanningPreference": "easy_to_adjust",
                "spacePreference": "focused_area",
                "routeRhythmPreference": "short_routes",
            }
        )


# ---------------------------------------------------------------------------
# LevelPlanService tests
# ---------------------------------------------------------------------------


async def test_level_plan_ha_short_circuit_and_scoped_history() -> None:
    clock = FakeClock()
    plan_a = _base_plan()
    plan_b = {**_base_plan(), "archetype": "split_route"}
    transport = _FakeTransport([json.dumps(plan_b)])
    svc = LevelPlanService(
        LevelPlanPipeline(transport, model="fake", clock=clock),
        clock=clock,
        history_ttl_seconds=60.0,
    )

    ha_option = {
        "id": "A",
        "title": "Open Workshop",
        "description": "Use an open workshop layout.",
        "promptText": json.dumps(
            {"changes": {"archetype": "open_workshop"}, "preserveUnlisted": True}
        ),
    }
    ha_res = await svc.create_level_plan(
        {
            "sessionId": "session-A",
            "revisionMode": "ha",
            "previousLevelPlan": plan_a,
            "selectedHAPlan": json.dumps(ha_option),
        },
        request_id="req-ha",
    )
    assert ha_res.used_ha_short_circuit is True
    assert ha_res.attempts_used == 0
    assert ha_res.plan["archetype"] == "open_workshop"
    assert len(transport.calls) == 0  # Zero LLM calls on HA short-circuit!

    # Session B must NOT see Session A's blueprint history.
    assert "open_workshop" in svc.get_recent_blueprint_hint("sessionId:session-A")
    assert svc.get_recent_blueprint_hint("sessionId:session-B") == "none"

    llm_res = await svc.create_level_plan(
        {"sessionId": "session-B", "ideaText": "split two boxes"},
        request_id="req-llm",
    )
    assert llm_res.used_ha_short_circuit is False
    assert llm_res.plan["archetype"] == "split_route"
    assert len(transport.calls) == 1
    user_payload = json.loads(transport.calls[0]["messages"][1]["content"])
    assert user_payload["recentBlueprintHint"] == "none"

    # TTL expiry prunes history cleanly.
    clock.advance(61.0)
    assert svc.get_recent_blueprint_hint("sessionId:session-A") == "none"
    assert svc.get_recent_blueprint_hint("sessionId:session-B") == "none"


# ---------------------------------------------------------------------------
# StudyRecordsService & DashboardService tests
# ---------------------------------------------------------------------------


async def test_study_records_bounds_nan_rejection_and_export() -> None:
    clock = FakeClock()
    db = SQLiteDatabase(":memory:")
    study_repo = StudyRepository(db)
    svc = StudyRecordsService(study_repo, clock=clock, max_payload_bytes=512)

    s_res = await svc.record_survey({"participantId": "p1", "rating": 5})
    assert s_res["recordType"] == "survey"
    await svc.record_level_start({"studySessionId": "st-1", "roundId": "r1"})
    await svc.record_level_end({"studySessionId": "st-1", "roundId": "r1", "completed": True})
    with db.read_conn() as conn:
        rows = conn.execute(
            "SELECT match_id, participant_id FROM study_records "
            "WHERE record_type IN ('level_start', 'level_end')"
        ).fetchall()
        assert len(rows) == 2
        assert all(r["match_id"] is None and r["participant_id"] == "st-1" for r in rows)
    j_res = await svc.record_journey({"ideaId": "idea-1", "phase": "dg", "action": "confirm"})
    assert j_res["journeyEventId"]

    with pytest.raises(ValidationError):
        await svc.record_journey({"phase": "dg"})  # missing ideaId

    with pytest.raises(ValidationError):
        await svc.record_survey({"bad": float("nan")})

    with pytest.raises(ValidationError):
        await svc.record_survey({"blob": "x" * 600})

    all_lines = [json.loads(line) async for line in svc.export_jsonl(chunk_size=2)]
    assert len(all_lines) == 4
    survey_lines = [json.loads(line) async for line in svc.export_jsonl("survey")]
    assert len(survey_lines) == 1 and survey_lines[0]["participantId"] == "p1"


async def test_dashboard_service_projection_auth_and_deletes(tmp_path: Path) -> None:
    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "dash.sqlite3")
    rooms_repo = RoomsRepository(db)
    room_svc = OnlineRoomService(rooms_repo, clock=clock)
    dash = DashboardService(rooms_repo, delete_password="admin-pw", clock=clock)

    # Create match 1 WITH first_stage + node update + final + challenge + result.
    r1 = await room_svc.create_room(study_session_id="st-p1", menu_started_flow=True)
    r2 = await room_svc.join_room(
        room_code=r1["roomCode"], study_session_id="st-p2", menu_started_flow=True
    )
    m1 = r1["matchId"]
    await room_svc.set_ready(m1, r1["playerToken"], ready=True)
    await room_svc.set_ready(m1, r2["playerToken"], ready=True)

    with db.write_tx() as conn:
        room_svc.record_cocreation_event_tx(
            conn,
            match_id=m1,
            player_slot=1,
            event_payload={
                "eventId": "ev1",
                "eventType": "first_stage",
                "sessionId": "s1",
                "versionId": "v1",
                "stageNumber": 1,
                "initialDraftMethod": "partial_completion",
                "rows": VALID_ROWS,
            },
            now_iso="2026-01-01T00:01:00Z",
        )
        room_svc.record_cocreation_event_tx(
            conn,
            match_id=m1,
            player_slot=1,
            event_payload={
                "eventId": "ev2",
                "eventType": "node",
                "sessionId": "s1",
                "nodeId": "n1",
                "nodeType": "proposal",
                "nodeStatus": "pending",
                "nodeEntries": [
                    {
                        "entryId": "ne1",
                        "kind": "llm_message",
                        "text": "Draft",
                        "occurredAt": "2026-01-01T00:01:05Z",
                    }
                ],
            },
            now_iso="2026-01-01T00:01:05Z",
        )
        room_svc.record_cocreation_event_tx(
            conn,
            match_id=m1,
            player_slot=1,
            event_payload={
                "eventId": "ev3",
                "eventType": "node",
                "sessionId": "s1",
                "nodeId": "n1",
                "nodeType": "proposal",
                "nodeStatus": "accepted",
                "nodeEntries": [
                    {
                        "entryId": "ne1",
                        "kind": "llm_message",
                        "text": "Accepted",
                        "occurredAt": "2026-01-01T00:01:10Z",
                    }
                ],
            },
            now_iso="2026-01-01T00:01:10Z",
        )
        room_svc.record_cocreation_event_tx(
            conn,
            match_id=m1,
            player_slot=1,
            event_payload={
                "eventId": "ev4",
                "eventType": "final",
                "sessionId": "s1",
                "versionId": "v1",
                "stageNumber": 1,
                "rows": VALID_ROWS,
                "coCreationDurationSeconds": 50.0,
            },
            now_iso="2026-01-01T00:02:00Z",
        )

    await room_svc.submit_challenge(m1, r1["playerToken"], rows=VALID_ROWS)
    await room_svc.submit_challenge(m1, r2["playerToken"], rows=VALID_ROWS)
    await room_svc.submit_result(
        m1, r1["playerToken"], {"durationSeconds": 10.0, "moveCount": 5, "minimumMoves": 5}
    )
    await room_svc.submit_result(
        m1,
        r2["playerToken"],
        {"durationSeconds": 20.0, "moveCount": 6, "minimumMoves": 5, "restartCount": 4},
    )

    # Create match 2 WITHOUT first_stage; it must be excluded from dashboard projection.
    await room_svc.create_room(study_session_id="st-no-fs", menu_started_flow=True)

    proj = await dash.get_matchmaking_records()
    assert proj["summary"]["matchCount"] == 1
    assert proj["summary"]["completedCount"] == 1
    assert proj["summary"]["averageRunDurationSeconds"] == 15.0
    match_obj = proj["matches"][0]
    assert match_obj["status"] == "completed"
    p1_flow = match_obj["players"][0]["coCreationFlow"]
    nodes = [ev for ev in p1_flow if ev["eventType"] == "node"]
    assert len(nodes) == 1 and nodes[0]["nodeStatus"] == "accepted"
    finals = [ev for ev in p1_flow if ev["eventType"] == "final"]
    assert finals[0]["opponentRestartCount"] == 4
    assert match_obj["players"][0]["challenge"]["runResult"]["restartCount"] == 4

    # Password checks & delete/clear operations.
    assert dash.verify_delete_password("admin-pw") is True
    with pytest.raises(AuthError):
        dash.verify_delete_password("wrong")
    with pytest.raises(DomainError):
        DashboardService(rooms_repo, delete_password="").verify_delete_password("x")

    with pytest.raises(AuthError):
        await dash.delete_online_match(m1, supplied_password="wrong")
    with pytest.raises(NotFoundError):
        await dash.delete_online_match("missing-id", supplied_password="admin-pw")

    deleted = await dash.delete_online_match(m1, supplied_password="admin-pw")
    assert deleted["matchId"] == m1
    assert (await dash.get_matchmaking_records())["summary"]["matchCount"] == 0

    cleared = await dash.clear_matchmaking_records(supplied_password="admin-pw")
    assert cleared["cleared"] is True


# ---------------------------------------------------------------------------
# Regression tests: study participant_id, Stage assess/CAS/replay, read-model safety
# ---------------------------------------------------------------------------


async def test_study_participant_id_session_id_and_play_attempt_status_enum(
    tmp_path: Path,
) -> None:
    from server.domain.sessions import PlayAttemptStatus
    from server.persistence._study_import import import_study_logs

    assert {s.value for s in PlayAttemptStatus} == {
        "issued",
        "loaded",
        "started",
        "completed",
        "abandoned",
        "interrupted",
    }

    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "study_pid.sqlite3")
    study_repo = StudyRepository(db)
    svc = StudyRecordsService(study_repo, clock=clock)
    await svc.record_survey({"sessionId": "sess-pid-1", "q1": "ok"})
    with db.read_conn() as conn:
        row = conn.execute("SELECT participant_id FROM study_records").fetchone()
        assert row is not None and row["participant_id"] == "sess-pid-1"

    logs_dir = tmp_path / "study_logs"
    logs_dir.mkdir()
    (logs_dir / "survey_responses.jsonl").write_text(
        json.dumps({"sessionId": "imported-sess-9", "q1": 4}) + "\n",
        encoding="utf-8",
    )
    import_db = SQLiteDatabase(tmp_path / "study_import.sqlite3")
    stats = import_study_logs(import_db, logs_dir)
    assert stats["study_inserted"] == 1
    with import_db.read_conn() as conn:
        imp_row = conn.execute("SELECT participant_id FROM study_records").fetchone()
        assert imp_row is not None and imp_row["participant_id"] == "imported-sess-9"


async def test_read_model_fallback_strips_internal_choices_and_treats_ready_active(
    tmp_path: Path,
) -> None:
    from server.application.sessions.read_model.progress import build_proposal_flow_state
    from server.application.sessions.read_model.projections import build_proposal_state
    from server.core.grid import map_fingerprint
    from server.persistence.models import ConversationTurnRecord, LevelVersionRecord
    from server.persistence.repositories._mappers import row_to_session

    db = SQLiteDatabase(tmp_path / "rm_safety.sqlite3")
    with db.write_tx() as conn:
        conn.execute(
            "INSERT INTO design_sessions("
            "session_id, mode, language, language_locked, status, access_token_hash, "
            "integration_token_hash, bootstrap_token_hash, current_version_id, "
            "created_at, updated_at"
            ") VALUES ('s1', 'unity', 'en', 1, 'active', 'a', 'i', 'b', 'v1', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        s_row = conn.execute("SELECT * FROM design_sessions WHERE session_id = 's1'").fetchone()
    assert s_row is not None
    session_rec = row_to_session(s_row)

    from server.application.sessions.stage.entity_bindings import build_entity_bindings
    from server.persistence.models import SessionReadModel

    bindings = build_entity_bindings(VALID_ROWS, source="initial")
    ver = LevelVersionRecord(
        version_id="v1",
        session_id="s1",
        version_number=1,
        source="initial",
        summary="init",
        rows=list(VALID_ROWS),
        bindings=bindings,
        entity_bindings=bindings,
        created_at="2026-01-01T00:00:00Z",
    )
    read_model = SessionReadModel(session=session_rec, versions=[ver])

    # Partial topic_state missing `binding` triggers fallback path in build_proposal_flow_state
    topic_state = {
        "topicId": "top-1",
        "versionId": "v1",
        "status": "clarifying",
        "choices": [],
        "pendingQuestion": {
            "questionId": "q1",
            "topicId": "top-1",
            "baseVersionId": "v1",
            "number": 2,
            "question": "Which?",
            "note": "Pick",
            "choices": [{"choiceId": "c1", "letter": "A", "label": "Opt A"}],
            "_internalChoices": [{"choiceId": "c1", "cachedRows": list(VALID_ROWS)}],
        },
    }
    flow = build_proposal_flow_state(read_model, topic_state)
    assert flow["active"] is True
    assert flow["clarificationQuestionCount"] == 2
    assert "_internalChoices" not in (flow["pendingQuestion"] or {})
    assert "cachedRows" not in json.dumps(flow)

    # Proposal binding status='ready' is treated as actionable alongside 'active'
    turn = ConversationTurnRecord(
        turn_id="t1",
        session_id="s1",
        sequence_number=1,
        role="assistant",
        content="Proposal ready",
        language="en",
        guidance={"proposalOffer": {"summary": "Move wall"}},
        proposal_binding={
            "baseVersionId": "v1",
            "mapFingerprint": map_fingerprint(VALID_ROWS),
            "entityBindingFingerprint": bindings["bindingFingerprint"],
            "status": "ready",
            "executionBrief": {
                "requiredTransitions": [{"row": 2, "column": 2, "from": ".", "to": "#"}]
            },
        },
        version_id="v1",
        created_at="2026-01-01T00:00:00Z",
    )
    p_state = build_proposal_state(
        turn,
        session_status="active",
        current_version_id="v1",
        deadline_expired=False,
        versions_by_id={"v1": ver},
        contexts_by_version={"v1": {}},
        latest_revision_turn_by_version={"v1": "t1"},
    )
    assert p_state is not None and p_state["actionable"] is True


async def test_assess_version_human_edit_review_idempotency_sweep_and_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    from server.application.sessions import (
        CoCreationAppSettings,
        SessionLifecycleService,
        SessionReadModelService,
        StageService,
    )
    from server.core.grid import map_fingerprint
    from server.domain.errors import IdempotencyConflictError
    from server.llm.pipelines.chat import ChatPipeline
    from server.llm.pipelines.understanding import UnderstandingPipeline
    from server.persistence import (
        DialogueRepository,
        OperationalRepository,
        SessionsRepository,
    )

    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "stage_fixes.sqlite3")
    settings = CoCreationAppSettings(
        token_secret="unit-test-secret",
        cocreation_deadline_seconds=600,
        draft_regeneration_pending_timeout_seconds=60,
        draft_regeneration_claimed_timeout_seconds=120,
        interrupted_after_seconds=300,
    )
    rooms_repo = RoomsRepository(db)
    sessions_repo = SessionsRepository(db)
    dialogue_repo = DialogueRepository(db)
    op_repo = OperationalRepository(db)
    room_sync = OnlineRoomService(rooms_repo, clock=clock)
    read_model = SessionReadModelService(
        sessions_repo, dialogue_repo, op_repo, clock=clock, settings=settings
    )
    edited_rows = list(VALID_ROWS)
    edited_rows[4] = "#....#.....#"

    opening_resp = json.dumps(
        {
            "assistantMessage": "Stage 1 ready.",
            "move": "observe_stage",
            "followUpQuestion": None,
            "intentDecision": None,
            "intentConfidence": None,
            "revisionOffer": None,
            "disagreement": None,
            "uiCues": [],
            "coordinateLinks": [],
            "assessment": {
                "solutionSummary": "Solvable.",
                "difficultyOpinion": "Manageable.",
                "features": ["Open"],
                "suggestions": ["Test"],
                "satisfactionQuestion": None,
            },
            "designContextPatch": None,
        }
    )
    conflict_resp = json.dumps(
        {
            "comparisons": [
                {
                    "directionId": "dir-goal-1",
                    "relation": "conflict",
                    "explanation": "Adds a wall where open center was desired.",
                }
            ]
        }
    )
    review_resp = json.dumps(
        {
            "openingMessage": "Saved Stage 2 stays solvable.",
            "assessment": {
                "solutionSummary": "Solvable.",
                "difficultyOpinion": "Tighter center.",
                "features": ["Center wall"],
                "suggestions": ["Compare routes"],
                "satisfactionQuestion": None,
            },
            "reviewMessage": "This edit adds a center wall opposite to keeping center open.",
            "conflict": {
                "userPosition": "You confirmed keeping the center open.",
                "aiPosition": "The saved edit places a wall in the center.",
                "coreDisagreement": "Open center vs central wall.",
                "nextQuestion": "Keep the wall or restore the open center?",
            },
        }
    )
    transport = _FakeTransport(
        [
            opening_resp,
            UpstreamLLMError("UPSTREAM_TIMEOUT", "timeout", retryable=False),
            conflict_resp,
            review_resp,
        ]
    )
    chat_pipe = ChatPipeline(transport, model="kimi-fake", clock=clock)
    und_pipe = UnderstandingPipeline(transport, model="kimi-fake", clock=clock)

    lifecycle = SessionLifecycleService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
        demo_level_generator=lambda: SimpleNamespace(rows=tuple(VALID_ROWS), seed=1),
    )
    stage = StageService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        chat_pipeline=chat_pipe,
        understanding_pipeline=und_pipe,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
        demo_level_generator=lambda: SimpleNamespace(rows=tuple(edited_rows), seed=2),
    )

    # 1. Demo draft_ops: atomic regeneration + non-latest duplicate idempotency_key
    demo = await lifecycle.create_demo_session(language="en", idempotency_key="demo-a1")
    d_sid = demo["sessionId"]
    d_boot = parse_qs(urlparse(demo["launchUrl"]).fragment)["bootstrap"][0]
    d_tok = (await lifecycle.authorize_browser_access(d_sid, d_boot))["accessToken"]
    await stage.request_draft_regeneration(d_sid, d_tok, idempotency_key="dr-1")
    clock.advance(1.0)
    await stage.request_draft_regeneration(d_sid, d_tok, idempotency_key="dr-2")
    # Replaying non-latest 'dr-1' must NOT raise IntegrityError
    replay_dr1 = await stage.request_draft_regeneration(d_sid, d_tok, idempotency_key="dr-1")
    assert replay_dr1["draftPreview"]["generation"] == 3

    # 2. Formal session: complete_draft_regeneration CAS, sweep & read-time expiry
    room = await room_sync.create_room(study_session_id="st-1", menu_started_flow=True)
    await room_sync.join_room(
        room_code=room["roomCode"], study_session_id="st-2", menu_started_flow=True
    )
    created = await lifecycle.create_formal_session(
        rows=VALID_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="f-1",
        match_id=room["matchId"],
        player_number=1,
    )
    sid = created["sessionId"]
    int_tok = created["integrationToken"]
    boot = parse_qs(urlparse(created["launchUrl"]).fragment)["bootstrap"][0]
    access = (await lifecycle.authorize_browser_access(sid, boot))["accessToken"]

    r_pend = await stage.request_draft_regeneration(sid, access, idempotency_key="u-dr-1")
    req_id = r_pend["draftPreview"]["regenerationRequestId"]
    await stage.claim_draft_regeneration(sid, req_id, int_tok)
    clock.advance(125.0)  # Past claimed timeout (120s)
    # Read-time expiry via get_integration_status marks it timed_out
    int_status = await read_model.get_integration_status(sid, int_tok)
    assert int_status["draftRegeneration"]["status"] == "timed_out"
    with pytest.raises(ConflictError):
        await stage.complete_draft_regeneration(sid, req_id, int_tok, rows=edited_rows)
    assert sessions_repo.get_session(sid).draft_rows == list(VALID_ROWS)  # type: ignore[union-attr]

    # language.py draft_generation CAS check inside write_tx()
    stale_session = sessions_repo.get_session(sid)
    assert stale_session is not None
    sessions_repo.update_session(sid, draft_generation=99)
    with pytest.raises(ConflictError):
        from server.application.sessions.lifecycle.language import update_language_impl

        await update_language_impl(
            db=db,
            room_sync=room_sync,
            clock=clock,
            settings=settings,
            session=stale_session,
            language="en",
        )
    sessions_repo.update_session(sid, draft_generation=1)

    # Lock language -> Stage 1, assess Stage 1
    p1 = await lifecycle.update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]
    await stage.assess_version(sid, v1, access, idempotency_key="as-1")

    # Seed confirmed direction into v1 DesignContext so v2 inherits it for conflict adjudication
    with db.write_tx() as conn:
        ctx = {
            "schemaVersion": 6,
            "userGoals": [
                {
                    "id": "dir-goal-1",
                    "goal": "Keep center open",
                    "authority": "confirmed",
                    "status": "active",
                    "confidence": 1.0,
                }
            ],
        }
        conn.execute(
            "UPDATE level_versions SET design_context_json = ? WHERE version_id = ?",
            (json.dumps(ctx), v1),
        )

    # Save manual edit -> Stage 2
    p2 = await stage.save_version(
        sid,
        access,
        base_version_id=v1,
        rows=edited_rows,
        idempotency_key="save-v2",
        summary="Add center wall",
    )
    v2 = p2["currentVersionId"]

    # Play attempt CAS & sweep_expired_operations
    pa = await stage.create_play_attempt(sid, v2, access, idempotency_key="pa-cas")
    att_id = pa["attemptId"]
    ticket = parse_qs(urlparse(pa["playUrl"]).query)["cocreationPlay"][0]
    await stage.bootstrap_play_attempt(att_id, ticket)
    with pytest.raises(ConflictError):
        await stage.bootstrap_play_attempt(att_id, ticket)
    clock.advance(301.0)
    await stage.sweep_expired_operations()
    assert sessions_repo.get_play_attempt(att_id).status == "interrupted"  # type: ignore[union-attr]

    # First assess on v2 fails (UpstreamLLMError queued) -> emits reply_generation_failed
    with pytest.raises(UpstreamLLMError):
        await stage.assess_version(sid, v2, access, idempotency_key="as-v2")
    events_after_fail = dialogue_repo.list_events(sid)
    assert any(ev.event_type == "reply_generation_failed" for ev in events_after_fail)

    # In-progress lock blocks concurrent assess_version without burning LLM; stale (>150s) recovers
    op_repo.claim_key(
        sid,
        "assess_version",
        "as-v2-lock",
        request_hash=op_repo.get_key(sid, "assess_version", "as-v2").request_hash,  # type: ignore[union-attr]
        created_at=clock.utc_now(),
    )
    with pytest.raises(ConflictError):
        await stage.assess_version(sid, v2, access, idempotency_key="as-v2-lock")

    # Retry with same key 'as-v2' succeeds: commits 2 turns, activeDisagreement, audits, & node
    assessed_v2 = await stage.assess_version(sid, v2, access, idempotency_key="as-v2")
    v2_turns = [
        t
        for t in assessed_v2["turns"]
        if t["versionId"] == v2 and t["role"] == "assistant"
    ]
    assert len(v2_turns) == 2
    assert assessed_v2["interactionState"]["proposalBlocked"] is True
    assert assessed_v2["interactionState"]["disagreementId"] is not None

    ev_types = {ev.event_type for ev in dialogue_repo.list_events(sid)}
    assert {
        "reply_delivery_outcome",
        "human_edit_reviewed",
        "disagreement_started",
        "intent_progress_rewrite",
    }.issubset(ev_types)

    room_evs = rooms_repo.list_room_events(room["matchId"])
    assert any(
        ev.event_type == "node" and ev.payload.get("nodeType") == "manual_edit_review"
        for ev in room_evs
    )

    # Advance past deadline: completed replays of save_version & assess_version still succeed!
    clock.advance(700.0)
    replay_save = await stage.save_version(
        sid,
        access,
        base_version_id=v1,
        rows=edited_rows,
        idempotency_key="save-v2",
        summary="Add center wall",
    )
    assert replay_save["currentVersionId"] == v2
    replay_assess = await stage.assess_version(sid, v2, access, idempotency_key="as-v2")
    assert len([t for t in replay_assess["turns"] if t["versionId"] == v2]) == 2
    with pytest.raises(IdempotencyConflictError):
        await stage.save_version(
            sid,
            access,
            base_version_id=v1,
            rows=VALID_ROWS,
            idempotency_key="save-v2",
        )
    assert map_fingerprint(VALID_ROWS) != map_fingerprint(edited_rows)
    monkeypatch.undo()


async def test_stage_save_restore_finalize_guards_cleanup_sync_and_cancellation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    from server.application.sessions import (
        CoCreationAppSettings,
        SessionLifecycleService,
        SessionReadModelService,
        StageService,
    )
    from server.application.sessions.lifecycle.finalize import finalize_session_impl
    from server.application.sessions.read_model.projections import sanitize_turn_guidance
    from server.application.sessions.stage.stage_ops import (
        assess_version_impl,
        restore_version_impl,
    )
    from server.domain.memory.models import Disagreement
    from server.llm.pipelines.chat import ChatPipeline
    from server.persistence import DialogueRepository, OperationalRepository, SessionsRepository

    # 1. Disagreement.phase accepts "reason_pending" & non-standard grid fallback in projections
    dis = Disagreement.model_validate(
        {
            "status": "active",
            "subject": "ai_revision_challenge",
            "phase": "reason_pending",
            "challengeId": "chg-1",
        }
    )
    assert dis.phase == "reason_pending"
    sanitized = sanitize_turn_guidance(
        {"move": "observe_stage", "coordinateLinks": [{"label": "P", "row": 1, "column": 1}]},
        "Player at (1, 1)",
        ["####"],
        None,
    )
    assert "coordinateLinks" not in sanitized

    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "stage_guards.sqlite3")
    settings = CoCreationAppSettings(
        token_secret="unit-test-secret", cocreation_deadline_seconds=600
    )
    rooms_repo = RoomsRepository(db)
    sessions_repo = SessionsRepository(db)
    dialogue_repo = DialogueRepository(db)
    op_repo = OperationalRepository(db)
    room_sync = OnlineRoomService(rooms_repo, clock=clock)
    read_model = SessionReadModelService(
        sessions_repo, dialogue_repo, op_repo, clock=clock, settings=settings
    )
    lifecycle = SessionLifecycleService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
        demo_level_generator=lambda: SimpleNamespace(rows=tuple(VALID_ROWS), seed=1),
    )
    stage = StageService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
    )

    room = await room_sync.create_room(study_session_id="st-1", menu_started_flow=True)
    await room_sync.join_room(
        room_code=room["roomCode"], study_session_id="st-2", menu_started_flow=True
    )
    created = await lifecycle.create_formal_session(
        rows=VALID_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="f-guard-1",
        match_id=room["matchId"],
        player_number=1,
    )
    sid = created["sessionId"]
    boot = parse_qs(urlparse(created["launchUrl"]).fragment)["bootstrap"][0]
    access = (await lifecycle.authorize_browser_access(sid, boot))["accessToken"]
    p1 = await lifecycle.update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]

    # 2. Missing online_rooms row during co-creation sync logs warning without rolling back
    rooms_repo.delete_match(room["matchId"])
    caplog.set_level("WARNING")
    with db.write_tx() as conn:
        ev_res, ev_created = room_sync.record_cocreation_event_tx(
            conn,
            match_id=room["matchId"],
            player_slot=1,
            event_payload={"eventId": "ev-miss", "eventType": "stage"},
        )
        assert ev_res["ignored"] is True and ev_created is False
        int_res = room_sync.record_designer_intention_tx(
            conn,
            match_id=room["matchId"],
            player_slot=1,
            designer_intention="Keep it simple.",
        )
        assert int_res["ignored"] is True
    assert any("online_rooms row was not found" in rec.message for rec in caplog.records)

    # 3. save_version rejects clarifying topic with PROPOSAL_CHOICE_REQUIRED
    op_repo.upsert_topic(
        sid,
        "top-clarify",
        {"topicId": "top-clarify", "versionId": v1, "status": "clarifying"},
        clock.utc_now(),
    )
    edited_v2 = list(VALID_ROWS)
    edited_v2[4] = "#....#.....#"
    with pytest.raises(ConflictError) as exc_info:
        await stage.save_version(
            sid,
            access,
            base_version_id=v1,
            rows=edited_v2,
            idempotency_key="save-blocked",
        )
    assert exc_info.value.code == "PROPOSAL_CHOICE_REQUIRED"

    # Once topic leaves clarifying (e.g. planning), save_version succeeds and cleans up active state
    op_repo.upsert_topic(
        sid,
        "top-clarify",
        {"topicId": "top-clarify", "versionId": v1, "status": "planning"},
        clock.utc_now(),
    )
    dialogue_repo.insert_turn(
        turn_id="t-v1", session_id=sid, role="assistant", content="Offer",
        version_id=v1, request_id="r-v1", created_at=clock.utc_now(),
    )
    dialogue_repo.insert_proposal(
        proposal_id="prop-v1", session_id=sid, assistant_turn_id="t-v1",
        base_version_id=v1, status="pending", proposal={"summary": "old"},
        created_at=clock.utc_now(), idempotency_key="pk-v1",
    )
    dialogue_repo.upsert_challenge(
        challenge_id="chg-v1", session_id=sid, base_version_id=v1,
        source_proposal_turn_id="t-v1", challenge_turn_id="t-v1",
        status="awaiting_choice", created_at=clock.utc_now(),
    )

    p2 = await stage.save_version(
        sid,
        access,
        base_version_id=v1,
        rows=edited_v2,
        idempotency_key="save-v2-ok",
    )
    v2 = p2["currentVersionId"]
    assert dialogue_repo.get_proposal(sid, "prop-v1").status == "superseded"  # type: ignore[union-attr]
    assert dialogue_repo.get_challenge(sid, "chg-v1").status == "resolved"  # type: ignore[union-attr]
    assert op_repo.get_topic(sid, "top-clarify").state["status"] == "cancelled"  # type: ignore[union-attr]

    # 4. restore_version also cleans up pending proposals, unfinished challenges, and active topics
    op_repo.upsert_topic(
        sid,
        "top-v2",
        {"topicId": "top-v2", "versionId": v2, "status": "retry_pending"},
        clock.utc_now(),
    )
    dialogue_repo.insert_turn(
        turn_id="t-v2", session_id=sid, role="assistant", content="Offer 2",
        version_id=v2, request_id="r-v2", created_at=clock.utc_now(),
    )
    dialogue_repo.insert_proposal(
        proposal_id="prop-v2", session_id=sid, assistant_turn_id="t-v2",
        base_version_id=v2, status="pending", proposal={"summary": "v2 offer"},
        created_at=clock.utc_now(), idempotency_key="pk-v2",
    )
    dialogue_repo.upsert_challenge(
        challenge_id="chg-v2", session_id=sid, base_version_id=v2,
        source_proposal_turn_id="t-v2", challenge_turn_id="t-v2",
        status="awaiting_reason", created_at=clock.utc_now(),
    )
    p3 = await stage.restore_version(
        sid,
        v1,
        access,
        base_version_id=v2,
        idempotency_key="restore-v1",
    )
    v3 = p3["currentVersionId"]
    assert dialogue_repo.get_proposal(sid, "prop-v2").status == "superseded"  # type: ignore[union-attr]
    assert dialogue_repo.get_challenge(sid, "chg-v2").status == "resolved"  # type: ignore[union-attr]
    assert op_repo.get_topic(sid, "top-v2").state["status"] == "cancelled"  # type: ignore[union-attr]

    # 5. CancelledError marks idempotency keys as "failed" across save/restore/assess/finalize
    class _CancellingTransport:
        async def complete(self, **kwargs: Any) -> TransportResponse:
            raise asyncio.CancelledError()

    sess_obj = sessions_repo.get_session(sid)
    assert sess_obj is not None

    with pytest.MonkeyPatch.context() as mp:
        async def _cancel_solve(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError()

        mp.setattr("server.application.sessions.stage.service.solve_stage_async", _cancel_solve)
        mp.setattr("server.application.sessions.stage.stage_ops.solve_stage_async", _cancel_solve)
        mp.setattr(
            "server.application.sessions.lifecycle.finalize.solve_stage_async", _cancel_solve
        )

        with pytest.raises(asyncio.CancelledError):
            await stage.save_version(
                sid, access, base_version_id=v3, rows=edited_v2, idempotency_key="cancel-save"
            )
        assert op_repo.get_key(sid, "save_version", "cancel-save").status == "failed"  # type: ignore[union-attr]

        with pytest.raises(asyncio.CancelledError):
            await restore_version_impl(
                db=db,
                sessions_repo=sessions_repo,
                operational_repo=op_repo,
                room_sync=room_sync,
                clock=clock,
                session=sess_obj,
                version_id=v2,
                base_version_id=v3,
                idempotency_key="cancel-restore",
            )
        assert op_repo.get_key(sid, "restore_version", "cancel-restore").status == "failed"  # type: ignore[union-attr]

        cancel_pipe = ChatPipeline(_CancellingTransport(), model="fake", clock=clock)
        with pytest.raises(asyncio.CancelledError):
            await assess_version_impl(
                db=db,
                sessions_repo=sessions_repo,
                operational_repo=op_repo,
                chat_pipeline=cancel_pipe,
                room_sync=room_sync,
                clock=clock,
                session=sess_obj,
                version_id=v3,
                idempotency_key="cancel-assess",
            )
        assert op_repo.get_key(sid, "assess_version", "cancel-assess").status == "failed"  # type: ignore[union-attr]

        clock.advance(700.0)
        expired_sess = sessions_repo.get_session(sid)
        assert expired_sess is not None
        with pytest.raises(asyncio.CancelledError):
            await finalize_session_impl(
                db=db,
                operational_repo=op_repo,
                room_sync=room_sync,
                clock=clock,
                settings=settings,
                session=expired_sess,
                base_version_id=v3,
                idempotency_key="cancel-fin",
                rows=edited_v2,
            )
        assert op_repo.get_key(sid, "finalize", "cancel-fin").status == "failed"  # type: ignore[union-attr]

    # 6. finalize_session_impl ignores stale pending proposal on older base_version_id
    #    while still blocking pending proposal on current base_version_id
    room2 = await room_sync.create_room(study_session_id="st-f1", menu_started_flow=True)
    await room_sync.join_room(
        room_code=room2["roomCode"], study_session_id="st-f2", menu_started_flow=True
    )
    c2 = await lifecycle.create_formal_session(
        rows=VALID_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="f-guard-2",
        match_id=room2["matchId"],
        player_number=1,
    )
    sid2 = c2["sessionId"]
    boot2 = parse_qs(urlparse(c2["launchUrl"]).fragment)["bootstrap"][0]
    acc2 = (await lifecycle.authorize_browser_access(sid2, boot2))["accessToken"]
    p2_init = await lifecycle.update_language(sid2, acc2, language="en")
    sv1 = p2_init["currentVersionId"]
    p2_v2 = await stage.save_version(
        sid2, acc2, base_version_id=sv1, rows=edited_v2, idempotency_key="sv2"
    )
    sv2 = p2_v2["currentVersionId"]
    # Insert a stale pending proposal on old sv1 AND verify current sv2 pending still blocks
    dialogue_repo.insert_turn(
        turn_id="t-old", session_id=sid2, role="assistant", content="old offer",
        version_id=sv1, request_id="r-old", created_at=clock.utc_now(),
    )
    dialogue_repo.insert_proposal(
        proposal_id="prop-old-stale", session_id=sid2, assistant_turn_id="t-old",
        base_version_id=sv1, status="pending", proposal={"summary": "stale"},
        created_at=clock.utc_now(), idempotency_key="pk-old",
    )
    dialogue_repo.insert_turn(
        turn_id="t-cur", session_id=sid2, role="assistant", content="cur offer",
        version_id=sv2, request_id="r-cur", created_at=clock.utc_now(),
    )
    dialogue_repo.insert_proposal(
        proposal_id="prop-cur-pending", session_id=sid2, assistant_turn_id="t-cur",
        base_version_id=sv2, status="pending", proposal={"summary": "cur"},
        created_at=clock.utc_now(), idempotency_key="pk-cur",
    )
    s2_obj = sessions_repo.get_session(sid2)
    assert s2_obj is not None
    with pytest.raises(ConflictError) as fin_err:
        await finalize_session_impl(
            db=db,
            operational_repo=op_repo,
            room_sync=room_sync,
            clock=clock,
            settings=settings,
            session=s2_obj,
            base_version_id=sv2,
            idempotency_key="fin-blocked",
        )
    assert fin_err.value.code == "PENDING_PROPOSAL"
    dialogue_repo.decide_proposal(
        sid2, "prop-cur-pending", status="rejected", decision={"decision": "reject"},
        candidate_version_id=None, decided_at=clock.utc_now(),
    )
    # Finalizing sv2 now succeeds despite stale pending proposal on older sv1, and supersedes it
    await finalize_session_impl(
        db=db,
        operational_repo=op_repo,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
        session=s2_obj,
        base_version_id=sv2,
        idempotency_key="fin-ok",
    )
    assert dialogue_repo.get_proposal(sid2, "prop-old-stale").status == "superseded"  # type: ignore[union-attr]
