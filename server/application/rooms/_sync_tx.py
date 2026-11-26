"""Atomic in-process SQLite transaction helpers for co-creation -> room sync."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from typing import Any

from server.domain.errors import ConflictError, ValidationError
from server.domain.rooms import SYNC_ID_PATTERN
from server.persistence.database import dump_json, load_json
from server.persistence.errors import PersistenceError
from server.persistence.repositories._room_events_ops import _RoomEventsMixin

from ._helpers import (
    build_validated_flow_event,
    map_persistence_error,
    to_public_room_payload,
)

logger = logging.getLogger(__name__)


def record_cocreation_event_on_conn(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    player_slot: int,
    event_payload: Mapping[str, Any],
    now_iso: str,
    expires_at_iso: str,
) -> tuple[dict[str, Any], bool]:
    """Append one idempotent co-creation flow event using caller's open write tx."""
    if player_slot not in (1, 2):
        raise ValidationError(
            "INVALID_PLAYER_NUMBER",
            "error.validation_failed",
            message="Invalid player number",
        )

    try:
        row = _RoomEventsMixin._load_room_for_write(conn, match_id)
    except PersistenceError as exc:
        if exc.code == "ROOM_NOT_FOUND":
            logger.warning(
                "Skipping room co-creation event sync because online_rooms row was not found "
                "(match_id=%s, player_slot=%s)",
                match_id,
                player_slot,
            )
            return {
                "status": "ok",
                "recorded": False,
                "ignored": True,
                "event": None,
            }, False
        map_persistence_error(exc)
    try:
        players: list[dict[str, Any]] = load_json(row["players_json"], [])
        player = _RoomEventsMixin._find_player(players, player_slot)
    except PersistenceError as exc:
        map_persistence_error(exc)

    if player.get("menuStartedFlow") is not True:
        _RoomEventsMixin._save_room(
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

    validated_event = build_validated_flow_event(
        match_id=match_id,
        room_code=str(row["room_code"]),
        player=player,
        event_payload=event_payload,
        now_iso=now_iso,
    )
    event_type = str(validated_event["eventType"])
    event_id = str(validated_event["eventId"])

    if event_type != "first_stage":
        has_first = conn.execute(
            "SELECT 1 FROM online_room_events "
            "WHERE match_id = ? AND player_slot = ? AND event_type = 'first_stage' LIMIT 1",
            (match_id, player_slot),
        ).fetchone()
        if has_first is None:
            raise ConflictError(
                "COCREATION_NOT_STARTED",
                "error.room_not_ready",
                message="Co-creation flow has not started",
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
                "roomCode": str(row["room_code"]),
                "playerNumber": player_slot,
                "studySessionId": str(player.get("studySessionId") or "").strip(),
                "serverReceivedAt": now_iso,
                **pending,
            }
            _RoomEventsMixin._append_event(
                conn, match_id, player_slot, "draft", draft_event, now_iso
            )
        player["pendingDraft"] = None

    conn.execute(
        "UPDATE online_rooms SET players_json = ?, expires_at = ?, updated_at = ? WHERE id = ?",
        (dump_json(players), expires_at_iso, now_iso, match_id),
    )
    stored = _RoomEventsMixin._append_event(
        conn, match_id, player_slot, event_type, validated_event, now_iso
    )
    return stored, True


def record_designer_intention_on_conn(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    player_slot: int,
    designer_intention: str,
    event_id: str | None,
    session_id: str | None,
    now_iso: str,
    expires_at_iso: str,
) -> dict[str, Any]:
    """Record final designer intention atomically inside caller's open write tx."""
    if player_slot not in (1, 2):
        raise ValidationError(
            "INVALID_PLAYER_NUMBER",
            "error.validation_failed",
            message="Invalid player number",
        )
    clean_intention = str(designer_intention or "").strip()
    if not clean_intention:
        raise ValidationError(
            "DESIGNER_INTENTION_REQUIRED",
            "error.validation_failed",
            message="Final designer intention is required",
        )
    if len(clean_intention) > 4000:
        raise ValidationError(
            "DESIGNER_INTENTION_TOO_LONG",
            "error.validation_failed",
            message="Designer intention must contain at most 4000 characters",
        )

    clean_event_id = str(event_id or "").strip()
    clean_session_id = str(session_id or "").strip()
    if clean_event_id and not SYNC_ID_PATTERN.fullmatch(clean_event_id):
        raise ValidationError(
            "INVALID_EVENT_ID",
            "error.invalid_identifier",
            message="Invalid event ID",
        )
    if clean_session_id and not SYNC_ID_PATTERN.fullmatch(clean_session_id):
        raise ValidationError(
            "INVALID_SESSION_ID",
            "error.invalid_identifier",
            message="Invalid session ID",
        )

    try:
        row = _RoomEventsMixin._load_room_for_write(conn, match_id)
    except PersistenceError as exc:
        if exc.code == "ROOM_NOT_FOUND":
            logger.warning(
                "Skipping room designer intention sync because online_rooms row was not found "
                "(match_id=%s, player_slot=%s)",
                match_id,
                player_slot,
            )
            return {"status": "ok", "recorded": False, "ignored": True}
        map_persistence_error(exc)
    try:
        players: list[dict[str, Any]] = load_json(row["players_json"], [])
        player = _RoomEventsMixin._find_player(players, player_slot)
    except PersistenceError as exc:
        map_persistence_error(exc)

    if player.get("menuStartedFlow") is not True:
        _RoomEventsMixin._save_room(
            conn,
            match_id,
            status=row["status"],
            players=players,
            now_iso=now_iso,
            expires_at_iso=expires_at_iso,
        )
        return {"status": "ok", "recorded": False, "ignored": True}

    has_first = conn.execute(
        "SELECT 1 FROM online_room_events "
        "WHERE match_id = ? AND player_slot = ? AND event_type = 'first_stage' LIMIT 1",
        (match_id, player_slot),
    ).fetchone()
    if has_first is None:
        raise ConflictError(
            "COCREATION_NOT_STARTED",
            "error.room_not_ready",
            message="Co-creation flow has not started",
        )

    existing = str(player.get("designerIntention") or "").strip()
    if existing and existing != clean_intention:
        raise ConflictError(
            "INTENTION_FROZEN",
            "error.idempotency_conflict",
            message="Final designer intention is already frozen",
        )
    if not existing:
        player["designerIntention"] = clean_intention
        _RoomEventsMixin._record_match_event_if_flow_started(
            conn,
            match_id=match_id,
            room_code=str(row["room_code"]),
            status_after=str(row["status"]),
            players=players,
            event_type="designer_intention_synchronized",
            player_slot=player_slot,
            now_iso=now_iso,
            designerIntention=clean_intention,
        )

    if clean_event_id and clean_session_id:
        existing_msg = conn.execute(
            "SELECT 1 FROM online_room_events "
            "WHERE match_id = ? AND player_slot = ? "
            "AND json_extract(payload_json, '$.eventId') = ? LIMIT 1",
            (match_id, player_slot, clean_event_id),
        ).fetchone()
        if existing_msg is None:
            msg_event = {
                "eventId": clean_event_id,
                "eventType": "message",
                "matchId": match_id,
                "roomCode": str(row["room_code"]),
                "playerNumber": player_slot,
                "sessionId": clean_session_id,
                "studySessionId": str(player.get("studySessionId") or "").strip(),
                "message": clean_intention,
                "serverReceivedAt": now_iso,
            }
            _RoomEventsMixin._append_event(
                conn, match_id, player_slot, "message", msg_event, now_iso
            )

    updated = _RoomEventsMixin._save_room(
        conn,
        match_id,
        status=str(row["status"]),
        players=players,
        now_iso=now_iso,
        expires_at_iso=expires_at_iso,
    )
    return to_public_room_payload(updated, player_number=player_slot)
