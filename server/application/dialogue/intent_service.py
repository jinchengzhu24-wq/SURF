"""Open-question and intent-hypothesis feedback application service."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from server.application.sessions import (
    CoCreationAppSettings,
    SessionReadModelService,
    append_audit_event_tx,
)
from server.core.clock import SYSTEM_CLOCK, Clock
from server.domain.errors import ConflictError
from server.domain.memory import apply_question_feedback, resolve_intent_hypothesis
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.repositories.sessions_repo import SessionsRepository

from ._shared import require_active_session, require_current_version_tx


class IntentService:
    """Applies designer feedback on open questions and tentative intent hypotheses."""

    def __init__(
        self,
        db: SQLiteDatabase,
        sessions_repo: SessionsRepository,
        read_model: SessionReadModelService,
        *,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
    ) -> None:
        self._db = db
        self._sessions_repo = sessions_repo
        self._read_model = read_model
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
        require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        now_iso = self._clock.utc_now()
        with self._db.write_tx() as conn:
            _, cur_ver = require_current_version_tx(conn, session_id, base_version_id)
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
        supersedes_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Confirm, reject, or revise a displayed tentative intent hypothesis."""
        session = require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        now_iso = self._clock.utc_now()
        with self._db.write_tx() as conn:
            _, cur_ver = require_current_version_tx(conn, session_id, base_version_id)
            next_ctx, resolved_id = resolve_intent_hypothesis(
                cur_ver.design_context,
                hypothesis_id,
                action,
                candidate_text=candidate_text,
                supersedes_ids=supersedes_ids,
                stage_id=cur_ver.version_id,
                display_language=session.language,
            )
            conn.execute(
                "UPDATE level_versions SET design_context_json = ? "
                "WHERE session_id = ? AND version_id = ?",
                (dump_json(next_ctx.to_wire_dict()), session_id, cur_ver.version_id),
            )
            append_audit_event_tx(
                conn,
                session_id,
                "intent_hypothesis_feedback_applied",
                {
                    "hypothesisId": resolved_id,
                    "versionId": cur_ver.version_id,
                    "action": action,
                    "idempotencyKey": idempotency_key,
                },
                now_iso,
            )
        return await self._read_model.get_session_payload(session_id)


__all__ = ["IntentService"]
