import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from component_recovery import interaction_state
from design_context import add_confirmed_decision, add_open_question, add_rejected_decision, apply_question_answer_review, deduplicate_open_questions, design_level_open_questions, empty_design_context, infer_intent_topic, merge_chat_update, normalize_design_context, question_dedup_key, set_active_disagreement
from level_validation import build_entity_bindings, build_untrusted_entity_bindings, derive_entity_transitions, entity_binding_fingerprint, entity_bindings_match_rows
from ..runtime_ref import database_path
BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = BACKEND_DIR / 'data'
DATABASE_PATH = Path(os.getenv('COCREATION_DATABASE_PATH', DEFAULT_DATA_DIR / 'cocreation.sqlite3'))
SCHEMA = '\nCREATE TABLE IF NOT EXISTS design_sessions (\n    id TEXT PRIMARY KEY,\n    creation_key TEXT NOT NULL UNIQUE,\n    access_hash TEXT NOT NULL,\n    integration_hash TEXT NOT NULL,\n    bootstrap_hash TEXT NOT NULL,\n    bootstrap_used_at TEXT,\n    demo_mode INTEGER NOT NULL DEFAULT 0,\n    deadline_started_at TEXT,\n    deadline_at TEXT,\n    match_id TEXT,\n    player_number INTEGER,\n    initial_draft_method TEXT NOT NULL,\n    language TEXT NOT NULL,\n    language_locked_at TEXT,\n    status TEXT NOT NULL,\n    current_version_id TEXT,\n    final_version_id TEXT,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL,\n    finalized_at TEXT,\n    completed_at TEXT,\n    draft_rows_json TEXT,\n    draft_validation_json TEXT,\n    draft_generation INTEGER NOT NULL DEFAULT 1,\n    draft_regeneration_status TEXT,\n    draft_regeneration_request_id TEXT,\n    draft_regeneration_key TEXT,\n    draft_regeneration_requested_at TEXT,\n    draft_regeneration_updated_at TEXT,\n    draft_regeneration_failure_code TEXT\n);\n\nCREATE TABLE IF NOT EXISTS level_versions (\n    id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    stage_number INTEGER NOT NULL,\n    parent_version_id TEXT,\n    source TEXT NOT NULL,\n    rows_json TEXT NOT NULL,\n    summary TEXT NOT NULL,\n    diff_json TEXT NOT NULL,\n    validation_json TEXT NOT NULL,\n    design_context_json TEXT,\n    entity_bindings_json TEXT,\n    idempotency_key TEXT NOT NULL,\n    created_at TEXT NOT NULL,\n    UNIQUE(session_id, stage_number),\n    UNIQUE(session_id, idempotency_key)\n);\n\nCREATE TABLE IF NOT EXISTS conversation_turns (\n    id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    sequence_number INTEGER NOT NULL,\n    role TEXT NOT NULL,\n    content TEXT NOT NULL,\n    language TEXT NOT NULL,\n    version_id TEXT NOT NULL,\n    request_id TEXT,\n    model TEXT,\n    attempts_used INTEGER,\n    latency_ms INTEGER,\n    guidance_json TEXT,\n    proposal_binding_json TEXT,\n    created_at TEXT NOT NULL,\n    UNIQUE(session_id, sequence_number)\n);\n\nCREATE TABLE IF NOT EXISTS turn_translations (\n    id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    turn_id TEXT NOT NULL REFERENCES conversation_turns(id),\n    language TEXT NOT NULL,\n    body TEXT NOT NULL,\n    guidance_json TEXT,\n    proposal_summary TEXT,\n    model TEXT,\n    attempts_used INTEGER,\n    latency_ms INTEGER,\n    created_at TEXT NOT NULL,\n    UNIQUE(turn_id, language)\n);\n\nCREATE TABLE IF NOT EXISTS llm_assessments (\n    id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    version_id TEXT NOT NULL UNIQUE,\n    assistant_turn_id TEXT NOT NULL,\n    payload_json TEXT NOT NULL,\n    prompt_version TEXT NOT NULL,\n    created_at TEXT NOT NULL\n);\n\nCREATE TABLE IF NOT EXISTS change_proposals (\n    id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    base_version_id TEXT NOT NULL,\n    proposed_rows_json TEXT,\n    summary TEXT NOT NULL,\n    diff_json TEXT NOT NULL,\n    validation_json TEXT NOT NULL,\n    status TEXT NOT NULL,\n    assistant_turn_id TEXT NOT NULL,\n    idempotency_key TEXT NOT NULL,\n    created_at TEXT NOT NULL,\n    decided_at TEXT,\n    UNIQUE(session_id, idempotency_key)\n);\n\nCREATE TABLE IF NOT EXISTS designer_decisions (\n    id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    version_id TEXT,\n    proposal_id TEXT,\n    decision_type TEXT NOT NULL,\n    reason TEXT NOT NULL,\n    idempotency_key TEXT NOT NULL,\n    created_at TEXT NOT NULL,\n    UNIQUE(session_id, idempotency_key)\n);\n\nCREATE TABLE IF NOT EXISTS play_attempts (\n    id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    version_id TEXT NOT NULL,\n    initial_draft_method TEXT NOT NULL,\n    language TEXT NOT NULL,\n    status TEXT NOT NULL,\n    ticket_hash TEXT NOT NULL,\n    ticket_expires_at TEXT NOT NULL,\n    ticket_used_at TEXT,\n    attempt_token_hash TEXT,\n    issued_at TEXT NOT NULL,\n    loaded_at TEXT,\n    first_move_at TEXT,\n    finished_at TEXT,\n    duration_seconds REAL NOT NULL DEFAULT 0,\n    move_count INTEGER NOT NULL DEFAULT 0,\n    push_count INTEGER NOT NULL DEFAULT 0,\n    restart_count INTEGER NOT NULL DEFAULT 0,\n    minimum_moves INTEGER NOT NULL DEFAULT -1,\n    minimum_pushes INTEGER NOT NULL DEFAULT -1,\n    idempotency_key TEXT NOT NULL,\n    UNIQUE(session_id, idempotency_key)\n);\n\nCREATE TABLE IF NOT EXISTS designer_intentions (\n    id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL UNIQUE REFERENCES design_sessions(id),\n    content TEXT NOT NULL,\n    language TEXT NOT NULL,\n    idempotency_key TEXT NOT NULL,\n    created_at TEXT NOT NULL\n);\n\nCREATE TABLE IF NOT EXISTS audit_events (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    session_id TEXT NOT NULL,\n    event_type TEXT NOT NULL,\n    payload_json TEXT NOT NULL,\n    created_at TEXT NOT NULL\n);\n\nCREATE TABLE IF NOT EXISTS revision_challenges (\n    challenge_id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    base_version_id TEXT NOT NULL,\n    source_proposal_turn_id TEXT NOT NULL,\n    challenge_turn_id TEXT NOT NULL,\n    status TEXT NOT NULL,\n    current_reason_turn_id TEXT,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL,\n    UNIQUE(session_id, challenge_turn_id)\n);\n\nCREATE TABLE IF NOT EXISTS challenge_reason_reviews (\n    review_id TEXT PRIMARY KEY,\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    challenge_id TEXT NOT NULL REFERENCES revision_challenges(challenge_id),\n    source_user_turn_id TEXT NOT NULL REFERENCES conversation_turns(id),\n    message_key TEXT NOT NULL,\n    status TEXT NOT NULL,\n    attempts_used INTEGER NOT NULL DEFAULT 0,\n    failure_code TEXT,\n    result_json TEXT,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL,\n    UNIQUE(session_id, source_user_turn_id),\n    UNIQUE(session_id, message_key)\n);\n\nCREATE TABLE IF NOT EXISTS challenge_review_requests (\n    session_id TEXT NOT NULL REFERENCES design_sessions(id),\n    idempotency_key TEXT NOT NULL,\n    review_id TEXT NOT NULL REFERENCES challenge_reason_reviews(review_id),\n    status TEXT NOT NULL,\n    result_json TEXT,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL,\n    PRIMARY KEY(session_id, idempotency_key)\n);\n\nCREATE INDEX IF NOT EXISTS idx_versions_session\n    ON level_versions(session_id, stage_number);\nCREATE INDEX IF NOT EXISTS idx_turns_session\n    ON conversation_turns(session_id, sequence_number);\nCREATE INDEX IF NOT EXISTS idx_turn_translations_session\n    ON turn_translations(session_id, turn_id);\nCREATE INDEX IF NOT EXISTS idx_attempts_version\n    ON play_attempts(session_id, version_id, issued_at);\nCREATE INDEX IF NOT EXISTS idx_revision_challenges_session\n    ON revision_challenges(session_id, updated_at);\nCREATE INDEX IF NOT EXISTS idx_challenge_reviews_challenge\n    ON challenge_reason_reviews(session_id, challenge_id, updated_at);\n'

@contextmanager
def connect(immediate=False):
    database = sqlite3.connect(database_path(), timeout=15)
    database.row_factory = sqlite3.Row
    database.execute('PRAGMA foreign_keys=ON')
    database.execute('PRAGMA busy_timeout=15000')
    try:
        if immediate:
            database.execute('BEGIN IMMEDIATE')
        yield database
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()

def get_session(database, session_id):
    return database.execute('SELECT * FROM design_sessions WHERE id = ?', (session_id,)).fetchone()

def backfill_language_locks(database):
    """Preserve the language of sessions that already entered the workbench."""
    database.execute('\n        UPDATE design_sessions\n        SET language_locked_at = COALESCE(deadline_started_at, created_at)\n        WHERE language_locked_at IS NULL\n          AND (\n              (demo_mode = 1 AND current_version_id IS NOT NULL)\n              OR deadline_started_at IS NOT NULL\n              OR EXISTS (\n                  SELECT 1 FROM conversation_turns\n                  WHERE conversation_turns.session_id = design_sessions.id\n              )\n          )\n        ')

def delete_demo_sessions(database, keep_session_id=None):
    """Delete standalone demo sessions while preserving formal Unity sessions."""
    demo_rows = database.execute("SELECT id FROM design_sessions WHERE demo_mode = 1 AND id != COALESCE(?, '')", (keep_session_id,)).fetchall()
    session_ids = [row['id'] for row in demo_rows]
    if not session_ids:
        return 0
    placeholders = ', '.join(('?' for _ in session_ids))
    delete_statements = ('DELETE FROM challenge_review_requests WHERE session_id IN ({})', 'DELETE FROM challenge_reason_reviews WHERE session_id IN ({})', 'DELETE FROM revision_challenges WHERE session_id IN ({})', 'DELETE FROM designer_decisions WHERE session_id IN ({})', 'DELETE FROM designer_intentions WHERE session_id IN ({})', 'DELETE FROM play_attempts WHERE session_id IN ({})', 'DELETE FROM change_proposals WHERE session_id IN ({})', 'DELETE FROM llm_assessments WHERE session_id IN ({})', 'DELETE FROM turn_translations WHERE session_id IN ({})', 'DELETE FROM conversation_turns WHERE session_id IN ({})', 'DELETE FROM level_versions WHERE session_id IN ({})', 'DELETE FROM audit_events WHERE session_id IN ({})', 'DELETE FROM design_sessions WHERE id IN ({})')
    for statement in delete_statements:
        database.execute(statement.format(placeholders), session_ids)
    return len(session_ids)

def get_version(database, session_id, version_id):
    return database.execute('SELECT * FROM level_versions WHERE session_id = ? AND id = ?', (session_id, version_id)).fetchone()

def get_current_version(database, session):
    return get_version(database, session['id'], session['current_version_id'])

def record_event(database, session_id, event_type, payload, created_at):
    database.execute('\n        INSERT INTO audit_events(session_id, event_type, payload_json, created_at)\n        VALUES (?, ?, ?, ?)\n        ', (session_id, event_type, dump_json(payload), created_at))

def next_turn_sequence(database, session_id):
    return database.execute('\n        SELECT COALESCE(MAX(sequence_number), 0) + 1\n        FROM conversation_turns WHERE session_id = ?\n        ', (session_id,)).fetchone()[0]

def next_stage_number(database, session_id):
    return database.execute('\n        SELECT COALESCE(MAX(stage_number), 0) + 1\n        FROM level_versions WHERE session_id = ?\n        ', (session_id,)).fetchone()[0]

def dump_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def map_fingerprint(rows):
    """Return a stable fingerprint for the complete saved Stage grid."""
    import hashlib
    canonical = json.dumps(list(rows or []), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()

def _public_guidance(value):
    result = dict(value or {}) if isinstance(value, dict) else {}
    result.pop('designContextPatch', None)
    result.pop('designContextPatchError', None)
    result.pop('proposalDiscovery', None)
    result.pop('openingPresentation', None)
    result.pop('_intentHypothesisId', None)
    result.pop('_intentReviewState', None)
    offer = result.get('proposalOffer')
    if isinstance(offer, dict) and 'executionBrief' in offer:
        offer = dict(offer)
        offer.pop('executionBrief', None)
        result['proposalOffer'] = offer
    return result

def load_json(value):
    if value is None:
        return None
    return json.loads(value)

def _ensure_column(database, table_name, column_name, declaration):
    columns = {row[1] for row in database.execute(f'PRAGMA table_info({table_name})').fetchall()}
    if column_name not in columns:
        database.execute(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}')

def _deadline_expired(value):
    return value is not None and _parse_time(value) <= datetime.now(timezone.utc)

def _remaining_deadline_seconds(value):
    if value is None:
        return None
    return max(0, int((_parse_time(value) - datetime.now(timezone.utc)).total_seconds()))

def _parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))
