"""Repository for turns, change proposals, revision challenges, and audit events."""

from __future__ import annotations

from typing import Any

from ..database import SQLiteDatabase, dump_json, load_json
from ..models import (
    AuditEventRecord,
    ChangeProposalRecord,
    ConversationTurnRecord,
    RevisionChallengeRecord,
)
from ._mappers import (
    build_update_clause,
    row_to_audit_event,
    row_to_challenge,
    row_to_proposal,
    row_to_turn,
)

_CHALLENGE_SCALAR_FIELDS = {
    "status": "status",
    "current_reason_turn_id": "current_reason_turn_id",
    "updated_at": "updated_at",
}
_CHALLENGE_JSON_FIELDS = {"reviews": "reviews_json"}


class DialogueRepository:
    """Persistence for co-creation turns, proposals, challenges, and append-only audit logs."""

    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    def insert_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        role: str,
        content: str,
        version_id: str,
        created_at: str,
        language: str = "zh-CN",
        request_id: str | None = None,
        guidance: dict[str, Any] | None = None,
        translations: dict[str, Any] | None = None,
        proposal_binding: dict[str, Any] | None = None,
        model: str | None = None,
        attempts_used: int | None = None,
        latency_ms: int | None = None,
    ) -> ConversationTurnRecord:
        with self._db.write_tx() as conn:
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq "
                "FROM conversation_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(seq_row["next_seq"]) if seq_row is not None else 1
            conn.execute(
                "INSERT INTO conversation_turns("
                "turn_id, session_id, sequence_number, request_id, role, content, language, "
                "guidance_json, translations_json, proposal_binding_json, model, attempts_used, "
                "latency_ms, version_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id, session_id, seq, request_id, role, content, language,
                    dump_json(guidance) if guidance is not None else None,
                    dump_json(translations) if translations is not None else None,
                    dump_json(proposal_binding) if proposal_binding is not None else None,
                    model, attempts_used, latency_ms, version_id, created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM conversation_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            assert row is not None
            return row_to_turn(row)

    def get_turn(self, session_id: str, turn_id: str) -> ConversationTurnRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            return row_to_turn(row) if row is not None else None

    def list_turns(
        self, session_id: str, *, version_id: str | None = None, request_id: str | None = None
    ) -> list[ConversationTurnRecord]:
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if version_id is not None:
            clauses.append("version_id = ?")
            params.append(version_id)
        if request_id is not None:
            clauses.append("request_id = ?")
            params.append(request_id)
        with self._db.read_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM conversation_turns WHERE {' AND '.join(clauses)} "
                "ORDER BY sequence_number ASC",
                params,
            ).fetchall()
            return [row_to_turn(row) for row in rows]

    def find_turns_by_request(
        self, session_id: str, request_id: str, *, role: str | None = None
    ) -> list[ConversationTurnRecord]:
        with self._db.read_conn() as conn:
            if role is None:
                rows = conn.execute(
                    "SELECT * FROM conversation_turns "
                    "WHERE session_id = ? AND request_id = ? ORDER BY sequence_number ASC",
                    (session_id, request_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conversation_turns "
                    "WHERE session_id = ? AND request_id = ? AND role = ? "
                    "ORDER BY sequence_number ASC",
                    (session_id, request_id, role),
                ).fetchall()
            return [row_to_turn(row) for row in rows]

    def upsert_turn_translation(
        self, session_id: str, turn_id: str, language: str, translation_payload: dict[str, Any]
    ) -> ConversationTurnRecord | None:
        with self._db.write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if row is None:
                return None
            translations = load_json(row["translations_json"], {})
            if not isinstance(translations, dict):
                translations = {}
            translations[language] = dict(translation_payload)
            conn.execute(
                "UPDATE conversation_turns SET translations_json = ? "
                "WHERE session_id = ? AND turn_id = ?",
                (dump_json(translations), session_id, turn_id),
            )
            updated = conn.execute(
                "SELECT * FROM conversation_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            return row_to_turn(updated) if updated is not None else None

    def insert_proposal(
        self,
        *,
        proposal_id: str,
        session_id: str,
        assistant_turn_id: str,
        base_version_id: str,
        status: str,
        proposal: dict[str, Any],
        created_at: str,
        candidate_version_id: str | None = None,
        decision: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        decided_at: str | None = None,
    ) -> ChangeProposalRecord:
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO change_proposals("
                "proposal_id, session_id, assistant_turn_id, base_version_id, "
                "candidate_version_id, status, proposal_json, decision_json, "
                "idempotency_key, created_at, decided_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal_id, session_id, assistant_turn_id, base_version_id,
                    candidate_version_id, status, dump_json(proposal),
                    dump_json(decision) if decision is not None else None,
                    idempotency_key, created_at, decided_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM change_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            assert row is not None
            return row_to_proposal(row)

    def get_proposal(self, session_id: str, proposal_id: str) -> ChangeProposalRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM change_proposals WHERE session_id = ? AND proposal_id = ?",
                (session_id, proposal_id),
            ).fetchone()
            return row_to_proposal(row) if row is not None else None

    def get_proposal_by_idempotency(
        self, session_id: str, idempotency_key: str
    ) -> ChangeProposalRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM change_proposals WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
            return row_to_proposal(row) if row is not None else None

    def list_proposals(
        self,
        session_id: str,
        *,
        status: str | None = None,
        assistant_turn_id: str | None = None,
    ) -> list[ChangeProposalRecord]:
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if assistant_turn_id is not None:
            clauses.append("assistant_turn_id = ?")
            params.append(assistant_turn_id)
        with self._db.read_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM change_proposals WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at ASC",
                params,
            ).fetchall()
            return [row_to_proposal(row) for row in rows]

    def decide_proposal(
        self,
        session_id: str,
        proposal_id: str,
        *,
        status: str,
        decision: dict[str, Any],
        candidate_version_id: str | None,
        decided_at: str,
    ) -> ChangeProposalRecord | None:
        with self._db.write_tx() as conn:
            cur = conn.execute(
                "UPDATE change_proposals "
                "SET status = ?, decision_json = ?, candidate_version_id = ?, decided_at = ? "
                "WHERE session_id = ? AND proposal_id = ?",
                (status, dump_json(decision), candidate_version_id, decided_at,
                 session_id, proposal_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM change_proposals WHERE session_id = ? AND proposal_id = ?",
                (session_id, proposal_id),
            ).fetchone()
            return row_to_proposal(row) if row is not None else None

    def supersede_pending_proposals(
        self, session_id: str, *, decided_at: str, reason: str = ""
    ) -> list[str]:
        with self._db.write_tx() as conn:
            rows = conn.execute(
                "SELECT proposal_id FROM change_proposals "
                "WHERE session_id = ? AND status = 'pending'",
                (session_id,),
            ).fetchall()
            ids = [row["proposal_id"] for row in rows]
            if not ids:
                return []
            decision_json = dump_json({"decisionType": "superseded", "reason": reason})
            conn.execute(
                "UPDATE change_proposals SET status = 'superseded', "
                "decision_json = COALESCE(decision_json, ?), decided_at = ? "
                "WHERE session_id = ? AND status = 'pending'",
                (decision_json, decided_at, session_id),
            )
            return ids

    def upsert_challenge(
        self,
        *,
        challenge_id: str,
        session_id: str,
        base_version_id: str,
        source_proposal_turn_id: str,
        challenge_turn_id: str,
        status: str,
        created_at: str,
        updated_at: str | None = None,
        current_reason_turn_id: str | None = None,
        reviews: list[dict[str, Any]] | None = None,
    ) -> RevisionChallengeRecord:
        ts = updated_at or created_at
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO revision_challenges("
                "challenge_id, session_id, base_version_id, source_proposal_turn_id, "
                "challenge_turn_id, status, current_reason_turn_id, reviews_json, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(challenge_id) DO UPDATE SET "
                "status = excluded.status, reviews_json = excluded.reviews_json, "
                "current_reason_turn_id = excluded.current_reason_turn_id, "
                "updated_at = excluded.updated_at",
                (
                    challenge_id, session_id, base_version_id, source_proposal_turn_id,
                    challenge_turn_id, status, current_reason_turn_id,
                    dump_json(reviews or []), created_at, ts,
                ),
            )
            row = conn.execute(
                "SELECT * FROM revision_challenges WHERE challenge_id = ?", (challenge_id,)
            ).fetchone()
            assert row is not None
            return row_to_challenge(row)

    def get_challenge(self, session_id: str, challenge_id: str) -> RevisionChallengeRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM revision_challenges WHERE session_id = ? AND challenge_id = ?",
                (session_id, challenge_id),
            ).fetchone()
            return row_to_challenge(row) if row is not None else None

    def list_challenges(self, session_id: str) -> list[RevisionChallengeRecord]:
        with self._db.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM revision_challenges WHERE session_id = ? ORDER BY updated_at ASC",
                (session_id,),
            ).fetchall()
            return [row_to_challenge(row) for row in rows]

    def update_challenge(
        self, session_id: str, challenge_id: str, **fields: Any
    ) -> RevisionChallengeRecord | None:
        if not fields:
            return self.get_challenge(session_id, challenge_id)
        mapped = {
            k: (dump_json(v or []) if k in _CHALLENGE_JSON_FIELDS else v)
            for k, v in fields.items()
        }
        clauses, params = build_update_clause(
            mapped, {**_CHALLENGE_SCALAR_FIELDS, **_CHALLENGE_JSON_FIELDS}
        )
        params.extend([session_id, challenge_id])
        with self._db.write_tx() as conn:
            cur = conn.execute(
                f"UPDATE revision_challenges SET {', '.join(clauses)} "
                "WHERE session_id = ? AND challenge_id = ?",
                params,
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM revision_challenges WHERE session_id = ? AND challenge_id = ?",
                (session_id, challenge_id),
            ).fetchone()
            return row_to_challenge(row) if row is not None else None

    def append_event(
        self, session_id: str, event_type: str, payload: dict[str, Any], created_at: str
    ) -> AuditEventRecord:
        with self._db.write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, event_type, dump_json(payload), created_at),
            )
            row = conn.execute(
                "SELECT * FROM audit_events WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            assert row is not None
            return row_to_audit_event(row)

    def list_events(
        self, session_id: str, *, event_type: str | None = None, limit: int | None = None
    ) -> list[AuditEventRecord]:
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(int(limit))
        with self._db.read_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} "
                f"ORDER BY id ASC{limit_sql}",
                params,
            ).fetchall()
            return [row_to_audit_event(row) for row in rows]
