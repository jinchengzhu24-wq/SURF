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
    failure_reason,
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

from .chat_generation import _messages_with_validation_feedback
from .config_and_runtime import CHAT_MAX_ATTEMPTS, CHAT_TIMEOUT_SECONDS, EmptyModelResponse, LLMServiceError, PRIMARY_ATTEMPT_TIMEOUT_SECONDS, TRANSLATION_MAX_TOKENS, TranslationExecutionResult, _current_service, _empty_response_diagnostics, _log_llm_event, _provider_error_fields, _request_completion, _request_component_patch, _request_deadline, _response_diagnostics, _retry_budget_available, _safe_validation_reason, _unified_model_attempts, classify_exception
from .proposal_planning import _llm_failure_class
from .snapshot_grounding import _assert_visible_output_language, _coordinate_link_is_grounded, _normalize_single_level_language, _sanitize_visible_model_text, _strip_invalid_stage_grounding_sentences, _validate_map_grounding_texts


def translate_turns(items, target_language, request_id, *, _deadline=None):
    svc = _current_service()
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    target_name = 'Simplified Chinese' if target_language == 'zh-CN' else 'English'
    prompt_items = [{key: value for key, value in item.items() if key not in {'stageRows', 'entityBindings', 'guidance'}} for item in items]
    messages = [{'role': 'system', 'content': f'Translate the supplied Sokoban co-design UI text into {target_name}. Return one complete JSON object only. Preserve meaning, uncertainty, direct first/second-person voice, paragraph breaks, and question marks. Preserve the source tone and strength of judgment. Naturally rephrase existing metaphor or humor when needed, but do not add jokes, intensify conclusions, or change the relative appeal of options. Cards and status text must remain clear. When a source has proposalQuestion, return that complete object too: translate only question, note and choices[].label, retaining all IDs, letters, counts and status. Treat its question, note and two buttons as one indivisible translation group. When a source includes proposalVerifiedActions, return and translate that entire list in the same order. Do not add advice, questions, claims, or explanations. Keep null values null and keep every turnId unchanged. Translate each uiCueTexts item in the same order. Return exactly {{"translations":[{{"turnId":"...","body":"...","followUpQuestion":null,"intentHypothesis":null,"proposalOfferSummary":null,"proposalOfferRationale":null,"proposalPresentationReason":null,"uiCueTexts":[],"proposalSummary":null,"disagreement":null}}]}}. When the source item has a disagreement object, preserve its status, subject, and resolution, and translate only its userPosition, aiPosition, coreDisagreement, and nextQuestion fields. When a source item includes coordinateLinks, return coordinateLinkTexts in the same order and translate only their text values; EVERY coordinateLinkTexts string MUST be an exact, verbatim substring copied character-for-character from your translated body (matching spacing and punctuation exactly) and preserve the original link endpoints; omit coordinateLinkTexts for items without coordinateLinks.' + (" In Simplified Chinese, use natural Chinese, preserve names and minor natural English alongside the map's visible P/B/T, B1/B2, T1/T2 labels, coordinates, and Stage in Latin characters. Never expose implementation fields. Do not reject or replace names and minor natural English." if target_language == 'zh-CN' else '')}, {'role': 'user', 'content': json.dumps({'items': prompt_items}, ensure_ascii=False)}]
    models = _unified_model_attempts(svc.config.chat_max_attempts)
    started_at = svc.clock.monotonic()
    deadline = _deadline or _request_deadline(started_at)
    _log_llm_event('llm_request_started', requestId=request_id, task='translation', primaryModel=models[0], fallbackModel=None, timeoutSeconds=svc.config.backend_request_timeout_seconds, responseMode='json_object')
    try:
        return asyncio.run(asyncio.wait_for(_current_service().translate_with_model_fallback(api_key=api_key, base_url=base_url, models=models, messages=messages, items=items, target_language=target_language, request_id=request_id, started_at=started_at, deadline=deadline), timeout=min(svc.config.backend_request_timeout_seconds, svc.remaining_until(deadline))))
    except asyncio.TimeoutError as exception:
        elapsed_ms = int((svc.clock.monotonic() - started_at) * 1000)
        _log_llm_event('llm_request_completed', requestId=request_id, task='translation', outcome='error', code='UPSTREAM_TIMEOUT', attemptsUsed=min(len(models), svc.config.chat_max_attempts), latencyMs=elapsed_ms, responseMode='json_object')
        raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not complete the translation before the 120 second limit.', request_id, True, min(len(models), svc.config.chat_max_attempts), 504) from exception

def _validate_translated_proposal_question(group, original, target_language, stage_rows=None, entity_bindings=None):
    if not isinstance(group, dict) or set(group) != set(original):
        raise ValueError('Binary translation must retain the whole question group.')
    if any((group[k] != original[k] for k in original if k not in {'question', 'note', 'choices'})):
        raise ValueError('Binary translation changed question identity or state.')
    group = copy.deepcopy(group)
    for field in ('question', 'note'):
        group[field] = _sanitize_visible_model_text(
            _validate_translated_text(group[field], original[field], 'proposalQuestion.' + field, allow_empty=field == 'note'),
            target_language,
        )
    if not isinstance(group['choices'], list) or len(group['choices']) != 2:
        raise ValueError('Binary translation lost a choice.')
    for before, after in zip(original['choices'], group['choices']):
        if not isinstance(after, dict) or set(before) != set(after) or any((before[k] != after[k] for k in before if k != 'label')):
            raise ValueError('Binary translation changed choice identity.')
        after['label'] = _sanitize_visible_model_text(
            _validate_translated_text(after['label'], before['label'], 'proposalQuestion.label'),
            target_language,
        )
    texts = [group['question'], group['note'], *[c['label'] for c in group['choices']]]
    _assert_visible_output_language(target_language, *texts)
    _validate_map_grounding_texts(texts, stage_rows, entity_bindings=entity_bindings)
    return group

def _translation_patch_request(payload, items, target_language, locked):
    raw = payload.get('translations') if isinstance(payload, dict) else []
    raw_by_id = {item.get('turnId'): item for item in raw or [] if isinstance(item, dict)} if isinstance(raw, list) else {}
    candidate = {'translations': []}
    targets = []
    scalar_fields = ('body', 'followUpQuestion', 'intentHypothesis', 'proposalOfferSummary', 'proposalOfferRationale', 'proposalSummary', 'proposalPresentationReason')
    for index, source in enumerate(items):
        raw_item = raw_by_id.get(source['turnId'], {})
        prefix = f"turn-{source['turnId']}"
        translated = {'turnId': source['turnId']}
        verified = locked.get(source['turnId'], {})

        def restore_text(source_text, value, path, field):
            if source_text is None:
                return None
            try:
                checked = _validate_translated_text(value, source_text, field, allow_empty=field.endswith('.body'))
                if checked is not None:
                    checked = _sanitize_visible_model_text(checked, target_language)
                if source_text and (not checked):
                    raise ValueError('Empty translated body')
                _assert_visible_output_language(target_language, checked or '')
                _validate_map_grounding_texts([checked], source.get('stageRows'), entity_bindings=source.get('entityBindings'))
                return checked
            except (ValueError, TypeError):
                targets.append(Target(field, path, (str,)))
                return value
        if source.get('proposalQuestion'):
            if 'proposalQuestion' in verified:
                translated['proposalQuestion'] = copy.deepcopy(verified['proposalQuestion'])
            else:
                raw_question = raw_item.get('proposalQuestion')
                try:
                    translated['proposalQuestion'] = _validate_translated_proposal_question(
                        raw_question,
                        source['proposalQuestion'],
                        target_language,
                        source.get('stageRows'),
                        source.get('entityBindings'),
                    )
                except (ValueError, TypeError):
                    targets.append(Target(f'{prefix}.proposalQuestion', ('translations', index, 'proposalQuestion'), (dict,)))
                    translated['proposalQuestion'] = raw_question
        for field in scalar_fields:
            if field not in source:
                continue
            translated[field] = verified[field] if field in verified else restore_text(source[field], raw_item.get(field), ('translations', index, field), prefix + '.' + field)
        body_text = translated.get('body') if isinstance(translated.get('body'), str) else ''
        for field, source_texts in (('uiCueTexts', source.get('uiCueTexts') or []), ('coordinateLinkTexts', [link['text'] for link in source.get('coordinateLinks') or []]), *((('proposalVerifiedActions', source['proposalVerifiedActions']),) if 'proposalVerifiedActions' in source else ())):
            if field == 'coordinateLinkTexts' and (not source_texts):
                continue
            raw_texts = raw_item.get(field)
            translated[field] = []
            for text_index, source_text in enumerate(source_texts):
                value = raw_texts[text_index] if isinstance(raw_texts, list) and text_index < len(raw_texts) else None
                if field == 'coordinateLinkTexts':
                    cleaned_link = _sanitize_visible_model_text(value, target_language) if isinstance(value, str) else None
                    source_link = (source.get('coordinateLinks') or [])[text_index]
                    if (
                        not isinstance(cleaned_link, str)
                        or cleaned_link not in body_text
                        or (
                            source.get('stageRows')
                            and isinstance(source_link, dict)
                            and 'from' in source_link
                            and 'to' in source_link
                            and not _coordinate_link_is_grounded(
                                {'text': cleaned_link, 'from': source_link['from'], 'to': source_link['to']},
                                source.get('stageRows'),
                                entity_bindings=source.get('entityBindings'),
                            )
                        )
                    ):
                        value = None
                translated[field].append(restore_text(source_text, value, ('translations', index, field, text_index), f'{prefix}.{field}.{text_index}'))
        if 'disagreement' in source:
            source_disagreement = source.get('disagreement')
            if source_disagreement is None:
                translated['disagreement'] = None
            else:
                translated['disagreement'] = dict(source_disagreement)
                raw_disagreement = raw_item.get('disagreement') or {}
                raw_disagreement = raw_disagreement if isinstance(raw_disagreement, dict) else {}
                for field in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion'):
                    translated['disagreement'][field] = restore_text(source_disagreement.get(field), raw_disagreement.get(field), ('translations', index, 'disagreement', field), f'{prefix}.disagreement.{field}')
        candidate['translations'].append(translated)
    if not targets:
        return (None, candidate)
    evidence = {}
    for index, item in enumerate(items):
        item_evidence = {key: value for key, value in item.items() if key not in {'stageRows', 'entityBindings', 'guidance'}}
        candidate_body = candidate['translations'][index].get('body') if index < len(candidate['translations']) else None
        if isinstance(candidate_body, str) and candidate_body.strip():
            item_evidence['lockedTranslatedBody'] = candidate_body
        evidence[f"turn:{item['turnId']}"] = item_evidence
    return (PatchRequest('translation', candidate, targets, evidence), candidate)

def prompt_translation_sources(items):
    return [{key: item[key] for key in ('turnId', 'body', 'followUpQuestion', 'intentHypothesis', 'proposalOfferSummary', 'proposalOfferRationale', 'proposalSummary', 'proposalPresentationReason', 'uiCueTexts', 'disagreement', 'proposalQuestion', 'proposalVerifiedActions') if key in item} for item in items]

async def _translate_with_model_fallback(*, api_key, base_url, models, messages, items, target_language, request_id, started_at, deadline=None):
    svc = _current_service()
    last_error = None
    validation_feedback = None
    locked_translations = {}
    option_translation = any((item.get('proposalQuestion') or 'proposalVerifiedActions' in item or ((item.get('guidance') or {}).get('proposalDiscovery') or {}).get('optionSet') for item in items))
    pending_option_translation = None
    repair_request = None
    payload = None
    deadline = deadline or _request_deadline(started_at)
    max_attempts = len(models)
    for attempt, model in enumerate(models[:max_attempts], start=1):
        payload = None
        remaining = svc.remaining_until(deadline)
        response_fields = _empty_response_diagnostics()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        attempt_timeout = min(svc.config.primary_attempt_timeout_seconds if attempt == 1 else remaining, remaining)
        _log_llm_event('llm_attempt_started', requestId=request_id, task='translation', model=model, attempt=attempt, maxAttempts=max_attempts, timeoutSeconds=round(attempt_timeout, 3), responseMode='json_object')
        try:
            if pending_option_translation is not None:
                review_messages = [{'role': 'system', 'content': 'Independently verify a translation of proposal directions. Return only {"accepted":true} or {"accepted":false}. Preserve the original option labels, every choice/tradeoff, question, uncertainty and speaker attribution. Reject new advice or claims, omitted choices, and design relocation/addition/removal of boxes, targets, player start or shell. Only water/internal walls are editable; gameplay pushes are distinct. Judge the actual translated text against the source, never map geometry.'}, {'role': 'user', 'content': json.dumps({'sources': prompt_translation_sources(items), 'translations': pending_option_translation}, ensure_ascii=False)}]
                timeout = min(10.0, svc.remaining_until(deadline))
                if timeout <= 0:
                    raise asyncio.TimeoutError()
                reviewed = await asyncio.wait_for(_request_completion(api_key, base_url, model, review_messages, 120, timeout, task='translation_permission_review'), timeout=timeout)
                if not getattr(reviewed, 'choices', None):
                    raise EmptyModelResponse('The model returned an empty response.')
                verdict = parse_json_object(reviewed.choices[0].message.content)
                if str(getattr(reviewed.choices[0], 'finish_reason', '')) == 'length' or set(verdict) != {'accepted'} or verdict['accepted'] is not True:
                    raise ValueError('The proposal translation could not be independently verified.')
                return TranslationExecutionResult(translations=pending_option_translation, attempts_used=attempt, request_id=request_id, model=model, latency_ms=int((svc.clock.monotonic() - started_at) * 1000))
            if option_translation and attempt >= max_attempts:
                raise ValueError('The proposal translation has no remaining verification slot.')
            if repair_request is not None:
                payload = await _request_component_patch(api_key, base_url, repair_request, messages, request_id, deadline, 'translation', TRANSLATION_MAX_TOKENS, attempt_timeout)
            else:
                response = await asyncio.wait_for(_request_completion(api_key, base_url, model, _messages_with_validation_feedback(messages, validation_feedback, 'translation'), TRANSLATION_MAX_TOKENS, attempt_timeout, task='translation'), timeout=attempt_timeout)
                if not getattr(response, 'choices', None):
                    raise EmptyModelResponse('The model returned an empty response.')
                choice = response.choices[0]
                response_fields = _response_diagnostics(response, choice)
                if str(getattr(choice, 'finish_reason', '') or '') == 'length':
                    raise ValueError('The model output reached its token limit.')
                content = str(choice.message.content or '')
                if not content.strip():
                    raise EmptyModelResponse('The model returned an empty response.')
                payload = parse_json_object(content)
            for translated in payload.get('translations') or []:
                if isinstance(translated, dict) and isinstance(translated.get('turnId'), str):
                    translated.update(locked_translations.get(translated['turnId'], {}))
            locked_translations.update(_lock_valid_translation_fields(payload, items, target_language))
            translations = validate_translation_response(payload, items, target_language=target_language)
            if option_translation:
                if attempt >= max_attempts:
                    raise ValueError('The proposal translation has no remaining verification slot.')
                pending_option_translation = translations
                continue
            latency_ms = int((svc.clock.monotonic() - started_at) * 1000)
            _log_llm_event('llm_request_completed', requestId=request_id, task='translation', outcome='success', model=model, attemptsUsed=attempt, latencyMs=latency_ms, responseMode='json_object', **response_fields)
            return TranslationExecutionResult(translations=translations, attempts_used=attempt, request_id=request_id, model=model, latency_ms=latency_ms)
        except asyncio.TimeoutError as exception:
            failure_reason = None
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not respond before the translation attempt timeout.', request_id, True, attempt, 504)
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            last_error = classify_exception(exception, request_id, attempt)
            if last_error.code == 'MODEL_RESPONSE_INVALID' and failure_reason:
                validation_feedback = failure_reason
                if isinstance(payload, dict) and isinstance(payload.get('translations'), list) and payload['translations']:
                    messages = list(messages) + [{'role': 'user', 'content': json.dumps({'lockedTranslationFields': locked_translations, 'instruction': 'Preserve verified fields exactly; return only patches for listed failed translation fields. For any coordinateLinkTexts field, copy an exact verbatim substring from lockedTranslationFields or lockedTranslatedBody in evidence matching the original coordinateLinks endpoints.'}, ensure_ascii=False)}]
                    repair_request, recovered = _translation_patch_request(payload, items, target_language, locked_translations)
                    if repair_request is None and (not option_translation):
                        try:
                            translations = validate_translation_response(recovered, items, target_language)
                            return TranslationExecutionResult(translations=translations, attempts_used=attempt, request_id=request_id, model=model, latency_ms=int((svc.clock.monotonic() - started_at) * 1000))
                        except ValueError:
                            pass
                else:
                    repair_request = None
        failure_fields = {'requestId': request_id, 'task': 'translation', 'model': model, 'attempt': attempt, 'code': last_error.code, 'retryable': last_error.retryable, 'latencyMs': int((svc.clock.monotonic() - started_at) * 1000), 'responseMode': 'json_object', 'failureClass': _llm_failure_class(last_error, failure_reason), **response_fields, **_provider_error_fields(last_error)}
        if failure_reason:
            failure_fields['validationReason'] = failure_reason
        _log_llm_event('llm_attempt_failed', **failure_fields)
        if pending_option_translation is not None:
            raise last_error
        if not last_error.retryable:
            raise last_error
        if attempt < max_attempts and (not _retry_budget_available(deadline, request_id=request_id, task='translation', attempt=attempt, max_attempts=max_attempts, response_mode='json_object', fallback_reason=last_error.code)):
            break
    if last_error is not None:
        failure_class = _llm_failure_class(last_error, validation_feedback)
        _log_llm_event('llm_request_completed', requestId=request_id, task='translation', outcome='error', code=last_error.code, attemptsUsed=last_error.attempts_used, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), responseMode='json_object', failureClass=failure_class, salvageAction='none', remainingSeconds=round(svc.remaining_until(deadline), 3), **_provider_error_fields(last_error))
        raise last_error
    raise LLMServiceError('INTERNAL_ERROR', 'The translation request ended unexpectedly.', request_id, False, 0, 500)

def _lock_valid_translation_fields(payload, source_items, target_language):
    sources = {item['turnId']: item for item in source_items}
    locked = {}
    for translated in payload.get('translations') or []:
        if not isinstance(translated, dict) or not isinstance(translated.get('turnId'), str) or translated['turnId'] not in sources:
            continue
        source = sources[translated['turnId']]
        fields = {}
        for name in ('body', 'followUpQuestion', 'intentHypothesis', 'proposalOfferSummary', 'proposalOfferRationale', 'proposalSummary', 'proposalPresentationReason'):
            if name not in translated or name not in source:
                continue
            try:
                value = _validate_translated_text(translated[name], source[name], name, allow_empty=name == 'body')
                if name == 'body' and source[name] and (not value):
                    continue
                _assert_visible_output_language(target_language, value or '')
                _validate_map_grounding_texts([value], source.get('stageRows'), entity_bindings=source.get('entityBindings'))
                fields[name] = value
            except (ValueError, TypeError):
                continue
        if source.get('proposalQuestion') and 'proposalQuestion' in translated:
            try:
                fields['proposalQuestion'] = _validate_translated_proposal_question(
                    translated['proposalQuestion'],
                    source['proposalQuestion'],
                    target_language,
                    source.get('stageRows'),
                    source.get('entityBindings'),
                )
            except (ValueError, TypeError):
                pass
        locked[translated['turnId']] = fields
    return locked

def validate_translation_response(payload, source_items, target_language='en'):
    if not isinstance(payload, dict) or set(payload) != {'translations'}:
        raise ValueError('Translation output must contain only translations.')
    translations = payload['translations']
    if not isinstance(translations, list) or len(translations) != len(source_items):
        raise ValueError('Translation output must contain one item for every source turn.')
    source_by_id = {item['turnId']: item for item in source_items}
    expected_fields = {'turnId', 'body', 'followUpQuestion', 'intentHypothesis', 'proposalOfferSummary', 'proposalOfferRationale', 'uiCueTexts', 'proposalSummary'}
    translated_by_id = {}
    for index, translation in enumerate(translations):
        if not isinstance(translation, dict):
            raise ValueError(f'Translation item {index} does not match the required fields.')
        turn_id = translation.get('turnId')
        if not isinstance(turn_id, str) or turn_id not in source_by_id or turn_id in translated_by_id:
            raise ValueError('Translation turnIds must match the requested turns exactly.')
        source = source_by_id[turn_id]
        source_links = source.get('coordinateLinks') or []
        item_expected_fields = expected_fields | ({'proposalVerifiedActions'} if 'proposalVerifiedActions' in source else set()) | ({'proposalQuestion'} if source.get('proposalQuestion') else set()) | ({'disagreement'} if 'disagreement' in source else set()) | ({'coordinateLinkTexts'} if source_links else set()) | ({'proposalPresentationReason'} if 'proposalPresentationReason' in source else set())
        if set(translation) != item_expected_fields:
            raise ValueError(f'Translation item {index} does not match the required fields.')
        normalized = {'turnId': turn_id}
        if 'proposalVerifiedActions' in source:
            values = translation.get('proposalVerifiedActions')
            if not isinstance(values, list) or len(values) != len(source['proposalVerifiedActions']):
                raise ValueError('Verified action translations must retain every selection.')
            normalized['proposalVerifiedActions'] = [_validate_translated_text(value, original, 'proposalVerifiedActions') for value, original in zip(values, source['proposalVerifiedActions'])]
            _assert_visible_output_language(target_language, *normalized['proposalVerifiedActions'])
            _validate_map_grounding_texts(normalized['proposalVerifiedActions'], source.get('stageRows'), entity_bindings=source.get('entityBindings'))
        if source.get('proposalQuestion'):
            normalized['proposalQuestion'] = _validate_translated_proposal_question(
                translation.get('proposalQuestion'),
                source['proposalQuestion'],
                target_language,
                source.get('stageRows'),
                source.get('entityBindings'),
            )
        normalized['body'] = _validate_translated_text(translation['body'], source['body'], f'translations[{index}].body', allow_empty=True)
        normalized['body'] = _sanitize_visible_model_text(normalized['body'], target_language)
        normalized['body'], _dropped_grounding = _strip_invalid_stage_grounding_sentences(normalized['body'], source.get('stageRows'), entity_bindings=source.get('entityBindings'))
        if not normalized['body'] and source.get('body'):
            raise ValueError('No reliable translated body remains; preserve the source turn.')
        translated_text_fields = ['followUpQuestion', 'intentHypothesis', 'proposalOfferSummary', 'proposalOfferRationale', 'proposalSummary']
        if 'proposalPresentationReason' in item_expected_fields:
            translated_text_fields.append('proposalPresentationReason')
        for field_name in translated_text_fields:
            normalized[field_name] = _validate_translated_text(translation[field_name], source[field_name], f'translations[{index}].{field_name}')
            normalized[field_name] = _sanitize_visible_model_text(normalized[field_name], target_language)
        source_cues = source['uiCueTexts']
        translated_cues = translation['uiCueTexts']
        if not isinstance(translated_cues, list) or len(translated_cues) != len(source_cues):
            raise ValueError('Translated uiCueTexts must preserve the source item count.')
        normalized['uiCueTexts'] = [_validate_translated_text(translated_text, source_cues[cue_index], f'translations[{index}].uiCueTexts[{cue_index}]') for cue_index, translated_text in enumerate(translated_cues)]
        normalized['uiCueTexts'] = [_sanitize_visible_model_text(text, target_language) for text in normalized['uiCueTexts']]
        if source_links:
            translated_link_texts = translation['coordinateLinkTexts']
            if not isinstance(translated_link_texts, list) or len(translated_link_texts) != len(source_links):
                raise ValueError('Translated coordinateLinkTexts must preserve the source item count.')
            normalized['coordinateLinkTexts'] = [_validate_translated_text(translated_text, source_links[link_index]['text'], f'translations[{index}].coordinateLinkTexts[{link_index}]') for link_index, translated_text in enumerate(translated_link_texts)]
            normalized['coordinateLinkTexts'] = [_sanitize_visible_model_text(text, target_language) for text in normalized['coordinateLinkTexts']]
            if any((link_text not in (normalized['body'] or '') for link_text in normalized['coordinateLinkTexts'])):
                raise ValueError(f'translations[{index}].coordinateLinkTexts must remain exact body substrings.')
            if source.get('stageRows'):
                for link_index, (link_text, source_link) in enumerate(zip(normalized['coordinateLinkTexts'], source_links)):
                    if not isinstance(source_link, dict) or 'from' not in source_link or 'to' not in source_link:
                        continue
                    candidate_link = {'text': link_text, 'from': source_link['from'], 'to': source_link['to']}
                    if not _coordinate_link_is_grounded(candidate_link, source.get('stageRows'), entity_bindings=source.get('entityBindings')):
                        raise ValueError(f'translations[{index}].coordinateLinkTexts[{link_index}] is not grounded in the original link coordinates.')
        if 'disagreement' in item_expected_fields:
            source_disagreement = source.get('disagreement')
            translated_disagreement = translation.get('disagreement')
            if source_disagreement is None:
                if translated_disagreement is not None:
                    raise ValueError(f'translations[{index}].disagreement must remain null.')
            else:
                normalized['disagreement'] = _validate_translated_disagreement(translated_disagreement, source_disagreement, f'translations[{index}].disagreement')
                for field_name in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion'):
                    normalized['disagreement'][field_name] = _sanitize_visible_model_text(normalized['disagreement'][field_name], target_language)
        card_and_link_texts = [normalized.get('followUpQuestion'), normalized.get('intentHypothesis'), normalized.get('proposalOfferSummary'), normalized.get('proposalOfferRationale'), normalized.get('proposalPresentationReason'), normalized.get('proposalSummary'), *(normalized.get('uiCueTexts') or []), *(normalized.get('coordinateLinkTexts') or [])]
        if isinstance(normalized.get('disagreement'), dict):
            card_and_link_texts.extend((normalized['disagreement'].get(field_name) for field_name in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion')))
        if source.get('stageRows'):
            grounding_targets = [text for text in card_and_link_texts if text]
            if grounding_targets:
                _validate_map_grounding_texts(grounding_targets, source.get('stageRows'), entity_bindings=source.get('entityBindings'))
        translation_texts = [normalized.get('body'), *card_and_link_texts]
        _assert_visible_output_language(target_language, *translation_texts)
        translated_by_id[turn_id] = normalized
    if set(translated_by_id) != set(source_by_id):
        raise ValueError('Translation output omitted one or more requested turns.')
    return [translated_by_id[item['turnId']] for item in source_items]

def _validate_translated_disagreement(value, source, field_name):
    if not isinstance(value, dict) or set(value) != set(source):
        raise ValueError(f'{field_name} must preserve the disagreement fields.')
    for field in ('status', 'subject', 'resolution', 'phase', 'displayCard'):
        if value.get(field) != source.get(field):
            raise ValueError(f'{field_name}.{field} must remain unchanged.')
    result = dict(source)
    for field in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion'):
        result[field] = _validate_translated_text(value.get(field), source.get(field), f'{field_name}.{field}')
    return result

def _validate_translated_text(value, source_value, field_name, allow_empty=False):
    if source_value is None:
        if value is not None:
            raise ValueError(f'{field_name} must remain null.')
        return None
    if not isinstance(value, str):
        raise ValueError(f'{field_name} must be a string.')
    normalized = value.strip()
    if not normalized and (not allow_empty):
        raise ValueError(f'{field_name} must not be empty.')
    maximum_length = max(400, len(str(source_value)) * 4 + 200)
    if len(normalized) > maximum_length:
        raise ValueError(f'{field_name} is unexpectedly long.')
    return _normalize_single_level_language(normalized)
