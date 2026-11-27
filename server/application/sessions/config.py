"""Shared settings, RoomSyncWriter protocol, solver wrappers, and TX helpers for sessions."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.grid import GridValidationError, map_fingerprint
from server.core.solver import SolverError, SolveResult, solve_min_steps
from server.domain.errors import DeadlineExceededError, ValidationError
from server.persistence.database import dump_json, load_json

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_SECRET = "development-only-change-before-deployment"
DEFAULT_PUBLIC_BASE_URL = "https://sokobanaidemo.top/cocreation"
DEFAULT_WEBGL_BASE_URL = "https://sokobanaidemo.top/game/"
COCREATION_DEADLINE_SECONDS = 1200
PLAY_TICKET_LIFETIME_SECONDS = 300
INTERRUPTED_AFTER_SECONDS = 1800
DEMO_TTL_SECONDS = 86400
DRAFT_REGEN_PENDING_TIMEOUT_SECONDS = 60
DRAFT_REGEN_CLAIMED_TIMEOUT_SECONDS = 120


def warn_if_default_token_secret(token_secret: str) -> None:
    """Emit a warning when CoCreationAppSettings is initialized with the default secret."""
    if token_secret == DEFAULT_TOKEN_SECRET:
        logger.warning(
            "CoCreationAppSettings is using the default TOKEN_SECRET; "
            "configure a strong secret before production deployment."
        )


@dataclass(frozen=True)
class CoCreationAppSettings:
    """Explicitly injected configuration for co-creation application services."""

    public_base_url: str = DEFAULT_PUBLIC_BASE_URL
    webgl_base_url: str = DEFAULT_WEBGL_BASE_URL
    token_secret: str = DEFAULT_TOKEN_SECRET
    cocreation_deadline_seconds: int = COCREATION_DEADLINE_SECONDS
    play_ticket_lifetime_seconds: int = PLAY_TICKET_LIFETIME_SECONDS
    interrupted_after_seconds: int = INTERRUPTED_AFTER_SECONDS
    demo_ttl_seconds: int = DEMO_TTL_SECONDS
    draft_regeneration_pending_timeout_seconds: int = DRAFT_REGEN_PENDING_TIMEOUT_SECONDS
    draft_regeneration_claimed_timeout_seconds: int = DRAFT_REGEN_CLAIMED_TIMEOUT_SECONDS
    binary_discovery_enabled: bool = True

    def __post_init__(self) -> None:
        warn_if_default_token_secret(self.token_secret)

    @property
    def token_secret_bytes(self) -> bytes:
        return self.token_secret.encode("utf-8")


class RoomSyncWriter(Protocol):
    """In-process transactional port for recording online room flow events & intentions."""

    def record_cocreation_event_tx(
        self,
        conn: sqlite3.Connection,
        *,
        match_id: str,
        player_slot: int,
        event_payload: Mapping[str, Any],
        now_iso: str | None = None,
        expires_at_iso: str | None = None,
    ) -> tuple[dict[str, Any], bool]: ...

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
    ) -> dict[str, Any]: ...


def solve_stage_sync(
    rows: Sequence[str],
    *,
    clock: Clock = SYSTEM_CLOCK,
    deadline_seconds: float | None = 5.0,
) -> SolveResult:
    """Validate and solve a 12x10 Stage synchronously, mapping errors to DomainError."""
    try:
        return solve_min_steps(rows, clock=clock, deadline_seconds=deadline_seconds)
    except GridValidationError as exc:
        raise ValidationError(exc.code, details=exc.details, message=str(exc)) from exc
    except SolverError as exc:
        if exc.code == "DEADLINE_EXCEEDED":
            raise DeadlineExceededError(
                exc.code, details={"searchedStates": exc.searched_states}, message=str(exc)
            ) from exc
        raise ValidationError(
            exc.code, details={"searchedStates": exc.searched_states}, message=str(exc)
        ) from exc


async def solve_stage_async(
    rows: Sequence[str],
    *,
    clock: Clock = SYSTEM_CLOCK,
    deadline_seconds: float | None = 5.0,
) -> SolveResult:
    """Offload CPU-bound Stage solving off the asyncio event loop."""
    return await asyncio.to_thread(
        solve_stage_sync, list(rows), clock=clock, deadline_seconds=deadline_seconds
    )


def append_audit_event_tx(
    conn: sqlite3.Connection,
    session_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    created_at: str,
) -> None:
    """Append an immutable audit event row inside the caller's write transaction."""
    conn.execute(
        "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, event_type, dump_json(dict(payload)), created_at),
    )


def build_disagreement_audit_summary(
    disagreement: Mapping[str, Any] | None,
    *,
    result_version_id: str | None = None,
) -> dict[str, Any] | None:
    """Return structured metadata for disagreement audits without duplicating long prose."""
    if not isinstance(disagreement, Mapping):
        return None
    summary: dict[str, Any] = {
        "disagreementId": disagreement.get("disagreementId") or disagreement.get("challengeId"),
        "challengeId": disagreement.get("challengeId"),
        "subject": disagreement.get("subject"),
        "status": disagreement.get("status"),
        "phase": disagreement.get("phase"),
        "resolution": disagreement.get("resolution"),
        "resultVersionId": result_version_id or disagreement.get("resultVersionId"),
    }
    return {
        k: (v[:500] if isinstance(v, str) else v)
        for k, v in summary.items()
        if v is not None
    }


def next_version_number_tx(conn: sqlite3.Connection, session_id: str) -> int:
    """Return the next sequential Stage version number for a session."""
    row = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_num "
        "FROM level_versions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["next_num"]) if row is not None else 1


def build_stage_sync_event(
    *,
    session_id: str,
    player_slot: int,
    version_id: str,
    version_number: int,
    source: str,
    rows: Sequence[str],
    diff: Sequence[Mapping[str, Any]],
    initial_draft_method: str,
    event_type: str = "stage",
    restored_from_version_id: str | None = None,
    restored_from_stage_number: int | None = None,
    replaced_version_id: str | None = None,
    replaced_stage_number: int | None = None,
    duration_seconds: int | None = None,
) -> dict[str, Any] | None:
    """Construct an in-process CoCreationFlowEvent-compatible dict."""
    source_map = {
        "human_edit": "manual",
        "ai_proposal": "ai",
        "llm_accepted": "ai",
        "restored": "restored",
    }
    if event_type == "stage" and source not in source_map:
        return None
    if event_type == "first_stage" and (source != "initial" or version_number != 1):
        return None
    event: dict[str, Any] = {
        "eventId": f"{event_type}:{version_id}",
        "eventType": event_type,
        "playerNumber": player_slot,
        "sessionId": session_id,
        "versionId": version_id,
        "stageNumber": version_number,
        "rows": list(rows),
        "diff": [dict(d) for d in diff],
    }
    if event_type == "stage":
        event["source"] = source_map[source]
        if source == "restored":
            if not (
                restored_from_version_id
                and restored_from_stage_number is not None
                and replaced_version_id
                and replaced_stage_number is not None
            ):
                return None
            event.update(
                {
                    "restoredFromVersionId": restored_from_version_id,
                    "restoredFromStageNumber": restored_from_stage_number,
                    "replacedVersionId": replaced_version_id,
                    "replacedStageNumber": replaced_stage_number,
                }
            )
    elif event_type == "first_stage":
        event["initialDraftMethod"] = initial_draft_method
    elif event_type == "final" and duration_seconds is not None:
        event["coCreationDurationSeconds"] = duration_seconds
    return event


def emit_room_cocreation_event_tx(
    conn: sqlite3.Connection,
    room_sync: RoomSyncWriter | None,
    *,
    match_id: str | None,
    player_slot: int | None,
    is_demo: bool,
    session_id: str,
    event: Mapping[str, Any] | None,
    now_iso: str,
) -> None:
    """Write a co-creation flow event into rooms in the active transaction when applicable."""
    if is_demo or not match_id or player_slot not in (1, 2) or event is None:
        return
    if room_sync is not None:
        room_sync.record_cocreation_event_tx(
            conn,
            match_id=match_id,
            player_slot=player_slot,
            event_payload={**dict(event), "sessionId": session_id},
            now_iso=now_iso,
        )
        return
    row = conn.execute(
        "SELECT room_code FROM online_rooms WHERE id = ?", (match_id,)
    ).fetchone()
    if row is None:
        return
    from server.persistence.repositories._room_events_ops import _RoomEventsMixin

    _RoomEventsMixin._append_event(
        conn,
        match_id,
        player_slot,
        str(event["eventType"]),
        {
            **dict(event),
            "matchId": match_id,
            "roomCode": str(row["room_code"]),
            "serverReceivedAt": now_iso,
        },
        now_iso,
    )


def emit_room_designer_intention_tx(
    conn: sqlite3.Connection,
    room_sync: RoomSyncWriter | None,
    *,
    match_id: str | None,
    player_slot: int | None,
    is_demo: bool,
    session_id: str,
    designer_intention: str,
    event_id: str,
    now_iso: str,
) -> None:
    """Synchronize final designer intention into online_rooms inside the active write_tx."""
    if is_demo or not match_id or player_slot not in (1, 2):
        return
    if room_sync is not None:
        room_sync.record_designer_intention_tx(
            conn,
            match_id=match_id,
            player_slot=player_slot,
            designer_intention=designer_intention,
            event_id=event_id,
            session_id=session_id,
            now_iso=now_iso,
        )
        return
    from server.persistence.repositories._room_events_ops import _RoomEventsMixin

    row = conn.execute("SELECT * FROM online_rooms WHERE id = ?", (match_id,)).fetchone()
    if row is None:
        return
    players: list[dict[str, Any]] = load_json(row["players_json"], [])
    player = next((p for p in players if p.get("playerNumber") == player_slot), None)
    if player is None or player.get("menuStartedFlow") is not True:
        return
    player["designerIntention"] = designer_intention
    conn.execute(
        "UPDATE online_rooms SET players_json = ?, updated_at = ? WHERE id = ?",
        (dump_json(players), now_iso, match_id),
    )
    _RoomEventsMixin._append_event(
        conn,
        match_id,
        player_slot,
        "message",
        {
            "eventId": event_id,
            "eventType": "message",
            "matchId": match_id,
            "roomCode": str(row["room_code"]),
            "playerNumber": player_slot,
            "sessionId": session_id,
            "studySessionId": str(player.get("studySessionId") or "").strip(),
            "message": designer_intention,
            "serverReceivedAt": now_iso,
        },
        now_iso,
    )


def build_turn_cards(guidance: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Build bounded CoCreationCard entries from turn guidance."""
    if not isinstance(guidance, Mapping):
        return []
    cards: list[dict[str, str]] = []
    dis = guidance.get("disagreement")
    if (
        isinstance(dis, Mapping)
        and dis.get("status") == "active"
        and dis.get("displayCard") is not False
    ):
        text = str(dis.get("coreDisagreement") or dis.get("nextQuestion") or "").strip()
        if text:
            cards.append({"type": "discussion", "text": text[:4000]})
    elif guidance.get("followUpQuestion"):
        q = str(guidance["followUpQuestion"]).strip()
        if q:
            cards.append({"type": "discussion", "text": q[:4000]})
    offer = guidance.get("proposalOffer")
    if isinstance(offer, Mapping) and offer.get("summary"):
        cards.append({"type": "proposal", "text": str(offer["summary"]).strip()[:4000]})
    pq = guidance.get("proposalQuestion")
    if isinstance(pq, Mapping) and pq.get("question"):
        cards.append({"type": "question", "text": str(pq["question"]).strip()[:4000]})
    if guidance.get("intentHypothesis"):
        cards.append({"type": "intent", "text": str(guidance["intentHypothesis"]).strip()[:4000]})
    for cue in guidance.get("uiCues") or ():
        if isinstance(cue, Mapping) and cue.get("type") and cue.get("text"):
            ctype = str(cue["type"]).strip().lower().replace("-", "_")[:40]
            ctext = str(cue["text"]).strip()[:4000]
            if ctype and ctext:
                cards.append({"type": ctype, "text": ctext})
    return cards[:8]


__all__ = [
    "COCREATION_DEADLINE_SECONDS",
    "CoCreationAppSettings",
    "DEFAULT_PUBLIC_BASE_URL",
    "DEFAULT_TOKEN_SECRET",
    "DEFAULT_WEBGL_BASE_URL",
    "DEMO_TTL_SECONDS",
    "RoomSyncWriter",
    "append_audit_event_tx",
    "build_disagreement_audit_summary",
    "build_stage_sync_event",
    "build_turn_cards",
    "emit_room_cocreation_event_tx",
    "emit_room_designer_intention_tx",
    "map_fingerprint",
    "next_version_number_tx",
    "solve_stage_async",
    "solve_stage_sync",
    "warn_if_default_token_secret",
]
