"""Conversation, proposal discovery, challenge, recovery, and decision routes."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta, timezone
from functools import wraps
from threading import Lock

import httpx
from fastapi import APIRouter, Cookie, Depends, Request, Response

from cocreation.clock import iso_time, parse_time
from cocreation.domain.design_context import (
    add_confirmed_decision,
    add_rejected_decision,
    apply_question_feedback,
    resolve_intent_hypothesis,
    sanitize_user_design_text,
)
from cocreation.domain.level_validation import summarize_verified_diff
from cocreation.domain.proposal_diagnostics import (
    CURRENT_PROPOSAL_DIAGNOSTIC,
    ProposalDiagnostic,
    proposal_phase,
    trace_proposal,
)
from cocreation.domain.proposal_understanding import validate_engagement
from cocreation.domain.revision_workflow import SemanticConstraintError, validate_semantic_constraints
from cocreation.llm.facade import KIMI_MODEL, LLMExecutionResult, LLMServiceError, _dialogue_background_prompt
from cocreation.persistence.repository import (
    get_current_version,
    get_session,
    get_version,
    load_design_context,
    load_entity_bindings,
    load_json,
    map_fingerprint,
    save_design_context,
)
from cocreation.reliability import component_recovery
from cocreation.reliability.reply_reliability import CURRENT_REPLY_TRACE, ReplyTrace, failure_reason

from . import binary_routes
from .config_models import (
    ApiError,
    BinaryCancelRequest,
    BinaryChoiceRequest,
    ChallengeReviewRetryRequest,
    ComponentRecoveryRequest,
    IntentFeedbackRequest,
    MAX_MESSAGE_LENGTH,
    MessageRequest,
    ProposalDecisionRequest,
    QuestionFeedbackRequest,
    SESSION_COOKIE_NAME,
    _solve_changed_proposal_or_api_error,
    _validate_identifier,
    clean_optional,
    require_active_session,
    require_browser_session,
    require_current_base,
)
from .context_and_discovery import (
    _pending_intent_evidence,
    build_llm_context,
    record_intent_evidence,
    record_intent_hypothesis,
)
from .message_send_workflow import run_send_message_pipeline
from .online_sync_and_dashboard import (
    synchronize_dashboard_intent_node,
    synchronize_dashboard_player_challenge,
    synchronize_dashboard_proposal_node,
    synchronize_manual_edit_review_nodes,
    synchronize_version_with_online_match,
)
from .proposal_and_challenge import _validate_message_action_payload
from .services import CoCreationServices, get_services
from .session_persistence_helpers import _insert_decision, _insert_version

router = APIRouter()

_FALLBACK_MESSAGE_LOCKS: dict[tuple[str, str], dict] = {}
_FALLBACK_MESSAGE_LOCKS_GUARD = Lock()


@router.post('/api/sessions/{session_id}/component-recoveries/{recovery_id}/retry')
def retry_component_recovery(
    session_id: str,
    recovery_id: str,
    payload: ComponentRecoveryRequest,
    request: Request,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(recovery_id, 'recoveryId')
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    deadline = request.state.llm_started_at + 10.0
    source_error = None
    with services.connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        try:
            task = component_recovery.claim(
                database,
                session,
                recovery_id,
                payload.baseVersionId,
                payload.idempotencyKey,
                clock=services.clock,
                record_event_fn=services.record_event,
            )
        except ValueError as error:
            raise ApiError(409, str(error), 'This state recovery source or key is invalid.') from error
        if task is None:
            return services.serialize_session(database, session_id)
        try:
            source = component_recovery.replay_sources(database, session_id, task)
            context = build_llm_context(database, session_id, get_current_version(database, session), services=services)
            snapshot = context['stageContext']['stageSnapshot']
            original_background = _dialogue_background_prompt(
                {
                    'stageSnapshot': snapshot,
                    'dialogueEvidence': {
                        'turns': [
                            {
                                'id': task.get('sourceAssistantTurnId'),
                                'role': 'assistant',
                                'content': source.pop('boundAssistantMessage'),
                            }
                        ]
                    },
                }
            )
            subsequent = source.pop('subsequentTurns')
            source.update(
                {
                    'boundQuestionBackground': original_background,
                    'subsequentUserMessages': [item for item in subsequent if item['role'] == 'user'],
                    'discussionBackground': _dialogue_background_prompt(context['stageContext']),
                    'activeDisagreement': context['stageContext'].get('activeDisagreement'),
                    'currentStageSnapshot': snapshot,
                }
            )
        except ValueError as error:
            source_error = str(error)
            component_recovery.record(
                database,
                session_id,
                task,
                'expired',
                failureCode=source_error,
                clock=services.clock,
                record_event_fn=services.record_event,
            )
    if source_error:
        with services.connect() as database:
            return services.serialize_session(database, session_id)
    decision, failure_code = (None, None)
    try:
        decision = services.llm.recover_human_edit_engagement(source, request.state.request_id, deadline)
        decision = validate_engagement(decision, source['originalUserMessage'])
        if services.monotonic() >= deadline:
            failure_code = 'RECOVERY_TIMEOUT'
    except (LLMServiceError, ValueError, TypeError, KeyError, IndexError, TimeoutError, httpx.HTTPError) as error:
        if isinstance(error, TimeoutError):
            failure_code = 'RECOVERY_TIMEOUT'
        else:
            failure_code = getattr(error, 'code', None) or 'STATE_UNVERIFIED'
    recovery_status = None
    with services.connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        recovery_status = component_recovery.finish(
            database,
            session,
            task,
            decision,
            failure_code,
            clock=services.clock,
            record_event_fn=services.record_event,
        )
        session_payload = services.serialize_session(database, session_id)
    if recovery_status == 'applied':
        synchronize_manual_edit_review_nodes(session_id, payload.baseVersionId, services=services)
    return session_payload


def _intent_feedback_event(database, session_id, idempotency_key):
    rows = database.execute(
        "\n        SELECT event_type, payload_json FROM audit_events\n"
        "        WHERE session_id = ?\n"
        "          AND event_type IN (\n"
        "            'intent_hypothesis_feedback_applied',\n"
        "            'intent_hypothesis_feedback_pending'\n"
        "          )\n"
        "        ORDER BY id DESC\n        ",
        (session_id,),
    ).fetchall()
    for row in rows:
        event = load_json(row['payload_json']) or {}
        if event.get('idempotencyKey') == idempotency_key:
            return event
    return None


def _intent_feedback_signature(payload, hypothesis_id, candidate):
    return {
        'hypothesisId': hypothesis_id,
        'action': payload.action,
        'candidateText': candidate or None,
        'selectedHypothesisId': payload.selectedHypothesisId or None,
        'sourceTurnId': payload.sourceTurnId,
        'baseVersionId': payload.baseVersionId,
    }


def _intent_feedback_signature_matches(stored, current):
    """Keep pre-conflict-choice idempotency records replayable."""
    stored = dict(stored or {})
    stored.setdefault('selectedHypothesisId', None)
    return stored == current


def _question_feedback_event(database, session_id, idempotency_key):
    rows = database.execute(
        "\n        SELECT payload_json FROM audit_events\n"
        "        WHERE session_id = ? AND event_type = 'question_feedback_applied'\n"
        "        ORDER BY id DESC\n        ",
        (session_id,),
    ).fetchall()
    for row in rows:
        event = load_json(row['payload_json']) or {}
        if event.get('idempotencyKey') == idempotency_key:
            return event
    return None


@router.post('/api/sessions/{session_id}/questions/{question_id}/feedback')
def submit_question_feedback(
    session_id: str,
    question_id: str,
    payload: QuestionFeedbackRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    """Deterministically ignore or restore one visible current-Stage question."""
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    _validate_identifier(question_id, 'questionId')
    signature = {'questionId': question_id, 'action': payload.action, 'baseVersionId': payload.baseVersionId}
    with services.connect() as auth_database:
        require_browser_session(auth_database, session_id, access_cookie, services=services)
    with message_request_lock(session_id, payload.idempotencyKey, scope='question_feedback', request_signature=signature, services=services):
        with services.connect(immediate=True) as database:
            session = require_browser_session(database, session_id, access_cookie, services=services)
            prior = _question_feedback_event(database, session_id, payload.idempotencyKey)
            if prior is not None:
                if prior.get('request') != signature:
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The question feedback key was reused for a different request.')
                return {'outcome': prior['outcome'], 'session': services.serialize_session(database, session_id)}
            session = require_active_session(database, session_id, access_cookie, services=services)
            require_current_base(session, payload.baseVersionId)
            context = load_design_context(database, session_id, payload.baseVersionId)
            target = next((item for item in context.get('openQuestions', []) if item.get('id') == question_id), None)
            expected_status = 'open' if payload.action == 'ignore' else 'ignored'
            if target is None or target.get('sourceKind') not in {'visible_output', 'legacy'} or target.get('status') != expected_status:
                raise ApiError(409, 'STALE_QUESTION', 'This question is no longer available for that action.')
            changed_at = services.utc_now()
            context, changed = apply_question_feedback(context, question_id, payload.action, payload.baseVersionId, changed_at)
            if not changed:
                raise ApiError(409, 'STALE_QUESTION', 'This question is no longer available for that action.')
            save_design_context(database, payload.baseVersionId, context)
            services.record_event(
                database,
                session_id,
                'question_feedback_applied',
                {
                    'request': signature,
                    'idempotencyKey': payload.idempotencyKey,
                    'questionId': question_id,
                    'versionId': payload.baseVersionId,
                    'action': payload.action,
                    'outcome': 'applied',
                },
                changed_at,
            )
            return {'outcome': 'applied', 'session': services.serialize_session(database, session_id)}


def _require_actionable_intent_card(database, session_id, version_id, hypothesis_id, source_turn_id):
    latest = None
    for row in database.execute(
        "\n        SELECT id, guidance_json FROM conversation_turns\n"
        "        WHERE session_id = ? AND version_id = ? AND role = 'assistant'\n"
        "        ORDER BY sequence_number\n        ",
        (session_id, version_id),
    ).fetchall():
        guidance = load_json(row['guidance_json']) or {}
        if guidance.get('_intentHypothesisId'):
            latest = (row, guidance)
    if latest is None or latest[0]['id'] != source_turn_id or latest[1].get('_intentHypothesisId') != hypothesis_id:
        raise ApiError(409, 'INVALID_CARD_SOURCE', 'This intent card is no longer the current actionable card.')
    return latest


def _sync_intent_dashboard_after(handler):
    """Keep a visible intent node current after every successful card decision."""

    @wraps(handler)
    def wrapped(session_id, hypothesis_id, *args, **kwargs):
        result = handler(session_id, hypothesis_id, *args, **kwargs)
        services = kwargs.get('services')
        if services is not None:
            synchronize_dashboard_intent_node(session_id, hypothesis_id, services=services)
        return result

    return wrapped


@router.post('/api/sessions/{session_id}/intent-hypotheses/{hypothesis_id}/feedback')
@_sync_intent_dashboard_after
def submit_intent_feedback(
    session_id: str,
    hypothesis_id: str,
    payload: IntentFeedbackRequest,
    request: Request,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    """Resolve one displayed, current-Stage tentative intent card."""
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    _validate_identifier(hypothesis_id, 'hypothesisId')
    candidate = sanitize_user_design_text(payload.candidateText)
    selected_hypothesis_id = str(payload.selectedHypothesisId or '').strip()
    if selected_hypothesis_id:
        _validate_identifier(selected_hypothesis_id, 'selectedHypothesisId')
    if payload.action == 'revise':
        if not candidate or len(candidate) < 4 or len(candidate) > 1200:
            raise ApiError(400, 'INVALID_INTENT_FEEDBACK', 'The revised inclination must contain 4 to 1200 meaningful characters.')
    elif candidate:
        raise ApiError(400, 'INVALID_INTENT_FEEDBACK', 'candidateText is accepted only when revising an inclination.')
    if payload.action == 'keep' and (not selected_hypothesis_id):
        raise ApiError(400, 'INVALID_INTENT_FEEDBACK', 'selectedHypothesisId is required when retaining a conflicting inclination.')
    if payload.action != 'keep' and selected_hypothesis_id:
        raise ApiError(400, 'INVALID_INTENT_FEEDBACK', 'selectedHypothesisId is accepted only for a conflict choice.')
    signature = _intent_feedback_signature(payload, hypothesis_id, candidate)
    with services.connect() as auth_database:
        require_browser_session(auth_database, session_id, access_cookie, services=services)
    with message_request_lock(session_id, payload.idempotencyKey, scope='intent_feedback', request_signature=signature, services=services):
        with services.connect(immediate=True) as database:
            session = require_browser_session(database, session_id, access_cookie, services=services)
            prior = _intent_feedback_event(database, session_id, payload.idempotencyKey)
            if prior is not None:
                if not _intent_feedback_signature_matches(prior.get('request'), signature):
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The feedback key was already used for a different request.')
                return {'outcome': prior['outcome'], 'session': services.serialize_session(database, session_id)}
            if database.execute(
                'SELECT 1 FROM conversation_turns WHERE session_id = ? AND request_id = ? LIMIT 1',
                (session_id, payload.idempotencyKey),
            ).fetchone() is not None:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The feedback key was already used for another message.')
            session = require_active_session(database, session_id, access_cookie, services=services)
            require_current_base(session, payload.baseVersionId)
            _, source_guidance = _require_actionable_intent_card(
                database,
                session_id,
                payload.baseVersionId,
                hypothesis_id,
                payload.sourceTurnId,
            )
            context = load_design_context(database, session_id, payload.baseVersionId)
            target = next((item for item in context.get('intentHypotheses', []) if item.get('id') == hypothesis_id), None)
            if target is None or target.get('status') != 'tentative' or (not target.get('displayed')):
                raise ApiError(409, 'STALE_INTENT_CARD', 'This intent card has already been handled or has expired.')
            review_state = source_guidance.get('_intentReviewState') or {}
            if review_state.get('interactionMode') == 'conflict_choice':
                new_id = hypothesis_id
                existing_id = str(review_state.get('existingHypothesisId') or '').strip()
                if payload.action != 'keep' or selected_hypothesis_id not in {new_id, existing_id}:
                    raise ApiError(409, 'STALE_INTENT_CARD', 'This conflict must be resolved by retaining one displayed inclination.')
                existing = next((item for item in context.get('intentHypotheses', []) if item.get('id') == existing_id), None)
                if existing is None or existing.get('status') != 'confirmed':
                    raise ApiError(409, 'STALE_INTENT_CARD', 'The confirmed inclination in this conflict is no longer current.')
                choice = 'new' if selected_hypothesis_id == new_id else 'existing'
                revised_statement = str(review_state.get('candidateText') or '').strip() if choice == 'new' else ''
                chosen_new_statement = revised_statement or target.get('statement')
                evidence_id = record_intent_evidence(
                    database,
                    session_id,
                    payload.baseVersionId,
                    'intent_conflict_choice',
                    chosen_new_statement if choice == 'new' else existing.get('statement'),
                    turn_id=payload.sourceTurnId,
                    details={
                        'action': 'keep',
                        'choice': choice,
                        'newHypothesisId': new_id,
                        'existingHypothesisId': existing_id,
                        'explanation': str(review_state.get('reviewExplanation') or '')[:1200],
                    },
                    services=services,
                )
                if choice == 'new':
                    context, _ = resolve_intent_hypothesis(
                        context,
                        new_id,
                        'revise' if revised_statement else 'confirm',
                        candidate_text=revised_statement or None,
                        supersedes_ids=[existing_id],
                        evidence_id=evidence_id,
                        stage_id=payload.baseVersionId,
                        turn_id=payload.sourceTurnId,
                        display_statement=chosen_new_statement,
                        display_language=session['language'],
                    )
                    outcome = 'kept_new'
                else:
                    context, _ = resolve_intent_hypothesis(
                        context,
                        new_id,
                        'reject',
                        evidence_id=evidence_id,
                        stage_id=payload.baseVersionId,
                        turn_id=payload.sourceTurnId,
                    )
                    outcome = 'kept_existing'
                retained_old = next((item for item in context.get('intentHypotheses', []) if item.get('id') == existing_id), None)
                if retained_old is not None:
                    evidence_field = 'contradictingEvidenceIds' if choice == 'new' else 'supportingEvidenceIds'
                    retained_old[evidence_field] = list(dict.fromkeys(retained_old.get(evidence_field, []) + [evidence_id]))[:32]
                    retained_old['lastUpdatedStageId'] = payload.baseVersionId
                save_design_context(database, payload.baseVersionId, context)
                services.record_event(
                    database,
                    session_id,
                    'intent_hypothesis_feedback_applied',
                    {
                        'request': signature,
                        'idempotencyKey': payload.idempotencyKey,
                        'hypothesisId': new_id,
                        'versionId': payload.baseVersionId,
                        'outcome': outcome,
                        'evidenceId': evidence_id,
                        'selectedHypothesisId': selected_hypothesis_id,
                        'newHypothesisId': new_id,
                        'existingHypothesisId': existing_id,
                        'supersedesHypothesisIds': [existing_id] if choice == 'new' else [],
                        'explanation': str(review_state.get('reviewExplanation') or '')[:1200],
                    },
                    services.utc_now(),
                )
                return {'outcome': outcome, 'session': services.serialize_session(database, session_id)}
            if payload.action == 'keep':
                raise ApiError(409, 'STALE_INTENT_CARD', 'This intent card is not an active conflict choice.')
            if review_state.get('interactionMode') == 'adjust_only' and payload.action != 'revise':
                raise ApiError(409, 'STALE_INTENT_CARD', 'This intent needs a revised statement before it can be saved.')
            statement = candidate if payload.action == 'revise' else target['statement']
            active = [
                {'hypothesisId': item['id'], 'statement': item['statement']}
                for item in context.get('intentHypotheses', [])
                if item.get('status') == 'confirmed' and item.get('id') != hypothesis_id
            ]
            if payload.action == 'reject':
                evidence_id = record_intent_evidence(
                    database,
                    session_id,
                    payload.baseVersionId,
                    'intent_feedback',
                    statement,
                    turn_id=payload.sourceTurnId,
                    details={'action': payload.action},
                    services=services,
                )
                context, _ = resolve_intent_hypothesis(
                    context,
                    hypothesis_id,
                    payload.action,
                    evidence_id=evidence_id,
                    stage_id=payload.baseVersionId,
                    turn_id=payload.sourceTurnId,
                )
                save_design_context(database, payload.baseVersionId, context)
                outcome = 'rejected'
                services.record_event(
                    database,
                    session_id,
                    'intent_hypothesis_feedback_applied',
                    {
                        'request': signature,
                        'idempotencyKey': payload.idempotencyKey,
                        'hypothesisId': hypothesis_id,
                        'versionId': payload.baseVersionId,
                        'outcome': outcome,
                        'evidenceId': evidence_id,
                        'supersedesHypothesisIds': [],
                    },
                    services.utc_now(),
                )
                return {'outcome': outcome, 'session': services.serialize_session(database, session_id)}
            evidence_summary = [
                str(item.get('observation') or '')[:500]
                for item in _pending_intent_evidence(
                    database,
                    session_id,
                    get_version(database, session_id, payload.baseVersionId),
                    context,
                    limit=8,
                )
            ]
        if payload.action == 'confirm' and (not active):
            review = {'verdict': 'compatible', 'explanation': None, 'supersedesHypothesisIds': []}
        else:
            review = services.llm.review_intent_feedback(
                statement,
                active,
                evidence_summary,
                session['language'],
                request.state.request_id,
                _deadline=request.state.llm_deadline,
            )
        display_rewrite = {'detailedText': None, 'summaryText': statement, 'model': 'deterministic-fallback'}
        if review['verdict'] == 'compatible':
            try:
                display_rewrite = services.llm.rewrite_intent_progress(
                    'inclination',
                    statement,
                    evidence_summary,
                    session['language'],
                    request.state.request_id,
                    _deadline=min(services.monotonic() + 10.0, request.state.llm_deadline - 20.0),
                )
            except (LLMServiceError, TypeError, ValueError, KeyError) as exception:
                display_rewrite['failureCode'] = getattr(exception, 'code', None) or type(exception).__name__
                display_rewrite['failureReason'] = str(exception)[:500]
        display_statement = display_rewrite['summaryText']
        with services.connect(immediate=True) as database:
            session = require_browser_session(database, session_id, access_cookie, services=services)
            prior = _intent_feedback_event(database, session_id, payload.idempotencyKey)
            if prior is not None:
                if not _intent_feedback_signature_matches(prior.get('request'), signature):
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The feedback key was reused.')
                return {'outcome': prior['outcome'], 'session': services.serialize_session(database, session_id)}
            if database.execute(
                'SELECT 1 FROM conversation_turns WHERE session_id = ? AND request_id = ? LIMIT 1',
                (session_id, payload.idempotencyKey),
            ).fetchone() is not None:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The feedback key was already used for another message.')
            session = require_active_session(database, session_id, access_cookie, services=services)
            require_current_base(session, payload.baseVersionId)
            _require_actionable_intent_card(database, session_id, payload.baseVersionId, hypothesis_id, payload.sourceTurnId)
            context = load_design_context(database, session_id, payload.baseVersionId)
            target = next((item for item in context.get('intentHypotheses', []) if item.get('id') == hypothesis_id), None)
            if target is None or target.get('status') != 'tentative':
                raise ApiError(409, 'STALE_INTENT_CARD', 'This intent card is stale.')
            if review['verdict'] == 'compatible':
                evidence_id = record_intent_evidence(
                    database,
                    session_id,
                    payload.baseVersionId,
                    'intent_feedback',
                    statement,
                    turn_id=payload.sourceTurnId,
                    details={'action': payload.action},
                    services=services,
                )
                context, _ = resolve_intent_hypothesis(
                    context,
                    hypothesis_id,
                    payload.action,
                    candidate_text=candidate,
                    supersedes_ids=[],
                    evidence_id=evidence_id,
                    stage_id=payload.baseVersionId,
                    turn_id=payload.sourceTurnId,
                    display_statement=display_statement,
                    display_language=session['language'],
                )
                save_design_context(database, payload.baseVersionId, context)
                services.record_event(
                    database,
                    session_id,
                    'intent_hypothesis_feedback_applied',
                    {
                        'request': signature,
                        'idempotencyKey': payload.idempotencyKey,
                        'hypothesisId': hypothesis_id,
                        'versionId': payload.baseVersionId,
                        'outcome': 'applied',
                        'reviewedStatement': display_statement or statement,
                        'evidenceId': evidence_id,
                        'supersedesHypothesisIds': [],
                    },
                    services.utc_now(),
                )
                services.record_event(
                    database,
                    session_id,
                    'intent_progress_rewrite',
                    {
                        'artifactType': 'inclination',
                        'artifactId': hypothesis_id,
                        'versionId': payload.baseVersionId,
                        'language': session['language'],
                        **display_rewrite,
                    },
                    services.utc_now(),
                )
                return {'outcome': 'applied', 'session': services.serialize_session(database, session_id)}
            conflict_existing_id = None
            if review['verdict'] == 'conflict':
                conflicting_ids = set(review.get('conflictingHypothesisIds') or [])
                candidate_existing_id = next(
                    (item['hypothesisId'] for item in reversed(active) if item['hypothesisId'] in conflicting_ids),
                    None,
                )
                if candidate_existing_id is None:
                    raise ApiError(502, 'MODEL_RESPONSE_INVALID', 'Kimi did not bind the conflict to confirmed memory.', retryable=True)
                current_confirmed_ids = {
                    item['id']
                    for item in context.get('intentHypotheses', [])
                    if item.get('status') == 'confirmed' and item.get('id') != hypothesis_id
                }
                if candidate_existing_id not in current_confirmed_ids:
                    raise ApiError(409, 'INTENT_CONTEXT_CHANGED', 'Confirmed design inclinations changed while reviewing this conflict.')
                conflict_existing_id = candidate_existing_id
            user_text = candidate if payload.action == 'revise' else '符合我的想法' if session['language'] == 'zh-CN' else 'This matches my idea.'
            services.insert_turn(database, session, 'user', user_text, payload.baseVersionId, payload.idempotencyKey, None)
            explanation = review['explanation']
            execution = LLMExecutionResult(
                assistant_message=explanation,
                attempts_used=1,
                request_id=request.state.request_id,
                model=KIMI_MODEL,
                guidance={
                    'move': 'clarify_intent',
                    'intentHypothesis': statement,
                    'intentConfidence': 'low',
                    'followUpQuestion': None,
                    'proposalOffer': None,
                    'disagreement': None,
                    'uiCues': [],
                    '_intentHypothesisId': hypothesis_id,
                    '_intentReviewState': {
                        'interactionMode': 'conflict_choice' if review['verdict'] == 'conflict' else 'adjust_only',
                        'reviewOutcome': review['verdict'],
                        'reviewExplanation': explanation,
                        'existingHypothesisId': conflict_existing_id,
                        'newHypothesisId': hypothesis_id,
                        'candidateText': statement,
                    },
                },
            )
            assistant_turn_id = services.insert_turn(
                database,
                session,
                'assistant',
                explanation,
                payload.baseVersionId,
                payload.idempotencyKey,
                execution,
            )
            services.record_event(
                database,
                session_id,
                'intent_hypothesis_feedback_pending',
                {
                    'request': signature,
                    'idempotencyKey': payload.idempotencyKey,
                    'hypothesisId': hypothesis_id,
                    'versionId': payload.baseVersionId,
                    'outcome': review['verdict'],
                    'candidateText': statement,
                    'assistantTurnId': assistant_turn_id,
                    'explanation': explanation,
                    'existingHypothesisId': conflict_existing_id,
                },
                services.utc_now(),
            )
            return {'outcome': review['verdict'], 'session': services.serialize_session(database, session_id)}


def _normalize_challenge_reason_text(value):
    return re.sub('[\\W_]+', '', str(value or '').casefold(), flags=re.UNICODE)


def _route_message_to_active_challenge(
    session_id,
    payload,
    content,
    access_cookie,
    *,
    services: CoCreationServices,
):
    """Resolve challenge routing before any ordinary-chat side effect."""
    if payload.exitChallenge or payload.action not in {'none', 'continue_challenge'}:
        return payload
    with services.connect(immediate=True) as database:
        require_browser_session(database, session_id, access_cookie, services=services)
        answered = database.execute(
            "SELECT guidance_json FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'assistant'",
            (session_id, payload.idempotencyKey),
        ).fetchone()
        if answered is not None:
            prior_state = (load_json(answered['guidance_json']) or {}).get('challengeState') or {}
            if prior_state.get('challengeId'):
                if payload.challengeId and payload.challengeId != prior_state['challengeId']:
                    raise ApiError(409, 'CHALLENGE_STALE', 'The retry belongs to another challenge.')
                return payload.model_copy(update={'action': 'continue_challenge', 'challengeId': prior_state['challengeId']})
            return payload
        session = require_active_session(database, session_id, access_cookie, services=services)
        require_current_base(session, payload.baseVersionId)
        challenge = database.execute(
            "\n            SELECT * FROM revision_challenges\n"
            "            WHERE session_id = ? AND base_version_id = ?\n"
            "              AND status NOT IN ('resolved', 'superseded')\n"
            "            ORDER BY updated_at DESC LIMIT 1\n            ",
            (session_id, payload.baseVersionId),
        ).fetchone()
        if challenge is None:
            if payload.action == 'continue_challenge':
                active_disagreement = (load_design_context(database, session_id, payload.baseVersionId) or {}).get('activeDisagreement') or {}
                if (
                    active_disagreement.get('status') == 'active'
                    and active_disagreement.get('subject') != 'ai_revision_challenge'
                    and payload.challengeId
                    and active_disagreement.get('challengeId') == payload.challengeId
                ):
                    return payload
                raise ApiError(409, 'CHALLENGE_STALE', 'The selected challenge is no longer active.')
            return payload
        if payload.challengeId and payload.challengeId != challenge['challenge_id']:
            raise ApiError(409, 'CHALLENGE_STALE', 'The selected challenge is no longer active.')
        if payload.action == 'none' and payload.requestProposal:
            raise ApiError(409, 'DISAGREEMENT_ACTIVE', 'Resolve the active design disagreement before requesting a new proposal.')
        cutoff = services.utc_datetime() - timedelta(seconds=125)
        now_str = services.utc_now()
        stale_reviews = database.execute(
            "\n            SELECT review_id, updated_at FROM challenge_reason_reviews\n"
            "            WHERE session_id = ? AND challenge_id = ? AND status = 'reviewing'\n            ",
            (session_id, challenge['challenge_id']),
        ).fetchall()
        expired_review = False
        for stale_row in stale_reviews:
            parsed_updated = parse_time(stale_row['updated_at'])
            if parsed_updated.tzinfo is None:
                parsed_updated = parsed_updated.replace(tzinfo=timezone.utc)
            if parsed_updated <= cutoff:
                database.execute(
                    "\n                    UPDATE challenge_reason_reviews\n"
                    "                    SET status = 'review_pending', updated_at = ?\n"
                    "                    WHERE review_id = ? AND session_id = ? AND status = 'reviewing'\n                    ",
                    (now_str, stale_row['review_id'], session_id),
                )
                expired_review = True
        if expired_review and challenge['status'] == 'reviewing':
            database.execute(
                "\n                UPDATE revision_challenges\n"
                "                SET status = 'review_pending', updated_at = ?\n"
                "                WHERE challenge_id = ? AND session_id = ? AND status = 'reviewing'\n                ",
                (now_str, challenge['challenge_id'], session_id),
            )
        routed = payload.model_copy(update={'action': 'continue_challenge', 'challengeId': challenge['challenge_id']})
        review = database.execute(
            "\n            SELECT review.message_key, review.status, turn.content\n"
            "            FROM challenge_reason_reviews AS review\n"
            "            JOIN conversation_turns AS turn ON turn.id = review.source_user_turn_id\n"
            "            WHERE review.session_id = ? AND review.challenge_id = ?\n"
            "              AND review.status IN ('review_pending', 'reviewing')\n"
            "            ORDER BY review.updated_at DESC LIMIT 1\n            ",
            (session_id, challenge['challenge_id']),
        ).fetchone()
        if review is not None and _normalize_challenge_reason_text(review['content']) == _normalize_challenge_reason_text(content):
            routed = routed.model_copy(update={'idempotencyKey': review['message_key'], 'content': review['content']})
        elif review is not None and review['status'] == 'reviewing':
            raise ApiError(409, 'CHALLENGE_REVIEW_BUSY', 'This judgment is already being reviewed.')
        return routed


@router.post('/api/sessions/{session_id}/messages')
def send_message(
    session_id: str,
    payload: MessageRequest,
    request: Request,
    response: Response,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    content = payload.content.strip()
    if not content or len(content) > MAX_MESSAGE_LENGTH:
        raise ApiError(400, 'INVALID_MESSAGE', 'The message must contain 1 to 2000 characters.')
    with services.connect() as auth_database:
        require_browser_session(auth_database, session_id, access_cookie, services=services)
    payload = _route_message_to_active_challenge(session_id, payload, content, access_cookie, services=services)
    content = payload.content.strip()
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    _validate_message_action_payload(payload)
    message_signature = payload.model_dump()
    with message_request_lock(session_id, payload.idempotencyKey, scope='message', request_signature=message_signature, services=services):
        guard = None
        with services.connect(immediate=True) as database:
            guard_session = require_browser_session(database, session_id, access_cookie, services=services)
            active = load_design_context(database, session_id, payload.baseVersionId).get('activeDisagreement') or {}
            answered = database.execute(
                "SELECT 1 FROM conversation_turns WHERE session_id=? AND request_id=? AND role='assistant'",
                (session_id, payload.idempotencyKey),
            ).fetchone()
            if (
                not answered
                and guard_session['current_version_id'] == payload.baseVersionId
                and (active.get('subject') == 'human_edit')
                and (active.get('status') == 'active')
            ):
                guard = {
                    'guardId': uuid.uuid4().hex,
                    'versionId': payload.baseVersionId,
                    'messageKey': payload.idempotencyKey,
                    'status': 'started',
                    'leaseUntil': iso_time(services.utc_datetime() + timedelta(seconds=120)),
                }
                services.record_event(database, session_id, 'interaction_message_guard', guard, services.utc_now())
        delivered_payload = None
        request.state.interaction_guard_id = guard.get('guardId') if guard else None
        try:
            delivered_payload = _send_message_locked(
                session_id,
                payload,
                content,
                request,
                response,
                access_cookie,
                services=services,
            )
            return delivered_payload
        except (ApiError, LLMServiceError) as error:
            projection = _record_dashboard_failed_request(
                session_id,
                payload,
                content,
                access_cookie,
                error,
                services=services,
            )
            if projection is not None:
                try:
                    kind, identifier = projection
                    if kind == 'player_challenge':
                        synchronize_dashboard_player_challenge(session_id, identifier, services=services)
                    else:
                        synchronize_manual_edit_review_nodes(session_id, identifier, services=services)
                except ApiError:
                    pass
            raise
        finally:
            if guard:
                with services.connect(immediate=True) as database:
                    services.record_event(
                        database,
                        session_id,
                        'interaction_message_guard',
                        {**guard, 'status': 'finished'},
                        services.utc_now(),
                    )
                    if delivered_payload is not None:
                        delivered_payload['interactionState'] = component_recovery.interaction_state(
                            database,
                            get_session(database, session_id),
                            clock=services.clock,
                        )


@router.post('/api/sessions/{session_id}/proposal-discovery/choices')
def choose_binary_proposal(
    session_id: str,
    payload: BinaryChoiceRequest,
    request: Request,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    for field in ('idempotencyKey', 'topicId', 'questionId', 'choiceId', 'baseVersionId'):
        _validate_identifier(getattr(payload, field), field)
    with services.connect() as auth_database:
        require_browser_session(auth_database, session_id, access_cookie, services=services)
    with message_request_lock(session_id, payload.idempotencyKey, scope='binary_choice', request_signature=payload.model_dump(), services=services):
        return binary_routes.choose(services, session_id, payload, request, access_cookie)


@router.post('/api/sessions/{session_id}/proposal-discovery/cancel')
def cancel_binary_proposal(
    session_id: str,
    payload: BinaryCancelRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    for field in ('idempotencyKey', 'topicId', 'baseVersionId'):
        _validate_identifier(getattr(payload, field), field)
    with services.connect() as auth_database:
        require_browser_session(auth_database, session_id, access_cookie, services=services)
    with message_request_lock(session_id, payload.idempotencyKey, scope='binary_cancel', request_signature=payload.model_dump(), services=services):
        return binary_routes.cancel(services, session_id, payload, access_cookie)


@router.post('/api/sessions/{session_id}/challenge-reviews/{review_id}/retry')
def retry_challenge_review(
    session_id: str,
    review_id: str,
    payload: ChallengeReviewRetryRequest,
    request: Request,
    response: Response,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(review_id, 'reviewId')
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    with services.connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie, services=services)
        require_current_base(session, payload.baseVersionId)
    raise ApiError(410, 'CHALLENGE_REVIEW_RETRY_RETIRED', 'This dedicated retry endpoint has been retired. Retry the original chat message instead.')


def _record_dashboard_failed_request(
    session_id,
    payload,
    content,
    access_cookie,
    error,
    *,
    services: CoCreationServices,
):
    if not error.retryable:
        return None
    with services.connect(immediate=True) as database:
        try:
            require_browser_session(database, session_id, access_cookie, services=services)
        except ApiError:
            return None
        user = database.execute(
            "SELECT * FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'user'",
            (session_id, payload.idempotencyKey),
        ).fetchone()
        assistant = database.execute(
            "SELECT id FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'assistant'",
            (session_id, payload.idempotencyKey),
        ).fetchone()
        if user is None or assistant is not None or user['content'] != content or (user['version_id'] != payload.baseVersionId):
            return None
        event = {'messageKey': payload.idempotencyKey, 'code': error.code, 'versionId': payload.baseVersionId, 'userTurnId': user['id']}
        if payload.action == 'continue_challenge' and payload.challengeId:
            challenge = database.execute(
                'SELECT challenge_id FROM revision_challenges WHERE session_id = ? AND challenge_id = ? AND base_version_id = ?',
                (session_id, payload.challengeId, payload.baseVersionId),
            ).fetchone()
            if challenge is None:
                return None
            event['challengeId'] = payload.challengeId
            services.record_event(database, session_id, 'dashboard_challenge_request_failed', event, services.utc_now())
            return ('player_challenge', payload.challengeId)
        context = load_design_context(database, session_id, payload.baseVersionId) or {}
        if (context.get('activeDisagreement') or {}).get('subject') == 'human_edit':
            services.record_event(database, session_id, 'dashboard_human_edit_request_failed', event, services.utc_now())
            return ('llm_challenge', payload.baseVersionId)
    return None


def _send_message_locked(
    session_id,
    payload,
    content,
    request,
    response,
    access_cookie,
    *,
    services: CoCreationServices,
):
    trace = ReplyTrace(
        request.state.request_id,
        payload.idempotencyKey,
        payload.baseVersionId,
        services.llm.log_event,
        clock=services.clock,
    )
    token = CURRENT_REPLY_TRACE.set(trace)
    proposal_trace = ProposalDiagnostic(trace.identity, services.llm.log_event)
    proposal_token = CURRENT_PROPOSAL_DIAGNOSTIC.set(proposal_trace)
    trace.record('routing', 'started')
    try:
        result = _send_message_locked_impl(
            session_id,
            payload,
            content,
            request,
            response,
            access_cookie,
            services=services,
        )
        proposal_trace.outcome = 'passed'
        proposal_trace.final_failure = None
        trace.record('http_response', 'prepared')
        return result
    except (ApiError, LLMServiceError) as error:
        proposal_trace.outcome = 'failed'
        proposal_trace.error((getattr(error, 'details', {}) or {}).get('failureStage', 'request_preflight'), 'request', error)
        trace.record('request', 'failed', failure_reason(error))
        error.details = {
            **(getattr(error, 'details', {}) or {}),
            'replyPipeline': trace.diagnostic(),
            'proposalDiagnostic': proposal_trace.snapshot(),
        }
        raise
    except Exception as error:
        proposal_trace.outcome = 'failed'
        committing = any((step['stage'] == 'commit' and step['status'] == 'started' for step in trace.steps))
        proposal_trace.error('commit' if committing else 'request', 'commit' if committing else 'request', error)
        raise
    finally:
        try:
            with services.connect(immediate=True) as database:
                require_browser_session(database, session_id, access_cookie, services=services)
                observed = database.execute(
                    'SELECT 1 FROM conversation_turns WHERE session_id=? AND request_id=? LIMIT 1',
                    (session_id, payload.idempotencyKey),
                ).fetchone()
                if not observed:
                    observed = database.execute(
                        "SELECT 1 FROM audit_events WHERE session_id=? AND event_type='message_generation_failed' "
                        "AND json_extract(payload_json,'$.messageKey')=? LIMIT 1",
                        (session_id, payload.idempotencyKey),
                    ).fetchone()
                if observed:
                    services.record_event(
                        database,
                        session_id,
                        'reply_pipeline_diagnostic',
                        {**trace.diagnostic(), 'proposalDiagnostic': proposal_trace.snapshot()},
                        services.utc_now(),
                    )
        except (ApiError, sqlite3.Error):
            pass
        CURRENT_REPLY_TRACE.reset(token)
        CURRENT_PROPOSAL_DIAGNOSTIC.reset(proposal_token)


def _send_message_locked_impl(
    session_id,
    payload,
    content,
    request,
    response,
    access_cookie,
    *,
    services: CoCreationServices,
):
    return run_send_message_pipeline(
        session_id,
        payload,
        content,
        request,
        response,
        access_cookie,
        services=services,
    )


def _lock_fingerprint(request_signature):
    if request_signature is None:
        return None
    return hashlib.sha256(
        json.dumps(request_signature, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
    ).hexdigest()


@contextmanager
def message_request_lock(
    session_id,
    idempotency_key,
    *,
    scope='default',
    request_signature=None,
    services: CoCreationServices | None = None,
):
    locks = services._message_locks if services is not None else _FALLBACK_MESSAGE_LOCKS
    guard = services._message_locks_guard if services is not None else _FALLBACK_MESSAGE_LOCKS_GUARD
    key = (session_id, scope, idempotency_key)
    fingerprint = _lock_fingerprint(request_signature)
    with guard:
        entry = locks.get(key)
        if entry is None:
            entry = {'lock': Lock(), 'users': 0, 'error': None, 'fingerprint': fingerprint}
            locks[key] = entry
        is_leader = entry['users'] == 0
        entry['users'] += 1
    entry['lock'].acquire()
    try:
        if (
            not is_leader
            and entry['error'] is not None
            and (fingerprint is None or entry['fingerprint'] == fingerprint)
        ):
            raise entry['error']
        yield
    except Exception as error:
        if is_leader:
            entry['error'] = error
        raise
    finally:
        with guard:
            if is_leader and locks.get(key) is entry:
                locks.pop(key, None)
            entry['users'] -= 1
            if entry['users'] == 0 and locks.get(key) is entry:
                locks.pop(key, None)
        entry['lock'].release()


@router.post('/api/sessions/{session_id}/proposals/{proposal_id}/decision')
def decide_proposal(
    session_id: str,
    proposal_id: str,
    payload: ProposalDecisionRequest,
    request: Request,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    identity = {
        'requestId': request.state.request_id,
        'messageKey': payload.idempotencyKey,
        'baseVersionId': payload.baseVersionId,
        'proposalId': proposal_id,
        'task': 'proposal_decision',
    }
    diagnostic = ProposalDiagnostic(identity, services.llm.log_event)
    token = CURRENT_PROPOSAL_DIAGNOSTIC.set(diagnostic)
    try:
        result = _decide_proposal_impl(session_id, proposal_id, payload, request, access_cookie, services=services)
        diagnostic.outcome = 'passed'
        diagnostic.final_failure = None
        trace_proposal('acceptance_commit', 'commit', 'passed')
        return result
    except (ApiError, LLMServiceError) as error:
        diagnostic.outcome = 'failed'
        diagnostic.error('acceptance', 'proposal', error)
        error.details = {**(getattr(error, 'details', {}) or {}), 'proposalDiagnostic': diagnostic.snapshot()}
        raise
    finally:
        try:
            with services.connect(immediate=True) as database:
                require_browser_session(database, session_id, access_cookie, services=services)
                exists = database.execute('SELECT 1 FROM change_proposals WHERE session_id=? AND id=?', (session_id, proposal_id)).fetchone()
                if exists:
                    services.record_event(
                        database,
                        session_id,
                        'reply_pipeline_diagnostic',
                        {**identity, 'proposalDiagnostic': diagnostic.snapshot()},
                        services.utc_now(),
                    )
        except (ApiError, sqlite3.Error):
            pass
        CURRENT_PROPOSAL_DIAGNOSTIC.reset(token)


@proposal_phase('acceptance', 'proposal')
def _decide_proposal_impl(
    session_id,
    proposal_id,
    payload,
    request,
    access_cookie,
    *,
    services: CoCreationServices,
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    decision_rewrite = None
    decision_rewrite_language = 'zh-CN'
    with services.connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        existing_decision = database.execute(
            '\n            SELECT d.id, d.proposal_id, d.decision_type, d.version_id, p.base_version_id\n'
            '            FROM designer_decisions AS d\n'
            '            LEFT JOIN change_proposals AS p ON p.id = d.proposal_id\n'
            '            WHERE d.session_id = ? AND d.idempotency_key = ?\n            ',
            (session_id, payload.idempotencyKey),
        ).fetchone()
        if existing_decision is not None:
            if (
                existing_decision['proposal_id'] != proposal_id
                or existing_decision['decision_type'] != payload.decision
                or (existing_decision['base_version_id'] is not None and existing_decision['base_version_id'] != payload.baseVersionId)
            ):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'This idempotencyKey belongs to another proposal decision.')
            new_version_id = existing_decision['version_id']
            session_payload = services.serialize_session(database, session_id)
        else:
            session = require_active_session(database, session_id, access_cookie, services=services)
            require_current_base(session, payload.baseVersionId)
            proposal = database.execute(
                '\n                SELECT * FROM change_proposals WHERE id = ? AND session_id = ?\n                ',
                (proposal_id, session_id),
            ).fetchone()
            if proposal is None:
                raise ApiError(404, 'PROPOSAL_NOT_FOUND', 'The proposal was not found.')
            if proposal['status'] != 'pending':
                raise ApiError(409, 'PROPOSAL_ALREADY_DECIDED', 'The proposal was already decided.')
            if proposal['base_version_id'] != session['current_version_id']:
                raise ApiError(409, 'VERSION_CONFLICT', 'The proposal belongs to an older Stage.')
            proposal_turn = database.execute(
                'SELECT guidance_json, proposal_binding_json FROM conversation_turns WHERE id = ?',
                (proposal['assistant_turn_id'],),
            ).fetchone()
            proposal_guidance = load_json(proposal_turn['guidance_json']) if proposal_turn is not None else None
            proposal_binding = load_json(proposal_turn['proposal_binding_json']) if proposal_turn is not None and proposal_turn['proposal_binding_json'] else None
            now = services.utc_now()
            new_version_id = None
            verified_summary = None
            if payload.decision == 'accept':
                decision_rewrite_language = session['language']
                proposed_rows = load_json(proposal['proposed_rows_json'])
                current = get_current_version(database, session)
                current_rows = load_json(current['rows_json'])
                validation = _solve_changed_proposal_or_api_error(
                    current_rows,
                    proposed_rows,
                    deadline=request.state.llm_deadline,
                    services=services,
                )
                verified_summary = summarize_verified_diff(current_rows, validation.rows, session['language'])
                proposal_offer = proposal_guidance.get('proposalOffer') if isinstance(proposal_guidance, dict) else None
                proposal_brief = (
                    (proposal_offer.get('executionBrief') if isinstance(proposal_offer, dict) else None)
                    or (proposal_binding.get('executionBrief') if isinstance(proposal_binding, dict) else None)
                )
                revision_workflow = (
                    (proposal_offer.get('revisionWorkflow') if isinstance(proposal_offer, dict) and isinstance(proposal_offer.get('revisionWorkflow'), dict) else None)
                    or (proposal_binding.get('revisionWorkflow') if isinstance(proposal_binding, dict) and isinstance(proposal_binding.get('revisionWorkflow'), dict) else None)
                    or {}
                )
                source_offer_turn_id = proposal_binding.get('sourceOfferTurnId') if isinstance(proposal_binding, dict) else None
                if source_offer_turn_id and (not proposal_brief or not revision_workflow):
                    source_turn_row = database.execute(
                        'SELECT guidance_json, proposal_binding_json FROM conversation_turns WHERE id = ?',
                        (source_offer_turn_id,),
                    ).fetchone()
                    if source_turn_row is not None:
                        src_guidance = load_json(source_turn_row['guidance_json']) or {}
                        src_offer = src_guidance.get('proposalOffer') if isinstance(src_guidance, dict) else None
                        src_binding = load_json(source_turn_row['proposal_binding_json']) if source_turn_row['proposal_binding_json'] else {}
                        if not proposal_brief:
                            proposal_brief = (
                                (src_offer.get('executionBrief') if isinstance(src_offer, dict) else None)
                                or (src_binding.get('executionBrief') if isinstance(src_binding, dict) else None)
                            )
                        if not revision_workflow:
                            revision_workflow = (
                                (src_offer.get('revisionWorkflow') if isinstance(src_offer, dict) and isinstance(src_offer.get('revisionWorkflow'), dict) else None)
                                or (src_binding.get('revisionWorkflow') if isinstance(src_binding, dict) and isinstance(src_binding.get('revisionWorkflow'), dict) else None)
                                or {}
                            )
                actual_transitions = [
                    {'row': row_index + 1, 'column': column_index + 1, 'from': before, 'to': after}
                    for row_index, (before_row, after_row) in enumerate(zip(current_rows, validation.rows))
                    for column_index, (before, after) in enumerate(zip(before_row, after_row))
                    if before != after
                ]
                frozen_transitions = proposal_brief.get('requiredTransitions') or [] if isinstance(proposal_brief, dict) else []
                if frozen_transitions:
                    actual_signature = {(item['row'], item['column'], item['from'], item['to']) for item in actual_transitions}
                    frozen_signature = {(item.get('row'), item.get('column'), item.get('from'), item.get('to')) for item in frozen_transitions}
                    if actual_signature != frozen_signature:
                        raise ApiError(
                            409,
                            'EXECUTION_REPLAY_MISMATCH',
                            'The proposal no longer matches its frozen tile transitions.',
                            False,
                            {
                                'task': 'execution_replay',
                                'failureStage': 'postcondition',
                                'failureCode': 'FROZEN_DIFF_MISMATCH',
                                'retryable': False,
                                'safeReason': 'The actual map diff differs from the reviewed purple card.',
                            },
                        )
                try:
                    constraint_results = validate_semantic_constraints(current_rows, validation.rows, revision_workflow)
                    requirement_outcomes = services.llm.validate_frozen_requirements(
                        current_rows,
                        validation.rows,
                        revision_workflow,
                        load_json(current['validation_json']),
                        validation,
                        load_entity_bindings(database, session_id, current['id']),
                        deadline=request.state.llm_deadline,
                    )
                except SemanticConstraintError as exception:
                    raise ApiError(
                        422,
                        'SEMANTIC_POSTCONDITION_FAILED',
                        'The proposal no longer satisfies the authorized semantic contract.',
                        False,
                        {
                            'task': 'postcondition',
                            'failureStage': 'semantic_contract',
                            'failureCode': 'SEMANTIC_CONSTRAINT_FAILED',
                            'retryable': False,
                            'safeReason': 'One or more explicit design requirements were not satisfied.',
                            'constraintResults': exception.results,
                        },
                    ) from exception
                new_version_id = _insert_version(
                    database,
                    session,
                    validation,
                    'llm_accepted',
                    verified_summary,
                    'proposal:' + payload.idempotencyKey,
                    current,
                    entity_transitions=proposal_brief.get('requiredTransitions', []) if isinstance(proposal_brief, dict) else [],
                    services=services,
                )
                child_context = add_confirmed_decision(
                    load_design_context(database, session_id, new_version_id),
                    proposal['summary'],
                    clean_optional(payload.reason, 2000) or 'Designer accepted the validated map proposal.',
                    new_version_id,
                    proposal['assistant_turn_id'],
                    proposal_id,
                )
                for requirement in (revision_workflow.get('requirementRecord') or {}).get('requirements') or []:
                    if requirement['strength'] == 'preference':
                        continue
                    field, collection = ('constraint', 'designConstraints') if requirement['strength'] == 'invariant' else ('goal', 'userGoals')
                    if not any((item.get('id') == requirement['requirementId'] for item in child_context[collection])):
                        child_context[collection].append(
                            {
                                'id': requirement['requirementId'],
                                field: requirement['statement'],
                                'authority': 'explicit',
                                'status': 'active',
                                'sourceStageId': current['id'],
                                'sourceTurnId': requirement['sourceTurnId'],
                                'confidence': 1.0,
                            }
                        )
                save_design_context(database, new_version_id, child_context)
                execution_outcome = {
                    'revisionWorkflowId': revision_workflow.get('revisionWorkflowId'),
                    'sourceProposalTurnId': proposal['assistant_turn_id'],
                    'actualTransitions': actual_transitions,
                    'constraintResults': constraint_results,
                    'goalOutcomes': requirement_outcomes,
                    'softEvidence': [],
                    'validation': validation.as_dict(),
                    'beforeFingerprint': map_fingerprint(current_rows),
                    'afterFingerprint': map_fingerprint(validation.rows),
                    'status': 'verified',
                }
                services.record_event(database, session_id, 'revision_execution_outcome', {'versionId': new_version_id, **execution_outcome}, now)
                services.record_event(
                    database,
                    session_id,
                    'design_context_updated',
                    {
                        'versionId': new_version_id,
                        'sourceTurnId': proposal['assistant_turn_id'],
                        'kind': 'confirmed_decision',
                        'proposalId': proposal_id,
                    },
                    now,
                )
                decision_rewrite = {
                    'artifactId': proposal_id,
                    'versionId': new_version_id,
                    'canonical': proposal['summary'],
                    'evidence': [clean_optional(payload.reason, 2000) or '', verified_summary or ''],
                }
            database.execute(
                '\n                UPDATE change_proposals\n'
                '                SET status = ?, decided_at = ?, summary = COALESCE(?, summary)\n'
                '                WHERE id = ?\n                ',
                ('accepted' if payload.decision == 'accept' else 'rejected', now, verified_summary, proposal_id),
            )
            if payload.decision == 'accept':
                database.execute(
                    "\n                    UPDATE change_proposals SET status = 'superseded', decided_at = ?\n"
                    "                    WHERE session_id = ? AND id != ? AND status = 'pending'\n                    ",
                    (now, session_id, proposal_id),
                )
            _insert_decision(
                database,
                session_id,
                new_version_id,
                proposal_id,
                payload.decision,
                clean_optional(payload.reason, 2000) or '',
                payload.idempotencyKey,
                services=services,
            )
            record_intent_evidence(
                database,
                session_id,
                new_version_id or proposal['base_version_id'],
                'proposal_' + payload.decision,
                proposal['summary'],
                turn_id=proposal['assistant_turn_id'],
                proposal_id=proposal_id,
                details={
                    'decision': payload.decision,
                    'reason': clean_optional(payload.reason, 2000) or '',
                    'baseVersionId': proposal['base_version_id'],
                },
                created_at=now,
                services=services,
            )
            if payload.decision == 'reject':
                current_context = load_design_context(database, session_id, proposal['base_version_id'])
                current_context = add_rejected_decision(
                    current_context,
                    proposal['summary'],
                    clean_optional(payload.reason, 2000) or '',
                    proposal['base_version_id'],
                    proposal['assistant_turn_id'],
                    proposal_id,
                )
                save_design_context(database, proposal['base_version_id'], current_context)
                services.record_event(
                    database,
                    session_id,
                    'design_context_updated',
                    {
                        'versionId': proposal['base_version_id'],
                        'sourceTurnId': proposal['assistant_turn_id'],
                        'kind': 'rejected_decision',
                        'proposalId': proposal_id,
                        'hasReason': bool(clean_optional(payload.reason, 2000)),
                    },
                    now,
                )
                record_intent_hypothesis(
                    database,
                    session_id,
                    proposal['base_version_id'],
                    proposal['assistant_turn_id'],
                    proposal_guidance,
                    'proposal_review',
                    status='rejected',
                    proposal_id=proposal_id,
                    reason=clean_optional(payload.reason, 2000) or '',
                    services=services,
                )
            session_payload = services.serialize_session(database, session_id)
    if decision_rewrite is not None:
        try:
            rewritten = services.llm.rewrite_intent_progress(
                'confirmed_decision',
                decision_rewrite['canonical'],
                decision_rewrite['evidence'],
                decision_rewrite_language,
                'decision-' + payload.idempotencyKey,
                _deadline=min(services.monotonic() + 10.0, request.state.llm_deadline - 20.0),
            )
        except (LLMServiceError, TypeError, ValueError, KeyError):
            rewritten = {'detailedText': None, 'summaryText': decision_rewrite['canonical'], 'model': 'deterministic-fallback'}
        with services.connect(immediate=True) as database:
            services.record_event(
                database,
                session_id,
                'intent_progress_rewrite',
                {
                    'artifactType': 'confirmed_decision',
                    'artifactId': decision_rewrite['artifactId'],
                    'versionId': decision_rewrite['versionId'],
                    'language': decision_rewrite_language,
                    **rewritten,
                },
                services.utc_now(),
            )
            session_payload = services.serialize_session(database, session_id)
    if new_version_id:
        synchronize_version_with_online_match(session_id, new_version_id, 'stage', services=services)
    synchronize_dashboard_proposal_node(session_id, proposal_id, services=services)
    return session_payload
