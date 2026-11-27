"""Preloading and operational/audit extraction helpers for 8010 DB migration."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from .database import dump_json, load_json

_LEGACY_MESSAGE_ACTION_MAP: dict[str, str] = {
    "none": "send",
}


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


def normalize_message_request(
    raw_request: Mapping[str, Any] | None,
    *,
    fallback_base_version_id: str | None = None,
    fallback_content: str = "",
    fallback_request_proposal: bool = False,
) -> dict[str, Any]:
    """Build canonical 9-key request dict matching DialogueOrchestrator.send_message."""
    req = raw_request if isinstance(raw_request, Mapping) else {}
    raw_action = str(req.get("action") or "send").strip() or "send"
    action = _LEGACY_MESSAGE_ACTION_MAP.get(raw_action, raw_action)
    base_vid = req.get("baseVersionId")
    if base_vid is None:
        base_vid = fallback_base_version_id
    content = str(
        req.get("content") if req.get("content") is not None else fallback_content
    ).strip()
    return {
        "action": action,
        "baseVersionId": base_vid,
        "content": content,
        "requestProposal": bool(req.get("requestProposal", fallback_request_proposal)),
        "assistantTurnId": req.get("assistantTurnId"),
        "proposalId": req.get("proposalId"),
        "challengeId": req.get("challengeId"),
        "challengeChoice": req.get("challengeChoice"),
        "exitChallenge": bool(req.get("exitChallenge", False)),
    }


def normalize_finalize_request(
    *,
    base_version_id: str | None,
    raw_request: Mapping[str, Any] | None,
    deadline_stage_version_id: str | None,
    version_meta: Mapping[str, dict[str, Any]],
) -> Any:
    """Build canonical request dict matching finalize_session_impl."""
    req = raw_request if isinstance(raw_request, Mapping) else {}
    resolved_base = req.get("baseVersionId") or base_version_id
    rows_hash = req.get("rowsHash")
    if rows_hash is None:
        raw_rows = req.get("rows")
        return {
            "baseVersionId": resolved_base,
            "rows": list(raw_rows) if isinstance(raw_rows, list) and raw_rows else None,
        }
    if deadline_stage_version_id and deadline_stage_version_id in version_meta:
        dl_rows = version_meta[deadline_stage_version_id].get("rows")
        if isinstance(dl_rows, list) and hash_json(dl_rows) == rows_hash:
            return {"baseVersionId": resolved_base, "rows": list(dl_rows)}
    return {"baseVersionId": resolved_base, "rowsHash": rows_hash}


def _normalize_binary_receipt(
    payload: Mapping[str, Any],
) -> tuple[str, str | None, Any, str]:
    key = payload.get("key")
    clean_key = str(key) if key else None
    raw_status = str(payload.get("status") or "in_progress")
    if raw_status == "done":
        status_val = "completed"
    elif raw_status == "pending":
        status_val = "in_progress"
    else:
        status_val = raw_status

    req = payload.get("request")
    if isinstance(req, Mapping):
        kind = str(req.get("kind") or "").strip().lower()
        if kind == "start":
            return ("message", clean_key, normalize_message_request(req), status_val)
        if kind == "choice" or any(k in req for k in ("topicId", "questionId", "choiceId")):
            canonical_choice = {
                "topicId": req.get("topicId"),
                "questionId": req.get("questionId"),
                "choiceId": req.get("choiceId"),
                "baseVersionId": req.get("baseVersionId"),
                "displayLanguage": req.get("displayLanguage"),
            }
            return ("binary_choice", clean_key, canonical_choice, status_val)
    return ("binary_choice", clean_key, req, status_val)


def _normalize_intent_feedback_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    req = payload.get("request") if isinstance(payload.get("request"), Mapping) else {}
    return {
        "hypothesisId": req.get("hypothesisId") or payload.get("hypothesisId"),
        "action": req.get("action") or payload.get("action"),
        "baseVersionId": req.get("baseVersionId") or payload.get("versionId"),
        "candidateText": str(
            req.get("candidateText") or payload.get("candidateText") or ""
        ).strip(),
        "sourceTurnId": req.get("sourceTurnId") or payload.get("sourceTurnId"),
    }


def _normalize_question_feedback_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    req = payload.get("request") if isinstance(payload.get("request"), Mapping) else {}
    return {
        "questionId": req.get("questionId") or payload.get("questionId"),
        "action": req.get("action") or payload.get("action"),
        "baseVersionId": req.get("baseVersionId") or payload.get("versionId"),
    }


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

    version_meta: dict[str, dict[str, Any]] = {}
    if "level_versions" in tables:
        for row in src.execute("SELECT id, parent_version_id, rows_json FROM level_versions"):
            version_meta[row["id"]] = {
                "parent_version_id": row["parent_version_id"],
                "rows": load_json(row["rows_json"], []),
            }

    turn_contents: dict[str, str] = {}
    user_turns_by_req: dict[tuple[str, str], dict[str, Any]] = {}
    assistant_req_ids: set[tuple[str, str]] = set()
    if "conversation_turns" in tables:
        for row in src.execute(
            "SELECT id, session_id, role, content, version_id, request_id "
            "FROM conversation_turns ORDER BY sequence_number ASC"
        ):
            turn_contents[row["id"]] = str(row["content"] or "")
            req_id = row["request_id"]
            if req_id:
                key_pair = (str(row["session_id"]), str(req_id))
                if row["role"] == "user":
                    user_turns_by_req[key_pair] = {
                        "turn_id": row["id"],
                        "content": str(row["content"] or ""),
                        "version_id": row["version_id"],
                    }
                elif row["role"] == "assistant":
                    assistant_req_ids.add(key_pair)

    assessments: dict[str, dict[str, Any]] = {}
    if "llm_assessments" in tables:
        for row in src.execute("SELECT * FROM llm_assessments"):
            raw_payload = load_json(row["payload_json"], {})
            flat_assessment: dict[str, Any] = (
                dict(raw_payload) if isinstance(raw_payload, dict) else {}
            )
            if row["assistant_turn_id"]:
                flat_assessment.setdefault("assistantTurnId", row["assistant_turn_id"])
            if row["prompt_version"]:
                flat_assessment.setdefault("promptVersion", row["prompt_version"])
            if row["id"]:
                flat_assessment.setdefault("assessmentId", row["id"])
            if row["created_at"]:
                flat_assessment.setdefault("createdAt", row["created_at"])
            assessments[row["version_id"]] = flat_assessment

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

    review_requests_by_review_id: dict[str, list[dict[str, Any]]] = {}
    all_review_requests: list[tuple[str, str, dict[str, Any]]] = []
    if "challenge_review_requests" in tables:
        for row in src.execute(
            "SELECT * FROM challenge_review_requests ORDER BY created_at, idempotency_key"
        ):
            req_entry: dict[str, Any] = {
                "idempotencyKey": row["idempotency_key"],
                "reviewId": row["review_id"],
                "status": row["status"],
                "result": load_json(row["result_json"]),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            review_requests_by_review_id.setdefault(row["review_id"], []).append(req_entry)
            all_review_requests.append((str(row["session_id"]), str(row["review_id"]), req_entry))

    reviews_by_challenge: dict[str, list[dict[str, Any]]] = {}
    review_challenge_map: dict[str, str] = {}
    if "challenge_reason_reviews" in tables:
        for row in src.execute(
            "SELECT * FROM challenge_reason_reviews ORDER BY created_at, review_id"
        ):
            review_id = str(row["review_id"])
            challenge_id = str(row["challenge_id"])
            review_challenge_map[review_id] = challenge_id
            raw_result = load_json(row["result_json"])
            attached_reqs = review_requests_by_review_id.get(review_id, [])
            result_dict: Mapping[str, Any]
            if isinstance(raw_result, Mapping):
                result_dict = raw_result
            else:
                fallback_res = next(
                    (
                        item["result"]
                        for item in reversed(attached_reqs)
                        if isinstance(item.get("result"), Mapping)
                    ),
                    {},
                )
                result_dict = fallback_res if isinstance(fallback_res, Mapping) else {}
            source_tid = str(row["source_user_turn_id"])
            reason_text = str(
                turn_contents.get(source_tid) or result_dict.get("reason") or ""
            ).strip()
            entry: dict[str, Any] = {
                "reasonTurnId": source_tid,
                "reason": reason_text,
                "relation": str(result_dict.get("relation") or ""),
                "merit": str(result_dict.get("merit") or ""),
                "comparison": str(result_dict.get("comparison") or ""),
                "createdAt": row["created_at"],
                "reviewId": review_id,
                "sourceUserTurnId": source_tid,
                "messageKey": row["message_key"],
                "status": row["status"],
                "attemptsUsed": row["attempts_used"],
                "failureCode": row["failure_code"],
                "result": raw_result,
                "updatedAt": row["updated_at"],
            }
            if attached_reqs:
                entry["reviewRequests"] = list(attached_reqs)
            reviews_by_challenge.setdefault(challenge_id, []).append(entry)

    stage_restored_by_vid: dict[str, dict[str, Any]] = {}
    finalize_audit_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    if "audit_events" in tables:
        for row in src.execute(
            "SELECT session_id, event_type, payload_json FROM audit_events "
            "WHERE event_type IN ('stage_restored', 'finalization_request_committed') "
            "ORDER BY id ASC"
        ):
            payload = load_json(row["payload_json"], {})
            if not isinstance(payload, dict):
                continue
            if row["event_type"] == "stage_restored" and payload.get("versionId"):
                stage_restored_by_vid[str(payload["versionId"])] = payload
            elif (
                row["event_type"] == "finalization_request_committed"
                and payload.get("idempotencyKey")
            ):
                finalize_audit_by_key[(str(row["session_id"]), str(payload["idempotencyKey"]))] = (
                    payload
                )

    return {
        "intentions": intentions,
        "version_meta": version_meta,
        "user_turns_by_req": user_turns_by_req,
        "assistant_req_ids": assistant_req_ids,
        "assessments": assessments,
        "translations": translations,
        "decisions_by_proposal": decisions_by_proposal,
        "other_decisions": other_decisions,
        "reviews_by_challenge": reviews_by_challenge,
        "review_challenge_map": review_challenge_map,
        "all_review_requests": all_review_requests,
        "stage_restored_by_vid": stage_restored_by_vid,
        "finalize_audit_by_key": finalize_audit_by_key,
    }


def migrate_audit_and_ops(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    tables: set[str],
    stats: dict[str, int],
    *,
    maps: Mapping[str, Any] | None = None,
) -> None:
    valid_sessions = {
        str(row[0]) for row in dst.execute("SELECT session_id FROM design_sessions")
    }
    existing_audits = {
        (row["session_id"], row["event_type"], row["created_at"], row["payload_json"])
        for row in dst.execute(
            "SELECT session_id, event_type, created_at, payload_json FROM audit_events"
        )
    }
    latest_topics: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
    latest_recovery: dict[str, tuple[str, str, str, dict[str, Any], str]] = {}
    # Deduplicate in-pass idempotency keys keeping latest status (e.g. pending -> done).
    pending_idem: dict[tuple[str, str, str], tuple[Any, str, Any, str]] = {}

    version_meta: Mapping[str, dict[str, Any]] = (maps or {}).get("version_meta") or {}
    user_turns_by_req: Mapping[tuple[str, str], dict[str, Any]] = (
        (maps or {}).get("user_turns_by_req") or {}
    )
    assistant_req_ids: set[tuple[str, str]] = (maps or {}).get("assistant_req_ids") or set()

    if "audit_events" in tables:
        for row in src.execute("SELECT * FROM audit_events ORDER BY id ASC"):
            sid, etype, pjson, created_at = (
                row["session_id"],
                row["event_type"],
                row["payload_json"],
                row["created_at"],
            )
            if sid not in valid_sessions:
                continue
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
                target_id = str(
                    payload.get("disagreementId") or payload.get("baseVersionId") or ""
                )
                latest_recovery[str(payload["recoveryId"])] = (
                    sid,
                    str(payload.get("component") or "human_edit_engagement"),
                    target_id,
                    payload,
                    str(payload.get("updatedAt") or created_at),
                )
            elif etype == "binary_discovery_receipt":
                scope, key, req_payload, status_val = _normalize_binary_receipt(payload)
                if key:
                    pending_idem[(sid, scope, key)] = (req_payload, status_val, None, created_at)
            elif etype == "finalization_request_committed":
                idem_key = payload.get("idempotencyKey")
                if idem_key:
                    req_payload = normalize_finalize_request(
                        base_version_id=payload.get("finalVersionId"),
                        raw_request=payload.get("request"),
                        deadline_stage_version_id=payload.get("deadlineStageVersionId"),
                        version_meta=version_meta,
                    )
                    resp_payload = {
                        "finalVersionId": payload.get("finalVersionId"),
                        **(
                            {"deadlineStageVersionId": payload["deadlineStageVersionId"]}
                            if payload.get("deadlineStageVersionId")
                            else {}
                        ),
                    }
                    pending_idem[(sid, "finalize", str(idem_key))] = (
                        req_payload,
                        "completed",
                        resp_payload,
                        created_at,
                    )
            elif etype == "message_generation_failed":
                msg_key = payload.get("messageKey")
                if msg_key:
                    clean_key = str(msg_key)
                    fallback_turn = user_turns_by_req.get((sid, clean_key))
                    raw_sig = payload.get("requestSignature")
                    req_payload = normalize_message_request(
                        raw_sig if isinstance(raw_sig, Mapping) else None,
                        fallback_base_version_id=(
                            payload.get("baseVersionId")
                            or (fallback_turn["version_id"] if fallback_turn else None)
                        ),
                        fallback_content=fallback_turn["content"] if fallback_turn else "",
                        fallback_request_proposal=bool(payload.get("proposalRequested", False)),
                    )
                    status_val = "completed" if (sid, clean_key) in assistant_req_ids else "failed"
                    pending_idem[(sid, "message", clean_key)] = (
                        req_payload,
                        status_val,
                        payload,
                        created_at,
                    )
            elif etype in {
                "intent_hypothesis_feedback_applied",
                "intent_hypothesis_feedback_pending",
            }:
                idem_key = payload.get("idempotencyKey")
                if idem_key:
                    pending_idem[(sid, "intent_feedback", str(idem_key))] = (
                        _normalize_intent_feedback_request(payload),
                        "completed",
                        payload,
                        created_at,
                    )
            elif etype == "question_feedback_applied":
                idem_key = payload.get("idempotencyKey")
                if idem_key:
                    pending_idem[(sid, "question_feedback", str(idem_key))] = (
                        _normalize_question_feedback_request(payload),
                        "completed",
                        payload,
                        created_at,
                    )

    for (sid, scope, key), (req_payload, status_val, resp_payload, created_at) in (
        pending_idem.items()
    ):
        stats["idempotency_keys"] += insert_idempotency(
            dst, sid, scope, key, req_payload, status_val, resp_payload, created_at
        )

    review_challenge_map: Mapping[str, str] = (maps or {}).get("review_challenge_map") or {}
    for sid, rev_id, req_entry in (maps or {}).get("all_review_requests") or ():
        if sid not in valid_sessions:
            continue
        crr_payload = {
            "reviewId": rev_id,
            "challengeId": review_challenge_map.get(rev_id),
            "idempotencyKey": req_entry["idempotencyKey"],
            "status": req_entry["status"],
            "result": req_entry["result"],
            "updatedAt": req_entry["updatedAt"],
        }
        crr_json = dump_json(crr_payload)
        created_at = str(req_entry["createdAt"])
        audit_key = (sid, "challenge_review_request_migrated", created_at, crr_json)
        if audit_key not in existing_audits:
            dst.execute(
                "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, "challenge_review_request_migrated", crr_json, created_at),
            )
            existing_audits.add(audit_key)
            stats["audit_events"] += 1
            stats["challenge_review_requests"] = stats.get("challenge_review_requests", 0) + 1

    for (sid, topic_id), (state, updated_at) in latest_topics.items():
        if sid not in valid_sessions:
            continue
        cur = dst.execute(
            "INSERT INTO binary_discovery_topics(session_id, topic_id, state_json, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id, topic_id) DO UPDATE SET "
            "state_json = excluded.state_json, updated_at = excluded.updated_at",
            (sid, topic_id, dump_json(state), updated_at),
        )
        stats["binary_topics"] += cur.rowcount

    for rec_id, (sid, kind, target_id, state, updated_at) in latest_recovery.items():
        if sid not in valid_sessions:
            continue
        cur = dst.execute(
            "INSERT INTO component_recovery_tasks("
            "recovery_id, session_id, target_kind, target_id, state_json, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(recovery_id) DO UPDATE SET "
            "state_json = excluded.state_json, updated_at = excluded.updated_at",
            (rec_id, sid, kind, target_id, dump_json(state), updated_at),
        )
        stats["recovery_tasks"] += cur.rowcount
