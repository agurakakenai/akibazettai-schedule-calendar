"""Synthetic model-contract regressions. Every HTTP call is intercepted."""
import contextlib
import copy
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import urllib.error

import test_collect_personal_shifts as base


personal, azure = base.personal, base.personal.azure
ENV = {'AZURE_OPENAI_ENDPOINT': 'https://offline.openai.azure.com/',
       'AZURE_OPENAI_DEPLOYMENT': 'gpt-5.4-nano',
       'AZURE_OPENAI_API_KEY': 'OFFLINE_AZURE_SENTINEL'}


def event(shift, kind, line_ids, store=None, time=None):
    return {'shift': shift, 'kind': kind, 'evidenceLineIds': line_ids, 'storeId': store, 'time': time}


def result(*events, decision=None):
    return {'date': base.DATE.isoformat(), 'decision': decision or ('events' if events else 'no_event'),
            'events': list(events)}


def response(value=None, *, content=None, refusal=None, finish='stop'):
    envelope = {'choices': [{'finish_reason': finish, 'message': {
        'content': json.dumps(value) if content is None else content, 'refusal': refusal}}]}
    reply = mock.MagicMock()
    reply.getcode.return_value = 200
    reply.read.return_value = json.dumps(envelope).encode()
    reply.__enter__.return_value = reply
    return reply


class AzureTests(base.Offline):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory(dir=base.TOOLS / 'tests', prefix='personal-test-')
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.snapshot = self.folder / 'private.json'
        self.http = self.folder / 'observed-shifts.http-state.json'
        self.state = personal.empty_state()
        self.targets = {'あむ': copy.deepcopy(base.AMU)}
        self.clock = base.NOW
        self.opener = mock.Mock()
        self.sleeps = []
        self.analyzer = self.make_analyzer()

    def save(self):
        personal.official.atomic_json(self.snapshot, self.state)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += dt.timedelta(seconds=seconds)

    def make_analyzer(self, environment=None):
        return azure.AzureAnalyzer(self.state, self.save, personal.azure_context(),
                                   ENV if environment is None else environment,
                                   clock=lambda: self.clock, sleep=self.sleep, opener=self.opener)

    def parse(self, text, value, *, analyzer=None, target=base.AMU, tid=base.TID, created=base.CREATED):
        self.opener.open.return_value = response(value)
        return personal.validate_post(base.candidate(tid, created, target),
                                      base.post(text, tid, created, target), target, self.clock,
                                      roster=('あむ', 'ららこ'), analyzer=analyzer or self.analyzer)

    def validate(self, text, value, shifts=('昼', '夜')):
        return azure.grounded_events(value, text, base.DATE, shifts, personal.azure_context())

    def collect(self, *, payloads=None, entries=None, analyzer=None):
        durable = personal.DurableHttp(self.state, self.snapshot, self.http, base.DATE, self.targets,
                                       3, 3, clock=lambda: self.clock, sleep=self.sleep)
        client = mock.Mock()

        def search(*args):
            durable.reserve(personal.SEARCH_HOST, 'searches')
            return [base.candidate()] if entries is None else entries

        def fetch(tid):
            durable.reserve(personal.POST_HOST, 'posts')
            return base.post()

        client.search.side_effect = search
        client.fetch_post.side_effect = fetch
        report, code = personal.collect(
            self.state, durable, client, self.targets, base.DATE, 3, 3,
            clock=lambda: self.clock, roster=('あむ', 'ららこ'),
            analyzer=analyzer or self.analyzer, saved_payloads=payloads)
        personal.read_state(self.snapshot)
        return report, code, client

    def known(self, tid=base.TID, created=base.CREATED):
        self.state['pending'].append({
            **base.candidate(tid, created), 'reason': 'discovered', 'firstSeenAt': created,
            'lastAttemptAt': None, 'attempts': 0})

    def test_whole_body_and_minimal_context_are_sent_once_even_after_a_rule_hit(self):
        text = '今日 昼1号店、夜2。新刊を買うかも'
        self.assertEqual(len(personal.parse_events(
            text, personal.official.timestamp(base.CREATED), base.DATE, base.AMU['shifts'])[0]), 1)
        post, _ = self.parse(text, result(event('昼', 'placement', [1], 's1'),
                                          event('夜', 'placement', [1], 's2')))
        self.assertEqual([e['storeId'] for e in post['events']], ['s1', 's2'])
        self.opener.open.assert_called_once()
        sent = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(json.loads(sent['messages'][1]['content']), {
            'bodyLines': [{'id': 1, 'text': text}], 'postedAt': base.CREATED, 'date': '2026-09-06',
            'author': 'あむ', 'allowedShifts': ['昼', '夜']})
        self.assertEqual(sent['reasoning_effort'], 'none')
        self.assertEqual(sent['max_completion_tokens'], 1200)
        self.assertNotIn('tools', sent)
        self.assertNotIn(ENV['AZURE_OPENAI_API_KEY'], json.dumps(sent))

    def test_line_ids_preserve_endings_blank_lines_and_unicode_without_offsets(self):
        text = '🍓\r\n\r\n　♡ ꒱\n最後\r'
        lines = azure.source_lines(text)
        self.assertEqual([line['text'] for line in lines], ['🍓\r\n', '\r\n', '　♡ ꒱\n', '最後\r', ''])
        self.assertEqual([line['id'] for line in lines], [1, 2, 3, 4, 5])
        self.assertEqual(''.join(line['text'] for line in lines), text)
        for line in lines:
            self.assertEqual(text[line['start']:line['end']], line['text'])
        self.assertEqual(azure.source_lines('')[0]['text'], '')
        self.parse(text, result())
        sent = json.loads(self.opener.open.call_args.args[0].data)
        body_lines = json.loads(sent['messages'][1]['content'])['bodyLines']
        self.assertTrue(all(set(line) == {'id', 'text'} for line in body_lines))
        self.assertEqual(''.join(line['text'] for line in body_lines), text)
        schema = sent['response_format']['json_schema']['schema']
        ids = schema['properties']['events']['items']['properties']['evidenceLineIds']
        self.assertEqual(ids['items']['enum'], [1, 2, 3, 4, 5])
        self.assertEqual(ids['maxItems'], 5)
        self.assertNotIn('enum', azure.SCHEMA['properties']['events']['items']['properties']['evidenceLineIds']['items'])

    def test_body_wording_reaches_model_without_a_semantic_prefilter(self):
        for text in ('明日 夜2号店', '今日「夜2号店」', '今日 ららこは夜2号店',
                     '今日 夜2号店かも', '今日 昼お休みの予定でしたが撤回します'):
            with self.subTest(text=text):
                self.state = personal.empty_state()
                analyzer = self.make_analyzer()
                self.parse(text, result(), analyzer=analyzer)
                self.assertEqual(analyzer.used, 1)
        self.assertEqual(self.opener.open.call_count, 5)

    def test_mechanical_validation_does_not_claim_to_prove_japanese_meaning(self):
        for text in ('今日 夜2号店かも', '今日 夜2号店には出ません',
                     '明日 夜2号店', '今日「夜2号店」', '今日 夜2号店、訂正、夜3号店'):
            with self.subTest(text=text):
                events, _ = self.validate(text, result(event('夜', 'placement', [1], 's2')))
                self.assertEqual(events[0]['storeId'], 's2')
        # These deliberately wrong model decisions require model evaluation,
        # not another Japanese interpreter hidden in the mechanical validator.

    def test_shared_multishift_evidence_is_converted_once_per_event(self):
        text = '今日🍓\r\n　♡ 3号店ひる→2号店よる ꒱\r\n'
        selected = azure.selected_lines(azure.source_lines(text), [3, 2, 1])
        self.assertEqual([line['text'] for line in selected], ['今日🍓\r\n', '　♡ 3号店ひる→2号店よる ꒱\r\n', ''])
        post, _ = self.parse(text, result(
            event('昼', 'placement', [1, 2, 3], 's3'), event('夜', 'placement', [2], 's2')))
        self.assertEqual(post['events'], [
            {'shift': '昼', 'kind': 'placement', 'storeId': 's3', 'excerpt': '3号店'},
            {'shift': '夜', 'kind': 'placement', 'storeId': 's2', 'excerpt': '2号店'}])
        post, _ = self.parse('今日 昼4/夜2', result(
            event('昼', 'placement', [1], 's4'), event('夜', 'placement', [1], 's2')))
        self.assertEqual([e['storeId'] for e in post['events']], ['s4', 's2'])

    def test_noncontiguous_lines_never_form_an_invented_number_or_quote(self):
        text = '今日\n1\n余談\n号店\n19:\n余談\n30に遅れます'
        selected = azure.selected_lines(azure.source_lines(text), [2, 4, 5, 7])
        self.assertEqual([line['text'] for line in selected], ['1\n', '号店\n', '19:\n', '30に遅れます'])
        for proposed in (event('夜', 'placement', [2, 4], 's1'),
                         event('夜', 'late', [5, 7], time='19:30')):
            with self.subTest(proposed=proposed), self.assertRaises(azure.AnalysisFailure):
                self.validate(text, result(proposed))
        events, _ = self.validate('今日\n3号店\n余談🍓\n夜19時に遅れます', result(
            event('夜', 'late', [2, 4], 's3', '19:00')))
        self.assertEqual(events[0]['storeId'], 's3')
        self.assertEqual(events[0]['time'], '19:00')

    def test_bad_line_id_types_duplicates_and_limits_are_rejected(self):
        text = '今日\n2号店\n夜\n'
        for ids in (None, '2', 2, [], [0], [-1], [5], [True], [2.0], ['2'], [[2]], [2, 2],
                    list(range(1, azure.MAX_EVIDENCE_LINES + 2))):
            with self.subTest(ids=ids), self.assertRaises(azure.AnalysisFailure):
                self.validate(text, result(event('夜', 'placement', ids, 's2')))
        with self.assertRaises(azure.AnalysisFailure):
            self.validate(text, result(event('夜', 'absence', [4])))
        boundary = '2号店\n' + '\n' * (azure.MAX_SOURCE_LINES - 2)
        self.assertEqual(len(azure.source_lines(boundary)), azure.MAX_SOURCE_LINES)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_input_limit'):
            self.parse(boundary + '\n', result())
        self.opener.open.assert_not_called()

    def test_numeric_contradictions_and_cropped_numbers_are_rejected(self):
        cases = [
            ('今日 昼4号店', event('昼', 'placement', [1], 's1')),
            ('今日 夜2号店', event('夜', 'placement', [1], 's3')),
            ('今日\n2号店\n夜は遅れます', event('夜', 'late', [3], 's2')),
            ('今日 夜19時に遅れます', event('夜', 'late', [1], time='20:00')),
            ('今日 41号店', event('夜', 'placement', [1], 's1')),
            ('今日 夜41号店', event('夜', 'placement', [1], 's1')),
            ('今日 夜2時', event('夜', 'placement', [1], 's2')),
            ('今日 夜19:30に遅れます', event('夜', 'late', [1], time='09:30')),
            ('今日 夜19時30分に遅れます', event('夜', 'late', [1], time='19:00')),
            ('今日 夜19時半に遅れます', event('夜', 'late', [1], time='19:00')),
        ]
        for text, proposed in cases:
            with self.subTest(text=text), self.assertRaises(azure.AnalysisFailure):
                self.validate(text, result(proposed))
        post, _ = self.parse('今日 夜４号店に１９時３０分から遅れて行きます', result(
            event('夜', 'late', [1], 's4', '19:30')))
        self.assertEqual(post['events'][0], {
            'shift': '夜', 'kind': 'late', 'storeId': 's4', 'time': '19:30', 'excerpt': '４号店'})
        for evidence in ('夜　２', '夜\u00a0２', '夜\t２'):
            with self.subTest(evidence=evidence):
                events, _ = self.validate('今日 ' + evidence, result(event('夜', 'placement', [1], 's2')))
                self.assertEqual(events[0]['storeId'], 's2')
                self.assertEqual(events[0]['excerpt'], evidence.replace('\t', ' '))

    def test_schema_target_boundaries_and_duplicate_events_fail_closed(self):
        text = '今日 昼1号店、夜2号店'
        valid = event('夜', 'placement', [1], 's2')
        cases = [result(valid, valid), result(valid, valid, valid),
                 {'date': '2026-09-07', 'decision': 'events', 'events': [valid]},
                 {'date': base.DATE.isoformat(), 'decision': 'no_event', 'events': [valid]},
                 {**result(valid), 'author': 'someone'},
                 result({**valid, 'confidence': 1}),
                 result({**valid, 'shift': '朝'}), result({**valid, 'shift': []}),
                 result({**valid, 'storeId': 's5'}), result({**valid, 'storeId': 2}),
                 result({**valid, 'kind': 'working'}), result({**valid, 'evidenceLineIds': None}),
                 result({'shift': '夜', 'kind': 'placement', 'storeId': 's2', 'time': None,
                         'evidence': '夜2号店'}),
                 result({**valid, 'storeId': None}),
                 result({**valid, 'time': '19:00'}),
                 result(event('夜', 'absence', [1], 's2')),
                 result(event('夜', 'return', [1], 's2', False))]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(azure.AnalysisFailure):
                self.validate(text, value)
        with self.assertRaises(azure.AnalysisFailure):
            self.validate(text, result(valid), shifts=('昼',))

    def test_short_public_anchors_do_not_persist_full_evidence_or_health_prose(self):
        text = '今日は体調の事情で、今日は終日お休みします'
        post, _ = self.parse(text, result(event('夜', 'absence', [1])),
                            target={**base.AMU, 'shifts': ['夜']})
        self.assertEqual(post['events'], [{'shift': '夜', 'kind': 'absence', 'excerpt': 'お休み'}])
        self.assertNotIn('体調', self.snapshot.read_text(encoding='utf-8'))
        evidence = '2号店\n15時〜21時\n早めの夜担当です'
        post, _ = self.parse('今日\n' + evidence, result(event('夜', 'placement', [2, 3, 4], 's2')))
        self.assertEqual(post['events'], [{'shift': '夜', 'kind': 'placement',
                                          'storeId': 's2', 'excerpt': '2号店'}])
        self.assertNotIn('evidence', self.snapshot.read_text(encoding='utf-8'))

    def test_model_pending_refusal_and_metadata_are_distinct(self):
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_pending'):
            self.parse('今日 夜2号店かも', result(decision='pending'))
        self.opener.open.assert_called_once()
        for field in ('quoted_tweet', 'retweeted_status', 'in_reply_to_status_id_str'):
            payload = base.post()
            payload[field] = 'present'
            self.assertEqual(personal.validate_post(base.candidate(), payload, base.AMU, self.clock,
                                                    analyzer=self.analyzer), (None, 'quoted_or_reply'))
        for key, value in (('id_str', base.UID), ('user', {'id_str': base.UID, 'screen_name': 'other'}),
                           ('created_at', '2026-09-07T15:00:02Z')):
            payload = base.post()
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(personal.Failure):
                personal.validate_post(base.candidate(), payload, base.AMU, self.clock, analyzer=self.analyzer)
        self.opener.open.assert_called_once()

    def test_same_body_cache_edit_and_old_version_failure_are_separate(self):
        text = '今日 昼1号店'
        expected = result(event('昼', 'placement', [1], 's1'))
        with mock.patch.object(azure, 'VERSION', 'personal-nano-v3-gpt-5.4-nano-2026-03-17'):
            old = self.make_analyzer()
        self.opener.open.side_effect = TimeoutError()
        with self.assertRaises(azure.AnalysisFailure):
            self.parse(text, expected, analyzer=old)
        old_cache = copy.deepcopy(old.state['cache'])
        self.clock += dt.timedelta(minutes=1)
        self.state = personal.read_state(self.snapshot)
        current = self.make_analyzer()
        self.opener.open.side_effect = None
        for _ in range(2):
            self.parse(text, expected, analyzer=current)
        self.assertEqual(self.opener.open.call_count, 2)
        self.parse('今日 昼2号店', result(event('昼', 'placement', [1], 's2')), analyzer=current)
        self.assertEqual(self.opener.open.call_count, 3)
        for key, entry in old_cache.items():
            self.assertEqual(current.state['cache'][key], entry)

    def test_invalid_responses_are_negative_cached_without_raw_errors(self):
        cases = [response(content='{'), response(result(), refusal='refused'),
                 response(result(), finish='length'),
                 response(content='{"date":"2026-09-06","date":"2026-09-06","decision":"no_event","events":[]}'),
                 response({'decision': 'events', 'date': 'wrong', 'events': []}),
                 response(result(event('昼', 'placement', [999], 's1')))]
        for reply in cases:
            with self.subTest(reply=reply):
                self.state = personal.empty_state()
                analyzer = self.make_analyzer()
                self.opener.open.return_value = reply
                for _ in range(2):
                    with self.assertRaises(azure.AnalysisFailure):
                        personal.validate_post(base.candidate(), base.post(), base.AMU,
                                               self.clock, analyzer=analyzer)
                self.assertEqual(analyzer.used, 1)
                self.assertNotIn(ENV['AZURE_OPENAI_API_KEY'], self.snapshot.read_text(encoding='utf-8'))

    def test_timeout_preserves_facts_and_pending_without_source_refetch(self):
        self.state['posts'] = [personal.validate_post(base.candidate(), base.post(), base.AMU, self.clock)[0]]
        self.state['identityBindings']['あむ'] = {
            'authorId': base.UID, 'authorScreenName': 'amu_zettai', 'verifiedAt': base.CREATED}
        before = copy.deepcopy(self.state['posts'])
        self.opener.open.side_effect = TimeoutError('sensitive error must not be saved')
        _, code, client = self.collect(payloads={base.TID: base.post('今日 昼2号店')})
        self.assertEqual(code, 3)
        self.assertEqual(self.state['posts'], before)
        self.assertEqual(self.state['pending'][0]['reason'], 'azure_timeout')
        client.search.assert_not_called()
        client.fetch_post.assert_not_called()
        self.analyzer = self.make_analyzer()
        _, _, client = self.collect(entries=[])
        client.fetch_post.assert_not_called()
        self.assertEqual(len(self.state['pending']), 1)
        self.opener.open.assert_called_once()

    def test_429_and_auth_stops_do_not_affect_source_budgets(self):
        for status in (429, 401, 403):
            with self.subTest(status=status):
                self.state = personal.empty_state()
                self.analyzer = self.make_analyzer()
                self.opener.open.side_effect = urllib.error.HTTPError(
                    'https://offline.openai.azure.com/', status, 'secret', {'Retry-After': '900'}, None)
                with self.assertRaises(azure.AnalysisFailure):
                    self.parse('今日 昼1号店', result())
                self.assertIsNone(self.state['paused'])
                self.assertEqual(self.state['lastRequests'], {})
                self.assertEqual(self.state['budgets'], {})
                count = self.opener.open.call_count
                with self.assertRaises(azure.AnalysisFailure):
                    self.parse('今日 昼2号店', result(), analyzer=self.make_analyzer())
                self.assertEqual(self.opener.open.call_count, count)
                if status == 429:
                    self.assertEqual(personal.official.timestamp(self.analyzer.state['nextRequestAt']),
                                     self.clock + dt.timedelta(seconds=900))
                else:
                    self.assertEqual(self.analyzer.state['paused']['httpStatus'], status)

    def test_request_caps_input_limit_and_durable_reservation(self):
        self.opener.open.side_effect = lambda *a, **k: (
            self.assertEqual(personal.read_state(self.snapshot)['azureAnalysis']['budgets']['2026-09-06'],
                             self.analyzer.used) or response(result()))
        for index in range(3):
            self.parse('今日 案内なし' + str(index), result())
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_budget_exhausted'):
            self.parse('今日 案内なし4', result())
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertGreaterEqual(sum(self.sleeps), 120)
        self.state['azureAnalysis']['budgets']['2026-09-06'] = 30
        self.clock += dt.timedelta(minutes=1)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_budget_exhausted'):
            self.parse('今日 案内なし5', result(), analyzer=self.make_analyzer())
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_input_limit'):
            self.parse('今日' + 'a' * 6000, result())

    def test_saved_edit_preserves_original_chronology_before_later_absence(self):
        self.known()
        self.opener.open.return_value = response(result(event('昼', 'placement', [1], 's1')))
        self.collect(payloads={base.TID: base.post('今日 昼1号店')})
        original = copy.deepcopy(self.state['posts'][0])
        later_time = '2026-09-06T02:00:00Z'
        later_id = base.snowflake(later_time)
        self.known(later_id, later_time)
        self.opener.open.return_value = response(result(event('昼', 'absence', [1])))
        self.collect(payloads={later_id: base.post('今日 昼お休みします', later_id, later_time)})
        self.opener.open.return_value = response(result(event('昼', 'placement', [1], 's2')))
        self.collect(payloads={base.TID: base.post('今日 昼2号店')})
        self.assertEqual([p['id'] for p in self.state['posts']], [base.TID, later_id])
        self.assertEqual(self.state['posts'][0]['createdAt'], original['createdAt'])
        self.assertEqual(self.state['posts'][-1]['events'][0]['kind'], 'absence')
        self.assertEqual(self.state['azureAnalysis']['history'], [original])
        self.assertEqual(self.state['budgets'], {})
        personal.read_state(self.snapshot)

    def test_legacy_no_event_only_marks_explicit_saved_body_review(self):
        self.state['resolved'] = [{'id': base.TID, 'name': 'あむ', 'url': base.candidate()['url'],
                                  'date': '2026-09-06', 'resolvedAt': base.CREATED, 'reason': 'no_event'}]
        analyzer = self.make_analyzer()
        _, _, client = self.collect(analyzer=analyzer)
        client.fetch_post.assert_not_called()
        self.opener.open.assert_not_called()
        self.assertEqual(analyzer.state['review'], {base.TID: 'azure_saved_body_required'})
        self.assertEqual(self.state['resolved'][0]['reason'], 'no_event')
        with self.assertRaisesRegex(ValueError, 'saved_post_identity_required'):
            personal.saved_candidates(self.state, {base.TID: base.post()}, base.DATE)

    def test_bad_configuration_never_falls_back_or_persists_credentials(self):
        for env in ({}, {**ENV, 'AZURE_OPENAI_DEPLOYMENT': 'expensive-model'},
                    {**ENV, 'AZURE_OPENAI_ENDPOINT': 'https://example.com/'}):
            with self.assertRaisesRegex(ValueError, 'invalid_azure_configuration'):
                self.make_analyzer(env)
        self.parse('今日 昼1号店', result(event('昼', 'placement', [1], 's1')))
        self.assertNotIn('azureAnalysis', personal.public_state(self.state))
        self.assertNotIn(ENV['AZURE_OPENAI_API_KEY'], self.snapshot.read_text(encoding='utf-8'))
        self.assertNotIn('offline.openai.azure.com', self.snapshot.read_text(encoding='utf-8'))

    def test_saved_cli_uses_no_source_client_and_validates_identity(self):
        self.known()
        self.save()
        seed, schedule, insights, accounts, saved = [
            self.folder / name for name in ('seed.json', 'schedule.js', 'insights.js', 'accounts.csv', 'saved.json')]
        personal.official.atomic_json(seed, personal.empty_snapshot())
        schedule.write_text('window.SCHEDULE_DATA=' + json.dumps({
            'roster': ['あむ'], 'schedule': {'2026-09-06': {
                '昼': [{'name': 'あむ'}], '夜': [{'name': 'あむ'}]}}}) + ';', encoding='utf-8')
        insights.write_text('window.STORE_INSIGHTS=' + json.dumps({
            'maidTendency': {'あむ': {'x': 'amu_zettai'}}}) + ';', encoding='utf-8')
        accounts.write_text('name,handle,source\nあむ,amu_zettai,本人確認済み\n', encoding='utf-8')
        personal.official.atomic_json(saved, {base.TID: base.post('今日 昼1号店')})
        self.opener.open.return_value = response(result(event('昼', 'placement', [1], 's1')))
        args = personal.argument_parser().parse_args([
            '--snapshot', str(self.snapshot), '--http-state', str(self.http),
            '--seed', str(seed), '--schedule', str(schedule), '--insights', str(insights),
            '--accounts', str(accounts), '--analysis-backend', 'azure', '--analyze-saved', str(saved)])
        with contextlib.redirect_stdout(io.StringIO()):
            code = personal.run(args, clock=lambda: self.clock, sleep=self.sleep, environment=ENV,
                                analyzer_factory=lambda *a, **k: azure.AzureAnalyzer(*a, **k, opener=self.opener),
                                client_factory=lambda _: self.fail('no source client allowed'))
        self.assertEqual(code, 0)
        self.state = personal.read_state(self.snapshot)
        self.assertEqual(self.state['budgets']['2026-09-06'], {'searches': 7, 'posts': 2})
        original = copy.deepcopy(self.state['posts'])
        wrong = base.post('今日 昼2号店')
        wrong['user']['screen_name'] = 'other'
        self.analyzer = self.make_analyzer()
        self.collect(payloads={base.TID: wrong})
        self.assertEqual(self.state['posts'], original)
        self.assertEqual(self.state['pending'][0]['reason'], 'azure_saved_author_mismatch')
        personal.merge_seed(self.state, personal.public_state(self.state))
        self.assertEqual(len(self.state['pending']), 1)
        self.collect(entries=[])
        self.opener.open.assert_called_once()

    def test_no_event_pending_and_budget_do_not_delete_existing_facts(self):
        self.known()
        self.opener.open.return_value = response(result(event('昼', 'placement', [1], 's1')))
        self.collect(payloads={base.TID: base.post('今日 昼1号店')})
        original = copy.deepcopy(self.state['posts'])
        resolved = copy.deepcopy(self.state['resolved'])
        for text, value, reason in (
                ('今日 昼2号店', result(decision='pending'), 'azure_pending'),
                ('今日 晴れ', result(), 'azure_no_event_conflict')):
            self.opener.open.return_value = response(value)
            self.collect(payloads={base.TID: base.post(text)})
            self.assertEqual(self.state['posts'], original)
            self.assertEqual(self.state['resolved'], resolved)
            self.assertEqual(self.state['pending'][0]['reason'], reason)
        self.analyzer.state['budgets']['2026-09-06'] = 30
        self.collect(payloads={base.TID: base.post('今日 昼3号店')})
        self.assertEqual(self.state['pending'][0]['reason'], 'azure_budget_exhausted')
        self.assertEqual(self.state['posts'], original)

    def test_legacy_removed_fact_is_not_resurrected_from_seed(self):
        old = personal.validate_post(base.candidate(), base.post(), base.AMU, self.clock)[0]
        self.state['azureAnalysis']['history'] = [copy.deepcopy(old)]
        self.state['resolved'] = [{'id': base.TID, 'url': old['url'], 'name': old['name'],
                                  'date': old['date'], 'reason': 'no_event', 'resolvedAt': base.CREATED}]
        seed = {**personal.empty_snapshot(), 'posts': [old]}
        personal.merge_seed(self.state, seed)
        self.assertEqual(self.state['posts'], [])

    def test_line_id_response_reaches_existing_history_and_ui_resolver(self):
        executable = shutil.which('node') or str(personal.NODE_FALLBACK)
        if not Path(executable).is_file():
            self.skipTest('Existing Node runtime is unavailable')
        self.known()
        text = '今日🍓\r\n　♡ 3号店ひる→2号店よる ꒱\r\n'
        self.opener.open.return_value = response(result(
            event('昼', 'placement', [1, 2, 3], 's3'), event('夜', 'placement', [2], 's2')))
        _, code, client = self.collect(payloads={base.TID: base.post(text)})
        self.assertEqual(code, 0)
        client.fetch_post.assert_not_called()
        public = personal.public_state(self.state)
        encoded = json.dumps(public, ensure_ascii=False)
        for private in ('🍓', '꒱', 'evidenceLineIds', 'bodyLines'):
            self.assertNotIn(private, encoded)
        script = """
const api=require('./app.js'),fs=require('node:fs');
const snapshot=JSON.parse(fs.readFileSync(0,'utf8'));
api.validatePersonalShifts(snapshot);
const person=api.personalShift(snapshot,{},'2026-09-06','夜',{byMaid:new Map()}).byMaid.get('あむ');
process.stdout.write(JSON.stringify({store:person.storeId,history:person.history.map(x=>x.post.id)}));
"""
        process = subprocess.run(
            [executable, '-e', script], cwd=personal.ROOT, input=encoded,
            capture_output=True, encoding='utf-8', timeout=10, check=True,
            env={key: value for key, value in os.environ.items() if not key.startswith('AZURE_OPENAI_')},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(json.loads(process.stdout), {'store': 's2', 'history': [base.TID]})


if __name__ == '__main__':
    unittest.main()
