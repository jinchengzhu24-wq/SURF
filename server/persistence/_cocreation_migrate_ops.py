"""Preloading and operational/audit extraction helpers for 8010 DB migration."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from .database import dump_json, load_json


def hash_json(value: Any) -> str:
    return hashlib.sha256(dump_json(value).encode("utf-8")).hexdigest()


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def insert_idempotency(
    conn: sqlite3.Connection,
    session_id: str,
    scope: str,
    key: str | None,
    request_payload: Any,
    status: str,
    response_payload: Any,
    created_at: str,
) -> int:
    if not key:
        return 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO idempotency_keys("
        "session_id, scope, idempotency_key, request_hash, status, response_json, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            scope,
            key,
            hash_json(request_payload),
            status,
            dump_json(response_payload) if response_payload is not None else None,
            created_at,
        ),
    )
    return cur.rowcount


def preload_legacy_maps(src: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    intentions: dict[str, dict[str, Any]] = {}
    if "designer_intentions" in tables:
        for row in src.execute("SELECT * FROM designer_intentions"):
            intentions[row["session_id"]] = {
                "id": row["id"],
                "content": row["content"],
                "language": row["language"],
                "idempotencyKey": row["idempotency_key"],
                "createdAt": row["created_at"],
            }

    assessments: dict[str, dict[str, Any]] = {}
    if "llm_assessments" in tables:
        for row in src.execute("SELECT * FROM llm_assessments"):
            assessments[row["version_id"]] = {
                "id": row["id"],
                "assistantTurnId": row["assistant_turn_id"],
                "payload": load_json(row["payload_json"], {}),
                "promptVersion": row["prompt_version"],
                "createdAt": row["created_at"],
            }

    translations: dict[str, dict[str, Any]] = {}
    if "turn_translations" in tables:
        for row in src.execute("SELECT * FROM turn_translations ORDER BY created_at"):
            translations.setdefault(row["turn_id"], {})[row["language"]] = {
                "id": row["id"],
                "body": row["body"],
                "guidance": load_json(row["guidance_json"]),
                "proposalSummary": row["proposal_summary"],
                "model": row["model"],
                "attemptsUsed": row["attempts_used"],
                "latencyMs": row["latency_ms"],
                "createdAt": row["created_at"],
            }

    decisions_by_proposal: dict[str, dict[str, Any]] = {}
    other_decisions: list[dict[str, Any]] = []
    if "designer_decisions" in tables:
        for row in src.execute("SELECT * FROM designer_decisions ORDER BY created_at"):
            item = row_dict(row)
            if item.get("proposal_id"):
                decisions_by_proposal[item["proposal_id"]] = item
            else:
                other_decisions.append(item)

    reviews_by_challenge: dict[str, list[dict[str, Any]]] = {}
    if "challenge_reason_reviews" in tables:
        for row in src.execute(
            "SELECT * FROM challenge_reason_reviews ORDER BY created_at, review_id"
        ):
            reviews_by_challenge.setdefault(row["challenge_id"], []).append(
                {
                    "reviewId": row["review_id"],
                    "sourceUserTurnId": row["source_user_turn_id"],
                    "messageKey": row["message_key"],
                    "status": row["status"],
                    "attemptsUsed": row["attempts_used"],
                    "failureCode": row["failure_code"],
                    "result": load_json(row["result_json"]),
                    "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"],
                }
            )

    return {
        "intentions": intentions,
        "assessments": assessments,
        "translations": translations,
        "decisions_by_proposal": decisions_by_proposal,
        "other_decisions": other_decisions,
        "reviews_by_challenge": reviews_by_challenge,
    }


def migrate_audit_and_ops(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    tables: set[str],
    stats: dict[str, int],
) -> None:
    if "audit_events" not in tables:
        return
    existing_audits = {
        (row["session_id"], row["event_type"], row["created_at"], row["payload_json"])
        for row in dst.execute(
            "SELECT session_id, event_type, created_at, payload_json FROM audit_events"
        )
    }
    latest_topics: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
    latest_recovery: dict[str, tuple[str, str, str, dict[str, Any], str]] = {}

    for row in src.execute("SELECT * FROM audit_events ORDER BY id ASC"):
        sid, etype, pjson, created_at = (
            row["session_id"],
            row["event_type"],
            row["payload_json"],
            row["created_at"],
        )
        if (sid, etype, created_at, pjson) not in existing_audits:
            dst.execute(
                "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, etype, pjson, created_at),
            )
            existing_audits.add((sid, etype, created_at, pjson))
            stats["audit_events"] += 1

        payload = load_json(pjson, {})
        if not isinstance(payload, dict):
            continue
        if etype == "binary_discovery_state" and payload.get("topicId"):
            latest_topics[sid, str(payload["topicId"])] = (payload, created_at)
        elif etype == "component_recovery" and payload.get("recoveryId"):
            target_id = str(payload.get("disagreementId") or payload.get("baseVersionId") or "")
            latest_recovery[str(payload["recoveryId"])] = (
                sid,
                str(payload.get("component") or "human_edit_engagement"),
                target_id,
                payload,
                str(payload.get("updatedAt") or created_at),
            )
        elif etype == "binary_discovery_receipt":
            raw_status = str(payload.get("status") or "in_progress")
            status_val = "completed" if raw_status == "done" else raw_status
            stats["idempotency_keys"] += insert_idempotency(
                dst, sid, "binary_discovery", payload.get("key"),
                payload.get("request"), status_val, None, created_at,
            )
        elif etype == "finalization_request_committed":
            stats["idempotency_keys"] += insert_idempotency(
                dst, sid, "finalize", payload.get("idempotencyKey"),
                payload.get("request"), "completed", payload, created_at,
            )
        elif etype == "message_generation_failed":
            stats["idempotency_keys"] += insert_idempotency(
                dst, sid, "message", payload.get("messageKey"),
                payload.get("requestSignature") or payload, "failed", payload, created_at,
            )
        elif etype in {
            "intent_hypothesis_feedback_applied",
            "intent_hypothesis_feedback_pending",
        }:
            stats["idempotency_keys"] += insert_idempotency(
                dst, sid, "intent_feedback", payload.get("idempotencyKey"),
                payload.get("request") or payload, "completed", payload, created_at,
            )
        elif etype == "question_feedback_applied":
            stats["idempotency_keys"] += insert_idempotency(
                dst, sid, "question_feedback", payload.get("idempotencyKey"),
                payload, "completed", payload, created_at,
            )

    for (sid, topic_id), (state, updated_at) in latest_topics.items():
        cur = dst.execute(
            "INSERT INTO binary_discovery_topics(session_id, topic_id, state_json, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id, topic_id) DO UPDATE SET "
            "state_json = excluded.state_json, updated_at = excluded.updated_at",
            (sid, topic_id, dump_json(state), updated_at),
        )
        stats["binary_topics"] += cur.rowcount

    for rec_id, (sid, kind, target_id, state, updated_at) in latest_recovery.items():
        cur = dst.execute(
            "INSERT INTO component_recovery_tasks("
            "recovery_id, session_id, target_kind, target_id, state_json, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(recovery_id) DO UPDATE SET "
            "state_json = excluded.state_json, updated_at = excluded.updated_at",
            (rec_id, sid, kind, target_id, dump_json(state), updated_at),
        )
        stats["recovery_tasks"] += cur.rowcount
