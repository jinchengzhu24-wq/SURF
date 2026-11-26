"""Unified SQLite DDL statements for online/study and co-creation persistence."""

from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS online_rooms (
        id TEXT PRIMARY KEY,
        room_code TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        config_json TEXT NOT NULL DEFAULT '{}',
        players_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_online_rooms_room_code ON online_rooms(room_code)",
    (
        "CREATE INDEX IF NOT EXISTS idx_online_rooms_status_expires "
        "ON online_rooms(status, expires_at)"
    ),
    """
    CREATE TABLE IF NOT EXISTS online_room_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT NOT NULL,
        player_slot INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_online_room_events_match_slot_seq
        ON online_room_events(match_id, player_slot, seq)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_online_room_events_event_id
        ON online_room_events(match_id, player_slot, json_extract(payload_json, '$.eventId'))
    """,
    """
    CREATE TABLE IF NOT EXISTS study_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_type TEXT NOT NULL,
        match_id TEXT,
        participant_id TEXT,
        round_id TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_study_records_type_created
        ON study_records(record_type, created_at)
    """,
    "CREATE INDEX IF NOT EXISTS idx_study_records_match ON study_records(match_id)",
    """
    CREATE TABLE IF NOT EXISTS design_sessions (
        session_id TEXT PRIMARY KEY,
        match_id TEXT,
        player_slot INTEGER,
        mode TEXT NOT NULL,
        language TEXT NOT NULL,
        language_locked INTEGER NOT NULL DEFAULT 0,
        language_locked_at TEXT,
        status TEXT NOT NULL,
        secret_hash TEXT,
        access_token_hash TEXT NOT NULL,
        integration_token_hash TEXT NOT NULL,
        bootstrap_token_hash TEXT NOT NULL,
        bootstrap_used_at TEXT,
        creation_key TEXT UNIQUE,
        current_version_id TEXT,
        deadline_started_at TEXT,
        deadline_at TEXT,
        finalized_version_id TEXT,
        finalized_at TEXT,
        completed_at TEXT,
        final_intention_json TEXT,
        draft_rows_json TEXT,
        draft_validation_json TEXT,
        draft_generation INTEGER NOT NULL DEFAULT 1,
        is_demo INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_design_sessions_demo_created
        ON design_sessions(is_demo, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_design_sessions_match_slot
        ON design_sessions(match_id, player_slot)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_design_sessions_demo_expires
        ON design_sessions(is_demo, expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS level_versions (
        version_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES design_sessions(session_id) ON DELETE CASCADE,
        version_number INTEGER NOT NULL,
        source TEXT NOT NULL,
        summary TEXT NOT NULL,
        rows_json TEXT NOT NULL,
        diff_json TEXT NOT NULL DEFAULT '[]',
        design_context_json TEXT,
        solver_json TEXT NOT NULL,
        entity_bindings_json TEXT,
        assessment_json TEXT,
        parent_version_id TEXT,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(session_id, version_number)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_level_versions_session_number
        ON level_versions(session_id, version_number)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_level_versions_idem
        ON level_versions(session_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_turns (
        turn_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES design_sessions(session_id) ON DELETE CASCADE,
        sequence_number INTEGER NOT NULL,
        request_id TEXT,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        language TEXT NOT NULL DEFAULT 'zh-CN',
        guidance_json TEXT,
        translations_json TEXT,
        proposal_binding_json TEXT,
        model TEXT,
        attempts_used INTEGER,
        latency_ms INTEGER,
        version_id TEXT NOT NULL REFERENCES level_versions(version_id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        UNIQUE(session_id, sequence_number)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conversation_turns_session_created
        ON conversation_turns(session_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conversation_turns_session_req_role
        ON conversation_turns(session_id, request_id, role)
    """,
    """
    CREATE TABLE IF NOT EXISTS change_proposals (
        proposal_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES design_sessions(session_id) ON DELETE CASCADE,
        assistant_turn_id TEXT NOT NULL,
        base_version_id TEXT NOT NULL,
        candidate_version_id TEXT,
        status TEXT NOT NULL,
        proposal_json TEXT NOT NULL,
        decision_json TEXT,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_change_proposals_session_status
        ON change_proposals(session_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_change_proposals_session_turn
        ON change_proposals(session_id, assistant_turn_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_change_proposals_idem
        ON change_proposals(session_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS revision_challenges (
        challenge_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES design_sessions(session_id) ON DELETE CASCADE,
        base_version_id TEXT NOT NULL REFERENCES level_versions(version_id) ON DELETE CASCADE,
        source_proposal_turn_id TEXT NOT NULL,
        challenge_turn_id TEXT NOT NULL,
        status TEXT NOT NULL,
        current_reason_turn_id TEXT,
        reviews_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(session_id, challenge_turn_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_revision_challenges_session_updated
        ON revision_challenges(session_id, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS play_attempts (
        attempt_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES design_sessions(session_id) ON DELETE CASCADE,
        version_id TEXT NOT NULL REFERENCES level_versions(version_id) ON DELETE CASCADE,
        mode TEXT NOT NULL,
        language TEXT NOT NULL,
        status TEXT NOT NULL,
        ticket_hash TEXT NOT NULL,
        ticket_expires_at TEXT NOT NULL,
        ticket_used_at TEXT,
        attempt_token_hash TEXT,
        issued_at TEXT NOT NULL,
        loaded_at TEXT,
        first_move_at TEXT,
        finished_at TEXT,
        duration_seconds REAL NOT NULL DEFAULT 0.0,
        move_count INTEGER NOT NULL DEFAULT 0,
        push_count INTEGER NOT NULL DEFAULT 0,
        restart_count INTEGER NOT NULL DEFAULT 0,
        minimum_moves INTEGER NOT NULL DEFAULT -1,
        minimum_pushes INTEGER NOT NULL DEFAULT -1,
        idempotency_key TEXT NOT NULL,
        UNIQUE(session_id, idempotency_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_play_attempts_session_version_issued
        ON play_attempts(session_id, version_id, issued_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS draft_regenerations (
        request_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES design_sessions(session_id) ON DELETE CASCADE,
        idempotency_key TEXT NOT NULL,
        status TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1,
        failure_code TEXT,
        rows_json TEXT,
        validation_json TEXT,
        requested_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(session_id, idempotency_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_draft_regenerations_session_status
        ON draft_regenerations(session_id, status, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_events_session_type_id
        ON audit_events(session_id, event_type, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        session_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        response_json TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (session_id, scope, idempotency_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS binary_discovery_topics (
        session_id TEXT NOT NULL REFERENCES design_sessions(session_id) ON DELETE CASCADE,
        topic_id TEXT NOT NULL,
        state_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (session_id, topic_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_binary_discovery_topics_updated
        ON binary_discovery_topics(session_id, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS component_recovery_tasks (
        recovery_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES design_sessions(session_id) ON DELETE CASCADE,
        target_kind TEXT NOT NULL,
        target_id TEXT NOT NULL,
        state_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_component_recovery_tasks_session_updated
        ON component_recovery_tasks(session_id, updated_at)
    """,
)
