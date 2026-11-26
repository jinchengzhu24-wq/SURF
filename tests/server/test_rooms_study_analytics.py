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
