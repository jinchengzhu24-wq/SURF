"""Idempotent importer for legacy Backend/study_logs JSONL files."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .database import SQLiteDatabase, dump_json

_MATCH_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_PLAYER_FILE_RE = re.compile(r"^player-([12])\.jsonl$")

STUDY_LOG_SOURCES: tuple[tuple[str, str | None], ...] = (
    ("level_records.jsonl", None),
    ("survey_responses.jsonl", "survey"),
    ("creative_ideas.jsonl", "creative_idea"),
    ("creative_expansion_choices.jsonl", "expansion_choice"),
    ("ha_plan_events.jsonl", "ha_plan"),
    ("journey_events.jsonl", "journey"),
)

_LEVEL_EVENT_TYPE_MAP = {
    "level-start": "level_start",
    "level-end": "level_end",
}


def _reject_non_finite(constant: str) -> Any:
    raise ValueError(f"Non-finite JSON constant is not allowed: {constant}")


def _fallback_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _first_non_empty(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        raw = record.get(key)
        if raw is not None:
            text = str(raw).strip()
            if text and text != "-":
                return text
    return None


def _read_jsonl_dicts(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    records: list[dict[str, Any]] = []
    malformed = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped, parse_constant=_reject_non_finite)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                malformed += 1
    return records, malformed


def _resolve_record_type(
    filename: str, record: dict[str, Any], fixed_type: str | None
) -> str:
    if fixed_type is not None:
        return fixed_type
    raw_event = str(record.get("eventType") or "").strip()
    if raw_event in _LEVEL_EVENT_TYPE_MAP:
        return _LEVEL_EVENT_TYPE_MAP[raw_event]
    return raw_event or filename.removesuffix(".jsonl")


def import_study_logs(db: SQLiteDatabase, study_logs_dir: Path | str) -> dict[str, int]:
    """Import Backend/study_logs/*.jsonl and matches/*/*.jsonl idempotently."""
    base_dir = Path(study_logs_dir)
    if not base_dir.exists():
        raise FileNotFoundError(f"Study log directory does not exist: {base_dir}")

    stats = {
        "study_inserted": 0,
        "study_skipped": 0,
        "room_events_inserted": 0,
        "room_events_skipped": 0,
        "malformed_lines": 0,
    }

    with db.write_tx() as conn:
        existing_study = {
            (row["record_type"], row["payload_json"])
            for row in conn.execute("SELECT record_type, payload_json FROM study_records")
        }
        for filename, fixed_type in STUDY_LOG_SOURCES:
            records, malformed = _read_jsonl_dicts(base_dir / filename)
            stats["malformed_lines"] += malformed
            for record in records:
                record_type = _resolve_record_type(filename, record, fixed_type)
                payload_json = dump_json(record)
                dedup_key = (record_type, payload_json)
                if dedup_key in existing_study:
                    stats["study_skipped"] += 1
                    continue
                created_at = (
                    _first_non_empty(record, "serverReceivedAt", "timestamp", "createdAt")
                    or _fallback_iso()
                )
                participant_id = _first_non_empty(
                    record, "participantId", "playerId", "studySessionId", "userId"
                )
                round_id = _first_non_empty(record, "gameRoundId", "roundId", "ideaId")
                conn.execute(
                    "INSERT INTO study_records("
                    "record_type, match_id, participant_id, round_id, payload_json, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record_type,
                        _first_non_empty(record, "matchId", "studySessionId"),
                        participant_id,
                        round_id,
                        payload_json,
                        created_at,
                    ),
                )
                existing_study.add(dedup_key)
                stats["study_inserted"] += 1

        existing_events = {
            (row["match_id"], int(row["player_slot"]), row["event_type"], row["payload_json"])
            for row in conn.execute(
                "SELECT match_id, player_slot, event_type, payload_json FROM online_room_events"
            )
        }
        existing_event_ids = {
            (row["match_id"], int(row["player_slot"]), row["event_id"])
            for row in conn.execute(
                "SELECT match_id, player_slot, "
                "json_extract(payload_json, '$.eventId') AS event_id "
                "FROM online_room_events"
            )
            if row["event_id"]
        }

        def insert_room_event(match_id: str, player_slot: int, record: dict[str, Any]) -> None:
            event_type = str(record.get("eventType") or "unknown").strip() or "unknown"
            payload_json = dump_json(record)
            event_id = _first_non_empty(record, "eventId")
            if event_id and (match_id, player_slot, event_id) in existing_event_ids:
                stats["room_events_skipped"] += 1
                return
            if (match_id, player_slot, event_type, payload_json) in existing_events:
                stats["room_events_skipped"] += 1
                return
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                "FROM online_room_events WHERE match_id = ? AND player_slot = ?",
                (match_id, player_slot),
            ).fetchone()
            seq = int(seq_row["next_seq"]) if seq_row is not None else 1
            created_at = (
                _first_non_empty(record, "serverReceivedAt", "timestamp", "createdAt")
                or _fallback_iso()
            )
            conn.execute(
                "INSERT INTO online_room_events("
                "match_id, player_slot, seq, event_type, payload_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (match_id, player_slot, seq, event_type, payload_json, created_at),
            )
            existing_events.add((match_id, player_slot, event_type, payload_json))
            if event_id:
                existing_event_ids.add((match_id, player_slot, event_id))
            stats["room_events_inserted"] += 1

        match_records, malformed = _read_jsonl_dicts(base_dir / "online_match_events.jsonl")
        stats["malformed_lines"] += malformed
        for record in match_records:
            match_id = _first_non_empty(record, "matchId") or ""
            raw_slot = record.get("playerNumber")
            slot = int(raw_slot) if isinstance(raw_slot, int) and raw_slot in (0, 1, 2) else 0
            insert_room_event(match_id, slot, record)

        matches_dir = base_dir / "matches"
        if matches_dir.is_dir():
            for match_dir in sorted(matches_dir.iterdir()):
                if not match_dir.is_dir() or not _MATCH_ID_RE.fullmatch(match_dir.name):
                    continue
                for file_path in sorted(match_dir.iterdir()):
                    matched = _PLAYER_FILE_RE.fullmatch(file_path.name)
                    if not matched:
                        continue
                    slot = int(matched.group(1))
                    flow_records, malformed = _read_jsonl_dicts(file_path)
                    stats["malformed_lines"] += malformed
                    for record in flow_records:
                        insert_room_event(match_dir.name, slot, record)

    return stats
