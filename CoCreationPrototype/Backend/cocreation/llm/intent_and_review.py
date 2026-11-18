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

from .config_and_runtime import DISAGREEMENT_RESOLUTIONS, DISAGREEMENT_STATUSES, DISAGREEMENT_SUBJECTS, EmptyModelResponse, INTENT_CANDIDATE_REVIEW_MAX_COMPLETION_TOKENS, INTENT_COMPONENT_REPAIR_MAX_COMPLETION_TOKENS, INTENT_FEEDBACK_REVIEW_MAX_COMPLETION_TOKENS, INTENT_PROGRESS_REWRITE_MAX_COMPLETION_TOKENS, INTENT_REVIEW_VERSION, KIMI_MODEL, LLMServiceError, LowQualityModelResponse, MANUAL_EDIT_CONFLICT_MAX_COMPLETION_TOKENS, MANUAL_EDIT_DIRECTION_KINDS, MANUAL_EDIT_EFFECT_EVIDENCE_KINDS, MIN_RETRY_BUDGET_SECONDS, QUESTION_ANSWER_REVIEW_MAX_COMPLETION_TOKENS, _clean_text, _content_block_sentences, _current_service, _dialogue_background_prompt, _latest_role_content, _log_llm_event, _request_completion, _request_component_patch, _request_deadline, _safe_validation_reason, _visible_reply_style, classify_exception
from .snapshot_grounding import _assert_visible_output_language, _guidance_reuses_visible_sentence, _guidance_text_matches, _intent_sentence_count, _normalize_response_paragraphs, _normalize_single_level_language, _validate_map_grounding_texts


_DESIGN_TOPIC_TERMS_ZH = {'entities': ('关卡', '地图', '布局', '排版', '排布', '构图', '水域', '水面', '水边', '水塘', '水格', '墙', '墙体', '箱子', '木箱', '箱体', '目标', '目标点', '终点', '落点', '玩家', '玩家位置', '起点', '出生点', '地面', '地板', '空地', '格子', '方格', '地块', '障碍', '障碍物'), 'topology': ('空间', '路线', '路径', '道路', '通道', '走廊', '入口', '出口', '岔路', '分叉', '转角', '转折', '死路', '死角', '角落', '区域', '局部', '中央', '中间', '边界', '边缘', '外壳', '轮廓', '左侧', '右侧', '上方', '下方'), 'relations': ('位置', '距离', '间距', '相邻', '靠近', '贴近', '紧邻', '紧挨', '分开', '远离', '远近', '集中', '分散', '分布', '对称', '平衡', '密度', '拥挤', '密集', '紧凑'), 'mechanics': ('推动', '推箱', '推法', '推数', '推动次数', '顺序', '步数', '解法', '最短解', '最短路线', '可解', '死锁', '软锁', '卡死', '卡住', '退路', '回头路', '回旋', '绕行', '移动', '运输', '重开', '试错'), 'experience': ('难度', '节奏', '选择', '体验', '游玩', '玩法', '思考', '探索', '判断', '压力', '压迫', '可读', '清晰', '直观', '自然', '公平', '流畅', '顺畅', '挑战', '犹豫', '停顿', '紧张', '趣味', '有趣', '无聊', '单调', '成就', '自由', '多样', '观感', '美观', '外观', '视觉', '好看', '形状', '造型', '色彩', '简单', '容易', '复杂', '空旷', '空荡', '拥挤', '时间')}
_DESIGN_TOPIC_TERMS_EN = {'entities': ('level', 'map', 'layout', 'water', 'wall', 'box', 'boxes', 'crate', 'crates', 'target', 'targets', 'goal', 'goals', 'player', 'start', 'spawn', 'floor', 'ground', 'tile', 'tiles', 'cell', 'cells', 'grid', 'obstacle', 'obstacles'), 'topology': ('space', 'route', 'routes', 'path', 'paths', 'road', 'roads', 'corridor', 'corridors', 'passage', 'passages', 'entrance', 'exit', 'fork', 'branch', 'turn', 'corner', 'dead end', 'area', 'region', 'boundary', 'edge', 'outline'), 'relations': ('position', 'distance', 'spacing', 'adjacent', 'adjacency', 'near', 'nearby', 'close', 'apart', 'separate', 'separation', 'far', 'cluster', 'concentrated', 'distribution', 'symmetry', 'symmetric', 'balance', 'balanced', 'density', 'dense', 'sparse', 'cramped', 'compact'), 'mechanics': ('push', 'pushes', 'push order', 'move', 'moves', 'step', 'steps', 'solution', 'solvable', 'solvability', 'deadlock', 'soft lock', 'stuck', 'escape', 'return route', 'detour', 'transport', 'restart', 'trial and error'), 'experience': ('difficulty', 'pacing', 'rhythm', 'choice', 'experience', 'gameplay', 'play', 'thinking', 'exploration', 'judgment', 'pressure', 'readable', 'readability', 'clear', 'clarity', 'intuitive', 'natural', 'fair', 'smooth', 'challenge', 'hesitation', 'pause', 'tension', 'fun', 'boring', 'monotonous', 'achievement', 'freedom', 'variety', 'appearance', 'aesthetic', 'visual', 'shape', 'color', 'easy', 'hard', 'complex', 'empty', 'crowded')}
_INTENT_SUBJECT_TERMS_ZH = (('layout', ('排版', '排布', '构图', '布局')), ('space', ('空间', '区域', '走廊', '通道', '空地')), ('route', ('路线', '路径', '路程', '走法')), ('target', ('目标', '终点', '落点')), ('water', ('水域', '水面', '水格', '水')), ('wall', ('墙体', '墙', '障碍')), ('box', ('箱子', '木箱', '箱体')), ('difficulty', ('难度', '难', '容易', '简单')), ('rhythm', ('节奏', '绕行', '回旋')))
_INTENT_SUBJECT_TERMS_EN = (('layout', ('layout', 'composition', 'arrangement')), ('space', ('space', 'area', 'corridor', 'passage', 'floor')), ('route', ('route', 'path', 'journey')), ('target', ('target', 'targets', 'goal', 'goals')), ('water', ('water',)), ('wall', ('wall', 'walls', 'obstacle', 'obstacles')), ('box', ('box', 'boxes', 'crate', 'crates')), ('difficulty', ('difficulty', 'hard', 'easy')), ('rhythm', ('rhythm', 'pacing', 'detour')))
_INTENT_PROPERTY_TERMS_ZH = (('coverage', ('占地过大', '占地太大', '覆盖过大', '覆盖太大', '覆盖过小', '覆盖太小', '覆盖不足', '覆盖面积')), ('crowded', ('太拥挤', '过于拥挤', '拥挤', '太挤', '过密', '密集', '拥堵', '狭促')), ('empty', ('太空', '空旷', '空荡', '显得空')), ('complex', ('太复杂', '复杂')), ('short', ('太短', '过短', '短')), ('long', ('太长', '过长', '长')), ('clear', ('不清晰', '不够清晰', '清晰', '可读')), ('concentrated', ('太集中', '集中', '靠在一起', '太近')), ('separated', ('分开', '间距', '太远')), ('amount', ('太多', '过多', '太少', '过少', '不够多', '数量', '更多', '更少', '多一点', '少一点', '增加', '增多', '减少')))
_INTENT_PROPERTY_TERMS_EN = (('coverage', ('too large', 'too small', 'coverage', 'footprint', 'takes up too much', 'takes up too little')), ('crowded', ('too crowded', 'crowded', 'cramped', 'too dense', 'packed')), ('empty', ('too empty', 'empty', 'sparse', 'vacant')), ('complex', ('too complex', 'complex')), ('short', ('too short', 'short')), ('long', ('too long', 'long')), ('clear', ('unclear', 'not clear', 'clear', 'clearer', 'readable', 'readability', 'easier to read', 'legible', 'legibility')), ('concentrated', ('too concentrated', 'concentrated', 'too close')), ('separated', ('separate', 'separated', 'far apart')), ('amount', ('too much', 'too many', 'too few', 'too little', 'not enough water', 'more', 'less', 'fewer', 'increase', 'reduce', 'decrease', 'amount', 'quantity')))
_INTENT_LABELS_ZH = {'layout': '地图布局', 'space': '空间安排', 'route': '路线', 'target': '目标关系', 'water': '水域', 'wall': '墙体与障碍', 'box': '箱子关系', 'difficulty': '难度', 'rhythm': '路线节奏', 'crowded': '拥挤感', 'empty': '空旷感', 'complex': '复杂度', 'short': '偏短', 'long': '偏长', 'clear': '清晰度', 'concentrated': '集中关系', 'separated': '分离关系', 'amount': '数量', 'coverage': '覆盖范围'}
_INTENT_LABELS_EN = {'layout': 'layout', 'space': 'spatial arrangement', 'route': 'route', 'target': 'target relationship', 'water': 'water', 'wall': 'walls and obstacles', 'box': 'box relationship', 'difficulty': 'difficulty', 'rhythm': 'route rhythm', 'crowded': 'crowded feeling', 'empty': 'emptiness', 'complex': 'complexity', 'short': 'shortness', 'long': 'length', 'clear': 'clarity', 'concentrated': 'concentration', 'separated': 'separation', 'amount': 'amount', 'coverage': 'coverage'}

def _manual_edit_review_evidence(stage_context, solver_metrics, play_summary, language='en'):
    """Build bounded, server-owned evidence that a manual-edit review may cite."""
    context = stage_context or {}
    evidence = []
    for index, change in enumerate((context.get('diff') or [])[:24], start=1):
        if not isinstance(change, dict):
            continue
        try:
            row = int(change.get('y')) + 1
            column = int(change.get('x')) + 1
        except (TypeError, ValueError):
            continue
        before = str(change.get('before') or ' ')[:1]
        after = str(change.get('after') or ' ')[:1]
        evidence.append({'id': f'diff-{index}', 'kind': 'verified_map_diff', 'fact': {'row': row, 'column': column, 'beforeSymbol': before, 'beforeTile': TILE_LEGEND.get(before, 'unknown'), 'afterSymbol': after, 'afterTile': TILE_LEGEND.get(after, 'unknown')}})
    before_rows = context.get('beforeRows')
    after_rows = context.get('afterRows')
    if isinstance(before_rows, list) and isinstance(after_rows, list):
        evidence.append({'id': 'stage-rows', 'kind': 'verified_stage_rows', 'fact': {'parentRows': [str(row) for row in before_rows], 'currentRows': [str(row) for row in after_rows]}})
    change_summary = context.get('changeSummary')
    verified_diff = str(context.get('verifiedDiff') or '').strip()
    if isinstance(before_rows, list) and isinstance(after_rows, list):
        verified_diff = summarize_verified_diff(before_rows, after_rows, language=language)
    if isinstance(change_summary, dict) or verified_diff:
        evidence.append({'id': 'verified-change-summary', 'kind': 'verified_change_summary', 'fact': {'structured': change_summary if isinstance(change_summary, dict) else {}, 'description': verified_diff[:1600]}})
    current_solver = solver_metrics or {}
    if current_solver.get('solvable'):
        evidence.append({'id': 'solver-current', 'kind': 'solver', 'fact': {'solvable': True, 'solutionSteps': current_solver.get('solutionSteps'), 'solutionPushes': current_solver.get('solutionPushes')}})
    parent_solver = context.get('parentValidation') or {}
    if parent_solver.get('solvable') and current_solver.get('solvable'):
        evidence.append({'id': 'solver-delta', 'kind': 'solver_comparison', 'fact': {'parentSolutionSteps': parent_solver.get('solutionSteps'), 'currentSolutionSteps': current_solver.get('solutionSteps'), 'parentSolutionPushes': parent_solver.get('solutionPushes'), 'currentSolutionPushes': current_solver.get('solutionPushes')}})
    if isinstance(play_summary, dict) and play_summary:
        evidence.append({'id': 'play-current', 'kind': 'play_evidence', 'fact': {key: play_summary.get(key) for key in ('status', 'moveCount', 'pushCount', 'restartCount', 'minimumMoves', 'minimumPushes')}})
    design = context.get('evaluatorDesignContext') or {}
    for index, item in enumerate((design.get('intentHypotheses') or [])[-12:], start=1):
        if not isinstance(item, dict) or item.get('status') != 'confirmed':
            continue
        statement = str(item.get('displayStatement') or item.get('statement') or '').strip()
        semantic_claims = [claim for claim in item.get('semanticClaims') or [] if isinstance(claim, dict)]
        if not statement:
            continue
        evidence.append({'id': f'confirmed_inclination-{index}', 'kind': 'confirmed_inclination', 'fact': statement[:800], 'semanticClaims': semantic_claims[:4], 'sourceStageId': item.get('sourceStageId'), 'sourceTurnId': item.get('sourceTurnId')})
    confirmed_inclination_keys = {str(item.get('fact') or '').strip().casefold() for item in evidence if item.get('kind') == 'confirmed_inclination'}
    confirmed_inclination_turn_ids = {item.get('sourceTurnId') for item in evidence if item.get('kind') == 'confirmed_inclination' and item.get('sourceTurnId')}
    design_sources = (('userGoals', 'goal', 'design_goal'), ('designConstraints', 'constraint', 'design_constraint'), ('confirmedDecisions', 'decision', 'confirmed_decision'))
    for collection, text_key, kind in design_sources:
        for index, item in enumerate((design.get(collection) or [])[-12:], start=1):
            if not isinstance(item, dict) or item.get('status') != 'active':
                continue
            if collection != 'confirmedDecisions' and item.get('authority') not in {'explicit', 'confirmed'}:
                continue
            statement = str(item.get(text_key) or '').strip()
            if not statement:
                continue
            if collection == 'userGoals' and (statement.casefold() in confirmed_inclination_keys or item.get('sourceTurnId') in confirmed_inclination_turn_ids):
                continue
            evidence.append({'id': f'{kind}-{index}', 'kind': kind, 'fact': statement[:800], 'sourceStageId': item.get('sourceStageId')})
    return evidence[:96]

def _manual_edit_has_design_evidence(evidence):
    return any((item.get('kind') in MANUAL_EDIT_DIRECTION_KINDS for item in evidence or [] if isinstance(item, dict)))

def _manual_edit_solver_delta(evidence):
    item = next((entry for entry in evidence or [] if isinstance(entry, dict) and entry.get('id') == 'solver-delta'), None)
    return dict(item.get('fact') or {}) if item else {}

def _manual_edit_direction_evidence(evidence):
    return [item for item in evidence or [] if isinstance(item, dict) and item.get('kind') in MANUAL_EDIT_DIRECTION_KINDS and item.get('id')]

def _manual_edit_effect_evidence(evidence):
    """Return the bounded server-owned evidence that may support a comparison."""
    eligible = [item for item in evidence or [] if isinstance(item, dict) and item.get('kind') in MANUAL_EDIT_EFFECT_EVIDENCE_KINDS and item.get('id')]
    priority = {'verified_change_summary': 0, 'verified_map_diff': 1, 'solver_comparison': 2, 'play_evidence': 3}
    return sorted(eligible, key=lambda item: (priority.get(item.get('kind'), 99), str(item.get('id'))))[:12]

def _validate_manual_edit_conflict_classification(payload, evidence):
    if not isinstance(payload, dict) or set(payload) != {'comparisons'}:
        raise ValueError('The manual-edit conflict classification has an invalid envelope.')
    directions = _manual_edit_direction_evidence(evidence)
    effect_evidence_ids = [item['id'] for item in _manual_edit_effect_evidence(evidence)]
    raw_comparisons = payload.get('comparisons')
    expected_slots = [f'direction_{index}' for index in range(1, len(directions) + 1)]
    if not isinstance(raw_comparisons, dict) or set(raw_comparisons) != set(expected_slots):
        raise ValueError('The manual-edit conflict classification requires comparisons.')
    comparisons = []
    for slot, direction in zip(expected_slots, directions):
        raw = raw_comparisons.get(slot)
        if not isinstance(raw, dict) or set(raw) != {'relation', 'explanation'}:
            raise ValueError('A manual-edit direction comparison has an invalid envelope.')
        relation = raw.get('relation')
        if relation not in {'conflict', 'aligned', 'tradeoff', 'unrelated', 'unclear'}:
            raise ValueError('A manual-edit direction comparison has an invalid relation.')
        if not effect_evidence_ids:
            raise ValueError('Every manual-edit comparison must cite concrete edit evidence.')
        explanation = str(raw.get('explanation') or '').strip()
        if not explanation:
            raise ValueError('Every manual-edit comparison requires an explanation.')
        comparisons.append({'confirmedDirectionId': direction['id'], 'relation': relation, 'evidenceIds': list(effect_evidence_ids), 'explanation': explanation[:800]})
    conflicts = [item for item in comparisons if item['relation'] == 'conflict']
    verdict = 'conflict' if conflicts else 'unclear' if any((item['relation'] == 'unclear' for item in comparisons)) else 'no_conflict'
    relevant = conflicts if conflicts else comparisons
    direction_ids = [item['confirmedDirectionId'] for item in relevant]
    effect_ids = list(dict.fromkeys((evidence_id for item in relevant for evidence_id in item['evidenceIds'])))
    return {'verdict': verdict, 'decisionSource': 'kimi_adjudication', 'confirmedDirectionIds': direction_ids, 'effectEvidenceIds': effect_ids, 'relation': '\n'.join((item['explanation'] for item in relevant))[:1600], 'solverDelta': _manual_edit_solver_delta(evidence), 'comparisons': comparisons}

def _classify_manual_edit_conflict(evidence, language, request_id, deadline):
    svc = _current_service()
    started_at = svc.clock.monotonic()
    directions = _manual_edit_direction_evidence(evidence)
    effect_evidence = _manual_edit_effect_evidence(evidence)
    if not directions or not effect_evidence:
        return {'verdict': 'unrelated', 'decisionSource': 'deterministic_no_comparable_evidence', 'confirmedDirectionIds': [], 'effectEvidenceIds': [], 'relation': 'no_comparable_confirmed_direction_and_effect', 'solverDelta': _manual_edit_solver_delta(evidence), 'comparisons': []}
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    response_language = 'Simplified Chinese' if language == 'zh-CN' else 'English'
    direction_slots = {f'direction_{index}': {'kind': item.get('kind'), 'fact': item.get('fact'), 'semanticClaims': item.get('semanticClaims') or []} for index, item in enumerate(directions, start=1)}
    effect_facts = [{'kind': item.get('kind'), 'fact': item.get('fact')} for item in effect_evidence]
    messages = [{'role': 'system', 'content': f'You are the Kimi K2.6 manual-edit conflict adjudicator. Decide only whether the verified edit conflicts with each active explicit/confirmed design direction. Fill exactly the server-provided direction slots, with no omissions or additional slots. You—not the server—judge whether each relationship is conflict, aligned, tradeoff, unrelated, or unclear. Do not return direction IDs or evidence IDs; the server owns those bindings. Judge only from the eligible effect evidence supplied below. The map legend is: # wall, . floor, @ water, p player, s box, t target. Never apply conventional Sokoban glyph meanings to this project. Treat verified_change_summary as the authoritative semantic description of changed tile types when it is present. Use conflict when the edit directly reverses a confirmed direction, even if it improves another metric; reserve tradeoff for competing effects without a direct reversal. Aesthetic difference alone is not conflict. Solver improvement may coexist with conflict and must not erase a directional contradiction. Use unclear when evidence is insufficient. Write each explanation in {response_language}. Direction slots:\n' + json.dumps(direction_slots, ensure_ascii=False, separators=(',', ':')) + '\nEligible effect evidence:\n' + json.dumps(effect_facts, ensure_ascii=False, separators=(',', ':'))}]
    last_error = None
    model = svc.config.model
    for attempt, cap in enumerate((25.0, 15.0), start=1):
        raw_content = None
        remaining = svc.remaining_until(deadline)
        if remaining < svc.config.min_retry_budget_seconds:
            break
        timeout_seconds = min(cap, remaining)
        _log_llm_event('llm_attempt_started', requestId=request_id, task='manual_edit_conflict_classification', model=model, attempt=attempt, maxAttempts=2, timeoutSeconds=round(timeout_seconds, 3), responseMode='json_schema')
        try:
            response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, model, messages, MANUAL_EDIT_CONFLICT_MAX_COMPLETION_TOKENS, timeout_seconds, task='manual_edit_conflict_classification', manual_edit_direction_count=len(directions)), timeout=timeout_seconds))
            choice = response.choices[0]
            if str(getattr(choice, 'finish_reason', '') or '') == 'length':
                raise ValueError('The manual-edit conflict classification reached its output limit.')
            raw_content = str(choice.message.content or '')
            decision = _validate_manual_edit_conflict_classification(json.loads(raw_content), evidence)
            _log_llm_event('llm_request_completed', requestId=request_id, task='manual_edit_conflict_classification', outcome='success', model=model, attemptsUsed=attempt, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), verdict=decision.get('verdict'), comparisonCount=len(decision.get('comparisons') or []))
            return decision
        except asyncio.TimeoutError:
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not complete the manual-edit conflict classification in time.', request_id, True, attempt, 504)
            _log_llm_event('llm_attempt_failed', requestId=request_id, task='manual_edit_conflict_classification', model=model, attempt=attempt, maxAttempts=2, code=last_error.code, retryable=True, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), validationReason='The Kimi request exceeded the classification time budget.')
        except LLMServiceError as exception:
            _log_llm_event('llm_attempt_failed', requestId=request_id, task='manual_edit_conflict_classification', model=model, attempt=attempt, maxAttempts=2, code=exception.code, retryable=exception.retryable, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), validationReason=_safe_validation_reason(exception))
            raise
        except Exception as exception:
            last_error = classify_exception(exception, request_id, attempt)
            last_error.retryable = True
            _log_llm_event('llm_attempt_failed', requestId=request_id, task='manual_edit_conflict_classification', model=model, attempt=attempt, maxAttempts=2, code=last_error.code, retryable=True, latencyMs=int((svc.clock.monotonic() - started_at) * 1000), validationReason=_safe_validation_reason(exception))
            if raw_content and attempt < 2:
                messages.extend([{'role': 'assistant', 'content': raw_content}, {'role': 'user', 'content': f'Your classification failed server validation: {exception}. Return a corrected JSON object with exactly the supplied direction slots, one valid relation and one non-empty explanation per slot, and no direction IDs, evidence IDs, omissions, or additional fields.'}])
    if last_error is not None:
        raise last_error
    raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not complete the manual-edit conflict classification in time.', request_id, True, 0, 504)

def _assert_no_unsupported_manual_edit_intent(texts, evidence):
    """Reject designer-intent attribution when no explicit/confirmed evidence exists."""
    if _manual_edit_has_design_evidence(evidence):
        return
    text = '\n'.join((str(item or '') for item in texts))
    chinese_patterns = ('(?:隐性|潜在|内在|背后(?:的)?)[^。！？\\n]{0,16}(?:追求|意图|倾向|偏好|目标|希望)', '(?:你|用户|玩家|设计者|设计师)(?:所)?(?:想要|希望|意图|倾向|偏好|追求|目标)', '(?:符合|呼应|延续|支持|实现|背离|违背)[^。！？\\n]{0,28}(?:你的|用户的|玩家的|设计者的|设计师的)?(?:意图|倾向|偏好|追求|目标|希望)')
    english_patterns = ("\\b(?:your|the (?:user|designer|player)'s)\\s+(?:implicit\\s+)?(?:intent(?:ion)?|preference|pursuit|goal|aim|hope|motivation)\\b", '\\b(?:you|the (?:user|designer|player))\\s+(?:want(?:ed)?|hope(?:d)?|intend(?:ed)?|prefer(?:red)?|aim(?:ed)?|seek|sought)\\b', '\\b(?:implicit|hidden|underlying)\\s+(?:intent(?:ion)?|preference|pursuit|goal|aim|hope|motivation)\\b')
    if any((re.search(pattern, text, re.IGNORECASE) for pattern in (*chinese_patterns, *english_patterns))):
        raise ValueError('The manual-edit review attributes an unsupported designer intention.')

@proposal_phase('understanding', 'understanding', revalidates='understanding')
def classify_turn_understanding(conversation, snapshot, request_id, *, forced_proposal=False, proposal_context=None, _deadline=None):
    """Ask Kimi what the designer referred to and what action they requested.

    This is semantic routing only. The snapshot, proposal authorization, and
    executable tile transitions remain server-owned.
    """
    svc = _current_service()
    latest = _latest_role_content(conversation, 'user').strip()
    _deadline = _deadline or _request_deadline(svc.clock.monotonic())
    if not latest:
        raise ValueError('A user message is required for turn understanding.')
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    previous_user_wording = [str(item.get('content') or '')[:400] for item in conversation[:-1] if item.get('role') == 'user'][-3:]
    messages = [{'role': 'system', 'content': "Classify the latest designer message as natural language, not by keyword lookup. Return only the requested JSON. Acts may overlap: evaluation describes a reaction; intent states a desired experience without requesting a map operation; explanation_request asks what a map element or label means or does; idea_request asks for conceptual design thoughts; revision_request asks for a concrete map plan or asks you to change the map. Do not promote an evaluation, explanation question, or intention into a revision request. A request to change an element may be ambiguous about how; directionSufficient is true only when the desired effect and referent are clear enough to begin proposal discovery. For example, '我想让里面更通透' states intent, '里面怎样能更通透' asks for ideas, '里面太堵了' evaluates, and '给我一个修改方案' requests a proposal. These illustrate speech acts, not an element synonym list. Infer references from ordinary wording and context. The canonical elements are water, internal walls, connected outer shell, player start, boxes, and target destinations. A comment about the map's shape or contour normally refers to its outer shell even without a technical label, unless context points to internal composition; retain uncertainty in the subsequent reply. An overall appearance remark may refer to more than one element; use unknown only when no particular element is a defensible primary referent. The visible map labels P, B, and T refer to the player, boxes, and targets; numbered labels identify individual boxes or targets. '那个T是什么' is an explanation_request about targets, not a request for design ideas. Set mapRelated false for requests clearly about another subject. Copy evidenceSpan exactly from the latest user message. Earlier user wording is linguistic context only; current map facts come exclusively from Current Stage Snapshot. Never infer permission to edit from the map itself. Platform permissions are fixed: only water and internal walls may be edited. Judge whether an unnamed layout request concerns editable composition from its actual meaning and context. Do not infer a box/target/player/shell relocation from vague elements or an earlier assistant option. If no editable object was explicitly selected, editScope may be empty and a feasible implementation may choose only water/internal walls as proposal assumptions. Do not claim that the designer named them. Platform protection belongs to server rules, not evidence-bound user preserve items. Separate mentioned entities from requested changes. Return changes as component/property/operation records. operation=change requests an actual change; operation=preserve is a prohibition or lock; operation=mention only references an entity. A request not to move B1 is preserve, not change. Increasing switching between boxes changes gameplay/switching, NOT boxes/count or boxes/position. Interrupting B1's consecutive pushes changes gameplay/push_order, NOT boxes/position. Only explicit moves/additions/removals of an entity change its protected count/position. Bind short answers to the active proposal question before classifying them; an answer to a proposal preference question continues the existing topic and is not a new edit command. Each changes item needs its own exact evidenceSpan from the latest message. For an active topic, proposal button, or concrete revision request, return proposalUnderstanding; otherwise return null. Reconstruct the COMPLETE topic from designerTurns, bound questions/answers and the previous understanding. Keep goals, editScope, focus and preserve separate. Each item has statement, sourceTurnId and exact designer evidenceSpan. editScope also has component; focus also has entities (current labels, or [] for a region). 'latest' identifies the latest designer turn. Select a source ID from designerTurns first, then copy a nonempty contiguous substring of its content verbatim into evidenceSpan. Never paraphrase, join fragments, normalize punctuation or line breaks, or invent a preservation requirement for a server-owned protection. A water/wall layout request around T1/T2 edits water/internal_walls and focuses on targets; it does NOT move targets. Do not replace earlier scope when a short answer supplies a focus. Only a clear designer correction changes earlier goals/scope/locks. Assistant suggestions, numbers in questions, solution witness counts and inferred intent are NOT designer constraints. An aesthetic goal need not change push order or increase transport: aspect=visual; ask about visual_direction, edit_scope or binding only if truly missing. Gameplay goals may use mechanism. Select nextQuestionDimension semantically, not by keyword presence. If a safe plan can be attempted with current goals and editable scope, set sufficient=true and dimension=none; unspecified implementation details/focus can be selected by feasibility. Do not force three questions."}, {'role': 'user', 'content': json.dumps({'latestUserMessage': latest, 'previousUserWording': previous_user_wording, 'forcedProposalButton': bool(forced_proposal), 'currentStageSnapshot': snapshot, 'proposalContext': {key: value for key, value in (proposal_context or {}).items() if key != 'dialogueEvidence'}, 'designerTurns': [{'id': key, 'content': value} for key, value in designer_sources(proposal_context, latest).items()], 'editableComponents': ['water', 'internal_walls'], 'protectedComponents': ['outer_shell', 'player', 'boxes', 'targets']}, ensure_ascii=False)}]
    messages[0]['content'] += ' Historical designerTurns are available even before a proposal topic starts. Cite their real IDs when carrying forward a goal; do not attribute earlier words to latest. Historical evaluations are background, not permission. Keep new changes evidence in the latest message. For ordinary questions/evaluations/ideas without an active topic, proposalUnderstanding must be null.'
    messages[0]['content'] += _dialogue_background_prompt({'dialogueEvidence': (proposal_context or {}).get('dialogueEvidence'), 'stageSnapshot': snapshot})
    if proposal_options.current_set(proposal_context, snapshot):
        messages[0]['content'] += ' Return optionDecision with preferredOptionIds (ranked), rejectedOptionIds, selection (explicit/inferred/unclear/revise), and exact latest-user evidenceSpan. Bind the answer to the actually displayed options and question. explicit requires an unambiguous designer choice; inferred is only a proposal assumption. unclear has no preferred IDs. revise means an actual direction correction and has no preferred IDs. Preserve earlier rejections unless explicitly withdrawn. Never infer consent to hidden option details. A remaining implementation detail is not a reason to keep asking: sufficient=true when a compatible route can be selected. When three questions have already been displayed, rank remaining compatible directions by expressed priorities, current focus and plausible snapshot fit as an inferred planning choice even if an implementation detail remains unspecified; do not invent a designer preference or solvability guarantee. Genuine correction or rejection of every option is still revise.'
    active_human_edit = ((proposal_context or {}).get('activeDisagreement') or {}).get('subject') == 'human_edit'
    messages[0]['content'] += ' Return humanEditEngagement=null unless there is an active human-edit disagreement. Otherwise judge this component once: engage only for explicit willingness to discuss/explain/adjust/re-edit, unchanged for unrelated/negative wording, unclear for genuine ambiguity. Short answers require the exact bound assistant question from discussion background. Engage needs an exact latest-user evidenceSpan. Other decisions may use an empty span. This is never consensus or map permission.'
    last_error = None
    locked_base = None
    repair_request = None
    payload = {}
    review_repair_request = None
    review_cold_retry = False
    verdict = {}
    for attempt in range(1, 3):
        try:
            phase = 'understanding'
            timeout = min(15.0, svc.remaining_until(_deadline)) if _deadline is not None else 15.0
            if timeout <= 0:
                raise asyncio.TimeoutError()
            if review_repair_request is not None or review_cold_retry:
                pass
            elif repair_request is not None:
                merged = asyncio.run(_request_component_patch(api_key, base_url, repair_request, messages, request_id, _deadline or svc.clock.monotonic() + timeout, 'turn_understanding', max_tokens=1800, timeout_limit=timeout))
                payload = merged['understanding']
            else:
                response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, 1800, timeout, task='turn_understanding'), timeout=timeout))
                if not getattr(response, 'choices', None):
                    raise EmptyModelResponse('The model returned no completion choices.')
                if str(getattr(response.choices[0], 'finish_reason', '')) == 'length':
                    raise ValueError('Turn understanding was truncated.')
                payload = parse_json_object(response.choices[0].message.content)
            if set(payload) & {'semanticPolicyVersion', 'criticalReview', 'versionId', 'evidenceSignature'}:
                raise ValueError('A model cannot author server review metadata.')
            candidate = {**locked_base, 'proposalUnderstanding': payload.get('proposalUnderstanding'), 'optionDecision': payload.get('optionDecision')} if locked_base is not None else _validate_turn_understanding(payload, latest, forced_proposal)
            if active_human_edit and (not (locked_base or {}).get('humanEditEngagement')):
                try:
                    candidate['humanEditEngagement'] = validate_engagement(payload.get('humanEditEngagement'), latest)
                except ValueError:
                    candidate.pop('humanEditEngagement', None)
            elif not active_human_edit:
                candidate.pop('humanEditEngagement', None)
            locked_base = {key: value for key, value in candidate.items() if key not in {'proposalUnderstanding', 'optionDecision'}}
            _validate_proposal_interpretation(candidate, latest, snapshot, proposal_context, forced_proposal)
            candidate['optionDecision'] = proposal_options.validate_decision(payload.get('optionDecision'), latest, proposal_context, snapshot)
            review_reason = critical_review_reason(candidate, proposal_context)
            if review_reason:
                phase = 'understanding_review'
                trace_proposal(phase, 'understanding_review', 'started', attempt=attempt)
                verdict = {}
                review_started = svc.clock.monotonic()
                remaining = min(12.0, svc.remaining_until(_deadline)) if _deadline is not None else 12.0
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                review_messages = [{'role': 'system', 'content': "Independently check the candidate understanding against designer evidence and its bound question. Return exactly decision, issues, corrections. APPROVE means every conclusion is reliable: issues=[] and corrections=[], do not copy or rewrite the understanding. CORRECT means you independently verify the FINAL understanding after all necessary corrections: issues=[]; each correction has a server targetId, a specific semantic reason, value containing the native JSON replacement (or list of appended items), and evidenceRefs using supplied user:<sourceTurnId> IDs. Select only supplied targets. Correct only actual defects, never rephrase valid items. Existing goals/preserve items cannot be deleted or reordered; no whole-object or collection replacement. If safe correction is impossible, return REJECT with issues and corrections=[]. Never approve a candidate with unresolved semantic errors. Copy evidenceSpan verbatim as a nonempty contiguous substring of its bound designerTurns content, including punctuation/newlines; A substring may omit punctuation outside its selected boundaries: that is valid, not a reason to rephrase or expand the quote. value must match the target's value type: replace an evidence item with its complete object, never just a quote string. do not add server protections as designer preservation requirements. A region/focus is not an entity move. Preserve earlier edit scope unless the latest designer clearly corrects it. Distinguish mention/preserve/change; protected position/count/layout/shape edits require an actual designer request, not a label or assistant option. Each change needs exact latest-user evidence; topic items need exact evidence and IDs from designerTurns. Check coverage of all active goals and locks, numerical inventions, visual versus gameplay meaning, and short-answer binding. Assistant claims and solver counts cannot create user requirements. Current map facts use only Current Stage Snapshot. Do not judge feasibility here. When proposalContext has optionSet, independently verify optionDecision against the latest answer and the displayed optionQuestion. Preserve previously rejected options unless the user withdraws that rejection. Distinguish explicit choice from inferred planning preference; AI alternatives never establish hidden designer constraints."}, {'role': 'user', 'content': json.dumps({'source': json.loads(messages[1]['content']), 'candidate': candidate, 'reviewReason': review_reason, 'correctionTargets': review_targets(candidate), 'reviewVersion': REVIEW_VERSION}, ensure_ascii=False)}]
                if review_repair_request is not None:
                    verdict = asyncio.run(_request_component_patch(api_key, base_url, review_repair_request, review_messages, request_id, _deadline or svc.clock.monotonic() + remaining, 'turn_understanding_review', max_tokens=2000, timeout_limit=remaining))
                else:
                    reviewed = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, review_messages, 2000, remaining, task='turn_understanding_review'), timeout=remaining))
                    if not getattr(reviewed, 'choices', None):
                        raise EmptyModelResponse('The model returned no review completion choices.')
                    if str(getattr(reviewed.choices[0], 'finish_reason', '')) == 'length':
                        raise ValueError('Turn understanding review was truncated.')
                    verdict = parse_json_object(reviewed.choices[0].message.content)
                review_source = json.loads(review_messages[1]['content'])
                corrected = merge_review(verdict, candidate, review_source['correctionTargets'], designer_sources(proposal_context, latest))
                corrected = _validate_turn_understanding(corrected, latest, forced_proposal)
                _validate_proposal_interpretation(corrected, latest, snapshot, proposal_context, forced_proposal)
                if candidate.get('proposalUnderstanding') and (not corrected.get('proposalUnderstanding')):
                    raise ProposalValidationError([{'fieldPath': 'criticalReview', 'reasonCode': 'REVIEW_TOPIC_REMOVED', 'sourceKind': 'unknown'}])
                corrected['optionDecision'] = proposal_options.validate_decision(corrected.get('optionDecision'), latest, proposal_context, snapshot)
                if any((not x.get('evidenceSpan') for x in corrected.get('changes') or [])):
                    raise ValueError('Reviewed changes lack individual designer evidence.')
                corrected.pop('humanEditEngagement', None)
                if candidate.get('humanEditEngagement'):
                    corrected['humanEditEngagement'] = candidate['humanEditEngagement']
                corrected['criticalReview'] = {'verified': True, 'reason': review_reason, 'policyVersion': MEANING_REVIEW_VERSION, 'corrected': corrected != candidate, 'attempt': attempt, 'latencyMs': int((svc.clock.monotonic() - review_started) * 1000), 'initialChanges': candidate.get('changes') or []}
                changed_targets = [x['targetId'] for x in verdict['corrections']]
                trace_proposal(phase, 'understanding_review', 'merged', reviewVersion=REVIEW_VERSION, reviewDecision=verdict['decision'], correctionCount=len(changed_targets), targetIds=changed_targets, bindingSignature=hashlib.sha256(json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
                candidate = corrected
                if review_repair_request is not None:
                    trace_repair('understanding_review', 'revalidated')
                trace_proposal(phase, 'understanding_review', 'passed', attempt=attempt)
                _log_llm_event('proposal_understanding_reviewed', requestId=request_id, reason=review_reason, corrected=candidate['criticalReview']['corrected'], attempt=attempt)
            candidate['semanticPolicyVersion'] = 1
            candidate['dialogueContextVersion'] = CONTEXT_VERSION
            candidate['evidenceSignature'] = topic_signature(snapshot, proposal_context, latest)
            if proposal_options.current_set(proposal_context, snapshot):
                candidate['optionContextSignature'] = proposal_options.context_signature(proposal_context)
            return candidate
        except LLMServiceError as exception:
            if not exception.retryable or attempt >= 2 or svc.remaining_until(_deadline) <= 1.0:
                if not getattr(exception, 'details', None):
                    exception.details = {'task': 'turn_understanding', 'failureStage': phase, 'reasonCode': exception.code, 'issues': [], 'failureKind': 'upstream', 'maximumAttempts': 2}
                    diagnostic = CURRENT_PROPOSAL_DIAGNOSTIC.get()
                    if diagnostic is not None:
                        exception.details['proposalDiagnostic'] = diagnostic.snapshot()
                raise
            last_error = exception
            repair_request = None
            review_repair_request = None
            review_cold_retry = phase == 'understanding_review'
            continue
        except (TimeoutError, asyncio.TimeoutError, APITimeoutError, RateLimitError, APIConnectionError, APIStatusError) as exception:
            upstream_error = (
                LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not respond before the turn understanding timeout.', request_id, True, attempt, 504)
                if isinstance(exception, (TimeoutError, asyncio.TimeoutError))
                else classify_exception(exception, request_id, attempt)
            )
            last_error = upstream_error
            _log_llm_event('proposal_understanding_rejected', requestId=request_id, attempt=attempt, reason=upstream_error.code, issues=[], issuesTruncated=False, issueCount=0)
            if not upstream_error.retryable or attempt >= 2 or svc.remaining_until(_deadline) <= 1.0:
                upstream_error.details = {
                    'task': 'turn_understanding',
                    'failureStage': phase,
                    'reasonCode': upstream_error.code,
                    'issues': [],
                    'failureKind': 'timeout' if upstream_error.code == 'UPSTREAM_TIMEOUT' else 'upstream',
                    'maximumAttempts': 2,
                }
                diagnostic = CURRENT_PROPOSAL_DIAGNOSTIC.get()
                if diagnostic is not None:
                    upstream_error.details['proposalDiagnostic'] = diagnostic.snapshot()
                raise upstream_error from exception
            repair_request = None
            review_repair_request = None
            review_cold_retry = phase == 'understanding_review'
            continue
        except Exception as exception:
            last_error = exception
            diagnostic = CURRENT_PROPOSAL_DIAGNOSTIC.get()
            if (getattr(exception, 'details', {}) or {}).get('proposalDiagnostic') and diagnostic is not None:
                diagnostic.error(phase, 'understanding', exception)
            else:
                trace_proposal(phase, 'understanding_review' if phase == 'understanding_review' else 'understanding', 'revalidation_failed' if repair_request is not None else 'rejected', reason_code(exception), attempt=attempt, issues=safe_issues(exception), issuesTruncated=getattr(exception, 'issues_truncated', False), issueCount=getattr(exception, 'issue_count', None))
            _log_llm_event('proposal_understanding_rejected', requestId=request_id, attempt=attempt, reason=reason_code(exception), issues=safe_issues(exception), issuesTruncated=getattr(exception, 'issues_truncated', False), issueCount=getattr(exception, 'issue_count', None))
            if reason_code(exception) == 'SEMANTIC_REVIEW_REJECTED':
                trace_proposal(phase, 'understanding_review', 'not_repairable', 'SEMANTIC_REVIEW_REJECTED', attempt=attempt)
                break
            sources = designer_sources(proposal_context, latest)
            if phase == 'understanding_review':
                review_source = json.loads(review_messages[1]['content'])
                review_repair_request = review_patch_request(verdict, exception, review_source['correctionTargets'], sources, review_source)
                review_cold_retry = not verdict
                trace_repair('understanding_review', 'proposed' if attempt < 2 else 'exhausted')
                if verdict and review_repair_request is None:
                    break
                continue
            frozen = {**locked_base, 'proposalUnderstanding': payload.get('proposalUnderstanding'), 'optionDecision': payload.get('optionDecision')} if locked_base else payload
            if any((issue.get('fieldPath') == 'optionDecision' for issue in getattr(exception, 'issues', []))):
                repair_request = PatchRequest('understanding', {'understanding': payload}, [Target('optionDecision', ('understanding', 'optionDecision'), (dict,))], {'snapshot': snapshot, 'proposal_context': proposal_context, **{f'user:{key}': content for key, content in sources.items()}})
            elif locked_base is not None:
                repair_request = understanding_patch_request(frozen, exception, sources, snapshot)
            elif payload:
                repair_request = PatchRequest('understanding', {'understanding': frozen}, [Target('understanding', ('understanding',), (dict,))], {'snapshot': snapshot, **{f'user:{key}': content for key, content in sources.items()}})
            else:
                repair_request = None
            review_repair_request = None
            review_cold_retry = False
            trace_repair('understanding', 'proposed' if attempt < 2 else 'exhausted')
            messages.append({'role': 'user', 'content': ('Repair only the server-listed failed fields. Validated speech acts, changes and latest evidence are locked: ' + json.dumps(locked_base, ensure_ascii=False) + '. ' if locked_base is not None else 'Repair the failed understanding with the same original evidence and topic. ') + json.dumps({'issues': safe_issues(exception), 'reasonCode': reason_code(exception), 'designerTurns': [{'id': key, 'content': value} for key, value in sources.items()], 'sourceIdRule': 'sourceTurnId must be a designerTurns.id (latest or a real Turn ID). evidenceRefs use external user:<id> keys only; never copy these keys into sourceTurnId. Do not invent a source, rewrite a quote or drop a goal/lock to pass validation.'}, ensure_ascii=False)})
    error = LLMServiceError('MODEL_RESPONSE_INVALID', "Kimi could not reliably classify the designer's latest message. Retry it.", request_id, True, attempt, 502)
    error.details = {'task': 'turn_understanding', 'failureStage': phase, 'reasonCode': reason_code(last_error), 'issues': safe_issues(last_error), 'failureKind': 'timeout' if isinstance(last_error, (TimeoutError, asyncio.TimeoutError)) else 'semantic_rejection' if reason_code(last_error) == 'SEMANTIC_REVIEW_REJECTED' else 'review_validation' if phase == 'understanding_review' else 'understanding', 'maximumAttempts': 2}
    diagnostic = CURRENT_PROPOSAL_DIAGNOSTIC.get()
    if diagnostic is not None:
        error.details['proposalDiagnostic'] = diagnostic.snapshot()
    raise error from last_error

def _validate_proposal_interpretation(candidate, latest, snapshot, context, forced):
    topic = candidate.get('proposalUnderstanding')
    required = bool((context or {}).get('topicId') or forced or 'revision_request' in candidate.get('acts', []))
    if not required:
        candidate['proposalUnderstanding'] = None
        return
    if topic is None:
        if required:
            raise ProposalValidationError([{'fieldPath': 'proposalUnderstanding', 'reasonCode': 'TOPIC_REQUIRED', 'sourceKind': 'unknown'}])
        return
    roles = {turn['id']: turn['role'] for turn in ((context or {}).get('dialogueEvidence') or {}).get('turns', [])}
    candidate['proposalUnderstanding'] = validate_topic(topic, designer_sources(context, latest), snapshot, roles)

def _validate_turn_understanding(payload, latest, forced_proposal=False):

    def fail(field, code):
        raise ProposalValidationError([{'fieldPath': field, 'reasonCode': code, 'sourceKind': 'latest_user'}])
    metadata = {'proposalUnderstanding', 'humanEditEngagement', 'optionDecision', 'semanticPolicyVersion', 'evidenceSignature', 'criticalReview', 'versionId'}
    if not isinstance(payload, dict) or set(payload) - metadata not in ({'acts', 'elements', 'evidenceSpan', 'directionSufficient', 'mapRelated'}, {'acts', 'elements', 'evidenceSpan', 'directionSufficient', 'mapRelated', 'changes'}):
        fail('understanding', 'UNDERSTANDING_SCHEMA_INVALID')
    allowed_acts = {'evaluation', 'intent', 'explanation_request', 'idea_request', 'revision_request', 'unclear'}
    allowed_elements = {'water', 'internal_walls', 'outer_shell', 'player', 'boxes', 'targets', 'unknown'}
    acts = payload['acts']
    elements = payload['elements']
    evidence = payload['evidenceSpan']
    if not isinstance(acts, list) or not 1 <= len(acts) <= 3 or any((not isinstance(act, str) or act not in allowed_acts for act in acts)) or (len(acts) != len(set(acts))):
        fail('acts', 'SPEECH_ACT_INVALID')
    if not isinstance(elements, list) or len(elements) > 6 or any((not isinstance(element, str) or element not in allowed_elements for element in elements)) or (len(elements) != len(set(elements))):
        fail('elements', 'ELEMENT_INVALID')
    if not isinstance(evidence, str) or not evidence.strip() or evidence not in latest:
        fail('evidenceSpan', 'EVIDENCE_SPAN_NOT_EXACT')
    if not isinstance(payload['directionSufficient'], bool) or not isinstance(payload['mapRelated'], bool):
        fail('directionSufficient', 'ROUTING_BOOLEAN_INVALID')
    if forced_proposal and 'revision_request' not in acts:
        acts = [*acts[:2], 'revision_request']
    result = {'acts': acts, 'elements': elements, 'evidenceSpan': evidence, 'directionSufficient': payload['directionSufficient'], 'mapRelated': payload['mapRelated']}
    result.update({key: payload[key] for key in metadata if key in payload})
    if 'changes' in payload:
        changes = payload['changes']
        allowed_properties = {'count', 'position', 'shape', 'layout', 'route', 'push_order', 'switching', 'rhythm', 'appearance', 'unknown'}
        if not isinstance(changes, list) or any((not isinstance(x, dict) or set(x) not in ({'component', 'property'}, {'component', 'property', 'operation'}, {'component', 'property', 'operation', 'evidenceSpan'}) or any((not isinstance(x.get(key), str) for key in ('component', 'property'))) or (x.get('operation', 'change') not in {'change', 'preserve', 'mention'}) or (x['component'] not in allowed_elements | {'gameplay'}) or (x['property'] not in allowed_properties) for x in changes)):
            fail('changes', 'CHANGE_ATTRIBUTE_INVALID')
        if any(('evidenceSpan' in x and (not isinstance(x['evidenceSpan'], str) or not x['evidenceSpan'].strip() or x['evidenceSpan'] not in latest) for x in changes)):
            fail('changes', 'EVIDENCE_SPAN_NOT_EXACT')
        result['changes'] = changes
    return result

@proposal_phase('challenge_review', 'challenge_review')
def classify_challenge_reason(user_reason, hypotheses, proposal_summary, request_id, *, _deadline=None, simple=False, comparison_validator=None, stage_snapshot=None):
    """Review semantics and current-map prose in the same two-attempt budget."""
    primary = str((hypotheses or {}).get('primary') or '').strip()
    secondary = str((hypotheses or {}).get('secondary') or '').strip()
    reason = str(user_reason or '').strip()
    if not reason or (not simple and (not primary or not secondary)):
        raise LLMServiceError('MODEL_RESPONSE_INVALID', 'The challenge reason could not be compared with the two saved hypotheses.', request_id, True, 0, 502)
    svc = _current_service()
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    deadline = _deadline or svc.clock.monotonic() + svc.config.llm_internal_deadline_seconds
    timeout_seconds = min(20.0, svc.remaining_until(deadline))
    if timeout_seconds <= 0:
        raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not classify the challenge reason before the request deadline.', request_id, True, 0, 504)
    simple_instruction = f"Review the designer's reason for challenging the original proposal. Return JSON only with relation='different', merit, and comparison. merit is reasonable, not_yet_reasonable, or unclear. comparison must be one or two concise first-person sentences in the designer's language: state whether the reason is persuasive, give the concrete reason, and contrast its priority with the original proposal. Do not mention tentative guesses or claim any map change. Do not invent map facts.\n\nProposal summary: {str(proposal_summary or '')[:500]}\nLatest designer reason: {reason[:1200]}"
    messages = [{'role': 'system', 'content': simple_instruction if simple else f"Classify the designer's latest reason against exactly two earlier tentative guesses. Return JSON only with relation, merit, and comparison. relation is primary, secondary, different, or unclear. merit is reasonable, not_yet_reasonable, or unclear. A materially new concern is different even when it is reasonable. comparison must be a warm first-person design judgment of two to four sentences: explicitly say whether you currently agree with the designer, explain why, contrast what the designer's concern prioritizes with what the original proposal prioritizes, and name the decision at the heart of the disagreement. Write comparison in the same language as the latest designer reason. It must be suitable for the visible Core disagreement section. Do not invent map facts.\n\nProposal summary: {str(proposal_summary or '')[:500]}\nPrimary guess: {primary[:800]}\nSecondary guess: {secondary[:800]}\nLatest designer reason: {reason[:1200]}"}]
    last_exception = None
    if stage_snapshot is not None:
        messages[0]['content'] += '\nOnly this current StageSnapshot is map evidence. The cited proposal describes a proposed change, never a current map fact:\n' + json.dumps(stage_snapshot, ensure_ascii=False)
    attempts_used = 0
    for attempt in range(1, 3):
        remaining = svc.remaining_until(deadline)
        if remaining <= 0:
            break
        attempt_timeout = min(20.0, remaining)
        attempts_used = attempt
        try:
            response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, 640, attempt_timeout, task='challenge_reason_classification'), timeout=attempt_timeout))
            if not getattr(response, 'choices', None):
                raise EmptyModelResponse('The model returned no completion choices.')
            if str(getattr(response.choices[0], 'finish_reason', '') or '') == 'length':
                raise ValueError('Challenge reason classification was truncated.')
            payload = parse_json_object(response.choices[0].message.content)
            relation = payload.get('relation')
            merit = payload.get('merit')
            comparison = _normalize_response_paragraphs(str(payload.get('comparison') or ''))
            if relation not in {'primary', 'secondary', 'different', 'unclear'}:
                raise ValueError('challenge reason relation is invalid')
            if simple and relation != 'different':
                relation = 'different'
            if merit not in {'reasonable', 'not_yet_reasonable', 'unclear'}:
                raise ValueError('challenge reason merit is invalid')
            if relation != 'unclear' and merit != 'unclear' and (len(comparison) < (16 if simple else 40)):
                if attempt < 2 and svc.remaining_until(deadline) > 1.0:
                    messages.append({'role': 'system', 'content': 'Your semantic labels were usable, but comparison was missing or too short. Return the same JSON contract and provide a concrete two-to-four-sentence comparison.'})
                    continue
                raise ValueError('No substantive Kimi comparison was returned.')
            if comparison_validator is not None:
                comparison = comparison_validator(comparison)
                if not comparison:
                    raise ValueError('MAP_GROUNDING_INVALID: no reliable comparison remained.')
            return {'relation': relation, 'merit': merit, 'comparison': comparison[:1200] or None, 'attemptsUsed': attempt}
        except (TimeoutError, asyncio.TimeoutError, APITimeoutError, RateLimitError, APIConnectionError, APIStatusError, LLMServiceError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exception:
            classified = classify_exception(exception, request_id, attempt) if isinstance(exception, (APITimeoutError, RateLimitError, APIConnectionError, APIStatusError)) else exception
            last_exception = classified
            _log_llm_event('llm_component_rejected', requestId=request_id, task='challenge_review', attempt=attempt, component='comparison', failureCode=classified.code if isinstance(classified, LLMServiceError) else ('MAP_GROUNDING_INVALID' if 'MAP_GROUNDING_INVALID' in str(exception) else 'MODEL_RESPONSE_INVALID'))
            if isinstance(classified, LLMServiceError) and not classified.retryable:
                break
            if attempt < 2 and svc.remaining_until(deadline) > 1.0:
                if not isinstance(classified, (TimeoutError, asyncio.TimeoutError, LLMServiceError)):
                    messages.append({'role': 'system', 'content': "Repair the failed comparison against the current snapshot while preserving the designer's reason. Return the complete JSON contract. Issue: " + str(exception)[:400]})
                continue
            break
    if isinstance(last_exception, LLMServiceError):
        last_exception.attempts_used = attempts_used
        raise last_exception
    if isinstance(last_exception, asyncio.TimeoutError) or svc.remaining_until(deadline) <= 0:
        raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not classify the challenge reason before the request deadline.', request_id, True, attempts_used, 504) from last_exception
    error = classify_exception(last_exception or ValueError('empty challenge review'), request_id, attempts_used)
    error.retryable = True
    raise error from last_exception

def review_intent_feedback(candidate, active_inclinations, evidence_summary, language, request_id, *, candidate_claims=None, _deadline=None):
    """Check one user-confirmed inclination without map or chat-history context."""
    candidate = re.sub('\\s+', ' ', str(candidate or '')).strip()[:1200]
    active = []
    for item in active_inclinations or []:
        if not isinstance(item, dict):
            continue
        hypothesis_id = str(item.get('hypothesisId') or '').strip()[:96]
        statement = re.sub('\\s+', ' ', str(item.get('statement') or '')).strip()[:600]
        if hypothesis_id and statement:
            active.append({'hypothesisId': hypothesis_id, 'statement': statement, 'semanticClaims': item.get('semanticClaims') or []})
    active = active[:12]
    evidence = [re.sub('\\s+', ' ', str(item or '')).strip()[:500] for item in evidence_summary or [] if str(item or '').strip()][:8]
    if len(candidate) < 4:
        raise LLMServiceError('INVALID_INTENT_FEEDBACK', 'The revised design inclination is too short to review.', request_id, False, 0, 400)
    svc = _current_service()
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    deadline = _deadline or svc.clock.monotonic() + svc.config.llm_internal_deadline_seconds
    response_language = 'Simplified Chinese' if language == 'zh-CN' else 'English'
    messages = [{'role': 'system', 'content': f'You review one designer-authored candidate for long-term semantic memory. Return JSON only. verdict is compatible when the candidate is a clear, meaningful design inclination and can coexist with every active inclination. Never infer or authorize replacement of an active inclination. verdict is conflict when it contradicts an active inclination or combines mutually incompatible priorities. verdict is unclear when it is vague, meaningless, merely an action acknowledgement, or not a design inclination. explanation must be null for compatible. For conflict or unclear, explanation must be a concise, warm first-person explanation in {response_language}. For conflict, write two to four detailed sentences that name the candidate direction, name the conflicting prior direction, and explain exactly why they cannot both guide the same design decision. Do not ask the designer to revise, compromise, or accept a recommendation. For unclear, explain the missing meaning and ask for clarification. Do not propose a map edit, invent map facts, mention prompts or internal fields, or claim the candidate was saved. Return the exact IDs of directly contradictory prior inclinations in conflictingHypothesisIds only when verdict is conflict. supersedesHypothesisIds must always be empty; choosing what to retain belongs exclusively to the designer. Evidence entries are observations only and cannot establish intention by themselves.\n\nCandidate inclination: {candidate}\nCandidate server-extracted semantic claims:\n' + json.dumps(candidate_claims or [], ensure_ascii=False, separators=(',', ':')) + '\nActive confirmed inclinations:\n' + json.dumps(active, ensure_ascii=False, separators=(',', ':')) + '\nRelevant audit evidence:\n' + json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}]
    last_error = None
    for attempt in range(1, 3):
        remaining = svc.remaining_until(deadline)
        timeout_seconds = min(45.0 if attempt == 1 else remaining, remaining)
        if timeout_seconds <= 0:
            break
        try:
            response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, INTENT_FEEDBACK_REVIEW_MAX_COMPLETION_TOKENS, timeout_seconds, task='intent_feedback_review'), timeout=timeout_seconds))
            choice = response.choices[0]
            if str(getattr(choice, 'finish_reason', '') or '') == 'length':
                raise ValueError('The intent review reached its output limit.')
            payload = parse_json_object(choice.message.content)
            if set(payload) != {'verdict', 'explanation', 'supersedesHypothesisIds', 'conflictingHypothesisIds'}:
                raise ValueError('Intent review contains unexpected or missing fields.')
            verdict = payload.get('verdict')
            supersedes_ids = payload.get('supersedesHypothesisIds')
            conflicting_ids = payload.get('conflictingHypothesisIds')
            active_ids = {item['hypothesisId'] for item in active}
            if not isinstance(supersedes_ids, list) or any((not isinstance(item, str) for item in supersedes_ids)) or (not set(supersedes_ids).issubset(active_ids)) or (not isinstance(conflicting_ids, list)) or any((not isinstance(item, str) for item in conflicting_ids)) or (not set(conflicting_ids).issubset(active_ids)):
                raise ValueError('Intent review replacement IDs are invalid.')
            supersedes_ids = list(dict.fromkeys(supersedes_ids))
            conflicting_ids = list(dict.fromkeys(conflicting_ids))
            explanation = _normalize_response_paragraphs(str(payload.get('explanation') or ''))[:1200]
            if verdict not in {'compatible', 'conflict', 'unclear'}:
                raise ValueError('Intent review verdict is invalid.')
            if verdict == 'compatible':
                if payload.get('explanation') is not None:
                    raise ValueError('A compatible review must not include an explanation.')
                if conflicting_ids:
                    raise ValueError('A compatible review cannot cite conflicts.')
                explanation = None
            elif len(explanation) < 12:
                raise ValueError('Intent review explanation is too short.')
            elif supersedes_ids:
                raise ValueError('A rejected intent review cannot replace active memory.')
            elif verdict == 'conflict' and (not conflicting_ids):
                raise ValueError('A conflict review must identify prior inclination IDs.')
            elif verdict == 'unclear' and conflicting_ids:
                raise ValueError('An unclear review cannot cite confirmed conflicts.')
            if verdict == 'conflict':
                conflict_sentences = [item for item in re.split('[.!?\\u3002\\uFF01\\uFF1F]+', explanation) if item.strip()]
                forbidden_direction = re.search('(?:please\\s+(?:revise|rewrite|adjust|change)|you\\s+(?:need|have)\\s+to\\s+(?:revise|rewrite|adjust|change)|compromise|\\u8bf7(?:\\u4fee\\u6539|\\u8c03\\u6574|\\u91cd\\u65b0\\u8868\\u8ff0)|\\u9700\\u8981(?:\\u4fee\\u6539|\\u8c03\\u6574|\\u91cd\\u65b0\\u8868\\u8ff0)|\\u6298\\u4e2d|\\u59a5\\u534f)', explanation, flags=re.IGNORECASE)
                if len(conflict_sentences) < 2 or forbidden_direction or re.search('[?\\uFF1F]', explanation):
                    raise ValueError('A conflict explanation must compare both directions without directing the designer.')
            if supersedes_ids:
                raise ValueError('Intent review cannot authorize automatic replacement.')
            return {'verdict': verdict, 'explanation': explanation, 'supersedesHypothesisIds': [], 'conflictingHypothesisIds': conflicting_ids}
        except asyncio.TimeoutError as exception:
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not review the design inclination before the request deadline.', request_id, True, attempt, 504)
        except LLMServiceError:
            raise
        except Exception as exception:
            last_error = classify_exception(exception, request_id, attempt)
            last_error.retryable = True
        if attempt == 1:
            messages.append({'role': 'system', 'content': 'The prior response was invalid. Return exactly verdict, explanation, supersedesHypothesisIds, and conflictingHypothesisIds using the required JSON schema; do not add markdown or other fields.'})
    if last_error is not None:
        raise last_error
    raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not review the design inclination before the request deadline.', request_id, True, 0, 504)

def review_question_answers(questions, user_reply, language, request_id, *, _deadline=None):
    """Semantically decide which visible assistant questions were actually answered."""
    safe_questions = []
    for item in questions or []:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get('questionId') or item.get('id') or '').strip()[:96]
        question = re.sub('\\s+', ' ', str(item.get('question') or '')).strip()[:1200]
        if question_id and question:
            safe_questions.append({'questionId': question_id, 'question': question})
    safe_questions = safe_questions[:24]
    reply = re.sub('\\s+', ' ', str(user_reply or '')).strip()[:2000]
    if not safe_questions or not reply:
        return {'answeredQuestionIds': [], 'results': []}
    svc = _current_service()
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    response_language = 'Simplified Chinese' if language == 'zh-CN' else 'English'
    messages = [{'role': 'system', 'content': f"Review whether the designer's latest reply genuinely answers each previously visible assistant question. Return every supplied questionId exactly once. Use answered only when the reply gives a position, choice, concrete information, or explicitly closes that issue. A tangential reply, vague acknowledgement, or merely saying they do not know remains unanswered. Do not infer intent, discuss the map, or add prose. The interface language is {response_language}.\nQuestions:\n" + json.dumps(safe_questions, ensure_ascii=False, separators=(',', ':')) + '\nLatest designer reply:\n' + reply}]
    deadline = _deadline or svc.clock.monotonic() + svc.config.llm_internal_deadline_seconds
    last_error = None
    for attempt in range(1, 3):
        remaining = svc.remaining_until(deadline)
        timeout_seconds = min(35.0, remaining)
        if timeout_seconds <= 0:
            break
        try:
            response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, QUESTION_ANSWER_REVIEW_MAX_COMPLETION_TOKENS, timeout_seconds, task='question_answer_review'), timeout=timeout_seconds))
            payload = parse_json_object(response.choices[0].message.content)
            if set(payload) != {'results'} or not isinstance(payload['results'], list):
                raise ValueError('Question review has an invalid envelope.')
            expected = {item['questionId'] for item in safe_questions}
            results = payload['results']
            if len(results) != len(expected):
                raise ValueError('Question review omitted a question.')
            seen = set()
            for item in results:
                if not isinstance(item, dict) or set(item) != {'questionId', 'status'}:
                    raise ValueError('Question review contains an invalid result.')
                if item['questionId'] not in expected or item['questionId'] in seen:
                    raise ValueError('Question review contains an invalid question ID.')
                if item['status'] not in {'answered', 'unanswered'}:
                    raise ValueError('Question review contains an invalid status.')
                seen.add(item['questionId'])
            return {'results': results, 'answeredQuestionIds': [item['questionId'] for item in results if item['status'] == 'answered']}
        except asyncio.TimeoutError as exception:
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not review question answers in time.', request_id, True, attempt, 504)
        except LLMServiceError:
            raise
        except Exception as exception:
            last_error = classify_exception(exception, request_id, attempt)
            last_error.retryable = True
    if last_error is not None:
        raise last_error
    raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not review question answers in time.', request_id, True, 0, 504)

def rewrite_intent_progress(kind, canonical_text, safe_evidence, language, request_id, *, _deadline=None):
    """Create display copy without changing the canonical memory or inventing evidence."""
    kind = str(kind or '').strip()
    if kind not in {'inclination', 'confirmed_decision', 'manual_edit'}:
        raise ValueError('Unsupported intent progress rewrite kind.')
    canonical = re.sub('\\s+', ' ', str(canonical_text or '')).strip()[:1200]
    evidence = [re.sub('\\s+', ' ', str(item or '')).strip()[:700] for item in safe_evidence or [] if str(item or '').strip()][:12]
    if not canonical:
        raise ValueError('Intent progress rewrite requires canonical text.')
    svc = _current_service()
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'The configured LLM API key is missing.', request_id, False, 0, 503)
    response_language = 'Simplified Chinese' if language == 'zh-CN' else 'English'
    detail_rule = 'For manual_edit, detailedText must be two to five factual sentences and summaryText one or two information-dense sentences. For other kinds detailedText must be null. '
    messages = [{'role': 'system', 'content': 'Rewrite one co-creation progress record for display. Use only the supplied canonical text and evidence. Preserve uncertainty and never add motives, map facts, coordinates, metrics, or causal claims. Do not mention internal fields. ' + detail_rule + f'Write in {response_language}.\nKind: {kind}\nCanonical text: {canonical}' + '\nSafe evidence:\n' + json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}]
    deadline = _deadline or svc.clock.monotonic() + svc.config.llm_internal_deadline_seconds
    model = svc.config.model
    last_error = None
    for attempt in range(1, 3):
        remaining = svc.remaining_until(deadline)
        if remaining <= 0:
            break
        timeout_seconds = min(40.0 if attempt == 1 else remaining, remaining)
        validation_feedback = None
        try:
            response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, model, messages, INTENT_PROGRESS_REWRITE_MAX_COMPLETION_TOKENS, timeout_seconds, task='intent_progress_rewrite'), timeout=timeout_seconds))
            payload = parse_json_object(response.choices[0].message.content)
            if set(payload) != {'detailedText', 'summaryText'}:
                raise ValueError('Progress rewrite has unexpected fields.')
            summary = _normalize_response_paragraphs(str(payload.get('summaryText') or ''))[:1200]
            detail = payload.get('detailedText')
            detail = _normalize_response_paragraphs(str(detail or ''))[:1200] or None
            if len(summary) < 4:
                raise ValueError('Progress rewrite summary is too short.')
            canonical_key = re.sub('\\W+', '', canonical).casefold()
            summary_key = re.sub('\\W+', '', summary).casefold()
            if summary_key == canonical_key:
                raise ValueError('Progress rewrite copied the canonical text unchanged.')
            if kind == 'manual_edit' and (detail is None or len(detail) < 12):
                raise ValueError('Manual-edit detail is too short.')
            if kind != 'manual_edit':
                detail = None
            source_facts = canonical + ' ' + ' '.join(evidence)
            source_tokens = {token.casefold() for token in re.findall('(?<![A-Za-z0-9_])(?:B\\d+|T\\d+|P|\\d+(?:\\.\\d+)?)(?![A-Za-z0-9_])', source_facts)}
            output_tokens = {token.casefold() for token in re.findall('(?<![A-Za-z0-9_])(?:B\\d+|T\\d+|P|\\d+(?:\\.\\d+)?)(?![A-Za-z0-9_])', (detail or '') + ' ' + summary)}
            if not output_tokens.issubset(source_tokens):
                raise ValueError('Progress rewrite introduced an unsupported map fact or number.')
            return {'detailedText': detail, 'summaryText': summary, 'model': model}
        except asyncio.TimeoutError:
            last_error = LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not rewrite progress before the deadline.', request_id, True, attempt, 504)
        except LLMServiceError as exception:
            last_error = exception
        except Exception as exception:
            validation_feedback = str(exception)[:500]
            last_error = classify_exception(exception, request_id, attempt)
            last_error.retryable = True
        if attempt == 1 and last_error.retryable:
            feedback = validation_feedback or str(last_error)
            messages.append({'role': 'system', 'content': 'The prior rewrite was rejected: ' + feedback + ' Return fresh JSON with a genuinely concise summaryText that does not copy the canonical text. For inclination and confirmed_decision, set detailedText to null. Do not mention this correction.'})
            continue
        raise last_error
    if last_error is not None:
        raise last_error
    raise LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not rewrite progress before the deadline.', request_id, True, 0, 504)

async def _review_intent_candidate_async(*, api_key, base_url, user_text, body, decision, active_inclinations, language, request_id, deadline, discussion_background='', visible_questions=None, component_state=None, locked_review=None):
    """One independent call; intent, presentation and question results validate separately."""
    svc = _current_service()
    safe_questions = _ordinary_review_questions(visible_questions)
    if locked_review is not None:
        timeout = min(30.0, svc.remaining_until(deadline))
        if timeout <= 0:
            raise asyncio.TimeoutError()
        schema = {'type': 'json_schema', 'json_schema': {'name': 'cocreation_intent_body_review', 'strict': True, 'schema': {'type': 'object', 'additionalProperties': False, 'properties': {'bodyValid': {'type': 'boolean'}, 'issues': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['bodyValid', 'issues']}}}
        messages = [{'role': 'system', 'content': 'Independently review ONLY the changed BODY against exact user evidence and LOCKED CLAIMS. Return JSON bodyValid and concise issues. Reject irrelevant prose, invented user purposes, claim contradictions or unfinished prose; length, sentence count and stylistic depth are advisory. Reject design suggestions to relocate/reconfigure boxes, targets, player start or outer shell; only water/internal walls may be edited. Normal pushing during play is not a design edit. Map facts are checked by the server. Do not generate or reclassify intent, a card, question progress, permission or state. The locked semantic result remains authoritative.'}, {'role': 'user', 'content': json.dumps({'userEvidence': user_text, 'body': body, 'lockedClaims': locked_review['claims']}, ensure_ascii=False)}]
        trace_reply('body_review', 'started', modelCalls=1)
        response = await asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, 900, timeout, task='intent_candidate_review', response_format_override=schema), timeout=timeout)
        choice = response.choices[0]
        if str(getattr(choice, 'finish_reason', '')) == 'length':
            raise ValueError('Body review was truncated.')
        verdict = parse_json_object(choice.message.content)
        if set(verdict) != {'bodyValid', 'issues'} or type(verdict['bodyValid']) is not bool or (not isinstance(verdict['issues'], list)) or any((not isinstance(x, str) for x in verdict['issues'])):
            raise ValueError('Body review has invalid fields.')
        result = {**locked_review, 'bodyValid': verdict['bodyValid'], 'issues': verdict['issues'][:4]}
        trace_reply('body_review', 'passed' if result['bodyValid'] else 'rejected', '' if result['bodyValid'] else 'BODY_SEMANTIC_INVALID')
        return result
    active = []
    for item in active_inclinations or []:
        if not isinstance(item, dict):
            continue
        hypothesis_id = str(item.get('hypothesisId') or item.get('id') or '').strip()[:96]
        statement = re.sub('\\s+', ' ', str(item.get('statement') or '')).strip()[:800]
        if hypothesis_id and statement:
            active.append({'hypothesisId': hypothesis_id, 'statement': statement, 'evidenceText': str(item.get('evidenceText') or '')[:800], 'evidenceTurnId': item.get('evidenceTurnId'), 'semanticClaims': item.get('semanticClaims') or []})
    response_language = 'Simplified Chinese' if language == 'zh-CN' else 'English'
    messages = [{'role': 'system', 'content': f"You are the independent intent-candidate reviewer for a Sokoban co-design chat. Return JSON only. Judge the CURRENT USER WORDS, not keywords. classification=candidate only when this turn expresses or materially revises the designer's own design direction; reference_only is an acknowledgement or reference such as 可以, this is okay, or proceed with the prior proposal; none is a question, neutral observation, or unrelated message. For candidate, return one to four complete claims grounded in exact contiguous evidenceSpan quotes from the current user text. Preserve object, attribute, direction, degree, aspect, and scope. subjectType is layout, space, route, target, water, wall, box, difficulty, rhythm, or other; attributeType is amount, coverage, density, complexity, length, clarity, concentration, separation, or other. degree is excessive, insufficient, neutral, or unspecified; aspect is visual, gameplay, or unspecified; scopeType is stage, region, entity, or other. Use other plus natural-language Text fields when needed. Do not invent a gameplay purpose. For candidate return cardText: concise complete, tentative, correctable sentences that preserve every claim without restating the draft body. For non-candidate return empty cardText and no claims. cardTextValid means the draft card covers every claim, stays tentative/correctable, and adds no purpose. For none or reference_only, bodyValid checks that the prose directly answers the user and has no unauthorized design advice relocating/adding/removing boxes, targets, player start or shell (only water/internal walls are editable; normal gameplay pushes are different), or dangling introduction, option-list fragment, workflow talk, or unfinished paragraph; do not require Stage evidence, two effects, or an uncertainty boundary. Only for candidate, bodyValid also checks semantic agreement with the evidence-bound claims. Stage facts are verified by the server. Sentence counts, depth, two effects, uncertainty phrasing and repetition are quality issues only: report them in issues without setting bodyValid=false. A short complete answer needs no extra analysis, route passage, personal reflection, humor or summary. relation is none without a candidate or active inclination, otherwise compatible, conflict, or unclear. conflict must cite exact active IDs and explain directly in {response_language} why the new and old directions cannot both guide the same decision; never choose, recommend compromise, or demand revision. reviewedActiveClaims may provide claims only for legacy active inclinations whose evidenceText supports them unambiguously; otherwise omit. issues must precisely tell the drafting model what to correct, without hidden reasoning.\n\nCURRENT USER TEXT:\n" + str(user_text or '')[:2000] + '\n\nDRAFT BODY:\n' + str(body or '')[:5000] + '\n\nDRAFT INTENT DECISION:\n' + json.dumps(decision, ensure_ascii=False, separators=(',', ':')) + '\n\nACTIVE CONFIRMED INTENTIONS:\n' + json.dumps(active[:12], ensure_ascii=False, separators=(',', ':'))}]
    messages[0]['content'] += discussion_background
    messages[0]['content'] += '\nAlso independently judge which OPEN QUESTIONS the current user actually answers. Return answeredQuestionIds only from the supplied IDs, once each; use [] when none. A concrete position, choice, information or explicit closure answers a question; vague acknowledgement or not knowing does not. An answer can ALSO express a candidate inclination. Question progress never confirms intent, clears disagreement or authorizes an edit.\nOPEN QUESTIONS:\n' + json.dumps(safe_questions, ensure_ascii=False, separators=(',', ':'))
    trace_reply('joint_review', 'started', modelCalls=1, questionCount=len(safe_questions))
    remaining = svc.remaining_until(deadline)
    if remaining <= 0:
        raise asyncio.TimeoutError()
    response = await asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, INTENT_CANDIDATE_REVIEW_MAX_COMPLETION_TOKENS, min(30.0, remaining), task='intent_candidate_review'), timeout=min(30.0, remaining))
    choice = response.choices[0]
    if str(getattr(choice, 'finish_reason', '') or '') == 'length':
        raise ValueError('The intent candidate review reached its output limit.')
    payload = parse_json_object(choice.message.content)
    trace_reply('joint_review', 'passed')
    try:
        question_review = _validate_joint_question_answers(payload.get('answeredQuestionIds', [] if not safe_questions else None), safe_questions)
        if component_state is not None:
            component_state.setdefault('questionAnswerReview', question_review)
            component_state.pop('questionReviewIssue', None)
        trace_reply('question_review', 'passed', questionCount=len(safe_questions), answeredCount=len(question_review['answeredQuestionIds']), questionIds=question_review['answeredQuestionIds'])
    except (ValueError, TypeError):
        if component_state is not None:
            component_state['questionReviewIssue'] = 'QUESTION_RESULTS_INVALID'
        trace_reply('question_review', 'omitted', 'QUESTION_RESULTS_INVALID')
    trace_reply('intent_semantics', 'started')
    try:
        reviewed = _validate_intent_review_payload({k: v for k, v in payload.items() if k != 'answeredQuestionIds'}, user_text, active, language)
    except (ValueError, TypeError, KeyError) as error:
        trace_reply('intent_semantics', 'omitted', failure_reason(error))
        raise
    trace_reply('intent_semantics', 'passed', classification=reviewed['classification'], relation=reviewed['relation'])
    if component_state is not None:
        component_state['intentReviewAudit'] = copy.deepcopy(reviewed)
    return reviewed

def _ordinary_review_questions(questions):
    result = []
    for item in questions or []:
        if not isinstance(item, dict):
            continue
        identity = str(item.get('questionId') or item.get('id') or '').strip()[:96]
        text = str(item.get('question') or '').strip()[:1200]
        if identity and text and (not any((q['questionId'] == identity for q in result))):
            result.append({'questionId': identity, 'question': text})
    return result[:24]

def _validate_joint_question_answers(identities, questions):
    expected = {item['questionId'] for item in questions}
    if not isinstance(identities, list) or any((not isinstance(x, str) for x in identities)) or len(identities) != len(set(identities)) or (not set(identities).issubset(expected)):
        raise ValueError('Question results must use unique supplied IDs.')
    return {'answeredQuestionIds': list(identities), 'results': [{'questionId': q['questionId'], 'status': 'answered' if q['questionId'] in identities else 'unanswered'} for q in questions]}

def _validate_intent_review_payload(payload, user_text, active, language):
    expected = {'classification', 'claims', 'cardText', 'cardTextValid', 'bodyValid', 'relation', 'conflictingHypothesisIds', 'explanation', 'issues', 'reviewedActiveClaims'}
    semantic_fields = {'classification', 'claims', 'relation', 'conflictingHypothesisIds'}
    if not isinstance(payload, dict) or set(payload) - expected or (not semantic_fields.issubset(payload)):
        raise ValueError('Intent candidate review contains invalid semantic fields.')
    classification = payload.get('classification')
    if classification not in {'none', 'candidate', 'reference_only'}:
        raise ValueError('Intent candidate review classification is invalid.')
    claims = payload.get('claims')
    if not isinstance(claims, list) or len(claims) > 4:
        raise ValueError('Intent candidate review claims are invalid.')
    claims = [_normalize_reviewed_claim(item, user_text) for item in claims]
    card_text = _normalize_response_paragraphs(payload.get('cardText') or '')[:1000] if isinstance(payload.get('cardText'), str) else ''
    if classification == 'candidate' and (not claims):
        raise ValueError('A reviewed candidate must contain claims.')
    if classification != 'candidate' and claims:
        raise ValueError('A reviewed non-candidate cannot contain claims.')
    if classification == 'candidate':
        if not isinstance(payload.get('cardText'), str):
            card_text = ''
    elif card_text:
        raise ValueError('A reviewed non-candidate must have empty cardText.')
    active_ids = {item['hypothesisId'] for item in active}
    conflict_ids = payload.get('conflictingHypothesisIds')
    if not isinstance(conflict_ids, list) or not set(conflict_ids).issubset(active_ids):
        raise ValueError('Intent candidate review conflict IDs are invalid.')
    relation = payload.get('relation')
    if relation not in {'none', 'compatible', 'conflict', 'unclear'}:
        raise ValueError('Intent candidate review relation is invalid.')
    if relation == 'conflict' and (not conflict_ids):
        raise ValueError('A conflict review must bind a confirmed inclination.')
    if relation != 'conflict' and conflict_ids:
        raise ValueError('Only a conflict review may cite conflicting IDs.')
    if classification != 'candidate' and relation != 'none':
        raise ValueError('A reviewed non-candidate must use relation none.')
    if classification == 'candidate' and active and (relation == 'none'):
        raise ValueError('A reviewed candidate with confirmed memory requires a relation.')
    if classification == 'candidate' and (not active) and (relation != 'none'):
        raise ValueError('A reviewed candidate without confirmed memory must use relation none.')
    explanation = _normalize_response_paragraphs(str(payload.get('explanation') or ''))[:1200] or None
    if relation == 'conflict':
        forbidden_direction = re.search('(?:please\\s+(?:revise|rewrite|adjust|change)|compromise|请(?:修改|调整|重新表述)|需要(?:修改|调整|重新表述)|折中|妥协)', explanation or '', flags=re.IGNORECASE)
        minimum_length = 36 if language == 'zh-CN' else 80
        if len(explanation or '') < minimum_length or forbidden_direction or re.search('[?？]', explanation or ''):
            raise ValueError('A conflict explanation must substantively compare both directions without directing the designer.')
    elif relation != 'conflict':
        explanation = None
    legacy_updates = []
    for update in payload.get('reviewedActiveClaims') or []:
        if not isinstance(update, dict) or set(update) != {'hypothesisId', 'claims'}:
            raise ValueError('A legacy intent review update is malformed.')
        if update['hypothesisId'] not in active_ids:
            raise ValueError('A legacy intent review update cites an unknown ID.')
        evidence = next((item['evidenceText'] for item in active if item['hypothesisId'] == update['hypothesisId']), '')
        source_user_turn_id = next((item.get('evidenceTurnId') for item in active if item['hypothesisId'] == update['hypothesisId']), None)
        normalized_update_claims = [{**_normalize_reviewed_claim(item, evidence), 'sourceUserTurnId': source_user_turn_id} for item in update.get('claims') or []]
        legacy_updates.append({'hypothesisId': update['hypothesisId'], 'claims': normalized_update_claims})
    return {'classification': classification, 'claims': claims, 'cardText': card_text, 'cardTextValid': payload.get('cardTextValid') is True, 'bodyValid': payload.get('bodyValid') is True, 'relation': relation, 'conflictingHypothesisIds': list(dict.fromkeys(conflict_ids)), 'explanation': explanation, 'issues': [str(item).strip()[:400] for item in payload.get('issues') or [] if str(item).strip()][:8], 'reviewedActiveClaims': legacy_updates, 'reviewVersion': INTENT_REVIEW_VERSION}

async def _repair_intent_components_async(*, api_key, base_url, user_text, body, card_text, claims, issues, repair_body, repair_card, map_facts, language, request_id, deadline, discussion_background=''):
    """Repair only rejected intent presentation components under locked semantics."""
    svc = _current_service()
    if not repair_body and (not repair_card):
        return (body, card_text)
    if repair_body and (not repair_card):
        timeout = min(30.0, svc.remaining_until(deadline))
        if timeout <= 0:
            raise asyncio.TimeoutError()
        messages = [{'role': 'system', 'content': 'Repair ONLY the rejected body, preserving locked claims and exact user evidence. Only water/internal walls may be edited; never propose rearranging boxes/targets/player/shell. Normal Sokoban pushes during play may be discussed. Return JSON with only bodyParagraphs, a non-empty array of complete paragraphs. Fix the specified relevance or semantic defect; add no user purpose, authorization, state success or map changes. Use the supplied Stage facts only. Keep concise natural prose without a fixed sentence count. Write in ' + ('Simplified Chinese.' if language == 'zh-CN' else 'English.') + '\n\n' + _visible_reply_style('repair') + '\nLOCKED CLAIMS:\n' + json.dumps(claims, ensure_ascii=False) + '\n\nAUTHORITATIVE STAGE FACTS:\n' + str(map_facts or '')[:8000] + '\n\nCURRENT BODY:\n' + str(body or '')[:5000] + '\n\nCURRENT CARD:\n' + str(card_text or '')[:1200] + '\n\nREVIEW ISSUES:\n' + json.dumps(issues or [], ensure_ascii=False) + '\nREPAIR BODY: yes\nREPAIR CARD: no' + discussion_background}, {'role': 'user', 'content': str(user_text or '')[:2000]}]
        response = await asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, INTENT_COMPONENT_REPAIR_MAX_COMPLETION_TOKENS, timeout, task='intent_body_repair'), timeout=timeout)
        choice = response.choices[0]
        if str(getattr(choice, 'finish_reason', '')) == 'length':
            raise ValueError('Intent body repair was truncated.')
        payload = parse_json_object(choice.message.content)
        paragraphs = payload.get('bodyParagraphs')
        if set(payload) != {'bodyParagraphs'} or not isinstance(paragraphs, list) or (not 1 <= len(paragraphs) <= 3) or any((not isinstance(part, str) or not part.strip() for part in paragraphs)):
            raise ValueError('Intent body repair has invalid paragraph fields.')
        repaired = '\n\n'.join((_normalize_response_paragraphs(part) for part in paragraphs))
        _assert_visible_output_language(language, repaired)
        return (repaired, card_text)
    if repair_card and (not repair_body):
        timeout = min(30.0, svc.remaining_until(deadline))
        if timeout <= 0:
            raise asyncio.TimeoutError()
        messages = [{'role': 'system', 'content': 'Repair only the tentative intent card. Return JSON with only cardText. Keep every locked claim and exact user evidence, remain tentative and explicitly correctable; add no purpose, certainty, permission or state conclusion. Use Simplified Chinese.' if language == 'zh-CN' else 'Repair only the tentative intent card. Return JSON with only cardText. Keep every locked claim and exact user evidence, remain tentative and explicitly correctable; add no purpose, certainty, permission or state conclusion. Use English.'}, {'role': 'user', 'content': json.dumps({'userEvidence': user_text, 'lockedClaims': claims, 'currentCard': card_text, 'stageFacts': map_facts, 'issues': issues, 'discussionBackground': discussion_background}, ensure_ascii=False)}]
        response = await asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, messages, INTENT_COMPONENT_REPAIR_MAX_COMPLETION_TOKENS, timeout, task='intent_card_repair'), timeout=timeout)
        choice = response.choices[0]
        if str(getattr(choice, 'finish_reason', '')) == 'length':
            raise ValueError('Intent card repair was truncated.')
        payload = parse_json_object(choice.message.content)
        if set(payload) != {'cardText'} or not isinstance(payload['cardText'], str):
            raise ValueError('Intent card repair has an invalid envelope.')
        repaired = _normalize_response_paragraphs(payload['cardText'])
        issue = _intent_hypothesis_detail_issue(repaired, language, semantic_reviewed=True)
        if issue:
            raise ValueError(issue)
        _assert_visible_output_language(language, repaired)
        return (body, repaired)
    raise ValueError('Repair one rejected presentation component at a time.')

async def _review_ordinary_intent_with_recovery(*, optional_budget, component_checks, **kwargs):
    svc = _current_service()
    state = kwargs.get('component_state')
    if state is None:
        state = {}
    locked = state.get('lockedIntentReview')
    optional = not bool(locked and state.get('bodyReviewRequired'))
    if not optional:
        kwargs['locked_review'] = locked
    started = svc.clock.monotonic()
    try:
        if optional:
            timeout = optional_budget.timeout()
            if timeout <= 0:
                raise asyncio.TimeoutError()
            kwargs['deadline'] = min(kwargs['deadline'], started + timeout)
        result = await svc.review_intent_candidate_async(**kwargs)
        if not result['bodyValid'] and (result['classification'] == 'candidate' or result.get('issues')):
            state['lockedIntentReview'] = copy.deepcopy(result)
            state['bodyReviewRequired'] = True
        if not optional and (not result['bodyValid']):
            raise LowQualityModelResponse('The changed body still contradicts the locked designer evidence.')
        if result['classification'] != 'candidate' and (not result['bodyValid']):
            if result.get('issues'):
                raise LowQualityModelResponse('The ordinary body did not answer the designer reliably: ' + str(result['issues'])[:500])
            component_checks.append(ComponentCheck('body', 'preserved', 'INTENT_SCORE_NOT_APPLICABLE'))
        return result
    except (LLMServiceError, APIConnectionError, APIStatusError, asyncio.TimeoutError, ValueError, TypeError, KeyError, IndexError) as error:
        body = kwargs['body']
        if not optional or isinstance(error, LowQualityModelResponse) or (not body.strip()):
            raise
        trace = CURRENT_REPLY_TRACE.get()
        parsed = bool(trace and any((step['stage'] == 'joint_review' and step['status'] == 'passed' for step in trace.steps)))
        if not parsed:
            trace_reply('joint_review', 'omitted', 'OPTIONAL_BUDGET_EXHAUSTED' if optional_budget.timeout() <= 0 else failure_reason(error), optionalSpentSeconds=round(optional_budget.spent, 3))
        component_checks.append(ComponentCheck('intent_discovery', 'omitted', 'OPTIONAL_REVIEW_FAILED'))
        return {'classification': 'none', 'claims': [], 'cardText': '', 'cardTextValid': True, 'bodyValid': True, 'relation': 'none', 'conflictingHypothesisIds': [], 'explanation': None, 'issues': [], 'reviewedActiveClaims': [], 'reviewVersion': INTENT_REVIEW_VERSION, 'omitted': True}
    finally:
        if optional:
            optional_budget.record(started)

def _engagement_source(latest_user, stage_context):
    return {'latestUserMessage': latest_user, 'activeDisagreement': (stage_context or {}).get('activeDisagreement'), 'discussionBackground': _dialogue_background_prompt(stage_context), 'currentStageSnapshot': (stage_context or {}).get('stageSnapshot')}

async def _request_engagement_decision(source, api_key, base_url, timeout, task):
    svc = _current_service()
    response = await asyncio.wait_for(_request_completion(api_key, base_url, svc.config.model, [{'role': 'system', 'content': 'Judge only human-edit discussion engagement. Return JSON decision engage/unchanged/unclear and evidenceSpan. Engage means the source designer explicitly agrees to discuss, explain, adjust or re-edit, not consensus, map acceptance or permission. Bind short answers to the original assistant question, whose suggestions are not designer consent. Use unchanged for unrelated/negative wording, unclear for genuine ambiguity. Engage needs an exact substring of latestUserMessage/originalUserMessage. Empty evidence is allowed otherwise. In delayed replay also check every subsequent USER message: a withdrawal, correction or replacement invalidates earlier engagement. Only original source consent may engage, not later consent. Map evidence uses only currentStageSnapshot. Never repair ambiguity into agreement.'}, {'role': 'user', 'content': json.dumps(source, ensure_ascii=False)}], 250, timeout, task=task), timeout=timeout)
    if str(getattr(response.choices[0], 'finish_reason', '')) == 'length':
        raise ValueError('Engagement decision was truncated.')
    return validate_engagement(parse_json_object(response.choices[0].message.content), source.get('latestUserMessage', source.get('originalUserMessage', '')))

def recover_human_edit_engagement(source, request_id, deadline):
    svc = _current_service()
    api_key, base_url = svc.credentials()
    if not api_key or api_key in {'your_kimi_api_key_here', 'your_llm_api_key_here'}:
        raise LLMServiceError('CONFIGURATION_ERROR', 'LLM is unavailable.', request_id, True, 0, 503)
    timeout = min(8.0, max(0.0, deadline - svc.clock.monotonic() - 2.0))
    if timeout <= 0:
        raise TimeoutError('Recovery deadline exceeded.')
    try:
        return asyncio.run(_request_engagement_decision(source, api_key, base_url, timeout, 'human_edit_engagement_recovery'))
    except (APIConnectionError, APIStatusError) as error:
        raise LLMServiceError('UPSTREAM_ERROR', 'State review is unavailable.', request_id, True, 1, 502) from error

async def _repair_human_edit_engagement(content, latest_user, stage_context, disagreement, optional_budget, component_checks, api_key, base_url, request_id):
    """Repair only one state signal, with the same bound conversational evidence."""
    svc = _current_service()
    started = svc.clock.monotonic()
    active = {**((stage_context or {}).get('activeDisagreement') or {}), 'displayCard': False}
    try:
        timeout = optional_budget.timeout()
        if timeout <= 0:
            raise asyncio.TimeoutError()
        repaired = await _request_engagement_decision(_engagement_source(latest_user, stage_context), api_key, base_url, timeout, 'human_edit_engagement_repair')
        candidate = {**active, 'status': 'acknowledged' if repaired['decision'] == 'engage' else 'active', 'resolution': None}
        component_checks.append(ComponentCheck('human_edit_engagement', 'repaired'))
        return (candidate, repaired, False)
    except (ValueError, TypeError, KeyError, IndexError, LLMServiceError, APIConnectionError, APIStatusError, asyncio.TimeoutError):
        component_checks.append(ComponentCheck('human_edit_engagement', 'preserved', 'STATE_UNVERIFIED'))
        return (active, None, True)
    finally:
        optional_budget.record(started)

def _strip_unverified_state_claims(body):
    """Drop asserted state success when the corresponding transition failed."""
    return ' '.join((sentence for sentence in _content_block_sentences(body) if not re.search('(?:分歧|冲突).{0,10}(?:已经|已|现已)(?:解除|解决|结束)|(?:已经|已)(?:确认|接受|选定|撤回).{0,15}(?:方案|立场|倾向)|(?:disagreement|conflict) (?:is|has been) (?:resolved|cleared)|(?:I have|we have|you have) (?:accepted|confirmed|chosen)|(?:现在|已经|现已|已).{0,8}(?:可以|能够|允许|解锁).{0,10}(?:执行|应用|批准).{0,8}(?:方案|修改)|\\b(?:now|already)\\b.{0,15}\\b(?:execute|apply|approved|unlocked)\\b', sentence, re.I)))

async def _repair_reviewed_intent_presentation(*, repair_body, repair_card, body, card_text, deadline, optional_budget, card_required, component_checks, rows=None, entity_bindings=None, **kwargs):
    svc = _current_service()
    if repair_body:
        body, _ = await svc.repair_intent_components_async(body=body, card_text=card_text, repair_body=True, repair_card=False, deadline=deadline, **kwargs)
        component_checks.append(ComponentCheck('body', 'repaired', repairable=True))
    if not repair_card:
        return (body, card_text)
    timeout = svc.remaining_until(deadline) if card_required else optional_budget.timeout()
    started = svc.clock.monotonic()
    trace_reply('card_repair', 'started', repairAttempts=int(timeout > 0), optionalRemainingSeconds=round(timeout, 3))
    try:
        if timeout <= 0:
            raise asyncio.TimeoutError()
        _, card_text = await svc.repair_intent_components_async(body=body, card_text=card_text, repair_body=False, repair_card=True, deadline=min(deadline, started + timeout), **kwargs)
        if rows:
            _validate_map_grounding_texts([card_text], rows, entity_bindings=entity_bindings)
        component_checks.append(ComponentCheck('intent_card', 'repaired', repairable=True))
        trace_reply('card_repair', 'passed', cardPresent=True)
        return (body, card_text)
    except (ValueError, TypeError, KeyError, IndexError, LLMServiceError, APIConnectionError, APIStatusError, asyncio.TimeoutError) as error:
        if card_required:
            raise
        component_checks.append(ComponentCheck('intent_card', 'omitted', 'OPTIONAL_PRESENTATION_FAILED'))
        trace_reply('card_repair', 'omitted', 'OPTIONAL_BUDGET_EXHAUSTED' if timeout <= 0 else failure_reason(error), repairAttempts=int(timeout > 0), optionalSpentSeconds=round(optional_budget.spent, 3))
        return (body, '')
    finally:
        if not card_required:
            optional_budget.record(started)

def _components_quality_passed(checks):
    advisory = {'INTENT_SCORE_NOT_APPLICABLE', 'PRESENTATION_QUALITY', 'QUALITY_REVIEW_ISSUES'}
    return all((check.status in {'passed', 'repaired'} or (check.status == 'preserved' and check.code in advisory) for check in checks))

def _extract_plain_intent_decision(content):
    marker = '<GUIDANCE>'
    marker_index = str(content or '').find(marker)
    if marker_index < 0:
        return None
    tail = str(content)[marker_index + len(marker):]
    closing_index = tail.find('</GUIDANCE>')
    if closing_index < 0:
        return None
    block = tail[:closing_index]
    match = re.search('(?:^|\\|\\|)\\s*INTENT_DECISION\\s*:\\s*(\\{.*?\\})\\s*(?=\\|\\||$)', block, re.DOTALL)
    if not match:
        return None
    return _validate_intent_decision(json.loads(match.group(1)), None)

def _normalize_reviewed_claim(raw_claim, user_text, *, require_evidence=True):
    if not isinstance(raw_claim, dict):
        raise ValueError('An intent claim must be an object.')
    required = {'normalizedMeaning', 'subjectType', 'subjectText', 'attributeType', 'attributeText', 'direction', 'degree', 'aspect', 'scopeType', 'scopeText', 'evidenceSpan'}
    if set(raw_claim) != required:
        raise ValueError('An intent claim contains unexpected or missing fields.')
    clean = {key: re.sub('\\s+', ' ', str(raw_claim.get(key) or '')).strip() for key in required}
    if not clean['normalizedMeaning'] or not clean['subjectText'] or (not clean['attributeText']):
        raise ValueError('An intent claim is missing its natural-language meaning.')
    if clean['direction'] not in {'increase', 'decrease', 'maintain', 'avoid', 'evaluate', 'unspecified'}:
        raise ValueError('An intent claim direction is invalid.')
    if clean['subjectType'] not in {'layout', 'space', 'route', 'target', 'water', 'wall', 'box', 'difficulty', 'rhythm', 'other'}:
        raise ValueError('subjectType must use a supported label or other.')
    if clean['attributeType'] not in {'amount', 'coverage', 'density', 'complexity', 'length', 'clarity', 'concentration', 'separation', 'other'}:
        raise ValueError('attributeType must use a supported label or other.')
    if clean['degree'] not in {'excessive', 'insufficient', 'neutral', 'unspecified'}:
        raise ValueError('An intent claim degree is invalid.')
    if clean['aspect'] not in {'visual', 'gameplay', 'unspecified'}:
        raise ValueError('An intent claim aspect is invalid.')
    if clean['scopeType'] not in {'stage', 'region', 'entity', 'other'}:
        raise ValueError('An intent claim scopeType is invalid.')
    if require_evidence and (not clean['evidenceSpan'] or clean['evidenceSpan'] not in str(user_text or '')):
        raise ValueError('Intent evidenceSpan must be an exact contiguous current-user quote.')
    return clean

def _validate_intent_decision(payload, user_text):
    if not isinstance(payload, dict) or set(payload) != {'classification', 'claims', 'cardText'}:
        raise ValueError('intentDecision contains unexpected or missing fields.')
    classification = payload.get('classification')
    if classification not in {'none', 'candidate', 'reference_only'}:
        raise ValueError('intentDecision classification is invalid.')
    claims = payload.get('claims')
    if not isinstance(claims, list) or len(claims) > 4:
        raise ValueError('intentDecision claims must contain at most four items.')
    normalized = [_normalize_reviewed_claim(item, user_text, require_evidence=user_text is not None) for item in claims]
    card_text = _normalize_response_paragraphs(str(payload.get('cardText') or ''))[:1000]
    if classification == 'candidate':
        if not normalized or not card_text:
            raise ValueError('A candidate intentDecision requires claims and cardText.')
    elif normalized or card_text:
        raise ValueError('A non-candidate intentDecision must have empty claims and cardText.')
    return {'classification': classification, 'claims': normalized, 'cardText': card_text}

def _active_human_edit_disagreement(stage_context):
    disagreement = (stage_context or {}).get('activeDisagreement') or {}
    return bool(disagreement.get('status') == 'active' and disagreement.get('subject') == 'human_edit')

def _warning_text_is_evidence_grounded(text, language):
    lowered = str(text or '').casefold()
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', lowered):
        anchors = ('水', '箱', '目标', '墙', '通道', '路线', '入口', '退路', '推动', '绕行')
        risks = ('可能', '担心', '风险', '值得', '我有点在意', '我不太放心', '死锁', '卡住', '误读', '重复', '顺序', '退路')
    else:
        anchors = ('water', 'box', 'crate', 'target', 'wall', 'corridor', 'route', 'entrance', 'escape', 'push', 'player', 'opening', 'position', 'only', 'block')
        risks = ('may', 'might', 'concern', 'risk', 'worth', 'i notice', 'i am uneasy', 'deadlock', 'stuck', 'misread', 'repeat', 'order', 'escape', 'close', 'block', 'trap', 'lock')
    return any((word in lowered for word in anchors)) and any((word in lowered for word in risks))

def _user_explicitly_off_topic(message):
    text = str(message or '').strip().casefold()
    if not text:
        return False
    chinese = ('换个话题', '题外话', '与这个项目无关', '和这个项目无关', '与地图无关', '和地图无关', '先不聊地图', '不谈这个关卡', '聊点别的', '说点别的')
    english = ('change the subject', 'off topic', 'unrelated to this project', 'unrelated to the map', "let's talk about something else", 'stop talking about the map')
    return any((marker in text for marker in (*chinese, *english)))

@dataclass(frozen=True)
class DesignStanceClassification:
    eligible: bool
    language: str
    stance_kind: str = ''
    topics: tuple = ()
    ambiguous_reference: bool = False

@dataclass(frozen=True)
class IntentSemanticFrame:
    """A bounded, user-authored inclination that an orange card may carry."""
    subject: str
    property: str
    direction: str
    degree: str = ''
    specificity: int = 0
    aspect: str = 'unspecified'
    scope: str = 'stage'

def _english_term_present(text, term):
    return re.search(f'(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])', text, re.IGNORECASE) is not None

def _intent_term_present(text, term, language):
    return term in text if language == 'zh-CN' else _english_term_present(text, term)

def _intent_semantic_frames(message):
    """Extract only explicit, current-user preferences safe for an orange card."""
    text = re.sub('\\s+', ' ', str(message or '')).strip()
    language = 'zh-CN' if re.search('[\\u3400-\\u9fff]', text) else 'en'
    lowered = text.casefold().replace('’', "'")
    searchable = text if language == 'zh-CN' else lowered
    if not text:
        return ()
    if language == 'zh-CN':
        if re.search('我(?:觉得|认为|担心|感觉).{0,18}(?:玩家|别人).{0,24}(?:会|可能|觉得|认为)', text):
            return ()
        if re.search('(?:你说|你认为|你觉得).{0,48}我.{0,24}(?:没(?:有)?这个意思|不是这个意思|没说)', text):
            return ()
        if text.startswith('如果') and (not re.search('我(?:希望|想要|不想要|喜欢|不喜欢)', text)):
            return ()
        denied = bool(re.search('(?:不觉得|并不觉得|不认为).{0,24}(?:拥挤|密集|复杂|太难|太长|太短)', text))
        avoid = bool(re.search('(?:不喜欢|不希望|不想(?:要|让)?|不愿意|宁可不)', text))
        prefer = bool(re.search('(?:喜欢|希望|想要|更想要|宁愿|偏向|倾向)', text))
        insufficient = bool(re.search('(?:太少|过少|不够(?:多|大)?|还不够|覆盖(?:太小|过小|不足))', text))
        excessive = bool(re.search('(?:太|过多|占地(?:太大|过大)|覆盖(?:太大|过大)|过于|过分)', text))
        subject_terms = _INTENT_SUBJECT_TERMS_ZH
        property_terms = _INTENT_PROPERTY_TERMS_ZH
    else:
        if re.search('\\bi (?:think|feel|worry|believe).{0,36}\\bplayers?\\b.{0,48}\\b(?:will|may|might|feel|think)\\b', searchable):
            return ()
        if re.search("\\byou (?:said|think|thought|claim).{0,72}\\bi (?:did not|didn't) (?:say|mean)\\b", searchable):
            return ()
        if searchable.startswith('if ') and (not re.search('\\bi (?:want|prefer|like|dislike|hope)\\b', searchable)):
            return ()
        denied = bool(re.search("\\b(?:do not|don't) (?:think|feel) .{0,24}(?:crowded|dense|complex|hard|long|short)\\b", searchable))
        avoid = bool(re.search("\\b(?:do not|don't) (?:like|want|hope|prefer)|\\bdislike\\b", searchable))
        prefer = bool(re.search('\\b(?:like|want|hope|prefer|favor|would rather)\\b', searchable))
        insufficient = bool(re.search('\\b(?:not enough|too (?:few|little|small))\\b', searchable))
        excessive = bool(re.search('\\btoo\\b|\\boverly\\b', searchable))
        subject_terms = _INTENT_SUBJECT_TERMS_EN
        property_terms = _INTENT_PROPERTY_TERMS_EN
    if language == 'zh-CN':
        clauses = [part.strip() for part in re.split('[。！？；;]|(?<=，)(?:但|不过|而不是|同时)', text) if part.strip()]
    else:
        clauses = [part.strip() for part in re.split('[.!?;]|\\b(?:but|rather than|instead)\\b', searchable) if part.strip()]
    frames = []
    for clause in clauses or [searchable]:
        subjects = [key for key, terms in subject_terms if any((_intent_term_present(clause, term, language) for term in terms))]
        properties = [key for key, terms in property_terms if any((_intent_term_present(clause, term, language) for term in terms))]
        if not subjects or not properties:
            continue
        if subjects[0] == 'water' and properties[0] == 'amount':
            properties[0] = 'coverage'
        if language == 'zh-CN':
            clause_denied = bool(re.search('(?:不觉得|并不觉得|不认为).{0,24}(?:拥挤|密集|复杂|太难|太长|太短)', clause))
            clause_avoid = bool(re.search('(?:不喜欢|不希望|不想(?:要|让)?|不愿意|宁可不)', clause))
            clause_prefer = bool(re.search('(?:喜欢|希望|想要|更想要|宁愿|偏向|倾向)', clause))
            clause_insufficient = bool(re.search('(?:太少|过少|不够(?:多|大)?|还不够|覆盖(?:太小|过小|不足))', clause))
            clause_excessive = bool(re.search('(?:太|过多|占地(?:太大|过大)|覆盖(?:太大|过大)|过于|过分)', clause))
            clause_increase = bool(re.search('(?:增加|增多|更多|多一点)', clause))
            clause_decrease = bool(re.search('(?:减少|更少|少一点|缩小)', clause))
            aspect = 'visual' if re.search('(?:视觉|观感|外观|构图)', clause) else 'gameplay' if re.search('(?:路线|路径|推箱|玩法|游玩|节奏|限制)', clause) else 'unspecified'
            scope_match = re.search('(?:左侧|右侧|上方|下方|中央|中间|左上|右上|左下|右下|第\\s*\\d+\\s*行|第\\s*\\d+\\s*列)', clause)
        else:
            clause_denied = bool(re.search("\\b(?:do not|don't) (?:think|feel) .{0,24}(?:crowded|dense|complex|hard|long|short)\\b", clause))
            clause_avoid = bool(re.search("\\b(?:do not|don't) (?:like|want|hope|prefer)|\\bdislike\\b", clause))
            clause_prefer = bool(re.search('\\b(?:like|want|hope|prefer|favor|would rather)\\b', clause))
            clause_insufficient = bool(re.search('\\b(?:not enough|too (?:few|little|small))\\b', clause))
            clause_excessive = bool(re.search('\\btoo\\b|\\boverly\\b', clause))
            clause_increase = bool(re.search('\\b(?:increase|add|more)\\b', clause))
            clause_decrease = bool(re.search('\\b(?:reduce|decrease|less|fewer|shrink)\\b', clause))
            aspect = 'visual' if re.search('\\b(?:visual|appearance|composition|aesthetic)\\b', clause) else 'gameplay' if re.search('\\b(?:route|path|push|gameplay|play|rhythm|constraint)\\b', clause) else 'unspecified'
            scope_match = re.search('\\b(?:left|right|upper|lower|center|middle)\\b', clause)
        quantitative = properties[0] in {'amount', 'coverage'}
        direction = 'decrease' if clause_decrease or (quantitative and clause_excessive and (not clause_insufficient)) else 'increase' if clause_increase or (quantitative and clause_insufficient) else 'avoid' if clause_denied or clause_avoid or clause_excessive else 'prefer' if clause_prefer else 'evaluate'
        degree = 'insufficient' if clause_insufficient else 'excessive' if clause_excessive else ''
        explicit_commitment = clause_denied or clause_avoid or clause_prefer
        frames.append(IntentSemanticFrame(subjects[0], properties[0], direction, degree, 4 + (2 if explicit_commitment else int(direction != 'evaluate')), aspect, scope_match.group(0).casefold() if scope_match else 'stage'))
    return tuple(dict.fromkeys(frames))

def _primary_intent_semantic_frame(message):
    frames = _intent_semantic_frames(message)
    return max(frames, key=lambda item: item.specificity) if frames else None

def _intent_semantic_claims(message):
    direction_map = {'prefer': 'maintain', 'denied': 'avoid'}
    attribute_map = {'crowded': 'density', 'empty': 'density', 'complex': 'complexity', 'short': 'length', 'long': 'length', 'clear': 'clarity', 'concentrated': 'concentration', 'separated': 'separation'}
    return [{'subject': frame.subject, 'attribute': attribute_map.get(frame.property, frame.property), 'direction': direction_map.get(frame.direction, frame.direction), 'degree': frame.degree or 'neutral', 'aspect': frame.aspect, 'scope': frame.scope, 'confidence': 1.0} for frame in _intent_semantic_frames(message)[:4]]

def _intent_frame_options(source, language):
    frame = _primary_intent_semantic_frame(source)
    if frame is None:
        return None
    labels = _INTENT_LABELS_ZH if language == 'zh-CN' else _INTENT_LABELS_EN
    subject = labels[frame.subject]
    property_label = labels[frame.property]
    if language == 'zh-CN':
        if frame.subject == 'water' and frame.property == 'coverage':
            if frame.direction == 'decrease':
                if frame.degree == 'excessive':
                    core = '你认为当前水域覆盖过多，倾向于降低它占据的空间'
                else:
                    core = '你倾向于减少当前水域覆盖范围'
                text = f'我暂时理解为，{core}。你主要在意视觉比例还是路线限制，目前仍未确定，可以纠正我。'
                return (text, text, text)
            if frame.direction == 'increase':
                if frame.degree == 'insufficient':
                    core = '你认为当前水域覆盖不足，倾向于增加它占据的空间'
                else:
                    core = '你倾向于增加当前水域覆盖范围'
                text = f'我暂时理解为，{core}。你主要在意视觉比例还是路线限制，目前仍未确定，可以纠正我。'
                return (text, text, text)
        if frame.degree == 'excessive':
            property_label = f'过于{property_label}'
        elif frame.degree == 'insufficient':
            property_label = f'不足的{property_label}'
        if frame.direction == 'denied':
            core = f'你目前不认为当前{subject}呈现出{property_label}'
            alternate = f'你目前不把当前{subject}看作具有{property_label}'
        elif frame.direction == 'avoid':
            core = f'你不喜欢当前{subject}呈现出的{property_label}'
            alternate = f'你不希望当前{subject}继续维持这种{property_label}'
        elif frame.direction == 'increase':
            core = f'你希望当前{subject}在{property_label}上得到加强'
            alternate = f'你希望当前{subject}不再缺少这种{property_label}'
        elif frame.direction == 'prefer':
            core = f'你更偏好当前{subject}呈现出合适的{property_label}'
            alternate = f'你希望当前{subject}保留或形成合适的{property_label}'
        else:
            core = f'你正在评价当前{subject}的{property_label}'
            alternate = core
        return (f'我暂时理解为，{core}。这先是你对当前设计的可纠正判断；它具体应怎样影响玩法或调整方式，仍由你确认。', f'听起来{alternate}。我会保留这一判断本身，而不会把它直接等同于某个具体改图方案。', f'我读到的是，{core}。至于它主要关乎视觉、游玩感受还是两者兼顾，我先保持开放。')
    if frame.degree == 'excessive':
        property_label = f'overly {property_label}'
    elif frame.degree == 'insufficient':
        property_label = f'insufficient {property_label}'
    if frame.direction == 'denied':
        core = f'you do not currently see the {subject} as {property_label}'
        alternate = f'you do not currently treat the {subject} as {property_label}'
    elif frame.direction == 'avoid':
        core = f'you dislike the {property_label} of the current {subject}'
        alternate = f'you do not want the current {subject} to retain this {property_label}'
    elif frame.direction == 'increase':
        core = f'you want more {property_label} in the current {subject}'
        alternate = f'you do not want the current {subject} to lack this {property_label}'
    elif frame.direction == 'prefer':
        core = f'you prefer an intentional {property_label} in the current {subject}'
        alternate = f'you want the current {subject} to keep or form an intentional {property_label}'
    else:
        core = f'you are evaluating the {property_label} of the current {subject}'
        alternate = core
    return (f'For now, I understand that {core}. This is a correctable judgment about the current design, not yet a specific map revision.', f'It sounds to me like {alternate}. I will keep that evaluation distinct from an unconfirmed gameplay goal or edit.', f'I read that {core}. Whether it mainly concerns appearance, play, or both remains open for correction.')

def _intent_semantic_binding_issue(hypothesis, latest_user, language):
    """Reject a card that loses an explicit current-turn object, property, or direction."""
    expected = _primary_intent_semantic_frame(latest_user)
    if expected is None:
        return None
    actual = _primary_intent_semantic_frame(hypothesis)
    if actual is None:
        return "intentHypothesis lost the user's explicit design subject and evaluation"
    if actual.subject != expected.subject:
        return "intentHypothesis changed the user's explicit design subject"
    relation_readability_reframe = expected.subject in {'box', 'target'} and expected.property in {'concentrated', 'separated'} and (actual.property == 'clear')
    if actual.property != expected.property and (not relation_readability_reframe):
        return "intentHypothesis lost the user's explicit design evaluation"
    if actual.direction != expected.direction and (not relation_readability_reframe):
        return "intentHypothesis reversed or weakened the user's explicit direction"
    if actual.degree != expected.degree and (not relation_readability_reframe):
        return "intentHypothesis lost the user's explicit degree or comparison"
    return None

def _classify_explicit_design_stance(message):
    text = re.sub('\\s+', ' ', str(message or '')).strip()
    lowered = text.casefold().replace('’', "'")
    language = 'zh-CN' if re.search('[\\u3400-\\u9fff]', text) else 'en'
    if not text or _user_explicitly_off_topic(text):
        return DesignStanceClassification(False, language)
    if not _intent_semantic_frames(text):
        if language == 'zh-CN' and re.search('我(?:觉得|认为|担心|感觉).{0,18}(?:玩家|别人).{0,24}(?:会|可能|觉得|认为)|(?:你说|你认为|你觉得).{0,48}我.{0,24}(?:没(?:有)?这个意思|不是这个意思|没说)', text):
            return DesignStanceClassification(False, language)
        if language == 'en' and re.search("\\bi (?:think|feel|worry|believe).{0,36}\\bplayers?\\b.{0,48}\\b(?:will|may|might|feel|think)\\b|\\byou (?:said|think|thought|claim).{0,72}\\bi (?:did not|didn't) (?:say|mean)\\b", lowered):
            return DesignStanceClassification(False, language)
    if language == 'zh-CN':
        rebuttal = re.search('(?:我(?:不认同|不同意|反对)|在我看来并不是|对我来说并不是)', text)
        constraint = re.search('(?:我(?:个人)?(?:希望|更希望|不希望|更不希望|想要|不想要|想让|不想让|更想要|更想让|不愿意)|我(?:宁愿|宁可))', text)
        preference = re.search('(?:我(?:个人)?(?:喜欢|不喜欢|比较喜欢|更喜欢|会更喜欢|倾向(?:于)?|更倾向(?:于)?|偏向|在意|更在意|介意)|对我(?:来说|而言))', text)
        evaluation = re.search('(?:我(?:个人|倒是|反而)?(?:认为|觉得|感觉)|我的看法是|在我看来|在我眼里|依我看)', text)
        topic_terms = _DESIGN_TOPIC_TERMS_ZH
        reference = re.search('(?:这样|这个|这种|这里|那里|当前|现在(?:这样)?|它们?|两者|这两个|那两个|刚才|原来|原先)', text)
    else:
        rebuttal = re.search("\\b(?:i(?: personally)? (?:disagree|object)|i do not agree|i don't agree|in my view that is not|to me that is not)\\b", lowered)
        constraint = re.search("\\b(?:i(?: personally)? (?:want|do not want|don't want|hope|do not hope|don't hope)|i would like|i would not like|i'd like|i'd not like|i would rather|i'd rather)\\b", lowered)
        preference = re.search("\\b(?:i(?: personally)? (?:prefer|like|dislike|care about|favor)|i'd prefer|my preference is|i am inclined|i'm inclined|for me|to me)\\b", lowered)
        evaluation = re.search('\\b(?:i(?: personally)? (?:think|feel|believe)|in my view|from my perspective|as i see it)\\b', lowered)
        topic_terms = _DESIGN_TOPIC_TERMS_EN
        reference = re.search('\\b(?:this|that|these|those|it|they|them|both|here|there|currently|right now|the current one|the previous one)\\b', lowered)
    stance_kind = next((name for name, match in (('rebuttal', rebuttal), ('constraint', constraint), ('preference', preference), ('evaluation', evaluation)) if match is not None), '')
    if not stance_kind:
        return DesignStanceClassification(False, language)
    topics = tuple((topic for topic, terms in topic_terms.items() if any((term in text if language == 'zh-CN' else _english_term_present(lowered, term) for term in terms))))
    if re.search('(?<![A-Za-z0-9])(?:P|B\\d+|T\\d+)(?![A-Za-z0-9])', text, re.IGNORECASE):
        topics = tuple(dict.fromkeys((*topics, 'entities')))
    ambiguous_reference = not topics and (reference is not None or stance_kind == 'rebuttal')
    return DesignStanceClassification(bool(topics or ambiguous_reference), language, stance_kind, topics, ambiguous_reference)

def _user_explicitly_states_design_stance(message):
    """Gate orange cards on an explicit current-turn design stance.

    The broader first-person detector is retained for compatibility in other
    language heuristics.  This stricter predicate is the server-owned card
    boundary: observations, requests for ideas, operations, and inferred
    behavioral evidence do not qualify on their own.
    """
    return _classify_explicit_design_stance(message).eligible

def _unclear_revision_reply(language, latest_user=None):
    source = str(latest_user or '')
    visual_only = bool(re.search('(?:\\u4e0d\\u597d\\u770b|\\u597d\\u770b|\\u7f8e\\u89c2|\\u89c6\\u89c9|\\u8f6e\\u5ed3|\\b(?:ugly|looks?|visual|shape|outline|aesthetic)\\b)', source, re.IGNORECASE))
    if language == 'zh-CN':
        if visual_only:
            return '收到，你对当前局部观感的反馈很明确，但“不好看”还不足以确定应该改动哪种设计关系。我观察到这可能涉及地图轮廓和地形分割的几何观感，也可能涉及玩家推箱时路线转折与节奏的感受。这两种重点会导向不同的修改，而你也可能希望两者兼顾，所以我现在不会擅自把视觉反馈解释成确定的玩法目标。我会把这个边界留在方案澄清中，再根据你接下来的回答收敛修改。'
        return '可以，我愿意和你一起调整，但你这次只明确表达了希望改动，还没有指出具体的设计对象和期望效果。这意味着我目前无法判断你更在意路线阅读、推动关系、空间感受还是其他玩家体验，这些方向会导向不同的修改。如果现在直接替你改图，我其实是在替你猜测设计目标，而不是响应已经明确的意图。因此，我会先通过方案澄清收集必要信息，而不是用暂定意图代替方案。'
    if visual_only:
        return 'Your feedback clearly says that this part does not look right, but that phrase alone does not identify which design relationship should change. I can read it as concern about the map outline and geometric composition, or as concern about how route turns feel while the player is pushing boxes. Those priorities lead to different revisions, and you may also want a balance of both, so I am not converting visual feedback into a confirmed gameplay goal. I will keep that boundary inside proposal clarification and use your next answer to narrow the revision.'
    return 'Yes, I am happy to work on it with you, but this request identifies a desire for change without naming the design object or the intended effect. I still cannot tell whether the priority is route reading, push relationships, spatial feeling, or another part of the player experience, and each would lead to a different revision. If I edited the map now, I would be guessing on your behalf rather than responding to a direction you actually established. I will therefore collect the missing proposal detail before attempting a validated revision.'

def _natural_intent_candidate(latest_user, language, explicit_agreement, *, difficulty_reframe=False):
    source = re.sub('\\s+', ' ', str(latest_user or '')).strip()
    variant = sum((ord(character) for character in source)) % 3
    if difficulty_reframe:
        stance = _difficulty_stance(source)
        if language == 'zh-CN':
            if stance == 'easy':
                options = ('我暂时把你的方向理解为：你要的不是多走几步，而是让关键推动前真的出现需要停下来读路线的判断；如果我理解偏了，请纠正我。', '听起来你更在意的是，玩家不能只顺着流程把箱子推到底，而要在关键节点承担一次真实的路线选择；这只是我目前的理解。', '我读到的倾向是，你希望这张图的难度来自推动顺序和后果，而不只是表面上多加一点障碍；若重点不是这里，你可以改正我。')
            else:
                options = ('我暂时把你的方向理解为：你更想减轻关键推动前的负担，让玩家能读懂路线而不是被无谓阻力拖住；如果我理解偏了，请纠正我。', '听起来你在意的是保留判断感，但别让路线复杂到遮住真正的选择；这只是我目前的理解。', '我读到的倾向是，你希望难度来自清楚的取舍，而不是让人因为读不清路线而受挫；若重点不是这里，你可以改正我。')
        elif stance == 'easy':
            options = ('For now, I understand your direction as wanting a real route judgment before a key push, not simply more walking; please correct me if I have that wrong.', 'It sounds to me like you want the player to make a consequential choice rather than push straight through the route; that is only my current reading.', 'I read your preference as difficulty coming from push order and consequences, not merely from adding obstacles; please correct me if that misses the point.')
        else:
            options = ('For now, I understand your direction as keeping the key route readable instead of adding friction that hides the real choice; please correct me if I have that wrong.', 'It sounds to me like you want to preserve meaningful judgment without making the route harder to read; that is only my current reading.', 'I read your preference as wanting difficulty to come from clear trade-offs rather than confusing resistance; please correct me if that misses the point.')
        return _ensure_intent_card_sentence_shape(options[variant], language)
    if language == 'zh-CN':
        if explicit_agreement:
            options = ('听起来你已经准备把刚才的想法放进一次具体尝试里；这是我目前的理解。', '我暂时把你的方向理解为：先让这个局部想法真正参与一次游玩判断。', '我读到的倾向是，你更想先做出一个能亲手比较的局部变化。')
        else:
            options = _semantic_intent_options(source, 'zh-CN')
    elif explicit_agreement:
        options = ('It sounds to me like you are ready to put that idea into a concrete trial.', 'For now, I understand your direction as testing this idea through a local change.', 'I read your preference as making this idea tangible enough to compare in play.')
    else:
        options = _semantic_intent_options(source, 'en')
    return _ensure_intent_card_sentence_shape(options[variant], language)

def _ensure_intent_card_sentence_shape(value, language):
    text = re.sub('\\s+', ' ', str(value or '')).strip()
    sentence_count = len([part for part in re.split('(?<=[.!?;。！？；])\\s*', text) if part.strip()])
    if sentence_count >= 2:
        return text
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', text):
        if re.search('(?:只是我(?:当前|目前)的理解|可以(?:直接)?纠正|请纠正|仍可纠正)', text):
            return text
        return f'{text} 这只是我目前的理解，你可以直接纠正它。'
    return f'{text} This is only my current reading, and you can correct it directly.'

def _replace_echoed_intent_hypothesis(hypothesis, latest_user, language):
    """Keep intent cards interpretive rather than a prefixed copy of user text."""
    if not hypothesis or not latest_user:
        return hypothesis
    source = _intent_comparison_text(latest_user, language)
    candidate = _intent_comparison_text(hypothesis, language)
    if len(source) < 5 or len(candidate) < 5:
        return hypothesis
    similarity = SequenceMatcher(None, source, candidate).ratio()
    if source in candidate or similarity >= 0.82:
        variant = sum((ord(character) for character in source)) % 3
        return _semantic_intent_options(str(latest_user), language)[variant]
    return hypothesis

def _intent_comparison_text(text, language):
    value = re.sub('\\s+', '', str(text or '')).strip('。！？?!：:”“"\' ')
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', value):
        value = re.sub('^(?:我暂时把你的方向理解为|听起来你更在意的是|我读到的倾向是|我猜你可能|我觉得你可能|我感觉你(?:可能|似乎))[:：]?', '', value)
    else:
        value = re.sub('^(?:For now, I understand your direction as(?: wanting)?|It sounds to me like you|I read your preference as(?: wanting)?)[: ]*', '', value, flags=re.IGNORECASE)
    return value.casefold()

def _semantic_intent_options(source, language):
    value = str(source or '').casefold()
    classification = _classify_explicit_design_stance(source)
    framed_options = _intent_frame_options(source, language)
    if framed_options is not None:
        return framed_options
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', str(source or '')):
        if classification.ambiguous_reference:
            return ('我暂时理解为，你对当前呈现出的整体设计关系有一个明确偏好。具体在意的是布局观感还是实际玩法效果，目前仍然开放，你可以纠正我。', '听起来你更偏向保留或调整现在这种整体感觉。因为指代还不具体，我不会替你绑定某个地图对象或修改方式。', '我读到的是你对当前设计方向有所取舍。至于这个取舍落在空间、路线还是游玩体验上，我先保持为可修正的开放理解。')
        if re.search('(?:太空了?|空旷|空荡|显得空|过于空|比较空|别那么空|不要那么空|不那么空)', source):
            region = next((name for name in ('左下角', '右下角', '左上角', '右上角', '中间') if name in source), '这片局部区域')
            return (f'我暂时理解的是，你希望{region}不再显得空旷；我还不确定你主要在意视觉平衡，还是希望它影响实际路线与推箱节奏，请纠正我。', f'听起来你希望{region}的空间分布更有内容；这可能是视觉上的不平衡，也可能关系到实际玩法，我暂时不替你确定是哪一种。', f'我读到的倾向是，你想改善{region}的空旷感；我还需要确认重点是布局观感，还是玩家经过这里时的路线与推动体验。')
        if any((marker in source for marker in ('目标', '终点', '落点', 'T1', 'T2'))):
            return ('我暂时理解为，你更在意目标点之间保持清楚、可区分的空间关系。它主要影响布局观感还是实际推箱路线，目前仍需要由你确认。', '听起来你不希望目标区域的关系显得过于集中或含混。至于重点是视觉分布还是推进选择，我先不替你确定。', '我读到的倾向是，你希望目标位置之间呈现更合适的关系。具体应该怎样影响路线和节奏，仍是可以纠正的部分。')
        if '水' in source and any((marker in source for marker in ('形', '改', '变', '水域'))):
            return ('我暂时理解的是，你想让水域真正重写玩家读路线和推进时机的方式，而不是只补一个局部缺口；如果我抓错重点，请纠正我。', '听起来你更在意的是，水要从背景变成会迫使玩家重新判断绕行与推进的边界；这只是我当前的理解。', '我读到的倾向是，你希望水域的布局改变箱子经过时的选择感，而不只是换一个视觉形状；若重点不是这里，请改正我。')
        if '墙' in source:
            return ('我暂时理解的是，你想让墙体改变路线的取舍，而不只是增加一块阻挡；如果我抓错重点，请纠正我。', '听起来你更在意的是，墙应当让玩家在推进前读出不同路径的后果；这只是我当前的理解。', '我读到的倾向是，你希望障碍承担选择压力，而不是单纯拉长路线；若重点不是这里，请改正我。')
        if 'relations' in classification.topics or 'topology' in classification.topics:
            return ('我暂时理解为，你对当前空间或路径关系有一个明确偏好。它更偏向视觉组织还是实际推进体验，我现在不替你下结论。', '听起来你更在意各部分之间怎样形成清楚的空间关系。至于这种关系应当怎样改变游玩，仍然可以由你修正。', '我读到的是你希望当前局部关系更符合自己的设计判断。它具体指向布局、路线还是两者兼顾，我先保持开放。')
        if 'mechanics' in classification.topics:
            return ('我暂时理解为，你更在意当前解法与推动机制呈现出的实际体验。具体需要保留或改变哪一种机制，仍然由你确认。', '听起来你的判断重点落在玩家实际解题和推进的过程上。我不会把它直接解释成某一种地图修改。', '我读到的是你对当前解法机制有一个可修正的倾向。它应当怎样影响难度或路线，我先不替你确定。')
        if 'experience' in classification.topics:
            return ('我暂时理解为，你更在意玩家实际感受到的设计体验。具体由哪一种布局或路线机制来实现，目前仍然开放。', '听起来你对当前体验效果有一个明确偏好。我不会把这份偏好直接等同于某个具体改图方案。', '我读到的是你希望当前关卡呈现不同的体验重点。这个重点怎样落到地图结构上，仍需要由你确认。')
        return ('我暂时理解的是，你希望这次局部改动真的改变玩家读路线和作决定的方式，而不只是调整某个格子；如果我抓错重点，请纠正我。', '听起来你更在意的是，下一步要能在游玩中感到明确差异，而不是停留在表面的布局变化；这只是我当前的理解。', '我读到的倾向是，你希望这项调整影响实际的推进选择；若重点不是这里，请改正我。')
    if classification.ambiguous_reference:
        return ('For now, I understand that you have a clear preference about the current overall design relationship. Whether it concerns appearance or actual play remains open for correction.', 'It sounds to me like you prefer keeping or changing the current overall feel. Because the reference is still broad, I will not bind it to a specific map object or edit.', 'I read a definite choice about the current design direction. Whether that choice concerns space, routes, or play experience is still deliberately unresolved.')
    if re.search('(?:too\\s+empty|feels?\\s+empty|sparse|vacant)', value):
        return ('For now, I understand that you want this area to feel less empty; I am not yet sure whether you mainly mean visual balance or an effect on routes and push rhythm.', 'It sounds like you want more purpose in this open area, while the distinction between appearance and play remains open for correction.', "I read your preference as improving this area's emptiness, but I still need to confirm whether the priority is composition or the player's route and pushing experience.")
    if any((_english_term_present(value, term) for term in ('target', 'targets', 'goal', 'goals'))):
        return ("For now, I understand that you care about keeping the targets' spatial relationship clear and distinguishable. Whether the priority is visual distribution or the pushing route remains open for correction.", 'It sounds to me like you do not want the target area to feel overly concentrated or ambiguous. I am not yet deciding whether that preference is mainly visual or mechanical.', 'I read your preference as wanting a more suitable relationship between target positions. How that should affect route rhythm remains for you to confirm.')
    if 'water' in value:
        return ('For now, I understand your direction as wanting water to reshape route reading and push timing, rather than merely change a local patch; please correct me if I have that wrong.', 'It sounds to me like you want water to become a boundary that makes rerouting and pushing worth judging, not background decoration; that is only my current reading.', "I read your preference as wanting the water layout to change the choices around a box's passage, rather than only its visual shape; please correct me if that misses the point.")
    if 'relations' in classification.topics or 'topology' in classification.topics:
        return ('For now, I understand that you have a clear preference about the current spatial or path relationship. I am not yet deciding whether its main purpose is visual organization or play.', 'It sounds to me like you care about how the parts form a readable spatial relationship. The intended gameplay consequence remains open for correction.', 'I read a preference for changing how this local relationship feels. Whether that means composition, route structure, or both is still unresolved.')
    if 'mechanics' in classification.topics:
        return ('For now, I understand that you care about the experience created by the current solution and pushing mechanism. The particular mechanism to preserve or change remains for you to confirm.', 'It sounds to me like your judgment concerns the actual solving and pushing process. I will not turn that directly into a specific map edit.', 'I read a correctable preference about the current solution mechanism. How it should affect difficulty or routes remains open.')
    if 'experience' in classification.topics:
        return ("For now, I understand that you care about the player's experienced design effect. The layout or route mechanism that should create it remains open for correction.", 'It sounds to me like you have a clear preference about the current experience. I will not equate that preference with a specific map revision.', 'I read a preference for a different experiential emphasis in the current level. How that should map onto structure remains for you to confirm.')
    return ('For now, I understand your direction as wanting this local change to alter how the player reads the route and makes a decision, rather than merely changing a tile; please correct me if I have that wrong.', 'It sounds to me like the next change needs to create a felt difference in play, not just a surface-level layout adjustment; that is only my current reading.', 'I read your preference as wanting this adjustment to affect a real push decision; please correct me if that misses the point.')

def _difficulty_stance(text):
    value = str(text or '').casefold()
    if any((marker in value for marker in ('太简单', '太容易', '不够难', '没难度', '太顺', '一路平推', 'too easy', 'too simple', 'not hard enough', 'no challenge'))):
        return 'easy'
    if any((marker in value for marker in ('太难', '太复杂', '太绕', '不够简单', '太卡', 'too hard', 'too difficult', 'too complex', 'too confusing'))):
        return 'hard'
    return None

def _user_reframes_difficulty_judgment(messages):
    latest = _latest_role_content(messages, 'user')
    if not latest or '?' in latest or '？' in latest:
        return False
    stance = _difficulty_stance(latest)
    if stance is None:
        return False
    previous_assistant = _latest_role_content(messages[:-1], 'assistant')
    if not previous_assistant:
        return False
    lowered = latest.casefold()
    first_person_evaluation = any((marker in latest for marker in ('我认为', '我觉得', '我感觉', '我不认同', '我不同意'))) or bool(re.search('\\b(?:i think|i feel|i disagree|to me)\\b', lowered))
    contrast = any((marker in lowered for marker in ('还是', '其实', '但', '不认同', '不同意', 'still', 'actually', 'but', 'however', 'disagree')))
    previous_stance = _difficulty_stance(previous_assistant)
    return first_person_evaluation and (contrast or previous_stance is not None)

def _intent_is_only_execution_authorization(text, language='en'):
    value = str(text or '').strip().casefold()
    if not value:
        return False
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', value):
        execution = any((marker in value for marker in ('你帮我改', '帮我改一下', '你来改', '请你改', '交给你改', '我来帮你改', '把修改交给我', '由我来改', '帮我做', '就这么做', '照这个做', '按这个做')))
        design_anchors = ('水', '箱', '目标', '墙', '通道', '路线', '推动', '落点', '节奏', '难度', '选择', '可读', '绕行', '空间')
    else:
        execution = bool(re.search('\\b(?:help me|you|assistant|i should)\\s+(?:change|modify|revise|edit|do)\\b', value))
        design_anchors = ('water', 'box', 'crate', 'target', 'wall', 'corridor', 'route', 'push', 'rhythm', 'difficulty', 'choice', 'readability', 'space')
    return execution and (not any((anchor in value for anchor in design_anchors)))

def _cue_repeats_current_evidence(cue_type, text, recent_guidance, evidence_signature):
    previous = ((recent_guidance or {}).get('uiCues') or {}).get(cue_type) or {}
    return previous.get('evidenceSignature') == evidence_signature and _guidance_text_matches(text, previous.get('text'))

def _warning_has_strong_evidence(text, language, rows, stage_context, play_summary):
    if not _warning_text_is_evidence_grounded(text, language):
        return False
    lowered = str(text or '').casefold()
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', lowered):
        categories = (('water', ('水', '水边', '水域')), ('box', ('箱', '箱子')), ('target', ('目标', '目标点')), ('wall', ('墙', '通道', '角落')))
        actions = ('推', '进入', '贴着', '绕', '到达', '退回', '卡住')
        consequences = ('退路', '顺序', '死锁', '软锁', '误读', '重复', '无法', '卡住')
        uncertainty = ('可能', '也许', '我有点', '我在意', '我不太放心', '值得')
    else:
        categories = (('water', ('water', 'water edge')), ('box', ('box', 'crate')), ('target', ('target', 'goal')), ('wall', ('wall', 'corridor', 'corner')))
        actions = ('push', 'enter', 'along', 'detour', 'reach', 'return', 'stuck')
        consequences = ('escape', 'order', 'deadlock', 'soft lock', 'misread', 'repeat', 'unable', 'stuck')
        uncertainty = ('may', 'might', 'I notice', 'I am uneasy', 'I am not sure', 'worth')
    present_tiles = set(''.join((str(row) for row in rows or [])))
    tile_requirements = {'water': '@', 'box': 's', 'target': 't', 'wall': '#'}
    mentioned = {name for name, words in categories if tile_requirements[name] in present_tiles and any((word.casefold() in lowered for word in words))}
    mechanical = len(mentioned) >= 2 and any((word.casefold() in lowered for word in actions)) and any((word.casefold() in lowered for word in consequences)) and any((word.casefold() in lowered for word in uncertainty))
    return mechanical or _play_evidence_is_strong(play_summary)

def _play_evidence_is_strong(play_summary):
    evidence = play_summary or {}
    restarts = int(evidence.get('restartCount') or 0)
    moves = evidence.get('moveCount')
    minimum_moves = evidence.get('minimumMoves')
    pushes = evidence.get('pushCount')
    minimum_pushes = evidence.get('minimumPushes')
    excessive_moves = isinstance(moves, int) and isinstance(minimum_moves, int) and (minimum_moves > 0) and (moves - minimum_moves >= 8) and (moves / minimum_moves >= 1.5)
    excessive_pushes = isinstance(pushes, int) and isinstance(minimum_pushes, int) and (minimum_pushes > 0) and (pushes - minimum_pushes >= 2) and (pushes / minimum_pushes >= 1.35)
    return restarts > 0 or excessive_moves or excessive_pushes

def _deterministic_play_warning(language, play_summary):
    if not _play_evidence_is_strong(play_summary):
        return None
    evidence = play_summary or {}
    restarts = int(evidence.get('restartCount') or 0)
    if language == 'zh-CN':
        if restarts:
            return f'我注意到这次试玩重开了 {restarts} 次；某个推动后的退路可能不够直观，我会把它当成值得继续观察的信号。'
        return '这次实际路线明显长于最短解，我会留意玩家是否在某个推动节点反复绕行，而不急着把它判定为难度。'
    if restarts:
        return f'I notice this playthrough restarted {restarts} time(s); an escape route after one of the pushes may not read clearly, so I would keep an eye on that moment.'
    return 'This playthrough wandered well beyond the shortest route; I would watch for a push where the player circles back, without treating that alone as proof of difficulty.'

def _contextual_manual_edit(rows, language):
    rows = list(rows or [])
    water = []
    boxes = []
    targets = []
    for row_index, row in enumerate(rows):
        for column_index, tile in enumerate(str(row)):
            if tile == '@':
                water.append((row_index, column_index))
            elif tile == 's':
                boxes.append((row_index, column_index))
            elif tile == 't':
                targets.append((row_index, column_index))
    if water:
        anchor_zh = '水域边缘与相邻的推箱路线'
        anchor_en = 'the water edge and its neighboring box route'
    elif boxes and targets:
        anchor_zh = '箱子进入目标区域前的通路'
        anchor_en = 'the route before a box enters the target area'
    else:
        anchor_zh = '当前讨论的局部通路'
        anchor_en = 'the local route under discussion'
    if language == 'zh-CN':
        return f'如果你想亲手验证，可以在右侧编辑器只围绕{anchor_zh}做一次小范围尝试，保存后观察第一次推动时路线选择是否更清楚；这只是用来比较体验，不必当作最终方案。'
    return f'If you want to test it directly, you could make one small experiment around {anchor_en} in the editor, then save and watch whether the route choice is clearer at the first push; it is a comparison, not a required final layout.'

def _validate_disagreement(value, language):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('guidance.disagreement must be an object or null.')
    required = {'status', 'subject', 'userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion', 'resolution'}
    if set(value) != required:
        raise ValueError('guidance.disagreement contains unexpected or missing fields.')
    status = value.get('status')
    subject = value.get('subject')
    resolution = value.get('resolution')
    if status not in DISAGREEMENT_STATUSES:
        raise ValueError('guidance.disagreement.status is invalid.')
    if subject not in DISAGREEMENT_SUBJECTS:
        raise ValueError('guidance.disagreement.subject is invalid.')
    if status in {'active', 'acknowledged'} and resolution is not None:
        raise ValueError('An active or acknowledged disagreement must have a null resolution.')
    if status == 'acknowledged' and subject != 'human_edit':
        raise ValueError('Only a human_edit disagreement may be acknowledged.')
    if status == 'resolved' and resolution not in DISAGREEMENT_RESOLUTIONS:
        raise ValueError('A resolved disagreement requires a valid resolution.')
    normalized = {'status': status, 'subject': subject}
    for field_name in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion'):
        normalized[field_name] = _normalize_single_level_language(_clean_text(value.get(field_name), f'guidance.disagreement.{field_name}'))[:1200]
    if status == 'active':
        normalized['coreDisagreement'] = _detailed_disagreement_core(normalized['userPosition'], normalized['aiPosition'], normalized['coreDisagreement'], language)
    normalized['resolution'] = resolution
    return normalized

def _detailed_disagreement_core(user_position, ai_position, core, language):
    """Keep a model comparison, expanding legacy terse cards from validated fields."""
    text = _normalize_response_paragraphs(str(core or ''))[:1200]
    user_summary = _normalize_response_paragraphs(str(user_position or '')).rstrip('.。')[:320]
    ai_summary = _normalize_response_paragraphs(str(ai_position or '')).rstrip('.。')[:420]
    core_summary = text.rstrip('.。')[:320]
    if language == 'zh-CN':
        has_stance = any((token in text for token in ('认同', '同意', '不同意', '不认同', '理解', '担心')))
        has_reason = any((token in text for token in ('因为', '由于', '原因', '风险', '证据')))
        has_contrast = any((token in text for token in ('但是', '不过', '而我', '相比', '分歧', '更关注')))
        if len(text) >= 48 and has_stance and has_reason and has_contrast:
            return text
        expanded = f'我理解你的观点是：{user_summary}。我目前仍有不同意见，因为{ai_summary}。你的方向更关注前一种体验，而我的判断更关注已经指出的玩法风险；我们的分歧主要来源于：{core_summary}。'
    else:
        lowered = text.lower()
        has_stance = any((token in lowered for token in ('agree', 'understand', 'concern', 'accept')))
        has_reason = any((token in lowered for token in ('because', 'since', 'reason', 'risk', 'evidence')))
        has_contrast = any((token in lowered for token in ('but', 'however', 'while', 'whereas', 'difference', 'focus')))
        if len(text) >= 80 and has_stance and has_reason and has_contrast:
            return text
        expanded = f'I understand your position as: {user_summary}. I still see it differently because {ai_summary}. Your direction prioritizes that intended experience, while my judgment prioritizes the identified play risk; the concrete decision at the heart of our disagreement is: {core_summary}.'
    return _normalize_response_paragraphs(expanded)[:1200]

def _disagreement_from_warning(warning_text, language, stage_context, user_position=None):
    warning = str(warning_text or '').strip()
    context = stage_context or {}
    change_summary = context.get('changeSummary') or {}
    components = ', '.join((str(item) for item in change_summary.get('components') or []))
    if language == 'zh-CN':
        user_position = user_position or f"用户刚刚完成了{components or '这处地图'}的手动修改，并希望保留这次调整。"
        ai_position = f'我担心这次修改在具体游玩时刻带来问题：{warning}'
        core = f'我理解你希望保留这次修改，因为它更接近你想要的地图体验。但是我目前不能完全认同直接保留，因为已经确认的风险是：{warning}。你的方向更关注修改带来的体验，而我的判断更关注具体推动风险和可解性；我们的分歧主要来源于是否应原样保留当前布局，还是先处理风险再保留它的设计目标。'
        next_question = '这次修改最想保留的游玩效果是什么？如果保留当前布局，如何处理我指出的具体风险？'
    else:
        user_position = user_position or f"The designer just saved a manual change to {components or 'this part of the map'} and wants to keep it."
        ai_position = f'I am concerned that the change creates a concrete play problem: {warning}'
        core = f"I understand why you want to keep this edit because it is closer to the experience you intend. However, I do not yet agree that it should remain unchanged because the verified risk is: {warning}. Your direction focuses more on the edit's intended experience, while my concern focuses on the concrete push risk and solvability; our disagreement is whether to keep this layout as-is or address the risk while preserving its goal."
        next_question = 'Which play effect is most important to preserve, and how should we address the concrete risk I observed?'
    return {'status': 'active', 'subject': 'human_edit' if context.get('source') == 'human_edit' else 'user_request', 'userPosition': user_position[:1200], 'aiPosition': ai_position[:1200], 'coreDisagreement': core[:1200], 'nextQuestion': next_question[:1200], 'resolution': None}

def _normalize_intent_hypothesis(hypothesis, language):
    text = hypothesis.strip()
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', text):
        direct_prefixes = ('我猜你可能', '我觉得你可能', '我感觉你可能', '我感觉你似乎', '在我看来，你可能', '我倾向于认为你可能', '听起来你', '我暂时把你的方向理解为', '我读到的倾向是', '我在想，你可能')
        if text.startswith(direct_prefixes):
            return text
        text = re.sub('^(?:这位)?(?:设计者|玩家)', '你', text)
        match = re.match('^你(?:想要|希望|想)(.*)$', text)
        if match:
            return f'听起来你更想要{match.group(1)}'
        if text.startswith('你可能'):
            return f'我觉得{text}'
        if text.startswith('你似乎'):
            return f'我感觉{text}'
        return f'我暂时把你的方向理解为：{text}'
    if re.match('^(?:I think you (?:may|might)|My guess is (?:that )?you (?:may|might)|I (?:suspect|wonder whether) you (?:may|might)|I get the sense that you (?:may|might)|It seems to me that you (?:may|might)|It sounds to me like you|For now, I understand your direction as|I read your preference as)\\b', text, re.IGNORECASE):
        return text
    third_person_patterns = (('^(?:the )?(?:designer|player) wants? to\\b(.*)$', 'It sounds to me like you want to'), ('^(?:the )?(?:designer|player) wants?\\b(.*)$', 'I read your preference as wanting'), ('^(?:the )?(?:designer|player) (?:seems|appears) to want\\b(.*)$', 'I get the sense that you may want'), ('^(?:the )?(?:designer|player) may want\\b(.*)$', 'For now, I understand your direction as wanting'), ('^(?:the )?(?:designer|player) (?:is|seems to be) aiming to\\b(.*)$', 'I think you may be aiming to'), ('^(?:the )?(?:designer|player) (?:prefers|seems to prefer)\\b(.*)$', 'I think you may prefer'))
    for pattern, prefix in third_person_patterns:
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            return f'{prefix}{match.group(1)}'
    text = re.sub('^(?:the )?(?:designer|player)\\b', 'you', text, flags=re.IGNORECASE)
    patterns = (('^you (?:want|want to|hope|hope to)\\b(.*)$', 'It sounds to me like you want'), ('^you (?:may|might) want\\b(.*)$', 'For now, I understand your direction as wanting'), ('^you (?:seem|appear) to want\\b(.*)$', 'I get the sense that you may want'), ('^you (?:are|seem to be) aiming to\\b(.*)$', 'I think you may be aiming to'), ('^you (?:prefer|seem to prefer)\\b(.*)$', 'I think you may prefer'))
    for pattern, prefix in patterns:
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            return f'{prefix}{match.group(1)}'
    if re.match('^you\\b', text, re.IGNORECASE):
        return f'I think you may be signaling this direction: {text}'
    return f'For now, I understand your direction as: {text}'

def _intent_hypothesis_detail_issue(value, language, *, semantic_reviewed=False):
    """Require one concise, tentative, coherent orange-card understanding."""
    text = re.sub('\\s+', ' ', str(value or '')).strip()
    chinese = language == 'zh-CN' or bool(re.search('[\\u3400-\\u9fff]', text))
    if not text:
        return 'intentHypothesis is empty'
    sentence_count = _intent_sentence_count(text)
    if re.search('(?:\\u4e92\\u4e0d\\u517c\\u5bb9|\\u65e2\\u8981.{0,80}\\u53c8\\u4e0d\\u8981|\\bmutually incompatible\\b|\\bboth\\b.{0,100}\\b(?:and the opposite|but not that)\\b)', text, re.IGNORECASE):
        return 'intentHypothesis combines incompatible directions'
    if re.search('(?:\\u8fd9\\u8bc1\\u660e\\u4f60|\\u56e0\\u4e3a.{0,100}\\u6240\\u4ee5\\u4f60\\u4e00\\u5b9a|\\b(?:this )?proves you\\b|\\bbecause\\b.{0,120}\\byou (?:definitely|certainly) (?:want|prefer)\\b)', text, re.IGNORECASE):
        return 'intentHypothesis turns behavioral evidence into certainty'
    if chinese:
        tentative = re.search('(?:\\u6682\\u65f6|\\u542c\\u8d77\\u6765|\\u6211\\u8bfb\\u5230|\\u6211\\u7406\\u89e3|\\u76ee\\u524d|\\u53ef\\u80fd|\\u50cf\\u662f)', text)
    else:
        tentative = re.search('\\b(?:for now|it sounds|i read|i understand|my current(?: tentative)? reading|may|might|seem)\\b', text, re.IGNORECASE)
    if not tentative and (not semantic_reviewed):
        return 'intentHypothesis must remain explicitly tentative'
    return None

def _intent_body_detail_issue(value, language, hypothesis=None):
    return None

def _intent_body_presentation_issue(value, language, hypothesis=None):
    text = re.sub('\\s+', ' ', str(value or '')).strip()
    chinese = language == 'zh-CN' or bool(re.search('[\\u3400-\\u9fff]', text))
    if re.search('(?:下面的倾向|当作对当前设计效果的直接反馈|尚未被你确认的设计边界|inclination below|treating what you just said as direct feedback)', text, re.IGNORECASE):
        return 'intent body contains workflow boilerplate'
    paragraphs = [part.strip() for part in re.split('\\n\\s*\\n', str(value or '')) if part.strip()]
    if re.search('(?:一种|另一种|第二种|其二)[^。！？]{0,40}(?:效果|影响)[^。！？]{0,20}是[^。！？]{1,160}的[。！？]\\s*$', text):
        return 'intent body leaves an enumerated consequence incomplete'
    if any((part.endswith((';', '；', ':', '：', ',', '，')) for part in paragraphs)) or re.search('(?:^|[。！？.!?]\\s*)(?:是让|而是让|还是让|because|while)\\s*[^。！？.!?]*[;；]?\\s*$', text, re.IGNORECASE):
        return 'intent body ends with an incomplete clause'
    if any((re.search('(?:你是希望|你希望的是|具体来说|例如|包括|the options are|do you mean)\\s*[:：]\\s*$', part, re.IGNORECASE) for part in paragraphs)):
        return 'intent body contains a dangling introduction'
    if any((re.search('^\\s*(?:[-*•]|\\d+[.)、])\\s*\\S+', line) for line in str(value or '').splitlines())):
        return 'intent body contains an option or fragment list'
    if hypothesis and _guidance_reuses_visible_sentence(hypothesis, text):
        return 'intent body repeats the intent card'
    map_related = bool(re.search('(?:水|墙|箱|目标|地图|布局|路线|路径|空间|water|wall|box|crate|target|map|layout|route|path|space)', str(hypothesis or ''), re.IGNORECASE))
    if map_related:
        effect_families = ('(?:视觉|观感|外观|构图|visual|appearance|composition|aesthetic)', '(?:空间|区域|通道|走廊|活动|spatial|space|area|corridor|passage)', '(?:路线|路径|绕行|可达|route|path|detour|reach)', '(?:推箱|推动|箱子|顺序|push|box|crate|order)', '(?:节奏|难度|压力|选择|rhythm|difficulty|pressure|choice)')
        if sum((bool(re.search(pattern, text, re.IGNORECASE)) for pattern in effect_families)) < 2:
            return 'intent body does not distinguish two design consequences'
        if not re.search('(?:可能|也可能|尚未|还不|仍未|不确定|区分|或者|还是|may|might|uncertain|not yet|distinguish|whether|or)', text, re.IGNORECASE):
            return 'intent body does not preserve the unresolved design boundary'
    if chinese:
        return None if len(text) >= 120 and _intent_sentence_count(text) >= 3 else 'Chinese intent body is too brief'
    return None if len(re.findall("\\b[\\w'-]+\\b", text)) >= 70 and _intent_sentence_count(text) >= 3 else 'English intent body is too brief'

def _detailed_intent_fallback(hypothesis, latest_user, language):
    """Create a concise, correctable inclination without map invention."""
    source = re.sub('\\s+', ' ', str(latest_user or '')).strip()
    visual_only = bool(re.search('(?:\\u4e0d\\u597d\\u770b|\\u597d\\u770b|\\u7f8e\\u89c2|\\u89c6\\u89c9|\\u8f6e\\u5ed3|\\b(?:ugly|looks?|visual|shape|outline|aesthetic)\\b)', source, re.IGNORECASE))
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', source):
        if visual_only:
            return '我暂时理解为，你对当前局部观感不满意。我还不确定你更在意地图轮廓，还是游玩时路线转折的观感。'
        core = next((part.strip().rstrip('。！？.!?') for part in re.split('(?<=[\\u3002\\uFF01\\uFF1F.!?])\\s*', str(hypothesis or '').strip()) if part.strip()), '我暂时理解到你对当前设计效果有一个待确认的偏好')
        return f'{core}。我目前只把它当作一个可修正的设计倾向。'
    core = next((part.strip().rstrip('.!?') for part in re.split('(?<=[.!?])\\s*', str(hypothesis or '').strip()) if part.strip()), 'For now, I understand that you may have a preference about the current design effect')
    if visual_only:
        return 'For now, I understand that the local appearance does not yet match what you want. I cannot yet tell whether you care more about the map outline or how route turns look during play.'
    return f'{core}. I am treating this only as a correctable design inclination for now.'
