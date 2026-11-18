import asyncio
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

from fastapi import HTTPException, Request

from ..runtime_state import app_attr, current_time, utc_timestamp


study_record_lock = threading.RLock()


def _lock():
    return app_attr("study_record_lock") or study_record_lock


def _study_log_dir():
    return Path(app_attr("STUDY_LOG_DIR"))


def ensure_study_log_dir():
    _study_log_dir().mkdir(parents=True, exist_ok=True)


def normalize_identifier(value):
    if value is None:
        return ""
    return str(value).strip()


def normalize_identifier_or_empty(value):
    text = normalize_identifier(value)
    return "" if text == "-" else text


normalize_creative_idea_identifier = normalize_identifier_or_empty
normalize_expansion_choice_identifier = normalize_identifier_or_empty


def _normalize_identifier_component(value):
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
    encoded = [_normalize_identifier_component(c) for c in components]
    payload = json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return "choice-" + digest


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


def read_jsonl_records(path):
    path = Path(path)
    with _lock():
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


def read_raw_jsonl_text(path):
    path = Path(path)
    with _lock():
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")


def write_jsonl_records(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock():
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as log_file:
                for record in records:
                    log_file.write(json.dumps(record, ensure_ascii=False))
                    log_file.write("\n")
                log_file.flush()
                os.fsync(log_file.fileno())
            os.replace(temp_path, path)
        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise


def append_jsonl_record(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock():
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, ensure_ascii=False))
            log_file.write("\n")


async def _parse_json_dict(request: Request):
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exception
    if not isinstance(data, dict):
        data = {"payload": data}
    return data


async def append_level_record(request: Request, default_event_type: str):
    data = await _parse_json_dict(request)
    data.setdefault("eventType", default_event_type)
    data["serverReceivedAt"] = utc_timestamp()
    log_path = Path(app_attr("STUDY_LOG_FILE"))

    def _write():
        ensure_study_log_dir()
        append_jsonl_record(log_path, data)

    await asyncio.to_thread(_write)
    return {
        "status": "ok",
        "eventType": data["eventType"],
        "logFile": str(log_path),
    }


async def append_survey_record(request: Request):
    data = await _parse_json_dict(request)
    data.setdefault("eventType", "survey-response")
    data["serverReceivedAt"] = utc_timestamp()
    log_path = Path(app_attr("SURVEY_LOG_FILE"))

    def _write():
        ensure_study_log_dir()
        append_jsonl_record(log_path, data)

    await asyncio.to_thread(_write)
    return {
        "status": "ok",
        "eventType": data["eventType"],
        "logFile": str(log_path),
    }


async def append_creative_idea_record(request: Request):
    data = await _parse_json_dict(request)
    idea_text = str(data.get("ideaText") or data.get("idea") or "").strip()
    if not idea_text:
        raise HTTPException(status_code=400, detail="ideaText is required")
    data["ideaText"] = idea_text
    data.setdefault("eventType", "creative-idea")
    data.setdefault("ideaId", f"idea-{int(current_time() * 1000)}")
    data["serverReceivedAt"] = utc_timestamp()
    log_path = Path(app_attr("CREATIVE_IDEA_LOG_FILE"))

    def _write():
        ensure_study_log_dir()
        append_jsonl_record(log_path, data)

    await asyncio.to_thread(_write)
    return {
        "status": "ok",
        "eventType": data["eventType"],
        "ideaId": data["ideaId"],
        "logFile": str(log_path),
    }


async def append_expansion_choice_record(request: Request):
    data = await _parse_json_dict(request)
    idea_id = str(data.get("ideaId") or "").strip()
    selected_title = str(data.get("selectedOptionTitle") or "").strip()
    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")
    if not selected_title:
        raise HTTPException(status_code=400, detail="selectedOptionTitle is required")
    data.setdefault("eventType", "creative-expansion-choice")
    data["serverReceivedAt"] = utc_timestamp()
    if not normalize_expansion_choice_identifier(data.get("choiceId")):
        data["choiceId"] = build_expansion_choice_identifier(data)
    log_path = Path(app_attr("CREATIVE_EXPANSION_CHOICE_LOG_FILE"))

    def _write():
        ensure_study_log_dir()
        append_jsonl_record(log_path, data)

    await asyncio.to_thread(_write)
    return {
        "status": "ok",
        "eventType": data["eventType"],
        "choiceId": data["choiceId"],
        "ideaId": idea_id,
        "logFile": str(log_path),
    }


async def append_ha_plan_event(request: Request, default_event_type: str):
    data = await _parse_json_dict(request)
    idea_id = str(data.get("ideaId") or "").strip()
    selected_title = str(data.get("selectedOptionTitle") or "").strip()
    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")
    if default_event_type == "ha-plan-choice" and not selected_title:
        raise HTTPException(status_code=400, detail="selectedOptionTitle is required")
    data.setdefault("eventType", default_event_type)
    data["serverReceivedAt"] = utc_timestamp()
    await asyncio.to_thread(write_ha_plan_event, data)
    return {
        "status": "ok",
        "eventType": data["eventType"],
        "ideaId": idea_id,
        "logFile": str(app_attr("HA_PLAN_EVENT_LOG_FILE")),
    }


async def append_journey_event(request: Request):
    data = await _parse_json_dict(request)
    idea_id = str(data.get("ideaId") or "").strip()
    phase = str(data.get("phase") or "").strip()
    if not idea_id:
        raise HTTPException(status_code=400, detail="ideaId is required")
    if not phase:
        raise HTTPException(status_code=400, detail="phase is required")
    data.setdefault("eventType", "journey-event")
    data["serverReceivedAt"] = utc_timestamp()
    data["journeyEventId"] = build_journey_event_identifier(data)
    log_path = Path(app_attr("JOURNEY_EVENT_LOG_FILE"))

    def _write():
        ensure_study_log_dir()
        append_jsonl_record(log_path, data)

    await asyncio.to_thread(_write)
    return {
        "status": "ok",
        "eventType": data["eventType"],
        "journeyEventId": data["journeyEventId"],
        "ideaId": idea_id,
        "logFile": str(log_path),
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
        "serverReceivedAt": utc_timestamp(),
    }
    write_ha_plan_event(data)


def write_ha_plan_event(data):
    ensure_study_log_dir()
    append_jsonl_record(Path(app_attr("HA_PLAN_EVENT_LOG_FILE")), data)


def read_level_record_events():
    return read_jsonl_records(app_attr("STUDY_LOG_FILE"))


def read_survey_response_events():
    return read_jsonl_records(app_attr("SURVEY_LOG_FILE"))


def read_creative_idea_events():
    return read_jsonl_records(app_attr("CREATIVE_IDEA_LOG_FILE"))


def read_expansion_choice_events():
    return read_jsonl_records(app_attr("CREATIVE_EXPANSION_CHOICE_LOG_FILE"))


def read_ha_plan_events():
    return read_jsonl_records(app_attr("HA_PLAN_EVENT_LOG_FILE"))


def read_journey_events():
    return read_jsonl_records(app_attr("JOURNEY_EVENT_LOG_FILE"))


def read_online_match_events():
    return read_jsonl_records(app_attr("ONLINE_MATCH_LOG_FILE"))
