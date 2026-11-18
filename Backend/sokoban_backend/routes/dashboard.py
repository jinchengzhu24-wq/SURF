from pathlib import Path
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ..config import require_delete_password
from ..llm.runtime import readiness_payload
from ..runtime_state import app_attr
from ..schemas import (
    DeleteCreativeIdeaRequest,
    DeleteExpansionChoiceRequest,
    DeleteHAPlanEventRequest,
    DeleteIdeaRecordsRequest,
    DeleteJourneyEventRequest,
    DeleteLevelRunRequest,
    DeleteOnlineMatchRequest,
    DeleteSurveyResponseRequest,
    RenameLevelRunRequest,
    RenameRoundRequest,
    RoundRequest,
)
from ..services.dashboard_projections import (
    build_creative_ideas_payload,
    build_expansion_choices_payload,
    build_ha_plan_events_payload,
    build_journey_events_payload,
    build_level_records_payload,
    build_matchmaking_records_payload,
    build_survey_records_payload,
    filter_frontend_ha_plan_records,
    filter_frontend_records,
    get_survey_player_identifier,
    is_level_event_in_round,
    merge_level_records,
    normalize_level_display_name,
    normalize_level_run_id,
    normalize_round_display_name,
    normalize_round_id,
    normalize_survey_identifier,
    render_level_records_view,
)
from ..services.online_rooms import ONLINE_ROOMS, ONLINE_ROOMS_LOCK, online_match_flow_directory
from ..storage.jsonl_store import (
    append_creative_idea_record,
    append_expansion_choice_record,
    append_ha_plan_event,
    append_journey_event,
    append_level_record,
    append_survey_record,
    build_expansion_choice_identifier,
    build_ha_plan_event_identifier,
    build_journey_event_identifier,
    ensure_study_log_dir,
    normalize_creative_idea_identifier,
    normalize_expansion_choice_identifier,
    read_creative_idea_events,
    read_expansion_choice_events,
    read_ha_plan_events,
    read_journey_events,
    read_level_record_events,
    read_online_match_events,
    read_raw_jsonl_text,
    read_survey_response_events,
    study_record_lock,
    write_jsonl_records,
)


router = APIRouter()


def _record_lock():
    return app_attr("study_record_lock") or study_record_lock


def _rooms_lock():
    return app_attr("ONLINE_ROOMS_LOCK") or ONLINE_ROOMS_LOCK


def _rooms():
    rooms = app_attr("ONLINE_ROOMS")
    return ONLINE_ROOMS if rooms is None else rooms


@router.post("/verify-dashboard-password")
def verify_dashboard_password(_authorized=Depends(require_delete_password)):
    """Validate the existing dashboard password without changing any records."""
    return {"ok": True}


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/ready")
def ready():
    payload = readiness_payload()
    return JSONResponse(
        status_code=200 if payload["status"] == "ready" else 503,
        content=payload,
    )


@router.post("/record-level-start")
async def record_level_start(request: Request):
    return await append_level_record(request, "level-start")


@router.post("/record-level-end")
async def record_level_end(request: Request):
    return await append_level_record(request, "level-end")


@router.post("/record-survey-response")
async def record_survey_response(request: Request):
    return await append_survey_record(request)


@router.post("/record-creative-idea")
async def record_creative_idea(request: Request):
    return await append_creative_idea_record(request)


@router.post("/record-expansion-choice")
async def record_expansion_choice(request: Request):
    return await append_expansion_choice_record(request)


@router.post("/record-ha-plan-choice")
async def record_ha_plan_choice(request: Request):
    return await append_ha_plan_event(request, "ha-plan-choice")


@router.post("/record-journey-event")
async def record_journey_event(request: Request):
    return await append_journey_event(request)


@router.get("/level-records", response_class=PlainTextResponse)
def get_level_records():
    return read_raw_jsonl_text(app_attr("STUDY_LOG_FILE"))


@router.get("/survey-records", response_class=PlainTextResponse)
def get_survey_records():
    return read_raw_jsonl_text(app_attr("SURVEY_LOG_FILE"))


@router.get("/matchmaking-records", response_class=PlainTextResponse)
def get_matchmaking_records():
    return read_raw_jsonl_text(app_attr("ONLINE_MATCH_LOG_FILE"))


@router.get("/creative-ideas", response_class=PlainTextResponse)
def get_creative_ideas():
    return read_raw_jsonl_text(app_attr("CREATIVE_IDEA_LOG_FILE"))


@router.get("/survey-records-data")
def get_survey_records_data():
    responses, malformed_count = read_survey_response_events()
    return build_survey_records_payload(responses, malformed_count)


@router.get("/matchmaking-records-data")
def get_matchmaking_records_data():
    events, malformed_count = read_online_match_events()
    return build_matchmaking_records_payload(events, malformed_count)


@router.get("/creative-ideas-data")
def get_creative_ideas_data():
    ideas, malformed_count = read_creative_idea_events()
    return build_creative_ideas_payload(ideas, malformed_count)


@router.get("/level-records-view", response_class=HTMLResponse)
def get_level_records_view(cleared: int = 0):
    target = "/frontend/"
    if cleared == 1:
        target += "?cleared=1"
    return RedirectResponse(target, status_code=302)


@router.get("/level-records-dashboard")
def get_level_records_dashboard(cleared: int = 0):
    target = "/frontend/"
    if cleared == 1:
        target += "?cleared=1"
    return RedirectResponse(target, status_code=302)


@router.get("/level-records-data")
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


@router.get("/level-records-legacy", response_class=HTMLResponse)
def get_level_records_legacy(cleared: int = 0):
    events, malformed_count = read_level_record_events()
    levels = merge_level_records(events)
    return render_level_records_view(events, levels, malformed_count, cleared == 1)


@router.post("/rename-round")
def rename_round(request: RenameRoundRequest):
    round_id = normalize_round_id(request.roundId)
    display_name = normalize_round_display_name(request.displayName)

    if not round_id:
        raise HTTPException(status_code=400, detail="roundId is required")

    if round_id == "legacy-round":
        raise HTTPException(status_code=400, detail="Legacy Round cannot be renamed")

    if not display_name:
        raise HTTPException(status_code=400, detail="displayName is required")

    ensure_study_log_dir()

    with _record_lock():
        records, _ = read_level_record_events()
        matched_count = 0

        for record in records:
            if is_level_event_in_round(record, round_id):
                record["roundDisplayName"] = display_name
                matched_count += 1

        if matched_count == 0:
            raise HTTPException(status_code=404, detail="Round not found")

        write_jsonl_records(app_attr("STUDY_LOG_FILE"), records)

    return {
        "status": "ok",
        "roundId": round_id,
        "displayName": display_name,
        "updatedEventCount": matched_count,
    }


@router.post("/rename-level-run")
def rename_level_run(request: RenameLevelRunRequest):
    level_run_id = normalize_level_run_id(request.levelRunId)
    display_name = normalize_level_display_name(request.displayName)

    if not level_run_id:
        raise HTTPException(status_code=400, detail="levelRunId is required")

    if not display_name:
        raise HTTPException(status_code=400, detail="displayName is required")

    ensure_study_log_dir()

    with _record_lock():
        records, _ = read_level_record_events()
        matched_count = 0

        for record in records:
            record_level_run_id = normalize_level_run_id(record.get("levelRunId"))

            if record_level_run_id == level_run_id:
                record["levelDisplayName"] = display_name
                matched_count += 1

        if matched_count == 0:
            raise HTTPException(status_code=404, detail="Level run not found")

        write_jsonl_records(app_attr("STUDY_LOG_FILE"), records)

    return {
        "status": "ok",
        "levelRunId": level_run_id,
        "displayName": display_name,
        "updatedEventCount": matched_count,
    }


def _filter_records_or_404(path, reader, predicate, not_found_message):
    ensure_study_log_dir()
    with _record_lock():
        records, _ = reader()
        remaining_records = []
        deleted_count = 0

        for record in records:
            if predicate(record):
                deleted_count += 1
            else:
                remaining_records.append(record)

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail=not_found_message)

        write_jsonl_records(path, remaining_records)
    return deleted_count


@router.post("/delete-round", dependencies=[Depends(require_delete_password)])
def delete_round(request: RoundRequest):
    round_id = normalize_round_id(request.roundId)
    if not round_id:
        raise HTTPException(status_code=400, detail="roundId is required")

    deleted_count = _filter_records_or_404(
        app_attr("STUDY_LOG_FILE"),
        read_level_record_events,
        lambda record: is_level_event_in_round(record, round_id),
        "Round not found",
    )
    return {
        "status": "ok",
        "roundId": round_id,
        "deletedEventCount": deleted_count,
    }


@router.post("/delete-level-run", dependencies=[Depends(require_delete_password)])
def delete_level_run(request: DeleteLevelRunRequest):
    level_run_id = normalize_level_run_id(request.levelRunId)
    if not level_run_id:
        raise HTTPException(status_code=400, detail="levelRunId is required")

    deleted_count = _filter_records_or_404(
        app_attr("STUDY_LOG_FILE"),
        read_level_record_events,
        lambda record: normalize_level_run_id(record.get("levelRunId")) == level_run_id,
        "Level run not found",
    )
    return {
        "status": "ok",
        "levelRunId": level_run_id,
        "deletedEventCount": deleted_count,
    }


@router.post(
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

    ensure_study_log_dir()

    with _record_lock():
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

        write_jsonl_records(app_attr("SURVEY_LOG_FILE"), remaining_records)

    return {
        "status": "ok",
        "deletedResponseCount": deleted_count,
    }


@router.post(
    "/delete-creative-idea",
    dependencies=[Depends(require_delete_password)],
)
def delete_creative_idea(request: DeleteCreativeIdeaRequest):
    idea_id = normalize_creative_idea_identifier(request.ideaId)
    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")

    deleted_count = _filter_records_or_404(
        app_attr("CREATIVE_IDEA_LOG_FILE"),
        read_creative_idea_events,
        lambda record: normalize_creative_idea_identifier(
            record.get("ideaId") or record.get("id")
        ) == idea_id,
        "Creative idea not found",
    )
    return {
        "status": "ok",
        "ideaId": idea_id,
        "deletedIdeaCount": deleted_count,
    }


@router.post(
    "/delete-expansion-choice",
    dependencies=[Depends(require_delete_password)],
)
def delete_expansion_choice(request: DeleteExpansionChoiceRequest):
    choice_id = normalize_expansion_choice_identifier(request.choiceId)
    if not choice_id:
        raise HTTPException(status_code=400, detail="choiceId is required")

    deleted_count = _filter_records_or_404(
        app_attr("CREATIVE_EXPANSION_CHOICE_LOG_FILE"),
        read_expansion_choice_events,
        lambda record: (
            normalize_expansion_choice_identifier(record.get("choiceId"))
            or build_expansion_choice_identifier(record)
        ) == choice_id,
        "Expansion choice not found",
    )
    return {
        "status": "ok",
        "choiceId": choice_id,
        "deletedChoiceCount": deleted_count,
    }


@router.post(
    "/delete-ha-plan-event",
    dependencies=[Depends(require_delete_password)],
)
def delete_ha_plan_event(request: DeleteHAPlanEventRequest):
    ha_event_id = normalize_expansion_choice_identifier(request.haEventId)
    if not ha_event_id:
        raise HTTPException(status_code=400, detail="haEventId is required")

    deleted_count = _filter_records_or_404(
        app_attr("HA_PLAN_EVENT_LOG_FILE"),
        read_ha_plan_events,
        lambda record: build_ha_plan_event_identifier(record) == ha_event_id,
        "HA plan event not found",
    )
    return {
        "status": "ok",
        "haEventId": ha_event_id,
        "deletedHAPlanEventCount": deleted_count,
    }


@router.post(
    "/delete-journey-event",
    dependencies=[Depends(require_delete_password)],
)
def delete_journey_event(request: DeleteJourneyEventRequest):
    journey_event_id = normalize_expansion_choice_identifier(request.journeyEventId)
    if not journey_event_id:
        raise HTTPException(status_code=400, detail="journeyEventId is required")

    deleted_count = _filter_records_or_404(
        app_attr("JOURNEY_EVENT_LOG_FILE"),
        read_journey_events,
        lambda record: build_journey_event_identifier(record) == journey_event_id,
        "Journey event not found",
    )
    return {
        "status": "ok",
        "journeyEventId": journey_event_id,
        "deletedJourneyEventCount": deleted_count,
    }


@router.post(
    "/delete-idea-records",
    dependencies=[Depends(require_delete_password)],
)
def delete_idea_records(request: DeleteIdeaRecordsRequest):
    idea_id = normalize_creative_idea_identifier(request.ideaId)
    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")

    ensure_study_log_dir()

    with _record_lock():
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

        write_jsonl_records(app_attr("STUDY_LOG_FILE"), remaining_level_records)
        write_jsonl_records(app_attr("CREATIVE_IDEA_LOG_FILE"), remaining_idea_records)
        write_jsonl_records(
            app_attr("CREATIVE_EXPANSION_CHOICE_LOG_FILE"),
            remaining_choice_records,
        )
        write_jsonl_records(app_attr("SURVEY_LOG_FILE"), remaining_survey_records)
        write_jsonl_records(app_attr("HA_PLAN_EVENT_LOG_FILE"), remaining_ha_plan_records)
        write_jsonl_records(app_attr("JOURNEY_EVENT_LOG_FILE"), remaining_journey_records)

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


@router.post(
    "/delete-online-match",
    dependencies=[Depends(require_delete_password)],
)
def delete_online_match(request: DeleteOnlineMatchRequest):
    match_id = normalize_survey_identifier(request.matchId)

    if not match_id:
        raise HTTPException(status_code=400, detail="matchId is required")

    ensure_study_log_dir()

    with _rooms_lock():
        with _record_lock():
            match_events, _ = read_online_match_events()
            remaining_events = [
                event
                for event in match_events
                if normalize_survey_identifier(event.get("matchId")) != match_id
            ]
            deleted_event_count = len(match_events) - len(remaining_events)

            flow_directory = online_match_flow_directory(match_id)
            flow_exists = flow_directory.exists()

            if deleted_event_count == 0 and not flow_exists and match_id not in _rooms():
                raise HTTPException(status_code=404, detail="Online match not found")

            _rooms().pop(match_id, None)
            write_jsonl_records(app_attr("ONLINE_MATCH_LOG_FILE"), remaining_events)
            if flow_exists:
                shutil.rmtree(flow_directory)

    return {
        "status": "ok",
        "matchId": match_id,
        "deletedEventCount": deleted_event_count,
    }


@router.post(
    "/clear-matchmaking-records",
    dependencies=[Depends(require_delete_password)],
)
def clear_matchmaking_records():
    ensure_study_log_dir()

    with _rooms_lock():
        with _record_lock():
            _rooms().clear()
            write_jsonl_records(app_attr("ONLINE_MATCH_LOG_FILE"), [])
            flow_dir = Path(app_attr("ONLINE_MATCH_FLOW_DIR"))
            if flow_dir.exists():
                shutil.rmtree(flow_dir)

    return {
        "status": "ok",
        "cleared": True,
    }


@router.post(
    "/clear-level-records",
    dependencies=[Depends(require_delete_password)],
)
def clear_level_records():
    ensure_study_log_dir()

    with _record_lock():
        for key in (
            "STUDY_LOG_FILE",
            "SURVEY_LOG_FILE",
            "CREATIVE_IDEA_LOG_FILE",
            "CREATIVE_EXPANSION_CHOICE_LOG_FILE",
            "HA_PLAN_EVENT_LOG_FILE",
            "JOURNEY_EVENT_LOG_FILE",
        ):
            write_jsonl_records(app_attr(key), [])

    return RedirectResponse("/level-records-view?cleared=1", status_code=303)
