"""Pure Matchmaking Dashboard projection builder from online_room_events."""

from __future__ import annotations

import math
from typing import Any

from server.persistence.models import OnlineRoomEventRecord

_MATCH_LIFECYCLE_EVENTS: frozenset[str] = frozenset(
    {
        "room_created",
        "player_joined",
        "ready_changed",
        "challenge_submitted",
        "designer_intention_synchronized",
        "result_submitted",
        "player_left",
        "room_expired",
    }
)
_FLOW_EVENTS: frozenset[str] = frozenset(
    {"draft", "first_stage", "stage", "opening", "turn", "final", "message", "node"}
)
_ALLOWED_MATCH_EVENT_FIELDS: frozenset[str] = frozenset(
    {
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
        "restartCount",
        "outcome",
        "studySessionId",
        "menuStartedFlow",
    }
)
_ALLOWED_FLOW_EVENT_FIELDS: frozenset[str] = frozenset(
    {
        "eventId",
        "eventType",
        "matchId",
        "roomCode",
        "playerNumber",
        "studySessionId",
        "sessionId",
        "serverReceivedAt",
        "versionId",
        "stageNumber",
        "source",
        "initialDraftMethod",
        "rows",
        "diff",
        "userText",
        "assistantText",
        "language",
        "cards",
        "assistantTurnId",
        "openingAssistantTurnId",
        "openingAssistantText",
        "openingLanguage",
        "openingCards",
        "message",
        "finalDifficulty",
        "finalLayout",
        "q1Answer",
        "q2Answer",
        "q3Answer",
        "q4Answer",
        "aiReflection",
        "aiDifficultyRationale",
        "aiLayoutRationale",
        "aiRecommendedDifficulty",
        "aiRecommendedLayout",
        "aiRecommendationSource",
        "draftMetadataComplete",
        "coCreationDurationSeconds",
        "opponentRestartCount",
        "nodeId",
        "nodeType",
        "nodeStatus",
        "nodeParentId",
        "nodeEntries",
        "nodeUpdatedAt",
        "restoredFromVersionId",
        "restoredFromStageNumber",
        "replacedVersionId",
        "replacedStageNumber",
    }
)


def clean_id(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _create_player_projection(player_number: int) -> dict[str, Any]:
    return {
        "playerNumber": player_number,
        "joinedAt": None,
        "ready": False,
        "readyAt": None,
        "leftAt": None,
        "challenge": None,
        "result": None,
        "designerIntention": "",
        "studySessionId": "",
        "menuStartedFlow": False,
        "coCreationFlow": [],
    }


def _aggregate_node_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse immutable node snapshots into the latest public node view."""
    nodes: dict[str, tuple[int, dict[str, Any]]] = {}
    passthrough: list[tuple[int, dict[str, Any]]] = []
    for index, event in enumerate(events):
        node_id = event.get("nodeId")
        if event.get("eventType") != "node" or not node_id:
            passthrough.append((index, event))
            continue
        prior = nodes.get(node_id)
        if prior is None:
            record = dict(event)
            record["nodeUpdatedAt"] = event.get("serverReceivedAt")
            nodes[node_id] = (index, record)
            continue
        prior_index, prior_record = prior
        if str(event.get("serverReceivedAt") or "") >= str(
            prior_record.get("nodeUpdatedAt") or ""
        ):
            replacement = dict(event)
            replacement["serverReceivedAt"] = prior_record.get("serverReceivedAt")
            replacement["nodeUpdatedAt"] = event.get("serverReceivedAt")
            nodes[node_id] = (prior_index, replacement)
    combined = passthrough + list(nodes.values())
    combined.sort(key=lambda item: (str(item[1].get("serverReceivedAt") or ""), item[0]))
    return [item[1] for item in combined]


def _apply_lifecycle_event(
    match_record: dict[str, Any],
    record: OnlineRoomEventRecord,
    event: dict[str, Any],
) -> None:
    match_id = match_record["matchId"]
    room_code = clean_id(event.get("roomCode"))
    if room_code and not match_record["roomCode"]:
        match_record["roomCode"] = room_code

    safe_event = {k: v for k, v in event.items() if k in _ALLOWED_MATCH_EVENT_FIELDS}
    safe_event["matchId"] = match_id
    safe_event["roomCode"] = room_code or match_record["roomCode"]
    timestamp = clean_id(event.get("serverReceivedAt") or record.created_at)
    if timestamp and not safe_event.get("serverReceivedAt"):
        safe_event["serverReceivedAt"] = timestamp
    match_record["events"].append(safe_event)

    if timestamp:
        if not match_record["createdAt"]:
            match_record["createdAt"] = timestamp
        match_record["updatedAt"] = timestamp

    player_number = record.player_slot
    if player_number not in (1, 2):
        raw_num = event.get("playerNumber")
        is_valid_int = isinstance(raw_num, int) and not isinstance(raw_num, bool)
        player_number = int(raw_num) if is_valid_int else 0

    player = match_record["players"].get(player_number)
    event_type = clean_id(event.get("eventType") or record.event_type)

    if player is not None:
        session_id = clean_id(event.get("studySessionId"))
        player["studySessionId"] = session_id or player["studySessionId"]
        if event.get("menuStartedFlow") is True:
            player["menuStartedFlow"] = True

    if event_type == "room_created":
        match_record["createdAt"] = timestamp or match_record["createdAt"]
        p1 = match_record["players"][1]
        p1["joinedAt"] = p1["joinedAt"] or timestamp
    elif event_type == "player_joined" and player is not None:
        player["joinedAt"] = player["joinedAt"] or timestamp
    elif event_type == "ready_changed" and player is not None:
        player["ready"] = event.get("ready") is True
        player["readyAt"] = timestamp
    elif event_type == "challenge_submitted" and player is not None:
        rows = event.get("rows")
        player["challenge"] = {
            "submittedAt": timestamp,
            "aiAssistantMode": clean_id(event.get("aiAssistantMode")),
            "rows": list(rows) if isinstance(rows, list) else [],
        }
    elif event_type == "designer_intention_synchronized" and player is not None:
        player["designerIntention"] = str(event.get("designerIntention") or "").strip()
    elif event_type == "result_submitted" and player is not None:
        result_entry: dict[str, Any] = {
            "submittedAt": timestamp,
            "durationSeconds": event.get("durationSeconds"),
            "moveCount": event.get("moveCount"),
            "minimumMoves": event.get("minimumMoves"),
            "outcome": event.get("outcome", "completed"),
        }
        if "restartCount" in event:
            result_entry["restartCount"] = event.get("restartCount")
        player["result"] = result_entry
    elif event_type == "player_left" and player is not None:
        player["leftAt"] = timestamp


def build_matchmaking_projection(
    records: list[OnlineRoomEventRecord],
    *,
    generated_at: str,
    source_label: str,
) -> dict[str, Any]:
    """Build exact Matchmaking Dashboard payload from persisted room/flow events."""
    lifecycle: list[OnlineRoomEventRecord] = []
    flow_by_slot: dict[tuple[str, int], list[OnlineRoomEventRecord]] = {}

    for rec in records:
        event_type = clean_id(rec.payload.get("eventType") or rec.event_type)
        if event_type in _FLOW_EVENTS and rec.player_slot in (1, 2):
            flow_by_slot.setdefault((rec.match_id, rec.player_slot), []).append(rec)
        elif event_type in _MATCH_LIFECYCLE_EVENTS:
            lifecycle.append(rec)

    lifecycle.sort(
        key=lambda r: (clean_id(r.payload.get("serverReceivedAt") or r.created_at), r.id)
    )

    matches: dict[str, dict[str, Any]] = {}
    for rec in lifecycle:
        match_id = clean_id(rec.payload.get("matchId") or rec.match_id)
        if not match_id:
            continue
        if match_id not in matches:
            matches[match_id] = {
                "matchId": match_id,
                "roomCode": clean_id(rec.payload.get("roomCode")),
                "status": "in_progress",
                "createdAt": None,
                "updatedAt": None,
                "completedAt": None,
                "endedAt": None,
                "playerCount": 0,
                "players": {
                    1: _create_player_projection(1),
                    2: _create_player_projection(2),
                },
                "events": [],
            }
        _apply_lifecycle_event(matches[match_id], rec, rec.payload)

    result_records: list[dict[str, Any]] = []
    total_duration = 0.0
    duration_count = 0
    completed_count = 0
    cancelled_count = 0
    expired_count = 0

    for match_record in matches.values():
        match_id = match_record["matchId"]
        players = match_record["players"]

        for slot in (1, 2):
            slot_records = flow_by_slot.get((match_id, slot), [])
            has_first = any(
                clean_id(r.payload.get("eventType") or r.event_type) == "first_stage"
                for r in slot_records
            )
            indexed_flow: list[tuple[int, dict[str, Any]]] = []
            if players[slot]["menuStartedFlow"] and has_first:
                for idx, rec in enumerate(slot_records):
                    safe_flow = {
                        k: v for k, v in rec.payload.items() if k in _ALLOWED_FLOW_EVENT_FIELDS
                    }
                    if not safe_flow.get("serverReceivedAt") and rec.created_at:
                        safe_flow["serverReceivedAt"] = rec.created_at
                    indexed_flow.append((idx, safe_flow))

            indexed_flow.sort(
                key=lambda item: (str(item[1].get("serverReceivedAt") or ""), item[0])
            )
            aggregated = _aggregate_node_events([item[1] for item in indexed_flow])
            players[slot]["coCreationFlow"] = aggregated
            if aggregated:
                latest_ts = max(
                    (
                        str(ev.get("nodeUpdatedAt") or ev.get("serverReceivedAt") or "")
                        for ev in aggregated
                    ),
                    default="",
                )
                if latest_ts > str(match_record.get("updatedAt") or ""):
                    match_record["updatedAt"] = latest_ts

        for creator_slot, creator in players.items():
            if creator["challenge"] is not None:
                opp_slot = 3 - creator_slot
                opp_result = players[opp_slot]["result"]
                creator["challenge"]["createdByPlayerNumber"] = creator_slot
                creator["challenge"]["playedByPlayerNumber"] = opp_slot
                creator["challenge"]["runResult"] = (
                    dict(opp_result) if opp_result is not None else None
                )

        for creator in players.values():
            opp_result = players[3 - creator["playerNumber"]]["result"]
            if opp_result is None or "restartCount" not in opp_result:
                continue
            for flow_ev in creator["coCreationFlow"]:
                if flow_ev.get("eventType") == "final":
                    flow_ev["opponentRestartCount"] = opp_result.get("restartCount")

        if not any(p["menuStartedFlow"] and p["coCreationFlow"] for p in players.values()):
            continue

        completed = all(players[slot]["result"] is not None for slot in (1, 2))
        event_types = {ev.get("eventType") for ev in match_record["events"]}

        if completed:
            match_record["status"] = "completed"
            completed_count += 1
            result_times = [
                players[slot]["result"].get("submittedAt")
                for slot in (1, 2)
                if players[slot]["result"] is not None
            ]
            match_record["completedAt"] = max((v for v in result_times if v), default=None)
            match_record["endedAt"] = match_record["completedAt"]
        elif "player_left" in event_types:
            match_record["status"] = "cancelled"
            cancelled_count += 1
            match_record["endedAt"] = match_record["updatedAt"]
        elif "room_expired" in event_types:
            match_record["status"] = "expired"
            expired_count += 1
            match_record["endedAt"] = match_record["updatedAt"]

        for player in players.values():
            res = player["result"]
            if res is not None and _is_finite_number(res.get("durationSeconds")):
                total_duration += float(res["durationSeconds"])
                duration_count += 1

        match_record["playerCount"] = sum(1 for p in players.values() if p["joinedAt"])
        for player in players.values():
            player.pop("menuStartedFlow", None)
        match_record["players"] = [players[1], players[2]]
        result_records.append(match_record)

    result_records.sort(key=lambda m: str(m.get("updatedAt") or ""), reverse=True)
    average_duration = total_duration / duration_count if duration_count else 0.0
    total_matches = len(result_records)
    return {
        "summary": {
            "matchCount": total_matches,
            "completedCount": completed_count,
            "inProgressCount": total_matches - completed_count - cancelled_count - expired_count,
            "cancelledCount": cancelled_count,
            "expiredCount": expired_count,
            "averageRunDurationSeconds": round(average_duration, 2),
            "malformedCount": 0,
        },
        "matches": result_records,
        "malformedCount": 0,
        "logFile": source_label,
        "generatedAt": generated_at,
    }
