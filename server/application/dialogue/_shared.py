"""Shared session-auth, context, proposal-binding, error, and commit helpers for dialogue."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from server.application.sessions import (
    CoCreationAppSettings,
    append_audit_event_tx,
    derive_entity_transitions,
    is_deadline_expired,
    token_matches,
)
from server.application.sessions.config import build_turn_cards as build_turn_cards
from server.core.clock import SYSTEM_CLOCK, Clock, parse_iso, utc_now_iso
from server.core.grid import map_fingerprint, parse_grid
from server.core.llm_client import LLMError
from server.domain.errors import (
    AuthError,
    ConflictError,
    DomainError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from server.domain.memory import (
    add_open_question,
    apply_design_context_patch,
    ensure_design_context,
    merge_intent_hypothesis,
    validate_design_context_patch,
)
from server.domain.sessions import describe_diff, summarize_stage_changes
from server.i18n.messages import format_verified_diff_summary, get_message
from server.persistence.database import dump_json
from server.persistence.models import (
    ConversationTurnRecord,
    DesignSessionRecord,
    LevelVersionRecord,
    PlayAttemptRecord,
)
from server.persistence.repositories._mappers import row_to_session, row_to_version
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

ACTIVE_CHALLENGE_STATUSES: frozenset[str] = frozenset(
    {
        "awaiting_reason",
        "awaiting_choice",
        "review_pending",
        "reason_review",
        "choice_pending",
    }
)


def stale_before_iso(clock: Clock, seconds: float = 150.0) -> str:
    """Return an ISO timestamp `seconds` before the current clock time."""
    return utc_now_iso(clock.utc_datetime() - timedelta(seconds=seconds))


def classify_failure(exc: Exception) -> tuple[str, bool, dict[str, Any]]:
    """Classify core LLMError, DomainError, TimeoutError, or internal errors for audit logs."""
    if isinstance(exc, LLMError):
        details: dict[str, Any] = {
            "attemptsUsed": exc.attempts_used,
            "requestId": exc.request_id,
        }
        if exc.provider_status is not None:
            details["providerStatus"] = exc.provider_status
        return exc.code, exc.retryable, details
    if isinstance(exc, DomainError):
        return exc.code, exc.retryable, dict(exc.details)
    if isinstance(exc, TimeoutError):
        return "DEADLINE_EXCEEDED", True, {}
    return "INTERNAL_ERROR", True, {}


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


def check_completed_idempotency(
    operational_repo: OperationalRepository,
    session_id: str,
    scope: str,
    idempotency_key: str,
    request_hash: str,
) -> bool:
    """Return True if an identical completed key exists; raise on digest mismatch."""
    existing = operational_repo.get_key(session_id, scope, idempotency_key)
    if existing is None or existing.status != "completed":
        return False
    if existing.request_hash != request_hash:
        raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
    return True


def require_current_version_tx(
    conn: sqlite3.Connection,
    session_id: str,
    base_version_id: str,
    *,
    clock: Clock | None = None,
) -> tuple[DesignSessionRecord, LevelVersionRecord]:
    """Inside write_tx, verify active session, deadline, and CAS match against base_version_id."""
    active_clock = clock if clock is not None else SYSTEM_CLOCK
    s_row = conn.execute(
        "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if s_row is None:
        raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
    cur_session = row_to_session(s_row)
    if cur_session.status != "active":
        raise ConflictError("SESSION_LOCKED", "error.session_locked")
    if is_deadline_expired(cur_session.deadline_at, now=active_clock.utc_datetime()):
        raise ConflictError("SESSION_DEADLINE_EXPIRED", "error.session_locked")
    if cur_session.current_version_id != base_version_id:
        raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
    v_row = conn.execute(
        "SELECT * FROM level_versions WHERE session_id = ? AND version_id = ?",
        (session_id, base_version_id),
    ).fetchone()
    if v_row is None:
        raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
    return cur_session, row_to_version(v_row)


def apply_transitions_to_rows(
    base_rows: Sequence[str],
    transitions: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Deterministically apply bound cell transitions onto a playable Stage grid."""
    grid = parse_grid(base_rows, validate_playable=True)
    cells = [list(row) for row in grid.rows]
    for t in transitions:
        r, c = t.get("row"), t.get("column")
        if (
            isinstance(r, bool)
            or isinstance(c, bool)
            or not isinstance(r, int)
            or not isinstance(c, int)
            or not (1 <= r <= len(cells))
            or not (1 <= c <= len(cells[r - 1]))
        ):
            raise ValidationError("INVALID_REVISION_BINDING", "error.version_conflict")
        if cells[r - 1][c - 1] != t.get("from"):
            raise ConflictError("STALE_REVISION_BINDING", "error.version_conflict")
        cells[r - 1][c - 1] = str(t["to"])
    return ["".join(row) for row in cells]


def build_play_summary(
    attempts: Sequence[PlayAttemptRecord],
    version_id: str,
) -> dict[str, Any] | None:
    """Summarize playtesting history for the current Stage."""
    matching = [a for a in attempts if a.version_id == version_id]
    if not matching:
        return None

    def _ts_key(value: str | None) -> tuple[int, float, str]:
        if not value:
            return (0, 0.0, "")
        try:
            return (1, parse_iso(value).timestamp(), value)
        except ValueError:
            return (1, 0.0, value)

    latest = max(
        matching,
        key=lambda a: (
            _ts_key(a.issued_at),
            _ts_key(a.finished_at),
            _ts_key(a.first_move_at),
            _ts_key(a.loaded_at),
        ),
    )
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
    available_actions: Sequence[str] | None = None,
    solve_result: Mapping[str, Any] | None = None,
    request_text: str | Sequence[str] | None = None,
    answer_texts: Sequence[str] | None = None,
    verified_actions: Sequence[str] | None = None,
    binary_compact: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Construct Policy-2 proposalOffer (with optional v2 presentation) and execution binding."""
    clean_rows = list(candidate_rows)
    diff = describe_diff(base_version.rows, clean_rows)
    transitions = derive_entity_transitions(base_version.rows, clean_rows)
    change_summary = summarize_stage_changes(base_version.rows, clean_rows).to_wire_dict()
    verified_diff = format_verified_diff_summary(diff, language)
    clean_summary = summary.strip()
    clean_rationale = rationale.strip() or clean_summary
    offer: dict[str, Any] = {
        "summary": clean_summary,
        "rationale": clean_rationale,
        "verifiedDiff": verified_diff,
        "changeSummary": change_summary,
    }
    if available_actions is not None:
        offer["availableActions"] = list(available_actions)
    if solve_result is not None or request_text is not None or binary_compact:
        changes_v2 = [
            {
                "row": item["row"],
                "column": item["column"],
                "before": item["from"],
                "after": item["to"],
            }
            for item in transitions
        ]
        presentation: dict[str, Any] = {
            "schemaVersion": 2,
            "summary": clean_summary,
            "rationale": clean_rationale,
            "changes": changes_v2,
            "reason": clean_rationale,
        }
        if binary_compact:
            presentation["binaryCompact"] = True
            presentation["verifiedActions"] = list(verified_actions or ())
        if request_text:
            presentation["requestText"] = (
                list(request_text) if isinstance(request_text, (list, tuple)) else str(request_text)
            )
        if answer_texts:
            presentation["answerTexts"] = [str(a) for a in answer_texts if str(a).strip()]
        if isinstance(solve_result, Mapping):
            steps = solve_result.get("solutionSteps")
            pushes = solve_result.get("solutionPushes")
            if isinstance(steps, int) and isinstance(pushes, int):
                presentation["solution"] = {
                    "minimumMoves": steps,
                    "pushesOnMinimumMoveSolution": pushes,
                }
        offer["proposalPresentation"] = presentation

    binding = {
        "baseVersionId": base_version.version_id,
        "mapFingerprint": map_fingerprint(base_version.rows),
        "entityBindingFingerprint": (base_version.entity_bindings or {}).get("bindingFingerprint"),
        "status": "active",
        "executionBrief": {
            "summary": clean_summary,
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
    evidence_ids: Sequence[str] | None = None,
    semantic_claims: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Apply optional DesignContext patch, open question, and intent hypothesis safely."""
    state = ensure_design_context(design_context)
    intent_audit: dict[str, Any] | None = None
    extra_hyp = ""
    if isinstance(patch, Mapping):
        raw_patch = dict(patch)
        extra_questions = list(raw_patch.pop("newOpenQuestions", ()) or ())
        extra_hyp = str(raw_patch.pop("intentHypothesis", "") or "").strip()
        try:
            validated = validate_design_context_patch(_normalize_patch_for_memory(raw_patch))
            state = apply_design_context_patch(
                state,
                validated,
                user_text=user_text,
                stage_id=version_id,
                turn_id=user_turn_id,
            )
        except ValueError:
            pass
        for q_text in extra_questions:
            clean_q = str(q_text or "").strip()
            if clean_q:
                state = add_open_question(
                    state,
                    clean_q,
                    stage_id=version_id,
                    turn_id=assistant_turn_id,
                )

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
        resolved_evidence_ids = (
            [str(eid) for eid in evidence_ids if str(eid).strip()]
            if evidence_ids is not None
            else [f"ie_{uuid.uuid4().hex}"]
        )
        claims = (
            semantic_claims
            if semantic_claims is not None
            else ((guidance.get("_intentDecision") or {}).get("claims") or ())
        )
        state, hyp_id = merge_intent_hypothesis(
            state,
            hyp_stmt,
            evidence_ids=resolved_evidence_ids,
            stage_id=version_id,
            turn_id=assistant_turn_id,
            displayed=True,
            semantic_claims=claims,
            source_user_turn_id=user_turn_id,
        )
        if hyp_id:
            guidance["_intentHypothesisId"] = hyp_id
            guidance["intentHypothesis"] = hyp_stmt
            guidance["_intentReviewState"] = {"interactionMode": "full"}
            intent_audit = {
                "hypothesisId": hyp_id,
                "statement": hyp_stmt,
                "evidenceId": resolved_evidence_ids[0] if resolved_evidence_ids else None,
            }
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


def deterministic_proposal_failure_text(language: str) -> str:
    """Return bilingual explanation when proposal search is exhausted."""
    reason = get_message("requirements.conflict.default", language)
    return get_message("requirements.failure_wrapper", language, reason=reason)


async def generate_safe_failure_analysis(
    proposal_pipeline: Any,
    *,
    rows: Sequence[str],
    designer_turns: Sequence[Mapping[str, Any]],
    failure_code: str,
    failure_summary: str,
    requirements: Mapping[str, Any] | None = None,
    entity_bindings: Mapping[str, Any] | None = None,
    language: str,
    deadline: float | None = None,
    request_id: str = "",
) -> str:
    """Invoke proposal_pipeline.generate_failure_analysis with safe deterministic fallback."""
    if proposal_pipeline is not None:
        try:
            text = await proposal_pipeline.generate_failure_analysis(
                grid_or_rows=rows,
                designer_turns=designer_turns,
                failure_code=failure_code,
                failure_summary=failure_summary,
                requirements=requirements,
                entity_bindings=entity_bindings,
                language=language,
                deadline=deadline,
                request_id=request_id,
            )
            if str(text or "").strip():
                return str(text).strip()
        except (LLMError, TimeoutError, ValueError):
            pass
    return deterministic_proposal_failure_text(language)


def record_message_failure_audit_tx(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    base_version_id: str,
    message_key: str,
    request_id: str,
    code: str,
    retryable: bool = True,
    details: Mapping[str, Any] | None = None,
    now_iso: str,
    emit_reply_failure: bool = False,
) -> None:
    """Persist structured message (and optional reply) failure events for read-model/audits."""
    req_id = request_id or uuid.uuid4().hex
    payload = {
        "messageKey": message_key,
        "baseVersionId": base_version_id,
        "code": code,
        "requestId": req_id,
        "retryable": bool(retryable),
        "details": dict(details or {}),
    }
    append_audit_event_tx(conn, session_id, "message_generation_failed", payload, now_iso)
    if emit_reply_failure:
        append_audit_event_tx(
            conn,
            session_id,
            "reply_generation_failed",
            {
                **payload,
                "task": "message",
                "reliableBodyDelivered": False,
            },
            now_iso,
        )
