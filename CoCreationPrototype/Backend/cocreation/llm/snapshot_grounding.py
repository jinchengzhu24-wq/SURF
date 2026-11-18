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
from .config_and_runtime import CHAT_MAX_PARAGRAPHS, CHAT_PARAGRAPH_MAX_CHINESE_CHARS, CHAT_PARAGRAPH_MAX_LATIN_WORDS, CHAT_RESPONSE_HARD_LENGTH, COORDINATE_LINK_LIMIT, _VISIBLE_CHINESE_LATIN_ALLOWLIST, _content_block_sentences, _latest_user_text, _log_llm_event, _strip_unreferenced_current_map_claims, _visible_internal_identifier


_GROUNDING_MEMO = ContextVar('cocreation_grounding_memo', default=None)
_VISIBLE_INTERNAL_TERM_REPLACEMENTS = {'gridDistance': ('曼哈顿距离', 'Manhattan distance'), '_solver': ('确定性求解器', 'deterministic solver'), 'solutionSteps': ('求解步数', 'solution steps'), 'solutionPushes': ('推动次数', 'pushes'), 'minimumPushes': ('最少推动数', 'minimum pushes'), 'pushBlocks': ('推动分段数', 'push blocks'), 'boxAlternations': ('箱子交替次数', 'box alternations'), 'pushesByBox': ('各箱子的推动次数', 'pushes by box'), 'pushOrder': ('推动顺序', 'push order'), 'targetAssignments': ('目标对应关系', 'target assignments'), 'routeMode': ('路线概况', 'route mode'), 'analysisContext': ('路线分析信息', 'route analysis'), 'searchedStates': ('搜索状态数', 'searched states'), 'tileAt': ('当前格子', 'current tile'), 'mapFacts': ('当前地图事实', 'current map facts'), 'mapFingerprint': ('当前地图', 'current map'), 'orthogonallyAdjacentToWater': ('与水域正交相邻', 'orthogonally adjacent to water'), 'entityCoordinates': ('实体坐标', 'entity coordinates'), 'verifiedEntityChangesFromParent': ('已核对的版本变化', 'verified version changes'), 'designContextPatch': ('设计进度', 'design progress'), 'coordinateLinks': ('路线端点', 'route endpoints')}
_VISIBLE_LATIN_TOKEN_RE = re.compile('[A-Za-z][A-Za-z0-9_]*')
_VISIBLE_MAP_SYMBOLS = {'p': 'P', 'b': 'B', 's': 'B', 't': 'T', 'b1': 'B1', 'b2': 'B2', 't1': 'T1', 't2': 'T2'}
_VISIBLE_MAP_SYMBOL_RE = re.compile('(?<![A-Za-z0-9_])(?:b1|b2|t1|t2|p|b|s|t)(?![A-Za-z0-9_])')
_VISIBLE_CHINESE_DESIGN_TERM_REPLACEMENTS = (('\\bpeninsula\\b', '半岛'), ('\\bchoke\\s+point\\b', '瓶颈点'), ('\\bbottleneck\\b', '瓶颈点'), ('\\bplayable\\s+moment\\b', '关键操作时刻'), ('\\broute\\s+choice\\b', '路线选择'), ('\\btrade[- ]off\\b', '设计取舍'), ('\\bcorridor\\b', '通道'), ('\\breadability\\b', '可读性'), ('\\bpush\\s+order\\b', '推箱顺序'), ('\\bdeadlock\\b', '死局'), ('\\bcluster\\b', '聚集区'))

def _llm_solver_evidence(solver_metrics):
    allowed_fields = ('valid', 'solvable', 'searchedStates', 'solutionSteps', 'solutionPushes')
    return {field: solver_metrics[field] for field in allowed_fields if field in solver_metrics}

def _solver_evidence_for_prompt(solver_metrics, language):
    """Project verified metrics into the response language before prose generation."""
    evidence = _llm_solver_evidence(solver_metrics)
    if language != 'zh-CN':
        return evidence
    labels = {'valid': '地图格式有效', 'solvable': '存在可解路线', 'searchedStates': '搜索状态数', 'solutionSteps': '求解步数', 'solutionPushes': '推动次数'}
    return {labels[key]: value for key, value in evidence.items() if key in labels}

def _route_evidence_for_prompt(evidence, language):
    """Keep route evidence useful without exposing implementation-shaped keys."""
    if language != 'zh-CN':
        return evidence
    evidence = evidence or {}
    mode_labels = {'verified_route_summary': '已核对的路线概况', 'totals_only': '仅有总量信息', 'unavailable': '路线信息暂不可用'}
    result = {'路线证据状态': mode_labels.get(evidence.get('mode'), '路线信息暂不可用')}
    scalar_labels = {'solutionSteps': '求解步数', 'solutionPushes': '推动次数', 'boxAlternations': '箱子交替次数'}
    for key, label in scalar_labels.items():
        if isinstance(evidence.get(key), int):
            result[label] = evidence[key]
    if isinstance(evidence.get('pushesByBox'), dict):
        result['各箱子的推动次数'] = [{'箱子': label, '推动次数': count} for label, count in evidence['pushesByBox'].items() if isinstance(count, int)]
    if isinstance(evidence.get('pushOrder'), list):
        result['推动分段'] = [{'箱子': item.get('box'), '连续推动次数': item.get('pushes')} for item in evidence['pushOrder'] if isinstance(item, dict)]
    if isinstance(evidence.get('targetAssignments'), dict):
        result['箱子与目标的对应'] = [{'箱子': box, '目标': target} for box, target in evidence['targetAssignments'].items()]
    return result

def _proposal_clarification_route_evidence(rows, solver_metrics, stage_context=None):
    """Derive a coordinate-free witness-route summary from server-verified data."""
    metrics = solver_metrics or {}
    summary = {key: metrics[key] for key in ('solutionSteps', 'solutionPushes') if isinstance(metrics.get(key), int) and metrics.get(key) >= 0}
    solution = str(metrics.get('solution') or '').strip().upper()
    bindings = (stage_context or {}).get('entityBindings') or {}
    if not solution or bindings.get('identityStatus') != 'exact':
        return {'mode': 'totals_only', **summary} if summary else {'mode': 'unavailable'}
    entities = bindings.get('entities') or []
    player_records = [item for item in entities if item.get('kind') == 'player']
    box_records = [item for item in entities if item.get('kind') == 'box']
    target_records = [item for item in entities if item.get('kind') == 'target']
    if len(player_records) != 1 or not box_records or len(box_records) != len(target_records):
        return {'mode': 'totals_only', **summary} if summary else {'mode': 'unavailable'}
    try:
        normalized = tuple((str(row) for row in rows))
        player = (int(player_records[0]['column']) - 1, int(player_records[0]['row']) - 1)
        boxes = {(int(item['column']) - 1, int(item['row']) - 1): str(item['label']) for item in box_records}
        targets = {(int(item['column']) - 1, int(item['row']) - 1): str(item['label']) for item in target_records}
        directions = {'U': (0, -1), 'D': (0, 1), 'L': (-1, 0), 'R': (1, 0)}
        push_sequence = []
        push_counts = {str(item['label']): 0 for item in box_records}
        for move in solution:
            if move not in directions:
                raise ValueError('invalid move')
            dx, dy = directions[move]
            destination = (player[0] + dx, player[1] + dy)
            if not (0 <= destination[0] < len(normalized[0]) and 0 <= destination[1] < len(normalized)):
                raise ValueError('move outside map')
            if normalized[destination[1]][destination[0]] in {'#', ' ', '@'}:
                raise ValueError('move into blocked tile')
            if destination in boxes:
                box_destination = (destination[0] + dx, destination[1] + dy)
                if not (0 <= box_destination[0] < len(normalized[0]) and 0 <= box_destination[1] < len(normalized)):
                    raise ValueError('push outside map')
                if normalized[box_destination[1]][box_destination[0]] in {'#', ' ', '@'} or box_destination in boxes:
                    raise ValueError('invalid push')
                label = boxes.pop(destination)
                boxes[box_destination] = label
                push_sequence.append(label)
                push_counts[label] += 1
            player = destination
        if set(boxes) != set(targets):
            raise ValueError('solution does not finish on targets')
        if 'solutionSteps' in summary and summary['solutionSteps'] != len(solution):
            raise ValueError('step count mismatch')
        if 'solutionPushes' in summary and summary['solutionPushes'] != len(push_sequence):
            raise ValueError('push count mismatch')
        compressed = []
        for label in push_sequence:
            if compressed and compressed[-1]['box'] == label:
                compressed[-1]['pushes'] += 1
            else:
                compressed.append({'box': label, 'pushes': 1})
        return {'mode': 'verified_route_summary', **summary, 'pushesByBox': push_counts, 'pushOrder': compressed, 'boxAlternations': sum((left != right for left, right in zip(push_sequence, push_sequence[1:]))), 'targetAssignments': {label: targets[position] for position, label in boxes.items()}, 'analysisContext': {'authoritativeRows': list(normalized), 'legend': {'#': 'wall', '.': 'floor', '@': 'water', 'p': 'player', 's': 'box', 't': 'target', ' ': 'void'}, 'exactEntities': [{'label': str(item.get('label') or ''), 'kind': str(item.get('kind') or ''), 'row': int(item.get('row')), 'column': int(item.get('column'))} for item in entities], 'verifiedSolutionTrace': solution}}
    except (KeyError, TypeError, ValueError, IndexError):
        return {'mode': 'totals_only', **summary} if summary else {'mode': 'unavailable'}

def _map_facts_for_prompt(rows, stage_context):
    try:
        context = stage_context or {}
        facts = build_map_facts(rows, entity_bindings=context.get('entityBindings'))
        supplied = (stage_context or {}).get('mapFacts')
        if isinstance(supplied, dict) and supplied.get('tileAt') == facts.get('tileAt') and (supplied.get('mapFingerprint') == facts.get('mapFingerprint')) and (supplied.get('entityBindingFingerprint') == facts.get('entityBindingFingerprint')) and isinstance(supplied.get('verifiedEntityChangesFromParent'), dict):
            facts['verifiedEntityChangesFromParent'] = supplied['verifiedEntityChangesFromParent']
    except ValueError:
        facts = {'available': False, 'reason': 'The supplied rows are not a complete validated Stage.'}
    return json.dumps(facts, ensure_ascii=False, separators=(',', ':'))

def _stage_snapshot_for_prompt(rows, stage_context):
    """Serialize exactly one server-derived current Stage snapshot.

    ``stage_context`` is an internal compatibility envelope and may contain
    stale parent annotations.  Those annotations are intentionally ignored for
    the map payload; the current rows and the current binding are rebuilt here.
    """
    context = stage_context or {}
    try:
        snapshot = build_stage_snapshot(rows, version_id=context.get('versionId'), stage_number=context.get('stageNumber'), entity_bindings=context.get('entityBindings'))
        ordered_entities = sorted(snapshot.get('entities') or [], key=lambda item: ({'player': 0, 'box': 1, 'target': 2}.get(item.get('kind'), 9), item.get('id') or ''))
        snapshot['canonicalEntityTable'] = [f"{item.get('id')} = {item.get('kind')} at row {item.get('row')}, column {item.get('column')} (identity exact)" if item.get('identityConfidence') == 'exact' and item.get('id') else f"{item.get('kind')} at row {item.get('row')}, column {item.get('column')} (identity unknown; do not use a historical label as a hard constraint)" for item in ordered_entities]
        for collection_name in ('entities', 'boxes', 'targets'):
            for item in snapshot.get(collection_name) or []:
                if item.get('identityConfidence') != 'exact':
                    item['id'] = None
                    item['entityId'] = None
        player = snapshot.get('player')
        if isinstance(player, dict) and player.get('identityConfidence') != 'exact':
            player['id'] = None
            player['entityId'] = None
        claim_check = context.get('userMapClaims')
        if isinstance(claim_check, dict):
            snapshot['userMapClaimCheck'] = {'conflicts': [{key: value for key, value in item.items() if key != 'sourceText'} for item in claim_check.get('conflicts', []) if isinstance(item, dict)], 'instruction': 'Correct conflicting present-tense user map claims in ordinary prose; do not treat them as design constraints or proposal coordinates.'}
    except (TypeError, ValueError):
        return json.dumps({'available': False, 'reason': 'The supplied current Stage is not structurally complete.'}, ensure_ascii=False, separators=(',', ':'))
    return json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))

def _map_grounding_contract():
    return 'Map-grounding rule: Deterministic Map Facts are authoritative. Use B1/B2, T1/T2, P, or one-based (row,column) when identifying a particular entity. Do not invent a current entity position or relation from a vague visual impression. In particular, do not call a box/player/target upper-right, lower-left, near another entity, beside water, or in a narrow passage unless the facts explicitly support that exact current claim. gridDistance is Manhattan distance only, not a traversable route or a push solution. If the facts do not establish a relation, state it as a question about a future play moment rather than as a fact about the saved map. The JSON field names in Deterministic Map Facts are prompt-only; never repeat them in assistantMessage, assessment, guidance, or any other user-visible text.'

def _has_historical_stage_reference(text):
    lowered = str(text or '').casefold()
    return any((marker in lowered for marker in ('第一版', '原版', '之前的版本', '上一版', '旧版本', '旧版', '之前的stage', '上一个stage', 'first version', 'original version', 'previous version', 'prior version', 'earlier version', 'previous stage', 'prior stage', 'earlier stage')))

def _historical_stage_reference_instruction(conversation):
    if not _has_historical_stage_reference(_latest_user_text(conversation)):
        return ''
    return 'Historical Stage reference rule: the designer is referring to an older version. The numbered map and Deterministic Map Facts describe only the current saved Stage. Discuss the older version conceptually unless a historical snapshot is explicitly supplied; do not invent, copy, or transfer its coordinates or entity positions into current-map claims, and do not treat that reflection as an edit authorization.'

def _contains_unverified_historical_map_claim(text):
    coordinate = re.compile('[\\(\\uFF08]\\s*\\d{1,2}\\s*[,\\uFF0C]\\s*\\d{1,2}\\s*[\\)\\uFF09]')
    entity_position = re.compile('\\b(?:P|B\\d+|T\\d+)\\b.{0,28}?(?:at|in|on|located|position|coordinate)|(?:玩家|箱子|目标|起点|B\\d+|T\\d+|P).{0,18}?(?:位于|坐标|在)', flags=re.IGNORECASE)
    for sentence in re.split('(?<=[.!?。！？])\\s*', str(text or '')):
        if _has_historical_stage_reference(sentence) and (coordinate.search(sentence) or entity_position.search(sentence)):
            return True
    return False

def _validate_named_entity_relations(text, facts):
    """Validate only explicit present-tense relations involving named entities."""
    records = {item.get('id'): item for item in facts.get('entities') or [] if item.get('id') and item.get('identityConfidence') == 'exact'}
    if not records:
        return

    def point(label):
        item = records.get(label)
        return (item.get('row'), item.get('column')) if item else None
    label = '(?<![A-Za-z0-9])(?:P|B\\d+|T\\d+)(?![A-Za-z0-9])'
    relation = '(?:is\\s+|are\\s+)?(?:near|close\\s+to|adjacent\\s+to|beside|next\\s+to)|靠近|相邻|在.{0,4}旁边|旁边|紧挨'
    pair_pattern = re.compile(f'(?P<first>{label})\\s*(?:与|和|跟)?\\s*(?:{relation})\\s*(?P<second>{label})', flags=re.IGNORECASE)
    box_water_patterns = (re.compile(f'(?P<box>\\bB\\d+\\b)\\s*(?:is\\s+|在)?(?:adjacent\\s+to|beside|next\\s+to|相邻|紧挨|紧贴|贴着|靠着|在.{{0,4}}旁边)\\s*(?:the\\s+)?(?:water|水域|水边|水)', flags=re.IGNORECASE), re.compile(f'(?:the\\s+)?(?:water|水域|水边|水)\\s*(?:is\\s+|在)?(?:adjacent\\s+to|beside|next\\s+to|相邻|紧挨|紧贴|贴着|靠着|在.{{0,4}}旁边)\\s*(?P<box>\\bB\\d+\\b)', flags=re.IGNORECASE))
    clauses = re.split('[,，;；.!?。！？\\r\\n]+', str(text or ''))
    for clause in clauses:
        if not clause.strip() or _entity_claim_is_non_current(clause):
            continue
        for match in pair_pattern.finditer(clause):
            first = match.group('first').upper()
            second = match.group('second').upper()
            first_point, second_point = (point(first), point(second))
            if first_point and second_point:
                distance = abs(first_point[0] - second_point[0]) + abs(first_point[1] - second_point[1])
                if distance > 2:
                    raise ValueError(f'The reply claims {first} is close to {second}, but the saved map does not support that relation.')
        for pattern in box_water_patterns:
            for match in pattern.finditer(clause):
                box_label = match.group('box').upper()
                item = records.get(box_label)
                if item and item.get('kind') == 'box':
                    adjacent = any((cell['row'] == item['row'] + row_delta and cell['column'] == item['column'] + column_delta for cell in facts.get('waterCells') or [] for row_delta, column_delta in ((-1, 0), (1, 0), (0, -1), (0, 1))))
                    if not adjacent:
                        raise ValueError(f'The reply claims {box_label} is beside water, but the saved map does not support that relation.')

def _validate_map_grounding_texts(texts, rows, *, historical_reference=False, entity_bindings=None):
    memo = _GROUNDING_MEMO.get()
    key = json.dumps([texts, rows, historical_reference, entity_bindings], sort_keys=True, ensure_ascii=False)
    if memo is not None and key in memo['passed']:
        memo['hits'] += 1
        return
    _check_map_grounding_texts(texts, rows, historical_reference=historical_reference, entity_bindings=entity_bindings)
    if memo is not None:
        memo['checks'] += 1
        if len(memo['passed']) < 256:
            memo['passed'].add(key)

def _check_map_grounding_texts(texts, rows, *, historical_reference=False, entity_bindings=None):
    """Reject a small set of high-impact, checkable spatial hallucinations.

    Natural prose is intentionally not parsed wholesale.  This guard covers the claims that
    most often made the visible feedback misleading: assigning an entity to a wrong corner,
    saying that a box currently touches water, and saying that a current box/player is close
    to a target when no such pair exists.  The prompt remains the primary grounding layer.
    """
    text = '\n'.join((str(value or '') for value in texts)).casefold()
    if not text:
        return
    if historical_reference and _contains_unverified_historical_map_claim(text):
        raise ValueError('The reply makes a concrete historical Stage map claim without a supplied historical snapshot.')
    _validate_coordinate_claims(text, rows, entity_bindings=entity_bindings)
    try:
        facts = build_map_facts(rows, entity_bindings=entity_bindings)
    except ValueError:
        return
    _validate_named_entity_relations(text, facts)
    height = len(rows)
    width = len(rows[0]) if rows else 0
    entities = {'box': facts['boxes'], 'target': facts['targets'], 'player': [facts['player']]}
    regions = (('upper_right', ('右上角', '右上', 'upper-right', 'upper right'), lambda item: item['row'] <= (height + 1) // 2 and item['column'] > width // 2), ('upper_left', ('左上角', '左上', 'upper-left', 'upper left'), lambda item: item['row'] <= (height + 1) // 2 and item['column'] <= width // 2), ('lower_right', ('右下角', '右下', 'lower-right', 'lower right'), lambda item: item['row'] > (height + 1) // 2 and item['column'] > width // 2), ('lower_left', ('左下角', '左下', 'lower-left', 'lower left'), lambda item: item['row'] > (height + 1) // 2 and item['column'] <= width // 2))
    entity_patterns = {'box': '(?:箱子|箱|box(?:es)?|crates?)', 'target': '(?:目标点|目标|终点|targets?|goals?)', 'player': '(?:玩家起点|玩家|起点|players?)'}
    for entity_name, pattern in entity_patterns.items():
        for _, labels, in_region in regions:
            if any((in_region(item) for item in entities[entity_name])):
                continue
            for label in labels:
                escaped = re.escape(label)
                before = f'{escaped}(?:的)?(?:那(?:个)?|这(?:个)?|一(?:个)?)?\\s*{pattern}'
                after = f'{pattern}(?:在|位于|处在|挪到(?:了)?|移到(?:了)?|到了|in|at|is in)?\\s*{escaped}'
                if re.search(before, text) or re.search(after, text):
                    raise ValueError(f'The reply assigns a {entity_name} to {label}, which conflicts with deterministic map facts.')
    if not any((box['orthogonallyAdjacentToWater'] for box in facts['boxes'])):
        current_box_water_patterns = ('(?:现在|当前).{0,18}(?:箱子|箱|它).{0,12}(?:贴着|紧贴|紧邻|挨着|靠着)(?:水|水边|水域)', '(?:箱子|箱).{0,16}(?:贴着|紧贴|紧邻|挨着|靠着)(?:水|水边|水域)', '(?:box(?:es)?|crate).{0,28}(?:next to|adjacent to|beside|against).{0,18}water')
        if any((re.search(pattern, text) for pattern in current_box_water_patterns)):
            raise ValueError('The reply claims that a current box touches water, which conflicts with deterministic map facts.')
    target_positions = facts['targets']
    close_box_target = any((abs(box['row'] - target['row']) + abs(box['column'] - target['column']) <= 2 for box in facts['boxes'] for target in target_positions))
    close_player_target = any((abs(facts['player']['row'] - target['row']) + abs(facts['player']['column'] - target['column']) <= 2 for target in target_positions))
    closeness = '(?:很近|靠近|紧挨|相邻|close\\s+to|near|adjacent\\s+to)'
    box_target_claim = re.search(f'(?:箱子|箱|boxes?|crates?)\\s*(?:与|和|跟|is\\s+|are\\s+)?{closeness}\\s*(?:目标点|目标|终点|targets?|goals?)|(?:目标点|目标|终点|targets?|goals?)\\s*(?:与|和|跟|is\\s+|are\\s+)?{closeness}\\s*(?:箱子|箱|boxes?|crates?)', text)
    if not close_box_target and box_target_claim:
        raise ValueError('The reply claims that a current box is close to a target, which conflicts with deterministic map facts.')
    player_target_claim = re.search(f'(?:玩家|起点|players?)\\s*(?:与|和|跟|is\\s+|are\\s+)?{closeness}\\s*(?:目标点|目标|终点|targets?|goals?)|(?:目标点|目标|终点|targets?|goals?)\\s*(?:与|和|跟|is\\s+|are\\s+)?{closeness}\\s*(?:玩家|起点|players?)', text)
    if not close_player_target and player_target_claim:
        raise ValueError('The reply claims that the current player is close to a target, which conflicts with deterministic map facts.')

def _strip_invalid_stage_grounding_sentences(text, rows, *, historical_reference=False, entity_bindings=None):
    """Drop only checkable map-claim sentences that contradict this Stage.

    Stage openings are explanatory prose, not execution contracts.  A single
    hallucinated coordinate should therefore not erase every useful observation
    in a fallback response.  Formal proposals continue to use the strict whole-
    payload validation paths.
    """
    if not text or not rows:
        return (str(text or '').strip(), [])
    sentences = re.split('\\n{2,}|(?<=[.!?。！？])(?:[ \\t]+|(?=[\\u3400-\\u9fff]))', str(text))
    kept = []
    removed = []
    missing_premise = False
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if missing_premise and re.match('^(?:因此|所以|因而|由此|这样|这就|这也|这意味着|基于上述|据此|therefore\\b|thus\\b|hence\\b|consequently\\b|as a result\\b|this (?:means|makes|would)|that (?:means|makes))', sentence, flags=re.IGNORECASE):
            removed.append({'text': sentence, 'reason': 'Its map premise was rejected.'})
            continue
        try:
            _validate_map_grounding_texts([sentence], rows, historical_reference=historical_reference, entity_bindings=entity_bindings)
        except ValueError as exception:
            removed.append({'text': sentence, 'reason': str(exception)[:300]})
            missing_premise = True
            continue
        kept.append(sentence)
        missing_premise = False
    if not removed:
        return (str(text or '').strip(), removed)
    return ('\n\n'.join(kept).strip(), removed)

def _strip_invalid_grounding_sentences(text, rows, *, historical_reference=False, entity_bindings=None):
    """Shared sentence-level recovery for non-executable visible replies."""
    return _strip_invalid_stage_grounding_sentences(text, rows, historical_reference=historical_reference, entity_bindings=entity_bindings)

def _sanitize_ordinary_grounding_metadata(guidance, rows, *, historical_reference=False, entity_bindings=None):
    """Keep a useful ordinary reply when optional model metadata is not grounded.

    A normal design discussion is not an edit contract. Its visible body is validated on
    its own, while optional cards/questions are discarded individually when they contain a
    stale coordinate or spatial claim. This prevents one bad Kimi tag from replacing the
    whole conversation with an edit-oriented clarification.
    """
    sanitized = dict(guidance or {})

    def valid_text(value):
        if not value:
            return True
        try:
            _validate_map_grounding_texts([value], rows, historical_reference=historical_reference, entity_bindings=entity_bindings)
        except ValueError:
            return False
        return True
    for field in ('followUpQuestion', 'intentHypothesis'):
        if not valid_text(sanitized.get(field)):
            sanitized[field] = None
    offer = sanitized.get('proposalOffer')
    if isinstance(offer, dict):
        offer_values = [offer.get('summary'), offer.get('rationale')]
        execution_brief = offer.get('executionBrief')
        if execution_brief:
            offer_values.append(json.dumps(execution_brief, ensure_ascii=False))
        if not all((valid_text(value) for value in offer_values)):
            sanitized['proposalOffer'] = None
            if sanitized.get('move') == 'offer_revision':
                sanitized['move'] = 'offer_perspective'
    disagreement = sanitized.get('disagreement')
    if isinstance(disagreement, dict):
        fields = ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion')
        if not all((valid_text(disagreement.get(field)) for field in fields)):
            sanitized['disagreement'] = None
    cues = []
    for cue in sanitized.get('uiCues') or []:
        if not isinstance(cue, dict) or valid_text(cue.get('text')):
            cues.append(cue)
    sanitized['uiCues'] = cues[:2]
    return sanitized

def _chat_validation_mode(conversation, stage_context=None, *, stage_opening=False, guidance_mode=None):
    """Classify how strictly current-map claims should affect a chat response."""
    if stage_opening:
        return 'stage_opening'
    context = stage_context or {}
    explicit_action = context.get('explicitAction') or 'none'
    if explicit_action in {'execute_revision', 'challenge_revision', 'alternative_revision'}:
        return 'edit_request'
    if guidance_mode == 'revision_advice':
        return 'edit_request'
    if guidance_mode in {'needs_clarification', 'disagreement'}:
        return 'edit_request'
    if context.get('revisionRequestState') in {'authorized', 'authorized_relaxed', 'needs_direction'}:
        return 'edit_request'
    latest = _latest_user_text(conversation).casefold()
    route_markers = ('route', 'path', 'corridor', '路线', '路径', '通道', '绕路', '推到', '移动', '从', '到')
    if any((marker in latest for marker in route_markers)):
        return 'route_discussion'
    return 'ordinary_chat'

def _tile_from_claim(value):
    value = str(value or '').casefold()
    return {'#': '#', '墙': '#', '墙体': '#', 'wall': '#', '.': '.', '地板': '.', '空地': '.', 'floor': '.', 'ground': '.', '@': '@', '水': '@', '水域': '@', 'water': '@', 'p': 'p', '玩家': 'p', '起点': 'p', 'player': 'p', 'start': 'p', 's': 's', '箱': 's', '箱子': 's', 'box': 's', 'crate': 's', 't': 't', '目标': 't', '目标点': 't', 'target': 't', 'goal': 't'}.get(value)

def _validate_coordinate_claims(text, rows, *, entity_bindings=None):
    """Check explicit coordinate/tile statements before they reach a proposal card."""
    if not rows:
        return
    claim_pattern = re.compile('\\(\\s*(\\d+)\\s*[,，]\\s*(\\d+)\\s*\\)\\s*(?:是|为|属于|is|was|contains|contains the|has)\\s*(墙体?|墙|地板|空地|水域?|水|箱子?|目标点?|玩家|起点|wall|floor|ground|water|box|crate|target|goal|player|start|#|\\.|@|p|s|t)', flags=re.IGNORECASE)
    transition_pattern = re.compile('\\(\\s*(\\d+)\\s*[,，]\\s*(\\d+)\\s*\\)\\s*.{0,18}?(?:从|由|from)\\s*(墙体?|墙|地板|空地|水域?|水|箱子?|目标点?|玩家|起点|wall|floor|ground|water|box|crate|target|goal|player|start|#|\\.|@|p|s|t)\\s*(?:变成|改成|换成|变为|to|into|becomes?)\\s*(墙体?|墙|地板|空地|水域?|水|箱子?|目标点?|玩家|起点|wall|floor|ground|water|box|crate|target|goal|player|start|#|\\.|@|p|s|t)', flags=re.IGNORECASE)
    height = len(rows)
    neighbor_prefix = re.compile(r'(?:\badjacent\s+to|\bnext\s+to|\bclose\s+to|\bbeside|\bnear|靠近|相邻|紧挨|旁边)\s*$', flags=re.IGNORECASE)
    for match in claim_pattern.finditer(text):
        if _entity_claim_is_non_current(text, match.start(), match.end()):
            continue
        if neighbor_prefix.search(text[max(0, match.start() - 24):match.start()]):
            continue
        row, column = (int(match.group(1)), int(match.group(2)))
        expected = _tile_from_claim(match.group(3))
        if not (1 <= row <= height and 1 <= column <= len(rows[row - 1])):
            raise ValueError('The reply refers to a coordinate outside the saved Stage.')
        actual = rows[row - 1][column - 1]
        if expected is not None and actual != expected:
            raise ValueError(f'The reply claims row {row}, column {column} is {expected!r}, but deterministic map facts say it is {actual!r}.')
    for match in transition_pattern.finditer(text):
        row, column = (int(match.group(1)), int(match.group(2)))
        before = _tile_from_claim(match.group(3))
        after = _tile_from_claim(match.group(4))
        if not (1 <= row <= height and 1 <= column <= len(rows[row - 1])):
            raise ValueError('The reply refers to a coordinate outside the saved Stage.')
        actual = rows[row - 1][column - 1]
        if before is not None and actual != before:
            raise ValueError(f'The reply claims row {row}, column {column} starts as {before!r}, but deterministic map facts say it is {actual!r}.')
    _validate_entity_coordinate_claims(text, rows, entity_bindings=entity_bindings)

def _map_entity_coordinates(rows, entity_bindings=None):
    """Return the stable entity labels used by the map-facts contract."""
    facts = build_map_facts(rows, entity_bindings=entity_bindings)
    result = {item['id']: item for item in facts.get('entities') or []}
    if 'P' not in result:
        result['P'] = facts['player']
    return {key: {'row': value['row'], 'column': value['column']} for key, value in result.items()}

def _entity_claim_is_non_current(text, start=0, end=None):
    """Ignore a future or hypothetical claim within its own clause only."""
    value = str(text or '')
    lookbehind_start = max(0, start - 32)
    prefix = value[lookbehind_start:start]
    delimiter = re.search('[,，;；.!?。！？\\r\\n](?!.*[,，;；.!?。！？\\r\\n])', prefix)
    if delimiter is not None:
        lookbehind_start += delimiter.end()
    window = value[lookbehind_start:end]
    return bool(re.search('(?:\\bif\\b|\\bwhen\\b|\\bwould\\b|\\bwill\\b|\\bmove(?:s|d|ing)?\\b|\\bpush(?:es|ed|ing)?\\b|\\bto\\b|\\btoward(?:s)?\\b|\\bfrom\\b|\\u5982\\u679c|\\u82e5|\\u5047\\u8bbe|\\u5c06|\\u4f1a|\\u79fb\\u52a8|\\u63a8\\u5230|\\u63a8\\u5411|\\u6539\\u5230|\\u53d8\\u6210)', window, flags=re.IGNORECASE))

def _entity_coordinate_claims(text, rows):
    """Yield only explicit entity-to-coordinate claims inside one clause.

    Do not infer ownership from proximity. In particular, a coordinate before a
    semicolon or another entity must never be attached to that later entity.
    """
    entity = '(?<![A-Za-z0-9])(?:P|B\\d+|T\\d+|玩家|起点)(?![A-Za-z0-9])'
    coordinate = '(?:[\\(\\uFF08]\\s*(?P<row_paren>\\d{1,2})\\s*[,，]\\s*(?P<column_paren>\\d{1,2})\\s*[\\)\\uFF09]|(?:row|line)\\s*(?P<row_en>\\d{1,2})\\s*,?\\s*(?:column|col)\\s*(?P<column_en>\\d{1,2})|第\\s*(?P<row_zh>\\d{1,2})\\s*行\\s*第\\s*(?P<column_zh>\\d{1,2})\\s*列)'
    forward = re.compile(f"(?P<entity>{entity})\\s*(?:当前|目前|currently\\s+)?(?:(?:的)?坐标\\s*(?:是|为|位于|:|：)?|(?:'s\\s+)?coordinates?\\s*(?:is|are|:)?|位于|在|处于|坐落于|起点为|is\\s+(?:currently\\s+)?(?:at|in|on)|(?:is\\s+)?located\\s+(?:at|in|on)|sits?\\s+(?:at|in|on)|occup(?:y|ies|ied)|positioned\\s+(?:at|in|on)|placed\\s+(?:at|in|on))\\s*{coordinate}", flags=re.IGNORECASE)
    reverse = re.compile(f'{coordinate}\\s*(?:的位置|这个位置|该位置|the\\s+(?:cell|position)\\s+)?(?:是|为|属于|is|contains?)\\s*(?P<entity>{entity})', flags=re.IGNORECASE)

    def coordinates(match):
        for suffix in ('paren', 'en', 'zh'):
            row = match.groupdict().get(f'row_{suffix}')
            column = match.groupdict().get(f'column_{suffix}')
            if row is not None and column is not None:
                return (int(row), int(column))
        raise ValueError('An entity coordinate claim could not be parsed.')
    seen_claims = set()
    protected = re.sub('(?<=\\d)[,，](?=\\s*\\d)', '\x00', str(text or ''))
    protected = re.sub('((?:row|line)\\s*\\d{1,2})\\s*,\\s*((?:column|col)\\b)', lambda match: f'{match.group(1)}\x00 {match.group(2)}', protected, flags=re.IGNORECASE)
    clauses = (clause.replace('\x00', ',') for clause in re.split('[,，;；.!?。！？\\r\\n]+', protected))
    for clause in clauses:
        clause = clause.strip()
        if not clause or _entity_claim_is_non_current(clause):
            continue
        for pattern in (forward, reverse):
            for match in pattern.finditer(clause):
                row, column = coordinates(match)
                entity_name = match.group('entity').upper()
                if entity_name in {'玩家', '起点'}:
                    entity_name = 'P'
                claim = (entity_name, row, column)
                if claim in seen_claims:
                    continue
                seen_claims.add(claim)
                yield (claim[0], {'row': claim[1], 'column': claim[2]})

def _validate_entity_coordinate_claims(text, rows, *, entity_bindings=None):
    try:
        expected = _map_entity_coordinates(rows, entity_bindings)
        facts = build_map_facts(rows, entity_bindings=entity_bindings)
        uncertain = {item.get('id') for item in facts.get('entities') or [] if item.get('id') and item.get('identityConfidence') != 'exact'}
    except (TypeError, ValueError, KeyError):
        return
    for entity_name, claimed in _entity_coordinate_claims(text, rows):
        if entity_name in {'玩家', '起点'}:
            entity_name = 'P'
        if entity_name in uncertain:
            raise ValueError(f'The saved Stage cannot verify the identity of {entity_name}; use a coordinate instead.')
        actual = expected.get(entity_name)
        if actual is None:
            raise ValueError(f'The reply refers to an unknown map entity {entity_name}.')
        if actual != claimed:
            raise ValueError(f"The reply places {entity_name} at row {claimed['row']}, column {claimed['column']}, but deterministic map facts place it at row {actual['row']}, column {actual['column']}.")

def _snapshot_entity_records(rows, stage_context=None):
    try:
        snapshot = build_stage_snapshot(rows, version_id=(stage_context or {}).get('versionId'), stage_number=(stage_context or {}).get('stageNumber'), entity_bindings=(stage_context or {}).get('entityBindings'))
    except (TypeError, ValueError):
        return {}
    return {item.get('id'): item for item in snapshot.get('entities') or [] if item.get('id') and item.get('identityConfidence') == 'exact'}

def _server_fact_text(block, rows, stage_context, language, records):
    fact_type = str(block.get('factType') or '').strip()
    if fact_type == 'entity_position':
        item = records.get(str(block.get('entity') or '').upper())
        if not item:
            return None
        if language == 'zh-CN':
            return f"{item['id']}当前位于第{item['row']}行第{item['column']}列。"
        return f"{item['id']} is currently at row {item['row']}, column {item['column']}."
    if fact_type == 'tile_state':
        row = block.get('row')
        column = block.get('column')
        if not isinstance(row, int) or not isinstance(column, int):
            return None
        if not rows or not (1 <= row <= len(rows) and 1 <= column <= len(rows[row - 1])):
            return None
        tile = rows[row - 1][column - 1]
        labels = {'#': ('墙体', 'a wall'), '.': ('地板', 'floor'), '@': ('水域', 'water'), 'w': ('水域', 'water'), 'p': ('玩家', 'the player'), 's': ('箱子', 'a box'), 't': ('目标', 'a target')}
        label = labels.get(tile)
        if not label:
            return None
        if language == 'zh-CN':
            return f'第{row}行第{column}列是{label[0]}。'
        return f'Row {row}, column {column} is {label[1]}.'
    return None

def _render_content_blocks(content_blocks, fallback_message, rows, stage_context, language):
    """Render only server-verified map facts; preserve model design analysis.

    A block is optional for compatibility.  Untrusted analysis remains useful,
    but direct current-map claims are removed unless they arrive as a factRef.
    """
    if not isinstance(content_blocks, list):
        return (_strip_unreferenced_current_map_claims(fallback_message), [])
    records = _snapshot_entity_records(rows, stage_context)
    rendered = []
    route_links = []
    for block in content_blocks[:12]:
        if not isinstance(block, dict):
            continue
        kind = str(block.get('kind') or '').strip()
        if kind == 'factRef':
            text = _server_fact_text(block, rows, stage_context, language, records)
            if text:
                rendered.append(text)
            continue
        text = str(block.get('text') or '').strip()
        if not text or len(text) > CHAT_RESPONSE_HARD_LENGTH:
            continue
        text = _strip_unreferenced_current_map_claims(text)
        if not text:
            continue
        if kind == 'routeReasoning':
            source = records.get(str(block.get('fromEntity') or '').upper())
            destination = records.get(str(block.get('toEntity') or '').upper())
            if not source or not destination:
                continue
            prefix = f"{source['id']} → {destination['id']}：" if language == 'zh-CN' else f"{source['id']} → {destination['id']}: "
            rendered_text = f'{prefix}{text}'
            candidate = {'text': rendered_text, 'from': {'row': source['row'], 'column': source['column']}, 'to': {'row': destination['row'], 'column': destination['column']}}
            if _coordinate_link_is_grounded(candidate, rows, (stage_context or {}).get('entityBindings')):
                rendered.append(rendered_text)
                route_links.append(candidate)
            continue
        if kind in {'analysis', 'personalReflection'}:
            rendered.append(text)
    if not rendered:
        return (_strip_unreferenced_current_map_claims(fallback_message), [])
    return ('\n\n'.join(rendered), route_links)

def _sanitize_assessment_grounding(assessment, rows, stage_context, language, historical_reference):
    """Drop only ungrounded archival assessment fragments, never the whole reply."""
    if not assessment or not rows:
        return assessment
    bindings = (stage_context or {}).get('entityBindings')
    fallback = '这个当前 Stage 仍然可以从玩家的第一个选择继续观察。' if language == 'zh-CN' else "This current Stage can still be observed through the player's first meaningful choice."
    result = dict(assessment)
    for key in ('solutionSummary', 'difficultyOpinion'):
        value = str(result.get(key) or '').strip()
        if not value:
            result[key] = fallback
            continue
        cleaned, _removed = _strip_invalid_stage_grounding_sentences(value, rows, historical_reference=historical_reference, entity_bindings=bindings)
        result[key] = cleaned or fallback
    for key in ('features', 'suggestions'):
        kept = []
        for item in result.get(key) or []:
            cleaned, _removed = _strip_invalid_stage_grounding_sentences(item, rows, historical_reference=historical_reference, entity_bindings=bindings)
            if cleaned:
                kept.append(cleaned)
        result[key] = kept or [fallback]
    question = result.get('satisfactionQuestion')
    if question:
        cleaned, _removed = _strip_invalid_stage_grounding_sentences(question, rows, historical_reference=historical_reference, entity_bindings=bindings)
        result['satisfactionQuestion'] = cleaned or None
    return result

def _normalize_single_level_language(value):
    """Keep model prose aligned with the one-level, multi-version study structure."""
    if value is None:
        return None
    text = str(value)
    chinese_ordinal = '[零〇一二三四五六七八九十百两\\d]+'
    text = re.sub(f'第{chinese_ordinal}关(?:卡)?该有的', '这个版本现在呈现出的', text)
    text = re.sub('(?:后面|之后|后续|未来)(?:的)?关卡', '后续版本', text)
    text = re.sub('(?:下一个|下一)关(?:卡)?', '下一个版本', text)
    text = re.sub('(?:上一个|上一)关(?:卡)?', '上一个版本', text)
    text = re.sub('前面(?:的)?关卡', '之前的版本', text)
    text = re.sub(f'第{chinese_ordinal}关(?:卡)?', '这个 Stage', text)
    english_ordinal = '(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\\d+(?:st|nd|rd|th))'
    text = re.sub(f'\\b(?:the\\s+)?{english_ordinal}\\s+levels?\\b', 'this Stage', text, flags=re.IGNORECASE)
    text = re.sub('\\b(?:the\\s+)?(?:next|following)\\s+level\\b', 'the next version', text, flags=re.IGNORECASE)
    text = re.sub('\\b(?:later|future|subsequent|following)\\s+levels\\b', 'later versions', text, flags=re.IGNORECASE)
    text = re.sub('\\b(?:the\\s+)?previous\\s+level\\b', 'the previous version', text, flags=re.IGNORECASE)
    return text

def _sanitize_visible_model_text(value, language='en'):
    """Remove implementation vocabulary that must never become visible prose."""
    if value is None:
        return None
    text = str(value)
    text = re.sub('```(?:json|text|plaintext)?\\s*', '', text, flags=re.IGNORECASE)
    text = re.sub('`([^`\\r\\n]+)`', '\\1', text)
    language_index = 0 if language == 'zh-CN' else 1
    for token, replacements in _VISIBLE_INTERNAL_TERM_REPLACEMENTS.items():
        replacement = replacements[language_index]
        text = re.sub(f'(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])', replacement, text)
    if language == 'zh-CN':
        for pattern, replacement in _VISIBLE_CHINESE_DESIGN_TERM_REPLACEMENTS:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        text = _VISIBLE_MAP_SYMBOL_RE.sub(lambda match: _VISIBLE_MAP_SYMBOLS[match.group(0)], text)
    text = re.sub('[ \\t]{2,}', ' ', text)
    text = re.sub('\\n[ \\t]+', '\n', text)
    return text.strip()

def repair_legacy_visible_text(value, language='en'):
    """Read-time compatibility repair for immutable historical assistant prose."""
    text = _sanitize_visible_model_text(value, language)
    if language != 'zh-CN':
        return text
    paragraphs = []
    for paragraph in str(text or '').split('\n\n'):
        if not any((_visible_internal_identifier(token) for token in _VISIBLE_LATIN_TOKEN_RE.findall(paragraph))):
            paragraphs.append(paragraph)
            continue
        kept = [sentence for sentence in _content_block_sentences(paragraph) if not any((_visible_internal_identifier(token) for token in _VISIBLE_LATIN_TOKEN_RE.findall(sentence)))]
        if kept:
            paragraphs.append(''.join(kept))
    return '\n\n'.join(paragraphs)

def challenge_choice_card_copy(user_reason, proposal_summary, language='en', *, legacy=False):
    """Explain the actual choice without inventing an AI objection or proposal."""
    reason = re.sub('\\s+', ' ', str(user_reason or '')).strip().rstrip('.。')[:220]
    original = re.sub('\\s+', ' ', str(proposal_summary or '')).strip().rstrip('.。')[:220]
    if language == 'zh-CN':
        if legacy:
            return {'aiPosition': f'我原先提出的是：{original}。' if original else '我原先提出了另一项局部修改。', 'coreDisagreement': f'你现在希望：{reason}。原方案是：{original}。两者的修改范围不同；此处还没有改变地图。请先确认采用哪个方向，再逐格核对随后出现的待审查提案。', 'nextQuestion': '你是否仍选择原方案的方向？回答“是”或“否”；之后请核对待审查提案的具体改动。'}
        return {'aiPosition': f'我原先提出的是：{original}。' if original else '我原先提出了另一项局部修改。', 'coreDisagreement': f'原方案：{original}。保留它会重验原来的格子；不保留则按你的理由另拟方案。两种结果都需你审查后才会保存。', 'nextQuestion': '是否沿用原方案？“是”保留原方案，“否”按你的理由生成新方案。'}
    if legacy:
        return {'aiPosition': f'My original proposal was: {original}.' if original else 'I proposed a different local change.', 'coreDisagreement': f'You now want: {reason}. The original proposal was: {original}. The map has not changed. Choose a direction, then inspect every tile in the pending proposal.', 'nextQuestion': 'Do you still choose the original direction? Answer yes or no, then review the pending changes.'}
    return {'aiPosition': f'My original proposal was: {original}.' if original else 'I proposed a different local change.', 'coreDisagreement': f'Original proposal: {original}. Yes revalidates its original tiles; no prepares a new proposal from your reason. Either result requires your review before saving.', 'nextQuestion': 'Keep the original proposal? Yes keeps it; no plans a new one from your reason.'}

def _visible_chinese_language_issue(value):
    """Return a safe diagnostic when Chinese visible prose still code-switches."""
    text = str(value or '')
    if not text:
        return None
    tokens = [token for token in _VISIBLE_LATIN_TOKEN_RE.findall(text) if token not in _VISIBLE_CHINESE_LATIN_ALLOWLIST]
    if not tokens:
        return None
    internal = next((token for token in tokens if _visible_internal_identifier(token)), None)
    chinese_chars = len(re.findall('[\\u3400-\\u9fff]', text))
    if internal is None and len(tokens) <= 6 and (chinese_chars >= len(''.join(tokens))):
        return None
    token = internal or tokens[0]
    kind = 'implementation identifier' if '_' in token or re.search('[a-z][A-Z]|[A-Z][a-z]+[A-Z]', token) else 'English term'
    return f'Chinese visible text contains {kind}: {token}'

def _visible_guidance_texts(guidance):
    """Yield only user-facing strings; never inspect backend metadata values."""
    guidance = guidance or {}
    for field_name in ('intentHypothesis', 'followUpQuestion'):
        yield guidance.get(field_name)
    offer = guidance.get('proposalOffer') or {}
    if isinstance(offer, dict):
        yield offer.get('summary')
        yield offer.get('rationale')
        presentation = offer.get('proposalPresentation') or {}
        if isinstance(presentation, dict):
            yield presentation.get('summary')
            yield presentation.get('rationale')
    disagreement = guidance.get('disagreement') or {}
    if isinstance(disagreement, dict) and disagreement.get('displayCard', True) is not False:
        for field_name in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion'):
            yield disagreement.get(field_name)
    for cue in guidance.get('uiCues') or []:
        if isinstance(cue, dict):
            yield cue.get('text')
    for link in guidance.get('coordinateLinks') or []:
        if isinstance(link, dict):
            yield link.get('text')

def _assert_visible_output_language(language, *values, guidance=None, assessment=None):
    """Reject a model response before persistence when Chinese prose is mixed."""
    if language != 'zh-CN':
        return
    candidates = list(values)
    candidates.extend(_visible_guidance_texts(guidance))
    if isinstance(assessment, dict):
        for value in assessment.values():
            candidates.extend(value if isinstance(value, list) else [value])
    for value in candidates:
        issue = _visible_chinese_language_issue(value)
        if issue:
            _log_llm_event('visible_language_violation', language=language, violationKind='implementation_identifier' if 'implementation identifier' in issue else 'english_term')
            raise ValueError(issue)

def _isolate_optional_language_components(guidance, language, *, required_question=False):
    """Optional display failures cannot revoke a reliable body or required state."""
    result = _sanitize_visible_guidance(guidance, language)
    if language != 'zh-CN':
        return result
    conflict = (result.get('_intentCandidateReview') or {}).get('relation') == 'conflict'
    if not conflict and _visible_chinese_language_issue(result.get('intentHypothesis')):
        result['intentHypothesis'] = None
        result['intentConfidence'] = None
        for field in ('_intentDecision', '_intentSemanticClaims', '_intentCandidateReview'):
            result.pop(field, None)
        _log_llm_event('llm_component_omitted', component='intent_card', failureCode='VISIBLE_LANGUAGE_INVALID')
    if not required_question and _visible_chinese_language_issue(result.get('followUpQuestion')):
        result['followUpQuestion'] = None
    required_cues = result.get('move') == 'challenge_tradeoff' or bool(result.get('disagreement'))
    if not required_cues:
        result['uiCues'] = [cue for cue in result.get('uiCues') or [] if not _visible_chinese_language_issue(cue.get('text'))]
    result['coordinateLinks'] = [link for link in result.get('coordinateLinks') or [] if not _visible_chinese_language_issue(link.get('text'))]
    return result

def _sanitize_visible_guidance(guidance, language='en'):
    """Sanitize public guidance while preserving backend-only context patches."""
    result = dict(guidance or {})
    for field_name in ('intentHypothesis', 'followUpQuestion'):
        if result.get(field_name) is not None:
            result[field_name] = _sanitize_visible_model_text(result[field_name], language)
    offer = result.get('proposalOffer')
    if isinstance(offer, dict):
        offer = dict(offer)
        if offer.get('summary') is not None:
            offer['summary'] = re.sub('\\s+', ' ', _sanitize_visible_model_text(offer['summary'], language)).strip()
        if offer.get('rationale') is not None:
            offer['rationale'] = '\n\n'.join((normalized for paragraph in re.split('(?:\\r?\\n\\s*){2,}', _sanitize_visible_model_text(offer['rationale'], language)) if (normalized := re.sub('\\s+', ' ', paragraph).strip())))
        presentation = offer.get('proposalPresentation')
        if isinstance(presentation, dict):
            presentation = dict(presentation)
            if presentation.get('summary') is not None:
                presentation['summary'] = re.sub('\\s+', ' ', _sanitize_visible_model_text(presentation['summary'], language)).strip()
            if presentation.get('rationale') is not None:
                presentation['rationale'] = '\n\n'.join((normalized for paragraph in re.split('(?:\\r?\\n\\s*){2,}', _sanitize_visible_model_text(presentation['rationale'], language)) if (normalized := re.sub('\\s+', ' ', paragraph).strip())))
            offer['proposalPresentation'] = presentation
        result['proposalOffer'] = offer
    disagreement = result.get('disagreement')
    if isinstance(disagreement, dict):
        disagreement = dict(disagreement)
        for field_name in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion'):
            if disagreement.get(field_name) is not None:
                disagreement[field_name] = _sanitize_visible_model_text(disagreement[field_name], language)
        result['disagreement'] = disagreement
    cues = result.get('uiCues')
    if isinstance(cues, list):
        result['uiCues'] = [{**cue, 'text': _sanitize_visible_model_text(cue.get('text'), language)} if isinstance(cue, dict) else cue for cue in cues]
    links = result.get('coordinateLinks')
    if isinstance(links, list):
        result['coordinateLinks'] = [{**link, 'text': _sanitize_visible_model_text(link.get('text'), language)} if isinstance(link, dict) else link for link in links]
    return result

def repair_legacy_visible_guidance(guidance, language='en'):
    """Apply the historical read-time language repair to every display field."""
    result = _sanitize_visible_guidance(guidance, language)
    if language != 'zh-CN':
        return result
    for field_name in ('intentHypothesis', 'followUpQuestion'):
        if result.get(field_name) is not None:
            result[field_name] = repair_legacy_visible_text(result[field_name], language)
    offer = result.get('proposalOffer')
    if isinstance(offer, dict):
        offer = dict(offer)
        for field_name in ('summary', 'rationale'):
            if offer.get(field_name) is not None:
                offer[field_name] = repair_legacy_visible_text(offer[field_name], language)
        presentation = offer.get('proposalPresentation')
        if isinstance(presentation, dict):
            presentation = dict(presentation)
            for field_name in ('summary', 'rationale'):
                if presentation.get(field_name) is not None:
                    presentation[field_name] = repair_legacy_visible_text(presentation[field_name], language)
            offer['proposalPresentation'] = presentation
        result['proposalOffer'] = offer
    disagreement = result.get('disagreement')
    if isinstance(disagreement, dict):
        disagreement = dict(disagreement)
        if disagreement.get('subject') == 'ai_revision_challenge' and disagreement.get('phase') == 'choice_pending' and (disagreement.get('status') == 'active') and disagreement.get('proposalSummary') and (disagreement.get('nextQuestion') != challenge_choice_card_copy(disagreement.get('userPosition'), disagreement.get('proposalSummary'), language)['nextQuestion'] or (language == 'zh-CN' and (_visible_chinese_language_issue(disagreement.get('coreDisagreement')) or '相关设计指标' in str(disagreement.get('coreDisagreement') or '')))):
            disagreement.update(challenge_choice_card_copy(disagreement.get('userPosition'), disagreement.get('proposalSummary'), language, legacy=True))
        for field_name in ('userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion'):
            if disagreement.get(field_name) is not None:
                disagreement[field_name] = repair_legacy_visible_text(disagreement[field_name], language)
        result['disagreement'] = disagreement
    for collection_name in ('uiCues', 'coordinateLinks'):
        if isinstance(result.get(collection_name), list):
            result[collection_name] = [{**item, 'text': repair_legacy_visible_text(item.get('text'), language)} if isinstance(item, dict) and item.get('text') is not None else item for item in result[collection_name]]
    return result

def _normalize_unsaved_change_claims(value, language='en'):
    text = str(value or '')
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', text):
        text = re.sub('(?:^|(?<=[。！？\\n]))\\s*(?:好[，,。！!]?\\s*)?(?:我)?(?:已经|已)?(?:帮你)?(?:改好了|改完了|修改完成了)[，,。！!]*(?:你)?(?:可以)?(?:去|先)?试试(?:看)?[。！!]?', '我先把这个修改方向说具体，生成可审查的方案后再由你决定是否采用。', text)
        text = re.sub('(?:^|(?<=[。！？\\n]))\\s*(?:我)?(?:已经|已)(?:帮你)?(?:改好|修改|调整)了[。！!]?', '我现在说的是修改方向，还没有替你保存地图。', text)
        text = re.sub('(?:^|(?<=[。！？\\n]))\\s*(?:好[，,。！!]?\\s*)?(?:那)?我(?:就)?(?:按[^。！？\\n]{0,48})?把[^。！？\\n]{0,72}(?:改成|改为|变成|连成|移到|挪到)[^。！？\\n]*[。！？!]?', '我已经理解这个修改方向；生成出可审查地图前，它还没有实际落到地图上。', text)
        text = re.sub('(?:这份|这个)?(?:提案|方案)(?:会|将)?(?:保持|处于)[^。！？\\n]{0,24}(?:待审查|审查状态)[。！？!]?', '如果你明确授权生成，它会先以待审查提案的形式出现。', text)
    else:
        text = re.sub("(?:^|(?<=[.!?\\n]))\\s*(?:done[.!]?\\s*)?(?:i(?:'ve| have)?|we(?:'ve| have)?)\\s+(?:changed|modified|revised|updated|finished)\\s+(?:it|this|the map|the level)[.!]*(?:\\s+(?:go ahead and )?(?:try|play)(?: it)?[.!]?)?", 'I am describing the revision direction for now; I have not saved a map change.', text, flags=re.IGNORECASE)
        text = re.sub("(?:^|(?<=[.!?\\n]))\\s*(?:okay[, ]+)?i(?: will|'ll)?\\s+(?:change|modify|revise|update)\\b[^.!?\\n]*", 'I understand the revision direction; no map has been generated or saved yet.', text, flags=re.IGNORECASE)
    return text.strip()

def _normalize_change_claims_for_proposal(value, language, has_proposed_rows):
    if not has_proposed_rows:
        return _normalize_unsaved_change_claims(value, language)
    text = str(value or '')
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', text):
        text = re.sub('我按(.{0,80}?)(?:方向|思路|方案)?改[：:]', '我按\\1方向做了一份修改提案：', text)
        text = re.sub('(?:我)?(?:已经|已)?(?:帮你)?(?:改好了|改完了|修改完成了)', '我做了一份可审查的修改提案', text)
        text = re.sub('改完的版本', '这份待审查提案', text)
        text = re.sub('修改后的版本', '这份待审查提案', text)
        text = re.sub('(?:你)?(?:可以)?(?:去|先)?试试(?:看)?', '你可以先看提案的实际差异，再决定是否接受', text)
    else:
        text = re.sub('\\bI (?:changed|revised|updated) it (?:as|by|to)\\b', 'I made a revision proposal by', text, flags=re.IGNORECASE)
        text = re.sub("\\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\\s+(?:changed|modified|revised|updated|finished)\\s+(?:it|this|the map|the level)\\b", 'I made a reviewable revision proposal', text, flags=re.IGNORECASE)
        text = re.sub('\\b(?:the )?(?:changed|revised|updated|finished) version\\b', 'this pending proposal', text, flags=re.IGNORECASE)
    return text.strip()

def _manual_edit_text_is_contextual(text, language):
    lowered = str(text or '').casefold()
    exact_coordinate = re.search('(?:第\\s*\\d+\\s*(?:行|列|格)|\\b(?:row|column|col)\\s*\\d+\\b|\\bx\\s*=\\s*\\d+|\\by\\s*=\\s*\\d+)', lowered)
    if exact_coordinate:
        return False
    if language == 'zh-CN' or re.search('[\\u3400-\\u9fff]', lowered):
        anchors = ('水', '箱', '目标', '墙', '通道', '路线', '区域', '边缘', '编辑器')
        experiments = ('尝试', '实验', '调整', '移动', '改动', '试玩')
        observations = ('观察', '比较', '确认', '看看', '判断', '是否')
    else:
        anchors = ('water', 'box', 'crate', 'target', 'wall', 'corridor', 'route', 'area', 'edge', 'editor')
        experiments = ('try', 'experiment', 'adjust', 'move', 'edit', 'playtest')
        observations = ('observe', 'watch', 'compare', 'confirm', 'judge', 'whether')
    return any((word in lowered for word in anchors)) and any((word in lowered for word in experiments)) and any((word in lowered for word in observations))

def _remove_extracted_warning_sentence(visible_content, ui_cues):
    warning_texts = [cue.get('text', '') for cue in ui_cues if cue.get('type') == 'warning']
    result = str(visible_content or '')
    for warning in warning_texts:
        candidates = [warning]
        if ':' in warning or '：' in warning:
            candidates.append(re.split('[:：]', warning, maxsplit=1)[1].strip())
        for candidate in candidates:
            if candidate and candidate in result:
                result = result.replace(candidate, '', 1)
                break
    result = re.sub('[ \\t]+\\n', '\n', result)
    result = re.sub('\\n{3,}', '\n\n', result)
    return result.strip()

def _deduplicate_assistant_body(value):
    """Keep one natural copy of a repeated model paragraph or sentence."""
    paragraphs = []
    for raw_paragraph in str(value or '').split('\n\n'):
        paragraph = raw_paragraph.strip()
        if not paragraph:
            continue
        sentences = [sentence.strip() for sentence in re.split('(?<=[.!?。！？])\\s*', paragraph) if sentence.strip()]
        kept_sentences = []
        for sentence in sentences:
            if not any((_guidance_text_matches(sentence, kept) for kept in kept_sentences)):
                kept_sentences.append(sentence)
        separator = '' if re.search('[\\u3400-\\u9fff]', paragraph) else ' '
        candidate = separator.join(kept_sentences).strip()
        if candidate and (not any((_guidance_text_matches(candidate, previous) for previous in paragraphs))):
            paragraphs.append(candidate)
    return '\n\n'.join(paragraphs)

def _normalize_response_paragraphs(value):
    """Balance visible prose without dropping sentences or route details."""
    body = _deduplicate_assistant_body(value)
    if not body:
        return body

    def sentences(paragraph):
        return [sentence.strip() for sentence in re.split('(?<=[.!?\\u3002\\uff01\\uff1f])\\s*|(?<=[;\\uff1b])\\s*|[\\r\\n]+', paragraph) if sentence.strip()]

    def is_cjk(text):
        return bool(re.search('[\\u3400-\\u9fff]', text))

    def too_large(items):
        text = ''.join(items) if is_cjk(''.join(items)) else ' '.join(items)
        if is_cjk(text):
            return len(text) > CHAT_PARAGRAPH_MAX_CHINESE_CHARS
        return len(re.findall('\\b\\w+\\b', text)) > CHAT_PARAGRAPH_MAX_LATIN_WORDS
    groups = []
    pending = []

    def flush():
        nonlocal pending
        if pending:
            groups.append(list(pending))
            pending = []
    for raw_paragraph in body.split('\n\n'):
        paragraph_sentences = sentences(raw_paragraph.strip())
        if not paragraph_sentences:
            continue
        for sentence in paragraph_sentences:
            if pending and (len(pending) >= 4 or too_large(pending + [sentence])):
                flush()
            pending.append(sentence)
        if len(paragraph_sentences) > 1:
            flush()
    flush()

    def render(items):
        separator = '' if is_cjk(''.join(items)) else ' '
        return separator.join(items).strip()
    rendered = [render(group) for group in groups if render(group)]
    while len(rendered) > CHAT_MAX_PARAGRAPHS:
        merge_index = next((index for index in range(len(rendered) - 1) if len(sentences(rendered[index])) + len(sentences(rendered[index + 1])) <= 4), None)
        if merge_index is None:
            merge_index = min(range(len(rendered) - 1), key=lambda index: len(sentences(rendered[index])) + len(sentences(rendered[index + 1])))
        merged = [rendered[merge_index], rendered[merge_index + 1]]
        separator = '' if is_cjk(''.join(merged)) else ' '
        rendered[merge_index:merge_index + 2] = [separator.join(merged)]
    return '\n\n'.join(rendered).strip()

def _remove_guidance_from_body(value, guidance_text):
    """Cards are rendered separately, so their thought must not remain in the body."""
    body = _deduplicate_assistant_body(value)
    guidance = str(guidance_text or '').strip()
    if not body or not guidance:
        return body
    paragraphs = []
    for paragraph in body.split('\n\n'):
        if _guidance_text_matches(paragraph, guidance):
            continue
        sentences = [sentence.strip() for sentence in re.split('(?<=[.!?。！？])\\s*', paragraph) if sentence.strip()]
        kept = [sentence for sentence in sentences if not _guidance_text_matches(sentence, guidance)]
        if kept:
            separator = '' if re.search('[\\u3400-\\u9fff]', paragraph) else ' '
            paragraphs.append(separator.join(kept))
    return '\n\n'.join(paragraphs).strip() or body

def _guidance_text_matches(current, previous):

    def normalize(value):
        return re.sub('[^\\w\\u3400-\\u9fff]+', '', str(value or '').casefold())
    current_text = normalize(current)
    previous_text = normalize(previous)
    if not current_text or not previous_text:
        return False
    return current_text == previous_text or SequenceMatcher(None, current_text, previous_text).ratio() >= 0.88

def _guidance_reuses_visible_sentence(card_text, visible_content):
    card = re.sub('\\s+', ' ', str(card_text or '')).strip()
    if not card:
        return False
    sentences = [re.sub('\\s+', ' ', sentence).strip() for sentence in re.split('(?<=[.!?。！？])\\s*|[\\r\\n]+', str(visible_content or '')) if sentence.strip()]
    normalized_card = re.sub('[^\\w\\u3400-\\u9fff]+', '', card.casefold())
    for sentence in sentences:
        normalized_sentence = re.sub('[^\\w\\u3400-\\u9fff]+', '', sentence.casefold())
        if not normalized_sentence:
            continue
        if normalized_card in normalized_sentence or normalized_sentence in normalized_card:
            return True
        if SequenceMatcher(None, normalized_card, normalized_sentence).ratio() >= 0.76:
            return True
    return False

def _proposal_card_is_meta_language(value):
    text = ' '.join(str(value or '').split()).strip().casefold()
    if not text:
        return False
    chinese_patterns = ('这个判断.{0,30}(?:影响|决定).{0,35}(?:接下来|下一步).{0,30}(?:建议|方案|调整|怎么)', '(?:接下来|下一步)(?:我|我们)?(?:会|将|再)?(?:建议|调整|讨论)', '(?:我会|我将)根据(?:这个|该)(?:判断|结论|想法).{0,50}(?:建议|方案|调整|修改|路线|怎么)', '我注意到你.{0,35}(?:进行|做)了?(?:修改|调整)', '这个改动.{0,30}(?:影响|决定)我(?:接下来|下一步)')
    if any((re.search(pattern, text) for pattern in chinese_patterns)):
        return True
    english_patterns = ('\\bthis judgment.{0,40}(?:affect|determine).{0,35}(?:next|following).{0,30}(?:suggest|recommend|adjust)', '\\b(?:next|then)\\s+(?:i|we)\\s+(?:will|would|can)\\s+(?:suggest|recommend|adjust|discuss)', '\\b(?:i|we)\\s+will\\s+base\\s+(?:my|our)\\s+(?:next|following)\\s+(?:suggestion|recommendation|adjustment)', '\\bi noticed that you (?:changed|modified|adjusted)', '\\bthis change.{0,30}(?:affect|determine)\\s+(?:my|our)\\s+(?:next|following)')
    return any((re.search(pattern, text, re.IGNORECASE) for pattern in english_patterns))

def _remove_proposal_text_from_body(body, proposal_offer):
    result = str(body or '').strip()
    for field in ('summary', 'rationale'):
        card_text = str((proposal_offer or {}).get(field) or '').strip()
        if card_text and _guidance_reuses_visible_sentence(card_text, result):
            result = _remove_exact_guidance_from_body(result, card_text)
    return result

def _remove_exact_guidance_from_body(value, guidance_text):
    """Remove only a complete duplicated card sentence or paragraph.

    Similarity checks intentionally accept a card that is a shortened form of a sentence,
    but rendering cleanup must not delete the surrounding analysis in that case.
    """
    body = _deduplicate_assistant_body(value)
    guidance = str(guidance_text or '').strip()
    if not body or not guidance:
        return body

    def normalize(text):
        return re.sub('[^\\w\\u3400-\\u9fff]+', '', str(text or '').casefold())
    normalized_guidance = normalize(guidance)
    paragraphs = []
    for paragraph in body.split('\n\n'):
        if normalize(paragraph) == normalized_guidance:
            continue
        sentences = [sentence.strip() for sentence in re.split('(?<=[.!?。！？])\\s*|[\\r\\n]+', paragraph) if sentence.strip()]
        kept = [sentence for sentence in sentences if normalize(sentence) != normalized_guidance]
        if kept:
            separator = '' if re.search('[\\u3400-\\u9fff]', paragraph) else ' '
            paragraphs.append(separator.join(kept))
    return '\n\n'.join(paragraphs).strip() or body

def _normalize_coordinate_links(value, rows=None):
    """Keep only safe, map-bounded visual annotations from the model."""
    if not isinstance(value, list):
        return []
    height = len(rows) if rows is not None else 10
    width = len(rows[0]) if rows and isinstance(rows[0], str) else 12
    normalized = []
    seen = set()
    for item in value[:COORDINATE_LINK_LIMIT]:
        if not isinstance(item, dict):
            continue
        text = item.get('text')
        source = item.get('from')
        destination = item.get('to')
        if not isinstance(text, str) or not text.strip():
            continue
        if len(text.strip()) > 400:
            continue
        if not isinstance(source, dict) or not isinstance(destination, dict):
            continue
        coordinates = []
        valid = True
        for point in (source, destination):
            row = point.get('row')
            column = point.get('column')
            if isinstance(row, bool) or not isinstance(row, int) or isinstance(column, bool) or (not isinstance(column, int)) or (not (1 <= row <= height and 1 <= column <= width)):
                valid = False
                break
            if rows is not None:
                if row > len(rows) or not isinstance(rows[row - 1], str):
                    valid = False
                    break
                if column > len(rows[row - 1]) or rows[row - 1][column - 1] not in {'.', 'p', 's', 't'}:
                    valid = False
                    break
            coordinates.append((row, column))
        if not valid or coordinates[0] == coordinates[1]:
            continue
        link = {'text': text.strip(), 'from': {'row': coordinates[0][0], 'column': coordinates[0][1]}, 'to': {'row': coordinates[1][0], 'column': coordinates[1][1]}}
        key = (link['text'], coordinates[0], coordinates[1])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(link)
    return normalized

def _route_text_has_direction(text):
    return bool(re.search('(?:\\u2192|->|\\b(?:to|toward|towards|through|via|along|walk(?:ing)?|move(?:s|d|ing)?|go(?:es|ing)?|lead(?:s|ing)?)\\b|到|向|往|朝向|通往|通向|经过|沿着|走到|走向|前往|抵达|推向|推到|移动到|绕到|绕向)', str(text or ''), flags=re.IGNORECASE))

def _route_anchor_matches(text, rows, entity_bindings=None):
    """Return ordered entity/coordinate anchors with their visible spans."""
    try:
        entity_coordinates = _map_entity_coordinates(rows, entity_bindings)
        facts = build_map_facts(rows, entity_bindings=entity_bindings)
        uncertain = {item.get('id') for item in facts.get('entities') or [] if item.get('id') and item.get('identityConfidence') != 'exact'}
    except (TypeError, ValueError, KeyError):
        return None
    coordinate_pattern = re.compile('[\\(\\uFF08]\\s*(\\d{1,2})\\s*[,\\uFF0C]\\s*(\\d{1,2})\\s*[\\)\\uFF09]')
    entity_pattern = re.compile('(?<![A-Za-z0-9])(?:P|B\\d+|T\\d+|玩家|起点)(?![A-Za-z0-9])', flags=re.IGNORECASE)
    anchors = []
    for match in coordinate_pattern.finditer(str(text or '')):
        anchors.append({'start': match.start(), 'end': match.end(), 'point': {'row': int(match.group(1)), 'column': int(match.group(2))}})
    for match in entity_pattern.finditer(str(text or '')):
        label = match.group(0).upper()
        if label in {'玩家', '起点'}:
            label = 'P'
        if label in uncertain:
            return None
        point = entity_coordinates.get(label)
        if point is None:
            return None
        anchors.append({'start': match.start(), 'end': match.end(), 'point': dict(point)})
    anchors.sort(key=lambda item: item['start'])
    return anchors

def _route_anchors(text, rows, entity_bindings=None):
    """Return ordered, authoritative coordinate anchors from route prose."""
    matches = _route_anchor_matches(text, rows, entity_bindings)
    if matches is None:
        return None
    anchors = []
    for item in matches:
        point = item['point']
        if anchors and anchors[-1] == point:
            continue
        anchors.append(point)
    return anchors

def _route_text_is_concise(text):
    """Reject a whole evaluation/report being used as one route label."""
    value = str(text or '').strip()
    if not value or '\n' in value or '\r' in value or (len(value) > 220):
        return False
    return len(re.findall('[.!?。！？]', value)) <= 1

def _coordinate_route_exists(rows, source, destination):
    """Use the same conservative walkability rule as the browser arrow renderer."""
    if not isinstance(rows, list) or not rows or (not all((isinstance(row, str) for row in rows))):
        return False
    height = len(rows)
    width = len(rows[0])
    for row in rows:
        if len(row) != width:
            return False

    def point(value):
        if not isinstance(value, dict):
            return None
        row = value.get('row')
        column = value.get('column')
        if isinstance(row, bool) or not isinstance(row, int) or isinstance(column, bool) or (not isinstance(column, int)) or (not (1 <= row <= height and 1 <= column <= width)):
            return None
        return (row - 1, column - 1)
    start = point(source)
    end = point(destination)
    if start is None or end is None or start == end:
        return False
    start_key = f'{start[0]},{start[1]}'
    end_key = f'{end[0]},{end[1]}'

    def is_open(row, column):
        if not (0 <= row < height and 0 <= column < width):
            return False
        tile = rows[row][column]
        if tile in {' ', '#', '@'}:
            return False
        key = f'{row},{column}'
        return tile != 's' or key in {start_key, end_key}
    if not is_open(*start) or not is_open(*end):
        return False
    queue = deque([start])
    visited = {start}
    while queue:
        row, column = queue.popleft()
        if (row, column) == end:
            return True
        for row_delta, column_delta in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            next_point = (row + row_delta, column + column_delta)
            if next_point in visited or not is_open(*next_point):
                continue
            visited.add(next_point)
            queue.append(next_point)
    return False

def _coordinate_link_is_grounded(link, rows, entity_bindings=None):
    if rows is None:
        return True
    if not _route_relation_is_explicit(link.get('text'), link.get('from'), link.get('to'), rows, entity_bindings):
        return False
    if not _coordinate_route_exists(rows, link.get('from'), link.get('to')):
        return False
    return True

def _route_relation_clauses(text, rows=None, entity_bindings=None):
    """Split route prose at sentence and explicit relation boundaries."""
    sentences = re.split('(?<=[.!?\\u3002\\uff01\\uff1f])\\s*|[\\r\\n]+', str(text or ''))
    clauses = []
    split_pattern = re.compile('\\s*(?:[;\\u003b\\uff1b]+|\\s+(?:and then|while|whereas)\\s+|,(?=\\s*(?:while|whereas|before|after|or)\\b)|\\uff0c(?=\\s*(?:\\u540c\\u65f6|\\u7136\\u540e|\\u968f\\u540e|\\u518d|\\u800c|\\u6216\\u8005|\\u4e4b\\u524d|\\u4e4b\\u540e)))\\s*', flags=re.IGNORECASE)
    for sentence in sentences:
        for piece in split_pattern.split(sentence):
            piece = piece.strip()
            if not piece:
                continue
            comma_parts = re.split('(?<=\\S),\\s*(?=then\\b)|(?<=\\S)\\uff0c\\s*(?=\\u7136\\u540e|\\u968f\\u540e)', piece, flags=re.IGNORECASE)
            if rows is not None and len(comma_parts) > 1:
                split_independent = any((len(_route_anchor_matches(left, rows, entity_bindings) or []) >= 2 and len(_route_anchor_matches(right, rows, entity_bindings) or []) >= 2 for left, right in zip(comma_parts, comma_parts[1:])))
                if split_independent:
                    clauses.extend((part.strip() for part in comma_parts if part.strip()))
                    continue
            clauses.append(piece)
    return clauses

def _route_relation_is_explicit(text, source, destination, rows, entity_bindings=None):
    """Accept one local movement relation, not a location statement or route chain."""
    if not _route_text_is_concise(text):
        return False
    matches = _route_relation_anchor_matches(str(text or ''), rows, entity_bindings)
    if not matches:
        return False
    anchors = []
    for match in matches:
        point = match['point']
        if anchors and anchors[-1] == point:
            continue
        anchors.append(point)
    if len(anchors) != 2 or anchors[0] != source or anchors[-1] != destination:
        return False
    return any((candidate['from'] == source and candidate['to'] == destination and (candidate['text'] in str(text or '')) for candidate in _route_relation_candidates_without_grounding(text, rows, entity_bindings)))

def _route_relation_anchor_matches(text, rows, entity_bindings=None):
    """Ignore an entity's ``from`` coordinate qualifier, but keep real waypoints."""
    matches = _route_anchor_matches(text, rows, entity_bindings)
    if matches is None:
        return None
    filtered = []
    value = str(text or '')
    entity_before_coordinate = re.compile('(?<![A-Za-z0-9])(?:P|B\\d+|T\\d+)(?![A-Za-z0-9])[^()\\r\\n]{0,32}(?:\\bfrom\\b|\\u4ece)\\s*$', flags=re.IGNORECASE)
    for match in matches:
        is_coordinate = value[match['start']:match['end']].lstrip().startswith(('(', '（'))
        prefix = value[:match['start']]
        if is_coordinate and entity_before_coordinate.search(prefix):
            continue
        filtered.append(match)
    return filtered

def _route_relation_candidates_without_grounding(text, rows, entity_bindings=None):
    """Build local relation snippets before the recursive grounding check."""
    candidates = []
    movement = re.compile('(?:\\u2192|->|\\b(?:to|toward|towards|through|via|along|walk(?:s|ed|ing)?|move(?:s|d|ing)?|go(?:es|ing)?|lead(?:s|ing)?|reach(?:es|ed|ing)?)\\b|(?:\\u5230|\\u5411|\\u671d\\u5411|\\u901a\\u5f80|\\u5f80|\\u7ecf\\u8fc7|\\u6cbf\\u7740|\\u8d70|\\u8d70\\u5230|\\u79fb\\u52a8\\u5230|\\u63a8\\u5411|\\u63a8\\u5230|\\u7ed5\\u5411|\\u7a7f\\u8fc7))', flags=re.IGNORECASE)
    for clause in _route_relation_clauses(text, rows, entity_bindings):
        matches = _route_relation_anchor_matches(clause, rows, entity_bindings)
        if not matches or len(matches) < 2:
            continue
        anchors = []
        for match in matches:
            if anchors and anchors[-1]['point'] == match['point']:
                anchors[-1]['end'] = max(anchors[-1]['end'], match['end'])
                continue
            anchors.append(dict(match))
        for source, destination in zip(anchors, anchors[1:]):
            between = clause[source['end']:destination['start']]
            if re.search('\\b(?:is|are|lies?|sits?|located|positioned|placed)\\b[^.!?]{0,40}\\b(?:near|close\\s+to|beside|adjacent|next\\s+to|to\\s+the\\s+left|to\\s+the\\s+right|above|below)\\b|(?:\\u5728|\\u4f4d\\u4e8e|\\u9760\\u8fd1|\\u65c1\\u8fb9|\\u76f8\\u90bb|\\u5de6\\u4fa7|\\u53f3\\u4fa7|\\u4e0a\\u65b9|\\u4e0b\\u65b9)', between, flags=re.IGNORECASE):
                continue
            if not movement.search(between):
                continue
            if re.search('\\b(?:go|walk|move)\\w*\\b.*\\bfrom\\b', between, re.IGNORECASE) and (not re.search('\\b(?:to|toward|towards|through|via)\\b', between, re.IGNORECASE)):
                continue
            candidates.append({'text': clause.strip() if len(anchors) == 2 else clause[source['start']:destination['end']].strip(' ,，;；'), 'from': dict(source['point']), 'to': dict(destination['point'])})
    return candidates

def _recover_coordinate_links(body, rows, existing=None, entity_bindings=None):
    """Recover up to twelve local route links from grounded visible prose."""
    if not rows:
        return list(existing or [])
    links = list(existing or [])
    seen = {(item['text'], item['from']['row'], item['from']['column'], item['to']['row'], item['to']['column']) for item in links}
    for candidate in _route_relation_candidates_without_grounding(body, rows, entity_bindings):
        if not _coordinate_link_is_grounded(candidate, rows, entity_bindings):
            continue
        key = (candidate['text'], candidate['from']['row'], candidate['from']['column'], candidate['to']['row'], candidate['to']['column'])
        if key in seen:
            continue
        overlaps_existing = any((item['text'] in candidate['text'] or candidate['text'] in item['text'] for item in links))
        if overlaps_existing:
            continue
        seen.add(key)
        links.append(candidate)
        if len(links) >= COORDINATE_LINK_LIMIT:
            break
    return links[:COORDINATE_LINK_LIMIT]

def _filter_coordinate_links(value, body, rows=None, entity_bindings=None):
    """Keep only LLM-authored annotations that survived visible-body cleanup."""
    visible_body = str(body or '')
    filtered = []
    occupied_ranges = []
    for link in _normalize_coordinate_links(value, rows=rows):
        if not _coordinate_link_text_is_complete_clause(link.get('text')):
            continue
        if not _coordinate_link_is_grounded(link, rows, entity_bindings):
            continue
        start = visible_body.find(link['text'])
        if start < 0:
            continue
        end = start + len(link['text'])
        if any((start < occupied_end and end > occupied_start for occupied_start, occupied_end in occupied_ranges)):
            continue
        occupied_ranges.append((start, end))
        filtered.append(link)
    return filtered

def _coordinate_link_text_is_complete_clause(value):
    """Avoid underlining a sentence fragment that leaves punctuation stranded."""
    text = str(value or '').strip()
    if not text:
        return False
    if text[0] in ',，、;；:：)）]}】':
        return False
    if text[-1] in ',，、;；:：(（[{【':
        return False
    return not re.search('(?:\\b(?:and|or|while|then|because|so|but)\\b|(?:以及|并且|同时|然后|因此|但是|而且))$', text, flags=re.IGNORECASE)

def _intent_sentence_count(value):
    return len([part for part in re.split('(?<=[.!?;\\u3002\\uFF01\\uFF1F\\uFF1B])\\s*', str(value or '')) if part.strip()])

def _localized_change_labels(components, language):
    labels = {'zh-CN': {'outerShell': '外壳', 'water': '水域', 'internalWalls': '内部墙体', 'boxes': '箱子位置', 'targets': '目标点', 'player': '玩家位置', 'floorArea': '可用地面'}, 'en': {'outerShell': 'the outer shell', 'water': 'the water area', 'internalWalls': 'the internal walls', 'boxes': 'the box positions', 'targets': 'the target positions', 'player': 'the player position', 'floorArea': 'the usable floor area'}}
    selected = labels['zh-CN' if language == 'zh-CN' else 'en']
    return [selected[component] for component in components if component in selected]

def _join_english(values):
    if len(values) < 2:
        return values[0] if values else ''
    if len(values) == 2:
        return ' and '.join(values)
    return ', '.join(values[:-1]) + ', and ' + values[-1]
