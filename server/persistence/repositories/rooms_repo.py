"""Repository for online matchmaking rooms and per-player co-creation sync events."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from ..database import SQLiteDatabase, dump_json, load_json
from ..errors import ConflictError, NotFoundError
from ..models import OnlineRoomRecord
from ._mappers import row_to_room
from ._room_events_ops import _RoomEventsMixin


class RoomsRepository(_RoomEventsMixin):
    """Transactional persistence for 2-player online rooms and match flow events."""

    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    def create_room(
        self,
        match_id: str,
        room_code: str,
        *,
        player1: dict[str, Any],
        now_iso: str,
        expires_at_iso: str,
        config: dict[str, Any] | None = None,
    ) -> OnlineRoomRecord:
        players = [player1]
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO online_rooms("
                "id, room_code, status, config_json, players_json, "
                "created_at, expires_at, updated_at"
                ") VALUES (?, ?, 'waiting_for_opponent', ?, ?, ?, ?, ?)",
                (
                    match_id, room_code, dump_json(config or {}), dump_json(players),
                    now_iso, expires_at_iso, now_iso,
                ),
            )
            self._record_match_event_if_flow_started(
                conn, match_id=match_id, room_code=room_code,
                status_after="waiting_for_opponent", players=players,
                event_type="room_created", player_slot=1, now_iso=now_iso,
            )
            row = conn.execute("SELECT * FROM online_rooms WHERE id = ?", (match_id,)).fetchone()
            assert row is not None
            return row_to_room(row)

    def allocate_and_create_room(
        self,
        match_id: str,
        code_generator: Callable[[], str],
        *,
        player1: dict[str, Any],
        now_iso: str,
        expires_at_iso: str,
        config: dict[str, Any] | None = None,
        max_attempts: int = 100,
    ) -> OnlineRoomRecord:
        for _ in range(max_attempts):
            room_code = code_generator()
            try:
                return self.create_room(
                    match_id, room_code, player1=player1, now_iso=now_iso,
                    expires_at_iso=expires_at_iso, config=config,
                )
            except sqlite3.IntegrityError:
                continue
        raise ConflictError("ROOM_CODE_EXHAUSTED", "Unable to allocate a room code")

    def get_room(self, match_id: str) -> OnlineRoomRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute("SELECT * FROM online_rooms WHERE id = ?", (match_id,)).fetchone()
            return row_to_room(row) if row is not None else None

    def get_room_by_code(self, room_code: str) -> OnlineRoomRecord | None:
        with self._db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM online_rooms WHERE room_code = ?", (room_code,)
            ).fetchone()
            return row_to_room(row) if row is not None else None

    def touch_room(
        self, match_id: str, *, now_iso: str, expires_at_iso: str
    ) -> OnlineRoomRecord | None:
        with self._db.write_tx() as conn:
            row = conn.execute("SELECT * FROM online_rooms WHERE id = ?", (match_id,)).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE online_rooms SET expires_at = ?, updated_at = ? WHERE id = ?",
                (expires_at_iso, now_iso, match_id),
            )
            updated = conn.execute(
                "SELECT * FROM online_rooms WHERE id = ?", (match_id,)
            ).fetchone()
            assert updated is not None
            return row_to_room(updated)

    def join_room(
        self, room_code: str, *, player2: dict[str, Any], now_iso: str, expires_at_iso: str
    ) -> OnlineRoomRecord:
        with self._db.write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM online_rooms WHERE room_code = ?", (room_code,)
            ).fetchone()
            if row is None:
                raise NotFoundError("ROOM_NOT_FOUND", "Room not found")
            if row["status"] == "cancelled":
                raise ConflictError("ROOM_CANCELLED", "Room has been cancelled")
            players: list[dict[str, Any]] = load_json(row["players_json"], [])
            if len(players) >= 2:
                raise ConflictError("ROOM_FULL", "Room is full")
            players.append(player2)
            updated = self._save_room(
                conn, row["id"], status="briefing", players=players,
                now_iso=now_iso, expires_at_iso=expires_at_iso,
            )
            self._record_match_event_if_flow_started(
                conn, match_id=row["id"], room_code=row["room_code"], status_after="briefing",
                players=players, event_type="player_joined", player_slot=2, now_iso=now_iso,
            )
            return updated

    def set_ready(
        self, match_id: str, player_slot: int, *, ready: bool, now_iso: str, expires_at_iso: str
    ) -> OnlineRoomRecord:
        with self._db.write_tx() as conn:
            row = self._load_room_for_write(conn, match_id)
            if row["status"] == "cancelled":
                raise ConflictError("ROOM_CANCELLED", "Room has been cancelled")
            players: list[dict[str, Any]] = load_json(row["players_json"], [])
            player = self._find_player(players, player_slot)
            if len(players) < 2:
                raise ConflictError(
                    "OPPONENT_REQUIRED", "Cannot become ready before an opponent joins"
                )
            if row["status"] in {
                "waiting_for_challenges", "challenges_ready",
                "waiting_for_results", "results_ready",
            }:
                if ready and player.get("ready") is True:
                    return self._save_room(
                        conn, match_id, status=row["status"], players=players,
                        now_iso=now_iso, expires_at_iso=expires_at_iso,
                    )
                raise ConflictError(
                    "MATCH_ALREADY_ADVANCED", "Match has already advanced past briefing"
                )
            previous_ready = bool(player.get("ready"))
            player["ready"] = ready
            new_status = (
                "waiting_for_challenges"
                if all(bool(item.get("ready")) for item in players)
                else "briefing"
            )
            updated = self._save_room(
                conn, match_id, status=new_status, players=players,
                now_iso=now_iso, expires_at_iso=expires_at_iso,
            )
            if previous_ready != ready:
                self._record_match_event_if_flow_started(
                    conn, match_id=match_id, room_code=row["room_code"], status_after=new_status,
                    players=players, event_type="ready_changed", player_slot=player_slot,
                    now_iso=now_iso, ready=ready,
                )
            return updated

    def record_draft(
        self,
        match_id: str,
        player_slot: int,
        *,
        requested_draft: dict[str, Any],
        now_iso: str,
        expires_at_iso: str,
        match_fn: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        with self._db.write_tx() as conn:
            row = self._load_room_for_write(conn, match_id)
            if row["status"] == "cancelled":
                raise ConflictError("ROOM_CANCELLED", "Room has been cancelled")
            players: list[dict[str, Any]] = load_json(row["players_json"], [])
            player = self._find_player(players, player_slot)
            if player.get("menuStartedFlow") is not True:
                return {
                    "status": "ok", "recorded": False, "pending": False,
                    "ignored": True, "event": None,
                }
            pending = player.get("pendingDraft")
            if pending is not None and pending != requested_draft:
                raise ConflictError("DRAFT_PENDING", "Draft settings are already pending")
            has_first_stage = conn.execute(
                "SELECT 1 FROM online_room_events "
                "WHERE match_id = ? AND player_slot = ? AND event_type = 'first_stage' LIMIT 1",
                (match_id, player_slot),
            ).fetchone() is not None
            if has_first_stage:
                draft_row = conn.execute(
                    "SELECT payload_json FROM online_room_events "
                    "WHERE match_id = ? AND player_slot = ? AND event_type = 'draft' LIMIT 1",
                    (match_id, player_slot),
                ).fetchone()
                if draft_row is None:
                    raise ConflictError(
                        "DRAFT_AFTER_FIRST_STAGE", "Draft must be submitted before first_stage"
                    )
                existing_draft = load_json(draft_row["payload_json"], {})
                is_same = (
                    match_fn(existing_draft, requested_draft)
                    if match_fn is not None
                    else all(existing_draft.get(k) == v for k, v in requested_draft.items())
                )
                if not is_same:
                    raise ConflictError("DRAFT_FROZEN", "Draft record is already frozen")
                self._save_room(
                    conn, match_id, status=row["status"], players=players,
                    now_iso=now_iso, expires_at_iso=expires_at_iso,
                )
                return {
                    "status": "ok",
                    "recorded": False,
                    "pending": False,
                    "event": existing_draft,
                }
            player["pendingDraft"] = requested_draft
            self._save_room(
                conn, match_id, status=row["status"], players=players,
                now_iso=now_iso, expires_at_iso=expires_at_iso,
            )
            return {"status": "ok", "recorded": False, "pending": True, "event": None}

    def submit_challenge(
        self,
        match_id: str,
        player_slot: int,
        *,
        rows: list[str],
        ai_assistant_mode: str,
        now_iso: str,
        expires_at_iso: str,
    ) -> OnlineRoomRecord:
        with self._db.write_tx() as conn:
            row = self._load_room_for_write(conn, match_id)
            if row["status"] == "cancelled":
                raise ConflictError("ROOM_CANCELLED", "Room has been cancelled")
            players: list[dict[str, Any]] = load_json(row["players_json"], [])
            player = self._find_player(players, player_slot)
            if len(players) < 2 or not all(bool(item.get("ready")) for item in players):
                raise ConflictError(
                    "PLAYERS_NOT_READY", "Both players must be ready before submitting challenges"
                )
            existing_rows = player.get("challengeRows")
            if existing_rows is not None:
                if existing_rows != rows or player.get("aiAssistantMode") != ai_assistant_mode:
                    raise ConflictError("CHALLENGE_FROZEN", "Submitted challenge is already frozen")
                return self._save_room(
                    conn, match_id, status=row["status"], players=players,
                    now_iso=now_iso, expires_at_iso=expires_at_iso,
                )
            player["challengeRows"] = list(rows)
            player["aiAssistantMode"] = ai_assistant_mode
            both_submitted = all(item.get("challengeRows") is not None for item in players)
            new_status = "challenges_ready" if both_submitted else "waiting_for_challenges"
            updated = self._save_room(
                conn, match_id, status=new_status, players=players,
                now_iso=now_iso, expires_at_iso=expires_at_iso,
            )
            self._record_match_event_if_flow_started(
                conn, match_id=match_id, room_code=row["room_code"], status_after=new_status,
                players=players, event_type="challenge_submitted", player_slot=player_slot,
                now_iso=now_iso, aiAssistantMode=ai_assistant_mode, rows=list(rows),
            )
            return updated

    def submit_result(
        self,
        match_id: str,
        player_slot: int,
        *,
        result: dict[str, Any],
        now_iso: str,
        expires_at_iso: str,
    ) -> OnlineRoomRecord:
        with self._db.write_tx() as conn:
            row = self._load_room_for_write(conn, match_id)
            if row["status"] == "cancelled":
                raise ConflictError("ROOM_CANCELLED", "Room has been cancelled")
            players: list[dict[str, Any]] = load_json(row["players_json"], [])
            player = self._find_player(players, player_slot)
            ready_count = sum(1 for item in players if item.get("challengeRows") is not None)
            if len(players) < 2 or ready_count < 2:
                raise ConflictError(
                    "CHALLENGES_NOT_READY",
                    "Both challenges must be ready before submitting a result",
                )
            existing_result = player.get("result")
            if existing_result is not None:
                if existing_result != result:
                    raise ConflictError("RESULT_FROZEN", "Submitted result is already frozen")
                return self._save_room(
                    conn, match_id, status=row["status"], players=players,
                    now_iso=now_iso, expires_at_iso=expires_at_iso,
                )
            player["result"] = dict(result)
            both_finished = all(item.get("result") is not None for item in players)
            new_status = "results_ready" if both_finished else "waiting_for_results"
            updated = self._save_room(
                conn, match_id, status=new_status, players=players,
                now_iso=now_iso, expires_at_iso=expires_at_iso,
            )
            self._record_match_event_if_flow_started(
                conn, match_id=match_id, room_code=row["room_code"], status_after=new_status,
                players=players, event_type="result_submitted", player_slot=player_slot,
                now_iso=now_iso, **result,
            )
            return updated

    def leave_room(
        self, match_id: str, player_slot: int, *, now_iso: str, expires_at_iso: str
    ) -> OnlineRoomRecord:
        with self._db.write_tx() as conn:
            row = self._load_room_for_write(conn, match_id)
            players: list[dict[str, Any]] = load_json(row["players_json"], [])
            player = self._find_player(players, player_slot)
            already_left = bool(player.get("left"))
            player["left"] = True
            if row["status"] == "results_ready" or player.get("result") is not None:
                return self._save_room(
                    conn, match_id, status=row["status"], players=players,
                    now_iso=now_iso, expires_at_iso=expires_at_iso,
                )
            updated = self._save_room(
                conn, match_id, status="cancelled", players=players,
                now_iso=now_iso, expires_at_iso=expires_at_iso,
            )
            if not already_left:
                self._record_match_event_if_flow_started(
                    conn, match_id=match_id, room_code=row["room_code"], status_after="cancelled",
                    players=players, event_type="player_left", player_slot=player_slot,
                    now_iso=now_iso,
                )
            return updated
