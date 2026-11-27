"""Tests for local Dashboard demo seed/clean/reset CLI tool."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from server.application import DashboardService
from server.core.clock import SYSTEM_CLOCK
from server.persistence import RoomsRepository, SQLiteDatabase, StudyRepository
from server.persistence.database import dump_json
from server.tools.seed_dashboard_demo import (
    SEED_PREFIX,
    ProductionGuardError,
    clean_seed_data,
    is_production_db_path,
    main,
    reset_demo_data,
    seed_demo_data,
)
from tests.conftest import BASE_ROWS


def _build_dashboard(db_path: Path) -> tuple[SQLiteDatabase, DashboardService]:
    db = SQLiteDatabase(db_path)
    db.initialize()
    return db, DashboardService(
        RoomsRepository(db),
        db=db,
        study_repo=StudyRepository(db),
        clock=SYSTEM_CLOCK,
    )


def _count_rows(db: SQLiteDatabase, sql: str, params: tuple[object, ...] = ()) -> int:
    with db.read_conn() as conn:
        row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])


def test_production_path_guard_refuses_without_force(tmp_path: Path) -> None:
    assert is_production_db_path("/opt/sokoban/server/data/sokoban.sqlite3") is True
    assert is_production_db_path(r"D:\opt\sokoban\sokoban.sqlite3") is True
    assert is_production_db_path(tmp_path / "local.sqlite3") is False

    with pytest.raises(ProductionGuardError):
        seed_demo_data("/opt/sokoban/server/data/sokoban.sqlite3")
    with pytest.raises(ProductionGuardError):
        clean_seed_data("/opt/sokoban/server/data/sokoban.sqlite3")

    exit_code = main(["reset", "--db", "/opt/sokoban/server/data/sokoban.sqlite3"])
    assert exit_code == 2


@pytest.mark.asyncio
async def test_seed_projects_into_dashboard_analytics_and_csv(tmp_path: Path) -> None:
    db_path = tmp_path / "seed_verify.sqlite3"
    inserted = seed_demo_data(db_path)
    assert inserted["online_rooms"] == 1
    assert inserted["design_sessions"] == 2
    assert inserted["study_records"] == 4

    _, dashboard = _build_dashboard(db_path)

    # 1. Dashboard /matchmaking-records-data projection (only ROOM SEED01)
    proj = await dashboard.get_matchmaking_records()
    assert proj["summary"]["matchCount"] == 1
    assert proj["summary"]["completedCount"] == 1
    assert proj["summary"]["inProgressCount"] == 0
    assert proj["summary"]["malformedCount"] == 0

    m1 = proj["matches"][0]
    assert m1["roomCode"] == "SEED01"
    assert m1["status"] == "completed"
    assert len(m1["surveys"]) == 4
    assert all(s["surveyVersion"] == "v2.0-surf-study" for s in m1["surveys"])
    post_by_slot = {
        s["playerSlot"]: s["payload"]["answers"]
        for s in m1["surveys"]
        if s["surveyId"] == "online_post_match_survey"
    }
    assert len(post_by_slot[1]) == 26
    assert len(post_by_slot[2]) == 19
    p1_ids = {a["questionId"] for a in post_by_slot[1]}
    p2_ids = {a["questionId"] for a in post_by_slot[2]}
    assert "demo_open_feedback" not in p1_ids
    assert "emo_frustration_disrupted_plan" not in p1_ids
    assert {"rel_comfort", "final_self_reported_intent", "ai_collaboration_reflection"}.issubset(
        p1_ids
    )
    assert "rel_comfort" not in p2_ids
    assert "final_self_reported_intent" in p2_ids
    assert "ai_collaboration_reflection" not in p2_ids
    m1_p1, m1_p2 = m1["players"][0], m1["players"][1]
    assert len(m1_p1["draftRegenerations"]) == 2
    assert len(m1_p1["playAttempts"]) == 2
    p1_node_types = {
        ev.get("nodeType") for ev in m1_p1["coCreationFlow"] if ev.get("eventType") == "node"
    }
    expected_nodes = {
        "discussion",
        "manual_edit_review",
        "llm_challenge",
        "proposal",
        "player_challenge",
    }
    assert expected_nodes.issubset(p1_node_types)
    assert m1_p1["challenge"]["runResult"]["outcome"] == "timed_out"
    assert m1_p2["challenge"]["runResult"]["outcome"] == "completed"

    # 2. Survey analytics scopes (all 4 seeded surveys belong to SEED01)
    active_analytics = await dashboard.get_survey_analytics(scope="active_matches")
    assert active_analytics["scopeCounts"] == {
        "active_matches": 4,
        "formal": 4,
        "standalone": 0,
        "all": 4,
    }
    assert active_analytics["totalResponses"] == 4

    # 3. CSV export includes pre-match fallback match_id/room_code/player_slot
    csv_active, _ = await dashboard.export_surveys_csv(scope="active_matches")
    assert csv_active.startswith("\ufeff")
    rows_active = list(csv.DictReader(io.StringIO(csv_active.lstrip("\ufeff"))))
    assert len(rows_active) == 4
    pre_m1_p1 = next(r for r in rows_active if r["response_id"] == "seed-demo-resp-m1-p1-pre")
    assert pre_m1_p1["match_id"] == "seed-demo-match-01"
    assert pre_m1_p1["room_code"] == "SEED01"
    assert pre_m1_p1["player_slot"] == "1"
    assert pre_m1_p1["participant_id"] == "G01-A"
    post_m1_p2 = next(r for r in rows_active if r["response_id"] == "seed-demo-resp-m1-p2-post")
    assert post_m1_p2["participant_id"] == "G01-B"


def test_clean_removes_only_seed_rows_and_reset_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "seed_isolation.sqlite3"
    db = SQLiteDatabase(db_path)
    db.initialize()

    # Insert real/non-seed records across rooms, events, sessions, child tables, and study_records
    with db.write_tx() as conn:
        conn.execute(
            "INSERT INTO online_rooms("
            "id, room_code, status, config_json, players_json, created_at, expires_at, updated_at"
            ") VALUES ('real-match-1', 'REAL01', 'waiting', '{}', '[]', "
            "'2026-11-27T08:00:00Z', '2026-11-28T08:00:00Z', '2026-11-27T08:00:00Z')"
        )
        conn.execute(
            "INSERT INTO online_room_events("
            "match_id, player_slot, seq, event_type, payload_json, created_at"
            ") VALUES ('real-match-1', 1, 1, 'room_created', ?, '2026-11-27T08:00:00Z')",
            (dump_json({"eventType": "room_created", "matchId": "real-match-1"}),),
        )
        conn.execute(
            "INSERT INTO design_sessions("
            "session_id, match_id, player_slot, mode, language, status, "
            "access_token_hash, integration_token_hash, bootstrap_token_hash, "
            "is_demo, created_at, updated_at"
            ") VALUES ('real-co-1', 'real-match-1', 1, 'partial_completion', 'en', 'active', "
            "'h1', 'h2', 'h3', 0, '2026-11-27T08:00:00Z', '2026-11-27T08:00:00Z')"
        )
        conn.execute(
            "INSERT INTO level_versions("
            "version_id, session_id, version_number, source, summary, rows_json, diff_json, "
            "solver_json, created_at"
            ") VALUES ('real-ver-1', 'real-co-1', 1, 'initial', 'Real stage', ?, '[]', "
            "'{\"solvable\": true}', '2026-11-27T08:00:00Z')",
            (dump_json(BASE_ROWS),),
        )
        conn.execute(
            "INSERT INTO study_records("
            "record_type, match_id, participant_id, round_id, payload_json, created_at"
            ") VALUES ('survey', 'real-match-1', 'G01-A', 'online_pre_match_survey', ?, "
            "'2026-11-27T08:01:00Z')",
            (
                dump_json(
                    {
                        "responseId": "real-resp-1",
                        "studySessionId": "real-study-1",
                        "participantId": "G01-A",
                    }
                ),
            ),
        )

    # Run seed -> seed -> reset -> reset (idempotency check)
    assert main(["seed", "--db", str(db_path)]) == 0
    assert main(["seed", "--db", str(db_path)]) == 0
    assert main(["reset", "--db", str(db_path)]) == 0
    assert main(["reset", "--db", str(db_path)]) == 0

    assert _count_rows(db, "SELECT COUNT(*) FROM online_rooms") == 2
    assert _count_rows(db, "SELECT COUNT(*) FROM design_sessions") == 3
    assert _count_rows(db, "SELECT COUNT(*) FROM study_records") == 5
    assert (
        _count_rows(
            db,
            "SELECT COUNT(*) FROM study_records "
            "WHERE json_extract(payload_json, '$.responseId') LIKE ?",
            (f"{SEED_PREFIX}%",),
        )
        == 4
    )

    # Clean via reset summary & clean CLI
    res = reset_demo_data(db_path)
    assert json.loads(dump_json(res))["seeded"]["study_records"] == 4
    assert main(["clean", "--db", str(db_path)]) == 0

    # Zero seed rows remain anywhere, while ALL real rows are preserved verbatim
    assert _count_rows(db, "SELECT COUNT(*) FROM online_rooms") == 1
    assert _count_rows(db, "SELECT COUNT(*) FROM online_rooms WHERE id = 'real-match-1'") == 1
    assert _count_rows(db, "SELECT COUNT(*) FROM online_room_events") == 1
    assert _count_rows(db, "SELECT COUNT(*) FROM design_sessions") == 1
    assert _count_rows(db, "SELECT COUNT(*) FROM level_versions") == 1
    assert _count_rows(db, "SELECT COUNT(*) FROM audit_events") == 0
    assert _count_rows(db, "SELECT COUNT(*) FROM play_attempts") == 0
    assert _count_rows(db, "SELECT COUNT(*) FROM draft_regenerations") == 0
    assert _count_rows(db, "SELECT COUNT(*) FROM binary_discovery_topics") == 0
    assert _count_rows(db, "SELECT COUNT(*) FROM revision_challenges") == 0
    assert _count_rows(db, "SELECT COUNT(*) FROM change_proposals") == 0
    assert _count_rows(db, "SELECT COUNT(*) FROM conversation_turns") == 0
    assert _count_rows(db, "SELECT COUNT(*) FROM study_records") == 1
    assert (
        _count_rows(db, "SELECT COUNT(*) FROM study_records WHERE match_id = 'real-match-1'") == 1
    )
