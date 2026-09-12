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


def result_v5(*events, decision=None, links=(), link_decision=None):
    return {**legacy_result(*events, decision=decision), 'links': list(links),
            'linkDecision': link_decision or ('links' if links else 'no_link')}


def result_v6(*events, links=(), pending_events=False):
    return {'date': base.DATE.isoformat(), 'events': None if pending_events else list(events),
            'links': None if links is None else list(links)}


def dated(value, day=base.DATE.isoformat()):
    return {'serviceDate': day, **value} if isinstance(value, dict) else value


def result(*events, links=(), pending_events=False, work_timing=()):
    return {'events': None if pending_events else [dated(value) for value in events],
            'links': None if links is None else [dated(value) for value in links],
            'workTiming': None if work_timing is None else list(work_timing)}


def timing_fact(shift='昼', boundary='end', qualifier=None, explicit_time='16:00',
                status='set', day=base.DATE.isoformat(), line_ids=(1,)):
    assert boundary == ('end' if shift == '昼' else 'start')
    kind = ('not-' + (qualifier or 'time') if status == 'excluded'
            else status if status != 'set' else qualifier or 'time')
    return {'serviceDate': day, 'shift': shift,
            'kind': kind,
            'time': explicit_time, 'evidenceLineIds': list(line_ids)}


def maximum_timing_response():
    days = [(base.DATE + dt.timedelta(days=offset)).isoformat() for offset in range(4)]
    source = [f'9月{6 + offset}日 昼1号店。12時から18時まで、ながめ昼。'
              if shift == '昼' else f'9月{6 + offset}日 夜2号店。16時から22時まで、早め夜。'
              for offset in range(4) for shift in ('昼', '夜')]
    text = '\n'.join(['本文'] * 112 + source * 2)
    ids = list(range(113, 129))
    events = [dated(event(shift, 'placement', ids, store), day)
              for day in days[:2] for shift, store in (('昼', 's1'), ('夜', 's2'))]
    links = [dated(link(scope, line_ids=ids), day)
             for day in days[:2] for scope in ('昼', '夜', 'unspecified')]
    facts = [timing_fact(shift, 'end' if shift == '昼' else 'start',
                         'long' if shift == '昼' else 'early', '18:00' if shift == '昼' else '16:00',
                         day=day, line_ids=ids) for day in days for shift in ('昼', '夜')]
    return text, result(*events, links=links, work_timing=facts)


def capacity_stress_text():
    marker = 'PRIVATE_CAPACITY_SOURCE'
    return '\n'.join(['"' * 46] * 127 + ['"' * (31 - len(marker)) + marker])


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

    def test_discovery_keeps_empty_known_shifts_and_admits_grounded_explicit_event(self):
        target = {**base.AMU, 'shifts': []}
        parsed, reason = self.parse('今日 夜2号店', result(
            event('夜', 'placement', [1], 's2')), target=target)
        self.assertEqual(reason, 'events')
        self.assertEqual([value['shift'] for value in parsed['events']], ['夜'])
        request = self.opener.open.call_args.args[0]
        content = json.loads(request.data)
        payload = json.loads(content['messages'][1]['content'])
        self.assertEqual(payload['knownShiftsByDate'], {'2026-09-06': []})
        self.assertEqual(target['shifts'], [])
        for text, value in (
                ('今日 終日お休みです', result(event('昼', 'absence', [1]))),
                ('今日 2号店です', result(event('夜', 'placement', [1], 's2'))),
                ('今日 お給仕します', result(links=[link('夜')]))):
            with self.subTest(text=text), self.assertRaisesRegex(azure.AnalysisFailure, 'azure_ungrounded'):
                azure.grounded_assessment_v8(value, text, base.DATE, [], personal.azure_context())

    def test_discovery_storeless_link_and_timing_only_never_create_core(self):
        target = {**base.AMU, 'shifts': []}
        parsed, _ = self.parse('今日 お給仕します', result(links=[link('unspecified')]), target=target)
        self.assertEqual(parsed['events'], [])
        self.assertNotIn('workTiming', parsed)
        self.assertEqual(parsed['links'], [{'scope': 'unspecified', 'status': 'work'}])
        timing_post, _ = self.parse('今日 昼は16:00までお給仕', result(
            work_timing=[timing_fact()]), target=target)
        self.assertEqual(timing_post['events'], [])
        self.assertEqual(timing_post['links'], [])
        self.assertEqual(len(timing_post['workTiming']['facts']), 1)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_ungrounded'):
            azure.grounded_assessment_v8(result(work_timing=[timing_fact()]),
                                        '今日16:00', base.DATE, [], personal.azure_context())
        self.assertEqual(azure.VERSION, 'personal-line-ids-v8')

    def test_registry_change_during_ai_wait_stops_issuance_without_changing_old_cache(self):
        registry = base.registry_fixture(base.AMU)
        path = self.folder / 'members.json'
        personal.official.atomic_json(path, registry)
        self.analyzer.registry_guard = personal.members.RegistryGuard(path, bindings=({},))
        self.parse('今日 昼1号店', result(event('昼', 'placement', [1], 's1')))
        before = copy.deepcopy(self.analyzer.state)
        sleep = self.analyzer.sleep

        def pause(seconds):
            sleep(seconds)
            personal.official.atomic_json(path, personal.members.update_member(
                registry, 'あむ', now=self.clock, collection='paused'))

        self.analyzer.sleep = pause
        with self.assertRaisesRegex(azure.AnalysisFailure, 'registry_changed'):
            self.parse('今日 昼2号店', result(event('昼', 'placement', [1], 's2')))
        self.opener.open.assert_called_once()
        self.assertEqual(self.analyzer.state, before)
        personal.read_state(self.snapshot)

    def test_registry_change_after_ai_reservation_keeps_budget_and_interrupted_receipt(self):
        registry = base.registry_fixture(base.AMU)
        path = self.folder / 'members.json'
        personal.official.atomic_json(path, registry)
        self.analyzer.registry_guard = personal.members.RegistryGuard(path, bindings=({},))
        save = self.analyzer.save

        def reserve_then_pause():
            save()
            if self.analyzer.state['cache']:
                personal.official.atomic_json(path, personal.members.update_member(
                    registry, 'あむ', now=self.clock, collection='paused'))

        self.analyzer.save = reserve_then_pause
        with self.assertRaisesRegex(azure.AnalysisFailure, 'registry_changed'):
            self.parse('今日 昼1号店', result(event('昼', 'placement', [1], 's1')))
        self.opener.open.assert_not_called()
        self.assertEqual(self.analyzer.state['budgets'], {'2026-09-06': 1})
        self.assertEqual(next(iter(self.analyzer.state['cache'].values()))['reason'], 'azure_interrupted')
        personal.read_state(self.snapshot)

    def test_known_night_manual_ai_spacing_rechecks_deadline(self):
        target = {**base.AMU, 'shifts': ['夜']}
        self.clock = dt.datetime.combine(base.DATE, dt.time(19, 29, 30), personal.JST)
        self.analyzer.deadline = lambda: bool(personal.active_targets(
            {'あむ': target}, base.DATE, self.clock))
        self.parse('今日 夜1号店', result(event('夜', 'placement', [1], 's1')), target=target)
        previous = copy.deepcopy(self.analyzer.state)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_deadline'):
            self.parse('今日 夜2号店', result(event('夜', 'placement', [1], 's2')), target=target)
        self.opener.open.assert_called_once()
        self.assertEqual(self.analyzer.state, previous)
        self.assertEqual(target['shifts'], ['夜'])

    def test_unknown_daily_shift_stops_before_ai_reservation(self):
        target = {**base.AMU, 'shifts': []}
        self.analyzer.deadline = lambda: bool(personal.active_targets(
            {'あむ': target}, base.DATE, self.clock))
        previous = copy.deepcopy(self.analyzer.state)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_deadline'):
            self.parse('今日 夜1号店', result(event('夜', 'placement', [1], 's1')), target=target)
        self.opener.open.assert_not_called()
        self.assertEqual(self.analyzer.state, previous)

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
        for next_day_count in (0, 40):
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
            usage.apply_import(ledger.state, historical('2026-09-06', 40))
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

    def assess_v5(self, text, value, shifts=('昼', '夜')):
        return azure.grounded_assessment_v5(value, text, base.DATE, shifts, personal.azure_context())

    def test_work_timing_exact_boundaries_and_explicit_words_are_independent(self):
        cases = (
            ('本日昼16時終了', timing_fact()),
            ('本日昼１２〜１６', timing_fact()),
            ('本日昼12:00-16:00', timing_fact()),
            ('本日昼18時終了', timing_fact(explicit_time='18:00')),
            ('本日夜16時開始', timing_fact('夜', 'start')),
            ('本日夜18時開始', timing_fact('夜', 'start', explicit_time='18:00')),
            ('本日昼は短め', timing_fact(qualifier='short', explicit_time=None)),
            ('本日昼はながめ', timing_fact(qualifier='long', explicit_time=None)),
            ('本日早め夜', timing_fact('夜', 'start', 'early', None)),
            ('本日おそめ夜', timing_fact('夜', 'start', 'late', None)),
            ('本日昼ながめ18時終了', timing_fact(qualifier='long', explicit_time='18:00')),
            ('本日早め夜16時開始', timing_fact('夜', 'start', 'early')),
            ('本日昼17時終了に訂正', timing_fact(qualifier=None, explicit_time='17:00')),
            ('本日昼15時終了', timing_fact(qualifier=None, explicit_time='15:00')),
            ('本日夜19時開始', timing_fact('夜', 'start', None, '19:00')),
        )
        for text, proposed in cases:
            with self.subTest(text=text):
                events, links, facts, reason = azure.grounded_assessment_v8(
                    result(pending_events=True, links=None, work_timing=[proposed]),
                    text, base.DATE, ('昼', '夜'), personal.azure_context())
                self.assertEqual((events, links, reason), ([], [], 'work_timing'))
                self.assertEqual(facts, [azure.timing.expand_compact(
                    {key: proposed[key] for key in azure.timing.COMPACT_FIELDS}, proposed['serviceDate'])])
                if proposed['kind'] == 'time':
                    self.assertIsNone(facts[0]['qualifier'])
        self.opener.open.assert_not_called()

    def test_saved_empty_events_keep_effective_core_and_word_early_without_filling_source_clock(self):
        text = '9月6日\n2号店夜\n16時から22時\n早め夜です'
        payload = base.post(text)
        previous, _ = personal.validate_post(base.candidate(), payload, base.AMU, self.clock)
        self.assertEqual([(item['shift'], item['storeId']) for item in previous['events']], [('夜', 's2')])
        self.assertNotIn('links', previous)
        self.state['posts'] = [copy.deepcopy(previous)]
        self.state['identityBindings'][base.AMU['name']] = {
            'authorId': base.UID, 'authorScreenName': base.AMU['handle'],
            'verifiedAt': personal.stamp(self.clock)}
        raw = result(
            links=[link('夜', line_ids=[1, 2, 4])],
            work_timing=[timing_fact('夜', 'start', 'early', None, line_ids=[1, 3, 4])])
        original_raw = copy.deepcopy(raw)
        analyzer = mock.Mock()
        analyzer.state = self.analyzer.state

        def grounded(body, created, date, shifts, name, **identity):
            self.assertEqual((body, date, identity['post_id']), (text, base.DATE, base.TID))
            normalized = azure.grounded_assessment_v8(raw, body, date, shifts, personal.azure_context())
            events, links, facts, reason = normalized
            self.assertEqual((events, links, reason), ([], personal.legacy_links(previous), 'links'))
            self.assertEqual((facts[0]['qualifier'], facts[0]['explicitTime']), ('early', None))
            return normalized

        analyzer.parse_with_timing.side_effect = grounded
        self.clock += dt.timedelta(minutes=5)
        with mock.patch.object(personal, 'parse_events', side_effect=AssertionError('No semantic fallback')):
            report, code, client = self.collect(payloads={base.TID: payload}, analyzer=analyzer)
        current = self.state['posts'][0]

        def effective_core(post):
            return {**{key: value for key, value in post.items()
                       if key not in ('observedAt', 'links', 'workTiming')},
                    'links': post.get('links', personal.legacy_links(post))}

        self.assertEqual((code, report['status'], report['failures']), (0, 'ok', []))
        self.assertEqual(effective_core(current), effective_core(previous))
        self.assertEqual(current['events'], previous['events'])
        self.assertNotEqual(current['observedAt'], previous['observedAt'])
        self.assertEqual(current['links'], [{'scope': '夜', 'status': 'work'}])
        self.assertEqual(analyzer.state['history'], [previous])
        fact = current['workTiming']['facts'][0]
        self.assertEqual((fact['boundary'], fact['status'], fact['qualifier'], fact['explicitTime']),
                         ('start', 'set', 'early', None))
        self.assertEqual(fact['source'], personal.timing.source_metadata(previous, 'personal-work-post'))
        self.assertEqual(raw, original_raw)
        self.assertEqual(report['requests'], {'searches': 0, 'posts': 0})
        analyzer.parse_with_timing.assert_called_once()
        client.search.assert_not_called()
        client.fetch_post.assert_not_called()
        self.opener.open.assert_not_called()
        self.assertEqual(self.analyzer.used, 0)

    def test_work_timing_compact_maximum_response_fits_transport_and_rejects_ninth_scope(self):
        text, value = maximum_timing_response()
        lines = azure.source_lines(text)
        self.assertEqual(len(lines), 128)
        self.assertLessEqual(len(text.encode('utf-8')), 6000)
        self.assertEqual([len(value[field]) for field in ('events', 'links', 'workTiming')], [4, 6, 8])
        schema = azure.response_schema(lines)
        self.assertEqual(schema['properties']['workTiming']['maxItems'], 8)
        self.assertEqual(set(schema['properties']['workTiming']['items']['required']),
                         {'serviceDate', 'shift', 'kind', 'time', 'evidenceLineIds'})
        for field in ('events', 'links', 'workTiming'):
            refs = schema['properties'][field]['items']['properties']['evidenceLineIds']
            self.assertEqual(refs['maxItems'], 16)
            self.assertEqual(refs['items']['enum'], list(range(1, 129)))
            self.assertTrue(all(len(item['evidenceLineIds']) == 16 for item in value[field]))
        for indent in (None, 2):
            with self.subTest(indent=indent):
                separators = (',', ':') if indent is None else None
                content = json.dumps(value, indent=indent, separators=separators)
                reply = response(content=content)
                envelope = json.loads(reply.read.return_value)
                payload = json.dumps(envelope, indent=indent, separators=separators).encode('utf-8')
                self.assertLess(len(payload), 24000)
                self.assertEqual(azure.MAX_RESPONSE_BYTES, 24000)
                reply.read.return_value = payload
                self.opener.open.return_value = reply
                decoded = self.analyzer.request(lines, personal.official.timestamp(base.CREATED),
                                                base.DATE, ('昼', '夜'), base.AMU['name'])
                self.assertEqual(decoded, value)
                events, links, facts, channels, dates = azure._grounded_v8(
                    decoded, text, base.DATE, ('昼', '夜'), personal.azure_context(), lines)
                self.assertEqual([len(events), len(links), len(facts)], [2, 3, 2])
                self.assertEqual(channels, {'events': 'confirmed', 'links': 'confirmed', 'workTiming': 'confirmed'})
                self.assertEqual(len(dates['workTiming']), 4)
                self.assertEqual([fact['boundary'] for fact in facts], ['end', 'start'])
                self.assertTrue(all(set(fact) == set(azure.timing.FACT_FIELDS) for fact in facts))
        self.assertEqual(self.opener.open.call_count, 2)
        ninth = copy.deepcopy(value)
        ninth['workTiming'].append({**value['workTiming'][0], 'serviceDate': '2026-09-10'})
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            azure.grounded_assessment_v8(ninth, text, base.DATE, ('昼', '夜'), personal.azure_context())

    def test_capacity_admits_known_small_and_maximum_fixture_before_reservation(self):
        for text, value in (('本日昼16時終了', result(work_timing=[timing_fact()])),
                            maximum_timing_response()):
            with self.subTest(lines=len(azure.source_lines(text))):
                self.state = personal.empty_state()
                analyzer = self.make_analyzer()
                self.opener.reset_mock()
                lines = azure.source_lines(text)
                messages, schema, payload = azure.request_components(
                    lines, personal.official.timestamp(base.CREATED), base.DATE, ('昼', '夜'), base.AMU['name'])
                self.assertEqual(json.loads(messages[1]['content']), payload)
                self.assertEqual(''.join(line['text'] for line in payload['bodyLines']), text)
                self.assertEqual(schema, azure.response_schema(lines))
                self.assertTrue(all(set(line) == {'id', 'text'} for line in payload['bodyLines']))
                report = azure.capacity.personal(payload, azure.PROMPT, azure.SCHEMA, azure.MAX_OUTPUT_TOKENS)
                self.assertLessEqual(report['textReservationBound'], 10000)
                self.assertEqual(report['framingReserve'], 512)
                self.assertIs(report['serviceTpmGuaranteed'], False)
                self.assertIs(report['imageTokensIncluded'], False)
                with mock.patch.object(analyzer, 'reserve', wraps=analyzer.reserve) as reserve:
                    post, reason = self.parse(text, value, analyzer=analyzer)
                self.assertIsNotNone(post)
                self.assertIn(reason, ('events', 'work_timing'))
                self.assertEqual(analyzer.used, 1)
                reserve.assert_called_once()
                self.opener.open.assert_called_once()

    def test_capacity_diagnostics_are_pure_and_direct_request_cannot_bypass_stress_hold(self):
        text = capacity_stress_text()
        lines = azure.source_lines(text)
        self.assertEqual((len(text.encode('utf-8')), len(lines)), (6000, 128))
        original = copy.deepcopy(lines)
        created = personal.official.timestamp(base.CREATED)
        with mock.patch.object(azure.transport, 'AzureOpenAI',
                               side_effect=AssertionError('Diagnostics must not construct a client')):
            messages, schema, payload = azure.request_components(lines, created, base.DATE, ('昼', '夜'), 'あむ')
        self.assertEqual(lines, original)
        self.assertEqual(len(payload['bodyLines']), 128)
        self.assertEqual(''.join(line['text'] for line in payload['bodyLines']), text)
        self.assertEqual(json.loads(messages[1]['content']), payload)
        self.assertEqual(schema['properties']['workTiming']['maxItems'], 8)
        with self.assertRaisesRegex(azure.capacity.CapacityHold, '^azure_capacity_hold$'):
            azure.capacity.personal(payload, azure.PROMPT, azure.SCHEMA, azure.MAX_OUTPUT_TOKENS)
        with mock.patch.object(self.analyzer.client, 'structured') as structured:
            with self.assertRaisesRegex(azure.AnalysisFailure, '^azure_capacity_hold$'):
                self.analyzer.request(lines, created, base.DATE, ('昼', '夜'), 'あむ')
            for malformed in ([], [{'id': 2, 'text': '本日昼16時終了'}],
                              [{'id': True, 'text': '本日昼16時終了'}]):
                with self.subTest(lines=malformed), self.assertRaisesRegex(
                        azure.AnalysisFailure, '^azure_capacity_hold$'):
                    self.analyzer.request(malformed, created, base.DATE, ('昼',), 'あむ')
            structured.assert_not_called()
        self.assertEqual(self.analyzer.used, 0)
        self.assertEqual(self.analyzer.state['cache'], {})
        self.opener.open.assert_not_called()

    def test_capacity_stress_caches_pending_before_any_usage_and_preserves_old_facts(self):
        previous, _ = personal.validate_post(base.candidate(), base.post(), base.AMU, self.clock)
        self.state['posts'] = [previous]
        self.state['identityBindings'][previous['name']] = {
            'authorId': previous['authorId'], 'authorScreenName': previous['authorScreenName'],
            'verifiedAt': base.CREATED}
        self.state['resolved'] = [
            {key: previous[key] for key in ('id', 'url', 'name', 'date')}
            | {'reason': 'events', 'resolvedAt': base.CREATED}]
        old_key = azure.digest('old accepted cache')
        old_cache = {
            'postId': base.TID, 'bodyHash': azure.digest('old body'), 'versionHash': self.analyzer.version,
            'at': base.CREATED, 'reason': 'events', 'events': copy.deepcopy(previous['events']),
            'links': [], 'workTiming': [],
        }
        self.analyzer.state['cache'][old_key] = copy.deepcopy(old_cache)
        self.analyzer.state['history'] = [copy.deepcopy(previous)]
        kept = {field: copy.deepcopy(self.state[field])
                for field in ('posts', 'resolved', 'identityBindings', 'budgets')}
        history = copy.deepcopy(self.analyzer.state['history'])
        text = capacity_stress_text()
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            with mock.patch.object(analyzer, 'reserve', wraps=analyzer.reserve) as reserve, \
                    mock.patch.object(ledger, 'reserve', wraps=ledger.reserve) as usage_reserve, \
                    mock.patch.object(ledger, 'issued', wraps=ledger.issued) as issued, \
                    mock.patch.object(analyzer.client, 'structured') as structured, \
                    mock.patch.object(azure.capacity, 'personal', wraps=azure.capacity.personal) as admission, \
                    mock.patch.object(personal, 'parse_events', side_effect=AssertionError('No semantic fallback')):
                for _ in range(2):
                    report, code, client = self.collect(payloads={base.TID: base.post(text)}, analyzer=analyzer)
                    self.assertEqual((report['status'], code), ('partial', 2))
                    self.assertEqual(report['failures'], [{'id': base.TID, 'reason': 'azure_capacity_hold'}])
                    self.assertEqual(self.state['pending'][0]['reason'], 'azure_capacity_hold')
                    self.assertEqual((report['newPostCount'], report['newEventCount']), (0, 0))
                    for field, value in kept.items():
                        self.assertEqual(self.state[field], value)
                    self.assertEqual(analyzer.state['history'], history)
                    self.assertEqual(analyzer.state['cache'][old_key], old_cache)
                    client.search.assert_not_called()
                    client.fetch_post.assert_not_called()
                reserve.assert_not_called()
                usage_reserve.assert_not_called()
                issued.assert_not_called()
                structured.assert_not_called()
                admission.assert_called_once()
            self.assertEqual((analyzer.used, ledger.used), (0, 0))
            self.assertEqual(ledger.state['receipts'], {})
            self.assertEqual(len(analyzer.state['cache']), 2)
            self.assertEqual(next(value['reason'] for key, value in analyzer.state['cache'].items()
                                  if key != old_key), 'azure_capacity_hold')
            self.assertEqual(analyzer.state['budgets'], {})
            self.assertIsNone(analyzer.state['nextRequestAt'])
        self.opener.open.assert_not_called()
        self.assertNotIn('PRIVATE_CAPACITY_SOURCE', self.snapshot.read_text(encoding='utf-8'))
        personal.read_state(self.snapshot)

    def test_capacity_stale_profile_is_cached_fail_closed_without_reservation_or_client(self):
        stale = {**azure.capacity.PERSONAL, 'promptHash': '0' * 64}
        with self.shared_usage() as ledger, mock.patch.object(azure.capacity, 'PERSONAL', stale):
            analyzer = self.make_analyzer(usage=ledger)
            with mock.patch.object(analyzer, 'reserve') as reserve, \
                    mock.patch.object(ledger, 'issued') as issued, \
                    mock.patch.object(analyzer.client, 'structured') as structured:
                for _ in range(2):
                    with self.assertRaisesRegex(azure.AnalysisFailure, '^azure_capacity_profile_stale$'):
                        self.parse('本日昼16時終了', result(work_timing=[timing_fact()]), analyzer=analyzer)
                with self.assertRaisesRegex(azure.AnalysisFailure, '^azure_capacity_profile_stale$'):
                    analyzer.request(azure.source_lines('本日昼16時終了'), personal.official.timestamp(base.CREATED),
                                     base.DATE, ('昼',), 'あむ')
                reserve.assert_not_called()
                issued.assert_not_called()
                structured.assert_not_called()
            self.assertEqual((analyzer.used, ledger.used), (0, 0))
            self.assertEqual(ledger.state['receipts'], {})
            self.assertEqual(next(iter(analyzer.state['cache'].values()))['reason'], 'azure_capacity_profile_stale')
        self.opener.open.assert_not_called()
        personal.read_state(self.snapshot)

    def test_capacity_profile_hash_changes_namespace_without_changing_semantic_contract(self):
        original_version, original_contract = self.analyzer.version, azure.CONTRACT_HASH
        original_prompt, original_schema = azure.PROMPT, copy.deepcopy(azure.SCHEMA)
        self.assertEqual(azure.capacity.digest(azure.PROMPT), azure.capacity.PERSONAL['promptHash'])
        self.assertEqual(azure.capacity.digest(azure.capacity.canonical(azure.SCHEMA)),
                         azure.capacity.PERSONAL['schemaHash'])
        changed = {**azure.capacity.PERSONAL, 'system': azure.capacity.PERSONAL['system'] + 1}
        with mock.patch.object(azure.capacity, 'PERSONAL', changed):
            self.assertNotEqual(self.make_analyzer().version, original_version)
        self.assertEqual((azure.PROMPT, azure.SCHEMA, azure.CONTRACT_HASH),
                         (original_prompt, original_schema, original_contract))
        for replacements in ({'PROMPT': azure.PROMPT + ' '},
                             {'SCHEMA': {**azure.SCHEMA, 'maxProperties': 3}},
                             {'MAX_OUTPUT_TOKENS': azure.MAX_OUTPUT_TOKENS + 1}):
            with self.subTest(replacements=list(replacements)), mock.patch.multiple(azure, **replacements):
                with self.assertRaisesRegex(azure.AnalysisFailure, '^azure_capacity_profile_stale$'):
                    self.analyzer.request(azure.source_lines('本日昼16時終了'),
                                          personal.official.timestamp(base.CREATED), base.DATE, ('昼',), 'あむ')
        self.opener.open.assert_not_called()

    def test_work_timing_wire_only_requests_day_end_and_night_start(self):
        self.assertEqual(azure.MAX_OUTPUT_TOKENS, 2304)
        self.assertEqual((azure.MAX_INPUT_BYTES, azure.MAX_DATED_EVENTS, azure.MAX_DATED_LINKS,
                          azure.MAX_EVIDENCE_LINES, azure.timing.MAX_FACTS), (6000, 4, 6, 16, 512))
        for policy in ('day WORK END or night WORK START', 'Day START and night END',
                       'Numbers NEVER generate a qualifier kind', "opposite boundary's clock",
                       'kind=time with time=17:00'):
            self.assertIn(policy, azure.PROMPT)
        for shift, bad_kind in (('昼', 'early'), ('昼', 'late'), ('夜', 'short'), ('夜', 'long')):
            raw = {'serviceDate': base.DATE.isoformat(), 'shift': shift, 'kind': bad_kind,
                   'time': None, 'evidenceLineIds': [1]}
            with self.subTest(raw=raw), self.assertRaises(azure.AnalysisFailure):
                self.assess('本日の勤務時間の案内', result(work_timing=[raw]))
        for raw in ({**timing_fact(), 'boundary': 'start'},
                    {**timing_fact('夜', 'start'), 'boundary': 'end'}):
            with self.subTest(raw=raw), self.assertRaises(azure.AnalysisFailure):
                self.assess('本日昼12時開始、夜22時終了', result(work_timing=[raw]))
        for text in ('本日昼12時開始', '本日夜22時終了'):
            self.assertEqual(azure.grounded_assessment_v8(
                result(), text, base.DATE, ('昼', '夜'), personal.azure_context()), ([], [], [], 'no_event'))

    def test_work_timing_compaction_preserves_rich_saved_channels_and_history(self):
        post, _ = personal.validate_post(base.candidate(), base.post(), base.AMU, self.clock)
        facts = [{'serviceDate': post['date'], 'shift': shift, 'boundary': boundary,
                  'status': 'set', 'qualifier': None, 'explicitTime': when}
                 for shift, boundary, when in (('昼', 'start', '12:00'), ('昼', 'end', '17:00'),
                                                ('夜', 'start', '18:00'), ('夜', 'end', '22:00'))]
        post['workTiming'] = azure.timing.bind(facts, post, 'personal-work-post')
        self.state['posts'] = [post]
        self.analyzer.state['history'] = [copy.deepcopy(post)]
        self.analyzer.state['cache'][azure.digest('rich prior cache')] = {
            'postId': post['id'], 'bodyHash': azure.digest('prior body'),
            'versionHash': azure.digest('prior wire'), 'at': base.CREATED,
            'reason': 'events', 'events': copy.deepcopy(post['events']), 'links': [],
            'workTiming': copy.deepcopy(facts),
            'channels': {'events': 'confirmed', 'links': 'none', 'workTiming': 'confirmed'},
            'serviceDates': {'events': [post['date']], 'links': [], 'workTiming': [post['date']]},
        }
        self.save()
        self.assertEqual(personal.read_state(self.snapshot), self.state)
        self.assertEqual(personal.public_state(self.state)['posts'][0]['workTiming'], post['workTiming'])
        self.opener.open.assert_not_called()

    def test_work_timing_targeted_denials_keep_late_and_distinct_targets_in_private_cache(self):
        late = timing_fact('夜', 'start', 'late', None)
        not_early = timing_fact('夜', 'start', 'early', None, status='excluded', line_ids=(1, 2))
        not_sixteen = timing_fact('夜', 'start', None, '16:00', status='excluded', line_ids=(1, 3))
        text = '本日夜はおそめです\n早めではありません\n16時開始でもありません'
        value = result(work_timing=[late, not_early, not_sixteen])
        post, reason = self.parse(text, value)
        facts = post['workTiming']['facts']
        self.assertEqual(reason, 'work_timing')
        self.assertEqual((post['events'], post['links']), ([], []))
        self.assertEqual([(fact['status'], fact['qualifier'], fact['explicitTime']) for fact in facts],
                         [('set', 'late', None), ('excluded', 'early', None), ('excluded', None, '16:00')])
        self.assertEqual(len({azure.timing.scope(fact) for fact in facts}), 1)
        self.assertEqual(len({azure.timing.fact_key(fact) for fact in facts}), 3)
        self.assertNotIn('withdrawn', [fact['status'] for fact in facts])
        self.state['posts'] = [post]
        self.analyzer.state['history'] = [copy.deepcopy(post)]
        self.save()
        self.assertEqual(personal.read_state(self.snapshot), self.state)
        self.assertEqual(self.parse(text, value), (post, reason))
        self.opener.open.assert_called_once()
        duplicate = copy.deepcopy(self.analyzer.state)
        cached = next(iter(duplicate['cache'].values()))
        cached['workTiming'].append(copy.deepcopy(cached['workTiming'][1]))
        with self.assertRaises(ValueError):
            azure.validate_state(duplicate, personal.azure_context())
        with self.assertRaises(azure.AnalysisFailure):
            self.assess(text, result(work_timing=[late, not_early, not_early]))

    def test_work_timing_all_targeted_denial_kinds_validate_and_bind_only_their_target(self):
        cases = (
            ('昼', 'end', 'short', None, '本日昼は短めではありません'),
            ('昼', 'end', 'long', None, '本日昼はながめではありません'),
            ('夜', 'start', 'early', None, '本日夜は早めではありません'),
            ('夜', 'start', 'late', None, '本日夜はおそめではありません'),
            ('昼', 'end', None, '18:00', '本日昼18時終了ではありません'),
            ('夜', 'start', None, '16:00', '本日夜16時開始ではありません'),
        )
        for shift, boundary, qualifier, when, text in cases:
            proposed = timing_fact(shift, boundary, qualifier, when, status='excluded')
            with self.subTest(proposed=proposed):
                events, links, facts, reason = azure.grounded_assessment_v8(
                    result(pending_events=True, links=None, work_timing=[proposed]),
                    text, base.DATE, ('昼', '夜'), personal.azure_context())
                self.assertEqual((events, links, reason), ([], [], 'work_timing'))
                self.assertEqual((facts[0]['status'], facts[0]['qualifier'], facts[0]['explicitTime']),
                                 ('excluded', qualifier, when))
                self.assertEqual((facts[0]['qualifier'] is None) != (facts[0]['explicitTime'] is None), True)
        invalid = (
            {**timing_fact(), 'kind': 'not-short', 'time': '16:00'},
            {**timing_fact(), 'kind': 'not-time', 'time': None},
            {**timing_fact(), 'kind': 'not-early', 'time': None},
            {**timing_fact('夜', 'start'), 'kind': 'not-long', 'time': None},
            {**timing_fact(), 'kind': 'not-time', 'time': '19:00'},
            {**timing_fact(), 'kind': 'excluded'},
        )
        for proposed in invalid:
            with self.subTest(proposed=proposed), self.assertRaises(azure.AnalysisFailure):
                self.assess('本日昼16時終了という案内の否定です', result(work_timing=[proposed]))

    def test_work_timing_normal_usual_or_no_info_is_not_a_withdrawal_or_inferred_time(self):
        for policy in ('Current late plus a later "not early" must retain late',
                       'Normal/usual/no-info wording alone is NOT a whole-boundary',
                       'Do not invent a positive value from a denial'):
            self.assertIn(policy, azure.PROMPT)
        for text in ('本日いつもどおりです', '本日の勤務時間は普通です', '本日の時間情報はありません'):
            self.assertEqual(azure.grounded_assessment_v8(
                result(), text, base.DATE, ('昼', '夜'), personal.azure_context()), ([], [], [], 'no_event'))
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_pending'):
                azure.grounded_assessment_v8(result(work_timing=None), text, base.DATE,
                                             ('昼', '夜'), personal.azure_context())
        for kind in ('normal', 'usual', 'no-info', 'none'):
            proposed = {**timing_fact(explicit_time=None), 'kind': kind}
            with self.subTest(kind=kind), self.assertRaises(azure.AnalysisFailure):
                self.assess('本日いつもどおりです', result(work_timing=[proposed]))

    def test_work_timing_eight_distinct_denials_keep_wire_cap_separate_from_aggregate_guard(self):
        values = [timing_fact('夜', 'start', None, f'16:{minute:02d}', status='excluded')
                  for minute in range(9)]
        text = '本日夜の開始時刻ではありません: ' + '、'.join(value['time'] for value in values)
        parsed = azure.grounded_assessment_v8(
            result(work_timing=values[:8]), text, base.DATE, ('夜',), personal.azure_context())
        self.assertEqual(len(parsed[2]), 8)
        self.assertEqual(len({azure.timing.scope(fact) for fact in parsed[2]}), 1)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            self.assess(text, result(work_timing=values))
        facts = [azure.timing.expand_compact(
            {'shift': '夜', 'kind': 'not-time', 'time': f'{minute // 60:02d}:{minute % 60:02d}'},
            base.DATE.isoformat()) for minute in range(513)]
        state = azure.empty_state()
        cached = {'postId': base.TID, 'bodyHash': azure.digest('prior body'),
                  'versionHash': azure.digest('prior wire'), 'at': base.CREATED,
                  'reason': 'work_timing', 'events': [], 'links': [], 'workTiming': facts[:512],
                  'channels': {'events': 'none', 'links': 'none', 'workTiming': 'confirmed'},
                  'serviceDates': {'events': [], 'links': [], 'workTiming': [base.DATE.isoformat()]}}
        state['cache'][azure.digest('retained aggregate')] = cached
        azure.validate_state(state, personal.azure_context())
        cached['workTiming'] = facts
        with self.assertRaises(ValueError):
            azure.validate_state(state, personal.azure_context())
        self.opener.open.assert_not_called()

    def test_work_timing_storage_overflow_holds_old_facts_and_reports_partial_without_reissue(self):
        previous, _ = personal.validate_post(base.candidate(), base.post(), base.AMU, self.clock)
        previous['links'] = personal.legacy_links(previous)
        values = [{'shift': '夜', 'kind': 'late', 'time': None}]
        values.extend({'shift': '夜', 'kind': 'not-time', 'time': f'{minute // 60:02d}:{minute % 60:02d}'}
                      for minute in range(511))
        facts = [azure.timing.expand_compact(value, previous['date']) for value in values]
        previous['workTiming'] = azure.timing.bind(facts, previous, 'personal-work-post')
        self.state['posts'] = [previous]
        self.state['identityBindings'][previous['name']] = {
            'authorId': previous['authorId'], 'authorScreenName': previous['authorScreenName'],
            'verifiedAt': base.CREATED}
        self.state['resolved'] = [
            {key: previous[key] for key in ('id', 'url', 'name', 'date')}
            | {'reason': 'events', 'resolvedAt': base.CREATED}]
        self.state['budgets'][previous['date']] = {'searches': 7, 'posts': 2}
        self.analyzer.state['history'] = [copy.deepcopy(previous)]
        kept = {field: copy.deepcopy(self.state[field])
                for field in ('posts', 'resolved', 'identityBindings', 'budgets')}
        history = copy.deepcopy(self.analyzer.state['history'])
        text = '本日夜は16時開始ではありません'
        self.opener.open.return_value = response(result(work_timing=[
            timing_fact('夜', 'start', None, '16:00', status='excluded')]))
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            for _ in range(2):
                report, code, client = self.collect(payloads={base.TID: base.post(text)}, analyzer=analyzer)
                self.assertEqual((report['status'], code), ('partial', 2))
                self.assertEqual(report['failures'],
                                 [{'id': base.TID, 'reason': 'azure_work_timing_storage_limit'}])
                self.assertEqual((report['newPostCount'], report['newEventCount'], report['pendingCount']),
                                 (0, 0, 1))
                self.assertEqual(self.state['pending'][0]['reason'], 'azure_work_timing_storage_limit')
                for field, value in kept.items():
                    self.assertEqual(self.state[field], value)
                self.assertEqual(analyzer.state['history'], history)
                self.assertEqual(len(self.state['posts'][0]['workTiming']['facts']), 512)
                self.assertEqual(self.state['posts'][0]['workTiming']['facts'][0]['qualifier'], 'late')
                client.search.assert_not_called()
                client.fetch_post.assert_not_called()
            self.assertEqual(ledger.used, 1)
            self.assertEqual(next(iter(ledger.state['receipts'].values()))['reason'], 'work_timing')
            self.assertEqual(next(iter(analyzer.state['cache'].values()))['reason'], 'work_timing')
        self.opener.open.assert_called_once()
        personal.read_state(self.snapshot)

    def test_work_timing_nullable_cross_product_has_no_cross_channel_deletion(self):
        for events in (None, [], [dated(event('昼', 'placement', [1], 's1'))]):
            for links in (None, [], [dated(link('昼'))]):
                for facts in (None, [], [timing_fact()]):
                    value = {'events': events, 'links': links, 'workTiming': facts}
                    with self.subTest(value=value):
                        reason = ('events' if events else 'links' if links else
                                  'work_timing' if facts else
                                  'azure_pending' if None in (events, links, facts) else 'no_event')
                        if reason == 'azure_pending':
                            with self.assertRaisesRegex(azure.AnalysisFailure, reason):
                                azure.grounded_assessment_v8(value, '今日 昼1号店16時終了',
                                                            base.DATE, ('昼',), personal.azure_context())
                        else:
                            parsed = azure.grounded_assessment_v8(value, '今日 昼1号店16時終了',
                                                                 base.DATE, ('昼',), personal.azure_context())
                            self.assertEqual(parsed[-1], reason)
                            self.assertEqual([bool(items) for items in parsed[:3]],
                                             [bool(events), bool(links), bool(facts)])

    def test_work_timing_only_persists_source_bound_cache_without_attendance_or_links(self):
        self.known()
        text = '本日昼16時終了\nPRIVATE_TIMING_BODY'
        self.opener.open.return_value = response(result(work_timing=[timing_fact()]))
        with mock.patch.object(personal, 'parse_events', side_effect=AssertionError('no rules fallback')):
            _, code, client = self.collect(payloads={base.TID: base.post(text)})
        self.assertEqual(code, 0)
        client.search.assert_not_called()
        client.fetch_post.assert_not_called()
        self.opener.open.assert_called_once()
        post = self.state['posts'][0]
        self.assertEqual((post['events'], post['links']), ([], []))
        self.assertEqual(post['workTiming']['facts'][0]['source'],
                         azure.timing.source_metadata(post, 'personal-work-post'))
        cached = next(iter(self.analyzer.state['cache'].values()))
        self.assertEqual(cached['reason'], 'work_timing')
        self.assertEqual(cached['channels'], {'events': 'none', 'links': 'none', 'workTiming': 'confirmed'})
        self.assertNotIn('source', cached['workTiming'][0])
        self.assertEqual(cached['serviceDates']['workTiming'], [base.DATE.isoformat()])
        for value in (self.state, personal.public_state(self.state)):
            encoded = json.dumps(value)
            for private in ('PRIVATE_TIMING_BODY', 'evidenceLineIds', 'bodyLines', ENV['AZURE_OPENAI_API_KEY']):
                self.assertNotIn(private, encoded)
        cached_post, reason = self.parse(text, result(work_timing=[timing_fact()]))
        self.assertEqual((cached_post, reason), (post, 'work_timing'))
        self.opener.open.assert_called_once()

    def test_work_timing_shared_usage_is_one_successful_accounted_request(self):
        text = '本日昼16時終了'
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            for _ in range(2):
                post, reason = self.parse(text, result(work_timing=[timing_fact()]), analyzer=analyzer)
                self.assertEqual((post['events'], post['links'], reason), ([], [], 'work_timing'))
            self.assertEqual(ledger.used, 1)
            receipt = next(iter(ledger.state['receipts'].values()))
            self.assertEqual(receipt['reason'], 'work_timing')
            self.assertIsNotNone(receipt['issuedAt'])
            self.assertIsNotNone(receipt['completedAt'])
        self.opener.open.assert_called_once()
        personal.read_state(self.snapshot)

    def test_work_timing_validates_ignored_dates_and_numeric_occurrence_per_selected_line(self):
        today = timing_fact()
        tomorrow = timing_fact(day='2026-09-07', explicit_time='18:00', line_ids=(2,))
        text = '今日昼16時終了\r\n明日昼18時終了'
        _, _, facts, _, dates = azure._grounded_v8(
            result(work_timing=[today, tomorrow]), text, base.DATE, ('昼',), personal.azure_context())
        self.assertEqual([fact['serviceDate'] for fact in facts], ['2026-09-06'])
        self.assertEqual(dates['workTiming'], ['2026-09-06', '2026-09-07'])
        self.assertEqual(azure.grounded_assessment_v8(
            result(work_timing=[tomorrow]), text, base.DATE, ('昼',), personal.azure_context()),
            ([], [], [], 'no_event'))
        bad_facts = (
            {**tomorrow, 'serviceDate': '2026-02-30'}, {**tomorrow, 'evidenceLineIds': [1]},
            {**tomorrow, 'evidenceLineIds': [1, 1]}, {**tomorrow, 'evidenceLineIds': [True]},
            {**tomorrow, 'evidenceLineIds': [999]}, {**tomorrow, 'shift': '朝'},
            {**tomorrow, 'kind': 'early'}, {**tomorrow, 'shift': '夜', 'kind': 'short'},
            {**tomorrow, 'time': '17:00'}, {**tomorrow, 'unexpected': None},
            {**tomorrow, 'kind': 'withdrawn'}, {**tomorrow, 'time': None},
            {**tomorrow, 'boundary': 'start'}, {**tomorrow, 'status': 'set'},
            {**tomorrow, 'qualifier': None}, {**tomorrow, 'explicitTime': '18:00'},
        )
        for proposed in bad_facts:
            with self.subTest(proposed=proposed), self.assertRaises(azure.AnalysisFailure):
                azure.grounded_assessment_v8(result(work_timing=[today, proposed]),
                                             text, base.DATE, ('昼',), personal.azure_context())
        for proposed in (result(work_timing=[today, today]),
                         result(work_timing=[today] * (azure.MAX_DATED_TIMING + 1)),
                         {**result(), 'workTiming': {}}, {'events': [], 'links': []}):
            with self.subTest(proposed=proposed), self.assertRaises(azure.AnalysisFailure):
                self.assess(text, proposed)
        with self.assertRaises(azure.AnalysisFailure):
            self.assess('本日昼終了16\n時', result(work_timing=[timing_fact(line_ids=(1, 2))]))
        for text in ('本日昼12-16時半', '本日昼12-16:300', '本日昼12-16時30分'):
            with self.subTest(text=text), self.assertRaises(azure.AnalysisFailure):
                self.assess(text, result(work_timing=[timing_fact()]))
        with self.assertRaises(azure.AnalysisFailure):
            self.assess('本日昼16時終了', result(work_timing=[today]), shifts=('夜',))

    def test_work_timing_does_not_reinterpret_late_return_break_or_default_hours(self):
        for kind in ('late', 'return'):
            value = result(event('夜', kind, [1], 's1', '18:00'))
            parsed = azure.grounded_assessment_v8(
                value, '本日夜1号店18時に到着・休憩から復帰', base.DATE, ('夜',), personal.azure_context())
            self.assertEqual(parsed[0][0]['time'], '18:00')
            self.assertEqual(parsed[2], [])
        for text in ('今日通しでお給仕', '今日昼休憩16時から18時まで', '投稿は16時です', '明日は早め夜'):
            self.assertEqual(azure.grounded_assessment_v8(
                result(), text, base.DATE, ('昼', '夜'), personal.azure_context()), ([], [], [], 'no_event'))
        for status in ('withdrawn', 'conflict'):
            proposed = timing_fact(status=status, qualifier=None, explicit_time=None)
            parsed = azure.grounded_assessment_v8(
                result(work_timing=[proposed]), '本日の昼終了の案内を取り消します',
                base.DATE, ('昼',), personal.azure_context())
            self.assertEqual(parsed[2][0]['status'], status)

    def test_work_timing_v7_adapter_is_explicit_and_cache_failure_is_not_migrated(self):
        old = {'events': [], 'links': [dated(link('昼'))]}
        self.assertEqual(azure.grounded_assessment_v7(
            old, '本日昼のお給仕', base.DATE, ('昼',), personal.azure_context()),
            ([], [{'scope': '昼', 'status': 'work'}], 'links'))
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            self.assess('本日昼のお給仕', old)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            azure.grounded_assessment_v7(
                result(), '本日昼のお給仕', base.DATE, ('昼',), personal.azure_context())

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
            'bodyLines': [{'id': 1, 'text': text}], 'postedAtJST': '2026-09-06T00:00:02+09:00',
            'postedDateJST': '2026-09-06', 'relativeDatesJST': {
                'yesterday': '2026-09-05', 'today': '2026-09-06',
                'tomorrow': '2026-09-07', 'dayAfterTomorrow': '2026-09-08'},
            'author': 'あむ', 'knownShiftsByDate': {'2026-09-06': ['昼', '夜']}})
        self.assertEqual(sent['reasoning_effort'], 'none')
        self.assertEqual(sent['model'], 'gpt-5.6-luna')
        self.assertEqual(sent['max_completion_tokens'], 2304)
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
            (result(pending_events=True, links=[link('夜')]),
             ([], [{'scope': '夜', 'status': 'work'}], 'links')),
            (result(event('昼', 'placement', [1], 's1'), links=None),
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
        for events, links in ((None, []), ([], None), (None, None)):
            with self.subTest(events=events, links=links):
                with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_pending'):
                    self.assess('判断保留', {**result(), 'events': events, 'links': links})
        self.assertEqual(self.assess('今日晴れ', result()), ([], [], 'no_event'))
        self.opener.open.assert_not_called()

    def test_v7_nullable_combinations_derive_status_and_cache_arrays_without_extra_calls(self):
        proposed_event = dated(event('昼', 'placement', [1], 's1'))
        proposed_link = dated(link('unspecified'))
        for events in (None, [], [proposed_event]):
            for links in (None, [], [proposed_link]):
                with self.subTest(events=events, links=links):
                    self.state = personal.empty_state()
                    self.opener.reset_mock()
                    analyzer = self.make_analyzer()
                    value = {**result(), 'events': events, 'links': links}
                    expected_events = ([{'shift': '昼', 'kind': 'placement',
                                         'storeId': 's1', 'excerpt': '1号店'}] if events else [])
                    expected_links = [{'scope': 'unspecified', 'status': 'work'}] if links else []
                    expected_reason = ('events' if events else 'links' if links else
                                       'azure_pending' if events is None or links is None else 'no_event')
                    for _ in range(2):
                        if expected_reason == 'azure_pending':
                            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_pending'):
                                self.parse('本日昼1号店でお給仕します', value, analyzer=analyzer)
                        else:
                            post, reason = self.parse('本日昼1号店でお給仕します', value, analyzer=analyzer)
                            self.assertEqual(reason, expected_reason)
                            if expected_events or expected_links:
                                self.assertEqual(post['events'], expected_events)
                                self.assertEqual(post['links'], expected_links)
                            else:
                                self.assertIsNone(post)
                    entry = next(iter(analyzer.state['cache'].values()))
                    self.assertEqual(entry['reason'], expected_reason)
                    self.assertEqual(entry['events'], expected_events)
                    self.assertEqual(entry['links'], expected_links)
                    self.assertEqual(entry['channels'], {
                        field: 'pending' if value is None else 'confirmed' if value else 'none'
                        for field, value in (('events', events), ('links', links), ('workTiming', []))})
                    self.assertEqual(entry['serviceDates'], {
                        field: [base.DATE.isoformat()] if value else []
                        for field, value in (('events', events), ('links', links), ('workTiming', []))})
                    self.assertEqual(set(entry), {
                        'postId', 'bodyHash', 'versionHash', 'at', 'reason', 'events', 'links',
                        'channels', 'serviceDates', 'workTiming'})
                    self.opener.open.assert_called_once()
                    self.assertEqual(analyzer.used, 1)
                    personal.read_state(self.snapshot)

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
        self.assertEqual((azure.RUN_LIMIT, azure.DAY_LIMIT, azure.SPACING_SECONDS), (3, 40, 60))
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

    def test_v7_links_reject_bad_scope_status_fields_and_evidence_ids(self):
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
        valid = result_v5(event('昼', 'placement', [1], 's1'), links=[link('昼')])
        cases = [
            {key: value for key, value in valid.items() if key != 'links'},
            {key: value for key, value in valid.items() if key != 'linkDecision'},
            {**valid, 'linkDecision': 'no_link'}, {**valid, 'linkDecision': 'pending'},
            {**valid, 'linkDecision': None}, {**valid, 'links': None},
            {**valid, 'links': []}, {**valid, 'date': '2026-09-07'},
            {**valid, 'decision': 'no_event'}, {**valid, 'decision': 'pending'},
            {**valid, 'decision': []}, {**valid, 'events': None},
            {**valid, 'author': 'other'}, {**valid, 'events': valid['events'] * 2},
            result_v5(links=[], link_decision='links'),
            result_v5(event('昼', 'placement', [1], 's2'), links=[link('昼')]),
            result_v5(event('昼', 'placement', [1, 2], 's1'), links=[link('昼')]),
        ]
        for proposed in cases:
            with self.subTest(proposed=proposed), self.assertRaises(azure.AnalysisFailure):
                self.assess_v5('本日昼1号店\n', proposed)

    def test_v7_requires_exact_nullable_array_contract_and_bounded_grounded_facts(self):
        valid = result(event('昼', 'placement', [1], 's1'), links=[link('昼')])
        cases = [None, False, [], '', {}, {**valid, 'date': '2026-09-07'},
                 {**valid, 'decision': 'events'}, {**valid, 'linkDecision': 'links'},
                 {**valid, 'author': 'other'},
                 result_v5(event('昼', 'placement', [1], 's1'), links=[link('昼')]),
                 result(event('昼', 'placement', [1], 's2'), links=None),
                 result(event('昼', 'placement', [1, 2], 's1'), links=[link('昼')])]
        for field in ('events', 'links'):
            cases.append({key: value for key, value in valid.items() if key != field})
        for field in ('events', 'links'):
            for invalid in (False, True, 0, 1, 1.5, '', 'pending', {}, (), [None], [True]):
                cases.append({**valid, field: invalid})
        cases.extend([
            {**valid, 'events': valid['events'] * 2},
            {**valid, 'events': valid['events'] * 3},
            {**valid, 'links': valid['links'] * 4},
            result(pending_events=True, links=[{'scope': 'unspecified', 'status': 'work'}]),
        ])
        for value in cases:
            with self.subTest(value=value), self.assertRaises(azure.AnalysisFailure):
                self.assess('本日昼1号店\n', value)
        bounded = result(event('昼', 'placement', [1], 's1'),
                         event('夜', 'placement', [1], 's2'),
                         links=[link('昼'), link('夜'), link('unspecified')])
        events, links, reason = self.assess('本日昼1号店、夜2号店でお給仕します', bounded)
        self.assertEqual((len(events), len(links), reason), (2, 3, 'events'))

    def test_explicit_v5_replay_retains_confirmed_and_pending_semantics_without_v7_relabeling(self):
        text = '本日昼1号店、夜2号店でお給仕します'
        old = result_v5(event('昼', 'placement', [1], 's1'),
                        event('夜', 'placement', [1], 's2'), links=[link('昼'), link('夜')])
        events, links, reason = self.assess_v5(text, old)
        self.assertEqual(([item['storeId'] for item in events], links, reason),
                         (['s1', 's2'], [{'scope': '昼', 'status': 'work'},
                                        {'scope': '夜', 'status': 'work'}], 'events'))
        self.assertEqual(self.assess_v5(text, result_v5(decision='pending', links=[link('夜')])),
                         ([], [{'scope': '夜', 'status': 'work'}], 'links'))
        self.assertEqual(self.assess_v5(text, result_v5(
            event('昼', 'placement', [1], 's1'), link_decision='pending'))[2], 'events')
        self.assertEqual(self.assess_v5(text, result_v5()), ([], [], 'no_event'))
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_pending'):
            self.assess_v5(text, result_v5(decision='pending', link_decision='pending'))
        for value in (old, legacy_result()):
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
                self.assess(text, value)
        for value in (result(), legacy_result()):
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
                self.assess_v5(text, value)
        self.opener.open.assert_not_called()
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            self.parse(text, old)
        self.opener.open.assert_called_once()

    def test_malformed_v5_empty_events_success_stays_rejected_and_negative_cached(self):
        text = '本日お給仕します'
        malformed = result_v5(decision='events', links=[link('unspecified')])
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            self.assess_v5(text, malformed)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            self.assess(text, malformed)
        for proposed in (malformed, result(links=[link('unspecified')])):
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
                self.parse(text, proposed)
        entry = next(iter(self.analyzer.state['cache'].values()))
        self.assertEqual((entry['reason'], entry['events'], entry['links']),
                         ('azure_invalid_output', [], []))
        self.assertNotIn('channels', entry)
        self.assertEqual(self.state['posts'], [])
        self.opener.open.assert_called_once()
        personal.read_state(self.snapshot)

    def test_normal_v7_path_rejects_v4_while_explicit_saved_v4_replay_remains_available(self):
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
        self.assertEqual(azure.VERSION, 'personal-line-ids-v8')
        for version in ('personal-line-ids-v4', 'personal-line-ids-v5', 'personal-line-ids-v6',
                        'personal-line-ids-v7'):
            with mock.patch.object(azure, 'VERSION', version):
                old_namespace = self.make_analyzer().version
            self.assertNotEqual(self.analyzer.version, old_namespace)

    def test_v7_prompt_keeps_semantic_decisions_in_one_model_request(self):
        text = '同じ日に投稿しただけ\n9/7の募集について話しています'
        self.parse(text, result())
        sent = json.loads(self.opener.open.call_args.args[0].data)
        context = json.loads(sent['messages'][1]['content'])
        self.assertEqual(context['postedAtJST'], '2026-09-06T00:00:02+09:00')
        self.assertNotIn('postedAt', context)
        self.assertNotIn('date', context)
        self.assertEqual(len(sent['messages']), 2)
        for boundary in ('Publication', 'half-month schedules', 'third-party', 'recruitment',
                         'unspecified', 'already-displayed', 'independently', 'knownShiftsByDate'):
            self.assertIn(boundary, sent['messages'][0]['content'])
        self.assertEqual(sent['model'], 'gpt-5.6-luna')
        self.assertEqual(sent['max_completion_tokens'], 2304)
        self.assertEqual(self.analyzer.client.identity['modelVersion'], '2026-07-09')
        schema = sent['response_format']['json_schema']['schema']
        self.assertEqual(set(schema['required']), {'events', 'links', 'workTiming'})
        self.assertEqual(set(schema['properties']), {'events', 'links', 'workTiming'})
        self.assertIs(schema['additionalProperties'], False)
        self.assertNotIn('linkDecision', sent['messages'][0]['content'])
        self.assertIn('events=[]', sent['messages'][0]['content'])
        self.assertIn('events=null', sent['messages'][0]['content'])
        self.assertIn('links=[]', sent['messages'][0]['content'])
        self.assertIn('links=null', sent['messages'][0]['content'])
        self.assertEqual(schema['properties']['events']['maxItems'], 4)
        self.assertEqual(schema['properties']['links']['maxItems'], 6)
        for field in ('events', 'links'):
            self.assertEqual(schema['properties'][field]['type'], ['array', 'null'])
            self.assertIn('serviceDate', schema['properties'][field]['items']['required'])
            self.assertEqual(schema['properties'][field]['items']['properties']['serviceDate'],
                             {'type': 'string', 'pattern': r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'})
            ids = schema['properties'][field]['items']['properties']['evidenceLineIds']
            self.assertEqual(ids['items']['enum'], [1, 2])
            self.assertEqual(ids['maxItems'], 2)
        self.opener.open.assert_called_once()

    def test_dated_work_filters_future_claims_and_retains_validated_dates_in_cache(self):
        cases = (
            ('今日、好きな本を買いました。次のお給仕は明日です。',
             [dated(link('unspecified'), '2026-09-07')], 'no_event', ['2026-09-07']),
            ('今日もお給仕します。', [link('unspecified')], 'links', ['2026-09-06']),
            ('今日も明日もお給仕します。',
             [dated(link('unspecified'), '2026-09-07'), link('unspecified')],
             'links', ['2026-09-06', '2026-09-07']),
        )
        for text, links, expected_reason, expected_dates in cases:
            with self.subTest(expected_reason=expected_reason, dates=expected_dates):
                self.state = personal.empty_state()
                analyzer = self.make_analyzer()
                self.opener.reset_mock()
                for _ in range(2):
                    post, reason = self.parse(text, result(links=links), analyzer=analyzer)
                    self.assertEqual(reason, expected_reason)
                    if reason == 'no_event':
                        self.assertIsNone(post)
                    else:
                        self.assertEqual(post['events'], [])
                        self.assertEqual(post['links'], [{'scope': 'unspecified', 'status': 'work'}])
                        self.assertEqual(post['date'], '2026-09-06')
                        self.assertEqual(post['createdAt'], base.CREATED)
                cached = next(iter(analyzer.state['cache'].values()))
                self.assertEqual(cached['serviceDates'], {'events': [], 'links': expected_dates, 'workTiming': []})
                self.assertEqual(cached['channels'], {
                    'events': 'none', 'links': 'confirmed' if expected_reason == 'links' else 'none',
                    'workTiming': 'none'})
                self.assertEqual(self.state['posts'], [])
                self.opener.open.assert_called_once()
                personal.read_state(self.snapshot)

    def test_service_dates_are_required_canonical_and_never_defaulted(self):
        for field, item in (('events', event('昼', 'placement', [1], 's1')),
                            ('links', link('unspecified'))):
            values = [item]
            for day in (None, True, 0, [], {}, '2026-9-6', '2026-02-30', '2026-13-01',
                        '0000-09-06', '2026-W36-7', '2026-09-06T00:00:00Z',
                        '2026-09-06\n', ' 2026-09-06', '２０２６-０９-０６'):
                values.append(dated(item, day))
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(azure.AnalysisFailure):
                    self.assess('本日昼1号店でお給仕します', {**result(), field: [value]})
        with self.assertRaises(azure.AnalysisFailure):
            self.assess('本日お給仕します', {'events': [], 'links': [link('unspecified')]})
        self.assertEqual(self.assess('2月29日にお給仕します',
                                    result(links=[dated(link('unspecified'), '2028-02-29')])),
                         ([], [], 'no_event'))

    def test_all_dated_groups_validate_before_filter_with_per_date_and_wire_bounds(self):
        text = '今日も明日も昼1号店、夜2号店でお給仕します'
        events = [dated(event(shift, 'placement', [1], store), day)
                  for day in ('2026-09-06', '2026-09-07')
                  for shift, store in (('昼', 's1'), ('夜', 's2'))]
        links = [dated(link(scope), day) for day in ('2026-09-06', '2026-09-07')
                 for scope in ('昼', '夜', 'unspecified')]
        value = result(*events, links=links)
        accepted, accepted_links, reason = self.assess(text, value)
        self.assertEqual(([item['storeId'] for item in accepted], len(accepted_links), reason),
                         (['s1', 's2'], 3, 'events'))
        _, _, channels, dates = azure._grounded_v7(
            {key: value[key] for key in ('events', 'links')},
            text, base.DATE, base.AMU['shifts'], personal.azure_context())
        self.assertEqual(channels, {'events': 'confirmed', 'links': 'confirmed'})
        self.assertEqual(dates, {field: ['2026-09-06', '2026-09-07'] for field in ('events', 'links')})
        invalid = [
            result(*events, dated(event('昼', 'placement', [1], 's1'), '2026-09-08'), links=links),
            result(*events, links=[*links, dated(link('昼'), '2026-09-08')]),
            result(events[0], events[0]),
            result(events[2], events[2], links=[link('昼')]),
            result(*events[:2], events[0]),
            result(links=[links[0], links[0]]),
            result(links=[*links[:3], links[0]]),
            result(dated(event('夜', 'placement', [1], 's4'), '2026-09-07'), links=[link('昼')]),
            result(links=[link('昼'), dated(link('夜', line_ids=[999]), '2026-09-07')]),
            result(links=[link('昼'), dated(link('朝'), '2026-09-07')]),
        ]
        for proposed in invalid:
            with self.subTest(proposed=proposed), self.assertRaises(azure.AnalysisFailure):
                self.assess(text, proposed)
        self.assertEqual(self.assess(text, result(events[3]), shifts=('昼',)), ([], [], 'no_event'))
        with self.assertRaises(azure.AnalysisFailure):
            self.assess(text, result(events[1]), shifts=('昼',))
        _, _, channels, dates = azure._grounded_v7(
            {'events': [events[3]], 'links': None}, text, base.DATE, ('昼',), personal.azure_context())
        self.assertEqual(channels, {'events': 'none', 'links': 'pending'})
        self.assertEqual(dates, {'events': ['2026-09-07'], 'links': []})

    def test_request_dates_use_original_jst_publication_with_calendar_rollovers(self):
        cases = (
            ('2026-09-05T14:59:59Z', '2026-09-05T23:59:59+09:00',
             ['2026-09-04', '2026-09-05', '2026-09-06', '2026-09-07']),
            ('2026-09-05T15:11:00Z', '2026-09-06T00:11:00+09:00',
             ['2026-09-05', '2026-09-06', '2026-09-07', '2026-09-08']),
            ('2026-09-30T15:00:00Z', '2026-10-01T00:00:00+09:00',
             ['2026-09-30', '2026-10-01', '2026-10-02', '2026-10-03']),
            ('2026-12-31T14:59:59Z', '2026-12-31T23:59:59+09:00',
             ['2026-12-30', '2026-12-31', '2027-01-01', '2027-01-02']),
            ('2026-12-31T15:00:00Z', '2027-01-01T00:00:00+09:00',
             ['2026-12-31', '2027-01-01', '2027-01-02', '2027-01-03']),
            ('2028-02-28T15:00:00Z', '2028-02-29T00:00:00+09:00',
             ['2028-02-28', '2028-02-29', '2028-03-01', '2028-03-02']),
        )
        self.clock = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
        for created, expected_stamp, days in cases:
            with self.subTest(created=created), mock.patch.object(
                    self.analyzer.client, 'structured', return_value=result()) as structured:
                self.analyzer.request(azure.source_lines('今日と明日のお給仕について'),
                                      personal.official.timestamp(created), base.DATE, ['昼'], 'あむ')
                context = json.loads(structured.call_args.args[0][1]['content'])
                self.assertEqual(set(context), {'bodyLines', 'postedAtJST', 'postedDateJST',
                                                'relativeDatesJST', 'author', 'knownShiftsByDate'})
                self.assertEqual(context['postedAtJST'], expected_stamp)
                self.assertEqual(context['postedDateJST'], days[1])
                self.assertEqual(context['relativeDatesJST'], dict(zip(
                    ('yesterday', 'today', 'tomorrow', 'dayAfterTomorrow'), days)))
                self.assertEqual(context['knownShiftsByDate'], {'2026-09-06': ['昼']})
        self.opener.open.assert_not_called()

    def test_explicit_v6_replay_keeps_typed_valid_but_semantically_wrong_negative(self):
        text = '今日、好きな本を買いました。次のお給仕は明日です。'
        old = result_v6(links=[link('unspecified')])
        replay = lambda value: azure.grounded_assessment_v6(
            value, text, base.DATE, base.AMU['shifts'], personal.azure_context())
        self.assertEqual(replay(old), ([], [{'scope': 'unspecified', 'status': 'work'}], 'links'))
        self.assertEqual(replay(result_v6(pending_events=True, links=[link('夜')]))[2], 'links')
        self.assertEqual(replay(result_v6()), ([], [], 'no_event'))
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_pending'):
            replay(result_v6(links=None))
        for value in (result(), result_v5(), legacy_result()):
            with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
                replay(value)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            self.parse(text, old)
        self.opener.open.assert_called_once()

    def test_broad_calendar_and_daily_chatter_mock_no_results_do_not_claim_model_acceptance(self):
        for text in ('9月前半のお給仕予定表です。1日昼、3日夜、6日昼、8日夜。',
                     '今日、好きな本を買いました。'):
            with self.subTest(text=text):
                post, reason = self.parse(text, result())
                self.assertIsNone(post)
                self.assertEqual(reason, 'no_event')
        self.assertEqual(self.opener.open.call_count, 2)

    def test_work_expectation_is_semantic_context_not_a_banned_word(self):
        self.assertIn("Enthusiasm alone is not a dated work announcement", azure.PROMPT)
        self.assertIn("not evidence of a work date", azure.PROMPT)
        # These are mocked decisions, not claims that a live model passed an evaluation.
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_pending'):
            self.parse('次のお給仕が楽しみです', result(links=None))
        post, reason = self.parse('今日9月6日、夜2号店です。お給仕が楽しみです',
                                  result(event('夜', 'placement', [1], 's2'), links=[link('夜')]))
        self.assertEqual(reason, 'events')
        self.assertEqual(post['events'][0]['storeId'], 's2')
        self.assertEqual(post['links'], [{'scope': '夜', 'status': 'work'}])
        self.assertEqual(self.opener.open.call_count, 2)

    def test_optional_link_cache_fields_validate_legacy_and_v7_results(self):
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
            {**entry, 'links': None}, {**entry, 'events': None},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                azure.validate_state({**azure.empty_state(), 'cache': {azure.digest('cache'): value}},
                                     personal.azure_context())

    def test_optional_cache_channels_require_exact_states_consistent_with_facts_and_reason(self):
        entry = {'postId': base.TID, 'bodyHash': azure.digest('body'),
                 'versionHash': azure.digest('v6'), 'at': base.CREATED, 'reason': 'events',
                 'events': [{'shift': '昼', 'kind': 'placement', 'storeId': 's1', 'excerpt': '1号店'}],
                 'links': [], 'channels': {'events': 'confirmed', 'links': 'pending'}}
        azure.validate_state({**azure.empty_state(), 'cache': {azure.digest('cache'): entry}},
                             personal.azure_context())
        invalid_channels = [
            None, [], 'pending', {}, {'events': 'confirmed'}, {'links': 'pending'},
            {'events': 'confirmed', 'links': 'pending', 'body': 'PRIVATE_SENTINEL'},
            {'events': 'confirmed', 'links': 'confirmed'},
            {'events': 'none', 'links': 'pending'},
            {'events': 'pending', 'links': 'none'},
        ]
        for value in (None, True, False, 0, [], {}, 'unknown', 'work'):
            invalid_channels.append({'events': 'confirmed', 'links': value})
        invalid = [{**entry, 'channels': channels} for channels in invalid_channels]
        invalid.extend([
            {**entry, 'events': [], 'reason': 'no_event',
             'channels': {'events': 'none', 'links': 'pending'}},
            {**entry, 'events': [], 'reason': 'azure_pending',
             'channels': {'events': 'none', 'links': 'none'}},
            {**entry, 'events': [], 'reason': 'azure_timeout',
             'channels': {'events': 'pending', 'links': 'pending'}},
        ])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                azure.validate_state({**azure.empty_state(), 'cache': {azure.digest('cache'): value}},
                                     personal.azure_context())
        legacy = {key: value for key, value in entry.items() if key != 'channels'}
        azure.validate_state({**azure.empty_state(), 'cache': {azure.digest('cache'): legacy}},
                             personal.azure_context())

    def test_optional_cache_service_dates_are_private_canonical_sorted_and_bounded(self):
        entry = {'postId': base.TID, 'bodyHash': azure.digest('body'),
                 'versionHash': azure.digest('v7'), 'at': base.CREATED, 'reason': 'no_event',
                 'events': [], 'links': [], 'channels': {'events': 'none', 'links': 'none'},
                 'serviceDates': {'events': [], 'links': ['2026-09-07']}}
        azure.validate_state({**azure.empty_state(), 'cache': {azure.digest('cache'): entry}},
                             personal.azure_context())
        invalid_dates = [
            None, [], {}, {'events': []}, {'events': [], 'links': [], 'raw': 'PRIVATE_SENTINEL'},
            {'events': None, 'links': []}, {'events': [], 'links': '2026-09-07'},
            {'events': [], 'links': [None]}, {'events': [], 'links': ['2026-02-30']},
            {'events': [], 'links': ['2026-9-7']},
            {'events': [], 'links': ['2026-09-07', '2026-09-06']},
            {'events': [], 'links': ['2026-09-07', '2026-09-07']},
            {'events': [f'2026-09-{day:02}' for day in range(1, 6)], 'links': []},
            {'events': [], 'links': [f'2026-09-{day:02}' for day in range(1, 8)]},
        ]
        invalid = [{**entry, 'serviceDates': dates} for dates in invalid_dates]
        invalid.extend([
            {**entry, 'reason': 'azure_pending', 'channels': {'events': 'none', 'links': 'pending'}},
            {**entry, 'reason': 'links', 'links': [{'scope': '昼', 'status': 'work'}],
             'channels': {'events': 'none', 'links': 'confirmed'},
             'serviceDates': {'events': [], 'links': []}},
            {**entry, 'reason': 'azure_invalid_output', 'channels': {'events': 'none', 'links': 'none'}},
        ])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                azure.validate_state({**azure.empty_state(), 'cache': {azure.digest('cache'): value}},
                                     personal.azure_context())
        legacy = {key: value for key, value in entry.items() if key != 'serviceDates'}
        azure.validate_state({**azure.empty_state(), 'cache': {azure.digest('cache'): legacy}},
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
            self.parse('今日 夜2号店かも', result(pending_events=True))
        self.opener.open.assert_called_once()
        for field in ('retweeted_tweet', 'retweeted_status'):
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

    def test_version_change_never_automatically_reissues_known_legacy_results(self):
        cases = [('v4', reason) for reason in ('events', 'no_event', 'azure_pending')]
        cases.extend(('v5', reason) for reason in ('events', 'links', 'no_event',
                                                  'azure_pending', 'azure_invalid_output'))
        cases.extend(('v6', reason) for reason in ('events', 'links', 'no_event',
                                                 'azure_pending', 'azure_ungrounded'))
        cases.extend(('v7', reason) for reason in ('events', 'links', 'no_event',
                                                  'azure_pending', 'azure_invalid_output'))
        cases.extend(('v8', reason) for reason in ('azure_capacity_hold', 'azure_capacity_profile_stale'))
        for version, reason in cases:
            with self.subTest(version=version, reason=reason):
                self.state = personal.empty_state()
                events = ([{'shift': '昼', 'kind': 'placement', 'storeId': 's1', 'excerpt': '1号店'}]
                          if reason == 'events' else [])
                links = [{'scope': 'unspecified', 'status': 'work'}] if reason == 'links' else []
                old = {'postId': base.TID, 'bodyHash': azure.digest('今日昼1号店'),
                       'versionHash': azure.digest('saved-' + version), 'at': base.CREATED,
                       'reason': reason, 'events': events}
                if version in ('v5', 'v6', 'v7'):
                    old['links'] = links
                if version == 'v8':
                    old.update(links=[], workTiming=[])
                self.state['azureAnalysis'] = {
                    **azure.empty_state(), 'cache': {azure.digest('saved-' + version + '-key'): old}}
                if reason.startswith('azure_'):
                    self.known()
                    self.state['pending'][0]['reason'] = reason
                else:
                    self.state['resolved'] = [{
                        'id': base.TID, 'name': 'あむ', 'url': base.candidate()['url'],
                        'date': base.DATE.isoformat(), 'resolvedAt': base.CREATED, 'reason': reason}]
                if events or links:
                    post = personal.validate_post(base.candidate(), base.post(), base.AMU, self.clock)[0]
                    if links:
                        post.update(events=[], links=copy.deepcopy(links))
                    self.state['posts'] = [post]
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
        self.state['azureAnalysis']['budgets']['2026-09-06'] = 40
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
        registry = self.folder / 'members.json'
        personal.official.atomic_json(registry, base.registry_fixture(base.AMU))
        personal.official.atomic_json(saved, {base.TID: base.post('今日 昼1号店')})
        self.opener.open.return_value = response(result(event('昼', 'placement', [1], 's1')))
        args = personal.argument_parser().parse_args([
            '--snapshot', str(self.snapshot), '--http-state', str(self.http),
            '--seed', str(seed), '--schedule', str(schedule), '--insights', str(insights),
            '--accounts', str(accounts), '--members', str(registry),
            '--analysis-backend', 'azure', '--analyze-saved', str(saved)])
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
                ('今日 昼2号店', result(pending_events=True), 'azure_pending'),
                ('今日 晴れ', result(), 'azure_no_event_conflict')):
            self.opener.open.return_value = response(value)
            self.collect(payloads={base.TID: base.post(text)})
            self.assertEqual(self.state['posts'], original)
            self.assertEqual(self.state['resolved'], resolved)
            self.assertEqual(self.state['pending'][0]['reason'], reason)
        self.analyzer.state['budgets']['2026-09-06'] = 40
        self.collect(payloads={base.TID: base.post('今日 昼3号店')})
        self.assertEqual(self.state['pending'][0]['reason'], 'azure_budget_exhausted')
        self.assertEqual(self.state['posts'], original)

    def test_partial_saved_assessments_preserve_existing_events_and_links(self):
        self.known()
        self.opener.open.return_value = response(result(
            event('昼', 'placement', [1], 's1'), links=[link('昼')]))
        self.collect(payloads={base.TID: base.post('今日 昼1号店')})
        original = copy.deepcopy(self.state['posts'][0])
        self.opener.open.return_value = response(result(pending_events=True, links=[link('夜')]))
        self.collect(payloads={base.TID: base.post('今日 夜お給仕します')})
        intermediate = copy.deepcopy(self.state['posts'][0])
        self.assertEqual(intermediate['events'], original['events'])
        self.assertEqual({item['scope']: item['status'] for item in intermediate['links']},
                         {'昼': 'work', '夜': 'work'})
        self.opener.open.return_value = response(result(
            event('昼', 'placement', [1], 's2'), links=None))
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

    def test_pending_events_confirmed_link_crosses_cloud_pages_ui_without_creating_work_facts(self):
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
        self.opener.open.return_value = response(result(pending_events=True, links=[link('unspecified')]))
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
        self.assertEqual(next(iter(checked['azureAnalysis']['cache'].values()))['channels'],
                         {'events': 'pending', 'links': 'confirmed', 'workTiming': 'none'})
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
                        'azureAnalysis', 'cache', 'channels', 'coverage', 'receipts', 'bodyHash', 'versionHash',
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

    def test_confirmed_events_pending_link_crosses_cloud_pages_ui_without_inferred_link(self):
        from test_cloud_collection import cloud
        from test_pages import pages

        executable = shutil.which('node') or str(personal.NODE_FALLBACK)
        if not Path(executable).is_file():
            self.skipTest('Existing Node runtime is unavailable')
        self.known()
        text = '本日昼1号店でお給仕します\nPRIVATE_PENDING_LINK_BODY'
        self.opener.open.return_value = response(result(event('昼', 'placement', [1], 's1'), links=None))
        with self.shared_usage() as ledger:
            analyzer = self.make_analyzer(usage=ledger)
            _, code, client = self.collect(payloads={base.TID: base.post(text)}, analyzer=analyzer)
            self.assertEqual(code, 0)
            self.assertEqual(ledger.used, 1)
            self.assertEqual(next(iter(ledger.state['receipts'].values()))['reason'], 'events')
        client.search.assert_not_called()
        client.fetch_post.assert_not_called()
        self.opener.open.assert_called_once()
        checked, raw = cloud.validate_personal(self.snapshot, personal)
        self.assertEqual(raw, self.snapshot.read_bytes())
        self.assertEqual(next(iter(checked['azureAnalysis']['cache'].values()))['channels'],
                         {'events': 'confirmed', 'links': 'pending', 'workTiming': 'none'})
        self.assertEqual(checked['posts'][0]['links'], [])
        public = pages.personal_projection(checked, collector=personal.official)
        self.assertEqual(public['posts'][0]['events'], [
            {'shift': '昼', 'kind': 'placement', 'storeId': 's1', 'excerpt': '1号店'}])
        self.assertEqual(public['posts'][0]['links'], [])
        encoded = json.dumps(public, ensure_ascii=False)
        for private in ('PRIVATE_PENDING_LINK_BODY', text, 'bodyLines', 'evidenceLineIds',
                        'channels', 'azureAnalysis', 'cache', 'receipts'):
            self.assertNotIn(private, encoded)
        script = """
const assert=require('node:assert/strict'),fs=require('node:fs'),api=require('./app.js');
const snapshot=api.validatePersonalShifts(JSON.parse(fs.readFileSync(0,'utf8')));
const post=snapshot.posts[0];
assert.ok(Object.hasOwn(post,'links'));
assert.deepEqual(post.links,[]);
const options={personal:snapshot,dateKey:post.date,name:post.name,shift:'昼',
  insights:{stores:[{id:'s1'}],maidTendency:{[post.name]:{x:post.authorScreenName}}},
  schedule:{},roster:[post.name]};
const roster=api.resolveShiftRoster(options);
assert.equal(roster.personal.byMaid.get(post.name).storeId,'s1');
assert.equal(roster.entries.length,1);
assert.equal(api.personalPostLink(options),null);
assert.equal(api.personalPostLink({...options,shift:'夜'}),null);
const legacy=JSON.parse(JSON.stringify(snapshot));
delete legacy.posts[0].links;
assert.equal(api.personalPostLink({...options,personal:legacy}).url,post.url);
assert.deepEqual(snapshot.posts[0].links,[]);
process.stdout.write(JSON.stringify({store:roster.personal.byMaid.get(post.name).storeId,link:null}));
"""
        process = subprocess.run(
            [executable, '-e', script], cwd=personal.ROOT, input=encoded,
            capture_output=True, encoding='utf-8', timeout=10, check=True,
            env={key: value for key, value in os.environ.items() if not key.startswith('AZURE_OPENAI_')},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(json.loads(process.stdout), {'store': 's1', 'link': None})

    def test_future_only_and_mixed_dates_cross_cloud_pages_ui_without_creating_work_facts(self):
        from test_cloud_collection import cloud
        from test_pages import pages

        executable = shutil.which('node') or str(personal.NODE_FALLBACK)
        if not Path(executable).is_file():
            self.skipTest('Existing Node runtime is unavailable')
        cases = (
            ('今日、好きな本を買いました。次のお給仕は明日です。',
             [dated(link('unspecified'), '2026-09-07')], None, ['2026-09-07']),
            ('今日と明日はお給仕します。',
             [dated(link('unspecified'), '2026-09-07'), link('unspecified')],
             base.candidate()['url'], ['2026-09-06', '2026-09-07']),
        )
        for text, links, url, dates in cases:
            with self.subTest(dates=dates):
                self.state = personal.empty_state()
                analyzer = self.make_analyzer()
                self.known()
                self.opener.reset_mock()
                self.opener.open.return_value = response(result(links=links))
                _, code, client = self.collect(payloads={base.TID: base.post(text)}, analyzer=analyzer)
                self.assertEqual(code, 0)
                client.search.assert_not_called()
                client.fetch_post.assert_not_called()
                self.opener.open.assert_called_once()
                checked, raw = cloud.validate_personal(self.snapshot, personal)
                self.assertEqual(raw, self.snapshot.read_bytes())
                cached = next(iter(checked['azureAnalysis']['cache'].values()))
                self.assertEqual(cached['serviceDates'], {'events': [], 'links': dates, 'workTiming': []})
                self.assertEqual(cached['channels'], {
                    'events': 'none', 'links': 'confirmed' if url else 'none', 'workTiming': 'none'})
                public = pages.personal_projection(checked, collector=personal.official)
                self.assertEqual(len(public['posts']), 1 if url else 0)
                if url:
                    self.assertEqual(public['posts'][0]['date'], base.DATE.isoformat())
                    self.assertEqual(public['posts'][0]['events'], [])
                encoded = json.dumps(public, ensure_ascii=False)
                for private in ('serviceDate', 'channels', 'azureAnalysis', 'evidenceLineIds',
                                'bodyLines', text, 'receipts'):
                    self.assertNotIn(private, encoded)
                script = """
const assert=require('node:assert/strict'),fs=require('node:fs'),api=require('./app.js');
const input=JSON.parse(fs.readFileSync(0,'utf8')),personal=api.validatePersonalShifts(input.personal);
const insights={stores:[{id:'s1'}],maidTendency:{[input.name]:{x:input.handle}}};
for(const shift of ['昼','夜']){
  const options={personal,insights,dateKey:input.date,shift,name:input.name,roster:[input.name],
    schedule:{[input.date]:{[shift]:[{name:input.name}]}}};
  const roster=api.resolveShiftRoster(options);
  assert.deepEqual(roster,api.resolveShiftRoster({...options,personal:null}));
  assert.deepEqual(roster.entries,[{name:input.name}]);
  assert.equal(roster.personal.byMaid.size,0);
  assert.deepEqual(api.resolveShiftRoster({...options,schedule:{}}).entries,[]);
  const source=api.personalPostLink(options);
  assert.equal(source?.url??null,input.url);
}
assert.equal(api.dayHasPersonStoreEvidence(insights,null,input.date,personal),false);
process.stdout.write('ok');
"""
                process = subprocess.run(
                    [executable, '-e', script], cwd=personal.ROOT,
                    input=json.dumps({'personal': public, 'name': base.AMU['name'],
                                      'handle': base.AMU['handle'], 'date': base.DATE.isoformat(), 'url': url}),
                    capture_output=True, encoding='utf-8', timeout=10, check=True,
                    env={key: value for key, value in os.environ.items() if not key.startswith('AZURE_OPENAI_')},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                self.assertEqual(process.stdout, 'ok')

    def test_future_only_reanalysis_cannot_delete_existing_target_events_or_links(self):
        self.known()
        self.opener.open.return_value = response(result(
            event('昼', 'placement', [1], 's1'), links=[link('昼')]))
        self.collect(payloads={base.TID: base.post('本日昼1号店でお給仕します')})
        original = copy.deepcopy(self.state['posts'])
        self.opener.open.return_value = response(result(links=[dated(link('夜'), '2026-09-07')]))
        self.collect(payloads={base.TID: base.post('次のお給仕は明日夜です')})
        self.assertEqual(self.state['posts'], original)
        self.assertEqual(self.state['pending'][0]['reason'], 'azure_no_event_conflict')
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertTrue(any(entry.get('serviceDates', {}).get('links') == ['2026-09-07']
                            for entry in self.analyzer.state['cache'].values()))
        personal.read_state(self.snapshot)


if __name__ == '__main__':
    unittest.main()
