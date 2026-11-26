"""Revision challenge handler (schema-v2 disagreement + challengeState + activeDisagreement)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from server.application.sessions import (
    RoomSyncWriter,
    append_audit_event_tx,
    solve_stage_async,
)
from server.core.clock import Clock
from server.core.grid import map_fingerprint
from server.core.llm_client import LLMError
from server.domain.errors import ConflictError, NotFoundError, ValidationError
from server.domain.memory import ensure_design_context, set_active_disagreement
from server.domain.proposals import ProposalSearchExhausted
from server.domain.sessions import describe_diff
from server.i18n.messages import format_solver_delta_notice, get_message
from server.llm.pipelines.proposal import ProposalPipeline
from server.llm.pipelines.understanding import UnderstandingPipeline
from server.persistence.database import SQLiteDatabase, dump_json, load_json
from server.persistence.models import RevisionChallengeRecord, SessionReadModel

from .._shared import (
    apply_transitions_to_rows,
    build_proposal_offer_and_binding,
    build_turn_cards,
    classify_failure,
    emit_dashboard_node_tx,
    emit_turn_room_event_tx,
    generate_safe_failure_analysis,
    insert_turn_tx,
    require_current_version_tx,
)

CHALLENGE_CARD_SCHEMA_VERSION = 2


def _update_turn_binding_status_tx(
    conn: Any,
    *,
    session_id: str,
    turn_id: str,
    status: str,
) -> None:
    row = conn.execute(
        "SELECT proposal_binding_json FROM conversation_turns "
        "WHERE session_id = ? AND turn_id = ?",
        (session_id, turn_id),
    ).fetchone()
    if row is None or not row["proposal_binding_json"]:
        return
    binding = load_json(row["proposal_binding_json"], None)
    if not isinstance(binding, dict):
        return
    binding["status"] = status
    conn.execute(
        "UPDATE conversation_turns SET proposal_binding_json = ? "
        "WHERE session_id = ? AND turn_id = ?",
        (dump_json(binding), session_id, turn_id),
    )


def _challenge_choices(language: str) -> list[dict[str, str]]:
    return [
        {
            "choice": "keep_proposal",
            "label": "Yes" if language != "zh-CN" else "是",
        },
        {
            "choice": "revise_proposal",
            "label": "No" if language != "zh-CN" else "否",
        },
    ]


def _challenge_state(
    challenge_id: str,
    status: str,
    source_turn_id: str,
    choices: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "challengeId": challenge_id,
        "status": status,
        "sourceTurnId": source_turn_id,
        "choices": choices,
    }


class ChallengeHandler:
    """Handles proposal challenges and schema-v2 reason/choice flows."""

    def __init__(
        self,
        db: SQLiteDatabase,
        understanding_pipeline: UnderstandingPipeline | None,
        *,
        proposal_pipeline: ProposalPipeline | None = None,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock,
    ) -> None:
        self._db = db
        self._understanding_pipeline = understanding_pipeline
        self._proposal_pipeline = proposal_pipeline
        self._room_sync = room_sync
        self._clock = clock

    async def start_challenge(
        self,
        *,
        read_model: SessionReadModel,
        proposal_id: str,
        user_text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Start a challenge against an active pending proposal."""
        session = read_model.session
        proposal = next(
            (p for p in read_model.proposals if p.proposal_id == proposal_id), None
        )
        if proposal is None or proposal.status != "pending":
            raise NotFoundError("PROPOSAL_NOT_FOUND", "error.version_conflict")
        if proposal.base_version_id != session.current_version_id:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")

        challenge_id = f"chg_{uuid.uuid4().hex[:16]}"
        now_iso = self._clock.utc_now()
        user_turn_id = uuid.uuid4().hex
        assistant_turn_id = uuid.uuid4().hex
        proposal_summary = str(proposal.proposal.get("summary") or "")
        user_pos = user_text.strip()[:400] or (
            "Challenge this proposal" if session.language != "zh-CN" else "质疑当前修改方案"
        )
        ai_pos = proposal_summary[:400]
        core_dis = (
            f"You questioned the current proposal ({proposal_summary})."
            if session.language != "zh-CN"
            else f"你对当前修改方案（{proposal_summary}）提出了质疑。"
        )
        next_q = (
            "Tell me why you want to challenge this proposal so I can compare both directions."
            if session.language != "zh-CN"
            else "请说明你质疑当前修改方案的原因，我会把你的观点与当前方案做对比。"
        )
        disagreement = {
            "status": "active",
            "subject": "ai_revision_challenge",
            "phase": "reason_pending",
            "challengeId": challenge_id,
            "disagreementId": challenge_id,
            "userPosition": user_pos,
            "aiPosition": ai_pos,
            "coreDisagreement": core_dis,
            "nextQuestion": next_q,
            "proposalSummary": proposal_summary,
            "displayCard": True,
            "resolution": None,
        }
        challenge_state = _challenge_state(
            challenge_id, "awaiting_reason", proposal.assistant_turn_id, []
        )
        challenge_card = {
            "challengeId": challenge_id,
            "proposalId": proposal_id,
            "sourceTurnId": proposal.assistant_turn_id,
            "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
            "status": "awaiting_reason",
            "choices": [],
        }
        guidance = {
            "move": "offer_perspective",
            "disagreement": disagreement,
            "challengeState": challenge_state,
            "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
            "challengeCard": challenge_card,
        }

        with self._db.write_tx() as conn:
            cur_session, cur_ver = require_current_version_tx(
                conn, session.session_id, proposal.base_version_id, clock=self._clock
            )
            _update_turn_binding_status_tx(
                conn,
                session_id=cur_session.session_id,
                turn_id=proposal.assistant_turn_id,
                status="challenged",
            )
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
                content=next_q,
                language=cur_session.language,
                version_id=cur_ver.version_id,
                created_at=now_iso,
                guidance=guidance,
            )
            conn.execute(
                "INSERT INTO revision_challenges("
                "challenge_id, session_id, base_version_id, source_proposal_turn_id, "
                "challenge_turn_id, status, current_reason_turn_id, reviews_json, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 'awaiting_reason', NULL, '[]', ?, ?)",
                (
                    challenge_id,
                    cur_session.session_id,
                    cur_ver.version_id,
                    proposal.assistant_turn_id,
                    assistant_turn_id,
                    now_iso,
                    now_iso,
                ),
            )
            ctx_dis = {**disagreement, "phase": "reason_review"}
            updated_ctx = set_active_disagreement(
                ensure_design_context(cur_ver.design_context),
                ctx_dis,
                cur_ver.version_id,
                assistant_turn_id,
            )
            conn.execute(
                "UPDATE level_versions SET design_context_json = ? "
                "WHERE session_id = ? AND version_id = ?",
                (dump_json(updated_ctx.to_wire_dict()), cur_session.session_id, cur_ver.version_id),
            )
            append_audit_event_tx(
                conn,
                cur_session.session_id,
                "disagreement_started",
                {
                    "challengeId": challenge_id,
                    "proposalId": proposal_id,
                    "versionId": cur_ver.version_id,
                    "disagreement": disagreement,
                },
                now_iso,
            )
            emit_turn_room_event_tx(
                conn,
                self._room_sync,
                session=cur_session,
                version=cur_ver,
                user_turn_id=user_turn_id,
                assistant_turn_id=assistant_turn_id,
                user_text=user_text,
                assistant_text=next_q,
                cards=build_turn_cards(guidance),
                now_iso=now_iso,
            )
            emit_dashboard_node_tx(
                conn,
                self._room_sync,
                session=cur_session,
                version=cur_ver,
                node_id=f"player-challenge:{challenge_id}",
                node_type="player_challenge",
                node_status="awaiting_reason",
                entries=[
                    {
                        "entryId": f"challenge-action:{challenge_id}",
                        "kind": "player_action",
                        "text": "Challenge this plan",
                        "label": "Player action",
                        "occurredAt": now_iso,
                        "language": cur_session.language,
                    },
                    {
                        "entryId": f"llm:{assistant_turn_id}",
                        "kind": "llm_message",
                        "text": next_q,
                        "label": "LLM",
                        "occurredAt": now_iso,
                        "language": cur_session.language,
                    },
                ],
                now_iso=now_iso,
            )
        return {"challengeId": challenge_id, "assistantTurnId": assistant_turn_id}

    async def continue_challenge(
        self,
        *,
        read_model: SessionReadModel,
        challenge: RevisionChallengeRecord,
        user_text: str,
        choice: str | None,
        idempotency_key: str,
        request_id: str,
        deadline: float | None = None,
        exit_challenge: bool = False,
        proposal_pipeline: ProposalPipeline | None = None,
    ) -> dict[str, Any]:
        """Review challenge reason, resolve keep/cancel, or build alternative on revise."""
        session = read_model.session
        proposal = next(
            (
                p
                for p in read_model.proposals
                if p.assistant_turn_id == challenge.source_proposal_turn_id
            ),
            None,
        )
        if proposal is None:
            proposal = next(
                (
                    p
                    for p in reversed(read_model.proposals)
                    if p.base_version_id == challenge.base_version_id
                ),
                None,
            )
        if proposal is None:
            raise NotFoundError("PROPOSAL_NOT_FOUND", "error.version_conflict")

        current_ver = next(
            (v for v in read_model.versions if v.version_id == challenge.base_version_id), None
        )
        if current_ver is None:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")

        now_iso = self._clock.utc_now()
        user_turn_id = uuid.uuid4().hex
        assistant_turn_id = uuid.uuid4().hex
        model, attempts_used, latency_ms = "", 0, 0
        reviews = list(challenge.reviews or ())
        proposal_summary = str(proposal.proposal.get("summary") or "")
        active_dis_ctx: Mapping[str, Any] = (
            (current_ver.design_context or {}).get("activeDisagreement") or {}
        )
        effective_choice = "cancel_challenge" if exit_challenge else choice
        new_proposal_payload: dict[str, Any] | None = None
        new_binding: dict[str, Any] | None = None
        disagreement_event_type = "disagreement_updated"

        if effective_choice:
            if effective_choice not in {"keep_proposal", "revise_proposal", "cancel_challenge"}:
                raise ValidationError("INVALID_CHALLENGE_CHOICE", "error.validation_failed")
            if effective_choice == "keep_proposal":
                if challenge.status not in {"awaiting_choice", "choice_pending"}:
                    raise ConflictError("CHALLENGE_NOT_READY", "error.version_conflict")
                source_turn = next(
                    (
                        t
                        for t in read_model.turns
                        if t.turn_id == challenge.source_proposal_turn_id
                    ),
                    None,
                )
                source_binding = (source_turn.proposal_binding if source_turn else None) or {}
                transitions = list(
                    ((source_binding.get("executionBrief") or {}).get("requiredTransitions"))
                    or ()
                )
                proposed_rows = list(proposal.proposal.get("proposedRows") or ())
                if transitions:
                    replay_rows = apply_transitions_to_rows(current_ver.rows, transitions)
                elif proposed_rows:
                    replay_rows = proposed_rows
                else:
                    raise ConflictError("INVALID_CARD_SOURCE", "error.version_conflict")
                await solve_stage_async(replay_rows, clock=self._clock)

            next_status = "resolved"
            disagreement_event_type = "disagreement_resolved"
            if effective_choice == "revise_proposal":
                active_prop_pipe = proposal_pipeline or self._proposal_pipeline
                if active_prop_pipe is None:
                    raise ConflictError("LLM_UNAVAILABLE", "error.configuration")
                excluded_fps = [
                    map_fingerprint(p.proposal.get("proposedRows") or ())
                    for p in read_model.proposals
                    if p.base_version_id == current_ver.version_id
                    and p.proposal.get("proposedRows")
                ]
                if proposal.proposal.get("proposedRows"):
                    challenged_fp = map_fingerprint(proposal.proposal["proposedRows"])
                    if challenged_fp not in excluded_fps:
                        excluded_fps.append(challenged_fp)
                excluded_fps.append(map_fingerprint(current_ver.rows))
                reason_bits = [
                    str(r.get("reason") or "").strip()
                    for r in reviews
                    if isinstance(r, Mapping) and str(r.get("reason") or "").strip()
                ]
                if user_text.strip() and user_text.strip().lower() not in {"no", "否"}:
                    reason_bits.append(user_text.strip())
                brief = "; ".join(reason_bits) or f"Revise proposal excluding: {proposal_summary}"
                user_turns = [
                    {"id": t.turn_id, "content": t.content}
                    for t in read_model.turns
                    if t.version_id == current_ver.version_id and t.role == "user"
                ]
                if user_text.strip() and user_text.strip().lower() not in {"no", "否"}:
                    user_turns.append({"id": user_turn_id, "content": user_text.strip()})
                if not user_turns:
                    user_turns = [{"id": user_turn_id, "content": brief}]
                resolution_label = "user"
                try:
                    prop_res = await active_prop_pipe.generate_proposal(
                        grid_or_rows=current_ver.rows,
                        authorized_brief=brief,
                        designer_turns=user_turns,
                        excluded_fingerprints=excluded_fps,
                        design_context=current_ver.design_context,
                        entity_bindings=current_ver.entity_bindings,
                        language=session.language,
                        deadline=deadline,
                        request_id=request_id or idempotency_key,
                    )
                except ProposalSearchExhausted as exc:
                    assistant_text = await generate_safe_failure_analysis(
                        active_prop_pipe,
                        rows=current_ver.rows,
                        designer_turns=user_turns,
                        failure_code="PROPOSAL_SEARCH_EXHAUSTED",
                        failure_summary=str(exc.diagnostics.get("failureReasons") or exc),
                        requirements=None,
                        entity_bindings=current_ver.entity_bindings,
                        language=session.language,
                        deadline=deadline,
                        request_id=request_id or idempotency_key,
                    )
                    guidance: dict[str, Any] = {
                        "move": "offer_perspective",
                        "disagreement": {
                            **dict(active_dis_ctx),
                            "status": "resolved",
                            "subject": "ai_revision_challenge",
                            "challengeId": challenge.challenge_id,
                            "resolution": resolution_label,
                            "displayCard": False,
                            "nextQuestion": None,
                        },
                        "challengeState": _challenge_state(
                            challenge.challenge_id,
                            "resolved",
                            challenge.source_proposal_turn_id,
                            [],
                        ),
                        "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                        "challengeCard": {
                            "challengeId": challenge.challenge_id,
                            "proposalId": proposal.proposal_id,
                            "sourceTurnId": challenge.source_proposal_turn_id,
                            "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                            "status": "resolved",
                            "selectedChoice": effective_choice,
                            "choices": [],
                        },
                    }
                else:
                    candidate_rows = list(prop_res.candidate_grid.rows)
                    diff = describe_diff(current_ver.rows, candidate_rows)
                    notice = prop_res.tradeoff_notice or format_solver_delta_notice(
                        current_ver.solver, prop_res.solve_result, session.language
                    )
                    summary = str(
                        (prop_res.requirement_record or {}).get("summary") or brief
                    )[:240]
                    assistant_text = notice or summary
                    offer, new_binding = build_proposal_offer_and_binding(
                        base_version=current_ver,
                        candidate_rows=candidate_rows,
                        summary=summary,
                        rationale=assistant_text,
                        language=session.language,
                        available_actions=["execute_revision"],
                        solve_result=prop_res.solve_result.as_dict(),
                        request_text=brief,
                    )
                    new_proposal_payload = {
                        "proposedRows": candidate_rows,
                        "summary": summary,
                        "diff": diff,
                        "validation": prop_res.solve_result.as_dict(),
                        "requirementRecord": prop_res.requirement_record,
                        "revisionPlan": prop_res.revision_plan,
                        "tradeoffNotice": notice,
                        "source": prop_res.source,
                    }
                    model = active_prop_pipe.model
                    attempts_used = prop_res.attempts_used
                    latency_ms = prop_res.latency_ms
                    guidance = {
                        "move": "offer_revision",
                        "proposalOffer": offer,
                        "disagreement": {
                            **dict(active_dis_ctx),
                            "status": "resolved",
                            "subject": "ai_revision_challenge",
                            "challengeId": challenge.challenge_id,
                            "resolution": resolution_label,
                            "displayCard": False,
                            "nextQuestion": None,
                        },
                        "challengeState": _challenge_state(
                            challenge.challenge_id,
                            "resolved",
                            challenge.source_proposal_turn_id,
                            [],
                        ),
                        "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                        "challengeCard": {
                            "challengeId": challenge.challenge_id,
                            "proposalId": proposal.proposal_id,
                            "sourceTurnId": challenge.source_proposal_turn_id,
                            "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                            "status": "resolved",
                            "selectedChoice": effective_choice,
                            "choices": [],
                        },
                    }
            else:
                resolution_label = (
                    "ai" if effective_choice == "keep_proposal" else "retain_current"
                )
                assistant_text = (
                    "Understood. Let's continue with your selected direction."
                    if session.language != "zh-CN"
                    else "明白，我将按你选择的方向继续。"
                )
                guidance = {
                    "move": "offer_perspective",
                    "disagreement": {
                        **dict(active_dis_ctx),
                        "status": "resolved",
                        "subject": "ai_revision_challenge",
                        "challengeId": challenge.challenge_id,
                        "resolution": resolution_label,
                        "displayCard": False,
                        "nextQuestion": None,
                    },
                    "challengeState": _challenge_state(
                        challenge.challenge_id,
                        "resolved",
                        challenge.source_proposal_turn_id,
                        [],
                    ),
                    "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                    "challengeCard": {
                        "challengeId": challenge.challenge_id,
                        "proposalId": proposal.proposal_id,
                        "sourceTurnId": challenge.source_proposal_turn_id,
                        "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                        "status": "resolved",
                        "selectedChoice": effective_choice,
                        "choices": [],
                    },
                }
        else:
            if self._understanding_pipeline is None:
                raise ConflictError("LLM_UNAVAILABLE", "error.configuration")
            try:
                review = await self._understanding_pipeline.review_challenge_reason(
                    user_reason=user_text,
                    proposal_summary=proposal_summary,
                    primary_hypothesis=str(
                        active_dis_ctx.get("primaryHypothesis") or user_text
                    ),
                    secondary_hypothesis=str(
                        active_dis_ctx.get("secondaryHypothesis") or proposal_summary
                    ),
                    language=session.language,
                    deadline=deadline,
                    request_id=request_id or idempotency_key,
                )
            except (LLMError, TimeoutError) as exc:
                code, _, details = classify_failure(exc)
                with self._db.write_tx() as conn:
                    append_audit_event_tx(
                        conn,
                        session.session_id,
                        "challenge_reason_review_pending",
                        {
                            "challengeId": challenge.challenge_id,
                            "messageKey": idempotency_key,
                            "baseVersionId": challenge.base_version_id,
                            "code": code,
                            "requestId": request_id or idempotency_key,
                            "details": details,
                        },
                        now_iso,
                    )
                raise
            next_status = "awaiting_choice"
            assistant_text = review.comparison or get_message("error.internal", session.language)
            reviews.append(
                {
                    "reasonTurnId": user_turn_id,
                    "reason": user_text,
                    "relation": review.relation,
                    "merit": review.merit,
                    "comparison": review.comparison,
                    "createdAt": now_iso,
                }
            )
            choices = _challenge_choices(session.language)
            next_q = (
                "Do you still want to keep the current proposal direction?"
                if session.language != "zh-CN"
                else "你是否仍希望保留当前方案方向？"
            )
            disagreement = {
                "status": "active",
                "subject": "ai_revision_challenge",
                "phase": "choice_pending",
                "challengeId": challenge.challenge_id,
                "disagreementId": challenge.challenge_id,
                "userPosition": user_text.strip()[:400],
                "aiPosition": proposal_summary[:400],
                "coreDisagreement": review.comparison,
                "nextQuestion": next_q,
                "proposalSummary": proposal_summary,
                "acceptedReason": user_text.strip()[:1200],
                "displayCard": True,
                "resolution": None,
            }
            challenge_state = _challenge_state(
                challenge.challenge_id,
                "choice_pending",
                challenge.source_proposal_turn_id,
                choices,
            )
            challenge_card = {
                "challengeId": challenge.challenge_id,
                "proposalId": proposal.proposal_id,
                "sourceTurnId": challenge.source_proposal_turn_id,
                "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                "status": "choice_pending",
                "relation": review.relation,
                "merit": review.merit,
                "comparison": review.comparison,
                "choices": choices,
            }
            guidance = {
                "move": "offer_perspective",
                "disagreement": disagreement,
                "challengeState": challenge_state,
                "challengeCardSchemaVersion": CHALLENGE_CARD_SCHEMA_VERSION,
                "challengeCard": challenge_card,
            }
            model = self._understanding_pipeline.model
            attempts_used = review.attempts_used
            latency_ms = review.latency_ms

        with self._db.write_tx() as conn:
            cur_session, cur_ver = require_current_version_tx(
                conn, session.session_id, challenge.base_version_id, clock=self._clock
            )
            if next_status == "resolved":
                next_ctx = set_active_disagreement(
                    ensure_design_context(cur_ver.design_context),
                    None,
                    cur_ver.version_id,
                    assistant_turn_id,
                )
            else:
                next_ctx = set_active_disagreement(
                    ensure_design_context(cur_ver.design_context),
                    guidance["disagreement"],
                    cur_ver.version_id,
                    assistant_turn_id,
                )
            conn.execute(
                "UPDATE level_versions SET design_context_json = ? "
                "WHERE session_id = ? AND version_id = ?",
                (dump_json(next_ctx.to_wire_dict()), cur_session.session_id, cur_ver.version_id),
            )
            if effective_choice == "keep_proposal":
                _update_turn_binding_status_tx(
                    conn,
                    session_id=cur_session.session_id,
                    turn_id=challenge.source_proposal_turn_id,
                    status="active",
                )
            elif effective_choice in {"cancel_challenge", "revise_proposal"}:
                conn.execute(
                    "UPDATE change_proposals SET status = 'superseded', decided_at = ? "
                    "WHERE session_id = ? AND status = 'pending'",
                    (now_iso, cur_session.session_id),
                )
                _update_turn_binding_status_tx(
                    conn,
                    session_id=cur_session.session_id,
                    turn_id=challenge.source_proposal_turn_id,
                    status="stale",
                )
            visible_user = user_text or str(effective_choice)
            insert_turn_tx(
                conn,
                turn_id=user_turn_id,
                session_id=cur_session.session_id,
                request_id=idempotency_key,
                role="user",
                content=visible_user,
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
                proposal_binding=new_binding,
                model=model,
                attempts_used=attempts_used,
                latency_ms=latency_ms,
            )
            new_proposal_id: str | None = None
            if new_proposal_payload is not None:
                new_proposal_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO change_proposals("
                    "proposal_id, session_id, base_version_id, assistant_turn_id, status, "
                    "proposal_json, created_at"
                    ") VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        new_proposal_id,
                        cur_session.session_id,
                        cur_ver.version_id,
                        assistant_turn_id,
                        dump_json(new_proposal_payload),
                        now_iso,
                    ),
                )
            reason_turn_id = (
                user_turn_id if effective_choice is None else challenge.current_reason_turn_id
            )
            conn.execute(
                "UPDATE revision_challenges SET status = ?, current_reason_turn_id = ?, "
                "reviews_json = ?, updated_at = ? WHERE challenge_id = ?",
                (
                    next_status,
                    reason_turn_id,
                    dump_json(reviews),
                    now_iso,
                    challenge.challenge_id,
                ),
            )
            append_audit_event_tx(
                conn,
                cur_session.session_id,
                disagreement_event_type,
                {
                    "challengeId": challenge.challenge_id,
                    "versionId": cur_ver.version_id,
                    "status": next_status,
                    "choice": effective_choice,
                    "disagreement": guidance.get("disagreement"),
                    **({"proposalId": new_proposal_id} if new_proposal_id else {}),
                },
                now_iso,
            )
            append_audit_event_tx(
                conn,
                cur_session.session_id,
                "reply_delivery_outcome",
                {
                    "messageKey": idempotency_key,
                    "requestId": request_id or idempotency_key,
                    "task": "challenge",
                    "baseVersionId": cur_ver.version_id,
                    "challengeId": challenge.challenge_id,
                    "modelGenerationSucceeded": True,
                    "reliableBodyDelivered": True,
                    "fullQualityPassed": True,
                    "verifiedProposal": new_proposal_id is not None,
                    "latencyMs": latency_ms,
                },
                now_iso,
            )
            cards = build_turn_cards(guidance)
            emit_turn_room_event_tx(
                conn,
                self._room_sync,
                session=cur_session,
                version=cur_ver,
                user_turn_id=user_turn_id,
                assistant_turn_id=assistant_turn_id,
                user_text=visible_user,
                assistant_text=assistant_text,
                cards=cards,
                now_iso=now_iso,
            )
            challenge_entries = [
                {
                    "entryId": f"player:{user_turn_id}",
                    "kind": "player_message",
                    "text": visible_user,
                    "label": "Player",
                    "occurredAt": now_iso,
                    "language": cur_session.language,
                },
                {
                    "entryId": f"llm:{assistant_turn_id}",
                    "kind": "llm_message",
                    "text": assistant_text,
                    "label": "LLM",
                    "occurredAt": now_iso,
                    "language": cur_session.language,
                },
            ]
            emit_dashboard_node_tx(
                conn,
                self._room_sync,
                session=cur_session,
                version=cur_ver,
                node_id=f"player-challenge:{challenge.challenge_id}",
                node_type="player_challenge",
                node_status=(
                    "choice_pending" if next_status == "awaiting_choice" else next_status
                ),
                entries=challenge_entries,
                now_iso=now_iso,
            )
            if new_proposal_id is not None and new_proposal_payload is not None:
                emit_dashboard_node_tx(
                    conn,
                    self._room_sync,
                    session=cur_session,
                    version=cur_ver,
                    node_id=f"proposal:challenge:{challenge.challenge_id}",
                    node_type="proposal",
                    node_status="proposal_ready",
                    node_parent_id=f"player-challenge:{challenge.challenge_id}",
                    entries=[
                        {
                            "entryId": f"proposal-verified:{new_proposal_id}",
                            "kind": "map_diff",
                            "text": dump_json(
                                {
                                    "diff": new_proposal_payload["diff"],
                                    "validation": new_proposal_payload["validation"],
                                }
                            ),
                            "label": "Verified proposal result",
                            "occurredAt": now_iso,
                        }
                    ],
                    now_iso=now_iso,
                )
        return {
            "challengeId": challenge.challenge_id,
            "status": next_status,
            **({"proposalId": new_proposal_id} if new_proposal_id else {}),
        }
