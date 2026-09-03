import json
import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from design_context import (
    add_confirmed_decision,
    add_rejected_decision,
    design_level_open_questions,
    empty_design_context,
    merge_chat_update,
    normalize_design_context,
    set_active_disagreement,
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

CREATE INDEX IF NOT EXISTS idx_versions_session
    ON level_versions(session_id, stage_number);
CREATE INDEX IF NOT EXISTS idx_turns_session
    ON conversation_turns(session_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_turn_translations_session
    ON turn_translations(session_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_attempts_version
    ON play_attempts(session_id, version_id, issued_at);
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
        _ensure_column(database, "design_sessions", "demo_mode", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(database, "design_sessions", "deadline_started_at", "TEXT")
        _ensure_column(database, "design_sessions", "deadline_at", "TEXT")
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA foreign_keys=ON")
        database.commit()
        backfill_design_contexts(database)
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


def _has_valid_design_context(raw):
    if not raw:
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("schemaVersion") == 1


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
                        dump_json({"versionId": version["id"], "schemaVersion": 1}),
                        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    ),
                )
            changed += 1
    return changed


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

    def public_turn_guidance(turn_guidance, body, rows, language):
        """Recheck stored route annotations before exposing historical turns."""
        guidance = _public_guidance(turn_guidance)
        try:
            # Import lazily to avoid the repository <-> LLM client import cycle.
            from llm_client import _sanitize_visible_guidance

            guidance = _sanitize_visible_guidance(guidance, language)
        except (ImportError, TypeError, ValueError, KeyError):
            pass
        if not guidance.get("coordinateLinks"):
            return guidance
        try:
            # Import lazily to avoid the repository <-> LLM client import cycle.
            from llm_client import _filter_coordinate_links

            links = _filter_coordinate_links(
                guidance.get("coordinateLinks"),
                body,
                rows,
            )
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
            from llm_client import _sanitize_visible_model_text

            return _sanitize_visible_model_text(value, language)
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
                from llm_client import _ensure_stage_one_orientation

                content = _ensure_stage_one_orientation(
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
            from llm_client import _sanitize_visible_model_text
        except ImportError:
            return payload

        result = dict(payload)
        for field_name in (
            "solutionSummary",
            "difficultyOpinion",
            "satisfactionQuestion",
        ):
            if result.get(field_name) is not None:
                result[field_name] = _sanitize_visible_model_text(
                    result[field_name], language
                )
        for field_name in ("features", "suggestions"):
            if isinstance(result.get(field_name), list):
                result[field_name] = [
                    _sanitize_visible_model_text(item, language)
                    for item in result[field_name]
                ]
        return result

    public_guidance_by_turn = {
        turn["id"]: public_turn_guidance(
            load_json(turn["guidance_json"]),
            public_content_by_turn[turn["id"]],
            version_rows.get(turn["version_id"], []),
            turn["language"],
        )
        for turn in turns
    }

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

        rows = version_rows.get(base_version_id) or []
        expected_fingerprint = binding.get("mapFingerprint")
        if expected_fingerprint and map_fingerprint(rows) != expected_fingerprint:
            return {
                "status": "stale",
                "actionable": False,
                "reason": "map_changed",
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
    for version in versions:
        context = load_design_context(database, session_id, version["id"])
        progress_contexts.append({
            "versionId": version["id"],
            "stageNumber": version["stage_number"],
            "parentVersionId": version["parent_version_id"],
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
            )
        translations_by_turn.setdefault(translation["turn_id"], {})[
            translation["language"]
        ] = {
            "body": public_text(translation["body"], translation["language"]),
            "guidance": translation_guidance,
            "proposalSummary": translation["proposal_summary"],
            "createdAt": translation["created_at"],
        }

    return {
        "sessionId": session["id"],
        "status": session["status"],
        "language": session["language"],
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
        "turns": [
            {
                "turnId": turn["id"],
                "sequence": turn["sequence_number"],
                "role": turn["role"],
                "content": public_content_by_turn[turn["id"]],
                "language": turn["language"],
                "versionId": turn["version_id"],
                "requestId": turn["request_id"],
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
