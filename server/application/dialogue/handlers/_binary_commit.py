"""Extracted Policy-2 binary discovery proposal and commit helpers."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from server.application.sessions import RoomSyncWriter, append_audit_event_tx
from server.core.clock import Clock
from server.core.grid import GridValidationError, map_fingerprint, parse_grid
from server.domain.blueprints import GRID_HEIGHT, GRID_WIDTH
from server.domain.errors import ConflictError, ValidationError
from server.domain.proposals import (
    ALLOWED_EFFECTS,
    EDITABLE_OPERATORS,
    EFFECT_OPERATORS,
    MAX_EDIT_BUDGET,
    OPERATOR_COMPONENT,
    ProposalSearchExhausted,
    compile_choice_requirements_deterministic,
    normalize_requirement_record,
    validate_requirement_record,
)
from server.domain.sessions import describe_diff
from server.i18n.messages import format_solver_delta_notice, get_message
from server.llm.grounding import build_stage_facts
from server.llm.pipelines.proposal import ProposalPipeline
from server.persistence.database import SQLiteDatabase, dump_json, load_json
from server.persistence.models import SessionReadModel
from server.persistence.repositories.operational_repo import OperationalRepository

from .._shared import (
    apply_transitions_to_rows,
    build_proposal_offer_and_binding,
    generate_safe_failure_analysis,
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
        "status": "pending",
        "selectedChoiceId": None,
        "choices": [
            {"choiceId": c["choiceId"], "letter": c["letter"], "label": c["label"]}
            for c in choices
        ],
        "_internalChoices": choices,
    }


def _public_question_card(card: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in card.items() if k != "_internalChoices"}


def _collect_excluded_fingerprints(
    read_model: SessionReadModel,
    current_ver: Any,
) -> list[str]:
    excluded: list[str] = []
    seen: set[str] = set()

    def _add(fp: str) -> None:
        if fp and fp not in seen:
            seen.add(fp)
            excluded.append(fp)

    for prop in read_model.proposals:
        if prop.base_version_id == current_ver.version_id and prop.proposal.get("proposedRows"):
            _add(map_fingerprint(prop.proposal["proposedRows"]))

    for turn in read_model.turns:
        if turn.version_id != current_ver.version_id or turn.role != "assistant":
            continue
        binding = turn.proposal_binding
        if not isinstance(binding, Mapping):
            continue
        brief = binding.get("executionBrief")
        if not isinstance(brief, Mapping):
            continue
        transitions = brief.get("requiredTransitions")
        if not isinstance(transitions, list) or not transitions:
            continue
        try:
            cand_rows = apply_transitions_to_rows(current_ver.rows, transitions)
        except (ConflictError, ValidationError, GridValidationError, ValueError):
            continue
        _add(map_fingerprint(cand_rows))

    return excluded


def _mark_prior_active_offers_stale_tx(
    conn: Any,
    *,
    session_id: str,
    version_id: str,
) -> None:
    rows = conn.execute(
        "SELECT turn_id, proposal_binding_json FROM conversation_turns "
        "WHERE session_id = ? AND version_id = ? AND role = 'assistant' "
        "AND proposal_binding_json IS NOT NULL",
        (session_id, version_id),
    ).fetchall()
    for row in rows:
        binding = load_json(row["proposal_binding_json"], None)
        if not isinstance(binding, dict):
            continue
        if str(binding.get("status") or "active") in {"active", "ready"}:
            binding["status"] = "stale"
            conn.execute(
                "UPDATE conversation_turns SET proposal_binding_json = ? "
                "WHERE session_id = ? AND turn_id = ?",
                (dump_json(binding), session_id, row["turn_id"]),
            )


def _sanitize_choices_for_state(choices: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_qid: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for idx, ch in enumerate(choices):
        item = dict(ch)
        if not item.get("letter"):
            item["letter"] = "A" if idx == 0 else "B"
        qid = str(item.get("questionId") or f"__idx_{idx}")
        if qid not in by_qid:
            order.append(qid)
        by_qid[qid] = item
    return [by_qid[qid] for qid in order]


def _merge_choice_strategies(choices: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    strategies = [
        dict(ch["strategy"])
        for ch in choices
        if isinstance(ch.get("strategy"), Mapping)
    ]
    if not strategies:
        return []
    if len(strategies) == 1:
        single = dict(strategies[0])
        budget = single.get("editBudget")
        single["editBudget"] = max(
            1,
            min(
                MAX_EDIT_BUDGET,
                int(budget) if isinstance(budget, int) and not isinstance(budget, bool) else 1,
            ),
        )
        single.setdefault("metricGoals", [])
        return [single]

    merged_ops = list(
        dict.fromkeys(
            op
            for strat in strategies
            for op in (strat.get("operators") or ())
            if op in EDITABLE_OPERATORS
        )
    )[:4]
    if not merged_ops:
        return strategies[:3]

    touched = {OPERATOR_COMPONENT[op] for op in merged_ops if op in OPERATOR_COMPONENT}
    merged_preserve = list(
        dict.fromkeys(
            p
            for strat in strategies
            for p in (strat.get("preserve") or ())
            if p not in touched
        )
    )
    op_set = set(merged_ops)
    preferred_effects = [
        *(str(s.get("effect") or "") for s in reversed(strategies)),
        "open_route",
        "narrow_route",
        "adjust_internal_walls",
        "reshape_water",
    ]
    merged_effect = next(
        (
            eff
            for eff in preferred_effects
            if eff in ALLOWED_EFFECTS and op_set.intersection(EFFECT_OPERATORS[eff])
        ),
        str(strategies[-1].get("effect") or "open_route"),
    )

    focuses = [s.get("focus") for s in strategies]
    merged_focus: dict[str, int] | None = None
    if all(
        isinstance(f, Mapping)
        and isinstance(f.get("row"), int)
        and isinstance(f.get("column"), int)
        and isinstance(f.get("radius"), int)
        for f in focuses
    ):
        min_r = min(max(1, f["row"] - f["radius"]) for f in focuses)  # type: ignore[index]
        max_r = max(min(GRID_HEIGHT, f["row"] + f["radius"]) for f in focuses)  # type: ignore[index]
        min_c = min(max(1, f["column"] - f["radius"]) for f in focuses)  # type: ignore[index]
        max_c = max(min(GRID_WIDTH, f["column"] + f["radius"]) for f in focuses)  # type: ignore[index]
        center_r = max(1, min(GRID_HEIGHT, (min_r + max_r) // 2))
        center_c = max(1, min(GRID_WIDTH, (min_c + max_c) // 2))
        needed_radius = max(
            abs(max_r - center_r),
            abs(center_r - min_r),
            abs(max_c - center_c),
            abs(center_c - min_c),
        )
        if needed_radius <= 3:
            merged_focus = {
                "row": center_r,
                "column": center_c,
                "radius": max(1, needed_radius),
            }

    total_budget = sum(
        int(s["editBudget"])
        if isinstance(s.get("editBudget"), int) and not isinstance(s.get("editBudget"), bool)
        else 1
        for s in strategies
    )
    merged_strategy: dict[str, Any] = {
        "effect": merged_effect,
        "focus": merged_focus,
        "operators": merged_ops,
        "preserve": merged_preserve,
        "editBudget": max(1, min(MAX_EDIT_BUDGET, total_budget)),
        "metricGoals": [],
    }
    return [merged_strategy]


def _mark_prior_question_selected_tx(
    conn: Any,
    *,
    session_id: str,
    question_id: str,
    choice_id: str,
) -> None:
    rows = conn.execute(
        "SELECT turn_id, guidance_json FROM conversation_turns "
        "WHERE session_id = ? AND role = 'assistant' ORDER BY sequence_number DESC",
        (session_id,),
    ).fetchall()
    for row in rows:
        g = load_json(row["guidance_json"], {})
        if not isinstance(g, dict):
            continue
        pq = g.get("proposalQuestion")
        if isinstance(pq, dict) and pq.get("questionId") == question_id:
            pq["status"] = "selected"
            pq["selectedChoiceId"] = choice_id
            conn.execute(
                "UPDATE conversation_turns SET guidance_json = ? "
                "WHERE session_id = ? AND turn_id = ?",
                (dump_json(g), session_id, row["turn_id"]),
            )
            break


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
    display_language: str | None = None,
    record_choice_selected: bool = False,
    complete_idempotency_scope: str | None = None,
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
    topic_binding = f"{current_ver.version_id}:{map_fingerprint(current_ver.rows)}"
    clean_choices = _sanitize_choices_for_state(prior_choices)
    initial_turn_id = str(initial_turns[0]["id"]) if initial_turns else ""
    search_exhausted = False

    if action == "protected_refusal":
        assistant_text = step_res.refusal_message or get_message(
            "error.protected_edit", session.language
        )
        guidance = {"replyType": "protected_refusal"}
        topic_state: dict[str, Any] = {
            "topicId": topic_id,
            "sessionId": session.session_id,
            "versionId": current_ver.version_id,
            "binding": topic_binding,
            "language": session.language,
            "status": "cancelled",
            "questionCount": len(clean_choices),
            "initialRequest": initial_request,
            "initialTurnId": initial_turn_id,
            "initialTurns": initial_turns,
            "userTurns": initial_turns,
            "choices": clean_choices,
            "pendingQuestion": None,
            "retryAction": None,
            "failureCode": None,
            "initialMessageKey": idempotency_key,
            "updatedAt": now_iso,
        }
    elif action == "ask":
        qid = f"q_{uuid.uuid4().hex[:12]}"
        q_num = len(clean_choices) + 1
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
        guidance = {
            "move": "clarify_intent",
            "proposalQuestion": _public_question_card(card),
            "proposalDiscovery": {"topicId": topic_id, "status": "clarifying"},
        }
        topic_state = {
            "topicId": topic_id,
            "sessionId": session.session_id,
            "versionId": current_ver.version_id,
            "binding": topic_binding,
            "language": session.language,
            "status": "clarifying",
            "questionCount": q_num,
            "initialRequest": initial_request,
            "initialTurnId": initial_turn_id,
            "initialTurns": initial_turns,
            "userTurns": initial_turns,
            "initialRequirements": verified_initial_req,
            "initialMessageKey": idempotency_key,
            "choices": clean_choices,
            "pendingQuestion": card,
            "retryAction": None,
            "failureCode": None,
            "updatedAt": now_iso,
        }
    elif action == "blocked":
        assistant_text = str(step.get("note") or "") or get_message(
            "error.validation_failed", session.language
        )
        guidance = {
            "replyType": "clarification_blocked",
            "proposalDiscovery": {"topicId": topic_id, "status": "revision_needed"},
        }
        topic_state = {
            "topicId": topic_id,
            "sessionId": session.session_id,
            "versionId": current_ver.version_id,
            "binding": topic_binding,
            "language": session.language,
            "status": "revision_needed",
            "questionCount": len(clean_choices),
            "initialRequest": initial_request,
            "initialTurnId": initial_turn_id,
            "initialTurns": initial_turns,
            "userTurns": initial_turns,
            "choices": clean_choices,
            "pendingQuestion": None,
            "retryAction": None,
            "failureCode": "BINARY_DIRECTIONS_UNAVAILABLE",
            "initialMessageKey": idempotency_key,
            "updatedAt": now_iso,
        }
    else:
        compiled_reqs = [
            {k: v for k, v in req.items() if k != "requirementId"}
            for req in (
                list((verified_initial_req or {}).get("requirements") or ())
                + compile_choice_requirements_deterministic(clean_choices, initial_turns)
            )
        ]
        compiled_bindings = [
            {k: v for k, v in binding.items() if k != "authority"}
            if isinstance(binding, Mapping)
            else binding
            for binding in ((verified_initial_req or {}).get("automaticBindings") or ())
        ]
        snapshot = build_stage_facts(
            parse_grid(current_ver.rows), current_ver.entity_bindings
        ).to_prompt_dict()
        raw_req_payload, _ = normalize_requirement_record(
            {
                "requirements": compiled_reqs,
                "automaticBindings": compiled_bindings,
                "exactTransitions": list(
                    (verified_initial_req or {}).get("exactTransitions") or ()
                ),
            },
            initial_turns,
            snapshot,
            design_context=current_ver.design_context,
        )
        req_record = validate_requirement_record(raw_req_payload, initial_turns, snapshot)
        plan_strategies = list(step.get("planStrategies") or ())
        precompiled = (
            plan_strategies if plan_strategies else _merge_choice_strategies(clean_choices)
        )
        promises = [c["promise"] for c in clean_choices if c.get("promise")]
        excluded_fps = _collect_excluded_fingerprints(read_model, current_ver)
        assert proposal_pipeline is not None
        try:
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
        except ProposalSearchExhausted as exc:
            search_exhausted = True
            assistant_text = await generate_safe_failure_analysis(
                proposal_pipeline,
                rows=current_ver.rows,
                designer_turns=initial_turns,
                failure_code="PROPOSAL_SEARCH_EXHAUSTED",
                failure_summary=str(exc.diagnostics.get("failureReasons") or exc),
                requirements=req_record,
                entity_bindings=current_ver.entity_bindings,
                language=session.language,
                deadline=deadline,
                request_id=request_id or idempotency_key,
            )
            if clean_choices:
                last_choice = clean_choices[-1]
                retry_action: dict[str, Any] = {
                    "kind": "choice",
                    "baseVersionId": current_ver.version_id,
                    "topicId": topic_id,
                    "questionId": str(last_choice.get("questionId") or ""),
                    "choiceId": str(last_choice.get("choiceId") or ""),
                    "displayLanguage": display_language or session.language,
                    "idempotencyKey": idempotency_key,
                }
            else:
                retry_action = {
                    "kind": "start",
                    "baseVersionId": current_ver.version_id,
                    "topicId": topic_id,
                    "content": initial_request,
                    "requestProposal": True,
                    "idempotencyKey": idempotency_key,
                }
            guidance = {
                "replyType": "proposal_search_exhausted",
                "proposalDiscovery": {"topicId": topic_id, "status": "retry_pending"},
            }
            topic_state = {
                "topicId": topic_id,
                "sessionId": session.session_id,
                "versionId": current_ver.version_id,
                "binding": topic_binding,
                "language": session.language,
                "status": "retry_pending",
                "questionCount": len(clean_choices),
                "initialRequest": initial_request,
                "initialTurnId": initial_turn_id,
                "initialTurns": initial_turns,
                "userTurns": initial_turns,
                "initialRequirements": verified_initial_req,
                "choices": clean_choices,
                "pendingQuestion": None,
                "retryAction": retry_action,
                "failureCode": "PROPOSAL_SEARCH_EXHAUSTED",
                "initialMessageKey": idempotency_key,
                "updatedAt": now_iso,
            }
        else:
            diff = describe_diff(current_ver.rows, proposal_res.candidate_grid.rows)
            notice = proposal_res.tradeoff_notice or format_solver_delta_notice(
                current_ver.solver, proposal_res.solve_result, session.language
            )
            summary = str(req_record.get("summary") or initial_request)[:240]
            answer_labels = [str(c.get("label") or "") for c in clean_choices if c.get("label")]
            offer, binding = build_proposal_offer_and_binding(
                base_version=current_ver,
                candidate_rows=proposal_res.candidate_grid.rows,
                summary=summary,
                rationale=notice,
                language=session.language,
                solve_result=proposal_res.solve_result.as_dict(),
                request_text=initial_request,
                answer_texts=answer_labels,
                verified_actions=answer_labels,
                binary_compact=bool(answer_labels),
            )
            binding["requirementRecord"] = proposal_res.requirement_record
            binding["revisionPlan"] = proposal_res.revision_plan
            assistant_text = notice or summary
            guidance = {
                "move": "offer_revision",
                "proposalOffer": offer,
                "proposalDiscovery": {"topicId": topic_id, "status": "proposal_ready"},
            }
            topic_state = {
                "topicId": topic_id,
                "sessionId": session.session_id,
                "versionId": current_ver.version_id,
                "binding": topic_binding,
                "language": session.language,
                "status": "proposal_ready",
                "questionCount": len(clean_choices),
                "initialRequest": initial_request,
                "initialTurnId": initial_turn_id,
                "initialTurns": initial_turns,
                "userTurns": initial_turns,
                "initialRequirements": verified_initial_req,
                "choices": clean_choices,
                "pendingQuestion": None,
                "retryAction": None,
                "failureCode": None,
                "initialMessageKey": idempotency_key,
                "diffCount": len(diff),
                "updatedAt": now_iso,
            }

    with db.write_tx() as conn:
        cur_session, cur_ver = require_current_version_tx(
            conn, session.session_id, current_ver.version_id, clock=clock
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
            append_audit_event_tx(
                conn,
                cur_session.session_id,
                "proposal_request_requested",
                {
                    "messageKey": idempotency_key,
                    "baseVersionId": cur_ver.version_id,
                    "topicId": topic_id,
                },
                now_iso,
            )
        elif clean_choices:
            last_choice = clean_choices[-1]
            _mark_prior_question_selected_tx(
                conn,
                session_id=cur_session.session_id,
                question_id=str(last_choice.get("questionId") or ""),
                choice_id=str(last_choice.get("choiceId") or ""),
            )
            if record_choice_selected:
                append_audit_event_tx(
                    conn,
                    cur_session.session_id,
                    "binary_choice_selected",
                    {
                        "topicId": topic_id,
                        "questionId": str(last_choice.get("questionId") or ""),
                        "choiceId": str(last_choice.get("choiceId") or ""),
                        "displayLanguage": display_language or cur_session.language,
                    },
                    now_iso,
                )
        if proposal_res is not None:
            _mark_prior_active_offers_stale_tx(
                conn,
                session_id=cur_session.session_id,
                version_id=cur_ver.version_id,
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
        conn.execute(
            "INSERT INTO binary_discovery_topics(session_id, topic_id, state_json, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(session_id, topic_id) DO UPDATE "
            "SET state_json = excluded.state_json, updated_at = excluded.updated_at",
            (cur_session.session_id, topic_id, dump_json(topic_state), now_iso),
        )
        total_attempts = int(getattr(step_res, "attempts_used", 0) or 0) + (
            int(getattr(proposal_res, "attempts_used", 0) or 0) if proposal_res is not None else 0
        )
        is_deterministic = action == "protected_refusal" and total_attempts == 0
        append_audit_event_tx(
            conn,
            cur_session.session_id,
            "reply_delivery_outcome",
            {
                "messageKey": idempotency_key,
                "requestId": request_id or idempotency_key,
                "task": "binary_discovery",
                "baseVersionId": cur_ver.version_id,
                "topicId": topic_id,
                "modelGenerationSucceeded": not search_exhausted and not is_deterministic,
                "deterministicReceipt": is_deterministic,
                "reliableBodyDelivered": True,
                "fullQualityPassed": proposal_res is not None or action == "ask",
                "verifiedProposal": proposal_res is not None,
                "proposalRequested": True,
                "attemptsUsed": total_attempts,
                "latencyMs": step_res.latency_ms,
                **(
                    {"failureCode": "PROPOSAL_SEARCH_EXHAUSTED"}
                    if search_exhausted
                    else {}
                ),
            },
            now_iso,
        )
        result_payload: dict[str, Any] = {
            "topicId": topic_id,
            "assistantTurnId": assistant_turn_id,
            "action": "retry_pending" if search_exhausted else action,
        }
        if complete_idempotency_scope:
            OperationalRepository.complete_key_tx(
                conn,
                cur_session.session_id,
                complete_idempotency_scope,
                idempotency_key,
                response=result_payload,
            )
    if complete_idempotency_scope:
        return {**result_payload, "_idempotencyCompleted": True}
    return result_payload
