"""Room event, co-creation sync, reaper, and deletion operations for RoomsRepository."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from ..database import SQLiteDatabase, dump_json, load_json
from ..errors import ConflictError, NotFoundError
from ..models import OnlineRoomEventRecord, OnlineRoomRecord
from ._mappers import row_to_room, row_to_room_event


class _RoomEventsMixin:
    _db: SQLiteDatabase

    @staticmethod
    def _load_room_for_write(conn: sqlite3.Connection, match_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM online_rooms WHERE id = ?", (match_id,)).fetchone()
        if row is None:
            raise NotFoundError("ROOM_NOT_FOUND", "Room not found")
        return row

    @staticmethod
    def _find_player(players: list[dict[str, Any]], player_slot: int) -> dict[str, Any]:
        for player in players:
            if player.get("playerNumber") == player_slot:
                return player
        raise NotFoundError("PLAYER_NOT_FOUND", "Player not found")

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        match_id: str,
        player_slot: int,
        event_type: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> dict[str, Any]:
        next_seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM online_room_events WHERE match_id = ? AND player_slot = ?",
            (match_id, player_slot),
        ).fetchone()
        seq = int(next_seq_row["next_seq"]) if next_seq_row is not None else 1
        conn.execute(
            "INSERT INTO online_room_events("
            "match_id, player_slot, seq, event_type, payload_json, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (match_id, player_slot, seq, event_type, dump_json(payload), created_at),
        )
        return payload

    @staticmethod
    def _save_room(
        conn: sqlite3.Connection,
        match_id: str,
        *,
        status: str,
        players: list[dict[str, Any]],
        now_iso: str,
        expires_at_iso: str,
    ) -> OnlineRoomRecord:
        conn.execute(
            "UPDATE online_rooms SET status = ?, players_json = ?, expires_at = ?, updated_at = ? "
            "WHERE id = ?",
            (status, dump_json(players), expires_at_iso, now_iso, match_id),
        )
        row = conn.execute("SELECT * FROM online_rooms WHERE id = ?", (match_id,)).fetchone()
        assert row is not None
        return row_to_room(row)

    @staticmethod
    def _record_match_event_if_flow_started(
        conn: sqlite3.Connection,
        *,
        match_id: str,
        room_code: str,
        status_after: str,
        players: list[dict[str, Any]],
        event_type: str,
        player_slot: int,
        now_iso: str,
        **details: Any,
    ) -> dict[str, Any] | None:
        player = next((p for p in players if p.get("playerNumber") == player_slot), None)
        if player_slot in (1, 2):
            if not (player and player.get("menuStartedFlow") is True):
                return None
        elif not any(p.get("menuStartedFlow") is True for p in players):
            return None

        event: dict[str, Any] = {
            "eventId": uuid.uuid4().hex,
            "eventType": event_type,
            "matchId": match_id,
            "roomCode": room_code,
            "playerNumber": player_slot,
            "statusAfter": status_after,
            "serverReceivedAt": now_iso,
            **details,
        }
        if player is not None:
            event["studySessionId"] = str(player.get("studySessionId") or "").strip()
            event["menuStartedFlow"] = True
        return _RoomEventsMixin._append_event(
            conn, match_id, player_slot, event_type, event, now_iso
        )

    def append_cocreation_event(
        self,
        match_id: str,
        player_slot: int,
        *,
        event_type: str,
        event_id: str,
        payload: dict[str, Any],
        now_iso: str,
        expires_at_iso: str,
    ) -> tuple[dict[str, Any], bool]:
        with self._db.write_tx() as conn:
            row = self._load_room_for_write(conn, match_id)
            players: list[dict[str, Any]] = load_json(row["players_json"], [])
            player = self._find_player(players, player_slot)
            if player.get("menuStartedFlow") is not True:
                self._save_room(
                    conn,
                    match_id,
                    status=row["status"],
                    players=players,
                    now_iso=now_iso,
                    expires_at_iso=expires_at_iso,
                )
                return {
                    "status": "ok",
                    "recorded": False,
                    "ignored": True,
                    "event": None,
                }, False

            if event_type != "first_stage":
                has_first = conn.execute(
                    "SELECT 1 FROM online_room_events "
                    "WHERE match_id = ? AND player_slot = ? AND event_type = 'first_stage' LIMIT 1",
                    (match_id, player_slot),
                ).fetchone()
                if has_first is None:
                    raise ConflictError(
                        "COCREATION_NOT_STARTED",
                        "Co-creation flow has not started",
                    )

            existing = conn.execute(
                "SELECT payload_json FROM online_room_events "
                "WHERE match_id = ? AND player_slot = ? "
                "AND json_extract(payload_json, '$.eventId') = ? LIMIT 1",
                (match_id, player_slot, event_id),
            ).fetchone()
            if existing is not None:
                return load_json(existing["payload_json"], {}), False

            if event_type == "first_stage" and isinstance(player.get("pendingDraft"), dict):
                pending = player["pendingDraft"]
                existing_draft = conn.execute(
                    "SELECT 1 FROM online_room_events "
                    "WHERE match_id = ? AND player_slot = ? AND event_type = 'draft' LIMIT 1",
                    (match_id, player_slot),
                ).fetchone()
                if existing_draft is None:
                    draft_event = {
                        "eventId": f"draft:{match_id}:{player_slot}",
                        "eventType": "draft",
                        "matchId": match_id,
                        "roomCode": row["room_code"],
                        "playerNumber": player_slot,
                        "studySessionId": str(player.get("studySessionId") or "").strip(),
                        "serverReceivedAt": now_iso,
                        **pending,
                    }
                    self._append_event(conn, match_id, player_slot, "draft", draft_event, now_iso)
                player["pendingDraft"] = None

            self._save_room(
                conn,
                match_id,
                status=row["status"],
                players=players,
                now_iso=now_iso,
                expires_at_iso=expires_at_iso,
            )
            stored = self._append_event(conn, match_id, player_slot, event_type, payload, now_iso)
            return stored, True

    def sync_designer_intention(
        self,
        match_id: str,
        player_slot: int,
        *,
        designer_intention: str,
        event_id: str | None,
        session_id: str | None,
        now_iso: str,
        expires_at_iso: str,
    ) -> OnlineRoomRecord:
        with self._db.write_tx() as conn:
            row = self._load_room_for_write(conn, match_id)
            players: list[dict[str, Any]] = load_json(row["players_json"], [])
            player = self._find_player(players, player_slot)
            if player.get("menuStartedFlow") is not True:
                return self._save_room(
                    conn,
                    match_id,
                    status=row["status"],
                    players=players,
                    now_iso=now_iso,
                    expires_at_iso=expires_at_iso,
                )
            has_first = conn.execute(
                "SELECT 1 FROM online_room_events "
                "WHERE match_id = ? AND player_slot = ? AND event_type = 'first_stage' LIMIT 1",
                (match_id, player_slot),
            ).fetchone()
            if has_first is None:
                raise ConflictError("COCREATION_NOT_STARTED", "Co-creation flow has not started")
            existing = str(player.get("designerIntention") or "").strip()
            if existing and existing != designer_intention:
                raise ConflictError(
                    "INTENTION_FROZEN",
                    "Final designer intention is already frozen",
                )
            if not existing:
                player["designerIntention"] = designer_intention
                self._record_match_event_if_flow_started(
                    conn,
                    match_id=match_id,
                    room_code=row["room_code"],
                    status_after=row["status"],
                    players=players,
                    event_type="designer_intention_synchronized",
                    player_slot=player_slot,
                    now_iso=now_iso,
                    designerIntention=designer_intention,
                )
            if event_id and session_id:
                existing_msg = conn.execute(
                    "SELECT 1 FROM online_room_events "
                    "WHERE match_id = ? AND player_slot = ? "
                    "AND json_extract(payload_json, '$.eventId') = ? LIMIT 1",
                    (match_id, player_slot, event_id),
                ).fetchone()
                if existing_msg is None:
                    msg_event = {
                        "eventId": event_id,
                        "eventType": "message",
                        "matchId": match_id,
                        "roomCode": row["room_code"],
                        "playerNumber": player_slot,
                        "sessionId": session_id,
                        "studySessionId": str(player.get("studySessionId") or "").strip(),
                        "message": designer_intention,
                        "serverReceivedAt": now_iso,
                    }
                    self._append_event(conn, match_id, player_slot, "message", msg_event, now_iso)
            return self._save_room(
                conn,
                match_id,
                status=row["status"],
                players=players,
                now_iso=now_iso,
                expires_at_iso=expires_at_iso,
            )

    def list_room_events(
        self, match_id: str, player_slot: int | None = None
    ) -> list[OnlineRoomEventRecord]:
        with self._db.read_conn() as conn:
            if player_slot is None:
                rows = conn.execute(
                    "SELECT * FROM online_room_events WHERE match_id = ? ORDER BY id",
                    (match_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM online_room_events WHERE match_id = ? AND player_slot = ? "
                    "ORDER BY seq, id",
                    (match_id, player_slot),
                ).fetchall()
            return [row_to_room_event(row) for row in rows]

    def list_all_match_events(self) -> list[OnlineRoomEventRecord]:
        with self._db.read_conn() as conn:
            rows = conn.execute("SELECT * FROM online_room_events ORDER BY id").fetchall()
            return [row_to_room_event(row) for row in rows]

    def reap_expired_rooms(self, now_iso: str) -> int:
        with self._db.write_tx() as conn:
            expired_rows = conn.execute(
                "SELECT * FROM online_rooms WHERE expires_at <= ? "
                "AND status IN ('waiting_for_opponent', 'briefing', 'waiting_for_challenges', "
                "'challenges_ready', 'waiting_for_results', 'results_ready', 'cancelled') "
                "AND NOT EXISTS ("
                "SELECT 1 FROM design_sessions ds "
                "WHERE ds.match_id = online_rooms.id "
                "AND ds.status IN ('active', 'awaiting_intention')"
                ")",
                (now_iso,),
            ).fetchall()
            if not expired_rows:
                return 0
            for row in expired_rows:
                players = load_json(row["players_json"], [])
                if row["status"] not in {"results_ready", "cancelled"}:
                    self._record_match_event_if_flow_started(
                        conn,
                        match_id=row["id"],
                        room_code=row["room_code"],
                        status_after="expired",
                        players=players,
                        event_type="room_expired",
                        player_slot=0,
                        now_iso=now_iso,
                    )
                conn.execute("DELETE FROM online_rooms WHERE id = ?", (row["id"],))
            return len(expired_rows)

    _SESSION_CASCADE_TABLES: tuple[str, ...] = (
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
    )

    @classmethod
    def _delete_sessions_tx(cls, conn: sqlite3.Connection, session_ids: list[str]) -> int:
        if not session_ids:
            return 0
        placeholders = ", ".join("?" for _ in session_ids)
        for table in cls._SESSION_CASCADE_TABLES:
            conn.execute(
                f"DELETE FROM {table} WHERE session_id IN ({placeholders})",
                session_ids,
            )
        return len(session_ids)

    def delete_match(self, match_id: str) -> dict[str, Any]:
        with self._db.write_tx() as conn:
            sess_rows = conn.execute(
                "SELECT session_id FROM design_sessions WHERE match_id = ? AND is_demo = 0",
                (match_id,),
            ).fetchall()
            session_ids = [str(r["session_id"]) for r in sess_rows]
            room_cur = conn.execute("DELETE FROM online_rooms WHERE id = ?", (match_id,))
            event_cur = conn.execute(
                "DELETE FROM online_room_events WHERE match_id = ?", (match_id,)
            )
            deleted_sessions = self._delete_sessions_tx(conn, session_ids)
            if room_cur.rowcount == 0 and event_cur.rowcount == 0 and deleted_sessions == 0:
                raise NotFoundError("MATCH_NOT_FOUND", "Online match not found")
            return {
                "status": "ok",
                "matchId": match_id,
                "deletedRoomCount": room_cur.rowcount,
                "deletedEventCount": event_cur.rowcount,
                "deletedSessionCount": deleted_sessions,
            }

    def clear_matches(self) -> dict[str, Any]:
        with self._db.write_tx() as conn:
            sess_rows = conn.execute(
                "SELECT session_id FROM design_sessions WHERE match_id IS NOT NULL AND is_demo = 0"
            ).fetchall()
            session_ids = [str(r["session_id"]) for r in sess_rows]
            conn.execute("DELETE FROM online_rooms")
            conn.execute("DELETE FROM online_room_events")
            deleted_sessions = self._delete_sessions_tx(conn, session_ids)
        return {
            "status": "ok",
            "cleared": True,
            "deletedSessionCount": deleted_sessions,
        }
