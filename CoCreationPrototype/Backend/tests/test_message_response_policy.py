import copy
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cocreation.api.config_models import SAMPLE_ROWS
from cocreation.api.message_response_policy import apply_conversation_only_policy, apply_map_claim_conflict_policy
from cocreation.llm.config_and_runtime import LLMExecutionResult
from cocreation.persistence.database import load_json
from tests.fakes import RecordedCall, SessionTestCase


class MessageResponsePolicyTests(unittest.TestCase):
    def setUp(self):
        self.conflicts = [{'kind': 'tile', 'row': 3, 'column': 4, 'actualTile': '.'}]
        self.execution = LLMExecutionResult(
            ' Keep the passage readable. ', 2, 'offline', model='kimi-test',
            proposed_rows=['candidate'],
            revision_plan={'effect': 'widen'},
            revision_contract={'allowedOperators': ['remove_wall']},
            revision_operations=[{'operator': 'remove_wall'}],
            proposal_binding={'baseVersionId': 'stage-1'},
            proposal_diagnostics={'fullQualityPassed': True},
            guidance={
                'proposalOffer': {'summary': 'Move an internal wall'},
                'intentHypothesis': 'You prefer a wider passage.',
                'intentConfidence': 'medium',
                'followUpQuestion': 'Should I change it?',
                'disagreement': {'status': 'acknowledged'},
                'uiCues': [{'type': 'warning'}],
                'coordinateLinks': [{'text': 'a grounded route'}],
                'designContextPatch': {'userGoals': []},
                'designContextPatchError': 'invalid patch',
                'proposalDiscovery': {'topicId': 'topic-1', 'status': 'clarifying'},
            },
        )

    def assert_revision_removed(self, result):
        self.assertIsNone(result.proposed_rows)
        self.assertEqual(result.revision_plan, {})
        self.assertEqual(result.revision_contract, {})
        self.assertEqual(result.revision_operations, [])
        self.assertEqual(result.proposal_binding, {})

    def test_map_correction_uses_reply_language_and_is_not_prepended_twice(self):
        cases = (
            ('en', 'I would keep the passage readable.', 'The tile you gave at row 3, column 4', 'the saved map has floor'),
            ('zh-CN', '我会保留清楚的通道。', '你提到的第 3 行第 4 列', '当前保存地图中这里是地面'),
        )
        for language, body, start, tile in cases:
            with self.subTest(language=language):
                execution = replace(self.execution, assistant_message=f' {body} ')
                corrected = apply_map_claim_conflict_policy(execution, self.conflicts, language)
                prefix, visible_body = corrected.assistant_message.split('\n\n', 1)
                self.assertTrue(prefix.startswith(start), prefix)
                self.assertIn(tile, prefix)
                self.assertEqual(visible_body, body)
                # The existing correction may already be present later in the reply.
                existing = replace(execution, assistant_message=f'{body}\n\n{prefix}')
                unchanged_body = apply_map_claim_conflict_policy(existing, self.conflicts, language)
                self.assertEqual(unchanged_body.assistant_message, existing.assistant_message)

    def test_map_conflict_discards_unverified_actions_and_keeps_other_diagnostics(self):
        original = copy.deepcopy(self.execution)
        result = apply_map_claim_conflict_policy(self.execution, self.conflicts, 'en')
        self.assert_revision_removed(result)
        for key in ('proposalOffer', 'intentHypothesis', 'intentConfidence', 'followUpQuestion', 'disagreement'):
            self.assertIsNone(result.guidance[key], key)
        self.assertEqual(result.guidance['coordinateLinks'], [])
        self.assertEqual(result.guidance['uiCues'], [])
        self.assertNotIn('designContextPatch', result.guidance)
        self.assertNotIn('designContextPatchError', result.guidance)
        self.assertEqual(result.proposal_diagnostics, {'fullQualityPassed': True, 'category': 'map_claim_conflict'})
        self.assertEqual((result.attempts_used, result.request_id, result.model), (2, 'offline', 'kimi-test'))
        self.assertEqual(self.execution, original)

    def test_no_map_conflict_leaves_original_reply_untouched(self):
        for conflicts in (None, []):
            with self.subTest(conflicts=conflicts):
                self.assertIs(apply_map_claim_conflict_policy(self.execution, conflicts, 'en'), self.execution)

    def test_conversation_only_carries_links_and_canonical_disagreement_without_cards(self):
        active = {'subject': 'human_edit', 'status': 'active', 'disagreementId': 'current', 'displayCard': True}
        original = copy.deepcopy(self.execution)
        result = apply_conversation_only_policy(self.execution, active_disagreement=active)
        self.assert_revision_removed(result)
        self.assertEqual(result.assistant_message, original.assistant_message)
        self.assertEqual(result.proposal_diagnostics, original.proposal_diagnostics)
        self.assertEqual(result.guidance['coordinateLinks'], original.guidance['coordinateLinks'])
        self.assertEqual(result.guidance['proposalDiscovery'], original.guidance['proposalDiscovery'])
        self.assertEqual(result.guidance['disagreement'], {**active, 'displayCard': False})
        self.assertIsNone(result.guidance['proposalOffer'])
        self.assertIsNone(result.guidance['intentHypothesis'])
        self.assertEqual(result.guidance['uiCues'], [])
        self.assertNotIn('followUpQuestion', result.guidance)
        self.assertNotIn('designContextPatch', result.guidance)
        self.assertTrue(active['displayCard'])
        self.assertEqual(self.execution, original)

    def test_conversation_only_accepts_reply_without_optional_metadata(self):
        execution = LLMExecutionResult('A plain reply.', 1, 'offline')
        result = apply_conversation_only_policy(execution, active_disagreement=None)
        self.assertEqual(result.assistant_message, execution.assistant_message)
        self.assertEqual(result.guidance['coordinateLinks'], [])
        self.assertIsNone(result.guidance['proposalDiscovery'])
        self.assertIsNone(result.guidance['disagreement'])


class MessageResponsePolicyHttpTests(SessionTestCase):
    def setUp(self):
        self.open_workspace()
        created = self.client.post('/api/sessions', json={
            'rows': SAMPLE_ROWS,
            'initialDraftMethod': 'partial_completion',
            'language': 'en',
            'idempotencyKey': 'response-policy-session',
        })
        self.assertEqual(created.status_code, 200, created.text)
        session = created.json()
        self.session_url = f"/api/sessions/{session['sessionId']}"
        bootstrap = parse_qs(urlparse(session['launchUrl']).fragment)['bootstrap'][0]
        access = self.client.post(f'{self.session_url}/browser-access', json={'bootstrapToken': bootstrap})
        self.assertEqual(access.status_code, 200, access.text)
        started = self.client.patch(f'{self.session_url}/language', json={'language': 'en'})
        self.assertEqual(started.status_code, 200, started.text)
        self.version_id = started.json()['currentVersionId']

    def test_map_conflict_correction_is_committed_once_without_offer(self):
        content = 'B1 is at (4,4).'
        text = 'I would focus on room for the first push.'
        generate = RecordedCall(return_value=LLMExecutionResult(text, 1, 'offline', guidance={
            'proposalOffer': {'summary': 'Move an internal wall'},
            'intentHypothesis': 'You prefer a wider passage.',
            'followUpQuestion': 'Should I change it?',
            'uiCues': [{'type': 'warning', 'text': 'Unverified warning'}],
        }))
        payload = {'content': content, 'baseVersionId': self.version_id, 'idempotencyKey': 'conflicting-claim'}
        with self.using_dependencies(llm_handlers={
            'classify_turn_understanding': {'acts': ['evaluation'], 'evidenceSpan': content, 'mapRelated': True},
            'generate_chat_reply': generate,
        }):
            response = self.client.post(f'{self.session_url}/messages', json=payload)
            replay = self.client.post(f'{self.session_url}/messages', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        generate.assert_called_once()
        session = response.json()
        self.assertEqual(session['turns'], replay.json()['turns'])
        self.assertEqual([turn['role'] for turn in session['turns']], ['user', 'assistant'])
        self.assertEqual(session['currentVersionId'], self.version_id)
        self.assertEqual(session['proposals'], [])
        assistant = session['turns'][-1]
        self.assertTrue(assistant['content'].startswith('The position you gave for B1 does not match the current Stage.'))
        self.assertIn('row 6, column 5', assistant['content'])
        self.assertTrue(assistant['content'].endswith(f'\n\n{text}'))
        self.assertIsNone(assistant['guidance']['proposalOffer'])
        self.assertIsNone(assistant['guidance']['intentHypothesis'])
        self.assertEqual(assistant['guidance']['uiCues'], [])
        with self.services.connect() as database:
            count = database.execute("SELECT count(*) FROM audit_events WHERE event_type = 'map_claim_conflict'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_conversation_only_keeps_grounded_route_without_actions(self):
        content = 'How would you approach this level?'
        text = 'I can walk from (5,5) to (5,7) before the first push.'
        link = {'text': text, 'from': {'row': 5, 'column': 5}, 'to': {'row': 5, 'column': 7}}
        advisory_reply = LLMExecutionResult(text, 1, 'offline', guidance={
            'coordinateLinks': [link],
            'proposalOffer': {'summary': 'Move an internal wall'},
            'followUpQuestion': 'Should I change it?',
            'disagreement': {'subject': 'human_edit', 'status': 'active', 'displayCard': True},
        })
        with self.using_dependencies(llm_handlers={
            'classify_turn_understanding': {
                'acts': ['unclear'], 'evidenceSpan': content, 'mapRelated': True, 'conversationOnly': True,
            },
            'generate_chat_reply': advisory_reply,
        }):
            response = self.client.post(f'{self.session_url}/messages', json={
                'content': content, 'baseVersionId': self.version_id, 'idempotencyKey': 'conversation-only',
            })
        self.assertEqual(response.status_code, 200, response.text)
        assistant = response.json()['turns'][-1]
        self.assertEqual(assistant['content'], text)
        self.assertEqual(assistant['guidance']['coordinateLinks'], [link])
        self.assertIsNone(assistant['guidance']['proposalOffer'])
        self.assertIsNone(assistant['guidance']['disagreement'])
        self.assertNotIn('followUpQuestion', assistant['guidance'])
        self.assertNotIn('discussionCardMode', assistant['guidance'])
        with self.services.connect() as database:
            stored = database.execute('SELECT guidance_json FROM conversation_turns WHERE id = ?', (assistant['turnId'],)).fetchone()
        self.assertEqual(load_json(stored['guidance_json'])['coordinateLinks'], [link])
