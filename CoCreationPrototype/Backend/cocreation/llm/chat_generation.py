import asyncio
import copy
from contextvars import ContextVar
from difflib import SequenceMatcher
import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field, replace
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from cocreation.domain.proposal_search import (
    Focus,
    MetricGoal,
    ProposalSearchExhausted,
    RevisionPlan,
    parse_revision_plan,
    search_revision_plan,
    validate_revision_plan_against_map,
)
from cocreation.domain.level_validation import (
    TILE_LEGEND,
    build_map_facts,
    build_stage_snapshot,
    analyze_user_map_claims,
    minimum_pushes,
    summarize_verified_diff,
    validate_and_solve,
)
from cocreation.domain.design_context import validate_design_context_patch
from cocreation.persistence.repository import map_fingerprint
from cocreation.reliability.reply_reliability import (
    ComponentCheck,
    OptionalRepairBudget,
    parse_json_object,
    requirement_cache_key,
    CURRENT_REPLY_TRACE,
    REPLY_PIPELINE_VERSION,
    trace_reply,
    failure_reason as _diagnostic_failure_reason,
)
from cocreation.reliability.component_patches import (
    PatchRequest,
    PatchError,
    Target,
    patch_response_format,
    dependent_components,
    restore_operation_sources,
    PATCH_VERSION,
)
from cocreation.domain.conversation_evidence import CONTEXT_VERSION
from cocreation.domain.understanding_review import (
    review_schema,
    review_targets,
    merge_review,
    review_patch_request,
    REVIEW_VERSION,
)
import cocreation.domain.proposal_options as proposal_options
from cocreation.domain.proposal_understanding import (
    turn_schema,
    validate_topic,
    designer_sources,
    critical_review_reason,
    topic_signature,
    validate_plan_scope,
    engagement_schema,
    validate_engagement,
    MEANING_REVIEW_VERSION,
    understanding_patch_request,
    validate_option_plan_scope,
)
from cocreation.domain.proposal_diagnostics import (
    proposal_phase,
    trace_proposal,
    trace_repair,
    reason_code,
    safe_issues,
    CURRENT_PROPOSAL_DIAGNOSTIC,
    ProposalValidationError,
)
from cocreation.domain.design_requirements import (
    requirement_response_schema,
    validate_requirement_record,
    evaluate_requirements,
    preflight_requirements,
    goal_rank,
    tradeoff_message,
    protected_change_requested,
    validate_requirement_components,
    requirement_model_view,
    REQUIREMENT_SEMANTICS,
    REQUIREMENT_DEFINITIONS,
    INTERPRETATION_VERSION,
)
from cocreation.domain.revision_workflow import (
    SemanticConstraintError,
    build_revision_workflow,
    validate_semantic_constraints,
)

from .config_and_runtime import CHAT_MAX_ATTEMPTS, CHAT_MAX_TOKENS, CHAT_RESPONSE_HARD_LENGTH, CHAT_TIMEOUT_SECONDS, COORDINATE_LINK_LIMIT, EmptyModelResponse, GUIDANCE_MOVES, INTENT_CONFIDENCE_LEVELS, KIMI_MODEL, LLMExecutionResult, LLMServiceError, LowQualityModelResponse, MANUAL_EDIT_EFFECT_EVIDENCE_KINDS, MANUAL_EDIT_PAIR_MAX_COMPLETION_TOKENS, MIN_RETRY_BUDGET_SECONDS, ORDINARY_CHAT_ATTEMPT_TIMEOUTS, ORDINARY_CHAT_MAX_ATTEMPTS, PLAIN_CHAT_MAX_TOKENS, PRIMARY_ATTEMPT_TIMEOUT_SECONDS, PROPOSAL_INTERNAL_DEADLINE_SECONDS, PROPOSAL_MAX_TOKENS, ROUTE_REASONING_PASSAGE_LIMIT, UI_CUE_TYPES, _clean_list, _clean_optional_text, _clean_text, _content_block_sentences, _current_service, _dialogue_background_prompt, _empty_response_diagnostics, _is_human_edit_stage_opening, _is_length_failure, _is_map_grounding_failure, _is_stage_one, _latest_role_content, _latest_user_explicitly_agrees, _latest_user_text, _log_llm_event, _plain_fallback_allowed, _provider_error_fields, _questionless_body, _request_completion, _request_component_patch, _request_deadline, _response_diagnostics, _retry_budget_available, _safe_validation_reason, _strip_unreferenced_current_map_claims, _unified_model_attempts, _visible_reply_style, classify_exception
from .intent_and_review import _active_human_edit_disagreement, _assert_no_unsupported_manual_edit_intent, _components_quality_passed, _disagreement_from_warning, _extract_plain_intent_decision, _intent_body_detail_issue, _intent_body_presentation_issue, _intent_hypothesis_detail_issue, _intent_is_only_execution_authorization, _intent_semantic_binding_issue, _manual_edit_has_design_evidence, _manual_edit_review_evidence, _natural_intent_candidate, _normalize_intent_hypothesis, _repair_human_edit_engagement, _repair_reviewed_intent_presentation, _review_ordinary_intent_with_recovery, _strip_unverified_state_claims, _unclear_revision_reply, _user_reframes_difficulty_judgment, _validate_disagreement, _validate_intent_decision, _warning_text_is_evidence_grounded
from .prompts_and_messages import _human_edit_intent_discussion_focus, _is_human_edit_intent_question, build_chat_messages, build_plain_chat_messages
from .proposal_planning import _apply_deterministic_guidance_fallback, _apply_guidance_card_policy, _clarification_envelope_patch, _clarification_needs_semantic_review, _classify_revision_request, _deterministic_exact_revision, _distill_proposal_offer, _ensure_required_guidance_card, _exact_revision_clarification, _generate_verified_proposal_failure_analysis, _has_proposal_clarification, _is_proposal_conversation_branch, _latest_user_states_direction, _llm_failure_class, _multiple_proposal_fallback_reply, _parse_proposal_clarification_payload, _plain_guidance_move, _proposal_clarification_fallback_message, _proposal_clarification_fallback_question, _proposal_clarification_observability, _proposal_offer_binding_issue, _proposal_offer_has_exact_execution_plan, _repair_conceptual_proposal_binding, _response_contains_multiple_proposals, _review_proposal_clarification, _validate_execution_brief, _validate_revision_plan_payload, classify_guidance_request, proposal_offer_requires_execution_brief
from .snapshot_grounding import _GROUNDING_MEMO, _assert_visible_output_language, _chat_validation_mode, _deduplicate_assistant_body, _filter_coordinate_links, _guidance_reuses_visible_sentence, _guidance_text_matches, _has_historical_stage_reference, _isolate_optional_language_components, _join_english, _localized_change_labels, _manual_edit_text_is_contextual, _map_facts_for_prompt, _normalize_change_claims_for_proposal, _normalize_coordinate_links, _normalize_response_paragraphs, _normalize_single_level_language, _normalize_unsaved_change_claims, _proposal_clarification_route_evidence, _recover_coordinate_links, _remove_exact_guidance_from_body, _remove_extracted_warning_sentence, _remove_guidance_from_body, _render_content_blocks, _route_evidence_for_prompt, _sanitize_assessment_grounding, _sanitize_ordinary_grounding_metadata, _sanitize_visible_guidance, _sanitize_visible_model_text, _snapshot_entity_records, _stage_snapshot_for_prompt, _strip_invalid_grounding_sentences, _strip_invalid_stage_grounding_sentences, _validate_map_grounding_texts


def _validate_manual_edit_pair_payload(payload, rows, language, solver_metrics, stage_context, evidence, conflict_decision, request_id, attempts_used, model, latency_ms, _component_state=None):
    if not isinstance(payload, dict) or set(payload) != {'openingMessage', 'assessment', 'reviewMessage', 'conflict'}:
        raise ValueError('The manual-edit assessment pair has an invalid envelope.')
    conflict = payload.get('conflict')
    requires_conflict = (conflict_decision or {}).get('verdict') == 'conflict'
    if requires_conflict and conflict is None:
        raise ValueError('The frozen manual-edit conflict decision requires a discussion card.')
    if not requires_conflict and conflict is not None:
        raise ValueError('The frozen manual-edit review decision requires conflict to be null.')
    opening_payload = {'assistantMessage': payload.get('openingMessage'), 'guidance': {'move': 'observe_stage', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': [], 'coordinateLinks': []}, 'assessment': payload.get('assessment'), 'proposedRows': None, 'modificationSummary': ''}
    opening_message, assessment, _, _, opening_guidance = validate_chat_response(opening_payload, assessment_only=True, language=language, stage_context=stage_context, rows=rows)
    opening_guidance.update({'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': [], 'coordinateLinks': []})
    if _component_state is not None:
        _component_state.update({'openingMessage': payload['openingMessage'], 'assessment': payload['assessment']})
    review_message = _sanitize_visible_model_text(_normalize_single_level_language(_clean_text(payload.get('reviewMessage'), 'reviewMessage')), language)
    review_message = _normalize_response_paragraphs(review_message)
    if not review_message.strip():
        raise ValueError('The manual-edit review is empty.')
    if review_message.count('?') + review_message.count('？'):
        raise ValueError('The manual-edit review body must remain declarative.')
    _assert_no_unsupported_manual_edit_intent([review_message], evidence)
    _validate_map_grounding_texts([review_message], rows, entity_bindings=(stage_context or {}).get('entityBindings'))
    _assert_visible_output_language(language, review_message)
    if _component_state is not None:
        _component_state['reviewMessage'] = payload['reviewMessage']
    disagreement = None
    cited_evidence_ids = []
    if conflict is not None:
        expected_fields = {'userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion'}
        if not isinstance(conflict, dict) or set(conflict) != expected_fields:
            raise ValueError('The manual-edit conflict has an invalid envelope.')
        cited_evidence_ids = list(dict.fromkeys(list((conflict_decision or {}).get('confirmedDirectionIds') or []) + list((conflict_decision or {}).get('effectEvidenceIds') or [])))[:12]
        allowed_ids = {item['id'] for item in evidence}
        if any((not isinstance(item, str) or item not in allowed_ids for item in cited_evidence_ids)):
            raise ValueError('The frozen manual-edit conflict cites unknown evidence.')
        concrete_ids = {item['id'] for item in evidence if item.get('kind') in MANUAL_EDIT_EFFECT_EVIDENCE_KINDS}
        if not concrete_ids.intersection(cited_evidence_ids):
            raise ValueError('A manual-edit conflict requires concrete map, solver, or play evidence.')
        disagreement = _validate_disagreement({'status': 'active', 'subject': 'human_edit', 'userPosition': conflict.get('userPosition'), 'aiPosition': conflict.get('aiPosition'), 'coreDisagreement': conflict.get('coreDisagreement'), 'nextQuestion': conflict.get('nextQuestion'), 'resolution': None}, language)
        next_question = disagreement.get('nextQuestion') or ''
        if next_question.count('?') + next_question.count('？') != 1:
            raise ValueError('A manual-edit discussion must ask exactly one next question.')
    intent_scope_texts = [review_message]
    if disagreement:
        intent_scope_texts.extend((disagreement.get(field) for field in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion')))
    _assert_no_unsupported_manual_edit_intent(intent_scope_texts, evidence)
    review_guidance = {'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': disagreement, 'uiCues': [], 'coordinateLinks': [], 'discussionCardMode': 'disagreement_only', 'manualEditReview': {'evidenceIds': cited_evidence_ids, 'outcome': (conflict_decision or {}).get('verdict', 'no_conflict'), 'decisionSource': (conflict_decision or {}).get('decisionSource'), 'confirmedDirectionIds': (conflict_decision or {}).get('confirmedDirectionIds') or [], 'effectEvidenceIds': (conflict_decision or {}).get('effectEvidenceIds') or [], 'relation': (conflict_decision or {}).get('relation'), 'solverDelta': (conflict_decision or {}).get('solverDelta') or {}, 'comparisons': (conflict_decision or {}).get('comparisons') or []}}
    grounding_texts = [review_message]
    if disagreement:
        grounding_texts.extend((disagreement.get(field) for field in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion')))
    _validate_map_grounding_texts(grounding_texts, rows, entity_bindings=(stage_context or {}).get('entityBindings'))
    _assert_visible_output_language(language, review_message, guidance=review_guidance)
    secondary = LLMExecutionResult(assistant_message=review_message, attempts_used=attempts_used, request_id=f'{request_id}:manual-review', model=model, latency_ms=latency_ms, guidance=review_guidance)
    return LLMExecutionResult(assistant_message=opening_message, attempts_used=attempts_used, request_id=request_id, assessment=assessment, model=model, latency_ms=latency_ms, guidance=opening_guidance, secondary_execution=secondary)

def _generate_manual_edit_assessment_pair(conversation, rows, language, solver_metrics, play_summary, request_id, stage_context, _deadline=None):
    svc = _current_service()
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    evidence = _manual_edit_review_evidence(stage_context, solver_metrics, play_summary, language)
    started_at = svc.clock.monotonic()
    deadline = _deadline or _request_deadline(started_at)
    conflict_decision = svc.classify_manual_edit_conflict(evidence, language, f'{request_id}:manual-conflict', deadline)
    requires_conflict = conflict_decision.get('verdict') == 'conflict'
    response_language = 'Simplified Chinese' if language == 'zh-CN' else 'English'
    current_snapshot = _stage_snapshot_for_prompt(rows, stage_context)
    progress = (stage_context or {}).get('progressContext') or {}
    review_context = {'changeSummary': (stage_context or {}).get('changeSummary'), 'evidence': evidence, 'confirmedDesignDirectionAvailable': _manual_edit_has_design_evidence(evidence), 'conflictDecision': conflict_decision, 'stageLineage': progress.get('stageLineage', [])[-12:], 'rejectedDecisions': progress.get('rejectedDecisions', [])[-12:]}
    conflict_instruction = 'The conflict decision is frozen as conflict. conflict must be a non-null object. The server owns and binds every evidence ID; do not copy, print, or return evidence IDs. In your own natural language, remind the designer that this edit moves against a previously confirmed direction. You may positively describe verified solver or play improvements, but they do not cancel the disagreement. Warmly present two available paths: manually revise the Stage again, or continue discussing the changed design priority with me. Let nextQuestion naturally invite that choice or explanation. Do not use a stock sentence or a fixed response template.' if requires_conflict else 'The frozen decision does not establish a conflict. conflict must be null. Describe any verified alignment, trade-off, uncertainty, or improvement in your own natural language without inventing a disagreement.'
    messages = [{'role': 'system', 'content': 'You are the Kimi K2.6 Sokoban co-creation evaluator. A designer has saved a deterministically validated manual edit. Return one JSON object containing two distinct messages. Write every natural-language field in ' + response_language + ".\n\nopeningMessage is the first Stage-opening bubble. It must be declarative and card-free: acknowledge the saved solvable Stage once and highlight the key current layout feature and its supported play effect. A personal reaction is optional; do not cover every design dimension or repeat the comparison below. Do not ask a question or infer intent.\n\nreviewMessage is a second, always-visible declarative evaluator bubble. Compare the verified manual diff with active explicit/confirmed design directions and the bounded Stage lineage. Explain where the edit aligns, shifts, or creates a trade-off without repeating openingMessage. Do not invent a motive, proposal, map change, question, or warning card. Do not print evidence IDs or raw tile transitions in visible prose.\n\nThis project's tile legend is # wall, . floor, @ water, p player, s box, t target. Never reinterpret these glyphs using another Sokoban convention. The server's verified change summary and named beforeTile/afterTile fields are authoritative for changed tile types.\n\nIf confirmedDesignDirectionAvailable is false, explicitly say that there is no confirmed design direction available for comparison and discuss only verified map, solver, or play effects. In that case, never describe the edit as expressing, supporting, or conflicting with an implicit intention, preference, pursuit, hope, or goal. The saved edit itself is not proof of motive.\n\n" + _visible_reply_style('review') + '\n\n' + conflict_instruction + f"\n\nWhen conflict is non-null, fill one warm human_edit discussion: userPosition, aiPosition, a substantive coreDisagreement, and exactly one nextQuestion. This produces LET'S DISCUSS only; never produce WARNING. Historical Stage facts are comparison-only. Current-map claims must use only the Current Stage Snapshot. Avoid coordinates in reviewMessage and conflict fields. assessment.satisfactionQuestion must be null.\n\nCurrent Stage Snapshot (authoritative):\n{current_snapshot}\n\nManual-edit review context (server-owned):\n" + json.dumps(review_context, ensure_ascii=False, separators=(',', ':'))}]
    last_error = None
    locked_components = {}
    model = svc.config.model
    for attempt in range(1, 3):
        raw_content = None
        remaining = svc.remaining_until(deadline)
        if remaining <= 0:
            break
        timeout_seconds = min(60.0 if attempt == 1 else remaining, remaining)
        _log_llm_event('llm_attempt_started', requestId=request_id, task='manual_edit_assessment_pair', model=model, attempt=attempt, maxAttempts=2, timeoutSeconds=round(timeout_seconds, 3), responseMode='json_schema', frozenVerdict=conflict_decision.get('verdict'))
        try:
            response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, model, messages, MANUAL_EDIT_PAIR_MAX_COMPLETION_TOKENS, timeout_seconds, task='manual_edit_assessment_pair_conflict' if requires_conflict else 'manual_edit_assessment_pair_no_conflict'), timeout=timeout_seconds))
            choice = response.choices[0]
            if str(getattr(choice, 'finish_reason', '') or '') == 'length':
                raise ValueError('The manual-edit assessment pair reached its output limit.')
            raw_content = str(choice.message.content or '')
            payload = parse_json_object(raw_content)
            payload.update(locked_components)
            latency_ms = int((svc.clock.monotonic() - started_at) * 1000)
            result = _current_service().validate_manual_edit_pair(payload, rows, language, solver_metrics, stage_context, evidence, conflict_decision, request_id, attempt, model, latency_ms, _component_state=locked_components)
            _log_llm_event('llm_request_completed', requestId=request_id, task='manual_edit_assessment_pair', outcome='success', model=model, attemptsUsed=attempt, latencyMs=latency_ms, frozenVerdict=conflict_decision.get('verdict'), discussionCard=bool(result.secondary_execution.guidance.get('disagreement')), reliableBodyDelivered=True, componentChecks=[ComponentCheck(name, 'preserved').diagnostic() for name in locked_components])
            return result
        except asyncio.TimeoutError as exception:
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not complete the manual-edit review before the request deadline.', request_id, True, attempt, 504)
            _log_llm_event('llm_attempt_failed', requestId=request_id, task='manual_edit_assessment_pair', model=model, attempt=attempt, maxAttempts=2, code=last_error.code, retryable=True, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), validationReason='The Kimi request exceeded the manual-review time budget.', frozenVerdict=conflict_decision.get('verdict'))
        except LLMServiceError as exception:
            _log_llm_event('llm_attempt_failed', requestId=request_id, task='manual_edit_assessment_pair', model=model, attempt=attempt, maxAttempts=2, code=exception.code, retryable=exception.retryable, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), validationReason=_safe_validation_reason(exception), frozenVerdict=conflict_decision.get('verdict'))
            raise
        except Exception as exception:
            last_error = classify_exception(exception, request_id, attempt)
            last_error.retryable = True
            _log_llm_event('llm_attempt_failed', requestId=request_id, task='manual_edit_assessment_pair', model=model, attempt=attempt, maxAttempts=2, code=last_error.code, retryable=True, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), validationReason=_safe_validation_reason(exception), frozenVerdict=conflict_decision.get('verdict'))
            if raw_content and attempt < 2:
                messages.extend([{'role': 'assistant', 'content': raw_content}, {'role': 'user', 'content': 'Preserve these already validated components exactly: ' + json.dumps(locked_components, ensure_ascii=False) + f'. Repair only rejected components. Your review failed server validation: {exception}. Keep the frozen conflict decision unchanged and return a corrected complete JSON object.'}])
    if last_error is not None:
        raise last_error
    raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not complete the manual-edit review before the request deadline.', request_id, True, 0, 504)

def generate_stage_assessment(conversation, rows, language, solver_metrics, play_summary, request_id, stage_context=None, _deadline=None):
    svc = _current_service()
    if _is_human_edit_stage_opening(True, stage_context):
        return _generate_manual_edit_assessment_pair(conversation, rows, language, solver_metrics, play_summary, request_id, stage_context or {}, _deadline=_deadline)
    deadline = _deadline or _request_deadline()
    try:
        return generate_chat_reply(conversation, rows, request_id, language=language, solver_metrics=solver_metrics, play_summary=play_summary, assessment_only=True, stage_context=stage_context, _max_attempts=2, _deadline=deadline)
    except LLMServiceError as exception:
        if exception.code not in {'MODEL_EMPTY_RESPONSE', 'MODEL_RESPONSE_INVALID', 'UPSTREAM_TIMEOUT', 'UPSTREAM_CONNECTION_ERROR'}:
            raise
        can_attempt_plain_recovery = svc.remaining_until(deadline) >= svc.config.min_retry_budget_seconds
        _log_llm_event('llm_stage_opening_structured_failed' if can_attempt_plain_recovery else 'llm_stage_opening_fallback', requestId=request_id, fromCode=exception.code, responseMode='plain_text_recovery' if can_attempt_plain_recovery else 'server_snapshot', remainingSeconds=round(svc.remaining_until(deadline), 3), fallbackReason=exception.code)
        if can_attempt_plain_recovery:
            try:
                recovered = _current_service().generate_plain_chat(conversation=conversation, rows=rows, request_id=request_id, language=language, solver_metrics=solver_metrics, play_summary=play_summary, stage_context=stage_context, stage_opening=True, deadline=deadline, max_attempts=1)
                _log_llm_event('llm_stage_opening_recovered' if recovered.model != 'kimi-k2.6-safe-opening' else 'llm_stage_opening_recovery_exhausted', requestId=request_id, fromCode=exception.code, responseMode='plain_text' if recovered.model != 'kimi-k2.6-safe-opening' else 'server_snapshot', finalDisplayMode='plain_text_recovery' if recovered.model != 'kimi-k2.6-safe-opening' else 'server_snapshot', bodyPreserved=bool(recovered.assistant_message.strip()), droppedSentenceCount=0, remainingSeconds=round(svc.remaining_until(deadline), 3))
                return recovered
            except LLMServiceError as recovery_exception:
                _log_llm_event('llm_stage_opening_recovery_failed', requestId=request_id, fromCode=exception.code, recoveryCode=recovery_exception.code, remainingSeconds=round(svc.remaining_until(deadline), 3))
        _log_llm_event('llm_stage_opening_fallback', requestId=request_id, fromCode=exception.code, responseMode='server_snapshot', remainingSeconds=round(svc.remaining_until(deadline), 3), fallbackReason=exception.code)
        return _stage_opening_safe_execution(language=language, rows=rows, request_id=request_id, attempts_used=exception.attempts_used, started_at=deadline - svc.config.llm_internal_deadline_seconds, fallback_reason=exception.code, stage_context=stage_context, solver_metrics=solver_metrics)

def generate_chat_reply(conversation, rows, request_id, language='en', solver_metrics=None, play_summary=None, assessment_only=False, proposal_validator=None, stage_context=None, _max_attempts=CHAT_MAX_ATTEMPTS, _deadline=None):
    svc = _current_service()
    request_started_at = svc.clock.monotonic() if _deadline is None else None
    deadline = _deadline or _request_deadline(request_started_at)
    shared_reliability = stage_context.setdefault('_reliabilityState', {}) if isinstance(stage_context, dict) else {}
    effective_stage_context = dict(stage_context or {})
    effective_stage_context['_reliabilityState'] = shared_reliability
    explicit_action = effective_stage_context.get('explicitAction') or 'none'
    if explicit_action == 'execute_revision' and effective_stage_context.get('deterministicExactExecution') and isinstance(effective_stage_context.get('authorizedExecutionBrief'), dict):
        try:
            exact = _deterministic_exact_revision(rows, effective_stage_context['authorizedExecutionBrief'], request_id, language, proposal_validator, effective_stage_context.get('entityBindings'))
        except LLMServiceError:
            raise
        except Exception as exception:
            error = LLMServiceError('PROPOSAL_SEARCH_EXHAUSTED', 'The frozen execution contract could not produce a verified proposal.', request_id, False, 0, 422)
            error.proposal_diagnostics = {'source': 'deterministic_contract', 'category': 'exact_contract_execution'}
            raise error from exception
        if exact is not None:
            return exact
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    revision_state, revision_brief = _classify_revision_request(conversation, stage_context)
    effective_stage_context['responseLanguage'] = language
    if explicit_action == 'execute_revision':
        revision_state = 'authorized'
        source_offer = effective_stage_context.get('sourceProposalOffer') or {}
        revision_brief = ' '.join((str(source_offer.get(field) or '').strip() for field in ('summary', 'rationale'))).strip() or revision_brief
        if source_offer.get('executionBrief') and (not effective_stage_context.get('proposalBindingFrozen')):
            effective_stage_context['authorizedExecutionBrief'] = source_offer['executionBrief']
    elif explicit_action == 'challenge_revision':
        revision_state, revision_brief = ('not_request', None)
    elif explicit_action == 'alternative_revision':
        source_offer = effective_stage_context.get('sourceProposalOffer') or {}
        revision_state = 'proposal_requested'
        revision_brief = ' '.join((str(source_offer.get(field) or '').strip() for field in ('summary', 'rationale'))).strip() or revision_brief
    if explicit_action == 'alternative_revision':
        cited_offer = effective_stage_context.get('sourceProposalOffer') or {}
        cited_summary = str(cited_offer.get('summary') or '').strip()
        cited_rationale = str(cited_offer.get('rationale') or '').strip()
        effective_stage_context['alternativeRevisionBrief'] = f'Generate one materially different proposal for the same designer direction. The cited proposal was {cited_summary!r}: {cited_rationale!r}. Do not reuse its exact required transition set; choose a different local treatment that remains grounded in the current saved Stage.'
    if not assessment_only and revision_state != 'not_request':
        effective_stage_context['revisionRequestState'] = revision_state
    if revision_brief:
        effective_stage_context['authorizedRevisionBrief'] = revision_brief
    automatic_proposal = effective_stage_context.get('revisionRouting') in {'proposal', 'proposal_conservative'}
    proposal_request = not assessment_only and (explicit_action == 'execute_revision' or explicit_action == 'alternative_revision' or automatic_proposal or (not effective_stage_context.get('deferRevisionExecution') and (revision_state in {'authorized', 'authorized_relaxed'} or automatic_proposal)))
    if proposal_request and _deadline is None:
        deadline = _request_deadline(request_started_at, budget_seconds=svc.config.llm_internal_deadline_seconds if effective_stage_context.get('requirementPolicyVersion') == 1 else svc.config.proposal_internal_deadline_seconds)
    if revision_state == 'authorized_relaxed':
        effective_stage_context['revisionRelaxed'] = True
        effective_stage_context['relaxationOriginalBrief'] = str((((stage_context or {}).get('recentGuidance') or {}).get('relaxationOffer') or {}).get('originalBrief') or '')
    if automatic_proposal and (not proposal_request):
        raise LLMServiceError('PROPOSAL_ROUTING_INVARIANT', 'A ready automatic proposal cannot enter the plain-chat path.', request_id, False, 0, 500)
    direction_handoff = None
    if not assessment_only and (not proposal_request):
        discussion = _current_service().generate_plain_chat(conversation=conversation, rows=rows, request_id=request_id, language=language, solver_metrics=solver_metrics, play_summary=play_summary, stage_context=effective_stage_context, deadline=deadline)
        if (discussion.proposal_diagnostics or {}).get('optionOutcome') != 'ready':
            return discussion
        discovery = dict(effective_stage_context.get('proposalDiscovery') or {})
        previous_options = proposal_options.current_set(discovery)
        discovery['optionSet'] = discussion.proposal_diagnostics['optionSet']
        if not previous_options or (discovery.get('optionDecision') or {}).get('selection') == 'revise':
            discovery.pop('optionDecision', None)
        effective_stage_context.update({'proposalDiscovery': discovery, 'revisionRouting': 'proposal_conservative', 'proposalBindingMode': 'conservative'})
        effective_stage_context.pop('proposalClarification', None)
        direction_handoff = {'optionSet': discovery['optionSet'], 'optionDecision': discovery.get('optionDecision'), 'optionAutoPlan': True}
        proposal_request = True
    if proposal_request:
        reserve = 12.0 if effective_stage_context.get('requirementPolicyVersion') == 1 and svc.remaining_until(deadline) > 32 else 0.0
        try:
            planned = svc.generate_revision_search_proposal_sync(api_key=api_key, base_url=base_url, conversation=conversation, rows=rows, request_id=request_id, language=language, proposal_validator=proposal_validator, stage_context=effective_stage_context, baseline_metrics=solver_metrics, deadline=deadline - reserve)
            if direction_handoff:
                planned = replace(planned, proposal_diagnostics={**(planned.proposal_diagnostics or {}), **direction_handoff})
            return planned
        except LLMServiceError as error:
            recovery = _current_service().generate_verified_proposal_failure_analysis(api_key, base_url, effective_stage_context, rows, language, request_id, deadline, error)
            if recovery is None:
                raise
            if direction_handoff:
                recovery = replace(recovery, proposal_diagnostics={**(recovery.proposal_diagnostics or {}), **direction_handoff})
            return recovery
    messages = build_chat_messages(conversation, rows, language, solver_metrics, play_summary, assessment_only, effective_stage_context)
    models = _unified_model_attempts(_max_attempts)
    primary_model = models[0]
    task = 'stage_assessment' if assessment_only else 'map_proposal' if proposal_request else 'chat'
    max_tokens = PROPOSAL_MAX_TOKENS if proposal_request else CHAT_MAX_TOKENS
    started_at = svc.clock.monotonic()
    _log_llm_event('llm_request_started', requestId=request_id, task=task, primaryModel=primary_model, fallbackModel=None, timeoutSeconds=svc.config.backend_request_timeout_seconds, responseMode='json_object')
    try:
        return asyncio.run(asyncio.wait_for(_generate_with_model_fallback(api_key=api_key, base_url=base_url, models=models, messages=messages, rows=rows, max_tokens=max_tokens, request_id=request_id, task=task, language=language, assessment_only=assessment_only, proposal_validator=proposal_validator, stage_context=effective_stage_context, started_at=started_at, deadline=deadline, max_attempts=_max_attempts), timeout=min(svc.config.backend_request_timeout_seconds, svc.remaining_until(deadline))))
    except asyncio.TimeoutError as exception:
        elapsed_ms = int((svc.clock.monotonic() - started_at) * 1000)
        _log_llm_event('llm_request_completed', requestId=request_id, task=task, outcome='error', code='UPSTREAM_TIMEOUT', attemptsUsed=min(len(models), _max_attempts), latencyMs=elapsed_ms, responseMode='json_object')
        raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not complete the request before the 120 second limit.', request_id, True, min(len(models), _max_attempts), 504) from exception

def _generate_plain_chat_sync(*, conversation, rows, request_id, language, solver_metrics, play_summary, stage_context, stage_opening=False, deadline=None, max_attempts=None):
    svc = _current_service()
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    shared_reliability = stage_context.setdefault('_reliabilityState', {}) if isinstance(stage_context, dict) else {}
    effective_stage_context = dict(stage_context or {})
    effective_stage_context['_reliabilityState'] = shared_reliability
    if isinstance(effective_stage_context.get('proposalClarification'), dict):
        clarification = dict(effective_stage_context['proposalClarification'])
        clarification['routeEvidence'] = _route_evidence_for_prompt(_proposal_clarification_route_evidence(rows, solver_metrics or {}, effective_stage_context), language)
        effective_stage_context['proposalClarification'] = clarification
    guidance_mode = classify_guidance_request(conversation, effective_stage_context, stage_opening=stage_opening)
    task = 'stage_assessment_fallback' if stage_opening else 'chat'
    validation_mode = _chat_validation_mode(conversation, effective_stage_context, stage_opening=stage_opening, guidance_mode=guidance_mode)
    if max_attempts is None:
        effective_max_attempts = 1 if stage_opening else svc.config.ordinary_chat_max_attempts if validation_mode in {'ordinary_chat', 'route_discussion'} else svc.config.chat_max_attempts
    else:
        effective_max_attempts = max(1, int(max_attempts))
    models = _unified_model_attempts(effective_max_attempts)
    messages = build_plain_chat_messages(conversation, rows, language, solver_metrics, play_summary, effective_stage_context, stage_opening=stage_opening, validation_mode=validation_mode)
    historical_reference = _has_historical_stage_reference(_latest_user_text(conversation))
    started_at = svc.clock.monotonic()
    deadline = deadline or _request_deadline(started_at)
    _log_llm_event('llm_request_started', requestId=request_id, task=task, primaryModel=models[0], fallbackModel=None, timeoutSeconds=svc.config.plain_chat_timeout_seconds, responseMode='plain_text', guidanceMode=guidance_mode)
    attempt_state = {'count': 0}
    try:
        return asyncio.run(asyncio.wait_for(_generate_plain_with_model_fallback(api_key=api_key, base_url=base_url, models=models, messages=messages, request_id=request_id, task=task, language=language, solver_metrics=solver_metrics, rows=rows, play_summary=play_summary, stage_opening=stage_opening, stage_context=effective_stage_context, guidance_mode=guidance_mode, validation_mode=validation_mode, historical_reference=historical_reference, semantic_messages=conversation, started_at=started_at, deadline=deadline, attempt_state=attempt_state), timeout=min(svc.config.plain_chat_timeout_seconds, svc.remaining_until(deadline))))
    except asyncio.TimeoutError as exception:
        elapsed_ms = int((svc.clock.monotonic() - started_at) * 1000)
        _log_llm_event('llm_request_completed', requestId=request_id, task=task, outcome='error', code='UPSTREAM_TIMEOUT', attemptsUsed=attempt_state['count'], latencyMs=elapsed_ms, responseMode='plain_text')
        raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not complete the request before the 120 second limit.', request_id, True, attempt_state['count'], 504) from exception

async def _generate_plain_with_model_fallback(**kwargs):
    memo = {'passed': set(), 'hits': 0, 'checks': 0}
    token = _GROUNDING_MEMO.set(memo)
    try:
        return await _generate_plain_with_model_fallback_impl(**kwargs)
    finally:
        trace_reply('map_validation', 'completed', cacheHits=memo['hits'], mapChecks=memo['checks'])
        _GROUNDING_MEMO.reset(token)

async def _generate_plain_with_model_fallback_impl(*, api_key, base_url, models, messages, request_id, task, language, solver_metrics, rows, play_summary, stage_opening, stage_context, guidance_mode, validation_mode='ordinary_chat', historical_reference=False, semantic_messages=None, started_at, deadline=None, attempt_state=None):
    svc = _current_service()
    stage_context = stage_context if stage_context is not None else {}
    last_error = None
    validation_feedback = None
    deadline = deadline or _request_deadline(started_at)
    optional_budget = OptionalRepairBudget(deadline, clock=svc.clock)
    optional_budget.spent = float((stage_context or {}).get('_optionalRepairSpent') or 0.0)
    component_checks = []
    semantic_messages = semantic_messages or messages
    proposal_clarification = (stage_context or {}).get('proposalClarification') if guidance_mode == 'needs_clarification' else None
    clarification_active = bool(isinstance(proposal_clarification, dict) and proposal_clarification.get('questionKey'))
    clarification_body_candidate = ''
    clarification_repair_request = None
    clarification_semantic_review_failed = False
    total_grounding_dropped_count = 0
    intent_repair_map_facts = json.dumps(_stage_snapshot_for_prompt(rows, stage_context), ensure_ascii=False, separators=(',', ':'))
    max_attempts = len(models)
    ordinary_discussion = validation_mode in {'ordinary_chat', 'route_discussion'}
    human_edit_engagement_required = _active_human_edit_disagreement(stage_context)
    for attempt, model in enumerate(models[:max_attempts], start=1):
        remaining = svc.remaining_until(deadline)
        if remaining <= 0:
            raise asyncio.TimeoutError()
        if ordinary_discussion and attempt == 3 and (remaining < 20.0):
            break
        if attempt_state is not None:
            attempt_state['count'] = attempt
        if ordinary_discussion and attempt <= len(svc.config.ordinary_chat_attempt_timeouts):
            attempt_timeout = min(svc.config.ordinary_chat_attempt_timeouts[attempt - 1], remaining)
        else:
            attempt_timeout = min(svc.config.plain_primary_timeout_seconds if attempt == 1 else remaining, remaining)
        response_fields = _empty_response_diagnostics()
        grounding_dropped_count = 0
        clarification_fallback_used = False
        snapshot_fallback_used = False
        _log_llm_event('llm_attempt_started', requestId=request_id, task=task, model=model, attempt=attempt, maxAttempts=max_attempts, timeoutSeconds=round(attempt_timeout, 3), responseMode='plain_text')
        try:
            attempt_messages = _plain_messages_with_validation_feedback(messages, validation_feedback, validation_mode=validation_mode, rows=rows, stage_context=stage_context, coordinate_free_recovery=ordinary_discussion and (not clarification_active) and (attempt == max_attempts) and (not _active_human_edit_disagreement(stage_context)))
            if clarification_active and clarification_body_candidate:
                attempt_messages = [dict(message) for message in attempt_messages]
                attempt_messages[0]['content'] += '\nThe body is already verified and locked. Repair only the server-listed rejected components for this topic; change options only when options is a listed target. Do not invent user goals. Return body unchanged: ' + clarification_body_candidate
            if clarification_repair_request is not None:
                envelope = await _request_component_patch(api_key, base_url, clarification_repair_request, attempt_messages, request_id, deadline, 'proposal_clarification', PLAIN_CHAT_MAX_TOKENS, attempt_timeout)
                content = json.dumps(envelope, ensure_ascii=False)
            else:
                response = await asyncio.wait_for(_request_completion(api_key, base_url, model, attempt_messages, PLAIN_CHAT_MAX_TOKENS, attempt_timeout, structured=clarification_active, task='proposal_clarification' if clarification_active else 'stage_assessment_fallback' if stage_opening else 'plain_chat', response_format_override=proposal_options.clarification_response_format(stage_context.get('proposalDiscovery') or {}, stage_context.get('stageSnapshot') or {}) if clarification_active and (proposal_clarification or {}).get('optionPolicyVersion') == proposal_options.VERSION else None), timeout=attempt_timeout)
                choice = response.choices[0]
                response_fields = _response_diagnostics(response, choice)
                if str(getattr(choice, 'finish_reason', '') or '') == 'length':
                    raise ValueError('The model output reached its token limit.')
                content = str(choice.message.content or '')
                if not content.strip():
                    raise EmptyModelResponse('The model returned an empty response.')
                if len(content.strip()) > CHAT_RESPONSE_HARD_LENGTH:
                    raise ValueError('The model response exceeded the transport safety limit.')
            clarification_question = ''
            clarification_question_issue = ''
            clarification_question_repair_attempts = max(0, attempt - 1)
            clarification_body_dropped = 0
            clarification_author = None
            clarification_fallback_reason = None
            if clarification_active:
                if clarification_body_candidate:
                    try:
                        envelope = parse_json_object(content)
                        envelope['body'] = clarification_body_candidate
                        content = json.dumps(envelope, ensure_ascii=False)
                    except (TypeError, ValueError):
                        pass
                try:
                    parsed_body, clarification_question, clarification_question_issue, clarification_body_dropped = _parse_proposal_clarification_payload(content, language, stage_context)
                except ProposalValidationError as error:
                    if attempt < max_attempts:
                        clarification_repair_request = _clarification_envelope_patch(parse_json_object(content), error, stage_context)
                    raise
                if (proposal_clarification or {}).get('optionPolicyVersion') == proposal_options.VERSION or ((proposal_clarification or {}).get('semanticPolicyVersion') == 1 and _clarification_needs_semantic_review('' if clarification_body_candidate else parsed_body, clarification_question)):
                    clarification_semantic_review_failed = True
                    reviewed_body, reviewed_question_issue = await _current_service().review_proposal_clarification(api_key, base_url, '' if clarification_body_candidate else parsed_body, clarification_question, stage_context, request_id, deadline)
                    if not clarification_body_candidate:
                        parsed_body = reviewed_body
                    clarification_question_issue = reviewed_question_issue or clarification_question_issue
                    clarification_semantic_review_failed = False
                grounding_dropped_count += clarification_body_dropped
                total_grounding_dropped_count += clarification_body_dropped
                if parsed_body:
                    clarification_body_candidate = parsed_body
                if clarification_question_issue and attempt < max_attempts:
                    frozen_envelope = {'body': parsed_body, 'question': clarification_question}
                    targets = [Target('question', ('question',), (str,))]
                    if (proposal_clarification or {}).get('optionPolicyVersion') == proposal_options.VERSION:
                        frozen_envelope = {**parse_json_object(content), 'body': parsed_body}
                        targets += [Target('comparedOptions', ('comparedOptions',), (list,)), Target('discriminant', ('discriminant',), (str,))]
                        if stage_context.get('_optionReviewIssues'):
                            targets.append(Target('options', ('options',), (list, type(None))))
                    clarification_repair_request = PatchRequest('clarification_question', frozen_envelope, targets, {'snapshot': (stage_context or {}).get('stageSnapshot'), 'proposal_topic': (stage_context or {}).get('proposalDiscovery'), 'question_specification': proposal_clarification})
                    raise ValueError(clarification_question_issue)
                if clarification_question_issue:
                    if (proposal_clarification or {}).get('optionPolicyVersion') == proposal_options.VERSION:
                        raise LowQualityModelResponse('No verified option-bound question remains; retry the original answer.')
                    clarification_question = _proposal_clarification_fallback_question(language, stage_context)
                    clarification_fallback_reason = clarification_question_issue
                if clarification_repair_request is not None and (not clarification_question_issue):
                    trace_repair(clarification_repair_request.component, 'revalidated')
                content = parsed_body or clarification_body_candidate
                if (proposal_clarification or {}).get('optionPolicyVersion') == proposal_options.VERSION:
                    option_set = stage_context['_clarificationOptions']
                    content = '\n\n'.join((part for part in (content, proposal_options.render_options(option_set)) if part))
                if not content:
                    if clarification_question and (not clarification_question_issue):
                        content = clarification_question
                    else:
                        raise LowQualityModelResponse('No independently reliable Kimi clarification remains.')
                clarification_author = 'server_fallback' if clarification_fallback_used and clarification_question_issue else 'mixed' if clarification_fallback_used or clarification_question_issue else 'kimi'
            content = _sanitize_visible_model_text(_normalize_unsaved_change_claims(_normalize_single_level_language(content.strip()), language), language)
            if not stage_opening and (stage_context.get('conversationOnly') or (proposal_options.current_set(stage_context.get('proposalDiscovery')) and (not clarification_active))):
                content, permission_issue = await _current_service().review_proposal_clarification(api_key, base_url, content, '', stage_context, request_id, deadline)
                if permission_issue or not content.strip():
                    raise LowQualityModelResponse('No independently reliable permission-safe discussion remains.')
            discussion_focus = _extract_plain_discussion_focus(content, language, stage_opening, stage_context)
            visible_content, intent_hypothesis, proposal_offer, ui_cues = _extract_plain_guidance(content, language, stage_context, stage_opening, rows=rows, strict_metadata=False)
            latest_user_text = _latest_role_content(semantic_messages, 'user')
            try:
                intent_decision = _extract_plain_intent_decision(content)
            except ValueError:
                if not stage_opening and validation_mode in {'ordinary_chat', 'route_discussion'}:
                    intent_decision = None
                else:
                    raise
            if intent_decision is not None:
                try:
                    intent_decision = _validate_intent_decision(intent_decision, latest_user_text)
                except ValueError:
                    if not stage_opening and validation_mode in {'ordinary_chat', 'route_discussion'}:
                        intent_decision = None
                    else:
                        raise
            if clarification_active:
                visible_content = _questionless_body(visible_content) or (clarification_question if not clarification_question_issue else '')
                intent_hypothesis = None
                proposal_offer = None
                ui_cues = []
                discussion_focus = None
            proposal_text = ' '.join((part for part in (visible_content, (proposal_offer or {}).get('summary') if isinstance(proposal_offer, dict) else '', (proposal_offer or {}).get('rationale') if isinstance(proposal_offer, dict) else '') if str(part or '').strip()))
            if guidance_mode == 'revision_advice' and _response_contains_multiple_proposals(proposal_text):
                if attempt < max_attempts:
                    raise ValueError('A single response may contain only one primary proposal.')
                visible_content = _multiple_proposal_fallback_reply(language)
                intent_hypothesis = None
                proposal_offer = None
                ui_cues = []
            if stage_opening:
                visible_content, removed_grounding = _strip_invalid_stage_grounding_sentences(visible_content, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
                if removed_grounding:
                    grounding_dropped_count += len(removed_grounding)
                    total_grounding_dropped_count += len(removed_grounding)
                    _log_llm_event('llm_grounding_sentences_dropped', requestId=request_id, task=task, sentenceCount=len(removed_grounding), reasonCodes=[item['reason'].split(':', 1)[0] for item in removed_grounding])
                if not visible_content:
                    snapshot_fallback_used = True
                    visible_content = _current_service().server_snapshot_fallback_message(rows, language, stage_context=stage_context, stage_opening=True)
                discussion_focus = _extract_plain_discussion_focus(visible_content, language, stage_opening, stage_context)
            if rows and (not stage_opening):
                visible_content, removed_grounding = _strip_invalid_grounding_sentences(visible_content, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
                if removed_grounding:
                    grounding_dropped_count += len(removed_grounding)
                    total_grounding_dropped_count += len(removed_grounding)
                    _log_llm_event('llm_grounding_sentences_dropped', requestId=request_id, task=task, sentenceCount=len(removed_grounding), reasonCodes=[item['reason'].split(':', 1)[0] for item in removed_grounding])
                if not visible_content:
                    raise LowQualityModelResponse('No reliable model prose remained after map-grounding cleanup.')
                discussion_focus = _extract_plain_discussion_focus(visible_content, language, stage_opening, stage_context)
            if not ordinary_discussion:
                visible_content = _strip_unreferenced_current_map_claims(visible_content)
            if not visible_content:
                if ordinary_discussion or clarification_active:
                    raise LowQualityModelResponse('No reliable model prose remained after current-map cleanup.')
                clarification_fallback_used = clarification_active
                snapshot_fallback_used = stage_opening and (not clarification_active)
                visible_content = _proposal_clarification_fallback_message(language, stage_context) if clarification_active else _current_service().server_snapshot_fallback_message(rows, language, stage_context=stage_context, stage_opening=stage_opening)
            visible_content, route_recovery = _recover_overlong_content(visible_content, rows, language=language)
            route_recovery['droppedSentenceCount'] += grounding_dropped_count
            if route_recovery['changed']:
                discussion_focus = _extract_plain_discussion_focus(visible_content, language, stage_opening, stage_context)
            coordinate_links = _extract_plain_coordinate_links(content, rows)
            design_context_patch, design_context_patch_error = _extract_plain_design_context_patch(content)
            if clarification_active:
                coordinate_links = []
                design_context_patch = None
                design_context_patch_error = None
            disagreement = _extract_plain_disagreement(content, language, stage_context)
            state_transition_deferred = False
            human_edit_engagement = None
            if human_edit_engagement_required:
                disagreement = {**stage_context['activeDisagreement'], 'displayCard': False}
                signal = stage_context.get('_verifiedHumanEditEngagement') or (stage_context.get('turnUnderstanding') or {}).get('humanEditEngagement')
                try:
                    if stage_context.get('conversationOnly'):
                        raise ValueError('Understanding is unverified.')
                    human_edit_engagement = validate_engagement(signal, latest_user_text)
                    disagreement.update(status='acknowledged' if signal['decision'] == 'engage' else 'active', resolution=None)
                    component_checks.append(ComponentCheck('human_edit_engagement', 'passed'))
                except ValueError:
                    if stage_context.get('conversationOnly'):
                        state_transition_deferred = True
                        component_checks.append(ComponentCheck('human_edit_engagement', 'preserved', 'STATE_UNVERIFIED'))
                    else:
                        disagreement, human_edit_engagement, state_transition_deferred = await _repair_human_edit_engagement(content, latest_user_text, stage_context, disagreement, optional_budget, component_checks, api_key, base_url, request_id)
                        if human_edit_engagement is not None:
                            stage_context['_verifiedHumanEditEngagement'] = human_edit_engagement
                if state_transition_deferred:
                    visible_content = _strip_unverified_state_claims(visible_content)
                    intent_hypothesis, proposal_offer, ui_cues = (None, None, [])
            if human_edit_engagement_required:
                visible_content = _strip_unverified_state_claims(visible_content)
            proposal_binding_downgraded = False
            kimi_ordinary = bool(not stage_opening and (not clarification_active) and (not (stage_context or {}).get('conversationOnly')) and (not _is_proposal_conversation_branch(stage_context)) and (not isinstance(disagreement, dict)) and (proposal_offer is None))
            intent_hypothesis, proposal_offer, ui_cues, guidance_fallback_used = _current_service().apply_deterministic_guidance_fallback(semantic_messages, visible_content, language, stage_context, stage_opening, intent_hypothesis, proposal_offer, ui_cues, rows, play_summary, guidance_mode=guidance_mode, allow_required_fallback=attempt >= max_attempts, semantic_intent=kimi_ordinary)
            if stage_opening:
                intent_hypothesis = None
                proposal_offer = None
                design_context_patch = None
                design_context_patch_error = None
                if not _is_human_edit_stage_opening(True, stage_context):
                    ui_cues = []
            if proposal_offer is not None:
                proposal_offer = _distill_proposal_offer(proposal_offer, visible_content, _latest_role_content(semantic_messages[:-1], 'assistant'), language)
                if proposal_offer is not None and proposal_offer_requires_execution_brief(proposal_offer, visible_content, proposal_offer.get('summary'), proposal_offer.get('rationale'), _latest_role_content(semantic_messages, 'user')):
                    proposal_offer = None
                    proposal_binding_downgraded = True
                    guidance_fallback_used = True
            if guidance_mode == 'revision_advice' and proposal_offer is None and (attempt < max_attempts) and (not proposal_binding_downgraded):
                raise ValueError('REVISION_ADVICE requires a substantive proposalOffer card.')
            visible_content = _remove_extracted_warning_sentence(visible_content, ui_cues)
            visible_content_had_question = '?' in visible_content or '？' in visible_content
            body, question, pure_low_quality = _extract_plain_message_question(visible_content, language)
            if clarification_active:
                body = _questionless_body(body)
                question = clarification_question
                pure_low_quality = False
                if not body and clarification_question_issue:
                    raise LowQualityModelResponse('No reliable Kimi body remains for a replacement question.')
                body = '\n\n'.join((part for part in (body.strip(), question.strip()) if part))
                question = None
            if discussion_focus is not None:
                question = discussion_focus
            if pure_low_quality:
                raise LowQualityModelResponse('The model returned only a generic question without a useful response.')
            if question and _question_repeats_recent_judgment(question, semantic_messages):
                question = None
            if stage_opening and _is_stage_one(stage_context):
                body = _questionless_body(visible_content)
                question = None
                if not body:
                    raise LowQualityModelResponse('The Stage 1 opening contained only questions.')
                body = _ensure_stage_one_orientation(body, rows, language)
            elif stage_opening:
                body = _questionless_body(visible_content)
                question = None
                if not body:
                    raise LowQualityModelResponse('A Stage opening cannot contain only questions.')
            if not stage_opening and question is not None:
                question = _refine_discussion_focus(question, visible_content, language)
            if (stage_context or {}).get('discussionCardMode') == 'disagreement_only':
                if disagreement is None and (not (stage_context or {}).get('activeDisagreement')):
                    if question:
                        body = '\n\n'.join((part for part in (body.strip(), question.strip()) if part))
                    elif visible_content_had_question and body.strip() != visible_content.strip():
                        body = visible_content.strip()
                    question = None
                elif disagreement is not None and disagreement.get('status') == 'active':
                    question = None
            active_context_disagreement = (stage_context or {}).get('activeDisagreement')
            if disagreement is None and isinstance(active_context_disagreement, dict) and (active_context_disagreement.get('status') == 'active') and (proposal_offer is None):
                disagreement = active_context_disagreement
            if disagreement is None and (stage_context or {}).get('discussionCardMode') == 'disagreement_only' and ((stage_context or {}).get('revisionRequestState') in {'authorized', 'needs_direction'}):
                warning_text = next((cue.get('text') for cue in ui_cues if cue.get('type') == 'warning'), None)
                if warning_text:
                    disagreement = _disagreement_from_warning(warning_text, language, stage_context, user_position=_latest_role_content(semantic_messages, 'user'))
            if (stage_context or {}).get('revisionRequestState') == 'needs_direction' and (not (stage_context or {}).get('turnUnderstanding')):
                latest_user = _latest_role_content(semantic_messages, 'user')
                body = _unclear_revision_reply(language, latest_user)
                question = None
                intent_hypothesis = None
                proposal_offer = None
                ui_cues = []
                guidance_fallback_used = True
            if (stage_context or {}).get('turnUnderstanding') and (stage_context or {}).get('revisionRouting') not in {'proposal', 'proposal_conservative'}:
                proposal_offer = None
                if (stage_context or {}).get('revisionRouting') == 'protected_request':
                    ui_cues = []
            body = _normalize_response_paragraphs(body)
            if stage_context is not None and body:
                stage_context.setdefault('_reliabilityState', {})['modelGenerationSucceeded'] = True
            guidance = {'move': _plain_guidance_move(stage_opening, intent_hypothesis, proposal_offer), 'intentHypothesis': intent_hypothesis, 'intentConfidence': 'low' if (stage_context or {}).get('revisionRequestState') == 'needs_direction' else 'medium' if intent_hypothesis else None, 'followUpQuestion': question, 'proposalOffer': proposal_offer, 'disagreement': disagreement, 'uiCues': ui_cues[:2], 'coordinateLinks': coordinate_links}
            if human_edit_engagement is not None:
                guidance['_humanEditEngagement'] = human_edit_engagement
            if state_transition_deferred:
                guidance['_stateTransitionDeferred'] = True
                guidance.pop('designContextPatch', None)
                design_context_patch = None
            guidance = _sanitize_visible_guidance(guidance, language)
            if design_context_patch is not None:
                guidance['designContextPatch'] = design_context_patch
            if design_context_patch_error:
                guidance['designContextPatchError'] = design_context_patch_error
            if stage_opening:
                guidance = _sanitize_ordinary_grounding_metadata(guidance, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
                guidance['move'] = 'observe_stage'
                guidance['intentHypothesis'] = None
                guidance['intentConfidence'] = None
                guidance['proposalOffer'] = None
                guidance.pop('designContextPatch', None)
                guidance.pop('designContextPatchError', None)
            guidance = _apply_guidance_card_policy(guidance)
            guidance = _ensure_required_guidance_card(guidance, semantic_messages, language, rows, stage_opening, stage_context, visible_content=visible_content, guidance_mode=guidance_mode, semantic_intent=kimi_ordinary)
            body = _deduplicate_assistant_body(body)
            if guidance.get('intentHypothesis'):
                presentation_issue = _intent_body_presentation_issue(body, language, guidance['intentHypothesis'])
                if presentation_issue:
                    component_checks.append(ComponentCheck('body', 'preserved', 'PRESENTATION_QUALITY'))
            ordinary_branch = bool(not stage_opening and (not (stage_context or {}).get('conversationOnly')) and (not clarification_active) and (not _is_proposal_conversation_branch(stage_context)) and (not isinstance(guidance.get('disagreement'), dict)) and (guidance.get('proposalOffer') is None))
            if ordinary_branch:
                if not body and visible_content_had_question and (not pure_low_quality):
                    body = visible_content
                if question:
                    body = '\n\n'.join((part for part in (body, question) if part))
                    question = None
                guidance['followUpQuestion'] = None
                if not body:
                    raise LowQualityModelResponse('An ordinary reply has no reliable visible content.')
                draft_decision_missing = intent_decision is None
                if draft_decision_missing:
                    intent_decision = {'classification': 'none', 'claims': [], 'cardText': ''}
                stage_context.setdefault('_reliabilityState', {})['intentDraftAudit'] = copy.deepcopy(intent_decision) if not draft_decision_missing else {'classification': None, 'claims': [], 'cardText': ''}
                draft_classification = intent_decision['classification']
                guidance['intentHypothesis'] = intent_decision['cardText'] if draft_classification == 'candidate' else None
                guidance['intentConfidence'] = 'medium' if draft_classification == 'candidate' else None
                active_intents = [{'hypothesisId': item.get('id'), 'statement': item.get('statement'), 'semanticClaims': item.get('semanticClaims') or [], 'evidenceText': item.get('evidenceText') or ''} for item in ((stage_context or {}).get('designContext') or {}).get('intentHypotheses', []) if item.get('status') == 'confirmed']
                intent_review = await _current_service().review_ordinary_intent_with_recovery(optional_budget=optional_budget, component_checks=component_checks, api_key=api_key, base_url=base_url, user_text=latest_user_text, body=body, decision=intent_decision, active_inclinations=active_intents, language=language, request_id=request_id, deadline=deadline, discussion_background=_dialogue_background_prompt(stage_context), visible_questions=(stage_context or {}).get('ordinaryOpenQuestions'), component_state=stage_context.setdefault('_reliabilityState', {}))
                if stage_context.get('_reliabilityState', {}).get('questionReviewIssue'):
                    component_checks.append(ComponentCheck('question_review', 'omitted', 'QUESTION_RESULTS_INVALID'))
                if intent_review.get('issues'):
                    component_checks.append(ComponentCheck('intent_presentation', 'preserved', 'QUALITY_REVIEW_ISSUES'))
                core_fields = ('subjectType', 'attributeType', 'direction', 'degree', 'aspect', 'scopeType', 'scopeText', 'evidenceSpan')
                draft_claim_keys = [tuple((claim.get(key) for key in core_fields)) for claim in intent_decision.get('claims') or []]
                reviewed_claim_keys = [tuple((claim.get(key) for key in core_fields)) for claim in intent_review.get('claims') or []]
                classification_mismatch = bool(not draft_decision_missing and intent_review['classification'] != draft_classification)
                relation_unverified = intent_review['relation'] == 'unclear'
                if relation_unverified and intent_review['bodyValid']:
                    component_checks.append(ComponentCheck('intent_card', 'omitted', 'RELATION_UNCLEAR'))
                    intent_review = {**intent_review, 'presentationOmitted': True}
                if intent_review['classification'] != 'candidate' or relation_unverified:
                    intent_decision = {'classification': intent_review['classification'], 'claims': [], 'cardText': ''}
                    draft_classification = intent_decision['classification']
                    guidance['intentHypothesis'] = None
                    guidance['intentConfidence'] = None
                else:
                    claims_changed = bool(not draft_decision_missing and draft_classification == 'candidate' and (draft_claim_keys != reviewed_claim_keys))
                    selected_card = intent_review['cardText'] if draft_decision_missing or classification_mismatch or claims_changed or (not intent_review['cardTextValid']) else intent_decision['cardText']
                    trace_reply('card_selection', 'passed', source='reviewer' if selected_card == intent_review['cardText'] else 'draft', cardPresent=bool(selected_card))
                    pre_repair_body_issue = _intent_body_detail_issue(body, language, selected_card)
                    if pre_repair_body_issue in {'intent body does not distinguish two design consequences', 'intent body does not preserve the unresolved design boundary', 'Chinese intent body is too brief', 'English intent body is too brief', 'intent body contains an option or fragment list', 'intent body repeats the intent card'}:
                        component_checks.append(ComponentCheck('body', 'preserved', 'PRESENTATION_QUALITY'))
                        pre_repair_body_issue = None
                    repair_body = bool(not intent_review['bodyValid'] or pre_repair_body_issue)
                    repair_card = bool(_intent_hypothesis_detail_issue(selected_card, language, semantic_reviewed=True))
                    try:
                        _assert_visible_output_language(language, selected_card)
                        _validate_map_grounding_texts([selected_card], rows, entity_bindings=(stage_context or {}).get('entityBindings'))
                        if not repair_card:
                            state = stage_context.setdefault('_reliabilityState', {})
                            state['lockedIntentReview'] = {**copy.deepcopy(intent_review), 'cardText': selected_card, 'cardTextValid': True}
                    except ValueError:
                        repair_card = True
                    if repair_body or repair_card:
                        if repair_body:
                            stage_context.setdefault('_reliabilityState', {})['bodyReviewRequired'] = True
                            trace_reply('body_repair', 'started', repairAttempts=1)
                        body, selected_card = await _repair_reviewed_intent_presentation(api_key=api_key, base_url=base_url, user_text=latest_user_text, body=body, card_text=selected_card, claims=intent_review['claims'], issues=(intent_review.get('issues') or []) + ([pre_repair_body_issue] if pre_repair_body_issue else []), repair_body=repair_body, repair_card=repair_card, map_facts=intent_repair_map_facts, language=language, request_id=request_id, deadline=deadline, optional_budget=optional_budget, card_required=intent_review['relation'] == 'conflict', component_checks=component_checks, rows=rows, entity_bindings=(stage_context or {}).get('entityBindings'), discussion_background=_dialogue_background_prompt(stage_context))
                        if repair_body and rows:
                            body, removed_repair_grounding = _strip_invalid_grounding_sentences(body, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
                            if not body:
                                raise LowQualityModelResponse('No reliable repaired intent prose remained after map-grounding cleanup.')
                        post_repair_body_issue = _intent_body_detail_issue(body, language, selected_card)
                        if post_repair_body_issue not in {None, 'intent body does not distinguish two design consequences', 'intent body does not preserve the unresolved design boundary', 'Chinese intent body is too brief', 'English intent body is too brief', 'intent body contains an option or fragment list', 'intent body repeats the intent card'}:
                            body, selected_card = await svc.repair_intent_components_async(api_key=api_key, base_url=base_url, user_text=latest_user_text, body=body, card_text=selected_card, claims=intent_review['claims'], issues=[post_repair_body_issue], repair_body=True, repair_card=False, map_facts=intent_repair_map_facts, language=language, request_id=request_id, deadline=deadline)
                            if rows:
                                body, removed_repair_grounding = _strip_invalid_grounding_sentences(body, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
                            if not body:
                                raise LowQualityModelResponse('No reliable second-pass intent prose remained after map-grounding cleanup.')
                    if repair_body:
                        stage_context['_reliabilityState']['bodyReviewRequired'] = False
                        trace_reply('body_repair', 'passed')
                    if relation_unverified:
                        selected_card = ''
                        intent_review = {**intent_review, 'presentationOmitted': True}
                    if selected_card:
                        intent_decision = {'classification': 'candidate', 'claims': intent_review['claims'], 'cardText': selected_card}
                        draft_classification = 'candidate'
                        guidance['intentHypothesis'] = selected_card
                        guidance['intentConfidence'] = 'medium'
                    else:
                        intent_decision = {'classification': 'candidate', 'claims': intent_review['claims'], 'cardText': ''}
                        intent_review = {**intent_review, 'presentationOmitted': True}
                        guidance['intentHypothesis'] = None
                        guidance['intentConfidence'] = None
                reviewed_claims = []
                for claim in intent_review.get('claims') or []:
                    reviewed_claims.append({**claim, 'subject': claim['subjectType'], 'attribute': claim['attributeType'], 'scope': claim['scopeText'] or 'stage', 'semanticSource': 'kimi_reviewed', 'reviewVersion': intent_review['reviewVersion'], 'sourceUserTurnId': None, 'confidence': 1.0})
                guidance['_intentDecision'] = intent_decision
                guidance['_intentSemanticClaims'] = reviewed_claims
                guidance['_intentCandidateReview'] = intent_review
            elif clarification_active or _is_proposal_conversation_branch(stage_context) or guidance.get('proposalOffer') is not None or isinstance(guidance.get('disagreement'), dict):
                guidance['intentHypothesis'] = None
                guidance['intentConfidence'] = None
            if guidance.get('intentHypothesis'):
                intent_issue = _intent_hypothesis_detail_issue(guidance['intentHypothesis'], language, semantic_reviewed=ordinary_branch)
                semantic_issue = None if ordinary_branch else _intent_semantic_binding_issue(guidance['intentHypothesis'], latest_user_text, language)
                body_issue = _intent_body_detail_issue(body, language, guidance['intentHypothesis'])
                if ordinary_branch and body_issue in {'intent body does not distinguish two design consequences', 'intent body does not preserve the unresolved design boundary', 'Chinese intent body is too brief', 'English intent body is too brief', 'intent body contains an option or fragment list', 'intent body repeats the intent card'}:
                    body_issue = None
                if (intent_issue or semantic_issue or body_issue) and attempt < max_attempts:
                    raise ValueError('Detailed TENTATIVE INTENT presentation required: ' + '; '.join((issue for issue in (intent_issue, semantic_issue, body_issue) if issue)))
                if (intent_issue or semantic_issue) and (not ordinary_branch):
                    guidance['intentHypothesis'] = _natural_intent_candidate(latest_user_text, language, _latest_user_explicitly_agrees(latest_user_text), difficulty_reframe=_user_reframes_difficulty_judgment(semantic_messages))
                    guidance_fallback_used = True
                if body_issue:
                    raise LowQualityModelResponse('The intent response lacks distinct, complete design analysis: ' + body_issue)
            if guidance_mode in {'revision_advice', 'needs_clarification'} and (not guidance.get('proposalOffer')) and (not re.search('[?？]', body)):
                body = '\n\n'.join((part for part in (body, ('你希望调整这一部分的哪种观感或游玩效果？' if language == 'zh-CN' else 'What visual or play effect would you like this part to have?') if (stage_context or {}).get('turnUnderstanding') else _exact_revision_clarification(language)) if str(part or '').strip()))
            body = _remove_guidance_from_body(body, guidance.get('followUpQuestion'))
            original_coordinate_link_count = len(guidance.get('coordinateLinks') or [])
            guidance['coordinateLinks'] = _filter_coordinate_links(guidance.get('coordinateLinks'), body, rows, (stage_context or {}).get('entityBindings'))
            guidance['coordinateLinks'] = _recover_coordinate_links(body, rows, guidance['coordinateLinks'], (stage_context or {}).get('entityBindings'))
            route_recovery['coordinateLinksDropped'] = original_coordinate_link_count > len(guidance['coordinateLinks'])
            proposal_binding_issue = _proposal_offer_binding_issue(guidance.get('proposalOffer'), body, semantic_messages, language)
            binding_is_required = guidance_mode == 'revision_advice'
            if proposal_binding_issue and binding_is_required:
                if attempt < max_attempts:
                    raise ValueError(f'proposalOffer binding failed: {proposal_binding_issue}')
                repaired = _repair_conceptual_proposal_binding(semantic_messages, body, guidance.get('proposalOffer'), visible_content, rows, language, stage_context)
                if repaired['proposalOffer'] is not None:
                    body = repaired['body']
                    guidance['move'] = 'offer_revision'
                    guidance['intentHypothesis'] = None
                    guidance['intentConfidence'] = None
                    guidance['followUpQuestion'] = None
                    guidance['proposalOffer'] = repaired['proposalOffer']
                    guidance['uiCues'] = repaired['uiCues']
                else:
                    body = repaired['body']
                    guidance['move'] = 'clarify_intent'
                    guidance['intentHypothesis'] = None
                    guidance['intentConfidence'] = None
                    guidance['proposalOffer'] = None
                    guidance['followUpQuestion'] = repaired['followUpQuestion']
                    guidance['uiCues'] = repaired['uiCues']
                guidance_fallback_used = True
            if not stage_opening:
                _validate_map_grounding_texts([body], rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
                guidance = _sanitize_ordinary_grounding_metadata(guidance, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
            else:
                grounding_texts = [body, guidance.get('followUpQuestion'), guidance.get('intentHypothesis')]
                if guidance.get('proposalOffer'):
                    grounding_texts.extend(guidance['proposalOffer'].values())
                grounding_texts.extend((cue.get('text') for cue in guidance.get('uiCues') or []))
                _validate_map_grounding_texts(grounding_texts, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
            evidence_signature = (stage_context or {}).get('guidanceEvidenceSignature')
            if human_edit_engagement_required:
                guidance['disagreement'] = disagreement
            if (stage_context or {}).get('conversationOnly'):
                body = _strip_unverified_state_claims(body)
                guidance.update({'intentHypothesis': None, 'intentConfidence': None, 'proposalOffer': None, 'uiCues': [], 'followUpQuestion': None})
                for field in ('designContextPatch', '_intentDecision', '_intentSemanticClaims', '_intentCandidateReview', '_humanEditEngagement'):
                    guidance.pop(field, None)
                active = (stage_context or {}).get('activeDisagreement')
                guidance['disagreement'] = {**active, 'displayCard': False} if active else None
                guidance['_stateTransitionDeferred'] = True
            if not body.strip():
                raise LowQualityModelResponse('No reliable visible answer remains.')
            if evidence_signature and guidance['uiCues']:
                guidance['evidenceSignature'] = evidence_signature
            assessment = _build_minimal_stage_assessment(body, question, language, solver_metrics) if stage_opening else {}
            latency_ms = int((svc.clock.monotonic() - started_at) * 1000)
            opening_body = body
            guidance = _isolate_optional_language_components(guidance, language, required_question=clarification_active or bool(guidance.get('disagreement')))
            if stage_opening and _is_human_edit_stage_opening(True, stage_context):
                opening_body = _compact_human_edit_opening_inventory(body, language)
            assistant_message = _compose_assistant_message(_format_stage_opening_paragraphs(opening_body) if stage_opening else body, guidance, language, stage_opening, stage_context)
            _assert_visible_output_language(language, assistant_message, guidance=guidance, assessment=assessment)
            if ordinary_discussion:
                _validate_map_grounding_texts([assistant_message], rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
            state = (stage_context or {}).get('_reliabilityState') or {}
            if state.get('intentDraftAudit') is not None:
                guidance['_intentDraftAudit'] = copy.deepcopy(state['intentDraftAudit'])
            if state.get('intentReviewAudit') is not None:
                guidance['_intentReviewAudit'] = copy.deepcopy(state['intentReviewAudit'])
            trace_reply('final_projection', 'passed', cardPresent=bool(guidance.get('intentHypothesis')))
            visible_body_source = 'kimi_salvaged' if grounding_dropped_count else 'kimi_corrective' if ordinary_discussion and attempt > 1 else 'kimi'
            result = LLMExecutionResult(assistant_message=assistant_message, assessment=assessment, proposed_rows=None, modification_summary='', attempts_used=attempt, request_id=request_id, model='kimi-k2.6-safe-opening' if snapshot_fallback_used else model, latency_ms=latency_ms, guidance=guidance, proposal_diagnostics={'groundingSentencesDropped': total_grounding_dropped_count, 'clarificationRecoveryMode': 'server_fallback' if clarification_fallback_used and clarification_question_issue else 'question_replaced' if clarification_question_issue else 'body_recovered' if clarification_fallback_used else 'kimi_complete', 'clarificationAuthor': 'server_fallback' if clarification_fallback_used and clarification_question_issue else 'mixed' if clarification_fallback_used or clarification_question_issue else 'kimi', 'clarificationTargetDimension': (proposal_clarification or {}).get('questionKey'), 'clarificationQuestion': clarification_question, **({'optionSet': stage_context.get('_clarificationOptions'), 'optionQuestion': stage_context.get('_clarificationOptionQuestion'), 'optionOutcome': 'clarifying' if len(stage_context['_clarificationOptions']['options']) > 1 else 'ready' if stage_context['_clarificationOptions']['options'] else 'revision_needed'} if (proposal_clarification or {}).get('optionPolicyVersion') == proposal_options.VERSION else {}), 'clarificationQuestionValidated': not bool(clarification_question_issue), 'clarificationQuestionRepairAttempts': clarification_question_repair_attempts, 'clarificationFallbackReason': clarification_fallback_reason, **_proposal_clarification_observability(content, clarification_question, stage_context)} if clarification_active else {'visibleBodySource': visible_body_source, 'groundingSentencesDropped': total_grounding_dropped_count, 'intentReviewVerified': bool(state.get('intentReviewAudit') and state['intentReviewAudit'].get('relation') != 'unclear'), 'questionReviewVerified': 'questionAnswerReview' in state, 'componentChecks': [check.diagnostic() for check in component_checks], 'fullQualityPassed': _components_quality_passed(component_checks) and (not total_grounding_dropped_count)} if ordinary_discussion else {})
            _log_llm_event('llm_request_completed', requestId=request_id, task=task, outcome='success', modelGenerationSucceeded=True, reliableBodyDelivered=True, fullQualityPassed=_components_quality_passed(component_checks) and (not total_grounding_dropped_count), componentChecks=[check.diagnostic() for check in component_checks], model=model, attemptsUsed=attempt, latencyMs=latency_ms, responseMode='plain_text', guidanceMode=guidance_mode, guidanceFallbackUsed=guidance_fallback_used, visibleBodySource=visible_body_source, intentCard=bool(guidance.get('intentHypothesis')), questionCard=bool(guidance.get('followUpQuestion')), proposalCard=bool(guidance.get('proposalOffer')), warningCard=any((cue.get('type') in {'warning', 'tradeoff'} for cue in guidance.get('uiCues', []))), manualEditCard=any((cue.get('type') == 'manual_edit' for cue in guidance.get('uiCues', []))), failureClass=None, salvageAction=route_recovery['salvageAction'], routeSentenceCount=route_recovery['routeSentenceCount'], routeCoordinateCount=route_recovery['routeCoordinateCount'], droppedSentenceCount=total_grounding_dropped_count, coordinateLinksDropped=route_recovery['coordinateLinksDropped'], clarificationAuthor='server_fallback' if clarification_active and clarification_fallback_used and clarification_question_issue else 'mixed' if clarification_active and (clarification_fallback_used or clarification_question_issue) else 'kimi' if clarification_active else None, clarificationTargetDimension=(proposal_clarification or {}).get('questionKey') if clarification_active else None, clarificationQuestionValidated=not bool(clarification_question_issue) if clarification_active else None, clarificationQuestionRepairAttempts=clarification_question_repair_attempts if clarification_active else None, clarificationFallbackReason=clarification_fallback_reason if clarification_active else None, **_proposal_clarification_observability(content, clarification_question, stage_context) if clarification_active else {}, remainingSeconds=round(svc.remaining_until(deadline), 3), **response_fields)
            return result
        except asyncio.TimeoutError:
            failure_reason = None
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not respond before the attempt timeout.', request_id, True, attempt, 504)
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            trace_reply('body_validation', 'rejected', _diagnostic_failure_reason(exception), attempt=attempt)
            last_error = classify_exception(exception, request_id, attempt)
            if last_error.code == 'MODEL_RESPONSE_INVALID' and failure_reason:
                validation_feedback = failure_reason
        failure_fields = {'requestId': request_id, 'task': task, 'model': model, 'attempt': attempt, 'code': last_error.code, 'retryable': last_error.retryable, 'latencyMs': int((svc.clock.monotonic() - started_at) * 1000), 'responseMode': 'plain_text', **response_fields, **_provider_error_fields(last_error)}
        if failure_reason:
            failure_fields['validationReason'] = failure_reason
        failure_fields['failureClass'] = _llm_failure_class(last_error, validation_feedback)
        failure_fields['remainingSeconds'] = round(svc.remaining_until(deadline), 3)
        failure_fields['salvageAction'] = 'pending_retry' if ordinary_discussion else 'pending_retry_or_fallback'
        _log_llm_event('llm_attempt_failed', **failure_fields)
        last_error.attempts_used = attempt
        last_error.details = {'maximumAttempts': max_attempts, **getattr(last_error, 'details', {})}
        if getattr(last_error, 'details', {}).get('component') == 'clarification_review':
            break
        if attempt >= max_attempts or not _plain_fallback_allowed(last_error):
            break
        if not _retry_budget_available(deadline, request_id=request_id, task=task, attempt=attempt, max_attempts=max_attempts, response_mode='plain_text', fallback_reason=last_error.code):
            break
    if last_error is not None:
        _log_llm_event('llm_request_completed', requestId=request_id, task=task, outcome='error', code=last_error.code, attemptsUsed=last_error.attempts_used, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), responseMode='plain_text', failureClass=_llm_failure_class(last_error, validation_feedback), salvageAction='return_error' if ordinary_discussion or human_edit_engagement_required else 'pending_fallback', routeSentenceCount=0, routeCoordinateCount=0, droppedSentenceCount=total_grounding_dropped_count, coordinateLinksDropped=False, remainingSeconds=round(svc.remaining_until(deadline), 3), **_provider_error_fields(last_error))
        if stage_opening:
            return _stage_opening_safe_execution(language=language, rows=rows, request_id=request_id, attempts_used=last_error.attempts_used, started_at=started_at, fallback_reason=last_error.code, stage_context=stage_context, solver_metrics=solver_metrics)
        if ordinary_discussion or clarification_active or human_edit_engagement_required or clarification_semantic_review_failed:
            raise last_error
        if _is_length_failure(last_error, validation_feedback):
            fallback_message = _proposal_clarification_fallback_message(language, stage_context) if clarification_active else _safe_incomplete_chat_reply(language, stage_opening=stage_opening, rows=rows)
            return LLMExecutionResult(assistant_message=fallback_message, assessment={}, proposed_rows=None, modification_summary='', attempts_used=last_error.attempts_used, request_id=request_id, model=svc.config.model, latency_ms=int((svc.clock.monotonic() - started_at) * 1000), guidance={'move': 'observe_stage' if stage_opening else 'clarify_intent', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': [], 'coordinateLinks': []}, proposal_diagnostics={'groundingSentencesDropped': 0, 'clarificationRecoveryMode': 'deterministic_clarification'} if clarification_active else {})
        if rows is not None and _is_map_grounding_failure(last_error.safe_message, validation_feedback):
            clarification = _current_service().safe_grounding_chat_reply(language, rows=rows, stage_context=stage_context)
            return LLMExecutionResult(assistant_message=clarification, assessment={}, proposed_rows=None, modification_summary='', attempts_used=last_error.attempts_used, request_id=request_id, model='grounding-safe-chat-fallback', latency_ms=int((svc.clock.monotonic() - started_at) * 1000), guidance={'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': [], 'coordinateLinks': []}, proposal_diagnostics={'groundingSentencesDropped': 0, 'clarificationRecoveryMode': 'deterministic_clarification'} if _has_proposal_clarification(stage_context) else {})
        raise last_error
    raise LLMServiceError('INTERNAL_ERROR', 'The LLM request ended unexpectedly.', request_id, False, 0, 500)

async def _generate_with_model_fallback(*, api_key, base_url, models, messages, rows, max_tokens, request_id, task, language, assessment_only, proposal_validator, stage_context, started_at, max_attempts, deadline=None):
    svc = _current_service()
    last_error = None
    validation_feedback = None
    deadline = deadline or _request_deadline(started_at)
    for attempt, configured_model in enumerate(models[:max_attempts], start=1):
        model = models[0] if task == 'map_proposal' and validation_feedback is not None else configured_model
        elapsed = svc.clock.monotonic() - started_at
        remaining = svc.remaining_until(deadline)
        response_fields = _empty_response_diagnostics()
        route_recovery = {'changed': False, 'salvageAction': 'none', 'routeSentenceCount': 0, 'routeCoordinateCount': 0, 'droppedSentenceCount': 0, 'coordinateLinksDropped': False}
        if remaining <= 0:
            raise asyncio.TimeoutError()
        attempt_timeout = min(svc.config.primary_attempt_timeout_seconds if attempt == 1 else remaining, remaining)
        _log_llm_event('llm_attempt_started', requestId=request_id, task=task, model=model, attempt=attempt, maxAttempts=len(models[:max_attempts]), timeoutSeconds=round(attempt_timeout, 3), responseMode='json_object')
        try:
            attempt_messages = _messages_with_validation_feedback(messages, validation_feedback, task)
            response = await asyncio.wait_for(_request_completion(api_key, base_url, model, attempt_messages, max_tokens, attempt_timeout, task=task), timeout=attempt_timeout)
            choice = response.choices[0]
            finish_reason = str(getattr(choice, 'finish_reason', '') or '')
            if finish_reason == 'length':
                raise ValueError('The model output reached its token limit.')
            response_fields = _response_diagnostics(response, choice)
            content = str(choice.message.content or '')
            if not content.strip():
                raise EmptyModelResponse('The model returned an empty response.')
            if len(content) > CHAT_RESPONSE_HARD_LENGTH * 2:
                raise ValueError('The model response exceeded the transport safety limit.')
            payload = json.loads(content)
            if assessment_only:
                payload = _canonicalize_stage_assessment_payload(payload, stage_context)
                if isinstance(payload.get('assistantMessage'), str):
                    payload = dict(payload)
                    payload['assistantMessage'], route_recovery = _recover_overlong_content(payload['assistantMessage'], rows, language=language)
                    if route_recovery['changed']:
                        _log_llm_event('llm_route_salvaged', requestId=request_id, task=task, salvageAction=route_recovery['salvageAction'], routeSentenceCount=route_recovery['routeSentenceCount'], routeCoordinateCount=route_recovery['routeCoordinateCount'], droppedSentenceCount=route_recovery['droppedSentenceCount'])
            validated = validate_chat_response(payload, assessment_only, language, stage_context, rows)
            if validated[4].get('proposalOffer') is not None and proposal_offer_requires_execution_brief(validated[4]['proposalOffer'], validated[0], validated[4]['proposalOffer'].get('summary'), validated[4]['proposalOffer'].get('rationale'), _latest_role_content(messages, 'user')):
                if attempt < len(models[:max_attempts]):
                    raise ValueError('An exact coordinate edit requires a server-validated executionBrief.')
                validated[4]['proposalOffer'] = None
                if validated[4].get('move') == 'offer_revision':
                    validated[4]['move'] = 'offer_perspective'
            if task == 'map_proposal' and validated[2] is None:
                raise ValueError('An explicitly requested map proposal requires proposedRows.')
            if validated[2] is not None and proposal_validator is not None:
                proposal_validator(validated[2])
            route_recovery['coordinateLinksDropped'] = isinstance(payload.get('guidance'), dict) and len(payload['guidance'].get('coordinateLinks') or []) > len(validated[4].get('coordinateLinks') or [])
            latency_ms = int((svc.clock.monotonic() - started_at) * 1000)
            result = LLMExecutionResult(assistant_message=_compose_assistant_message(validated[0], validated[4], language, assessment_only, stage_context), assessment=validated[1], proposed_rows=validated[2], modification_summary=validated[3], attempts_used=attempt, request_id=request_id, model=model, latency_ms=latency_ms, guidance=validated[4])
            opening_recovery = validated[4].get('openingRecovery') or []
            opening_presentation = dict(validated[4].get('openingPresentation') or {})
            if assessment_only:
                opening_presentation['sentenceCount'] = len([item.strip() for item in re.split('(?<=[.!?。！？])\\s*', result.assistant_message or '') if item.strip()])
            _log_llm_event('llm_request_completed', requestId=request_id, task=task, outcome='success', model=model, attemptsUsed=attempt, latencyMs=latency_ms, responseMode='json_object', failureClass=None, salvageAction=route_recovery['salvageAction'], routeSentenceCount=route_recovery['routeSentenceCount'], routeCoordinateCount=route_recovery['routeCoordinateCount'], droppedSentenceCount=route_recovery['droppedSentenceCount'], coordinateLinksDropped=route_recovery['coordinateLinksDropped'], bodyPreserved=bool(validated[0].strip()), openingRecoveryActions=opening_recovery, openingPresentation=opening_presentation, remainingSeconds=round(svc.remaining_until(deadline), 3), **response_fields)
            return result
        except asyncio.TimeoutError as exception:
            failure_reason = None
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not respond before the attempt timeout.', request_id, True, attempt, 504)
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            last_error = classify_exception(exception, request_id, attempt)
            if last_error.code == 'MODEL_RESPONSE_INVALID' and failure_reason:
                validation_feedback = failure_reason
        failure_fields = {'requestId': request_id, 'task': task, 'model': model, 'attempt': attempt, 'code': last_error.code, 'retryable': last_error.retryable, 'latencyMs': int((svc.clock.monotonic() - started_at) * 1000), 'responseMode': 'json_object', **_provider_error_fields(last_error)}
        failure_fields.update(response_fields)
        if failure_reason:
            failure_fields['validationReason'] = failure_reason
        failure_fields['failureClass'] = _llm_failure_class(last_error, validation_feedback)
        failure_fields['salvageAction'] = 'pending_retry'
        failure_fields['remainingSeconds'] = round(svc.remaining_until(deadline), 3)
        _log_llm_event('llm_attempt_failed', **failure_fields)
        if not last_error.retryable:
            raise last_error
        if attempt < max_attempts and (not _retry_budget_available(deadline, request_id=request_id, task=task, attempt=attempt, max_attempts=max_attempts, response_mode='json_object', fallback_reason=last_error.code)):
            break
    if last_error is not None:
        failure_class = _llm_failure_class(last_error, validation_feedback)
        _log_llm_event('llm_request_completed', requestId=request_id, task=task, outcome='error', code=last_error.code, attemptsUsed=last_error.attempts_used, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), responseMode='json_object', failureClass=failure_class, salvageAction='none', routeSentenceCount=0, routeCoordinateCount=0, droppedSentenceCount=0, coordinateLinksDropped=False, remainingSeconds=round(svc.remaining_until(deadline), 3), **_provider_error_fields(last_error))
        raise last_error
    raise LLMServiceError('INTERNAL_ERROR', 'The LLM request ended unexpectedly.', request_id, False, 0, 500)

def _messages_with_validation_feedback(messages, validation_feedback, task=None):
    if not validation_feedback:
        return messages
    corrected = [dict(message) for message in messages]
    instruction = f'Your previous response for this same request was rejected for this structural reason: {validation_feedback} Return a fresh complete JSON object that corrects that reason while following every original content and safety rule. Do not mention the retry or validation error to the designer.'
    if any((marker in str(validation_feedback).casefold() for marker in ('token limit', 'too long', 'cut off'))):
        instruction += ' Preserve reliable content needed to answer the question and remove repetition, including unnecessary route detail; do not enumerate a full solver trace. End the assistantMessage with a complete sentence.'
    if task in {'chat', 'stage_assessment', 'stage_assessment_fallback'}:
        instruction += ' Preserve reliable views and natural voice; repair the specified defect without adding forced humor.'
    if task == 'map_proposal':
        instruction += ' The designer explicitly authorized a complete map proposal, so keep guidance.move as deliver_revision, return non-null proposedRows and a truthful modificationSummary, and do not downgrade the response to an offer_revision or a text-only suggestion.'
    for message in corrected:
        if message.get('role') == 'system':
            message['content'] = f"{message.get('content', '')}\n\n{instruction}"
            return corrected
    corrected.insert(0, {'role': 'system', 'content': instruction})
    return corrected

def _plain_messages_with_validation_feedback(messages, validation_feedback, *, validation_mode='ordinary_chat', rows=None, stage_context=None, coordinate_free_recovery=False):
    if not validation_feedback and (not coordinate_free_recovery):
        return messages
    corrected = [dict(message) for message in messages]
    feedback_text = str(validation_feedback)
    feedback_lower = feedback_text.casefold()
    if coordinate_free_recovery:
        instruction = "This is the final corrective attempt for an ordinary conversation. Return only a fresh, complete visible reply that directly answers the designer's latest message. Preserve the useful design interpretation, but do not state coordinates, current entity positions, adjacency, tile states, route endpoints, or map inventories. Do not output a GUIDANCE block, cards, metadata, workflow language, or mention this correction."
    elif _has_proposal_clarification(stage_context):
        specification = (stage_context or {}).get('proposalClarification') or {}
        envelope_instruction = 'Return fresh JSON with exactly body, question, options, comparedOptions and discriminant. Keep options null for locked directions; only an authorized revision may replace them. Ask one question only for two or more directions; for zero/one directions keep question, comparedOptions and discriminant empty. ' if specification.get('optionPolicyVersion') == proposal_options.VERSION else f"Return fresh JSON with exactly body and question. Write exactly one question for target dimension {specification.get('questionKey')}. "
        instruction = f'Your previous private proposal-clarification envelope was rejected: {feedback_text} ' + envelope_instruction + 'Preserve the useful design interpretation and do not mention this repair. Do not ask for coordinates, cells, positions, adjacency, routes, or implementation details.'
    elif 'token limit' in feedback_lower or 'too long' in feedback_lower:
        instruction = 'Your previous reply for this same request was cut off or exceeded the visible response limit. Write a fresh, complete reply rather than continuing the partial text. Preserve the reliable observations and trade-offs needed to answer the question; remove repetition and unnecessary route detail, without an exhaustive solver trace. Do not mention this correction to the designer. End with a complete conclusion.'
    elif 'chinese visible text contains' in feedback_lower:
        instruction = f"Your previous reply used a Latin term that is not permitted in Chinese visible prose: {feedback_text} Rewrite the complete reply in natural Chinese. The map's visible labels P, B, T, B1, B2, T1, T2 and Stage may remain; explain a map label directly when the designer asks what it means. Translate or paraphrase other English terms, and never expose implementation identifiers. Keep the same grounded answer and do not mention this correction."
    elif 'route reasoning' in feedback_lower:
        instruction = 'Your previous reply used an over-expanded route trace. Write a fresh reply that preserves reliable content needed to answer the question and reduces route reasoning to a short, grounded passage with only the key corridor, endpoint, and design consequence. Do not enumerate every coordinate, movement, alternative, BFS result, or solver state. Do not mention this correction to the designer.'
    elif 'HUMAN_EDIT_ENGAGEMENT' in feedback_text or 'human-edit engagement' in feedback_lower:
        instruction = f'The previous human-edit discussion reply has an invalid state-dependent claim. Repair the affected prose only: {feedback_text}. State is owned by structured understanding, so do not emit DISAGREEMENT or HUMAN_EDIT_ENGAGEMENT here. Do not claim consensus, accepted edits, or permission from willingness to discuss.'
    elif 'REVISION_ADVICE' in validation_feedback or 'proposalOffer' in validation_feedback:
        instruction = f'Your previous reply for this same request was rejected because it omitted the required guidance card: {validation_feedback} Write a fresh reply with a substantive PROPOSAL_SUMMARY and PROPOSAL_RATIONALE in the trailing GUIDANCE block, plus the required MANUAL_EDIT companion card. Keep it conceptual: do not output map rows, tile operations, or claim that the map changed. Do not mention this correction to the designer.'
    elif validation_mode in {'ordinary_chat', 'route_discussion'}:
        instruction = 'Your previous reply contained a spatial claim that conflicts with deterministic map facts and is not verified for the current Stage. Write a fresh, complete design discussion that preserves the useful interpretation and removes only the unsupported coordinate, position, or optional card. Do not ask the designer to confirm a modification unless they explicitly requested an edit. If the designer referred to an older version, discuss it conceptually unless a historical snapshot is supplied. Do not mention this correction to the designer.'
    else:
        instruction = f'Your previous reply for this same request was rejected because it made a spatial claim that conflicts with deterministic map facts: {validation_feedback} Write a fresh grounded reply. Use only verified entity IDs or coordinates for current map relations, and do not mention this correction to the designer.'
    if validation_mode not in {'ordinary_chat', 'route_discussion'} and rows is not None and any((marker in feedback_lower for marker in ('spatial', 'grounding', 'map facts', 'does not match', 'saved map'))):
        try:
            facts = json.loads(_map_facts_for_prompt(rows, stage_context))
            entities = list(facts.get('entities') or [])
            table = '\n'.join((f"{item.get('id')}: row {item.get('row')}, column {item.get('column')} (identity {item.get('identityConfidence') or 'unknown'})" if item.get('identityConfidence') == 'exact' else f"unlabelled {item.get('kind') or 'entity'} at row {item.get('row')}, column {item.get('column')} (identity unknown; do not use a historical label)" for item in entities if item.get('row') and item.get('column')))
            if table:
                instruction += f"\n\nGrounding failure type/details (use only for correction, do not mention it): {feedback_text[:500]}\nAuthoritative current entity table for this retry (do not reorder or infer it):\n{table}\nCurrent map fingerprint: {facts.get('mapFingerprint')}\nRecheck every related entity and rewrite the complete reply using these exact bindings."
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    instruction += ' Preserve reliable views and the original natural voice while correcting only the specified defect; do not add jokes merely to meet a style preference.'
    for message in corrected:
        if message.get('role') == 'system':
            message['content'] = f"{message.get('content', '')}\n\n{instruction}"
            return corrected
    corrected.insert(0, {'role': 'system', 'content': instruction})
    return corrected

def _canonicalize_stage_assessment_payload(payload, stage_context=None):
    """Apply endpoint-owned Stage opening fields before validation.

    Stage assessment is an observation, not a proposal or an intent-confirmation
    step.  Kimi sometimes reuses the ordinary-chat envelope and fills optional
    intent/proposal fields anyway.  Those fields are independently disposable;
    rejecting the whole response would throw away otherwise useful map analysis.
    Human-edit openings may still retain their warning/disagreement fields.
    """
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    guidance = payload.get('guidance')
    if isinstance(guidance, dict):
        guidance = dict(guidance)
        guidance['move'] = 'observe_stage'
        guidance['intentHypothesis'] = None
        guidance['intentConfidence'] = None
        guidance['proposalOffer'] = None
        guidance.pop('designContextPatch', None)
        if not _is_human_edit_stage_opening(True, stage_context):
            guidance['uiCues'] = []
        normalized['guidance'] = guidance
    if 'designContextPatch' in normalized:
        normalized['designContextPatch'] = None
    return normalized

def _stage_opening_assessment_defaults(language):
    """Provide archival fields without making display metadata a hard failure."""
    if language == 'zh-CN':
        observation = '这个当前 Stage 可以继续从第一次推动时的选择来观察。'
        difficulty = '在我看来，还需要结合实际试玩来判断它的体验难度。'
        feature = '当前布局中的首次推进判断'
        suggestion = '留意玩家最先读到的通道如何影响推动顺序'
    else:
        observation = 'This current Stage can be observed through the first push choice.'
        difficulty = 'In my view, play evidence is still needed to judge the experienced difficulty.'
        feature = 'The first-push judgment in the current layout'
        suggestion = 'Watch how the first readable corridor affects push order'
    return {'solutionSummary': observation, 'difficultyOpinion': difficulty, 'features': [feature], 'suggestions': [suggestion], 'satisfactionQuestion': None}

def _opening_discussion_candidate(value, language):
    """Return a safe optional opening focus, never a leading question."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    question_marks = text.count('?') + text.count('？')
    if question_marks > 1:
        return None
    if question_marks == 1:
        try:
            return _normalize_opening_question(text)
        except ValueError:
            return None
    return text if _discussion_insight_is_useful(text, language) else None

def _split_opening_questions(message, follow_up_question, language, stage_one):
    """Keep declarative opening prose when question placement is imperfect.

    Questions in a Stage opening are presentation metadata, not map evidence.
    A leading, duplicated, or misplaced question must therefore be dropped (or
    moved to the discussion field), never make an otherwise grounded body fail.
    """
    paragraphs = []
    body_questions = []
    for paragraph in (part.strip() for part in str(message or '').split('\n\n')):
        if not paragraph:
            continue
        declarative = []
        for sentence in (part.strip() for part in re.split('(?<=[.!?。！？])\\s*', paragraph) if part.strip()):
            if sentence.endswith(('?', '？')):
                body_questions.append(sentence)
            else:
                declarative.append(sentence)
        if declarative:
            separator = '' if re.search('[\\u3400-\\u9fff]', paragraph) else ' '
            paragraphs.append(separator.join(declarative))
    body = '\n\n'.join(paragraphs).strip()
    if stage_one:
        return (body, None, ['opening_questions_removed'] if body_questions else [])
    candidate = _opening_discussion_candidate(follow_up_question, language)
    actions = []
    if follow_up_question and candidate is None:
        actions.append('invalid_opening_focus_dropped')
    if candidate is None:
        for question in body_questions:
            candidate = _opening_discussion_candidate(question, language)
            if candidate is not None:
                actions.append('body_question_moved_to_discussion')
                break
    if body_questions and candidate is None:
        actions.append('leading_or_extra_questions_removed')
    elif len(body_questions) > 1:
        actions.append('extra_opening_questions_removed')
    return (body, candidate, actions)

def _recover_stage_opening_payload(payload, stage_context, language):
    """Normalize recoverable opening envelope errors before strict validation.

    This deliberately leaves map-grounding, revision execution, and human-edit
    risk checks to their existing validators.  It only makes display-only
    fields non-fatal so useful prose reaches those validators first.
    """
    if not isinstance(payload, dict):
        return (payload, [])
    actions = []
    allowed = {'assistantMessage', 'contentBlocks', 'guidance', 'assessment', 'proposedRows', 'modificationSummary', 'designContextPatch'}
    normalized = {key: value for key, value in payload.items() if key in allowed}
    if set(payload) != set(normalized):
        actions.append('unexpected_opening_fields_dropped')
    raw_guidance = payload.get('guidance')
    if not isinstance(raw_guidance, dict):
        raw_guidance = {}
        actions.append('malformed_opening_guidance_replaced')
    human_edit = _is_human_edit_stage_opening(True, stage_context)
    follow_up = _opening_discussion_candidate(raw_guidance.get('followUpQuestion'), language)
    if raw_guidance.get('followUpQuestion') and follow_up is None:
        actions.append('invalid_opening_focus_dropped')
    guidance = {'move': 'observe_stage', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': follow_up, 'proposalOffer': None, 'disagreement': raw_guidance.get('disagreement') if human_edit else None, 'uiCues': raw_guidance.get('uiCues') if human_edit else [], 'coordinateLinks': []}
    if any((raw_guidance.get(key) is not None for key in ('intentHypothesis', 'proposalOffer', 'designContextPatch'))) or payload.get('designContextPatch') is not None:
        actions.append('opening_metadata_dropped')
    if human_edit and (raw_guidance.get('uiCues') is not None or raw_guidance.get('disagreement') is not None):
        candidate = dict(guidance)
        candidate['followUpQuestion'] = None
        candidate['uiCues'] = raw_guidance.get('uiCues') or []
        candidate['disagreement'] = raw_guidance.get('disagreement')
        try:
            checked = _validate_guidance(candidate, True, language, stage_context, rows=(stage_context or {}).get('snapshotRows'))
            guidance['uiCues'] = checked['uiCues']
            guidance['disagreement'] = checked['disagreement']
        except (TypeError, ValueError):
            guidance['uiCues'] = []
            guidance['disagreement'] = None
            actions.append('invalid_human_edit_risk_metadata_dropped')
    normalized['guidance'] = guidance
    normalized['proposedRows'] = None
    normalized['modificationSummary'] = ''
    normalized['designContextPatch'] = None
    defaults = _stage_opening_assessment_defaults(language)
    raw_assessment = payload.get('assessment')
    if isinstance(raw_assessment, dict):
        assessment = dict(defaults)
        for key in ('solutionSummary', 'difficultyOpinion'):
            value = raw_assessment.get(key)
            if isinstance(value, str) and value.strip():
                assessment[key] = value
            elif value is not None:
                actions.append('opening_assessment_fields_normalized')
        for key in ('features', 'suggestions'):
            value = raw_assessment.get(key)
            if isinstance(value, list) and value and all((isinstance(item, str) and item.strip() for item in value)):
                assessment[key] = value
            elif value is not None:
                actions.append('opening_assessment_fields_normalized')
        if set(raw_assessment) != set(assessment):
            actions.append('opening_assessment_fields_normalized')
    else:
        assessment = defaults
        actions.append('opening_assessment_rebuilt')
    normalized['assessment'] = assessment
    return (normalized, actions)

def validate_chat_response(payload, assessment_only=False, language='en', stage_context=None, rows=None, validation_mode='strict', historical_reference=False):
    opening_recovery_actions = []
    if assessment_only:
        payload = _canonicalize_stage_assessment_payload(payload, stage_context)
        payload, opening_recovery_actions = _recover_stage_opening_payload(payload, stage_context, language)
    if not isinstance(payload, dict):
        raise ValueError('The model response must be a JSON object.')
    allowed_payload_fields = {'assistantMessage', 'contentBlocks', 'guidance', 'assessment', 'proposedRows', 'modificationSummary', 'designContextPatch'}
    required_payload_fields = allowed_payload_fields - {'designContextPatch', 'contentBlocks'}
    if not required_payload_fields.issubset(payload) or set(payload) - allowed_payload_fields:
        raise ValueError('The model response contains unexpected or missing fields.')
    raw_assistant_message = _clean_text(payload.get('assistantMessage'), 'assistantMessage', maximum=CHAT_RESPONSE_HARD_LENGTH)
    assistant_message, server_route_links = _render_content_blocks(payload.get('contentBlocks'), raw_assistant_message, rows, stage_context, language)
    assistant_message = _sanitize_visible_model_text(_normalize_single_level_language(assistant_message), language)
    if assessment_only and rows:
        assistant_message, _removed_grounding = _strip_invalid_stage_grounding_sentences(assistant_message, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
        if not assistant_message:
            raise ValueError('A Stage opening has no grounded visible analysis left.')
    if assessment_only:
        stage_one = _is_stage_one(stage_context)
        assistant_message, recovered_focus, question_actions = _split_opening_questions(assistant_message, (payload.get('guidance') or {}).get('followUpQuestion'), language, stage_one)
        opening_recovery_actions.extend(question_actions)
        payload = dict(payload)
        payload_guidance = dict(payload.get('guidance') or {})
        payload_guidance['followUpQuestion'] = recovered_focus
        payload['guidance'] = payload_guidance
        if not assistant_message:
            raise ValueError('A Stage opening has no grounded visible analysis left.')
    guidance = _validate_guidance(payload.get('guidance'), assessment_only, language, stage_context, rows=rows)
    guidance = _sanitize_visible_guidance(guidance, language)
    if server_route_links:
        guidance['coordinateLinks'] = [*server_route_links, *(guidance.get('coordinateLinks') or [])][:COORDINATE_LINK_LIMIT]
    patch_value = payload.get('designContextPatch')
    if patch_value is not None:
        try:
            guidance['designContextPatch'] = validate_design_context_patch(patch_value)
        except (TypeError, ValueError) as exception:
            guidance['designContextPatchError'] = str(exception)[:500]
    if assessment_only and _is_human_edit_stage_opening(assessment_only, stage_context) and (guidance.get('disagreement') is None):
        warning_text = next((cue.get('text') for cue in guidance.get('uiCues') or [] if cue.get('type') in {'warning', 'tradeoff'}), None)
        if warning_text:
            guidance['disagreement'] = _disagreement_from_warning(warning_text, language, stage_context)
    if guidance.get('proposalOffer') is not None:
        guidance['proposalOffer'] = _distill_proposal_offer(guidance['proposalOffer'], assistant_message, '', language)
    stage_one_opening = assessment_only and _is_stage_one(stage_context)
    deterministic_opening_question = None
    extracted_question = None
    if stage_one_opening:
        assistant_message = _questionless_body(assistant_message)
        if not assistant_message:
            raise ValueError('A Stage 1 opening must contain guidance outside its questions.')
        assistant_message = _ensure_stage_one_orientation(assistant_message, None, language)
        guidance['followUpQuestion'] = None
        extracted_question = None
    elif not assessment_only:
        clarification_mode = classify_guidance_request([{'role': 'user', 'content': ''}], stage_context, stage_opening=assessment_only)
        assistant_message, extracted_question = _extract_message_question(assistant_message, max_questions=3 if clarification_mode == 'needs_clarification' else 1)
    if extracted_question is not None:
        if guidance['followUpQuestion'] is None:
            if assessment_only:
                raise ValueError('A Stage opening question must use guidance.followUpQuestion.')
            guidance['followUpQuestion'] = extracted_question
    if guidance['followUpQuestion'] is not None:
        if not (_is_human_edit_stage_opening(assessment_only, stage_context) and _is_human_edit_intent_question(guidance['followUpQuestion'], language, stage_context)):
            guidance['followUpQuestion'] = _refine_discussion_focus(guidance['followUpQuestion'], assistant_message, language)
            if _discussion_focus_repeats_recent(guidance['followUpQuestion'], stage_context):
                guidance['followUpQuestion'] = None
    if assessment_only and (not stage_one_opening) and (guidance['followUpQuestion'] is None):
        guidance['followUpQuestion'] = _perspective_discussion_focus(assistant_message, language)
    if _is_human_edit_stage_opening(assessment_only, stage_context):
        guidance['followUpQuestion'] = _human_edit_intent_discussion_focus(guidance.get('followUpQuestion'), stage_context, language)
    if assessment_only and (stage_context or {}).get('discussionCardMode') == 'disagreement_only' and (not (isinstance(guidance.get('disagreement'), dict) and guidance['disagreement'].get('status') == 'active')):
        guidance['followUpQuestion'] = None
    opening_presentation = {}
    if assessment_only:
        opening_before_sentences = [item.strip() for item in re.split('(?<=[.!?。！？])\\s*', assistant_message or '') if item.strip()]
        if _is_human_edit_stage_opening(assessment_only, stage_context):
            assistant_message = _compact_human_edit_opening_inventory(assistant_message, language)
        opening_after_sentences = [item.strip() for item in re.split('(?<=[.!?。！？])\\s*', assistant_message or '') if item.strip()]
        compacted_inventory_count = max(0, len(opening_before_sentences) - len(opening_after_sentences))
        if compacted_inventory_count:
            opening_recovery_actions.append('human_edit_inventory_compacted')
        assistant_message = _format_stage_opening_paragraphs(assistant_message)
        opening_presentation = {'sentenceCount': len(opening_after_sentences), 'confirmationSentenceCount': sum((1 for item in opening_after_sentences if re.search('(?:已保存|可解|确认|saved|solvable|confirmed)', item, re.IGNORECASE))), 'compactedInventorySentenceCount': compacted_inventory_count, 'displayMode': 'human_edit_balanced' if _is_human_edit_stage_opening(assessment_only, stage_context) else 'stage_opening_balanced'}
    assessment_payload = payload.get('assessment')
    if assessment_payload is None:
        if assessment_only:
            raise ValueError('An assessment-only response requires assessment.')
        assessment = {}
    elif not isinstance(assessment_payload, dict):
        raise ValueError('assessment must be an object.')
    else:
        assessment = {'solutionSummary': _sanitize_visible_model_text(_normalize_single_level_language(_clean_text(assessment_payload.get('solutionSummary'), 'assessment.solutionSummary')), language), 'difficultyOpinion': _sanitize_visible_model_text(_normalize_single_level_language(_clean_text(assessment_payload.get('difficultyOpinion'), 'assessment.difficultyOpinion')), language), 'features': [_sanitize_visible_model_text(_normalize_single_level_language(item), language) for item in _clean_list(assessment_payload.get('features'), 'features')], 'suggestions': [_sanitize_visible_model_text(_normalize_single_level_language(item), language) for item in _clean_list(assessment_payload.get('suggestions'), 'suggestions')], 'satisfactionQuestion': _sanitize_visible_model_text(_normalize_single_level_language(_clean_optional_text(assessment_payload.get('satisfactionQuestion'), 'assessment.satisfactionQuestion')), language)}
        if stage_one_opening:
            assessment['satisfactionQuestion'] = None
        elif deterministic_opening_question is not None:
            assessment['satisfactionQuestion'] = deterministic_opening_question
        if assessment_only:
            assessment['satisfactionQuestion'] = guidance['followUpQuestion']
        if assessment_only and assessment['satisfactionQuestion'] is not None:
            satisfaction_marks = assessment['satisfactionQuestion'].count('?') + assessment['satisfactionQuestion'].count('？')
            if satisfaction_marks == 1:
                assessment['satisfactionQuestion'] = _normalize_opening_question(assessment['satisfactionQuestion'])
            elif satisfaction_marks > 1 or not _discussion_insight_is_useful(assessment['satisfactionQuestion'], language):
                raise ValueError('assessment.satisfactionQuestion must be one useful discussion focus.')
        if assessment_only and assessment['satisfactionQuestion'] != guidance['followUpQuestion']:
            raise ValueError('assessment.satisfactionQuestion must match guidance.followUpQuestion.')
    assessment = _sanitize_assessment_grounding(assessment, rows, stage_context, language, historical_reference)
    if assessment_only and opening_recovery_actions:
        guidance['openingRecovery'] = sorted(set(opening_recovery_actions))
    if assessment_only:
        guidance['openingPresentation'] = opening_presentation
    proposed_rows = payload.get('proposedRows')
    if assessment_only and proposed_rows is not None:
        raise ValueError('An assessment-only response cannot propose a map.')
    if guidance['move'] == 'offer_revision' and proposed_rows is not None:
        raise ValueError('A revision offer cannot include proposedRows.')
    if proposed_rows is not None and guidance['move'] != 'deliver_revision':
        raise ValueError('proposedRows requires the deliver_revision move.')
    if guidance['move'] == 'deliver_revision' and proposed_rows is None:
        raise ValueError('The deliver_revision move requires proposedRows.')
    proposal_text = ' '.join((part for part in (assistant_message, (guidance.get('proposalOffer') or {}).get('summary') if isinstance(guidance.get('proposalOffer'), dict) else '', (guidance.get('proposalOffer') or {}).get('rationale') if isinstance(guidance.get('proposalOffer'), dict) else '') if str(part or '').strip()))
    if guidance.get('proposalOffer') is not None and _response_contains_multiple_proposals(proposal_text):
        raise ValueError('A single response may contain only one primary proposal.')
    if proposed_rows is not None:
        if not isinstance(proposed_rows, list) or not all((isinstance(row, str) for row in proposed_rows)):
            raise ValueError('proposedRows must be null or a list of strings.')
        proposed_rows = list(proposed_rows)
    assistant_message = _normalize_change_claims_for_proposal(assistant_message, language, proposed_rows is not None)
    assistant_message, _content_recovery = _recover_overlong_content(assistant_message, rows, language=language)
    assistant_message = _normalize_response_paragraphs(assistant_message)
    assistant_message = _remove_guidance_from_body(assistant_message, guidance.get('followUpQuestion'))
    guidance['coordinateLinks'] = _filter_coordinate_links(guidance.get('coordinateLinks'), assistant_message, rows, (stage_context or {}).get('entityBindings'))
    guidance['coordinateLinks'] = _recover_coordinate_links(assistant_message, rows, guidance['coordinateLinks'], (stage_context or {}).get('entityBindings'))
    modification_summary = payload.get('modificationSummary', '')
    if not isinstance(modification_summary, str):
        raise ValueError('modificationSummary must be a string.')
    if rows is not None:
        if validation_mode in {'ordinary_chat', 'route_discussion'}:
            _validate_map_grounding_texts([assistant_message], rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
            guidance = _sanitize_ordinary_grounding_metadata(guidance, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
        else:
            grounding_texts = [assistant_message, guidance.get('followUpQuestion'), guidance.get('intentHypothesis'), modification_summary]
            if guidance.get('proposalOffer'):
                grounding_texts.extend(guidance['proposalOffer'].values())
            if guidance.get('disagreement'):
                grounding_texts.extend((guidance['disagreement'].get(field) for field in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion')))
            grounding_texts.extend((cue.get('text') for cue in guidance.get('uiCues') or []))
            for value in assessment.values():
                grounding_texts.extend(value if isinstance(value, list) else [value])
            _validate_map_grounding_texts(grounding_texts, rows, historical_reference=historical_reference, entity_bindings=(stage_context or {}).get('entityBindings'))
    modification_summary = _sanitize_visible_model_text(_normalize_single_level_language(modification_summary.strip()), language)[:1000]
    guidance = _isolate_optional_language_components(guidance, language, required_question=bool(guidance.get('disagreement')) or guidance.get('move') == 'clarify_intent')
    _assert_visible_output_language(language, assistant_message, modification_summary, guidance=guidance, assessment=assessment)
    return (assistant_message, assessment, proposed_rows, modification_summary, guidance)

def _extract_message_question(message, max_questions=1):
    if '?' not in message and '？' not in message:
        return (message, None)
    declarative_paragraphs = []
    questions = []
    for paragraph in (part.strip() for part in message.split('\n\n')):
        if not paragraph:
            continue
        sentences = [part.strip() for part in re.split('(?<=[.!?。！？])\\s*', paragraph) if part.strip()]
        declarative_sentences = []
        for sentence in sentences:
            if sentence.endswith(('?', '？')):
                questions.append(sentence)
            else:
                declarative_sentences.append(sentence)
        if declarative_sentences:
            separator = '' if re.search('[\\u3400-\\u9fff]', paragraph) else ' '
            declarative_paragraphs.append(separator.join(declarative_sentences))
    try:
        max_questions = max(1, int(max_questions))
    except (TypeError, ValueError):
        max_questions = 1
    if len(questions) > max_questions:
        raise ValueError(f'assistantMessage can contain at most {max_questions} question(s).')
    if len(questions) > 1:
        return (message, None)
    if not declarative_paragraphs:
        raise ValueError('assistantMessage must contain a declarative response outside its questions.')
    return ('\n\n'.join(declarative_paragraphs), questions[0] if questions else None)

def _extract_plain_message_question(message, language='en'):
    """Extract one useful question and discard low-information questions beside prose."""
    if '?' not in message and '？' not in message:
        return (message, None, False)
    declarative_paragraphs = []
    questions = []
    for paragraph in (part.strip() for part in message.split('\n\n')):
        if not paragraph:
            continue
        sentences = [part.strip() for part in re.split('(?<=[.!?。！？])\\s*', paragraph) if part.strip()]
        declarative_sentences = []
        for sentence in sentences:
            if sentence.endswith(('?', '？')):
                questions.append(sentence)
            else:
                declarative_sentences.append(sentence)
        if declarative_sentences:
            separator = '' if re.search('[\\u3400-\\u9fff]', paragraph) else ' '
            declarative_paragraphs.append(separator.join(declarative_sentences))
    question_marks = message.count('?') + message.count('？')
    if len(questions) == 1 and question_marks == 1:
        question = questions[0]
        useful = _question_is_specific_and_vivid(question, language)
        if not declarative_paragraphs:
            return ('', question, False) if useful else (message, None, True)
        if useful:
            return ('\n\n'.join(declarative_paragraphs), question, False)
        return ('\n\n'.join(declarative_paragraphs), None, False)
    if declarative_paragraphs and questions and all((not _question_is_specific_and_vivid(question, language) for question in questions)):
        return ('\n\n'.join(declarative_paragraphs), None, False)
    return (message, None, False)

def _question_is_specific_and_vivid(question, language='en'):
    text = str(question or '').strip().casefold()
    if not text:
        return False
    generic_patterns = ('^(?:你)?(?:觉得|认为)(?:这个|这条|该)?(?:方向|思路|方案)(?:如何|怎么样|可行吗)[？?]?$', '^(?:你)?怎么看[？?]?$', '^(?:这样|这)(?:可以|行|好吗|合适吗)[？?]?$', '^(?:what do you think|does (?:this|that) (?:direction )?(?:work|seem good)|is (?:this|that) (?:direction )?(?:okay|ok|good|feasible))[?]?$', '^(?:would you like|do you want) (?:a different|another|a) (?:direction|idea|option)[?]?$')
    if any((re.match(pattern, text, re.IGNORECASE) for pattern in generic_patterns)):
        return False
    return True

def _question_repeats_recent_judgment(question, messages):
    previous_assistant = _latest_role_content(messages[:-1], 'assistant')
    previous_questions = [sentence.strip() for sentence in re.split('(?<=[.!?。！？])\\s*|[\\r\\n]+', previous_assistant) if sentence.strip().endswith(('?', '？'))]
    return any((_guidance_text_matches(question, previous) for previous in previous_questions))

def _deterministic_key_question(messages, language, rows):
    latest = _latest_role_content(messages, 'user')
    lowered = latest.casefold()
    if not latest or '?' in latest or '？' in latest:
        return None
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', latest):
        evaluation = any((marker in latest for marker in ('我觉得', '我认为', '感觉', '显得', '只是', '太', '不够', '像')))
        direction = _latest_user_states_direction(messages)
        if not (evaluation or direction):
            return None
        if '水' in latest and '箱' in latest:
            return '当箱子第一次贴着水边推进后，哪一段路线最该让玩家意识到水域正在影响判断？'
        if '水' in latest:
            return '当箱子第一次经过水域边缘时，哪条路线应该最先让玩家读出水域并不只是背景？'
        if '箱' in latest and '目标' in latest:
            return '当箱子第一次朝目标推进时，哪个落点最该帮助玩家读懂接下来的推动顺序？'
        if '通道' in latest or '路线' in latest:
            return '当箱子第一次进入这条通道时，哪个转折最该让玩家看清后续路线选择？'
        return None
    evaluation = bool(re.search('\\b(?:i think|i feel|feels?|seems?|looks?|too|only|just|not enough)\\b', lowered))
    direction = _latest_user_states_direction(messages)
    if not (evaluation or direction):
        return None
    if any((word in lowered for word in ('water', 'pond'))) and any((word in lowered for word in ('box', 'crate'))):
        return 'When the box first moves along the water edge, which part of the route should make the player notice that the water is shaping their decision?'
    if any((word in lowered for word in ('water', 'pond'))):
        return 'When the box first passes the water edge, which route should make the player recognize that the water is more than scenery?'
    if any((word in lowered for word in ('box', 'crate'))) and any((word in lowered for word in ('target', 'goal'))):
        return 'When the box first moves toward the target, which landing point should help the player read the next push order?'
    if any((word in lowered for word in ('corridor', 'route'))):
        return 'When the box first enters that corridor, which turn should help the player read the route choice ahead?'
    return None

def _deterministic_reply_discussion_focus(visible_content, language):
    text = str(visible_content or '')
    lowered = text.casefold()
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', text):
        if '水' in text and '箱' in text:
            return '试玩时，请直接看箱子第一次贴着调整后的水边推进：在推之前，玩家能不能看清该先向上绕开，还是沿水边继续下推？如果还是只有一条很显眼的走法，说明这次调整还没有带来真正需要判断的路线。'
        if '目标' in text and '箱' in text:
            return '试玩时，请看箱子第一次靠近目标的那一推：玩家能不能马上判断该先处理哪只箱子、从哪一侧接近目标？如果仍能不经思考地一路推到底，说明目标附近还需要补出一个会影响顺序的局部选择。'
        if '墙' in text and any((word in text for word in ('通道', '路线', '转折'))):
            return '试玩时，请看箱子第一次进入这段墙边通道：玩家会不会在入口停下来确认下一步该往哪边走？如果只是多走几步却没有新的判断，说明墙的位置只增加了距离，没有改变路线。'
        if '目标' in text and '箱' in text and any((word in text for word in ('下方', '下半', '底部', '右下', '空旷'))):
            return '我想先陪你看下方箱子第一次朝新目标推进时，玩家会不会因为绕路自然停一下想一想。这个瞬间已经有意思，我们就保留大结构；如果还是太直白，再一起改路线。'
        if '水' in text and '箱' in text:
            return '我想先陪你看箱子第一次贴着水边推进时，会不会自然感到路线得重新想一下。这个小停顿能告诉我们，水域真的参与了判断，还是只是摆在旁边。'
        if '目标' in text and any((word in text for word in ('移动', '挪', '落位', '落点'))):
            return '我想先看看箱子第一次靠近新目标时，玩家会不会自然读到新的推进顺序。'
        if '墙' in text and any((word in text for word in ('通道', '路线', '绕', '转折'))):
            return '我想先陪你看箱子第一次走进这段墙边通道时，会不会自然停下来重新想路线。这能告诉我们，墙是真的让路线更有意思，还是只是多走了几步。'
        return None
    if 'target' in lowered and any((word in lowered for word in ('box', 'crate'))) and any((word in lowered for word in ('lower', 'bottom', 'open area'))):
        return 'When the lower box first moves toward the new target, which step best reveals the new detour burden and the rhythm difference between the upper and lower areas? I would use that moment to judge whether to tune the route or preserve the space.'
    if any((word in lowered for word in ('water', 'pond'))) and any((word in lowered for word in ('box', 'crate'))):
        return 'When the box first moves beside the water, which turn should make the player realize that it is changing the route? I would use that moment to judge whether the linkage is actually working.'
    if 'target' in lowered and any((word in lowered for word in ('move', 'shift', 'position'))):
        return 'When the box first approaches the adjusted target, which move should make the new push order readable?'
    if 'wall' in lowered and any((word in lowered for word in ('corridor', 'route', 'turn'))):
        return 'When the box first passes the adjusted wall corridor, I would watch where the player pauses to reread the route.'
    return None

def _discussion_focus_is_grounded_in_reply(focus, visible_content, language):
    value = str(focus or '').strip()
    content = str(visible_content or '').strip()
    if not value or not content:
        return False
    chinese = language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', f'{value}{content}')
    if chinese:
        components = ('水', '墙', '箱', '目标', '玩家', '通道', '路线', '开局', '区域')
        moments = ('第一次', '推', '绕', '入口', '进入', '靠近', '顺序', '停', '选择', '往哪', '哪边', '判断', '处理', '放下')
        minimum_length = 16
    else:
        components = ('water', 'wall', 'box', 'crate', 'target', 'goal', 'player', 'corridor', 'route', 'opening', 'area')
        moments = ('first', 'push', 'turn', 'approach', 'order', 'pause', 'choose', 'which', 'where')
        minimum_length = 36
    lowered_value = value.casefold()
    lowered_content = content.casefold()
    shared_components = [component for component in components if component in lowered_value and component in lowered_content]
    return len(value) >= minimum_length and bool(shared_components) and any((moment in lowered_value for moment in moments))

def _perspective_discussion_focus(visible_content, language):
    """Distill a concrete judgment or uncertainty already present in the reply."""
    sentences = [re.sub('\\s+', ' ', sentence).strip() for sentence in re.split('(?<=[.!?。！？])\\s*|[\\r\\n]+', str(visible_content or '')) if sentence.strip()]
    chinese = language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', str(visible_content or ''))
    if chinese:
        uncertainty = re.compile('(?:我唯一有点拿不准|我有点拿不准|我不太确定|我还不确定|我担心|不清楚|难以判断)')
        anchors = ('水', '墙', '箱', '目标', 'T1', 'T2', '通道', '路线', '入口', '推')
        for index, sentence in enumerate(sentences):
            if not uncertainty.search(sentence) or not any((anchor in sentence for anchor in anchors)):
                continue
            related = [sentence.rstrip('。！!？?')]
            if index + 1 < len(sentences) and any((anchor in sentences[index + 1] for anchor in anchors)):
                related.append(sentences[index + 1].rstrip('。！!？?'))
            claim = '。'.join(related)
            return f'我想先把这点看清：{claim}。试玩时，重点看它是否真的让玩家改变推箱顺序；这个结果会决定下一步该保留当前入口，还是继续调整附近的局部空间。'[:1000]
        marker = re.compile('(?:我(?:更)?倾向于认为|我倾向于觉得|在我看来|我觉得|我感觉|我(?:比较|更)?在意的是|我(?:更)?喜欢的是|我(?:更)?喜欢)')
        anchors = ('水', '墙', '箱', '目标', '通道', '路线', '开局', '推')
        for sentence in sentences:
            match = marker.search(sentence)
            if not match or not any((anchor in sentence for anchor in anchors)):
                continue
            claim = sentence[match.end():].strip(' ：:，,').rstrip('。！!？?')
            if len(claim) < 12:
                continue
            return f'我会先把这版理解为：{claim}。试玩时，重点看玩家第一次把箱子推到相关位置时，会不会因为这个变化停下来重新判断顺序；这能验证上面的取舍是否真的落在操作上。'[:1000]
        for sentence in sentences:
            if not any((anchor in str(visible_content or '') for anchor in anchors)) or '还是' not in sentence or (not any((marker in sentence for marker in ('这个选择', '这次改动', '这个改动', '这会', '取舍')))):
                continue
            claim = sentence.rstrip('。！!？?')
            return f'我会先把这版的重点放在这个取舍上：{claim}。试玩时，重点看玩家第一次把箱子推到调整区域附近时，会不会因为水域、墙缝或目标位置停下来重新判断顺序；这能看出它是在形成有效的中段判断，还是只增加了绕路。'[:1000]
        return None
    marker = re.compile('\\b(?:I tend to think|I am inclined to think|I think|I feel|in my view)\\b', re.IGNORECASE)
    anchors = ('water', 'wall', 'box', 'crate', 'target', 'goal', 'corridor', 'route', 'opening', 'push')
    uncertainty = re.compile("\\b(?:i am not sure|i'm not sure|i am uncertain|i worry|i am concerned|unclear|hard to tell)\\b", re.IGNORECASE)
    for sentence in sentences:
        if not uncertainty.search(sentence) or not any((anchor in sentence.casefold() for anchor in anchors)):
            continue
        claim = sentence.rstrip('.!?')
        return f'I want to keep this uncertainty in view: {claim}. During play, watch whether it actually changes the push order; that tells us whether to preserve the current opening or adjust the nearby space.'[:1000]
    for sentence in sentences:
        match = marker.search(sentence)
        if not match or not any((anchor in sentence.casefold() for anchor in anchors)):
            continue
        claim = sentence[match.end():].strip(' :,').rstrip('.!?')
        if len(claim) < 18:
            continue
        return f'My reading of this version is: {claim}. During play, watch whether the first push at that changed area makes the player stop and reconsider the order; that will show whether this trade-off is actually present in play.'[:1000]
    for sentence in sentences:
        lowered_sentence = sentence.casefold()
        if not any((anchor in lowered_sentence for anchor in anchors)) or not any((marker in lowered_sentence for marker in ('this trade-off', 'this choice', 'this change'))) or ' or ' not in lowered_sentence:
            continue
        claim = sentence.rstrip('.!?')
        return f'I would keep the focus on this trade-off: {claim}. During play, watch whether the first push near the changed area makes the player reconsider the order; that will show whether it creates a meaningful mid-game judgment rather than only a detour.'[:1000]
    return None

def _reply_needs_clarifying_question(visible_content, language):
    text = str(visible_content or '').casefold()
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', text):
        uncertainty_markers = ('不清楚', '不确定', '看不出', '难以判断', '需要确认', '还不够明确', '会不会', '是否', '我担心', '拿不准', '不太确定')
    else:
        uncertainty_markers = ('unclear', 'uncertain', 'not sure', 'hard to tell', 'need to confirm', 'i worry', 'i am concerned', 'whether', 'not yet clear')
    return any((marker in text for marker in uncertainty_markers))

def _refine_discussion_focus(focus, visible_content, language):
    value = re.sub('\\s+', ' ', str(focus or '')).strip()
    if not value:
        return value
    perspective_focus = _perspective_discussion_focus(visible_content, language)
    if perspective_focus:
        return perspective_focus
    question_marks = value.count('?') + value.count('？')
    if question_marks and (not _reply_needs_clarifying_question(visible_content, language)):
        return None
    if _discussion_focus_is_grounded_in_reply(value, visible_content, language):
        return value[:1000]
    return None

def _extract_plain_discussion_focus(content, language, stage_opening=False, stage_context=None):
    if stage_opening:
        return None
    value = str(content or '')
    marker_index = value.find('<GUIDANCE>')
    closing_index = value.find('</GUIDANCE>', marker_index + 10)
    if marker_index < 0 or closing_index < 0:
        return None
    focus = None
    block = value[marker_index + len('<GUIDANCE>'):closing_index]
    for raw_line in re.split('\\s*\\|\\|\\s*|[\\r\\n]+', block):
        match = re.match('^DISCUSS\\s*:\\s*(.+?)\\s*$', raw_line.strip(), re.IGNORECASE)
        if match:
            focus = _normalize_single_level_language(match.group(1).strip())[:1000]
            break
    if not focus:
        return None
    question_marks = focus.count('?') + focus.count('？')
    useful = _question_is_specific_and_vivid(focus, language) if question_marks == 1 else question_marks == 0 and _discussion_insight_is_useful(focus, language)
    visible = value[:marker_index].strip()
    if not useful or _discussion_focus_repeats_recent(focus, stage_context) or _guidance_reuses_visible_sentence(focus, visible):
        return None
    return focus

def _discussion_insight_is_useful(text, language='en'):
    value = str(text or '').strip()
    if not 18 <= len(value) <= 320 or '?' in value or '？' in value:
        return False
    lowered = value.casefold()
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', value):
        first_person = any((marker in value for marker in ('我', '在我看来', '对我来说')))
        anchors = ('水', '箱', '目标', '墙', '通道', '路线', '推动', '落点', '转折', '玩家')
        judgments = ('选择', '顺序', '读', '判断', '节奏', '犹豫', '取舍', '注意', '自由', '压力')
    else:
        first_person = bool(re.search('\\b(?:i|my|me)\\b', lowered)) or 'to me' in lowered
        anchors = ('water', 'box', 'crate', 'target', 'wall', 'corridor', 'route', 'push', 'landing', 'turn', 'player')
        judgments = ('choice', 'order', 'read', 'judg', 'rhythm', 'hesitat', 'trade-off', 'notice', 'freedom', 'pressure')
    return first_person and any((anchor in lowered for anchor in anchors)) and any((judgment in lowered for judgment in judgments))

def _extract_plain_guidance(content, language, stage_context, stage_opening=False, rows=None, strict_metadata=True):
    marker = '<GUIDANCE>'
    marker_index = content.find(marker)
    if marker_index < 0:
        return (content.strip(), None, None, [])
    visible = content[:marker_index].strip()
    if not visible:
        raise ValueError('The natural-language reply must contain visible text.')
    if stage_opening:
        return (visible, None, None, [])
    block_tail = content[marker_index + len(marker):]
    closing_index = block_tail.find('</GUIDANCE>')
    if closing_index < 0:
        return (visible, None, None, [])
    fields = {}
    for raw_line in re.split('\\s*\\|\\|\\s*|[\\r\\n]+', block_tail[:closing_index]):
        match = re.match('^(WARNING|MANUAL_EDIT|CLARIFICATION|INTENT|INTENT_DECISION|PROPOSAL_SUMMARY|PROPOSAL_RATIONALE|EXECUTION_BRIEF|REVISION_PLAN)\\s*:\\s*(.+?)\\s*$', raw_line.strip())
        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2).strip()
    intent = fields.get('INTENT')
    if intent:
        intent = _normalize_intent_hypothesis(intent[:1000], language)
        if _intent_is_only_execution_authorization(intent, language):
            intent = None
    summary = fields.get('PROPOSAL_SUMMARY')
    rationale = fields.get('PROPOSAL_RATIONALE')
    execution_brief = None
    revision_plan = None
    proposal_metadata_invalid = False
    if fields.get('REVISION_PLAN'):
        try:
            revision_plan = _validate_revision_plan_payload(json.loads(fields['REVISION_PLAN']), rows)
        except (TypeError, ValueError, json.JSONDecodeError) as exception:
            if rows is not None and strict_metadata:
                raise ValueError(f'revisionPlan is invalid for the saved Stage: {exception}') from exception
            proposal_metadata_invalid = True
            revision_plan = None
    if fields.get('EXECUTION_BRIEF'):
        try:
            execution_brief = _validate_execution_brief(json.loads(fields['EXECUTION_BRIEF']), rows)
        except (TypeError, ValueError, json.JSONDecodeError) as exception:
            if rows is not None and strict_metadata:
                raise ValueError(f'executionBrief is invalid for the saved Stage: {exception}') from exception
            proposal_metadata_invalid = True
            execution_brief = None
    if revision_plan is not None:
        execution_brief = revision_plan
    proposal_offer = {'summary': summary[:600], 'rationale': rationale[:1000], **({'executionBrief': execution_brief} if execution_brief else {})} if summary and rationale and (not proposal_metadata_invalid) else None
    recent_guidance = (stage_context or {}).get('recentGuidance') or {}
    evidence_signature = (stage_context or {}).get('guidanceEvidenceSignature')
    if intent and _guidance_text_matches(intent, recent_guidance.get('intentHypothesis')):
        intent = None
    previous_offer = recent_guidance.get('proposalOffer') or {}
    if proposal_offer and _guidance_text_matches(f"{proposal_offer['summary']} {proposal_offer['rationale']}", f"{previous_offer.get('summary', '')} {previous_offer.get('rationale', '')}"):
        proposal_offer = None
    ui_cues = []
    previous_cues = recent_guidance.get('uiCues') or {}
    for field_name, cue_type in (('WARNING', 'warning'), ('MANUAL_EDIT', 'manual_edit'), ('CLARIFICATION', 'clarification')):
        cue_text = str(fields.get(field_name) or '').strip()[:1000]
        previous = previous_cues.get(cue_type) or {}
        if not cue_text:
            continue
        if cue_type == 'warning' and (not _warning_text_is_evidence_grounded(cue_text, language)):
            continue
        if cue_type == 'manual_edit' and (not _manual_edit_text_is_contextual(cue_text, language)):
            continue
        if _guidance_text_matches(cue_text, previous.get('text')) and previous.get('evidenceSignature') == evidence_signature:
            continue
        ui_cues.append({'type': cue_type, 'text': cue_text})
    return (visible, intent, proposal_offer, ui_cues[:2])

def _extract_plain_coordinate_links(content, rows=None):
    """Parse optional route annotations without changing visible plain-text replies."""
    marker = '<GUIDANCE>'
    marker_index = str(content or '').find(marker)
    if marker_index < 0:
        return []
    block_tail = str(content)[marker_index + len(marker):]
    closing_index = block_tail.find('</GUIDANCE>')
    if closing_index < 0:
        return []
    for raw_line in re.split('\\s*\\|\\|\\s*|[\\r\\n]+', block_tail[:closing_index]):
        match = re.match('^COORDINATE_LINKS\\s*:\\s*(.+?)\\s*$', raw_line.strip(), flags=re.IGNORECASE)
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return _normalize_coordinate_links(value, rows=rows)
    return []

def _extract_plain_design_context_patch(content):
    """Read the optional internal patch from the plain-text metadata line."""
    marker = re.search('DESIGN_CONTEXT_PATCH\\s*:\\s*(\\{.*?\\})(?:\\s*\\|\\||\\s*</GUIDANCE>)', str(content or ''), flags=re.IGNORECASE | re.DOTALL)
    if marker is None:
        return (None, None)
    try:
        return (validate_design_context_patch(json.loads(marker.group(1))), None)
    except (TypeError, ValueError, json.JSONDecodeError) as exception:
        return (None, str(exception)[:500])

def _extract_plain_disagreement(content, language, stage_context=None):
    """Parse the optional structured disagreement from the legacy plain-text path."""
    marker = re.search('DISAGREEMENT\\s*:\\s*(\\{.*?\\})(?:\\s*\\|\\||\\s*</GUIDANCE>)', str(content or ''), flags=re.IGNORECASE | re.DOTALL)
    if marker is None:
        return None
    try:
        value = json.loads(marker.group(1))
    except (TypeError, json.JSONDecodeError):
        return None
    try:
        return _validate_disagreement(value, language)
    except ValueError:
        return None

def _recent_discussion_focuses(stage_context):
    recent_guidance = (stage_context or {}).get('recentGuidance') or {}
    history = recent_guidance.get('discussionFocusHistory') or []
    if not isinstance(history, (list, tuple)):
        history = []
    values = [str(item).strip() for item in history if str(item or '').strip()]
    latest = str(recent_guidance.get('discussionFocus') or '').strip()
    if latest and (not any((_guidance_text_matches(latest, item) for item in values))):
        values.insert(0, latest)
    return values[:3]

def _discussion_focus_repeats_recent(focus, stage_context):
    return any((_guidance_text_matches(focus, previous) for previous in _recent_discussion_focuses(stage_context)))

def _route_reasoning_profile(value):
    """Describe route density without treating it as a response-validity error."""
    text = str(value or '')
    route_sentence_count = 0
    route_coordinate_count = 0
    overlong = False
    for paragraph in re.split('\\n\\s*\\n', text):
        paragraph_profile = _route_sentence_profile(paragraph)
        route_coordinate_count += paragraph_profile['coordinateCount']
        route_sentence_count += sum((1 for sentence in re.split('(?<=[.!?\\u3002\\uff01\\uff1f])\\s*|[\\r\\n]+', paragraph) if _route_sentence_profile(sentence)['routeLike']))
        if paragraph_profile['coordinateCount'] > 6 and (paragraph_profile['movementCount'] >= 2 or paragraph_profile['arrowCount'] >= 2):
            overlong = True
        if paragraph_profile['arrowCount'] >= 5:
            overlong = True
    return {'overlong': overlong, 'routeSentenceCount': route_sentence_count, 'routeCoordinateCount': route_coordinate_count}

def _route_reasoning_is_overlong(value):
    """Compatibility predicate for tests and diagnostics.

    Callers that process a response should use _recover_overlong_route_reasoning
    instead of turning this quality signal into a validation exception.
    """
    return _route_reasoning_profile(value)['overlong']

def _recover_overlong_route_reasoning(value, rows=None, *, language='en'):
    """Salvage design prose when route tracing exceeds the visible-route budget.

    Route verbosity is a presentation concern, not a safety failure. Keep ordinary
    design sentences, keep one concise route sentence when it can be identified, and
    discard only later route-trace sentences. Formal proposal validation does not use
    this helper.
    """
    original = str(value or '').strip()
    profile = _route_reasoning_profile(original)
    result = {'changed': False, 'salvageAction': 'none', 'routeSentenceCount': profile['routeSentenceCount'], 'routeCoordinateCount': profile['routeCoordinateCount'], 'droppedSentenceCount': 0, 'coordinateLinksDropped': False}
    if not original or not rows or (not profile['overlong']):
        return (original, result)
    kept_paragraphs = []
    route_kept = 0
    for raw_paragraph in re.split('\\n\\s*\\n', original):
        paragraph = raw_paragraph.strip()
        if not paragraph:
            continue
        kept_sentences = []
        sentences = [sentence.strip() for sentence in re.split('(?<=[.!?\\u3002\\uff01\\uff1f])\\s*|[\\r\\n]+', paragraph) if sentence.strip()]
        for sentence in sentences:
            sentence_profile = _route_sentence_profile(sentence)
            if not sentence_profile['routeLike']:
                kept_sentences.append(sentence)
                continue
            if route_kept >= ROUTE_REASONING_PASSAGE_LIMIT:
                result['droppedSentenceCount'] += 1
                continue
            candidate = sentence
            if sentence_profile['coordinateCount'] > 6 or sentence_profile['arrowCount'] >= 5:
                candidate = _compact_arrow_route_sentence(sentence)
                if not candidate or _route_reasoning_profile(candidate)['overlong']:
                    candidate = None
            if candidate:
                kept_sentences.append(candidate)
                route_kept += 1
            else:
                result['droppedSentenceCount'] += 1
                route_kept += 1
        if kept_sentences:
            kept_paragraphs.append(' '.join(kept_sentences))
    recovered = '\n\n'.join(kept_paragraphs).strip()
    result['changed'] = recovered != original
    if result['changed']:
        result['salvageAction'] = 'route_compacted' if result['droppedSentenceCount'] == 0 else 'route_compacted_and_sentences_dropped'
    return (recovered, result)

def _personal_reflection_sentences(value):
    first_person = re.compile("(?:\\b(?:i|i'm|i\\u2019m|my|personally)\\b|\\u6211\\u89c9\\u5f97|\\u6211\\u8ba4\\u4e3a|\\u8ba9\\u6211\\u611f\\u5230|\\u5bf9\\u6211\\u6765\\u8bf4|\\u6211\\u4f1a\\u62c5\\u5fc3|\\u6211\\u559c\\u6b22)", flags=re.IGNORECASE)
    reflection = re.compile('(?:\\b(?:feel|felt|think|read|find|worry|like|prefer|notice)\\b|\\u611f\\u89c9|\\u4f53\\u4f1a|\\u8bfb\\u8d77\\u6765|\\u62c5\\u5fc3|\\u559c\\u6b22|\\u6709\\u8da3|\\u7d27\\u5f20)', flags=re.IGNORECASE)
    return [sentence for sentence in _content_block_sentences(value) if first_person.search(sentence) and reflection.search(sentence)]

def _recover_overlong_personal_reflection(value, *, language='en'):
    """Deduplicate reflections without imposing a quota on distinct views."""
    original = str(value or '').strip()
    reflections = _personal_reflection_sentences(original)
    result = {'changed': False, 'reflectionSentenceCount': len(reflections), 'reflectionDroppedSentenceCount': 0}
    if len(reflections) < 2:
        return (original, result)
    kept_reflections = []
    for sentence in reflections:
        normalized = re.sub('\\W+', '', sentence).casefold()
        if any((SequenceMatcher(None, normalized, re.sub('\\W+', '', item).casefold()).ratio() >= 0.8 for item in kept_reflections)):
            continue
        kept_reflections.append(sentence)
    reflection_set = set(reflections)
    kept_set = set(kept_reflections)
    emitted_reflections = set()
    kept_sentences = []
    for sentence in _content_block_sentences(original):
        if sentence not in reflection_set:
            kept_sentences.append(sentence)
        elif sentence in kept_set and sentence not in emitted_reflections:
            kept_sentences.append(sentence)
            emitted_reflections.add(sentence)
        else:
            result['reflectionDroppedSentenceCount'] += 1
    if not result['reflectionDroppedSentenceCount']:
        return (original, result)
    separator = '' if re.search('[\\u3400-\\u9fff]', original) else ' '
    recovered = separator.join(kept_sentences).strip()
    result['changed'] = recovered != original
    return (recovered, result)

def _recover_overlong_content(value, rows=None, *, language='en'):
    """Apply presentation budgets only to route and reflection content blocks."""
    recovered, route_result = _recover_overlong_route_reasoning(value, rows, language=language)
    recovered, reflection_result = _recover_overlong_personal_reflection(recovered, language=language)
    result = dict(route_result)
    result['changed'] = bool(route_result['changed'] or reflection_result['changed'])
    result['reflectionSentenceCount'] = reflection_result['reflectionSentenceCount']
    result['reflectionDroppedSentenceCount'] = reflection_result['reflectionDroppedSentenceCount']
    if reflection_result['changed'] and result['salvageAction'] == 'none':
        result['salvageAction'] = 'personal_reflection_compacted'
    elif reflection_result['changed']:
        result['salvageAction'] = f"{result['salvageAction']}_and_personal_reflection_compacted"
    return (recovered, result)

def _route_sentence_profile(value):
    """Profile one visible sentence using escaped Unicode route markers."""
    text = str(value or '')
    coordinate_pattern = re.compile('[\\(\\uFF08]\\s*\\d{1,2}\\s*[,\\uFF0C]\\s*\\d{1,2}\\s*[\\)\\uFF09]')
    movement_pattern = re.compile('(?:from|toward|through|via|along|then|next|move(?:s|d)? to|go(?:es)? to|reach(?:es|ed)?|push(?:es|ed)?|\\u4ECE|\\u5230|\\u524D\\u5F80|\\u7ECF\\u8FC7|\\u6CBF\\u7740|\\u8D70\\u5230|\\u63A8\\u5230|\\u79FB\\u52A8\\u5230|\\u5148.*\\u518D)', flags=re.IGNORECASE)
    coordinates = coordinate_pattern.findall(text)
    movement_matches = movement_pattern.findall(text)
    arrow_count = text.count('→') + text.count('->')
    return {'coordinateCount': len(coordinates), 'movementCount': len(movement_matches), 'arrowCount': arrow_count, 'routeLike': len(coordinates) >= 2 and (bool(movement_matches) or arrow_count > 0)}

def _compact_arrow_route_sentence(sentence):
    """Collapse a long explicit arrow chain while preserving its endpoints."""
    coordinate = '[\\(\\uFF08]\\s*\\d{1,2}\\s*[,\\uFF0C]\\s*\\d{1,2}\\s*[\\)\\uFF09]'
    chain_pattern = re.compile(f'(?P<chain>{coordinate}(?:\\s*(?:\\u2192|->)\\s*{coordinate}){{4,}})')
    match = chain_pattern.search(str(sentence or ''))
    if match is None:
        return None
    points = re.findall(coordinate, match.group('chain'))
    if len(points) < 2:
        return None
    compact_chain = f'{points[0]} → … → {points[-1]}'
    return (str(sentence)[:match.start()] + compact_chain + str(sentence)[match.end():]).strip()

def _safe_incomplete_chat_reply(language, stage_opening=False, rows=None):
    if rows:
        return _current_service().server_snapshot_fallback_message(rows, language, stage_opening=stage_opening)
    if language == 'zh-CN':
        return '我会继续从当前已确认的设计信息出发。'
    return 'I will continue from the currently confirmed design information.'
    if language == 'zh-CN':
        message = '这次分析没有完整生成，我先不保留半截结论；请再发送一次，我会保留关键设计判断，并把路径推演收束得更清楚。'
    else:
        message = 'This analysis did not finish cleanly, so I will not keep the partial conclusion. Please send it once more; I will preserve the key design judgment and keep the route reasoning tighter.'
    if stage_opening:
        return _ensure_stage_one_orientation(message, rows, language)
    return message

def _server_snapshot_fallback_message(rows, language, *, stage_context=None, stage_opening=False):
    """Produce a useful body from server facts when model prose is unusable."""
    if stage_opening:
        try:
            snapshot = build_stage_snapshot(rows)
            box_count = len(snapshot.get('boxes') or [])
            target_count = len(snapshot.get('targets') or [])
            water_count = len(snapshot.get('waterCells') or [])
        except (TypeError, ValueError, KeyError):
            box_count = target_count = water_count = 0
        if language == 'zh-CN':
            layout = f'这个版本保留了 {box_count} 个箱子和 {target_count} 个目标，' + (f'水域用 {water_count} 个格子收紧了可走空间。' if water_count else '可走空间主要由墙体轮廓和内部转折塑形。') + '我会先把这些区域之间的开口看作布局阅读的重点。'
            play = '对玩家来说，第一次推动不只是靠近某个目标，而是在确认哪条通道值得先投入。一旦箱子经过较窄的转折，后续可站位空间和推动顺序就会互相牵制；这让路线节奏有机会从直接搬运变成先观察、再承诺的过程。'
            reflection = '我个人会把这种布局读成中等偏需要留心的开场：它的难点更可能来自顺序判断，而不是单纯拉长步数。只要首个可读的方向足够清楚，这种张力会显得有意图而不生硬。'
        else:
            layout = f"This version keeps {box_count} box{('es' if box_count != 1 else '')} and {target_count} target{('s' if target_count != 1 else '')}. " + (f'Its {water_count} water tiles tighten the walkable space. ' if water_count else 'Its wall outline and internal turns shape the walkable space. ') + "I would first read the openings between those areas as the layout's main visual structure."
            play = 'The first push is not only about approaching a target; it asks which corridor is worth committing to first. When a box passes through a tighter turn, standing room and push order begin to constrain one another. That can make the route feel like observe-then-commit rather than simple transport.'
            reflection = 'Personally, I read this as a moderately attentive opening: its pressure is more likely to come from ordering judgments than from simply extending the move count. If the first readable direction remains clear, that tension should feel deliberate rather than abrupt.'
        message = f'{layout}\n\n{play}\n\n{reflection}'
        return _ensure_stage_one_orientation(message, rows, language) if _is_stage_one(stage_context) else message
    records = _snapshot_entity_records(rows, stage_context)
    boxes = [item for item in records.values() if item.get('kind') == 'box']
    targets = [item for item in records.values() if item.get('kind') == 'target']
    boxes.sort(key=lambda item: item.get('id') or '')
    targets.sort(key=lambda item: item.get('id') or '')
    facts = []
    if boxes:
        box = boxes[0]
        facts.append((box['id'], box['row'], box['column']))
    if targets:
        target = targets[0]
        facts.append((target['id'], target['row'], target['column']))
    if language == 'zh-CN':
        if facts:
            detail = '、'.join((f'{label}位于第{row}行第{column}列' for label, row, column in facts))
            first = f'我会以当前保存的 Stage 为准继续分析。当前可确认：{detail}。'
        else:
            first = '我会以当前保存的 Stage 为准继续分析。'
        second = '对我来说，这个布局更值得从第一次推箱时的选择开始看：哪个通道先被读到，以及它是否让推动顺序变得清楚。'
    else:
        if facts:
            detail = ', '.join((f'{label} at row {row}, column {column}' for label, row, column in facts))
            first = f'I will continue from the current saved Stage. The verified facts include {detail}.'
        else:
            first = 'I will continue from the current saved Stage.'
        second = 'Personally, I would begin with the first push choice: which corridor is legible first, and whether that makes the push order feel deliberate rather than arbitrary.'
    message = f'{first}\n\n{second}'
    return _ensure_stage_one_orientation(message, rows, language) if stage_opening else message

def _stage_opening_safe_execution(*, language, rows, request_id, attempts_used, started_at, fallback_reason, stage_context=None, solver_metrics=None):
    """Return a complete opening without spending another upstream request."""
    svc = _current_service()
    fallback_message = _current_service().server_snapshot_fallback_message(rows, language, stage_opening=True, stage_context=stage_context)
    _log_llm_event('llm_fallback_returned', requestId=request_id, task='stage_assessment_fallback', fallbackReason=fallback_reason, finalDisplayMode='server_snapshot', bodyPreserved=bool(fallback_message.strip()), droppedSentenceCount=0, remainingSeconds=round(svc.remaining_until(_request_deadline(started_at)), 3))
    return LLMExecutionResult(assistant_message=fallback_message, assessment=_build_minimal_stage_assessment(fallback_message, None, language, solver_metrics or {}), proposed_rows=None, modification_summary='', attempts_used=attempts_used, request_id=request_id, model='kimi-k2.6-safe-opening', latency_ms=int((svc.clock.monotonic() - started_at) * 1000), guidance={'move': 'observe_stage', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': [], 'coordinateLinks': []})

def _safe_grounding_chat_reply(language, rows=None, stage_context=None):
    if _has_proposal_clarification(stage_context):
        return _proposal_clarification_fallback_message(language, stage_context)
    if rows:
        return _current_service().server_snapshot_fallback_message(rows, language, stage_context=stage_context)
    if language == 'zh-CN':
        return '我会以当前保存的地图为准继续分析；刚才回复中的一处具体地图事实没有被可靠确认，所以我不会把未经验证的坐标当成结论。我们仍然可以继续讨论空间分布、节奏和引导效果。'
    return 'I understand that you are discussing the design feel of the current layout. One concrete map fact in the previous reply could not be verified, so I will not treat an unverified coordinate as fact; we can still continue from the layout, rhythm, and guidance effect.'

def _ensure_stage_one_orientation(message, rows, language):
    text = str(message or '').strip()
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', text):
        process_markers = ('试玩', '编辑器', '右侧', '局部调整', '局部编辑', '第一反应', '直觉说起', '小调整', '小改', '小范围', '大改', '大幅', '可审查', '梳理思路', '亲手试')
        map_markers = ('水', '墙', '箱', '目标', '玩家', '路线', '通道', '区域', '格')
        compact_guidance = '可以先聊聊或试玩，再一起商量；你也可以在右侧局部编辑。我能帮你梳理思路、提出可审查的小改动；大改由你掌舵，我当参谋。'
    else:
        process_markers = ('play the stage', 'right panel', 'editor', 'local edit', 'first impression', 'small edit', 'reviewable edit', 'broad rebuild', 'think through')
        map_markers = ('water', 'wall', 'box', 'target', 'player', 'route', 'corridor', 'area', 'tile')
        compact_guidance = 'Share a reaction or try playing; you can also make local edits in the right panel. I help think through small, reviewable changes; you steer any broad rebuild, with me as a sounding board.'
    kept = []
    for paragraph in (part.strip() for part in text.split('\n\n')):
        sentences = [sentence.strip() for sentence in re.split('(?<=[.!?。！？])\\s*', paragraph) if sentence.strip()]
        remaining = [sentence for sentence in sentences if not (any((marker in sentence.casefold() for marker in process_markers)) and (not any((marker in sentence.casefold() for marker in map_markers))))]
        if remaining:
            separator = '' if re.search('[\\u3400-\\u9fff]', paragraph) else ' '
            kept.append(separator.join(remaining))
    body = '\n\n'.join(kept).strip()
    if compact_guidance.casefold() in body.casefold():
        return body
    return f'{body}\n\n{compact_guidance}' if body else compact_guidance

def _stage_one_coordinate_summary(rows, language):
    """Render one compact, current-snapshot entity position sentence."""
    if not rows:
        return ''
    try:
        snapshot = build_stage_snapshot(rows)
    except (TypeError, ValueError, KeyError):
        return ''
    entities = {str(item.get('id') or '').upper(): item for item in snapshot.get('entities') or [] if isinstance(item, dict) and item.get('identityConfidence') == 'exact' and item.get('id')}
    ordered = [label for label in ('P', 'B1', 'B2', 'T1', 'T2') if label in entities]
    if not ordered:
        return ''
    if language == 'zh-CN':
        values = [f"{label}位于第{entities[label]['row']}行第{entities[label]['column']}列" for label in ordered]
        return '当前快照中的实体位置是：' + '；'.join(values) + '。'
    values = [f"{label} is at row {entities[label]['row']}, column {entities[label]['column']}" for label in ordered]
    return 'Verified current entity positions: ' + '; '.join(values) + '.'

def _repair_stage_one_opening_display(message, rows, language):
    """Give legacy Stage 1 openings the current analysis-plus-guidance presentation.

    This is deliberately a read-time repair: it never rewrites the archived
    turn.  The operation guidance still appears exactly once, while old
    coordinate-heavy openings gain the same concise observation/reflection
    rhythm used for new Stage openings.
    """
    oriented = _ensure_stage_one_orientation(message, rows, language)
    paragraphs = [part.strip() for part in oriented.split('\n\n') if part.strip()]
    if not paragraphs:
        return oriented
    is_chinese = language == 'zh-CN' or bool(re.search('[\\u3400-\\u9fff]', oriented))
    guidance_markers = ('小范围更改', '可审查', '亲手试') if is_chinese else ('small, reviewable', 'broad rebuild', 'designer-led')
    guidance_paragraphs = [item for item in paragraphs if any((marker.casefold() in item.casefold() for marker in guidance_markers))]
    body = ' '.join((item for item in paragraphs if item not in guidance_paragraphs)).strip()
    if not body:
        body = ''
    coordinate_summary = _stage_one_coordinate_summary(rows, language)
    if coordinate_summary:
        try:
            coordinate_snapshot = build_stage_snapshot(rows)
            coordinate_labels = [str(item.get('id')).upper() for item in coordinate_snapshot.get('entities') or [] if isinstance(item, dict) and item.get('identityConfidence') == 'exact' and item.get('id')]
        except (TypeError, ValueError, KeyError):
            coordinate_labels = []
        entity_position = re.compile('(?<![A-Za-z0-9])(?:P|B\\d+|T\\d+)(?![A-Za-z0-9]).{0,36}?(?:\\u4f4d\\u4e8e|\\u5728(?:\\u7b2c)?|\\u5750\\u843d\\u4e8e|is\\s+(?:at|located\\s+at)|sits\\s+at)', flags=re.IGNORECASE)
        future = re.compile('(?:\\u5c06|\\u4f1a|\\u5e0c\\u671b|\\u60f3(?:\\u8981)?|\\u63a8\\u5230|\\u79fb\\u52a8|\\b(?:will|would|move|push|toward|from)\\b)', flags=re.IGNORECASE)
        complete_inventory = all((re.search(f'(?<![A-Za-z0-9]){label}(?![A-Za-z0-9]).{{0,48}}(?:\\u4f4d\\u4e8e|\\u5728|\\u5750\\u843d\\u4e8e|is\\s+(?:at|located\\s+at)).{{0,32}}(?:\\(\\s*\\d+\\s*[,，]\\s*\\d+\\s*\\)|\\u7b2c\\s*\\d+\\s*\\u884c\\s*\\u7b2c\\s*\\d+\\s*\\u5217)', body, flags=re.IGNORECASE) for label in coordinate_labels))
        cleaned_sentences = []
        for sentence in re.split('(?<=[.!?\\u3002\\uff01\\uff1f])\\s*|[\\r\\n]+', body):
            sentence = sentence.strip()
            if not sentence:
                continue
            if entity_position.search(sentence) and (not future.search(sentence)):
                malformed = re.search('(?:\\u4f4d\\u4e8e|\\u5728|\\u5750\\u843d\\u4e8e|is\\s+(?:at|located))\\s*(?:[，,、]|\\u548c\\s*[，,。]|$)', sentence, flags=re.IGNORECASE)
                if malformed or not complete_inventory:
                    continue
            cleaned_sentences.append(sentence)
        body_parts = [' '.join(cleaned_sentences)]
        if not complete_inventory:
            body_parts.insert(0, coordinate_summary)
        body = '\n\n'.join((part for part in body_parts if part)).strip()
    sentences = [item.strip() for item in re.split('(?<=[.!?\\u3002\\uff01\\uff1f])\\s*', body) if item.strip()]
    reflections = ['我会先把这种分隔带来的首步选择，当作关卡节奏是否清晰的信号。', '在我看来，先观察玩家如何读懂空间，比立即判断要改哪里更有价值。'] if is_chinese else ['I would first treat that split as a signal of whether the opening rhythm reads clearly.', 'For me, watching how a player reads the space matters before deciding what needs to change.']
    for reflection in reflections:
        if len(sentences) >= 4:
            break
        sentences.append(reflection)
    body = ' '.join(sentences[:5]).strip()
    body = _format_stage_opening_paragraphs(body) if body else ''
    guidance = ' '.join(guidance_paragraphs).strip()
    if not guidance:
        return body
    if not body:
        return guidance
    return f'{body}\n\n{guidance}'

def _build_minimal_stage_assessment(message, question, language, solver_metrics):
    solver_metrics = solver_metrics or {}
    steps = solver_metrics.get('solutionSteps')
    pushes = solver_metrics.get('solutionPushes')
    if language == 'zh-CN':
        if isinstance(steps, int) and isinstance(pushes, int):
            solution_summary = f'确定性求解器已验证可解：{steps} 步，其中 {pushes} 次推动。'
        else:
            solution_summary = '这个已保存的 Stage 已通过确定性检查并确认可解。'
        difficulty_opinion = '在我看来，仅凭求解器验证还不足以确定实际游玩难度。'
        features = ['已验证可解的当前 Stage']
        suggestions = ['结合设计者反馈或实际游玩证据继续判断设计效果']
    else:
        if isinstance(steps, int) and isinstance(pushes, int):
            solution_summary = f'The deterministic solver verified a solution in {steps} steps with {pushes} pushes.'
        else:
            solution_summary = 'This saved Stage passed deterministic validation and is solvable.'
        difficulty_opinion = 'In my view, solver validation alone is not enough to determine the experienced difficulty.'
        features = ['A deterministically verified, solvable current Stage']
        suggestions = ['Use designer feedback or play evidence to judge its design effect']
    return {'solutionSummary': solution_summary, 'difficultyOpinion': difficulty_opinion, 'features': features, 'suggestions': suggestions, 'satisfactionQuestion': question}

def _validate_guidance(payload, assessment_only, language='en', stage_context=None, rows=None):
    if payload is None:
        raise ValueError('guidance is required.')
    if not isinstance(payload, dict):
        raise ValueError('guidance must be an object.')
    required_fields = {'move', 'intentHypothesis', 'intentConfidence', 'followUpQuestion', 'proposalOffer'}
    allowed_fields = required_fields | {'uiCues', 'disagreement', 'designContextPatch', 'coordinateLinks'}
    if not required_fields.issubset(payload) or not set(payload).issubset(allowed_fields):
        raise ValueError('guidance contains unexpected or missing fields.')
    move = payload.get('move')
    if move not in GUIDANCE_MOVES:
        raise ValueError('guidance.move is invalid.')
    intent_hypothesis = _clean_optional_text(payload.get('intentHypothesis'), 'guidance.intentHypothesis')
    intent_suppressed = False
    if intent_hypothesis is not None:
        intent_hypothesis = _normalize_single_level_language(intent_hypothesis)
        intent_hypothesis = _normalize_intent_hypothesis(intent_hypothesis, language)
        if _intent_is_only_execution_authorization(intent_hypothesis, language):
            intent_hypothesis = None
            intent_suppressed = True
    intent_confidence = payload.get('intentConfidence')
    if intent_hypothesis is None:
        if intent_confidence is not None and (not intent_suppressed):
            raise ValueError('intentConfidence requires intentHypothesis.')
        intent_confidence = None
    elif intent_confidence not in INTENT_CONFIDENCE_LEVELS:
        raise ValueError('intentConfidence is invalid.')
    follow_up_question = _clean_optional_text(payload.get('followUpQuestion'), 'guidance.followUpQuestion')
    if follow_up_question is not None:
        follow_up_question = _normalize_single_level_language(follow_up_question)
        question_marks = follow_up_question.count('?') + follow_up_question.count('？')
        if question_marks > 1:
            raise ValueError('followUpQuestion must contain at most one question.')
        if question_marks == 0 and (not _discussion_insight_is_useful(follow_up_question, language)):
            raise ValueError('A declarative followUpQuestion must be a concrete first-person design insight.')
        if assessment_only and question_marks == 1:
            follow_up_question = _normalize_opening_question(follow_up_question)
    proposal_offer = payload.get('proposalOffer')
    if proposal_offer is not None:
        if move != 'offer_revision' or not isinstance(proposal_offer, dict):
            raise ValueError('proposalOffer requires the offer_revision move.')
        if not {'summary', 'rationale'}.issubset(proposal_offer):
            raise ValueError('proposalOffer must contain summary and rationale.')
        if not set(proposal_offer).issubset({'summary', 'rationale', 'executionBrief', 'revisionPlan'}):
            raise ValueError('proposalOffer contains an invalid field.')
        raw_proposal_offer = proposal_offer
        proposal_offer = {'summary': _normalize_single_level_language(_clean_text(proposal_offer.get('summary'), 'proposalOffer.summary')), 'rationale': _normalize_single_level_language(_clean_text(proposal_offer.get('rationale'), 'proposalOffer.rationale'))}
        normalized_brief = None
        if 'revisionPlan' in raw_proposal_offer:
            normalized_brief = _validate_revision_plan_payload(raw_proposal_offer['revisionPlan'], rows, (stage_context or {}).get('entityBindings'))
        if 'executionBrief' in raw_proposal_offer:
            execution_brief = _validate_execution_brief(raw_proposal_offer['executionBrief'], rows, (stage_context or {}).get('entityBindings'))
            if normalized_brief is not None and execution_brief != normalized_brief:
                raise ValueError('proposalOffer executionBrief conflicts with revisionPlan.')
            normalized_brief = execution_brief
        if normalized_brief is not None:
            proposal_offer['executionBrief'] = normalized_brief
        if not _proposal_offer_has_exact_execution_plan(proposal_offer):
            raise ValueError('proposalOffer requires exact requiredTransitions; ask for clarification when the intended cells cannot be resolved from the authoritative map facts.')
    elif move == 'offer_revision':
        raise ValueError('The offer_revision move requires proposalOffer.')
    ui_cues = payload.get('uiCues', [])
    if not isinstance(ui_cues, list) or len(ui_cues) > 2:
        raise ValueError('guidance.uiCues must be an array with at most two items.')
    normalized_cues = []
    seen_cue_types = set()
    for index, cue in enumerate(ui_cues):
        if not isinstance(cue, dict) or set(cue) != {'type', 'text'}:
            raise ValueError('Each guidance.uiCue must contain type and text.')
        cue_type = cue.get('type')
        if cue_type not in UI_CUE_TYPES:
            raise ValueError('guidance.uiCue.type is invalid.')
        if cue_type in seen_cue_types:
            raise ValueError('guidance.uiCues cannot repeat a type.')
        if cue_type in {'warning', 'tradeoff'} and (stage_context or {}).get('discussionCardMode') == 'disagreement_only' and (not _warning_text_is_evidence_grounded(str(cue.get('text') or ''), language)):
            raise ValueError('A new warning must cite a concrete map or play interaction.')
        seen_cue_types.add(cue_type)
        normalized_cues.append({'type': cue_type, 'text': _normalize_single_level_language(_clean_text(cue.get('text'), f'guidance.uiCues[{index}].text'))})
    risk_cue_types = seen_cue_types.intersection({'warning', 'tradeoff'})
    if len(risk_cue_types) > 1:
        raise ValueError('guidance.uiCues can contain only one warning.')
    if move == 'challenge_tradeoff' and (not risk_cue_types):
        raise ValueError('challenge_tradeoff requires a warning uiCue.')
    disagreement = _validate_disagreement(payload.get('disagreement'), language)
    existing_disagreement = (stage_context or {}).get('activeDisagreement') or {}
    if existing_disagreement.get('status') == 'active' and existing_disagreement.get('subject') == 'human_edit' and (disagreement is None or disagreement.get('subject') != 'human_edit' or disagreement.get('status') not in {'active', 'acknowledged'}):
        raise ValueError('An active human_edit disagreement must remain active or become acknowledged.')
    if disagreement and disagreement['status'] in {'active', 'acknowledged'}:
        if proposal_offer is not None:
            raise ValueError('An active or acknowledged disagreement cannot contain a proposalOffer.')
        if move == 'offer_revision':
            raise ValueError('An active or acknowledged disagreement cannot use offer_revision.')
    if disagreement and disagreement['status'] == 'acknowledged':
        active_disagreement = existing_disagreement
        if not (disagreement['subject'] == 'human_edit' and active_disagreement.get('status') == 'active' and (active_disagreement.get('subject') == 'human_edit')):
            raise ValueError('acknowledged is valid only for an existing active human_edit disagreement.')
    if disagreement and disagreement['status'] == 'active':
        if not risk_cue_types and disagreement['subject'] in {'ai_revision', 'human_edit', 'user_request'}:
            pass
    if disagreement and disagreement['status'] == 'resolved' and (disagreement['resolution'] == 'retain_current'):
        if proposal_offer is not None:
            raise ValueError('retain_current cannot contain a proposalOffer.')
    if assessment_only:
        if move != 'observe_stage':
            raise ValueError('A Stage opening must use observe_stage.')
        human_edit_disagreement = disagreement is not None and disagreement['status'] == 'active' and (disagreement['subject'] == 'human_edit')
        human_edit_risk_review = _is_human_edit_stage_opening(assessment_only, stage_context) and bool(risk_cue_types)
        if intent_hypothesis is not None or proposal_offer is not None or (normalized_cues and (not (human_edit_disagreement or human_edit_risk_review))):
            raise ValueError('A Stage opening cannot infer intention or offer a revision.')
        if human_edit_disagreement and (not risk_cue_types):
            raise ValueError('A human-edit disagreement opening requires a warning uiCue.')
    normalized_guidance = {'move': move, 'intentHypothesis': intent_hypothesis, 'intentConfidence': intent_confidence, 'followUpQuestion': follow_up_question, 'proposalOffer': proposal_offer, 'disagreement': disagreement, 'uiCues': normalized_cues, 'coordinateLinks': _normalize_coordinate_links(payload.get('coordinateLinks'), rows=rows)}
    if 'designContextPatch' in payload:
        try:
            normalized_guidance['designContextPatch'] = validate_design_context_patch(payload.get('designContextPatch'))
        except (TypeError, ValueError) as exception:
            normalized_guidance['designContextPatchError'] = str(exception)[:500]
    return normalized_guidance

def _normalize_opening_question(question):
    chinese_anchor = re.search('还是|或者|或是|抑或', question)
    english_anchor = re.search('\\b(?:or|versus|vs\\.?)\\b', question, re.IGNORECASE)
    chinese_yes_no = re.search('吗[？?]\\s*$', question)
    english_yes_no = re.match('^(?:do|does|did|is|are|was|were|would|could|can|will|have|has)\\b', question, re.IGNORECASE)
    if chinese_anchor is None and english_anchor is None and (chinese_yes_no is None) and (english_yes_no is None):
        return question
    if chinese_anchor is not None or chinese_yes_no is not None:
        choice_intro = re.search('[，,](?:你)?(?:是|是否|主要是|更倾向于)', question)
        if choice_intro is not None:
            subject = question[:choice_intro.start()].rstrip('，,。！？? ')
            if subject:
                time_suffix = '' if subject.endswith('时') else '时'
                return f'{subject}{time_suffix}，你最先考虑的是什么？'
    if english_anchor is not None:
        for separator in ('—', '--', ' - '):
            if separator not in question:
                continue
            subject = question.split(separator, 1)[0].strip().rstrip('.?! ')
            if re.match('^(?:what|why|how|where|when|which)\\b', subject, re.IGNORECASE):
                return f'{subject}?'
    raise ValueError('A Stage opening question cannot anchor the designer with choices.')

def _compact_human_edit_opening_inventory(message, language):
    """Prefer design effects over duplicate status and coordinate inventories."""
    sentences = [item.strip() for item in re.split('(?<=[.!?。！？])\\s*', str(message or '')) if item.strip()]
    confirmation = re.compile('(?:已保存|已通过|确认可解|确定性检查|saved|solvable|verified)', flags=re.IGNORECASE)
    coordinate = re.compile('(?:\\(\\s*\\d+\\s*[,，]\\s*\\d+\\s*\\)|第\\s*\\d+\\s*行\\s*第\\s*\\d+\\s*列)')
    entity = re.compile('\\b(?:P|B\\d+|T\\d+)\\b', flags=re.IGNORECASE)
    non_inventory = [sentence for sentence in sentences if not (coordinate.search(sentence) or len(entity.findall(sentence)) >= 2)]
    can_drop_inventory = len(non_inventory) >= 3
    kept = []
    seen_confirmation = False
    for sentence in sentences:
        if confirmation.search(sentence):
            if seen_confirmation:
                continue
            seen_confirmation = True
        if can_drop_inventory and (coordinate.search(sentence) or len(entity.findall(sentence)) >= 2):
            continue
        kept.append(sentence)
    return ' '.join(kept).strip()

def _format_stage_opening_paragraphs(message):
    paragraphs = [part.strip() for part in message.split('\n\n') if part.strip()]
    if len(paragraphs) > 3:
        paragraphs = [paragraphs[0], paragraphs[1], ' '.join(paragraphs[2:])]
    if len(paragraphs) == 1:
        sentences = [item.strip() for item in re.split('(?<=[.!?。！？])\\s*', paragraphs[0]) if item.strip()]
        if len(sentences) >= 4:
            midpoint = 2 if len(sentences) <= 5 else len(sentences) // 2
            paragraphs = [' '.join(sentences[:midpoint]), ' '.join(sentences[midpoint:])]
    return '\n\n'.join(paragraphs)

def _compose_assistant_message(message, guidance, language='en', assessment_only=False, stage_context=None):
    stage_context = stage_context or {}
    message = _sanitize_visible_model_text(_deduplicate_assistant_body(message), language)
    if assessment_only and _is_stage_one(stage_context):
        snapshot_rows = (stage_context.get('stageSnapshot') or {}).get('rows')
        message = _repair_stage_one_opening_display(message, snapshot_rows, language)
    change_summary = stage_context.get('changeSummary') or {}
    components = change_summary.get('components') or []
    if assessment_only and stage_context.get('source') == 'human_edit':
        labels = _localized_change_labels(components, language)
        if language == 'zh-CN':
            subject = '、'.join(labels) if labels else '地图布局'
            acknowledgement = f'我注意到你对{subject}进行了修改。这个已保存的 Stage 已通过确定性检查并确认可解。'
        else:
            subject = _join_english(labels) if labels else 'the map layout'
            acknowledgement = f'I noticed that you changed {subject}. This saved Stage passed deterministic validation and is solvable.'
        message = f'{acknowledgement}\n\n{message}'
    ui_cues = guidance.get('uiCues') or []
    additions = [_sanitize_visible_model_text(cue['text'], language) for cue in ui_cues if cue.get('text')]
    follow_up = guidance.get('followUpQuestion')
    follow_up = _sanitize_visible_model_text(follow_up, language)
    proposal_offer = guidance.get('proposalOffer') or {}
    for addition in [*additions, follow_up]:
        message = _remove_guidance_from_body(message, addition)
    for addition in [_sanitize_visible_model_text(proposal_offer.get('summary'), language), _sanitize_visible_model_text(proposal_offer.get('rationale'), language)]:
        message = _remove_exact_guidance_from_body(message, addition)
    if follow_up:
        additions.append(follow_up)
    return _sanitize_visible_model_text('\n\n'.join((part for part in [message, *additions] if part)), language)
