"""Staged execution workflow for the primary POST /messages endpoint."""
from __future__ import annotations

import copy
import hashlib
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from cocreation.domain import binary_discovery, proposal_options
from cocreation.domain.conversation_evidence import with_latest
from cocreation.domain.design_context import extract_explicit_user_memory, question_dedup_key
from cocreation.domain.level_validation import (
    analyze_user_map_claims,
    describe_diff,
    summarize_verified_diff,
)
from cocreation.domain.proposal_diagnostics import trace_proposal
from cocreation.domain.proposal_understanding import (
    bind_latest,
    protected_request_message,
    reusable_understanding,
    topic_signature,
)
from cocreation.llm.facade import (
    KIMI_MODEL,
    LLMExecutionResult,
    LLMServiceError,
    _strip_unverified_state_claims,
    validate_execution_brief,
)
from cocreation.persistence.repository import (
    dump_json,
    get_current_version,
    load_design_context,
    load_json,
    map_fingerprint,
)
from cocreation.reliability import component_recovery
from cocreation.reliability.reply_reliability import trace_reply

from . import binary_routes
from .config_models import (
    ApiError,
    REVISION_CARD_ACTIONS,
    _execute_revision_candidate_or_api_error,
    _llm_failure_details,
    _solve_changed_proposal_or_api_error,
    _validate_changed_proposal,
    record_agent_handoff,
    require_active_session,
    require_browser_session,
    require_current_base,
)
from .context_and_discovery import (
    _adaptive_revision_routing,
    _allow_adaptive_proposal_completion,
    _design_context_guidance,
    _deterministic_conflict_explanation,
    _deterministic_intent_conflict,
    _intent_claims_equivalent,
    _is_vague_aesthetic_revision,
    _message_commit_binding,
    _proposal_clarification_spec,
    _proposal_feasibility_profile,
    _public_guidance,
    _record_disagreement_event,
    _update_design_context_from_turn,
    _verified_proposal_message,
    active_intent_conflict_choice as _active_intent_conflict_choice,
    build_llm_context,
    record_intent_evidence,
    record_intent_hypothesis,
)
from .message_request_identity import conflicts_with_saved_user_turn, message_request_signature
from .message_response_policy import apply_conversation_only_policy, apply_map_claim_conflict_policy
from .online_sync_and_dashboard import synchronize_turn_with_online_match
from .proposal_and_challenge import (
    _action_for_message_key,
    _automatic_proposal_failure_execution,
    _bind_execution_to_stage,
    _challenge_choice,
    _classified_proposal_failure_execution,
    _enforce_challenge_choice_execution,
    _enforce_challenge_reason_execution,
    _enforce_human_edit_disagreement_followup,
    _ensure_alternative_revision_execution,
    _mark_new_discussion_guidance,
    _mark_proposal_discovery_guidance,
    _materialize_verified_automatic_offer,
    _normalize_manual_edit_review_execution,
    _persist_verified_requirements,
    _preflight_proposal,
    _proposal_cancelled_execution,
    _proposal_request_for_message_key,
    _proposal_review_guidance,
    _proposal_search_failure_execution,
    _record_card_action,
    _record_reply_outcome,
    _relaxed_revision_suggestion_execution,
    _require_offer_action,
    _revision_contract_clarification_execution,
    _rows_after_frozen_proposal,
    _salvage_challenge_analysis,
    _sanitize_challenge_execution,
    _source_revision_offer,
    _stage_has_prior_assistant_reply,
    _verified_recovery_suggestion_execution,
)
from .services import CoCreationServices


@dataclass(frozen=True)
class _MessageRequestContext:
    """Immutable inputs and tracing parameters for one POST /messages execution."""

    session_id: str
    payload: Any
    content: str
    request: Any
    response: Any
    access_cookie: str | None
    services: CoCreationServices
    message_started_at: float
    llm_budget_seconds: float
    request_signature: str


@dataclass
class _PreflightState:
    """Read-only idempotency and turn-understanding preparation state."""

    replayed_payload: dict | None = None
    precomputed_understanding: dict | None = None
    expected_understanding_signature: str | None = None
    understanding_context: dict | None = None
    understanding_freshly_classified: bool = False
    pending_binary_start: bool = False
    binary_topic: dict | None = None
    prior_conversation: list[dict] | None = None
    snapshot_for_understanding: dict | None = None
    proposal_context_for_understanding: dict | None = None


@dataclass
class _RoutedMessageState:
    """Explicit stage state shared across routing, generation, and atomic commit."""

    replayed_payload: dict | None = None
    user_turn_id: str = ''
    language: str = 'en'
    context: dict = field(default_factory=dict)
    allow_progress: bool = False
    retrying_failed_message: bool = False
    user_map_claims: dict = field(default_factory=dict)
    turn_understanding: dict | None = None
    proposal_discovery: dict = field(default_factory=dict)
    source_offer: dict | None = None
    source_turn: Any = None
    source_binding: dict | None = None
    excluded_rows: list[str] | None = None
    proposal_branch: bool = False
    prechecked_challenge_row: Any = None
    prechecked_challenge_binding: dict | None = None
    deterministic_challenge_choice: str | None = None
    challenge_reason_classification: dict | None = None
    challenge_review_id: str | None = None
    challenge_choice_resolution: str | None = None
    challenge_deadline: float | None = None
    challenge_id: str | None = None
    challenged_binding: dict | None = None
    question_answer_review: dict = field(default_factory=lambda: {'answeredQuestionIds': [], 'results': []})
    joint_ordinary_review: bool = False
    active_disagreement: dict = field(default_factory=dict)
    revision_state: str = 'none'
    revision_brief: str | None = None
    revision_failure: Any = None
    recovery_execution: LLMExecutionResult | None = None
    message_deadline: float = 0.0
    execution: LLMExecutionResult | None = None
    proposal_recovery: bool = False
    design_context_guidance: dict = field(default_factory=dict)
    state_transition_deferred: bool = False

    @property
    def stage_context(self) -> dict:
        return self.context['stageContext']


def _preflight_message_request(req: _MessageRequestContext) -> _PreflightState:
    """Check idempotency keys, active binary flows, and reusable turn understanding."""
    state = _PreflightState()
    payload = req.payload
    services = req.services
    with services.connect() as read_database:
        read_session = require_browser_session(read_database, req.session_id, req.access_cookie, services=services)
        prior_failure = read_database.execute(
            "SELECT payload_json FROM audit_events WHERE session_id=? AND event_type='message_generation_failed' "
            "AND json_extract(payload_json, '$.messageKey')=? ORDER BY id DESC LIMIT 1",
            (req.session_id, payload.idempotencyKey),
        ).fetchone()
        previous_signature = (load_json(prior_failure['payload_json']) or {}).get('requestSignature') if prior_failure else None
        if previous_signature and previous_signature != req.request_signature:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The failed message key belongs to a different request.')
        read_user = read_database.execute(
            "SELECT id, content, version_id FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'user'",
            (req.session_id, payload.idempotencyKey),
        ).fetchone()
        if conflicts_with_saved_user_turn(read_user, req.content, payload.baseVersionId):
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used for different content or Stage.')
        binary_receipt = binary_routes._receipt(read_database, req.session_id, payload.idempotencyKey)
        if binary_receipt:
            if binary_receipt['request'].get('kind') != 'start':
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'This key belongs to another action.')
            expected = {
                'kind': 'start',
                'baseVersionId': payload.baseVersionId,
                'content': req.content,
                'requestProposal': payload.requestProposal,
            }
            if payload.action != 'none' or binary_receipt['request'] != expected:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'This key belongs to another action.')
            if binary_receipt['status'] == 'done':
                state.replayed_payload = services.serialize_session(read_database, req.session_id)
            else:
                binary_topic = binary_discovery.current_topic(
                    read_database,
                    req.session_id,
                    payload.baseVersionId,
                    clock=services.clock,
                )
                if (
                    not binary_topic
                    or binary_topic['status'] not in binary_discovery.ACTIVE
                    or binary_topic['initialMessageKey'] != payload.idempotencyKey
                ):
                    raise ApiError(409, 'PROPOSAL_STALE', 'This proposal discovery request is no longer active.')
                state.binary_topic = binary_topic
                state.pending_binary_start = True
        else:
            prior_action = _action_for_message_key(read_database, req.session_id, payload.idempotencyKey)
            if prior_action is not None and prior_action != (payload.action, payload.sourceTurnId):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used for a different card action.')
            if read_user is not None and payload.action in REVISION_CARD_ACTIONS and (prior_action is None):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used by an ordinary message.')
            prior_proposal_request = _proposal_request_for_message_key(read_database, req.session_id, payload.idempotencyKey)
            if read_user is not None and bool(prior_proposal_request) != payload.requestProposal:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used with a different proposal-request mode.')
            already_answered = read_database.execute(
                "SELECT 1 FROM conversation_turns\n"
                "                WHERE session_id = ? AND request_id = ? AND role = 'assistant'",
                (req.session_id, payload.idempotencyKey),
            ).fetchone()
            if already_answered is not None:
                state.replayed_payload = services.serialize_session(read_database, req.session_id)
        if state.replayed_payload is None:
            read_session = require_active_session(read_database, req.session_id, req.access_cookie, services=services)
            if payload.action != 'none':
                active_binary = binary_discovery.current_topic(
                    read_database,
                    req.session_id,
                    payload.baseVersionId,
                    clock=services.clock,
                )
                if active_binary and active_binary['status'] in binary_discovery.ACTIVE:
                    raise ApiError(409, 'PROPOSAL_CHOICE_REQUIRED', 'Finish or cancel the current A/B proposal first.')
            else:
                require_current_base(read_session, payload.baseVersionId)
                binary_topic = binary_discovery.current_topic(
                    read_database,
                    req.session_id,
                    payload.baseVersionId,
                    clock=services.clock,
                )
                if (
                    binary_topic
                    and binary_topic['status'] in binary_discovery.ACTIVE
                    and (payload.idempotencyKey != binary_topic['initialMessageKey'] or req.content != binary_topic['initialRequest'])
                ):
                    raise ApiError(409, 'PROPOSAL_CHOICE_REQUIRED', 'Use the current A/B buttons, retry, or cancel this proposal.')
                state.binary_topic = binary_topic
                if _active_intent_conflict_choice(read_database, req.session_id, payload.baseVersionId) is not None:
                    raise ApiError(409, 'INTENT_CONFLICT_PENDING', 'Retain one of the conflicting inclinations before sending another message.')
                read_version = get_current_version(read_database, read_session)
                read_context = build_llm_context(read_database, req.session_id, read_version, services=services)
                state.snapshot_for_understanding = read_context['stageContext']['stageSnapshot']
                state.proposal_context_for_understanding = read_context['stageContext'].get('proposalDiscovery') or {}
                interpretation_context = (
                    {
                        key: state.proposal_context_for_understanding.get(key)
                        for key in (
                            'topicId',
                            'versionId',
                            'initialRequest',
                            'questions',
                            'answers',
                            'lastQuestionText',
                            'userTurns',
                            'understanding',
                            'optionSet',
                            'optionQuestion',
                            'optionDecision',
                            'clarificationQuestionCount',
                        )
                    }
                    if state.proposal_context_for_understanding
                    else {}
                )
                dialogue_evidence = copy.deepcopy(read_context['stageContext'].get('dialogueEvidence'))
                if read_user is not None and isinstance(dialogue_evidence, dict):
                    dialogue_evidence['turns'] = [
                        item for item in (dialogue_evidence.get('turns') or []) if item.get('id') != read_user['id']
                    ]
                    if 'userTurns' in interpretation_context and isinstance(interpretation_context.get('userTurns'), list):
                        interpretation_context['userTurns'] = [
                            item for item in interpretation_context['userTurns'] if item.get('id') != read_user['id']
                        ]
                interpretation_context['dialogueEvidence'] = with_latest(dialogue_evidence, req.content)
                interpretation_context['activeDisagreement'] = read_context['stageContext'].get('activeDisagreement')
                state.understanding_context = interpretation_context
                state.expected_understanding_signature = topic_signature(
                    state.snapshot_for_understanding,
                    interpretation_context,
                    req.content,
                )
                active_disagreement = interpretation_context.get('activeDisagreement') or {}
                if (
                    payload.requestProposal
                    and payload.action == 'none'
                    and active_disagreement.get('status') == 'active'
                ):
                    raise ApiError(
                        409,
                        'DISAGREEMENT_ACTIVE',
                        'Resolve the active design disagreement before requesting a new proposal.',
                    )
                saved_understanding = read_database.execute(
                    "SELECT payload_json FROM audit_events\n"
                    "                WHERE session_id = ? AND event_type = 'turn_understanding'\n"
                    "                  AND json_extract(payload_json, '$.messageKey') = ?\n"
                    "                ORDER BY id DESC LIMIT 1",
                    (req.session_id, payload.idempotencyKey),
                ).fetchone()
                if saved_understanding is not None and not reusable_understanding(
                    load_json(saved_understanding['payload_json'])['result'],
                    payload.baseVersionId,
                    expected_signature=state.expected_understanding_signature,
                    context=state.understanding_context,
                ):
                    saved_understanding = None
                if saved_understanding is not None:
                    state.precomputed_understanding = load_json(saved_understanding['payload_json'])['result']
                else:
                    prior_conversation = read_context['conversation']
                    if (
                        not prior_conversation
                        or prior_conversation[-1].get('role') != 'user'
                        or prior_conversation[-1].get('content') != req.content
                    ):
                        prior_conversation = [*prior_conversation, {'role': 'user', 'content': req.content}]
                    state.prior_conversation = prior_conversation
    return state


def _resolve_turn_understanding_or_binary(
    req: _MessageRequestContext,
    preflight: _PreflightState,
) -> dict | None:
    """Classify turn understanding when needed and hand off eligible requests to Policy 2."""
    payload = req.payload
    services = req.services
    if payload.action == 'none' and preflight.precomputed_understanding is None:
        try:
            preflight.precomputed_understanding = services.llm.classify_turn_understanding(
                preflight.prior_conversation,
                preflight.snapshot_for_understanding,
                req.request.state.request_id,
                forced_proposal=payload.requestProposal,
                proposal_context=preflight.understanding_context,
                _deadline=req.message_started_at + req.llm_budget_seconds,
            )
            preflight.understanding_freshly_classified = True
        except LLMServiceError as error:
            with services.connect(immediate=True) as database:
                active_session = require_active_session(database, req.session_id, req.access_cookie, services=services)
                require_current_base(active_session, payload.baseVersionId)
                services.record_event(
                    database,
                    req.session_id,
                    'message_generation_failed',
                    {
                        'messageKey': payload.idempotencyKey,
                        'baseVersionId': payload.baseVersionId,
                        'requestId': req.request.state.request_id,
                        'code': error.code,
                        'retryable': error.retryable,
                        'attemptsUsed': error.attempts_used,
                        'proposalRequested': payload.requestProposal,
                        'requestSignature': req.request_signature,
                        'details': {
                            **_llm_failure_details(error),
                            'task': 'turn_understanding',
                            'failureStage': (getattr(error, 'details', {}) or {}).get('failureStage', 'understanding'),
                            'maximumAttempts': 2,
                        },
                    },
                    services.utc_now(),
                )
                if preflight.proposal_context_for_understanding:
                    services.record_event(
                        database,
                        req.session_id,
                        'proposal_discovery_progress',
                        {
                            'stageId': payload.baseVersionId,
                            'topicId': preflight.proposal_context_for_understanding['topicId'],
                            'messageKey': payload.idempotencyKey,
                            'status': 'retry_pending',
                            'retryable': True,
                            'failureCode': error.code,
                            'clarificationQuestionCount': preflight.proposal_context_for_understanding.get(
                                'clarificationQuestionCount', 0
                            ),
                        },
                        services.utc_now(),
                    )
            if payload.requestProposal or preflight.proposal_context_for_understanding or error.code != 'MODEL_RESPONSE_INVALID':
                raise
            preflight.precomputed_understanding = {
                'acts': ['unclear'],
                'elements': [],
                'evidenceSpan': req.content,
                'directionSufficient': False,
                'mapRelated': True,
                'changes': [],
                'proposalUnderstanding': None,
                'conversationOnly': True,
                'semanticPolicyVersion': 1,
            }
            preflight.understanding_freshly_classified = True
    if payload.action == 'none' and preflight.precomputed_understanding is not None:
        with services.connect() as database:
            session = require_active_session(database, req.session_id, req.access_cookie, services=services)
            require_current_base(session, payload.baseVersionId)
            if _active_intent_conflict_choice(database, req.session_id, payload.baseVersionId) is not None:
                raise ApiError(
                    409,
                    'INTENT_CONFLICT_PENDING',
                    'Retain one of the conflicting inclinations before sending another message.',
                )
            binary_context = build_llm_context(database, req.session_id, get_current_version(database, session), services=services)
            binary_topic = binary_discovery.current_topic(
                database,
                req.session_id,
                payload.baseVersionId,
                clock=services.clock,
            )
            retry_binary = preflight.pending_binary_start or bool(
                binary_topic
                and binary_topic['status'] in binary_discovery.ACTIVE
                and (binary_topic['initialMessageKey'] == payload.idempotencyKey)
            )
        if retry_binary or services.should_start_binary(payload, preflight.precomputed_understanding, binary_context):
            return binary_routes.start(
                services,
                req.session_id,
                payload,
                req.content,
                preflight.precomputed_understanding,
                req.request,
                req.access_cookie,
            )
    return None


def _commit_user_turn_and_route(req: _MessageRequestContext, preflight: _PreflightState) -> _RoutedMessageState:
    """Validate card/challenge preconditions, persist the user turn, and compute Policy 1 routing."""
    state = _RoutedMessageState()
    payload = req.payload
    services = req.services
    with services.connect(immediate=True) as database:
        session = require_browser_session(database, req.session_id, req.access_cookie, services=services)
        prior_user = database.execute(
            "\n            SELECT * FROM conversation_turns\n"
            "            WHERE session_id = ? AND request_id = ? AND role = 'user'\n            ",
            (req.session_id, payload.idempotencyKey),
        ).fetchone()
        if conflicts_with_saved_user_turn(prior_user, req.content, payload.baseVersionId):
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used for different content or Stage.')
        prior_action = _action_for_message_key(database, req.session_id, payload.idempotencyKey)
        if prior_action is not None and prior_action != (payload.action, payload.sourceTurnId):
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used for a different card action.')
        if prior_user is not None and payload.action in REVISION_CARD_ACTIONS and (prior_action is None):
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used by an ordinary message.')
        prior_proposal_request = _proposal_request_for_message_key(database, req.session_id, payload.idempotencyKey)
        if prior_user is not None and bool(prior_proposal_request) != payload.requestProposal:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'The message key was already used with a different proposal-request mode.')
        prior_assistant = database.execute(
            "\n            SELECT * FROM conversation_turns\n"
            "            WHERE session_id = ? AND request_id = ? AND role = 'assistant'\n            ",
            (req.session_id, payload.idempotencyKey),
        ).fetchone()
        if prior_assistant is not None:
            state.replayed_payload = services.serialize_session(database, req.session_id)
            return state

        session = require_active_session(database, req.session_id, req.access_cookie, services=services)
        if payload.action in {'none', 'continue_challenge'}:
            require_current_base(session, payload.baseVersionId)
        elif payload.baseVersionId != session['current_version_id']:
            raise ApiError(409, 'PROPOSAL_STALE', 'The selected proposal belongs to an older Stage.', details={'reason': 'version_changed'})
        if payload.action == 'none' and _active_intent_conflict_choice(database, req.session_id, payload.baseVersionId) is not None:
            raise ApiError(409, 'INTENT_CONFLICT_PENDING', 'Retain one of the conflicting inclinations before sending another message.')
        if payload.action in REVISION_CARD_ACTIONS:
            state.source_turn, state.source_offer = _source_revision_offer(
                database, req.session_id, payload.baseVersionId, payload.sourceTurnId
            )
            _require_offer_action(state.source_offer, payload.action)
        current = get_current_version(database, session)
        current_rows_for_precheck = load_json(current['rows_json'])
        if payload.action in REVISION_CARD_ACTIONS:
            state.source_binding = _preflight_proposal(
                database,
                req.session_id,
                session,
                state.source_turn,
                state.source_offer,
                current_rows_for_precheck,
                services=services,
            )
        precheck_disagreement = (load_design_context(database, req.session_id, payload.baseVersionId) or {}).get('activeDisagreement') or {}
        requested_challenge_choice = getattr(payload, 'challengeChoice', None)
        if requested_challenge_choice is not None and (
            not (
                precheck_disagreement.get('subject') == 'ai_revision_challenge'
                and precheck_disagreement.get('phase') == 'choice_pending'
                and (precheck_disagreement.get('challengeId') == payload.challengeId)
            )
        ):
            raise ApiError(409, 'CHALLENGE_STALE', 'This choice no longer belongs to the active challenge.')
        deterministic_challenge_choice = (
            requested_challenge_choice or _challenge_choice(req.content, session['language'])
            if precheck_disagreement.get('subject') == 'ai_revision_challenge'
            and precheck_disagreement.get('phase') == 'choice_pending'
            else None
        )
        state.deterministic_challenge_choice = deterministic_challenge_choice
        if deterministic_challenge_choice is not None:
            prechecked_challenge_row = database.execute(
                'SELECT source.id, source.version_id, source.proposal_binding_json,\n'
                '                          source.guidance_json, source.request_id, challenge.current_reason_turn_id\n'
                '                FROM revision_challenges AS challenge\n'
                '                JOIN conversation_turns AS source ON source.id = challenge.source_proposal_turn_id\n'
                '                WHERE challenge.session_id = ? AND challenge.challenge_id = ?\n'
                '                  AND challenge.base_version_id = ?',
                (req.session_id, precheck_disagreement.get('challengeId'), payload.baseVersionId),
            ).fetchone()
            prechecked_challenge_binding = (
                load_json(prechecked_challenge_row['proposal_binding_json']) or {}
                if prechecked_challenge_row
                else {}
            )
            if (
                prechecked_challenge_row is None
                or prechecked_challenge_row['version_id'] != payload.baseVersionId
                or prechecked_challenge_binding.get('mapFingerprint') != map_fingerprint(current_rows_for_precheck)
                or (not (prechecked_challenge_binding.get('executionBrief') or {}).get('requiredTransitions'))
            ):
                raise ApiError(409, 'CHALLENGE_STALE', 'The challenged proposal is no longer available on this Stage.')
            state.prechecked_challenge_row = prechecked_challenge_row
            state.prechecked_challenge_binding = prechecked_challenge_binding

        state.allow_progress = payload.action == 'none' and _stage_has_prior_assistant_reply(
            database, req.session_id, payload.baseVersionId
        )
        if prior_user is None:
            state.user_turn_id = services.insert_turn(
                database,
                session,
                'user',
                req.content,
                payload.baseVersionId,
                payload.idempotencyKey,
                None,
            )
            if payload.action == 'none':
                explicit_goals, explicit_constraints = extract_explicit_user_memory(req.content)
                for explicit_text in dict.fromkeys([*explicit_goals, *explicit_constraints]):
                    record_intent_evidence(
                        database,
                        req.session_id,
                        payload.baseVersionId,
                        'expressed_direction',
                        explicit_text,
                        turn_id=state.user_turn_id,
                        details={'requestProposal': bool(payload.requestProposal)},
                        services=services,
                    )
            if payload.requestProposal:
                services.record_event(
                    database,
                    req.session_id,
                    'proposal_request_requested',
                    {'messageKey': payload.idempotencyKey, 'turnId': state.user_turn_id, 'baseVersionId': payload.baseVersionId},
                    services.utc_now(),
                )
        else:
            state.user_turn_id = prior_user['id']
        if payload.action in REVISION_CARD_ACTIONS and prior_action is None:
            _record_card_action(database, req.session_id, payload, state.source_offer, services=services)
        elif payload.action == 'continue_challenge' and prior_action is None:
            services.record_event(
                database,
                req.session_id,
                'challenge_continued',
                {
                    'messageKey': payload.idempotencyKey,
                    'action': payload.action,
                    'challengeId': payload.challengeId,
                    'baseVersionId': payload.baseVersionId,
                },
                services.utc_now(),
            )
        state.retrying_failed_message = prior_user is not None
        state.context = build_llm_context(database, req.session_id, current, services=services)
        state.language = session['language']
        if payload.action == 'none':
            saved_understanding = database.execute(
                "SELECT payload_json FROM audit_events\n"
                "                WHERE session_id = ? AND event_type = 'turn_understanding'\n"
                "                  AND json_extract(payload_json, '$.messageKey') = ?\n"
                "                ORDER BY id DESC LIMIT 1",
                (req.session_id, payload.idempotencyKey),
            ).fetchone()
            if (
                not preflight.understanding_freshly_classified
                and saved_understanding is not None
                and reusable_understanding(
                    load_json(saved_understanding['payload_json'])['result'],
                    payload.baseVersionId,
                    expected_signature=preflight.expected_understanding_signature,
                    context=preflight.understanding_context,
                )
            ):
                turn_understanding = load_json(saved_understanding['payload_json'])['result']
            else:
                turn_understanding = bind_latest(preflight.precomputed_understanding, state.user_turn_id, payload.baseVersionId)
                services.record_event(
                    database,
                    req.session_id,
                    'turn_understanding',
                    {'messageKey': payload.idempotencyKey, 'baseVersionId': payload.baseVersionId, 'result': turn_understanding},
                    services.utc_now(),
                )
                if turn_understanding.get('criticalReview'):
                    services.record_event(
                        database,
                        req.session_id,
                        'proposal_understanding_reviewed',
                        {
                            'messageKey': payload.idempotencyKey,
                            'baseVersionId': payload.baseVersionId,
                            **turn_understanding['criticalReview'],
                        },
                        services.utc_now(),
                    )
            state.turn_understanding = turn_understanding
            state.context = build_llm_context(database, req.session_id, current, services=services)

        stage_context = state.stage_context
        stage_context.setdefault('_reliabilityState', {})
        stage_context['requirementPolicyVersion'] = 1
        stage_context['requirementUserTurns'] = [{'id': state.user_turn_id, 'content': req.content}]
        if state.turn_understanding is not None:
            stage_context['turnUnderstanding'] = state.turn_understanding
            stage_context['conversationOnly'] = bool(state.turn_understanding.get('conversationOnly'))
            if stage_context['conversationOnly']:
                state.allow_progress = False
        stage_context['aiEditableTilesOnly'] = True
        state.user_map_claims = analyze_user_map_claims(req.content, stage_context.get('stageSnapshot'))
        stage_context['userMapClaims'] = state.user_map_claims
        active_disagreement_state = stage_context.get('activeDisagreement') or {}
        if isinstance(active_disagreement_state, dict) and active_disagreement_state.get('status') == 'active':
            state.proposal_discovery = {}
            stage_context['proposalDiscovery'] = {}
            revision_routing = 'none'
            stage_context['adaptiveProposalCompletion'] = False
        else:
            state.proposal_discovery = stage_context.get('proposalDiscovery') or {}
            revision_routing = _adaptive_revision_routing(
                req.content,
                state.user_map_claims,
                stage_context.get('stageSnapshot'),
                proposal_discovery=state.proposal_discovery,
                turn_understanding=state.turn_understanding,
                services=services,
            )
            stage_context['adaptiveProposalCompletion'] = (
                revision_routing == 'needs_clarification'
                and (not proposal_options.current_set(state.proposal_discovery, stage_context.get('stageSnapshot')))
                and _allow_adaptive_proposal_completion(
                    req.content,
                    state.user_map_claims,
                    stage_context.get('stageSnapshot'),
                    stage_context.get('clarificationQuestionCount', 0),
                )
            )
            if stage_context['adaptiveProposalCompletion']:
                revision_routing = 'proposal'
        stage_context['revisionRouting'] = revision_routing
        stage_context['clarificationBudgetExhausted'] = bool(
            state.proposal_discovery and int(state.proposal_discovery.get('clarificationQuestionCount') or 0) >= 3
        )
        stage_context['feasibilityProfile'] = _proposal_feasibility_profile(
            stage_context.get('stageSnapshot'),
            state.context.get('rows') or [],
            stage_context.get('designContext'),
            state.context.get('validation'),
        )
        stage_context['vagueAestheticRevision'] = _is_vague_aesthetic_revision(req.content)
        proposal_state = {
            'proposal': 'ready_with_explicit_binding',
            'proposal_conservative': 'ready_with_conservative_binding',
            'proposal_blocked': 'revision_needed',
            'proposal_cancelled': 'cancelled',
        }.get(revision_routing, 'clarifying')
        stage_context['proposalState'] = proposal_state
        stage_context['proposalBindingMode'] = {
            'ready_with_explicit_binding': 'explicit',
            'ready_with_conservative_binding': 'conservative',
        }.get(proposal_state)
        stage_context['responseLanguage'] = state.language
        if state.proposal_discovery and state.proposal_discovery.get('sourceTurnId'):
            started = database.execute(
                "\n                SELECT 1 FROM audit_events\n"
                "                WHERE session_id = ? AND event_type = 'proposal_discovery_started'\n"
                "                  AND json_extract(payload_json, '$.topicId') = ?\n"
                "                LIMIT 1\n                ",
                (req.session_id, state.proposal_discovery['topicId']),
            ).fetchone()
            if started is None:
                services.record_event(
                    database,
                    req.session_id,
                    'proposal_discovery_started',
                    {
                        'stageId': payload.baseVersionId,
                        'topicId': state.proposal_discovery['topicId'],
                        'sourceTurnId': state.proposal_discovery['sourceTurnId'],
                        'messageKey': payload.idempotencyKey,
                        'trigger': 'proposal_button' if payload.requestProposal else 'direct_modification',
                    },
                    services.utc_now(),
                )
        if (
            payload.action == 'none'
            and revision_routing == 'needs_clarification'
            and state.proposal_discovery
            and (not state.user_map_claims.get('conflicts'))
        ):
            stage_context['proposalClarification'] = _proposal_clarification_spec(
                state.proposal_discovery,
                stage_context.get('stageSnapshot'),
                state.language,
            )
        if proposal_state.startswith('ready_') and state.proposal_discovery.get('brief'):
            stage_context['authorizedRevisionBrief'] = state.proposal_discovery['brief']
        if state.user_map_claims.get('conflicts'):
            state.allow_progress = False
            services.record_event(
                database,
                req.session_id,
                'map_claim_conflict',
                {
                    'versionId': payload.baseVersionId,
                    'messageKey': payload.idempotencyKey,
                    'claimCount': len(state.user_map_claims.get('conflicts') or []),
                    'conflicts': [
                        {key: value for key, value in conflict.items() if key != 'sourceText'}
                        for conflict in state.user_map_claims['conflicts']
                    ],
                    'mapFingerprint': stage_context.get('mapFingerprint'),
                    'entityBindingFingerprint': stage_context.get('entityBindingFingerprint'),
                },
                services.utc_now(),
            )
        stage_context['discussionCardMode'] = 'disagreement_only'
        stage_context['revisionSourceTurnIds'] = [state.user_turn_id]
        stage_context['explicitProposalRequest'] = payload.requestProposal
        stage_context['explicitAction'] = payload.action
        stage_context['actionSourceTurnId'] = payload.sourceTurnId
        stage_context['sourceProposalOffer'] = state.source_offer
        if state.source_binding is not None and payload.action == 'execute_revision':
            stage_context['proposalBindingFrozen'] = True
            stage_context['authorizedExecutionBrief'] = state.source_binding.get('executionBrief')
            transitions = state.source_binding.get('executionBrief', {}).get('requiredTransitions', [])
            stage_context['deterministicExactExecution'] = bool(transitions)
        stage_context['deferRevisionExecution'] = payload.action != 'execute_revision' and (not proposal_state.startswith('ready_'))
        if (payload.action in REVISION_CARD_ACTIONS or payload.requestProposal) and stage_context.get('activeDisagreement'):
            raise ApiError(409, 'DISAGREEMENT_ACTIVE', 'Resolve the active design disagreement before choosing another revision card.')
        if payload.action == 'challenge_revision':
            stage_context['challengeRevision'] = {'sourceTurnId': payload.sourceTurnId, 'proposalOffer': state.source_offer}
        elif payload.action == 'alternative_revision':
            stage_context['alternativeRevision'] = {'sourceTurnId': payload.sourceTurnId, 'proposalOffer': state.source_offer}
            state.excluded_rows = _rows_after_frozen_proposal(
                state.context['rows'],
                (state.source_binding or {}).get('executionBrief'),
            )
            if state.excluded_rows is not None:
                stage_context['excludedProposalCandidateFingerprint'] = map_fingerprint(state.excluded_rows)
    return state


def _prepare_question_and_challenge_context(req: _MessageRequestContext, state: _RoutedMessageState) -> None:
    """Attach prechecked challenge evidence and evaluate open-question progress."""
    payload = req.payload
    services = req.services
    stage_context = state.stage_context
    if state.deterministic_challenge_choice is not None and state.prechecked_challenge_row is not None:
        stage_context['sourceProposalOffer'] = (load_json(state.prechecked_challenge_row['guidance_json']) or {}).get('proposalOffer') or {}
        stage_context['requirementSourceCardId'] = state.prechecked_challenge_row['id']
        stage_context['requirementReasonTurnId'] = state.prechecked_challenge_row['current_reason_turn_id']
        stage_context['requirementDecisionEvidence'] = {
            'sourceTurnId': state.user_turn_id,
            'choice': state.deterministic_challenge_choice,
            'reasonTurnId': state.prechecked_challenge_row['current_reason_turn_id'],
        }
        if state.deterministic_challenge_choice == 'user':
            state.excluded_rows = _rows_after_frozen_proposal(
                state.context['rows'],
                (state.prechecked_challenge_binding or {}).get('executionBrief'),
            )
            if state.excluded_rows is not None:
                stage_context['excludedProposalCandidateFingerprint'] = map_fingerprint(state.excluded_rows)
    state.question_answer_review = {'answeredQuestionIds': [], 'results': []}
    state.joint_ordinary_review = bool(
        payload.action == 'none'
        and (not payload.requestProposal)
        and (not state.proposal_discovery)
        and (not stage_context.get('activeDisagreement'))
        and (not stage_context.get('conversationOnly'))
        and (not set((stage_context.get('turnUnderstanding') or {}).get('acts') or []) & {'revision_request'})
    )
    stage_context['ordinaryOpenQuestions'] = (
        [
            {'questionId': q.get('id'), 'question': q.get('question')}
            for q in stage_context.get('designContext', {}).get('openQuestions', [])
            if q.get('status') == 'open' and q.get('sourceKind') in {'visible_output', 'legacy'}
        ][:24]
        if state.joint_ordinary_review and state.allow_progress
        else []
    )
    if (
        not state.joint_ordinary_review
        and payload.action in {'none', 'continue_challenge'}
        and (state.deterministic_challenge_choice is None)
        and (state.allow_progress or bool(state.proposal_discovery) or bool(stage_context.get('activeDisagreement')))
    ):
        visible_open_questions = [
            {'questionId': item.get('id'), 'question': item.get('question')}
            for item in stage_context.get('designContext', {}).get('openQuestions', [])
            if item.get('status') == 'open' and item.get('sourceKind') in {'visible_output', 'legacy'}
        ]
        if visible_open_questions:
            optional_started = services.monotonic()
            try:
                state.question_answer_review = services.llm.review_question_answers(
                    visible_open_questions,
                    req.content,
                    state.language,
                    req.request.state.request_id,
                    _deadline=min(optional_started + 10.0, req.message_started_at + req.llm_budget_seconds - 20.0),
                )
            except LLMServiceError:
                state.question_answer_review = {'answeredQuestionIds': [], 'results': []}
            finally:
                stage_context['_optionalRepairSpent'] = services.monotonic() - optional_started
    stage_context['answeredQuestionIds'] = list(state.question_answer_review.get('answeredQuestionIds') or [])
    stage_context['answeredVisibleQuestion'] = bool(stage_context['answeredQuestionIds'])
    state.active_disagreement = stage_context.get('activeDisagreement') or {}


def _resolve_challenge_continuation(req: _MessageRequestContext, state: _RoutedMessageState) -> None:
    """Resolve active AI-revision challenge choices or classify a challenge reason with lease cleanup."""
    payload = req.payload
    services = req.services
    if not (payload.action == 'continue_challenge' or (payload.action == 'none' and (not payload.exitChallenge))):
        return
    stage_context = state.stage_context
    active_disagreement = stage_context.get('activeDisagreement') or {}
    state.active_disagreement = active_disagreement
    if active_disagreement.get('subject') == 'ai_revision_challenge' and active_disagreement.get('phase') == 'choice_pending':
        state.challenge_choice_resolution = state.deterministic_challenge_choice
        stage_context['challengeChoiceResolution'] = state.challenge_choice_resolution
        if state.challenge_choice_resolution:
            choice_key = question_dedup_key(active_disagreement.get('nextQuestion'))
            for item in stage_context.get('designContext', {}).get('openQuestions', []):
                if item.get('status') == 'open' and question_dedup_key(item.get('question')) == choice_key and item.get('id'):
                    answered_ids = state.question_answer_review.setdefault('answeredQuestionIds', [])
                    if item['id'] not in answered_ids:
                        answered_ids.append(item['id'])
                        state.question_answer_review.setdefault('results', []).append(
                            {'questionId': item['id'], 'answered': True, 'source': 'deterministic_challenge_choice'}
                        )
                    break
        return

    challenge_context = stage_context.get('challengeContext') or {}
    hypotheses = challenge_context.get('hypotheses') or {}
    if active_disagreement.get('subject') == 'ai_revision_challenge' and active_disagreement.get('phase') == 'reason_review':
        hypotheses = {
            'primary': active_disagreement.get('primaryHypothesis'),
            'secondary': active_disagreement.get('secondaryHypothesis'),
            'challengeId': active_disagreement.get('challengeId'),
        }
    if not (
        hypotheses.get('primary')
        and hypotheses.get('secondary')
        and (not payload.challengeId or payload.challengeId == hypotheses.get('challengeId'))
    ):
        return

    challenge_id = hypotheses.get('challengeId')
    state.challenge_id = challenge_id
    challenge_review_id = 'cr-' + hashlib.sha256(f'{challenge_id}:{state.user_turn_id}'.encode('utf-8')).hexdigest()[:24]
    state.challenge_review_id = challenge_review_id
    with services.connect(immediate=True) as database:
        now = services.utc_now()
        database.execute(
            "\n                        UPDATE challenge_reason_reviews\n"
            "                        SET status = 'superseded', updated_at = ?\n"
            "                        WHERE session_id = ? AND challenge_id = ?\n"
            "                          AND status = 'review_pending'\n"
            "                          AND source_user_turn_id != ?\n                        ",
            (now, req.session_id, challenge_id, state.user_turn_id),
        )
        database.execute(
            "\n                        INSERT INTO challenge_reason_reviews(\n"
            "                            review_id, session_id, challenge_id,\n"
            "                            source_user_turn_id, message_key, status,\n"
            "                            attempts_used, failure_code, result_json,\n"
            "                            created_at, updated_at\n"
            "                        ) VALUES (?, ?, ?, ?, ?, 'reviewing', 0, NULL, NULL, ?, ?)\n"
            "                        ON CONFLICT(session_id, source_user_turn_id) DO UPDATE SET\n"
            "                            status = 'reviewing', failure_code = NULL,\n"
            "                            updated_at = excluded.updated_at\n                        ",
            (challenge_review_id, req.session_id, challenge_id, state.user_turn_id, payload.idempotencyKey, now, now),
        )
        database.execute(
            "\n                        UPDATE revision_challenges\n"
            "                        SET status = 'reviewing', current_reason_turn_id = ?, updated_at = ?\n"
            "                        WHERE challenge_id = ? AND session_id = ?\n                        ",
            (state.user_turn_id, now, challenge_id, req.session_id),
        )
    state.challenge_deadline = req.message_started_at + req.llm_budget_seconds
    review_settled = False
    try:
        try:
            classification = services.llm.classify_challenge_reason(
                req.content,
                hypotheses,
                active_disagreement.get('proposalSummary') or hypotheses.get('proposalSummary') or challenge_context.get('proposalSummary'),
                req.request.state.request_id,
                _deadline=state.challenge_deadline,
                simple=True,
                stage_snapshot=stage_context.get('stageSnapshot'),
                comparison_validator=lambda comparison: _salvage_challenge_analysis(
                    comparison,
                    state.context['rows'],
                    stage_context.get('entityBindings'),
                    state.language,
                ),
            )
        except LLMServiceError as exception:
            with services.connect(immediate=True) as database:
                active_session = require_active_session(database, req.session_id, req.access_cookie, services=services)
                require_current_base(active_session, payload.baseVersionId)
                duplicate = database.execute(
                    "\n                            SELECT 1 FROM audit_events\n"
                    "                            WHERE session_id = ?\n"
                    "                              AND event_type = 'challenge_reason_review_pending'\n"
                    "                              AND json_extract(payload_json, '$.messageKey') = ?\n"
                    "                            LIMIT 1\n                            ",
                    (req.session_id, payload.idempotencyKey),
                ).fetchone()
                if duplicate is None:
                    services.record_event(
                        database,
                        req.session_id,
                        'challenge_reason_review_pending',
                        {
                            'messageKey': payload.idempotencyKey,
                            'baseVersionId': payload.baseVersionId,
                            'challengeId': hypotheses.get('challengeId') or challenge_context.get('challengeId'),
                            'sourceUserTurnId': state.user_turn_id,
                            'state': 'review_pending',
                            'task': 'challenge_review',
                            'failureStage': 'upstream',
                            'failureCode': exception.code,
                            'attemptsUsed': exception.attempts_used,
                            'maximumAttempts': 2,
                            'retryable': True,
                            'safeReason': exception.safe_message,
                        },
                        services.utc_now(),
                    )
                now = services.utc_now()
                database.execute(
                    "\n                            UPDATE challenge_reason_reviews\n"
                    "                            SET status = 'review_pending', attempts_used = ?,\n"
                    "                                failure_code = ?, updated_at = ?\n"
                    "                            WHERE review_id = ? AND session_id = ?\n                            ",
                    (exception.attempts_used, exception.code, now, challenge_review_id, req.session_id),
                )
                database.execute(
                    "\n                            UPDATE revision_challenges\n"
                    "                            SET status = 'review_pending', updated_at = ?\n"
                    "                            WHERE challenge_id = ? AND session_id = ?\n                            ",
                    (now, challenge_id, req.session_id),
                )
            raise ApiError(
                exception.status_code,
                'CHALLENGE_REVIEW_FAILED',
                exception.safe_message,
                retryable=True,
                details={
                    'task': 'challenge_review',
                    'failureStage': 'upstream',
                    'failureCode': exception.code,
                    'attemptsUsed': exception.attempts_used,
                    'maximumAttempts': 2,
                },
            ) from exception
        comparison = classification.get('comparison')
        if comparison:
            safe_comparison = _salvage_challenge_analysis(comparison, state.context['rows'], stage_context.get('entityBindings'), state.language)
            if not safe_comparison:
                exception = ValueError('No grounded comparison remained after removing invalid fragments.')
                with services.connect(immediate=True) as database:
                    active_session = require_active_session(database, req.session_id, req.access_cookie, services=services)
                    require_current_base(active_session, payload.baseVersionId)
                    services.record_event(
                        database,
                        req.session_id,
                        'challenge_reason_review_pending',
                        {
                            'messageKey': payload.idempotencyKey,
                            'baseVersionId': payload.baseVersionId,
                            'challengeId': hypotheses.get('challengeId'),
                            'sourceUserTurnId': state.user_turn_id,
                            'state': 'review_pending',
                            'task': 'challenge_review',
                            'failureStage': 'binding',
                            'failureCode': 'MAP_GROUNDING_INVALID',
                            'attemptsUsed': classification.get('attemptsUsed', 1),
                            'maximumAttempts': 2,
                            'retryable': True,
                            'safeReason': 'The comparison did not match the current Stage facts.',
                        },
                        services.utc_now(),
                    )
                    now = services.utc_now()
                    database.execute(
                        "\n                                UPDATE challenge_reason_reviews\n"
                        "                                SET status = 'review_pending', attempts_used = ?,\n"
                        "                                    failure_code = 'MAP_GROUNDING_INVALID', updated_at = ?\n"
                        "                                WHERE review_id = ? AND session_id = ?\n                                ",
                        (classification.get('attemptsUsed', 1), now, challenge_review_id, req.session_id),
                    )
                    database.execute(
                        "\n                                UPDATE revision_challenges\n"
                        "                                SET status = 'review_pending', updated_at = ?\n"
                        "                                WHERE challenge_id = ? AND session_id = ?\n                                ",
                        (now, challenge_id, req.session_id),
                    )
                raise ApiError(
                    502,
                    'CHALLENGE_REVIEW_FAILED',
                    "Kimi's judgment did not match the current Stage facts. Retry the message.",
                    retryable=True,
                    details={
                        'task': 'challenge_review',
                        'failureStage': 'binding',
                        'failureCode': 'MAP_GROUNDING_INVALID',
                        'attemptsUsed': classification.get('attemptsUsed', 1),
                        'maximumAttempts': 2,
                    },
                ) from exception
            classification['comparison'] = safe_comparison
        state.challenge_reason_classification = classification
        stage_context['challengeReasonClassification'] = classification
        with services.connect(immediate=True) as database:
            services.record_event(
                database,
                req.session_id,
                'challenge_reason_review_resolved',
                {
                    'messageKey': payload.idempotencyKey,
                    'baseVersionId': payload.baseVersionId,
                    'challengeId': hypotheses.get('challengeId') or challenge_context.get('challengeId'),
                    'sourceUserTurnId': state.user_turn_id,
                    'state': 'reason_review',
                    'attemptsUsed': classification.get('attemptsUsed', 1),
                },
                services.utc_now(),
            )
            now = services.utc_now()
            database.execute(
                "\n                        UPDATE challenge_reason_reviews\n"
                "                        SET status = 'resolved', attempts_used = ?, failure_code = NULL,\n"
                "                            result_json = ?, updated_at = ?\n"
                "                        WHERE review_id = ? AND session_id = ?\n                        ",
                (
                    classification.get('attemptsUsed', 1),
                    dump_json(classification),
                    now,
                    challenge_review_id,
                    req.session_id,
                ),
            )
            review_settled = True
    finally:
        if not review_settled:
            with services.connect(immediate=True) as cleanup_database:
                cleanup_now = services.utc_now()
                cleanup_database.execute(
                    "\n                                UPDATE challenge_reason_reviews\n"
                    "                                SET status = 'review_pending', updated_at = ?\n"
                    "                                WHERE review_id = ? AND session_id = ? AND status = 'reviewing'\n                                ",
                    (cleanup_now, challenge_review_id, req.session_id),
                )
                cleanup_database.execute(
                    "\n                                UPDATE revision_challenges\n"
                    "                                SET status = 'review_pending', updated_at = ?\n"
                    "                                WHERE challenge_id = ? AND session_id = ? AND status = 'reviewing'\n                                ",
                    (cleanup_now, challenge_id, req.session_id),
                )


def _prepare_revision_context_and_evidence(req: _MessageRequestContext, state: _RoutedMessageState) -> None:
    """Resolve revision authorization, evidence user turns, requirement cache, and pre-generation deadline."""
    payload = req.payload
    services = req.services
    stage_context = state.stage_context
    revision_state, revision_brief = services.llm.classify_revision_request(state.context['conversation'], stage_context)
    if payload.action == 'execute_revision':
        revision_state = 'authorized'
        revision_brief = ' '.join((str(state.source_offer.get(field) or '').strip() for field in ('summary', 'rationale'))).strip()
    elif payload.action == 'challenge_revision':
        revision_state, revision_brief = ('not_request', None)
    elif payload.action == 'alternative_revision':
        revision_state = 'proposal_requested'
        revision_brief = ' '.join((str(state.source_offer.get(field) or '').strip() for field in ('summary', 'rationale'))).strip()
        stage_context['authorizedRevisionBrief'] = revision_brief
    elif state.challenge_choice_resolution == 'retain_current':
        revision_state, revision_brief = ('not_request', None)
    elif state.challenge_choice_resolution is not None:
        active_disagreement = stage_context.get('activeDisagreement') or {}
        state.active_disagreement = active_disagreement
        accepted_reason = str(active_disagreement.get('acceptedReason') or '').strip()
        proposal_summary = str(active_disagreement.get('proposalSummary') or '').strip()
        if state.challenge_choice_resolution == 'ai':
            challenge_source_id = (stage_context.get('challengeContext') or {}).get('sourceTurnId')
            challenge_id = active_disagreement.get('challengeId')
            state.challenge_id = challenge_id
            if challenge_id:
                with services.connect() as database:
                    challenge_row = database.execute(
                        'SELECT source_proposal_turn_id FROM revision_challenges\n'
                        '                        WHERE session_id = ? AND challenge_id = ? AND base_version_id = ?',
                        (req.session_id, challenge_id, payload.baseVersionId),
                    ).fetchone()
                if challenge_row is None:
                    raise ApiError(409, 'CHALLENGE_STALE', 'The challenged proposal is no longer available.')
                challenge_source_id = challenge_row['source_proposal_turn_id']
            else:
                raise ApiError(409, 'CHALLENGE_STALE', 'The active challenge has no source record.')
            if not challenge_source_id:
                raise ApiError(409, 'INVALID_CARD_SOURCE', 'The challenged proposal has no source card.')
            with services.connect() as database:
                challenge_source = database.execute(
                    'SELECT version_id, guidance_json, proposal_binding_json\n'
                    '                        FROM conversation_turns WHERE session_id = ? AND id = ?',
                    (req.session_id, challenge_source_id),
                ).fetchone()
            if challenge_source is None or challenge_source['version_id'] != payload.baseVersionId:
                raise ApiError(409, 'PROPOSAL_STALE', 'The challenged proposal belongs to another Stage.')
            challenged_binding = load_json(challenge_source['proposal_binding_json']) or {}
            state.challenged_binding = challenged_binding
            challenged_brief = challenged_binding.get('executionBrief')
            if not isinstance(challenged_brief, dict) or not challenged_brief.get('requiredTransitions'):
                raise ApiError(409, 'INVALID_CARD_SOURCE', 'The challenged proposal has no exact tile changes.')
            if challenged_binding.get('mapFingerprint') != map_fingerprint(state.context['rows']):
                raise ApiError(409, 'PROPOSAL_STALE', 'The challenged map has changed.')
            try:
                challenged_brief = validate_execution_brief(
                    challenged_brief,
                    state.context['rows'],
                    stage_context.get('entityBindings'),
                )
            except ValueError as error:
                raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The challenged tile changes are no longer applicable.') from error
            source_guidance = load_json(challenge_source['guidance_json']) or {}
            challenged_offer = source_guidance.get('proposalOffer') or {}
            challenged_workflow = (
                (
                    challenged_binding.get('revisionWorkflow')
                    if isinstance(challenged_binding.get('revisionWorkflow'), dict) and challenged_binding.get('revisionWorkflow')
                    else None
                )
                or (
                    challenged_offer.get('revisionWorkflow')
                    if isinstance(challenged_offer, dict)
                    and isinstance(challenged_offer.get('revisionWorkflow'), dict)
                    and challenged_offer.get('revisionWorkflow')
                    else None
                )
                or {}
            )
            stage_context.update(
                {
                    'explicitAction': 'execute_revision',
                    'deterministicExactExecution': True,
                    'proposalBindingFrozen': True,
                    'authorizedExecutionBrief': challenged_brief,
                    'sourceProposalOffer': challenged_offer,
                    'challengedRevisionWorkflow': copy.deepcopy(challenged_workflow),
                    'challengedSourceOfferTurnId': challenge_source_id,
                }
            )
            revision_brief = proposal_summary
        else:
            revision_brief = f"Use the designer's accepted reason as the primary direction: {accepted_reason}".strip()
        revision_state = 'proposal_requested'
        stage_context['revisionRouting'] = 'proposal'
        stage_context['proposalState'] = 'ready_with_explicit_binding'
        stage_context['authorizedRevisionBrief'] = revision_brief
        stage_context['activeDisagreement'] = None
    elif state.challenge_reason_classification is not None:
        revision_state, revision_brief = ('not_request', None)
    elif stage_context.get('proposalState', '').startswith('ready_'):
        revision_state = 'proposal_requested'
        revision_brief = str(stage_context.get('authorizedRevisionBrief') or revision_brief or '').strip()

    state.revision_state = revision_state
    state.revision_brief = revision_brief
    stage_context['answeredQuestionIds'] = list(state.question_answer_review.get('answeredQuestionIds') or [])
    stage_context['answeredVisibleQuestion'] = bool(stage_context['answeredQuestionIds'])
    stage_context['revisionRequestState'] = revision_state
    evidence_context = stage_context
    source_workflow = (evidence_context.get('sourceProposalOffer') or {}).get('revisionWorkflow') or {}
    source_record = source_workflow.get('requirementRecord') or {}
    source_ids = {x['sourceTurnId'] for x in source_record.get('requirements') or []}
    source_ids.update((x['sourceTurnId'] for x in source_record.get('exactTransitions') or []))
    source_ids.update(
        (
            x
            for x in (
                evidence_context.get('requirementSourceCardId'),
                evidence_context.get('requirementReasonTurnId'),
                payload.sourceTurnId,
            )
            if x
        )
    )
    projection = evidence_context.get('revisionDesignContext') or {}
    for memory in (projection.get('activeGoals') or []) + (projection.get('activeConstraints') or []):
        if memory.get('sourceTurnId'):
            source_ids.add(memory['sourceTurnId'])
    with services.connect() as evidence_database:
        prior_sources = evidence_database.execute(
            'SELECT id, content, role, request_id, version_id FROM conversation_turns WHERE session_id = ? ORDER BY sequence_number',
            (req.session_id,),
        ).fetchall()
    evidence_by_id = {row['id']: row for row in prior_sources}
    paired_keys = {
        (evidence_by_id[source_id]['request_id'], evidence_by_id[source_id]['version_id'])
        for source_id in source_ids
        if source_id in evidence_by_id and evidence_by_id[source_id]['role'] == 'assistant' and evidence_by_id[source_id]['request_id']
    }
    prior_sources = [
        {'id': row['id'], 'content': row['content']}
        for row in prior_sources
        if row['role'] == 'user' and (row['id'] in source_ids or (row['request_id'], row['version_id']) in paired_keys)
    ]
    current_sources = (evidence_context.get('proposalDiscovery') or {}).get('userTurns') or evidence_context.get('requirementUserTurns') or []
    if state.challenge_choice_resolution is not None:
        current_sources = [item for item in current_sources if item['id'] != state.user_turn_id]
    evidence_context['requirementUserTurns'] = list({x['id']: x for x in [*prior_sources, *current_sources]}.values())
    with services.connect() as evidence_database:
        cached_reviews = evidence_database.execute(
            "SELECT payload_json FROM audit_events WHERE session_id = ?\n"
            "               AND event_type = 'requirements_verified' AND json_extract(payload_json, '$.baseVersionId') = ?\n"
            "               ORDER BY created_at DESC LIMIT 8",
            (req.session_id, payload.baseVersionId),
        ).fetchall()
    evidence_context['verifiedRequirementCache'] = [load_json(row['payload_json']) for row in cached_reviews]
    evidence_context['_reliabilityState'] = {}
    if payload.action in REVISION_CARD_ACTIONS and (not source_record):
        evidence_context.pop('requirementPolicyVersion', None)
    state.proposal_branch = bool(
        payload.action in REVISION_CARD_ACTIONS
        or payload.requestProposal
        or stage_context.get('activeDisagreement')
        or stage_context.get('proposalDiscovery')
        or (stage_context.get('revisionRouting') in {'needs_clarification', 'proposal', 'proposal_conservative', 'proposal_blocked'})
    )
    stage_context['conversationBranch'] = 'proposal' if state.proposal_branch else 'ordinary'
    trace_reply(
        'routing',
        'passed',
        branch='conversation_only'
        if stage_context.get('conversationOnly')
        else 'disagreement'
        if stage_context.get('activeDisagreement')
        else stage_context['conversationBranch'],
    )
    state.message_deadline = state.challenge_deadline or req.message_started_at + req.llm_budget_seconds
    state.recovery_execution = (
        _verified_recovery_suggestion_execution(
            state.proposal_discovery,
            req.content,
            state.context['rows'],
            state.language,
            req.request.state.request_id,
            services=services,
        )
        if state.proposal_discovery
        else None
    )


def _generate_message_execution(req: _MessageRequestContext, state: _RoutedMessageState) -> None:
    """Produce the assistant LLMExecutionResult across deterministic guards or Kimi generation."""
    payload = req.payload
    services = req.services
    stage_context = state.stage_context
    state.revision_failure = None
    if payload.action == 'challenge_revision':
        state.execution = LLMExecutionResult(
            '请说明你质疑的理由。' if state.language == 'zh-CN' else 'Please explain why you challenge this proposal.',
            0,
            req.request.state.request_id,
            model='server',
            guidance={'proposalOffer': None, 'disagreement': None, 'uiCues': []},
        )
    elif state.challenge_reason_classification is not None:
        comparison = str(state.challenge_reason_classification.get('comparison') or '').strip()
        if not comparison:
            comparison = (
                '我还无法判断这个理由具体指向哪处玩法变化。请再说明你担心的效果。'
                if state.language == 'zh-CN'
                else 'I cannot yet tell which play effect concerns you. Please explain that effect more concretely.'
            )
        state.execution = LLMExecutionResult(
            comparison,
            state.challenge_reason_classification.get('attemptsUsed', 1),
            req.request.state.request_id,
            model=KIMI_MODEL,
            guidance={'proposalOffer': None, 'disagreement': None, 'uiCues': []},
        )
    elif stage_context.get('revisionRouting') == 'proposal_cancelled':
        state.execution = _proposal_cancelled_execution(state.language, req.request.state.request_id)
    elif state.recovery_execution is not None:
        state.execution = state.recovery_execution
    elif stage_context.get('revisionRouting') == 'protected_request':
        try:
            permission_message = protected_request_message(state.turn_understanding, state.language)
        except ValueError as exception:
            raise ApiError(
                502,
                'MODEL_RESPONSE_INVALID',
                'The requested edit could not be reliably verified. Retry this message.',
                retryable=True,
            ) from exception
        state.execution = LLMExecutionResult(
            permission_message,
            0,
            req.request.state.request_id,
            model='server-permission-guard',
            guidance={'proposalOffer': None, 'disagreement': None, 'followUpQuestion': None, 'uiCues': []},
        )
    elif state.revision_state == 'relaxation_confirmed':
        state.execution = _relaxed_revision_suggestion_execution(stage_context, state.language, req.request.state.request_id)
    else:
        if state.proposal_discovery and stage_context.get('revisionRouting') in {'proposal', 'proposal_conservative'}:
            with services.connect(immediate=True) as database:
                duplicate_planning = database.execute(
                    "\n                    SELECT 1 FROM audit_events\n"
                    "                    WHERE session_id = ? AND event_type = 'proposal_discovery_progress'\n"
                    "                      AND json_extract(payload_json, '$.messageKey') = ?\n"
                    "                      AND json_extract(payload_json, '$.status') = 'planning'\n"
                    "                    LIMIT 1\n                    ",
                    (req.session_id, payload.idempotencyKey),
                ).fetchone()
                if duplicate_planning is None:
                    services.record_event(
                        database,
                        req.session_id,
                        'proposal_discovery_progress',
                        {
                            'stageId': payload.baseVersionId,
                            'topicId': state.proposal_discovery.get('topicId'),
                            'messageKey': payload.idempotencyKey,
                            'status': 'planning',
                            'clarificationQuestionCount': state.proposal_discovery.get('clarificationQuestionCount', 0),
                        },
                        services.utc_now(),
                    )
        try:
            state.execution = services.llm.generate_chat_reply(
                state.context['conversation'],
                state.context['rows'],
                req.request.state.request_id,
                language=state.language,
                solver_metrics=state.context['validation'],
                play_summary=state.context['playSummary'],
                proposal_validator=lambda proposed_rows: _validate_changed_proposal(
                    state.context['rows'],
                    proposed_rows,
                    services=services,
                ),
                stage_context=stage_context,
                _deadline=state.message_deadline,
            )
        except LLMServiceError as exception:
            _persist_verified_requirements(req.session_id, payload.baseVersionId, stage_context, services=services)
            state.revision_failure = exception
            exception.requirement_policy_version = stage_context.get('requirementPolicyVersion')
            proposal_retryable_failure = bool(state.proposal_branch and exception.retryable)
            if proposal_retryable_failure:
                execution = None
            elif state.proposal_discovery:
                execution = _classified_proposal_failure_execution(
                    language=state.language,
                    request_id=exception.request_id,
                    exception=exception,
                )
            elif state.revision_state == 'proposal_requested':
                execution = _automatic_proposal_failure_execution(
                    language=state.language,
                    request_id=exception.request_id,
                    exception=exception,
                )
            elif exception.code in {'REVISION_CONTRACT_INVALID', 'REVISION_CONTRACT_CONFLICT'}:
                execution = _revision_contract_clarification_execution(
                    language=state.language,
                    request_id=exception.request_id,
                    exception=exception,
                )
            else:
                execution = _proposal_search_failure_execution(
                    session_id=req.session_id,
                    access_cookie=req.access_cookie,
                    base_version_id=payload.baseVersionId,
                    idempotency_key=payload.idempotencyKey,
                    revision_state=state.revision_state,
                    revision_brief=state.revision_brief,
                    language=state.language,
                    exception=exception,
                    services=services,
                )
            if execution is None:
                ordinary_message_failure = payload.action == 'none' and state.revision_state == 'not_request'
                if proposal_retryable_failure or not state.retrying_failed_message or ordinary_message_failure:
                    with services.connect(immediate=True) as database:
                        active_session = require_active_session(database, req.session_id, req.access_cookie, services=services)
                        require_current_base(active_session, payload.baseVersionId)
                        services.record_event(
                            database,
                            req.session_id,
                            'message_generation_failed',
                            {
                                'baseVersionId': payload.baseVersionId,
                                'messageKey': payload.idempotencyKey,
                                'code': exception.code,
                                'attemptsUsed': exception.attempts_used,
                                'requestId': exception.request_id,
                                'retryable': exception.retryable,
                                'details': _llm_failure_details(exception),
                                'retryingExistingUserTurn': state.retrying_failed_message,
                                'modelGenerationSucceeded': (stage_context.get('_reliabilityState') or {}).get('modelGenerationSucceeded'),
                                'proposalRequested': (
                                    payload.requestProposal
                                    or state.revision_state == 'proposal_requested'
                                    or payload.action in {'execute_revision', 'alternative_revision'}
                                    or bool(state.challenge_choice_resolution)
                                ),
                            },
                            services.utc_now(),
                        )
                        if state.proposal_discovery:
                            services.record_event(
                                database,
                                req.session_id,
                                'proposal_discovery_progress',
                                {
                                    'stageId': payload.baseVersionId,
                                    'topicId': state.proposal_discovery.get('topicId'),
                                    'messageKey': payload.idempotencyKey,
                                    'status': 'retry_pending',
                                    'retryable': True,
                                    'failureCode': exception.code,
                                    'clarificationQuestionCount': state.proposal_discovery.get('clarificationQuestionCount', 0),
                                },
                                services.utc_now(),
                            )
                    raise
                execution = services.retry_exhausted_execution(state.language, exception)
                with services.connect(immediate=True) as database:
                    active_session = require_active_session(database, req.session_id, req.access_cookie, services=services)
                    require_current_base(active_session, payload.baseVersionId)
                    services.record_event(
                        database,
                        req.session_id,
                        'message_retry_exhausted',
                        {
                            'baseVersionId': payload.baseVersionId,
                            'messageKey': payload.idempotencyKey,
                            'code': exception.code,
                            'attemptsUsed': exception.attempts_used,
                        },
                        services.utc_now(),
                    )
            state.execution = execution
    _persist_verified_requirements(req.session_id, payload.baseVersionId, stage_context, services=services)


def _review_candidate_intent_step(
    req: _MessageRequestContext,
    state: _RoutedMessageState,
    execution_guidance: dict,
    candidate_claims: list,
) -> None:
    """Resolve deterministic or Kimi-verified conflict state for an eligible candidate intent hypothesis."""
    confirmed_intents = [
        {
            'hypothesisId': str(item.get('id') or '').strip(),
            'statement': str(item.get('statement') or '').strip(),
            'semanticClaims': item.get('semanticClaims') or [],
        }
        for item in state.stage_context.get('designContext', {}).get('intentHypotheses', [])
        if item.get('status') == 'confirmed'
        and str(item.get('id') or '').strip()
        and str(item.get('statement') or '').strip()
    ]
    if not confirmed_intents:
        return
    deterministic_conflicts = []
    for item in confirmed_intents:
        pair = _deterministic_intent_conflict(candidate_claims, item.get('semanticClaims'))
        if pair:
            deterministic_conflicts.append((item, pair))
    if deterministic_conflicts:
        selected, conflict_pair = deterministic_conflicts[-1]
        intent_review = {
            'verdict': 'conflict',
            'explanation': _deterministic_conflict_explanation(conflict_pair, state.language),
            'conflictingHypothesisIds': [selected['hypothesisId']],
            'supersedesHypothesisIds': [],
            'reviewSource': 'deterministic',
            'conflictClaims': conflict_pair,
        }
    elif any((_intent_claims_equivalent(candidate_claims, item.get('semanticClaims')) for item in confirmed_intents)):
        execution_guidance['intentHypothesis'] = None
        execution_guidance['intentConfidence'] = None
        execution_guidance.pop('_intentSemanticClaims', None)
        state.execution = replace(state.execution, guidance=execution_guidance)
        return
    else:
        candidate_review = execution_guidance.get('_intentCandidateReview') or {}
        intent_review = {
            'verdict': candidate_review.get('relation', 'unclear'),
            'explanation': candidate_review.get('explanation'),
            'conflictingHypothesisIds': candidate_review.get('conflictingHypothesisIds', []),
            'supersedesHypothesisIds': [],
            'reviewSource': 'kimi',
            'reviewVersion': candidate_review.get('reviewVersion'),
            'reviewedActiveClaims': candidate_review.get('reviewedActiveClaims', []),
        }
    if intent_review and intent_review.get('verdict') == 'conflict':
        conflicting_ids = set(intent_review.get('conflictingHypothesisIds') or [])
        latest_conflict = next((item for item in reversed(confirmed_intents) if item['hypothesisId'] in conflicting_ids), None)
        if latest_conflict is None:
            raise LLMServiceError(
                'MODEL_RESPONSE_INVALID',
                'Kimi did not bind the intent conflict to confirmed memory.',
                req.request.state.request_id,
                True,
                1,
                502,
            )
        explanation = str(intent_review.get('explanation') or '').strip()
        if explanation and explanation not in str(state.execution.assistant_message or ''):
            state.execution = replace(
                state.execution,
                assistant_message='\n\n'.join((part for part in (str(state.execution.assistant_message or '').strip(), explanation) if part)),
            )
        execution_guidance['_intentReviewState'] = {
            'interactionMode': 'conflict_choice',
            'reviewOutcome': 'conflict',
            'reviewExplanation': explanation,
            'existingHypothesisId': latest_conflict['hypothesisId'],
            'reviewSource': intent_review.get('reviewSource', 'kimi'),
            'conflictClaims': intent_review.get('conflictClaims'),
            'newEvidenceText': req.content,
        }
        state.execution = replace(state.execution, guidance=execution_guidance)


def _finalize_execution_guidance_and_intent(req: _MessageRequestContext, state: _RoutedMessageState) -> None:
    """Enforce challenge/manual-edit/conversation policies, bind stage links, and split public vs internal guidance."""
    payload = req.payload
    services = req.services
    stage_context = state.stage_context
    if state.joint_ordinary_review:
        state.question_answer_review = stage_context.get('_reliabilityState', {}).get('questionAnswerReview') or state.question_answer_review
        state.execution = replace(
            state.execution,
            proposal_diagnostics={
                **(state.execution.proposal_diagnostics or {}),
                'questionProgressUpdated': bool(state.question_answer_review.get('answeredQuestionIds')),
            },
        )
    state.proposal_recovery = bool((state.execution.proposal_diagnostics or {}).get('verifiedFailureAnalysis'))
    if state.proposal_recovery and state.challenge_choice_resolution is not None:
        state.challenge_choice_resolution = None
    if payload.action == 'challenge_revision':
        state.execution = _sanitize_challenge_execution(
            state.execution,
            state.source_offer,
            state.language,
            state.source_binding,
            payload.sourceTurnId,
        )
    elif state.challenge_reason_classification is not None:
        state.execution = _enforce_challenge_reason_execution(
            state.execution,
            state.challenge_reason_classification,
            req.content,
            stage_context.get('challengeContext') or {},
            state.active_disagreement,
            state.language,
        )
    if (
        payload.action != 'challenge_revision'
        and payload.action != 'execute_revision'
        and (payload.action != 'continue_challenge' or state.challenge_choice_resolution == 'user')
        and (state.execution.proposed_rows is not None)
    ):
        if state.revision_state == 'proposal_requested':
            state.execution = _materialize_verified_automatic_offer(
                state.execution,
                state.context['rows'],
                state.language,
                stage_context,
                deadline=req.request.state.llm_deadline,
                services=services,
            )
        else:
            state.execution = replace(
                state.execution,
                proposed_rows=None,
                revision_plan={},
                revision_contract={},
                revision_operations=[],
                proposal_diagnostics={},
            )
    if payload.action == 'alternative_revision' and (not state.proposal_recovery):
        state.execution = _ensure_alternative_revision_execution(
            state.execution,
            state.source_offer,
            state.language,
            state.source_binding,
        )
    model_disagreement = (state.execution.guidance or {}).get('disagreement')
    model_retain_resolution = (
        isinstance(model_disagreement, dict)
        and model_disagreement.get('status') == 'resolved'
        and model_disagreement.get('resolution') == 'retain_current'
    )
    if (
        payload.action in {'none', 'continue_challenge'}
        and state.active_disagreement.get('subject') == 'ai_revision_challenge'
        and (state.active_disagreement.get('phase') == 'choice_pending' or model_retain_resolution)
    ):
        state.execution = _enforce_challenge_choice_execution(
            state.execution,
            state.active_disagreement,
            state.challenge_choice_resolution,
            state.language,
        )
    if state.challenge_choice_resolution == 'user':
        offer = (state.execution.guidance or {}).get('proposalOffer')
        if not isinstance(offer, dict) or not offer.get('executionBrief'):
            raise ApiError(
                502,
                'PROPOSAL_SEARCH_EXHAUSTED',
                'No verified new proposal could be produced from this reason; the map was not changed.',
                retryable=True,
            )
        with services.connect() as database:
            source_row = database.execute(
                'SELECT turn.proposal_binding_json FROM revision_challenges AS challenge\n'
                '                JOIN conversation_turns AS turn ON turn.id = challenge.source_proposal_turn_id\n'
                '                WHERE challenge.session_id = ? AND challenge.challenge_id = ?\n'
                '                  AND challenge.base_version_id = ?',
                (req.session_id, state.active_disagreement.get('challengeId'), payload.baseVersionId),
            ).fetchone()
        if source_row is None:
            raise ApiError(409, 'CHALLENGE_STALE', 'The challenged proposal is no longer available.')
        original_brief = (load_json(source_row['proposal_binding_json']) or {}).get('executionBrief') or {}

        def transition_set(brief):
            return {
                (item.get('row'), item.get('column'), item.get('from'), item.get('to'))
                for item in brief.get('requiredTransitions') or []
                if isinstance(item, dict)
            }

        if transition_set(offer['executionBrief']) == transition_set(original_brief):
            raise ApiError(
                502,
                'PROPOSAL_SEARCH_EXHAUSTED',
                'The new direction repeated the challenged proposal; the map was not changed.',
                retryable=True,
            )
        guidance = dict(state.execution.guidance or {})
        guidance['proposalOffer'] = {**offer, 'availableActions': ['execute_revision']}
        state.execution = replace(state.execution, guidance=guidance)
    if payload.action == 'none' and stage_context.get('source') == 'human_edit':
        state.execution = _normalize_manual_edit_review_execution(state.execution, stage_context)
    if payload.action == 'none':
        state.execution = apply_map_claim_conflict_policy(state.execution, state.user_map_claims.get('conflicts'), state.language)
    state.execution = _mark_new_discussion_guidance(state.execution, stage_context)
    state.execution = _mark_proposal_discovery_guidance(state.execution, stage_context)
    state.execution = _enforce_human_edit_disagreement_followup(state.execution, stage_context)
    if stage_context.get('conversationOnly'):
        state.execution = apply_conversation_only_policy(
            state.execution,
            active_disagreement=stage_context.get('activeDisagreement'),
        )
    execution_guidance = dict(state.execution.guidance or {})
    active_disagreement_card = isinstance(execution_guidance.get('disagreement'), dict) and execution_guidance['disagreement'].get('status') == 'active'
    candidate_intent = str(execution_guidance.get('intentHypothesis') or '').strip()
    candidate_claims = execution_guidance.get('_intentSemanticClaims') or [] if candidate_intent else []
    intent_allowed = bool(
        state.allow_progress
        and stage_context.get('conversationBranch') == 'ordinary'
        and execution_guidance.get('_intentDecision', {}).get('classification') == 'candidate'
    )
    if candidate_intent and (
        not intent_allowed
        or payload.requestProposal
        or payload.action != 'none'
        or execution_guidance.get('proposalOffer')
        or active_disagreement_card
    ):
        execution_guidance['intentHypothesis'] = None
        execution_guidance['intentConfidence'] = None
        state.execution = replace(state.execution, guidance=execution_guidance)
        candidate_intent = ''
    if candidate_intent:
        _review_candidate_intent_step(req, state, execution_guidance, candidate_claims)
    state.execution = _bind_execution_to_stage(
        state.execution,
        payload.baseVersionId,
        state.context['rows'],
        req.content,
        state.language,
        stage_context.get('entityBindings'),
    )
    state.state_transition_deferred = bool((state.execution.guidance or {}).get('_stateTransitionDeferred'))
    state.design_context_guidance = _design_context_guidance(state.execution.guidance)
    state.execution = replace(state.execution, guidance=_public_guidance(state.execution.guidance))
    req.response.headers['X-LLM-Attempts-Used'] = str(state.execution.attempts_used)


def _validate_commit_cas_and_prepare_state(database, req: _MessageRequestContext, state: _RoutedMessageState) -> tuple[Any, Any]:
    """Enforce final-write-transaction CAS, disagreement, intent-conflict, and card guards."""
    payload = req.payload
    services = req.services
    session = require_active_session(database, req.session_id, req.access_cookie, services=services)
    if payload.baseVersionId != session['current_version_id'] and (
        state.execution.proposed_rows is not None
        or (state.execution.guidance or {}).get('proposalOffer')
        or (state.execution.guidance or {}).get('proposalDiscovery')
        or (payload.action != 'none')
    ):
        raise ApiError(
            409,
            'PROPOSAL_STALE',
            'The Stage version changed while preparing this proposal. Send a request based on the current Stage.',
            details={'reason': 'version_changed'},
        )
    require_current_base(session, payload.baseVersionId)
    current = get_current_version(database, session)
    captured_binding = state.context.get('commitBinding')
    current_binding = _message_commit_binding(database, req.session_id, payload.baseVersionId)
    source_sequence = database.execute(
        'SELECT sequence_number FROM conversation_turns WHERE id = ? AND session_id = ?',
        (state.user_turn_id, req.session_id),
    ).fetchone()
    guard_id = getattr(req.request.state, 'interaction_guard_id', None)
    guard_receipt = (
        database.execute(
            "SELECT id FROM audit_events WHERE session_id = ? AND event_type = 'interaction_message_guard'\n"
            "            AND json_extract(payload_json, '$.guardId') = ? AND json_extract(payload_json, '$.status') = 'started' ORDER BY id LIMIT 1",
            (req.session_id, guard_id),
        ).fetchone()
        if guard_id
        else None
    )
    superseded = bool(source_sequence and source_sequence['sequence_number'] != current_binding['latestUserSequence'])
    newer_receipt = bool(guard_receipt and guard_receipt['id'] != current_binding['latestMessageReceipt'])
    if captured_binding and (captured_binding != current_binding or superseded or newer_receipt):
        if (
            state.execution.proposed_rows is not None
            or (state.execution.guidance or {}).get('proposalOffer')
            or (state.execution.guidance or {}).get('proposalDiscovery')
            or (payload.action != 'none')
        ):
            raise ApiError(
                409,
                'PROPOSAL_STALE',
                'The dialogue evidence or interaction state changed. Send a request based on the current dialogue.',
                retryable=False,
                details={'failureStage': 'context_binding', 'failureKind': 'state_or_evidence_change'},
            )
        trace_reply('commit', 'preserved', 'CONCURRENT_CHANGE', cardPresent=False)
        body = _strip_unverified_state_claims(state.execution.assistant_message)
        if not body.strip():
            raise ApiError(
                502,
                'MODEL_RESPONSE_INVALID',
                'No reliable answer remains after the context changed. Retry the original message.',
                retryable=True,
            )
        canonical_active = load_design_context(database, req.session_id, payload.baseVersionId).get('activeDisagreement')
        safe_guidance = {
            'move': 'offer_perspective',
            'intentHypothesis': None,
            'intentConfidence': None,
            'proposalOffer': None,
            'followUpQuestion': None,
            'uiCues': [],
            'disagreement': None,
        }
        state.design_context_guidance = dict(safe_guidance)
        state.allow_progress = False
        state.question_answer_review = {'results': [], 'answeredQuestionIds': []}
        initial_disagreement_for_deferred = state.stage_context.get('activeDisagreement')
        initial_identity = component_recovery.disagreement_identity(
            database, req.session_id, payload.baseVersionId, initial_disagreement_for_deferred
        )
        canonical_identity = component_recovery.disagreement_identity(
            database, req.session_id, payload.baseVersionId, canonical_active
        )
        state.state_transition_deferred = bool(
            not superseded
            and not newer_receipt
            and canonical_active
            and canonical_active.get('subject') == 'human_edit'
            and (canonical_active.get('status') == 'active')
            and canonical_identity
            and canonical_identity == initial_identity
        )
        state.execution = replace(
            state.execution,
            assistant_message=body,
            guidance=safe_guidance,
            proposal_diagnostics={
                **(state.execution.proposal_diagnostics or {}),
                'fullQualityPassed': False,
                'componentChecks': [{'component': 'context_binding', 'status': 'preserved', 'code': 'CONCURRENT_CHANGE'}],
            },
        )
    requested_disagreement = state.design_context_guidance.get('disagreement')
    initial_disagreement = state.stage_context.get('activeDisagreement')
    if initial_disagreement and initial_disagreement.get('subject') == 'human_edit':
        current_disagreement = load_design_context(database, req.session_id, payload.baseVersionId).get('activeDisagreement')
        if component_recovery.signature(initial_disagreement) != component_recovery.signature(current_disagreement):
            state.design_context_guidance.pop('disagreement', None)
            guidance = dict(state.execution.guidance or {})
            guidance.pop('disagreement', None)
            state.execution = replace(
                state.execution,
                guidance=guidance,
                assistant_message=_strip_unverified_state_claims(state.execution.assistant_message),
            )
            state.state_transition_deferred = False
            if not state.execution.assistant_message.strip():
                raise ApiError(
                    502,
                    'MODEL_RESPONSE_INVALID',
                    'No reliable answer remains after the state changed. Retry the message.',
                    retryable=True,
                )
    if isinstance(requested_disagreement, dict) and requested_disagreement.get('subject') == 'human_edit':
        identity = component_recovery.disagreement_identity(database, req.session_id, payload.baseVersionId, initial_disagreement)
        if identity or not initial_disagreement or initial_disagreement.get('subject') != 'human_edit':
            requested_disagreement['disagreementId'] = identity or uuid.uuid4().hex
    if state.design_context_guidance.get('intentHypothesis'):
        pending_conflict = _active_intent_conflict_choice(database, req.session_id, payload.baseVersionId)
        if pending_conflict is not None:
            raise ApiError(409, 'INTENT_CONFLICT_PENDING', 'Retain one of the conflicting inclinations before adding another intent card.')
        review_state = state.design_context_guidance.get('_intentReviewState') or {}
        if review_state.get('interactionMode') == 'conflict_choice':
            current_context = load_design_context(database, req.session_id, payload.baseVersionId)
            existing_id = review_state.get('existingHypothesisId')
            existing = next((item for item in current_context.get('intentHypotheses', []) if item.get('id') == existing_id), None)
            if existing is None or existing.get('status') != 'confirmed':
                raise ApiError(
                    409,
                    'INTENT_CONTEXT_CHANGED',
                    'Confirmed intent memory changed while the conflict was being reviewed. Retry the message.',
                    retryable=True,
                )
    if payload.action in REVISION_CARD_ACTIONS:
        source_turn, source_offer = _source_revision_offer(
            database, req.session_id, payload.baseVersionId, payload.sourceTurnId
        )
        _require_offer_action(source_offer, payload.action)
        source_binding = _preflight_proposal(
            database,
            req.session_id,
            session,
            source_turn,
            source_offer,
            load_json(current['rows_json']),
            services=services,
        )
        state.source_turn = source_turn
        state.source_offer = source_offer
        state.source_binding = source_binding
    return session, current


def _persist_pending_proposal_if_needed(
    database,
    req: _MessageRequestContext,
    state: _RoutedMessageState,
    current,
) -> tuple[Any, list[str] | None]:
    """Validate candidate proposed rows and finalize execution metadata inside the commit transaction."""
    payload = req.payload
    services = req.services
    proposal_validation = None
    current_rows = None
    if state.revision_failure is not None and payload.action == 'execute_revision':
        services.record_event(
            database,
            req.session_id,
            'proposal_generation_exhausted',
            {
                'stageId': payload.baseVersionId,
                'sourceTurnId': payload.sourceTurnId,
                'mapFingerprint': map_fingerprint(load_json(current['rows_json'])),
                'reason': state.revision_failure.code,
                'attempts': state.revision_failure.attempts_used,
                'deterministicFallback': bool((getattr(state.revision_failure, 'proposal_diagnostics', {}) or {}).get('deterministicFallback')),
                'providerStatus': getattr(state.revision_failure, 'provider_status', None),
                'providerErrorType': getattr(state.revision_failure, 'provider_error_type', '') or None,
                'providerErrorCode': getattr(state.revision_failure, 'provider_error_code', '') or None,
                'providerParam': getattr(state.revision_failure, 'provider_param', '') or None,
                'providerMessage': getattr(state.revision_failure, 'provider_message', '') or None,
            },
            services.utc_now(),
        )
    if state.execution.proposed_rows is not None:
        current_rows = load_json(current['rows_json'])
        _execute_revision_candidate_or_api_error(current_rows, state.execution, services=services)
        proposal_validation = _solve_changed_proposal_or_api_error(
            current_rows,
            state.execution.proposed_rows,
            deadline=req.request.state.llm_deadline,
            services=services,
        )
        verified_summary = summarize_verified_diff(current_rows, proposal_validation.rows, state.language)
        if state.stage_context.get('proposalBindingMode') == 'conservative':
            prefix = (
                '这是基于当前布局补全对象关系的一份最小改动候选，仍等待你审查。'
                if state.language == 'zh-CN'
                else 'This is a minimal-change candidate whose object binding was completed from the current layout; it remains yours to review.'
            )
            verified_summary = f'{prefix}\n\n{verified_summary}'
        pre_review_offer = (state.execution.guidance or {}).get('proposalOffer') if isinstance(state.execution.guidance, dict) else None
        preserved_binding = dict(state.execution.proposal_binding or {})
        entity_bindings = state.stage_context.get('entityBindings')
        actual_transitions = [
            {'row': row_index + 1, 'column': column_index + 1, 'from': before, 'to': after}
            for row_index, (before_row, after_row) in enumerate(zip(current_rows, proposal_validation.rows))
            for column_index, (before, after) in enumerate(zip(before_row, after_row))
            if before != after
        ]
        execution_brief = (
            preserved_binding.get('executionBrief')
            or ((pre_review_offer or {}).get('executionBrief') if isinstance(pre_review_offer, dict) else None)
            or ((state.source_binding or {}).get('executionBrief') if isinstance(state.source_binding, dict) else None)
            or (
                state.stage_context.get('authorizedExecutionBrief')
                if isinstance(state.stage_context.get('authorizedExecutionBrief'), dict)
                else None
            )
        )
        if not isinstance(execution_brief, dict) or not execution_brief.get('requiredTransitions'):
            strategies = (state.execution.revision_contract or {}).get('strategies') or []
            selected_idx = (state.execution.proposal_diagnostics or {}).get('selectedStrategyIndex')
            selected_strategy = (
                strategies[selected_idx - 1]
                if isinstance(selected_idx, int) and 1 <= selected_idx <= len(strategies)
                else (strategies[0] if strategies else {})
            )
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
            or ((state.execution.revision_contract or {}).get('revisionWorkflow') if isinstance(state.execution.revision_contract, dict) else None)
            or ((pre_review_offer or {}).get('revisionWorkflow') if isinstance(pre_review_offer, dict) else None)
            or (
                copy.deepcopy(state.stage_context.get('challengedRevisionWorkflow'))
                if isinstance(state.stage_context.get('challengedRevisionWorkflow'), dict)
                and state.stage_context.get('challengedRevisionWorkflow')
                else None
            )
            or ((state.source_binding or {}).get('revisionWorkflow') if isinstance(state.source_binding, dict) else None)
            or ((state.source_offer or {}).get('revisionWorkflow') if isinstance(state.source_offer, dict) else None)
            or {}
        )
        source_offer_turn_id = (
            payload.sourceTurnId
            if payload.action == 'execute_revision' and payload.sourceTurnId
            else (
                state.stage_context.get('challengedSourceOfferTurnId')
                if state.challenge_choice_resolution == 'ai'
                else None
            )
        )
        execution_revision_contract = dict(state.execution.revision_contract or {})
        if isinstance(revision_workflow, dict) and revision_workflow and not execution_revision_contract.get('revisionWorkflow'):
            execution_revision_contract['revisionWorkflow'] = dict(revision_workflow)
        preserved_binding = {
            **preserved_binding,
            'baseVersionId': payload.baseVersionId,
            'mapFingerprint': map_fingerprint(current_rows),
            'entityBindingFingerprint': (entity_bindings or {}).get('bindingFingerprint'),
            **({'executionBrief': execution_brief} if isinstance(execution_brief, dict) else {}),
            'revisionWorkflow': dict(revision_workflow) if isinstance(revision_workflow, dict) else {},
            **({'sourceOfferTurnId': source_offer_turn_id} if source_offer_turn_id else {}),
            'status': preserved_binding.get('status') or 'active',
        }
        state.execution = replace(
            state.execution,
            assistant_message=_verified_proposal_message(state.language),
            modification_summary=verified_summary,
            guidance=_proposal_review_guidance(state.execution.guidance, state.language),
            revision_contract=execution_revision_contract,
            proposal_binding=preserved_binding,
        )
    return proposal_validation, current_rows


def _persist_new_assistant_turn_records(
    database,
    req: _MessageRequestContext,
    state: _RoutedMessageState,
    session,
    current_rows: list[str] | None,
    proposal_validation,
) -> str:
    """Insert the assistant turn and all audit, proposal, challenge, memory, and recovery records."""
    payload = req.payload
    services = req.services
    execution = state.execution
    proposal_id = None
    assistant_turn_id = services.insert_turn(
        database,
        session,
        'assistant',
        execution.assistant_message,
        payload.baseVersionId,
        payload.idempotencyKey,
        execution,
    )
    _record_reply_outcome(
        database,
        req.session_id,
        payload.idempotencyKey,
        payload.baseVersionId,
        execution,
        'message',
        proposal_requested=(
            payload.requestProposal
            or state.revision_state == 'proposal_requested'
            or payload.action in {'execute_revision', 'alternative_revision'}
            or bool(state.challenge_choice_resolution)
            or state.proposal_recovery
            or bool((execution.guidance or {}).get('proposalOffer'))
        ),
        services=services,
    )
    if payload.action == 'challenge_revision':
        hypotheses = (execution.proposal_diagnostics or {}).get('challengeHypotheses')
        if isinstance(hypotheses, dict):
            challenge_id = hypotheses.get('challengeId')
            now = services.utc_now()
            database.execute(
                "\n                        INSERT INTO revision_challenges(\n"
                "                            challenge_id, session_id, base_version_id,\n"
                "                            source_proposal_turn_id, challenge_turn_id, status,\n"
                "                            current_reason_turn_id, created_at, updated_at\n"
                "                        ) VALUES (?, ?, ?, ?, ?, 'awaiting_reason', NULL, ?, ?)\n"
                "                        ON CONFLICT(challenge_id) DO UPDATE SET\n"
                "                            status = 'awaiting_reason',\n"
                "                            challenge_turn_id = excluded.challenge_turn_id,\n"
                "                            updated_at = excluded.updated_at\n                        ",
                (challenge_id, req.session_id, payload.baseVersionId, payload.sourceTurnId, assistant_turn_id, now, now),
            )
            services.record_event(
                database,
                req.session_id,
                'proposal_challenge_hypotheses_recorded',
                {
                    'messageKey': payload.idempotencyKey,
                    'challengeTurnId': assistant_turn_id,
                    'sourceTurnId': payload.sourceTurnId,
                    'baseVersionId': payload.baseVersionId,
                    'proposalSummary': (state.source_offer or {}).get('summary'),
                    'challengeId': challenge_id,
                    'hypotheses': hypotheses.get('items') or [],
                    'primary': hypotheses.get('primary'),
                    'secondary': hypotheses.get('secondary'),
                    'state': 'awaiting_reason',
                },
                now,
            )
        challenged_binding = dict(state.source_binding or {})
        if state.source_turn is not None and challenged_binding:
            challenged_binding['status'] = 'challenged'
            database.execute(
                'UPDATE conversation_turns SET proposal_binding_json = ? WHERE id = ?',
                (dump_json(challenged_binding), state.source_turn['id']),
            )
    if payload.action == 'continue_challenge':
        challenge_state = (execution.guidance or {}).get('challengeState') or {}
        challenge_status = str(challenge_state.get('status') or 'reason_review')
        challenge_id = challenge_state.get('challengeId')
        if challenge_id:
            database.execute(
                '\n                        UPDATE revision_challenges\n'
                '                        SET status = ?, updated_at = ?\n'
                '                        WHERE challenge_id = ? AND session_id = ?\n                        ',
                (challenge_status, services.utc_now(), challenge_id, req.session_id),
            )
    discovery_marker = (execution.guidance or {}).get('proposalDiscovery')
    if isinstance(discovery_marker, dict):
        services.record_event(
            database,
            req.session_id,
            'proposal_discovery_progress',
            {
                'stageId': payload.baseVersionId,
                'turnId': assistant_turn_id,
                'topicId': discovery_marker.get('topicId'),
                'status': discovery_marker.get('status'),
                'clarificationQuestionCount': discovery_marker.get('clarificationQuestionCount'),
                'optionPolicyVersion': (discovery_marker.get('optionSet') or {}).get('schemaVersion'),
                'optionCount': len((discovery_marker.get('optionSet') or {}).get('options') or []),
                'optionSelection': (discovery_marker.get('optionDecision') or {}).get('selection'),
                'clarificationQuestionKey': discovery_marker.get('clarificationQuestionKey'),
                'clarificationCountBefore': discovery_marker.get('clarificationCountBefore'),
                'clarificationCountAfter': discovery_marker.get('clarificationCountAfter'),
                'groundingSentencesDropped': (execution.proposal_diagnostics or {}).get('groundingSentencesDropped', 0),
                'clarificationRecoveryMode': (
                    discovery_marker.get('clarificationRecoveryMode')
                    or (execution.proposal_diagnostics or {}).get('clarificationRecoveryMode')
                ),
                'clarificationAuthor': (
                    discovery_marker.get('clarificationAuthor')
                    or (execution.proposal_diagnostics or {}).get('clarificationAuthor')
                ),
                'clarificationTargetDimension': (execution.proposal_diagnostics or {}).get('clarificationTargetDimension'),
                'clarificationQuestionValidated': (execution.proposal_diagnostics or {}).get('clarificationQuestionValidated'),
                'clarificationQuestionRepairAttempts': (execution.proposal_diagnostics or {}).get('clarificationQuestionRepairAttempts'),
                'clarificationFallbackReason': (execution.proposal_diagnostics or {}).get('clarificationFallbackReason'),
                'proposalTopicContinued': discovery_marker.get('proposalTopicContinued'),
                'routing': state.stage_context.get('revisionRouting'),
                'proposalState': state.stage_context.get('proposalState'),
                'bindingMode': state.stage_context.get('proposalBindingMode'),
                'enteredRevisionPlan': bool(execution.revision_plan),
                'failureClass': (execution.proposal_diagnostics or {}).get('failureClass') or (execution.proposal_diagnostics or {}).get('category'),
                'failureCode': (execution.proposal_diagnostics or {}).get('failureCode'),
                'failureEnvelope': (execution.proposal_diagnostics or {}).get('failureEnvelope'),
                'recoverySuggestions': (execution.proposal_diagnostics or {}).get('recoverySuggestions') or [],
                'contractPreflightFailures': (execution.proposal_diagnostics or {}).get('contractPreflightFailures'),
                'proposalAnswers': state.proposal_discovery.get('answers') or [],
                'proposalSupplements': state.proposal_discovery.get('supplements') or [],
                'providerStatus': (execution.proposal_diagnostics or {}).get('providerStatus'),
                'providerErrorType': (execution.proposal_diagnostics or {}).get('providerErrorType'),
                'providerErrorCode': (execution.proposal_diagnostics or {}).get('providerErrorCode'),
                'providerParam': (execution.proposal_diagnostics or {}).get('providerParam'),
                'providerMessage': (execution.proposal_diagnostics or {}).get('providerMessage'),
            },
            services.utc_now(),
        )
    if execution.proposal_binding:
        services.record_event(
            database,
            req.session_id,
            'proposal_binding_created',
            {
                'stageId': payload.baseVersionId,
                'sourceTurnId': assistant_turn_id,
                'mapFingerprint': execution.proposal_binding.get('mapFingerprint'),
                'status': execution.proposal_binding.get('status'),
                'transitions': [
                    {key: item.get(key) for key in ('row', 'column', 'from', 'to', 'anchorEntity')}
                    for item in (
                        execution.proposal_binding.get('executionBrief', {}).get('requiredTransitions', [])
                        if isinstance(execution.proposal_binding.get('executionBrief'), dict)
                        else []
                    )
                ],
                'transitionCount': len(
                    execution.proposal_binding.get('executionBrief', {}).get('requiredTransitions', [])
                    if isinstance(execution.proposal_binding.get('executionBrief'), dict)
                    else []
                ),
            },
            services.utc_now(),
        )
    if execution.proposal_diagnostics.get('source') == 'deterministic_contract':
        services.record_event(
            database,
            req.session_id,
            'revision_plan_repaired',
            {
                'stageId': payload.baseVersionId,
                'sourceTurnId': payload.sourceTurnId,
                'attempts': execution.attempts_used,
                'deterministicFallback': True,
                'reason': 'exact_transition_contract',
            },
            services.utc_now(),
        )
    if execution.revision_plan:
        revision_contract = execution.revision_contract or {}
        revision_workflow = revision_contract.get('revisionWorkflow') or {}
        if revision_workflow:
            semantic_results = ((execution.proposal_diagnostics or {}).get('mechanismEvidence') or {}).get('semanticConstraintResults') or []
            services.record_event(
                database,
                req.session_id,
                'revision_workflow_v2_evaluated',
                {
                    'baseVersionId': payload.baseVersionId,
                    'sourceTurnId': assistant_turn_id,
                    'messageKey': payload.idempotencyKey,
                    'workflow': revision_workflow,
                    'constraintResults': semantic_results,
                    'mode': revision_workflow.get('mode'),
                    'candidateAccepted': bool(
                        execution.proposed_rows is not None
                        or (execution.proposal_diagnostics or {}).get('candidateFrozen')
                    ),
                },
                services.utc_now(),
            )
        record_agent_handoff(
            database,
            req.session_id,
            'co_creation_chat',
            'co_creation_revision',
            'revision_plan',
            {
                'baseVersionId': payload.baseVersionId,
                'authorizedBrief': revision_contract.get('authorizedBrief'),
                'revisionPlan': execution.revision_plan,
                'executionContract': revision_contract,
                'attempts': execution.proposal_diagnostics.get('planAttempts'),
            },
            evidence=[{'type': 'explicit_revision_authorization', 'status': 'confirmed'}],
            status='confirmed',
            services=services,
        )
    if execution.revision_plan and execution.proposed_rows is not None:
        services.record_event(
            database,
            req.session_id,
            'proposal_search_completed',
            {
                'baseVersionId': payload.baseVersionId,
                'messageKey': payload.idempotencyKey,
                'sourceTurnId': payload.sourceTurnId,
                'revisionPlan': execution.revision_plan,
                'executionContract': execution.revision_contract,
                'operations': execution.revision_operations,
                'search': execution.proposal_diagnostics,
            },
            services.utc_now(),
        )
    if execution.proposed_rows is not None:
        proposal_id = uuid.uuid4().hex
        database.execute(
            "\n                    INSERT INTO change_proposals(\n"
            "                        id, session_id, base_version_id, proposed_rows_json,\n"
            "                        summary, diff_json, validation_json, status,\n"
            "                        assistant_turn_id, idempotency_key, created_at\n"
            "                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)\n                    ",
            (
                proposal_id,
                req.session_id,
                payload.baseVersionId,
                dump_json(list(proposal_validation.rows)),
                execution.modification_summary,
                dump_json(describe_diff(current_rows, proposal_validation.rows)),
                dump_json(proposal_validation.as_dict()),
                assistant_turn_id,
                payload.idempotencyKey,
                services.utc_now(),
            ),
        )
        if payload.action == 'execute_revision' and state.source_turn is not None:
            consumed_binding = dict(state.source_binding or {})
            if consumed_binding:
                consumed_binding['status'] = 'stale'
                database.execute(
                    'UPDATE conversation_turns SET proposal_binding_json = ? WHERE id = ?',
                    (dump_json(consumed_binding), state.source_turn['id']),
                )
        record_agent_handoff(
            database,
            req.session_id,
            'co_creation_revision',
            'deterministic_validator',
            'revision_operations',
            {
                'proposalId': proposal_id,
                'baseVersionId': payload.baseVersionId,
                'revisionPlan': execution.revision_plan,
                'executionContract': execution.revision_contract,
                'strategyIndex': execution.proposal_diagnostics.get('selectedStrategyIndex'),
                'operations': execution.revision_operations,
                'changedCellCount': execution.proposal_diagnostics.get('changedCellCount'),
                'candidateCount': execution.proposal_diagnostics.get('candidateCount'),
                'attempts': execution.proposal_diagnostics.get('modifierAttempts'),
            },
            evidence=[
                {'type': 'deterministic_solver', 'status': 'passed', 'validation': proposal_validation.as_dict()},
                {'type': 'verified_diff', 'diff': describe_diff(current_rows, proposal_validation.rows)},
            ],
            status='confirmed',
            services=services,
        )
    elif execution.revision_plan:
        diagnostics = execution.proposal_diagnostics or {}
        record_agent_handoff(
            database,
            req.session_id,
            'co_creation_revision',
            'deterministic_validator',
            'revision_operations',
            {
                'baseVersionId': payload.baseVersionId,
                'revisionPlan': execution.revision_plan,
                'executionContract': execution.revision_contract,
                'operations': execution.revision_operations,
                'retryAttempts': diagnostics.get('attempts') or diagnostics.get('modifierAttempts'),
                'failureReason': diagnostics.get('modifierFailure'),
            },
            evidence=[
                {
                    'type': 'deterministic_validation',
                    'status': 'rejected',
                    'reason': diagnostics.get('modifierFailure') or 'No executable candidate satisfied the contract.',
                }
            ],
            status='rejected',
            services=services,
        )
    record_intent_hypothesis(
        database,
        req.session_id,
        payload.baseVersionId,
        assistant_turn_id,
        state.design_context_guidance,
        'conversation_reply',
        proposal_id=proposal_id,
        services=services,
    )
    _record_disagreement_event(
        database,
        req.session_id,
        payload.baseVersionId,
        assistant_turn_id,
        execution.guidance,
        services=services,
    )
    if state.question_answer_review.get('results'):
        services.record_event(
            database,
            req.session_id,
            'question_answer_reviewed',
            {
                'versionId': payload.baseVersionId,
                'userTurnId': state.user_turn_id,
                'results': state.question_answer_review['results'],
                'model': KIMI_MODEL,
            },
            services.utc_now(),
        )
    _update_design_context_from_turn(
        database,
        req.session_id,
        payload.baseVersionId,
        req.content,
        assistant_turn_id,
        state.design_context_guidance,
        assistant_content=execution.assistant_message,
        allow_progress=state.allow_progress,
        answered_question_ids=state.question_answer_review.get('answeredQuestionIds'),
        answer_turn_id=state.user_turn_id,
        proposal_discovery=(execution.guidance or {}).get('proposalDiscovery'),
        services=services,
    )
    if state.state_transition_deferred:
        component_recovery.enqueue(
            database,
            req.session_id,
            payload.baseVersionId,
            state.user_turn_id,
            assistant_turn_id,
            payload.idempotencyKey,
            clock=services.clock,
            record_event_fn=services.record_event,
        )
    return assistant_turn_id


def _commit_assistant_turn_and_sync(req: _MessageRequestContext, state: _RoutedMessageState) -> dict:
    """Commit the assistant turn and associated records atomically, then sync to 8000."""
    services = req.services
    payload = req.payload
    trace_reply('commit', 'started')
    trace_proposal('commit', 'commit', 'started')
    with services.connect(immediate=True) as database:
        session, current = _validate_commit_cas_and_prepare_state(database, req, state)
        proposal_validation, current_rows = _persist_pending_proposal_if_needed(database, req, state, current)
        existing = database.execute(
            "\n            SELECT * FROM conversation_turns\n"
            "            WHERE session_id = ? AND request_id = ? AND role = 'assistant'\n            ",
            (req.session_id, payload.idempotencyKey),
        ).fetchone()
        assistant_turn_id = existing['id'] if existing is not None else None
        if existing is None:
            assistant_turn_id = _persist_new_assistant_turn_records(
                database,
                req,
                state,
                session,
                current_rows,
                proposal_validation,
            )
        trace_proposal('commit', 'commit', 'passed')
        trace_reply(
            'commit',
            'passed',
            assistantTurnId=assistant_turn_id,
            cardPresent=bool((state.execution.guidance or {}).get('intentHypothesis')),
            answeredCount=len(state.question_answer_review.get('answeredQuestionIds') or []),
        )
        session_payload = services.serialize_session(database, req.session_id)
    synchronize_turn_with_online_match(req.session_id, payload.idempotencyKey, services=services)
    return session_payload


def run_send_message_pipeline(
    session_id: str,
    payload: Any,
    content: str,
    request: Any,
    response: Any,
    access_cookie: str | None,
    *,
    services: CoCreationServices,
) -> dict:
    """Run the staged Policy 1/chat/card message pipeline with typed explicit phase state."""
    req = _MessageRequestContext(
        session_id=session_id,
        payload=payload,
        content=content,
        request=request,
        response=response,
        access_cookie=access_cookie,
        services=services,
        message_started_at=getattr(request.state, 'llm_started_at', services.monotonic()),
        llm_budget_seconds=services.settings.llm_internal_deadline_seconds,
        request_signature=message_request_signature(
            content,
            base_version_id=payload.baseVersionId,
            request_proposal=payload.requestProposal,
            action=payload.action,
            source_turn_id=payload.sourceTurnId,
        ),
    )
    trace_proposal('request_binding', 'binding', 'passed', bindingSignature=req.request_signature, sourceTurnId=payload.sourceTurnId)
    preflight = _preflight_message_request(req)
    if preflight.replayed_payload is not None:
        synchronize_turn_with_online_match(req.session_id, req.payload.idempotencyKey, services=req.services)
        return preflight.replayed_payload
    binary_response = _resolve_turn_understanding_or_binary(req, preflight)
    if binary_response is not None:
        return binary_response
    state = _commit_user_turn_and_route(req, preflight)
    if state.replayed_payload is not None:
        synchronize_turn_with_online_match(req.session_id, req.payload.idempotencyKey, services=req.services)
        return state.replayed_payload
    _prepare_question_and_challenge_context(req, state)
    _resolve_challenge_continuation(req, state)
    _prepare_revision_context_and_evidence(req, state)
    _generate_message_execution(req, state)
    _finalize_execution_guidance_and_intent(req, state)
    return _commit_assistant_turn_and_sync(req, state)
