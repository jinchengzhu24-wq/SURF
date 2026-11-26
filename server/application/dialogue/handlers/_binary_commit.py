"""Extracted Policy-2 binary discovery proposal and commit helpers."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from server.application.sessions import RoomSyncWriter
from server.core.clock import Clock
from server.core.grid import map_fingerprint, parse_grid
from server.domain.proposals import (
    compile_choice_requirements_deterministic,
    validate_requirement_record,
)
from server.domain.sessions import describe_diff
from server.i18n.messages import format_tradeoff_notice, get_message
from server.llm.grounding import build_stage_facts
from server.llm.pipelines.proposal import ProposalPipeline
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import SessionReadModel

from .._shared import (
    build_proposal_offer_and_binding,
    emit_turn_room_event_tx,
    insert_turn_tx,
    require_current_version_tx,
)


def _question_card_payload(
    *,
    topic_id: str,
    version_id: str,
    question_id: str,
    number: int,
    step: Mapping[str, Any],
    candidates: Sequence[Any],
    language: str,
) -> dict[str, Any]:
    choices: list[dict[str, Any]] = []
    for idx, branch in enumerate(step.get("branches") or (), start=1):
        cand = next((c for c in candidates if c.branch_index == idx), None)
        choices.append(
            {
                "choiceId": f"ch_{uuid.uuid4().hex[:12]}",
                "letter": "A" if idx == 1 else "B",
                "label": str(branch.get("label") or ""),
                "promise": dict(branch.get("promise") or {}),
                "strategy": dict(branch.get("strategy") or {}),
                "cachedRows": list(cand.rows) if cand is not None else None,
                "cachedValidation": cand.solve_result.as_dict() if cand is not None else None,
            }
        )
    return {
        "questionId": question_id,
        "topicId": topic_id,
        "baseVersionId": version_id,
        "number": number,
        "question": str(step.get("question") or ""),
        "note": str(step.get("note") or ""),
        "sourceLanguage": language,
        "choices": [
            {"choiceId": c["choiceId"], "letter": c["letter"], "label": c["label"]}
            for c in choices
        ],
        "_internalChoices": choices,
    }


def _public_question_card(card: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in card.items() if k != "_internalChoices"}


def _insert_proposal_tx(
    conn: Any,
    *,
    session_id: str,
    base_version_id: str,
    assistant_turn_id: str,
    proposal_res: Any,
    user_summary: str,
    now_iso: str,
) -> str:
    proposal_id = uuid.uuid4().hex
    diff = [
        {"x": ch.x, "y": ch.y, "before": ch.before, "after": ch.after}
        for ch in proposal_res.changes
    ]
    payload = {
        "proposedRows": list(proposal_res.candidate_grid.rows),
        "summary": user_summary,
        "diff": diff,
        "validation": proposal_res.solve_result.as_dict(),
        "requirementRecord": proposal_res.requirement_record,
        "revisionPlan": proposal_res.revision_plan,
        "tradeoffNotice": proposal_res.tradeoff_notice,
        "source": proposal_res.source,
    }
    conn.execute(
        "UPDATE change_proposals SET status = 'superseded', decided_at = ? "
        "WHERE session_id = ? AND status = 'pending'",
        (now_iso, session_id),
    )
    conn.execute(
        "INSERT INTO change_proposals("
        "proposal_id, session_id, base_version_id, assistant_turn_id, status, "
        "proposal_json, created_at"
        ") VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (proposal_id, session_id, base_version_id, assistant_turn_id, dump_json(payload), now_iso),
    )
    return proposal_id


async def commit_binary_step_impl(
    *,
    db: SQLiteDatabase,
    proposal_pipeline: ProposalPipeline | None,
    binary_model: str,
    room_sync: RoomSyncWriter | None,
    clock: Clock,
    read_model: SessionReadModel,
    current_ver: Any,
    topic_id: str,
    initial_request: str,
    initial_turns: list[dict[str, Any]],
    prior_choices: list[dict[str, Any]],
    verified_initial_req: dict[str, Any] | None,
    step_res: Any,
    user_text: str,
    user_visible_action: bool,
    idempotency_key: str,
    request_id: str,
    deadline: float | None,
) -> dict[str, Any]:
    """Generate final proposal if `propose_ready` (OUTSIDE write_tx), then commit step."""
    session = read_model.session
    step = step_res.step
    action = step["action"]
    now_iso = clock.utc_now()
    user_turn_id = uuid.uuid4().hex
    assistant_turn_id = uuid.uuid4().hex
    proposal_res = None
    guidance: dict[str, Any] = {}
    binding: dict[str, Any] | None = None

    if action == "protected_refusal":
        assistant_text = step_res.refusal_message or get_message(
            "error.protected_edit", session.language
        )
        guidance = {"replyType": "protected_refusal"}
        topic_state: dict[str, Any] = {
            "topicId": topic_id,
            "versionId": current_ver.version_id,
            "status": "cancelled",
            "initialRequest": initial_request,
            "choices": prior_choices,
            "updatedAt": now_iso,
        }
    elif action == "ask":
        qid = f"q_{uuid.uuid4().hex[:12]}"
        q_num = len(prior_choices) + 1
        card = _question_card_payload(
            topic_id=topic_id,
            version_id=current_ver.version_id,
            question_id=qid,
            number=q_num,
            step=step,
            candidates=step_res.branch_candidates,
            language=session.language,
        )
        assistant_text = str(step.get("question") or "")
        guidance = {"proposalQuestion": _public_question_card(card)}
        topic_state = {
            "topicId": topic_id,
            "versionId": current_ver.version_id,
            "status": "clarifying",
            "initialRequest": initial_request,
            "initialTurnId": initial_turns[0]["id"] if initial_turns else "",
            "initialTurns": initial_turns,
            "initialRequirements": verified_initial_req,
            "initialMessageKey": idempotency_key,
            "choices": prior_choices,
            "pendingQuestion": card,
            "updatedAt": now_iso,
        }
    elif action == "blocked":
        assistant_text = str(step.get("note") or "") or get_message(
            "error.validation_failed", session.language
        )
        guidance = {"replyType": "clarification_blocked"}
        topic_state = {
            "topicId": topic_id,
            "versionId": current_ver.version_id,
            "status": "blocked",
            "initialRequest": initial_request,
            "choices": prior_choices,
            "updatedAt": now_iso,
        }
    else:
        compiled_reqs = [
            {k: v for k, v in req.items() if k != "requirementId"}
            for req in (
                list((verified_initial_req or {}).get("requirements") or ())
                + compile_choice_requirements_deterministic(prior_choices, initial_turns)
            )
        ]
        snapshot = build_stage_facts(
            parse_grid(current_ver.rows), current_ver.entity_bindings
        ).to_prompt_dict()
        req_record = validate_requirement_record(
            {
                "requirements": compiled_reqs,
                "automaticBindings": list(
                    (verified_initial_req or {}).get("automaticBindings") or ()
                ),
                "exactTransitions": list(
                    (verified_initial_req or {}).get("exactTransitions") or ()
                ),
            },
            initial_turns,
            snapshot,
        )
        precompiled = [c["strategy"] for c in prior_choices if c.get("strategy")]
        if not precompiled:
            precompiled = list(step.get("planStrategies") or ())
        promises = [c["promise"] for c in prior_choices if c.get("promise")]
        excluded_fps = [
            map_fingerprint(p.proposal.get("proposedRows") or ())
            for p in read_model.proposals
            if p.base_version_id == current_ver.version_id and p.proposal.get("proposedRows")
        ]
        assert proposal_pipeline is not None
        proposal_res = await proposal_pipeline.generate_proposal(
            grid_or_rows=current_ver.rows,
            authorized_brief=initial_request,
            designer_turns=initial_turns,
            requirement_record=req_record,
            precompiled_strategies=precompiled or None,
            binary_promises=promises,
            excluded_fingerprints=excluded_fps,
            design_context=current_ver.design_context,
            entity_bindings=current_ver.entity_bindings,
            language=session.language,
            deadline=deadline,
            request_id=request_id or idempotency_key,
        )
        diff = describe_diff(current_ver.rows, proposal_res.candidate_grid.rows)
        notice = proposal_res.tradeoff_notice or format_tradeoff_notice(
            current_ver.solver, proposal_res.solve_result, session.language
        )
        summary = str(req_record.get("summary") or initial_request)[:240]
        offer, binding = build_proposal_offer_and_binding(
            base_version=current_ver,
            candidate_rows=proposal_res.candidate_grid.rows,
            summary=summary,
            rationale=notice,
            language=session.language,
        )
        assistant_text = notice or summary
        guidance = {"proposalOffer": offer}
        topic_state = {
            "topicId": topic_id,
            "versionId": current_ver.version_id,
            "status": "proposal_ready",
            "initialRequest": initial_request,
            "initialTurns": initial_turns,
            "initialRequirements": verified_initial_req,
            "choices": prior_choices,
            "diffCount": len(diff),
            "updatedAt": now_iso,
        }

    with db.write_tx() as conn:
        cur_session, cur_ver = require_current_version_tx(
            conn, session.session_id, current_ver.version_id
        )
        if user_visible_action:
            insert_turn_tx(
                conn,
                turn_id=user_turn_id,
                session_id=cur_session.session_id,
                request_id=idempotency_key,
                role="user",
                content=user_text,
                language=cur_session.language,
                version_id=cur_ver.version_id,
                created_at=now_iso,
            )
        insert_turn_tx(
            conn,
            turn_id=assistant_turn_id,
            session_id=cur_session.session_id,
            request_id=idempotency_key,
            role="assistant",
            content=assistant_text,
            language=cur_session.language,
            version_id=cur_ver.version_id,
            created_at=now_iso,
            guidance=guidance,
            proposal_binding=binding,
            model=binary_model,
            attempts_used=step_res.attempts_used,
            latency_ms=step_res.latency_ms,
        )
        if proposal_res is not None:
            _insert_proposal_tx(
                conn,
                session_id=cur_session.session_id,
                base_version_id=cur_ver.version_id,
                assistant_turn_id=assistant_turn_id,
                proposal_res=proposal_res,
                user_summary=str(guidance["proposalOffer"]["summary"]),
                now_iso=now_iso,
            )
        conn.execute(
            "INSERT INTO binary_discovery_topics(session_id, topic_id, state_json, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(session_id, topic_id) DO UPDATE "
            "SET state_json = excluded.state_json, updated_at = excluded.updated_at",
            (cur_session.session_id, topic_id, dump_json(topic_state), now_iso),
        )
        emit_turn_room_event_tx(
            conn,
            room_sync,
            session=cur_session,
            version=cur_ver,
            user_turn_id=user_turn_id if user_visible_action else "",
            assistant_turn_id=assistant_turn_id,
            user_text=user_text,
            assistant_text=assistant_text,
            now_iso=now_iso,
        )
    return {"topicId": topic_id, "assistantTurnId": assistant_turn_id, "action": action}
