from fastapi import APIRouter, Depends, Request

from ..dependencies import AppContainer, get_container
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


router = APIRouter()


@router.post("/online/rooms")
def create_online_room(
    payload: OnlineRoomCreateRequest | None = None,
    container: AppContainer = Depends(get_container),
):
    return container.room_manager.create_room(payload)


@router.post("/online/rooms/join")
def join_online_room(
    payload: OnlineRoomJoinRequest,
    container: AppContainer = Depends(get_container),
):
    return container.room_manager.join_room(payload)


@router.get("/online/rooms/{match_id}")
def get_online_room(
    match_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    return container.room_manager.get_room(
        match_id,
        request.headers.get("X-Player-Token"),
    )


@router.post("/online/rooms/{match_id}/ready")
def set_online_ready(
    match_id: str,
    payload: OnlineReadyRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    return container.room_manager.set_ready(
        match_id,
        payload,
        request.headers.get("X-Player-Token"),
    )


@router.post("/online/rooms/{match_id}/draft")
def record_online_draft(
    match_id: str,
    payload: OnlineDraftRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    return container.room_manager.record_draft(
        match_id,
        payload,
        request.headers.get("X-Player-Token"),
    )


@router.post("/online/rooms/{match_id}/challenge")
def submit_online_challenge(
    match_id: str,
    payload: OnlineChallengeRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    return container.room_manager.submit_challenge(
        match_id,
        payload,
        request.headers.get("X-Player-Token"),
    )


@router.post("/online/rooms/{match_id}/designer-intention")
def synchronize_online_designer_intention(
    match_id: str,
    payload: OnlineDesignerIntentionSyncRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    """Record the 8010 final intention without trusting a browser hand-off."""
    return container.room_manager.synchronize_designer_intention(
        match_id,
        payload,
        request,
    )


@router.post("/online/rooms/{match_id}/cocreation-events")
def synchronize_cocreation_flow_event(
    match_id: str,
    payload: OnlineCoCreationFlowEventRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    """Append one idempotent, player-scoped co-creation research event."""
    return container.room_manager.synchronize_cocreation_flow_event(
        match_id,
        payload,
        request,
    )


@router.post("/online/rooms/{match_id}/result")
def submit_online_result(
    match_id: str,
    payload: OnlineResultRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    return container.room_manager.submit_result(
        match_id,
        payload,
        request.headers.get("X-Player-Token"),
    )


@router.post("/online/rooms/{match_id}/leave")
def leave_online_room(
    match_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    return container.room_manager.leave_room(
        match_id,
        request.headers.get("X-Player-Token"),
    )
