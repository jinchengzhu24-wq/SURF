"""Open-question and intent-hypothesis feedback application service."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Literal

from server.application.sessions import (
    CoCreationAppSettings,
    RoomSyncWriter,
    SessionReadModelService,
    append_audit_event_tx,
    request_digest,
)
from server.core.clock import SYSTEM_CLOCK, Clock
from server.domain.errors import ConflictError, IdempotencyConflictError
from server.domain.memory import apply_question_feedback, resolve_intent_hypothesis
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ._shared import (
    check_completed_idempotency,
    require_active_session,
    require_browser_session,
    require_current_version_tx,
    stale_before_iso,
)


class IntentService:
    """Applies designer feedback on open questions and tentative intent hypotheses."""

    def __init__(
        self,
        db: SQLiteDatabase,
        sessions_repo: SessionsRepository,
        read_model: SessionReadModelService,
        *,
        operational_repo: OperationalRepository | None = None,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
    ) -> None:
        self._db = db
        self._sessions_repo = sessions_repo
        self._operational_repo = operational_repo or OperationalRepository(db)
        self._read_model = read_model
        self._room_sync = room_sync
        self._clock = clock
        self._settings = settings or CoCreationAppSettings()

    async def submit_question_feedback(
        self,
        session_id: str,
        question_id: str,
        access_token: str | None,
        *,
        action: Literal["ignore", "restore"],
        base_version_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Ignore or restore an open question in the current Stage DesignContext."""
        require_browser_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
        )
        sig = request_digest(
            {"questionId": question_id, "action": action, "baseVersionId": base_version_id}
        )
        if check_completed_idempotency(
            self._operational_repo, session_id, "question_feedback", idempotency_key, sig
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
            "question_feedback",
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
            raise ConflictError("QUESTION_FEEDBACK_IN_PROGRESS", "error.version_conflict")

        committed_in_tx = False
        try:
            now_iso = self._clock.utc_now()
            with self._db.write_tx() as conn:
                _, cur_ver = require_current_version_tx(
                    conn, session_id, base_version_id, clock=self._clock
                )
                next_ctx, changed = apply_question_feedback(
                    cur_ver.design_context,
                    question_id,
                    action,
                    stage_id=cur_ver.version_id,
                    changed_at=now_iso,
                )
                if not changed:
                    raise ConflictError("QUESTION_FEEDBACK_STALE", "error.version_conflict")
                conn.execute(
                    "UPDATE level_versions SET design_context_json = ? "
                    "WHERE session_id = ? AND version_id = ?",
                    (dump_json(next_ctx.to_wire_dict()), session_id, cur_ver.version_id),
                )
                append_audit_event_tx(
                    conn,
                    session_id,
                    "question_feedback_applied",
                    {
                        "questionId": question_id,
                        "action": action,
                        "versionId": cur_ver.version_id,
                        "idempotencyKey": idempotency_key,
                    },
                    now_iso,
                )
                self._operational_repo.complete_key_tx(
                    conn,
                    session_id,
                    "question_feedback",
                    idempotency_key,
                    response={"questionId": question_id, "action": action},
                )
            committed_in_tx = True
        except asyncio.CancelledError:
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "question_feedback", idempotency_key, status="failed"
                )
            raise
        except Exception:
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "question_feedback", idempotency_key, status="failed"
                )
            raise
        return await self._read_model.get_session_payload(session_id)

    async def submit_intent_feedback(
        self,
        session_id: str,
        hypothesis_id: str,
        access_token: str | None,
        *,
        action: Literal["confirm", "reject", "revise"],
        base_version_id: str,
        idempotency_key: str,
        candidate_text: str | None = None,
        source_turn_id: str | None = None,
        supersedes_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Confirm, reject, or revise a displayed tentative intent hypothesis."""
        require_browser_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
        )
        sig = request_digest(
            {
                "hypothesisId": hypothesis_id,
                "action": action,
                "baseVersionId": base_version_id,
                "candidateText": str(candidate_text or "").strip(),
                "sourceTurnId": source_turn_id,
            }
        )
        if check_completed_idempotency(
            self._operational_repo, session_id, "intent_feedback", idempotency_key, sig
        ):
            return await self._read_model.get_session_payload(session_id)
        session = require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        claim = self._operational_repo.claim_key(
            session_id,
            "intent_feedback",
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
            raise ConflictError("INTENT_FEEDBACK_IN_PROGRESS", "error.version_conflict")

        committed_in_tx = False
        try:
            now_iso = self._clock.utc_now()
            with self._db.write_tx() as conn:
                cur_session, cur_ver = require_current_version_tx(
                    conn, session_id, base_version_id, clock=self._clock
                )
                next_ctx, resolved_id = resolve_intent_hypothesis(
                    cur_ver.design_context,
                    hypothesis_id,
                    action,
                    candidate_text=candidate_text,
                    supersedes_ids=supersedes_ids,
                    stage_id=cur_ver.version_id,
                    turn_id=source_turn_id,
                    display_language=session.language,
                )
                conn.execute(
                    "UPDATE level_versions SET design_context_json = ? "
                    "WHERE session_id = ? AND version_id = ?",
                    (dump_json(next_ctx.to_wire_dict()), session_id, cur_ver.version_id),
                )
                resolved_hyp = next(
                    (h for h in next_ctx.intent_hypotheses if h.id == resolved_id), None
                )
                resolved_statement = resolved_hyp.statement if resolved_hyp is not None else ""
                append_audit_event_tx(
                    conn,
                    session_id,
                    "intent_hypothesis_feedback_applied",
                    {
                        "hypothesisId": resolved_id,
                        "versionId": cur_ver.version_id,
                        "action": action,
                        "sourceTurnId": source_turn_id,
                        "idempotencyKey": idempotency_key,
                    },
                    now_iso,
                )
                if action in {"confirm", "revise"} and resolved_statement:
                    append_audit_event_tx(
                        conn,
                        session_id,
                        "intent_progress_rewrite",
                        {
                            "artifactType": "intent_hypothesis",
                            "artifactId": resolved_id,
                            "versionId": cur_ver.version_id,
                            "action": action,
                            "summaryText": resolved_statement[:500],
                            "sourceTurnId": source_turn_id,
                        },
                        now_iso,
                    )
                self._operational_repo.complete_key_tx(
                    conn,
                    session_id,
                    "intent_feedback",
                    idempotency_key,
                    response={"hypothesisId": resolved_id, "action": action},
                )
            committed_in_tx = True
        except asyncio.CancelledError:
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "intent_feedback", idempotency_key, status="failed"
                )
            raise
        except Exception:
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "intent_feedback", idempotency_key, status="failed"
                )
            raise
        return await self._read_model.get_session_payload(session_id)


__all__ = ["IntentService"]
