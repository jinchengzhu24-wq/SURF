from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..config import AGENT_HANDOFF_SCHEMA_VERSION, DEFAULT_LLM_MAX_ATTEMPTS
from ..dependencies import AppContainer, EventLogger, get_container
from ..llm.runtime import LLMServiceError
from ..schemas import (
    CreativeIdeaExpansionRequest,
    DGGuideSummaryRequest,
    HARevisionPlanEditRequest,
    HARevisionPlanRequest,
    HumanAdjustmentValidationRequest,
    LevelPlanRequest,
    PCLevelGenerationRequest,
)
from ..services.blueprint_planner import (
    normalize_generation_preferences,
    validate_client_level_plan_inputs,
    validate_ha_revision_contract,
    validate_human_adjustment_clarity_payload,
    validate_plan,
)


router = APIRouter()


def apply_llm_execution_headers(response, execution):
    response.headers["X-Request-ID"] = execution.request_id
    response.headers["X-LLM-Attempts-Used"] = str(execution.attempts_used)


def log_agent_handoff(
    event_logger: EventLogger,
    from_agent,
    to_agent,
    artifact_type,
    artifact,
    evidence=None,
    status="proposed",
    request_id="",
):
    """Record the 8000-side structured handoff in the existing runtime log."""
    event_logger.log_event(
        "INFO",
        "agent_handoff",
        requestId=request_id,
        schemaVersion=AGENT_HANDOFF_SCHEMA_VERSION,
        fromAgent=from_agent,
        toAgent=to_agent,
        artifactType=artifact_type,
        artifact=artifact or {},
        evidence=evidence or [],
        status=status,
    )


@router.post("/expand-creative-idea")
def expand_creative_idea(
    payload: CreativeIdeaExpansionRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    data = payload.model_dump()
    idea_text = str(data.get("ideaText") or "").strip()

    if not idea_text:
        raise HTTPException(status_code=400, detail="ideaText is required")

    data["ideaText"] = idea_text
    execution = container.blueprint_service.create_creative_idea_expansion(
        data,
        request.state.request_id,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


def _validate_previous_plan_or_400(previous_plan):
    try:
        validate_plan(
            previous_plan,
            {
                "noWater": previous_plan.get("minWaterAreas") == 0
                and previous_plan.get("maxWaterAreas") == 0,
                "noInternalWalls": previous_plan.get("minWallObstacleBlocks") == 0
                and previous_plan.get("maxWallObstacleBlocks") == 0,
            },
        )
    except (TypeError, ValueError) as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception


@router.post("/generate-ha-revision-plans")
def generate_ha_revision_plans(
    payload: HARevisionPlanRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    data = payload.model_dump()
    adjustment_text = str(data.get("adjustmentText") or "").strip()
    previous_plan = data.get("previousLevelPlan")

    if not adjustment_text:
        raise HTTPException(status_code=400, detail="adjustmentText is required")

    if not isinstance(previous_plan, dict) or not previous_plan:
        raise HTTPException(status_code=400, detail="previousLevelPlan is required")

    _validate_previous_plan_or_400(previous_plan)

    data["adjustmentText"] = adjustment_text

    try:
        execution = container.blueprint_service.create_ha_revision_plans(
            data,
            request.state.request_id,
        )
        container.store.append_ha_generation_event(data, execution.value["options"])
        apply_llm_execution_headers(response, execution)
        return execution.value
    except LLMServiceError as exception:
        container.store.append_ha_generation_event(
            data,
            [],
            error=exception.safe_message,
        )
        raise


@router.post("/revise-ha-revision-plan")
def revise_ha_revision_plan(
    payload: HARevisionPlanEditRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    data = payload.model_dump()
    edited_description = str(data.get("editedDescription") or "").strip()
    previous_plan = data.get("previousLevelPlan")
    original_option = data.get("originalOption")

    if not edited_description:
        raise HTTPException(status_code=400, detail="editedDescription is required")

    if not isinstance(previous_plan, dict) or not previous_plan:
        raise HTTPException(status_code=400, detail="previousLevelPlan is required")

    _validate_previous_plan_or_400(previous_plan)

    if not isinstance(original_option, dict) or not original_option:
        raise HTTPException(status_code=400, detail="originalOption is required")

    if not str(original_option.get("id") or "").strip():
        raise HTTPException(status_code=400, detail="original HA option id is required")

    if not str(original_option.get("title") or "").strip():
        raise HTTPException(status_code=400, detail="original HA option title is required")

    try:
        validate_ha_revision_contract(
            original_option.get("promptText"),
            previous_plan,
            data.get("adjustmentText"),
        )
    except (TypeError, ValueError) as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception

    data["editedDescription"] = edited_description[:420]
    execution = container.blueprint_service.create_ha_revision_plan_edit(
        data,
        request.state.request_id,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


@router.get("/validate-human-adjustment")
def validate_human_adjustment_legacy(
    request: Request,
    response: Response,
    adjustmentText: str = "",
    container: AppContainer = Depends(get_container),
):
    adjustment_text = str(adjustmentText or "").strip()

    if not adjustment_text:
        return validate_human_adjustment_clarity_payload({})

    execution = container.blueprint_service.create_human_adjustment_clarity_check(
        adjustment_text,
        request.state.request_id,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


@router.post("/validate-human-adjustment")
def validate_human_adjustment(
    payload: HumanAdjustmentValidationRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    adjustment_text = str(payload.adjustmentText or "").strip()

    if not adjustment_text:
        raise HTTPException(status_code=400, detail="adjustmentText is required")

    execution = container.blueprint_service.create_human_adjustment_clarity_check(
        adjustment_text,
        request.state.request_id,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


@router.post("/dg/guide/summary")
def dg_guide_summary(
    payload: DGGuideSummaryRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    context = {
        "firstMovePreference": payload.firstMovePreference.strip(),
        "pushPlanningPreference": payload.pushPlanningPreference.strip(),
        "spacePreference": payload.spacePreference.strip(),
        "routeRhythmPreference": payload.routeRhythmPreference.strip(),
    }
    if (
        context["firstMovePreference"] not in {"quick_start", "observe_then_decide", "plan_ahead", "no_preference"}
        or context["pushPlanningPreference"] not in {"easy_to_adjust", "consider_order", "connected_pushes", "no_preference"}
        or context["spacePreference"] not in {"focused_area", "connected_areas", "wide_area", "no_preference"}
        or context["routeRhythmPreference"] not in {"short_routes", "occasional_detours", "long_routes", "no_preference"}
    ):
        raise HTTPException(status_code=400, detail="Invalid DG neutral answer.")
    result = container.dg_service.create_summary(context, request.state.request_id)
    log_agent_handoff(
        container.event_logger,
        "draft_understanding",
        "blueprint_planning",
        "dgContext",
        {
            "dgContext": context,
            "guideSummary": result,
        },
        evidence=[
            {"type": "dg_answers", "fields": list(context.keys())},
            {"type": "summary_source", "source": result.get("source", "")},
        ],
        status="confirmed",
        request_id=request.state.request_id,
    )
    response.headers["X-DG-Guide-Source"] = result.get("source", "")
    return result


@router.post("/generate-pc-level")
def generate_pc_level(
    payload: PCLevelGenerationRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    data = payload.model_dump(exclude={"maxAttempts"})

    try:
        data = container.pc_service.normalize_request(data)
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception

    max_attempts = max(
        1,
        min(
            container.settings.llm_max_attempts,
            int(payload.maxAttempts or 1),
        ),
    )
    execution = container.pc_service.create_candidate(
        data,
        request.state.request_id,
        max_attempts,
        context_is_normalized=True,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


@router.get("/generate-level-plan")
def generate_level_plan_legacy(
    request: Request,
    response: Response,
    ideaText: str = "",
    ideaId: str = "",
    sessionId: str = "",
    sceneName: str = "",
    originalIdeaText: str = "",
    selectedDirectionText: str = "",
    refinementFeedbackText: str = "",
    adjustmentHistoryText: str = "",
    latestAdjustmentText: str = "",
    revisionMode: str = "",
    previousLevelPlan: str = "",
    previousLevelMetrics: str = "",
    selectedHAPlan: str = "",
    maxAttempts: int = DEFAULT_LLM_MAX_ATTEMPTS,
    container: AppContainer = Depends(get_container),
):
    payload = LevelPlanRequest(
        ideaText=ideaText,
        ideaId=ideaId,
        sessionId=sessionId,
        sceneName=sceneName,
        originalIdeaText=originalIdeaText,
        selectedDirectionText=selectedDirectionText,
        refinementFeedbackText=refinementFeedbackText,
        adjustmentHistoryText=adjustmentHistoryText,
        latestAdjustmentText=latestAdjustmentText,
        revisionMode=revisionMode,
        previousLevelPlan=previousLevelPlan,
        previousLevelMetrics=previousLevelMetrics,
        selectedHAPlan=selectedHAPlan,
        maxAttempts=maxAttempts,
    )
    return execute_level_plan_request(payload, request, response, container)


@router.post("/generate-level-plan")
def generate_level_plan(
    payload: LevelPlanRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    return execute_level_plan_request(payload, request, response, container)


def execute_level_plan_request(
    payload: LevelPlanRequest,
    request: Request,
    response: Response,
    container: AppContainer,
):
    data = payload.model_dump(exclude={"maxAttempts"})

    try:
        data["generationPreferences"] = normalize_generation_preferences(
            data.get("generationPreferences")
        )
        validate_client_level_plan_inputs(data)
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception

    max_attempts = max(
        1,
        min(
            container.settings.llm_max_attempts,
            int(payload.maxAttempts or 1),
        ),
    )
    execution = container.blueprint_service.create_level_plan(
        data,
        request.state.request_id,
        max_attempts,
    )
    log_agent_handoff(
        container.event_logger,
        "blueprint_planning",
        "unity_generator",
        "LevelDesignPlan",
        {"levelDesignPlan": execution.value},
        evidence=[
            {"type": "blueprint_validation", "status": "passed"},
            {
                "type": "dgContext",
                "present": bool(data.get("dgContext")),
            },
        ],
        status="confirmed",
        request_id=request.state.request_id,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value
