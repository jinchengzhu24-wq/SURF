import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from cocreation.domain import binary_discovery
from cocreation.reliability.component_recovery import interaction_state
from cocreation.domain.design_context import add_confirmed_decision, add_open_question, add_rejected_decision, apply_question_answer_review, deduplicate_open_questions, design_level_open_questions, empty_design_context, infer_intent_topic, merge_chat_update, normalize_design_context, question_dedup_key, set_active_disagreement
from cocreation.domain.level_validation import build_entity_bindings, build_untrusted_entity_bindings, derive_entity_transitions, entity_binding_fingerprint, entity_bindings_match_rows
from .database import DEFAULT_DATABASE_PATH, SCHEMA, _deadline_expired, _ensure_column, _parse_time, _public_guidance, _remaining_deadline_seconds, backfill_language_locks, get_session, load_json, map_fingerprint
from .backfill import load_design_context, load_entity_bindings, backfill_design_contexts, backfill_entity_bindings, backfill_intent_semantics, backfill_revision_challenges, repair_resolved_challenge_disagreements, repair_duplicate_progress_questions, repair_answered_challenge_choice_questions

def _last_message_failure(database, session, turns):
    """Current request diagnostics only; never expose audit prose or stale failures."""
    version_id = session['current_version_id']
    row = database.execute("SELECT id, payload_json, created_at FROM audit_events\n        WHERE session_id = ? AND event_type = 'message_generation_failed'\n          AND json_extract(payload_json, '$.baseVersionId') = ?\n        ORDER BY id DESC LIMIT 1", (session['id'], version_id)).fetchone()
    if row is None:
        return None
    payload = load_json(row['payload_json']) or {}
    key = payload.get('messageKey')
    current_turns = [turn for turn in turns if turn['version_id'] == version_id]
    if not key or any((turn['role'] == 'assistant' and turn['request_id'] == key for turn in current_turns)):
        return None
    if current_turns and max((turn['created_at'] for turn in current_turns)) > row['created_at']:
        return None
    binary_rows = database.execute(
        "SELECT id, payload_json, created_at FROM audit_events\n"
        " WHERE session_id = ? AND event_type = 'binary_discovery_state'\n"
        "   AND json_extract(payload_json, '$.versionId') = ?\n"
        " ORDER BY id DESC",
        (session['id'], version_id),
    ).fetchall()
    seen_topics = set()
    for topic_row in binary_rows:
        topic = load_json(topic_row['payload_json']) or {}
        topic_id = topic.get('topicId')
        if topic_id:
            if topic_id in seen_topics:
                continue
            seen_topics.add(topic_id)
        if topic.get('status') not in {'cancelled', 'proposal_ready'}:
            continue
        topic_keys = {
            topic.get('initialMessageKey'),
            (topic.get('retryAction') or {}).get('idempotencyKey'),
        }
        if key in topic_keys or topic_row['id'] > row['id'] or topic_row['created_at'] > row['created_at']:
            return None
    details = payload.get('details') or {}
    return {'messageKey': key, 'baseVersionId': version_id, 'code': payload.get('code'), 'requestId': payload.get('requestId'), 'retryable': bool(payload.get('retryable', True)), 'committed': False, 'details': {name: details[name] for name in ('task', 'failureStage', 'failureKind', 'failureCode', 'attemptsUsed', 'maximumAttempts', 'recoveryState', 'component', 'repairAttemptsUsed', 'maximumRepairAttempts') if name in details}}

def _serialize_attempt(attempt):
    return {'attemptId': attempt['id'], 'status': attempt['status'], 'issuedAt': attempt['issued_at'], 'loadedAt': attempt['loaded_at'], 'firstMoveAt': attempt['first_move_at'], 'finishedAt': attempt['finished_at'], 'durationSeconds': attempt['duration_seconds'], 'moveCount': attempt['move_count'], 'pushCount': attempt['push_count'], 'restartCount': attempt['restart_count'], 'minimumMoves': attempt['minimum_moves'], 'minimumPushes': attempt['minimum_pushes']}

def serialize_session(database, session_id, *, now=None, clock=None):
    effective_now = now if now is not None else (clock.utc_datetime() if clock is not None else None)
    session = get_session(database, session_id)
    if session is None:
        return None
    versions = database.execute('\n        SELECT * FROM level_versions\n        WHERE session_id = ? ORDER BY stage_number\n        ', (session_id,)).fetchall()
    turns = database.execute('\n        SELECT id, sequence_number, role, content, language, version_id,\n               request_id, guidance_json, proposal_binding_json, created_at\n        FROM conversation_turns\n        WHERE session_id = ? ORDER BY sequence_number\n        ', (session_id,)).fetchall()
    translations = database.execute('\n        SELECT turn_id, language, body, guidance_json, proposal_summary,\n               created_at\n        FROM turn_translations\n        WHERE session_id = ? ORDER BY created_at\n        ', (session_id,)).fetchall()
    assessments = database.execute('\n        SELECT id, version_id, assistant_turn_id, payload_json, created_at\n        FROM llm_assessments WHERE session_id = ?\n        ', (session_id,)).fetchall()
    proposals = database.execute('\n        SELECT id, base_version_id, proposed_rows_json, summary, diff_json,\n               validation_json, status, assistant_turn_id, created_at, decided_at\n        FROM change_proposals WHERE session_id = ? ORDER BY created_at\n        ', (session_id,)).fetchall()
    attempts = database.execute('\n        SELECT id, version_id, status, issued_at, loaded_at, first_move_at,\n               finished_at, duration_seconds, move_count, push_count,\n               restart_count, minimum_moves, minimum_pushes\n        FROM play_attempts WHERE session_id = ? ORDER BY issued_at DESC\n        ', (session_id,)).fetchall()
    accepted_openings = database.execute("\n        SELECT decision.version_id, decision.proposal_id,\n               proposal.assistant_turn_id\n        FROM designer_decisions AS decision\n        JOIN change_proposals AS proposal\n          ON proposal.id = decision.proposal_id\n        WHERE decision.session_id = ?\n          AND decision.decision_type = 'accept'\n          AND decision.version_id IS NOT NULL\n        ORDER BY decision.created_at\n        ", (session_id,)).fetchall()
    intention = database.execute('\n        SELECT content, language, created_at\n        FROM designer_intentions WHERE session_id = ?\n        ', (session_id,)).fetchone()
    proposal_request_keys = {str(row['message_key']) for row in database.execute("\n            SELECT json_extract(payload_json, '$.messageKey') AS message_key\n            FROM audit_events\n            WHERE session_id = ? AND event_type = 'proposal_request_requested'\n            ", (session_id,)).fetchall() if row['message_key']}
    challenge_review_rows = database.execute('\n        SELECT review_id, challenge_id, source_user_turn_id, message_key,\n               status, attempts_used, failure_code, result_json, updated_at\n        FROM challenge_reason_reviews\n        WHERE session_id = ? ORDER BY created_at, review_id\n        ', (session_id,)).fetchall()
    challenge_reviews = {row['message_key']: {**(load_json(row['result_json']) or {}), 'reviewId': row['review_id'], 'challengeId': row['challenge_id'], 'sourceUserTurnId': row['source_user_turn_id'], 'messageKey': row['message_key'], 'status': row['status'], 'attemptsUsed': row['attempts_used'], 'failureCode': row['failure_code'], 'retryable': False, 'updatedAt': row['updated_at']} for row in challenge_review_rows}
    execution_outcomes = {}
    for row in database.execute("\n        SELECT payload_json FROM audit_events\n        WHERE session_id = ? AND event_type = 'revision_execution_outcome'\n        ORDER BY id\n        ", (session_id,)).fetchall():
        payload = load_json(row['payload_json']) or {}
        if payload.get('versionId'):
            execution_outcomes[payload['versionId']] = payload
    intent_audit_rows = database.execute("\n        SELECT id, event_type, payload_json, created_at FROM audit_events\n        WHERE session_id = ? AND event_type IN (\n          'intent_evidence_recorded',\n          'intent_hypothesis_feedback_applied',\n          'intent_hypothesis_feedback_pending',\n          'intent_progress_rewrite'\n        )\n        ORDER BY id\n        ", (session_id,)).fetchall()
    intent_evidence_by_id = {}
    latest_intent_feedback = {}
    progress_rewrites = {}
    for row in intent_audit_rows:
        payload = load_json(row['payload_json']) or {}
        if row['event_type'] == 'intent_evidence_recorded':
            evidence_id = payload.get('evidenceId')
            if evidence_id:
                intent_evidence_by_id[evidence_id] = {**payload, 'auditId': row['id'], 'createdAt': row['created_at']}
        elif row['event_type'] == 'intent_progress_rewrite':
            artifact_type = payload.get('artifactType')
            artifact_id = payload.get('artifactId')
            if artifact_type and artifact_id:
                progress_rewrites[artifact_type, artifact_id] = payload
        else:
            hypothesis_id = payload.get('hypothesisId')
            version_id = payload.get('versionId')
            if hypothesis_id and version_id:
                latest_intent_feedback[version_id, hypothesis_id] = {**payload, 'eventType': row['event_type'], 'createdAt': row['created_at']}
    attempts_by_version = {}
    translations_by_turn = {}
    opening_by_version = {opening['version_id']: opening for opening in accepted_openings}
    turn_created_at = {turn['id']: turn['created_at'] for turn in turns}
    stage_numbers = {version['id']: version['stage_number'] for version in versions}
    version_rows = {version['id']: load_json(version['rows_json']) or [] for version in versions}
    version_bindings = {version['id']: load_json(version['entity_bindings_json']) if version['entity_bindings_json'] else None for version in versions}
    opening_turn_ids_by_version = {}
    for assessment in assessments:
        opening_turn_ids_by_version.setdefault(assessment['version_id'], set()).add(assessment['assistant_turn_id'])
    for opening in accepted_openings:
        opening_turn_ids_by_version.setdefault(opening['version_id'], set()).add(opening['assistant_turn_id'])
    for version in versions:
        if version['stage_number'] != 1:
            continue
        if opening_turn_ids_by_version.get(version['id']):
            continue
        version_turns = [turn for turn in turns if turn['version_id'] == version['id']]
        first_user_index = next((index for index, turn in enumerate(version_turns) if turn['role'] == 'user'), None)
        first_assistant = next((turn for index, turn in enumerate(version_turns) if turn['role'] == 'assistant' and (first_user_index is None or index < first_user_index)), None)
        if first_assistant is not None:
            opening_turn_ids_by_version.setdefault(version['id'], set()).add(first_assistant['id'])

    def public_turn_guidance(turn_guidance, body, rows, language, entity_bindings=None):
        """Recheck stored route annotations before exposing historical turns."""
        guidance = _public_guidance(turn_guidance)
        try:
            from cocreation.llm.facade import repair_legacy_visible_guidance
            guidance = repair_legacy_visible_guidance(guidance, language)
        except (ImportError, TypeError, ValueError, KeyError):
            pass
        try:
            from cocreation.llm.facade import _filter_coordinate_links, _recover_coordinate_links
            links = _filter_coordinate_links(guidance.get('coordinateLinks'), body, rows, entity_bindings)
            links = _recover_coordinate_links(body, rows, links, entity_bindings)
        except (ImportError, TypeError, ValueError, KeyError):
            links = []
        if links:
            guidance['coordinateLinks'] = links
        else:
            guidance.pop('coordinateLinks', None)
        return guidance

    def public_text(value, language):
        """Hide prompt-only implementation labels in legacy stored text."""
        try:
            from cocreation.llm.facade import repair_legacy_visible_text
            return repair_legacy_visible_text(value, language)
        except (ImportError, TypeError, ValueError, KeyError):
            return value

    def public_turn_content(turn):
        if turn['role'] != 'assistant':
            return turn['content']
        guidance = load_json(turn['guidance_json']) or {}
        disagreement = guidance.get('disagreement') or {}
        if isinstance(disagreement, dict) and disagreement.get('subject') == 'ai_revision_challenge' and (disagreement.get('phase') == 'choice_pending') and (disagreement.get('status') == 'active') and (guidance.get('challengeCardSchemaVersion') != 2):
            return '我理解你提出了不同的修改范围。现在先确认采用原方案，还是按你的新方向重新规划；地图还没有改变。' if turn['language'] == 'zh-CN' else 'I understand that you want a different scope of changes. Please choose the original proposal or a new plan; the map has not changed.'
        content = public_text(turn['content'], turn['language'])
        stage_number = stage_numbers.get(turn['version_id'])
        if stage_number == 1 and turn['id'] in opening_turn_ids_by_version.get(turn['version_id'], set()):
            try:
                from cocreation.llm.facade import _repair_stage_one_opening_display
                content = _repair_stage_one_opening_display(content, version_rows.get(turn['version_id']), turn['language'])
            except (ImportError, TypeError, ValueError, KeyError):
                pass
        return content
    public_content_by_turn = {turn['id']: public_turn_content(turn) for turn in turns}
    raw_content_by_turn = {turn['id']: turn['content'] for turn in turns}
    translated_content_by_turn = {(translation['turn_id'], translation['language']): translation['body'] for translation in translations if translation['body']}

    def hypothesis_evidence_text(hypothesis, language):
        for claim in hypothesis.get('semanticClaims') or []:
            source_turn_id = claim.get('sourceUserTurnId')
            translated = translated_content_by_turn.get((source_turn_id, language))
            if translated:
                return translated
            if source_turn_id and raw_content_by_turn.get(source_turn_id):
                return raw_content_by_turn[source_turn_id]
        return None

    def public_assessment_payload(payload, language):
        if not isinstance(payload, dict):
            return payload
        try:
            from cocreation.llm.facade import repair_legacy_visible_text
        except ImportError:
            return payload
        result = dict(payload)
        for field_name in ('solutionSummary', 'difficultyOpinion', 'satisfactionQuestion'):
            if result.get(field_name) is not None:
                result[field_name] = repair_legacy_visible_text(result[field_name], language)
        for field_name in ('features', 'suggestions'):
            if isinstance(result.get(field_name), list):
                result[field_name] = [repair_legacy_visible_text(item, language) for item in result[field_name]]
        return result
    public_guidance_by_turn = {turn['id']: public_turn_guidance(load_json(turn['guidance_json']), public_content_by_turn[turn['id']], version_rows.get(turn['version_id'], []), turn['language'], version_bindings.get(turn['version_id'])) for turn in turns}
    shown_human_edit_disagreements = set()
    for turn in turns:
        if turn['role'] != 'assistant':
            continue
        guidance = public_guidance_by_turn.get(turn['id']) or {}
        disagreement = guidance.get('disagreement')
        if not (isinstance(disagreement, dict) and disagreement.get('status') == 'active' and (disagreement.get('subject') == 'human_edit')):
            continue
        version_id = turn['version_id']
        if version_id in shown_human_edit_disagreements:
            disagreement = dict(disagreement)
            disagreement['displayCard'] = False
            guidance['disagreement'] = disagreement
        elif disagreement.get('displayCard') is not False:
            shown_human_edit_disagreements.add(version_id)
    latest_intent_turn = {}
    stored_guidance_by_turn = {}
    for turn in turns:
        guidance = load_json(turn['guidance_json']) or {}
        stored_guidance_by_turn[turn['id']] = guidance
        hypothesis_id = guidance.get('_intentHypothesisId')
        if turn['role'] == 'assistant' and hypothesis_id:
            latest_intent_turn[turn['version_id']] = turn['id']
    translated_intent_statements = {}
    for translation in translations:
        source_guidance = stored_guidance_by_turn.get(translation['turn_id'], {})
        translated_guidance = load_json(translation['guidance_json']) or {}
        translated_statement = str(translated_guidance.get('intentHypothesis') or '').strip()
        source_hypothesis_id = source_guidance.get('_intentHypothesisId')
        if source_hypothesis_id and translated_statement:
            translated_intent_statements[source_hypothesis_id, translation['language']] = translated_statement

    def localized_hypothesis_statement(hypothesis, language):
        translated = translated_intent_statements.get((hypothesis.get('id'), language))
        if translated:
            return translated
        if hypothesis.get('displayLanguage') == language:
            return hypothesis.get('displayStatement') or hypothesis.get('statement')
        return hypothesis.get('statement')

    def conflict_choice_statement(hypothesis, language):
        claims = hypothesis.get('semanticClaims') or []
        if len(claims) == 1:
            claim = claims[0]
            if claim.get('subject') == 'water' and claim.get('attribute') == 'coverage':
                if language == 'zh-CN' and claim.get('direction') == 'decrease':
                    return '你认为当前水域覆盖过多，倾向于降低它占据的空间。'
                if language == 'zh-CN' and claim.get('direction') == 'increase':
                    return '你认为当前水域覆盖不足，倾向于增加它占据的空间。'
                if language != 'zh-CN' and claim.get('direction') == 'decrease':
                    return 'You see the current water coverage as excessive and prefer reducing its footprint.'
                if language != 'zh-CN' and claim.get('direction') == 'increase':
                    return 'You see the current water coverage as insufficient and prefer increasing its footprint.'
        return localized_hypothesis_statement(hypothesis, language)

    def intent_state(turn):
        guidance = stored_guidance_by_turn.get(turn['id'], {})
        hypothesis_id = guidance.get('_intentHypothesisId')
        if turn['role'] != 'assistant' or not hypothesis_id:
            return None
        context = load_design_context(database, session_id, turn['version_id'])
        hypothesis = next((item for item in context.get('intentHypotheses', []) if item.get('id') == hypothesis_id), None)
        if hypothesis is None:
            return None
        is_latest = latest_intent_turn.get(turn['version_id']) == turn['id']
        status = hypothesis.get('status', 'tentative')
        review = guidance.get('_intentReviewState') or {}
        actionable = bool(
            is_latest and status == 'tentative'
            and turn['version_id'] == session['current_version_id']
            and session['status'] == 'active'
            and not _deadline_expired(session['deadline_at'], now=effective_now)
        )
        review_mode = review.get('interactionMode', 'full')
        mode = 'conflict_choice' if review_mode == 'conflict_choice' else review_mode if actionable else 'resolved'
        result = {'hypothesisId': hypothesis_id, 'status': status, 'interactionMode': mode, 'actionable': actionable, 'resolvedStatement': hypothesis.get('statement') if status in {'confirmed', 'superseded'} else None, 'reviewExplanation': review.get('reviewExplanation') if actionable else None}
        if review_mode == 'conflict_choice':
            existing_id = str(review.get('existingHypothesisId') or '').strip()
            existing = next((item for item in context.get('intentHypotheses', []) if item.get('id') == existing_id), None)
            if existing is not None:
                selected_id = None
                if status == 'confirmed' and existing.get('status') == 'superseded':
                    selected_id = hypothesis_id
                elif status == 'rejected' and existing.get('status') == 'confirmed':
                    selected_id = existing_id
                candidate_statement = str(review.get('candidateText') or '').strip()
                new_statement = candidate_statement or conflict_choice_statement(hypothesis, turn['language'])
                result['conflictChoice'] = {'explanation': str(review.get('reviewExplanation') or '').strip(), 'selectedHypothesisId': selected_id, 'options': [{'role': 'new', 'hypothesisId': hypothesis_id, 'statement': new_statement, 'status': status, 'evidenceText': hypothesis_evidence_text(hypothesis, turn['language'])}, {'role': 'existing', 'hypothesisId': existing_id, 'statement': conflict_choice_statement(existing, turn['language']), 'status': existing.get('status', 'confirmed'), 'evidenceText': hypothesis_evidence_text(existing, turn['language'])}]}
        return result
    for turn in turns:
        state = intent_state(turn)
        if state is not None:
            public_guidance_by_turn[turn['id']]['intentState'] = state

    def source_stage_number(source_version_id, current_version):
        return stage_numbers.get(source_version_id, current_version['stage_number'])
    latest_revision_turn_by_version = {}
    for turn in reversed(turns):
        if turn['role'] != 'assistant':
            continue
        guidance = load_json(turn['guidance_json']) or {}
        offer = guidance.get('proposalOffer') if isinstance(guidance, dict) else None
        if isinstance(offer, dict) and str(offer.get('summary') or '').strip() and (turn['version_id'] not in latest_revision_turn_by_version):
            latest_revision_turn_by_version[turn['version_id']] = turn['id']

    def proposal_state(turn, guidance):
        offer = guidance.get('proposalOffer') if isinstance(guidance, dict) else None
        if turn['role'] != 'assistant' or not isinstance(offer, dict) or (not str(offer.get('summary') or '').strip()):
            return None
        if session['status'] != 'active' or _deadline_expired(session['deadline_at'], now=effective_now):
            return {'status': 'locked', 'actionable': False}
        try:
            binding = load_json(turn['proposal_binding_json'])
        except (TypeError, ValueError):
            binding = None
        if not isinstance(binding, dict) or not isinstance(binding.get('executionBrief'), dict):
            return {'status': 'unbound', 'actionable': False, 'reason': 'missing_binding'}
        base_version_id = binding.get('baseVersionId')
        current_version = next((version for version in versions if version['id'] == base_version_id), None)
        if current_version is None or base_version_id != session['current_version_id']:
            return {'status': 'stale', 'actionable': False, 'reason': 'version_changed'}
        current_context = load_design_context(database, session_id, base_version_id)
        if (current_context.get('activeDisagreement') or {}).get('status') == 'active':
            return {'status': 'disagreement_active', 'actionable': False, 'reason': 'disagreement_active'}
        rows = version_rows.get(base_version_id) or []
        expected_fingerprint = binding.get('mapFingerprint')
        if expected_fingerprint and map_fingerprint(rows) != expected_fingerprint:
            return {'status': 'stale', 'actionable': False, 'reason': 'map_changed'}
        expected_binding_fingerprint = binding.get('entityBindingFingerprint')
        current_bindings = version_bindings.get(base_version_id)
        actual_binding_fingerprint = (current_bindings or {}).get('bindingFingerprint')
        if not expected_binding_fingerprint or not actual_binding_fingerprint or expected_binding_fingerprint != actual_binding_fingerprint:
            return {'status': 'stale', 'actionable': False, 'reason': 'entity_binding_changed'}
        brief = binding['executionBrief']
        if not isinstance(brief.get('requiredTransitions'), list) or not brief['requiredTransitions']:
            return {'status': 'unbound', 'actionable': False, 'reason': 'missing_exact_transitions'}
        for transition in brief.get('requiredTransitions') or []:
            row = transition.get('row')
            column = transition.get('column')
            if not isinstance(row, int) or not isinstance(column, int) or (not 1 <= row <= len(rows)) or (not rows) or (not 1 <= column <= len(rows[row - 1])):
                return {'status': 'stale', 'actionable': False, 'reason': 'invalid_binding'}
            actual = rows[row - 1][column - 1]
            if actual == transition.get('to'):
                return {'status': 'already_satisfied', 'actionable': False, 'reason': 'already_satisfied'}
            if actual != transition.get('from'):
                return {'status': 'stale', 'actionable': False, 'reason': 'precondition_failed'}
        binding_status = binding.get('status')
        if binding_status == 'already_satisfied':
            return {'status': 'already_satisfied', 'actionable': False, 'reason': 'already_satisfied'}
        if binding_status == 'stale':
            return {'status': 'stale', 'actionable': False, 'reason': 'proposal_consumed_or_stale'}
        if binding_status == 'challenged':
            return {'status': 'challenged', 'actionable': False, 'reason': 'proposal_challenged'}
        return {'status': 'active', 'actionable': binding.get('status', 'active') == 'active' and latest_revision_turn_by_version.get(turn['version_id']) == turn['id'] and (turn['version_id'] == session['current_version_id']), 'reason': None}
    progress_contexts = []
    parent_by_version = {version['id']: version['parent_version_id'] for version in versions}

    def lineage_ids(version_id):
        result = set()
        cursor = version_id
        while cursor and cursor not in result:
            result.add(cursor)
            cursor = parent_by_version.get(cursor)
        return result

    def clean_evidence_text(value):
        text = ' '.join(str(value or '').split())
        text = re.sub('[\\(（]\\s*\\d{1,2}\\s*[,，]\\s*\\d{1,2}\\s*[\\)）]', '', text)
        return ' '.join(text.split())[:500]

    def evidence_projection(evidence, language):
        kind = str(evidence.get('kind') or 'feedback')
        public_kind = {'expressed_direction': 'expressed_direction', 'proposal_accept': 'confirmed_decision', 'disagreement_resolved': 'confirmed_decision', 'manual_edit': 'manual_edit'}.get(kind, kind)
        details = evidence.get('details') if isinstance(evidence.get('details'), dict) else {}
        labels = {'zh-CN': {'conversation': '已表达方向', 'proposal_accept': '已确认方案', 'proposal_reject': '方案反馈', 'manual_edit': '手动修改观察', 'play_completed': '试玩记录', 'play_abandoned': '试玩记录', 'disagreement_active': '分歧记录', 'disagreement_resolved': '分歧解决', 'stage_restore': '历史恢复', 'intent_feedback': '倾向确认或修订'}, 'en': {'conversation': 'Expressed direction', 'expressed_direction': 'Expressed direction', 'confirmed_decision': 'Confirmed decision', 'proposal_accept': 'Confirmed proposal', 'proposal_reject': 'Proposal feedback', 'manual_edit': 'Manual-edit observation', 'play_completed': 'Playtest record', 'play_abandoned': 'Playtest record', 'disagreement_active': 'Disagreement', 'disagreement_resolved': 'Disagreement resolved', 'stage_restore': 'Stage restore', 'intent_feedback': 'Inclination feedback'}}
        text = clean_evidence_text(evidence.get('observation'))
        if public_kind == 'confirmed_decision':
            rewrite = progress_rewrites.get(('confirmed_decision', evidence.get('proposalId'))) or {}
            if rewrite.get('summaryText'):
                text = clean_evidence_text(rewrite['summaryText'])
        if language == 'zh-CN' and public_kind == 'expressed_direction':
            label = '已表达方向'
        elif language == 'zh-CN' and public_kind == 'confirmed_decision':
            label = '已确认决策'
        else:
            label = labels['zh-CN' if language == 'zh-CN' else 'en'].get(public_kind, labels['zh-CN' if language == 'zh-CN' else 'en']['intent_feedback'])
        if kind == 'manual_edit':
            rewrite = progress_rewrites.get(('manual_edit', evidence.get('versionId'))) or {}
            if rewrite.get('summaryText'):
                text = clean_evidence_text(rewrite['summaryText'])
            changed = len(details.get('diff') or [])
            suffix = f'确定性 diff 记录了 {changed} 个格子变化，且保存时已通过验证。' if language == 'zh-CN' else f'The deterministic diff recorded {changed} changed tiles and validation passed on save.'
            text = ' '.join((part for part in (text, suffix) if part))
        elif kind.startswith('play_'):
            suffix = f"{details.get('moveCount', 0)} 次移动、{details.get('pushCount', 0)} 次推动、{details.get('restartCount', 0)} 次重开。" if language == 'zh-CN' else f"{details.get('moveCount', 0)} moves, {details.get('pushCount', 0)} pushes, and {details.get('restartCount', 0)} restarts were recorded."
            text = ' '.join((part for part in (text, suffix) if part))
        elif kind.startswith('proposal_') and details.get('reason'):
            text = ' '.join((text, clean_evidence_text(details['reason'])))
        elif kind == 'intent_feedback':
            action = details.get('action')
            suffix = {'confirm': '你确认了这条倾向。' if language == 'zh-CN' else 'You confirmed this inclination.', 'revise': '你修订并确认了这条倾向。' if language == 'zh-CN' else 'You revised and confirmed this inclination.', 'reject': '你否定了这条推测。' if language == 'zh-CN' else 'You rejected this hypothesis.'}.get(action, '')
            text = ' '.join((part for part in (text, suffix) if part))
        if text:
            text = f'{label}：{text}' if language == 'zh-CN' else f'{label}: {text}'
        else:
            text = label
        return {'evidenceId': evidence['evidenceId'], 'stageNumber': stage_numbers.get(evidence.get('versionId'), 1), 'kind': public_kind, 'text': text, 'createdAt': evidence.get('createdAt'), 'detailedText': clean_evidence_text((progress_rewrites.get(('manual_edit', evidence.get('versionId'))) or {}).get('detailedText')) or None if kind == 'manual_edit' else None}
    for version in versions:
        context = load_design_context(database, session_id, version['id'])
        expressed_directions = []
        for field_name, kind in (('goal', 'goal'), ('constraint', 'constraint')):
            for item in context.get('userGoals' if field_name == 'goal' else 'designConstraints', []):
                if item.get('status') != 'active' or item.get('authority') != 'explicit':
                    continue
                text = str(item.get(field_name) or '').strip()
                if not text:
                    continue
                expressed_directions.append({'kind': kind, 'text': text, 'sourceStageNumber': source_stage_number(item.get('sourceStageId'), version), 'updatedAt': turn_created_at.get(item.get('sourceTurnId')), 'label': 'explicit'})
        confirmed_hypotheses = [item for item in context.get('intentHypotheses', []) if item.get('status') == 'confirmed']
        confirmed_by_topic = {}
        for item in confirmed_hypotheses:
            confirmed_by_topic.setdefault(item.get('topicKey'), []).append(item)
        lineage = lineage_ids(version['id'])
        design_inclinations = []
        for hypothesis in confirmed_hypotheses:
            evidence_ids = list(hypothesis.get('supportingEvidenceIds') or [])
            for evidence in intent_evidence_by_id.values():
                if evidence.get('versionId') not in lineage:
                    continue
                topic = infer_intent_topic(evidence.get('observation'))
                if topic != 'other' and hypothesis.get('topicKey') != 'other' and (topic == hypothesis.get('topicKey')) and (len(confirmed_by_topic.get(topic, [])) == 1):
                    evidence_ids.append(evidence['evidenceId'])
            evidence = [intent_evidence_by_id[evidence_id] for evidence_id in dict.fromkeys(evidence_ids) if evidence_id in intent_evidence_by_id and intent_evidence_by_id[evidence_id].get('versionId') in lineage and (intent_evidence_by_id[evidence_id].get('kind') in {'expressed_direction', 'proposal_accept', 'disagreement_resolved', 'manual_edit'})]
            evidence.sort(key=lambda item: (item.get('createdAt') or '', item['auditId']))
            trail = [evidence_projection(item, session['language']) for item in evidence[-12:]]
            feedback = latest_intent_feedback.get((version['id'], hypothesis['id']), {})
            design_inclinations.append({'hypothesisId': hypothesis['id'], 'statement': hypothesis.get('displayStatement') or hypothesis['statement'], 'confirmedAtStageNumber': source_stage_number(hypothesis.get('confirmedAtStageId') or hypothesis.get('sourceStageId'), version), 'updatedAt': feedback.get('createdAt') or (trail[-1]['createdAt'] if trail else None) or turn_created_at.get(hypothesis.get('sourceTurnId')), 'evidenceTrail': trail})
        progress_contexts.append({'versionId': version['id'], 'stageNumber': version['stage_number'], 'parentVersionId': version['parent_version_id'], 'expressedDirections': expressed_directions[-16:], 'designInclinations': design_inclinations[-16:], 'confirmedDecisions': [{'decision': item['decision'], 'reason': item.get('reason') or None, 'sourceStageNumber': source_stage_number(item.get('sourceStageId'), version), 'updatedAt': turn_created_at.get(item.get('sourceTurnId')), 'label': 'confirmed'} for item in context.get('confirmedDecisions', []) if item.get('status') == 'active'], 'unresolvedQuestions': [{'question': item['question'], 'sourceStageNumber': source_stage_number(item.get('sourceStageId'), version), 'updatedAt': turn_created_at.get(item.get('updatedFromTurnId') or item.get('resolvedByTurnId') or item.get('sourceTurnId')), 'label': 'open'} for item in design_level_open_questions(context)], 'questionRecords': [{'questionId': item.get('id'), 'question': item.get('question'), 'status': 'answered' if item.get('status') in {'answered', 'resolved'} else 'ignored' if item.get('status') == 'ignored' else 'unanswered', 'askedAtStageNumber': source_stage_number(item.get('sourceStageId'), version), 'answeredAtStageNumber': source_stage_number(item.get('answeredAtStageId'), version) if item.get('answeredAtStageId') else None, 'ignoredAtStageNumber': source_stage_number(item.get('ignoredAtStageId'), version) if item.get('ignoredAtStageId') else None, 'sourceTurnId': item.get('sourceTurnId'), 'answeredByTurnId': item.get('resolvedByTurnId'), 'updatedAt': item.get('ignoredAt') or turn_created_at.get(item.get('resolvedByTurnId') or item.get('updatedFromTurnId') or item.get('sourceTurnId'))} for item in context.get('openQuestions', []) if item.get('sourceKind') in {'visible_output', 'legacy'}]})
    for attempt in attempts:
        attempts_by_version.setdefault(attempt['version_id'], []).append(_serialize_attempt(attempt))
    for translation in translations:
        translation_guidance = load_json(translation['guidance_json'])
        translation_turn = next((turn for turn in turns if turn['id'] == translation['turn_id']), None)
        if translation_turn is not None:
            translation_guidance = public_turn_guidance(translation_guidance, public_text(translation['body'], translation['language']), version_rows.get(translation_turn['version_id'], []), translation['language'], version_bindings.get(translation_turn['version_id']))
            source_intent_state = public_guidance_by_turn.get(translation['turn_id'], {}).get('intentState')
            source_disagreement = public_guidance_by_turn.get(translation['turn_id'], {}).get('disagreement')
            translated_disagreement = translation_guidance.get('disagreement')
            if isinstance(source_disagreement, dict) and source_disagreement.get('subject') == 'human_edit' and (source_disagreement.get('displayCard') is False) and isinstance(translated_disagreement, dict) and (translated_disagreement.get('subject') == 'human_edit'):
                translated_disagreement = dict(translated_disagreement)
                translated_disagreement['displayCard'] = False
                translation_guidance['disagreement'] = translated_disagreement
            if source_intent_state is not None:
                source_intent_state = json.loads(json.dumps(source_intent_state))
                conflict_choice = source_intent_state.get('conflictChoice')
                if isinstance(conflict_choice, dict):
                    for option in conflict_choice.get('options', []):
                        translated_statement = translated_intent_statements.get((option.get('hypothesisId'), translation['language']))
                        if translated_statement:
                            option['statement'] = translated_statement
                translation_guidance['intentState'] = source_intent_state
        translations_by_turn.setdefault(translation['turn_id'], {})[translation['language']] = {'body': public_text(translation['body'], translation['language']), 'guidance': translation_guidance, 'proposalSummary': translation['proposal_summary'], 'createdAt': translation['created_at']}
    proposal_flow_status = 'inactive'
    proposal_flow_question_count = 0
    proposal_flow_topic_id = None
    proposal_flow_failure_category = None
    proposal_flow_recovery_suggestions = []
    latest_flow_event = database.execute("\n        SELECT event_type, payload_json FROM audit_events\n        WHERE session_id = ?\n          AND event_type IN ('proposal_discovery_started', 'proposal_discovery_progress')\n          AND json_extract(payload_json, '$.stageId') = ?\n        ORDER BY id DESC LIMIT 1\n        ", (session_id, session['current_version_id'])).fetchone()
    if latest_flow_event is not None:
        flow_payload = load_json(latest_flow_event['payload_json']) or {}
        marker_status = 'clarifying' if latest_flow_event['event_type'] == 'proposal_discovery_started' else str(flow_payload.get('status') or 'inactive')
        if marker_status in {'clarifying', 'planning', 'retry_pending', 'revision_needed', 'proposal_ready', 'cancelled'}:
            proposal_flow_status = marker_status
            proposal_flow_question_count = max(0, min(3, int(flow_payload.get('clarificationQuestionCount') or 0)))
            proposal_flow_topic_id = flow_payload.get('topicId')
            proposal_flow_failure_category = flow_payload.get('failureCode') or flow_payload.get('failureClass')
            proposal_flow_recovery_suggestions = list(flow_payload.get('recoverySuggestions') or [])[:3]
    if proposal_flow_status == 'inactive':
        latest_marker = None
        for turn in turns:
            if turn['version_id'] != session['current_version_id'] or turn['role'] != 'assistant':
                continue
            guidance = load_json(turn['guidance_json']) or {}
            marker = guidance.get('proposalDiscovery')
            if isinstance(marker, dict):
                latest_marker = marker
        if isinstance(latest_marker, dict) and latest_marker.get('status') in {'clarifying', 'planning', 'retry_pending', 'revision_needed', 'proposal_ready', 'cancelled'}:
            proposal_flow_status = latest_marker.get('status')
            proposal_flow_question_count = max(0, min(3, int(latest_marker.get('clarificationQuestionCount') or 0)))
            proposal_flow_topic_id = latest_marker.get('topicId')
    binary_topic = binary_discovery.current_topic(database, session_id, session['current_version_id'])
    for turn in turns:
        question = public_guidance_by_turn.get(turn['id'], {}).get('proposalQuestion')
        if not question:
            continue
        selected = next((c['choiceId'] for c in (binary_topic or {}).get('choices', []) if c['questionId'] == question['questionId']), None)
        if selected is None:
            saved_choice = database.execute("SELECT payload_json FROM audit_events WHERE session_id=? AND event_type='binary_choice_selected' AND json_extract(payload_json,'$.questionId')=? ORDER BY id DESC LIMIT 1", (session_id, question['questionId'])).fetchone()
            selected = (load_json(saved_choice['payload_json']) or {}).get('choiceId') if saved_choice else None
        active = bool(binary_topic and binary_topic['topicId'] == question['topicId'] and (binary_topic['status'] == 'clarifying') and ((binary_topic.get('pendingQuestion') or {}).get('questionId') == question['questionId']))
        question.update(selectedChoiceId=selected, status='selected' if selected else 'pending' if active else 'expired', sourceLanguage=turn['language'])
        for locale, translation in translations_by_turn.get(turn['id'], {}).items():
            translated_question = (translation.get('guidance') or {}).get('proposalQuestion')
            if translated_question:
                translated_question.update(selectedChoiceId=selected, status=question['status'], sourceLanguage=locale)
    return {'sessionId': session['id'], 'status': session['status'], 'language': session['language'], 'languageLocked': session['language_locked_at'] is not None, 'demoMode': bool(session['demo_mode']), 'initialDraftMethod': session['initial_draft_method'], 'matchId': session['match_id'], 'playerNumber': session['player_number'], 'currentVersionId': session['current_version_id'], 'lastMessageFailure': _last_message_failure(database, session, turns), 'interactionState': interaction_state(database, session, clock=clock), 'finalVersionId': session['final_version_id'], 'createdAt': session['created_at'], 'updatedAt': session['updated_at'], 'deadlineStartedAt': session['deadline_started_at'], 'deadlineAt': session['deadline_at'], 'deadlineExpired': _deadline_expired(session['deadline_at'], now=effective_now), 'remainingSeconds': _remaining_deadline_seconds(session['deadline_at'], now=effective_now), 'draftPreview': {'rows': load_json(session['draft_rows_json']), 'validation': load_json(session['draft_validation_json']), 'generation': int(session['draft_generation'] or 1), 'regenerationStatus': session['draft_regeneration_status'], 'regenerationRequestId': session['draft_regeneration_request_id'], 'regenerationRequestedAt': session['draft_regeneration_requested_at'], 'regenerationUpdatedAt': session['draft_regeneration_updated_at'], 'failureCode': session['draft_regeneration_failure_code'], 'mutable': session['current_version_id'] is None} if session['draft_rows_json'] else None, 'proposalFlowState': binary_discovery.public_flow(binary_topic) if binary_topic else {'active': proposal_flow_status in {'clarifying', 'planning', 'retry_pending', 'revision_needed'}, 'status': proposal_flow_status, 'topicId': proposal_flow_topic_id, 'clarificationQuestionCount': proposal_flow_question_count, 'canSupplement': proposal_flow_status == 'revision_needed', 'retryable': proposal_flow_status == 'retry_pending', 'failureCategory': proposal_flow_failure_category, 'recoverySuggestions': proposal_flow_recovery_suggestions}, 'versions': [{'versionId': version['id'], 'stageNumber': version['stage_number'], 'parentVersionId': version['parent_version_id'], 'source': version['source'], 'rows': load_json(version['rows_json']), 'summary': version['summary'], 'diff': load_json(version['diff_json']), 'validation': load_json(version['validation_json']), 'createdAt': version['created_at'], 'playAttempts': attempts_by_version.get(version['id'], []), 'executionOutcome': execution_outcomes.get(version['id']), 'openingTurnId': opening_by_version[version['id']]['assistant_turn_id'] if version['id'] in opening_by_version else None, 'openingProposalId': opening_by_version[version['id']]['proposal_id'] if version['id'] in opening_by_version else None} for version in versions], 'progressContexts': progress_contexts, 'challengeReviewRecords': list(challenge_reviews.values()), 'turns': [{'turnId': turn['id'], 'sequence': turn['sequence_number'], 'role': turn['role'], 'content': public_content_by_turn[turn['id']], 'language': turn['language'], 'versionId': turn['version_id'], 'requestId': turn['request_id'], 'requestProposal': turn['role'] == 'user' and turn['request_id'] in proposal_request_keys, 'guidance': public_guidance_by_turn.get(turn['id'], {}), 'proposalState': proposal_state(turn, load_json(turn['guidance_json']) or {}), 'translations': translations_by_turn.get(turn['id'], {}), 'createdAt': turn['created_at']} for turn in turns], 'assessments': [{'assessmentId': assessment['id'], 'versionId': assessment['version_id'], 'assistantTurnId': assessment['assistant_turn_id'], 'payload': public_assessment_payload(load_json(assessment['payload_json']), next((turn['language'] for turn in turns if turn['id'] == assessment['assistant_turn_id']), session['language'])), 'createdAt': assessment['created_at']} for assessment in assessments], 'proposals': [{'proposalId': proposal['id'], 'baseVersionId': proposal['base_version_id'], 'proposedRows': load_json(proposal['proposed_rows_json']), 'summary': proposal['summary'], 'diff': load_json(proposal['diff_json']), 'validation': load_json(proposal['validation_json']), 'status': proposal['status'], 'assistantTurnId': proposal['assistant_turn_id'], 'createdAt': proposal['created_at'], 'decidedAt': proposal['decided_at']} for proposal in proposals], 'intention': {'content': intention['content'], 'language': intention['language'], 'createdAt': intention['created_at']} if intention is not None else None}

def initialize_database(db_path=None, *, clock=None, database=None):
    target_path = Path(db_path) if db_path is not None else DEFAULT_DATABASE_PATH
    owns_connection = database is None
    if owns_connection:
        if str(target_path) != ':memory:':
            target_path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(target_path, timeout=15)
        database.row_factory = sqlite3.Row
    try:
        database.execute('PRAGMA busy_timeout=15000')
        database.execute('PRAGMA journal_mode=WAL')
        database.execute('PRAGMA foreign_keys=ON')
        database.executescript(SCHEMA)
        database.execute('PRAGMA busy_timeout=15000')
        database.execute('PRAGMA foreign_keys=ON')
        _ensure_column(database, 'conversation_turns', 'guidance_json', 'TEXT')
        _ensure_column(database, 'conversation_turns', 'proposal_binding_json', 'TEXT')
        _ensure_column(database, 'level_versions', 'design_context_json', 'TEXT')
        _ensure_column(database, 'level_versions', 'entity_bindings_json', 'TEXT')
        _ensure_column(database, 'design_sessions', 'demo_mode', 'INTEGER NOT NULL DEFAULT 0')
        _ensure_column(database, 'design_sessions', 'deadline_started_at', 'TEXT')
        _ensure_column(database, 'design_sessions', 'deadline_at', 'TEXT')
        _ensure_column(database, 'design_sessions', 'language_locked_at', 'TEXT')
        _ensure_column(database, 'design_sessions', 'draft_rows_json', 'TEXT')
        _ensure_column(database, 'design_sessions', 'draft_validation_json', 'TEXT')
        _ensure_column(database, 'design_sessions', 'draft_generation', 'INTEGER NOT NULL DEFAULT 1')
        _ensure_column(database, 'design_sessions', 'draft_regeneration_status', 'TEXT')
        _ensure_column(database, 'design_sessions', 'draft_regeneration_request_id', 'TEXT')
        _ensure_column(database, 'design_sessions', 'draft_regeneration_key', 'TEXT')
        _ensure_column(database, 'design_sessions', 'draft_regeneration_requested_at', 'TEXT')
        _ensure_column(database, 'design_sessions', 'draft_regeneration_updated_at', 'TEXT')
        _ensure_column(database, 'design_sessions', 'draft_regeneration_failure_code', 'TEXT')
        database.commit()
        backfill_design_contexts(database, clock=clock)
        backfill_entity_bindings(database)
        backfill_revision_challenges(database)
        repair_resolved_challenge_disagreements(database, clock=clock)
        repair_duplicate_progress_questions(database, clock=clock)
        repair_answered_challenge_choice_questions(database, clock=clock)
        backfill_language_locks(database)
        database.commit()
    finally:
        if owns_connection:
            database.close()
