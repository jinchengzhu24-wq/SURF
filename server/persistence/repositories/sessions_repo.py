"""Repository for design sessions, level versions, and batch read models."""

from __future__ import annotations

from typing import Any

from ..database import SQLiteDatabase, dump_json
from ..models import DesignSessionRecord, LevelVersionRecord, SessionReadModel
from ._attempt_draft_ops import _AttemptDraftMixin
from ._mappers import (
    build_update_clause,
    row_to_challenge,
    row_to_draft_regeneration,
    row_to_play_attempt,
    row_to_proposal,
    row_to_session,
    row_to_turn,
    row_to_version,
)

_SESSION_JSON_FIELDS = {
    "final_intention": "final_intention_json",
    "draft_rows": "draft_rows_json",
    "draft_validation": "draft_validation_json",
}
_SESSION_SCALAR_FIELDS = {
    "match_id": "match_id",
    "player_slot": "player_slot",
    "mode": "mode",
    "language": "language",
    "language_locked": "language_locked",
    "language_locked_at": "language_locked_at",
    "status": "status",
    "secret_hash": "secret_hash",
    "bootstrap_used_at": "bootstrap_used_at",
    "current_version_id": "current_version_id",
    "deadline_started_at": "deadline_started_at",
    "deadline_at": "deadline_at",
    "finalized_version_id": "finalized_version_id",
    "finalized_at": "finalized_at",
    "completed_at": "completed_at",
    "draft_generation": "draft_generation",
    "updated_at": "updated_at",
    "expires_at": "expires_at",
}


class SessionsRepository(_AttemptDraftMixin):
    """CRUD and batch-loaded SessionReadModel queries for co-creation sessions."""

    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    def create_session(
        self,
        *,
        session_id: str,
        mode: str,
        language: str,
        access_token_hash: str,
        integration_token_hash: str,
        bootstrap_token_hash: str,
        created_at: str,
        updated_at: str | None = None,
        match_id: str | None = None,
        player_slot: int | None = None,
        secret_hash: str | None = None,
        creation_key: str | None = None,
        draft_rows: list[str] | None = None,
        draft_validation: dict[str, Any] | None = None,
        is_demo: bool = False,
        expires_at: str | None = None,
    ) -> DesignSessionRecord:
        ts = updated_at or created_at
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO design_sessions("
                "session_id, match_id, player_slot, mode, language, language_locked, status, "
                "secret_hash, access_token_hash, integration_token_hash, bootstrap_token_hash, "
                "creation_key, draft_rows_json, draft_validation_json, draft_generation, "
                "is_demo, created_at, updated_at, expires_at"
                ") VALUES (?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (
                    session_id,
                    match_id,
                    player_slot,
                    mode,
                    language,
                    secret_hash,
                    access_token_hash,
                    integration_token_hash,
                    bootstrap_token_hash,
                    creation_key,
                    dump_json(draft_rows) if draft_rows is not None else None,
                    dump_json(draft_validation) if draft_validation is not None else None,
                    1 if is_demo else 0,
                    created_at,
                    ts,
                    expires_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert row is not None
            return row_to_session(row)

    def get_session(self, session_id: str) -> DesignSessionRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return row_to_session(row) if row is not None else None

    def get_session_by_creation_key(self, creation_key: str) -> DesignSessionRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM design_sessions WHERE creation_key = ?", (creation_key,)
            ).fetchone()
            return row_to_session(row) if row is not None else None

    def update_session(self, session_id: str, **fields: Any) -> DesignSessionRecord | None:
        if not fields:
            return self.get_session(session_id)
        mapped: dict[str, Any] = {}
        allowed = {**_SESSION_SCALAR_FIELDS, **_SESSION_JSON_FIELDS}
        for key, value in fields.items():
            if key in _SESSION_JSON_FIELDS:
                mapped[key] = dump_json(value) if value is not None else None
            elif key == "language_locked":
                mapped[key] = 1 if value else 0
            else:
                mapped[key] = value
        clauses, params = build_update_clause(mapped, allowed)
        params.append(session_id)
        with self._db.write_tx() as conn:
            cur = conn.execute(
                f"UPDATE design_sessions SET {', '.join(clauses)} WHERE session_id = ?",
                params,
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return row_to_session(row) if row is not None else None

    def delete_expired_demo_sessions(
        self,
        before_iso: str,
        *,
        null_expiry_cutoff_iso: str | None = None,
    ) -> int:
        """Delete only expired demo sessions (TTL-scoped); never wipe active demos."""
        null_cutoff = (
            null_expiry_cutoff_iso if null_expiry_cutoff_iso is not None else before_iso
        )
        with self._db.write_tx() as conn:
            rows = conn.execute(
                "SELECT session_id FROM design_sessions "
                "WHERE is_demo = 1 AND ("
                "(expires_at IS NOT NULL AND expires_at <= ?) "
                "OR (expires_at IS NULL AND created_at <= ?)"
                ")",
                (before_iso, null_cutoff),
            ).fetchall()
            session_ids = [row["session_id"] for row in rows]
            if not session_ids:
                return 0
            placeholders = ", ".join("?" for _ in session_ids)
            for table in (
                "conversation_turns",
                "change_proposals",
                "revision_challenges",
                "play_attempts",
                "draft_regenerations",
                "binary_discovery_topics",
                "component_recovery_tasks",
                "audit_events",
                "idempotency_keys",
                "level_versions",
                "design_sessions",
            ):
                conn.execute(
                    f"DELETE FROM {table} WHERE session_id IN ({placeholders})",
                    session_ids,
                )
            return len(session_ids)

    def insert_version(
        self,
        *,
        version_id: str,
        session_id: str,
        version_number: int,
        source: str,
        summary: str,
        rows: list[str],
        solver: dict[str, Any],
        created_at: str,
        diff: list[dict[str, Any]] | None = None,
        design_context: dict[str, Any] | None = None,
        entity_bindings: dict[str, Any] | None = None,
        assessment: dict[str, Any] | None = None,
        parent_version_id: str | None = None,
        idempotency_key: str | None = None,
        set_current: bool = True,
    ) -> LevelVersionRecord:
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO level_versions("
                "version_id, session_id, version_number, source, summary, rows_json, diff_json, "
                "design_context_json, solver_json, entity_bindings_json, assessment_json, "
                "parent_version_id, idempotency_key, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    session_id,
                    version_number,
                    source,
                    summary,
                    dump_json(list(rows)),
                    dump_json(diff or []),
                    dump_json(design_context) if design_context is not None else None,
                    dump_json(solver),
                    dump_json(entity_bindings) if entity_bindings is not None else None,
                    dump_json(assessment) if assessment is not None else None,
                    parent_version_id,
                    idempotency_key,
                    created_at,
                ),
            )
            if set_current:
                conn.execute(
                    "UPDATE design_sessions SET current_version_id = ?, updated_at = ? "
                    "WHERE session_id = ?",
                    (version_id, created_at, session_id),
                )
            row = conn.execute(
                "SELECT * FROM level_versions WHERE version_id = ?", (version_id,)
            ).fetchone()
            assert row is not None
            return row_to_version(row)

    def get_version(self, session_id: str, version_id: str) -> LevelVersionRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM level_versions WHERE session_id = ? AND version_id = ?",
                (session_id, version_id),
            ).fetchone()
            return row_to_version(row) if row is not None else None

    def get_version_by_idempotency(
        self, session_id: str, idempotency_key: str
    ) -> LevelVersionRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM level_versions WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
            return row_to_version(row) if row is not None else None

    def list_versions(self, session_id: str) -> list[LevelVersionRecord]:
        with self._db.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM level_versions WHERE session_id = ? ORDER BY version_number ASC",
                (session_id,),
            ).fetchall()
            return [row_to_version(row) for row in rows]

    def update_version_fields(
        self,
        session_id: str,
        version_id: str,
        *,
        design_context: dict[str, Any] | None = None,
        entity_bindings: dict[str, Any] | None = None,
        assessment: dict[str, Any] | None = None,
    ) -> bool:
        updates: list[str] = []
        params: list[Any] = []
        if design_context is not None:
            updates.append("design_context_json = ?")
            params.append(dump_json(design_context))
        if entity_bindings is not None:
            updates.append("entity_bindings_json = ?")
            params.append(dump_json(entity_bindings))
        if assessment is not None:
            updates.append("assessment_json = ?")
            params.append(dump_json(assessment))
        if not updates:
            return False
        params.extend([session_id, version_id])
        with self._db.write_tx() as conn:
            cur = conn.execute(
                f"UPDATE level_versions SET {', '.join(updates)} "
                "WHERE session_id = ? AND version_id = ?",
                params,
            )
            return cur.rowcount > 0

    def load_session_read_model(self, session_id: str) -> SessionReadModel | None:
        """Batch-load a session and all related records in one snapshot transaction."""
        with self._db.read_conn() as conn:
            session_row = conn.execute(
                "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session_row is None:
                return None
            version_rows = conn.execute(
                "SELECT * FROM level_versions WHERE session_id = ? ORDER BY version_number ASC",
                (session_id,),
            ).fetchall()
            turn_rows = conn.execute(
                "SELECT * FROM conversation_turns "
                "WHERE session_id = ? ORDER BY sequence_number ASC",
                (session_id,),
            ).fetchall()
            proposal_rows = conn.execute(
                "SELECT * FROM change_proposals WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            challenge_rows = conn.execute(
                "SELECT * FROM revision_challenges WHERE session_id = ? ORDER BY updated_at ASC",
                (session_id,),
            ).fetchall()
            attempt_rows = conn.execute(
                "SELECT * FROM play_attempts WHERE session_id = ? ORDER BY issued_at DESC",
                (session_id,),
            ).fetchall()
            regen_rows = conn.execute(
                "SELECT * FROM draft_regenerations WHERE session_id = ? "
                "ORDER BY requested_at DESC",
                (session_id,),
            ).fetchall()
            return SessionReadModel(
                session=row_to_session(session_row),
                versions=[row_to_version(r) for r in version_rows],
                turns=[row_to_turn(r) for r in turn_rows],
                proposals=[row_to_proposal(r) for r in proposal_rows],
                challenges=[row_to_challenge(r) for r in challenge_rows],
                play_attempts=[row_to_play_attempt(r) for r in attempt_rows],
                draft_regenerations=[row_to_draft_regeneration(r) for r in regen_rows],
            )
