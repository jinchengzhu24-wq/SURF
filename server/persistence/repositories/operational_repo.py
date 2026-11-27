"""Repository for DB-backed idempotency keys, binary discovery topics, and recovery tasks."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..database import SQLiteDatabase, dump_json
from ..models import (
    BinaryDiscoveryTopicRecord,
    ComponentRecoveryTaskRecord,
    IdempotencyClaimResult,
    IdempotencyRecord,
)
from ._mappers import row_to_idempotency, row_to_recovery_task, row_to_topic


class OperationalRepository:
    """Indexed operational state promoted out of append-only audit logs."""

    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    def claim_key(
        self,
        session_id: str,
        scope: str,
        idempotency_key: str,
        *,
        request_hash: str,
        created_at: str,
        stale_before_iso: str | None = None,
        allow_failed_retry: bool = True,
    ) -> IdempotencyClaimResult:
        """Atomically claim an idempotency key or return existing conflict/replay/in-progress."""
        with self._db.write_tx() as conn:
            try:
                conn.execute(
                    "INSERT INTO idempotency_keys("
                    "session_id, scope, idempotency_key, request_hash, "
                    "status, response_json, created_at"
                    ") VALUES (?, ?, ?, ?, 'in_progress', NULL, ?)",
                    (session_id, scope, idempotency_key, request_hash, created_at),
                )
                row = conn.execute(
                    "SELECT * FROM idempotency_keys "
                    "WHERE session_id = ? AND scope = ? AND idempotency_key = ?",
                    (session_id, scope, idempotency_key),
                ).fetchone()
                assert row is not None
                return IdempotencyClaimResult(outcome="acquired", record=row_to_idempotency(row))
            except sqlite3.IntegrityError:
                pass

            existing = conn.execute(
                "SELECT * FROM idempotency_keys "
                "WHERE session_id = ? AND scope = ? AND idempotency_key = ?",
                (session_id, scope, idempotency_key),
            ).fetchone()
            assert existing is not None
            if existing["request_hash"] != request_hash:
                return IdempotencyClaimResult(
                    outcome="conflict", record=row_to_idempotency(existing)
                )
            if existing["status"] == "completed":
                return IdempotencyClaimResult(
                    outcome="completed", record=row_to_idempotency(existing)
                )

            takeover = (
                (existing["status"] == "failed" and allow_failed_retry)
                or (
                    existing["status"] == "in_progress"
                    and stale_before_iso is not None
                    and existing["created_at"] <= stale_before_iso
                )
            )
            if takeover:
                conn.execute(
                    "UPDATE idempotency_keys SET status = 'in_progress', created_at = ? "
                    "WHERE session_id = ? AND scope = ? AND idempotency_key = ?",
                    (created_at, session_id, scope, idempotency_key),
                )
                refreshed = conn.execute(
                    "SELECT * FROM idempotency_keys "
                    "WHERE session_id = ? AND scope = ? AND idempotency_key = ?",
                    (session_id, scope, idempotency_key),
                ).fetchone()
                assert refreshed is not None
                return IdempotencyClaimResult(
                    outcome="acquired", record=row_to_idempotency(refreshed)
                )

            return IdempotencyClaimResult(
                outcome="in_progress", record=row_to_idempotency(existing)
            )

    @staticmethod
    def complete_key_tx(
        conn: sqlite3.Connection,
        session_id: str,
        scope: str,
        idempotency_key: str,
        *,
        response: dict[str, Any] | None = None,
        status: str = "completed",
    ) -> IdempotencyRecord | None:
        """Update an idempotency key inside an existing write_tx connection."""
        cur = conn.execute(
            "UPDATE idempotency_keys SET status = ?, response_json = ? "
            "WHERE session_id = ? AND scope = ? AND idempotency_key = ?",
            (
                status,
                dump_json(response) if response is not None else None,
                session_id,
                scope,
                idempotency_key,
            ),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM idempotency_keys "
            "WHERE session_id = ? AND scope = ? AND idempotency_key = ?",
            (session_id, scope, idempotency_key),
        ).fetchone()
        return row_to_idempotency(row) if row is not None else None

    def complete_key(
        self,
        session_id: str,
        scope: str,
        idempotency_key: str,
        *,
        response: dict[str, Any] | None = None,
        status: str = "completed",
    ) -> IdempotencyRecord | None:
        with self._db.write_tx() as conn:
            return self.complete_key_tx(
                conn,
                session_id,
                scope,
                idempotency_key,
                response=response,
                status=status,
            )

    def get_key(
        self, session_id: str, scope: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_keys "
                "WHERE session_id = ? AND scope = ? AND idempotency_key = ?",
                (session_id, scope, idempotency_key),
            ).fetchone()
            return row_to_idempotency(row) if row is not None else None

    def delete_key(self, session_id: str, scope: str, idempotency_key: str) -> bool:
        with self._db.write_tx() as conn:
            cur = conn.execute(
                "DELETE FROM idempotency_keys "
                "WHERE session_id = ? AND scope = ? AND idempotency_key = ?",
                (session_id, scope, idempotency_key),
            )
            return cur.rowcount > 0

    def upsert_topic(
        self,
        session_id: str,
        topic_id: str,
        state: dict[str, Any],
        updated_at: str,
    ) -> BinaryDiscoveryTopicRecord:
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO binary_discovery_topics(session_id, topic_id, state_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id, topic_id) DO UPDATE SET "
                "state_json = excluded.state_json, updated_at = excluded.updated_at",
                (session_id, topic_id, dump_json(state), updated_at),
            )
            row = conn.execute(
                "SELECT * FROM binary_discovery_topics WHERE session_id = ? AND topic_id = ?",
                (session_id, topic_id),
            ).fetchone()
            assert row is not None
            return row_to_topic(row)

    def get_topic(
        self, session_id: str, topic_id: str
    ) -> BinaryDiscoveryTopicRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM binary_discovery_topics WHERE session_id = ? AND topic_id = ?",
                (session_id, topic_id),
            ).fetchone()
            return row_to_topic(row) if row is not None else None

    def list_topics(self, session_id: str) -> list[BinaryDiscoveryTopicRecord]:
        with self._db.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM binary_discovery_topics WHERE session_id = ? "
                "ORDER BY updated_at ASC, rowid ASC",
                (session_id,),
            ).fetchall()
            return [row_to_topic(row) for row in rows]

    def get_latest_topic(self, session_id: str) -> BinaryDiscoveryTopicRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM binary_discovery_topics WHERE session_id = ? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return row_to_topic(row) if row is not None else None

    def upsert_task(
        self,
        recovery_id: str,
        session_id: str,
        target_kind: str,
        target_id: str,
        state: dict[str, Any],
        updated_at: str,
    ) -> ComponentRecoveryTaskRecord:
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO component_recovery_tasks("
                "recovery_id, session_id, target_kind, target_id, state_json, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(recovery_id) DO UPDATE SET "
                "target_kind = excluded.target_kind, "
                "target_id = excluded.target_id, "
                "state_json = excluded.state_json, "
                "updated_at = excluded.updated_at",
                (recovery_id, session_id, target_kind, target_id, dump_json(state), updated_at),
            )
            row = conn.execute(
                "SELECT * FROM component_recovery_tasks WHERE recovery_id = ?", (recovery_id,)
            ).fetchone()
            assert row is not None
            return row_to_recovery_task(row)

    def get_task(
        self, session_id: str, recovery_id: str
    ) -> ComponentRecoveryTaskRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM component_recovery_tasks "
                "WHERE session_id = ? AND recovery_id = ?",
                (session_id, recovery_id),
            ).fetchone()
            return row_to_recovery_task(row) if row is not None else None

    def list_tasks(
        self, session_id: str, *, target_kind: str | None = None
    ) -> list[ComponentRecoveryTaskRecord]:
        with self._db.read_conn() as conn:
            if target_kind is None:
                rows = conn.execute(
                    "SELECT * FROM component_recovery_tasks WHERE session_id = ? "
                    "ORDER BY updated_at ASC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM component_recovery_tasks "
                    "WHERE session_id = ? AND target_kind = ? ORDER BY updated_at ASC",
                    (session_id, target_kind),
                ).fetchall()
            return [row_to_recovery_task(row) for row in rows]
