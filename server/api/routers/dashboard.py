"""Thin async routes for Matchmaking Dashboard, Decision 5B deletes, and JSONL export."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, StreamingResponse

from server.api.deps import ContainerDep, DeletePasswordHeader
from server.api.schemas import DeleteOnlineMatchRequest
from server.persistence.database import dump_json

router = APIRouter()


@router.post("/verify-dashboard-password")
async def verify_dashboard_password(
    container: ContainerDep,
    x_delete_password: DeletePasswordHeader = None,
) -> dict[str, bool]:
    container.dashboard_service.verify_delete_password(x_delete_password)
    return {"ok": True}


@router.get("/matchmaking-records-data")
async def get_matchmaking_records_data(container: ContainerDep) -> dict[str, Any]:
    return await container.dashboard_service.get_matchmaking_records()


@router.get("/matchmaking-records", response_class=PlainTextResponse)
async def get_matchmaking_records(container: ContainerDep) -> PlainTextResponse:
    events = await asyncio.to_thread(container.rooms_repo.list_all_match_events)
    body = "\n".join(dump_json(ev.payload) for ev in events) + ("\n" if events else "")
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@router.post("/delete-online-match")
async def delete_online_match(
    payload: DeleteOnlineMatchRequest,
    container: ContainerDep,
    x_delete_password: DeletePasswordHeader = None,
) -> dict[str, Any]:
    return await container.dashboard_service.delete_online_match(
        payload.matchId,
        supplied_password=x_delete_password,
    )


@router.post("/clear-matchmaking-records")
async def clear_matchmaking_records(
    container: ContainerDep,
    x_delete_password: DeletePasswordHeader = None,
) -> dict[str, Any]:
    return await container.dashboard_service.clear_matchmaking_records(
        supplied_password=x_delete_password,
    )


@router.get("/export/study-logs.jsonl")
async def export_study_logs(
    container: ContainerDep,
    record_type: str | None = None,
) -> StreamingResponse:
    return StreamingResponse(
        container.study_records_service.export_jsonl(record_type=record_type),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="study-logs.jsonl"'},
    )
