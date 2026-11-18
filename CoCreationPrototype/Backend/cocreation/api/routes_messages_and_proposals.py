import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import quote
import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from level_validation import (
    HEIGHT,
    WIDTH,
    build_entity_bindings,
    build_untrusted_entity_bindings,
    build_map_facts,
    build_stage_snapshot,
    analyze_user_map_claims,
    format_map_claim_correction,
    derive_entity_transitions,
    entity_binding_fingerprint,
    entity_bindings_match_rows,
    LevelValidationError,
    describe_diff,
    summarize_verified_diff,
    summarize_stage_changes,
    validate_and_solve,
)
from demo_level_generator import generate_demo_level
from conversation_evidence import build_dialogue_evidence, with_latest
import component_recovery
import proposal_options
import binary_discovery
import binary_routes
from reply_reliability import CURRENT_REPLY_TRACE, REPLY_PIPELINE_VERSION, ReplyTrace, trace_reply, failure_reason
from proposal_diagnostics import (ProposalDiagnostic, CURRENT_PROPOSAL_DIAGNOSTIC, proposal_phase, trace_proposal)
from llm_client import validate_ai_edit_diff
from llm_client import (
    LLMExecutionResult,
    LLMServiceError,
    LLM_INTERNAL_DEADLINE_SECONDS,
    PROMPT_VERSION,
    PROPOSAL_GENERATION_ATTEMPTS,
    classify_challenge_reason,
    challenge_choice_card_copy,
    classify_revision_request,
    classify_turn_understanding,
    recover_human_edit_engagement,
    _dialogue_background_prompt,
    _strip_unverified_state_claims,
    review_intent_feedback,
    review_question_answers,
    rewrite_intent_progress,
    _contains_user_design_direction,
    _guidance_advice_request,
    _guidance_confusion_request,
    execute_revision_operations,
    generate_chat_reply,
    generate_stage_assessment,
    normalize_revision_plan,
    translate_turns,
    validate_execution_brief,
    _repair_stage_one_opening_display,
    _log_llm_event,
)
from repository import (
    DATABASE_PATH,
    connect,
    delete_demo_sessions,
    dump_json,
    get_current_version,
    get_session,
    get_version,
    load_entity_bindings,
    load_design_context,
    load_json,
    map_fingerprint,
    next_stage_number,
    next_turn_sequence,
    record_event,
    save_design_context,
    serialize_session,
)
from design_context import (
    add_confirmed_decision,
    add_open_question,
    apply_question_answer_review,
    apply_question_feedback,
    add_rejected_decision,
    clone_design_context,
    design_level_open_questions,
    evaluator_projection,
    empty_design_context,
    extract_explicit_user_memory,
    infer_intent_topic,
    is_design_level_question,
    merge_intent_hypothesis,
    merge_chat_update,
    resolve_intent_hypothesis,
    question_dedup_key,
    revision_projection,
    set_active_disagreement,
    sanitize_user_design_text,
)
from design_requirements import protected_change_requested, tradeoff_message
from proposal_understanding import bind_latest, protected_request_message, validate_topic, designer_sources, reusable_understanding
from revision_workflow import (
    SemanticConstraintError,
    validate_semantic_constraints,
)
from fastapi import APIRouter
from ..runtime_ref import app_attr, app_module, database_path
router = APIRouter()
from .config_models import ApiError, BinaryCancelRequest, BinaryChoiceRequest, ChallengeReviewRetryRequest, ComponentRecoveryRequest, IntentFeedbackRequest, MAX_MESSAGE_LENGTH, MessageRequest, ProposalDecisionRequest, QuestionFeedbackRequest, REVISION_CARD_ACTIONS, SESSION_COOKIE_NAME, _execute_revision_candidate_or_api_error, _llm_failure_details, _message_locks, _message_locks_guard, _solve_changed_proposal_or_api_error, _validate_changed_proposal, _validate_identifier, clean_optional, iso_time, record_agent_handoff, require_active_session, require_browser_session, require_current_base, utc_now
from .context_and_discovery import _adaptive_revision_routing, _allow_adaptive_proposal_completion, _design_context_guidance, _deterministic_conflict_explanation, _deterministic_intent_conflict, _intent_claims_equivalent, _is_vague_aesthetic_revision, _message_commit_binding, _pending_intent_evidence, _proposal_clarification_spec, _proposal_feasibility_profile, _public_guidance, _record_disagreement_event, _update_design_context_from_turn, _verified_proposal_message, build_llm_context, record_intent_evidence, record_intent_hypothesis
from .online_sync_and_dashboard import synchronize_dashboard_intent_node, synchronize_dashboard_player_challenge, synchronize_dashboard_proposal_node, synchronize_manual_edit_review_nodes
from .proposal_and_challenge import _action_for_message_key, _automatic_proposal_failure_execution, _bind_execution_to_stage, _challenge_choice, _classified_proposal_failure_execution, _enforce_challenge_choice_execution, _enforce_challenge_reason_execution, _enforce_human_edit_disagreement_followup, _ensure_alternative_revision_execution, _mark_new_discussion_guidance, _mark_proposal_discovery_guidance, _materialize_verified_automatic_offer, _normalize_manual_edit_review_execution, _persist_verified_requirements, _preflight_proposal, _proposal_cancelled_execution, _proposal_request_for_message_key, _proposal_review_guidance, _proposal_search_failure_execution, _record_card_action, _record_reply_outcome, _relaxed_revision_suggestion_execution, _require_offer_action, _revision_contract_clarification_execution, _rows_after_frozen_proposal, _salvage_challenge_analysis, _sanitize_challenge_execution, _source_revision_offer, _stage_has_prior_assistant_reply, _validate_message_action_payload, _verified_recovery_suggestion_execution
from .routes_sessions import read_session
from .session_persistence_helpers import _insert_decision, _insert_version


@router.post('/api/sessions/{session_id}/component-recoveries/{recovery_id}/retry')
def retry_component_recovery(session_id: str, recovery_id: str, payload: ComponentRecoveryRequest, request: Request, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(recovery_id, 'recoveryId')
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    deadline = request.state.llm_started_at + 10.0
    source_error = None
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        try:
            task = component_recovery.claim(database, session, recovery_id, payload.baseVersionId, payload.idempotencyKey)
        except ValueError as error:
            raise ApiError(409, str(error), 'This state recovery source or key is invalid.') from error
        if task is None:
            return serialize_session(database, session_id)
        try:
            source = component_recovery.replay_sources(database, session_id, task)
            context = build_llm_context(database, session_id, get_current_version(database, session))
            snapshot = context['stageContext']['stageSnapshot']
            original_background = _dialogue_background_prompt({'stageSnapshot': snapshot, 'dialogueEvidence': {'turns': [{'id': task.get('sourceAssistantTurnId'), 'role': 'assistant', 'content': source.pop('boundAssistantMessage')}]}})
            subsequent = source.pop('subsequentTurns')
            source.update({'boundQuestionBackground': original_background, 'subsequentUserMessages': [item for item in subsequent if item['role'] == 'user'], 'discussionBackground': _dialogue_background_prompt(context['stageContext']), 'activeDisagreement': context['stageContext'].get('activeDisagreement'), 'currentStageSnapshot': snapshot})
        except ValueError as error:
            source_error = str(error)
            component_recovery.record(database, session_id, task, 'expired', failureCode=source_error)
    if source_error:
        with connect() as database:
            return serialize_session(database, session_id)
    decision, failure_code = (None, None)
    try:
        decision = app_attr('recover_human_edit_engagement')(source, request.state.request_id, deadline)
        from proposal_understanding import validate_engagement
        decision = validate_engagement(decision, source['originalUserMessage'])
        if time.monotonic() >= deadline:
            failure_code = 'RECOVERY_TIMEOUT'
    except (LLMServiceError, ValueError, TypeError, KeyError, IndexError, TimeoutError, httpx.HTTPError) as error:
        failure_code = getattr(error, 'code', None) or 'STATE_UNVERIFIED'
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        component_recovery.finish(database, session, task, decision, failure_code)
        return serialize_session(database, session_id)

def _intent_feedback_event(database, session_id, idempotency_key):
    rows = database.execute("\n        SELECT event_type, payload_json FROM audit_events\n        WHERE session_id = ?\n          AND event_type IN (\n            'intent_hypothesis_feedback_applied',\n            'intent_hypothesis_feedback_pending'\n          )\n        ORDER BY id DESC\n        ", (session_id,)).fetchall()
    for row in rows:
        event = load_json(row['payload_json']) or {}
        if event.get('idempotencyKey') == idempotency_key:
            return event
    return None

def _intent_feedback_signature(payload, hypothesis_id, candidate):
    return {'hypothesisId': hypothesis_id, 'action': payload.action, 'candidateText': candidate or None, 'selectedHypothesisId': payload.selectedHypothesisId or None, 'sourceTurnId': payload.sourceTurnId, 'baseVersionId': payload.baseVersionId}

def _intent_feedback_signature_matches(stored, current):
    """Keep pre-conflict-choice idempotency records replayable."""
    stored = dict(stored or {})
    stored.setdefault('selectedHypothesisId', None)
    return stored == current

def _active_intent_conflict_choice(database, session_id, version_id):
    """Return the latest unresolved conflict choice for the current Stage."""
    rows = database.execute("\n        SELECT id, guidance_json FROM conversation_turns\n        WHERE session_id = ? AND version_id = ? AND role = 'assistant'\n        ORDER BY sequence_number DESC\n        ", (session_id, version_id)).fetchall()
    context = None
    for row in rows:
        guidance = load_json(row['guidance_json']) or {}
        if not guidance.get('_intentHypothesisId'):
            continue
        review = guidance.get('_intentReviewState') or {}
        if review.get('interactionMode') != 'conflict_choice':
            return None
        new_id = str(guidance.get('_intentHypothesisId') or '').strip()
        existing_id = str(review.get('existingHypothesisId') or '').strip()
        if not new_id or not existing_id:
            continue
        if context is None:
            context = load_design_context(database, session_id, version_id)
        hypotheses = {item.get('id'): item for item in context.get('intentHypotheses', []) if item.get('id')}
        new_item = hypotheses.get(new_id)
        existing_item = hypotheses.get(existing_id)
        if new_item is not None and existing_item is not None and (new_item.get('status') == 'tentative') and (existing_item.get('status') == 'confirmed'):
            return {'sourceTurnId': row['id'], 'newHypothesisId': new_id, 'existingHypothesisId': existing_id, 'explanation': str(review.get('reviewExplanation') or '').strip()}
        return None
    return None

def _question_feedback_event(database, session_id, idempotency_key):
    rows = database.execute("\n        SELECT payload_json FROM audit_events\n        WHERE session_id = ? AND event_type = 'question_feedback_applied'\n        ORDER BY id DESC\n        ", (session_id,)).fetchall()
    for row in rows:
        event = load_json(row['payload_json']) or {}
        if event.get('idempotencyKey') == idempotency_key:
            return event
    return None

@router.post('/api/sessions/{session_id}/questions/{question_id}/feedback')
def submit_question_feedback(session_id: str, question_id: str, payload: QuestionFeedbackRequest, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    """Deterministically ignore or restore one visible current-Stage question."""
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    _validate_identifier(question_id, 'questionId')
    signature = {'questionId': question_id, 'action': payload.action, 'baseVersionId': payload.baseVersionId}
    with message_request_lock(session_id, payload.idempotencyKey):
        with connect(immediate=True) as database:
            session = require_browser_session(database, session_id, access_cookie)
            prior = _question_feedback_event(database, session_id, payload.idempotencyKey)
            if prior is not None:
                if prior.get('request') != signature:
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The question feedback key was reused for a different request.')
                return {'outcome': prior['outcome'], 'session': serialize_session(database, session_id)}
            session = require_active_session(database, session_id, access_cookie)
            require_current_base(session, payload.baseVersionId)
            context = load_design_context(database, session_id, payload.baseVersionId)
            target = next((item for item in context.get('openQuestions', []) if item.get('id') == question_id), None)
            expected_status = 'open' if payload.action == 'ignore' else 'ignored'
            if target is None or target.get('sourceKind') not in {'visible_output', 'legacy'} or target.get('status') != expected_status:
                raise ApiError(409, 'STALE_QUESTION', 'This question is no longer available for that action.')
            changed_at = utc_now()
            context, changed = apply_question_feedback(context, question_id, payload.action, payload.baseVersionId, changed_at)
            if not changed:
                raise ApiError(409, 'STALE_QUESTION', 'This question is no longer available for that action.')
            save_design_context(database, payload.baseVersionId, context)
            app_attr('record_event')(database, session_id, 'question_feedback_applied', {'request': signature, 'idempotencyKey': payload.idempotencyKey, 'questionId': question_id, 'versionId': payload.baseVersionId, 'action': payload.action, 'outcome': 'applied'}, changed_at)
            return {'outcome': 'applied', 'session': serialize_session(database, session_id)}

def _require_actionable_intent_card(database, session_id, version_id, hypothesis_id, source_turn_id):
    latest = None
    for row in database.execute("\n        SELECT id, guidance_json FROM conversation_turns\n        WHERE session_id = ? AND version_id = ? AND role = 'assistant'\n        ORDER BY sequence_number\n        ", (session_id, version_id)).fetchall():
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
        synchronize_dashboard_intent_node(session_id, hypothesis_id)
        return result
    return wrapped

@router.post('/api/sessions/{session_id}/intent-hypotheses/{hypothesis_id}/feedback')
@_sync_intent_dashboard_after
def submit_intent_feedback(session_id: str, hypothesis_id: str, payload: IntentFeedbackRequest, request: Request, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
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
    with message_request_lock(session_id, payload.idempotencyKey):
        with connect(immediate=True) as database:
            session = require_browser_session(database, session_id, access_cookie)
            prior = _intent_feedback_event(database, session_id, payload.idempotencyKey)
            if prior is not None:
                if not _intent_feedback_signature_matches(prior.get('request'), signature):
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The feedback key was already used for a different request.')
                return {'outcome': prior['outcome'], 'session': serialize_session(database, session_id)}
            session = require_active_session(database, session_id, access_cookie)
            require_current_base(session, payload.baseVersionId)
            _, source_guidance = _require_actionable_intent_card(database, session_id, payload.baseVersionId, hypothesis_id, payload.sourceTurnId)
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
                evidence_id = record_intent_evidence(database, session_id, payload.baseVersionId, 'intent_conflict_choice', target.get('statement') if choice == 'new' else existing.get('statement'), turn_id=payload.sourceTurnId, details={'action': 'keep', 'choice': choice, 'newHypothesisId': new_id, 'existingHypothesisId': existing_id, 'explanation': str(review_state.get('reviewExplanation') or '')[:1200]})
                if choice == 'new':
                    context, _ = resolve_intent_hypothesis(context, new_id, 'confirm', supersedes_ids=[existing_id], evidence_id=evidence_id, stage_id=payload.baseVersionId, turn_id=payload.sourceTurnId, display_statement=target.get('statement'), display_language=session['language'])
                    outcome = 'kept_new'
                else:
                    context, _ = resolve_intent_hypothesis(context, new_id, 'reject', evidence_id=evidence_id, stage_id=payload.baseVersionId, turn_id=payload.sourceTurnId)
                    outcome = 'kept_existing'
                retained_old = next((item for item in context.get('intentHypotheses', []) if item.get('id') == existing_id), None)
                if retained_old is not None:
                    evidence_field = 'contradictingEvidenceIds' if choice == 'new' else 'supportingEvidenceIds'
                    retained_old[evidence_field] = list(dict.fromkeys(retained_old.get(evidence_field, []) + [evidence_id]))[:32]
                    retained_old['lastUpdatedStageId'] = payload.baseVersionId
                save_design_context(database, payload.baseVersionId, context)
                app_attr('record_event')(database, session_id, 'intent_hypothesis_feedback_applied', {'request': signature, 'idempotencyKey': payload.idempotencyKey, 'hypothesisId': new_id, 'versionId': payload.baseVersionId, 'outcome': outcome, 'evidenceId': evidence_id, 'selectedHypothesisId': selected_hypothesis_id, 'newHypothesisId': new_id, 'existingHypothesisId': existing_id, 'supersedesHypothesisIds': [existing_id] if choice == 'new' else [], 'explanation': str(review_state.get('reviewExplanation') or '')[:1200]}, utc_now())
                return {'outcome': outcome, 'session': serialize_session(database, session_id)}
            if payload.action == 'keep':
                raise ApiError(409, 'STALE_INTENT_CARD', 'This intent card is not an active conflict choice.')
            if review_state.get('interactionMode') == 'adjust_only' and payload.action != 'revise':
                raise ApiError(409, 'STALE_INTENT_CARD', 'This intent needs a revised statement before it can be saved.')
            statement = candidate if payload.action == 'revise' else target['statement']
            active = [{'hypothesisId': item['id'], 'statement': item['statement']} for item in context.get('intentHypotheses', []) if item.get('status') == 'confirmed' and item.get('id') != hypothesis_id]
            if payload.action == 'reject':
                evidence_id = record_intent_evidence(database, session_id, payload.baseVersionId, 'intent_feedback', statement, turn_id=payload.sourceTurnId, details={'action': payload.action})
                context, _ = resolve_intent_hypothesis(context, hypothesis_id, payload.action, evidence_id=evidence_id, stage_id=payload.baseVersionId, turn_id=payload.sourceTurnId)
                save_design_context(database, payload.baseVersionId, context)
                outcome = 'rejected'
                app_attr('record_event')(database, session_id, 'intent_hypothesis_feedback_applied', {'request': signature, 'idempotencyKey': payload.idempotencyKey, 'hypothesisId': hypothesis_id, 'versionId': payload.baseVersionId, 'outcome': outcome, 'evidenceId': evidence_id, 'supersedesHypothesisIds': []}, utc_now())
                return {'outcome': outcome, 'session': serialize_session(database, session_id)}
            evidence_summary = [str(item.get('observation') or '')[:500] for item in _pending_intent_evidence(database, session_id, get_version(database, session_id, payload.baseVersionId), context, limit=8)]
        if payload.action == 'confirm' and (not active):
            review = {'verdict': 'compatible', 'explanation': None, 'supersedesHypothesisIds': []}
        else:
            review = app_attr('review_intent_feedback')(statement, active, evidence_summary, session['language'], request.state.request_id, _deadline=request.state.llm_deadline)
        display_rewrite = {'detailedText': None, 'summaryText': statement, 'model': 'deterministic-fallback'}
        if review['verdict'] == 'compatible':
            try:
                display_rewrite = app_attr('rewrite_intent_progress')('inclination', statement, evidence_summary, session['language'], request.state.request_id, _deadline=min(time.monotonic() + 10.0, request.state.llm_deadline - 20.0))
            except (LLMServiceError, TypeError, ValueError, KeyError) as exception:
                display_rewrite['failureCode'] = getattr(exception, 'code', None) or type(exception).__name__
                display_rewrite['failureReason'] = str(exception)[:500]
        display_statement = display_rewrite['summaryText']
        with connect(immediate=True) as database:
            session = require_browser_session(database, session_id, access_cookie)
            prior = _intent_feedback_event(database, session_id, payload.idempotencyKey)
            if prior is not None:
                if not _intent_feedback_signature_matches(prior.get('request'), signature):
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The feedback key was reused.')
                return {'outcome': prior['outcome'], 'session': serialize_session(database, session_id)}
            session = require_active_session(database, session_id, access_cookie)
            require_current_base(session, payload.baseVersionId)
            _require_actionable_intent_card(database, session_id, payload.baseVersionId, hypothesis_id, payload.sourceTurnId)
            context = load_design_context(database, session_id, payload.baseVersionId)
            target = next((item for item in context.get('intentHypotheses', []) if item.get('id') == hypothesis_id), None)
            if target is None or target.get('status') != 'tentative':
                raise ApiError(409, 'STALE_INTENT_CARD', 'This intent card is stale.')
            if review['verdict'] == 'compatible':
                evidence_id = record_intent_evidence(database, session_id, payload.baseVersionId, 'intent_feedback', statement, turn_id=payload.sourceTurnId, details={'action': payload.action})
                context, _ = resolve_intent_hypothesis(context, hypothesis_id, payload.action, candidate_text=candidate, supersedes_ids=[], evidence_id=evidence_id, stage_id=payload.baseVersionId, turn_id=payload.sourceTurnId, display_statement=display_statement, display_language=session['language'])
                save_design_context(database, payload.baseVersionId, context)
                app_attr('record_event')(database, session_id, 'intent_hypothesis_feedback_applied', {'request': signature, 'idempotencyKey': payload.idempotencyKey, 'hypothesisId': hypothesis_id, 'versionId': payload.baseVersionId, 'outcome': 'applied', 'reviewedStatement': display_statement or statement, 'evidenceId': evidence_id, 'supersedesHypothesisIds': []}, utc_now())
                app_attr('record_event')(database, session_id, 'intent_progress_rewrite', {'artifactType': 'inclination', 'artifactId': hypothesis_id, 'versionId': payload.baseVersionId, 'language': session['language'], **display_rewrite}, utc_now())
                return {'outcome': 'applied', 'session': serialize_session(database, session_id)}
            conflict_existing_id = None
            if review['verdict'] == 'conflict':
                conflicting_ids = set(review.get('conflictingHypothesisIds') or [])
                conflict_existing_id = next((item['hypothesisId'] for item in reversed(active) if item['hypothesisId'] in conflicting_ids), None)
                if conflict_existing_id is None:
                    raise ApiError(502, 'MODEL_RESPONSE_INVALID', 'Kimi did not bind the conflict to confirmed memory.', retryable=True)
            user_text = candidate if payload.action == 'revise' else '符合我的想法' if session['language'] == 'zh-CN' else 'This matches my idea.'
            app_attr('insert_turn')(database, session, 'user', user_text, payload.baseVersionId, payload.idempotencyKey, None)
            explanation = review['explanation']
            execution = LLMExecutionResult(assistant_message=explanation, attempts_used=1, request_id=request.state.request_id, model='kimi-k2.6', guidance={'move': 'clarify_intent', 'intentHypothesis': statement, 'intentConfidence': 'low', 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': [], '_intentHypothesisId': hypothesis_id, '_intentReviewState': {'interactionMode': 'conflict_choice' if review['verdict'] == 'conflict' else 'adjust_only', 'reviewOutcome': review['verdict'], 'reviewExplanation': explanation, 'existingHypothesisId': conflict_existing_id, 'newHypothesisId': hypothesis_id}})
            assistant_turn_id = app_attr('insert_turn')(database, session, 'assistant', explanation, payload.baseVersionId, payload.idempotencyKey, execution)
            app_attr('record_event')(database, session_id, 'intent_hypothesis_feedback_pending', {'request': signature, 'idempotencyKey': payload.idempotencyKey, 'hypothesisId': hypothesis_id, 'versionId': payload.baseVersionId, 'outcome': review['verdict'], 'candidateText': statement, 'assistantTurnId': assistant_turn_id, 'explanation': explanation, 'existingHypothesisId': conflict_existing_id}, utc_now())
            return {'outcome': review['verdict'], 'session': serialize_session(database, session_id)}

def _normalize_challenge_reason_text(value):
    return re.sub('[\\W_]+', '', str(value or '').casefold(), flags=re.UNICODE)

def _route_message_to_active_challenge(session_id, payload, content, access_cookie):
    """Resolve challenge routing before any ordinary-chat side effect."""
    if payload.exitChallenge or payload.action not in {'none', 'continue_challenge'}:
        return payload
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        answered = database.execute("SELECT guidance_json FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'assistant'", (session_id, payload.idempotencyKey)).fetchone()
        if answered is not None:
            prior_state = (load_json(answered['guidance_json']) or {}).get('challengeState') or {}
            if prior_state.get('challengeId'):
                if payload.challengeId and payload.challengeId != prior_state['challengeId']:
                    raise ApiError(409, 'CHALLENGE_STALE', 'The retry belongs to another challenge.')
                return payload.model_copy(update={'action': 'continue_challenge', 'challengeId': prior_state['challengeId']})
            return payload
        require_current_base(session, payload.baseVersionId)
        challenge = database.execute("\n            SELECT * FROM revision_challenges\n            WHERE session_id = ? AND base_version_id = ?\n              AND status NOT IN ('resolved', 'superseded')\n            ORDER BY updated_at DESC LIMIT 1\n            ", (session_id, payload.baseVersionId)).fetchone()
        if challenge is None:
            return payload
        if payload.challengeId and payload.challengeId != challenge['challenge_id']:
            raise ApiError(409, 'CHALLENGE_STALE', 'The selected challenge is no longer active.')
        routed = payload.model_copy(update={'action': 'continue_challenge', 'challengeId': challenge['challenge_id']})
        review = database.execute("\n            SELECT review.message_key, review.status, turn.content\n            FROM challenge_reason_reviews AS review\n            JOIN conversation_turns AS turn ON turn.id = review.source_user_turn_id\n            WHERE review.session_id = ? AND review.challenge_id = ?\n              AND review.status IN ('review_pending', 'reviewing')\n            ORDER BY review.updated_at DESC LIMIT 1\n            ", (session_id, challenge['challenge_id'])).fetchone()
        if review is not None and _normalize_challenge_reason_text(review['content']) == _normalize_challenge_reason_text(content):
            routed = routed.model_copy(update={'idempotencyKey': review['message_key'], 'content': review['content']})
        elif review is not None and review['status'] == 'reviewing':
            raise ApiError(409, 'CHALLENGE_REVIEW_BUSY', 'This judgment is already being reviewed.')
        return routed

@router.post('/api/sessions/{session_id}/messages')
def send_message(session_id: str, payload: MessageRequest, request: Request, response: Response, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    content = payload.content.strip()
    if not content or len(content) > MAX_MESSAGE_LENGTH:
        raise ApiError(400, 'INVALID_MESSAGE', 'The message must contain 1 to 2000 characters.')
    payload = _route_message_to_active_challenge(session_id, payload, content, access_cookie)
    content = payload.content.strip()
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    _validate_message_action_payload(payload)
    with message_request_lock(session_id, payload.idempotencyKey):
        guard = None
        with connect(immediate=True) as database:
            guard_session = require_browser_session(database, session_id, access_cookie)
            active = load_design_context(database, session_id, payload.baseVersionId).get('activeDisagreement') or {}
            answered = database.execute("SELECT 1 FROM conversation_turns WHERE session_id=? AND request_id=? AND role='assistant'", (session_id, payload.idempotencyKey)).fetchone()
            if not answered and guard_session['current_version_id'] == payload.baseVersionId and (active.get('subject') == 'human_edit') and (active.get('status') == 'active'):
                guard = {'guardId': uuid.uuid4().hex, 'versionId': payload.baseVersionId, 'messageKey': payload.idempotencyKey, 'status': 'started', 'leaseUntil': iso_time(datetime.now(timezone.utc) + timedelta(seconds=120))}
                app_attr('record_event')(database, session_id, 'interaction_message_guard', guard, utc_now())
        delivered_payload = None
        request.state.interaction_guard_id = guard.get('guardId') if guard else None
        try:
            delivered_payload = _send_message_locked(session_id, payload, content, request, response, access_cookie)
            return delivered_payload
        except (ApiError, LLMServiceError) as error:
            projection = _record_dashboard_failed_request(session_id, payload, content, access_cookie, error)
            if projection is not None:
                try:
                    kind, identifier = projection
                    if kind == 'player_challenge':
                        synchronize_dashboard_player_challenge(session_id, identifier)
                    else:
                        synchronize_manual_edit_review_nodes(session_id, identifier)
                except ApiError:
                    pass
            raise
        finally:
            if guard:
                with connect(immediate=True) as database:
                    app_attr('record_event')(database, session_id, 'interaction_message_guard', {**guard, 'status': 'finished'}, utc_now())
                    if delivered_payload is not None:
                        delivered_payload['interactionState'] = component_recovery.interaction_state(database, get_session(database, session_id))

@router.post('/api/sessions/{session_id}/proposal-discovery/choices')
def choose_binary_proposal(session_id: str, payload: BinaryChoiceRequest, request: Request, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    for field in ('idempotencyKey', 'topicId', 'questionId', 'choiceId', 'baseVersionId'):
        _validate_identifier(getattr(payload, field), field)
    with message_request_lock(session_id, payload.idempotencyKey):
        return binary_routes.choose(app_module(), session_id, payload, request, access_cookie)

@router.post('/api/sessions/{session_id}/proposal-discovery/cancel')
def cancel_binary_proposal(session_id: str, payload: BinaryCancelRequest, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    for field in ('idempotencyKey', 'topicId', 'baseVersionId'):
        _validate_identifier(getattr(payload, field), field)
    with message_request_lock(session_id, payload.idempotencyKey):
        return binary_routes.cancel(app_module(), session_id, payload, access_cookie)

@router.post('/api/sessions/{session_id}/challenge-reviews/{review_id}/retry')
def retry_challenge_review(session_id: str, review_id: str, payload: ChallengeReviewRetryRequest, request: Request, response: Response, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(review_id, 'reviewId')
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        require_current_base(session, payload.baseVersionId)
    raise ApiError(410, 'CHALLENGE_REVIEW_RETRY_RETIRED', 'This dedicated retry endpoint has been retired. Retry the original chat message instead.')

def _record_dashboard_failed_request(session_id, payload, content, access_cookie, error):
    if not error.retryable:
        return None
    with connect(immediate=True) as database:
        try:
            require_browser_session(database, session_id, access_cookie)
        except ApiError:
            return None
        user = database.execute("SELECT * FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'user'", (session_id, payload.idempotencyKey)).fetchone()
        assistant = database.execute("SELECT id FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'assistant'", (session_id, payload.idempotencyKey)).fetchone()
        if user is None or assistant is not None or user['content'] != content or (user['version_id'] != payload.baseVersionId):
            return None
        event = {'messageKey': payload.idempotencyKey, 'code': error.code, 'versionId': payload.baseVersionId, 'userTurnId': user['id']}
        if payload.action == 'continue_challenge' and payload.challengeId:
            challenge = database.execute('SELECT challenge_id FROM revision_challenges WHERE session_id = ? AND challenge_id = ? AND base_version_id = ?', (session_id, payload.challengeId, payload.baseVersionId)).fetchone()
            if challenge is None:
                return None
            event['challengeId'] = payload.challengeId
            app_attr('record_event')(database, session_id, 'dashboard_challenge_request_failed', event, utc_now())
            return ('player_challenge', payload.challengeId)
        context = load_design_context(database, session_id, payload.baseVersionId) or {}
        if (context.get('activeDisagreement') or {}).get('subject') == 'human_edit':
            app_attr('record_event')(database, session_id, 'dashboard_human_edit_request_failed', event, utc_now())
            return ('llm_challenge', payload.baseVersionId)
    return None

def _send_message_locked(session_id, payload, content, request, response, access_cookie):
    trace = ReplyTrace(request.state.request_id, payload.idempotencyKey, payload.baseVersionId, _log_llm_event)
    token = CURRENT_REPLY_TRACE.set(trace)
    proposal_trace = ProposalDiagnostic(trace.identity, _log_llm_event)
    proposal_token = CURRENT_PROPOSAL_DIAGNOSTIC.set(proposal_trace)
    trace.record('routing', 'started')
    try:
        result = _send_message_locked_impl(session_id, payload, content, request, response, access_cookie)
        proposal_trace.outcome = 'passed'
        proposal_trace.final_failure = None
        trace.record('http_response', 'prepared')
        return result
    except (ApiError, LLMServiceError) as error:
        proposal_trace.outcome = 'failed'
        proposal_trace.error((getattr(error, 'details', {}) or {}).get('failureStage', 'request_preflight'), 'request', error)
        trace.record('request', 'failed', failure_reason(error))
        error.details = {**(getattr(error, 'details', {}) or {}), 'replyPipeline': trace.diagnostic(), 'proposalDiagnostic': proposal_trace.snapshot()}
        raise
    except Exception as error:
        proposal_trace.outcome = 'failed'
        committing = any((step['stage'] == 'commit' and step['status'] == 'started' for step in trace.steps))
        proposal_trace.error('commit' if committing else 'request', 'commit' if committing else 'request', error)
        raise
    finally:
        try:
            with connect(immediate=True) as database:
                require_browser_session(database, session_id, access_cookie)
                observed = database.execute('SELECT 1 FROM conversation_turns WHERE session_id=? AND request_id=? LIMIT 1', (session_id, payload.idempotencyKey)).fetchone()
                if not observed:
                    observed = database.execute("SELECT 1 FROM audit_events WHERE session_id=? AND event_type='message_generation_failed' AND json_extract(payload_json,'$.messageKey')=? LIMIT 1", (session_id, payload.idempotencyKey)).fetchone()
                if observed:
                    app_attr('record_event')(database, session_id, 'reply_pipeline_diagnostic', {**trace.diagnostic(), 'proposalDiagnostic': proposal_trace.snapshot()}, utc_now())
        except (ApiError, sqlite3.Error):
            pass
        CURRENT_REPLY_TRACE.reset(token)
        CURRENT_PROPOSAL_DIAGNOSTIC.reset(proposal_token)

def _send_message_locked_impl(session_id, payload, content, request, response, access_cookie):
    message_started_at = getattr(request.state, 'llm_started_at', time.monotonic())
    precomputed_understanding = None
    if payload.action != 'none':
        with connect() as database:
            require_active_session(database, session_id, access_cookie)
            active_binary = binary_discovery.current_topic(database, session_id, payload.baseVersionId)
            if active_binary and active_binary['status'] in binary_discovery.ACTIVE:
                raise ApiError(409, 'PROPOSAL_CHOICE_REQUIRED', 'Finish or cancel the current A/B proposal first.')
    if payload.action == 'none':
        with connect() as read_database:
            read_session = require_active_session(read_database, session_id, access_cookie)
            request_signature = hashlib.sha256(dump_json({'content': content, 'baseVersionId': payload.baseVersionId, 'requestProposal': payload.requestProposal, 'action': payload.action, 'sourceTurnId': payload.sourceTurnId}).encode('utf-8')).hexdigest()
            trace_proposal('request_binding', 'binding', 'passed', bindingSignature=request_signature, sourceTurnId=payload.sourceTurnId)
            prior_failure = read_database.execute("SELECT payload_json FROM audit_events WHERE session_id=? AND event_type='message_generation_failed' AND json_extract(payload_json, '$.messageKey')=? ORDER BY id DESC LIMIT 1", (session_id, payload.idempotencyKey)).fetchone()
            previous_signature = (load_json(prior_failure['payload_json']) or {}).get('requestSignature') if prior_failure else None
            if previous_signature and previous_signature != request_signature:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The failed message key belongs to a different request.')
            read_user = read_database.execute("SELECT content, version_id FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'user'", (session_id, payload.idempotencyKey)).fetchone()
            if read_user is not None and (read_user['content'] != content or read_user['version_id'] != payload.baseVersionId):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used for different content or Stage.')
            already_answered = read_database.execute("SELECT 1 FROM conversation_turns\n                WHERE session_id = ? AND request_id = ? AND role = 'assistant'", (session_id, payload.idempotencyKey)).fetchone()
            binary_receipt = binary_routes._receipt(read_database, session_id, payload.idempotencyKey)
            if binary_receipt and binary_receipt['request'].get('kind') == 'start':
                expected = {'kind': 'start', 'baseVersionId': payload.baseVersionId, 'content': content, 'requestProposal': payload.requestProposal}
                if binary_receipt['request'] != expected:
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'This key belongs to another action.')
                if binary_receipt['status'] == 'done':
                    return serialize_session(read_database, session_id)
            if already_answered is None:
                require_current_base(read_session, payload.baseVersionId)
                binary_topic = binary_discovery.current_topic(read_database, session_id, payload.baseVersionId)
                if binary_topic and binary_topic['status'] in binary_discovery.ACTIVE and (payload.idempotencyKey != binary_topic['initialMessageKey'] or content != binary_topic['initialRequest']):
                    raise ApiError(409, 'PROPOSAL_CHOICE_REQUIRED', 'Use the current A/B buttons, retry, or cancel this proposal.')
            saved_understanding = read_database.execute("SELECT payload_json FROM audit_events\n                WHERE session_id = ? AND event_type = 'turn_understanding'\n                  AND json_extract(payload_json, '$.messageKey') = ?\n                ORDER BY id DESC LIMIT 1", (session_id, payload.idempotencyKey)).fetchone()
            if saved_understanding is not None and already_answered is None and (not reusable_understanding(load_json(saved_understanding['payload_json'])['result'], payload.baseVersionId)):
                saved_understanding = None
            if saved_understanding is not None:
                precomputed_understanding = load_json(saved_understanding['payload_json'])['result']
            elif already_answered is None:
                read_version = get_current_version(read_database, read_session)
                read_context = build_llm_context(read_database, session_id, read_version)
                prior_conversation = read_context['conversation']
                if not prior_conversation or prior_conversation[-1].get('role') != 'user' or prior_conversation[-1].get('content') != content:
                    prior_conversation = [*prior_conversation, {'role': 'user', 'content': content}]
                snapshot_for_understanding = read_context['stageContext']['stageSnapshot']
                proposal_context_for_understanding = read_context['stageContext'].get('proposalDiscovery') or {}
            else:
                prior_conversation = None
        if saved_understanding is None and already_answered is None:
            interpretation_context = {key: proposal_context_for_understanding.get(key) for key in ('topicId', 'versionId', 'initialRequest', 'questions', 'answers', 'lastQuestionText', 'userTurns', 'understanding', 'optionSet', 'optionQuestion', 'optionDecision', 'clarificationQuestionCount')} if proposal_context_for_understanding else {}
            interpretation_context['dialogueEvidence'] = with_latest(read_context['stageContext'].get('dialogueEvidence'), content)
            interpretation_context['activeDisagreement'] = read_context['stageContext'].get('activeDisagreement')
            try:
                precomputed_understanding = app_attr('classify_turn_understanding')(prior_conversation, snapshot_for_understanding, request.state.request_id, forced_proposal=payload.requestProposal, proposal_context=interpretation_context, _deadline=message_started_at + LLM_INTERNAL_DEADLINE_SECONDS)
            except LLMServiceError as error:
                with connect(immediate=True) as database:
                    active_session = require_active_session(database, session_id, access_cookie)
                    require_current_base(active_session, payload.baseVersionId)
                    app_attr('record_event')(database, session_id, 'message_generation_failed', {'messageKey': payload.idempotencyKey, 'baseVersionId': payload.baseVersionId, 'requestId': request.state.request_id, 'code': error.code, 'retryable': error.retryable, 'attemptsUsed': error.attempts_used, 'proposalRequested': payload.requestProposal, 'requestSignature': request_signature, 'details': {**_llm_failure_details(error), 'task': 'turn_understanding', 'failureStage': (getattr(error, 'details', {}) or {}).get('failureStage', 'understanding'), 'maximumAttempts': 2}}, utc_now())
                    if proposal_context_for_understanding:
                        app_attr('record_event')(database, session_id, 'proposal_discovery_progress', {'stageId': payload.baseVersionId, 'topicId': proposal_context_for_understanding['topicId'], 'messageKey': payload.idempotencyKey, 'status': 'retry_pending', 'retryable': True, 'failureCode': error.code, 'clarificationQuestionCount': proposal_context_for_understanding.get('clarificationQuestionCount', 0)}, utc_now())
                if payload.requestProposal or proposal_context_for_understanding or error.code != 'MODEL_RESPONSE_INVALID':
                    raise
                precomputed_understanding = {'acts': ['unclear'], 'elements': [], 'evidenceSpan': content, 'directionSufficient': False, 'mapRelated': True, 'changes': [], 'proposalUnderstanding': None, 'conversationOnly': True, 'semanticPolicyVersion': 1}
    if payload.action == 'none' and precomputed_understanding is not None and (already_answered is None):
        with connect() as database:
            session = require_active_session(database, session_id, access_cookie)
            require_current_base(session, payload.baseVersionId)
            binary_context = build_llm_context(database, session_id, get_current_version(database, session))
            binary_topic = binary_discovery.current_topic(database, session_id, payload.baseVersionId)
            retry_binary = bool(binary_topic and binary_topic['status'] in binary_discovery.ACTIVE and (binary_topic['initialMessageKey'] == payload.idempotencyKey))
        if retry_binary or binary_routes.should_start(payload, precomputed_understanding, binary_context):
            return binary_routes.start(app_module(), session_id, payload, content, precomputed_understanding, request, access_cookie)
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        prior_user = database.execute("\n            SELECT * FROM conversation_turns\n            WHERE session_id = ? AND request_id = ? AND role = 'user'\n            ", (session_id, payload.idempotencyKey)).fetchone()
        if prior_user is not None and (prior_user['content'] != content or prior_user['version_id'] != payload.baseVersionId):
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used for different content or Stage.')
        prior_action = _action_for_message_key(database, session_id, payload.idempotencyKey)
        if prior_action is not None and prior_action != (payload.action, payload.sourceTurnId):
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used for a different card action.')
        if prior_user is not None and payload.action in REVISION_CARD_ACTIONS and (prior_action is None):
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used by an ordinary message.')
        prior_proposal_request = _proposal_request_for_message_key(database, session_id, payload.idempotencyKey)
        if prior_user is not None and bool(prior_proposal_request) != payload.requestProposal:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used with a different proposal-request mode.')
        prior_assistant = database.execute("\n            SELECT * FROM conversation_turns\n            WHERE session_id = ? AND request_id = ? AND role = 'assistant'\n            ", (session_id, payload.idempotencyKey)).fetchone()
        if prior_assistant is not None:
            session_payload = serialize_session(database, session_id)
            database.commit()
            app_attr('synchronize_turn_with_online_match')(session_id, payload.idempotencyKey)
            return session_payload
        if payload.action in {'none', 'continue_challenge'}:
            require_current_base(session, payload.baseVersionId)
        elif payload.baseVersionId != session['current_version_id']:
            raise ApiError(409, 'PROPOSAL_STALE', 'The selected proposal belongs to an older Stage.', details={'reason': 'version_changed'})
        if payload.action == 'none' and _active_intent_conflict_choice(database, session_id, payload.baseVersionId) is not None:
            raise ApiError(409, 'INTENT_CONFLICT_PENDING', 'Retain one of the conflicting inclinations before sending another message.')
        source_turn = None
        source_offer = None
        if payload.action in REVISION_CARD_ACTIONS:
            source_turn, source_offer = _source_revision_offer(database, session_id, payload.baseVersionId, payload.sourceTurnId)
            _require_offer_action(source_offer, payload.action)
        current = get_current_version(database, session)
        if payload.action in REVISION_CARD_ACTIONS:
            source_binding = _preflight_proposal(database, session_id, session, source_turn, source_offer, load_json(current['rows_json']))
        else:
            source_binding = None
        allow_progress = payload.action == 'none' and _stage_has_prior_assistant_reply(database, session_id, payload.baseVersionId)
        if prior_user is None:
            user_turn_id = app_attr('insert_turn')(database, session, 'user', content, payload.baseVersionId, payload.idempotencyKey, None)
            if payload.action == 'none':
                explicit_goals, explicit_constraints = extract_explicit_user_memory(content)
                for explicit_text in dict.fromkeys([*explicit_goals, *explicit_constraints]):
                    record_intent_evidence(database, session_id, payload.baseVersionId, 'expressed_direction', explicit_text, turn_id=user_turn_id, details={'requestProposal': bool(payload.requestProposal)})
            if payload.requestProposal:
                app_attr('record_event')(database, session_id, 'proposal_request_requested', {'messageKey': payload.idempotencyKey, 'turnId': user_turn_id, 'baseVersionId': payload.baseVersionId}, utc_now())
        else:
            user_turn_id = prior_user['id']
        if payload.action in REVISION_CARD_ACTIONS and prior_action is None:
            _record_card_action(database, session_id, payload, source_offer)
        elif payload.action == 'continue_challenge' and prior_action is None:
            app_attr('record_event')(database, session_id, 'challenge_continued', {'messageKey': payload.idempotencyKey, 'action': payload.action, 'challengeId': payload.challengeId, 'baseVersionId': payload.baseVersionId}, utc_now())
        retrying_failed_message = prior_user is not None
        context = build_llm_context(database, session_id, current)
        language = session['language']
        turn_understanding = None
        if payload.action == 'none':
            saved_understanding = database.execute("SELECT payload_json FROM audit_events\n                WHERE session_id = ? AND event_type = 'turn_understanding'\n                  AND json_extract(payload_json, '$.messageKey') = ?\n                ORDER BY id DESC LIMIT 1", (session_id, payload.idempotencyKey)).fetchone()
            if saved_understanding is not None and reusable_understanding(load_json(saved_understanding['payload_json'])['result'], payload.baseVersionId):
                turn_understanding = load_json(saved_understanding['payload_json'])['result']
            else:
                turn_understanding = bind_latest(precomputed_understanding, user_turn_id, payload.baseVersionId)
                app_attr('record_event')(database, session_id, 'turn_understanding', {'messageKey': payload.idempotencyKey, 'baseVersionId': payload.baseVersionId, 'result': turn_understanding}, utc_now())
                if turn_understanding.get('criticalReview'):
                    app_attr('record_event')(database, session_id, 'proposal_understanding_reviewed', {'messageKey': payload.idempotencyKey, 'baseVersionId': payload.baseVersionId, **turn_understanding['criticalReview']}, utc_now())
            context = build_llm_context(database, session_id, current)
        stage_context = context['stageContext']
        stage_context.setdefault('_reliabilityState', {})
        stage_context['requirementPolicyVersion'] = 1
        stage_context['requirementUserTurns'] = [{'id': user_turn_id, 'content': content}]
        if turn_understanding is not None:
            stage_context['turnUnderstanding'] = turn_understanding
            stage_context['conversationOnly'] = bool(turn_understanding.get('conversationOnly'))
            if stage_context['conversationOnly']:
                allow_progress = False
        stage_context['aiEditableTilesOnly'] = True
        user_map_claims = analyze_user_map_claims(content, stage_context.get('stageSnapshot'))
        stage_context['userMapClaims'] = user_map_claims
        proposal_discovery = stage_context.get('proposalDiscovery') or {}
        revision_routing = _adaptive_revision_routing(content, user_map_claims, stage_context.get('stageSnapshot'), proposal_discovery=proposal_discovery, turn_understanding=turn_understanding)
        stage_context['adaptiveProposalCompletion'] = revision_routing == 'needs_clarification' and (not proposal_options.current_set(proposal_discovery, stage_context.get('stageSnapshot'))) and _allow_adaptive_proposal_completion(content, user_map_claims, stage_context.get('stageSnapshot'), stage_context.get('clarificationQuestionCount', 0))
        if stage_context['adaptiveProposalCompletion']:
            revision_routing = 'proposal'
        stage_context['revisionRouting'] = revision_routing
        stage_context['clarificationBudgetExhausted'] = bool(proposal_discovery and int(proposal_discovery.get('clarificationQuestionCount') or 0) >= 3)
        stage_context['feasibilityProfile'] = _proposal_feasibility_profile(stage_context.get('stageSnapshot'), context.get('rows') or [], stage_context.get('designContext'), context.get('validation'))
        stage_context['vagueAestheticRevision'] = _is_vague_aesthetic_revision(content)
        proposal_state = {'proposal': 'ready_with_explicit_binding', 'proposal_conservative': 'ready_with_conservative_binding', 'proposal_blocked': 'revision_needed', 'proposal_cancelled': 'cancelled'}.get(revision_routing, 'clarifying')
        stage_context['proposalState'] = proposal_state
        stage_context['proposalBindingMode'] = {'ready_with_explicit_binding': 'explicit', 'ready_with_conservative_binding': 'conservative'}.get(proposal_state)
        stage_context['responseLanguage'] = language
        if proposal_discovery and proposal_discovery.get('sourceTurnId'):
            started = database.execute("\n                SELECT 1 FROM audit_events\n                WHERE session_id = ? AND event_type = 'proposal_discovery_started'\n                  AND json_extract(payload_json, '$.topicId') = ?\n                LIMIT 1\n                ", (session_id, proposal_discovery['topicId'])).fetchone()
            if started is None:
                app_attr('record_event')(database, session_id, 'proposal_discovery_started', {'stageId': payload.baseVersionId, 'topicId': proposal_discovery['topicId'], 'sourceTurnId': proposal_discovery['sourceTurnId'], 'messageKey': payload.idempotencyKey, 'trigger': 'proposal_button' if payload.requestProposal else 'direct_modification'}, utc_now())
        if payload.action == 'none' and revision_routing == 'needs_clarification' and proposal_discovery and (not user_map_claims.get('conflicts')):
            stage_context['proposalClarification'] = _proposal_clarification_spec(proposal_discovery, stage_context.get('stageSnapshot'), language)
        if proposal_state.startswith('ready_') and proposal_discovery.get('brief'):
            stage_context['authorizedRevisionBrief'] = proposal_discovery['brief']
        if user_map_claims.get('conflicts'):
            allow_progress = False
            app_attr('record_event')(database, session_id, 'map_claim_conflict', {'versionId': payload.baseVersionId, 'messageKey': payload.idempotencyKey, 'claimCount': len(user_map_claims.get('conflicts') or []), 'conflicts': [{key: value for key, value in conflict.items() if key != 'sourceText'} for conflict in user_map_claims['conflicts']], 'mapFingerprint': stage_context.get('mapFingerprint'), 'entityBindingFingerprint': stage_context.get('entityBindingFingerprint')}, utc_now())
        stage_context['discussionCardMode'] = 'disagreement_only'
        stage_context['revisionSourceTurnIds'] = [user_turn_id]
        stage_context['explicitProposalRequest'] = payload.requestProposal
        stage_context['explicitAction'] = payload.action
        stage_context['actionSourceTurnId'] = payload.sourceTurnId
        stage_context['sourceProposalOffer'] = source_offer
        if source_binding is not None and payload.action == 'execute_revision':
            stage_context['proposalBindingFrozen'] = True
            stage_context['authorizedExecutionBrief'] = source_binding.get('executionBrief')
            transitions = source_binding.get('executionBrief', {}).get('requiredTransitions', [])
            stage_context['deterministicExactExecution'] = bool(transitions)
        stage_context['deferRevisionExecution'] = payload.action != 'execute_revision' and (not proposal_state.startswith('ready_'))
        if (payload.action in REVISION_CARD_ACTIONS or payload.requestProposal) and stage_context.get('activeDisagreement'):
            raise ApiError(409, 'DISAGREEMENT_ACTIVE', 'Resolve the active design disagreement before choosing another revision card.')
        if payload.action == 'challenge_revision':
            stage_context['challengeRevision'] = {'sourceTurnId': payload.sourceTurnId, 'proposalOffer': source_offer}
        elif payload.action == 'alternative_revision':
            stage_context['alternativeRevision'] = {'sourceTurnId': payload.sourceTurnId, 'proposalOffer': source_offer}
            excluded_rows = _rows_after_frozen_proposal(context['rows'], (source_binding or {}).get('executionBrief'))
            if excluded_rows is not None:
                stage_context['excludedProposalCandidateFingerprint'] = map_fingerprint(excluded_rows)
    pending_disagreement = stage_context.get('activeDisagreement') or {}
    if payload.challengeChoice is not None and (not (pending_disagreement.get('subject') == 'ai_revision_challenge' and pending_disagreement.get('phase') == 'choice_pending' and (pending_disagreement.get('challengeId') == payload.challengeId))):
        raise ApiError(409, 'CHALLENGE_STALE', 'This choice no longer belongs to the active challenge.')
    deterministic_challenge_choice = payload.challengeChoice or _challenge_choice(content, language) if pending_disagreement.get('subject') == 'ai_revision_challenge' and pending_disagreement.get('phase') == 'choice_pending' else None
    if deterministic_challenge_choice is not None:
        with connect() as challenge_database:
            challenge_row = challenge_database.execute('SELECT source.id, source.version_id, source.proposal_binding_json,\n                          source.guidance_json, source.request_id, challenge.current_reason_turn_id\n                FROM revision_challenges AS challenge\n                JOIN conversation_turns AS source ON source.id = challenge.source_proposal_turn_id\n                WHERE challenge.session_id = ? AND challenge.challenge_id = ?\n                  AND challenge.base_version_id = ?', (session_id, pending_disagreement.get('challengeId'), payload.baseVersionId)).fetchone()
        binding = load_json(challenge_row['proposal_binding_json']) or {} if challenge_row else {}
        if challenge_row is None or challenge_row['version_id'] != payload.baseVersionId or binding.get('mapFingerprint') != map_fingerprint(context['rows']) or (not (binding.get('executionBrief') or {}).get('requiredTransitions')):
            raise ApiError(409, 'CHALLENGE_STALE', 'The challenged proposal is no longer available on this Stage.')
        stage_context['sourceProposalOffer'] = (load_json(challenge_row['guidance_json']) or {}).get('proposalOffer') or {}
        stage_context['requirementSourceCardId'] = challenge_row['id']
        stage_context['requirementReasonTurnId'] = challenge_row['current_reason_turn_id']
        stage_context['requirementDecisionEvidence'] = {'sourceTurnId': user_turn_id, 'choice': deterministic_challenge_choice, 'reasonTurnId': challenge_row['current_reason_turn_id']}
        if deterministic_challenge_choice == 'user':
            excluded_rows = _rows_after_frozen_proposal(context['rows'], binding.get('executionBrief'))
            if excluded_rows is not None:
                stage_context['excludedProposalCandidateFingerprint'] = map_fingerprint(excluded_rows)
    question_answer_review = {'answeredQuestionIds': [], 'results': []}
    joint_ordinary_review = bool(payload.action == 'none' and (not payload.requestProposal) and (not proposal_discovery) and (not stage_context.get('activeDisagreement')) and (not stage_context.get('conversationOnly')) and (not set((stage_context.get('turnUnderstanding') or {}).get('acts') or []) & {'revision_request'}))
    stage_context['ordinaryOpenQuestions'] = [{'questionId': q.get('id'), 'question': q.get('question')} for q in stage_context.get('designContext', {}).get('openQuestions', []) if q.get('status') == 'open' and q.get('sourceKind') in {'visible_output', 'legacy'}][:24] if joint_ordinary_review and allow_progress else []
    if not joint_ordinary_review and payload.action in {'none', 'continue_challenge'} and (deterministic_challenge_choice is None) and (allow_progress or bool(proposal_discovery) or bool(stage_context.get('activeDisagreement'))):
        visible_open_questions = [{'questionId': item.get('id'), 'question': item.get('question')} for item in stage_context.get('designContext', {}).get('openQuestions', []) if item.get('status') == 'open' and item.get('sourceKind') in {'visible_output', 'legacy'}]
        if visible_open_questions:
            optional_started = time.monotonic()
            try:
                question_answer_review = app_attr('review_question_answers')(visible_open_questions, content, language, request.state.request_id, _deadline=min(optional_started + 10.0, message_started_at + LLM_INTERNAL_DEADLINE_SECONDS - 20.0))
            except LLMServiceError:
                question_answer_review = {'answeredQuestionIds': [], 'results': []}
            finally:
                stage_context['_optionalRepairSpent'] = time.monotonic() - optional_started
    stage_context['answeredQuestionIds'] = list(question_answer_review.get('answeredQuestionIds') or [])
    stage_context['answeredVisibleQuestion'] = bool(stage_context['answeredQuestionIds'])
    challenge_reason_classification = None
    challenge_review_id = None
    challenge_choice_resolution = None
    challenge_deadline = None
    active_disagreement = stage_context.get('activeDisagreement') or {}
    if payload.action == 'continue_challenge' or (payload.action == 'none' and (not payload.exitChallenge)):
        stage_context = context['stageContext']
        active_disagreement = stage_context.get('activeDisagreement') or {}
        if active_disagreement.get('subject') == 'ai_revision_challenge' and active_disagreement.get('phase') == 'choice_pending':
            challenge_choice_resolution = deterministic_challenge_choice
            stage_context['challengeChoiceResolution'] = challenge_choice_resolution
            if challenge_choice_resolution:
                choice_key = question_dedup_key(active_disagreement.get('nextQuestion'))
                for item in stage_context.get('designContext', {}).get('openQuestions', []):
                    if item.get('status') == 'open' and question_dedup_key(item.get('question')) == choice_key and item.get('id'):
                        answered_ids = question_answer_review.setdefault('answeredQuestionIds', [])
                        if item['id'] not in answered_ids:
                            answered_ids.append(item['id'])
                            question_answer_review.setdefault('results', []).append({'questionId': item['id'], 'answered': True, 'source': 'deterministic_challenge_choice'})
                        break
        else:
            challenge_context = stage_context.get('challengeContext') or {}
            hypotheses = challenge_context.get('hypotheses') or {}
            if active_disagreement.get('subject') == 'ai_revision_challenge' and active_disagreement.get('phase') == 'reason_review':
                hypotheses = {'primary': active_disagreement.get('primaryHypothesis'), 'secondary': active_disagreement.get('secondaryHypothesis'), 'challengeId': active_disagreement.get('challengeId')}
            if hypotheses.get('primary') and hypotheses.get('secondary') and (not payload.challengeId or payload.challengeId == hypotheses.get('challengeId')):
                challenge_id = hypotheses.get('challengeId')
                challenge_review_id = 'cr-' + hashlib.sha256(f'{challenge_id}:{user_turn_id}'.encode('utf-8')).hexdigest()[:24]
                with connect(immediate=True) as database:
                    now = utc_now()
                    database.execute("\n                        UPDATE challenge_reason_reviews\n                        SET status = 'superseded', updated_at = ?\n                        WHERE session_id = ? AND challenge_id = ?\n                          AND status = 'review_pending'\n                          AND source_user_turn_id != ?\n                        ", (now, session_id, challenge_id, user_turn_id))
                    database.execute("\n                        INSERT INTO challenge_reason_reviews(\n                            review_id, session_id, challenge_id,\n                            source_user_turn_id, message_key, status,\n                            attempts_used, failure_code, result_json,\n                            created_at, updated_at\n                        ) VALUES (?, ?, ?, ?, ?, 'reviewing', 0, NULL, NULL, ?, ?)\n                        ON CONFLICT(session_id, source_user_turn_id) DO UPDATE SET\n                            status = 'reviewing', failure_code = NULL,\n                            updated_at = excluded.updated_at\n                        ", (challenge_review_id, session_id, challenge_id, user_turn_id, payload.idempotencyKey, now, now))
                    database.execute("\n                        UPDATE revision_challenges\n                        SET status = 'reviewing', current_reason_turn_id = ?, updated_at = ?\n                        WHERE challenge_id = ? AND session_id = ?\n                        ", (user_turn_id, now, challenge_id, session_id))
                challenge_deadline = message_started_at + LLM_INTERNAL_DEADLINE_SECONDS
                try:
                    challenge_reason_classification = app_attr('classify_challenge_reason')(content, hypotheses, active_disagreement.get('proposalSummary') or hypotheses.get('proposalSummary') or challenge_context.get('proposalSummary'), request.state.request_id, _deadline=challenge_deadline, simple=True, stage_snapshot=stage_context.get('stageSnapshot'), comparison_validator=lambda comparison: _salvage_challenge_analysis(comparison, context['rows'], stage_context.get('entityBindings'), language))
                except LLMServiceError as exception:
                    with connect(immediate=True) as database:
                        active_session = require_active_session(database, session_id, access_cookie)
                        require_current_base(active_session, payload.baseVersionId)
                        duplicate = database.execute("\n                            SELECT 1 FROM audit_events\n                            WHERE session_id = ?\n                              AND event_type = 'challenge_reason_review_pending'\n                              AND json_extract(payload_json, '$.messageKey') = ?\n                            LIMIT 1\n                            ", (session_id, payload.idempotencyKey)).fetchone()
                        if duplicate is None:
                            app_attr('record_event')(database, session_id, 'challenge_reason_review_pending', {'messageKey': payload.idempotencyKey, 'baseVersionId': payload.baseVersionId, 'challengeId': hypotheses.get('challengeId') or challenge_context.get('challengeId'), 'sourceUserTurnId': user_turn_id, 'state': 'review_pending', 'task': 'challenge_review', 'failureStage': 'upstream', 'failureCode': exception.code, 'attemptsUsed': exception.attempts_used, 'maximumAttempts': 2, 'retryable': True, 'safeReason': exception.safe_message}, utc_now())
                        now = utc_now()
                        database.execute("\n                            UPDATE challenge_reason_reviews\n                            SET status = 'review_pending', attempts_used = ?,\n                                failure_code = ?, updated_at = ?\n                            WHERE review_id = ? AND session_id = ?\n                            ", (exception.attempts_used, exception.code, now, challenge_review_id, session_id))
                        database.execute("\n                            UPDATE revision_challenges\n                            SET status = 'review_pending', updated_at = ?\n                            WHERE challenge_id = ? AND session_id = ?\n                            ", (now, challenge_id, session_id))
                    raise ApiError(exception.status_code, 'CHALLENGE_REVIEW_FAILED', exception.safe_message, retryable=True, details={'task': 'challenge_review', 'failureStage': 'upstream', 'failureCode': exception.code, 'attemptsUsed': exception.attempts_used, 'maximumAttempts': 2}) from exception
                comparison = challenge_reason_classification.get('comparison')
                if comparison:
                    safe_comparison = _salvage_challenge_analysis(comparison, context['rows'], stage_context.get('entityBindings'), language)
                    if not safe_comparison:
                        exception = ValueError('No grounded comparison remained after removing invalid fragments.')
                        with connect(immediate=True) as database:
                            active_session = require_active_session(database, session_id, access_cookie)
                            require_current_base(active_session, payload.baseVersionId)
                            app_attr('record_event')(database, session_id, 'challenge_reason_review_pending', {'messageKey': payload.idempotencyKey, 'baseVersionId': payload.baseVersionId, 'challengeId': hypotheses.get('challengeId'), 'sourceUserTurnId': user_turn_id, 'state': 'review_pending', 'task': 'challenge_review', 'failureStage': 'binding', 'failureCode': 'MAP_GROUNDING_INVALID', 'attemptsUsed': challenge_reason_classification.get('attemptsUsed', 1), 'maximumAttempts': 2, 'retryable': True, 'safeReason': 'The comparison did not match the current Stage facts.'}, utc_now())
                            now = utc_now()
                            database.execute("\n                                UPDATE challenge_reason_reviews\n                                SET status = 'review_pending', attempts_used = ?,\n                                    failure_code = 'MAP_GROUNDING_INVALID', updated_at = ?\n                                WHERE review_id = ? AND session_id = ?\n                                ", (challenge_reason_classification.get('attemptsUsed', 1), now, challenge_review_id, session_id))
                            database.execute("\n                                UPDATE revision_challenges\n                                SET status = 'review_pending', updated_at = ?\n                                WHERE challenge_id = ? AND session_id = ?\n                                ", (now, challenge_id, session_id))
                        raise ApiError(502, 'CHALLENGE_REVIEW_FAILED', "Kimi's judgment did not match the current Stage facts. Retry the message.", retryable=True, details={'task': 'challenge_review', 'failureStage': 'binding', 'failureCode': 'MAP_GROUNDING_INVALID', 'attemptsUsed': challenge_reason_classification.get('attemptsUsed', 1), 'maximumAttempts': 2}) from exception
                    challenge_reason_classification['comparison'] = safe_comparison
                stage_context['challengeReasonClassification'] = challenge_reason_classification
                with connect(immediate=True) as database:
                    app_attr('record_event')(database, session_id, 'challenge_reason_review_resolved', {'messageKey': payload.idempotencyKey, 'baseVersionId': payload.baseVersionId, 'challengeId': hypotheses.get('challengeId') or challenge_context.get('challengeId'), 'sourceUserTurnId': user_turn_id, 'state': 'reason_review', 'attemptsUsed': challenge_reason_classification.get('attemptsUsed', 1)}, utc_now())
                    now = utc_now()
                    database.execute("\n                        UPDATE challenge_reason_reviews\n                        SET status = 'resolved', attempts_used = ?, failure_code = NULL,\n                            result_json = ?, updated_at = ?\n                        WHERE review_id = ? AND session_id = ?\n                        ", (challenge_reason_classification.get('attemptsUsed', 1), dump_json(challenge_reason_classification), now, challenge_review_id, session_id))
    revision_state, revision_brief = classify_revision_request(context['conversation'], context['stageContext'])
    if payload.action == 'execute_revision':
        revision_state = 'authorized'
        revision_brief = ' '.join((str(source_offer.get(field) or '').strip() for field in ('summary', 'rationale'))).strip()
    elif payload.action == 'challenge_revision':
        revision_state, revision_brief = ('not_request', None)
    elif payload.action == 'alternative_revision':
        revision_state = 'proposal_requested'
        revision_brief = ' '.join((str(source_offer.get(field) or '').strip() for field in ('summary', 'rationale'))).strip()
        context['stageContext']['authorizedRevisionBrief'] = revision_brief
    elif challenge_choice_resolution is not None:
        active_disagreement = context['stageContext'].get('activeDisagreement') or {}
        accepted_reason = str(active_disagreement.get('acceptedReason') or '').strip()
        proposal_summary = str(active_disagreement.get('proposalSummary') or '').strip()
        if challenge_choice_resolution == 'ai':
            challenge_source_id = (context['stageContext'].get('challengeContext') or {}).get('sourceTurnId')
            challenge_id = active_disagreement.get('challengeId')
            if challenge_id:
                with connect() as database:
                    challenge_row = database.execute('SELECT source_proposal_turn_id FROM revision_challenges\n                        WHERE session_id = ? AND challenge_id = ? AND base_version_id = ?', (session_id, challenge_id, payload.baseVersionId)).fetchone()
                if challenge_row is None:
                    raise ApiError(409, 'CHALLENGE_STALE', 'The challenged proposal is no longer available.')
                challenge_source_id = challenge_row['source_proposal_turn_id']
            else:
                raise ApiError(409, 'CHALLENGE_STALE', 'The active challenge has no source record.')
            if not challenge_source_id:
                raise ApiError(409, 'INVALID_CARD_SOURCE', 'The challenged proposal has no source card.')
            if challenge_source_id:
                with connect() as database:
                    challenge_source = database.execute('SELECT version_id, guidance_json, proposal_binding_json\n                        FROM conversation_turns WHERE session_id = ? AND id = ?', (session_id, challenge_source_id)).fetchone()
                if challenge_source is None or challenge_source['version_id'] != payload.baseVersionId:
                    raise ApiError(409, 'PROPOSAL_STALE', 'The challenged proposal belongs to another Stage.')
                challenged_binding = load_json(challenge_source['proposal_binding_json']) or {}
                challenged_brief = challenged_binding.get('executionBrief')
                if not isinstance(challenged_brief, dict) or not challenged_brief.get('requiredTransitions'):
                    raise ApiError(409, 'INVALID_CARD_SOURCE', 'The challenged proposal has no exact tile changes.')
                if challenged_binding.get('mapFingerprint') != map_fingerprint(context['rows']):
                    raise ApiError(409, 'PROPOSAL_STALE', 'The challenged map has changed.')
                try:
                    challenged_brief = validate_execution_brief(challenged_brief, context['rows'], context['stageContext'].get('entityBindings'))
                except ValueError as error:
                    raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The challenged tile changes are no longer applicable.') from error
                source_guidance = load_json(challenge_source['guidance_json']) or {}
                context['stageContext'].update({'explicitAction': 'execute_revision', 'deterministicExactExecution': True, 'proposalBindingFrozen': True, 'authorizedExecutionBrief': challenged_brief, 'sourceProposalOffer': source_guidance.get('proposalOffer') or {}})
            revision_brief = proposal_summary
        else:
            revision_brief = f"Use the designer's accepted reason as the primary direction: {accepted_reason}".strip()
        revision_state = 'proposal_requested'
        context['stageContext']['revisionRouting'] = 'proposal'
        context['stageContext']['proposalState'] = 'ready_with_explicit_binding'
        context['stageContext']['authorizedRevisionBrief'] = revision_brief
        context['stageContext']['activeDisagreement'] = None
    elif challenge_reason_classification is not None:
        revision_state, revision_brief = ('not_request', None)
    elif context['stageContext'].get('proposalState', '').startswith('ready_'):
        revision_state = 'proposal_requested'
        revision_brief = str(context['stageContext'].get('authorizedRevisionBrief') or revision_brief or '').strip()
    context['stageContext']['answeredQuestionIds'] = list(question_answer_review.get('answeredQuestionIds') or [])
    context['stageContext']['answeredVisibleQuestion'] = bool(context['stageContext']['answeredQuestionIds'])
    context['stageContext']['revisionRequestState'] = revision_state
    evidence_context = context['stageContext']
    source_workflow = (evidence_context.get('sourceProposalOffer') or {}).get('revisionWorkflow') or {}
    source_record = source_workflow.get('requirementRecord') or {}
    source_ids = {x['sourceTurnId'] for x in source_record.get('requirements') or []}
    source_ids.update((x['sourceTurnId'] for x in source_record.get('exactTransitions') or []))
    source_ids.update((x for x in (evidence_context.get('requirementSourceCardId'), evidence_context.get('requirementReasonTurnId'), payload.sourceTurnId) if x))
    projection = evidence_context.get('revisionDesignContext') or {}
    for memory in (projection.get('activeGoals') or []) + (projection.get('activeConstraints') or []):
        if memory.get('sourceTurnId'):
            source_ids.add(memory['sourceTurnId'])
    with connect() as evidence_database:
        prior_sources = evidence_database.execute('SELECT id, content, role, request_id, version_id FROM conversation_turns WHERE session_id = ? ORDER BY sequence_number', (session_id,)).fetchall()
    evidence_by_id = {row['id']: row for row in prior_sources}
    paired_keys = {(evidence_by_id[source_id]['request_id'], evidence_by_id[source_id]['version_id']) for source_id in source_ids if source_id in evidence_by_id and evidence_by_id[source_id]['role'] == 'assistant' and evidence_by_id[source_id]['request_id']}
    prior_sources = [{'id': row['id'], 'content': row['content']} for row in prior_sources if row['role'] == 'user' and (row['id'] in source_ids or (row['request_id'], row['version_id']) in paired_keys)]
    current_sources = (evidence_context.get('proposalDiscovery') or {}).get('userTurns') or evidence_context.get('requirementUserTurns') or []
    if challenge_choice_resolution is not None:
        current_sources = [item for item in current_sources if item['id'] != user_turn_id]
    evidence_context['requirementUserTurns'] = list({x['id']: x for x in [*prior_sources, *current_sources]}.values())
    with connect() as evidence_database:
        cached_reviews = evidence_database.execute("SELECT payload_json FROM audit_events WHERE session_id = ?\n               AND event_type = 'requirements_verified' AND json_extract(payload_json, '$.baseVersionId') = ?\n               ORDER BY created_at DESC LIMIT 8", (session_id, payload.baseVersionId)).fetchall()
    evidence_context['verifiedRequirementCache'] = [load_json(row['payload_json']) for row in cached_reviews]
    evidence_context['_reliabilityState'] = {}
    if payload.action in REVISION_CARD_ACTIONS and (not source_record):
        evidence_context.pop('requirementPolicyVersion', None)
    proposal_branch = bool(payload.action in REVISION_CARD_ACTIONS or payload.requestProposal or context['stageContext'].get('activeDisagreement') or context['stageContext'].get('proposalDiscovery') or (context['stageContext'].get('revisionRouting') in {'needs_clarification', 'proposal', 'proposal_conservative', 'proposal_blocked'}))
    context['stageContext']['conversationBranch'] = 'proposal' if proposal_branch else 'ordinary'
    trace_reply('routing', 'passed', branch='conversation_only' if context['stageContext'].get('conversationOnly') else 'disagreement' if context['stageContext'].get('activeDisagreement') else context['stageContext']['conversationBranch'])
    message_deadline = challenge_deadline or message_started_at + LLM_INTERNAL_DEADLINE_SECONDS
    revision_failure = None
    recovery_execution = _verified_recovery_suggestion_execution(proposal_discovery, content, context['rows'], language, request.state.request_id) if proposal_discovery else None
    if payload.action == 'challenge_revision':
        execution = LLMExecutionResult('请说明你质疑的理由。' if language == 'zh-CN' else 'Please explain why you challenge this proposal.', 0, request.state.request_id, model='server', guidance={'proposalOffer': None, 'disagreement': None, 'uiCues': []})
    elif challenge_reason_classification is not None:
        comparison = str(challenge_reason_classification.get('comparison') or '').strip()
        if not comparison:
            comparison = '我还无法判断这个理由具体指向哪处玩法变化。请再说明你担心的效果。' if language == 'zh-CN' else 'I cannot yet tell which play effect concerns you. Please explain that effect more concretely.'
        execution = LLMExecutionResult(comparison, challenge_reason_classification.get('attemptsUsed', 1), request.state.request_id, model='kimi-k2.6', guidance={'proposalOffer': None, 'disagreement': None, 'uiCues': []})
    elif context['stageContext'].get('revisionRouting') == 'proposal_cancelled':
        execution = _proposal_cancelled_execution(language, request.state.request_id)
    elif recovery_execution is not None:
        execution = recovery_execution
    elif context['stageContext'].get('revisionRouting') == 'protected_request':
        try:
            permission_message = protected_request_message(turn_understanding, language)
        except ValueError as exception:
            raise ApiError(502, 'MODEL_RESPONSE_INVALID', 'The requested edit could not be reliably verified. Retry this message.', retryable=True) from exception
        execution = LLMExecutionResult(permission_message, 0, request.state.request_id, model='server-permission-guard', guidance={'proposalOffer': None, 'disagreement': None, 'followUpQuestion': None, 'uiCues': []})
    elif revision_state == 'relaxation_confirmed':
        execution = _relaxed_revision_suggestion_execution(context['stageContext'], language, request.state.request_id)
    else:
        if proposal_discovery and context['stageContext'].get('revisionRouting') in {'proposal', 'proposal_conservative'}:
            with connect(immediate=True) as database:
                duplicate_planning = database.execute("\n                    SELECT 1 FROM audit_events\n                    WHERE session_id = ? AND event_type = 'proposal_discovery_progress'\n                      AND json_extract(payload_json, '$.messageKey') = ?\n                      AND json_extract(payload_json, '$.status') = 'planning'\n                    LIMIT 1\n                    ", (session_id, payload.idempotencyKey)).fetchone()
                if duplicate_planning is None:
                    app_attr('record_event')(database, session_id, 'proposal_discovery_progress', {'stageId': payload.baseVersionId, 'topicId': proposal_discovery.get('topicId'), 'messageKey': payload.idempotencyKey, 'status': 'planning', 'clarificationQuestionCount': proposal_discovery.get('clarificationQuestionCount', 0)}, utc_now())
        try:
            execution = app_attr('generate_chat_reply')(context['conversation'], context['rows'], request.state.request_id, language=language, solver_metrics=context['validation'], play_summary=context['playSummary'], proposal_validator=lambda proposed_rows: _validate_changed_proposal(context['rows'], proposed_rows), stage_context=context['stageContext'], _deadline=message_deadline)
        except LLMServiceError as exception:
            _persist_verified_requirements(session_id, payload.baseVersionId, context['stageContext'])
            revision_failure = exception
            exception.requirement_policy_version = context['stageContext'].get('requirementPolicyVersion')
            proposal_retryable_failure = bool(proposal_branch and exception.retryable)
            if proposal_retryable_failure:
                execution = None
            elif proposal_discovery:
                execution = _classified_proposal_failure_execution(language=language, request_id=exception.request_id, exception=exception)
            elif revision_state == 'proposal_requested':
                execution = _automatic_proposal_failure_execution(language=language, request_id=exception.request_id, exception=exception)
            elif exception.code in {'REVISION_CONTRACT_INVALID', 'REVISION_CONTRACT_CONFLICT'}:
                execution = _revision_contract_clarification_execution(language=language, request_id=exception.request_id, exception=exception)
            else:
                execution = _proposal_search_failure_execution(session_id=session_id, access_cookie=access_cookie, base_version_id=payload.baseVersionId, idempotency_key=payload.idempotencyKey, revision_state=revision_state, revision_brief=revision_brief, language=language, exception=exception)
            if execution is None:
                ordinary_message_failure = payload.action == 'none' and revision_state == 'not_request'
                if proposal_retryable_failure or not retrying_failed_message or ordinary_message_failure:
                    with connect(immediate=True) as database:
                        active_session = require_active_session(database, session_id, access_cookie)
                        require_current_base(active_session, payload.baseVersionId)
                        app_attr('record_event')(database, session_id, 'message_generation_failed', {'baseVersionId': payload.baseVersionId, 'messageKey': payload.idempotencyKey, 'code': exception.code, 'attemptsUsed': exception.attempts_used, 'requestId': exception.request_id, 'retryable': exception.retryable, 'details': _llm_failure_details(exception), 'retryingExistingUserTurn': retrying_failed_message, 'modelGenerationSucceeded': (context['stageContext'].get('_reliabilityState') or {}).get('modelGenerationSucceeded'), 'proposalRequested': payload.requestProposal or revision_state == 'proposal_requested' or payload.action in {'execute_revision', 'alternative_revision'} or bool(challenge_choice_resolution)}, utc_now())
                        if proposal_discovery:
                            app_attr('record_event')(database, session_id, 'proposal_discovery_progress', {'stageId': payload.baseVersionId, 'topicId': proposal_discovery.get('topicId'), 'messageKey': payload.idempotencyKey, 'status': 'retry_pending', 'retryable': True, 'failureCode': exception.code, 'clarificationQuestionCount': proposal_discovery.get('clarificationQuestionCount', 0)}, utc_now())
                    raise
                execution = app_attr('_retry_exhausted_execution')(language, exception)
                with connect(immediate=True) as database:
                    active_session = require_active_session(database, session_id, access_cookie)
                    require_current_base(active_session, payload.baseVersionId)
                    app_attr('record_event')(database, session_id, 'message_retry_exhausted', {'baseVersionId': payload.baseVersionId, 'messageKey': payload.idempotencyKey, 'code': exception.code, 'attemptsUsed': exception.attempts_used}, utc_now())
    _persist_verified_requirements(session_id, payload.baseVersionId, context['stageContext'])
    if joint_ordinary_review:
        question_answer_review = context['stageContext'].get('_reliabilityState', {}).get('questionAnswerReview') or question_answer_review
        execution = replace(execution, proposal_diagnostics={**(execution.proposal_diagnostics or {}), 'questionProgressUpdated': bool(question_answer_review.get('answeredQuestionIds'))})
    proposal_recovery = bool((execution.proposal_diagnostics or {}).get('verifiedFailureAnalysis'))
    if proposal_recovery and challenge_choice_resolution is not None:
        challenge_choice_resolution = None
    if payload.action == 'challenge_revision':
        execution = _sanitize_challenge_execution(execution, source_offer, language, source_binding, payload.sourceTurnId)
    elif challenge_reason_classification is not None:
        execution = _enforce_challenge_reason_execution(execution, challenge_reason_classification, content, context['stageContext'].get('challengeContext') or {}, active_disagreement, language)
    if payload.action != 'challenge_revision' and payload.action != 'execute_revision' and (payload.action != 'continue_challenge' or challenge_choice_resolution == 'user') and (execution.proposed_rows is not None):
        if revision_state == 'proposal_requested':
            execution = _materialize_verified_automatic_offer(execution, context['rows'], language, context['stageContext'])
        else:
            execution = replace(execution, proposed_rows=None, revision_plan={}, revision_contract={}, revision_operations=[], proposal_diagnostics={})
    if payload.action == 'alternative_revision' and (not proposal_recovery):
        execution = _ensure_alternative_revision_execution(execution, source_offer, language, source_binding)
    if payload.action in {'none', 'continue_challenge'} and active_disagreement.get('subject') == 'ai_revision_challenge' and (active_disagreement.get('phase') == 'choice_pending'):
        execution = _enforce_challenge_choice_execution(execution, active_disagreement, challenge_choice_resolution, language)
    if challenge_choice_resolution == 'user':
        offer = (execution.guidance or {}).get('proposalOffer')
        if not isinstance(offer, dict) or not offer.get('executionBrief'):
            raise ApiError(502, 'PROPOSAL_SEARCH_EXHAUSTED', 'No verified new proposal could be produced from this reason; the map was not changed.', retryable=True)
        with connect() as database:
            source_row = database.execute('SELECT turn.proposal_binding_json FROM revision_challenges AS challenge\n                JOIN conversation_turns AS turn ON turn.id = challenge.source_proposal_turn_id\n                WHERE challenge.session_id = ? AND challenge.challenge_id = ?\n                  AND challenge.base_version_id = ?', (session_id, active_disagreement.get('challengeId'), payload.baseVersionId)).fetchone()
        if source_row is None:
            raise ApiError(409, 'CHALLENGE_STALE', 'The challenged proposal is no longer available.')
        original_brief = (load_json(source_row['proposal_binding_json']) or {}).get('executionBrief') or {}

        def transition_set(brief):
            return {(item.get('row'), item.get('column'), item.get('from'), item.get('to')) for item in brief.get('requiredTransitions') or [] if isinstance(item, dict)}
        if transition_set(offer['executionBrief']) == transition_set(original_brief):
            raise ApiError(502, 'PROPOSAL_SEARCH_EXHAUSTED', 'The new direction repeated the challenged proposal; the map was not changed.', retryable=True)
        guidance = dict(execution.guidance or {})
        guidance['proposalOffer'] = {**offer, 'availableActions': ['execute_revision']}
        execution = replace(execution, guidance=guidance)
    if payload.action == 'none' and context['stageContext'].get('source') == 'human_edit':
        execution = _normalize_manual_edit_review_execution(execution, context['stageContext'])
    if payload.action == 'none' and user_map_claims.get('conflicts'):
        correction = format_map_claim_correction(user_map_claims['conflicts'], language)
        visible_body = str(execution.assistant_message or '').strip()
        if correction and correction not in visible_body:
            visible_body = '\n\n'.join((part for part in (correction, visible_body) if part))
        corrected_guidance = dict(execution.guidance or {})
        corrected_guidance.update({'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': [], 'coordinateLinks': []})
        corrected_guidance.pop('designContextPatch', None)
        corrected_guidance.pop('designContextPatchError', None)
        execution = replace(execution, assistant_message=visible_body, proposed_rows=None, revision_plan={}, revision_contract={}, revision_operations=[], proposal_binding={}, proposal_diagnostics={**(execution.proposal_diagnostics or {}), 'category': 'map_claim_conflict'}, guidance=corrected_guidance)
    execution = _mark_new_discussion_guidance(execution, context['stageContext'])
    execution = _mark_proposal_discovery_guidance(execution, context['stageContext'])
    execution = _enforce_human_edit_disagreement_followup(execution, context['stageContext'])
    execution_guidance = dict(execution.guidance or {})
    if context['stageContext'].get('conversationOnly'):
        active = context['stageContext'].get('activeDisagreement')
        execution_guidance = {'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'proposalOffer': None, 'uiCues': [], 'coordinateLinks': execution_guidance.get('coordinateLinks') or [], 'proposalDiscovery': execution_guidance.get('proposalDiscovery'), 'disagreement': {**active, 'displayCard': False} if active else None}
        execution = replace(execution, guidance=execution_guidance, proposed_rows=None, revision_plan={}, revision_contract={}, revision_operations=[], proposal_binding={})
    active_disagreement_card = isinstance(execution_guidance.get('disagreement'), dict) and execution_guidance['disagreement'].get('status') == 'active'
    candidate_intent = str(execution_guidance.get('intentHypothesis') or '').strip()
    candidate_claims = execution_guidance.get('_intentSemanticClaims') or [] if candidate_intent else []
    intent_allowed = bool(context['stageContext'].get('conversationBranch') == 'ordinary' and execution_guidance.get('_intentDecision', {}).get('classification') == 'candidate')
    if candidate_intent and (not intent_allowed or payload.requestProposal or payload.action != 'none' or execution_guidance.get('proposalOffer') or active_disagreement_card):
        execution_guidance['intentHypothesis'] = None
        execution_guidance['intentConfidence'] = None
        execution = replace(execution, guidance=execution_guidance)
        candidate_intent = ''
    if candidate_intent:
        confirmed_intents = [{'hypothesisId': str(item.get('id') or '').strip(), 'statement': str(item.get('statement') or '').strip(), 'semanticClaims': item.get('semanticClaims') or []} for item in context['stageContext'].get('designContext', {}).get('intentHypotheses', []) if item.get('status') == 'confirmed' and str(item.get('id') or '').strip() and str(item.get('statement') or '').strip()]
        if confirmed_intents:
            deterministic_conflicts = []
            for item in confirmed_intents:
                pair = _deterministic_intent_conflict(candidate_claims, item.get('semanticClaims'))
                if pair:
                    deterministic_conflicts.append((item, pair))
            if deterministic_conflicts:
                selected, conflict_pair = deterministic_conflicts[-1]
                intent_review = {'verdict': 'conflict', 'explanation': _deterministic_conflict_explanation(conflict_pair, language), 'conflictingHypothesisIds': [selected['hypothesisId']], 'supersedesHypothesisIds': [], 'reviewSource': 'deterministic', 'conflictClaims': conflict_pair}
            elif any((_intent_claims_equivalent(candidate_claims, item.get('semanticClaims')) for item in confirmed_intents)):
                execution_guidance['intentHypothesis'] = None
                execution_guidance['intentConfidence'] = None
                execution_guidance.pop('_intentSemanticClaims', None)
                execution = replace(execution, guidance=execution_guidance)
                candidate_intent = ''
                intent_review = None
            else:
                candidate_review = execution_guidance.get('_intentCandidateReview') or {}
                intent_review = {'verdict': candidate_review.get('relation', 'unclear'), 'explanation': candidate_review.get('explanation'), 'conflictingHypothesisIds': candidate_review.get('conflictingHypothesisIds', []), 'supersedesHypothesisIds': [], 'reviewSource': 'kimi', 'reviewVersion': candidate_review.get('reviewVersion'), 'reviewedActiveClaims': candidate_review.get('reviewedActiveClaims', [])}
            if intent_review and intent_review.get('verdict') == 'conflict':
                conflicting_ids = set(intent_review.get('conflictingHypothesisIds') or [])
                latest_conflict = next((item for item in reversed(confirmed_intents) if item['hypothesisId'] in conflicting_ids), None)
                if latest_conflict is None:
                    raise LLMServiceError('MODEL_RESPONSE_INVALID', 'Kimi did not bind the intent conflict to confirmed memory.', request.state.request_id, True, 1, 502)
                explanation = str(intent_review.get('explanation') or '').strip()
                if explanation and explanation not in str(execution.assistant_message or ''):
                    execution = replace(execution, assistant_message='\n\n'.join((part for part in (str(execution.assistant_message or '').strip(), explanation) if part)))
                execution_guidance['_intentReviewState'] = {'interactionMode': 'conflict_choice', 'reviewOutcome': 'conflict', 'reviewExplanation': explanation, 'existingHypothesisId': latest_conflict['hypothesisId'], 'reviewSource': intent_review.get('reviewSource', 'kimi'), 'conflictClaims': intent_review.get('conflictClaims'), 'newEvidenceText': content}
                execution = replace(execution, guidance=execution_guidance)
    execution = _bind_execution_to_stage(execution, payload.baseVersionId, context['rows'], content, language, context['stageContext'].get('entityBindings'))
    state_transition_deferred = bool((execution.guidance or {}).get('_stateTransitionDeferred'))
    design_context_guidance = _design_context_guidance(execution.guidance)
    execution = replace(execution, guidance=_public_guidance(execution.guidance))
    response.headers['X-LLM-Attempts-Used'] = str(execution.attempts_used)
    trace_reply('commit', 'started')
    trace_proposal('commit', 'commit', 'started')
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        require_current_base(session, payload.baseVersionId)
        current = get_current_version(database, session)
        captured_binding = context.get('commitBinding')
        current_binding = _message_commit_binding(database, session_id, payload.baseVersionId)
        source_sequence = database.execute('SELECT sequence_number FROM conversation_turns WHERE id = ? AND session_id = ?', (user_turn_id, session_id)).fetchone()
        guard_id = getattr(request.state, 'interaction_guard_id', None)
        guard_receipt = database.execute("SELECT id FROM audit_events WHERE session_id = ? AND event_type = 'interaction_message_guard'\n            AND json_extract(payload_json, '$.guardId') = ? AND json_extract(payload_json, '$.status') = 'started' ORDER BY id LIMIT 1", (session_id, guard_id)).fetchone() if guard_id else None
        superseded = bool(source_sequence and source_sequence['sequence_number'] != current_binding['latestUserSequence'])
        newer_receipt = bool(guard_receipt and guard_receipt['id'] != current_binding['latestMessageReceipt'])
        if captured_binding and (captured_binding != current_binding or superseded or newer_receipt):
            if execution.proposed_rows is not None or (execution.guidance or {}).get('proposalOffer') or (execution.guidance or {}).get('proposalDiscovery') or (payload.action != 'none'):
                raise ApiError(409, 'PROPOSAL_STALE', 'The dialogue evidence or interaction state changed. Send a request based on the current dialogue.', retryable=False, details={'failureStage': 'context_binding', 'failureKind': 'state_or_evidence_change'})
            trace_reply('commit', 'preserved', 'CONCURRENT_CHANGE', cardPresent=False)
            body = _strip_unverified_state_claims(execution.assistant_message)
            if not body.strip():
                raise ApiError(502, 'MODEL_RESPONSE_INVALID', 'No reliable answer remains after the context changed. Retry the original message.', retryable=True)
            canonical_active = load_design_context(database, session_id, payload.baseVersionId).get('activeDisagreement')
            safe_guidance = {'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'proposalOffer': None, 'followUpQuestion': None, 'uiCues': [], 'disagreement': None}
            design_context_guidance = dict(safe_guidance)
            allow_progress = False
            question_answer_review = {'results': [], 'answeredQuestionIds': []}
            state_transition_deferred = bool(canonical_active and canonical_active.get('subject') == 'human_edit' and (canonical_active.get('status') == 'active'))
            execution = replace(execution, assistant_message=body, guidance=safe_guidance, proposal_diagnostics={**(execution.proposal_diagnostics or {}), 'fullQualityPassed': False, 'componentChecks': [{'component': 'context_binding', 'status': 'preserved', 'code': 'CONCURRENT_CHANGE'}]})
        requested_disagreement = design_context_guidance.get('disagreement')
        initial_disagreement = context['stageContext'].get('activeDisagreement')
        if initial_disagreement and initial_disagreement.get('subject') == 'human_edit':
            current_disagreement = load_design_context(database, session_id, payload.baseVersionId).get('activeDisagreement')
            if component_recovery.signature(initial_disagreement) != component_recovery.signature(current_disagreement):
                design_context_guidance.pop('disagreement', None)
                guidance = dict(execution.guidance or {})
                guidance.pop('disagreement', None)
                execution = replace(execution, guidance=guidance, assistant_message=_strip_unverified_state_claims(execution.assistant_message))
                state_transition_deferred = False
                if not execution.assistant_message.strip():
                    raise ApiError(502, 'MODEL_RESPONSE_INVALID', 'No reliable answer remains after the state changed. Retry the message.', retryable=True)
        if isinstance(requested_disagreement, dict) and requested_disagreement.get('subject') == 'human_edit':
            identity = component_recovery.disagreement_identity(database, session_id, payload.baseVersionId, initial_disagreement)
            if identity or not initial_disagreement or initial_disagreement.get('subject') != 'human_edit':
                requested_disagreement['disagreementId'] = identity or uuid.uuid4().hex
        if design_context_guidance.get('intentHypothesis'):
            pending_conflict = _active_intent_conflict_choice(database, session_id, payload.baseVersionId)
            if pending_conflict is not None:
                raise ApiError(409, 'INTENT_CONFLICT_PENDING', 'Retain one of the conflicting inclinations before adding another intent card.')
            review_state = design_context_guidance.get('_intentReviewState') or {}
            if review_state.get('interactionMode') == 'conflict_choice':
                current_context = load_design_context(database, session_id, payload.baseVersionId)
                existing_id = review_state.get('existingHypothesisId')
                existing = next((item for item in current_context.get('intentHypotheses', []) if item.get('id') == existing_id), None)
                if existing is None or existing.get('status') != 'confirmed':
                    raise ApiError(409, 'INTENT_CONTEXT_CHANGED', 'Confirmed intent memory changed while the conflict was being reviewed. Retry the message.', retryable=True)
        if payload.action in REVISION_CARD_ACTIONS:
            source_turn, source_offer = _source_revision_offer(database, session_id, payload.baseVersionId, payload.sourceTurnId)
            _require_offer_action(source_offer, payload.action)
            source_binding = _preflight_proposal(database, session_id, session, source_turn, source_offer, load_json(current['rows_json']))
        proposal_validation = None
        proposal_id = None
        if revision_failure is not None and payload.action == 'execute_revision':
            app_attr('record_event')(database, session_id, 'proposal_generation_exhausted', {'stageId': payload.baseVersionId, 'sourceTurnId': payload.sourceTurnId, 'mapFingerprint': map_fingerprint(load_json(current['rows_json'])), 'reason': revision_failure.code, 'attempts': revision_failure.attempts_used, 'deterministicFallback': bool((getattr(revision_failure, 'proposal_diagnostics', {}) or {}).get('deterministicFallback')), 'providerStatus': getattr(revision_failure, 'provider_status', None), 'providerErrorType': getattr(revision_failure, 'provider_error_type', '') or None, 'providerErrorCode': getattr(revision_failure, 'provider_error_code', '') or None, 'providerParam': getattr(revision_failure, 'provider_param', '') or None, 'providerMessage': getattr(revision_failure, 'provider_message', '') or None}, utc_now())
        if execution.proposed_rows is not None:
            _execute_revision_candidate_or_api_error(load_json(current['rows_json']), execution)
            proposal_validation = _solve_changed_proposal_or_api_error(load_json(current['rows_json']), execution.proposed_rows)
            current_rows = load_json(current['rows_json'])
            verified_summary = summarize_verified_diff(current_rows, proposal_validation.rows, language)
            if context['stageContext'].get('proposalBindingMode') == 'conservative':
                prefix = '这是基于当前布局补全对象关系的一份最小改动候选，仍等待你审查。' if language == 'zh-CN' else 'This is a minimal-change candidate whose object binding was completed from the current layout; it remains yours to review.'
                verified_summary = f'{prefix}\n\n{verified_summary}'
            pre_review_offer = (execution.guidance or {}).get('proposalOffer') if isinstance(execution.guidance, dict) else None
            preserved_binding = dict(execution.proposal_binding or {})
            entity_bindings = context['stageContext'].get('entityBindings')
            actual_transitions = [
                {'row': row_index + 1, 'column': column_index + 1, 'from': before, 'to': after}
                for row_index, (before_row, after_row) in enumerate(zip(current_rows, proposal_validation.rows))
                for column_index, (before, after) in enumerate(zip(before_row, after_row))
                if before != after
            ]
            execution_brief = (
                preserved_binding.get('executionBrief')
                or ((pre_review_offer or {}).get('executionBrief') if isinstance(pre_review_offer, dict) else None)
                or ((source_binding or {}).get('executionBrief') if isinstance(source_binding, dict) else None)
                or (context['stageContext'].get('authorizedExecutionBrief') if isinstance(context['stageContext'].get('authorizedExecutionBrief'), dict) else None)
            )
            if not isinstance(execution_brief, dict) or not execution_brief.get('requiredTransitions'):
                strategies = (execution.revision_contract or {}).get('strategies') or []
                selected_idx = (execution.proposal_diagnostics or {}).get('selectedStrategyIndex')
                selected_strategy = strategies[selected_idx - 1] if isinstance(selected_idx, int) and 1 <= selected_idx <= len(strategies) else (strategies[0] if strategies else {})
                if actual_transitions:
                    raw_brief = {
                        'schemaVersion': 1,
                        'effect': selected_strategy.get('effect') or 'adjust_internal_walls',
                        'anchors': list(selected_strategy.get('anchorEntities') or []),
                        'focus': selected_strategy.get('focus'),
                        'requiredTransitions': actual_transitions,
                        'allowedOperators': list(selected_strategy.get('allowedOperators') or []),
                        'preserve': list(selected_strategy.get('preserve') or []),
                        'playObjective': selected_strategy.get('playObjective'),
                    }
                    try:
                        execution_brief = validate_execution_brief(raw_brief, current_rows, entity_bindings)
                    except ValueError:
                        execution_brief = {'schemaVersion': 1, 'requiredTransitions': actual_transitions}
            revision_workflow = (
                preserved_binding.get('revisionWorkflow')
                or ((execution.revision_contract or {}).get('revisionWorkflow') if isinstance(execution.revision_contract, dict) else None)
                or ((pre_review_offer or {}).get('revisionWorkflow') if isinstance(pre_review_offer, dict) else None)
                or ((source_binding or {}).get('revisionWorkflow') if isinstance(source_binding, dict) else None)
                or ((source_offer or {}).get('revisionWorkflow') if isinstance(source_offer, dict) else None)
                or {}
            )
            preserved_binding = {
                **preserved_binding,
                'baseVersionId': payload.baseVersionId,
                'mapFingerprint': map_fingerprint(current_rows),
                'entityBindingFingerprint': (entity_bindings or {}).get('bindingFingerprint'),
                **({'executionBrief': execution_brief} if isinstance(execution_brief, dict) else {}),
                'revisionWorkflow': dict(revision_workflow) if isinstance(revision_workflow, dict) else {},
                **({'sourceOfferTurnId': payload.sourceTurnId} if payload.action == 'execute_revision' and payload.sourceTurnId else {}),
                'status': preserved_binding.get('status') or 'active',
            }
            execution = replace(
                execution,
                assistant_message=_verified_proposal_message(language),
                modification_summary=verified_summary,
                guidance=_proposal_review_guidance(execution.guidance, language),
                proposal_binding=preserved_binding,
            )
        existing = database.execute("\n            SELECT * FROM conversation_turns\n            WHERE session_id = ? AND request_id = ? AND role = 'assistant'\n            ", (session_id, payload.idempotencyKey)).fetchone()
        if existing is None:
            assistant_turn_id = app_attr('insert_turn')(database, session, 'assistant', execution.assistant_message, payload.baseVersionId, payload.idempotencyKey, execution)
            _record_reply_outcome(database, session_id, payload.idempotencyKey, payload.baseVersionId, execution, 'message', proposal_requested=payload.requestProposal or revision_state == 'proposal_requested' or payload.action in {'execute_revision', 'alternative_revision'} or bool(challenge_choice_resolution) or proposal_recovery or bool((execution.guidance or {}).get('proposalOffer')))
            if payload.action == 'challenge_revision':
                hypotheses = (execution.proposal_diagnostics or {}).get('challengeHypotheses')
                if isinstance(hypotheses, dict):
                    challenge_id = hypotheses.get('challengeId')
                    now = utc_now()
                    database.execute("\n                        INSERT INTO revision_challenges(\n                            challenge_id, session_id, base_version_id,\n                            source_proposal_turn_id, challenge_turn_id, status,\n                            current_reason_turn_id, created_at, updated_at\n                        ) VALUES (?, ?, ?, ?, ?, 'awaiting_reason', NULL, ?, ?)\n                        ON CONFLICT(challenge_id) DO UPDATE SET\n                            status = 'awaiting_reason',\n                            challenge_turn_id = excluded.challenge_turn_id,\n                            updated_at = excluded.updated_at\n                        ", (challenge_id, session_id, payload.baseVersionId, payload.sourceTurnId, assistant_turn_id, now, now))
                    app_attr('record_event')(database, session_id, 'proposal_challenge_hypotheses_recorded', {'messageKey': payload.idempotencyKey, 'challengeTurnId': assistant_turn_id, 'sourceTurnId': payload.sourceTurnId, 'baseVersionId': payload.baseVersionId, 'proposalSummary': (source_offer or {}).get('summary'), 'challengeId': challenge_id, 'hypotheses': hypotheses.get('items') or [], 'primary': hypotheses.get('primary'), 'secondary': hypotheses.get('secondary'), 'state': 'awaiting_reason'}, now)
                challenged_binding = dict(source_binding or {})
                if source_turn is not None and challenged_binding:
                    challenged_binding['status'] = 'challenged'
                    database.execute('UPDATE conversation_turns SET proposal_binding_json = ? WHERE id = ?', (dump_json(challenged_binding), source_turn['id']))
            if payload.action == 'continue_challenge':
                challenge_state = (execution.guidance or {}).get('challengeState') or {}
                challenge_status = str(challenge_state.get('status') or 'reason_review')
                challenge_id = challenge_state.get('challengeId')
                if challenge_id:
                    database.execute('\n                        UPDATE revision_challenges\n                        SET status = ?, updated_at = ?\n                        WHERE challenge_id = ? AND session_id = ?\n                        ', (challenge_status, utc_now(), challenge_id, session_id))
            discovery_marker = (execution.guidance or {}).get('proposalDiscovery')
            if isinstance(discovery_marker, dict):
                app_attr('record_event')(database, session_id, 'proposal_discovery_progress', {'stageId': payload.baseVersionId, 'turnId': assistant_turn_id, 'topicId': discovery_marker.get('topicId'), 'status': discovery_marker.get('status'), 'clarificationQuestionCount': discovery_marker.get('clarificationQuestionCount'), 'optionPolicyVersion': (discovery_marker.get('optionSet') or {}).get('schemaVersion'), 'optionCount': len((discovery_marker.get('optionSet') or {}).get('options') or []), 'optionSelection': (discovery_marker.get('optionDecision') or {}).get('selection'), 'clarificationQuestionKey': discovery_marker.get('clarificationQuestionKey'), 'clarificationCountBefore': discovery_marker.get('clarificationCountBefore'), 'clarificationCountAfter': discovery_marker.get('clarificationCountAfter'), 'groundingSentencesDropped': (execution.proposal_diagnostics or {}).get('groundingSentencesDropped', 0), 'clarificationRecoveryMode': discovery_marker.get('clarificationRecoveryMode') or (execution.proposal_diagnostics or {}).get('clarificationRecoveryMode'), 'clarificationAuthor': discovery_marker.get('clarificationAuthor') or (execution.proposal_diagnostics or {}).get('clarificationAuthor'), 'clarificationTargetDimension': (execution.proposal_diagnostics or {}).get('clarificationTargetDimension'), 'clarificationQuestionValidated': (execution.proposal_diagnostics or {}).get('clarificationQuestionValidated'), 'clarificationQuestionRepairAttempts': (execution.proposal_diagnostics or {}).get('clarificationQuestionRepairAttempts'), 'clarificationFallbackReason': (execution.proposal_diagnostics or {}).get('clarificationFallbackReason'), 'proposalTopicContinued': discovery_marker.get('proposalTopicContinued'), 'routing': context['stageContext'].get('revisionRouting'), 'proposalState': context['stageContext'].get('proposalState'), 'bindingMode': context['stageContext'].get('proposalBindingMode'), 'enteredRevisionPlan': bool(execution.revision_plan), 'failureClass': (execution.proposal_diagnostics or {}).get('failureClass') or (execution.proposal_diagnostics or {}).get('category'), 'failureCode': (execution.proposal_diagnostics or {}).get('failureCode'), 'failureEnvelope': (execution.proposal_diagnostics or {}).get('failureEnvelope'), 'recoverySuggestions': (execution.proposal_diagnostics or {}).get('recoverySuggestions') or [], 'contractPreflightFailures': (execution.proposal_diagnostics or {}).get('contractPreflightFailures'), 'proposalAnswers': proposal_discovery.get('answers') or [], 'proposalSupplements': proposal_discovery.get('supplements') or [], 'providerStatus': (execution.proposal_diagnostics or {}).get('providerStatus'), 'providerErrorType': (execution.proposal_diagnostics or {}).get('providerErrorType'), 'providerErrorCode': (execution.proposal_diagnostics or {}).get('providerErrorCode'), 'providerParam': (execution.proposal_diagnostics or {}).get('providerParam'), 'providerMessage': (execution.proposal_diagnostics or {}).get('providerMessage')}, utc_now())
            if execution.proposal_binding:
                app_attr('record_event')(database, session_id, 'proposal_binding_created', {'stageId': payload.baseVersionId, 'sourceTurnId': assistant_turn_id, 'mapFingerprint': execution.proposal_binding.get('mapFingerprint'), 'status': execution.proposal_binding.get('status'), 'transitions': [{key: item.get(key) for key in ('row', 'column', 'from', 'to', 'anchorEntity')} for item in (execution.proposal_binding.get('executionBrief', {}).get('requiredTransitions', []) if isinstance(execution.proposal_binding.get('executionBrief'), dict) else [])], 'transitionCount': len(execution.proposal_binding.get('executionBrief', {}).get('requiredTransitions', []) if isinstance(execution.proposal_binding.get('executionBrief'), dict) else [])}, utc_now())
            if execution.proposal_diagnostics.get('source') == 'deterministic_contract':
                app_attr('record_event')(database, session_id, 'revision_plan_repaired', {'stageId': payload.baseVersionId, 'sourceTurnId': payload.sourceTurnId, 'attempts': execution.attempts_used, 'deterministicFallback': True, 'reason': 'exact_transition_contract'}, utc_now())
            if execution.revision_plan:
                revision_contract = execution.revision_contract or {}
                revision_workflow = revision_contract.get('revisionWorkflow') or {}
                if revision_workflow:
                    semantic_results = ((execution.proposal_diagnostics or {}).get('mechanismEvidence') or {}).get('semanticConstraintResults') or []
                    app_attr('record_event')(database, session_id, 'revision_workflow_v2_evaluated', {'baseVersionId': payload.baseVersionId, 'sourceTurnId': assistant_turn_id, 'messageKey': payload.idempotencyKey, 'workflow': revision_workflow, 'constraintResults': semantic_results, 'mode': revision_workflow.get('mode'), 'candidateAccepted': bool(execution.proposed_rows is not None or (execution.proposal_diagnostics or {}).get('candidateFrozen'))}, utc_now())
                record_agent_handoff(database, session_id, 'co_creation_chat', 'co_creation_revision', 'revision_plan', {'baseVersionId': payload.baseVersionId, 'authorizedBrief': revision_contract.get('authorizedBrief'), 'revisionPlan': execution.revision_plan, 'executionContract': revision_contract, 'attempts': execution.proposal_diagnostics.get('planAttempts')}, evidence=[{'type': 'explicit_revision_authorization', 'status': 'confirmed'}], status='confirmed')
            if execution.revision_plan and execution.proposed_rows is not None:
                app_attr('record_event')(database, session_id, 'proposal_search_completed', {'baseVersionId': payload.baseVersionId, 'messageKey': payload.idempotencyKey, 'sourceTurnId': payload.sourceTurnId, 'revisionPlan': execution.revision_plan, 'executionContract': execution.revision_contract, 'operations': execution.revision_operations, 'search': execution.proposal_diagnostics}, utc_now())
            if execution.proposed_rows is not None:
                proposal_id = uuid.uuid4().hex
                database.execute("\n                    INSERT INTO change_proposals(\n                        id, session_id, base_version_id, proposed_rows_json,\n                        summary, diff_json, validation_json, status,\n                        assistant_turn_id, idempotency_key, created_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)\n                    ", (proposal_id, session_id, payload.baseVersionId, dump_json(list(proposal_validation.rows)), execution.modification_summary, dump_json(describe_diff(current_rows, proposal_validation.rows)), dump_json(proposal_validation.as_dict()), assistant_turn_id, payload.idempotencyKey, utc_now()))
                if payload.action == 'execute_revision' and source_turn is not None:
                    consumed_binding = dict(source_binding or {})
                    if consumed_binding:
                        consumed_binding['status'] = 'stale'
                        database.execute('UPDATE conversation_turns SET proposal_binding_json = ? WHERE id = ?', (dump_json(consumed_binding), source_turn['id']))
                record_agent_handoff(database, session_id, 'co_creation_revision', 'deterministic_validator', 'revision_operations', {'proposalId': proposal_id, 'baseVersionId': payload.baseVersionId, 'revisionPlan': execution.revision_plan, 'executionContract': execution.revision_contract, 'strategyIndex': execution.proposal_diagnostics.get('selectedStrategyIndex'), 'operations': execution.revision_operations, 'changedCellCount': execution.proposal_diagnostics.get('changedCellCount'), 'candidateCount': execution.proposal_diagnostics.get('candidateCount'), 'attempts': execution.proposal_diagnostics.get('modifierAttempts')}, evidence=[{'type': 'deterministic_solver', 'status': 'passed', 'validation': proposal_validation.as_dict()}, {'type': 'verified_diff', 'diff': describe_diff(current_rows, proposal_validation.rows)}], status='confirmed')
            elif execution.revision_plan:
                diagnostics = execution.proposal_diagnostics or {}
                record_agent_handoff(database, session_id, 'co_creation_revision', 'deterministic_validator', 'revision_operations', {'baseVersionId': payload.baseVersionId, 'revisionPlan': execution.revision_plan, 'executionContract': execution.revision_contract, 'operations': execution.revision_operations, 'retryAttempts': diagnostics.get('attempts') or diagnostics.get('modifierAttempts'), 'failureReason': diagnostics.get('modifierFailure')}, evidence=[{'type': 'deterministic_validation', 'status': 'rejected', 'reason': diagnostics.get('modifierFailure') or 'No executable candidate satisfied the contract.'}], status='rejected')
            record_intent_hypothesis(database, session_id, payload.baseVersionId, assistant_turn_id, design_context_guidance, 'conversation_reply', proposal_id=proposal_id)
            _record_disagreement_event(database, session_id, payload.baseVersionId, assistant_turn_id, execution.guidance)
            if question_answer_review.get('results'):
                app_attr('record_event')(database, session_id, 'question_answer_reviewed', {'versionId': payload.baseVersionId, 'userTurnId': user_turn_id, 'results': question_answer_review['results'], 'model': 'kimi-k2.6'}, utc_now())
            _update_design_context_from_turn(database, session_id, payload.baseVersionId, content, assistant_turn_id, design_context_guidance, assistant_content=execution.assistant_message, allow_progress=allow_progress, answered_question_ids=question_answer_review.get('answeredQuestionIds'), answer_turn_id=user_turn_id, proposal_discovery=(execution.guidance or {}).get('proposalDiscovery'))
            if state_transition_deferred:
                component_recovery.enqueue(database, session_id, payload.baseVersionId, user_turn_id, assistant_turn_id, payload.idempotencyKey)
        trace_proposal('commit', 'commit', 'passed')
        trace_reply('commit', 'passed', assistantTurnId=assistant_turn_id, cardPresent=bool((execution.guidance or {}).get('intentHypothesis')), answeredCount=len(question_answer_review.get('answeredQuestionIds') or []))
        session_payload = serialize_session(database, session_id)
    app_attr('synchronize_turn_with_online_match')(session_id, payload.idempotencyKey)
    return session_payload

@contextmanager
def message_request_lock(session_id, idempotency_key):
    key = (session_id, idempotency_key)
    with _message_locks_guard:
        entry = _message_locks.get(key)
        if entry is None:
            entry = {'lock': Lock(), 'users': 0, 'error': None}
            _message_locks[key] = entry
        is_leader = entry['users'] == 0
        entry['users'] += 1
    entry['lock'].acquire()
    try:
        if not is_leader and entry['error'] is not None:
            raise entry['error']
        yield
    except Exception as error:
        if is_leader:
            entry['error'] = error
        raise
    finally:
        entry['lock'].release()
        with _message_locks_guard:
            entry['users'] -= 1
            if entry['users'] == 0:
                _message_locks.pop(key, None)

@router.post('/api/sessions/{session_id}/proposals/{proposal_id}/decision')
def decide_proposal(session_id: str, proposal_id: str, payload: ProposalDecisionRequest, request: Request, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    identity = {'requestId': request.state.request_id, 'messageKey': payload.idempotencyKey, 'baseVersionId': payload.baseVersionId, 'proposalId': proposal_id, 'task': 'proposal_decision'}
    diagnostic = ProposalDiagnostic(identity, _log_llm_event)
    token = CURRENT_PROPOSAL_DIAGNOSTIC.set(diagnostic)
    try:
        result = _decide_proposal_impl(session_id, proposal_id, payload, request, access_cookie)
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
            with connect(immediate=True) as database:
                require_browser_session(database, session_id, access_cookie)
                exists = database.execute('SELECT 1 FROM change_proposals WHERE session_id=? AND id=?', (session_id, proposal_id)).fetchone()
                if exists:
                    app_attr('record_event')(database, session_id, 'reply_pipeline_diagnostic', {**identity, 'proposalDiagnostic': diagnostic.snapshot()}, utc_now())
        except (ApiError, sqlite3.Error):
            pass
        CURRENT_PROPOSAL_DIAGNOSTIC.reset(token)

@proposal_phase('acceptance', 'proposal')
def _decide_proposal_impl(session_id, proposal_id, payload, request, access_cookie):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    decision_rewrite = None
    decision_rewrite_language = 'zh-CN'
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        existing_decision = database.execute('\n            SELECT d.id, d.proposal_id, d.decision_type, d.version_id, p.base_version_id\n            FROM designer_decisions AS d\n            LEFT JOIN change_proposals AS p ON p.id = d.proposal_id\n            WHERE d.session_id = ? AND d.idempotency_key = ?\n            ', (session_id, payload.idempotencyKey)).fetchone()
        if existing_decision is not None:
            if (
                existing_decision['proposal_id'] != proposal_id
                or existing_decision['decision_type'] != payload.decision
                or (existing_decision['base_version_id'] is not None and existing_decision['base_version_id'] != payload.baseVersionId)
            ):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'This idempotencyKey belongs to another proposal decision.')
            new_version_id = existing_decision['version_id']
            session_payload = serialize_session(database, session_id)
        else:
            require_current_base(session, payload.baseVersionId)
            proposal = database.execute('\n                SELECT * FROM change_proposals WHERE id = ? AND session_id = ?\n                ', (proposal_id, session_id)).fetchone()
            if proposal is None:
                raise ApiError(404, 'PROPOSAL_NOT_FOUND', 'The proposal was not found.')
            if proposal['status'] != 'pending':
                raise ApiError(409, 'PROPOSAL_ALREADY_DECIDED', 'The proposal was already decided.')
            if proposal['base_version_id'] != session['current_version_id']:
                raise ApiError(409, 'VERSION_CONFLICT', 'The proposal belongs to an older Stage.')
            proposal_turn = database.execute('SELECT guidance_json, proposal_binding_json FROM conversation_turns WHERE id = ?', (proposal['assistant_turn_id'],)).fetchone()
            proposal_guidance = load_json(proposal_turn['guidance_json']) if proposal_turn is not None else None
            proposal_binding = load_json(proposal_turn['proposal_binding_json']) if proposal_turn is not None and proposal_turn['proposal_binding_json'] else None
            now = utc_now()
            new_version_id = None
            verified_summary = None
            if payload.decision == 'accept':
                decision_rewrite_language = session['language']
                proposed_rows = load_json(proposal['proposed_rows_json'])
                current = get_current_version(database, session)
                current_rows = load_json(current['rows_json'])
                validation = _solve_changed_proposal_or_api_error(current_rows, proposed_rows)
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
                    source_turn_row = database.execute('SELECT guidance_json, proposal_binding_json FROM conversation_turns WHERE id = ?', (source_offer_turn_id,)).fetchone()
                    if source_turn_row is not None:
                        src_guidance = load_json(source_turn_row['guidance_json']) or {}
                        src_offer = src_guidance.get('proposalOffer') if isinstance(src_guidance, dict) else None
                        src_binding = load_json(source_turn_row['proposal_binding_json']) if source_turn_row['proposal_binding_json'] else {}
                        if not proposal_brief:
                            proposal_brief = (src_offer.get('executionBrief') if isinstance(src_offer, dict) else None) or (src_binding.get('executionBrief') if isinstance(src_binding, dict) else None)
                        if not revision_workflow:
                            revision_workflow = (
                                (src_offer.get('revisionWorkflow') if isinstance(src_offer, dict) and isinstance(src_offer.get('revisionWorkflow'), dict) else None)
                                or (src_binding.get('revisionWorkflow') if isinstance(src_binding, dict) and isinstance(src_binding.get('revisionWorkflow'), dict) else None)
                                or {}
                            )
                actual_transitions = [{'row': row_index + 1, 'column': column_index + 1, 'from': before, 'to': after} for row_index, (before_row, after_row) in enumerate(zip(current_rows, validation.rows)) for column_index, (before, after) in enumerate(zip(before_row, after_row)) if before != after]
                frozen_transitions = proposal_brief.get('requiredTransitions') or [] if isinstance(proposal_brief, dict) else []
                if frozen_transitions:
                    actual_signature = {(item['row'], item['column'], item['from'], item['to']) for item in actual_transitions}
                    frozen_signature = {(item.get('row'), item.get('column'), item.get('from'), item.get('to')) for item in frozen_transitions}
                    if actual_signature != frozen_signature:
                        raise ApiError(409, 'EXECUTION_REPLAY_MISMATCH', 'The proposal no longer matches its frozen tile transitions.', False, {'task': 'execution_replay', 'failureStage': 'postcondition', 'failureCode': 'FROZEN_DIFF_MISMATCH', 'retryable': False, 'safeReason': 'The actual map diff differs from the reviewed purple card.'})
                try:
                    constraint_results = validate_semantic_constraints(current_rows, validation.rows, revision_workflow)
                    from llm_client import validate_frozen_requirements
                    requirement_outcomes = validate_frozen_requirements(current_rows, validation.rows, revision_workflow, load_json(current['validation_json']), validation, load_entity_bindings(database, session_id, current['id']))
                except SemanticConstraintError as exception:
                    raise ApiError(422, 'SEMANTIC_POSTCONDITION_FAILED', 'The proposal no longer satisfies the authorized semantic contract.', False, {'task': 'postcondition', 'failureStage': 'semantic_contract', 'failureCode': 'SEMANTIC_CONSTRAINT_FAILED', 'retryable': False, 'safeReason': 'One or more explicit design requirements were not satisfied.', 'constraintResults': exception.results}) from exception
                new_version_id = _insert_version(database, session, validation, 'llm_accepted', verified_summary, 'proposal:' + payload.idempotencyKey, current, entity_transitions=proposal_brief.get('requiredTransitions', []) if isinstance(proposal_brief, dict) else [])
                child_context = add_confirmed_decision(load_design_context(database, session_id, new_version_id), proposal['summary'], clean_optional(payload.reason, 2000) or 'Designer accepted the validated map proposal.', new_version_id, proposal['assistant_turn_id'], proposal_id)
                for requirement in (revision_workflow.get('requirementRecord') or {}).get('requirements') or []:
                    if requirement['strength'] == 'preference':
                        continue
                    field, collection = ('constraint', 'designConstraints') if requirement['strength'] == 'invariant' else ('goal', 'userGoals')
                    if not any((item.get('id') == requirement['requirementId'] for item in child_context[collection])):
                        child_context[collection].append({'id': requirement['requirementId'], field: requirement['statement'], 'authority': 'explicit', 'status': 'active', 'sourceStageId': current['id'], 'sourceTurnId': requirement['sourceTurnId'], 'confidence': 1.0})
                save_design_context(database, new_version_id, child_context)
                execution_outcome = {'revisionWorkflowId': revision_workflow.get('revisionWorkflowId'), 'sourceProposalTurnId': proposal['assistant_turn_id'], 'actualTransitions': actual_transitions, 'constraintResults': constraint_results, 'goalOutcomes': requirement_outcomes, 'softEvidence': [], 'validation': validation.as_dict(), 'beforeFingerprint': map_fingerprint(current_rows), 'afterFingerprint': map_fingerprint(validation.rows), 'status': 'verified'}
                app_attr('record_event')(database, session_id, 'revision_execution_outcome', {'versionId': new_version_id, **execution_outcome}, now)
                app_attr('record_event')(database, session_id, 'design_context_updated', {'versionId': new_version_id, 'sourceTurnId': proposal['assistant_turn_id'], 'kind': 'confirmed_decision', 'proposalId': proposal_id}, now)
                decision_rewrite = {'artifactId': proposal_id, 'versionId': new_version_id, 'canonical': proposal['summary'], 'evidence': [clean_optional(payload.reason, 2000) or '', verified_summary or '']}
            database.execute('\n                UPDATE change_proposals\n                SET status = ?, decided_at = ?, summary = COALESCE(?, summary)\n                WHERE id = ?\n                ', ('accepted' if payload.decision == 'accept' else 'rejected', now, verified_summary, proposal_id))
            _insert_decision(database, session_id, new_version_id, proposal_id, payload.decision, clean_optional(payload.reason, 2000) or '', payload.idempotencyKey)
            record_intent_evidence(database, session_id, new_version_id or proposal['base_version_id'], 'proposal_' + payload.decision, proposal['summary'], turn_id=proposal['assistant_turn_id'], proposal_id=proposal_id, details={'decision': payload.decision, 'reason': clean_optional(payload.reason, 2000) or '', 'baseVersionId': proposal['base_version_id']}, created_at=now)
            if payload.decision == 'reject':
                current_context = load_design_context(database, session_id, proposal['base_version_id'])
                current_context = add_rejected_decision(current_context, proposal['summary'], clean_optional(payload.reason, 2000) or '', proposal['base_version_id'], proposal['assistant_turn_id'], proposal_id)
                save_design_context(database, proposal['base_version_id'], current_context)
                app_attr('record_event')(database, session_id, 'design_context_updated', {'versionId': proposal['base_version_id'], 'sourceTurnId': proposal['assistant_turn_id'], 'kind': 'rejected_decision', 'proposalId': proposal_id, 'hasReason': bool(clean_optional(payload.reason, 2000))}, now)
                record_intent_hypothesis(database, session_id, proposal['base_version_id'], proposal['assistant_turn_id'], proposal_guidance, 'proposal_review', status='rejected', proposal_id=proposal_id, reason=clean_optional(payload.reason, 2000) or '')
            session_payload = serialize_session(database, session_id)
    if decision_rewrite is not None:
        try:
            rewritten = app_attr('rewrite_intent_progress')('confirmed_decision', decision_rewrite['canonical'], decision_rewrite['evidence'], decision_rewrite_language, 'decision-' + payload.idempotencyKey, _deadline=min(time.monotonic() + 10.0, request.state.llm_deadline - 20.0))
        except (LLMServiceError, TypeError, ValueError, KeyError):
            rewritten = {'detailedText': None, 'summaryText': decision_rewrite['canonical'], 'model': 'deterministic-fallback'}
        with connect(immediate=True) as database:
            app_attr('record_event')(database, session_id, 'intent_progress_rewrite', {'artifactType': 'confirmed_decision', 'artifactId': decision_rewrite['artifactId'], 'versionId': decision_rewrite['versionId'], 'language': decision_rewrite_language, **rewritten}, utc_now())
            session_payload = serialize_session(database, session_id)
    if new_version_id:
        app_attr('synchronize_version_with_online_match')(session_id, new_version_id, 'stage')
    synchronize_dashboard_proposal_node(session_id, proposal_id)
    return session_payload
