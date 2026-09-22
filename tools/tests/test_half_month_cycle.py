"""Finite v3 cycle tests; synthetic sources, encrypted temp cache and mocked Luna."""
import base64
import copy
import datetime as dt
import email.utils
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import test_half_month_schedules as base
import test_schedule_reading_azure as reading_fixture


collector, facts, azure = base.collector, base.facts, base.azure
cache_module = collector._module('schedule-evidence-cache.py', 'cycle_test_cache')


class Usage:
    def __init__(self, clock):
        self.clock, self.deadline = clock, None
        self.limit, self.calls, self.requests = 100, 0, []

    def check(self):
        if self.deadline is not None and not self.deadline():
            raise azure.AnalysisFailure('azure_deadline')
        if self.calls >= self.limit:
            error = collector.Failure('azure_budget_exhausted')
            error.retry_at = self.clock() + dt.timedelta(minutes=1)
            raise error

    def reserve(self, key, identity, **kwargs):
        self.check()
        if key in self.requests:
            raise AssertionError('duplicate paid request')
        self.requests.append(key)
        self.calls += 1

    def issued(self, key):
        pass

    def finish(self, key, reason):
        pass


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='half-cycle-')
        self.addCleanup(self.temp.cleanup)
        self.now = base.NOW
        self.clock = lambda: self.now
        self.path = Path(self.temp.name) / '_private-evidence' / 'cache.bin'
        self.key = base64.b64encode(b'S' * 32).decode()
        self.cache = cache_module.EvidenceCache(self.path, self.key, clock=self.clock)
        self.state = facts.empty_state()
        self.registry = base.registry_fixture()
        self.documents = {base.TARGET['handle']: base.document(base.entry())}
        self.payloads = {base.post_id(): base.payload()}
        self.gets = {'searches': [], 'posts': [], 'images': []}
        self.source = mock.Mock(run_id='2001-1', state={'cooldowns': {}, 'receipts': {}})
        self.usage = Usage(self.clock)
        self.model = mock.Mock(identity={'model': facts.MODEL})
        self.model.structured.return_value = reading_fixture.result()
        self.analyzer = azure.AzureAnalyzer(self.usage, clock=self.clock, client=self.model)
        self.network = mock.patch('socket.socket', side_effect=AssertionError('live network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def client(self):
        client = mock.Mock()
        def search(handle):
            self.gets['searches'].append(handle)
            return self.documents[handle]
        def post(tid):
            self.gets['posts'].append(tid)
            payload = copy.deepcopy(self.payloads[tid])
            return payload, facts.digest(payload)
        def image(url):
            self.gets['images'].append(url)
            return {'bytes': bytearray(base.png()), 'mime': 'image/png'}
        client.search.side_effect = search
        client.post.side_effect = post
        client.image.side_effect = image
        return client

    def run_cycle(self, **kwargs):
        def save():
            facts.validate_state(self.state)
            json.dumps(self.state, ensure_ascii=False)
        return collector.collect_cycle(
            self.state, base.SCHEDULE, self.source, self.analyzer, self.client,
            clock=self.clock, save=save, registry=self.registry, existing_bindings={},
            cache=self.cache, **kwargs)

    def reopen(self):
        self.state = json.loads(json.dumps(self.state))
        self.cache = cache_module.EvidenceCache(self.path, self.key, clock=self.clock)
        self.analyzer = azure.AzureAnalyzer(self.usage, clock=self.clock, client=self.model)

    def test_readable_initial_one_call_and_confirmed_zero_search_next_day(self):
        report, code = self.run_cycle()
        self.assertEqual((code, report['analysisRequests']), (0, 1))
        self.assertEqual(len(self.state['schedules']), 1)
        self.assertEqual(self.state['coverage']['2026-09-01']['あむ']['nextCheckAt'], None)
        calls, gets = self.usage.calls, copy.deepcopy(self.gets)
        self.now += dt.timedelta(days=1)
        self.reopen()
        report, code = self.run_cycle()
        self.assertEqual(report['requests'], {'searches': 0, 'posts': 0, 'images': 0})
        self.assertEqual(report['analysisRequests'], 0)
        self.assertEqual((self.usage.calls, self.gets), (calls, gets))

    def test_same_run_false_negative_without_caption_reaches_tiles(self):
        self.payloads[base.post_id()]['text'] = 'synthetic portrait'
        responses = iter([
            {'classification': 'non_schedule', 'complete': True, 'periods': []},
            reading_fixture.result()])
        wires = []
        def response(messages, *args, **kwargs):
            wires.append(copy.deepcopy(messages))
            return next(responses)
        self.model.structured.side_effect = response
        report, _ = self.run_cycle()
        self.assertEqual(report['analysisRequests'], 2)
        self.assertEqual(len(self.gets['images']), 1)
        self.assertEqual(len(self.state['schedules']), 1)
        contents = wires[1][1]['content']
        self.assertEqual(len(contents), 5)
        mapping = json.loads(contents[0]['text'])['imageMap']
        self.assertTrue(all(view['originalIndex'] == 0 and view['kind'] == 'crop' for view in mapping))
        state_text = json.dumps(self.state, ensure_ascii=False)
        self.assertNotIn('synthetic portrait', state_text)
        self.assertNotIn('5日 昼', state_text)
        self.assertNotIn('pbs.twimg.com', state_text)
        self.assertNotIn(str(self.path), state_text)

    def test_budget_after_initial_resume_cached_detail_then_noop(self):
        self.usage.limit = 1
        self.model.structured.side_effect = [
            reading_fixture.result(complete=False),
            reading_fixture.result()]
        report, _ = self.run_cycle()
        self.assertEqual(report['reasons'], ['budget_wait'])
        progress = next(iter(self.state['readings'].values()))
        self.assertEqual(progress['stage'], 'detail')
        self.assertEqual(len(progress['attemptedVariants']), 1)
        gets = copy.deepcopy(self.gets)
        self.assertEqual(len(self.state['schedules']), 0)
        self.assertEqual(len(self.state['partialSchedules']), 1)
        self.now += dt.timedelta(minutes=2)
        self.usage.limit = 10
        self.reopen()
        self.source.check.side_effect = AssertionError('cached resume must not request source')
        report, _ = self.run_cycle(resume=True)
        self.assertEqual(self.gets, gets)
        self.assertEqual(report['analysisRequests'], 1)
        self.assertEqual(len(self.state['schedules']), 1)
        report, _ = self.run_cycle(resume=True)
        self.assertEqual(report['analysisRequests'], 0)
        self.assertEqual(self.gets, gets)
        self.assertEqual(len(set(self.usage.requests)), 2)

    def test_persistent_uncertainty_is_held_not_deleted_or_recharged(self):
        self.model.structured.return_value = {'classification': 'uncertain', 'complete': False, 'periods': []}
        self.run_cycle()
        self.assertEqual(len(self.state['pending']), 1)
        progress = next(iter(self.state['readings'].values()))
        self.assertEqual((progress['stage'], progress['reason']), ('held', 'reading_uncertain'))
        self.assertFalse(self.state['coverage']['2026-09-01']['あむ']['confirmedIds'])
        before = copy.deepcopy(self.gets)
        self.now += dt.timedelta(days=1)
        self.reopen()
        self.run_cycle()
        self.assertEqual(self.usage.calls, 2)
        self.assertEqual(self.gets, before)

    def test_detail_cannot_confirm_after_omitting_an_already_read_date(self):
        self.model.structured.side_effect = [
            reading_fixture.result(
                reading_fixture.row(), reading_fixture.row(7, weekday='月'), complete=False),
            reading_fixture.result(reading_fixture.row(7, weekday='月'))]
        report, _ = self.run_cycle()
        self.assertEqual(self.state['schedules'], [])
        self.assertEqual([row['date'] for row in self.state['partialSchedules'][0]['days']],
                         ['2026-09-05', '2026-09-07'])
        self.assertEqual(report['unresolvedCount'], 1)
        self.assertEqual(next(iter(self.state['readings'].values()))['reason'], 'reading_partial')

    def test_partial_image_fetch_resumes_only_missing_image(self):
        self.payloads[base.post_id()] = base.payload(photos=2)
        original_factory = self.client
        failures = [False]
        def factory():
            client = original_factory()
            normal = client.image.side_effect
            def image(url):
                if len(self.gets['images']) == 1 and not failures[0]:
                    failures[0] = True
                    raise collector.Failure('network_error')
                return normal(url)
            client.image.side_effect = image
            return client
        self.client = factory
        report, _ = self.run_cycle()
        self.assertEqual(report['reasons'], ['image_fetch_failed'])
        self.assertEqual(self.usage.calls, 0)
        diagnostic = report['diagnostics'][0]
        self.assertEqual(diagnostic, {
            'name': 'あむ', 'postId': base.post_id(), 'postUrl': facts.public_url('amu_zettai', base.post_id()),
            'imageIndex': 1, 'failedAt': facts.stamp(self.now), 'host': 'pbs.twimg.com',
            'httpStatus': None, 'retryAt': facts.stamp(self.now + dt.timedelta(hours=6)),
            'stage': 'fetch', 'nextStage': 'fetch', 'reason': 'image_fetch_failed'})
        self.assertNotIn('https://pbs.twimg.com', json.dumps(report['diagnostics']))
        self.assertNotIn('readings', facts.public_state(self.state))
        bad = copy.deepcopy(self.state)
        next(iter(bad['readings'].values()))['failure']['raw'] = 'not allowed'
        with self.assertRaises(ValueError):
            facts.validate_state(bad)
        self.reopen()
        waiting, _ = self.run_cycle(resume=True)
        self.assertEqual(waiting['diagnostics'], [diagnostic])
        self.now += dt.timedelta(hours=7)
        self.reopen()
        self.model.structured.return_value = reading_fixture.result(indexes=(0, 1))
        recovered, _ = self.run_cycle(resume=True)
        self.assertEqual(recovered['diagnostics'], [])
        self.assertEqual(len(self.gets['posts']), 1)
        self.assertEqual(len(self.gets['images']), 2)
        self.assertEqual(len(set(self.gets['images'])), 2)
        self.assertEqual(self.usage.calls, 1)

    def test_multi_person_finite_snapshot_no_one_search_post_cap(self):
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
        report, _ = self.run_cycle()
        self.assertEqual(report['requests'], {'searches': 2, 'posts': 2, 'images': 2})
        self.assertEqual(report['analysisRequests'], 2)
        self.assertEqual({row['name'] for row in self.state['schedules']}, {'あむ', '新人'})

    def test_five_initial_image_negatives_all_reach_same_run_reread(self):
        for number, name in enumerate(('新甲', '新乙', '新丙', '新丁'), 2):
            handle, uid = f'new_{number}', str(int(base.AUTHOR) + number)
            self.registry['members'].append(collector.members.new_member(
                name, 'https://x.com/' + handle, base.NOW, member_id='m-' + str(number) * 32))
            entry = {**base.entry(suffix=number, handle=handle), 'userId': uid}
            payload = base.payload(suffix=number)
            payload['user'] = {'id_str': uid, 'screen_name': handle}
            payload['mediaDetails'][0]['source_user_id'] = int(uid)
            payload['mediaDetails'][0]['expanded_url'] = f'https://x.com/{handle}/status/{entry["id"]}/photo/1'
            self.documents[handle], self.payloads[entry['id']] = base.document(entry), payload
        self.model.structured.side_effect = [
            response for _ in range(5) for response in (
                {'classification': 'non_schedule', 'complete': True, 'periods': []},
                reading_fixture.result())]
        report, _ = self.run_cycle()
        self.assertEqual(report['analysisRequests'], 10)
        self.assertEqual(report['requests'], {'searches': 5, 'posts': 5, 'images': 5})
        self.assertEqual(report['unresolvedCount'], 0)
        self.assertEqual(len(self.state['schedules']), 5)

    def test_joined_active_member_without_account_remains_explicitly_unresolved(self):
        self.run_cycle()
        before = copy.deepcopy(self.gets)
        self.registry['members'].append(collector.members.new_member(
            '新人', None, base.NOW, member_id='m-' + '2' * 32))
        self.now += dt.timedelta(days=1)
        report, _ = self.run_cycle()
        self.assertEqual(self.gets, before)
        self.assertIn('新人', report['continuation']['names'])
        self.assertEqual(report['unresolvedCount'], 1)
        self.assertEqual(self.state['coverage']['2026-09-01']['新人']['reason'], 'account_unknown')
        self.assertEqual(report['nextStage'], 'waiting')

    def test_full_month_both_halves_and_date_only_not_discarded(self):
        self.model.structured.return_value = reading_fixture.result(
            reading_fixture.row(shifts=(), transcription='5日'),
            reading_fixture.row(20, shifts=(), weekday='日', transcription='20日'), half='full')
        self.run_cycle()
        self.assertEqual(len(self.state['schedules']), 2)
        effective = facts.effective_schedule({}, self.state)
        self.assertEqual(effective['2026-09-05']['unassigned'][0]['name'], 'あむ')
        self.assertEqual(effective['2026-09-20']['unassigned'][0]['name'], 'あむ')

    def test_display_text_body_priority_and_keywordless_photo_candidate(self):
        entry = {**base.entry(), 'text': 'not used', 'displayTextBody': [{'text': '9月'}, {'text': '予定'}]}
        rows, _ = collector.discover(base.document(entry), {'あむ': base.TARGET}, base.NOW)
        self.assertEqual(rows[0]['priority'], 0)
        entry['displayTextBody'] = 'portrait without keywords'
        rows, _ = collector.discover(base.document(entry), {'あむ': base.TARGET}, base.NOW)
        self.assertEqual((len(rows), rows[0]['priority']), (1, 1))
        self.assertNotIn('displayTextBody', rows[0])

    def test_reply_parent_id_and_author_verified_before_amendment(self):
        entry = {**base.entry(), 'isReply': True}
        self.documents[base.TARGET['handle']] = base.document(entry)
        parent = base.payload(created=base.CREATED - dt.timedelta(days=1))
        tid = parent['id_str']
        self.payloads[tid] = parent
        payload = self.payloads[base.post_id()]
        payload.update(in_reply_to_status_id_str=tid, in_reply_to_user_id_str=base.AUTHOR)
        self.model.structured.return_value = reading_fixture.result(
            reading_fixture.row(transcription='5日 昼追加', operation='add'))
        self.run_cycle()
        self.assertEqual(len(self.gets['posts']), 2)
        self.assertEqual(self.state['schedules'], [])
        partial = self.state['partialSchedules'][0]
        self.assertEqual((partial['sourceKind'], partial['replyToId']), ('own-reply', tid))
        self.assertFalse(partial['reading']['complete'])
        self.assertEqual(self.usage.calls, 1)
        self.assertEqual(next(iter(self.state['readings'].values()))['stage'], 'done')

    def test_wrong_reply_author_never_gets_parent_or_calls_model(self):
        payload = self.payloads[base.post_id()]
        payload.update(in_reply_to_status_id_str=base.post_id(base.CREATED - dt.timedelta(days=1)),
                       in_reply_to_user_id_str='12345')
        report, _ = self.run_cycle()
        self.assertEqual(report['reasons'], ['identity_unknown'])
        self.assertEqual(len(self.gets['posts']), 1)
        self.assertEqual(self.usage.calls, 0)

    def test_missing_cache_never_fetches_or_calls_ai(self):
        self.cache = None
        report, code = self.run_cycle()
        self.assertEqual((code, report['reasons']), (2, ['image_cache_unconfigured']))
        self.assertEqual(self.gets, {'searches': [], 'posts': [], 'images': []})
        self.assertEqual(self.usage.calls, 0)

    def test_runtime_interruption_has_finite_ready_resume_without_discovery(self):
        responses = iter([reading_fixture.result(complete=False), reading_fixture.result()])
        def response(*args, **kwargs):
            self.now += dt.timedelta(seconds=3)
            return next(responses)
        self.model.structured.side_effect = response
        report, _ = self.run_cycle(runtime_seconds=2)
        self.assertTrue(report['continuation']['ready'])
        self.assertEqual(report['reasons'], ['time_limit'])
        before = copy.deepcopy(self.gets)
        chain = report['continuation']['chainId']
        self.state['collection'].pop('serviceDate')
        self.now += dt.timedelta(minutes=2)
        self.reopen()
        report, _ = self.run_cycle(resume=True, cycle_id=chain)
        self.assertFalse(report['continuation']['ready'])
        self.assertNotIn('serviceDate', report['continuation'])
        self.assertEqual(self.gets, before)
        self.assertEqual(len(self.state['schedules']), 1)

    def test_resume_keeps_original_periods_cohort_and_real_clock_across_calendar_boundaries(self):
        for local_day in (7, 12, 15, 30):
            with self.subTest(local_day=local_day):
                self.now = dt.datetime(2026, 9, local_day, 14, 59, tzinfo=facts.UTC)
                self.state = facts.empty_state()
                self.registry = base.registry_fixture()
                self.path = Path(self.temp.name) / str(local_day) / '_private-evidence' / 'cache.bin'
                self.cache = cache_module.EvidenceCache(self.path, self.key, clock=self.clock)
                self.usage = Usage(self.clock)
                self.analyzer = azure.AzureAnalyzer(self.usage, clock=self.clock, client=self.model)
                self.gets = {'searches': [], 'posts': [], 'images': []}
                created = base.CREATED if local_day == 30 else self.now - dt.timedelta(days=1)
                self.documents = {base.TARGET['handle']: base.document(base.entry(created))}
                self.payloads = {base.post_id(created): base.payload(created)}
                response = reading_fixture.result(
                    reading_fixture.row(), reading_fixture.row(20, weekday='日', transcription='20日 昼'),
                    half='full')
                seen_times = []
                def model_response(*args, **kwargs):
                    seen_times.append(self.now)
                    self.now += dt.timedelta(seconds=3)
                    value = copy.deepcopy(response)
                    value['complete'] = len(seen_times) > 1
                    return value
                self.model.structured.side_effect = model_response
                report, _ = self.run_cycle(runtime_seconds=2)
                self.assertTrue(report['continuation']['ready'])
                self.state['collection']['mode'] = 'both'
                original = copy.deepcopy(self.state['collection'])
                before = copy.deepcopy(self.gets)
                key = next(iter(self.state['readings']))
                expiry = self.cache.records[key]['expiresAt']
                original_request = self.usage.requests[0]
                self.registry['members'].append(collector.members.new_member(
                    'いと', 'https://x.com/ito_synthetic', self.now, member_id='m-' + '2' * 32))
                self.now += dt.timedelta(minutes=2)
                self.reopen()
                resumed_at = self.now
                report, _ = self.run_cycle(resume=True, cycle_id=original['chainId'])
                for field in ('chainId', 'names', 'periods', 'serviceDate', 'mode'):
                    self.assertEqual(report['continuation'][field], original[field])
                self.assertEqual(report['nextStage'], 'complete')
                self.assertEqual(self.gets, before)
                self.assertEqual(self.usage.calls, 2)
                self.assertEqual(self.usage.requests.count(original_request), 1)
                self.assertEqual(seen_times[1], resumed_at)
                self.assertEqual(report['finishedAt'], facts.stamp(self.now))
                self.assertEqual(self.cache.records[key]['expiresAt'], expiry)
                self.assertTrue(any(revision['analysis']['analyzedAt'] >= facts.stamp(resumed_at)
                                    for revision in self.state['revisions'].values()))
                expected_periods = facts.target_periods(dt.date(2026, 9, local_day))
                self.assertEqual(set(self.state['coverage']), {period[0] for period in expected_periods})
                self.assertTrue(all('いと' not in people for people in self.state['coverage'].values()))
                self.reopen()
                report, _ = self.run_cycle(resume=True, cycle_id=original['chainId'])
                self.assertEqual(report['analysisRequests'], 0)
                self.assertEqual(self.gets, before)

    def test_delayed_new_service_date_anchors_scope_but_not_receipts_or_cache_ttl(self):
        self.now = dt.datetime(2026, 9, 30, 15, 30, tzinfo=facts.UTC)
        started = self.now
        self.model.structured.return_value = reading_fixture.result(
            reading_fixture.row(20, weekday='日', transcription='20日 昼'), half='second')
        report, code = self.run_cycle(service_date='2026-09-30')
        self.assertEqual((code, report['analysisRequests']), (0, 1))
        self.assertEqual(report['continuation']['serviceDate'], '2026-09-30')
        self.assertEqual(report['continuation']['periods'], [['2026-09-16', '2026-09-30']])
        self.assertEqual(report['finishedAt'], facts.stamp(started))
        self.assertEqual(self.state['schedules'][0]['period']['from'], '2026-09-16')
        record = next(iter(self.cache.records.values()))
        self.assertEqual(dt.datetime.fromisoformat(record['expiresAt']), started + dt.timedelta(days=7))
        self.assertEqual(next(iter(self.state['revisions'].values()))['analysis']['analyzedAt'],
                         facts.stamp(started))
        before = copy.deepcopy(self.gets)
        self.now += dt.timedelta(minutes=2)
        self.reopen()
        report, _ = self.run_cycle(resume=True, service_date='2026-10-13')
        self.assertEqual(report['continuation']['serviceDate'], '2026-09-30')
        self.assertEqual(report['continuation']['periods'], [['2026-09-16', '2026-09-30']])
        self.assertEqual(report['analysisRequests'], 0)
        self.assertEqual(self.gets, before)

    def test_invalid_service_dates_fail_before_source_io_and_require_cycle_cli(self):
        for value in ('', '2026-9-30', '2026-09-31', '2026-09-30T00:00:00Z', 123):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid_schedule_service_date'):
                self.run_cycle(service_date=value)
            with self.assertRaisesRegex(ValueError, 'invalid_schedule_service_date'):
                collector.run(mock.Mock(cycle=True, service_date=value))
        with self.assertRaisesRegex(ValueError, 'schedule_service_date_requires_cycle'):
            collector.run(mock.Mock(cycle=False, service_date='2026-09-30'))
        self.assertEqual(self.gets, {'searches': [], 'posts': [], 'images': []})
        self.assertEqual(self.usage.calls, 0)

    def test_interrupted_issued_original_never_repeats_same_paid_input(self):
        self.model.structured.side_effect = [
            azure.AnalysisFailure('azure_timeout'), reading_fixture.result()]
        self.run_cycle()
        original = self.usage.requests[0]
        before = copy.deepcopy(self.gets)
        self.now += dt.timedelta(hours=7)
        self.reopen()
        self.run_cycle(resume=True)
        self.assertEqual(self.gets, before)
        self.assertEqual(self.usage.requests.count(original), 1)
        self.assertEqual(len(self.state['schedules']), 1)

    def test_chunked_initial_all_originals_confirm_only_after_all_packs(self):
        import test_schedule_reading as raster
        self.payloads[base.post_id()] = base.payload(photos=3)
        images = [raster.image((5000, 4000)), raster.image((5000, 4000)), raster.image((1, 1))]
        self.model.structured.side_effect = [
            reading_fixture.result(indexes=(0, 1)),
            reading_fixture.result(reading_fixture.row(7, weekday='月', index=2), indexes=(2,))]
        factory = self.client
        def client():
            value = factory()
            def image(url):
                self.gets['images'].append(url)
                return images[len(self.gets['images']) - 1]
            value.image.side_effect = image
            return value
        self.client = client
        report, _ = self.run_cycle()
        self.assertEqual(report['analysisRequests'], 2)
        self.assertEqual(len(self.state['schedules']), 1)
        table = self.state['schedules'][0]
        self.assertEqual([row['date'] for row in table['days']], ['2026-09-05', '2026-09-07'])
        self.assertTrue(table['reading']['complete'])
        proof = next(rev['analysis'] for rev in self.state['revisions'].values()
                     if 'readingBatch' in rev['analysis'])
        self.assertEqual(len(proof['readingBatch']), 2)
        value = self.cache.get(next(iter(self.state['readings'])))
        self.assertEqual(value['variants'], [])
        self.assertEqual(len(value['seen']), 3)
        saved = collector._module('half-month-saved.py', 'cycle_accounting_test')
        usage = {'receipts': {part['receiptId']: {
            'component': 'schedule', 'requestHash': part['requestHash'], 'issuedAt': facts.stamp(self.now),
            'completedAt': facts.stamp(self.now), 'reason': 'events',
            'identity': {'model': facts.MODEL, 'modelVersion': facts.MODEL_VERSION, 'deployment': facts.MODEL}}
            for part in proof['readingBatch']}}
        saved.validate_accounting(self.state, usage, facts)
        del usage['receipts'][proof['readingBatch'][0]['receiptId']]
        with self.assertRaisesRegex(ValueError, 'half_month_usage_missing'):
            saved.validate_accounting(self.state, usage, facts)

    def test_last_valid_pack_survives_missing_earlier_result_and_invalid_detail(self):
        self.payloads[base.post_id()] = base.payload(photos=2)
        original_packs = azure.reading_packs
        def split(source, text, images, *, stage='original', **kwargs):
            if stage != 'original':
                return original_packs(source, text, images, stage=stage, **kwargs)
            packs = [azure.reading.pack_attachments(text, [view])[0]
                     for view in azure.reading.original_views(images)]
            for index, pack in enumerate(packs):
                pack.update(part=index + 1, parts=len(packs))
            return packs
        self.model.structured.side_effect = [
            azure.AnalysisFailure('azure_timeout'),
            reading_fixture.result(reading_fixture.row(7, weekday='月', index=1), indexes=(1,)),
            {'invalid': 'synthetic'}, {'invalid': 'synthetic'}]
        with mock.patch.object(azure, 'reading_packs', side_effect=split):
            self.run_cycle()
            self.now += dt.timedelta(hours=7)
            self.reopen()
            self.run_cycle(resume=True)
        self.assertEqual(self.state['schedules'], [])
        self.assertEqual(self.state['partialSchedules'][0]['days'],
                         [{'date': '2026-09-07', 'shifts': ['昼']}])
        self.assertFalse(self.state['coverage']['2026-09-01']['あむ']['confirmedIds'])
        self.assertEqual(next(iter(self.state['readings'].values()))['stage'], 'held')

    def test_resume_cannot_invent_new_cohort_and_expiry_never_refetches(self):
        with self.assertRaisesRegex(ValueError, 'schedule_resume_missing'):
            self.run_cycle(resume=True)
        self.usage.limit = 1
        self.model.structured.return_value = reading_fixture.result(complete=False)
        self.run_cycle()
        before = copy.deepcopy(self.gets)
        self.now += dt.timedelta(days=7)
        self.usage.limit = 10
        self.reopen()
        report, _ = self.run_cycle()
        self.assertEqual(report['reasons'], ['image_cache_expired'])
        diagnostic = report['diagnostics'][0]
        self.assertEqual(diagnostic['postId'], base.post_id())
        self.assertEqual(diagnostic['postUrl'], facts.public_url('amu_zettai', base.post_id()))
        self.assertEqual(diagnostic['failedAt'], facts.stamp(self.now))
        self.assertEqual((diagnostic['stage'], diagnostic['nextStage'], diagnostic['reason']),
                         ('cache', 'held', 'image_cache_expired'))
        self.assertIsNone(diagnostic['host'])
        self.assertIsNone(diagnostic['imageIndex'])
        self.assertEqual(self.gets, before)
        self.assertEqual(self.usage.calls, 1)

    def test_mixed_archive_pruning_preserves_canonical_expiry_and_never_refetches(self):
        self.usage.limit = 1
        self.model.structured.return_value = reading_fixture.result(complete=False)
        self.run_cycle()
        before = copy.deepcopy(self.gets)
        old_key = next(iter(self.state['readings']))
        self.now += dt.timedelta(days=1)
        self.cache.put('f' * 64, {'id': 'synthetic-fresh', 'authorId': 'synthetic-author'},
                       'synthetic live evidence', [{'bytes': base.png(), 'mime': 'image/png'}])
        fresh_record = copy.deepcopy(self.cache.records['f' * 64])
        self.now += dt.timedelta(days=6)
        archive = Path(self.temp.name) / 'schedule-evidence.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('cache.bin', self.path.read_bytes())
        restore = collector._module('restore-schedule-cache.py', 'cycle_restore_test')
        summary = restore.restore(archive, target=self.path, key=self.key, clock=self.clock)
        self.assertEqual((summary['records'], summary['expiredRecords']), (1, 1))
        self.usage.limit = 10
        self.reopen()
        self.assertEqual(self.cache.records['f' * 64], fresh_record)
        report, _ = self.run_cycle()
        self.assertEqual(report['reasons'], ['image_cache_expired'])
        self.assertEqual(self.state['readings'][old_key]['reason'], 'image_cache_expired')
        self.assertEqual(self.state['readings'][old_key]['stage'], 'held')
        self.assertEqual(self.gets, before)
        self.assertEqual(self.usage.calls, 1)
        self.reopen()
        self.run_cycle()
        self.assertEqual(self.state['readings'][old_key]['reason'], 'image_cache_expired')
        self.assertEqual(self.gets, before)
        self.assertEqual(self.usage.calls, 1)
        self.state = facts.empty_state()
        self.state['pending'] = collector.discover(
            base.document(base.entry()), {'あむ': base.TARGET}, base.NOW)[0]
        self.reopen()
        report, _ = self.run_cycle()
        self.assertEqual(report['reasons'], ['image_cache_expired'])
        self.assertEqual(self.gets, before, 'snapshot lag must not refetch an expired source')
        self.assertEqual(self.usage.calls, 1)

    def test_all_expired_restore_holds_old_source_but_allows_new_member_cycle(self):
        self.usage.limit = 1
        self.model.structured.return_value = reading_fixture.result(complete=False)
        self.run_cycle()
        old_key = next(iter(self.state['readings']))
        old_expiry = self.cache.records[old_key]['expiresAt']
        before = copy.deepcopy(self.gets)
        self.now += dt.timedelta(days=7)
        archive = Path(self.temp.name) / 'schedule-evidence.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('cache.bin', self.path.read_bytes())
        restore = collector._module('restore-schedule-cache.py', 'all_expired_cycle_restore')
        summary = restore.restore(archive, target=self.path, key=self.key, clock=self.clock)
        self.assertEqual((summary['status'], summary['records'], summary['expiredRecords']),
                         ('all_expired', 0, 1))
        self.reopen()
        self.assertEqual(self.cache.records[old_key]['expiresAt'], old_expiry)
        with self.assertRaisesRegex(ValueError, 'evidence_cache_expired'):
            self.cache.get(old_key)
        name, handle, author = 'いと', 'ito_synthetic', '2080944098043977778'
        self.registry['members'].append(collector.members.new_member(
            name, 'https://x.com/' + handle, self.now, member_id='m-' + '2' * 32))
        created = self.now - dt.timedelta(hours=1)
        entry = base.entry(created, suffix=2, name=name, handle=handle)
        entry['userId'] = author
        payload = base.payload(created, suffix=2)
        payload['user'] = {'id_str': author, 'screen_name': handle}
        for index, media in enumerate(payload['mediaDetails'], 1):
            media['expanded_url'] = f'https://x.com/{handle}/status/{entry["id"]}/photo/{index}'
        self.documents[handle] = base.document(entry)
        self.payloads[entry['id']] = payload
        self.usage.limit = 10
        self.model.structured.return_value = reading_fixture.result(
            reading_fixture.row(), reading_fixture.row(20, weekday='日', transcription='20日 昼'),
            half='full')
        report, _ = self.run_cycle(cycle_id='2002-1')
        self.assertIn('image_cache_expired', report['reasons'])
        self.assertEqual(report['analysisRequests'], 1, 'only the new member may issue AI')
        self.assertEqual(self.gets['searches'][len(before['searches']):], [handle])
        self.assertEqual(self.gets['posts'][len(before['posts']):], [entry['id']])
        self.assertEqual(len(self.gets['images']) - len(before['images']), 1)
        self.assertEqual(self.state['readings'][old_key]['reason'], 'image_cache_expired')
        self.assertEqual(self.state['readings'][old_key]['stage'], 'held')
        self.assertTrue(all(self.state['coverage'][start][name]['confirmedIds']
                            for start, _ in self.state['collection']['periods']))
        self.assertEqual(self.cache.records[old_key]['expiresAt'], old_expiry)
        self.assertEqual(self.cache.records[old_key]['value']['images'], [])
        before = copy.deepcopy(self.gets)
        self.reopen()
        report, _ = self.run_cycle(resume=True, cycle_id='2002-1')
        self.assertEqual(report['analysisRequests'], 0)
        self.assertEqual(self.gets, before)
        self.assertEqual(self.state['readings'][old_key]['reason'], 'image_cache_expired')

    def test_cycle_boundary_first_and_thirteenth(self):
        for date, expected in [(dt.date(2026, 10, 1), '2026-10-01'),
                               (dt.date(2026, 10, 12), '2026-10-01'),
                               (dt.date(2026, 10, 13), '2026-10-16')]:
            self.assertEqual(facts.discovery_periods(date)[0][0], expected)

    def test_cache_commit_ahead_of_snapshot_recovers_without_get_or_ai(self):
        self.run_cycle()
        before = copy.deepcopy(self.gets)
        self.state = facts.empty_state()
        self.state['pending'] = collector.discover(
            base.document(base.entry()), {'あむ': base.TARGET}, base.NOW)[0]
        self.reopen()
        report, _ = self.run_cycle()
        self.assertEqual(report['analysisRequests'], 0)
        self.assertEqual(self.gets, before)
        self.assertEqual(self.usage.calls, 1)
        self.assertEqual(len(self.state['schedules']), 1)


class SharedAccountingCycleTests(unittest.TestCase):
    """Actual CLI consumer, source ledger, money ledger and Azure wire admission."""
    def setUp(self):
        import test_analysis_state as accounting
        self.ledger, self.accounting = accounting.usage, accounting
        self.temp = tempfile.TemporaryDirectory(prefix='half-shared-accounting-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = base.NOW
        self.clock = lambda: self.now
        self.path = self.root / '_private-evidence' / 'cache.bin'
        self.key = base64.b64encode(b'S' * 32).decode()
        self.state, self.registry = facts.empty_state(), base.registry_fixture()
        self.documents = {base.TARGET['handle']: base.document(base.entry())}
        self.payloads = {base.post_id(): base.payload()}
        guard = mock.patch('socket.socket', side_effect=AssertionError('live network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.ai_path = self.root / 'ai.json'
        self.snapshot = self.root / 'half.json'
        self.personal_path = self.root / 'personal.json'
        self.source_path = self.root / 'source.json'
        self.members_path = self.root / 'members.json'
        self.schedule_path = self.root / 'schedule.js'
        self.http_path = self.root / 'http.json'
        self.ledger.atomic_json(self.ai_path, {
            **self.ledger.empty_state(), 'money': self.ledger.costs.empty()})
        collector.official.atomic_json(self.snapshot, self.state)
        personal = collector.personal.empty_state()
        collector.official.atomic_json(self.personal_path, personal)
        collector.source_safety.atomic_json(self.source_path, collector.source_safety.baseline_state(
            personal, source_hash=facts.digest(b'synthetic-baseline'), at=self.now))
        collector.official.atomic_json(self.members_path, self.registry)
        self.schedule_path.write_text(
            'window.SCHEDULE_DATA = ' + json.dumps(base.SCHEDULE) + ';', encoding='utf-8')
        self.http_calls, self.wires, self.waits = [], [], []
        self.source_status, self.azure_status = 200, 200
        self.responses = [
            {'classification': 'non_schedule', 'complete': True, 'periods': []},
            reading_fixture.result()]
        self.opener = mock.Mock()
        self.opener.open.side_effect = self.http

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += dt.timedelta(seconds=seconds)

    def opening(self, micro_jpy=707_000_000):
        state = self.ledger.load_state(self.ai_path)
        old = self.accounting.historical(date='2026-09-07', count=1)
        self.ledger.apply_import(state, old)
        state['money']['opening']['2026-09'] = {
            'amountMicroJPY': micro_jpy, 'basisHash': self.accounting.digest('synthetic-approved-hold'),
            'records': {'import:' + old['receiptId']: self.ledger.costs.digest(old)},
            'kind': 'provisional-azure-actual', 'throughDate': '2026-09-06',
            'observedAt': facts.stamp(base.NOW)}
        self.ledger.atomic_json(self.ai_path, state)
        return copy.deepcopy(state['money']['opening'])

    def http(self, request, *, timeout):
        url = request.full_url
        self.http_calls.append(url)
        status = 200
        if url.startswith('https://offline.openai.azure.com/'):
            wire = json.loads(request.data)
            state = self.ledger.load_state(self.ai_path)
            active = next(receipt for receipt in state['receipts'].values()
                          if receipt['requestHash'] == facts.digest(request.data))
            self.assertIsNotNone(active['issuedAt'])
            self.assertIsNone(active['completedAt'])
            self.assertEqual(active['money']['payloadHash'], facts.digest(request.data))
            self.assertEqual((active['money']['inputCeiling'], active['money']['outputCeiling']),
                             (922000, azure.READING_MAX_OUTPUT_TOKENS))
            self.assertEqual(wire['model'], 'gpt-5.6-luna')
            content = wire['messages'][1]['content']
            attachments = [part for part in content if part['type'] == 'image_url']
            self.assertLessEqual(len(attachments), 4)
            azure.image_facts([{
                'bytes': base64.b64decode(part['image_url']['url'].split(',', 1)[1], validate=True),
                'mime': part['image_url']['url'].split(';', 1)[0].removeprefix('data:'),
            } for part in attachments])
            self.assertLessEqual(len(request.data), azure.MAX_REQUEST_BYTES)
            self.wires.append(wire)
            status = self.azure_status
            raw = json.dumps({
                'model': facts.MODEL + '-' + facts.MODEL_VERSION,
                'usage': {'prompt_tokens': 1200, 'completion_tokens': 200, 'total_tokens': 1400,
                          'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}},
                'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps(self.responses[len(self.wires) - 1])}}],
            }).encode()
            mime = 'application/json'
        elif url.startswith('https://search.yahoo.co.jp/'):
            raw, mime = self.documents[base.TARGET['handle']].encode(), 'text/html'
        elif url.startswith('https://cdn.syndication.twimg.com/'):
            tid = collector.urllib.parse.parse_qs(collector.urllib.parse.urlsplit(url).query)['id'][0]
            raw, mime = json.dumps(self.payloads[tid]).encode(), 'application/json'
        elif url.startswith('https://pbs.twimg.com/'):
            raw, mime, status = base.png(), 'image/png', self.source_status
        else:
            raise AssertionError('unexpected offline route')
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = status
        response.headers = {'Content-Type': mime, 'Content-Length': str(len(raw)),
                            'Date': email.utils.format_datetime(self.now, usegmt=True)}
        response.read.side_effect = io.BytesIO(raw).read
        return response

    def invoke(self, *extra):
        arguments = collector.argument_parser().parse_args([
            '--once', '--cycle', '--snapshot', str(self.snapshot),
            '--source-state', str(self.source_path), '--personal-state', str(self.personal_path),
            '--http-state', str(self.http_path), '--ai-state', str(self.ai_path),
            '--analysis-run-id', '8001-1', '--analysis-limit', '1',
            '--schedule', str(self.schedule_path), '--members', str(self.members_path),
            '--evidence-cache', str(self.path), *extra])
        environment = {'AZURE_OPENAI_ENDPOINT': 'https://offline.openai.azure.com',
                       'AZURE_OPENAI_API_KEY': 'SYNTHETIC-NOT-A-SECRET',
                       'SCHEDULE_EVIDENCE_KEY': self.key}
        # Both clients retain their actual implementation; only their HTTP boundary is mocked.
        with mock.patch.object(azure.transport.urllib.request, 'build_opener', return_value=self.opener):
            return collector.run(arguments, clock=self.clock, sleep=self.sleep, environment=environment)

    def test_actual_consumer_and_shared_reservations_admit_initial_and_reread_same_run(self):
        report, code = self.invoke()
        self.assertEqual((code, report['analysisRequests']), (0, 2))
        self.assertEqual(report['requests'], {'searches': 1, 'posts': 1, 'images': 1})
        self.assertEqual([len(wire['messages'][1]['content']) - 1 for wire in self.wires], [1, 4])
        state = self.ledger.load_state(self.ai_path)
        receipts = sorted(state['receipts'].values(), key=lambda row: row['issuedAt'])
        self.assertEqual(len(receipts), 2)
        self.assertEqual({row['runId'] for row in receipts}, {'8001-1'})
        self.assertEqual({row['component'] for row in receipts}, {'schedule'})
        self.assertEqual(len({row['requestHash'] for row in receipts}), 2)
        self.assertTrue(all(row['completedAt'] and row['money']['usageStatus'] == 'settled' for row in receipts))
        self.assertGreaterEqual((facts.timestamp(receipts[1]['issuedAt']) -
                                 facts.timestamp(receipts[0]['issuedAt'])).total_seconds(), 60)
        balance = self.ledger.costs.balance(state, self.now)
        self.assertEqual(balance['reason'], 'ok')
        self.assertEqual(balance['reservedMicroJPY'], 0)
        self.assertGreater(balance['tokenPricedMicroJPY'], 0)
        saved = collector._module('half-month-saved.py', 'shared_cycle_accounting')
        saved.validate_accounting(facts.read_state(self.snapshot), state, facts)
        before = list(self.http_calls)
        self.assertEqual(self.invoke('--resume')[0]['analysisRequests'], 0)
        self.assertEqual(self.http_calls, before)

    def test_current_month_707_hold_is_not_bypassed_to_force_image_admission(self):
        opening = self.opening()
        report, _ = self.invoke()
        self.assertEqual(report['reasons'], ['budget_wait'])
        self.assertEqual(self.wires, [])
        state = self.ledger.load_state(self.ai_path)
        self.assertEqual(state['receipts'], {})
        self.assertEqual(state['money']['opening'], opening)
        balance = self.ledger.costs.balance(state, self.now)
        self.assertEqual(balance['openingProvisionalMicroJPY'], 707_000_000)
        self.assertEqual(balance['availableMicroJPY'], 293_000_000)
        self.assertEqual(self.ledger.costs.LIMIT, 1_000_000_000)
        self.assertEqual(next(iter(facts.read_state(self.snapshot)['readings'].values()))['stage'], 'original')

    def test_actual_crop_and_tile_packs_are_not_stopped_after_first_or_second_call(self):
        self.responses = [
            reading_fixture.result(reading_fixture.row(box=[.1, .1, .3, .3]), complete=False),
            reading_fixture.result(), reading_fixture.result()]
        report, code = self.invoke()
        self.assertEqual((code, report['analysisRequests']), (0, 3))
        self.assertEqual([len(wire['messages'][1]['content']) - 1 for wire in self.wires], [1, 4, 1])
        state = self.ledger.load_state(self.ai_path)
        self.assertEqual(len(state['receipts']), 3)
        self.assertTrue(all(row['money']['usageStatus'] == 'settled' for row in state['receipts'].values()))
        self.assertEqual(report['requests']['images'], 1)

    def test_october_cycle_admits_both_without_erasing_september_707_hold(self):
        opening = self.opening()
        self.now = dt.datetime(2026, 9, 30, 15, 30, tzinfo=dt.timezone.utc)
        created = self.now - dt.timedelta(days=1)
        entry, payload = base.entry(created=created), base.payload(created=created)
        payload['text'] = '10月前半のお給仕予定'
        self.documents = {base.TARGET['handle']: base.document(entry)}
        self.payloads = {entry['id']: payload}
        self.responses[1] = reading_fixture.result(reading_fixture.row(weekday='月'), month=10)
        report, code = self.invoke()
        self.assertEqual((code, report['analysisRequests']), (0, 2))
        state = self.ledger.load_state(self.ai_path)
        self.assertEqual(state['money']['opening'], opening)
        balance = self.ledger.costs.balance(state, self.now)
        self.assertEqual(balance['openingProvisionalMicroJPY'], 0)
        self.assertGreater(balance['availableMicroJPY'], 0)
        self.assertEqual(facts.read_state(self.snapshot)['schedules'][0]['period']['from'], '2026-10-01')

    def test_delayed_september_service_date_uses_actual_october_shared_accounting(self):
        opening = self.opening()
        self.now = dt.datetime(2026, 9, 30, 15, 30, tzinfo=facts.UTC)
        started = self.now
        self.responses[1] = reading_fixture.result(
            reading_fixture.row(20, weekday='日', transcription='20日 昼'), half='second')
        report, code = self.invoke('--service-date', '2026-09-30')
        self.assertEqual((code, report['analysisRequests']), (0, 2))
        self.assertEqual(report['continuation']['serviceDate'], '2026-09-30')
        self.assertEqual(report['continuation']['periods'], [['2026-09-16', '2026-09-30']])
        state = self.ledger.load_state(self.ai_path)
        self.assertEqual(state['money']['opening'], opening)
        receipts = list(state['receipts'].values())
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(facts.timestamp(row['issuedAt']) >= started for row in receipts))
        balance = self.ledger.costs.balance(state, self.now)
        self.assertEqual(balance['openingProvisionalMicroJPY'], 0)
        self.assertGreater(balance['tokenPricedMicroJPY'], 0)
        self.assertEqual(facts.read_state(self.snapshot)['schedules'][0]['period']['from'], '2026-09-16')
        before = list(self.http_calls)
        report, _ = self.invoke('--resume', '--service-date', '2026-10-13', '--analysis-run-id', '8002-1')
        self.assertEqual(report['continuation']['serviceDate'], '2026-09-30')
        self.assertEqual(report['analysisRequests'], 0)
        self.assertEqual(self.http_calls, before)

    def test_actual_spacing_deadline_checkpoints_before_second_reservation_and_resumes(self):
        report, _ = self.invoke('--max-runtime-seconds', '30')
        self.assertEqual(report['reasons'], ['time_limit'])
        self.assertTrue(report['continuation']['ready'])
        self.assertEqual(len(self.ledger.load_state(self.ai_path)['receipts']), 1)
        before_sources = [url for url in self.http_calls if 'offline.openai.azure.com' not in url]
        self.now += dt.timedelta(minutes=2)
        report, code = self.invoke('--resume', '--cycle-id', '8001-1', '--analysis-run-id', '8002-1')
        self.assertEqual((code, report['analysisRequests']), (0, 1))
        self.assertEqual([url for url in self.http_calls if 'offline.openai.azure.com' not in url], before_sources)
        self.assertEqual(len(self.ledger.load_state(self.ai_path)['receipts']), 2)

    def test_source_host_refusal_preserves_host_stop_and_never_reaches_azure(self):
        self.source_status = 403
        report, _ = self.invoke()
        self.assertEqual(report['reasons'], ['paused'])
        state = collector.source_safety.load_state(self.source_path, required=True)
        self.assertTrue(collector.source_safety.paused_for(state, 'images', self.now))
        self.assertFalse(collector.source_safety.paused_for(state, 'searches', self.now))
        self.assertEqual(self.wires, [])
        self.assertEqual(self.ledger.load_state(self.ai_path)['receipts'], {})
        diagnostic = report['diagnostics'][0]
        self.assertEqual((diagnostic['host'], diagnostic['httpStatus'], diagnostic['imageIndex']),
                         ('pbs.twimg.com', 403, 0))
        self.assertEqual(diagnostic['postUrl'], facts.public_url('amu_zettai', base.post_id()))
        self.assertEqual(diagnostic['name'], 'あむ')
        self.assertEqual(diagnostic['failedAt'], facts.stamp(self.now))
        self.assertGreater(facts.timestamp(diagnostic['retryAt']), self.now)
        self.assertEqual((diagnostic['stage'], diagnostic['nextStage'], diagnostic['reason']),
                         ('fetch', 'fetch', 'paused'))
        before = list(self.http_calls)
        waiting, _ = self.invoke('--resume')
        self.assertEqual(waiting['diagnostics'], [diagnostic])
        self.assertEqual(self.http_calls, before)

    def test_azure_refusal_remains_a_real_shared_accounting_stop(self):
        self.azure_status = 403
        report, _ = self.invoke()
        self.assertEqual(report['reasons'], ['paused'])
        state = self.ledger.load_state(self.ai_path)
        self.assertEqual(state['paused']['reason'], 'azure_auth_stopped')
        self.assertEqual(state['paused']['httpStatus'], 403)
        self.assertEqual(len(state['receipts']), 1)
        self.assertFalse(report['continuation']['ready'])
        before = list(self.http_calls)
        self.invoke('--resume')
        self.assertEqual(self.http_calls, before)


if __name__ == '__main__':
    unittest.main()
