"""Play attempts lifecycle mixin for StageService."""

from __future__ import annotations

import math
import uuid
from typing import Any

from server.core.clock import Clock, parse_iso
from server.domain.errors import (
    AuthError,
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from server.persistence.database import SQLiteDatabase
from server.persistence.models import DesignSessionRecord
from server.persistence.repositories.sessions_repo import SessionsRepository

from ..config import CoCreationAppSettings, append_audit_event_tx
from ..lifecycle.tokens import (
    build_play_url,
    build_return_url,
    generate_random_token,
    hash_token,
    is_deadline_expired,
    token_matches,
    utc_plus_seconds_iso,
)


def _validate_play_metrics(
    *,
    duration_seconds: float,
    move_count: int,
    push_count: int,
    restart_count: int,
    minimum_moves: int,
    minimum_pushes: int,
) -> None:
    if not math.isfinite(duration_seconds) or duration_seconds < 0 or duration_seconds > 86400:
        raise ValidationError("INVALID_PLAY_METRICS", "error.validation_failed")
    if (
        min(move_count, push_count, restart_count) < 0
        or max(move_count, push_count, restart_count, minimum_moves, minimum_pushes) > 1_000_000
        or min(minimum_moves, minimum_pushes) < -1
        or push_count > move_count
    ):
        raise ValidationError("INVALID_PLAY_METRICS", "error.validation_failed")


class _PlayLifecycleMixin:
    _db: SQLiteDatabase
    _sessions_repo: SessionsRepository
    _clock: Clock
    _settings: CoCreationAppSettings

    def _require_browser_session(
        self, session_id: str, access_token: str | None
    ) -> DesignSessionRecord:
        session = self._sessions_repo.get_session(session_id)
        if session is None:
            raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
        if not token_matches(
            access_token,
            session.access_token_hash,
            secret_bytes=self._settings.token_secret_bytes,
        ):
            raise AuthError("SESSION_ACCESS_DENIED", "error.session_access_denied")
        return session

    def _require_active_session(
        self, session_id: str, access_token: str | None
    ) -> DesignSessionRecord:
        session = self._require_browser_session(session_id, access_token)
        if session.status != "active":
            raise ConflictError("SESSION_LOCKED", "error.session_locked")
        if not session.language_locked and session.language_locked_at is None:
            raise ConflictError("LANGUAGE_NOT_SELECTED", "error.language_not_selected")
        if is_deadline_expired(session.deadline_at, now=self._clock.utc_datetime()):
            raise ConflictError("SESSION_DEADLINE_EXPIRED", "error.session_deadline_expired")
        return session

    async def create_play_attempt(
        self,
        session_id: str,
        version_id: str,
        access_token: str | None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session = self._require_browser_session(session_id, access_token)
        version = self._sessions_repo.get_version(session_id, version_id)
        if version is None:
            raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")

        existing = self._sessions_repo.get_play_attempt_by_idempotency(session_id, idempotency_key)
        secret_bytes = self._settings.token_secret_bytes
        if existing is not None:
            if existing.version_id != version_id:
                raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
            if existing.ticket_used_at is not None:
                raise ConflictError("PLAY_TICKET_USED", "error.version_conflict")
            ticket = generate_random_token()
            self._sessions_repo.update_play_attempt(
                existing.attempt_id,
                ticket_hash=hash_token(ticket, secret_bytes=secret_bytes),
            )
            return {
                "attemptId": existing.attempt_id,
                "playUrl": build_play_url(
                    existing.attempt_id, ticket, webgl_base_url=self._settings.webgl_base_url
                ),
            }

        self._require_active_session(session_id, access_token)
        attempt_id = uuid.uuid4().hex
        ticket = generate_random_token()
        now_iso = self._clock.utc_now()
        expires_iso = utc_plus_seconds_iso(self._clock, self._settings.play_ticket_lifetime_seconds)
        self._sessions_repo.create_play_attempt(
            attempt_id=attempt_id,
            session_id=session_id,
            version_id=version_id,
            mode=session.mode,
            language=session.language,
            ticket_hash=hash_token(ticket, secret_bytes=secret_bytes),
            ticket_expires_at=expires_iso,
            issued_at=now_iso,
            minimum_moves=int(version.solver.get("solutionSteps", -1)),
            minimum_pushes=int(version.solver.get("solutionPushes", -1)),
            idempotency_key=idempotency_key,
        )
        with self._db.write_tx() as conn:
            append_audit_event_tx(
                conn,
                session_id,
                "play_attempt_issued",
                {"attemptId": attempt_id, "versionId": version_id},
                now_iso,
            )
        return {
            "attemptId": attempt_id,
            "playUrl": build_play_url(
                attempt_id, ticket, webgl_base_url=self._settings.webgl_base_url
            ),
        }

    async def bootstrap_play_attempt(self, attempt_id: str, ticket: str) -> dict[str, Any]:
        secret_bytes = self._settings.token_secret_bytes
        attempt = self._sessions_repo.get_play_attempt(attempt_id)
        if attempt is None or not token_matches(
            ticket, attempt.ticket_hash, secret_bytes=secret_bytes
        ):
            raise AuthError("INVALID_PLAY_TICKET", "error.session_access_denied")
        if attempt.ticket_used_at is not None:
            raise ConflictError("PLAY_TICKET_USED", "error.version_conflict")
        if parse_iso(attempt.ticket_expires_at) < self._clock.utc_datetime():
            raise ConflictError("PLAY_TICKET_EXPIRED", "error.version_conflict")

        session = self._sessions_repo.get_session(attempt.session_id)
        if session is None:
            raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
        if session.status != "active":
            raise ConflictError("SESSION_LOCKED", "error.session_locked")
        if is_deadline_expired(session.deadline_at, now=self._clock.utc_datetime()):
            raise ConflictError("SESSION_DEADLINE_EXPIRED", "error.session_deadline_expired")

        version = self._sessions_repo.get_version(attempt.session_id, attempt.version_id)
        if version is None:
            raise NotFoundError("VERSION_NOT_FOUND", "error.version_conflict")

        attempt_token = generate_random_token()
        now_iso = self._clock.utc_now()
        self._sessions_repo.update_play_attempt(
            attempt_id,
            ticket_used_at=now_iso,
            attempt_token_hash=hash_token(attempt_token, secret_bytes=secret_bytes),
            loaded_at=now_iso,
            status="loaded",
        )
        return {
            "attemptId": attempt_id,
            "sessionId": attempt.session_id,
            "versionId": attempt.version_id,
            "rows": list(version.rows),
            "initialDraftMethod": attempt.mode,
            "language": attempt.language,
            "attemptToken": attempt_token,
            "returnUrl": build_return_url(
                session.session_id,
                version.version_id,
                public_base_url=self._settings.public_base_url,
            ),
        }

    async def start_play_attempt(
        self, attempt_id: str, attempt_token: str, **metrics: Any
    ) -> dict[str, Any]:
        return await self._update_play_attempt(attempt_id, attempt_token, "started", **metrics)

    async def progress_play_attempt(
        self, attempt_id: str, attempt_token: str, **metrics: Any
    ) -> dict[str, Any]:
        return await self._update_play_attempt(attempt_id, attempt_token, "started", **metrics)

    async def complete_play_attempt(
        self, attempt_id: str, attempt_token: str, **metrics: Any
    ) -> dict[str, Any]:
        return await self._update_play_attempt(attempt_id, attempt_token, "completed", **metrics)

    async def abandon_play_attempt(
        self, attempt_id: str, attempt_token: str, **metrics: Any
    ) -> dict[str, Any]:
        return await self._update_play_attempt(attempt_id, attempt_token, "abandoned", **metrics)

    async def _update_play_attempt(
        self,
        attempt_id: str,
        attempt_token: str,
        requested_status: str,
        *,
        duration_seconds: float = 0.0,
        move_count: int = 0,
        push_count: int = 0,
        restart_count: int = 0,
        minimum_moves: int = -1,
        minimum_pushes: int = -1,
    ) -> dict[str, Any]:
        _validate_play_metrics(
            duration_seconds=duration_seconds,
            move_count=move_count,
            push_count=push_count,
            restart_count=restart_count,
            minimum_moves=minimum_moves,
            minimum_pushes=minimum_pushes,
        )
        attempt = self._sessions_repo.get_play_attempt(attempt_id)
        if attempt is None or not token_matches(
            attempt_token,
            attempt.attempt_token_hash,
            secret_bytes=self._settings.token_secret_bytes,
        ):
            raise AuthError("INVALID_ATTEMPT_TOKEN", "error.session_access_denied")
        if attempt.status in {"completed", "abandoned", "interrupted"}:
            return {"attemptId": attempt_id, "status": attempt.status}

        now_iso = self._clock.utc_now()
        terminal = requested_status in {"completed", "abandoned"}
        next_dur = float(duration_seconds) if terminal else max(
            attempt.duration_seconds, float(duration_seconds)
        )
        next_moves = int(move_count) if terminal else max(attempt.move_count, int(move_count))
        next_pushes = int(push_count) if terminal else max(attempt.push_count, int(push_count))
        next_restarts = int(restart_count) if terminal else max(
            attempt.restart_count, int(restart_count)
        )
        next_min_m = (
            attempt.minimum_moves
            if attempt.minimum_moves >= 0 and minimum_moves < 0
            else minimum_moves
        )
        next_min_p = (
            attempt.minimum_pushes
            if attempt.minimum_pushes >= 0 and minimum_pushes < 0
            else minimum_pushes
        )
        first_move_at = attempt.first_move_at or (now_iso if next_moves > 0 else None)

        self._sessions_repo.update_play_attempt(
            attempt_id,
            status=requested_status,
            first_move_at=first_move_at,
            finished_at=now_iso if terminal else None,
            duration_seconds=next_dur,
            move_count=next_moves,
            push_count=next_pushes,
            restart_count=next_restarts,
            minimum_moves=next_min_m,
            minimum_pushes=next_min_p,
        )
        with self._db.write_tx() as conn:
            append_audit_event_tx(
                conn,
                attempt.session_id,
                f"play_attempt_{requested_status}",
                {
                    "attemptId": attempt_id,
                    "versionId": attempt.version_id,
                    "moveCount": next_moves,
                    "pushCount": next_pushes,
                    "restartCount": next_restarts,
                    "durationSeconds": next_dur,
                },
                now_iso,
            )
            if terminal:
                append_audit_event_tx(
                    conn,
                    attempt.session_id,
                    "intent_evidence_recorded",
                    {
                        "evidenceId": f"ie_{uuid.uuid4().hex}",
                        "kind": f"play_{requested_status}",
                        "versionId": attempt.version_id,
                        "playAttemptId": attempt_id,
                        "observation": "Designer play evidence was recorded for the saved Stage.",
                        "details": {
                            "status": requested_status,
                            "durationSeconds": next_dur,
                            "moveCount": next_moves,
                            "pushCount": next_pushes,
                            "restartCount": next_restarts,
                            "minimumMoves": next_min_m,
                            "minimumPushes": next_min_p,
                        },
                        "authority": "evidence_only",
                    },
                    now_iso,
                )
        return {"attemptId": attempt_id, "status": requested_status}
