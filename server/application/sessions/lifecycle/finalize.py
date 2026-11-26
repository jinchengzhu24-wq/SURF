"""Extracted finalization and intention submission logic for SessionLifecycleService."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

from server.core.clock import Clock
from server.domain.errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from server.domain.sessions import describe_diff
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import DesignSessionRecord
from server.persistence.repositories._mappers import row_to_session, row_to_version
from server.persistence.repositories.operational_repo import OperationalRepository

from ..config import (
    CoCreationAppSettings,
    RoomSyncWriter,
    append_audit_event_tx,
    build_stage_sync_event,
    emit_room_cocreation_event_tx,
    emit_room_designer_intention_tx,
    next_version_number_tx,
    solve_stage_async,
)
from ..stage.entity_bindings import build_entity_bindings
from .tokens import (
    compute_cocreation_duration_seconds,
    is_deadline_expired,
    request_digest,
    utc_plus_seconds_iso,
)


async def finalize_session_impl(
    *,
    db: SQLiteDatabase,
    operational_repo: OperationalRepository,
    room_sync: RoomSyncWriter | None,
    clock: Clock,
    settings: CoCreationAppSettings,
    session: DesignSessionRecord,
    base_version_id: str,
    idempotency_key: str,
    rows: Sequence[str] | None = None,
) -> None:
    """Finalize a Stage (with optional deadline draft auto-save) and sync `final` atomically."""
    session_id = session.session_id
    sig = request_digest({"baseVersionId": base_version_id, "rows": list(rows) if rows else None})
    claim = operational_repo.claim_key(
        session_id,
        "finalize",
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
        raise ConflictError("FINALIZE_IN_PROGRESS", "error.version_conflict")

    try:
        deadline_expired = is_deadline_expired(session.deadline_at, now=clock.utc_datetime())
        solved = (
            await solve_stage_async(rows, clock=clock)
            if (deadline_expired and rows is not None)
            else None
        )
        now_iso = clock.utc_now()
        with db.write_tx() as conn:
            cur_row = conn.execute(
                "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if cur_row is None:
                raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
            cur = row_to_session(cur_row)
            if cur.status != "active":
                raise ConflictError("SESSION_LOCKED", "error.session_locked")
            target_row = conn.execute(
                "SELECT * FROM level_versions WHERE session_id = ? AND version_id = ?",
                (session_id, base_version_id),
            ).fetchone()
            if target_row is None:
                raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
            target = row_to_version(target_row)
            historical = target.version_id != cur.current_version_id
            if historical and rows is not None:
                raise ConflictError(
                    "HISTORICAL_FINALIZE_ROWS_NOT_ALLOWED", "error.version_conflict"
                )

            pending_rows = conn.execute(
                "SELECT proposal_id FROM change_proposals "
                "WHERE session_id = ? AND base_version_id = ? AND status = 'pending'",
                (session_id, base_version_id),
            ).fetchall()
            if pending_rows and not deadline_expired and not historical:
                raise ConflictError("PENDING_PROPOSAL", "error.version_conflict")
            if (
                not deadline_expired
                and not historical
                and rows is not None
                and list(rows) != target.rows
            ):
                raise ConflictError("UNSAVED_DRAFT_ROWS", "error.version_conflict")

            final_ver = target
            if deadline_expired and solved is not None and cur.current_version_id:
                cur_ver_row = conn.execute(
                    "SELECT * FROM level_versions WHERE session_id = ? AND version_id = ?",
                    (session_id, cur.current_version_id),
                ).fetchone()
                assert cur_ver_row is not None
                cur_ver = row_to_version(cur_ver_row)
                if list(solved.rows) != cur_ver.rows:
                    new_vid = uuid.uuid4().hex
                    new_num = next_version_number_tx(conn, session_id)
                    diff = describe_diff(cur_ver.rows, solved.rows)
                    bindings = build_entity_bindings(
                        solved.rows,
                        parent_bindings=cur_ver.entity_bindings,
                        source="human_edit",
                    )
                    conn.execute(
                        "INSERT INTO level_versions("
                        "version_id, session_id, version_number, source, summary, rows_json, "
                        "diff_json, design_context_json, solver_json, entity_bindings_json, "
                        "parent_version_id, idempotency_key, created_at"
                        ") VALUES (?, ?, ?, 'human_edit', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_vid,
                            session_id,
                            new_num,
                            "Designer edit saved at the co-creation deadline",
                            dump_json(list(solved.rows)),
                            dump_json(diff),
                            dump_json(cur_ver.design_context or {}),
                            dump_json(solved.as_dict()),
                            dump_json(bindings),
                            cur_ver.version_id,
                            f"deadline-finalize:{idempotency_key}",
                            now_iso,
                        ),
                    )
                    final_row = conn.execute(
                        "SELECT * FROM level_versions WHERE version_id = ?", (new_vid,)
                    ).fetchone()
                    assert final_row is not None
                    final_ver = row_to_version(final_row)
                    stage_ev = build_stage_sync_event(
                        session_id=session_id,
                        player_slot=cur.player_slot or 1,
                        version_id=final_ver.version_id,
                        version_number=final_ver.version_number,
                        source="human_edit",
                        rows=final_ver.rows,
                        diff=final_ver.diff,
                        initial_draft_method=cur.mode,
                        event_type="stage",
                    )
                    emit_room_cocreation_event_tx(
                        conn,
                        room_sync,
                        match_id=cur.match_id,
                        player_slot=cur.player_slot,
                        is_demo=cur.is_demo,
                        session_id=session_id,
                        event=stage_ev,
                        now_iso=now_iso,
                    )

            conn.execute(
                "UPDATE change_proposals SET status = 'superseded', decided_at = ? "
                "WHERE session_id = ? AND status = 'pending'",
                (now_iso, session_id),
            )
            conn.execute(
                "UPDATE design_sessions "
                "SET status = 'awaiting_intention', current_version_id = ?, "
                "finalized_version_id = ?, finalized_at = ?, updated_at = ? "
                "WHERE session_id = ? AND status = 'active'",
                (final_ver.version_id, final_ver.version_id, now_iso, now_iso, session_id),
            )
            duration = compute_cocreation_duration_seconds(
                cur.deadline_at,
                now_iso,
                cocreation_deadline_seconds=settings.cocreation_deadline_seconds,
            )
            final_ev = build_stage_sync_event(
                session_id=session_id,
                player_slot=cur.player_slot or 1,
                version_id=final_ver.version_id,
                version_number=final_ver.version_number,
                source=final_ver.source,
                rows=final_ver.rows,
                diff=final_ver.diff,
                initial_draft_method=cur.mode,
                event_type="final",
                duration_seconds=duration,
            )
            emit_room_cocreation_event_tx(
                conn,
                room_sync,
                match_id=cur.match_id,
                player_slot=cur.player_slot,
                is_demo=cur.is_demo,
                session_id=session_id,
                event=final_ev,
                now_iso=now_iso,
            )
            append_audit_event_tx(
                conn,
                session_id,
                "finalization_request_committed",
                {"idempotencyKey": idempotency_key, "finalVersionId": final_ver.version_id},
                now_iso,
            )
        operational_repo.complete_key(
            session_id,
            "finalize",
            idempotency_key,
            response={"finalVersionId": final_ver.version_id},
        )
    except asyncio.CancelledError:
        operational_repo.complete_key(session_id, "finalize", idempotency_key, status="failed")
        raise
    except Exception:
        operational_repo.complete_key(session_id, "finalize", idempotency_key, status="failed")
        raise


def submit_intention_tx(
    *,
    db: SQLiteDatabase,
    room_sync: RoomSyncWriter | None,
    clock: Clock,
    session_id: str,
    content: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Atomically persist final designer intention and sync into rooms in ONE write_tx."""
    clean_content = str(content or "").strip()
    if not clean_content or len(clean_content) > 4000:
        raise ValidationError("INVALID_INTENTION", "error.validation_failed")
    now_iso = clock.utc_now()
    event_id = f"message:{session_id}:{idempotency_key}"

    already_completed = False
    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
        cur = row_to_session(row)
        if cur.status == "completed":
            existing = cur.final_intention or {}
            if (
                existing.get("content") != clean_content
                or existing.get("idempotencyKey") != idempotency_key
            ):
                raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
            already_completed = True
        elif cur.status != "awaiting_intention":
            raise ConflictError("SESSION_NOT_FINALIZED", "error.session_locked")
        if not already_completed:
            intention_payload = {
                "content": clean_content,
                "language": cur.language,
                "idempotencyKey": idempotency_key,
                "createdAt": now_iso,
            }
            conn.execute(
                "UPDATE design_sessions "
                "SET status = 'completed', completed_at = ?, final_intention_json = ?, "
                "updated_at = ? "
                "WHERE session_id = ? AND status = 'awaiting_intention'",
                (now_iso, dump_json(intention_payload), now_iso, session_id),
            )
            append_audit_event_tx(
                conn,
                session_id,
                "intention_submitted",
                {"idempotencyKey": idempotency_key, "matchId": cur.match_id},
                now_iso,
            )
            emit_room_designer_intention_tx(
                conn,
                room_sync,
                match_id=cur.match_id,
                player_slot=cur.player_slot,
                is_demo=cur.is_demo,
                session_id=session_id,
                designer_intention=clean_content,
                event_id=event_id,
                now_iso=now_iso,
            )
    return {"sessionId": session_id, "alreadyCompleted": already_completed}


__all__ = ["finalize_session_impl", "submit_intention_tx"]
