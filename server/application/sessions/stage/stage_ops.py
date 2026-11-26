"""Extracted Stage restore and assessment helpers for StageService."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from server.core.clock import Clock
from server.domain.errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
)
from server.domain.memory import ensure_design_context, set_active_disagreement
from server.domain.proposals import BINARY_ACTIVE_STATUSES
from server.domain.sessions import describe_diff, summarize_stage_changes
from server.llm.pipelines.chat import ChatPipeline
from server.llm.pipelines.understanding import UnderstandingPipeline
from server.persistence.database import SQLiteDatabase, dump_json, load_json
from server.persistence.models import DesignSessionRecord
from server.persistence.repositories._mappers import row_to_session, row_to_version
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ..config import (
    RoomSyncWriter,
    append_audit_event_tx,
    build_stage_sync_event,
    emit_room_cocreation_event_tx,
    next_version_number_tx,
    solve_stage_async,
)
from ..lifecycle.tokens import is_deadline_expired, request_digest, utc_plus_seconds_iso
from ._manual_review import (
    build_disagreement_payload,
    commit_manual_edit_review_tx,
    run_manual_edit_review,
)
from .entity_bindings import build_entity_bindings


def supersede_active_stage_artifacts_tx(
    conn: Any,
    session_id: str,
    now_iso: str,
) -> None:
    """Supersede pending proposals, resolve unfinished challenges, and cancel active topics."""
    conn.execute(
        "UPDATE change_proposals SET status = 'superseded', decided_at = ? "
        "WHERE session_id = ? AND status = 'pending'",
        (now_iso, session_id),
    )
    conn.execute(
        "UPDATE revision_challenges SET status = 'resolved', updated_at = ? "
        "WHERE session_id = ? "
        "AND status IN ('awaiting_reason', 'awaiting_choice', 'review_pending', "
        "'reason_review', 'choice_pending')",
        (now_iso, session_id),
    )
    topic_rows = conn.execute(
        "SELECT topic_id, state_json FROM binary_discovery_topics WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    for trow in topic_rows:
        state = load_json(trow["state_json"], None)
        if not isinstance(state, dict):
            continue
        if state.get("status") in BINARY_ACTIVE_STATUSES:
            state["status"] = "cancelled"
            state["retryAction"] = None
            state["updatedAt"] = now_iso
            conn.execute(
                "UPDATE binary_discovery_topics SET state_json = ?, updated_at = ? "
                "WHERE session_id = ? AND topic_id = ?",
                (dump_json(state), now_iso, session_id, trow["topic_id"]),
            )


def _require_active_for_write(session: DesignSessionRecord, *, clock: Clock) -> None:
    if session.status != "active":
        raise ConflictError("SESSION_LOCKED", "error.session_locked")
    if not session.language_locked and session.language_locked_at is None:
        raise ConflictError("LANGUAGE_NOT_SELECTED", "error.language_not_selected")
    if is_deadline_expired(session.deadline_at, now=clock.utc_datetime()):
        raise ConflictError("SESSION_DEADLINE_EXPIRED", "error.session_deadline_expired")


def _next_turn_seq_tx(conn: Any, session_id: str) -> int:
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq "
        "FROM conversation_turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(seq_row["next_seq"]) if seq_row is not None else 1


def _record_reply_outcome_tx(
    conn: Any,
    *,
    session_id: str,
    version_id: str,
    idempotency_key: str,
    task: str,
    model_succeeded: bool,
    body_delivered: bool,
    full_quality: bool,
    snapshot_fallback: bool,
    attempts_used: int,
    latency_ms: int,
    request_id: str,
    now_iso: str,
) -> None:
    append_audit_event_tx(
        conn,
        session_id,
        "reply_delivery_outcome",
        {
            "messageKey": idempotency_key,
            "baseVersionId": version_id,
            "task": task,
            "modelGenerationSucceeded": model_succeeded,
            "reliableBodyDelivered": body_delivered,
            "fullQualityPassed": full_quality,
            "snapshotFallback": snapshot_fallback,
            "deterministicReceipt": False,
            "attemptsUsed": attempts_used,
            "latencyMs": latency_ms,
            "requestId": request_id,
        },
        now_iso,
    )


def _record_reply_failure_safe(
    db: SQLiteDatabase,
    *,
    session_id: str,
    version_id: str,
    idempotency_key: str,
    task: str,
    request_id: str,
    exc: Exception,
    now_iso: str,
) -> None:
    code = getattr(exc, "code", None) or "INTERNAL_ERROR"
    attempts_used = int(getattr(exc, "attempts_used", 0) or 0)
    with db.write_tx() as conn:
        append_audit_event_tx(
            conn,
            session_id,
            "reply_generation_failed",
            {
                "messageKey": idempotency_key,
                "baseVersionId": version_id,
                "task": task,
                "code": str(code),
                "attemptsUsed": attempts_used,
                "reliableBodyDelivered": False,
                "fullQualityPassed": False,
                "requestId": request_id,
            },
            now_iso,
        )


def _emit_opening_sync_tx(
    conn: Any,
    room_sync: RoomSyncWriter | None,
    *,
    session: DesignSessionRecord,
    version_id: str,
    stage_number: int,
    opening_turn_id: str,
    opening_text: str,
    now_iso: str,
) -> None:
    opening_ev = {
        "eventId": f"opening:{version_id}",
        "eventType": "opening",
        "playerNumber": session.player_slot or 1,
        "sessionId": session.session_id,
        "versionId": version_id,
        "stageNumber": stage_number,
        "assistantTurnId": opening_turn_id,
        "assistantText": opening_text,
        "language": session.language,
        "cards": [],
    }
    emit_room_cocreation_event_tx(
        conn,
        room_sync,
        match_id=session.match_id,
        player_slot=session.player_slot,
        is_demo=session.is_demo,
        session_id=session.session_id,
        event=opening_ev,
        now_iso=now_iso,
    )


async def restore_version_impl(
    *,
    db: SQLiteDatabase,
    sessions_repo: SessionsRepository,
    operational_repo: OperationalRepository,
    room_sync: RoomSyncWriter | None,
    clock: Clock,
    session: DesignSessionRecord,
    version_id: str,
    base_version_id: str,
    idempotency_key: str,
) -> None:
    """Solve restored Stage OUTSIDE write_tx, then create child Stage via optimistic CAS."""
    session_id = session.session_id
    sig = request_digest({"versionId": version_id, "baseVersionId": base_version_id})
    claim = operational_repo.claim_key(
        session_id,
        "restore_version",
        idempotency_key,
        request_hash=sig,
        created_at=clock.utc_now(),
        stale_before_iso=utc_plus_seconds_iso(clock, -150),
    )
    if claim.outcome == "conflict":
        raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
    if claim.outcome == "completed":
        return
    if claim.outcome == "in_progress":
        raise ConflictError("RESTORE_IN_PROGRESS", "error.version_conflict")

    try:
        _require_active_for_write(session, clock=clock)
        if session.current_version_id != base_version_id:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
        source_ver = sessions_repo.get_version(session_id, version_id)
        if source_ver is None:
            raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
        if source_ver.version_id == session.current_version_id:
            raise ConflictError("UNCHANGED_LEVEL", "error.unchanged_level")

        solved = await solve_stage_async(source_ver.rows, clock=clock)
        now_iso = clock.utc_now()

        with db.write_tx() as conn:
            cur_row = conn.execute(
                "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if cur_row is None:
                raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
            cur_session = row_to_session(cur_row)
            if cur_session.status != "active":
                raise ConflictError("SESSION_LOCKED", "error.session_locked")
            if is_deadline_expired(cur_session.deadline_at, now=clock.utc_datetime()):
                raise ConflictError("SESSION_DEADLINE_EXPIRED", "error.session_deadline_expired")
            if cur_session.current_version_id != base_version_id:
                raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
            cur_ver_row = conn.execute(
                "SELECT * FROM level_versions WHERE session_id = ? AND version_id = ?",
                (session_id, base_version_id),
            ).fetchone()
            assert cur_ver_row is not None
            cur_ver = row_to_version(cur_ver_row)

            restored_ctx = set_active_disagreement(
                ensure_design_context(source_ver.design_context), None, source_ver.version_id, None
            )
            diff = describe_diff(cur_ver.rows, solved.rows)
            bindings = build_entity_bindings(
                solved.rows,
                parent_bindings=source_ver.entity_bindings,
                source="restored",
            )
            new_vid = uuid.uuid4().hex
            new_num = next_version_number_tx(conn, session_id)
            conn.execute(
                "INSERT INTO level_versions("
                "version_id, session_id, version_number, source, summary, rows_json, "
                "diff_json, design_context_json, solver_json, entity_bindings_json, "
                "parent_version_id, idempotency_key, created_at"
                ") VALUES (?, ?, ?, 'restored', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_vid,
                    session_id,
                    new_num,
                    f"Continued from Stage {source_ver.version_number}",
                    dump_json(list(solved.rows)),
                    dump_json(diff),
                    dump_json(restored_ctx.to_wire_dict()),
                    dump_json(solved.as_dict()),
                    dump_json(bindings),
                    source_ver.version_id,
                    f"restore:{idempotency_key}",
                    now_iso,
                ),
            )
            cas = conn.execute(
                "UPDATE design_sessions SET current_version_id = ?, updated_at = ? "
                "WHERE session_id = ? AND current_version_id = ?",
                (new_vid, now_iso, session_id, base_version_id),
            )
            if cas.rowcount == 0:
                raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
            supersede_active_stage_artifacts_tx(conn, session_id, now_iso)

            append_audit_event_tx(
                conn,
                session_id,
                "stage_restored",
                {
                    "versionId": new_vid,
                    "restoredFromVersionId": source_ver.version_id,
                    "restoredFromStageNumber": source_ver.version_number,
                    "replacedVersionId": cur_ver.version_id,
                    "replacedStageNumber": cur_ver.version_number,
                    "baseVersionId": base_version_id,
                },
                now_iso,
            )
            stage_ev = build_stage_sync_event(
                session_id=session_id,
                player_slot=cur_session.player_slot or 1,
                version_id=new_vid,
                version_number=new_num,
                source="restored",
                rows=solved.rows,
                diff=diff,
                initial_draft_method=cur_session.mode,
                event_type="stage",
                restored_from_version_id=source_ver.version_id,
                restored_from_stage_number=source_ver.version_number,
                replaced_version_id=cur_ver.version_id,
                replaced_stage_number=cur_ver.version_number,
            )
            emit_room_cocreation_event_tx(
                conn,
                room_sync,
                match_id=cur_session.match_id,
                player_slot=cur_session.player_slot,
                is_demo=cur_session.is_demo,
                session_id=session_id,
                event=stage_ev,
                now_iso=now_iso,
            )
        operational_repo.complete_key(
            session_id, "restore_version", idempotency_key, response={"versionId": new_vid}
        )
    except asyncio.CancelledError:
        operational_repo.complete_key(
            session_id, "restore_version", idempotency_key, status="failed"
        )
        raise
    except Exception:
        operational_repo.complete_key(
            session_id, "restore_version", idempotency_key, status="failed"
        )
        raise


async def assess_version_impl(
    *,
    db: SQLiteDatabase,
    sessions_repo: SessionsRepository,
    chat_pipeline: ChatPipeline | None,
    room_sync: RoomSyncWriter | None,
    clock: Clock,
    session: DesignSessionRecord,
    version_id: str,
    idempotency_key: str,
    operational_repo: OperationalRepository | None = None,
    understanding_pipeline: UnderstandingPipeline | None = None,
    request_id: str = "",
    deadline: float | None = None,
) -> None:
    """Generate Stage opening or manual-edit review OUTSIDE write_tx, then commit atomically."""
    session_id = session.session_id
    version = sessions_repo.get_version(session_id, version_id)
    if version is None:
        raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
    if version.assessment is not None:
        return
    _require_active_for_write(session, clock=clock)
    if session.current_version_id != version_id:
        raise ConflictError("VERSION_CONFLICT", "error.version_conflict")

    if operational_repo is not None:
        claim = operational_repo.claim_key(
            session_id,
            "assess_version",
            idempotency_key,
            request_hash=request_digest({"versionId": version_id}),
            created_at=clock.utc_now(),
            stale_before_iso=utc_plus_seconds_iso(clock, -150),
        )
        if claim.outcome == "conflict":
            raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
        if claim.outcome == "completed":
            return
        if claim.outcome == "in_progress":
            raise ConflictError("ASSESSMENT_IN_PROGRESS", "error.version_conflict")

    req_id = request_id or idempotency_key
    try:
        version = sessions_repo.get_version(session_id, version_id)
        if version is None:
            raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
        if version.assessment is not None:
            if operational_repo is not None:
                operational_repo.complete_key(
                    session_id,
                    "assess_version",
                    idempotency_key,
                    response={"versionId": version_id},
                )
            return

        parent = (
            sessions_repo.get_version(session_id, version.parent_version_id)
            if version.parent_version_id
            else None
        )
        change_summary = (
            summarize_stage_changes(parent.rows, version.rows).to_wire_dict()
            if parent is not None
            else None
        )
        use_manual_review = version.source == "human_edit" and understanding_pipeline is not None
        task = "manual_edit_review" if use_manual_review else "stage_opening"

        review_res = None
        conflict_decision = None
        solver_delta: dict[str, Any] = {}
        opening_res = None
        try:
            if use_manual_review:
                assert understanding_pipeline is not None
                conflict_decision, review_res, solver_delta = await run_manual_edit_review(
                    understanding_pipeline,
                    version=version,
                    parent=parent,
                    change_summary=change_summary,
                    language=session.language,
                    deadline=deadline,
                    request_id=req_id,
                )
            else:
                if chat_pipeline is None:
                    raise ConflictError("LLM_UNAVAILABLE", "error.configuration")
                opening_res = await chat_pipeline.generate_stage_opening(
                    version.rows,
                    stage_number=version.version_number,
                    language=session.language,
                    solver_metrics=version.solver,
                    design_context=version.design_context,
                    change_summary=change_summary,
                    entity_bindings=version.entity_bindings,
                    deadline=deadline,
                    request_id=req_id,
                )
        except Exception as exc:
            _record_reply_failure_safe(
                db,
                session_id=session_id,
                version_id=version_id,
                idempotency_key=idempotency_key,
                task=task,
                request_id=req_id,
                exc=exc,
                now_iso=clock.utc_now(),
            )
            raise

        now_iso = clock.utc_now()
        opening_turn_id = uuid.uuid4().hex
        review_turn_id = uuid.uuid4().hex if review_res is not None else None
        disagreement_payload = (
            build_disagreement_payload(review_res) if review_res is not None else None
        )

        with db.write_tx() as conn:
            cur_row = conn.execute(
                "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            cur_session = row_to_session(cur_row) if cur_row is not None else session
            ver_row = conn.execute(
                "SELECT * FROM level_versions WHERE session_id = ? AND version_id = ?",
                (session_id, version_id),
            ).fetchone()
            if ver_row is None:
                raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
            ver = row_to_version(ver_row)
            if ver.assessment is None:
                _require_active_for_write(cur_session, clock=clock)
                if cur_session.current_version_id != version_id:
                    raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
                seq = _next_turn_seq_tx(conn, session_id)
                if review_res is not None and conflict_decision is not None:
                    assert understanding_pipeline is not None and review_turn_id is not None
                    commit_manual_edit_review_tx(
                        conn,
                        room_sync=room_sync,
                        session=cur_session,
                        ver=ver,
                        model_name=understanding_pipeline.model,
                        opening_turn_id=opening_turn_id,
                        review_turn_id=review_turn_id,
                        seq=seq,
                        idempotency_key=idempotency_key,
                        conflict_decision=conflict_decision,
                        review_res=review_res,
                        change_summary=change_summary,
                        solver_delta=solver_delta,
                        disagreement_payload=disagreement_payload,
                        now_iso=now_iso,
                    )
                    _record_reply_outcome_tx(
                        conn,
                        session_id=session_id,
                        version_id=version_id,
                        idempotency_key=idempotency_key,
                        task="manual_edit_review",
                        model_succeeded=True,
                        body_delivered=bool(review_res.opening_message),
                        full_quality=True,
                        snapshot_fallback=False,
                        attempts_used=review_res.attempts_used,
                        latency_ms=review_res.latency_ms,
                        request_id=req_id,
                        now_iso=now_iso,
                    )
                    _emit_opening_sync_tx(
                        conn,
                        room_sync,
                        session=cur_session,
                        version_id=version_id,
                        stage_number=ver.version_number,
                        opening_turn_id=opening_turn_id,
                        opening_text=review_res.opening_message,
                        now_iso=now_iso,
                    )
                else:
                    assert opening_res is not None
                    assessment_payload = opening_res.assessment or {
                        "solutionSummary": "",
                        "difficultyOpinion": "",
                        "features": [],
                        "suggestions": [],
                        "satisfactionQuestion": None,
                    }
                    conn.execute(
                        "INSERT INTO conversation_turns("
                        "turn_id, session_id, sequence_number, request_id, role, content, "
                        "language, guidance_json, model, attempts_used, latency_ms, "
                        "version_id, created_at"
                        ") VALUES (?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            opening_turn_id,
                            session_id,
                            seq,
                            idempotency_key,
                            opening_res.assistant_message,
                            cur_session.language,
                            dump_json(opening_res.guidance),
                            opening_res.model,
                            opening_res.attempts_used,
                            opening_res.latency_ms,
                            version_id,
                            now_iso,
                        ),
                    )
                    conn.execute(
                        "UPDATE level_versions SET assessment_json = ? "
                        "WHERE session_id = ? AND version_id = ? AND assessment_json IS NULL",
                        (dump_json(assessment_payload), session_id, version_id),
                    )
                    is_fallback = opening_res.source == "snapshot_fallback"
                    _record_reply_outcome_tx(
                        conn,
                        session_id=session_id,
                        version_id=version_id,
                        idempotency_key=idempotency_key,
                        task="stage_opening",
                        model_succeeded=not is_fallback,
                        body_delivered=bool(opening_res.assistant_message),
                        full_quality=not is_fallback,
                        snapshot_fallback=is_fallback,
                        attempts_used=opening_res.attempts_used,
                        latency_ms=opening_res.latency_ms,
                        request_id=req_id,
                        now_iso=now_iso,
                    )
                    _emit_opening_sync_tx(
                        conn,
                        room_sync,
                        session=cur_session,
                        version_id=version_id,
                        stage_number=ver.version_number,
                        opening_turn_id=opening_turn_id,
                        opening_text=opening_res.assistant_message,
                        now_iso=now_iso,
                    )
        if operational_repo is not None:
            operational_repo.complete_key(
                session_id, "assess_version", idempotency_key, response={"versionId": version_id}
            )
    except asyncio.CancelledError:
        if operational_repo is not None:
            operational_repo.complete_key(
                session_id, "assess_version", idempotency_key, status="failed"
            )
        raise
    except Exception:
        if operational_repo is not None:
            operational_repo.complete_key(
                session_id, "assess_version", idempotency_key, status="failed"
            )
        raise


__all__ = [
    "assess_version_impl",
    "restore_version_impl",
    "supersede_active_stage_artifacts_tx",
]
