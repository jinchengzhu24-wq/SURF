"""Policy-2 binary discovery handler (question cards, choice selection, proposal creation)."""

from __future__ import annotations

import uuid
from typing import Any

from server.application.sessions import RoomSyncWriter, append_audit_event_tx
from server.core.clock import Clock
from server.domain.errors import ConflictError, NotFoundError, ValidationError
from server.llm.pipelines.binary import BinaryDiscoveryPipeline
from server.llm.pipelines.proposal import ProposalPipeline
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import SessionReadModel
from server.persistence.repositories.operational_repo import OperationalRepository

from ._binary_commit import commit_binary_step_impl


class BinaryDiscoveryHandler:
    """Handles Policy-2 binary discovery turns, A/B selection, and proposal generation."""

    def __init__(
        self,
        db: SQLiteDatabase,
        operational_repo: OperationalRepository,
        binary_pipeline: BinaryDiscoveryPipeline | None,
        proposal_pipeline: ProposalPipeline | None,
        *,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock,
    ) -> None:
        self._db = db
        self._operational_repo = operational_repo
        self._binary_pipeline = binary_pipeline
        self._proposal_pipeline = proposal_pipeline
        self._room_sync = room_sync
        self._clock = clock

    async def start_topic(
        self,
        *,
        read_model: SessionReadModel,
        user_text: str,
        idempotency_key: str,
        request_id: str,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Run Policy-2 binary discovery OUTSIDE write_tx and commit question or proposal."""
        if self._binary_pipeline is None or self._proposal_pipeline is None:
            raise ConflictError("LLM_UNAVAILABLE", "error.configuration")
        session = read_model.session
        base_vid = session.current_version_id
        current_ver = next((v for v in read_model.versions if v.version_id == base_vid), None)
        if current_ver is None:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")

        existing = self._operational_repo.get_latest_topic(session.session_id)
        if (
            existing is not None
            and existing.state.get("versionId") == current_ver.version_id
            and existing.state.get("status") == "clarifying"
        ):
            raise ConflictError("CLARIFICATION_IN_PROGRESS", "error.version_conflict")

        topic_id = f"top_{uuid.uuid4().hex[:16]}"
        turn_id_hint = f"user_{uuid.uuid4().hex[:12]}"
        initial_turns = [{"id": turn_id_hint, "content": user_text}]
        step_res = await self._binary_pipeline.run_step(
            grid_or_rows=current_ver.rows,
            initial_request=user_text,
            initial_designer_turns=initial_turns,
            question_count=0,
            selected_choices=(),
            verified_initial_requirements=None,
            design_context=current_ver.design_context,
            entity_bindings=current_ver.entity_bindings,
            language=session.language,
            deadline=deadline,
            request_id=request_id or idempotency_key,
        )
        return await self._commit_step(
            read_model=read_model,
            current_ver=current_ver,
            topic_id=topic_id,
            initial_request=user_text,
            initial_turns=initial_turns,
            prior_choices=[],
            verified_initial_req=step_res.initial_requirements,
            step_res=step_res,
            user_text=user_text,
            user_visible_action=True,
            idempotency_key=idempotency_key,
            request_id=request_id,
            deadline=deadline,
        )

    async def choose_choice(
        self,
        *,
        read_model: SessionReadModel,
        topic_id: str,
        question_id: str,
        choice_id: str,
        idempotency_key: str,
        request_id: str,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Apply designer's binary A/B selection and advance discovery or generate proposal."""
        if self._binary_pipeline is None or self._proposal_pipeline is None:
            raise ConflictError("LLM_UNAVAILABLE", "error.configuration")
        session = read_model.session
        current_ver = next(
            (v for v in read_model.versions if v.version_id == session.current_version_id), None
        )
        if current_ver is None:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
        topic_rec = self._operational_repo.get_topic(session.session_id, topic_id)
        if topic_rec is None:
            raise NotFoundError("TOPIC_NOT_FOUND", "error.version_conflict")
        state = dict(topic_rec.state)
        if state.get("versionId") != current_ver.version_id or state.get("status") != "clarifying":
            raise ConflictError("STALE_TOPIC", "error.version_conflict")
        pending = state.get("pendingQuestion") or {}
        if pending.get("questionId") != question_id:
            raise ConflictError("STALE_QUESTION", "error.version_conflict")
        chosen = next(
            (c for c in pending.get("_internalChoices") or () if c.get("choiceId") == choice_id),
            None,
        )
        if chosen is None:
            raise ValidationError("INVALID_CHOICE", "error.validation_failed")

        next_choices = [
            *list(state.get("choices") or ()),
            {
                "questionId": question_id,
                "choiceId": choice_id,
                "label": chosen["label"],
                "promise": chosen["promise"],
                "strategy": chosen["strategy"],
                "sourceTurnId": str(state.get("initialTurnId") or ""),
                "cachedRows": chosen.get("cachedRows"),
                "cachedValidation": chosen.get("cachedValidation"),
            },
        ]
        initial_request = str(state.get("initialRequest") or "")
        initial_turns = list(state.get("initialTurns") or ())
        verified_req = state.get("initialRequirements")
        step_res = await self._binary_pipeline.run_step(
            grid_or_rows=current_ver.rows,
            initial_request=initial_request,
            initial_designer_turns=initial_turns,
            question_count=len(next_choices),
            selected_choices=next_choices,
            verified_initial_requirements=verified_req,
            design_context=current_ver.design_context,
            entity_bindings=current_ver.entity_bindings,
            language=session.language,
            deadline=deadline,
            request_id=request_id or idempotency_key,
        )
        return await self._commit_step(
            read_model=read_model,
            current_ver=current_ver,
            topic_id=topic_id,
            initial_request=initial_request,
            initial_turns=initial_turns,
            prior_choices=next_choices,
            verified_initial_req=verified_req,
            step_res=step_res,
            user_text=chosen["label"],
            user_visible_action=False,
            idempotency_key=idempotency_key,
            request_id=request_id,
            deadline=deadline,
        )

    async def cancel_topic(
        self,
        *,
        read_model: SessionReadModel,
        topic_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Cancel an active binary discovery topic without locking future turns."""
        session = read_model.session
        topic_rec = self._operational_repo.get_topic(session.session_id, topic_id)
        if topic_rec is None:
            raise NotFoundError("TOPIC_NOT_FOUND", "error.version_conflict")
        state = dict(topic_rec.state)
        if state.get("status") == "cancelled":
            return {"topicId": topic_id, "status": "cancelled"}
        if state.get("status") != "clarifying":
            raise ConflictError("STALE_TOPIC", "error.version_conflict")
        now_iso = self._clock.utc_now()
        state["status"] = "cancelled"
        state["updatedAt"] = now_iso
        with self._db.write_tx() as conn:
            conn.execute(
                "INSERT INTO binary_discovery_topics(session_id, topic_id, state_json, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id, topic_id) DO UPDATE "
                "SET state_json = excluded.state_json, updated_at = excluded.updated_at",
                (session.session_id, topic_id, dump_json(state), now_iso),
            )
            append_audit_event_tx(
                conn,
                session.session_id,
                "binary_discovery_cancelled",
                {"topicId": topic_id, "idempotencyKey": idempotency_key},
                now_iso,
            )
        return {"topicId": topic_id, "status": "cancelled"}

    async def _commit_step(
        self,
        *,
        read_model: SessionReadModel,
        current_ver: Any,
        topic_id: str,
        initial_request: str,
        initial_turns: list[dict[str, Any]],
        prior_choices: list[dict[str, Any]],
        verified_initial_req: dict[str, Any] | None,
        step_res: Any,
        user_text: str,
        user_visible_action: bool,
        idempotency_key: str,
        request_id: str,
        deadline: float | None,
    ) -> dict[str, Any]:
        return await commit_binary_step_impl(
            db=self._db,
            proposal_pipeline=self._proposal_pipeline,
            binary_model=self._binary_pipeline.model if self._binary_pipeline else "",
            room_sync=self._room_sync,
            clock=self._clock,
            read_model=read_model,
            current_ver=current_ver,
            topic_id=topic_id,
            initial_request=initial_request,
            initial_turns=initial_turns,
            prior_choices=prior_choices,
            verified_initial_req=verified_initial_req,
            step_res=step_res,
            user_text=user_text,
            user_visible_action=user_visible_action,
            idempotency_key=idempotency_key,
            request_id=request_id,
            deadline=deadline,
        )
