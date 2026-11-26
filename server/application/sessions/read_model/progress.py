"""Pure progress-context and failure/interaction projection helpers for read_model."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from server.core.clock import parse_iso
from server.domain.memory import ensure_design_context
from server.domain.proposals import BINARY_ACTIVE_STATUSES, BinaryDiscoveryState
from server.persistence.models import (
    AuditEventRecord,
    ComponentRecoveryTaskRecord,
    LevelVersionRecord,
    PlayAttemptRecord,
    SessionReadModel,
)

_COORD_PAREN_RE = re.compile(r"[\(（]\s*\d{1,2}\s*[,，]\s*\d{1,2}\s*[\)）]")


def clean_evidence_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = _COORD_PAREN_RE.sub("", text)
    return " ".join(text.split())[:500]


def serialize_play_attempt(attempt: PlayAttemptRecord) -> dict[str, Any]:
    return {
        "attemptId": attempt.attempt_id,
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


def build_progress_context(
    version: LevelVersionRecord,
    *,
    context: Mapping[str, Any],
    session_language: str,
    stage_numbers: Mapping[str, int],
    parent_by_version: Mapping[str, str | None],
    turn_created_at: Mapping[str, str],
    intent_evidence_by_id: Mapping[str, dict[str, Any]],
    latest_intent_feedback: Mapping[tuple[str, str], dict[str, Any]],
    progress_rewrites: Mapping[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    state = ensure_design_context(context)

    def _stage_num(vid: str | None) -> int:
        return stage_numbers.get(vid or "", version.version_number)

    lineage: set[str] = set()
    cursor: str | None = version.version_id
    while cursor and cursor not in lineage:
        lineage.add(cursor)
        cursor = parent_by_version.get(cursor)

    expressed: list[dict[str, Any]] = []
    for item in state.user_goals:
        if item.status == "active" and item.authority == "explicit" and item.goal.strip():
            expressed.append(
                {
                    "kind": "goal",
                    "text": item.goal.strip(),
                    "sourceStageNumber": _stage_num(item.source_stage_id),
                    "updatedAt": turn_created_at.get(item.source_turn_id or ""),
                    "label": "explicit",
                }
            )
    for item in state.design_constraints:
        if item.status == "active" and item.authority == "explicit" and item.constraint.strip():
            expressed.append(
                {
                    "kind": "constraint",
                    "text": item.constraint.strip(),
                    "sourceStageNumber": _stage_num(item.source_stage_id),
                    "updatedAt": turn_created_at.get(item.source_turn_id or ""),
                    "label": "explicit",
                }
            )

    inclinations: list[dict[str, Any]] = []
    for hyp in (h for h in state.intent_hypotheses if h.status == "confirmed"):
        ev_records = [
            intent_evidence_by_id[eid]
            for eid in dict.fromkeys(hyp.supporting_evidence_ids)
            if eid in intent_evidence_by_id
            and intent_evidence_by_id[eid].get("versionId") in lineage
            and intent_evidence_by_id[eid].get("kind")
            in {"expressed_direction", "proposal_accept", "disagreement_resolved", "manual_edit"}
        ]
        ev_records.sort(key=lambda r: (str(r.get("createdAt") or ""), int(r.get("auditId") or 0)))
        trail = [
            _evidence_projection(ev, session_language, stage_numbers, progress_rewrites)
            for ev in ev_records[-12:]
        ]
        fb = latest_intent_feedback.get((version.version_id, hyp.id), {})
        inclinations.append(
            {
                "hypothesisId": hyp.id,
                "statement": hyp.display_statement or hyp.statement,
                "confirmedAtStageNumber": _stage_num(
                    hyp.confirmed_at_stage_id or hyp.source_stage_id
                ),
                "updatedAt": fb.get("createdAt")
                or (trail[-1]["createdAt"] if trail else None)
                or turn_created_at.get(hyp.source_turn_id or ""),
                "evidenceTrail": trail,
            }
        )

    return {
        "versionId": version.version_id,
        "stageNumber": version.version_number,
        "parentVersionId": version.parent_version_id,
        "expressedDirections": expressed[-16:],
        "designInclinations": inclinations[-16:],
        "confirmedDecisions": [
            {
                "decision": d.decision,
                "reason": d.reason or None,
                "sourceStageNumber": _stage_num(d.source_stage_id),
                "updatedAt": turn_created_at.get(d.source_turn_id or ""),
                "label": "confirmed",
            }
            for d in state.confirmed_decisions
            if d.status == "active"
        ],
        "unresolvedQuestions": [
            {
                "question": q.question,
                "sourceStageNumber": _stage_num(q.source_stage_id),
                "updatedAt": turn_created_at.get(
                    q.updated_from_turn_id or q.resolved_by_turn_id or q.source_turn_id or ""
                ),
                "label": "open",
            }
            for q in state.design_level_open_questions()
        ],
        "questionRecords": [
            {
                "questionId": q.id,
                "question": q.question,
                "status": (
                    "answered"
                    if q.status == "answered"
                    else "ignored"
                    if q.status == "ignored"
                    else "unanswered"
                ),
                "askedAtStageNumber": _stage_num(q.source_stage_id),
                "answeredAtStageNumber": (
                    _stage_num(q.answered_at_stage_id) if q.answered_at_stage_id else None
                ),
                "ignoredAtStageNumber": (
                    _stage_num(q.ignored_at_stage_id) if q.ignored_at_stage_id else None
                ),
                "sourceTurnId": q.source_turn_id,
                "answeredByTurnId": q.resolved_by_turn_id,
                "updatedAt": q.ignored_at
                or turn_created_at.get(
                    q.resolved_by_turn_id or q.updated_from_turn_id or q.source_turn_id or ""
                ),
            }
            for q in state.open_questions
            if q.source_kind in {"visible_output", "legacy"}
        ],
    }


def _evidence_projection(
    evidence: Mapping[str, Any],
    language: str,
    stage_numbers: Mapping[str, int],
    progress_rewrites: Mapping[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    kind = str(evidence.get("kind") or "feedback")
    public_kind = {
        "expressed_direction": "expressed_direction",
        "proposal_accept": "confirmed_decision",
        "disagreement_resolved": "confirmed_decision",
        "manual_edit": "manual_edit",
    }.get(kind, kind)
    details = evidence.get("details") if isinstance(evidence.get("details"), Mapping) else {}
    text = clean_evidence_text(evidence.get("observation"))
    if public_kind == "confirmed_decision":
        pid = str(evidence.get("proposalId") or "")
        rewrite = progress_rewrites.get(("confirmed_decision", pid)) or {}
        if rewrite.get("summaryText"):
            text = clean_evidence_text(rewrite["summaryText"])
    if kind == "manual_edit":
        vid = str(evidence.get("versionId") or "")
        rewrite = progress_rewrites.get(("manual_edit", vid)) or {}
        if rewrite.get("summaryText"):
            text = clean_evidence_text(rewrite["summaryText"])
        changed = len(details.get("diff") or [])
        suffix = (
            f"确定性 diff 记录了 {changed} 个格子变化，且保存时已通过验证。"
            if language == "zh-CN"
            else (
                f"The deterministic diff recorded {changed} changed tiles "
                "and validation passed on save."
            )
        )
        text = " ".join(p for p in (text, suffix) if p)
    return {
        "evidenceId": evidence.get("evidenceId"),
        "stageNumber": stage_numbers.get(str(evidence.get("versionId") or ""), 1),
        "kind": public_kind,
        "text": text,
        "createdAt": evidence.get("createdAt"),
        "detailedText": (
            clean_evidence_text(
                (progress_rewrites.get(("manual_edit", str(evidence.get("versionId") or ""))) or {})
                .get("detailedText")
            )
            or None
            if kind == "manual_edit"
            else None
        ),
    }


def build_last_message_failure(
    read_model: SessionReadModel,
    events: list[AuditEventRecord],
    topic_state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    version_id = read_model.session.current_version_id
    failure = next(
        (
            ev
            for ev in reversed(events)
            if ev.event_type == "message_generation_failed"
            and ev.payload.get("baseVersionId") == version_id
        ),
        None,
    )
    if failure is None:
        return None
    key = failure.payload.get("messageKey")
    current_turns = [t for t in read_model.turns if t.version_id == version_id]
    if not key or any(t.role == "assistant" and t.request_id == key for t in current_turns):
        return None
    if current_turns and max(t.created_at for t in current_turns) > failure.created_at:
        return None
    if topic_state and topic_state.get("versionId") == version_id:
        if topic_state.get("status") in {"cancelled", "proposal_ready"}:
            topic_keys = {
                topic_state.get("initialMessageKey"),
                (topic_state.get("retryAction") or {}).get("idempotencyKey"),
            }
            if key in topic_keys:
                return None
    details = failure.payload.get("details") or {}
    allowed = (
        "task",
        "failureStage",
        "failureKind",
        "failureCode",
        "attemptsUsed",
        "maximumAttempts",
        "recoveryState",
        "component",
        "repairAttemptsUsed",
        "maximumRepairAttempts",
    )
    return {
        "messageKey": key,
        "baseVersionId": version_id,
        "code": failure.payload.get("code"),
        "requestId": failure.payload.get("requestId"),
        "retryable": bool(failure.payload.get("retryable", True)),
        "committed": False,
        "details": {name: details[name] for name in allowed if name in details},
    }


def build_interaction_state(
    read_model: SessionReadModel,
    events: list[AuditEventRecord],
    recovery_tasks: list[ComponentRecoveryTaskRecord],
    *,
    now: datetime,
) -> dict[str, Any]:
    version_id = read_model.session.current_version_id
    current_ver = next((v for v in read_model.versions if v.version_id == version_id), None)
    cur_ctx = current_ver.design_context if current_ver else None
    active_dis = (cur_ctx or {}).get("activeDisagreement")
    revision = max(
        (
            ev.id
            for ev in events
            if ev.event_type.startswith("disagreement_") or ev.event_type == "component_recovery"
        ),
        default=0,
    )
    recoveries: list[dict[str, Any]] = []
    for task_rec in recovery_tasks:
        state = task_rec.state
        if state.get("baseVersionId") != version_id:
            continue
        status = str(state.get("status") or "pending")
        if (
            status == "claimed"
            and state.get("leaseUntil")
            and parse_iso(state["leaseUntil"]) <= now
        ):
            status = "retry_pending"
        attempts_used = int(state.get("attemptsUsed") or 0)
        recoveries.append(
            {
                "recoveryId": task_rec.recovery_id,
                "component": state.get("component") or task_rec.target_kind,
                "status": status,
                "attemptsUsed": attempts_used,
                "baseVersionId": version_id,
                "nextAttemptKey": f"{task_rec.recovery_id}-{attempts_used + 1}",
                "leaseUntil": state.get("leaseUntil"),
            }
        )
    dis_id = active_dis.get("disagreementId") if isinstance(active_dis, Mapping) else None
    return {
        "versionId": version_id,
        "revision": revision,
        "disagreementId": dis_id,
        "disagreementStatus": "active" if active_dis else None,
        "proposalBlocked": bool(active_dis and active_dis.get("status") == "active"),
        "recoveries": recoveries,
    }


def build_proposal_flow_state(
    read_model: SessionReadModel,
    topic_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if (
        isinstance(topic_state, Mapping)
        and topic_state.get("versionId") == read_model.session.current_version_id
    ):
        try:
            model = BinaryDiscoveryState.model_validate(
                {**topic_state, "sessionId": read_model.session.session_id}
            )
            return model.public_flow()
        except ValueError:
            if topic_state.get("status") in BINARY_ACTIVE_STATUSES:
                return {
                    "active": True,
                    "status": topic_state.get("status"),
                    "topicId": topic_state.get("topicId"),
                    "interactionMode": "binary_choice",
                    "clarificationQuestionCount": int(topic_state.get("questionCount") or 0),
                    "pendingQuestion": topic_state.get("pendingQuestion"),
                    "retryAction": topic_state.get("retryAction"),
                    "retryable": topic_state.get("status") == "retry_pending",
                    "failureCategory": topic_state.get("failureCode"),
                    "canSupplement": False,
                    "recoverySuggestions": [],
                }
    return {
        "active": False,
        "status": "inactive",
        "topicId": None,
        "clarificationQuestionCount": 0,
        "canSupplement": False,
        "retryable": False,
        "failureCategory": None,
        "recoverySuggestions": [],
    }


def build_draft_preview(read_model: SessionReadModel) -> dict[str, Any] | None:
    session = read_model.session
    if not session.draft_rows:
        return None
    latest_regen = read_model.draft_regenerations[0] if read_model.draft_regenerations else None
    return {
        "rows": list(session.draft_rows),
        "validation": session.draft_validation,
        "generation": session.draft_generation,
        "regenerationStatus": latest_regen.status if latest_regen else None,
        "regenerationRequestId": latest_regen.request_id if latest_regen else None,
        "regenerationRequestedAt": latest_regen.requested_at if latest_regen else None,
        "regenerationUpdatedAt": latest_regen.updated_at if latest_regen else None,
        "failureCode": latest_regen.failure_code if latest_regen else None,
        "mutable": session.current_version_id is None,
    }
