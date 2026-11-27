"""Authoritative read-time co-creation projection & sanitized research export from SQLite."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from server.application.sessions.config import build_turn_cards
from server.application.sessions.lifecycle.tokens import compute_cocreation_duration_seconds
from server.core.clock import parse_iso
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import (
    AuditEventRecord,
    BinaryDiscoveryTopicRecord,
    ChangeProposalRecord,
    ComponentRecoveryTaskRecord,
    ConversationTurnRecord,
    DesignSessionRecord,
    DraftRegenerationRecord,
    LevelVersionRecord,
    PlayAttemptRecord,
    RevisionChallengeRecord,
)
from server.persistence.repositories._mappers import (
    row_to_audit_event,
    row_to_challenge,
    row_to_draft_regeneration,
    row_to_play_attempt,
    row_to_proposal,
    row_to_recovery_task,
    row_to_session,
    row_to_topic,
    row_to_turn,
    row_to_version,
)

_SECRET_KEY_TOKENS: tuple[str, ...] = (
    "secret_hash",
    "secrethash",
    "token_hash",
    "tokenhash",
    "ticket_hash",
    "tickethash",
    "playertoken",
    "accesstoken",
    "integrationtoken",
    "bootstraptoken",
    "attempttoken",
    "deletepassword",
)

_VERSION_SOURCE_MAP: dict[str, str] = {
    "human_edit": "manual",
    "ai_proposal": "ai",
    "llm_accepted": "ai",
    "restored": "restored",
}


@dataclass(frozen=True, slots=True)
class SessionProjectionBundle:
    """Single-snapshot SQLite bundle for one formal co-creation design session."""

    session: DesignSessionRecord
    versions: list[LevelVersionRecord] = field(default_factory=list)
    turns: list[ConversationTurnRecord] = field(default_factory=list)
    proposals: list[ChangeProposalRecord] = field(default_factory=list)
    challenges: list[RevisionChallengeRecord] = field(default_factory=list)
    play_attempts: list[PlayAttemptRecord] = field(default_factory=list)
    draft_regenerations: list[DraftRegenerationRecord] = field(default_factory=list)
    topics: list[BinaryDiscoveryTopicRecord] = field(default_factory=list)
    recovery_tasks: list[ComponentRecoveryTaskRecord] = field(default_factory=list)
    audit_events: list[AuditEventRecord] = field(default_factory=list)


def redact_secret_keys(obj: Any) -> Any:
    """Recursively strip any token/secret/hash keys from exported structures."""
    if isinstance(obj, Mapping):
        cleaned: dict[str, Any] = {}
        for key, value in obj.items():
            low = str(key).lower().replace("-", "_")
            if any(tok in low for tok in _SECRET_KEY_TOKENS):
                continue
            cleaned[str(key)] = redact_secret_keys(value)
        return cleaned
    if isinstance(obj, list):
        return [redact_secret_keys(item) for item in obj]
    if isinstance(obj, tuple):
        return [redact_secret_keys(item) for item in obj]
    return obj


def _first_int(mapping: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        val = mapping.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    return None


def extract_solver_summary(solver: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return bounded public solver metrics without internal solution paths."""
    if not isinstance(solver, Mapping) or not solver:
        return None
    return {
        "solvable": bool(solver.get("solvable", True)),
        "minSteps": _first_int(
            solver, "minSteps", "solutionSteps", "minimumMoves", "minimumSteps"
        ),
        "minPushes": _first_int(solver, "minPushes", "solutionPushes", "minimumPushes"),
    }


def serialize_dashboard_play_attempt(
    attempt: PlayAttemptRecord,
    *,
    stage_number: int | None,
) -> dict[str, Any]:
    """Serialize one play_attempts row with all token/ticket hashes stripped."""
    return {
        "attemptId": attempt.attempt_id,
        "versionId": attempt.version_id,
        "stageNumber": stage_number,
        "mode": attempt.mode,
        "language": attempt.language,
        "status": attempt.status,
        "issuedAt": attempt.issued_at,
        "loadedAt": attempt.loaded_at,
        "firstMoveAt": attempt.first_move_at,
        "finishedAt": attempt.finished_at,
        "durationSeconds": attempt.duration_seconds,
        "moveCount": attempt.move_count,
        "pushCount": attempt.push_count,
        "restartCount": attempt.restart_count,
        "minimumMoves": attempt.minimum_moves,
        "minimumPushes": attempt.minimum_pushes,
    }


def serialize_dashboard_draft_regeneration(regen: DraftRegenerationRecord) -> dict[str, Any]:
    """Serialize one draft_regenerations row for Dashboard display."""
    return {
        "requestId": regen.request_id,
        "status": regen.status,
        "generation": regen.generation,
        "failureCode": regen.failure_code,
        "rows": list(regen.rows) if regen.rows is not None else None,
        "solver": extract_solver_summary(regen.validation),
        "requestedAt": regen.requested_at,
        "updatedAt": regen.updated_at,
    }


_SQL_IN_CHUNK_SIZE = 400


def _iter_chunks(
    items: Sequence[str], size: int = _SQL_IN_CHUNK_SIZE
) -> Iterator[list[str]]:
    """Yield slices of at most `size` bind parameters (well under SQLite's 999 limit)."""
    for idx in range(0, len(items), size):
        yield list(items[idx : idx + size])


def _query_in_chunks(
    conn: sqlite3.Connection,
    sql_template: str,
    items: Sequence[str],
    *,
    chunk_size: int = _SQL_IN_CHUNK_SIZE,
) -> list[sqlite3.Row]:
    """Execute a parameterized `IN ({placeholders})` query in bounded chunks."""
    rows: list[sqlite3.Row] = []
    for chunk in _iter_chunks(items, chunk_size):
        placeholders = ", ".join("?" for _ in chunk)
        rows.extend(
            conn.execute(sql_template.format(placeholders=placeholders), chunk).fetchall()
        )
    return rows


def load_formal_session_bundles(
    db: SQLiteDatabase,
    *,
    match_ids: set[str] | None = None,
) -> dict[tuple[str, int], SessionProjectionBundle]:
    """Batch-load all formal (non-demo) design sessions and child tables in ONE read snapshot."""
    if match_ids is not None and not match_ids:
        return {}

    with db.read_conn() as conn:
        if match_ids is not None:
            ordered_ids = sorted(match_ids)
            sess_rows = _query_in_chunks(
                conn,
                "SELECT *, rowid AS _sort_rowid FROM design_sessions "
                "WHERE is_demo = 0 AND match_id IN ({placeholders}) AND player_slot IN (1, 2) "
                "ORDER BY created_at ASC, rowid ASC",
                ordered_ids,
            )
            sess_rows.sort(
                key=lambda r: (str(r["created_at"] or ""), int(r["_sort_rowid"]))
            )
        else:
            sess_rows = conn.execute(
                "SELECT * FROM design_sessions "
                "WHERE is_demo = 0 AND match_id IS NOT NULL AND player_slot IN (1, 2) "
                "ORDER BY created_at ASC, rowid ASC"
            ).fetchall()

        if not sess_rows:
            return {}

        selected_by_key: dict[tuple[str, int], DesignSessionRecord] = {}
        for row in sess_rows:
            sess = row_to_session(row)
            if sess.match_id and sess.player_slot in (1, 2):
                selected_by_key[(sess.match_id, sess.player_slot)] = sess

        if not selected_by_key:
            return {}

        session_ids = list(dict.fromkeys(s.session_id for s in selected_by_key.values()))

        versions_map: dict[str, list[LevelVersionRecord]] = {sid: [] for sid in session_ids}
        for r in _query_in_chunks(
            conn,
            "SELECT * FROM level_versions WHERE session_id IN ({placeholders}) "
            "ORDER BY version_number ASC",
            session_ids,
        ):
            rec = row_to_version(r)
            versions_map[rec.session_id].append(rec)

        turns_map: dict[str, list[ConversationTurnRecord]] = {sid: [] for sid in session_ids}
        for r in _query_in_chunks(
            conn,
            "SELECT * FROM conversation_turns WHERE session_id IN ({placeholders}) "
            "ORDER BY sequence_number ASC",
            session_ids,
        ):
            rec = row_to_turn(r)
            turns_map[rec.session_id].append(rec)

        proposals_map: dict[str, list[ChangeProposalRecord]] = {sid: [] for sid in session_ids}
        for r in _query_in_chunks(
            conn,
            "SELECT * FROM change_proposals WHERE session_id IN ({placeholders}) "
            "ORDER BY created_at ASC, rowid ASC",
            session_ids,
        ):
            rec = row_to_proposal(r)
            proposals_map[rec.session_id].append(rec)

        challenges_map: dict[str, list[RevisionChallengeRecord]] = {sid: [] for sid in session_ids}
        for r in _query_in_chunks(
            conn,
            "SELECT * FROM revision_challenges WHERE session_id IN ({placeholders}) "
            "ORDER BY created_at ASC, updated_at ASC, rowid ASC",
            session_ids,
        ):
            rec = row_to_challenge(r)
            challenges_map[rec.session_id].append(rec)

        attempts_map: dict[str, list[PlayAttemptRecord]] = {sid: [] for sid in session_ids}
        for r in _query_in_chunks(
            conn,
            "SELECT * FROM play_attempts WHERE session_id IN ({placeholders}) "
            "ORDER BY issued_at ASC, rowid ASC",
            session_ids,
        ):
            rec = row_to_play_attempt(r)
            attempts_map[rec.session_id].append(rec)

        regens_map: dict[str, list[DraftRegenerationRecord]] = {sid: [] for sid in session_ids}
        for r in _query_in_chunks(
            conn,
            "SELECT * FROM draft_regenerations WHERE session_id IN ({placeholders}) "
            "ORDER BY requested_at ASC, rowid ASC",
            session_ids,
        ):
            rec = row_to_draft_regeneration(r)
            regens_map[rec.session_id].append(rec)

        topics_map: dict[str, list[BinaryDiscoveryTopicRecord]] = {sid: [] for sid in session_ids}
        for r in _query_in_chunks(
            conn,
            "SELECT * FROM binary_discovery_topics WHERE session_id IN ({placeholders}) "
            "ORDER BY updated_at ASC, rowid ASC",
            session_ids,
        ):
            rec = row_to_topic(r)
            topics_map[rec.session_id].append(rec)

        tasks_map: dict[str, list[ComponentRecoveryTaskRecord]] = {sid: [] for sid in session_ids}
        for r in _query_in_chunks(
            conn,
            "SELECT * FROM component_recovery_tasks WHERE session_id IN ({placeholders}) "
            "ORDER BY updated_at ASC, rowid ASC",
            session_ids,
        ):
            rec = row_to_recovery_task(r)
            tasks_map[rec.session_id].append(rec)

        audits_map: dict[str, list[AuditEventRecord]] = {sid: [] for sid in session_ids}
        for r in _query_in_chunks(
            conn,
            "SELECT * FROM audit_events WHERE session_id IN ({placeholders}) ORDER BY id ASC",
            session_ids,
        ):
            rec = row_to_audit_event(r)
            audits_map[rec.session_id].append(rec)

        bundles: dict[tuple[str, int], SessionProjectionBundle] = {}
        for key, sess in selected_by_key.items():
            sid = sess.session_id
            bundles[key] = SessionProjectionBundle(
                session=sess,
                versions=versions_map[sid],
                turns=turns_map[sid],
                proposals=proposals_map[sid],
                challenges=challenges_map[sid],
                play_attempts=attempts_map[sid],
                draft_regenerations=regens_map[sid],
                topics=topics_map[sid],
                recovery_tasks=tasks_map[sid],
                audit_events=audits_map[sid],
            )
        return bundles


def _make_entry(
    *,
    entry_id: str,
    kind: str,
    text: str,
    occurred_at: str,
    label: str = "",
    language: str | None = None,
) -> dict[str, Any] | None:
    clean_text = str(text or "").strip()
    if not clean_text:
        return None
    entry: dict[str, Any] = {
        "entryId": entry_id[:160],
        "kind": kind,
        "text": clean_text[:12000],
        "label": label[:160],
        "occurredAt": occurred_at,
    }
    if language in {"en", "zh-CN"}:
        entry["language"] = language
    return entry


def _append_entry(entries: list[dict[str, Any]], entry: dict[str, Any] | None) -> None:
    if entry is not None:
        entries.append(entry)


def _format_disagreement_card(disagreement: Mapping[str, Any]) -> str:
    parts = [
        str(disagreement.get(field_name) or "").strip()
        for field_name in ("userPosition", "aiPosition", "coreDisagreement", "nextQuestion")
    ]
    return "\n".join(part for part in parts if part)


def _format_question_card_text(pq: Mapping[str, Any]) -> str:
    lines: list[str] = []
    q_text = str(pq.get("question") or "").strip()
    if q_text:
        lines.append(q_text)
    note = str(pq.get("note") or "").strip()
    if note:
        lines.append(note)
    choices = pq.get("choices") or []
    if isinstance(choices, list) and choices:
        opt_parts: list[str] = []
        for idx, ch in enumerate(choices):
            if isinstance(ch, Mapping):
                letter = str(ch.get("letter") or ("A" if idx == 0 else "B"))
                label = str(ch.get("label") or "").strip()
                if label:
                    opt_parts.append(f"{letter}: {label}")
        if opt_parts:
            lines.append(" | ".join(opt_parts))
    return "\n".join(lines)


def _pair_user_turns(
    turns: list[ConversationTurnRecord],
) -> dict[str, ConversationTurnRecord]:
    """Map each assistant turn_id to its paired preceding user turn when present."""
    paired: dict[str, ConversationTurnRecord] = {}
    used_users: set[str] = set()
    by_req_ver: dict[tuple[str, str], list[ConversationTurnRecord]] = {}
    for turn in turns:
        if turn.role == "user" and turn.request_id:
            by_req_ver.setdefault((turn.version_id, turn.request_id), []).append(turn)

    prev_turn: ConversationTurnRecord | None = None
    for turn in turns:
        if turn.role == "assistant":
            matched: ConversationTurnRecord | None = None
            if turn.request_id:
                candidates = by_req_ver.get((turn.version_id, turn.request_id), [])
                for cand in candidates:
                    if (
                        cand.turn_id not in used_users
                        and cand.sequence_number < turn.sequence_number
                    ):
                        matched = cand
                        break
            if (
                matched is None
                and prev_turn is not None
                and prev_turn.role == "user"
                and prev_turn.version_id == turn.version_id
                and prev_turn.turn_id not in used_users
                and prev_turn.request_id == turn.request_id
            ):
                matched = prev_turn
            if matched is not None:
                paired[turn.turn_id] = matched
                used_users.add(matched.turn_id)
        prev_turn = turn
    return paired


def _compute_final_duration(
    session: DesignSessionRecord,
    *,
    fallback_duration: float | None,
    default_deadline_seconds: int,
) -> float | None:
    if session.deadline_started_at and session.deadline_at and session.finalized_at:
        try:
            budget = max(
                1,
                int(
                    (
                        parse_iso(session.deadline_at) - parse_iso(session.deadline_started_at)
                    ).total_seconds()
                ),
            )
            return compute_cocreation_duration_seconds(
                session.deadline_at,
                session.finalized_at,
                cocreation_deadline_seconds=budget,
            )
        except ValueError:
            pass
    if fallback_duration is not None:
        return fallback_duration
    if session.deadline_at and session.finalized_at:
        try:
            return compute_cocreation_duration_seconds(
                session.deadline_at,
                session.finalized_at,
                cocreation_deadline_seconds=default_deadline_seconds,
            )
        except ValueError:
            pass
    return None


def _build_node_event(
    *,
    match_id: str,
    room_code: str,
    player_number: int,
    study_session_id: str,
    session_id: str,
    version_id: str | None,
    stage_number: int | None,
    node_id: str,
    node_type: str,
    node_status: str,
    entries: list[dict[str, Any]],
    started_at: str,
    updated_at: str,
    node_parent_id: str | None = None,
    opening_assistant_turn_id: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "eventId": f"node:{node_id}",
        "eventType": "node",
        "matchId": match_id,
        "roomCode": room_code,
        "playerNumber": player_number,
        "studySessionId": study_session_id,
        "sessionId": session_id,
        "serverReceivedAt": started_at,
        "nodeUpdatedAt": updated_at or started_at,
        "nodeId": node_id,
        "nodeType": node_type,
        "nodeStatus": node_status,
        "nodeEntries": entries[-96:],
    }
    if version_id:
        event["versionId"] = version_id
    if stage_number is not None:
        event["stageNumber"] = stage_number
    if node_parent_id:
        event["nodeParentId"] = node_parent_id
    if opening_assistant_turn_id:
        event["openingAssistantTurnId"] = opening_assistant_turn_id
    return event


def _project_manual_edit_and_llm_challenges(
    *,
    bundle: SessionProjectionBundle,
    versions_by_id: dict[str, LevelVersionRecord],
    stage_num: dict[str, int],
    turns_by_id: dict[str, ConversationTurnRecord],
    user_by_asst: dict[str, ConversationTurnRecord],
    claimed_turns: set[str],
    base_meta: dict[str, Any],
) -> dict[str, list[tuple[str, int, dict[str, Any]]]]:
    """Build manual_edit_review and separate llm_challenge nodes per version."""
    nodes_by_vid: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}

    reviewed_by_vid: dict[str, AuditEventRecord] = {}
    dis_start_human_by_vid: dict[str, AuditEventRecord] = {}
    dis_ack_by_vid: dict[str, AuditEventRecord] = {}
    dis_resolved_human_by_vid: dict[str, AuditEventRecord] = {}

    for ev in bundle.audit_events:
        p = ev.payload
        vid = str(p.get("versionId") or "").strip()
        if not vid:
            continue
        if ev.event_type == "human_edit_reviewed":
            reviewed_by_vid[vid] = ev
        elif ev.event_type == "disagreement_started":
            dis = p.get("disagreement") or {}
            if p.get("subject") == "human_edit" or (
                isinstance(dis, Mapping) and dis.get("subject") == "human_edit"
            ):
                dis_start_human_by_vid[vid] = ev
        elif ev.event_type == "disagreement_acknowledged":
            dis = p.get("disagreement") or {}
            if (
                p.get("subject") == "human_edit"
                or not isinstance(dis, Mapping)
                or dis.get("subject", "human_edit") == "human_edit"
            ):
                dis_ack_by_vid[vid] = ev
        elif ev.event_type == "disagreement_resolved":
            dis = p.get("disagreement") or {}
            if p.get("subject") == "human_edit" or (
                isinstance(dis, Mapping) and dis.get("subject") == "human_edit"
            ):
                dis_resolved_human_by_vid[vid] = ev

    recovery_by_vid: dict[str, list[ComponentRecoveryTaskRecord]] = {}
    for task in bundle.recovery_tasks:
        t_vid = str(task.state.get("baseVersionId") or task.state.get("versionId") or "").strip()
        if t_vid:
            recovery_by_vid.setdefault(t_vid, []).append(task)

    for ver in bundle.versions:
        vid = ver.version_id
        rev_audit = reviewed_by_vid.get(vid)
        if rev_audit is None:
            continue
        payload = rev_audit.payload
        opening_tid = str(payload.get("openingTurnId") or "").strip()
        review_tid = str(payload.get("reviewTurnId") or "").strip()

        review_entries: list[dict[str, Any]] = []
        _append_entry(
            review_entries,
            _make_entry(
                entry_id=f"manual-save:{vid}",
                kind="player_action",
                text=str(ver.summary or "Designer saved an edited stage"),
                label="Player action",
                occurred_at=ver.created_at,
                language=bundle.session.language,
            ),
        )
        if ver.diff:
            _append_entry(
                review_entries,
                _make_entry(
                    entry_id=f"verified-diff:{vid}",
                    kind="map_diff",
                    text=dump_json(ver.diff),
                    label="Verified map change",
                    occurred_at=ver.created_at,
                ),
            )
        opening_turn = turns_by_id.get(opening_tid)
        if opening_turn is not None:
            claimed_turns.add(opening_turn.turn_id)
            _append_entry(
                review_entries,
                _make_entry(
                    entry_id=f"llm:{opening_turn.turn_id}",
                    kind="llm_message",
                    text=opening_turn.content,
                    label="LLM",
                    occurred_at=opening_turn.created_at,
                    language=opening_turn.language,
                ),
            )
        review_turn = turns_by_id.get(review_tid)
        if review_turn is not None:
            claimed_turns.add(review_turn.turn_id)
            _append_entry(
                review_entries,
                _make_entry(
                    entry_id=f"llm:{review_turn.turn_id}",
                    kind="llm_message",
                    text=review_turn.content,
                    label="LLM",
                    occurred_at=review_turn.created_at,
                    language=review_turn.language,
                ),
            )

        manual_node_id = f"manual-review:{vid}"
        manual_node = _build_node_event(
            **base_meta,
            version_id=vid,
            stage_number=ver.version_number,
            node_id=manual_node_id,
            node_type="manual_edit_review",
            node_status="reviewed",
            entries=review_entries,
            started_at=ver.created_at,
            updated_at=rev_audit.created_at,
        )
        nodes_by_vid.setdefault(vid, []).append((ver.created_at, 10, manual_node))

        # Check whether a human-edit disagreement occurred -> emit separate llm_challenge node!
        # Prefer full LET'S DISCUSS card from review_turn.guidance["disagreement"] first; fall
        # back to audit payload (which carries structured summaries on current writes or legacy
        # full cards on historical rows).
        turn_dis = (
            (review_turn.guidance or {}).get("disagreement")
            if review_turn is not None
            else None
        )
        audit_dis = payload.get("disagreement")
        if not isinstance(audit_dis, Mapping) and vid in dis_start_human_by_vid:
            audit_dis = dis_start_human_by_vid[vid].payload.get("disagreement")

        dis: Mapping[str, Any] | None = None
        if isinstance(turn_dis, Mapping) and turn_dis.get("subject") == "human_edit":
            dis = turn_dis
        elif isinstance(audit_dis, Mapping):
            dis = audit_dis
        if not isinstance(dis, Mapping):
            continue

        dis_id = str(
            dis.get("disagreementId")
            or dis.get("challengeId")
            or payload.get("disagreementId")
            or (
                audit_dis.get("disagreementId")
                if isinstance(audit_dis, Mapping)
                else ""
            )
            or vid
        ).strip()
        llm_entries: list[dict[str, Any]] = []
        card_text = (
            _format_disagreement_card(dis)
            or (
                _format_disagreement_card(audit_dis)
                if isinstance(audit_dis, Mapping)
                else ""
            )
            or (review_turn.content if review_turn is not None else "Manual edit disagreement")
        )
        _append_entry(
            llm_entries,
            _make_entry(
                entry_id=f"llm-challenge-card:{dis_id}",
                kind="card",
                text=card_text,
                label="LET'S DISCUSS",
                occurred_at=rev_audit.created_at,
                language=bundle.session.language,
            ),
        )

        # Claim engaging follow-up turns (direct ordinary chat ack or component recovery ack)
        ack_audit = dis_ack_by_vid.get(vid)
        res_audit = dis_resolved_human_by_vid.get(vid)
        engaged_turn_ids: list[str] = []
        if ack_audit is not None and ack_audit.payload.get("turnId"):
            engaged_turn_ids.append(str(ack_audit.payload["turnId"]))
        for task in recovery_by_vid.get(vid, []):
            dtid = str(task.state.get("deliveredAssistantTurnId") or "").strip()
            if dtid and dtid not in engaged_turn_ids:
                engaged_turn_ids.append(dtid)
        for turn in bundle.turns:
            if turn.version_id != vid or turn.role != "assistant":
                continue
            if turn.turn_id in {opening_tid, review_tid} or turn.turn_id in engaged_turn_ids:
                continue
            t_dis = (turn.guidance or {}).get("disagreement")
            if isinstance(t_dis, Mapping) and t_dis.get("subject") == "human_edit":
                engaged_turn_ids.append(turn.turn_id)

        for asst_tid in engaged_turn_ids:
            asst_t = turns_by_id.get(asst_tid)
            if asst_t is None or asst_t.turn_id in claimed_turns:
                continue
            user_t = user_by_asst.get(asst_t.turn_id)
            if user_t is not None and user_t.turn_id not in claimed_turns:
                claimed_turns.add(user_t.turn_id)
                _append_entry(
                    llm_entries,
                    _make_entry(
                        entry_id=f"player:{user_t.turn_id}",
                        kind="player_message",
                        text=user_t.content,
                        label="Player",
                        occurred_at=user_t.created_at,
                        language=user_t.language,
                    ),
                )
            claimed_turns.add(asst_t.turn_id)
            _append_entry(
                llm_entries,
                _make_entry(
                    entry_id=f"llm:{asst_t.turn_id}",
                    kind="llm_message",
                    text=asst_t.content,
                    label="LLM",
                    occurred_at=asst_t.created_at,
                    language=asst_t.language,
                ),
            )

        if ack_audit is not None:
            llm_status = "acknowledged"
            updated_at = ack_audit.created_at
            _append_entry(
                llm_entries,
                _make_entry(
                    entry_id=f"llm-challenge-status:{dis_id}:ack",
                    kind="status",
                    text="Responded · proposal block released",
                    label="Discussion status",
                    occurred_at=ack_audit.created_at,
                ),
            )
        elif res_audit is not None:
            llm_status = "manual_reedit"
            updated_at = res_audit.created_at
            res_dis = res_audit.payload.get("disagreement") or {}
            res_vid = str(
                res_audit.payload.get("resultVersionId")
                or (res_dis.get("resultVersionId") if isinstance(res_dis, Mapping) else "")
                or ""
            ).strip()
            res_stage = stage_num.get(res_vid)
            status_text = (
                f"Ended through a new manual edit (Stage {res_stage})"
                if res_stage is not None
                else "Ended through a new manual edit"
            )
            _append_entry(
                llm_entries,
                _make_entry(
                    entry_id=f"llm-challenge-status:{dis_id}:reedit",
                    kind="status",
                    text=status_text,
                    label="Discussion status",
                    occurred_at=res_audit.created_at,
                ),
            )
        else:
            llm_status = "in_progress"
            updated_at = rev_audit.created_at

        llm_node = _build_node_event(
            **base_meta,
            version_id=vid,
            stage_number=ver.version_number,
            node_id=f"llm-challenge:{dis_id}",
            node_type="llm_challenge",
            node_status=llm_status,
            node_parent_id=manual_node_id,
            entries=llm_entries,
            started_at=rev_audit.created_at,
            updated_at=updated_at,
        )
        nodes_by_vid.setdefault(vid, []).append((rev_audit.created_at, 15, llm_node))

    return nodes_by_vid


def _append_proposal_execution_and_decision(
    *,
    entries: list[dict[str, Any]],
    exec_turn: ConversationTurnRecord | None,
    exec_user: ConversationTurnRecord | None,
    proposal: ChangeProposalRecord | None,
    stage_num: dict[str, int],
    claimed_turns: set[str],
    skip_exec_turns: bool = False,
) -> tuple[str | None, str | None]:
    """Append execute_revision turn + verified map_diff + accept/reject decision onto entries."""
    latest_ts: str | None = None
    if not skip_exec_turns:
        if exec_user is not None and exec_user.turn_id not in claimed_turns:
            claimed_turns.add(exec_user.turn_id)
            _append_entry(
                entries,
                _make_entry(
                    entry_id=f"player:{exec_user.turn_id}",
                    kind="player_action",
                    text=exec_user.content,
                    label="Player action",
                    occurred_at=exec_user.created_at,
                    language=exec_user.language,
                ),
            )
            latest_ts = exec_user.created_at
        if exec_turn is not None and exec_turn.turn_id not in claimed_turns:
            claimed_turns.add(exec_turn.turn_id)
            _append_entry(
                entries,
                _make_entry(
                    entry_id=f"llm:{exec_turn.turn_id}",
                    kind="llm_message",
                    text=exec_turn.content,
                    label="LLM",
                    occurred_at=exec_turn.created_at,
                    language=exec_turn.language,
                ),
            )
            latest_ts = exec_turn.created_at

    if proposal is None:
        return None, latest_ts

    diff_data = proposal.proposal.get("diff")
    val_data = proposal.proposal.get("validation")
    if diff_data is not None or val_data is not None:
        _append_entry(
            entries,
            _make_entry(
                entry_id=f"proposal-verified:{proposal.proposal_id}",
                kind="map_diff",
                text=dump_json({"diff": diff_data or [], "validation": val_data or {}}),
                label="Verified proposal result",
                occurred_at=proposal.created_at,
            ),
        )
        latest_ts = proposal.created_at

    status = proposal.status
    decided_at = proposal.decided_at or proposal.created_at
    if status == "accepted":
        cand_stage = stage_num.get(proposal.candidate_version_id or "")
        text = (
            f"Accepted candidate map · saved as Stage {cand_stage}"
            if cand_stage is not None
            else "Accepted candidate map"
        )
        _append_entry(
            entries,
            _make_entry(
                entry_id=f"proposal-action:{proposal.proposal_id}",
                kind="player_action",
                text=text,
                label="Player decision",
                occurred_at=decided_at,
            ),
        )
        return "accepted", decided_at
    if status == "rejected":
        reason = ""
        if isinstance(proposal.decision, Mapping):
            reason = str(proposal.decision.get("reason") or "").strip()
        text = f"Rejected proposal ({reason})" if reason else "Rejected proposal"
        _append_entry(
            entries,
            _make_entry(
                entry_id=f"proposal-action:{proposal.proposal_id}",
                kind="player_action",
                text=text,
                label="Player decision",
                occurred_at=decided_at,
            ),
        )
        return "rejected", decided_at
    if status == "superseded":
        return "superseded", decided_at
    return "proposal_ready", latest_ts or proposal.created_at


def _project_proposals_and_player_challenges(
    *,
    bundle: SessionProjectionBundle,
    stage_num: dict[str, int],
    turns_by_id: dict[str, ConversationTurnRecord],
    user_by_asst: dict[str, ConversationTurnRecord],
    claimed_turns: set[str],
    base_meta: dict[str, Any],
) -> dict[str, list[tuple[str, int, dict[str, Any]]]]:
    """Build unified Policy-2 proposal nodes and linked player_challenge nodes."""
    nodes_by_vid: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}

    # Index audits relevant to binary discovery & player challenges
    choice_audits_by_tid: dict[str, list[AuditEventRecord]] = {}
    cancel_audit_by_tid: dict[str, AuditEventRecord] = {}
    review_pending_by_cid: dict[str, list[AuditEventRecord]] = {}
    for ev in bundle.audit_events:
        p = ev.payload
        if ev.event_type == "binary_choice_selected" and p.get("topicId"):
            choice_audits_by_tid.setdefault(str(p["topicId"]), []).append(ev)
        elif ev.event_type == "binary_discovery_cancelled" and p.get("topicId"):
            cancel_audit_by_tid[str(p["topicId"])] = ev
        elif ev.event_type == "challenge_reason_review_pending" and p.get("challengeId"):
            review_pending_by_cid.setdefault(str(p["challengeId"]), []).append(ev)

    # Map turns belonging to binary discovery topics
    topic_turns: dict[str, list[ConversationTurnRecord]] = {t.topic_id: [] for t in bundle.topics}
    for turn in bundle.turns:
        if turn.role != "assistant":
            continue
        g = turn.guidance or {}
        pd = g.get("proposalDiscovery")
        pq = g.get("proposalQuestion")
        tid = ""
        if isinstance(pd, Mapping) and pd.get("topicId"):
            tid = str(pd["topicId"])
        elif isinstance(pq, Mapping) and pq.get("topicId"):
            tid = str(pq["topicId"])
        if tid and tid in topic_turns:
            topic_turns[tid].append(turn)

    # Map turns belonging to revision challenges
    challenge_turns: dict[str, list[ConversationTurnRecord]] = {
        c.challenge_id: [] for c in bundle.challenges
    }
    challenge_by_turn: dict[str, RevisionChallengeRecord] = {}
    for chg in bundle.challenges:
        cid = chg.challenge_id
        for turn in bundle.turns:
            if turn.role != "assistant":
                continue
            g = turn.guidance or {}
            cc = g.get("challengeCard")
            cs = g.get("challengeState")
            dis = g.get("disagreement")
            matched = (
                turn.turn_id == chg.challenge_turn_id
                or (isinstance(cc, Mapping) and cc.get("challengeId") == cid)
                or (isinstance(cs, Mapping) and cs.get("challengeId") == cid)
                or (isinstance(dis, Mapping) and dis.get("challengeId") == cid)
            )
            if matched:
                challenge_turns[cid].append(turn)
                challenge_by_turn[turn.turn_id] = chg

    # Map offer_turn_id -> root proposal node_id (for parent linkage + execute_revision unification)
    offer_turn_to_node_id: dict[str, str] = {}
    topic_offer_turn_id: dict[str, str] = {}
    for topic in bundle.topics:
        node_id = f"proposal:{topic.topic_id}"
        for t in topic_turns.get(topic.topic_id, []):
            if isinstance((t.guidance or {}).get("proposalOffer"), Mapping):
                offer_turn_to_node_id[t.turn_id] = node_id
                topic_offer_turn_id[topic.topic_id] = t.turn_id

    challenge_res_turn: dict[str, ConversationTurnRecord] = {}
    challenge_has_child_proposal: set[str] = set()
    proposals_by_asst_turn: dict[str, ChangeProposalRecord] = {
        p.assistant_turn_id: p for p in bundle.proposals
    }
    for chg in bundle.challenges:
        cid = chg.challenge_id
        cturns = challenge_turns.get(cid, [])
        res_turn = next(
            (
                t
                for t in reversed(cturns)
                if isinstance((t.guidance or {}).get("challengeCard"), Mapping)
                and (t.guidance or {})["challengeCard"].get("selectedChoice")
            ),
            None,
        )
        if res_turn is not None:
            challenge_res_turn[cid] = res_turn
            has_repl_offer = isinstance((res_turn.guidance or {}).get("proposalOffer"), Mapping)
            has_direct_prop = res_turn.turn_id in proposals_by_asst_turn
            if has_repl_offer or has_direct_prop:
                challenge_has_child_proposal.add(cid)
                offer_turn_to_node_id[res_turn.turn_id] = f"proposal:challenge:{cid}"

    # Identify standalone purple offers (not owned by a topic or challenge)
    standalone_offer_turns: list[ConversationTurnRecord] = []
    for turn in bundle.turns:
        if turn.role != "assistant" or turn.turn_id in offer_turn_to_node_id:
            continue
        if turn.turn_id in challenge_by_turn:
            continue
        if isinstance((turn.guidance or {}).get("proposalOffer"), Mapping):
            offer_turn_to_node_id[turn.turn_id] = f"proposal:offer:{turn.turn_id}"
            standalone_offer_turns.append(turn)

    # Link execute_revision proposals back to their offer's root proposal node!
    linked_prop_by_node_id: dict[
        str, tuple[ChangeProposalRecord, ConversationTurnRecord | None]
    ] = {}
    claimed_proposal_ids: set[str] = set()
    for prop in bundle.proposals:
        asst_t = turns_by_id.get(prop.assistant_turn_id)
        if asst_t is not None and asst_t.turn_id in challenge_by_turn:
            chg = challenge_by_turn[asst_t.turn_id]
            linked_prop_by_node_id[f"proposal:challenge:{chg.challenge_id}"] = (prop, asst_t)
            claimed_proposal_ids.add(prop.proposal_id)
            continue
        src_offer_tid = ""
        if asst_t is not None and isinstance(asst_t.proposal_binding, Mapping):
            src_offer_tid = str(asst_t.proposal_binding.get("sourceOfferTurnId") or "").strip()
        if src_offer_tid and src_offer_tid in offer_turn_to_node_id:
            linked_prop_by_node_id[offer_turn_to_node_id[src_offer_tid]] = (prop, asst_t)
            claimed_proposal_ids.add(prop.proposal_id)
        elif prop.assistant_turn_id in offer_turn_to_node_id:
            linked_prop_by_node_id[offer_turn_to_node_id[prop.assistant_turn_id]] = (prop, asst_t)
            claimed_proposal_ids.add(prop.proposal_id)

    # Map source_proposal_turn_id -> challenges that challenged it (for "replaced" status)
    challenges_by_source_turn: dict[str, list[RevisionChallengeRecord]] = {}
    for chg in bundle.challenges:
        challenges_by_source_turn.setdefault(chg.source_proposal_turn_id, []).append(chg)

    # 1. Emit unified Binary Discovery Proposal nodes
    for topic in bundle.topics:
        tid = topic.topic_id
        state = topic.state
        vid = str(state.get("versionId") or "").strip()
        t_turns = topic_turns.get(tid, [])
        if not vid and t_turns:
            vid = t_turns[0].version_id
        node_id = f"proposal:{tid}"
        entries: list[dict[str, Any]] = []
        choices_list = list(state.get("choices") or ())
        choice_by_qid = {
            str(c.get("questionId") or ""): c for c in choices_list if isinstance(c, Mapping)
        }
        choice_ts_by_qid = {
            str(ev.payload.get("questionId") or ""): ev.created_at
            for ev in choice_audits_by_tid.get(tid, [])
        }
        started_at = ""
        updated_at = topic.updated_at

        if t_turns:
            for idx, asst_t in enumerate(t_turns):
                user_t = user_by_asst.get(asst_t.turn_id)
                if user_t is not None and user_t.turn_id not in claimed_turns:
                    claimed_turns.add(user_t.turn_id)
                    if not started_at:
                        started_at = user_t.created_at
                    _append_entry(
                        entries,
                        _make_entry(
                            entry_id=f"player:{user_t.turn_id}",
                            kind="player_message",
                            text=user_t.content,
                            label="Player",
                            occurred_at=user_t.created_at,
                            language=user_t.language,
                        ),
                    )
                elif idx == 0 and state.get("initialRequest"):
                    if not started_at:
                        started_at = asst_t.created_at
                    _append_entry(
                        entries,
                        _make_entry(
                            entry_id=f"topic-req:{tid}",
                            kind="player_message",
                            text=str(state["initialRequest"]),
                            label="Player",
                            occurred_at=asst_t.created_at,
                            language=asst_t.language,
                        ),
                    )
                claimed_turns.add(asst_t.turn_id)
                if not started_at:
                    started_at = asst_t.created_at
                _append_entry(
                    entries,
                    _make_entry(
                        entry_id=f"llm:{asst_t.turn_id}",
                        kind="llm_message",
                        text=asst_t.content,
                        label="LLM",
                        occurred_at=asst_t.created_at,
                        language=asst_t.language,
                    ),
                )
                g = asst_t.guidance or {}
                pq = g.get("proposalQuestion")
                if isinstance(pq, Mapping):
                    q_text = _format_question_card_text(pq)
                    _append_entry(
                        entries,
                        _make_entry(
                            entry_id=f"card:{asst_t.turn_id}:pq",
                            kind="card",
                            text=q_text,
                            label="Binary discovery question",
                            occurred_at=asst_t.created_at,
                            language=asst_t.language,
                        ),
                    )
                    qid = str(pq.get("questionId") or "")
                    selected = choice_by_qid.get(qid)
                    if selected is not None:
                        sel_label = str(
                            selected.get("label") or selected.get("choiceId") or ""
                        ).strip()
                        sel_ts = choice_ts_by_qid.get(qid) or asst_t.created_at
                        _append_entry(
                            entries,
                            _make_entry(
                                entry_id=f"choice:{tid}:{qid}",
                                kind="player_action",
                                text=sel_label,
                                label="Selected direction",
                                occurred_at=sel_ts,
                                language=asst_t.language,
                            ),
                        )
                offer = g.get("proposalOffer")
                if isinstance(offer, Mapping) and offer.get("summary"):
                    _append_entry(
                        entries,
                        _make_entry(
                            entry_id=f"card:{asst_t.turn_id}:offer",
                            kind="card",
                            text=str(offer["summary"]),
                            label="Proposal direction",
                            occurred_at=asst_t.created_at,
                            language=asst_t.language,
                        ),
                    )
                updated_at = asst_t.created_at
        else:
            started_at = topic.updated_at
            if state.get("initialRequest"):
                _append_entry(
                    entries,
                    _make_entry(
                        entry_id=f"topic-req:{tid}",
                        kind="player_message",
                        text=str(state["initialRequest"]),
                        label="Player",
                        occurred_at=topic.updated_at,
                        language=bundle.session.language,
                    ),
                )

        # If cancelled, record cancellation action without wiping prior rounds
        raw_status = str(state.get("status") or "in_progress")
        if raw_status == "cancelled" or tid in cancel_audit_by_tid:
            cancel_ts = (
                cancel_audit_by_tid[tid].created_at
                if tid in cancel_audit_by_tid
                else topic.updated_at
            )
            _append_entry(
                entries,
                _make_entry(
                    entry_id=f"proposal-cancel:{tid}",
                    kind="player_action",
                    text="Cancelled proposal discovery topic",
                    label="Player action",
                    occurred_at=cancel_ts,
                    language=bundle.session.language,
                ),
            )
            updated_at = cancel_ts

        # Attach linked execute_revision & change_proposals decision (ZERO split orphan nodes!)
        linked_entry = linked_prop_by_node_id.get(node_id)
        prop_status: str | None = None
        if linked_entry is not None:
            prop_rec, exec_asst = linked_entry
            exec_user = user_by_asst.get(exec_asst.turn_id) if exec_asst is not None else None
            prop_status, dec_ts = _append_proposal_execution_and_decision(
                entries=entries,
                exec_turn=exec_asst,
                exec_user=exec_user,
                proposal=prop_rec,
                stage_num=stage_num,
                claimed_turns=claimed_turns,
            )
            if dec_ts:
                updated_at = dec_ts

        # Determine canonical Dashboard nodeStatus
        offer_tid = topic_offer_turn_id.get(tid, "")
        challenged_list = challenges_by_source_turn.get(offer_tid, [])
        was_replaced = any(c.challenge_id in challenge_has_child_proposal for c in challenged_list)
        if prop_status is not None:
            node_status = (
                "replaced" if (prop_status == "superseded" and was_replaced) else prop_status
            )
        elif raw_status == "clarifying":
            q_num = min(3, max(1, int(state.get("questionCount") or (len(choices_list) + 1))))
            node_status = f"clarifying_{q_num}"
        elif raw_status == "proposal_ready":
            node_status = "replaced" if was_replaced else "proposal_ready"
        elif raw_status == "cancelled" or tid in cancel_audit_by_tid:
            node_status = "cancelled"
        else:
            node_status = raw_status

        if entries:
            node = _build_node_event(
                **base_meta,
                version_id=vid or None,
                stage_number=stage_num.get(vid),
                node_id=node_id,
                node_type="proposal",
                node_status=node_status,
                entries=entries,
                started_at=started_at or topic.updated_at,
                updated_at=updated_at or started_at or topic.updated_at,
            )
            nodes_by_vid.setdefault(vid, []).append((started_at or topic.updated_at, 30, node))

    # 2. Emit standalone purple offers (unified with execute_revision & decision)
    for offer_t in standalone_offer_turns:
        node_id = f"proposal:offer:{offer_t.turn_id}"
        vid = offer_t.version_id
        entries = []
        user_t = user_by_asst.get(offer_t.turn_id)
        started_at = offer_t.created_at
        if user_t is not None and user_t.turn_id not in claimed_turns:
            claimed_turns.add(user_t.turn_id)
            started_at = user_t.created_at
            _append_entry(
                entries,
                _make_entry(
                    entry_id=f"player:{user_t.turn_id}",
                    kind="player_message",
                    text=user_t.content,
                    label="Player",
                    occurred_at=user_t.created_at,
                    language=user_t.language,
                ),
            )
        claimed_turns.add(offer_t.turn_id)
        _append_entry(
            entries,
            _make_entry(
                entry_id=f"llm:{offer_t.turn_id}",
                kind="llm_message",
                text=offer_t.content,
                label="LLM",
                occurred_at=offer_t.created_at,
                language=offer_t.language,
            ),
        )
        offer = (offer_t.guidance or {}).get("proposalOffer") or {}
        if isinstance(offer, Mapping) and offer.get("summary"):
            _append_entry(
                entries,
                _make_entry(
                    entry_id=f"card:{offer_t.turn_id}:offer",
                    kind="card",
                    text=str(offer["summary"]),
                    label="Proposal direction",
                    occurred_at=offer_t.created_at,
                    language=offer_t.language,
                ),
            )
        updated_at = offer_t.created_at
        linked_entry = linked_prop_by_node_id.get(node_id)
        prop_status = None
        if linked_entry is not None:
            prop_rec, exec_asst = linked_entry
            exec_user = user_by_asst.get(exec_asst.turn_id) if exec_asst is not None else None
            prop_status, dec_ts = _append_proposal_execution_and_decision(
                entries=entries,
                exec_turn=exec_asst,
                exec_user=exec_user,
                proposal=prop_rec,
                stage_num=stage_num,
                claimed_turns=claimed_turns,
            )
            if dec_ts:
                updated_at = dec_ts

        challenged_list = challenges_by_source_turn.get(offer_t.turn_id, [])
        was_replaced = any(c.challenge_id in challenge_has_child_proposal for c in challenged_list)
        node_status = (
            ("replaced" if (prop_status == "superseded" and was_replaced) else prop_status)
            if prop_status is not None
            else ("replaced" if was_replaced else "proposal_ready")
        )
        node = _build_node_event(
            **base_meta,
            version_id=vid,
            stage_number=stage_num.get(vid),
            node_id=node_id,
            node_type="proposal",
            node_status=node_status,
            entries=entries,
            started_at=started_at,
            updated_at=updated_at,
        )
        nodes_by_vid.setdefault(vid, []).append((started_at, 32, node))

    # 3. Emit Player Challenge nodes + unified child Proposal nodes (replacement / keep_proposal)
    for chg in bundle.challenges:
        cid = chg.challenge_id
        vid = chg.base_version_id
        parent_node_id = offer_turn_to_node_id.get(chg.source_proposal_turn_id)
        cturns = challenge_turns.get(cid, [])
        res_turn = challenge_res_turn.get(cid)

        chg_entries: list[dict[str, Any]] = []
        started_at = chg.created_at
        updated_at = chg.updated_at

        # Source proposal summary for clear context at top of Player Challenge
        src_turn = turns_by_id.get(chg.source_proposal_turn_id)
        src_summary = ""
        if src_turn is not None:
            src_offer = (src_turn.guidance or {}).get("proposalOffer")
            if isinstance(src_offer, Mapping):
                src_summary = str(src_offer.get("summary") or "").strip()

        selected_choice: str | None = None
        resolution_val: str | None = None

        for idx, asst_t in enumerate(cturns):
            g = asst_t.guidance or {}
            cc = g.get("challengeCard") if isinstance(g.get("challengeCard"), Mapping) else {}
            dis = g.get("disagreement") if isinstance(g.get("disagreement"), Mapping) else {}
            if cc.get("selectedChoice"):
                selected_choice = str(cc["selectedChoice"])
            if dis.get("resolution"):
                resolution_val = str(dis["resolution"])

            user_t = user_by_asst.get(asst_t.turn_id)
            if idx == 0:
                if user_t is not None and user_t.turn_id not in claimed_turns:
                    claimed_turns.add(user_t.turn_id)
                    started_at = user_t.created_at
                start_text = (
                    f"Challenge this plan: {src_summary}"
                    if src_summary
                    else (user_t.content if user_t is not None else "Challenge this plan")
                )
                _append_entry(
                    chg_entries,
                    _make_entry(
                        entry_id=f"challenge-start:{cid}",
                        kind="player_action",
                        text=start_text,
                        label="Player action",
                        occurred_at=started_at,
                        language=asst_t.language,
                    ),
                )
            elif user_t is not None and user_t.turn_id not in claimed_turns:
                claimed_turns.add(user_t.turn_id)
                is_choice_turn = bool(cc.get("selectedChoice"))
                choice_label_map = {
                    "keep_proposal": "Kept original proposal",
                    "revise_proposal": "Requested new plan from player's reason",
                    "cancel_challenge": "Exited challenge",
                }
                _append_entry(
                    chg_entries,
                    _make_entry(
                        entry_id=f"player:{user_t.turn_id}",
                        kind="player_action" if is_choice_turn else "player_message",
                        text=(
                            choice_label_map.get(str(cc.get("selectedChoice")), user_t.content)
                            if is_choice_turn
                            else user_t.content
                        ),
                        label="Player choice" if is_choice_turn else "Challenge reason",
                        occurred_at=user_t.created_at,
                        language=user_t.language,
                    ),
                )

            claimed_turns.add(asst_t.turn_id)
            _append_entry(
                chg_entries,
                _make_entry(
                    entry_id=f"llm:{asst_t.turn_id}",
                    kind="llm_message",
                    text=asst_t.content,
                    label="LLM",
                    occurred_at=asst_t.created_at,
                    language=asst_t.language,
                ),
            )
            if cc.get("comparison"):
                rel = str(cc.get("relation") or "").strip()
                merit = str(cc.get("merit") or "").strip()
                prefix = f"[{rel} / {merit}] " if (rel or merit) else ""
                _append_entry(
                    chg_entries,
                    _make_entry(
                        entry_id=f"card:{asst_t.turn_id}:review",
                        kind="card",
                        text=f"{prefix}{cc['comparison']}",
                        label="Challenge comparison",
                        occurred_at=asst_t.created_at,
                        language=asst_t.language,
                    ),
                )
            updated_at = asst_t.created_at

        # Check review_pending audit
        pending_audits = review_pending_by_cid.get(cid, [])
        has_pending_retry = bool(
            pending_audits
            and chg.status == "awaiting_reason"
            and pending_audits[-1].created_at >= chg.updated_at
        )

        # Canonical Player Challenge status
        if selected_choice == "keep_proposal" or resolution_val == "ai":
            chg_status = "original_selected"
        elif selected_choice == "revise_proposal" or resolution_val == "user":
            chg_status = (
                "replacement_selected"
                if cid in challenge_has_child_proposal
                else "revision_needed"
            )
        elif selected_choice == "cancel_challenge" or resolution_val == "retain_current":
            chg_status = "exited"
        elif chg.status in {"awaiting_choice", "choice_pending"}:
            chg_status = "awaiting_player_choice"
        elif has_pending_retry:
            chg_status = "review_pending"
            updated_at = pending_audits[-1].created_at
            _append_entry(
                chg_entries,
                _make_entry(
                    entry_id=f"challenge-pending:{cid}",
                    kind="status",
                    text="Reason review pending retry",
                    label="Status",
                    occurred_at=pending_audits[-1].created_at,
                ),
            )
        else:
            chg_status = "awaiting_reason"

        chg_node_id = f"player-challenge:{cid}"
        chg_node = _build_node_event(
            **base_meta,
            version_id=vid,
            stage_number=stage_num.get(vid),
            node_id=chg_node_id,
            node_type="player_challenge",
            node_status=chg_status,
            node_parent_id=parent_node_id,
            entries=chg_entries,
            started_at=started_at,
            updated_at=updated_at,
        )
        nodes_by_vid.setdefault(vid, []).append((started_at, 40, chg_node))

        # Child Proposal node generated by challenge resolution (revise_proposal OR keep_proposal)
        if cid in challenge_has_child_proposal and res_turn is not None:
            child_node_id = f"proposal:challenge:{cid}"
            child_entries: list[dict[str, Any]] = []
            child_start = res_turn.created_at
            child_updated = res_turn.created_at

            res_offer = (res_turn.guidance or {}).get("proposalOffer")
            if isinstance(res_offer, Mapping):
                _append_entry(
                    child_entries,
                    _make_entry(
                        entry_id=f"card:{res_turn.turn_id}:repl-offer",
                        kind="card",
                        text=str(res_offer.get("summary") or res_turn.content),
                        label="Replacement proposal direction",
                        occurred_at=res_turn.created_at,
                        language=res_turn.language,
                    ),
                )

            linked_child = linked_prop_by_node_id.get(child_node_id)
            child_status = "proposal_ready"
            if linked_child is not None:
                prop_rec, exec_asst = linked_child
                is_direct_keep = exec_asst is not None and exec_asst.turn_id == res_turn.turn_id
                exec_user = user_by_asst.get(exec_asst.turn_id) if exec_asst is not None else None
                c_stat, c_ts = _append_proposal_execution_and_decision(
                    entries=child_entries,
                    exec_turn=exec_asst,
                    exec_user=exec_user,
                    proposal=prop_rec,
                    stage_num=stage_num,
                    claimed_turns=claimed_turns,
                    skip_exec_turns=is_direct_keep,
                )
                if c_stat:
                    child_status = c_stat
                if c_ts:
                    child_updated = c_ts

            if child_entries:
                child_node = _build_node_event(
                    **base_meta,
                    version_id=vid,
                    stage_number=stage_num.get(vid),
                    node_id=child_node_id,
                    node_type="proposal",
                    node_status=child_status,
                    node_parent_id=chg_node_id,
                    entries=child_entries,
                    started_at=child_start,
                    updated_at=child_updated,
                )
                nodes_by_vid.setdefault(vid, []).append((child_start, 45, child_node))

    # 4. Emit any orphan/legacy change_proposals not linked to a topic, offer, or challenge
    for prop in bundle.proposals:
        if prop.proposal_id in claimed_proposal_ids:
            continue
        vid = prop.base_version_id
        exec_asst = turns_by_id.get(prop.assistant_turn_id)
        exec_user = user_by_asst.get(exec_asst.turn_id) if exec_asst is not None else None
        entries = []
        p_status, dec_ts = _append_proposal_execution_and_decision(
            entries=entries,
            exec_turn=exec_asst,
            exec_user=exec_user,
            proposal=prop,
            stage_num=stage_num,
            claimed_turns=claimed_turns,
        )
        started_at = (
            exec_user.created_at
            if exec_user is not None
            else (exec_asst.created_at if exec_asst is not None else prop.created_at)
        )
        node = _build_node_event(
            **base_meta,
            version_id=vid,
            stage_number=stage_num.get(vid),
            node_id=f"proposal:{prop.proposal_id}",
            node_type="proposal",
            node_status=p_status or "proposal_ready",
            entries=entries,
            started_at=started_at,
            updated_at=dec_ts or prop.created_at,
        )
        nodes_by_vid.setdefault(vid, []).append((started_at, 48, node))

    return nodes_by_vid


def _project_intent_nodes(
    *,
    bundle: SessionProjectionBundle,
    stage_num: dict[str, int],
    user_by_asst: dict[str, ConversationTurnRecord],
    claimed_turns: set[str],
    base_meta: dict[str, Any],
) -> dict[str, list[tuple[str, int, dict[str, Any]]]]:
    """Build full cumulative intent nodes with confirmed/revised/rejected/awaiting_player_choice."""
    nodes_by_vid: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}

    hyp_by_id: dict[str, dict[str, Any]] = {}
    for ver in bundle.versions:
        ctx = ver.design_context or {}
        for item in ctx.get("intentHypotheses") or []:
            if isinstance(item, Mapping) and item.get("id"):
                hyp_by_id[str(item["id"])] = dict(item)

    recorded_by_hid: dict[str, AuditEventRecord] = {}
    feedback_by_hid: dict[str, list[AuditEventRecord]] = {}
    rewrite_by_hid: dict[str, AuditEventRecord] = {}
    for ev in bundle.audit_events:
        p = ev.payload
        if ev.event_type == "intent_hypothesis_recorded" and p.get("hypothesisId"):
            recorded_by_hid[str(p["hypothesisId"])] = ev
        elif ev.event_type == "intent_hypothesis_feedback_applied" and p.get("hypothesisId"):
            feedback_by_hid.setdefault(str(p["hypothesisId"]), []).append(ev)
        elif (
            ev.event_type == "intent_progress_rewrite"
            and p.get("artifactType") == "intent_hypothesis"
            and p.get("artifactId")
        ):
            rewrite_by_hid[str(p["artifactId"])] = ev

    for turn in bundle.turns:
        if turn.role != "assistant" or turn.turn_id in claimed_turns:
            continue
        g = turn.guidance or {}
        hid = str(g.get("_intentHypothesisId") or "").strip()
        if not hid:
            continue

        entries: list[dict[str, Any]] = []
        user_t = user_by_asst.get(turn.turn_id)
        started_at = turn.created_at
        if user_t is not None and user_t.turn_id not in claimed_turns:
            claimed_turns.add(user_t.turn_id)
            started_at = user_t.created_at
            _append_entry(
                entries,
                _make_entry(
                    entry_id=f"player:{user_t.turn_id}",
                    kind="player_message",
                    text=user_t.content,
                    label="Player",
                    occurred_at=user_t.created_at,
                    language=user_t.language,
                ),
            )
        claimed_turns.add(turn.turn_id)
        _append_entry(
            entries,
            _make_entry(
                entry_id=f"llm:{turn.turn_id}",
                kind="llm_message",
                text=turn.content,
                label="LLM",
                occurred_at=turn.created_at,
                language=turn.language,
            ),
        )
        rec_audit = recorded_by_hid.get(hid)
        initial_stmt = str(
            (rec_audit.payload.get("statement") if rec_audit else "")
            or g.get("intentHypothesis")
            or hyp_by_id.get(hid, {}).get("statement")
            or ""
        ).strip()
        if initial_stmt:
            _append_entry(
                entries,
                _make_entry(
                    entry_id=f"card:{turn.turn_id}:intent",
                    kind="card",
                    text=initial_stmt,
                    label="Intent hypothesis",
                    occurred_at=turn.created_at,
                    language=turn.language,
                ),
            )

        updated_at = turn.created_at
        fb_audits = feedback_by_hid.get(hid, [])
        hyp_record = hyp_by_id.get(hid, {})
        rew_audit = rewrite_by_hid.get(hid)
        resolved_stmt = str(
            (rew_audit.payload.get("summaryText") if rew_audit else "")
            or hyp_record.get("statement")
            or initial_stmt
        ).strip()

        node_status = "awaiting_player_choice"
        if fb_audits:
            for fb in fb_audits:
                action = str(fb.payload.get("action") or "").strip().lower()
                if action == "revise":
                    node_status = "revised"
                    action_text = (
                        f"Revised intent: {resolved_stmt}" if resolved_stmt else "Revised intent"
                    )
                elif action == "confirm":
                    node_status = "confirmed"
                    action_text = (
                        f"Confirmed intent: {resolved_stmt}"
                        if resolved_stmt
                        else "Confirmed intent"
                    )
                elif action == "reject":
                    node_status = "rejected"
                    action_text = "Rejected intent"
                else:
                    action_text = f"Intent {action}"
                _append_entry(
                    entries,
                    _make_entry(
                        entry_id=f"intent-feedback:{hid}:{fb.id}",
                        kind="player_action",
                        text=action_text,
                        label="Player action",
                        occurred_at=fb.created_at,
                        language=turn.language,
                    ),
                )
                updated_at = fb.created_at
        else:
            raw_hyp_status = str(hyp_record.get("status") or "tentative")
            if raw_hyp_status in {"confirmed", "rejected"}:
                node_status = raw_hyp_status
            elif raw_hyp_status == "superseded":
                node_status = "replaced"
            else:
                node_status = "awaiting_player_choice"

        node = _build_node_event(
            **base_meta,
            version_id=turn.version_id,
            stage_number=stage_num.get(turn.version_id),
            node_id=f"intent:{hid}",
            node_type="intent",
            node_status=node_status,
            entries=entries,
            started_at=started_at,
            updated_at=updated_at,
        )
        nodes_by_vid.setdefault(turn.version_id, []).append((started_at, 25, node))

    return nodes_by_vid


def build_authoritative_player_projection(
    bundle: SessionProjectionBundle,
    *,
    match_id: str,
    room_code: str,
    player_number: int,
    study_session_id: str,
    draft_event: dict[str, Any] | None = None,
    fallback_duration: float | None = None,
    default_deadline_seconds: int = 1200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build complete authoritative (coCreationFlow, playAttempts, draftRegenerations)."""
    session = bundle.session
    versions_by_id = {v.version_id: v for v in bundle.versions}
    stage_num = {v.version_id: v.version_number for v in bundle.versions}
    turns_by_id = {t.turn_id: t for t in bundle.turns}
    user_by_asst = _pair_user_turns(bundle.turns)
    claimed_turns: set[str] = set()

    base_meta = {
        "match_id": match_id,
        "room_code": room_code,
        "player_number": player_number,
        "study_session_id": study_session_id,
        "session_id": session.session_id,
    }

    # Sanitized playAttempts & draftRegenerations
    all_play_attempts = [
        serialize_dashboard_play_attempt(a, stage_number=stage_num.get(a.version_id))
        for a in bundle.play_attempts
    ]
    attempts_by_vid: dict[str, list[dict[str, Any]]] = {}
    for att in all_play_attempts:
        attempts_by_vid.setdefault(str(att["versionId"]), []).append(att)

    all_draft_regens = [
        serialize_dashboard_draft_regeneration(r) for r in bundle.draft_regenerations
    ]

    # Restored stage metadata from audit_events
    restored_by_vid: dict[str, dict[str, Any]] = {}
    for ev in bundle.audit_events:
        if ev.event_type == "stage_restored" and ev.payload.get("versionId"):
            restored_by_vid[str(ev.payload["versionId"])] = dict(ev.payload)

    # Project specialized nodes first (claiming their turns)
    manual_nodes_by_vid = _project_manual_edit_and_llm_challenges(
        bundle=bundle,
        versions_by_id=versions_by_id,
        stage_num=stage_num,
        turns_by_id=turns_by_id,
        user_by_asst=user_by_asst,
        claimed_turns=claimed_turns,
        base_meta=base_meta,
    )
    prop_nodes_by_vid = _project_proposals_and_player_challenges(
        bundle=bundle,
        stage_num=stage_num,
        turns_by_id=turns_by_id,
        user_by_asst=user_by_asst,
        claimed_turns=claimed_turns,
        base_meta=base_meta,
    )
    intent_nodes_by_vid = _project_intent_nodes(
        bundle=bundle,
        stage_num=stage_num,
        user_by_asst=user_by_asst,
        claimed_turns=claimed_turns,
        base_meta=base_meta,
    )

    # Identify opening turns for non-manual-review versions (first unclaimed assistant-only turn)
    opening_by_vid: dict[str, ConversationTurnRecord] = {}
    for ver in bundle.versions:
        if ver.version_id in manual_nodes_by_vid:
            continue
        for turn in bundle.turns:
            if turn.version_id != ver.version_id or turn.turn_id in claimed_turns:
                continue
            if turn.role == "assistant" and turn.turn_id not in user_by_asst:
                g = turn.guidance or {}
                if not any(
                    g.get(k)
                    for k in (
                        "proposalQuestion",
                        "proposalOffer",
                        "challengeCard",
                        "challengeState",
                    )
                ):
                    opening_by_vid[ver.version_id] = turn
                    claimed_turns.add(turn.turn_id)
                    break
            break  # Only the very first turn of a stage can be its opening

    # Group contiguous unclaimed ordinary dialogue turns into `discussion` nodes per version
    discussion_nodes_by_vid: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
    consumed_opening_vids: set[str] = set()
    run_turns: list[ConversationTurnRecord] = []

    def _flush_discussion_run() -> None:
        nonlocal run_turns
        if not run_turns:
            return
        first_t = run_turns[0]
        last_t = run_turns[-1]
        vid = first_t.version_id
        entries: list[dict[str, Any]] = []
        opening_turn_id: str | None = None
        started_at = first_t.created_at

        # First discussion node on a stage consumes its unconsumed opening evaluation
        if vid in opening_by_vid and vid not in consumed_opening_vids:
            op_turn = opening_by_vid[vid]
            consumed_opening_vids.add(vid)
            opening_turn_id = op_turn.turn_id
            _append_entry(
                entries,
                _make_entry(
                    entry_id=f"opening:{op_turn.turn_id}",
                    kind="llm_message",
                    text=op_turn.content,
                    label="AI opening evaluation",
                    occurred_at=op_turn.created_at,
                    language=op_turn.language,
                ),
            )
            for c_idx, card in enumerate(build_turn_cards(op_turn.guidance)):
                _append_entry(
                    entries,
                    _make_entry(
                        entry_id=f"opening-card:{op_turn.turn_id}:{c_idx}",
                        kind="card",
                        text=card["text"],
                        label=f"Opening card ({card['type']})",
                        occurred_at=op_turn.created_at,
                        language=op_turn.language,
                    ),
                )

        for turn in run_turns:
            if turn.role == "user":
                _append_entry(
                    entries,
                    _make_entry(
                        entry_id=f"player:{turn.turn_id}",
                        kind="player_message",
                        text=turn.content,
                        label="Player",
                        occurred_at=turn.created_at,
                        language=turn.language,
                    ),
                )
            else:
                _append_entry(
                    entries,
                    _make_entry(
                        entry_id=f"llm:{turn.turn_id}",
                        kind="llm_message",
                        text=turn.content,
                        label="LLM",
                        occurred_at=turn.created_at,
                        language=turn.language,
                    ),
                )
                for c_idx, card in enumerate(build_turn_cards(turn.guidance)):
                    _append_entry(
                        entries,
                        _make_entry(
                            entry_id=f"card:{turn.turn_id}:{c_idx}",
                            kind="card",
                            text=card["text"],
                            label="Card",
                            occurred_at=turn.created_at,
                            language=turn.language,
                        ),
                    )

        is_active_latest = (
            session.status == "active" and vid == session.current_version_id
        )
        node = _build_node_event(
            **base_meta,
            version_id=vid,
            stage_number=stage_num.get(vid),
            node_id=f"discussion:{first_t.turn_id}",
            node_type="discussion",
            node_status="in_progress" if is_active_latest else "resolved",
            entries=entries,
            started_at=started_at,
            updated_at=last_t.created_at,
            opening_assistant_turn_id=opening_turn_id,
        )
        discussion_nodes_by_vid.setdefault(vid, []).append((started_at, 20, node))
        run_turns = []

    for turn in bundle.turns:
        if turn.turn_id in claimed_turns:
            _flush_discussion_run()
            continue
        if run_turns and run_turns[-1].version_id != turn.version_id:
            _flush_discussion_run()
        claimed_turns.add(turn.turn_id)
        run_turns.append(turn)
    _flush_discussion_run()

    # Assemble stage-ordered authoritative coCreationFlow
    flow: list[dict[str, Any]] = []

    if draft_event is not None:
        draft_copy = dict(draft_event)
        draft_copy["eventType"] = "draft"
        draft_copy["matchId"] = match_id
        draft_copy["roomCode"] = room_code
        draft_copy["playerNumber"] = player_number
        draft_copy["studySessionId"] = study_session_id
        draft_copy["sessionId"] = session.session_id
        if all_draft_regens:
            draft_copy["draftRegenerations"] = list(all_draft_regens)
        flow.append(draft_copy)

    prev_ver: LevelVersionRecord | None = None
    for ver in bundle.versions:
        vid = ver.version_id
        is_first = ver.version_number == 1
        stage_ev: dict[str, Any] = {
            "eventId": f"{'first_stage' if is_first else 'stage'}:{vid}",
            "eventType": "first_stage" if is_first else "stage",
            "matchId": match_id,
            "roomCode": room_code,
            "playerNumber": player_number,
            "studySessionId": study_session_id,
            "sessionId": session.session_id,
            "serverReceivedAt": ver.created_at,
            "versionId": vid,
            "stageNumber": ver.version_number,
            "rows": list(ver.rows),
            "diff": [dict(d) for d in ver.diff],
            "solver": extract_solver_summary(ver.solver),
            "playAttempts": list(attempts_by_vid.get(vid, [])),
        }
        if is_first:
            stage_ev["initialDraftMethod"] = session.mode
        else:
            mapped_source = _VERSION_SOURCE_MAP.get(ver.source, "manual")
            stage_ev["source"] = mapped_source
            if mapped_source == "restored":
                rest_meta = restored_by_vid.get(vid, {})
                rf_vid = str(
                    rest_meta.get("restoredFromVersionId") or ver.parent_version_id or ""
                ).strip()
                rf_num = rest_meta.get("restoredFromStageNumber")
                if rf_num is None and rf_vid:
                    rf_num = stage_num.get(rf_vid)
                rp_vid = str(
                    rest_meta.get("replacedVersionId")
                    or (prev_ver.version_id if prev_ver is not None else "")
                ).strip()
                rp_num = rest_meta.get("replacedStageNumber")
                if rp_num is None and rp_vid:
                    rp_num = stage_num.get(rp_vid)
                if rf_vid:
                    stage_ev["restoredFromVersionId"] = rf_vid
                if rf_num is not None:
                    stage_ev["restoredFromStageNumber"] = int(rf_num)
                if rp_vid:
                    stage_ev["replacedVersionId"] = rp_vid
                if rp_num is not None:
                    stage_ev["replacedStageNumber"] = int(rp_num)
        flow.append(stage_ev)

        # Stage opening event (with real cards & accurate consumed flag)
        op_turn = opening_by_vid.get(vid)
        if op_turn is not None:
            flow.append(
                {
                    "eventId": f"opening:{op_turn.turn_id}",
                    "eventType": "opening",
                    "matchId": match_id,
                    "roomCode": room_code,
                    "playerNumber": player_number,
                    "studySessionId": study_session_id,
                    "sessionId": session.session_id,
                    "serverReceivedAt": op_turn.created_at,
                    "versionId": vid,
                    "stageNumber": ver.version_number,
                    "assistantTurnId": op_turn.turn_id,
                    "assistantText": op_turn.content,
                    "language": op_turn.language,
                    "cards": build_turn_cards(op_turn.guidance),
                    "consumed": vid in consumed_opening_vids,
                }
            )

        # Stage nodes sorted chronologically with causal tie-breaking
        stage_nodes: list[tuple[str, int, dict[str, Any]]] = []
        stage_nodes.extend(manual_nodes_by_vid.get(vid, []))
        stage_nodes.extend(discussion_nodes_by_vid.get(vid, []))
        stage_nodes.extend(intent_nodes_by_vid.get(vid, []))
        stage_nodes.extend(prop_nodes_by_vid.get(vid, []))
        stage_nodes.sort(key=lambda item: (item[0], item[1]))
        for _, _, node_ev in stage_nodes:
            flow.append(node_ev)

        prev_ver = ver

    # Final stage event
    if session.finalized_version_id and session.finalized_version_id in versions_by_id:
        final_ver = versions_by_id[session.finalized_version_id]
        duration = _compute_final_duration(
            session,
            fallback_duration=fallback_duration,
            default_deadline_seconds=default_deadline_seconds,
        )
        final_ev: dict[str, Any] = {
            "eventId": f"final:{final_ver.version_id}",
            "eventType": "final",
            "matchId": match_id,
            "roomCode": room_code,
            "playerNumber": player_number,
            "studySessionId": study_session_id,
            "sessionId": session.session_id,
            "serverReceivedAt": session.finalized_at or session.updated_at,
            "versionId": final_ver.version_id,
            "stageNumber": final_ver.version_number,
            "rows": list(final_ver.rows),
            "diff": [dict(d) for d in final_ver.diff],
            "solver": extract_solver_summary(final_ver.solver),
            "playAttempts": list(attempts_by_vid.get(final_ver.version_id, [])),
        }
        if duration is not None:
            final_ev["coCreationDurationSeconds"] = duration
        flow.append(final_ev)

    # Final designer intention message event
    if isinstance(session.final_intention, Mapping):
        clean_msg = str(session.final_intention.get("content") or "").strip()
        if clean_msg:
            msg_ts = str(
                session.final_intention.get("createdAt")
                or session.completed_at
                or session.updated_at
            )
            flow.append(
                {
                    "eventId": f"message:{session.session_id}",
                    "eventType": "message",
                    "matchId": match_id,
                    "roomCode": room_code,
                    "playerNumber": player_number,
                    "studySessionId": study_session_id,
                    "sessionId": session.session_id,
                    "serverReceivedAt": msg_ts,
                    "message": clean_msg,
                }
            )

    return flow, all_play_attempts, all_draft_regens


def serialize_session_research_bundle(bundle: SessionProjectionBundle) -> dict[str, Any]:
    """Serialize a formal design_session and all child tables for NDJSON research export.

    Strips ALL secret/token/ticket hashes and never exports demo sessions.
    """
    s = bundle.session
    stage_num = {v.version_id: v.version_number for v in bundle.versions}
    return redact_secret_keys(
        {
            "session": {
                "sessionId": s.session_id,
                "matchId": s.match_id,
                "playerSlot": s.player_slot,
                "mode": s.mode,
                "language": s.language,
                "languageLocked": s.language_locked,
                "languageLockedAt": s.language_locked_at,
                "status": s.status,
                "currentVersionId": s.current_version_id,
                "deadlineStartedAt": s.deadline_started_at,
                "deadlineAt": s.deadline_at,
                "finalizedVersionId": s.finalized_version_id,
                "finalizedAt": s.finalized_at,
                "completedAt": s.completed_at,
                "finalIntention": s.final_intention,
                "draftRows": list(s.draft_rows) if s.draft_rows is not None else None,
                "draftValidation": s.draft_validation,
                "draftGeneration": s.draft_generation,
                "isDemo": s.is_demo,
                "createdAt": s.created_at,
                "updatedAt": s.updated_at,
                "expiresAt": s.expires_at,
            },
            "levelVersions": [
                {
                    "versionId": v.version_id,
                    "versionNumber": v.version_number,
                    "source": v.source,
                    "summary": v.summary,
                    "rows": list(v.rows),
                    "diff": [dict(d) for d in v.diff],
                    "solver": dict(v.solver),
                    "entityBindings": v.entity_bindings,
                    "designContext": v.design_context,
                    "assessment": v.assessment,
                    "parentVersionId": v.parent_version_id,
                    "createdAt": v.created_at,
                }
                for v in bundle.versions
            ],
            "conversationTurns": [
                {
                    "turnId": t.turn_id,
                    "sequenceNumber": t.sequence_number,
                    "requestId": t.request_id,
                    "role": t.role,
                    "content": t.content,
                    "language": t.language,
                    "guidance": t.guidance,
                    "cards": build_turn_cards(t.guidance),
                    "translations": t.translations,
                    "model": t.model,
                    "attemptsUsed": t.attempts_used,
                    "latencyMs": t.latency_ms,
                    "versionId": t.version_id,
                    "createdAt": t.created_at,
                }
                for t in bundle.turns
            ],
            "changeProposals": [
                {
                    "proposalId": p.proposal_id,
                    "assistantTurnId": p.assistant_turn_id,
                    "baseVersionId": p.base_version_id,
                    "candidateVersionId": p.candidate_version_id,
                    "status": p.status,
                    "proposal": p.proposal,
                    "decision": p.decision,
                    "createdAt": p.created_at,
                    "decidedAt": p.decided_at,
                }
                for p in bundle.proposals
            ],
            "revisionChallenges": [
                {
                    "challengeId": c.challenge_id,
                    "baseVersionId": c.base_version_id,
                    "sourceProposalTurnId": c.source_proposal_turn_id,
                    "challengeTurnId": c.challenge_turn_id,
                    "status": c.status,
                    "currentReasonTurnId": c.current_reason_turn_id,
                    "reviews": list(c.reviews),
                    "createdAt": c.created_at,
                    "updatedAt": c.updated_at,
                }
                for c in bundle.challenges
            ],
            "playAttempts": [
                serialize_dashboard_play_attempt(a, stage_number=stage_num.get(a.version_id))
                for a in bundle.play_attempts
            ],
            "draftRegenerations": [
                {
                    "requestId": r.request_id,
                    "status": r.status,
                    "generation": r.generation,
                    "failureCode": r.failure_code,
                    "rows": list(r.rows) if r.rows is not None else None,
                    "validation": r.validation,
                    "requestedAt": r.requested_at,
                    "updatedAt": r.updated_at,
                }
                for r in bundle.draft_regenerations
            ],
            "binaryDiscoveryTopics": [
                {
                    "topicId": topic.topic_id,
                    "state": topic.state,
                    "updatedAt": topic.updated_at,
                }
                for topic in bundle.topics
            ],
            "componentRecoveryTasks": [
                {
                    "recoveryId": task.recovery_id,
                    "targetKind": task.target_kind,
                    "targetId": task.target_id,
                    "state": task.state,
                    "updatedAt": task.updated_at,
                }
                for task in bundle.recovery_tasks
            ],
            "auditEvents": [
                {
                    "id": ev.id,
                    "eventType": ev.event_type,
                    "payload": ev.payload,
                    "createdAt": ev.created_at,
                }
                for ev in bundle.audit_events
            ],
        }
    )


__all__ = [
    "SessionProjectionBundle",
    "build_authoritative_player_projection",
    "extract_solver_summary",
    "load_formal_session_bundles",
    "redact_secret_keys",
    "serialize_dashboard_draft_regeneration",
    "serialize_dashboard_play_attempt",
    "serialize_session_research_bundle",
]
