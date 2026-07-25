import html
import hashlib
import json
import os
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel

try:
    from prompt import (
        build_creative_idea_expansion_messages,
        build_ha_revision_plan_edit_messages,
        build_ha_revision_plan_messages,
        build_human_adjustment_clarity_messages,
        build_level_plan_messages,
        resolve_zero_feature_constraints,
    )
except ImportError:
    from .prompt import (
        build_creative_idea_expansion_messages,
        build_ha_revision_plan_edit_messages,
        build_ha_revision_plan_messages,
        build_human_adjustment_clarity_messages,
        build_level_plan_messages,
        resolve_zero_feature_constraints,
    )

HOST = "127.0.0.1"
PORT = 8000
START_URL = f"http://{HOST}:{PORT}/generate-level-plan"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
HA_PLAN_LLM_TIMEOUT_SECONDS = 45.0
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "Frontend"
STUDY_LOG_DIR = BASE_DIR / "study_logs"
STUDY_LOG_FILE = STUDY_LOG_DIR / "level_records.jsonl"
SURVEY_LOG_FILE = STUDY_LOG_DIR / "survey_responses.jsonl"
CREATIVE_IDEA_LOG_FILE = STUDY_LOG_DIR / "creative_ideas.jsonl"
CREATIVE_EXPANSION_CHOICE_LOG_FILE = STUDY_LOG_DIR / "creative_expansion_choices.jsonl"
HA_PLAN_EVENT_LOG_FILE = STUDY_LOG_DIR / "ha_plan_events.jsonl"
JOURNEY_EVENT_LOG_FILE = STUDY_LOG_DIR / "journey_events.jsonl"

load_dotenv(BASE_DIR / ".env")


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

app = FastAPI()
app.mount(
    "/frontend",
    StaticFiles(directory=FRONTEND_DIR, html=True, check_dir=False),
    name="frontend",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


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


@app.post("/expand-creative-idea")
def expand_creative_idea(request: CreativeIdeaExpansionRequest):
    data = request.model_dump()
    idea_text = str(data.get("ideaText") or "").strip()

    if not idea_text:
        raise HTTPException(status_code=400, detail="ideaText is required")

    data["ideaText"] = idea_text
    return create_creative_idea_expansion(data)


@app.post("/generate-ha-revision-plans")
def generate_ha_revision_plans(request: HARevisionPlanRequest):
    data = request.model_dump()
    adjustment_text = str(data.get("adjustmentText") or "").strip()
    previous_plan = data.get("previousLevelPlan")

    if not adjustment_text:
        raise HTTPException(status_code=400, detail="adjustmentText is required")

    if not isinstance(previous_plan, dict) or not previous_plan:
        raise HTTPException(status_code=400, detail="previousLevelPlan is required")

    data["adjustmentText"] = adjustment_text

    try:
        result = create_ha_revision_plans(data)
        append_ha_generation_event(data, result["options"])
        return result
    except HTTPException as exception:
        append_ha_generation_event(
            data,
            [],
            error=str(exception.detail),
        )
        raise


@app.post("/revise-ha-revision-plan")
def revise_ha_revision_plan(request: HARevisionPlanEditRequest):
    data = request.model_dump()
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
    return create_ha_revision_plan_edit(data)


@app.get("/validate-human-adjustment")
def validate_human_adjustment(adjustmentText: str = ""):
    adjustment_text = str(adjustmentText or "").strip()

    if not adjustment_text:
        return validate_human_adjustment_clarity_payload({})

    return create_human_adjustment_clarity_check(adjustment_text)


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


@app.get("/creative-ideas", response_class=PlainTextResponse)
def get_creative_ideas():
    if not CREATIVE_IDEA_LOG_FILE.exists():
        return ""

    return CREATIVE_IDEA_LOG_FILE.read_text(encoding="utf-8")


@app.get("/survey-records-data")
def get_survey_records_data():
    responses, malformed_count = read_survey_response_events()
    return build_survey_records_payload(responses, malformed_count)


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


@app.post("/delete-round")
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


@app.post("/delete-level-run")
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


@app.post("/delete-survey-response")
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


@app.post("/delete-creative-idea")
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


@app.post("/delete-expansion-choice")
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


@app.post("/delete-ha-plan-event")
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


@app.post("/delete-journey-event")
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


@app.post("/delete-idea-records")
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


@app.post("/clear-level-records")
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
def generate_level_plan(
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
):
    return create_level_plan(
        {
            "ideaText": ideaText,
            "ideaId": ideaId,
            "sessionId": sessionId,
            "sceneName": sceneName,
            "originalIdeaText": originalIdeaText,
            "selectedDirectionText": selectedDirectionText,
            "refinementFeedbackText": refinementFeedbackText,
            "adjustmentHistoryText": adjustmentHistoryText,
            "latestAdjustmentText": latestAdjustmentText,
            "revisionMode": revisionMode,
            "previousLevelPlan": previousLevelPlan,
            "previousLevelMetrics": previousLevelMetrics,
            "selectedHAPlan": selectedHAPlan,
        }
    )


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
        <form method="post" action="/clear-level-records" onsubmit="return confirm('Clear all level records? This cannot be undone.');">
            <button class="danger-button" type="submit">Clear Records</button>
        </form>
    </div>
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


def create_creative_idea_expansion(creative_context):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if not api_key or api_key == "your_deepseek_api_key_here":
        return fallback_creative_idea_expansion(creative_context, "DEEPSEEK_API_KEY is missing")

    try:
        model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        temperature = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.9"))
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)

        response = client.chat.completions.create(
            model=model,
            messages=build_creative_idea_expansion_messages(creative_context),
            response_format={"type": "json_object"},
            temperature=temperature,
            stream=False,
        )

        content = response.choices[0].message.content
        options = validate_creative_idea_expansion(json.loads(content))
        print(f"Generated creative idea expansion from DeepSeek using {model}: {options}")
        return {
            "options": options,
            "usedFallback": False,
            "message": "",
        }
    except Exception as exception:
        return fallback_creative_idea_expansion(
            creative_context,
            f"DeepSeek expansion request failed: {exception}",
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


def create_human_adjustment_clarity_check(adjustment_text):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if not api_key or api_key == "your_deepseek_api_key_here":
        raise HTTPException(
            status_code=503,
            detail="Remote LLM is unavailable because DEEPSEEK_API_KEY is missing",
        )

    try:
        model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)
        response = client.chat.completions.create(
            model=model,
            messages=build_human_adjustment_clarity_messages(adjustment_text),
            response_format={"type": "json_object"},
            temperature=0.0,
            stream=False,
        )
        content = response.choices[0].message.content
        result = validate_human_adjustment_clarity_payload(json.loads(content))
        print(
            "Validated Human-led adjustment clarity:"
            f" score={result['totalScore']}/8, clear={result['isClear']}"
        )
        return result
    except HTTPException:
        raise
    except Exception as exception:
        raise HTTPException(
            status_code=502,
            detail=f"Human adjustment clarity validation failed: {exception}",
        ) from exception


def create_ha_revision_plans(context):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if not api_key or api_key == "your_deepseek_api_key_here":
        raise HTTPException(
            status_code=503,
            detail="Remote LLM is unavailable because DEEPSEEK_API_KEY is missing",
        )

    try:
        model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=HA_PLAN_LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
        response = client.chat.completions.create(
            model=model,
            messages=build_ha_revision_plan_messages(context),
            response_format={"type": "json_object"},
            temperature=0.2,
            stream=False,
        )
        content = response.choices[0].message.content
        options = validate_ha_revision_plan_options(
            json.loads(content),
            context.get("previousLevelPlan"),
            context.get("adjustmentText"),
        )
        print(
            f"Generated HA revision plans from DeepSeek using {model}: "
            f"{[option['title'] for option in options]}"
        )
        return {"options": options}
    except HTTPException:
        raise
    except Exception as exception:
        print(f"HA revision-plan request failed: {exception}")
        raise HTTPException(
            status_code=502,
            detail=(
                "HA revision-plan generation or validation failed: "
                f"{type(exception).__name__}: {exception}"
            ),
        ) from exception


def create_ha_revision_plan_edit(context):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if not api_key or api_key == "your_deepseek_api_key_here":
        raise HTTPException(
            status_code=503,
            detail="Remote LLM is unavailable because DEEPSEEK_API_KEY is missing",
        )

    try:
        model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=HA_PLAN_LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
        response = client.chat.completions.create(
            model=model,
            messages=build_ha_revision_plan_edit_messages(context),
            response_format={"type": "json_object"},
            temperature=0.1,
            stream=False,
        )
        content = response.choices[0].message.content
        option = validate_ha_revision_plan_edit(
            json.loads(content),
            context.get("originalOption"),
            context.get("previousLevelPlan"),
            context.get("adjustmentText"),
            context.get("editedDescription"),
        )
        print(
            f"Revised HA option from DeepSeek using {model}: "
            f"{option['title']}"
        )
        return {"option": option}
    except HTTPException:
        raise
    except Exception as exception:
        print(f"HA revision-plan edit request failed: {exception}")
        raise HTTPException(
            status_code=502,
            detail=(
                "HA revision-plan edit or validation failed: "
                f"{type(exception).__name__}: {exception}"
            ),
        ) from exception


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


def apply_selected_ha_plan(generated_plan, creative_context, feature_constraints):
    creative_context = creative_context or {}

    if str(creative_context.get("revisionMode") or "").strip().lower() != "ha":
        return validate_plan(generated_plan, feature_constraints)

    selected_text = str(creative_context.get("selectedHAPlan") or "").strip()

    if not selected_text:
        return validate_plan(generated_plan, feature_constraints)

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

    return validate_plan(candidate, effective_constraints)


def create_level_plan(creative_context=None):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if not api_key or api_key == "your_deepseek_api_key_here":
        raise HTTPException(
            status_code=503,
            detail="Remote LLM is unavailable because DEEPSEEK_API_KEY is missing",
        )

    try:
        creative_context = creative_context or {}
        feature_constraints = resolve_zero_feature_constraints(creative_context)
        model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        temperature = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.9"))
        variation_seed = int(time.time() * 1000)
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)

        response = client.chat.completions.create(
            model=model,
            messages=build_level_plan_messages(
                variation_seed,
                get_recent_blueprint_hint(),
                creative_context,
                feature_constraints,
            ),
            response_format={"type": "json_object"},
            temperature=temperature,
            stream=False,
        )

        content = response.choices[0].message.content
        raw_plan = json.loads(content)
        plan = apply_selected_ha_plan(
            raw_plan,
            creative_context,
            feature_constraints,
        )
        remember_blueprint(plan)
        print(f"Generated level plan from DeepSeek using {model}: {plan}")
        return plan
    except Exception as exception:
        print(f"DeepSeek level-plan request failed: {exception}")
        raise HTTPException(
            status_code=502,
            detail="Remote LLM request, response parsing, or blueprint validation failed",
        ) from exception


def validate_plan(plan, feature_constraints=None):
    if plan is None:
        raise ValueError("model returned no parsed plan")

    data = plan.model_dump() if isinstance(plan, LevelDesignPlan) else dict(plan)
    feature_constraints = feature_constraints or {}
    no_water = bool(feature_constraints.get("noWater"))
    no_internal_walls = bool(feature_constraints.get("noInternalWalls"))

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

    if not no_water and (
        data["minWaterAreas"] < 1 or data["maxWaterAreas"] < 1
    ):
        raise ValueError(
            "water areas can be zero only when the user explicitly requests no water"
        )

    if not no_internal_walls and (
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
    chinese = contains_cjk(idea_text)
    options = build_contextual_expansion_fallback_options(idea_text, chinese)

    print(f"Generated creative idea expansion from fallback: {reason}")
    return {
        "options": options,
        "usedFallback": True,
        "message": reason,
    }


def build_contextual_expansion_fallback_options(idea_text, chinese):
    tags = classify_expansion_idea(idea_text)

    if "water" in tags:
        return build_water_fallback_options(chinese, compact="compact" in tags)

    if "maze" in tags:
        return build_maze_fallback_options(chinese)

    if "compact" in tags:
        return build_compact_fallback_options(chinese)

    return build_general_fallback_options(chinese)


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


def build_water_fallback_options(chinese, compact=False):
    if chinese:
        if compact:
            return [
                {
                    "id": "A",
                    "title": "紧凑水域分割",
                    "description": "在紧凑空间里把水障碍放在地图中部，让玩家必须从水边绕行并选择先处理哪只箱子。水会切断直线路径，同时压缩可站位空间。",
                    "promptText": "围绕原始想法设计紧凑水域分割；用中部水障碍、少量站位、绕行和两箱顺序选择制造压力。",
                },
                {
                    "id": "B",
                    "title": "水边窄位目标",
                    "description": "让目标区靠近水边，并把周围空间收紧。玩家需要在靠水的狭窄边缘调整箱子，避免把自己或箱子逼进死角。",
                    "promptText": "围绕原始想法设计紧凑靠水目标区；用水边站位限制、目标区压力和死锁风险制造解谜重点。",
                },
                {
                    "id": "C",
                    "title": "双水池挤压",
                    "description": "在小房间里用两块水域挤出一条弯折路线，让玩家在两个箱子之间来回切换。难点来自水障碍造成的绕行时机和紧凑空间里的站位取舍。",
                    "promptText": "围绕原始想法设计双水池紧凑绕行；用两块水域、弯折路线、有限站位和箱子切换节奏形成玩法。",
                },
            ]

        return [
            {
                "id": "A",
                "title": "水域分割路线",
                "description": "把水障碍放在地图中部，让玩家必须从水边绕行并选择先处理哪只箱子。水不是装饰，而是切断直线路径的核心压力。",
                "promptText": "围绕原始想法设计水域分割路线；用中部水障碍制造绕行、顺序选择和两箱路线规划。",
            },
            {
                "id": "B",
                "title": "水边目标压力",
                "description": "让目标区靠近水边，箱子推进时可站位空间更少。玩家需要在靠水的狭窄边缘调整箱子，避免把自己或箱子逼进死角。",
                "promptText": "围绕原始想法设计靠水目标区；用水边站位限制、目标区压力和死锁风险制造解谜重点。",
            },
            {
                "id": "C",
                "title": "双水池绕行",
                "description": "用两块小水域制造一条弯折路线，让玩家在两个箱子之间来回切换。难点来自绕水移动的时机，而不是单纯扩大地图。",
                "promptText": "围绕原始想法设计双水池绕行；用两块水域、弯折路线和箱子切换节奏形成差异化玩法。",
            },
        ]

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


def build_maze_fallback_options(chinese):
    if chinese:
        return [
            {
                "id": "A",
                "title": "单通道回环",
                "description": "把路线做成一条需要绕回来的通道，玩家先推开一个箱子打开站位，再回来处理另一个箱子。迷宫感来自回环路线和站位复用。",
                "promptText": "围绕原始想法设计单通道回环；强调绕路、回到旧位置和两箱处理顺序。",
            },
            {
                "id": "B",
                "title": "错位走廊",
                "description": "用错开的走廊让直线推进变得不可靠，玩家必须先把箱子推到中转点，再从另一侧接手。重点是路线切换而不是增加箱子数量。",
                "promptText": "围绕原始想法设计错位走廊；用中转点、换边接手和路线切换制造迷宫感。",
            },
            {
                "id": "C",
                "title": "目标区绕入口",
                "description": "让目标区入口不在正面，玩家需要绕到侧面才能把箱子送进去。难点是提前给第二只箱子留下通路。",
                "promptText": "围绕原始想法设计侧向目标入口；用绕入口、保留通路和目标区推进顺序形成玩法。",
            },
        ]

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


def build_compact_fallback_options(chinese):
    if chinese:
        return [
            {
                "id": "A",
                "title": "一步失误压力",
                "description": "空间保持紧凑，但每个箱子都有一个安全中转位。玩家必须判断哪一步会封住通道，避免过早把箱子推到边缘。",
                "promptText": "围绕原始想法设计紧凑空间；用安全中转位、通道封锁风险和精确推动制造压力。",
            },
            {
                "id": "B",
                "title": "交叉站位",
                "description": "两个箱子的路线在中部交叉，玩家需要先让出站位再推进。紧凑感来自互相占位，而不是单纯减少空地。",
                "promptText": "围绕原始想法设计交叉站位；用两箱路线交叉、让位和推进顺序形成紧凑解谜。",
            },
            {
                "id": "C",
                "title": "边缘救回",
                "description": "允许箱子接近边缘，但保留一条可以救回的路线。玩家要把箱子推到危险位置后再利用另一侧站位把它送回目标区。",
                "promptText": "围绕原始想法设计边缘救回；用危险边缘、可救回路线和站位转换制造难度。",
            },
        ]

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


def build_general_fallback_options(chinese):
    if chinese:
        return [
            {
                "id": "A",
                "title": "顺序抉择",
                "description": "把原始想法转成一个先后顺序清晰的两箱谜题：先处理一个箱子会打开站位，但也可能挡住另一个箱子的路线。",
                "promptText": "围绕原始想法设计两箱顺序抉择；强调先后处理、站位打开和路线保留。",
            },
            {
                "id": "B",
                "title": "入口控制",
                "description": "让关键区域只有一两个入口，玩家必须决定什么时候进入、什么时候把箱子推出入口。压力来自入口被箱子临时堵住。",
                "promptText": "围绕原始想法设计入口控制；用少量入口、临时堵路和站位判断制造玩法。",
            },
            {
                "id": "C",
                "title": "目标区压缩",
                "description": "目标区更集中，最后几步需要小心安排两个箱子的落点。玩家要提前避免第一个箱子挡住第二个箱子的收尾路线。",
                "promptText": "围绕原始想法设计目标区压缩；用集中目标、收尾路线和两箱落点顺序形成解谜。",
            },
        ]

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


def contains_cjk(text):
    return any("\u4e00" <= character <= "\u9fff" for character in text or "")


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
    print(f"Starting backend at http://{HOST}:{PORT}")
    print(f"Opening {START_URL}")
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT)
