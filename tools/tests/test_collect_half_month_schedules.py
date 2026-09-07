"""Discovery -> original metadata -> photos -> mocked Luna -> persistent facts."""
import copy
import csv
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest import mock
import unittest
import urllib.error

import test_half_month_schedules as base


collector, facts, azure = base.collector, base.facts, base.azure


class DiscoveryTests(base.Offline):
    def test_best_timeline_dedup_and_query_canonicalization(self):
        value = base.entry()
        rows, limited = collector.discover(base.document(value, best=value), {'あむ': base.TARGET}, base.NOW)
        self.assertFalse(limited)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['url'], facts.public_url(base.TARGET['handle'], value['id']))
        self.assertNotIn('text', rows[0])

    def test_first_binding_and_actual_post_metadata(self):
        rows, _ = collector.discover(base.document(best=base.entry()), {'あむ': base.TARGET}, base.NOW)
        verified, _, urls = collector.validate_post(rows[0], base.payload(), base.TARGET, base.NOW)
        self.assertEqual(verified['authorId'], base.AUTHOR)
        self.assertEqual(len(urls), 1)
        self.assertEqual(collector.media_url(urls[0])[2], 'medium')
        state = facts.empty_state()
        facts.bind_identity(state, verified)
        facts.validate_state(state)

    def test_legacy_short_author_id_is_valid_but_not_float_or_bool(self):
        entry, payload = base.entry(), base.payload()
        entry['userId'] = '12345'
        payload['user']['id_str'] = '12345'
        payload['mediaDetails'][0]['source_user_id'] = 12345
        candidate = collector.discover(base.document(entry), {'あむ': base.TARGET}, base.NOW)[0][0]
        verified, _, _ = collector.validate_post(candidate, payload, base.TARGET, base.NOW)
        self.assertEqual(verified['authorId'], '12345')
        for invalid in (True, 12345.0, '012345', '0'):
            self.assertIsNone(collector.author_id(invalid))

    def test_identity_timestamp_route_and_reply_rejected(self):
        for field, value in [('userId', '2080944098043977888'), ('screenName', 'different'),
                             ('url', 'https://x.com/other/status/' + base.post_id()),
                             ('url', 'https://x.com/' + base.TARGET['handle'] + '/status/' + base.post_id() + '/photo/1'),
                             ('createdAt', str(int(base.CREATED.timestamp()) + 3)),
                             ('isRetweet', True)]:
            candidate = base.entry()
            candidate[field] = value
            bindings = {'あむ': {'authorId': base.AUTHOR, 'authorScreenName': base.TARGET['handle']}}
            with self.subTest(field=field):
                self.assertEqual(collector.discover(base.document(candidate), {'あむ': base.TARGET},
                                                   base.NOW, bindings)[0], [])
        rows = collector.discover(base.document(base.entry()), {'あむ': base.TARGET}, base.NOW)[0]
        for field, value in [('quoted_tweet', {'id': 'other'}), ('in_reply_to_user_id', 1),
                             ('retweeted_status', {'id': 'other'})]:
            payload = base.payload()
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                collector.validate_post(rows[0], payload, base.TARGET, base.NOW)

    def test_conflicting_duplicate_and_candidate_cap(self):
        first, other = base.entry(), base.entry()
        other['userId'] = '2080944098043977888'
        self.assertEqual(collector.discover(base.document(first, other), {'あむ': base.TARGET}, base.NOW)[0], [])
        entries = [base.entry(suffix=n + 1) for n in range(210)]
        found, limited = collector.discover(base.document(*entries), {'あむ': base.TARGET}, base.NOW)
        self.assertTrue(limited)
        self.assertEqual(len(found), 200)
        same = base.entry()
        same['text'] = 'ordinary post'
        self.assertEqual(len(collector.discover(base.document(base.entry(), same),
                                                {'あむ': base.TARGET}, base.NOW)[0]), 1)

    def test_not_daily_collector_window(self):
        created = dt.datetime(2026, 8, 17, tzinfo=facts.UTC)
        found, _ = collector.discover(base.document(base.entry(created)), {'あむ': base.TARGET}, base.NOW)
        self.assertEqual(len(found), 1)
        created = dt.datetime(2026, 8, 15, tzinfo=facts.UTC)
        self.assertEqual(collector.discover(base.document(base.entry(created)),
                                            {'あむ': base.TARGET}, base.NOW)[0], [])

    def test_original_photo_ownership_counts_and_edit(self):
        candidate = collector.discover(base.document(base.entry()), {'あむ': base.TARGET}, base.NOW)[0][0]
        invalid = []
        item = base.payload()
        item['mediaDetails'][0]['expanded_url'] = 'https://x.com/other/status/' + base.post_id() + '/photo/1'
        invalid.append(item)
        item = base.payload()
        item['mediaDetails'][0]['source_user_id_str'] = '2080944098043977888'
        invalid.append(item)
        item = base.payload()
        item['mediaDetails'][0]['type'] = 'video'
        invalid.append(item)
        item = base.payload()
        item['photos'] = []
        invalid.append(item)
        item = base.payload()
        item['edit_control']['edit_tweet_ids'].append(str(int(base.post_id()) + 1))
        invalid.append(item)
        invalid.append(base.payload(photos=5))
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                collector.validate_post(candidate, item, base.TARGET, base.NOW)

    def test_bounded_queue_and_fast_recheck_without_reset(self):
        state = facts.empty_state()
        _, reasons = facts.population(base.SCHEDULE, {}, base.ACCOUNTS)
        periods = collector.refresh_coverage(state, reasons, base.NOW, {})
        found, _ = collector.discover(base.document(*[base.entry(suffix=n + 1) for n in range(10)]),
                                      {'あむ': base.TARGET}, base.NOW)
        dropped = collector.enqueue(state, found, base.NOW)
        self.assertEqual(dropped, {'あむ'})
        self.assertEqual(len(state['pending']), 6)
        first = state['coverage'][periods[0][0]]['あむ']
        self.assertEqual(first['reason'], 'queue_limit')
        first['lastSearchedAt'] = facts.stamp(base.NOW)
        collector.refresh_coverage(state, reasons, base.NOW, {})
        self.assertEqual(first['nextCheckAt'], facts.stamp(base.NOW + dt.timedelta(hours=6)))
        collector.refresh_coverage(state, reasons, base.NOW + dt.timedelta(hours=1), {})
        self.assertEqual(first['nextCheckAt'], facts.stamp(base.NOW + dt.timedelta(hours=6)))
        self.assertEqual(len(state['pending']), 6)


class FetchTests(base.Offline):
    def response(self, raw, mime='image/png', status=200, length=None):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = status
        response.headers = {'Content-Type': mime}
        if length is not None:
            response.headers['Content-Length'] = length
        response.read.side_effect = io.BytesIO(raw).read
        return response

    def client(self, response):
        source, opener = mock.Mock(), mock.Mock()
        source.state = {'cooldowns': {}}
        source.reserve.return_value = 'receipt'
        opener.open.return_value = response
        return collector.SourceClient(source, clock=lambda: base.NOW, opener=opener), source, opener

    def test_same_run_one_get_transient_cleanup(self):
        url = 'https://pbs.twimg.com/media/SYNTHETIC.png'
        client, source, opener = self.client(self.response(base.png()))
        first = client.image(url)
        self.assertIs(first, client.image(url))
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(client.requests, {'searches': 0, 'posts': 0, 'images': 1})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.headers, {})
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 35)
        source.issued.assert_called_once_with('receipt')
        client.close()
        self.assertEqual(set(first['bytes']), {0})
        with self.assertRaises(collector.Failure):
            client.image(url)
        self.assertEqual(opener.open.call_count, 1)

    def test_url_allowlist(self):
        good = ['https://pbs.twimg.com/media/TEST.jpg',
                'https://pbs.twimg.com/media/TEST?format=jpg&name=medium',
                'https://pbs.twimg.com/media/TEST.png:large']
        for url in good:
            self.assertTrue(collector.media_url(url))
        for url in ['http://pbs.twimg.com/media/TEST.jpg', 'https://pbs.twimg.com.evil/media/TEST.jpg',
                    'https://u@pbs.twimg.com/media/TEST.jpg', 'https://pbs.twimg.com:443/media/TEST.jpg',
                    'https://pbs.twimg.com/media/../TEST.jpg', 'https://pbs.twimg.com/media/%54EST.jpg',
                    'https://pbs.twimg.com/media/TEST.svg', 'https://pbs.twimg.com/media/TEST.jpg#foo',
                    'https://pbs.twimg.com/media/TEST?format=jpg&name=medium&x=1',
                    'https://pbs.twimg.com/media/TEST?format=jpg&format=png&name=medium']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                collector.media_url(url)

    def test_no_redirect_no_proxy_default(self):
        with mock.patch.object(collector.urllib.request, 'build_opener') as build:
            collector.SourceClient(mock.Mock(), clock=lambda: base.NOW)
            handlers = build.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], collector.NoRedirect)
        with self.assertRaises(collector.Failure):
            collector.NoRedirect().redirect_request(None, None, 302, None, {}, 'https://pbs.twimg.com/media/TEST.jpg')

    def test_http_stream_type_and_pixel_failures_never_retry(self):
        cases = [self.response(b'<html>challenge</html>', mime='text/html'),
                 self.response(b'', mime='image/png'),
                 self.response(base.png(), status=302),
                 self.response(base.png(), length=str(azure.MAX_IMAGE_BYTES + 1)),
                 self.response(base.jpeg(6000, 4000), mime='image/jpeg')]
        for response in cases:
            client, source, opener = self.client(response)
            with self.subTest(response=response), self.assertRaises((ValueError, collector.Failure)):
                client.image('https://pbs.twimg.com/media/SYNTHETIC.png')
            self.assertEqual(opener.open.call_count, 1)
            client.close()
            self.assertFalse(client.images)
        client, _, opener = self.client(self.response(b'x' * 20))
        with mock.patch.object(azure, 'MAX_IMAGE_BYTES', 10), self.assertRaises(collector.Failure):
            client.image('https://pbs.twimg.com/media/SYNTHETIC.png')
        self.assertEqual(opener.open.call_count, 1)

    def test_retry_after_and_failure_accounting(self):
        response = self.response(b'', status=429)
        response.headers['Retry-After'] = '7200'
        client, source, opener = self.client(response)
        with self.assertRaises(collector.Failure):
            client.image('https://pbs.twimg.com/media/SYNTHETIC.png')
        source.set_cooldown.assert_called_once_with(
            'pbs.twimg.com', base.NOW + dt.timedelta(hours=2),
            paused={'reason': 'access_denied', 'host': 'pbs.twimg.com', 'at': facts.stamp(base.NOW),
                    'retryAt': facts.stamp(base.NOW + dt.timedelta(hours=2)), 'httpStatus': 429})
        source.finish.assert_called_once_with('receipt', 'failed', http_status=429)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(client.requests['images'], 1)

    def test_immutable_photo_age_is_not_daily_post_freshness(self):
        response = self.response(base.png())
        response.headers['Age'] = '8640000'
        client, _, _ = self.client(response)
        self.assertEqual(client.image('https://pbs.twimg.com/media/SYNTHETIC.png')['mime'], 'image/png')
        client.close()

    def test_http_sidecar_keeps_every_host_maximum_from_both_states(self):
        path = self.work_dir() / 'shared.http-state.json'
        hour = lambda amount: facts.stamp(base.NOW + dt.timedelta(hours=amount))
        collector.official.atomic_json(path, {'schemaVersion': 1, 'cooldowns': {
            collector.SEARCH_HOST: hour(5), collector.POST_HOST: hour(1)}})
        client, source, _ = self.client(self.response(b'', status=403))
        client.http_state = path
        source.state['cooldowns'] = {
            collector.SEARCH_HOST: hour(2), collector.POST_HOST: hour(6), collector.PHOTO_HOST: hour(4)}
        with self.assertRaises(collector.Failure):
            client._deny(collector.PHOTO_HOST, 403, None)
        self.assertEqual(collector.official.load_transport(path), {
            collector.SEARCH_HOST: hour(5), collector.POST_HOST: hour(6), collector.PHOTO_HOST: hour(4)})
        self.assertEqual(source.set_cooldown.call_args.kwargs['paused']['retryAt'], hour(4))

    def test_source_denial_pause_survives_cooldown_and_next_run(self):
        module = collector._module('source-state.py', 'test_schedule_denial_usage')
        for status in (401, 403, 429, 302):
            with self.subTest(status=status):
                path = self.work_dir() / 'source.json'
                module.atomic_json(path, module.baseline_state(
                    {'budgets': {}, 'paused': None}, source_hash=facts.digest(b'offline-baseline'),
                    at=base.NOW))
                opener = mock.Mock()
                opener.open.return_value = self.response(b'', status=status)
                with module.SharedSource(path, run_id='first', component='schedule',
                                          clock=lambda: base.NOW, sleep=lambda _: None) as usage:
                    client = collector.SourceClient(usage, clock=lambda: base.NOW, opener=opener)
                    with self.assertRaises(collector.Failure):
                        client.image('https://pbs.twimg.com/media/SYNTHETIC.png')
                    self.assertEqual(usage.state['paused']['httpStatus'], status)
                    self.assertEqual(next(iter(usage.state['receipts'].values()))['httpStatus'], status)
                later = base.NOW + dt.timedelta(days=2)
                for component in ('personal', 'schedule'):
                    with module.SharedSource(path, run_id='next', component=component,
                                              clock=lambda: later, sleep=lambda _: None) as usage:
                        with self.assertRaises(module.SourceFailure) as caught:
                            usage.check('posts')
                        self.assertEqual(caught.exception.reason, 'source_paused')
                with module.SharedSource(path, run_id='official-next', component='official',
                                          clock=lambda: later, sleep=lambda _: None) as usage:
                    usage.check('posts')
                self.assertEqual(opener.open.call_count, 1)


class ProducerTests(base.Offline):
    def setUp(self):
        super().setUp()
        self.state = facts.empty_state()
        self.source = mock.Mock()
        self.client = mock.Mock()
        self.client.search.return_value = base.document(best=base.entry())
        self.client.post.return_value = base.payload(), facts.digest(b'synthetic source payload')
        self.client.image.return_value = {'bytes': base.png(), 'mime': 'image/png'}
        self.usage, self.model = mock.Mock(), mock.Mock()
        self.model.identity = {'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
                               'deployment': facts.MODEL, 'model': facts.MODEL, 'modelVersion': facts.MODEL_VERSION}
        self.model.structured.return_value = base.result()
        self.analyzer = azure.AzureAnalyzer(self.usage, clock=lambda: base.NOW, client=self.model)
        self.saved = []

    def save(self):
        facts.validate_state(self.state)
        self.saved.append(copy.deepcopy(self.state))

    def collect(self, **kwargs):
        return collector.collect(self.state, base.SCHEDULE, {}, base.ACCOUNTS,
                                 self.client, self.source, self.analyzer, clock=lambda: base.NOW,
                                 save=self.save, **kwargs)

    def test_end_to_end_persistent_feed_and_source_fingerprint(self):
        report, code = self.collect()
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'ok')
        self.assertTrue(report['completed'])
        self.assertEqual(report['collectionStatus'], 'ok')
        self.assertEqual(report['exitCode'], code)
        self.assertEqual(report['requests'], {'searches': 1, 'posts': 1, 'images': 1})
        self.assertEqual(report['analysisRequests'], 1)
        self.assertEqual(len(self.state['schedules'][0]['days']), 6)
        self.assertEqual(self.state['pending'], [])
        self.assertTrue(any(record['status'] == 'issued'
                            for state in self.saved for record in state['sources'].values()))
        self.client.close.assert_called()
        public = facts.public_state(self.state)
        self.assertNotIn(base.payload()['text'], json.dumps(public))
        self.analyzer = azure.AzureAnalyzer(self.usage, clock=lambda: base.NOW, client=self.model)
        self.client.reset_mock()
        report, code = self.collect()
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'no-new')
        self.client.search.assert_not_called()
        self.client.post.assert_not_called()
        self.client.image.assert_not_called()

    def test_all_images_budget_preflight_before_download(self):
        self.client.post.return_value = base.payload(photos=2), facts.digest(b'payload2')
        def check(kind, count=1):
            if kind == 'images' and count == 2:
                raise collector.Failure('source_budget_exhausted')
        self.source.check.side_effect = check
        report, code = self.collect()
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'budget-exhausted')
        self.client.image.assert_not_called()
        self.model.structured.assert_not_called()
        self.assertEqual(next(iter(self.state['sources'].values()))['status'], 'pending')
        self.assertEqual(len(self.state['pending']), 1)

    def test_ai_preflight_before_post(self):
        self.usage.check.side_effect = azure.AnalysisFailure('azure_budget_exhausted')
        report, _ = self.collect()
        self.assertEqual(report['status'], 'budget-exhausted')
        self.client.search.assert_not_called()
        self.client.post.assert_not_called()
        self.client.image.assert_not_called()

    def test_existing_personal_binding_cannot_be_rebound_by_discovery(self):
        previous = {'あむ': {'authorId': '2080944098043977888',
                            'authorScreenName': base.TARGET['handle'],
                            'verifiedAt': facts.stamp(base.CREATED)}}
        self.state['pending'] = collector.discover(
            base.document(best=base.entry()), {'あむ': base.TARGET}, base.NOW)[0]
        self.collect(existing_bindings=previous)
        self.client.post.assert_not_called()
        self.model.structured.assert_not_called()
        self.assertFalse(self.state['identityBindings'])
        self.assertEqual(previous['あむ']['authorId'], '2080944098043977888')

    def test_bad_original_metadata_is_remembered_not_a_queue_head_blocker(self):
        self.client.post.return_value[0]['user']['id_str'] = '2080944098043977888'
        self.collect()
        self.assertFalse(self.state['pending'])
        self.assertEqual(len(self.state['candidateHistory']), 1)
        found, _ = collector.discover(base.document(best=base.entry()), {'あむ': base.TARGET}, base.NOW)
        collector.enqueue(self.state, found, base.NOW)
        self.assertFalse(self.state['pending'])
        self.model.structured.assert_not_called()

    def test_transient_post_failure_backoff_leaves_other_candidates_fair(self):
        self.client.search.return_value = base.document(base.entry(suffix=1), base.entry(suffix=2))
        self.client.post.side_effect = collector.Failure('network_error')
        self.collect()
        attempted = next(item for item in self.state['pending'] if item['lastAttemptAt'])
        self.assertEqual(attempted['nextAttemptAt'], facts.stamp(base.NOW + dt.timedelta(hours=6)))
        first_id = self.client.post.call_args.args[0]
        self.collect()
        self.assertNotEqual(self.client.post.call_args.args[0], first_id)

    def test_cloud_command_line_contract(self):
        cloud = collector._module('cloud-collection.py', 'test_schedule_cloud_command')
        with mock.patch.object(cloud, 'child_process', return_value=mock.Mock(returncode=0)) as child:
            cloud.invoke_half_month_collector(
                base.TOOLS.parent, Path('state'), Path('report.json'),
                {'CLOUD_COLLECTION_SOURCE_ENABLED': 'true', 'CLOUD_COLLECTION_SHARED': 'true',
                 'CLOUD_COLLECTION_RUN_ID': '12345-1'})
            argv = child.call_args.args[0]
            args = collector.argument_parser().parse_args(argv[4:])
            self.assertEqual(args.source_run_id, args.analysis_run_id)
            self.assertEqual(args.max_images, 4)
            self.assertEqual(args.personal_state.name, 'personal-shifts.json')

    def test_image_failure_no_partial_inference_or_replacement(self):
        verified, schedules, analysis = base.normalized(suffix=9)
        facts.apply_revision(self.state, schedules, verified, analysis)
        before = copy.deepcopy(self.state['schedules'])
        self.client.post.return_value = base.payload(photos=2), facts.digest(b'payload2')
        self.client.image.side_effect = [{'bytes': base.png(), 'mime': 'image/png'},
                                         collector.Failure('network_error')]
        report, _ = self.collect()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(self.state['schedules'], before)
        self.model.structured.assert_not_called()
        self.client.close.assert_called()

    def test_failed_analysis_never_auto_retries_preserves_previous(self):
        verified, schedules, analysis = base.normalized(suffix=9)
        facts.apply_revision(self.state, schedules, verified, analysis)
        before = copy.deepcopy(self.state['schedules'])
        self.client.post.return_value[0]['text'] += ' 更新があります。'
        self.model.structured.side_effect = azure.AnalysisFailure('azure_timeout')
        report, _ = self.collect()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(self.state['schedules'], before)
        self.assertEqual(self.state['pending'], [])
        self.assertTrue(any(record['status'] == 'failed' for record in self.state['sources'].values()))
        self.assertEqual(self.model.structured.call_count, 1)

    def test_storage_overflow_holds_all_old_facts_and_surfaces_fixed_partial_reason(self):
        old_source, old_schedules, old_analysis = base.normalized(
            created=base.CREATED - dt.timedelta(hours=1), suffix=9)
        old_schedules[0]['workTiming'] = base.full_timing_channel(old_source)
        facts.apply_revision(self.state, old_schedules, old_source, old_analysis)
        self.state['checkedAt'] = self.state['lastSuccessAt'] = facts.stamp(
            base.NOW - dt.timedelta(minutes=1))
        before = copy.deepcopy(self.state)
        self.client.post.return_value[0]['text'] += ' 5日昼は20:00ではありません。'
        self.model.structured.return_value = base.timing_result({
            5: [base.work_note(qualifier=None, explicit_time='20:00', status='excluded')]})

        report, code = self.collect()
        self.assertEqual((code, report['status'], report['analysisRequests']), (2, 'partial', 1))
        self.assertEqual(self.state['lastRun'], {'status': 'partial'})
        for field in ('schedules', 'revisions', 'receipts', 'savedImports', 'identityBindings', 'lastSuccessAt'):
            self.assertEqual(self.state[field], before[field])
        self.assertTrue(all(self.state['sources'][key] == value for key, value in before['sources'].items()))
        self.assertEqual(len(self.state['schedules'][0]['workTiming']['facts']), 512)
        self.assertEqual(self.state['schedules'][0]['workTiming']['facts'][0]['status'], 'set')
        failed = next(record for record in self.state['sources'].values() if record['status'] == 'failed')
        self.assertEqual(failed['reason'], 'work_timing_storage_limit')
        self.assertTrue(failed['imageHashes'])
        self.usage.finish.assert_called_once_with(failed['requestHash'], 'events')
        self.assertEqual(self.state['pending'], [])
        for start, _ in facts.target_periods(base.NOW):
            self.assertEqual(self.state['coverage'][start][base.TARGET['name']]['reason'],
                             'work_timing_storage_limit')
        facts.validate_state(self.state)
        self.assertEqual(self.saved[-1], self.state)

        self.analyzer = azure.AzureAnalyzer(self.usage, clock=lambda: base.NOW, client=self.model)
        self.client.reset_mock()
        self.model.reset_mock()
        self.collect()
        self.client.search.assert_not_called()
        self.client.post.assert_not_called()
        self.client.image.assert_not_called()
        self.model.structured.assert_not_called()
        self.assertEqual(self.state['schedules'], before['schedules'])
        for start, _ in facts.target_periods(base.NOW):
            self.assertEqual(self.state['coverage'][start][base.TARGET['name']]['reason'],
                             'work_timing_storage_limit')

    def test_identical_reuploaded_image_is_not_charged_again(self):
        verified, schedules, analysis = base.normalized(suffix=9, contract_version=facts.LEGACY_VERSION)
        facts.apply_revision(self.state, schedules, verified, analysis)
        report, code = self.collect()
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'no-new')
        self.client.image.assert_called_once()
        self.model.structured.assert_not_called()
        self.assertEqual(len(self.state['revisions']), 1)

    def test_version_bump_keeps_old_negative_and_failed_sources_without_refetch_or_reanalysis(self):
        verified, _, _ = base.source()
        for status, reason in [('negative', 'not_schedule'), ('failed', 'analysis_failed'),
                               ('negative', 'known_source')]:
            self.state = facts.empty_state()
            source_key = facts.record_source(self.state, verified, status, reason, base.NOW,
                                             facts.digest(b'old-v1-request'),
                                             [facts.digest(base.png())])
            previous = copy.deepcopy(self.state['sources'][source_key])
            self.client.reset_mock()
            self.collect()
            self.client.post.assert_not_called()
            self.client.image.assert_not_called()
            self.model.structured.assert_not_called()
            self.assertEqual(self.state['sources'][source_key], previous)
            self.assertEqual(self.state['pending'], [])

    def test_interrupted_ai_issue_preserves_failed_source_fingerprint(self):
        self.usage.issued.side_effect = azure.AnalysisFailure('azure_interrupted')
        self.collect()
        record = next(iter(self.state['sources'].values()))
        self.assertEqual(record['status'], 'failed')
        self.assertTrue(record['imageHashes'])
        self.assertFalse(self.state['pending'])
        self.model.structured.assert_not_called()

    def test_negative_fingerprint_and_text_only(self):
        self.model.structured.return_value = {'periods': []}
        self.collect()
        self.assertEqual(next(iter(self.state['sources'].values()))['status'], 'negative')
        self.assertFalse(self.state['schedules'])
        self.state = facts.empty_state()
        self.client.post.return_value = base.payload(photos=0), facts.digest(b'no-photo')
        self.model.structured.return_value = base.result(images=False)
        self.analyzer = azure.AzureAnalyzer(self.usage, clock=lambda: base.NOW, client=self.model)
        self.client.image.reset_mock()
        report, code = self.collect()
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'ok')
        self.client.image.assert_not_called()

    def test_saved_api0_bootstrap_and_old_contract_rejected(self):
        packet = collector.replay_saved(
            self.state, document=base.document(best=base.entry()),
            payload_bytes=json.dumps(base.payload()).encode(),
            images=[{'bytes': base.png(), 'mime': 'image/png'}], result=base.result(),
            schedule=base.SCHEDULE, insights={}, accounts=base.ACCOUNTS, post_id=base.post_id(),
            now=base.NOW, receipt_id=facts.digest(b'approved-mock-receipt'))
        self.assertEqual(packet['subject']['accountSource'], '公式サイト')
        self.assertEqual(packet['source']['payloadHash'], facts.digest(json.dumps(base.payload()).encode()))
        self.assertEqual(len(packet['schedules'][0]['days']), 6)
        self.assertEqual(collector.validate_saved_packet(packet), packet)
        with self.assertRaisesRegex(ValueError, 'saved_binding_mismatch'):
            collector.validate_saved_packet(packet, existing_bindings={
                'あむ': {'authorId': '2080944098043977888', 'authorScreenName': base.TARGET['handle']}})
        facts.apply_revision(self.state, packet['schedules'], packet['source'], packet['analysis'])
        facts.validate_state(self.state)
        with self.assertRaises(ValueError):
            collector.replay_saved(
                facts.empty_state(), document=base.document(best=base.entry()),
                payload_bytes=json.dumps(base.payload()).encode(),
                images=[{'bytes': base.png(), 'mime': 'image/png'}],
                result={'decision': 'schedule', 'shifts': []},
                schedule=base.SCHEDULE, insights={}, accounts=base.ACCOUNTS, post_id=base.post_id(),
                now=base.NOW, receipt_id=facts.digest(b'old-comparison-not-valid'))
        self.client.search.assert_not_called()
        self.model.structured.assert_not_called()

    def test_saved_same_post_v2_material_is_offline_not_implicit_apply_or_inference(self):
        options = dict(document=base.document(best=base.entry()),
                       payload_bytes=json.dumps(base.payload()).encode(),
                       images=[{'bytes': base.png(), 'mime': 'image/png'}],
                       schedule=base.SCHEDULE, insights={}, accounts=base.ACCOUNTS,
                       post_id=base.post_id(), now=base.NOW)
        legacy_raw = base.result(contract_version=facts.LEGACY_VERSION)
        before_raw = copy.deepcopy(legacy_raw)
        legacy = collector.replay_saved(
            self.state, **options, result=legacy_raw,
            contract_version=facts.LEGACY_VERSION, receipt_id=facts.digest(b'old-saved-receipt'))
        facts.apply_revision(self.state, legacy['schedules'], legacy['source'], legacy['analysis'])
        before = copy.deepcopy(self.state)
        packet = collector.replay_saved(
            self.state, **options,
            result=base.timing_result({5: [base.work_note()], 12: [base.work_note()]}),
            receipt_id=facts.digest(b'selected-new-receipt'))
        self.assertEqual(self.state, before)
        self.assertEqual(legacy_raw, before_raw)
        self.assertEqual(legacy['analysis']['contract'], facts.LEGACY_VERSION)
        self.assertEqual(packet['analysis']['contract'], facts.VERSION)
        self.assertEqual(packet['source']['bodyHash'], legacy['source']['bodyHash'])
        self.assertEqual(packet['analysis']['images'], legacy['analysis']['images'])
        self.assertEqual(packet['schedules'][0]['days'], legacy['schedules'][0]['days'])
        self.assertEqual(collector.validate_saved_packet(packet), packet)
        with self.assertRaisesRegex(ValueError, 'same_revision_conflict'):
            facts.apply_revision(self.state, packet['schedules'], packet['source'], packet['analysis'])
        self.assertEqual(self.state, before)
        self.client.search.assert_not_called()
        self.client.post.assert_not_called()
        self.client.image.assert_not_called()
        self.model.structured.assert_not_called()

    def test_unknown_saved_post_requires_discovery_not_only_id(self):
        with self.assertRaisesRegex(ValueError, 'saved_discovery_required'):
            collector.replay_saved(
                self.state, document=base.document(), payload_bytes=json.dumps(base.payload()).encode(),
                images=[{'bytes': base.png(), 'mime': 'image/png'}], result=base.result(),
                schedule=base.SCHEDULE, insights={}, accounts=base.ACCOUNTS, post_id=base.post_id(),
                now=base.NOW, receipt_id=facts.digest(b'mock'))

    def test_shared_ledgers_real_offline_round_trip(self):
        source_module = collector._module('source-state.py', 'test_schedule_source_shared')
        usage_module = collector._module('analysis-state.py', 'test_schedule_ai_shared')
        folder = self.work_dir()
        source_path, ai_path = folder / 'source-usage.json', folder / 'ai-usage.json'
        source_module.atomic_json(source_path, source_module.baseline_state(
            {'budgets': {}, 'paused': None}, source_hash=facts.digest(b'synthetic-approved-baseline'),
            at=base.NOW))
        usage_module.atomic_json(ai_path, usage_module.empty_state())
        clock = [base.NOW]
        def sleep(seconds):
            clock[0] += dt.timedelta(seconds=seconds)
        with source_module.SharedSource(source_path, run_id='schedule-test', component='schedule',
                                        clock=lambda: clock[0], sleep=sleep) as source:
            source.check('images', 4)
            receipt = source.reserve('images', 'https://pbs.twimg.com/media/SYNTHETIC.png')
            source.issued(receipt)
            source.finish(receipt)
            with self.assertRaises(Exception):
                source.reserve('images', 'https://pbs.twimg.com/media/SYNTHETIC?format=png&name=medium')
        with usage_module.SharedUsage(ai_path, run_id='schedule-test', component='schedule',
                                      clock=lambda: clock[0], sleep=sleep, request_limit=1) as usage:
            analyzer = azure.AzureAnalyzer(usage, clock=lambda: clock[0], client=self.model)
            verified, text, _ = base.source()
            result, proof = analyzer.analyze(verified, text, [{'bytes': base.png(), 'mime': 'image/png'}],
                                             facts.target_periods(base.NOW), lambda _: None)
            self.assertEqual(len(result), 1)
            self.assertEqual(usage.used, 1)
            self.assertIn(proof['receiptId'], usage.state['receipts'])

    def test_zero_allocation_persists_status_without_clients_or_secrets(self):
        source_module = collector._module('source-state.py', 'test_zero_schedule_source')
        usage_module = collector._module('analysis-state.py', 'test_zero_schedule_usage')
        folder = self.work_dir()
        snapshot, source_path, ai_path, personal_path = [
            folder / name for name in ('schedule.json', 'source.json', 'ai.json', 'personal.json')]
        personal_state = collector.personal.empty_state()
        collector.official.atomic_json(snapshot, facts.empty_state())
        collector.official.atomic_json(personal_path, personal_state)
        source_module.atomic_json(source_path, source_module.baseline_state(
            personal_state, source_hash=facts.digest(b'explicit-offline-baseline'), at=base.NOW))
        usage_module.atomic_json(ai_path, usage_module.empty_state())
        args = collector.argument_parser().parse_args([
            '--snapshot', str(snapshot), '--source-state', str(source_path),
            '--personal-snapshot', str(personal_path), '--ai-state', str(ai_path),
            '--http-state', str(folder / 'http.json'), '--analysis-run-id', 'zero-test',
            '--analysis-limit', '0'])
        with mock.patch.object(collector, 'SourceClient', side_effect=AssertionError('no source client')):
            report, code = collector.run(args, clock=lambda: base.NOW, environment={})
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], facts.read_state(snapshot)['lastRun']['status'])
        self.assertEqual(report['exitCode'], code)
        self.assertFalse(usage_module.load_state(ai_path)['receipts'])
        self.assertFalse(source_module.load_state(source_path)['receipts'])

    def test_full_shared_ai_budget_defers_before_any_client_or_credentials(self):
        source_module = collector._module('source-state.py', 'test_full_schedule_source')
        usage_module = collector._module('analysis-state.py', 'test_full_schedule_usage')
        folder = self.work_dir()
        snapshot, source_path, ai_path, personal_path, report_path = [
            folder / name for name in ('schedule.json', 'source.json', 'ai.json', 'personal.json', 'report.json')]
        personal_state = collector.personal.empty_state()
        collector.official.atomic_json(snapshot, facts.empty_state())
        collector.official.atomic_json(personal_path, personal_state)
        source_module.atomic_json(source_path, source_module.baseline_state(
            personal_state, source_hash=facts.digest(b'explicit-offline-baseline'), at=base.NOW))
        usage_module.atomic_json(ai_path, usage_module.empty_state())
        clock = [base.NOW]
        def sleep(seconds):
            clock[0] += dt.timedelta(seconds=seconds)
        with usage_module.SharedUsage(ai_path, run_id='12345-1', component='official',
                                      clock=lambda: clock[0], sleep=sleep) as usage:
            for index in range(3):
                key = facts.digest(('MOCK-ONLY-' + str(index)).encode())
                usage.reserve(key, self.model.identity)
                usage.issued(key)
                usage.finish(key, 'no_event')
        args = collector.argument_parser().parse_args([
            '--once', '--snapshot', str(snapshot), '--source-state', str(source_path),
            '--personal-snapshot', str(personal_path), '--ai-state', str(ai_path),
            '--http-state', str(folder / 'http.json'), '--analysis-run-id', '12345-1',
            '--source-run-id', '12345-1', '--analysis-limit', '1',
            '--max-searches', '1', '--max-posts', '1', '--max-images', '4', '--report', str(report_path)])
        with mock.patch.object(collector, 'SourceClient', side_effect=AssertionError('no source client')), \
                mock.patch.object(azure, 'AzureAnalyzer', side_effect=AssertionError('no Azure client')):
            report, code = collector.run(args, clock=lambda: clock[0], environment={})
        self.assertEqual(code, 2)
        self.assertTrue(report['completed'])
        self.assertEqual(report['collectionStatus'], 'budget-exhausted')
        self.assertEqual(report['exitCode'], 2)
        self.assertEqual(report['requests'], {'searches': 0, 'posts': 0, 'images': 0})
        self.assertEqual(report['analysisRequests'], 0)
        self.assertEqual(report['status'], facts.read_state(snapshot)['lastRun']['status'])
        self.assertFalse(source_module.load_state(source_path)['receipts'])
        self.assertEqual(len(usage_module.load_state(ai_path)['receipts']), 3)
        cloud = collector._module('cloud-collection.py', 'test_schedule_completion_contract')
        self.assertEqual(cloud.validate_completion(report_path, 'budget-exhausted', 2, 'schedule'), report)

    def test_run_attests_all_normal_exit_codes_at_report_write(self):
        source_module = collector._module('source-state.py', 'test_completion_source')
        usage_module = collector._module('analysis-state.py', 'test_completion_usage')
        folder = self.work_dir()
        snapshot, source_path, ai_path, personal_path, report_path = [
            folder / name for name in ('schedule.json', 'source.json', 'ai.json', 'personal.json', 'report.json')]
        personal_state = collector.personal.empty_state()
        collector.official.atomic_json(snapshot, facts.empty_state())
        collector.official.atomic_json(personal_path, personal_state)
        source_module.atomic_json(source_path, source_module.baseline_state(
            personal_state, source_hash=facts.digest(b'mock-baseline'), at=base.NOW))
        usage_module.atomic_json(ai_path, usage_module.empty_state())
        args = collector.argument_parser().parse_args([
            '--snapshot', str(snapshot), '--source-state', str(source_path),
            '--personal-snapshot', str(personal_path), '--ai-state', str(ai_path),
            '--http-state', str(folder / 'http.json'), '--analysis-run-id', 'report-test',
            '--report', str(report_path)])
        cloud = collector._module('cloud-collection.py', 'test_all_schedule_completions')
        for status, code in [('ok', 0), ('partial', 2), ('unavailable', 3)]:
            def collection(state, *unused, **kwargs):
                state['lastRun'] = {'status': status}
                kwargs['save']()
                return {'component': 'schedule', 'status': status,
                        'requests': {'searches': 0, 'posts': 0, 'images': 0}}, code
            with self.subTest(status=status), \
                    mock.patch.object(collector, 'collect', side_effect=collection), \
                    mock.patch.object(collector, 'SourceClient'), \
                    mock.patch.object(azure, 'AzureAnalyzer'), \
                    mock.patch.object(collector.personal, 'read_js', side_effect=[base.SCHEDULE, {}]):
                report, actual_code = collector.run(args, clock=lambda: base.NOW, environment={})
            self.assertEqual(actual_code, code)
            self.assertEqual(report['exitCode'], code)
            self.assertEqual(cloud.validate_completion(report_path, status, code, 'schedule'), report)

    def test_main_local_failure_cannot_attest_a_stale_report(self):
        path = self.work_dir() / 'report.json'
        path.write_text('{"stale":true}', encoding='utf-8')
        argv = ['--snapshot', 'unused.json', '--source-state', 'source.json',
                '--personal-snapshot', 'personal.json', '--http-state', 'http.json',
                '--ai-state', 'ai.json', '--analysis-run-id', 'mock', '--report', str(path)]
        with mock.patch.object(collector, 'run', side_effect=ValueError('SYNTHETIC_RAW_NOT_FOR_LOGS')), \
                mock.patch('builtins.print') as output:
            code = collector.main(argv)
        self.assertEqual(code, 1)
        self.assertEqual(path.read_text(encoding='utf-8'), '{"stale":true}')
        emitted = json.loads(output.call_args.args[0])
        self.assertFalse(emitted['completed'])
        self.assertEqual(emitted['exitCode'], 1)
        self.assertNotIn('SYNTHETIC_RAW_NOT_FOR_LOGS', output.call_args.args[0])

    def test_failed_run_preserves_success_and_monotonic_check_time(self):
        self.state.update(checkedAt=facts.stamp(base.NOW + dt.timedelta(seconds=10)),
                          lastSuccessAt=facts.stamp(base.NOW + dt.timedelta(seconds=10)))
        clock = [base.NOW + dt.timedelta(seconds=9)]
        def unavailable():
            clock[0] += dt.timedelta(seconds=7)
            raise azure.AnalysisFailure('azure_budget_exhausted')
        self.analyzer.check = unavailable
        report, code = collector.collect(
            self.state, base.SCHEDULE, {}, base.ACCOUNTS, self.client, self.source, self.analyzer,
            clock=lambda: clock[0], save=self.save)
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'budget-exhausted')
        self.assertEqual(self.state['lastSuccessAt'], facts.stamp(base.NOW + dt.timedelta(seconds=10)))
        self.assertEqual(self.state['checkedAt'], facts.stamp(clock[0]))
        self.assertTrue(all(facts.timestamp(saved['checkedAt']) >=
                            facts.timestamp(saved['lastSuccessAt']) for saved in self.saved))
        self.client.search.assert_not_called()
        self.client.post.assert_not_called()

    def native_pipeline(self, *, start=base.NOW, model_result=None):
        """Real native/ledger/transport code with synthetic HTTP responses and moving time."""
        cloud = collector._module('cloud-collection.py', 'moving_native_cloud')
        sources = collector._module('source-state.py', 'moving_native_sources')
        usage = collector._module('analysis-state.py', 'moving_native_usage')
        root = self.work_dir()
        canonical = root / 'canonical'
        canonical.mkdir()
        data = root / 'data'
        data.mkdir()
        inputs = root / 'inputs'
        inputs.mkdir()
        paths = {'snapshot': canonical / cloud.HALF_MONTH, 'source-state': canonical / cloud.SOURCE_USAGE,
                 'personal-snapshot': canonical / cloud.PERSONAL, 'ai-state': canonical / cloud.AI_USAGE,
                 'http-state': canonical / cloud.HTTP_STATE, 'report': root / 'report.json',
                 'schedule': inputs / 'schedule.js', 'insights': inputs / 'store-insights.js',
                 'accounts': inputs / 'accounts.csv'}
        paths['schedule'].write_text('window.SCHEDULE_DATA=' + json.dumps(base.SCHEDULE) + ';', encoding='utf-8')
        paths['insights'].write_text('window.STORE_INSIGHTS={};', encoding='utf-8')
        paths['accounts'].write_text('name,handle,source\nあむ,amu_zettai,公式サイト\n', encoding='utf-8')
        personal = collector.personal.empty_state()
        collector.official.atomic_json(paths['snapshot'], facts.empty_state())
        collector.official.atomic_json(paths['personal-snapshot'], personal)
        collector.official.atomic_json(paths['http-state'], {'schemaVersion': 1, 'cooldowns': {}})
        collector.official.atomic_json(canonical / cloud.SNAPSHOT, collector.official.empty_snapshot())
        sources.atomic_json(paths['source-state'], sources.baseline_state(
            personal, source_hash=facts.digest(b'advancing-clock-offline-baseline'), at=start))
        usage.atomic_json(paths['ai-state'], usage.empty_state())
        clock, sleeps = [start], []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += dt.timedelta(seconds=seconds)
        value = base.result() if model_result is None else model_result
        post = base.payload(photos=2)
        if model_result is not None:
            post['text'] = '9月全月のお給仕予定'
        source_opener, model_opener = mock.Mock(), mock.Mock()
        issued_urls = []
        def open_response(request, timeout):
            clock[0] += dt.timedelta(seconds=2)
            url = request.full_url
            issued_urls.append(url)
            if url.startswith('https://search.yahoo.co.jp/'):
                raw, mime = base.document(best=base.entry()).encode(), 'text/html'
            elif url.startswith('https://cdn.syndication.twimg.com/'):
                raw, mime = json.dumps(post).encode(), 'application/json'
            elif url.startswith('https://pbs.twimg.com/'):
                raw, mime = base.png(), 'image/png'
            elif url == 'https://offline.openai.azure.com/openai/v1/chat/completions':
                raw = json.dumps({'model': 'gpt-5.6-luna-2026-07-09', 'choices': [
                    {'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}).encode()
                mime = 'application/json'
            else:
                raise AssertionError('unexpected_mock_route')
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.getcode.return_value = 200
            response.headers = {'Content-Type': mime}
            response.read.side_effect = io.BytesIO(raw).read
            return response
        source_opener.open.side_effect = open_response
        model_opener.open.side_effect = open_response
        source_type, analyzer_type = collector.SourceClient, azure.AzureAnalyzer
        def client_factory(shared, **kwargs):
            return source_type(shared, opener=source_opener, **kwargs)
        def analyzer_factory(shared, environment, **kwargs):
            transport = azure.transport.AzureOpenAI(environment, on_http_failure=shared.http_failure,
                                                     opener=model_opener)
            return analyzer_type(shared, client=transport, **kwargs)
        argv = [value for key, path in paths.items() for value in ('--' + key, str(path))]
        argv.extend(['--analysis-run-id', 'advancing-native', '--source-run-id', 'advancing-native'])
        with mock.patch.object(collector, 'SourceClient', side_effect=client_factory), \
                mock.patch.object(azure, 'AzureAnalyzer', side_effect=analyzer_factory):
            report, code = collector.run(
                collector.argument_parser().parse_args(argv), clock=lambda: clock[0], sleep=sleep,
                environment={'AZURE_OPENAI_ENDPOINT': 'https://offline.openai.azure.com',
                             'AZURE_OPENAI_API_KEY': 'SYNTHETIC_OFFLINE_ONLY'})
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['requests'], {'searches': 1, 'posts': 1, 'images': 2})
        self.assertEqual(report['analysisRequests'], 1)
        self.assertEqual(len(issued_urls), 5)
        self.assertTrue(sleeps)
        self.assertGreater(clock[0], start + dt.timedelta(seconds=10))
        state = facts.read_state(paths['snapshot'])
        self.assertEqual(state['checkedAt'], facts.stamp(clock[0]))
        self.assertEqual(state['lastSuccessAt'], state['checkedAt'])
        self.assertEqual(cloud.validate_completion(paths['report'], 'ok', 0, 'schedule'), report)
        ledger = usage.load_state(paths['ai-state'], required=True)
        self.assertEqual(len(ledger['receipts']), 1)
        self.assertEqual(next(iter(ledger['receipts'].values()))['reason'], 'events')
        self.assertEqual(len(sources.load_state(paths['source-state'], required=True)['receipts']), 4)
        cloud.copy_pair(canonical, data, collector.official, include_personal=True,
                        personal=collector.personal, include_ai=True, include_half_month=True, include_source=True)
        self.assertEqual((data / cloud.HALF_MONTH).read_bytes(), paths['snapshot'].read_bytes())
        return root, paths, state, cloud, clock[0]

    def test_advancing_native_ledgers_cloud_pages_feed_passes_actual_js_validator(self):
        root, paths, state, cloud, finished = self.native_pipeline()
        pages = collector._module('pages.py', 'advancing_native_pages')
        shutil.copyfile(base.TOOLS.parent / 'app.js', root / 'app.js')
        (root / 'index.html').write_text('<!doctype html><html><body></body></html>', encoding='utf-8')
        (root / 'styles.css').write_text('body {}', encoding='utf-8')
        shutil.copyfile(paths['schedule'], root / 'data' / 'schedule.js')
        shutil.copyfile(paths['insights'], root / 'data' / 'store-insights.js')
        shutil.copytree(base.TOOLS.parent / 'assets' / 'events', root / 'assets' / 'events')
        site = root / 'site'
        pages.stage(site, 'a' * 40, root=root, clock=lambda: finished)
        public = json.loads((site / 'data' / cloud.HALF_MONTH).read_bytes())
        self.assertEqual(public, facts.public_state(state))
        self.assertEqual((site / 'app.js').read_bytes(), (base.TOOLS.parent / 'app.js').read_bytes())
        script = """
const fs=require('node:fs'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const api=require(input.app);
const feed=JSON.parse(fs.readFileSync(input.feed,'utf8'));
assert.equal(api.validateHalfMonthSchedules(feed,{roster:['あむ']}),feed);
assert(Date.parse(feed.lastSuccessAt)<=Date.parse(feed.checkedAt));
assert(Date.parse(feed.checkedAt)>Date.parse(input.startedAt));
assert.throws(()=>api.validateHalfMonthSchedules({...feed,checkedAt:input.startedAt}));
process.stdout.write(String(feed.schedules[0].days.length));
"""
        options = {}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
            options['startupinfo'] = subprocess.STARTUPINFO()
            options['startupinfo'].dwFlags |= subprocess.STARTF_USESHOWWINDOW
            options['startupinfo'].wShowWindow = 0
        node = shutil.which('node') or str(collector.personal.NODE_FALLBACK)
        result = subprocess.run(
            [node, '-e', script], input=json.dumps({'app': str(site / 'app.js'),
             'feed': str(site / 'data' / cloud.HALF_MONTH), 'startedAt': facts.stamp(base.NOW)}),
            capture_output=True, text=True, encoding='utf-8', timeout=15, check=True,
            env={'SystemRoot': os.environ.get('SystemRoot', r'C:\Windows')}, **options)
        self.assertEqual(result.stdout, '6')

    def test_native_full_month_keeps_current_half_and_success_fingerprint(self):
        full = base.result([(2, '水', ['夜']), (20, '日', ['昼'])], half='full')
        _, _, state, _, _ = self.native_pipeline(
            start=dt.datetime(2026, 9, 20, tzinfo=facts.UTC), model_result=full)
        self.assertEqual(len(state['schedules']), 1)
        self.assertEqual(state['schedules'][0]['days'], [{'date': '2026-09-20', 'shifts': ['昼']}])
        self.assertEqual(next(iter(state['sources'].values()))['status'], 'valid')


@unittest.skipUnless(os.environ.get('HALF_MONTH_SAVED_SOURCE_DIR'), 'optional private saved sources not supplied')
class SavedSourceReplayTests(base.Offline):
    def test_real_saved_sources_with_clearly_mocked_result_api0(self):
        folder = Path(os.environ['HALF_MONTH_SAVED_SOURCE_DIR'])
        raw = (folder / 'post-2096568567798128871.json').read_bytes()
        photo = (folder / 'ito-schedule-2096568567798128871.jpg').read_bytes()
        html = (folder / 'yahoo-id.html').read_text(encoding='utf-8')
        self.assertEqual(facts.digest(raw), '4780d92c1e8d2e3d90e687d2bb9f63b872d84a3a8ed4739d2d72e95506cd92bf')
        self.assertEqual(facts.digest(photo), '863b6b93878144c164d51d4ddb0c581ef30eb0ceccbbc7e00597499161850df6')
        self.assertEqual(facts.digest(html.encode()), '5b4c44772cbcb7008a155d6f43c4fe6d54876ed01639edb3032e58f2aa6cd5b4')
        schedule = collector.personal.read_js(base.TOOLS.parent / 'data' / 'schedule.js', 'SCHEDULE_DATA')
        insights = collector.personal.read_js(base.TOOLS.parent / 'data' / 'store-insights.js', 'STORE_INSIGHTS')
        with (base.TOOLS / 'data' / 'accounts.csv').open(encoding='utf-8-sig', newline='') as stream:
            accounts = list(csv.DictReader(stream))
        self.assertEqual(len(schedule['roster']), 40)
        packet = collector.replay_saved(
            facts.empty_state(), document=html, payload_bytes=raw,
            images=[{'bytes': photo, 'mime': 'image/jpeg'}],
            result=base.timing_result({5: [base.work_note()], 12: [base.work_note()]}),
            schedule=schedule, insights=insights, accounts=accounts, post_id='2096568567798128871',
            now=dt.datetime(2026, 9, 6, 22, 34, tzinfo=facts.UTC),
            receipt_id=facts.digest(b'SYNTHETIC-MOCK-RESULT-NOT-CANONICAL-OR-STABLE-LIVE'))
        collector.validate_saved_packet(packet)
        image = packet['analysis']['images'][0]
        self.assertEqual((image['bytes'], image['width'], image['height']), (239442, 900, 1200))
        self.assertEqual(packet['source']['media'][0]['originalWidth'], 1536)
        self.assertEqual(packet['source']['media'][0]['originalHeight'], 2048)
        self.assertEqual(packet['source']['name'], 'いと')
        self.assertEqual(packet['source']['authorId'], '2080944098043977728')
        self.assertEqual([(row['date'], row['shifts']) for row in packet['schedules'][0]['days']],
                         [('2026-09-02', ['夜']), ('2026-09-05', ['昼']), ('2026-09-07', ['昼']),
                          ('2026-09-10', ['夜']), ('2026-09-12', ['昼']), ('2026-09-14', ['昼'])])
        self.assertEqual(packet['schedules'][0]['period']['yearBasis'], 'post-context')
        self.assertEqual([(note['serviceDate'], note['shift'], note['boundary'],
                           note['qualifier'], note['explicitTime'])
                          for note in packet['schedules'][0]['workTiming']['facts']],
                         [('2026-09-05', '昼', 'end', 'long', None),
                          ('2026-09-12', '昼', 'end', 'long', None)])


if __name__ == '__main__':
    unittest.main()
