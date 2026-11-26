"""Ordinary Policy-2 conversation handler (LLM chat + DesignContext + protected refusal)."""

from __future__ import annotations

import uuid
from typing import Any

from server.application.sessions import RoomSyncWriter, append_audit_event_tx
from server.core.clock import Clock
from server.domain.errors import ConflictError
from server.domain.proposals import render_protected_refusal
from server.llm.pipelines.chat import ChatPipeline
from server.llm.pipelines.understanding import TurnRoutingResult
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import SessionReadModel

from .._shared import (
    apply_turn_context_patch,
    build_play_summary,
    emit_turn_room_event_tx,
    insert_turn_tx,
    require_current_version_tx,
    serialize_turns_for_llm,
)


class OrdinaryChatHandler:
    """Handles non-proposal Policy-2 dialogue turns and deterministic protected refusals."""

    def __init__(
        self,
        db: SQLiteDatabase,
        chat_pipeline: ChatPipeline | None,
        *,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock,
    ) -> None:
        self._db = db
        self._chat_pipeline = chat_pipeline
        self._room_sync = room_sync
        self._clock = clock

    async def handle(
        self,
        *,
        read_model: SessionReadModel,
        user_text: str,
        idempotency_key: str,
        request_id: str,
        routing: TurnRoutingResult | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Run ChatPipeline OUTSIDE write_tx (or emit deterministic refusal), then commit."""
        session = read_model.session
        base_vid = session.current_version_id
        if not base_vid:
            raise ConflictError("STAGE_NOT_INITIALIZED", "error.version_conflict")
        current_ver = next((v for v in read_model.versions if v.version_id == base_vid), None)
        if current_ver is None:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")

        if routing is not None and routing.protected_change_requested:
            reply_text = render_protected_refusal(
                routing.protected_entities, user_text, session.language
            )
            guidance: dict[str, Any] = {
                "replyType": "protected_refusal",
                "protectedEntities": list(routing.protected_entities),
            }
            patch: dict[str, Any] | None = None
            model, attempts_used, latency_ms = "deterministic", 0, 0
        else:
            if self._chat_pipeline is None:
                raise ConflictError("LLM_UNAVAILABLE", "error.configuration")
            conversation = [
                *serialize_turns_for_llm(read_model.turns),
                {
                    "role": "user",
                    "content": user_text,
                    "language": session.language,
                    "versionId": base_vid,
                },
            ]
            routing_hint = (
                routing.payload.model_dump(by_alias=True) if routing is not None else None
            )
            chat_res = await self._chat_pipeline.generate_chat_reply(
                conversation=conversation,
                grid_or_rows=current_ver.rows,
                language=session.language,
                solver_metrics=current_ver.solver,
                play_summary=build_play_summary(read_model.play_attempts, base_vid),
                design_context=current_ver.design_context,
                routing_hint=routing_hint,
                entity_bindings=current_ver.entity_bindings,
                deadline=deadline,
                request_id=request_id or idempotency_key,
            )
            reply_text = chat_res.assistant_message
            guidance = dict(chat_res.guidance or {})
            patch = dict(chat_res.design_context_patch) if chat_res.design_context_patch else None
            model = chat_res.model
            attempts_used = chat_res.attempts_used
            latency_ms = chat_res.latency_ms

        now_iso = self._clock.utc_now()
        user_turn_id = uuid.uuid4().hex
        assistant_turn_id = uuid.uuid4().hex

        with self._db.write_tx() as conn:
            cur_session, cur_ver = require_current_version_tx(
                conn, session.session_id, current_ver.version_id
            )
            updated_ctx, intent_audit = apply_turn_context_patch(
                cur_ver.design_context,
                patch=patch,
                version_id=cur_ver.version_id,
                user_turn_id=user_turn_id,
                assistant_turn_id=assistant_turn_id,
                user_text=user_text,
                guidance=guidance,
            )
            insert_turn_tx(
                conn,
                turn_id=user_turn_id,
                session_id=cur_session.session_id,
                request_id=idempotency_key,
                role="user",
                content=user_text,
                language=cur_session.language,
                version_id=cur_ver.version_id,
                created_at=now_iso,
            )
            insert_turn_tx(
                conn,
                turn_id=assistant_turn_id,
                session_id=cur_session.session_id,
                request_id=idempotency_key,
                role="assistant",
                content=reply_text,
                language=cur_session.language,
                version_id=cur_ver.version_id,
                created_at=now_iso,
                guidance=guidance,
                model=model,
                attempts_used=attempts_used,
                latency_ms=latency_ms,
            )
            if updated_ctx != (cur_ver.design_context or {}):
                conn.execute(
                    "UPDATE level_versions SET design_context_json = ? "
                    "WHERE session_id = ? AND version_id = ?",
                    (dump_json(updated_ctx), cur_session.session_id, cur_ver.version_id),
                )
            if intent_audit is not None:
                append_audit_event_tx(
                    conn,
                    cur_session.session_id,
                    "intent_hypothesis_recorded",
                    {
                        "versionId": cur_ver.version_id,
                        "turnId": assistant_turn_id,
                        **intent_audit,
                    },
                    now_iso,
                )
            emit_turn_room_event_tx(
                conn,
                self._room_sync,
                session=cur_session,
                version=cur_ver,
                user_turn_id=user_turn_id,
                assistant_turn_id=assistant_turn_id,
                user_text=user_text,
                assistant_text=reply_text,
                now_iso=now_iso,
            )
        return {
            "userTurnId": user_turn_id,
            "assistantTurnId": assistant_turn_id,
            "versionId": current_ver.version_id,
        }
