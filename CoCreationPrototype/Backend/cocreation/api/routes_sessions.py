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
from .config_models import ApiError, AssessmentRequest, BrowserAccessRequest, CreateSessionRequest, DemoSessionRequest, DraftRegenerationCompleteRequest, DraftRegenerationFailRequest, DraftRegenerationRequest, FinalizeRequest, IntentionRequest, LanguageRequest, LegacyChatRequest, MAX_INTENTION_LENGTH, PLAY_TICKET_LIFETIME, PUBLIC_BASE_URL, PlayAttemptRequest, PlayBootstrapRequest, PlayMetricsRequest, RestoreRequest, SAMPLE_LEGEND, SAMPLE_ROWS, SESSION_COOKIE_NAME, TOKEN_SECRET, TranslationRequest, VersionRequest, _solve_or_api_error, _validate_identifier, _validate_metrics, build_launch_url, build_play_url, build_return_url, clean_optional, derive_token, hash_token, iso_time, parse_time, require_active_session, require_browser_session, require_current_base, session_deadline_expired, start_deadline_if_missing, token_matches, utc_now
from .context_and_discovery import _design_context_guidance, _public_guidance, _record_disagreement_event, _update_design_context_from_turn, build_llm_context, record_intent_evidence, record_intent_hypothesis
from .online_sync_and_dashboard import _displayed_cards, synchronize_manual_edit_review_nodes, synchronize_opening_with_online_match
from .proposal_and_challenge import _mark_initial_human_edit_disagreement_card, _mark_new_discussion_guidance, _record_reply_outcome
from .session_persistence_helpers import _create_session_record, _expire_draft_regeneration, _insert_decision, _insert_version, _pending_assistant_translations, _promote_draft_preview, _require_draft_preview, _translated_guidance, expire_interrupted_attempts, validate_legacy_conversation


@router.get('/health')
def health():
    return {'status': 'ok'}

@router.get('/ready')
def ready():
    database_ready = database_path().parent.exists()
    return {'status': 'ready' if database_ready else 'not_ready', 'database': str(database_path()), 'tokenSecretConfigured': TOKEN_SECRET != b'development-only-change-before-deployment'}

@router.get('/api/sample')
def get_sample():
    validation = validate_and_solve(SAMPLE_ROWS)
    return {'width': WIDTH, 'height': HEIGHT, 'rows': list(SAMPLE_ROWS), 'legend': dict(SAMPLE_LEGEND), 'validation': validation.as_dict()}

@router.post('/api/chat')
def legacy_chat(payload: LegacyChatRequest, request: Request, response: Response):
    messages = [message.model_dump() for message in payload.messages]
    validate_legacy_conversation(messages)
    validation = validate_and_solve(SAMPLE_ROWS)
    execution = app_attr('generate_chat_reply')(messages, SAMPLE_ROWS, request.state.request_id, solver_metrics=validation.as_dict(), stage_context={'deferRevisionExecution': True})
    response.headers['X-LLM-Attempts-Used'] = str(execution.attempts_used)
    return {'assistantMessage': execution.assistant_message, 'requestId': execution.request_id}

@router.post('/api/sessions')
def create_session(payload: CreateSessionRequest):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    session_id, _version_id, bootstrap_token, integration_token = _create_session_record(rows=payload.rows, initial_draft_method=payload.initialDraftMethod, language=payload.language, idempotency_key=payload.idempotencyKey, match_id=payload.matchId, player_number=payload.playerNumber)
    return {'sessionId': session_id, 'launchUrl': build_launch_url(session_id, bootstrap_token), 'integrationToken': integration_token}

@router.post('/api/demo-sessions')
def create_demo_session(payload: DemoSessionRequest):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    generated = app_attr('generate_demo_level')()
    session_id, _version_id, bootstrap_token, _integration_token = _create_session_record(rows=list(generated.rows), initial_draft_method='algorithm_demo', language=payload.language, idempotency_key=payload.idempotencyKey, demo_mode=True, initial_summary='Algorithm-generated demo map', generation_seed=generated.seed, generation_attempts=generated.attempts, generation_summary=generated.generation_summary)
    return {'sessionId': session_id, 'launchUrl': build_launch_url(session_id, bootstrap_token, mode='demo')}

@router.post('/api/sessions/{session_id}/draft-regenerations')
def request_draft_regeneration(session_id: str, payload: DraftRegenerationRequest, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session)
        if session['draft_regeneration_key'] == payload.idempotencyKey:
            return serialize_session(database, session_id)
        if session['draft_regeneration_status'] in {'pending', 'claimed'}:
            raise ApiError(409, 'DRAFT_REGENERATION_ACTIVE', 'Draft regeneration is already running.')
        if bool(session['demo_mode']):
            generated = app_attr('generate_demo_level')()
            validation = _solve_or_api_error(list(generated.rows))
            request_id = uuid.uuid4().hex
            now = utc_now()
            database.execute("UPDATE design_sessions\n                   SET draft_rows_json = ?, draft_validation_json = ?,\n                       draft_generation = draft_generation + 1,\n                       draft_regeneration_status = 'completed',\n                       draft_regeneration_request_id = ?, draft_regeneration_key = ?,\n                       draft_regeneration_requested_at = ?, draft_regeneration_updated_at = ?,\n                       draft_regeneration_failure_code = NULL, updated_at = ?\n                   WHERE id = ?", (dump_json(list(validation.rows)), dump_json(validation.as_dict()), request_id, payload.idempotencyKey, now, now, now, session_id))
            app_attr('record_event')(database, session_id, 'draft_regeneration_completed', {'requestId': request_id, 'generation': int(session['draft_generation'] or 1) + 1, 'mode': 'algorithm_demo'}, now)
            return serialize_session(database, session_id)
        request_id = uuid.uuid4().hex
        now = utc_now()
        database.execute("UPDATE design_sessions\n               SET draft_regeneration_status = 'pending',\n                   draft_regeneration_request_id = ?, draft_regeneration_key = ?,\n                   draft_regeneration_requested_at = ?, draft_regeneration_updated_at = ?,\n                   draft_regeneration_failure_code = NULL, updated_at = ?\n               WHERE id = ?", (request_id, payload.idempotencyKey, now, now, now, session_id))
        app_attr('record_event')(database, session_id, 'draft_regeneration_requested', {'requestId': request_id, 'generation': int(session['draft_generation'] or 1) + 1, 'mode': 'unity'}, now)
        return serialize_session(database, session_id)

@router.post('/api/sessions/{session_id}/draft-regenerations/{request_id}/cancel')
def cancel_draft_regeneration(session_id: str, request_id: str, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session)
        if session['draft_regeneration_request_id'] != request_id:
            raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
        if session['draft_regeneration_status'] in {'completed', 'failed', 'cancelled', 'timed_out'}:
            return serialize_session(database, session_id)
        if session['draft_regeneration_status'] != 'pending':
            return serialize_session(database, session_id)
        now = utc_now()
        database.execute("UPDATE design_sessions SET draft_regeneration_status = 'cancelled',\n               draft_regeneration_updated_at = ?, draft_regeneration_failure_code = 'unity_unavailable',\n               updated_at = ? WHERE id = ?", (now, now, session_id))
        app_attr('record_event')(database, session_id, 'draft_regeneration_cancelled', {'requestId': request_id}, now)
        return serialize_session(database, session_id)

@router.post('/api/sessions/{session_id}/browser-access')
def exchange_browser_access(session_id: str, payload: BrowserAccessRequest, response: Response):
    with connect(immediate=True) as database:
        session = get_session(database, session_id)
        if session is None or not token_matches(payload.bootstrapToken, session['bootstrap_hash']):
            raise ApiError(401, 'INVALID_BOOTSTRAP_TOKEN', 'The session link is invalid.')
        if session['bootstrap_used_at'] is not None:
            raise ApiError(409, 'BOOTSTRAP_TOKEN_USED', 'The session link was already used.')
        accessed_at = utc_now()
        database.execute('UPDATE design_sessions\n               SET bootstrap_used_at = ?\n               WHERE id = ?', (accessed_at, session_id))
        app_attr('record_event')(database, session_id, 'browser_access_granted', {'deadlineAt': session['deadline_at']}, accessed_at)
    response.set_cookie(SESSION_COOKIE_NAME, derive_token('access', session_id), httponly=True, samesite='lax', secure=PUBLIC_BASE_URL.startswith('https://'), max_age=60 * 60 * 24 * 30, path='/')
    return {'sessionId': session_id}

@router.get('/api/sessions/{session_id}')
def read_session(session_id: str, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME), session_token: str | None=Header(None, alias='X-Session-Token')):
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie or session_token)
        session = _expire_draft_regeneration(database, session)
        expire_interrupted_attempts(database, session_id)
        return serialize_session(database, session['id'])

@router.patch('/api/sessions/{session_id}/language')
def change_language(session_id: str, payload: LanguageRequest, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    promoted_version_id = None
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        if session['status'] != 'active':
            raise ApiError(409, 'SESSION_LOCKED', 'This co-creation session is no longer editable.')
        if session['language_locked_at'] is not None:
            if session['language'] == payload.language:
                result = serialize_session(database, session['id'])
                first = database.execute("SELECT id FROM level_versions WHERE session_id = ? AND stage_number = 1 AND source = 'initial'", (session_id,)).fetchone()
                database.commit()
                if first is not None:
                    app_attr('synchronize_version_with_online_match')(session_id, first['id'], 'first_stage')
                return result
            raise ApiError(409, 'LANGUAGE_LOCKED', 'The session language was already selected and cannot be changed.')
        now = utc_now()
        promoted_version_id, promoted = _promote_draft_preview(database, session, now)
        should_sync_first_stage = promoted and (not bool(session['demo_mode']))
        session = get_session(database, session_id)
        database.execute('UPDATE design_sessions\n               SET language = ?, language_locked_at = ?, updated_at = ?\n               WHERE id = ?', (payload.language, now, now, session_id))
        _deadline_started_at, deadline_at = start_deadline_if_missing(database, session)
        app_attr('record_event')(database, session_id, 'language_selected', {'language': payload.language, 'deadlineAt': deadline_at}, now)
        result = serialize_session(database, session['id'])
    if should_sync_first_stage and promoted_version_id is not None:
        app_attr('synchronize_version_with_online_match')(session_id, promoted_version_id, 'first_stage')
    return result

@router.post('/api/sessions/{session_id}/translations/{language}')
def translate_session_turns(session_id: str, language: Literal['en', 'zh-CN'], payload: TranslationRequest, request: Request, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        pending = _pending_assistant_translations(database, session_id, language, payload.turnIds)
        if not pending:
            return serialize_session(database, session['id'])
    first_error = None
    translation_deadline = request.state.llm_deadline
    completed_turn_ids = []
    failed_turn_ids = []
    for source in pending:
        try:
            execution = app_attr('translate_turns')([source], language, f"{request.state.request_id}:{source['turnId']}", _deadline=translation_deadline)
            translated = execution.translations[0]
        except LLMServiceError as exception:
            first_error = first_error or exception
            failed_turn_ids.append(source['turnId'])
            continue
        with connect(immediate=True) as database:
            session = require_active_session(database, session_id, access_cookie)
            guidance = _translated_guidance(source['guidance'], translated)
            database.execute('\n                INSERT OR IGNORE INTO turn_translations(\n                    id, session_id, turn_id, language, body, guidance_json,\n                    proposal_summary, model, attempts_used, latency_ms, created_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ', (uuid.uuid4().hex, session_id, translated['turnId'], language, translated['body'], dump_json(guidance) if guidance else None, translated['proposalSummary'], execution.model, execution.attempts_used, execution.latency_ms, utc_now()))
            app_attr('record_event')(database, session_id, 'turn_translations_created', {'language': language, 'turnIds': [translated['turnId']], 'model': execution.model}, utc_now())
        completed_turn_ids.append(source['turnId'])
    if first_error is not None:
        first_error.details = {'task': 'translation', 'failureStage': 'translation_validation', 'completedTurnIds': completed_turn_ids, 'pendingTurnIds': failed_turn_ids, 'committed': bool(completed_turn_ids), 'maximumAttempts': 2, 'recoveryState': 'retry_pending'}
        raise first_error
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        return serialize_session(database, session['id'])

@router.post('/api/sessions/{session_id}/versions')
def create_manual_version(session_id: str, payload: VersionRequest, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    validation = _solve_or_api_error(payload.rows)
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        existing = database.execute('\n            SELECT * FROM level_versions\n            WHERE session_id = ? AND idempotency_key = ?\n            ', (session_id, payload.idempotencyKey)).fetchone()
        if existing is not None:
            if load_json(existing['rows_json']) != list(validation.rows) or existing['parent_version_id'] != payload.baseVersionId:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The Stage save key was already used for different input.')
            version_id = existing['id']
        else:
            require_current_base(session, payload.baseVersionId)
            current = get_current_version(database, session)
            current_rows = load_json(current['rows_json'])
            if list(validation.rows) == current_rows:
                raise ApiError(400, 'UNCHANGED_LEVEL', 'Save requires at least one tile change.')
            inherited_design_context = load_design_context(database, session_id, current['id'])
            previous_manual_disagreement = inherited_design_context.get('activeDisagreement') or {}
            closes_manual_disagreement = previous_manual_disagreement.get('status') == 'active' and previous_manual_disagreement.get('subject') == 'human_edit'
            if closes_manual_disagreement:
                inherited_design_context = set_active_disagreement(inherited_design_context, None, current['id'], None)
            version_id = _insert_version(database, session, validation, 'human_edit', clean_optional(payload.summary, 1000) or 'Designer saved an edited stage', payload.idempotencyKey, current, design_context=inherited_design_context)
            if closes_manual_disagreement:
                resolved_disagreement = {**previous_manual_disagreement, 'status': 'resolved', 'resolution': 'user', 'displayCard': False, 'resolutionMode': 'manual_reedit', 'resultVersionId': version_id}
                _record_disagreement_event(database, session_id, current['id'], None, {'disagreement': resolved_disagreement})
            record_intent_evidence(database, session_id, version_id, 'manual_edit', clean_optional(payload.summary, 1000) or 'Designer saved a validated manual edit.', details={'parentVersionId': current['id'], 'diff': describe_diff(current_rows, validation.rows), 'changeSummary': summarize_stage_changes(current_rows, validation.rows), 'verifiedDiff': summarize_verified_diff(current_rows, validation.rows, session['language']), 'validation': validation.as_dict()})
        session_payload = serialize_session(database, session_id)
    app_attr('synchronize_version_with_online_match')(session_id, version_id, 'stage')
    with connect() as database:
        saved_version = get_version(database, session_id, version_id)
        parent_id = saved_version['parent_version_id']
    if parent_id:
        synchronize_manual_edit_review_nodes(session_id, parent_id)
    return session_payload

@router.post('/api/sessions/{session_id}/versions/{version_id}/restore')
def restore_version(session_id: str, version_id: str, payload: RestoreRequest, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        existing_decision = database.execute('\n            SELECT id, version_id, decision_type FROM designer_decisions\n            WHERE session_id = ? AND idempotency_key = ?\n            ', (session_id, payload.idempotencyKey)).fetchone()
        if existing_decision is not None:
            if existing_decision['decision_type'] != 'restore':
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The request key belongs to another decision.')
            restored = get_version(database, session_id, existing_decision['version_id'])
            if restored['parent_version_id'] != version_id:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The request key belongs to another restored Stage.')
            event = database.execute("SELECT payload_json FROM audit_events WHERE session_id = ? AND event_type = 'stage_restored' AND json_extract(payload_json, '$.versionId') = ?", (session_id, restored['id'])).fetchone()
            if event and load_json(event['payload_json'])['baseVersionId'] != payload.baseVersionId:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The request key belongs to another restore request.')
            result = serialize_session(database, session_id)
            database.commit()
            app_attr('synchronize_version_with_online_match')(session_id, restored['id'], 'stage')
            return result
        require_current_base(session, payload.baseVersionId)
        source = get_version(database, session_id, version_id)
        if source is None:
            raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
        validation = _solve_or_api_error(load_json(source['rows_json']))
        current = get_current_version(database, session)
        new_version_id = _insert_version(database, session, validation, 'restored', f"Continued from Stage {source['stage_number']}", 'restore:' + payload.idempotencyKey, current, parent_version_id=source['id'], design_context=load_design_context(database, session_id, source['id']))
        _insert_decision(database, session_id, new_version_id, None, 'restore', '', payload.idempotencyKey)
        record_intent_evidence(database, session_id, new_version_id, 'stage_restore', f"Designer chose to continue from Stage {source['stage_number']}.", details={'sourceVersionId': source['id'], 'replacedVersionId': current['id']})
        app_attr('record_event')(database, session_id, 'stage_restored', {'versionId': new_version_id, 'restoredFromVersionId': source['id'], 'restoredFromStageNumber': source['stage_number'], 'replacedVersionId': current['id'], 'replacedStageNumber': current['stage_number'], 'baseVersionId': payload.baseVersionId}, utc_now())
        result = serialize_session(database, session_id)
    app_attr('synchronize_version_with_online_match')(session_id, new_version_id, 'stage')
    return result

@router.post('/api/sessions/{session_id}/versions/{version_id}/assessments')
def assess_version(session_id: str, version_id: str, payload: AssessmentRequest, request: Request, response: Response, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    existing_opening_sync = None
    existing_session_payload = None
    with connect() as database:
        session = require_active_session(database, session_id, access_cookie)
        version = get_version(database, session_id, version_id)
        if version is None:
            raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
        existing = database.execute('\n            SELECT assessment.id, opening_turn.id AS assistant_turn_id,\n                   opening_turn.content AS assistant_text,\n                   opening_turn.language, opening_turn.guidance_json\n            FROM llm_assessments AS assessment\n            JOIN conversation_turns AS opening_turn\n              ON opening_turn.id = assessment.assistant_turn_id\n            WHERE assessment.version_id = ?\n            ', (version_id,)).fetchone()
        if existing is not None:
            existing_opening_text = existing['assistant_text']
            if version['stage_number'] == 1:
                existing_opening_text = _repair_stage_one_opening_display(existing_opening_text, load_json(version['rows_json']), existing['language'])
            existing_opening_sync = {'assistantTurnId': existing['assistant_turn_id'], 'assistantText': existing_opening_text, 'language': existing['language'], 'cards': _displayed_cards(load_json(existing['guidance_json']))}
            existing_session_payload = serialize_session(database, session_id)
        accepted_opening = database.execute("\n            SELECT decision.id\n            FROM designer_decisions AS decision\n            JOIN change_proposals AS proposal\n              ON proposal.id = decision.proposal_id\n            WHERE decision.session_id = ?\n              AND decision.version_id = ?\n              AND decision.decision_type = 'accept'\n              AND proposal.assistant_turn_id IS NOT NULL\n            LIMIT 1\n            ", (session_id, version_id)).fetchone()
        if existing_session_payload is not None:
            pass
        elif accepted_opening is not None:
            return serialize_session(database, session_id)
        if existing_session_payload is None:
            context = build_llm_context(database, session_id, version)
            session_language = session['language']
            context['stageContext']['discussionCardMode'] = 'disagreement_only'
            context['stageContext']['agentRole'] = 'evaluator'
    if existing_session_payload is not None:
        synchronize_opening_with_online_match(session_id, version_id, existing_opening_sync)
        synchronize_manual_edit_review_nodes(session_id, version_id)
        return existing_session_payload
    try:
        execution = app_attr('generate_stage_assessment')(context['conversation'], context['rows'], session_language, context['validation'], context['playSummary'], request.state.request_id, stage_context=context['stageContext'], _deadline=request.state.llm_deadline)
    except LLMServiceError as exception:
        with connect(immediate=True) as database:
            require_active_session(database, session_id, access_cookie)
            app_attr('record_event')(database, session_id, 'reply_generation_failed', {'messageKey': payload.idempotencyKey, 'baseVersionId': version_id, 'task': 'manual_edit_review' if version['source'] == 'human_edit' else 'stage_opening', 'code': exception.code, 'attemptsUsed': exception.attempts_used, 'reliableBodyDelivered': False, 'fullQualityPassed': False, 'requestId': request.state.request_id}, utc_now())
        raise
    execution = _mark_new_discussion_guidance(execution, context['stageContext'])
    execution = _mark_initial_human_edit_disagreement_card(execution, context['stageContext'])
    review_execution = getattr(execution, 'secondary_execution', None)
    if review_execution is not None:
        review_execution = _mark_new_discussion_guidance(review_execution, context['stageContext'])
        review_execution = _mark_initial_human_edit_disagreement_card(review_execution, context['stageContext'])
    review_metadata = dict(review_execution.guidance.get('manualEditReview') or {}) if review_execution is not None else {}
    design_context_guidance = _design_context_guidance(review_execution.guidance if review_execution is not None else execution.guidance)
    execution = replace(execution, guidance=_public_guidance(execution.guidance))
    if review_execution is not None:
        review_execution = replace(review_execution, guidance=_public_guidance(review_execution.guidance))
    manual_progress_rewrite = None
    if context['stageContext'].get('source') == 'human_edit':
        change_summary = context['stageContext'].get('changeSummary') or {}
        canonical_manual = clean_optional(context['stageContext'].get('summary'), 1200) or 'Designer saved a validated manual edit.'
        try:
            manual_progress_rewrite = app_attr('rewrite_intent_progress')('manual_edit', canonical_manual, [dump_json(change_summary), str(execution.assistant_message or '')[:1200], str(review_execution.assistant_message if review_execution is not None else '')[:1200]], session_language, request.state.request_id, _deadline=min(time.monotonic() + 10.0, request.state.llm_deadline - 20.0))
        except (LLMServiceError, TypeError, ValueError, KeyError):
            manual_progress_rewrite = {'detailedText': str(execution.assistant_message or '')[:1200], 'summaryText': canonical_manual, 'model': 'deterministic-fallback'}
    response.headers['X-LLM-Attempts-Used'] = str(execution.attempts_used)
    opening_sync = None
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        existing = database.execute('\n            SELECT assessment.id, opening_turn.id AS assistant_turn_id,\n                   opening_turn.content AS assistant_text,\n                   opening_turn.language, opening_turn.guidance_json\n            FROM llm_assessments AS assessment\n            JOIN conversation_turns AS opening_turn\n              ON opening_turn.id = assessment.assistant_turn_id\n            WHERE assessment.version_id = ?\n            ', (version_id,)).fetchone()
        if existing is None:
            turn_id = app_attr('insert_turn')(database, session, 'assistant', execution.assistant_message, version_id, execution.request_id, execution)
            review_turn_id = None
            if review_execution is not None:
                review_turn_id = app_attr('insert_turn')(database, session, 'assistant', review_execution.assistant_message, version_id, review_execution.request_id, review_execution)
            assessment_id = uuid.uuid4().hex
            database.execute('\n                INSERT INTO llm_assessments(\n                    id, session_id, version_id, assistant_turn_id,\n                    payload_json, prompt_version, created_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?)\n                ', (assessment_id, session_id, version_id, turn_id, dump_json(execution.assessment), PROMPT_VERSION, utc_now()))
            record_intent_hypothesis(database, session_id, version_id, turn_id, execution.guidance, 'stage_assessment')
            _record_reply_outcome(database, session_id, payload.idempotencyKey, version_id, execution, 'manual_edit_review' if version['source'] == 'human_edit' else 'stage_opening')
            if version['source'] == 'human_edit':
                app_attr('record_event')(database, session_id, 'human_edit_reviewed', {'versionId': version_id, 'turnId': review_turn_id or turn_id, 'openingTurnId': turn_id, 'reviewTurnId': review_turn_id, 'diff': load_json(version['diff_json']), 'changeSummary': context['stageContext'].get('changeSummary'), 'hasWarning': False, 'reviewOutcome': review_metadata.get('outcome'), 'evidenceIds': review_metadata.get('evidenceIds', []), 'decisionSource': review_metadata.get('decisionSource'), 'confirmedDirectionIds': review_metadata.get('confirmedDirectionIds', []), 'effectEvidenceIds': review_metadata.get('effectEvidenceIds', []), 'relation': review_metadata.get('relation'), 'solverDelta': review_metadata.get('solverDelta', {}), 'comparisons': review_metadata.get('comparisons', []), 'disagreement': review_execution.guidance.get('disagreement') if review_execution is not None else None}, utc_now())
                app_attr('record_event')(database, session_id, 'intent_progress_rewrite', {'artifactType': 'manual_edit', 'artifactId': version_id, 'versionId': version_id, 'language': session_language, **(manual_progress_rewrite or {})}, utc_now())
            if review_execution is not None:
                _record_disagreement_event(database, session_id, version_id, review_turn_id, review_execution.guidance)
            _update_design_context_from_turn(database, session_id, version_id, None, review_turn_id or turn_id, design_context_guidance, assistant_content=review_execution.assistant_message if review_execution is not None else execution.assistant_message, allow_progress=False)
            opening_sync = {'assistantTurnId': turn_id, 'assistantText': execution.assistant_message, 'language': session['language'], 'cards': _displayed_cards(execution.guidance)}
        else:
            existing_opening_text = existing['assistant_text']
            if version['stage_number'] == 1:
                existing_opening_text = _repair_stage_one_opening_display(existing_opening_text, load_json(version['rows_json']), existing['language'])
            opening_sync = {'assistantTurnId': existing['assistant_turn_id'], 'assistantText': existing_opening_text, 'language': existing['language'], 'cards': _displayed_cards(load_json(existing['guidance_json']))}
        session_payload = serialize_session(database, session_id)
    if opening_sync is not None:
        synchronize_opening_with_online_match(session_id, version_id, opening_sync)
        synchronize_manual_edit_review_nodes(session_id, version_id)
    return session_payload

@router.post('/api/sessions/{session_id}/versions/{version_id}/play-attempts')
def create_play_attempt(session_id: str, version_id: str, payload: PlayAttemptRequest, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        version = get_version(database, session_id, version_id)
        if version is None:
            raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
        existing = database.execute('\n            SELECT * FROM play_attempts\n            WHERE session_id = ? AND idempotency_key = ?\n            ', (session_id, payload.idempotencyKey)).fetchone()
        if existing is None:
            attempt_id = uuid.uuid4().hex
            ticket = derive_token('play-ticket', attempt_id)
            now_value = datetime.now(timezone.utc)
            issued_at = iso_time(now_value)
            expires_at = iso_time(now_value + PLAY_TICKET_LIFETIME)
            validation = load_json(version['validation_json'])
            database.execute("\n                INSERT INTO play_attempts(\n                    id, session_id, version_id, initial_draft_method,\n                    language, status, ticket_hash, ticket_expires_at,\n                    issued_at, minimum_moves, minimum_pushes, idempotency_key\n                ) VALUES (?, ?, ?, ?, ?, 'issued', ?, ?, ?, ?, ?, ?)\n                ", (attempt_id, session_id, version_id, session['initial_draft_method'], session['language'], hash_token(ticket), expires_at, issued_at, validation.get('solutionSteps', -1), validation.get('solutionPushes', -1), payload.idempotencyKey))
            app_attr('record_event')(database, session_id, 'play_attempt_issued', {'attemptId': attempt_id, 'versionId': version_id}, issued_at)
        else:
            if existing['version_id'] != version_id:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The Play key was already used for another Stage.')
            attempt_id = existing['id']
            ticket = derive_token('play-ticket', attempt_id)
    return {'attemptId': attempt_id, 'playUrl': build_play_url(attempt_id, ticket)}

@router.post('/api/play-attempts/{attempt_id}/bootstrap')
def bootstrap_play_attempt(attempt_id: str, payload: PlayBootstrapRequest):
    with connect(immediate=True) as database:
        attempt = database.execute('SELECT * FROM play_attempts WHERE id = ?', (attempt_id,)).fetchone()
        if attempt is None or not token_matches(payload.ticket, attempt['ticket_hash']):
            raise ApiError(401, 'INVALID_PLAY_TICKET', 'The Play ticket is invalid.')
        if attempt['ticket_used_at'] is not None:
            raise ApiError(409, 'PLAY_TICKET_USED', 'The Play ticket was already used.')
        if parse_time(attempt['ticket_expires_at']) < datetime.now(timezone.utc):
            raise ApiError(410, 'PLAY_TICKET_EXPIRED', 'The Play ticket has expired.')
        version = get_version(database, attempt['session_id'], attempt['version_id'])
        session = get_session(database, attempt['session_id'])
        attempt_token = derive_token('play-attempt', attempt_id)
        now = utc_now()
        database.execute("\n            UPDATE play_attempts\n            SET ticket_used_at = ?, attempt_token_hash = ?, loaded_at = ?, status = 'loaded'\n            WHERE id = ?\n            ", (now, hash_token(attempt_token), now, attempt_id))
        return {'attemptId': attempt_id, 'sessionId': attempt['session_id'], 'versionId': attempt['version_id'], 'rows': load_json(version['rows_json']), 'initialDraftMethod': attempt['initial_draft_method'], 'language': attempt['language'], 'attemptToken': attempt_token, 'returnUrl': build_return_url(session['id'], version['id'])}

@router.post('/api/play-attempts/{attempt_id}/start')
def start_play_attempt(attempt_id: str, payload: PlayMetricsRequest):
    return update_play_attempt(attempt_id, payload, 'started')

@router.post('/api/play-attempts/{attempt_id}/progress')
def progress_play_attempt(attempt_id: str, payload: PlayMetricsRequest):
    return update_play_attempt(attempt_id, payload, 'started')

@router.post('/api/play-attempts/{attempt_id}/complete')
def complete_play_attempt(attempt_id: str, payload: PlayMetricsRequest):
    return update_play_attempt(attempt_id, payload, 'completed')

@router.post('/api/play-attempts/{attempt_id}/abandon')
def abandon_play_attempt(attempt_id: str, payload: PlayMetricsRequest):
    return update_play_attempt(attempt_id, payload, 'abandoned')

@router.post('/api/sessions/{session_id}/finalize')
def finalize_session(session_id: str, payload: FinalizeRequest, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    deadline_stage_version_id = None
    request_signature = {'baseVersionId': payload.baseVersionId, 'rowsHash': hashlib.sha256(dump_json(payload.rows).encode('utf-8')).hexdigest() if payload.rows is not None else None}
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        existing = database.execute('\n            SELECT id, decision_type, version_id FROM designer_decisions\n            WHERE session_id = ? AND idempotency_key = ?\n            ', (session_id, payload.idempotencyKey)).fetchone()
        if existing is None:
            if session['status'] != 'active':
                raise ApiError(409, 'SESSION_LOCKED', 'This co-creation session is no longer editable.')
            deadline_expired = session_deadline_expired(session)
            target_version = get_version(database, session_id, payload.baseVersionId)
            if target_version is None:
                raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
            historical_target = target_version['id'] != session['current_version_id']
            if historical_target and payload.rows is not None:
                raise ApiError(409, 'HISTORICAL_FINALIZE_ROWS_NOT_ALLOWED', 'An unsaved draft can only finalize the current Stage.')
            pending = database.execute("\n                SELECT id FROM change_proposals\n                WHERE session_id = ? AND status = 'pending'\n                ", (session_id,)).fetchall()
            if pending and (not deadline_expired) and (not historical_target):
                raise ApiError(409, 'PENDING_PROPOSAL', 'Decide the pending proposal first.')
            final_version_id = payload.baseVersionId
            if not deadline_expired and not historical_target and payload.rows is not None:
                if list(payload.rows) != load_json(target_version['rows_json']):
                    raise ApiError(409, 'UNSAVED_DRAFT_ROWS', 'Save the edited Stage before finalizing.')
            if deadline_expired and payload.rows is not None:
                validation = _solve_or_api_error(payload.rows)
                current = get_current_version(database, session)
                if list(validation.rows) != load_json(current['rows_json']):
                    final_version_id = _insert_version(database, session, validation, 'human_edit', 'Designer edit saved at the co-creation deadline', 'deadline-finalize:' + payload.idempotencyKey, current)
                    deadline_stage_version_id = final_version_id
                    session = get_session(database, session_id)
            now = utc_now()
            if (historical_target or deadline_expired) and pending:
                pending_proposal_ids = [row['id'] for row in pending]
                database.execute("\n                    UPDATE change_proposals\n                    SET status = 'superseded', decided_at = ?\n                    WHERE session_id = ? AND status = 'pending'\n                    ", (now, session_id))
                event_name = 'pending_proposals_superseded_by_historical_finalization' if historical_target else 'pending_proposals_superseded_by_deadline_finalization'
                app_attr('record_event')(database, session_id, event_name, {'finalVersionId': final_version_id, 'proposalIds': pending_proposal_ids}, now)
            database.execute("\n                UPDATE design_sessions\n                SET status = 'awaiting_intention', final_version_id = ?,\n                    finalized_at = ?, updated_at = ? WHERE id = ?\n                ", (final_version_id, now, now, session_id))
            _insert_decision(database, session_id, final_version_id, None, 'finalize', '', payload.idempotencyKey)
            app_attr('record_event')(database, session_id, 'finalization_request_committed', {'idempotencyKey': payload.idempotencyKey, 'request': request_signature, 'finalVersionId': final_version_id, 'deadlineStageVersionId': deadline_stage_version_id}, now)
            if deadline_expired:
                app_attr('record_event')(database, session_id, 'deadline_finalized', {'versionId': final_version_id}, now)
        else:
            if existing['decision_type'] != 'finalize':
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The request key was already used for another decision.')
            final_version_id = existing['version_id'] or session['final_version_id'] or payload.baseVersionId
            committed = database.execute("SELECT payload_json FROM audit_events WHERE session_id = ?\n                   AND event_type = 'finalization_request_committed'\n                   AND json_extract(payload_json, '$.idempotencyKey') = ?\n                   ORDER BY id DESC LIMIT 1", (session_id, payload.idempotencyKey)).fetchone()
            committed_payload = load_json(committed['payload_json']) if committed else None
            if committed_payload is not None:
                if committed_payload.get('request') != request_signature:
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The finalization key belongs to a different request.')
                final_version_id = committed_payload['finalVersionId']
                deadline_stage_version_id = committed_payload.get('deadlineStageVersionId')
            elif final_version_id != payload.baseVersionId:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The finalization key was already used for another Stage.')
        session_payload = serialize_session(database, session_id)
    if deadline_stage_version_id:
        app_attr('synchronize_version_with_online_match')(session_id, deadline_stage_version_id, 'stage')
    app_attr('synchronize_version_with_online_match')(session_id, final_version_id, 'final')
    return session_payload

@router.post('/api/sessions/{session_id}/intention')
def submit_intention(session_id: str, payload: IntentionRequest, access_cookie: str | None=Cookie(None, alias=SESSION_COOKIE_NAME)):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    content = payload.content.strip()
    if not content or len(content) > MAX_INTENTION_LENGTH:
        raise ApiError(400, 'INVALID_INTENTION', 'The intention response is required.')
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        if session['status'] == 'completed':
            existing = database.execute('SELECT content, idempotency_key FROM designer_intentions WHERE session_id = ?', (session_id,)).fetchone()
            if existing and (existing['idempotency_key'] != payload.idempotencyKey or existing['content'] != content):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The final designer intention has already been submitted.')
            content = existing['content'] if existing else content
        elif session['status'] != 'awaiting_intention':
            raise ApiError(409, 'SESSION_NOT_FINALIZED', 'Finalize a Stage first.')
        else:
            now = utc_now()
            database.execute('\n                INSERT INTO designer_intentions(\n                    id, session_id, content, language, idempotency_key, created_at\n                ) VALUES (?, ?, ?, ?, ?, ?)\n                ', (uuid.uuid4().hex, session_id, content, session['language'], payload.idempotencyKey, now))
            database.execute("\n                UPDATE design_sessions\n                SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?\n                ", (now, now, session_id))
            app_attr('record_event')(database, session_id, 'intention_submitted', {}, now)
        match_id = str(session['match_id'] or '').strip()
        player_number = session['player_number']
        is_demo_session = bool(session['demo_mode'])
    if not is_demo_session:
        app_attr('synchronize_final_intention_with_online_match')(match_id, player_number, content, 'message:' + session_id + ':' + payload.idempotencyKey, session_id)
    with connect() as database:
        return serialize_session(database, session_id)

@router.get('/api/integrations/sessions/{session_id}')
def integration_status(session_id: str, authorization: str | None=Header(None, alias='Authorization')):
    with connect(immediate=True) as database:
        session = _require_integration_session(database, session_id, authorization)
        session = _expire_draft_regeneration(database, session)
        payload = {'sessionId': session_id, 'status': session['status'], 'finalVersionId': session['final_version_id'], 'finalRows': None, 'designerIntention': None, 'draftRegeneration': None}
        if session['current_version_id'] is None and session['draft_regeneration_request_id']:
            payload['draftRegeneration'] = {'requestId': session['draft_regeneration_request_id'], 'status': session['draft_regeneration_status'], 'generation': int(session['draft_generation'] or 1) + (1 if session['draft_regeneration_status'] in {'pending', 'claimed'} else 0), 'requestedAt': session['draft_regeneration_requested_at'], 'updatedAt': session['draft_regeneration_updated_at'], 'failureCode': session['draft_regeneration_failure_code']}
        if session['status'] == 'completed' and session['final_version_id']:
            version = get_version(database, session_id, session['final_version_id'])
            payload['finalRows'] = load_json(version['rows_json'])
            intention = database.execute('SELECT content FROM designer_intentions WHERE session_id = ?', (session_id,)).fetchone()
            payload['designerIntention'] = intention['content'] if intention else None
        return payload

def _require_integration_session(database, session_id, authorization):
    token = ''
    if authorization and authorization.startswith('Bearer '):
        token = authorization[7:].strip()
    session = get_session(database, session_id)
    if session is None or not token_matches(token, session['integration_hash']):
        raise ApiError(401, 'INVALID_INTEGRATION_TOKEN', 'Integration access was denied.')
    return session

@router.post('/api/integrations/sessions/{session_id}/draft-regenerations/{request_id}/claim')
def claim_draft_regeneration(session_id: str, request_id: str, authorization: str | None=Header(None, alias='Authorization')):
    with connect(immediate=True) as database:
        session = _require_integration_session(database, session_id, authorization)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session)
        if session['draft_regeneration_request_id'] != request_id:
            raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
        status = session['draft_regeneration_status']
        if status == 'claimed':
            return {'sessionId': session_id, 'requestId': request_id, 'status': status}
        if status != 'pending':
            raise ApiError(409, 'DRAFT_REGENERATION_NOT_PENDING', 'The regeneration request cannot be claimed.')
        now = utc_now()
        requested_at = session['draft_regeneration_requested_at']
        claim_delay_seconds = None
        if requested_at:
            claim_delay_seconds = max(0, round((parse_time(now) - parse_time(requested_at)).total_seconds(), 3))
        database.execute("UPDATE design_sessions SET draft_regeneration_status = 'claimed',\n               draft_regeneration_updated_at = ?, updated_at = ? WHERE id = ?", (now, now, session_id))
        app_attr('record_event')(database, session_id, 'draft_regeneration_claimed', {'requestId': request_id, 'claimDelaySeconds': claim_delay_seconds}, now)
        return {'sessionId': session_id, 'requestId': request_id, 'status': 'claimed'}

@router.post('/api/integrations/sessions/{session_id}/draft-regenerations/{request_id}/complete')
def complete_draft_regeneration(session_id: str, request_id: str, payload: DraftRegenerationCompleteRequest, authorization: str | None=Header(None, alias='Authorization')):
    with connect(immediate=True) as database:
        session = _require_integration_session(database, session_id, authorization)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session)
        if session['draft_regeneration_request_id'] != request_id:
            raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
        if session['draft_regeneration_status'] == 'completed':
            validation = _solve_or_api_error(payload.rows)
            if load_json(session['draft_rows_json']) != list(validation.rows):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Completed regeneration rows cannot be replaced.')
            return {'sessionId': session_id, 'requestId': request_id, 'status': 'completed'}
        if session['draft_regeneration_status'] != 'claimed':
            raise ApiError(409, 'DRAFT_REGENERATION_NOT_CLAIMED', 'The regeneration request is not claimable.')
    try:
        validation = _solve_or_api_error(payload.rows)
    except ApiError as exception:
        with connect(immediate=True) as database:
            session = _require_integration_session(database, session_id, authorization)
            session = _expire_draft_regeneration(database, session)
            if session['draft_regeneration_request_id'] == request_id and session['draft_regeneration_status'] == 'claimed':
                now = utc_now()
                database.execute("UPDATE design_sessions SET draft_regeneration_status = 'failed',\n                       draft_regeneration_updated_at = ?,\n                       draft_regeneration_failure_code = 'validation_failed', updated_at = ?\n                       WHERE id = ? AND draft_regeneration_request_id = ?\n                         AND draft_regeneration_status = 'claimed'", (now, now, session_id, request_id))
                app_attr('record_event')(database, session_id, 'draft_regeneration_failed', {'requestId': request_id, 'failureCode': 'validation_failed', 'phase': 'claimed'}, now)
        raise ApiError(422, exception.code, exception.message, exception.retryable, exception.details) from exception
    normalized_rows = list(validation.rows)
    with connect(immediate=True) as database:
        session = _require_integration_session(database, session_id, authorization)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session)
        if session['draft_regeneration_request_id'] != request_id:
            raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
        if session['draft_regeneration_status'] == 'completed':
            if load_json(session['draft_rows_json']) != normalized_rows:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Completed regeneration rows cannot be replaced.')
            return {'sessionId': session_id, 'requestId': request_id, 'status': 'completed'}
        if session['draft_regeneration_status'] != 'claimed':
            raise ApiError(409, 'DRAFT_REGENERATION_NOT_CLAIMED', 'The regeneration request is not claimable.')
        now = utc_now()
        claimed_at = session['draft_regeneration_updated_at']
        execution_seconds = None
        if claimed_at:
            execution_seconds = max(0, round((parse_time(now) - parse_time(claimed_at)).total_seconds(), 3))
        generation = int(session['draft_generation'] or 1) + 1
        database.execute("UPDATE design_sessions SET draft_rows_json = ?, draft_validation_json = ?,\n               draft_generation = ?, draft_regeneration_status = 'completed',\n               draft_regeneration_updated_at = ?, draft_regeneration_failure_code = NULL,\n               updated_at = ? WHERE id = ?", (dump_json(normalized_rows), dump_json(validation.as_dict()), generation, now, now, session_id))
        app_attr('record_event')(database, session_id, 'draft_regeneration_completed', {'requestId': request_id, 'generation': generation, 'mode': 'unity', 'executionSeconds': execution_seconds}, now)
        return {'sessionId': session_id, 'requestId': request_id, 'status': 'completed'}

@router.post('/api/integrations/sessions/{session_id}/draft-regenerations/{request_id}/fail')
def fail_draft_regeneration(session_id: str, request_id: str, payload: DraftRegenerationFailRequest, authorization: str | None=Header(None, alias='Authorization')):
    with connect(immediate=True) as database:
        session = _require_integration_session(database, session_id, authorization)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session)
        if session['draft_regeneration_request_id'] != request_id:
            raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
        if session['draft_regeneration_status'] == 'failed':
            return {'sessionId': session_id, 'requestId': request_id, 'status': 'failed'}
        if session['draft_regeneration_status'] not in {'pending', 'claimed'}:
            raise ApiError(409, 'DRAFT_REGENERATION_NOT_ACTIVE', 'The regeneration request is not active.')
        now = utc_now()
        lease_started_at = session['draft_regeneration_requested_at'] if session['draft_regeneration_status'] == 'pending' else session['draft_regeneration_updated_at']
        terminal_delay_seconds = None
        if lease_started_at:
            terminal_delay_seconds = max(0, round((parse_time(now) - parse_time(lease_started_at)).total_seconds(), 3))
        database.execute("UPDATE design_sessions SET draft_regeneration_status = 'failed',\n               draft_regeneration_updated_at = ?, draft_regeneration_failure_code = ?,\n               updated_at = ? WHERE id = ?", (now, payload.failureCode, now, session_id))
        app_attr('record_event')(database, session_id, 'draft_regeneration_failed', {'requestId': request_id, 'failureCode': payload.failureCode, 'phase': session['draft_regeneration_status'], 'phaseDurationSeconds': terminal_delay_seconds}, now)
        return {'sessionId': session_id, 'requestId': request_id, 'status': 'failed'}

def update_play_attempt(attempt_id, payload, requested_status):
    _validate_metrics(payload)
    with connect(immediate=True) as database:
        attempt = database.execute('SELECT * FROM play_attempts WHERE id = ?', (attempt_id,)).fetchone()
        if attempt is None or not token_matches(payload.attemptToken, attempt['attempt_token_hash']):
            raise ApiError(401, 'INVALID_ATTEMPT_TOKEN', 'Play result access was denied.')
        if attempt['status'] in {'completed', 'abandoned', 'interrupted'}:
            return {'attemptId': attempt_id, 'status': attempt['status']}
        now = utc_now()
        first_move_at = attempt['first_move_at']
        if first_move_at is None and payload.moveCount > 0:
            first_move_at = now
        terminal = requested_status in {'completed', 'abandoned'}
        database.execute('\n            UPDATE play_attempts SET\n                status = ?, first_move_at = ?, finished_at = ?,\n                duration_seconds = ?, move_count = ?, push_count = ?,\n                restart_count = ?, minimum_moves = ?, minimum_pushes = ?\n            WHERE id = ?\n            ', (requested_status, first_move_at, now if terminal else None, payload.durationSeconds, payload.moveCount, payload.pushCount, payload.restartCount, payload.minimumMoves, payload.minimumPushes, attempt_id))
        app_attr('record_event')(database, attempt['session_id'], 'play_attempt_' + requested_status, {'attemptId': attempt_id, 'versionId': attempt['version_id'], 'moveCount': payload.moveCount, 'pushCount': payload.pushCount, 'restartCount': payload.restartCount, 'durationSeconds': payload.durationSeconds}, now)
        if terminal:
            record_intent_evidence(database, attempt['session_id'], attempt['version_id'], 'play_' + requested_status, 'Designer play evidence was recorded for the saved Stage.', play_attempt_id=attempt_id, details={'status': requested_status, 'durationSeconds': payload.durationSeconds, 'moveCount': payload.moveCount, 'pushCount': payload.pushCount, 'restartCount': payload.restartCount, 'minimumMoves': payload.minimumMoves, 'minimumPushes': payload.minimumPushes}, created_at=now)
        return {'attemptId': attempt_id, 'status': requested_status}
