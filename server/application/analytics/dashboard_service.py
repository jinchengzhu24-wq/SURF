"""Clean read-model, research export, and deletion service for the Matchmaking Dashboard."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.settings import safe_compare_digest
from server.domain.errors import AuthError, DomainError, NotFoundError, ValidationError
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.errors import NotFoundError as PersistenceNotFoundError
from server.persistence.models import OnlineRoomEventRecord, StudyRecord
from server.persistence.repositories.rooms_repo import RoomsRepository
from server.persistence.repositories.study_repo import StudyRepository

from ._cocreation_projection import (
    SessionProjectionBundle,
    load_formal_session_bundles,
    redact_secret_keys,
    serialize_session_research_bundle,
)
from ._projection import build_matchmaking_projection, clean_id
from ._survey_analytics import (
    _index_formal_surveys,
    _match_surveys_for_match,
    build_survey_analytics,
    build_surveys_csv,
    normalize_survey_scope,
)

logger = logging.getLogger(__name__)
_UNSET = object()
_PLACEHOLDER_DELETE_PASSWORDS = frozenset({"replace-with-dashboard-delete-password"})


class DashboardService:
    """Async read-model, NDJSON research export, and authorized deletion service."""

    def __init__(
        self,
        rooms_repo: RoomsRepository,
        *,
        db: SQLiteDatabase | None = None,
        study_repo: StudyRepository | None = None,
        delete_password: str = "",
        clock: Clock | None = None,
        source_label: str = "sqlite:online_room_events",
        cocreation_deadline_seconds: int = 1200,
    ) -> None:
        self._repo = rooms_repo
        self._db: SQLiteDatabase | None = (
            db if db is not None else getattr(rooms_repo, "_db", None)
        )
        self._study_repo: StudyRepository | None = study_repo
        raw_password = str(delete_password or "").strip()
        if raw_password in _PLACEHOLDER_DELETE_PASSWORDS:
            logger.warning(
                "SECURITY WARNING: DASHBOARD_DELETE_PASSWORD is set to an example "
                "placeholder and will be treated as unconfigured (HTTP 503 on delete)."
            )
            self._delete_password = ""
        else:
            self._delete_password = raw_password
        self._clock = clock if clock is not None else SYSTEM_CLOCK
        self._source_label = source_label
        self._cocreation_deadline_seconds = max(60, int(cocreation_deadline_seconds))

    def verify_delete_password(self, supplied_password: str | None) -> bool:
        """Verify X-Delete-Password with constant-time comparison."""
        if not self._delete_password or self._delete_password in _PLACEHOLDER_DELETE_PASSWORDS:
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

    def _load_projection_inputs(
        self,
    ) -> tuple[list[OnlineRoomEventRecord], dict[tuple[str, int], SessionProjectionBundle]]:
        records = self._repo.list_all_match_events()
        if self._db is None or not records:
            return records, {}
        match_ids = {
            clean_id(rec.payload.get("matchId") or rec.match_id)
            for rec in records
            if clean_id(rec.payload.get("matchId") or rec.match_id)
        }
        bundles = load_formal_session_bundles(self._db, match_ids=match_ids)
        return records, bundles

    async def get_matchmaking_records(self) -> dict[str, Any]:
        """Project online room events and authoritative co-creation SQLite tables."""
        records, bundles, survey_records = await asyncio.to_thread(self._load_export_inputs)
        projection = build_matchmaking_projection(
            records,
            generated_at=self._clock.utc_now(),
            source_label=self._source_label,
            session_bundles=bundles,
            default_deadline_seconds=self._cocreation_deadline_seconds,
        )
        by_match_id, by_session_id = _index_formal_surveys(survey_records)
        for match_record in projection["matches"]:
            match_record["surveys"] = _match_surveys_for_match(
                match_record,
                by_match_id=by_match_id,
                by_session_id=by_session_id,
            )
        return projection

    def _load_export_inputs(
        self,
    ) -> tuple[
        list[OnlineRoomEventRecord],
        dict[tuple[str, int], SessionProjectionBundle],
        list[StudyRecord],
    ]:
        records, bundles = self._load_projection_inputs()
        surveys = (
            self._study_repo.query_records(record_type="survey")
            if self._study_repo is not None and records
            else []
        )
        return records, bundles, surveys

    def _load_survey_analytics_inputs(
        self,
    ) -> tuple[
        list[OnlineRoomEventRecord],
        dict[tuple[str, int], SessionProjectionBundle],
        list[StudyRecord],
    ]:
        records, bundles = self._load_projection_inputs()
        surveys = (
            self._study_repo.query_records(record_type="survey")
            if self._study_repo is not None
            else []
        )
        return records, bundles, surveys

    async def get_survey_analytics(
        self,
        *,
        scope: str = "active_matches",
        survey_id: str | None = None,
        survey_version: str | None = None,
    ) -> dict[str, Any]:
        """Return data-driven survey analytics filtered by scope, survey_id, and version."""
        normalized_scope = normalize_survey_scope(scope)
        generated_at = self._clock.utc_now()
        records, bundles, survey_records = await asyncio.to_thread(
            self._load_survey_analytics_inputs
        )
        projection = build_matchmaking_projection(
            records,
            generated_at=generated_at,
            source_label=self._source_label,
            session_bundles=bundles,
            default_deadline_seconds=self._cocreation_deadline_seconds,
        )
        return build_survey_analytics(
            matches=projection["matches"],
            survey_records=survey_records,
            generated_at=generated_at,
            scope=normalized_scope,
            survey_id=survey_id,
            survey_version=survey_version,
        )

    async def export_surveys_csv(
        self,
        *,
        scope: str = "active_matches",
        survey_id: str | None = None,
        survey_version: str | None = None,
    ) -> tuple[str, str]:
        """Export filtered survey responses as a UTF-8 BOM CRLF wide-table CSV."""
        normalized_scope = normalize_survey_scope(scope)
        generated_at = self._clock.utc_now()
        records, bundles, survey_records = await asyncio.to_thread(
            self._load_survey_analytics_inputs
        )
        projection = build_matchmaking_projection(
            records,
            generated_at=generated_at,
            source_label=self._source_label,
            session_bundles=bundles,
            default_deadline_seconds=self._cocreation_deadline_seconds,
        )
        return build_surveys_csv(
            matches=projection["matches"],
            survey_records=survey_records,
            scope=normalized_scope,
            survey_id=survey_id,
            survey_version=survey_version,
        )

    async def export_matchmaking_research_jsonl(self) -> AsyncIterator[str]:
        """Stream one sanitized NDJSON line per formal matchmaking record (demo excluded)."""
        exported_at = self._clock.utc_now()
        records, bundles, survey_records = await asyncio.to_thread(self._load_export_inputs)
        projection = build_matchmaking_projection(
            records,
            generated_at=exported_at,
            source_label=self._source_label,
            session_bundles=bundles,
            default_deadline_seconds=self._cocreation_deadline_seconds,
        )
        by_match_id, by_session_id = _index_formal_surveys(survey_records)

        events_by_match: dict[str, list[dict[str, Any]]] = {}
        for rec in records:
            mid = clean_id(rec.payload.get("matchId") or rec.match_id)
            if not mid:
                continue
            events_by_match.setdefault(mid, []).append(
                redact_secret_keys(
                    {
                        "id": rec.id,
                        "matchId": mid,
                        "playerSlot": rec.player_slot,
                        "seq": rec.seq,
                        "eventType": rec.event_type,
                        "payload": rec.payload,
                        "createdAt": rec.created_at,
                    }
                )
            )

        for match_record in projection["matches"]:
            mid = str(match_record["matchId"])
            cocreation_sessions: dict[str, Any] = {}
            for slot in (1, 2):
                bundle = bundles.get((mid, slot))
                if bundle is not None and not bundle.session.is_demo:
                    cocreation_sessions[str(slot)] = serialize_session_research_bundle(bundle)
            matched_surveys = _match_surveys_for_match(
                match_record,
                by_match_id=by_match_id,
                by_session_id=by_session_id,
            )

            line_obj = {
                "schemaVersion": 1,
                "exportedAt": exported_at,
                "match": redact_secret_keys(match_record),
                "roomEvents": events_by_match.get(mid, []),
                "coCreationSessions": cocreation_sessions,
                "surveys": matched_surveys,
            }
            yield dump_json(line_obj) + "\n"

    async def delete_online_match(
        self,
        match_id: str | None,
        *,
        supplied_password: str | None | object = _UNSET,
        verify_password: bool = True,
    ) -> dict[str, Any]:
        """Atomically delete an online match, its events, and linked formal design_sessions."""
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
        """Atomically clear all matchmaking rooms, events, and linked formal design_sessions."""
        if verify_password:
            pw = None if supplied_password is _UNSET else str(supplied_password or "")
            self.verify_delete_password(pw)
        return await asyncio.to_thread(self._repo.clear_matches)
