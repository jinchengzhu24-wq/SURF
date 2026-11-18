import mimetypes
from pathlib import Path
import sys
import threading
import time
import webbrowser

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sokoban_backend.config import (  # noqa: E402, F401
    AGENT_HANDOFF_SCHEMA_VERSION,
    BASE_DIR,
    CREATIVE_EXPANSION_CHOICE_LOG_FILE,
    CREATIVE_IDEA_LOG_FILE,
    DEFAULT_LLM_MAX_ATTEMPTS,
    FRONTEND_DIR,
    HA_PLAN_EVENT_LOG_FILE,
    HOST,
    JOURNEY_EVENT_LOG_FILE,
    ONLINE_AI_ASSISTANT_MODES,
    ONLINE_MATCH_FLOW_DIR,
    ONLINE_MATCH_LOG_FILE,
    ONLINE_ROOM_TTL_SECONDS,
    PC_DESIGN_MIN_ACTIVITY_AREA,
    PC_FALLBACK_MIN_INTERNAL_WALLS,
    PC_FEASIBILITY_MAX_SEARCH_STATES,
    PC_INTERMEDIATE_INTERNAL_WALLS,
    PC_LAYOUT_MAX_WATER_CHECKS,
    PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES,
    PC_LAYOUT_PREFILTER_SECONDS,
    PC_LEVEL_LLM_TIMEOUT_SECONDS,
    PC_MAX_LAYOUT_CANDIDATES,
    PC_MAX_WATER_AREA_CANDIDATES,
    PC_PRIMARY_INTERNAL_WALLS,
    PLAN_LLM_TIMEOUT_SECONDS,
    PORT,
    PROJECT_DIR,
    SHORT_LLM_TIMEOUT_SECONDS,
    START_URL,
    STUDY_LOG_DIR,
    STUDY_LOG_FILE,
    SURVEY_LOG_FILE,
    WEBGL_BUILD_DIR,
    env_float,
    safe_compare_digest,
    verify_delete_password_header,
)
from sokoban_backend.dependencies import (  # noqa: E402, F401
    AppContainer,
    BackendSettings,
    BlueprintPlannerService,
    Clock,
    DGGuideService,
    EventLogger,
    LLMGateway,
    OnlineRoomManager,
    PCLevelGenerationService,
    StudyLogPaths,
    StudyLogStore,
    build_container,
    get_container,
    require_delete_password,
)
from sokoban_backend.llm.prompts import (  # noqa: E402, F401
    build_creative_idea_expansion_messages,
    build_dg_context_prompt,
    build_dg_guide_summary_messages,
    build_feature_constraint_prompt,
    build_generation_preferences_prompt,
    build_ha_revision_plan_edit_messages,
    build_ha_revision_plan_messages,
    build_human_adjustment_clarity_messages,
    build_level_plan_messages,
    build_pc_level_generation_messages,
    build_user_constraint_snippets,
    resolve_zero_feature_constraints,
)
from sokoban_backend.llm.runtime import (  # noqa: E402, F401
    LLMExecutionResult,
    LLMServiceError,
    classify_llm_exception,
    new_request_id,
    redact_log_text,
)
from sokoban_backend.routes import dashboard, generation, online  # noqa: E402
from sokoban_backend.schemas import (  # noqa: E402, F401
    CreativeIdeaExpansionOption,
    CreativeIdeaExpansionRequest,
    DEFAULT_PLAN,
    DGGuideSummaryRequest,
    DeleteCreativeIdeaRequest,
    DeleteExpansionChoiceRequest,
    DeleteHAPlanEventRequest,
    DeleteIdeaRecordsRequest,
    DeleteJourneyEventRequest,
    DeleteLevelRunRequest,
    DeleteOnlineMatchRequest,
    DeleteSurveyResponseRequest,
    ENUMS,
    GenerationPreferences,
    HA_CHANGE_FIELDS,
    HARevisionPlanEditRequest,
    HARevisionPlanRequest,
    HumanAdjustmentValidationRequest,
    LIMITS,
    LevelDesignPlan,
    LevelPlanRequest,
    ONLINE_DRAFT_ANSWER_CHOICES,
    ONLINE_DRAFT_METADATA_KEYS,
    ONLINE_DRAFT_RECOMMENDATION_SOURCES,
    OnlineChallengeRequest,
    OnlineCoCreationFlowEventRequest,
    OnlineDesignerIntentionSyncRequest,
    OnlineDraftRequest,
    OnlineReadyRequest,
    OnlineResultRequest,
    OnlineRoomCreateRequest,
    OnlineRoomJoinRequest,
    PCLevelGenerationRequest,
    PCSolvabilityError,
    RECENT_BLUEPRINT_LIMIT,
    RenameLevelRunRequest,
    RenameRoundRequest,
    RoundRequest,
)
from sokoban_backend.services.blueprint_planner import (  # noqa: E402, F401
    apply_selected_ha_plan,
    build_contextual_expansion_fallback_options,
    clean_expansion_text,
    get_blueprint_key,
    normalize_generation_preferences,
    parse_ha_revision_contract,
    validate_client_level_plan_inputs,
    validate_creative_idea_expansion,
    validate_ha_revision_contract,
    validate_ha_revision_plan_edit,
    validate_ha_revision_plan_options,
    validate_human_adjustment_clarity_payload,
    validate_plan,
)
from sokoban_backend.services.dashboard_projections import (  # noqa: E402, F401
    aggregate_dashboard_node_events,
    build_creative_ideas_payload,
    build_expansion_choices_payload,
    build_ha_plan_events_payload,
    build_journey_events_payload,
    build_level_records_payload,
    build_matchmaking_records_payload,
    build_survey_records_payload,
    filter_frontend_ha_plan_records,
    filter_frontend_records,
    merge_level_records,
    render_level_records_view,
)
from sokoban_backend.services.dg_guide import (  # noqa: E402, F401
    DG_DIFFICULTY_ANSWERS,
    DG_LAYOUT_ANSWERS,
    build_dg_fallback_summary,
    dg_choice_phrase,
    dg_preference_allowed_scores,
    dg_preference_score,
    normalize_dg_rationale,
    normalize_dg_reflection,
)
from sokoban_backend.services.online_rooms import (  # noqa: E402, F401
    build_cocreation_flow_event,
    normalize_online_designer_intention,
    normalize_online_room_code,
    normalize_study_session_id,
    serialize_online_challenge_metadata,
    serialize_online_room,
    validate_online_challenge_rows,
    validate_online_result,
)
from sokoban_backend.services.pc_generator import (  # noqa: E402, F401
    build_pc_level_candidate,
    build_pc_solvability_rejection_feedback,
    contains_pc_two_by_two_block,
    diagnose_pc_static_solvability,
    filter_pc_static_wall_coordinates,
    find_pc_enclosed_cells,
    normalize_pc_level_request,
    parse_pc_layout_candidate_selection,
    search_pc_minimum_pushes,
    validate_pc_completed_level_solvability,
    validate_pc_level_candidate,
    validate_pc_open_sketch_feasibility,
    validate_pc_required_features,
    validate_pc_water_rectangles,
)


mimetypes.add_type("application/octet-stream", ".data")
mimetypes.add_type("application/wasm", ".wasm")


def create_app(container: AppContainer | None = None) -> FastAPI:
    resolved_container = container or build_container()
    application = FastAPI(title="Sokoban AI Level Generator Backend")
    application.state.container = resolved_container
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_container.settings.allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-LLM-Attempts-Used"],
    )

    @application.middleware("http")
    async def attach_request_context(request: Request, call_next):
        request_id = (
            str(request.headers.get("X-Request-ID") or "").strip()
            or new_request_id()
        )
        request.state.request_id = request_id
        request.state.container = resolved_container
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request_id)
        return response

    @application.exception_handler(LLMServiceError)
    async def handle_llm_service_error(request: Request, exc: LLMServiceError):
        request_id = (
            getattr(request.state, "request_id", None)
            or exc.request_id
            or new_request_id()
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.to_detail()},
            headers={
                "X-Request-ID": request_id,
                "X-LLM-Attempts-Used": str(exc.attempts_used),
            },
        )

    application.include_router(online.router)
    application.include_router(generation.router)
    application.include_router(dashboard.router)

    if FRONTEND_DIR.exists():
        application.mount(
            "/frontend",
            StaticFiles(directory=str(FRONTEND_DIR), html=True),
            name="frontend",
        )

    if WEBGL_BUILD_DIR.exists():
        application.mount(
            "/game",
            StaticFiles(directory=str(WEBGL_BUILD_DIR), html=True),
            name="game",
        )

    return application


app = create_app()


def open_browser():
    time.sleep(1.0)
    webbrowser.open(START_URL)


if __name__ == "__main__":
    if "--open-browser" in sys.argv:
        threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT)
