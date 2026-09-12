"""Offline shared accounting and actual cross-process lock regressions."""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('test_shared_usage', TOOLS / 'analysis-state.py')
usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage)
NOW = dt.datetime(2026, 9, 7, 0, tzinfo=dt.timezone.utc)
IDENTITY = {'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
            'deployment': 'gpt-5.6-luna', 'model': 'gpt-5.6-luna', 'modelVersion': '2026-07-09'}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def historical(date='2026-09-07', count=4, label='approved', breakdown=None):
    return {'receiptId': digest(label), 'date': date, 'counts': {'requests': count},
            'modelBreakdown': breakdown or [{'model': 'gpt-5.6-luna', 'kind': 'text', 'count': count}],
            'sourceHash': digest('provenance:' + label)}


def source_receipt(date='2026-09-06', searches=8, posts=6, label='approved-source'):
    return {'receiptId': digest(label), 'date': date, 'searches': searches, 'posts': posts,
            'sourceHash': digest('source-provenance:' + label)}


def result_attestation(key):
    return {'contract': 'half-month-timing-v1', 'requestHash': key, 'resultHash': digest('raw-result'),
            'semanticResultHash': digest('normalized-result'), 'analysisHash': digest('analysis'), 'consumed': 1}


class UsageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=TOOLS / 'tests', prefix='usage-test-')
        self.addCleanup(folder.cleanup)
        self.folder, self.clock, self.sleeps = Path(folder.name), NOW, []
        self.path = self.folder / 'ai-usage.json'
        usage.atomic_json(self.path, usage.empty_state())

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += dt.timedelta(seconds=seconds)

    def shared(self, component='official', run_id='run-1', **kwargs):
        return usage.SharedUsage(self.path, component=component, run_id=run_id,
                                 clock=lambda: self.clock, sleep=self.sleep, **kwargs)

    def completed(self, ledger, label, reason='no_event'):
        key = digest(label)
        ledger.reserve(key, IDENTITY)
        ledger.issued(key)
        ledger.finish(key, reason)
        return key

    def test_catchup_same_run_has_sixteen_shared_slots_but_keeps_daily_forty_and_spacing(self):
        with self.shared('personal', request_limit=14, run_limit=16) as ledger:
            for index in range(14):
                self.completed(ledger, f'personal-{index}')
            with self.assertRaises(usage.UsageFailure):
                ledger.check()
        for component in ('schedule', 'official'):
            with self.shared(component, request_limit=1, run_limit=16) as ledger:
                self.completed(ledger, component)
        with self.shared('official', request_limit=3, run_limit=16) as ledger:
            with self.assertRaises(usage.UsageFailure):
                ledger.check()
        self.assertTrue(all(seconds >= 60 for seconds in self.sleeps))
        with self.shared('personal', run_id='next-run', request_limit=14, run_limit=16) as ledger:
            for index in range(14):
                self.completed(ledger, f'next-{index}')
        with self.shared('personal', run_id='third-run', request_limit=14, run_limit=16) as ledger:
            for index in range(10):
                self.completed(ledger, f'last-{index}')
            with self.assertRaises(usage.UsageFailure):
                ledger.check()
            self.assertEqual(usage.usage_counts(ledger.state, 'third-run', self.clock, 16)['day'], 40)

    def child(self, code):
        process = subprocess.run(
            [sys.executable, '-c', code, str(TOOLS / 'analysis-state.py'), str(self.path)],
            cwd=TOOLS.parent, capture_output=True, text=True, timeout=15,
            env={key: value for key, value in os.environ.items() if not key.startswith('AZURE_OPENAI_')},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return process

    def test_missing_is_not_an_authoritative_zero_and_creation_is_explicit(self):
        self.path.unlink()
        self.assertIsNone(usage.load_state(self.path))
        with self.assertRaisesRegex(ValueError, 'missing_ai_usage'):
            with self.shared():
                self.fail('Missing production ledger must not open')
        self.assertFalse(self.path.exists())
        with self.shared(create=True) as ledger:
            self.assertEqual(ledger.state, usage.empty_state())
        self.assertEqual(usage.load_state(self.path, required=True), usage.empty_state())

    def test_failure_type_is_bound_to_the_loaded_shared_module(self):
        self.assertIs(usage.SharedUsage.failure_type, usage.UsageFailure)
        spec = importlib.util.spec_from_file_location('other_usage_import', TOOLS / 'analysis-state.py')
        other = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(other)
        self.assertIs(other.SharedUsage.failure_type, other.UsageFailure)
        self.assertIsNot(other.SharedUsage.failure_type, usage.UsageFailure)

    def test_optional_result_attestation_is_single_consumption_immutable_and_phase_checked(self):
        key = digest('attested')
        attestation = result_attestation(key)
        with self.shared('schedule') as ledger:
            ledger.reserve(key, IDENTITY)
            before = copy.deepcopy(ledger.state)
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_interrupted'):
                ledger.finish(key, 'azure_pending', result_attestation=attestation)
            self.assertEqual(ledger.state, before)
            ledger.issued(key)
            ledger.finish(key, 'events', result_attestation=attestation)
            frozen = self.path.read_bytes()
            ledger.finish(key, 'events', result_attestation=copy.deepcopy(attestation))
            ledger.finish(key, 'events')
            self.assertEqual(self.path.read_bytes(), frozen)
            for change in ('analysisHash', 'consumed', 'contract', 'requestHash'):
                altered = copy.deepcopy(attestation)
                altered[change] = 2 if change == 'consumed' else digest('other')
                with self.subTest(change=change), self.assertRaises((ValueError, usage.UsageFailure)):
                    ledger.finish(key, 'events', result_attestation=altered)
                self.assertEqual(self.path.read_bytes(), frozen)
            self.assertEqual(usage.usage_counts(ledger.state, 'run-1', self.clock)['run'], 1)
            self.assertEqual(ledger.state['imports'], {})
            receipt = next(iter(ledger.state['receipts'].values()))
            attestation['analysisHash'] = digest('mutated-caller')
            self.assertNotEqual(receipt['resultAttestation'], attestation)

    def test_attested_external_receipt_blocks_second_import_and_native_reservation(self):
        key = digest('approved-external-result')
        receipt = historical(count=1, breakdown=[{'model': IDENTITY['model'], 'kind': 'image',
                                                 'component': 'schedule', 'count': 1}])
        receipt['resultAttestation'] = result_attestation(key)
        state = usage.empty_state()
        usage.apply_import(state, receipt)
        before = copy.deepcopy(state)
        usage.apply_import(state, copy.deepcopy(receipt))
        self.assertEqual(state, before)
        duplicate = copy.deepcopy(receipt)
        duplicate.update(receiptId=digest('second-import'), sourceHash=digest('second-source'))
        with self.assertRaisesRegex(ValueError, 'duplicate_ai_usage_result'):
            usage.apply_import(state, duplicate)
        self.assertEqual(state, before)
        usage.atomic_json(self.path, state)
        with self.shared('schedule') as ledger:
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_already_analyzed'):
                ledger.reserve(key, IDENTITY)
            self.assertEqual(ledger.state, before)
            self.assertEqual(ledger.used, 0)
            self.assertEqual(usage.usage_counts(ledger.state, 'run-1', self.clock)['day'], 1)

    def test_attestation_rejects_native_import_double_count_and_invalid_private_shapes(self):
        key = digest('native-result')
        with self.shared('schedule') as ledger:
            ledger.reserve(key, IDENTITY)
            ledger.issued(key)
            ledger.finish(key, 'no_event', result_attestation=result_attestation(key))
            before = copy.deepcopy(ledger.state)
            receipt = historical(count=1, breakdown=[{'model': IDENTITY['model'], 'kind': 'image',
                                                     'component': 'schedule', 'count': 1}])
            receipt['resultAttestation'] = result_attestation(key)
            with self.assertRaisesRegex(ValueError, 'duplicate_ai_usage_result'):
                usage.apply_import(ledger.state, receipt)
            self.assertEqual(ledger.state, before)
            invalid = copy.deepcopy(before)
            invalid['imports'][receipt['receiptId']] = receipt
            with self.assertRaises(ValueError):
                usage.validate_state(invalid)
            native_id = usage._receipt_id('schedule', key)
            for change in ('boolConsumption', 'rawResponse', 'unissued', 'uncompleted', 'failure'):
                invalid = copy.deepcopy(before)
                native = invalid['receipts'][native_id]
                if change == 'boolConsumption':
                    native['resultAttestation']['consumed'] = True
                elif change == 'rawResponse':
                    native['resultAttestation']['rawResponse'] = {'slots': None}
                elif change == 'unissued':
                    native['issuedAt'] = None
                elif change == 'uncompleted':
                    native['completedAt'] = None
                    native['reason'] = 'azure_interrupted'
                else:
                    native['reason'] = 'azure_invalid_output'
                with self.subTest(change=change), self.assertRaises(ValueError):
                    usage.validate_state(invalid)

    def test_preflight_never_saves_spends_or_waits_even_when_spacing_is_pending(self):
        with self.shared() as ledger:
            original = self.path.read_bytes()
            self.assertIsNone(ledger.check())
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(ledger.used, 0)
            self.completed(ledger, 'one')
            original = self.path.read_bytes()
            self.assertIsNone(ledger.check())
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(ledger.used, 1)
            self.assertEqual(self.sleeps, [])

    def test_preflight_blocks_own_global_day_deadline_and_pause_without_mutation(self):
        cases = (
            ('own', 'azure_budget_exhausted'), ('global', 'azure_budget_exhausted'),
            ('day', 'azure_budget_exhausted'), ('deadline', 'azure_deadline'),
            ('auth', 'azure_auth_stopped'), ('rate', 'azure_backoff'),
        )
        for case, reason in cases:
            with self.subTest(case=case):
                usage.atomic_json(self.path, usage.empty_state())
                self.clock, self.sleeps = NOW, []
                if case == 'global':
                    with self.shared('personal') as previous:
                        for index in range(3):
                            self.completed(previous, str(index))
                if case == 'day':
                    state = usage.load_state(self.path)
                    usage.apply_import(state, historical(count=40))
                    usage.atomic_json(self.path, state)
                with self.shared(request_limit=0 if case == 'own' else 3,
                                 deadline=(lambda: False) if case == 'deadline' else None) as ledger:
                    if case in ('auth', 'rate'):
                        with self.assertRaises(usage.UsageFailure):
                            ledger.http_failure(401 if case == 'auth' else 429, None)
                    original, sleeps = self.path.read_bytes(), list(self.sleeps)
                    with self.assertRaisesRegex(usage.UsageFailure, reason):
                        ledger.check()
                    self.assertEqual(self.path.read_bytes(), original)
                    self.assertEqual(ledger.used, 0)
                    self.assertEqual(self.sleeps, sleeps)

    def test_two_children_share_three_not_six_and_private_quota_is_separate(self):
        with self.shared(request_limit=1) as official:
            self.completed(official, 'official-1')
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_budget_exhausted'):
                official.reserve(digest('official-2'), IDENTITY)
            self.assertEqual(official.used, 1)
        with self.shared('personal', request_limit=2) as personal:
            self.completed(personal, 'personal-1')
            self.completed(personal, 'personal-2')
            self.assertEqual(personal.used, 2)
        with self.shared() as official:
            self.assertEqual(official.used, 1)
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_budget_exhausted'):
                official.reserve(digest('official-2'), IDENTITY)
        self.assertEqual(usage.usage_counts(usage.load_state(self.path), 'run-1', self.clock),
                         {'run': 3, 'day': 3, 'remaining': 0})
        self.assertEqual(self.sleeps, [60, 60])

    def test_schedule_is_third_shared_component_with_one_per_run_and_success_dedup(self):
        with self.shared('official') as ledger:
            self.completed(ledger, 'official')
        with self.shared('personal') as ledger:
            self.completed(ledger, 'personal', 'events')
        for reason in ('schedule', 'not_schedule'):
            with self.subTest(reason=reason):
                run_id = 'run-1' if reason == 'schedule' else 'run-2'
                with self.shared('schedule', run_id=run_id) as ledger:
                    key = self.completed(ledger, reason, reason)
                    with self.assertRaisesRegex(usage.UsageFailure, 'azure_already_analyzed'):
                        ledger.reserve(key, IDENTITY)
                    with self.assertRaisesRegex(usage.UsageFailure, 'azure_budget_exhausted'):
                        ledger.reserve(digest(reason + ':second'), IDENTITY)
                with self.shared('schedule', run_id=run_id) as ledger:
                    with self.assertRaisesRegex(usage.UsageFailure, 'azure_budget_exhausted'):
                        ledger.check()
        self.assertEqual(usage.usage_counts(usage.load_state(self.path), 'run-1', self.clock),
                         {'run': 3, 'day': 4, 'remaining': 0})
        self.assertEqual(self.sleeps, [60, 60, 60])

    def test_schedule_success_requires_issued_and_shared_forty_day_counts_imports(self):
        state = usage.load_state(self.path)
        imported = historical(count=39, breakdown=[
            {'model': 'gpt-5.6-luna', 'kind': 'image', 'component': 'schedule', 'count': 39}])
        usage.apply_import(state, imported)
        old = copy.deepcopy(state)
        usage.atomic_json(self.path, state)
        with self.shared('schedule') as ledger:
            key = digest('schedule-unissued')
            ledger.reserve(key, IDENTITY)
            for reason in ('schedule', 'not_schedule'):
                with self.assertRaisesRegex(usage.UsageFailure, 'azure_interrupted'):
                    ledger.finish(key, reason)
            ledger.issued(key)
            ledger.finish(key, 'schedule')
        for component in ('official', 'personal', 'schedule'):
            with self.shared(component, run_id='different') as ledger:
                with self.assertRaisesRegex(usage.UsageFailure, 'azure_budget_exhausted'):
                    ledger.check()
        final = usage.load_state(self.path)
        self.assertEqual(final['imports'], old['imports'])
        self.assertEqual(final['sourceImports'], old['sourceImports'])

    def test_external_model_records_import_once_and_forty_actual_day_cap(self):
        previous = historical('2026-09-06', 24, 'old-models', [
            {'model': 'gpt-5.4-nano', 'kind': 'text', 'count': 18},
            {'model': 'gpt-5.4-mini', 'kind': 'text', 'count': 6,
             'deployment': 'gpt-5.4-mini-compare', 'modelVersion': '2026-03-17'}])
        current = historical(breakdown=[
            {'model': 'gpt-5.6-luna', 'kind': 'image', 'count': 1,
             'deployment': 'gpt-5.6-luna-compare', 'modelVersion': '2026-07-09', 'component': 'external'},
            {'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 3,
             'deployment': 'gpt-5.6-luna', 'modelVersion': '2026-07-09', 'component': 'personal'}])
        state = usage.load_state(self.path)
        for receipt in (previous, current, previous, current):
            self.assertIs(usage.apply_import(state, receipt), state)
        usage.atomic_json(self.path, state)
        for index in range(36):
            with self.shared(run_id='run-' + str(index // 3)) as ledger:
                self.completed(ledger, str(index))
        with self.shared(run_id='unused-run') as ledger:
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_budget_exhausted'):
                ledger.reserve(digest('41st'), IDENTITY)
        state = usage.load_state(self.path)
        self.assertEqual(usage.usage_counts(state, 'unused-run', self.clock),
                         {'run': 0, 'day': 40, 'remaining': 0})
        self.assertEqual(state['imports'][previous['receiptId']], previous)
        self.assertEqual(state['imports'][current['receiptId']], current)
        self.assertEqual(len(state['receipts']), 36)

    def test_import_conflicts_duplicate_provenance_and_unknowns_do_not_change_state(self):
        state = usage.empty_state()
        receipt = historical()
        usage.apply_import(state, receipt)
        original = copy.deepcopy(state)
        cases = [
            {**receipt, 'date': '2026-09-08'},
            {**receipt, 'receiptId': digest('different')},
            {**receipt, 'body': 'PRIVATE_SENTINEL'},
            {**receipt, 'counts': {'requests': True}},
            {**receipt, 'modelBreakdown': [{'model': 'model', 'kind': 'text', 'count': 3}]},
            {**receipt, 'sourceHash': 'not-a-hash'},
            {**receipt, 'modelBreakdown': [{'model': 'PRIVATE SENTINEL', 'kind': 'text', 'count': 4}]},
            {**receipt, 'counts': {'requests': 4, 'tokens': 1}},
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                usage.apply_import(state, invalid)
            self.assertEqual(state, original)

    def test_import_distinguishes_deployment_version_and_component_without_relabeling(self):
        rows = [
            {'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 1, 'deployment': 'gpt-5.6-luna-compare',
             'modelVersion': '2026-07-09', 'component': 'external'},
            {'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 1, 'deployment': 'gpt-5.6-luna',
             'modelVersion': '2026-07-09', 'component': 'official'},
            {'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 1, 'deployment': 'gpt-5.6-luna',
             'modelVersion': '2026-07-09', 'component': 'personal'},
            {'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 1, 'deployment': 'gpt-5.6-luna',
             'modelVersion': '2026-07-10', 'component': 'personal'},
        ]
        receipt, state = historical(breakdown=rows), usage.empty_state()
        usage.apply_import(state, receipt)
        usage.atomic_json(self.path, state)
        self.assertEqual(usage.load_state(self.path)['imports'][receipt['receiptId']]['modelBreakdown'], rows)
        invalid_rows = [
            {**rows[0], 'endpoint': 'PRIVATE_SENTINEL'},
            {**rows[0], 'component': 'PRIVATE_SENTINEL'},
            {**rows[0], 'deployment': 'not a deployment'},
            {**rows[0], 'modelVersion': None},
        ]
        for row in invalid_rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                usage.apply_import(usage.empty_state(), historical(count=1, breakdown=[row]))
        with self.assertRaises(ValueError):
            usage.apply_import(usage.empty_state(), historical(count=2, breakdown=[rows[0], rows[0]]))

    def test_source_import_is_once_only_separate_from_ai_and_preserves_other_days(self):
        state = usage.empty_state()
        usage.apply_import(state, historical())
        personal_state = {'budgets': {'2026-09-06': {'searches': 7, 'posts': 2},
                                      '2026-09-07': {'searches': 1, 'posts': 1}},
                          'paused': {'reason': 'unmodified'}}
        receipt = source_receipt()
        self.assertIs(usage.apply_source_import(state, receipt, personal_state), state)
        original = copy.deepcopy((state, personal_state))
        self.assertIs(usage.apply_source_import(state, receipt, personal_state), state)
        self.assertEqual((state, personal_state), original)
        self.assertEqual(personal_state['budgets']['2026-09-06'], {'searches': 15, 'posts': 8})
        self.assertEqual(personal_state['budgets']['2026-09-07'], {'searches': 1, 'posts': 1})
        self.assertEqual(personal_state['paused'], {'reason': 'unmodified'})
        self.assertEqual(usage.usage_counts(state, 'run-1', NOW), {'run': 0, 'day': 4, 'remaining': 3})
        usage.atomic_json(self.path, state)
        self.assertEqual(usage.load_state(self.path), state)

    def test_source_import_accepts_old_ledger_without_source_field_and_arbitrary_dates(self):
        state = usage.empty_state()
        del state['sourceImports']
        usage.atomic_json(self.path, state)
        self.assertEqual(usage.load_state(self.path), state)
        personal_state = {'budgets': {}}
        receipt = source_receipt('2030-01-03', searches=0, posts=1)
        usage.apply_source_import(state, receipt, personal_state)
        self.assertEqual(personal_state['budgets'], {'2030-01-03': {'searches': 0, 'posts': 1}})
        usage.validate_state(state)

    def test_invalid_source_imports_do_not_mutate_either_state(self):
        receipt = source_receipt()
        cases = [
            {**receipt, 'raw': 'PRIVATE_BODY_SENTINEL'}, {**receipt, 'searches': True},
            {**receipt, 'searches': -1}, {**receipt, 'posts': 0, 'searches': 0},
            {**receipt, 'posts': 100, 'searches': 1}, {**receipt, 'sourceHash': 'invalid'},
            {**receipt, 'date': '2026-9-6'}, {**receipt, 'posts': 1.5},
            {**receipt, 'searches': 61}, {**receipt, 'posts': 41},
        ]
        for invalid in cases:
            state, personal_state = usage.empty_state(), {'budgets': {}}
            original = copy.deepcopy((state, personal_state))
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                usage.apply_source_import(state, invalid, personal_state)
            self.assertEqual((state, personal_state), original)

    def test_source_import_conflicts_and_rolled_back_budget_fail_closed(self):
        state, personal_state = usage.empty_state(), {'budgets': {}}
        receipt = source_receipt()
        usage.apply_source_import(state, receipt, personal_state)
        original = copy.deepcopy((state, personal_state))
        for invalid in ({**receipt, 'searches': 9}, {**receipt, 'receiptId': digest('different')}):
            with self.assertRaises(ValueError):
                usage.apply_source_import(state, invalid, personal_state)
            self.assertEqual((state, personal_state), original)
        personal_state['budgets'][receipt['date']]['posts'] -= 1
        rolled_back = copy.deepcopy((state, personal_state))
        for next_receipt in (receipt, source_receipt(label='another')):
            with self.assertRaisesRegex(ValueError, 'source_usage_budget_mismatch'):
                usage.apply_source_import(state, next_receipt, personal_state)
            self.assertEqual((state, personal_state), rolled_back)

    def test_source_import_markers_are_strict_and_do_not_share_caller_objects(self):
        state, personal_state = usage.empty_state(), {'budgets': {}}
        receipt = source_receipt()
        usage.apply_source_import(state, receipt, personal_state)
        original = copy.deepcopy(state)
        receipt['posts'] = 99
        personal_state['budgets']['2026-09-06']['posts'] += 1
        self.assertEqual(state, original)
        receipt_id = next(iter(state['sourceImports']))
        for target in ('record', 'receipt', 'budgetBefore', 'budgetAfter'):
            invalid = copy.deepcopy(state)
            obj = invalid['sourceImports'][receipt_id]
            if target != 'record':
                obj = obj[target]
            obj['body'] = 'PRIVATE_BODY_SENTINEL'
            with self.subTest(target=target), self.assertRaises(ValueError):
                usage.validate_state(invalid)
        invalid = copy.deepcopy(state)
        invalid['sourceImports'][receipt_id]['budgetAfter']['posts'] += 1
        with self.assertRaises(ValueError):
            usage.validate_state(invalid)

    def test_reservation_issue_completion_and_repeat_never_count_twice(self):
        key = digest('one')
        with self.shared() as ledger:
            ledger.reserve(key, IDENTITY)
            reserved = usage.load_state(self.path)
            receipt = next(iter(reserved['receipts'].values()))
            self.assertIsNone(receipt['issuedAt'])
            self.assertEqual(receipt['reason'], 'azure_interrupted')
            ledger.issued(key)
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_interrupted'):
                ledger.issued(key)
            ledger.finish(key, 'no_event')
            ledger.finish(key, 'no_event')
            self.assertEqual(ledger.used, 1)
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_already_analyzed'):
                ledger.reserve(key, IDENTITY)
            self.assertEqual(usage.usage_counts(ledger.state, 'run-1', self.clock)['day'], 1)
        with self.shared(run_id='run-2') as ledger:
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_already_analyzed'):
                ledger.reserve(key, IDENTITY)
            self.assertEqual(ledger.used, 0)

    def test_link_success_is_durable_idempotent_and_never_reserved_again(self):
        key = digest('link-only')
        with self.shared('personal') as ledger:
            ledger.reserve(key, IDENTITY)
            ledger.issued(key)
            issued = next(iter(usage.load_state(self.path)['receipts'].values()))
            self.assertIsNotNone(issued['issuedAt'])
            self.assertIsNone(issued['completedAt'])
            self.assertEqual(issued['reason'], 'azure_interrupted')
            ledger.finish(key, 'links')
            completed = self.path.read_bytes()
            ledger.finish(key, 'links')
            self.assertEqual(self.path.read_bytes(), completed)
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_interrupted'):
                ledger.issued(key)
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_interrupted'):
                ledger.finish(key, 'no_event')
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_already_analyzed'):
                ledger.reserve(key, IDENTITY)
            self.assertEqual(self.path.read_bytes(), completed)
            self.assertEqual(ledger.used, 1)
        state = usage.load_state(self.path)
        receipt = next(iter(state['receipts'].values()))
        self.assertEqual(receipt['reason'], 'links')
        self.assertEqual(receipt['issuedAt'], issued['issuedAt'])
        self.assertIsNotNone(receipt['completedAt'])
        self.assertEqual(set(receipt), set(issued))
        self.assertEqual(state['schemaVersion'], 1)
        with self.shared('personal', run_id='run-2') as ledger:
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_already_analyzed'):
                ledger.reserve(key, IDENTITY)
            self.assertEqual(ledger.used, 0)
            self.assertEqual(usage.usage_counts(ledger.state, 'run-2', self.clock),
                             {'run': 0, 'day': 1, 'remaining': 3})
        self.assertEqual(self.path.read_bytes(), completed)
        self.assertEqual(self.sleeps, [])

    def test_link_success_requires_issued_and_completed_receipt(self):
        key = digest('link-only')
        with self.shared('personal') as ledger:
            ledger.reserve(key, IDENTITY)
            original = self.path.read_bytes()
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_interrupted'):
                ledger.finish(key, 'links')
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(ledger.used, 1)
            ledger.issued(key)
            ledger.finish(key, 'links')
        original = usage.load_state(self.path)
        for field in ('issuedAt', 'completedAt'):
            invalid = copy.deepcopy(original)
            next(iter(invalid['receipts'].values()))[field] = None
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'invalid_ai_usage'):
                usage.validate_state(invalid)
        self.assertEqual(usage.load_state(self.path), original)

    def test_same_key_in_other_component_is_a_distinct_request(self):
        with self.shared() as ledger:
            self.completed(ledger, 'same')
        with self.shared('personal') as ledger:
            self.completed(ledger, 'same')
            self.assertEqual(len(ledger.state['receipts']), 2)

    def test_midnight_after_spacing_checks_the_actual_new_day(self):
        state = usage.load_state(self.path)
        usage.apply_import(state, historical('2026-09-08', 40, 'tomorrow'))
        usage.atomic_json(self.path, state)
        self.clock = dt.datetime(2026, 9, 7, 14, 59, 30, tzinfo=dt.timezone.utc)
        with self.shared() as ledger:
            self.completed(ledger, 'before-midnight')
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_budget_exhausted'):
                ledger.reserve(digest('after-midnight'), IDENTITY)
            self.assertEqual(len(ledger.state['receipts']), 1)
        self.assertEqual(self.clock.astimezone(usage.JST).date().isoformat(), '2026-09-08')

    def test_after_midnight_reservation_is_charged_to_new_day(self):
        self.clock = dt.datetime(2026, 9, 7, 14, 59, 30, tzinfo=dt.timezone.utc)
        with self.shared() as ledger:
            self.completed(ledger, 'before')
            self.completed(ledger, 'after')
            self.assertEqual([entry['date'] for entry in ledger.state['receipts'].values()],
                             ['2026-09-07', '2026-09-08'])
            self.assertEqual(usage.usage_counts(ledger.state, 'run-1', self.clock),
                             {'run': 2, 'day': 1, 'remaining': 1})

    def test_issue_rechecks_actual_day_without_double_counting_reservation(self):
        self.clock = dt.datetime(2026, 9, 7, 14, 59, 59, tzinfo=dt.timezone.utc)
        with self.shared() as ledger:
            key = digest('boundary')
            ledger.reserve(key, IDENTITY)
            self.clock += dt.timedelta(seconds=2)
            ledger.issued(key)
            ledger.finish(key, 'no_event')
            self.assertEqual(usage.usage_counts(ledger.state, 'run-1', self.clock),
                             {'run': 1, 'day': 1, 'remaining': 2})
            self.assertEqual(usage.usage_counts(ledger.state, 'run-1', self.clock - dt.timedelta(days=1))['day'], 0)

    def test_issue_after_midnight_cannot_use_an_exhausted_new_day(self):
        state = usage.load_state(self.path)
        usage.apply_import(state, historical('2026-09-08', 40, 'tomorrow'))
        usage.atomic_json(self.path, state)
        self.clock = dt.datetime(2026, 9, 7, 14, 59, 59, tzinfo=dt.timezone.utc)
        with self.shared() as ledger:
            key = digest('boundary')
            ledger.reserve(key, IDENTITY)
            self.clock += dt.timedelta(seconds=2)
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_budget_exhausted'):
                ledger.issued(key)
            ledger.finish(key, 'azure_budget_exhausted')
            receipt = next(iter(ledger.state['receipts'].values()))
            self.assertIsNone(receipt['issuedAt'])
            self.assertEqual(receipt['date'], '2026-09-07')
            self.assertEqual(ledger.used, 1)

    def test_deadline_after_sleep_does_not_reserve_or_issue(self):
        deadline = self.clock + dt.timedelta(seconds=30)
        with self.shared(deadline=lambda: self.clock < deadline) as ledger:
            self.completed(ledger, 'first')
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_deadline'):
                ledger.reserve(digest('late'), IDENTITY)
            self.assertEqual(len(ledger.state['receipts']), 1)
            self.assertEqual(self.sleeps, [60])
        with self.shared(run_id='next', deadline=lambda: False) as ledger:
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_deadline'):
                ledger.reserve(digest('never'), IDENTITY)
            self.assertEqual(ledger.used, 0)

    def test_issue_deadline_leaves_one_conservatively_spent_reservation(self):
        allowed = True
        with self.shared(deadline=lambda: allowed) as ledger:
            key = digest('stopped')
            ledger.reserve(key, IDENTITY)
            allowed = False
            with self.assertRaisesRegex(usage.UsageFailure, 'azure_deadline'):
                ledger.issued(key)
            ledger.finish(key, 'azure_deadline')
            self.assertEqual(ledger.used, 1)
            self.assertIsNone(next(iter(ledger.state['receipts'].values()))['issuedAt'])

    def test_429_auth_and_negative_receipts_are_shared_without_retry(self):
        for status in (429, 401, 403, 503):
            with self.subTest(status=status):
                usage.atomic_json(self.path, usage.empty_state())
                self.clock = NOW
                with self.shared() as ledger:
                    key = digest('failed')
                    ledger.reserve(key, IDENTITY)
                    ledger.issued(key)
                    with self.assertRaises(usage.UsageFailure) as caught:
                        ledger.http_failure(status, '900')
                    ledger.finish(key, caught.exception.reason)
                    self.assertEqual(caught.exception.facts()['httpStatus'], status)
                    self.assertEqual(ledger.used, 1)
                with self.shared('personal') as ledger:
                    if status in (401, 403, 429):
                        with self.assertRaises(usage.UsageFailure):
                            ledger.reserve(digest('other-component'), IDENTITY)
                        self.assertEqual(ledger.used, 0)
                    else:
                        self.completed(ledger, 'other-component')
                with self.shared(run_id='another') as ledger:
                    with self.assertRaises(usage.UsageFailure) as repeated:
                        ledger.reserve(key, IDENTITY)
                    self.assertEqual(repeated.exception.reason, caught.exception.reason)
                    self.assertEqual(ledger.used, 0)
                if status == 429:
                    self.assertEqual(usage.load_state(self.path)['retryAt'], '2026-09-07T00:15:00Z')
                    self.assertEqual(self.sleeps, [])
                self.sleeps.clear()

    def test_retry_after_is_at_least_five_minutes_and_accepts_http_date(self):
        for retry, seconds in ((None, 300), ('1', 300), ('garbage', 300),
                               ('Mon, 07 Sep 2026 00:20:00 GMT', 1200)):
            with self.subTest(retry=retry):
                usage.atomic_json(self.path, usage.empty_state())
                with self.shared() as ledger:
                    with self.assertRaises(usage.UsageFailure) as caught:
                        ledger.http_failure(429, retry)
                    self.assertEqual(usage._time(caught.exception.retry_at),
                                     self.clock + dt.timedelta(seconds=seconds))

    def test_unknown_fields_identity_secrets_and_duplicate_json_fail_closed(self):
        with self.shared() as ledger:
            self.completed(ledger, 'safe')
        original = usage.load_state(self.path)
        receipt_id = next(iter(original['receipts']))
        for field in ('body', 'bodyLines', 'lines', 'apiKey', 'token', 'url'):
            for target in ('root', 'receipt', 'identity'):
                invalid = copy.deepcopy(original)
                obj = invalid if target == 'root' else invalid['receipts'][receipt_id]
                if target == 'identity':
                    obj = obj['identity']
                obj[field] = 'PRIVATE_SENTINEL'
                with self.subTest(field=field, target=target), self.assertRaises(ValueError):
                    usage.validate_state(invalid)
        self.assertNotIn('offline.openai.azure.com', self.path.read_text())
        self.assertNotIn('PRIVATE_SENTINEL', self.path.read_text())
        with self.shared() as ledger:
            with self.assertRaises(ValueError):
                ledger.reserve(digest('unsafe'), {**IDENTITY, 'apiKey': 'PRIVATE_SENTINEL'})
            self.assertEqual(len(ledger.state['receipts']), 1)
        self.path.write_text('{"schemaVersion":1,"schemaVersion":1}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'invalid_ai_usage'):
            usage.load_state(self.path)

    def test_saved_safety_markers_cannot_disagree_with_receipts(self):
        with self.shared() as ledger:
            self.completed(ledger, 'safe')
        original = usage.load_state(self.path)
        for next_at in (None, '2026-09-07T00:00:00Z'):
            invalid = copy.deepcopy(original)
            invalid['nextRequestAt'] = next_at
            with self.assertRaisesRegex(ValueError, 'invalid_ai_usage'):
                usage.validate_state(invalid)
        for status, field in ((401, 'paused'), (429, 'retryAt')):
            usage.atomic_json(self.path, usage.empty_state())
            with self.shared() as ledger:
                key = digest('failure')
                ledger.reserve(key, IDENTITY)
                ledger.issued(key)
                with self.assertRaises(usage.UsageFailure) as caught:
                    ledger.http_failure(status, None)
                ledger.finish(key, caught.exception.reason)
            invalid = usage.load_state(self.path)
            invalid[field] = None
            with self.assertRaisesRegex(ValueError, 'invalid_ai_usage'):
                usage.validate_state(invalid)

    def test_atomic_replace_failure_preserves_previous_file(self):
        original = self.path.read_bytes()
        changed = usage.load_state(self.path)
        usage.apply_import(changed, historical())
        with mock.patch.object(usage.os, 'replace', side_effect=OSError('offline failure')):
            with self.assertRaises(OSError):
                usage.atomic_json(self.path, changed)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.folder.glob('*.partial')), [])

    def test_cross_process_lock_rejects_concurrent_writer_and_releases_after_exit(self):
        code = """
import importlib.util, sys, datetime as dt
spec=importlib.util.spec_from_file_location('child_usage', sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
try:
    with m.SharedUsage(sys.argv[2],run_id='child',component='personal',
                       clock=lambda:dt.datetime.now(dt.timezone.utc),sleep=lambda _:None):
        print('opened')
except m.UsageFailure as exc:
    print(exc.reason)
"""
        with self.shared():
            process = self.child(code)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout.strip(), 'azure_usage_locked')
        process = self.child(code)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), 'opened')

    def test_crashed_child_reservation_is_spent_and_cannot_be_issued_again(self):
        code = """
import importlib.util, sys, datetime as dt, os, hashlib
spec=importlib.util.spec_from_file_location('child_usage', sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with m.SharedUsage(sys.argv[2],run_id='crashed-run',component='official',
                   clock=lambda:dt.datetime(2026,9,7,tzinfo=dt.timezone.utc),sleep=lambda _:None) as ledger:
    ledger.reserve(hashlib.sha256(b'crash').hexdigest(), {
        'provider':'azure_openai','endpoint':'https://offline.openai.azure.com',
        'deployment':'gpt-5.6-luna','model':'gpt-5.6-luna','modelVersion':'2026-07-09'})
    os._exit(7)
"""
        process = self.child(code)
        self.assertEqual(process.returncode, 7, process.stderr)
        with self.shared(run_id='crashed-run') as ledger:
            self.assertEqual(ledger.used, 1)
            for method in ('reserve', 'issued'):
                with self.assertRaisesRegex(usage.UsageFailure, 'azure_interrupted'):
                    getattr(ledger, method)(digest('crash'), *([IDENTITY] if method == 'reserve' else []))
            self.assertEqual(len(ledger.state['receipts']), 1)
            self.completed(ledger, 'other')
            self.assertEqual(ledger.used, 2)


if __name__ == '__main__':
    unittest.main()
