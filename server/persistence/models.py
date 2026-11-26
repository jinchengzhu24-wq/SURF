"""Typed Pydantic read models and records for the unified persistence layer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _FrozenRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class OnlineRoomRecord(_FrozenRecord):
    id: str
    room_code: str
    status: str
    config: dict[str, Any] = Field(default_factory=dict)
    players: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    expires_at: str
    updated_at: str


class OnlineRoomEventRecord(_FrozenRecord):
    id: int
    match_id: str
    player_slot: int
    seq: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class StudyRecord(_FrozenRecord):
    id: int
    record_type: str
    match_id: str | None = None
    participant_id: str | None = None
    round_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class DesignSessionRecord(_FrozenRecord):
    session_id: str
    match_id: str | None = None
    player_slot: int | None = None
    mode: str
    language: str
    language_locked: bool = False
    language_locked_at: str | None = None
    status: str
    secret_hash: str | None = None
    access_token_hash: str
    integration_token_hash: str
    bootstrap_token_hash: str
    bootstrap_used_at: str | None = None
    creation_key: str | None = None
    current_version_id: str | None = None
    deadline_started_at: str | None = None
    deadline_at: str | None = None
    finalized_version_id: str | None = None
    finalized_at: str | None = None
    completed_at: str | None = None
    final_intention: dict[str, Any] | None = None
    draft_rows: list[str] | None = None
    draft_validation: dict[str, Any] | None = None
    draft_generation: int = 1
    is_demo: bool = False
    created_at: str
    updated_at: str
    expires_at: str | None = None


class LevelVersionRecord(_FrozenRecord):
    version_id: str
    session_id: str
    version_number: int
    source: str
    summary: str
    rows: list[str]
    diff: list[dict[str, Any]] = Field(default_factory=list)
    design_context: dict[str, Any] | None = None
    solver: dict[str, Any] = Field(default_factory=dict)
    entity_bindings: dict[str, Any] | None = None
    assessment: dict[str, Any] | None = None
    parent_version_id: str | None = None
    idempotency_key: str | None = None
    created_at: str


class ConversationTurnRecord(_FrozenRecord):
    turn_id: str
    session_id: str
    sequence_number: int
    request_id: str | None = None
    role: str
    content: str
    language: str = "zh-CN"
    guidance: dict[str, Any] | None = None
    translations: dict[str, Any] | None = None
    proposal_binding: dict[str, Any] | None = None
    model: str | None = None
    attempts_used: int | None = None
    latency_ms: int | None = None
    version_id: str
    created_at: str


class ChangeProposalRecord(_FrozenRecord):
    proposal_id: str
    session_id: str
    assistant_turn_id: str
    base_version_id: str
    candidate_version_id: str | None = None
    status: str
    proposal: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any] | None = None
    idempotency_key: str | None = None
    created_at: str
    decided_at: str | None = None


class RevisionChallengeRecord(_FrozenRecord):
    challenge_id: str
    session_id: str
    base_version_id: str
    source_proposal_turn_id: str
    challenge_turn_id: str
    status: str
    current_reason_turn_id: str | None = None
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    updated_at: str


class PlayAttemptRecord(_FrozenRecord):
    attempt_id: str
    session_id: str
    version_id: str
    mode: str
    language: str
    status: str
    ticket_hash: str
    ticket_expires_at: str
    ticket_used_at: str | None = None
    attempt_token_hash: str | None = None
    issued_at: str
    loaded_at: str | None = None
    first_move_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float = 0.0
    move_count: int = 0
    push_count: int = 0
    restart_count: int = 0
    minimum_moves: int = -1
    minimum_pushes: int = -1
    idempotency_key: str


class DraftRegenerationRecord(_FrozenRecord):
    request_id: str
    session_id: str
    idempotency_key: str
    status: str
    generation: int = 1
    failure_code: str | None = None
    rows: list[str] | None = None
    validation: dict[str, Any] | None = None
    requested_at: str
    updated_at: str


class AuditEventRecord(_FrozenRecord):
    id: int
    session_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class IdempotencyRecord(_FrozenRecord):
    session_id: str
    scope: str
    idempotency_key: str
    request_hash: str
    status: str
    response: dict[str, Any] | None = None
    created_at: str


class IdempotencyClaimResult(_FrozenRecord):
    outcome: Literal["acquired", "completed", "in_progress", "conflict"]
    record: IdempotencyRecord


class BinaryDiscoveryTopicRecord(_FrozenRecord):
    session_id: str
    topic_id: str
    state: dict[str, Any] = Field(default_factory=dict)
    updated_at: str


class ComponentRecoveryTaskRecord(_FrozenRecord):
    recovery_id: str
    session_id: str
    target_kind: str
    target_id: str
    state: dict[str, Any] = Field(default_factory=dict)
    updated_at: str


class SessionReadModel(_FrozenRecord):
    """Batch-loaded session aggregate with zero N+1 queries."""

    session: DesignSessionRecord
    versions: list[LevelVersionRecord] = Field(default_factory=list)
    turns: list[ConversationTurnRecord] = Field(default_factory=list)
    proposals: list[ChangeProposalRecord] = Field(default_factory=list)
    challenges: list[RevisionChallengeRecord] = Field(default_factory=list)
    play_attempts: list[PlayAttemptRecord] = Field(default_factory=list)
    draft_regenerations: list[DraftRegenerationRecord] = Field(default_factory=list)
