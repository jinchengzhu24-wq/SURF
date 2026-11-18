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
from .config_models import ApiError, DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS, DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS, DRAFT_REGENERATION_SWEEP_INTERVAL_SECONDS, INTERRUPTED_AFTER, MAX_MESSAGE_LENGTH, _solve_or_api_error, _validate_identifier, clean_optional, derive_token, hash_token, iso_time, logger, parse_time, record_agent_handoff, utc_now


def _create_session_record(rows, initial_draft_method, language, idempotency_key, match_id=None, player_number=None, demo_mode=False, initial_summary='Initial draft from Unity', generation_seed=None, generation_attempts=None, generation_summary=None):
    validation = _solve_or_api_error(rows)
    created_at = utc_now()
    with connect(immediate=True) as database:
        existing = database.execute('SELECT * FROM design_sessions WHERE creation_key = ?', (idempotency_key,)).fetchone()
        if existing is None:
            session_id = uuid.uuid4().hex
            version_id = None
            access_token = derive_token('access', session_id)
            integration_token = derive_token('integration', session_id)
            bootstrap_token = derive_token('bootstrap', session_id)
            database.execute("\n                INSERT INTO design_sessions(\n                    id, creation_key, access_hash, integration_hash,\n                    bootstrap_hash, demo_mode, match_id, player_number,\n                    initial_draft_method, language, language_locked_at, status,\n                    current_version_id, created_at, updated_at,\n                    draft_rows_json, draft_validation_json, draft_generation\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', NULL, ?, ?, ?, ?, 1)\n                ", (session_id, idempotency_key, hash_token(access_token), hash_token(integration_token), hash_token(bootstrap_token), 1 if demo_mode else 0, clean_optional(match_id, 128), player_number if player_number in (1, 2) else None, initial_draft_method, language, created_at, created_at, dump_json(list(validation.rows)), dump_json(validation.as_dict())))
            event_payload = {'initialDraftMethod': initial_draft_method, 'demoMode': bool(demo_mode)}
            if generation_seed is not None:
                event_payload['generationSeed'] = generation_seed
            if generation_attempts is not None:
                event_payload['generationAttempts'] = generation_attempts
            if generation_summary is not None:
                event_payload['generationSummary'] = generation_summary
            app_attr('record_event')(database, session_id, 'session_created', event_payload, created_at)
            if demo_mode:
                delete_demo_sessions(database, keep_session_id=session_id)
        else:
            session_id = existing['id']
            existing_version = database.execute('\n                SELECT * FROM level_versions\n                WHERE session_id = ? AND stage_number = 1\n                ', (session_id,)).fetchone()
            candidate_rows = load_json(existing['draft_rows_json']) if existing['draft_rows_json'] else None
            same_demo_request = demo_mode and bool(existing['demo_mode']) and (existing['initial_draft_method'] == initial_draft_method)
            same_regular_request = not demo_mode and (not bool(existing['demo_mode'])) and (existing['initial_draft_method'] == initial_draft_method) and (candidate_rows == list(validation.rows) or (existing_version is not None and load_json(existing_version['rows_json']) == list(validation.rows)))
            if not (same_demo_request or same_regular_request):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The session creation key was already used for different input.')
            access_token = derive_token('access', session_id)
            integration_token = derive_token('integration', session_id)
            bootstrap_token = derive_token('bootstrap', session_id)
            version_id = existing_version['id'] if existing_version is not None else None
    return (session_id, version_id, bootstrap_token, integration_token)

def _expire_draft_regeneration(database, session):
    if session is None or session['draft_regeneration_status'] not in {'pending', 'claimed'}:
        return session
    status = session['draft_regeneration_status']
    lease_started_at = session['draft_regeneration_requested_at'] if status == 'pending' else session['draft_regeneration_updated_at']
    if not lease_started_at:
        return session
    age = (datetime.now(timezone.utc) - parse_time(lease_started_at)).total_seconds()
    timeout_seconds = DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS if status == 'pending' else DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS
    if age < timeout_seconds:
        return session
    failure_code = 'unity_unavailable' if status == 'pending' else 'generation_timed_out'
    now = utc_now()
    database.execute("UPDATE design_sessions\n           SET draft_regeneration_status = 'timed_out',\n               draft_regeneration_updated_at = ?,\n               draft_regeneration_failure_code = ?,\n               updated_at = ?\n           WHERE id = ? AND draft_regeneration_status = ?", (now, failure_code, now, session['id'], status))
    app_attr('record_event')(database, session['id'], 'draft_regeneration_timed_out', {'requestId': session['draft_regeneration_request_id'], 'phase': status, 'failureCode': failure_code, 'leaseAgeSeconds': max(0, round(age, 3))}, now)
    return get_session(database, session['id'])

def sweep_expired_draft_regenerations():
    expired_count = 0
    with connect(immediate=True) as database:
        sessions = database.execute("SELECT * FROM design_sessions\n               WHERE draft_regeneration_status IN ('pending', 'claimed')").fetchall()
        for session in sessions:
            previous_status = session['draft_regeneration_status']
            refreshed = _expire_draft_regeneration(database, session)
            if refreshed['draft_regeneration_status'] != previous_status:
                expired_count += 1
    return expired_count

async def _draft_regeneration_sweeper():
    while True:
        try:
            await asyncio.to_thread(sweep_expired_draft_regenerations)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Draft regeneration lease sweep failed.')
        await asyncio.sleep(DRAFT_REGENERATION_SWEEP_INTERVAL_SECONDS)

def _require_draft_preview(database, session):
    if session['status'] != 'active' or session['language_locked_at'] is not None:
        raise ApiError(409, 'SESSION_LOCKED', 'The co-creation session already started.')
    if session['current_version_id'] is not None or not session['draft_rows_json']:
        raise ApiError(409, 'DRAFT_PREVIEW_UNAVAILABLE', 'This session has no mutable draft preview.')

def _promote_draft_preview(database, session, created_at):
    if session['current_version_id'] is not None:
        return (session['current_version_id'], False)
    _require_draft_preview(database, session)
    session = _expire_draft_regeneration(database, session)
    if session['draft_regeneration_status'] in {'pending', 'claimed'}:
        raise ApiError(409, 'DRAFT_REGENERATION_ACTIVE', 'Wait for draft regeneration to finish.')
    rows = load_json(session['draft_rows_json'])
    validation = _solve_or_api_error(rows)
    version_id = uuid.uuid4().hex
    bindings = build_entity_bindings(validation.rows, source='initial')
    database.execute("\n        INSERT INTO level_versions(\n            id, session_id, stage_number, parent_version_id, source,\n            rows_json, summary, diff_json, validation_json,\n            design_context_json, entity_bindings_json,\n            idempotency_key, created_at\n        ) VALUES (?, ?, 1, NULL, 'initial', ?, ?, '[]', ?, ?, ?, ?, ?)\n        ", (version_id, session['id'], dump_json(list(validation.rows)), 'Algorithm-generated demo map' if bool(session['demo_mode']) else 'Initial draft from Unity', dump_json(validation.as_dict()), dump_json({**empty_design_context(), 'updatedFromStageId': version_id}), dump_json(bindings), 'initial:' + session['creation_key'], created_at))
    database.execute('UPDATE design_sessions\n           SET current_version_id = ?, draft_rows_json = NULL,\n               draft_validation_json = NULL, draft_regeneration_status = NULL,\n               draft_regeneration_request_id = NULL, draft_regeneration_key = NULL,\n               draft_regeneration_requested_at = NULL,\n               draft_regeneration_updated_at = NULL,\n               draft_regeneration_failure_code = NULL, updated_at = ?\n           WHERE id = ? AND current_version_id IS NULL', (version_id, created_at, session['id']))
    if not bool(session['demo_mode']):
        record_agent_handoff(database, session['id'], 'blueprint_planning', 'co_creation_chat', 'validated_initial_stage', {'versionId': version_id, 'initialDraftMethod': session['initial_draft_method']}, evidence=[{'type': 'deterministic_solver', 'status': 'passed', 'validation': validation.as_dict()}], status='confirmed', created_at=created_at)
    app_attr('record_event')(database, session['id'], 'draft_preview_promoted', {'versionId': version_id, 'generation': int(session['draft_generation'] or 1)}, created_at)
    return (version_id, True)

def _insert_version(database, session, validation, source, summary, idempotency_key, current, parent_version_id=None, design_context=None, entity_bindings=None, entity_transitions=None):
    version_id = uuid.uuid4().hex
    now = utc_now()
    current_rows = load_json(current['rows_json'])
    binding_parent_id = parent_version_id or current['id']
    parent_bindings = load_entity_bindings(database, session['id'], binding_parent_id)
    if not entity_transitions:
        parent_rows = current_rows
        if parent_version_id and parent_version_id != current['id']:
            parent_version = get_version(database, session['id'], parent_version_id)
            if parent_version is not None:
                parent_rows = load_json(parent_version['rows_json'])
        entity_transitions = derive_entity_transitions(parent_rows, validation.rows)
    if isinstance(entity_bindings, dict) and entity_bindings_match_rows(entity_bindings, validation.rows):
        version_bindings = dict(entity_bindings)
    else:
        version_bindings = build_entity_bindings(validation.rows, parent_bindings=parent_bindings, entity_transitions=entity_transitions, source=source)
    version_bindings['mapFingerprint'] = map_fingerprint(validation.rows)
    version_bindings['bindingFingerprint'] = entity_binding_fingerprint(version_bindings)
    database.execute('\n        INSERT INTO level_versions(\n            id, session_id, stage_number, parent_version_id, source,\n            rows_json, summary, diff_json, validation_json,\n            design_context_json, entity_bindings_json,\n            idempotency_key, created_at\n        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n        ', (version_id, session['id'], next_stage_number(database, session['id']), parent_version_id or current['id'], source, dump_json(list(validation.rows)), summary, dump_json(describe_diff(current_rows, validation.rows)), dump_json(validation.as_dict()), dump_json(clone_design_context(design_context if design_context is not None else load_design_context(database, session['id'], current['id']))), dump_json(version_bindings), idempotency_key, now))
    database.execute('\n        UPDATE design_sessions SET current_version_id = ?, updated_at = ? WHERE id = ?\n        ', (version_id, now, session['id']))
    if source in {'human_edit', 'restored'}:
        database.execute(
            "UPDATE change_proposals SET status = 'superseded', decided_at = ? WHERE session_id = ? AND status = 'pending'",
            (now, session['id']),
        )
    app_attr('record_event')(database, session['id'], 'version_created', {'versionId': version_id, 'source': source}, now)
    return version_id

def _insert_decision(database, session_id, version_id, proposal_id, decision_type, reason, idempotency_key):
    now = utc_now()
    database.execute('\n        INSERT INTO designer_decisions(\n            id, session_id, version_id, proposal_id, decision_type,\n            reason, idempotency_key, created_at\n        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n        ', (uuid.uuid4().hex, session_id, version_id, proposal_id, decision_type, reason, idempotency_key, now))
    app_attr('record_event')(database, session_id, 'designer_decision', {'decision': decision_type, 'proposalId': proposal_id, 'versionId': version_id}, now)

def insert_turn(database, session, role, content, version_id, request_id, execution):
    turn_id = uuid.uuid4().hex
    now = utc_now()
    proposal_binding = getattr(execution, 'proposal_binding', None) if execution else None
    if not proposal_binding and role == 'assistant' and execution:
        guidance = getattr(execution, 'guidance', None) or {}
        offer = guidance.get('proposalOffer') if isinstance(guidance, dict) else None
        if isinstance(offer, dict) and str(offer.get('summary') or '').strip():
            version = database.execute('SELECT rows_json FROM level_versions WHERE id = ?', (version_id,)).fetchone()
            if version is not None:
                try:
                    proposal_binding = _proposal_binding_for_offer(offer, version_id, load_json(version['rows_json']), load_entity_bindings(database, session['id'], version_id))
                except (TypeError, ValueError):
                    proposal_binding = None
    database.execute('\n        INSERT INTO conversation_turns(\n            id, session_id, sequence_number, role, content, language,\n            version_id, request_id, model, attempts_used, latency_ms,\n            guidance_json, proposal_binding_json, created_at\n        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n        ', (turn_id, session['id'], next_turn_sequence(database, session['id']), role, content, session['language'], version_id, request_id, execution.model if execution else None, execution.attempts_used if execution else None, execution.latency_ms if execution else None, dump_json(execution.guidance) if execution and execution.guidance else None, dump_json(proposal_binding) if proposal_binding else None, now))
    app_attr('record_event')(database, session['id'], 'conversation_turn', {'turnId': turn_id, 'role': role, 'versionId': version_id}, now)
    return turn_id

def _pending_assistant_translations(database, session_id, language, turn_ids):
    unique_turn_ids = list(dict.fromkeys(turn_ids))
    if not unique_turn_ids or len(unique_turn_ids) > 8:
        raise ApiError(400, 'INVALID_TRANSLATION_REQUEST', 'Translation requests require between 1 and 8 unique turnIds.')
    for turn_id in unique_turn_ids:
        _validate_identifier(turn_id, 'turnId')
    placeholders = ','.join(('?' for _ in unique_turn_ids))
    rows = database.execute(f"\n        SELECT turn.id, turn.content, turn.guidance_json, turn.version_id,\n               version.rows_json, version.entity_bindings_json, proposal.summary\n        FROM conversation_turns AS turn\n        JOIN level_versions AS version ON version.id = turn.version_id\n        LEFT JOIN change_proposals AS proposal\n          ON proposal.assistant_turn_id = turn.id\n        LEFT JOIN turn_translations AS translation\n          ON translation.turn_id = turn.id AND translation.language = ?\n        WHERE turn.session_id = ?\n          AND turn.role = 'assistant'\n          AND turn.language != ?\n          AND translation.id IS NULL\n          AND turn.id IN ({placeholders})\n        ORDER BY turn.sequence_number\n        ", (language, session_id, language, *unique_turn_ids)).fetchall()
    items = []
    for row in rows:
        guidance = load_json(row['guidance_json']) or {}
        proposal_offer = guidance.get('proposalOffer') or {}
        proposal_presentation = proposal_offer.get('proposalPresentation') or {}
        presentation_reason = proposal_presentation.get('reason') if proposal_presentation.get('schemaVersion') == 2 else None
        ui_cues = guidance.get('uiCues') or []
        excluded_suffixes = [*[str(cue.get('text') or '').strip() for cue in ui_cues], str(guidance.get('followUpQuestion') or '').strip()]
        body = str(row['content'] or '').rstrip()
        for suffix in reversed([value for value in excluded_suffixes if value]):
            if body.endswith(suffix):
                body = body[:-len(suffix)].rstrip()
        item = {'turnId': row['id'], 'body': body, 'followUpQuestion': guidance.get('followUpQuestion'), 'intentHypothesis': guidance.get('intentHypothesis'), 'proposalOfferSummary': proposal_offer.get('summary'), 'proposalOfferRationale': proposal_offer.get('rationale'), 'proposalPresentationReason': presentation_reason, 'uiCueTexts': [cue.get('text') for cue in ui_cues], 'proposalSummary': row['summary'], 'disagreement': guidance.get('disagreement'), 'guidance': guidance, 'stageRows': load_json(row['rows_json']) or [], 'entityBindings': load_json(row['entity_bindings_json']) or {}}
        coordinate_links = guidance.get('coordinateLinks') or []
        if guidance.get('proposalQuestion'):
            item['proposalQuestion'] = guidance['proposalQuestion']
        if proposal_presentation.get('binaryCompact'):
            item['proposalVerifiedActions'] = proposal_presentation.get('verifiedActions') or []
        if coordinate_links:
            item['coordinateLinks'] = coordinate_links
        items.append(item)
    return items

def _translated_guidance(source_guidance, translated):
    guidance = dict(source_guidance or {})
    if guidance.get('proposalQuestion') and translated.get('proposalQuestion'):
        guidance['proposalQuestion'] = translated['proposalQuestion']
    guidance['followUpQuestion'] = translated['followUpQuestion']
    guidance['intentHypothesis'] = translated['intentHypothesis']
    source_offer = guidance.get('proposalOffer')
    if source_offer:
        guidance['proposalOffer'] = {**source_offer, 'summary': translated['proposalOfferSummary'], 'rationale': translated['proposalOfferRationale']}
        presentation = source_offer.get('proposalPresentation')
        if isinstance(presentation, dict):
            presentation = dict(presentation)
            if presentation.get('schemaVersion') == 2:
                if presentation.get('binaryCompact') and 'proposalVerifiedActions' in translated:
                    presentation['verifiedActions'] = translated['proposalVerifiedActions']
                if translated['proposalPresentationReason'] is not None:
                    presentation['reason'] = translated['proposalPresentationReason']
            else:
                if translated['proposalOfferSummary'] is not None:
                    presentation['summary'] = translated['proposalOfferSummary']
                if translated['proposalOfferRationale'] is not None:
                    presentation['rationale'] = translated['proposalOfferRationale']
            guidance['proposalOffer']['proposalPresentation'] = presentation
    translated_cues = translated['uiCueTexts']
    guidance['uiCues'] = [{**cue, 'text': translated_cues[index]} for index, cue in enumerate(guidance.get('uiCues') or [])]
    translated_disagreement = translated.get('disagreement')
    if translated_disagreement is not None and guidance.get('disagreement') is not None:
        guidance['disagreement'] = {**guidance['disagreement'], **translated_disagreement}
    source_links = guidance.get('coordinateLinks') or []
    translated_link_texts = translated.get('coordinateLinkTexts') or []
    if source_links and len(translated_link_texts) == len(source_links):
        translated_body = str(translated.get('body') or '')
        guidance['coordinateLinks'] = [{**link, 'text': translated_text} for link, translated_text in zip(source_links, translated_link_texts) if isinstance(translated_text, str) and translated_text.strip() and (translated_text in translated_body)]
    elif source_links:
        guidance['coordinateLinks'] = []
    return guidance

@proposal_phase('freeze_binding', 'brief')
def _proposal_binding_for_offer(offer, version_id, rows, entity_bindings=None):
    """Normalize and bind a proposal to the exact saved Stage it was generated from."""
    if not isinstance(offer, dict) or not str(offer.get('summary') or '').strip():
        return None
    raw_brief = offer.get('executionBrief')
    if raw_brief is None and 'revisionPlan' in offer:
        raw_brief = normalize_revision_plan(offer.get('revisionPlan'), rows, entity_bindings)
    if not isinstance(raw_brief, dict) or not isinstance(raw_brief.get('requiredTransitions'), list) or (not raw_brief['requiredTransitions']):
        raise ValueError('proposal requires exact requiredTransitions')
    try:
        brief = validate_execution_brief(raw_brief, rows, entity_bindings)
    except ValueError:
        brief = validate_execution_brief(raw_brief, None)
        transitions = brief.get('requiredTransitions') or []
        if not transitions or any((rows[item['row'] - 1][item['column'] - 1] != item['to'] for item in transitions)):
            raise
        return {'baseVersionId': version_id, 'mapFingerprint': map_fingerprint(rows), 'entityBindingFingerprint': (entity_bindings or {}).get('bindingFingerprint'), 'executionBrief': brief, 'revisionWorkflow': dict(offer.get('revisionWorkflow')) if isinstance(offer.get('revisionWorkflow'), dict) else {}, 'status': 'already_satisfied'}
    status = 'active'
    for transition in brief.get('requiredTransitions') or []:
        actual = rows[transition['row'] - 1][transition['column'] - 1]
        if actual == transition['to']:
            status = 'already_satisfied'
            break
        if actual != transition['from']:
            status = 'stale'
            break
    return {'baseVersionId': version_id, 'mapFingerprint': map_fingerprint(rows), 'entityBindingFingerprint': (entity_bindings or {}).get('bindingFingerprint'), 'executionBrief': brief, 'revisionWorkflow': dict(offer.get('revisionWorkflow')) if isinstance(offer.get('revisionWorkflow'), dict) else {}, 'status': status}

def expire_interrupted_attempts(database, session_id):
    cutoff = iso_time(datetime.now(timezone.utc) - INTERRUPTED_AFTER)
    database.execute("\n        UPDATE play_attempts SET status = 'interrupted', finished_at = ?\n        WHERE session_id = ? AND status IN ('issued', 'loaded', 'started')\n          AND issued_at < ?\n        ", (utc_now(), session_id, cutoff))

def validate_legacy_conversation(messages):
    if not messages:
        raise ApiError(400, 'EMPTY_CONVERSATION', 'At least one chat message is required.')
    if messages[-1]['role'] != 'user':
        raise ApiError(400, 'LAST_MESSAGE_MUST_BE_USER', 'The final message must be from the user.')
    if len(messages) > 20:
        raise ApiError(400, 'TOO_MANY_MESSAGES', 'A request may contain at most 20 messages.')
    total = 0
    for message in messages:
        if not message['content'].strip():
            raise ApiError(400, 'EMPTY_MESSAGE', 'Messages must not be empty.')
        if len(message['content']) > MAX_MESSAGE_LENGTH:
            raise ApiError(400, 'MESSAGE_TOO_LONG', 'A message exceeds 2000 characters.')
        total += len(message['content'])
    if total > 12000:
        raise ApiError(400, 'CONVERSATION_TOO_LONG', 'Conversation text is too long.')
