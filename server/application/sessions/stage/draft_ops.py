"""Draft regenerations lifecycle mixin for StageService."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from server.core.clock import Clock, parse_iso
from server.domain.errors import AuthError, ConflictError, IdempotencyConflictError, ValidationError
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import DesignSessionRecord, DraftRegenerationRecord
from server.persistence.repositories.sessions_repo import SessionsRepository

from ..config import CoCreationAppSettings, append_audit_event_tx, solve_stage_async
from ..lifecycle.tokens import token_matches
from ..read_model import SessionReadModelService


class _DraftRegenLifecycleMixin:
    _db: SQLiteDatabase
    _sessions_repo: SessionsRepository
    _read_model: SessionReadModelService
    _clock: Clock
    _settings: CoCreationAppSettings
    _demo_level_generator: Callable[..., Any]

    def _require_browser_session(
        self, session_id: str, access_token: str | None
    ) -> DesignSessionRecord: ...

    def _require_integration_session(
        self, session_id: str, integration_token: str | None
    ) -> DesignSessionRecord:
        session = self._sessions_repo.get_session(session_id)
        if session is None or not token_matches(
            integration_token,
            session.integration_token_hash,
            secret_bytes=self._settings.token_secret_bytes,
        ):
            raise AuthError("INVALID_INTEGRATION_TOKEN", "error.session_access_denied")
        return session

    def _require_draft_preview(self, session: DesignSessionRecord) -> None:
        if (
            session.status != "active"
            or session.language_locked
            or session.language_locked_at is not None
        ):
            raise ConflictError("SESSION_LOCKED", "error.session_locked")
        if session.current_version_id is not None or not session.draft_rows:
            raise ConflictError("DRAFT_PREVIEW_UNAVAILABLE", "error.version_conflict")

    def _expire_regen_if_needed(
        self, session_id: str, latest: DraftRegenerationRecord | None
    ) -> DraftRegenerationRecord | None:
        if latest is None or latest.status not in {"pending", "claimed"}:
            return latest
        lease_at = latest.requested_at if latest.status == "pending" else latest.updated_at
        age = (self._clock.utc_datetime() - parse_iso(lease_at)).total_seconds()
        limit = (
            self._settings.draft_regeneration_pending_timeout_seconds
            if latest.status == "pending"
            else self._settings.draft_regeneration_claimed_timeout_seconds
        )
        if age < limit:
            return latest
        failure_code = "unity_unavailable" if latest.status == "pending" else "generation_timed_out"
        now_iso = self._clock.utc_now()
        updated = self._sessions_repo.update_draft_regeneration(
            latest.request_id,
            status="timed_out",
            failure_code=failure_code,
            updated_at=now_iso,
        )
        with self._db.write_tx() as conn:
            append_audit_event_tx(
                conn,
                session_id,
                "draft_regeneration_timed_out",
                {
                    "requestId": latest.request_id,
                    "phase": latest.status,
                    "failureCode": failure_code,
                },
                now_iso,
            )
        return updated

    async def request_draft_regeneration(
        self,
        session_id: str,
        access_token: str | None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session = self._require_browser_session(session_id, access_token)
        self._require_draft_preview(session)
        latest = self._expire_regen_if_needed(
            session_id, self._sessions_repo.get_latest_draft_regeneration(session_id)
        )
        if latest is not None and latest.idempotency_key == idempotency_key:
            return await self._read_model.get_session_payload(session_id)
        if latest is not None and latest.status in {"pending", "claimed"}:
            raise ConflictError("DRAFT_REGENERATION_ACTIVE", "error.draft_regeneration_active")

        if session.is_demo:
            generated = await asyncio.to_thread(self._demo_level_generator)
            solved = await solve_stage_async(list(generated.rows), clock=self._clock)
            now_iso = self._clock.utc_now()
            req_id = uuid.uuid4().hex
            next_gen = session.draft_generation + 1
            self._sessions_repo.create_draft_regeneration(
                request_id=req_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
                status="completed",
                generation=next_gen,
                requested_at=now_iso,
                rows=list(solved.rows),
                validation=solved.as_dict(),
            )
            self._sessions_repo.update_session(
                session_id,
                draft_rows=list(solved.rows),
                draft_validation=solved.as_dict(),
                draft_generation=next_gen,
                updated_at=now_iso,
            )
            with self._db.write_tx() as conn:
                append_audit_event_tx(
                    conn,
                    session_id,
                    "draft_regeneration_completed",
                    {"requestId": req_id, "generation": next_gen, "mode": "algorithm_demo"},
                    now_iso,
                )
            return await self._read_model.get_session_payload(session_id)

        now_iso = self._clock.utc_now()
        req_id = uuid.uuid4().hex
        next_gen = session.draft_generation + 1
        self._sessions_repo.create_draft_regeneration(
            request_id=req_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            status="pending",
            generation=next_gen,
            requested_at=now_iso,
        )
        with self._db.write_tx() as conn:
            append_audit_event_tx(
                conn,
                session_id,
                "draft_regeneration_requested",
                {"requestId": req_id, "generation": next_gen, "mode": "unity"},
                now_iso,
            )
        return await self._read_model.get_session_payload(session_id)

    async def cancel_draft_regeneration(
        self,
        session_id: str,
        request_id: str,
        access_token: str | None,
    ) -> dict[str, Any]:
        session = self._require_browser_session(session_id, access_token)
        self._require_draft_preview(session)
        latest = self._expire_regen_if_needed(
            session_id, self._sessions_repo.get_latest_draft_regeneration(session_id)
        )
        if latest is None or latest.request_id != request_id:
            raise ConflictError("STALE_DRAFT_REGENERATION", "error.stale_draft_regeneration")
        if latest.status == "pending":
            now_iso = self._clock.utc_now()
            self._sessions_repo.update_draft_regeneration(
                request_id,
                status="cancelled",
                failure_code="unity_unavailable",
                updated_at=now_iso,
            )
            with self._db.write_tx() as conn:
                append_audit_event_tx(
                    conn,
                    session_id,
                    "draft_regeneration_cancelled",
                    {"requestId": request_id},
                    now_iso,
                )
        return await self._read_model.get_session_payload(session_id)

    async def claim_draft_regeneration(
        self,
        session_id: str,
        request_id: str,
        integration_token: str | None,
    ) -> dict[str, Any]:
        session = self._require_integration_session(session_id, integration_token)
        self._require_draft_preview(session)
        latest = self._expire_regen_if_needed(
            session_id, self._sessions_repo.get_latest_draft_regeneration(session_id)
        )
        if latest is None or latest.request_id != request_id:
            raise ConflictError("STALE_DRAFT_REGENERATION", "error.stale_draft_regeneration")
        if latest.status == "claimed":
            return {"sessionId": session_id, "requestId": request_id, "status": "claimed"}
        if latest.status != "pending":
            raise ConflictError("DRAFT_REGENERATION_NOT_PENDING", "error.stale_draft_regeneration")
        now_iso = self._clock.utc_now()
        self._sessions_repo.update_draft_regeneration(
            request_id, status="claimed", updated_at=now_iso
        )
        with self._db.write_tx() as conn:
            append_audit_event_tx(
                conn, session_id, "draft_regeneration_claimed", {"requestId": request_id}, now_iso
            )
        return {"sessionId": session_id, "requestId": request_id, "status": "claimed"}

    async def complete_draft_regeneration(
        self,
        session_id: str,
        request_id: str,
        integration_token: str | None,
        *,
        rows: Sequence[str],
    ) -> dict[str, Any]:
        session = self._require_integration_session(session_id, integration_token)
        self._require_draft_preview(session)
        latest = self._expire_regen_if_needed(
            session_id, self._sessions_repo.get_latest_draft_regeneration(session_id)
        )
        if latest is None or latest.request_id != request_id:
            raise ConflictError("STALE_DRAFT_REGENERATION", "error.stale_draft_regeneration")
        try:
            solved = await solve_stage_async(rows, clock=self._clock)
        except ValidationError:
            if latest.status == "claimed":
                now_iso = self._clock.utc_now()
                self._sessions_repo.update_draft_regeneration(
                    request_id,
                    status="failed",
                    failure_code="validation_failed",
                    updated_at=now_iso,
                )
            raise
        if latest.status == "completed":
            if latest.rows != list(solved.rows):
                raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
            return {"sessionId": session_id, "requestId": request_id, "status": "completed"}
        if latest.status != "claimed":
            raise ConflictError("DRAFT_REGENERATION_NOT_CLAIMED", "error.stale_draft_regeneration")

        now_iso = self._clock.utc_now()
        next_gen = session.draft_generation + 1
        with self._db.write_tx() as conn:
            conn.execute(
                "UPDATE draft_regenerations SET status = 'completed', generation = ?, "
                "rows_json = ?, validation_json = ?, failure_code = NULL, updated_at = ? "
                "WHERE request_id = ? AND status = 'claimed'",
                (
                    next_gen,
                    dump_json(list(solved.rows)),
                    dump_json(solved.as_dict()),
                    now_iso,
                    request_id,
                ),
            )
            conn.execute(
                "UPDATE design_sessions SET draft_rows_json = ?, draft_validation_json = ?, "
                "draft_generation = ?, updated_at = ? WHERE session_id = ?",
                (
                    dump_json(list(solved.rows)),
                    dump_json(solved.as_dict()),
                    next_gen,
                    now_iso,
                    session_id,
                ),
            )
            append_audit_event_tx(
                conn,
                session_id,
                "draft_regeneration_completed",
                {"requestId": request_id, "generation": next_gen, "mode": "unity"},
                now_iso,
            )
        return {"sessionId": session_id, "requestId": request_id, "status": "completed"}

    async def fail_draft_regeneration(
        self,
        session_id: str,
        request_id: str,
        integration_token: str | None,
        *,
        failure_code: str,
    ) -> dict[str, Any]:
        session = self._require_integration_session(session_id, integration_token)
        self._require_draft_preview(session)
        latest = self._expire_regen_if_needed(
            session_id, self._sessions_repo.get_latest_draft_regeneration(session_id)
        )
        if latest is None or latest.request_id != request_id:
            raise ConflictError("STALE_DRAFT_REGENERATION", "error.stale_draft_regeneration")
        if latest.status == "failed":
            return {"sessionId": session_id, "requestId": request_id, "status": "failed"}
        if latest.status not in {"pending", "claimed"}:
            raise ConflictError("DRAFT_REGENERATION_NOT_ACTIVE", "error.stale_draft_regeneration")
        now_iso = self._clock.utc_now()
        self._sessions_repo.update_draft_regeneration(
            request_id, status="failed", failure_code=failure_code, updated_at=now_iso
        )
        with self._db.write_tx() as conn:
            append_audit_event_tx(
                conn,
                session_id,
                "draft_regeneration_failed",
                {"requestId": request_id, "failureCode": failure_code, "phase": latest.status},
                now_iso,
            )
        return {"sessionId": session_id, "requestId": request_id, "status": "failed"}
