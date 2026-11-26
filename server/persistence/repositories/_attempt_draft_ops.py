"""Play attempt and draft regeneration CRUD mixin for SessionsRepository."""

from __future__ import annotations

from typing import Any

from ..database import SQLiteDatabase, dump_json
from ..models import DraftRegenerationRecord, PlayAttemptRecord
from ._mappers import build_update_clause, row_to_draft_regeneration, row_to_play_attempt

_PLAY_ATTEMPT_FIELDS = {
    "status": "status",
    "ticket_used_at": "ticket_used_at",
    "attempt_token_hash": "attempt_token_hash",
    "loaded_at": "loaded_at",
    "first_move_at": "first_move_at",
    "finished_at": "finished_at",
    "duration_seconds": "duration_seconds",
    "move_count": "move_count",
    "push_count": "push_count",
    "restart_count": "restart_count",
    "minimum_moves": "minimum_moves",
    "minimum_pushes": "minimum_pushes",
}

_DRAFT_REGEN_SCALAR_FIELDS = {
    "status": "status",
    "generation": "generation",
    "failure_code": "failure_code",
    "updated_at": "updated_at",
}
_DRAFT_REGEN_JSON_FIELDS = {
    "rows": "rows_json",
    "validation": "validation_json",
}


class _AttemptDraftMixin:
    _db: SQLiteDatabase

    def create_play_attempt(
        self,
        *,
        attempt_id: str,
        session_id: str,
        version_id: str,
        mode: str,
        language: str,
        ticket_hash: str,
        ticket_expires_at: str,
        issued_at: str,
        minimum_moves: int,
        minimum_pushes: int,
        idempotency_key: str,
    ) -> PlayAttemptRecord:
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO play_attempts("
                "attempt_id, session_id, version_id, mode, language, status, ticket_hash, "
                "ticket_expires_at, issued_at, minimum_moves, minimum_pushes, idempotency_key"
                ") VALUES (?, ?, ?, ?, ?, 'issued', ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    session_id,
                    version_id,
                    mode,
                    language,
                    ticket_hash,
                    ticket_expires_at,
                    issued_at,
                    minimum_moves,
                    minimum_pushes,
                    idempotency_key,
                ),
            )
            row = conn.execute(
                "SELECT * FROM play_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            assert row is not None
            return row_to_play_attempt(row)

    def get_play_attempt(self, attempt_id: str) -> PlayAttemptRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM play_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            return row_to_play_attempt(row) if row is not None else None

    def get_play_attempt_by_idempotency(
        self, session_id: str, idempotency_key: str
    ) -> PlayAttemptRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM play_attempts WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
            return row_to_play_attempt(row) if row is not None else None

    def list_play_attempts(
        self, session_id: str, version_id: str | None = None
    ) -> list[PlayAttemptRecord]:
        with self._db.read_conn() as conn:
            if version_id is None:
                rows = conn.execute(
                    "SELECT * FROM play_attempts WHERE session_id = ? ORDER BY issued_at DESC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM play_attempts WHERE session_id = ? AND version_id = ? "
                    "ORDER BY issued_at DESC",
                    (session_id, version_id),
                ).fetchall()
            return [row_to_play_attempt(row) for row in rows]

    def update_play_attempt(self, attempt_id: str, **fields: Any) -> PlayAttemptRecord | None:
        if not fields:
            return self.get_play_attempt(attempt_id)
        clauses, params = build_update_clause(fields, _PLAY_ATTEMPT_FIELDS)
        params.append(attempt_id)
        with self._db.write_tx() as conn:
            cur = conn.execute(
                f"UPDATE play_attempts SET {', '.join(clauses)} WHERE attempt_id = ?",
                params,
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM play_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            return row_to_play_attempt(row) if row is not None else None

    def expire_interrupted_attempts(
        self, session_id: str, *, cutoff_iso: str, now_iso: str
    ) -> int:
        with self._db.write_tx() as conn:
            cur = conn.execute(
                "UPDATE play_attempts SET status = 'interrupted', finished_at = ? "
                "WHERE session_id = ? AND status IN ('issued', 'loaded', 'started') "
                "AND COALESCE(first_move_at, loaded_at, issued_at) <= ?",
                (now_iso, session_id, cutoff_iso),
            )
            return cur.rowcount

    def create_draft_regeneration(
        self,
        *,
        request_id: str,
        session_id: str,
        idempotency_key: str,
        status: str,
        generation: int,
        requested_at: str,
        updated_at: str | None = None,
        failure_code: str | None = None,
        rows: list[str] | None = None,
        validation: dict[str, Any] | None = None,
    ) -> DraftRegenerationRecord:
        ts = updated_at or requested_at
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO draft_regenerations("
                "request_id, session_id, idempotency_key, status, generation, "
                "failure_code, rows_json, validation_json, requested_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    session_id,
                    idempotency_key,
                    status,
                    generation,
                    failure_code,
                    dump_json(rows) if rows is not None else None,
                    dump_json(validation) if validation is not None else None,
                    requested_at,
                    ts,
                ),
            )
            row = conn.execute(
                "SELECT * FROM draft_regenerations WHERE request_id = ?", (request_id,)
            ).fetchone()
            assert row is not None
            return row_to_draft_regeneration(row)

    def get_draft_regeneration(self, request_id: str) -> DraftRegenerationRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM draft_regenerations WHERE request_id = ?", (request_id,)
            ).fetchone()
            return row_to_draft_regeneration(row) if row is not None else None

    def get_latest_draft_regeneration(
        self, session_id: str
    ) -> DraftRegenerationRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM draft_regenerations WHERE session_id = ? "
                "ORDER BY requested_at DESC, rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return row_to_draft_regeneration(row) if row is not None else None

    def list_active_draft_regenerations(self) -> list[DraftRegenerationRecord]:
        with self._db.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM draft_regenerations WHERE status IN ('pending', 'claimed') "
                "ORDER BY requested_at ASC"
            ).fetchall()
            return [row_to_draft_regeneration(row) for row in rows]

    def update_draft_regeneration(
        self, request_id: str, **fields: Any
    ) -> DraftRegenerationRecord | None:
        if not fields:
            return self.get_draft_regeneration(request_id)
        mapped: dict[str, Any] = {}
        allowed = {**_DRAFT_REGEN_SCALAR_FIELDS, **_DRAFT_REGEN_JSON_FIELDS}
        for key, value in fields.items():
            if key in _DRAFT_REGEN_JSON_FIELDS:
                mapped[key] = dump_json(value) if value is not None else None
            else:
                mapped[key] = value
        clauses, params = build_update_clause(mapped, allowed)
        params.append(request_id)
        with self._db.write_tx() as conn:
            cur = conn.execute(
                f"UPDATE draft_regenerations SET {', '.join(clauses)} WHERE request_id = ?",
                params,
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM draft_regenerations WHERE request_id = ?", (request_id,)
            ).fetchone()
            return row_to_draft_regeneration(row) if row is not None else None
