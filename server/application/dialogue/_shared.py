"""Shared session-auth, context, proposal-binding, and commit helpers for dialogue services."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from server.application.sessions import (
    CoCreationAppSettings,
    RoomSyncWriter,
    append_audit_event_tx,
    derive_entity_transitions,
    emit_room_cocreation_event_tx,
    is_deadline_expired,
    token_matches,
)
from server.core.clock import Clock
from server.core.grid import map_fingerprint
from server.domain.errors import AuthError, ConflictError, NotFoundError
from server.domain.memory import (
    add_open_question,
    apply_design_context_patch,
    ensure_design_context,
    merge_intent_hypothesis,
    validate_design_context_patch,
)
from server.domain.sessions import describe_diff, summarize_stage_changes
from server.i18n.messages import format_verified_diff_summary
from server.persistence.database import dump_json
from server.persistence.models import (
    ConversationTurnRecord,
    DesignSessionRecord,
    LevelVersionRecord,
    PlayAttemptRecord,
)
from server.persistence.repositories._mappers import row_to_session, row_to_version
from server.persistence.repositories.sessions_repo import SessionsRepository


def require_browser_session(
    sessions_repo: SessionsRepository,
    session_id: str,
    access_token: str | None,
    *,
    settings: CoCreationAppSettings,
) -> DesignSessionRecord:
    """Validate browser token against Decision 5B hashed token without opening write_tx."""
    session = sessions_repo.get_session(session_id)
    if session is None:
        raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
    if not token_matches(
        access_token,
        session.access_token_hash,
        secret_bytes=settings.token_secret_bytes,
    ):
        raise AuthError("SESSION_ACCESS_DENIED", "error.session_access_denied")
    return session


def require_active_session(
    sessions_repo: SessionsRepository,
    session_id: str,
    access_token: str | None,
    *,
    settings: CoCreationAppSettings,
    clock: Clock,
) -> DesignSessionRecord:
    """Ensure session exists, browser token matches, language is locked, and deadline is active."""
    session = require_browser_session(sessions_repo, session_id, access_token, settings=settings)
    if session.status != "active":
        raise ConflictError("SESSION_LOCKED", "error.session_locked")
    if not session.language_locked and session.language_locked_at is None:
        raise ConflictError("LANGUAGE_NOT_SELECTED", "error.language_not_selected")
    if is_deadline_expired(session.deadline_at, now=clock.utc_datetime()):
        raise ConflictError("SESSION_DEADLINE_EXPIRED", "error.session_deadline_expired")
    return session


def require_current_version_tx(
    conn: sqlite3.Connection,
    session_id: str,
    base_version_id: str,
) -> tuple[DesignSessionRecord, LevelVersionRecord]:
    """Inside write_tx, verify active session and CAS match against base_version_id."""
    s_row = conn.execute(
        "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if s_row is None:
        raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
    cur_session = row_to_session(s_row)
    if cur_session.status != "active":
        raise ConflictError("SESSION_LOCKED", "error.session_locked")
    if cur_session.current_version_id != base_version_id:
        raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
    v_row = conn.execute(
        "SELECT * FROM level_versions WHERE session_id = ? AND version_id = ?",
        (session_id, base_version_id),
    ).fetchone()
    if v_row is None:
        raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
    return cur_session, row_to_version(v_row)


def build_play_summary(
    attempts: Sequence[PlayAttemptRecord],
    version_id: str,
) -> dict[str, Any] | None:
    """Summarize playtesting history for the current Stage."""
    matching = [a for a in attempts if a.version_id == version_id]
    if not matching:
        return None
    latest = matching[-1]
    return {
        "attemptCount": len(matching),
        "latestStatus": latest.status,
        "solvedByDesigner": any(a.status == "completed" for a in matching),
        "latestDurationSeconds": latest.duration_seconds,
        "latestMoves": latest.move_count,
        "latestPushes": latest.push_count,
        "minimumMoves": latest.minimum_moves,
        "minimumPushes": latest.minimum_pushes,
    }


def serialize_turns_for_llm(turns: Sequence[ConversationTurnRecord]) -> list[dict[str, Any]]:
    """Serialize conversation history for ChatPipeline without leaking internal keys."""
    return [
        {
            "turnId": t.turn_id,
            "role": t.role,
            "content": t.content,
            "language": t.language,
            "versionId": t.version_id,
            "guidance": dict(t.guidance or {}),
            "createdAt": t.created_at,
        }
        for t in turns
    ]


def build_proposal_offer_and_binding(
    *,
    base_version: LevelVersionRecord,
    candidate_rows: Sequence[str],
    summary: str,
    language: str,
    rationale: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Construct Policy-2 proposalOffer and conservative execution binding."""
    clean_rows = list(candidate_rows)
    diff = describe_diff(base_version.rows, clean_rows)
    transitions = derive_entity_transitions(base_version.rows, clean_rows)
    change_summary = summarize_stage_changes(base_version.rows, clean_rows).to_wire_dict()
    verified_diff = format_verified_diff_summary(diff, language)
    offer = {
        "summary": summary.strip(),
        "rationale": rationale.strip() or summary.strip(),
        "verifiedDiff": verified_diff,
        "changeSummary": change_summary,
    }
    binding = {
        "baseVersionId": base_version.version_id,
        "mapFingerprint": map_fingerprint(base_version.rows),
        "entityBindingFingerprint": (base_version.entity_bindings or {}).get("bindingFingerprint"),
        "status": "ready",
        "executionBrief": {
            "summary": summary.strip(),
            "requiredTransitions": transitions,
            "changeSummary": change_summary,
            "verifiedDiff": verified_diff,
        },
    }
    return offer, binding


def _normalize_patch_for_memory(patch: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(patch)
    if isinstance(patch.get("goals"), list):
        normalized["goals"] = [
            {**item, "goal": item.get("goal") or item.get("text")}
            if isinstance(item, Mapping)
            else item
            for item in patch["goals"]
        ]
    if isinstance(patch.get("constraints"), list):
        normalized["constraints"] = [
            {**item, "constraint": item.get("constraint") or item.get("text")}
            if isinstance(item, Mapping)
            else item
            for item in patch["constraints"]
        ]
    return normalized


def apply_turn_context_patch(
    design_context: Mapping[str, Any] | None,
    *,
    patch: Mapping[str, Any] | None,
    version_id: str,
    user_turn_id: str,
    assistant_turn_id: str,
    user_text: str,
    guidance: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Apply validated LLM DesignContext patch, open question, and intent hypothesis."""
    state = ensure_design_context(design_context)
    intent_audit: dict[str, Any] | None = None
    if isinstance(patch, Mapping):
        raw_patch = dict(patch)
        extra_questions = list(raw_patch.pop("newOpenQuestions", ()) or ())
        extra_hyp = str(raw_patch.pop("intentHypothesis", "") or "").strip()
        validated = validate_design_context_patch(_normalize_patch_for_memory(raw_patch))
        state = apply_design_context_patch(
            state,
            validated,
            user_text=user_text,
            stage_id=version_id,
            turn_id=user_turn_id,
        )
        for q_text in extra_questions:
            clean_q = str(q_text or "").strip()
            if clean_q:
                state = add_open_question(
                    state,
                    clean_q,
                    stage_id=version_id,
                    turn_id=assistant_turn_id,
                )
    else:
        extra_hyp = ""
    follow_up = str(guidance.get("followUpQuestion") or "").strip()
    if follow_up:
        state = add_open_question(
            state,
            follow_up,
            stage_id=version_id,
            turn_id=assistant_turn_id,
            source_kind="visible_output",
        )
    hyp_stmt = (extra_hyp or str(guidance.get("intentHypothesis") or "")).strip()
    if hyp_stmt:
        state, hyp_id = merge_intent_hypothesis(
            state,
            hyp_stmt,
            stage_id=version_id,
            turn_id=assistant_turn_id,
            displayed=True,
            source_user_turn_id=user_turn_id,
        )
        if hyp_id:
            guidance["_intentHypothesisId"] = hyp_id
            guidance["intentHypothesis"] = hyp_stmt
            guidance["_intentReview"] = {"interactionMode": "full"}
            intent_audit = {"hypothesisId": hyp_id, "statement": hyp_stmt}
    return state.to_wire_dict(), intent_audit


def next_turn_seq_tx(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq "
        "FROM conversation_turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["next_seq"]) if row is not None else 1


def insert_turn_tx(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    session_id: str,
    request_id: str,
    role: str,
    content: str,
    language: str,
    version_id: str,
    created_at: str,
    guidance: Mapping[str, Any] | None = None,
    proposal_binding: Mapping[str, Any] | None = None,
    model: str = "",
    attempts_used: int = 0,
    latency_ms: int = 0,
) -> int:
    """Insert a user or assistant conversation turn monotonically inside write_tx."""
    seq = next_turn_seq_tx(conn, session_id)
    conn.execute(
        "INSERT INTO conversation_turns("
        "turn_id, session_id, sequence_number, request_id, role, content, language, "
        "guidance_json, proposal_binding_json, model, attempts_used, latency_ms, "
        "version_id, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            turn_id,
            session_id,
            seq,
            request_id,
            role,
            content,
            language,
            dump_json(dict(guidance or {})),
            dump_json(dict(proposal_binding)) if proposal_binding is not None else None,
            model,
            attempts_used,
            latency_ms,
            version_id,
            created_at,
        ),
    )
    return seq


def emit_turn_room_event_tx(
    conn: sqlite3.Connection,
    room_sync: RoomSyncWriter | None,
    *,
    session: DesignSessionRecord,
    version: LevelVersionRecord,
    user_turn_id: str,
    assistant_turn_id: str,
    user_text: str,
    assistant_text: str,
    cards: Sequence[Mapping[str, Any]] = (),
    now_iso: str,
) -> None:
    """Emit unified `turn` event into online rooms inside the same write_tx."""
    del user_turn_id
    clean_user = str(user_text or "").strip()
    clean_assistant = str(assistant_text or "").strip()
    if not clean_user or not clean_assistant:
        return
    event = {
        "eventId": f"turn:{assistant_turn_id}",
        "eventType": "turn",
        "playerNumber": session.player_slot or 1,
        "sessionId": session.session_id,
        "versionId": version.version_id,
        "userText": clean_user[:2000],
        "assistantText": clean_assistant[:12000],
        "language": session.language,
        "cards": [dict(c) for c in cards],
    }
    emit_room_cocreation_event_tx(
        conn,
        room_sync,
        match_id=session.match_id,
        player_slot=session.player_slot,
        is_demo=session.is_demo,
        session_id=session.session_id,
        event=event,
        now_iso=now_iso,
    )


def record_message_failure_audit_tx(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    base_version_id: str,
    message_key: str,
    request_id: str,
    code: str,
    details: Mapping[str, Any] | None,
    now_iso: str,
) -> None:
    """Persist structured message generation failure event for read-model display."""
    append_audit_event_tx(
        conn,
        session_id,
        "message_generation_failed",
        {
            "messageKey": message_key,
            "baseVersionId": base_version_id,
            "code": code,
            "requestId": request_id or uuid.uuid4().hex,
            "retryable": True,
            "details": dict(details or {}),
        },
        now_iso,
    )
