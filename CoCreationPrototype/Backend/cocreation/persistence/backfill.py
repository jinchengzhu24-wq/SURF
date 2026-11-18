import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from cocreation.reliability.component_recovery import interaction_state
from cocreation.domain.design_context import (
    add_confirmed_decision,
    add_open_question,
    add_rejected_decision,
    apply_question_answer_review,
    deduplicate_open_questions,
    design_level_open_questions,
    empty_design_context,
    infer_intent_topic,
    merge_chat_update,
    normalize_design_context,
    question_dedup_key,
    set_active_disagreement,
)
from cocreation.domain.level_validation import (
    build_entity_bindings,
    build_untrusted_entity_bindings,
    derive_entity_transitions,
    entity_binding_fingerprint,
    entity_bindings_match_rows,
)

from .database import (
    _public_guidance,
    dump_json,
    load_json,
    map_fingerprint,
    record_event,
)


def _utc_now_iso(clock=None):
    if clock is not None:
        return clock.utc_now()
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_design_context(database, session_id, version_id):
    row = database.execute(
        "SELECT design_context_json FROM level_versions WHERE session_id = ? AND id = ?",
        (session_id, version_id),
    ).fetchone()
    if row is None:
        return empty_design_context()
    raw = row["design_context_json"]
    if not raw:
        return empty_design_context()
    try:
        return normalize_design_context(load_json(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return empty_design_context()


def save_design_context(database, version_id, context):
    normalized = normalize_design_context(context)
    database.execute(
        "UPDATE level_versions SET design_context_json = ? WHERE id = ?",
        (dump_json(normalized), version_id),
    )
    return normalized


def load_entity_bindings(database, session_id, version_id):
    row = database.execute(
        "SELECT entity_bindings_json FROM level_versions WHERE session_id = ? AND id = ?",
        (session_id, version_id),
    ).fetchone()
    if row is None or not row["entity_bindings_json"]:
        return None
    try:
        value = load_json(row["entity_bindings_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def save_entity_bindings(database, version_id, bindings):
    if not isinstance(bindings, dict):
        raise ValueError("entity bindings must be an object")
    normalized = dict(bindings)
    normalized["bindingFingerprint"] = entity_binding_fingerprint(normalized)
    database.execute(
        "UPDATE level_versions SET entity_bindings_json = ? WHERE id = ?",
        (dump_json(normalized), version_id),
    )
    return normalized


def _has_valid_design_context(raw):
    if not raw:
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("schemaVersion") == 6


def _legacy_visible_questions(content, guidance):
    """Extract questions from fields old clients actually rendered, excluding intent cards."""
    guidance = guidance if isinstance(guidance, dict) else {}
    values = [content, guidance.get("followUpQuestion")]
    disagreement = guidance.get("disagreement")
    if isinstance(disagreement, dict):
        values.extend([disagreement.get("coreDisagreement"), disagreement.get("nextQuestion")])
    offer = guidance.get("proposalOffer")
    if isinstance(offer, dict):
        values.extend([offer.get("summary"), offer.get("rationale")])
    values.extend(
        cue.get("text")
        for cue in guidance.get("uiCues") or []
        if isinstance(cue, dict)
    )
    result = []
    seen = set()
    for value in values:
        for segment in re.split(
            r"\s*(?:\n+|(?<=[.!?\u3002\uFF01\uFF1F]))\s*", str(value or "")
        ):
            question = re.sub(r"\s+", " ", segment).strip()
            key = question.strip("?\uFF1F").casefold()
            if question.endswith(("?", "\uFF1F")) and key and key not in seen:
                seen.add(key)
                result.append(question[:1200])
    return result


def _has_valid_entity_bindings(raw, rows=None):
    if not raw:
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not (
        isinstance(value, dict)
        and value.get("schemaVersion") == 1
        and isinstance(value.get("entities"), list)
        and bool(value.get("mapFingerprint"))
        and value.get("bindingFingerprint") == entity_binding_fingerprint(value)
    ):
        return False
    return rows is None or entity_bindings_match_rows(value, rows)


def backfill_entity_bindings(database):
    """Seed persistent entity identities without making model-based guesses.

    Existing versions are processed in Stage order.  The level validation
    layer only inherits unchanged or uniquely provable moves; ambiguous legacy
    edits are deliberately recorded as partial/unknown.
    """
    sessions = database.execute(
        "SELECT id FROM design_sessions ORDER BY created_at, id"
    ).fetchall()
    for session in sessions:
        versions = database.execute(
            "SELECT * FROM level_versions WHERE session_id = ? ORDER BY stage_number, created_at",
            (session["id"],),
        ).fetchall()
        for version in versions:
            rows = load_json(version["rows_json"])
            if _has_valid_entity_bindings(version["entity_bindings_json"], rows):
                continue
            parent = (
                database.execute(
                    "SELECT * FROM level_versions WHERE session_id = ? AND id = ?",
                    (session["id"], version["parent_version_id"]),
                ).fetchone()
                if version["parent_version_id"]
                else None
            )
            parent_bindings = (
                load_json(parent["entity_bindings_json"])
                if parent is not None and parent["entity_bindings_json"]
                else None
            )
            parent_rows = load_json(parent["rows_json"]) if parent is not None else None
            try:
                transitions = (
                    derive_entity_transitions(parent_rows, rows)
                    if parent_rows is not None
                    else None
                )
                bindings = build_entity_bindings(
                    rows,
                    parent_bindings=parent_bindings,
                    entity_transitions=transitions,
                    source="legacy_backfill",
                )
            except (ValueError, TypeError):
                # Historical semantic validation rules must not prevent the
                # service from starting. These identities are not execution
                # safe and remain unknown until a valid child Stage proves
                # them again.
                bindings = build_untrusted_entity_bindings(
                    rows,
                    source="legacy_untrusted",
                )
            database.execute(
                "UPDATE level_versions SET entity_bindings_json = ? WHERE id = ?",
                (dump_json(bindings), version["id"]),
            )
            record_event(
                database,
                session["id"],
                "entity_binding_backfilled",
                {
                    "versionId": version["id"],
                    "stageNumber": version["stage_number"],
                    "identityStatus": bindings["identityStatus"],
                    "bindingFingerprint": bindings["bindingFingerprint"],
                },
                version["created_at"],
            )


def backfill_design_contexts(database, *, clock=None):
    """Conservatively seed snapshots for databases created before DesignContext.

    This performs no model calls. Only user turns, formal decisions, and
    explicitly structured legacy disagreement fields are considered. Historical
    assistant intent prose is deliberately not promoted into semantic memory.
    """
    sessions = database.execute("SELECT id FROM design_sessions ORDER BY created_at, id").fetchall()
    changed = 0
    for session in sessions:
        versions = database.execute(
            "SELECT * FROM level_versions WHERE session_id = ? ORDER BY stage_number, created_at",
            (session["id"],),
        ).fetchall()
        for version in versions:
            if _has_valid_design_context(version["design_context_json"]):
                continue

            legacy_context = None
            legacy_schema_version = None
            if version["design_context_json"]:
                try:
                    raw_context = load_json(version["design_context_json"])
                    if isinstance(raw_context, dict) and raw_context.get("schemaVersion") in {1, 2, 3, 4, 5}:
                        legacy_schema_version = raw_context.get("schemaVersion")
                        legacy_context = normalize_design_context(raw_context)
                except (TypeError, ValueError, json.JSONDecodeError):
                    legacy_context = None

            if legacy_context is not None:
                if version["parent_version_id"]:
                    parent_context = load_design_context(
                        database, session["id"], version["parent_version_id"]
                    )
                    known = {item.get("id") for item in legacy_context["openQuestions"]}
                    legacy_context["openQuestions"] = [
                        *[
                            item for item in parent_context.get("openQuestions", [])
                            if item.get("id") not in known
                        ],
                        *legacy_context["openQuestions"],
                    ]
                assistant_turns = database.execute(
                    """
                    SELECT id, content, guidance_json FROM conversation_turns
                    WHERE session_id = ? AND version_id = ? AND role = 'assistant'
                    ORDER BY sequence_number
                    """,
                    (session["id"], version["id"]),
                ).fetchall()
                for turn in assistant_turns:
                    for question in _legacy_visible_questions(
                        turn["content"], load_json(turn["guidance_json"]) or {}
                    ):
                        legacy_context = add_open_question(
                            legacy_context,
                            question,
                            version["id"],
                            turn["id"],
                            source_kind="visible_output",
                        )
                legacy_context["updatedFromStageId"] = version["id"]
                save_design_context(database, version["id"], legacy_context)
                database.execute(
                    """
                    INSERT INTO audit_events(session_id, event_type, payload_json, created_at)
                    VALUES (?, 'design_context_migrated', ?, ?)
                    """,
                    (
                        session["id"],
                        dump_json({
                            "versionId": version["id"],
                            "fromSchemaVersion": legacy_schema_version,
                            "toSchemaVersion": 6,
                        }),
                        _utc_now_iso(clock),
                    ),
                )
                changed += 1
                continue

            context = (
                load_design_context(database, session["id"], version["parent_version_id"])
                if version["parent_version_id"]
                else empty_design_context()
            )
            turns = database.execute(
                """
                SELECT id, role, content, guidance_json
                FROM conversation_turns
                WHERE session_id = ? AND version_id = ?
                ORDER BY sequence_number
                """,
                (session["id"], version["id"]),
            ).fetchall()
            for turn in turns:
                if turn["role"] == "user":
                    context = merge_chat_update(
                        context,
                        user_text=turn["content"],
                        stage_id=version["id"],
                        turn_id=turn["id"],
                    )
                    continue
                guidance = load_json(turn["guidance_json"]) or {}
                for question in _legacy_visible_questions(turn["content"], guidance):
                    context = add_open_question(
                        context,
                        question,
                        version["id"],
                        turn["id"],
                        source_kind="visible_output",
                    )
                disagreement = guidance.get("disagreement")
                if isinstance(disagreement, dict) and disagreement.get("status") == "active":
                    context = set_active_disagreement(
                        context, disagreement, version["id"], turn["id"]
                    )

            decisions = database.execute(
                """
                SELECT decision.*, proposal.summary, proposal.assistant_turn_id,
                       proposal.base_version_id
                FROM designer_decisions AS decision
                LEFT JOIN change_proposals AS proposal ON proposal.id = decision.proposal_id
                WHERE decision.session_id = ?
                  AND (decision.version_id = ? OR proposal.base_version_id = ?)
                ORDER BY decision.created_at
                """,
                (session["id"], version["id"], version["id"]),
            ).fetchall()
            for decision in decisions:
                if decision["decision_type"] == "accept" and decision["version_id"] == version["id"]:
                    context = add_confirmed_decision(
                        context,
                        decision["summary"] or "Accepted the proposed map revision",
                        decision["reason"] or "Designer accepted the validated proposal.",
                        version["id"],
                        decision["assistant_turn_id"],
                        decision["proposal_id"],
                    )
                elif decision["decision_type"] == "reject" and decision["proposal_id"] is not None:
                    context = add_rejected_decision(
                        context,
                        decision["summary"] or "Rejected the proposed map revision",
                        decision["reason"] or "",
                        version["id"],
                        decision["assistant_turn_id"],
                        decision["proposal_id"],
                    )

            context["updatedFromStageId"] = version["id"]
            save_design_context(database, version["id"], context)
            event_exists = database.execute(
                """
                SELECT 1 FROM audit_events
                WHERE session_id = ? AND event_type = 'design_context_backfilled'
                  AND json_extract(payload_json, '$.versionId') = ?
                LIMIT 1
                """,
                (session["id"], version["id"]),
            ).fetchone()
            if event_exists is None:
                database.execute(
                    """
                    INSERT INTO audit_events(session_id, event_type, payload_json, created_at)
                    VALUES (?, 'design_context_backfilled', ?, ?)
                    """,
                    (
                        session["id"],
                        dump_json({"versionId": version["id"], "schemaVersion": 6}),
                        _utc_now_iso(clock),
                    ),
                )
            changed += 1
    return changed


def backfill_intent_semantics(database, *, clock=None):
    """Attach high-confidence user-authored semantic claims without rewriting history."""
    topic_subjects = {
        "water_function": {"water"},
        "route_readability": {"route"},
        "route_rhythm": {"route", "rhythm"},
        "space_distribution": {"layout", "space"},
        "difficulty": {"difficulty"},
        "entity_placement": {"box", "target", "wall"},
    }
    changed = 0

    def legacy_claims(text):
        value = str(text or "").strip()
        lowered = value.casefold()
        if re.search(r"我(?:觉得|认为|担心|感觉).{0,18}(?:玩家|别人).{0,24}(?:会|可能|觉得|认为)", value):
            return []
        if re.search(r"\bi (?:think|feel|worry|believe).{0,36}\bplayers?\b.{0,48}\b(?:will|may|might|feel|think)\b", lowered):
            return []
        if (value.startswith("如果") or lowered.startswith("if ")) and not re.search(
            r"(?:我(?:希望|想要|不想要|喜欢|不喜欢)|\bi (?:want|prefer|like|dislike|hope)\b)",
            lowered,
        ):
            return []
        subject = (
            "water" if ("水" in value or "water" in lowered)
            else "wall" if ("墙" in value or "wall" in lowered)
            else "box" if ("箱" in value or "box" in lowered or "crate" in lowered)
            else "target" if ("目标" in value or "target" in lowered)
            else None
        )
        if subject is None:
            return []
        decrease = bool(re.search(
            r"(?:太多|过多|占地(?:太大|过大)|覆盖(?:太大|过大)|减少|更少|少一点|"
            r"\btoo (?:much|many|large)\b|\breduce\b|\bdecrease\b|\bless\b|\bfewer\b)",
            lowered,
            re.IGNORECASE,
        ))
        increase = bool(re.search(
            r"(?:太少|过少|不够(?:多|大)?|覆盖(?:太小|过小|不足)|增加|增多|更多|多一点|"
            r"\btoo (?:few|little|small)\b|\bnot enough\b|\bincrease\b|\bmore\b)",
            lowered,
            re.IGNORECASE,
        ))
        if decrease == increase:
            return []
        aspect = "visual" if re.search(r"(?:视觉|观感|外观|构图|\bvisual\b|\bappearance\b)", lowered) else "gameplay" if re.search(r"(?:路线|路径|推箱|玩法|游玩|节奏|限制|\broute\b|\bpush\b|\bgameplay\b)", lowered) else "unspecified"
        scope_match = re.search(r"(?:左侧|右侧|上方|下方|中央|中间|左上|右上|左下|右下|\b(?:left|right|upper|lower|center|middle)\b)", lowered)
        return [{
            "subject": subject,
            "attribute": "coverage" if subject == "water" else "amount",
            "direction": "decrease" if decrease else "increase",
            "degree": "excessive" if decrease else "insufficient",
            "aspect": aspect,
            "scope": scope_match.group(0) if scope_match else "stage",
            "confidence": 1.0,
        }]

    def statement_mentions_subject(statement, subject):
        value = str(statement or "").casefold()
        terms = {
            "water": ("水", "water"),
            "wall": ("墙", "障碍", "wall", "obstacle"),
            "box": ("箱", "box", "crate"),
            "target": ("目标", "终点", "target", "goal"),
        }.get(subject, ())
        return any(term in value for term in terms)

    versions = database.execute(
        "SELECT id, session_id, design_context_json FROM level_versions ORDER BY created_at, id"
    ).fetchall()
    for version in versions:
        context = normalize_design_context(load_json(version["design_context_json"]) or {})
        version_changed = False
        for hypothesis in context.get("intentHypotheses", []):
            if hypothesis.get("semanticClaims") or not hypothesis.get("sourceTurnId"):
                continue
            assistant = database.execute(
                "SELECT sequence_number, version_id FROM conversation_turns WHERE id = ? AND session_id = ? AND role = 'assistant'",
                (hypothesis["sourceTurnId"], version["session_id"]),
            ).fetchone()
            if assistant is None:
                continue
            source = database.execute(
                """
                SELECT id, content FROM conversation_turns
                WHERE session_id = ? AND version_id = ? AND role = 'user' AND sequence_number < ?
                ORDER BY sequence_number DESC LIMIT 1
                """,
                (version["session_id"], assistant["version_id"], assistant["sequence_number"]),
            ).fetchone()
            if source is None:
                continue
            claims = legacy_claims(source["content"])
            if len(claims) != 1:
                continue
            allowed_subjects = topic_subjects.get(hypothesis.get("topicKey"))
            if allowed_subjects and claims[0].get("subject") not in allowed_subjects:
                continue
            if not allowed_subjects and not statement_mentions_subject(
                hypothesis.get("statement"), claims[0].get("subject")
            ):
                continue
            claim = dict(claims[0])
            claim["sourceUserTurnId"] = source["id"]
            hypothesis["semanticClaims"] = [claim]
            version_changed = True
            database.execute(
                """
                INSERT INTO audit_events(session_id, event_type, payload_json, created_at)
                VALUES (?, 'intent_semantics_backfilled', ?, ?)
                """,
                (
                    version["session_id"],
                    dump_json({
                        "versionId": version["id"],
                        "hypothesisId": hypothesis["id"],
                        "sourceUserTurnId": source["id"],
                        "semanticClaims": [claim],
                    }),
                    _utc_now_iso(clock),
                ),
            )
            changed += 1
        if version_changed:
            save_design_context(database, version["id"], context)
    return changed


def _stable_challenge_review_id(challenge_id, source_user_turn_id):
    seed = f"{challenge_id}:{source_user_turn_id}".encode("utf-8")
    return "cr-" + hashlib.sha256(seed).hexdigest()[:24]


def backfill_revision_challenges(database):
    """Restore canonical challenge state from structured audit records only."""
    preexisting_challenge_ids = {
        row["challenge_id"]
        for row in database.execute("SELECT challenge_id FROM revision_challenges").fetchall()
    }
    migrated_challenge_ids = set()
    rows = database.execute(
        """
        SELECT session_id, event_type, payload_json, created_at
        FROM audit_events
        WHERE event_type IN (
          'proposal_challenge_hypotheses_recorded',
          'challenge_reason_review_pending',
          'challenge_reason_review_resolved'
        )
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        payload = load_json(row["payload_json"]) or {}
        challenge_id = str(payload.get("challengeId") or "").strip()
        if not challenge_id:
            continue
        if challenge_id in preexisting_challenge_ids:
            continue
        if row["event_type"] == "proposal_challenge_hypotheses_recorded":
            challenge_turn_id = str(payload.get("challengeTurnId") or "").strip()
            source_turn_id = str(payload.get("sourceTurnId") or "").strip()
            base_version_id = str(payload.get("baseVersionId") or "").strip()
            if not challenge_turn_id or not source_turn_id or not base_version_id:
                continue
            database.execute(
                """
                INSERT OR IGNORE INTO revision_challenges(
                    challenge_id, session_id, base_version_id,
                    source_proposal_turn_id, challenge_turn_id, status,
                    current_reason_turn_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'awaiting_reason', NULL, ?, ?)
                """,
                (
                    challenge_id,
                    row["session_id"],
                    base_version_id,
                    source_turn_id,
                    challenge_turn_id,
                    row["created_at"],
                    row["created_at"],
                ),
            )
            migrated_challenge_ids.add(challenge_id)
            continue

        source_user_turn_id = str(payload.get("sourceUserTurnId") or "").strip()
        message_key = str(payload.get("messageKey") or "").strip()
        if not source_user_turn_id or not message_key:
            continue
        challenge = database.execute(
            "SELECT challenge_id FROM revision_challenges WHERE challenge_id = ? AND session_id = ?",
            (challenge_id, row["session_id"]),
        ).fetchone()
        if challenge is None:
            continue
        review_id = _stable_challenge_review_id(challenge_id, source_user_turn_id)
        review_status = (
            "review_pending"
            if row["event_type"] == "challenge_reason_review_pending"
            else "resolved"
        )
        challenge_status = (
            "review_pending"
            if review_status == "review_pending"
            else str(payload.get("state") or "reason_review")
        )
        database.execute(
            """
            INSERT INTO challenge_reason_reviews(
                review_id, session_id, challenge_id, source_user_turn_id,
                message_key, status, attempts_used, failure_code, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, source_user_turn_id) DO UPDATE SET
                status = excluded.status,
                attempts_used = excluded.attempts_used,
                failure_code = excluded.failure_code,
                result_json = excluded.result_json,
                updated_at = excluded.updated_at
            """,
            (
                review_id,
                row["session_id"],
                challenge_id,
                source_user_turn_id,
                message_key,
                review_status,
                int(payload.get("attemptsUsed") or 0),
                payload.get("failureCode"),
                dump_json(payload) if review_status == "resolved" else None,
                row["created_at"],
                row["created_at"],
            ),
        )
        database.execute(
            """
            UPDATE revision_challenges
            SET current_reason_turn_id = ?, status = ?, updated_at = ?
            WHERE challenge_id = ? AND session_id = ?
            """,
            (
                source_user_turn_id,
                challenge_status,
                row["created_at"],
                challenge_id,
                row["session_id"],
            ),
        )

    # Preserve all reasons while exposing only the latest unresolved one as
    # actionable after migration.
    challenges = database.execute(
        "SELECT challenge_id, current_reason_turn_id FROM revision_challenges"
    ).fetchall()
    for challenge in challenges:
        if (
            challenge["challenge_id"] not in migrated_challenge_ids
            or not challenge["current_reason_turn_id"]
        ):
            continue
        database.execute(
            """
            UPDATE challenge_reason_reviews
            SET status = 'superseded'
            WHERE challenge_id = ? AND status = 'review_pending'
              AND source_user_turn_id != ?
            """,
            (challenge["challenge_id"], challenge["current_reason_turn_id"]),
        )
    for challenge_id in migrated_challenge_ids:
        challenge_session = database.execute(
            "SELECT session_id FROM revision_challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if challenge_session is None:
            continue
        guidance_rows = database.execute(
            """
            SELECT guidance_json, created_at FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND guidance_json IS NOT NULL
            ORDER BY sequence_number DESC
            """,
            (challenge_session["session_id"],),
        ).fetchall()
        for guidance_row in guidance_rows:
            guidance = load_json(guidance_row["guidance_json"]) or {}
            challenge_state = guidance.get("challengeState") or {}
            if challenge_state.get("challengeId") != challenge_id:
                continue
            status = str(challenge_state.get("status") or "").strip()
            if status:
                database.execute(
                    "UPDATE revision_challenges SET status = ?, updated_at = ? WHERE challenge_id = ?",
                    (status, guidance_row["created_at"], challenge_id),
                )
            break


def repair_resolved_challenge_disagreements(database, *, clock=None):
    """Clear choice-pending snapshots already resolved by a validated proposal.

    A short-lived routing mismatch allowed the frontend's ``continue_challenge``
    action to start a replacement proposal without persisting the corresponding
    resolved disagreement.  Repair only when the immutable audit trail contains
    both that continuation and a later accepted Revision Workflow V2 candidate
    for the same message key.  Descendants are repaired only while they carry
    the exact inherited disagreement, so a later independent disagreement is
    never cleared.
    """
    continuations = database.execute(
        """
        SELECT id, session_id, payload_json
        FROM audit_events
        WHERE event_type = 'challenge_continued'
        ORDER BY id
        """
    ).fetchall()
    repaired = 0
    for continuation in continuations:
        payload = load_json(continuation["payload_json"]) or {}
        if payload.get("action") != "continue_challenge":
            continue
        message_key = str(payload.get("messageKey") or "").strip()
        base_version_id = str(payload.get("baseVersionId") or "").strip()
        if not message_key or not base_version_id:
            continue

        workflow_event = None
        workflow_rows = database.execute(
            """
            SELECT id, payload_json
            FROM audit_events
            WHERE session_id = ? AND id > ?
              AND event_type = 'revision_workflow_v2_evaluated'
            ORDER BY id
            """,
            (continuation["session_id"], continuation["id"]),
        ).fetchall()
        for workflow_row in workflow_rows:
            workflow = load_json(workflow_row["payload_json"]) or {}
            if workflow.get("messageKey") != message_key:
                continue
            if workflow.get("candidateAccepted") is not True:
                continue
            if (workflow.get("workflow") or {}).get("status") != "authorized":
                continue
            workflow_event = workflow_row
            break
        if workflow_event is None:
            continue

        base_context = load_design_context(
            database, continuation["session_id"], base_version_id
        )
        stale_disagreement = base_context.get("activeDisagreement")
        if not stale_disagreement:
            disagreement_event = database.execute(
                """
                SELECT payload_json FROM audit_events
                WHERE session_id = ? AND id < ?
                  AND event_type IN ('disagreement_started', 'disagreement_updated')
                  AND json_extract(payload_json, '$.versionId') = ?
                ORDER BY id DESC LIMIT 1
                """,
                (
                    continuation["session_id"],
                    continuation["id"],
                    base_version_id,
                ),
            ).fetchone()
            if disagreement_event is not None:
                disagreement_payload = load_json(
                    disagreement_event["payload_json"]
                ) or {}
                stale_disagreement = normalize_design_context({
                    "activeDisagreement": disagreement_payload.get("disagreement")
                }).get("activeDisagreement")
        if not (
            isinstance(stale_disagreement, dict)
            and stale_disagreement.get("status") == "active"
            and stale_disagreement.get("subject") == "ai_revision_challenge"
            and stale_disagreement.get("phase") == "choice_pending"
        ):
            continue

        identity_fields = (
            "subject",
            "userPosition",
            "proposalSummary",
            "acceptedReason",
            "primaryHypothesis",
            "secondaryHypothesis",
        )
        stale_identity = tuple(
            stale_disagreement.get(field) for field in identity_fields
        )

        versions = database.execute(
            """
            SELECT id, parent_version_id
            FROM level_versions
            WHERE session_id = ?
            ORDER BY stage_number, created_at, id
            """,
            (continuation["session_id"],),
        ).fetchall()
        descendants = {base_version_id}
        for version in versions:
            if version["parent_version_id"] in descendants:
                descendants.add(version["id"])

        for version in versions:
            if version["id"] not in descendants:
                continue
            context = load_design_context(
                database, continuation["session_id"], version["id"]
            )
            candidate = context.get("activeDisagreement")
            if not isinstance(candidate, dict):
                continue
            candidate_identity = tuple(
                candidate.get(field) for field in identity_fields
            )
            if candidate_identity != stale_identity:
                continue
            context = set_active_disagreement(
                context, None, version["id"], None
            )
            save_design_context(database, version["id"], context)
            record_event(
                database,
                continuation["session_id"],
                "stale_disagreement_repaired",
                {
                    "versionId": version["id"],
                    "baseVersionId": base_version_id,
                    "messageKey": message_key,
                    "reason": "validated_replacement_proposal",
                },
                _utc_now_iso(clock),
            )
            repaired += 1
    return repaired


def repair_duplicate_progress_questions(database, *, clock=None):
    """Idempotently merge duplicate visible questions in Stage snapshots."""
    versions = database.execute(
        """
        SELECT id, session_id, design_context_json
        FROM level_versions
        WHERE design_context_json IS NOT NULL
        ORDER BY session_id, stage_number, created_at, id
        """
    ).fetchall()
    repaired = 0
    for version in versions:
        context = normalize_design_context(load_json(version["design_context_json"]))
        before_count = len(context.get("openQuestions", []))
        if before_count < 2:
            continue
        preferred_questions = []
        guidance_rows = database.execute(
            """
            SELECT guidance_json FROM conversation_turns
            WHERE session_id = ? AND version_id = ?
              AND role = 'assistant' AND guidance_json IS NOT NULL
            ORDER BY sequence_number
            """,
            (version["session_id"], version["id"]),
        ).fetchall()
        for row in guidance_rows:
            guidance = load_json(row["guidance_json"]) or {}
            disagreement = guidance.get("disagreement") or {}
            if (
                isinstance(disagreement, dict)
                and disagreement.get("nextQuestion")
            ):
                preferred_questions.append(disagreement["nextQuestion"])

        repaired_context = deduplicate_open_questions(
            context, preferred_questions=preferred_questions
        )
        removed = before_count - len(repaired_context.get("openQuestions", []))
        if removed <= 0:
            continue
        save_design_context(database, version["id"], repaired_context)
        record_event(
            database,
            version["session_id"],
            "duplicate_progress_questions_repaired",
            {"versionId": version["id"], "removedCount": removed},
            _utc_now_iso(clock),
        )
        repaired += removed
    return repaired


def repair_answered_challenge_choice_questions(database, *, clock=None):
    """Backfill explicit yes/no answers skipped by the legacy challenge route."""
    continuations = database.execute(
        """
        SELECT id, session_id, payload_json FROM audit_events
        WHERE event_type = 'challenge_continued'
        ORDER BY id
        """
    ).fetchall()
    repaired = 0
    for continuation in continuations:
        payload = load_json(continuation["payload_json"]) or {}
        message_key = str(payload.get("messageKey") or "").strip()
        base_version_id = str(payload.get("baseVersionId") or "").strip()
        if not message_key or not base_version_id:
            continue
        user_turn = database.execute(
            """
            SELECT id, content FROM conversation_turns
            WHERE session_id = ? AND request_id = ? AND role = 'user'
            LIMIT 1
            """,
            (continuation["session_id"], message_key),
        ).fetchone()
        if user_turn is None:
            continue
        answer = re.sub(
            r"[\s,.!?;:\u3002\uFF0C\uFF01\uFF1F\uFF1B\uFF1A]+",
            "",
            str(user_turn["content"] or "").casefold(),
        )
        if answer not in {"yes", "no", "\u662f", "\u5426"}:
            continue
        disagreement_row = database.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE session_id = ? AND id < ?
              AND event_type IN ('disagreement_started', 'disagreement_updated')
              AND json_extract(payload_json, '$.versionId') = ?
            ORDER BY id DESC LIMIT 1
            """,
            (continuation["session_id"], continuation["id"], base_version_id),
        ).fetchone()
        if disagreement_row is None:
            continue
        disagreement = (
            load_json(disagreement_row["payload_json"]) or {}
        ).get("disagreement") or {}
        if not (
            disagreement.get("subject") == "ai_revision_challenge"
            and disagreement.get("phase") == "choice_pending"
            and disagreement.get("nextQuestion")
        ):
            continue
        question_key = question_dedup_key(disagreement["nextQuestion"])
        versions = database.execute(
            """
            SELECT id, parent_version_id FROM level_versions
            WHERE session_id = ? ORDER BY stage_number, created_at, id
            """,
            (continuation["session_id"],),
        ).fetchall()
        descendants = {base_version_id}
        for version in versions:
            if version["parent_version_id"] in descendants:
                descendants.add(version["id"])
        for version in versions:
            if version["id"] not in descendants:
                continue
            context = load_design_context(
                database, continuation["session_id"], version["id"]
            )
            matching_ids = [
                item.get("id") for item in context.get("openQuestions", [])
                if item.get("status") == "open"
                and question_dedup_key(item.get("question")) == question_key
                and item.get("id")
            ]
            if not matching_ids:
                continue
            context = apply_question_answer_review(
                context,
                matching_ids,
                base_version_id,
                user_turn["id"],
            )
            save_design_context(database, version["id"], context)
            record_event(
                database,
                continuation["session_id"],
                "challenge_choice_question_repaired",
                {
                    "versionId": version["id"],
                    "baseVersionId": base_version_id,
                    "userTurnId": user_turn["id"],
                    "questionIds": matching_ids,
                },
                _utc_now_iso(clock),
            )
            repaired += len(matching_ids)
    return repaired
