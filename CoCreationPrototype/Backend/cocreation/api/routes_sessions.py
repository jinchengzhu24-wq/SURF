"""Session lifecycle, language, Stage versions, assessments, play attempts, and finalization routes."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from typing import Literal

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response

from cocreation.clock import iso_time, parse_time
from cocreation.domain.design_context import set_active_disagreement
from cocreation.domain.level_validation import (
    HEIGHT,
    WIDTH,
    describe_diff,
    summarize_stage_changes,
    summarize_verified_diff,
)
from cocreation.llm.facade import LLMServiceError, PROMPT_VERSION
from cocreation.persistence.repository import (
    dump_json,
    get_current_version,
    get_session,
    get_version,
    load_design_context,
    load_json,
)

from .config_models import (
    ApiError,
    AssessmentRequest,
    BrowserAccessRequest,
    CreateSessionRequest,
    DEFAULT_TOKEN_SECRET,
    DemoSessionRequest,
    DraftRegenerationCompleteRequest,
    DraftRegenerationFailRequest,
    DraftRegenerationRequest,
    FinalizeRequest,
    IntentionRequest,
    LanguageRequest,
    LegacyChatRequest,
    MAX_INTENTION_LENGTH,
    PlayAttemptRequest,
    PlayBootstrapRequest,
    PlayMetricsRequest,
    RestoreRequest,
    SAMPLE_LEGEND,
    SAMPLE_ROWS,
    SESSION_COOKIE_NAME,
    TranslationRequest,
    VersionRequest,
    _solve_or_api_error,
    _validate_identifier,
    _validate_metrics,
    clean_optional,
    hash_token,
    require_active_session,
    require_browser_session,
    require_current_base,
    start_deadline_if_missing,
    token_matches,
)
from .context_and_discovery import (
    _design_context_guidance,
    _public_guidance,
    _record_disagreement_event,
    _update_design_context_from_turn,
    build_llm_context,
    record_intent_evidence,
    record_intent_hypothesis,
)
from .online_sync_and_dashboard import (
    _displayed_cards,
    synchronize_final_intention_with_online_match,
    synchronize_manual_edit_review_nodes,
    synchronize_opening_with_online_match,
    synchronize_version_with_online_match,
)
from .proposal_and_challenge import (
    _mark_initial_human_edit_disagreement_card,
    _mark_new_discussion_guidance,
    _record_reply_outcome,
)
from .services import CoCreationServices, get_services
from .session_persistence_helpers import (
    _create_session_record,
    _draft_regeneration_timeout_expired,
    _expire_draft_regeneration,
    _insert_decision,
    _insert_version,
    _pending_assistant_translations,
    _promote_draft_preview,
    _require_draft_preview,
    _translated_guidance,
    expire_interrupted_attempts,
    has_expired_interrupted_attempts,
    validate_legacy_conversation,
)

router = APIRouter()


@router.get('/health')
def health():
    return {'status': 'ok'}


@router.get('/ready')
def ready(services: CoCreationServices = Depends(get_services)):
    db_path = services.settings.database_path
    database_ready = True if str(db_path) == ':memory:' else db_path.parent.exists()
    return {
        'status': 'ready' if database_ready else 'not_ready',
        'database': 'ready' if database_ready else 'not_ready',
        'tokenSecretConfigured': services.settings.token_secret_bytes != DEFAULT_TOKEN_SECRET.encode('utf-8'),
    }


@router.get('/api/sample')
def get_sample(services: CoCreationServices = Depends(get_services)):
    validation = services.llm.solve_stage(SAMPLE_ROWS)
    return {
        'width': WIDTH,
        'height': HEIGHT,
        'rows': list(SAMPLE_ROWS),
        'legend': dict(SAMPLE_LEGEND),
        'validation': validation.as_dict(),
    }


@router.post('/api/chat')
def legacy_chat(
    payload: LegacyChatRequest,
    request: Request,
    response: Response,
    services: CoCreationServices = Depends(get_services),
):
    if not services.settings.legacy_chat_enabled:
        raise ApiError(404, 'LEGACY_CHAT_DISABLED', 'The legacy chat endpoint is disabled.')
    messages = [message.model_dump() for message in payload.messages]
    validate_legacy_conversation(messages)
    validation = services.llm.solve_stage(SAMPLE_ROWS)
    execution = services.llm.generate_chat_reply(
        messages,
        SAMPLE_ROWS,
        request.state.request_id,
        solver_metrics=validation.as_dict(),
        stage_context={'deferRevisionExecution': True},
        _deadline=request.state.llm_deadline,
    )
    response.headers['X-LLM-Attempts-Used'] = str(execution.attempts_used)
    return {'assistantMessage': execution.assistant_message, 'requestId': execution.request_id}


@router.post('/api/sessions')
def create_session(
    payload: CreateSessionRequest,
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    session_id, _version_id, bootstrap_token, integration_token = _create_session_record(
        rows=payload.rows,
        initial_draft_method=payload.initialDraftMethod,
        language=payload.language,
        idempotency_key=payload.idempotencyKey,
        match_id=payload.matchId,
        player_number=payload.playerNumber,
        services=services,
    )
    return {
        'sessionId': session_id,
        'launchUrl': services.build_launch_url(session_id, bootstrap_token),
        'integrationToken': integration_token,
    }


@router.post('/api/demo-sessions')
def create_demo_session(
    payload: DemoSessionRequest,
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    generated = services.demo_level_generator()
    session_id, _version_id, bootstrap_token, _integration_token = _create_session_record(
        rows=list(generated.rows),
        initial_draft_method='algorithm_demo',
        language=payload.language,
        idempotency_key=payload.idempotencyKey,
        demo_mode=True,
        initial_summary='Algorithm-generated demo map',
        generation_seed=generated.seed,
        generation_attempts=generated.attempts,
        generation_summary=generated.generation_summary,
        validation=getattr(generated, 'validation', None),
        services=services,
    )
    return {
        'sessionId': session_id,
        'launchUrl': services.build_launch_url(session_id, bootstrap_token, mode='demo'),
    }


@router.post('/api/sessions/{session_id}/draft-regenerations')
def request_draft_regeneration(
    session_id: str,
    payload: DraftRegenerationRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    is_demo = False
    with services.connect() as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        _require_draft_preview(database, session)
        expired, _, _ = _draft_regeneration_timeout_expired(session, services=services)
        if not expired:
            if session['draft_regeneration_key'] == payload.idempotencyKey:
                return services.serialize_session(database, session_id)
            if session['draft_regeneration_status'] in {'pending', 'claimed'}:
                raise ApiError(409, 'DRAFT_REGENERATION_ACTIVE', 'Draft regeneration is already running.')
        is_demo = bool(session['demo_mode'])

    generated_validation = None
    if is_demo:
        generated = services.demo_level_generator()
        generated_validation = getattr(generated, 'validation', None) or _solve_or_api_error(
            list(generated.rows),
            services=services,
        )

    with services.connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session, services=services)
        if session['draft_regeneration_key'] == payload.idempotencyKey:
            return services.serialize_session(database, session_id)
        if session['draft_regeneration_status'] in {'pending', 'claimed'}:
            raise ApiError(409, 'DRAFT_REGENERATION_ACTIVE', 'Draft regeneration is already running.')
        if bool(session['demo_mode']):
            if generated_validation is None:
                generated = services.demo_level_generator()
                generated_validation = getattr(generated, 'validation', None) or _solve_or_api_error(
                    list(generated.rows),
                    services=services,
                )
            request_id = uuid.uuid4().hex
            now = services.utc_now()
            database.execute(
                "UPDATE design_sessions\n"
                "                   SET draft_rows_json = ?, draft_validation_json = ?,\n"
                "                       draft_generation = draft_generation + 1,\n"
                "                       draft_regeneration_status = 'completed',\n"
                "                       draft_regeneration_request_id = ?, draft_regeneration_key = ?,\n"
                "                       draft_regeneration_requested_at = ?, draft_regeneration_updated_at = ?,\n"
                "                       draft_regeneration_failure_code = NULL, updated_at = ?\n"
                "                   WHERE id = ?",
                (
                    dump_json(list(generated_validation.rows)),
                    dump_json(generated_validation.as_dict()),
                    request_id,
                    payload.idempotencyKey,
                    now,
                    now,
                    now,
                    session_id,
                ),
            )
            services.record_event(
                database,
                session_id,
                'draft_regeneration_completed',
                {
                    'requestId': request_id,
                    'generation': int(session['draft_generation'] or 1) + 1,
                    'mode': 'algorithm_demo',
                },
                now,
            )
            return services.serialize_session(database, session_id)
        request_id = uuid.uuid4().hex
        now = services.utc_now()
        database.execute(
            "UPDATE design_sessions\n"
            "               SET draft_regeneration_status = 'pending',\n"
            "                   draft_regeneration_request_id = ?, draft_regeneration_key = ?,\n"
            "                   draft_regeneration_requested_at = ?, draft_regeneration_updated_at = ?,\n"
            "                   draft_regeneration_failure_code = NULL, updated_at = ?\n"
            "               WHERE id = ?",
            (request_id, payload.idempotencyKey, now, now, now, session_id),
        )
        services.record_event(
            database,
            session_id,
            'draft_regeneration_requested',
            {
                'requestId': request_id,
                'generation': int(session['draft_generation'] or 1) + 1,
                'mode': 'unity',
            },
            now,
        )
        return services.serialize_session(database, session_id)


@router.post('/api/sessions/{session_id}/draft-regenerations/{request_id}/cancel')
def cancel_draft_regeneration(
    session_id: str,
    request_id: str,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    with services.connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session, services=services)
        if session['draft_regeneration_request_id'] != request_id:
            raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
        if session['draft_regeneration_status'] in {'completed', 'failed', 'cancelled', 'timed_out'}:
            return services.serialize_session(database, session_id)
        if session['draft_regeneration_status'] != 'pending':
            return services.serialize_session(database, session_id)
        now = services.utc_now()
        database.execute(
            "UPDATE design_sessions SET draft_regeneration_status = 'cancelled',\n"
            "               draft_regeneration_updated_at = ?, draft_regeneration_failure_code = 'unity_unavailable',\n"
            "               updated_at = ? WHERE id = ?",
            (now, now, session_id),
        )
        services.record_event(database, session_id, 'draft_regeneration_cancelled', {'requestId': request_id}, now)
        return services.serialize_session(database, session_id)


@router.post('/api/sessions/{session_id}/browser-access')
def exchange_browser_access(
    session_id: str,
    payload: BrowserAccessRequest,
    response: Response,
    services: CoCreationServices = Depends(get_services),
):
    with services.connect(immediate=True) as database:
        session = get_session(database, session_id)
        if session is None or not token_matches(payload.bootstrapToken, session['bootstrap_hash']):
            raise ApiError(401, 'INVALID_BOOTSTRAP_TOKEN', 'The session link is invalid.')
        if session['bootstrap_used_at'] is not None:
            raise ApiError(409, 'BOOTSTRAP_TOKEN_USED', 'The session link was already used.')
        accessed_at = services.utc_now()
        database.execute(
            'UPDATE design_sessions\n               SET bootstrap_used_at = ?\n               WHERE id = ?',
            (accessed_at, session_id),
        )
        services.record_event(
            database,
            session_id,
            'browser_access_granted',
            {'deadlineAt': session['deadline_at']},
            accessed_at,
        )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        services.derive_token('access', session_id),
        httponly=True,
        samesite='lax',
        secure=services.settings.public_base_url.startswith('https://'),
        max_age=60 * 60 * 24 * 30,
        path='/',
    )
    return {'sessionId': session_id}


def _require_read_session(database, session_id, access_cookie, session_token, *, services: CoCreationServices):
    try:
        return require_browser_session(database, session_id, access_cookie, services=services)
    except ApiError as error:
        if error.code == 'SESSION_ACCESS_DENIED' and session_token:
            return require_browser_session(database, session_id, session_token, services=services)
        raise


@router.get('/api/sessions/{session_id}')
def read_session(
    session_id: str,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    session_token: str | None = Header(None, alias='X-Session-Token'),
    services: CoCreationServices = Depends(get_services),
):
    with services.connect() as database:
        session = _require_read_session(database, session_id, access_cookie, session_token, services=services)
        draft_expired, _, _ = _draft_regeneration_timeout_expired(session, services=services)
        attempts_expired = has_expired_interrupted_attempts(database, session_id, services=services)
        if not (draft_expired or attempts_expired):
            return services.serialize_session(database, session['id'])
    with services.connect(immediate=True) as database:
        session = _require_read_session(database, session_id, access_cookie, session_token, services=services)
        session = _expire_draft_regeneration(database, session, services=services)
        expire_interrupted_attempts(database, session_id, services=services)
        return services.serialize_session(database, session['id'])


@router.patch('/api/sessions/{session_id}/language')
def change_language(
    session_id: str,
    payload: LanguageRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    replay_result = None
    replay_first_id = None
    with services.connect() as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        if session['status'] != 'active':
            raise ApiError(409, 'SESSION_LOCKED', 'This co-creation session is no longer editable.')
        if session['language_locked_at'] is not None:
            if session['language'] != payload.language:
                raise ApiError(409, 'LANGUAGE_LOCKED', 'The session language was already selected and cannot be changed.')
            replay_result = services.serialize_session(database, session['id'])
            first = database.execute(
                "SELECT id FROM level_versions WHERE session_id = ? AND stage_number = 1 AND source = 'initial'",
                (session_id,),
            ).fetchone()
            replay_first_id = first['id'] if first is not None else None
    if replay_result is not None:
        if replay_first_id is not None:
            synchronize_version_with_online_match(session_id, replay_first_id, 'first_stage', services=services)
        return replay_result

    promoted_version_id = None
    should_sync_first_stage = False
    with services.connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        if session['status'] != 'active':
            raise ApiError(409, 'SESSION_LOCKED', 'This co-creation session is no longer editable.')
        if session['language_locked_at'] is not None:
            if session['language'] != payload.language:
                raise ApiError(409, 'LANGUAGE_LOCKED', 'The session language was already selected and cannot be changed.')
            result = services.serialize_session(database, session['id'])
            first = database.execute(
                "SELECT id FROM level_versions WHERE session_id = ? AND stage_number = 1 AND source = 'initial'",
                (session_id,),
            ).fetchone()
            promoted_version_id = first['id'] if first is not None else None
            should_sync_first_stage = promoted_version_id is not None
        else:
            if services.session_deadline_expired(session):
                raise ApiError(409, 'SESSION_DEADLINE_EXPIRED', 'Time is up. Submit the current map as the final Stage now.')
            now = services.utc_now()
            promoted_version_id, promoted = _promote_draft_preview(database, session, now, services=services)
            should_sync_first_stage = promoted and (not bool(session['demo_mode']))
            session = get_session(database, session_id)
            database.execute(
                'UPDATE design_sessions\n               SET language = ?, language_locked_at = ?, updated_at = ?\n               WHERE id = ?',
                (payload.language, now, now, session_id),
            )
            _deadline_started_at, deadline_at = start_deadline_if_missing(database, session, services=services)
            services.record_event(
                database,
                session_id,
                'language_selected',
                {'language': payload.language, 'deadlineAt': deadline_at},
                now,
            )
            result = services.serialize_session(database, session['id'])
    if should_sync_first_stage and promoted_version_id is not None:
        synchronize_version_with_online_match(session_id, promoted_version_id, 'first_stage', services=services)
    return result


@router.post('/api/sessions/{session_id}/translations/{language}')
def translate_session_turns(
    session_id: str,
    language: Literal['en', 'zh-CN'],
    payload: TranslationRequest,
    request: Request,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    with services.connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie, services=services)
        pending = _pending_assistant_translations(database, session_id, language, payload.turnIds)
        if not pending:
            return services.serialize_session(database, session['id'])
    first_error = None
    translation_deadline = request.state.llm_deadline
    completed_turn_ids = []
    failed_turn_ids = []
    for source in pending:
        try:
            execution = services.llm.translate_turns(
                [source],
                language,
                f"{request.state.request_id}:{source['turnId']}",
                _deadline=translation_deadline,
            )
            translated = execution.translations[0]
        except LLMServiceError as exception:
            first_error = first_error or exception
            failed_turn_ids.append(source['turnId'])
            continue
        with services.connect(immediate=True) as database:
            session = require_active_session(database, session_id, access_cookie, services=services)
            guidance = _translated_guidance(source['guidance'], translated)
            database.execute(
                '\n                INSERT OR IGNORE INTO turn_translations(\n'
                '                    id, session_id, turn_id, language, body, guidance_json,\n'
                '                    proposal_summary, model, attempts_used, latency_ms, created_at\n'
                '                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ',
                (
                    uuid.uuid4().hex,
                    session_id,
                    translated['turnId'],
                    language,
                    translated['body'],
                    dump_json(guidance) if guidance else None,
                    translated['proposalSummary'],
                    execution.model,
                    execution.attempts_used,
                    execution.latency_ms,
                    services.utc_now(),
                ),
            )
            services.record_event(
                database,
                session_id,
                'turn_translations_created',
                {'language': language, 'turnIds': [translated['turnId']], 'model': execution.model},
                services.utc_now(),
            )
        completed_turn_ids.append(source['turnId'])
    if first_error is not None:
        first_error.details = {
            'task': 'translation',
            'failureStage': 'translation_validation',
            'completedTurnIds': completed_turn_ids,
            'pendingTurnIds': failed_turn_ids,
            'committed': bool(completed_turn_ids),
            'maximumAttempts': 2,
            'recoveryState': 'retry_pending',
        }
        raise first_error
    with services.connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie, services=services)
        return services.serialize_session(database, session['id'])


@router.post('/api/sessions/{session_id}/versions')
def create_manual_version(
    session_id: str,
    payload: VersionRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    version_id = None
    session_payload = None
    existing_rows = None
    existing_parent_id = None
    with services.connect() as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        existing = database.execute(
            '\n            SELECT * FROM level_versions\n            WHERE session_id = ? AND idempotency_key = ?\n            ',
            (session_id, payload.idempotencyKey),
        ).fetchone()
        if existing is not None:
            existing_rows = load_json(existing['rows_json'])
            existing_parent_id = existing['parent_version_id']
            version_id = existing['id']
            if existing_rows == list(payload.rows) and existing_parent_id == payload.baseVersionId:
                session_payload = services.serialize_session(database, session_id)
        else:
            session = require_active_session(database, session_id, access_cookie, services=services)
            require_current_base(session, payload.baseVersionId)
    if session_payload is None:
        validation = _solve_or_api_error(payload.rows, services=services)
        if version_id is not None:
            if existing_rows != list(validation.rows) or existing_parent_id != payload.baseVersionId:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The Stage save key was already used for different input.')
            with services.connect() as database:
                session_payload = services.serialize_session(database, session_id)
        else:
            with services.connect(immediate=True) as database:
                existing = database.execute(
                    '\n                    SELECT * FROM level_versions\n                    WHERE session_id = ? AND idempotency_key = ?\n                    ',
                    (session_id, payload.idempotencyKey),
                ).fetchone()
                if existing is not None:
                    if load_json(existing['rows_json']) != list(validation.rows) or existing['parent_version_id'] != payload.baseVersionId:
                        raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The Stage save key was already used for different input.')
                    version_id = existing['id']
                else:
                    session = require_active_session(database, session_id, access_cookie, services=services)
                    require_current_base(session, payload.baseVersionId)
                    current = get_current_version(database, session)
                    current_rows = load_json(current['rows_json'])
                    if list(validation.rows) == current_rows:
                        raise ApiError(400, 'UNCHANGED_LEVEL', 'Save requires at least one tile change.')
                    inherited_design_context = load_design_context(database, session_id, current['id'])
                    previous_manual_disagreement = inherited_design_context.get('activeDisagreement') or {}
                    closes_manual_disagreement = (
                        previous_manual_disagreement.get('status') == 'active'
                        and previous_manual_disagreement.get('subject') == 'human_edit'
                    )
                    if closes_manual_disagreement:
                        inherited_design_context = set_active_disagreement(inherited_design_context, None, current['id'], None)
                    version_id = _insert_version(
                        database,
                        session,
                        validation,
                        'human_edit',
                        clean_optional(payload.summary, 1000) or 'Designer saved an edited stage',
                        payload.idempotencyKey,
                        current,
                        design_context=inherited_design_context,
                        services=services,
                    )
                    if closes_manual_disagreement:
                        resolved_disagreement = {
                            **previous_manual_disagreement,
                            'status': 'resolved',
                            'resolution': 'user',
                            'displayCard': False,
                            'resolutionMode': 'manual_reedit',
                            'resultVersionId': version_id,
                        }
                        _record_disagreement_event(
                            database,
                            session_id,
                            current['id'],
                            None,
                            {'disagreement': resolved_disagreement},
                            services=services,
                        )
                    record_intent_evidence(
                        database,
                        session_id,
                        version_id,
                        'manual_edit',
                        clean_optional(payload.summary, 1000) or 'Designer saved a validated manual edit.',
                        details={
                            'parentVersionId': current['id'],
                            'diff': describe_diff(current_rows, validation.rows),
                            'changeSummary': summarize_stage_changes(current_rows, validation.rows),
                            'verifiedDiff': summarize_verified_diff(current_rows, validation.rows, session['language']),
                            'validation': validation.as_dict(),
                        },
                        services=services,
                    )
                session_payload = services.serialize_session(database, session_id)
    synchronize_version_with_online_match(session_id, version_id, 'stage', services=services)
    with services.connect() as database:
        saved_version = get_version(database, session_id, version_id)
        parent_id = saved_version['parent_version_id']
    if parent_id:
        synchronize_manual_edit_review_nodes(session_id, parent_id, services=services)
    return session_payload


def _resolve_existing_restore_version(database, session_id, version_id, payload, existing_decision):
    if existing_decision['decision_type'] != 'restore':
        raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The request key belongs to another decision.')
    restored = get_version(database, session_id, existing_decision['version_id'])
    if restored['parent_version_id'] != version_id:
        raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The request key belongs to another restored Stage.')
    event = database.execute(
        "SELECT payload_json FROM audit_events WHERE session_id = ? AND event_type = 'stage_restored' "
        "AND json_extract(payload_json, '$.versionId') = ?",
        (session_id, restored['id']),
    ).fetchone()
    if event and load_json(event['payload_json'])['baseVersionId'] != payload.baseVersionId:
        raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The request key belongs to another restore request.')
    return restored['id']


@router.post('/api/sessions/{session_id}/versions/{version_id}/restore')
def restore_version(
    session_id: str,
    version_id: str,
    payload: RestoreRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    new_version_id = None
    result = None
    source_rows = None
    with services.connect() as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        existing_decision = database.execute(
            '\n            SELECT id, version_id, decision_type FROM designer_decisions\n'
            '            WHERE session_id = ? AND idempotency_key = ?\n            ',
            (session_id, payload.idempotencyKey),
        ).fetchone()
        if existing_decision is not None:
            new_version_id = _resolve_existing_restore_version(database, session_id, version_id, payload, existing_decision)
            result = services.serialize_session(database, session_id)
        else:
            session = require_active_session(database, session_id, access_cookie, services=services)
            require_current_base(session, payload.baseVersionId)
            source = get_version(database, session_id, version_id)
            if source is None:
                raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
            current = get_current_version(database, session)
            if source['id'] == current['id']:
                raise ApiError(409, 'UNCHANGED_LEVEL', 'The selected Stage is already the current Stage.')
            source_rows = load_json(source['rows_json'])
    if result is not None:
        synchronize_version_with_online_match(session_id, new_version_id, 'stage', services=services)
        return result
    validation = _solve_or_api_error(source_rows, services=services)
    with services.connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        existing_decision = database.execute(
            '\n            SELECT id, version_id, decision_type FROM designer_decisions\n'
            '            WHERE session_id = ? AND idempotency_key = ?\n            ',
            (session_id, payload.idempotencyKey),
        ).fetchone()
        if existing_decision is not None:
            new_version_id = _resolve_existing_restore_version(database, session_id, version_id, payload, existing_decision)
        else:
            session = require_active_session(database, session_id, access_cookie, services=services)
            require_current_base(session, payload.baseVersionId)
            source = get_version(database, session_id, version_id)
            if source is None:
                raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
            current = get_current_version(database, session)
            if source['id'] == current['id']:
                raise ApiError(409, 'UNCHANGED_LEVEL', 'The selected Stage is already the current Stage.')
            new_version_id = _insert_version(
                database,
                session,
                validation,
                'restored',
                f"Continued from Stage {source['stage_number']}",
                'restore:' + payload.idempotencyKey,
                current,
                parent_version_id=source['id'],
                design_context=load_design_context(database, session_id, source['id']),
                services=services,
            )
            _insert_decision(database, session_id, new_version_id, None, 'restore', '', payload.idempotencyKey, services=services)
            record_intent_evidence(
                database,
                session_id,
                new_version_id,
                'stage_restore',
                f"Designer chose to continue from Stage {source['stage_number']}.",
                details={'sourceVersionId': source['id'], 'replacedVersionId': current['id']},
                services=services,
            )
            services.record_event(
                database,
                session_id,
                'stage_restored',
                {
                    'versionId': new_version_id,
                    'restoredFromVersionId': source['id'],
                    'restoredFromStageNumber': source['stage_number'],
                    'replacedVersionId': current['id'],
                    'replacedStageNumber': current['stage_number'],
                    'baseVersionId': payload.baseVersionId,
                },
                services.utc_now(),
            )
        result = services.serialize_session(database, session_id)
    synchronize_version_with_online_match(session_id, new_version_id, 'stage', services=services)
    return result


def _build_existing_opening_sync(version, existing, *, services: CoCreationServices):
    existing_opening_text = existing['assistant_text']
    if version['stage_number'] == 1:
        existing_opening_text = services.llm.repair_stage_one_opening_display(
            existing_opening_text,
            load_json(version['rows_json']),
            existing['language'],
        )
    return {
        'assistantTurnId': existing['assistant_turn_id'],
        'assistantText': existing_opening_text,
        'language': existing['language'],
        'cards': _displayed_cards(load_json(existing['guidance_json'])),
    }


@router.post('/api/sessions/{session_id}/versions/{version_id}/assessments')
def assess_version(
    session_id: str,
    version_id: str,
    payload: AssessmentRequest,
    request: Request,
    response: Response,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    existing_opening_sync = None
    existing_session_payload = None
    with services.connect() as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        version = get_version(database, session_id, version_id)
        if version is None:
            raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
        existing = database.execute(
            '\n            SELECT assessment.id, opening_turn.id AS assistant_turn_id,\n'
            '                   opening_turn.content AS assistant_text,\n'
            '                   opening_turn.language, opening_turn.guidance_json\n'
            '            FROM llm_assessments AS assessment\n'
            '            JOIN conversation_turns AS opening_turn\n'
            '              ON opening_turn.id = assessment.assistant_turn_id\n'
            '            WHERE assessment.version_id = ?\n            ',
            (version_id,),
        ).fetchone()
        if existing is not None:
            existing_opening_sync = _build_existing_opening_sync(version, existing, services=services)
            existing_session_payload = services.serialize_session(database, session_id)
        accepted_opening = database.execute(
            "\n            SELECT decision.id\n"
            "            FROM designer_decisions AS decision\n"
            "            JOIN change_proposals AS proposal\n"
            "              ON proposal.id = decision.proposal_id\n"
            "            WHERE decision.session_id = ?\n"
            "              AND decision.version_id = ?\n"
            "              AND decision.decision_type = 'accept'\n"
            "              AND proposal.assistant_turn_id IS NOT NULL\n"
            "            LIMIT 1\n            ",
            (session_id, version_id),
        ).fetchone()
        if existing_session_payload is not None:
            pass
        elif accepted_opening is not None:
            return services.serialize_session(database, session_id)
        if existing_session_payload is None:
            session = require_active_session(database, session_id, access_cookie, services=services)
            require_current_base(session, version_id)
            context = build_llm_context(database, session_id, version, services=services)
            session_language = session['language']
            context['stageContext']['discussionCardMode'] = 'disagreement_only'
            context['stageContext']['agentRole'] = 'evaluator'
    if existing_session_payload is not None:
        synchronize_opening_with_online_match(session_id, version_id, existing_opening_sync, services=services)
        synchronize_manual_edit_review_nodes(session_id, version_id, services=services)
        return existing_session_payload
    try:
        execution = services.llm.generate_stage_assessment(
            context['conversation'],
            context['rows'],
            session_language,
            context['validation'],
            context['playSummary'],
            request.state.request_id,
            stage_context=context['stageContext'],
            _deadline=request.state.llm_deadline,
        )
    except LLMServiceError as exception:
        with services.connect(immediate=True) as database:
            require_browser_session(database, session_id, access_cookie, services=services)
            services.record_event(
                database,
                session_id,
                'reply_generation_failed',
                {
                    'messageKey': payload.idempotencyKey,
                    'baseVersionId': version_id,
                    'task': 'manual_edit_review' if version['source'] == 'human_edit' else 'stage_opening',
                    'code': exception.code,
                    'attemptsUsed': exception.attempts_used,
                    'reliableBodyDelivered': False,
                    'fullQualityPassed': False,
                    'requestId': request.state.request_id,
                },
                services.utc_now(),
            )
        raise
    execution = _mark_new_discussion_guidance(execution, context['stageContext'])
    execution = _mark_initial_human_edit_disagreement_card(execution, context['stageContext'])
    review_execution = getattr(execution, 'secondary_execution', None)
    if review_execution is not None:
        review_execution = _mark_new_discussion_guidance(review_execution, context['stageContext'])
        review_execution = _mark_initial_human_edit_disagreement_card(review_execution, context['stageContext'])
    review_metadata = dict(review_execution.guidance.get('manualEditReview') or {}) if review_execution is not None else {}
    design_context_guidance = _design_context_guidance(
        review_execution.guidance if review_execution is not None else execution.guidance
    )
    execution = replace(execution, guidance=_public_guidance(execution.guidance))
    if review_execution is not None:
        review_execution = replace(review_execution, guidance=_public_guidance(review_execution.guidance))
    manual_progress_rewrite = None
    if context['stageContext'].get('source') == 'human_edit':
        change_summary = context['stageContext'].get('changeSummary') or {}
        canonical_manual = clean_optional(context['stageContext'].get('summary'), 1200) or 'Designer saved a validated manual edit.'
        try:
            manual_progress_rewrite = services.llm.rewrite_intent_progress(
                'manual_edit',
                canonical_manual,
                [
                    dump_json(change_summary),
                    str(execution.assistant_message or '')[:1200],
                    str(review_execution.assistant_message if review_execution is not None else '')[:1200],
                ],
                session_language,
                request.state.request_id,
                _deadline=min(services.monotonic() + 10.0, request.state.llm_deadline - 20.0),
            )
        except (LLMServiceError, TypeError, ValueError, KeyError):
            manual_progress_rewrite = {
                'detailedText': str(execution.assistant_message or '')[:1200],
                'summaryText': canonical_manual,
                'model': 'deterministic-fallback',
            }
    response.headers['X-LLM-Attempts-Used'] = str(execution.attempts_used)
    opening_sync = None
    with services.connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        existing = database.execute(
            '\n            SELECT assessment.id, opening_turn.id AS assistant_turn_id,\n'
            '                   opening_turn.content AS assistant_text,\n'
            '                   opening_turn.language, opening_turn.guidance_json\n'
            '            FROM llm_assessments AS assessment\n'
            '            JOIN conversation_turns AS opening_turn\n'
            '              ON opening_turn.id = assessment.assistant_turn_id\n'
            '            WHERE assessment.version_id = ?\n            ',
            (version_id,),
        ).fetchone()
        if existing is None:
            session = require_active_session(database, session_id, access_cookie, services=services)
            require_current_base(session, version_id)
            turn_id = services.insert_turn(
                database,
                session,
                'assistant',
                execution.assistant_message,
                version_id,
                execution.request_id,
                execution,
            )
            review_turn_id = None
            if review_execution is not None:
                review_turn_id = services.insert_turn(
                    database,
                    session,
                    'assistant',
                    review_execution.assistant_message,
                    version_id,
                    review_execution.request_id,
                    review_execution,
                )
            assessment_id = uuid.uuid4().hex
            database.execute(
                '\n                INSERT INTO llm_assessments(\n'
                '                    id, session_id, version_id, assistant_turn_id,\n'
                '                    payload_json, prompt_version, created_at\n'
                '                ) VALUES (?, ?, ?, ?, ?, ?, ?)\n                ',
                (
                    assessment_id,
                    session_id,
                    version_id,
                    turn_id,
                    dump_json(execution.assessment),
                    PROMPT_VERSION,
                    services.utc_now(),
                ),
            )
            record_intent_hypothesis(
                database,
                session_id,
                version_id,
                turn_id,
                execution.guidance,
                'stage_assessment',
                services=services,
            )
            _record_reply_outcome(
                database,
                session_id,
                payload.idempotencyKey,
                version_id,
                execution,
                'manual_edit_review' if version['source'] == 'human_edit' else 'stage_opening',
                services=services,
            )
            if version['source'] == 'human_edit':
                services.record_event(
                    database,
                    session_id,
                    'human_edit_reviewed',
                    {
                        'versionId': version_id,
                        'turnId': review_turn_id or turn_id,
                        'openingTurnId': turn_id,
                        'reviewTurnId': review_turn_id,
                        'diff': load_json(version['diff_json']),
                        'changeSummary': context['stageContext'].get('changeSummary'),
                        'hasWarning': False,
                        'reviewOutcome': review_metadata.get('outcome'),
                        'evidenceIds': review_metadata.get('evidenceIds', []),
                        'decisionSource': review_metadata.get('decisionSource'),
                        'confirmedDirectionIds': review_metadata.get('confirmedDirectionIds', []),
                        'effectEvidenceIds': review_metadata.get('effectEvidenceIds', []),
                        'relation': review_metadata.get('relation'),
                        'solverDelta': review_metadata.get('solverDelta', {}),
                        'comparisons': review_metadata.get('comparisons', []),
                        'disagreement': review_execution.guidance.get('disagreement') if review_execution is not None else None,
                    },
                    services.utc_now(),
                )
                services.record_event(
                    database,
                    session_id,
                    'intent_progress_rewrite',
                    {
                        'artifactType': 'manual_edit',
                        'artifactId': version_id,
                        'versionId': version_id,
                        'language': session_language,
                        **(manual_progress_rewrite or {}),
                    },
                    services.utc_now(),
                )
            if review_execution is not None:
                _record_disagreement_event(
                    database,
                    session_id,
                    version_id,
                    review_turn_id,
                    review_execution.guidance,
                    services=services,
                )
            _update_design_context_from_turn(
                database,
                session_id,
                version_id,
                None,
                review_turn_id or turn_id,
                design_context_guidance,
                assistant_content=review_execution.assistant_message if review_execution is not None else execution.assistant_message,
                allow_progress=False,
                services=services,
            )
            opening_sync = {
                'assistantTurnId': turn_id,
                'assistantText': execution.assistant_message,
                'language': session['language'],
                'cards': _displayed_cards(execution.guidance),
            }
        else:
            current_version = get_version(database, session_id, version_id)
            opening_sync = _build_existing_opening_sync(current_version, existing, services=services)
        session_payload = services.serialize_session(database, session_id)
    if opening_sync is not None:
        synchronize_opening_with_online_match(session_id, version_id, opening_sync, services=services)
        synchronize_manual_edit_review_nodes(session_id, version_id, services=services)
    return session_payload


@router.post('/api/sessions/{session_id}/versions/{version_id}/play-attempts')
def create_play_attempt(
    session_id: str,
    version_id: str,
    payload: PlayAttemptRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    with services.connect() as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        version = get_version(database, session_id, version_id)
        if version is None:
            raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
        existing = database.execute(
            '\n            SELECT * FROM play_attempts\n            WHERE session_id = ? AND idempotency_key = ?\n            ',
            (session_id, payload.idempotencyKey),
        ).fetchone()
        if existing is not None:
            if existing['version_id'] != version_id:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The Play key was already used for another Stage.')
            attempt_id = existing['id']
            ticket = services.derive_token('play-ticket', attempt_id)
            return {'attemptId': attempt_id, 'playUrl': services.build_play_url(attempt_id, ticket)}
        require_active_session(database, session_id, access_cookie, services=services)
    with services.connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        version = get_version(database, session_id, version_id)
        if version is None:
            raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
        existing = database.execute(
            '\n            SELECT * FROM play_attempts\n            WHERE session_id = ? AND idempotency_key = ?\n            ',
            (session_id, payload.idempotencyKey),
        ).fetchone()
        if existing is None:
            session = require_active_session(database, session_id, access_cookie, services=services)
            attempt_id = uuid.uuid4().hex
            ticket = services.derive_token('play-ticket', attempt_id)
            now_value = services.utc_datetime()
            issued_at = iso_time(now_value)
            expires_at = iso_time(now_value + services.play_ticket_lifetime_timedelta())
            validation = load_json(version['validation_json'])
            database.execute(
                "\n                INSERT INTO play_attempts(\n"
                "                    id, session_id, version_id, initial_draft_method,\n"
                "                    language, status, ticket_hash, ticket_expires_at,\n"
                "                    issued_at, minimum_moves, minimum_pushes, idempotency_key\n"
                "                ) VALUES (?, ?, ?, ?, ?, 'issued', ?, ?, ?, ?, ?, ?)\n                ",
                (
                    attempt_id,
                    session_id,
                    version_id,
                    session['initial_draft_method'],
                    session['language'],
                    hash_token(ticket),
                    expires_at,
                    issued_at,
                    validation.get('solutionSteps', -1),
                    validation.get('solutionPushes', -1),
                    payload.idempotencyKey,
                ),
            )
            services.record_event(
                database,
                session_id,
                'play_attempt_issued',
                {'attemptId': attempt_id, 'versionId': version_id},
                issued_at,
            )
        else:
            if existing['version_id'] != version_id:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The Play key was already used for another Stage.')
            attempt_id = existing['id']
            ticket = services.derive_token('play-ticket', attempt_id)
    return {'attemptId': attempt_id, 'playUrl': services.build_play_url(attempt_id, ticket)}


@router.post('/api/play-attempts/{attempt_id}/bootstrap')
def bootstrap_play_attempt(
    attempt_id: str,
    payload: PlayBootstrapRequest,
    services: CoCreationServices = Depends(get_services),
):
    with services.connect(immediate=True) as database:
        attempt = database.execute('SELECT * FROM play_attempts WHERE id = ?', (attempt_id,)).fetchone()
        if attempt is None or not token_matches(payload.ticket, attempt['ticket_hash']):
            raise ApiError(401, 'INVALID_PLAY_TICKET', 'The Play ticket is invalid.')
        if attempt['ticket_used_at'] is not None:
            raise ApiError(409, 'PLAY_TICKET_USED', 'The Play ticket was already used.')
        if parse_time(attempt['ticket_expires_at']) < services.utc_datetime():
            raise ApiError(410, 'PLAY_TICKET_EXPIRED', 'The Play ticket has expired.')
        session = get_session(database, attempt['session_id'])
        if session is None:
            raise ApiError(404, 'SESSION_NOT_FOUND', 'The co-creation session was not found.')
        if session['status'] != 'active':
            raise ApiError(409, 'SESSION_LOCKED', 'This co-creation session is no longer editable.')
        if services.session_deadline_expired(session):
            raise ApiError(409, 'SESSION_DEADLINE_EXPIRED', 'Time is up. Submit the current map as the final Stage now.')
        version = get_version(database, attempt['session_id'], attempt['version_id'])
        if version is None:
            raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
        attempt_token = services.derive_token('play-attempt', attempt_id)
        now = services.utc_now()
        database.execute(
            "\n            UPDATE play_attempts\n"
            "            SET ticket_used_at = ?, attempt_token_hash = ?, loaded_at = ?, status = 'loaded'\n"
            "            WHERE id = ?\n            ",
            (now, hash_token(attempt_token), now, attempt_id),
        )
        return {
            'attemptId': attempt_id,
            'sessionId': attempt['session_id'],
            'versionId': attempt['version_id'],
            'rows': load_json(version['rows_json']),
            'initialDraftMethod': attempt['initial_draft_method'],
            'language': attempt['language'],
            'attemptToken': attempt_token,
            'returnUrl': services.build_return_url(session['id'], version['id']),
        }


@router.post('/api/play-attempts/{attempt_id}/start')
def start_play_attempt(
    attempt_id: str,
    payload: PlayMetricsRequest,
    services: CoCreationServices = Depends(get_services),
):
    return update_play_attempt(attempt_id, payload, 'started', services=services)


@router.post('/api/play-attempts/{attempt_id}/progress')
def progress_play_attempt(
    attempt_id: str,
    payload: PlayMetricsRequest,
    services: CoCreationServices = Depends(get_services),
):
    return update_play_attempt(attempt_id, payload, 'started', services=services)


@router.post('/api/play-attempts/{attempt_id}/complete')
def complete_play_attempt(
    attempt_id: str,
    payload: PlayMetricsRequest,
    services: CoCreationServices = Depends(get_services),
):
    return update_play_attempt(attempt_id, payload, 'completed', services=services)


@router.post('/api/play-attempts/{attempt_id}/abandon')
def abandon_play_attempt(
    attempt_id: str,
    payload: PlayMetricsRequest,
    services: CoCreationServices = Depends(get_services),
):
    return update_play_attempt(attempt_id, payload, 'abandoned', services=services)


def _resolve_existing_finalization(database, session, session_id, payload, existing, request_signature):
    if existing['decision_type'] != 'finalize':
        raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The request key was already used for another decision.')
    final_version_id = existing['version_id'] or session['final_version_id'] or payload.baseVersionId
    deadline_stage_version_id = None
    committed = database.execute(
        "SELECT payload_json FROM audit_events WHERE session_id = ?\n"
        "                   AND event_type = 'finalization_request_committed'\n"
        "                   AND json_extract(payload_json, '$.idempotencyKey') = ?\n"
        "                   ORDER BY id DESC LIMIT 1",
        (session_id, payload.idempotencyKey),
    ).fetchone()
    committed_payload = load_json(committed['payload_json']) if committed else None
    if committed_payload is not None:
        if committed_payload.get('request') != request_signature:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The finalization key belongs to a different request.')
        final_version_id = committed_payload['finalVersionId']
        deadline_stage_version_id = committed_payload.get('deadlineStageVersionId')
    elif final_version_id != payload.baseVersionId:
        raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The finalization key was already used for another Stage.')
    return final_version_id, deadline_stage_version_id


def _validate_new_finalization_state(database, session, session_id, payload, *, services: CoCreationServices):
    if session['status'] != 'active':
        raise ApiError(409, 'SESSION_LOCKED', 'This co-creation session is no longer editable.')
    deadline_expired = services.session_deadline_expired(session)
    target_version = get_version(database, session_id, payload.baseVersionId)
    if target_version is None:
        raise ApiError(404, 'VERSION_NOT_FOUND', 'The selected Stage was not found.')
    historical_target = target_version['id'] != session['current_version_id']
    if historical_target and payload.rows is not None:
        raise ApiError(409, 'HISTORICAL_FINALIZE_ROWS_NOT_ALLOWED', 'An unsaved draft can only finalize the current Stage.')
    pending = database.execute(
        "\n        SELECT id FROM change_proposals\n        WHERE session_id = ? AND status = 'pending'\n        ",
        (session_id,),
    ).fetchall()
    if pending and (not deadline_expired) and (not historical_target):
        raise ApiError(409, 'PENDING_PROPOSAL', 'Decide the pending proposal first.')
    if not deadline_expired and not historical_target and payload.rows is not None:
        if list(payload.rows) != load_json(target_version['rows_json']):
            raise ApiError(409, 'UNSAVED_DRAFT_ROWS', 'Save the edited Stage before finalizing.')
    return deadline_expired, historical_target, pending


@router.post('/api/sessions/{session_id}/finalize')
def finalize_session(
    session_id: str,
    payload: FinalizeRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    deadline_stage_version_id = None
    request_signature = {
        'baseVersionId': payload.baseVersionId,
        'rowsHash': hashlib.sha256(dump_json(payload.rows).encode('utf-8')).hexdigest() if payload.rows is not None else None,
    }
    session_payload = None
    needs_deadline_solve = False
    with services.connect() as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        existing = database.execute(
            '\n            SELECT id, decision_type, version_id FROM designer_decisions\n'
            '            WHERE session_id = ? AND idempotency_key = ?\n            ',
            (session_id, payload.idempotencyKey),
        ).fetchone()
        if existing is not None:
            final_version_id, deadline_stage_version_id = _resolve_existing_finalization(
                database,
                session,
                session_id,
                payload,
                existing,
                request_signature,
            )
            session_payload = services.serialize_session(database, session_id)
        else:
            deadline_expired, _, _ = _validate_new_finalization_state(
                database,
                session,
                session_id,
                payload,
                services=services,
            )
            needs_deadline_solve = bool(deadline_expired and payload.rows is not None)
    if session_payload is None:
        validation = _solve_or_api_error(payload.rows, services=services) if needs_deadline_solve else None
        with services.connect(immediate=True) as database:
            session = require_browser_session(database, session_id, access_cookie, services=services)
            existing = database.execute(
                '\n                SELECT id, decision_type, version_id FROM designer_decisions\n'
                '                WHERE session_id = ? AND idempotency_key = ?\n                ',
                (session_id, payload.idempotencyKey),
            ).fetchone()
            if existing is not None:
                final_version_id, deadline_stage_version_id = _resolve_existing_finalization(
                    database,
                    session,
                    session_id,
                    payload,
                    existing,
                    request_signature,
                )
            else:
                deadline_expired, historical_target, pending = _validate_new_finalization_state(
                    database,
                    session,
                    session_id,
                    payload,
                    services=services,
                )
                final_version_id = payload.baseVersionId
                if deadline_expired and payload.rows is not None:
                    solved = validation if validation is not None else _solve_or_api_error(payload.rows, services=services)
                    current = get_current_version(database, session)
                    if list(solved.rows) != load_json(current['rows_json']):
                        final_version_id = _insert_version(
                            database,
                            session,
                            solved,
                            'human_edit',
                            'Designer edit saved at the co-creation deadline',
                            'deadline-finalize:' + payload.idempotencyKey,
                            current,
                            services=services,
                        )
                        deadline_stage_version_id = final_version_id
                        session = get_session(database, session_id)
                now = services.utc_now()
                if (historical_target or deadline_expired) and pending:
                    pending_proposal_ids = [row['id'] for row in pending]
                    database.execute(
                        "\n                        UPDATE change_proposals\n"
                        "                        SET status = 'superseded', decided_at = ?\n"
                        "                        WHERE session_id = ? AND status = 'pending'\n                        ",
                        (now, session_id),
                    )
                    event_name = (
                        'pending_proposals_superseded_by_historical_finalization'
                        if historical_target
                        else 'pending_proposals_superseded_by_deadline_finalization'
                    )
                    services.record_event(
                        database,
                        session_id,
                        event_name,
                        {'finalVersionId': final_version_id, 'proposalIds': pending_proposal_ids},
                        now,
                    )
                database.execute(
                    "\n                    UPDATE design_sessions\n"
                    "                    SET status = 'awaiting_intention', final_version_id = ?,\n"
                    "                        finalized_at = ?, updated_at = ? WHERE id = ?\n                    ",
                    (final_version_id, now, now, session_id),
                )
                _insert_decision(database, session_id, final_version_id, None, 'finalize', '', payload.idempotencyKey, services=services)
                services.record_event(
                    database,
                    session_id,
                    'finalization_request_committed',
                    {
                        'idempotencyKey': payload.idempotencyKey,
                        'request': request_signature,
                        'finalVersionId': final_version_id,
                        'deadlineStageVersionId': deadline_stage_version_id,
                    },
                    now,
                )
                if deadline_expired:
                    services.record_event(database, session_id, 'deadline_finalized', {'versionId': final_version_id}, now)
            session_payload = services.serialize_session(database, session_id)
    if deadline_stage_version_id:
        synchronize_version_with_online_match(session_id, deadline_stage_version_id, 'stage', services=services)
    synchronize_version_with_online_match(session_id, final_version_id, 'final', services=services)
    return session_payload


def _resolve_completed_intention_retry(database, session_id, session, content, idempotency_key):
    match_id = str(session['match_id'] or '').strip()
    is_demo_session = bool(session['demo_mode'])
    requires_online_sync = (not is_demo_session) and bool(match_id)
    existing = database.execute('SELECT content, idempotency_key FROM designer_intentions WHERE session_id = ?', (session_id,)).fetchone()
    synced_event = database.execute(
        "\n        SELECT id FROM audit_events\n"
        "        WHERE session_id = ? AND event_type = 'online_intention_synced'\n"
        "        LIMIT 1\n        ",
        (session_id,),
    ).fetchone()
    sync_event_id = 'message:' + session_id + ':' + idempotency_key
    if existing is not None:
        if existing['content'] != content:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The final designer intention has already been submitted.')
        same_key = existing['idempotency_key'] == idempotency_key
        allow_unsynced_replay = (not same_key) and requires_online_sync and (synced_event is None)
        if (not same_key) and (not allow_unsynced_replay):
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The final designer intention has already been submitted.')
        content = existing['content']
        sync_event_id = 'message:' + session_id + ':' + existing['idempotency_key']
    return content, sync_event_id, match_id, session['player_number'], is_demo_session, requires_online_sync


@router.post('/api/sessions/{session_id}/intention')
def submit_intention(
    session_id: str,
    payload: IntentionRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    services: CoCreationServices = Depends(get_services),
):
    _validate_identifier(payload.idempotencyKey, 'idempotencyKey')
    content = payload.content.strip()
    if not content or len(content) > MAX_INTENTION_LENGTH:
        raise ApiError(400, 'INVALID_INTENTION', 'The intention response is required.')
    needs_write = False
    with services.connect() as database:
        session = require_browser_session(database, session_id, access_cookie, services=services)
        if session['status'] == 'completed':
            (
                content,
                sync_event_id,
                match_id,
                player_number,
                is_demo_session,
                requires_online_sync,
            ) = _resolve_completed_intention_retry(database, session_id, session, content, payload.idempotencyKey)
        elif session['status'] != 'awaiting_intention':
            raise ApiError(409, 'SESSION_NOT_FINALIZED', 'Finalize a Stage first.')
        else:
            needs_write = True
    if needs_write:
        with services.connect(immediate=True) as database:
            session = require_browser_session(database, session_id, access_cookie, services=services)
            match_id = str(session['match_id'] or '').strip()
            player_number = session['player_number']
            is_demo_session = bool(session['demo_mode'])
            requires_online_sync = (not is_demo_session) and bool(match_id)
            sync_event_id = 'message:' + session_id + ':' + payload.idempotencyKey
            if session['status'] == 'completed':
                (
                    content,
                    sync_event_id,
                    match_id,
                    player_number,
                    is_demo_session,
                    requires_online_sync,
                ) = _resolve_completed_intention_retry(database, session_id, session, content, payload.idempotencyKey)
            elif session['status'] != 'awaiting_intention':
                raise ApiError(409, 'SESSION_NOT_FINALIZED', 'Finalize a Stage first.')
            else:
                now = services.utc_now()
                database.execute(
                    '\n                INSERT INTO designer_intentions(\n'
                    '                    id, session_id, content, language, idempotency_key, created_at\n'
                    '                ) VALUES (?, ?, ?, ?, ?, ?)\n                ',
                    (uuid.uuid4().hex, session_id, content, session['language'], payload.idempotencyKey, now),
                )
                database.execute(
                    "\n                UPDATE design_sessions\n"
                    "                SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?\n                ",
                    (now, now, session_id),
                )
                services.record_event(
                    database,
                    session_id,
                    'intention_submitted',
                    {'intentionSyncRequired': requires_online_sync},
                    now,
                )
    if not is_demo_session:
        synchronize_final_intention_with_online_match(
            match_id,
            player_number,
            content,
            sync_event_id,
            session_id,
            services=services,
        )
        if requires_online_sync:
            with services.connect(immediate=True) as database:
                synced_event = database.execute(
                    "\n                    SELECT id FROM audit_events\n"
                    "                    WHERE session_id = ? AND event_type = 'online_intention_synced'\n"
                    "                    LIMIT 1\n                    ",
                    (session_id,),
                ).fetchone()
                if synced_event is None:
                    services.record_event(
                        database,
                        session_id,
                        'online_intention_synced',
                        {'idempotencyKey': payload.idempotencyKey},
                        services.utc_now(),
                    )
    with services.connect() as database:
        return services.serialize_session(database, session_id)


def _build_integration_status_payload(database, session_id, session):
    payload = {
        'sessionId': session_id,
        'status': session['status'],
        'finalVersionId': session['final_version_id'],
        'finalRows': None,
        'designerIntention': None,
        'draftRegeneration': None,
    }
    if session['current_version_id'] is None and session['draft_regeneration_request_id']:
        payload['draftRegeneration'] = {
            'requestId': session['draft_regeneration_request_id'],
            'status': session['draft_regeneration_status'],
            'generation': int(session['draft_generation'] or 1) + (
                1 if session['draft_regeneration_status'] in {'pending', 'claimed'} else 0
            ),
            'requestedAt': session['draft_regeneration_requested_at'],
            'updatedAt': session['draft_regeneration_updated_at'],
            'failureCode': session['draft_regeneration_failure_code'],
        }
    if session['status'] == 'completed':
        intention_submitted = database.execute(
            "\n            SELECT payload_json FROM audit_events\n"
            "            WHERE session_id = ? AND event_type = 'intention_submitted'\n"
            "            ORDER BY id DESC LIMIT 1\n            ",
            (session_id,),
        ).fetchone()
        intention_payload = load_json(intention_submitted['payload_json']) if intention_submitted else {}
        intention_sync_required = bool(intention_payload.get('intentionSyncRequired'))
        online_synced = database.execute(
            "\n            SELECT 1 FROM audit_events\n"
            "            WHERE session_id = ? AND event_type = 'online_intention_synced'\n"
            "            LIMIT 1\n            ",
            (session_id,),
        ).fetchone()
        if intention_sync_required and online_synced is None:
            payload['status'] = 'awaiting_intention'
            return payload
        if session['final_version_id']:
            version = get_version(database, session_id, session['final_version_id'])
            payload['finalRows'] = load_json(version['rows_json'])
            intention = database.execute('SELECT content FROM designer_intentions WHERE session_id = ?', (session_id,)).fetchone()
            payload['designerIntention'] = intention['content'] if intention else None
    return payload


@router.get('/api/integrations/sessions/{session_id}')
def integration_status(
    session_id: str,
    authorization: str | None = Header(None, alias='Authorization'),
    services: CoCreationServices = Depends(get_services),
):
    with services.connect() as database:
        session = _require_integration_session(database, session_id, authorization)
        draft_expired, _, _ = _draft_regeneration_timeout_expired(session, services=services)
        if not draft_expired:
            return _build_integration_status_payload(database, session_id, session)
    with services.connect(immediate=True) as database:
        session = _require_integration_session(database, session_id, authorization)
        session = _expire_draft_regeneration(database, session, services=services)
        return _build_integration_status_payload(database, session_id, session)


def _require_integration_session(database, session_id, authorization):
    token = ''
    if authorization and authorization.startswith('Bearer '):
        token = authorization[7:].strip()
    session = get_session(database, session_id)
    if session is None or not token_matches(token, session['integration_hash']):
        raise ApiError(401, 'INVALID_INTEGRATION_TOKEN', 'Integration access was denied.')
    return session


@router.post('/api/integrations/sessions/{session_id}/draft-regenerations/{request_id}/claim')
def claim_draft_regeneration(
    session_id: str,
    request_id: str,
    authorization: str | None = Header(None, alias='Authorization'),
    services: CoCreationServices = Depends(get_services),
):
    with services.connect(immediate=True) as database:
        session = _require_integration_session(database, session_id, authorization)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session, services=services)
        if session['draft_regeneration_request_id'] != request_id:
            raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
        status = session['draft_regeneration_status']
        if status == 'claimed':
            return {'sessionId': session_id, 'requestId': request_id, 'status': status}
        if status != 'pending':
            raise ApiError(409, 'DRAFT_REGENERATION_NOT_PENDING', 'The regeneration request cannot be claimed.')
        now = services.utc_now()
        requested_at = session['draft_regeneration_requested_at']
        claim_delay_seconds = None
        if requested_at:
            claim_delay_seconds = max(0, round((parse_time(now) - parse_time(requested_at)).total_seconds(), 3))
        database.execute(
            "UPDATE design_sessions SET draft_regeneration_status = 'claimed',\n"
            "               draft_regeneration_updated_at = ?, updated_at = ? WHERE id = ?",
            (now, now, session_id),
        )
        services.record_event(
            database,
            session_id,
            'draft_regeneration_claimed',
            {'requestId': request_id, 'claimDelaySeconds': claim_delay_seconds},
            now,
        )
        return {'sessionId': session_id, 'requestId': request_id, 'status': 'claimed'}


@router.post('/api/integrations/sessions/{session_id}/draft-regenerations/{request_id}/complete')
def complete_draft_regeneration(
    session_id: str,
    request_id: str,
    payload: DraftRegenerationCompleteRequest,
    authorization: str | None = Header(None, alias='Authorization'),
    services: CoCreationServices = Depends(get_services),
):
    expired_check = False
    completed_rows = None
    with services.connect() as database:
        session = _require_integration_session(database, session_id, authorization)
        _require_draft_preview(database, session)
        expired_check, _, _ = _draft_regeneration_timeout_expired(session, services=services)
        if not expired_check:
            if session['draft_regeneration_request_id'] != request_id:
                raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
            if session['draft_regeneration_status'] == 'completed':
                completed_rows = load_json(session['draft_rows_json'])
            elif session['draft_regeneration_status'] != 'claimed':
                raise ApiError(409, 'DRAFT_REGENERATION_NOT_CLAIMED', 'The regeneration request is not claimable.')
    if expired_check:
        with services.connect(immediate=True) as database:
            session = _require_integration_session(database, session_id, authorization)
            _require_draft_preview(database, session)
            session = _expire_draft_regeneration(database, session, services=services)
            if session['draft_regeneration_request_id'] != request_id:
                raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
            if session['draft_regeneration_status'] == 'completed':
                completed_rows = load_json(session['draft_rows_json'])
            elif session['draft_regeneration_status'] != 'claimed':
                raise ApiError(409, 'DRAFT_REGENERATION_NOT_CLAIMED', 'The regeneration request is not claimable.')
    if completed_rows is not None:
        validation = _solve_or_api_error(payload.rows, services=services)
        if completed_rows != list(validation.rows):
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Completed regeneration rows cannot be replaced.')
        return {'sessionId': session_id, 'requestId': request_id, 'status': 'completed'}
    try:
        validation = _solve_or_api_error(payload.rows, services=services)
    except ApiError as exception:
        with services.connect(immediate=True) as database:
            session = _require_integration_session(database, session_id, authorization)
            session = _expire_draft_regeneration(database, session, services=services)
            if session['draft_regeneration_request_id'] == request_id and session['draft_regeneration_status'] == 'claimed':
                now = services.utc_now()
                database.execute(
                    "UPDATE design_sessions SET draft_regeneration_status = 'failed',\n"
                    "                       draft_regeneration_updated_at = ?,\n"
                    "                       draft_regeneration_failure_code = 'validation_failed', updated_at = ?\n"
                    "                       WHERE id = ? AND draft_regeneration_request_id = ?\n"
                    "                         AND draft_regeneration_status = 'claimed'",
                    (now, now, session_id, request_id),
                )
                services.record_event(
                    database,
                    session_id,
                    'draft_regeneration_failed',
                    {'requestId': request_id, 'failureCode': 'validation_failed', 'phase': 'claimed'},
                    now,
                )
        raise ApiError(422, exception.code, exception.message, exception.retryable, exception.details) from exception
    normalized_rows = list(validation.rows)
    with services.connect(immediate=True) as database:
        session = _require_integration_session(database, session_id, authorization)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session, services=services)
        if session['draft_regeneration_request_id'] != request_id:
            raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
        if session['draft_regeneration_status'] == 'completed':
            if load_json(session['draft_rows_json']) != normalized_rows:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Completed regeneration rows cannot be replaced.')
            return {'sessionId': session_id, 'requestId': request_id, 'status': 'completed'}
        if session['draft_regeneration_status'] != 'claimed':
            raise ApiError(409, 'DRAFT_REGENERATION_NOT_CLAIMED', 'The regeneration request is not claimable.')
        now = services.utc_now()
        claimed_at = session['draft_regeneration_updated_at']
        execution_seconds = None
        if claimed_at:
            execution_seconds = max(0, round((parse_time(now) - parse_time(claimed_at)).total_seconds(), 3))
        generation = int(session['draft_generation'] or 1) + 1
        database.execute(
            "UPDATE design_sessions SET draft_rows_json = ?, draft_validation_json = ?,\n"
            "               draft_generation = ?, draft_regeneration_status = 'completed',\n"
            "               draft_regeneration_updated_at = ?, draft_regeneration_failure_code = NULL,\n"
            "               updated_at = ? WHERE id = ?",
            (
                dump_json(normalized_rows),
                dump_json(validation.as_dict()),
                generation,
                now,
                now,
                session_id,
            ),
        )
        services.record_event(
            database,
            session_id,
            'draft_regeneration_completed',
            {
                'requestId': request_id,
                'generation': generation,
                'mode': 'unity',
                'executionSeconds': execution_seconds,
            },
            now,
        )
        return {'sessionId': session_id, 'requestId': request_id, 'status': 'completed'}


@router.post('/api/integrations/sessions/{session_id}/draft-regenerations/{request_id}/fail')
def fail_draft_regeneration(
    session_id: str,
    request_id: str,
    payload: DraftRegenerationFailRequest,
    authorization: str | None = Header(None, alias='Authorization'),
    services: CoCreationServices = Depends(get_services),
):
    with services.connect(immediate=True) as database:
        session = _require_integration_session(database, session_id, authorization)
        _require_draft_preview(database, session)
        session = _expire_draft_regeneration(database, session, services=services)
        if session['draft_regeneration_request_id'] != request_id:
            raise ApiError(409, 'STALE_DRAFT_REGENERATION', 'The regeneration request is no longer current.')
        if session['draft_regeneration_status'] == 'failed':
            return {'sessionId': session_id, 'requestId': request_id, 'status': 'failed'}
        if session['draft_regeneration_status'] not in {'pending', 'claimed'}:
            raise ApiError(409, 'DRAFT_REGENERATION_NOT_ACTIVE', 'The regeneration request is not active.')
        now = services.utc_now()
        lease_started_at = (
            session['draft_regeneration_requested_at']
            if session['draft_regeneration_status'] == 'pending'
            else session['draft_regeneration_updated_at']
        )
        terminal_delay_seconds = None
        if lease_started_at:
            terminal_delay_seconds = max(0, round((parse_time(now) - parse_time(lease_started_at)).total_seconds(), 3))
        database.execute(
            "UPDATE design_sessions SET draft_regeneration_status = 'failed',\n"
            "               draft_regeneration_updated_at = ?, draft_regeneration_failure_code = ?,\n"
            "               updated_at = ? WHERE id = ?",
            (now, payload.failureCode, now, session_id),
        )
        services.record_event(
            database,
            session_id,
            'draft_regeneration_failed',
            {
                'requestId': request_id,
                'failureCode': payload.failureCode,
                'phase': session['draft_regeneration_status'],
                'phaseDurationSeconds': terminal_delay_seconds,
            },
            now,
        )
        return {'sessionId': session_id, 'requestId': request_id, 'status': 'failed'}


def update_play_attempt(
    attempt_id,
    payload,
    requested_status,
    *,
    services: CoCreationServices,
):
    _validate_metrics(payload)
    with services.connect(immediate=True) as database:
        attempt = database.execute('SELECT * FROM play_attempts WHERE id = ?', (attempt_id,)).fetchone()
        if attempt is None or not token_matches(payload.attemptToken, attempt['attempt_token_hash']):
            raise ApiError(401, 'INVALID_ATTEMPT_TOKEN', 'Play result access was denied.')
        if attempt['status'] in {'completed', 'abandoned', 'interrupted'}:
            return {'attemptId': attempt_id, 'status': attempt['status']}
        now = services.utc_now()
        terminal = requested_status in {'completed', 'abandoned'}
        if terminal:
            duration_seconds = payload.durationSeconds
            move_count = payload.moveCount
            push_count = payload.pushCount
            restart_count = payload.restartCount
        else:
            duration_seconds = max(float(attempt['duration_seconds'] or 0), float(payload.durationSeconds))
            move_count = max(int(attempt['move_count'] or 0), int(payload.moveCount))
            push_count = max(int(attempt['push_count'] or 0), int(payload.pushCount))
            restart_count = max(int(attempt['restart_count'] or 0), int(payload.restartCount))
        existing_min_moves = attempt['minimum_moves']
        existing_min_pushes = attempt['minimum_pushes']
        minimum_moves = (
            existing_min_moves
            if existing_min_moves >= 0 and payload.minimumMoves < 0
            else payload.minimumMoves
        )
        minimum_pushes = (
            existing_min_pushes
            if existing_min_pushes >= 0 and payload.minimumPushes < 0
            else payload.minimumPushes
        )
        first_move_at = attempt['first_move_at']
        if first_move_at is None and move_count > 0:
            first_move_at = now
        database.execute(
            '\n            UPDATE play_attempts SET\n'
            '                status = ?, first_move_at = ?, finished_at = ?,\n'
            '                duration_seconds = ?, move_count = ?, push_count = ?,\n'
            '                restart_count = ?, minimum_moves = ?, minimum_pushes = ?\n'
            '            WHERE id = ?\n            ',
            (
                requested_status,
                first_move_at,
                now if terminal else None,
                duration_seconds,
                move_count,
                push_count,
                restart_count,
                minimum_moves,
                minimum_pushes,
                attempt_id,
            ),
        )
        services.record_event(
            database,
            attempt['session_id'],
            'play_attempt_' + requested_status,
            {
                'attemptId': attempt_id,
                'versionId': attempt['version_id'],
                'moveCount': move_count,
                'pushCount': push_count,
                'restartCount': restart_count,
                'durationSeconds': duration_seconds,
            },
            now,
        )
        if terminal:
            record_intent_evidence(
                database,
                attempt['session_id'],
                attempt['version_id'],
                'play_' + requested_status,
                'Designer play evidence was recorded for the saved Stage.',
                play_attempt_id=attempt_id,
                details={
                    'status': requested_status,
                    'durationSeconds': duration_seconds,
                    'moveCount': move_count,
                    'pushCount': push_count,
                    'restartCount': restart_count,
                    'minimumMoves': minimum_moves,
                    'minimumPushes': minimum_pushes,
                },
                created_at=now,
                services=services,
            )
        return {'attemptId': attempt_id, 'status': requested_status}
