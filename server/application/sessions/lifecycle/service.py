"""Native-async co-creation session lifecycle service (Decision 5B, TTL demo, ACID finalize)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Literal

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.generator import generate_demo_level
from server.core.grid import map_fingerprint
from server.domain.errors import (
    AuthError,
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import DesignSessionRecord
from server.persistence.repositories._mappers import row_to_session
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ..config import (
    CoCreationAppSettings,
    RoomSyncWriter,
    append_audit_event_tx,
    solve_stage_async,
)
from ..read_model import SessionReadModelService
from .finalize import finalize_session_impl, submit_intention_tx
from .language import update_language_impl
from .tokens import (
    build_launch_url,
    generate_random_token,
    generate_session_secret,
    hash_token,
    is_deadline_expired,
    token_matches,
    utc_plus_seconds_iso,
)


class SessionLifecycleService:
    """Manages creation, Decision 5B token auth, language lock, finalization, and intention."""

    def __init__(
        self,
        db: SQLiteDatabase,
        sessions_repo: SessionsRepository,
        operational_repo: OperationalRepository,
        read_model: SessionReadModelService,
        *,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
        demo_level_generator: Callable[..., Any] = generate_demo_level,
    ) -> None:
        self._db = db
        self._sessions_repo = sessions_repo
        self._operational_repo = operational_repo
        self._read_model = read_model
        self._room_sync = room_sync
        self._clock = clock
        self._settings = settings or CoCreationAppSettings()
        self._demo_level_generator = demo_level_generator

    def require_browser_session(
        self,
        session_id: str,
        access_token: str | None,
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

    def require_active_session(
        self,
        session_id: str,
        access_token: str | None,
    ) -> DesignSessionRecord:
        session = self.require_browser_session(session_id, access_token)
        if session.status != "active":
            raise ConflictError("SESSION_LOCKED", "error.session_locked")
        if not session.language_locked and session.language_locked_at is None:
            raise ConflictError("LANGUAGE_NOT_SELECTED", "error.language_not_selected")
        if is_deadline_expired(session.deadline_at, now=self._clock.utc_datetime()):
            raise ConflictError("SESSION_DEADLINE_EXPIRED", "error.session_deadline_expired")
        return session

    async def create_formal_session(
        self,
        *,
        rows: Sequence[str],
        initial_draft_method: Literal["partial_completion", "description_generation"],
        language: Literal["en", "zh-CN"] = "zh-CN",
        idempotency_key: str,
        match_id: str | None = None,
        player_number: int | None = None,
    ) -> dict[str, Any]:
        """Create a formal Unity-linked session with Decision 5B random hashed credentials."""
        clean_match = str(match_id or "").strip() or None
        if clean_match is not None and (
            isinstance(player_number, bool) or player_number not in (1, 2)
        ):
            raise ValidationError(
                "INVALID_MATCH_PLAYER", message="matchId requires playerNumber 1 or 2."
            )
        slot = player_number if clean_match is not None else None

        solved = await solve_stage_async(rows, clock=self._clock)
        draft_rows = list(solved.rows)
        draft_fp = map_fingerprint(draft_rows)
        now_iso = self._clock.utc_now()

        session_secret = generate_session_secret()
        access_token = generate_random_token()
        integration_token = generate_random_token()
        bootstrap_token = generate_random_token()
        secret_bytes = self._settings.token_secret_bytes

        with self._db.write_tx() as conn:
            existing_row = conn.execute(
                "SELECT * FROM design_sessions WHERE creation_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing_row is not None:
                existing = row_to_session(existing_row)
                if (
                    existing.is_demo
                    or existing.mode != initial_draft_method
                    or existing.match_id != clean_match
                    or existing.player_slot != slot
                    or (
                        existing.draft_rows is not None
                        and map_fingerprint(existing.draft_rows) != draft_fp
                    )
                ):
                    raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
                if existing.bootstrap_used_at is not None:
                    raise ConflictError("BOOTSTRAP_TOKEN_USED", "error.bootstrap_token_used")
                conn.execute(
                    "UPDATE design_sessions "
                    "SET bootstrap_token_hash = ?, integration_token_hash = ?, updated_at = ? "
                    "WHERE session_id = ?",
                    (
                        hash_token(bootstrap_token, secret_bytes=secret_bytes),
                        hash_token(integration_token, secret_bytes=secret_bytes),
                        now_iso,
                        existing.session_id,
                    ),
                )
                session_id = existing.session_id
            else:
                session_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO design_sessions("
                    "session_id, match_id, player_slot, mode, language, language_locked, status, "
                    "secret_hash, access_token_hash, integration_token_hash, bootstrap_token_hash, "
                    "creation_key, draft_rows_json, draft_validation_json, draft_generation, "
                    "is_demo, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                    (
                        session_id,
                        clean_match,
                        slot,
                        initial_draft_method,
                        language,
                        hash_token(session_secret, secret_bytes=secret_bytes),
                        hash_token(access_token, secret_bytes=secret_bytes),
                        hash_token(integration_token, secret_bytes=secret_bytes),
                        hash_token(bootstrap_token, secret_bytes=secret_bytes),
                        idempotency_key,
                        dump_json(draft_rows),
                        dump_json(solved.as_dict()),
                        now_iso,
                        now_iso,
                    ),
                )
                append_audit_event_tx(
                    conn,
                    session_id,
                    "session_created",
                    {
                        "initialDraftMethod": initial_draft_method,
                        "demoMode": False,
                        "initialRowsHash": draft_fp,
                    },
                    now_iso,
                )

        return {
            "sessionId": session_id,
            "launchUrl": build_launch_url(
                session_id,
                bootstrap_token,
                mode="unity",
                public_base_url=self._settings.public_base_url,
            ),
            "integrationToken": integration_token,
        }

    async def create_demo_session(
        self,
        *,
        language: Literal["en", "zh-CN"] = "zh-CN",
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Generate a demo level OUTSIDE write_tx and prune ONLY expired demo sessions."""
        generated = await asyncio.to_thread(self._demo_level_generator)
        solved = await solve_stage_async(list(generated.rows), clock=self._clock)
        draft_rows = list(solved.rows)
        now_iso = self._clock.utc_now()
        expires_iso = utc_plus_seconds_iso(self._clock, self._settings.demo_ttl_seconds)
        null_expiry_cutoff_iso = utc_plus_seconds_iso(
            self._clock, -self._settings.demo_ttl_seconds
        )

        session_secret = generate_session_secret()
        access_token = generate_random_token()
        integration_token = generate_random_token()
        bootstrap_token = generate_random_token()
        secret_bytes = self._settings.token_secret_bytes

        # Prune ONLY expired demo sessions (TTL-scoped); never wipe other active users' demos.
        self._sessions_repo.delete_expired_demo_sessions(
            now_iso,
            null_expiry_cutoff_iso=null_expiry_cutoff_iso,
        )

        with self._db.write_tx() as conn:
            existing_row = conn.execute(
                "SELECT * FROM design_sessions WHERE creation_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing_row is not None:
                existing = row_to_session(existing_row)
                if not existing.is_demo:
                    raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
                if existing.bootstrap_used_at is not None:
                    raise ConflictError("BOOTSTRAP_TOKEN_USED", "error.bootstrap_token_used")
                conn.execute(
                    "UPDATE design_sessions SET bootstrap_token_hash = ?, updated_at = ? "
                    "WHERE session_id = ?",
                    (
                        hash_token(bootstrap_token, secret_bytes=secret_bytes),
                        now_iso,
                        existing.session_id,
                    ),
                )
                session_id = existing.session_id
            else:
                session_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO design_sessions("
                    "session_id, mode, language, language_locked, status, "
                    "secret_hash, access_token_hash, integration_token_hash, bootstrap_token_hash, "
                    "creation_key, draft_rows_json, draft_validation_json, draft_generation, "
                    "is_demo, created_at, updated_at, expires_at"
                    ") VALUES ("
                    "?, 'algorithm_demo', ?, 0, 'active', ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?"
                    ")",
                    (
                        session_id,
                        language,
                        hash_token(session_secret, secret_bytes=secret_bytes),
                        hash_token(access_token, secret_bytes=secret_bytes),
                        hash_token(integration_token, secret_bytes=secret_bytes),
                        hash_token(bootstrap_token, secret_bytes=secret_bytes),
                        idempotency_key,
                        dump_json(draft_rows),
                        dump_json(solved.as_dict()),
                        now_iso,
                        now_iso,
                        expires_iso,
                    ),
                )
                append_audit_event_tx(
                    conn,
                    session_id,
                    "session_created",
                    {
                        "initialDraftMethod": "algorithm_demo",
                        "demoMode": True,
                        "initialRowsHash": map_fingerprint(draft_rows),
                        "generationSeed": getattr(generated, "seed", None),
                    },
                    now_iso,
                )

        return {
            "sessionId": session_id,
            "launchUrl": build_launch_url(
                session_id,
                bootstrap_token,
                mode="demo",
                public_base_url=self._settings.public_base_url,
            ),
        }

    async def authorize_browser_access(
        self,
        session_id: str,
        bootstrap_token: str,
    ) -> dict[str, Any]:
        """Exchange a one-time bootstrap token for a fresh random HttpOnly browser token."""
        secret_bytes = self._settings.token_secret_bytes
        access_token = generate_random_token()
        now_iso = self._clock.utc_now()
        with self._db.write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None or not token_matches(
                bootstrap_token, row["bootstrap_token_hash"], secret_bytes=secret_bytes
            ):
                raise AuthError("INVALID_BOOTSTRAP_TOKEN", "error.invalid_bootstrap_token")
            if row["bootstrap_used_at"] is not None:
                raise ConflictError("BOOTSTRAP_TOKEN_USED", "error.bootstrap_token_used")
            conn.execute(
                "UPDATE design_sessions "
                "SET bootstrap_used_at = ?, access_token_hash = ?, updated_at = ? "
                "WHERE session_id = ? AND bootstrap_used_at IS NULL",
                (now_iso, hash_token(access_token, secret_bytes=secret_bytes), now_iso, session_id),
            )
            append_audit_event_tx(
                conn,
                session_id,
                "browser_access_granted",
                {"deadlineAt": row["deadline_at"]},
                now_iso,
            )
        return {"sessionId": session_id, "accessToken": access_token}

    async def update_language(
        self,
        session_id: str,
        access_token: str | None,
        *,
        language: Literal["en", "zh-CN"],
    ) -> dict[str, Any]:
        """Lock session language, promote draft preview to Stage 1, and start formal deadline."""
        session = self.require_browser_session(session_id, access_token)
        await update_language_impl(
            db=self._db,
            room_sync=self._room_sync,
            clock=self._clock,
            settings=self._settings,
            session=session,
            language=language,
        )
        return await self._read_model.get_session_payload(session_id)

    async def finalize_session(
        self,
        session_id: str,
        access_token: str | None,
        *,
        base_version_id: str,
        idempotency_key: str,
        rows: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Finalize a Stage (with optional deadline draft auto-save) and sync `final` atomically."""
        session = self.require_browser_session(session_id, access_token)
        await finalize_session_impl(
            db=self._db,
            operational_repo=self._operational_repo,
            room_sync=self._room_sync,
            clock=self._clock,
            settings=self._settings,
            session=session,
            base_version_id=base_version_id,
            idempotency_key=idempotency_key,
            rows=rows,
        )
        return await self._read_model.get_session_payload(session_id)

    async def submit_intention(
        self,
        session_id: str,
        access_token: str | None,
        *,
        content: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Atomically persist final designer intention and sync into rooms in ONE write_tx."""
        self.require_browser_session(session_id, access_token)
        submit_intention_tx(
            db=self._db,
            room_sync=self._room_sync,
            clock=self._clock,
            session_id=session_id,
            content=content,
            idempotency_key=idempotency_key,
        )
        return await self._read_model.get_session_payload(session_id)


__all__ = ["SessionLifecycleService"]
