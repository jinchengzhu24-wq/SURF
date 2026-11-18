import html

from ..config import Clock, STUDY_LOG_DIR, StudyLogPaths
from ..storage.jsonl_store import (
    StudyLogStore,
    build_expansion_choice_identifier,
    build_ha_plan_event_identifier,
    build_journey_event_identifier,
    normalize_creative_idea_identifier,
    normalize_expansion_choice_identifier,
    normalize_identifier,
)

_DEFAULT_PATHS = StudyLogPaths(STUDY_LOG_DIR)


def aggregate_dashboard_node_events(events):
    """Collapse immutable node snapshots into the latest public node view."""
    nodes = {}
    passthrough = []
    for index, event in enumerate(events):
        if event.get("eventType") != "node" or not event.get("nodeId"):
            passthrough.append((index, event))
            continue
        node_id = event["nodeId"]
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


def build_matchmaking_records_payload(
    events,
    malformed_count,
    *,
    store: StudyLogStore | None = None,
    paths: StudyLogPaths | None = None,
    clock: Clock | None = None,
):
    resolved_paths = paths or (store.paths if store is not None else _DEFAULT_PATHS)
    resolved_clock = clock or (store.clock if store is not None else Clock())
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
        "restartCount",
        "outcome",
        "studySessionId",
        "menuStartedFlow",
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
            "studySessionId": "",
            "menuStartedFlow": False,
            "coCreationFlow": [],
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

    indexed_events = [
        (index, event)
        for index, event in enumerate(events)
        if isinstance(event, dict)
    ]
    indexed_events.sort(
        key=lambda item: (str(item[1].get("serverReceivedAt") or ""), item[0])
    )
    ordered_events = [item[1] for item in indexed_events]

    for event in ordered_events:
        match_id = normalize_identifier(event.get("matchId"))

        if not match_id:
            continue

        room_code = normalize_identifier(event.get("roomCode"))
        match_record = get_match(match_id, room_code)
        safe_event = {
            key: value
            for key, value in event.items()
            if key in allowed_event_fields
        }
        safe_event["matchId"] = match_id
        safe_event["roomCode"] = room_code or match_record["roomCode"]
        match_record["events"].append(safe_event)
        timestamp = normalize_identifier(event.get("serverReceivedAt"))

        if timestamp:
            if not match_record["createdAt"]:
                match_record["createdAt"] = timestamp
            match_record["updatedAt"] = timestamp

        try:
            player_number = int(event.get("playerNumber") or 0)
        except (TypeError, ValueError):
            player_number = 0

        player = match_record["players"].get(player_number)
        event_type = normalize_identifier(event.get("eventType"))

        if player is not None:
            player["studySessionId"] = normalize_identifier(
                event.get("studySessionId")
            ) or player["studySessionId"]
            if event.get("menuStartedFlow") is True:
                player["menuStartedFlow"] = True

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
                "aiAssistantMode": normalize_identifier(
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
            if "restartCount" in event:
                player["result"]["restartCount"] = event.get("restartCount")
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

        for player_number in (1, 2):
            if store is not None:
                flow_events, flow_malformed_count = store.read_online_match_flow_events(
                    match_record["matchId"],
                    player_number,
                )
            else:
                flow_events, flow_malformed_count = [], 0
            malformed_count += flow_malformed_count
            has_first_stage = any(
                isinstance(event, dict) and event.get("eventType") == "first_stage"
                for event in flow_events
            )
            indexed_flow = []
            if players[player_number]["menuStartedFlow"] and has_first_stage:
                for seq_index, event in enumerate(flow_events):
                    if not isinstance(event, dict):
                        continue
                    if event.get("eventType") not in {
                        "draft", "first_stage", "stage", "opening", "turn", "final", "message", "node"
                    }:
                        continue
                    indexed_flow.append((
                        seq_index,
                        {
                            key: value
                            for key, value in event.items()
                            if key in {
                                "eventId", "eventType", "matchId", "roomCode",
                                "playerNumber", "studySessionId", "sessionId",
                                "serverReceivedAt", "versionId", "stageNumber",
                                "source", "initialDraftMethod", "rows", "diff", "userText",
                                "assistantText", "language", "cards", "assistantTurnId",
                                "openingAssistantTurnId", "openingAssistantText",
                                "openingLanguage", "openingCards", "message", "finalDifficulty", "finalLayout",
                                "q1Answer", "q2Answer", "q3Answer", "q4Answer", "aiReflection",
                                "aiDifficultyRationale", "aiLayoutRationale", "aiRecommendedDifficulty",
                                "aiRecommendedLayout", "aiRecommendationSource", "draftMetadataComplete",
                                "coCreationDurationSeconds", "opponentRestartCount",
                                "nodeId", "nodeType", "nodeStatus", "nodeParentId",
                                "nodeEntries", "nodeUpdatedAt",
                                "restoredFromVersionId", "restoredFromStageNumber",
                                "replacedVersionId", "replacedStageNumber",
                            }
                        },
                    ))
            indexed_flow.sort(
                key=lambda item: (str(item[1].get("serverReceivedAt") or ""), item[0])
            )
            safe_flow_events = [item[1] for item in indexed_flow]
            aggregated_flow_events = aggregate_dashboard_node_events(safe_flow_events)
            players[player_number]["coCreationFlow"] = aggregated_flow_events
            if aggregated_flow_events:
                latest_timestamp = str(
                    max(
                        (
                            event.get("nodeUpdatedAt")
                            or event.get("serverReceivedAt")
                            or ""
                            for event in aggregated_flow_events
                        ),
                        default="",
                    )
                )
                if latest_timestamp > str(match_record.get("updatedAt") or ""):
                    match_record["updatedAt"] = latest_timestamp

        for creator_number, creator in players.items():
            if creator["challenge"] is not None:
                opponent_number = 3 - creator_number
                creator["challenge"]["createdByPlayerNumber"] = creator_number
                creator["challenge"]["playedByPlayerNumber"] = opponent_number
                opponent_result = players[opponent_number]["result"]
                creator["challenge"]["runResult"] = (
                    dict(opponent_result) if opponent_result is not None else None
                )

        for creator in players.values():
            opponent_result = players[3 - creator["playerNumber"]]["result"]
            for flow_event in creator["coCreationFlow"]:
                if flow_event.get("eventType") != "final":
                    continue
                if opponent_result is not None and "restartCount" in opponent_result:
                    flow_event["opponentRestartCount"] = opponent_result.get("restartCount")

        if not any(
            player["menuStartedFlow"]
            and player["coCreationFlow"]
            for player in players.values()
        ):
            continue

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
        elif "player_left" in event_types:
            match_record["status"] = "cancelled"
            cancelled_count += 1
            match_record["endedAt"] = match_record["updatedAt"]
        elif "room_expired" in event_types:
            match_record["status"] = "expired"
            expired_count += 1
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
        for player in players.values():
            player.pop("menuStartedFlow", None)
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
        "logFile": str(resolved_paths.online_match_log_file),
        "generatedAt": resolved_clock.utc_timestamp(),
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


def build_level_records_payload(
    events,
    levels,
    malformed_count,
    *,
    paths: StudyLogPaths | None = None,
    clock: Clock | None = None,
):
    resolved_paths = paths or _DEFAULT_PATHS
    resolved_clock = clock or Clock()
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
        "logFile": str(resolved_paths.study_log_file),
        "generatedAt": resolved_clock.utc_timestamp(),
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


def build_survey_records_payload(
    responses,
    malformed_count,
    *,
    paths: StudyLogPaths | None = None,
    clock: Clock | None = None,
):
    resolved_paths = paths or _DEFAULT_PATHS
    resolved_clock = clock or Clock()
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
        "logFile": str(resolved_paths.survey_log_file),
        "generatedAt": resolved_clock.utc_timestamp(),
    }


def build_creative_ideas_payload(
    ideas,
    malformed_count,
    *,
    paths: StudyLogPaths | None = None,
    clock: Clock | None = None,
):
    resolved_paths = paths or _DEFAULT_PATHS
    resolved_clock = clock or Clock()
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
        "logFile": str(resolved_paths.creative_idea_log_file),
        "generatedAt": resolved_clock.utc_timestamp(),
    }


def build_expansion_choices_payload(
    choices,
    malformed_count,
    *,
    paths: StudyLogPaths | None = None,
    clock: Clock | None = None,
):
    resolved_paths = paths or _DEFAULT_PATHS
    resolved_clock = clock or Clock()
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
        "logFile": str(resolved_paths.creative_expansion_choice_log_file),
        "generatedAt": resolved_clock.utc_timestamp(),
    }


def build_ha_plan_events_payload(
    events,
    malformed_count,
    *,
    paths: StudyLogPaths | None = None,
    clock: Clock | None = None,
):
    resolved_paths = paths or _DEFAULT_PATHS
    resolved_clock = clock or Clock()
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
        "logFile": str(resolved_paths.ha_plan_event_log_file),
        "generatedAt": resolved_clock.utc_timestamp(),
    }


def build_journey_events_payload(
    events,
    malformed_count,
    *,
    paths: StudyLogPaths | None = None,
    clock: Clock | None = None,
):
    resolved_paths = paths or _DEFAULT_PATHS
    resolved_clock = clock or Clock()
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
        "logFile": str(resolved_paths.journey_event_log_file),
        "generatedAt": resolved_clock.utc_timestamp(),
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


def render_level_records_view(
    events,
    levels,
    malformed_count,
    cleared,
    *,
    paths: StudyLogPaths | None = None,
):
    resolved_paths = paths or _DEFAULT_PATHS
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

    study_file = str(resolved_paths.study_log_file)
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
        h1 {{ margin: 0 0 8px; font-size: 28px; }}
        .meta {{ margin-bottom: 18px; color: #5e6875; }}
        .summary {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }}
        .stat {{
            min-width: 130px; padding: 12px 14px; border: 1px solid #d9e0ea;
            border-radius: 6px; background: #ffffff;
        }}
        .stat strong {{ display: block; font-size: 22px; color: #17202a; }}
        .toolbar {{ display: flex; align-items: center; gap: 14px; margin: 18px 0; }}
        .toolbar a {{ color: #175cd3; text-decoration: none; }}
        .danger-button {{
            padding: 7px 11px; border: 1px solid #c7372f; border-radius: 4px;
            color: #ffffff; background: #c7372f; cursor: pointer; font-size: 13px;
        }}
        .danger-button:hover {{ background: #a82d27; }}
        .notice {{
            margin: 12px 0 18px; padding: 10px 12px; border: 1px solid #b7dfc1;
            border-radius: 6px; color: #14532d; background: #eaf8ee;
        }}
        table {{
            width: 100%; border-collapse: collapse; background: #ffffff;
            border: 1px solid #d9e0ea;
        }}
        th, td {{
            padding: 8px 10px; border-bottom: 1px solid #e6ebf2;
            vertical-align: top; text-align: left; font-size: 13px;
        }}
        th {{ position: sticky; top: 0; background: #edf2f7; z-index: 1; }}
        tr:nth-child(even) td {{ background: #fafcff; }}
        .status-completed {{ color: #16703b; font-weight: 700; }}
        .status-missing {{ color: #a45c00; font-weight: 700; }}
        .map {{
            margin: 0; padding: 8px; min-width: 160px; border-radius: 4px;
            background: #17202a; color: #f7fafc; font-family: Consolas, "Courier New", monospace;
            font-size: 12px; line-height: 1.25; white-space: pre;
        }}
        .small {{ color: #697586; font-size: 12px; }}
        .empty {{ padding: 24px; text-align: center; color: #697586; }}
        .delete-dialog {{
            width: min(440px, calc(100vw - 28px)); padding: 0;
            border: 4px solid #41494d; border-radius: 4px; background: #efe2c4;
            box-shadow: inset 0 0 0 2px #c8c9c3, 6px 6px 0 rgba(25, 29, 26, 0.55);
        }}
        .delete-dialog::backdrop {{ background: rgba(20, 29, 23, 0.72); }}
        .delete-dialog h2 {{
            margin: 0; padding: 15px 18px; border-bottom: 4px solid #4a2d1c;
            color: #fff5d1; background: #8b562c; font-family: Consolas, "Courier New", monospace;
            text-shadow: 2px 2px 0 #382318;
        }}
        .delete-dialog-body {{ padding: 18px; }}
        .delete-dialog label {{
            display: grid; gap: 7px; font-family: Consolas, "Courier New", monospace; font-weight: 700;
        }}
        .delete-dialog input {{
            min-height: 40px; padding: 7px 10px; border: 3px solid #41494d;
            border-radius: 2px; font-family: Consolas, "Courier New", monospace;
            font-size: 17px; letter-spacing: 3px;
        }}
        .delete-error {{
            min-height: 18px; margin: 8px 0 0; color: #7b281d;
            font-family: Consolas, "Courier New", monospace; font-size: 13px; font-weight: 700;
        }}
        .delete-dialog-actions {{
            display: flex; justify-content: flex-end; gap: 10px;
            padding: 13px 18px 17px; border-top: 3px solid #8e673e; background: #d2b583;
        }}
        .cancel-button {{
            padding: 7px 11px; border: 2px solid #41494d; border-radius: 2px;
            background: #c8c9c3; cursor: pointer;
        }}
    </style>
</head>
<body>
    <h1>Sokoban Level Records</h1>
    <div class="meta">Human-readable view generated from <code>{escape_text(study_file)}</code>.</div>
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
                <th>Round</th><th>Level</th><th>Source</th><th>Status</th><th>Duration</th>
                <th>Moves</th><th>Pushes</th><th>Restarts</th><th>Solution</th><th>Solver Pushes</th>
                <th>Attempts</th><th>Wall</th><th>Water</th><th>Dead Corner</th><th>Map Hash</th><th>Map</th>
            </tr>
        </thead>
        <tbody>{rows_html}</tbody>
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
                passwordInput.value = ""; errorText.textContent = ""; dialog.showModal();
                requestAnimationFrame(() => passwordInput.focus());
            }});
            cancelButton.addEventListener("click", () => dialog.close());
            form.addEventListener("submit", async event => {{
                event.preventDefault(); errorText.textContent = "";
                passwordInput.disabled = true; cancelButton.disabled = true;
                confirmButton.disabled = true; confirmButton.textContent = "Clearing...";
                try {{
                    const response = await fetch("/clear-level-records", {{
                        method: "POST", headers: {{ "X-Delete-Password": passwordInput.value }}
                    }});
                    if (!response.ok) {{
                        let message = "Could not clear records.";
                        try {{ const data = await response.json(); message = data.detail || message; }}
                        catch (error) {{ message = "HTTP " + response.status; }}
                        throw new Error(message);
                    }}
                    window.location.href = "/level-records-view?cleared=1";
                }} catch (error) {{
                    errorText.textContent = error.message === "Incorrect delete password"
                        ? "Incorrect password. Please try again." : error.message;
                    passwordInput.disabled = false; cancelButton.disabled = false;
                    confirmButton.disabled = false; confirmButton.textContent = "Clear Records";
                    passwordInput.focus(); passwordInput.select();
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


normalize_round_id = normalize_identifier
normalize_round_display_name = normalize_identifier
normalize_level_run_id = normalize_identifier
normalize_level_display_name = normalize_identifier
normalize_survey_identifier = normalize_identifier


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
