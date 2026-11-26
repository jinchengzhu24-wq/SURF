"""Native-async Stage version lifecycle (save, restore, assess) with optimistic CAS."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.generator import generate_demo_level
from server.domain.errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from server.domain.memory import ensure_design_context, set_active_disagreement
from server.domain.sessions import describe_diff, summarize_stage_changes
from server.i18n.messages import format_verified_diff_summary
from server.llm.pipelines.chat import ChatPipeline
from server.llm.pipelines.understanding import UnderstandingPipeline
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.repositories._mappers import row_to_session, row_to_version
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ..config import (
    CoCreationAppSettings,
    RoomSyncWriter,
    append_audit_event_tx,
    build_stage_sync_event,
    emit_room_cocreation_event_tx,
    next_version_number_tx,
    solve_stage_async,
)
from ..lifecycle.tokens import request_digest
from ..read_model import SessionReadModelService
from .draft_ops import _DraftRegenLifecycleMixin
from .entity_bindings import build_entity_bindings, derive_entity_transitions
from .play_ops import _PlayLifecycleMixin
from .stage_ops import assess_version_impl, restore_version_impl


class StageService(_PlayLifecycleMixin, _DraftRegenLifecycleMixin):
    """Manages immutable Stage versions, play attempts, and draft regenerations."""

    def __init__(
        self,
        db: SQLiteDatabase,
        sessions_repo: SessionsRepository,
        operational_repo: OperationalRepository,
        read_model: SessionReadModelService,
        *,
        chat_pipeline: ChatPipeline | None = None,
        understanding_pipeline: UnderstandingPipeline | None = None,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
        demo_level_generator: Callable[..., Any] = generate_demo_level,
    ) -> None:
        self._db = db
        self._sessions_repo = sessions_repo
        self._operational_repo = operational_repo
        self._read_model = read_model
        self._chat_pipeline = chat_pipeline
        self._understanding_pipeline = understanding_pipeline
        self._room_sync = room_sync
        self._clock = clock
        self._settings = settings or CoCreationAppSettings()
        self._demo_level_generator = demo_level_generator

    async def save_version(
        self,
        session_id: str,
        access_token: str | None,
        *,
        base_version_id: str,
        rows: Sequence[str],
        idempotency_key: str,
        summary: str = "",
    ) -> dict[str, Any]:
        """Validate & solve OUTSIDE write_tx, then save manual Stage via optimistic CAS."""
        session = self._require_active_session(session_id, access_token)
        sig = request_digest({"baseVersionId": base_version_id, "rows": list(rows)})
        claim = self._operational_repo.claim_key(
            session_id,
            "save_version",
            idempotency_key,
            request_hash=sig,
            created_at=self._clock.utc_now(),
        )
        if claim.outcome == "conflict":
            raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
        if claim.outcome == "completed":
            return await self._read_model.get_session_payload(session_id)
        if claim.outcome == "in_progress":
            raise ConflictError("SAVE_IN_PROGRESS", "error.version_conflict")

        try:
            if session.current_version_id != base_version_id:
                raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
            solved = await solve_stage_async(rows, clock=self._clock)
            now_iso = self._clock.utc_now()
            clean_summary = str(summary or "").strip()[:1000] or "Designer saved an edited stage"

            with self._db.write_tx() as conn:
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

                base_row = conn.execute(
                    "SELECT * FROM level_versions WHERE session_id = ? AND version_id = ?",
                    (session_id, base_version_id),
                ).fetchone()
                if base_row is None:
                    raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")
                base_ver = row_to_version(base_row)
                if list(solved.rows) == base_ver.rows:
                    raise ValidationError("UNCHANGED_LEVEL", "error.unchanged_level")

                ctx = ensure_design_context(base_ver.design_context)
                prev_dis = ctx.active_disagreement
                closes_manual = (
                    prev_dis is not None
                    and prev_dis.status == "active"
                    and prev_dis.subject == "human_edit"
                )
                child_ctx = set_active_disagreement(ctx, None, base_ver.version_id, None)
                transitions = derive_entity_transitions(base_ver.rows, solved.rows)
                bindings = build_entity_bindings(
                    solved.rows,
                    parent_bindings=base_ver.entity_bindings,
                    entity_transitions=transitions,
                    source="human_edit",
                )
                diff = describe_diff(base_ver.rows, solved.rows)
                new_vid = uuid.uuid4().hex
                new_num = next_version_number_tx(conn, session_id)

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
                        clean_summary,
                        dump_json(list(solved.rows)),
                        dump_json(diff),
                        dump_json(child_ctx.to_wire_dict()),
                        dump_json(solved.as_dict()),
                        dump_json(bindings),
                        base_ver.version_id,
                        idempotency_key,
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

                if closes_manual and prev_dis is not None:
                    append_audit_event_tx(
                        conn,
                        session_id,
                        "disagreement_resolved",
                        {
                            "versionId": base_ver.version_id,
                            "disagreement": {
                                **prev_dis.model_dump(by_alias=True),
                                "status": "resolved",
                                "resolution": "user",
                                "displayCard": False,
                                "resultVersionId": new_vid,
                            },
                        },
                        now_iso,
                    )
                append_audit_event_tx(
                    conn,
                    session_id,
                    "intent_evidence_recorded",
                    {
                        "evidenceId": f"ie_{uuid.uuid4().hex}",
                        "kind": "manual_edit",
                        "versionId": new_vid,
                        "observation": clean_summary,
                        "details": {
                            "parentVersionId": base_ver.version_id,
                            "diff": diff,
                            "changeSummary": summarize_stage_changes(
                                base_ver.rows, solved.rows
                            ).to_wire_dict(),
                            "verifiedDiff": format_verified_diff_summary(
                                diff, cur_session.language
                            ),
                            "validation": solved.as_dict(),
                        },
                        "authority": "evidence_only",
                    },
                    now_iso,
                )
                stage_ev = build_stage_sync_event(
                    session_id=session_id,
                    player_slot=cur_session.player_slot or 1,
                    version_id=new_vid,
                    version_number=new_num,
                    source="human_edit",
                    rows=solved.rows,
                    diff=diff,
                    initial_draft_method=cur_session.mode,
                    event_type="stage",
                )
                emit_room_cocreation_event_tx(
                    conn,
                    self._room_sync,
                    match_id=cur_session.match_id,
                    player_slot=cur_session.player_slot,
                    is_demo=cur_session.is_demo,
                    session_id=session_id,
                    event=stage_ev,
                    now_iso=now_iso,
                )
            self._operational_repo.complete_key(
                session_id, "save_version", idempotency_key, response={"versionId": new_vid}
            )
        except Exception:
            self._operational_repo.complete_key(
                session_id, "save_version", idempotency_key, status="failed"
            )
            raise
        return await self._read_model.get_session_payload(session_id)

    async def restore_version(
        self,
        session_id: str,
        version_id: str,
        access_token: str | None,
        *,
        base_version_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Solve restored Stage OUTSIDE write_tx, then create child Stage via optimistic CAS."""
        session = self._require_active_session(session_id, access_token)
        await restore_version_impl(
            db=self._db,
            sessions_repo=self._sessions_repo,
            operational_repo=self._operational_repo,
            room_sync=self._room_sync,
            clock=self._clock,
            session=session,
            version_id=version_id,
            base_version_id=base_version_id,
            idempotency_key=idempotency_key,
        )
        return await self._read_model.get_session_payload(session_id)

    async def assess_version(
        self,
        session_id: str,
        version_id: str,
        access_token: str | None,
        *,
        idempotency_key: str,
        request_id: str = "",
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Generate Stage opening / manual-edit review OUTSIDE write_tx, then commit atomically."""
        session = self._require_active_session(session_id, access_token)
        await assess_version_impl(
            db=self._db,
            sessions_repo=self._sessions_repo,
            chat_pipeline=self._chat_pipeline,
            room_sync=self._room_sync,
            clock=self._clock,
            session=session,
            version_id=version_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            deadline=deadline,
        )
        return await self._read_model.get_session_payload(session_id)


__all__ = ["StageService"]
