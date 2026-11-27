"""Ordinary Policy-2 conversation handler (LLM chat + DesignContext + protected refusal)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from server.application.sessions import (
    RoomSyncWriter,
    append_audit_event_tx,
    build_disagreement_audit_summary,
)
from server.core.clock import Clock
from server.domain.errors import ConflictError
from server.domain.memory import ensure_design_context, set_active_disagreement
from server.domain.proposals import render_protected_refusal
from server.llm.pipelines.chat import ChatPipeline
from server.llm.pipelines.understanding import TurnRoutingResult
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import SessionReadModel
from server.persistence.repositories.operational_repo import OperationalRepository

from .._shared import (
    apply_turn_context_patch,
    build_play_summary,
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
        human_edit_engaged: bool = False,
        human_edit_recovery_needed: bool = False,
        active_disagreement: Mapping[str, Any] | None = None,
        recovery_task: Mapping[str, Any] | None = None,
        complete_idempotency_scope: str | None = None,
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
            guidance.pop("proposalOffer", None)
            patch = dict(chat_res.design_context_patch) if chat_res.design_context_patch else None
            model = chat_res.model
            attempts_used = chat_res.attempts_used
            latency_ms = chat_res.latency_ms

        now_iso = self._clock.utc_now()
        user_turn_id = uuid.uuid4().hex
        assistant_turn_id = uuid.uuid4().hex
        claims = (guidance.get("_intentDecision") or {}).get("claims")
        rec_id: str | None = None

        with self._db.write_tx() as conn:
            cur_session, cur_ver = require_current_version_tx(
                conn, session.session_id, current_ver.version_id, clock=self._clock
            )
            updated_ctx, intent_audit = apply_turn_context_patch(
                cur_ver.design_context,
                patch=patch,
                version_id=cur_ver.version_id,
                user_turn_id=user_turn_id,
                assistant_turn_id=assistant_turn_id,
                user_text=user_text,
                guidance=guidance,
                semantic_claims=claims,
            )
            dis_acknowledged_payload: dict[str, Any] | None = None
            if (
                human_edit_engaged
                and isinstance(active_disagreement, Mapping)
                and active_disagreement.get("subject") == "human_edit"
            ):
                dis_acknowledged_payload = {
                    **dict(active_disagreement),
                    "status": "acknowledged",
                    "displayCard": False,
                    "resolution": None,
                }
                guidance["disagreement"] = dis_acknowledged_payload
                cleared_state = set_active_disagreement(
                    ensure_design_context(updated_ctx),
                    None,
                    cur_ver.version_id,
                    assistant_turn_id,
                )
                updated_ctx = cleared_state.to_wire_dict()

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
            if dis_acknowledged_payload is not None:
                append_audit_event_tx(
                    conn,
                    cur_session.session_id,
                    "disagreement_acknowledged",
                    {
                        "versionId": cur_ver.version_id,
                        "turnId": assistant_turn_id,
                        "subject": "human_edit",
                        "disagreement": build_disagreement_audit_summary(
                            dis_acknowledged_payload
                        ),
                    },
                    now_iso,
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
                if intent_audit.get("evidenceId"):
                    append_audit_event_tx(
                        conn,
                        cur_session.session_id,
                        "intent_evidence_recorded",
                        {
                            "evidenceId": intent_audit["evidenceId"],
                            "kind": "expressed_direction",
                            "versionId": cur_ver.version_id,
                            "turnId": user_turn_id,
                            "observation": user_text[:500],
                        },
                        now_iso,
                    )
            append_audit_event_tx(
                conn,
                cur_session.session_id,
                "reply_delivery_outcome",
                {
                    "messageKey": idempotency_key,
                    "requestId": request_id or idempotency_key,
                    "task": "ordinary_chat",
                    "baseVersionId": cur_ver.version_id,
                    "modelGenerationSucceeded": model != "deterministic",
                    "deterministicReceipt": model == "deterministic",
                    "reliableBodyDelivered": True,
                    "fullQualityPassed": not human_edit_recovery_needed,
                    "attemptsUsed": attempts_used,
                    "latencyMs": latency_ms,
                    **(
                        {"component": "human_edit_engagement"}
                        if human_edit_recovery_needed
                        else {}
                    ),
                },
                now_iso,
            )
            if recovery_task is not None:
                rec_id = str(recovery_task["recoveryId"])
                rec_state = {
                    **dict(recovery_task["state"]),
                    "createdAt": str(recovery_task["state"].get("createdAt") or now_iso),
                    "sourceUserTurnId": user_turn_id,
                    "deliveredAssistantTurnId": assistant_turn_id,
                }
                conn.execute(
                    "INSERT INTO component_recovery_tasks("
                    "recovery_id, session_id, target_kind, target_id, state_json, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(recovery_id) DO UPDATE SET "
                    "target_kind = excluded.target_kind, "
                    "target_id = excluded.target_id, "
                    "state_json = excluded.state_json, "
                    "updated_at = excluded.updated_at",
                    (
                        rec_id,
                        cur_session.session_id,
                        str(recovery_task["targetKind"]),
                        str(recovery_task["targetId"]),
                        dump_json(rec_state),
                        now_iso,
                    ),
                )

            result_payload: dict[str, Any] = {
                "userTurnId": user_turn_id,
                "assistantTurnId": assistant_turn_id,
                "versionId": current_ver.version_id,
            }
            if rec_id is not None:
                result_payload["recoveryId"] = rec_id
            if complete_idempotency_scope:
                OperationalRepository.complete_key_tx(
                    conn,
                    cur_session.session_id,
                    complete_idempotency_scope,
                    idempotency_key,
                    response=result_payload,
                )
        if complete_idempotency_scope:
            return {**result_payload, "_idempotencyCompleted": True}
        return result_payload
