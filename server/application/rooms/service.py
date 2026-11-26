"""Native-async online matchmaking room service backed by SQLite WAL."""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock, utc_now_iso
from server.core.grid import GridValidationError
from server.core.solver import (
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_MAX_STATES,
    SolverError,
    solve_min_steps,
)
from server.domain.errors import NotFoundError, ValidationError
from server.domain.rooms import PlayResultSummary, validate_challenge_rows
from server.persistence.errors import PersistenceError
from server.persistence.repositories.rooms_repo import RoomsRepository

from ._helpers import (
    authenticate_room_record,
    draft_events_match,
    generate_room_code,
    hash_room_token,
    map_persistence_error,
    normalize_ai_assistant_mode,
    normalize_room_code,
    normalize_study_session_id,
    to_public_room_payload,
    validate_draft_payload,
)
from ._sync_tx import record_cocreation_event_on_conn, record_designer_intention_on_conn


class OnlineRoomService:
    """Async 2-player matchmaking service with atomic in-process co-creation sync."""

    def __init__(
        self,
        rooms_repo: RoomsRepository,
        *,
        clock: Clock | None = None,
        room_ttl_seconds: float = 1800.0,
        solver_max_states: int = DEFAULT_MAX_STATES,
        solver_deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        self._repo = rooms_repo
        self._clock = clock if clock is not None else SYSTEM_CLOCK
        self._room_ttl_seconds = max(1.0, float(room_ttl_seconds))
        self._solver_max_states = max(1, int(solver_max_states))
        self._solver_deadline_seconds = max(0.05, float(solver_deadline_seconds))

    def _now_and_expiry(self) -> tuple[str, str]:
        now_dt = self._clock.utc_datetime()
        expires_dt = now_dt + timedelta(seconds=self._room_ttl_seconds)
        return utc_now_iso(now_dt), utc_now_iso(expires_dt)

    async def reap_expired_rooms(self) -> int:
        """Mark expired rooms and prune them from active storage."""
        now_iso = self._clock.utc_now()
        return await asyncio.to_thread(self._repo.reap_expired_rooms, now_iso)

    async def create_room(
        self,
        *,
        study_session_id: str = "",
        menu_started_flow: bool = False,
    ) -> dict[str, Any]:
        """Create a new 2-player matchmaking room and issue player 1's token once."""
        clean_study_id = normalize_study_session_id(study_session_id)
        await self.reap_expired_rooms()

        match_id = uuid.uuid4().hex
        raw_token = secrets.token_urlsafe(32)
        player1 = {
            "playerNumber": 1,
            "studySessionId": clean_study_id,
            "menuStartedFlow": bool(menu_started_flow),
            "tokenHash": hash_room_token(raw_token),
            "ready": False,
            "challengeRows": None,
            "aiAssistantMode": None,
            "designerIntention": None,
            "result": None,
            "pendingDraft": None,
            "left": False,
        }
        now_iso, expires_at_iso = self._now_and_expiry()
        try:
            record = await asyncio.to_thread(
                self._repo.allocate_and_create_room,
                match_id,
                generate_room_code,
                player1=player1,
                now_iso=now_iso,
                expires_at_iso=expires_at_iso,
            )
        except PersistenceError as exc:
            map_persistence_error(exc)
        return to_public_room_payload(record, player_number=1, raw_token=raw_token)

    async def join_room(
        self,
        *,
        room_code: str,
        study_session_id: str = "",
        menu_started_flow: bool = False,
    ) -> dict[str, Any]:
        """Join an existing room by 6-char code and issue player 2's token once."""
        clean_code = normalize_room_code(room_code)
        clean_study_id = normalize_study_session_id(study_session_id)
        await self.reap_expired_rooms()

        raw_token = secrets.token_urlsafe(32)
        player2 = {
            "playerNumber": 2,
            "studySessionId": clean_study_id,
            "menuStartedFlow": bool(menu_started_flow),
            "tokenHash": hash_room_token(raw_token),
            "ready": False,
            "challengeRows": None,
            "aiAssistantMode": None,
            "designerIntention": None,
            "result": None,
            "pendingDraft": None,
            "left": False,
        }
        now_iso, expires_at_iso = self._now_and_expiry()
        try:
            record = await asyncio.to_thread(
                self._repo.join_room,
                clean_code,
                player2=player2,
                now_iso=now_iso,
                expires_at_iso=expires_at_iso,
            )
        except PersistenceError as exc:
            map_persistence_error(exc)
        return to_public_room_payload(record, player_number=2, raw_token=raw_token)

    async def _require_authenticated_room(
        self,
        match_id: str,
        player_token: str | None,
    ) -> tuple[Any, dict[str, Any]]:
        await self.reap_expired_rooms()
        clean_match_id = str(match_id or "").strip()
        record = await asyncio.to_thread(self._repo.get_room, clean_match_id)
        if record is None:
            raise NotFoundError("ROOM_NOT_FOUND", "error.room_not_found")
        player = authenticate_room_record(record, player_token)
        return record, player

    async def get_room(self, match_id: str, player_token: str | None) -> dict[str, Any]:
        """Fetch current public room state for an authenticated player and refresh TTL."""
        record, player = await self._require_authenticated_room(match_id, player_token)
        now_iso, expires_at_iso = self._now_and_expiry()
        touched = await asyncio.to_thread(
            self._repo.touch_room,
            record.id,
            now_iso=now_iso,
            expires_at_iso=expires_at_iso,
        )
        active = touched if touched is not None else record
        return to_public_room_payload(active, player_number=int(player["playerNumber"]))

    async def set_ready(
        self,
        match_id: str,
        player_token: str | None,
        *,
        ready: bool,
    ) -> dict[str, Any]:
        """Update ready flag for the authenticated player."""
        record, player = await self._require_authenticated_room(match_id, player_token)
        player_slot = int(player["playerNumber"])
        now_iso, expires_at_iso = self._now_and_expiry()
        try:
            updated = await asyncio.to_thread(
                self._repo.set_ready,
                record.id,
                player_slot,
                ready=bool(ready),
                now_iso=now_iso,
                expires_at_iso=expires_at_iso,
            )
        except PersistenceError as exc:
            map_persistence_error(exc)
        return to_public_room_payload(updated, player_number=player_slot)

    async def record_draft(
        self,
        match_id: str,
        player_token: str | None,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Attach pending Starting Draft metadata until first_stage materializes it."""
        requested_draft, _ = validate_draft_payload(payload)
        record, player = await self._require_authenticated_room(match_id, player_token)
        player_slot = int(player["playerNumber"])
        now_iso, expires_at_iso = self._now_and_expiry()
        try:
            return await asyncio.to_thread(
                self._repo.record_draft,
                record.id,
                player_slot,
                requested_draft=requested_draft,
                now_iso=now_iso,
                expires_at_iso=expires_at_iso,
                match_fn=draft_events_match,
            )
        except PersistenceError as exc:
            map_persistence_error(exc)

    async def submit_challenge(
        self,
        match_id: str,
        player_token: str | None,
        *,
        rows: Sequence[str],
        ai_assistant_mode: str = "description_generation",
    ) -> dict[str, Any]:
        """Validate challenge structure and solvability OUTSIDE write tx, then persist."""
        validated_rows = validate_challenge_rows(rows)
        clean_mode = normalize_ai_assistant_mode(ai_assistant_mode)
        record, player = await self._require_authenticated_room(match_id, player_token)
        player_slot = int(player["playerNumber"])

        # Solve strictly outside any SQLite write transaction to avoid holding locks.
        try:
            await asyncio.to_thread(
                solve_min_steps,
                validated_rows,
                max_states=self._solver_max_states,
                deadline_seconds=self._solver_deadline_seconds,
                clock=self._clock,
            )
        except (GridValidationError, SolverError) as exc:
            raise ValidationError(
                exc.code,
                "error.validation_failed",
                message=str(exc),
            ) from exc

        now_iso, expires_at_iso = self._now_and_expiry()
        try:
            updated = await asyncio.to_thread(
                self._repo.submit_challenge,
                record.id,
                player_slot,
                rows=validated_rows,
                ai_assistant_mode=clean_mode,
                now_iso=now_iso,
                expires_at_iso=expires_at_iso,
            )
        except PersistenceError as exc:
            map_persistence_error(exc)
        return to_public_room_payload(updated, player_number=player_slot)

    async def submit_result(
        self,
        match_id: str,
        player_token: str | None,
        result: PlayResultSummary | Mapping[str, Any],
    ) -> dict[str, Any]:
        """Submit the opponent-play result summary for the authenticated player."""
        try:
            summary = (
                result
                if isinstance(result, PlayResultSummary)
                else PlayResultSummary.model_validate(result)
            )
        except Exception as exc:
            raise ValidationError(
                "INVALID_PLAY_RESULT",
                "error.validation_failed",
                message=str(exc),
            ) from exc

        record, player = await self._require_authenticated_room(match_id, player_token)
        player_slot = int(player["playerNumber"])
        now_iso, expires_at_iso = self._now_and_expiry()
        try:
            updated = await asyncio.to_thread(
                self._repo.submit_result,
                record.id,
                player_slot,
                result=summary.model_dump(by_alias=True),
                now_iso=now_iso,
                expires_at_iso=expires_at_iso,
            )
        except PersistenceError as exc:
            map_persistence_error(exc)
        return to_public_room_payload(updated, player_number=player_slot)

    async def leave_room(self, match_id: str, player_token: str | None) -> dict[str, Any]:
        """Mark the authenticated player as left and cancel incomplete matches."""
        record, player = await self._require_authenticated_room(match_id, player_token)
        player_slot = int(player["playerNumber"])
        now_iso, expires_at_iso = self._now_and_expiry()
        try:
            updated = await asyncio.to_thread(
                self._repo.leave_room,
                record.id,
                player_slot,
                now_iso=now_iso,
                expires_at_iso=expires_at_iso,
            )
        except PersistenceError as exc:
            map_persistence_error(exc)
        return to_public_room_payload(updated, player_number=player_slot)

    def record_cocreation_event_tx(
        self,
        conn: sqlite3.Connection,
        *,
        match_id: str,
        player_slot: int,
        event_payload: Mapping[str, Any],
        now_iso: str | None = None,
        expires_at_iso: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Append one idempotent co-creation flow event inside caller's open write tx."""
        default_now, default_exp = self._now_and_expiry()
        return record_cocreation_event_on_conn(
            conn,
            match_id=match_id,
            player_slot=player_slot,
            event_payload=event_payload,
            now_iso=now_iso or default_now,
            expires_at_iso=expires_at_iso or default_exp,
        )

    def record_designer_intention_tx(
        self,
        conn: sqlite3.Connection,
        *,
        match_id: str,
        player_slot: int,
        designer_intention: str,
        event_id: str | None = None,
        session_id: str | None = None,
        now_iso: str | None = None,
        expires_at_iso: str | None = None,
    ) -> dict[str, Any]:
        """Record final designer intention atomically inside caller's open write tx."""
        default_now, default_exp = self._now_and_expiry()
        return record_designer_intention_on_conn(
            conn,
            match_id=match_id,
            player_slot=player_slot,
            designer_intention=designer_intention,
            event_id=event_id,
            session_id=session_id,
            now_iso=now_iso or default_now,
            expires_at_iso=expires_at_iso or default_exp,
        )
