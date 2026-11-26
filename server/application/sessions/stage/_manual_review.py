"""Helpers for manual-edit conflict classification, review, and Dashboard node events."""

from __future__ import annotations

import uuid
from typing import Any

from server.domain.memory import ensure_design_context, set_active_disagreement
from server.llm.pipelines.understanding import (
    ManualEditConflictResult,
    ManualEditReviewResult,
    UnderstandingPipeline,
)
from server.persistence.database import dump_json
from server.persistence.models import DesignSessionRecord, LevelVersionRecord

from ..config import RoomSyncWriter, append_audit_event_tx, emit_room_cocreation_event_tx


def extract_confirmed_directions(version: LevelVersionRecord) -> list[dict[str, Any]]:
    """Collect active explicit/confirmed goals, constraints, and decisions from DesignContext."""
    state = ensure_design_context(version.design_context)
    directions: list[dict[str, Any]] = []
    for goal in state.user_goals:
        if goal.status == "active" and goal.authority in {"explicit", "confirmed"}:
            directions.append(
                {
                    "id": goal.id,
                    "kind": "goal",
                    "authority": goal.authority,
                    "fact": goal.goal,
                }
            )
    for constraint in state.design_constraints:
        if constraint.status == "active" and constraint.authority in {"explicit", "confirmed"}:
            directions.append(
                {
                    "id": constraint.id,
                    "kind": "constraint",
                    "authority": constraint.authority,
                    "fact": constraint.constraint,
                }
            )
    for decision in state.confirmed_decisions:
        if decision.status == "active":
            directions.append(
                {
                    "id": decision.id,
                    "kind": "decision",
                    "authority": "confirmed",
                    "fact": decision.decision,
                }
            )
    return directions


async def run_manual_edit_review(
    understanding_pipeline: UnderstandingPipeline,
    *,
    version: LevelVersionRecord,
    parent: LevelVersionRecord | None,
    change_summary: dict[str, Any] | None,
    language: str,
    deadline: float | None,
    request_id: str,
) -> tuple[ManualEditConflictResult, ManualEditReviewResult, dict[str, Any]]:
    """Run manual-edit conflict adjudication and review OUTSIDE SQLite write_tx."""
    directions = extract_confirmed_directions(version)
    solver_delta: dict[str, Any] = {}
    if parent is not None:
        solver_delta = {
            "beforeSteps": parent.solver.get("solutionSteps"),
            "afterSteps": version.solver.get("solutionSteps"),
            "beforePushes": parent.solver.get("solutionPushes"),
            "afterPushes": version.solver.get("solutionPushes"),
        }
    effect_evidence = [
        {
            "id": f"diff:{version.version_id}",
            "kind": "verified_diff",
            "summary": version.summary,
            "verifiedChangeSummary": change_summary,
            "diff": list(version.diff),
            "solver": dict(version.solver),
            "solverDelta": solver_delta,
        }
    ]
    conflict_decision = await understanding_pipeline.classify_manual_edit_conflict(
        directions=directions,
        effect_evidence=effect_evidence,
        language=language,
        deadline=deadline,
        request_id=request_id,
    )
    review_res = await understanding_pipeline.generate_manual_edit_review(
        grid_or_rows=version.rows,
        conflict_decision=conflict_decision,
        review_context={
            "stageNumber": version.version_number,
            "summary": version.summary,
            "changeSummary": change_summary,
            "diff": list(version.diff),
            "solver": dict(version.solver),
            "solverDelta": solver_delta,
            "directions": directions,
            "comparisons": list(conflict_decision.comparisons),
        },
        language=language,
        entity_bindings=version.entity_bindings,
        deadline=deadline,
        request_id=request_id,
    )
    return conflict_decision, review_res, solver_delta


def commit_manual_edit_review_tx(
    conn: Any,
    *,
    room_sync: RoomSyncWriter | None,
    session: DesignSessionRecord,
    ver: LevelVersionRecord,
    model_name: str,
    opening_turn_id: str,
    review_turn_id: str,
    seq: int,
    idempotency_key: str,
    conflict_decision: ManualEditConflictResult,
    review_res: ManualEditReviewResult,
    change_summary: dict[str, Any] | None,
    solver_delta: dict[str, Any],
    disagreement_payload: dict[str, Any] | None,
    now_iso: str,
) -> None:
    """Persist opening + review turns, activeDisagreement, audits, and Dashboard node."""
    session_id = session.session_id
    version_id = ver.version_id
    opening_guidance = {
        "move": "observe_stage",
        "intentHypothesis": None,
        "intentConfidence": None,
        "followUpQuestion": None,
        "proposalOffer": None,
        "disagreement": None,
        "uiCues": [],
        "coordinateLinks": [],
    }
    review_guidance = {
        "move": "offer_perspective",
        "discussionCardMode": "disagreement_only",
        "disagreement": disagreement_payload,
        "uiCues": [],
        "coordinateLinks": [],
    }
    for offset, turn_id, content, guidance in (
        (0, opening_turn_id, review_res.opening_message, opening_guidance),
        (1, review_turn_id, review_res.review_message, review_guidance),
    ):
        conn.execute(
            "INSERT INTO conversation_turns("
            "turn_id, session_id, sequence_number, request_id, role, content, "
            "language, guidance_json, model, attempts_used, latency_ms, version_id, created_at"
            ") VALUES (?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn_id,
                session_id,
                seq + offset,
                idempotency_key,
                content,
                session.language,
                dump_json(guidance),
                model_name,
                review_res.attempts_used,
                review_res.latency_ms,
                version_id,
                now_iso,
            ),
        )

    if disagreement_payload is not None:
        updated_ctx = set_active_disagreement(
            ensure_design_context(ver.design_context),
            disagreement_payload,
            version_id,
            review_turn_id,
        )
        conn.execute(
            "UPDATE level_versions SET assessment_json = ?, design_context_json = ? "
            "WHERE session_id = ? AND version_id = ? AND assessment_json IS NULL",
            (
                dump_json(review_res.assessment),
                dump_json(updated_ctx.to_wire_dict()),
                session_id,
                version_id,
            ),
        )
    else:
        conn.execute(
            "UPDATE level_versions SET assessment_json = ? "
            "WHERE session_id = ? AND version_id = ? AND assessment_json IS NULL",
            (dump_json(review_res.assessment), session_id, version_id),
        )

    append_audit_event_tx(
        conn,
        session_id,
        "human_edit_reviewed",
        {
            "versionId": version_id,
            "turnId": review_turn_id,
            "openingTurnId": opening_turn_id,
            "reviewTurnId": review_turn_id,
            "diff": list(ver.diff),
            "changeSummary": change_summary,
            "hasWarning": False,
            "reviewOutcome": conflict_decision.verdict,
            "decisionSource": conflict_decision.decision_source,
            "confirmedDirectionIds": list(conflict_decision.confirmed_direction_ids),
            "effectEvidenceIds": list(conflict_decision.effect_evidence_ids),
            "solverDelta": solver_delta,
            "comparisons": list(conflict_decision.comparisons),
            "disagreement": disagreement_payload,
        },
        now_iso,
    )
    if disagreement_payload is not None:
        append_audit_event_tx(
            conn,
            session_id,
            "disagreement_started",
            {
                "versionId": version_id,
                "turnId": review_turn_id,
                "subject": "human_edit",
                "disagreement": disagreement_payload,
            },
            now_iso,
        )
    append_audit_event_tx(
        conn,
        session_id,
        "intent_progress_rewrite",
        {
            "artifactType": "manual_edit",
            "artifactId": version_id,
            "versionId": version_id,
            "language": session.language,
            "summaryText": str(ver.summary or "Designer saved a validated manual edit.")[:1200],
            "detailedText": str(review_res.review_message or review_res.opening_message)[:1200],
        },
        now_iso,
    )

    node_entries: list[dict[str, Any]] = [
        {
            "entryId": f"manual-save:{version_id}",
            "kind": "player_action",
            "text": str(ver.summary or "Designer saved an edited stage")[:12000],
            "label": "Player action",
            "occurredAt": ver.created_at,
            "language": session.language,
        }
    ]
    if ver.diff:
        node_entries.append(
            {
                "entryId": f"verified-diff:{version_id}",
                "kind": "map_diff",
                "text": dump_json(ver.diff),
                "label": "Verified map change",
                "occurredAt": ver.created_at,
            }
        )
    node_entries.extend(
        [
            {
                "entryId": f"llm:{opening_turn_id}",
                "kind": "llm_message",
                "text": review_res.opening_message,
                "label": "LLM",
                "occurredAt": now_iso,
                "language": session.language,
            },
            {
                "entryId": f"llm:{review_turn_id}",
                "kind": "llm_message",
                "text": review_res.review_message,
                "label": "LLM",
                "occurredAt": now_iso,
                "language": session.language,
            },
        ]
    )
    if disagreement_payload is not None:
        card_text = "\n".join(
            str(disagreement_payload.get(f) or "")
            for f in ("userPosition", "coreDisagreement", "nextQuestion")
        ).strip()
        node_entries.append(
            {
                "entryId": f"card:{review_turn_id}:0",
                "kind": "card",
                "text": card_text or review_res.review_message,
                "label": "LET'S DISCUSS",
                "occurredAt": now_iso,
                "language": session.language,
            }
        )
    manual_node_ev = {
        "eventId": f"node:manual-review:{version_id}",
        "eventType": "node",
        "playerNumber": session.player_slot or 1,
        "sessionId": session_id,
        "nodeId": f"manual-review:{version_id}",
        "nodeType": "manual_edit_review",
        "nodeStatus": "reviewed",
        "versionId": version_id,
        "stageNumber": ver.version_number,
        "nodeEntries": node_entries[-96:],
    }
    emit_room_cocreation_event_tx(
        conn,
        room_sync,
        match_id=session.match_id,
        player_slot=session.player_slot,
        is_demo=session.is_demo,
        session_id=session_id,
        event=manual_node_ev,
        now_iso=now_iso,
    )


def build_disagreement_payload(review_res: ManualEditReviewResult) -> dict[str, Any] | None:
    """Attach server identity & displayCard flag to a detected human-edit conflict."""
    if review_res.conflict is None:
        return None
    return {
        **review_res.conflict,
        "disagreementId": f"dis_{uuid.uuid4().hex[:16]}",
        "status": "active",
        "subject": "human_edit",
        "displayCard": True,
        "resolution": None,
    }
