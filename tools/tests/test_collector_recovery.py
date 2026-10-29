"""Recovery regressions: contained component failures, rebased half-month cohorts,
bounded official backfill, content-free child diagnostics and the alert issue job.

Only local fake Git remotes and offline clients are used; no live HTTP or Azure.
"""
import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import test_cloud_collection as legacy
import test_collect_personal_shifts as personal_fixture
import test_collection_routing as routing_fixture
import test_half_month_cloud as half_cloud


cloud, collector = legacy.cloud, legacy.collector
hcloud, hcollector = half_cloud.cloud, half_cloud.collector
personal = personal_fixture.personal
SECRET = 'github_pat_RECOVERY_SENTINEL_NEVER_EXPOSED'
PRIVATE_PATH = '/home/runner/work/private/state.json'


class ChildFactsTests(unittest.TestCase):
    def test_stdout_reason_class_and_frame_only(self):
        process = SimpleNamespace(returncode=1, stdout=(
            'noise\n' + json.dumps({
                'reason': 'schedule_infrastructure_failed', 'exceptionClass': 'TypeError',
                'location': 'collect-half-month-schedules.py:collect_cycle:1463',
                'message': SECRET + ' ' + PRIVATE_PATH, 'extra': 'source text'})).encode(),
            stderr=('Traceback\n  File "' + PRIVATE_PATH + '", line 9, in f\nValueError: ' + SECRET).encode())
        facts = cloud.child_failure_facts(process, 'schedule')
        self.assertEqual(facts, {'component': 'schedule', 'exitCode': 1,
                                 'reason': 'schedule_infrastructure_failed', 'exceptionClass': 'TypeError',
                                 'location': 'collect-half-month-schedules.py:collect_cycle:1463'})

    def test_uncaught_traceback_keeps_last_class_name_and_tools_frame(self):
        stderr = '\n'.join([
            'Traceback (most recent call last):',
            '  File "/opt/hostedtoolcache/Python/lib/python3.12/runpy.py", line 88, in _run_code',
            '  File "/home/runner/work/repo/repo/tools/collect-half-month-schedules.py", line 1463, in collect_cycle',
            '  File "/home/runner/work/repo/repo/tools/schedule-azure.py", line 210, in probe_image',
            '  File "/usr/lib/python3/dist-packages/PIL/Image.py", line 3300, in open',
            'struct.error: unpack requires a buffer of 4 bytes ' + SECRET])
        process = SimpleNamespace(returncode=-9, stdout=b'not json ' + SECRET.encode(), stderr=stderr.encode())
        facts = cloud.child_failure_facts(process, 'schedule')
        self.assertEqual(facts['exceptionClass'], 'error')
        self.assertEqual(facts['location'], 'schedule-azure.py:probe_image:210')
        self.assertNotIn(SECRET, json.dumps(facts))
        self.assertNotIn('/home/runner', json.dumps(facts))

    def test_unsafe_tokens_are_dropped_and_report_rows_are_strict(self):
        process = SimpleNamespace(returncode=1, stdout=json.dumps({
            'reason': 'Bad Reason ' + SECRET, 'exceptionClass': 'x.y', 'location': PRIVATE_PATH}).encode(),
            stderr=b'')
        self.assertEqual(cloud.child_failure_facts(process, 'personal'), {'component': 'personal', 'exitCode': 1})
        good = {'unexpectedFailures': [{'id': '2096252018260062487', 'stage': 'post',
                                        'exceptionClass': 'KeyError', 'location': 'collect-shifts.py:collect:1100'}]}
        self.assertEqual(cloud.unexpected_failures(good, 'official')[0]['exceptionClass'], 'KeyError')
        for row in ({'id': '1', 'message': SECRET}, {'exceptionClass': 'Key Error'},
                    {'location': PRIVATE_PATH}, {'id': 'abc'}):
            with self.subTest(row=row), self.assertRaises(cloud.CloudError):
                cloud.unexpected_failures({'unexpectedFailures': [row]}, 'official')


class CandidateIsolationTests(unittest.TestCase):
    def test_official_post_defect_is_pending_and_other_post_is_saved(self):
        defect = legacy.OTHER
        client = legacy.OfflineClient(ids=(legacy.TID, defect), posts={
            legacy.TID: legacy.payload(legacy.TID), defect: TypeError('private-detail ' + SECRET)})
        state, report, code = collector.collect(
            collector.empty_snapshot(), set(), client, dt.date(2026, 9, 4), dt.date(2026, 9, 5), 20,
            clock=lambda: legacy.NOW)
        self.assertEqual(code, 2)
        self.assertEqual([post['id'] for post in state['posts']], [legacy.TID])
        pending = {item['id']: item for item in state['pending']}
        self.assertEqual(pending[defect]['reason'], 'candidate_unexpected_error')
        self.assertEqual(report['unexpectedFailures'][0]['id'], defect)
        self.assertEqual(report['unexpectedFailures'][0]['exceptionClass'], 'TypeError')
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn('private-detail', json.dumps(state))

    def test_official_search_defect_keeps_other_query_and_fatal_storage_stops(self):
        class Client(legacy.OfflineClient):
            def search(self, url):
                if url == collector.SEARCH_URLS[0]:
                    raise KeyError('private-detail')
                return super().search(url)
        state, report, _ = collector.collect(
            collector.empty_snapshot(), set(), Client(), dt.date(2026, 9, 4), dt.date(2026, 9, 5), 20,
            clock=lambda: legacy.NOW)
        self.assertEqual(report['sources'][0]['reason'], 'candidate_unexpected_error')
        self.assertEqual([post['id'] for post in state['posts']], [legacy.TID])
        for error in (OSError('disk'), ValueError('invalid_ai_usage'), MemoryError()):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                collector.collect(collector.empty_snapshot(), set(),
                                  legacy.OfflineClient(posts={legacy.TID: error}),
                                  dt.date(2026, 9, 4), dt.date(2026, 9, 5), 20, clock=lambda: legacy.NOW)

    def test_personal_post_defect_is_isolated_and_other_person_is_saved(self):
        fx = personal_fixture.StateTests(methodName='runTest')
        self.addCleanup(fx.doCleanups)
        fx.setUp()
        other_tid, other_uid = str(int(personal_fixture.TID) + 7), '1180156105181159499'
        rarako = personal_fixture.RARAKO
        entries = [personal_fixture.candidate(),
                   personal_fixture.candidate(tid=other_tid, target=rarako, uid=other_uid)]
        payloads = {personal_fixture.TID: TypeError('private-detail ' + SECRET),
                    other_tid: personal_fixture.post('9月6日\n1号店昼', tid=other_tid, target=rarako, uid=other_uid)}
        durable = fx.durable()
        durable.preflight()
        client = fx.fake_client(durable, entries=entries, payloads=payloads)
        report, code = personal.collect(fx.state, durable, client, fx.targets, personal_fixture.DATE,
                                        durable.caps['searches'], durable.caps['posts'],
                                        clock=durable.clock, roster=fx.schedule['roster'])
        self.assertEqual(code, 2)
        self.assertEqual([post['name'] for post in fx.state['posts']], ['ららこ'])
        pending = {item['id']: item for item in fx.state['pending']}
        self.assertEqual(pending[personal_fixture.TID]['reason'], 'candidate_unexpected_error')
        self.assertEqual(pending[personal_fixture.TID]['attempts'], 1)
        self.assertEqual(report['unexpectedFailures'][0]['exceptionClass'], 'TypeError')
        self.assertNotIn(SECRET, json.dumps(report, ensure_ascii=False))
        personal.read_state(fx.snapshot)
        for error in (OSError('disk'), ValueError('source_usage_save_failed')):
            with self.subTest(error=str(error)), self.assertRaises(type(error)):
                state = personal.empty_state()
                durable = fx.durable(state)
                durable.preflight()
                personal.collect(state, durable, fx.fake_client(
                    durable, payloads={personal_fixture.TID: error}), fx.targets, personal_fixture.DATE,
                    durable.caps['searches'], durable.caps['posts'], clock=durable.clock,
                    roster=fx.schedule['roster'])


class OfficialCloudRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = legacy.CloudTests(methodName='runTest')
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.fx.seed_branch()
        clock = mock.patch.object(collector, 'utc_now', side_effect=lambda: legacy.NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def offline(self, client, *, code=None, child=None, corrupt=False, calls=None):
        def invoke(root, state, report, environment, correction_date=None, correction_to=None):
            argv = ['--once', '--days', '2', '--max-posts', '20',
                    '--snapshot', str(state / cloud.SNAPSHOT), '--report', str(report)]
            if correction_date is not None:
                argv += ['--correction-date', correction_date.isoformat()]
            if correction_to is not None:
                argv += ['--correction-to', correction_to.isoformat()]
            if calls is not None:
                calls.append((correction_date, correction_to))
            with contextlib.redirect_stdout(io.StringIO()):
                result = collector.run(collector.argument_parser().parse_args(argv),
                                       curated=self.fx.root / 'tools' / 'data' / 'shifts.csv',
                                       client=client(correction_date), clock=lambda: legacy.NOW)
            if corrupt:
                (state / cloud.SNAPSHOT).write_text('{"private": "' + SECRET + '"}', encoding='utf-8')
            if child is not None:
                report.with_name(report.stem + '.child.json').write_text(json.dumps(child), encoding='utf-8')
            return result if code is None else code
        return invoke

    def run_cloud(self, invoke):
        with mock.patch.object(cloud, 'invoke_collector', side_effect=invoke):
            return cloud.orchestrate(self.fx.args, root=self.fx.root,
                                     environment=self.fx.environment, collector=collector)

    def test_validated_child_failure_saves_facts_and_releases_its_own_lease(self):
        child = {'component': 'official', 'exitCode': 1, 'exceptionClass': 'TypeError',
                 'location': 'collect-shifts.py:collect:1100'}
        result = self.run_cloud(self.offline(lambda _: legacy.OfflineClient(), code=1, child=child))
        self.assertEqual(result['persistenceStatus'], 'saved')
        self.assertEqual((result['officialCollectionStatus'], result['officialCollectionCode']), ('unavailable', 3))
        self.assertEqual(result['componentFailures'], [{
            'component': 'official', 'exitCode': 1, 'exceptionClass': 'TypeError',
            'location': 'collect-shifts.py:collect:1100'}])
        self.assertNotIn(cloud.LEASE, self.fx.remote_names())
        self.assertEqual([post['id'] for post in self.fx.remote_json(cloud.SNAPSHOT)[0]['posts']], [legacy.TID])
        output = io.StringIO()
        summary = self.fx.base / 'summary.md'
        outputs = self.fx.base / 'outputs.txt'
        with contextlib.redirect_stdout(output):
            cloud.emit(result, {'GITHUB_STEP_SUMMARY': str(summary), 'GITHUB_OUTPUT': str(outputs)})
        self.assertIn('exceptionClass=TypeError', summary.read_text(encoding='utf-8'))
        self.assertIn('attention=true', outputs.read_text(encoding='utf-8'))
        self.assertIn('failedComponents=official', outputs.read_text(encoding='utf-8'))
        self.assertIn('::error::', output.getvalue())

    def test_unverifiable_child_state_keeps_the_lease_fail_closed(self):
        with self.assertRaises(cloud.CloudError):
            self.run_cloud(self.offline(lambda _: legacy.OfflineClient(), code=1, corrupt=True,
                                  child={'component': 'official', 'exitCode': 1}))
        self.assertIn(cloud.LEASE, self.fx.remote_names())

    def test_collector_reported_storage_fault_keeps_the_lease(self):
        with self.assertRaisesRegex(cloud.CloudError, 'collector_local_failure') as caught:
            self.run_cloud(self.offline(lambda _: legacy.OfflineClient(), code=4, child={
                'component': 'official', 'exitCode': 4, 'reason': 'local_io_error'}))
        self.assertEqual(caught.exception.diagnostics[0]['reason'], 'local_io_error')
        self.assertIn(cloud.LEASE, self.fx.remote_names())

    def test_bounded_correction_backfill_is_one_timeline_search_filtered_locally(self):
        day_ids = {dt.date(2026, 9, 3): legacy.make_id('2026-09-03T09:00:00Z'),
                   dt.date(2026, 9, 4): legacy.make_id('2026-09-04T09:00:00Z')}
        outside = legacy.make_id('2026-09-01T09:00:00Z')
        searches = []

        def client(day):
            ids = (*day_ids.values(), outside) if day else (legacy.TID,)
            value = legacy.OfflineClient(ids=ids)
            original = value.search
            value.search = lambda url: (searches.append(url), original(url))[1]
            return value
        calls = []
        self.fx.environment['OFFICIAL_CORRECTION_DATES'] = '2026-09-04,2026-09-03'
        result = self.run_cloud(self.offline(client, calls=calls))
        self.assertEqual(calls, [(dt.date(2026, 9, 3), dt.date(2026, 9, 4)), (None, None)])
        self.assertEqual(searches[0], collector.correction_search_url())
        self.assertNotIn('since', searches[0])
        self.assertNotIn('until', searches[0])
        self.assertEqual(result['officialCorrectionSearch']['searches'], 1)
        self.assertEqual([(row['date'], row['status']) for row in result['officialCorrections']],
                         [('2026-09-03', 'confirmed'), ('2026-09-04', 'confirmed')])
        posts = {post['id']: post['date'] for post in self.fx.remote_json(cloud.SNAPSHOT)[0]['posts']}
        self.assertEqual(posts, {day_ids[dt.date(2026, 9, 3)]: '2026-09-03',
                                 day_ids[dt.date(2026, 9, 4)]: '2026-09-04', legacy.TID: '2026-09-05'})
        self.assertNotIn(outside, posts)
        self.assertNotIn(cloud.LEASE, self.fx.remote_names())

    def test_zero_match_is_unconfirmed_never_an_empty_day(self):
        self.fx.environment['OFFICIAL_CORRECTION_DATES'] = '2026-09-03'
        result = self.run_cloud(self.offline(lambda day: legacy.OfflineClient(ids=() if day else (legacy.TID,))))
        self.assertEqual(result['officialCorrectionSearch']['discovered'], 0)
        self.assertEqual(result['officialCorrections'], [{'date': '2026-09-03', 'status': 'unconfirmed',
                                                          'day': 0, 'night': 0, 'notices': 0}])

    def test_collector_correction_query_has_no_date_operators(self):
        url = collector.correction_search_url(dt.date(2026, 10, 1))
        self.assertEqual(url, collector.correction_search_url())
        query = collector.urllib.parse.parse_qs(collector.urllib.parse.urlsplit(url).query)['p'][0]
        self.assertEqual(query, 'id:akibazettai')
        with self.assertRaisesRegex(ValueError, 'invalid_official_search_scope'):
            collector.collect(collector.empty_snapshot(), set(), legacy.OfflineClient(),
                              dt.date(2026, 8, 20), dt.date(2026, 9, 5), 20, clock=lambda: legacy.NOW,
                              search_urls=(url,))

    def test_correction_dates_are_manual_collect_only_and_bounded(self):
        for value, mode, scheduled in (
                ('2026-08-29', 'collect', False), ('2026-09-07', 'collect', False),
                ('2026-09-04,2026-09-04', 'collect', False),
                ('2026-09-01,2026-09-02,2026-09-03,2026-09-04', 'collect', False),
                ('2026-9-04', 'collect', False), ('2026-09-04', 'personal', False),
                ('2026-09-04', 'collect', True), ('2026-02-30', 'collect', False)):
            with self.subTest(value=value, mode=mode, scheduled=scheduled), \
                    self.assertRaisesRegex(cloud.CloudError, 'invalid_official_correction_dates'):
                cloud.official_correction_dates(value, mode, scheduled, collector)
        self.assertEqual(cloud.official_correction_dates('', 'collect', True, collector), [])
        self.assertEqual(cloud.official_correction_dates('2026-08-31', 'collect', False, collector),
                         [dt.date(2026, 8, 31)])


class HalfMonthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.h = half_cloud.HalfMonthCloudTests(methodName='runTest')
        self.addCleanup(self.h.doCleanups)
        self.h.setUp()
        self.fx = self.h.fx
        self.h.usage['money'] = self.h.ai.costs.empty()
        self.h.source_state = self.h.baseline()
        self.now = dt.datetime(2026, 10, 4, 22, tzinfo=hcloud.JST)
        cache = Path(tempfile.mkdtemp(prefix='recovery-cache-')) / '_private-evidence' / 'cache.bin'
        self.addCleanup(shutil.rmtree, cache.parent.parent, True)
        self.fx.personal_mode('schedule')
        self.fx.environment.update(
            GITHUB_EVENT_NAME='workflow_dispatch', DAILY_GUIDANCE_ENABLED='true',
            HALF_MONTH_SCHEDULE_ENABLED='false', RUN_CREATED_AT='2026-10-04T13:00:00Z',
            SCHEDULE_EVIDENCE_CACHE=str(cache), SCHEDULE_EVIDENCE_KEY='offline-not-a-real-key')

    def seed(self, reason):
        self.h.half_state['collection'] = {
            'chainId': '36771120722-1', 'names': ['あむ'], 'periods': [['2026-10-01', '2026-10-15']],
            'reason': reason, 'ready': False, 'cursor': 0, 'nextAt': None, 'serviceDate': '2026-10-01'}
        self.h.half.validate_state(self.h.half_state, private=True)
        self.h.seed()

    def run_half(self, *, code=2, in_flight=False):
        seen = []

        def invoke(root, state, report, environment):
            value = self.h.half.read_state(state / hcloud.HALF_MONTH)
            seen.append((environment['CLOUD_COLLECTION_RUN_ID'], environment['CLOUD_COLLECTION_HALF_CYCLE_ID'],
                         environment['CLOUD_COLLECTION_HALF_RESUME'], value['collection']['chainId']))
            if in_flight:
                with self.h.sources.SharedSource(
                        state / hcloud.SOURCE_USAGE, run_id=environment['CLOUD_COLLECTION_RUN_ID'],
                        component='schedule', clock=lambda: self.now, sleep=lambda _: None,
                        personal_path=state / hcloud.PERSONAL) as ledger:
                    ledger.reserve('images', 'https://pbs.twimg.com/media/IN_FLIGHT.jpg')
            if code == 2:
                value['collection'].update(reason='waiting', ready=False)
            value['lastRun'] = {'status': 'partial'}
            hcollector.atomic_json(state / hcloud.HALF_MONTH, value)
            hcollector.atomic_json(report, {
                'component': 'schedule', 'status': 'partial', 'exitCode': code,
                'requests': {'searches': 0, 'posts': 0, 'images': 0},
                'continuation': value['collection'], 'diagnostics': []})
            if code not in (0, 2, 3):
                report.with_name(report.stem + '.child.json').write_text(json.dumps({
                    'component': 'schedule', 'exitCode': code, 'exceptionClass': 'TypeError'}), encoding='utf-8')
            return code
        with mock.patch.object(hcollector, 'utc_now', side_effect=lambda: self.now), \
                mock.patch.object(self.h.ai.costs, 'sync', return_value=False), \
                mock.patch.object(hcloud, 'load_analysis_state', return_value=self.h.ai), \
                mock.patch.object(hcloud, 'invoke_collector', side_effect=AssertionError('official')), \
                mock.patch.object(hcloud, 'invoke_personal_collector', side_effect=AssertionError('personal')), \
                mock.patch.object(hcloud, 'invoke_half_month_collector', side_effect=invoke):
            result = self.h.invoke()
        return result, seen

    def test_interrupted_cohort_resumes_under_this_runs_accounting_id(self):
        self.seed('processing')
        result, seen = self.run_half()
        self.assertEqual(seen, [('12345-1', '12345-1', 'true', '12345-1')])
        self.assertEqual(result['halfMonthRebasedFrom'], '36771120722-1')
        saved = self.fx.remote_json(hcloud.HALF_MONTH)[0]['collection']
        self.assertEqual((saved['chainId'], saved['names'], saved['periods'], saved['cursor']),
                         ('12345-1', ['あむ'], [['2026-10-01', '2026-10-15']], 0))
        self.assertNotIn(hcloud.LEASE, self.fx.remote_names())

    def test_every_resumed_cohort_uses_this_runs_accounting_id(self):
        self.seed('waiting')
        result, seen = self.run_half()
        self.assertEqual(seen, [('12345-1', '12345-1', 'true', '12345-1')])
        self.assertEqual(result['halfMonthRebasedFrom'], '36771120722-1')

    def test_missing_isolated_dependency_stops_before_lease_or_child(self):
        self.seed('processing')
        head = self.fx.git(self.fx.remote, 'rev-parse', hcloud.REF)
        with mock.patch.object(hcloud, 'half_month_dependencies',
                               side_effect=hcloud.CloudError('half_month_dependencies_missing')), \
                self.assertRaisesRegex(hcloud.CloudError, 'half_month_dependencies_missing'):
            self.run_half()
        self.assertEqual(self.fx.git(self.fx.remote, 'rev-parse', hcloud.REF), head)
        self.assertNotIn(hcloud.LEASE, self.fx.remote_names())

    def test_dependency_probe_uses_the_childrens_isolated_flags(self):
        calls = []

        def record(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=1, stdout=b'', stderr=b'ModuleNotFoundError')
        with mock.patch.object(hcloud, 'child_process', side_effect=record), \
                self.assertRaisesRegex(hcloud.CloudError, 'half_month_dependencies_missing'):
            hcloud.half_month_dependencies(self.fx.root, {})
        self.assertEqual(calls[0][1:3], ['-I', '-B'])
        self.assertIn('PIL.Image', calls[0][-1])
        self.assertIn('cryptography', calls[0][-1])

    def test_validated_half_child_crash_releases_lease_and_stays_resumable(self):
        self.seed('processing')
        result, _ = self.run_half(code=1)
        self.assertEqual((result['halfMonthCollectionStatus'], result['halfMonthCollectionCode']), ('unavailable', 3))
        self.assertEqual(result['componentFailures'][0]['exceptionClass'], 'TypeError')
        self.assertNotIn(hcloud.LEASE, self.fx.remote_names())
        saved = self.fx.remote_json(hcloud.HALF_MONTH)[0]['collection']
        self.assertEqual((saved['reason'], saved['chainId']), ('processing', '12345-1'))
        source = self.fx.remote_json(hcloud.SOURCE_USAGE)[0]
        self.assertEqual(source['receipts'], self.h.source_state['receipts'])

    def test_image_host_stop_alone_does_not_freeze_text_continuation(self):
        state = {'collection': {
            'chainId': '12345-1', 'names': ['あむ'], 'periods': [['2026-10-01', '2026-10-15']],
            'reason': 'time_limit', 'ready': True, 'cursor': 0, 'nextAt': None}, 'pending': [], 'readings': {}}
        now = dt.datetime(2026, 10, 4, 15, 40, tzinfo=dt.timezone.utc)
        for host, expected in (('pbs.twimg.com', True), ('search.yahoo.co.jp', False),
                               ('cdn.syndication.twimg.com', False)):
            with self.subTest(host=host):
                source = copy.deepcopy(self.h.source_state)
                source['paused'] = {'host': host, 'reason': 'access_denied', 'httpStatus': 403,
                                    'at': '2026-10-04T15:35:18Z', 'retryAt': '2026-10-04T16:35:18Z'}
                self.assertEqual(hcloud.restored_half_month_continuation(
                    state, self.h.usage, source, now, hcollector), expected)

    def test_in_flight_request_is_never_self_released(self):
        self.seed('processing')
        with self.assertRaisesRegex(hcloud.CloudError, 'half_month_local_failure'):
            self.run_half(code=1, in_flight=True)
        self.assertIn(hcloud.LEASE, self.fx.remote_names())
        recovered = json.loads((self.fx.root / 'recovery' / hcloud.SOURCE_USAGE).read_bytes())
        self.assertEqual([row['status'] for row in recovered['receipts'].values()
                          if row['runId'] == '12345-1'], ['reserved'])

    def wait_state(self, next_at, checked, *, stage='fetch', confirmed=False, reason='waiting'):
        stamp = lambda value: hcollector.iso(value) if isinstance(value, dt.datetime) else value
        return {'collection': {'chainId': '12345-1', 'names': ['あむ'], 'periods': [['2026-10-01', '2026-10-15']],
                               'reason': reason, 'ready': reason == 'time_limit', 'cursor': 1,
                               'nextAt': stamp(next_at)},
                'pending': [{'id': '1', 'name': 'あむ', 'nextAttemptAt': stamp(next_at)}],
                'readings': {'k': {'sourceId': '1', 'cacheKey': 'k', 'stage': stage,
                                   'reason': 'paused', 'nextAt': stamp(next_at)}},
                'sources': {'k': {'source': {'media': [{'kind': 'photo'}]}}},
                'checkedAt': stamp(checked),
                'coverage': {'2026-10-01': {'あむ': {'confirmedIds': ['9'] if confirmed else []}}}}

    def image_stop(self, at, until):
        source = copy.deepcopy(self.h.source_state)
        source['paused'] = {'host': 'pbs.twimg.com', 'reason': 'access_denied', 'httpStatus': 403,
                            'at': hcollector.iso(at), 'retryAt': hcollector.iso(until)}
        return source

    def test_expired_wait_is_due_once_and_a_new_403_waits_a_full_hour_again(self):
        hour = dt.timedelta(hours=1)
        run = self.now - dt.timedelta(minutes=70)
        due = lambda state, source, now: hcloud.half_month_wait_due(state, self.h.usage, source, now, hcollector)
        # A run at `run` got a 403: the host and the candidate wait one hour.
        stopped = self.image_stop(run + dt.timedelta(minutes=2), run + dt.timedelta(minutes=2) + hour)
        state = self.wait_state(run + dt.timedelta(minutes=2) + hour, run)
        self.assertFalse(due(state, stopped, run + dt.timedelta(minutes=30)), 'no attempt inside the stop')
        self.assertTrue(due(state, stopped, self.now), 'due soon after the stop expired')
        # The dispatched run started at `now` and was denied again: a new full hour, no loop.
        again = self.wait_state(self.now + dt.timedelta(minutes=1) + hour, self.now)
        restopped = self.image_stop(self.now + dt.timedelta(minutes=1), self.now + dt.timedelta(minutes=1) + hour)
        for later in (dt.timedelta(minutes=10), dt.timedelta(minutes=40)):
            self.assertFalse(due(again, restopped, self.now + later))
        self.assertTrue(due(again, restopped, self.now + dt.timedelta(minutes=62)))

    def test_nothing_due_is_a_no_op(self):
        now, before = self.now, self.now - dt.timedelta(minutes=5)
        usage = copy.deepcopy(self.h.usage)
        usage['paused'] = {'reason': 'azure_auth_stopped', 'httpStatus': 401, 'at': '2026-10-04T12:00:00Z'}
        for label, state, ledger in (
                ('wait still running', self.wait_state(now + dt.timedelta(minutes=5), before), self.h.usage),
                # A later run already saw the expired wait and did not act on it: never re-dispatch.
                ('already checked after the wait', self.wait_state(before, now), self.h.usage),
                ('untimed candidate', self.wait_state(None, before - dt.timedelta(hours=1)), self.h.usage),
                ('person confirmed', self.wait_state(before, before - dt.timedelta(hours=1), confirmed=True),
                 self.h.usage),
                ('reading finished', self.wait_state(before, before - dt.timedelta(hours=1), stage='held'),
                 self.h.usage),
                ('time_limit uses the publication dispatch',
                 self.wait_state(before, before - dt.timedelta(hours=1), reason='time_limit'), self.h.usage),
                ('AI paused', self.wait_state(before, before - dt.timedelta(hours=1)), usage)):
            with self.subTest(label):
                self.assertFalse(hcloud.half_month_wait_due(state, ledger, self.h.source_state, now, hcollector))
        state = self.wait_state(before, before - dt.timedelta(hours=1))
        stopped = self.image_stop(before, now + dt.timedelta(minutes=30))
        self.assertFalse(hcloud.half_month_wait_due(state, self.h.usage, stopped, now, hcollector))
        state = self.wait_state(before, before - dt.timedelta(hours=1), stage='original')
        self.assertTrue(hcloud.half_month_wait_due(state, self.h.usage, stopped, now, hcollector),
                        'cached images need no HTTP, so an image-host stop does not block the AI reading')

    def due_check(self):
        self.fx.environment.update(HALF_MONTH_SCHEDULE_ENABLED='true')
        commands = []
        original = hcloud.child_process

        def record(argv, **kwargs):
            commands.append(argv)
            return original(argv, **kwargs)
        with mock.patch.object(hcollector, 'utc_now', side_effect=lambda: self.now), \
                mock.patch.object(hcloud, 'child_process', side_effect=record), \
                mock.patch.object(hcloud, 'invoke_half_month_collector', side_effect=AssertionError('child')):
            result = hcloud.half_month_due(root=self.fx.root, environment=self.fx.environment, collector=hcollector)
        self.assertFalse(any('push' in command or 'commit' in command for command in commands))
        return result

    def test_wait_check_is_read_only_and_reports_the_exact_state_commit(self):
        self.seed('waiting')
        head = self.fx.git(self.fx.remote, 'rev-parse', hcloud.REF).decode().strip()
        with mock.patch.object(hcloud, 'half_month_wait_due', return_value=True) as wait:
            result = self.due_check()
        self.assertEqual((result['halfMonthDue'], result['stateCommit'], result['reason']),
                         ('true', head, 'wait_expired'))
        self.assertEqual(wait.call_args.args[3], self.now)
        with mock.patch.object(hcloud, 'half_month_wait_due', return_value=False):
            result = self.due_check()
        self.assertEqual((result['halfMonthDue'], result['reason']), ('false', 'not_due'))
        self.assertEqual(self.fx.git(self.fx.remote, 'rev-parse', hcloud.REF).decode().strip(), head)
        self.assertNotIn(hcloud.LEASE, self.fx.remote_names())
        outputs, summary = self.fx.base / 'due-outputs.txt', self.fx.base / 'due-summary.md'
        with contextlib.redirect_stdout(io.StringIO()):
            hcloud.emit_half_month_due({'halfMonthDue': 'true', 'stateCommit': head, 'reason': 'wait_expired',
                                        'nextAt': None},
                                       {'GITHUB_OUTPUT': str(outputs), 'GITHUB_STEP_SUMMARY': str(summary)})
            hcloud.emit_half_month_due({'halfMonthDue': 'true', 'stateCommit': 'x; echo', 'reason': 'Bad Reason'},
                                       {'GITHUB_OUTPUT': str(outputs)})
        self.assertEqual(outputs.read_text(encoding='utf-8').splitlines(), [
            'halfMonthDue=true', 'stateCommit=' + head, 'reason=wait_expired',
            'halfMonthDue=false', 'stateCommit=', 'reason=invalid_reason'])
        self.assertIn('due=true', summary.read_text(encoding='utf-8'))

    def test_wait_check_never_dispatches_when_disabled_or_leased(self):
        self.seed('waiting')
        self.fx.environment.update(HALF_MONTH_SCHEDULE_ENABLED='false')
        result = hcloud.half_month_due(root=self.fx.root, environment=self.fx.environment, collector=hcollector)
        self.assertEqual((result['halfMonthDue'], result['reason']), ('false', 'half_month_disabled'))
        self.fx.bare_commit({hcloud.LEASE: self.fx.lease()})
        with mock.patch.object(hcloud, 'half_month_wait_due', return_value=True):
            result = self.due_check()
        self.assertEqual((result['halfMonthDue'], result['reason']), ('false', 'lease_present'))

    def test_checked_wait_continuation_runs_and_an_unexpired_one_is_refused(self):
        self.seed('waiting')
        head = self.fx.git(self.fx.remote, 'rev-parse', hcloud.REF).decode().strip()
        self.fx.environment['CONTINUATION_STATE'] = head
        with mock.patch.object(hcloud, 'half_month_wait_due', return_value=False), \
                self.assertRaisesRegex(hcloud.CloudError, 'continuation_not_ready'):
            self.run_half()
        self.assertEqual(self.fx.git(self.fx.remote, 'rev-parse', hcloud.REF).decode().strip(), head)
        with mock.patch.object(hcloud, 'half_month_wait_due', return_value=True):
            result, seen = self.run_half()
        self.assertEqual(seen, [('12345-1', '12345-1', 'true', '12345-1')])
        self.assertEqual(result['persistenceStatus'], 'saved')
        self.assertNotIn(hcloud.LEASE, self.fx.remote_names())
        # The same checkpoint cannot run twice: the state moved on, so it is a no-op.
        with mock.patch.object(hcloud, 'half_month_wait_due', return_value=True):
            result, seen = self.run_half()
        self.assertEqual((result['collectionStatus'], seen), ('stale-continuation', []))

    def test_image_reading_stall_needs_pending_images_and_24_hours_without_a_read(self):
        state = self.wait_state(None, None)
        receipt = lambda completed, image=True, reason='events': {
            'component': 'schedule', 'money': {'imageInput': image}, 'reason': reason,
            'issuedAt': hcollector.iso(completed - dt.timedelta(seconds=30)), 'completedAt': hcollector.iso(completed)}
        started = self.now - dt.timedelta(minutes=20)
        old = {'receipts': {'a': receipt(self.now - dt.timedelta(hours=25)),
                            'b': receipt(self.now - dt.timedelta(minutes=5), image=False),
                            'c': receipt(self.now - dt.timedelta(minutes=4), reason='azure_timeout')}}
        health = hcloud.half_month_image_health(state, old, self.now, started, hcollector)
        self.assertEqual(health, {'pendingImages': 1, 'stalled': True, 'readThisRun': False,
                                  'lastImageReadingAt': hcollector.iso(self.now - dt.timedelta(hours=25))})
        self.assertTrue(hcloud.half_month_image_health(state, {'receipts': {}}, self.now, started, hcollector)['stalled'])
        fresh = {'receipts': {**old['receipts'], 'd': receipt(self.now - dt.timedelta(minutes=3))}}
        health = hcloud.half_month_image_health(state, fresh, self.now, started, hcollector)
        self.assertEqual((health['stalled'], health['readThisRun']), (False, True))
        state['readings']['k']['stage'] = 'done'
        self.assertFalse(hcloud.half_month_image_health(state, old, self.now, started, hcollector)['stalled'])
        outputs, summary = self.fx.base / 'stall-outputs.txt', self.fx.base / 'stall-summary.md'
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            hcloud.emit({'halfMonthImageHealth': {'pendingImages': 3, 'lastImageReadingAt': None,
                                                  'stalled': True, 'readThisRun': False}},
                        {'GITHUB_OUTPUT': str(outputs), 'GITHUB_STEP_SUMMARY': str(summary)})
        self.assertIn('scheduleImageStall=true', outputs.read_text(encoding='utf-8'))
        self.assertIn('scheduleImageRead=false', outputs.read_text(encoding='utf-8'))
        self.assertIn('pending image-backed readings=3; last successful image reading=never',
                      summary.read_text(encoding='utf-8'))
        self.assertIn('::warning::Half-month image-backed candidates are pending', printed.getvalue())


class AlertWorkflowTests(unittest.TestCase):
    def script(self):
        block = routing_fixture.job_block('notify')
        raw = block.split('        run: |\n', 1)[1]
        return '\n'.join(line[10:] for line in raw.splitlines() if line.startswith('          '))

    def bash(self):
        if os.name == 'nt':
            git = shutil.which('git')
            candidates = [Path(git).parent.parent / 'bin' / 'bash.exe'] if git else []
            candidates.append(Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git' / 'bin' / 'bash.exe')
            bash = next((path for path in candidates if path.is_file()), None)
            if bash is None:
                self.skipTest('Git Bash is unavailable')
            return bash
        bash = shutil.which('bash')
        if not bash:
            self.skipTest('Bash is unavailable')
        return bash

    def test_guard_and_permissions(self):
        block = routing_fixture.job_block('notify')
        self.assertIn('needs: [collect, build, deploy]', block)
        self.assertIn('issues: write', block)
        self.assertNotIn('contents: write', block)
        condition = routing_fixture.job_condition('notify')
        self.assertTrue(routing_fixture.evaluate(condition, event='schedule', mode='', collect='failure'))
        for overrides in ({'event': 'pull_request'}, {'mode': 'probe'}, {'cancelled': True},
                          {'ref': 'refs/heads/feature'}, {'repository': 'someone/fork'}):
            with self.subTest(overrides=overrides):
                self.assertFalse(routing_fixture.evaluate(condition, **overrides))
        collect = routing_fixture.job_block('collect')
        for line in ('attention: ${{ steps.collection.outputs.attention }}',
                     'failed_components: ${{ steps.collection.outputs.failedComponents }}',
                     'OFFICIAL_CORRECTION_DATES: ${{ inputs.official_correction_dates }}'):
            self.assertIn(line, collect)

    def test_children_and_ci_use_the_same_isolated_venv(self):
        root = routing_fixture.ROOT / '.github' / 'actions'
        action = (root / 'collector-python' / 'action.yml').read_text(encoding='utf-8')
        self.assertIn('python -m venv "$RUNNER_TEMP/collector-venv"', action)
        self.assertIn('-I -B -c "import PIL.Image, cryptography', action)
        self.assertIn('>> "$GITHUB_PATH"', action)
        for forbidden in ('--user', 'PYTHONPATH', 'PYTHONUSERBASE', 'ENABLE_USER_SITE', 'sudo'):
            self.assertNotIn(forbidden, action)
        validate = (root / 'validate-site' / 'action.yml').read_text(encoding='utf-8')
        self.assertIn('uses: ./.github/actions/collector-python', validate)
        self.assertNotIn('pip install', validate)
        collect = routing_fixture.job_block('collect')
        install = collect.split('- name: Install declared image collection dependencies', 1)[1].split('- name:', 1)[0]
        self.assertIn("steps.route.outputs.halfMonthCacheRequired == 'true'", install)
        self.assertIn('uses: ./.github/actions/collector-python', install)
        self.assertNotIn('pip install', routing_fixture.WORKFLOW)

    def run_alert(self, values, open_number='', body=''):
        bash = self.bash()
        mock_gh = '''
gh() {
  echo "$*" >> "$GH_LOG"
  case "$1 $2" in
    "issue list") printf '%s\\n' "$OPEN";;
    "issue view") printf '%s\\n' "$ISSUE_BODY";;
  esac
}
'''
        with tempfile.TemporaryDirectory() as folder:
            environment = {**os.environ, 'GH_LOG': 'gh.txt', 'OPEN': open_number, 'ISSUE_BODY': body,
                           'BASH_ENV': '', 'RUN_URL': 'https://github.com/o/r/actions/runs/42',
                           'EVENT': 'schedule', 'COLLECT': 'success', 'BUILD': 'success',
                           'DEPLOY': 'success', 'ATTENTION': 'false', 'REASON': '', 'COMPONENTS': '',
                           'SCHEDULE_CAPACITY': '',
                           **{f'{name}_{field}': value for name in ('OFFICIAL', 'PERSONAL', 'SCHEDULE', 'ROSTER')
                              for field, value in (('STATUS', ''), ('CODE', '-1'), ('REASON', ''))}}
            environment.update(values)
            result = subprocess.run(
                [str(bash), '--noprofile', '--norc', '-e', '-o', 'pipefail', '-s'],
                input=mock_gh + self.script(), text=True, capture_output=True, cwd=folder,
                timeout=15, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(folder) / 'pwned').exists())
            path = Path(folder) / 'gh.txt'
            return path.read_text() if path.exists() else ''

    def state(self, *items):
        return '<!-- alert-state ' + ' '.join(items) + ' -->\nold body'

    def test_component_failure_closes_only_after_the_same_component_succeeds(self):
        open_schedule = self.state('schedule=schedule_infrastructure_failed:3:2026-10-18T19:30:00Z:39153171472')
        official_ok = {'OFFICIAL_STATUS': 'ok', 'OFFICIAL_CODE': '0'}
        personal_ok = {'PERSONAL_STATUS': 'partial', 'PERSONAL_CODE': '2'}
        for values in (official_ok, personal_ok, {**official_ok, **personal_ok},
                       {'SCHEDULE_STATUS': 'unavailable', 'SCHEDULE_CODE': '3'},
                       {'SCHEDULE_STATUS': 'ok', 'SCHEDULE_CODE': '0', 'DEPLOY': 'failure'}):
            with self.subTest(values=values):
                log = self.run_alert(values, '7', open_schedule)
                self.assertNotIn('issue close', log)
                self.assertNotIn('issue create', log)
        log = self.run_alert({'SCHEDULE_STATUS': 'partial', 'SCHEDULE_CODE': '2'}, '7', open_schedule)
        self.assertIn('issue close 7', log)
        # Another component still failing keeps the issue open with only that component listed.
        both = self.state('schedule=schedule_infrastructure_failed:3:2026-10-18T19:30:00Z:1',
                          'official=component_failure:1:2026-10-24T03:30:00Z:2')
        log = self.run_alert({'SCHEDULE_STATUS': 'ok', 'SCHEDULE_CODE': '0'}, '7', both)
        self.assertIn('issue edit 7', log)
        self.assertNotIn('issue close', log)
        self.assertIn('<!-- alert-state official=component_failure:1:2026-10-24T03:30:00Z:2 -->', log)
        self.assertNotIn('- schedule:', log)

    def test_repeated_cause_counts_consecutive_failures_and_keeps_first_time(self):
        open_schedule = self.state('schedule=schedule_infrastructure_failed:3:2026-10-18T19:30:00Z:39153171472')
        failing = {'ATTENTION': 'true', 'COMPONENTS': 'schedule', 'SCHEDULE_STATUS': 'unavailable',
                   'SCHEDULE_CODE': '3', 'SCHEDULE_REASON': 'schedule_infrastructure_failed'}
        log = self.run_alert(failing, '7', open_schedule)
        self.assertIn('issue edit 7', log)
        self.assertIn('schedule=schedule_infrastructure_failed:4:2026-10-18T19:30:00Z:42', log)
        self.assertIn('consecutive failures=4; first failed (UTC)=2026-10-18T19:30:00Z', log)
        self.assertIn('latest failed run: https://github.com/o/r/actions/runs/42', log)
        log = self.run_alert({**failing, 'SCHEDULE_REASON': 'reading_capacity'}, '7', open_schedule)
        self.assertRegex(log, r'schedule=reading_capacity:1:\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ:42')
        log = self.run_alert(failing)
        self.assertIn('issue create', log)
        self.assertIn('schedule=schedule_infrastructure_failed:1:', log)
        # Forged state tokens are ignored instead of reaching the body.
        log = self.run_alert(failing, '7', '<!-- alert-state schedule=$(touch pwned):1:x:1 -->')
        self.assertNotIn('$(touch', log)
        self.assertIn('schedule=schedule_infrastructure_failed:1:', log)

    def test_reached_queue_bound_is_alerted_until_a_schedule_run_without_it(self):
        capacity = {'SCHEDULE_STATUS': 'partial', 'SCHEDULE_CODE': '2', 'SCHEDULE_CAPACITY': 'queue_limit-reading_capacity'}
        log = self.run_alert(capacity)
        self.assertIn('issue create', log)
        self.assertIn('schedule=queue_limit:1:', log)
        open_state = self.state('schedule=queue_limit:1:2026-10-25T19:30:00Z:41')
        log = self.run_alert({'OFFICIAL_STATUS': 'ok', 'OFFICIAL_CODE': '0'}, '7', open_state)
        self.assertNotIn('issue close', log)
        log = self.run_alert({'SCHEDULE_STATUS': 'ok', 'SCHEDULE_CODE': '0'}, '7', open_state)
        self.assertIn('issue close 7', log)
        log = self.run_alert({**capacity, 'SCHEDULE_CAPACITY': 'x;$(touch pwned)'}, '', '')
        self.assertNotIn('issue create', log)

    def test_image_reading_stall_is_raised_and_closed_only_by_a_later_image_read(self):
        stalled = {'SCHEDULE_STATUS': 'partial', 'SCHEDULE_CODE': '2', 'SCHEDULE_IMAGE_STALL': 'true'}
        log = self.run_alert(stalled)
        self.assertIn('issue create', log)
        self.assertIn('schedule=image_reading_stalled:1:', log)
        open_state = self.state('schedule=image_reading_stalled:2:2026-10-26T15:30:00Z:41')
        log = self.run_alert(stalled, '7', open_state)
        self.assertIn('schedule=image_reading_stalled:3:2026-10-26T15:30:00Z:42', log)
        for values in ({'OFFICIAL_STATUS': 'ok', 'OFFICIAL_CODE': '0'},
                       {'SCHEDULE_STATUS': 'partial', 'SCHEDULE_CODE': '2', 'SCHEDULE_IMAGE_STALL': 'false'},
                       {'SCHEDULE_STATUS': 'ok', 'SCHEDULE_CODE': '0', 'SCHEDULE_IMAGE_READ': 'false'},
                       {'SCHEDULE_STATUS': 'ok', 'SCHEDULE_CODE': '0', 'SCHEDULE_IMAGE_READ': 'true;x'},
                       {'SCHEDULE_STATUS': 'ok', 'SCHEDULE_CODE': '0', 'SCHEDULE_IMAGE_READ': 'true',
                        'DEPLOY': 'failure'}):
            with self.subTest(values=values):
                log = self.run_alert(values, '7', open_state)
                self.assertNotIn('issue close', log)
                self.assertNotIn('issue create', log)
        log = self.run_alert({'SCHEDULE_STATUS': 'partial', 'SCHEDULE_CODE': '2', 'SCHEDULE_IMAGE_READ': 'true'},
                             '7', open_state)
        self.assertIn('issue close 7', log)
        # Other schedule causes keep their existing recovery rule.
        capacity = self.state('schedule=queue_limit:1:2026-10-25T19:30:00Z:41')
        self.assertIn('issue close 7', self.run_alert({'SCHEDULE_STATUS': 'ok', 'SCHEDULE_CODE': '0'}, '7', capacity))
        self.assertNotIn('issue create', self.run_alert({'SCHEDULE_STATUS': 'partial', 'SCHEDULE_CODE': '2',
                                                         'SCHEDULE_IMAGE_STALL': 'true;$(touch pwned)'}))

    def test_roster_failure_is_its_own_component_until_a_roster_run_succeeds(self):
        failing = {'ATTENTION': 'true', 'COMPONENTS': 'roster', 'ROSTER_STATUS': 'unavailable',
                   'ROSTER_CODE': '3', 'ROSTER_REASON': 'staff_page_structure_changed'}
        log = self.run_alert(failing)
        self.assertIn('issue create', log)
        self.assertIn('roster=staff_page_structure_changed:1:', log)
        open_state = self.state('roster=staff_page_structure_changed:1:2026-10-28T01:00:00Z:41')
        for values in ({'OFFICIAL_STATUS': 'ok', 'OFFICIAL_CODE': '0'},
                       {'ROSTER_STATUS': 'not-run', 'ROSTER_CODE': '-1'}):
            with self.subTest(values=values):
                log = self.run_alert(values, '7', open_state)
                self.assertNotIn('issue close', log)
        log = self.run_alert({'ROSTER_STATUS': 'no-new', 'ROSTER_CODE': '0'}, '7', open_state)
        self.assertIn('issue close 7', log)

    def test_single_issue_is_created_updated_closed_and_untrusted_tokens_dropped(self):
        cases = [
            ({'COLLECT': 'failure', 'REASON': 'unresolved_lease'}, '', 'issue create'),
            ({'COLLECT': 'success', 'ATTENTION': 'true', 'COMPONENTS': 'schedule'}, '7', 'issue edit 7'),
            ({'COLLECT': 'success'}, '7', 'issue close 7'),
            ({'COLLECT': 'skipped'}, '7', None),
            ({'COLLECT': 'failure', 'REASON': 'x $(touch pwned) ' + SECRET}, '', 'issue create')]
        for values, open_number, expected in cases:
            with self.subTest(values=values):
                log = self.run_alert(values, open_number)
                if expected:
                    self.assertIn(expected, log)
                else:
                    self.assertNotIn('issue create', log)
                    self.assertNotIn('issue edit', log)
                    self.assertNotIn('issue close', log)
                self.assertNotIn(SECRET, log)
                self.assertEqual(log.count('issue create') + log.count('issue edit'), 1 if expected and
                                 'close' not in expected else 0)


if __name__ == '__main__':
    unittest.main()
