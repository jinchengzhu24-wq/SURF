from __future__ import annotations

import math
from pathlib import Path
import re
import secrets
import shutil
import threading
from typing import Any
import uuid

from fastapi import HTTPException, Request

from ..config import (
    BackendSettings,
    Clock,
    ONLINE_AI_ASSISTANT_MODES,
    ONLINE_ROOM_CODE_ALPHABET,
    ONLINE_ROOM_CODE_PATTERN,
    safe_compare_digest,
)
from ..schemas import (
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
)
from ..storage.jsonl_store import StudyLogStore, normalize_identifier
from .pc_generator import validate_pc_rows


def normalize_online_room_code(room_code):
    normalized = str(room_code or "").strip().upper()

    if not ONLINE_ROOM_CODE_PATTERN.fullmatch(normalized):
        raise HTTPException(
            status_code=400,
            detail="Room code must contain exactly 6 letters or digits",
        )

    return normalized


def normalize_study_session_id(study_session_id):
    normalized = str(study_session_id or "").strip()

    if len(normalized) > 64:
        raise HTTPException(
            status_code=400,
            detail="Study session ID must contain at most 64 characters",
        )

    return normalized


def normalize_cocreation_sync_id(value, label):
    normalized = str(value or "").strip()

    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", normalized):
        raise HTTPException(status_code=400, detail=f"Invalid {label}")

    return normalized


def normalize_cocreation_cards(cards):
    if cards is None:
        return []

    if not isinstance(cards, list) or len(cards) > 8:
        raise HTTPException(status_code=400, detail="Invalid co-creation cards")

    normalized = []
    for card in cards:
        if not isinstance(card, dict):
            raise HTTPException(status_code=400, detail="Invalid co-creation card")

        card_type = str(card.get("type") or "").strip()
        text = str(card.get("text") or "").strip()
        if (
            not re.fullmatch(r"[a-z_]{1,40}", card_type)
            or not text
            or len(text) > 4000
        ):
            raise HTTPException(status_code=400, detail="Invalid co-creation card")
        normalized.append({"type": card_type, "text": text})

    return normalized


def build_cocreation_flow_event(
    room,
    payload,
    *,
    clock: Clock | None = None,
):
    resolved_clock = clock or Clock()
    event_type = str(payload.eventType or "").strip()
    if event_type not in {"first_stage", "stage", "opening", "turn", "final", "node"}:
        raise HTTPException(status_code=400, detail="Invalid co-creation event type")

    if payload.coCreationDurationSeconds is not None:
        duration = float(payload.coCreationDurationSeconds)
        if event_type != "final":
            raise HTTPException(
                status_code=400,
                detail="Co-creation duration is only valid for the final event",
            )
        if not math.isfinite(duration) or duration < 0 or duration > 1200:
            raise HTTPException(
                status_code=400,
                detail="Invalid co-creation duration",
            )
    else:
        duration = None

    event = {
        "eventId": normalize_cocreation_sync_id(payload.eventId, "event ID"),
        "eventType": event_type,
        "matchId": str(room.get("matchId") or "").strip(),
        "roomCode": str(room.get("roomCode") or "").strip(),
        "playerNumber": int(payload.playerNumber),
        "sessionId": normalize_cocreation_sync_id(payload.sessionId, "session ID"),
        "serverReceivedAt": resolved_clock.utc_timestamp(),
    }
    player = next(
        (
            item for item in room.get("players", [])
            if item.get("playerNumber") == payload.playerNumber
        ),
        None,
    )
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")
    event["studySessionId"] = str(player.get("studySessionId") or "").strip()

    if event_type in {"first_stage", "stage", "final"}:
        if not payload.versionId or payload.stageNumber is None or payload.rows is None:
            raise HTTPException(status_code=400, detail="Incomplete Stage event")
        try:
            validate_online_challenge_rows(payload.rows)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        event.update({
            "versionId": normalize_cocreation_sync_id(payload.versionId, "version ID"),
            "stageNumber": int(payload.stageNumber),
            "rows": list(payload.rows),
            "diff": list(payload.diff or []),
        })
        if event_type == "final" and duration is not None:
            event["coCreationDurationSeconds"] = round(duration, 2)
        if event_type == "stage":
            source = str(payload.source or "").strip()
            if source not in {"manual", "ai", "restored"}:
                raise HTTPException(status_code=400, detail="Invalid Stage source")
            event["source"] = source
            if source == "restored":
                for id_field, number_field in (
                    ("restoredFromVersionId", "restoredFromStageNumber"),
                    ("replacedVersionId", "replacedStageNumber"),
                ):
                    stage_number = getattr(payload, number_field)
                    if stage_number is None or stage_number < 1:
                        raise HTTPException(status_code=400, detail="Incomplete restored Stage")
                    event[id_field] = normalize_cocreation_sync_id(
                        getattr(payload, id_field), id_field,
                    )
                    event[number_field] = stage_number
        elif event_type == "first_stage":
            initial_draft_method = str(payload.initialDraftMethod or "").strip()
            if int(payload.stageNumber) != 1 or not re.fullmatch(
                r"[a-z_]{1,64}", initial_draft_method
            ):
                raise HTTPException(status_code=400, detail="Invalid first Stage event")
            event["initialDraftMethod"] = initial_draft_method

    if event_type == "opening":
        assistant_turn_id = normalize_cocreation_sync_id(
            payload.assistantTurnId,
            "assistant turn ID",
        )
        assistant_text = str(payload.assistantText or "").strip()
        language = str(payload.language or "").strip()
        if (
            not payload.versionId
            or payload.stageNumber is None
            or not assistant_text
            or len(assistant_text) > 12000
            or language not in {"en", "zh-CN"}
        ):
            raise HTTPException(status_code=400, detail="Incomplete Stage opening")
        event.update({
            "versionId": normalize_cocreation_sync_id(payload.versionId, "version ID"),
            "stageNumber": int(payload.stageNumber),
            "assistantTurnId": assistant_turn_id,
            "assistantText": assistant_text,
            "language": language,
            "cards": normalize_cocreation_cards(payload.cards),
        })

    if event_type == "turn":
        user_text = str(payload.userText or "").strip()
        assistant_text = str(payload.assistantText or "").strip()
        language = str(payload.language or "").strip()
        if not user_text or not assistant_text or len(user_text) > 2000 or len(assistant_text) > 12000:
            raise HTTPException(status_code=400, detail="Incomplete conversation turn")
        if language not in {"en", "zh-CN"}:
            raise HTTPException(status_code=400, detail="Invalid conversation language")
        event.update({
            "versionId": normalize_cocreation_sync_id(payload.versionId, "version ID"),
            "userText": user_text,
            "assistantText": assistant_text,
            "language": language,
            "cards": normalize_cocreation_cards(payload.cards),
        })
        opening_turn_id = str(payload.openingAssistantTurnId or "").strip()
        opening_text = str(payload.openingAssistantText or "").strip()
        opening_language = str(payload.openingLanguage or "").strip()
        opening_cards_supplied = payload.openingCards is not None
        opening_supplied = any(
            (opening_turn_id, opening_text, opening_language, opening_cards_supplied)
        )
        if opening_supplied:
            if (
                not opening_turn_id
                or not opening_text
                or len(opening_text) > 12000
                or opening_language not in {"en", "zh-CN"}
            ):
                raise HTTPException(status_code=400, detail="Incomplete opening conversation")
            event.update({
                "openingAssistantTurnId": normalize_cocreation_sync_id(
                    opening_turn_id,
                    "opening assistant turn ID",
                ),
                "openingAssistantText": opening_text,
                "openingLanguage": opening_language,
                "openingCards": normalize_cocreation_cards(payload.openingCards),
            })

    if event_type == "node":
        node_id = normalize_cocreation_sync_id(payload.nodeId, "node ID")
        node_type = str(payload.nodeType or "").strip()
        node_status = str(payload.nodeStatus or "").strip()
        parent_id = str(payload.nodeParentId or "").strip()
        allowed_node_types = {
            "discussion", "intent", "intent_conflict", "proposal",
            "player_challenge", "manual_edit_review", "llm_challenge",
        }
        allowed_entry_kinds = {
            "player_message", "llm_message", "card", "player_action",
            "map_diff", "status",
        }
        if node_type not in allowed_node_types or not re.fullmatch(
            r"[a-z0-9_]{2,64}", node_status
        ):
            raise HTTPException(status_code=400, detail="Invalid dashboard node")
        if parent_id:
            parent_id = normalize_cocreation_sync_id(parent_id, "parent node ID")
        raw_entries = payload.nodeEntries
        if not isinstance(raw_entries, list) or not raw_entries or len(raw_entries) > 96:
            raise HTTPException(status_code=400, detail="Invalid dashboard node entries")
        entries = []
        for item in raw_entries:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail="Invalid dashboard node entry")
            entry_id = normalize_cocreation_sync_id(item.get("entryId"), "node entry ID")
            kind = str(item.get("kind") or "").strip()
            text = str(item.get("text") or "").strip()
            label = str(item.get("label") or "").strip()
            occurred_at = str(item.get("occurredAt") or "").strip()
            language = str(item.get("language") or "").strip()
            if (
                kind not in allowed_entry_kinds
                or not text
                or len(text) > 12000
                or len(label) > 160
                or not occurred_at
                or (language and language not in {"en", "zh-CN"})
            ):
                raise HTTPException(status_code=400, detail="Invalid dashboard node entry")
            entries.append({
                "entryId": entry_id,
                "kind": kind,
                "text": text,
                "label": label,
                "occurredAt": occurred_at,
                "language": language or None,
            })
        event.update({
            "nodeId": node_id,
            "nodeType": node_type,
            "nodeStatus": node_status,
            "nodeParentId": parent_id or None,
            "nodeEntries": entries,
        })
        if payload.versionId:
            event["versionId"] = normalize_cocreation_sync_id(
                payload.versionId, "version ID"
            )
        if payload.stageNumber is not None:
            event["stageNumber"] = int(payload.stageNumber)

    return event


def normalize_online_draft_metadata(payload):
    metadata = {}
    for field in ONLINE_DRAFT_METADATA_KEYS:
        value = str(getattr(payload, field, "") or "").strip()
        if field in {"aiReflection", "aiDifficultyRationale", "aiLayoutRationale"}:
            max_length = 480 if field == "aiReflection" else 320
            if len(value) > max_length:
                raise HTTPException(
                    status_code=400,
                    detail=f"Draft {field} is too long",
                )
        metadata[field] = value

    for field, choices in ONLINE_DRAFT_ANSWER_CHOICES.items():
        if metadata[field] and metadata[field] not in choices:
            raise HTTPException(status_code=400, detail=f"Invalid Draft {field}")

    if (
        metadata["aiRecommendedDifficulty"]
        and metadata["aiRecommendedDifficulty"] not in {"Easy", "Medium", "Hard", "Random"}
    ):
        raise HTTPException(status_code=400, detail="Invalid Draft AI difficulty")
    if (
        metadata["aiRecommendedLayout"]
        and metadata["aiRecommendedLayout"] not in {"Compact", "Balanced", "Open", "Random"}
    ):
        raise HTTPException(status_code=400, detail="Invalid Draft AI layout")
    if (
        metadata["aiRecommendationSource"]
        and metadata["aiRecommendationSource"] not in ONLINE_DRAFT_RECOMMENDATION_SOURCES
    ):
        raise HTTPException(status_code=400, detail="Invalid Draft AI recommendation source")

    metadata_complete = all(metadata[field] for field in ONLINE_DRAFT_METADATA_KEYS)
    return metadata, metadata_complete


def _draft_snapshot_values(final_difficulty, final_layout, metadata):
    return {
        "finalDifficulty": str(final_difficulty or "").strip(),
        "finalLayout": str(final_layout or "").strip(),
        **{
            field: str(metadata.get(field) or "").strip()
            for field in ONLINE_DRAFT_METADATA_KEYS
        },
    }


def draft_events_match(existing_event, final_difficulty, final_layout, metadata, metadata_complete):
    existing_values = _draft_snapshot_values(
        existing_event.get("finalDifficulty"),
        existing_event.get("finalLayout"),
        existing_event,
    )
    requested_values = _draft_snapshot_values(final_difficulty, final_layout, metadata)
    existing_complete = bool(existing_event.get("draftMetadataComplete", False))
    return existing_values == requested_values and existing_complete == metadata_complete


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


def validate_online_result(payload):
    if (
        not math.isfinite(payload.durationSeconds)
        or payload.durationSeconds < 0
    ):
        raise HTTPException(
            status_code=400,
            detail="Result duration must be a finite non-negative number",
        )

    if payload.moveCount < 0 or payload.minimumMoves < 0 or payload.restartCount < 0:
        raise HTTPException(
            status_code=400,
            detail="Result move and restart counts must be non-negative",
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


class OnlineRoomManager:
    def __init__(
        self,
        store: StudyLogStore,
        settings: BackendSettings,
        clock: Clock | None = None,
        *,
        rooms: dict[str, dict[str, Any]] | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock or store.clock
        self.rooms: dict[str, dict[str, Any]] = {} if rooms is None else rooms
        self.lock = lock or threading.RLock()

    def cleanup_expired_online_rooms(self, now: float | None = None) -> None:
        current = self.clock.now() if now is None else float(now)
        ttl = self.settings.online_room_ttl_seconds
        with self.lock:
            expired_match_ids = [
                match_id
                for match_id, room in self.rooms.items()
                if current - room["lastActivity"] >= ttl
            ]

            for match_id in expired_match_ids:
                room = self.rooms[match_id]
                if room.get("status") not in {"results_ready", "cancelled"}:
                    self.append_online_match_event(
                        room,
                        "room_expired",
                        status_after="expired",
                    )
                del self.rooms[match_id]

    def create_unique_online_room_code(self) -> str:
        with self.lock:
            for _ in range(100):
                room_code = "".join(
                    secrets.choice(ONLINE_ROOM_CODE_ALPHABET) for _ in range(6)
                )
                if not any(
                    room["roomCode"] == room_code
                    for room in self.rooms.values()
                ):
                    return room_code

        raise HTTPException(
            status_code=503,
            detail="Unable to allocate a room code",
        )

    def append_online_match_event(
        self,
        room: dict[str, Any],
        event_type: str,
        player_number: int = 0,
        status_after: str | None = None,
        **details: Any,
    ) -> dict[str, Any] | None:
        player = None
        if player_number in (1, 2):
            player = next(
                (
                    item
                    for item in room.get("players", [])
                    if item.get("playerNumber") == player_number
                ),
                None,
            )
            if not bool(player and player.get("menuStartedFlow") is True):
                return None
        elif not any(
            item.get("menuStartedFlow") is True
            for item in room.get("players", [])
        ):
            return None

        event: dict[str, Any] = {
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
            "serverReceivedAt": self.clock.utc_timestamp(),
        }

        if player is not None:
            event["studySessionId"] = str(
                player.get("studySessionId") or ""
            ).strip()
            event["menuStartedFlow"] = True

        event.update(details)
        self.store.ensure_study_log_dir()
        self.store.append_jsonl_record(self.store.paths.online_match_log_file, event)
        return event

    def online_match_flow_directory(self, match_id: str) -> Path:
        return self.store.online_match_flow_directory(match_id)

    def online_match_flow_file(self, match_id: str, player_number: int) -> Path:
        return self.store.online_match_flow_file(match_id, player_number)

    def read_online_match_flow_events(
        self,
        match_id: str,
        player_number: int,
    ) -> tuple[list[dict[str, Any]], int]:
        return self.store.read_online_match_flow_events(match_id, player_number)

    def has_cocreation_first_stage(self, match_id: str, player_number: int) -> bool:
        flow_events, _ = self.read_online_match_flow_events(match_id, player_number)
        return any(
            isinstance(event, dict) and event.get("eventType") == "first_stage"
            for event in flow_events
        )

    def append_cocreation_flow_event(
        self,
        room: dict[str, Any],
        event: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        path = self.online_match_flow_file(event["matchId"], event["playerNumber"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.store.record_lock:
            existing, _ = self.store.read_jsonl_records(path)
            existing_event = next(
                (item for item in existing if item.get("eventId") == event["eventId"]),
                None,
            )
            if existing_event is not None:
                return existing_event, False
            self.store.append_jsonl_record(path, event)
        return event, True

    def find_existing_online_draft_event(
        self,
        match_id: str,
        player_number: int,
    ) -> dict[str, Any] | None:
        flow_events, _ = self.read_online_match_flow_events(match_id, player_number)
        return next(
            (item for item in flow_events if isinstance(item, dict) and item.get("eventType") == "draft"),
            None,
        )

    def append_online_draft_event(
        self,
        room: dict[str, Any],
        player: dict[str, Any],
        final_difficulty: str,
        final_layout: str,
        metadata: dict[str, Any],
        metadata_complete: bool,
    ) -> tuple[dict[str, Any], bool]:
        player_number = int(player["playerNumber"])
        path = self.online_match_flow_file(room["matchId"], player_number)
        path.parent.mkdir(parents=True, exist_ok=True)

        with self.store.record_lock:
            existing, _ = self.store.read_jsonl_records(path)
            existing_event = next(
                (item for item in existing if item.get("eventType") == "draft"),
                None,
            )
            if existing_event is not None:
                if not draft_events_match(
                    existing_event,
                    final_difficulty,
                    final_layout,
                    metadata,
                    metadata_complete,
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Draft record is already frozen",
                    )
                return existing_event, False

            event = {
                "eventId": f"draft:{room['matchId']}:{player_number}",
                "eventType": "draft",
                "matchId": str(room.get("matchId") or "").strip(),
                "roomCode": str(room.get("roomCode") or "").strip(),
                "playerNumber": player_number,
                "studySessionId": str(player.get("studySessionId") or "").strip(),
                "serverReceivedAt": self.clock.utc_timestamp(),
                "finalDifficulty": final_difficulty,
                "finalLayout": final_layout,
                **metadata,
                "draftMetadataComplete": metadata_complete,
            }
            self.store.append_jsonl_record(path, event)
        return event, True

    def materialize_pending_draft(
        self,
        room: dict[str, Any],
        player: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        pending = player.get("pendingDraft")
        if not isinstance(pending, dict):
            return None, False

        metadata = {
            field: pending.get(field, "")
            for field in ONLINE_DRAFT_METADATA_KEYS
        }
        stored_event, created = self.append_online_draft_event(
            room,
            player,
            pending["finalDifficulty"],
            pending["finalLayout"],
            metadata,
            bool(pending.get("draftMetadataComplete", False)),
        )
        player["pendingDraft"] = None
        return stored_event, created

    def append_cocreation_message(
        self,
        room: dict[str, Any],
        player_number: int,
        event_id: str,
        session_id: str,
        content: str,
    ) -> tuple[dict[str, Any], bool]:
        player = next(
            (
                item for item in room.get("players", [])
                if item.get("playerNumber") == player_number
            ),
            None,
        )
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")
        if not self.has_cocreation_first_stage(room["matchId"], player_number):
            raise HTTPException(
                status_code=409,
                detail="Co-creation flow has not started",
            )
        event = {
            "eventId": normalize_cocreation_sync_id(event_id, "event ID"),
            "eventType": "message",
            "matchId": str(room.get("matchId") or "").strip(),
            "roomCode": str(room.get("roomCode") or "").strip(),
            "playerNumber": player_number,
            "sessionId": normalize_cocreation_sync_id(session_id, "session ID"),
            "studySessionId": str(player.get("studySessionId") or "").strip(),
            "message": normalize_online_designer_intention(content),
            "serverReceivedAt": self.clock.utc_timestamp(),
        }
        return self.append_cocreation_flow_event(room, event)

    def require_online_room(
        self,
        match_id: str,
        player_token: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.cleanup_expired_online_rooms()
        room = self.rooms.get(str(match_id or "").strip())

        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")

        supplied_token = str(player_token or "").strip()
        player = next(
            (
                item
                for item in room["players"]
                if safe_compare_digest(item["token"], supplied_token)
            ),
            None,
        )

        if player is None:
            raise HTTPException(status_code=401, detail="Invalid player token")

        return room, player

    def require_cocreation_intention_sync_secret(self, request: Request) -> None:
        configured_secret = str(
            self.settings.cocreation_intention_sync_secret or ""
        ).strip()
        if not configured_secret:
            raise HTTPException(
                status_code=503,
                detail="Co-creation intention synchronization is not configured",
            )

        supplied_secret = request.headers.get("X-CoCreation-Sync-Secret", "")
        if not safe_compare_digest(supplied_secret, configured_secret):
            raise HTTPException(status_code=401, detail="Invalid co-creation sync secret")

    def create_room(
        self,
        payload: OnlineRoomCreateRequest | None = None,
    ) -> dict[str, Any]:
        study_session_id = normalize_study_session_id(
            payload.studySessionId if payload is not None else ""
        )

        with self.lock:
            self.cleanup_expired_online_rooms()
            player = {
                "playerNumber": 1,
                "studySessionId": study_session_id,
                "menuStartedFlow": payload.menuStartedFlow is True if payload else False,
                "token": secrets.token_urlsafe(32),
                "ready": False,
                "challengeRows": None,
                "aiAssistantMode": None,
                "designerIntention": None,
                "result": None,
                "pendingDraft": None,
            }
            match_id = uuid.uuid4().hex
            room = {
                "matchId": match_id,
                "roomCode": self.create_unique_online_room_code(),
                "status": "waiting_for_opponent",
                "players": [player],
                "lastActivity": self.clock.now(),
            }
            self.rooms[match_id] = room
            self.append_online_match_event(room, "room_created", player_number=1)
            return serialize_online_room(room, player, include_token=True)

    def join_room(self, payload: OnlineRoomJoinRequest) -> dict[str, Any]:
        room_code = normalize_online_room_code(payload.roomCode)
        study_session_id = normalize_study_session_id(payload.studySessionId)

        with self.lock:
            self.cleanup_expired_online_rooms()
            room = next(
                (
                    item
                    for item in self.rooms.values()
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
                "studySessionId": study_session_id,
                "menuStartedFlow": payload.menuStartedFlow is True,
                "token": secrets.token_urlsafe(32),
                "ready": False,
                "challengeRows": None,
                "aiAssistantMode": None,
                "designerIntention": None,
                "result": None,
                "pendingDraft": None,
            }
            room["players"].append(player)
            room["status"] = "briefing"
            room["lastActivity"] = self.clock.now()
            self.append_online_match_event(room, "player_joined", player_number=2)
            return serialize_online_room(room, player, include_token=True)

    def get_room(self, match_id: str, player_token: str | None) -> dict[str, Any]:
        with self.lock:
            room, player = self.require_online_room(match_id, player_token)
            room["lastActivity"] = self.clock.now()
            return serialize_online_room(room, player)

    def set_ready(
        self,
        match_id: str,
        payload: OnlineReadyRequest,
        player_token: str | None,
    ) -> dict[str, Any]:
        with self.lock:
            room, player = self.require_online_room(match_id, player_token)

            if room["status"] == "cancelled":
                raise HTTPException(status_code=409, detail="Room has been cancelled")
            if len(room["players"]) < 2:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot become ready before an opponent joins",
                )
            if room["status"] in {"challenges_ready", "waiting_for_results", "results_ready"}:
                if bool(payload.ready) and player.get("ready") is True:
                    room["lastActivity"] = self.clock.now()
                    return serialize_online_room(room, player)
                raise HTTPException(
                    status_code=409,
                    detail="Match has already advanced past briefing",
                )

            previous_ready = bool(player["ready"])
            player["ready"] = bool(payload.ready)
            room["status"] = (
                "waiting_for_challenges"
                if all(item["ready"] for item in room["players"])
                else "briefing"
            )
            room["lastActivity"] = self.clock.now()

            if previous_ready != player["ready"]:
                self.append_online_match_event(
                    room,
                    "ready_changed",
                    player_number=player["playerNumber"],
                    ready=player["ready"],
                )

            return serialize_online_room(room, player)

    def record_draft(
        self,
        match_id: str,
        payload: OnlineDraftRequest,
        player_token: str | None,
    ) -> dict[str, Any]:
        final_difficulty = str(payload.finalDifficulty or "").strip()
        final_layout = str(payload.finalLayout or "").strip()
        if final_difficulty not in {"Easy", "Medium", "Hard"}:
            raise HTTPException(status_code=400, detail="Invalid final difficulty")
        if final_layout not in {"Compact", "Balanced", "Open"}:
            raise HTTPException(status_code=400, detail="Invalid final layout")
        draft_metadata, metadata_complete = normalize_online_draft_metadata(payload)

        with self.lock:
            room, player = self.require_online_room(match_id, player_token)
            if room["status"] == "cancelled":
                raise HTTPException(status_code=409, detail="Room has been cancelled")
            if player.get("menuStartedFlow") is not True:
                return {
                    "status": "ok",
                    "recorded": False,
                    "pending": False,
                    "ignored": True,
                    "event": None,
                }
            requested_draft = {
                "finalDifficulty": final_difficulty,
                "finalLayout": final_layout,
                **draft_metadata,
                "draftMetadataComplete": metadata_complete,
            }
            pending_draft = player.get("pendingDraft")
            if pending_draft is not None and pending_draft != requested_draft:
                raise HTTPException(
                    status_code=409,
                    detail="Draft settings are already pending",
                )

            if self.has_cocreation_first_stage(match_id, player["playerNumber"]):
                existing_draft = self.find_existing_online_draft_event(
                    match_id, player["playerNumber"]
                )
                if existing_draft is not None:
                    if not draft_events_match(
                        existing_draft,
                        final_difficulty,
                        final_layout,
                        draft_metadata,
                        metadata_complete,
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="Draft record is already frozen",
                        )
                    room["lastActivity"] = self.clock.now()
                    return {
                        "status": "ok",
                        "recorded": False,
                        "pending": False,
                        "event": existing_draft,
                    }
                raise HTTPException(
                    status_code=409,
                    detail="Draft must be submitted before first_stage",
                )

            player["pendingDraft"] = requested_draft
            room["lastActivity"] = self.clock.now()
            return {
                "status": "ok",
                "recorded": False,
                "pending": True,
                "event": None,
            }

    def submit_challenge(
        self,
        match_id: str,
        payload: OnlineChallengeRequest,
        player_token: str | None,
    ) -> dict[str, Any]:
        try:
            validate_online_challenge_rows(payload.rows)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        validate_online_ai_assistant_mode(payload.aiAssistantMode)

        submitted_rows = list(payload.rows)
        with self.lock:
            room, player = self.require_online_room(match_id, player_token)

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

                room["lastActivity"] = self.clock.now()
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
            room["lastActivity"] = self.clock.now()
            self.append_online_match_event(
                room,
                "challenge_submitted",
                player_number=player["playerNumber"],
                aiAssistantMode=payload.aiAssistantMode,
                rows=submitted_rows,
            )
            return serialize_online_room(room, player)

    def synchronize_designer_intention(
        self,
        match_id: str,
        payload: OnlineDesignerIntentionSyncRequest,
        request: Request,
    ) -> dict[str, Any]:
        self.require_cocreation_intention_sync_secret(request)
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

        with self.lock:
            self.cleanup_expired_online_rooms()
            room = self.rooms.get(str(match_id or "").strip())

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

            if player.get("menuStartedFlow") is not True:
                room["lastActivity"] = self.clock.now()
                return {"status": "ok", "recorded": False, "ignored": True}

            if not self.has_cocreation_first_stage(match_id, payload.playerNumber):
                raise HTTPException(
                    status_code=409,
                    detail="Co-creation flow has not started",
                )

            existing_intention = str(player.get("designerIntention") or "").strip()

            if existing_intention and existing_intention != designer_intention:
                raise HTTPException(
                    status_code=409,
                    detail="Final designer intention is already frozen",
                )

            event_id = str(payload.eventId or "").strip()
            session_id = str(payload.sessionId or "").strip()
            room["lastActivity"] = self.clock.now()

            if not existing_intention:
                player["designerIntention"] = designer_intention
                self.append_online_match_event(
                    room,
                    "designer_intention_synchronized",
                    player_number=payload.playerNumber,
                    designerIntention=designer_intention,
                )

            if event_id and session_id:
                self.append_cocreation_message(
                    room,
                    payload.playerNumber,
                    event_id,
                    session_id,
                    designer_intention,
                )

            return serialize_online_room(room, player)

    def synchronize_cocreation_flow_event(
        self,
        match_id: str,
        payload: OnlineCoCreationFlowEventRequest,
        request: Request,
    ) -> dict[str, Any]:
        self.require_cocreation_intention_sync_secret(request)

        if payload.playerNumber not in (1, 2):
            raise HTTPException(status_code=400, detail="Invalid player number")

        with self.lock:
            self.cleanup_expired_online_rooms()
            room = self.rooms.get(str(match_id or "").strip())
            if room is None:
                raise HTTPException(status_code=404, detail="Room not found")

            player = next(
                (
                    item for item in room["players"]
                    if item["playerNumber"] == payload.playerNumber
                ),
                None,
            )
            if player is None:
                raise HTTPException(status_code=404, detail="Player not found")
            if player.get("menuStartedFlow") is not True:
                room["lastActivity"] = self.clock.now()
                return {"status": "ok", "recorded": False, "ignored": True}

            if (
                payload.eventType != "first_stage"
                and not self.has_cocreation_first_stage(match_id, payload.playerNumber)
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Co-creation flow has not started",
                )

            if payload.eventType == "first_stage":
                self.materialize_pending_draft(room, player)
            event = build_cocreation_flow_event(room, payload, clock=self.clock)
            room["lastActivity"] = self.clock.now()
            stored_event, created = self.append_cocreation_flow_event(room, event)
            return {"status": "ok", "recorded": created, "event": stored_event}

    def submit_result(
        self,
        match_id: str,
        payload: OnlineResultRequest,
        player_token: str | None,
    ) -> dict[str, Any]:
        validate_online_result(payload)
        submitted_result = {
            "durationSeconds": round(float(payload.durationSeconds), 2),
            "moveCount": int(payload.moveCount),
            "minimumMoves": int(payload.minimumMoves),
            "restartCount": int(payload.restartCount),
            "outcome": payload.outcome,
        }

        with self.lock:
            room, player = self.require_online_room(match_id, player_token)

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

                room["lastActivity"] = self.clock.now()
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
            room["lastActivity"] = self.clock.now()
            self.append_online_match_event(
                room,
                "result_submitted",
                player_number=player["playerNumber"],
                **submitted_result,
            )
            return serialize_online_room(room, player)

    def leave_room(self, match_id: str, player_token: str | None) -> dict[str, Any]:
        with self.lock:
            room, player = self.require_online_room(match_id, player_token)
            already_left = bool(player.get("left"))
            player["left"] = True
            room["lastActivity"] = self.clock.now()

            if room["status"] == "results_ready" or player.get("result") is not None:
                return serialize_online_room(room, player)

            room["status"] = "cancelled"

            if not already_left:
                self.append_online_match_event(
                    room,
                    "player_left",
                    player_number=player["playerNumber"],
                )

            return serialize_online_room(room, player)

    def delete_online_match(self, raw_match_id: str | None) -> dict[str, Any]:
        match_id = normalize_identifier(raw_match_id)
        if not match_id:
            raise HTTPException(status_code=400, detail="matchId is required")

        self.store.ensure_study_log_dir()

        with self.lock:
            with self.store.record_lock:
                match_events, _ = self.store.read_online_match_events()
                remaining_events = [
                    event
                    for event in match_events
                    if normalize_identifier(event.get("matchId")) != match_id
                ]
                deleted_event_count = len(match_events) - len(remaining_events)

                flow_directory = self.online_match_flow_directory(match_id)
                flow_exists = flow_directory.exists()

                if (
                    deleted_event_count == 0
                    and not flow_exists
                    and match_id not in self.rooms
                ):
                    raise HTTPException(status_code=404, detail="Online match not found")

                self.rooms.pop(match_id, None)
                self.store.write_jsonl_records(
                    self.store.paths.online_match_log_file,
                    remaining_events,
                )
                if flow_exists:
                    shutil.rmtree(flow_directory)

        return {
            "status": "ok",
            "matchId": match_id,
            "deletedEventCount": deleted_event_count,
        }

    def clear_matchmaking_records(self) -> dict[str, Any]:
        self.store.ensure_study_log_dir()

        with self.lock:
            with self.store.record_lock:
                self.rooms.clear()
                self.store.write_jsonl_records(
                    self.store.paths.online_match_log_file,
                    [],
                )
                flow_dir = self.store.paths.online_match_flow_dir
                if flow_dir.exists():
                    shutil.rmtree(flow_dir)

        return {
            "status": "ok",
            "cleared": True,
        }
