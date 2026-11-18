from fastapi import APIRouter, HTTPException, Request

from ..runtime_state import app_attr, call_app_hook, current_time
from ..schemas import (
    OnlineChallengeRequest,
    OnlineCoCreationFlowEventRequest,
    OnlineDesignerIntentionSyncRequest,
    OnlineDraftRequest,
    OnlineReadyRequest,
    OnlineResultRequest,
    OnlineRoomCreateRequest,
    OnlineRoomJoinRequest,
)
from ..services.online_rooms import (
    ONLINE_ROOMS,
    ONLINE_ROOMS_LOCK,
    append_cocreation_flow_event,
    append_cocreation_message,
    append_online_match_event,
    build_cocreation_flow_event,
    cleanup_expired_online_rooms,
    create_unique_online_room_code,
    draft_events_match,
    find_existing_online_draft_event,
    has_cocreation_first_stage,
    materialize_pending_draft,
    normalize_online_designer_intention,
    normalize_online_draft_metadata,
    normalize_online_room_code,
    normalize_study_session_id,
    require_cocreation_intention_sync_secret,
    require_online_room,
    serialize_online_room,
    validate_online_ai_assistant_mode,
    validate_online_challenge_rows,
    validate_online_result,
)

import secrets
import uuid


router = APIRouter()


def _rooms_lock():
    return app_attr("ONLINE_ROOMS_LOCK") or ONLINE_ROOMS_LOCK


def _rooms():
    rooms = app_attr("ONLINE_ROOMS")
    return ONLINE_ROOMS if rooms is None else rooms


@router.post("/online/rooms")
def create_online_room(payload: OnlineRoomCreateRequest | None = None):
    study_session_id = normalize_study_session_id(
        payload.studySessionId if payload is not None else ""
    )

    with _rooms_lock():
        cleanup_expired_online_rooms()
        player = {
            "playerNumber": 1,
            "studySessionId": study_session_id,
            "menuStartedFlow": payload.menuStartedFlow is True if payload else False,
            "token": secrets.token_urlsafe(32),
            "ready": False,
            "challengeRows": None,
            "aiAssistantMode": None,
            "designerIntention": None,
            "result": None,
            "pendingDraft": None,
        }
        match_id = uuid.uuid4().hex
        room = {
            "matchId": match_id,
            "roomCode": create_unique_online_room_code(),
            "status": "waiting_for_opponent",
            "players": [player],
            "lastActivity": current_time(),
        }
        _rooms()[match_id] = room
        append_online_match_event(room, "room_created", player_number=1)
        return serialize_online_room(room, player, include_token=True)


@router.post("/online/rooms/join")
def join_online_room(payload: OnlineRoomJoinRequest):
    room_code = normalize_online_room_code(payload.roomCode)
    study_session_id = normalize_study_session_id(payload.studySessionId)

    with _rooms_lock():
        cleanup_expired_online_rooms()
        room = next(
            (
                item
                for item in _rooms().values()
                if item["roomCode"] == room_code
            ),
            None,
        )

        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")

        if room["status"] == "cancelled":
            raise HTTPException(status_code=409, detail="Room has been cancelled")

        if len(room["players"]) >= 2:
            raise HTTPException(status_code=409, detail="Room is full")

        player = {
            "playerNumber": 2,
            "studySessionId": study_session_id,
            "menuStartedFlow": payload.menuStartedFlow is True,
            "token": secrets.token_urlsafe(32),
            "ready": False,
            "challengeRows": None,
            "aiAssistantMode": None,
            "designerIntention": None,
            "result": None,
            "pendingDraft": None,
        }
        room["players"].append(player)
        room["status"] = "briefing"
        room["lastActivity"] = current_time()
        append_online_match_event(room, "player_joined", player_number=2)
        return serialize_online_room(room, player, include_token=True)


@router.get("/online/rooms/{match_id}")
def get_online_room(match_id: str, request: Request):
    with _rooms_lock():
        room, player = require_online_room(
            match_id,
            request.headers.get("X-Player-Token"),
        )
        room["lastActivity"] = current_time()
        return serialize_online_room(room, player)


@router.post("/online/rooms/{match_id}/ready")
def set_online_ready(
    match_id: str,
    payload: OnlineReadyRequest,
    request: Request,
):
    with _rooms_lock():
        room, player = require_online_room(
            match_id,
            request.headers.get("X-Player-Token"),
        )

        if room["status"] == "cancelled":
            raise HTTPException(status_code=409, detail="Room has been cancelled")
        if len(room["players"]) < 2:
            raise HTTPException(
                status_code=409,
                detail="Cannot become ready before an opponent joins",
            )
        if room["status"] in {"challenges_ready", "waiting_for_results", "results_ready"}:
            if bool(payload.ready) and player.get("ready") is True:
                room["lastActivity"] = current_time()
                return serialize_online_room(room, player)
            raise HTTPException(
                status_code=409,
                detail="Match has already advanced past briefing",
            )

        previous_ready = bool(player["ready"])
        player["ready"] = bool(payload.ready)
        room["status"] = (
            "waiting_for_challenges"
            if all(item["ready"] for item in room["players"])
            else "briefing"
        )
        room["lastActivity"] = current_time()

        if previous_ready != player["ready"]:
            append_online_match_event(
                room,
                "ready_changed",
                player_number=player["playerNumber"],
                ready=player["ready"],
            )

        return serialize_online_room(room, player)


@router.post("/online/rooms/{match_id}/draft")
def record_online_draft(
    match_id: str,
    payload: OnlineDraftRequest,
    request: Request,
):
    final_difficulty = str(payload.finalDifficulty or "").strip()
    final_layout = str(payload.finalLayout or "").strip()
    if final_difficulty not in {"Easy", "Medium", "Hard"}:
        raise HTTPException(status_code=400, detail="Invalid final difficulty")
    if final_layout not in {"Compact", "Balanced", "Open"}:
        raise HTTPException(status_code=400, detail="Invalid final layout")
    draft_metadata, metadata_complete = normalize_online_draft_metadata(payload)

    with _rooms_lock():
        room, player = require_online_room(
            match_id,
            request.headers.get("X-Player-Token"),
        )
        if room["status"] == "cancelled":
            raise HTTPException(status_code=409, detail="Room has been cancelled")
        if player.get("menuStartedFlow") is not True:
            return {
                "status": "ok",
                "recorded": False,
                "pending": False,
                "ignored": True,
                "event": None,
            }
        requested_draft = {
            "finalDifficulty": final_difficulty,
            "finalLayout": final_layout,
            **draft_metadata,
            "draftMetadataComplete": metadata_complete,
        }
        pending_draft = player.get("pendingDraft")
        if pending_draft is not None and pending_draft != requested_draft:
            raise HTTPException(
                status_code=409,
                detail="Draft settings are already pending",
            )

        if has_cocreation_first_stage(match_id, player["playerNumber"]):
            existing_draft = find_existing_online_draft_event(
                match_id, player["playerNumber"]
            )
            if existing_draft is not None:
                if not draft_events_match(
                    existing_draft,
                    final_difficulty,
                    final_layout,
                    draft_metadata,
                    metadata_complete,
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Draft record is already frozen",
                    )
                room["lastActivity"] = current_time()
                return {
                    "status": "ok",
                    "recorded": False,
                    "pending": False,
                    "event": existing_draft,
                }
            raise HTTPException(
                status_code=409,
                detail="Draft must be submitted before first_stage",
            )
        player["pendingDraft"] = requested_draft
        stored_event, created = None, False
        room["lastActivity"] = current_time()
        return {
            "status": "ok",
            "recorded": created,
            "pending": stored_event is None,
            "event": stored_event,
        }


@router.post("/online/rooms/{match_id}/challenge")
def submit_online_challenge(
    match_id: str,
    payload: OnlineChallengeRequest,
    request: Request,
):
    try:
        validate_online_challenge_rows(payload.rows)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    validate_online_ai_assistant_mode(payload.aiAssistantMode)

    submitted_rows = list(payload.rows)
    with _rooms_lock():
        room, player = require_online_room(
            match_id,
            request.headers.get("X-Player-Token"),
        )

        if room["status"] == "cancelled":
            raise HTTPException(status_code=409, detail="Room has been cancelled")

        if len(room["players"]) < 2 or not all(
            item["ready"] for item in room["players"]
        ):
            raise HTTPException(
                status_code=409,
                detail="Both players must be ready before submitting challenges",
            )

        existing_rows = player.get("challengeRows")
        if existing_rows is not None:
            if (
                existing_rows != submitted_rows
                or player.get("aiAssistantMode") != payload.aiAssistantMode
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Submitted challenge is already frozen",
                )

            room["lastActivity"] = current_time()
            return serialize_online_room(room, player)

        player["challengeRows"] = submitted_rows
        player["aiAssistantMode"] = payload.aiAssistantMode
        both_submitted = all(
            item.get("challengeRows") is not None for item in room["players"]
        )
        room["status"] = (
            "challenges_ready"
            if both_submitted
            else "waiting_for_challenges"
        )
        room["lastActivity"] = current_time()
        append_online_match_event(
            room,
            "challenge_submitted",
            player_number=player["playerNumber"],
            aiAssistantMode=payload.aiAssistantMode,
            rows=submitted_rows,
        )
        return serialize_online_room(room, player)


@router.post("/online/rooms/{match_id}/designer-intention")
def synchronize_online_designer_intention(
    match_id: str,
    payload: OnlineDesignerIntentionSyncRequest,
    request: Request,
):
    """Record the 8010 final intention without trusting a browser hand-off."""
    require_cocreation_intention_sync_secret(request)
    designer_intention = normalize_online_designer_intention(
        payload.designerIntention
    )

    if not designer_intention:
        raise HTTPException(
            status_code=400,
            detail="Final designer intention is required",
        )

    if payload.playerNumber not in (1, 2):
        raise HTTPException(status_code=400, detail="Invalid player number")

    with _rooms_lock():
        cleanup_expired_online_rooms()
        room = _rooms().get(str(match_id or "").strip())

        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")

        player = next(
            (
                item
                for item in room["players"]
                if item["playerNumber"] == payload.playerNumber
            ),
            None,
        )

        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        if player.get("menuStartedFlow") is not True:
            room["lastActivity"] = current_time()
            return {"status": "ok", "recorded": False, "ignored": True}

        if not has_cocreation_first_stage(match_id, payload.playerNumber):
            raise HTTPException(
                status_code=409,
                detail="Co-creation flow has not started",
            )

        existing_intention = str(player.get("designerIntention") or "").strip()

        if existing_intention and existing_intention != designer_intention:
            raise HTTPException(
                status_code=409,
                detail="Final designer intention is already frozen",
            )

        event_id = str(payload.eventId or "").strip()
        session_id = str(payload.sessionId or "").strip()
        room["lastActivity"] = current_time()

        if not existing_intention:
            player["designerIntention"] = designer_intention
            append_online_match_event(
                room,
                "designer_intention_synchronized",
                player_number=payload.playerNumber,
                designerIntention=designer_intention,
            )

        if event_id and session_id:
            append_cocreation_message(
                room,
                payload.playerNumber,
                event_id,
                session_id,
                designer_intention,
            )

        return serialize_online_room(room, player)


@router.post("/online/rooms/{match_id}/cocreation-events")
def synchronize_cocreation_flow_event(
    match_id: str,
    payload: OnlineCoCreationFlowEventRequest,
    request: Request,
):
    """Append one idempotent, player-scoped co-creation research event."""
    require_cocreation_intention_sync_secret(request)

    if payload.playerNumber not in (1, 2):
        raise HTTPException(status_code=400, detail="Invalid player number")

    with _rooms_lock():
        cleanup_expired_online_rooms()
        room = _rooms().get(str(match_id or "").strip())
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")

        player = next(
            (
                item for item in room["players"]
                if item["playerNumber"] == payload.playerNumber
            ),
            None,
        )
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")
        if player.get("menuStartedFlow") is not True:
            room["lastActivity"] = current_time()
            return {"status": "ok", "recorded": False, "ignored": True}

        if (
            payload.eventType != "first_stage"
            and not has_cocreation_first_stage(match_id, payload.playerNumber)
        ):
            raise HTTPException(
                status_code=409,
                detail="Co-creation flow has not started",
            )

        if payload.eventType == "first_stage":
            materialize_pending_draft(room, player)
        event = build_cocreation_flow_event(room, payload)
        room["lastActivity"] = current_time()
        stored_event, created = append_cocreation_flow_event(room, event)
        return {"status": "ok", "recorded": created, "event": stored_event}


@router.post("/online/rooms/{match_id}/result")
def submit_online_result(
    match_id: str,
    payload: OnlineResultRequest,
    request: Request,
):
    validate_online_result(payload)
    submitted_result = {
        "durationSeconds": round(float(payload.durationSeconds), 2),
        "moveCount": int(payload.moveCount),
        "minimumMoves": int(payload.minimumMoves),
        "restartCount": int(payload.restartCount),
        "outcome": payload.outcome,
    }

    with _rooms_lock():
        room, player = require_online_room(
            match_id,
            request.headers.get("X-Player-Token"),
        )

        if room["status"] == "cancelled":
            raise HTTPException(status_code=409, detail="Room has been cancelled")

        if len(room["players"]) < 2 or not all(
            item.get("challengeRows") is not None for item in room["players"]
        ):
            raise HTTPException(
                status_code=409,
                detail="Both challenges must be ready before submitting a result",
            )

        existing_result = player.get("result")

        if existing_result is not None:
            if existing_result != submitted_result:
                raise HTTPException(
                    status_code=409,
                    detail="Submitted result is already frozen",
                )

            room["lastActivity"] = current_time()
            return serialize_online_room(room, player)

        player["result"] = submitted_result
        both_finished = all(
            item.get("result") is not None for item in room["players"]
        )
        room["status"] = (
            "results_ready"
            if both_finished
            else "waiting_for_results"
        )
        room["lastActivity"] = current_time()
        append_online_match_event(
            room,
            "result_submitted",
            player_number=player["playerNumber"],
            **submitted_result,
        )
        return serialize_online_room(room, player)


@router.post("/online/rooms/{match_id}/leave")
def leave_online_room(match_id: str, request: Request):
    with _rooms_lock():
        room, player = require_online_room(
            match_id,
            request.headers.get("X-Player-Token"),
        )
        already_left = bool(player.get("left"))
        player["left"] = True
        room["lastActivity"] = current_time()

        if room["status"] == "results_ready" or player.get("result") is not None:
            return serialize_online_room(room, player)

        room["status"] = "cancelled"

        if not already_left:
            append_online_match_event(
                room,
                "player_left",
                player_number=player["playerNumber"],
            )

        return serialize_online_room(room, player)
