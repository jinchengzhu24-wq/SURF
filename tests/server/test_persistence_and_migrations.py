"""Unit and integration tests for unified SQLite persistence and migrations."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from server.persistence import (
    SCHEMA_VERSION,
    ConflictError,
    DialogueRepository,
    NotFoundError,
    OperationalRepository,
    RoomsRepository,
    SessionsRepository,
    SQLiteDatabase,
    StudyRepository,
)


def _legacy_schema_sql() -> str:
    return """
    CREATE TABLE design_sessions (
        id TEXT PRIMARY KEY, creation_key TEXT NOT NULL UNIQUE,
        access_hash TEXT NOT NULL, integration_hash TEXT NOT NULL,
        bootstrap_hash TEXT NOT NULL, bootstrap_used_at TEXT,
        demo_mode INTEGER NOT NULL DEFAULT 0, deadline_started_at TEXT,
        deadline_at TEXT, match_id TEXT, player_number INTEGER,
        initial_draft_method TEXT NOT NULL, language TEXT NOT NULL,
        language_locked_at TEXT, status TEXT NOT NULL,
        current_version_id TEXT, final_version_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        finalized_at TEXT, completed_at TEXT,
        draft_rows_json TEXT, draft_validation_json TEXT,
        draft_generation INTEGER NOT NULL DEFAULT 1,
        draft_regeneration_status TEXT, draft_regeneration_request_id TEXT,
        draft_regeneration_key TEXT, draft_regeneration_requested_at TEXT,
        draft_regeneration_updated_at TEXT, draft_regeneration_failure_code TEXT
    );
    CREATE TABLE level_versions (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, stage_number INTEGER NOT NULL,
        parent_version_id TEXT, source TEXT NOT NULL, rows_json TEXT NOT NULL,
        summary TEXT NOT NULL, diff_json TEXT NOT NULL, validation_json TEXT NOT NULL,
        design_context_json TEXT, entity_bindings_json TEXT,
        idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE conversation_turns (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence_number INTEGER NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, language TEXT NOT NULL,
        version_id TEXT NOT NULL, request_id TEXT, model TEXT,
        attempts_used INTEGER, latency_ms INTEGER, guidance_json TEXT,
        proposal_binding_json TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE turn_translations (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
        language TEXT NOT NULL, body TEXT NOT NULL, guidance_json TEXT,
        proposal_summary TEXT, model TEXT, attempts_used INTEGER,
        latency_ms INTEGER, created_at TEXT NOT NULL
    );
    CREATE TABLE llm_assessments (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, version_id TEXT NOT NULL UNIQUE,
        assistant_turn_id TEXT NOT NULL, payload_json TEXT NOT NULL,
        prompt_version TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE change_proposals (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, base_version_id TEXT NOT NULL,
        proposed_rows_json TEXT, summary TEXT NOT NULL, diff_json TEXT NOT NULL,
        validation_json TEXT NOT NULL, status TEXT NOT NULL,
        assistant_turn_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        created_at TEXT NOT NULL, decided_at TEXT
    );
    CREATE TABLE designer_decisions (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, version_id TEXT,
        proposal_id TEXT, decision_type TEXT NOT NULL, reason TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE play_attempts (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, version_id TEXT NOT NULL,
        initial_draft_method TEXT NOT NULL, language TEXT NOT NULL,
        status TEXT NOT NULL, ticket_hash TEXT NOT NULL,
        ticket_expires_at TEXT NOT NULL, ticket_used_at TEXT,
        attempt_token_hash TEXT, issued_at TEXT NOT NULL, loaded_at TEXT,
        first_move_at TEXT, finished_at TEXT,
        duration_seconds REAL NOT NULL DEFAULT 0, move_count INTEGER NOT NULL DEFAULT 0,
        push_count INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0,
        minimum_moves INTEGER NOT NULL DEFAULT -1,
        minimum_pushes INTEGER NOT NULL DEFAULT -1,
        idempotency_key TEXT NOT NULL
    );
    CREATE TABLE designer_intentions (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE, content TEXT NOT NULL,
        language TEXT NOT NULL, idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE revision_challenges (
        challenge_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        base_version_id TEXT NOT NULL, source_proposal_turn_id TEXT NOT NULL,
        challenge_turn_id TEXT NOT NULL, status TEXT NOT NULL,
        current_reason_turn_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE challenge_reason_reviews (
        review_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        challenge_id TEXT NOT NULL, source_user_turn_id TEXT NOT NULL,
        message_key TEXT NOT NULL, status TEXT NOT NULL,
        attempts_used INTEGER NOT NULL DEFAULT 0, failure_code TEXT,
        result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    """


def test_schema_pragmas_and_no_cold_start_table_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "app.sqlite3"
    db = SQLiteDatabase(db_path)
    db.initialize()

    with db.read_conn() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert "challenge_review_requests" not in tables
        assert {
            "online_rooms", "online_room_events", "study_records", "design_sessions",
            "level_versions", "conversation_turns", "change_proposals",
            "revision_challenges", "play_attempts", "draft_regenerations",
            "audit_events", "idempotency_keys", "binary_discovery_topics",
            "component_recovery_tasks",
        }.issubset(tables)

        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
                "VALUES ('s1', 'test', '{}', '2026-11-26T00:00:00Z')"
            )

    # Re-opening an already stamped DB performs zero table scans.
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    reopened = SQLiteDatabase(db_path)
    reopened.initialize()
    assert not any("SELECT" in stmt.upper() and "FROM" in stmt.upper() for stmt in statements)
    assert any("PRAGMA user_version" in stmt for stmt in statements)
    with reopened.read_conn() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_rooms_repo_lifecycle_sync_and_reaper(tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path / "rooms.sqlite3")
    rooms = RoomsRepository(db)
    p1 = {"playerNumber": 1, "tokenHash": "h1", "studySessionId": "st1",
          "menuStartedFlow": True, "ready": False}
    p2 = {"playerNumber": 2, "tokenHash": "h2", "studySessionId": "st2",
          "menuStartedFlow": True, "ready": False}

    created = rooms.create_room(
        "m1", "ABC234", player1=p1,
        now_iso="2026-11-26T00:00:00Z", expires_at_iso="2026-11-26T00:30:00Z",
    )
    assert created.status == "waiting_for_opponent"
    joined = rooms.join_room(
        "ABC234", player2=p2,
        now_iso="2026-11-26T00:01:00Z", expires_at_iso="2026-11-26T00:31:00Z",
    )
    assert joined.status == "briefing"
    with pytest.raises(ConflictError):
        rooms.join_room(
            "ABC234", player2=p2,
            now_iso="2026-11-26T00:01:30Z", expires_at_iso="2026-11-26T00:31:30Z",
        )

    rooms.set_ready("m1", 1, ready=True, now_iso="2026-11-26T00:02:00Z",
                    expires_at_iso="2026-11-26T00:32:00Z")
    ready_room = rooms.set_ready("m1", 2, ready=True, now_iso="2026-11-26T00:03:00Z",
                                 expires_at_iso="2026-11-26T00:33:00Z")
    assert ready_room.status == "waiting_for_challenges"

    draft_res = rooms.record_draft(
        "m1", 1, requested_draft={"finalDifficulty": "Medium", "finalLayout": "Balanced"},
        now_iso="2026-11-26T00:03:10Z", expires_at_iso="2026-11-26T00:33:10Z",
    )
    assert draft_res["pending"] is True

    _, is_new = rooms.append_cocreation_event(
        "m1", 1, event_type="first_stage", event_id="ev-first",
        payload={"eventId": "ev-first", "eventType": "first_stage"},
        now_iso="2026-11-26T00:03:20Z", expires_at_iso="2026-11-26T00:33:20Z",
    )
    assert is_new is True
    _, is_dup = rooms.append_cocreation_event(
        "m1", 1, event_type="first_stage", event_id="ev-first",
        payload={"eventId": "ev-first", "eventType": "first_stage"},
        now_iso="2026-11-26T00:03:21Z", expires_at_iso="2026-11-26T00:33:21Z",
    )
    assert is_dup is False

    synced = rooms.sync_designer_intention(
        "m1", 1, designer_intention="Build a crisp box puzzle",
        event_id="ev-msg-1", session_id="sess1",
        now_iso="2026-11-26T00:03:30Z", expires_at_iso="2026-11-26T00:33:30Z",
    )
    assert synced.players[0]["designerIntention"] == "Build a crisp box puzzle"

    rooms.submit_challenge(
        "m1", 1, rows=["############"], ai_assistant_mode="description_generation",
        now_iso="2026-11-26T00:04:00Z", expires_at_iso="2026-11-26T00:34:00Z",
    )
    chall = rooms.submit_challenge(
        "m1", 2, rows=["############"], ai_assistant_mode="description_generation",
        now_iso="2026-11-26T00:04:10Z", expires_at_iso="2026-11-26T00:34:10Z",
    )
    assert chall.status == "challenges_ready"

    res_room = rooms.submit_result(
        "m1", 1,
        result={"outcome": "completed", "moveCount": 8, "minimumMoves": 6,
                "durationSeconds": 12.5, "restartCount": 0},
        now_iso="2026-11-26T00:05:00Z", expires_at_iso="2026-11-26T00:35:00Z",
    )
    assert res_room.status == "waiting_for_results"
    left_room = rooms.leave_room(
        "m1", 2, now_iso="2026-11-26T00:06:00Z", expires_at_iso="2026-11-26T00:36:00Z"
    )
    assert left_room.status == "cancelled"

    slot1_events = rooms.list_room_events("m1", player_slot=1)
    assert any(ev.event_type == "draft" for ev in slot1_events)
    assert rooms.reap_expired_rooms("2026-11-26T00:40:00Z") == 1
    assert rooms.get_room("m1") is None
    deleted = rooms.delete_match("m1")
    assert deleted["deletedEventCount"] > 0
    with pytest.raises(NotFoundError):
        rooms.delete_match("m1")


def test_study_repo_insert_query_and_jsonl_export(tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path / "study.sqlite3")
    study = StudyRepository(db)
    study.insert_record(
        "survey", {"eventType": "survey-response", "rating": 5},
        participant_id="p1", created_at="2026-11-26T00:00:00Z",
    )
    study.insert_record(
        "level_start", {"eventType": "level-start", "round": 1},
        match_id="m1", participant_id="p1", round_id="r1",
        created_at="2026-11-26T00:01:00Z",
    )
    assert len(study.query_records(record_type="survey")) == 1
    assert len(study.query_records(match_id="m1")) == 1
    lines = list(study.iter_jsonl_export("survey", chunk_size=1))
    assert len(lines) == 1 and json.loads(lines[0])["rating"] == 5
    assert study.delete_records(match_id="m1") == 1
    assert study.clear_records() == 1


def test_sessions_dialogue_operational_and_batch_read_model() -> None:
    db = SQLiteDatabase(":memory:")
    sessions = SessionsRepository(db)
    dialogue = DialogueRepository(db)
    ops = OperationalRepository(db)

    sessions.create_session(
        session_id="demo-expired", mode="algorithm_demo", language="en",
        access_token_hash="a1", integration_token_hash="i1", bootstrap_token_hash="b1",
        created_at="2026-11-26T00:00:00Z", is_demo=True, expires_at="2026-11-26T00:15:00Z",
    )
    sessions.create_session(
        session_id="demo-active", mode="algorithm_demo", language="zh-CN",
        access_token_hash="a2", integration_token_hash="i2", bootstrap_token_hash="b2",
        created_at="2026-11-26T00:10:00Z", is_demo=True, expires_at="2026-11-26T01:00:00Z",
    )
    sessions.create_session(
        session_id="formal-1", mode="partial_completion", language="en",
        access_token_hash="a3", integration_token_hash="i3", bootstrap_token_hash="b3",
        created_at="2026-11-26T00:00:00Z", is_demo=False,
    )
    assert sessions.delete_expired_demo_sessions("2026-11-26T00:20:00Z") == 1
    assert sessions.get_session("demo-expired") is None
    assert sessions.get_session("demo-active") is not None
    assert sessions.get_session("formal-1") is not None

    sessions.insert_version(
        version_id="v1", session_id="formal-1", version_number=1, source="initial",
        summary="init", rows=["####", "#p.#"], solver={"solvable": True},
        created_at="2026-11-26T00:01:00Z",
    )
    dialogue.insert_turn(
        turn_id="t1", session_id="formal-1", role="assistant", content="hello",
        version_id="v1", request_id="req-1", created_at="2026-11-26T00:01:05Z",
    )
    dialogue.upsert_turn_translation("formal-1", "t1", "zh-CN", {"body": "你好"})
    dialogue.insert_proposal(
        proposal_id="prop1", session_id="formal-1", assistant_turn_id="t1",
        base_version_id="v1", status="pending", proposal={"summary": "open corridor"},
        created_at="2026-11-26T00:01:10Z", idempotency_key="pk-1",
    )
    dialogue.decide_proposal(
        "formal-1", "prop1", status="accepted", decision={"decisionType": "accept"},
        candidate_version_id="v1", decided_at="2026-11-26T00:01:20Z",
    )
    dialogue.upsert_challenge(
        challenge_id="ch1", session_id="formal-1", base_version_id="v1",
        source_proposal_turn_id="t1", challenge_turn_id="t1", status="resolved",
        created_at="2026-11-26T00:01:30Z", reviews=[{"reviewId": "r1", "status": "done"}],
    )
    sessions.create_play_attempt(
        attempt_id="pa1", session_id="formal-1", version_id="v1",
        mode="partial_completion", language="en", ticket_hash="th",
        ticket_expires_at="2026-11-26T00:10:00Z", issued_at="2026-11-26T00:02:00Z",
        minimum_moves=4, minimum_pushes=1, idempotency_key="pak1",
    )
    sessions.create_draft_regeneration(
        request_id="dr1", session_id="formal-1", idempotency_key="drk1",
        status="pending", generation=2, requested_at="2026-11-26T00:02:10Z",
    )
    dialogue.append_event("formal-1", "session_created", {"ok": True}, "2026-11-26T00:00:00Z")

    c1 = ops.claim_key("formal-1", "message", "mk-1", request_hash="hash-a",
                       created_at="2026-11-26T00:02:00Z")
    assert c1.outcome == "acquired"
    c2 = ops.claim_key("formal-1", "message", "mk-1", request_hash="hash-b",
                       created_at="2026-11-26T00:02:01Z")
    assert c2.outcome == "conflict"
    c3 = ops.claim_key("formal-1", "message", "mk-1", request_hash="hash-a",
                       created_at="2026-11-26T00:02:02Z")
    assert c3.outcome == "in_progress"
    ops.complete_key("formal-1", "message", "mk-1", response={"turnId": "t1"})
    c4 = ops.claim_key("formal-1", "message", "mk-1", request_hash="hash-a",
                       created_at="2026-11-26T00:02:03Z")
    assert c4.outcome == "completed" and c4.record.response == {"turnId": "t1"}

    ops.upsert_topic("formal-1", "top1", {"status": "clarifying"}, "2026-11-26T00:02:10Z")
    ops.upsert_task("rec1", "formal-1", "human_edit", "dis1", {"status": "pending"},
                    "2026-11-26T00:02:11Z")
    assert ops.get_latest_topic("formal-1") is not None
    assert len(ops.list_tasks("formal-1", target_kind="human_edit")) == 1

    read_model = sessions.load_session_read_model("formal-1")
    assert read_model is not None
    assert len(read_model.versions) == 1
    assert read_model.turns[0].translations == {"zh-CN": {"body": "你好"}}
    assert read_model.proposals[0].status == "accepted"
    assert read_model.challenges[0].reviews[0]["reviewId"] == "r1"
    assert len(read_model.play_attempts) == 1
    assert len(read_model.draft_regenerations) == 1
    db.close()


def test_migrations_cli_idempotent_import_and_legacy_db(tmp_path: Path) -> None:
    study_dir = tmp_path / "study_logs"
    match_id = "a" * 32
    (study_dir / "matches" / match_id).mkdir(parents=True)
    (study_dir / "survey_responses.jsonl").write_text(
        json.dumps({"eventType": "survey-response", "playerId": "p1", "q1": 5,
                    "serverReceivedAt": "2026-11-26T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    (study_dir / "level_records.jsonl").write_text(
        json.dumps({"eventType": "level-start", "studySessionId": "s1",
                    "serverReceivedAt": "2026-11-26T00:01:00Z"}) + "\n",
        encoding="utf-8",
    )
    (study_dir / "matches" / match_id / "player-1.jsonl").write_text(
        json.dumps({"eventId": "ev1", "eventType": "first_stage", "matchId": match_id,
                    "playerNumber": 1, "serverReceivedAt": "2026-11-26T00:02:00Z"}) + "\n",
        encoding="utf-8",
    )

    legacy_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(legacy_path)
    try:
        conn.executescript(_legacy_schema_sql())
        conn.execute(
            "INSERT INTO design_sessions(id, creation_key, access_hash, integration_hash, "
            "bootstrap_hash, initial_draft_method, language, language_locked_at, status, "
            "current_version_id, final_version_id, created_at, updated_at, draft_generation, "
            "draft_regeneration_status, draft_regeneration_request_id, draft_regeneration_key) "
            "VALUES ('s1','ck1','ah','ih','bh','partial_completion','en','2026-11-26T00:00:00Z',"
            "'completed','v1','v1','2026-11-26T00:00:00Z','2026-11-26T00:05:00Z',2,"
            "'completed','dr1','drk1')"
        )
        conn.execute(
            "INSERT INTO level_versions(id, session_id, stage_number, source, rows_json, summary, "
            "diff_json, validation_json, idempotency_key, created_at) "
            "VALUES ('v1','s1',1,'initial','[\"####\"]','init','[]','{\"solvable\":true}',"
            "'vk1','2026-11-26T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO conversation_turns(id, session_id, sequence_number, role, content, "
            "language, version_id, request_id, created_at) "
            "VALUES ('t1','s1',1,'assistant','hi','en','v1','req1','2026-11-26T00:00:01Z')"
        )
        conn.execute(
            "INSERT INTO turn_translations(id, session_id, turn_id, language, body, created_at) "
            "VALUES ('tr1','s1','t1','zh-CN','你好','2026-11-26T00:00:02Z')"
        )
        conn.execute(
            "INSERT INTO llm_assessments(id, session_id, version_id, assistant_turn_id, "
            "payload_json, prompt_version, created_at) "
            "VALUES ('a1','s1','v1','t1','{\"ok\":true}','p1','2026-11-26T00:00:02Z')"
        )
        conn.execute(
            "INSERT INTO change_proposals(id, session_id, base_version_id, proposed_rows_json, "
            "summary, diff_json, validation_json, status, assistant_turn_id, idempotency_key, "
            "created_at) VALUES ('cp1','s1','v1','[\"####\"]','sum','[]','{}','accepted','t1',"
            "'pk1','2026-11-26T00:00:03Z')"
        )
        conn.execute(
            "INSERT INTO designer_decisions(id, session_id, version_id, proposal_id, "
            "decision_type, reason, idempotency_key, created_at) "
            "VALUES ('dd1','s1','v1','cp1','accept','','dk1','2026-11-26T00:00:04Z')"
        )
        conn.execute(
            "INSERT INTO designer_intentions(id, session_id, content, language, idempotency_key, "
            "created_at) VALUES ('di1','s1','goal','en','ik1','2026-11-26T00:05:00Z')"
        )
        conn.execute(
            "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
            "VALUES ('s1','binary_discovery_state',"
            "'{\"topicId\":\"top1\",\"status\":\"clarifying\"}','2026-11-26T00:01:00Z')"
        )
        conn.execute(
            "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
            "VALUES ('s1','component_recovery','{\"recoveryId\":\"rec1\",\"baseVersionId\":\"v1\","
            "\"status\":\"pending\"}','2026-11-26T00:02:00Z')"
        )
        conn.execute(
            "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
            "VALUES ('s1','binary_discovery_receipt','{\"key\":\"bdk1\",\"request\":{\"x\":1},"
            "\"status\":\"done\"}','2026-11-26T00:02:30Z')"
        )
        conn.commit()
    finally:
        conn.close()

    target_db = tmp_path / "unified.sqlite3"
    cmd = [
        sys.executable, "-m", "server.persistence.migrations",
        "--db", str(target_db),
        "--import-study-logs", str(study_dir),
        "--migrate-cocreation-db", str(legacy_path),
    ]
    first = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    assert first["study_import"]["study_inserted"] == 2
    assert first["study_import"]["room_events_inserted"] == 1
    assert first["cocreation_migration"]["sessions"] == 1
    assert first["cocreation_migration"]["binary_topics"] == 1
    assert first["cocreation_migration"]["recovery_tasks"] == 1
    assert first["cocreation_migration"]["idempotency_keys"] == 1

    second = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    assert second["study_import"]["study_inserted"] == 0
    assert second["study_import"]["room_events_inserted"] == 0
    assert second["cocreation_migration"]["sessions"] == 0
    assert second["cocreation_migration"]["audit_events"] == 0

    db = SQLiteDatabase(target_db)
    rm = SessionsRepository(db).load_session_read_model("s1")
    assert rm is not None and rm.session.final_intention is not None
    assert rm.session.final_intention["content"] == "goal"
    assert rm.versions[0].assessment is not None
    assert rm.turns[0].translations is not None and "zh-CN" in rm.turns[0].translations
    assert OperationalRepository(db).get_key("s1", "binary_discovery", "bdk1") is not None
