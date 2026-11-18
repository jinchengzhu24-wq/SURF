from __future__ import annotations

import asyncio
from collections.abc import Iterable
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any

from fastapi import HTTPException, Request

from ..config import Clock, StudyLogPaths


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


async def _parse_json_dict(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exception
    if not isinstance(data, dict):
        data = {"payload": data}
    return data


class StudyLogStore:
    def __init__(
        self,
        paths: StudyLogPaths,
        clock: Clock | None = None,
        record_lock: threading.RLock | None = None,
    ) -> None:
        self.paths = paths
        self.clock = clock or Clock()
        self.record_lock = record_lock or threading.RLock()

    def ensure_study_log_dir(self) -> None:
        self.paths.study_log_dir.mkdir(parents=True, exist_ok=True)

    def read_jsonl_records(self, path: Path | str) -> tuple[list[dict[str, Any]], int]:
        target = Path(path)
        with self.record_lock:
            if not target.exists():
                return [], 0

            records: list[dict[str, Any]] = []
            malformed_count = 0

            with target.open("r", encoding="utf-8") as log_file:
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

    def read_raw_jsonl_text(self, path: Path | str) -> str:
        target = Path(path)
        with self.record_lock:
            if not target.exists():
                return ""
            return target.read_text(encoding="utf-8")

    def write_jsonl_records(
        self,
        path: Path | str,
        records: Iterable[dict[str, Any]],
    ) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.record_lock:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=str(target.parent),
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as log_file:
                    for record in records:
                        log_file.write(json.dumps(record, ensure_ascii=False))
                        log_file.write("\n")
                    log_file.flush()
                    os.fsync(log_file.fileno())
                os.replace(temp_path, target)
            except Exception:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
                raise

    def append_jsonl_record(self, path: Path | str, record: dict[str, Any]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.record_lock:
            with target.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(record, ensure_ascii=False))
                log_file.write("\n")

    def online_match_flow_directory(self, match_id: str) -> Path:
        normalized = str(match_id or "").strip()
        if not re.fullmatch(r"[a-f0-9]{32}", normalized):
            raise HTTPException(status_code=400, detail="Invalid match ID")
        return self.paths.online_match_flow_dir / normalized

    def online_match_flow_file(self, match_id: str, player_number: int) -> Path:
        if player_number not in (1, 2):
            raise HTTPException(status_code=400, detail="Invalid player number")
        return self.online_match_flow_directory(match_id) / f"player-{player_number}.jsonl"

    def read_online_match_flow_events(
        self,
        match_id: str,
        player_number: int,
    ) -> tuple[list[dict[str, Any]], int]:
        if not re.fullmatch(r"[a-f0-9]{32}", str(match_id or "").strip()):
            return [], 0
        return self.read_jsonl_records(
            self.online_match_flow_file(match_id, player_number)
        )

    async def append_level_record(
        self,
        request: Request,
        default_event_type: str,
    ) -> dict[str, Any]:
        data = await _parse_json_dict(request)
        data.setdefault("eventType", default_event_type)
        data["serverReceivedAt"] = self.clock.utc_timestamp()
        log_path = self.paths.study_log_file

        def _write() -> None:
            self.ensure_study_log_dir()
            self.append_jsonl_record(log_path, data)

        await asyncio.to_thread(_write)
        return {
            "status": "ok",
            "eventType": data["eventType"],
            "logFile": str(log_path),
        }

    async def append_survey_record(self, request: Request) -> dict[str, Any]:
        data = await _parse_json_dict(request)
        data.setdefault("eventType", "survey-response")
        data["serverReceivedAt"] = self.clock.utc_timestamp()
        log_path = self.paths.survey_log_file

        def _write() -> None:
            self.ensure_study_log_dir()
            self.append_jsonl_record(log_path, data)

        await asyncio.to_thread(_write)
        return {
            "status": "ok",
            "eventType": data["eventType"],
            "logFile": str(log_path),
        }

    async def append_creative_idea_record(self, request: Request) -> dict[str, Any]:
        data = await _parse_json_dict(request)
        idea_text = str(data.get("ideaText") or data.get("idea") or "").strip()
        if not idea_text:
            raise HTTPException(status_code=400, detail="ideaText is required")
        data["ideaText"] = idea_text
        data.setdefault("eventType", "creative-idea")
        data.setdefault("ideaId", f"idea-{int(self.clock.now() * 1000)}")
        data["serverReceivedAt"] = self.clock.utc_timestamp()
        log_path = self.paths.creative_idea_log_file

        def _write() -> None:
            self.ensure_study_log_dir()
            self.append_jsonl_record(log_path, data)

        await asyncio.to_thread(_write)
        return {
            "status": "ok",
            "eventType": data["eventType"],
            "ideaId": data["ideaId"],
            "logFile": str(log_path),
        }

    async def append_expansion_choice_record(self, request: Request) -> dict[str, Any]:
        data = await _parse_json_dict(request)
        idea_id = str(data.get("ideaId") or "").strip()
        selected_title = str(data.get("selectedOptionTitle") or "").strip()
        if not idea_id:
            raise HTTPException(status_code=400, detail="ideaId is required")
        if not selected_title:
            raise HTTPException(status_code=400, detail="selectedOptionTitle is required")
        data.setdefault("eventType", "creative-expansion-choice")
        data["serverReceivedAt"] = self.clock.utc_timestamp()
        if not normalize_expansion_choice_identifier(data.get("choiceId")):
            data["choiceId"] = build_expansion_choice_identifier(data)
        log_path = self.paths.creative_expansion_choice_log_file

        def _write() -> None:
            self.ensure_study_log_dir()
            self.append_jsonl_record(log_path, data)

        await asyncio.to_thread(_write)
        return {
            "status": "ok",
            "eventType": data["eventType"],
            "choiceId": data["choiceId"],
            "ideaId": idea_id,
            "logFile": str(log_path),
        }

    async def append_ha_plan_event(
        self,
        request: Request,
        default_event_type: str,
    ) -> dict[str, Any]:
        data = await _parse_json_dict(request)
        idea_id = str(data.get("ideaId") or "").strip()
        selected_title = str(data.get("selectedOptionTitle") or "").strip()
        if not idea_id:
            raise HTTPException(status_code=400, detail="ideaId is required")
        if default_event_type == "ha-plan-choice" and not selected_title:
            raise HTTPException(status_code=400, detail="selectedOptionTitle is required")
        data.setdefault("eventType", default_event_type)
        data["serverReceivedAt"] = self.clock.utc_timestamp()
        await asyncio.to_thread(self.write_ha_plan_event, data)
        return {
            "status": "ok",
            "eventType": data["eventType"],
            "ideaId": idea_id,
            "logFile": str(self.paths.ha_plan_event_log_file),
        }

    async def append_journey_event(self, request: Request) -> dict[str, Any]:
        data = await _parse_json_dict(request)
        idea_id = str(data.get("ideaId") or "").strip()
        phase = str(data.get("phase") or "").strip()
        if not idea_id:
            raise HTTPException(status_code=400, detail="ideaId is required")
        if not phase:
            raise HTTPException(status_code=400, detail="phase is required")
        data.setdefault("eventType", "journey-event")
        data["serverReceivedAt"] = self.clock.utc_timestamp()
        data["journeyEventId"] = build_journey_event_identifier(data)
        log_path = self.paths.journey_event_log_file

        def _write() -> None:
            self.ensure_study_log_dir()
            self.append_jsonl_record(log_path, data)

        await asyncio.to_thread(_write)
        return {
            "status": "ok",
            "eventType": data["eventType"],
            "journeyEventId": data["journeyEventId"],
            "ideaId": idea_id,
            "logFile": str(log_path),
        }

    def append_ha_generation_event(
        self,
        request_data: dict[str, Any],
        options: list[dict[str, Any]],
        error: str = "",
    ) -> None:
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
            "serverReceivedAt": self.clock.utc_timestamp(),
        }
        self.write_ha_plan_event(data)

    def write_ha_plan_event(self, data: dict[str, Any]) -> None:
        self.ensure_study_log_dir()
        self.append_jsonl_record(self.paths.ha_plan_event_log_file, data)

    def read_level_record_events(self) -> tuple[list[dict[str, Any]], int]:
        return self.read_jsonl_records(self.paths.study_log_file)

    def read_survey_response_events(self) -> tuple[list[dict[str, Any]], int]:
        return self.read_jsonl_records(self.paths.survey_log_file)

    def read_creative_idea_events(self) -> tuple[list[dict[str, Any]], int]:
        return self.read_jsonl_records(self.paths.creative_idea_log_file)

    def read_expansion_choice_events(self) -> tuple[list[dict[str, Any]], int]:
        return self.read_jsonl_records(self.paths.creative_expansion_choice_log_file)

    def read_ha_plan_events(self) -> tuple[list[dict[str, Any]], int]:
        return self.read_jsonl_records(self.paths.ha_plan_event_log_file)

    def read_journey_events(self) -> tuple[list[dict[str, Any]], int]:
        return self.read_jsonl_records(self.paths.journey_event_log_file)

    def read_online_match_events(self) -> tuple[list[dict[str, Any]], int]:
        return self.read_jsonl_records(self.paths.online_match_log_file)
