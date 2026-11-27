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
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert {
            "idx_conversation_turns_version_id",
            "idx_revision_challenges_base_version",
            "idx_play_attempts_version_id",
        }.issubset(indexes)

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
    sessions.insert_version(
        version_id="demo-v1", session_id="demo-expired", version_number=1, source="initial",
        summary="init", rows=["####", "#p.#"], solver={"solvable": True},
        created_at="2026-11-26T00:00:01Z",
    )
    dialogue.insert_turn(
        turn_id="demo-t1", session_id="demo-expired", role="assistant", content="hi",
        version_id="demo-v1", request_id="demo-req", created_at="2026-11-26T00:00:02Z",
    )
    dialogue.insert_proposal(
        proposal_id="demo-p1", session_id="demo-expired", assistant_turn_id="demo-t1",
        base_version_id="demo-v1", status="pending", proposal={"summary": "edit"},
        created_at="2026-11-26T00:00:03Z", idempotency_key="demo-pk",
    )
    dialogue.upsert_challenge(
        challenge_id="demo-ch1", session_id="demo-expired", base_version_id="demo-v1",
        source_proposal_turn_id="demo-t1", challenge_turn_id="demo-t1",
        status="awaiting_reason", created_at="2026-11-26T00:00:04Z",
    )
    sessions.create_play_attempt(
        attempt_id="demo-pa1", session_id="demo-expired", version_id="demo-v1",
        mode="algorithm_demo", language="en", ticket_hash="dth",
        ticket_expires_at="2026-11-26T00:10:00Z", issued_at="2026-11-26T00:00:05Z",
        minimum_moves=4, minimum_pushes=1, idempotency_key="demo-pak",
    )
    sessions.create_draft_regeneration(
        request_id="demo-dr1", session_id="demo-expired", idempotency_key="demo-drk",
        status="completed", generation=1, requested_at="2026-11-26T00:00:06Z",
    )
    ops.upsert_topic("demo-expired", "demo-top", {"status": "clarifying"}, "2026-11-26T00:00:07Z")
    ops.upsert_task("demo-rec", "demo-expired", "human_edit", "d1", {"status": "pending"},
                    "2026-11-26T00:00:08Z")
    dialogue.append_event("demo-expired", "session_created", {}, "2026-11-26T00:00:00Z")
    ops.claim_key("demo-expired", "message", "dk", request_hash="dh",
                  created_at="2026-11-26T00:00:09Z")

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
    assert sessions.load_session_read_model("demo-expired") is None
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
    assert first["cocreation_migration"]["idempotency_keys"] == 2

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
    assert OperationalRepository(db).get_key("s1", "binary_choice", "bdk1") is not None
    assert OperationalRepository(db).get_key("s1", "decide_proposal", "dk1") is not None
    with db.read_conn() as conn:
        study_row = conn.execute(
            "SELECT match_id, participant_id FROM study_records WHERE record_type = 'level_start'"
        ).fetchone()
        assert study_row is not None
        assert study_row["match_id"] is None
        assert study_row["participant_id"] == "s1"


def test_migration_filters_orphans_normalizes_challenges_and_backfills_language_lock(
    tmp_path: Path,
) -> None:
    from server.persistence._cocreation_migrate import migrate_cocreation_db

    legacy_path = tmp_path / "legacy_orphans.sqlite3"
    conn = sqlite3.connect(legacy_path)
    try:
        conn.executescript(_legacy_schema_sql())
        # Session with current_version_id set but language_locked_at NULL
        conn.execute(
            "INSERT INTO design_sessions(id, creation_key, access_hash, integration_hash, "
            "bootstrap_hash, initial_draft_method, language, language_locked_at, status, "
            "current_version_id, deadline_started_at, created_at, updated_at) "
            "VALUES ('s1','ck1','ah','ih','bh','partial_completion','zh-CN',NULL,"
            "'active','v1',NULL,'2026-11-26T00:00:00Z','2026-11-26T00:01:00Z')"
        )
        # Session with deadline_started_at set but language_locked_at NULL
        conn.execute(
            "INSERT INTO design_sessions(id, creation_key, access_hash, integration_hash, "
            "bootstrap_hash, initial_draft_method, language, language_locked_at, status, "
            "current_version_id, deadline_started_at, created_at, updated_at) "
            "VALUES ('s2','ck2','ah2','ih2','bh2','partial_completion','en',NULL,"
            "'active',NULL,'2026-11-26T00:00:30Z','2026-11-26T00:00:10Z','2026-11-26T00:00:30Z')"
        )
        conn.execute(
            "INSERT INTO level_versions(id, session_id, stage_number, source, rows_json, summary, "
            "diff_json, validation_json, idempotency_key, created_at) "
            "VALUES ('v1','s1',1,'initial','[\"####\"]','init','[]','{\"solvable\":true}',"
            "'vk1','2026-11-26T00:00:00Z')"
        )
        # Orphan level_version for missing session
        conn.execute(
            "INSERT INTO level_versions(id, session_id, stage_number, source, rows_json, summary, "
            "diff_json, validation_json, idempotency_key, created_at) "
            "VALUES ('v-orphan','s-missing',1,'initial','[\"####\"]','init','[]','{}',"
            "'vk-o','2026-11-26T00:00:00Z')"
        )
        # Valid & orphan turns/proposals/play_attempts
        conn.execute(
            "INSERT INTO conversation_turns(id, session_id, sequence_number, role, content, "
            "language, version_id, created_at) "
            "VALUES ('t1','s1',1,'assistant','hi','zh-CN','v1','2026-11-26T00:00:01Z'),"
            "('t-orphan','s1',2,'user','orphan','zh-CN','v-missing','2026-11-26T00:00:02Z')"
        )
        conn.execute(
            "INSERT INTO change_proposals(id, session_id, base_version_id, proposed_rows_json, "
            "summary, diff_json, validation_json, status, assistant_turn_id, idempotency_key, "
            "created_at) VALUES ('cp1','s1','v1','[\"####\"]','sum','[]','{}','pending','t1',"
            "'pk1','2026-11-26T00:00:03Z'),"
            "('cp-orphan','s1','v-missing','[\"####\"]','sum','[]','{}','pending','t1',"
            "'pk-o','2026-11-26T00:00:04Z')"
        )
        conn.execute(
            "INSERT INTO play_attempts(id, session_id, version_id, initial_draft_method, "
            "language, status, ticket_hash, ticket_expires_at, issued_at, idempotency_key) "
            "VALUES ('pa-orphan','s1','v-missing','partial_completion','zh-CN','issued',"
            "'th','2026-11-26T00:10:00Z','2026-11-26T00:00:05Z','pak-o')"
        )
        # Legacy revision_challenges statuses + orphan
        conn.execute(
            "INSERT INTO revision_challenges(challenge_id, session_id, base_version_id, "
            "source_proposal_turn_id, challenge_turn_id, status, created_at, updated_at) "
            "VALUES ('c1','s1','v1','t1','t1','review_pending',"
            "'2026-11-26T00:00:01Z','2026-11-26T00:00:01Z'),"
            "('c2','s1','v1','t1','t2','reason_review',"
            "'2026-11-26T00:00:02Z','2026-11-26T00:00:02Z'),"
            "('c3','s1','v1','t1','t3','choice_pending',"
            "'2026-11-26T00:00:03Z','2026-11-26T00:00:03Z'),"
            "('c-orphan','s1','v-missing','t1','t4','review_pending',"
            "'2026-11-26T00:00:04Z','2026-11-26T00:00:04Z')"
        )
        # Orphan operational audit events for missing session
        conn.execute(
            "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
            "VALUES ('s-missing','binary_discovery_state',"
            "'{\"topicId\":\"top-o\",\"status\":\"clarifying\"}','2026-11-26T00:01:00Z'),"
            "('s-missing','component_recovery',"
            "'{\"recoveryId\":\"rec-o\",\"baseVersionId\":\"v1\"}','2026-11-26T00:01:01Z')"
        )
        conn.commit()
    finally:
        conn.close()

    target_db = SQLiteDatabase(tmp_path / "migrated.sqlite3")
    stats = migrate_cocreation_db(target_db, legacy_path)
    assert stats["sessions"] == 2
    assert stats["versions"] == 1
    assert stats["turns"] == 1
    assert stats["proposals"] == 1
    assert stats["challenges"] == 3
    assert stats["play_attempts"] == 0
    assert stats["binary_topics"] == 0
    assert stats["recovery_tasks"] == 0

    sessions_repo = SessionsRepository(target_db)
    s1 = sessions_repo.get_session("s1")
    s2 = sessions_repo.get_session("s2")
    assert s1 is not None and s1.language_locked is True
    assert s1.language_locked_at == "2026-11-26T00:00:00Z"
    assert s2 is not None and s2.language_locked is True
    assert s2.language_locked_at == "2026-11-26T00:00:10Z"
    rm = sessions_repo.load_session_read_model("s1")
    assert rm is not None
    statuses = {c.challenge_id: c.status for c in rm.challenges}
    assert statuses == {
        "c1": "awaiting_reason",
        "c2": "awaiting_reason",
        "c3": "awaiting_choice",
    }


def test_reap_expired_rooms_protects_active_cocreation_sessions(tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path / "reap_guard.sqlite3")
    rooms = RoomsRepository(db)
    sessions = SessionsRepository(db)
    p1 = {"playerNumber": 1, "tokenHash": "h1", "studySessionId": "st1",
          "menuStartedFlow": True, "ready": False}
    rooms.create_room(
        "m-active", "ABC234", player1=p1,
        now_iso="2026-11-26T00:00:00Z", expires_at_iso="2026-11-26T00:10:00Z",
    )
    sessions.create_session(
        session_id="sess-active", match_id="m-active", player_slot=1,
        mode="partial_completion", language="en",
        access_token_hash="a", integration_token_hash="i", bootstrap_token_hash="b",
        created_at="2026-11-26T00:00:00Z",
    )
    # Expired timestamp, but protected while design_session is active or awaiting_intention
    assert rooms.reap_expired_rooms("2026-11-26T00:20:00Z") == 0
    assert rooms.get_room("m-active") is not None

    sessions.update_session("sess-active", status="awaiting_intention")
    assert rooms.reap_expired_rooms("2026-11-26T00:20:00Z") == 0
    assert rooms.get_room("m-active") is not None

    # Once completed, expired room can be reaped
    sessions.update_session("sess-active", status="completed")
    assert rooms.reap_expired_rooms("2026-11-26T00:20:00Z") == 1
    assert rooms.get_room("m-active") is None


async def test_migration_runtime_compatibility(
    tmp_path: Path,
) -> None:
    """#9: Migrated 8010 DB rows match runtime schemas, digests, proposals, and read models."""
    from server.api.deps import ensure_session_shape
    from server.application.analytics._cocreation_projection import (
        build_authoritative_player_projection,
        load_formal_session_bundles,
        serialize_session_research_bundle,
    )
    from server.application.sessions import (
        CoCreationAppSettings,
        SessionReadModelService,
        request_digest,
    )
    from server.application.sessions.lifecycle.finalize import finalize_session_impl
    from server.application.sessions.stage.stage_ops import restore_version_impl
    from server.core.clock import FakeClock
    from server.domain.errors import IdempotencyConflictError
    from server.persistence._cocreation_migrate import migrate_cocreation_db

    valid_rows = [
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
    match_id = "b" * 32
    legacy_path = tmp_path / "legacy_full_compat.sqlite3"
    conn = sqlite3.connect(legacy_path)
    try:
        conn.executescript(
            _legacy_schema_sql()
            + """
            CREATE TABLE challenge_review_requests (
                session_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                review_id TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(session_id, idempotency_key)
            );
            """
        )
        conn.execute(
            "INSERT INTO design_sessions(id, creation_key, access_hash, integration_hash, "
            "bootstrap_hash, match_id, player_number, initial_draft_method, language, "
            "language_locked_at, status, current_version_id, final_version_id, "
            "created_at, updated_at) "
            "VALUES ('s-mig','ck-mig','ah','ih','bh',?,1,'partial_completion','en',"
            "'2026-11-26T00:00:00Z','active','v2',NULL,'2026-11-26T00:00:00Z','2026-11-26T00:05:00Z')",
            (match_id,),
        )
        conn.execute(
            "INSERT INTO level_versions(id, session_id, stage_number, parent_version_id, source, "
            "rows_json, summary, diff_json, validation_json, idempotency_key, created_at) "
            "VALUES ('v1','s-mig',1,NULL,'initial',?,'Stage 1','[]',"
            "'{\"solvable\":true,\"minSteps\":3,\"minPushes\":1}','vk1','2026-11-26T00:00:00Z'),"
            "('v2','s-mig',2,'v1','restored',?,'Continued from Stage 1','[]',"
            "'{\"solvable\":true,\"minSteps\":3,\"minPushes\":1}','restore:restore-key-1','2026-11-26T00:02:00Z')",
            (json.dumps(valid_rows), json.dumps(valid_rows)),
        )
        conn.execute(
            "INSERT INTO llm_assessments(id, session_id, version_id, assistant_turn_id, "
            "payload_json, prompt_version, created_at) "
            "VALUES ('a-mig','s-mig','v1','t-open',?,'prompt-v3','2026-11-26T00:00:05Z')",
            (
                json.dumps(
                    {
                        "solutionSummary": "Direct push to goal in 3 steps.",
                        "difficultyOpinion": "Very approachable opening.",
                        "features": ["Direct corridor"],
                        "suggestions": ["Add a side obstacle"],
                        "satisfactionQuestion": None,
                    }
                ),
            ),
        )
        cmp_guidance = json.dumps(
            {
                "move": "offer_perspective",
                "challengeCard": {
                    "challengeId": "chg-1",
                    "status": "choice_pending",
                    "relation": "different",
                    "merit": "reasonable",
                    "comparison": "Both priorities trade off.",
                },
            }
        )
        conn.execute(
            "INSERT INTO conversation_turns(id, session_id, sequence_number, role, content, "
            "language, version_id, request_id, guidance_json, created_at) VALUES "
            "('t-open','s-mig',1,'assistant','Stage 1 is solvable.','en','v1','open-1',"
            "'{\"move\":\"observe_stage\"}','2026-11-26T00:00:05Z'),"
            "('t-offer','s-mig',2,'assistant','Try adding a choke wall.','en','v1','req-offer',"
            "'{\"move\":\"offer_revision\",\"proposalOffer\":{\"summary\":\"Add a choke wall\"}}',"
            "'2026-11-26T00:00:20Z'),"
            "('t-chg','s-mig',3,'assistant','Why challenge this plan?','en','v1','req-chg',"
            "'{\"move\":\"offer_perspective\","
            "\"challengeCard\":{\"challengeId\":\"chg-1\",\"status\":\"reason_pending\"}}',"
            "'2026-11-26T00:00:30Z'),"
            "('t-reason','s-mig',4,'user','Keep the right corridor open','en','v1','req-reason',"
            "NULL,'2026-11-26T00:00:40Z'),"
            "('t-cmp','s-mig',5,'assistant','Both priorities trade off.','en','v1','req-reason',"
            "?,'2026-11-26T00:00:45Z')",
            (cmp_guidance,),
        )
        conn.execute(
            "INSERT INTO change_proposals(id, session_id, base_version_id, proposed_rows_json, "
            "summary, diff_json, validation_json, status, assistant_turn_id, idempotency_key, "
            "created_at, decided_at) VALUES "
            "('cp1','s-mig','v1',?,'Add a choke wall','[]','{\"solvable\":true}','accepted',"
            "'t-offer','pk-cp1','2026-11-26T00:00:20Z','2026-11-26T00:01:00Z')",
            (json.dumps(valid_rows),),
        )
        conn.executemany(
            "INSERT INTO designer_decisions(id, session_id, version_id, proposal_id, "
            "decision_type, reason, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "dd-prop", "s-mig", "v2", "cp1", "accept", "Good route",
                    "dp-key-1", "2026-11-26T00:01:00Z",
                ),
                (
                    "dd-rest", "s-mig", "v2", None, "restore", "Revert edit",
                    "restore-key-1", "2026-11-26T00:02:00Z",
                ),
                (
                    "dd-fin", "s-mig", "v2", None, "finalize", "",
                    "fin-key-1", "2026-11-26T00:04:00Z",
                ),
            ],
        )
        conn.execute(
            "INSERT INTO revision_challenges(challenge_id, session_id, base_version_id, "
            "source_proposal_turn_id, challenge_turn_id, status, current_reason_turn_id, "
            "created_at, updated_at) VALUES "
            "('chg-1','s-mig','v1','t-offer','t-chg','choice_pending','t-reason',"
            "'2026-11-26T00:00:30Z','2026-11-26T00:00:45Z')"
        )
        review_result = {
            "relation": "different",
            "merit": "reasonable",
            "comparison": "Both priorities trade off.",
        }
        conn.execute(
            "INSERT INTO challenge_reason_reviews(review_id, session_id, challenge_id, "
            "source_user_turn_id, message_key, status, attempts_used, failure_code, "
            "result_json, created_at, updated_at) VALUES "
            "('rev-1','s-mig','chg-1','t-reason','req-reason','resolved',1,NULL,?,"
            "'2026-11-26T00:00:40Z','2026-11-26T00:00:45Z')",
            (json.dumps(review_result),),
        )
        conn.execute(
            "INSERT INTO challenge_review_requests(session_id, idempotency_key, review_id, "
            "status, result_json, created_at, updated_at) VALUES "
            "('s-mig','crr-key-1','rev-1','completed',?,"
            "'2026-11-26T00:00:40Z','2026-11-26T00:00:45Z')",
            (json.dumps(review_result),),
        )
        # Audit events: pending->done binary receipt, restore, finalize, feedback, failed msg
        audits = [
            (
                "s-mig",
                "stage_restored",
                json.dumps(
                    {
                        "versionId": "v2",
                        "restoredFromVersionId": "v1",
                        "restoredFromStageNumber": 1,
                        "replacedVersionId": "v1-prev",
                        "replacedStageNumber": 2,
                        "baseVersionId": "v1-prev",
                    }
                ),
                "2026-11-26T00:02:00Z",
            ),
            (
                "s-mig",
                "finalization_request_committed",
                json.dumps(
                    {
                        "idempotencyKey": "fin-key-1",
                        "request": {"baseVersionId": "v2", "rowsHash": None},
                        "finalVersionId": "v2",
                        "deadlineStageVersionId": None,
                    }
                ),
                "2026-11-26T00:04:00Z",
            ),
            (
                "s-mig",
                "binary_discovery_receipt",
                json.dumps(
                    {
                        "key": "bc-key-1",
                        "request": {
                            "kind": "choice",
                            "topicId": "top-1",
                            "questionId": "q1",
                            "choiceId": "cA",
                            "baseVersionId": "v1",
                            "idempotencyKey": "bc-key-1",
                            "displayLanguage": "en",
                        },
                        "status": "pending",
                    }
                ),
                "2026-11-26T00:01:10Z",
            ),
            (
                "s-mig",
                "binary_discovery_receipt",
                json.dumps(
                    {
                        "key": "bc-key-1",
                        "request": {
                            "kind": "choice",
                            "topicId": "top-1",
                            "questionId": "q1",
                            "choiceId": "cA",
                            "baseVersionId": "v1",
                            "idempotencyKey": "bc-key-1",
                            "displayLanguage": "en",
                        },
                        "status": "done",
                    }
                ),
                "2026-11-26T00:01:15Z",
            ),
            (
                "s-mig",
                "intent_hypothesis_feedback_applied",
                json.dumps(
                    {
                        "idempotencyKey": "if-key-1",
                        "hypothesisId": "h1",
                        "versionId": "v1",
                        "outcome": "applied",
                        "request": {
                            "hypothesisId": "h1",
                            "action": "confirm",
                            "candidateText": None,
                            "selectedHypothesisId": None,
                            "sourceTurnId": "t-open",
                            "baseVersionId": "v1",
                        },
                    }
                ),
                "2026-11-26T00:01:20Z",
            ),
            (
                "s-mig",
                "question_feedback_applied",
                json.dumps(
                    {
                        "idempotencyKey": "qf-key-1",
                        "questionId": "oq1",
                        "versionId": "v1",
                        "action": "ignore",
                        "outcome": "applied",
                        "request": {
                            "questionId": "oq1",
                            "action": "ignore",
                            "baseVersionId": "v1",
                        },
                    }
                ),
                "2026-11-26T00:01:25Z",
            ),
            (
                "s-mig",
                "message_generation_failed",
                json.dumps(
                    {
                        "messageKey": "mf-key-1",
                        "baseVersionId": "v1",
                        "code": "UPSTREAM_TIMEOUT",
                        "requestSignature": {
                            "action": "none",
                            "baseVersionId": "v1",
                            "content": "Help me tighten the corridor",
                            "requestProposal": False,
                        },
                    }
                ),
                "2026-11-26T00:01:30Z",
            ),
        ]
        conn.executemany(
            "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            audits,
        )
        conn.commit()
    finally:
        conn.close()

    target_db = SQLiteDatabase(tmp_path / "unified_compat.sqlite3")
    stats1 = migrate_cocreation_db(target_db, legacy_path)
    assert stats1["sessions"] == 1
    assert stats1["challenges"] == 1
    assert stats1["challenge_review_requests"] == 1

    # Idempotency check: second migration run inserts zero new rows.
    stats2 = migrate_cocreation_db(target_db, legacy_path)
    assert stats2["sessions"] == 0
    assert stats2["audit_events"] == 0
    assert stats2["challenge_review_requests"] == 0
    assert stats2["idempotency_keys"] == 0

    sessions_repo = SessionsRepository(target_db)
    dialogue_repo = DialogueRepository(target_db)
    op_repo = OperationalRepository(target_db)

    rm = sessions_repo.load_session_read_model("s-mig")
    assert rm is not None

    # 1. Flat level_versions.assessment_json with optional metadata preserved
    v1_rec = next(v for v in rm.versions if v.version_id == "v1")
    assert v1_rec.assessment is not None
    assert "payload" not in v1_rec.assessment
    assert v1_rec.assessment["solutionSummary"] == "Direct push to goal in 3 steps."
    assert v1_rec.assessment["difficultyOpinion"] == "Very approachable opening."
    assert v1_rec.assessment["features"] == ["Direct corridor"]
    assert v1_rec.assessment["assistantTurnId"] == "t-open"
    assert v1_rec.assessment["promptVersion"] == "prompt-v3"

    # 2. Runtime-compatible revision_challenges.reviews_json + migrated challenge_review_requests
    assert len(rm.challenges) == 1
    chg = rm.challenges[0]
    assert chg.status == "awaiting_choice"
    assert len(chg.reviews) == 1
    rev = chg.reviews[0]
    assert rev["reasonTurnId"] == "t-reason"
    assert rev["reason"] == "Keep the right corridor open"
    assert rev["relation"] == "different"
    assert rev["merit"] == "reasonable"
    assert rev["comparison"] == "Both priorities trade off."
    assert rev["createdAt"] == "2026-11-26T00:00:40Z"
    assert rev["reviewId"] == "rev-1"
    assert rev["reviewRequests"][0]["idempotencyKey"] == "crr-key-1"

    migrated_crr_audits = [
        ev
        for ev in dialogue_repo.list_events("s-mig")
        if ev.event_type == "challenge_review_request_migrated"
    ]
    assert len(migrated_crr_audits) == 1
    assert migrated_crr_audits[0].payload["idempotencyKey"] == "crr-key-1"
    assert migrated_crr_audits[0].payload["reviewId"] == "rev-1"
    assert migrated_crr_audits[0].payload["challengeId"] == "chg-1"

    # 3. change_proposals.decision uses canonical "decision" key (and dialogue_repo.supersede too)
    assert len(rm.proposals) == 1
    assert rm.proposals[0].decision is not None
    assert rm.proposals[0].decision["decision"] == "accept"
    assert "decisionType" not in rm.proposals[0].decision

    dialogue_repo.insert_proposal(
        proposal_id="cp-pend",
        session_id="s-mig",
        assistant_turn_id="t-offer",
        base_version_id="v1",
        status="pending",
        proposal={"summary": "Pending edit"},
        created_at="2026-11-26T00:05:00Z",
    )
    superseded_ids = dialogue_repo.supersede_pending_proposals(
        "s-mig", decided_at="2026-11-26T00:05:05Z", reason="stage_saved"
    )
    assert superseded_ids == ["cp-pend"]
    sup_prop = dialogue_repo.get_proposal("s-mig", "cp-pend")
    assert sup_prop is not None and sup_prop.decision is not None
    assert sup_prop.decision["decision"] == "superseded"
    assert "decisionType" not in sup_prop.decision

    # 4. Aligned idempotency_keys scopes AND exact runtime request_digest parity
    bc_key = op_repo.get_key("s-mig", "binary_choice", "bc-key-1")
    assert bc_key is not None
    assert bc_key.status == "completed"
    assert bc_key.request_hash == request_digest(
        {
            "topicId": "top-1",
            "questionId": "q1",
            "choiceId": "cA",
            "baseVersionId": "v1",
            "displayLanguage": "en",
        }
    )
    assert op_repo.get_key("s-mig", "binary_discovery", "bc-key-1") is None

    rest_key = op_repo.get_key("s-mig", "restore_version", "restore-key-1")
    assert rest_key is not None
    assert rest_key.status == "completed"
    assert rest_key.request_hash == request_digest(
        {"versionId": "v1", "baseVersionId": "v1-prev"}
    )
    assert rest_key.response is not None and rest_key.response.get("reason") == "Revert edit"
    assert op_repo.get_key("s-mig", "restore", "restore-key-1") is None

    dp_key = op_repo.get_key("s-mig", "decide_proposal", "dp-key-1")
    assert dp_key is not None
    assert dp_key.status == "completed"
    assert dp_key.request_hash == request_digest(
        {
            "proposalId": "cp1",
            "decision": "accept",
            "base": "v1",
            "reason": "Good route",
        }
    )

    fin_key = op_repo.get_key("s-mig", "finalize", "fin-key-1")
    assert fin_key is not None
    assert fin_key.status == "completed"
    assert fin_key.request_hash == request_digest({"baseVersionId": "v2", "rows": None})

    if_key = op_repo.get_key("s-mig", "intent_feedback", "if-key-1")
    assert if_key is not None
    assert if_key.status == "completed"
    assert if_key.request_hash == request_digest(
        {
            "hypothesisId": "h1",
            "action": "confirm",
            "baseVersionId": "v1",
            "candidateText": "",
            "sourceTurnId": "t-open",
        }
    )

    qf_key = op_repo.get_key("s-mig", "question_feedback", "qf-key-1")
    assert qf_key is not None
    assert qf_key.status == "completed"
    assert qf_key.request_hash == request_digest(
        {"questionId": "oq1", "action": "ignore", "baseVersionId": "v1"}
    )

    mf_key = op_repo.get_key("s-mig", "message", "mf-key-1")
    assert mf_key is not None
    assert mf_key.status == "failed"
    assert mf_key.request_hash == request_digest(
        {
            "action": "send",
            "baseVersionId": "v1",
            "content": "Help me tighten the corridor",
            "requestProposal": False,
            "assistantTurnId": None,
            "proposalId": None,
            "challengeId": None,
            "challengeChoice": None,
            "exitChallenge": False,
        }
    )

    # 5. End-to-end runtime readers & idempotent replay through runtime services
    mig_settings = CoCreationAppSettings(token_secret="unit-test-secret")
    mig_clock = FakeClock()
    await restore_version_impl(
        db=target_db,
        sessions_repo=sessions_repo,
        operational_repo=op_repo,
        room_sync=None,
        clock=mig_clock,
        session=rm.session,
        version_id="v1",
        base_version_id="v1-prev",
        idempotency_key="restore-key-1",
    )
    with pytest.raises(IdempotencyConflictError):
        await restore_version_impl(
            db=target_db,
            sessions_repo=sessions_repo,
            operational_repo=op_repo,
            room_sync=None,
            clock=mig_clock,
            session=rm.session,
            version_id="v2",
            base_version_id="v1-prev",
            idempotency_key="restore-key-1",
        )
    await finalize_session_impl(
        db=target_db,
        operational_repo=op_repo,
        room_sync=None,
        clock=mig_clock,
        settings=mig_settings,
        session=rm.session,
        base_version_id="v2",
        idempotency_key="fin-key-1",
        rows=None,
    )

    read_model_svc = SessionReadModelService(
        sessions_repo,
        dialogue_repo,
        op_repo,
        clock=mig_clock,
        settings=mig_settings,
    )
    session_payload = ensure_session_shape(await read_model_svc.get_session_payload("s-mig"))
    assert session_payload["sessionId"] == "s-mig"
    assert len(session_payload["versions"]) == 2
    assert len(session_payload["turns"]) == 5

    bundles = load_formal_session_bundles(target_db, match_ids={match_id})
    assert (match_id, 1) in bundles
    flow, _, _ = build_authoritative_player_projection(
        bundles[(match_id, 1)],
        match_id=match_id,
        room_code="MIG123",
        player_number=1,
        study_session_id="study-mig",
    )
    assert any(ev.get("nodeType") == "player_challenge" for ev in flow)
    exported = serialize_session_research_bundle(bundles[(match_id, 1)])
    rev_reason = exported["revisionChallenges"][0]["reviews"][0]["reason"]
    assert rev_reason == "Keep the right corridor open"
    sol_summary = exported["levelVersions"][0]["assessment"]["solutionSummary"]
    assert sol_summary == "Direct push to goal in 3 steps."
    assert exported["changeProposals"][0]["decision"]["decision"] == "accept"


async def test_delete_expired_demo_sessions_respects_ttl_for_null_expires_at(
    tmp_path: Path,
) -> None:
    """#10: NULL expires_at demo sessions (e.g. migrated) survive within TTL and purge after TTL."""
    from types import SimpleNamespace

    from server.application.sessions import (
        CoCreationAppSettings,
        SessionLifecycleService,
        SessionReadModelService,
    )
    from server.core.clock import FakeClock
    from server.persistence._cocreation_migrate import migrate_cocreation_db

    valid_rows = [
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
    clock = FakeClock()  # 2026-01-01T00:00:00Z
    t0_iso = clock.utc_now()

    legacy_path = tmp_path / "legacy_demo.sqlite3"
    conn = sqlite3.connect(legacy_path)
    try:
        conn.executescript(_legacy_schema_sql())
        conn.execute(
            "INSERT INTO design_sessions(id, creation_key, access_hash, integration_hash, "
            "bootstrap_hash, demo_mode, initial_draft_method, language, status, "
            "created_at, updated_at) VALUES "
            "('demo-mig-recent','ck-dr','a1','i1','b1',1,'algorithm_demo','en','active',?,?),"
            "('formal-old','ck-fo','a2','i2','b2',0,'partial_completion','en','active',"
            "'2025-01-01T00:00:00Z','2025-01-01T00:00:00Z')",
            (t0_iso, t0_iso),
        )
        conn.commit()
    finally:
        conn.close()

    db = SQLiteDatabase(tmp_path / "demo_ttl.sqlite3")
    migrate_cocreation_db(db, legacy_path)

    sessions_repo = SessionsRepository(db)
    dialogue_repo = DialogueRepository(db)
    op_repo = OperationalRepository(db)

    # Confirm migrated demo session indeed has expires_at=NULL
    migrated_demo = sessions_repo.get_session("demo-mig-recent")
    assert migrated_demo is not None and migrated_demo.is_demo is True
    assert migrated_demo.expires_at is None

    # Seed one already-expired explicit demo and one still-active explicit demo
    sessions_repo.create_session(
        session_id="demo-exp-explicit",
        mode="algorithm_demo",
        language="en",
        access_token_hash="a3",
        integration_token_hash="i3",
        bootstrap_token_hash="b3",
        created_at=t0_iso,
        is_demo=True,
        expires_at="2026-01-01T00:05:00Z",
    )
    sessions_repo.create_session(
        session_id="demo-active-explicit",
        mode="algorithm_demo",
        language="en",
        access_token_hash="a4",
        integration_token_hash="i4",
        bootstrap_token_hash="b4",
        created_at=t0_iso,
        is_demo=True,
        expires_at="2026-01-01T01:00:00Z",
    )

    settings = CoCreationAppSettings(token_secret="unit-test-secret", demo_ttl_seconds=1800)
    read_model = SessionReadModelService(
        sessions_repo, dialogue_repo, op_repo, clock=clock, settings=settings
    )
    lifecycle = SessionLifecycleService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        clock=clock,
        settings=settings,
        demo_level_generator=lambda: SimpleNamespace(rows=tuple(valid_rows), seed=1),
    )

    # Advance 10 minutes (within 30-min demo TTL): explicit expired demo is purged,
    # migrated NULL-expiry demo & active explicit demo & old formal session ALL survive!
    clock.advance(600.0)
    await lifecycle.create_demo_session(language="en", idempotency_key="new-demo-1")

    assert sessions_repo.get_session("demo-exp-explicit") is None
    assert sessions_repo.get_session("demo-mig-recent") is not None
    assert sessions_repo.get_session("demo-active-explicit") is not None
    assert sessions_repo.get_session("formal-old") is not None

    # Advance another 25 minutes (total 35 min > 30 min TTL): migrated NULL-expiry demo IS purged,
    # while formal session (is_demo=0) is NEVER touched!
    clock.advance(1500.0)
    await lifecycle.create_demo_session(language="en", idempotency_key="new-demo-2")

    assert sessions_repo.get_session("demo-mig-recent") is None
    assert sessions_repo.get_session("formal-old") is not None

