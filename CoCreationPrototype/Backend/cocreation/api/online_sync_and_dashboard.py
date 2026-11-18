"""Online match synchronization (8000) and research dashboard projections."""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from cocreation.persistence.repository import (
    dump_json,
    get_session,
    get_version,
    load_design_context,
    load_json,
)

from .config_models import (
    COCREATION_DEADLINE_SECONDS,
    _online_sync_api_error,
)
from .services import compute_cocreation_duration_seconds

if TYPE_CHECKING:
    from .services import CoCreationServices


def synchronize_final_intention_with_online_match(
    match_id,
    player_number,
    content,
    event_id='',
    session_id='',
    *,
    services: CoCreationServices,
    session=None,
):
    """Synchronize the saved 8010 intention to its linked online player."""
    target = (
        session
        if session is not None
        else {
            'id': session_id,
            'match_id': match_id,
            'player_number': player_number,
            'demo_mode': 0,
        }
    )
    services.online_sync.sync_final_intention(
        target,
        content,
        None,
        None,
        event_id=event_id,
    )


def synchronize_cocreation_event_with_online_match(session, event, *, services: CoCreationServices):
    services.online_sync.sync_event(
        session,
        event.get('eventType', ''),
        stage_number=event.get('stageNumber'),
        version_id=event.get('versionId'),
        source=event.get('source'),
        metadata=event,
    )


def _displayed_cards(guidance):
    guidance = guidance or {}
    cards = []
    disagreement = guidance.get('disagreement')
    if not (isinstance(disagreement, dict) and disagreement.get('status') == 'active') and (
        not guidance.get('discussionCardMode')
    ):
        follow_up = str(guidance.get('followUpQuestion') or '').strip()
        if follow_up:
            cards.append({'type': 'discussion', 'text': follow_up})
    offer = guidance.get('proposalOffer') or {}
    if isinstance(offer, dict):
        offer_text = str(offer.get('summary') or offer.get('text') or '').strip()
        if offer_text:
            cards.append({'type': 'proposal', 'text': offer_text})
    intent = str(guidance.get('intentHypothesis') or '').strip()
    if intent:
        cards.append({'type': 'intent', 'text': intent})
    for cue in guidance.get('uiCues') or []:
        if not isinstance(cue, dict):
            continue
        cue_type = str(cue.get('type') or '').strip()
        cue_text = str(cue.get('text') or '').strip()
        if cue_type and cue_text:
            cards.append({'type': cue_type, 'text': cue_text})
    if isinstance(disagreement, dict) and disagreement.get('status') == 'active' and (
        disagreement.get('displayCard', True) is not False
    ):
        cards.append({
            'type': 'discussion',
            'text': disagreement.get('coreDisagreement') or disagreement.get('nextQuestion'),
            'disagreement': disagreement,
        })
    return cards


def _dashboard_node_entry(entry_id, kind, text, occurred_at, *, label='', language=''):
    """Create one bounded, public dashboard entry from persisted 8010 data."""
    return {
        'entryId': str(entry_id),
        'kind': str(kind),
        'text': str(text or '').strip()[:12000],
        'label': str(label or '').strip()[:160],
        'occurredAt': str(occurred_at),
        'language': language if language in {'en', 'zh-CN'} else '',
    }


def _dashboard_turn_kind(guidance, challenge_turn_ids):
    guidance = guidance or {}
    challenge_state = guidance.get('challengeState') or {}
    challenge_id = str(challenge_state.get('challengeId') or '').strip()
    if challenge_id:
        return ('player_challenge', 'player-challenge:' + challenge_id, None)
    if (guidance.get('disagreement') or {}).get('subject') == 'human_edit':
        return ('llm_challenge', '', None)
    marker = guidance.get('dashboardNode') or {}
    review = guidance.get('_intentReviewState') or {}
    hypothesis_id = str(marker.get('intentHypothesisId') or guidance.get('_intentHypothesisId') or '').strip()
    if hypothesis_id and (marker.get('intentConflict') or review.get('interactionMode') == 'conflict_choice'):
        return ('intent_conflict', 'intent-conflict:' + hypothesis_id, None)
    if hypothesis_id and guidance.get('intentHypothesis'):
        return ('intent', 'intent:' + hypothesis_id, None)
    discovery = guidance.get('proposalDiscovery') or {}
    topic_id = str(marker.get('proposalTopicId') or discovery.get('topicId') or '').strip()
    if topic_id:
        return ('proposal', 'proposal:' + topic_id, None)
    if guidance.get('proposalOffer'):
        return ('proposal', 'proposal:' + str(guidance.get('proposalId') or '').strip(), None)
    return ('discussion', '', None)


def _dashboard_node_status(node_type, guidance, challenge_status=''):
    guidance = guidance or {}
    if node_type == 'proposal':
        marker = guidance.get('proposalDiscovery') or {}
        status = str(marker.get('status') or 'in_progress')
        count = int(marker.get('clarificationQuestionCount') or 0)
        if status == 'clarifying' and count in {1, 2, 3}:
            return f'clarifying_{count}'
        if guidance.get('proposalOffer'):
            return 'proposal_ready'
        return status if re.fullmatch('[a-z_]{2,64}', status) else 'in_progress'
    if node_type in {'intent', 'intent_conflict'}:
        state = guidance.get('_intentReviewState') or {}
        if state.get('interactionMode') in {'full', 'adjust_only', 'conflict_choice'}:
            return 'awaiting_player_choice'
        return 'in_progress'
    if node_type == 'player_challenge':
        states = {
            'awaiting_reason': 'in_progress',
            'reviewing': 'in_progress',
            'reason_review': 'awaiting_player_choice',
            'choice_pending': 'awaiting_player_choice',
            'resolved': 'resolved',
        }
        return states.get(challenge_status, 'in_progress')
    return 'in_progress'


def _dashboard_entries_for_turns(database, session_id, assistant_rows, *, card_types=None):
    entries = []
    for assistant in assistant_rows:
        request_id = assistant['request_id']
        user = database.execute(
            "\n            SELECT * FROM conversation_turns\n"
            "            WHERE session_id = ? AND request_id = ? AND role = 'user'\n            ",
            (session_id, request_id),
        ).fetchone()
        if user is not None:
            entries.append(
                _dashboard_node_entry(
                    'player:' + user['id'],
                    'player_message',
                    user['content'],
                    user['created_at'],
                    label='Player',
                    language=user['language'],
                )
            )
        entries.append(
            _dashboard_node_entry(
                'llm:' + assistant['id'],
                'llm_message',
                assistant['content'],
                assistant['created_at'],
                label='LLM',
                language=assistant['language'],
            )
        )
        for index, card in enumerate(_displayed_cards(load_json(assistant['guidance_json']))):
            if card_types is not None and card.get('type') not in card_types:
                continue
            card_text = card.get('text')
            if card.get('type') == 'discussion':
                disagreement = card.get('disagreement') or {}
                card_text = '\n'.join(
                    (str(disagreement.get(field) or '') for field in ('userPosition', 'coreDisagreement', 'nextQuestion'))
                ).strip() or card_text
            entries.append(
                _dashboard_node_entry(
                    f"card:{assistant['id']}:{index}",
                    'card',
                    card_text,
                    assistant['created_at'],
                    label='Card',
                    language=assistant['language'],
                )
            )
    return [entry for entry in entries if entry['text']]


def _dashboard_challenge_proposal_origin(database, session_id, turn):
    """Bind a post-choice proposal and its execution to a distinct research node."""
    guidance = load_json(turn['guidance_json']) or {}
    state = guidance.get('challengeState') or {}
    if state.get('status') == 'resolved' and state.get('challengeId'):
        candidate = database.execute(
            'SELECT id FROM change_proposals WHERE session_id = ? AND assistant_turn_id = ?',
            (session_id, turn['id']),
        ).fetchone()
        return turn if guidance.get('proposalOffer') or candidate else None
    action = database.execute(
        "SELECT payload_json FROM audit_events WHERE session_id = ?\n"
        "           AND event_type = 'revision_execution_requested'\n"
        "           AND json_extract(payload_json, '$.messageKey') = ?\n"
        "           ORDER BY id DESC LIMIT 1",
        (session_id, turn['request_id']),
    ).fetchone()
    if action is None:
        return None
    source_id = (load_json(action['payload_json']) or {}).get('sourceTurnId')
    source = database.execute(
        'SELECT * FROM conversation_turns WHERE session_id = ? AND id = ?',
        (session_id, source_id),
    ).fetchone()
    state = (load_json(source['guidance_json']) or {}).get('challengeState') or {} if source else {}
    if state.get('status') == 'resolved' and state.get('challengeId'):
        return source
    return None


def _dashboard_append_proposal_results(database, session, node, turns):
    for turn in turns:
        proposals = database.execute(
            'SELECT * FROM change_proposals WHERE session_id = ? AND assistant_turn_id = ? ORDER BY created_at, id',
            (session['id'], turn['id']),
        ).fetchall()
        for proposal in proposals:
            node['nodeEntries'].append(
                _dashboard_node_entry(
                    'proposal-verified:' + proposal['id'],
                    'map_diff',
                    dump_json({'diff': load_json(proposal['diff_json']) or [], 'validation': load_json(proposal['validation_json']) or {}}),
                    proposal['created_at'],
                    label='Verified proposal result',
                )
            )
            node['nodeStatus'] = proposal['status']
            decision = database.execute(
                'SELECT * FROM designer_decisions WHERE session_id = ? AND proposal_id = ? ORDER BY created_at DESC, id DESC LIMIT 1',
                (session['id'], proposal['id']),
            ).fetchone()
            if decision is None:
                continue
            accepted = decision['decision_type'] == 'accept'
            action = 'Accepted candidate map' if accepted else 'Rejected proposal'
            if accepted and decision['version_id']:
                version = get_version(database, session['id'], decision['version_id'])
                action += f" · saved as Stage {version['stage_number']}"
            node['nodeEntries'].append(
                _dashboard_node_entry(
                    'proposal-action:' + proposal['id'],
                    'player_action',
                    action,
                    decision['created_at'],
                    label='Player action',
                )
            )
            if decision['reason']:
                node['nodeEntries'].append(
                    _dashboard_node_entry(
                        'proposal-reason:' + proposal['id'],
                        'player_message',
                        decision['reason'],
                        decision['created_at'],
                        label='Player',
                        language=session['language'],
                    )
                )
            node['nodeStatus'] = 'accepted' if accepted else 'rejected'
    node['nodeEntries'] = node['nodeEntries'][-96:]


def synchronize_dashboard_player_challenge(session_id, challenge_id, *, services: CoCreationServices):
    """Project the whole challenge, including persisted reasons without a reply."""
    with services.connect() as database:
        session = get_session(database, session_id)
        if session is None or bool(session['demo_mode']):
            return
        challenge = database.execute(
            'SELECT * FROM revision_challenges WHERE session_id = ? AND challenge_id = ?',
            (session_id, challenge_id),
        ).fetchone()
        if challenge is None:
            return
        source = database.execute(
            'SELECT * FROM conversation_turns WHERE session_id = ? AND id = ?',
            (session_id, challenge['source_proposal_turn_id']),
        ).fetchone()
        parent = _dashboard_node_projection_for_turn(database, session, source['request_id']) if source else None
        rows = database.execute(
            "SELECT * FROM conversation_turns WHERE session_id = ? AND version_id = ? AND role = 'assistant' ORDER BY sequence_number",
            (session_id, challenge['base_version_id']),
        ).fetchall()
        turns = [
            row
            for row in rows
            if row['id'] == challenge['challenge_turn_id']
            or ((load_json(row['guidance_json']) or {}).get('challengeState') or {}).get('challengeId') == challenge_id
        ]
        entries = _dashboard_entries_for_turns(database, session_id, turns, card_types={'discussion'})
        entries.insert(
            0,
            _dashboard_node_entry(
                'challenge-action:' + challenge_id,
                'player_action',
                'Challenge this plan',
                challenge['created_at'],
                label='Player action',
            ),
        )
        summary = ((load_json(source['guidance_json']) or {}).get('proposalOffer') or {}).get('summary') if source else None
        if summary:
            entries.insert(
                1,
                _dashboard_node_entry(
                    'challenge-source:' + challenge_id,
                    'status',
                    summary,
                    challenge['created_at'],
                    label='Challenged proposal',
                    language=session['language'],
                ),
            )
        continued = database.execute(
            "SELECT payload_json FROM audit_events WHERE session_id = ?\n"
            "               AND event_type = 'challenge_continued'\n"
            "               AND json_extract(payload_json, '$.challengeId') = ? ORDER BY id",
            (session_id, challenge_id),
        ).fetchall()
        entry_ids = {entry['entryId'] for entry in entries}
        for event in continued:
            message_key = load_json(event['payload_json'])['messageKey']
            user = database.execute(
                "SELECT * FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'user'",
                (session_id, message_key),
            ).fetchone()
            if user is not None and 'player:' + user['id'] not in entry_ids:
                reviewed = database.execute(
                    'SELECT review_id FROM challenge_reason_reviews WHERE session_id = ? AND challenge_id = ? AND source_user_turn_id = ?',
                    (session_id, challenge_id, user['id']),
                ).fetchone()
                failed = database.execute(
                    "SELECT id FROM audit_events WHERE session_id = ?\n"
                    "                       AND event_type = 'dashboard_challenge_request_failed'\n"
                    "                       AND json_extract(payload_json, '$.messageKey') = ?",
                    (session_id, message_key),
                ).fetchone()
                if reviewed is None and failed is None:
                    continue
                entries.append(
                    _dashboard_node_entry(
                        'player:' + user['id'],
                        'player_message',
                        user['content'],
                        user['created_at'],
                        label='Player',
                        language=user['language'],
                    )
                )
                entry_ids.add('player:' + user['id'])
        status = _dashboard_node_status('player_challenge', {}, challenge['status'])
        if challenge['status'] == 'awaiting_reason':
            status = 'awaiting_reason'
        elif challenge['status'] == 'review_pending':
            status = 'review_pending'
        if challenge['status'] == 'resolved':
            resolution = next(
                (
                    (load_json(row['guidance_json']) or {}).get('disagreement') or {}
                    for row in reversed(turns)
                    if ((load_json(row['guidance_json']) or {}).get('challengeState') or {}).get('status') == 'resolved'
                ),
                {},
            )
            choice = resolution.get('resolution')
            status = {'ai': 'original_selected', 'user': 'replacement_selected'}.get(choice, 'resolved')
            if turns and choice in {'ai', 'user'}:
                entries.append(
                    _dashboard_node_entry(
                        'challenge-choice:' + challenge_id,
                        'player_action',
                        'Selected the original frozen proposal'
                        if choice == 'ai'
                        else "Selected a new plan from the player's reason",
                        turns[-1]['created_at'],
                        label='Player choice',
                    )
                )
        failures = database.execute(
            "SELECT payload_json, created_at FROM audit_events WHERE session_id = ?\n"
            "               AND event_type = 'dashboard_challenge_request_failed'\n"
            "               AND json_extract(payload_json, '$.challengeId') = ? ORDER BY id",
            (session_id, challenge_id),
        ).fetchall()
        for failure in failures:
            data = load_json(failure['payload_json'])
            if challenge['status'] == 'resolved':
                continue
            if (
                database.execute(
                    "SELECT id FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'assistant'",
                    (session_id, data['messageKey']),
                ).fetchone()
                is None
            ):
                entries.append(
                    _dashboard_node_entry(
                        'challenge-failure:' + data['messageKey'],
                        'status',
                        'Request pending retry; no successful plan recorded (' + data['code'] + ').',
                        failure['created_at'],
                        label='Pending request',
                    )
                )
        version = get_version(database, session_id, challenge['base_version_id'])
        node = {
            'nodeId': 'player-challenge:' + challenge_id,
            'nodeType': 'player_challenge',
            'nodeStatus': status,
            'nodeParentId': parent['nodeId'] if parent else None,
            'versionId': version['id'],
            'stageNumber': version['stage_number'],
            'nodeEntries': list({entry['entryId']: entry for entry in entries}.values())[-96:],
        }
    _synchronize_dashboard_node(session, node, services=services)


def _dashboard_node_projection_for_turn(database, session, request_id):
    assistant = database.execute(
        "\n        SELECT * FROM conversation_turns\n"
        "        WHERE session_id = ? AND request_id = ? AND role = 'assistant'\n        ",
        (session['id'], request_id),
    ).fetchone()
    if assistant is None:
        return None
    rows = database.execute(
        "\n        SELECT * FROM conversation_turns\n"
        "        WHERE session_id = ? AND role = 'assistant' AND version_id = ?\n"
        "          AND id NOT IN (SELECT assistant_turn_id FROM llm_assessments)\n"
        "        ORDER BY sequence_number\n        ",
        (session['id'], assistant['version_id']),
    ).fetchall()
    challenge_rows = database.execute(
        '\n        SELECT challenge_id, challenge_turn_id, source_proposal_turn_id, status, created_at\n'
        '        FROM revision_challenges WHERE session_id = ?\n        ',
        (session['id'],),
    ).fetchall()
    challenge_by_turn = {row['challenge_turn_id']: row for row in challenge_rows}
    classified = []
    for row in rows:
        guidance = load_json(row['guidance_json']) or {}
        node_type, node_id, parent_id = _dashboard_turn_kind(guidance, challenge_by_turn)
        origin = _dashboard_challenge_proposal_origin(database, session['id'], row)
        if origin is not None:
            origin_guidance = load_json(origin['guidance_json']) or {}
            origin_challenge_id = origin_guidance['challengeState']['challengeId']
            node_type = 'proposal'
            node_id = 'proposal:challenge:' + origin_challenge_id
            parent_id = 'player-challenge:' + origin_challenge_id
        challenge = challenge_by_turn.get(row['id'])
        if challenge is not None:
            node_type = 'player_challenge'
            node_id = 'player-challenge:' + challenge['challenge_id']
            source = database.execute(
                'SELECT guidance_json FROM conversation_turns WHERE id = ?',
                (challenge['source_proposal_turn_id'],),
            ).fetchone()
            source_marker = load_json(source['guidance_json']) if source else {}
            source_marker = source_marker or {}
            topic_id = str((source_marker.get('proposalDiscovery') or {}).get('topicId') or '')
            parent_id = 'proposal:' + topic_id if topic_id else None
        classified.append((row, guidance, node_type, node_id, parent_id))
    current = next((item for item in classified if item[0]['id'] == assistant['id']))
    row, guidance, node_type, node_id, parent_id = current
    if node_type == 'discussion':
        current_index = next((i for i, item in enumerate(classified) if item[0]['id'] == assistant['id']))
        index = current_index
        while index > 0 and classified[index - 1][2] == 'discussion':
            index -= 1
        end = current_index + 1
        while end < len(classified) and classified[end][2] == 'discussion':
            end += 1
        selected = classified[index:end]
        node_id = 'discussion:' + selected[0][0]['id']
    else:
        selected = [item for item in classified if item[3] == node_id]
    guidance = selected[-1][1]
    challenge_status = ''
    if node_type == 'player_challenge':
        challenge_id = node_id.removeprefix('player-challenge:')
        challenge = next((item for item in challenge_rows if item['challenge_id'] == challenge_id), None)
        challenge_status = challenge['status'] if challenge is not None else ''
    version = get_version(database, session['id'], row['version_id'])
    entries = _dashboard_entries_for_turns(database, session['id'], [item[0] for item in selected])
    if node_type == 'player_challenge' and challenge_status:
        challenge_id = node_id.removeprefix('player-challenge:')
        challenge = next((item for item in challenge_rows if item['challenge_id'] == challenge_id), None)
        if challenge is not None:
            entries.insert(
                0,
                _dashboard_node_entry(
                    'challenge-action:' + challenge_id,
                    'player_action',
                    'Challenge this plan',
                    challenge['created_at'] if 'created_at' in challenge.keys() else row['created_at'],
                    label='Player action',
                    language='',
                ),
            )
    if not entries:
        return None
    node = {
        'nodeId': node_id,
        'nodeType': node_type,
        'nodeStatus': _dashboard_node_status(node_type, guidance, challenge_status),
        'nodeParentId': parent_id,
        'versionId': row['version_id'],
        'stageNumber': version['stage_number'] if version is not None else None,
        'nodeEntries': entries[-96:],
    }
    if node_type == 'proposal':
        _dashboard_append_proposal_results(database, session, node, [item[0] for item in selected])
    return node


def synchronize_dashboard_node_for_turn(session_id, request_id, *, services: CoCreationServices):
    with services.connect() as database:
        session = get_session(database, session_id)
        if session is None or bool(session['demo_mode']):
            return
        node = _dashboard_node_projection_for_turn(database, session, request_id)
        turn = database.execute(
            "SELECT * FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'assistant'",
            (session_id, request_id),
        ).fetchone()
        guidance = load_json(turn['guidance_json']) or {} if turn else {}
        challenge_id = (guidance.get('challengeState') or {}).get('challengeId')
        if not challenge_id and turn is not None:
            origin = _dashboard_challenge_proposal_origin(database, session_id, turn)
            if origin is not None:
                challenge_id = load_json(origin['guidance_json'])['challengeState']['challengeId']
        hypothesis_id = (guidance.get('dashboardNode') or {}).get('intentHypothesisId') or guidance.get('_intentHypothesisId')
    if node is None:
        return
    if challenge_id:
        synchronize_dashboard_player_challenge(session_id, challenge_id, services=services)
    if hypothesis_id:
        synchronize_dashboard_intent_node(session_id, hypothesis_id, services=services)
    elif node['nodeType'] not in {'player_challenge', 'llm_challenge'}:
        _synchronize_dashboard_node(session, node, services=services)
    if node.get('versionId'):
        synchronize_manual_edit_review_nodes(session_id, node['versionId'], services=services)


def _synchronize_dashboard_node(session, node, *, services: CoCreationServices):
    """Send one immutable snapshot.  8000 keeps only its latest revision."""
    fingerprint = hashlib.sha256(dump_json(node).encode('utf-8')).hexdigest()[:24]
    synchronize_cocreation_event_with_online_match(
        session,
        {'eventId': f"node:{node['nodeId']}:{fingerprint}", 'eventType': 'node', **node},
        services=services,
    )


def synchronize_manual_edit_review_nodes(session_id, version_id, *, services: CoCreationServices):
    """Project the two reviewed human-edit observations and any evidence-backed challenge."""
    with services.connect() as database:
        session = get_session(database, session_id)
        version = get_version(database, session_id, version_id)
        if session is None or version is None or bool(session['demo_mode']):
            return
        if version['source'] != 'human_edit':
            return
        event_row = database.execute(
            "\n            SELECT payload_json, created_at FROM audit_events\n"
            "            WHERE session_id = ? AND event_type = 'human_edit_reviewed'\n"
            "              AND json_extract(payload_json, '$.versionId') = ?\n"
            "            ORDER BY id DESC LIMIT 1\n            ",
            (session_id, version_id),
        ).fetchone()
        if event_row is None:
            return
        payload = load_json(event_row['payload_json']) or {}
        turn_ids = [payload.get('openingTurnId'), payload.get('reviewTurnId')]
        turns = []
        for turn_id in turn_ids:
            if not turn_id:
                continue
            turn = database.execute(
                "SELECT * FROM conversation_turns WHERE id = ? AND role = 'assistant'",
                (turn_id,),
            ).fetchone()
            if turn is not None:
                turns.append(turn)
        entries = []
        summary = payload.get('changeSummary')
        if summary:
            entries.append(
                _dashboard_node_entry(
                    'manual-save:' + version_id,
                    'player_action',
                    dump_json(summary),
                    event_row['created_at'],
                    label='Player action',
                    language=session['language'],
                )
            )
        entries.append(
            _dashboard_node_entry(
                'verified-diff:' + version_id,
                'map_diff',
                dump_json(payload.get('diff') or load_json(version['diff_json']) or {}),
                event_row['created_at'],
                label='Verified map change',
                language='',
            )
        )
        entries.extend(_dashboard_entries_for_turns(database, session_id, turns, card_types=set()))
        entries = [entry for entry in entries if entry['text']]
        if not entries:
            return
        manual_node_id = 'manual-review:' + version_id
        manual_node = {
            'nodeId': manual_node_id,
            'nodeType': 'manual_edit_review',
            'nodeStatus': 'reviewed',
            'versionId': version_id,
            'stageNumber': version['stage_number'],
            'nodeEntries': entries[-96:],
        }
        initial_disagreement = payload.get('disagreement') or {}
        challenge_node = None
        if initial_disagreement.get('subject') == 'human_edit':
            challenge_id = 'human-edit:' + version_id
            challenge_entries = [
                _dashboard_node_entry(
                    'llm-challenge-card:' + challenge_id,
                    'card',
                    '\n'.join(
                        (str(initial_disagreement.get(field) or '') for field in ('userPosition', 'coreDisagreement', 'nextQuestion'))
                    ).strip(),
                    event_row['created_at'],
                    label="LET'S DISCUSS",
                    language=session['language'],
                )
            ]
            events = database.execute(
                "SELECT payload_json, created_at FROM audit_events WHERE session_id = ?\n"
                "                   AND event_type IN ('disagreement_started', 'disagreement_updated',\n"
                "                                      'disagreement_acknowledged', 'disagreement_resolved')\n"
                "                   AND json_extract(payload_json, '$.versionId') = ?\n"
                "                   AND json_extract(payload_json, '$.subject') = 'human_edit'\n"
                "                   ORDER BY id",
                (session_id, version_id),
            ).fetchall()
            latest = initial_disagreement
            followups = []
            seen_turn_ids = set(turn_ids)
            for event in events:
                data = load_json(event['payload_json']) or {}
                latest = data.get('disagreement') or latest
                turn_id = data.get('turnId')
                if turn_id and turn_id not in seen_turn_ids:
                    turn = database.execute(
                        "SELECT * FROM conversation_turns WHERE session_id = ? AND id = ? AND role = 'assistant' AND version_id = ?",
                        (session_id, turn_id, version_id),
                    ).fetchone()
                    if turn is not None:
                        followups.append(turn)
                        seen_turn_ids.add(turn_id)
            challenge_entries.extend(_dashboard_entries_for_turns(database, session_id, followups, card_types=set()))
            failures = database.execute(
                "SELECT payload_json, created_at FROM audit_events WHERE session_id = ?\n"
                "                   AND event_type = 'dashboard_human_edit_request_failed'\n"
                "                   AND json_extract(payload_json, '$.versionId') = ? ORDER BY id",
                (session_id, version_id),
            ).fetchall()
            for failure in failures:
                data = load_json(failure['payload_json'])
                if (
                    database.execute(
                        "SELECT id FROM conversation_turns WHERE session_id = ? AND request_id = ? AND role = 'assistant'",
                        (session_id, data['messageKey']),
                    ).fetchone()
                    is not None
                ):
                    continue
                user = database.execute(
                    "SELECT * FROM conversation_turns WHERE session_id = ? AND id = ? AND role = 'user'",
                    (session_id, data['userTurnId']),
                ).fetchone()
                if user is not None:
                    challenge_entries.extend([
                        _dashboard_node_entry(
                            'player:' + user['id'],
                            'player_message',
                            user['content'],
                            user['created_at'],
                            label='Player',
                            language=user['language'],
                        ),
                        _dashboard_node_entry(
                            'llm-challenge-failure:' + data['messageKey'],
                            'status',
                            'No assistant reply was saved for this message (' + data['code'] + ').',
                            failure['created_at'],
                            label='Reply pending retry',
                        ),
                    ])
            status = {'active': 'in_progress', 'acknowledged': 'acknowledged', 'resolved': 'resolved'}.get(
                latest.get('status'),
                'in_progress',
            )
            if latest.get('resolutionMode') == 'manual_reedit':
                status = 'manual_reedit'
                result_version = get_version(database, session_id, latest.get('resultVersionId'))
                if result_version is not None:
                    challenge_entries.append(
                        _dashboard_node_entry(
                            'llm-challenge-result:' + challenge_id,
                            'player_action',
                            f"Saved a new manual edit as Stage {result_version['stage_number']} ({result_version['id']}).",
                            result_version['created_at'],
                            label='New Stage',
                        )
                    )
            if latest.get('status') == 'acknowledged':
                challenge_entries.append(
                    _dashboard_node_entry(
                        'llm-challenge-status:' + challenge_id,
                        'status',
                        'The designer responded; the proposal block was released. No agreement or map save is implied.',
                        events[-1]['created_at'] if events else event_row['created_at'],
                        label='Discussion status',
                    )
                )
            challenge_node = {
                'nodeId': 'llm-challenge:' + challenge_id,
                'nodeType': 'llm_challenge',
                'nodeStatus': status,
                'nodeParentId': manual_node_id,
                'versionId': version_id,
                'stageNumber': version['stage_number'],
                'nodeEntries': list({entry['entryId']: entry for entry in challenge_entries if entry['text']}.values())[-96:],
            }
    _synchronize_dashboard_node(session, manual_node, services=services)
    if challenge_node is not None:
        _synchronize_dashboard_node(session, challenge_node, services=services)


def synchronize_dashboard_intent_node(session_id, hypothesis_id, *, services: CoCreationServices):
    """Add the explicit card decision to the same public intent snapshot."""
    with services.connect() as database:
        session = get_session(database, session_id)
        if session is None or bool(session['demo_mode']):
            return
        source = None
        for row in database.execute(
            "SELECT * FROM conversation_turns WHERE session_id = ? AND role = 'assistant' ORDER BY sequence_number",
            (session_id,),
        ).fetchall():
            guidance = load_json(row['guidance_json']) or {}
            marker = guidance.get('dashboardNode') or {}
            if (marker.get('intentHypothesisId') or guidance.get('_intentHypothesisId')) == hypothesis_id:
                source = row
        if source is None:
            return
        node = _dashboard_node_projection_for_turn(database, session, source['request_id'])
        if node is None:
            return
        feedback = database.execute(
            "SELECT id, payload_json, created_at FROM audit_events\n"
            "               WHERE session_id = ? AND event_type IN (\n"
            "                 'intent_hypothesis_feedback_applied', 'intent_hypothesis_feedback_pending')\n"
            "               ORDER BY id",
            (session_id,),
        ).fetchall()
        context = load_design_context(database, session_id, source['version_id']) or {}
        for event in feedback:
            payload = load_json(event['payload_json']) or {}
            if payload.get('hypothesisId') != hypothesis_id:
                continue
            occurred_at = event['created_at']
            request_data = payload.get('request') or {}
            action = request_data.get('action')
            outcome = str(payload.get('outcome') or '')
            event_key = str(payload.get('idempotencyKey') or event['id'])
            candidate = request_data.get('candidateText')
            if candidate:
                node['nodeEntries'].append(
                    _dashboard_node_entry(
                        'intent-input:' + event_key,
                        'player_message',
                        candidate,
                        occurred_at,
                        label='Player revision',
                        language=session['language'],
                    )
                )
            labels = {
                'applied': 'Revised tentative intention' if action == 'revise' else 'Confirmed tentative intention',
                'rejected': 'Rejected tentative intention',
                'kept_new': 'Kept new intention',
                'kept_existing': 'Kept existing intention',
                'conflict': 'Intent conflict requires a choice',
                'unclear': 'Intent revision pending review',
            }
            node['nodeEntries'].append(
                _dashboard_node_entry(
                    'intent-action:' + event_key,
                    'player_action',
                    labels.get(outcome, 'Updated tentative intention'),
                    occurred_at,
                    label='Player action',
                )
            )
            node['nodeStatus'] = {
                'applied': 'revised' if action == 'revise' else 'confirmed',
                'rejected': 'rejected',
                'kept_new': 'resolved',
                'kept_existing': 'resolved',
                'conflict': 'awaiting_player_choice',
                'unclear': 'awaiting_player_choice',
            }.get(outcome, node['nodeStatus'])
            statement = payload.get('reviewedStatement')
            if statement:
                node['nodeEntries'].append(
                    _dashboard_node_entry(
                        'intent-statement:' + event_key,
                        'status',
                        statement,
                        occurred_at,
                        label='Reviewed intention',
                        language=session['language'],
                    )
                )
            if outcome in {'conflict', 'unclear'} and payload.get('explanation'):
                node['nodeEntries'].append(
                    _dashboard_node_entry(
                        'intent-review:' + event_key,
                        'llm_message',
                        payload['explanation'],
                        occurred_at,
                        label='Review',
                        language=session['language'],
                    )
                )
            if node['nodeType'] == 'intent_conflict':
                for item in context.get('intentHypotheses', []):
                    if item.get('id') not in {hypothesis_id, payload.get('existingHypothesisId')}:
                        continue
                    node['nodeEntries'].append(
                        _dashboard_node_entry(
                            'intent-conflict-statement:' + str(item.get('id')),
                            'status',
                            item.get('displayStatement') or item.get('statement') or '',
                            occurred_at,
                            label='Intention',
                            language=session['language'],
                        )
                    )
        node['nodeEntries'] = node['nodeEntries'][-96:]
    _synchronize_dashboard_node(session, node, services=services)


def synchronize_dashboard_proposal_node(session_id, proposal_id, *, services: CoCreationServices):
    """Publish the same canonical proposal projection used by message retries."""
    with services.connect() as database:
        session = get_session(database, session_id)
        proposal = database.execute(
            'SELECT * FROM change_proposals WHERE id = ? AND session_id = ?',
            (proposal_id, session_id),
        ).fetchone()
        if session is None or proposal is None or bool(session['demo_mode']):
            return
        source = database.execute(
            'SELECT * FROM conversation_turns WHERE session_id = ? AND id = ?',
            (session_id, proposal['assistant_turn_id']),
        ).fetchone()
        node = _dashboard_node_projection_for_turn(database, session, source['request_id']) if source else None
    if node is not None:
        _synchronize_dashboard_node(session, node, services=services)


def synchronize_version_with_online_match(session_id, version_id, event_type, *, services: CoCreationServices):
    with services.connect() as database:
        session = get_session(database, session_id)
        version = get_version(database, session_id, version_id)
        if session is None or version is None:
            return
        if bool(session['demo_mode']):
            return
        duration = (
            calculate_cocreation_duration_seconds(
                session,
                cocreation_deadline_seconds=services.settings.cocreation_deadline_seconds,
            )
            if event_type == 'final'
            else None
        )
        services.online_sync.sync_version(
            database,
            session,
            version,
            event_type=event_type,
            duration_seconds=duration,
        )


def calculate_cocreation_duration_seconds(
    session,
    *,
    cocreation_deadline_seconds: int = COCREATION_DEADLINE_SECONDS,
):
    """Return the consumed portion of the confirmed-language workbench deadline."""
    return compute_cocreation_duration_seconds(
        session,
        cocreation_deadline_seconds=cocreation_deadline_seconds,
    )


def synchronize_opening_with_online_match(session_id, version_id, opening, *, services: CoCreationServices):
    with services.connect() as database:
        session = get_session(database, session_id)
        version = get_version(database, session_id, version_id)
        if session is None or version is None:
            return
        event = {
            'eventId': f"opening:{opening['assistantTurnId']}",
            'eventType': 'opening',
            'versionId': version['id'],
            'stageNumber': version['stage_number'],
            'assistantTurnId': opening['assistantTurnId'],
            'assistantText': opening['assistantText'],
            'language': opening['language'],
            'cards': opening['cards'],
        }
    synchronize_cocreation_event_with_online_match(session, event, services=services)


def synchronize_turn_with_online_match(session_id, request_id, *, services: CoCreationServices):
    """Publish the safe, grouped dashboard projection instead of a raw Q&A turn."""
    synchronize_dashboard_node_for_turn(session_id, request_id, services=services)
