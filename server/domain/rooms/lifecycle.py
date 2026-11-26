"""Pure matchmaking state transitions and public room serialization."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.domain.errors import AuthError, ConflictError, NotFoundError

from .models import (
    OnlineRoom,
    PlayResultSummary,
    RoomPlayer,
    RoomStatus,
    validate_challenge_rows,
)


def create_room(
    *,
    match_id: str,
    room_code: str,
    player_token: str,
    now: float,
    study_session_id: str = "",
    menu_started_flow: bool = False,
    created_at: str | None = None,
    expires_at: str | None = None,
) -> OnlineRoom:
    player = RoomPlayer(
        playerNumber=1,
        studySessionId=study_session_id,
        menuStartedFlow=menu_started_flow,
        token=player_token,
    )
    return OnlineRoom(
        matchId=match_id,
        roomCode=room_code,
        status=RoomStatus.WAITING_FOR_OPPONENT,
        players=[player],
        lastActivity=now,
        createdAt=created_at,
        expiresAt=expires_at,
    )


def join_room(
    room: OnlineRoom,
    *,
    player_token: str,
    now: float,
    study_session_id: str = "",
    menu_started_flow: bool = False,
) -> tuple[OnlineRoom, RoomPlayer]:
    if room.status == RoomStatus.CANCELLED.value:
        raise ConflictError("ROOM_CANCELLED", "error.room_cancelled")
    if len(room.players) >= 2:
        raise ConflictError("ROOM_FULL", "error.room_full")

    next_room = room.model_copy(deep=True)
    player = RoomPlayer(
        playerNumber=2,
        studySessionId=study_session_id,
        menuStartedFlow=menu_started_flow,
        token=player_token,
    )
    next_room.players.append(player)
    next_room.status = RoomStatus.BRIEFING.value
    next_room.last_activity = now
    return next_room, player


def _find_player(room: OnlineRoom, player_number: int) -> RoomPlayer:
    for player in room.players:
        if player.player_number == player_number:
            return player
    raise NotFoundError("PLAYER_NOT_FOUND", "error.player_not_found")


def set_player_ready(
    room: OnlineRoom,
    *,
    player_number: int,
    ready: bool,
    now: float,
) -> tuple[OnlineRoom, bool]:
    if room.status == RoomStatus.CANCELLED.value:
        raise ConflictError("ROOM_CANCELLED", "error.room_cancelled")
    if len(room.players) < 2:
        raise ConflictError("OPPONENT_REQUIRED", "error.room_not_ready")

    next_room = room.model_copy(deep=True)
    player = _find_player(next_room, player_number)

    if next_room.status in {
        RoomStatus.WAITING_FOR_CHALLENGES.value,
        RoomStatus.CHALLENGES_READY.value,
        RoomStatus.WAITING_FOR_RESULTS.value,
        RoomStatus.RESULTS_READY.value,
    }:
        if ready and player.ready:
            next_room.last_activity = now
            return next_room, False
        raise ConflictError("MATCH_ALREADY_ADVANCED", "error.room_not_ready")

    changed = player.ready != ready
    player.ready = ready
    next_room.status = (
        RoomStatus.WAITING_FOR_CHALLENGES.value
        if all(item.ready for item in next_room.players)
        else RoomStatus.BRIEFING.value
    )
    next_room.last_activity = now
    return next_room, changed


def submit_player_challenge(
    room: OnlineRoom,
    *,
    player_number: int,
    rows: Sequence[str],
    ai_assistant_mode: str,
    now: float,
) -> tuple[OnlineRoom, bool]:
    validated_rows = validate_challenge_rows(rows)
    if room.status == RoomStatus.CANCELLED.value:
        raise ConflictError("ROOM_CANCELLED", "error.room_cancelled")
    if len(room.players) < 2 or not all(item.ready for item in room.players):
        raise ConflictError("PLAYERS_NOT_READY", "error.room_not_ready")

    next_room = room.model_copy(deep=True)
    player = _find_player(next_room, player_number)

    if player.challenge_rows is not None:
        if (
            player.challenge_rows != validated_rows
            or player.ai_assistant_mode != ai_assistant_mode
        ):
            raise ConflictError("CHALLENGE_FROZEN", "error.idempotency_conflict")
        next_room.last_activity = now
        return next_room, False

    player.challenge_rows = validated_rows
    player.ai_assistant_mode = ai_assistant_mode
    both_submitted = all(item.challenge_rows is not None for item in next_room.players)
    next_room.status = (
        RoomStatus.CHALLENGES_READY.value
        if both_submitted
        else RoomStatus.WAITING_FOR_CHALLENGES.value
    )
    next_room.last_activity = now
    return next_room, True


def submit_player_result(
    room: OnlineRoom,
    *,
    player_number: int,
    result: PlayResultSummary,
    now: float,
) -> tuple[OnlineRoom, bool]:
    if room.status == RoomStatus.CANCELLED.value:
        raise ConflictError("ROOM_CANCELLED", "error.room_cancelled")
    if len(room.players) < 2 or not all(item.challenge_rows is not None for item in room.players):
        raise ConflictError("CHALLENGES_NOT_READY", "error.room_not_ready")

    next_room = room.model_copy(deep=True)
    player = _find_player(next_room, player_number)

    if player.result is not None:
        if player.result != result:
            raise ConflictError("RESULT_FROZEN", "error.idempotency_conflict")
        next_room.last_activity = now
        return next_room, False

    player.result = result
    both_finished = all(item.result is not None for item in next_room.players)
    next_room.status = (
        RoomStatus.RESULTS_READY.value
        if both_finished
        else RoomStatus.WAITING_FOR_RESULTS.value
    )
    next_room.last_activity = now
    return next_room, True


def leave_room(
    room: OnlineRoom,
    *,
    player_number: int,
    now: float,
) -> tuple[OnlineRoom, bool]:
    next_room = room.model_copy(deep=True)
    player = _find_player(next_room, player_number)
    already_left = player.left
    player.left = True
    next_room.last_activity = now
    if next_room.status == RoomStatus.RESULTS_READY.value or player.result is not None:
        return next_room, False
    next_room.status = RoomStatus.CANCELLED.value
    return next_room, not already_left


def expire_room(room: OnlineRoom) -> OnlineRoom:
    next_room = room.model_copy(deep=True)
    if next_room.status not in {RoomStatus.RESULTS_READY.value, RoomStatus.CANCELLED.value}:
        next_room.status = RoomStatus.EXPIRED.value
    return next_room


def serialize_public_room(
    room: OnlineRoom,
    *,
    player_number: int | None = None,
    include_token: bool = False,
) -> dict[str, Any]:
    """Serialize OnlineRoom matching the Unity/WebGL room DTO contract."""
    player = (
        next((p for p in room.players if p.player_number == player_number), None)
        if player_number is not None
        else None
    )
    payload: dict[str, Any] = {
        "matchId": room.match_id,
        "roomCode": room.room_code,
        "status": room.status,
        "playerNumber": player.player_number if player else 0,
        "players": [
            {
                "playerNumber": item.player_number,
                "ready": bool(item.ready),
                "challengeSubmitted": item.challenge_rows is not None,
                "resultSubmitted": item.result is not None,
            }
            for item in room.players
        ],
    }
    if include_token and player is not None:
        payload["playerToken"] = player.token
    if player is not None and player.challenge_rows is not None:
        payload["ownChallengeMetadata"] = _challenge_metadata(player)
    if (
        player is not None
        and len(room.players) == 2
        and all(item.challenge_rows is not None for item in room.players)
    ):
        opponent = next(p for p in room.players if p.player_number != player.player_number)
        payload["opponentChallengeRows"] = list(opponent.challenge_rows or ())
        payload["opponentChallengeMetadata"] = _challenge_metadata(opponent)
    if player is not None and player.result is not None:
        payload["ownResult"] = player.result.model_dump(by_alias=True)
    if player is not None and len(room.players) == 2:
        opponent = next(p for p in room.players if p.player_number != player.player_number)
        if opponent.result is not None:
            payload["opponentResult"] = opponent.result.model_dump(by_alias=True)
    return payload


def _challenge_metadata(player: RoomPlayer) -> dict[str, Any]:
    metadata: dict[str, Any] = {"aiAssistantMode": player.ai_assistant_mode}
    intention = str(player.designer_intention or "").strip()
    if intention:
        metadata["designerIntention"] = intention
    return metadata


def authenticate_room_player(room: OnlineRoom, supplied_token: str | None) -> RoomPlayer:
    token = str(supplied_token or "").strip()
    for player in room.players:
        if player.token and player.token == token:
            return player
    raise AuthError("INVALID_PLAYER_TOKEN", "error.room_invalid_token")
