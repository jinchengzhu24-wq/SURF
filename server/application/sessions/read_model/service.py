"""Batch-loaded, zero-N+1, zero-LLM session read model projection service."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.domain.errors import AuthError, NotFoundError
from server.persistence.models import (
    AuditEventRecord,
    DesignSessionRecord,
    SessionReadModel,
)
from server.persistence.repositories.dialogue_repo import DialogueRepository
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ..config import CoCreationAppSettings
from ..lifecycle.tokens import (
    is_deadline_expired,
    remaining_deadline_seconds,
    token_matches,
)
from .progress import (
    build_draft_preview,
    build_interaction_state,
    build_last_message_failure,
    build_progress_context,
    build_proposal_flow_state,
    serialize_play_attempt,
)
from .projections import build_proposal_state, public_turn_content
from .turns import (
    annotate_proposal_questions,
    build_public_guidance_map,
    index_audit_events,
    index_turn_metadata,
    project_translations,
)


class SessionReadModelService:
    """Projects a complete session read payload in one batch load without LLM calls."""

    def __init__(
        self,
        sessions_repo: SessionsRepository,
        dialogue_repo: DialogueRepository,
        operational_repo: OperationalRepository,
        *,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._dialogue_repo = dialogue_repo
        self._operational_repo = operational_repo
        self._clock = clock
        self._settings = settings or CoCreationAppSettings()

    async def get_session_payload(self, session_id: str) -> dict[str, Any]:
        """Load and project the full public session payload with zero N+1 queries."""
        read_model = self._sessions_repo.load_session_read_model(session_id)
        if read_model is None:
            raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
        events = self._dialogue_repo.list_events(session_id)
        topic_rec = self._operational_repo.get_latest_topic(session_id)
        recovery_tasks = self._operational_repo.list_tasks(session_id)
        topic_state = topic_rec.state if topic_rec is not None else None
        return self._project(read_model, events, topic_state, recovery_tasks)

    async def get_integration_status(
        self,
        session_id: str,
        integration_token: str | None,
    ) -> dict[str, Any]:
        """Return Unity-scoped integration status, draft regeneration, and final deliverables."""
        read_model = self._sessions_repo.load_session_read_model(session_id)
        if read_model is None:
            raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
        session = read_model.session
        if not token_matches(
            integration_token,
            session.integration_token_hash,
            secret_bytes=self._settings.token_secret_bytes,
        ):
            raise AuthError("INVALID_INTEGRATION_TOKEN", "error.session_access_denied")
        return self._project_integration_status(session, read_model)

    def _project_integration_status(
        self,
        session: DesignSessionRecord,
        read_model: SessionReadModel,
    ) -> dict[str, Any]:
        latest_regen = (
            read_model.draft_regenerations[0] if read_model.draft_regenerations else None
        )
        draft_regen_payload = None
        if session.current_version_id is None and latest_regen is not None:
            draft_regen_payload = {
                "requestId": latest_regen.request_id,
                "status": latest_regen.status,
                "generation": latest_regen.generation,
                "requestedAt": latest_regen.requested_at,
                "updatedAt": latest_regen.updated_at,
                "failureCode": latest_regen.failure_code,
            }
        final_rows = None
        intention_text = None
        if session.status == "completed" and session.finalized_version_id:
            final_ver = next(
                (v for v in read_model.versions if v.version_id == session.finalized_version_id),
                None,
            )
            final_rows = list(final_ver.rows) if final_ver is not None else None
            if isinstance(session.final_intention, Mapping):
                intention_text = session.final_intention.get("content")
        return {
            "sessionId": session.session_id,
            "status": session.status,
            "finalVersionId": session.finalized_version_id,
            "finalRows": final_rows,
            "designerIntention": intention_text,
            "draftRegeneration": draft_regen_payload,
        }

    def _project(
        self,
        read_model: SessionReadModel,
        events: list[AuditEventRecord],
        topic_state: Mapping[str, Any] | None,
        recovery_tasks: list[Any],
    ) -> dict[str, Any]:
        now = self._clock.utc_datetime()
        session = read_model.session
        deadline_expired = is_deadline_expired(session.deadline_at, now=now)
        remaining_sec = remaining_deadline_seconds(session.deadline_at, now=now)

        versions_by_id = {v.version_id: v for v in read_model.versions}
        stage_numbers = {v.version_id: v.version_number for v in read_model.versions}
        parent_by_version = {v.version_id: v.parent_version_id for v in read_model.versions}
        contexts_by_version = {
            v.version_id: dict(v.design_context or {}) for v in read_model.versions
        }
        attempts_by_version: dict[str, list[dict[str, Any]]] = {}
        for attempt in read_model.play_attempts:
            attempts_by_version.setdefault(attempt.version_id, []).append(
                serialize_play_attempt(attempt)
            )

        ev_idx = index_audit_events(events)
        turn_created_at = {t.turn_id: t.created_at for t in read_model.turns}
        raw_content_by_turn = {t.turn_id: t.content for t in read_model.turns}
        (
            translated_content_by_turn,
            translated_intent_statements,
            latest_intent_turn,
            latest_revision_turn,
        ) = index_turn_metadata(read_model.turns)

        public_content_by_turn = {
            t.turn_id: public_turn_content(t, stage_number=stage_numbers.get(t.version_id))
            for t in read_model.turns
        }
        public_guidance_by_turn = build_public_guidance_map(
            read_model.turns,
            session=session,
            deadline_expired=deadline_expired,
            versions_by_id=versions_by_id,
            contexts_by_version=contexts_by_version,
            latest_intent_turn=latest_intent_turn,
            public_content_by_turn=public_content_by_turn,
            translated_intent_statements=translated_intent_statements,
            raw_content_by_turn=raw_content_by_turn,
            translated_content_by_turn=translated_content_by_turn,
        )
        translations_by_turn = project_translations(
            read_model.turns, versions_by_id, public_guidance_by_turn, translated_intent_statements
        )
        annotate_proposal_questions(
            read_model.turns, public_guidance_by_turn, translations_by_turn, topic_state
        )

        return {
            "sessionId": session.session_id,
            "status": session.status,
            "language": session.language,
            "languageLocked": session.language_locked or session.language_locked_at is not None,
            "demoMode": session.is_demo,
            "initialDraftMethod": session.mode,
            "matchId": session.match_id,
            "playerNumber": session.player_slot,
            "currentVersionId": session.current_version_id,
            "lastMessageFailure": build_last_message_failure(read_model, events, topic_state),
            "interactionState": build_interaction_state(
                read_model, events, recovery_tasks, now=now
            ),
            "finalVersionId": session.finalized_version_id,
            "createdAt": session.created_at,
            "updatedAt": session.updated_at,
            "deadlineStartedAt": session.deadline_started_at,
            "deadlineAt": session.deadline_at,
            "deadlineExpired": deadline_expired,
            "remainingSeconds": remaining_sec,
            "draftPreview": build_draft_preview(read_model),
            "proposalFlowState": build_proposal_flow_state(read_model, topic_state),
            "versions": [
                {
                    "versionId": v.version_id,
                    "stageNumber": v.version_number,
                    "parentVersionId": v.parent_version_id,
                    "source": v.source,
                    "rows": list(v.rows),
                    "summary": v.summary,
                    "diff": list(v.diff),
                    "validation": dict(v.solver),
                    "createdAt": v.created_at,
                    "playAttempts": attempts_by_version.get(v.version_id, []),
                    "executionOutcome": ev_idx["execution_outcomes"].get(v.version_id),
                }
                for v in read_model.versions
            ],
            "progressContexts": [
                build_progress_context(
                    v,
                    context=contexts_by_version.get(v.version_id, {}),
                    session_language=session.language,
                    stage_numbers=stage_numbers,
                    parent_by_version=parent_by_version,
                    turn_created_at=turn_created_at,
                    intent_evidence_by_id=ev_idx["intent_evidence"],
                    latest_intent_feedback=ev_idx["intent_feedback"],
                    progress_rewrites=ev_idx["rewrites"],
                )
                for v in read_model.versions
            ],
            "turns": [
                {
                    "turnId": t.turn_id,
                    "sequence": t.sequence_number,
                    "role": t.role,
                    "content": public_content_by_turn[t.turn_id],
                    "language": t.language,
                    "versionId": t.version_id,
                    "requestId": t.request_id,
                    "requestProposal": t.role == "user"
                    and (t.request_id or "") in ev_idx["proposal_request_keys"],
                    "guidance": public_guidance_by_turn.get(t.turn_id, {}),
                    "proposalState": build_proposal_state(
                        t,
                        session_status=session.status,
                        current_version_id=session.current_version_id,
                        deadline_expired=deadline_expired,
                        versions_by_id=versions_by_id,
                        contexts_by_version=contexts_by_version,
                        latest_revision_turn_by_version=latest_revision_turn,
                    ),
                    "translations": translations_by_turn.get(t.turn_id, {}),
                    "createdAt": t.created_at,
                }
                for t in read_model.turns
            ],
            "proposals": [
                {
                    "proposalId": p.proposal_id,
                    "baseVersionId": p.base_version_id,
                    "proposedRows": list(p.proposal.get("proposedRows") or []),
                    "summary": str(p.proposal.get("summary") or ""),
                    "diff": list(p.proposal.get("diff") or []),
                    "validation": dict(p.proposal.get("validation") or {}),
                    "status": p.status,
                    "assistantTurnId": p.assistant_turn_id,
                    "createdAt": p.created_at,
                    "decidedAt": p.decided_at,
                }
                for p in read_model.proposals
            ],
            "intention": dict(session.final_intention) if session.final_intention else None,
        }


__all__ = ["SessionReadModelService"]
