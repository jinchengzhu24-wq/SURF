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


logger = logging.getLogger(__name__)
HOST = '127.0.0.1'
PORT = 8010
BACKEND_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = BACKEND_DIR.parent / 'Frontend'
MAX_MESSAGE_LENGTH = 2000
MAX_INTENTION_LENGTH = 4000
REQUEST_ID_PATTERN = re.compile('^[A-Za-z0-9_-]{1,64}$')
SESSION_COOKIE_NAME = 'sokoban_cocreation_access'
PLAY_TICKET_LIFETIME = timedelta(minutes=5)
COCREATION_DEADLINE = timedelta(minutes=20)
COCREATION_DEADLINE_SECONDS = int(COCREATION_DEADLINE.total_seconds())
INTERRUPTED_AFTER = timedelta(minutes=30)
_message_locks = {}
_message_locks_guard = Lock()
AGENT_HANDOFF_SCHEMA_VERSION = 1
AGENT_HANDOFF_STATUSES = {'proposed', 'confirmed', 'rejected'}
MESSAGE_ACTIONS = {'none', 'execute_revision', 'challenge_revision', 'alternative_revision', 'continue_challenge'}
REVISION_CARD_ACTIONS = {'execute_revision', 'challenge_revision', 'alternative_revision'}
CARD_ACTION_EVENTS = {'execute_revision': 'revision_execution_requested', 'challenge_revision': 'proposal_challenge_started', 'alternative_revision': 'alternative_revision_requested'}
SAMPLE_ROWS = ['############', '#..........#', '#..........#', '#..........#', '#...p......#', '#...s.t....#', '#..........#', '#..........#', '#..........#', '############']
SAMPLE_LEGEND = {'#': 'wall', '.': 'floor', '@': 'water', 'p': 'player', 's': 'box', 't': 'target'}
load_dotenv(BACKEND_DIR / '.env')
PUBLIC_BASE_URL = os.getenv('COCREATION_PUBLIC_BASE_URL', 'https://sokobanaidemo.top/cocreation').rstrip('/')
WEBGL_BASE_URL = os.getenv('COCREATION_WEBGL_BASE_URL', 'https://sokobanaidemo.top/game/')
ONLINE_MATCH_SYNC_URL = os.getenv('COCREATION_ONLINE_MATCH_SYNC_URL', '').rstrip('/')
ONLINE_MATCH_SYNC_SECRET = os.getenv('COCREATION_INTENTION_SYNC_SECRET', '').strip()
ONLINE_MATCH_SYNC_TIMEOUT_SECONDS = 5.0
TOKEN_SECRET = os.getenv('COCREATION_TOKEN_SECRET', 'development-only-change-before-deployment').encode('utf-8')
ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv('COCREATION_ALLOWED_ORIGINS', 'https://sokobanaidemo.top,https://www.sokobanaidemo.top,http://127.0.0.1:8000,http://localhost:8000').split(',') if origin.strip()]
DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS = 60
DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS = 120
DRAFT_REGENERATION_SWEEP_INTERVAL_SECONDS = 10

def record_agent_handoff(database, session_id, from_agent, to_agent, artifact_type, artifact=None, evidence=None, status='proposed', created_at=None):
    """Persist one small, inspectable handoff without changing the public API."""
    if status not in AGENT_HANDOFF_STATUSES:
        raise ValueError(f'Unsupported agent handoff status: {status}')
    app_attr('record_event')(database, session_id, 'agent_handoff', {'schemaVersion': AGENT_HANDOFF_SCHEMA_VERSION, 'fromAgent': from_agent, 'toAgent': to_agent, 'artifactType': artifact_type, 'artifact': artifact or {}, 'evidence': evidence or [], 'status': status}, created_at or utc_now())

class ApiError(Exception):

    def __init__(self, status_code, code, message, retryable=False, details=None):
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        self.retryable = bool(retryable)
        self.details = details

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class ChatMessage(StrictModel):
    role: Literal['user', 'assistant']
    content: str

class LegacyChatRequest(StrictModel):
    messages: list[ChatMessage]

class CreateSessionRequest(StrictModel):
    rows: list[str]
    initialDraftMethod: Literal['partial_completion', 'description_generation']
    language: Literal['en', 'zh-CN'] = 'zh-CN'
    idempotencyKey: str
    matchId: str | None = None
    playerNumber: int | None = None

class DemoSessionRequest(StrictModel):
    language: Literal['en', 'zh-CN'] = 'zh-CN'
    idempotencyKey: str

class DraftRegenerationRequest(StrictModel):
    idempotencyKey: str

class DraftRegenerationCompleteRequest(StrictModel):
    rows: list[str]

class DraftRegenerationFailRequest(StrictModel):
    failureCode: Literal['generation_failed', 'llm_failed', 'validation_failed', 'unity_unavailable', 'blueprint_unavailable', 'generation_timed_out']

class BrowserAccessRequest(StrictModel):
    bootstrapToken: str

class LanguageRequest(StrictModel):
    language: Literal['en', 'zh-CN']

class TranslationRequest(StrictModel):
    turnIds: list[str]

class VersionRequest(StrictModel):
    rows: list[str]
    baseVersionId: str
    idempotencyKey: str
    summary: str = ''

class RestoreRequest(StrictModel):
    baseVersionId: str
    idempotencyKey: str

class AssessmentRequest(StrictModel):
    idempotencyKey: str

class MessageRequest(StrictModel):
    content: str
    baseVersionId: str
    idempotencyKey: str
    requestProposal: bool = False
    action: Literal['none', 'execute_revision', 'challenge_revision', 'alternative_revision', 'continue_challenge'] = 'none'
    sourceTurnId: str | None = None
    challengeId: str | None = None
    challengeChoice: Literal['ai', 'user'] | None = None
    exitChallenge: bool = False

class BinaryCancelRequest(StrictModel):
    baseVersionId: str
    topicId: str
    idempotencyKey: str

class BinaryChoiceRequest(BinaryCancelRequest):
    questionId: str
    choiceId: str
    displayLanguage: Literal['en', 'zh-CN']

class ChallengeReviewRetryRequest(StrictModel):
    baseVersionId: str
    idempotencyKey: str

class ComponentRecoveryRequest(StrictModel):
    baseVersionId: str
    idempotencyKey: str

class ProposalDecisionRequest(StrictModel):
    decision: Literal['accept', 'reject']
    baseVersionId: str
    idempotencyKey: str
    reason: str = ''

class FinalizeRequest(StrictModel):
    baseVersionId: str
    idempotencyKey: str
    rows: list[str] | None = None

class IntentionRequest(StrictModel):
    content: str
    idempotencyKey: str

class IntentFeedbackRequest(StrictModel):
    action: Literal['confirm', 'reject', 'revise', 'keep']
    candidateText: str | None = None
    selectedHypothesisId: str | None = None
    sourceTurnId: str
    baseVersionId: str
    idempotencyKey: str

class QuestionFeedbackRequest(StrictModel):
    action: Literal['ignore', 'restore']
    baseVersionId: str
    idempotencyKey: str

class PlayAttemptRequest(StrictModel):
    idempotencyKey: str

class PlayBootstrapRequest(StrictModel):
    ticket: str

class PlayMetricsRequest(StrictModel):
    attemptToken: str
    durationSeconds: float = 0
    moveCount: int = 0
    pushCount: int = 0
    restartCount: int = 0
    minimumMoves: int = -1
    minimumPushes: int = -1

def _llm_failure_details(exception):
    code = str(exception.code or '')
    if code.startswith('REVISION_PLAN'):
        task, failure_stage, maximum = ('revision_plan', 'schema', None)
    elif code.startswith('REQUIREMENT_'):
        task, failure_stage, maximum = ('revision_requirements', 'evidence', None)
    elif code in {'PROPOSAL_SEARCH_EXHAUSTED', 'DETERMINISTIC_SEARCH_EXHAUSTED', 'CANDIDATE_UNSOLVABLE', 'SEMANTIC_CONSTRAINT_NOT_MET', 'SOFT_OBJECTIVE_EVIDENCE_MISSING', 'HARD_OBJECTIVE_NOT_MET'}:
        task, failure_stage, maximum = ('deterministic_search', 'semantic_contract', None)
    else:
        task, failure_stage, maximum = ('chat', 'upstream', None)
    failure_kind = 'upstream'
    if code in {'MODEL_RESPONSE_INVALID', 'MODEL_LOW_QUALITY_RESPONSE', 'MODEL_EMPTY_RESPONSE'}:
        failure_stage, failure_kind = ('response_validation', 'model_validation')
    if code.startswith('REQUIREMENT_'):
        failure_kind = 'constraint_conflict' if code == 'REQUIREMENT_PERMISSION_CONFLICT' else 'understanding'
    elif code.startswith('REVISION_PLAN'):
        failure_kind = 'understanding'
    elif code == 'CANDIDATE_UNSOLVABLE':
        failure_stage, failure_kind = ('solvability', 'candidate_unsolvable')
    elif code in {'PROPOSAL_SEARCH_EXHAUSTED', 'DETERMINISTIC_SEARCH_EXHAUSTED'} or 'BUDGET' in code:
        failure_stage, failure_kind = ('search', 'search_incomplete')
    elif code in {'SEMANTIC_CONSTRAINT_NOT_MET', 'HARD_OBJECTIVE_NOT_MET'}:
        failure_kind = 'constraint_conflict'
    return {'task': task, 'failureStage': failure_stage, 'failureKind': failure_kind, 'failureCode': code, 'attemptsUsed': exception.attempts_used, **({'maximumAttempts': maximum} if maximum is not None else {}), 'retryable': exception.retryable, 'safeReason': exception.safe_message, 'committed': False, 'recoveryState': 'retry_pending' if exception.retryable else 'revision_needed', **getattr(exception, 'details', {})}

def _inline_display_text(value):
    return re.sub('\\s+', ' ', str(value or '')).strip()

def require_browser_session(database, session_id, token):
    session = get_session(database, session_id)
    if session is None:
        raise ApiError(404, 'SESSION_NOT_FOUND', 'The co-creation session was not found.')
    if not token_matches(token, session['access_hash']):
        raise ApiError(401, 'SESSION_ACCESS_DENIED', 'Session access was denied.')
    return session

def require_active_session(database, session_id, token):
    session = require_browser_session(database, session_id, token)
    if session['status'] != 'active':
        raise ApiError(409, 'SESSION_LOCKED', 'This co-creation session is no longer editable.')
    if session['language_locked_at'] is None:
        raise ApiError(409, 'LANGUAGE_NOT_SELECTED', 'Select the session language before entering the co-creation workspace.')
    if session_deadline_expired(session):
        raise ApiError(409, 'SESSION_DEADLINE_EXPIRED', 'Time is up. Submit the current map as the final Stage now.')
    return session

def session_deadline_expired(session):
    deadline_at = session['deadline_at']
    return deadline_at is not None and parse_time(deadline_at) <= datetime.now(timezone.utc)

def start_deadline_if_missing(database, session):
    if bool(session['demo_mode']):
        return (None, None)
    if session['deadline_at'] is not None:
        return (session['deadline_started_at'], session['deadline_at'])
    now = utc_now()
    deadline_at = iso_time(datetime.now(timezone.utc) + COCREATION_DEADLINE)
    database.execute('UPDATE design_sessions\n           SET deadline_started_at = ?, deadline_at = ?, updated_at = ?\n           WHERE id = ? AND deadline_at IS NULL', (now, deadline_at, now, session['id']))
    app_attr('record_event')(database, session['id'], 'deadline_started', {'deadlineAt': deadline_at}, now)
    return (now, deadline_at)

def require_current_base(session, base_version_id):
    if session['current_version_id'] != base_version_id:
        raise ApiError(409, 'VERSION_CONFLICT', 'The current Stage changed. Refresh before continuing.', details={'currentVersionId': session['current_version_id']})

def _solve_or_api_error(rows):
    try:
        return validate_and_solve(rows)
    except LevelValidationError as error:
        raise ApiError(400, error.code, str(error), details=error.details) from error

@proposal_phase('structure_and_solve', 'candidate')
def _validate_changed_proposal(base_rows, proposed_rows, *, deadline=None):
    try:
        validate_ai_edit_diff(base_rows, proposed_rows)
    except ValueError as error:
        raise LevelValidationError('PROTECTED_EDIT', 'AI revisions may edit water and internal walls only.', details=getattr(error, 'details', {})) from error
    validation = validate_and_solve(proposed_rows, **{'deadline': deadline} if deadline is not None else {})
    if not describe_diff(base_rows, validation.rows):
        raise LevelValidationError('UNCHANGED_PROPOSAL', 'The proposed map must change at least one tile from the current Stage.')
    return validation

@proposal_phase('structure_and_solve', 'candidate')
def _solve_changed_proposal_or_api_error(base_rows, proposed_rows, *, deadline=None):
    try:
        return _validate_changed_proposal(base_rows, proposed_rows, **{'deadline': deadline} if deadline is not None else {})
    except LevelValidationError as error:
        raise ApiError(400, error.code, str(error), details=error.details) from error

@proposal_phase('execution_replay', 'execution')
def _execute_revision_candidate_or_api_error(base_rows, execution):
    """Re-apply the modifier output before a proposal can enter persistence."""
    contract = execution.revision_contract or {}
    operations = execution.revision_operations or []
    if not contract:
        return
    if not operations:
        raise ApiError(502, 'REVISION_EXECUTION_INVALID', 'The level revision assistant returned no executable operations.')
    selected_index = (execution.proposal_diagnostics or {}).get('selectedStrategyIndex')
    try:
        executed_rows = execute_revision_operations(base_rows, operations, contract, selected_index)
    except (TypeError, ValueError, KeyError) as error:
        raise ApiError(502, 'REVISION_EXECUTION_INVALID', 'The level revision assistant output failed the execution contract.') from error
    if list(executed_rows) != list(execution.proposed_rows or []):
        raise ApiError(502, 'REVISION_EXECUTION_MISMATCH', 'The executable operations did not produce the proposed map.')

def _validate_identifier(value, field_name):
    if not isinstance(value, str) or not REQUEST_ID_PATTERN.fullmatch(value):
        raise ApiError(400, 'INVALID_IDENTIFIER', f'{field_name} is invalid.')

def _validate_metrics(payload):
    if payload.durationSeconds < 0 or payload.durationSeconds > 86400 or min(payload.moveCount, payload.pushCount, payload.restartCount) < 0 or (max(payload.moveCount, payload.pushCount, payload.restartCount) > 1000000) or (payload.pushCount > payload.moveCount):
        raise ApiError(400, 'INVALID_PLAY_METRICS', 'Play metrics are invalid.')

def derive_token(kind, identifier):
    digest = hmac.new(TOKEN_SECRET, f'{kind}:{identifier}'.encode('utf-8'), hashlib.sha256).hexdigest()
    return digest

def hash_token(token):
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()

def token_matches(token, expected_hash):
    if not token or not expected_hash:
        return False
    return secrets.compare_digest(hash_token(token), expected_hash)

def build_launch_url(session_id, bootstrap_token, mode='unity'):
    return f'{PUBLIC_BASE_URL}/#session={quote(session_id)}&bootstrap={quote(bootstrap_token)}&mode={quote(mode)}'

def build_return_url(session_id, version_id):
    return f'{PUBLIC_BASE_URL}/#session={quote(session_id)}&stage={quote(version_id)}'

def build_play_url(attempt_id, ticket):
    separator = '&' if '?' in WEBGL_BASE_URL else '?'
    return f'{WEBGL_BASE_URL}{separator}cocreationAttempt={quote(attempt_id)}&cocreationPlay={quote(ticket)}'

def clean_optional(value, maximum_length):
    if value is None:
        return None
    return str(value).strip()[:maximum_length]

def utc_now():
    return iso_time(datetime.now(timezone.utc))

def iso_time(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def error_response(status_code, code, message, request_id, retryable, details=None):
    payload = {'code': str(code), 'message': str(message), 'requestId': str(request_id), 'retryable': bool(retryable)}
    if details is not None:
        payload['details'] = details
    return JSONResponse(status_code=status_code, content=payload)
