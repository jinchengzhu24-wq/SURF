"""Component recovery retry service backed by OperationalRepository recovery tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from server.application.sessions import (
    CoCreationAppSettings,
    RoomSyncWriter,
    SessionReadModelService,
    append_audit_event_tx,
    assess_version_impl,
    build_disagreement_audit_summary,
)
from server.core.clock import SYSTEM_CLOCK, Clock, parse_iso, utc_now_iso
from server.core.llm_client import LLMValidationError, call_structured
from server.core.settings import redact_secrets
from server.domain.errors import ConflictError, NotFoundError
from server.domain.memory import ensure_design_context, set_active_disagreement
from server.llm.pipelines.chat import ChatPipeline
from server.llm.pipelines.understanding import UnderstandingPipeline
from server.llm.prompts.understanding import build_human_edit_engagement_recovery_messages
from server.llm.schemas import HumanEditEngagementRecoveryPayload
from server.persistence.database import SQLiteDatabase, dump_json, load_json
from server.persistence.repositories._mappers import (
    row_to_recovery_task,
    row_to_session,
)
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ._shared import require_active_session, require_current_version_tx

MAX_RECOVERY_ATTEMPTS = 2
DEFAULT_LEASE_SECONDS = 15
TOTAL_RECOVERY_CAP_SECONDS = 10.0
MODEL_ATTEMPT_TIMEOUT_SECONDS = 8.0


class RecoveryService:
    """Claims and retries recoverable co-creation UI/LLM components safely outside write_tx."""

    def __init__(
        self,
        db: SQLiteDatabase,
        sessions_repo: SessionsRepository,
        operational_repo: OperationalRepository,
        read_model: SessionReadModelService,
        *,
        chat_pipeline: ChatPipeline | None = None,
        understanding_pipeline: UnderstandingPipeline | None = None,
        room_sync: RoomSyncWriter | None = None,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        extra_secrets: Sequence[str] = (),
    ) -> None:
        self._db = db
        self._sessions_repo = sessions_repo
        self._operational_repo = operational_repo
        self._read_model = read_model
        self._chat_pipeline = chat_pipeline
        self._understanding_pipeline = understanding_pipeline
        self._room_sync = room_sync
        self._clock = clock
        self._settings = settings or CoCreationAppSettings()
        self._lease_seconds = max(5, int(lease_seconds))
        self._extra_secrets = tuple(s for s in extra_secrets if isinstance(s, str) and s.strip())

    async def retry_recovery(
        self,
        session_id: str,
        recovery_id: str,
        access_token: str | None,
        *,
        idempotency_key: str,
        base_version_id: str | None = None,
        request_id: str = "",
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Claim a recovery task atomically, run retry OUTSIDE write_tx, and persist outcome."""
        session = require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        now_dt = self._clock.utc_datetime()
        now_iso = utc_now_iso(now_dt)
        lease_until = utc_now_iso(now_dt + timedelta(seconds=self._lease_seconds))
        already_completed = False

        # Atomic CAS + lease check inside write_tx so concurrent claims cannot race.
        with self._db.write_tx() as conn:
            s_row = conn.execute(
                "SELECT * FROM design_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if s_row is None:
                raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
            cur_session = row_to_session(s_row)
            if cur_session.status != "active":
                raise ConflictError("SESSION_LOCKED", "error.session_locked")

            t_row = conn.execute(
                "SELECT * FROM component_recovery_tasks WHERE session_id = ? AND recovery_id = ?",
                (session_id, recovery_id),
            ).fetchone()
            if t_row is None:
                raise NotFoundError("RECOVERY_TASK_NOT_FOUND", "error.version_conflict")
            task = row_to_recovery_task(t_row)
            state = dict(task.state)
            task_base_vid = str(state.get("baseVersionId") or task.target_id)
            if base_version_id is not None and (
                cur_session.current_version_id != base_version_id
                or task_base_vid != base_version_id
            ):
                raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
            if cur_session.current_version_id != task_base_vid:
                raise ConflictError("VERSION_CONFLICT", "error.version_conflict")

            status = str(state.get("status") or "pending")
            if status == "completed":
                already_completed = True
            else:
                if status == "claimed" and state.get("leaseUntil"):
                    if parse_iso(str(state["leaseUntil"])) > now_dt:
                        raise ConflictError("RECOVERY_IN_PROGRESS", "error.version_conflict")

                max_attempts = min(
                    int(state.get("maxAttempts") or MAX_RECOVERY_ATTEMPTS), MAX_RECOVERY_ATTEMPTS
                )
                attempts_used = int(state.get("attemptsUsed") or 0)
                if attempts_used >= max_attempts:
                    raise ConflictError("RECOVERY_ATTEMPTS_EXHAUSTED", "error.version_conflict")

                claimed_state = {
                    **state,
                    "status": "claimed",
                    "maxAttempts": max_attempts,
                    "attemptsUsed": attempts_used + 1,
                    "leaseUntil": lease_until,
                    "lastAttemptKey": idempotency_key,
                }
                conn.execute(
                    "UPDATE component_recovery_tasks SET state_json = ?, updated_at = ? "
                    "WHERE session_id = ? AND recovery_id = ?",
                    (dump_json(claimed_state), now_iso, session_id, recovery_id),
                )

        if already_completed:
            return await self._read_model.get_session_payload(session_id)

        started = self._clock.monotonic()
        bounded_deadline = (
            min(float(deadline), started + TOTAL_RECOVERY_CAP_SECONDS)
            if deadline is not None
            else (started + TOTAL_RECOVERY_CAP_SECONDS)
        )
        component = str(state.get("component") or task.target_kind)
        attempts_now = int(claimed_state["attemptsUsed"])

        try:
            if component == "stage_opening":
                await assess_version_impl(
                    db=self._db,
                    sessions_repo=self._sessions_repo,
                    chat_pipeline=self._chat_pipeline,
                    understanding_pipeline=self._understanding_pipeline,
                    operational_repo=self._operational_repo,
                    room_sync=self._room_sync,
                    clock=self._clock,
                    session=session,
                    version_id=task_base_vid,
                    idempotency_key=f"recovery:{recovery_id}:{idempotency_key}",
                    request_id=request_id or idempotency_key,
                    deadline=bounded_deadline,
                )
            elif component == "human_edit_engagement":
                await self._recover_human_edit_engagement(
                    session_id=session_id,
                    task_base_vid=task_base_vid,
                    state=claimed_state,
                    request_id=request_id or idempotency_key,
                    deadline=bounded_deadline,
                )
            else:
                raise ConflictError("UNSUPPORTED_RECOVERY_COMPONENT", "error.configuration")
        except asyncio.CancelledError:
            cancel_iso = self._clock.utc_now()
            cancelled_state = {
                **state,
                "status": "retry_pending",
                "maxAttempts": max_attempts,
                "attemptsUsed": attempts_used,
                "leaseUntil": None,
            }
            with self._db.write_tx() as conn:
                conn.execute(
                    "UPDATE component_recovery_tasks SET state_json = ?, updated_at = ? "
                    "WHERE session_id = ? AND recovery_id = ?",
                    (dump_json(cancelled_state), cancel_iso, session_id, recovery_id),
                )
            raise
        except Exception as exc:
            fail_iso = self._clock.utc_now()
            failed_status = "failed" if attempts_now >= max_attempts else "retry_pending"
            failed_state = {
                **claimed_state,
                "status": failed_status,
                "leaseUntil": None,
                "lastError": redact_secrets(str(exc), self._extra_secrets)[:400],
            }
            with self._db.write_tx() as conn:
                conn.execute(
                    "UPDATE component_recovery_tasks SET state_json = ?, updated_at = ? "
                    "WHERE session_id = ? AND recovery_id = ?",
                    (dump_json(failed_state), fail_iso, session_id, recovery_id),
                )
                append_audit_event_tx(
                    conn,
                    session_id,
                    "component_recovery",
                    {
                        "recoveryId": recovery_id,
                        "component": component,
                        "status": failed_status,
                        "attemptsUsed": attempts_now,
                    },
                    fail_iso,
                )
            raise

        done_iso = self._clock.utc_now()
        done_state = {
            **claimed_state,
            "status": "completed",
            "leaseUntil": None,
            "lastError": None,
        }
        with self._db.write_tx() as conn:
            conn.execute(
                "UPDATE component_recovery_tasks SET state_json = ?, updated_at = ? "
                "WHERE session_id = ? AND recovery_id = ?",
                (dump_json(done_state), done_iso, session_id, recovery_id),
            )
            append_audit_event_tx(
                conn,
                session_id,
                "component_recovery",
                {
                    "recoveryId": recovery_id,
                    "component": component,
                    "status": "completed",
                    "attemptsUsed": attempts_now,
                },
                done_iso,
            )
        return await self._read_model.get_session_payload(session_id)

    async def _recover_human_edit_engagement(
        self,
        *,
        session_id: str,
        task_base_vid: str,
        state: Mapping[str, Any],
        request_id: str,
        deadline: float,
    ) -> None:
        transport = (
            self._understanding_pipeline.transport
            if self._understanding_pipeline is not None
            else (self._chat_pipeline.transport if self._chat_pipeline is not None else None)
        )
        model = (
            self._understanding_pipeline.model
            if self._understanding_pipeline is not None
            else (self._chat_pipeline.model if self._chat_pipeline is not None else "")
        )
        extra_body = (
            self._understanding_pipeline.extra_body
            if self._understanding_pipeline is not None
            else (self._chat_pipeline.extra_body if self._chat_pipeline is not None else None)
        )
        if transport is None or not model:
            raise ConflictError("LLM_UNAVAILABLE", "error.configuration")

        read_model = self._sessions_repo.load_session_read_model(session_id)
        if read_model is None:
            raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")
        ver = next((v for v in read_model.versions if v.version_id == task_base_vid), None)
        if ver is None:
            raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
        active_dis = (ver.design_context or {}).get("activeDisagreement")
        if not (
            isinstance(active_dis, Mapping)
            and active_dis.get("status") == "active"
            and active_dis.get("subject") == "human_edit"
        ):
            return

        user_msg = str(state.get("userMessage") or "").strip()
        if not user_msg and state.get("sourceUserTurnId"):
            src_turn = next(
                (t for t in read_model.turns if t.turn_id == state["sourceUserTurnId"]), None
            )
            user_msg = src_turn.content if src_turn is not None else ""
        if not user_msg:
            raise ConflictError("INVALID_RECOVERY_EVIDENCE", "error.version_conflict")

        prev_user_msgs = [
            t.content
            for t in read_model.turns
            if t.version_id == task_base_vid and t.role == "user"
        ][-4:]
        messages = build_human_edit_engagement_recovery_messages(
            latest_user_message=user_msg,
            previous_user_messages=prev_user_msgs,
            active_disagreement=active_dis,
        )

        def _validate_engagement(
            parsed: HumanEditEngagementRecoveryPayload,
        ) -> HumanEditEngagementRecoveryPayload:
            if parsed.decision == "engage" and (
                not parsed.evidence_span or parsed.evidence_span not in user_msg
            ):
                raise LLMValidationError(
                    "ENGAGEMENT_EVIDENCE_MISMATCH",
                    f"evidenceSpan {parsed.evidence_span!r} must appear verbatim in "
                    "latestUserMessage when decision='engage'.",
                )
            return parsed

        res = await call_structured(
            transport,
            task="human_edit_engagement_recovery",
            model=model,
            messages=messages,
            response_model=HumanEditEngagementRecoveryPayload,
            validator=_validate_engagement,
            deadline=deadline,
            max_attempts=2,
            temperature=0.0,
            max_tokens=1024,
            attempt_timeout_seconds=MODEL_ATTEMPT_TIMEOUT_SECONDS,
            extra_body=extra_body,
            clock=self._clock,
            request_id=request_id,
            extra_secrets=self._extra_secrets,
        )
        if res.parsed.decision != "engage":
            return

        now_iso = self._clock.utc_now()
        delivered_turn_id = str(state.get("deliveredAssistantTurnId") or "")
        expected_dis_id = str(state.get("disagreementId") or "")
        with self._db.write_tx() as conn:
            _, cur_ver = require_current_version_tx(
                conn, session_id, task_base_vid, clock=self._clock
            )
            cur_dis = (cur_ver.design_context or {}).get("activeDisagreement")
            if not (
                isinstance(cur_dis, Mapping)
                and cur_dis.get("status") == "active"
                and cur_dis.get("subject") == "human_edit"
                and (
                    not expected_dis_id
                    or cur_dis.get("disagreementId") == expected_dis_id
                )
            ):
                raise ConflictError("VERSION_CONFLICT", "error.version_conflict")
            cleared_ctx = set_active_disagreement(
                ensure_design_context(cur_ver.design_context),
                None,
                task_base_vid,
                delivered_turn_id or None,
            )
            conn.execute(
                "UPDATE level_versions SET design_context_json = ? "
                "WHERE session_id = ? AND version_id = ?",
                (dump_json(cleared_ctx.to_wire_dict()), session_id, task_base_vid),
            )
            ack_payload = {
                **dict(cur_dis),
                "status": "acknowledged",
                "displayCard": False,
                "resolution": None,
            }
            if delivered_turn_id:
                t_row = conn.execute(
                    "SELECT guidance_json FROM conversation_turns "
                    "WHERE session_id = ? AND turn_id = ?",
                    (session_id, delivered_turn_id),
                ).fetchone()
                if t_row is not None:
                    g = load_json(t_row["guidance_json"], {})
                    if isinstance(g, dict):
                        g["disagreement"] = ack_payload
                        conn.execute(
                            "UPDATE conversation_turns SET guidance_json = ? "
                            "WHERE session_id = ? AND turn_id = ?",
                            (dump_json(g), session_id, delivered_turn_id),
                        )
            append_audit_event_tx(
                conn,
                session_id,
                "disagreement_acknowledged",
                {
                    "versionId": task_base_vid,
                    "turnId": delivered_turn_id or None,
                    "subject": "human_edit",
                    "disagreement": build_disagreement_audit_summary(ack_payload),
                },
                now_iso,
            )


__all__ = ["RecoveryService"]
