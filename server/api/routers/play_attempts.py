"""Thin async routes for Stage play-attempt bootstrap and telemetry lifecycle."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from server.api.deps import ContainerDep, rewrite_loopback_url
from server.api.schemas import PlayBootstrapRequest, PlayMetricsRequest
from server.settings import DEFAULT_PUBLIC_BASE_URL

router = APIRouter()


def _metrics_kwargs(payload: PlayMetricsRequest) -> dict[str, Any]:
    return {
        "duration_seconds": payload.durationSeconds,
        "move_count": payload.moveCount,
        "push_count": payload.pushCount,
        "restart_count": payload.restartCount,
        "minimum_moves": payload.minimumMoves,
        "minimum_pushes": payload.minimumPushes,
    }


@router.post("/play-attempts/{attempt_id}/bootstrap")
async def bootstrap_play_attempt(
    attempt_id: str,
    payload: PlayBootstrapRequest,
    request: Request,
    container: ContainerDep,
) -> dict[str, Any]:
    result = await container.stage_service.bootstrap_play_attempt(attempt_id, payload.ticket)
    if "returnUrl" in result and isinstance(result["returnUrl"], str):
        result["returnUrl"] = rewrite_loopback_url(
            result["returnUrl"],
            request,
            configured_base_url=container.settings.public_base_url,
            default_base_url=DEFAULT_PUBLIC_BASE_URL,
        )
    return result


@router.post("/play-attempts/{attempt_id}/start")
async def start_play_attempt(
    attempt_id: str, payload: PlayMetricsRequest, container: ContainerDep
) -> dict[str, Any]:
    return await container.stage_service.start_play_attempt(
        attempt_id, payload.attemptToken, **_metrics_kwargs(payload)
    )


@router.post("/play-attempts/{attempt_id}/progress")
async def progress_play_attempt(
    attempt_id: str, payload: PlayMetricsRequest, container: ContainerDep
) -> dict[str, Any]:
    return await container.stage_service.progress_play_attempt(
        attempt_id, payload.attemptToken, **_metrics_kwargs(payload)
    )


@router.post("/play-attempts/{attempt_id}/complete")
async def complete_play_attempt(
    attempt_id: str, payload: PlayMetricsRequest, container: ContainerDep
) -> dict[str, Any]:
    return await container.stage_service.complete_play_attempt(
        attempt_id, payload.attemptToken, **_metrics_kwargs(payload)
    )


@router.post("/play-attempts/{attempt_id}/abandon")
async def abandon_play_attempt(
    attempt_id: str, payload: PlayMetricsRequest, container: ContainerDep
) -> dict[str, Any]:
    return await container.stage_service.abandon_play_attempt(
        attempt_id, payload.attemptToken, **_metrics_kwargs(payload)
    )
