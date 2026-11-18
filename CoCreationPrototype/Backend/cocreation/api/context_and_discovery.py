"""LLM context construction, proposal discovery state, and DesignContext updates."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import TYPE_CHECKING

from cocreation.domain import proposal_options
from cocreation.domain.conversation_evidence import build_dialogue_evidence
from cocreation.domain.design_context import (
    add_confirmed_decision,
    add_open_question,
    apply_question_answer_review,
    design_level_open_questions,
    evaluator_projection,
    infer_intent_topic,
    is_design_level_question,
    merge_chat_update,
    merge_intent_hypothesis,
    question_dedup_key,
    revision_projection,
    sanitize_user_design_text,
    set_active_disagreement,
)
from cocreation.domain.design_requirements import protected_change_requested
from cocreation.domain.level_validation import (
    build_entity_bindings,
    build_map_facts,
    build_stage_snapshot,
    build_untrusted_entity_bindings,
    entity_bindings_match_rows,
    summarize_stage_changes,
)
from cocreation.domain.proposal_understanding import designer_sources, validate_topic
from cocreation.llm.facade import (
    _contains_user_design_direction,
    _guidance_advice_request,
    _guidance_confusion_request,
    classify_revision_request,
)
from cocreation.persistence.repository import (
    dump_json,
    get_version,
    load_design_context,
    load_entity_bindings,
    load_json,
    map_fingerprint,
    record_event,
    save_design_context,
)
from cocreation.reliability import component_recovery

from .config_models import (
    AGENT_HANDOFF_SCHEMA_VERSION,
    AGENT_HANDOFF_STATUSES,
    ApiError,
    utc_now,
)

if TYPE_CHECKING:
    from .services import CoCreationServices


def _emit_event(database, session_id, event_type, payload, services: CoCreationServices | None = None, created_at=None):
    ts = created_at or (services.utc_now() if services is not None else utc_now())
    if services is not None:
        services.record_event(database, session_id, event_type, payload, ts)
    else:
        record_event(database, session_id, event_type, payload, ts)


def _intent_claim_key(claim):
    base = tuple(
        (
            str(claim.get(key) or '').strip().casefold()
            for key in ('subjectType', 'attributeType', 'direction', 'aspect', 'scopeType', 'scopeText')
        )
    )
    if base[0] == 'other' or base[1] == 'other':
        return base + (
            str(claim.get('subjectText') or '').strip().casefold(),
            str(claim.get('attributeText') or '').strip().casefold(),
            str(claim.get('normalizedMeaning') or '').strip().casefold(),
        )
    return base


def _intent_claims_equivalent(left, right):
    left_keys = {_intent_claim_key(item) for item in left or [] if isinstance(item, dict)}
    right_keys = {_intent_claim_key(item) for item in right or [] if isinstance(item, dict)}
    return bool(left_keys and left_keys == right_keys)


def _deterministic_intent_conflict(new_claims, existing_claims):
    opposites = {('increase', 'decrease'), ('decrease', 'increase')}
    for new_claim in new_claims or []:
        for old_claim in existing_claims or []:
            if not isinstance(new_claim, dict) or not isinstance(old_claim, dict):
                continue
            if (
                new_claim.get('semanticSource') != 'kimi_reviewed'
                or old_claim.get('semanticSource') != 'kimi_reviewed'
                or not new_claim.get('evidenceSpan')
                or not old_claim.get('evidenceSpan')
            ):
                continue
            new_subject = new_claim.get('subjectType') or new_claim.get('subject')
            old_subject = old_claim.get('subjectType') or old_claim.get('subject')
            if new_subject != old_subject:
                continue
            new_attribute = new_claim.get('attributeType') or new_claim.get('attribute')
            old_attribute = old_claim.get('attributeType') or old_claim.get('attribute')
            if new_attribute != old_attribute:
                continue
            if (new_claim.get('scopeType') or 'stage') != (old_claim.get('scopeType') or 'stage'):
                continue
            if (new_claim.get('scopeText') or 'stage') != (old_claim.get('scopeText') or 'stage'):
                continue
            new_aspect = new_claim.get('aspect') or 'unspecified'
            old_aspect = old_claim.get('aspect') or 'unspecified'
            if new_aspect != old_aspect:
                continue
            if (new_claim.get('direction'), old_claim.get('direction')) in opposites:
                return {'new': new_claim, 'existing': old_claim}
    return None


def _deterministic_conflict_explanation(conflict_pair, language):
    new_claim = (conflict_pair or {}).get('new', {})
    old_claim = (conflict_pair or {}).get('existing', {})
    new_direction = new_claim.get('direction')
    old_direction = old_claim.get('direction')
    if language == 'zh-CN':
        subject = {
            'water': '水域',
            'wall': '墙体',
            'box': '箱子',
            'target': '目标',
            'route': '路线',
            'layout': '布局',
            'space': '空间',
        }.get(new_claim.get('subjectType') or new_claim.get('subject'), new_claim.get('subjectText') or '设计对象')
        attribute = {
            'coverage': '覆盖范围',
            'amount': '数量',
            'density': '密度',
            'complexity': '复杂度',
            'length': '长度',
            'clarity': '清晰度',
            'concentration': '集中程度',
            'separation': '分离程度',
        }.get(new_claim.get('attributeType') or new_claim.get('attribute'), new_claim.get('attributeText') or '当前属性')
        new_label = f'增加{subject}的{attribute}' if new_direction == 'increase' else f'减少{subject}的{attribute}'
        old_label = f'增加{subject}的{attribute}' if old_direction == 'increase' else f'减少{subject}的{attribute}'
        return (
            f'我从你现在的表达中读到的核心方向是“{new_label}”，而最近确认意图对应的直接方向是“{old_label}”。'
            f'两者都在决定{subject}的{attribute}，却要求向相反方向调整，因此不能同时指导这项设计决定。'
            '我不会替你选择，请在下面两张卡中决定保留哪一个。'
        )
    subject = str(new_claim.get('subject') or 'design subject')
    attribute = str(new_claim.get('attribute') or 'property')
    new_label = f'increase {subject} {attribute}' if new_direction == 'increase' else f'reduce {subject} {attribute}'
    old_label = f'increase {subject} {attribute}' if old_direction == 'increase' else f'reduce {subject} {attribute}'
    return (
        f'I read the new core direction as “{new_label}”, while the most recently confirmed direct direction is “{old_label}”. '
        f'Both govern the same {attribute} of {subject} but require opposite changes, so they cannot both guide this decision. '
        'I will not choose between them; please keep one of the two cards below.'
    )


def record_intent_hypothesis(
    database,
    session_id,
    version_id,
    turn_id,
    guidance,
    source,
    status='proposed',
    proposal_id=None,
    reason='',
    created_at=None,
    *,
    services: CoCreationServices | None = None,
):
    """Keep an AI intent interpretation separate from the designer's final report."""
    guidance = guidance or {}
    hypothesis = str(guidance.get('intentHypothesis') or '').strip()
    if not hypothesis:
        return
    if status not in AGENT_HANDOFF_STATUSES:
        raise ValueError(f'Unsupported intent hypothesis status: {status}')
    payload = {
        'schemaVersion': AGENT_HANDOFF_SCHEMA_VERSION,
        'agent': 'co_creation_chat',
        'artifactType': 'intentHypothesis',
        'versionId': version_id,
        'turnId': turn_id,
        'artifact': {
            'hypothesis': hypothesis,
            'confidence': guidance.get('intentConfidence'),
            'semanticClaims': guidance.get('_intentSemanticClaims') or [],
        },
        'evidence': [
            {'type': 'stage', 'versionId': version_id},
            {'type': 'conversation_turn', 'turnId': turn_id, 'source': source},
        ],
        'status': status,
    }
    evidence_signature = guidance.get('evidenceSignature')
    if evidence_signature:
        payload['evidence'].append({'type': 'guidance_evidence_signature', 'value': evidence_signature})
    if proposal_id:
        payload['proposalId'] = proposal_id
    if reason:
        payload['reason'] = reason
    _emit_event(database, session_id, 'intent_hypothesis', payload, services=services, created_at=created_at)


def record_intent_evidence(
    database,
    session_id,
    version_id,
    kind,
    observation,
    *,
    turn_id=None,
    proposal_id=None,
    play_attempt_id=None,
    details=None,
    created_at=None,
    services: CoCreationServices | None = None,
):
    """Append an immutable observation; evidence never confirms intention by itself."""
    evidence_id = 'ie_' + uuid.uuid4().hex
    version = get_version(database, session_id, version_id)
    rows = load_json(version['rows_json']) if version is not None else []
    payload = {
        'schemaVersion': 1,
        'evidenceId': evidence_id,
        'kind': str(kind),
        'versionId': version_id,
        'turnId': turn_id,
        'proposalId': proposal_id,
        'playAttemptId': play_attempt_id,
        'mapFingerprint': map_fingerprint(rows) if rows else None,
        'observation': str(observation or '').strip()[:1200],
        'details': details or {},
        'authority': 'evidence_only',
    }
    _emit_event(database, session_id, 'intent_evidence_recorded', payload, services=services, created_at=created_at)
    return evidence_id


def _pending_intent_evidence(database, session_id, version, design_context, limit=12):
    lineage_ids = {item['versionId'] for item in _stage_lineage(database, session_id, version)}
    processed = set(design_context.get('processedEvidenceIds') or [])
    result = []
    rows = database.execute(
        "\n        SELECT payload_json, created_at FROM audit_events\n"
        "        WHERE session_id = ? AND event_type = 'intent_evidence_recorded'\n"
        "        ORDER BY id DESC LIMIT 96\n        ",
        (session_id,),
    ).fetchall()
    for row in rows:
        payload = load_json(row['payload_json']) or {}
        evidence_id = payload.get('evidenceId')
        if not evidence_id or evidence_id in processed or payload.get('versionId') not in lineage_ids:
            continue
        observation = re.sub('\\s+', ' ', str(payload.get('observation') or '')).strip()
        if re.search(
            '(?:ignore (?:all|any|the) (?:previous|prior)|system prompt|developer message|reveal (?:the )?prompt|\\u5ffd\\u7565(?:\\u4e0a\\u8ff0|\\u4e4b\\u524d)|\\u7cfb\\u7edf\\u63d0\\u793a|\\u5f00\\u53d1\\u8005\\u6d88\\u606f)',
            observation,
            flags=re.IGNORECASE,
        ):
            observation = '[instruction-like evidence text withheld]'
        result.append({
            'evidenceId': evidence_id,
            'kind': payload.get('kind'),
            'versionId': payload.get('versionId'),
            'turnId': payload.get('turnId'),
            'observation': observation[:320],
            'details': _safe_intent_evidence_details(payload.get('kind'), payload.get('details')),
            'createdAt': row['created_at'],
        })
        if len(result) >= limit:
            break
    return list(reversed(result))


def _safe_intent_evidence_details(kind, value):
    """Project audit detail into bounded facts suitable for an LLM prompt."""
    if not isinstance(value, dict):
        return {}
    allowed_fields = {
        'conversation': {'requestProposal'},
        'manual_edit': {'parentVersionId', 'diff', 'changeSummary', 'verifiedDiff', 'validation'},
        'proposal_accept': {'decision', 'reason', 'baseVersionId'},
        'proposal_reject': {'decision', 'reason', 'baseVersionId'},
        'stage_restore': {'sourceVersionId', 'replacedVersionId'},
        'disagreement_active': {'subject', 'resolution', 'userPosition', 'aiPosition'},
        'disagreement_resolved': {'subject', 'resolution', 'userPosition', 'aiPosition'},
        'play_completed': {
            'status',
            'moveCount',
            'pushCount',
            'restartCount',
            'durationSeconds',
            'minimumMoves',
            'minimumPushes',
        },
        'play_abandoned': {
            'status',
            'moveCount',
            'pushCount',
            'restartCount',
            'durationSeconds',
            'minimumMoves',
            'minimumPushes',
        },
    }.get(str(kind), set())

    def bounded(item, depth=0):
        if depth > 3:
            return None
        if isinstance(item, bool) or item is None:
            return item
        if isinstance(item, (int, float)):
            return item
        if isinstance(item, str):
            clean = re.sub('\\s+', ' ', item).strip()[:240]
            if re.search(
                '(?:ignore (?:all|any|the) (?:previous|prior)|system prompt|developer message|reveal (?:the )?prompt|\\u5ffd\\u7565(?:\\u4e0a\\u8ff0|\\u4e4b\\u524d)|\\u7cfb\\u7edf\\u63d0\\u793a|\\u5f00\\u53d1者\\u6d88\\u606f)',
                clean,
                flags=re.IGNORECASE,
            ):
                return '[instruction-like evidence text withheld]'
            return clean
        if isinstance(item, list):
            return [bounded(entry, depth + 1) for entry in item[:24]]
        if isinstance(item, dict):
            return {str(key)[:64]: bounded(entry, depth + 1) for key, entry in list(item.items())[:24]}
        return str(item)[:120]

    return {field: bounded(value.get(field)) for field in allowed_fields if field in value}


def _relevant_intent_evidence(hypothesis, evidence, current_turn_id=None):
    topic = infer_intent_topic(hypothesis)
    matched = []
    for item in evidence:
        searchable = ' '.join((str(item.get('observation') or ''), dump_json(item.get('details') or {})))
        if infer_intent_topic(searchable) == topic and topic != 'other':
            matched.append(item)
    current = [item for item in evidence if item.get('turnId') == current_turn_id]
    selected = matched or current or evidence[-1:]
    return selected[-4:]


def _verified_proposal_message(language):
    if language == 'zh-CN':
        return '我把这次地图提案整理好了，也逐格核对了前后的真实变化。它现在仍是一份等你审查的方案；我更想让你先看高亮位置是否真的回应了刚才的方向，再决定要不要接受。'
    return 'I have organized this map proposal and checked its real before/after tile changes. It is still yours to review; I would first look at whether the highlighted cells really answer the direction we discussed before deciding whether to accept it.'


def _looks_like_designer_goal(content):
    text = str(content or '').casefold()
    if '?' in text or '？' in text:
        return False
    return bool(
        re.search(
            '(?:我希望|我想|我更|保持|不要|增加|减少|让|改|调整|公平|难度|可读|路线|箱子|目标|墙|水域|i\\s+(?:want|prefer|would like)|keep|avoid|make|change|adjust|fair|difficulty|readable|route|box|target|wall|water)',
            text,
        )
    )


def _stage_lineage(database, session_id, version):
    """Return compact saved-version facts from the root through ``version``."""
    lineage = []
    current = version
    while current is not None and len(lineage) < 24:
        parent = get_version(database, session_id, current['parent_version_id']) if current['parent_version_id'] else None
        parent_rows = load_json(parent['rows_json']) if parent is not None else None
        current_rows = load_json(current['rows_json'])
        lineage.append({
            'versionId': current['id'],
            'stageNumber': current['stage_number'],
            'parentVersionId': current['parent_version_id'],
            'source': current['source'],
            'summary': current['summary'],
            'changeSummary': summarize_stage_changes(parent_rows, current_rows) if parent_rows is not None else None,
        })
        current = parent
    lineage.reverse()
    return lineage


def _progress_context(database, session_id, version, design_context, latest_user_direction, latest_play):
    """Build a bounded role-neutral continuity projection for the LLM."""
    lineage = _stage_lineage(database, session_id, version)
    stage_numbers = {item['versionId']: item['stageNumber'] for item in lineage}

    def source_stage_number(source_stage_id):
        return stage_numbers.get(source_stage_id, version['stage_number'])

    confirmed = [
        {
            'decision': item['decision'],
            'reason': item.get('reason') or None,
            'sourceStageNumber': source_stage_number(item.get('sourceStageId')),
            'sourceTurnId': item.get('sourceTurnId'),
        }
        for item in design_context.get('confirmedDecisions', [])
        if item.get('status') == 'active'
    ]
    unresolved = [
        {
            'question': item['question'],
            'sourceStageNumber': source_stage_number(item.get('sourceStageId')),
            'sourceTurnId': item.get('sourceTurnId'),
            'id': item.get('id'),
        }
        for item in design_level_open_questions(design_context)
    ]
    inherited = [item for item in confirmed if item['sourceStageNumber'] < version['stage_number']]
    latest_evidence = None
    if latest_play is not None:
        latest_evidence = {
            'kind': 'play',
            'status': latest_play['status'],
            'durationSeconds': latest_play['duration_seconds'],
            'moveCount': latest_play['move_count'],
            'pushCount': latest_play['push_count'],
            'restartCount': latest_play['restart_count'],
            'minimumMoves': latest_play['minimum_moves'],
            'minimumPushes': latest_play['minimum_pushes'],
        }
    return {
        'currentStage': {
            'stageNumber': version['stage_number'],
            'versionId': version['id'],
            'parentVersionId': version['parent_version_id'],
            'source': version['source'],
        },
        'stageLineage': lineage[-12:],
        'inheritedConfirmedDecisions': inherited[-12:],
        'confirmedDecisions': confirmed[-16:],
        'unresolvedQuestions': unresolved[-16:],
        'rejectedDecisions': design_context.get('rejectedDecisions', [])[-12:],
        'latestUserDirection': latest_user_direction or None,
        'latestEvidence': latest_evidence,
        'activeDisagreement': design_context.get('activeDisagreement'),
    }


def _message_commit_binding(database, session_id, version_id):
    """Server-only receipt/evidence/state binding; never placed in LLM context."""
    version = get_version(database, session_id, version_id)
    return {
        **component_recovery.state_stamp(database, session_id, version_id),
        'designContextSignature': component_recovery.signature(load_design_context(database, session_id, version_id)),
        'mapSignature': component_recovery.signature({
            'rows': load_json(version['rows_json']),
            'bindings': load_entity_bindings(database, session_id, version_id),
        }),
    }


def _binary_policy_turns_and_requests(database, session_id, proposal_turns):
    """Collect Policy 2 turn IDs and request keys so Policy 1 discovery stays isolated."""
    binary_turn_ids = set()
    binary_request_ids = set()
    events = database.execute(
        """
        SELECT event_type, payload_json FROM audit_events
        WHERE session_id = ? AND event_type IN ('binary_discovery_state', 'binary_choice_selected')
        """,
        (session_id,),
    ).fetchall()

    def collect_turn_and_request_refs(item):
        if not isinstance(item, dict):
            return
        for turn_key in ('id', 'turnId', 'userTurnId', 'assistantTurnId', 'questionTurnId', 'sourceTurnId'):
            value = item.get(turn_key)
            if isinstance(value, str) and value:
                binary_turn_ids.add(value)
        for req_key in ('requestId', 'messageKey', 'idempotencyKey'):
            value = item.get(req_key)
            if isinstance(value, str) and value:
                binary_request_ids.add(value)

    for row in events:
        payload = load_json(row['payload_json'])
        if not isinstance(payload, dict):
            continue
        collect_turn_and_request_refs(payload)
        if row['event_type'] == 'binary_discovery_state':
            for entry in payload.get('userTurns') or []:
                collect_turn_and_request_refs(entry)
            for entry in payload.get('choices') or []:
                collect_turn_and_request_refs(entry)
            collect_turn_and_request_refs(payload.get('pendingQuestion'))

    for turn in proposal_turns or []:
        try:
            guidance = load_json(turn['guidance_json']) or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            guidance = {}
        offer_presentation = (
            (guidance.get('proposalOffer') or {}).get('proposalPresentation')
            if isinstance(guidance.get('proposalOffer'), dict)
            else {}
        )
        discovery_marker = guidance.get('proposalDiscovery') if isinstance(guidance.get('proposalDiscovery'), dict) else {}
        if (
            guidance.get('proposalQuestion')
            or (isinstance(offer_presentation, dict) and offer_presentation.get('binaryCompact'))
            or discovery_marker.get('policyVersion') == 2
        ):
            if turn['id']:
                binary_turn_ids.add(str(turn['id']))
            if turn['request_id']:
                binary_request_ids.add(str(turn['request_id']))

    return binary_turn_ids, binary_request_ids


def build_llm_context(database, session_id, version, *, services: CoCreationServices | None = None):
    session = database.execute(
        'SELECT initial_draft_method, language, demo_mode FROM design_sessions WHERE id = ?',
        (session_id,),
    ).fetchone()
    turns = database.execute(
        '\n        SELECT id, role, content, request_id, sequence_number, guidance_json FROM conversation_turns\n'
        '        WHERE session_id = ? AND version_id = ?\n'
        '        ORDER BY sequence_number DESC LIMIT 24\n        ',
        (session_id, version['id']),
    ).fetchall()
    proposal_request_keys = {
        str(row['message_key'])
        for row in database.execute(
            "\n            SELECT json_extract(payload_json, '$.messageKey') AS message_key\n"
            "            FROM audit_events\n"
            "            WHERE session_id = ? AND event_type = 'proposal_request_requested'\n            ",
            (session_id,),
        ).fetchall()
        if row['message_key']
    }
    turn_understandings = {
        str(row['message_key']): load_json(row['payload_json'])['result']
        for row in database.execute(
            "SELECT json_extract(payload_json, '$.messageKey') AS message_key,\n"
            "                      payload_json FROM audit_events\n"
            "               WHERE session_id = ? AND event_type = 'turn_understanding'",
            (session_id,),
        ).fetchall()
        if row['message_key']
    }
    proposal_turns = database.execute(
        'SELECT id, role, content, request_id, sequence_number, guidance_json\n'
        '        FROM conversation_turns WHERE session_id = ? AND version_id = ?\n'
        '        ORDER BY sequence_number',
        (session_id, version['id']),
    ).fetchall()
    binary_turn_ids, binary_request_ids = _binary_policy_turns_and_requests(database, session_id, proposal_turns)
    policy1_proposal_turns = [
        turn
        for turn in proposal_turns
        if turn['id'] not in binary_turn_ids and (not turn['request_id'] or turn['request_id'] not in binary_request_ids)
    ]
    policy1_request_keys = proposal_request_keys - binary_request_ids
    clarification_question_count = _clarification_question_count(policy1_proposal_turns)
    proposal_discovery = _proposal_discovery_from_turns(
        policy1_proposal_turns,
        version['id'],
        policy1_request_keys,
        turn_understandings,
        services=services,
    )
    accepted_opening = database.execute(
        "\n        SELECT proposal.id AS proposal_id, proposal.assistant_turn_id,\n"
        "               turn.role, turn.content, turn.guidance_json\n"
        "        FROM designer_decisions AS decision\n"
        "        JOIN change_proposals AS proposal\n"
        "          ON proposal.id = decision.proposal_id\n"
        "        JOIN conversation_turns AS turn\n"
        "          ON turn.id = proposal.assistant_turn_id\n"
        "        WHERE decision.session_id = ?\n"
        "          AND decision.version_id = ?\n"
        "          AND decision.decision_type = 'accept'\n"
        "        ORDER BY decision.created_at LIMIT 1\n        ",
        (session_id, version['id']),
    ).fetchone()
    superseded_assessment_turn_ids = set()
    if accepted_opening is not None:
        superseded_assessment_turn_ids = {
            row['assistant_turn_id']
            for row in database.execute(
                '\n                SELECT assistant_turn_id FROM llm_assessments\n                WHERE session_id = ? AND version_id = ?\n                ',
                (session_id, version['id']),
            ).fetchall()
        }
    latest_play = database.execute(
        "\n        SELECT status, duration_seconds, move_count, push_count, restart_count,\n"
        "               minimum_moves, minimum_pushes, finished_at\n"
        "        FROM play_attempts\n"
        "        WHERE session_id = ? AND version_id = ?\n"
        "          AND status IN ('completed', 'abandoned')\n"
        "        ORDER BY issued_at DESC LIMIT 1\n        ",
        (session_id, version['id']),
    ).fetchone()
    current_rows = load_json(version['rows_json'])
    entity_bindings = load_entity_bindings(database, session_id, version['id'])
    if not isinstance(entity_bindings, dict) or not entity_bindings_match_rows(entity_bindings, current_rows):
        fallback_parent = None
        if isinstance(entity_bindings, dict):
            fallback_parent = dict(entity_bindings)
            fallback_parent['entities'] = [
                {**item, 'identityConfidence': 'unknown'}
                for item in entity_bindings.get('entities', [])
                if isinstance(item, dict)
            ]
        try:
            entity_bindings = build_entity_bindings(
                current_rows,
                parent_bindings=fallback_parent,
                source='runtime_legacy_fallback',
            )
        except (ValueError, TypeError):
            entity_bindings = build_untrusted_entity_bindings(current_rows, source='runtime_legacy_untrusted')
    design_context = load_design_context(database, session_id, version['id'])
    turn_by_id = {turn['id']: turn for turn in turns}
    ordered_turns = list(reversed(turns))
    for hypothesis in design_context.get('intentHypotheses', []):
        if hypothesis.get('status') != 'confirmed':
            continue
        source_user_id = next(
            (
                claim.get('sourceUserTurnId')
                for claim in hypothesis.get('semanticClaims') or []
                if isinstance(claim, dict) and claim.get('sourceUserTurnId')
            ),
            None,
        )
        source_user = turn_by_id.get(source_user_id)
        if source_user is None and source_user_id:
            source_user = database.execute(
                'SELECT id, content FROM conversation_turns WHERE session_id = ? AND id = ?',
                (session_id, source_user_id),
            ).fetchone()
        if source_user is None and hypothesis.get('sourceTurnId'):
            source_assistant = turn_by_id.get(hypothesis.get('sourceTurnId'))
            if source_assistant is not None:
                source_user = next(
                    (
                        turn
                        for turn in reversed(ordered_turns)
                        if turn['role'] == 'user' and turn['sequence_number'] < source_assistant['sequence_number']
                    ),
                    None,
                )
            else:
                db_assistant = database.execute(
                    'SELECT id, version_id, sequence_number FROM conversation_turns WHERE session_id = ? AND id = ?',
                    (session_id, hypothesis.get('sourceTurnId')),
                ).fetchone()
                if db_assistant is not None:
                    source_user = database.execute(
                        "SELECT id, content FROM conversation_turns WHERE session_id = ? AND version_id = ? AND role = 'user' AND sequence_number < ? ORDER BY sequence_number DESC LIMIT 1",
                        (session_id, db_assistant['version_id'], db_assistant['sequence_number']),
                    ).fetchone()
        hypothesis['evidenceText'] = str(source_user['content'])[:1200] if source_user is not None else ''
        hypothesis['evidenceTurnId'] = source_user['id'] if source_user is not None else None
    pending_intent_evidence = _pending_intent_evidence(database, session_id, version, design_context)
    parent = get_version(database, session_id, version['parent_version_id']) if version['parent_version_id'] else None
    parent_rows = load_json(parent['rows_json']) if parent is not None else None
    change_summary = summarize_stage_changes(parent_rows, current_rows) if parent_rows is not None else None
    conversation = [
        {'role': turn['role'], 'content': turn['content']}
        for turn in reversed(turns)
        if turn['id'] not in superseded_assessment_turn_ids
    ]
    recent_guidance = {
        'discussionFocus': None,
        'discussionFocusHistory': [],
        'intentHypothesis': None,
        'proposalOffer': None,
        'activeDisagreement': None,
        'relaxationOffer': next(
            ((load_json(turn['guidance_json']) or {}).get('relaxationOffer') for turn in turns if turn['role'] == 'assistant'),
            None,
        ),
        'uiCues': {},
    }
    lineage_ids = {item['versionId'] for item in _stage_lineage(database, session_id, version)}
    hypothesis_status_by_turn = {}
    for event in database.execute(
        "\n        SELECT payload_json FROM audit_events\n"
        "        WHERE session_id = ? AND event_type = 'intent_hypothesis'\n"
        "        ORDER BY id\n        ",
        (session_id,),
    ).fetchall():
        event_payload = load_json(event['payload_json']) or {}
        if event_payload.get('versionId') not in lineage_ids:
            continue
        event_turn_id = event_payload.get('turnId')
        if event_turn_id:
            hypothesis_status_by_turn[event_turn_id] = event_payload.get('status')
    guidance_sources = [
        (load_json(turn['guidance_json']) or {}, turn['id'])
        for turn in turns
        if turn['id'] not in superseded_assessment_turn_ids
    ]
    if accepted_opening is not None:
        guidance_sources.append((load_json(accepted_opening['guidance_json']) or {}, accepted_opening['assistant_turn_id']))
    latest_disagreement_seen = False
    for guidance, guidance_turn_id in guidance_sources:
        if hypothesis_status_by_turn.get(guidance_turn_id) == 'rejected':
            guidance = dict(guidance)
            guidance['intentHypothesis'] = None
            guidance['intentConfidence'] = None
        discussion_focus = guidance.get('followUpQuestion')
        if discussion_focus:
            if recent_guidance['discussionFocus'] is None:
                recent_guidance['discussionFocus'] = discussion_focus
            if (
                len(recent_guidance['discussionFocusHistory']) < 3
                and discussion_focus not in recent_guidance['discussionFocusHistory']
            ):
                recent_guidance['discussionFocusHistory'].append(discussion_focus)
        if recent_guidance['intentHypothesis'] is None and guidance.get('intentHypothesis'):
            recent_guidance['intentHypothesis'] = guidance['intentHypothesis']
        if recent_guidance['proposalOffer'] is None and guidance.get('proposalOffer'):
            recent_guidance['proposalOffer'] = guidance['proposalOffer']
        if not latest_disagreement_seen and 'disagreement' in guidance:
            latest_disagreement_seen = True
            disagreement = guidance.get('disagreement')
            recent_guidance['activeDisagreement'] = (
                disagreement if isinstance(disagreement, dict) and disagreement.get('status') == 'active' else None
            )
        for cue in guidance.get('uiCues') or []:
            cue_type = cue.get('type')
            if cue_type == 'tradeoff':
                cue_type = 'warning'
            if cue_type in {'warning', 'manual_edit', 'clarification'} and cue_type not in recent_guidance['uiCues']:
                recent_guidance['uiCues'][cue_type] = {
                    'text': cue.get('text'),
                    'evidenceSignature': guidance.get('evidenceSignature'),
                }
        if recent_guidance['intentHypothesis'] and recent_guidance['proposalOffer'] and len(recent_guidance['uiCues']) == 2:
            break
    latest_user_direction = _latest_substantive_design_direction(turns)
    progress_context = _progress_context(database, session_id, version, design_context, latest_user_direction, latest_play)
    progress_context['intentEvidence'] = pending_intent_evidence
    pending_reviews = []
    for row in database.execute(
        "\n        SELECT payload_json FROM audit_events\n"
        "        WHERE session_id = ? AND event_type = 'intent_hypothesis_feedback_pending'\n"
        "        ORDER BY id DESC LIMIT 8\n        ",
        (session_id,),
    ).fetchall():
        item = load_json(row['payload_json']) or {}
        if item.get('versionId') not in lineage_ids:
            continue
        pending_reviews.append({
            'hypothesisId': item.get('hypothesisId'),
            'candidate': str(item.get('candidateText') or '')[:600],
            'verdict': item.get('outcome'),
            'explanation': str(item.get('explanation') or '')[:600],
        })
        if len(pending_reviews) >= 2:
            break
    progress_context['pendingIntentReviews'] = pending_reviews
    challenge_rows = database.execute(
        "\n        SELECT payload_json FROM audit_events\n"
        "        WHERE session_id = ? AND event_type = 'proposal_challenge_started'\n"
        "        ORDER BY id DESC LIMIT 12\n        ",
        (session_id,),
    ).fetchall()
    challenge_context = None
    for challenge_row in challenge_rows:
        candidate = load_json(challenge_row['payload_json']) or {}
        if candidate.get('baseVersionId') == version['id'] or candidate.get('versionId') == version['id']:
            challenge_user = next(
                (turn for turn in turns if turn['role'] == 'user' and turn['request_id'] == candidate.get('messageKey')),
                None,
            )
            if challenge_user is not None:
                challenge_assistant = next(
                    (
                        turn
                        for turn in turns
                        if turn['role'] == 'assistant' and turn['request_id'] == candidate.get('messageKey')
                    ),
                    None,
                )
                challenge_boundary = (
                    challenge_assistant['sequence_number']
                    if challenge_assistant is not None
                    else challenge_user['sequence_number']
                )
                later_assistant = any(
                    (turn['role'] == 'assistant' and turn['sequence_number'] > challenge_boundary for turn in turns)
                )
                later_user = any(
                    (turn['role'] == 'user' and turn['sequence_number'] > challenge_boundary for turn in turns)
                )
                if not later_assistant and later_user:
                    hypotheses_row = database.execute(
                        "\n                        SELECT payload_json FROM audit_events\n"
                        "                        WHERE session_id = ?\n"
                        "                          AND event_type = 'proposal_challenge_hypotheses_recorded'\n"
                        "                          AND json_extract(payload_json, '$.messageKey') = ?\n"
                        "                        ORDER BY id DESC LIMIT 1\n                        ",
                        (session_id, candidate.get('messageKey')),
                    ).fetchone()
                    hypotheses = load_json(hypotheses_row['payload_json']) if hypotheses_row is not None else None
                    challenge_context = {**candidate, 'hypotheses': hypotheses}
            break
    evidence_payload = {
        'versionId': version['id'],
        'play': dict(latest_play) if latest_play is not None else None,
        'direction': latest_user_direction,
    }
    guidance_evidence_signature = hashlib.sha256(dump_json(evidence_payload).encode('utf-8')).hexdigest()[:16]
    stage_snapshot = build_stage_snapshot(
        current_rows,
        version_id=version['id'],
        stage_number=version['stage_number'],
        entity_bindings=entity_bindings,
    )
    if proposal_discovery and proposal_discovery.get('understanding'):
        try:
            validate_topic(proposal_discovery['understanding'], designer_sources(proposal_discovery), stage_snapshot)
        except ValueError:
            proposal_discovery.pop('understanding', None)
    if accepted_opening is not None and not any(
        (turn['content'] == _verified_proposal_message(session['language']) and turn['role'] == accepted_opening['role'] for turn in conversation)
    ):
        conversation.insert(0, {'role': accepted_opening['role'], 'content': _verified_proposal_message(session['language'])})
    durable_disagreement = design_context.get('activeDisagreement')
    if version['design_context_json']:
        recent_guidance['activeDisagreement'] = durable_disagreement
    elif isinstance(durable_disagreement, dict) and durable_disagreement.get('status') == 'active':
        recent_guidance['activeDisagreement'] = durable_disagreement
    return {
        'rows': current_rows,
        'validation': load_json(version['validation_json']),
        'conversation': conversation,
        'commitBinding': _message_commit_binding(database, session_id, version['id']),
        'stageContext': {
            'dialogueEvidence': build_dialogue_evidence(
                [dict(turn) for turn in proposal_turns if turn['id'] not in superseded_assessment_turn_ids],
                version['id'],
                proposal_discovery,
            ),
            'versionId': version['id'],
            'stageNumber': version['stage_number'],
            'source': version['source'],
            'initialDraftMethod': session['initial_draft_method'] if session is not None else None,
            'demoMode': bool(session['demo_mode']) if session is not None else False,
            'summary': version['summary'],
            'parentVersionId': version['parent_version_id'],
            'beforeRows': parent_rows if version['source'] == 'human_edit' else None,
            'afterRows': current_rows if version['source'] == 'human_edit' else None,
            'parentValidation': load_json(parent['validation_json']) if version['source'] == 'human_edit' and parent is not None else None,
            'diff': load_json(version['diff_json']),
            'changeSummary': change_summary,
            'mapFacts': build_map_facts(current_rows, parent_rows, entity_bindings),
            'stageSnapshot': stage_snapshot,
            'entityBindings': entity_bindings,
            'mapFingerprint': map_fingerprint(current_rows),
            'entityBindingFingerprint': entity_bindings.get('bindingFingerprint'),
            'recentGuidance': recent_guidance,
            'activeDisagreement': recent_guidance['activeDisagreement'],
            'challengeContext': challenge_context,
            'progressContext': progress_context,
            'designContext': design_context,
            'revisionDesignContext': revision_projection(design_context),
            'evaluatorDesignContext': evaluator_projection(design_context),
            'guidanceEvidenceSignature': guidance_evidence_signature,
            'clarificationQuestionCount': clarification_question_count,
            'clarificationQuestionBudget': max(0, 3 - clarification_question_count),
            'proposalDiscovery': proposal_discovery,
            'openingTurnId': accepted_opening['assistant_turn_id'] if accepted_opening is not None else None,
            'openingProposalId': accepted_opening['proposal_id'] if accepted_opening is not None else None,
        },
        'playSummary': {
            'status': latest_play['status'],
            'durationSeconds': latest_play['duration_seconds'],
            'moveCount': latest_play['move_count'],
            'pushCount': latest_play['push_count'],
            'restartCount': latest_play['restart_count'],
            'minimumMoves': latest_play['minimum_moves'],
            'minimumPushes': latest_play['minimum_pushes'],
            'finishedAt': latest_play['finished_at'],
        }
        if latest_play is not None
        else None,
    }


def _latest_substantive_design_direction(turns):
    design_terms = (
        '水',
        '箱',
        '目标',
        '墙',
        '通道',
        '路线',
        '推动',
        '绕行',
        '难度',
        '节奏',
        'water',
        'box',
        'crate',
        'target',
        'wall',
        'corridor',
        'route',
        'push',
        'difficulty',
        'pacing',
    )
    generic_continuations = {'继续', '展开', '展开讲讲', '可以', '好的', '好', '行', '试试', 'continue', 'go on', 'okay', 'ok', 'yes'}
    for turn in turns:
        if turn['role'] != 'user':
            continue
        content = sanitize_user_design_text(turn['content']).casefold()
        normalized = re.sub('[\\s,.!?。！？]+', '', content)
        if not content or normalized in generic_continuations:
            continue
        if any((term in content for term in design_terms)):
            return content
        if len(normalized) >= 4:
            return content
    return ''


def _proposal_discovery_from_turns(
    turns,
    version_id,
    proposal_request_keys=None,
    turn_understandings=None,
    *,
    services: CoCreationServices | None = None,
):
    """Rebuild one pending proposal topic from private turn metadata."""
    forced_keys = set(proposal_request_keys or ())
    reviewed = turn_understandings or {}
    ordered = sorted(turns or [], key=lambda turn: turn['sequence_number'])
    active = None
    source_turns = {turn['id']: turn for turn in ordered if turn['role'] == 'user'}
    conversation_prefix = []
    classifier = services.llm.classify_revision_request if services is not None else classify_revision_request
    for turn in ordered:
        content = str(turn['content'] or '').strip()
        if turn['role'] == 'user':
            conversation_prefix.append({'role': 'user', 'content': content})
            starts_new_topic = active is None
            request_key = turn['request_id'] if 'request_id' in turn.keys() else None
            if request_key in reviewed:
                acts = set(reviewed[request_key].get('acts') or [])
                protected_request = bool(protected_change_requested(reviewed[request_key], active is not None))
                direct_revision_request = (
                    reviewed[request_key].get('mapRelated') is not False
                    and bool(acts & {'revision_request', 'proposal_request', 'change_request'})
                    and not protected_request
                )
            else:
                protected_request = False
                revision_state, _ = classifier(conversation_prefix)
                direct_revision_request = revision_state in {'needs_direction', 'authorized', 'authorized_relaxed'}
            if (
                direct_revision_request
                or (
                    not protected_request
                    and (request_key not in reviewed or reviewed[request_key].get('mapRelated') is not False)
                    and (request_key in forced_keys)
                )
            ) and starts_new_topic:
                active = {
                    'topicId': turn['id'],
                    'sourceTurnId': turn['id'],
                    'initialRequest': content,
                    'sourceSequence': turn['sequence_number'],
                    'versionId': version_id,
                    'userEvidence': [content],
                    'userTurns': [{'id': turn['id'], 'content': content}],
                    'questions': [],
                    'answers': [],
                    'supplements': [],
                    'clarificationQuestionCount': 0,
                    'askedQuestionKeys': [],
                    'lastQuestionKey': None,
                    'status': 'clarifying',
                }
                interpretation = (reviewed.get(request_key) or {}).get('proposalUnderstanding') or {}
                cited = {
                    item.get('sourceTurnId')
                    for key in ('goals', 'editScope', 'focus', 'preserve')
                    for item in interpretation.get(key) or []
                }
                for source in sorted(
                    (source_turns[source_id] for source_id in cited if source_id in source_turns),
                    key=lambda source: source['sequence_number'],
                    reverse=True,
                ):
                    source_id = source['id']
                    if source is not None and source['sequence_number'] < turn['sequence_number']:
                        active['userTurns'].insert(0, {'id': source_id, 'content': source['content']})
                        active['userEvidence'].insert(0, source['content'])
            elif active is not None:
                active['userEvidence'].append(content)
                active['userTurns'].append({'id': turn['id'], 'content': content})
                unanswered_key = active.get('lastQuestionKey')
                answered_keys = {item.get('questionKey') for item in active.get('answers') or [] if isinstance(item, dict)}
                unanswered_questions = [q for q in active['questions'] if not q.get('answerTurnId')]
                if unanswered_questions:
                    for question in unanswered_questions:
                        question['answerTurnId'] = turn['id']
                        active['answers'].append({
                            'questionId': question['id'],
                            'questionKey': question['key'],
                            'questionText': question['text'],
                            'answerText': content,
                            'answerTurnId': turn['id'],
                        })
                elif unanswered_key and unanswered_key not in answered_keys:
                    active['answers'].append({
                        'questionKey': unanswered_key,
                        'questionText': active.get('lastQuestionText') or '',
                        'answerText': content,
                        'answerTurnId': turn['id'],
                    })
                else:
                    active['supplements'].append({'text': content, 'turnId': turn['id']})
                if active.get('status') in {'failed', 'blocked', 'revision_needed', 'retry_pending'}:
                    active['status'] = 'revision_needed'
            if active is not None and request_key in reviewed:
                decision = reviewed[request_key].get('optionDecision')
                if isinstance(decision, dict) and reviewed[request_key].get('versionId') == version_id:
                    active['optionDecision'] = decision
                interpretation = reviewed[request_key].get('proposalUnderstanding')
                if (
                    isinstance(interpretation, dict)
                    and reviewed[request_key].get('semanticPolicyVersion') == 1
                    and reviewed[request_key].get('versionId') == version_id
                ):
                    try:
                        active['understanding'] = validate_topic(interpretation, designer_sources(active), None)
                    except ValueError:
                        pass
            continue
        conversation_prefix.append({'role': turn['role'], 'content': content})
        if active is None or turn['role'] != 'assistant':
            continue
        try:
            guidance = load_json(turn['guidance_json']) or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            guidance = {}
        marker = guidance.get('proposalDiscovery')
        if isinstance(marker, dict) and marker.get('topicId') != active['topicId']:
            continue
        marker = marker if isinstance(marker, dict) else {}
        if isinstance(marker.get('optionSet'), dict):
            active['optionSet'] = marker['optionSet']
            active['optionQuestion'] = marker.get('optionQuestion')
        if isinstance(marker.get('optionDecision'), dict):
            active['optionDecision'] = marker['optionDecision']
        visible_questions = _visible_proposal_questions(content)
        for index, question_text in enumerate(visible_questions):
            question_id = f"{turn['id']}:{index + 1}"
            if not any((q['id'] == question_id for q in active['questions'])):
                active['questions'].append({
                    'id': question_id,
                    'turnId': turn['id'],
                    'key': marker.get('clarificationQuestionKey') or _infer_legacy_clarification_question_key(question_text),
                    'text': question_text,
                })
        active['observedQuestionCount'] = len(active['questions'])
        active['clarificationQuestionCount'] = max(
            active['clarificationQuestionCount'],
            int(marker.get('clarificationQuestionCount') or 0),
            min(3, active['observedQuestionCount']),
        )
        asked_keys = marker.get('askedQuestionKeys')
        if not isinstance(asked_keys, list):
            asked_keys = []
        question_key = str(
            (active['questions'][-1]['key'] if visible_questions else None)
            or marker.get('clarificationQuestionKey')
            or marker.get('lastQuestionKey')
            or ''
        ).strip()
        if not question_key and int(marker.get('clarificationQuestionCount') or 0) > 0:
            question_key = _infer_legacy_clarification_question_key(content)
        for key in [*asked_keys, question_key]:
            if key and key not in active['askedQuestionKeys']:
                active['askedQuestionKeys'].append(key)
        if question_key:
            active['lastQuestionKey'] = question_key
            active['lastQuestionText'] = str(marker.get('clarificationQuestionText') or content).strip()
            if visible_questions:
                active['lastQuestionText'] = visible_questions[-1]
        marker_status = marker.get('status') or active['status']
        if isinstance(marker.get('failureEnvelope'), dict):
            active['failureEnvelope'] = marker['failureEnvelope']
        if marker_status == 'proposal_ready' and not marker.get('hasValidatedCandidate'):
            marker_status = 'failed'
        if marker_status in {'proposal_ready', 'cancelled'}:
            active = None
            continue
        if marker_status in {'failed', 'blocked'}:
            marker_status = 'revision_needed'
        active['status'] = marker_status
    if active is None:
        return None
    active['userEvidence'] = [item for item in active['userEvidence'] if item]
    active['brief'] = '\n'.join(active['userEvidence'])
    return active


def _proposal_topic_reset_requested(content):
    """Only explicit topic changes replace an unfinished proposal discussion."""
    return bool(
        re.search(
            '(?:换个(?:方向|目标|话题)|另一个(?:方向|目标|问题)|放弃(?:刚才|这个)|重新开始|不讨论这个了|取消(?:这个)?方案|算了(?:吧)?|new\\s+(?:direction|goal|topic)|different\\s+(?:direction|goal)|drop\\s+this|cancel\\s+(?:this\\s+)?proposal|start\\s+over)',
            str(content or ''),
            flags=re.IGNORECASE,
        )
    )


def _infer_legacy_clarification_question_key(content):
    """Classify old question turns so read-time reconstruction avoids repeats."""
    text = str(content or '').casefold()
    if re.search('(?:B1|B2|哪个箱子|哪只箱子|which\\s+(?:box|crate))', text, re.IGNORECASE):
        return 'binding'
    if re.search('(?:推箱.*(?:步|次数)|陷阱|走错|绕行|顺序|运输|路线|push(?:es|ing)?|mistake|trap|detour|order|transport|route)', text, re.IGNORECASE):
        return 'mechanism'
    if re.search('(?:保留|不变|不能改|约束|preserve|keep|unchanged|constraint)', text):
        return 'preserve'
    if re.search('(?:时间|难度|思考|挑战|体验|time|difficulty|challenge|experience)', text):
        return 'experience_goal'
    return 'legacy_question'


def _semantic_proposal_clarification_spec(discovery, snapshot, language):
    interpretation = discovery['understanding']
    if interpretation['sufficient']:
        return None
    key = interpretation['nextQuestionDimension']
    count = min(3, int(discovery.get('clarificationQuestionCount') or 0))
    if count >= 3:
        return None
    asked = list(discovery.get('askedQuestionKeys') or [])
    question_key = key if key not in asked else f'{key}:{count + 1}'
    chinese = language == 'zh-CN'
    questions = {
        'visual_direction': (
            '你更希望这份布局呈现怎样的视觉秩序？',
            'What kind of visual organization would you like this layout to have?',
        ),
        'experience_goal': (
            '你最希望这项调整带来怎样的体验变化？',
            'What experience would you most like this change to create?',
        ),
        'edit_scope': (
            '你希望通过调整水域、内部墙体，还是两者的排布来尝试这个方向？',
            'Would you like to explore this through water, internal walls, or their arrangement together?',
        ),
        'mechanism': (
            '你希望哪种局部玩法变化来体现这个方向？',
            'What local play change should express this direction?',
        ),
        'binding': (
            '你希望先围绕哪个局部区域调整？没有偏好的话，我可以按可行性选择。',
            'Which local area would you like to focus on? I can choose by feasibility if you have no preference.',
        ),
        'preserve': (
            '这次调整中，你希望保留当前设计的哪一点？',
            'What part of the current design would you like to retain?',
        ),
    }
    intents = {
        'visual_direction': 'clarify visual organization, balance, repetition or spacing without assuming symmetry or gameplay changes',
        'experience_goal': "clarify the designer's desired experience without inventing a gameplay priority",
        'edit_scope': 'clarify which editable components carry the change; distinguish them from spatial references',
        'mechanism': 'clarify the explicitly requested gameplay effect without inventing quantities or locks',
        'binding': 'clarify a spatial focus only, never permission to move the referenced entity; focus is optional',
        'preserve': 'clarify an existing design preference without demanding a new hard constraint',
    }
    return {
        'questionKey': question_key,
        'semanticDimension': key,
        'questionIntent': intents[key],
        'fallbackQuestion': questions[key][0 if chinese else 1],
        'fallbackAcknowledgement': (
            '我会沿着你已经表达的方向继续考虑局部调整。'
            if chinese
            else 'I will continue exploring local changes along the direction you expressed.'
        ),
        'allowedEntityLabels': [str(x['id']) for x in (snapshot or {}).get('entities') or [] if x.get('id')],
        'countBefore': count,
        'askedQuestionKeys': asked,
        'topicContinued': len(discovery.get('userTurns') or []) > 1,
        'understanding': interpretation,
        'semanticPolicyVersion': 1,
    }


def _proposal_clarification_spec(discovery, snapshot, language):
    specification = _proposal_clarification_dimensions_spec(discovery, snapshot, language)
    if (
        specification is None
        and proposal_options.current_set(discovery, snapshot)
        and int(discovery.get('clarificationQuestionCount') or 0) < 3
    ):
        specification = {
            'countBefore': int(discovery.get('clarificationQuestionCount') or 0),
            'allowedEntityLabels': [str(item['id']) for item in (snapshot or {}).get('entities') or [] if item.get('id')],
            'topicContinued': True,
            'semanticPolicyVersion': 1,
        }
    if specification is None:
        return None
    return {
        **specification,
        'optionPolicyVersion': proposal_options.VERSION,
        'questionKey': f"option_choice_{int(discovery.get('clarificationQuestionCount') or 0) + 1}",
        'fallbackQuestion': '',
        'fallbackAcknowledgement': '',
        'questionIntent': 'compare materially different, permission-safe proposal directions before choosing a route',
    }


def _proposal_clarification_dimensions_spec(discovery, snapshot, language):
    """Choose one server-owned question for an active proposal topic."""
    if not discovery or discovery.get('status') != 'clarifying':
        return None
    understanding = discovery.get('understanding')
    if understanding:
        return _semantic_proposal_clarification_spec(discovery, snapshot, language)
    evidence = '\n'.join(discovery.get('userEvidence') or []).strip()
    lowered = evidence.casefold()
    spatial_emptiness = bool(
        re.search(
            '(?:太空了?|空旷|空荡|显得空|过于空|比较空|别那么空|不要那么空|不那么空|too\\s+empty|feels?\\s+empty|sparse|vacant)',
            lowered,
            re.IGNORECASE,
        )
    )
    spatial_region = next(
        (name for name in ('左下角', '右下角', '左上角', '右上角', '中间区域', '中央区域') if name in evidence),
        '这片区域',
    )
    asked = [str(key).strip() for key in discovery.get('askedQuestionKeys') or [] if str(key).strip()]
    count_before = max(0, min(3, int(discovery.get('clarificationQuestionCount') or 0)))
    has_goal = bool(
        re.search(
            '(?:时间|难度|思考|挑战|体验|节奏|压力|选择|视觉|空间分布|平衡|外观|time|difficulty|challenge|experience|pacing|pressure|choice|visual|spatial|balance|appearance)',
            lowered,
            re.IGNORECASE,
        )
    )
    has_mechanism = bool(
        re.search(
            '(?:推箱.*(?:步|次数)|总步数|陷阱|走错|绕行|顺序|运输|路线|死角|push(?:es|ing)?|move\\s+count|trap|mistake|detour|order|transport|route|deadlock)',
            lowered,
            re.IGNORECASE,
        )
    )
    labels = sorted(set((label.upper() for label in re.findall('\\b(?:P|B\\d+|T\\d+)\\b', evidence, re.IGNORECASE))))
    entities = (snapshot or {}).get('entities') or []
    box_labels = sorted(
        (str(item.get('id') or '').strip() for item in entities if item.get('kind') == 'box' and str(item.get('id') or '').strip())
    )
    candidates = []
    if not has_goal:
        candidates.append('experience_goal')
    if not has_mechanism:
        candidates.append('mechanism')
    if not labels and len(box_labels) > 1:
        candidates.append('binding')
    candidates.append('preserve')
    question_key = next((key for key in candidates if key not in asked), None)
    if question_key is None:
        question_key = f'implementation_detail_{count_before + 1}'
    chinese = language == 'zh-CN'
    if question_key == 'experience_goal':
        if spatial_emptiness:
            question = (
                f'你希望{spatial_region}主要改善视觉上的空间分布，还是加入会影响路线或推箱节奏的结构？'
                if chinese
                else 'Should this area mainly improve visual balance, or gain structure that affects routes or push rhythm?'
            )
        else:
            question = (
                '完成你提出的这项调整后，你最希望玩家在哪个具体游玩时刻感受到变化？'
                if chinese
                else 'After making the change you described, at what specific play moment should the player feel its effect most?'
            )
    elif question_key == 'mechanism':
        question = (
            '你希望这项调整主要通过什么局部玩法机制产生效果？'
            if chinese
            else 'What local play mechanism should make this change take effect?'
        )
    elif question_key == 'binding' and len(box_labels) > 1:
        first, second = box_labels[:2]
        question = (
            f'如果只先改一个局部，你希望围绕 {first} 还是 {second} 增加运输长度？这会决定方案绑定哪条箱子路线。'
            if chinese
            else f'If we revise one local area first, should the longer transport center on {first} or {second}? That determines which box route the proposal binds.'
        )
    else:
        question_key = 'preserve' if question_key == 'preserve' else question_key
        question = (
            '为了增加这部分挑战，当前布局中哪一种体验必须保留不变？'
            if chinese
            else 'While adding this challenge, which part of the current play experience must remain unchanged?'
        )
    latest = (discovery.get('userEvidence') or [''])[-1].strip()
    if chinese:
        if re.search('(?:增加|提高).{0,8}推箱.{0,8}(?:步|次数)', latest):
            acknowledgement = '明白，你希望通过增加实际推箱次数来延长解题时间。'
        elif re.search('(?:陷阱|走错|误导)', latest):
            acknowledgement = '明白，你希望额外的思考时间主要来自可识别但需要判断的局部风险。'
        else:
            acknowledgement = '我会沿着你刚才确认的方向继续收敛方案。'
    elif re.search('(?:increase|more).{0,16}(?:push|move)', latest, re.IGNORECASE):
        acknowledgement = 'Understood: you want the longer solve time to come from additional box pushes.'
    elif re.search('(?:trap|mistake|mislead)', latest, re.IGNORECASE):
        acknowledgement = 'Understood: you want the added thinking time to come from a readable local risk.'
    else:
        acknowledgement = 'I will keep narrowing the proposal around the direction you just confirmed.'
    fallback_questions = {
        'experience_goal': question
        if spatial_emptiness
        else (
            '完成你提出的调整后，你最希望玩家在哪个具体游玩时刻感受到变化？'
            if chinese
            else 'After making the change you described, at what specific play moment should the player feel its effect most?'
        ),
        'mechanism': (
            '你希望这项调整主要通过什么局部玩法机制产生效果？'
            if chinese
            else 'What local play mechanism should make this change take effect?'
        ),
        'binding': (
            '你希望先围绕哪个箱子或局部区域增加运输长度？'
            if chinese
            else 'Which box or local area should carry the longer transport first?'
        ),
        'preserve': (
            '增加这部分挑战时，当前体验中的哪一点必须保持不变？'
            if chinese
            else 'Which part of the current play experience must remain unchanged while adding this challenge?'
        ),
    }
    question = fallback_questions.get(question_key, question)
    plain_questions = {
        'mechanism': (
            '你更想让玩家多换几次箱子来处理，还是推着同一个箱子走更远一些？'
            if chinese
            else 'Would you prefer more switching between boxes, or longer pushing of one box?'
        ),
        'binding': (
            '你更想先调整哪个箱子周围？没有偏好的话，我可以根据可行性来选择。'
            if chinese
            else 'Which box would you prefer to focus on first? I can choose if you have no preference.'
        ),
        'preserve': (
            '这次调整中，你更希望保留当前玩法的哪一点？'
            if chinese
            else 'Which part of the current play would you prefer to retain?'
        ),
    }
    question = plain_questions.get(question_key, question)
    if question_key == 'binding':
        question = (
            '没有特别偏好的话，我可以根据可行性选择。你更想先调整哪个箱子周围？'
            if chinese
            else 'I can choose a feasible focus if you have no preference. Which box would you prefer to focus on first?'
        )
    return {
        'questionKey': question_key,
        'fallbackQuestion': question,
        'fallbackAcknowledgement': acknowledgement,
        'questionIntent': {
            'experience_goal': (
                'clarify whether the sparse area should improve visual balance or affect routes and push rhythm'
                if spatial_emptiness
                else 'clarify the desired player experience after carrying out the stated direction without changing or reversing that direction'
            ),
            'mechanism': 'clarify the local play mechanism that should create the requested effect',
            'binding': 'clarify which existing entity or local area should carry the change',
            'preserve': 'ask which current play quality the designer would prefer to retain without a new strict requirement',
        }.get(question_key, 'clarify one remaining implementation detail'),
        'allowedEntityLabels': [
            str(item.get('id') or '').strip() for item in entities if str(item.get('id') or '').strip()
        ]
        if (snapshot or {}).get('identityStatus') == 'exact'
        else [],
        'countBefore': count_before,
        'askedQuestionKeys': asked,
        'topicContinued': len(discovery.get('userEvidence') or []) > 1,
    }


def _proposal_discovery_has_unique_anchor(discovery, snapshot):
    evidence = '\n'.join((discovery or {}).get('userEvidence') or [])
    labels = set(re.findall('(?<![A-Za-z0-9])(?:P|B\\d+|T\\d+)(?![A-Za-z0-9])', evidence, flags=re.IGNORECASE))
    if labels:
        return (snapshot or {}).get('identityStatus') == 'exact'
    lowered = evidence.casefold()
    if re.search(
        '(?:左下角|右下角|左上角|右上角|中间区域|中央区域|lower[-\\s]?left|lower[-\\s]?right|upper[-\\s]?left|upper[-\\s]?right|central area)',
        lowered,
        re.IGNORECASE,
    ):
        return bool(snapshot)
    entities = (snapshot or {}).get('entities') or []
    if re.search('(?:箱子|box|crate)', lowered):
        return sum((item.get('kind') == 'box' for item in entities)) == 1
    if re.search('(?:目标|target|goal)', lowered):
        return sum((item.get('kind') == 'target' for item in entities)) == 1
    if re.search('(?:\\u73a9\\u5bb6|player)', lowered):
        return (snapshot or {}).get('identityStatus') == 'exact' and sum((item.get('kind') == 'player' for item in entities)) == 1
    return False


def _proposal_discovery_is_sufficient(discovery, snapshot):
    if not discovery or (snapshot or {}).get('identityStatus') not in {None, 'exact'}:
        return False
    return _proposal_discovery_has_unique_anchor(discovery, snapshot)


def _is_vague_aesthetic_revision(content):
    text = str(content or '')
    return bool(
        re.search(
            "(?:\\u4e0d\\u597d\\u770b|\\u4e0d\\u7f8e\\u89c2|\\u770b\\u8d77\\u6765\\u4e0d\\u5bf9|太空了?|空旷|空荡|显得空|过于空|比较空|别那么空|不要那么空|不那么空|\\b(?:ugly|doesn'?t look (?:good|right)|not aesthetic|too\\s+empty|feels?\\s+empty|sparse|vacant)\\b)",
            text,
            flags=re.IGNORECASE,
        )
    ) and not bool(
        re.search(
            '(?:\\u8f6e\\u5ed3|\\u5730\\u5f62|\\u5206\\u5272|\\u5bf9\\u79f0|\\u901a\\u9053|\\u8def\\u7ebf|\\u8f6c\\u6298|\\u8282\\u594f|\\u6c34\\u57df|\\u5899|\\u7bb1\\u5b50|\\u76ee\\u6807|\\b(?:outline|geometry|composition|symmetry|corridor|route|turn|rhythm|water|wall|box|target)\\b)',
            text,
            flags=re.IGNORECASE,
        )
    )


def _adaptive_revision_routing(
    content,
    user_map_claims,
    snapshot,
    *,
    proposal_discovery=None,
    turn_understanding=None,
    services: CoCreationServices | None = None,
):
    """Choose proposal expansion versus one targeted clarification question."""
    text = str(content or '').strip()
    if not text:
        return 'none'
    discovery = proposal_discovery or {}
    if (turn_understanding or {}).get('conversationOnly'):
        if discovery:
            raise ApiError(502, 'MODEL_RESPONSE_INVALID', 'Retry the original proposal answer.', retryable=True)
        return 'none'
    if isinstance(turn_understanding, dict):
        if turn_understanding.get('mapRelated') is False:
            return 'none'
        acts = set(turn_understanding.get('acts') or [])
        requested = bool(acts & {'revision_request', 'proposal_request', 'change_request'})
        if requested and protected_change_requested(turn_understanding, bool(discovery)):
            return 'protected_request'
        if not requested and not discovery:
            return 'none'
        interpretation = discovery.get('understanding') or turn_understanding.get('proposalUnderstanding')
        if interpretation:
            if discovery and _proposal_topic_reset_requested(text):
                return 'proposal_cancelled'
            option_set = proposal_options.current_set(discovery, snapshot)
            if option_set:
                if turn_understanding.get('optionContextSignature') != proposal_options.context_signature(discovery):
                    raise ApiError(409, 'PROPOSAL_STALE', 'The proposal question changed. Answer the current question.')
                decision = discovery.get('optionDecision') or {}
                if decision.get('selection') == 'unclear' and not requested and acts & {'explanation_request', 'idea_request'}:
                    return 'none'
                remaining_options = [
                    item
                    for item in proposal_options.all_options(option_set)
                    if item['id'] not in decision.get('rejectedOptionIds', [])
                ]
                count = int(discovery.get('clarificationQuestionCount') or 0)
                if decision.get('selection') == 'revise' and interpretation.get('sufficient'):
                    return 'proposal_conservative'
                if decision.get('selection') == 'revise' or not remaining_options:
                    return 'needs_clarification' if count < 3 else 'proposal_blocked'
                if decision.get('selection') == 'unclear' and len(remaining_options) > 1 and count < 3:
                    return 'needs_clarification'
                if len(remaining_options) == 1:
                    return 'proposal_conservative'
                if decision.get('selection') == 'explicit' and len(decision.get('preferredOptionIds') or []) == 1:
                    return 'proposal_conservative'
            if (
                interpretation.get('sufficient')
                or int(discovery.get('clarificationQuestionCount') or 0) >= 3
                or discovery.get('status') in {'revision_needed', 'retry_pending', 'planning'}
            ):
                return 'proposal' if _proposal_discovery_has_unique_anchor(discovery, snapshot) else 'proposal_conservative'
            return 'needs_clarification'
        if requested and not discovery:
            return 'needs_clarification'
    discovery_status = str(discovery.get('status') or '')
    question_count = int(discovery.get('clarificationQuestionCount') or 0)
    if discovery and _proposal_topic_reset_requested(text):
        return 'proposal_cancelled'
    if discovery and (question_count >= 3 or discovery_status in {'revision_needed', 'retry_pending', 'planning'}):
        return 'proposal' if _proposal_discovery_has_unique_anchor(discovery, snapshot) else 'proposal_conservative'
    if (
        isinstance(turn_understanding, dict)
        and isinstance(turn_understanding.get('changes'), list)
        and turn_understanding.get('directionSufficient')
    ):
        if not (user_map_claims or {}).get('conflicts'):
            return 'proposal' if _proposal_discovery_has_unique_anchor(discovery, snapshot) else 'proposal_conservative'
    if discovery and discovery_status == 'clarifying' and question_count == 0:
        return 'needs_clarification'
    if (user_map_claims or {}).get('conflicts'):
        return 'needs_clarification'
    if turn_understanding is None and _is_vague_aesthetic_revision(text):
        return 'needs_clarification'
    if discovery and discovery.get('status') == 'clarifying':
        if (
            isinstance(turn_understanding, dict)
            and turn_understanding.get('directionSufficient')
            and discovery.get('answers')
        ):
            return 'proposal' if _proposal_discovery_has_unique_anchor(discovery, snapshot) else 'proposal_conservative'
        if _proposal_discovery_is_sufficient(discovery, snapshot):
            return 'proposal'
        return 'needs_clarification'
    if _guidance_confusion_request(text):
        return 'confused'
    classifier = services.llm.classify_revision_request if services is not None else classify_revision_request
    revision_state, _ = classifier([{'role': 'user', 'content': text}])
    direct_edit = revision_state in {'needs_direction', 'authorized', 'authorized_relaxed'}
    if not direct_edit:
        return 'none'
    concrete_change = bool(
        re.search(
            '(?:\\u628a|\\u5c06|\\u8ba9|\\u5e0c\\u671b|\\u60f3(?:\\u8981|\\u8ba9|\\u628a)?|\\u589e\\u52a0|\\u51cf\\u5c11|\\u6269\\u5c55|\\u6269\\u5927|\\u7f29\\u5c0f|\\u6539\\u6210|\\u79fb\\u52a8|\\u8c03\\u6574|\\u4fee\\u6539).{0,100}(?:\\u6c34\\u57df|\\u6c34|\\u5899|\\u5899\\u4f53|\\u7bb1\\u5b50|\\u76ee\\u6807|\\u533a\\u57df|\\u901a\\u9053|\\u8def\\u7ebf|water|wall|box|target|area|corridor|route).{0,80}(?:\\u5411|\\u5f80|\\u5230|\\u6269\\u5c55|\\u6269\\u5927|\\u79fb\\u52a8|\\u6539|\\u8c03\\u6574|up|down|left|right|add|remove|move|extend|expand|change|adjust)',
            text,
            flags=re.IGNORECASE,
        )
    )
    has_direction = _contains_user_design_direction(text) or concrete_change
    if not has_direction:
        return 'needs_clarification'
    referenced_labels = re.findall('\\b(?:P|B\\d+|T\\d+)\\b', text, flags=re.IGNORECASE)
    if referenced_labels and (snapshot or {}).get('identityStatus') != 'exact':
        return 'needs_clarification'
    vague_region = bool(re.search('(?:一块|某个|一个|其中一个|a\\s+water|one\\s+of\\s+the\\s+water|some\\s+water)', text, flags=re.IGNORECASE))
    vague_region = vague_region and bool(
        re.search(
            '(?:\\u6c34\\u57df|\\u6c34|\\u5899|\\u5899\\u4f53|\\u7bb1\\u5b50|\\u76ee\\u6807|\\u533a\\u57df|\\u901a\\u9053|water|wall|box|target|area|corridor)',
            text,
            flags=re.IGNORECASE,
        )
    )
    water_cells = {(item.get('row'), item.get('column')) for item in (snapshot or {}).get('waterCells', []) if isinstance(item, dict)}
    components = 0
    while water_cells:
        components += 1
        pending = [water_cells.pop()]
        while pending:
            row, column = pending.pop()
            for neighbor in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
                if neighbor in water_cells:
                    water_cells.remove(neighbor)
                    pending.append(neighbor)
    if vague_region:
        if re.search('(?:\\u6c34\\u57df|\\u6c34|water)', text, flags=re.IGNORECASE) and components != 1:
            return 'needs_clarification'
        entity_counts = {
            'box': sum((item.get('kind') == 'box' for item in (snapshot or {}).get('entities', []))),
            'target': sum((item.get('kind') == 'target' for item in (snapshot or {}).get('entities', []))),
        }
        if re.search('(?:\\u7bb1\\u5b50|box)', text, flags=re.IGNORECASE) and entity_counts['box'] != 1:
            return 'needs_clarification'
        if re.search('(?:\\u76ee\\u6807|target)', text, flags=re.IGNORECASE) and entity_counts['target'] != 1:
            return 'needs_clarification'
    return 'proposal'


def _proposal_feasibility_profile(snapshot, rows, design_context=None, baseline_validation=None):
    """Build a compact, read-only capability profile from current Stage facts."""
    snapshot = snapshot or {}
    water_cells = {
        (item.get('row'), item.get('column'))
        for item in snapshot.get('waterCells') or []
        if isinstance(item, dict)
    }
    remaining = set(water_cells)
    component_sizes = []
    while remaining:
        seed = remaining.pop()
        pending = [seed]
        size = 0
        while pending:
            row, column = pending.pop()
            size += 1
            for neighbor in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    pending.append(neighbor)
        component_sizes.append(size)
    entities = [
        {'id': item.get('id'), 'kind': item.get('kind'), 'row': item.get('row'), 'column': item.get('column')}
        for item in snapshot.get('entities') or []
        if isinstance(item, dict)
    ]
    editable_cells = sum((tile not in {' '} for row in rows or [] for tile in row))
    return {
        'schemaVersion': 1,
        'mapFingerprint': snapshot.get('mapFingerprint'),
        'identityStatus': snapshot.get('identityStatus'),
        'bindableEntities': entities,
        'availableOperators': ['add_wall', 'remove_wall', 'add_water', 'remove_water'],
        'protected': ['outer_shell', 'player', 'boxes', 'targets', 'unrelated_areas', 'entity_overlap'],
        'waterComponents': len(component_sizes),
        'waterComponentSizes': sorted(component_sizes, reverse=True),
        'editableCellCount': editable_cells,
        'baselineSolverMetrics': {
            key: (baseline_validation or {}).get(key)
            for key in ('solutionSteps', 'solutionPushes', 'minimumPushes')
            if (baseline_validation or {}).get(key) is not None
        },
        'confirmedPreserveItems': [
            item.get('statement')
            for item in (design_context or {}).get('constraints') or []
            if isinstance(item, dict) and item.get('status') == 'confirmed'
        ][:8],
    }


def _clarification_question_count(turns):
    """Count the bounded clarification questions already asked in this Stage."""
    total = 0
    for turn in turns or []:
        if turn['role'] != 'assistant':
            continue
        try:
            guidance = load_json(turn['guidance_json']) or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            guidance = {}
        marker = guidance.get('proposalDiscovery')
        if isinstance(marker, dict):
            total = max(total, int(marker.get('clarificationQuestionCount') or 0))
            continue
        if guidance.get('move') != 'clarify_intent':
            continue
        total += str(turn['content'] or '').count('?')
        total += str(turn['content'] or '').count('？')
        if total >= 3:
            return 3
    return total


def _allow_adaptive_proposal_completion(content, user_map_claims, snapshot, clarification_question_count):
    """Allow conservative completion after the clarification budget is used."""
    try:
        clarification_question_count = int(clarification_question_count or 0)
    except (TypeError, ValueError):
        clarification_question_count = 0
    if clarification_question_count < 3:
        return False
    text = str(content or '').strip()
    if not text or (user_map_claims or {}).get('conflicts'):
        return False
    if _guidance_confusion_request(text):
        return False
    advice_request = _guidance_advice_request(text)
    direct_change = bool(
        re.search(
            '(?:\\u6539|\\u8c03\\u6574|\\u6269\\u5c55|\\u6269\\u5927|\\u79fb\\u52a8|\\u6539\\u6210|\\b(?:change|adjust|extend|expand|move|relocate)\\b)',
            text,
            flags=re.IGNORECASE,
        )
    )
    if not (advice_request or direct_change):
        return False
    referenced_labels = re.findall('\\b(?:P|B\\d+|T\\d+)\\b', text, flags=re.IGNORECASE)
    if referenced_labels:
        return (snapshot or {}).get('identityStatus') == 'exact'
    outcome_direction = bool(
        re.search(
            '(?:\\u7ed5\\u884c|\\u538b\\u529b|\\u9009\\u62e9|\\u8282\\u594f|\\u53ef\\u8bfb|\\u72b9\\u8c6b|\\u63a8\\u7bb1\\u987a\\u5e8f|\\u8def\\u7ebf\\u8fa8\\u8bc6|\\u96be\\u5ea6|\\u6311\\u6218|detour|pressure|choice|pacing|readab(?:le|ility)|hesitation|push\\s+order|difficulty|challenge)',
            text,
            flags=re.IGNORECASE,
        )
    )
    object_terms = {
        'water': bool(re.search('(?:\\u6c34\\u57df|\\u6c34|water)', text, flags=re.IGNORECASE)),
        'box': bool(re.search('(?:\\u7bb1\\u5b50|box|crate)', text, flags=re.IGNORECASE)),
        'target': bool(re.search('(?:\\u76ee\\u6807|target)', text, flags=re.IGNORECASE)),
        'wall': bool(re.search('(?:\\u5899\\u4f53|\\u5899|wall)', text, flags=re.IGNORECASE)),
        'region': bool(
            re.search(
                '(?:\\u533a\\u57df|\\u901a\\u9053|\\u8def\\u7ebf|\\u4e00\\u4fa7|\\u5de6\\u4fa7|\\u53f3\\u4fa7|\\u4e0a\\u65b9|\\u4e0b\\u65b9|area|corridor|route|left|right|upper|lower)',
                text,
                flags=re.IGNORECASE,
            )
        ),
    }
    if object_terms['box']:
        box_count = sum((item.get('kind') == 'box' for item in (snapshot or {}).get('entities', []) if isinstance(item, dict)))
        if box_count != 1:
            return False
    if object_terms['target']:
        target_count = sum((item.get('kind') == 'target' for item in (snapshot or {}).get('entities', []) if isinstance(item, dict)))
        if target_count != 1:
            return False
    water_cells = {(item.get('row'), item.get('column')) for item in (snapshot or {}).get('waterCells', []) if isinstance(item, dict)}
    components = 0
    while water_cells:
        components += 1
        pending = [water_cells.pop()]
        while pending:
            row, column = pending.pop()
            for neighbor in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
                if neighbor in water_cells:
                    water_cells.remove(neighbor)
                    pending.append(neighbor)
    if object_terms['water'] and components != 1:
        return False
    if object_terms['wall'] and not outcome_direction:
        return False
    return outcome_direction or any(object_terms.values())


def _visible_proposal_questions(message):
    return [
        sentence.strip()
        for sentence in re.split('(?<=[.!?。！？])\\s*', str(message or '').strip())
        if sentence.strip().endswith(('?', '？'))
    ]


def _record_disagreement_event(
    database,
    session_id,
    version_id,
    turn_id,
    guidance,
    *,
    services: CoCreationServices | None = None,
):
    disagreement = (guidance or {}).get('disagreement')
    if not isinstance(disagreement, dict):
        return
    status = disagreement.get('status')
    if status not in {'active', 'acknowledged', 'resolved'}:
        return
    if disagreement.get('subject') == 'human_edit':
        active = load_design_context(database, session_id, version_id).get('activeDisagreement')
        identity = component_recovery.disagreement_identity(database, session_id, version_id, active)
        if identity or not active or active.get('subject') != 'human_edit':
            disagreement['disagreementId'] = identity or disagreement.get('disagreementId') or uuid.uuid4().hex
    previous = database.execute(
        "\n        SELECT event_type FROM audit_events\n"
        "        WHERE session_id = ?\n"
        "          AND event_type IN ('disagreement_started', 'disagreement_updated',\n"
        "                             'disagreement_acknowledged', 'disagreement_resolved')\n"
        "          AND json_extract(payload_json, '$.versionId') = ?\n"
        "        ORDER BY id DESC LIMIT 1\n        ",
        (session_id, version_id),
    ).fetchone()
    if status == 'active':
        event_type = (
            'disagreement_updated'
            if previous is not None and previous['event_type'] not in {'disagreement_resolved', 'disagreement_acknowledged'}
            else 'disagreement_started'
        )
    elif status == 'acknowledged':
        event_type = 'disagreement_acknowledged'
    else:
        event_type = 'disagreement_resolved'
    _emit_event(
        database,
        session_id,
        event_type,
        {
            'versionId': version_id,
            'turnId': turn_id,
            'subject': disagreement.get('subject'),
            'resolution': disagreement.get('resolution'),
            'disagreement': disagreement,
        },
        services=services,
    )
    if status == 'acknowledged':
        return
    record_intent_evidence(
        database,
        session_id,
        version_id,
        'disagreement_' + status,
        disagreement.get('coreDisagreement') or disagreement.get('nextQuestion') or '',
        turn_id=turn_id,
        details={
            'subject': disagreement.get('subject'),
            'resolution': disagreement.get('resolution'),
            'userPosition': disagreement.get('userPosition'),
            'aiPosition': disagreement.get('aiPosition'),
        },
        services=services,
    )


def _public_guidance(guidance):
    """Remove backend-only semantic memory patches before storing/rendering a turn."""
    result = dict(guidance or {})
    dashboard_node = {}
    if result.get('_intentHypothesisId'):
        dashboard_node['intentHypothesisId'] = result['_intentHypothesisId']
        review_state = result.get('_intentReviewState') or {}
        if review_state.get('interactionMode') == 'conflict_choice':
            dashboard_node['intentConflict'] = True
    if isinstance(result.get('proposalDiscovery'), dict):
        topic_id = result['proposalDiscovery'].get('topicId')
        if topic_id:
            dashboard_node['proposalTopicId'] = topic_id
    if dashboard_node:
        result['dashboardNode'] = dashboard_node
    result.pop('designContextPatch', None)
    result.pop('designContextPatchError', None)
    result.pop('openingRecovery', None)
    result.pop('openingPresentation', None)
    result.pop('_intentHypothesisId', None)
    result.pop('_intentReviewState', None)
    result.pop('_intentSemanticClaims', None)
    result.pop('_intentDecision', None)
    result.pop('_intentCandidateReview', None)
    result.pop('_intentDraftAudit', None)
    result.pop('_intentReviewAudit', None)
    result.pop('_humanEditEngagement', None)
    result.pop('_stateTransitionDeferred', None)
    result.pop('manualEditReview', None)
    offer = result.get('proposalOffer')
    if isinstance(offer, dict) and ('executionBrief' in offer or 'revisionPlan' in offer):
        offer = dict(offer)
        offer.pop('executionBrief', None)
        offer.pop('revisionPlan', None)
        result['proposalOffer'] = offer
    return result


def _design_context_guidance(guidance):
    """Keep semantic patches while excluding display-only and execution metadata."""
    result = dict(guidance or {})
    result.pop('coordinateLinks', None)
    result.pop('proposalDiscovery', None)
    result.pop('openingPresentation', None)
    offer = result.get('proposalOffer')
    if isinstance(offer, dict) and ('executionBrief' in offer or 'revisionPlan' in offer or 'proposalPresentation' in offer):
        offer = dict(offer)
        offer.pop('executionBrief', None)
        offer.pop('revisionPlan', None)
        offer.pop('proposalPresentation', None)
        result['proposalOffer'] = offer
    return result


def _extract_conservative_map_question(content):
    """Extract one clearly map-specific question when the model omitted a patch."""
    map_anchor_pattern = re.compile(
        '(?:\\b(?:B\\d+|T\\d+|P|route|path|corridor|box|target|wall|water|push|position|tile|cell)\\b|[\\u73a9\\u5bb6\\u7bb1\\u5b50\\u76ee\\u6807\\u5899\\u6c34\\u57df\\u901a\\u9053\\u8def\\u7ebf\\u8def\\u5f84\\u63a8\\u52a8\\u8d77\\u70b9]|[\\(\\uFF08]\\s*\\d{1,2}\\s*[,，]\\s*\\d{1,2}\\s*[\\)\\uFF09])',
        flags=re.IGNORECASE,
    )
    design_marker_pattern = re.compile(
        '(?:\\b(?:route|path|corridor|box|target|wall|water|push|layout|difficulty|readable|choice|order|position|keep|change|preserve)\\b|\\u8def\\u7ebf|\\u8def\\u5f84|\\u901a\\u9053|\\u7bb1\\u5b50|\\u76ee\\u6807|\\u5899|\\u6c34|\\u63a8\\u52a8|\\u5e03\\u5c40|\\u96be\\u5ea6|\\u53ef\\u8bfb|\\u987a\\u5e8f|\\u4f4d\\u7f6e|\\u4fdd\\u7559|\\u8c03\\u6574|\\u6539\\u52a8)',
        flags=re.IGNORECASE,
    )
    segments = re.split('\\s*(?:\\n+|(?<=[.!?\\u3002\\uFF01\\uFF1F]))\\s*', str(content or ''))
    for segment in segments:
        question = re.sub('\\s+', ' ', segment).strip()
        if not question.endswith(('?', '？')):
            continue
        if not question or not map_anchor_pattern.search(question):
            continue
        if not is_design_level_question(question):
            continue
        if not design_marker_pattern.search(question) and not re.search('[\\(\\uFF08]\\s*\\d{1,2}\\s*[,，]\\s*\\d{1,2}\\s*[\\)\\uFF09]', question):
            continue
        return question[:1200]
    return None


def _extract_visible_questions(assistant_content, guidance=None):
    """Return every question that survived into visible non-intent output."""
    guidance = guidance if isinstance(guidance, dict) else {}
    visible_values = [assistant_content]
    follow_up = guidance.get('followUpQuestion')
    if follow_up:
        visible_values.append(follow_up)
    disagreement = guidance.get('disagreement')
    if isinstance(disagreement, dict):
        visible_values.extend([disagreement.get('coreDisagreement'), disagreement.get('nextQuestion')])
    offer = guidance.get('proposalOffer')
    if isinstance(offer, dict):
        visible_values.extend([offer.get('summary'), offer.get('rationale')])
    for cue in guidance.get('uiCues') or []:
        if isinstance(cue, dict) and cue.get('type') != 'intent':
            visible_values.append(cue.get('text'))
    questions = []
    seen = set()
    for value in visible_values:
        segments = re.split('\\s*(?:\\n+|(?<=[.!?\\u3002\\uFF01\\uFF1F]))\\s*', str(value or ''))
        for segment in segments:
            question = re.sub('\\s+', ' ', segment).strip()
            if not question.endswith(('?', '？')):
                continue
            key = re.sub('\\s+', ' ', question).strip('?？').casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            questions.append(question[:1200])
    return questions


def _update_design_context_from_turn(
    database,
    session_id,
    version_id,
    user_content,
    turn_id,
    guidance,
    assistant_content=None,
    allow_progress=True,
    answered_question_ids=None,
    answer_turn_id=None,
    proposal_discovery=None,
    *,
    services: CoCreationServices | None = None,
):
    """Merge server-owned memory after a normal chat or Stage review."""
    guidance = guidance or {}
    patch = guidance.get('designContextPatch')
    patch_error = guidance.get('designContextPatchError')
    if patch_error:
        _emit_event(
            database,
            session_id,
            'design_context_patch_rejected',
            {'versionId': version_id, 'turnId': turn_id, 'error': str(patch_error)[:500]},
            services=services,
        )
        patch = None
    previous = load_design_context(database, session_id, version_id)
    candidate_review = guidance.get('_intentCandidateReview') or {}
    lazy_updates = candidate_review.get('reviewedActiveClaims') or [] if guidance.get('intentHypothesis') else []
    for update in lazy_updates:
        target = next(
            (
                item
                for item in previous.get('intentHypotheses', [])
                if item.get('id') == update.get('hypothesisId') and item.get('status') == 'confirmed'
            ),
            None,
        )
        if target is None:
            continue
        current_claims = target.get('semanticClaims') or []
        if current_claims and all(
            (claim.get('semanticSource') == 'kimi_reviewed' for claim in current_claims if isinstance(claim, dict))
        ):
            continue
        target['semanticClaims'] = [
            {
                **claim,
                'subject': claim.get('subjectType'),
                'attribute': claim.get('attributeType'),
                'scope': claim.get('scopeText') or 'stage',
                'semanticSource': 'kimi_reviewed',
                'reviewVersion': candidate_review.get('reviewVersion'),
                'sourceUserTurnId': claim.get('sourceUserTurnId'),
                'confidence': 1.0,
            }
            for claim in update.get('claims') or []
        ]
        _emit_event(
            database,
            session_id,
            'intent_legacy_semantics_reviewed',
            {
                'versionId': version_id,
                'turnId': turn_id,
                'hypothesisId': target.get('id'),
                'reviewVersion': candidate_review.get('reviewVersion'),
                'semanticClaims': target['semanticClaims'],
            },
            services=services,
        )
    effective_patch = patch
    if isinstance(patch, dict):
        effective_patch = dict(patch)
        effective_patch.pop('openQuestions', None)
    try:
        context = merge_chat_update(
            previous,
            patch=effective_patch if allow_progress else None,
            user_text=user_content,
            stage_id=version_id,
            turn_id=turn_id,
        )
    except (TypeError, ValueError) as exception:
        _emit_event(
            database,
            session_id,
            'design_context_patch_rejected',
            {'versionId': version_id, 'turnId': turn_id, 'error': str(exception)[:500]},
            services=services,
        )
        context = previous
    if answered_question_ids:
        context = apply_question_answer_review(context, answered_question_ids, version_id, answer_turn_id or turn_id)
    hypothesis_id = None
    hypothesis = str(guidance.get('intentHypothesis') or '').strip()
    intent_decision = guidance.get('_intentDraftAudit', guidance.get('_intentDecision'))
    intent_candidate_review = guidance.get('_intentReviewAudit', guidance.get('_intentCandidateReview'))
    if allow_progress and isinstance(intent_decision, dict):
        _emit_event(
            database,
            session_id,
            'intent_candidate_decided',
            {
                'versionId': version_id,
                'turnId': turn_id,
                'sourceUserTurnId': answer_turn_id,
                'source': 'kimi_draft',
                'classification': intent_decision.get('classification'),
                'claims': intent_decision.get('claims') or [],
            },
            services=services,
        )
    if allow_progress and isinstance(intent_candidate_review, dict):
        _emit_event(
            database,
            session_id,
            'intent_candidate_reviewed',
            {
                'versionId': version_id,
                'turnId': turn_id,
                'sourceUserTurnId': answer_turn_id,
                'reviewVersion': intent_candidate_review.get('reviewVersion'),
                'classification': intent_candidate_review.get('classification'),
                'relation': intent_candidate_review.get('relation'),
                'conflictingHypothesisIds': intent_candidate_review.get('conflictingHypothesisIds', []),
                'evidenceSpans': [claim.get('evidenceSpan') for claim in intent_candidate_review.get('claims') or []],
                'claims': intent_candidate_review.get('claims') or [],
                'cardDelivered': bool(hypothesis),
            },
            services=services,
        )
    if allow_progress and hypothesis:
        reviewed_semantic_claims = [
            {**claim, 'sourceUserTurnId': answer_turn_id or claim.get('sourceUserTurnId')}
            for claim in guidance.get('_intentSemanticClaims') or []
            if isinstance(claim, dict)
        ]
        pending_evidence = _pending_intent_evidence(database, session_id, get_version(database, session_id, version_id), context)
        relevant_evidence = _relevant_intent_evidence(hypothesis, pending_evidence, turn_id)
        evidence_ids = [item['evidenceId'] for item in relevant_evidence]
        evidence_kinds = {item.get('kind') for item in relevant_evidence}
        confidence = 0.55 if len(evidence_kinds) >= 2 else 0.35
        context, hypothesis_id = merge_intent_hypothesis(
            context,
            hypothesis,
            evidence_ids,
            version_id,
            turn_id,
            confidence,
            displayed=True,
            semantic_claims=reviewed_semantic_claims,
            source_user_turn_id=answer_turn_id,
            origin='kimi_reviewed',
        )
        stored_turn = database.execute('SELECT guidance_json FROM conversation_turns WHERE id = ?', (turn_id,)).fetchone()
        if stored_turn is not None:
            stored_guidance = load_json(stored_turn['guidance_json']) or {}
            stored_guidance['_intentHypothesisId'] = hypothesis_id
            review_state = guidance.get('_intentReviewState')
            if isinstance(review_state, dict):
                stored_review_state = dict(review_state)
                stored_review_state['newHypothesisId'] = hypothesis_id
                stored_guidance['_intentReviewState'] = stored_review_state
            database.execute('UPDATE conversation_turns SET guidance_json = ? WHERE id = ?', (dump_json(stored_guidance), turn_id))
        _emit_event(
            database,
            session_id,
            'intent_hypothesis_memory_updated',
            {
                'versionId': version_id,
                'turnId': turn_id,
                'hypothesisId': hypothesis_id,
                'evidenceIds': evidence_ids,
                'confidence': confidence,
                'semanticClaims': reviewed_semantic_claims,
                'sourceUserTurnId': answer_turn_id,
            },
            services=services,
        )
        review_state = guidance.get('_intentReviewState') or {}
        if review_state.get('interactionMode') == 'conflict_choice':
            _emit_event(
                database,
                session_id,
                'intent_conflict_presented',
                {
                    'versionId': version_id,
                    'turnId': turn_id,
                    'newHypothesisId': hypothesis_id,
                    'existingHypothesisId': review_state.get('existingHypothesisId'),
                    'explanation': str(review_state.get('reviewExplanation') or '')[:1200],
                    'reviewSource': review_state.get('reviewSource', 'kimi'),
                    'conflictClaims': review_state.get('conflictClaims'),
                },
                services=services,
            )
    elif allow_progress and isinstance(patch, dict):
        patch_hypotheses = [
            entry.get('goal') for entry in patch.get('goals', []) if isinstance(entry, dict) and entry.get('goal')
        ] + [
            entry.get('constraint')
            for entry in patch.get('constraints', [])
            if isinstance(entry, dict) and entry.get('constraint')
        ]
        for patch_hypothesis in patch_hypotheses[:2]:
            pending_evidence = _pending_intent_evidence(database, session_id, get_version(database, session_id, version_id), context)
            relevant_evidence = _relevant_intent_evidence(patch_hypothesis, pending_evidence, turn_id)
            context, hypothesis_id = merge_intent_hypothesis(
                context,
                patch_hypothesis,
                [item['evidenceId'] for item in relevant_evidence],
                version_id,
                turn_id,
                0.35,
            )
            _emit_event(
                database,
                session_id,
                'intent_hypothesis_memory_updated',
                {
                    'versionId': version_id,
                    'turnId': turn_id,
                    'hypothesisId': hypothesis_id,
                    'evidenceIds': [item['evidenceId'] for item in relevant_evidence],
                    'confidence': 0.35,
                    'source': 'design_context_patch',
                },
                services=services,
            )
    disagreement = guidance.get('disagreement')
    if isinstance(disagreement, dict) and disagreement.get('status') == 'active':
        context = set_active_disagreement(context, disagreement, version_id, turn_id)
    elif isinstance(disagreement, dict) and disagreement.get('status') == 'acknowledged':
        context = set_active_disagreement(context, None, version_id, turn_id)
    elif isinstance(disagreement, dict) and disagreement.get('status') == 'resolved':
        resolution = disagreement.get('resolution')
        context = set_active_disagreement(context, None, version_id, turn_id)
        context = add_confirmed_decision(
            context,
            f'Resolve disagreement: {resolution}',
            disagreement.get('coreDisagreement') or 'The parties reached a shared design decision.',
            version_id,
            turn_id,
        )
    auto_open_questions = []
    question_candidates = []
    proposal_marker = proposal_discovery if isinstance(proposal_discovery, dict) else {}
    if proposal_marker.get('status') == 'clarifying':
        visible_questions = _extract_visible_questions(assistant_content, guidance)
        question_key = str(proposal_marker.get('clarificationQuestionKey') or '').strip()
        topic_id = str(proposal_marker.get('topicId') or '').strip()
        for index, question in enumerate(visible_questions):
            source_key = f'proposal:{topic_id}:{question_key}' if topic_id and question_key and index == 0 else None
            question_candidates.append((question, source_key, False))
    if isinstance(disagreement, dict) and disagreement.get('status') == 'active':
        next_question = disagreement.get('nextQuestion')
        challenge_id = str(disagreement.get('challengeId') or '').strip()
        source_key = f'disagreement:{challenge_id}:next' if challenge_id else None
        question_candidates.append((next_question, source_key, True))
        question_candidates.append((guidance.get('followUpQuestion'), None, False))
        question_candidates.extend(((question, None, False) for question in _extract_visible_questions(assistant_content, guidance)))
    seen_questions = set()
    for question, source_key, prefer_question in question_candidates:
        normalized_question = question_dedup_key(question)
        if not normalized_question or normalized_question in seen_questions:
            continue
        seen_questions.add(normalized_question)
        before_count = len(context.get('openQuestions', []))
        context = add_open_question(
            context,
            question,
            version_id,
            turn_id,
            source_kind='visible_output',
            source_key=source_key,
            prefer_question=prefer_question,
        )
        if len(context.get('openQuestions', [])) > before_count:
            auto_open_questions.append(str(question).strip())
    save_design_context(database, version_id, context)
    _emit_event(
        database,
        session_id,
        'design_context_updated',
        {
            'versionId': version_id,
            'turnId': turn_id,
            'hasExplicitUserMemory': bool(
                any(
                    (
                        item.get('sourceTurnId') == turn_id and item.get('authority') == 'explicit'
                        for item in context.get('userGoals', []) + context.get('designConstraints', [])
                    )
                )
            ),
            'hasInferredMemory': bool(hypothesis_id)
            or bool(allow_progress and isinstance(patch, dict) and (patch.get('goals') or patch.get('constraints'))),
            'hypothesisId': hypothesis_id,
            'hypothesisFeedback': None,
            'progressApplied': bool(allow_progress),
            'autoOpenQuestionCount': len(auto_open_questions),
            'disagreementStatus': disagreement.get('status') if isinstance(disagreement, dict) else None,
            'changed': context != previous,
        },
        services=services,
    )
    return context
