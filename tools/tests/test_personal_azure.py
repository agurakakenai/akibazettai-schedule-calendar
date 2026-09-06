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
from test_analysis_state import historical, usage


personal, azure = base.personal, base.personal.azure
ENV = {'AZURE_OPENAI_ENDPOINT': 'https://offline.openai.azure.com/',
       'AZURE_OPENAI_DEPLOYMENT': 'gpt-5.6-luna',
       'AZURE_OPENAI_API_KEY': 'OFFLINE_AZURE_SENTINEL'}


def event(shift, kind, line_ids, store=None, time=None):
    return {'shift': shift, 'kind': kind, 'evidenceLineIds': line_ids, 'storeId': store, 'time': time}


def legacy_result(*events, decision=None):
    return {'date': base.DATE.isoformat(), 'decision': decision or ('events' if events else 'no_event'),
            'events': list(events)}


def link(scope, status='work', line_ids=None):
    return {'scope': scope, 'status': status, 'evidenceLineIds': [1] if line_ids is None else line_ids}


def result(*events, decision=None, links=(), link_decision=None):
    return {**legacy_result(*events, decision=decision), 'links': list(links),
            'linkDecision': link_decision or ('links' if links else 'no_link')}


def response(value=None, *, content=None, refusal=None, finish='stop'):
    envelope = {'model': 'gpt-5.6-luna-2026-07-09', 'choices': [{'finish_reason': finish, 'message': {
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

    def make_analyzer(self, environment=None, usage=None):
        return azure.AzureAnalyzer(self.state, self.save, personal.azure_context(),
                                   ENV if environment is None else environment,
                                   clock=lambda: self.clock, sleep=self.sleep, opener=self.opener, usage=usage)

    def shared_usage(self, **kwargs):
        path = self.folder / 'ai-usage.json'
        if not path.exists():
            usage.atomic_json(path, usage.empty_state())
        return usage.SharedUsage(path, run_id='personal-test', component='personal',
                                 clock=lambda: self.clock, sleep=self.sleep, **kwargs)

    def test_shared_grounding_cache_identity_and_legacy_budget_are_preserved(self):
        text = '今日🍓\r\n3号店ひる→2号店よる\r\nPRIVATE_BODY_SENTINEL'
        self.analyzer.state['budgets']['2026-09-06'] = 30
        original_budgets = copy.deepcopy(self.analyzer.state['budgets'])
        expected = result(event('昼', 'placement', [2], 's3'), event('夜', 'placement', [2], 's2'))
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            self.assertEqual(analyzer.version, self.analyzer.version)
            for _ in range(2):
                post, _ = self.parse(text, expected, analyzer=analyzer)
            self.assertEqual([item['storeId'] for item in post['events']], ['s3', 's2'])
            self.assertEqual(ledger.used, 1)
            self.assertEqual(analyzer.used, 1)
            self.assertEqual(analyzer.state['budgets'], original_budgets)
            self.assertIsNone(analyzer.state['nextRequestAt'])
            receipt = next(iter(ledger.state['receipts'].values()))
            self.assertEqual(receipt['reason'], 'events')
            self.assertIsNotNone(receipt['issuedAt'])
            self.assertIsNotNone(receipt['completedAt'])
            self.assertEqual(receipt['identity']['model'], 'gpt-5.6-luna')
            encoded = json.dumps(ledger.state)
            for private in ('PRIVATE_BODY_SENTINEL', 'bodyLines', 'evidenceLineIds',
                            ENV['AZURE_OPENAI_API_KEY'], 'offline.openai.azure.com'):
                self.assertNotIn(private, encoded)
        self.opener.open.assert_called_once()
        personal.read_state(self.snapshot)

    def test_shared_saved_case_grounding_failure_is_negative_cached_without_source(self):
        self.known()
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            self.opener.open.return_value = response(result(event('昼', 'placement', [1], 's4')))
            _, code, client = self.collect(payloads={base.TID: base.post('今日 昼1号店')}, analyzer=analyzer)
            self.assertEqual(code, 3)
            self.assertEqual(self.state['pending'][0]['reason'], 'azure_ungrounded')
            client.search.assert_not_called()
            client.fetch_post.assert_not_called()
            _, _, client = self.collect(payloads={base.TID: base.post('今日 昼1号店')}, analyzer=analyzer)
            client.search.assert_not_called()
            client.fetch_post.assert_not_called()
            self.assertEqual(ledger.used, 1)
            self.assertEqual(next(iter(ledger.state['receipts'].values()))['reason'], 'azure_ungrounded')
        self.opener.open.assert_called_once()

    def test_shared_http_marker_is_durable_before_network_and_timeout_is_not_retried(self):
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)

            def failed(*args, **kwargs):
                receipt = next(iter(usage.load_state(ledger.path)['receipts'].values()))
                self.assertIsNotNone(receipt['issuedAt'])
                self.assertIsNone(receipt['completedAt'])
                raise TimeoutError('PRIVATE_ERROR_SENTINEL')

            self.opener.open.side_effect = failed
            for _ in range(2):
                with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_timeout'):
                    self.parse('今日 昼1号店', result(), analyzer=analyzer)
            self.assertEqual(ledger.used, 1)
            self.assertNotIn('PRIVATE_ERROR_SENTINEL', ledger.path.read_text())
        self.opener.open.assert_called_once()

    def test_shared_issue_save_crossing_18h_stops_before_transport_without_refund(self):
        self.clock = dt.datetime(2026, 9, 6, 8, 59, 59, tzinfo=dt.timezone.utc)
        cutoff = self.clock + dt.timedelta(seconds=1)
        with self.shared_usage(deadline=lambda: self.clock < cutoff) as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            save = ledger._save

            def slow_issue_save():
                save()
                if any(item['issuedAt'] is not None and item['completedAt'] is None
                       for item in ledger.state['receipts'].values()):
                    self.clock += dt.timedelta(seconds=2)

            with mock.patch.object(ledger, '_save', side_effect=slow_issue_save):
                with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_deadline'):
                    self.parse('今日 昼1号店', result(), analyzer=analyzer)
            self.assertEqual(ledger.used, 1)
            receipt = next(iter(usage.load_state(ledger.path)['receipts'].values()))
            self.assertIsNotNone(receipt['issuedAt'])
            self.assertEqual(receipt['reason'], 'azure_deadline')
        with self.shared_usage() as ledger:
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_deadline'):
                self.parse('今日 昼1号店', result(), analyzer=self.make_analyzer(usage=ledger))
            self.assertEqual(ledger.used, 1)
        self.opener.open.assert_not_called()
        personal.read_state(self.snapshot)

    def test_shared_issue_save_crossing_midnight_never_uses_new_day_capacity(self):
        for next_day_count in (0, 30):
            with self.subTest(next_day_count=next_day_count):
                self.clock = dt.datetime(2026, 9, 6, 14, 59, 59, tzinfo=dt.timezone.utc)
                self.state = personal.empty_state()
                usage.atomic_json(self.folder / 'ai-usage.json', usage.empty_state())
                with self.shared_usage() as ledger:
                    if next_day_count:
                        usage.apply_import(ledger.state, historical('2026-09-07', next_day_count))
                        ledger._save()
                    analyzer = self.make_analyzer(usage=ledger)
                    save = ledger._save

                    def slow_issue_save():
                        save()
                        if any(item['issuedAt'] is not None and item['completedAt'] is None
                               for item in ledger.state['receipts'].values()):
                            self.clock += dt.timedelta(seconds=2)

                    with mock.patch.object(ledger, '_save', side_effect=slow_issue_save):
                        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_interrupted'):
                            self.parse('今日 昼1号店', result(), analyzer=analyzer)
                    receipt = next(iter(usage.load_state(ledger.path)['receipts'].values()))
                    self.assertEqual(receipt['date'], '2026-09-06')
                    self.assertEqual(receipt['reason'], 'azure_interrupted')
                    self.assertEqual(ledger.used, 1)
                    self.assertEqual(usage.usage_counts(ledger.state, ledger.run_id, self.clock)['day'],
                                     next_day_count)
                    with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_interrupted'):
                        self.parse('今日 昼1号店', result(), analyzer=analyzer)
                personal.read_state(self.snapshot)
        self.opener.open.assert_not_called()

    def test_shared_crash_between_ledger_and_personal_save_never_reissues(self):
        text = '今日 昼1号店'
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            with mock.patch.object(analyzer, 'save', side_effect=OSError('offline disk failure')):
                with self.assertRaises(OSError):
                    self.parse(text, result(), analyzer=analyzer)
            self.assertEqual(ledger.used, 1)
            self.assertIsNone(next(iter(ledger.state['receipts'].values()))['issuedAt'])
        self.state = personal.empty_state()
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_interrupted'):
                self.parse(text, result(), analyzer=analyzer)
            self.assertEqual(ledger.used, 1)
        self.opener.open.assert_not_called()

    def test_shared_deadline_after_spacing_prevents_source_and_ai(self):
        self.known()
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            self.parse('今日 昼1号店', result(), analyzer=analyzer)
        deadline = self.clock + dt.timedelta(seconds=30)
        with self.shared_usage(deadline=lambda: self.clock < deadline) as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            self.opener.reset_mock()
            _, code, client = self.collect(payloads={base.TID: base.post('今日 昼2号店')}, analyzer=analyzer)
            self.assertEqual(code, 3)
            self.assertEqual(self.state['pending'][0]['reason'], 'azure_deadline')
            client.search.assert_not_called()
            client.fetch_post.assert_not_called()
            self.opener.open.assert_not_called()
            self.assertEqual(ledger.used, 1)

    def test_shared_401_and_429_pause_other_component_without_legacy_double_count(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.state = personal.empty_state()
                path = self.folder / 'ai-usage.json'
                usage.atomic_json(path, usage.empty_state())
                with self.shared_usage() as ledger:
                    analyzer = self.make_analyzer(usage=ledger)
                    self.opener.open.side_effect = urllib.error.HTTPError(
                        ENV['AZURE_OPENAI_ENDPOINT'], status, 'PRIVATE_ERROR_SENTINEL',
                        {'Retry-After': '900'}, None)
                    with self.assertRaises(azure.AnalysisFailure):
                        self.parse('今日 昼1号店', result(), analyzer=analyzer)
                    self.assertEqual(analyzer.state['budgets'], {})
                    self.assertIsNone(analyzer.state['paused'])
                    self.assertEqual(ledger.used, 1)
                with usage.SharedUsage(path, run_id='personal-test', component='official',
                                       clock=lambda: self.clock, sleep=self.sleep) as official:
                    with self.assertRaises(usage.UsageFailure):
                        official.reserve(azure.digest('official request'), self.analyzer.client.identity)
                    self.assertEqual(official.used, 0)
                personal.read_state(self.snapshot)
        self.assertEqual(self.opener.open.call_count, 3)

    def test_existing_legacy_pause_is_not_ignored_by_shared_mode(self):
        self.analyzer.state['paused'] = {'reason': 'azure_auth_stopped', 'httpStatus': 401,
                                         'at': base.CREATED}
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_auth_stopped'):
                analyzer.check()
            self.assertIsNone(ledger.state['paused'])
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_auth_stopped'):
                self.parse('今日 昼1号店', result(), analyzer=analyzer)
            self.assertEqual(ledger.state['paused']['httpStatus'], 401)
            self.assertEqual(ledger.used, 0)
        self.opener.open.assert_not_called()

    def test_shared_external_budget_cannot_be_treated_as_personal_zero(self):
        with self.shared_usage() as ledger:
            usage.apply_import(ledger.state, historical('2026-09-06', 30))
            ledger._save()
            analyzer = self.make_analyzer(usage=ledger)
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_budget_exhausted'):
                self.parse('今日 昼1号店', result(), analyzer=analyzer)
            self.assertEqual(ledger.used, 0)
            self.assertEqual(analyzer.state['budgets'], {})
        self.opener.open.assert_not_called()

    def test_shared_preflight_maps_deadline_without_source_or_negative_cache(self):
        for allowed, limit, reason in ((False, 3, 'outside_window'),
                                       (True, 0, 'azure_budget_exhausted')):
            with self.subTest(reason=reason):
                with self.shared_usage(request_limit=limit, deadline=lambda: allowed) as ledger:
                    analyzer = self.make_analyzer(usage=ledger)
                    previous = copy.deepcopy(analyzer.state)
                    with self.assertRaisesRegex(azure.AnalysisFailure, reason):
                        analyzer.check_capacity()
                    self.assertEqual(analyzer.state, previous)
                    self.assertEqual(ledger.used, 0)
                    self.assertEqual(ledger.state['receipts'], {})
        self.opener.open.assert_not_called()
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.snapshot.exists())

    def test_shared_adapter_does_not_convert_infrastructure_or_lookalike_failures(self):
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            for kind in (OSError, ValueError, RuntimeError):
                failure = kind('PRIVATE_INFRASTRUCTURE_ERROR')
                failure.reason, failure.status, failure.retry_at = 'azure_timeout', None, None
                with self.subTest(kind=kind), mock.patch.object(ledger, 'check', side_effect=failure):
                    with self.assertRaises(kind) as caught:
                        analyzer.check_capacity()
                    self.assertIs(caught.exception, failure)
            self.assertEqual(ledger.used, 0)
            self.assertEqual(analyzer.state['cache'], {})
        self.assertFalse(self.snapshot.exists())
        self.opener.open.assert_not_called()

    def test_shared_adapter_converts_only_valid_known_failures(self):
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            failure = ledger.failure_type('azure_rate_limited', 429, '2026-09-06T04:00:00Z')
            with mock.patch.object(ledger, 'check', side_effect=failure):
                with self.assertRaises(azure.AnalysisFailure) as caught:
                    analyzer.check_capacity()
                self.assertEqual(caught.exception.facts(), failure.facts())
            for invalid in (ledger.failure_type('PRIVATE_UNKNOWN_REASON'),
                            ledger.failure_type('azure_timeout', True)):
                with mock.patch.object(ledger, 'check', side_effect=invalid):
                    with self.assertRaises(ledger.failure_type) as caught:
                        analyzer.check_capacity()
                    self.assertIs(caught.exception, invalid)
            invalid = ledger.failure_type('azure_backoff', retry_at='not-a-timestamp')
            with mock.patch.object(ledger, 'check', side_effect=invalid):
                with self.assertRaises(ValueError):
                    analyzer.check_capacity()

    def test_legacy_capacity_checks_run_actual_day_and_pause_without_reserving(self):
        for cause, reason in (('run', 'azure_budget_exhausted'),
                              ('day', 'azure_budget_exhausted'),
                              ('pause', 'azure_auth_stopped')):
            with self.subTest(cause=cause):
                self.state = personal.empty_state()
                analyzer = self.make_analyzer()
                self.assertIsNone(analyzer.usage)
                self.assertIsNone(analyzer.check_capacity())
                if cause == 'run':
                    analyzer.used = azure.RUN_LIMIT
                elif cause == 'day':
                    analyzer.state['budgets'][base.DATE.isoformat()] = azure.DAY_LIMIT
                else:
                    analyzer.state['paused'] = {'reason': 'azure_auth_stopped', 'httpStatus': 403,
                                                 'at': base.CREATED}
                original = copy.deepcopy(analyzer.state)
                with self.assertRaisesRegex(azure.AnalysisFailure, reason):
                    analyzer.check_capacity()
                self.assertEqual(analyzer.state, original)
        self.opener.open.assert_not_called()
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.snapshot.exists())

    def test_legacy_capacity_uses_current_jst_day_without_changing_old_budget(self):
        analyzer = self.analyzer
        analyzer.state['budgets'][base.DATE.isoformat()] = azure.DAY_LIMIT
        self.clock = dt.datetime(2026, 9, 6, 15, tzinfo=dt.timezone.utc)
        original = copy.deepcopy(analyzer.state)
        self.assertIsNone(analyzer.check_capacity())
        self.assertEqual(analyzer.state, original)
        self.assertEqual(analyzer.used, 0)
        self.opener.open.assert_not_called()

    def parse(self, text, value, *, analyzer=None, target=base.AMU, tid=base.TID, created=base.CREATED):
        self.opener.open.return_value = response(value)
        return personal.validate_post(base.candidate(tid, created, target),
                                      base.post(text, tid, created, target), target, self.clock,
                                      roster=('あむ', 'ららこ'), analyzer=analyzer or self.analyzer)

    def validate(self, text, value, shifts=('昼', '夜')):
        return azure.grounded_events(value, text, base.DATE, shifts, personal.azure_context())

    def assess(self, text, value, shifts=('昼', '夜')):
        return azure.grounded_assessment(value, text, base.DATE, shifts, personal.azure_context())

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
        self.assertEqual(sent['model'], 'gpt-5.6-luna')
        self.assertEqual(sent['max_completion_tokens'], 1200)
        self.assertNotIn('tools', sent)
        self.assertNotIn(ENV['AZURE_OPENAI_API_KEY'], json.dumps(sent))

    def test_storeless_unspecified_link_uses_one_shared_inference_and_minimal_cache(self):
        text = '本日お給仕します\nPRIVATE_BODY_SENTINEL'
        expected = result(links=[link('unspecified')])
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            with mock.patch.object(personal, 'parse_events', side_effect=AssertionError('no rules fallback')):
                post, reason = self.parse(text, expected, analyzer=analyzer)
                first = copy.deepcopy(post)
                post['links'][0]['status'] = 'conflict'
                cached, cached_reason = self.parse(text, expected, analyzer=analyzer)
            self.assertEqual((reason, cached_reason), ('links', 'links'))
            self.assertEqual(cached, first)
            self.assertEqual(cached['events'], [])
            self.assertEqual(cached['links'], [{'scope': 'unspecified', 'status': 'work'}])
            self.assertEqual(ledger.used, 1)
            receipt = next(iter(ledger.state['receipts'].values()))
            self.assertEqual(receipt['reason'], 'links')
            self.assertIsNotNone(receipt['issuedAt'])
            self.assertIsNotNone(receipt['completedAt'])
        self.opener.open.assert_called_once()
        self.assertEqual(self.state['posts'], [])
        self.state['posts'] = [cached]
        public = personal.public_state(self.state)
        self.assertEqual(public['posts'][0]['events'], [])
        for encoded in (json.dumps(public), self.snapshot.read_text(encoding='utf-8')):
            for private in ('PRIVATE_BODY_SENTINEL', 'bodyLines', 'evidenceLineIds', text,
                            ENV['AZURE_OPENAI_API_KEY']):
                self.assertNotIn(private, encoded)
        personal.read_state(self.snapshot)

    def test_independent_pending_decisions_preserve_confirmed_counterpart(self):
        cases = (
            (result(decision='pending', links=[link('夜')]),
             ([], [{'scope': '夜', 'status': 'work'}], 'links')),
            (result(event('昼', 'placement', [1], 's1'), link_decision='pending'),
             ([{'shift': '昼', 'kind': 'placement', 'storeId': 's1', 'excerpt': '1号店'}], [], 'events')),
            (result(event('昼', 'placement', [1], 's1'), links=[link('昼')]),
             ([{'shift': '昼', 'kind': 'placement', 'storeId': 's1', 'excerpt': '1号店'}],
              [{'scope': '昼', 'status': 'work'}], 'events')),
        )
        for proposed, expected in cases:
            with self.subTest(proposed=proposed):
                self.assertEqual(self.assess('今日 昼1号店、夜もお給仕します', proposed), expected)
        self.assertEqual(self.assess('今日お給仕します', result(links=[link('unspecified')]), shifts=()),
                         ([], [{'scope': 'unspecified', 'status': 'work'}], 'links'))
        for event_decision, link_decision in (('pending', 'no_link'), ('no_event', 'pending'),
                                              ('pending', 'pending')):
            with self.subTest(event_decision=event_decision, link_decision=link_decision):
                with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_pending'):
                    self.assess('判断保留', result(decision=event_decision, link_decision=link_decision))
        self.assertEqual(self.assess('今日晴れ', result()), ([], [], 'no_event'))
        self.opener.open.assert_not_called()

    def test_link_only_successes_consume_the_same_shared_request_capacity(self):
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            for index in range(azure.RUN_LIMIT):
                post, reason = self.parse(f'本日お給仕します {index}',
                                          result(links=[link('unspecified')]), analyzer=analyzer)
                self.assertEqual((post['events'], reason), ([], 'links'))
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_budget_exhausted'):
                self.parse('本日お給仕します 追加', result(links=[link('unspecified')]), analyzer=analyzer)
            self.assertEqual(ledger.used, 3)
            self.assertEqual(len(ledger.state['receipts']), 3)
            self.assertEqual({item['reason'] for item in ledger.state['receipts'].values()}, {'links'})
            self.assertEqual(analyzer.state['budgets'], {})
            self.assertGreaterEqual(sum(self.sleeps), 120)
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual((azure.RUN_LIMIT, azure.DAY_LIMIT, azure.SPACING_SECONDS), (3, 30, 60))
        personal.read_state(self.snapshot)

    def test_explicit_negative_links_are_scoped_and_all_day_requires_known_shifts(self):
        for status in ('withdrawn', 'conflict'):
            with self.subTest(status=status):
                proposed = result(links=[link('昼', status), link('夜')])
                self.assertEqual(self.assess('本日昼の案内を訂正、夜お給仕します', proposed)[1],
                                 [{'scope': '昼', 'status': status}, {'scope': '夜', 'status': 'work'}])
                self.assertEqual(self.assess('本日終日お休み', result(links=[link('昼', status)]),
                                             shifts=('昼',))[1],
                                 [{'scope': '昼', 'status': status}])
                for shifts in ((), ('昼',)):
                    with self.assertRaises(azure.AnalysisFailure):
                        self.assess('本日終日お休み', result(links=[link('夜', status)]), shifts=shifts)
                with self.assertRaises(azure.AnalysisFailure):
                    self.assess('本日終日お休み', result(links=[link('unspecified', status)]))

    def test_v5_links_reject_bad_scope_status_fields_and_evidence_ids(self):
        text = '本日お給仕します\n\n夜もお給仕します\n'
        valid = link('夜', line_ids=[1, 3])
        bad_links = [
            {**valid, 'scope': '朝'}, {**valid, 'scope': []}, {**valid, 'scope': None},
            {**valid, 'status': 'pending'}, {**valid, 'status': []}, {**valid, 'status': True},
            {**valid, 'scope': 'unspecified', 'status': 'withdrawn'},
            {**valid, 'scope': 'unspecified', 'status': 'conflict'},
            {**valid, 'url': base.candidate()['url']}, {'scope': '夜', 'status': 'work'},
            None, [],
        ]
        for ids in (None, '1', 1, [], [0], [-1], [5], [True], [1.0], ['1'], [[1]], [1, 1],
                    [2], [1, 2], [4], list(range(1, azure.MAX_EVIDENCE_LINES + 2))):
            bad_links.append({**valid, 'evidenceLineIds': ids})
        for proposed in bad_links:
            with self.subTest(proposed=proposed), self.assertRaises(azure.AnalysisFailure):
                self.assess(text, result(links=[proposed]))
        for links in ([valid, valid], [valid] * 4):
            with self.subTest(links=links), self.assertRaises(azure.AnalysisFailure):
                self.assess(text, result(links=links))
        self.assertEqual(self.assess(text, result(links=[valid]))[1], [{'scope': '夜', 'status': 'work'}])
        boundary = '\n'.join(['今日お給仕'] * azure.MAX_SOURCE_LINES)
        self.assertEqual(self.assess(boundary, result(links=[link(
            'unspecified', line_ids=list(range(113, 129)))]))[2], 'links')
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_input_limit'):
            self.assess(boundary + '\n', result(links=[valid]))
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_input_limit'):
            self.assess('a' * (azure.MAX_INPUT_BYTES + 1), result(links=[valid]))

    def test_v5_requires_complete_contract_and_retains_event_grounding_checks(self):
        valid = result(event('昼', 'placement', [1], 's1'), links=[link('昼')])
        cases = [
            {key: value for key, value in valid.items() if key != 'links'},
            {key: value for key, value in valid.items() if key != 'linkDecision'},
            {**valid, 'linkDecision': 'no_link'}, {**valid, 'linkDecision': 'pending'},
            {**valid, 'linkDecision': None}, {**valid, 'links': None},
            {**valid, 'links': []}, {**valid, 'date': '2026-09-07'},
            {**valid, 'decision': 'no_event'}, {**valid, 'decision': 'pending'},
            {**valid, 'decision': []}, {**valid, 'events': None},
            {**valid, 'author': 'other'}, {**valid, 'events': valid['events'] * 2},
            result(links=[], link_decision='links'),
            result(event('昼', 'placement', [1], 's2'), links=[link('昼')]),
            result(event('昼', 'placement', [1, 2], 's1'), links=[link('昼')]),
        ]
        for proposed in cases:
            with self.subTest(proposed=proposed), self.assertRaises(azure.AnalysisFailure):
                self.assess('本日昼1号店\n', proposed)

    def test_normal_v5_path_rejects_v4_while_explicit_saved_v4_replay_remains_available(self):
        text = '今日昼1号店\n'
        old = legacy_result(event('昼', 'placement', [1, 2], 's1'))
        self.assertEqual(self.validate(text, old),
                         ([{'shift': '昼', 'kind': 'placement', 'storeId': 's1', 'excerpt': '1号店'}],
                          'events'))
        for _ in range(2):
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
                self.parse(text, old)
        self.opener.open.assert_called_once()
        self.assertEqual(next(iter(self.analyzer.state['cache'].values()))['reason'], 'azure_invalid_output')
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            self.validate(text, result(event('昼', 'placement', [1], 's1'), links=[link('昼')]))
        self.assertEqual(azure.VERSION, 'personal-line-ids-v5')
        with mock.patch.object(azure, 'VERSION', 'personal-line-ids-v4'):
            old_namespace = self.make_analyzer().version
        self.assertNotEqual(self.analyzer.version, old_namespace)

    def test_v5_prompt_keeps_semantic_decisions_in_one_model_request(self):
        text = '同じ日に投稿しただけ\n9/7の募集について話しています'
        self.parse(text, result())
        sent = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(json.loads(sent['messages'][1]['content'])['postedAt'], base.CREATED)
        self.assertEqual(len(sent['messages']), 2)
        for boundary in ('Publication', 'half-month schedules', 'third-party', 'recruitment',
                         'unspecified', 'already-displayed', 'independently', 'allowedShifts'):
            self.assertIn(boundary, sent['messages'][0]['content'])
        self.assertEqual(sent['model'], 'gpt-5.6-luna')
        self.assertEqual(sent['max_completion_tokens'], 1200)
        self.assertEqual(self.analyzer.client.identity['modelVersion'], '2026-07-09')
        schema = sent['response_format']['json_schema']['schema']
        self.assertEqual(set(schema['required']), {'decision', 'date', 'events', 'linkDecision', 'links'})
        self.assertEqual(schema['properties']['links']['maxItems'], 3)
        for field in ('events', 'links'):
            ids = schema['properties'][field]['items']['properties']['evidenceLineIds']
            self.assertEqual(ids['items']['enum'], [1, 2])
            self.assertEqual(ids['maxItems'], 2)
        self.opener.open.assert_called_once()

    def test_optional_link_cache_fields_validate_legacy_and_v5_results(self):
        entry = {'postId': base.TID, 'bodyHash': azure.digest('body'),
                 'versionHash': azure.digest('v4'), 'at': base.CREATED, 'reason': 'events',
                 'events': [{'shift': '昼', 'kind': 'placement', 'storeId': 's1', 'excerpt': '1号店'}]}
        for value in (entry, {**entry, 'reason': 'no_event', 'events': []},
                      {**entry, 'links': []},
                      {**entry, 'links': [{'scope': '昼', 'status': 'work'}]},
                      {**entry, 'reason': 'links', 'events': [],
                       'links': [{'scope': 'unspecified', 'status': 'work'}]}):
            state = {**azure.empty_state(), 'cache': {azure.digest('cache'): value}}
            azure.validate_state(state, personal.azure_context())
        invalid = [
            {**entry, 'reason': 'links'}, {**entry, 'reason': 'links', 'events': []},
            {**entry, 'reason': 'no_event', 'events': [], 'links': [{'scope': '昼', 'status': 'work'}]},
            {**entry, 'reason': 'azure_pending', 'events': [], 'links': [{'scope': '昼', 'status': 'work'}]},
            {**entry, 'links': [{'scope': '昼', 'status': 'work'}] * 2},
            {**entry, 'links': [{'scope': '昼', 'status': 'work'}] * 4},
            {**entry, 'links': [{'scope': 'unspecified', 'status': 'withdrawn'}]},
            {**entry, 'links': [{'scope': '昼', 'status': 'work', 'evidenceLineIds': [1]}]},
            {**entry, 'links': None},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                azure.validate_state({**azure.empty_state(), 'cache': {azure.digest('cache'): value}},
                                     personal.azure_context())

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
                events, _ = self.validate(text, legacy_result(event('夜', 'placement', [1], 's2')))
                self.assertEqual(events[0]['storeId'], 's2')
        # These deliberately wrong model decisions require model evaluation,
        # not another Japanese interpreter hidden in the mechanical validator.

    def test_shared_multishift_evidence_is_converted_once_per_event(self):
        text = '今日🍓\r\n　♡ 3号店ひる→2号店よる ꒱\r\n'
        selected = azure.selected_lines(azure.source_lines(text), [3, 2, 1])
        self.assertEqual([line['text'] for line in selected], ['今日🍓\r\n', '　♡ 3号店ひる→2号店よる ꒱\r\n', ''])
        post, _ = self.parse(text, result(
            event('昼', 'placement', [1, 2], 's3'), event('夜', 'placement', [2], 's2')))
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
                self.validate(text, legacy_result(proposed))
        events, _ = self.validate('今日\n3号店\n余談🍓\n夜19時に遅れます', legacy_result(
            event('夜', 'late', [2, 4], 's3', '19:00')))
        self.assertEqual(events[0]['storeId'], 's3')
        self.assertEqual(events[0]['time'], '19:00')

    def test_bad_line_id_types_duplicates_and_limits_are_rejected(self):
        text = '今日\n2号店\n夜\n'
        for ids in (None, '2', 2, [], [0], [-1], [5], [True], [2.0], ['2'], [[2]], [2, 2],
                    list(range(1, azure.MAX_EVIDENCE_LINES + 2))):
            with self.subTest(ids=ids), self.assertRaises(azure.AnalysisFailure):
                self.validate(text, legacy_result(event('夜', 'placement', ids, 's2')))
        with self.assertRaises(azure.AnalysisFailure):
            self.validate(text, legacy_result(event('夜', 'absence', [4])))
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
                self.validate(text, legacy_result(proposed))
        post, _ = self.parse('今日 夜４号店に１９時３０分から遅れて行きます', result(
            event('夜', 'late', [1], 's4', '19:30')))
        self.assertEqual(post['events'][0], {
            'shift': '夜', 'kind': 'late', 'storeId': 's4', 'time': '19:30', 'excerpt': '４号店'})
        for evidence in ('夜　２', '夜\u00a0２', '夜\t２'):
            with self.subTest(evidence=evidence):
                events, _ = self.validate('今日 ' + evidence, legacy_result(event('夜', 'placement', [1], 's2')))
                self.assertEqual(events[0]['storeId'], 's2')
                self.assertEqual(events[0]['excerpt'], evidence.replace('\t', ' '))

    def test_schema_target_boundaries_and_duplicate_events_fail_closed(self):
        text = '今日 昼1号店、夜2号店'
        valid = event('夜', 'placement', [1], 's2')
        cases = [legacy_result(valid, valid), legacy_result(valid, valid, valid),
                 {'date': '2026-09-07', 'decision': 'events', 'events': [valid]},
                 {'date': base.DATE.isoformat(), 'decision': 'no_event', 'events': [valid]},
                 {**legacy_result(valid), 'author': 'someone'},
                 legacy_result({**valid, 'confidence': 1}),
                 legacy_result({**valid, 'shift': '朝'}), legacy_result({**valid, 'shift': []}),
                 legacy_result({**valid, 'storeId': 's5'}), legacy_result({**valid, 'storeId': 2}),
                 legacy_result({**valid, 'kind': 'working'}), legacy_result({**valid, 'evidenceLineIds': None}),
                 legacy_result({'shift': '夜', 'kind': 'placement', 'storeId': 's2', 'time': None,
                         'evidence': '夜2号店'}),
                 legacy_result({**valid, 'storeId': None}),
                 legacy_result({**valid, 'time': '19:00'}),
                 legacy_result(event('夜', 'absence', [1], 's2')),
                 legacy_result(event('夜', 'return', [1], 's2', False))]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(azure.AnalysisFailure):
                self.validate(text, value)
        with self.assertRaises(azure.AnalysisFailure):
            self.validate(text, legacy_result(valid), shifts=('昼',))

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

    def test_explicit_saved_reanalysis_cache_edit_and_old_version_failure_are_separate(self):
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

    def test_version_change_never_automatically_reissues_known_v4_results(self):
        for reason in ('events', 'no_event', 'azure_pending'):
            with self.subTest(reason=reason):
                self.state = personal.empty_state()
                events = ([{'shift': '昼', 'kind': 'placement', 'storeId': 's1', 'excerpt': '1号店'}]
                          if reason == 'events' else [])
                old = {'postId': base.TID, 'bodyHash': azure.digest('今日昼1号店'),
                       'versionHash': azure.digest('saved-v4'), 'at': base.CREATED,
                       'reason': reason, 'events': events}
                self.state['azureAnalysis'] = {
                    **azure.empty_state(), 'cache': {azure.digest('saved-v4-key'): old}}
                if reason == 'azure_pending':
                    self.known()
                    self.state['pending'][0]['reason'] = reason
                else:
                    self.state['resolved'] = [{
                        'id': base.TID, 'name': 'あむ', 'url': base.candidate()['url'],
                        'date': base.DATE.isoformat(), 'resolvedAt': base.CREATED, 'reason': reason}]
                if events:
                    self.state['posts'] = [
                        personal.validate_post(base.candidate(), base.post(), base.AMU, self.clock)[0]]
                cache, posts = copy.deepcopy(self.state['azureAnalysis']['cache']), copy.deepcopy(self.state['posts'])
                analyzer = self.make_analyzer()
                _, _, client = self.collect(entries=[], analyzer=analyzer)
                client.fetch_post.assert_not_called()
                self.assertEqual(analyzer.state['cache'], cache)
                self.assertEqual(self.state['posts'], posts)
                self.assertEqual(analyzer.used, 0)
                self.assertEqual(analyzer.state['review'], {})
        self.opener.open.assert_not_called()

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
                    {**ENV, 'AZURE_OPENAI_DEPLOYMENT': 'gpt-5.4-nano'},
                    {**ENV, 'AZURE_OPENAI_DEPLOYMENT': 'gpt-5.4-mini-compare'},
                    {**ENV, 'AZURE_OPENAI_DEPLOYMENT': 'gpt-5.6-luna-compare'},
                    {**ENV, 'AZURE_OPENAI_ENDPOINT': 'https://example.com/'}):
            with self.assertRaisesRegex(ValueError, 'invalid_azure_configuration'):
                self.make_analyzer(env)
        self.parse('今日 昼1号店', result(event('昼', 'placement', [1], 's1')))
        self.assertNotIn('azureAnalysis', personal.public_state(self.state))
        self.assertNotIn(ENV['AZURE_OPENAI_API_KEY'], self.snapshot.read_text(encoding='utf-8'))
        self.assertNotIn('offline.openai.azure.com', self.snapshot.read_text(encoding='utf-8'))

    def test_production_model_identity_is_part_of_the_private_cache_namespace(self):
        self.assertEqual(self.analyzer.client.identity, {
            'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
            'deployment': 'gpt-5.6-luna', 'model': 'gpt-5.6-luna', 'modelVersion': '2026-07-09'})
        with mock.patch.object(azure.transport, 'MODEL_VERSION', '2026-07-10'):
            other = self.make_analyzer()
        self.assertNotEqual(self.analyzer.version, other.version)

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
        self.opener.open.return_value = response(result(
            event('昼', 'placement', [1], 's1'), links=[link('昼')]))
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

    def test_partial_saved_assessments_preserve_existing_events_and_links(self):
        self.known()
        self.opener.open.return_value = response(result(
            event('昼', 'placement', [1], 's1'), links=[link('昼')]))
        self.collect(payloads={base.TID: base.post('今日 昼1号店')})
        original = copy.deepcopy(self.state['posts'][0])
        self.opener.open.return_value = response(result(decision='pending', links=[link('夜')]))
        self.collect(payloads={base.TID: base.post('今日 夜お給仕します')})
        intermediate = copy.deepcopy(self.state['posts'][0])
        self.assertEqual(intermediate['events'], original['events'])
        self.assertEqual({item['scope']: item['status'] for item in intermediate['links']},
                         {'昼': 'work', '夜': 'work'})
        self.opener.open.return_value = response(result(
            event('昼', 'placement', [1], 's2'), link_decision='pending'))
        self.collect(payloads={base.TID: base.post('今日 昼2号店')})
        final = self.state['posts'][0]
        self.assertEqual(final['events'][0]['storeId'], 's2')
        self.assertEqual(final['links'], intermediate['links'])
        self.assertEqual(final['createdAt'], original['createdAt'])
        self.assertEqual(self.analyzer.state['history'], [original, intermediate])
        self.assertEqual(self.opener.open.call_count, 3)
        personal.read_state(self.snapshot)

    def test_refusal_never_withdraws_saved_links_or_uses_rule_fallback(self):
        self.known()
        self.opener.open.return_value = response(result(links=[link('unspecified')]))
        self.collect(payloads={base.TID: base.post('本日お給仕します')})
        before = copy.deepcopy(self.state['posts'])
        self.assertEqual(before[0]['events'], [])
        self.opener.open.return_value = response(result(), refusal='PRIVATE_REFUSAL')
        with mock.patch.object(personal, 'parse_events', side_effect=AssertionError('no rules fallback')):
            for _ in range(2):
                self.collect(payloads={base.TID: base.post('本日昼1号店に行きます')})
        self.assertEqual(self.state['posts'], before)
        self.assertEqual(self.state['pending'][0]['reason'], 'azure_refused')
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertNotIn('PRIVATE_REFUSAL', self.snapshot.read_text(encoding='utf-8'))
        personal.read_state(self.snapshot)

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
            event('昼', 'placement', [1, 2], 's3'), event('夜', 'placement', [2], 's2')))
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

    def test_link_only_collection_crosses_cloud_pages_and_ui_without_creating_work_facts(self):
        from test_cloud_collection import cloud
        from test_pages import pages

        executable = shutil.which('node') or str(personal.NODE_FALLBACK)
        if not Path(executable).is_file():
            self.skipTest('Existing Node runtime is unavailable')
        name, handle = base.AMU['name'], base.AMU['handle']
        schedule = {base.DATE.isoformat(): {shift: [{'name': name}] for shift in ('昼', '夜')}}
        self.targets = personal.select_targets(
            {'roster': [name], 'schedule': schedule}, {'maidTendency': {name: {'x': handle}}},
            [{'name': name, 'handle': handle, 'source': '本人確認済み'}], base.DATE, self.state)
        self.known()
        text = '本日お給仕します\nPRIVATE_CROSS_LAYER_BODY_SENTINEL'
        self.opener.open.return_value = response(result(links=[link('unspecified')]))
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            _, code, client = self.collect(payloads={base.TID: base.post(text)}, analyzer=analyzer)
            self.assertEqual(code, 0)
            self.assertEqual(ledger.used, 1)
            self.assertEqual(next(iter(ledger.state['receipts'].values()))['reason'], 'links')
        client.search.assert_not_called()
        client.fetch_post.assert_not_called()
        self.opener.open.assert_called_once()
        checked, raw = cloud.validate_personal(self.snapshot, personal)
        self.assertEqual(raw, self.snapshot.read_bytes())
        self.assertEqual(checked, personal.read_state(self.snapshot))
        self.assertTrue(checked['azureAnalysis']['cache'])
        self.assertTrue(checked['coverage'][base.DATE.isoformat()][name]['linkScopes'])
        public = pages.personal_projection(checked, collector=personal.official)
        self.assertEqual(public['posts'][0]['events'], [])
        self.assertEqual(public['posts'][0]['links'], [{'scope': 'unspecified', 'status': 'work'}])
        self.assertEqual(set(public), {'schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt',
                                      'posts', 'lastRun'})
        self.assertEqual(set(public['posts'][0]), {
            'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt', 'observedAt',
            'date', 'events', 'links'})
        encoded = json.dumps(public, ensure_ascii=False)
        for private in ('PRIVATE_CROSS_LAYER_BODY_SENTINEL', text, 'bodyLines', 'evidenceLineIds',
                        'azureAnalysis', 'cache', 'coverage', 'receipts', 'bodyHash', 'versionHash',
                        'identityBindings', 'pending', 'resolved', 'budgets', ENV['AZURE_OPENAI_API_KEY']):
            self.assertNotIn(private, encoded)
        script = """
const assert=require('node:assert/strict'),fs=require('node:fs'),api=require('./app.js');
const snapshot=api.validatePersonalShifts(JSON.parse(fs.readFileSync(0,'utf8')));
const post=snapshot.posts[0],shifts=['昼','夜'];
assert.deepEqual(post.events,[]);
const insights={stores:['s1','s2','s3','s4'].map(id=>({id})),
  maidTendency:{[post.name]:{x:post.authorScreenName}}};
const schedule={[post.date]:Object.fromEntries(shifts.map(shift=>[shift,[{name:post.name}]]))};
const original=JSON.stringify([snapshot,insights,schedule]),urls=[];
for(const shift of shifts){
  const options={insights,dateKey:post.date,shift,schedule,roster:[post.name],personal:snapshot};
  const displayed=api.resolveShiftRoster(options);
  assert.deepEqual(displayed,api.resolveShiftRoster({...options,personal:null}));
  assert.deepEqual(displayed.entries,[{name:post.name}]);
  assert.equal(displayed.assignment,null);
  assert.deepEqual(displayed.personal.posts,[]);
  assert.equal(displayed.personal.byMaid.size,0);
  assert.deepEqual(api.resolveShiftRoster({...options,schedule:{}}).entries,[]);
  const source=api.personalPostLink({...options,name:displayed.entries[0].name});
  assert.equal(source.url,post.url);
  urls.push(source.url);
}
assert.equal(api.dayHasPersonStoreEvidence(insights,null,post.date,snapshot),false);
assert.equal(JSON.stringify([snapshot,insights,schedule]),original);
process.stdout.write(JSON.stringify({urls,events:post.events}));
"""
        process = subprocess.run(
            [executable, '-e', script], cwd=personal.ROOT, input=encoded,
            capture_output=True, encoding='utf-8', timeout=10, check=True,
            env={key: value for key, value in os.environ.items() if not key.startswith('AZURE_OPENAI_')},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(json.loads(process.stdout), {
            'urls': [base.candidate()['url'], base.candidate()['url']], 'events': []})


if __name__ == '__main__':
    unittest.main()
