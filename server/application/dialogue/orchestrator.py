"""DB-idempotent Policy-2 message orchestrator (LLM and solver run outside write_tx)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from server.application.sessions import (
    CoCreationAppSettings,
    RoomSyncWriter,
    SessionReadModelService,
    request_digest,
)
from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.llm_client import LLMError
from server.domain.errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from server.llm.pipelines.understanding import UnderstandingPipeline
from server.persistence.database import SQLiteDatabase
from server.persistence.models import RevisionChallengeRecord, SessionReadModel
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ._shared import (
    ACTIVE_CHALLENGE_STATUSES,
    check_completed_idempotency,
    classify_failure,
    record_message_failure_audit_tx,
    require_active_session,
    require_browser_session,
    stale_before_iso,
)
from .handlers import (
    BinaryDiscoveryHandler,
    ChallengeHandler,
    OrdinaryChatHandler,
    RevisionCardHandler,
)

MessageAction = Literal[
    "send",
    "execute_revision",
    "alternative_revision",
    "challenge_revision",
    "continue_challenge",
]


class MessageOrchestrator:
    """Routes co-creation messages under DB-backed idempotency with zero in-memory locks."""

    def __init__(
        self,
        db: SQLiteDatabase,
        sessions_repo: SessionsRepository,
        operational_repo: OperationalRepository,
        read_model: SessionReadModelService,
        *,
        ordinary_handler: OrdinaryChatHandler,
        binary_handler: BinaryDiscoveryHandler,
        revision_handler: RevisionCardHandler,
        challenge_handler: ChallengeHandler,
        understanding_pipeline: UnderstandingPipeline | None = None,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
    ) -> None:
        self._db = db
        self._sessions_repo = sessions_repo
        self._operational_repo = operational_repo
        self._read_model = read_model
        self._ordinary_handler = ordinary_handler
        self._binary_handler = binary_handler
        self._revision_handler = revision_handler
        self._challenge_handler = challenge_handler
        self._understanding_pipeline = understanding_pipeline
        self._room_sync = room_sync
        self._clock = clock
        self._settings = settings or CoCreationAppSettings()

    async def send_message(
        self,
        session_id: str,
        access_token: str | None,
        *,
        base_version_id: str,
        idempotency_key: str,
        content: str = "",
        action: MessageAction = "send",
        request_proposal: bool = False,
        assistant_turn_id: str | None = None,
        proposal_id: str | None = None,
        challenge_id: str | None = None,
        challenge_choice: str | None = None,
        exit_challenge: bool = False,
        request_id: str = "",
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Execute one idempotent Policy-2 message turn without holding write locks on LLM."""
        require_browser_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
        )
        clean_text = str(content or "").strip()
        if len(clean_text) > 4000:
            raise ValidationError("MESSAGE_TOO_LONG", "error.validation_failed")
        if action == "send" and not clean_text and not exit_challenge:
            raise ValidationError("EMPTY_MESSAGE", "error.validation_failed")

        sig = request_digest(
            {
                "action": action,
                "baseVersionId": base_version_id,
                "content": clean_text,
                "requestProposal": bool(request_proposal),
                "assistantTurnId": assistant_turn_id,
                "proposalId": proposal_id,
                "challengeId": challenge_id,
                "challengeChoice": challenge_choice,
                "exitChallenge": bool(exit_challenge),
            }
        )
        if check_completed_idempotency(
            self._operational_repo, session_id, "message", idempotency_key, sig
        ):
            return await self._read_model.get_session_payload(session_id)
        require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        claim = self._operational_repo.claim_key(
            session_id,
            "message",
            idempotency_key,
            request_hash=sig,
            created_at=self._clock.utc_now(),
            stale_before_iso=stale_before_iso(self._clock, 150.0),
        )
        if claim.outcome == "conflict":
            raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
        if claim.outcome == "completed":
            return await self._read_model.get_session_payload(session_id)
        if claim.outcome == "in_progress":
            raise ConflictError("MESSAGE_IN_PROGRESS", "error.version_conflict")

        committed_in_tx = False
        try:
            read_model = self._sessions_repo.load_session_read_model(session_id)
            if read_model is None:
                raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
            if read_model.session.current_version_id != base_version_id:
                raise ConflictError("VERSION_CONFLICT", "error.version_conflict")

            outcome = await self._dispatch_inside_guard(
                read_model=read_model,
                clean_text=clean_text,
                action=action,
                request_proposal=request_proposal,
                assistant_turn_id=assistant_turn_id,
                proposal_id=proposal_id,
                challenge_id=challenge_id,
                challenge_choice=challenge_choice,
                exit_challenge=exit_challenge,
                idempotency_key=idempotency_key,
                request_id=request_id or idempotency_key,
                deadline=deadline,
            )
            committed_in_tx = bool(outcome.pop("_idempotencyCompleted", False))
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "message", idempotency_key, response=outcome
                )
        except asyncio.CancelledError:
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "message", idempotency_key, status="failed"
                )
            raise
        except Exception as exc:
            self._record_failure_safe(
                session_id=session_id,
                base_version_id=base_version_id,
                idempotency_key=idempotency_key,
                request_id=request_id or idempotency_key,
                exc=exc,
            )
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "message", idempotency_key, status="failed"
                )
            raise
        return await self._read_model.get_session_payload(session_id)

    async def _dispatch_inside_guard(
        self,
        *,
        read_model: SessionReadModel,
        clean_text: str,
        action: MessageAction,
        request_proposal: bool,
        assistant_turn_id: str | None,
        proposal_id: str | None,
        challenge_id: str | None,
        challenge_choice: str | None,
        exit_challenge: bool,
        idempotency_key: str,
        request_id: str,
        deadline: float | None,
    ) -> dict[str, Any]:
        """Enforce active topic/disagreement guards and dispatch outside write_tx."""
        session = read_model.session
        base_vid = session.current_version_id or ""
        current_ver = next((v for v in read_model.versions if v.version_id == base_vid), None)
        active_dis = (
            (current_ver.design_context or {}).get("activeDisagreement")
            if current_ver
            else None
        )
        has_active_dis = isinstance(active_dis, Mapping) and active_dis.get("status") == "active"
        active_challenge = _find_active_challenge(read_model, base_vid, challenge_id)
        latest_topic = self._operational_repo.get_latest_topic(session.session_id)
        topic_clarifying = (
            latest_topic is not None
            and latest_topic.state.get("versionId") == base_vid
            and latest_topic.state.get("status") == "clarifying"
        )

        if exit_challenge:
            if active_challenge is None:
                raise NotFoundError("CHALLENGE_NOT_FOUND", "error.version_conflict")
            return await self._challenge_handler.continue_challenge(
                read_model=read_model,
                challenge=active_challenge,
                user_text=clean_text,
                choice="cancel_challenge",
                exit_challenge=True,
                idempotency_key=idempotency_key,
                request_id=request_id,
                deadline=deadline,
                proposal_pipeline=self._revision_handler.proposal_pipeline,
            )

        if action == "execute_revision":
            if topic_clarifying:
                raise ConflictError("PROPOSAL_CHOICE_REQUIRED", "error.version_conflict")
            if has_active_dis:
                raise ConflictError("DISAGREEMENT_ACTIVE", "error.version_conflict")
            if not assistant_turn_id:
                raise ValidationError("MISSING_ASSISTANT_TURN_ID", "error.validation_failed")
            return await self._revision_handler.execute_revision(
                read_model=read_model,
                assistant_turn_id=assistant_turn_id,
                user_text=clean_text or "Apply proposed revision",
                idempotency_key=idempotency_key,
            )
        if action == "alternative_revision":
            if topic_clarifying:
                raise ConflictError("PROPOSAL_CHOICE_REQUIRED", "error.version_conflict")
            if has_active_dis:
                raise ConflictError("DISAGREEMENT_ACTIVE", "error.version_conflict")
            return await self._revision_handler.alternative_revision(
                read_model=read_model,
                user_text=clean_text,
                idempotency_key=idempotency_key,
                request_id=request_id,
                deadline=deadline,
            )
        if action == "challenge_revision":
            if topic_clarifying:
                raise ConflictError("PROPOSAL_CHOICE_REQUIRED", "error.version_conflict")
            if not proposal_id:
                raise ValidationError("MISSING_PROPOSAL_ID", "error.validation_failed")
            return await self._challenge_handler.start_challenge(
                read_model=read_model,
                proposal_id=proposal_id,
                user_text=clean_text or "Challenge this proposal",
                idempotency_key=idempotency_key,
            )
        if request_proposal:
            if has_active_dis or active_challenge is not None:
                raise ConflictError("DISAGREEMENT_ACTIVE", "error.version_conflict")
            if topic_clarifying:
                raise ConflictError("PROPOSAL_CHOICE_REQUIRED", "error.version_conflict")
            return await self._binary_handler.start_topic(
                read_model=read_model,
                user_text=clean_text,
                idempotency_key=idempotency_key,
                request_id=request_id,
                deadline=deadline,
            )
        if action == "continue_challenge" or active_challenge is not None:
            if active_challenge is None:
                raise NotFoundError("CHALLENGE_NOT_FOUND", "error.version_conflict")
            return await self._challenge_handler.continue_challenge(
                read_model=read_model,
                challenge=active_challenge,
                user_text=clean_text,
                choice=challenge_choice,
                idempotency_key=idempotency_key,
                request_id=request_id,
                deadline=deadline,
                proposal_pipeline=self._revision_handler.proposal_pipeline,
            )

        if topic_clarifying:
            raise ConflictError("PROPOSAL_CHOICE_REQUIRED", "error.version_conflict")

        active_human_edit = has_active_dis and active_dis.get("subject") == "human_edit"
        routing = None
        human_edit_engaged = False
        human_edit_recovery_needed = False

        if self._understanding_pipeline is not None:
            prev_user_msgs = [
                t.content
                for t in read_model.turns
                if t.version_id == base_vid and t.role == "user"
            ][-4:]
            has_pending = any(
                p.base_version_id == base_vid and p.status == "pending"
                for p in read_model.proposals
            )
            try:
                routing = await self._understanding_pipeline.route_turn(
                    clean_text,
                    previous_user_messages=prev_user_msgs,
                    active_disagreement=active_dis,
                    has_active_proposal=has_pending,
                    deadline=deadline,
                    request_id=request_id,
                )
            except (LLMError, TimeoutError):
                if not active_human_edit:
                    raise
                human_edit_recovery_needed = True

            if routing is not None:
                if active_human_edit:
                    eng = routing.payload.human_edit_engagement
                    if eng is None:
                        human_edit_recovery_needed = True
                    elif eng.decision == "engage":
                        human_edit_engaged = True
                if (
                    routing.payload.route_target == "binary_discovery"
                    and not routing.protected_change_requested
                    and not has_active_dis
                ):
                    return await self._binary_handler.start_topic(
                        read_model=read_model,
                        user_text=clean_text,
                        idempotency_key=idempotency_key,
                        request_id=request_id,
                        deadline=deadline,
                    )

        recovery_task: dict[str, Any] | None = None
        if human_edit_recovery_needed and isinstance(active_dis, Mapping):
            rec_id = f"rec_{uuid.uuid4().hex[:16]}"
            dis_id = str(active_dis.get("disagreementId") or base_vid)
            recovery_task = {
                "recoveryId": rec_id,
                "targetKind": "human_edit_engagement",
                "targetId": dis_id,
                "state": {
                    "component": "human_edit_engagement",
                    "baseVersionId": base_vid,
                    "disagreementId": dis_id,
                    "userMessage": clean_text,
                    "status": "retry_pending",
                    "attemptsUsed": 0,
                    "maxAttempts": 2,
                },
            }

        outcome = await self._ordinary_handler.handle(
            read_model=read_model,
            user_text=clean_text,
            idempotency_key=idempotency_key,
            request_id=request_id,
            routing=routing,
            deadline=deadline,
            human_edit_engaged=human_edit_engaged,
            human_edit_recovery_needed=human_edit_recovery_needed,
            active_disagreement=active_dis if active_human_edit else None,
            recovery_task=recovery_task,
            complete_idempotency_scope="message",
        )
        return outcome

    def _record_failure_safe(
        self,
        *,
        session_id: str,
        base_version_id: str,
        idempotency_key: str,
        request_id: str,
        exc: Exception,
    ) -> None:
        code, retryable, details = classify_failure(exc)
        with self._db.write_tx() as conn:
            record_message_failure_audit_tx(
                conn,
                session_id=session_id,
                base_version_id=base_version_id,
                message_key=idempotency_key,
                request_id=request_id,
                code=code,
                retryable=retryable,
                details=details,
                now_iso=self._clock.utc_now(),
                emit_reply_failure=isinstance(exc, (LLMError, TimeoutError)),
            )


def _find_active_challenge(
    read_model: SessionReadModel,
    base_version_id: str,
    challenge_id: str | None,
) -> RevisionChallengeRecord | None:
    for ch in reversed(read_model.challenges):
        if ch.base_version_id != base_version_id:
            continue
        if ch.status not in ACTIVE_CHALLENGE_STATUSES:
            continue
        if challenge_id and ch.challenge_id != challenge_id:
            continue
        return ch
    return None


__all__ = ["MessageAction", "MessageOrchestrator"]
