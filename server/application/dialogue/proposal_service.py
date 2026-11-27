"""Proposal acceptance/rejection and Policy-2 binary discovery choice/cancel service."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from server.application.sessions import (
    CoCreationAppSettings,
    RoomSyncWriter,
    SessionReadModelService,
    append_audit_event_tx,
    build_entity_bindings,
    build_stage_sync_event,
    emit_room_cocreation_event_tx,
    next_version_number_tx,
    request_digest,
    solve_stage_async,
)
from server.application.sessions.stage.stage_ops import supersede_active_stage_artifacts_tx
from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.grid import map_fingerprint
from server.core.llm_client import LLMError
from server.domain.errors import ConflictError, IdempotencyConflictError, NotFoundError
from server.domain.memory import (
    add_confirmed_decision,
    add_rejected_decision,
    set_active_disagreement,
)
from server.domain.sessions import describe_diff, summarize_stage_changes
from server.i18n.messages import format_verified_diff_summary
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.repositories._mappers import row_to_proposal
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ._shared import (
    ACTIVE_CHALLENGE_STATUSES,
    check_completed_idempotency,
    classify_failure,
    record_message_failure_audit_tx,
    require_active_session,
    require_browser_session,
    require_current_version_tx,
    stale_before_iso,
)
from .handlers.binary_discovery import BinaryDiscoveryHandler


class ProposalService:
    """Manages proposal accept/reject (solving before write_tx) and Policy-2 binary actions."""

    def __init__(
        self,
        db: SQLiteDatabase,
        sessions_repo: SessionsRepository,
        operational_repo: OperationalRepository,
        read_model: SessionReadModelService,
        binary_handler: BinaryDiscoveryHandler,
        *,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
    ) -> None:
        self._db = db
        self._sessions_repo = sessions_repo
        self._operational_repo = operational_repo
        self._read_model = read_model
        self._binary_handler = binary_handler
        self._room_sync = room_sync
        self._clock = clock
        self._settings = settings or CoCreationAppSettings()

    async def decide_proposal(
        self,
        session_id: str,
        proposal_id: str,
        access_token: str | None,
        *,
        decision: Literal["accept", "reject"],
        base_version_id: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Accept or reject a pending proposal; runs solver OUTSIDE write_tx before CAS commit."""
        require_browser_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
        )
        clean_reason = str(reason or "").strip()[:2000]
        sig = request_digest(
            {
                "proposalId": proposal_id,
                "decision": decision,
                "base": base_version_id,
                "reason": clean_reason,
            }
        )
        if check_completed_idempotency(
            self._operational_repo, session_id, "decide_proposal", idempotency_key, sig
        ):
            return await self._read_model.get_session_payload(session_id)
        session = require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        claim = self._operational_repo.claim_key(
            session_id,
            "decide_proposal",
            idempotency_key,
            request_hash=sig,
            created_at=self._clock.utc_now(),
            stale_before_iso=stale_before_iso(self._clock, 150.0),
        )
        if claim.outcome == "conflict":
            raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
        if claim.outcome == "completed":
            return await self._read_model.get_session_payload(session_id)
        if claim.outcome == "in_progress":
            raise ConflictError("PROPOSAL_DECISION_IN_PROGRESS", "error.version_conflict")

        committed_in_tx = False
        try:
            if session.current_version_id != base_version_id:
                raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
            read_model = self._sessions_repo.load_session_read_model(session_id)
            if read_model is None:
                raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
            proposal = next(
                (p for p in read_model.proposals if p.proposal_id == proposal_id), None
            )
            if proposal is None:
                raise NotFoundError("PROPOSAL_NOT_FOUND", "error.version_conflict")
            if proposal.status != "pending" or proposal.base_version_id != base_version_id:
                raise ConflictError("STALE_PROPOSAL", "error.version_conflict")
            if decision == "accept":
                cur_ver_rm = next(
                    (v for v in read_model.versions if v.version_id == base_version_id), None
                )
                active_dis = (
                    (cur_ver_rm.design_context or {}).get("activeDisagreement")
                    if cur_ver_rm is not None
                    else None
                )
                has_active_dis = (
                    isinstance(active_dis, Mapping) and active_dis.get("status") == "active"
                )
                has_open_challenge = any(
                    ch.base_version_id == base_version_id
                    and ch.status in ACTIVE_CHALLENGE_STATUSES
                    for ch in read_model.challenges
                )
                if has_active_dis or has_open_challenge:
                    raise ConflictError("DISAGREEMENT_ACTIVE", "error.version_conflict")

            solved = (
                await solve_stage_async(
                    proposal.proposal.get("proposedRows") or (), clock=self._clock
                )
                if decision == "accept"
                else None
            )
            now_iso = self._clock.utc_now()
            new_vid: str | None = None

            with self._db.write_tx() as conn:
                cur_session, base_ver = require_current_version_tx(
                    conn, session_id, base_version_id, clock=self._clock
                )
                p_row = conn.execute(
                    "SELECT * FROM change_proposals WHERE session_id = ? AND proposal_id = ?",
                    (session_id, proposal_id),
                ).fetchone()
                if p_row is None:
                    raise NotFoundError("PROPOSAL_NOT_FOUND", "error.version_conflict")
                cur_prop = row_to_proposal(p_row)
                if cur_prop.status != "pending":
                    raise ConflictError("STALE_PROPOSAL", "error.version_conflict")
                if decision == "accept":
                    tx_dis = (base_ver.design_context or {}).get("activeDisagreement")
                    tx_has_dis = isinstance(tx_dis, Mapping) and tx_dis.get("status") == "active"
                    tx_open_chg = conn.execute(
                        "SELECT 1 FROM revision_challenges "
                        "WHERE session_id = ? AND base_version_id = ? "
                        "AND status NOT IN ('resolved', 'cancelled') LIMIT 1",
                        (session_id, base_version_id),
                    ).fetchone()
                    if tx_has_dis or tx_open_chg is not None:
                        raise ConflictError("DISAGREEMENT_ACTIVE", "error.version_conflict")

                summary = str(cur_prop.proposal.get("summary") or "AI proposal")
                decision_reason = clean_reason or str(
                    cur_prop.proposal.get("tradeoffNotice") or summary
                )
                if decision == "accept":
                    assert solved is not None
                    new_vid = uuid.uuid4().hex
                    new_num = next_version_number_tx(conn, session_id)
                    diff = describe_diff(base_ver.rows, solved.rows)
                    change_summary = summarize_stage_changes(
                        base_ver.rows, solved.rows
                    ).to_wire_dict()
                    verified_diff = format_verified_diff_summary(diff, cur_session.language)
                    bindings = build_entity_bindings(
                        solved.rows,
                        parent_bindings=base_ver.entity_bindings,
                        source="ai_proposal",
                    )
                    next_ctx = set_active_disagreement(
                        add_confirmed_decision(
                            base_ver.design_context,
                            summary,
                            decision_reason,
                            new_vid,
                            cur_prop.assistant_turn_id,
                            proposal_id=proposal_id,
                        ),
                        None,
                        new_vid,
                        cur_prop.assistant_turn_id,
                    )
                    conn.execute(
                        "INSERT INTO level_versions("
                        "version_id, session_id, version_number, source, summary, rows_json, "
                        "diff_json, design_context_json, solver_json, entity_bindings_json, "
                        "parent_version_id, idempotency_key, created_at"
                        ") VALUES (?, ?, ?, 'ai_proposal', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_vid,
                            session_id,
                            new_num,
                            summary,
                            dump_json(list(solved.rows)),
                            dump_json(diff),
                            dump_json(next_ctx.to_wire_dict()),
                            dump_json(solved.as_dict()),
                            dump_json(bindings),
                            base_ver.version_id,
                            f"proposal:{proposal_id}",
                            now_iso,
                        ),
                    )
                    cas = conn.execute(
                        "UPDATE design_sessions SET current_version_id = ?, updated_at = ? "
                        "WHERE session_id = ? AND current_version_id = ?",
                        (new_vid, now_iso, session_id, base_version_id),
                    )
                    if cas.rowcount == 0:
                        raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
                    conn.execute(
                        "UPDATE change_proposals "
                        "SET status = 'accepted', candidate_version_id = ?, decided_at = ?, "
                        "decision_json = ? "
                        "WHERE proposal_id = ? AND status = 'pending'",
                        (
                            new_vid,
                            now_iso,
                            dump_json(
                                {
                                    "decision": "accept",
                                    "versionId": new_vid,
                                    **({"reason": clean_reason} if clean_reason else {}),
                                }
                            ),
                            proposal_id,
                        ),
                    )
                    supersede_active_stage_artifacts_tx(conn, session_id, now_iso)
                    append_audit_event_tx(
                        conn,
                        session_id,
                        "revision_execution_outcome",
                        {
                            "versionId": new_vid,
                            "proposalId": proposal_id,
                            "sourceProposalTurnId": cur_prop.assistant_turn_id,
                            "summary": summary,
                            "verifiedDiff": verified_diff,
                            "changeSummary": change_summary,
                            "diff": diff,
                            "validation": solved.as_dict(),
                            "beforeFingerprint": map_fingerprint(base_ver.rows),
                            "afterFingerprint": map_fingerprint(solved.rows),
                            "status": "verified",
                        },
                        now_iso,
                    )
                    append_audit_event_tx(
                        conn,
                        session_id,
                        "proposal_accepted",
                        {"proposalId": proposal_id, "versionId": new_vid},
                        now_iso,
                    )
                    append_audit_event_tx(
                        conn,
                        session_id,
                        "intent_evidence_recorded",
                        {
                            "evidenceId": f"ie_{uuid.uuid4().hex}",
                            "kind": "proposal_accept",
                            "proposalId": proposal_id,
                            "versionId": new_vid,
                            "observation": summary[:500],
                        },
                        now_iso,
                    )
                    stage_ev = build_stage_sync_event(
                        session_id=session_id,
                        player_slot=cur_session.player_slot or 1,
                        version_id=new_vid,
                        version_number=new_num,
                        source="ai_proposal",
                        rows=solved.rows,
                        diff=diff,
                        initial_draft_method=cur_session.mode,
                        event_type="stage",
                    )
                    emit_room_cocreation_event_tx(
                        conn,
                        self._room_sync,
                        match_id=cur_session.match_id,
                        player_slot=cur_session.player_slot,
                        is_demo=cur_session.is_demo,
                        session_id=session_id,
                        event=stage_ev,
                        now_iso=now_iso,
                    )
                else:
                    reject_reason = clean_reason or "Rejected by designer"
                    next_ctx = add_rejected_decision(
                        base_ver.design_context,
                        summary,
                        reject_reason,
                        base_ver.version_id,
                        cur_prop.assistant_turn_id,
                        proposal_id=proposal_id,
                    )
                    conn.execute(
                        "UPDATE level_versions SET design_context_json = ? "
                        "WHERE session_id = ? AND version_id = ?",
                        (dump_json(next_ctx.to_wire_dict()), session_id, base_ver.version_id),
                    )
                    conn.execute(
                        "UPDATE change_proposals "
                        "SET status = 'rejected', decided_at = ?, decision_json = ? "
                        "WHERE proposal_id = ? AND status = 'pending'",
                        (
                            now_iso,
                            dump_json(
                                {
                                    "decision": "reject",
                                    **({"reason": clean_reason} if clean_reason else {}),
                                }
                            ),
                            proposal_id,
                        ),
                    )
                    append_audit_event_tx(
                        conn,
                        session_id,
                        "proposal_rejected",
                        {"proposalId": proposal_id, "baseVersionId": base_ver.version_id},
                        now_iso,
                    )

                self._operational_repo.complete_key_tx(
                    conn,
                    session_id,
                    "decide_proposal",
                    idempotency_key,
                    response={"proposalId": proposal_id, "versionId": new_vid},
                )
            committed_in_tx = True
        except asyncio.CancelledError:
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "decide_proposal", idempotency_key, status="failed"
                )
            raise
        except Exception:
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "decide_proposal", idempotency_key, status="failed"
                )
            raise
        return await self._read_model.get_session_payload(session_id)

    async def choose_binary_choice(
        self,
        session_id: str,
        access_token: str | None,
        *,
        topic_id: str,
        question_id: str,
        choice_id: str,
        idempotency_key: str,
        base_version_id: str | None = None,
        display_language: str | None = None,
        request_id: str = "",
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Select a Policy-2 A/B binary discovery choice with DB idempotency."""
        require_browser_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
        )
        sig = request_digest(
            {
                "topicId": topic_id,
                "questionId": question_id,
                "choiceId": choice_id,
                "baseVersionId": base_version_id,
                "displayLanguage": display_language,
            }
        )
        if check_completed_idempotency(
            self._operational_repo, session_id, "binary_choice", idempotency_key, sig
        ):
            return await self._read_model.get_session_payload(session_id)
        session = require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        claim = self._operational_repo.claim_key(
            session_id,
            "binary_choice",
            idempotency_key,
            request_hash=sig,
            created_at=self._clock.utc_now(),
            stale_before_iso=stale_before_iso(self._clock, 150.0),
        )
        if claim.outcome == "conflict":
            raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
        if claim.outcome == "completed":
            return await self._read_model.get_session_payload(session_id)
        if claim.outcome == "in_progress":
            raise ConflictError("BINARY_CHOICE_IN_PROGRESS", "error.version_conflict")

        committed_in_tx = False
        try:
            read_model = self._sessions_repo.load_session_read_model(session_id)
            if read_model is None:
                raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
            outcome = await self._binary_handler.choose_choice(
                read_model=read_model,
                topic_id=topic_id,
                question_id=question_id,
                choice_id=choice_id,
                idempotency_key=idempotency_key,
                request_id=request_id or idempotency_key,
                base_version_id=base_version_id,
                display_language=display_language,
                deadline=deadline,
                complete_idempotency_scope="binary_choice",
            )
            committed_in_tx = bool(outcome.pop("_idempotencyCompleted", False))
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "binary_choice", idempotency_key, response=outcome
                )
        except asyncio.CancelledError:
            if not committed_in_tx:
                self._operational_repo.complete_key(
                    session_id, "binary_choice", idempotency_key, status="failed"
                )
            raise
        except Exception as exc:
            code, retryable, details = classify_failure(exc)
            with self._db.write_tx() as conn:
                record_message_failure_audit_tx(
                    conn,
                    session_id=session_id,
                    base_version_id=base_version_id or (session.current_version_id or ""),
                    message_key=idempotency_key,
                    request_id=request_id or idempotency_key,
                    code=code,
                    retryable=retryable,
                    details=details,
                    now_iso=self._clock.utc_now(),
                    emit_reply_failure=isinstance(exc, (LLMError, TimeoutError)),
                )
                if not committed_in_tx:
                    self._operational_repo.complete_key_tx(
                        conn, session_id, "binary_choice", idempotency_key, status="failed"
                    )
            raise
        return await self._read_model.get_session_payload(session_id)

    async def cancel_binary_topic(
        self,
        session_id: str,
        access_token: str | None,
        *,
        topic_id: str,
        idempotency_key: str,
        base_version_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel an active Policy-2 binary discovery topic."""
        require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        read_model = self._sessions_repo.load_session_read_model(session_id)
        if read_model is None:
            raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
        await self._binary_handler.cancel_topic(
            read_model=read_model,
            topic_id=topic_id,
            idempotency_key=idempotency_key,
            base_version_id=base_version_id,
        )
        return await self._read_model.get_session_payload(session_id)


__all__ = ["ProposalService"]
