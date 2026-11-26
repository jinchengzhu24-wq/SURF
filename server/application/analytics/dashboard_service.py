"""Clean read-model and deletion service for the Matchmaking Dashboard."""

from __future__ import annotations

import asyncio
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.settings import safe_compare_digest
from server.domain.errors import AuthError, DomainError, NotFoundError, ValidationError
from server.persistence.errors import NotFoundError as PersistenceNotFoundError
from server.persistence.repositories.rooms_repo import RoomsRepository

from ._projection import build_matchmaking_projection, clean_id

_UNSET = object()


class DashboardService:
    """Async read-model and authorized deletion service for Matchmaking Dashboard."""

    def __init__(
        self,
        rooms_repo: RoomsRepository,
        *,
        delete_password: str = "",
        clock: Clock | None = None,
        source_label: str = "sqlite:online_room_events",
    ) -> None:
        self._repo = rooms_repo
        self._delete_password = str(delete_password or "").strip()
        self._clock = clock if clock is not None else SYSTEM_CLOCK
        self._source_label = source_label

    def verify_delete_password(self, supplied_password: str | None) -> bool:
        """Verify X-Delete-Password with constant-time comparison."""
        if not self._delete_password:
            raise DomainError(
                "DELETE_PASSWORD_NOT_CONFIGURED",
                "error.configuration",
                retryable=False,
                message="Delete password is not configured",
            )
        if not safe_compare_digest(supplied_password or "", self._delete_password):
            raise AuthError(
                "INVALID_DELETE_PASSWORD",
                "error.session_access_denied",
                message="Incorrect delete password",
            )
        return True

    async def get_matchmaking_records(self) -> dict[str, Any]:
        """Project online room and co-creation flow events for the Matchmaking Dashboard."""
        records = await asyncio.to_thread(self._repo.list_all_match_events)
        return build_matchmaking_projection(
            records,
            generated_at=self._clock.utc_now(),
            source_label=self._source_label,
        )

    async def delete_online_match(
        self,
        match_id: str | None,
        *,
        supplied_password: str | None | object = _UNSET,
        verify_password: bool = True,
    ) -> dict[str, Any]:
        """Atomically delete an online match and its events after password verification."""
        if verify_password:
            pw = None if supplied_password is _UNSET else str(supplied_password or "")
            self.verify_delete_password(pw)
        clean_match_id = clean_id(match_id)
        if not clean_match_id:
            raise ValidationError(
                "MATCH_ID_REQUIRED",
                "error.validation_failed",
                message="matchId is required",
            )
        try:
            return await asyncio.to_thread(self._repo.delete_match, clean_match_id)
        except PersistenceNotFoundError as exc:
            raise NotFoundError(
                "MATCH_NOT_FOUND",
                "error.room_not_found",
                message=exc.message,
            ) from exc

    async def clear_matchmaking_records(
        self,
        *,
        supplied_password: str | None | object = _UNSET,
        verify_password: bool = True,
    ) -> dict[str, Any]:
        """Atomically clear all matchmaking rooms and flow events after password verification."""
        if verify_password:
            pw = None if supplied_password is _UNSET else str(supplied_password or "")
            self.verify_delete_password(pw)
        return await asyncio.to_thread(self._repo.clear_matches)
