from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from cocreation.clock import Clock, SYSTEM_CLOCK

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = BACKEND_DIR / "data"
DEFAULT_DATABASE_PATH = DEFAULT_DATA_DIR / "cocreation.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS design_sessions (
    id TEXT PRIMARY KEY,
    creation_key TEXT NOT NULL UNIQUE,
    access_hash TEXT NOT NULL,
    integration_hash TEXT NOT NULL,
    bootstrap_hash TEXT NOT NULL,
    bootstrap_used_at TEXT,
    demo_mode INTEGER NOT NULL DEFAULT 0,
    deadline_started_at TEXT,
    deadline_at TEXT,
    match_id TEXT,
    player_number INTEGER,
    initial_draft_method TEXT NOT NULL,
    language TEXT NOT NULL,
    language_locked_at TEXT,
    status TEXT NOT NULL,
    current_version_id TEXT,
    final_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finalized_at TEXT,
    completed_at TEXT,
    draft_rows_json TEXT,
    draft_validation_json TEXT,
    draft_generation INTEGER NOT NULL DEFAULT 1,
    draft_regeneration_status TEXT,
    draft_regeneration_request_id TEXT,
    draft_regeneration_key TEXT,
    draft_regeneration_requested_at TEXT,
    draft_regeneration_updated_at TEXT,
    draft_regeneration_failure_code TEXT
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
    design_context_json TEXT,
    entity_bindings_json TEXT,
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
    guidance_json TEXT,
    proposal_binding_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS turn_translations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    turn_id TEXT NOT NULL REFERENCES conversation_turns(id),
    language TEXT NOT NULL,
    body TEXT NOT NULL,
    guidance_json TEXT,
    proposal_summary TEXT,
    model TEXT,
    attempts_used INTEGER,
    latency_ms INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(turn_id, language)
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

CREATE TABLE IF NOT EXISTS revision_challenges (
    challenge_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    base_version_id TEXT NOT NULL,
    source_proposal_turn_id TEXT NOT NULL,
    challenge_turn_id TEXT NOT NULL,
    status TEXT NOT NULL,
    current_reason_turn_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, challenge_turn_id)
);

CREATE TABLE IF NOT EXISTS challenge_reason_reviews (
    review_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    challenge_id TEXT NOT NULL REFERENCES revision_challenges(challenge_id),
    source_user_turn_id TEXT NOT NULL REFERENCES conversation_turns(id),
    message_key TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts_used INTEGER NOT NULL DEFAULT 0,
    failure_code TEXT,\n    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, source_user_turn_id),
    UNIQUE(session_id, message_key)
);

CREATE TABLE IF NOT EXISTS challenge_review_requests (
    session_id TEXT NOT NULL REFERENCES design_sessions(id),
    idempotency_key TEXT NOT NULL,
    review_id TEXT NOT NULL REFERENCES challenge_reason_reviews(review_id),
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(session_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_versions_session
    ON level_versions(session_id, stage_number);
CREATE INDEX IF NOT EXISTS idx_turns_session
    ON conversation_turns(session_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_turn_translations_session
    ON turn_translations(session_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_attempts_version
    ON play_attempts(session_id, version_id, issued_at);
CREATE INDEX IF NOT EXISTS idx_revision_challenges_session
    ON revision_challenges(session_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_challenge_reviews_challenge
    ON challenge_reason_reviews(session_id, challenge_id, updated_at);
"""


@contextmanager
def connect(db_path: Path | str, immediate: bool = False):
    database = sqlite3.connect(Path(db_path), timeout=15)
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


class SQLiteRepository:
    """Thread-safe SQLite repository with lazy schema/migration initialization."""

    def __init__(self, db_path: Path | str, clock: Clock | None = None):
        self.db_path = Path(db_path)
        self.clock: Clock = clock or SYSTEM_CLOCK
        self._init_lock = Lock()
        self._initialized = False

    def initialize(self) -> None:
        from .session_projection import initialize_database

        file_missing = str(self.db_path) != ":memory:" and not self.db_path.exists()
        if self._initialized and not file_missing:
            return
        with self._init_lock:
            file_missing = str(self.db_path) != ":memory:" and not self.db_path.exists()
            if self._initialized and not file_missing:
                return
            initialize_database(self.db_path, clock=self.clock)
            self._initialized = True

    @contextmanager
    def connect(self, immediate: bool = False):
        self.initialize()
        with connect(self.db_path, immediate=immediate) as database:
            yield database

    def serialize_session(self, database: sqlite3.Connection, session_id: str, *, now: datetime | None = None):
        from .session_projection import serialize_session

        effective_now = now if now is not None else self.clock.utc_datetime()
        return serialize_session(database, session_id, now=effective_now, clock=self.clock)


def get_session(database, session_id):
    return database.execute("SELECT * FROM design_sessions WHERE id = ?", (session_id,)).fetchone()


def backfill_language_locks(database):
    """Preserve the language of sessions that already entered the workbench."""
    database.execute(
        """
        UPDATE design_sessions
        SET language_locked_at = COALESCE(deadline_started_at, created_at)
        WHERE language_locked_at IS NULL
          AND (
              (demo_mode = 1 AND current_version_id IS NOT NULL)
              OR deadline_started_at IS NOT NULL
              OR EXISTS (
                  SELECT 1 FROM conversation_turns
                  WHERE conversation_turns.session_id = design_sessions.id
              )
          )
        """
    )


def delete_demo_sessions(database, keep_session_id=None):
    """Delete standalone demo sessions while preserving formal Unity sessions."""
    demo_rows = database.execute(
        "SELECT id FROM design_sessions WHERE demo_mode = 1 AND id != COALESCE(?, '')",
        (keep_session_id,),
    ).fetchall()
    session_ids = [row["id"] for row in demo_rows]
    if not session_ids:
        return 0
    placeholders = ", ".join(("?" for _ in session_ids))
    delete_statements = (
        "DELETE FROM challenge_review_requests WHERE session_id IN ({})",
        "DELETE FROM challenge_reason_reviews WHERE session_id IN ({})",
        "DELETE FROM revision_challenges WHERE session_id IN ({})",
        "DELETE FROM designer_decisions WHERE session_id IN ({})",
        "DELETE FROM designer_intentions WHERE session_id IN ({})",
        "DELETE FROM play_attempts WHERE session_id IN ({})",
        "DELETE FROM change_proposals WHERE session_id IN ({})",
        "DELETE FROM llm_assessments WHERE session_id IN ({})",
        "DELETE FROM turn_translations WHERE session_id IN ({})",
        "DELETE FROM conversation_turns WHERE session_id IN ({})",
        "DELETE FROM level_versions WHERE session_id IN ({})",
        "DELETE FROM audit_events WHERE session_id IN ({})",
        "DELETE FROM design_sessions WHERE id IN ({})",
    )
    for statement in delete_statements:
        database.execute(statement.format(placeholders), session_ids)
    return len(session_ids)


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


def map_fingerprint(rows):
    """Return a stable fingerprint for the complete saved Stage grid."""
    canonical = json.dumps(list(rows or []), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _public_guidance(value):
    result = dict(value or {}) if isinstance(value, dict) else {}
    result.pop("designContextPatch", None)
    result.pop("designContextPatchError", None)
    result.pop("proposalDiscovery", None)
    result.pop("openingPresentation", None)
    result.pop("_intentHypothesisId", None)
    result.pop("_intentReviewState", None)
    offer = result.get("proposalOffer")
    if isinstance(offer, dict) and "executionBrief" in offer:
        offer = dict(offer)
        offer.pop("executionBrief", None)
        result["proposalOffer"] = offer
    return result


def load_json(value):
    if value is None:
        return None
    return json.loads(value)


def _ensure_column(database, table_name, column_name, declaration):
    columns = {row[1] for row in database.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in columns:
        database.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}")


def _deadline_expired(value, *, now: datetime | None = None):
    current = now if now is not None else datetime.now(timezone.utc)
    return value is not None and _parse_time(value) <= current


def _remaining_deadline_seconds(value, *, now: datetime | None = None):
    if value is None:
        return None
    current = now if now is not None else datetime.now(timezone.utc)
    return max(0, int((_parse_time(value) - current).total_seconds()))


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
