import json
import math
from pathlib import Path
import re
import secrets
import threading
import uuid

from fastapi import HTTPException

from ..config import (
    ONLINE_AI_ASSISTANT_MODES,
    ONLINE_ROOM_CODE_ALPHABET,
    ONLINE_ROOM_CODE_PATTERN,
    ONLINE_ROOM_TTL_SECONDS,
    safe_compare_digest,
)
from ..runtime_state import app_attr, current_time, utc_timestamp
from ..schemas import ONLINE_DRAFT_ANSWER_CHOICES, ONLINE_DRAFT_METADATA_KEYS, ONLINE_DRAFT_RECOMMENDATION_SOURCES
from ..storage.jsonl_store import append_jsonl_record, ensure_study_log_dir, read_jsonl_records
from .pc_generator import validate_pc_rows


ONLINE_ROOMS = {}
ONLINE_ROOMS_LOCK = threading.Lock()


def _rooms():
    rooms = app_attr("ONLINE_ROOMS")
    return ONLINE_ROOMS if rooms is None else rooms


def _ttl_seconds():
    value = app_attr("ONLINE_ROOM_TTL_SECONDS")
    return ONLINE_ROOM_TTL_SECONDS if value is None else value


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


def cleanup_expired_online_rooms(now=None):
    current = current_time() if now is None else float(now)
    rooms = _rooms()
    ttl = _ttl_seconds()
    expired_match_ids = [
        match_id
        for match_id, room in rooms.items()
        if current - room["lastActivity"] >= ttl
    ]

    for match_id in expired_match_ids:
        room = rooms[match_id]
        if room.get("status") not in {"results_ready", "cancelled"}:
            append_online_match_event(
                room,
                "room_expired",
                status_after="expired",
            )
        del rooms[match_id]


def create_unique_online_room_code():
    rooms = _rooms()
    for _ in range(100):
        room_code = "".join(
            secrets.choice(ONLINE_ROOM_CODE_ALPHABET) for _ in range(6)
        )

        if not any(room["roomCode"] == room_code for room in rooms.values()):
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
        "serverReceivedAt": utc_timestamp(),
    }

    if player is not None:
        event["studySessionId"] = str(
            player.get("studySessionId") or ""
        ).strip()
        event["menuStartedFlow"] = True

    event.update(details)
    ensure_study_log_dir()
    append_jsonl_record(Path(app_attr("ONLINE_MATCH_LOG_FILE")), event)
    return event


def online_match_flow_directory(match_id):
    normalized = str(match_id or "").strip()

    if not re.fullmatch(r"[a-f0-9]{32}", normalized):
        raise HTTPException(status_code=400, detail="Invalid match ID")

    return Path(app_attr("ONLINE_MATCH_FLOW_DIR")) / normalized


def online_match_flow_file(match_id, player_number):
    if player_number not in (1, 2):
        raise HTTPException(status_code=400, detail="Invalid player number")

    return online_match_flow_directory(match_id) / f"player-{player_number}.jsonl"


def read_online_match_flow_events(match_id, player_number):
    if not re.fullmatch(r"[a-f0-9]{32}", str(match_id or "").strip()):
        return [], 0
    return read_jsonl_records(online_match_flow_file(match_id, player_number))


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


def build_cocreation_flow_event(room, payload):
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
        "serverReceivedAt": utc_timestamp(),
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


def has_cocreation_first_stage(match_id, player_number):
    """Return whether this player's persisted co-creation flow has started."""
    flow_events, _ = read_online_match_flow_events(match_id, player_number)
    return any(
        isinstance(event, dict) and event.get("eventType") == "first_stage"
        for event in flow_events
    )


def append_cocreation_flow_event(room, event):
    path = online_match_flow_file(event["matchId"], event["playerNumber"])
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = app_attr("study_record_lock")
    with lock:
        existing, _ = read_jsonl_records(path)
        existing_event = next(
            (item for item in existing if item.get("eventId") == event["eventId"]),
            None,
        )
        if existing_event is not None:
            return existing_event, False
        append_jsonl_record(path, event)
    return event, True


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


def find_existing_online_draft_event(match_id, player_number):
    flow_events, _ = read_online_match_flow_events(match_id, player_number)
    return next(
        (item for item in flow_events if isinstance(item, dict) and item.get("eventType") == "draft"),
        None,
    )


def draft_events_match(existing_event, final_difficulty, final_layout, metadata, metadata_complete):
    existing_values = _draft_snapshot_values(
        existing_event.get("finalDifficulty"),
        existing_event.get("finalLayout"),
        existing_event,
    )
    requested_values = _draft_snapshot_values(final_difficulty, final_layout, metadata)
    existing_complete = bool(existing_event.get("draftMetadataComplete", False))
    return existing_values == requested_values and existing_complete == metadata_complete


def append_online_draft_event(
    room,
    player,
    final_difficulty,
    final_layout,
    metadata,
    metadata_complete,
):
    """Append one immutable pre-generation Draft node for this player."""
    player_number = int(player["playerNumber"])
    path = online_match_flow_file(room["matchId"], player_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = app_attr("study_record_lock")

    with lock:
        existing, _ = read_jsonl_records(path)
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
            "serverReceivedAt": utc_timestamp(),
            "finalDifficulty": final_difficulty,
            "finalLayout": final_layout,
            **metadata,
            "draftMetadataComplete": metadata_complete,
        }
        append_jsonl_record(path, event)
    return event, True


def materialize_pending_draft(room, player):
    """Persist a player's in-memory Draft immediately before first_stage."""
    pending = player.get("pendingDraft")
    if not isinstance(pending, dict):
        return None, False

    metadata = {
        field: pending.get(field, "")
        for field in ONLINE_DRAFT_METADATA_KEYS
    }
    stored_event, created = append_online_draft_event(
        room,
        player,
        pending["finalDifficulty"],
        pending["finalLayout"],
        metadata,
        bool(pending.get("draftMetadataComplete", False)),
    )
    player["pendingDraft"] = None
    return stored_event, created


def append_cocreation_message(room, player_number, event_id, session_id, content):
    player = next(
        (
            item for item in room.get("players", [])
            if item.get("playerNumber") == player_number
        ),
        None,
    )
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")
    if not has_cocreation_first_stage(room["matchId"], player_number):
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
        "serverReceivedAt": utc_timestamp(),
    }
    return append_cocreation_flow_event(room, event)


def require_online_room(match_id, player_token):
    cleanup_expired_online_rooms()
    room = _rooms().get(str(match_id or "").strip())

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
    configured_secret = str(app_attr("COCREATION_INTENTION_SYNC_SECRET") or "").strip()
    if not configured_secret:
        raise HTTPException(
            status_code=503,
            detail="Co-creation intention synchronization is not configured",
        )

    supplied_secret = request.headers.get("X-CoCreation-Sync-Secret", "")

    if not safe_compare_digest(supplied_secret, configured_secret):
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
