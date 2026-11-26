"""Component recovery retry service backed by OperationalRepository recovery tasks."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from server.application.sessions import (
    CoCreationAppSettings,
    RoomSyncWriter,
    SessionReadModelService,
    append_audit_event_tx,
    assess_version_impl,
)
from server.core.clock import SYSTEM_CLOCK, Clock, utc_now_iso
from server.domain.errors import ConflictError, NotFoundError
from server.llm.pipelines.chat import ChatPipeline
from server.persistence.database import SQLiteDatabase
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ._shared import require_active_session


class RecoveryService:
    """Claims and retries recoverable co-creation UI/LLM components safely outside write_tx."""

    def __init__(
        self,
        db: SQLiteDatabase,
        sessions_repo: SessionsRepository,
        operational_repo: OperationalRepository,
        read_model: SessionReadModelService,
        *,
        chat_pipeline: ChatPipeline | None = None,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
        lease_seconds: int = 30,
    ) -> None:
        self._db = db
        self._sessions_repo = sessions_repo
        self._operational_repo = operational_repo
        self._read_model = read_model
        self._chat_pipeline = chat_pipeline
        self._room_sync = room_sync
        self._clock = clock
        self._settings = settings or CoCreationAppSettings()
        self._lease_seconds = max(5, int(lease_seconds))

    async def retry_recovery(
        self,
        session_id: str,
        recovery_id: str,
        access_token: str | None,
        *,
        idempotency_key: str,
        request_id: str = "",
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Claim a recovery task lease, retry work OUTSIDE write_tx, and persist outcome."""
        session = require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        task = self._operational_repo.get_task(session_id, recovery_id)
        if task is None:
            raise NotFoundError("RECOVERY_TASK_NOT_FOUND", "error.version_conflict")
        state = dict(task.state)
        status = str(state.get("status") or "pending")
        if status == "completed":
            return await self._read_model.get_session_payload(session_id)
        max_attempts = int(state.get("maxAttempts") or 3)
        attempts_used = int(state.get("attemptsUsed") or 0)
        if attempts_used >= max_attempts:
            raise ConflictError("RECOVERY_ATTEMPTS_EXHAUSTED", "error.version_conflict")

        now_dt = self._clock.utc_datetime()
        now_iso = utc_now_iso(now_dt)
        lease_until = utc_now_iso(now_dt + timedelta(seconds=self._lease_seconds))
        claimed_state = {
            **state,
            "status": "claimed",
            "attemptsUsed": attempts_used + 1,
            "leaseUntil": lease_until,
            "lastAttemptKey": idempotency_key,
        }
        self._operational_repo.upsert_task(
            recovery_id,
            session_id,
            task.target_kind,
            task.target_id,
            claimed_state,
            updated_at=now_iso,
        )

        try:
            component = str(state.get("component") or task.target_kind)
            if component == "stage_opening":
                await assess_version_impl(
                    db=self._db,
                    sessions_repo=self._sessions_repo,
                    chat_pipeline=self._chat_pipeline,
                    room_sync=self._room_sync,
                    clock=self._clock,
                    session=session,
                    version_id=str(state.get("baseVersionId") or task.target_id),
                    idempotency_key=f"recovery:{recovery_id}:{idempotency_key}",
                    request_id=request_id or idempotency_key,
                    deadline=deadline,
                )
            else:
                raise ConflictError("UNSUPPORTED_RECOVERY_COMPONENT", "error.configuration")
        except Exception as exc:
            fail_iso = self._clock.utc_now()
            next_attempts = claimed_state["attemptsUsed"]
            failed_state = {
                **claimed_state,
                "status": "failed" if next_attempts >= max_attempts else "retry_pending",
                "leaseUntil": None,
                "lastError": str(exc)[:400],
            }
            self._operational_repo.upsert_task(
                recovery_id,
                session_id,
                task.target_kind,
                task.target_id,
                failed_state,
                updated_at=fail_iso,
            )
            with self._db.write_tx() as conn:
                append_audit_event_tx(
                    conn,
                    session_id,
                    "component_recovery",
                    {"recoveryId": recovery_id, "status": failed_state["status"]},
                    fail_iso,
                )
            raise

        done_iso = self._clock.utc_now()
        done_state = {**claimed_state, "status": "completed", "leaseUntil": None, "lastError": None}
        self._operational_repo.upsert_task(
            recovery_id,
            session_id,
            task.target_kind,
            task.target_id,
            done_state,
            updated_at=done_iso,
        )
        with self._db.write_tx() as conn:
            append_audit_event_tx(
                conn,
                session_id,
                "component_recovery",
                {"recoveryId": recovery_id, "status": "completed"},
                done_iso,
            )
        return await self._read_model.get_session_payload(session_id)


__all__ = ["RecoveryService"]
