"""Repository for study records and legacy JSONL export compatibility."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from ..database import SQLiteDatabase, dump_json, load_json
from ..models import StudyRecord
from ._mappers import row_to_study_record


class StudyRepository:
    """Persistence for survey, level-start/end, journey, and study artifact logs."""

    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    def insert_record(
        self,
        record_type: str,
        payload: dict[str, Any],
        *,
        match_id: str | None = None,
        participant_id: str | None = None,
        round_id: str | None = None,
        created_at: str,
    ) -> StudyRecord:
        with self._db.write_tx() as conn:
            cursor = conn.execute(
                "INSERT INTO study_records("
                "record_type, match_id, participant_id, round_id, payload_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record_type,
                    match_id,
                    participant_id,
                    round_id,
                    dump_json(payload),
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM study_records WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            assert row is not None
            return row_to_study_record(row)

    def query_records(
        self,
        *,
        record_type: str | None = None,
        match_id: str | None = None,
        participant_id: str | None = None,
        round_id: str | None = None,
        since_iso: str | None = None,
        until_iso: str | None = None,
        limit: int | None = None,
    ) -> list[StudyRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if record_type is not None:
            clauses.append("record_type = ?")
            params.append(record_type)
        if match_id is not None:
            clauses.append("match_id = ?")
            params.append(match_id)
        if participant_id is not None:
            clauses.append("participant_id = ?")
            params.append(participant_id)
        if round_id is not None:
            clauses.append("round_id = ?")
            params.append(round_id)
        if since_iso is not None:
            clauses.append("created_at >= ?")
            params.append(since_iso)
        if until_iso is not None:
            clauses.append("created_at <= ?")
            params.append(until_iso)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(int(limit))

        with self._db.read_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM study_records {where_sql} ORDER BY id ASC{limit_sql}",
                params,
            ).fetchall()
            return [row_to_study_record(row) for row in rows]

    def delete_records(
        self,
        *,
        record_type: str | None = None,
        match_id: str | None = None,
        participant_id: str | None = None,
        round_id: str | None = None,
        record_id: int | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if record_id is not None:
            clauses.append("id = ?")
            params.append(record_id)
        if record_type is not None:
            clauses.append("record_type = ?")
            params.append(record_type)
        if match_id is not None:
            clauses.append("match_id = ?")
            params.append(match_id)
        if participant_id is not None:
            clauses.append("participant_id = ?")
            params.append(participant_id)
        if round_id is not None:
            clauses.append("round_id = ?")
            params.append(round_id)
        if not clauses:
            raise ValueError("Refusing unfiltered delete_records; use clear_records()")
        with self._db.write_tx() as conn:
            cur = conn.execute(
                f"DELETE FROM study_records WHERE {' AND '.join(clauses)}", params
            )
            return cur.rowcount

    def clear_records(self, *, record_type: str | None = None) -> int:
        with self._db.write_tx() as conn:
            if record_type is None:
                cur = conn.execute("DELETE FROM study_records")
            else:
                cur = conn.execute(
                    "DELETE FROM study_records WHERE record_type = ?", (record_type,)
                )
            return cur.rowcount

    def iter_jsonl_export(
        self,
        record_type: str | None = None,
        *,
        chunk_size: int = 500,
    ) -> Iterator[str]:
        """Yield JSONL-formatted lines ordered by insertion id using keyset pagination."""
        last_id = 0
        while True:
            with self._db.read_conn() as conn:
                if record_type is None:
                    rows = conn.execute(
                        "SELECT id, payload_json FROM study_records "
                        "WHERE id > ? ORDER BY id ASC LIMIT ?",
                        (last_id, chunk_size),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, payload_json FROM study_records "
                        "WHERE record_type = ? AND id > ? ORDER BY id ASC LIMIT ?",
                        (record_type, last_id, chunk_size),
                    ).fetchall()
            if not rows:
                break
            for row in rows:
                payload = load_json(row["payload_json"], {})
                yield json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n"
                last_id = int(row["id"])
            if len(rows) < chunk_size:
                break
