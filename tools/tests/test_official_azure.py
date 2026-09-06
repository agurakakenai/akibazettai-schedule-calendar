"""Offline producer/transport/ledger tests for supplemental official notices."""
import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock
import urllib.error
import uuid


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('official_test_collector', TOOLS / 'collect-shifts.py')
official = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(official)
azure = official.analysis_module()
DAY = dt.date(2026, 9, 5)
CREATED = '2026-09-05T09:17:05Z'
TID = '2096165714604486679'
NOW = official.timestamp('2026-09-05T12:00:00.290Z')
ENV = {'AZURE_OPENAI_ENDPOINT': 'https://offline-test.openai.azure.com',
       'AZURE_OPENAI_API_KEY': 'offline-secret'}
NAMES = ('あむ', 'こい', 'みりあ', 'まこと')


def payload(text=None, tid=TID, created=CREATED):
    return {'id_str': tid, 'created_at': created,
            'user': {'id_str': official.AUTHOR_ID, 'screen_name': official.AUTHOR},
            'text': text if text is not None else (
                '【アキバ絶対領域】\r\nよるにゃんこ\r\n\r\nあむ\r\nこい\r\n'
                '⊂(´ω´⊂)))\r\n\r\nみりあちゃんもあとから来るにゃんね~⊂(´ω´⊂)))\r\n')}


def decision(name='みりあ', when=None, ids=(1, 2, 8), kind='notices'):
    return {'decision': kind, 'date': DAY.isoformat(), 'notices': [] if kind != 'notices' else [
        {'name': name, 'kind': 'late', 'time': when, 'evidenceLineIds': list(ids)}]}


class Response:
    headers = {}

    def __init__(self, body):
        self.body = json.dumps(body, ensure_ascii=False).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def getcode(self):
        return 200

    def read(self, limit):
        return self.body[:limit]


class Opener:
    def __init__(self, result, model=azure.transport.MODEL_NAME):
        self.result, self.model, self.requests = result, model, []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        if isinstance(self.result, Response):
            return self.result
        return Response({'model': self.model, 'choices': [{
            'finish_reason': 'stop', 'message': {'content': json.dumps(self.result)}}]})


class Source:
    def __init__(self, posts=None, ids=None):
        self.posts = {TID: payload()} if posts is None else posts
        self.ids = list(self.posts) if ids is None else ids
        self.calls, self.searches = [], []

    def begin_run(self):
        pass

    def search(self, url):
        self.searches.append(url)
        return self.ids

    def fetch_post(self, tid):
        self.calls.append(tid)
        value = self.posts[tid]
        if isinstance(value, Exception):
            raise value
        return value


class OfficialAzureTests(unittest.TestCase):
    def setUp(self):
        self.directory = TOOLS / 'tests' / ('official-azure-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = self.directory / 'usage.json'
        azure.ledger.atomic_json(self.path, azure.ledger.empty_state())
        self.now = NOW

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += dt.timedelta(seconds=seconds)

    def usage(self, run='offline', limit=3):
        return azure.ledger.SharedUsage(self.path, run_id=run, component='official',
                                        clock=self.clock, sleep=self.sleep, request_limit=limit)

    def analyzer(self, state, usage, result=None, **kwargs):
        opener = Opener(decision() if result is None else result)
        analyzer = azure.AzureAnalyzer(state, official.analysis_context(), ENV, usage,
                                       clock=self.clock, names=NAMES, opener=opener, **kwargs)
        return analyzer, opener

    def collect(self, state, analyzer, source=None, **kwargs):
        return official.collect(state, set(), source or Source(), DAY, DAY, 20,
                                clock=self.clock, analyzer=analyzer, **kwargs)

    def old_state(self):
        post = official.validate_post(TID, payload(), DAY, DAY, NOW - dt.timedelta(hours=1))
        state = official.empty_snapshot()
        state['posts'].append(post)
        return state

    def source_posts(self, count, start=CREATED):
        result = {}
        for index in range(count):
            created = official.timestamp(start) + dt.timedelta(seconds=index * 3)
            tid = str((int(created.timestamp() * 1000) - 1288834974657) << 22)
            result[tid] = payload(tid=tid, created=official.iso(created))
        return result

    def queued_state(self, sources):
        state = official.empty_snapshot()
        state['officialAnalysis'] = azure.empty_state()
        for tid, value in sources.items():
            post = official.validate_post(tid, value, DAY, DAY, self.now - dt.timedelta(hours=1))
            state['posts'].append(post)
            state['officialAnalysis']['queue'][tid] = {
                'createdAt': post['createdAt'],
                'fetchedAt': official.iso(self.now - dt.timedelta(minutes=30)),
                'bodyHash': azure.digest(value['text']), 'reason': 'azure_budget_exhausted'}
        return state

    def seed_personal_requests(self, run_id, count):
        identity = azure.transport.AzureOpenAI(ENV, on_http_failure=lambda *_: None).identity
        with azure.ledger.SharedUsage(self.path, run_id=run_id, component='personal',
                                      clock=self.clock, sleep=self.sleep) as usage:
            for index in range(count):
                key = azure.digest('personal-' + str(index))
                usage.reserve(key, identity)
                usage.issued(key)
                usage.finish(key, 'no_event')

    def roundtrip(self, state):
        path = self.directory / 'snapshot.json'
        official.atomic_json(path, state)
        return official.load_snapshot(path)

    def test_full_unchanged_body_and_dynamic_enum_through_producer_roundtrip(self):
        state = official.empty_snapshot()
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage)
            updated, report, code = self.collect(state, analyzer)
            self.assertEqual(usage.used, 1)
        post = self.roundtrip(updated)['posts'][0]
        self.assertEqual((code, report['newNameCount']), (0, 2))
        self.assertEqual(post['names'], ['あむ', 'こい'])
        self.assertEqual(post['notices'], [{
            'name': 'みりあ', 'kind': 'late', 'excerpt': 'みりあ',
            'observedAt': NOW.isoformat().replace('+00:00', 'Z')}])
        request = json.loads(opener.requests[0].data)
        self.assertEqual(request['max_completion_tokens'], 1200)
        user = json.loads(request['messages'][1]['content'])
        self.assertEqual(''.join(line['text'] for line in user['bodyLines']), payload()['text'])
        self.assertEqual(user['bodyLines'][-1]['text'], '')
        self.assertTrue(all(set(line) == {'id', 'text'} for line in user['bodyLines']))
        props = request['response_format']['json_schema']['schema']['properties']['notices']['items']['properties']
        self.assertEqual(set(props['name']['enum']), set(NAMES))
        self.assertEqual(props['evidenceLineIds']['items']['enum'], list(range(1, 10)))
        public = json.dumps(post, ensure_ascii=False)
        for forbidden in ('bodyLines', 'evidenceLineIds', 'offline-secret', '来るにゃんね'):
            self.assertNotIn(forbidden, public)

    def test_service_boundary_metadata_is_authoritative_within_two_seconds(self):
        created = '2026-09-04T20:00:00Z'
        moment = official.timestamp('2026-09-04T19:59:59Z')
        milliseconds = int(moment.timestamp() * 1000)
        tid = str((milliseconds - 1288834974657) << 22)
        data = payload(tid=tid, created=created)
        state = official.empty_snapshot()
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage)
            updated, _, code = self.collect(state, analyzer, Source({tid: data}))
        self.assertEqual(code, 0)
        self.assertEqual(updated['posts'][0]['date'], DAY.isoformat())
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_metadata_never_reaches_model(self):
        mutations = [
            lambda data: data['user'].update(id_str='111111111111111111'),
            lambda data: data['user'].update(screen_name='other'),
            lambda data: data.update(id_str='111111111111111111'),
            lambda data: data.update(created_at='2026-09-05T09:17:08Z'),
            lambda data: data.update(created_at='2026-09-05T09:17:05'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                state, data = official.empty_snapshot(), payload()
                mutate(data)
                with self.usage() as usage:
                    analyzer, opener = self.analyzer(state, usage)
                    updated, _, code = self.collect(state, analyzer, Source({TID: data}))
                    self.assertEqual(usage.used, 0)
                self.assertEqual(code, 2)
                self.assertFalse(updated['posts'])
                self.assertFalse(opener.requests)

    def test_arbitrary_late_name_and_explicit_time_are_not_hardcoded(self):
        data = payload('【アキバ絶対領域】\nよるにゃんこ\n\nあむ\n⊂(´ω´⊂)))\n'
                       'まことは19時30分から遅れて合流します\n')
        state = official.empty_snapshot()
        with self.usage() as usage:
            analyzer, _ = self.analyzer(state, usage, decision('まこと', '19:30', (1, 2, 6)))
            updated, _, code = self.collect(state, analyzer, Source({TID: data}))
        self.assertEqual(code, 0)
        self.assertEqual(updated['posts'][0]['notices'][0]['time'], '19:30')
        self.assertEqual(updated['posts'][0]['names'], ['あむ'])

    def test_mechanical_grounding_rejects_bad_name_line_ids_time_and_extra_fields(self):
        bad = []
        for field, value in [('name', '知らない人'), ('name', 'こい'), ('time', '19:30'),
                             ('evidenceLineIds', [True]), ('evidenceLineIds', [0]),
                             ('evidenceLineIds', [99]), ('evidenceLineIds', [8, 8]),
                             ('kind', 'placement')]:
            result = decision(ids=(8,))
            result['notices'][0][field] = value
            bad.append(result)
        result = decision()
        result['notices'][0]['confidence'] = 1
        bad.append(result)
        result = decision()
        result['date'] = '2026-09-06'
        bad.append(result)
        post = self.old_state()['posts'][0]
        for result in bad:
            with self.subTest(result=result), self.assertRaises(azure.AnalysisFailure):
                azure.grounded_notices(result, azure.source_lines(payload()['text']), post,
                                       NAMES, official.iso(NOW), official.analysis_context())

    def test_selected_store_alias_and_shift_conflicts_are_not_forced_into_header(self):
        post = self.old_state()['posts'][0]
        for footer in ('みりあは2号店へ遅れて到着', 'みりあはＡ．Ｄ．２０４５にあとから到着',
                       'みりあはひるにゃんこに遅れて到着'):
            data = payload()['text'] + footer
            with self.subTest(footer=footer), self.assertRaisesRegex(azure.AnalysisFailure, 'azure_ungrounded'):
                azure.grounded_notices(decision(ids=(1, 2, 9)), azure.source_lines(data),
                                       post, NAMES, official.iso(NOW), official.analysis_context())

    def test_pending_noevent_and_transport_failures_preserve_old_notice_and_names(self):
        initial = self.old_state()
        initial['posts'][0]['notices'] = [{
            'name': 'みりあ', 'kind': 'late', 'excerpt': 'みりあ',
            'observedAt': official.iso(NOW - dt.timedelta(minutes=30))}]
        for index, response in enumerate((
                decision(kind='pending'), decision(kind='no_event'), TimeoutError(),
                Response({'model': azure.transport.MODEL_NAME, 'choices': [{
                    'finish_reason': 'stop', 'message': {'refusal': 'no'}}]}))):
            azure.ledger.atomic_json(self.path, azure.ledger.empty_state())
            with self.subTest(response=response), self.usage('run-' + str(index)) as usage:
                analyzer, opener = self.analyzer(copy.deepcopy(initial), usage, response)
                updated, report, _ = self.collect(initial, analyzer,
                                                  saved_payloads={TID: payload()},
                                                  source_fetched_at=official.iso(NOW))
                self.assertEqual(updated['posts'], initial['posts'])
                self.assertEqual(report['attemptedCount'], 0)
                self.assertEqual(report['newNameCount'], 0)
                self.assertEqual(len(opener.requests), 1)
                self.roundtrip(updated)

    def test_model_mismatch_negative_cache_does_not_refetch_known_id(self):
        state = official.empty_snapshot()
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage)
            opener.model = 'preview-model'
            updated, _, code = self.collect(state, analyzer)
        self.assertEqual(code, 2)
        self.assertEqual(updated['pending'][0]['reason'], 'azure_model_mismatch')
        self.roundtrip(updated)
        with self.usage('second') as usage:
            analyzer, opener = self.analyzer(updated, usage)
            source = Source()
            again, _, code = self.collect(updated, analyzer, source)
            self.assertEqual(usage.used, 0)
        self.assertEqual(code, 2)
        self.assertFalse(source.calls)
        self.assertFalse(opener.requests)
        self.assertEqual(again['pending'], updated['pending'])

    def test_cached_saved_failure_is_not_issued_again(self):
        state = self.old_state()
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage, TimeoutError())
            updated, _, _ = self.collect(state, analyzer, saved_payloads={TID: payload()},
                                         source_fetched_at=official.iso(NOW))
        with self.usage('second') as usage:
            analyzer, opener = self.analyzer(updated, usage)
            again, _, _ = self.collect(updated, analyzer, saved_payloads={TID: payload()},
                                       source_fetched_at=official.iso(NOW))
            self.assertEqual(usage.used, 0)
        self.assertFalse(opener.requests)
        self.assertEqual(again['posts'], updated['posts'])

    def test_unissued_capacity_queue_is_retried_only_with_next_run_capacity(self):
        state = official.empty_snapshot()
        with self.usage(limit=0) as usage:
            analyzer, opener = self.analyzer(state, usage)
            updated, report, code = self.collect(state, analyzer)
        self.assertEqual(code, 2)
        self.assertFalse(opener.requests)
        self.assertIn(TID, updated['officialAnalysis']['queue'])
        self.assertEqual(report['analysisDeferredCount'], 1)
        self.assertEqual(report['analysisPendingCount'], 1)
        self.assertFalse(updated['officialAnalysis']['cache'])
        self.assertEqual(len(updated['posts']), 1)
        with self.usage(limit=0) as usage:
            analyzer, opener = self.analyzer(updated, usage)
            source = Source()
            blocked, _, _ = self.collect(updated, analyzer, source)
        self.assertFalse(source.calls)
        with self.usage('next') as usage:
            analyzer, opener = self.analyzer(blocked, usage)
            source = Source()
            completed, report, code = self.collect(blocked, analyzer, source)
        self.assertEqual(code, 0)
        self.assertEqual(source.calls, [TID])
        self.assertEqual(report['attemptedCount'], 1)
        self.assertEqual(report['newNameCount'], 0)
        self.assertFalse(completed['officialAnalysis']['queue'])
        self.assertFalse(completed['pending'])
        self.assertTrue(self.roundtrip(completed)['posts'][0]['notices'])

    def test_new_roster_precedes_twenty_known_queue_items_and_rechecks_capacity_before_get(self):
        sources = self.source_posts(21)
        known_ids = list(sources)[:20]
        new_id = list(sources)[20]
        state = official.empty_snapshot()
        state['officialAnalysis'] = azure.empty_state()
        for tid in known_ids:
            post = official.validate_post(tid, sources[tid], DAY, DAY, NOW - dt.timedelta(hours=1))
            state['posts'].append(post)
            state['officialAnalysis']['queue'][tid] = {
                'createdAt': post['createdAt'], 'fetchedAt': official.iso(NOW - dt.timedelta(minutes=30)),
                'bodyHash': azure.digest(sources[tid]['text']), 'reason': 'azure_budget_exhausted'}
        with self.usage(limit=1) as usage:
            analyzer, opener = self.analyzer(state, usage)
            source = Source(sources)
            updated, report, _ = self.collect(state, analyzer, source)
        self.assertEqual(source.calls, [new_id])
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(report['newNameCount'], 2)
        self.assertEqual(report['requests']['posts'], 1)
        self.assertEqual(len(updated['posts']), 21)
        self.assertEqual(set(updated['officialAnalysis']['queue']), set(known_ids))
        self.assertFalse(any(item['id'] == new_id for item in updated['pending']))
        state = copy.deepcopy(state)
        with self.usage('known-only', limit=1) as usage:
            analyzer, opener = self.analyzer(state, usage)
            source = Source({tid: sources[tid] for tid in known_ids})
            self.collect(state, analyzer, source)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(len(opener.requests), 1)

    def test_queued_get_failure_is_not_automatically_repeated(self):
        state = official.empty_snapshot()
        with self.usage(limit=0) as usage:
            analyzer, _ = self.analyzer(state, usage)
            queued, _, _ = self.collect(state, analyzer)
        with self.usage('next') as usage:
            analyzer, _ = self.analyzer(queued, usage)
            failed, _, _ = self.collect(queued, analyzer, Source({
                TID: official.FetchFailure('network_error')}))
        self.assertFalse(failed['officialAnalysis']['queue'])
        self.assertEqual(failed['pending'][0]['reason'], 'azure_saved_body_required')
        with self.usage('last') as usage:
            analyzer, opener = self.analyzer(failed, usage)
            source = Source()
            self.collect(failed, analyzer, source)
        self.assertFalse(source.calls)
        self.assertFalse(opener.requests)

    def test_shared_preflight_blocks_queued_raw_get_without_spending(self):
        state = official.empty_snapshot()
        with self.usage(limit=0) as usage:
            analyzer, _ = self.analyzer(state, usage)
            queued, _, _ = self.collect(state, analyzer)
        with self.usage('next') as usage:
            analyzer, opener = self.analyzer(queued, usage)
            source = Source()
            with mock.patch.object(usage, 'check',
                                   side_effect=azure.ledger.UsageFailure('azure_backoff')) as check:
                updated, report, _ = self.collect(queued, analyzer, source)
            self.assertTrue(check.called)
            self.assertEqual(usage.used, 0)
        self.assertFalse(source.calls)
        self.assertFalse(opener.requests)
        self.assertEqual(report['requests']['posts'], 0)
        self.assertIn(TID, updated['officialAnalysis']['queue'])

    def test_prior_reserved_budget_failure_never_becomes_a_capacity_queue(self):
        state = self.old_state()
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage)
            post = state['posts'][0]
            key = azure.digest(azure.canonical_json([
                post['id'], post['authorId'], post['createdAt'], post['date'],
                post['shift'], post['storeId'], azure.digest(payload()['text']), analyzer.version]))
            usage.reserve(key, analyzer.client.identity)
            usage.finish(key, 'azure_budget_exhausted')
            updated, _, code = self.collect(state, analyzer, saved_payloads={TID: payload()},
                                             source_fetched_at=official.iso(NOW))
        self.assertEqual(code, 2)
        self.assertFalse(updated['officialAnalysis']['queue'])
        self.assertEqual(updated['officialAnalysis']['cache'][key]['reason'], 'azure_budget_exhausted')
        self.assertFalse(opener.requests)
        with self.usage('later') as usage:
            analyzer, opener = self.analyzer(updated, usage)
            source = Source()
            self.collect(updated, analyzer, source)
        self.assertFalse(source.calls)
        self.assertFalse(opener.requests)

    def test_unreserved_backoff_can_queue_without_an_issued_negative(self):
        state = official.empty_snapshot()
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage)
            with mock.patch.object(usage, 'reserve',
                                   side_effect=azure.ledger.UsageFailure('azure_backoff')):
                updated, _, code = self.collect(state, analyzer)
            self.assertEqual(usage.used, 0)
        self.assertEqual(code, 2)
        self.assertEqual(updated['officialAnalysis']['queue'][TID]['reason'], 'azure_backoff')
        self.assertFalse(updated['officialAnalysis']['cache'])
        self.assertFalse(opener.requests)
        self.roundtrip(updated)

    def test_only_explicit_usage_failure_type_is_converted(self):
        state = self.old_state()
        foreign = azure.module('separately_imported_usage', 'analysis-state.py')
        with foreign.SharedUsage(self.path, run_id='foreign', component='official',
                                 clock=self.clock, sleep=self.sleep) as usage:
            analyzer, _ = self.analyzer(state, usage)
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_backoff'):
                analyzer.usage_call(mock.Mock(side_effect=usage.failure_type('azure_backoff')))
            for failure in (OSError('disk failure'), ValueError('bad configuration')):
                with self.subTest(failure=type(failure)), self.assertRaises(type(failure)) as caught:
                    analyzer.usage_call(mock.Mock(side_effect=failure))
                self.assertIs(caught.exception, failure)
            for failure in (usage.failure_type('private response body'),
                            usage.failure_type('azure_http_error', 999),
                            usage.failure_type('azure_backoff', retry_at='not a timestamp')):
                with self.subTest(failure=failure), self.assertRaises(ValueError):
                    analyzer.usage_call(mock.Mock(side_effect=failure))
            with mock.patch.object(usage, 'reserve', side_effect=OSError('disk failure')):
                with self.assertRaisesRegex(OSError, 'disk failure'):
                    analyzer.parse(payload(), state['posts'][0], official.iso(NOW))

    def test_unsupported_edit_chain_keeps_facts_pending_without_inference(self):
        for edit in ({'edit_tweet_ids': [TID, '2096165714604486680']},
                     {'edit_tweet_ids': ['2096165714604486680']}, None):
            with self.subTest(edit=edit), self.usage() as usage:
                state, data = self.old_state(), payload()
                data['edit_control'] = edit
                analyzer, opener = self.analyzer(state, usage)
                updated, _, code = self.collect(state, analyzer, saved_payloads={TID: data},
                                                 source_fetched_at=official.iso(NOW))
                self.assertEqual(code, 2)
                self.assertEqual(updated['posts'], state['posts'])
                self.assertEqual(updated['pending'][0]['reason'], 'azure_edit_metadata')
                self.assertFalse(opener.requests)
                self.assertEqual(usage.used, 0)

    def test_edit_flags_require_literal_false_even_with_single_or_missing_edit_control(self):
        for field in ('isEdited', 'isStaleEdit'):
            for invalid in (True, 0, 1, None, 'false', [], {}):
                for control in (None, {'edit_tweet_ids': [TID]}):
                    with self.subTest(field=field, invalid=invalid, control=control):
                        data, state = payload(), self.old_state()
                        data[field] = invalid
                        if control is not None:
                            data['edit_control'] = control
                        with self.usage() as usage:
                            analyzer, opener = self.analyzer(state, usage)
                            updated, _, code = self.collect(
                                state, analyzer, saved_payloads={TID: data},
                                source_fetched_at=official.iso(NOW))
                            self.assertEqual(usage.used, 0)
                        self.assertEqual(code, 2)
                        self.assertEqual(updated['pending'][0]['reason'], 'azure_edit_metadata')
                        self.assertEqual(updated['posts'], state['posts'])
                        self.assertFalse(opener.requests)
        data = payload()
        data.update(isEdited=False, isStaleEdit=False, edit_control={'edit_tweet_ids': [TID]})
        self.assertTrue(azure.edit_metadata_supported(data, TID))

    def test_stale_flag_cannot_replay_a_previous_success_cache(self):
        state = self.old_state()
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage)
            analyzer.parse(payload(), state['posts'][0], official.iso(NOW))
            before = copy.deepcopy(analyzer.state['cache'])
            data = payload()
            data.update(isEdited=False, isStaleEdit=True)
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_edit_metadata'):
                analyzer.parse(data, state['posts'][0], official.iso(NOW))
            self.assertEqual(len(opener.requests), 1)
            self.assertEqual(analyzer.state['cache'], before)

    def test_approved_limits_are_enforced_and_included_in_cache_version(self):
        self.assertEqual(azure.LIMITS, {
            'maxInputBytes': 6000, 'maxSourceLines': 128, 'maxEvidenceLines': 16,
            'maxNotices': 20, 'maxCompletionTokens': 1200})
        self.assertEqual(len(azure.source_lines('a' * 6000)), 1)
        self.assertEqual(len(azure.source_lines('\n' * 127)), 128)
        for text in ('a' * 6001, '\n' * 128):
            with self.subTest(length=len(text)), self.assertRaisesRegex(
                    azure.AnalysisFailure, 'azure_input_limit'):
                azure.source_lines(text)
        with self.usage() as usage:
            analyzer, _ = self.analyzer(self.old_state(), usage)
            expected = azure.digest(azure.canonical_json([
                azure.VERSION, azure.PROMPT, azure.SCHEMA, azure.LIMITS,
                analyzer.names, analyzer.client.identity]))
            self.assertEqual(analyzer.version, expected)
            with mock.patch.dict(azure.LIMITS, maxInputBytes=6001):
                changed, _ = self.analyzer(self.old_state(), usage)
            self.assertNotEqual(analyzer.version, changed.version)

    def test_names_observation_source_acquisition_and_analysis_times_are_distinct(self):
        state = self.old_state()
        acquired = official.iso(NOW - dt.timedelta(minutes=15))
        original_observed = state['posts'][0]['observedAt']
        with self.usage() as usage:
            analyzer, _ = self.analyzer(state, usage)
            updated, report, code = self.collect(state, analyzer, saved_payloads={TID: payload()},
                                                 source_fetched_at=acquired)
        post = updated['posts'][0]
        self.assertEqual(code, 0)
        self.assertEqual(post['createdAt'], CREATED)
        self.assertEqual(post['observedAt'], original_observed)
        self.assertEqual(post['notices'][0]['observedAt'], acquired)
        entry = next(iter(updated['officialAnalysis']['cache'].values()))
        self.assertEqual(entry['fetchedAt'], acquired)
        self.assertEqual(official.timestamp(entry['at']), NOW)
        self.assertEqual(report['newNameCount'], 0)
        self.roundtrip(updated)

    def test_saved_input_unknown_id_or_missing_acquisition_is_rejected_offline(self):
        state = self.old_state()
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage)
            for saved, fetched in (({'2096165714604486680': payload()}, official.iso(NOW)),
                                   ({TID: payload()}, None)):
                with self.subTest(saved=saved), self.assertRaises(ValueError):
                    self.collect(state, analyzer, saved_payloads=saved, source_fetched_at=fetched)
        self.assertFalse(opener.requests)

    def test_explicit_saved_mode_never_constructs_source_client(self):
        state = self.old_state()
        snapshot = self.directory / 'snapshot.json'
        official.atomic_json(snapshot, state)
        saved = self.directory / 'saved.json'
        official.atomic_json(saved, {TID: payload()})
        curated = self.directory / 'curated.csv'
        curated.write_text('tweet_id,maid\n', encoding='utf-8')
        args = official.argument_parser().parse_args([
            '--analysis-backend', 'azure', '--ai-state', str(self.path),
            '--analysis-run-id', 'cli', '--analyze-saved', str(saved),
            '--source-fetched-at', official.iso(NOW), '--date-from', str(DAY),
            '--date-to', str(DAY), '--snapshot', str(snapshot)])
        opener = Opener(decision())
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(azure, 'load_names', return_value=list(NAMES)), \
                mock.patch.object(azure.transport.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(official, 'PublicClient', side_effect=AssertionError('network client constructed')), \
                mock.patch.object(official, 'write_report') as report:
            self.assertEqual(official.run(args, curated=curated, clock=self.clock, sleep=self.sleep), 0)
        self.assertEqual(report.call_args.args[0]['attemptedCount'], 0)
        self.assertEqual(report.call_args.args[0]['requests'], {'searches': 0, 'posts': 0})
        self.assertTrue(official.load_snapshot(snapshot)['posts'][0]['notices'])

    def test_cli_zero_allocation_still_collects_names_and_reports_deferral(self):
        snapshot = self.directory / 'snapshot.json'
        curated = self.directory / 'curated.csv'
        curated.write_text('tweet_id,maid\n', encoding='utf-8')
        args = official.argument_parser().parse_args([
            '--analysis-backend', 'azure', '--ai-state', str(self.path),
            '--analysis-run-id', 'zero', '--analysis-limit', '0',
            '--date-from', str(DAY), '--date-to', str(DAY), '--snapshot', str(snapshot)])
        opener, source = Opener(decision()), Source()
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(azure, 'load_names', return_value=list(NAMES)), \
                mock.patch.object(azure.transport.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(official, 'write_report') as report:
            self.assertEqual(official.run(args, curated=curated, client=source,
                                          clock=self.clock, sleep=self.sleep), 2)
        state = official.load_snapshot(snapshot)
        self.assertEqual(state['posts'][0]['names'], ['あむ', 'こい'])
        self.assertEqual(report.call_args.args[0]['newNameCount'], 2)
        self.assertEqual(report.call_args.args[0]['analysisDeferredCount'], 1)
        self.assertEqual(report.call_args.args[0]['analysisRequests'], 0)
        self.assertFalse(opener.requests)
        self.assertEqual(source.calls, [TID])

    def test_azure_dry_run_positive_cache_does_not_hide_unsaved_roster_on_live_run(self):
        snapshot = self.directory / 'snapshot.json'
        curated = self.directory / 'curated.csv'
        curated.write_text('tweet_id,maid\n', encoding='utf-8')
        args = official.argument_parser().parse_args([
            '--analysis-backend', 'azure', '--ai-state', str(self.path),
            '--analysis-run-id', 'dry', '--dry-run',
            '--date-from', str(DAY), '--date-to', str(DAY), '--snapshot', str(snapshot)])
        opener, first_source = Opener(decision()), Source()
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(azure, 'load_names', return_value=list(NAMES)), \
                mock.patch.object(azure.transport.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(official, 'write_report') as report:
            self.assertEqual(official.run(args, curated=curated, client=first_source,
                                          clock=self.clock, sleep=self.sleep), 0)
            first = official.load_snapshot(snapshot)
            self.assertFalse(first['posts'])
            self.assertTrue(first['officialAnalysis']['cache'])
            self.assertEqual(len(opener.requests), 1)
            self.now += dt.timedelta(minutes=5)
            args.dry_run, args.analysis_run_id = False, 'normal'
            next_source = Source()
            self.assertEqual(official.run(args, curated=curated, client=next_source,
                                          clock=self.clock, sleep=self.sleep), 0)
            self.assertEqual(report.call_args.args[0]['newNameCount'], 2)
            self.assertEqual(report.call_args.args[0]['analysisRequests'], 0)
        saved = official.load_snapshot(snapshot)
        self.assertEqual(saved['posts'][0]['names'], ['あむ', 'こい'])
        self.assertTrue(saved['posts'][0]['notices'])
        self.assertEqual(next_source.calls, [TID])
        self.assertEqual(len(opener.requests), 1)

    def test_same_run_buffer_reuses_unused_slots_without_source_or_losing_initial_report(self):
        work = official.ROOT / ('.cc-work-' + uuid.uuid4().hex[:16])
        work.mkdir()
        self.addCleanup(shutil.rmtree, work)
        buffer_path, snapshot = work / 'official-buffer.json', self.directory / 'snapshot.json'
        curated = self.directory / 'curated.csv'
        curated.write_text('tweet_id,maid\n', encoding='utf-8')
        common = [
            '--analysis-backend', 'azure', '--ai-state', str(self.path),
            '--analysis-run-id', 'same-run', '--date-from', str(DAY), '--date-to', str(DAY),
            '--snapshot', str(snapshot)]
        args = official.argument_parser().parse_args(
            common + ['--analysis-limit', '1', '--analysis-buffer', str(buffer_path)])
        source = Source(self.source_posts(5))
        fetch = source.fetch_post
        search = source.search

        def fetched(tid):
            self.now += dt.timedelta(seconds=1)
            return fetch(tid)

        def searched(url):
            if source.searches:
                source.searches.append(url)
                raise official.FetchFailure('network_error')
            return search(url)

        source.fetch_post = fetched
        source.search = searched
        opener = Opener(decision())
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(azure, 'load_names', return_value=list(NAMES)), \
                mock.patch.object(azure.transport.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(official, 'write_report') as reports:
            self.assertEqual(official.run(args, curated=curated, client=source,
                                          clock=self.clock, sleep=self.sleep), 2)
            initial = official.load_snapshot(snapshot)
            first_report = copy.deepcopy(reports.call_args.args[0])
            buffered = json.loads(buffer_path.read_text(encoding='utf-8'))
            self.assertEqual(len(buffered['items']), 2)
            self.assertEqual(len({entry['fetchedAt'] for entry in buffered['items']}), 2)
            self.assertEqual(len(opener.requests), 1)
            self.assertEqual(first_report['requests'], {'searches': 2, 'posts': 5})
            original = [{key: value for key, value in post.items() if key != 'notices'}
                        for post in initial['posts']]
            args = official.argument_parser().parse_args(
                common + ['--analysis-limit', '3', '--replay-buffer', str(buffer_path)])
            with mock.patch.object(official, 'PublicClient', side_effect=AssertionError('source client constructed')):
                self.assertEqual(official.run(args, curated=curated, clock=self.clock, sleep=self.sleep), 2)
            result = official.load_snapshot(snapshot)
            replay_report = reports.call_args.args[0]
        self.assertFalse(buffer_path.exists())
        self.assertEqual(len(source.calls), 5)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(replay_report['requests'], {'searches': 0, 'posts': 0})
        self.assertEqual(replay_report['analysisReplayCount'], 2)
        self.assertEqual(replay_report['analysisRequests'], 3)
        self.assertEqual(replay_report['newNameCount'], 0)
        self.assertEqual(replay_report['initialRun'], initial['lastRun'])
        self.assertEqual(result['lastRun'], initial['lastRun'])
        self.assertEqual(result['lastRun']['failures'], first_report['failures'])
        self.assertEqual(replay_report['sources'][1]['reason'], 'network_error')
        self.assertEqual(original, [{key: value for key, value in post.items() if key != 'notices'}
                                    for post in result['posts']])
        by_id = {post['id']: post for post in result['posts']}
        for entry in buffered['items'][:2]:
            self.assertEqual(by_id[entry['id']]['notices'][0]['observedAt'], entry['fetchedAt'])
        for value in (result, replay_report, first_report):
            self.assertNotIn('"payload"', json.dumps(value))
            self.assertNotIn('あとから来るにゃんね', json.dumps(value, ensure_ascii=False))

    def test_known_queue_prefetch_returns_unused_slot_or_discards_raw_after_personal_use(self):
        work = official.ROOT / ('.cc-work-' + uuid.uuid4().hex[:16])
        work.mkdir()
        self.addCleanup(shutil.rmtree, work)
        curated = self.directory / 'curated.csv'
        curated.write_text('tweet_id,maid\n', encoding='utf-8')
        for personal_active in (False, True):
            with self.subTest(personal_active=personal_active):
                azure.ledger.atomic_json(self.path, azure.ledger.empty_state())
                self.now = official.timestamp('2026-09-05T05:30:00Z')
                sources = self.source_posts(3, '2026-09-05T03:00:00Z')
                initial = self.queued_state(sources)
                snapshot, buffer_path = self.directory / 'known.json', work / 'known-buffer.json'
                official.atomic_json(snapshot, initial)
                run_id = 'known-prefetch'
                common = [
                    '--analysis-backend', 'azure', '--ai-state', str(self.path),
                    '--analysis-run-id', run_id, '--date-from', str(DAY), '--date-to', str(DAY),
                    '--snapshot', str(snapshot)]
                args = official.argument_parser().parse_args(
                    common + ['--analysis-limit', '2', '--analysis-buffer', str(buffer_path)])
                opener, source = Opener(decision()), Source(sources)
                fetch = source.fetch_post

                def before_ai(tid):
                    self.assertFalse(opener.requests)
                    self.assertFalse(azure.ledger.load_state(self.path)['receipts'])
                    return fetch(tid)

                source.fetch_post = before_ai
                with mock.patch.dict(os.environ, ENV), \
                        mock.patch.object(azure, 'load_names', return_value=list(NAMES)), \
                        mock.patch.object(azure.transport.urllib.request, 'build_opener', return_value=opener), \
                        mock.patch.object(official, 'write_report') as reports:
                    self.assertEqual(official.run(args, curated=curated, client=source,
                                                  clock=self.clock, sleep=self.sleep), 2)
                    first = official.load_snapshot(snapshot)
                    self.assertEqual(len(source.calls), 3)
                    self.assertEqual(len(opener.requests), 2)
                    buffered = json.loads(buffer_path.read_text(encoding='utf-8'))
                    self.assertEqual(len(buffered['items']), 1)
                    if personal_active:
                        self.seed_personal_requests(run_id, 1)
                    args = official.argument_parser().parse_args(
                        common + ['--analysis-limit', '2' if personal_active else '3',
                                  '--replay-buffer', str(buffer_path)])
                    with mock.patch.object(official, 'PublicClient',
                                           side_effect=AssertionError('resume source forbidden')):
                        self.assertEqual(official.run(args, curated=curated,
                                                      clock=self.clock, sleep=self.sleep), 2)
                    replay = reports.call_args.args[0]
                result = official.load_snapshot(snapshot)
                receipts = azure.ledger.load_state(self.path)['receipts'].values()
                self.assertEqual(sum(item['component'] == 'official' for item in receipts),
                                 2 if personal_active else 3)
                self.assertEqual(sum(item['component'] == 'personal' for item in receipts),
                                 1 if personal_active else 0)
                self.assertEqual(len(azure.ledger.load_state(self.path)['receipts']), 3)
                self.assertEqual(len(source.calls), 3)
                self.assertEqual(len(opener.requests), 2 if personal_active else 3)
                self.assertEqual(replay['requests'], {'searches': 0, 'posts': 0})
                self.assertEqual(result['lastRun'], first['lastRun'])
                self.assertEqual(len(result['pending']), 1 if personal_active else 0)
                self.assertFalse(buffer_path.exists())
                for before, after in zip(initial['posts'], result['posts']):
                    self.assertEqual(before['names'], after['names'])
                    self.assertEqual(before['observedAt'], after['observedAt'])

    def test_mixed_new_and_known_sources_use_three_slots_and_cache_hits_leave_capacity_free(self):
        work = official.ROOT / ('.cc-work-' + uuid.uuid4().hex[:16])
        work.mkdir()
        self.addCleanup(shutil.rmtree, work)
        curated = self.directory / 'curated.csv'
        curated.write_text('tweet_id,maid\n', encoding='utf-8')
        for cache_hit in (False, True):
            with self.subTest(cache_hit=cache_hit):
                azure.ledger.atomic_json(self.path, azure.ledger.empty_state())
                self.now = NOW
                sources = self.source_posts(4 if cache_hit else 3)
                new_id = list(sources)[-1]
                known = {tid: value for tid, value in sources.items() if tid != new_id}
                state = self.queued_state(known)
                if cache_hit:
                    with self.usage('previous-run') as usage:
                        analyzer, previous_opener = self.analyzer(state, usage)
                        new_post = official.validate_post(new_id, sources[new_id], DAY, DAY, self.clock())
                        analyzer.parse(sources[new_id], new_post, self.now.isoformat().replace('+00:00', 'Z'))
                        self.assertEqual(len(previous_opener.requests), 1)
                    self.now += dt.timedelta(minutes=5)
                snapshot, buffer_path = self.directory / 'mixed.json', work / 'mixed-buffer.json'
                official.atomic_json(snapshot, state)
                common = [
                    '--analysis-backend', 'azure', '--ai-state', str(self.path),
                    '--analysis-run-id', 'mixed-run', '--date-from', str(DAY), '--date-to', str(DAY),
                    '--snapshot', str(snapshot)]
                args = official.argument_parser().parse_args(
                    common + ['--analysis-limit', '2', '--analysis-buffer', str(buffer_path)])
                opener, source = Opener(decision()), Source(sources)
                fetch = source.fetch_post

                def before_ai(tid):
                    self.assertFalse(opener.requests)
                    current = [item for item in azure.ledger.load_state(self.path)['receipts'].values()
                               if item['runId'] == 'mixed-run']
                    self.assertFalse(current)
                    return fetch(tid)

                source.fetch_post = before_ai
                with mock.patch.dict(os.environ, ENV), \
                        mock.patch.object(azure, 'load_names', return_value=sorted(NAMES)), \
                        mock.patch.object(azure.transport.urllib.request, 'build_opener', return_value=opener), \
                        mock.patch.object(official, 'write_report') as reports:
                    self.assertEqual(official.run(args, curated=curated, client=source,
                                                  clock=self.clock, sleep=self.sleep), 2)
                    first = official.load_snapshot(snapshot)
                    self.assertEqual(reports.call_args.args[0]['newNameCount'], 2)
                    self.assertEqual(source.calls[0], new_id)
                    self.assertEqual(len(source.calls), 4 if cache_hit else 3)
                    self.assertEqual(len(opener.requests), 2)
                    self.assertEqual(len(json.loads(buffer_path.read_text(encoding='utf-8'))['items']), 1)
                    args = official.argument_parser().parse_args(
                        common + ['--analysis-limit', '3', '--replay-buffer', str(buffer_path)])
                    with mock.patch.object(official, 'PublicClient',
                                           side_effect=AssertionError('resume source forbidden')):
                        self.assertEqual(official.run(args, curated=curated,
                                                      clock=self.clock, sleep=self.sleep), 2)
                    self.assertEqual(reports.call_args.args[0]['requests'], {'searches': 0, 'posts': 0})
                result = official.load_snapshot(snapshot)
                self.assertFalse(buffer_path.exists())
                self.assertEqual(len(opener.requests), 3)
                self.assertEqual(len(source.calls), 4 if cache_hit else 3)
                self.assertEqual(sum(item['runId'] == 'mixed-run' for item in
                                     azure.ledger.load_state(self.path)['receipts'].values()), 3)
                self.assertFalse(result['pending'])
                self.assertEqual(result['lastRun'], first['lastRun'])
                self.assertTrue(all(post.get('notices') for post in result['posts']))

    def test_prefetch_prioritizes_new_source_and_never_fetches_after_any_ai_reservation(self):
        sources = self.source_posts(4)
        known = dict(list(sources.items())[:3])
        new_id = list(sources)[3]
        state, buffer = self.queued_state(known), []
        with self.usage(limit=1) as usage:
            analyzer, opener = self.analyzer(state, usage)
            source = Source(sources)
            fetch = source.fetch_post

            def before_ai(tid):
                self.assertEqual(usage.used, 0)
                return fetch(tid)

            source.fetch_post = before_ai
            updated, report, _ = self.collect(state, analyzer, source, buffered_payloads=buffer)
            self.assertEqual(usage.used, 1)
        self.assertEqual(source.calls[0], new_id)
        self.assertEqual(len(source.calls), 3)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(report['newNameCount'], 2)
        self.assertEqual(report['requests']['posts'], 3)
        self.assertEqual(len(buffer), 2)
        self.assertTrue(next(post for post in updated['posts'] if post['id'] == new_id)['notices'])

    def test_prefetch_is_bounded_by_shared_one_or_zero_slots_even_with_component_zero(self):
        for personal_count, official_limit in ((2, 1), (2, 0), (3, 1)):
            with self.subTest(personal_count=personal_count, official_limit=official_limit):
                azure.ledger.atomic_json(self.path, azure.ledger.empty_state())
                self.now = NOW
                self.seed_personal_requests('remaining', personal_count)
                sources = self.source_posts(3)
                state, buffer = self.queued_state(sources), []
                with self.usage('remaining', limit=official_limit) as usage:
                    analyzer, opener = self.analyzer(state, usage)
                    source = Source(sources)
                    _, report, _ = self.collect(state, analyzer, source, buffered_payloads=buffer)
                expected_gets = 1 if personal_count == 2 else 0
                self.assertEqual(len(source.calls), expected_gets)
                self.assertEqual(report['requests']['posts'], expected_gets)
                self.assertEqual(len(opener.requests), 1 if expected_gets and official_limit else 0)
                self.assertEqual(len(buffer), 1 if expected_gets and not official_limit else 0)
                self.assertLessEqual(len(azure.ledger.load_state(self.path)['receipts']), 3)

    def test_prefetch_respects_host_stop_and_twenty_source_cap(self):
        sources = self.source_posts(3)
        state, buffer = self.queued_state(sources), []
        middle = list(sources)[1]
        limited = dict(sources)
        limited[middle] = official.FetchFailure('http_error', 429, NOW + dt.timedelta(hours=1))
        with self.usage('host') as usage:
            analyzer, _ = self.analyzer(state, usage)
            source = Source(limited)
            updated, report, _ = self.collect(state, analyzer, source, buffered_payloads=buffer)
        self.assertEqual(len(source.calls), 2)
        self.assertEqual(report['requests']['posts'], 2)
        self.assertIn(official.POST_HOST, updated['cooldowns'])
        azure.ledger.atomic_json(self.path, azure.ledger.empty_state())
        self.now = NOW
        all_sources = self.source_posts(22)
        known = dict(list(all_sources.items())[:3])
        state = self.queued_state(known)
        for tid in list(all_sources)[3:]:
            all_sources[tid]['text'] = 'お知らせ'
        with self.usage('source-cap') as usage:
            analyzer, _ = self.analyzer(state, usage)
            source, buffer = Source(all_sources), []
            _, report, _ = self.collect(state, analyzer, source, buffered_payloads=buffer)
        self.assertEqual(len(source.calls), 20)
        self.assertEqual(report['requests']['posts'], 20)
        self.assertEqual(sum(tid in known for tid in source.calls), 1)

    def test_buffer_rejects_other_run_tampered_source_and_public_paths(self):
        state, entries = official.empty_snapshot(), []
        with self.usage(limit=0) as usage:
            analyzer, opener = self.analyzer(state, usage)
            updated, _, _ = official.collect(
                state, set(), Source(), DAY, DAY, 20, clock=self.clock,
                analyzer=analyzer, buffered_payloads=entries)
            value = official.make_analysis_buffer(updated, usage.run_id, analyzer.version, entries, self.clock())
            with self.assertRaises(ValueError):
                official.validate_analysis_buffer(value, updated, 'different-run', self.clock())
            for mutation in (
                    lambda item: item['items'][0]['payload'].update(text='different raw'),
                    lambda item: item.update(versionHash='a' * 64),
                    lambda item: item.update(stateHash='b' * 64),
                    lambda item: item['items'].extend(copy.deepcopy(item['items']) * 3)):
                changed = copy.deepcopy(value)
                mutation(changed)
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    official.replay_analysis_buffer(updated, changed, analyzer, self.clock)
        self.assertFalse(opener.requests)
        with self.assertRaises(ValueError):
            official.analysis_buffer_path(official.ROOT / 'data' / 'official-buffer.json')

    def test_input_limit_keeps_names_without_an_azure_request(self):
        state, data = official.empty_snapshot(), payload()
        data['text'] += '長い文' * 5000
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage)
            updated, _, code = self.collect(state, analyzer, Source({TID: data}))
            self.assertEqual(usage.used, 0)
        self.assertEqual(code, 2)
        self.assertEqual(updated['posts'][0]['names'], ['あむ', 'こい'])
        self.assertFalse(opener.requests)
        self.assertEqual(updated['pending'][0]['reason'], 'azure_input_limit')

    def test_notice_changes_history_and_reanalysis_replay_is_idempotent(self):
        state = self.old_state()
        with self.usage() as usage:
            analyzer, _ = self.analyzer(state, usage)
            first, _, _ = self.collect(state, analyzer, saved_payloads={TID: payload()},
                                       source_fetched_at=official.iso(NOW))
        self.assertEqual(len(first['officialAnalysis']['history']), 1)
        with self.usage('replay') as usage:
            analyzer, opener = self.analyzer(first, usage)
            second, _, _ = self.collect(first, analyzer, saved_payloads={TID: payload()},
                                        source_fetched_at=official.iso(NOW))
        self.assertEqual(second['posts'], first['posts'])
        self.assertEqual(second['officialAnalysis']['history'], first['officialAnalysis']['history'])
        self.assertFalse(opener.requests)
        self.now += dt.timedelta(minutes=5)
        data = payload(payload()['text'] + 'みりあは19時から遅れて合流\n')
        with self.usage('changed') as usage:
            analyzer, _ = self.analyzer(second, usage, decision(when='19:00', ids=(1, 2, 9)))
            changed, _, _ = self.collect(second, analyzer, saved_payloads={TID: data},
                                         source_fetched_at=self.now.isoformat().replace('+00:00', 'Z'))
        self.assertEqual(changed['posts'][0]['notices'][0]['time'], '19:00')
        self.assertEqual(len(changed['officialAnalysis']['history']), 2)
        self.assertEqual(changed['officialAnalysis']['history'][-1]['notices'], first['posts'][0]['notices'])
        self.roundtrip(changed)

    def test_name_choices_accepts_only_validated_roster_and_local_aliases(self):
        self.assertEqual(azure.name_choices({'roster': ['あむ']},
                                           {'maidTendency': {'あむ': {'alias': 'あむちゃん'}}}),
                         ['あむ', 'あむちゃん'])
        for schedule in ({'roster': []}, {'roster': ['あむ', 'あむ']}, {'roster': ['bad\nname']}):
            with self.subTest(schedule=schedule), self.assertRaises(ValueError):
                azure.name_choices(schedule)

    def test_shared_http_failures_remain_private_and_are_never_retried(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                azure.ledger.atomic_json(self.path, azure.ledger.empty_state())
                state = official.empty_snapshot()
                error = urllib.error.HTTPError('https://offline-test.openai.azure.com',
                                               status, 'private text', {'Retry-After': '3600'}, None)
                with self.usage('http-' + str(status)) as usage:
                    analyzer, opener = self.analyzer(state, usage, error)
                    updated, report, code = self.collect(state, analyzer)
                self.assertEqual(code, 2)
                self.assertEqual(len(opener.requests), 1)
                self.assertEqual(len(updated['posts']), 1)
                self.assertNotIn('private text', json.dumps(report))
                self.roundtrip(updated)
                ledger = azure.ledger.load_state(self.path)
                if status == 429:
                    self.assertIsNotNone(ledger['retryAt'])
                else:
                    self.assertIsNotNone(ledger['paused'])

    def test_checkpointed_response_recovers_without_raw_or_another_request(self):
        state, checkpoints = official.empty_snapshot(), []
        with self.usage() as usage:
            analyzer, _ = self.analyzer(state, usage, save=lambda value: checkpoints.append(copy.deepcopy(value)))
            completed, _, _ = self.collect(state, analyzer)
        checkpoint = self.roundtrip(checkpoints[-1])
        self.assertNotIn('notices', checkpoint['posts'][0])
        with self.usage('recover') as usage:
            analyzer, opener = self.analyzer(checkpoint, usage)
            source = Source()
            recovered, _, _ = self.collect(checkpoint, analyzer, source)
        self.assertEqual(recovered['posts'], completed['posts'])
        self.assertFalse(source.calls)
        self.assertFalse(opener.requests)
        self.assertEqual(len(recovered['officialAnalysis']['history']), 1)

    def test_saved_older_raw_preserves_notice_and_stays_pending_without_inference(self):
        state = self.old_state()
        state['posts'][0]['notices'] = [{
            'name': 'みりあ', 'kind': 'late', 'excerpt': 'みりあ', 'observedAt': official.iso(NOW)}]
        with self.usage() as usage:
            analyzer, opener = self.analyzer(state, usage)
            updated, _, code = self.collect(
                state, analyzer, saved_payloads={TID: payload()},
                source_fetched_at=official.iso(NOW - dt.timedelta(minutes=1)))
        self.assertEqual(code, 2)
        self.assertEqual(updated['posts'], state['posts'])
        self.assertEqual(updated['pending'][0]['reason'], 'azure_stale_source')
        self.assertFalse(opener.requests)

    def test_malformed_cached_notice_cannot_claim_a_different_acquisition(self):
        state = official.empty_snapshot()
        with self.usage() as usage:
            analyzer, _ = self.analyzer(state, usage)
            updated, _, _ = self.collect(state, analyzer)
        cached = next(iter(updated['officialAnalysis']['cache'].values()))
        cached['notices'][0]['observedAt'] = official.iso(NOW - dt.timedelta(minutes=1))
        with self.assertRaises(ValueError):
            azure.validate_state(updated['officialAnalysis'], official.analysis_context())

    def test_legacy_notice_without_acquisition_remains_readable_and_can_be_upgraded(self):
        state = self.old_state()
        state['posts'][0]['notices'] = [{'name': 'みりあ', 'kind': 'late', 'excerpt': 'みりあ'}]
        self.roundtrip(state)
        with self.usage() as usage:
            analyzer, _ = self.analyzer(state, usage)
            updated, _, code = self.collect(state, analyzer, saved_payloads={TID: payload()},
                                             source_fetched_at=official.iso(NOW))
        self.assertEqual(code, 0)
        self.assertEqual(updated['posts'][0]['notices'][0]['observedAt'], official.iso(NOW))
        self.assertEqual(updated['posts'][0]['observedAt'], state['posts'][0]['observedAt'])
        self.assertFalse(updated['officialAnalysis']['history'])
        self.roundtrip(updated)


class AmendmentTests(unittest.TestCase):
    def state(self):
        result = official.empty_snapshot()
        result['posts'] = [official.validate_post(TID, payload(), DAY, DAY,
                                                   NOW - dt.timedelta(hours=1))]
        return result

    def amendment(self):
        return {
            'schemaVersion': 1, 'id': TID, 'source': {
                'url': official.canonical(TID), 'authorId': official.AUTHOR_ID,
                'authorScreenName': official.AUTHOR, 'createdAt': CREATED,
                'fetchedAt': official.iso(NOW), 'bodyHash': azure.digest(payload()['text'])},
            'notices': [{'name': 'みりあ', 'kind': 'late', 'excerpt': 'みりあ',
                         'observedAt': official.iso(NOW)}]}

    def test_target_only_metadata_preservation_receipt_and_no_raw_storage(self):
        state, amendment = self.state(), self.amendment()
        original = copy.deepcopy(state)
        updated = official.apply_saved_notice(state, amendment)
        self.assertEqual(state, original)
        post = updated['posts'][0]
        self.assertEqual({key: value for key, value in post.items() if key != 'notices'},
                         original['posts'][0])
        self.assertEqual(len(updated['officialAnalysis']['receipts']), 1)
        self.assertEqual(official.apply_saved_notice(updated, amendment), updated)
        self.assertNotIn('text', json.dumps(updated))
        azure.validate_state(updated['officialAnalysis'], official.analysis_context())

    def test_invalid_amendment_metadata_hash_and_notice_fields_are_rejected(self):
        mutations = [
            lambda value: value['source'].update(bodyHash='bad'),
            lambda value: value['source'].update(authorId='111111111111111111'),
            lambda value: value['source'].update(createdAt='2026-09-05T09:17:07Z'),
            lambda value: value['source'].update(url='https://example.com'),
            lambda value: value['notices'][0].update(time='25:90'),
            lambda value: value['notices'][0].update(evidenceLineIds=[8]),
            lambda value: value['notices'][0].update(observedAt=CREATED),
            lambda value: value['notices'][0].pop('observedAt'),
            lambda value: value['source'].update(analyzedAt=official.iso(NOW)),
            lambda value: value['source'].update(analysisReceiptHash='a' * 64),
            lambda value: value['source'].update(analyzedAt=CREATED, analysisReceiptHash='a' * 64),
            lambda value: value.update(text='private raw'),
        ]
        for mutate in mutations:
            value = self.amendment()
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                official.apply_saved_notice(self.state(), value)

    def test_analysis_receipt_time_is_private_and_distinct_from_raw_acquisition(self):
        amendment = self.amendment()
        analyzed_at = official.iso(NOW + dt.timedelta(minutes=15))
        amendment['source'].update(analyzedAt=analyzed_at, analysisReceiptHash='a' * 64)
        state = self.state()
        updated = official.apply_saved_notice(state, amendment)
        self.assertEqual(updated['posts'][0]['notices'][0]['observedAt'], official.iso(NOW))
        self.assertEqual(updated['posts'][0]['observedAt'], state['posts'][0]['observedAt'])
        self.assertEqual(updated['posts'][0]['createdAt'], CREATED)
        self.assertEqual(updated['officialAnalysis']['history'][0]['at'], analyzed_at)
        receipt = next(iter(updated['officialAnalysis']['receipts'].values()))
        self.assertEqual(receipt['at'], official.iso(NOW))
        self.assertEqual(receipt['analyzedAt'], analyzed_at)
        self.assertEqual(receipt['analysisReceiptHash'], 'a' * 64)
        azure.validate_state(updated['officialAnalysis'], official.analysis_context())
        self.assertEqual(official.apply_saved_notice(updated, amendment), updated)
        self.assertNotIn('analysisReceiptHash', json.dumps(updated['posts']))

    def test_snapshot_rejects_private_raw_and_undeclared_notice_fields(self):
        state = official.apply_saved_notice(self.state(), self.amendment())
        for field, value in [('text', 'private'), ('evidenceLineIds', [1]), ('time', None)]:
            notices = copy.deepcopy(state['posts'][0]['notices'])
            notices[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                azure.validate_notices(notices, official.analysis_context())
        state['officialAnalysis']['text'] = 'private raw'
        with self.assertRaises(ValueError):
            azure.validate_state(state['officialAnalysis'], official.analysis_context())


if __name__ == '__main__':
    unittest.main()
