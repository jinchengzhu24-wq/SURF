"""Idempotent migration from legacy 8010 SQLite database to unified persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ._cocreation_migrate_ops import (
    insert_idempotency,
    migrate_audit_and_ops,
    normalize_finalize_request,
    preload_legacy_maps,
    row_dict,
)
from .database import SQLiteDatabase, dump_json, load_json

_LEGACY_CHALLENGE_STATUS_MAP: dict[str, str] = {
    "review_pending": "awaiting_reason",
    "reason_review": "awaiting_reason",
    "choice_pending": "awaiting_choice",
}

_LEGACY_DECISION_SCOPE_MAP: dict[str, str] = {
    "restore": "restore_version",
    "finalize": "finalize",
    "save": "save_version",
    "accept": "decide_proposal",
    "reject": "decide_proposal",
}


def _src_tables(src: sqlite3.Connection) -> set[str]:
    rows = src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _valid_session_ids(dst: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in dst.execute("SELECT session_id FROM design_sessions")}


def _valid_session_versions(dst: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in dst.execute("SELECT session_id, version_id FROM level_versions")
    }


def _migrate_core_tables(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    tables: set[str],
    maps: dict[str, Any],
    stats: dict[str, int],
) -> None:
    pending_regens: list[dict[str, Any]] = []
    if "design_sessions" in tables:
        for row in src.execute("SELECT * FROM design_sessions"):
            r = row_dict(row)
            sid = r["id"]
            intention = maps["intentions"].get(sid)
            locked_at = r.get("language_locked_at")
            has_started = bool(r.get("current_version_id") or r.get("deadline_started_at"))
            language_locked = 1 if (locked_at or r.get("language_locked") or has_started) else 0
            if language_locked and not locked_at:
                locked_at = r["created_at"]
            cur = dst.execute(
                "INSERT OR IGNORE INTO design_sessions("
                "session_id, match_id, player_slot, mode, language, language_locked, "
                "language_locked_at, status, secret_hash, access_token_hash, "
                "integration_token_hash, bootstrap_token_hash, bootstrap_used_at, "
                "creation_key, current_version_id, deadline_started_at, deadline_at, "
                "finalized_version_id, finalized_at, completed_at, final_intention_json, "
                "draft_rows_json, draft_validation_json, draft_generation, is_demo, "
                "created_at, updated_at, expires_at"
                ") VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, NULL"
                ")",
                (
                    sid, r.get("match_id"), r.get("player_number"), r["initial_draft_method"],
                    r["language"], language_locked, locked_at, r["status"],
                    r["access_hash"], r["integration_hash"], r["bootstrap_hash"],
                    r.get("bootstrap_used_at"), r.get("creation_key"), r.get("current_version_id"),
                    r.get("deadline_started_at"), r.get("deadline_at"), r.get("final_version_id"),
                    r.get("finalized_at"), r.get("completed_at"),
                    dump_json(intention) if intention is not None else None,
                    r.get("draft_rows_json"), r.get("draft_validation_json"),
                    int(r.get("draft_generation") or 1), int(r.get("demo_mode") or 0),
                    r["created_at"], r["updated_at"],
                ),
            )
            stats["sessions"] += cur.rowcount

            if r.get("draft_regeneration_request_id"):
                pending_regens.append(r)

    valid_sessions = _valid_session_ids(dst)
    for r in pending_regens:
        sid = r["id"]
        if sid not in valid_sessions:
            continue
        req_id = r["draft_regeneration_request_id"]
        rcur = dst.execute(
            "INSERT OR IGNORE INTO draft_regenerations("
            "request_id, session_id, idempotency_key, status, generation, "
            "failure_code, rows_json, validation_json, requested_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                req_id, sid, r.get("draft_regeneration_key") or req_id,
                r.get("draft_regeneration_status") or "completed",
                int(r.get("draft_generation") or 1),
                r.get("draft_regeneration_failure_code"),
                r.get("draft_rows_json"), r.get("draft_validation_json"),
                r.get("draft_regeneration_requested_at") or r["created_at"],
                r.get("draft_regeneration_updated_at") or r["updated_at"],
            ),
        )
        stats["draft_regenerations"] += rcur.rowcount

    if "level_versions" in tables:
        for row in src.execute("SELECT * FROM level_versions ORDER BY stage_number"):
            r = row_dict(row)
            if r["session_id"] not in valid_sessions:
                continue
            assessment = maps["assessments"].get(r["id"])
            cur = dst.execute(
                "INSERT OR IGNORE INTO level_versions("
                "version_id, session_id, version_number, source, summary, rows_json, "
                "diff_json, design_context_json, solver_json, entity_bindings_json, "
                "assessment_json, parent_version_id, idempotency_key, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"], r["session_id"], r["stage_number"], r["source"], r["summary"],
                    r["rows_json"], r.get("diff_json") or "[]", r.get("design_context_json"),
                    r["validation_json"], r.get("entity_bindings_json"),
                    dump_json(assessment) if assessment is not None else None,
                    r.get("parent_version_id"), r.get("idempotency_key"), r["created_at"],
                ),
            )
            stats["versions"] += cur.rowcount

    valid_versions = _valid_session_versions(dst)
    if "conversation_turns" in tables:
        for row in src.execute("SELECT * FROM conversation_turns ORDER BY sequence_number"):
            r = row_dict(row)
            if (r["session_id"], r["version_id"]) not in valid_versions:
                continue
            translations = maps["translations"].get(r["id"])
            cur = dst.execute(
                "INSERT OR IGNORE INTO conversation_turns("
                "turn_id, session_id, sequence_number, request_id, role, content, "
                "language, guidance_json, translations_json, proposal_binding_json, "
                "model, attempts_used, latency_ms, version_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"], r["session_id"], r["sequence_number"], r.get("request_id"),
                    r["role"], r["content"], r["language"], r.get("guidance_json"),
                    dump_json(translations) if translations is not None else None,
                    r.get("proposal_binding_json"), r.get("model"),
                    r.get("attempts_used"), r.get("latency_ms"),
                    r["version_id"], r["created_at"],
                ),
            )
            stats["turns"] += cur.rowcount


def _migrate_proposals_challenges_attempts(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    tables: set[str],
    maps: dict[str, Any],
    stats: dict[str, int],
) -> None:
    valid_sessions = _valid_session_ids(dst)
    valid_versions = _valid_session_versions(dst)

    if "change_proposals" in tables:
        for row in src.execute("SELECT * FROM change_proposals ORDER BY created_at"):
            r = row_dict(row)
            if (r["session_id"], r["base_version_id"]) not in valid_versions:
                continue
            dec = maps["decisions_by_proposal"].get(r["id"])
            decision_payload = (
                {
                    "id": dec["id"],
                    "decision": dec["decision_type"],
                    "reason": dec["reason"],
                    "versionId": dec["version_id"],
                    "idempotencyKey": dec["idempotency_key"],
                    "createdAt": dec["created_at"],
                }
                if dec is not None
                else None
            )
            proposal_payload = {
                "summary": r["summary"],
                "proposedRows": load_json(r.get("proposed_rows_json")),
                "diff": load_json(r.get("diff_json"), []),
                "validation": load_json(r.get("validation_json"), {}),
            }
            cur = dst.execute(
                "INSERT OR IGNORE INTO change_proposals("
                "proposal_id, session_id, assistant_turn_id, base_version_id, "
                "candidate_version_id, status, proposal_json, decision_json, "
                "idempotency_key, created_at, decided_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"], r["session_id"], r["assistant_turn_id"], r["base_version_id"],
                    dec["version_id"] if dec is not None else None, r["status"],
                    dump_json(proposal_payload),
                    dump_json(decision_payload) if decision_payload is not None else None,
                    r.get("idempotency_key"), r["created_at"], r.get("decided_at"),
                ),
            )
            stats["proposals"] += cur.rowcount
            if dec is not None and dec.get("idempotency_key"):
                clean_reason = str(dec.get("reason") or "").strip()[:2000]
                dp_req = {
                    "proposalId": r["id"],
                    "decision": dec["decision_type"],
                    "base": r["base_version_id"],
                    "reason": clean_reason,
                }
                dp_resp = {
                    "proposalId": r["id"],
                    "decision": dec["decision_type"],
                    "versionId": dec["version_id"],
                    **({"reason": clean_reason} if clean_reason else {}),
                }
                stats["idempotency_keys"] += insert_idempotency(
                    dst,
                    dec["session_id"],
                    "decide_proposal",
                    dec["idempotency_key"],
                    dp_req,
                    "completed",
                    dp_resp,
                    dec["created_at"],
                )

    stage_restored_by_vid = maps.get("stage_restored_by_vid") or {}
    finalize_audit_by_key = maps.get("finalize_audit_by_key") or {}
    version_meta = maps.get("version_meta") or {}

    for dec in maps["other_decisions"]:
        if dec["session_id"] not in valid_sessions:
            continue
        dec_type = str(dec["decision_type"] or "").strip()
        scope = _LEGACY_DECISION_SCOPE_MAP.get(dec_type, dec_type)
        clean_reason = str(dec.get("reason") or "").strip()
        if scope == "restore_version":
            rest_audit = stage_restored_by_vid.get(dec["version_id"]) or {}
            ver_info = version_meta.get(dec["version_id"]) or {}
            source_vid = (
                rest_audit.get("restoredFromVersionId")
                or ver_info.get("parent_version_id")
                or dec["version_id"]
            )
            base_vid = (
                rest_audit.get("baseVersionId")
                or rest_audit.get("replacedVersionId")
                or ver_info.get("parent_version_id")
                or dec["version_id"]
            )
            req_payload: Any = {"versionId": source_vid, "baseVersionId": base_vid}
            resp_payload: Any = {
                "versionId": dec["version_id"],
                "decisionId": dec["id"],
                **({"reason": clean_reason} if clean_reason else {}),
            }
        elif scope == "finalize":
            fin_audit = finalize_audit_by_key.get(
                (dec["session_id"], dec.get("idempotency_key") or "")
            ) or {}
            req_payload = normalize_finalize_request(
                base_version_id=dec["version_id"],
                raw_request=fin_audit.get("request"),
                deadline_stage_version_id=fin_audit.get("deadlineStageVersionId"),
                version_meta=version_meta,
            )
            resp_payload = {
                "finalVersionId": fin_audit.get("finalVersionId") or dec["version_id"],
                **(
                    {"deadlineStageVersionId": fin_audit["deadlineStageVersionId"]}
                    if fin_audit.get("deadlineStageVersionId")
                    else {}
                ),
            }
        else:
            req_payload = {"decision": dec_type, "versionId": dec["version_id"]}
            resp_payload = {
                "versionId": dec["version_id"],
                "decisionId": dec["id"],
                **({"reason": clean_reason} if clean_reason else {}),
            }
        stats["idempotency_keys"] += insert_idempotency(
            dst, dec["session_id"], scope, dec["idempotency_key"],
            req_payload, "completed", resp_payload, dec["created_at"],
        )

    if "revision_challenges" in tables:
        for row in src.execute("SELECT * FROM revision_challenges"):
            r = row_dict(row)
            if (r["session_id"], r["base_version_id"]) not in valid_versions:
                continue
            reviews = maps["reviews_by_challenge"].get(r["challenge_id"], [])
            raw_status = str(r["status"])
            normalized_status = _LEGACY_CHALLENGE_STATUS_MAP.get(raw_status, raw_status)
            cur = dst.execute(
                "INSERT OR IGNORE INTO revision_challenges("
                "challenge_id, session_id, base_version_id, source_proposal_turn_id, "
                "challenge_turn_id, status, current_reason_turn_id, reviews_json, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["challenge_id"], r["session_id"], r["base_version_id"],
                    r["source_proposal_turn_id"], r["challenge_turn_id"], normalized_status,
                    r.get("current_reason_turn_id"), dump_json(reviews),
                    r["created_at"], r["updated_at"],
                ),
            )
            stats["challenges"] += cur.rowcount

    if "play_attempts" in tables:
        for row in src.execute("SELECT * FROM play_attempts"):
            r = row_dict(row)
            if (r["session_id"], r["version_id"]) not in valid_versions:
                continue
            min_moves = int(r["minimum_moves"]) if r.get("minimum_moves") is not None else -1
            min_pushes = int(r["minimum_pushes"]) if r.get("minimum_pushes") is not None else -1
            cur = dst.execute(
                "INSERT OR IGNORE INTO play_attempts("
                "attempt_id, session_id, version_id, mode, language, status, ticket_hash, "
                "ticket_expires_at, ticket_used_at, attempt_token_hash, issued_at, "
                "loaded_at, first_move_at, finished_at, duration_seconds, move_count, "
                "push_count, restart_count, minimum_moves, minimum_pushes, idempotency_key"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"], r["session_id"], r["version_id"], r["initial_draft_method"],
                    r["language"], r["status"], r["ticket_hash"], r["ticket_expires_at"],
                    r.get("ticket_used_at"), r.get("attempt_token_hash"), r["issued_at"],
                    r.get("loaded_at"), r.get("first_move_at"), r.get("finished_at"),
                    float(r.get("duration_seconds") or 0.0), int(r.get("move_count") or 0),
                    int(r.get("push_count") or 0), int(r.get("restart_count") or 0),
                    min_moves, min_pushes, r["idempotency_key"],
                ),
            )
            stats["play_attempts"] += cur.rowcount


def migrate_cocreation_db(
    target_db: SQLiteDatabase, source_sqlite_path: Path | str
) -> dict[str, int]:
    """Migrate a legacy 8010 SQLite DB into the unified schema idempotently."""
    src_path = Path(source_sqlite_path)
    if not src_path.exists():
        raise FileNotFoundError(f"Source co-creation SQLite DB does not exist: {src_path}")

    src = sqlite3.connect(f"file:{src_path.resolve().as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    stats: dict[str, int] = {
        "sessions": 0, "versions": 0, "turns": 0, "proposals": 0, "challenges": 0,
        "challenge_review_requests": 0, "play_attempts": 0, "draft_regenerations": 0,
        "audit_events": 0, "binary_topics": 0, "recovery_tasks": 0, "idempotency_keys": 0,
    }
    try:
        tables = _src_tables(src)
        maps = preload_legacy_maps(src, tables)
        with target_db.write_tx() as dst:
            _migrate_core_tables(src, dst, tables, maps, stats)
            _migrate_proposals_challenges_attempts(src, dst, tables, maps, stats)
            migrate_audit_and_ops(src, dst, tables, stats, maps=maps)
    finally:
        src.close()
    return stats
