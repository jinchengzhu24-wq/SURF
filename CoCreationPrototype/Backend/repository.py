import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from design_context import (
    add_confirmed_decision,
    add_open_question,
    add_rejected_decision,
    design_level_open_questions,
    empty_design_context,
    infer_intent_topic,
    merge_chat_update,
    normalize_design_context,
    set_active_disagreement,
)
from level_validation import (
    build_entity_bindings,
    build_untrusted_entity_bindings,
    derive_entity_transitions,
    entity_binding_fingerprint,
    entity_bindings_match_rows,
)


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
    failure_code TEXT,
    result_json TEXT,
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


def initialize_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    database = sqlite3.connect(DATABASE_PATH)
    database.row_factory = sqlite3.Row

    try:
        database.executescript(SCHEMA)
        _ensure_column(database, "conversation_turns", "guidance_json", "TEXT")
        _ensure_column(database, "conversation_turns", "proposal_binding_json", "TEXT")
        _ensure_column(database, "level_versions", "design_context_json", "TEXT")
        _ensure_column(database, "level_versions", "entity_bindings_json", "TEXT")
        _ensure_column(database, "design_sessions", "demo_mode", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(database, "design_sessions", "deadline_started_at", "TEXT")
        _ensure_column(database, "design_sessions", "deadline_at", "TEXT")
        _ensure_column(database, "design_sessions", "language_locked_at", "TEXT")
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA foreign_keys=ON")
        database.commit()
        backfill_design_contexts(database)
        backfill_entity_bindings(database)
        backfill_revision_challenges(database)
        backfill_language_locks(database)
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


def backfill_language_locks(database):
    """Preserve the language of sessions that already entered the workbench."""
    database.execute(
        """
        UPDATE design_sessions
        SET language_locked_at = COALESCE(deadline_started_at, created_at)
        WHERE language_locked_at IS NULL
          AND (
              demo_mode = 1
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

    placeholders = ", ".join("?" for _ in session_ids)
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


def load_design_context(database, session_id, version_id):
    row = database.execute(
        "SELECT design_context_json FROM level_versions WHERE session_id = ? AND id = ?",
        (session_id, version_id),
    ).fetchone()
    if row is None:
        return empty_design_context()
    raw = row["design_context_json"]
    if not raw:
        return empty_design_context()
    try:
        return normalize_design_context(load_json(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return empty_design_context()


def save_design_context(database, version_id, context):
    normalized = normalize_design_context(context)
    database.execute(
        "UPDATE level_versions SET design_context_json = ? WHERE id = ?",
        (dump_json(normalized), version_id),
    )
    return normalized


def load_entity_bindings(database, session_id, version_id):
    row = database.execute(
        "SELECT entity_bindings_json FROM level_versions WHERE session_id = ? AND id = ?",
        (session_id, version_id),
    ).fetchone()
    if row is None or not row["entity_bindings_json"]:
        return None
    try:
        value = load_json(row["entity_bindings_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def save_entity_bindings(database, version_id, bindings):
    if not isinstance(bindings, dict):
        raise ValueError("entity bindings must be an object")
    normalized = dict(bindings)
    normalized["bindingFingerprint"] = entity_binding_fingerprint(normalized)
    database.execute(
        "UPDATE level_versions SET entity_bindings_json = ? WHERE id = ?",
        (dump_json(normalized), version_id),
    )
    return normalized


def _has_valid_design_context(raw):
    if not raw:
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("schemaVersion") == 4


def _legacy_visible_questions(content, guidance):
    """Extract questions from fields old clients actually rendered, excluding intent cards."""
    guidance = guidance if isinstance(guidance, dict) else {}
    values = [content, guidance.get("followUpQuestion")]
    disagreement = guidance.get("disagreement")
    if isinstance(disagreement, dict):
        values.extend([disagreement.get("coreDisagreement"), disagreement.get("nextQuestion")])
    offer = guidance.get("proposalOffer")
    if isinstance(offer, dict):
        values.extend([offer.get("summary"), offer.get("rationale")])
    values.extend(
        cue.get("text")
        for cue in guidance.get("uiCues") or []
        if isinstance(cue, dict)
    )
    result = []
    seen = set()
    for value in values:
        for segment in re.split(
            r"\s*(?:\n+|(?<=[.!?\u3002\uFF01\uFF1F]))\s*", str(value or "")
        ):
            question = re.sub(r"\s+", " ", segment).strip()
            key = question.strip("?\uFF1F").casefold()
            if question.endswith(("?", "\uFF1F")) and key and key not in seen:
                seen.add(key)
                result.append(question[:1200])
    return result


def _has_valid_entity_bindings(raw, rows=None):
    if not raw:
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not (
        isinstance(value, dict)
        and value.get("schemaVersion") == 1
        and isinstance(value.get("entities"), list)
        and bool(value.get("mapFingerprint"))
        and value.get("bindingFingerprint") == entity_binding_fingerprint(value)
    ):
        return False
    return rows is None or entity_bindings_match_rows(value, rows)


def backfill_entity_bindings(database):
    """Seed persistent entity identities without making model-based guesses.

    Existing versions are processed in Stage order.  The level validation
    layer only inherits unchanged or uniquely provable moves; ambiguous legacy
    edits are deliberately recorded as partial/unknown.
    """
    sessions = database.execute(
        "SELECT id FROM design_sessions ORDER BY created_at, id"
    ).fetchall()
    for session in sessions:
        versions = database.execute(
            "SELECT * FROM level_versions WHERE session_id = ? ORDER BY stage_number, created_at",
            (session["id"],),
        ).fetchall()
        for version in versions:
            rows = load_json(version["rows_json"])
            if _has_valid_entity_bindings(version["entity_bindings_json"], rows):
                continue
            parent = (
                database.execute(
                    "SELECT * FROM level_versions WHERE session_id = ? AND id = ?",
                    (session["id"], version["parent_version_id"]),
                ).fetchone()
                if version["parent_version_id"]
                else None
            )
            parent_bindings = (
                load_json(parent["entity_bindings_json"])
                if parent is not None and parent["entity_bindings_json"]
                else None
            )
            parent_rows = load_json(parent["rows_json"]) if parent is not None else None
            try:
                transitions = (
                    derive_entity_transitions(parent_rows, rows)
                    if parent_rows is not None
                    else None
                )
                bindings = build_entity_bindings(
                    rows,
                    parent_bindings=parent_bindings,
                    entity_transitions=transitions,
                    source="legacy_backfill",
                )
            except (ValueError, TypeError):
                # Historical semantic validation rules must not prevent the
                # service from starting. These identities are not execution
                # safe and remain unknown until a valid child Stage proves
                # them again.
                bindings = build_untrusted_entity_bindings(
                    rows,
                    source="legacy_untrusted",
                )
            database.execute(
                "UPDATE level_versions SET entity_bindings_json = ? WHERE id = ?",
                (dump_json(bindings), version["id"]),
            )
            record_event(
                database,
                session["id"],
                "entity_binding_backfilled",
                {
                    "versionId": version["id"],
                    "stageNumber": version["stage_number"],
                    "identityStatus": bindings["identityStatus"],
                    "bindingFingerprint": bindings["bindingFingerprint"],
                },
                version["created_at"],
            )


def backfill_design_contexts(database):
    """Conservatively seed snapshots for databases created before DesignContext.

    This performs no model calls. Only user turns, formal decisions, and
    explicitly structured legacy disagreement fields are considered. Historical
    assistant intent prose is deliberately not promoted into semantic memory.
    """
    sessions = database.execute("SELECT id FROM design_sessions ORDER BY created_at, id").fetchall()
    changed = 0
    for session in sessions:
        versions = database.execute(
            "SELECT * FROM level_versions WHERE session_id = ? ORDER BY stage_number, created_at",
            (session["id"],),
        ).fetchall()
        for version in versions:
            if _has_valid_design_context(version["design_context_json"]):
                continue

            legacy_context = None
            legacy_schema_version = None
            if version["design_context_json"]:
                try:
                    raw_context = load_json(version["design_context_json"])
                    if isinstance(raw_context, dict) and raw_context.get("schemaVersion") in {1, 2, 3}:
                        legacy_schema_version = raw_context.get("schemaVersion")
                        legacy_context = normalize_design_context(raw_context)
                except (TypeError, ValueError, json.JSONDecodeError):
                    legacy_context = None

            if legacy_context is not None:
                if version["parent_version_id"]:
                    parent_context = load_design_context(
                        database, session["id"], version["parent_version_id"]
                    )
                    known = {item.get("id") for item in legacy_context["openQuestions"]}
                    legacy_context["openQuestions"] = [
                        *[
                            item for item in parent_context.get("openQuestions", [])
                            if item.get("id") not in known
                        ],
                        *legacy_context["openQuestions"],
                    ]
                assistant_turns = database.execute(
                    """
                    SELECT id, content, guidance_json FROM conversation_turns
                    WHERE session_id = ? AND version_id = ? AND role = 'assistant'
                    ORDER BY sequence_number
                    """,
                    (session["id"], version["id"]),
                ).fetchall()
                for turn in assistant_turns:
                    for question in _legacy_visible_questions(
                        turn["content"], load_json(turn["guidance_json"]) or {}
                    ):
                        legacy_context = add_open_question(
                            legacy_context,
                            question,
                            version["id"],
                            turn["id"],
                            source_kind="visible_output",
                        )
                legacy_context["updatedFromStageId"] = version["id"]
                save_design_context(database, version["id"], legacy_context)
                database.execute(
                    """
                    INSERT INTO audit_events(session_id, event_type, payload_json, created_at)
                    VALUES (?, 'design_context_migrated', ?, ?)
                    """,
                    (
                        session["id"],
                        dump_json({
                            "versionId": version["id"],
                            "fromSchemaVersion": legacy_schema_version,
                            "toSchemaVersion": 4,
                        }),
                        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    ),
                )
                changed += 1
                continue

            context = (
                load_design_context(database, session["id"], version["parent_version_id"])
                if version["parent_version_id"]
                else empty_design_context()
            )
            turns = database.execute(
                """
                SELECT id, role, content, guidance_json
                FROM conversation_turns
                WHERE session_id = ? AND version_id = ?
                ORDER BY sequence_number
                """,
                (session["id"], version["id"]),
            ).fetchall()
            for turn in turns:
                if turn["role"] == "user":
                    context = merge_chat_update(
                        context,
                        user_text=turn["content"],
                        stage_id=version["id"],
                        turn_id=turn["id"],
                    )
                    continue
                guidance = load_json(turn["guidance_json"]) or {}
                for question in _legacy_visible_questions(turn["content"], guidance):
                    context = add_open_question(
                        context,
                        question,
                        version["id"],
                        turn["id"],
                        source_kind="visible_output",
                    )
                disagreement = guidance.get("disagreement")
                if isinstance(disagreement, dict) and disagreement.get("status") == "active":
                    context = set_active_disagreement(
                        context, disagreement, version["id"], turn["id"]
                    )

            decisions = database.execute(
                """
                SELECT decision.*, proposal.summary, proposal.assistant_turn_id,
                       proposal.base_version_id
                FROM designer_decisions AS decision
                LEFT JOIN change_proposals AS proposal ON proposal.id = decision.proposal_id
                WHERE decision.session_id = ?
                  AND (decision.version_id = ? OR proposal.base_version_id = ?)
                ORDER BY decision.created_at
                """,
                (session["id"], version["id"], version["id"]),
            ).fetchall()
            for decision in decisions:
                if decision["decision_type"] == "accept" and decision["version_id"] == version["id"]:
                    context = add_confirmed_decision(
                        context,
                        decision["summary"] or "Accepted the proposed map revision",
                        decision["reason"] or "Designer accepted the validated proposal.",
                        version["id"],
                        decision["assistant_turn_id"],
                        decision["proposal_id"],
                    )
                elif decision["decision_type"] == "reject" and decision["proposal_id"] is not None:
                    context = add_rejected_decision(
                        context,
                        decision["summary"] or "Rejected the proposed map revision",
                        decision["reason"] or "",
                        version["id"],
                        decision["assistant_turn_id"],
                        decision["proposal_id"],
                    )

            context["updatedFromStageId"] = version["id"]
            save_design_context(database, version["id"], context)
            event_exists = database.execute(
                """
                SELECT 1 FROM audit_events
                WHERE session_id = ? AND event_type = 'design_context_backfilled'
                  AND json_extract(payload_json, '$.versionId') = ?
                LIMIT 1
                """,
                (session["id"], version["id"]),
            ).fetchone()
            if event_exists is None:
                database.execute(
                    """
                    INSERT INTO audit_events(session_id, event_type, payload_json, created_at)
                    VALUES (?, 'design_context_backfilled', ?, ?)
                    """,
                    (
                        session["id"],
                        dump_json({"versionId": version["id"], "schemaVersion": 4}),
                        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    ),
                )
            changed += 1
    return changed


def _stable_challenge_review_id(challenge_id, source_user_turn_id):
    seed = f"{challenge_id}:{source_user_turn_id}".encode("utf-8")
    return "cr-" + hashlib.sha256(seed).hexdigest()[:24]


def backfill_revision_challenges(database):
    """Restore canonical challenge state from structured audit records only."""
    preexisting_challenge_ids = {
        row["challenge_id"]
        for row in database.execute("SELECT challenge_id FROM revision_challenges").fetchall()
    }
    migrated_challenge_ids = set()
    rows = database.execute(
        """
        SELECT session_id, event_type, payload_json, created_at
        FROM audit_events
        WHERE event_type IN (
          'proposal_challenge_hypotheses_recorded',
          'challenge_reason_review_pending',
          'challenge_reason_review_resolved'
        )
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        payload = load_json(row["payload_json"]) or {}
        challenge_id = str(payload.get("challengeId") or "").strip()
        if not challenge_id:
            continue
        if challenge_id in preexisting_challenge_ids:
            continue
        if row["event_type"] == "proposal_challenge_hypotheses_recorded":
            challenge_turn_id = str(payload.get("challengeTurnId") or "").strip()
            source_turn_id = str(payload.get("sourceTurnId") or "").strip()
            base_version_id = str(payload.get("baseVersionId") or "").strip()
            if not challenge_turn_id or not source_turn_id or not base_version_id:
                continue
            database.execute(
                """
                INSERT OR IGNORE INTO revision_challenges(
                    challenge_id, session_id, base_version_id,
                    source_proposal_turn_id, challenge_turn_id, status,
                    current_reason_turn_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'awaiting_reason', NULL, ?, ?)
                """,
                (
                    challenge_id,
                    row["session_id"],
                    base_version_id,
                    source_turn_id,
                    challenge_turn_id,
                    row["created_at"],
                    row["created_at"],
                ),
            )
            migrated_challenge_ids.add(challenge_id)
            continue

        source_user_turn_id = str(payload.get("sourceUserTurnId") or "").strip()
        message_key = str(payload.get("messageKey") or "").strip()
        if not source_user_turn_id or not message_key:
            continue
        challenge = database.execute(
            "SELECT challenge_id FROM revision_challenges WHERE challenge_id = ? AND session_id = ?",
            (challenge_id, row["session_id"]),
        ).fetchone()
        if challenge is None:
            continue
        review_id = _stable_challenge_review_id(challenge_id, source_user_turn_id)
        review_status = (
            "review_pending"
            if row["event_type"] == "challenge_reason_review_pending"
            else "resolved"
        )
        challenge_status = (
            "review_pending"
            if review_status == "review_pending"
            else str(payload.get("state") or "reason_review")
        )
        database.execute(
            """
            INSERT INTO challenge_reason_reviews(
                review_id, session_id, challenge_id, source_user_turn_id,
                message_key, status, attempts_used, failure_code, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, source_user_turn_id) DO UPDATE SET
                status = excluded.status,
                attempts_used = excluded.attempts_used,
                failure_code = excluded.failure_code,
                result_json = excluded.result_json,
                updated_at = excluded.updated_at
            """,
            (
                review_id,
                row["session_id"],
                challenge_id,
                source_user_turn_id,
                message_key,
                review_status,
                int(payload.get("attemptsUsed") or 0),
                payload.get("failureCode"),
                dump_json(payload) if review_status == "resolved" else None,
                row["created_at"],
                row["created_at"],
            ),
        )
        database.execute(
            """
            UPDATE revision_challenges
            SET current_reason_turn_id = ?, status = ?, updated_at = ?
            WHERE challenge_id = ? AND session_id = ?
            """,
            (
                source_user_turn_id,
                challenge_status,
                row["created_at"],
                challenge_id,
                row["session_id"],
            ),
        )

    # Preserve all reasons while exposing only the latest unresolved one as
    # actionable after migration.
    challenges = database.execute(
        "SELECT challenge_id, current_reason_turn_id FROM revision_challenges"
    ).fetchall()
    for challenge in challenges:
        if (
            challenge["challenge_id"] not in migrated_challenge_ids
            or not challenge["current_reason_turn_id"]
        ):
            continue
        database.execute(
            """
            UPDATE challenge_reason_reviews
            SET status = 'superseded'
            WHERE challenge_id = ? AND status = 'review_pending'
              AND source_user_turn_id != ?
            """,
            (challenge["challenge_id"], challenge["current_reason_turn_id"]),
        )
    for challenge_id in migrated_challenge_ids:
        challenge_session = database.execute(
            "SELECT session_id FROM revision_challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if challenge_session is None:
            continue
        guidance_rows = database.execute(
            """
            SELECT guidance_json, created_at FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND guidance_json IS NOT NULL
            ORDER BY sequence_number DESC
            """,
            (challenge_session["session_id"],),
        ).fetchall()
        for guidance_row in guidance_rows:
            guidance = load_json(guidance_row["guidance_json"]) or {}
            challenge_state = guidance.get("challengeState") or {}
            if challenge_state.get("challengeId") != challenge_id:
                continue
            status = str(challenge_state.get("status") or "").strip()
            if status:
                database.execute(
                    "UPDATE revision_challenges SET status = ?, updated_at = ? WHERE challenge_id = ?",
                    (status, guidance_row["created_at"], challenge_id),
                )
            break


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
               request_id, guidance_json, proposal_binding_json, created_at
        FROM conversation_turns
        WHERE session_id = ? ORDER BY sequence_number
        """,
        (session_id,),
    ).fetchall()
    translations = database.execute(
        """
        SELECT turn_id, language, body, guidance_json, proposal_summary,
               created_at
        FROM turn_translations
        WHERE session_id = ? ORDER BY created_at
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
    accepted_openings = database.execute(
        """
        SELECT decision.version_id, decision.proposal_id,
               proposal.assistant_turn_id
        FROM designer_decisions AS decision
        JOIN change_proposals AS proposal
          ON proposal.id = decision.proposal_id
        WHERE decision.session_id = ?
          AND decision.decision_type = 'accept'
          AND decision.version_id IS NOT NULL
        ORDER BY decision.created_at
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
    proposal_request_keys = {
        str(row["message_key"])
        for row in database.execute(
            """
            SELECT json_extract(payload_json, '$.messageKey') AS message_key
            FROM audit_events
            WHERE session_id = ? AND event_type = 'proposal_request_requested'
            """,
            (session_id,),
        ).fetchall()
        if row["message_key"]
    }
    challenge_review_rows = database.execute(
        """
        SELECT review_id, challenge_id, source_user_turn_id, message_key,
               status, attempts_used, failure_code, result_json, updated_at
        FROM challenge_reason_reviews
        WHERE session_id = ? ORDER BY created_at, review_id
        """,
        (session_id,),
    ).fetchall()
    challenge_reviews = {
        row["message_key"]: {
            **(load_json(row["result_json"]) or {}),
            "reviewId": row["review_id"],
            "challengeId": row["challenge_id"],
            "sourceUserTurnId": row["source_user_turn_id"],
            "messageKey": row["message_key"],
            "status": row["status"],
            "attemptsUsed": row["attempts_used"],
            "failureCode": row["failure_code"],
            "retryable": False,
            "updatedAt": row["updated_at"],
        }
        for row in challenge_review_rows
    }
    execution_outcomes = {}
    for row in database.execute(
        """
        SELECT payload_json FROM audit_events
        WHERE session_id = ? AND event_type = 'revision_execution_outcome'
        ORDER BY id
        """,
        (session_id,),
    ).fetchall():
        payload = load_json(row["payload_json"]) or {}
        if payload.get("versionId"):
            execution_outcomes[payload["versionId"]] = payload
    intent_audit_rows = database.execute(
        """
        SELECT id, event_type, payload_json, created_at FROM audit_events
        WHERE session_id = ? AND event_type IN (
          'intent_evidence_recorded',
          'intent_hypothesis_feedback_applied',
          'intent_hypothesis_feedback_pending',
          'intent_progress_rewrite'
        )
        ORDER BY id
        """,
        (session_id,),
    ).fetchall()
    intent_evidence_by_id = {}
    latest_intent_feedback = {}
    progress_rewrites = {}
    for row in intent_audit_rows:
        payload = load_json(row["payload_json"]) or {}
        if row["event_type"] == "intent_evidence_recorded":
            evidence_id = payload.get("evidenceId")
            if evidence_id:
                intent_evidence_by_id[evidence_id] = {
                    **payload,
                    "auditId": row["id"],
                    "createdAt": row["created_at"],
                }
        elif row["event_type"] == "intent_progress_rewrite":
            artifact_type = payload.get("artifactType")
            artifact_id = payload.get("artifactId")
            if artifact_type and artifact_id:
                progress_rewrites[(artifact_type, artifact_id)] = payload
        else:
            hypothesis_id = payload.get("hypothesisId")
            version_id = payload.get("versionId")
            if hypothesis_id and version_id:
                latest_intent_feedback[(version_id, hypothesis_id)] = {
                    **payload,
                    "eventType": row["event_type"],
                    "createdAt": row["created_at"],
                }

    attempts_by_version = {}
    translations_by_turn = {}
    opening_by_version = {
        opening["version_id"]: opening for opening in accepted_openings
    }
    turn_created_at = {turn["id"]: turn["created_at"] for turn in turns}

    stage_numbers = {version["id"]: version["stage_number"] for version in versions}
    version_rows = {
        version["id"]: load_json(version["rows_json"]) or []
        for version in versions
    }
    version_bindings = {
        version["id"]: load_json(version["entity_bindings_json"])
        if version["entity_bindings_json"]
        else None
        for version in versions
    }

    # A Stage opening is normally owned by its assessment or accepted
    # proposal.  Older records may have neither marker, so use the earliest
    # assistant turn only for Stage 1 as a conservative read-time fallback.
    opening_turn_ids_by_version = {}
    for assessment in assessments:
        opening_turn_ids_by_version.setdefault(assessment["version_id"], set()).add(
            assessment["assistant_turn_id"]
        )
    for opening in accepted_openings:
        opening_turn_ids_by_version.setdefault(opening["version_id"], set()).add(
            opening["assistant_turn_id"]
        )
    for version in versions:
        if version["stage_number"] != 1:
            continue
        if opening_turn_ids_by_version.get(version["id"]):
            continue
        version_turns = [turn for turn in turns if turn["version_id"] == version["id"]]
        first_user_index = next(
            (
                index
                for index, turn in enumerate(version_turns)
                if turn["role"] == "user"
            ),
            None,
        )
        first_assistant = next(
            (
                turn
                for index, turn in enumerate(version_turns)
                if turn["role"] == "assistant"
                and (first_user_index is None or index < first_user_index)
            ),
            None,
        )
        if first_assistant is not None:
            opening_turn_ids_by_version.setdefault(version["id"], set()).add(
                first_assistant["id"]
            )

    def public_turn_guidance(turn_guidance, body, rows, language, entity_bindings=None):
        """Recheck stored route annotations before exposing historical turns."""
        guidance = _public_guidance(turn_guidance)
        try:
            # Import lazily to avoid the repository <-> LLM client import cycle.
            from llm_client import repair_legacy_visible_guidance

            guidance = repair_legacy_visible_guidance(guidance, language)
        except (ImportError, TypeError, ValueError, KeyError):
            pass
        try:
            # Import lazily to avoid the repository <-> LLM client import cycle.
            from llm_client import _filter_coordinate_links, _recover_coordinate_links

            links = _filter_coordinate_links(
                guidance.get("coordinateLinks"),
                body,
                rows,
                entity_bindings,
            )
            links = _recover_coordinate_links(body, rows, links, entity_bindings)
        except (ImportError, TypeError, ValueError, KeyError):
            links = []
        if links:
            guidance["coordinateLinks"] = links
        else:
            guidance.pop("coordinateLinks", None)
        return guidance

    def public_text(value, language):
        """Hide prompt-only implementation labels in legacy stored text."""
        try:
            # Import lazily to avoid the repository <-> LLM client import cycle.
            from llm_client import repair_legacy_visible_text

            return repair_legacy_visible_text(value, language)
        except (ImportError, TypeError, ValueError, KeyError):
            return value

    def public_turn_content(turn):
        if turn["role"] != "assistant":
            return turn["content"]
        content = public_text(turn["content"], turn["language"])
        stage_number = stage_numbers.get(turn["version_id"])
        if (
            stage_number == 1
            and turn["id"] in opening_turn_ids_by_version.get(turn["version_id"], set())
        ):
            try:
                # This is a display-time compatibility repair; historical
                # database rows remain unchanged.
                from llm_client import _repair_stage_one_opening_display

                content = _repair_stage_one_opening_display(
                    content,
                    version_rows.get(turn["version_id"]),
                    turn["language"],
                )
            except (ImportError, TypeError, ValueError, KeyError):
                pass
        return content

    public_content_by_turn = {
        turn["id"]: public_turn_content(turn)
        for turn in turns
    }

    def public_assessment_payload(payload, language):
        if not isinstance(payload, dict):
            return payload
        try:
            # Import lazily to avoid the repository <-> LLM client import cycle.
            from llm_client import repair_legacy_visible_text
        except ImportError:
            return payload

        result = dict(payload)
        for field_name in (
            "solutionSummary",
            "difficultyOpinion",
            "satisfactionQuestion",
        ):
            if result.get(field_name) is not None:
                result[field_name] = repair_legacy_visible_text(
                    result[field_name], language
                )
        for field_name in ("features", "suggestions"):
            if isinstance(result.get(field_name), list):
                result[field_name] = [
                    repair_legacy_visible_text(item, language)
                    for item in result[field_name]
                ]
        return result

    public_guidance_by_turn = {
        turn["id"]: public_turn_guidance(
            load_json(turn["guidance_json"]),
            public_content_by_turn[turn["id"]],
            version_rows.get(turn["version_id"], []),
            turn["language"],
            version_bindings.get(turn["version_id"]),
        )
        for turn in turns
    }

    latest_intent_turn = {}
    stored_guidance_by_turn = {}
    for turn in turns:
        guidance = load_json(turn["guidance_json"]) or {}
        stored_guidance_by_turn[turn["id"]] = guidance
        hypothesis_id = guidance.get("_intentHypothesisId")
        if turn["role"] == "assistant" and hypothesis_id:
            latest_intent_turn[turn["version_id"]] = turn["id"]

    def intent_state(turn):
        guidance = stored_guidance_by_turn.get(turn["id"], {})
        hypothesis_id = guidance.get("_intentHypothesisId")
        if turn["role"] != "assistant" or not hypothesis_id:
            return None
        context = load_design_context(database, session_id, turn["version_id"])
        hypothesis = next((
            item for item in context.get("intentHypotheses", [])
            if item.get("id") == hypothesis_id
        ), None)
        if hypothesis is None:
            return None
        is_latest = latest_intent_turn.get(turn["version_id"]) == turn["id"]
        status = hypothesis.get("status", "tentative")
        review = guidance.get("_intentReviewState") or {}
        actionable = bool(
            is_latest
            and status == "tentative"
            and turn["version_id"] == session["current_version_id"]
            and session["status"] == "active"
            and not _deadline_expired(session["deadline_at"])
        )
        mode = (
            review.get("interactionMode", "full")
            if actionable
            else "resolved"
        )
        return {
            "hypothesisId": hypothesis_id,
            "status": status,
            "interactionMode": mode,
            "actionable": actionable,
            "resolvedStatement": (
                hypothesis.get("statement")
                if status in {"confirmed", "superseded"}
                else None
            ),
            "reviewExplanation": (
                review.get("reviewExplanation") if actionable else None
            ),
        }

    for turn in turns:
        state = intent_state(turn)
        if state is not None:
            public_guidance_by_turn[turn["id"]]["intentState"] = state

    def source_stage_number(source_version_id, current_version):
        return stage_numbers.get(source_version_id, current_version["stage_number"])

    latest_revision_turn_by_version = {}
    for turn in reversed(turns):
        if turn["role"] != "assistant":
            continue
        guidance = load_json(turn["guidance_json"]) or {}
        offer = guidance.get("proposalOffer") if isinstance(guidance, dict) else None
        if (
            isinstance(offer, dict)
            and str(offer.get("summary") or "").strip()
            and turn["version_id"] not in latest_revision_turn_by_version
        ):
            latest_revision_turn_by_version[turn["version_id"]] = turn["id"]

    def proposal_state(turn, guidance):
        offer = guidance.get("proposalOffer") if isinstance(guidance, dict) else None
        if (
            turn["role"] != "assistant"
            or not isinstance(offer, dict)
            or not str(offer.get("summary") or "").strip()
        ):
            return None

        try:
            binding = load_json(turn["proposal_binding_json"])
        except (TypeError, ValueError):
            binding = None
        if not isinstance(binding, dict) or not isinstance(binding.get("executionBrief"), dict):
            return {
                "status": "unbound",
                "actionable": False,
                "reason": "missing_binding",
            }

        base_version_id = binding.get("baseVersionId")
        current_version = next(
            (version for version in versions if version["id"] == base_version_id),
            None,
        )
        if current_version is None or base_version_id != session["current_version_id"]:
            return {
                "status": "stale",
                "actionable": False,
                "reason": "version_changed",
            }

        current_context = load_design_context(database, session_id, base_version_id)
        if (current_context.get("activeDisagreement") or {}).get("status") == "active":
            return {
                "status": "disagreement_active",
                "actionable": False,
                "reason": "disagreement_active",
            }

        rows = version_rows.get(base_version_id) or []
        expected_fingerprint = binding.get("mapFingerprint")
        if expected_fingerprint and map_fingerprint(rows) != expected_fingerprint:
            return {
                "status": "stale",
                "actionable": False,
                "reason": "map_changed",
            }

        expected_binding_fingerprint = binding.get("entityBindingFingerprint")
        current_bindings = version_bindings.get(base_version_id)
        actual_binding_fingerprint = (
            current_bindings or {}
        ).get("bindingFingerprint")
        if (
            not expected_binding_fingerprint
            or not actual_binding_fingerprint
            or expected_binding_fingerprint != actual_binding_fingerprint
        ):
            return {
                "status": "stale",
                "actionable": False,
                "reason": "entity_binding_changed",
            }

        brief = binding["executionBrief"]
        if (
            not isinstance(brief.get("requiredTransitions"), list)
            or not brief["requiredTransitions"]
        ):
            return {
                "status": "unbound",
                "actionable": False,
                "reason": "missing_exact_transitions",
            }
        for transition in brief.get("requiredTransitions") or []:
            row = transition.get("row")
            column = transition.get("column")
            if (
                not isinstance(row, int)
                or not isinstance(column, int)
                or not 1 <= row <= len(rows)
                or not rows
                or not 1 <= column <= len(rows[row - 1])
            ):
                return {
                    "status": "stale",
                    "actionable": False,
                    "reason": "invalid_binding",
                }
            actual = rows[row - 1][column - 1]
            if actual == transition.get("to"):
                return {
                    "status": "already_satisfied",
                    "actionable": False,
                    "reason": "already_satisfied",
                }
            if actual != transition.get("from"):
                return {
                    "status": "stale",
                    "actionable": False,
                    "reason": "precondition_failed",
                }

        binding_status = binding.get("status")
        if binding_status == "already_satisfied":
            return {
                "status": "already_satisfied",
                "actionable": False,
                "reason": "already_satisfied",
            }
        if binding_status == "stale":
            return {
                "status": "stale",
                "actionable": False,
                "reason": "proposal_consumed_or_stale",
            }
        if binding_status == "challenged":
            return {
                "status": "challenged",
                "actionable": False,
                "reason": "proposal_challenged",
            }
        return {
            "status": "active",
            "actionable": (
                binding.get("status", "active") == "active"
                and latest_revision_turn_by_version.get(turn["version_id"]) == turn["id"]
                and turn["version_id"] == session["current_version_id"]
            ),
            "reason": None,
        }

    progress_contexts = []
    parent_by_version = {
        version["id"]: version["parent_version_id"] for version in versions
    }

    def lineage_ids(version_id):
        result = set()
        cursor = version_id
        while cursor and cursor not in result:
            result.add(cursor)
            cursor = parent_by_version.get(cursor)
        return result

    def clean_evidence_text(value):
        text = " ".join(str(value or "").split())
        # Evidence explanations are historical semantics, not a second source
        # of current-map coordinate truth.
        text = re.sub(
            r"[\(（]\s*\d{1,2}\s*[,，]\s*\d{1,2}\s*[\)）]",
            "",
            text,
        )
        return " ".join(text.split())[:500]

    def evidence_projection(evidence, language):
        kind = str(evidence.get("kind") or "feedback")
        public_kind = {
            "expressed_direction": "expressed_direction",
            "proposal_accept": "confirmed_decision",
            "disagreement_resolved": "confirmed_decision",
            "manual_edit": "manual_edit",
        }.get(kind, kind)
        details = evidence.get("details") if isinstance(evidence.get("details"), dict) else {}
        labels = {
            "zh-CN": {
                "conversation": "已表达方向",
                "proposal_accept": "已确认方案",
                "proposal_reject": "方案反馈",
                "manual_edit": "手动修改观察",
                "play_completed": "试玩记录",
                "play_abandoned": "试玩记录",
                "disagreement_active": "分歧记录",
                "disagreement_resolved": "分歧解决",
                "stage_restore": "历史恢复",
                "intent_feedback": "倾向确认或修订",
            },
            "en": {
                "conversation": "Expressed direction",
                "expressed_direction": "Expressed direction",
                "confirmed_decision": "Confirmed decision",
                "proposal_accept": "Confirmed proposal",
                "proposal_reject": "Proposal feedback",
                "manual_edit": "Manual-edit observation",
                "play_completed": "Playtest record",
                "play_abandoned": "Playtest record",
                "disagreement_active": "Disagreement",
                "disagreement_resolved": "Disagreement resolved",
                "stage_restore": "Stage restore",
                "intent_feedback": "Inclination feedback",
            },
        }
        text = clean_evidence_text(evidence.get("observation"))
        if public_kind == "confirmed_decision":
            rewrite = progress_rewrites.get((
                "confirmed_decision", evidence.get("proposalId")
            )) or {}
            if rewrite.get("summaryText"):
                text = clean_evidence_text(rewrite["summaryText"])
        if language == "zh-CN" and public_kind == "expressed_direction":
            label = "\u5df2\u8868\u8fbe\u65b9\u5411"
        elif language == "zh-CN" and public_kind == "confirmed_decision":
            label = "\u5df2\u786e\u8ba4\u51b3\u7b56"
        else:
            label = labels["zh-CN" if language == "zh-CN" else "en"].get(
                public_kind,
                labels["zh-CN" if language == "zh-CN" else "en"]["intent_feedback"],
            )
        if kind == "manual_edit":
            rewrite = progress_rewrites.get(("manual_edit", evidence.get("versionId"))) or {}
            if rewrite.get("summaryText"):
                text = clean_evidence_text(rewrite["summaryText"])
            changed = len(details.get("diff") or [])
            suffix = (
                f"\u786e\u5b9a\u6027 diff \u8bb0\u5f55\u4e86 {changed} \u4e2a\u683c\u5b50\u53d8\u5316\uff0c\u4e14\u4fdd\u5b58\u65f6\u5df2\u901a\u8fc7\u9a8c\u8bc1\u3002"
                if language == "zh-CN"
                else f"The deterministic diff recorded {changed} changed tiles and validation passed on save."
            )
            text = " ".join(part for part in (text, suffix) if part)
        elif kind.startswith("play_"):
            suffix = (
                f"{details.get('moveCount', 0)} \u6b21\u79fb\u52a8\u3001{details.get('pushCount', 0)} \u6b21\u63a8\u52a8\u3001{details.get('restartCount', 0)} \u6b21\u91cd\u5f00\u3002"
                if language == "zh-CN"
                else f"{details.get('moveCount', 0)} moves, {details.get('pushCount', 0)} pushes, and {details.get('restartCount', 0)} restarts were recorded."
            )
            text = " ".join(part for part in (text, suffix) if part)
        elif kind.startswith("proposal_") and details.get("reason"):
            text = " ".join((text, clean_evidence_text(details["reason"])))
        elif kind == "intent_feedback":
            action = details.get("action")
            suffix = {
                "confirm": "\u4f60\u786e\u8ba4\u4e86\u8fd9\u6761\u503e\u5411\u3002" if language == "zh-CN" else "You confirmed this inclination.",
                "revise": "\u4f60\u4fee\u8ba2\u5e76\u786e\u8ba4\u4e86\u8fd9\u6761\u503e\u5411\u3002" if language == "zh-CN" else "You revised and confirmed this inclination.",
                "reject": "\u4f60\u5426\u5b9a\u4e86\u8fd9\u6761\u63a8\u6d4b\u3002" if language == "zh-CN" else "You rejected this hypothesis.",
            }.get(action, "")
            text = " ".join(part for part in (text, suffix) if part)
        if text:
            text = f"{label}：{text}" if language == "zh-CN" else f"{label}: {text}"
        else:
            text = label
        return {
            "evidenceId": evidence["evidenceId"],
            "stageNumber": stage_numbers.get(evidence.get("versionId"), 1),
            "kind": public_kind,
            "text": text,
            "createdAt": evidence.get("createdAt"),
            "detailedText": (
                clean_evidence_text(
                    (progress_rewrites.get(("manual_edit", evidence.get("versionId"))) or {}).get(
                        "detailedText"
                    )
                ) or None
                if kind == "manual_edit" else None
            ),
        }

    for version in versions:
        context = load_design_context(database, session_id, version["id"])
        expressed_directions = []
        for field_name, kind in (("goal", "goal"), ("constraint", "constraint")):
            for item in context.get(
                "userGoals" if field_name == "goal" else "designConstraints",
                [],
            ):
                if item.get("status") != "active" or item.get("authority") != "explicit":
                    continue
                text = str(item.get(field_name) or "").strip()
                if not text:
                    continue
                expressed_directions.append({
                    "kind": kind,
                    "text": text,
                    "sourceStageNumber": source_stage_number(
                        item.get("sourceStageId"), version
                    ),
                    "updatedAt": turn_created_at.get(item.get("sourceTurnId")),
                    "label": "explicit",
                })
        confirmed_hypotheses = [
            item for item in context.get("intentHypotheses", [])
            if item.get("status") == "confirmed"
        ]
        confirmed_by_topic = {}
        for item in confirmed_hypotheses:
            confirmed_by_topic.setdefault(item.get("topicKey"), []).append(item)
        lineage = lineage_ids(version["id"])
        design_inclinations = []
        for hypothesis in confirmed_hypotheses:
            evidence_ids = list(hypothesis.get("supportingEvidenceIds") or [])
            for evidence in intent_evidence_by_id.values():
                if evidence.get("versionId") not in lineage:
                    continue
                topic = infer_intent_topic(evidence.get("observation"))
                if (
                    topic != "other"
                    and hypothesis.get("topicKey") != "other"
                    and topic == hypothesis.get("topicKey")
                    and len(confirmed_by_topic.get(topic, [])) == 1
                ):
                    evidence_ids.append(evidence["evidenceId"])
            evidence = [
                intent_evidence_by_id[evidence_id]
                for evidence_id in dict.fromkeys(evidence_ids)
                if evidence_id in intent_evidence_by_id
                and intent_evidence_by_id[evidence_id].get("versionId") in lineage
                and intent_evidence_by_id[evidence_id].get("kind") in {
                    "expressed_direction",
                    "proposal_accept",
                    "disagreement_resolved",
                    "manual_edit",
                }
            ]
            evidence.sort(key=lambda item: (item.get("createdAt") or "", item["auditId"]))
            trail = [
                evidence_projection(item, session["language"])
                for item in evidence[-12:]
            ]
            feedback = latest_intent_feedback.get(
                (version["id"], hypothesis["id"]), {}
            )
            design_inclinations.append({
                "hypothesisId": hypothesis["id"],
                "statement": hypothesis.get("displayStatement") or hypothesis["statement"],
                "confirmedAtStageNumber": source_stage_number(
                    hypothesis.get("confirmedAtStageId")
                    or hypothesis.get("sourceStageId"),
                    version,
                ),
                "updatedAt": (
                    feedback.get("createdAt")
                    or (trail[-1]["createdAt"] if trail else None)
                    or turn_created_at.get(hypothesis.get("sourceTurnId"))
                ),
                "evidenceTrail": trail,
            })
        progress_contexts.append({
            "versionId": version["id"],
            "stageNumber": version["stage_number"],
            "parentVersionId": version["parent_version_id"],
            "expressedDirections": expressed_directions[-16:],
            "designInclinations": design_inclinations[-16:],
            "confirmedDecisions": [
                {
                    "decision": item["decision"],
                    "reason": item.get("reason") or None,
                    "sourceStageNumber": source_stage_number(
                        item.get("sourceStageId"), version
                    ),
                    "updatedAt": turn_created_at.get(item.get("sourceTurnId")),
                    "label": "confirmed",
                }
                for item in context.get("confirmedDecisions", [])
                if item.get("status") == "active"
            ],
            "unresolvedQuestions": [
                {
                    "question": item["question"],
                    "sourceStageNumber": source_stage_number(
                        item.get("sourceStageId"), version
                    ),
                    "updatedAt": turn_created_at.get(
                        item.get("updatedFromTurnId")
                        or item.get("resolvedByTurnId")
                        or item.get("sourceTurnId")
                    ),
                    "label": "open",
                }
                for item in design_level_open_questions(context)
            ],
            "questionRecords": [
                {
                    "questionId": item.get("id"),
                    "question": item.get("question"),
                    "status": (
                        "answered" if item.get("status") in {"answered", "resolved"}
                        else "ignored" if item.get("status") == "ignored"
                        else "unanswered"
                    ),
                    "askedAtStageNumber": source_stage_number(
                        item.get("sourceStageId"), version
                    ),
                    "answeredAtStageNumber": (
                        source_stage_number(item.get("answeredAtStageId"), version)
                        if item.get("answeredAtStageId") else None
                    ),
                    "ignoredAtStageNumber": (
                        source_stage_number(item.get("ignoredAtStageId"), version)
                        if item.get("ignoredAtStageId") else None
                    ),
                    "sourceTurnId": item.get("sourceTurnId"),
                    "answeredByTurnId": item.get("resolvedByTurnId"),
                    "updatedAt": item.get("ignoredAt") or turn_created_at.get(
                        item.get("resolvedByTurnId")
                        or item.get("updatedFromTurnId")
                        or item.get("sourceTurnId")
                    ),
                }
                for item in context.get("openQuestions", [])
                if item.get("sourceKind") in {"visible_output", "legacy"}
            ],
        })

    for attempt in attempts:
        attempts_by_version.setdefault(attempt["version_id"], []).append(
            _serialize_attempt(attempt)
        )

    for translation in translations:
        translation_guidance = load_json(translation["guidance_json"])
        translation_turn = next(
            (turn for turn in turns if turn["id"] == translation["turn_id"]),
            None,
        )
        if translation_turn is not None:
            translation_guidance = public_turn_guidance(
                translation_guidance,
                public_text(translation["body"], translation["language"]),
                version_rows.get(translation_turn["version_id"], []),
                translation["language"],
                version_bindings.get(translation_turn["version_id"]),
            )
            source_intent_state = public_guidance_by_turn.get(
                translation["turn_id"], {}
            ).get("intentState")
            if source_intent_state is not None:
                translation_guidance["intentState"] = source_intent_state
        translations_by_turn.setdefault(translation["turn_id"], {})[
            translation["language"]
        ] = {
            "body": public_text(translation["body"], translation["language"]),
            "guidance": translation_guidance,
            "proposalSummary": translation["proposal_summary"],
            "createdAt": translation["created_at"],
        }

    proposal_flow_status = "inactive"
    proposal_flow_question_count = 0
    latest_flow_event = database.execute(
        """
        SELECT event_type, payload_json FROM audit_events
        WHERE session_id = ?
          AND event_type IN ('proposal_discovery_started', 'proposal_discovery_progress')
          AND json_extract(payload_json, '$.stageId') = ?
        ORDER BY id DESC LIMIT 1
        """,
        (session_id, session["current_version_id"]),
    ).fetchone()
    if latest_flow_event is not None:
        flow_payload = load_json(latest_flow_event["payload_json"]) or {}
        marker_status = (
            "clarifying"
            if latest_flow_event["event_type"] == "proposal_discovery_started"
            else str(flow_payload.get("status") or "inactive")
        )
        if marker_status == "clarifying":
            proposal_flow_status = "clarifying"
            proposal_flow_question_count = max(
                0,
                min(3, int(flow_payload.get("clarificationQuestionCount") or 0)),
            )
    if proposal_flow_status == "inactive":
        # Compatibility for sessions created before proposal-flow audit events.
        latest_marker = None
        for turn in turns:
            if turn["version_id"] != session["current_version_id"] or turn["role"] != "assistant":
                continue
            guidance = load_json(turn["guidance_json"]) or {}
            marker = guidance.get("proposalDiscovery")
            if isinstance(marker, dict):
                latest_marker = marker
        if isinstance(latest_marker, dict) and latest_marker.get("status") == "clarifying":
            proposal_flow_status = "clarifying"
            proposal_flow_question_count = max(
                0,
                min(3, int(latest_marker.get("clarificationQuestionCount") or 0)),
            )

    return {
        "sessionId": session["id"],
        "status": session["status"],
        "language": session["language"],
        "languageLocked": session["language_locked_at"] is not None,
        "demoMode": bool(session["demo_mode"]),
        "initialDraftMethod": session["initial_draft_method"],
        "matchId": session["match_id"],
        "playerNumber": session["player_number"],
        "currentVersionId": session["current_version_id"],
        "finalVersionId": session["final_version_id"],
        "createdAt": session["created_at"],
        "updatedAt": session["updated_at"],
        "deadlineStartedAt": session["deadline_started_at"],
        "deadlineAt": session["deadline_at"],
        "deadlineExpired": _deadline_expired(session["deadline_at"]),
        "remainingSeconds": _remaining_deadline_seconds(session["deadline_at"]),
        "proposalFlowState": {
            "active": proposal_flow_status == "clarifying",
            "status": proposal_flow_status,
            "clarificationQuestionCount": proposal_flow_question_count,
        },
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
                "executionOutcome": execution_outcomes.get(version["id"]),
                "openingTurnId": (
                    opening_by_version[version["id"]]["assistant_turn_id"]
                    if version["id"] in opening_by_version
                    else None
                ),
                "openingProposalId": (
                    opening_by_version[version["id"]]["proposal_id"]
                    if version["id"] in opening_by_version
                    else None
                ),
            }
            for version in versions
        ],
        "progressContexts": progress_contexts,
        "challengeReviewRecords": list(challenge_reviews.values()),
        "turns": [
            {
                "turnId": turn["id"],
                "sequence": turn["sequence_number"],
                "role": turn["role"],
                "content": public_content_by_turn[turn["id"]],
                "language": turn["language"],
                "versionId": turn["version_id"],
                "requestId": turn["request_id"],
                "requestProposal": (
                    turn["role"] == "user"
                    and turn["request_id"] in proposal_request_keys
                ),
                "guidance": public_guidance_by_turn.get(turn["id"], {}),
                "proposalState": proposal_state(
                    turn,
                    load_json(turn["guidance_json"]) or {},
                ),
                "translations": translations_by_turn.get(turn["id"], {}),
                "createdAt": turn["created_at"],
            }
            for turn in turns
        ],
        "assessments": [
            {
                "assessmentId": assessment["id"],
                "versionId": assessment["version_id"],
                "assistantTurnId": assessment["assistant_turn_id"],
                "payload": public_assessment_payload(
                    load_json(assessment["payload_json"]),
                    next(
                        (
                            turn["language"]
                            for turn in turns
                            if turn["id"] == assessment["assistant_turn_id"]
                        ),
                        session["language"],
                    ),
                ),
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


def map_fingerprint(rows):
    """Return a stable fingerprint for the complete saved Stage grid."""
    import hashlib

    canonical = json.dumps(
        list(rows or []),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
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
    columns = {
        row[1]
        for row in database.execute(f"PRAGMA table_info({table_name})").fetchall()
    }

    if column_name not in columns:
        database.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
        )


def _deadline_expired(value):
    return value is not None and _parse_time(value) <= datetime.now(timezone.utc)


def _remaining_deadline_seconds(value):
    if value is None:
        return None
    return max(0, int((_parse_time(value) - datetime.now(timezone.utc)).total_seconds()))


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
