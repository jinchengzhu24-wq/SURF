"""Shared SQLite row-to-model mappers for persistence repositories."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..database import load_json
from ..models import (
    AuditEventRecord,
    BinaryDiscoveryTopicRecord,
    ChangeProposalRecord,
    ComponentRecoveryTaskRecord,
    ConversationTurnRecord,
    DesignSessionRecord,
    DraftRegenerationRecord,
    IdempotencyRecord,
    LevelVersionRecord,
    OnlineRoomEventRecord,
    OnlineRoomRecord,
    PlayAttemptRecord,
    RevisionChallengeRecord,
    StudyRecord,
)


def row_to_room(row: sqlite3.Row) -> OnlineRoomRecord:
    return OnlineRoomRecord(
        id=row["id"],
        room_code=row["room_code"],
        status=row["status"],
        config=load_json(row["config_json"], {}),
        players=load_json(row["players_json"], []),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        updated_at=row["updated_at"],
    )


def row_to_room_event(row: sqlite3.Row) -> OnlineRoomEventRecord:
    return OnlineRoomEventRecord(
        id=row["id"],
        match_id=row["match_id"],
        player_slot=row["player_slot"],
        seq=row["seq"],
        event_type=row["event_type"],
        payload=load_json(row["payload_json"], {}),
        created_at=row["created_at"],
    )


def row_to_study_record(row: sqlite3.Row) -> StudyRecord:
    return StudyRecord(
        id=row["id"],
        record_type=row["record_type"],
        match_id=row["match_id"],
        participant_id=row["participant_id"],
        round_id=row["round_id"],
        payload=load_json(row["payload_json"], {}),
        created_at=row["created_at"],
    )


def row_to_session(row: sqlite3.Row) -> DesignSessionRecord:
    return DesignSessionRecord(
        session_id=row["session_id"],
        match_id=row["match_id"],
        player_slot=row["player_slot"],
        mode=row["mode"],
        language=row["language"],
        language_locked=bool(row["language_locked"]),
        language_locked_at=row["language_locked_at"],
        status=row["status"],
        secret_hash=row["secret_hash"],
        access_token_hash=row["access_token_hash"],
        integration_token_hash=row["integration_token_hash"],
        bootstrap_token_hash=row["bootstrap_token_hash"],
        bootstrap_used_at=row["bootstrap_used_at"],
        creation_key=row["creation_key"],
        current_version_id=row["current_version_id"],
        deadline_started_at=row["deadline_started_at"],
        deadline_at=row["deadline_at"],
        finalized_version_id=row["finalized_version_id"],
        finalized_at=row["finalized_at"],
        completed_at=row["completed_at"],
        final_intention=load_json(row["final_intention_json"]),
        draft_rows=load_json(row["draft_rows_json"]),
        draft_validation=load_json(row["draft_validation_json"]),
        draft_generation=int(row["draft_generation"] or 1),
        is_demo=bool(row["is_demo"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


def row_to_version(row: sqlite3.Row) -> LevelVersionRecord:
    return LevelVersionRecord(
        version_id=row["version_id"],
        session_id=row["session_id"],
        version_number=row["version_number"],
        source=row["source"],
        summary=row["summary"],
        rows=load_json(row["rows_json"], []),
        diff=load_json(row["diff_json"], []),
        design_context=load_json(row["design_context_json"]),
        solver=load_json(row["solver_json"], {}),
        entity_bindings=load_json(row["entity_bindings_json"]),
        assessment=load_json(row["assessment_json"]),
        parent_version_id=row["parent_version_id"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
    )


def row_to_turn(row: sqlite3.Row) -> ConversationTurnRecord:
    return ConversationTurnRecord(
        turn_id=row["turn_id"],
        session_id=row["session_id"],
        sequence_number=row["sequence_number"],
        request_id=row["request_id"],
        role=row["role"],
        content=row["content"],
        language=row["language"],
        guidance=load_json(row["guidance_json"]),
        translations=load_json(row["translations_json"]),
        proposal_binding=load_json(row["proposal_binding_json"]),
        model=row["model"],
        attempts_used=row["attempts_used"],
        latency_ms=row["latency_ms"],
        version_id=row["version_id"],
        created_at=row["created_at"],
    )


def row_to_proposal(row: sqlite3.Row) -> ChangeProposalRecord:
    return ChangeProposalRecord(
        proposal_id=row["proposal_id"],
        session_id=row["session_id"],
        assistant_turn_id=row["assistant_turn_id"],
        base_version_id=row["base_version_id"],
        candidate_version_id=row["candidate_version_id"],
        status=row["status"],
        proposal=load_json(row["proposal_json"], {}),
        decision=load_json(row["decision_json"]),
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


def row_to_challenge(row: sqlite3.Row) -> RevisionChallengeRecord:
    return RevisionChallengeRecord(
        challenge_id=row["challenge_id"],
        session_id=row["session_id"],
        base_version_id=row["base_version_id"],
        source_proposal_turn_id=row["source_proposal_turn_id"],
        challenge_turn_id=row["challenge_turn_id"],
        status=row["status"],
        current_reason_turn_id=row["current_reason_turn_id"],
        reviews=load_json(row["reviews_json"], []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_play_attempt(row: sqlite3.Row) -> PlayAttemptRecord:
    return PlayAttemptRecord(
        attempt_id=row["attempt_id"],
        session_id=row["session_id"],
        version_id=row["version_id"],
        mode=row["mode"],
        language=row["language"],
        status=row["status"],
        ticket_hash=row["ticket_hash"],
        ticket_expires_at=row["ticket_expires_at"],
        ticket_used_at=row["ticket_used_at"],
        attempt_token_hash=row["attempt_token_hash"],
        issued_at=row["issued_at"],
        loaded_at=row["loaded_at"],
        first_move_at=row["first_move_at"],
        finished_at=row["finished_at"],
        duration_seconds=float(row["duration_seconds"] or 0.0),
        move_count=int(row["move_count"] or 0),
        push_count=int(row["push_count"] or 0),
        restart_count=int(row["restart_count"] or 0),
        minimum_moves=int(row["minimum_moves"] if row["minimum_moves"] is not None else -1),
        minimum_pushes=int(row["minimum_pushes"] if row["minimum_pushes"] is not None else -1),
        idempotency_key=row["idempotency_key"],
    )


def row_to_draft_regeneration(row: sqlite3.Row) -> DraftRegenerationRecord:
    return DraftRegenerationRecord(
        request_id=row["request_id"],
        session_id=row["session_id"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        generation=int(row["generation"] or 1),
        failure_code=row["failure_code"],
        rows=load_json(row["rows_json"]),
        validation=load_json(row["validation_json"]),
        requested_at=row["requested_at"],
        updated_at=row["updated_at"],
    )


def row_to_audit_event(row: sqlite3.Row) -> AuditEventRecord:
    return AuditEventRecord(
        id=row["id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        payload=load_json(row["payload_json"], {}),
        created_at=row["created_at"],
    )


def row_to_idempotency(row: sqlite3.Row) -> IdempotencyRecord:
    return IdempotencyRecord(
        session_id=row["session_id"],
        scope=row["scope"],
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        status=row["status"],
        response=load_json(row["response_json"]),
        created_at=row["created_at"],
    )


def row_to_topic(row: sqlite3.Row) -> BinaryDiscoveryTopicRecord:
    return BinaryDiscoveryTopicRecord(
        session_id=row["session_id"],
        topic_id=row["topic_id"],
        state=load_json(row["state_json"], {}),
        updated_at=row["updated_at"],
    )


def row_to_recovery_task(row: sqlite3.Row) -> ComponentRecoveryTaskRecord:
    return ComponentRecoveryTaskRecord(
        recovery_id=row["recovery_id"],
        session_id=row["session_id"],
        target_kind=row["target_kind"],
        target_id=row["target_id"],
        state=load_json(row["state_json"], {}),
        updated_at=row["updated_at"],
    )


def build_update_clause(
    fields: dict[str, Any],
    allowed: dict[str, str],
) -> tuple[list[str], list[Any]]:
    """Build parameterized SET clauses from an allowlisted field->column mapping."""
    clauses: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"Unsupported update field: {key}")
        clauses.append(f"{allowed[key]} = ?")
        params.append(value)
    return clauses, params
