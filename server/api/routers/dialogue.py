"""Thin async routes for co-creation chat, Policy-2 binary discovery, and feedback."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request

from server.api.deps import (
    ACTION_MAP,
    CHALLENGE_CHOICE_MAP,
    AccessCookie,
    ContainerDep,
    ensure_session_shape,
    get_llm_deadline,
    get_request_id,
    resolve_challenge_proposal_id,
    validate_fields,
    validate_identifier,
)
from server.api.schemas import (
    BinaryCancelRequest,
    BinaryChoiceRequest,
    ComponentRecoveryRequest,
    IntentFeedbackRequest,
    MessageRequest,
    QuestionFeedbackRequest,
)

router = APIRouter()


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    payload: MessageRequest,
    request: Request,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    action = ACTION_MAP[payload.action]
    assistant_turn_id = payload.assistantTurnId or payload.sourceTurnId
    proposal_id = payload.proposalId
    if action == "challenge_revision" and not proposal_id:
        proposal_id = await resolve_challenge_proposal_id(
            container,
            session_id,
            proposal_id=proposal_id,
            assistant_turn_id=assistant_turn_id,
            base_version_id=payload.baseVersionId,
        )
    choice = CHALLENGE_CHOICE_MAP[payload.challengeChoice] if payload.challengeChoice else None
    result = await container.message_orchestrator.send_message(
        session_id,
        access_cookie,
        base_version_id=payload.baseVersionId,
        idempotency_key=payload.idempotencyKey,
        content=payload.content,
        action=action,
        request_proposal=payload.requestProposal,
        assistant_turn_id=assistant_turn_id,
        proposal_id=proposal_id,
        challenge_id=payload.challengeId,
        challenge_choice=choice,
        exit_challenge=payload.exitChallenge,
        request_id=get_request_id(request),
        deadline=get_llm_deadline(request),
    )
    return ensure_session_shape(result)


@router.post("/sessions/{session_id}/proposal-discovery/choices")
async def choose_binary_proposal(
    session_id: str,
    payload: BinaryChoiceRequest,
    request: Request,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_fields(payload, "idempotencyKey", "topicId", "questionId", "choiceId", "baseVersionId")
    result = await container.proposal_service.choose_binary_choice(
        session_id,
        access_cookie,
        topic_id=payload.topicId,
        question_id=payload.questionId,
        choice_id=payload.choiceId,
        base_version_id=payload.baseVersionId,
        display_language=payload.displayLanguage,
        idempotency_key=payload.idempotencyKey,
        request_id=get_request_id(request),
        deadline=get_llm_deadline(request),
    )
    return ensure_session_shape(result)


@router.post("/sessions/{session_id}/proposal-discovery/cancel")
async def cancel_binary_proposal(
    session_id: str,
    payload: BinaryCancelRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_fields(payload, "idempotencyKey", "topicId", "baseVersionId")
    result = await container.proposal_service.cancel_binary_topic(
        session_id,
        access_cookie,
        topic_id=payload.topicId,
        base_version_id=payload.baseVersionId,
        idempotency_key=payload.idempotencyKey,
    )
    return ensure_session_shape(result)


@router.post("/sessions/{session_id}/questions/{question_id}/feedback")
async def submit_question_feedback(
    session_id: str,
    question_id: str,
    payload: QuestionFeedbackRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    session_payload = await container.intent_service.submit_question_feedback(
        session_id,
        question_id,
        access_cookie,
        action=payload.action,
        base_version_id=payload.baseVersionId,
        idempotency_key=payload.idempotencyKey,
    )
    return {"outcome": "applied", "session": ensure_session_shape(session_payload)}


@router.post("/sessions/{session_id}/intent-hypotheses/{hypothesis_id}/feedback")
async def submit_intent_feedback(
    session_id: str,
    hypothesis_id: str,
    payload: IntentFeedbackRequest,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    resolved_action: Literal["confirm", "reject", "revise"]
    if payload.action == "keep":
        resolved_action = "confirm" if payload.selectedHypothesisId == hypothesis_id else "reject"
    else:
        resolved_action = payload.action
    session_payload = await container.intent_service.submit_intent_feedback(
        session_id,
        hypothesis_id,
        access_cookie,
        action=resolved_action,
        base_version_id=payload.baseVersionId,
        idempotency_key=payload.idempotencyKey,
        candidate_text=payload.candidateText,
        source_turn_id=payload.sourceTurnId,
    )
    outcome = "rejected" if resolved_action == "reject" else "applied"
    return {"outcome": outcome, "session": ensure_session_shape(session_payload)}


@router.post("/sessions/{session_id}/component-recoveries/{recovery_id}/retry")
async def retry_component_recovery(
    session_id: str,
    recovery_id: str,
    payload: ComponentRecoveryRequest,
    request: Request,
    container: ContainerDep,
    access_cookie: AccessCookie = None,
) -> dict[str, Any]:
    validate_identifier(recovery_id, "recoveryId")
    validate_identifier(payload.idempotencyKey, "idempotencyKey")
    full_deadline = get_llm_deadline(request)
    started_at = getattr(request.state, "llm_started_at", None)
    if full_deadline is not None and isinstance(started_at, (int, float)):
        recovery_deadline: float | None = min(full_deadline, float(started_at) + 10.0)
    elif isinstance(started_at, (int, float)):
        recovery_deadline = float(started_at) + 10.0
    else:
        recovery_deadline = full_deadline
    result = await container.recovery_service.retry_recovery(
        session_id,
        recovery_id,
        access_cookie,
        base_version_id=payload.baseVersionId,
        idempotency_key=payload.idempotencyKey,
        request_id=get_request_id(request),
        deadline=recovery_deadline,
    )
    return ensure_session_shape(result)
