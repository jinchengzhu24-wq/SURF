"""DB-idempotent Policy-2 message orchestrator (LLM and solver run outside write_tx)."""

from __future__ import annotations

from typing import Any, Literal

from server.application.sessions import (
    CoCreationAppSettings,
    RoomSyncWriter,
    SessionReadModelService,
    request_digest,
)
from server.core.clock import SYSTEM_CLOCK, Clock
from server.domain.errors import (
    ConflictError,
    DomainError,
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
    record_message_failure_audit_tx,
    require_active_session,
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
        request_id: str = "",
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Execute one idempotent Policy-2 message turn without holding write locks on LLM."""
        require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        clean_text = str(content or "").strip()
        if len(clean_text) > 4000:
            raise ValidationError("MESSAGE_TOO_LONG", "error.validation_failed")
        if action == "send" and not clean_text:
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
            }
        )
        claim = self._operational_repo.claim_key(
            session_id,
            "message",
            idempotency_key,
            request_hash=sig,
            created_at=self._clock.utc_now(),
        )
        if claim.outcome == "conflict":
            raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
        if claim.outcome == "completed":
            return await self._read_model.get_session_payload(session_id)
        if claim.outcome == "in_progress":
            raise ConflictError("MESSAGE_IN_PROGRESS", "error.version_conflict")

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
                idempotency_key=idempotency_key,
                request_id=request_id or idempotency_key,
                deadline=deadline,
            )
            self._operational_repo.complete_key(
                session_id, "message", idempotency_key, response=outcome
            )
        except Exception as exc:
            self._record_failure_safe(
                session_id=session_id,
                base_version_id=base_version_id,
                idempotency_key=idempotency_key,
                request_id=request_id or idempotency_key,
                exc=exc,
            )
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
        idempotency_key: str,
        request_id: str,
        deadline: float | None,
    ) -> dict[str, Any]:
        """Check active challenge routing inside the idempotency guard and dispatch."""
        session = read_model.session
        base_vid = session.current_version_id or ""
        active_challenge = _find_active_challenge(read_model, base_vid, challenge_id)

        if action == "execute_revision":
            if not assistant_turn_id:
                raise ValidationError("MISSING_ASSISTANT_TURN_ID", "error.validation_failed")
            return await self._revision_handler.execute_revision(
                read_model=read_model,
                assistant_turn_id=assistant_turn_id,
                user_text=clean_text or "Apply proposed revision",
                idempotency_key=idempotency_key,
            )
        if action == "alternative_revision":
            return await self._revision_handler.alternative_revision(
                read_model=read_model,
                user_text=clean_text,
                idempotency_key=idempotency_key,
                request_id=request_id,
                deadline=deadline,
            )
        if action == "challenge_revision":
            if not proposal_id:
                raise ValidationError("MISSING_PROPOSAL_ID", "error.validation_failed")
            return await self._challenge_handler.start_challenge(
                read_model=read_model,
                proposal_id=proposal_id,
                user_text=clean_text or "Challenge this proposal",
                idempotency_key=idempotency_key,
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
            )

        if request_proposal:
            return await self._binary_handler.start_topic(
                read_model=read_model,
                user_text=clean_text,
                idempotency_key=idempotency_key,
                request_id=request_id,
                deadline=deadline,
            )

        routing = None
        if self._understanding_pipeline is not None:
            current_ver = next(
                (v for v in read_model.versions if v.version_id == base_vid), None
            )
            prev_user_msgs = [
                t.content
                for t in read_model.turns
                if t.version_id == base_vid and t.role == "user"
            ][-4:]
            active_dis = (
                (current_ver.design_context or {}).get("activeDisagreement")
                if current_ver
                else None
            )
            has_pending = any(
                p.base_version_id == base_vid and p.status == "pending"
                for p in read_model.proposals
            )
            routing = await self._understanding_pipeline.route_turn(
                clean_text,
                previous_user_messages=prev_user_msgs,
                active_disagreement=active_dis,
                has_active_proposal=has_pending,
                deadline=deadline,
                request_id=request_id,
            )
            if (
                routing.payload.route_target == "binary_discovery"
                and not routing.protected_change_requested
            ):
                return await self._binary_handler.start_topic(
                    read_model=read_model,
                    user_text=clean_text,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    deadline=deadline,
                )

        return await self._ordinary_handler.handle(
            read_model=read_model,
            user_text=clean_text,
            idempotency_key=idempotency_key,
            request_id=request_id,
            routing=routing,
            deadline=deadline,
        )

    def _record_failure_safe(
        self,
        *,
        session_id: str,
        base_version_id: str,
        idempotency_key: str,
        request_id: str,
        exc: Exception,
    ) -> None:
        code = exc.code if isinstance(exc, DomainError) else "INTERNAL_ERROR"
        details = dict(exc.details) if isinstance(exc, DomainError) else {}
        with self._db.write_tx() as conn:
            record_message_failure_audit_tx(
                conn,
                session_id=session_id,
                base_version_id=base_version_id,
                message_key=idempotency_key,
                request_id=request_id,
                code=code,
                details=details,
                now_iso=self._clock.utc_now(),
            )


def _find_active_challenge(
    read_model: SessionReadModel,
    base_version_id: str,
    challenge_id: str | None,
) -> RevisionChallengeRecord | None:
    for ch in reversed(read_model.challenges):
        if ch.base_version_id != base_version_id:
            continue
        if ch.status not in {"awaiting_reason", "awaiting_choice"}:
            continue
        if challenge_id and ch.challenge_id != challenge_id:
            continue
        return ch
    return None


__all__ = ["MessageOrchestrator"]
