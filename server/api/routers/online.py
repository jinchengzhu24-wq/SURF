"""Thin async routes for 2-player online matchmaking rooms."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from server.api.deps import ContainerDep, PlayerTokenHeader
from server.api.schemas import (
    OnlineChallengeRequest,
    OnlineDraftRequest,
    OnlineReadyRequest,
    OnlineResultRequest,
    OnlineRoomCreateRequest,
    OnlineRoomJoinRequest,
)

router = APIRouter()


@router.post("/online/rooms")
async def create_online_room(
    container: ContainerDep,
    payload: OnlineRoomCreateRequest | None = None,
) -> dict[str, Any]:
    req = payload or OnlineRoomCreateRequest()
    return await container.online_room_service.create_room(
        study_session_id=req.studySessionId or "",
        menu_started_flow=req.menuStartedFlow,
    )


@router.post("/online/rooms/join")
async def join_online_room(
    payload: OnlineRoomJoinRequest,
    container: ContainerDep,
) -> dict[str, Any]:
    return await container.online_room_service.join_room(
        room_code=payload.roomCode or "",
        study_session_id=payload.studySessionId or "",
        menu_started_flow=payload.menuStartedFlow,
    )


@router.get("/online/rooms/{match_id}")
async def get_online_room(
    match_id: str,
    container: ContainerDep,
    x_player_token: PlayerTokenHeader = None,
) -> dict[str, Any]:
    return await container.online_room_service.get_room(match_id, x_player_token)


@router.post("/online/rooms/{match_id}/ready")
async def set_online_ready(
    match_id: str,
    payload: OnlineReadyRequest,
    container: ContainerDep,
    x_player_token: PlayerTokenHeader = None,
) -> dict[str, Any]:
    return await container.online_room_service.set_ready(
        match_id, x_player_token, ready=payload.ready
    )


@router.post("/online/rooms/{match_id}/leave")
async def leave_online_room(
    match_id: str,
    container: ContainerDep,
    x_player_token: PlayerTokenHeader = None,
) -> dict[str, Any]:
    return await container.online_room_service.leave_room(match_id, x_player_token)


@router.post("/online/rooms/{match_id}/draft")
async def record_online_draft(
    match_id: str,
    payload: OnlineDraftRequest,
    container: ContainerDep,
    x_player_token: PlayerTokenHeader = None,
) -> dict[str, Any]:
    return await container.online_room_service.record_draft(
        match_id, x_player_token, payload.model_dump()
    )


@router.post("/online/rooms/{match_id}/challenge")
async def submit_online_challenge(
    match_id: str,
    payload: OnlineChallengeRequest,
    container: ContainerDep,
    x_player_token: PlayerTokenHeader = None,
) -> dict[str, Any]:
    return await container.online_room_service.submit_challenge(
        match_id,
        x_player_token,
        rows=payload.rows,
        ai_assistant_mode=payload.aiAssistantMode,
    )


@router.post("/online/rooms/{match_id}/result")
async def submit_online_result(
    match_id: str,
    payload: OnlineResultRequest,
    container: ContainerDep,
    x_player_token: PlayerTokenHeader = None,
) -> dict[str, Any]:
    return await container.online_room_service.submit_result(
        match_id, x_player_token, payload.model_dump()
    )
