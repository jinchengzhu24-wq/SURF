"""Thin async routes for DG guide summary, level blueprint planning, and study records."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from server.api.deps import ContainerDep, get_request_id
from server.api.schemas import DGGuideSummaryRequest, LevelPlanRequest
from server.domain.errors import ValidationError

router = APIRouter()


async def _read_json_mapping(request: Request) -> dict[str, Any]:
    try:
        parsed = await request.json()
    except Exception as exc:
        raise ValidationError(
            "INVALID_STUDY_PAYLOAD",
            "error.validation_failed",
            message="Request body must be valid JSON.",
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationError(
            "INVALID_STUDY_PAYLOAD",
            "error.validation_failed",
            message="Study record payload must be a JSON object.",
        )
    return parsed


@router.post("/generate-level-plan")
async def generate_level_plan(
    payload: LevelPlanRequest,
    request: Request,
    response: Response,
    container: ContainerDep,
) -> dict[str, Any]:
    req_id = get_request_id(request)
    deadline = container.clock.monotonic() + container.settings.level_plan_timeout_seconds
    result = await container.level_plan_service.create_level_plan(
        payload.model_dump(exclude={"maxAttempts"}),
        request_id=req_id,
        max_attempts=payload.maxAttempts,
        deadline=deadline,
    )
    response.headers["X-Request-ID"] = result.request_id or req_id
    response.headers["X-LLM-Attempts-Used"] = str(result.attempts_used)
    return result.plan


@router.post("/dg/guide/summary")
async def dg_guide_summary(
    payload: DGGuideSummaryRequest,
    request: Request,
    response: Response,
    container: ContainerDep,
) -> dict[str, Any]:
    req_id = get_request_id(request)
    deadline = container.clock.monotonic() + container.settings.dg_timeout_seconds
    result = await container.dg_guide_service.create_summary(
        payload.model_dump(),
        request_id=req_id,
        deadline=deadline,
    )
    response.headers["X-Request-ID"] = result.request_id or req_id
    response.headers["X-DG-Guide-Source"] = result.source
    response.headers["X-LLM-Attempts-Used"] = str(result.attempts_used)
    return result.value


@router.post("/record-survey-response")
async def record_survey_response(
    request: Request, container: ContainerDep
) -> dict[str, Any]:
    return await container.study_records_service.record_survey(await _read_json_mapping(request))


@router.post("/record-level-start")
async def record_level_start(
    request: Request, container: ContainerDep
) -> dict[str, Any]:
    return await container.study_records_service.record_level_start(
        await _read_json_mapping(request)
    )


@router.post("/record-level-end")
async def record_level_end(
    request: Request, container: ContainerDep
) -> dict[str, Any]:
    return await container.study_records_service.record_level_end(
        await _read_json_mapping(request)
    )


@router.post("/record-journey-event")
async def record_journey_event(
    request: Request, container: ContainerDep
) -> dict[str, Any]:
    return await container.study_records_service.record_journey(
        await _read_json_mapping(request)
    )
