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
from .config_models import ApiError, CARD_ACTION_EVENTS, MESSAGE_ACTIONS, REVISION_CARD_ACTIONS, _execute_revision_candidate_or_api_error, _inline_display_text, _solve_changed_proposal_or_api_error, _validate_changed_proposal, _validate_identifier, require_active_session, require_current_base, utc_now
from .context_and_discovery import _visible_proposal_questions
from .session_persistence_helpers import _proposal_binding_for_offer


def _record_reply_outcome(database, session_id, message_key, version_id, execution, task, proposal_requested=False):
    diagnostics = execution.proposal_diagnostics or {}
    snapshot_fallback = str(execution.model or '').endswith('safe-opening')
    deterministic = diagnostics.get('source') == 'deterministic_contract' or (not snapshot_fallback and execution.model != 'kimi-k2.6')
    app_attr('record_event')(database, session_id, 'reply_delivery_outcome', {'messageKey': message_key, 'baseVersionId': version_id, 'task': task, 'modelGenerationSucceeded': not snapshot_fallback and (not deterministic), 'reliableBodyDelivered': bool(execution.assistant_message), 'fullQualityPassed': not snapshot_fallback and (not deterministic) and diagnostics.get('fullQualityPassed', not diagnostics.get('groundingSentencesDropped')), 'verifiedProposal': bool((execution.guidance or {}).get('proposalOffer')) or execution.proposed_rows is not None, 'proposalRequested': proposal_requested, 'snapshotFallback': snapshot_fallback, 'deterministicReceipt': deterministic, 'attemptsUsed': execution.attempts_used, 'latencyMs': execution.latency_ms, 'componentChecks': diagnostics.get('componentChecks') or [], 'requestId': CURRENT_REPLY_TRACE.get().identity['requestId'] if CURRENT_REPLY_TRACE.get() else execution.request_id, 'pipelineVersion': REPLY_PIPELINE_VERSION, 'intentCardCommitted': bool((execution.guidance or {}).get('intentHypothesis')), 'intentReviewVerified': diagnostics.get('intentReviewVerified'), 'questionReviewVerified': diagnostics.get('questionReviewVerified'), 'questionProgressUpdated': diagnostics.get('questionProgressUpdated'), 'failureCode': diagnostics.get('failureCode'), 'proposalStatus': diagnostics.get('proposalStatus')}, utc_now())

def _persist_verified_requirements(session_id, version_id, stage_context):
    verified = (stage_context.get('_reliabilityState') or {}).get('verifiedRequirements')
    if not verified:
        return
    with connect(immediate=True) as database:
        session = get_session(database, session_id)
        if session is None or session['current_version_id'] != version_id:
            return
        existing = database.execute("SELECT 1 FROM audit_events WHERE session_id = ? AND event_type = 'requirements_verified'\n               AND json_extract(payload_json, '$.cacheKey') = ? LIMIT 1", (session_id, verified['cacheKey'])).fetchone()
        if existing is None:
            app_attr('record_event')(database, session_id, 'requirements_verified', {'baseVersionId': version_id, **verified}, utc_now())

def _retry_exhausted_execution(language, exception):
    if language == 'zh-CN':
        message = '我注意到你尝试了一次重新生成，非常抱歉，由于我的能力不足，我可能无法帮助你进行这个修改，请你根据我们商量好的方案进行自主修改。'
    else:
        message = 'I noticed that you tried generating the revision again. I am sorry, but this request appears to be beyond what I can reliably produce. Please make the change yourself using the direction we discussed.'
    return LLMExecutionResult(assistant_message=message, attempts_used=exception.attempts_used, request_id=exception.request_id, model='deterministic-retry-exhausted-guidance', latency_ms=0, guidance={'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': []})

def _verified_transition_description(brief, language, limit=4):
    transitions = list((brief or {}).get('requiredTransitions') or [])
    displayed = transitions[:limit]
    if language == 'zh-CN':
        details = '、'.join((f"（{item['row']},{item['column']}）从{_proposal_tile_label(item['from'], language)}改为{_proposal_tile_label(item['to'], language)}" for item in displayed))
        suffix = f'等共 {len(transitions)} 处' if len(transitions) > limit else ''
        return f'{details}{suffix}' if details else '已冻结的局部格子变化'
    details = '; '.join((f"row {item['row']}, column {item['column']} from {_proposal_tile_label(item['from'], language)} to {_proposal_tile_label(item['to'], language)}" for item in displayed))
    suffix = f' among {len(transitions)} changes' if len(transitions) > limit else ''
    return f'{details}{suffix}' if details else 'the frozen local tile changes'

def _verified_offer_summary(brief, language):
    effect = str((brief or {}).get('effect') or '')
    effect_labels = {'zh-CN': {'adjust_internal_walls': '调整内部墙体与通路', 'open_route': '打开局部通路', 'reshape_water': '调整水域与通路关系', 'relocate_start': '调整玩家起点', 'relocate_box': '调整箱子位置', 'relocate_target': '调整目标位置'}, 'en': {'adjust_internal_walls': 'Adjust the internal wall and route', 'open_route': 'Open the local route', 'reshape_water': 'Reshape the water-route relationship', 'relocate_start': 'Relocate the player start', 'relocate_box': 'Relocate a box', 'relocate_target': 'Relocate a target'}}
    locale = 'zh-CN' if language == 'zh-CN' else 'en'
    lead = effect_labels[locale].get(effect, '落实一处局部路线调整' if locale == 'zh-CN' else 'Apply a local route adjustment')
    detail = _verified_transition_description(brief, language, limit=2)
    value = f'{lead}：{detail}' if locale == 'zh-CN' else f'{lead}: {detail}'
    return value[:180 if locale == 'zh-CN' else 240]

def _metric_evidence_text(mechanism, language):
    if not isinstance(mechanism, dict) or not mechanism.get('passed'):
        return ''
    facts = []
    if mechanism.get('routeAffected'):
        facts.append('改动贴近已验证解法路线' if language == 'zh-CN' else 'the change touches the verified solution route')
    if mechanism.get('bothBoxesUsed'):
        facts.append('两只箱子都参与解法' if language == 'zh-CN' else 'both boxes participate in the solution')
    labels = {'solutionSteps': ('解法步数', 'solution steps'), 'solutionPushes': ('推动次数', 'solution pushes'), 'minimumPushes': ('最少推动数', 'minimum pushes'), 'pushBlocks': ('推动分段数', 'push blocks'), 'boxAlternations': ('箱子交替次数', 'box alternations')}
    for key, delta in (mechanism.get('metricDeltas') or {}).items():
        if key not in labels or not isinstance(delta, int) or delta == 0:
            continue
        label = labels[key][0 if language == 'zh-CN' else 1]
        if language == 'zh-CN':
            facts.append(f"{label}{('增加' if delta > 0 else '减少')} {abs(delta)}")
        else:
            facts.append(f"{label} {('increases' if delta > 0 else 'decreases')} by {abs(delta)}")
    if language == 'zh-CN':
        return '、'.join(facts)
    return ', '.join(facts)

def _verified_offer_rationale(authorized, brief, mechanism, validation, language):
    need = _inline_display_text(authorized)[:220].rstrip()
    objective = _inline_display_text((brief or {}).get('playObjective'))[:180].rstrip()
    change = _verified_transition_description(brief, language)
    evidence = _metric_evidence_text(mechanism, language)
    validation_payload = validation.as_dict()
    steps = validation_payload.get('solutionSteps')
    pushes = validation_payload.get('solutionPushes')
    if language == 'zh-CN':
        need_text = need or '让局部路线产生更清楚、可观察的判断'
        objective_text = objective or '让这处局部变化参与玩家的路线判断，而不是只改变外观'
        proof = '地图结构与可解性已经通过确定性验证'
        if isinstance(steps, int) and isinstance(pushes, int):
            proof += f'，验证解法为 {steps} 步、{pushes} 次推动'
        if evidence:
            proof += f'；机制检查还显示{evidence}'
        return f"回应你的需求：{need_text}\n\n这次怎么改、为什么：{change}。这样处理是为了{objective_text.rstrip('。')}。\n\n验证与边界：{proof}。这些证据支持它会作用于目标机制，但是否真的形成你期待的思考时间与体验，仍需要试玩确认。"
    need_text = need or 'make the local route ask for a clearer, observable judgment'
    objective_text = objective or 'make the local change affect route judgment rather than appearance alone'
    proof = 'The map structure and solvability passed deterministic validation'
    if isinstance(steps, int) and isinstance(pushes, int):
        proof += f', with a verified solution of {steps} steps and {pushes} pushes'
    if evidence:
        proof += f'; the mechanism check also shows that {evidence}'
    return f"How this answers your request: {need_text}\n\nWhat changes and why: {change}. This treatment is intended to {objective_text.rstrip('.')}.\n\nEvidence and limits: {proof}. This supports the intended mechanism, but whether it creates the desired thinking time and experience still requires playtesting."

def _fallback_proposal_reason(brief, language):
    effect = str((brief or {}).get('effect') or '')
    chinese = language == 'zh-CN'
    reasons = {'open_route': ('打开这段局部通路，有助于减少不必要的绕行，并给玩家更清楚的路线选择。', 'Opening this local passage can reduce unnecessary detours and give the player a clearer route choice.'), 'narrow_route': ('收紧这段通路，有助于让玩家更早判断推箱顺序，避免直接通过。', 'Narrowing this passage can make the player judge the push order earlier instead of taking a direct route.'), 'adjust_internal_walls': ('调整这里的墙体会改变原来的通行关系，让玩家在行动前比较不同路线。', 'Adjusting these walls changes the local passage relationship so the player compares routes before acting.'), 'reshape_water': ('调整这里的水域会重新组织可通行空间，让路线边界和选择更清楚。', 'Reshaping this water area reorganizes the walkable space so route boundaries and choices are clearer.'), 'relocate_start': ('调整玩家起点会改变开局接近各条路线的方式，让第一步判断更有意义。', 'Relocating the player start changes how the opening routes are approached and makes the first decision more meaningful.'), 'relocate_box': ('调整箱子位置会改变它进入主要路线的时机，让推运过程产生新的判断。', 'Relocating the box changes when it enters the main route and creates a new transport decision.'), 'relocate_target': ('调整目标位置会改变箱子的最终推进方向，让后段路线需要重新规划。', "Relocating the target changes the box's final approach and asks the player to replan the later route."), 'change_box_order': ('这项局部调整会改变两个箱子的先后关系，让玩家更早考虑整体推箱顺序。', "This local adjustment changes the boxes' ordering relationship and encourages earlier planning of the overall push sequence.")}
    pair = reasons.get(effect) or ('这项局部调整会改变路线选择，让玩家在推动前有更明确的判断。', 'This local adjustment changes the route choice and gives the player a clearer decision before pushing.')
    return pair[0 if chinese else 1]

def _safe_proposal_reason(value, brief, language):
    if not isinstance(value, str):
        return _fallback_proposal_reason(brief, language)
    text = _inline_display_text(value)[:600]
    coordinate = re.search('[（(]\\s*\\d+\\s*[,，]\\s*\\d+\\s*[）)]', text)
    internal = re.search('(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*|solutionSteps|solutionPushes|playObjective|displayReason|metricGoals|executionBrief|RevisionPlan|\\u76f8\\u5173\\u8bbe\\u8ba1\\u6307\\u6807|\\u5904\\u7406\\u76f8\\u5173\\u6307\\u6807|relevant design metrics?)', text, flags=re.IGNORECASE)
    if not text or coordinate or internal:
        return _fallback_proposal_reason(brief, language)
    return text

def _proposal_origin_texts(stage_context):
    context = stage_context or {}
    discovery = context.get('proposalDiscovery') or {}
    request_text = _inline_display_text(discovery.get('initialRequest'))
    answer_texts = [_inline_display_text(item.get('answerText')) for item in discovery.get('answers') or [] if isinstance(item, dict) and _inline_display_text(item.get('answerText'))][:3]
    if request_text and (answer_texts or context.get('binaryDiscovery')):
        return (request_text[:2000], [item[:2000] for item in answer_texts])
    source_offer = context.get('sourceProposalOffer') or {}
    source_presentation = source_offer.get('proposalPresentation') or {}
    if source_presentation.get('schemaVersion') == 2:
        request_text = _inline_display_text(source_presentation.get('requestText'))
        answer_texts = [_inline_display_text(item) for item in source_presentation.get('answerTexts') or [] if _inline_display_text(item)][:3]
        if request_text and answer_texts:
            return (request_text[:2000], [item[:2000] for item in answer_texts])
    return None

def _verified_offer_presentation_v2(brief, validation, stage_context, display_reason, language):
    origin = _proposal_origin_texts(stage_context)
    if origin is None:
        return None
    request_text, answer_texts = origin
    metrics = validation.as_dict()
    return {'schemaVersion': 2, 'requestText': request_text, 'answerTexts': answer_texts, 'changes': [{'row': item['row'], 'column': item['column'], 'before': item['from'], 'after': item['to']} for item in (brief or {}).get('requiredTransitions') or []], 'reason': _safe_proposal_reason(display_reason, brief, language), 'solution': {'minimumMoves': metrics.get('solutionSteps'), 'pushesOnMinimumMoveSolution': metrics.get('solutionPushes')}}

@proposal_phase('freeze', 'brief')
def _materialize_verified_automatic_offer(execution, base_rows, language, stage_context, *, deadline=None):
    """Freeze a validated semantic candidate into the existing exact purple-card binding."""
    _execute_revision_candidate_or_api_error(base_rows, execution)
    validation = _solve_changed_proposal_or_api_error(base_rows, execution.proposed_rows, **{'deadline': deadline} if deadline is not None else {})
    strategies = (execution.revision_contract or {}).get('strategies') or []
    selected_index = (execution.proposal_diagnostics or {}).get('selectedStrategyIndex')
    if not isinstance(selected_index, int) or not 1 <= selected_index <= len(strategies):
        raise ApiError(502, 'REVISION_EXECUTION_INVALID', 'The verified proposal has no selected execution strategy.')
    strategy = strategies[selected_index - 1]
    transitions = []
    for operation in execution.revision_operations or []:
        row = operation.get('row')
        column = operation.get('column')
        if not isinstance(row, int) or not isinstance(column, int):
            raise ApiError(502, 'REVISION_EXECUTION_INVALID', 'A proposal operation is invalid.')
        transitions.append({'row': row, 'column': column, 'from': base_rows[row - 1][column - 1], 'to': operation.get('to')})
    if (stage_context or {}).get('aiEditableTilesOnly') and any((item['from'] not in {'#', '.', '@'} or item['to'] not in {'#', '.', '@'} for item in transitions)):
        raise ApiError(502, 'REVISION_EXECUTION_INVALID', 'A new AI proposal tried to move a protected map entity.')
    brief = validate_execution_brief({'schemaVersion': 1, 'effect': strategy.get('effect'), 'anchors': list(strategy.get('anchorEntities') or []), 'focus': strategy.get('focus'), 'requiredTransitions': transitions, 'allowedOperators': list(strategy.get('allowedOperators') or []), 'preserve': list(strategy.get('preserve') or []), 'playObjective': strategy.get('playObjective')}, base_rows, (stage_context or {}).get('entityBindings'))
    mechanism = (execution.proposal_diagnostics or {}).get('mechanismEvidence') or {}
    authorized = str((execution.revision_contract or {}).get('authorizedBrief') or '').strip()
    summary = _verified_offer_summary(brief, language)
    rationale = _verified_offer_rationale(authorized, brief, mechanism, validation, language)
    presentation = _verified_offer_presentation_v2(brief, validation, stage_context, strategy.get('displayReason'), language)
    if language == 'zh-CN':
        body = '我已经把刚才确认的方向落实成一份可审查候选。这张紫色方案卡绑定的是已经验证过的真实格子变化；地图目前还没有改变。'
    else:
        body = 'I turned the direction we confirmed into a reviewable candidate. The purple card is bound to verified tile changes; the current map has not changed yet.'
    guidance = dict(execution.guidance or {})
    revision_workflow = dict((execution.revision_contract or {}).get('revisionWorkflow') or {})
    if revision_workflow:
        revision_workflow['status'] = 'proposed'
    if isinstance(revision_workflow.get('requirementRecord'), dict):
        from llm_client import validate_frozen_requirements
        try:
            outcomes = validate_frozen_requirements(base_rows, validation.rows, revision_workflow, (stage_context or {}).get('validation') or validate_and_solve(base_rows, **{'deadline': deadline} if deadline is not None else {}), validation, (stage_context or {}).get('entityBindings'), **{'deadline': deadline} if deadline is not None else {})
        except SemanticConstraintError as exception:
            raise ApiError(422, 'SEMANTIC_POSTCONDITION_FAILED', 'The candidate violated a fixed requirement or reversed a design direction.') from exception
        revision_workflow['goalOutcomes'] = outcomes
        explanation = tradeoff_message(revision_workflow['requirementRecord'], outcomes, language)
        body += '\n\n' + explanation if explanation else ''
        rationale = explanation or rationale
        if presentation is not None:
            presentation['reason'] = explanation or presentation['reason']
    guidance.update({'move': 'offer_revision', 'followUpQuestion': None, 'proposalOffer': {'summary': summary, 'rationale': rationale, 'executionBrief': brief, **({'proposalPresentation': presentation} if presentation is not None else {}), 'revisionWorkflow': revision_workflow}, 'uiCues': []})
    diagnostics = dict(execution.proposal_diagnostics or {})
    diagnostics.update({'candidateFrozen': True, 'mechanismEvidence': mechanism, 'verifiedValidation': validation.as_dict(), 'componentChecks': [{'component': 'operations', 'status': 'verified', 'dependencies': ['contract']}, {'component': 'validation', 'status': 'verified', 'dependencies': ['operations', 'requirements']}, {'component': 'brief', 'status': 'verified', 'dependencies': ['validation']}, {'component': 'presentation', 'status': 'verified', 'dependencies': ['brief'], 'source': 'server_verified_diff'}]})
    return replace(execution, assistant_message=body, proposed_rows=None, modification_summary='', guidance=guidance, revision_contract={**(execution.revision_contract or {}), 'revisionWorkflow': revision_workflow}, proposal_diagnostics=diagnostics)

def _revision_contract_invalid_execution(*, language, request_id, exception):
    if language == 'zh-CN':
        message = '这次方案没有通过当前 Stage 的执行校验，所以我没有改动地图。我会保留我们已经讨论的方向，重新整理一份与当前地图事实一致、可以审查的方案。'
    else:
        message = "This proposal did not pass the current Stage's execution checks, so I left the map unchanged. I will keep the direction we discussed and reframe it as a reviewable proposal grounded in the current map facts."
    return LLMExecutionResult(assistant_message=message, attempts_used=exception.attempts_used, request_id=request_id, model='revision-contract-guard', guidance={'move': 'clarify_intent', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'uiCues': []}, revision_plan=getattr(exception, 'revision_plan', {}) or {}, revision_contract=getattr(exception, 'revision_contract', {}) or {}, proposal_diagnostics={**(getattr(exception, 'proposal_diagnostics', {}) or {}), 'category': 'internal_contract_error'})

def _proposal_search_failure_execution(*, session_id, access_cookie, base_version_id, idempotency_key, revision_state, revision_brief, language, exception):
    if revision_state not in {'authorized', 'authorized_relaxed'} or exception.code != 'PROPOSAL_SEARCH_EXHAUSTED' or (not str(revision_brief or '').strip()):
        return None
    original_brief = str(revision_brief).strip()
    brief_hash = hashlib.sha256(original_brief.encode('utf-8')).hexdigest()[:20]
    revision_plan = getattr(exception, 'revision_plan', {}) or {}
    revision_contract = getattr(exception, 'revision_contract', {}) or {}
    diagnostics = getattr(exception, 'proposal_diagnostics', {}) or {}
    failure_payload = {'baseVersionId': base_version_id, 'messageKey': idempotency_key, 'briefHash': brief_hash, 'code': exception.code, 'attemptsUsed': exception.attempts_used, 'revisionPlan': revision_plan, 'executionContract': revision_contract, 'search': diagnostics, 'providerStatus': getattr(exception, 'provider_status', None), 'providerErrorType': getattr(exception, 'provider_error_type', '') or None, 'providerErrorCode': getattr(exception, 'provider_error_code', '') or None, 'providerParam': getattr(exception, 'provider_param', '') or None, 'providerMessage': getattr(exception, 'provider_message', '') or None}
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        require_current_base(session, base_version_id)
        already_recorded = database.execute("\n            SELECT 1 FROM audit_events\n            WHERE session_id = ? AND event_type = 'proposal_search_failed'\n              AND json_extract(payload_json, '$.baseVersionId') = ?\n              AND json_extract(payload_json, '$.messageKey') = ?\n              AND json_extract(payload_json, '$.briefHash') = ?\n            LIMIT 1\n            ", (session_id, base_version_id, idempotency_key, brief_hash)).fetchone()
        if already_recorded is None:
            app_attr('record_event')(database, session_id, 'proposal_search_failed', failure_payload, utc_now())
    constructed = int(diagnostics.get('constructedCandidates') or 0)
    if language == 'zh-CN':
        message = '我已经把你的方向整理成修改计划，并检查了可用的局部候选，但没有找到一份既产生真实改动又保持可解的地图。当前 Stage 没有变化；这是这次修改请求的可行性限制，不是对你设计方向的否定。你可以沿着这个方向在右侧编辑器亲自调整，也可以继续和我商讨如何缩小或重新表述修改目标。没有新的明确授权和可行提案前，我不会自动改动当前地图。'
        warning = f'确定性搜索检查了{constructed}个局部候选，但没有找到既产生真实改动又保持可解的方案。当前 Stage 保持不变。'
    else:
        message = 'I translated your direction into a revision plan and searched the available local candidates, but none made the requested real changes while staying solvable. The current Stage is unchanged; this is a feasibility limit of this request, not a judgment on your design direction. You can adjust the map yourself in the editor, or we can discuss how to narrow or restate the revision goal. I will not change the current map without a new authorized, feasible proposal.'
        warning = f'Deterministic search checked {constructed} local candidates but found no proposal that made a real change and remained solvable. The current Stage remains unchanged.'
    return LLMExecutionResult(assistant_message=message, attempts_used=exception.attempts_used, request_id=exception.request_id, model='deterministic-search-failure-guidance', latency_ms=0, guidance={'move': 'challenge_tradeoff', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'uiCues': [{'type': 'warning', 'text': warning}], 'relaxationOffer': None}, revision_plan=revision_plan, revision_contract=revision_contract, proposal_diagnostics=diagnostics)

def _automatic_proposal_failure_execution(*, language, request_id, exception):
    """Never turn a failed automatic proposal into ordinary-chat fallback prose."""
    failure_code = str(getattr(exception, 'code', '') or '')
    provider_status = getattr(exception, 'provider_status', None)
    provider_message = str(getattr(exception, 'provider_message', '') or '').strip()
    diagnostics = getattr(exception, 'proposal_diagnostics', {}) or {}
    attempt_failures = diagnostics.get('attemptFailures') or []
    truncated_then_timed_out = failure_code == 'UPSTREAM_TIMEOUT' and any((item.get('failureClass') == 'truncated_output' for item in attempt_failures[:-1])) and bool(attempt_failures) and (attempt_failures[-1].get('code') == 'UPSTREAM_TIMEOUT')
    provider_detail = ''
    if provider_status or provider_message:
        provider_detail = f"Moonshot HTTP {provider_status or 'error'}"
        if provider_message:
            provider_detail += f': {provider_message}'
    if failure_code not in {'UPSTREAM_TIMEOUT', 'UPSTREAM_REQUEST_REJECTED', 'UPSTREAM_SERVER_ERROR'}:
        return _classified_proposal_failure_execution(language=language, request_id=request_id, exception=exception)
    if language == 'zh-CN':
        if truncated_then_timed_out:
            message = '我已经按刚才确认的方向尝试生成待审查方案。首次 RevisionPlan 回复达到输出上限，随后修复重试超时，因此没有得到完整可验证的方案。地图没有改变，我不会把截断结果当成方案。'
        elif failure_code == 'UPSTREAM_TIMEOUT':
            message = '我已经按刚才确认的方向尝试生成待审查方案，但上游模型在本次方案生成的时间预算内未返回（请求超时）。这不是地图或合同验证失败；地图没有改变，我不会把不完整的生成结果当成方案。'
        elif provider_detail:
            message = f'我已经按刚才确认的方向尝试生成待审查方案，但上游方案接口拒绝了这次请求（{provider_detail}）。这不是地图或合同验证结果，地图没有改变。我不会把不完整的生成结果当成方案。'
        else:
            message = '我已经按刚才确认的方向尝试生成待审查方案，但上游方案接口拒绝了这次请求。地图没有改变；我不会把不完整的生成结果当成方案。'
    elif truncated_then_timed_out:
        message = 'I tried to generate a reviewable proposal. The first RevisionPlan response reached its output limit, and the corrective retry then timed out, so no complete verifiable proposal was produced. The map is unchanged and I will not present the truncated result as a proposal.'
    elif failure_code == 'UPSTREAM_TIMEOUT':
        message = 'I tried to generate a reviewable proposal, but the upstream model did not respond within the proposal time budget. This is not a map or contract-validation result; the map is unchanged and I will not present an incomplete generation as a proposal.'
    elif provider_detail:
        message = f'I tried to generate a reviewable proposal, but the upstream proposal endpoint rejected the request ({provider_detail}). This is not a map or contract-validation result; the map is unchanged and I will not present an incomplete generation as a proposal.'
    else:
        message = 'I tried to generate a reviewable proposal, but the upstream proposal endpoint rejected the request. The map is unchanged; I will not present an incomplete generation as a proposal.'
    return LLMExecutionResult(assistant_message=message, attempts_used=exception.attempts_used, request_id=request_id, model='automatic-proposal-failure', guidance={'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': []}, revision_plan=getattr(exception, 'revision_plan', {}) or {}, revision_contract=getattr(exception, 'revision_contract', {}) or {}, proposal_diagnostics={**(getattr(exception, 'proposal_diagnostics', {}) or {}), 'category': 'automatic_proposal_failed', 'failureCode': exception.code, 'failureClass': 'upstream_timeout' if failure_code == 'UPSTREAM_TIMEOUT' else 'upstream_request_rejected' if failure_code == 'UPSTREAM_REQUEST_REJECTED' else 'proposal_generation_failed', 'providerStatus': provider_status, 'providerErrorType': getattr(exception, 'provider_error_type', '') or None, 'providerErrorCode': getattr(exception, 'provider_error_code', '') or None, 'providerParam': getattr(exception, 'provider_param', '') or None, 'providerMessage': provider_message or None})

def _classified_proposal_failure_execution(*, language, request_id, exception):
    code = str(getattr(exception, 'code', '') or 'INTERNAL_PIPELINE_ERROR')
    diagnostics = dict(getattr(exception, 'proposal_diagnostics', {}) or {})
    rejections = [item for item in diagnostics.get('rejectionRecords') or [] if isinstance(item, dict)]
    constructed = int(diagnostics.get('constructedCandidates') or (diagnostics.get('deterministicFallback') or {}).get('constructedCandidates') or 0)
    model_candidates = int(diagnostics.get('candidateCount') or 0)
    search_diagnostics = diagnostics.get('deterministicFallback') or {}
    search_candidates = int(search_diagnostics.get('constructedCandidates') or 0)
    search_deadline_reached = bool(search_diagnostics.get('deadlineReached'))
    replan_attempted = bool(diagnostics.get('replanAttempted'))
    revision_contract = dict(getattr(exception, 'revision_contract', {}) or {})
    original_direction = str(revision_contract.get('authorizedBrief') or '').strip()
    strategy_summaries = []
    for strategy in revision_contract.get('strategies') or []:
        if not isinstance(strategy, dict):
            continue
        strategy_summaries.append({'strategyIndex': strategy.get('strategyIndex'), 'effect': strategy.get('effect'), 'anchors': strategy.get('anchorEntities') or [], 'focus': strategy.get('focus'), 'allowedOperators': strategy.get('allowedOperators') or []})
    recovery_suggestions = []
    for item in rejections:
        if not isinstance(item.get('hiddenCandidate'), dict):
            continue
        suggestion_id = str(item.get('recoverySuggestionId') or '').strip()
        if not suggestion_id:
            continue
        minimum_delta = item.get('minimumDelta')
        baseline_value = item.get('baselineValue')
        candidate_value = item.get('candidateValue')
        actual_delta = candidate_value - baseline_value if isinstance(candidate_value, int) and isinstance(baseline_value, int) else None
        if language == 'zh-CN':
            suggestion_text = f'允许把该量化增幅从至少 {minimum_delta} 调整为候选实际达到的 {actual_delta}；核心体验目标、局部锚点和其他硬约束保持不变。'
        else:
            suggestion_text = f"Allow the measurable increase to change from at least {minimum_delta} to the candidate's verified {actual_delta}; keep the core experience goal, local anchor, and all other hard constraints unchanged."
        recovery_suggestions.append({'suggestionId': suggestion_id, 'text': suggestion_text, 'verified': True, 'avoidsFailure': 'hard_objective_not_met'})
        if len(recovery_suggestions) >= 3:
            break
    labels_zh = {'REVISION_PLAN_INVALID': 'RevisionPlan 的结构化内容无法解析', 'MODEL_RESPONSE_INVALID': 'RevisionPlan 的结构化内容无法解析', 'REVISION_CONTRACT_INVALID': 'RevisionPlan 与当前地图或执行合同冲突', 'REVISION_CONTRACT_CONFLICT': 'RevisionPlan 与当前地图或执行合同冲突', 'EXACT_TRANSITION_INFEASIBLE': '你明确指定的格子变化无法形成合法且可解的候选', 'HARD_OBJECTIVE_NOT_MET': '候选没有满足你明确提出的可量化硬指标', 'SOFT_OBJECTIVE_EVIDENCE_MISSING': '候选虽然可能可解，但缺少支持目标体验的可核验机制变化', 'SEMANTIC_CONSTRAINT_NOT_MET': '候选没有满足原方向中尚未被修改的明确语义约束', 'CANDIDATE_DUPLICATED': '所有候选都重复了已拒绝或正在替换的方案', 'CANDIDATE_UNSOLVABLE': '所有不同候选都未通过确定性可解性验证', 'DETERMINISTIC_SEARCH_EXHAUSTED': '修改助手和确定性局部搜索都没有找到满足当前约束的候选', 'GLOBAL_PROPOSAL_TIMEOUT': '完整提案管线耗尽了 300 秒总预算', 'INTERNAL_PIPELINE_ERROR': '提案管线发生内部错误', 'PROPOSAL_SEARCH_EXHAUSTED': '候选搜索没有找到满足当前约束的可解改动', 'PROPOSAL_ROUTING_INVARIANT': '提案状态机发生内部路由错误', 'CONFIGURATION_ERROR': '提案服务配置不完整'}
    labels_en = {'REVISION_PLAN_INVALID': 'the structured RevisionPlan could not be parsed', 'MODEL_RESPONSE_INVALID': 'the structured RevisionPlan could not be parsed', 'REVISION_CONTRACT_INVALID': 'the RevisionPlan conflicts with the current map or execution contract', 'REVISION_CONTRACT_CONFLICT': 'the RevisionPlan conflicts with the current map or execution contract', 'EXACT_TRANSITION_INFEASIBLE': 'the exact requested tile transitions could not form a valid solvable candidate', 'HARD_OBJECTIVE_NOT_MET': 'no candidate met the explicit measurable requirement', 'SOFT_OBJECTIVE_EVIDENCE_MISSING': 'solvable candidates lacked verified evidence for the requested play mechanism', 'SEMANTIC_CONSTRAINT_NOT_MET': 'candidates did not satisfy an unchanged explicit semantic constraint from the original direction', 'CANDIDATE_DUPLICATED': 'every candidate repeated a rejected or cited proposal', 'CANDIDATE_UNSOLVABLE': 'every distinct candidate failed deterministic solvability validation', 'DETERMINISTIC_SEARCH_EXHAUSTED': 'the modifier and deterministic local search found no admissible candidate', 'GLOBAL_PROPOSAL_TIMEOUT': 'the complete proposal pipeline exhausted its 300-second budget', 'INTERNAL_PIPELINE_ERROR': 'the proposal pipeline encountered an internal error', 'PROPOSAL_SEARCH_EXHAUSTED': 'candidate search found no solvable change satisfying the current constraints', 'PROPOSAL_ROUTING_INVARIANT': 'the proposal state machine encountered an internal routing error', 'CONFIGURATION_ERROR': 'the proposal service configuration is incomplete'}
    rejection_categories = []
    for item in rejections:
        category = str(item.get('category') or '')
        if category and category not in rejection_categories:
            rejection_categories.append(category)
    if language == 'zh-CN':
        reason = labels_zh.get(code, '提案管线发生内部错误')
        details = []
        if model_candidates:
            details.append(f'修改助手给出了 {model_candidates} 个候选')
        if search_candidates:
            details.append(f'确定性搜索检查了 {search_candidates} 个候选')
        if replan_attempted:
            details.append('已尝试一次语义重规划')
        if search_deadline_reached:
            details.append('确定性搜索到达阶段截止时间')
        detail = '；'.join(details) + '。' if details else ''
        if rejection_categories:
            detail += ' 主要拒绝类型：' + '、'.join(rejection_categories[:3]) + '。'
        core = f' 原方向是：{original_direction[:360]}。' if original_direction else ''
        strategy_detail = ''
        if strategy_summaries:
            descriptions = [f"策略{item.get('strategyIndex') or index}（{item.get('effect') or '未命名机制'}，锚点 {','.join(item.get('anchors') or []) or '局部区域'}）" for index, item in enumerate(strategy_summaries[:3], start=1)]
            strategy_detail = ' 本次尝试覆盖了' + '、'.join(descriptions) + '，但均被上述约束或验证结果淘汰。'
        message = f'我已经让这次请求进入正式方案管线，但{reason}。{detail}{core}{strategy_detail}原样重试仍会保留同一组硬约束，因此大概率重复相同冲突。当前 Stage 没有改变，也没有生成紫色方案卡。'
    else:
        reason = labels_en.get(code, 'the proposal pipeline encountered an internal error')
        details = []
        if model_candidates:
            details.append(f'the modifier returned {model_candidates} candidates')
        if search_candidates:
            details.append(f'deterministic search checked {search_candidates} candidates')
        if replan_attempted:
            details.append('one semantic replan was attempted')
        if search_deadline_reached:
            details.append('deterministic search reached its phase deadline')
        detail = ' ' + '; '.join(details) + '.' if details else ''
        if rejection_categories:
            detail += ' Main rejection classes: ' + ', '.join(rejection_categories[:3]) + '.'
        core = f' The original direction was: {original_direction[:360]}.' if original_direction else ''
        strategy_detail = ''
        if strategy_summaries:
            descriptions = [f"strategy {item.get('strategyIndex') or index} ({item.get('effect') or 'unnamed mechanism'}, anchors {','.join(item.get('anchors') or []) or 'local area'})" for index, item in enumerate(strategy_summaries[:3], start=1)]
            strategy_detail = ' The attempt covered ' + ', '.join(descriptions) + ', and each was rejected by the constraints or validation above.'
        message = f'I sent this request through the formal proposal pipeline, but {reason}.{detail}{core}{strategy_detail} An unchanged retry keeps the same hard constraints and is therefore likely to repeat the conflict. The current Stage is unchanged and no purple proposal card was created.'
    diagnostics.update({'category': 'proposal_pipeline_failed', 'failureClass': code.casefold(), 'failureCode': code, 'constructedCandidates': constructed, 'modelCandidateCount': model_candidates, 'deterministicSearchCandidateCount': search_candidates, 'deterministicSearchDeadlineReached': search_deadline_reached, 'replanAttempted': replan_attempted, 'failureEnvelope': {'failureCode': code, 'modelCandidateCount': model_candidates, 'deterministicCandidateCount': search_candidates, 'rejectionCategories': rejection_categories[:8], 'rejections': rejections[-12:], 'replanAttempted': replan_attempted, 'originalDirection': original_direction, 'strategies': strategy_summaries, 'recoverySuggestions': recovery_suggestions}, 'recoverySuggestions': recovery_suggestions})
    if language == 'zh-CN':
        recovery_lines = ['当前主题和前三轮回答均已保留。请补充或修改一个实现维度后继续；不会重新开始三问。']
        if recovery_suggestions:
            pass
        elif code in {'REVISION_CONTRACT_INVALID', 'REVISION_CONTRACT_CONFLICT', 'EXACT_TRANSITION_INFEASIBLE'}:
            recovery_lines.append('1. 改用同一体验目标下的其他局部区域或实体锚点，避开当前坐标/合同冲突。')
        elif code == 'HARD_OBJECTIVE_NOT_MET':
            recovery_lines.append('1. 明确是否允许降低量化幅度；未得到你的授权前，系统不会放宽该硬指标。')
        elif code == 'SOFT_OBJECTIVE_EVIDENCE_MISSING':
            recovery_lines.append('1. 补充希望由运输长度、推箱顺序、绕行或空间限制中的哪种机制产生体验。')
        else:
            recovery_lines.append('1. 指定可调整的局部区域、实体锚点或允许的操作类型。')
        if recovery_suggestions:
            recovery_lines.extend((f"{index}. {item['text']}（已验证候选）" for index, item in enumerate(recovery_suggestions, start=1)))
            recovery_lines.append('可回复“建议1”或直接用自然语言补充。')
        else:
            recovery_lines.append('目前尚未找到经过完整验证的最小调整，因此不会把这些方向标成已验证建议。')
        warning = '\n'.join(recovery_lines)
    else:
        recovery_lines = ['This topic and all three answers are retained. Add or revise one implementation dimension to continue; the three questions will not restart.']
        if recovery_suggestions:
            pass
        elif code in {'REVISION_CONTRACT_INVALID', 'REVISION_CONTRACT_CONFLICT', 'EXACT_TRANSITION_INFEASIBLE'}:
            recovery_lines.append('1. Keep the same experience goal but choose another local area or entity anchor to avoid the coordinate/contract conflict.')
        elif code == 'HARD_OBJECTIVE_NOT_MET':
            recovery_lines.append('1. Say whether the measurable amount may be reduced; the server will not relax that hard requirement without permission.')
        elif code == 'SOFT_OBJECTIVE_EVIDENCE_MISSING':
            recovery_lines.append('1. Specify whether transport length, push order, detour, or space restriction should create the experience.')
        else:
            recovery_lines.append('1. Name an editable local area, entity anchor, or allowed operation type.')
        if recovery_suggestions:
            recovery_lines.extend((f"{index}. {item['text']} (verified candidate)" for index, item in enumerate(recovery_suggestions, start=1)))
            recovery_lines.append('Reply with "suggestion 1" or add a natural-language supplement.')
        else:
            recovery_lines.append('No fully verified minimal adjustment was found yet, so these directions are not labeled as verified suggestions.')
        warning = '\n'.join(recovery_lines)
    typed_record = (revision_contract.get('revisionWorkflow') or {}).get('requirementRecord')
    if isinstance(typed_record, dict) or getattr(exception, 'requirement_policy_version', None) == 1:
        descriptions = {'REQUIREMENT_PERMISSION_CONFLICT': ('这部分明确要求与固定实体或允许修改的范围冲突，我不能执行。', 'An explicit requirement conflicts with fixed entities or editing permissions.'), 'SEMANTIC_CONSTRAINT_NOT_MET': ('本次候选没有通过固定要求或目标方向检查。', 'The candidates failed fixed requirements or direction checks.'), 'CANDIDATE_UNSOLVABLE': ('本次候选没有通过可解性验证。', 'The candidates failed solvability validation.'), 'REVISION_CONTRACT_CONFLICT': ('生成的操作计划与当前地图或执行规则不一致。', 'The generated operation plans conflict with the current map or execution rules.'), 'EXACT_TRANSITION_INFEASIBLE': ('本次没有找到满足指定格子变化的有效候选。', 'No valid candidate matched the specified tile changes.')}
        reason = descriptions.get(code, ('在本次有限搜索中，没有找到通过全部检查的修改。', 'The bounded search found no change passing every check.'))[0 if language == 'zh-CN' else 1]
        message = '我尝试了这次修改，但' + reason + ' 当前地图没有改变，也没有生成紫色方案卡。你的明确目标和本主题的回答仍保留；这次失败不能证明这些目标永远无法实现。' if language == 'zh-CN' else reason + ' The current map is unchanged and no purple card was created. The goals and answers are retained; this attempt does not prove the goals impossible.'
    return LLMExecutionResult(assistant_message=message, attempts_used=getattr(exception, 'attempts_used', 0), request_id=request_id, model='classified-proposal-failure', latency_ms=0, guidance={'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': [] if isinstance(typed_record, dict) or getattr(exception, 'requirement_policy_version', None) == 1 else [{'type': 'warning', 'text': warning}]}, revision_plan=getattr(exception, 'revision_plan', {}) or {}, revision_contract=getattr(exception, 'revision_contract', {}) or {}, proposal_diagnostics=diagnostics)

def _proposal_cancelled_execution(language, request_id):
    message = '明白，我已结束当前方案主题并解除方案入口锁定。当前 Stage 没有发生变化。' if language == 'zh-CN' else 'Understood. I ended the current proposal topic and unlocked proposal entry. The current Stage is unchanged.'
    return LLMExecutionResult(assistant_message=message, attempts_used=0, request_id=request_id, model='deterministic-proposal-cancel', guidance={'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': []})

def _verified_recovery_suggestion_execution(discovery, content, base_rows, language, request_id):
    """Replay, then fully revalidate, a user-selected hidden recovery candidate."""
    match = re.fullmatch('\\s*(?:建议|方案建议|suggestion)\\s*([1-3])\\s*[。.!！]?\\s*', str(content or ''), flags=re.IGNORECASE)
    if not match:
        return None
    envelope = (discovery or {}).get('failureEnvelope') or {}
    suggestions = [item for item in envelope.get('recoverySuggestions') or [] if isinstance(item, dict)]
    selected_position = int(match.group(1)) - 1
    if not 0 <= selected_position < len(suggestions):
        return None
    suggestion = suggestions[selected_position]
    suggestion_id = str(suggestion.get('suggestionId') or '')
    rejection = next((item for item in envelope.get('rejections') or [] if isinstance(item, dict) and item.get('recoverySuggestionId') == suggestion_id and isinstance(item.get('hiddenCandidate'), dict)), None)
    if rejection is None:
        return None
    hidden = rejection['hiddenCandidate']
    if hidden.get('baseMapFingerprint') != map_fingerprint(base_rows):
        return None
    try:
        contract = load_json(dump_json(hidden.get('revisionContract') or {}))
        strategy_index = int(hidden.get('strategyIndex') or 0)
        strategies = contract.get('strategies') or []
        if not 1 <= strategy_index <= len(strategies):
            return None
        strategies[strategy_index - 1]['metricGoals'] = []
        policy = contract.get('objectivePolicy') or {}
        policy['hardMetricGoals'] = []
        contract['objectivePolicy'] = policy
        rows = execute_revision_operations(base_rows, hidden.get('operations') or [], contract, strategy_index)
        validation = _validate_changed_proposal(base_rows, rows)
        if map_fingerprint(rows) != hidden.get('candidateMapFingerprint'):
            return None
    except (TypeError, ValueError, ApiError, LevelValidationError):
        return None
    return LLMExecutionResult(assistant_message='我已按你选择的已验证调整重新检查隐藏候选；它仍满足结构与可解性要求，下面是等待你审查的方案。' if language == 'zh-CN' else 'I replayed and revalidated the hidden candidate under your selected adjustment; it still passes structure and solvability checks and is ready for review.', attempts_used=0, request_id=request_id, proposed_rows=list(rows), model='verified-recovery-replay', guidance={'move': 'deliver_revision', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': []}, revision_plan=contract.get('revisionPlan') or {}, revision_contract=contract, revision_operations=list(hidden.get('operations') or []), proposal_diagnostics={'source': 'verified_recovery_replay', 'recoverySuggestionId': suggestion_id, 'validation': validation.as_dict() if hasattr(validation, 'as_dict') else {}, 'mechanismEvidence': hidden.get('mechanismEvidence') or {}})

def _build_relaxed_revision_brief(original_brief):
    return f'Preserve this designer-authored core direction exactly: {original_brief!r}. Implement one coherent, play-testable local effect that advances that direction. It is not necessary for this single proposal to realize every secondary route, rhythm, or difficulty effect at once. Do not change an unrelated component or area.'

def _relaxed_revision_suggestion_execution(stage_context, language, request_id):
    previous = ((stage_context or {}).get('recentGuidance') or {}).get('relaxationOffer') or {}
    original_brief = str(previous.get('originalBrief') or '').strip()
    relaxed_brief = str(previous.get('relaxedBrief') or '').strip()
    if not relaxed_brief:
        raise ApiError(409, 'RELAXATION_OFFER_EXPIRED', 'The fallback offer is no longer available.')
    if language == 'zh-CN':
        summary = '先落实一个可试玩的局部效果'
        rationale = '降低后的修改要求：保留你原来的核心方向、地图可解性、外壳、明确禁止事项和未涉及区域；这一版不再要求同时实现全部次要路线、节奏或难度效果，只先完成其中一个连贯、可试玩、能够通过实际游玩判断的局部效果。'
        message = f'可以，我们先把后备方案说清楚，但我现在还不会直接改图。\n\n原始方向仍然保留：{original_brief}\n\n{rationale} 如果这个边界符合你的意思，可以点击下方“请助手具体生成这个方案”；只有那一步之后，我才会生成并展示具体的修改地图。'
    else:
        summary = 'Realize one local, play-testable effect first'
        rationale = 'Relaxed requirement: preserve your original core direction, solvability, outer shell, explicit prohibitions, and every unrelated area. This proposal no longer has to realize all secondary route, rhythm, or difficulty effects at once; it should first implement one coherent local effect that can be judged through play.'
        message = f'Agreed. Let us define the fallback proposal first; I will not change the map yet.\n\nYour original direction remains: {original_brief}\n\n{rationale} If that boundary matches what you mean, use “Ask the assistant to draft this ”option below. Only that separate request will generate and display a concrete map.'
    return LLMExecutionResult(assistant_message=message, attempts_used=0, request_id=request_id, model='deterministic-relaxed-suggestion', latency_ms=0, guidance={'move': 'offer_revision', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': {'summary': summary, 'rationale': rationale}, 'uiCues': [], 'relaxationOffer': {'status': 'suggestion_ready', 'originalBrief': original_brief, 'relaxedBrief': relaxed_brief, 'baseVersionId': previous.get('baseVersionId'), 'briefHash': previous.get('briefHash')}})

def _proposal_review_guidance(guidance, language):
    source = dict(guidance or {})
    warning = next((dict(cue) for cue in source.get('uiCues') or [] if cue.get('type') in {'warning', 'tradeoff'} and cue.get('text')), None)
    manual_text = '这份提案会先保持待审查状态。如果你更想亲手微调，可以先拒绝它，再在右侧编辑器沿着高亮区域继续改；我会把你的实际修改当作下一轮共同判断的依据。' if language == 'zh-CN' else 'This proposal stays pending while you review it. If you would rather tune it yourself, reject it first and continue around the highlighted area in the right-hand editor; I will use your actual edit as our next shared evidence.'
    source['intentHypothesis'] = source.get('intentHypothesis') or None
    source['intentConfidence'] = source.get('intentConfidence') if source.get('intentHypothesis') else None
    source['followUpQuestion'] = None
    source['proposalOffer'] = None
    source['uiCues'] = [{'type': 'manual_edit', 'text': manual_text}, *([warning] if warning is not None else [])]
    return source

def _validate_message_action_payload(payload):
    action = payload.action or 'none'
    if action not in MESSAGE_ACTIONS:
        raise ApiError(400, 'INVALID_MESSAGE_ACTION', 'The message action is invalid.')
    source_turn_id = payload.sourceTurnId
    if payload.requestProposal and action != 'none':
        raise ApiError(400, 'INVALID_MESSAGE_ACTION', 'A proposal request cannot also be a revision-card action.')
    if payload.exitChallenge:
        raise ApiError(409, 'CHALLENGE_EXIT_UNSUPPORTED', 'An active proposal challenge must be resolved through the conversation.')
    if payload.challengeChoice is not None:
        expected = {'是', 'yes'} if payload.challengeChoice == 'ai' else {'否', 'no'}
        if action != 'continue_challenge' or payload.content.strip().lower() not in expected:
            raise ApiError(400, 'INVALID_MESSAGE_ACTION', 'The challenge choice does not match its button.')
    if action in {'none', 'continue_challenge'} and source_turn_id:
        raise ApiError(400, 'INVALID_MESSAGE_ACTION', 'A source turn is only valid for a card action.')
    if action in REVISION_CARD_ACTIONS:
        if not source_turn_id:
            raise ApiError(400, 'INVALID_MESSAGE_ACTION', 'A card action requires its source turn.')
        _validate_identifier(source_turn_id, 'sourceTurnId')
    if action == 'continue_challenge':
        if not payload.challengeId:
            raise ApiError(400, 'INVALID_MESSAGE_ACTION', 'Continuing a challenge requires its challenge ID.')
        _validate_identifier(payload.challengeId, 'challengeId')
    elif payload.challengeId:
        raise ApiError(400, 'INVALID_MESSAGE_ACTION', 'A challenge ID is only valid while continuing a challenge.')

def _revision_offer_from_json(guidance_json):
    try:
        guidance = load_json(guidance_json) or {}
    except (TypeError, ValueError):
        return None
    if not isinstance(guidance, dict):
        return None
    offer = guidance.get('proposalOffer')
    if not isinstance(offer, dict) or not str(offer.get('summary') or '').strip():
        return None
    return offer

def _latest_revision_offer_source(database, session_id, version_id):
    sources = database.execute("\n        SELECT id, guidance_json\n        FROM conversation_turns\n        WHERE session_id = ?\n          AND version_id = ?\n          AND role = 'assistant'\n        ORDER BY sequence_number DESC\n        ", (session_id, version_id)).fetchall()
    for source in sources:
        offer = _revision_offer_from_json(source['guidance_json'])
        if offer is not None:
            return (source, offer)
    return (None, None)

def _require_offer_action(offer, action):
    allowed = (offer or {}).get('availableActions')
    if allowed is not None and action not in allowed:
        raise ApiError(409, 'INVALID_CARD_SOURCE', 'This revision card does not allow that action.')

@proposal_phase('card_source', 'binding')
def _source_revision_offer(database, session_id, version_id, source_turn_id):
    source = database.execute('\n        SELECT id, role, version_id, guidance_json, proposal_binding_json\n        FROM conversation_turns\n        WHERE session_id = ? AND id = ?\n        ', (session_id, source_turn_id)).fetchone()
    if source is None or source['role'] != 'assistant':
        raise ApiError(409, 'INVALID_CARD_SOURCE', 'The selected revision card is no longer attached to the current Stage.')
    if source['version_id'] != version_id:
        raise ApiError(409, 'PROPOSAL_STALE', 'The selected proposal belongs to an older Stage.', details={'reason': 'version_changed'})
    offer = _revision_offer_from_json(source['guidance_json'])
    if offer is None:
        raise ApiError(409, 'INVALID_CARD_SOURCE', 'The selected turn does not contain an executable revision direction.')
    latest_source, _ = _latest_revision_offer_source(database, session_id, version_id)
    if latest_source is None or source['id'] != latest_source['id']:
        raise ApiError(409, 'INVALID_CARD_SOURCE', 'Only the latest revision card in the current Stage can be acted on.')
    return (source, offer)

@proposal_phase('proposal_preflight', 'permissions')
def _preflight_proposal(database, session_id, session, source_turn, offer, current_rows):
    """Check the immutable proposal binding before any revision LLM call."""
    try:
        binding = load_json(source_turn['proposal_binding_json'])
    except (TypeError, ValueError):
        binding = None
    if not isinstance(binding, dict):
        raw_brief = offer.get('executionBrief') if isinstance(offer, dict) else None
        if not isinstance(raw_brief, dict) or not isinstance(raw_brief.get('requiredTransitions'), list) or (not raw_brief['requiredTransitions']):
            raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The selected proposal has no reliable execution binding.', details={'reason': 'unbound'})
        current_version = get_version(database, session_id, session['current_version_id'])
        current_bindings = load_entity_bindings(database, session_id, session['current_version_id'])
        if current_version is None:
            raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The selected proposal has no reliable execution binding.', details={'reason': 'version_missing'})
        try:
            brief = validate_execution_brief(raw_brief, current_rows, current_bindings)
        except (TypeError, ValueError) as exception:
            raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The selected proposal has no reliable execution binding.', details={'reason': 'unbound'}) from exception
        binding = {'baseVersionId': source_turn['version_id'], 'mapFingerprint': map_fingerprint(current_rows), 'entityBindingFingerprint': (current_bindings or {}).get('bindingFingerprint'), 'executionBrief': brief, 'status': 'active'}
        database.execute('UPDATE conversation_turns SET proposal_binding_json = ? WHERE id = ?', (dump_json(binding), source_turn['id']))
        app_attr('record_event')(database, session_id, 'proposal_binding_created', {'stageId': source_turn['version_id'], 'sourceTurnId': source_turn['id'], 'mapFingerprint': binding['mapFingerprint'], 'transitions': [{key: item.get(key) for key in ('row', 'column', 'from', 'to', 'anchorEntity')} for item in brief.get('requiredTransitions', [])], 'legacy': True}, utc_now())
    brief = binding.get('executionBrief')
    if not isinstance(brief, dict):
        raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The selected proposal has no reliable execution binding.', details={'reason': 'unbound'})
    if not isinstance(brief.get('requiredTransitions'), list) or not brief['requiredTransitions']:
        raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The selected proposal has no reliable execution binding.', details={'reason': 'unbound'})
    current_bindings = load_entity_bindings(database, session_id, session['current_version_id'])
    try:
        brief = validate_execution_brief(brief, None)
        binding['executionBrief'] = brief
    except (TypeError, ValueError) as exception:
        raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The selected proposal has no reliable execution binding.', details={'reason': 'unbound'}) from exception
    if binding.get('baseVersionId') != source_turn['version_id']:
        _mark_proposal_binding_status(database, session_id, source_turn['id'], binding, 'stale', 'source_version_mismatch')
        raise ApiError(409, 'PROPOSAL_STALE', 'The selected proposal binding is attached to a different Stage.', details={'reason': 'version_changed'})
    if binding.get('baseVersionId') != session['current_version_id']:
        _mark_proposal_binding_status(database, session_id, source_turn['id'], binding, 'stale', 'version_changed')
        raise ApiError(409, 'PROPOSAL_STALE', 'The selected proposal belongs to an older Stage.', details={'reason': 'version_changed'})
    for transition in brief.get('requiredTransitions') or []:
        row = transition['row']
        column = transition['column']
        if current_rows[row - 1][column - 1] == transition['to']:
            details = {'row': row, 'column': column, 'expected': transition['from'], 'actual': current_rows[row - 1][column - 1]}
            _mark_proposal_binding_status(database, session_id, source_turn['id'], binding, 'already_satisfied', 'already_satisfied', details)
            raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', "The current Stage already contains this proposal's requested change.", details={'reason': 'already_satisfied', **details})
    actual_fingerprint = map_fingerprint(current_rows)
    if binding.get('mapFingerprint') != actual_fingerprint:
        _mark_proposal_binding_status(database, session_id, source_turn['id'], binding, 'stale', 'map_changed')
        raise ApiError(409, 'PROPOSAL_STALE', 'The selected proposal was generated from an older map snapshot.', details={'reason': 'map_changed'})
    actual_binding_fingerprint = (current_bindings or {}).get('bindingFingerprint')
    if not actual_binding_fingerprint or binding.get('entityBindingFingerprint') != actual_binding_fingerprint:
        _mark_proposal_binding_status(database, session_id, source_turn['id'], binding, 'stale', 'entity_binding_changed')
        raise ApiError(409, 'PROPOSAL_STALE', 'The selected proposal was generated from an older entity snapshot.', details={'reason': 'entity_binding_changed'})
    if binding.get('status') not in {None, 'active'}:
        reason = binding.get('status')
        raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The selected proposal is no longer active.', details={'reason': reason})
    executed = database.execute("\n        SELECT 1 FROM audit_events\n        WHERE session_id = ?\n          AND event_type = 'proposal_search_completed'\n          AND json_extract(payload_json, '$.sourceTurnId') = ?\n        LIMIT 1\n        ", (session_id, source_turn['id'])).fetchone()
    if executed is not None:
        _mark_proposal_binding_status(database, session_id, source_turn['id'], binding, 'stale', 'already_executed')
        raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'This proposal has already been executed once and is no longer active.', details={'reason': 'already_executed'})
    try:
        brief = validate_execution_brief(brief, current_rows, current_bindings)
        binding['executionBrief'] = brief
    except (TypeError, ValueError) as exception:
        _mark_proposal_binding_status(database, session_id, source_turn['id'], binding, 'stale', 'execution_binding_mismatch')
        raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', 'The selected proposal no longer matches the saved map snapshot.', details={'reason': 'execution_binding_mismatch'}) from exception
    for transition in brief.get('requiredTransitions') or []:
        row = transition['row']
        column = transition['column']
        actual = current_rows[row - 1][column - 1]
        details = {'row': row, 'column': column, 'expected': transition['from'], 'actual': actual}
        if actual == transition['to']:
            _mark_proposal_binding_status(database, session_id, source_turn['id'], binding, 'already_satisfied', 'already_satisfied', details)
            raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', "The current Stage already contains this proposal's requested change.", details={'reason': 'already_satisfied', **details})
        if actual != transition['from']:
            _mark_proposal_binding_status(database, session_id, source_turn['id'], binding, 'stale', 'tile_mismatch', details)
            raise ApiError(409, 'PROPOSAL_PRECONDITION_FAILED', "The current tile no longer matches this proposal's prerequisite.", details={'reason': 'tile_mismatch', **details})
    return binding

def _mark_proposal_binding_status(database, session_id, source_turn_id, binding, status, reason, details=None):
    updated = dict(binding)
    updated['status'] = status
    database.execute('UPDATE conversation_turns SET proposal_binding_json = ? WHERE id = ?', (dump_json(updated), source_turn_id))
    app_attr('record_event')(database, session_id, 'proposal_preflight_failed', {'sourceTurnId': source_turn_id, 'stageId': updated.get('baseVersionId'), 'mapFingerprint': updated.get('mapFingerprint'), 'reason': reason, 'status': status, **({key: details[key] for key in ('row', 'column', 'expected', 'actual') if key in details} if isinstance(details, dict) else {})}, utc_now())

def _record_card_action(database, session_id, payload, offer):
    event_payload = {'messageKey': payload.idempotencyKey, 'idempotencyKey': payload.idempotencyKey, 'requestId': payload.idempotencyKey, 'action': payload.action, 'sourceTurnId': payload.sourceTurnId, 'baseVersionId': payload.baseVersionId, 'versionId': payload.baseVersionId, 'proposalSummary': offer.get('summary'), 'proposalRationale': offer.get('rationale')}
    app_attr('record_event')(database, session_id, 'card_action_requested', event_payload, utc_now())
    app_attr('record_event')(database, session_id, CARD_ACTION_EVENTS[payload.action], event_payload, utc_now())

def _action_for_message_key(database, session_id, message_key):
    row = database.execute("\n        SELECT event_type, payload_json\n        FROM audit_events\n        WHERE session_id = ?\n          AND json_extract(payload_json, '$.messageKey') = ?\n          AND event_type IN ('card_action_requested',\n                             'revision_execution_requested',\n                             'proposal_challenge_started',\n                             'alternative_revision_requested',\n                             'challenge_continued')\n        ORDER BY id DESC LIMIT 1\n        ", (session_id, message_key)).fetchone()
    if row is None:
        return None
    payload = load_json(row['payload_json']) or {}
    return (payload.get('action'), payload.get('sourceTurnId'))

def _proposal_request_for_message_key(database, session_id, message_key):
    row = database.execute("\n        SELECT 1 FROM audit_events\n        WHERE session_id = ?\n          AND event_type = 'proposal_request_requested'\n          AND json_extract(payload_json, '$.messageKey') = ?\n        LIMIT 1\n        ", (session_id, message_key)).fetchone()
    return row is not None

def _challenge_choice(value, language):
    raw = str(value or '').strip().casefold()
    normalized = re.sub('[\\s,.!?。！？]+', '', raw)
    yes = {'是', '是的', '可以', '沿用', '继续沿用', 'yes', 'yesplease', 'useyourapproach'}
    no = {'否', '不是', '不要', '不沿用', '用我的', 'no', 'noplease', 'usemyreason'}
    if normalized in no:
        return 'user'
    if normalized in yes:
        return 'ai'
    if re.search("(?:不沿用|不要|不用|用我的|do\\s+not|don't|use\\s+my)", raw):
        return 'user'
    english_yes = bool(re.match('^yes\\b', raw))
    english_no = bool(re.match('^no\\b', raw))
    chinese_no = bool(re.match('^(?:否|不|不要|不用|别)', raw))
    chinese_yes = bool(re.match('^(?:是(?:的)?|好(?:的)?|可以|沿用|继续沿用)', raw))
    if (english_yes or chinese_yes) and (not (english_no or chinese_no)):
        return 'ai'
    if (english_no or chinese_no) and (not (english_yes or chinese_yes)):
        return 'user'
    return None

def _salvage_challenge_analysis(value, rows, entity_bindings, language):
    """Keep grounded, correctly localized sentences instead of rejecting a whole review."""
    from llm_client import _sanitize_visible_model_text, _validate_map_grounding_texts, _visible_chinese_language_issue
    sentences = re.split('(?<=[。！？.!?])\\s*', str(value or '').strip())
    kept = []
    for sentence in sentences:
        sentence = _sanitize_visible_model_text(sentence, language).strip()
        if not sentence or (language == 'zh-CN' and _visible_chinese_language_issue(sentence)):
            continue
        try:
            _validate_map_grounding_texts([sentence], rows, entity_bindings=entity_bindings)
        except ValueError:
            continue
        if sentence not in kept:
            kept.append(sentence)
        if len(kept) == 2:
            break
    return ' '.join(kept)[:380]

def _enforce_challenge_reason_execution(execution, classification, user_reason, challenge_context, active_disagreement, language):
    relation = (classification or {}).get('relation')
    merit = (classification or {}).get('merit')
    if relation == 'unclear' or merit == 'unclear':
        guidance = dict(execution.guidance or {})
        guidance['proposalOffer'] = None
        if isinstance(active_disagreement, dict) and active_disagreement.get('subject') == 'ai_revision_challenge' and (active_disagreement.get('status') == 'active'):
            preserved = dict(active_disagreement)
            preserved['displayCard'] = False
            guidance['disagreement'] = preserved
        else:
            guidance['disagreement'] = None
        return replace(execution, guidance=guidance, proposed_rows=None, revision_plan={}, revision_contract={}, revision_operations=[], proposal_binding={})
    source = active_disagreement or {}
    hypotheses = (challenge_context or {}).get('hypotheses') or {}
    primary = str(source.get('primaryHypothesis') or hypotheses.get('primary') or '').strip()
    secondary = str(source.get('secondaryHypothesis') or hypotheses.get('secondary') or '').strip()
    proposal_summary = str(source.get('proposalSummary') or hypotheses.get('proposalSummary') or (challenge_context or {}).get('proposalSummary') or '').strip()
    choice_copy = challenge_choice_card_copy(user_reason, proposal_summary, language)
    next_question = choice_copy['nextQuestion']
    disagreement = {'status': 'active', 'subject': 'ai_revision_challenge', 'userPosition': str(user_reason or '').strip()[:220], 'aiPosition': choice_copy['aiPosition'], 'coreDisagreement': choice_copy['coreDisagreement'], 'nextQuestion': next_question, 'resolution': None, 'phase': 'choice_pending', 'displayCard': not bool(active_disagreement), 'primaryHypothesis': primary, 'secondaryHypothesis': secondary, 'proposalSummary': proposal_summary, 'acceptedReason': str(user_reason or '').strip()[:1200], 'challengeId': (challenge_context.get('hypotheses') or {}).get('challengeId') or challenge_context.get('challengeId')}
    guidance = dict(execution.guidance or {})
    guidance.update({'move': 'offer_perspective', 'challengeCardSchemaVersion': 2, 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': disagreement, 'challengeState': {'challengeId': (challenge_context.get('hypotheses') or {}).get('challengeId') or challenge_context.get('challengeId'), 'status': disagreement['phase'], 'interactionMode': 'choice'}, 'uiCues': []})
    return replace(execution, assistant_message=execution.assistant_message, proposed_rows=None, revision_plan={}, revision_contract={}, revision_operations=[], proposal_binding={}, guidance=guidance)

def _enforce_challenge_choice_execution(execution, active_disagreement, resolution, language):
    guidance = dict(execution.guidance or {})
    if resolution not in {'ai', 'user'}:
        pending = dict(active_disagreement or {})
        pending.update({'status': 'active', 'phase': 'choice_pending', 'displayCard': False, 'nextQuestion': '请只确认：你是否仍希望沿用我原来提出的办法？回答“是”或“否”即可。' if language == 'zh-CN' else 'Please confirm only whether you still want to use my original approach: yes or no.'})
        guidance.update({'proposalOffer': None, 'disagreement': pending, 'challengeState': {'challengeId': active_disagreement.get('challengeId'), 'status': 'choice_pending', 'interactionMode': 'choice'}, 'followUpQuestion': None, 'uiCues': []})
        return replace(execution, proposed_rows=None, revision_plan={}, revision_contract={}, revision_operations=[], proposal_binding={}, guidance=guidance)
    resolved = dict(active_disagreement or {})
    resolved.update({'status': 'resolved', 'phase': 'choice_pending', 'displayCard': False, 'resolution': resolution, 'nextQuestion': None})
    guidance['disagreement'] = resolved
    guidance['challengeState'] = {'challengeId': active_disagreement.get('challengeId'), 'status': 'resolved', 'interactionMode': 'resolved'}
    return replace(execution, guidance=guidance)

def _challenge_preserve_description(brief, language):
    labels = {'outer_shell': ('外壳', 'outer shell'), 'unrelated_areas': ('其他区域', 'unrelated areas'), 'player': ('玩家起点', 'player start'), 'boxes': ('箱子位置', 'box positions'), 'targets': ('目标位置', 'target positions'), 'water': ('水域', 'water'), 'walls': ('墙体', 'walls')}
    index = 0 if language == 'zh-CN' else 1
    values = [labels[item][index] for item in (brief or {}).get('preserve') or [] if item in labels and item not in {'outer_shell', 'unrelated_areas'}]
    if not values:
        return '原有的开局判断节奏' if language == 'zh-CN' else 'the original opening judgment'
    return ('、' if language == 'zh-CN' else ', ').join(values[:4])

def _sanitize_challenge_execution(execution, offer, language, source_binding=None, source_turn_id=None):
    body = '请说明你质疑的理由。' if language == 'zh-CN' else 'Please explain why you challenge this proposal.'
    summary = _inline_display_text((offer or {}).get('summary'))
    brief = ((source_binding or {}).get('executionBrief') if isinstance(source_binding, dict) else {}) or {}
    change = _verified_transition_description(brief, language, limit=3)
    objective = _inline_display_text(brief.get('playObjective'))
    preserved = _challenge_preserve_description(brief, language)
    if language == 'zh-CN':
        primary = f"这项质疑可能是在判断：{change}是否足以实现{objective or '方案承诺的实际玩法效果'}，而不只是改变外观或移动距离。"
        secondary = f'另一种可能是，这项改动会削弱方案承诺保留的{preserved}。'
    else:
        primary = f"The challenge may be asking whether {change} is sufficient to {objective or 'produce the promised play effect'}, rather than merely changing appearance or walking distance."
        secondary = f'Another possibility is that the change weakens the promised preservation of {preserved}.'
    workflow_id = str(((source_binding or {}).get('revisionWorkflow') or {}).get('revisionWorkflowId') or source_turn_id or 'legacy-challenge')
    challenge_seed = '\n'.join((workflow_id, summary, primary, secondary))
    challenge_id = 'ch-' + hashlib.sha256(challenge_seed.encode('utf-8')).hexdigest()[:24]
    hypotheses = {'primary': primary[:800], 'secondary': secondary[:800], 'challengeId': challenge_id, 'items': [{'hypothesisId': challenge_id + '-primary', 'kind': 'mechanism_adequacy', 'statement': primary[:800], 'basis': summary[:600]}, {'hypothesisId': challenge_id + '-secondary', 'kind': 'preservation_risk', 'statement': secondary[:800], 'basis': summary[:600]}]}
    return replace(execution, assistant_message=body, proposed_rows=None, revision_plan={}, revision_contract={}, revision_operations=[], proposal_diagnostics={'challengeHypotheses': hypotheses}, guidance={'move': 'offer_perspective', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'challengeState': {'challengeId': challenge_id, 'status': 'awaiting_reason', 'interactionMode': 'reason'}, 'uiCues': []})

def _ensure_alternative_revision_execution(execution, original_offer, language, original_binding=None):
    guidance = dict(execution.guidance or {})
    offer = guidance.get('proposalOffer')
    original_text = ' '.join((str(original_offer.get(field) or '').strip() for field in ('summary', 'rationale')))
    original_summary = str(original_offer.get('summary') or '').strip().casefold()
    original_rationale = str(original_offer.get('rationale') or '').strip().casefold()
    if not isinstance(offer, dict) or not str(offer.get('summary') or '').strip():
        if language == 'zh-CN':
            offer = {'summary': '改用另一段局部绕行路线', 'rationale': '我避开原方案的处理方式，把变化放在相邻的路线关系上；重点观察箱子第一次进入绕行区域时，玩家是否获得新的判断，而不是只增加移动距离。'}
        else:
            offer = {'summary': 'Use a different local detour around the route', 'rationale': "I am avoiding the original treatment and changing the neighboring route relationship instead; the test is whether the box's first entry creates a new judgment rather than only more steps."}
    candidate_summary = str(offer.get('summary') or '').strip().casefold()
    candidate_rationale = str(offer.get('rationale') or '').strip().casefold()
    original_transitions = {(item.get('row'), item.get('column'), item.get('from'), item.get('to'), item.get('anchorEntity')) for item in ((original_binding or {}).get('executionBrief', {}).get('requiredTransitions', []) if isinstance((original_binding or {}).get('executionBrief'), dict) else []) if isinstance(item, dict)}
    candidate_transitions = {(item.get('row'), item.get('column'), item.get('from'), item.get('to'), item.get('anchorEntity')) for item in (offer.get('executionBrief') or {}).get('requiredTransitions', []) if isinstance(item, dict)} if isinstance(offer.get('executionBrief'), dict) else set()
    if candidate_transitions and candidate_transitions != original_transitions:
        return execution
    if ' '.join((str(value or '') for value in offer.values())).strip() == original_text.strip() or candidate_summary == original_summary or candidate_rationale == original_rationale or (original_transitions and candidate_transitions and (original_transitions == candidate_transitions)):
        if language == 'zh-CN':
            offer = {'summary': '把关键推动顺序改成另一种局部取舍', 'rationale': '这次不重复原来的布局方向，而是改变箱子接近目标时的先后关系；试玩时确认新的顺序是否仍然可读且可解。'}
        else:
            offer = {'summary': 'Use a different local trade-off in the push order', 'rationale': 'This avoids the original layout direction and changes the order in which the box approaches the target; play should confirm that the new order remains readable and solvable.'}
    guidance['move'] = 'offer_revision'
    guidance['intentHypothesis'] = None
    guidance['intentConfidence'] = None
    guidance['followUpQuestion'] = None
    guidance['disagreement'] = None
    guidance['proposalOffer'] = offer
    return replace(execution, proposed_rows=None, revision_plan={}, revision_contract={}, revision_operations=[], proposal_diagnostics={}, guidance=guidance)

def _rows_after_frozen_proposal(rows, execution_brief):
    """Replay a card's exact diff only to exclude it from alternative search."""
    if not isinstance(execution_brief, dict):
        return None
    transitions = execution_brief.get('requiredTransitions')
    if not isinstance(transitions, list) or not transitions:
        return None
    candidate = [list(row) for row in rows]
    try:
        for item in transitions:
            row = int(item['row'])
            column = int(item['column'])
            before = str(item['from'])
            after = str(item['to'])
            if candidate[row - 1][column - 1] != before:
                return None
            candidate[row - 1][column - 1] = after
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return tuple((''.join(row) for row in candidate))

def _normalize_manual_edit_review_execution(execution, stage_context):
    """Keep safe manual-edit openings in ordinary prose; retain evidence-backed disputes."""
    if (stage_context or {}).get('source') != 'human_edit':
        return execution
    guidance = dict(execution.guidance or {})
    disagreement = guidance.get('disagreement')
    if isinstance(disagreement, dict) and disagreement.get('status') in {'active', 'acknowledged'}:
        return execution
    if (stage_context or {}).get('discussionCardMode') != 'disagreement_only':
        return execution
    guidance['followUpQuestion'] = None
    guidance['disagreement'] = None
    return replace(execution, guidance=guidance)

def _mark_initial_human_edit_disagreement_card(execution, stage_context):
    """Make the evidence-backed human-edit entry card explicit in stored JSON."""
    if (stage_context or {}).get('source') != 'human_edit':
        return execution
    guidance = dict(execution.guidance or {})
    disagreement = guidance.get('disagreement')
    if not (isinstance(disagreement, dict) and disagreement.get('status') == 'active' and (disagreement.get('subject') == 'human_edit')):
        return execution
    disagreement = dict(disagreement)
    disagreement['displayCard'] = True
    guidance['disagreement'] = disagreement
    return replace(execution, guidance=guidance)

def _enforce_human_edit_disagreement_followup(execution, stage_context):
    """Keep a human-edit disagreement card single-use and bound acknowledgements safe."""
    active = (stage_context or {}).get('activeDisagreement') or {}
    if not (active.get('status') == 'active' and active.get('subject') == 'human_edit'):
        return execution
    guidance = dict(execution.guidance or {})
    disagreement = guidance.get('disagreement')
    if not isinstance(disagreement, dict) or disagreement.get('subject') != 'human_edit':
        return execution
    if disagreement.get('status') not in {'active', 'acknowledged'}:
        return execution
    disagreement = dict(disagreement)
    disagreement['displayCard'] = False
    guidance['disagreement'] = disagreement
    guidance['proposalOffer'] = None
    if disagreement.get('status') == 'acknowledged':
        guidance['move'] = 'offer_perspective'
        guidance['intentHypothesis'] = None
        guidance['intentConfidence'] = None
        guidance.pop('designContextPatch', None)
        guidance.pop('designContextPatchError', None)
        guidance.pop('_intentSemanticClaims', None)
        guidance.pop('_intentDecision', None)
        guidance['uiCues'] = []
    return replace(execution, guidance=guidance, proposed_rows=None, revision_plan={}, revision_contract={}, revision_operations=[], proposal_binding={})

def _mark_new_discussion_guidance(execution, stage_context):
    if (stage_context or {}).get('discussionCardMode') != 'disagreement_only':
        return execution
    guidance = dict(execution.guidance or {})
    guidance['discussionCardMode'] = 'disagreement_only'
    return replace(execution, guidance=guidance)

def _without_model_questions(message):
    """Keep declarative model prose; the server owns proposal questions."""
    sentences = re.split('(?<=[.!?。！？])\\s*', str(message or '').strip())
    return ' '.join((sentence.strip() for sentence in sentences if sentence.strip() and (not sentence.rstrip().endswith(('?', '？'))))).strip()

def _is_snapshot_chat_fallback(message):
    text = str(message or '').strip().casefold()
    return '我会以当前保存的 stage 为准继续分析' in text or 'i will continue from the current saved stage' in text

def _mark_proposal_discovery_guidance(execution, stage_context):
    """Persist server-owned proposal-topic progress without exposing a card."""
    discovery = (stage_context or {}).get('proposalDiscovery') or {}
    routing = (stage_context or {}).get('revisionRouting')
    if not discovery or not discovery.get('topicId'):
        return execution
    guidance = dict(execution.guidance or {})
    marker = {'topicId': discovery['topicId'], 'sourceTurnId': discovery.get('sourceTurnId'), 'status': 'clarifying', 'hasValidatedCandidate': False, 'clarificationQuestionCount': int(discovery.get('clarificationQuestionCount') or 0), 'askedQuestionKeys': list(discovery.get('askedQuestionKeys') or []), 'lastQuestionKey': discovery.get('lastQuestionKey'), **{key: discovery[key] for key in ('optionSet', 'optionQuestion', 'optionDecision') if key in discovery}}
    body = execution.assistant_message
    diagnostics = execution.proposal_diagnostics or {}
    if diagnostics.get('optionAutoPlan'):
        routing = 'proposal_conservative'
        marker['optionSet'] = diagnostics['optionSet']
        if diagnostics.get('optionDecision'):
            marker['optionDecision'] = diagnostics['optionDecision']
        else:
            marker.pop('optionDecision', None)
    specification = (stage_context or {}).get('proposalClarification') or {}
    if routing == 'needs_clarification' and specification.get('optionPolicyVersion') == proposal_options.VERSION:
        option_set = diagnostics.get('optionSet')
        if not option_set or not proposal_options.current_set({**discovery, 'optionSet': option_set}, (stage_context or {}).get('stageSnapshot') or {}):
            raise ApiError(502, 'MODEL_RESPONSE_INVALID', 'No verified proposal directions remain. Retry the original message.', retryable=True)
        marker['optionSet'] = option_set
        marker['optionQuestion'] = diagnostics.get('optionQuestion')
        if (discovery.get('optionDecision') or {}).get('selection') == 'revise':
            marker.pop('optionDecision', None)
        if diagnostics.get('optionOutcome') == 'revision_needed':
            routing = 'proposal_blocked'
        else:
            question = diagnostics.get('clarificationQuestion')
            compact = lambda text: re.sub('\\s+', '', str(text or ''))
            if not question or question not in body or (not diagnostics.get('clarificationQuestionValidated')) or any((compact(item[key]) not in compact(body) for item in option_set['options'] for key in proposal_options.FIELDS - {'components'})):
                raise ApiError(502, 'MODEL_RESPONSE_INVALID', 'The displayed question or directions changed after verification. Retry the message.', retryable=True)
    if (stage_context or {}).get('conversationOnly'):
        marker['status'] = discovery.get('status') or 'clarifying'
        marker['failureEnvelope'] = discovery.get('failureEnvelope')
        remaining = max(0, 3 - marker['clarificationQuestionCount'])
        sentences = []
        for sentence in re.split('(?<=[.!?。！？])\\s*', body):
            if sentence.rstrip().endswith(('?', '？')):
                if remaining <= 0:
                    continue
                remaining -= 1
                marker['clarificationQuestionCount'] += 1
            sentences.append(sentence)
        body = ' '.join(sentences).strip()
        if not body:
            raise ApiError(502, 'MODEL_RESPONSE_INVALID', 'No reliable reply remains within the topic question budget.', retryable=True)
        guidance['proposalDiscovery'] = marker
        return replace(execution, assistant_message=body, guidance=guidance)
    if routing in {'proposal', 'proposal_conservative'}:
        offer = guidance.get('proposalOffer')
        marker['hasValidatedCandidate'] = bool(execution.proposed_rows is not None or (execution.revision_plan and isinstance(offer, dict) and offer.get('executionBrief')))
        marker['status'] = 'proposal_ready' if execution.proposed_rows is not None or (execution.revision_plan and isinstance(offer, dict)) else 'revision_needed'
    if routing == 'needs_clarification':
        specification = (stage_context or {}).get('proposalClarification')
        if isinstance(specification, dict) and specification.get('questionKey'):
            question_key = str(specification.get('questionKey') or '').strip()
            diagnostics = dict(execution.proposal_diagnostics or {})
            question = str(diagnostics.get('clarificationQuestion') or '').strip()
            fallback_question = str(specification.get('fallbackQuestion') or '').strip()
            fallback_acknowledgement = str(specification.get('fallbackAcknowledgement') or '').strip()
            recovery_mode = str(diagnostics.get('clarificationRecoveryMode') or 'kimi_complete')
            clarification_author = str(diagnostics.get('clarificationAuthor') or '').strip()
            if not question or question not in str(body or ''):
                cleaned_body = _without_model_questions(body)
                if not cleaned_body or _is_snapshot_chat_fallback(cleaned_body):
                    cleaned_body = fallback_acknowledgement
                    clarification_author = 'server_fallback'
                    recovery_mode = 'server_fallback'
                else:
                    clarification_author = 'mixed'
                    recovery_mode = 'question_replaced'
                question = fallback_question
                body = '\n\n'.join((part for part in (cleaned_body, question) if part))
            else:
                clarification_author = clarification_author or 'kimi'
            asked_keys = marker['askedQuestionKeys']
            count_before = max(marker['clarificationQuestionCount'], int(specification.get('countBefore') or 0))
            if question_key and question_key not in asked_keys:
                asked_keys.append(question_key)
                count_after = min(3, count_before + 1)
            else:
                count_after = count_before
            marker.update({'clarificationQuestionKey': question_key or None, 'clarificationQuestionText': question, 'lastQuestionKey': question_key or marker.get('lastQuestionKey'), 'askedQuestionKeys': asked_keys, 'clarificationCountBefore': count_before, 'clarificationCountAfter': count_after, 'clarificationQuestionCount': count_after, 'clarificationRecoveryMode': recovery_mode, 'clarificationAuthor': clarification_author, 'proposalTopicContinued': bool(specification.get('topicContinued'))})
            guidance.update({'move': 'clarify_intent', 'intentHypothesis': None, 'intentConfidence': None, 'followUpQuestion': None, 'proposalOffer': None, 'disagreement': None, 'uiCues': [], 'coordinateLinks': []})
            diagnostics.update({'clarificationQuestionKey': question_key or None, 'clarificationCountBefore': count_before, 'clarificationCountAfter': count_after, 'clarificationRecoveryMode': recovery_mode, 'clarificationAuthor': clarification_author, 'clarificationTargetDimension': question_key or None, 'proposalTopicContinued': bool(specification.get('topicContinued'))})
            execution = replace(execution, proposal_diagnostics=diagnostics)
        else:
            pass
    elif routing == 'proposal_blocked':
        marker['status'] = 'revision_needed'
    elif routing == 'proposal_cancelled':
        marker['status'] = 'cancelled'
    failure_envelope = (execution.proposal_diagnostics or {}).get('failureEnvelope')
    count_before = int(discovery.get('clarificationQuestionCount') or 0)
    questions = _visible_proposal_questions(body)
    if routing != 'needs_clarification' or count_before >= 3:
        body = _without_model_questions(body)
        questions = []
    elif len(questions) > 3 - count_before:
        remaining_questions = 3 - count_before
        sentences = []
        for sentence in re.split('(?<=[.!?。！？])\\s*', str(body)):
            if sentence.rstrip().endswith(('?', '？')):
                if remaining_questions <= 0:
                    continue
                remaining_questions -= 1
            sentences.append(sentence)
        body = ' '.join(sentences)
        questions = _visible_proposal_questions(body)
    marker['clarificationQuestionCount'] = min(3, count_before + len(questions))
    marker['clarificationCountBefore'] = count_before
    marker['clarificationCountAfter'] = marker['clarificationQuestionCount']
    marker['questionTexts'] = questions
    guidance['followUpQuestion'] = None
    if marker.get('status') == 'revision_needed' and isinstance(failure_envelope, dict):
        marker['failureEnvelope'] = failure_envelope
        marker['canSupplement'] = True
    guidance['proposalDiscovery'] = marker
    return replace(execution, assistant_message=body, guidance=guidance)

def _proposal_tile_label(tile, language):
    labels = {'zh-CN': {'#': '墙（#）', '.': '地面（.）', '@': '水域（@）', 'p': '玩家（p）', 's': '箱子（s）', 't': '目标（t）'}, 'en': {'#': 'wall (#)', '.': 'floor (.)', '@': 'water (@)', 'p': 'player (p)', 's': 'box (s)', 't': 'target (t)'}}
    return labels['zh-CN' if language == 'zh-CN' else 'en'].get(tile, tile)

@proposal_phase('proposal_presentation', 'presentation')
def _proposal_presentation_for_binding(binding, language='en', summary=None, rationale=None, source_presentation=None):
    """Create the safe, human-readable projection of an exact proposal."""
    brief = binding.get('executionBrief') if isinstance(binding, dict) else None
    if not isinstance(brief, dict):
        return None
    transitions = brief.get('requiredTransitions') or []
    if not transitions:
        return None
    if isinstance(source_presentation, dict) and source_presentation.get('schemaVersion') == 2:
        request_text = _inline_display_text(source_presentation.get('requestText'))
        answer_texts = [_inline_display_text(item) for item in source_presentation.get('answerTexts') or [] if _inline_display_text(item)][:3]
        solution = source_presentation.get('solution') or {}
        minimum_moves = solution.get('minimumMoves')
        route_pushes = solution.get('pushesOnMinimumMoveSolution')
        if request_text and isinstance(minimum_moves, int) and isinstance(route_pushes, int):
            return {'schemaVersion': 2, **({'binaryCompact': True, 'verifiedActions': list(source_presentation.get('verifiedActions') or []), 'verification': source_presentation.get('verification', '')} if source_presentation.get('binaryCompact') else {}), 'requestText': request_text[:2000], 'answerTexts': [item[:2000] for item in answer_texts], 'changes': [{'row': item['row'], 'column': item['column'], 'before': item['from'], 'after': item['to']} for item in transitions], 'reason': _safe_proposal_reason(source_presentation.get('reason'), brief, language), 'solution': {'minimumMoves': minimum_moves, 'pushesOnMinimumMoveSolution': route_pushes}}
    presentation = {'schemaVersion': 1, 'changes': [{'row': item['row'], 'column': item['column'], 'before': item['from'], 'after': item['to'], 'beforeLabel': _proposal_tile_label(item['from'], language), 'afterLabel': _proposal_tile_label(item['to'], language)} for item in transitions], 'preserved': list(brief.get('preserve') or [])}
    workflow = binding.get('revisionWorkflow') or {}
    if workflow:
        presentation['objective'] = str(workflow.get('objective') or '')[:1200]
        presentation['mustSatisfy'] = list(dict.fromkeys((str(item.get('sourceText') or item.get('kind') or '').strip() for item in workflow.get('semanticConstraints') or [] if str(item.get('sourceText') or item.get('kind') or '').strip())))[:8]
        soft_labels = {'planning_depth': ('增强需要提前规划的路线判断', 'strengthen route judgments that require advance planning'), 'box_dependency': ('增强箱子之间可验证的推动依赖', 'strengthen verifiable push dependencies between boxes'), 'shared_blocking': ('增强共享通道中的局部阻挡关系', 'strengthen local blocking relationships in shared passages'), 'longer_transport': ('增强较长推运过程中的选择', 'strengthen choices during longer box transport'), 'space_route_choice': ('增强空间分布带来的路线选择', 'strengthen route choices created by spatial distribution')}
        language_index = 0 if language == 'zh-CN' else 1
        presentation['tryToAchieve'] = [soft_labels.get(str(item.get('statement') if isinstance(item, dict) else item), (str(item.get('statement') if isinstance(item, dict) else item), str(item.get('statement') if isinstance(item, dict) else item)))[language_index] for item in workflow.get('softObjectives') or [] if str(item.get('statement') if isinstance(item, dict) else item).strip()][:6]
    if isinstance(summary, str) and summary.strip():
        presentation['summary'] = summary.strip()[:600]
    if isinstance(rationale, str) and rationale.strip():
        presentation['rationale'] = rationale.strip()[:1000]
    return presentation

def _execution_binding_clarification(language):
    if language == 'zh-CN':
        return '请明确指出要调整的区域或实体，以及每个格子修改前后的状态；我会先按当前保存的 Stage 核对，再继续给出一个方向。'
    return "Please identify the area or entity to adjust and state each cell's current and desired tile. I will check it against the current saved Stage before proposing a direction."

def _revision_contract_clarification_execution(*, language, request_id, exception):
    """Expose contract rejection as ordinary, non-executable body prose."""
    execution = _revision_contract_invalid_execution(language=language, request_id=request_id, exception=exception)
    clarification = _execution_binding_clarification(language)
    guidance = dict(execution.guidance or {})
    guidance['move'] = 'clarify_intent'
    guidance['proposalOffer'] = None
    guidance['uiCues'] = []
    return replace(execution, assistant_message=clarification, guidance=guidance, proposal_diagnostics={**(execution.proposal_diagnostics or {}), 'category': 'execution_binding_rejected'})

def _bind_execution_to_stage(execution, version_id, rows, content='', language='en', entity_bindings=None):
    """Attach backend-only proposal metadata without changing visible assistant prose."""
    guidance = dict(execution.guidance or {})
    offer = guidance.get('proposalOffer')
    if not isinstance(offer, dict) or not str(offer.get('summary') or '').strip():
        return replace(execution, proposal_binding={})
    if offer.get('executionBrief') is None and 'revisionPlan' in offer:
        try:
            normalized_offer = dict(offer)
            normalized_offer['executionBrief'] = normalize_revision_plan(offer.get('revisionPlan'), rows, entity_bindings)
            normalized_offer.pop('revisionPlan', None)
            offer = normalized_offer
            guidance['proposalOffer'] = offer
        except (TypeError, ValueError):
            pass
    try:
        binding = _proposal_binding_for_offer(offer, version_id, rows, entity_bindings)
    except (TypeError, ValueError):
        clarification = _execution_binding_clarification(language)
        guidance['proposalOffer'] = None
        guidance['move'] = 'clarify_intent'
        guidance['followUpQuestion'] = None
        guidance['uiCues'] = []
        return replace(execution, assistant_message=execution.assistant_message.strip() if str(execution.assistant_message or '').strip() else clarification, guidance=guidance, proposal_binding={}, proposal_diagnostics={**(execution.proposal_diagnostics or {}), 'category': 'execution_binding_rejected'})
    presentation = _proposal_presentation_for_binding(binding, language, offer.get('summary'), offer.get('rationale'), offer.get('proposalPresentation'))
    if presentation is not None:
        public_offer = dict(offer)
        public_offer['proposalPresentation'] = presentation
        guidance['proposalOffer'] = public_offer
    return replace(execution, guidance=guidance, proposal_binding=binding)

def _stage_has_prior_assistant_reply(database, session_id, version_id):
    """Return whether this Stage already has its opening assistant reply."""
    opening = database.execute('SELECT 1 FROM llm_assessments WHERE session_id = ? AND version_id = ? LIMIT 1', (session_id, version_id)).fetchone()
    if opening is not None:
        return True
    accepted_opening = database.execute("\n        SELECT 1\n        FROM designer_decisions AS decision\n        JOIN change_proposals AS proposal\n          ON proposal.id = decision.proposal_id\n        WHERE decision.session_id = ?\n          AND decision.version_id = ?\n          AND decision.decision_type = 'accept'\n          AND proposal.assistant_turn_id IS NOT NULL\n        LIMIT 1\n        ", (session_id, version_id)).fetchone()
    if accepted_opening is not None:
        return True
    return database.execute("\n        SELECT 1 FROM conversation_turns\n        WHERE session_id = ? AND version_id = ? AND role = 'assistant'\n        LIMIT 1\n        ", (session_id, version_id)).fetchone() is not None
_proposal_relaxation_fallback_after_failure = _proposal_search_failure_execution
