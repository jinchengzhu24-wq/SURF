"""Revision card execution & alternative generation handler (deterministic solve outside tx)."""

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
from server.core.grid import GridValidationError, map_fingerprint
from server.domain.errors import ConflictError, NotFoundError, ValidationError
from server.domain.proposals import ProposalSearchExhausted
from server.domain.sessions import describe_diff
from server.i18n.messages import format_solver_delta_notice, format_verified_diff_summary
from server.llm.pipelines.proposal import ProposalPipeline
from server.persistence.database import SQLiteDatabase, dump_json, load_json
from server.persistence.models import LevelVersionRecord, SessionReadModel
from server.persistence.repositories.operational_repo import OperationalRepository

from .._shared import (
    apply_transitions_to_rows,
    build_proposal_offer_and_binding,
    generate_safe_failure_analysis,
    insert_turn_tx,
    require_current_version_tx,
)


def _proposal_review_guidance(language: str, *, warning_text: str = "") -> dict[str, Any]:
    """Build Policy-2 executed-proposal review guidance (no duplicate purple offer card)."""
    manual_text = (
        "这份提案会先保持待审查状态。如果你更想亲手微调，可以先拒绝它，再在右侧编辑器沿着高亮区域继续改；"
        "我会把你的实际修改当作下一轮共同判断的依据。"
        if language == "zh-CN"
        else "This proposal stays pending while you review it. If you would rather tune it "
        "yourself, reject it first and continue around the highlighted area in the right-hand "
        "editor; I will use your actual edit as our next shared evidence."
    )
    cues: list[dict[str, str]] = [{"type": "manual_edit", "text": manual_text}]
    clean_warn = str(warning_text or "").strip()
    if clean_warn:
        cues.append({"type": "warning", "text": clean_warn})
    return {
        "move": "execute_revision",
        "intentHypothesis": None,
        "intentConfidence": None,
        "followUpQuestion": None,
        "proposalOffer": None,
        "uiCues": cues,
    }


def _mark_turn_binding_status_tx(
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


def _mark_prior_active_offers_stale_tx(
    conn: Any,
    *,
    session_id: str,
    version_id: str,
    source_turn_id: str | None = None,
) -> None:
    if source_turn_id:
        _mark_turn_binding_status_tx(
            conn, session_id=session_id, turn_id=source_turn_id, status="stale"
        )
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


def _collect_excluded_fingerprints(
    read_model: SessionReadModel,
    current_ver: LevelVersionRecord,
) -> list[str]:
    """Collect candidate fingerprints from both change_proposals and offer turn bindings."""
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

    _add(map_fingerprint(current_ver.rows))
    return excluded


class RevisionCardHandler:
    """Executes bound revision cards or generates alternatives OUTSIDE write_tx."""

    def __init__(
        self,
        db: SQLiteDatabase,
        proposal_pipeline: ProposalPipeline | None,
        *,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock,
    ) -> None:
        self._db = db
        self._proposal_pipeline = proposal_pipeline
        self._room_sync = room_sync
        self._clock = clock

    @property
    def proposal_pipeline(self) -> ProposalPipeline | None:
        return self._proposal_pipeline

    async def execute_revision(
        self,
        *,
        read_model: SessionReadModel,
        assistant_turn_id: str,
        user_text: str,
        idempotency_key: str,
        request_id: str = "",
        complete_idempotency_scope: str | None = None,
    ) -> dict[str, Any]:
        """Deterministically apply bound transitions, solve OUTSIDE tx, and propose."""
        session = read_model.session
        current_ver = next(
            (v for v in read_model.versions if v.version_id == session.current_version_id), None
        )
        if current_ver is None:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
        target_turn = next(
            (t for t in read_model.turns if t.turn_id == assistant_turn_id), None
        )
        if target_turn is None:
            raise NotFoundError("TURN_NOT_FOUND", "error.version_conflict")
        target_offer = (target_turn.guidance or {}).get("proposalOffer")
        binding = target_turn.proposal_binding
        if (
            target_turn.role != "assistant"
            or not isinstance(target_offer, Mapping)
            or not str(target_offer.get("summary") or "").strip()
            or not isinstance(binding, Mapping)
            or not isinstance(binding.get("executionBrief"), Mapping)
        ):
            raise ConflictError("INVALID_CARD_SOURCE", "error.version_conflict")
        brief = binding["executionBrief"]
        transitions = list(brief.get("requiredTransitions") or ())
        if not transitions:
            raise ConflictError("INVALID_CARD_SOURCE", "error.version_conflict")

        latest_offer_turn_id = next(
            (
                t.turn_id
                for t in reversed(read_model.turns)
                if t.role == "assistant"
                and t.version_id == current_ver.version_id
                and isinstance((t.guidance or {}).get("proposalOffer"), Mapping)
                and str((t.guidance or {}).get("proposalOffer", {}).get("summary") or "").strip()
            ),
            None,
        )
        if (
            target_turn.version_id != current_ver.version_id
            or target_turn.turn_id != latest_offer_turn_id
        ):
            raise ConflictError("PROPOSAL_STALE", "error.version_conflict")
        if binding.get("baseVersionId") != current_ver.version_id:
            raise ConflictError("STALE_REVISION_BINDING", "error.version_conflict")
        expected_fp = binding.get("mapFingerprint")
        if expected_fp and map_fingerprint(current_ver.rows) != expected_fp:
            raise ConflictError("STALE_REVISION_BINDING", "error.version_conflict")
        expected_bfp = binding.get("entityBindingFingerprint")
        actual_bfp = (current_ver.entity_bindings or {}).get("bindingFingerprint")
        if not expected_bfp or not actual_bfp or expected_bfp != actual_bfp:
            raise ConflictError("STALE_REVISION_BINDING", "error.version_conflict")
        binding_status = str(binding.get("status") or "active")
        if binding_status not in {"active", "ready"}:
            raise ConflictError("PROPOSAL_STALE", "error.version_conflict")

        candidate_rows = apply_transitions_to_rows(current_ver.rows, transitions)
        solved = await solve_stage_async(candidate_rows, clock=self._clock)
        return self._commit_executed_proposal(
            session_id=session.session_id,
            base_version_id=current_ver.version_id,
            source_turn_id=target_turn.turn_id,
            source_binding=dict(binding),
            candidate_rows=list(solved.rows),
            solver_dict=solved.as_dict(),
            summary=str(brief.get("summary") or "Revision proposal"),
            user_text=user_text,
            idempotency_key=idempotency_key,
            request_id=request_id,
            source="revision_card_execute",
            model="deterministic",
            attempts_used=0,
            latency_ms=0,
            revision_plan=binding.get("revisionPlan"),
            requirement_record=binding.get("requirementRecord"),
            tradeoff_notice=format_solver_delta_notice(
                current_ver.solver, solved, session.language
            ),
            complete_idempotency_scope=complete_idempotency_scope,
        )

    async def alternative_revision(
        self,
        *,
        read_model: SessionReadModel,
        user_text: str,
        idempotency_key: str,
        request_id: str,
        assistant_turn_id: str | None = None,
        deadline: float | None = None,
        complete_idempotency_scope: str | None = None,
    ) -> dict[str, Any]:
        """Generate an alternative purple offer excluding prior proposed maps OUTSIDE write_tx."""
        if self._proposal_pipeline is None:
            raise ConflictError("LLM_UNAVAILABLE", "error.configuration")
        session = read_model.session
        current_ver = next(
            (v for v in read_model.versions if v.version_id == session.current_version_id), None
        )
        if current_ver is None:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")

        if assistant_turn_id:
            target_turn = next(
                (t for t in read_model.turns if t.turn_id == assistant_turn_id), None
            )
            if target_turn is None:
                raise NotFoundError("TURN_NOT_FOUND", "error.version_conflict")
            target_offer = (target_turn.guidance or {}).get("proposalOffer")
            binding = target_turn.proposal_binding
            if (
                target_turn.role != "assistant"
                or not isinstance(target_offer, Mapping)
                or not str(target_offer.get("summary") or "").strip()
                or not isinstance(binding, Mapping)
            ):
                raise ConflictError("INVALID_CARD_SOURCE", "error.version_conflict")
            available_actions = target_offer.get("availableActions")
            if (
                isinstance(available_actions, list)
                and "alternative_revision" not in available_actions
            ):
                raise ConflictError("INVALID_CARD_SOURCE", "error.version_conflict")
            if (
                target_turn.version_id != current_ver.version_id
                or str(binding.get("status") or "active") not in {"active", "ready"}
            ):
                raise ConflictError("PROPOSAL_STALE", "error.version_conflict")

        excluded_fps = _collect_excluded_fingerprints(read_model, current_ver)
        user_turns = [
            {"id": t.turn_id, "content": t.content}
            for t in read_model.turns
            if t.version_id == current_ver.version_id and t.role == "user"
        ]
        brief = user_text.strip() or (
            user_turns[-1]["content"] if user_turns else "Provide an alternative revision"
        )
        try:
            res = await self._proposal_pipeline.generate_proposal(
                grid_or_rows=current_ver.rows,
                authorized_brief=brief,
                designer_turns=user_turns or [{"id": uuid.uuid4().hex, "content": brief}],
                excluded_fingerprints=excluded_fps,
                design_context=current_ver.design_context,
                entity_bindings=current_ver.entity_bindings,
                language=session.language,
                deadline=deadline,
                request_id=request_id or idempotency_key,
            )
        except ProposalSearchExhausted as exc:
            explanation = await generate_safe_failure_analysis(
                self._proposal_pipeline,
                rows=current_ver.rows,
                designer_turns=user_turns or [{"id": uuid.uuid4().hex, "content": brief}],
                failure_code="PROPOSAL_SEARCH_EXHAUSTED",
                failure_summary=str(exc.diagnostics.get("failureReasons") or exc),
                requirements=None,
                entity_bindings=current_ver.entity_bindings,
                language=session.language,
                deadline=deadline,
                request_id=request_id or idempotency_key,
            )
            exhausted_attempts = int((exc.diagnostics or {}).get("attemptsUsed") or 0)
            return self._commit_exhausted_explanation(
                session_id=session.session_id,
                base_version_id=current_ver.version_id,
                user_text=user_text or brief,
                assistant_text=explanation,
                idempotency_key=idempotency_key,
                request_id=request_id,
                attempts_used=exhausted_attempts,
                complete_idempotency_scope=complete_idempotency_scope,
            )

        notice = res.tradeoff_notice or format_solver_delta_notice(
            current_ver.solver, res.solve_result, session.language
        )
        return self._commit_offer(
            session_id=session.session_id,
            base_version_id=current_ver.version_id,
            source_turn_id=assistant_turn_id,
            candidate_rows=list(res.candidate_grid.rows),
            solver_dict=res.solve_result.as_dict(),
            summary=str((res.requirement_record or {}).get("summary") or brief)[:240],
            user_text=user_text or brief,
            idempotency_key=idempotency_key,
            request_id=request_id,
            source=res.source,
            model=self._proposal_pipeline.model,
            attempts_used=res.attempts_used,
            latency_ms=res.latency_ms,
            revision_plan=res.revision_plan,
            requirement_record=res.requirement_record,
            tradeoff_notice=notice,
            complete_idempotency_scope=complete_idempotency_scope,
        )

    def _commit_exhausted_explanation(
        self,
        *,
        session_id: str,
        base_version_id: str,
        user_text: str,
        assistant_text: str,
        idempotency_key: str,
        request_id: str,
        attempts_used: int = 0,
        complete_idempotency_scope: str | None = None,
    ) -> dict[str, Any]:
        now_iso = self._clock.utc_now()
        user_turn_id = uuid.uuid4().hex
        assistant_turn_id = uuid.uuid4().hex
        guidance = {"replyType": "proposal_search_exhausted"}
        result_payload: dict[str, Any] = {
            "assistantTurnId": assistant_turn_id,
            "exhausted": True,
        }
        with self._db.write_tx() as conn:
            cur_session, cur_ver = require_current_version_tx(
                conn, session_id, base_version_id, clock=self._clock
            )
            insert_turn_tx(
                conn,
                turn_id=user_turn_id,
                session_id=session_id,
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
                session_id=session_id,
                request_id=idempotency_key,
                role="assistant",
                content=assistant_text,
                language=cur_session.language,
                version_id=cur_ver.version_id,
                created_at=now_iso,
                guidance=guidance,
                model=self._proposal_pipeline.model if self._proposal_pipeline else "",
                attempts_used=attempts_used,
            )
            append_audit_event_tx(
                conn,
                session_id,
                "reply_delivery_outcome",
                {
                    "messageKey": idempotency_key,
                    "requestId": request_id or idempotency_key,
                    "task": "alternative_revision",
                    "baseVersionId": cur_ver.version_id,
                    "modelGenerationSucceeded": False,
                    "deterministicReceipt": False,
                    "reliableBodyDelivered": True,
                    "fullQualityPassed": False,
                    "verifiedProposal": False,
                    "proposalRequested": True,
                    "attemptsUsed": attempts_used,
                    "failureCode": "PROPOSAL_SEARCH_EXHAUSTED",
                },
                now_iso,
            )
            if complete_idempotency_scope:
                OperationalRepository.complete_key_tx(
                    conn,
                    session_id,
                    complete_idempotency_scope,
                    idempotency_key,
                    response=result_payload,
                )
        if complete_idempotency_scope:
            return {**result_payload, "_idempotencyCompleted": True}
        return result_payload

    def _commit_offer(
        self,
        *,
        session_id: str,
        base_version_id: str,
        source_turn_id: str | None,
        candidate_rows: list[str],
        solver_dict: dict[str, Any],
        summary: str,
        user_text: str,
        idempotency_key: str,
        source: str,
        model: str,
        attempts_used: int,
        latency_ms: int,
        revision_plan: dict[str, Any] | None = None,
        requirement_record: dict[str, Any] | None = None,
        tradeoff_notice: str,
        request_id: str = "",
        complete_idempotency_scope: str | None = None,
    ) -> dict[str, Any]:
        """Commit a Policy-2 purple offer turn without creating a change_proposals row."""
        now_iso = self._clock.utc_now()
        user_turn_id = uuid.uuid4().hex
        assistant_turn_id = uuid.uuid4().hex
        result_payload: dict[str, Any] = {"assistantTurnId": assistant_turn_id}

        with self._db.write_tx() as conn:
            cur_session, cur_ver = require_current_version_tx(
                conn, session_id, base_version_id, clock=self._clock
            )
            diff = describe_diff(cur_ver.rows, candidate_rows)
            verified_diff = format_verified_diff_summary(diff, cur_session.language)
            assistant_text = tradeoff_notice or verified_diff or summary
            offer, binding = build_proposal_offer_and_binding(
                base_version=cur_ver,
                candidate_rows=candidate_rows,
                summary=summary,
                rationale=assistant_text,
                language=cur_session.language,
                solve_result=solver_dict,
                request_text=user_text,
            )
            binding["requirementRecord"] = requirement_record
            binding["revisionPlan"] = revision_plan
            guidance = {"move": "offer_revision", "proposalOffer": offer}
            _mark_prior_active_offers_stale_tx(
                conn,
                session_id=session_id,
                version_id=cur_ver.version_id,
                source_turn_id=source_turn_id,
            )
            insert_turn_tx(
                conn,
                turn_id=user_turn_id,
                session_id=session_id,
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
                session_id=session_id,
                request_id=idempotency_key,
                role="assistant",
                content=assistant_text,
                language=cur_session.language,
                version_id=cur_ver.version_id,
                created_at=now_iso,
                guidance=guidance,
                proposal_binding=binding,
                model=model,
                attempts_used=attempts_used,
                latency_ms=latency_ms,
            )
            append_audit_event_tx(
                conn,
                session_id,
                "reply_delivery_outcome",
                {
                    "messageKey": idempotency_key,
                    "requestId": request_id or idempotency_key,
                    "task": "alternative_revision",
                    "source": source,
                    "baseVersionId": cur_ver.version_id,
                    "modelGenerationSucceeded": True,
                    "deterministicReceipt": False,
                    "reliableBodyDelivered": True,
                    "fullQualityPassed": True,
                    "verifiedProposal": True,
                    "proposalRequested": True,
                    "attemptsUsed": attempts_used,
                    "latencyMs": latency_ms,
                },
                now_iso,
            )
            if complete_idempotency_scope:
                OperationalRepository.complete_key_tx(
                    conn,
                    session_id,
                    complete_idempotency_scope,
                    idempotency_key,
                    response=result_payload,
                )
        if complete_idempotency_scope:
            return {**result_payload, "_idempotencyCompleted": True}
        return result_payload

    def _commit_executed_proposal(
        self,
        *,
        session_id: str,
        base_version_id: str,
        source_turn_id: str,
        source_binding: dict[str, Any],
        candidate_rows: list[str],
        solver_dict: dict[str, Any],
        summary: str,
        user_text: str,
        idempotency_key: str,
        source: str,
        model: str,
        attempts_used: int,
        latency_ms: int,
        revision_plan: dict[str, Any] | None,
        requirement_record: dict[str, Any] | None,
        tradeoff_notice: str,
        request_id: str = "",
        complete_idempotency_scope: str | None = None,
    ) -> dict[str, Any]:
        """Commit an authorized execute_revision turn, creating a pending change_proposals row."""
        now_iso = self._clock.utc_now()
        user_turn_id = uuid.uuid4().hex
        assistant_turn_id = uuid.uuid4().hex
        proposal_id = uuid.uuid4().hex
        result_payload: dict[str, Any] = {
            "proposalId": proposal_id,
            "assistantTurnId": assistant_turn_id,
        }

        with self._db.write_tx() as conn:
            cur_session, cur_ver = require_current_version_tx(
                conn, session_id, base_version_id, clock=self._clock
            )
            diff = describe_diff(cur_ver.rows, candidate_rows)
            verified_diff = format_verified_diff_summary(diff, cur_session.language)
            assistant_text = tradeoff_notice or verified_diff or summary
            guidance = _proposal_review_guidance(cur_session.language)
            preserved_binding = {
                **source_binding,
                "sourceOfferTurnId": source_turn_id,
                "status": "active",
            }
            _mark_turn_binding_status_tx(
                conn,
                session_id=session_id,
                turn_id=source_turn_id,
                status="stale",
            )
            insert_turn_tx(
                conn,
                turn_id=user_turn_id,
                session_id=session_id,
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
                session_id=session_id,
                request_id=idempotency_key,
                role="assistant",
                content=assistant_text,
                language=cur_session.language,
                version_id=cur_ver.version_id,
                created_at=now_iso,
                guidance=guidance,
                proposal_binding=preserved_binding,
                model=model,
                attempts_used=attempts_used,
                latency_ms=latency_ms,
            )
            conn.execute(
                "UPDATE change_proposals SET status = 'superseded', decided_at = ? "
                "WHERE session_id = ? AND status = 'pending'",
                (now_iso, session_id),
            )
            payload = {
                "proposedRows": candidate_rows,
                "summary": summary,
                "diff": diff,
                "validation": solver_dict,
                "requirementRecord": requirement_record,
                "revisionPlan": revision_plan,
                "tradeoffNotice": tradeoff_notice,
                "source": source,
            }
            conn.execute(
                "INSERT INTO change_proposals("
                "proposal_id, session_id, base_version_id, assistant_turn_id, status, "
                "proposal_json, created_at"
                ") VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (
                    proposal_id,
                    session_id,
                    cur_ver.version_id,
                    assistant_turn_id,
                    dump_json(payload),
                    now_iso,
                ),
            )
            is_deterministic = model == "deterministic"
            append_audit_event_tx(
                conn,
                session_id,
                "reply_delivery_outcome",
                {
                    "messageKey": idempotency_key,
                    "requestId": request_id or idempotency_key,
                    "task": source,
                    "baseVersionId": cur_ver.version_id,
                    "proposalId": proposal_id,
                    "modelGenerationSucceeded": not is_deterministic,
                    "deterministicReceipt": is_deterministic,
                    "reliableBodyDelivered": True,
                    "fullQualityPassed": True,
                    "verifiedProposal": True,
                    "proposalRequested": True,
                    "attemptsUsed": attempts_used,
                    "latencyMs": latency_ms,
                },
                now_iso,
            )
            if complete_idempotency_scope:
                OperationalRepository.complete_key_tx(
                    conn,
                    session_id,
                    complete_idempotency_scope,
                    idempotency_key,
                    response=result_payload,
                )
        if complete_idempotency_scope:
            return {**result_payload, "_idempotencyCompleted": True}
        return result_payload
