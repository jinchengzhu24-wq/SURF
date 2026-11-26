"""Revision challenge handler (challengeCardSchemaVersion=2, UnderstandingPipeline outside tx)."""

from __future__ import annotations

import uuid
from typing import Any

from server.application.sessions import RoomSyncWriter, append_audit_event_tx
from server.core.clock import Clock
from server.domain.errors import ConflictError, NotFoundError, ValidationError
from server.domain.memory import ensure_design_context, set_active_disagreement
from server.i18n.messages import get_message
from server.llm.pipelines.understanding import UnderstandingPipeline
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import RevisionChallengeRecord, SessionReadModel

from .._shared import (
    emit_turn_room_event_tx,
    insert_turn_tx,
    require_current_version_tx,
)

CHALLENGE_CARD_SCHEMA_VERSION = 2


def _challenge_choices(language: str) -> list[dict[str, str]]:
    return [
        {
            "choice": "keep_proposal",
            "label": "Keep current proposal" if language != "zh-CN" else "保留当前方案",
        },
        {
            "choice": "revise_proposal",
            "label": "Adjust with my reason" if language != "zh-CN" else "按我的理由调整方案",
        },
        {
            "choice": "cancel_challenge",
            "label": "Cancel challenge" if language != "zh-CN" else "取消质疑",
        },
    ]


class ChallengeHandler:
    """Handles proposal challenges and schema-v2 reason/choice flows."""

    def __init__(
        self,
        db: SQLiteDatabase,
        understanding_pipeline: UnderstandingPipeline | None,
        *,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock,
    ) -> None:
        self._db = db
        self._understanding_pipeline = understanding_pipeline
        self._room_sync = room_sync
        self._clock = clock

    async def start_challenge(
        self,
        *,
        read_model: SessionReadModel,
        proposal_id: str,
        user_text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Start a challenge against an active pending proposal."""
        session = read_model.session
        proposal = next(
            (p for p in read_model.proposals if p.proposal_id == proposal_id), None
        )
        if proposal is None or proposal.status != "pending":
            raise NotFoundError("PROPOSAL_NOT_FOUND", "error.version_conflict")
        if proposal.base_version_id != session.current_version_id:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")

        challenge_id = f"chg_{uuid.uuid4().hex[:16]}"
        now_iso = self._clock.utc_now()
        user_turn_id = uuid.uuid4().hex
        assistant_turn_id = uuid.uuid4().hex
        prompt_text = (
            "Tell me why you want to challenge this proposal so I can compare both directions."
            if session.language != "zh-CN"
            else "请说明你质疑当前修改方案的原因，我会把你的观点与当前方案做对比。"
        )
        card = {
            "challengeId": challenge_id,
            "proposalId": proposal_id,
            "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
            "status": "awaiting_reason",
            "choices": [],
        }

        with self._db.write_tx() as conn:
            cur_session, cur_ver = require_current_version_tx(
                conn, session.session_id, proposal.base_version_id
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
                content=prompt_text,
                language=cur_session.language,
                version_id=cur_ver.version_id,
                created_at=now_iso,
                guidance={"challengeCard": card},
            )
            conn.execute(
                "INSERT INTO revision_challenges("
                "challenge_id, session_id, base_version_id, proposal_id, status, "
                "card_json, reviews_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 'awaiting_reason', ?, '[]', ?, ?)",
                (
                    challenge_id,
                    cur_session.session_id,
                    cur_ver.version_id,
                    proposal_id,
                    dump_json(card),
                    now_iso,
                    now_iso,
                ),
            )
            append_audit_event_tx(
                conn,
                cur_session.session_id,
                "challenge_started",
                {"challengeId": challenge_id, "proposalId": proposal_id},
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
                assistant_text=prompt_text,
                now_iso=now_iso,
            )
        return {"challengeId": challenge_id, "assistantTurnId": assistant_turn_id}

    async def continue_challenge(
        self,
        *,
        read_model: SessionReadModel,
        challenge: RevisionChallengeRecord,
        user_text: str,
        choice: str | None,
        idempotency_key: str,
        request_id: str,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Review challenge reason via LLM OUTSIDE tx or commit choice resolution."""
        session = read_model.session
        proposal = next(
            (p for p in read_model.proposals if p.proposal_id == challenge.proposal_id), None
        )
        if proposal is None:
            raise NotFoundError("PROPOSAL_NOT_FOUND", "error.version_conflict")

        now_iso = self._clock.utc_now()
        user_turn_id = uuid.uuid4().hex
        assistant_turn_id = uuid.uuid4().hex
        model, attempts_used, latency_ms = "", 0, 0
        reviews = list(challenge.reviews or ())

        if choice:
            if choice not in {"keep_proposal", "revise_proposal", "cancel_challenge"}:
                raise ValidationError("INVALID_CHALLENGE_CHOICE", "error.validation_failed")
            next_status = "resolved"
            assistant_text = (
                "Understood. Let's continue with your selected direction."
                if session.language != "zh-CN"
                else "明白，我将按你选择的方向继续。"
            )
            card = {
                **dict(challenge.card),
                "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                "status": "resolved",
                "selectedChoice": choice,
            }
        else:
            if self._understanding_pipeline is None:
                raise ConflictError("LLM_UNAVAILABLE", "error.configuration")
            review = await self._understanding_pipeline.review_challenge_reason(
                user_reason=user_text,
                proposal_summary=str(proposal.proposal.get("summary") or ""),
                primary_hypothesis=str(challenge.card.get("primaryHypothesis") or user_text),
                secondary_hypothesis=str(proposal.proposal.get("summary") or ""),
                language=session.language,
                deadline=deadline,
                request_id=request_id or idempotency_key,
            )
            next_status = "awaiting_choice"
            assistant_text = review.comparison or get_message("error.internal", session.language)
            reviews.append(
                {
                    "reason": user_text,
                    "relation": review.relation,
                    "merit": review.merit,
                    "comparison": review.comparison,
                    "createdAt": now_iso,
                }
            )
            card = {
                **dict(challenge.card),
                "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                "status": "awaiting_choice",
                "relation": review.relation,
                "merit": review.merit,
                "comparison": review.comparison,
                "choices": _challenge_choices(session.language),
            }
            model = self._understanding_pipeline.model
            attempts_used = review.attempts_used
            latency_ms = review.latency_ms

        with self._db.write_tx() as conn:
            cur_session, cur_ver = require_current_version_tx(
                conn, session.session_id, challenge.base_version_id
            )
            if choice in {"keep_proposal", "cancel_challenge"}:
                cleared_ctx = set_active_disagreement(
                    ensure_design_context(cur_ver.design_context),
                    None,
                    cur_ver.version_id,
                    assistant_turn_id,
                )
                conn.execute(
                    "UPDATE level_versions SET design_context_json = ? "
                    "WHERE session_id = ? AND version_id = ?",
                    (
                        dump_json(cleared_ctx.to_wire_dict()),
                        cur_session.session_id,
                        cur_ver.version_id,
                    ),
                )
            insert_turn_tx(
                conn,
                turn_id=user_turn_id,
                session_id=cur_session.session_id,
                request_id=idempotency_key,
                role="user",
                content=user_text or str(choice),
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
                content=assistant_text,
                language=cur_session.language,
                version_id=cur_ver.version_id,
                created_at=now_iso,
                guidance={"challengeCard": card},
                model=model,
                attempts_used=attempts_used,
                latency_ms=latency_ms,
            )
            conn.execute(
                "UPDATE revision_challenges SET status = ?, card_json = ?, "
                "reviews_json = ?, updated_at = ? WHERE challenge_id = ?",
                (
                    next_status,
                    dump_json(card),
                    dump_json(reviews),
                    now_iso,
                    challenge.challenge_id,
                ),
            )
            append_audit_event_tx(
                conn,
                cur_session.session_id,
                "challenge_updated",
                {"challengeId": challenge.challenge_id, "status": next_status, "choice": choice},
                now_iso,
            )
            emit_turn_room_event_tx(
                conn,
                self._room_sync,
                session=cur_session,
                version=cur_ver,
                user_turn_id=user_turn_id,
                assistant_turn_id=assistant_turn_id,
                user_text=user_text or str(choice),
                assistant_text=assistant_text,
                now_iso=now_iso,
            )
        return {"challengeId": challenge.challenge_id, "status": next_status}
