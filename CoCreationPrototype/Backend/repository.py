import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BACKEND_DIR / "data"
DATABASE_PATH = Path(
    os.getenv("COCREATION_DATABASE_PATH", DEFAULT_DATA_DIR / "cocreation.sqlite3")
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS design_sessions (
    id TEXT PRIMARY KEY,
    creation_key TEXT NOT NULL UNIQUE,
    access_hash TEXT NOT NULL,
    integration_hash TEXT NOT NULL,
    bootstrap_hash TEXT NOT NULL,
    bootstrap_used_at TEXT,
    match_id TEXT,
    player_number INTEGER,
    initial_draft_method TEXT NOT NULL,
    language TEXT NOT NULL,
    status TEXT NOT NULL,
    current_version_id TEXT,
    final_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finalized_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS level_versions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    stage_number INTEGER NOT NULL,
    parent_version_id TEXT,
    source TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, stage_number),
    UNIQUE(session_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    sequence_number INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    language TEXT NOT NULL,
    version_id TEXT NOT NULL,
    request_id TEXT,
    model TEXT,
    attempts_used INTEGER,
    latency_ms INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS llm_assessments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    version_id TEXT NOT NULL UNIQUE,
    assistant_turn_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_proposals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    base_version_id TEXT NOT NULL,
    proposed_rows_json TEXT,
    summary TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    status TEXT NOT NULL,
    assistant_turn_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(session_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS designer_decisions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    version_id TEXT,
    proposal_id TEXT,
    decision_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS play_attempts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    version_id TEXT NOT NULL,
    initial_draft_method TEXT NOT NULL,
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
    duration_seconds REAL NOT NULL DEFAULT 0,
    move_count INTEGER NOT NULL DEFAULT 0,
    push_count INTEGER NOT NULL DEFAULT 0,
    restart_count INTEGER NOT NULL DEFAULT 0,
    minimum_moves INTEGER NOT NULL DEFAULT -1,
    minimum_pushes INTEGER NOT NULL DEFAULT -1,
    idempotency_key TEXT NOT NULL,
    UNIQUE(session_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS designer_intentions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE REFERENCES design_sessions(id),
    content TEXT NOT NULL,
    language TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_versions_session
    ON level_versions(session_id, stage_number);
CREATE INDEX IF NOT EXISTS idx_turns_session
    ON conversation_turns(session_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_attempts_version
    ON play_attempts(session_id, version_id, issued_at);
"""


def initialize_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    database = sqlite3.connect(DATABASE_PATH)

    try:
        database.executescript(SCHEMA)
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA foreign_keys=ON")
        database.commit()
    finally:
        database.close()


@contextmanager
def connect(immediate=False):
    database = sqlite3.connect(DATABASE_PATH, timeout=15)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    database.execute("PRAGMA busy_timeout=15000")

    try:
        if immediate:
            database.execute("BEGIN IMMEDIATE")

        yield database
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def get_session(database, session_id):
    return database.execute(
        "SELECT * FROM design_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()


def get_version(database, session_id, version_id):
    return database.execute(
        "SELECT * FROM level_versions WHERE session_id = ? AND id = ?",
        (session_id, version_id),
    ).fetchone()


def get_current_version(database, session):
    return get_version(database, session["id"], session["current_version_id"])


def record_event(database, session_id, event_type, payload, created_at):
    database.execute(
        """
        INSERT INTO audit_events(session_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (session_id, event_type, dump_json(payload), created_at),
    )


def serialize_session(database, session_id):
    session = get_session(database, session_id)

    if session is None:
        return None

    versions = database.execute(
        """
        SELECT * FROM level_versions
        WHERE session_id = ? ORDER BY stage_number
        """,
        (session_id,),
    ).fetchall()
    turns = database.execute(
        """
        SELECT id, sequence_number, role, content, language, version_id,
               request_id, created_at
        FROM conversation_turns
        WHERE session_id = ? ORDER BY sequence_number
        """,
        (session_id,),
    ).fetchall()
    assessments = database.execute(
        """
        SELECT id, version_id, assistant_turn_id, payload_json, created_at
        FROM llm_assessments WHERE session_id = ?
        """,
        (session_id,),
    ).fetchall()
    proposals = database.execute(
        """
        SELECT id, base_version_id, proposed_rows_json, summary, diff_json,
               validation_json, status, assistant_turn_id, created_at, decided_at
        FROM change_proposals WHERE session_id = ? ORDER BY created_at
        """,
        (session_id,),
    ).fetchall()
    attempts = database.execute(
        """
        SELECT id, version_id, status, issued_at, loaded_at, first_move_at,
               finished_at, duration_seconds, move_count, push_count,
               restart_count, minimum_moves, minimum_pushes
        FROM play_attempts WHERE session_id = ? ORDER BY issued_at DESC
        """,
        (session_id,),
    ).fetchall()
    intention = database.execute(
        """
        SELECT content, language, created_at
        FROM designer_intentions WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()

    attempts_by_version = {}

    for attempt in attempts:
        attempts_by_version.setdefault(attempt["version_id"], []).append(
            _serialize_attempt(attempt)
        )

    return {
        "sessionId": session["id"],
        "status": session["status"],
        "language": session["language"],
        "initialDraftMethod": session["initial_draft_method"],
        "matchId": session["match_id"],
        "playerNumber": session["player_number"],
        "currentVersionId": session["current_version_id"],
        "finalVersionId": session["final_version_id"],
        "createdAt": session["created_at"],
        "updatedAt": session["updated_at"],
        "versions": [
            {
                "versionId": version["id"],
                "stageNumber": version["stage_number"],
                "parentVersionId": version["parent_version_id"],
                "source": version["source"],
                "rows": load_json(version["rows_json"]),
                "summary": version["summary"],
                "diff": load_json(version["diff_json"]),
                "validation": load_json(version["validation_json"]),
                "createdAt": version["created_at"],
                "playAttempts": attempts_by_version.get(version["id"], []),
            }
            for version in versions
        ],
        "turns": [
            {
                "turnId": turn["id"],
                "sequence": turn["sequence_number"],
                "role": turn["role"],
                "content": turn["content"],
                "language": turn["language"],
                "versionId": turn["version_id"],
                "requestId": turn["request_id"],
                "createdAt": turn["created_at"],
            }
            for turn in turns
        ],
        "assessments": [
            {
                "assessmentId": assessment["id"],
                "versionId": assessment["version_id"],
                "assistantTurnId": assessment["assistant_turn_id"],
                "payload": load_json(assessment["payload_json"]),
                "createdAt": assessment["created_at"],
            }
            for assessment in assessments
        ],
        "proposals": [
            {
                "proposalId": proposal["id"],
                "baseVersionId": proposal["base_version_id"],
                "proposedRows": load_json(proposal["proposed_rows_json"]),
                "summary": proposal["summary"],
                "diff": load_json(proposal["diff_json"]),
                "validation": load_json(proposal["validation_json"]),
                "status": proposal["status"],
                "assistantTurnId": proposal["assistant_turn_id"],
                "createdAt": proposal["created_at"],
                "decidedAt": proposal["decided_at"],
            }
            for proposal in proposals
        ],
        "intention": (
            {
                "content": intention["content"],
                "language": intention["language"],
                "createdAt": intention["created_at"],
            }
            if intention is not None
            else None
        ),
    }


def next_turn_sequence(database, session_id):
    return database.execute(
        """
        SELECT COALESCE(MAX(sequence_number), 0) + 1
        FROM conversation_turns WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()[0]


def next_stage_number(database, session_id):
    return database.execute(
        """
        SELECT COALESCE(MAX(stage_number), 0) + 1
        FROM level_versions WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()[0]


def dump_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_json(value):
    if value is None:
        return None

    return json.loads(value)


def _serialize_attempt(attempt):
    return {
        "attemptId": attempt["id"],
        "status": attempt["status"],
        "issuedAt": attempt["issued_at"],
        "loadedAt": attempt["loaded_at"],
        "firstMoveAt": attempt["first_move_at"],
        "finishedAt": attempt["finished_at"],
        "durationSeconds": attempt["duration_seconds"],
        "moveCount": attempt["move_count"],
        "pushCount": attempt["push_count"],
        "restartCount": attempt["restart_count"],
        "minimumMoves": attempt["minimum_moves"],
        "minimumPushes": attempt["minimum_pushes"],
    }


initialize_database()
