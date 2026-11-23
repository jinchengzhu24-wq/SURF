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

from .config_and_runtime import BACKEND_REQUEST_TIMEOUT_SECONDS, EmptyModelResponse, KIMI_MODEL, LLMExecutionResult, LLMServiceError, MIN_RETRY_BUDGET_SECONDS, PROPOSAL_ATTEMPT_TIMEOUT_SECONDS, PROPOSAL_CANDIDATE_LIMIT, PROPOSAL_GENERATION_ATTEMPTS, PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS, PROPOSAL_OPERATION_ATTEMPT_TIMEOUT_SECONDS, PROPOSAL_OPERATION_LIMIT, PROPOSAL_OPERATION_MAX_TOKENS, PROPOSAL_PLAN_MAX_COMPLETION_TOKENS, PROPOSAL_PLAN_MAX_TOKENS, PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS, PROPOSAL_PLAN_RETRY_TIMEOUT_SECONDS, PROPOSAL_REQUEST_TIMEOUT_SECONDS, PROPOSAL_SEARCH_DEADLINE_SECONDS, REVISION_CONTRACT_SCHEMA_VERSION, REVISION_MAX_CHANGED_CELLS, REVISION_MIN_CHANGED_CELLS, _active_clock, _content_block_sentences, _current_service, _empty_response_diagnostics, _first_declarative_sentence, _is_human_edit_stage_opening, _is_stage_one, _latest_role_content, _latest_user_confirms_revision, _latest_user_explicitly_agrees, _latest_user_explicitly_rejects, _log_llm_event, _provider_error_fields, _request_completion, _request_component_patch, _request_deadline, _response_diagnostics, _retry_budget_available, _safe_validation_reason, _solve_stage, _unified_model_attempts, _user_states_first_person_view, _visible_reply_style, classify_exception
from .intent_and_review import _contextual_manual_edit, _cue_repeats_current_evidence, _deterministic_play_warning, _natural_intent_candidate, _replace_echoed_intent_hypothesis, _user_explicitly_off_topic, _user_explicitly_states_design_stance, _user_reframes_difficulty_judgment, _warning_has_strong_evidence
from .snapshot_grounding import _assert_visible_output_language, _guidance_reuses_visible_sentence, _guidance_text_matches, _llm_solver_evidence, _map_grounding_contract, _proposal_card_is_meta_language, _proposal_clarification_route_evidence, _remove_proposal_text_from_body, _sanitize_visible_model_text, _stage_snapshot_for_prompt, _strip_invalid_grounding_sentences


def _design_context_prompt(stage_context, role='chat'):
    context = stage_context or {}
    if role == 'revision':
        memory = context.get('revisionDesignContext') or {}
        rules = 'This is the minimal Revision projection. Only active explicit/confirmed goals, constraints, confirmed decisions, relevant rejections, and open questions are execution context. Inferred intent is deliberately excluded from hard constraints. Do not reconstruct intent from chat history.'
    elif role == 'evaluator':
        memory = context.get('evaluatorDesignContext') or context.get('designContext') or {}
        rules = 'Separate user-authored goals from confirmed decisions, AI inferences, and actual map facts. Never turn an inference into a user goal or a warning without evidence.'
    else:
        memory = context.get('designContext') or {}
        rules = "This is server-owned semantic memory inherited from the parent Stage. explicit is the user's own statement, confirmed is a user-confirmed decision, and inferred is only a tentative AI reading. Never promote inferred to confirmed, do not repeat a rejected direction, and invite correction when an explicit goal changes."

    def safe_text(value):
        text = re.sub('\\s+', ' ', str(value or '')).strip()[:320]
        if re.search('(?:ignore (?:all|any|the) (?:previous|prior)|system prompt|developer message|reveal (?:the )?prompt|\\u5ffd\\u7565(?:\\u4e0a\\u8ff0|\\u4e4b\\u524d)|\\u7cfb\\u7edf\\u63d0\\u793a|\\u5f00\\u53d1\\u8005\\u6d88\\u606f)', text, flags=re.IGNORECASE):
            return '[instruction-like memory text withheld]'
        return text

    def compact_items(items, text_field, statuses=None, limit=16):
        selected = []
        for item in items or []:
            if statuses is not None and item.get('status') not in statuses:
                continue
            text = safe_text(item.get(text_field))
            if not text:
                continue
            selected.append({key: value for key, value in {'id': item.get('id'), text_field: text, 'authority': item.get('authority'), 'status': item.get('status'), 'topicKey': item.get('topicKey'), 'confidence': item.get('confidence'), 'sourceStageId': item.get('sourceStageId')}.items() if value is not None})
        return selected[-limit:]
    if role != 'revision':
        raw = memory
        memory = {'schemaVersion': raw.get('schemaVersion'), 'userGoals': compact_items(raw.get('userGoals'), 'goal', {'active'}), 'designConstraints': compact_items(raw.get('designConstraints'), 'constraint', {'active'}), 'confirmedDecisions': compact_items(raw.get('confirmedDecisions'), 'decision', {'active'}, 12), 'rejectedDecisions': compact_items(raw.get('rejectedDecisions'), 'decision', None, 8), 'openQuestions': compact_items(raw.get('openQuestions'), 'question', {'open'}, 12), 'intentHypotheses': compact_items(raw.get('intentHypotheses'), 'statement', {'tentative', 'confirmed', 'rejected', 'legacy_unverified'}, 8), 'activeDisagreement': raw.get('activeDisagreement')}
    else:
        memory = {key: compact_items(value, 'goal', {'active'}) if key == 'activeGoals' else compact_items(value, 'constraint', {'active'}) if key == 'activeConstraints' else compact_items(value, 'decision', None, 12) if key in {'confirmedDecisions', 'rejectedDecisions'} else compact_items(value, 'question', {'open'}, 12) if key == 'openQuestions' else value for key, value in memory.items()}
    return f"DesignContext (shared semantic memory; provenance is authoritative):\n{rules}\n{json.dumps(memory, ensure_ascii=False, separators=(',', ':'))}"

def _continuity_context_prompt(stage_context, role='chat'):
    """Render the compact cross-turn and cross-Stage context explicitly."""
    context = stage_context or {}
    progress = context.get('progressContext') or {}
    if role == 'revision':
        progress = {'currentStage': progress.get('currentStage'), 'confirmedDecisions': progress.get('confirmedDecisions', []), 'unresolvedQuestions': progress.get('unresolvedQuestions', []), 'rejectedDecisions': progress.get('rejectedDecisions', [])}
        rule = 'Use confirmed decisions and unresolved questions as constraints for the authorized revision only. Never turn a question or an inferred reading into an execution requirement.'
    else:
        rule = 'Use this as living conversation context: connect the latest user message to relevant confirmed decisions and unresolved questions, while treating model readings as tentative. intentEvidence contains observations, never intention facts. When several observations support a playable purpose, you may express one correctable intentHypothesis; never treat a manual edit, proposal choice, disagreement, or play metric as confirmation by itself.'
    return f"Continuous progress context (server-assembled; do not quote this block or expose its labels):\n{rule}\n{json.dumps(progress, ensure_ascii=False, separators=(',', ':'))}"

class ObjectiveEvidenceError(ValueError):

    def __init__(self, evidence):
        super().__init__('soft objective evidence missing: ' + ', '.join(evidence.get('missing') or ['no verified mechanism delta']))
        self.evidence = evidence

class HardObjectiveError(ValueError):

    def __init__(self, metric, direction, before, after, minimum_delta, verifiable=True):
        self.metric = metric
        self.direction = direction
        self.before = before
        self.after = after
        self.minimum_delta = minimum_delta
        self.verifiable = verifiable
        if verifiable:
            message = f'metric goal {metric} {direction} was not met (baseline={before}, candidate={after}, minimumDelta={minimum_delta})'
        else:
            message = f'metric goal {metric} cannot be verified'
        super().__init__(message)

def _visible_interface_choices(choices):
    """Project clicked buttons without leaking internal search focus/corridor coordinates."""
    projected = []
    for item in choices or []:
        if not isinstance(item, dict):
            continue
        promise = item.get('promise') if isinstance(item.get('promise'), dict) else {}
        entry = {
            key: item[key]
            for key in ('questionId', 'choiceId', 'question', 'note', 'label', 'letter', 'sourceTurnId', 'displayLanguage')
            if key in item
        }
        if promise.get('kind') or promise.get('component'):
            entry['action'] = {
                key: promise[key]
                for key in ('kind', 'component')
                if promise.get(key) is not None
            }
        projected.append(entry)
    return projected

@proposal_phase('requirements', 'requirements', revalidates='requirements')
def _review_proposal_requirements(api_key, base_url, stage_context, request_id, deadline):
    """Compile one topic's designer language, never assistant map assertions."""
    discovery = stage_context.get('proposalDiscovery') or {}
    turns = stage_context.get('requirementUserTurns') or discovery.get('userTurns') or []
    if not turns:
        raise LLMServiceError('REQUIREMENT_EVIDENCE_MISSING', 'The proposal has no traceable designer evidence.', request_id, True, 0, 502)
    snapshot = stage_context.get('stageSnapshot') or {}
    cache_key = requirement_cache_key(stage_context, turns)
    for cached in stage_context.get('verifiedRequirementCache') or []:
        if cached.get('cacheKey') != cache_key:
            continue
        candidate = cached.get('record') or {}
        try:
            restored = validate_requirement_record(requirement_model_view(candidate), turns, snapshot)
            from cocreation.domain.binary_discovery import require_choice_coverage
            require_choice_coverage(restored, stage_context.get('binaryChoices') or [])
        except (ValueError, TypeError, KeyError):
            continue
        restored.update({'topicId': candidate.get('topicId'), 'evidenceSignature': cache_key, 'interpretationVersion': INTERPRETATION_VERSION})
        _log_llm_event('requirements_reused', requestId=request_id, task='revision_requirements', cacheKey=cache_key)
        stage_context.setdefault('_reliabilityState', {})['verifiedRequirements'] = {'cacheKey': cache_key, 'record': restored}
        return restored
    inherited = []
    sources = {item['id']: item['content'] for item in turns}
    for cached in stage_context.get('verifiedRequirementCache') or []:
        prior = cached.get('record') or {}
        if cached.get('baseVersionId') != snapshot.get('versionId') or prior.get('interpretationVersion') != INTERPRETATION_VERSION:
            continue
        for raw in requirement_model_view(prior)['requirements']:
            if raw.get('evidenceSpan') and raw['evidenceSpan'] in sources.get(raw.get('sourceTurnId'), '') and (raw not in inherited):
                inherited.append(raw)
    visible_choices = _visible_interface_choices(stage_context.get('binaryChoices') or [])
    messages = [{'role': 'system', 'content': REQUIREMENT_SEMANTICS + ' ' + "Interpret this single authorized proposal topic into the supplied JSON schema. selectedInterfaceChoices are explicit clicks, not inferred agreement. Cover EACH sourceTurnId with its visible action meaning. Do not import hidden promise focus, coordinates or budgets as user constraints. Concrete button promises receive a separate strict execution check; keep the existing negotiable-goal policy for other goals. Every requirement must use an exact evidenceSpan and sourceTurnId from a designer turn. Question text is linguistic context only, never evidence of a user requirement or current map fact. The current StageSnapshot is the only map authority. Ignore 'I do not know', 'either is fine' and other noncommittal answers without discarding earlier clear directions. Distinguish entity counts, entity positions, gameplay switching, push order, route length and timing. 'Increase switching between boxes' is gameplay/boxAlternations/increase, never box/count. 'Make the player spend longer' is gameplay/experience/seek, never player/count. 'Interrupt B1 early' is gameplay/timing/early with B1 as the explicit entity. An AI-proposed preference selected by the designer stays a preference or negotiable goal, not a new prohibition, fixed number, coordinate or mandatory route dependency. Clearly expressed directional goals are strength=goal, even if described as must; they may be partly achieved or deferred, but never reversed. Use invariant only for explicit prohibitions, fixed counts, unchanged positions/distribution, or explicitly fixed edit scope. Numeric values must come from user evidence, not B1/T2 labels; null means no number was stated. unit=delta means an explicitly stated change amount; absolute means an explicitly fixed value. Do not invent numeric thresholds. priorityEvidenceSpan is empty unless the user explicitly prioritized that goal; focused=true only for the current clearly named focus. Preserve all clear still-active directions. A later explicit correction can replace an earlier direction for the same property; an unclear reply cannot supersede it. Unspecified entities may be chosen from current snapshot labels within the water/internal-wall editing scope. automaticBindings accepts ONLY existing entity LABELS (P, B1, T1, etc.), never terrain names, cells, zones or invented IDs. It may be empty; terrain-only wishes do not require choosing an entity. editing scope. Record those choices only in automaticBindings, never as explicit requirements or confirmed design memory. Never replace a designer's explicit entity. statements must be short, understandable, and in the response language; avoid new map claims. Do not infer stronger requirements from assistant explanations or unconfirmed DesignContext hypotheses. Use the topic understanding to distinguish goals, edit scope, spatial focus and preservation. Recheck it against original designerTurns: it is a candidate interpretation, not extra evidence. A focus around an entity does not request relocating it. Visual composition is a valid goal without additional transport or gameplay objectives.  exactTransitions is empty unless the designer explicitly specified the exact coordinates and tile change. Never extract exact changes or coordinates from question text. Interface clicks (selectedInterfaceChoices) NEVER authorize exactTransitions; keep exactTransitions=[] unless the designer typed explicit row/column coordinates in a free-text turn. Preserve inherited verified meanings (such as gameplay/experience for a broad 'make it simpler' turn) alongside the concrete obstacle/route requirement for each clicked choice."}, {'role': 'user', 'content': json.dumps({'designerTurns': turns, 'answeredQuestions': discovery.get('answers') or [], 'selectedInterfaceChoices': visible_choices, 'currentStageSnapshot': snapshot, 'responseLanguage': stage_context.get('responseLanguage', 'en'), 'editableComponents': ['water', 'internal_walls'], 'proposalUnderstanding': discovery.get('understanding') or {}, 'selectedDirection': proposal_options.planning_projection(discovery), 'inheritedVerifiedRequirements': inherited, 'requirementDefinitions': REQUIREMENT_DEFINITIONS, 'inheritanceRule': 'Retain unchanged verified meanings. Only exact later designer correction may supersede related items; recheck complete coverage, including inherited items, against all supplied evidence.'}, ensure_ascii=False)}]
    last_issue = ''
    failure_details = {'task': 'revision_requirements', 'failureStage': 'compilation', 'failureKind': 'timeout'}
    locked = {}
    repair_request = None
    review_repair_request = None
    retry_review_only = False
    verdict = {}
    payload = {}
    compile_attempts = 0
    review_attempts = 0
    attempt = 0
    svc = _current_service()
    while True:
        reusing_compilation = review_repair_request is not None or retry_review_only
        if not reusing_compilation and compile_attempts >= 2:
            break
        if reusing_compilation and review_attempts >= 2:
            break
        remaining = svc.remaining_until(deadline)
        if remaining < 1:
            break
        if not reusing_compilation:
            compile_attempts += 1
        attempt = max(compile_attempts, review_attempts + (1 if reusing_compilation else 0))
        phase = 'compilation'
        repair_issues = []
        try:
            if review_repair_request is not None or retry_review_only:
                pass
            elif repair_request is not None:
                patched = asyncio.run(_request_component_patch(api_key, base_url, repair_request, messages, request_id, deadline, 'revision_requirements', max_tokens=3200))
                payload = {key: patched[key] for key in ('requirements', 'automaticBindings', 'exactTransitions')}
                payload['requirements'] += patched.get('additionalRequirements', [])
            else:
                response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, 3200, min(20.0, remaining), task='revision_requirements'), timeout=min(20.0, remaining)))
                if getattr(response.choices[0], 'finish_reason', None) == 'length':
                    raise ValueError('Requirements JSON was truncated.')
                payload = parse_json_object(response.choices[0].message.content)
            valid_items, component_issues = validate_requirement_components(payload, turns, snapshot)
            if component_issues:
                repair_issues = component_issues
                locked = {item['requirementId']: raw for item, raw in valid_items}
                raise ValueError(json.dumps(component_issues, ensure_ascii=False))
            raw_by_id = {item['requirementId']: raw for item, raw in valid_items}
            try:
                record = validate_requirement_record(payload, turns, snapshot)
            except (ValueError, TypeError, KeyError):
                locked = dict(raw_by_id)
                raise
            try:
                from cocreation.domain.binary_discovery import require_choice_coverage
                require_choice_coverage(record, stage_context.get('binaryChoices') or [])
            except ValueError:
                locked = dict(raw_by_id)
                raise
            if any((raw_by_id.get(identity) != raw for identity, raw in locked.items())):
                raise ValueError('A local repair changed a locked, previously valid requirement.')
            review_messages = [{'role': 'system', 'content': REQUIREMENT_SEMANTICS + ' ' + "Independently verify this interpretation against exact designer evidence. Return JSON accepted and structured issues. Each issue has requirementIds, kind (evidence/meaning/coverage/conflict), exact evidenceSpans from designerTurns, and repairInstruction. Use IDs from the interpretation; coverage issues can have an empty ID list. Identify all related conflicting requirements. Reject misread attributes, directions, units, invented priorities, invented exact transitions, missing prohibitions, or missing still-active goals. Entity mentions are not entity changes. Questions provide linguistic context only; a selected preference never confirms assistant-invented numerical/coordinate/mandatory dependency requirements. Do not judge feasibility here. Only explicit prohibitions/fixed values are invariants; clear directional wishes are negotiable goals. Automatic bindings can fill an unspecified goal but cannot replace an explicit binding. A short answer binds to its supplied question. Reject 'early' interpreted as a fixed push count. Keep each repairInstruction to one concise sentence (under 25 words) and return at most 3 issues."}, {'role': 'user', 'content': json.dumps({'designerTurns': turns, 'selectedInterfaceChoices': visible_choices, 'answeredQuestions': discovery.get('answers') or [], 'interpretation': record, 'proposalUnderstanding': discovery.get('understanding') or {}}, ensure_ascii=False)}]
            review_messages[1]['content'] = json.dumps({**json.loads(review_messages[1]['content']), 'inheritedVerifiedRequirements': inherited, 'requirementDefinitions': REQUIREMENT_DEFINITIONS, 'inheritanceRule': 'Reject loss or reinterpretation of unchanged verified goals. Allow replacement only for exact later designer correction; qualitative appearance is not a numeric distribution claim.'}, ensure_ascii=False)
            remaining = svc.remaining_until(deadline)
            if remaining <= 0 or review_attempts >= 2:
                break
            phase = 'review'
            review_attempts += 1
            attempt = max(compile_attempts, review_attempts)
            trace_proposal('requirements_review', 'requirement_review', 'started', attempt=attempt)
            verdict = {}
            if review_repair_request is not None:
                verdict = asyncio.run(_request_component_patch(api_key, base_url, review_repair_request, review_messages, request_id, deadline, 'revision_requirement_review', max_tokens=1600, timeout_limit=min(12.0, remaining)))
            else:
                reviewed = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, review_messages, 1600, min(12.0, remaining), task='revision_requirement_review'), timeout=min(12.0, remaining)))
                if getattr(reviewed.choices[0], 'finish_reason', None) == 'length':
                    raise ValueError('Independent requirements review JSON was truncated.')
                verdict = parse_json_object(reviewed.choices[0].message.content)
            if set(verdict) != {'accepted', 'issues'} or not isinstance(verdict['accepted'], bool) or (not isinstance(verdict['issues'], list)):
                raise ValueError('Invalid independent evidence review envelope.')
            if verdict['accepted'] is not True or verdict['issues']:
                repair_issues = verdict['issues']
                rejected = set()
                for issue in verdict['issues']:
                    if not isinstance(issue, dict) or set(issue) != {'requirementIds', 'kind', 'evidenceSpans', 'repairInstruction'}:
                        raise ValueError('Independent review rejected interpretation: ' + str(verdict['issues'])[:600])
                    if issue['kind'] not in {'evidence', 'meaning', 'coverage', 'conflict'} or not isinstance(issue['requirementIds'], list):
                        raise ValueError('Invalid requirement review issue.')
                    if not set(issue['requirementIds']).issubset(raw_by_id):
                        raise ValueError('Requirement review cites unknown IDs.')
                    spans = issue['evidenceSpans']
                    if not isinstance(spans, list) or not spans or any((not isinstance(span, str) or not span or (not any((span in turn['content'] for turn in turns))) for span in spans)):
                        raise ValueError('Requirement review issue lacks exact designer evidence.')
                    if not isinstance(issue['repairInstruction'], str) or not issue['repairInstruction'].strip():
                        raise ValueError('Requirement review issue lacks a repair instruction.')
                    rejected.update(issue['requirementIds'])
                related = {(item['component'], item['property']) for item in record['requirements'] if item['requirementId'] in rejected}
                locked = {item['requirementId']: raw_by_id[item['requirementId']] for item in record['requirements'] if item['requirementId'] not in rejected and (item['component'], item['property']) not in related}
                raise ValueError('Independent evidence review rejected requirements.')
            if review_repair_request is not None:
                trace_repair('requirement_review', 'revalidated')
            record['topicId'] = discovery.get('topicId')
            record['evidenceSignature'] = cache_key
            record['interpretationVersion'] = INTERPRETATION_VERSION
            stage_context.setdefault('_reliabilityState', {})['verifiedRequirements'] = {'cacheKey': cache_key, 'record': record}
            _log_llm_event('requirements_verified', requestId=request_id, task='revision_requirements', attempt=attempt, requirementCount=len(record['requirements']), cacheKey=cache_key)
            trace_proposal('requirements_review', 'requirement_review', 'passed', attempt=attempt)
            return record
        except LLMServiceError:
            raise
        except (APIConnectionError, APIStatusError) as exception:
            error = classify_exception(exception, request_id, attempt)
            error.details = {'task': 'revision_requirements', 'failureStage': 'upstream', 'failureKind': 'upstream'}
            raise error from exception
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, asyncio.TimeoutError) as exception:
            failure_kind = 'timeout' if isinstance(exception, asyncio.TimeoutError) else 'truncated_json' if 'truncated' in str(exception).lower() else 'invalid_json' if isinstance(exception, json.JSONDecodeError) else 'interpretation'
            last_issue = str(exception) or 'Requirements phase exceeded its time limit.'
            failure_details = {'task': 'revision_requirements', 'failureStage': phase, 'failureKind': failure_kind}
            _log_llm_event('llm_component_rejected', requestId=request_id, task='revision_requirements', attempt=attempt, component='requirements', failureCode='REQUIREMENT_INTERPRETATION_INVALID', issues=last_issue[:600], **{key: value for key, value in failure_details.items() if key != 'task'})
            trace_proposal('requirements_review' if phase == 'review' else 'requirements', 'requirements', 'revalidation_failed' if repair_request is not None or review_repair_request is not None else 'rejected', reason_code(exception), attempt=attempt)
            review_repair_request = None
            retry_review_only = False
            repair_request = None
            if phase == 'review':
                reviewed_parts, technical_fields = _requirement_review_components(verdict, record, turns)
                if technical_fields:
                    if reviewed_parts:
                        review_repair_request = PatchRequest('requirement_review', reviewed_parts, [Target(field, (field,), (bool,) if field == 'accepted' else (list,)) for field in technical_fields], {'compiled_requirements': record, 'snapshot': snapshot, **{f"user:{turn['id']}": turn for turn in turns}})
                    else:
                        retry_review_only = True
            if review_repair_request is not None and failure_details['failureKind'] == 'interpretation':
                failure_details['failureKind'] = 'review_validation'
            next_reuses_compilation = review_repair_request is not None or retry_review_only
            exhausted = (
                (phase == 'review' and review_attempts >= 2)
                or (next_reuses_compilation and review_attempts >= 2)
                or (not next_reuses_compilation and compile_attempts >= 2)
            )
            if exhausted:
                trace_repair('requirements', 'exhausted')
                break
            if not next_reuses_compilation:
                if phase == 'compilation' and (failure_kind in {'timeout', 'truncated_json', 'invalid_json'} or not isinstance(payload, dict) or not payload):
                    repair_request = None
                else:
                    messages.append({'role': 'user', 'content': json.dumps({'repairIssue': last_issue, 'structuredIssues': repair_issues, 'failureDetails': failure_details, 'lockedRequirements': list(locked.values()), 'instruction': 'Repair only the server-listed targets. Locked requirements must remain unchanged. Recheck complete coverage after merging.'}, ensure_ascii=False)})
                    repair_request = _requirements_patch_request(payload, locked, turns, snapshot)
    error = LLMServiceError('REQUIREMENT_INTERPRETATION_INVALID', 'The design requirements could not be interpreted reliably. Retry this message.', request_id, True, attempt, 502)
    error.details = failure_details
    raise error

def _requirement_review_components(verdict, record, turns):
    verdict = verdict if isinstance(verdict, dict) else {}
    valid, failed = ({}, [])
    if type(verdict.get('accepted')) is bool:
        valid['accepted'] = verdict['accepted']
    else:
        failed.append('accepted')
    issues = verdict.get('issues')
    ids = {item['requirementId'] for item in record['requirements']}
    try:
        if not isinstance(issues, list):
            raise ValueError()
        for issue in issues:
            if not isinstance(issue, dict) or set(issue) != {'requirementIds', 'kind', 'evidenceSpans', 'repairInstruction'}:
                raise ValueError()
            if not isinstance(issue['requirementIds'], list) or not all((isinstance(item, str) and item in ids for item in issue['requirementIds'])) or issue['kind'] not in {'evidence', 'meaning', 'coverage', 'conflict'} or (not isinstance(issue['evidenceSpans'], list)) or (not issue['evidenceSpans']) or any((not isinstance(span, str) or not span or (not any((span in turn['content'] for turn in turns))) for span in issue['evidenceSpans'])) or (not isinstance(issue['repairInstruction'], str)) or (not issue['repairInstruction'].strip()):
                raise ValueError()
        valid['issues'] = issues
    except (ValueError, TypeError, KeyError):
        failed.append('issues')
    return (valid, failed)

def _requirements_patch_request(payload, locked, turns, snapshot):
    candidate = {key: payload.get(key) for key in ('requirements', 'automaticBindings', 'exactTransitions')}
    targets = []
    raw_items = candidate.get('requirements')
    if not isinstance(raw_items, list):
        targets.append(Target('requirements', ('requirements',), (list,)))
    else:
        for index, raw in enumerate(raw_items):
            if raw not in locked.values():
                targets.append(Target(f'requirement-{index + 1}', ('requirements', index), (dict,)))
        candidate['additionalRequirements'] = []
        targets.append(Target('additionalRequirements', ('additionalRequirements',), (list,)))
    for key in ('automaticBindings', 'exactTransitions'):
        try:
            validate_requirement_record({'requirements': [], 'automaticBindings': candidate['automaticBindings'] if key == 'automaticBindings' else [], 'exactTransitions': candidate['exactTransitions'] if key == 'exactTransitions' else []}, turns, snapshot)
        except (ValueError, TypeError, KeyError, AttributeError):
            targets.append(Target(key, (key,), (list,)))
    return PatchRequest('requirements', candidate, targets, {'snapshot': snapshot, **{f"user:{turn['id']}": turn for turn in turns}})

def _generate_verified_proposal_failure_analysis(api_key, base_url, context, rows, language, request_id, deadline, failure):
    svc = _current_service()
    verified = (context.get('_reliabilityState') or {}).get('verifiedRequirements')
    recoverable = {'PROPOSAL_SEARCH_EXHAUSTED', 'DETERMINISTIC_SEARCH_EXHAUSTED', 'CANDIDATE_UNSOLVABLE', 'CANDIDATE_DUPLICATED', 'SEMANTIC_CONSTRAINT_NOT_MET', 'SOFT_OBJECTIVE_EVIDENCE_MISSING', 'HARD_OBJECTIVE_NOT_MET'}
    if not verified or failure.code not in recoverable or svc.remaining_until(deadline) < 3.0:
        return None
    prompt = [{'role': 'system', 'content': 'Return JSON with body only, in ' + ('Simplified Chinese' if language == 'zh-CN' else 'English') + '. Explain this unsuccessful proposal search directly: the verified result, its key reason and a useful next step, without recapping the entire planning process. Respond to the verified designer requirements, explain only the supplied verified failure, and suggest a conceptual next direction without inventing a cause or new hard requirement. No candidate passed: do not claim a proposal exists, a goal was achieved or any map was changed. Do not expose internal fields, add cards, or promise an unverified mechanism. The supplied StageSnapshot is the only current map authority. ' + _visible_reply_style()}, {'role': 'user', 'content': json.dumps({'designerTurns': context.get('requirementUserTurns') or [], 'requirements': verified['record'], 'failureCode': failure.code, 'verifiedFailure': failure.safe_message, 'currentStageSnapshot': context.get('stageSnapshot')}, ensure_ascii=False)}]
    started = svc.clock.monotonic()
    model = svc.config.model
    try:
        timeout = min(12.0, svc.remaining_until(deadline))
        response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, model, prompt, 700, timeout, task='proposal_failure_analysis'), timeout=timeout))
        choice = response.choices[0]
        if getattr(choice, 'finish_reason', None) == 'length':
            raise ValueError('Failure analysis was truncated.')
        payload = parse_json_object(choice.message.content)
        if set(payload) != {'body'} or not isinstance(payload['body'], str):
            raise ValueError('Failure analysis envelope is invalid.')
        body = _sanitize_visible_model_text(payload['body'], language)
        body, _ = _strip_invalid_grounding_sentences(body, rows, entity_bindings=context.get('entityBindings'))
        if len(body) < (40 if language == 'zh-CN' else 100) or re.search('我(?:已经|已|刚刚)(?:修改|保存|应用)|已(?:成功)?(?:应用|保存)|I (?:have |already )(?:applied|changed|saved)', body, re.I):
            raise ValueError('No substantive, truthful failure analysis remained.')
        _assert_visible_output_language(language, body)
        marker = {**(context.get('proposalDiscovery') or {}), 'status': 'revision_needed', 'failureCode': failure.code, 'topicId': (context.get('proposalDiscovery') or {}).get('topicId'), 'clarificationQuestionCount': (context.get('proposalDiscovery') or {}).get('clarificationQuestionCount', 0)}
        _log_llm_event('llm_request_completed', requestId=request_id, task='proposal_failure_analysis', outcome='success', modelGenerationSucceeded=True, reliableBodyDelivered=True, fullQualityPassed=False, verifiedProposal=False, failureCode=failure.code)
        return LLMExecutionResult(body, failure.attempts_used, request_id, model=model, latency_ms=int((svc.clock.monotonic() - started) * 1000), guidance={'move': 'offer_perspective', 'proposalOffer': None, 'intentHypothesis': None, 'disagreement': None, 'followUpQuestion': None, 'uiCues': [], 'coordinateLinks': [], 'proposalDiscovery': marker}, proposal_diagnostics={'verifiedFailureAnalysis': True, 'proposalStatus': 'revision_needed', 'failureCode': failure.code, 'reliableBodyDelivered': True, 'fullQualityPassed': False})
    except (LLMServiceError, ValueError, TypeError, KeyError, asyncio.TimeoutError):
        _log_llm_event('llm_component_rejected', requestId=request_id, task='proposal_failure_analysis', component='body', failureCode='RECOVERY_FAILED')
        return None

def _proposal_objective_policy(conversation, stage_context):
    """Derive metric authority from designer text; model metrics are never hard by default."""
    record = (stage_context or {}).get('requirementRecord')
    if isinstance(record, dict):
        requirements = record.get('requirements') or []
        classes = {'boxAlternations': 'box_dependency', 'dependency': 'box_dependency', 'minimumPushes': 'longer_transport', 'solutionPushes': 'longer_transport', 'solutionSteps': 'planning_depth', 'appearance': 'visual_composition'}
        goal = next((x for x in requirements if x['strength'] != 'invariant' and x['focused']), None)
        goal = goal or next((x for x in requirements if x['property'] in classes), {})
        return {'schemaVersion': 2, 'hardMetricGoals': [], 'softObjectiveClass': classes.get(goal.get('property'), 'general'), 'targetEntities': sorted({e for x in requirements for e in x['entities']}), 'requiresMechanismEvidence': False, 'requirementPolicyVersion': 1, 'designAspect': ((stage_context.get('proposalDiscovery') or {}).get('understanding') or {}).get('aspect', 'unspecified'), 'exactTransitions': record.get('exactTransitions') or [], 'editableRequestedComponents': [x['component'] for x in ((stage_context.get('proposalDiscovery') or {}).get('understanding') or {}).get('editScope') or []], 'requirements': requirements}
    user_text = ' '.join((str(item.get('content') or '') for item in (conversation or [])[-12:] if item.get('role') == 'user'))
    brief = str((stage_context or {}).get('authorizedRevisionBrief') or '')
    text = ' '.join((brief, user_text)).strip()
    lowered = text.casefold()
    mandatory_pattern = re.compile('(?:必须|至少|不得少于|务必|minimum|at least|must|required)')
    clauses = [item.strip() for item in re.split('[。！？!?；;\\n]+', lowered) if item.strip()]

    def requested_delta(keywords):
        matching = [clause for clause in clauses if mandatory_pattern.search(clause) and any((word in clause for word in keywords))]
        if not matching:
            return None
        numbers = re.findall('\\d+', matching[-1])
        return max(1, int(numbers[-1])) if numbers else 1
    hard_metrics = []
    step_delta = requested_delta(('最短解', '最短路径', '最短路线', 'solution steps', 'shortest solution'))
    push_delta = requested_delta(('最少推动', '最小推动', '推动次数', '推箱次数', 'minimum pushes', 'push count'))
    if step_delta is not None:
        hard_metrics.append({'metric': 'solutionSteps', 'direction': 'increase', 'minimumDelta': step_delta, 'source': 'explicit_user'})
    if push_delta is not None:
        hard_metrics.append({'metric': 'minimumPushes', 'direction': 'increase', 'minimumDelta': push_delta, 'source': 'explicit_user'})
    objective_class = 'general'
    if any((word in lowered for word in ('运输', '长距离推', '推箱深度', 'transport'))):
        objective_class = 'longer_transport'
    elif any((word in lowered for word in ('互相牵制', '顺序依赖', '互相阻挡', '先后顺序', 'dependency', 'interlock'))):
        objective_class = 'box_dependency'
    elif any((word in lowered for word in ('共享通道', '共享走廊', '局部阻挡', 'shared corridor', 'shared passage'))):
        objective_class = 'shared_blocking'
    elif any((word in lowered for word in ('绕行', '更长规划', '花更多时间', '宏观规划', '规划再执行', 'detour', 'planning'))):
        objective_class = 'planning_depth'
    elif any((word in lowered for word in ('空间压迫', '路线选择', '扩展探索', 'space pressure', 'route choice'))):
        objective_class = 'space_route_choice'
    exact_labels = {str(item.get('label') or item.get('id') or '').upper() for item in ((stage_context or {}).get('entityBindings') or {}).get('entities') or [] if item.get('identityConfidence') == 'exact'}
    target_entities = sorted({label.upper() for label in re.findall('\\b(?:B\\d+|T\\d+|P)\\b', text, flags=re.IGNORECASE) if label.upper() in exact_labels})
    return {'schemaVersion': 1, 'hardMetricGoals': hard_metrics, 'softObjectiveClass': objective_class, 'targetEntities': target_entities, 'requiresMechanismEvidence': objective_class != 'general'}

def _apply_objective_policy_to_plan(plan, policy, preserved_components=None):
    hard_goals = tuple((MetricGoal(item['metric'], item['direction'], int(item.get('minimumDelta') or 1)) for item in policy.get('hardMetricGoals') or []))
    plan = replace(plan, strategies=tuple((replace(strategy, metric_goals=hard_goals, required_transitions=tuple(((x['row'], x['column'], x['from'], x['to']) for x in policy['exactTransitions'])) if policy.get('exactTransitions') else strategy.required_transitions, preserve=frozenset(strategy.preserve).union(preserved_components or ())) for strategy in plan.strategies)))
    if policy.get('requirementPolicyVersion') != 1:
        return plan
    aliases = {'water': 'water', 'wall': 'internal_walls'}
    requested = set(policy.get('editableRequestedComponents') or [])
    if not requested:
        requested = {aliases[x['component']] for x in policy.get('requirements') or [] if x['component'] in aliases}
    strategies = []
    for strategy in plan.strategies:
        if strategy.required_transitions:
            strategies.append(strategy)
            continue
        preserve = set(strategy.preserve)
        operators = list(strategy.operators)
        for component, public_component in aliases.items():
            preserve_name = 'walls' if component == 'wall' else 'water'
            if public_component not in requested or preserve_name in set(preserved_components or ()):
                continue
            preserve.discard(preserve_name)
            removal_forbidden = any((x['component'] == component and x['property'] == 'count' and (x['strength'] == 'invariant') and (x.get('scope') == 'original_cells') and (x['relation'] in {'preserve', 'equal'}) and (x['value'] is None) for x in policy.get('requirements') or []))
            if removal_forbidden:
                operators = [operator for operator in operators if operator != 'remove_' + component]
            if any((x['component'] == component and x['property'] == 'count' and (x['relation'] == 'increase') and (x.get('scope', 'all') == 'all') for x in policy.get('requirements') or [])):
                if not removal_forbidden and 'add_' + component in operators and ('remove_' + component not in operators):
                    operators.append('remove_' + component)
        strategies.append(replace(strategy, operators=tuple(operators), preserve=frozenset(preserve)))
    return replace(plan, strategies=tuple(strategies))

def _proposal_route_features(rows, validation, entity_bindings=None, *, deadline=None):
    metrics = validation.as_dict() if hasattr(validation, 'as_dict') else dict(validation or {})
    evidence = _proposal_clarification_route_evidence(rows, metrics, {'entityBindings': entity_bindings or {}})
    clock = _active_clock()
    try:
        minimum = minimum_pushes(rows, deadline=deadline, clock=clock) if deadline is not None else minimum_pushes(rows, clock=clock)
    except TimeoutError:
        raise
    except Exception:
        minimum = None
    push_order = evidence.get('pushOrder') or []
    return {'solutionSteps': metrics.get('solutionSteps'), 'solutionPushes': metrics.get('solutionPushes'), 'minimumPushes': minimum, 'pushBlocks': len(push_order), 'boxAlternations': evidence.get('boxAlternations'), 'pushesByBox': evidence.get('pushesByBox') or {}, 'routeMode': evidence.get('mode'), 'solution': metrics.get('solution') or '', 'longestPushRun': max([0] + [int(x.get('pushes') or 0) for x in push_order]), 'firstRuns': {label: next((int(x.get('pushes') or 0) for x in push_order if x.get('box') == label)) for label in {x.get('box') for x in push_order}}, 'interruptions': {label: sum((x.get('box') == label for x in push_order)) > 1 for label in {x.get('box') for x in push_order}}, 'entityPositions': {str(x.get('id') or x.get('label') or '').upper(): (x.get('row'), x.get('column')) for x in (entity_bindings or {}).get('entities') or [] if x.get('row') is not None and x.get('column') is not None}}

def _trace_cells_for_solution(rows, solution):
    player = next(((x, y) for y, row in enumerate(rows) for x, tile in enumerate(row) if tile == 'p'), None)
    if player is None:
        return set()
    boxes = {(x, y) for y, row in enumerate(rows) for x, tile in enumerate(row) if tile == 's'}
    traced = {player, *boxes}
    for move in str(solution or '').upper():
        delta = {'U': (0, -1), 'D': (0, 1), 'L': (-1, 0), 'R': (1, 0)}.get(move)
        if delta is None:
            return set()
        destination = (player[0] + delta[0], player[1] + delta[1])
        traced.add(destination)
        if destination in boxes:
            box_destination = (destination[0] + delta[0], destination[1] + delta[1])
            boxes.remove(destination)
            boxes.add(box_destination)
            traced.add(box_destination)
        player = destination
    return traced

def _objective_mechanism_evidence(base_rows, candidate_rows, baseline_features, candidate_features, policy):
    changed = {(x, y) for y, (before_row, after_row) in enumerate(zip(base_rows, candidate_rows)) for x, (before, after) in enumerate(zip(before_row, after_row)) if before != after}
    trace = _trace_cells_for_solution(candidate_rows, candidate_features.get('solution'))
    route_affected = any((abs(cx - tx) + abs(cy - ty) <= 1 for cx, cy in changed for tx, ty in trace))
    deltas = {}
    for key in ('solutionSteps', 'solutionPushes', 'minimumPushes', 'pushBlocks', 'boxAlternations'):
        before = baseline_features.get(key)
        after = candidate_features.get(key)
        if isinstance(before, int) and isinstance(after, int):
            deltas[key] = after - before
    both_boxes = sum((value > 0 for value in (candidate_features.get('pushesByBox') or {}).values())) >= 2
    objective = policy.get('softObjectiveClass') or 'general'
    passed = True
    missing = []
    if objective == 'longer_transport':
        target_boxes = [item for item in policy.get('targetEntities') or [] if str(item).startswith('B')]
        if target_boxes:
            before_pushes = baseline_features.get('pushesByBox') or {}
            after_pushes = candidate_features.get('pushesByBox') or {}
            passed = any((int(after_pushes.get(label) or 0) > int(before_pushes.get(label) or 0) for label in target_boxes))
        else:
            passed = any((deltas.get(key, 0) > 0 for key in ('minimumPushes', 'solutionPushes', 'pushBlocks')))
        missing = [] if passed else ['longer_transport_delta']
    elif objective == 'box_dependency':
        passed = both_boxes and route_affected and any((deltas.get(key, 0) > 0 for key in ('pushBlocks', 'boxAlternations')))
        missing = [] if passed else ['box_dependency_delta']
    elif objective == 'shared_blocking':
        passed = both_boxes and route_affected and any((deltas.get(key, 0) != 0 for key in ('pushBlocks', 'boxAlternations', 'minimumPushes')))
        missing = [] if passed else ['shared_blocking_route_effect']
    elif objective == 'planning_depth':
        passed = route_affected and any((deltas.get(key, 0) > 0 for key in ('solutionSteps', 'minimumPushes', 'pushBlocks', 'boxAlternations')))
        missing = [] if passed else ['planning_depth_delta']
    elif objective == 'space_route_choice':
        structural_change = any((base_rows[y][x] in {'.', '#', '@'} and candidate_rows[y][x] in {'.', '#', '@'} for x, y in changed))
        passed = route_affected and structural_change
        missing = [] if passed else ['route_relevant_space_change']
    elif objective == 'visual_composition':
        passed, missing = (False, ['visual_quality_not_machine_verified'])
    return {'passed': bool(passed), 'objectiveClass': objective, 'routeAffected': route_affected, 'bothBoxesUsed': both_boxes, 'metricDeltas': deltas, 'missing': missing}

def _objective_validating_proposal_validator(proposal_validator, base_rows, baseline_validation, policy, entity_bindings, evidence_by_fingerprint, revision_workflow=None, deadline=None):
    baseline_features = _proposal_route_features(base_rows, baseline_validation, entity_bindings, **{'deadline': deadline} if deadline is not None else {})

    @proposal_phase('structure_and_solve', 'candidate')
    def validate(candidate_rows):
        validation = proposal_validator(candidate_rows) if proposal_validator is not None else _solve_stage(candidate_rows)
        features = _proposal_route_features(candidate_rows, validation, entity_bindings, **{'deadline': deadline} if deadline is not None else {})
        evidence = _objective_mechanism_evidence(base_rows, candidate_rows, baseline_features, features, policy)
        evidence_by_fingerprint[map_fingerprint(candidate_rows)] = evidence
        semantic_results = validate_semantic_constraints(base_rows, candidate_rows, revision_workflow or {})
        evidence['semanticConstraintResults'] = semantic_results
        record = (revision_workflow or {}).get('requirementRecord')
        if isinstance(record, dict):
            outcomes = evaluate_requirements(base_rows, candidate_rows, record, baseline_features, features)
            evidence['goalOutcomes'] = outcomes
            evidence['goalRank'] = goal_rank(outcomes)
            if any((not x['passed'] for x in outcomes)):
                raise SemanticConstraintError(outcomes)
        if (revision_workflow or {}).get('binaryPromises'):
            from cocreation.domain.binary_discovery import check_promises
            check_promises(base_rows, candidate_rows, revision_workflow['binaryPromises'], baseline_validation, validation)
        if policy.get('requiresMechanismEvidence') and (not evidence['passed']):
            raise ObjectiveEvidenceError(evidence)
        return validation
    if isinstance((revision_workflow or {}).get('requirementRecord'), dict):
        validate.candidate_rank = lambda candidate_rows: tuple((evidence_by_fingerprint.get(map_fingerprint(candidate_rows)) or {}).get('goalRank') or ())
    return validate

@proposal_phase('requirement_postcondition', 'requirements')
def validate_frozen_requirements(base_rows, candidate_rows, workflow, baseline_validation, candidate_validation, entity_bindings, *, deadline=None):
    if (workflow or {}).get('binaryPromises'):
        from cocreation.domain.binary_discovery import check_promises, BinaryError
        try:
            check_promises(base_rows, candidate_rows, workflow['binaryPromises'], baseline_validation, candidate_validation)
        except BinaryError as error:
            raise SemanticConstraintError([{'reason': error.code, 'passed': False}]) from error
    record = (workflow or {}).get('requirementRecord')
    if not isinstance(record, dict):
        return []
    outcomes = evaluate_requirements(base_rows, candidate_rows, record, _proposal_route_features(base_rows, baseline_validation, entity_bindings, **{'deadline': deadline} if deadline is not None else {}), _proposal_route_features(candidate_rows, candidate_validation, entity_bindings, **{'deadline': deadline} if deadline is not None else {}))
    promised = {x['requirementId']: x for x in (workflow or {}).get('goalOutcomes') or []}
    failures = [x for x in outcomes if not x['passed'] or (promised.get(x['requirementId'], {}).get('status') == 'fulfilled' and x['status'] != 'fulfilled')]
    if failures:
        raise SemanticConstraintError(failures)
    return outcomes

def _attempt_semantic_revision_replan(*, api_key, base_url, model, original_messages, rows, request_id, language, stage_context, objective_policy, objective_validator, baseline_metrics, movement_requirement, preserved_components, started_at, deadline, first_failure, evidence_by_fingerprint, excluded_map_fingerprints=None):
    svc = _current_service()
    if svc.remaining_until(deadline) < svc.config.min_retry_budget_seconds:
        raise first_failure
    messages = [dict(item) for item in original_messages]
    failure_summary = json.dumps((getattr(first_failure, 'proposal_diagnostics', {}) or {}).get('rejectionRecords', [])[-3:], ensure_ascii=False, separators=(',', ':'))[:1000]
    messages[0]['content'] += '\n\nThe previous semantic plan produced no admissible candidate. Create one to three materially different local strategies while preserving the authorized direction, explicit anchors, prohibitions, and objectivePolicy. Change focus, effect, or allowed operators as needed. Do not repeat the previous concrete treatment and keep requiredTransitions empty unless the supplied execution brief itself froze exact transitions. Previous safe rejection summary: ' + failure_summary
    replan_deadline = min(deadline, svc.clock.monotonic() + svc.config.proposal_plan_retry_timeout_seconds)
    plan, plan_attempts, _ = asyncio.run(asyncio.wait_for(_compile_revision_plan(api_key=api_key, base_url=base_url, models=[model], messages=messages, request_id=request_id, started_at=started_at, deadline=replan_deadline), timeout=max(0.001, svc.remaining_until(replan_deadline))))
    plan = _apply_objective_policy_to_plan(plan, objective_policy, preserved_components)
    plan, replan_preflight_failures = _preflight_revision_strategies(plan, rows, stage_context, stage_context.get('authorizedRevisionBrief') or '')
    if not plan.strategies:
        raise ValueError('Every semantic replan strategy failed contract preflight.')
    contract = _build_revision_execution_contract(plan, stage_context.get('authorizedRevisionBrief') or '', stage_context)
    contract['objectivePolicy'] = objective_policy
    workflow = contract.get('revisionWorkflow') or {}
    if workflow and objective_policy.get('softObjectiveClass') not in {None, 'general'}:
        workflow['softObjectives'] = [{'kind': 'visual_preference' if objective_policy.get('softObjectiveClass') == 'visual_composition' else 'route_mechanism', 'statement': str(objective_policy.get('softObjectiveClass'))[:600]}]
    operation_messages = _build_map_operation_messages(contract, rows, language, stage_context, baseline_metrics)
    try:
        result = asyncio.run(asyncio.wait_for(_generate_map_operation_candidates(api_key=api_key, base_url=base_url, models=[model], messages=operation_messages, base_rows=rows, request_id=request_id, language=language, proposal_validator=objective_validator, revision_contract=contract, baseline_metrics=baseline_metrics, started_at=started_at, deadline=deadline, candidate_evidence=evidence_by_fingerprint, excluded_map_fingerprints=excluded_map_fingerprints), timeout=max(0.001, svc.remaining_until(deadline))))
    except LLMServiceError as operation_error:
        if operation_error.code != 'PROPOSAL_SEARCH_EXHAUSTED':
            raise
        result = _deterministic_revision_fallback(rows=rows, plan=plan, revision_contract=contract, request_id=request_id, language=language, proposal_validator=objective_validator, baseline_metrics=baseline_metrics, movement_requirement=movement_requirement, preserved_components=preserved_components, entity_bindings=stage_context.get('entityBindings'), started_at=started_at, deadline=deadline, excluded_map_fingerprints=excluded_map_fingerprints)
    diagnostics = dict(result.proposal_diagnostics or {})
    diagnostics.update({'replanAttempted': True, 'replanSucceeded': True, 'replanContractPreflightFailures': replan_preflight_failures})
    return (replace(result, proposal_diagnostics=diagnostics), plan, contract, plan_attempts)

@proposal_phase('proposal_generation', 'proposal')
def _generate_revision_search_proposal_sync(*, api_key, base_url, conversation, rows, request_id, language, proposal_validator, stage_context, baseline_metrics=None, deadline=None):
    svc = _current_service()
    started_at = svc.clock.monotonic()
    deadline = deadline or _request_deadline(started_at)
    stage_context = dict(stage_context or {})
    plan_phase_seconds = svc.config.proposal_llm_phase_timeout_seconds
    plan_primary_seconds = svc.config.proposal_plan_primary_timeout_seconds
    if stage_context.get('requirementPolicyVersion') == 1:
        plan_phase_seconds, plan_primary_seconds = (50.0, 35.0)
        stage_context['requirementRecord'] = _current_service().review_proposal_requirements(api_key, base_url, stage_context, request_id, deadline)
        issues = preflight_requirements(rows, stage_context['requirementRecord'])
        if issues:
            error = LLMServiceError('REQUIREMENT_PERMISSION_CONFLICT', 'Verified requirements conflict with fixed editing permissions.', request_id, False, 0, 422)
            error.revision_contract = {'revisionWorkflow': {'requirementRecord': stage_context['requirementRecord']}}
            error.proposal_diagnostics = {'category': 'requirement_preflight_conflict', 'rejectionRecords': issues}
            raise error
    objective_policy = _proposal_objective_policy(conversation, stage_context)
    stage_context['objectivePolicy'] = objective_policy
    excluded_candidate_fingerprints = {str(stage_context.get('excludedProposalCandidateFingerprint') or '').strip()} - {''}
    baseline_metrics = dict(baseline_metrics or {})
    if 'minimumPushes' not in baseline_metrics:
        try:
            baseline_metrics['minimumPushes'] = minimum_pushes(rows, clock=svc.clock)
        except Exception:
            baseline_metrics['minimumPushes'] = None
    models = _unified_model_attempts(svc.config.proposal_generation_attempts)
    movement_requirement = _authorized_movement_requirement(conversation)
    preserved_components = _authorized_preserved_components(conversation, stage_context)
    messages = _build_revision_plan_messages(conversation, rows, language, stage_context, movement_requirement, preserved_components)
    _log_llm_event('llm_request_started', requestId=request_id, task='revision_plan', primaryModel=models[0], fallbackModel=None, timeoutSeconds=svc.config.backend_request_timeout_seconds if stage_context.get('requirementPolicyVersion') == 1 else svc.config.proposal_request_timeout_seconds, internalDeadlineSeconds=round(svc.remaining_until(deadline), 3), revisionPlanPhaseSeconds=min(svc.config.proposal_llm_phase_timeout_seconds, svc.remaining_until(deadline)), revisionPlanTokenLimit=PROPOSAL_PLAN_MAX_COMPLETION_TOKENS, operationAttemptSeconds=svc.config.proposal_operation_attempt_timeout_seconds, deterministicSearchSeconds=svc.config.proposal_search_deadline_seconds, responseMode='revision_plan')
    try:
        plan, attempts_used, model = asyncio.run(asyncio.wait_for(_compile_revision_plan(api_key=api_key, base_url=base_url, models=models, messages=messages, request_id=request_id, started_at=started_at, deadline=min(deadline, svc.clock.monotonic() + plan_phase_seconds), first_attempt_timeout=plan_primary_seconds), timeout=min(plan_phase_seconds, svc.remaining_until(deadline))))
    except asyncio.TimeoutError as exception:
        elapsed_ms = int((svc.clock.monotonic() - started_at) * 1000)
        _log_llm_event('llm_request_completed', requestId=request_id, task='revision_plan', outcome='error', code='UPSTREAM_TIMEOUT', attemptsUsed=svc.config.proposal_generation_attempts, latencyMs=elapsed_ms, responseMode='revision_plan')
        raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not compile the revision plan before the proposal time limit.', request_id, True, svc.config.proposal_generation_attempts, 504) from exception
    except LLMServiceError as exception:
        if exception.code in {'MODEL_RESPONSE_INVALID', 'MODEL_EMPTY_RESPONSE', 'MODEL_LOW_QUALITY_RESPONSE'}:
            exception.code = 'REVISION_PLAN_INVALID'
            exception.safe_message = 'Kimi did not return a complete valid RevisionPlan after the bounded repair attempt.'
        _log_llm_event('llm_request_completed', requestId=request_id, task='revision_plan', outcome='error', code=exception.code, attemptsUsed=exception.attempts_used, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), responseMode='revision_plan', **_provider_error_fields(exception))
        raise
    plan = _bind_execution_brief_to_plan(plan, stage_context.get('authorizedExecutionBrief') if stage_context else None)
    plan = _apply_objective_policy_to_plan(plan, objective_policy, preserved_components)
    plan, contract_preflight_failures = _preflight_revision_strategies(plan, rows, stage_context, stage_context.get('authorizedRevisionBrief') if stage_context else '')
    if not plan.strategies:
        correction_feedback = json.dumps(contract_preflight_failures[-3:], ensure_ascii=False, separators=(',', ':'))[:1600]
        try:
            corrected_plan, correction_attempts, _ = asyncio.run(asyncio.wait_for(_compile_revision_plan(api_key=api_key, base_url=base_url, models=models, messages=messages, request_id=request_id, started_at=started_at, deadline=min(deadline, svc.clock.monotonic() + svc.config.proposal_plan_retry_timeout_seconds), max_attempts=1, initial_validation_feedback=correction_feedback, first_attempt_timeout=svc.config.proposal_plan_retry_timeout_seconds), timeout=max(0.001, min(svc.config.proposal_plan_retry_timeout_seconds, svc.remaining_until(deadline)))))
            attempts_used += correction_attempts
            corrected_plan = _apply_objective_policy_to_plan(corrected_plan, objective_policy, preserved_components)
            plan, corrected_failures = _preflight_revision_strategies(corrected_plan, rows, stage_context, stage_context.get('authorizedRevisionBrief') if stage_context else '')
            contract_preflight_failures.extend(corrected_failures)
        except (asyncio.TimeoutError, LLMServiceError):
            plan = RevisionPlan(tuple())
    if not plan.strategies:
        error = LLMServiceError('REVISION_CONTRACT_CONFLICT', 'Every proposed strategy failed contract feasibility preflight.', request_id, False, attempts_used, 422)
        error.revision_plan = {'strategies': []}
        error.revision_contract = {'authorizedBrief': str(stage_context.get('authorizedRevisionBrief') if stage_context else '').strip()[:1200], 'strategies': []}
        error.proposal_diagnostics = {'category': 'contract_preflight_exhausted', 'contractPreflightFailures': contract_preflight_failures[-6:], 'rejectionRecords': contract_preflight_failures[-6:], 'contractCorrectionAttempted': True}
        raise error
    try:
        revision_contract = _build_revision_execution_contract(plan, stage_context.get('authorizedRevisionBrief') if stage_context else '', stage_context)
        revision_contract['objectivePolicy'] = objective_policy
        workflow = revision_contract.get('revisionWorkflow') or {}
        if workflow and objective_policy.get('softObjectiveClass') not in {None, 'general'}:
            workflow['softObjectives'] = [{'kind': 'visual_preference' if objective_policy.get('softObjectiveClass') == 'visual_composition' else 'route_mechanism', 'statement': str(objective_policy.get('softObjectiveClass'))[:600]}]
    except ValueError as exception:
        error = LLMServiceError('REVISION_CONTRACT_CONFLICT', str(exception), request_id, False, attempts_used, 422)
        error.revision_plan = plan.as_dict()
        error.revision_contract = {}
        error.proposal_diagnostics = {'category': 'contract_conflict'}
        raise error from exception
    try:
        validate_revision_plan_against_map(rows, plan, (stage_context or {}).get('entityBindings'))
        _validate_revision_plan_entities(plan, rows, (stage_context or {}).get('entityBindings'))
    except ValueError as exception:
        error = LLMServiceError('REVISION_CONTRACT_CONFLICT', str(exception), request_id, False, attempts_used, 422)
        error.revision_plan = plan.as_dict()
        error.revision_contract = revision_contract
        error.proposal_diagnostics = {'category': 'coordinate_or_contract_conflict'}
        raise error from exception
    evidence_by_fingerprint = {}
    baseline_validation = _solve_stage(rows, **{'deadline': deadline} if stage_context.get('binaryDiscovery') else {})
    objective_validator = _objective_validating_proposal_validator(proposal_validator, rows, baseline_validation, objective_policy, stage_context.get('entityBindings'), evidence_by_fingerprint, revision_contract.get('revisionWorkflow'), deadline=deadline if stage_context.get('binaryDiscovery') else None)
    exact_strategies = [item for item in revision_contract.get('strategies') or [] if item.get('requiredTransitions')]
    can_execute_exactly = exact_strategies and len(exact_strategies) == len(revision_contract.get('strategies') or []) and (exact_strategies[0].get('effect') not in {'relocate_start', 'relocate_box', 'relocate_target'} or bool(stage_context.get('entityBindings')))
    if can_execute_exactly:
        strategy = exact_strategies[0]
        exact_brief = {'schemaVersion': 1, 'effect': strategy.get('effect'), 'anchors': strategy.get('anchorEntities') or [], 'focus': strategy.get('focus'), 'requiredTransitions': strategy.get('requiredTransitions') or [], 'allowedOperators': strategy.get('allowedOperators') or [], 'preserve': strategy.get('preserve') or [], 'playObjective': strategy.get('playObjective')}
        try:
            exact = _deterministic_exact_revision(rows, exact_brief, request_id, language, objective_validator, stage_context.get('entityBindings'))
        except Exception as exception:
            error = LLMServiceError('EXACT_TRANSITION_INFEASIBLE', 'The frozen tile transitions could not produce a valid solvable proposal.', request_id, False, attempts_used, 422)
            error.revision_plan = plan.as_dict()
            error.revision_contract = revision_contract
            error.proposal_diagnostics = {'category': 'exact_transition_infeasible', 'reason': _safe_validation_reason(exception)}
            raise error from exception
        if exact is not None:
            exact_fingerprint = map_fingerprint(exact.proposed_rows or [])
            if exact_fingerprint in excluded_candidate_fingerprints:
                error = LLMServiceError('CANDIDATE_DUPLICATED', 'The exact alternative repeats the candidate stored in the cited purple card.', request_id, False, attempts_used, 422)
                error.revision_plan = plan.as_dict()
                error.revision_contract = revision_contract
                error.proposal_diagnostics = {'category': 'candidate_duplicated', 'excludedCandidateCount': len(excluded_candidate_fingerprints)}
                raise error
            evidence = evidence_by_fingerprint.get(exact_fingerprint)
            return replace(exact, attempts_used=attempts_used, revision_plan=plan.as_dict(), revision_contract=revision_contract, proposal_diagnostics={**(exact.proposal_diagnostics or {}), 'selectedStrategyIndex': 1, 'planAttempts': attempts_used, 'modifierAttempts': 0, 'objectivePolicy': objective_policy, 'mechanismEvidence': evidence})
    remaining_seconds = svc.remaining_until(deadline)
    if remaining_seconds <= 0:
        error = LLMServiceError('GLOBAL_PROPOSAL_TIMEOUT', 'The complete proposal pipeline exhausted its global time budget.', request_id, True, attempts_used, 504)
        error.revision_plan = plan.as_dict()
        error.revision_contract = revision_contract
        raise error
    operation_messages = _build_map_operation_messages(revision_contract, rows, language, stage_context, baseline_metrics)
    try:
        operation_result = asyncio.run(asyncio.wait_for(_generate_map_operation_candidates(api_key=api_key, base_url=base_url, models=models, messages=operation_messages, base_rows=rows, request_id=request_id, language=language, proposal_validator=objective_validator, revision_contract=revision_contract, baseline_metrics=baseline_metrics, started_at=started_at, deadline=deadline, candidate_evidence=evidence_by_fingerprint, excluded_map_fingerprints=excluded_candidate_fingerprints), timeout=remaining_seconds))
    except asyncio.TimeoutError as exception:
        exhausted_global_budget = svc.remaining_until(deadline) <= 0.05
        error = LLMServiceError('GLOBAL_PROPOSAL_TIMEOUT' if exhausted_global_budget else 'UPSTREAM_TIMEOUT', 'The complete proposal pipeline exhausted its global time budget.' if exhausted_global_budget else 'Kimi did not create executable revision operations before the operation-phase time limit.', request_id, True, attempts_used + svc.config.proposal_generation_attempts, 504)
        error.revision_plan = plan.as_dict()
        error.revision_contract = revision_contract
        raise error from exception
    except LLMServiceError as exception:
        exception.attempts_used += attempts_used
        exception.revision_plan = plan.as_dict()
        exception.revision_contract = revision_contract
        if exception.code != 'PROPOSAL_SEARCH_EXHAUSTED':
            raise
        fallback = None
        replan_failure = None
        semantic_contract = not any((item.get('requiredTransitions') for item in revision_contract.get('strategies') or []))
        if semantic_contract:
            try:
                fallback, plan, revision_contract, replan_attempts = _attempt_semantic_revision_replan(api_key=api_key, base_url=base_url, model=models[0], original_messages=messages, rows=rows, request_id=request_id, language=language, stage_context=stage_context, objective_policy=objective_policy, objective_validator=objective_validator, baseline_metrics=baseline_metrics, movement_requirement=movement_requirement, preserved_components=preserved_components, started_at=started_at, deadline=deadline, first_failure=exception, evidence_by_fingerprint=evidence_by_fingerprint, excluded_map_fingerprints=excluded_candidate_fingerprints)
                attempts_used += replan_attempts
            except Exception as replan_exception:
                replan_failure = _safe_validation_reason(replan_exception)
        try:
            if fallback is None:
                fallback = _deterministic_revision_fallback(rows=rows, plan=plan, revision_contract=revision_contract, request_id=request_id, language=language, proposal_validator=objective_validator, baseline_metrics=baseline_metrics, movement_requirement=movement_requirement, preserved_components=preserved_components, entity_bindings=(stage_context or {}).get('entityBindings'), started_at=started_at, deadline=deadline, excluded_map_fingerprints=excluded_candidate_fingerprints)
        except ProposalSearchExhausted as search_exception:
            diagnostics = dict(getattr(exception, 'proposal_diagnostics', {}) or {})
            diagnostics['deterministicFallback'] = search_exception.diagnostics
            diagnostics['replanAttempted'] = semantic_contract
            diagnostics['replanSucceeded'] = False
            if replan_failure:
                diagnostics['replanFailure'] = replan_failure[:500]
            exception.proposal_diagnostics = diagnostics
            rejection_categories = {item.get('category') for item in diagnostics.get('rejectionRecords') or [] if isinstance(item, dict)}
            fallback_reasons = set((search_exception.diagnostics.get('failureReasons') or {}).keys())
            if rejection_categories and rejection_categories <= {'soft_objective_evidence_missing', 'candidate_duplicated'} and (fallback_reasons <= {'ObjectiveEvidenceError'}):
                exception.code = 'SOFT_OBJECTIVE_EVIDENCE_MISSING'
                exception.safe_message = 'Solvable local candidates were found, but none produced verified evidence for the requested play mechanism.'
            elif 'semantic_contract' in rejection_categories or 'SemanticConstraintError' in fallback_reasons:
                exception.code = 'SEMANTIC_CONSTRAINT_NOT_MET'
                exception.safe_message = 'Candidates were found, but none satisfied every explicit semantic requirement in the authorized revision.'
            elif 'hard_objective_not_met' in rejection_categories or 'metric_goal_not_met' in fallback_reasons:
                exception.code = 'HARD_OBJECTIVE_NOT_MET'
                exception.safe_message = "No candidate satisfied the designer's explicit measurable requirement."
            elif rejection_categories and rejection_categories <= {'candidate_duplicated'} and (fallback_reasons <= {'CANDIDATE_DUPLICATED'}):
                exception.code = 'CANDIDATE_DUPLICATED'
                exception.safe_message = 'Every candidate repeated a previously rejected or cited proposal.'
            elif rejection_categories and rejection_categories <= {'candidate_unsolvable', 'candidate_duplicated'} and (fallback_reasons <= {'UNSOLVABLE_LEVEL', 'CANDIDATE_DUPLICATED'}):
                exception.code = 'CANDIDATE_UNSOLVABLE'
                exception.safe_message = 'Every distinct candidate failed deterministic solvability validation.'
            else:
                exception.code = 'DETERMINISTIC_SEARCH_EXHAUSTED'
                exception.safe_message = 'The modifier and bounded deterministic search found no admissible solvable candidate.'
            raise exception
        fallback_diagnostics = dict(fallback.proposal_diagnostics or {})
        fallback_diagnostics.setdefault('replanAttempted', semantic_contract)
        fallback_diagnostics.setdefault('replanSucceeded', bool(semantic_contract and (not replan_failure)))
        fallback_diagnostics['modifierFailure'] = (getattr(exception, 'proposal_diagnostics', {}) or {}).get('modifierFailure')
        operation_result = replace(fallback, attempts_used=exception.attempts_used, revision_plan=plan.as_dict(), revision_contract=revision_contract, proposal_diagnostics=fallback_diagnostics)
    elapsed_ms = int((svc.clock.monotonic() - started_at) * 1000)
    guidance = dict(operation_result.guidance)
    latest_user = _latest_role_content(conversation, 'user')
    intent_hypothesis = _replace_echoed_intent_hypothesis(_natural_intent_candidate(latest_user, language, _latest_user_explicitly_agrees(latest_user), difficulty_reframe=_user_reframes_difficulty_judgment(conversation)), latest_user, language) if _user_states_first_person_view(latest_user) else None
    guidance['intentHypothesis'] = intent_hypothesis
    guidance['intentConfidence'] = 'medium' if intent_hypothesis else None
    diagnostics = dict(operation_result.proposal_diagnostics)
    selected_evidence = evidence_by_fingerprint.get(map_fingerprint(operation_result.proposed_rows or []))
    diagnostics['objectivePolicy'] = objective_policy
    diagnostics['mechanismEvidence'] = selected_evidence
    diagnostics['planAttempts'] = attempts_used
    diagnostics['contractPreflightFailures'] = contract_preflight_failures
    diagnostics['modifierAttempts'] = operation_result.attempts_used
    diagnostics['revisionContract'] = revision_contract
    record = (revision_contract.get('revisionWorkflow') or {}).get('requirementRecord')
    if isinstance(record, dict):
        outcomes = (selected_evidence or {}).get('goalOutcomes') or []
        diagnostics['goalOutcomes'] = outcomes
        diagnostics['automaticBindings'] = record.get('automaticBindings') or []
        revision_contract['revisionWorkflow']['goalOutcomes'] = outcomes
        operation_result = replace(operation_result, assistant_message='\n\n'.join((x for x in (operation_result.assistant_message, tradeoff_message(record, outcomes, language)) if x)))
    _log_llm_event('llm_request_completed', requestId=request_id, task='map_proposal', outcome='success', model=operation_result.model, attemptsUsed=attempts_used + operation_result.attempts_used, latencyMs=elapsed_ms, responseMode='two_agent_revision', changedCellCount=diagnostics.get('changedCellCount'), candidateCount=diagnostics.get('candidateCount'))
    return replace(operation_result, attempts_used=attempts_used + operation_result.attempts_used, latency_ms=elapsed_ms, guidance=guidance, revision_plan=plan.as_dict(), revision_contract=revision_contract, proposal_diagnostics=diagnostics)

def _bind_execution_brief_to_plan(plan, execution_brief):
    """Make explicit card coordinates survive the semantic-agent handoff."""
    if not isinstance(execution_brief, dict):
        return plan
    transitions = tuple(((item['row'], item['column'], item['from'], item['to']) for item in execution_brief.get('requiredTransitions') or []))
    transition_entities = tuple((item.get('anchorEntity') for item in execution_brief.get('requiredTransitions') or []))
    if not transitions and (not execution_brief.get('effect')):
        return plan
    first = plan.strategies[0]
    focus_payload = execution_brief.get('focus')
    focus = first.focus
    if isinstance(focus_payload, dict):
        focus = Focus(focus_payload['row'], focus_payload['column'], focus_payload['radius'])
    operators = tuple(execution_brief.get('allowedOperators') or first.operators)
    preserve = frozenset(set(execution_brief.get('preserve') or first.preserve) | {'outer_shell', 'unrelated_areas'})
    minimum = max(1, len(transitions))
    if transitions and any(((before, after) in {('p', '.'), ('.', 'p'), ('s', '.'), ('.', 's'), ('t', '.'), ('.', 't')} for _, _, before, after in transitions)):
        minimum = max(2, minimum)
    edit_budget = max(first.edit_budget, minimum)
    bound_first = replace(first, effect=execution_brief.get('effect') or first.effect, focus=focus, operators=operators, preserve=preserve, edit_budget=min(12, edit_budget), required_transitions=transitions, required_transition_entities=transition_entities, anchor_entities=tuple(execution_brief.get('anchors') or first.anchor_entities), play_objective=execution_brief.get('playObjective') or first.play_objective)
    return replace(plan, strategies=(bound_first,))

def _validate_revision_plan_entities(plan, rows, entity_bindings=None):
    """Apply the same current-stage identity rules to the planner output."""
    if entity_bindings is None:
        return
    for index, strategy in enumerate(plan.strategies, start=1):
        data = strategy.as_dict()
        brief = {'schemaVersion': 1, 'effect': data.get('effect'), 'anchors': data.get('anchorEntities') or [], 'focus': data.get('focus'), 'requiredTransitions': data.get('requiredTransitions') or [], 'allowedOperators': data.get('operators') or [], 'preserve': data.get('preserve') or [], 'playObjective': data.get('playObjective')}
        try:
            _validate_execution_brief(brief, rows, entity_bindings)
        except (TypeError, ValueError) as exception:
            raise ValueError(f'RevisionPlan strategy {index} is not bound to the current entity snapshot: {exception}') from exception

def _preflight_revision_strategies(plan, rows, stage_context, authorized_brief):
    """Drop only infeasible strategies and return field-level safe failures."""
    valid = []
    failures = []
    for index, strategy in enumerate(plan.strategies, start=1):
        partial = RevisionPlan((strategy,))
        try:
            validate_plan_scope(partial, ((stage_context or {}).get('proposalDiscovery') or {}).get('understanding'))
            validate_option_plan_scope(partial, (stage_context or {}).get('proposalDiscovery') or {})
            validate_revision_plan_against_map(rows, partial, (stage_context or {}).get('entityBindings'))
            _validate_revision_plan_entities(partial, rows, (stage_context or {}).get('entityBindings'))
            _build_revision_execution_contract(partial, authorized_brief, stage_context)
        except (TypeError, ValueError) as exception:
            failures.append({'strategyIndex': index, 'category': 'contract_preflight', 'reason': _safe_validation_reason(exception)[:600]})
            continue
        valid.append(strategy)
    return (RevisionPlan(tuple(valid)), failures)

def _build_revision_plan_messages(conversation, rows, language, stage_context, movement_requirement=None, preserved_components=None):
    response_language = 'Simplified Chinese' if language == 'zh-CN' else 'English'
    revision_brief = str(stage_context.get('authorizedRevisionBrief') or '').strip()
    alternative_brief = str(stage_context.get('alternativeRevisionBrief') or '').strip()
    execution_brief = stage_context.get('authorizedExecutionBrief') or {}
    original_brief = str(stage_context.get('relaxationOriginalBrief') or '').strip()
    relaxation_rule = 'This is a designer-approved fallback. Preserve the original core direction, but one coherent local effect is sufficient; do not weaken solvability, the outer shell, explicit prohibitions, or protection of unrelated areas.' if stage_context.get('revisionRelaxed') else 'Do not weaken or reinterpret the authorized direction.'
    conservative_binding_rule = 'The designer confirmed the experience direction but did not name the exact map objects. Choose exactly one existing entity combination or one uniquely identifiable connected region from the current Stage Snapshot. Prefer the smallest local change, preserve every unrelated component, and treat that binding only as this reviewable candidate, never as a confirmed designer instruction.' if stage_context.get('proposalBindingMode') == 'conservative' else 'If the direction is under-specified, do not guess; the caller will ask the designer for clarification.'
    movement_rule = _movement_requirement_prompt(movement_requirement)
    preservation_rule = _preserved_components_prompt(preserved_components)
    map_facts = _stage_snapshot_for_prompt(rows, stage_context)
    continuity_context = _continuity_context_prompt(stage_context, role='revision')
    edit_facts = ''
    numbered_map = ''
    system_prompt = "You are the Sokoban co-creation revision-planning assistant. Compile a designer-authorized semantic RevisionPlan for one saved 10-row × 12-column Stage into a detailed, executable RevisionPlan. Resolve the intended change into exact one-based coordinates and before/after tile states using only the authoritative map facts below. Do not generate map rows or tile operations. Do not generate full map rows; do not return map rows or tile operations; a separate level revision assistant may realize the frozen transitions, while the application owns all cell changes, structural validation, and solvability. Preserve the authorized direction and every explicit prohibition. Treat unmentioned areas as protected. Return JSON only with exactly one key, strategies, containing one to three objects. Prefer three materially different implementation strategies for the same core experience goal; vary local anchors, spatial mechanisms, or allowed operators, not the goal itself. If three genuinely different strategies are impossible, return fewer and never pad the list with duplicates. When a prior failure envelope is present, retain the core goal and every unrelaxed hard constraint, incorporate the designer's new supplement, and do not repeat failed anchors, transitions, operators, or concrete treatments. Every strategy has: effect, focus, operators, preserve, editBudget, metricGoals, requiredTransitions, anchorEntities, playObjective, and optional displayReason. effect is one of open_route, narrow_route, adjust_internal_walls, reshape_water. Player, boxes, targets, and the connected outer shell must stay fixed. focus is null or {row,column,radius}; coordinates are one-based, row 1..10, column 1..12, radius 1..3. operators contains one to four distinct values from add_wall, remove_wall, add_water, remove_water. preserve contains distinct values from outer_shell, player, boxes, targets, water, walls, unrelated_areas. Never list an operator that edits a preserved component. Each strategy must include requiredTransitions, anchorEntities (P, B1, B2, T1, T2), and playObjective. playObjective is null or one concise single-line string of at most 120 characters, such as route_choice, push_order, transport_length, spatial_balance, or route_readability; never return an object or list there. requiredTransitions must be the exact supplied transitions only when the structured execution brief already contains them; otherwise return an empty list so the modifier can explore concrete cells inside focus and the allowed operators. Never invent a hard coordinate binding from a qualitative experience goal. " + conservative_binding_rule + " When possible, set displayReason to a short, natural participant-facing explanation in the response language: say why this strategy fits the designer's request and what visual or play benefit it should create. Visual goals need not change push order or transport length; fixed entity anchors can frame water/wall composition. For a count increase, allow adding and removing that same authorized component to rearrange obstacles, unless the designer prohibited removal or fixed their positions. Preserve only designer-established restrictions; do not freeze an old route, obstacle total, or unspecified cells. Do not claim aesthetic success from solvability. Do not put coordinates, internal field names, metric identifiers, or implementation vocabulary in displayReason. This presentation field is optional and must not replace any execution field. focus must contain every required transition. editBudget is an integer 1..12; a single structural tile change may use budget 1, while moving a player, box, or target requires two paired cells. Set the budget to the smallest honest upper bound, never a range that cannot be satisfied. entity. metricGoals must be an empty list. The server separately derives hard measurable requirements from explicit designer wording; searchedStates is never a player-experience goal. Always preserve outer_shell, player, boxes, targets, and unrelated_areas. Choose a concrete focus for a local request, select operators that can realize the effect, and use metricGoals when the designer clearly requests a measurable change. The first strategy is preferred and any second strategy is a strict alternative, not permission to weaken the request. Do not output analysis, thinking, explanations, or markdown: emit the complete compact JSON object immediately. The current Stage Snapshot below is the only map source. Never use a coordinate from conversation text or an older Stage. " + _map_grounding_contract() + f' Interpret conversation in {response_language}.'
    user_prompt = f"{_design_context_prompt(stage_context, role='revision')}\n\n{continuity_context}\n\nAuthorized revision brief: {revision_brief!r}. Alternative proposal constraint: {alternative_brief!r}. Structured execution brief (authoritative when present): {json.dumps(execution_brief, ensure_ascii=False, separators=(',', ':'))}. Proposal discovery record (initial request, bound question answers, and supplements): {json.dumps(stage_context.get('proposalDiscovery') or {}, ensure_ascii=False, separators=(',', ':'))}. Clarification budget exhausted: {bool(stage_context.get('clarificationBudgetExhausted'))}. Read-only feasibility profile: {json.dumps(stage_context.get('feasibilityProfile') or {}, ensure_ascii=False, separators=(',', ':'))}. Server objective policy (authoritative): {json.dumps(stage_context.get('objectivePolicy') or {}, ensure_ascii=False, separators=(',', ':'))}. Original pre-fallback brief: {original_brief!r}. {relaxation_rule} {movement_rule} {preservation_rule}\n\nColumn ruler (one-based): 123456789012\nMap snapshot fingerprint for this planning pass: {map_fingerprint(rows)}\nCurrent Stage Snapshot (authoritative; 10 rows x 12 columns):\n{map_facts}\nAuthorized direction (no chat transcript): {revision_brief!r}"
    selected_route = proposal_options.planning_projection(stage_context.get('proposalDiscovery') or {})
    if selected_route:
        user_prompt += '\nSelected direction context (not additional user requirements): ' + json.dumps(selected_route, ensure_ascii=False)
        system_prompt += ' All strategies must realize the first selected direction in this context. Vary its local implementations only. Do not switch to another direction during repair/search, invent consent to hidden details, or claim an inferred preference was explicitly chosen.'
    record = stage_context.get('requirementRecord')
    if isinstance(record, dict):
        system_prompt = system_prompt.replace('second strategy is a strict alternative, not permission to weaken the request.', 'second strategy may prioritize a feasible subset of goals without reversing any direction.')
        system_prompt += " The typed requirement record below is the authoritative interpretation. Immutable requirements must all hold. Clear directional goals may be partly achieved or deferred if necessary; never reverse their direction. Try the user's explicitly prioritized goals first, then current focus, then maximize other goals. Do not claim that every goal is achieved. An explicit entity binding cannot be replaced. Use automaticBindings only for unspecified focus. Do not assert a necessary dependency from one solver route or claim player time will increase."
        user_prompt += '\nAuthoritative typed requirement record: ' + json.dumps(record, ensure_ascii=False)
    return [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}]

def _authorized_movement_requirement(conversation):
    messages = list(conversation or [])
    user_indexes = [index for index, message in enumerate(messages) if message.get('role') == 'user']
    if not user_indexes:
        return None
    latest_user_index = user_indexes[-1]
    latest_user = str(messages[latest_user_index].get('content') or '')
    if not latest_user or not _is_explicit_revision_authorization(latest_user):
        return None
    direct_requirement = _movement_requirement_from_text(latest_user)
    if direct_requirement is not None:
        return direct_requirement
    direction_source = _latest_role_content(messages[:latest_user_index], 'assistant')
    if not direction_source:
        return None
    return _movement_requirement_from_text(direction_source)

def _movement_requirement_from_text(value):
    source = str(value or '')
    lowered = source.casefold()
    if not source:
        return None
    if re.search("(?:不要|别|不(?:要|能)?|do not|don't)\\s*.{0,12}(?:向|往|to |right|left|up|down)", lowered):
        return None
    operator = None
    if '目标' in source or 'target' in lowered:
        operator = 'move_target'
    elif '箱' in source or 'box' in lowered:
        operator = 'move_box'
    elif any((marker in source for marker in ('玩家', '出生', '起点'))) or 'player' in lowered:
        operator = 'move_player'
    if operator is None:
        return None
    direction = _movement_direction_from_text(source, lowered)
    if direction is None:
        return None
    return {'operator': operator, 'direction': direction}

def _authorized_preserved_components(conversation, stage_context):
    record = (stage_context or {}).get('requirementRecord')
    if isinstance(record, dict):
        aliases = {'wall': 'walls', 'box': 'boxes', 'target': 'targets'}
        return frozenset((aliases.get(x['component'], x['component']) for x in record.get('requirements') or [] if x['strength'] == 'invariant' and x['property'] == 'positions'))
    messages = list(conversation or [])
    sources = tuple((str(message.get('content') or '') for message in messages if message.get('role') == 'user'))
    rules = {'water': ('水域', '水塘', '水面', 'water', 'pond'), 'walls': ('墙', '墙体', 'wall'), 'player': ('玩家', '起点', '出生', 'player', 'start'), 'boxes': ('箱子', '箱', 'box', 'crate'), 'targets': ('目标', '终点', '目标点', 'target', 'goal')}
    preserved = set()
    for component, markers in rules.items():
        if any((_text_explicitly_preserves_component(text, markers) for text in sources)):
            preserved.add(component)
    return frozenset(preserved)

def _text_explicitly_preserves_component(text, markers):
    value = str(text or '')
    lowered = value.casefold()
    preservation_words = ('不要动', '别动', '不改', '不改变', '保留', '保持', '维持', '不变', 'do not change', "don't change", 'do not move', "don't move", 'preserve', 'keep', 'leave unchanged', 'remain unchanged')
    for marker in markers:
        position = lowered.find(marker.casefold())
        while position >= 0:
            window = lowered[max(0, position - 18):position + len(marker) + 18]
            if any((word in window for word in preservation_words)):
                return True
            position = lowered.find(marker.casefold(), position + len(marker))
    return False

def _is_explicit_revision_authorization(message):
    text = str(message or '').casefold()
    return any((marker in text for marker in ('请根据这个方向生成', '生成一份可供审查', '请助手具体生成', '帮我改', '你来改吧', '按这个思路改', 'generate a reviewable', 'draft this revision', 'go ahead and revise', 'change it')))

def _movement_direction_from_text(source, lowered):
    chinese_phrases = ((('向右上', '往右上', '移到右上', '移动到右上', '挪到右上', '放到右上'), 'upper_right'), (('向左上', '往左上', '移到左上', '移动到左上', '挪到左上', '放到左上'), 'upper_left'), (('向右下', '往右下', '移到右下', '移动到右下', '挪到右下', '放到右下'), 'lower_right'), (('向左下', '往左下', '移到左下', '移动到左下', '挪到左下', '放到左下'), 'lower_left'), (('向右', '往右', '移到右边', '移动到右边', '挪到右边', '放到右边'), 'right'), (('向左', '往左', '移到左边', '移动到左边', '挪到左边', '放到左边'), 'left'), (('向下', '往下', '移到下方', '移动到下方', '挪到下方', '放到下方'), 'down'), (('向上', '往上', '移到上方', '移动到上方', '挪到上方', '放到上方'), 'up'))
    for markers, direction in chinese_phrases:
        if any((marker in source for marker in markers)):
            return direction
    english_patterns = (('\\b(?:move|shift|relocate)\\b.{0,48}\\b(?:to|toward)\\s+(?:the\\s+)?upper[- ]right\\b', 'upper_right'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\b(?:to|toward)\\s+(?:the\\s+)?upper[- ]left\\b', 'upper_left'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\b(?:to|toward)\\s+(?:the\\s+)?lower[- ]right\\b', 'lower_right'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\b(?:to|toward)\\s+(?:the\\s+)?lower[- ]left\\b', 'lower_left'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\bup and right\\b', 'upper_right'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\bup and left\\b', 'upper_left'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\bdown and right\\b', 'lower_right'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\bdown and left\\b', 'lower_left'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\b(?:to|toward)\\s+(?:the\\s+)?right\\b', 'right'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\b(?:to|toward)\\s+(?:the\\s+)?left\\b', 'left'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\b(?:to|toward)\\s+(?:the\\s+)?down\\b', 'down'), ('\\b(?:move|shift|relocate)\\b.{0,48}\\b(?:to|toward)\\s+(?:the\\s+)?up\\b', 'up'))
    for pattern, direction in english_patterns:
        if re.search(pattern, lowered):
            return direction
    return None

def _movement_requirement_prompt(requirement):
    if not requirement:
        return ''
    entity = {'move_target': 'target', 'move_box': 'box', 'move_player': 'player'}[requirement['operator']]
    direction = requirement['direction'].replace('_', ' ')
    return f'Hard verified movement requirement: the {entity} must move {direction} relative to its current cell. Use the corresponding move operator and choose a focus containing that destination; no other operator may substitute for this requirement.'

def _preserved_components_prompt(components):
    if not components:
        return ''
    labels = {'water': 'water', 'walls': 'internal walls', 'player': 'player', 'boxes': 'boxes', 'targets': 'targets'}
    named = ', '.join((labels[component] for component in sorted(components)))
    return f'Hard verified preservation requirement: do not change {named}, even if another strategy would make that easier.'

def _revision_plan_patch_request(payload, messages):
    payload = payload if isinstance(payload, dict) else {}
    candidate = {'strategies': payload.get('strategies')}
    strategies = candidate['strategies']
    targets = []
    required_types = {'effect': (str,), 'focus': (dict, type(None)), 'operators': (list,), 'preserve': (list,), 'editBudget': (int,), 'metricGoals': (list,)}
    if not isinstance(strategies, list) or not 1 <= len(strategies) <= 3:
        targets.append(Target('strategies', ('strategies',), (list,)))
    else:
        for index, strategy in enumerate(strategies):
            try:
                parse_revision_plan({'strategies': [strategy]})
            except (ValueError, TypeError, KeyError):
                missing = [field for field in required_types if isinstance(strategy, dict) and field not in strategy]
                if missing:
                    targets.extend((Target(f'strategy-{index + 1}.{field}', ('strategies', index, field), required_types[field]) for field in missing))
                else:
                    targets.append(Target(f'strategy-{index + 1}', ('strategies', index), (dict,)))
    if not targets:
        raise PatchError('PLAN_ENVELOPE_INVALID')
    return PatchRequest('plan', candidate, targets, {'planning_contract': messages})

@proposal_phase('plan', 'plan')
async def _compile_revision_plan(*, api_key, base_url, models, messages, request_id, started_at, deadline=None, max_attempts=PROPOSAL_GENERATION_ATTEMPTS, initial_validation_feedback=None, first_attempt_timeout=PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS):
    last_error = None
    attempt_failures = []
    validation_feedback = initial_validation_feedback
    first_failure_code = None
    repair_request = None
    raw_plan = {}
    svc = _current_service()
    deadline = deadline or min(_request_deadline(started_at), started_at + svc.config.proposal_llm_phase_timeout_seconds)
    for attempt in range(1, max_attempts + 1):
        if attempt == 1 or first_failure_code == 'MODEL_RESPONSE_INVALID':
            model = models[0]
        else:
            model = models[1] if len(models) > 1 else models[0]
        remaining = svc.remaining_until(deadline)
        if remaining <= 0:
            raise asyncio.TimeoutError()
        attempt_timeout = min(first_attempt_timeout if attempt == 1 else svc.config.proposal_plan_retry_timeout_seconds, remaining)
        response_fields = _empty_response_diagnostics()
        _log_llm_event('llm_attempt_started', requestId=request_id, task='map_proposal', model=model, attempt=attempt, maxAttempts=max_attempts, timeoutSeconds=round(attempt_timeout, 3), completionTokenLimit=PROPOSAL_PLAN_MAX_COMPLETION_TOKENS, deadlineRemainingSeconds=round(remaining, 3), responseMode='revision_plan')
        try:
            if repair_request is not None:
                raw_plan = await _request_component_patch(api_key, base_url, repair_request, _revision_plan_messages_with_feedback(messages, validation_feedback), request_id, deadline, 'revision_plan', PROPOSAL_PLAN_MAX_TOKENS, attempt_timeout)
                plan = parse_revision_plan(raw_plan)
                return (plan, attempt, model)
            response = await asyncio.wait_for(_request_completion(api_key, base_url, model, _revision_plan_messages_with_feedback(messages, validation_feedback), PROPOSAL_PLAN_MAX_TOKENS, attempt_timeout, task='revision_plan'), timeout=attempt_timeout)
            choice = response.choices[0]
            response_fields = _response_diagnostics(response, choice)
            if str(getattr(choice, 'finish_reason', '') or '') == 'length':
                raise ValueError('The RevisionPlan output reached its token limit.')
            content = str(choice.message.content or '')
            if not content.strip():
                raise EmptyModelResponse('The model returned an empty response.')
            raw_plan = parse_json_object(content)
            plan = parse_revision_plan(raw_plan)
            return (plan, attempt, model)
        except asyncio.TimeoutError:
            failure_reason = None
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not respond before the revision-plan attempt timeout.', request_id, True, attempt, 504)
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            last_error = classify_exception(exception, request_id, attempt)
            if last_error.code == 'MODEL_RESPONSE_INVALID' and failure_reason:
                last_error.safe_message = failure_reason[:1200]
                validation_feedback = failure_reason[:1200]
                try:
                    repair_request = _revision_plan_patch_request(raw_plan, messages)
                except PatchError:
                    last_error.details = {'task': 'revision_plan', 'failureStage': 'envelope', 'component': 'plan'}
                    break
        if attempt == 1:
            first_failure_code = last_error.code
        fields = {'requestId': request_id, 'task': 'map_proposal', 'model': model, 'attempt': attempt, 'code': last_error.code, 'retryable': last_error.retryable, 'latencyMs': int((svc.clock.monotonic() - started_at) * 1000), 'responseMode': 'revision_plan', 'failureClass': _llm_failure_class(last_error, failure_reason), **response_fields, **_provider_error_fields(last_error)}
        if failure_reason:
            fields['validationReason'] = failure_reason
        trace_proposal('plan', 'plan', 'revalidation_failed' if repair_request is not None else 'rejected', reason_code(last_error), attempt=attempt, maximumAttempts=max_attempts)
        if attempt >= max_attempts:
            trace_repair('plan', 'exhausted')
        _log_llm_event('llm_attempt_failed', **fields)
        attempt_failures.append({'attempt': attempt, 'code': last_error.code, 'failureClass': fields['failureClass'], 'finishReason': response_fields.get('finishReason'), 'completionTokens': response_fields.get('completionTokens'), 'validationReason': failure_reason})
        last_error.details = {'task': 'revision_plan', 'failureStage': 'revision_plan', 'component': 'plan', 'maximumAttempts': max_attempts, **getattr(last_error, 'details', {})}
        last_error.proposal_diagnostics = {'failureStage': 'revision_plan', 'attemptFailures': attempt_failures, 'remainingSeconds': round(svc.remaining_until(deadline), 3)}
        if not last_error.retryable:
            raise last_error
        if attempt == 1 and last_error.code not in {'MODEL_RESPONSE_INVALID', 'MODEL_EMPTY_RESPONSE', 'UPSTREAM_TIMEOUT', 'UPSTREAM_CONNECTION_ERROR'}:
            raise last_error
        if attempt < max_attempts and (not _retry_budget_available(deadline, request_id=request_id, task='map_proposal', attempt=attempt, max_attempts=max_attempts, response_mode='revision_plan', fallback_reason=last_error.code)):
            break
    raise last_error

def _revision_plan_messages_with_feedback(messages, validation_feedback):
    if not validation_feedback:
        return messages
    corrected = [dict(message) for message in messages]
    instruction = f'The previous RevisionPlan was rejected for this safe reason: {validation_feedback} Return a fresh RevisionPlan JSON object. Keep the authorized brief, explicit prohibitions, and preserve-unlisted contract unchanged. Use only these effect/operator combinations: open_route with remove_wall/remove_water; narrow_route with add_wall/add_water; adjust_internal_walls with add_wall/remove_wall; or reshape_water with add_water/remove_water. Preserve player, boxes, targets, and the outer shell. Use only a listed effect and ensure at least one operator can realize it. Keep requiredTransitions empty for a qualitative request; include exact transitions only when the supplied structured execution brief already froze them. Set playObjective to null or one concise single-line string no longer than 120 characters; never return an object or list for playObjective. Optionally include displayReason as short natural participant-facing prose explaining the reason and expected play benefit, without coordinates, internal field names, metric identifiers, or implementation vocabulary. Do not invent coordinates. Do not return map rows or tile operations. If the prior response reached the token limit, stop reasoning and emit the complete compact JSON immediately. Return up to three non-duplicate strategies for the same authorized goal, replacing any strategy implicated by the validation feedback; include no explanatory prose.'
    corrected[0]['content'] = f"{corrected[0]['content']}\n\n{instruction}"
    return corrected

def _build_legacy_revision_execution_contract(stage_context=None):
    return {'schemaVersion': REVISION_CONTRACT_SCHEMA_VERSION, 'authorizedBrief': str((stage_context or {}).get('authorizedRevisionBrief') or '').strip()[:1200], 'revisionPlan': {'strategies': []}, 'strategies': [{'strategyIndex': 1, 'effect': 'adjust_internal_walls', 'focus': None, 'allowedOperators': ['add_wall', 'remove_wall', 'move_player', 'move_box', 'move_target', 'add_water', 'remove_water'], 'preserve': ['outer_shell', 'unrelated_areas'], 'minimumChangedCells': 1, 'maximumChangedCells': REVISION_MAX_CHANGED_CELLS, 'metricGoals': []}], 'explicitlyRelaxedByDesigner': False}

@proposal_phase('contract', 'contract', revalidates='plan')
def _build_revision_execution_contract(plan, authorized_brief, stage_context=None):
    validate_plan_scope(plan, ((stage_context or {}).get('proposalDiscovery') or {}).get('understanding'))
    validate_option_plan_scope(plan, (stage_context or {}).get('proposalDiscovery') or {})
    if (stage_context or {}).get('aiEditableTilesOnly'):
        allowed = {'add_wall', 'remove_wall', 'add_water', 'remove_water'}
        for strategy in plan.strategies:
            if set(strategy.operators) - allowed or any((before not in {'#', '.', '@'} or after not in {'#', '.', '@'} for _, _, before, after in strategy.required_transitions)):
                raise ProposalValidationError([{'fieldPath': 'strategies', 'reasonCode': 'PROTECTED_EDIT', 'sourceKind': 'server'}], message='New AI plans may edit water and internal walls only.')
    relocation_effects = {'relocate_start', 'relocate_box', 'relocate_target'}
    strategies = []
    for index, strategy in enumerate(plan.strategies, start=1):
        maximum_changed_cells = min(REVISION_MAX_CHANGED_CELLS, strategy.edit_budget)
        strategy_data = strategy.as_dict()
        entity_operators = {'move_player', 'move_box', 'move_target'}
        required_transitions = strategy_data.get('requiredTransitions') or []
        required_operators = {_operation_kind(item['from'], item['to']) for item in required_transitions}
        if required_transitions:
            minimum_changed_cells = max(2, len(required_transitions)) if required_operators.intersection(entity_operators) else len(required_transitions)
        else:
            minimum_changed_cells = 2 if strategy.effect in relocation_effects or set(strategy_data['operators']).intersection(entity_operators) else REVISION_MIN_CHANGED_CELLS
        strategies.append({'strategyIndex': index, 'effect': strategy_data['effect'], 'focus': strategy_data['focus'], 'focusRegions': [strategy_data['focus']] if strategy_data['focus'] else [], 'allowedOperators': strategy_data['operators'], 'preserve': strategy_data['preserve'], 'minimumChangedCells': minimum_changed_cells, 'maximumChangedCells': maximum_changed_cells, 'metricGoals': strategy_data['metricGoals'], 'requiredTransitions': required_transitions, 'anchorEntities': strategy_data.get('anchorEntities') or [], 'playObjective': strategy_data.get('playObjective'), 'displayReason': strategy_data.get('displayReason')})
    source_offer = (stage_context or {}).get('sourceProposalOffer') or {}
    source_workflow = source_offer.get('revisionWorkflow') if isinstance(source_offer, dict) and isinstance(source_offer.get('revisionWorkflow'), dict) else None
    if source_workflow:
        revision_workflow = dict(source_workflow)
        revision_workflow['status'] = 'authorized'
        if (stage_context or {}).get('requirementPolicyVersion') == 1 and (stage_context or {}).get('explicitAction') != 'execute_revision':
            revision_workflow['requirementRecord'] = stage_context['requirementRecord']
            revision_workflow['requirementPolicyVersion'] = 1
            revision_workflow.pop('goalOutcomes', None)
    elif (stage_context or {}).get('explicitAction') in {'execute_revision', 'challenge_revision', 'alternative_revision'}:
        revision_workflow = build_revision_workflow('', stage_context)
        revision_workflow.update({'objective': str(authorized_brief or '').strip()[:1200], 'semanticConstraints': [], 'mode': 'off'})
    else:
        revision_workflow = build_revision_workflow(authorized_brief, stage_context)
    workflow_minimum = int((revision_workflow.get('scope') or {}).get('minimumChangedCells') or 1)
    if revision_workflow.get('mode') == 'enforce':
        for strategy in strategies:
            strategy['minimumChangedCells'] = max(int(strategy.get('minimumChangedCells') or 1), workflow_minimum)
    contract = {'schemaVersion': REVISION_CONTRACT_SCHEMA_VERSION, 'authorizedBrief': str(authorized_brief or '').strip()[:1200], 'revisionPlan': plan.as_dict(), 'strategies': strategies, 'explicitlyRelaxedByDesigner': bool((stage_context or {}).get('revisionRelaxed')), 'revisionWorkflow': revision_workflow, 'aiEditableTilesOnly': bool((stage_context or {}).get('aiEditableTilesOnly'))}
    if (stage_context or {}).get('binaryPromises'):
        contract['revisionWorkflow']['binaryPromises'] = copy.deepcopy(stage_context['binaryPromises'])
    for strategy in contract['strategies']:
        if strategy['minimumChangedCells'] > strategy['maximumChangedCells']:
            raise ValueError(f"revision contract has an impossible changed-cell range: {strategy['minimumChangedCells']}..{strategy['maximumChangedCells']}")
    return contract

def _build_map_operation_messages(revision_contract, rows, language, stage_context, baseline_metrics=None):
    response_language = 'Simplified Chinese' if language == 'zh-CN' else 'English'
    numbered_map = ''
    map_facts = _stage_snapshot_for_prompt(rows, stage_context)
    focus_facts = []
    solver_evidence = _llm_solver_evidence(baseline_metrics or {})
    modifier_contract = _modifier_contract_view(revision_contract)
    system_prompt = f"""You are the Sokoban co-creation level revision assistant. The saved Stage has 10 rows × 12 columns and is immutable input. Execute only the supplied execution contract; do not reinterpret the designer's request, invent a broader goal, or use any conversation outside the contract. Return only concrete cell-operation candidates. The Stage Snapshot in this request is the only map source. The application constructs the complete map, enforces the contract, checks structure, and runs the deterministic solver. Every candidate must make a meaningful, coherent local change within the contract. Do not add unrelated cells just to make a diff. Never edit void cells or the connected outer shell. Never modify preserved components or cells outside the strategy focus. Keep the map structurally valid with exactly one player and matching box/target pairs. Produce up to three distinct candidates, each tagged with its strategyIndex. Coordinates are one-based. Each operation must contain row, column, and to, and may include from as a claim that the server will verify against the real before tile. Required transitions are the complete frozen edit set: when they are present, return exactly those transitions and do not add optional or compensating edits. Never replace a required remove_wall with a box/player move. The server objectivePolicy distinguishes hard metrics from a soft play objective. Hard metricGoals must be met. For visual_composition, create coherent local spacing, repetition, boundary or balance changes within the contract; a gameplay-route change is not required. The solver validates playability, never subjective aesthetic success. Net increases may remove old tiles and add more elsewhere within the contract. Goals may be partial or unchanged, never reversed. For a gameplay soft objective, try to create candidates whose changed area affects a route and changes the relevant push order, alternation, transport, or detour mechanism; do not add irrelevant cells merely to change a metric. Only water and internal walls may change. Player, boxes, targets and outer shell stay fixed, even if a supplied strategy claims otherwise. Return JSON only with exactly this shape: {{"candidates":[{{"strategyIndex":1,"operations":[{{"row":5,"column":6,"from":".","to":"#"}}]}}]}}. Each operations array contains one to 24 unique cells. Allowed destination tiles are #, . and @ only. Never overwrite a player, box, target, void or connected outer-shell cell. Natural-language reasoning is internal; any unavoidable text must use {response_language}."""
    user_prompt = f"The following contract is authoritative and is the only revision instruction. The map rows and facts below are the exact snapshot for this planning pass:\n{json.dumps(modifier_contract, ensure_ascii=False, separators=(',', ':'))}\n\nColumn ruler (one-based): 123456789012\nMap snapshot fingerprint for this execution pass: {map_fingerprint(rows)}\nCurrent Stage Snapshot (authoritative; 10 rows x 12 columns):\n{map_facts}\nDeterministic solver evidence (authoritative): {json.dumps(solver_evidence, ensure_ascii=False)}"
    return [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}]

def _modifier_contract_view(revision_contract):
    """Expose only the frozen, executable fields to the operation agent.

    The modifier is deliberately not given the Chat/RevisionPlan prose.  It
    receives the immutable cell contract and enough local constraints to
    produce an operation candidate that can be replayed by the server.
    """
    strategies = []
    for strategy in revision_contract.get('strategies') or []:
        strategies.append({'strategyIndex': strategy.get('strategyIndex'), 'effect': strategy.get('effect'), 'focus': strategy.get('focus'), 'focusRegions': list(strategy.get('focusRegions') or []), 'allowedOperators': list(strategy.get('allowedOperators') or []), 'preserve': list(strategy.get('preserve') or []), 'minimumChangedCells': strategy.get('minimumChangedCells'), 'maximumChangedCells': strategy.get('maximumChangedCells'), 'metricGoals': list(strategy.get('metricGoals') or []), 'requiredTransitions': [{key: item.get(key) for key in ('row', 'column', 'from', 'to', 'anchorEntity') if key in item} for item in strategy.get('requiredTransitions') or []], 'anchorEntities': list(strategy.get('anchorEntities') or []), 'playObjective': strategy.get('playObjective')})
    return {'schemaVersion': revision_contract.get('schemaVersion', 1), 'strategies': strategies, 'objectivePolicy': revision_contract.get('objectivePolicy') or {}, 'semanticConstraints': list((revision_contract.get('revisionWorkflow') or {}).get('semanticConstraints') or []), 'requirements': [{key: item[key] for key in ('requirementId', 'component', 'property', 'relation', 'strength', 'entities', 'value', 'unit', 'focused', 'scope') if key in item} for item in ((revision_contract.get('revisionWorkflow') or {}).get('requirementRecord') or {}).get('requirements') or []], 'automaticBindings': ((revision_contract.get('revisionWorkflow') or {}).get('requirementRecord') or {}).get('automaticBindings') or []}

def _operation_patch_request(payload, contract, rows):
    payload = payload if isinstance(payload, dict) else {}
    candidate = {'candidates': payload.get('candidates')}
    targets = []
    candidates = candidate['candidates']
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= PROPOSAL_CANDIDATE_LIMIT:
        targets.append(Target('candidates', ('candidates',), (list,)))
    else:
        for index, item in enumerate(candidates):
            if not isinstance(item, dict):
                targets.append(Target(f'candidate-{index + 1}', ('candidates', index), (dict,)))
                continue
            strategy = item.get('strategyIndex')
            if type(strategy) is not int or not 1 <= strategy <= len(contract.get('strategies') or []):
                targets.append(Target(f'candidate-{index + 1}.strategyIndex', ('candidates', index, 'strategyIndex'), (int,)))
            operations = item.get('operations')
            missing = []
            if isinstance(operations, list):
                for op_index, operation in enumerate(operations):
                    if isinstance(operation, dict) and type(operation.get('row')) is int and (type(operation.get('column')) is int) and ('to' not in operation):
                        missing.append(Target(f'candidate-{index + 1}.operation-{op_index + 1}.to', ('candidates', index, 'operations', op_index, 'to'), (str,)))
            targets.extend(missing or [Target(f'candidate-{index + 1}.operations', ('candidates', index, 'operations'), (list,))])
    return PatchRequest('operations', candidate, targets, {'execution_contract': contract, 'snapshot': build_stage_snapshot(rows)})

@proposal_phase('operation_candidates', 'operations')
async def _generate_map_operation_candidates(*, api_key, base_url, models, messages, base_rows, request_id, language, proposal_validator, revision_contract, baseline_metrics, started_at, deadline=None, candidate_evidence=None, excluded_map_fingerprints=None):
    last_error = None
    repair_request = None
    payload = {}
    validation_feedback = None
    attempted_candidate_count = 0
    rejected_operation_signatures = set()
    rejected_map_signatures = set()
    rejection_records = []
    excluded_map_fingerprints = set(excluded_map_fingerprints or ())
    deadline = deadline or _request_deadline(started_at)
    svc = _current_service()
    attempted_models = list(models[:svc.config.proposal_generation_attempts])
    while len(attempted_models) < svc.config.proposal_generation_attempts:
        attempted_models.append(models[0])
    for attempt, configured_model in enumerate(attempted_models, start=1):
        model = models[0] if validation_feedback is not None else configured_model
        remaining = svc.remaining_until(deadline)
        if remaining <= 0:
            raise asyncio.TimeoutError()
        attempt_timeout = min(svc.config.proposal_operation_attempt_timeout_seconds, remaining)
        response_fields = _empty_response_diagnostics()
        _log_llm_event('llm_attempt_started', requestId=request_id, task='map_proposal', model=model, attempt=attempt, maxAttempts=len(attempted_models), timeoutSeconds=round(attempt_timeout, 3), responseMode='operation_candidates')
        try:
            attempt_messages = _operation_messages_with_feedback(messages, validation_feedback)
            if repair_request is not None:
                payload = await _request_component_patch(api_key, base_url, repair_request, attempt_messages, request_id, deadline, 'operation_candidates', PROPOSAL_OPERATION_MAX_TOKENS, attempt_timeout)
                content = json.dumps(payload, ensure_ascii=False)
            else:
                response = await asyncio.wait_for(_request_completion(api_key, base_url, model, attempt_messages, PROPOSAL_OPERATION_MAX_TOKENS, attempt_timeout, task='operation_candidates'), timeout=attempt_timeout)
                choice = response.choices[0]
                if str(getattr(choice, 'finish_reason', '') or '') == 'length':
                    raise ValueError('The operation-candidate output reached its token limit.')
                response_fields = _response_diagnostics(response, choice)
                content = str(choice.message.content or '')
            if not content.strip():
                raise EmptyModelResponse('The model returned an empty response.')
            payload = json.loads(content)
            if isinstance(payload, dict) and isinstance(payload.get('candidates'), list):
                attempted_candidate_count = max(attempted_candidate_count, len(payload['candidates']))
            selected_rows, selected_index, candidate_count, selected_operations = _select_operation_candidate(payload, base_rows, revision_contract, proposal_validator, baseline_metrics, rejected_operation_signatures, rejected_map_signatures, rejection_records, candidate_evidence, excluded_map_fingerprints)
            latency_ms = int((svc.clock.monotonic() - started_at) * 1000)
            changed_cell_count = _changed_cell_count(base_rows, selected_rows)
            result = LLMExecutionResult(assistant_message='我整理了一份等待你审查的地图提案。' if language == 'zh-CN' else 'I prepared a map proposal for your review.', attempts_used=attempt, request_id=request_id, proposed_rows=list(selected_rows), modification_summary='', model=model, latency_ms=latency_ms, guidance={'move': 'deliver_revision', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'uiCues': []}, revision_contract=revision_contract, revision_operations=selected_operations, proposal_diagnostics={'candidateCount': candidate_count, 'validCandidates': 1, 'constructedCandidates': candidate_count, 'selectedCandidateIndex': selected_index, 'selectedStrategyIndex': _candidate_strategy_index(payload, selected_index), 'changedCellCount': changed_cell_count})
            _log_llm_event('llm_request_completed', requestId=request_id, task='map_proposal', outcome='success', model=model, attemptsUsed=attempt, latencyMs=latency_ms, responseMode='operation_candidates', candidateCount=candidate_count, selectedCandidateIndex=selected_index, changedCellCount=changed_cell_count, **response_fields)
            return result
        except asyncio.TimeoutError:
            failure_reason = None
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not respond before the attempt timeout.', request_id, True, attempt, 504)
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            last_error = classify_exception(exception, request_id, attempt)
            if last_error.code == 'MODEL_RESPONSE_INVALID' and failure_reason:
                last_error.safe_message = failure_reason[:1200]
                validation_feedback = json.dumps(rejection_records[-3:] or [{'reason': failure_reason[:500]}], ensure_ascii=False, separators=(',', ':'))[:1200]
                repair_request = _operation_patch_request(payload, revision_contract, base_rows)
        failure_fields = {'requestId': request_id, 'task': 'map_proposal', 'model': model, 'attempt': attempt, 'code': last_error.code, 'retryable': last_error.retryable, 'latencyMs': int((svc.clock.monotonic() - started_at) * 1000), 'responseMode': 'operation_candidates', 'failureClass': _llm_failure_class(last_error, failure_reason), **response_fields, **_provider_error_fields(last_error)}
        if failure_reason:
            failure_fields['validationReason'] = failure_reason[:1200]
        trace_proposal('operation_candidates', 'operations', 'rejected', reason_code(last_error), attempt=attempt, maximumAttempts=len(attempted_models))
        if attempt >= len(attempted_models):
            trace_repair('operations', 'exhausted')
        _log_llm_event('llm_attempt_failed', **failure_fields)
        if not last_error.retryable:
            raise last_error
        if attempt < len(attempted_models) and (not _retry_budget_available(deadline, request_id=request_id, task='map_proposal', attempt=attempt, max_attempts=len(attempted_models), response_mode='operation_candidates', fallback_reason=last_error.code)):
            break
    _log_llm_event('llm_request_completed', requestId=request_id, task='map_proposal', outcome='error', code=last_error.code, attemptsUsed=last_error.attempts_used, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), responseMode='operation_candidates', **_provider_error_fields(last_error))
    if last_error.code == 'MODEL_RESPONSE_INVALID':
        error = LLMServiceError('PROPOSAL_SEARCH_EXHAUSTED', 'The level revision assistant produced no executable candidate satisfying the revision contract.', request_id, False, last_error.attempts_used, 502)
        error.proposal_diagnostics = {'modifierFailure': str(last_error.safe_message)[:1200], 'attempts': last_error.attempts_used, 'constructedCandidates': attempted_candidate_count, 'validCandidates': 0, 'candidateCount': attempted_candidate_count, 'rejectionRecords': rejection_records[-12:], 'uniqueRejectedOperationSets': len(rejected_operation_signatures), 'uniqueRejectedMaps': len(rejected_map_signatures)}
        error.revision_contract = revision_contract
        raise error from last_error
    raise last_error

@proposal_phase('search', 'deterministic_search')
def _deterministic_revision_fallback(*, rows, plan, revision_contract, request_id, language, proposal_validator, baseline_metrics, movement_requirement, preserved_components, started_at, entity_bindings=None, deadline=None, excluded_map_fingerprints=None):
    """Use the bounded local search when the modifier model cannot supply a valid candidate."""
    svc = _current_service()
    result = search_revision_plan(rows, plan, proposal_validator or _solve_stage, baseline_metrics=baseline_metrics, deadline=min(deadline or _request_deadline(started_at), svc.clock.monotonic() + svc.config.proposal_search_deadline_seconds), movement_requirement=movement_requirement, preserved_components=preserved_components, entity_bindings=entity_bindings, excluded_map_fingerprints=excluded_map_fingerprints, clock=svc.clock)
    strategy_index = result.strategy_index
    selected_contract = (revision_contract.get('strategies') or [])[result.strategy_index - 1]
    operations = _operations_from_diff(rows, result.rows, include_from=bool(selected_contract.get('requiredTransitions')))
    if not operations:
        raise ProposalSearchExhausted({**result.diagnostics, 'failureReasons': {'empty_diff': 1}})
    verified_rows = execute_revision_operations(rows, operations, revision_contract, strategy_index)
    if tuple(verified_rows) != tuple(result.rows):
        raise ProposalSearchExhausted({**result.diagnostics, 'failureReasons': {'contract_replay_mismatch': 1}})
    diagnostics = dict(result.diagnostics or {})
    diagnostics.update({'source': 'deterministic_search', 'selectedCandidateIndex': None, 'selectedStrategyIndex': strategy_index, 'changedCellCount': _changed_cell_count(rows, result.rows), 'candidateCount': diagnostics.get('constructedCandidates', 0)})
    return LLMExecutionResult(assistant_message='我整理了一份等待你审查的地图提案。' if language == 'zh-CN' else 'I prepared a map proposal for your review.', attempts_used=0, request_id=request_id, proposed_rows=list(result.rows), modification_summary='', model='deterministic-revision-search', latency_ms=diagnostics.get('elapsedMs', 0), guidance={'move': 'deliver_revision', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'uiCues': []}, revision_contract=revision_contract, revision_operations=operations, proposal_diagnostics=diagnostics)

def _operations_from_diff(before_rows, after_rows, include_from=False):
    operations = []
    for y, (before_row, after_row) in enumerate(zip(before_rows, after_rows)):
        for x, (before, after) in enumerate(zip(before_row, after_row)):
            if before == after:
                continue
            operation = {'row': y + 1, 'column': x + 1, 'to': after}
            if include_from:
                operation['from'] = before
            operations.append(operation)
    return operations

def _operation_messages_with_feedback(messages, validation_feedback):
    if not validation_feedback:
        return messages
    corrected = [dict(message) for message in messages]
    instruction = f'Every candidate in the previous attempt was rejected for these safe reasons: {validation_feedback} Return a fresh candidates JSON object. Do not reuse a rejected operation set, do not return the original map unchanged, and keep the authorized brief and preserve-unlisted contract unchanged.'
    corrected[0]['content'] = f"{corrected[0]['content']}\n\n{instruction}"
    return corrected

@proposal_phase('candidate_validation', 'candidate', revalidates='operations')
def _select_operation_candidate(payload, base_rows, revision_contract, proposal_validator, baseline_metrics=None, rejected_operation_signatures=None, rejected_map_signatures=None, rejection_records=None, candidate_evidence=None, excluded_map_fingerprints=None):
    if not isinstance(payload, dict):
        raise ValueError('The map proposal must be a JSON object.')
    candidates = payload.get('candidates')
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= PROPOSAL_CANDIDATE_LIMIT:
        raise ValueError('candidates must contain one to three operation candidates.')
    failures = []
    rejected_operation_signatures = rejected_operation_signatures if rejected_operation_signatures is not None else set()
    rejected_map_signatures = rejected_map_signatures if rejected_map_signatures is not None else set()
    rejection_records = rejection_records if rejection_records is not None else []
    candidate_evidence = candidate_evidence if candidate_evidence is not None else {}
    excluded_map_fingerprints = set(excluded_map_fingerprints or ())
    valid = []
    strategies = revision_contract.get('strategies') or []
    if not strategies:
        raise ValueError('The revision contract must contain at least one strategy.')
    for index, candidate in enumerate(candidates, start=1):
        operation_hash = None
        map_hash = None
        rows = None
        validation = None
        operations = None
        strategy_index = None
        try:
            if not isinstance(candidate, dict) or set(candidate) != {'strategyIndex', 'operations'}:
                raise ValueError('candidate must be an object')
            strategy_index = candidate['strategyIndex']
            if isinstance(strategy_index, bool) or not isinstance(strategy_index, int) or (not 1 <= strategy_index <= len(strategies)):
                raise ValueError('candidate strategyIndex is invalid')
            strategy = strategies[strategy_index - 1]
            operations = candidate.get('operations')
            operation_signature = _canonical_operation_signature(operations)
            operation_hash = hashlib.sha256(repr(operation_signature).encode('utf-8')).hexdigest()[:16]
            if operation_signature in rejected_operation_signatures:
                raise ValueError('candidate repeats an operation set rejected in an earlier attempt')
            rejected_operation_signatures.add(operation_signature)
            rows = execute_revision_operations(base_rows, operations, revision_contract, strategy_index)
            signature = tuple(rows)
            full_map_hash = map_fingerprint(rows)
            map_hash = full_map_hash[:16]
            if full_map_hash in excluded_map_fingerprints:
                raise ValueError('candidate duplicates the cited proposal result')
            if signature in rejected_map_signatures:
                raise ValueError('candidate duplicates an earlier operation result')
            rejected_map_signatures.add(signature)
            changed_cells = _changed_cell_count(base_rows, rows)
            if not strategy['minimumChangedCells'] <= changed_cells <= strategy['maximumChangedCells']:
                raise ValueError(f"candidate changed {changed_cells} cells; expected {strategy['minimumChangedCells']}..{strategy['maximumChangedCells']}")
            if proposal_validator is not None:
                validation = proposal_validator(rows)
            else:
                validation = _solve_stage(rows)
            _validate_metric_goals(strategy.get('metricGoals') or [], baseline_metrics or {}, validation)
            evidence = candidate_evidence.get(full_map_hash) or {}
            positive_deltas = [value for value in (evidence.get('metricDeltas') or {}).values() if isinstance(value, int) and value > 0]
            valid.append((tuple(evidence['goalRank']) + (-changed_cells, -strategy_index, tuple(rows)) if 'goalRank' in evidence else (int(bool(evidence.get('passed', True))), len(positive_deltas), sum(positive_deltas), -changed_cells, -strategy_index, tuple(rows)), rows, index, list(operations)))
        except Exception as exception:
            reason = _safe_validation_reason(exception)
            trace_proposal('candidate_validation', 'candidate', 'rejected', reason_code(exception), attempt=index, issues=safe_issues(exception))
            failures.append(f'candidate {index}: {reason}')
            metric_detail = {}
            if isinstance(exception, HardObjectiveError):
                metric_detail = {'metric': exception.metric, 'direction': exception.direction, 'baselineValue': exception.before, 'candidateValue': exception.after, 'minimumDelta': exception.minimum_delta, 'metricVerifiable': exception.verifiable}
            rejection_records.append({'candidate': index, 'category': _candidate_rejection_category(exception), 'reason': reason[:500], **({'operationSignature': operation_hash} if operation_hash else {}), **({'mapFingerprint': map_hash} if map_hash else {}), **metric_detail, **({'recoverySuggestionId': f'suggestion-{index}', 'hiddenCandidate': {'baseMapFingerprint': map_fingerprint(base_rows), 'candidateMapFingerprint': map_fingerprint(rows), 'operations': list(operations or []), 'strategyIndex': strategy_index, 'revisionContract': {**json.loads(json.dumps(revision_contract)), 'objectivePolicy': {**json.loads(json.dumps(revision_contract.get('objectivePolicy') or {})), 'hardMetricGoals': []}}, 'validation': validation.as_dict() if hasattr(validation, 'as_dict') else {}, 'mechanismEvidence': candidate_evidence.get(map_fingerprint(rows)) or {}}} if isinstance(exception, HardObjectiveError) and exception.verifiable and (rows is not None) else {})})
    if valid:
        _, rows, index, operations = max(valid, key=lambda item: item[0])
        return (rows, index, len(candidates), operations)
    raise ValueError('; '.join(failures)[:1200])

def _canonical_operation_signature(operations):
    if not isinstance(operations, list):
        return ('invalid', repr(operations)[:200])
    normalized = []
    for item in operations:
        if not isinstance(item, dict):
            normalized.append(('invalid', repr(item)[:120]))
            continue
        normalized.append((item.get('row'), item.get('column'), item.get('from'), item.get('to')))
    return tuple(sorted(normalized, key=repr))

def _candidate_rejection_category(exception):
    if isinstance(exception, SemanticConstraintError):
        return 'semantic_contract'
    if isinstance(exception, ObjectiveEvidenceError):
        return 'soft_objective_evidence_missing'
    if isinstance(exception, HardObjectiveError):
        return 'hard_objective_not_met'
    code = str(getattr(exception, 'code', '') or '')
    if code:
        if code == 'UNSOLVABLE_LEVEL':
            return 'candidate_unsolvable'
        if 'BUDGET' in code:
            return 'solver_budget_exhausted'
        return code.casefold()
    text = _safe_validation_reason(exception).casefold()
    if 'metric goal' in text:
        return 'hard_objective_not_met'
    if 'duplicate' in text or 'repeats' in text:
        return 'candidate_duplicated'
    if 'contract' in text or 'outside' in text or 'preserved' in text:
        return 'contract_mismatch'
    return 'candidate_invalid'

def _candidate_strategy_index(payload, candidate_index):
    candidates = payload.get('candidates') if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not 1 <= candidate_index <= len(candidates):
        return None
    candidate = candidates[candidate_index - 1]
    return candidate.get('strategyIndex') if isinstance(candidate, dict) else None

@proposal_phase('execution_replay', 'execution')
def execute_revision_operations(base_rows, operations, revision_contract, strategy_index):
    """Apply one modifier-agent candidate under the immutable execution contract."""
    if not isinstance(operations, list) or any((not isinstance(operation, dict) or set(operation) not in ({'row', 'column', 'to'}, {'row', 'column', 'from', 'to'}) for operation in operations)):
        raise ValueError('modifier operations must contain only row, column, and to, with optional from')
    operations = restore_operation_sources(operations, base_rows)
    strategies = (revision_contract or {}).get('strategies') or []
    if isinstance(strategy_index, bool) or not isinstance(strategy_index, int) or (not 1 <= strategy_index <= len(strategies)):
        raise ValueError('the selected revision strategy is invalid')
    strategy = strategies[strategy_index - 1]
    rows = _apply_map_operations(base_rows, operations, strategy_contract=strategy)
    if (revision_contract or {}).get('aiEditableTilesOnly'):
        validate_ai_edit_diff(base_rows, rows)
    changed_cells = _changed_cell_count(base_rows, rows)
    minimum = int(strategy.get('minimumChangedCells') or 0)
    maximum = int(strategy.get('maximumChangedCells') or REVISION_MAX_CHANGED_CELLS)
    if not minimum <= changed_cells <= maximum:
        raise ValueError(f'candidate changed {changed_cells} cells; expected {minimum}..{maximum}')
    return rows

@proposal_phase('permission_validation', 'permissions')
def validate_ai_edit_diff(base_rows, candidate_rows):
    """Fixed platform policy, independent of any model-authored permission."""
    if len(base_rows) != len(candidate_rows) or any((len(a) != len(b) for a, b in zip(base_rows, candidate_rows))):
        raise ProposalValidationError([{'fieldPath': 'candidate', 'reasonCode': 'MAP_SHAPE_CHANGED', 'sourceKind': 'server'}])
    shell = _connected_outer_shell(base_rows)
    for y, (before_row, after_row) in enumerate(zip(base_rows, candidate_rows)):
        for x, (before, after) in enumerate(zip(before_row, after_row)):
            if before != after and (before not in {'#', '.', '@'} or after not in {'#', '.', '@'} or (x, y) in shell):
                raise ProposalValidationError([{'fieldPath': 'candidate', 'reasonCode': 'PROTECTED_EDIT', 'sourceKind': 'server'}], message='AI revisions may edit water and internal walls only.')

@proposal_phase('execution_replay', 'execution')
def _deterministic_exact_revision(rows, execution_brief, request_id, language, proposal_validator, entity_bindings=None):
    """Execute a frozen transition contract without asking an LLM to guess cells."""
    brief = _validate_execution_brief(execution_brief, rows, entity_bindings)
    transitions = brief['requiredTransitions']
    if not transitions:
        return None
    operator_by_transition = {('.', '#'): 'add_wall', ('#', '.'): 'remove_wall', ('.', '@'): 'add_water', ('@', '.'): 'remove_water', ('p', '.'): 'move_player', ('.', 'p'): 'move_player', ('s', '.'): 'move_box', ('.', 's'): 'move_box', ('t', '.'): 'move_target', ('.', 't'): 'move_target'}
    operators = list(dict.fromkeys((operator_by_transition[item['from'], item['to']] for item in transitions)))
    if len(operators) > 4:
        raise LLMServiceError('PROPOSAL_SEARCH_EXHAUSTED', 'The frozen transition contract is too broad for one local proposal.', request_id, False, 0, 422)
    effect = brief.get('effect')
    effect_by_operator = {'add_wall': 'adjust_internal_walls', 'remove_wall': 'open_route', 'add_water': 'reshape_water', 'remove_water': 'open_route', 'move_player': 'relocate_start', 'move_box': 'relocate_box', 'move_target': 'relocate_target'}
    if effect is None:
        effect = effect_by_operator[operators[0]]
    focus = brief.get('focus')
    entity_operator = any((operator.startswith('move_') for operator in operators))
    edit_budget = max(len(transitions), 2 if entity_operator else 1)
    plan = parse_revision_plan({'strategies': [{'effect': effect, 'focus': focus, 'operators': operators, 'preserve': brief.get('preserve') or [], 'editBudget': edit_budget, 'metricGoals': [], 'requiredTransitions': transitions, 'anchorEntities': brief.get('anchors') or [], 'playObjective': brief.get('playObjective')}]})
    validate_revision_plan_against_map(rows, plan, entity_bindings)
    _validate_revision_plan_entities(plan, rows, entity_bindings)
    contract = _build_revision_execution_contract(plan, '', {})
    operations = [{'row': item['row'], 'column': item['column'], 'from': item['from'], 'to': item['to']} for item in transitions]
    proposed_rows = execute_revision_operations(rows, operations, contract, 1)
    if proposal_validator is not None:
        proposal_validator(proposed_rows)
    changed = _changed_cell_count(rows, proposed_rows)
    return LLMExecutionResult(assistant_message='', attempts_used=0, request_id=request_id, model='deterministic-exact-contract', proposed_rows=proposed_rows, revision_plan=plan.as_dict(), revision_contract=contract, revision_operations=operations, proposal_diagnostics={'source': 'deterministic_contract', 'selectedStrategyIndex': 1, 'changedCellCount': changed, 'candidateCount': 1, 'modifierAttempts': 0}, guidance={'move': 'deliver_revision', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': []})

def _apply_map_operations(base_rows, operations, revision_brief=None, strategy_contract=None):
    if not isinstance(operations, list) or not 1 <= len(operations) <= PROPOSAL_OPERATION_LIMIT:
        raise ValueError('operations must contain one to 24 cell changes')
    allowed_tiles = set(' #.@pst')
    mutable = [list(row) for row in base_rows]
    seen = {}
    shell = _connected_outer_shell(base_rows)
    normalized = []
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) not in ({'row', 'column', 'to'}, {'row', 'column', 'from', 'to'}):
            raise ValueError('each operation must contain row, column, and to')
        row = operation['row']
        column = operation['column']
        after = operation['to']
        if isinstance(row, bool) or isinstance(column, bool) or (not isinstance(row, int)) or (not isinstance(column, int)):
            raise ValueError('operation coordinates must be integers')
        y, x = (row - 1, column - 1)
        if not (0 <= y < len(base_rows) and 0 <= x < len(base_rows[y])):
            raise ValueError('operation coordinate is outside the 10-row × 12-column Stage')
        before = base_rows[y][x]
        declared_before = operation.get('from')
        if declared_before is not None and declared_before != before:
            raise ValueError(f'row {row}, column {column} does not match its declared from tile')
        if before not in allowed_tiles or after not in allowed_tiles or len(before) != 1 or (len(after) != 1):
            raise ValueError('operation contains an unsupported tile')
        if (x, y) in seen:
            if seen[x, y] == after:
                continue
            raise ValueError('duplicate operation coordinates conflict')
        seen[x, y] = after
        if before == after:
            continue
        if before == ' ' or after == ' ':
            raise ValueError('void cells cannot be edited')
        if (x, y) in shell:
            raise ValueError('the connected outer shell cannot be edited')
        mutable[y][x] = after
        normalized.append((x, y, before, after))
    if not normalized:
        raise ValueError('operations must contain at least one real tile change')
    if strategy_contract is not None:
        _validate_operation_contract(normalized, strategy_contract)
    else:
        _validate_operation_intent_scope(normalized, revision_brief)
    return [''.join(row) for row in mutable]

def _operation_kind(before, after):
    return {('.', '#'): 'add_wall', ('#', '.'): 'remove_wall', ('p', '.'): 'move_player', ('.', 'p'): 'move_player', ('s', '.'): 'move_box', ('.', 's'): 'move_box', ('t', '.'): 'move_target', ('.', 't'): 'move_target', ('.', '@'): 'add_water', ('@', '.'): 'remove_water'}.get((before, after))

def _changed_cell_count(before_rows, after_rows):
    return sum((before != after for before_row, after_row in zip(before_rows, after_rows) for before, after in zip(before_row, after_row)))

def _validate_operation_contract(operations, strategy):
    effect_operators = {'open_route': {'remove_wall', 'remove_water'}, 'narrow_route': {'add_wall', 'add_water'}, 'adjust_internal_walls': {'add_wall', 'remove_wall'}, 'relocate_start': {'move_player'}, 'relocate_box': {'move_box'}, 'relocate_target': {'move_target'}, 'reshape_water': {'add_water', 'remove_water'}, 'change_box_order': {'move_box', 'move_target', 'add_wall', 'remove_wall'}}
    component_by_operator = {'move_player': 'player', 'move_box': 'boxes', 'move_target': 'targets', 'add_water': 'water', 'remove_water': 'water'}
    allowed = set(strategy.get('allowedOperators') or [])
    preserved = set(strategy.get('preserve') or [])
    observed = set()
    positions = []
    operator_counts = {}
    for x, y, before, after in operations:
        operator = _operation_kind(before, after)
        if operator is None:
            raise ValueError(f'unsupported tile transition {before!r} to {after!r}')
        if operator not in allowed:
            raise ValueError(f'operation {operator} is outside the revision contract')
        component = component_by_operator.get(operator)
        if component and component in preserved:
            raise ValueError(f'operation changes preserved component {component}')
        if operator in {'add_wall', 'remove_wall'} and 'walls' in preserved:
            raise ValueError('operation changes preserved walls')
        focus = strategy.get('focus')
        if focus is not None:
            if max(abs(focus['column'] - 1 - x), abs(focus['row'] - 1 - y)) > focus['radius']:
                raise ValueError('operation is outside the revision focus')
        observed.add(operator)
        positions.append((x, y))
        operator_counts[operator] = operator_counts.get(operator, 0) + 1
    expected = effect_operators.get(strategy.get('effect'), set())
    if not observed.intersection(expected):
        raise ValueError('operations do not realize the planned effect')
    for operator in ('move_player', 'move_box', 'move_target'):
        if operator_counts.get(operator, 0) % 2:
            raise ValueError(f'{operator} requires paired source and destination operations')
    if len(set(positions)) != len(positions):
        raise ValueError('the revision contract cannot contain duplicate changed cells')
    required = {(item.get('row'), item.get('column'), item.get('from'), item.get('to')) for item in strategy.get('requiredTransitions') or []}
    if required:
        observed_transitions = {(y + 1, x + 1, before, after) for x, y, before, after in operations}
        if observed_transitions != required:
            raise ValueError('operations must exactly match the frozen required tile transitions')

def _validate_metric_goals(goals, baseline_metrics, validation):
    values = {'solutionSteps': getattr(validation, 'solution_steps', None), 'solutionPushes': getattr(validation, 'solution_pushes', None), 'minimumPushes': None, 'searchedStates': getattr(validation, 'searched_states', None)}
    for goal in goals:
        metric = goal.get('metric')
        if metric == 'minimumPushes':
            try:
                values[metric] = minimum_pushes(validation.rows, clock=_active_clock())
            except Exception:
                values[metric] = None
        before = baseline_metrics.get(metric)
        after = values.get(metric)
        if before is None or after is None:
            raise HardObjectiveError(metric, goal.get('direction'), before, after, int(goal.get('minimumDelta') or 1), verifiable=False)
        direction = goal.get('direction')
        minimum_delta = int(goal.get('minimumDelta') or 1)
        matched = {'increase': after - before >= minimum_delta, 'decrease': before - after >= minimum_delta, 'preserve': after == before}.get(direction, False)
        if not matched:
            raise HardObjectiveError(metric, direction, before, after, minimum_delta)

def _connected_outer_shell(rows):
    pending = []
    visited = set()
    height = len(rows)
    width = len(rows[0]) if rows else 0
    for y, row in enumerate(rows):
        for x, tile in enumerate(row):
            if tile == '#' and (x in {0, width - 1} or y in {0, height - 1}):
                pending.append((x, y))
    while pending:
        position = pending.pop()
        if position in visited:
            continue
        visited.add(position)
        x, y = position
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and (rows[ny][nx] == '#'):
                pending.append((nx, ny))
    return visited

def _validate_operation_intent_scope(operations, revision_brief):
    text = str(revision_brief or '').casefold()
    touched = {tile for _, _, before, after in operations for tile in (before, after)}
    component_markers = ((('目标', '落点', 'target', 'goal'), 't', 'target'), (('箱', 'box', 'crate'), 's', 'box'), (('水', 'water', 'pond'), '@', 'water'), (('墙', 'wall'), '#', 'wall'), (('玩家', '起点', 'player start'), 'p', 'player'))
    for markers, tile, label in component_markers:
        if _brief_requests_component_change(text, markers) and tile not in touched:
            raise ValueError(f'the operations do not change the explicitly requested {label}')
    positions = [(x, y) for x, y, _, _ in operations]
    region_rules = ((('下半', '下方', '底部', 'lower', 'bottom'), lambda x, y: y >= 5, 'lower area'), (('上半', '上方', '顶部', 'upper', 'top'), lambda x, y: y < 5, 'upper area'), (('左侧', '左边', 'left'), lambda x, y: x < 6, 'left area'), (('右侧', '右边', 'right'), lambda x, y: x >= 6, 'right area'))
    matched_regions = [(predicate, label) for markers, predicate, label in region_rules if any((marker in text for marker in markers))]
    if len(matched_regions) == 1:
        predicate, label = matched_regions[0]
        if not any((predicate(x, y) for x, y in positions)):
            raise ValueError(f'the operations do not touch the explicitly requested {label}')

def _brief_requests_component_change(text, markers):
    action_pattern = re.compile('移动|调整|重排|增加|减少|移除|改变|拉开|收紧|添加|删除|改动|修改|move|shift|adjust|rearrange|add|remove|reduce|increase|change|reshape|revise')
    preserve_pattern = re.compile('保持|保留|不动|不改|preserve|keep|unchanged')
    for marker in markers:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            window = text[max(0, index - 10):index + len(marker) + 10]
            if action_pattern.search(window) and (not preserve_pattern.search(window)):
                return True
            start = index + len(marker)
    return False

def _proposal_summary_is_substantive(summary, language):
    value = re.sub('\\s+', '', str(summary or '')).casefold()
    if not value:
        return False
    if _proposal_card_is_meta_language(summary):
        return False
    confirmations = {'好', '好的', '可以', '行', '同意', '就这样', '没问题', '嗯', 'ok', 'okay', 'yes', 'yep', 'soundsgood', 'agreed'}
    if value in confirmations:
        return False
    chinese = language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', value)
    anchors = ('水', '箱', '目标', '墙', '通道', '路线', '推进', '区域', '位置') if chinese else ('water', 'box', 'crate', 'target', 'goal', 'wall', 'corridor', 'route', 'push', 'area', 'position')
    return len(value) >= (4 if chinese else 8) and any((anchor in value for anchor in anchors))

def _distill_proposal_offer(proposal_offer, visible_content, previous_content, language):
    if not proposal_offer:
        return None
    original_summary = str(proposal_offer.get('summary') or '').strip()
    original_rationale = str(proposal_offer.get('rationale') or '').strip()
    execution_brief = proposal_offer.get('executionBrief')
    summary_was_meta_language = _proposal_card_is_meta_language(original_summary)
    rationale_was_meta_language = _proposal_card_is_meta_language(original_rationale)
    if summary_was_meta_language:
        original_summary = ''
    if rationale_was_meta_language:
        original_rationale = ''
    visible_action = _revision_direction_sentence(visible_content)
    if (summary_was_meta_language or rationale_was_meta_language) and (not visible_action):
        return None
    if visible_action and (not _proposal_summary_has_concrete_action(original_summary, language)):
        original_summary = visible_action
    if _proposal_summary_is_substantive(original_summary, language):
        grounded_offer = _semantics_preserving_proposal_offer(original_summary, original_rationale, visible_content, language)
        if grounded_offer is not None:
            return _attach_execution_brief(_ensure_proposal_offer_explanation(grounded_offer, visible_content, language), execution_brief)
    corpus = ' '.join((part for part in (visible_content, previous_content, original_summary, original_rationale) if part))
    lowered = corpus.casefold()
    chinese = language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', corpus)
    if chinese:
        has_water = '水' in corpus
        has_target = '目标' in corpus
        has_box = '箱' in corpus
        has_wall = '墙' in corpus
        lower_area = any((word in corpus for word in ('下方', '下半', '右下', '底部', '空旷')))
        route = any((word in corpus for word in ('路线', '通道', '绕', '推进', '推动')))
        box_spacing = has_box and any((word in corpus for word in ('错开', '挪', '旁边', '一格', '对着')))
        if box_spacing:
            summary = '错开相邻箱子的推进位置'
            rationale = '我会只调整其中一只箱子的相对位置，让另一只箱子先获得可读的推进空间；然后观察这个错开是否真的带来路线选择，而不是只改变画面。'
        elif has_target and lower_area:
            summary = '重排下半区的目标落点与推进路线'
            rationale = '我会把判断重点放在目标下移后的第一次推进：它是否让下方箱子承担新的绕行，同时让上下区域的操作密度更均衡，而不是只把空地填满。'
        elif has_water and has_target:
            summary = '让目标落点与水域形成路线联动'
            rationale = '我想让玩家在箱子第一次靠近目标时就感到水域正在改变进入方向；这样既保留空间感，也能用实际推动判断水域是否真正参与了解法。'
        elif has_water and (has_box or route):
            summary = '让水域参与箱子的首次路线判断'
            rationale = '这一步关注的不是水域面积本身，而是箱子贴近水边推进时，玩家是否必须重新读取绕行空间与推动顺序，从而让水域从背景变成有作用的路径条件。'
        elif has_wall and route:
            summary = '调整内部墙体形成的通道节奏'
            rationale = '我会观察箱子第一次进入调整区域时，墙体是否制造了清楚但不唯一的转折；这能帮助我们判断路线选择变丰富了，还是只是增加了额外移动。'
        elif has_box and has_target:
            summary = '重新组织箱子与目标之间的推进关系'
            rationale = '我想用第一次接近目标的推动来检验这项调整：玩家应该能读出新的先后关系，但仍保留一点需要亲手确认的路线犹豫。'
        else:
            summary = '把当前方向落实为可比较的局部调整'
            rationale = '我会让改动集中在刚才讨论的区域，并用第一次推动时的路线选择来判断它是否真正改变了体验，而不是只在视觉上显得不同。'
        title_limit = 42
    else:
        has_water = any((word in lowered for word in ('water', 'pond')))
        has_target = any((word in lowered for word in ('target', 'goal')))
        has_box = any((word in lowered for word in ('box', 'crate')))
        has_wall = 'wall' in lowered
        lower_area = any((word in lowered for word in ('lower', 'bottom', 'bottom-right', 'open area')))
        route = any((word in lowered for word in ('route', 'corridor', 'detour', 'push', 'path')))
        box_spacing = has_box and any((word in lowered for word in ('separate', 'space apart', 'shift', 'move over', 'one tile', 'beside')))
        if box_spacing:
            summary = "Separate the adjacent boxes' push positions"
            rationale = 'I would move only one box relative to the other so the remaining box has a legible first push, then judge whether that separation creates a real route choice rather than only a visual change.'
        elif has_target and lower_area:
            summary = 'Rebalance the lower targets and push routes'
            rationale = 'I would judge this through the first push after the target moves: whether it gives the lower box a meaningful detour and balances activity across the map, rather than merely filling empty floor.'
        elif has_water and has_target:
            summary = 'Link the target approach to the water'
            rationale = 'I want the water to change how the player enters the target route on the first approach, so play can tell us whether it has become a real path condition.'
        elif has_water and (has_box or route):
            summary = "Make water shape the box's first route choice"
            rationale = "The useful test is not the water's size but whether pushing beside it makes the player reread detour space and push order, turning scenery into a path condition."
        elif has_wall and route:
            summary = 'Reshape the corridor rhythm around the inner walls'
            rationale = "I would watch the box's first entry into this area to see whether the wall creates a legible but non-obvious turn instead of merely adding movement."
        elif has_box and has_target:
            summary = 'Reframe the push relationship between box and target'
            rationale = 'I would use the first target approach to judge whether the new order reads clearly while still leaving a route decision worth testing by hand.'
        else:
            summary = 'Turn the direction into a comparable local revision'
            rationale = 'I would keep the change around the area we discussed and judge it through the first route decision, so the result changes play rather than only appearance.'
        title_limit = 90
    summary_needs_distilling = len(original_summary) > title_limit or _guidance_reuses_visible_sentence(original_summary, visible_content) or _guidance_reuses_visible_sentence(original_summary, previous_content)
    rationale_needs_expansion = len(original_rationale) < (28 if chinese else 60) or _guidance_reuses_visible_sentence(original_rationale, visible_content) or _guidance_reuses_visible_sentence(original_rationale, previous_content) or _guidance_text_matches(original_summary, original_rationale)
    return _attach_execution_brief(_ensure_proposal_offer_explanation({'summary': (summary if summary_needs_distilling else original_summary)[:600], 'rationale': (rationale if rationale_needs_expansion else original_rationale)[:1000]}, visible_content, language), execution_brief)

@proposal_phase('freeze', 'brief')
def _attach_execution_brief(offer, execution_brief):
    if offer is None or execution_brief is None:
        return offer
    result = dict(offer)
    result['executionBrief'] = execution_brief
    return result

def _proposal_summary_has_concrete_action(summary, language):
    value = str(summary or '').strip()
    if not value:
        return False
    if _proposal_card_is_meta_language(value):
        return False
    lowered = value.casefold()
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', value):
        return bool(re.search('\\(\\s*\\d+\\s*,\\s*\\d+\\s*\\)', value)) or any((word in value for word in ('移', '挪', '调', '改', '加', '减', '删', '缩', '拉开', '收紧', '打开', '封住', '让', '形成', '制造')))
    return bool(re.search('\\b(?:row|column|tile)\\s*\\d+', lowered)) or any((word in lowered for word in ('move', 'shift', 'adjust', 'add', 'remove', 'reduce', 'expand', 'tighten', 'open', 'close')))

def _ensure_proposal_offer_explanation(offer, visible_content, language):
    summary = _strip_revision_summary_leadin(offer.get('summary') or '')[:600]
    rationale = str(offer.get('rationale') or '').strip()
    chinese = language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', f'{summary}{rationale}{visible_content}')
    visible_action = _revision_direction_sentence(visible_content)
    if visible_action and re.search('\\(\\s*\\d+\\s*,\\s*\\d+\\s*\\)', visible_action) and (not re.search('\\(\\s*\\d+\\s*,\\s*\\d+\\s*\\)', summary)):
        summary = _strip_revision_summary_leadin(visible_action)[:600]
    generic_markers = ('唯一改动方向', '实际格子变化', '只调整', '是否成立') if chinese else ('only revision direction', 'verified diff', 'only local cells', 'whether it works')
    should_expand = not rationale or any((marker in rationale.casefold() for marker in generic_markers)) or _guidance_text_matches(rationale, summary)
    if should_expand:
        sentences = [sentence.strip() for sentence in re.split('(?<=[。！？!?])\\s*', str(visible_content or '')) if sentence.strip()]
        explanation_markers = ('让', '使', '这样', '玩家', '推', '路线', '选择', '顺序') if chinese else ('make', 'let', 'so that', 'player', 'push', 'route', 'choice', 'order')
        explanation = next((sentence for sentence in sentences if any((marker in sentence.casefold() for marker in explanation_markers)) and summary not in sentence and (sentence not in summary)), '')
        if chinese:
            rationale = f"具体做法是：{summary.rstrip('。.!！')}。{explanation or '这样做是为了让相关箱子的第一次推进出现新的路线判断；试玩时需要确认它带来的是选择，而不只是外观变化。'}"
        else:
            rationale = f"Concrete change: {summary}. {explanation or 'The goal is to make the first relevant push create a new route judgment; play should confirm that it creates a choice rather than only a visual difference.'}"
    elif chinese and (not rationale.startswith('具体做法是：')):
        rationale = f'具体做法是：{summary}。{rationale}'
    elif not chinese and (not rationale.lower().startswith('concrete change:')):
        rationale = f'Concrete change: {summary}. {rationale}'
    return {'summary': summary, 'rationale': rationale[:1000]}

def _proposal_rationale_has_playable_effect(rationale, language):
    text = str(rationale or '').strip().casefold()
    if not text or _proposal_card_is_meta_language(text):
        return False
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', text):
        markers = ('玩家', '游玩', '试玩', '观察', '判断', '路线', '推箱', '推动', '选择', '顺序', '可读', '体验', '停留', '时间')
    else:
        markers = ('player', 'play', 'playtest', 'observe', 'judge', 'route', 'push', 'choice', 'order', 'read', 'experience', 'time')
    return any((marker in text for marker in markers))

def _proposal_body_has_playable_support(body, language):
    sentences = [sentence.strip() for sentence in re.split('(?<=[.!?。！？])\\s*|[\\r\\n]+', str(body or '')) if sentence.strip() and (not _proposal_card_is_meta_language(sentence))]
    text = ' '.join(sentences).strip().casefold()
    if len(text) < (18 if language == 'zh-CN' else 35):
        return False
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', text):
        anchors = ('水', '箱', '目标', '墙', '通道', '路线', '推动', '推箱', '空间', '难度', '时间', '游玩', '障碍')
        actions = ('移动', '调整', '挪', '改', '增加', '减少', '绕', '形成', '让', '使', '重排', '保留', '延长', '停留', '多花')
        effects = ('玩家', '游玩', '试玩', '观察', '判断', '路线', '选择', '顺序', '体验', '时间', '停留', '推')
    else:
        anchors = ('water', 'box', 'crate', 'target', 'wall', 'corridor', 'route', 'push', 'space', 'difficulty', 'time', 'play', 'obstacle')
        actions = ('move', 'adjust', 'shift', 'change', 'add', 'remove', 'detour', 'make', 'let', 'rearrange', 'preserve', 'extend', 'slow')
        effects = ('player', 'play', 'playtest', 'observe', 'judge', 'route', 'choice', 'order', 'experience', 'time', 'push')
    return any((marker in text for marker in anchors)) and any((marker in text for marker in actions)) and any((marker in text for marker in effects))

def _proposal_offer_has_exact_execution_plan(proposal_offer):
    """Return whether a proposal is bound to at least one exact tile transition."""
    if not isinstance(proposal_offer, dict):
        return False
    brief = proposal_offer.get('executionBrief')
    return isinstance(brief, dict) and isinstance(brief.get('requiredTransitions'), list) and bool(brief['requiredTransitions'])

def _response_contains_multiple_proposals(text):
    """Reject explicit multi-option answers; one purple card represents one plan."""
    value = str(text or '')
    option_markers = re.findall('(?:\\b(?:option|plan|proposal)\\s*(?:[a-d]|[1-4]|one|two|three|four|first|second|third|fourth)\\b|\\b(?:first|second|third|fourth)\\s+(?:option|plan|proposal)\\b|方案\\s*(?:[A-D一二三四]|[1-4]))', value, flags=re.IGNORECASE)
    return len(option_markers) >= 2

def proposal_offer_requires_execution_brief(proposal_offer, *texts):
    """Reject every proposal card that would require a downstream agent to guess cells."""
    return isinstance(proposal_offer, dict) and (not _proposal_offer_has_exact_execution_plan(proposal_offer))

def _plain_action_instruction(stage_context):
    context = stage_context or {}
    action = context.get('explicitAction') or 'none'
    offer = context.get('sourceProposalOffer') or {}
    offer_text = ' '.join((str(offer.get(field) or '').strip() for field in ('summary', 'rationale'))).strip()
    if action == 'challenge_revision':
        return f"CARD ACTION: challenge_revision. This is only the first invitation to discuss the cited proposal ({offer_text}). Write ordinary prose that restates it, explains its reason and target play moment. Then give exactly two explicitly tentative, correctable hypotheses about why the designer challenged it: one primary concern about whether the exact change really creates the intended mechanism, and one secondary concern about whether it harms an experience the designer asked to preserve. Ask one open question that invites correction. Do not claim either hypothesis as the designer's actual reason. Do not emit any GUIDANCE fields, including DISCUSS, WARNING, or proposal fields."
    if action == 'alternative_revision':
        alternative_brief = str(context.get('alternativeRevisionBrief') or '').strip()
        return f'CARD ACTION: alternative_revision. The cited proposal is {offer_text}. Offer a different conceptual local treatment with a different summary and playable rationale. Return exactly one proposalOffer with a complete, current-Stage-validated executionBrief containing every required transition and anchorEntity for any entity movement. Do not emit map rows or a disagreement. If the current facts cannot support a different exact proposal, ask for clarification and omit proposalOffer. {alternative_brief}'
    classification = context.get('challengeReasonClassification') or {}
    if classification:
        relation = classification.get('relation')
        merit = classification.get('merit')
        if relation == 'different' and merit == 'reasonable':
            return "CHALLENGE REASON REVIEW: the designer gave a new reason that differs from both saved hypotheses, and the independent reviewer found it reasonable. Explicitly acknowledge that the new reason has merit, then ask whether the designer still wants to use the AI's original approach. Ask for a yes-or-no answer. Also return an active disagreement whose coreDisagreement contains the complete first-person comparison: why the reason is persuasive, what it prioritizes, what the original proposal prioritizes, and what choice remains. Do not resolve the disagreement, produce a proposal, or claim a map change. The server owns status, phase, displayCard, and card-count state."
        if relation == 'different' and merit == 'not_yet_reasonable':
            return 'CHALLENGE REASON REVIEW: the designer gave a new reason that differs from both saved hypotheses, but it is not yet persuasive. Explain the precise design disagreement in warm first-person prose and ask one open question that could change your judgment. Return an active disagreement whose coreDisagreement contains that complete comparison, including both priorities and the exact decision separating them. Do not resolve it or produce a proposal. The server owns status, phase, displayCard, and card-count state.'
        if relation == 'unclear' or merit == 'unclear':
            return 'CHALLENGE REASON REVIEW: the latest message does not yet express a reason that can be compared with the two saved hypotheses. Ask one concise ordinary-prose clarification. Do not emit a disagreement card or proposal.'
        return "CHALLENGE REASON REVIEW: the independent reviewer matched the designer's reason to one of the two saved hypotheses. Continue the existing challenge flow: accept and resolve only if the reason persuades you; otherwise keep a concrete active disagreement. Do not add a card merely because the classification matched a hypothesis."
    active = context.get('activeDisagreement') or {}
    if active.get('subject') == 'ai_revision_challenge' and active.get('phase') == 'choice_pending':
        return "CHALLENGE CHOICE: only determine whether the designer clearly answered yes or no to using the AI's original approach. If ambiguous, ask that same concise yes-or-no question in ordinary prose. Do not add a proposal or change the disagreement; the server performs the deterministic transition and proposal routing for clear answers."
    if context.get('challengeContext'):
        return "CARD STATE: the designer has now supplied a reason in response to a prior purple-card challenge. Judge that reason against the cited proposal and the concrete map evidence. If you still disagree, emit an active DISAGREEMENT object and no proposal. If the designer's reason persuades you, or the exchange reaches an AI-led or compromise resolution, emit a resolved DISAGREEMENT and a new conceptual proposal. If both sides retain the current map, use resolution retain_current and omit the proposal."
    if active.get('status') == 'active' and active.get('subject') == 'human_edit':
        return 'CARD STATE: an evidence-backed human-edit disagreement is already active. Its entry card has already been shown. The server owns its state using structured turn understanding. Do not emit DISAGREEMENT or HUMAN_EDIT_ENGAGEMENT for this existing disagreement. Use ordinary prose, with no proposal or repeated card. Willingness to discuss only releases the interaction block: it never accepts either position, creates consensus or authorizes edits. Do not claim the disagreement is resolved or an operation is now authorized.'
    if context.get('activeDisagreement'):
        return 'CARD STATE: an unresolved disagreement is active. Use DISAGREEMENT as an object with status active/resolved, subject, userPosition, aiPosition, coreDisagreement, nextQuestion, and resolution. Put the detailed first-person comparison in coreDisagreement, including stance, reason, both priorities, and the concrete choice. Keep it active unless the latest reason truly resolves the issue; do not use an ordinary DISCUSS field as a substitute.'
    if context.get('discussionCardMode') == 'disagreement_only':
        return 'CARD STATE: there is no active disagreement. Keep ordinary questions in the visible reply and omit DISCUSS/followUpQuestion. Only emit DISAGREEMENT when you can state a specific unresolved design decision supported by map evidence.'
    return ''

def _proposal_offer_binding_issue(proposal_offer, body, messages, language):
    if not proposal_offer:
        return None
    if not _proposal_offer_has_exact_execution_plan(proposal_offer):
        return 'the proposal has no complete executionBrief with exact requiredTransitions; the assistant must ask for clarification instead of making the executor infer cells'
    summary = str(proposal_offer.get('summary') or '').strip()
    rationale = str(proposal_offer.get('rationale') or '').strip()
    if _proposal_card_is_meta_language(summary):
        return 'the summary is transition language rather than a design action'
    if not _proposal_summary_is_substantive(summary, language):
        return 'the summary does not state a substantive map or play direction'
    if not _proposal_summary_has_concrete_action(summary, language):
        return 'the summary does not state a concrete design action'
    if not _proposal_rationale_has_playable_effect(rationale, language):
        return 'the rationale does not state a playable effect or observation'
    if _guidance_reuses_visible_sentence(summary, body):
        return 'the summary repeats a sentence from the visible reply'
    if _guidance_reuses_visible_sentence(rationale, body):
        return 'the rationale repeats a sentence from the visible reply'
    if not _proposal_body_has_playable_support(body, language):
        return 'the visible reply contains no complete supporting design analysis'
    return None

def _llm_failure_class(exception, validation_feedback=None):
    code = str(getattr(exception, 'code', '') or '')
    text = ' '.join((str(value or '') for value in (exception, validation_feedback))).casefold()
    if 'route reasoning' in text or ('route' in text and 'detailed' in text):
        return 'route_quality'
    if 'token limit' in text or 'cut off' in text or code == 'MODEL_TOO_LONG':
        return 'truncated_output'
    if code == 'MODEL_EMPTY_RESPONSE':
        return 'empty_output'
    if 'map facts' in text or 'map-fact' in text or 'places b' in text or ('places t' in text):
        return 'map_grounding'
    if code == 'UPSTREAM_TIMEOUT':
        return 'upstream_timeout'
    if code == 'UPSTREAM_REQUEST_REJECTED':
        return 'upstream_request_rejected'
    if code.startswith('UPSTREAM_'):
        return 'upstream_transport'
    return 'schema_or_model_validation'

def _has_proposal_clarification(stage_context):
    specification = (stage_context or {}).get('proposalClarification')
    return bool(isinstance(specification, dict) and str(specification.get('questionKey') or '').strip())

def _proposal_clarification_fallback_message(language, stage_context=None):
    """Recover a proposal discussion without restating the Stage snapshot."""
    specification = (stage_context or {}).get('proposalClarification') or {}
    acknowledgement = str(specification.get('fallbackAcknowledgement') or '').strip()
    if acknowledgement:
        return acknowledgement
    if language == 'zh-CN':
        return '我会沿着你刚才确认的设计方向继续收敛这份方案。'
    return 'I will keep narrowing the proposal around the design direction you just confirmed.'

def _proposal_clarification_fallback_question(language, stage_context=None):
    specification = (stage_context or {}).get('proposalClarification') or {}
    configured = str(specification.get('fallbackQuestion') or '').strip()
    if configured:
        return configured
    key = str(specification.get('questionKey') or '').strip()
    chinese = language == 'zh-CN'
    questions = {'experience_goal': '你希望额外的解题时间主要消耗在哪种判断或操作上？' if chinese else 'What kind of judgment or action should account for the extra solving time?', 'mechanism': '你希望通过哪种局部机制增加实际推箱次数？' if chinese else 'What local mechanism should create the additional box pushes?', 'binding': '你希望先围绕哪个箱子或局部区域增加运输长度？' if chinese else 'Which box or local area should carry the longer transport first?', 'preserve': '增加这部分挑战时，当前体验中的哪一点必须保持不变？' if chinese else 'Which part of the current play experience must remain unchanged while adding this challenge?'}
    return questions.get(key, '你还希望为这份方案补充哪项关键限制？' if chinese else 'What remaining constraint should guide this proposal?')

def _proposal_clarification_dimension_matches(question, question_key):
    text = str(question or '').casefold()
    patterns = {'experience_goal': '(?:时间|难度|体验|节奏|压力|思考|判断|操作|视觉|空间|平衡|外观|time|difficulty|experience|pacing|pressure|judg|action|thinking|visual|spatial|balance|appearance)', 'mechanism': '(?:机制|推箱|推动|运输|顺序|陷阱|绕行|试错|误导|死角|mechanism|push|transport|order|trap|detour|mistake|deadlock)', 'binding': '(?:哪个(?:箱子|对象|区域)|哪(?:个|一)(?:箱子|对象|区域|片局部)|箱子|区域|局部|对象|B\\d+|which|what (?:box|area|region)|box|crate|area|region|local)', 'preserve': '(?:保留|保持|不变|不能改变|约束|preserve|keep|remain|unchanged|constraint|must not change)'}
    pattern = patterns.get(str(question_key or ''))
    return True if pattern is None else bool(re.search(pattern, text, re.IGNORECASE))

def _proposal_clarification_forbidden_detail(value):
    text = str(value or '')
    return bool(re.search('(?:\\(\\s*\\d+\\s*[,，]\\s*\\d+\\s*\\)|第\\s*\\d+\\s*行|第\\s*\\d+\\s*列|坐标|逐格|每个格子|格子修改|具体格子|\\brow\\s*\\d+\\b|\\bcol(?:umn)?\\s*\\d+\\b|coordinates?|per[- ]cell|each cell|exact cells?|located at|positioned at|adjacent to|next to|route endpoint|路线端点|相邻|位于)', text, re.IGNORECASE))

def _proposal_clarification_allowed_labels(value, stage_context):
    specification = (stage_context or {}).get('proposalClarification') or {}
    allowed = {str(label).upper() for label in specification.get('allowedEntityLabels') or [] if str(label).strip()}
    mentioned = {label.upper() for label in re.findall('\\b(?:P|B\\d+|T\\d+)\\b', str(value or ''), re.IGNORECASE)}
    return mentioned.issubset(allowed)

def _clean_proposal_clarification_body(body, stage_context):
    kept = []
    dropped = 0
    for sentence in _content_block_sentences(body):
        if sentence.endswith(('?', '？')) or _proposal_clarification_forbidden_detail(sentence) or (not _proposal_clarification_allowed_labels(sentence, stage_context)) or re.search('(?:请.{0,12}(?:提供|指出|说明).{0,12}(?:坐标|格子|修改)|please.{0,16}(?:provide|specify|list).{0,16}(?:coordinate|cell|edit))', sentence, re.IGNORECASE):
            dropped += 1
            continue
        kept.append(sentence)
    separator = '' if re.search('[\\u3400-\\u9fff]', str(body or '')) else ' '
    return (separator.join(kept).strip(), dropped)

def _clarification_needs_semantic_review(body, question):
    """Risk triggers select a semantic review, never infer intent or reject prose."""
    text = re.sub('(?<![A-Za-z0-9])(?:B\\d+|T\\d+|P)(?![A-Za-z0-9])', '', body + '\n' + question, flags=re.I)
    if re.search('\\d|我(?:想|希望|要|打算)|\\bI (?:want|hope|aim|intend)\\b|必须|保持|不变|锁定|保留|\\b(?:must|exactly|preserve|unchanged|keep)\\b', text, re.I):
        return True
    if re.search('你(?:希望|想要|要求|决定)|\\byou (?:want|wish|require|decided)\\b', body, re.I):
        return True
    return bool(re.search('(?:移动|挪|增加|删除|减少|move|relocat|add|remov|reduc).{0,24}(?:箱子|目标|玩家|外壳|box|target|player|shell)', text, re.I))

def _clarification_review_components(verdict, sentence_ids):
    """Return valid components and technical defects, never reinterpret meaning."""
    valid, issues = ({}, [])
    if not isinstance(verdict, dict):
        return (valid, ['bodyIssues', 'questionIssue'])
    body_issues = verdict.get('bodyIssues')
    if isinstance(body_issues, list) and all((isinstance(item, str) and item in sentence_ids for item in body_issues)):
        valid['bodyIssues'] = list(dict.fromkeys(body_issues))
    else:
        issues.append('bodyIssues')
    if 'questionIssue' in verdict and (verdict['questionIssue'] is None or (isinstance(verdict['questionIssue'], str) and verdict['questionIssue'].strip())):
        valid['questionIssue'] = verdict['questionIssue']
    else:
        issues.append('questionIssue')
    if set(verdict) - {'bodyIssues', 'questionIssue'}:
        issues = list(dict.fromkeys(issues))
    return (valid, issues)

@proposal_phase('clarification_review', 'clarification_review', revalidates='clarification_review')
async def _review_proposal_clarification(api_key, base_url, body, question, stage_context, request_id, deadline):
    discovery = stage_context.get('proposalDiscovery') or {}
    specification = stage_context.get('proposalClarification') or {}
    option_mode = specification.get('optionPolicyVersion') == proposal_options.VERSION
    option_set = stage_context.get('_clarificationOptions') or {}
    messages = [{'role': 'system', 'content': "Review the actual body and question, not just annotations. Reject design options/questions moving boxes, targets, player start or shell: only water/internal walls may change. Gameplay pushes are distinct. Return bodyIssues as IDs from bodySentences for sentences that must be dropped, and questionIssue as null or a repair instruction. Never quote, paraphrase or invent a sentence ID. Check speaker attribution: the designer owns their wish; AI can express its own tentative suggestion/opinion, but cannot claim the designer's goal as its own. Reject invented user locks, numeric requirements, fixed solution lengths, edit scope, and guaranteed effects. Witness counts may be observations, never new preservation constraints. Only exact designer evidence establishes requirements. Water/internal walls are editable; player/boxes/targets/outer shell are fixed. Spatial references do not authorize entity edits. Visual goals need not increase transport or change gameplay. Preserve reliable sentences and personal opinions; do not rewrite them, reject a response for length/style, or require a stock opening. Questions may offer valid optional choices. Do not put assistant suggestions into designer memory." + (" Also return optionIssues, an array of exact option IDs that are unsafe, inconsistent with the designer's expressed goals/prohibitions, unsupported by the snapshot, duplicate directions, or claim guaranteed effects. Empty means all options verified. Check every option's actual text, not just its components. questionIssue must also reject a question that fails to discriminate the compared options, introduces an unrelated choice, repeats an answered tradeoff, or attributes an inferred option to the designer. With zero/one options the question must be empty. Do not require extra choices or questions merely to fill a quota." if option_mode else '')}, {'role': 'user', 'content': json.dumps({'body': body, 'bodySentences': [{'id': f'sentence-{i}', 'text': sentence} for i, sentence in enumerate(_content_block_sentences(body), 1)], 'question': question, 'designerTurns': discovery.get('userTurns') or [], 'answeredQuestions': discovery.get('answers') or [], 'understanding': discovery.get('understanding') or {}, 'questionIntent': specification.get('questionIntent'), 'currentStageSnapshot': stage_context.get('stageSnapshot') or {}, 'verifiedRouteWitness': specification.get('routeEvidence') or {}, **({'optionSet': option_set, 'optionQuestion': stage_context.get('_clarificationOptionQuestion')} if option_mode else {})}, ensure_ascii=False)}]
    svc = _current_service()
    review_deadline = min(deadline, svc.clock.monotonic() + 10.0)
    remaining = min(6.0, svc.remaining_until(review_deadline))
    if remaining <= 0:
        raise asyncio.TimeoutError()
    try:
        response = await asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, 1000, remaining, task='proposal_clarification_review', response_format_override=proposal_options.review_response_format() if option_mode else None), timeout=remaining)
    except asyncio.TimeoutError:
        response = None
    sentences = _content_block_sentences(body)
    sentence_ids = {f'sentence-{i}': sentence for i, sentence in enumerate(sentences, 1)}
    try:
        verdict = parse_json_object(response.choices[0].message.content)
        if str(getattr(response.choices[0], 'finish_reason', '')) == 'length':
            verdict = {}
    except (ValueError, TypeError, IndexError, AttributeError):
        verdict = {}
    valid, issues = _clarification_review_components(verdict, sentence_ids)

    def option_component(verdict, valid, issues):
        if not option_mode:
            return
        option_ids = {item['id'] for item in option_set.get('options') or []}
        value = verdict.get('optionIssues') if isinstance(verdict, dict) else None
        if isinstance(value, list) and all((isinstance(item, str) and item in option_ids for item in value)):
            valid['optionIssues'] = list(dict.fromkeys(value))
        else:
            issues.append('optionIssues')
    option_component(verdict, valid, issues)
    if issues:
        patch = PatchRequest('clarification_review', valid, [Target(field, (field,), (list,) if field in {'bodyIssues', 'optionIssues'} else (str, type(None))) for field in issues], {'review_source': json.loads(messages[1]['content'])})
        try:
            verdict = await _request_component_patch(api_key, base_url, patch, messages, request_id, review_deadline, 'proposal_clarification_review', max_tokens=1000, timeout_limit=4.0)
            valid, issues = _clarification_review_components(verdict, sentence_ids)
            option_component(verdict, valid, issues)
            if issues:
                raise PatchError('CLARIFICATION_REVIEW_REFERENCE_INVALID')
        except (ValueError, TypeError, KeyError, asyncio.TimeoutError) as error:
            failure = LLMServiceError('MODEL_RESPONSE_INVALID', 'The clarification review could not be verified. Retry the original message.', request_id, True, 0, 502)
            failure.details = {'task': 'proposal_clarification', 'failureStage': 'clarification_review', 'failureKind': 'review_validation', 'component': 'clarification_review', 'repairAttemptsUsed': 1, 'maximumRepairAttempts': 1}
            raise failure from error
    verdict = valid
    stage_context['_optionReviewIssues'] = verdict.get('optionIssues') or []
    rejected_ids = set(verdict['bodyIssues'])
    for identity in rejected_ids:
        body = body.replace(sentence_ids[identity], '', 1)
    _log_llm_event('proposal_clarification_semantics_reviewed', requestId=request_id, droppedSentenceCount=len(verdict['bodyIssues']), questionAccepted=verdict['questionIssue'] is None)
    return (body.strip(), 'Repair the rejected direction options and their dependent question.' if verdict.get('optionIssues') else verdict['questionIssue'] or '')

def _clarification_envelope_patch(payload, error, stage_context):
    """Keep parseable candidate fields, never label them semantically verified."""
    if not isinstance(payload, dict) or set(payload) - proposal_options.ENVELOPE_FIELDS:
        return None
    fields = set()
    for issue in safe_issues(error):
        path = issue['fieldPath']
        if path == 'question' and issue['reasonCode'] != 'OPTION_FIELD_INVALID':
            fields.update({'question', 'comparedOptions', 'discriminant'})
        elif path in proposal_options.ENVELOPE_FIELDS:
            fields.add(path)
        else:
            return None
    if not fields:
        return None
    reuse = proposal_options.reuses_options(stage_context.get('proposalDiscovery') or {}, stage_context.get('stageSnapshot') or {})
    kinds = {'body': (str,), 'question': (str,), 'options': (type(None),) if reuse else (list,), 'comparedOptions': (list,), 'discriminant': (str,)}
    return PatchRequest('clarification_question', payload, [Target(field, (field,), kinds[field]) for field in sorted(fields)], {'snapshot': stage_context.get('stageSnapshot'), 'proposal_topic': stage_context.get('proposalDiscovery'), 'question_specification': stage_context.get('proposalClarification')})

@proposal_phase('clarification', 'clarification')
def _parse_proposal_clarification_payload(content, language, stage_context):
    """Validate the selected envelope and bindings before independent semantic review."""
    specification = (stage_context or {}).get('proposalClarification') or {}
    option_mode = specification.get('optionPolicyVersion') == proposal_options.VERSION
    try:
        payload = json.loads(str(content or ''))
    except (TypeError, ValueError, json.JSONDecodeError):
        if option_mode:
            raise
        return ('', '', 'The clarification response must be valid JSON.', 0)
    if option_mode:
        proposal_options.validate_envelope_shape(payload, stage_context.get('proposalDiscovery') or {}, stage_context.get('stageSnapshot') or {})
    if not isinstance(payload, dict) or 'question' not in payload:
        return ('', '', 'The clarification response must contain only body and question.', 0)
    raw_body = payload.get('body') if isinstance(payload.get('body'), str) else ''
    raw_question = payload.get('question')
    if not isinstance(raw_question, str):
        return ('', '', 'The clarification body and question must be strings.', 0)
    body = _sanitize_visible_model_text(raw_body.strip(), language)
    question = _sanitize_visible_model_text(raw_question.strip(), language)
    body, dropped = _clean_proposal_clarification_body(body, stage_context)
    if option_mode:
        option_set, option_question = proposal_options.parse_envelope(payload, stage_context.get('proposalDiscovery') or {}, stage_context.get('stageSnapshot') or {})
        for item in option_set['options']:
            for key in proposal_options.FIELDS - {'components'}:
                text = item[key]
                if '?' in text or '？' in text or _proposal_clarification_forbidden_detail(text) or (not _proposal_clarification_allowed_labels(text, stage_context)):
                    proposal_options.invalid('options', 'OPTION_MAP_DETAIL_INVALID')
        stage_context['_clarificationOptions'] = option_set
        stage_context['_clarificationOptionQuestion'] = option_question
        if len(option_set['options']) < 2:
            return (body, '', '', dropped)
    issue = ''
    if not question or sum((question.count(mark) for mark in ('?', '？'))) != 1:
        issue = 'The clarification must contain exactly one question.'
    elif not question.endswith(('?', '？')):
        issue = 'The clarification question must end with a question mark.'
    elif _proposal_clarification_forbidden_detail(question):
        issue = 'The clarification question asks for or asserts forbidden map details.'
    elif re.search('策略性必须|效率性诱惑|必然|保证|guarantee|strategic necessity|efficiency temptation', question, re.I):
        issue = 'Use plain preference wording; do not offer an unverified mandatory effect.'
    elif not _proposal_clarification_allowed_labels(question, stage_context):
        issue = 'The clarification question uses an entity label not supplied by the server.'
    else:
        specification = (stage_context or {}).get('proposalClarification') or {}
        key = specification.get('questionKey')
        if specification.get('semanticPolicyVersion') != 1 and (not _proposal_clarification_dimension_matches(question, key)):
            issue = f'The clarification question does not address the target dimension {key}.'
    return (body, question if not issue else '', issue, dropped)

def _proposal_clarification_observability(body, question, stage_context):
    specification = (stage_context or {}).get('proposalClarification') or {}
    route_evidence = specification.get('routeEvidence') or {}
    choice_question = bool(re.search('(?:\\bor\\b|\\bversus\\b|\\bvs\\.?\\b|还是|或者|或是)', str(question or ''), re.IGNORECASE))
    question_labels = set(re.findall('\\b(?:P|B\\d+|T\\d+)\\b', str(question or ''), re.IGNORECASE))
    body_labels = set(re.findall('\\b(?:P|B\\d+|T\\d+)\\b', str(body or ''), re.IGNORECASE))
    return {'clarificationRouteEvidenceMode': route_evidence.get('mode', 'unavailable'), 'clarificationBodySentenceCount': len(_content_block_sentences(body)), 'clarificationAnalysisOptionCount': 2 if choice_question else None, 'clarificationQuestionForm': 'choice' if choice_question else 'open', 'clarificationOptionsCovered': question_labels.issubset(body_labels) if choice_question and question_labels else None}

def _deterministic_revision_advice_body(messages, visible_content, language):
    corpus = ' '.join((part for part in (_latest_role_content(messages, 'user'), visible_content) if str(part or '').strip())).casefold()
    chinese = language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', corpus)
    if chinese:
        if any((marker in corpus for marker in ('时间', '游玩', '停留', '延长', '慢下来', '更久', '太快'))):
            return '我会把重点放在关键箱子的第一次推进：让玩家需要在直接路线和绕行路线之间做一次判断。这样游玩时间的增加来自路线和推箱顺序，而不只是多走几步；试玩时可以观察这次停顿是否真的出现。'
        if any((marker in corpus for marker in ('障碍', '绕过', '绕行', '路线'))):
            return '我会让关键障碍附近出现一次需要比较的路线选择；试玩时看玩家是否先读出绕行关系，而不是只因为路变长才多走几步。'
        if '水' in corpus:
            return '我会让箱子靠近水域时必须重新读取可站位置和绕行空间；试玩时观察水是否改变了推进选择，而不是只增加水面的面积。'
        if any((marker in corpus for marker in ('箱', '目标', '推动', '顺序'))):
            return '我会把调整集中在相关箱子接近目标前的推进关系上，让玩家需要判断先后顺序；试玩时确认变化真正影响了推箱，而不是只改变位置。'
        return '我会把刚才的体验目标落到当前路线的一次局部取舍上；试玩时观察玩家第一次推动是否需要比较两种走法。'
    if any((marker in corpus for marker in ('time', 'play', 'stay', 'longer', 'slow', 'too quickly'))):
        return 'I would focus on the first push of the key box and make the player compare a direct route with a detour. The extra time should come from judging route and push order, not only from walking farther.'
    if any((marker in corpus for marker in ('obstacle', 'detour', 'route'))):
        return 'I would make the space around the key obstacle create one route comparison; play should show the player reading the detour rather than merely walking farther.'
    if 'water' in corpus:
        return 'I would make the box reread standing space and detour room beside the water; play should show whether water changes the push choice rather than only increasing its area.'
    if any((marker in corpus for marker in ('box', 'target', 'push', 'order'))):
        return "I would focus the change on the box's approach to the target so the player must judge push order; play should confirm a changed relationship rather than a visual move."
    return 'I would turn the stated experience goal into one local route trade-off; play should show whether the first push requires a comparison between two choices.'

def _repair_conceptual_proposal_binding(messages, body, proposal_offer, visible_content, rows, language, stage_context):
    cleaned_body = _remove_proposal_text_from_body(body, proposal_offer)
    fallback_offer = None
    recent_cues = ((stage_context or {}).get('recentGuidance') or {}).get('uiCues') or {}
    if isinstance(recent_cues, dict):
        recent_cues = recent_cues.values()
    warnings = [dict(cue) for cue in recent_cues if isinstance(cue, dict) if cue.get('type') in {'warning', 'tradeoff'} and cue.get('text')][:1]
    if fallback_offer is not None:
        if not _proposal_body_has_playable_support(cleaned_body, language):
            supporting_body = _deterministic_revision_advice_body(messages, visible_content, language)
            cleaned_body = '\n\n'.join((part for part in (cleaned_body, supporting_body) if part)).strip()
        return {'body': cleaned_body, 'proposalOffer': fallback_offer, 'followUpQuestion': None, 'uiCues': [{'type': 'manual_edit', 'text': _contextual_manual_edit(rows, language)}, *warnings][:2]}
    if not cleaned_body:
        cleaned_body = '我还没有找到足够可靠的具体修改方向，先从一个可观察的游玩瞬间开始。' if language == 'zh-CN' else 'I have not found a reliable concrete revision direction yet, so I would start from one observable play moment.'
    question = _exact_revision_clarification(language)
    return {'body': cleaned_body, 'proposalOffer': None, 'followUpQuestion': None, 'uiCues': []}

def _semantics_preserving_proposal_offer(summary, rationale, visible_content, language):
    summary = _strip_revision_summary_leadin(summary)
    source = ' '.join((part for part in (summary, rationale) if part)).strip()
    if not source:
        return None
    chinese = language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', source)
    title_limit = 42 if chinese else 90
    chosen_summary = summary
    if not chosen_summary:
        chosen_summary = _first_declarative_sentence(rationale)
    if len(chosen_summary) > title_limit or _guidance_reuses_visible_sentence(chosen_summary, visible_content):
        clauses = [clause.strip(' ，,。.!！') for clause in re.split('[；;。.!！]|(?:，|,)(?=.{8,})', source) if clause.strip()]
        action_words = ('移动', '调整', '重排', '增加', '减少', '拉开', '收紧', '改变', '让', '保留') if chinese else ('move', 'adjust', 'rearrange', 'add', 'reduce', 'separate', 'tighten', 'change', 'make', 'keep')
        action_clause = next((clause for clause in clauses if any((word in clause.casefold() for word in action_words))), clauses[0] if clauses else chosen_summary)
        chosen_summary = action_clause[:title_limit].rstrip(' ，,。.!！')
    chosen_summary = chosen_summary or ('落实刚才确认的局部修改' if chinese else 'Apply the agreed local revision')
    chosen_rationale = rationale
    if not chosen_rationale or len(chosen_rationale) < (28 if chinese else 60) or _guidance_text_matches(chosen_summary, chosen_rationale) or _guidance_reuses_visible_sentence(chosen_rationale, visible_content):
        chosen_rationale = f'我会把“{chosen_summary}”作为唯一改动方向，只调整实现它所必需的局部，并用实际格子变化与游玩结果判断它是否成立。' if chinese else f'I will treat “{chosen_summary}” as the only revision direction, change only the local cells needed to realize it, and judge it through the verified diff and play.'
    return {'summary': chosen_summary[:600], 'rationale': chosen_rationale[:1000]}

def _strip_revision_summary_leadin(summary):
    value = str(summary or '').strip()
    if not value:
        return value
    value = re.sub('^(?:(?:如果是我|如果让我来|我的想法是|我倾向于)[，,:：\\s]*)?(?:我会(?:考虑|想先)?|我想(?:先)?|建议(?:先)?|可以(?:先)?)?[，,:：\\s]*', '', value).strip()
    value = re.sub('^(?:(?:if it were me|my suggestion is|my thought is)[,:\\s]*)?(?:i would(?: consider)?|i would suggest|i suggest|we could)[,:\\s]*', '', value, flags=re.IGNORECASE).strip()
    return value

def _plain_guidance_move(stage_opening, intent_hypothesis, proposal_offer):
    if stage_opening:
        return 'observe_stage'
    if proposal_offer:
        return 'offer_revision'
    if intent_hypothesis:
        return 'clarify_intent'
    return 'offer_perspective'

def _apply_deterministic_guidance_fallback(messages, visible_content, language, stage_context, stage_opening, intent_hypothesis, proposal_offer, ui_cues, rows, play_summary, guidance_mode=None, allow_required_fallback=False, semantic_intent=False):
    if stage_opening:
        return (intent_hypothesis, proposal_offer, [], False)
    guidance_mode = guidance_mode or classify_guidance_request(messages, stage_context)
    latest_user = _latest_role_content(messages, 'user')
    explicit_direction = _latest_user_states_direction(messages)
    explicit_agreement = _latest_user_explicitly_agrees(latest_user)
    recent = (stage_context or {}).get('recentGuidance') or {}
    evidence_signature = (stage_context or {}).get('guidanceEvidenceSignature')
    fallback_used = False
    if not semantic_intent:
        difficulty_reframe = _user_reframes_difficulty_judgment(messages)
        intent_hypothesis = _replace_echoed_intent_hypothesis(intent_hypothesis, latest_user, language)
        if intent_hypothesis is None and (difficulty_reframe or (explicit_direction and (not recent.get('intentHypothesis') or _latest_direction_is_new(messages)))):
            candidate = _natural_intent_candidate(latest_user, language, explicit_agreement, difficulty_reframe=difficulty_reframe)
            if not _guidance_text_matches(candidate, recent.get('intentHypothesis')):
                intent_hypothesis = candidate
                fallback_used = True
    cue_by_type = {cue.get('type'): cue for cue in ui_cues if cue.get('type') in {'warning', 'manual_edit'} and cue.get('text')}
    warning_cue = cue_by_type.get('warning')
    if warning_cue and (not _warning_has_strong_evidence(warning_cue['text'], language, rows, stage_context, play_summary)):
        cue_by_type.pop('warning', None)
    if 'warning' not in cue_by_type:
        warning = _deterministic_play_warning(language, play_summary)
        if warning and (not _cue_repeats_current_evidence('warning', warning, recent, evidence_signature)):
            cue_by_type['warning'] = {'type': 'warning', 'text': warning}
            fallback_used = True
    needs_direction = (stage_context or {}).get('revisionRequestState') == 'needs_direction'
    needs_action_companion = proposal_offer is not None
    if 'manual_edit' not in cue_by_type and needs_action_companion:
        manual_edit = _contextual_manual_edit(rows, language)
        if manual_edit and (not _cue_repeats_current_evidence('manual_edit', manual_edit, recent, evidence_signature)):
            cue_by_type['manual_edit'] = {'type': 'manual_edit', 'text': manual_edit}
            fallback_used = True
    ordered_cues = [cue_by_type[cue_type] for cue_type in ('warning', 'manual_edit') if cue_type in cue_by_type][:2]
    return (intent_hypothesis, proposal_offer, ordered_cues, fallback_used)

def _apply_guidance_card_policy(guidance):
    """Normalize guidance into one of the approved one-to-three card families."""
    normalized = dict(guidance or {})
    disagreement = normalized.get('disagreement')
    if isinstance(disagreement, dict) and disagreement.get('status') == 'active':
        normalized['proposalOffer'] = None
        normalized['intentHypothesis'] = None
        normalized['intentConfidence'] = None
        normalized['followUpQuestion'] = None
    cues = [dict(cue) for cue in normalized.get('uiCues') or [] if cue.get('type') in {'warning', 'tradeoff', 'manual_edit'} and cue.get('text')]
    warning = next((cue for cue in cues if cue.get('type') in {'warning', 'tradeoff'}), None)
    manual = next((cue for cue in cues if cue.get('type') == 'manual_edit'), None)
    clarification = None
    if isinstance(disagreement, dict) and disagreement.get('status') == 'active':
        normalized['uiCues'] = [cue for cue in (warning, manual) if cue is not None]
        return normalized
    if normalized.get('proposalOffer') is not None:
        normalized['intentHypothesis'] = None
        normalized['intentConfidence'] = None
        normalized['followUpQuestion'] = None
        normalized['uiCues'] = [cue for cue in (manual, warning) if cue is not None]
        return normalized
    if manual is not None:
        normalized['intentHypothesis'] = None
        normalized['intentConfidence'] = None
        normalized['followUpQuestion'] = None
        normalized['uiCues'] = [manual]
        return normalized
    normalized['uiCues'] = [warning] if warning is not None else []
    return normalized

def _exact_revision_clarification(language):
    if language == 'zh-CN':
        return '为了避免按猜测改图，请明确指出要修改的坐标，并说明每个格子修改前是什么、修改后要变成什么。'
    return 'To keep the revision precise, please specify the cells to change and state the tile before and after each change.'

def _multiple_proposal_fallback_reply(language):
    if language == 'zh-CN':
        return '我会一次只提出一个可执行方案。刚才的回复包含了多个候选方向，请点击重新生成方案，或先说明你希望保留的那一个方向。'
    return 'I will present one executable proposal at a time. The previous reply contained multiple candidate directions, so please request a regenerated proposal or tell me which direction to keep.'

def _ensure_required_guidance_card(guidance, messages, language, rows, stage_opening, stage_context, visible_content='', guidance_mode=None, semantic_intent=False):
    normalized = dict(guidance or {})
    latest_user = _latest_role_content(messages, 'user')
    guidance_mode = guidance_mode or classify_guidance_request(messages, stage_context, stage_opening=stage_opening)
    active_disagreement = (stage_context or {}).get('activeDisagreement')
    if isinstance(active_disagreement, dict) and active_disagreement.get('status') == 'active' and (active_disagreement.get('subject') == 'human_edit') and (normalized.get('disagreement') is None):
        raise ValueError('A human-edit engagement response cannot silently restore the previous active disagreement.')
    if normalized.get('disagreement') is None and isinstance(active_disagreement, dict) and (active_disagreement.get('status') == 'active') and (not normalized.get('proposalOffer')):
        normalized['disagreement'] = active_disagreement
    if _is_stage_one(stage_context) and stage_opening:
        return normalized
    if _is_human_edit_stage_opening(stage_opening, stage_context):
        normalized['followUpQuestion'] = None
        return normalized
    if _user_explicitly_off_topic(latest_user):
        normalized['intentHypothesis'] = None
        normalized['intentConfidence'] = None
        normalized['followUpQuestion'] = None
        normalized['proposalOffer'] = None
        normalized['uiCues'] = []
        return normalized
    if guidance_mode == 'needs_clarification':
        normalized['move'] = 'clarify_intent'
        normalized['proposalOffer'] = None
        normalized['intentHypothesis'] = None
        normalized['intentConfidence'] = None
        normalized['followUpQuestion'] = None
        normalized['uiCues'] = []
        return normalized
    if guidance_mode == 'revision_advice':
        proposal_offer = normalized.get('proposalOffer')
        if _proposal_offer_has_exact_execution_plan(proposal_offer):
            normalized['move'] = 'offer_revision'
            normalized['proposalOffer'] = proposal_offer
            normalized['intentHypothesis'] = None
            normalized['intentConfidence'] = None
            normalized['followUpQuestion'] = None
            cues = [dict(cue) for cue in normalized.get('uiCues') or [] if cue.get('type') in {'warning', 'tradeoff', 'manual_edit'} and cue.get('text')]
            if not any((cue.get('type') == 'manual_edit' for cue in cues)):
                cues.append({'type': 'manual_edit', 'text': _contextual_manual_edit(rows, language)})
            normalized['uiCues'] = [cue for cue in cues if cue.get('type') in {'warning', 'tradeoff', 'manual_edit'}][:2]
            return normalized
        normalized['move'] = 'clarify_intent'
        normalized['proposalOffer'] = None
        normalized['intentHypothesis'] = None
        normalized['intentConfidence'] = None
        clarification = normalized.get('followUpQuestion') or _exact_revision_clarification(language)
        normalized['followUpQuestion'] = None
        normalized['uiCues'] = []
        return normalized
    if guidance_mode == 'discussion':
        normalized['proposalOffer'] = None
        if (stage_context or {}).get('discussionCardMode') == 'disagreement_only':
            normalized['followUpQuestion'] = None
            normalized['uiCues'] = [dict(cue) for cue in normalized.get('uiCues') or [] if cue.get('type') in {'warning', 'tradeoff'} and cue.get('text')][:1]
            return normalized
        normalized['followUpQuestion'] = normalized.get('followUpQuestion') or _friendly_default_discussion_focus(rows, language, latest_user, ((stage_context or {}).get('recentGuidance') or {}).get('discussionFocusHistory', []))
        normalized['move'] = 'clarify_intent'
        normalized['uiCues'] = [dict(cue) for cue in normalized.get('uiCues') or [] if cue.get('type') in {'warning', 'tradeoff'} and cue.get('text')][:1]
        return normalized
    if not semantic_intent:
        ordinary_intent_allowed = bool(not _is_proposal_conversation_branch(stage_context) and _user_explicitly_states_design_stance(latest_user))
        if not ordinary_intent_allowed:
            normalized['intentHypothesis'] = None
            normalized['intentConfidence'] = None
        elif _user_explicitly_states_design_stance(latest_user):
            hypothesis = normalized.get('intentHypothesis') or _natural_intent_candidate(latest_user, language, _latest_user_explicitly_agrees(latest_user), difficulty_reframe=_user_reframes_difficulty_judgment(messages))
            normalized['intentHypothesis'] = _replace_echoed_intent_hypothesis(hypothesis, latest_user, language)
            normalized['intentConfidence'] = 'medium'
    card_count = int(bool(normalized.get('intentHypothesis'))) + int(bool(normalized.get('followUpQuestion'))) + int(bool(normalized.get('proposalOffer'))) + len(normalized.get('uiCues') or [])
    if card_count:
        return normalized
    return normalized

def classify_guidance_request(conversation, stage_context=None, stage_opening=False):
    """Classify when the visible guidance should prefer action or discussion.

    This is intentionally a small deterministic gate around the model's prose.  It does not
    decide the user's intention or authorize a revision; it only selects the existing card
    family for an obvious request for help.
    """
    if stage_opening:
        return 'none'
    context = stage_context or {}
    explicit_action = context.get('explicitAction') or 'none'
    if explicit_action == 'challenge_revision':
        return 'none'
    if explicit_action == 'alternative_revision':
        return 'revision_advice'
    if context.get('activeDisagreement'):
        return 'disagreement'
    if context.get('challengeContext'):
        return 'disagreement'
    if context.get('deferRevisionExecution') and context.get('revisionRequestState') in {'authorized', 'authorized_relaxed'}:
        return 'revision_advice'
    routing = context.get('revisionRouting')
    if routing in {'confused', 'needs_clarification'}:
        return 'needs_clarification'
    if routing == 'proposal_blocked':
        return 'proposal_blocked'
    if routing in {'proposal', 'proposal_conservative'}:
        return 'revision_advice'
    if context.get('revisionRequestState') not in (None, 'not_request'):
        return 'none'
    latest_user = _latest_role_content(conversation, 'user')
    if not latest_user or _user_explicitly_off_topic(latest_user):
        return 'none'
    revision_state, _ = _classify_revision_request(conversation, context)
    if revision_state != 'not_request':
        return 'none'
    user_messages = [str(message.get('content') or '').strip() for message in reversed(conversation) if message.get('role') == 'user' and str(message.get('content') or '').strip()][:3]
    advice_request = any((_guidance_advice_request(message) for message in user_messages))
    has_direction = any((_contains_user_design_direction(message) for message in user_messages))
    if advice_request:
        return 'discussion'
    return 'none'

def _guidance_advice_request(message):
    text = str(message or '').strip().casefold()
    if not text:
        return False
    if re.search('(?:\\u7ed9\\u6211(?:\\u4e00\\u4e2a|\\u4e00\\u70b9)?\\u65b9\\u6848|\\u6211\\u60f3\\u8981(?:\\u4e00\\u4e2a)?\\u65b9\\u6848|\\u6211\\u5e0c\\u671b\\u6709\\u4e00\\u4e2a\\u65b9\\u6848|\\u8bf7\\u7ed9\\u6211\\u4e00\\u4e2a\\u65b9\\u6848|\\u7ed9\\u6211\\u5efa\\u8bae|\\u7ed9\\u6211\\u601d\\u8def|\\u8bf7\\u5efa\\u8bae|\\u8bf7\\u63d0\\u4f9b\\u65b9\\u6848|\\u600e\\u4e48\\u6539|\\u5982\\u4f55\\u6539|\\u600e\\u4e48\\u8c03\\u6574|\\u5982\\u4f55\\u8c03\\u6574|\\u4f60\\u4f1a\\u600e\\u4e48\\u505a|\\u8bf7\\u4f60\\u5206\\u6790)', text):
        return True
    chinese_markers = ('建议', '方案', '思路', '怎么改', '如何改', '怎么调整', '如何调整', '有什么办法', '帮我想', '给我点想法', '给点思路', '你会怎么', '请你分析', '说说修改', '怎么开始')
    if any((marker in text for marker in chinese_markers)):
        return True
    return bool(re.search('\\b(?:suggest(?:ion)?s?|recommend(?:ation)?s?|advice|plan|idea(?:s)?|brainstorm|how (?:to|should|would|could)|what (?:would|could|do) you suggest|what would you change|help me think|give me (?:an? )?(?:idea|direction|plan)|(?:a|an|the|some)\\s+(?:concrete\\s+)?(?:revision|change|direction|plan|edit))\\b', text))

def _contains_user_design_direction(message):
    text = str(message or '').strip().casefold()
    if not text:
        return False
    actual_chinese_direction = bool(re.search('(?:\\u6211\\u60f3(?:\\u8981|\\u8ba9|\\u628a)|\\u6211\\u5e0c\\u671b|\\u6211\\u503e\\u5411\\u4e8e|\\u6211\\u66f4\\u503e\\u5411\\u4e8e|\\u8bf7\\u4fdd\\u6301|\\u4fdd\\u6301|\\u4e0d\\u8981|\\u5fc5\\u987b|\\u8ba9\\u73a9\\u5bb6|\\u4f7f\\u73a9\\u5bb6|\\u589e\\u52a0|\\u51cf\\u5c11|\\u5f3a\\u5316|\\u5f31\\u5316|\\u8c03\\u6574|\\u4fee\\u6539|\\u6539\\u6210).{0,100}(?:\\u6c34\\u57df|\\u6c34|\\u7bb1\\u5b50|\\u76ee\\u6807|\\u8def\\u7ebf|\\u901a\\u9053|\\u969c\\u788d|\\u7a7a\\u95f4|\\u96be\\u5ea6|\\u9009\\u62e9|\\u8282\\u594f|\\u53ef\\u8bfb|\\u63a8\\u7bb1|\\u63a8\\u52a8)', text))
    has_question = '?' in text or '？' in text
    if has_question and (not actual_chinese_direction) and (not re.search('(?:我想|我希望|我更想|我的目标|希望让|想让|需要让|我倾向于|\\bi (?:want|would like|hope|prefer)|\\bmy goal is\\b|\\bmake the player\\b|\\blet the player\\b)', text, re.IGNORECASE)):
        return False
    if actual_chinese_direction or _contains_concrete_revision_direction(text):
        return True
    chinese_direction_leads = ('我想', '我希望', '我更想', '我的目标', '希望让', '想让', '需要让', '我倾向于', '强调', '突出', '增强', '强化', '弱化', '改善')
    chinese_effects = ('绕过障碍', '绕行', '路线', '通道', '推箱', '推动顺序', '推进顺序', '难度', '节奏', '选择', '空间', '水域', '墙', '箱子', '目标', '压力', '犹豫', '停顿', '挑战', '更难', '更简单', '可读', '时间', '游玩', '停留', '延长', '空旷', '空荡', '太空', '别那么空', '不要那么空')
    if any((marker in text for marker in chinese_direction_leads)) and any((marker in text for marker in chinese_effects)):
        return True
    english_leads = re.search('\\b(?:i want|i would like|i hope|i prefer|my goal is|make the player|let the player|increase|reduce|strengthen|weaken|preserve|avoid)\\b', text, re.IGNORECASE)
    english_effects = ('detour', 'obstacle', 'route', 'corridor', 'push order', 'difficulty', 'challenge', 'rhythm', 'choice', 'space', 'water', 'wall', 'box', 'target', 'pressure', 'hesitation')
    if english_leads and any((marker in text for marker in english_effects)):
        return True
    return not has_question and bool(re.search('\\b(?:add|remove|move|change|adjust|reshape|rearrange)\\b.{0,40}\\b(?:wall|water|box|target|route|corridor|obstacle|space)\\b', text, re.IGNORECASE))

def _friendly_default_discussion_focus(rows, language, latest_user, recent_focuses):
    serialized = ''.join((str(row) for row in rows or []))
    seed = sum((ord(character) for character in f'{serialized}{latest_user}'))
    has_water = '@' in serialized
    has_boxes = serialized.count('s') > 0
    has_multiple_boxes = serialized.count('s') > 1
    has_targets = serialized.count('t') > 0
    if language == 'zh-CN':
        options = (('我会留意第一次推箱时，玩家会不会自然停下来读一眼路线；这能帮我们判断附近该微调，还是该留出空间。', True), ('箱子第一次靠近目标时，玩家会不会停一下想下一步？我想先看这个，因为它最能看出路线有没有形成真实选择。', has_boxes and has_targets), ('玩家第一次决定先推哪个箱子时，心里会不会有一点犹豫？这个瞬间能告诉我们下一步只动局部，还是回头整理路线。', has_multiple_boxes), ('水边第一次推进时，箱子还有没有回旋余地？看这里就能知道水是在参与路线，还是只停在视觉上。', has_water and has_boxes), ('玩家走进调整区域的第一步，会不会重新想一下推箱顺序？如果会，这次变化就真的参与到路线里了。', has_boxes))
    else:
        options = (('I would watch whether the first push makes the player pause to read the route; that moment tells us whether to tune nearby space or preserve it.', True), ('To me, the hesitation before a box first approaches its target is worth watching; it says more about route weight than the tile arrangement alone.', has_boxes and has_targets), ('I would focus on the first moment the player chooses which box to push; that judgment decides whether the next revision stays local or revisits the route.', has_multiple_boxes), ('The first push beside the water is the moment I would watch; it shows whether water is shaping the route or remaining visual scenery.', has_water and has_boxes), ('I would look at whether the first step into the adjusted area makes the player reread the order; that matters more than merely adding movement.', has_boxes))
    available_options = [candidate for candidate, available in options if available]
    if not (has_water or has_boxes or has_targets):
        available_options = [candidate for candidate, _ in options]
    if not available_options:
        available_options = [options[0][0]]
    for offset in range(len(options)):
        candidate = available_options[(seed + offset) % len(available_options)]
        if not any((_guidance_text_matches(candidate, recent_focus) for recent_focus in recent_focuses or [])):
            return candidate
    return available_options[seed % len(available_options)]

def _is_proposal_conversation_branch(stage_context):
    context = stage_context or {}
    if context.get('conversationBranch') == 'proposal':
        return True
    return bool(context.get('proposalDiscovery') or context.get('activeDisagreement') or context.get('revisionRouting') in {'needs_clarification', 'proposal', 'proposal_conservative', 'proposal_blocked'} or (context.get('explicitAction') in {'execute_revision', 'challenge_revision', 'alternative_revision', 'continue_challenge'}))

def _revision_direction_sentence(message):
    anchors = ('水', '箱', '目标', '墙', '通道', '路线', '落点', '区域', '开局', 'water', 'box', 'crate', 'target', 'goal', 'wall', 'corridor', 'route', 'opening')
    actions = ('移动', '调整', '重排', '重组', '增加', '减少', '移除', '拉开', '收紧', '加一块', '绕', '改', 'move', 'adjust', 'rearrange', 'reorganize', 'add', 'reduce', 'remove', 'separate', 'detour', 'tighten', 'change')
    for sentence in re.split('(?<=[.!?。！？])\\s*|[\\r\\n]+', str(message or '')):
        cleaned = sentence.strip()
        lowered = cleaned.casefold()
        if _proposal_card_is_meta_language(cleaned):
            continue
        if re.search('(?:把|将).{0,24}(?:水|墙|箱子?|目标|通道|路线|区域).{0,32}(?:挪|移|调|缩|拉|留出|打开|收紧|绕|改)', cleaned):
            return cleaned
        if cleaned and (not cleaned.endswith(('?', '？'))) and any((anchor in lowered for anchor in anchors)) and any((action in lowered for action in actions)):
            return cleaned
    return ''

def _latest_user_states_direction(messages):
    latest = _latest_role_content(messages, 'user')
    if not latest or '?' in latest or '？' in latest:
        return False
    english_direction = re.search("\\b(?:i\\s+(?:want|prefer|would like)|please|keep|make|change|remove|add|preserve|reduce|increase|strengthen|weaken|go ahead|do (?:it|that)|let'?s)\\b", latest, re.IGNORECASE)
    chinese_direction = any((marker in latest for marker in ('我想', '我希望', '我更喜欢', '请', '做', '改', '保留', '让', '不要', '增加', '减少', '加强', '弱化')))
    return english_direction is not None or chinese_direction

def _latest_direction_is_new(messages):
    """Detect a new user direction without suppressing later corrections."""
    user_messages = [str(message.get('content') or '').strip() for message in messages or [] if message.get('role') == 'user' and str(message.get('content') or '').strip()]
    if not user_messages:
        return False
    latest = user_messages[-1]
    if _latest_user_explicitly_agrees(latest):
        return False
    previous_directions = [message for message in user_messages[:-1] if _latest_user_states_direction([{'role': 'user', 'content': message}])]
    if not previous_directions:
        return True
    return not _guidance_text_matches(latest, previous_directions[-1])

def _proposal_operation_family(operations):
    operation_set = set(operations)
    if operation_set.issubset({'add_wall', 'remove_wall'}):
        return 'adjust_internal_walls'
    if operation_set.issubset({'add_water', 'remove_water'}):
        return 'reshape_water'
    if operation_set == {'move_player'}:
        return 'relocate_start'
    if operation_set == {'move_box'}:
        return 'relocate_box'
    if operation_set == {'move_target'}:
        return 'relocate_target'
    if operation_set.issubset({'move_box', 'move_target', 'add_wall', 'remove_wall'}):
        return 'change_box_order'
    raise ValueError('revisionPlan.changes must use one supported operation family.')

def _validate_revision_plan_payload(value, rows=None, entity_bindings=None):
    """Normalize the Chat plan into the existing exact executionBrief shape."""
    if not isinstance(value, dict):
        raise ValueError('revisionPlan must be an object.')
    required = {'objective', 'changes', 'preserve', 'rationale', 'expectedEffect'}
    if set(value) != required:
        raise ValueError('revisionPlan must contain objective, changes, preserve, rationale, and expectedEffect.')
    text_fields = {}
    for field in ('objective', 'rationale', 'expectedEffect'):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip() or len(item) > 1000 or ('\n' in item) or ('\r' in item):
            raise ValueError(f'revisionPlan.{field} is invalid.')
        text_fields[field] = item.strip()
    changes = value.get('changes')
    if not isinstance(changes, list) or not 1 <= len(changes) <= 12:
        raise ValueError('revisionPlan.changes must contain one to twelve exact cell changes.')
    transition_by_operation = {('.', '#'): 'add_wall', ('#', '.'): 'remove_wall', ('p', '.'): 'move_player', ('.', 'p'): 'move_player', ('s', '.'): 'move_box', ('.', 's'): 'move_box', ('t', '.'): 'move_target', ('.', 't'): 'move_target', ('.', '@'): 'add_water', ('@', '.'): 'remove_water'}
    transitions = []
    operations = []
    seen_coordinates = set()
    for index, change in enumerate(changes):
        if not isinstance(change, dict) or not {'row', 'column', 'before', 'after', 'operation'}.issubset(change) or set(change) - {'row', 'column', 'before', 'after', 'operation', 'anchorEntity'}:
            raise ValueError(f'revisionPlan.changes[{index}] must contain row, column, before, after, and operation.')
        row = change['row']
        column = change['column']
        before = change['before']
        after = change['after']
        operation = change['operation']
        anchor_entity = change.get('anchorEntity')
        if isinstance(row, bool) or isinstance(column, bool) or (not isinstance(row, int)) or (not isinstance(column, int)) or (not 1 <= row <= 10) or (not 1 <= column <= 12) or (before not in set('#.@pst')) or (after not in set('#.@pst')) or (before == after) or ((row, column) in seen_coordinates) or (operation not in {'add_wall', 'remove_wall', 'move_player', 'move_box', 'move_target', 'add_water', 'remove_water'}) or (transition_by_operation.get((before, after)) != operation) or (anchor_entity is not None and anchor_entity not in {'P', 'B1', 'B2', 'T1', 'T2'}):
            raise ValueError(f'revisionPlan.changes[{index}] is not an exact supported transition.')
        seen_coordinates.add((row, column))
        transitions.append({'row': row, 'column': column, 'from': before, 'to': after, **({'anchorEntity': anchor_entity} if anchor_entity is not None else {})})
        operations.append(operation)
    preserve = value.get('preserve')
    valid_preserve = {'outer_shell', 'player', 'boxes', 'targets', 'water', 'walls', 'unrelated_areas'}
    if not isinstance(preserve, list) or any((not isinstance(item, str) for item in preserve)) or len(set(preserve)) != len(preserve) or any((item not in valid_preserve for item in preserve)):
        raise ValueError('revisionPlan.preserve is invalid.')
    effect = _proposal_operation_family(operations)
    brief = {'schemaVersion': 1, 'effect': effect, 'anchors': [], 'focus': None, 'requiredTransitions': transitions, 'allowedOperators': list(dict.fromkeys(operations)), 'preserve': list(preserve), 'playObjective': text_fields['objective']}
    return _validate_execution_brief(brief, rows, entity_bindings)

@proposal_phase('execution_brief_validation', 'brief')
def _validate_execution_brief(value, rows=None, entity_bindings=None):
    """Validate the hidden, machine-facing part of a conceptual revision card."""
    if not isinstance(value, dict):
        raise ValueError('executionBrief must be an object.')
    allowed = {'schemaVersion', 'effect', 'anchors', 'focus', 'requiredTransitions', 'allowedOperators', 'preserve', 'playObjective'}
    if not set(value).issubset(allowed):
        raise ValueError('executionBrief contains unexpected fields.')
    if value.get('schemaVersion', 1) != 1:
        raise ValueError('executionBrief.schemaVersion is unsupported.')
    effect = value.get('effect')
    if effect is not None and effect not in {'open_route', 'narrow_route', 'adjust_internal_walls', 'relocate_start', 'relocate_box', 'relocate_target', 'reshape_water', 'change_box_order'}:
        raise ValueError('executionBrief.effect is invalid.')
    anchors = value.get('anchors', [])
    if not isinstance(anchors, list) or len(anchors) > 5 or any((not isinstance(anchor, str) for anchor in anchors)) or (len(set(anchors)) != len(anchors)) or any((anchor not in {'P', 'B1', 'B2', 'T1', 'T2'} for anchor in anchors)):
        raise ValueError('executionBrief.anchors is invalid.')
    focus = value.get('focus')
    if focus is not None:
        if not isinstance(focus, dict) or set(focus) != {'row', 'column', 'radius'}:
            raise ValueError('executionBrief.focus is invalid.')
        if any((isinstance(focus[key], bool) or not isinstance(focus[key], int) for key in ('row', 'column', 'radius'))) or not 1 <= focus['row'] <= 10 or (not 1 <= focus['column'] <= 12) or (not 1 <= focus['radius'] <= 3):
            raise ValueError('executionBrief.focus is outside the Stage.')
    transitions = value.get('requiredTransitions', [])
    if not isinstance(transitions, list) or len(transitions) > 12:
        raise ValueError('executionBrief.requiredTransitions is invalid.')
    normalized_transitions = []
    seen_coordinates = set()
    allowed_tiles = set('#.@pst')
    for transition in transitions:
        if not isinstance(transition, dict) or not {'row', 'column', 'from', 'to'}.issubset(transition) or set(transition) - {'row', 'column', 'from', 'to', 'anchorEntity'}:
            raise ValueError('executionBrief has an invalid required transition.')
        row, column = (transition['row'], transition['column'])
        before, after = (transition['from'], transition['to'])
        if isinstance(row, bool) or isinstance(column, bool) or (not isinstance(row, int)) or (not isinstance(column, int)) or (not 1 <= row <= 10) or (not 1 <= column <= 12) or (before not in allowed_tiles) or (after not in allowed_tiles) or (before == after) or ((row, column) in seen_coordinates):
            raise ValueError('executionBrief has an invalid required transition.')
        seen_coordinates.add((row, column))
        anchor_entity = transition.get('anchorEntity')
        if anchor_entity is not None and anchor_entity not in {'P', 'B1', 'B2', 'T1', 'T2'}:
            raise ValueError('executionBrief has an invalid anchorEntity.')
        normalized_transitions.append({'row': row, 'column': column, 'from': before, 'to': after, **({'anchorEntity': anchor_entity} if anchor_entity is not None else {})})
    operators = value.get('allowedOperators', [])
    valid_operators = {'add_wall', 'remove_wall', 'move_player', 'move_box', 'move_target', 'add_water', 'remove_water'}
    if not isinstance(operators, list) or not 1 <= len(operators) <= 7 or any((not isinstance(operator, str) for operator in operators)) or (len(set(operators)) != len(operators)) or any((operator not in valid_operators for operator in operators)):
        raise ValueError('executionBrief.allowedOperators is invalid.')
    preserve = value.get('preserve', [])
    valid_preserve = {'outer_shell', 'player', 'boxes', 'targets', 'water', 'walls', 'unrelated_areas'}
    if not isinstance(preserve, list) or any((not isinstance(component, str) for component in preserve)) or len(set(preserve)) != len(preserve) or any((component not in valid_preserve for component in preserve)):
        raise ValueError('executionBrief.preserve is invalid.')
    objective = value.get('playObjective')
    if objective is not None and (not isinstance(objective, str) or not objective.strip() or len(objective) > 120 or ('\n' in objective) or ('\r' in objective)):
        raise ValueError('executionBrief.playObjective is invalid.')
    operator_for_transition = {('.', '#'): 'add_wall', ('#', '.'): 'remove_wall', ('p', '.'): 'move_player', ('.', 'p'): 'move_player', ('s', '.'): 'move_box', ('.', 's'): 'move_box', ('t', '.'): 'move_target', ('.', 't'): 'move_target', ('.', '@'): 'add_water', ('@', '.'): 'remove_water'}
    for transition in normalized_transitions:
        operator = operator_for_transition.get((transition['from'], transition['to']))
        if operator is None or operator not in operators:
            raise ValueError('executionBrief.allowedOperators cannot realize a required transition.')
        if operator in {'add_wall', 'remove_wall'} and 'walls' in preserve:
            raise ValueError('executionBrief preserves walls but requires a wall edit.')
        component = {'move_player': 'player', 'move_box': 'boxes', 'move_target': 'targets', 'add_water': 'water', 'remove_water': 'water'}.get(operator)
        if component and component in preserve:
            raise ValueError(f'executionBrief preserves {component} but requires an edit.')
        if focus is not None and max(abs(focus['column'] - transition['column']), abs(focus['row'] - transition['row'])) > focus['radius']:
            raise ValueError('executionBrief.focus does not contain every required transition.')
    if rows is not None:
        if len(rows) != 10 or any((len(row) != 12 for row in rows)):
            raise ValueError('executionBrief cannot be checked against an invalid Stage.')
        facts = build_map_facts(rows, entity_bindings=entity_bindings)
        fact_entities = list(facts.get('entities') or [])
        known_anchors = {item.get('id') for item in fact_entities if item.get('id')}
        if any((anchor not in known_anchors for anchor in anchors)):
            raise ValueError('executionBrief names an entity that is not on the saved Stage.')
        entities_by_id = {item.get('id'): item for item in fact_entities if item.get('id')}
        if any((entities_by_id.get(anchor, {}).get('identityConfidence') != 'exact' for anchor in anchors)):
            raise ValueError('executionBrief cannot use an entity with unknown identity.')
        entity_tiles = {'P': 'p', 'B1': 's', 'B2': 's', 'T1': 't', 'T2': 't'}
        for label in anchors:
            entity = entities_by_id[label]
            tile = entity_tiles[label]
            source_indexes = [index for index, item in enumerate(normalized_transitions) if item.get('anchorEntity') is None and item['from'] == tile and (item['to'] == '.') and ((item['row'], item['column']) == (entity['row'], entity['column']))]
            destination_indexes = [index for index, item in enumerate(normalized_transitions) if item.get('anchorEntity') is None and item['from'] == '.' and (item['to'] == tile)]
            if len(source_indexes) == 1 and len(destination_indexes) == 1:
                normalized_transitions[source_indexes[0]] = {**normalized_transitions[source_indexes[0]], 'anchorEntity': label}
                normalized_transitions[destination_indexes[0]] = {**normalized_transitions[destination_indexes[0]], 'anchorEntity': label}
        tagged = {}
        for transition in normalized_transitions:
            label = transition.get('anchorEntity')
            if label is None:
                continue
            entity = entities_by_id.get(label)
            if entity is None:
                raise ValueError('executionBrief anchorEntity is not on the saved Stage.')
            if entity.get('identityConfidence') == 'unknown':
                raise ValueError('executionBrief cannot use an entity with unknown identity.')
            tile = {'P': 'p', 'B1': 's', 'B2': 's', 'T1': 't', 'T2': 't'}[label]
            coordinate = (transition['row'], transition['column'])
            entity_coordinate = (entity['row'], entity['column'])
            is_source = transition['from'] == tile and transition['to'] == '.'
            is_destination = transition['from'] == '.' and transition['to'] == tile
            if not (is_source or is_destination):
                raise ValueError('executionBrief anchorEntity does not match its tile transition.')
            if is_source and coordinate != entity_coordinate:
                raise ValueError(f'executionBrief anchorEntity {label} does not match its source coordinate.')
            tagged.setdefault(label, {'sources': [], 'destinations': []})['sources' if is_source else 'destinations'].append(transition)
        for label, group in tagged.items():
            if len(group['sources']) != 1 or len(group['destinations']) != 1:
                raise ValueError(f'executionBrief entity move for {label} requires one source and one destination.')
        moving_operators = {operator_for_transition[item['from'], item['to']] for item in normalized_transitions if (item['from'], item['to']) in operator_for_transition} & {'move_player', 'move_box', 'move_target'}
        if moving_operators and entity_bindings is not None and any((item.get('anchorEntity') is None for item in normalized_transitions)):
            raise ValueError('entity movement transitions must identify their anchorEntity.')
        if any((label not in anchors for label in tagged)):
            raise ValueError('executionBrief anchors must include every transition entity.')
        if moving_operators and effect in {'relocate_start', 'relocate_box', 'relocate_target'}:
            expected_prefix = {'relocate_start': 'P', 'relocate_box': 'B', 'relocate_target': 'T'}[effect]
            relevant_anchors = [anchor for anchor in anchors if anchor.startswith(expected_prefix)]
            if len(relevant_anchors) != 1:
                raise ValueError(f'{effect} requires exactly one explicitly bound entity anchor.')
            anchor = relevant_anchors[0]
            entity = entities_by_id[anchor]
            source_coordinates = {(item['row'], item['column']) for item in normalized_transitions if item['from'] == {'P': 'p', 'B1': 's', 'B2': 's', 'T1': 't', 'T2': 't'}[anchor] and item['to'] == '.'}
            if source_coordinates != {(entity['row'], entity['column'])}:
                raise ValueError(f'{effect} transitions do not operate on anchor {anchor}.')
        for transition in normalized_transitions:
            current = rows[transition['row'] - 1][transition['column'] - 1]
            if current != transition['from']:
                raise ValueError(f"executionBrief coordinate conflict: row {transition['row']}, column {transition['column']} is {current!r}, not {transition['from']!r}.")
    return {'schemaVersion': 1, 'effect': effect, 'anchors': list(anchors), 'focus': dict(focus) if focus is not None else None, 'requiredTransitions': normalized_transitions, 'allowedOperators': list(operators), 'preserve': list(preserve), 'playObjective': objective.strip() if isinstance(objective, str) else None}

def validate_execution_brief(value, rows=None, entity_bindings=None):
    """Public server-side entry point for validating a proposal execution brief."""
    return _validate_execution_brief(value, rows, entity_bindings)

def normalize_revision_plan(value, rows=None, entity_bindings=None):
    """Public server-side entry point for normalizing a Chat revision plan."""
    return _validate_revision_plan_payload(value, rows, entity_bindings)

def _classify_revision_request(conversation, stage_context=None):
    if (stage_context or {}).get('conversationOnly'):
        return ('not_request', None)
    latest_user_message = next((str(message.get('content') or '').strip().casefold() for message in reversed(conversation) if message.get('role') == 'user'), '')
    if not latest_user_message:
        return ('not_request', None)
    if _user_explicitly_off_topic(latest_user_message):
        return ('not_request', None)
    understanding = (stage_context or {}).get('turnUnderstanding')
    if isinstance(understanding, dict):
        if understanding.get('mapRelated') is False:
            return ('not_request', None)
        acts = set(understanding.get('acts') or [])
        elements = set(understanding.get('elements') or [])
        if acts & {'revision_request', 'proposal_request', 'change_request'}:
            if protected_change_requested(understanding, bool((stage_context or {}).get('proposalDiscovery'))):
                return ('not_request', None)
            return ('needs_direction', None)
        return ('not_request', None)
    if (stage_context or {}).get('vagueAestheticRevision'):
        return ('needs_direction', None)
    relaxation_offer = ((stage_context or {}).get('recentGuidance') or {}).get('relaxationOffer') or {}
    if relaxation_offer.get('status') == 'awaiting_confirmation':
        if _latest_user_explicitly_agrees(latest_user_message):
            relaxed_brief = str(relaxation_offer.get('relaxedBrief') or '').strip()
            if relaxed_brief:
                return ('relaxation_confirmed', relaxed_brief)
        if _latest_user_explicitly_rejects(latest_user_message):
            return ('not_request', None)
    english_markers = ('map proposal', 'give me a plan', 'provide a plan', 'suggest a plan', 'concrete revision', 'concrete map', 'draft this', 'draft that', 'draft the revision', 'generate a map', 'create a map', 'revise the map', 'rework the map')
    chinese_markers = ('地图提案', '给我方案', '给我个方案', '给我一个方案', '提供方案', '生成方案', '具体生成', '生成地图', '生成一份', '修改地图', '改一下地图', '把地图改', '你帮我改', '帮我改一下', '帮我改改', '你来改', '请你改', '交给你改', '你直接改', '帮我做', '帮我做个方案', '帮我做一个方案', '就这么做', '照这个做', '按这个做')
    actual_direct_edit = bool(re.search('(?:\\u8bf7|\\u5e2e\\u6211|\\u4f60\\u6765|\\u76f4\\u63a5)(?:\\u628a|\\u5c06).{0,100}(?:\\u6539|\\u8c03\\u6574|\\u4fee\\u6539|\\u79fb\\u52a8|\\u589e\\u52a0|\\u51cf\\u5c11|\\u5220\\u9664|\\u4fdd\\u7559)', latest_user_message))
    marker_match = actual_direct_edit or any((marker in latest_user_message for marker in (*english_markers, *chinese_markers)))
    chinese_polite_request = re.search('(?:可以|能|能不能|可不可以|请|麻烦)?(?:你)?(?:帮我|替我|给我)(?:修改|改|调整)(?:一下|一版|这个|地图|关卡|布局)?', latest_user_message)
    chinese_short_command = re.fullmatch('\\s*(?:开始)?(?:修改|改|调整)(?:一下|吧)?\\s*[。！!？?]*\\s*', latest_user_message)
    chinese_authorization = re.search('(?:按|照着|依照)(?:这个|这条|刚才的|上面的)?(?:方向|思路|方案)?(?:帮我|你来|直接)?改(?:一下|改|吧)?', latest_user_message)
    english_authorization = re.search('\\b(?:(?:can|could|would|will) you(?: please)?|please|go ahead and|you can|i want you to)\\s+(?:change|modify|revise|rework|edit)(?:\\s+(?:it|this|that|the (?:map|level|layout|revision)))?\\b', latest_user_message)
    english_design_question = re.search('\\b(?:how|what)\\s+(?:would|could|should|can)\\s+you\\s+(?:change|modify|revise|rework|edit)\\b', latest_user_message)
    chinese_design_question = re.search('(?:怎么|如何|怎样)(?:来)?改|改(?:成)?什么样', latest_user_message)
    english_imperative = re.search('\\b(?:please|go ahead and|then)\\s+(?:change|modify|revise|rework|edit|do it)\\b', latest_user_message)
    chinese_imperative = re.search('(?:直接|就|按这个|照这个|帮我|请你)(?:方向|思路|方案)?改(?:一下|改|吧)?', latest_user_message)
    if english_design_question is not None and english_imperative is None:
        english_authorization = None
        marker_match = False
    if chinese_design_question is not None and chinese_imperative is None:
        chinese_authorization = None
        marker_match = False
        chinese_polite_request = None
        chinese_short_command = None
    inherited_brief = _authorized_revision_brief(conversation, stage_context, latest_user_message)
    requested = marker_match or chinese_authorization is not None or chinese_polite_request is not None or (chinese_short_command is not None) or (english_authorization is not None) or (_latest_user_confirms_revision(latest_user_message) and inherited_brief is not None)
    if not requested:
        return ('not_request', None)
    if relaxation_offer.get('status') == 'suggestion_ready':
        relaxed_brief = str(relaxation_offer.get('relaxedBrief') or '').strip()
        if relaxed_brief:
            return ('authorized_relaxed', relaxed_brief)
    brief = inherited_brief
    return ('authorized', brief) if brief else ('needs_direction', None)

def _requests_complete_map(conversation, stage_context=None):
    state, _ = _classify_revision_request(conversation, stage_context)
    return state in {'authorized', 'authorized_relaxed'}

def classify_revision_request(conversation, stage_context=None):
    """Expose the deterministic request state to the API orchestration layer."""
    return _classify_revision_request(conversation, stage_context)

def _authorized_revision_brief(conversation, stage_context, latest_user_message):
    if _contains_concrete_revision_direction(latest_user_message):
        return latest_user_message[:1200]
    recent_offer = ((stage_context or {}).get('recentGuidance') or {}).get('proposalOffer') or {}
    offer_text = ' '.join((str(recent_offer.get(field) or '').strip() for field in ('summary', 'rationale'))).strip()
    if _contains_concrete_revision_direction(offer_text):
        return offer_text[:1200]
    skipped_latest_user = False
    for message in reversed(conversation):
        role = message.get('role')
        content = str(message.get('content') or '').strip()
        if role == 'user' and (not skipped_latest_user):
            skipped_latest_user = True
            continue
        if role == 'assistant' and _contains_concrete_revision_direction(content, require_proposal_framing=True):
            return content[:1200]
        if role == 'user' and _contains_concrete_revision_direction(content, require_proposal_framing=True):
            return content[:1200]
    return None

def _contains_concrete_revision_direction(value, require_proposal_framing=False):
    text = str(value or '').strip().casefold()
    if not text:
        return False
    if re.search('[\\u3400-\\u9fff]', text):
        anchors = ('水', '箱', '目标', '墙', '通道', '路线', '区域', '入口', '落点', '推动', '节奏', '左上', '右上', '左下', '右下')
        actions = ('移', '挪', '调整', '重排', '保留', '减少', '增加', '改变', '集中', '连接', '连成', '缩短', '拉开', '让', '改动', '修改', '改成', '改为', '变成', '放', '设', '铺', '不动')
    else:
        anchors = ('water', 'box', 'crate', 'target', 'goal', 'wall', 'corridor', 'route', 'area', 'entrance', 'landing', 'push', 'rhythm', 'upper', 'lower', 'left', 'right')
        actions = ('move', 'shift', 'adjust', 'rearrange', 'keep', 'preserve', 'reduce', 'increase', 'change', 'connect', 'shorten', 'separate', 'make', 'revise')
    has_direction = any((anchor in text for anchor in anchors)) and any((action in text for action in actions))
    if not has_direction or not require_proposal_framing:
        return has_direction
    if re.search('[\\u3400-\\u9fff]', text):
        framing = ('建议', '可以把', '不如', '试试', '我倾向', '我想改', '我的方案', '修改方案', '具体改', '改成', '把它', '把这个', '把那')
        framed_action = re.search('把.{0,48}(?:移|挪|调整|重排|保留|减少|增加|改变|集中|连接|连成|缩短|拉开|改|变成|放|设|铺)', text)
        explicit_operation = re.search('第\\s*\\d+\\s*行.{0,32}(?:第\\s*\\d+\\s*(?:到|至|-)?\\s*第?\\s*\\d*\\s*列|第\\s*\\d+\\s*列).{0,48}(?:水|箱|目标|墙|地面)', text)
    else:
        framing = ('i suggest', 'we could', 'you could', "let's", 'let us', 'try ', 'i would', 'my proposal', 'revision direction', 'change it to')
        framed_action = re.match('(?:move|shift|adjust|rearrange|keep|preserve|reduce|increase|change|connect|shorten|separate|revise)\\b', text)
        explicit_operation = re.search('\\brow\\s*\\d+.{0,40}\\b(?:col(?:umn)?\\s*\\d+|columns?\\s*\\d+).{0,48}\\b(?:water|box|crate|target|wall|floor)\\b', text)
    return any((marker in text for marker in framing)) or framed_action is not None or explicit_operation is not None
