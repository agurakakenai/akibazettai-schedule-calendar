"""Candidate-level fault isolation for the finite half-month cycle.

Synthetic sources only; Luna is mocked and no live network is reachable.
"""
import copy
import datetime as dt
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import test_half_month_cycle as cycle
import test_half_month_schedules as base


collector, facts, azure = base.collector, base.facts, base.azure
reading_fixture = cycle.reading_fixture
failure_facts = collector.failure_facts


class IsolationBase(unittest.TestCase):
    setUp_cycle = cycle.CycleTests.setUp
    client = cycle.CycleTests.client
    run_cycle = cycle.CycleTests.run_cycle
    reopen = cycle.CycleTests.reopen

    def setUp(self):
        self.setUp_cycle()
        self.registry['members'].append(collector.members.new_member(
            '新人', 'https://x.com/new_member', base.NOW, member_id='m-' + '2' * 32))
        entry = base.entry(suffix=2, handle='new_member')
        entry['userId'] = '2080944098043977778'
        payload = base.payload(suffix=2)
        payload['user'] = {'id_str': entry['userId'], 'screen_name': 'new_member'}
        payload['mediaDetails'][0]['source_user_id'] = int(entry['userId'])
        payload['mediaDetails'][0]['expanded_url'] = f'https://x.com/new_member/status/{entry["id"]}/photo/1'
        self.documents['new_member'] = base.document(entry)
        self.payloads[entry['id']] = payload
        self.first_id = base.post_id()

    def reading(self):
        return next(row for row in self.state['readings'].values() if row['sourceId'] == self.first_id)


class CycleIsolationTests(IsolationBase):
    def assert_isolated(self, report, exception_class, stage):
        self.assertIn(failure_facts.UNEXPECTED_REASON, report['reasons'])
        diagnostic = next(row for row in report['diagnostics'] if row['postId'] == self.first_id)
        self.assertEqual(diagnostic['reason'], failure_facts.UNEXPECTED_REASON)
        self.assertEqual(diagnostic['exceptionClass'], exception_class)
        self.assertEqual(diagnostic['stage'], stage)
        self.assertRegex(diagnostic.get('location', 'x.py:f:1'), r'\.py:[A-Za-z_<>]+:[0-9]+\Z')
        text = json.dumps(report, ensure_ascii=False)
        for private in ('private-detail', 'pbs.twimg.com/media', str(self.path), 'SYNTHETIC'):
            self.assertNotIn(private, text)
        # The other person is still searched, fetched, read and confirmed in the same run.
        self.assertIn('新人', {row['name'] for row in self.state['schedules']})
        facts.validate_state(self.state)
        public = json.dumps(facts.public_state(self.state), ensure_ascii=False)
        self.assertNotIn('exceptionClass', public)

    def inject(self, attribute, error):
        factory = self.client

        def client():
            value = factory()
            normal = getattr(value, attribute).side_effect

            def failing(*args):
                if not self.raised and (attribute != 'image' or 'SYNTHETIC_1_' in args[0]):
                    self.raised = True
                    if attribute == 'image':
                        self.gets['images'].append(args[0])
                    raise error
                return normal(*args)
            getattr(value, attribute).side_effect = failing
            return value
        self.raised = False
        self.client = client

    def test_image_fetch_defect_after_issue_is_isolated_and_retried_once_then_held(self):
        self.inject('image', TypeError('private-detail'))
        report, _ = self.run_cycle()
        self.assert_isolated(report, 'TypeError', 'fetch')
        self.assertEqual(self.reading()['stage'], 'fetch')
        self.assertIsNotNone(self.reading()['nextAt'])
        self.assertEqual(self.usage.calls, 1)
        calls = self.usage.calls
        # Not due yet: no source or paid request is repeated.
        self.reopen()
        gets = copy.deepcopy(self.gets)
        self.run_cycle(resume=True)
        self.assertEqual((self.gets, self.usage.calls), (gets, calls))
        # One automatic retry after the delay, then the same defect is held.
        self.now += dt.timedelta(hours=7)
        self.reopen()
        self.inject('image', TypeError('private-detail'))
        held, _ = self.run_cycle(resume=True)
        self.assertEqual(self.reading()['stage'], 'held')
        self.assertIsNone(self.reading()['nextAt'])
        self.assertEqual(next(row for row in held['diagnostics'] if row['postId'] == self.first_id)['nextStage'],
                         'held')
        self.now += dt.timedelta(days=1)
        self.reopen()
        gets = copy.deepcopy(self.gets)
        self.run_cycle(resume=True)
        self.assertEqual(self.gets, gets)
        self.assertEqual(self.usage.calls, calls)

    def test_cache_write_defect_after_image_get_is_isolated(self):
        put = self.cache.put
        state = {'calls': 0}

        def failing(key, source, text, images, **kwargs):
            if images and state['calls'] == 0:
                state['calls'] += 1
                raise struct.error('private-detail')
            return put(key, source, text, images, **kwargs)
        with mock.patch.object(self.cache, 'put', side_effect=failing):
            report, _ = self.run_cycle()
        self.assert_isolated(report, 'error', 'fetch')
        self.assertEqual(self.gets['images'].count(next(
            url for url in self.gets['images'] if 'SYNTHETIC_1_0' in url)), 1)

    def test_reading_defect_before_or_after_reservation_is_isolated(self):
        original = collector.read_evidence
        state = {'raised': False}

        def failing(*args, **kwargs):
            if not state['raised']:
                state['raised'] = True
                raise KeyError('private-detail')
            return original(*args, **kwargs)
        with mock.patch.object(collector, 'read_evidence', side_effect=failing):
            report, _ = self.run_cycle()
        self.assert_isolated(report, 'KeyError', 'fetch')
        self.assertEqual(self.usage.calls, 1)
        self.assertEqual(len(set(self.usage.requests)), len(self.usage.requests))

    def test_ledger_string_retry_at_cannot_escape_the_failure_handler(self):
        module = collector.source_safety
        retry = facts.stamp(base.NOW + dt.timedelta(hours=2))
        self.inject('search', module.SourceFailure('source_host_cooldown', retry_at=retry))
        report, _ = self.run_cycle()
        self.assertIn('paused', report['reasons'])
        self.assertEqual(self.state['coverage']['2026-09-01']['あむ']['nextCheckAt'], retry)
        self.assertIn('新人', {row['name'] for row in self.state['schedules']})
        for invalid in ('not-a-time', 12345):
            self.assertEqual(collector._cycle_failure(
                module.SourceFailure('source_host_cooldown', retry_at=invalid), base.NOW)[1],
                base.NOW + dt.timedelta(hours=1))
        usage_failure = collector._module('analysis-state.py', 'isolation_usage').UsageFailure(
            'azure_backoff', retry_at=retry)
        self.assertEqual(collector._cycle_failure(usage_failure, base.NOW)[1], facts.timestamp(retry))

    def test_pre_post_reservation_defect_makes_no_source_request(self):
        check = self.usage.check
        state = {'raised': False}

        def failing():
            if not state['raised']:
                state['raised'] = True
                raise IndexError('private-detail')
            return check()
        self.usage.check = failing
        report, _ = self.run_cycle()
        self.assertIn(failure_facts.UNEXPECTED_REASON, report['reasons'])
        self.assertNotIn(self.first_id, self.gets['posts'])
        self.assertIn('新人', {row['name'] for row in self.state['schedules']})

    def test_ledger_storage_faults_still_stop_the_cycle(self):
        for error in (ValueError('source_usage_save_failed'), ValueError('invalid_ai_usage'),
                      MemoryError()):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.inject('image', error)
                with self.assertRaises(type(error)):
                    self.run_cycle()

    def test_missing_dependency_stops_without_holding_or_blaming_the_candidate(self):
        self.inject('image', ModuleNotFoundError("No module named 'PIL'"))
        with self.assertRaises(ModuleNotFoundError):
            self.run_cycle()
        for row in self.state['readings'].values():
            self.assertNotEqual(row['stage'], 'held')
            self.assertNotIn('failure', row)

    def test_reply_to_another_account_is_dropped_not_an_identity_verdict(self):
        reply = str(int(self.first_id) + 5)
        entry = base.entry(suffix=6)
        self.documents[base.TARGET['handle']] = base.document(base.entry(), entry)
        payload = base.payload(suffix=6, photos=0)
        payload.update(in_reply_to_status_id_str=str(int(self.first_id) - 9),
                       in_reply_to_user_id_str='1234567890123',
                       parent={'id_str': str(int(self.first_id) - 9),
                               'user': {'id_str': '1234567890123', 'screen_name': 'someone_else'}})
        self.payloads[entry['id']] = payload
        report, _ = self.run_cycle()
        self.assertNotIn(entry['id'], {item['id'] for item in self.state['pending']})
        history = [row for row in self.state['candidateHistory'].values() if row['candidate']['id'] == entry['id']]
        self.assertEqual([row['reason'] for row in history], ['not_schedule'])
        self.assertNotIn('identity_unknown', report['reasons'])
        self.assertEqual({row['name'] for row in self.state['schedules']}, {'あむ', '新人'})
        self.assertEqual(report['diagnostics'], [])
        posts = list(self.gets['posts'])
        self.now += dt.timedelta(days=1)
        self.reopen()
        self.run_cycle()
        self.assertEqual(self.gets['posts'].count(entry['id']), posts.count(entry['id']))
        facts.validate_state(self.state)

    def test_reason_token_detail_is_kept_but_messages_are_not(self):
        self.inject('image', collector.Failure('network_error'))
        report, _ = self.run_cycle()
        row = next(item for item in report['diagnostics'] if item['postId'] == self.first_id)
        self.assertEqual((row['reason'], row['detail']), ('image_fetch_failed', 'network_error'))
        self.setUp()
        self.inject('image', ValueError('Expecting value: line 1 private-detail'))
        report, _ = self.run_cycle()
        row = next(item for item in report['diagnostics'] if item['postId'] == self.first_id)
        self.assertNotIn('detail', row)
        self.assertNotIn('private-detail', json.dumps(report))

    def test_main_reports_storage_faults_as_4_and_other_crashes_as_1(self):
        argv = ['--snapshot', 'unused.json', '--source-state', 'source.json',
                '--personal-snapshot', 'personal.json', '--http-state', 'http.json',
                '--ai-state', 'ai.json', '--analysis-run-id', 'mock']
        for error, code, reason in (
                (ValueError('source_usage_save_failed'), 4, 'schedule_storage_failed'),
                (OSError('disk private-detail'), 4, 'schedule_storage_failed'),
                (TypeError('private-detail'), 1, 'schedule_infrastructure_failed')):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(collector, 'run', side_effect=error), \
                    mock.patch('builtins.print') as output:
                self.assertEqual(collector.main(argv), code)
                emitted = json.loads(output.call_args.args[0])
                self.assertEqual((emitted['exitCode'], emitted['reason']), (code, reason))
                self.assertEqual(emitted['exceptionClass'], type(error).__name__)
                self.assertNotIn('private-detail', output.call_args.args[0])


class ResumeOrderTests(IsolationBase):
    def ordered_client(self, calls, deadline_handle=None):
        factory = cycle.CycleTests.client.__get__(self)

        def client():
            value = factory()
            for kind in ('search', 'post', 'image'):
                normal = getattr(value, kind).side_effect

                def wrapped(arg, kind=kind, normal=normal):
                    if kind == 'search' and arg == deadline_handle:
                        raise collector.Failure('time_limit')
                    calls.append((kind, arg))
                    return normal(arg)
                getattr(value, kind).side_effect = wrapped
            return value
        return client

    def test_resume_starts_at_cursor_before_earlier_names_pending_work(self):
        calls = []
        # あむ's first reading stays unresolved, so she keeps actionable pending work.
        self.model.structured.return_value = {'classification': 'uncertain', 'complete': False, 'periods': []}
        self.client = self.ordered_client(calls, deadline_handle='new_member')
        self.run_cycle()
        self.assertEqual(self.state['collection']['cursor'], 1)
        self.assertEqual(self.state['collection']['reason'], 'time_limit')
        later = base.entry(suffix=3)
        self.payloads[later['id']] = base.payload(suffix=3)
        candidates, _ = collector.discover(base.document(later), {'あむ': base.TARGET}, self.now, {},
                                           registry=self.registry)
        collector.enqueue(self.state, candidates, self.now)
        calls.clear()
        self.model.structured.return_value = reading_fixture.result()
        self.now += dt.timedelta(minutes=5)
        self.reopen()
        self.client = self.ordered_client(calls)
        self.run_cycle(resume=True)
        self.assertEqual(calls[0], ('search', 'new_member'))
        self.assertLess(calls.index(('search', 'new_member')), calls.index(('post', later['id'])))

    def test_past_check_times_never_become_a_stale_checkpoint_next_at(self):
        self.documents['new_member'] = base.document()
        self.run_cycle()
        row = self.state['coverage']['2026-09-01']['新人']
        self.assertFalse(row['confirmedIds'])
        row['nextCheckAt'] = facts.stamp(base.NOW - dt.timedelta(days=10))
        calls = []
        self.reopen()
        self.client = self.ordered_client(calls, deadline_handle='new_member')
        report, _ = self.run_cycle(resume=True)
        next_at = report['continuation']['nextAt']
        self.assertTrue(next_at is None or facts.timestamp(next_at) > self.now, next_at)

    def test_environment_fault_delay_is_released_once_the_dependency_exists(self):
        self.inject = CycleIsolationTests.inject.__get__(self)
        self.inject('image', TypeError('first defect'))
        self.run_cycle()
        reading = self.reading()
        reading['failure']['exceptionClass'] = 'ModuleNotFoundError'
        reading['stage'] = 'held'
        self.reopen()
        self.client = cycle.CycleTests.client.__get__(self)
        calls = self.usage.calls
        report, _ = self.run_cycle(resume=True)
        self.assertEqual(self.reading()['stage'], 'done')
        self.assertNotIn('failure', self.reading())
        self.assertEqual(self.usage.calls, calls + 1)
        self.assertEqual(len(set(self.usage.requests)), len(self.usage.requests))


class RealTransportRetakeTests(IsolationBase):
    """Real SourceClient + SharedSource ledger, JPEG bytes and the encrypted cache."""

    def setUp(self):
        super().setUp()
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (900, 1200), 'white').save(buffer, format='JPEG', quality=85)
        self.real_jpeg = buffer.getvalue()
        self.ledger = collector._module('source-state.py', 'isolation_source_state')
        self.ledger_path = Path(self.temp.name) / 'source-usage.json'
        self.ledger.atomic_json(self.ledger_path, self.ledger.baseline_state(
            {'budgets': {}, 'paused': None}, source_hash=facts.digest(b'isolation-baseline'), at=base.NOW))
        self.payloads[self.first_id] = base.payload(mime='jpg')
        self.http = {'searches': 0, 'posts': 0, 'images': 0}

    def response(self, raw, mime):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = 200
        response.headers = {'Content-Type': mime}
        response.read.side_effect = io.BytesIO(raw).read
        return response

    def opener(self):
        opener = mock.Mock()

        def open_url(request, timeout):
            url = request.full_url
            if 'search.yahoo.co.jp' in url:
                self.http['searches'] += 1
                handle = url.split('id%3A', 1)[1].split('&', 1)[0]
                return self.response(self.documents[handle].encode(), 'text/html')
            if 'cdn.syndication.twimg.com' in url:
                self.http['posts'] += 1
                tid = url.split('id=', 1)[1].split('&', 1)[0]
                return self.response(json.dumps(self.payloads[tid]).encode(), 'application/json')
            self.http['images'] += 1
            if url.endswith('.jpg') or 'format=jpg' in url:
                return self.response(self.real_jpeg, 'image/jpeg')
            return self.response(base.png(), 'image/png')
        opener.open.side_effect = open_url
        return opener

    def run_real(self, run_id, *, resume=False, cycle_id=None, fail_put=False):
        opener = self.opener()

        def advance(seconds):
            self.now += dt.timedelta(seconds=seconds)
        with self.ledger.SharedSource(self.ledger_path, run_id=run_id, component='schedule',
                                      clock=self.clock, sleep=advance) as shared:
            factory = lambda: collector.SourceClient(shared, clock=self.clock, opener=opener, chunk_images=True)
            put = self.cache.put
            calls = {'count': 0}

            def put_once(key, source, text, images, **kwargs):
                if fail_put and images and calls['count'] == 0 and source['id'] == self.first_id:
                    calls['count'] += 1
                    raise TypeError('private-detail')
                return put(key, source, text, images, **kwargs)
            with mock.patch.object(self.cache, 'put', side_effect=put_once):
                report, code = collector.collect_cycle(
                    self.state, base.SCHEDULE, shared, self.analyzer, factory, clock=self.clock,
                    save=lambda: facts.validate_state(self.state), registry=self.registry,
                    existing_bindings={}, cache=self.cache, resume=resume, cycle_id=cycle_id)
            receipts = copy.deepcopy(shared.state['receipts'])
        return report, receipts

    def test_crash_after_image_200_keeps_receipt_and_retakes_once_under_new_run_id(self):
        report, receipts = self.run_real('101-1', fail_put=True)
        self.assertIn(failure_facts.UNEXPECTED_REASON, report['reasons'])
        first_images = [row for row in receipts.values()
                        if row['kind'] == 'images' and row['runId'] == '101-1' and row['status'] == 'ok']
        self.assertEqual(len(first_images), 2)  # both people; one of them lost after HTTP 200
        self.assertEqual(self.reading()['stage'], 'fetch')
        self.assertEqual(self.usage.calls, 1)
        self.assertIn('新人', {row['name'] for row in self.state['schedules']})
        before = copy.deepcopy(self.http)
        # Same accounting run: the issued image URL is refused, never fetched twice.
        self.now += dt.timedelta(hours=7)
        self.reopen()
        _, same = self.run_real('101-1', resume=True, cycle_id='101-1')
        self.assertEqual(self.http, before)
        self.assertEqual(same, receipts)
        self.assertNotEqual(self.reading()['stage'], 'done')
        # An interrupted cohort rebased to a new run ID re-acquires only the lost image.
        self.now += dt.timedelta(hours=7)
        self.reopen()
        self.state['collection']['chainId'] = '202-1'
        report, after = self.run_real('202-1', resume=True, cycle_id='202-1')
        self.assertEqual(self.http['images'], before['images'] + 1)
        self.assertEqual((self.http['searches'], self.http['posts']), (before['searches'], before['posts']))
        self.assertEqual({key: after[key] for key in receipts}, receipts)
        new = [row for key, row in after.items() if key not in receipts]
        self.assertEqual([(row['kind'], row['runId'], row['status']) for row in new],
                         [('images', '202-1', 'ok')])
        self.assertEqual(self.reading()['stage'], 'done')
        self.assertEqual({row['name'] for row in self.state['schedules']}, {'あむ', '新人'})
        self.assertEqual(self.usage.calls, 2)
        self.assertEqual(len(set(self.usage.requests)), 2)
        self.assertEqual(report['diagnostics'], [])


if __name__ == '__main__':
    unittest.main()
