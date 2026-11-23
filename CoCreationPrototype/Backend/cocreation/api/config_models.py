"""Shared API configuration defaults, request/error models, and pure validation helpers."""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

import httpx
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from cocreation.clock import iso_time, parse_time
from cocreation.domain.level_validation import (
    LevelValidationError,
    describe_diff,
    validate_and_solve,
)
from cocreation.domain.proposal_diagnostics import proposal_phase
from cocreation.llm.facade import (
    execute_revision_operations,
    validate_ai_edit_diff,
)
from cocreation.persistence.database import (
    get_session,
    record_event,
)

if TYPE_CHECKING:
    from .services import CoCreationServices


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
AGENT_HANDOFF_SCHEMA_VERSION = 1
AGENT_HANDOFF_STATUSES = {'proposed', 'confirmed', 'rejected'}
MESSAGE_ACTIONS = {'none', 'execute_revision', 'challenge_revision', 'alternative_revision', 'continue_challenge'}
REVISION_CARD_ACTIONS = {'execute_revision', 'challenge_revision', 'alternative_revision'}
CARD_ACTION_EVENTS = {
    'execute_revision': 'revision_execution_requested',
    'challenge_revision': 'proposal_challenge_started',
    'alternative_revision': 'alternative_revision_requested',
}
SAMPLE_ROWS = [
    '############',
    '#..........#',
    '#..........#',
    '#..........#',
    '#...p......#',
    '#...s.t....#',
    '#..........#',
    '#..........#',
    '#..........#',
    '############',
]
SAMPLE_LEGEND = {'#': 'wall', '.': 'floor', '@': 'water', 'p': 'player', 's': 'box', 't': 'target'}
DEFAULT_PUBLIC_BASE_URL = 'https://sokobanaidemo.top/cocreation'
DEFAULT_WEBGL_BASE_URL = 'https://sokobanaidemo.top/game/'
DEFAULT_TOKEN_SECRET = 'development-only-change-before-deployment'
DEFAULT_ALLOWED_ORIGINS = (
    'https://sokobanaidemo.top',
    'https://www.sokobanaidemo.top',
    'http://127.0.0.1:8000',
    'http://localhost:8000',
)
ONLINE_MATCH_SYNC_TIMEOUT_SECONDS = 5.0
DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS = 60
DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS = 120
DRAFT_REGENERATION_SWEEP_INTERVAL_SECONDS = 10


def record_agent_handoff(
    database,
    session_id,
    from_agent,
    to_agent,
    artifact_type,
    artifact=None,
    evidence=None,
    status='proposed',
    created_at=None,
    *,
    services: CoCreationServices | None = None,
):
    """Persist one small, inspectable handoff without changing the public API."""
    if status not in AGENT_HANDOFF_STATUSES:
        raise ValueError(f'Unsupported agent handoff status: {status}')
    timestamp = created_at or (services.utc_now() if services is not None else utc_now())
    payload = {
        'schemaVersion': AGENT_HANDOFF_SCHEMA_VERSION,
        'fromAgent': from_agent,
        'toAgent': to_agent,
        'artifactType': artifact_type,
        'artifact': artifact or {},
        'evidence': evidence or [],
        'status': status,
    }
    if services is not None:
        services.record_event(database, session_id, 'agent_handoff', payload, timestamp)
    else:
        record_event(database, session_id, 'agent_handoff', payload, timestamp)


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
    failureCode: Literal[
        'generation_failed',
        'llm_failed',
        'validation_failed',
        'unity_unavailable',
        'blueprint_unavailable',
        'generation_timed_out',
    ]


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
    action: Literal[
        'none',
        'execute_revision',
        'challenge_revision',
        'alternative_revision',
        'continue_challenge',
    ] = 'none'
    sourceTurnId: str | None = None
    challengeId: str | None = None
    challengeChoice: Literal['ai', 'user', 'retain_current'] | None = None
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


def _online_sync_api_error(error, *, rejected_code, unavailable_code, saved_item):
    """Keep permanent upstream rejections distinct from retryable outages."""
    response = error.response if isinstance(error, httpx.HTTPStatusError) else None
    upstream_status = response.status_code if response is not None else None
    upstream_detail = ''
    if response is not None:
        try:
            body = response.json()
            upstream_detail = str(body.get('detail') or body.get('message') or '').strip()
        except (ValueError, AttributeError):
            upstream_detail = str(response.text or '').strip()
        upstream_detail = upstream_detail[:500]
    logger.warning(
        'Online match synchronization failed: status=%s detail=%s error=%s',
        upstream_status,
        upstream_detail,
        type(error).__name__,
    )
    details = {'upstreamStatus': upstream_status, 'upstreamDetail': upstream_detail or None}
    if upstream_status is not None and 400 <= upstream_status < 500 and upstream_status not in {408, 429}:
        return ApiError(
            502,
            rejected_code,
            f'The {saved_item} was saved, but the online match rejected its synchronization data.',
            retryable=False,
            details=details,
        )
    return ApiError(
        503,
        unavailable_code,
        f'The {saved_item} was saved but cannot yet be synchronized to the online match. Please retry.',
        retryable=True,
        details=details,
    )


def _llm_failure_details(exception):
    code = str(exception.code or '')
    if code.startswith('REVISION_PLAN'):
        task, failure_stage, maximum = ('revision_plan', 'schema', None)
    elif code.startswith('REQUIREMENT_'):
        task, failure_stage, maximum = ('revision_requirements', 'evidence', None)
    elif code in {
        'PROPOSAL_SEARCH_EXHAUSTED',
        'DETERMINISTIC_SEARCH_EXHAUSTED',
        'CANDIDATE_UNSOLVABLE',
        'SEMANTIC_CONSTRAINT_NOT_MET',
        'SOFT_OBJECTIVE_EVIDENCE_MISSING',
        'HARD_OBJECTIVE_NOT_MET',
    }:
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
    return {
        'task': task,
        'failureStage': failure_stage,
        'failureKind': failure_kind,
        'failureCode': code,
        'attemptsUsed': exception.attempts_used,
        **({'maximumAttempts': maximum} if maximum is not None else {}),
        'retryable': exception.retryable,
        'safeReason': exception.safe_message,
        'committed': False,
        'recoveryState': 'retry_pending' if exception.retryable else 'revision_needed',
        **getattr(exception, 'details', {}),
    }


def _inline_display_text(value):
    return re.sub('\\s+', ' ', str(value or '')).strip()


def require_browser_session(database, session_id, token, *, services: CoCreationServices | None = None):
    session = get_session(database, session_id)
    if session is None:
        raise ApiError(404, 'SESSION_NOT_FOUND', 'The co-creation session was not found.')
    if not token_matches(token, session['access_hash']):
        raise ApiError(401, 'SESSION_ACCESS_DENIED', 'Session access was denied.')
    return session


def require_active_session(database, session_id, token, *, now=None, services: CoCreationServices | None = None):
    session = require_browser_session(database, session_id, token, services=services)
    if session['status'] != 'active':
        raise ApiError(409, 'SESSION_LOCKED', 'This co-creation session is no longer editable.')
    if session['language_locked_at'] is None:
        raise ApiError(409, 'LANGUAGE_NOT_SELECTED', 'Select the session language before entering the co-creation workspace.')
    if session_deadline_expired(session, now=now, services=services):
        raise ApiError(409, 'SESSION_DEADLINE_EXPIRED', 'Time is up. Submit the current map as the final Stage now.')
    return session


def session_deadline_expired(session, *, now=None, services: CoCreationServices | None = None):
    deadline_at = session['deadline_at']
    if deadline_at is None:
        return False
    current = now if now is not None else (services.utc_datetime() if services is not None else datetime.now(timezone.utc))
    return parse_time(deadline_at) <= current


def start_deadline_if_missing(database, session, *, services: CoCreationServices | None = None):
    if bool(session['demo_mode']):
        return (None, None)
    if session['deadline_at'] is not None:
        return (session['deadline_started_at'], session['deadline_at'])
    current_dt = services.utc_datetime() if services is not None else datetime.now(timezone.utc)
    now = iso_time(current_dt)
    deadline_delta = (
        timedelta(seconds=services.settings.cocreation_deadline_seconds)
        if services is not None
        else COCREATION_DEADLINE
    )
    deadline_at = iso_time(current_dt + deadline_delta)
    database.execute(
        'UPDATE design_sessions\n           SET deadline_started_at = ?, deadline_at = ?, updated_at = ?\n           WHERE id = ? AND deadline_at IS NULL',
        (now, deadline_at, now, session['id']),
    )
    if services is not None:
        services.record_event(database, session['id'], 'deadline_started', {'deadlineAt': deadline_at}, now)
    else:
        record_event(database, session['id'], 'deadline_started', {'deadlineAt': deadline_at}, now)
    return (now, deadline_at)


def require_current_base(session, base_version_id):
    if session['current_version_id'] != base_version_id:
        raise ApiError(
            409,
            'VERSION_CONFLICT',
            'The current Stage changed. Refresh before continuing.',
            details={'currentVersionId': session['current_version_id']},
        )


def _solve_or_api_error(rows, *, services: CoCreationServices | None = None):
    try:
        if services is not None:
            return validate_and_solve(rows, clock=services.clock)
        return validate_and_solve(rows)
    except LevelValidationError as error:
        raise ApiError(400, error.code, str(error), details=error.details) from error


@proposal_phase('structure_and_solve', 'candidate')
def _validate_changed_proposal(base_rows, proposed_rows, *, deadline=None, services: CoCreationServices | None = None):
    diff_validator = services.llm.validate_ai_edit_diff if services is not None else validate_ai_edit_diff
    try:
        diff_validator(base_rows, proposed_rows)
    except ValueError as error:
        raise LevelValidationError(
            'PROTECTED_EDIT',
            'AI revisions may edit water and internal walls only.',
            details=getattr(error, 'details', {}),
        ) from error
    solve_kwargs = {}
    if deadline is not None:
        solve_kwargs['deadline'] = deadline
    if services is not None:
        solve_kwargs['clock'] = services.clock
    validation = validate_and_solve(proposed_rows, **solve_kwargs)
    if not describe_diff(base_rows, validation.rows):
        raise LevelValidationError('UNCHANGED_PROPOSAL', 'The proposed map must change at least one tile from the current Stage.')
    return validation


@proposal_phase('structure_and_solve', 'candidate')
def _solve_changed_proposal_or_api_error(base_rows, proposed_rows, *, deadline=None, services: CoCreationServices | None = None):
    try:
        return _validate_changed_proposal(
            base_rows,
            proposed_rows,
            deadline=deadline,
            services=services,
        )
    except LevelValidationError as error:
        raise ApiError(400, error.code, str(error), details=error.details) from error


@proposal_phase('execution_replay', 'execution')
def _execute_revision_candidate_or_api_error(base_rows, execution, *, services: CoCreationServices | None = None):
    """Re-apply the modifier output before a proposal can enter persistence."""
    contract = execution.revision_contract or {}
    operations = execution.revision_operations or []
    if not contract:
        return
    if not operations:
        raise ApiError(502, 'REVISION_EXECUTION_INVALID', 'The level revision assistant returned no executable operations.')
    selected_index = (execution.proposal_diagnostics or {}).get('selectedStrategyIndex')
    executor = services.llm.execute_revision_operations if services is not None else execute_revision_operations
    try:
        executed_rows = executor(base_rows, operations, contract, selected_index)
    except (TypeError, ValueError, KeyError) as error:
        raise ApiError(502, 'REVISION_EXECUTION_INVALID', 'The level revision assistant output failed the execution contract.') from error
    if list(executed_rows) != list(execution.proposed_rows or []):
        raise ApiError(502, 'REVISION_EXECUTION_MISMATCH', 'The executable operations did not produce the proposed map.')


def _validate_identifier(value, field_name):
    if not isinstance(value, str) or not REQUEST_ID_PATTERN.fullmatch(value):
        raise ApiError(400, 'INVALID_IDENTIFIER', f'{field_name} is invalid.')


def _validate_metrics(payload):
    if not math.isfinite(payload.durationSeconds):
        raise ApiError(400, 'INVALID_METRICS', 'Play durationSeconds must be finite.')
    if (
        payload.durationSeconds < 0
        or payload.durationSeconds > 86400
        or min(payload.moveCount, payload.pushCount, payload.restartCount) < 0
        or max(payload.moveCount, payload.pushCount, payload.restartCount) > 1000000
        or payload.pushCount > payload.moveCount
    ):
        raise ApiError(400, 'INVALID_PLAY_METRICS', 'Play metrics are invalid.')


def derive_token(kind, identifier, *, secret: bytes | None = None, services: CoCreationServices | None = None):
    resolved_secret = (
        secret
        if secret is not None
        else (services.settings.token_secret_bytes if services is not None else DEFAULT_TOKEN_SECRET.encode('utf-8'))
    )
    return hmac.new(resolved_secret, f'{kind}:{identifier}'.encode('utf-8'), hashlib.sha256).hexdigest()


def hash_token(token):
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()


def token_matches(token, expected_hash):
    if not token or not expected_hash:
        return False
    return secrets.compare_digest(hash_token(token), expected_hash)


def build_launch_url(session_id, bootstrap_token, mode='unity', *, public_base_url: str | None = None, services: CoCreationServices | None = None):
    base_url = (
        public_base_url
        if public_base_url is not None
        else (services.settings.public_base_url if services is not None else DEFAULT_PUBLIC_BASE_URL)
    ).rstrip('/')
    return f'{base_url}/#session={quote(session_id)}&bootstrap={quote(bootstrap_token)}&mode={quote(mode)}'


def build_return_url(session_id, version_id, *, public_base_url: str | None = None, services: CoCreationServices | None = None):
    base_url = (
        public_base_url
        if public_base_url is not None
        else (services.settings.public_base_url if services is not None else DEFAULT_PUBLIC_BASE_URL)
    ).rstrip('/')
    return f'{base_url}/#session={quote(session_id)}&stage={quote(version_id)}'


def build_play_url(attempt_id, ticket, *, webgl_base_url: str | None = None, services: CoCreationServices | None = None):
    base_url = (
        webgl_base_url
        if webgl_base_url is not None
        else (services.settings.webgl_base_url if services is not None else DEFAULT_WEBGL_BASE_URL)
    )
    separator = '&' if '?' in base_url else '?'
    return f'{base_url}{separator}cocreationAttempt={quote(attempt_id)}&cocreationPlay={quote(ticket)}'


def clean_optional(value, maximum_length):
    if value is None:
        return None
    return str(value).strip()[:maximum_length]


def utc_now():
    return iso_time(datetime.now(timezone.utc))


def error_response(status_code, code, message, request_id, retryable, details=None):
    payload = {
        'code': str(code),
        'message': str(message),
        'requestId': str(request_id),
        'retryable': bool(retryable),
    }
    if details is not None:
        payload['details'] = details
    return JSONResponse(status_code=status_code, content=payload)
