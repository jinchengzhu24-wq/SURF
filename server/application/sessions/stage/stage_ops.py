"""Extracted Stage restore and assessment helpers for StageService."""

from __future__ import annotations

import uuid

from server.core.clock import Clock
from server.domain.errors import ConflictError, IdempotencyConflictError, NotFoundError
from server.domain.memory import ensure_design_context, set_active_disagreement
from server.domain.sessions import describe_diff, summarize_stage_changes
from server.llm.pipelines.chat import ChatPipeline
from server.persistence.database import SQLiteDatabase, dump_json
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
from ..lifecycle.tokens import request_digest
from .entity_bindings import build_entity_bindings


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
    )
    if claim.outcome == "conflict":
        raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
    if claim.outcome == "completed":
        return
    if claim.outcome == "in_progress":
        raise ConflictError("RESTORE_IN_PROGRESS", "error.version_conflict")

    try:
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
    request_id: str = "",
    deadline: float | None = None,
) -> None:
    """Generate Stage opening OUTSIDE write_tx, then commit turn & assessment atomically."""
    session_id = session.session_id
    version = sessions_repo.get_version(session_id, version_id)
    if version is None:
        raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
    if version.assessment is not None:
        return
    if session.current_version_id != version_id:
        raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
    if chat_pipeline is None:
        raise ConflictError("LLM_UNAVAILABLE", "error.configuration")

    change_summary = None
    if version.parent_version_id:
        parent = sessions_repo.get_version(session_id, version.parent_version_id)
        if parent is not None:
            change_summary = summarize_stage_changes(parent.rows, version.rows).to_wire_dict()

    opening_res = await chat_pipeline.generate_stage_opening(
        version.rows,
        stage_number=version.version_number,
        language=session.language,
        solver_metrics=version.solver,
        design_context=version.design_context,
        change_summary=change_summary,
        entity_bindings=version.entity_bindings,
        deadline=deadline,
        request_id=request_id or idempotency_key,
    )
    now_iso = clock.utc_now()
    turn_id = uuid.uuid4().hex
    assessment_payload = opening_res.assessment or {
        "solutionSummary": "",
        "difficultyOpinion": "",
        "features": [],
        "suggestions": [],
        "satisfactionQuestion": None,
    }

    with db.write_tx() as conn:
        ver_row = conn.execute(
            "SELECT * FROM level_versions WHERE session_id = ? AND version_id = ?",
            (session_id, version_id),
        ).fetchone()
        if ver_row is None:
            raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
        ver = row_to_version(ver_row)
        if ver.assessment is None:
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq "
                "FROM conversation_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(seq_row["next_seq"]) if seq_row is not None else 1
            conn.execute(
                "INSERT INTO conversation_turns("
                "turn_id, session_id, sequence_number, request_id, role, content, language, "
                "guidance_json, model, attempts_used, latency_ms, version_id, created_at"
                ") VALUES (?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    session_id,
                    seq,
                    idempotency_key,
                    opening_res.assistant_message,
                    session.language,
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
            opening_ev = {
                "eventId": f"opening:{version_id}",
                "eventType": "opening",
                "playerNumber": session.player_slot or 1,
                "sessionId": session_id,
                "versionId": version_id,
                "stageNumber": ver.version_number,
                "assistantTurnId": turn_id,
                "assistantText": opening_res.assistant_message,
                "language": session.language,
                "cards": [],
            }
            emit_room_cocreation_event_tx(
                conn,
                room_sync,
                match_id=session.match_id,
                player_slot=session.player_slot,
                is_demo=session.is_demo,
                session_id=session_id,
                event=opening_ev,
                now_iso=now_iso,
            )


__all__ = ["assess_version_impl", "restore_version_impl"]
