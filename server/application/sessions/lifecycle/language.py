"""Extracted language-lock and Stage-1 promotion logic for SessionLifecycleService."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Literal

from server.core.clock import Clock, utc_now_iso
from server.domain.errors import ConflictError, NotFoundError
from server.domain.memory import DesignContextState
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import DesignSessionRecord
from server.persistence.repositories._mappers import row_to_session

from ..config import (
    CoCreationAppSettings,
    RoomSyncWriter,
    append_audit_event_tx,
    build_stage_sync_event,
    emit_room_cocreation_event_tx,
    solve_stage_async,
)
from ..stage.entity_bindings import build_entity_bindings


async def update_language_impl(
    *,
    db: SQLiteDatabase,
    room_sync: RoomSyncWriter | None,
    clock: Clock,
    settings: CoCreationAppSettings,
    session: DesignSessionRecord,
    language: Literal["en", "zh-CN"],
) -> None:
    """Lock language, solve draft outside write_tx, and promote draft to Stage 1."""
    if session.status != "active":
        raise ConflictError("SESSION_LOCKED", "error.session_locked")
    if session.language_locked or session.language_locked_at is not None:
        if session.language != language:
            raise ConflictError("LANGUAGE_LOCKED", "error.language_locked")
        return

    if not session.draft_rows:
        raise ConflictError("DRAFT_PREVIEW_UNAVAILABLE", "error.version_conflict")
    solved = await solve_stage_async(session.draft_rows, clock=clock)
    now_dt = clock.utc_datetime()
    now_iso = utc_now_iso(now_dt)
    deadline_at = (
        None
        if session.is_demo
        else utc_now_iso(now_dt + timedelta(seconds=settings.cocreation_deadline_seconds))
    )
    bindings = build_entity_bindings(solved.rows, source="initial")
    version_id = uuid.uuid4().hex
    context_payload = DesignContextState(updatedFromStageId=version_id).to_wire_dict()

    with db.write_tx() as conn:
        cur_row = conn.execute(
            "SELECT * FROM design_sessions WHERE session_id = ?", (session.session_id,)
        ).fetchone()
        if cur_row is None:
            raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
        cur_session = row_to_session(cur_row)
        if cur_session.status != "active":
            raise ConflictError("SESSION_LOCKED", "error.session_locked")
        if cur_session.language_locked or cur_session.language_locked_at is not None:
            if cur_session.language != language:
                raise ConflictError("LANGUAGE_LOCKED", "error.language_locked")
            return

        if cur_session.current_version_id is None:
            active_regen = conn.execute(
                "SELECT 1 FROM draft_regenerations "
                "WHERE session_id = ? AND status IN ('pending', 'claimed') LIMIT 1",
                (session.session_id,),
            ).fetchone()
            if active_regen is not None:
                raise ConflictError(
                    "DRAFT_REGENERATION_ACTIVE", "error.draft_regeneration_active"
                )
            conn.execute(
                "INSERT INTO level_versions("
                "version_id, session_id, version_number, source, summary, rows_json, "
                "diff_json, design_context_json, solver_json, entity_bindings_json, "
                "parent_version_id, idempotency_key, created_at"
                ") VALUES (?, ?, 1, 'initial', ?, ?, '[]', ?, ?, ?, NULL, ?, ?)",
                (
                    version_id,
                    session.session_id,
                    "Algorithm-generated demo map"
                    if cur_session.is_demo
                    else "Initial draft from Unity",
                    dump_json(list(solved.rows)),
                    dump_json(context_payload),
                    dump_json(solved.as_dict()),
                    dump_json(bindings),
                    f"initial:{cur_session.creation_key or session.session_id}",
                    now_iso,
                ),
            )
            target_vid = version_id
        else:
            target_vid = cur_session.current_version_id

        conn.execute(
            "UPDATE design_sessions "
            "SET current_version_id = ?, language = ?, language_locked = 1, "
            "language_locked_at = ?, deadline_started_at = COALESCE(deadline_started_at, ?), "
            "deadline_at = COALESCE(deadline_at, ?), draft_rows_json = NULL, "
            "draft_validation_json = NULL, updated_at = ? "
            "WHERE session_id = ?",
            (
                target_vid,
                language,
                now_iso,
                None if cur_session.is_demo else now_iso,
                deadline_at,
                now_iso,
                session.session_id,
            ),
        )
        append_audit_event_tx(
            conn,
            session.session_id,
            "language_selected",
            {"language": language, "deadlineAt": deadline_at, "versionId": target_vid},
            now_iso,
        )
        if target_vid == version_id:
            first_event = build_stage_sync_event(
                session_id=session.session_id,
                player_slot=cur_session.player_slot or 1,
                version_id=version_id,
                version_number=1,
                source="initial",
                rows=solved.rows,
                diff=[],
                initial_draft_method=cur_session.mode,
                event_type="first_stage",
            )
            emit_room_cocreation_event_tx(
                conn,
                room_sync,
                match_id=cur_session.match_id,
                player_slot=cur_session.player_slot,
                is_demo=cur_session.is_demo,
                session_id=session.session_id,
                event=first_event,
                now_iso=now_iso,
            )


__all__ = ["update_language_impl"]
