import html
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid
import webbrowser
from collections import deque
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if __package__:
    from .llm_runtime import (
        LLMExecutionResult,
        LLMServiceError,
        execute_json_request,
        log_event,
        new_request_id,
        readiness_payload,
        safe_log_text,
    )
    from .prompt import (
        build_creative_idea_expansion_messages,
        build_dg_guide_summary_messages,
        build_ha_revision_plan_edit_messages,
        build_ha_revision_plan_messages,
        build_human_adjustment_clarity_messages,
        build_level_plan_messages,
        build_pc_level_generation_messages,
        resolve_zero_feature_constraints,
    )
else:
    from llm_runtime import (
        LLMExecutionResult,
        LLMServiceError,
        execute_json_request,
        log_event,
        new_request_id,
        readiness_payload,
        safe_log_text,
    )
    from prompt import (
        build_creative_idea_expansion_messages,
        build_dg_guide_summary_messages,
        build_ha_revision_plan_edit_messages,
        build_ha_revision_plan_messages,
        build_human_adjustment_clarity_messages,
        build_level_plan_messages,
        build_pc_level_generation_messages,
        resolve_zero_feature_constraints,
    )

HOST = "127.0.0.1"
PORT = 8000
START_URL = f"http://{HOST}:{PORT}/generate-level-plan"
SHORT_LLM_TIMEOUT_SECONDS = 25.0
PLAN_LLM_TIMEOUT_SECONDS = 45.0
PC_LEVEL_LLM_TIMEOUT_SECONDS = 15.0
PC_FEASIBILITY_MAX_SEARCH_STATES = 120000
PC_DESIGN_MIN_ACTIVITY_AREA = 56
PC_PRIMARY_INTERNAL_WALLS = 5
PC_INTERMEDIATE_INTERNAL_WALLS = 4
PC_FALLBACK_MIN_INTERNAL_WALLS = 3
PC_MAX_WATER_AREA_CANDIDATES = 12
PC_LAYOUT_MAX_WATER_CHECKS = 6
PC_MAX_LAYOUT_CANDIDATES = 6
PC_LAYOUT_PREFILTER_SECONDS = 3.0
PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES = 300000
DEFAULT_LLM_MAX_ATTEMPTS = 2
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "Frontend"
WEBGL_BUILD_DIR = PROJECT_DIR / "WebGLBuild"
STUDY_LOG_DIR = BASE_DIR / "study_logs"
STUDY_LOG_FILE = STUDY_LOG_DIR / "level_records.jsonl"
SURVEY_LOG_FILE = STUDY_LOG_DIR / "survey_responses.jsonl"
CREATIVE_IDEA_LOG_FILE = STUDY_LOG_DIR / "creative_ideas.jsonl"
CREATIVE_EXPANSION_CHOICE_LOG_FILE = STUDY_LOG_DIR / "creative_expansion_choices.jsonl"
HA_PLAN_EVENT_LOG_FILE = STUDY_LOG_DIR / "ha_plan_events.jsonl"
JOURNEY_EVENT_LOG_FILE = STUDY_LOG_DIR / "journey_events.jsonl"
ONLINE_MATCH_LOG_FILE = STUDY_LOG_DIR / "online_match_events.jsonl"
ONLINE_ROOM_TTL_SECONDS = 30 * 60
ONLINE_ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ONLINE_ROOM_CODE_PATTERN = re.compile(r"^[A-Z0-9]{6}$")
ONLINE_AI_ASSISTANT_MODES = {
    "description_generation",
}
ONLINE_ROOMS = {}
ONLINE_ROOMS_LOCK = threading.Lock()

class PCSolvabilityError(ValueError):
    def __init__(self, reason_code, message, searched_states=0, details=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.searched_states = int(searched_states or 0)
        self.details = dict(details or {})


class OnlineRoomJoinRequest(BaseModel):
    roomCode: str | None = ""


class OnlineReadyRequest(BaseModel):
    ready: bool = True


class OnlineChallengeRequest(BaseModel):
    rows: list[str]
    aiAssistantMode: str = "description_generation"


class OnlineDesignerIntentionSyncRequest(BaseModel):
    playerNumber: int
    designerIntention: str


class OnlineResultRequest(BaseModel):
    durationSeconds: float
    moveCount: int
    minimumMoves: int
    outcome: str = "completed"


load_dotenv(BASE_DIR / ".env")
COCREATION_INTENTION_SYNC_SECRET = os.environ.get(
    "COCREATION_INTENTION_SYNC_SECRET",
    "",
).strip()


def require_delete_password(request: Request):
    configured_password = os.environ.get("DASHBOARD_DELETE_PASSWORD", "").strip()

    if not configured_password:
        raise HTTPException(
            status_code=503,
            detail="Delete password is not configured",
        )

    supplied_password = request.headers.get("X-Delete-Password", "")

    if not secrets.compare_digest(supplied_password, configured_password):
        raise HTTPException(status_code=401, detail="Incorrect delete password")


def normalize_online_room_code(room_code):
    normalized = str(room_code or "").strip().upper()

    if not ONLINE_ROOM_CODE_PATTERN.fullmatch(normalized):
        raise HTTPException(
            status_code=400,
            detail="Room code must contain exactly 6 letters or digits",
        )

    return normalized


def cleanup_expired_online_rooms(now=None):
    current_time = time.time() if now is None else float(now)
    expired_match_ids = [
        match_id
        for match_id, room in ONLINE_ROOMS.items()
        if current_time - room["lastActivity"] >= ONLINE_ROOM_TTL_SECONDS
    ]

    for match_id in expired_match_ids:
        append_online_match_event(
            ONLINE_ROOMS[match_id],
            "room_expired",
            status_after="expired",
        )
        del ONLINE_ROOMS[match_id]


def create_unique_online_room_code():
    for _ in range(100):
        room_code = "".join(
            secrets.choice(ONLINE_ROOM_CODE_ALPHABET) for _ in range(6)
        )

        if not any(
            room["roomCode"] == room_code for room in ONLINE_ROOMS.values()
        ):
            return room_code

    raise HTTPException(
        status_code=503,
        detail="Unable to allocate a room code",
    )


def append_online_match_event(
    room,
    event_type,
    player_number=0,
    status_after=None,
    **details,
):
    event = {
        "eventId": uuid.uuid4().hex,
        "eventType": str(event_type or "").strip(),
        "matchId": str(room.get("matchId") or "").strip(),
        "roomCode": str(room.get("roomCode") or "").strip(),
        "playerNumber": int(player_number or 0),
        "statusAfter": str(
            status_after
            if status_after is not None
            else room.get("status") or ""
        ).strip(),
        "serverReceivedAt": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
    }
    event.update(details)
    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        with ONLINE_MATCH_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(event, ensure_ascii=False))
            log_file.write("\n")

    return event


def require_online_room(match_id, player_token):
    cleanup_expired_online_rooms()
    room = ONLINE_ROOMS.get(str(match_id or "").strip())

    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    supplied_token = str(player_token or "").strip()
    player = next(
        (
            item
            for item in room["players"]
            if secrets.compare_digest(item["token"], supplied_token)
        ),
        None,
    )

    if player is None:
        raise HTTPException(status_code=401, detail="Invalid player token")

    return room, player


def serialize_online_room(room, player=None, include_token=False):
    payload = {
        "matchId": room["matchId"],
        "roomCode": room["roomCode"],
        "status": room["status"],
        "playerNumber": player["playerNumber"] if player else 0,
        "players": [
            {
                "playerNumber": item["playerNumber"],
                "ready": bool(item["ready"]),
                "challengeSubmitted": item.get("challengeRows") is not None,
                "resultSubmitted": item.get("result") is not None,
            }
            for item in room["players"]
        ],
    }

    if include_token and player is not None:
        payload["playerToken"] = player["token"]

    if (
        player is not None
        and player.get("challengeRows") is not None
    ):
        payload["ownChallengeMetadata"] = serialize_online_challenge_metadata(player)

    if (
        player is not None
        and len(room["players"]) == 2
        and all(item.get("challengeRows") is not None for item in room["players"])
    ):
        opponent = next(
            item
            for item in room["players"]
            if item["playerNumber"] != player["playerNumber"]
        )
        payload["opponentChallengeRows"] = list(opponent["challengeRows"])
        payload["opponentChallengeMetadata"] = serialize_online_challenge_metadata(opponent)

    if player is not None and player.get("result") is not None:
        payload["ownResult"] = dict(player["result"])

    if player is not None and len(room["players"]) == 2:
        opponent = next(
            item
            for item in room["players"]
            if item["playerNumber"] != player["playerNumber"]
        )

        if opponent.get("result") is not None:
            payload["opponentResult"] = dict(opponent["result"])

    return payload


def serialize_online_challenge_metadata(player):
    metadata = {"aiAssistantMode": player["aiAssistantMode"]}
    designer_intention = str(player.get("designerIntention") or "").strip()

    if designer_intention:
        metadata["designerIntention"] = designer_intention

    return metadata


def validate_online_challenge_rows(rows):
    width = 12
    height = 10
    validate_pc_rows(
        rows,
        width,
        height,
        {" ", "#", ".", "@", "p", "s", "t"},
        "rows",
    )

    player_count = sum(row.count("p") for row in rows)
    box_count = sum(row.count("s") for row in rows)
    target_count = sum(row.count("t") for row in rows)

    if player_count != 1:
        raise HTTPException(
            status_code=400,
            detail="Challenge must contain exactly one player start",
        )

    if box_count < 1 or box_count > 2:
        raise HTTPException(
            status_code=400,
            detail="Challenge must contain one or two box starts",
        )

    if target_count != box_count:
        raise HTTPException(
            status_code=400,
            detail="Challenge box and target counts must match",
        )


def validate_online_ai_assistant_mode(ai_assistant_mode):
    if ai_assistant_mode not in ONLINE_AI_ASSISTANT_MODES:
        raise HTTPException(
            status_code=400,
            detail="Unknown AI assistant mode",
        )


def normalize_online_designer_intention(value):
    intention = str(value or "").strip()

    if len(intention) > 4000:
        raise HTTPException(
            status_code=400,
            detail="Designer intention must contain at most 4000 characters",
        )

    return intention


def require_cocreation_intention_sync_secret(request):
    if not COCREATION_INTENTION_SYNC_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Co-creation intention synchronization is not configured",
        )

    supplied_secret = request.headers.get("X-CoCreation-Sync-Secret", "")

    if not secrets.compare_digest(
        supplied_secret,
        COCREATION_INTENTION_SYNC_SECRET,
    ):
        raise HTTPException(status_code=401, detail="Invalid co-creation sync secret")


def validate_online_result(payload):
    if (
        not math.isfinite(payload.durationSeconds)
        or payload.durationSeconds < 0
    ):
        raise HTTPException(
            status_code=400,
            detail="Result duration must be a finite non-negative number",
        )

    if payload.moveCount < 0 or payload.minimumMoves < 0:
        raise HTTPException(
            status_code=400,
            detail="Result move counts must be non-negative",
        )

    if payload.outcome not in {"completed", "timed_out"}:
        raise HTTPException(
            status_code=400,
            detail="Result outcome must be completed or timed_out",
        )

    if payload.outcome == "completed" and payload.moveCount < payload.minimumMoves:
        raise HTTPException(
            status_code=400,
            detail="Result move count cannot be lower than the theoretical minimum",
        )


class LevelDesignPlan(BaseModel):
    minSolutionSteps: int
    maxSolutionSteps: int
    minWaterAreas: int
    maxWaterAreas: int
    minWallObstacleBlocks: int
    maxWallObstacleBlocks: int
    minPushes: int
    maxPushes: int
    minReversePulls: int
    maxReversePulls: int
    style: str
    archetype: str
    targetLayout: str
    obstacleStyle: str
    waterStyle: str
    designNote: str
    corridorPlacement: str = "none"
    corridorWidth: int = 0
    corridorOrientation: str = "any"
    corridorRole: str = "visual_only"
    corridorPriority: str = "preferred"


class CreativeIdeaExpansionRequest(BaseModel):
    ideaId: str | None = ""
    sessionId: str | None = ""
    ideaText: str | None = ""
    sceneName: str | None = ""
    regenerationAttempt: int | None = 0
    previousOptions: list[dict] | None = None


class CreativeIdeaExpansionOption(BaseModel):
    id: str
    title: str
    description: str
    promptText: str


class HARevisionPlanRequest(BaseModel):
    ideaId: str | None = ""
    sessionId: str | None = ""
    gameRoundId: str | None = ""
    gameRoundIndex: int | None = 0
    adjustmentText: str
    sceneName: str | None = ""
    officialRound: bool | None = False
    previousLevelPlan: dict
    corridorValidation: dict | None = None
    regenerationAttempt: int | None = 0
    previousOptions: list[dict] | None = None


class HARevisionPlanEditRequest(BaseModel):
    ideaId: str | None = ""
    sessionId: str | None = ""
    adjustmentText: str
    sceneName: str | None = ""
    previousLevelPlan: dict
    corridorValidation: dict | None = None
    originalOption: dict
    editedDescription: str


class HumanAdjustmentValidationRequest(BaseModel):
    adjustmentText: str


class GenerationPreferences(BaseModel):
    minSolutionSteps: int | None = None
    maxSolutionSteps: int | None = None
    minWaterAreas: int | None = None
    maxWaterAreas: int | None = None
    minWallObstacleBlocks: int | None = None
    maxWallObstacleBlocks: int | None = None
    minPushes: int | None = None
    maxPushes: int | None = None
    minReversePulls: int | None = None
    maxReversePulls: int | None = None
    archetype: str | None = ""
    targetLayout: str | None = ""
    obstacleStyle: str | None = ""
    waterStyle: str | None = ""
    corridorPlacement: str | None = ""
    corridorWidth: int | None = None
    corridorOrientation: str | None = ""
    corridorRole: str | None = ""
    corridorPriority: str | None = ""


class LevelPlanRequest(BaseModel):
    ideaText: str | None = ""
    ideaId: str | None = ""
    sessionId: str | None = ""
    sceneName: str | None = ""
    originalIdeaText: str | None = ""
    selectedDirectionText: str | None = ""
    refinementFeedbackText: str | None = ""
    adjustmentHistoryText: str | None = ""
    latestAdjustmentText: str | None = ""
    revisionMode: str | None = ""
    previousLevelPlan: str | None = ""
    previousLevelMetrics: str | None = ""
    selectedHAPlan: str | None = ""
    styleDescription: str | None = ""
    generationPreferences: GenerationPreferences | None = None
    dgContext: dict | None = None
    maxAttempts: int | None = DEFAULT_LLM_MAX_ATTEMPTS


class DGGuideSummaryRequest(BaseModel):
    opponentRelationship: str
    opponentExperience: str
    language: str | None = "en"


class PCLevelGenerationRequest(BaseModel):
    width: int
    height: int
    sketchRows: list[str]
    previousCandidateRows: list[str] | None = None
    rejectionReason: str | None = ""
    maxAttempts: int | None = DEFAULT_LLM_MAX_ATTEMPTS


class RenameRoundRequest(BaseModel):
    roundId: str
    displayName: str


class RenameLevelRunRequest(BaseModel):
    levelRunId: str
    displayName: str


class RoundRequest(BaseModel):
    roundId: str


class DeleteLevelRunRequest(BaseModel):
    levelRunId: str | None = ""


class DeleteSurveyResponseRequest(BaseModel):
    responseId: str | None = ""
    playerNickname: str | None = ""


class DeleteCreativeIdeaRequest(BaseModel):
    ideaId: str | None = ""


class DeleteExpansionChoiceRequest(BaseModel):
    choiceId: str | None = ""


class DeleteHAPlanEventRequest(BaseModel):
    haEventId: str | None = ""


class DeleteJourneyEventRequest(BaseModel):
    journeyEventId: str | None = ""


class DeleteIdeaRecordsRequest(BaseModel):
    ideaId: str | None = ""


class DeleteOnlineMatchRequest(BaseModel):
    matchId: str | None = ""


DEFAULT_PLAN = {
    "minSolutionSteps": 22,
    "maxSolutionSteps": 42,
    "minWaterAreas": 1,
    "maxWaterAreas": 2,
    "minWallObstacleBlocks": 2,
    "maxWallObstacleBlocks": 3,
    "minPushes": 10,
    "maxPushes": 22,
    "minReversePulls": 18,
    "maxReversePulls": 34,
    "style": "hard classic choke route",
    "archetype": "bottleneck_corridor",
    "targetLayout": "split_pair",
    "obstacleStyle": "side_choke",
    "waterStyle": "side_pool",
    "designNote": "Hard two-box route with a forced choke point and separated goals.",
    "corridorPlacement": "none",
    "corridorWidth": 0,
    "corridorOrientation": "any",
    "corridorRole": "visual_only",
    "corridorPriority": "preferred",
}

RECENT_BLUEPRINT_LIMIT = 3
recent_blueprints = []
plan_history_lock = threading.Lock()
study_record_lock = threading.Lock()

LIMITS = {
    "minSolutionSteps": (18, 30),
    "maxSolutionSteps": (32, 50),
    "minWaterAreas": (0, 2),
    "maxWaterAreas": (0, 2),
    "minWallObstacleBlocks": (0, 2),
    "maxWallObstacleBlocks": (0, 3),
    "minPushes": (8, 16),
    "maxPushes": (14, 28),
    "minReversePulls": (14, 24),
    "maxReversePulls": (24, 40),
}

ENUMS = {
    "archetype": {"goal_room", "bottleneck_corridor", "split_route", "open_workshop"},
    "targetLayout": {"clustered", "split_pair", "edge_cluster"},
    "obstacleStyle": {"central_baffle", "side_choke", "goal_guard"},
    "waterStyle": {"corner_pool", "side_pool", "route_divider"},
    "corridorPlacement": {"none", "center", "side"},
    "corridorOrientation": {"horizontal", "vertical", "any"},
    "corridorRole": {"visual_only", "player_route", "required_box_route"},
    "corridorPriority": {"preferred", "required"},
}

HA_CHANGE_FIELDS = set(LIMITS) | set(ENUMS) | {"corridorWidth", "style"}

mimetypes.add_type("application/octet-stream", ".data")

app = FastAPI()
app.mount(
    "/frontend",
    StaticFiles(directory=FRONTEND_DIR, html=True, check_dir=False),
    name="frontend",
)
app.mount(
    "/game",
    StaticFiles(directory=WEBGL_BUILD_DIR, html=True, check_dir=False),
    name="game",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def attach_request_context(request: Request, call_next):
    request_id = new_request_id(request.headers.get("X-Request-ID", ""))
    request.state.request_id = request_id
    started_at = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception as exception:
        log_event(
            "ERROR",
            "http_request_unhandled",
            requestId=request_id,
            method=request.method,
            path=request.url.path,
            exceptionType=type(exception).__name__,
            errorMessage=safe_log_text(exception),
        )
        raise

    response.headers["X-Request-ID"] = request_id
    log_event(
        "INFO",
        "http_request_completed",
        requestId=request_id,
        method=request.method,
        path=request.url.path,
        statusCode=response.status_code,
        elapsedMs=round((time.perf_counter() - started_at) * 1000),
    )
    return response


@app.exception_handler(LLMServiceError)
async def handle_llm_service_error(request: Request, exception: LLMServiceError):
    request_id = getattr(request.state, "request_id", exception.request_id)
    detail = exception.to_detail()
    detail["requestId"] = request_id
    return JSONResponse(
        status_code=exception.status_code,
        content={"detail": detail},
        headers={
            "X-Request-ID": request_id,
            "X-LLM-Attempts-Used": str(exception.attempts_used),
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    payload = readiness_payload()
    return JSONResponse(
        status_code=200 if payload["status"] == "ready" else 503,
        content=payload,
    )


@app.post("/online/rooms")
def create_online_room():
    with ONLINE_ROOMS_LOCK:
        cleanup_expired_online_rooms()
        player = {
            "playerNumber": 1,
            "token": secrets.token_urlsafe(32),
            "ready": False,
            "challengeRows": None,
            "aiAssistantMode": None,
            "designerIntention": None,
            "result": None,
        }
        match_id = uuid.uuid4().hex
        room = {
            "matchId": match_id,
            "roomCode": create_unique_online_room_code(),
            "status": "waiting_for_opponent",
            "players": [player],
            "lastActivity": time.time(),
        }
        ONLINE_ROOMS[match_id] = room
        append_online_match_event(room, "room_created", player_number=1)
        return serialize_online_room(room, player, include_token=True)


@app.post("/online/rooms/join")
def join_online_room(payload: OnlineRoomJoinRequest):
    room_code = normalize_online_room_code(payload.roomCode)

    with ONLINE_ROOMS_LOCK:
        cleanup_expired_online_rooms()
        room = next(
            (
                item
                for item in ONLINE_ROOMS.values()
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
            "token": secrets.token_urlsafe(32),
            "ready": False,
            "challengeRows": None,
            "aiAssistantMode": None,
            "designerIntention": None,
            "result": None,
        }
        room["players"].append(player)
        room["status"] = "briefing"
        room["lastActivity"] = time.time()
        append_online_match_event(room, "player_joined", player_number=2)
        return serialize_online_room(room, player, include_token=True)


@app.get("/online/rooms/{match_id}")
def get_online_room(match_id: str, request: Request):
    with ONLINE_ROOMS_LOCK:
        room, player = require_online_room(
            match_id,
            request.headers.get("X-Player-Token"),
        )
        room["lastActivity"] = time.time()
        return serialize_online_room(room, player)


@app.post("/online/rooms/{match_id}/ready")
def set_online_ready(
    match_id: str,
    payload: OnlineReadyRequest,
    request: Request,
):
    with ONLINE_ROOMS_LOCK:
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

        previous_ready = bool(player["ready"])
        player["ready"] = bool(payload.ready)
        room["status"] = (
            "waiting_for_challenges"
            if all(item["ready"] for item in room["players"])
            else "briefing"
        )
        room["lastActivity"] = time.time()

        if previous_ready != player["ready"]:
            append_online_match_event(
                room,
                "ready_changed",
                player_number=player["playerNumber"],
                ready=player["ready"],
            )

        return serialize_online_room(room, player)


@app.post("/online/rooms/{match_id}/challenge")
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
    with ONLINE_ROOMS_LOCK:
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

            room["lastActivity"] = time.time()
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
        room["lastActivity"] = time.time()
        append_online_match_event(
            room,
            "challenge_submitted",
            player_number=player["playerNumber"],
            aiAssistantMode=payload.aiAssistantMode,
            rows=submitted_rows,
        )
        return serialize_online_room(room, player)


@app.post("/online/rooms/{match_id}/designer-intention")
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

    with ONLINE_ROOMS_LOCK:
        cleanup_expired_online_rooms()
        room = ONLINE_ROOMS.get(str(match_id or "").strip())

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

        existing_intention = str(player.get("designerIntention") or "").strip()

        if existing_intention and existing_intention != designer_intention:
            raise HTTPException(
                status_code=409,
                detail="Final designer intention is already frozen",
            )

        if not existing_intention:
            player["designerIntention"] = designer_intention
            room["lastActivity"] = time.time()
            append_online_match_event(
                room,
                "designer_intention_synchronized",
                player_number=payload.playerNumber,
                designerIntention=designer_intention,
            )

        return serialize_online_room(room, player)


@app.post("/online/rooms/{match_id}/result")
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
        "outcome": payload.outcome,
    }

    with ONLINE_ROOMS_LOCK:
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

            room["lastActivity"] = time.time()
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
        room["lastActivity"] = time.time()
        append_online_match_event(
            room,
            "result_submitted",
            player_number=player["playerNumber"],
            **submitted_result,
        )
        return serialize_online_room(room, player)


@app.post("/online/rooms/{match_id}/leave")
def leave_online_room(match_id: str, request: Request):
    with ONLINE_ROOMS_LOCK:
        room, player = require_online_room(
            match_id,
            request.headers.get("X-Player-Token"),
        )
        already_left = bool(player.get("left"))
        player["left"] = True
        room["status"] = "cancelled"
        room["lastActivity"] = time.time()

        if not already_left:
            append_online_match_event(
                room,
                "player_left",
                player_number=player["playerNumber"],
            )

        return serialize_online_room(room, player)


@app.post("/record-level-start")
async def record_level_start(request: Request):
    return await append_level_record(request, "level-start")


@app.post("/record-level-end")
async def record_level_end(request: Request):
    return await append_level_record(request, "level-end")


@app.post("/record-survey-response")
async def record_survey_response(request: Request):
    return await append_survey_record(request)


@app.post("/record-creative-idea")
async def record_creative_idea(request: Request):
    return await append_creative_idea_record(request)


@app.post("/record-expansion-choice")
async def record_expansion_choice(request: Request):
    return await append_expansion_choice_record(request)


@app.post("/record-ha-plan-choice")
async def record_ha_plan_choice(request: Request):
    return await append_ha_plan_event(request, "ha-plan-choice")


@app.post("/record-journey-event")
async def record_journey_event(request: Request):
    return await append_journey_event(request)


def apply_llm_execution_headers(response, execution):
    response.headers["X-Request-ID"] = execution.request_id
    response.headers["X-LLM-Attempts-Used"] = str(execution.attempts_used)


@app.post("/expand-creative-idea")
def expand_creative_idea(
    payload: CreativeIdeaExpansionRequest,
    request: Request,
    response: Response,
):
    data = payload.model_dump()
    idea_text = str(data.get("ideaText") or "").strip()

    if not idea_text:
        raise HTTPException(status_code=400, detail="ideaText is required")

    data["ideaText"] = idea_text
    execution = create_creative_idea_expansion(
        data,
        request.state.request_id,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


@app.post("/generate-ha-revision-plans")
def generate_ha_revision_plans(
    payload: HARevisionPlanRequest,
    request: Request,
    response: Response,
):
    data = payload.model_dump()
    adjustment_text = str(data.get("adjustmentText") or "").strip()
    previous_plan = data.get("previousLevelPlan")

    if not adjustment_text:
        raise HTTPException(status_code=400, detail="adjustmentText is required")

    if not isinstance(previous_plan, dict) or not previous_plan:
        raise HTTPException(status_code=400, detail="previousLevelPlan is required")

    data["adjustmentText"] = adjustment_text

    try:
        execution = create_ha_revision_plans(
            data,
            request.state.request_id,
        )
        append_ha_generation_event(data, execution.value["options"])
        apply_llm_execution_headers(response, execution)
        return execution.value
    except LLMServiceError as exception:
        append_ha_generation_event(
            data,
            [],
            error=exception.safe_message,
        )
        raise


@app.post("/revise-ha-revision-plan")
def revise_ha_revision_plan(
    payload: HARevisionPlanEditRequest,
    request: Request,
    response: Response,
):
    data = payload.model_dump()
    edited_description = str(data.get("editedDescription") or "").strip()
    previous_plan = data.get("previousLevelPlan")
    original_option = data.get("originalOption")

    if not edited_description:
        raise HTTPException(status_code=400, detail="editedDescription is required")

    if not isinstance(previous_plan, dict) or not previous_plan:
        raise HTTPException(status_code=400, detail="previousLevelPlan is required")

    if not isinstance(original_option, dict) or not original_option:
        raise HTTPException(status_code=400, detail="originalOption is required")

    data["editedDescription"] = edited_description[:420]
    execution = create_ha_revision_plan_edit(
        data,
        request.state.request_id,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


@app.get("/validate-human-adjustment")
def validate_human_adjustment_legacy(
    request: Request,
    response: Response,
    adjustmentText: str = "",
):
    adjustment_text = str(adjustmentText or "").strip()

    if not adjustment_text:
        return validate_human_adjustment_clarity_payload({})

    execution = create_human_adjustment_clarity_check(
        adjustment_text,
        request.state.request_id,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


@app.post("/validate-human-adjustment")
def validate_human_adjustment(
    payload: HumanAdjustmentValidationRequest,
    request: Request,
    response: Response,
):
    adjustment_text = str(payload.adjustmentText or "").strip()

    if not adjustment_text:
        raise HTTPException(status_code=400, detail="adjustmentText is required")

    execution = create_human_adjustment_clarity_check(
        adjustment_text,
        request.state.request_id,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


@app.get("/level-records", response_class=PlainTextResponse)
def get_level_records():
    if not STUDY_LOG_FILE.exists():
        return ""

    return STUDY_LOG_FILE.read_text(encoding="utf-8")


@app.get("/survey-records", response_class=PlainTextResponse)
def get_survey_records():
    if not SURVEY_LOG_FILE.exists():
        return ""

    return SURVEY_LOG_FILE.read_text(encoding="utf-8")


@app.get("/matchmaking-records", response_class=PlainTextResponse)
def get_matchmaking_records():
    if not ONLINE_MATCH_LOG_FILE.exists():
        return ""

    return ONLINE_MATCH_LOG_FILE.read_text(encoding="utf-8")


@app.get("/creative-ideas", response_class=PlainTextResponse)
def get_creative_ideas():
    if not CREATIVE_IDEA_LOG_FILE.exists():
        return ""

    return CREATIVE_IDEA_LOG_FILE.read_text(encoding="utf-8")


@app.get("/survey-records-data")
def get_survey_records_data():
    responses, malformed_count = read_survey_response_events()
    return build_survey_records_payload(responses, malformed_count)


@app.get("/matchmaking-records-data")
def get_matchmaking_records_data():
    events, malformed_count = read_online_match_events()
    return build_matchmaking_records_payload(
        events,
        malformed_count,
    )


@app.get("/creative-ideas-data")
def get_creative_ideas_data():
    ideas, malformed_count = read_creative_idea_events()
    return build_creative_ideas_payload(ideas, malformed_count)


@app.get("/level-records-view", response_class=HTMLResponse)
def get_level_records_view(cleared: int = 0):
    target = "/frontend/"

    if cleared == 1:
        target += "?cleared=1"

    return RedirectResponse(target, status_code=302)


@app.get("/level-records-dashboard")
def get_level_records_dashboard(cleared: int = 0):
    target = "/frontend/"

    if cleared == 1:
        target += "?cleared=1"

    return RedirectResponse(target, status_code=302)


@app.get("/level-records-data")
def get_level_records_data():
    events, malformed_count = read_level_record_events()
    events = filter_frontend_records(events)
    levels = merge_level_records(events)
    payload = build_level_records_payload(events, levels, malformed_count)
    survey_responses, survey_malformed_count = read_survey_response_events()
    survey_responses = filter_frontend_records(survey_responses)
    survey_payload = build_survey_records_payload(
        survey_responses,
        survey_malformed_count,
    )
    payload["surveySummary"] = survey_payload["summary"]
    payload["surveyResponses"] = survey_payload["responses"]
    payload["surveyMalformedCount"] = survey_payload["malformedCount"]
    creative_ideas, creative_malformed_count = read_creative_idea_events()
    creative_ideas = filter_frontend_records(creative_ideas)
    creative_payload = build_creative_ideas_payload(
        creative_ideas,
        creative_malformed_count,
    )
    payload["creativeIdeaSummary"] = creative_payload["summary"]
    payload["creativeIdeas"] = creative_payload["ideas"]
    payload["creativeIdeaMalformedCount"] = creative_payload["malformedCount"]
    expansion_choices, expansion_malformed_count = read_expansion_choice_events()
    expansion_choices = filter_frontend_records(expansion_choices)
    expansion_payload = build_expansion_choices_payload(
        expansion_choices,
        expansion_malformed_count,
    )
    payload["creativeExpansionChoiceSummary"] = expansion_payload["summary"]
    payload["creativeExpansionChoices"] = expansion_payload["choices"]
    payload["creativeExpansionChoiceMalformedCount"] = expansion_payload["malformedCount"]
    ha_plan_events, ha_plan_malformed_count = read_ha_plan_events()
    ha_plan_events = filter_frontend_ha_plan_records(ha_plan_events)
    ha_plan_payload = build_ha_plan_events_payload(
        ha_plan_events,
        ha_plan_malformed_count,
    )
    payload["haPlanSummary"] = ha_plan_payload["summary"]
    payload["haPlanEvents"] = ha_plan_payload["events"]
    payload["haPlanMalformedCount"] = ha_plan_payload["malformedCount"]
    journey_events, journey_malformed_count = read_journey_events()
    journey_events = filter_frontend_records(journey_events)
    journey_payload = build_journey_events_payload(
        journey_events,
        journey_malformed_count,
    )
    payload["journeyEventSummary"] = journey_payload["summary"]
    payload["journeyEvents"] = journey_payload["events"]
    payload["journeyEventMalformedCount"] = journey_payload["malformedCount"]
    return payload


@app.get("/level-records-legacy", response_class=HTMLResponse)
def get_level_records_legacy(cleared: int = 0):
    events, malformed_count = read_level_record_events()
    levels = merge_level_records(events)
    return render_level_records_view(events, levels, malformed_count, cleared == 1)


@app.post("/rename-round")
def rename_round(request: RenameRoundRequest):
    round_id = normalize_round_id(request.roundId)
    display_name = normalize_round_display_name(request.displayName)

    if not round_id:
        raise HTTPException(status_code=400, detail="roundId is required")

    if round_id == "legacy-round":
        raise HTTPException(status_code=400, detail="Legacy Round cannot be renamed")

    if not display_name:
        raise HTTPException(status_code=400, detail="displayName is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        records, _ = read_level_record_events()
        matched_count = 0

        for record in records:
            if is_level_event_in_round(record, round_id):
                record["roundDisplayName"] = display_name
                matched_count += 1

        if matched_count == 0:
            raise HTTPException(status_code=404, detail="Round not found")

        write_jsonl_records(STUDY_LOG_FILE, records)

    return {
        "status": "ok",
        "roundId": round_id,
        "displayName": display_name,
        "updatedEventCount": matched_count,
    }


@app.post("/rename-level-run")
def rename_level_run(request: RenameLevelRunRequest):
    level_run_id = normalize_level_run_id(request.levelRunId)
    display_name = normalize_level_display_name(request.displayName)

    if not level_run_id:
        raise HTTPException(status_code=400, detail="levelRunId is required")

    if not display_name:
        raise HTTPException(status_code=400, detail="displayName is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        records, _ = read_level_record_events()
        matched_count = 0

        for record in records:
            record_level_run_id = normalize_level_run_id(record.get("levelRunId"))

            if record_level_run_id == level_run_id:
                record["levelDisplayName"] = display_name
                matched_count += 1

        if matched_count == 0:
            raise HTTPException(status_code=404, detail="Level run not found")

        write_jsonl_records(STUDY_LOG_FILE, records)

    return {
        "status": "ok",
        "levelRunId": level_run_id,
        "displayName": display_name,
        "updatedEventCount": matched_count,
    }


@app.post("/delete-round", dependencies=[Depends(require_delete_password)])
def delete_round(request: RoundRequest):
    round_id = normalize_round_id(request.roundId)

    if not round_id:
        raise HTTPException(status_code=400, detail="roundId is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        records, _ = read_level_record_events()
        remaining_records = []
        deleted_count = 0

        for record in records:
            if is_level_event_in_round(record, round_id):
                deleted_count += 1
            else:
                remaining_records.append(record)

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail="Round not found")

        write_jsonl_records(STUDY_LOG_FILE, remaining_records)

    return {
        "status": "ok",
        "roundId": round_id,
        "deletedEventCount": deleted_count,
    }


@app.post("/delete-level-run", dependencies=[Depends(require_delete_password)])
def delete_level_run(request: DeleteLevelRunRequest):
    level_run_id = normalize_level_run_id(request.levelRunId)

    if not level_run_id:
        raise HTTPException(status_code=400, detail="levelRunId is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        records, _ = read_level_record_events()
        remaining_records = []
        deleted_count = 0

        for record in records:
            record_level_run_id = normalize_level_run_id(record.get("levelRunId"))

            if record_level_run_id == level_run_id:
                deleted_count += 1
            else:
                remaining_records.append(record)

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail="Level run not found")

        write_jsonl_records(STUDY_LOG_FILE, remaining_records)

    return {
        "status": "ok",
        "levelRunId": level_run_id,
        "deletedEventCount": deleted_count,
    }


@app.post(
    "/delete-survey-response",
    dependencies=[Depends(require_delete_password)],
)
def delete_survey_response(request: DeleteSurveyResponseRequest):
    response_id = normalize_survey_identifier(request.responseId)
    player_nickname = normalize_survey_identifier(request.playerNickname)

    if not response_id and not player_nickname:
        raise HTTPException(
            status_code=400,
            detail="responseId or playerNickname is required",
        )

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        records, _ = read_survey_response_events()
        remaining_records = []
        deleted_count = 0

        if response_id:
            for record in records:
                record_response_id = normalize_survey_identifier(record.get("responseId"))

                if record_response_id == response_id:
                    deleted_count += 1
                else:
                    remaining_records.append(record)

            if deleted_count == 0:
                raise HTTPException(status_code=404, detail="Survey response not found")
        else:
            matching_indexes = [
                index
                for index, record in enumerate(records)
                if not normalize_survey_identifier(record.get("responseId"))
                and get_survey_player_identifier(record) == player_nickname
            ]

            if len(matching_indexes) == 0:
                raise HTTPException(status_code=404, detail="Survey response not found")

            if len(matching_indexes) > 1:
                raise HTTPException(
                    status_code=409,
                    detail="Multiple survey responses match this nickname",
                )

            matched_index = matching_indexes[0]
            remaining_records = [
                record
                for index, record in enumerate(records)
                if index != matched_index
            ]
            deleted_count = 1

        write_jsonl_records(SURVEY_LOG_FILE, remaining_records)

    return {
        "status": "ok",
        "deletedResponseCount": deleted_count,
    }


@app.post(
    "/delete-creative-idea",
    dependencies=[Depends(require_delete_password)],
)
def delete_creative_idea(request: DeleteCreativeIdeaRequest):
    idea_id = normalize_creative_idea_identifier(request.ideaId)

    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        records, _ = read_creative_idea_events()
        remaining_records = []
        deleted_count = 0

        for record in records:
            record_idea_id = normalize_creative_idea_identifier(
                record.get("ideaId")
                or record.get("id")
            )

            if record_idea_id == idea_id:
                deleted_count += 1
            else:
                remaining_records.append(record)

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail="Creative idea not found")

        write_jsonl_records(CREATIVE_IDEA_LOG_FILE, remaining_records)

    return {
        "status": "ok",
        "ideaId": idea_id,
        "deletedIdeaCount": deleted_count,
    }


@app.post(
    "/delete-expansion-choice",
    dependencies=[Depends(require_delete_password)],
)
def delete_expansion_choice(request: DeleteExpansionChoiceRequest):
    choice_id = normalize_expansion_choice_identifier(request.choiceId)

    if not choice_id:
        raise HTTPException(status_code=400, detail="choiceId is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        records, _ = read_expansion_choice_events()
        remaining_records = []
        deleted_count = 0

        for record in records:
            record_choice_id = (
                normalize_expansion_choice_identifier(record.get("choiceId"))
                or build_expansion_choice_identifier(record)
            )

            if record_choice_id == choice_id:
                deleted_count += 1
            else:
                remaining_records.append(record)

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail="Expansion choice not found")

        write_jsonl_records(CREATIVE_EXPANSION_CHOICE_LOG_FILE, remaining_records)

    return {
        "status": "ok",
        "choiceId": choice_id,
        "deletedChoiceCount": deleted_count,
    }


@app.post(
    "/delete-ha-plan-event",
    dependencies=[Depends(require_delete_password)],
)
def delete_ha_plan_event(request: DeleteHAPlanEventRequest):
    ha_event_id = normalize_expansion_choice_identifier(request.haEventId)

    if not ha_event_id:
        raise HTTPException(status_code=400, detail="haEventId is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        records, _ = read_ha_plan_events()
        remaining_records = []
        deleted_count = 0

        for record in records:
            record_event_id = build_ha_plan_event_identifier(record)

            if record_event_id == ha_event_id:
                deleted_count += 1
            else:
                remaining_records.append(record)

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail="HA plan event not found")

        write_jsonl_records(HA_PLAN_EVENT_LOG_FILE, remaining_records)

    return {
        "status": "ok",
        "haEventId": ha_event_id,
        "deletedHAPlanEventCount": deleted_count,
    }


@app.post(
    "/delete-journey-event",
    dependencies=[Depends(require_delete_password)],
)
def delete_journey_event(request: DeleteJourneyEventRequest):
    journey_event_id = normalize_expansion_choice_identifier(request.journeyEventId)

    if not journey_event_id:
        raise HTTPException(status_code=400, detail="journeyEventId is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        records, _ = read_journey_events()
        remaining_records = []
        deleted_count = 0

        for record in records:
            if build_journey_event_identifier(record) == journey_event_id:
                deleted_count += 1
            else:
                remaining_records.append(record)

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail="Journey event not found")

        write_jsonl_records(JOURNEY_EVENT_LOG_FILE, remaining_records)

    return {
        "status": "ok",
        "journeyEventId": journey_event_id,
        "deletedJourneyEventCount": deleted_count,
    }


@app.post(
    "/delete-idea-records",
    dependencies=[Depends(require_delete_password)],
)
def delete_idea_records(request: DeleteIdeaRecordsRequest):
    idea_id = normalize_creative_idea_identifier(request.ideaId)

    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        level_records, _ = read_level_record_events()
        idea_records, _ = read_creative_idea_events()
        choice_records, _ = read_expansion_choice_events()
        ha_plan_records, _ = read_ha_plan_events()
        journey_records, _ = read_journey_events()
        survey_records, _ = read_survey_response_events()
        paired_survey_session_ids = {
            normalize_survey_identifier(record.get("sessionId"))
            for record in survey_records
            if normalize_creative_idea_identifier(record.get("creativeIdeaId")) == idea_id
            and normalize_survey_identifier(record.get("sessionId"))
        }

        remaining_level_records = [
            record for record in level_records
            if normalize_creative_idea_identifier(record.get("creativeIdeaId")) != idea_id
        ]
        remaining_idea_records = [
            record for record in idea_records
            if normalize_creative_idea_identifier(record.get("ideaId")) != idea_id
        ]
        remaining_choice_records = [
            record for record in choice_records
            if normalize_creative_idea_identifier(record.get("ideaId")) != idea_id
        ]
        remaining_ha_plan_records = [
            record for record in ha_plan_records
            if normalize_creative_idea_identifier(record.get("ideaId")) != idea_id
        ]
        remaining_journey_records = [
            record for record in journey_records
            if normalize_creative_idea_identifier(record.get("ideaId")) != idea_id
        ]
        remaining_survey_records = [
            record for record in survey_records
            if normalize_creative_idea_identifier(record.get("creativeIdeaId")) != idea_id
            and normalize_survey_identifier(record.get("sessionId"))
            not in paired_survey_session_ids
        ]

        deleted_level_event_count = len(level_records) - len(remaining_level_records)
        deleted_idea_count = len(idea_records) - len(remaining_idea_records)
        deleted_choice_count = len(choice_records) - len(remaining_choice_records)
        deleted_ha_plan_count = len(ha_plan_records) - len(remaining_ha_plan_records)
        deleted_journey_event_count = len(journey_records) - len(remaining_journey_records)
        deleted_survey_count = len(survey_records) - len(remaining_survey_records)

        if (
            deleted_level_event_count
            + deleted_idea_count
            + deleted_choice_count
            + deleted_ha_plan_count
            + deleted_journey_event_count
            + deleted_survey_count
            == 0
        ):
            raise HTTPException(status_code=404, detail="Idea records not found")

        write_jsonl_records(STUDY_LOG_FILE, remaining_level_records)
        write_jsonl_records(CREATIVE_IDEA_LOG_FILE, remaining_idea_records)
        write_jsonl_records(
            CREATIVE_EXPANSION_CHOICE_LOG_FILE,
            remaining_choice_records,
        )
        write_jsonl_records(SURVEY_LOG_FILE, remaining_survey_records)
        write_jsonl_records(HA_PLAN_EVENT_LOG_FILE, remaining_ha_plan_records)
        write_jsonl_records(JOURNEY_EVENT_LOG_FILE, remaining_journey_records)

    return {
        "status": "ok",
        "ideaId": idea_id,
        "deletedLevelEventCount": deleted_level_event_count,
        "deletedIdeaCount": deleted_idea_count,
        "deletedChoiceCount": deleted_choice_count,
        "deletedHAPlanEventCount": deleted_ha_plan_count,
        "deletedJourneyEventCount": deleted_journey_event_count,
        "deletedSurveyCount": deleted_survey_count,
    }


@app.post(
    "/delete-online-match",
    dependencies=[Depends(require_delete_password)],
)
def delete_online_match(request: DeleteOnlineMatchRequest):
    match_id = normalize_survey_identifier(request.matchId)

    if not match_id:
        raise HTTPException(status_code=400, detail="matchId is required")

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        match_events, _ = read_online_match_events()
        remaining_events = [
            event
            for event in match_events
            if normalize_survey_identifier(event.get("matchId")) != match_id
        ]
        deleted_event_count = len(match_events) - len(remaining_events)

        if deleted_event_count == 0:
            raise HTTPException(status_code=404, detail="Online match not found")

        write_jsonl_records(ONLINE_MATCH_LOG_FILE, remaining_events)

    return {
        "status": "ok",
        "matchId": match_id,
        "deletedEventCount": deleted_event_count,
    }


@app.post(
    "/clear-matchmaking-records",
    dependencies=[Depends(require_delete_password)],
)
def clear_matchmaking_records():
    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        ONLINE_MATCH_LOG_FILE.write_text("", encoding="utf-8")

    return {
        "status": "ok",
    }


@app.post(
    "/clear-level-records",
    dependencies=[Depends(require_delete_password)],
)
def clear_level_records():
    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        STUDY_LOG_FILE.write_text("", encoding="utf-8")
        SURVEY_LOG_FILE.write_text("", encoding="utf-8")
        CREATIVE_IDEA_LOG_FILE.write_text("", encoding="utf-8")
        CREATIVE_EXPANSION_CHOICE_LOG_FILE.write_text("", encoding="utf-8")
        HA_PLAN_EVENT_LOG_FILE.write_text("", encoding="utf-8")
        JOURNEY_EVENT_LOG_FILE.write_text("", encoding="utf-8")

    return RedirectResponse("/level-records-view?cleared=1", status_code=303)


@app.get("/generate-level-plan")
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
    return execute_level_plan_request(payload, request, response)


@app.post("/generate-level-plan")
def generate_level_plan(
    payload: LevelPlanRequest,
    request: Request,
    response: Response,
):
    return execute_level_plan_request(payload, request, response)


DG_RELATIONSHIPS = {
    "friend": "a close friend",
    "acquaintance": "an acquaintance",
    "stranger": "a stranger",
}
DG_EXPERIENCES = {
    "relaxed": ("Easy", "relaxed and approachable"),
    "challenging_fair": ("Medium", "challenging but fair"),
    "breakthrough": ("Hard", "several attempts followed by a breakthrough"),
}
DG_FIRST_PERSON_PREFIXES = (
    "i get the sense that",
    "my guess is that",
    "i think",
    "i feel",
    "from my perspective",
)


def normalize_dg_reflection(summary):
    summary = str(summary or "").strip()
    if not summary:
        return ""
    if summary.lower().startswith(DG_FIRST_PERSON_PREFIXES):
        return summary[:320]
    return ("I get the sense that " + summary[:290]).strip()


def build_dg_fallback_summary(relationship, experience, reason=""):
    difficulty, _ = DG_EXPERIENCES[experience]
    relationship_tone = {
        "friend": "a caring shared challenge",
        "acquaintance": "a clear and welcoming challenge",
        "stranger": "a readable challenge that earns trust quickly",
    }[relationship]
    reflection = {
        "relaxed": "I get the sense that you may want a calm opening where the other player can settle into the puzzle and feel capable early on.",
        "challenging_fair": "I get the sense that you may want a few decisions to create real pressure, while still letting the other player understand why each solution works.",
        "breakthrough": "I get the sense that you may be aiming for a patient struggle that turns into a satisfying moment of recognition rather than a punishing surprise.",
    }[experience]
    return {
        "summary": reflection,
        "recommendedDifficulty": difficulty,
        "rationale": (
            "I would begin with " + difficulty + " because it best supports " + relationship_tone
            + " without treating the relationship itself as a difficulty setting."
        ),
        "source": "deterministic_fallback",
        "fallbackReason": reason,
    }


def create_dg_guide_summary(context, request_id=""):
    relationship = context["opponentRelationship"]
    experience = context["opponentExperience"]
    fallback = build_dg_fallback_summary(relationship, experience)

    def validate(payload):
        if not isinstance(payload, dict):
            raise ValueError("DG guide response must be a JSON object")
        summary = normalize_dg_reflection(payload.get("summary"))
        rationale = str(payload.get("rationale") or "").strip()[:320]
        difficulty = str(payload.get("recommendedDifficulty") or "").strip()
        if not summary or not rationale:
            raise ValueError("DG guide summary and rationale are required")
        if difficulty not in {"Easy", "Medium", "Hard", "Random"}:
            raise ValueError("DG guide difficulty is invalid")
        return {
            "summary": summary,
            "recommendedDifficulty": difficulty,
            "rationale": rationale,
            "source": "llm",
        }

    try:
        execution = execute_json_request(
            task="dg_guide_summary",
            messages=build_dg_guide_summary_messages(context),
            validator=validate,
            temperature=0.2,
            timeout_seconds=SHORT_LLM_TIMEOUT_SECONDS,
            max_attempts=1,
            request_id=request_id,
            validation_stage="dg_guide_validation",
        )
        return execution.value
    except LLMServiceError as exception:
        fallback["fallbackReason"] = exception.safe_message
        return fallback


@app.post("/dg/guide/summary")
def dg_guide_summary(
    payload: DGGuideSummaryRequest,
    request: Request,
    response: Response,
):
    relationship = payload.opponentRelationship.strip()
    experience = payload.opponentExperience.strip()
    if relationship not in DG_RELATIONSHIPS:
        raise HTTPException(status_code=400, detail="Invalid DG opponent relationship.")
    if experience not in DG_EXPERIENCES:
        raise HTTPException(status_code=400, detail="Invalid DG opponent experience.")
    result = create_dg_guide_summary(
        {"opponentRelationship": relationship, "opponentExperience": experience},
        request.state.request_id,
    )
    response.headers["X-DG-Guide-Source"] = result.get("source", "")
    return result


@app.post("/generate-pc-level")
def generate_pc_level(
    payload: PCLevelGenerationRequest,
    request: Request,
    response: Response,
):
    data = payload.model_dump(exclude={"maxAttempts"})

    try:
        data = normalize_pc_level_request(data)
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception

    max_attempts = max(
        1,
        min(DEFAULT_LLM_MAX_ATTEMPTS, int(payload.maxAttempts or 1)),
    )
    execution = create_pc_level_candidate(
        data,
        request.state.request_id,
        max_attempts,
        context_is_normalized=True,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


def execute_level_plan_request(
    payload: LevelPlanRequest,
    request: Request,
    response: Response,
):
    data = payload.model_dump(exclude={"maxAttempts"})

    try:
        data["generationPreferences"] = normalize_generation_preferences(
            data.get("generationPreferences")
        )
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception

    max_attempts = max(
        1,
        min(DEFAULT_LLM_MAX_ATTEMPTS, int(payload.maxAttempts or 1)),
    )
    execution = create_level_plan(
        data,
        request.state.request_id,
        max_attempts,
    )
    apply_llm_execution_headers(response, execution)
    return execution.value


async def append_level_record(request: Request, default_event_type: str):
    data = await request.json()

    if not isinstance(data, dict):
        data = {"payload": data}

    data.setdefault("eventType", default_event_type)
    data["serverReceivedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        with STUDY_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(data, ensure_ascii=False))
            log_file.write("\n")

    return {
        "status": "ok",
        "eventType": data["eventType"],
        "logFile": str(STUDY_LOG_FILE),
    }


async def append_survey_record(request: Request):
    data = await request.json()

    if not isinstance(data, dict):
        data = {"payload": data}

    data.setdefault("eventType", "survey-response")
    data["serverReceivedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        with SURVEY_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(data, ensure_ascii=False))
            log_file.write("\n")

    return {
        "status": "ok",
        "eventType": data["eventType"],
        "logFile": str(SURVEY_LOG_FILE),
    }


async def append_creative_idea_record(request: Request):
    data = await request.json()

    if not isinstance(data, dict):
        data = {"payload": data}

    idea_text = str(data.get("ideaText") or data.get("idea") or "").strip()

    if not idea_text:
        raise HTTPException(status_code=400, detail="ideaText is required")

    data["ideaText"] = idea_text
    data.setdefault("eventType", "creative-idea")
    data.setdefault("ideaId", f"idea-{int(time.time() * 1000)}")
    data["serverReceivedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        with CREATIVE_IDEA_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(data, ensure_ascii=False))
            log_file.write("\n")

    return {
        "status": "ok",
        "eventType": data["eventType"],
        "ideaId": data["ideaId"],
        "logFile": str(CREATIVE_IDEA_LOG_FILE),
    }


async def append_expansion_choice_record(request: Request):
    data = await request.json()

    if not isinstance(data, dict):
        data = {"payload": data}

    idea_id = str(data.get("ideaId") or "").strip()
    selected_title = str(data.get("selectedOptionTitle") or "").strip()

    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")

    if not selected_title:
        raise HTTPException(status_code=400, detail="selectedOptionTitle is required")

    data.setdefault("eventType", "creative-expansion-choice")
    data["serverReceivedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not normalize_expansion_choice_identifier(data.get("choiceId")):
        data["choiceId"] = build_expansion_choice_identifier(data)

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        with CREATIVE_EXPANSION_CHOICE_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(data, ensure_ascii=False))
            log_file.write("\n")

    return {
        "status": "ok",
        "eventType": data["eventType"],
        "choiceId": data["choiceId"],
        "ideaId": idea_id,
        "logFile": str(CREATIVE_EXPANSION_CHOICE_LOG_FILE),
    }


async def append_ha_plan_event(request: Request, default_event_type: str):
    data = await request.json()

    if not isinstance(data, dict):
        data = {"payload": data}

    idea_id = str(data.get("ideaId") or "").strip()
    selected_title = str(data.get("selectedOptionTitle") or "").strip()

    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")

    if default_event_type == "ha-plan-choice" and not selected_title:
        raise HTTPException(status_code=400, detail="selectedOptionTitle is required")

    data.setdefault("eventType", default_event_type)
    data["serverReceivedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_ha_plan_event(data)
    return {
        "status": "ok",
        "eventType": data["eventType"],
        "ideaId": idea_id,
        "logFile": str(HA_PLAN_EVENT_LOG_FILE),
    }


async def append_journey_event(request: Request):
    data = await request.json()

    if not isinstance(data, dict):
        data = {"payload": data}

    idea_id = str(data.get("ideaId") or "").strip()
    phase = str(data.get("phase") or "").strip()

    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")

    if not phase:
        raise HTTPException(status_code=400, detail="phase is required")

    data.setdefault("eventType", "journey-event")
    data["serverReceivedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data["journeyEventId"] = build_journey_event_identifier(data)
    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        with JOURNEY_EVENT_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(data, ensure_ascii=False))
            log_file.write("\n")

    return {
        "status": "ok",
        "eventType": data["eventType"],
        "journeyEventId": data["journeyEventId"],
        "ideaId": idea_id,
        "logFile": str(JOURNEY_EVENT_LOG_FILE),
    }


def append_ha_generation_event(request_data, options, error=""):
    data = {
        "eventType": "ha-plan-generation",
        "ideaId": str(request_data.get("ideaId") or "").strip(),
        "sessionId": str(request_data.get("sessionId") or "").strip(),
        "gameRoundId": str(request_data.get("gameRoundId") or "").strip(),
        "gameRoundIndex": int(request_data.get("gameRoundIndex") or 0),
        "adjustmentText": str(request_data.get("adjustmentText") or "").strip(),
        "sceneName": str(request_data.get("sceneName") or "").strip(),
        "officialRound": request_data.get("officialRound") is True,
        "previousLevelPlan": request_data.get("previousLevelPlan"),
        "corridorValidation": request_data.get("corridorValidation"),
        "regenerationAttempt": int(request_data.get("regenerationAttempt") or 0),
        "previousOptions": request_data.get("previousOptions"),
        "options": options,
        "error": str(error or ""),
        "serverReceivedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_ha_plan_event(data)


def write_ha_plan_event(data):
    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        with HA_PLAN_EVENT_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(data, ensure_ascii=False))
            log_file.write("\n")


def read_level_record_events():
    return read_jsonl_records(STUDY_LOG_FILE)


def read_survey_response_events():
    return read_jsonl_records(SURVEY_LOG_FILE)


def read_creative_idea_events():
    return read_jsonl_records(CREATIVE_IDEA_LOG_FILE)


def read_expansion_choice_events():
    return read_jsonl_records(CREATIVE_EXPANSION_CHOICE_LOG_FILE)


def read_ha_plan_events():
    return read_jsonl_records(HA_PLAN_EVENT_LOG_FILE)


def read_journey_events():
    return read_jsonl_records(JOURNEY_EVENT_LOG_FILE)


def read_online_match_events():
    return read_jsonl_records(ONLINE_MATCH_LOG_FILE)


def build_matchmaking_records_payload(
    events,
    malformed_count,
):
    matches = {}
    allowed_event_fields = {
        "eventId",
        "eventType",
        "matchId",
        "roomCode",
        "playerNumber",
        "statusAfter",
        "serverReceivedAt",
        "ready",
        "aiAssistantMode",
        "designerIntention",
        "rows",
        "durationSeconds",
        "moveCount",
        "minimumMoves",
        "outcome",
    }

    def create_player(player_number):
        return {
            "playerNumber": player_number,
            "joinedAt": None,
            "ready": False,
            "readyAt": None,
            "leftAt": None,
            "challenge": None,
            "result": None,
            "designerIntention": "",
        }

    def get_match(match_id, room_code=""):
        if match_id not in matches:
            matches[match_id] = {
                "matchId": match_id,
                "roomCode": room_code,
                "status": "in_progress",
                "createdAt": None,
                "updatedAt": None,
                "completedAt": None,
                "endedAt": None,
                "playerCount": 0,
                "players": {
                    1: create_player(1),
                    2: create_player(2),
                },
                "events": [],
            }
        elif room_code and not matches[match_id]["roomCode"]:
            matches[match_id]["roomCode"] = room_code

        return matches[match_id]

    ordered_events = sorted(
        (event for event in events if isinstance(event, dict)),
        key=lambda event: str(event.get("serverReceivedAt") or ""),
    )

    for event in ordered_events:
        match_id = normalize_survey_identifier(event.get("matchId"))

        if not match_id:
            continue

        room_code = normalize_survey_identifier(event.get("roomCode"))
        match_record = get_match(match_id, room_code)
        safe_event = {
            key: value
            for key, value in event.items()
            if key in allowed_event_fields
        }
        safe_event["matchId"] = match_id
        safe_event["roomCode"] = room_code or match_record["roomCode"]
        match_record["events"].append(safe_event)
        timestamp = normalize_survey_identifier(event.get("serverReceivedAt"))

        if timestamp:
            if not match_record["createdAt"]:
                match_record["createdAt"] = timestamp
            match_record["updatedAt"] = timestamp

        try:
            player_number = int(event.get("playerNumber") or 0)
        except (TypeError, ValueError):
            player_number = 0

        player = match_record["players"].get(player_number)
        event_type = normalize_survey_identifier(event.get("eventType"))

        if event_type == "room_created":
            match_record["createdAt"] = timestamp or match_record["createdAt"]
            player = match_record["players"][1]
            player["joinedAt"] = player["joinedAt"] or timestamp
        elif event_type == "player_joined" and player is not None:
            player["joinedAt"] = player["joinedAt"] or timestamp
        elif event_type == "ready_changed" and player is not None:
            player["ready"] = event.get("ready") is True
            player["readyAt"] = timestamp
        elif event_type == "challenge_submitted" and player is not None:
            rows = event.get("rows")
            player["challenge"] = {
                "submittedAt": timestamp,
                "aiAssistantMode": normalize_survey_identifier(
                    event.get("aiAssistantMode")
                ),
                "rows": list(rows) if isinstance(rows, list) else [],
            }
        elif event_type == "designer_intention_synchronized" and player is not None:
            player["designerIntention"] = str(
                event.get("designerIntention") or ""
            ).strip()
        elif event_type == "result_submitted" and player is not None:
            player["result"] = {
                "submittedAt": timestamp,
                "durationSeconds": event.get("durationSeconds"),
                "moveCount": event.get("moveCount"),
                "minimumMoves": event.get("minimumMoves"),
                "outcome": event.get("outcome", "completed"),
            }
        elif event_type == "player_left" and player is not None:
            player["leftAt"] = timestamp

    result_records = []
    total_duration = 0.0
    duration_count = 0
    completed_count = 0
    cancelled_count = 0
    expired_count = 0

    for match_record in matches.values():
        players = match_record["players"]

        for creator_number, creator in players.items():
            if creator["challenge"] is not None:
                opponent_number = 3 - creator_number
                creator["challenge"]["createdByPlayerNumber"] = creator_number
                creator["challenge"]["playedByPlayerNumber"] = opponent_number
                opponent_result = players[opponent_number]["result"]
                creator["challenge"]["runResult"] = (
                    dict(opponent_result) if opponent_result is not None else None
                )

        completed = all(players[number]["result"] is not None for number in (1, 2))
        event_types = {
            event.get("eventType")
            for event in match_record["events"]
        }

        if completed:
            match_record["status"] = "completed"
            completed_count += 1
            result_times = [
                players[number]["result"].get("submittedAt")
                for number in (1, 2)
                if players[number]["result"] is not None
            ]
            match_record["completedAt"] = max(
                (value for value in result_times if value),
                default=None,
            )
            match_record["endedAt"] = match_record["completedAt"]
        elif "room_expired" in event_types:
            match_record["status"] = "expired"
            expired_count += 1
            match_record["endedAt"] = match_record["updatedAt"]
        elif "player_left" in event_types:
            match_record["status"] = "cancelled"
            cancelled_count += 1
            match_record["endedAt"] = match_record["updatedAt"]

        for player in players.values():
            result = player["result"]

            if result is not None and isinstance(
                result.get("durationSeconds"),
                (int, float),
            ):
                total_duration += float(result["durationSeconds"])
                duration_count += 1

        match_record["playerCount"] = sum(
            1 for player in players.values() if player["joinedAt"]
        )
        match_record["players"] = [players[1], players[2]]
        result_records.append(match_record)

    result_records.sort(
        key=lambda match_record: str(match_record.get("updatedAt") or ""),
        reverse=True,
    )
    average_duration = total_duration / duration_count if duration_count else 0
    return {
        "summary": {
            "matchCount": len(result_records),
            "completedCount": completed_count,
            "inProgressCount": (
                len(result_records)
                - completed_count
                - cancelled_count
                - expired_count
            ),
            "cancelledCount": cancelled_count,
            "expiredCount": expired_count,
            "averageRunDurationSeconds": round(average_duration, 2),
            "malformedCount": int(malformed_count or 0),
        },
        "matches": result_records,
        "malformedCount": int(malformed_count or 0),
        "logFile": str(ONLINE_MATCH_LOG_FILE),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def filter_frontend_records(records):
    return [
        record
        for record in records
        if is_frontend_visible_record(record)
    ]


def filter_frontend_ha_plan_records(records):
    official_idea_ids = {
        normalize_creative_idea_identifier(record.get("ideaId"))
        for record in records
        if record.get("officialRound") is True
    }

    return [
        record
        for record in records
        if record.get("officialRound") is True
        or (
            record.get("eventType") == "ha-plan-generation"
            and normalize_creative_idea_identifier(record.get("ideaId"))
            in official_idea_ids
        )
    ]


def is_frontend_visible_record(record):
    return record.get("officialRound") is True


def read_jsonl_records(path):
    if not path.exists():
        return [], 0

    records = []
    malformed_count = 0

    with path.open("r", encoding="utf-8") as log_file:
        for line in log_file:
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                malformed_count += 1
                continue

            if isinstance(data, dict):
                records.append(data)
            else:
                malformed_count += 1

    return records, malformed_count


def write_jsonl_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as log_file:
        for record in records:
            log_file.write(json.dumps(record, ensure_ascii=False))
            log_file.write("\n")


def merge_level_records(events):
    levels = {}

    for index, event in enumerate(events):
        level_run_id = str(event.get("levelRunId") or f"missing-run-{index + 1}")

        if level_run_id not in levels:
            levels[level_run_id] = {
                "levelRunId": level_run_id,
                "start": None,
                "end": None,
                "events": [],
                "order": index,
            }

        level = levels[level_run_id]
        level["events"].append(event)
        event_type = event.get("eventType")

        if event_type == "level-start":
            level["start"] = event
        elif event_type == "level-end":
            level["end"] = event

    return sorted(
        levels.values(),
        key=lambda level: (
            get_level_sort_value(level),
            level["order"],
        ),
    )


def build_level_records_payload(events, levels, malformed_count):
    session_ids = {
        event.get("sessionId")
        for event in events
        if event.get("sessionId")
    }
    rounds = build_round_records(levels)
    completed_count = 0
    missing_end_count = 0
    restarted_count = 0
    total_duration_seconds = 0.0
    ended_level_count = 0
    total_moves = 0
    total_pushes = 0
    source_counts = {}

    for level in levels:
        start = get_level_start(level)
        end = get_level_end(level)
        source = value_or_dash(get_record_value(start, "source"))
        source_counts[source] = source_counts.get(source, 0) + 1

        if not end:
            missing_end_count += 1
            continue

        if get_record_value(end, "completed"):
            completed_count += 1

        if get_record_value(end, "endReason") == "restarted":
            restarted_count += 1

        duration = get_record_value(end, "durationSeconds")

        if isinstance(duration, (int, float)):
            total_duration_seconds += duration
            ended_level_count += 1

        move_count = get_record_value(end, "moveCount")
        push_count = get_record_value(end, "pushCount")

        if isinstance(move_count, int):
            total_moves += move_count

        if isinstance(push_count, int):
            total_pushes += push_count

    average_duration_seconds = (
        total_duration_seconds / ended_level_count
        if ended_level_count > 0
        else 0
    )

    return {
        "summary": {
            "eventCount": len(events),
            "levelCount": len(levels),
            "roundCount": len(rounds),
            "sessionCount": len(session_ids),
            "completedCount": completed_count,
            "missingEndCount": missing_end_count,
            "restartedCount": restarted_count,
            "malformedCount": malformed_count,
            "totalMoves": total_moves,
            "totalPushes": total_pushes,
            "averageDurationSeconds": round(average_duration_seconds, 2),
            "sourceCounts": source_counts,
        },
        "events": events,
        "levels": levels,
        "rounds": rounds,
        "malformedCount": malformed_count,
        "logFile": str(STUDY_LOG_FILE),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_round_records(levels):
    rounds = {}

    for level in levels:
        start = get_level_start(level)
        end = get_level_end(level)
        raw_round_id = (
            get_record_value(start, "gameRoundId")
            or get_record_value(end, "gameRoundId")
        )
        has_round_id = bool(raw_round_id)
        round_id = str(raw_round_id) if has_round_id else "legacy-round"

        if round_id not in rounds:
            rounds[round_id] = {
                "roundId": round_id,
                "displayName": "Legacy Round" if not has_round_id else "",
                "customDisplayName": "",
                "shortId": short_id(round_id),
                "isLegacy": not has_round_id,
                "isInferred": not has_round_id,
                "roundIndex": None,
                "levels": [],
                "levelCount": 0,
                "completedCount": 0,
                "missingEndCount": 0,
                "failedCount": 0,
                "restartedCount": 0,
                "totalDurationSeconds": 0.0,
                "startedAt": None,
                "endedAt": None,
                "sceneNames": [],
                "order": level.get("order", 0),
            }

        round_record = rounds[round_id]
        level["roundId"] = round_id

        if round_record["isLegacy"] and has_round_id:
            round_record["isLegacy"] = False
            round_record["isInferred"] = False

        custom_display_name = get_round_display_name(start, end)
        if custom_display_name and not round_record["isLegacy"]:
            round_record["customDisplayName"] = custom_display_name

        round_index = (
            get_record_value(start, "gameRoundIndex")
            or get_record_value(end, "gameRoundIndex")
        )
        if isinstance(round_index, int) and round_index > 0:
            round_record["roundIndex"] = round_index

        scene_name = (
            get_record_value(start, "sceneName")
            or get_record_value(end, "sceneName")
        )
        if scene_name and scene_name not in round_record["sceneNames"]:
            round_record["sceneNames"].append(scene_name)

        started_at = get_level_started_at(level)
        ended_at = get_level_ended_at(level)
        round_record["startedAt"] = earliest_time(round_record["startedAt"], started_at)
        round_record["endedAt"] = latest_time(round_record["endedAt"], ended_at)

        round_record["levels"].append(level)
        round_record["levelCount"] += 1

        if not end:
            round_record["missingEndCount"] += 1
        elif get_record_value(end, "completed"):
            round_record["completedCount"] += 1
        elif get_record_value(end, "endReason") == "restarted":
            round_record["restartedCount"] += 1
        else:
            round_record["failedCount"] += 1

        duration = get_record_value(end, "durationSeconds")
        if isinstance(duration, (int, float)):
            round_record["totalDurationSeconds"] += duration

    ordered_for_labels = sorted(
        rounds.values(),
        key=lambda round_record: (
            round_record["startedAt"] or round_record["endedAt"] or "",
            round_record["order"],
        ),
    )

    sequence = 1
    for round_record in ordered_for_labels:
        round_record["levels"] = sorted(
            round_record["levels"],
            key=lambda level: (
                get_round_level_sort_value(level),
                level.get("order", 0),
            ),
        )
        round_record["totalDurationSeconds"] = round(
            round_record["totalDurationSeconds"],
            2,
        )

        if round_record["isLegacy"]:
            round_record["displayName"] = "Legacy Round"
            continue

        round_record["displayName"] = (
            round_record["customDisplayName"]
            or f"Round {sequence}"
        )
        sequence += 1

    return sorted(
        ordered_for_labels,
        key=lambda round_record: (
            round_record["startedAt"] or round_record["endedAt"] or "",
            round_record["order"],
        ),
        reverse=True,
    )


def build_survey_records_payload(responses, malformed_count):
    session_ids = {
        response.get("sessionId")
        for response in responses
        if response.get("sessionId")
    }
    survey_counts = {}
    total_duration_seconds = 0.0
    duration_count = 0
    answer_count = 0

    for response in responses:
        survey_id = value_or_dash(response.get("surveyId"))
        survey_counts[survey_id] = survey_counts.get(survey_id, 0) + 1

        duration = response.get("durationSeconds")

        if isinstance(duration, (int, float)):
            total_duration_seconds += duration
            duration_count += 1

        answers = response.get("answers")

        if isinstance(answers, list):
            answer_count += len(answers)

    average_duration_seconds = (
        total_duration_seconds / duration_count
        if duration_count > 0
        else 0
    )

    normalized_responses = [
        normalize_survey_response(response)
        for response in responses
    ]

    sorted_responses = sorted(
        normalized_responses,
        key=lambda response: response.get("serverReceivedAt") or response.get("timestamp") or "",
        reverse=True,
    )

    return {
        "summary": {
            "responseCount": len(responses),
            "sessionCount": len(session_ids),
            "answerCount": answer_count,
            "averageDurationSeconds": round(average_duration_seconds, 2),
            "malformedCount": malformed_count,
            "surveyCounts": survey_counts,
        },
        "responses": sorted_responses,
        "malformedCount": malformed_count,
        "logFile": str(SURVEY_LOG_FILE),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_creative_ideas_payload(ideas, malformed_count):
    session_ids = {
        idea.get("sessionId")
        for idea in ideas
        if idea.get("sessionId")
    }
    normalized_ideas = [
        normalize_creative_idea(idea)
        for idea in ideas
    ]
    sorted_ideas = sorted(
        normalized_ideas,
        key=lambda idea: idea.get("serverReceivedAt") or idea.get("timestamp") or "",
        reverse=True,
    )

    return {
        "summary": {
            "ideaCount": len(ideas),
            "sessionCount": len(session_ids),
            "malformedCount": malformed_count,
        },
        "ideas": sorted_ideas,
        "malformedCount": malformed_count,
        "logFile": str(CREATIVE_IDEA_LOG_FILE),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_expansion_choices_payload(choices, malformed_count):
    session_ids = {
        choice.get("sessionId")
        for choice in choices
        if choice.get("sessionId")
    }
    normalized_choices = [
        normalize_expansion_choice(choice)
        for choice in choices
    ]
    sorted_choices = sorted(
        normalized_choices,
        key=lambda choice: choice.get("serverReceivedAt") or choice.get("timestamp") or "",
        reverse=True,
    )

    return {
        "summary": {
            "choiceCount": len(choices),
            "sessionCount": len(session_ids),
            "malformedCount": malformed_count,
        },
        "choices": sorted_choices,
        "malformedCount": malformed_count,
        "logFile": str(CREATIVE_EXPANSION_CHOICE_LOG_FILE),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_ha_plan_events_payload(events, malformed_count):
    normalized_events = [
        normalize_ha_plan_event(event)
        for event in events
    ]
    sorted_events = sorted(
        normalized_events,
        key=lambda event: event.get("serverReceivedAt") or event.get("timestamp") or "",
        reverse=True,
    )
    choice_count = sum(
        1 for event in events
        if event.get("eventType") == "ha-plan-choice"
    )
    generation_count = sum(
        1 for event in events
        if event.get("eventType") == "ha-plan-generation"
    )

    return {
        "summary": {
            "eventCount": len(events),
            "generationCount": generation_count,
            "choiceCount": choice_count,
            "malformedCount": malformed_count,
        },
        "events": sorted_events,
        "malformedCount": malformed_count,
        "logFile": str(HA_PLAN_EVENT_LOG_FILE),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_journey_events_payload(events, malformed_count):
    normalized_events = [
        normalize_journey_event(event)
        for event in events
    ]
    sorted_events = sorted(
        normalized_events,
        key=lambda event: event.get("serverReceivedAt") or event.get("timestamp") or "",
        reverse=True,
    )
    phase_counts = {}

    for event in events:
        phase = value_or_dash(event.get("phase"))
        phase_counts[phase] = phase_counts.get(phase, 0) + 1

    return {
        "summary": {
            "eventCount": len(events),
            "phaseCounts": phase_counts,
            "malformedCount": malformed_count,
        },
        "events": sorted_events,
        "malformedCount": malformed_count,
        "logFile": str(JOURNEY_EVENT_LOG_FILE),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def normalize_journey_event(event):
    normalized = dict(event)
    normalized["journeyEventId"] = build_journey_event_identifier(event)
    normalized["ideaId"] = value_or_dash(event.get("ideaId"))
    normalized["sessionId"] = value_or_dash(event.get("sessionId"))
    normalized["gameRoundId"] = value_or_dash(event.get("gameRoundId"))
    normalized["phase"] = value_or_dash(event.get("phase"))
    normalized["action"] = value_or_dash(event.get("action"))
    normalized["detailText"] = value_or_dash(event.get("detailText"))
    normalized["revisionMode"] = value_or_dash(event.get("revisionMode"))
    return normalized


def build_journey_event_identifier(event):
    existing = normalize_expansion_choice_identifier(event.get("journeyEventId"))

    if existing:
        return existing

    identity = {
        "eventType": event.get("eventType"),
        "ideaId": event.get("ideaId"),
        "sessionId": event.get("sessionId"),
        "gameRoundId": event.get("gameRoundId"),
        "phase": event.get("phase"),
        "action": event.get("action"),
        "timestamp": event.get("timestamp"),
        "serverReceivedAt": event.get("serverReceivedAt"),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def normalize_ha_plan_event(event):
    normalized = dict(event)
    normalized["haEventId"] = build_ha_plan_event_identifier(event)
    normalized["ideaId"] = value_or_dash(event.get("ideaId"))
    normalized["sessionId"] = value_or_dash(event.get("sessionId"))
    normalized["gameRoundId"] = value_or_dash(event.get("gameRoundId"))
    normalized["adjustmentText"] = value_or_dash(event.get("adjustmentText"))
    normalized["selectedOptionId"] = value_or_dash(event.get("selectedOptionId"))
    normalized["selectedOptionTitle"] = value_or_dash(event.get("selectedOptionTitle"))
    normalized["selectedOptionDescription"] = value_or_dash(
        event.get("selectedOptionDescription")
    )
    normalized["selectedOptionPromptText"] = value_or_dash(
        event.get("selectedOptionPromptText")
    )
    return normalized


def build_ha_plan_event_identifier(event):
    existing = normalize_expansion_choice_identifier(event.get("haEventId"))

    if existing:
        return existing

    identity = {
        "eventType": event.get("eventType"),
        "ideaId": event.get("ideaId"),
        "sessionId": event.get("sessionId"),
        "serverReceivedAt": event.get("serverReceivedAt"),
        "timestamp": event.get("timestamp"),
        "regenerationAttempt": event.get("regenerationAttempt"),
        "selectedOptionId": event.get("selectedOptionId"),
        "selectedOptionTitle": event.get("selectedOptionTitle"),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def normalize_creative_idea(idea):
    normalized = dict(idea)
    normalized["ideaId"] = value_or_dash(
        idea.get("ideaId")
        or idea.get("id")
    )
    normalized["sessionId"] = value_or_dash(idea.get("sessionId"))
    normalized["ideaText"] = value_or_dash(
        idea.get("ideaText")
        or idea.get("idea")
        or idea.get("text")
    )
    normalized["sceneName"] = value_or_dash(idea.get("sceneName"))
    return normalized


def normalize_expansion_choice(choice):
    normalized = dict(choice)
    normalized["choiceId"] = value_or_dash(
        choice.get("choiceId")
        or build_expansion_choice_identifier(choice)
    )
    normalized["ideaId"] = value_or_dash(choice.get("ideaId"))
    normalized["sessionId"] = value_or_dash(choice.get("sessionId"))
    normalized["gameRoundId"] = value_or_dash(choice.get("gameRoundId"))
    normalized["originalIdeaText"] = value_or_dash(
        choice.get("originalIdeaText")
        or choice.get("originalIdea")
    )
    normalized["selectedOptionId"] = value_or_dash(choice.get("selectedOptionId"))
    normalized["selectedOptionTitle"] = value_or_dash(choice.get("selectedOptionTitle"))
    normalized["selectedOptionDescription"] = value_or_dash(
        choice.get("selectedOptionDescription")
    )
    normalized["selectedOptionPromptText"] = value_or_dash(
        choice.get("selectedOptionPromptText")
    )
    normalized["finalIdeaText"] = value_or_dash(
        choice.get("finalIdeaText")
        or choice.get("expandedIdeaText")
    )
    normalized["sceneName"] = value_or_dash(choice.get("sceneName"))
    return normalized


def normalize_survey_response(response):
    normalized = dict(response)
    answer_details = []
    answers = response.get("answers")

    if isinstance(answers, list):
        for answer in answers:
            if isinstance(answer, dict):
                answer_details.append(normalize_survey_answer(answer))

    normalized["playerNickname"] = get_survey_player_name(response)
    normalized["answerDetails"] = answer_details
    normalized["answersSummary"] = (
        "; ".join(answer["displayText"] for answer in answer_details)
        if answer_details
        else "-"
    )
    return normalized


def normalize_survey_answer(answer):
    normalized = dict(answer)
    question_index = value_or_dash(answer.get("questionIndex"))
    question_text = value_or_dash(answer.get("questionText") or answer.get("questionId"))
    option_text = value_or_dash(answer.get("optionText") or answer.get("optionId"))
    question_label = f"Q{question_index}" if question_index != "-" else "Question"

    normalized["questionLabel"] = question_label
    normalized["optionLabel"] = option_text
    normalized["displayText"] = (
        f"{question_label}: {question_text} -> {option_text}"
        if question_text != "-"
        else f"{question_label}: {option_text}"
    )
    return normalized


def get_survey_player_name(response):
    return value_or_dash(
        response.get("playerName")
        or response.get("playerNickname")
        or response.get("nickname")
    )


def render_level_records_view(events, levels, malformed_count, cleared):
    rounds = build_round_records(levels)
    completed_count = sum(1 for level in levels if get_level_end(level) and get_level_end(level).get("completed"))
    missing_end_count = sum(1 for level in levels if not get_level_end(level))
    rows_html = "\n".join(render_level_row(level) for level in levels)

    if not rows_html:
        rows_html = (
            '<tr><td colspan="16" class="empty">'
            "No level records found yet."
            "</td></tr>"
        )

    notice_html = ""

    if cleared:
        notice_html = '<div class="notice">Records cleared.</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sokoban Level Records</title>
    <style>
        body {{
            margin: 24px;
            font-family: Arial, Helvetica, sans-serif;
            color: #20242a;
            background: #f5f7fb;
        }}
        h1 {{
            margin: 0 0 8px;
            font-size: 28px;
        }}
        .meta {{
            margin-bottom: 18px;
            color: #5e6875;
        }}
        .summary {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin: 18px 0;
        }}
        .stat {{
            min-width: 130px;
            padding: 12px 14px;
            border: 1px solid #d9e0ea;
            border-radius: 6px;
            background: #ffffff;
        }}
        .stat strong {{
            display: block;
            font-size: 22px;
            color: #17202a;
        }}
        .toolbar {{
            display: flex;
            align-items: center;
            gap: 14px;
            margin: 18px 0;
        }}
        .toolbar a {{
            color: #175cd3;
            text-decoration: none;
        }}
        .toolbar form {{
            margin: 0;
        }}
        .danger-button {{
            padding: 7px 11px;
            border: 1px solid #c7372f;
            border-radius: 4px;
            color: #ffffff;
            background: #c7372f;
            cursor: pointer;
            font-size: 13px;
        }}
        .danger-button:hover {{
            background: #a82d27;
        }}
        .notice {{
            margin: 12px 0 18px;
            padding: 10px 12px;
            border: 1px solid #b7dfc1;
            border-radius: 6px;
            color: #14532d;
            background: #eaf8ee;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: #ffffff;
            border: 1px solid #d9e0ea;
        }}
        th, td {{
            padding: 8px 10px;
            border-bottom: 1px solid #e6ebf2;
            vertical-align: top;
            text-align: left;
            font-size: 13px;
        }}
        th {{
            position: sticky;
            top: 0;
            background: #edf2f7;
            z-index: 1;
        }}
        tr:nth-child(even) td {{
            background: #fafcff;
        }}
        .status-completed {{
            color: #16703b;
            font-weight: 700;
        }}
        .status-missing {{
            color: #a45c00;
            font-weight: 700;
        }}
        .map {{
            margin: 0;
            padding: 8px;
            min-width: 160px;
            border-radius: 4px;
            background: #17202a;
            color: #f7fafc;
            font-family: Consolas, "Courier New", monospace;
            font-size: 12px;
            line-height: 1.25;
            white-space: pre;
        }}
        .small {{
            color: #697586;
            font-size: 12px;
        }}
        .empty {{
            padding: 24px;
            text-align: center;
            color: #697586;
        }}
        .delete-dialog {{
            width: min(440px, calc(100vw - 28px));
            padding: 0;
            border: 4px solid #41494d;
            border-radius: 4px;
            background: #efe2c4;
            box-shadow: inset 0 0 0 2px #c8c9c3, 6px 6px 0 rgba(25, 29, 26, 0.55);
        }}
        .delete-dialog::backdrop {{
            background: rgba(20, 29, 23, 0.72);
        }}
        .delete-dialog h2 {{
            margin: 0;
            padding: 15px 18px;
            border-bottom: 4px solid #4a2d1c;
            color: #fff5d1;
            background: #8b562c;
            font-family: Consolas, "Courier New", monospace;
            text-shadow: 2px 2px 0 #382318;
        }}
        .delete-dialog-body {{
            padding: 18px;
        }}
        .delete-dialog label {{
            display: grid;
            gap: 7px;
            font-family: Consolas, "Courier New", monospace;
            font-weight: 700;
        }}
        .delete-dialog input {{
            min-height: 40px;
            padding: 7px 10px;
            border: 3px solid #41494d;
            border-radius: 2px;
            font-family: Consolas, "Courier New", monospace;
            font-size: 17px;
            letter-spacing: 3px;
        }}
        .delete-error {{
            min-height: 18px;
            margin: 8px 0 0;
            color: #7b281d;
            font-family: Consolas, "Courier New", monospace;
            font-size: 13px;
            font-weight: 700;
        }}
        .delete-dialog-actions {{
            display: flex;
            justify-content: flex-end;
            gap: 10px;
            padding: 13px 18px 17px;
            border-top: 3px solid #8e673e;
            background: #d2b583;
        }}
        .cancel-button {{
            padding: 7px 11px;
            border: 2px solid #41494d;
            border-radius: 2px;
            background: #c8c9c3;
            cursor: pointer;
        }}
    </style>
</head>
<body>
    <h1>Sokoban Level Records</h1>
    <div class="meta">Human-readable view generated from <code>{escape_text(str(STUDY_LOG_FILE))}</code>.</div>
    {notice_html}
    <div class="summary">
        <div class="stat"><strong>{len(events)}</strong>events</div>
        <div class="stat"><strong>{len(levels)}</strong>levels</div>
        <div class="stat"><strong>{len(rounds)}</strong>rounds</div>
        <div class="stat"><strong>{completed_count}</strong>completed</div>
        <div class="stat"><strong>{missing_end_count}</strong>missing end</div>
        <div class="stat"><strong>{malformed_count}</strong>malformed</div>
    </div>
    <div class="toolbar">
        <a href="/level-records">Raw JSONL</a>
        <a href="/docs">API Docs</a>
        <button class="danger-button" id="legacyClearButton" type="button">Clear Records</button>
    </div>
    <dialog class="delete-dialog" id="legacyDeleteDialog">
        <form id="legacyDeleteForm">
            <h2>Clear all records?</h2>
            <div class="delete-dialog-body">
                <p>This cannot be undone. Enter the deletion password to continue.</p>
                <label>
                    Deletion password
                    <input id="legacyDeletePassword" type="password" inputmode="numeric" autocomplete="off" required>
                </label>
                <p class="delete-error" id="legacyDeleteError" role="alert"></p>
            </div>
            <div class="delete-dialog-actions">
                <button class="cancel-button" id="legacyDeleteCancel" type="button">Cancel</button>
                <button class="danger-button" id="legacyDeleteConfirm" type="submit">Clear Records</button>
            </div>
        </form>
    </dialog>
    <table>
        <thead>
            <tr>
                <th>Round</th>
                <th>Level</th>
                <th>Source</th>
                <th>Status</th>
                <th>Duration</th>
                <th>Moves</th>
                <th>Pushes</th>
                <th>Restarts</th>
                <th>Solution</th>
                <th>Solver Pushes</th>
                <th>Attempts</th>
                <th>Wall</th>
                <th>Water</th>
                <th>Dead Corner</th>
                <th>Map Hash</th>
                <th>Map</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
    <script>
        (() => {{
            const dialog = document.getElementById("legacyDeleteDialog");
            const form = document.getElementById("legacyDeleteForm");
            const passwordInput = document.getElementById("legacyDeletePassword");
            const errorText = document.getElementById("legacyDeleteError");
            const cancelButton = document.getElementById("legacyDeleteCancel");
            const confirmButton = document.getElementById("legacyDeleteConfirm");

            document.getElementById("legacyClearButton").addEventListener("click", () => {{
                passwordInput.value = "";
                errorText.textContent = "";
                dialog.showModal();
                requestAnimationFrame(() => passwordInput.focus());
            }});

            cancelButton.addEventListener("click", () => dialog.close());

            form.addEventListener("submit", async event => {{
                event.preventDefault();
                errorText.textContent = "";
                passwordInput.disabled = true;
                cancelButton.disabled = true;
                confirmButton.disabled = true;
                confirmButton.textContent = "Clearing...";

                try {{
                    const response = await fetch("/clear-level-records", {{
                        method: "POST",
                        headers: {{ "X-Delete-Password": passwordInput.value }}
                    }});

                    if (!response.ok) {{
                        let message = "Could not clear records.";

                        try {{
                            const data = await response.json();
                            message = data.detail || message;
                        }} catch (error) {{
                            message = "HTTP " + response.status;
                        }}

                        throw new Error(message);
                    }}

                    window.location.href = "/level-records-view?cleared=1";
                }} catch (error) {{
                    errorText.textContent = error.message === "Incorrect delete password"
                        ? "Incorrect password. Please try again."
                        : error.message;
                    passwordInput.disabled = false;
                    cancelButton.disabled = false;
                    confirmButton.disabled = false;
                    confirmButton.textContent = "Clear Records";
                    passwordInput.focus();
                    passwordInput.select();
                }}
            }});
        }})();
    </script>
</body>
</html>"""


def render_level_row(level):
    start = get_level_start(level)
    end = get_level_end(level)
    source = value_or_dash(get_record_value(start, "source"))
    completed = get_record_value(end, "completed") if end else None
    status_class = "status-completed" if completed else "status-missing"
    status_text = "completed" if completed else "missing end"

    if end and not completed:
        status_text = value_or_dash(get_record_value(end, "endReason"))

    structure = get_record_value(start, "structure") or {}
    round_id = get_record_value(start, "gameRoundId") or get_record_value(end, "gameRoundId")
    round_text = (
        get_round_display_name(start, end)
        or (short_id(round_id) if round_id else "Legacy Round")
    )

    return f"""<tr>
    <td>{escape_text(round_text)}</td>
    <td>{escape_text(value_or_dash(get_record_value(start, "levelIndex") or get_record_value(end, "levelIndex")))}</td>
    <td>{escape_text(source)}</td>
    <td class="{status_class}">{escape_text(status_text)}</td>
    <td>{escape_text(format_seconds(get_record_value(end, "durationSeconds")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(end, "moveCount")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(end, "pushCount")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(end, "restartCount")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(start, "solutionSteps")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(start, "solverPushes")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(start, "generationAttempts")))}</td>
    <td>{escape_text(format_ratio(structure.get("wallDensity")))}</td>
    <td>{escape_text(format_ratio(structure.get("waterDensity")))}</td>
    <td>{escape_text(format_ratio(structure.get("deadCornerRisk")))}</td>
    <td><span class="small">{escape_text(value_or_dash(structure.get("mapHash")))}</span></td>
    <td><pre class="map">{render_map_rows(get_record_value(start, "rows"))}</pre></td>
</tr>"""


def get_level_sort_value(level):
    start = get_level_start(level)
    end = get_level_end(level)
    level_index = get_record_value(start, "levelIndex") or get_record_value(end, "levelIndex")

    if isinstance(level_index, int):
        return level_index

    return 999999


def get_round_level_sort_value(level):
    start = get_level_start(level)
    end = get_level_end(level)
    round_level_index = (
        get_record_value(start, "roundLevelIndex")
        or get_record_value(end, "roundLevelIndex")
    )

    if isinstance(round_level_index, int):
        return round_level_index

    return get_level_sort_value(level)


def get_level_started_at(level):
    start = get_level_start(level)
    end = get_level_end(level)
    return (
        get_record_value(start, "gameRoundStartedAt")
        or get_record_value(start, "timestamp")
        or get_record_value(start, "serverReceivedAt")
        or get_record_value(end, "timestamp")
        or get_record_value(end, "serverReceivedAt")
    )


def get_level_ended_at(level):
    start = get_level_start(level)
    end = get_level_end(level)
    return (
        get_record_value(end, "timestamp")
        or get_record_value(end, "serverReceivedAt")
        or get_record_value(start, "timestamp")
        or get_record_value(start, "serverReceivedAt")
        or get_record_value(start, "gameRoundStartedAt")
    )


def earliest_time(current_value, next_value):
    if not current_value:
        return next_value

    if not next_value:
        return current_value

    return min(str(current_value), str(next_value))


def latest_time(current_value, next_value):
    if not current_value:
        return next_value

    if not next_value:
        return current_value

    return max(str(current_value), str(next_value))


def get_level_start(level):
    return level.get("start")


def get_level_end(level):
    return level.get("end")


def get_record_value(record, key):
    if not isinstance(record, dict):
        return None

    return record.get(key)


def normalize_round_id(value):
    if value is None:
        return ""

    return str(value).strip()


def normalize_round_display_name(value):
    if value is None:
        return ""

    return str(value).strip()


def normalize_level_run_id(value):
    if value is None:
        return ""

    return str(value).strip()


def normalize_level_display_name(value):
    if value is None:
        return ""

    return str(value).strip()


def normalize_survey_identifier(value):
    if value is None:
        return ""

    return str(value).strip()


def normalize_creative_idea_identifier(value):
    if value is None:
        return ""

    text = str(value).strip()
    return "" if text == "-" else text


def normalize_expansion_choice_identifier(value):
    return normalize_creative_idea_identifier(value)


def build_expansion_choice_identifier(choice):
    if not isinstance(choice, dict):
        choice = {}

    components = [
        choice.get("serverReceivedAt"),
        choice.get("timestamp"),
        choice.get("ideaId"),
        choice.get("sessionId"),
        choice.get("gameRoundId"),
        choice.get("selectedOptionId"),
        choice.get("selectedOptionTitle"),
        choice.get("originalIdeaText"),
        choice.get("finalIdeaText"),
    ]
    encoded_components = [
        normalize_expansion_choice_identifier_component(component)
        for component in components
    ]
    payload = json.dumps(
        encoded_components,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return "choice-" + digest


def normalize_expansion_choice_identifier_component(value):
    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    return str(value).strip()


def get_survey_player_identifier(response):
    if not isinstance(response, dict):
        return ""

    return (
        normalize_survey_identifier(response.get("playerName"))
        or normalize_survey_identifier(response.get("playerNickname"))
        or normalize_survey_identifier(response.get("nickname"))
    )


def is_level_event_in_round(record, round_id):
    if not isinstance(record, dict):
        return False

    record_round_id = record.get("gameRoundId")

    if round_id == "legacy-round":
        return not record_round_id

    return str(record_round_id or "") == round_id


def get_round_display_name(start, end):
    for record in (end, start):
        value = get_record_value(record, "roundDisplayName")

        if value is not None:
            display_name = normalize_round_display_name(value)

            if display_name:
                return display_name

    return ""


def render_map_rows(rows):
    if not rows:
        return "-"

    return escape_text("\n".join(str(row) for row in rows))


def format_seconds(value):
    if isinstance(value, (int, float)):
        return f"{value:.1f}s"

    return "-"


def format_ratio(value):
    if isinstance(value, (int, float)):
        return f"{value:.3f}"

    return "-"


def value_or_dash(value):
    if value is None or value == "":
        return "-"

    return str(value)


def short_id(value):
    if not value:
        return "-"

    value = str(value)
    return value[:8]


def escape_text(value):
    return html.escape(str(value), quote=True)


def create_creative_idea_expansion(
    creative_context,
    request_id="",
    max_attempts=DEFAULT_LLM_MAX_ATTEMPTS,
):
    request_id = new_request_id(request_id)

    def validate_expansion(payload):
        return {
            "options": validate_creative_idea_expansion(payload),
            "usedFallback": False,
            "message": "",
        }

    try:
        return execute_json_request(
            task="creative_idea_expansion",
            messages=build_creative_idea_expansion_messages(creative_context),
            validator=validate_expansion,
            temperature=float(
                os.getenv(
                    "DEEPSEEK_EXPANSION_TEMPERATURE",
                    os.getenv("DEEPSEEK_TEMPERATURE", "0.9"),
                )
            ),
            timeout_seconds=SHORT_LLM_TIMEOUT_SECONDS,
            max_attempts=max_attempts,
            request_id=request_id,
            validation_stage="expansion_validation",
        )
    except LLMServiceError as exception:
        fallback = fallback_creative_idea_expansion(
            creative_context,
            exception.safe_message,
        )
        return LLMExecutionResult(
            fallback,
            exception.attempts_used,
            request_id,
        )


def validate_human_adjustment_clarity_payload(payload):
    payload = payload if isinstance(payload, dict) else {}

    def score(name):
        try:
            return max(0, min(2, int(payload.get(name, 0))))
        except (TypeError, ValueError):
            return 0

    problem_score = score("problemScore")
    target_score = score("targetScore")
    direction_score = score("directionScore")
    detail_score = score("detailScore")
    total_score = problem_score + target_score + direction_score + detail_score
    is_clear = total_score >= 4 and target_score >= 1 and direction_score >= 1
    reason = str(payload.get("reason") or "").strip()

    if not reason:
        reason = (
            "State which level feature should change and the direction of that change."
            if not is_clear
            else "The instruction contains an actionable user-directed revision."
        )

    return {
        "problemScore": problem_score,
        "targetScore": target_score,
        "directionScore": direction_score,
        "detailScore": detail_score,
        "totalScore": total_score,
        "isClear": is_clear,
        "reason": reason,
    }


def create_human_adjustment_clarity_check(
    adjustment_text,
    request_id="",
    max_attempts=DEFAULT_LLM_MAX_ATTEMPTS,
):
    required_fields = {
        "problemScore",
        "targetScore",
        "directionScore",
        "detailScore",
        "reason",
    }

    def validate_clarity(payload):
        if not isinstance(payload, dict):
            raise ValueError("Human clarity response must be a JSON object")

        missing_fields = sorted(required_fields - set(payload))

        if missing_fields:
            raise ValueError(
                "Human clarity response is missing fields: "
                + ", ".join(missing_fields)
            )

        return validate_human_adjustment_clarity_payload(payload)

    return execute_json_request(
        task="human_adjustment_clarity",
        messages=build_human_adjustment_clarity_messages(adjustment_text),
        validator=validate_clarity,
        temperature=0.0,
        timeout_seconds=SHORT_LLM_TIMEOUT_SECONDS,
        max_attempts=max_attempts,
        request_id=request_id,
        validation_stage="clarity_validation",
    )


def create_ha_revision_plans(
    context,
    request_id="",
    max_attempts=DEFAULT_LLM_MAX_ATTEMPTS,
):
    def validate_options(payload):
        options = validate_ha_revision_plan_options(
            payload,
            context.get("previousLevelPlan"),
            context.get("adjustmentText"),
        )
        return {"options": options}

    return execute_json_request(
        task="ha_revision_plans",
        messages=build_ha_revision_plan_messages(context),
        validator=validate_options,
        temperature=0.2,
        timeout_seconds=PLAN_LLM_TIMEOUT_SECONDS,
        max_attempts=max_attempts,
        request_id=request_id,
        validation_stage="ha_plan_validation",
    )


def create_ha_revision_plan_edit(
    context,
    request_id="",
    max_attempts=DEFAULT_LLM_MAX_ATTEMPTS,
):
    def validate_option(payload):
        option = validate_ha_revision_plan_edit(
            payload,
            context.get("originalOption"),
            context.get("previousLevelPlan"),
            context.get("adjustmentText"),
            context.get("editedDescription"),
        )
        return {"option": option}

    return execute_json_request(
        task="ha_revision_plan_edit",
        messages=build_ha_revision_plan_edit_messages(context),
        validator=validate_option,
        temperature=0.1,
        timeout_seconds=PLAN_LLM_TIMEOUT_SECONDS,
        max_attempts=max_attempts,
        request_id=request_id,
        validation_stage="ha_plan_edit_validation",
    )


def parse_ha_revision_contract(value):
    if isinstance(value, str):
        contract = json.loads(value)
    elif isinstance(value, dict):
        contract = dict(value)
    else:
        raise ValueError("HA promptText must contain a JSON object")

    if contract.get("preserveUnlisted") is not True:
        raise ValueError("HA contract preserveUnlisted must be true")

    changes = contract.get("changes")

    if not isinstance(changes, dict) or not changes:
        raise ValueError("HA contract changes must be a non-empty object")

    unknown_fields = set(changes) - HA_CHANGE_FIELDS

    if unknown_fields:
        raise ValueError(
            "HA contract contains unsupported fields: "
            + ", ".join(sorted(unknown_fields))
        )

    normalized_changes = {}

    for field, value in changes.items():
        if field in LIMITS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"HA contract {field} must be an integer")

            if field == "minWallObstacleBlocks" and value == 3:
                value = 2

            minimum, maximum = LIMITS[field]

            if value < minimum or value > maximum:
                raise ValueError(
                    f"HA contract {field}={value} is outside {minimum}-{maximum}"
                )

            normalized_changes[field] = value
        elif field == "corridorWidth":
            if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1, 2}:
                raise ValueError("HA contract corridorWidth must be 0, 1, or 2")

            normalized_changes[field] = value
        elif field in ENUMS:
            clean_value = str(value or "").strip()

            if clean_value not in ENUMS[field]:
                raise ValueError(
                    f"HA contract {field}={clean_value} is not supported"
                )

            normalized_changes[field] = clean_value
        elif field == "style":
            clean_value = str(value or "").strip()

            if not clean_value:
                raise ValueError("HA contract style cannot be empty")

            normalized_changes[field] = clean_value[:80]

    return {
        "changes": normalized_changes,
        "preserveUnlisted": True,
    }


def build_ha_contract_constraints(previous_plan, changes, adjustment_text):
    constraints = resolve_zero_feature_constraints(
        {"latestAdjustmentText": str(adjustment_text or "")}
    )
    previous_plan = previous_plan or {}
    water_fields = {"minWaterAreas", "maxWaterAreas"}
    wall_fields = {"minWallObstacleBlocks", "maxWallObstacleBlocks"}

    if not water_fields.intersection(changes):
        constraints["noWater"] = (
            previous_plan.get("minWaterAreas") == 0
            and previous_plan.get("maxWaterAreas") == 0
        )

    if not wall_fields.intersection(changes):
        constraints["noInternalWalls"] = (
            previous_plan.get("minWallObstacleBlocks") == 0
            and previous_plan.get("maxWallObstacleBlocks") == 0
        )

    return constraints


def validate_ha_revision_contract(contract, previous_plan, adjustment_text):
    if not isinstance(previous_plan, dict) or not previous_plan:
        raise ValueError("previous LevelDesignPlan is required")

    parsed = parse_ha_revision_contract(contract)
    candidate_wall_min = parsed["changes"].get(
        "minWallObstacleBlocks",
        previous_plan.get("minWallObstacleBlocks"),
    )
    candidate_wall_max = parsed["changes"].get(
        "maxWallObstacleBlocks",
        previous_plan.get("maxWallObstacleBlocks"),
    )

    if candidate_wall_min == 0 and candidate_wall_max == 0:
        parsed["changes"].update(
            {
                "corridorPlacement": "none",
                "corridorWidth": 0,
                "corridorOrientation": "any",
                "corridorRole": "visual_only",
                "corridorPriority": "preferred",
            }
        )

    candidate = dict(previous_plan)
    candidate.update(parsed["changes"])
    constraints = build_ha_contract_constraints(
        previous_plan,
        parsed["changes"],
        adjustment_text,
    )
    validated_candidate = validate_plan(candidate, constraints)
    parsed["changes"] = {
        field: validated_candidate[field]
        for field in parsed["changes"]
    }
    return parsed


def validate_ha_revision_plan_options(payload, previous_plan, adjustment_text):
    if payload is None:
        raise ValueError("model returned no HA revision payload")

    data = payload.model_dump() if hasattr(payload, "model_dump") else payload
    raw_options = data if isinstance(data, list) else dict(data).get("options")

    if not isinstance(raw_options, list) or len(raw_options) != 3:
        raise ValueError("HA revision payload must contain exactly three options")

    validated = []
    canonical_contracts = set()

    for index, raw_option in enumerate(raw_options):
        option = (
            raw_option.model_dump()
            if hasattr(raw_option, "model_dump")
            else dict(raw_option)
        )
        option_id = clean_expansion_text(option.get("id")) or "ABC"[index]
        title = clean_expansion_text(option.get("title"))[:64]
        description = clean_expansion_text(option.get("description"))[:420]
        contract = validate_ha_revision_contract(
            option.get("promptText"),
            previous_plan,
            adjustment_text,
        )
        prompt_text = json.dumps(
            contract,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        if not title:
            raise ValueError(f"HA option {index + 1} title is required")

        if not description:
            raise ValueError(f"HA option {index + 1} description is required")

        if prompt_text in canonical_contracts:
            raise ValueError("HA revision options must use distinct change contracts")

        canonical_contracts.add(prompt_text)
        validated.append(
            {
                "id": option_id[:12],
                "title": title,
                "description": description,
                "promptText": prompt_text,
            }
        )

    return validated


def validate_ha_revision_plan_edit(
    payload,
    original_option,
    previous_plan,
    adjustment_text,
    edited_description,
):
    if payload is None:
        raise ValueError("model returned no HA revision-plan edit payload")

    data = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
    original_option = dict(original_option or {})
    option_id = clean_expansion_text(original_option.get("id"))[:12]
    title = clean_expansion_text(original_option.get("title"))[:64]
    description = clean_expansion_text(data.get("description"))[:420]
    edit_intent = " ".join(
        value
        for value in (
            clean_expansion_text(adjustment_text),
            clean_expansion_text(edited_description),
        )
        if value
    )
    contract = validate_ha_revision_contract(
        data.get("promptText"),
        previous_plan,
        edit_intent,
    )
    original_contract = validate_ha_revision_contract(
        original_option.get("promptText"),
        previous_plan,
        adjustment_text,
    )

    if not option_id:
        raise ValueError("original HA option id is required")

    if not title:
        raise ValueError("original HA option title is required")

    if not description:
        raise ValueError("edited HA option description is required")

    if contract == original_contract:
        raise ValueError("edited HA option must produce a different change contract")

    prompt_text = json.dumps(
        contract,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "id": option_id,
        "title": title,
        "description": description,
        "promptText": prompt_text,
    }


def normalize_generation_preferences(raw_preferences):
    if raw_preferences is None:
        return {}

    data = (
        raw_preferences.model_dump()
        if hasattr(raw_preferences, "model_dump")
        else dict(raw_preferences)
    )
    normalized = {}

    for key, (minimum, maximum) in LIMITS.items():
        value = data.get(key)

        if value is None or value == -1:
            continue

        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"generationPreferences.{key} must be an integer")

        if value < minimum or value > maximum:
            raise ValueError(
                f"generationPreferences.{key}={value} is outside "
                f"{minimum}-{maximum}"
            )

        normalized[key] = value

    range_pairs = (
        ("minSolutionSteps", "maxSolutionSteps"),
        ("minPushes", "maxPushes"),
        ("minWaterAreas", "maxWaterAreas"),
        ("minWallObstacleBlocks", "maxWallObstacleBlocks"),
        ("minReversePulls", "maxReversePulls"),
    )

    for minimum_key, maximum_key in range_pairs:
        has_minimum = minimum_key in normalized
        has_maximum = maximum_key in normalized

        if has_minimum != has_maximum:
            raise ValueError(
                "generationPreferences must provide both "
                f"{minimum_key} and {maximum_key}"
            )

        if has_minimum and normalized[maximum_key] < normalized[minimum_key]:
            raise ValueError(
                f"generationPreferences.{maximum_key} must be >= "
                f"{minimum_key}"
            )

    for key, allowed_values in ENUMS.items():
        value = str(data.get(key) or "").strip()

        if not value:
            continue

        if value not in allowed_values:
            raise ValueError(
                f"generationPreferences.{key}={value} is not supported"
            )

        normalized[key] = value

    corridor_width = data.get("corridorWidth")

    if corridor_width not in (None, -1):
        if isinstance(corridor_width, bool) or not isinstance(
            corridor_width,
            int,
        ):
            raise ValueError(
                "generationPreferences.corridorWidth must be an integer"
            )

        normalized["corridorWidth"] = corridor_width

    corridor_placement = normalized.get("corridorPlacement")

    if corridor_placement:
        if corridor_placement == "none":
            if normalized.get("corridorWidth", 0) != 0:
                raise ValueError(
                    "generationPreferences.corridorWidth must be 0 when "
                    "corridorPlacement is none"
                )

            normalized["corridorWidth"] = 0
            normalized.setdefault("corridorOrientation", "any")
            normalized.setdefault("corridorRole", "visual_only")
            normalized.setdefault("corridorPriority", "preferred")
        else:
            if normalized.get("corridorWidth") not in {1, 2}:
                raise ValueError(
                    "generationPreferences.corridorWidth must be 1 or 2 "
                    "when a corridor is requested"
                )

            normalized.setdefault("corridorOrientation", "any")
            normalized.setdefault("corridorRole", "player_route")
            normalized.setdefault("corridorPriority", "preferred")
    elif "corridorWidth" in normalized:
        raise ValueError(
            "generationPreferences.corridorPlacement is required when "
            "corridorWidth is provided"
        )

    if (
        normalized.get("maxWallObstacleBlocks") == 0
        and normalized.get("corridorPlacement") not in (None, "none")
    ):
        raise ValueError(
            "generationPreferences cannot request a corridor with zero "
            "internal wall obstacles"
        )

    return normalized


def apply_generation_preferences(plan, generation_preferences):
    data = plan.model_dump() if isinstance(plan, LevelDesignPlan) else dict(plan)

    for key, value in (generation_preferences or {}).items():
        data[key] = value

    return data


def apply_selected_ha_plan(
    generated_plan,
    creative_context,
    feature_constraints,
    generation_preferences=None,
):
    creative_context = creative_context or {}

    if str(creative_context.get("revisionMode") or "").strip().lower() != "ha":
        candidate = apply_generation_preferences(
            generated_plan,
            generation_preferences,
        )
        effective_constraints = dict(feature_constraints or {})

        if (
            candidate.get("minWaterAreas") == 0
            and candidate.get("maxWaterAreas") == 0
        ):
            effective_constraints["noWater"] = True

        if (
            candidate.get("minWallObstacleBlocks") == 0
            and candidate.get("maxWallObstacleBlocks") == 0
        ):
            effective_constraints["noInternalWalls"] = True

        return validate_plan(
            candidate,
            effective_constraints,
            generation_preferences,
        )

    selected_text = str(creative_context.get("selectedHAPlan") or "").strip()

    if not selected_text:
        candidate = apply_generation_preferences(
            generated_plan,
            generation_preferences,
        )
        return validate_plan(
            candidate,
            feature_constraints,
            generation_preferences,
        )

    previous_value = creative_context.get("previousLevelPlan")
    previous_plan = (
        json.loads(previous_value)
        if isinstance(previous_value, str)
        else previous_value
    )
    selected_option = json.loads(selected_text)
    contract = validate_ha_revision_contract(
        selected_option.get("promptText"),
        previous_plan,
        creative_context.get("latestAdjustmentText"),
    )
    candidate = dict(previous_plan)
    candidate.update(contract["changes"])
    title = clean_expansion_text(selected_option.get("title"))
    description = clean_expansion_text(selected_option.get("description"))
    candidate["designNote"] = (
        "Human-AI revision: "
        + (title or "selected plan")
        + ". "
        + description
    )[:160]
    candidate = apply_generation_preferences(
        candidate,
        generation_preferences,
    )
    effective_constraints = dict(feature_constraints or {})

    if (
        candidate.get("minWaterAreas") == 0
        and candidate.get("maxWaterAreas") == 0
    ):
        effective_constraints["noWater"] = True

    if (
        candidate.get("minWallObstacleBlocks") == 0
        and candidate.get("maxWallObstacleBlocks") == 0
    ):
        effective_constraints["noInternalWalls"] = True

    return validate_plan(
        candidate,
        effective_constraints,
        generation_preferences,
    )

def create_level_plan(
    creative_context=None,
    request_id="",
    max_attempts=DEFAULT_LLM_MAX_ATTEMPTS,
):
    creative_context = dict(creative_context or {})
    creative_context["styleDescription"] = str(
        creative_context.get("styleDescription") or ""
    ).strip()[:420]
    generation_preferences = normalize_generation_preferences(
        creative_context.get("generationPreferences")
    )
    creative_context["generationPreferences"] = generation_preferences
    feature_constraints = resolve_zero_feature_constraints(creative_context)

    if "minWaterAreas" in generation_preferences:
        feature_constraints["noWater"] = (
            generation_preferences["minWaterAreas"] == 0
            and generation_preferences["maxWaterAreas"] == 0
        )

    if "minWallObstacleBlocks" in generation_preferences:
        feature_constraints["noInternalWalls"] = (
            generation_preferences["minWallObstacleBlocks"] == 0
            and generation_preferences["maxWallObstacleBlocks"] == 0
        )

    variation_seed = int(time.time() * 1000)

    def validate_level_plan(payload):
        return apply_selected_ha_plan(
            payload,
            creative_context,
            feature_constraints,
            generation_preferences,
        )

    execution = execute_json_request(
        task="level_plan",
        messages=build_level_plan_messages(
            variation_seed,
            get_recent_blueprint_hint(),
            creative_context,
            feature_constraints,
        ),
        validator=validate_level_plan,
        temperature=float(os.getenv("DEEPSEEK_LEVEL_TEMPERATURE", "0.5")),
        timeout_seconds=PLAN_LLM_TIMEOUT_SECONDS,
        max_attempts=max_attempts,
        request_id=request_id,
        validation_stage="blueprint_validation",
    )
    remember_blueprint(execution.value)
    return execution


def create_pc_level_candidate(
    context,
    request_id="",
    max_attempts=DEFAULT_LLM_MAX_ATTEMPTS,
    context_is_normalized=False,
):
    if not context_is_normalized:
        context = normalize_pc_level_request(context)

    def validate_candidate(payload):
        layout_candidate_id = parse_pc_layout_candidate_selection(payload)
        candidate = next(
            (
                item
                for item in context.get("layoutCandidates", [])
                if item["id"] == layout_candidate_id
            ),
            None,
        )

        if candidate is None:
            raise ValueError(
                f"layoutCandidateId {layout_candidate_id} is not available"
            )

        return {
            "rows": list(candidate["rows"]),
        }

    fallback_candidate = context["layoutCandidates"][0]

    try:
        return execute_json_request(
            task="pc_level_generation",
            messages=build_pc_level_generation_messages(context),
            validator=validate_candidate,
            temperature=float(
                os.getenv("DEEPSEEK_PC_LEVEL_TEMPERATURE", "0.15")
            ),
            timeout_seconds=float(
                os.getenv(
                    "DEEPSEEK_PC_LEVEL_TIMEOUT_SECONDS",
                    str(PC_LEVEL_LLM_TIMEOUT_SECONDS),
                )
            ),
            max_attempts=1,
            thinking_mode="disabled",
            retry_error_codes=set(),
            request_id=request_id,
            validation_stage="pc_level_validation",
        )
    except LLMServiceError as exception:
        log_event(
            "INFO",
            "pc_layout_fallback_used",
            requestId=request_id or exception.request_id,
            reasonCode=exception.code,
            stage=exception.stage,
            layoutCandidateId=fallback_candidate["id"],
            internalWallCount=len(
                fallback_candidate["internalWallCellIds"]
            ),
        )
        return LLMExecutionResult(
            {"rows": list(fallback_candidate["rows"])},
            max(1, exception.attempts_used),
            request_id or exception.request_id,
        )


def parse_pc_layout_candidate_selection(payload):
    if not isinstance(payload, dict) or set(payload) != {"layoutCandidateId"}:
        raise ValueError(
            "PC layout selection must contain only layoutCandidateId"
        )

    layout_candidate_id = payload.get("layoutCandidateId")

    if isinstance(layout_candidate_id, bool) or not isinstance(
        layout_candidate_id,
        int,
    ):
        raise ValueError("layoutCandidateId must be an integer")

    return layout_candidate_id


def build_pc_level_candidate(
    layout,
    context,
    request_id="",
    minimum_internal_walls=0,
    minimum_water_areas=0,
):
    water_area_id, player_cell_id, internal_wall_cell_ids = (
        parse_pc_indexed_layout(layout)
    )

    width = context["width"]
    height = context["height"]
    sketch_rows = context["sketchRows"]
    enclosed = find_pc_enclosed_cells(sketch_rows, width, height)
    rows = [
        [
            "." if enclosed[y][x] and sketch_rows[y][x] == " " else sketch_rows[y][x]
            for x in range(width)
        ]
        for y in range(height)
    ]
    editable_cells = {
        cell["id"]: cell
        for cell in context.get("editableCells", [])
    }
    allowed_water_areas = {
        area["id"]: area
        for area in context.get("allowedWaterAreas", [])
    }
    water_area = allowed_water_areas.get(water_area_id)

    if water_area is None:
        raise ValueError(
            f"waterAreaId {water_area_id} is not an allowed water area ID"
        )

    water_cell_ids = set(water_area.get("cellIds") or [])
    player_cell = editable_cells.get(player_cell_id)

    if player_cell is None or not player_cell.get("canPlacePlayer"):
        raise ValueError(
            f"playerCellId {player_cell_id} is not an allowed player cell ID"
        )

    if player_cell_id in water_cell_ids:
        raise ValueError(
            f"playerCellId {player_cell_id} overlaps waterAreaId {water_area_id}"
        )

    if len(set(internal_wall_cell_ids)) != len(internal_wall_cell_ids):
        raise ValueError("internalWallCellIds must not contain duplicate IDs")

    if player_cell_id in internal_wall_cell_ids:
        raise ValueError(
            f"internalWallCellIds cannot contain playerCellId {player_cell_id}"
        )

    overlapping_water_ids = sorted(
        set(internal_wall_cell_ids) & water_cell_ids
    )

    if overlapping_water_ids:
        raise ValueError(
            "internalWallCellIds overlap the selected water area at cell IDs "
            + ",".join(str(value) for value in overlapping_water_ids)
        )

    wall_cells = []

    for cell_id in internal_wall_cell_ids:
        cell = editable_cells.get(cell_id)

        if cell is None or not cell.get("canPlaceWall"):
            raise ValueError(
                f"internalWallCellId {cell_id} is not an allowed wall cell ID"
            )

        wall_cells.append(cell)

    compatible_wall_cell_ids = set(
        water_area.get("compatibleWallCellIds") or []
    )
    incompatible_wall_ids = sorted(
        set(internal_wall_cell_ids) - compatible_wall_cell_ids
    )

    if incompatible_wall_ids:
        raise ValueError(
            "internalWallCellIds must come from compatibleWallCellIds for "
            f"waterAreaId {water_area_id}; incompatible IDs "
            + ",".join(str(value) for value in incompatible_wall_ids)
        )

    for cell_id in water_cell_ids:
        cell = editable_cells.get(cell_id)

        if cell is None:
            raise ValueError(
                f"waterAreaId {water_area_id} references unknown cell ID {cell_id}"
            )

        rows[cell["y"]][cell["x"]] = "@"

    rows[player_cell["y"]][player_cell["x"]] = "p"

    for cell in wall_cells:
        rows[cell["y"]][cell["x"]] = "#"

    candidate = validate_pc_level_candidate(
        {"rows": ["".join(row) for row in rows]},
        context,
    )
    validate_pc_required_features(
        candidate["rows"],
        sketch_rows,
        width,
        height,
        minimum_internal_walls,
        minimum_water_areas,
    )
    try:
        validate_pc_completed_level_solvability(
            candidate["rows"],
            width,
            height,
        )
    except PCSolvabilityError as exception:
        feedback = build_pc_solvability_rejection_feedback(
            layout,
            context,
            candidate["rows"],
            exception,
        )
        fields = {
            "reasonCode": exception.reason_code,
            "waterAreaId": water_area_id,
            "playerCellId": player_cell_id,
            "internalWallCellIds": internal_wall_cell_ids,
            "searchedStates": exception.searched_states,
            "detail": safe_log_text(feedback),
        }

        if request_id:
            fields["requestId"] = request_id

        log_event("INFO", "pc_candidate_rejected", **fields)
        raise ValueError(feedback) from exception

    return candidate


def build_pc_solvability_rejection_feedback(
    layout,
    context,
    rows,
    exception,
):
    water_area_id, player_cell_id, internal_wall_cell_ids = (
        parse_pc_indexed_layout(layout)
    )
    static_codes = {
        "BOX_NO_TARGET_ROUTE",
        "TARGET_MATCHING_IMPOSSIBLE",
        "NO_LEGAL_INITIAL_PUSH",
    }
    blocking_wall_cell_ids = []
    water_blocks_static_route = False

    if exception.reason_code in static_codes:
        walkable = {
            (x, y)
            for y in range(context["height"])
            for x in range(context["width"])
            if rows[y][x] in {".", "p", "s", "t"}
        }
        start_boxes = tuple(
            sorted(
                (x, y)
                for y in range(context["height"])
                for x in range(context["width"])
                if rows[y][x] == "s"
            )
        )
        targets = {
            (x, y)
            for y in range(context["height"])
            for x in range(context["width"])
            if rows[y][x] == "t"
        }
        players = [
            (x, y)
            for y in range(context["height"])
            for x in range(context["width"])
            if rows[y][x] == "p"
        ]
        editable_cells = {
            cell["id"]: cell
            for cell in context["editableCells"]
        }

        for cell_id in internal_wall_cell_ids:
            cell = editable_cells[cell_id]
            diagnostic = diagnose_pc_static_solvability(
                walkable | {(cell["x"], cell["y"])},
                start_boxes,
                targets,
                players,
            )

            if diagnostic is None:
                blocking_wall_cell_ids.append(cell_id)

        water_area = next(
            area
            for area in context["allowedWaterAreas"]
            if area["id"] == water_area_id
        )
        water_cells = {
            (
                editable_cells[cell_id]["x"],
                editable_cells[cell_id]["y"],
            )
            for cell_id in water_area["cellIds"]
        }
        water_blocks_static_route = diagnose_pc_static_solvability(
            walkable | water_cells,
            start_boxes,
            targets,
            players,
        ) is None

    detail = describe_pc_solvability_diagnostic(
        {
            "code": exception.reason_code,
            **exception.details,
        }
    )

    if water_blocks_static_route:
        correction = "choose a different waterAreaId"
    elif blocking_wall_cell_ids:
        correction = "replace the listed blocking internal wall cell IDs"
    elif exception.reason_code == "NO_LEGAL_INITIAL_PUSH":
        correction = "change playerCellId or remove obstacles near a box"
    elif exception.reason_code == "SEARCH_BUDGET_EXCEEDED":
        correction = "choose a simpler water and wall arrangement"
    else:
        correction = "change waterAreaId, playerCellId, or internalWallCellIds"

    parts = [
        "candidate has no Sokoban solution",
        f"reasonCode={exception.reason_code}",
        f"waterAreaId={water_area_id}",
        f"playerCellId={player_cell_id}",
        "internalWallCellIds=["
        + ",".join(str(value) for value in internal_wall_cell_ids)
        + "]",
        f"detail={detail}",
    ]

    if exception.searched_states:
        parts.append(f"searchedStates={exception.searched_states}")

    if blocking_wall_cell_ids:
        parts.append(
            "blockingWallCellIds=["
            + ",".join(str(value) for value in blocking_wall_cell_ids)
            + "]"
        )

    if water_blocks_static_route:
        parts.append("selectedWaterBlocksStaticRoute=true")

    parts.append(f"correction={correction}")
    return "; ".join(parts)


def parse_pc_indexed_layout(layout):
    if layout is None or not isinstance(layout, dict):
        raise ValueError("model returned no PC indexed layout")

    required_fields = {
        "waterAreaId",
        "playerCellId",
        "internalWallCellIds",
    }

    if set(layout) != required_fields:
        raise ValueError(
            "PC indexed layout must contain only waterAreaId, playerCellId, "
            "and internalWallCellIds"
        )

    internal_wall_cell_ids = layout.get("internalWallCellIds")

    if not isinstance(internal_wall_cell_ids, list):
        raise ValueError("internalWallCellIds must be an array")

    return (
        parse_pc_integer(layout.get("waterAreaId"), "waterAreaId"),
        parse_pc_integer(layout.get("playerCellId"), "playerCellId"),
        [
            parse_pc_integer(value, f"internalWallCellIds[{index}]")
            for index, value in enumerate(internal_wall_cell_ids)
        ],
    )


def parse_pc_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")

    return value


def normalize_pc_level_request(context):
    context = dict(context or {})
    width = context.get("width")
    height = context.get("height")
    rows = context.get("sketchRows")

    if width != 12 or height != 10:
        raise ValueError("PC sketch size must be exactly 12x10")

    validate_pc_rows(rows, width, height, {" ", "#", "s", "t"}, "sketchRows")
    box_count = sum(row.count("s") for row in rows)
    target_count = sum(row.count("t") for row in rows)

    if box_count < 1 or box_count > 2 or box_count != target_count:
        raise ValueError("PC sketch must contain one or two matching s/t pairs")

    enclosed = find_pc_enclosed_cells(rows, width, height)
    enclosed_cells = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if enclosed[y][x]
    }

    if len(enclosed_cells) < PC_DESIGN_MIN_ACTIVITY_AREA:
        raise ValueError(
            "PC sketch enclosed activity area must contain at least "
            f"{PC_DESIGN_MIN_ACTIVITY_AREA} cells"
        )

    if any(
        rows[y][x] in {"s", "t"} and (x, y) not in enclosed_cells
        for y in range(height)
        for x in range(width)
    ):
        raise ValueError("Every s and t must be inside the enclosed activity area")

    if count_pc_components(enclosed_cells) != 1:
        raise ValueError("PC sketch must contain exactly one enclosed activity area")

    validate_pc_start_clearance(rows, width, height)

    previous_rows = context.get("previousCandidateRows")

    # Unity JsonUtility serializes a null string array as an empty array in the
    # first request. Treat that representation as the optional field being
    # absent; non-empty retry candidates must still be complete 12x10 maps.
    if previous_rows == []:
        previous_rows = None

    if previous_rows is not None:
        validate_pc_rows(
            previous_rows,
            width,
            height,
            {" ", "#", ".", "@", "p", "s", "t"},
            "previousCandidateRows",
        )

    box_starts = [
        [x, y]
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "s"
    ]
    targets = [
        [x, y]
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "t"
    ]
    editable_coordinates = [
        [x, y]
        for y in range(height)
        for x in range(width)
        if enclosed[y][x] and rows[y][x] == " "
    ]
    cells_next_to_box_starts = {
        (neighbor_x, neighbor_y)
        for start_x, start_y in box_starts
        for neighbor_x, neighbor_y in (
            (start_x + 1, start_y),
            (start_x - 1, start_y),
            (start_x, start_y + 1),
            (start_x, start_y - 1),
        )
    }
    outer_shell_cells = find_pc_outer_shell_cells(
        rows,
        width,
        height,
    )
    cells_next_to_outer_shell = {
        (wall_x + direction_x, wall_y + direction_y)
        for wall_x, wall_y in outer_shell_cells
        for direction_x, direction_y in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
        )
    }
    allowed_wall_coordinates = [
        coordinate
        for coordinate in editable_coordinates
        if (
            tuple(coordinate) not in cells_next_to_box_starts
            and tuple(coordinate) not in cells_next_to_outer_shell
        )
    ]
    allowed_wall_coordinates = filter_pc_static_wall_coordinates(
        enclosed_cells,
        box_starts,
        targets,
        allowed_wall_coordinates,
    )
    allowed_wall_coordinate_set = {
        tuple(coordinate)
        for coordinate in allowed_wall_coordinates
    }
    wall_impact_scores = calculate_pc_wall_impact_scores(
        enclosed_cells,
        box_starts,
        targets,
        allowed_wall_coordinates,
    )
    editable_cells = [
        {
            "id": index,
            "x": coordinate[0],
            "y": coordinate[1],
            "canPlacePlayer": True,
            "canPlaceWall": tuple(coordinate) in allowed_wall_coordinate_set,
            "wallImpactScore": wall_impact_scores.get(tuple(coordinate), 0),
        }
        for index, coordinate in enumerate(editable_coordinates)
    ]
    all_allowed_water_areas = enumerate_pc_allowed_water_areas(
        rows,
        enclosed_cells,
        width,
        height,
    )

    ranked_water_areas = select_pc_water_area_candidates(
        all_allowed_water_areas,
        editable_cells,
        box_starts,
        targets,
        -1,
        len(all_allowed_water_areas),
    )
    ranked_water_areas = attach_pc_compatible_wall_ids(
        ranked_water_areas,
        editable_cells,
    )
    layout_candidates, checked_water_areas, searched_states = (
        build_pc_safe_layout_candidates(
            rows,
            enclosed_cells,
            ranked_water_areas[:PC_LAYOUT_MAX_WATER_CHECKS],
            editable_cells,
            box_starts,
            targets,
            previous_rows,
            width,
            height,
        )
    )

    if not layout_candidates:
        raise ValueError(
            "PC sketch has no safe completion with at least three internal "
            "walls and one water area"
        )

    log_event(
        "INFO",
        "pc_layout_prefilter_completed",
        checkedWaterAreas=checked_water_areas,
        layoutCandidateCount=len(layout_candidates),
        searchedStates=searched_states,
        preferredWallCount=len(layout_candidates[0]["internalWallCellIds"]),
    )

    return {
        "width": width,
        "height": height,
        "sketchRows": list(rows),
        "boxStarts": box_starts,
        "targets": targets,
        "editableCells": editable_cells,
        "editableCoordinates": editable_coordinates,
        "allowedWallCoordinates": allowed_wall_coordinates,
        "outerShellCells": [
            [x, y]
            for x, y in sorted(
                outer_shell_cells,
                key=lambda position: (position[1], position[0]),
            )
        ],
        "allowedWaterAreas": ranked_water_areas,
        "layoutCandidates": layout_candidates,
        "previousCandidateRows": (
            list(previous_rows) if previous_rows is not None else None
        ),
        "rejectionReason": str(context.get("rejectionReason") or "").strip()[:500],
    }


def validate_pc_level_candidate(payload, context):
    if payload is None or not isinstance(payload, dict):
        raise ValueError("model returned no PC level candidate")

    if set(payload) != {"rows"}:
        raise ValueError("PC level response must contain only rows")

    width = context["width"]
    height = context["height"]
    sketch_rows = context["sketchRows"]
    rows = payload.get("rows")
    validate_pc_rows(
        rows,
        width,
        height,
        {" ", "#", ".", "@", "p", "s", "t"},
        "rows",
    )
    enclosed = find_pc_enclosed_cells(sketch_rows, width, height)
    normalized_rows = [list(row) for row in rows]

    for y in range(height):
        for x in range(width):
            if (
                enclosed[y][x]
                and sketch_rows[y][x] == " "
                and normalized_rows[y][x] == " "
            ):
                normalized_rows[y][x] = "."

    rows = ["".join(row) for row in normalized_rows]

    for y in range(height):
        for x in range(width):
            source = sketch_rows[y][x]
            candidate = rows[y][x]

            if source in {"#", "s", "t"} and candidate != source:
                raise ValueError(f"candidate changed fixed tile at ({x},{y})")

            if not enclosed[y][x] and candidate != source:
                raise ValueError(
                    f"candidate changed a tile outside the activity area at ({x},{y})"
                )

            if enclosed[y][x] and source == " " and candidate not in {".", "#", "@", "p"}:
                raise ValueError(f"candidate left incomplete tile at ({x},{y})")

    for tile, label in (("s", "box starts"), ("t", "targets")):
        if sum(row.count(tile) for row in rows) != sum(
            row.count(tile) for row in sketch_rows
        ):
            raise ValueError(f"candidate changed the number of {label}")

    if sum(row.count("p") for row in rows) != 1:
        raise ValueError("candidate must contain exactly one p")

    validate_pc_water_wall_clearance(rows, width, height)
    validate_pc_generated_wall_shapes(
        rows,
        sketch_rows,
        width,
        height,
    )
    validate_pc_start_clearance(rows, width, height)
    validate_pc_candidate_activity(rows, width, height)

    return {"rows": list(rows)}


def validate_pc_water_wall_clearance(rows, width, height):
    for y in range(1, height):
        for x in range(width):
            if rows[y][x] == "@" and rows[y - 1][x] == "#":
                raise ValueError(
                    "water tile at "
                    f"({x},{y}) cannot have a wall directly above it"
                )


def validate_pc_generated_wall_shapes(
    rows,
    sketch_rows,
    width,
    height,
):
    generated_walls = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "#" and sketch_rows[y][x] != "#"
    }

    if contains_pc_two_by_two_block(generated_walls):
        raise ValueError(
            "generated internal walls must not contain a complete 2x2 block"
        )


def validate_pc_candidate_activity(rows, width, height):
    validate_pc_water_rectangles(rows, width, height)
    walkable = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] in {".", "p", "s", "t"}
    }

    if count_pc_components(walkable) != 1:
        raise ValueError("candidate walkable cells must form one connected component")


def validate_pc_required_features(
    rows,
    sketch_rows,
    width,
    height,
    minimum_internal_walls,
    minimum_water_areas,
):
    internal_wall_count = sum(
        1
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "#" and sketch_rows[y][x] != "#"
    )
    water_area_count = validate_pc_water_rectangles(rows, width, height)

    if internal_wall_count < minimum_internal_walls:
        raise ValueError(
            "candidate retained "
            f"{internal_wall_count} internal wall tiles; at least "
            f"{minimum_internal_walls} are required"
        )

    if water_area_count < minimum_water_areas:
        raise ValueError(
            "candidate retained "
            f"{water_area_count} water areas; at least "
            f"{minimum_water_areas} are required"
        )

    return internal_wall_count, water_area_count


def enumerate_pc_allowed_water_areas(
    sketch_rows,
    enclosed_cells,
    width,
    height,
):
    editable_cells = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if sketch_rows[y][x] == " " and (x, y) in enclosed_cells
    }
    allowed_areas = []

    for y in range(height):
        for x in range(width):
            for area_height in range(2, 5):
                for area_width in range(2, 5):
                    positions = {
                        (cell_x, cell_y)
                        for cell_y in range(y, y + area_height)
                        for cell_x in range(x, x + area_width)
                    }

                    if not positions.issubset(editable_cells):
                        continue

                    if any(
                        cell_y > 0
                        and sketch_rows[cell_y - 1][cell_x] == "#"
                        for cell_x, cell_y in positions
                    ):
                        continue

                    remaining_walkable = enclosed_cells - positions

                    if count_pc_components(remaining_walkable) != 1:
                        continue

                    allowed_areas.append(
                        {
                            "id": len(allowed_areas),
                            "x": x,
                            "y": y,
                            "width": area_width,
                            "height": area_height,
                        }
                    )

    return allowed_areas


def calculate_pc_wall_impact_scores(
    enclosed_cells,
    box_starts,
    targets,
    allowed_wall_coordinates,
):
    walkable = set(enclosed_cells)
    starts = [tuple(position) for position in box_starts]
    target_cells = [tuple(position) for position in targets]
    baseline_distances = {
        (start, target): find_pc_shortest_grid_distance(
            start,
            target,
            walkable,
        )
        for start in starts
        for target in target_cells
    }
    unreachable_penalty = max(1, len(walkable))
    scores = {}

    for coordinate in allowed_wall_coordinates:
        wall = tuple(coordinate)
        walkable_without_wall = walkable - {wall}
        score = 0

        for pair, baseline_distance in baseline_distances.items():
            if baseline_distance is None:
                continue

            changed_distance = find_pc_shortest_grid_distance(
                pair[0],
                pair[1],
                walkable_without_wall,
            )

            if changed_distance is None:
                score += unreachable_penalty
            else:
                score += max(0, changed_distance - baseline_distance)

        scores[wall] = score

    return scores


def find_pc_shortest_grid_distance(start, target, walkable):
    if start not in walkable or target not in walkable:
        return None

    open_cells = deque([(start, 0)])
    visited = {start}

    while open_cells:
        current, distance = open_cells.popleft()

        if current == target:
            return distance

        x, y = current

        for neighbor in (
            (x + 1, y),
            (x - 1, y),
            (x, y + 1),
            (x, y - 1),
        ):
            if neighbor in walkable and neighbor not in visited:
                visited.add(neighbor)
                open_cells.append((neighbor, distance + 1))

    return None


def filter_pc_static_wall_coordinates(
    enclosed_cells,
    box_starts,
    targets,
    allowed_wall_coordinates,
):
    start_boxes = tuple(sorted(tuple(position) for position in box_starts))
    target_set = {tuple(position) for position in targets}
    filtered = []

    for coordinate in allowed_wall_coordinates:
        wall = tuple(coordinate)
        remaining_walkable = set(enclosed_cells) - {wall}

        if count_pc_components(remaining_walkable) != 1:
            continue

        diagnostic = diagnose_pc_static_solvability(
            remaining_walkable,
            start_boxes,
            target_set,
        )

        if diagnostic is None:
            filtered.append(list(coordinate))

    return filtered


def select_pc_water_area_candidates(
    allowed_water_areas,
    editable_cells,
    box_starts,
    targets,
    preferred_area_id,
    limit=PC_MAX_WATER_AREA_CANDIDATES,
):
    limit = max(1, int(limit))
    coordinate_to_cell_id = {
        (cell["x"], cell["y"]): cell["id"]
        for cell in editable_cells
    }
    wall_cell_ids = {
        cell["id"]
        for cell in editable_cells
        if cell.get("canPlaceWall")
    }
    fixed_positions = [
        tuple(position)
        for position in list(box_starts) + list(targets)
    ]
    enriched_areas = []

    for area in allowed_water_areas:
        cell_ids = [
            coordinate_to_cell_id[(cell_x, cell_y)]
            for cell_y in range(area["y"], area["y"] + area["height"])
            for cell_x in range(area["x"], area["x"] + area["width"])
        ]
        water_positions = [
            (cell_x, cell_y)
            for cell_y in range(area["y"], area["y"] + area["height"])
            for cell_x in range(area["x"], area["x"] + area["width"])
        ]
        minimum_fixed_distance = min(
            (
                abs(cell_x - fixed_x) + abs(cell_y - fixed_y)
                for cell_x, cell_y in water_positions
                for fixed_x, fixed_y in fixed_positions
            ),
            default=999,
        )
        enriched_areas.append(
            {
                **area,
                "cellIds": cell_ids,
                "_areaSize": len(cell_ids),
                "_minimumFixedDistance": minimum_fixed_distance,
                "_remainingWallOptions": len(wall_cell_ids - set(cell_ids)),
            }
        )

    groups = {}

    for area in enriched_areas:
        groups.setdefault(area["_areaSize"], []).append(area)

    for group in groups.values():
        group.sort(
            key=lambda area: (
                -area["_minimumFixedDistance"],
                -area["_remainingWallOptions"],
                area["y"],
                area["x"],
                area["height"],
                area["width"],
            )
        )

    selected = []
    preferred_area = next(
        (
            area
            for area in enriched_areas
            if area["id"] == preferred_area_id
        ),
        None,
    )

    if preferred_area is not None:
        selected.append(preferred_area)

    group_offsets = {
        area_size: 0
        for area_size in groups
    }
    area_sizes = sorted(groups)

    while len(selected) < min(limit, len(enriched_areas)):
        added = False

        for area_size in area_sizes:
            group = groups[area_size]
            offset = group_offsets[area_size]

            while offset < len(group) and group[offset] in selected:
                offset += 1

            group_offsets[area_size] = offset

            if offset >= len(group):
                continue

            selected.append(group[offset])
            group_offsets[area_size] += 1
            added = True

            if len(selected) >= min(limit, len(enriched_areas)):
                break

        if not added:
            break

    return [
        {
            "id": index,
            "x": area["x"],
            "y": area["y"],
            "width": area["width"],
            "height": area["height"],
            "cellIds": list(area["cellIds"]),
        }
        for index, area in enumerate(selected)
    ]


def attach_pc_compatible_wall_ids(allowed_water_areas, editable_cells):
    allowed_wall_ids = {
        cell["id"]
        for cell in editable_cells
        if cell.get("canPlaceWall")
    }
    editable_by_position = {
        (cell["x"], cell["y"]): cell["id"]
        for cell in editable_cells
    }
    result = []

    for area in allowed_water_areas:
        water_cell_ids = set(area.get("cellIds") or [])
        water_positions = {
            (cell["x"], cell["y"])
            for cell in editable_cells
            if cell["id"] in water_cell_ids
        }
        wall_ids_above_water = {
            editable_by_position[(water_x, water_y - 1)]
            for water_x, water_y in water_positions
            if (water_x, water_y - 1) in editable_by_position
        }
        result.append(
            {
                **area,
                "compatibleWallCellIds": sorted(
                    allowed_wall_ids
                    - water_cell_ids
                    - wall_ids_above_water
                ),
            }
        )

    return result


def build_pc_safe_layout_candidates(
    sketch_rows,
    enclosed_cells,
    allowed_water_areas,
    editable_cells,
    box_starts,
    targets,
    previous_rows,
    width,
    height,
):
    started = time.perf_counter()
    budget = {
        "deadline": started + PC_LAYOUT_PREFILTER_SECONDS,
        "remainingStates": PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES,
        "searchedStates": 0,
    }
    editable_by_id = {
        cell["id"]: cell
        for cell in editable_cells
    }
    start_boxes = tuple(sorted(tuple(position) for position in box_starts))
    target_cells = {tuple(position) for position in targets}
    candidates = []
    checked_water_areas = 0

    for area in allowed_water_areas[:PC_LAYOUT_MAX_WATER_CHECKS]:
        if (
            len(candidates) >= PC_MAX_LAYOUT_CANDIDATES
            or time.perf_counter() >= budget["deadline"]
            or budget["remainingStates"] <= 0
        ):
            break

        checked_water_areas += 1
        water_cell_ids = set(area.get("cellIds") or [])
        water_positions = {
            (
                editable_by_id[cell_id]["x"],
                editable_by_id[cell_id]["y"],
            )
            for cell_id in water_cell_ids
            if cell_id in editable_by_id
        }
        walkable = set(enclosed_cells) - water_positions

        if count_pc_components(walkable) != 1:
            continue

        if diagnose_pc_static_solvability(
            walkable,
            start_boxes,
            target_cells,
        ) is not None:
            continue

        player_and_trace = find_pc_safe_player_and_solution_trace(
            walkable,
            start_boxes,
            target_cells,
            editable_cells,
            water_cell_ids,
            budget,
        )

        if player_and_trace is None:
            continue

        player_cell_id, protected_positions = player_and_trace
        compatible_wall_cell_ids = set(
            area.get("compatibleWallCellIds") or []
        )
        safe_wall_cells = [
            cell
            for cell in editable_cells
            if (
                cell.get("canPlaceWall")
                and cell["id"] in compatible_wall_cell_ids
                and cell["id"] not in water_cell_ids
                and cell["id"] != player_cell_id
                and (cell["x"], cell["y"]) not in protected_positions
            )
        ]
        wall_groups = build_pc_greedy_wall_groups(
            safe_wall_cells,
            walkable,
        )

        for wall_group in wall_groups:
            if len(candidates) >= PC_MAX_LAYOUT_CANDIDATES:
                break

            rows = assemble_pc_layout_rows(
                sketch_rows,
                enclosed_cells,
                editable_by_id,
                water_cell_ids,
                player_cell_id,
                wall_group["internalWallCellIds"],
                width,
                height,
            )
            candidate = validate_pc_level_candidate(
                {"rows": rows},
                {
                    "width": width,
                    "height": height,
                    "sketchRows": sketch_rows,
                },
            )
            player_cell = editable_by_id[player_cell_id]
            wall_cells = [
                editable_by_id[cell_id]
                for cell_id in wall_group["internalWallCellIds"]
            ]
            candidates.append(
                {
                    "id": len(candidates),
                    "waterAreaId": area["id"],
                    "playerCellId": player_cell_id,
                    "internalWallCellIds": list(
                        wall_group["internalWallCellIds"]
                    ),
                    "wallStyle": wall_group["wallStyle"],
                    "waterArea": {
                        "x": area["x"],
                        "y": area["y"],
                        "width": area["width"],
                        "height": area["height"],
                    },
                    "player": {
                        "x": player_cell["x"],
                        "y": player_cell["y"],
                    },
                    "internalWalls": [
                        {"x": cell["x"], "y": cell["y"]}
                        for cell in wall_cells
                    ],
                    "rows": list(candidate["rows"]),
                }
            )

    if previous_rows:
        different_candidates = [
            candidate
            for candidate in candidates
            if candidate["rows"] != list(previous_rows)
        ]

        if different_candidates:
            candidates = different_candidates

    style_rank = {
        "bent": 0,
        "split": 1,
        "dispersed": 2,
        "straight": 3,
    }
    candidates.sort(
        key=lambda candidate: (
            -len(candidate["internalWallCellIds"]),
            style_rank.get(candidate["wallStyle"], 99),
            candidate["waterAreaId"],
            candidate["internalWallCellIds"],
        )
    )

    for index, candidate in enumerate(candidates):
        candidate["id"] = index

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    log_event(
        "INFO",
        "pc_layout_prefilter_timing",
        elapsedMs=elapsed_ms,
        checkedWaterAreas=checked_water_areas,
        layoutCandidateCount=len(candidates),
        searchedStates=budget["searchedStates"],
        budgetExhausted=(
            time.perf_counter() >= budget["deadline"]
            or budget["remainingStates"] <= 0
        ),
    )
    return candidates, checked_water_areas, budget["searchedStates"]


def find_pc_safe_player_and_solution_trace(
    walkable,
    start_boxes,
    targets,
    editable_cells,
    water_cell_ids,
    budget,
):
    box_set = set(start_boxes)
    remaining_player_ids = {
        cell["id"]
        for cell in editable_cells
        if (
            cell["id"] not in water_cell_ids
            and (cell["x"], cell["y"]) in walkable
            and (cell["x"], cell["y"]) not in box_set
        )
    }

    for representative in find_pc_region_representatives(
        walkable,
        box_set,
    ):
        reachable = find_pc_reachable_cells(
            representative,
            walkable,
            box_set,
        )
        region_player_cells = sorted(
            (
                cell
                for cell in editable_cells
                if (
                    cell["id"] in remaining_player_ids
                    and (cell["x"], cell["y"]) in reachable
                )
            ),
            key=lambda cell: cell["id"],
        )

        if not region_player_cells:
            continue

        player_cell = region_player_cells[0]

        try:
            protected_positions = search_pc_solution_trace(
                walkable,
                start_boxes,
                targets,
                (player_cell["x"], player_cell["y"]),
                budget,
            )
        except PCSolvabilityError:
            continue

        return player_cell["id"], protected_positions

    return None


def search_pc_solution_trace(
    walkable,
    start_boxes,
    targets,
    player,
    budget,
):
    start_state = (player, tuple(sorted(start_boxes)))
    open_states = deque([start_state])
    parents = {start_state: None}

    while open_states:
        if (
            budget["remainingStates"] <= 0
            or time.perf_counter() >= budget["deadline"]
        ):
            raise PCSolvabilityError(
                "SEARCH_BUDGET_EXCEEDED",
                "PC safe-layout search exceeded its shared preprocessing budget",
                searched_states=budget["searchedStates"],
            )

        current_player, boxes = open_states.popleft()
        budget["remainingStates"] -= 1
        budget["searchedStates"] += 1
        state = (current_player, boxes)

        if set(boxes) == targets:
            protected = set()
            current = state

            while current is not None:
                current_state_player, current_state_boxes = current
                protected.add(current_state_player)
                protected.update(current_state_boxes)
                current = parents[current]

            return protected

        box_set = set(boxes)

        for direction_x, direction_y in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
        ):
            next_player = (
                current_player[0] + direction_x,
                current_player[1] + direction_y,
            )

            if next_player not in walkable:
                continue

            next_boxes = boxes

            if next_player in box_set:
                next_box = (
                    next_player[0] + direction_x,
                    next_player[1] + direction_y,
                )

                if next_box not in walkable or next_box in box_set:
                    continue

                moved_boxes = list(boxes)
                moved_boxes[moved_boxes.index(next_player)] = next_box
                next_boxes = tuple(sorted(moved_boxes))

            next_state = (next_player, next_boxes)

            if next_state not in parents:
                parents[next_state] = state
                open_states.append(next_state)

    raise PCSolvabilityError(
        "NO_SOLUTION_AFTER_SEARCH",
        "PC water-only candidate has no Sokoban solution",
        searched_states=budget["searchedStates"],
    )


def build_pc_greedy_wall_groups(
    safe_wall_cells,
    walkable,
):
    groups = []

    for required_count in (
        PC_PRIMARY_INTERNAL_WALLS,
        PC_INTERMEDIATE_INTERNAL_WALLS,
        PC_FALLBACK_MIN_INTERNAL_WALLS,
    ):
        group = choose_pc_greedy_wall_group(
            safe_wall_cells,
            walkable,
            required_count,
        )

        if group:
            groups.append(group)

    return groups


def choose_pc_greedy_wall_group(
    safe_wall_cells,
    walkable,
    required_count,
):
    if len(safe_wall_cells) < required_count:
        return None

    ordered_cells = sorted(
        safe_wall_cells,
        key=lambda cell: (
            -int(cell.get("wallImpactScore") or 0),
            cell["id"],
        ),
    )
    variants = []

    for seed in ordered_cells:
        selected = [seed]

        while len(selected) < required_count:
            best_cell = None
            best_score = None

            for candidate in ordered_cells:
                if candidate in selected:
                    continue

                proposed = selected + [candidate]
                proposed_positions = {
                    (cell["x"], cell["y"])
                    for cell in proposed
                }

                if (
                    contains_pc_two_by_two_block(proposed_positions)
                    or count_pc_components(
                        set(walkable) - proposed_positions
                    ) != 1
                ):
                    continue

                style = classify_pc_wall_style(proposed)
                pair_distances = [
                    abs(left["x"] - right["x"])
                    + abs(left["y"] - right["y"])
                    for index, left in enumerate(proposed)
                    for right in proposed[index + 1:]
                ]
                adjacency_count = sum(
                    distance == 1
                    for distance in pair_distances
                )
                style_bonus = {
                    "bent": 80,
                    "split": 70,
                    "dispersed": 60,
                    "straight": 0,
                }.get(style, 0)
                score = (
                    style_bonus,
                    adjacency_count,
                    min(pair_distances, default=0),
                    int(candidate.get("wallImpactScore") or 0),
                    -candidate["id"],
                )

                if best_score is None or score > best_score:
                    best_score = score
                    best_cell = candidate

            if best_cell is None:
                break

            selected.append(best_cell)

        if len(selected) != required_count:
            continue

        style = classify_pc_wall_style(selected)
        variants.append(
            {
                "internalWallCellIds": sorted(
                    cell["id"]
                    for cell in selected
                ),
                "wallStyle": style,
            }
        )

    if not variants:
        return None

    style_rank = {
        "bent": 0,
        "split": 1,
        "dispersed": 2,
        "straight": 3,
    }
    variants.sort(
        key=lambda variant: (
            style_rank.get(variant["wallStyle"], 99),
            variant["internalWallCellIds"],
        )
    )
    return variants[0]


def contains_pc_two_by_two_block(positions):
    positions = set(positions)

    return any(
        {
            (x, y),
            (x + 1, y),
            (x, y + 1),
            (x + 1, y + 1),
        }.issubset(positions)
        for x, y in positions
    )


def classify_pc_wall_style(wall_cells):
    positions = {
        (cell["x"], cell["y"])
        for cell in wall_cells
    }

    if len(positions) >= 3 and (
        len({x for x, _ in positions}) == 1
        or len({y for _, y in positions}) == 1
    ):
        return "straight"

    component_count = count_pc_components(positions)

    if component_count == 1:
        return "bent"

    if component_count == 2:
        return "split"

    return "dispersed"


def assemble_pc_layout_rows(
    sketch_rows,
    enclosed_cells,
    editable_by_id,
    water_cell_ids,
    player_cell_id,
    internal_wall_cell_ids,
    width,
    height,
):
    rows = [
        [
            "." if (x, y) in enclosed_cells and sketch_rows[y][x] == " "
            else sketch_rows[y][x]
            for x in range(width)
        ]
        for y in range(height)
    ]

    for cell_id in water_cell_ids:
        cell = editable_by_id[cell_id]
        rows[cell["y"]][cell["x"]] = "@"

    player_cell = editable_by_id[player_cell_id]
    rows[player_cell["y"]][player_cell["x"]] = "p"

    for cell_id in internal_wall_cell_ids:
        cell = editable_by_id[cell_id]
        rows[cell["y"]][cell["x"]] = "#"

    return ["".join(row) for row in rows]


def validate_pc_rows(rows, width, height, allowed_tiles, field_name):
    if not isinstance(rows, list) or len(rows) != height:
        raise ValueError(f"{field_name} must contain exactly {height} rows")

    for row_index, row in enumerate(rows):
        if not isinstance(row, str) or len(row) != width:
            raise ValueError(
                f"{field_name}[{row_index}] must contain exactly {width} characters"
            )

        invalid = next((tile for tile in row if tile not in allowed_tiles), None)

        if invalid is not None:
            raise ValueError(f"{field_name} contains unsupported tile {invalid!r}")


def find_pc_enclosed_cells(rows, width, height):
    outside = [[False for _ in range(width)] for _ in range(height)]
    open_cells = []

    def enqueue(x, y):
        if (
            x < 0
            or x >= width
            or y < 0
            or y >= height
            or outside[y][x]
            or rows[y][x] == "#"
        ):
            return

        outside[y][x] = True
        open_cells.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)

    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    index = 0

    while index < len(open_cells):
        x, y = open_cells[index]
        index += 1
        enqueue(x + 1, y)
        enqueue(x - 1, y)
        enqueue(x, y + 1)
        enqueue(x, y - 1)

    return [
        [rows[y][x] != "#" and not outside[y][x] for x in range(width)]
        for y in range(height)
    ]


def find_pc_outer_shell_cells(rows, width, height):
    outside = set()
    open_cells = deque()

    def enqueue(x, y):
        position = (x, y)

        if (
            x < 0
            or x >= width
            or y < 0
            or y >= height
            or position in outside
            or rows[y][x] == "#"
        ):
            return

        outside.add(position)
        open_cells.append(position)

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)

    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    while open_cells:
        x, y = open_cells.popleft()

        for neighbor_x, neighbor_y in (
            (x + 1, y),
            (x - 1, y),
            (x, y + 1),
            (x, y - 1),
        ):
            enqueue(neighbor_x, neighbor_y)

    shell_cells = set()

    for y in range(height):
        for x in range(width):
            if rows[y][x] != "#":
                continue

            for neighbor_x, neighbor_y in (
                (x + 1, y),
                (x - 1, y),
                (x, y + 1),
                (x, y - 1),
            ):
                if (
                    neighbor_x < 0
                    or neighbor_x >= width
                    or neighbor_y < 0
                    or neighbor_y >= height
                    or (neighbor_x, neighbor_y) in outside
                ):
                    shell_cells.add((x, y))
                    break

    return shell_cells


def count_pc_components(cells):
    remaining = set(cells)
    component_count = 0

    while remaining:
        component_count += 1
        open_cells = [remaining.pop()]

        while open_cells:
            x, y = open_cells.pop()

            for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    open_cells.append(neighbor)

    return component_count


def validate_pc_start_clearance(rows, width, height):
    for y in range(height):
        for x in range(width):
            if rows[y][x] != "s":
                continue

            for neighbor_x, neighbor_y in (
                (x + 1, y),
                (x - 1, y),
                (x, y + 1),
                (x, y - 1),
            ):
                if (
                    0 <= neighbor_x < width
                    and 0 <= neighbor_y < height
                    and rows[neighbor_y][neighbor_x] == "#"
                ):
                    raise ValueError(
                        "box start at row "
                        f"{y + 1}, column {x + 1} cannot touch a wall"
                    )


def find_pc_reverse_box_reachable_cells(target, walkable):
    reachable = {target}
    open_cells = [target]

    while open_cells:
        current_x, current_y = open_cells.pop()

        for direction_x, direction_y in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
        ):
            previous = (
                current_x - direction_x,
                current_y - direction_y,
            )
            standing = (
                previous[0] - direction_x,
                previous[1] - direction_y,
            )

            if (
                previous in walkable
                and standing in walkable
                and previous not in reachable
            ):
                reachable.add(previous)
                open_cells.append(previous)

    return reachable


def has_pc_box_target_matching(box_target_options, box_index=0, used_targets=None):
    if box_index >= len(box_target_options):
        return True

    used_targets = set(used_targets or ())

    for target in box_target_options[box_index]:
        if target in used_targets:
            continue

        if has_pc_box_target_matching(
            box_target_options,
            box_index + 1,
            used_targets | {target},
        ):
            return True

    return False


def has_pc_legal_initial_push(walkable, start_boxes, initial_players):
    box_set = set(start_boxes)

    for player in initial_players:
        reachable = find_pc_reachable_cells(player, walkable, box_set)

        for box_x, box_y in start_boxes:
            for direction_x, direction_y in (
                (1, 0),
                (-1, 0),
                (0, 1),
                (0, -1),
            ):
                standing = (
                    box_x - direction_x,
                    box_y - direction_y,
                )
                destination = (
                    box_x + direction_x,
                    box_y + direction_y,
                )

                if (
                    standing in reachable
                    and destination in walkable
                    and destination not in box_set
                ):
                    return True

    return False


def diagnose_pc_static_solvability(
    walkable,
    start_boxes,
    targets,
    initial_players=None,
):
    walkable = set(walkable)
    start_boxes = tuple(sorted(start_boxes))
    targets = set(targets)
    reverse_reachable = {
        target: find_pc_reverse_box_reachable_cells(target, walkable)
        for target in targets
    }
    box_target_options = []

    for box in start_boxes:
        options = sorted(
            target
            for target, reachable in reverse_reachable.items()
            if box in reachable
        )

        if not options:
            return {
                "code": "BOX_NO_TARGET_ROUTE",
                "box": box,
            }

        box_target_options.append(options)

    if not has_pc_box_target_matching(box_target_options):
        return {
            "code": "TARGET_MATCHING_IMPOSSIBLE",
            "boxTargetOptions": [
                {
                    "box": start_boxes[index],
                    "targets": list(options),
                }
                for index, options in enumerate(box_target_options)
            ],
        }

    if (
        initial_players is not None
        and set(start_boxes) != targets
        and not has_pc_legal_initial_push(
            walkable,
            start_boxes,
            initial_players,
        )
    ):
        return {
            "code": "NO_LEGAL_INITIAL_PUSH",
        }

    return None


def describe_pc_solvability_diagnostic(diagnostic):
    code = diagnostic.get("code", "NO_SOLUTION_AFTER_SEARCH")

    if code == "BOX_NO_TARGET_ROUTE":
        box_x, box_y = diagnostic["box"]
        return (
            f"box start ({box_x},{box_y}) has no static push route to any target"
        )

    if code == "TARGET_MATCHING_IMPOSSIBLE":
        return "box-to-target routes cannot form a complete matching"

    if code == "NO_LEGAL_INITIAL_PUSH":
        return "the selected player position has no legal initial box push"

    if code == "SEARCH_BUDGET_EXCEEDED":
        return "candidate solvability search exceeded its state budget"

    return "complete search found no Sokoban solution"


def validate_pc_open_sketch_feasibility(
    rows,
    enclosed_cells,
    width,
    height,
    maximum_search_states=PC_FEASIBILITY_MAX_SEARCH_STATES,
):
    walkable = set(enclosed_cells)
    start_boxes = tuple(
        sorted(
            (x, y)
            for y in range(height)
            for x in range(width)
            if rows[y][x] == "s"
        )
    )
    targets = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "t"
    }
    initial_regions = find_pc_region_representatives(
        walkable,
        set(start_boxes),
    )

    if not initial_regions:
        raise ValueError("PC sketch has no possible player start")

    return validate_pc_push_solvability(
        walkable,
        start_boxes,
        targets,
        initial_regions,
        maximum_search_states,
        "PC sketch open-map solvability check exceeded its search budget",
        (
            "PC sketch has no solvable open completion; "
            "move box starts, targets, or walls"
        ),
    )


def validate_pc_completed_level_solvability(
    rows,
    width,
    height,
    maximum_search_states=PC_FEASIBILITY_MAX_SEARCH_STATES,
):
    walkable = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] in {".", "p", "s", "t"}
    }
    start_boxes = tuple(
        sorted(
            (x, y)
            for y in range(height)
            for x in range(width)
            if rows[y][x] == "s"
        )
    )
    targets = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "t"
    }
    players = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "p"
    ]

    if len(players) != 1:
        raise ValueError("candidate must contain exactly one player start")

    diagnostic = diagnose_pc_static_solvability(
        walkable,
        start_boxes,
        targets,
        players,
    )

    if diagnostic is not None:
        raise PCSolvabilityError(
            diagnostic["code"],
            "candidate has no Sokoban solution: "
            + describe_pc_solvability_diagnostic(diagnostic),
            details=diagnostic,
        )

    return validate_pc_push_solvability(
        walkable,
        start_boxes,
        targets,
        players,
        maximum_search_states,
        "candidate solvability check exceeded its search budget",
        "candidate has no Sokoban solution",
    )


def validate_pc_push_solvability(
    walkable,
    start_boxes,
    targets,
    initial_players,
    maximum_search_states,
    budget_error,
    unsolvable_error,
):
    _, searched_states = search_pc_minimum_pushes(
        walkable,
        start_boxes,
        targets,
        initial_players,
        maximum_search_states,
        budget_error,
        unsolvable_error,
    )
    return searched_states


def search_pc_minimum_pushes(
    walkable,
    start_boxes,
    targets,
    initial_players,
    maximum_search_states,
    budget_error,
    unsolvable_error,
):
    open_states = deque()
    visited_states = set()

    for player in initial_players:
        key = build_pc_push_state_key(player, start_boxes, walkable)

        if key not in visited_states:
            visited_states.add(key)
            open_states.append((player, start_boxes, 0))

    searched_states = 0
    search_limit = max(1, int(maximum_search_states))

    while open_states and searched_states < search_limit:
        player, boxes, pushes = open_states.popleft()
        searched_states += 1

        if set(boxes) == targets:
            return pushes, searched_states

        box_set = set(boxes)
        reachable = find_pc_reachable_cells(player, walkable, box_set)

        for box_index, box in enumerate(boxes):
            for direction_x, direction_y in (
                (1, 0),
                (-1, 0),
                (0, 1),
                (0, -1),
            ):
                standing_cell = (
                    box[0] - direction_x,
                    box[1] - direction_y,
                )
                destination = (
                    box[0] + direction_x,
                    box[1] + direction_y,
                )

                if (
                    standing_cell not in reachable
                    or destination not in walkable
                    or destination in box_set
                ):
                    continue

                next_boxes = list(boxes)
                next_boxes[box_index] = destination
                next_boxes = tuple(sorted(next_boxes))
                next_player = box
                key = build_pc_push_state_key(
                    next_player,
                    next_boxes,
                    walkable,
                )

                if key not in visited_states:
                    visited_states.add(key)
                    open_states.append((next_player, next_boxes, pushes + 1))

    if open_states:
        raise PCSolvabilityError(
            "SEARCH_BUDGET_EXCEEDED",
            budget_error,
            searched_states=searched_states,
        )

    raise PCSolvabilityError(
        "NO_SOLUTION_AFTER_SEARCH",
        unsolvable_error,
        searched_states=searched_states,
    )


def find_pc_region_representatives(walkable, boxes):
    remaining = set(walkable) - set(boxes)
    representatives = []

    while remaining:
        representative = min(remaining, key=lambda position: (position[1], position[0]))
        representatives.append(representative)
        open_cells = [representative]
        remaining.remove(representative)

        while open_cells:
            x, y = open_cells.pop()

            for neighbor in (
                (x + 1, y),
                (x - 1, y),
                (x, y + 1),
                (x, y - 1),
            ):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    open_cells.append(neighbor)

    return representatives


def find_pc_reachable_cells(player, walkable, boxes):
    if player not in walkable or player in boxes:
        return set()

    reachable = {player}
    open_cells = [player]

    while open_cells:
        x, y = open_cells.pop()

        for neighbor in (
            (x + 1, y),
            (x - 1, y),
            (x, y + 1),
            (x, y - 1),
        ):
            if (
                neighbor in walkable
                and neighbor not in boxes
                and neighbor not in reachable
            ):
                reachable.add(neighbor)
                open_cells.append(neighbor)

    return reachable


def build_pc_push_state_key(player, boxes, walkable):
    reachable = find_pc_reachable_cells(player, walkable, set(boxes))
    representative = min(
        reachable,
        key=lambda position: (position[1], position[0]),
    )
    return boxes, representative


def validate_pc_water_rectangles(rows, width, height):
    remaining = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if rows[y][x] == "@"
    }
    water_area_count = 0

    while remaining:
        water_area_count += 1
        start = remaining.pop()
        component = {start}
        open_cells = [start]

        while open_cells:
            x, y = open_cells.pop()

            for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    open_cells.append(neighbor)

        xs = [position[0] for position in component]
        ys = [position[1] for position in component]
        area_width = max(xs) - min(xs) + 1
        area_height = max(ys) - min(ys) + 1

        if (
            area_width < 2
            or area_width > 4
            or area_height < 2
            or area_height > 4
            or len(component) != area_width * area_height
        ):
            raise ValueError("every water area must be a complete 2-4 by 2-4 rectangle")

    return water_area_count


def validate_plan(
    plan,
    feature_constraints=None,
    generation_preferences=None,
):
    if plan is None:
        raise ValueError("model returned no parsed plan")

    data = plan.model_dump() if isinstance(plan, LevelDesignPlan) else dict(plan)
    feature_constraints = feature_constraints or {}
    generation_preferences = generation_preferences or {}
    no_water = bool(feature_constraints.get("noWater"))
    no_internal_walls = bool(feature_constraints.get("noInternalWalls"))
    has_custom_water = "minWaterAreas" in generation_preferences
    has_custom_walls = "minWallObstacleBlocks" in generation_preferences

    if no_water:
        data["minWaterAreas"] = 0
        data["maxWaterAreas"] = 0

    if no_internal_walls:
        data["minWallObstacleBlocks"] = 0
        data["maxWallObstacleBlocks"] = 0
        data["corridorPlacement"] = "none"
        data["corridorWidth"] = 0
        data["corridorOrientation"] = "any"
        data["corridorRole"] = "visual_only"
        data["corridorPriority"] = "preferred"

    for key, (minimum, maximum) in LIMITS.items():
        value = data.get(key)

        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")

        if value < minimum or value > maximum:
            raise ValueError(f"{key}={value} is outside {minimum}-{maximum}")

    if not no_water and not has_custom_water and (
        data["minWaterAreas"] < 1 or data["maxWaterAreas"] < 1
    ):
        raise ValueError(
            "water areas can be zero only when the user explicitly requests no water"
        )

    if not no_internal_walls and not has_custom_walls and (
        data["minWallObstacleBlocks"] != 2
        or data["maxWallObstacleBlocks"] < 2
    ):
        raise ValueError(
            "internal wall obstacles can be zero only when the user explicitly "
            "requests no internal walls"
        )

    if data["maxSolutionSteps"] < data["minSolutionSteps"]:
        raise ValueError("maxSolutionSteps must be >= minSolutionSteps")

    if data["maxPushes"] < data["minPushes"]:
        raise ValueError("maxPushes must be >= minPushes")

    if data["maxWaterAreas"] < data["minWaterAreas"]:
        raise ValueError("maxWaterAreas must be >= minWaterAreas")

    if data["maxWallObstacleBlocks"] < data["minWallObstacleBlocks"]:
        raise ValueError("maxWallObstacleBlocks must be >= minWallObstacleBlocks")

    if data["maxReversePulls"] < data["minReversePulls"]:
        raise ValueError("maxReversePulls must be >= minReversePulls")

    for key, allowed_values in ENUMS.items():
        value = str(data.get(key, "")).strip()

        if value not in allowed_values:
            raise ValueError(f"{key}={value} is not supported")

        data[key] = value

    corridor_width = data.get("corridorWidth", 0)

    if isinstance(corridor_width, bool) or not isinstance(corridor_width, int):
        raise ValueError("corridorWidth must be an integer")

    if data["corridorPlacement"] == "none":
        if corridor_width != 0:
            raise ValueError("corridorWidth must be 0 when corridorPlacement is none")
    elif corridor_width not in {1, 2}:
        raise ValueError("corridorWidth must be 1 or 2 when a corridor is requested")

    if data["corridorPriority"] == "required" and data["corridorPlacement"] == "none":
        raise ValueError("required corridorPriority needs a corridor placement")

    if data["corridorRole"] == "required_box_route" and data["corridorPlacement"] == "none":
        raise ValueError("required_box_route needs a corridor placement")

    style = str(data.get("style", "")).strip()
    if not style:
        style = DEFAULT_PLAN["style"]

    design_note = str(data.get("designNote", "")).strip()
    if not design_note:
        design_note = DEFAULT_PLAN["designNote"]

    return {
        "minSolutionSteps": data["minSolutionSteps"],
        "maxSolutionSteps": data["maxSolutionSteps"],
        "minPushes": data["minPushes"],
        "maxPushes": data["maxPushes"],
        "minWaterAreas": data["minWaterAreas"],
        "maxWaterAreas": data["maxWaterAreas"],
        "minWallObstacleBlocks": data["minWallObstacleBlocks"],
        "maxWallObstacleBlocks": data["maxWallObstacleBlocks"],
        "minReversePulls": data["minReversePulls"],
        "maxReversePulls": data["maxReversePulls"],
        "style": style[:80],
        "archetype": data["archetype"],
        "targetLayout": data["targetLayout"],
        "obstacleStyle": data["obstacleStyle"],
        "waterStyle": data["waterStyle"],
        "designNote": design_note[:160],
        "corridorPlacement": data["corridorPlacement"],
        "corridorWidth": corridor_width,
        "corridorOrientation": data["corridorOrientation"],
        "corridorRole": data["corridorRole"],
        "corridorPriority": data["corridorPriority"],
    }


def validate_creative_idea_expansion(payload):
    if payload is None:
        raise ValueError("model returned no expansion payload")

    data = payload.model_dump() if hasattr(payload, "model_dump") else payload

    if isinstance(data, list):
        raw_options = data
    else:
        raw_options = dict(data).get("options")

    if not isinstance(raw_options, list) or len(raw_options) < 3:
        raise ValueError("expansion payload must include at least three options")

    validated = []
    option_ids = ["A", "B", "C"]

    for index, raw_option in enumerate(raw_options[:3]):
        option = raw_option.model_dump() if hasattr(raw_option, "model_dump") else dict(raw_option)
        option_id = clean_expansion_text(option.get("id")) or option_ids[index]
        title = clean_expansion_text(option.get("title"))[:64]
        description = clean_expansion_text(option.get("description"))[:320]
        prompt_text = clean_expansion_text(option.get("promptText"))[:420]

        if not title:
            raise ValueError(f"option {index + 1} title is required")

        if not description:
            raise ValueError(f"option {index + 1} description is required")

        if not prompt_text:
            prompt_text = description

        validated.append(
            CreativeIdeaExpansionOption(
                id=option_id[:12],
                title=title,
                description=description,
                promptText=prompt_text,
            ).model_dump()
        )

    return validated


def fallback_creative_idea_expansion(creative_context, reason):
    idea_text = clean_expansion_text((creative_context or {}).get("ideaText"))
    options = build_contextual_expansion_fallback_options(idea_text)

    log_event(
        "WARNING",
        "creative_expansion_fallback",
        reason=str(reason or "")[:1000],
    )
    return {
        "options": options,
        "usedFallback": True,
        "message": reason,
    }


def build_contextual_expansion_fallback_options(idea_text):
    tags = classify_expansion_idea(idea_text)

    if "water" in tags:
        return build_water_fallback_options(compact="compact" in tags)

    if "maze" in tags:
        return build_maze_fallback_options()

    if "compact" in tags:
        return build_compact_fallback_options()

    return build_general_fallback_options()


def classify_expansion_idea(idea_text):
    normalized = clean_expansion_text(idea_text).lower()
    tags = set()

    if contains_any(normalized, ["water", "river", "pool", "lake", "pond", "obstacle", "水", "河", "池", "湖", "障碍"]):
        tags.add("water")

    if contains_any(normalized, ["maze", "detour", "corridor", "route", "path", "绕路", "迷宫", "通道", "路线", "走廊"]):
        tags.add("maze")

    if contains_any(normalized, ["hard", "difficult", "compact", "tight", "deadlock", "tricky", "难", "困难", "紧凑", "狭小", "卡死", "精确"]):
        tags.add("compact")

    return tags


def build_water_fallback_options(compact=False):
    if compact:
        return [
            {
                "id": "A",
                "title": "Compact Water Split",
                "description": "In a compact room, place water through the middle so the player must route around it and choose which box to handle first. The water cuts the direct path and compresses standing space.",
                "promptText": "Use the original idea with compact space, a central water split, limited standing positions, detour routing, and two-box order pressure.",
            },
            {
                "id": "B",
                "title": "Tight Waterside Goals",
                "description": "Put the goal area near water and keep the surrounding space tight. The player has to adjust boxes along a narrow waterside edge without trapping a box or themselves.",
                "promptText": "Use the original idea with compact waterside goals, edge-position pressure, and deadlock-aware box placement.",
            },
            {
                "id": "C",
                "title": "Twin Pool Squeeze",
                "description": "Use two small water areas in a tight room to bend the route and make the player switch attention between boxes. The challenge comes from water detours plus limited standing room.",
                "promptText": "Use the original idea with two water pools, compact routing, limited standing space, and box-switching rhythm.",
            },
        ]

    return [
        {
            "id": "A",
            "title": "Water-Split Route",
            "description": "Place water through the middle so the player has to route around it and decide which box to handle first. The water acts as the main blocker, not decoration.",
            "promptText": "Use the original idea with a central water split, detour routing, box-order decisions, and two-box planning pressure.",
        },
        {
            "id": "B",
            "title": "Waterside Goals",
            "description": "Put the goal area near water so pushing space is limited along the edge. The puzzle asks the player to adjust boxes without trapping them against the water.",
            "promptText": "Use the original idea with goals near water, edge-position pressure, and deadlock-aware box placement.",
        },
        {
            "id": "C",
            "title": "Twin Pool Detour",
            "description": "Use two small water areas to bend the route and make the player switch attention between boxes. The challenge comes from timing movement around water, not from a larger map.",
            "promptText": "Use the original idea with two water pools, a bent route, and box-switching rhythm.",
        },
    ]


def build_maze_fallback_options():
    return [
        {
            "id": "A",
            "title": "Loopback Corridor",
            "description": "Shape the route as a corridor the player must loop back through, using one box to open position before returning for the other. The maze feel comes from reused standing space.",
            "promptText": "Use the original idea with a loopback corridor, detour movement, reused positions, and two-box order planning.",
        },
        {
            "id": "B",
            "title": "Offset Hallways",
            "description": "Use staggered corridors so direct pushing is unreliable. The player must move a box to a transfer point, then approach it from another side.",
            "promptText": "Use the original idea with offset corridors, transfer points, side switching, and route-change pressure.",
        },
        {
            "id": "C",
            "title": "Side-Entry Goals",
            "description": "Make the goal area reachable from the side rather than straight ahead. The player has to preserve a path for the second box before closing the entrance.",
            "promptText": "Use the original idea with side-entry goals, preserved access paths, and ordered target delivery.",
        },
    ]


def build_compact_fallback_options():
    return [
        {
            "id": "A",
            "title": "One-Push Pressure",
            "description": "Keep the space tight but give each box one safe transfer spot. The player has to notice which push will close a corridor too early.",
            "promptText": "Use the original idea with compact space, safe transfer spots, corridor-blocking risk, and precise pushes.",
        },
        {
            "id": "B",
            "title": "Crossed Positions",
            "description": "Make the two box routes cross near the center so the player must clear standing space before advancing. The tightness comes from shared positions.",
            "promptText": "Use the original idea with crossed box routes, shared standing space, and ordered movement.",
        },
        {
            "id": "C",
            "title": "Edge Recovery",
            "description": "Let a box approach a risky edge while preserving one recovery route. The player pushes into danger, then uses the other side to bring it back toward the goals.",
            "promptText": "Use the original idea with risky edge pushes, a recovery path, and side-switching position play.",
        },
    ]


def build_general_fallback_options():
    return [
        {
            "id": "A",
            "title": "Order Decision",
            "description": "Turn the idea into a clear two-box order puzzle: solving one box opens standing space, but can also block the other box's route.",
            "promptText": "Use the original idea with two-box order decisions, opened standing space, and preserved routes.",
        },
        {
            "id": "B",
            "title": "Entrance Control",
            "description": "Give the key area only one or two entrances, so the player has to decide when to enter and when to push a box out of the doorway.",
            "promptText": "Use the original idea with limited entrances, temporary blockage, and standing-position judgment.",
        },
        {
            "id": "C",
            "title": "Compressed Goals",
            "description": "Cluster the goal area so the final pushes need careful placement. The first box must not block the second box's finishing route.",
            "promptText": "Use the original idea with clustered goals, endgame route preservation, and two-box placement order.",
        },
    ]


def contains_any(text, keywords):
    return any(keyword in text for keyword in keywords)


def clean_expansion_text(value):
    if value is None:
        return ""

    return " ".join(str(value).strip().split())


def remember_blueprint(plan):
    key = get_blueprint_key(plan)

    with plan_history_lock:
        if key in recent_blueprints:
            recent_blueprints.remove(key)

        recent_blueprints.append(key)

        while len(recent_blueprints) > RECENT_BLUEPRINT_LIMIT:
            recent_blueprints.pop(0)


def get_recent_blueprint_hint():
    with plan_history_lock:
        if not recent_blueprints:
            return "none"

        return "; ".join(recent_blueprints)


def get_blueprint_key(plan):
    return "|".join(
        [
            str(plan.get("archetype", "")),
            str(plan.get("targetLayout", "")),
            str(plan.get("obstacleStyle", "")),
            str(plan.get("waterStyle", "")),
        ]
    )


def open_browser():
    time.sleep(1)
    webbrowser.open(START_URL)


if __name__ == "__main__":
    log_event(
        "INFO",
        "backend_starting",
        host=HOST,
        port=PORT,
        workers=1,
    )
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, workers=1)
