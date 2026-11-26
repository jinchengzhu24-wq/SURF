"""Async study records persistence and JSONL export service."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

from server.core.clock import SYSTEM_CLOCK, Clock
from server.domain.errors import ValidationError
from server.persistence.database import dump_json
from server.persistence.repositories.study_repo import StudyRepository

StudyRecordKind = Literal["survey", "level_start", "level_end", "journey"]

DEFAULT_MAX_PAYLOAD_BYTES = 65_536

_DEFAULT_EVENT_TYPES: dict[str, str] = {
    "survey": "survey-response",
    "level_start": "level-start",
    "level_end": "level-end",
    "journey": "journey-event",
}


def _first_non_empty(record: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        raw = record.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text and text != "-":
            return text
    return None


def _build_journey_event_id(payload: Mapping[str, Any]) -> str:
    existing = _first_non_empty(payload, "journeyEventId")
    if existing:
        return existing
    identity = {
        "eventType": payload.get("eventType"),
        "ideaId": payload.get("ideaId"),
        "sessionId": payload.get("sessionId"),
        "gameRoundId": payload.get("gameRoundId"),
        "phase": payload.get("phase"),
        "action": payload.get("action"),
        "timestamp": payload.get("timestamp"),
        "serverReceivedAt": payload.get("serverReceivedAt"),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


class StudyRecordsService:
    """Async service for recording bounded study events and streaming JSONL exports."""

    def __init__(
        self,
        study_repo: StudyRepository,
        *,
        clock: Clock | None = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        self._repo = study_repo
        self._clock = clock if clock is not None else SYSTEM_CLOCK
        self._max_payload_bytes = max(256, int(max_payload_bytes))

    def _validate_and_enrich(
        self,
        record_type: StudyRecordKind,
        raw_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(raw_payload, Mapping):
            raise ValidationError(
                "INVALID_STUDY_PAYLOAD",
                "error.validation_failed",
                message="Study record payload must be a JSON object",
            )
        payload = dict(raw_payload)
        payload.setdefault("eventType", _DEFAULT_EVENT_TYPES[record_type])
        now_iso = self._clock.utc_now()
        payload["serverReceivedAt"] = now_iso

        if record_type == "journey":
            idea_id = str(payload.get("ideaId") or "").strip()
            phase = str(payload.get("phase") or "").strip()
            if not idea_id:
                raise ValidationError(
                    "IDEA_ID_REQUIRED",
                    "error.validation_failed",
                    message="ideaId is required",
                )
            if not phase:
                raise ValidationError(
                    "PHASE_REQUIRED",
                    "error.validation_failed",
                    message="phase is required",
                )
            payload["ideaId"] = idea_id
            payload["phase"] = phase
            payload["journeyEventId"] = _build_journey_event_id(payload)

        try:
            encoded = dump_json(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "INVALID_STUDY_PAYLOAD",
                "error.validation_failed",
                message="Study payload contains non-JSON or non-finite values",
            ) from exc

        if len(encoded) > self._max_payload_bytes:
            raise ValidationError(
                "STUDY_PAYLOAD_TOO_LARGE",
                "error.validation_failed",
                message=(
                    f"Study payload size ({len(encoded)} bytes) exceeds "
                    f"limit ({self._max_payload_bytes} bytes)"
                ),
            )
        return payload

    async def record_event(
        self,
        record_type: StudyRecordKind,
        raw_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate, bound, and persist one study event record."""
        if record_type not in _DEFAULT_EVENT_TYPES:
            raise ValidationError(
                "INVALID_STUDY_RECORD_TYPE",
                "error.validation_failed",
                message=f"Unsupported study record type: {record_type}",
            )
        payload = self._validate_and_enrich(record_type, raw_payload)
        created_at = str(payload["serverReceivedAt"])
        match_id = _first_non_empty(payload, "matchId", "studySessionId")
        participant_id = _first_non_empty(
            payload, "participantId", "playerId", "studySessionId", "userId"
        )
        round_id = _first_non_empty(payload, "gameRoundId", "roundId", "ideaId")

        stored = await asyncio.to_thread(
            self._repo.insert_record,
            record_type,
            payload,
            match_id=match_id,
            participant_id=participant_id,
            round_id=round_id,
            created_at=created_at,
        )
        response: dict[str, Any] = {
            "status": "ok",
            "id": stored.id,
            "recordType": stored.record_type,
            "eventType": payload["eventType"],
        }
        if record_type == "journey":
            response["journeyEventId"] = payload["journeyEventId"]
            response["ideaId"] = payload["ideaId"]
        return response

    async def record_survey(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Record a survey response into study_records."""
        return await self.record_event("survey", payload)

    async def record_level_start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Record a level-start telemetry event into study_records."""
        return await self.record_event("level_start", payload)

    async def record_level_end(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Record a level-end telemetry event into study_records."""
        return await self.record_event("level_end", payload)

    async def record_journey(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Record a study journey event into study_records."""
        return await self.record_event("journey", payload)

    async def export_jsonl(
        self,
        record_type: str | None = None,
        *,
        chunk_size: int = 500,
    ) -> AsyncIterator[str]:
        """Stream JSONL lines cleanly using keyset pagination off the event loop."""
        bounded_chunk = max(1, min(5000, int(chunk_size)))
        iterator = self._repo.iter_jsonl_export(record_type, chunk_size=bounded_chunk)

        def _next_batch() -> list[str]:
            batch: list[str] = []
            for _ in range(bounded_chunk):
                line = next(iterator, None)
                if line is None:
                    break
                batch.append(line)
            return batch

        while True:
            batch = await asyncio.to_thread(_next_batch)
            if not batch:
                break
            for line in batch:
                yield line
