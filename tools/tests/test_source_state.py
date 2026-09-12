"""Offline source accounting, migration and cross-process lock regressions."""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('test_source_usage', TOOLS / 'source-state.py')
source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source)
NOW = dt.datetime(2026, 9, 7, 0, tzinfo=dt.timezone.utc)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def url(kind, number=1):
    if kind == 'posts':
        return f'https://cdn.syndication.twimg.com/tweet-result?id={2000000000000000000 + number}&lang=ja&token=a'
    if kind == 'images':
        return f'https://pbs.twimg.com/media/synthetic{number}.jpg?name=small'
    return f'https://search.yahoo.co.jp/realtime/search?p=synthetic{number}'


def imported_source(label, *, day='2026-09-07', searches=2, posts=1):
    return {'receiptId': digest('source-import:' + label), 'date': day,
            'searches': searches, 'posts': posts, 'sourceHash': digest('source-evidence:' + label)}


def prepare_imports(personal, analysis, receipts):
    personal_after, analysis_after = copy.deepcopy(personal), copy.deepcopy(analysis)
    for receipt in receipts:
        source.usage.apply_source_import(analysis_after, receipt, personal_after)
    return personal_after, analysis_after


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TOOLS / 'tests' / ('source-test-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.folder))
        self.path, self.personal_path = self.folder / 'source-usage.json', self.folder / 'personal.json'
        self.clock, self.sleeps = NOW, []
        self.personal = {'budgets': {'2026-09-06': {'searches': 18, 'posts': 11}},
                         'paused': None, 'lastRequests': {}}
        self.initialize()

    def initialize(self, **kwargs):
        if self.path.exists():
            self.path.unlink()
        source.initialize(self.path, self.personal, source_hash=digest('approved-canonical'),
                          at=self.clock, **kwargs)
        self.save_personal()

    def save_personal(self):
        self.personal_path.write_text(json.dumps(self.personal), encoding='utf-8')

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += dt.timedelta(seconds=seconds)

    def shared(self, component='schedule', run_id='run-1', **kwargs):
        return source.SharedSource(self.path, run_id=run_id, component=component,
                                   clock=lambda: self.clock, sleep=self.sleep, **kwargs)

    def reserve(self, ledger, kind, number=1, complete=True):
        key = ledger.reserve(kind, url(kind, number))
        if ledger.component == 'personal':
            self.personal['budgets'] = source.legacy_budgets(ledger.state)
            self.save_personal()
        if complete:
            ledger.issued(key)
            ledger.finish(key)
        return key

    def test_catchup_first_pass_fits_fourteen_searches_in_one_real_run(self):
        with self.shared('personal', catch_up=True, personal_path=self.personal_path) as ledger:
            for index in range(14):
                self.reserve(ledger, 'searches', index)
                self.reserve(ledger, 'posts', index)
            self.assertEqual(ledger.report()['run']['personal']['searches'], 14)
            self.assertEqual(ledger.report()['remaining']['posts'], 0)
        with self.shared('schedule', catch_up=True) as ledger:
            self.reserve(ledger, 'searches', 99)
            self.reserve(ledger, 'posts', 99)
            for index in range(4):
                self.reserve(ledger, 'images', index)
            self.assertEqual(ledger.report()['remaining']['searches'], 0)
        with self.shared('personal', run_id='next', catch_up=True, personal_path=self.personal_path) as ledger:
            for index in range(11):
                self.reserve(ledger, 'posts', 100 + index)
            with self.assertRaises(source.SourceFailure):
                ledger.check('posts')
            self.assertEqual(sum(ledger.report()['day'][name]['posts'] + ledger.report()['day'][name]['images']
                                 for name in ('personal', 'schedule')), 30)

    def canonical_import_baseline(self, **kwargs):
        analysis = source.usage.empty_state()
        old = imported_source('baseline', day='2026-09-06', searches=11, posts=9)
        analysis['sourceImports'][old['receiptId']] = {
            'receipt': old, 'budgetBefore': {'searches': 7, 'posts': 2},
            'budgetAfter': {'searches': 18, 'posts': 11}}
        self.initialize(source_imports=analysis['sourceImports'], **kwargs)
        return analysis

    def test_missing_ledger_never_creates_a_day_zero_state(self):
        self.path.unlink()
        self.assertIsNone(source.load_state(self.path))
        with self.assertRaisesRegex(ValueError, 'missing_source_usage'), self.shared():
            self.fail('must not initialize')
        self.assertFalse(self.path.exists())
        source.initialize(self.path, self.personal, source_hash=digest('approved'), at=NOW)
        original = self.path.read_bytes()
        source.initialize(self.path, self.personal, source_hash=digest('approved'), at=NOW)
        self.assertEqual(self.path.read_bytes(), original)
        with self.shared() as ledger:
            self.reserve(ledger, 'images')
        after_request = self.path.read_bytes()
        source.initialize(self.path, self.personal, source_hash=digest('approved'), at=NOW)
        self.assertEqual(self.path.read_bytes(), after_request)
        with self.assertRaisesRegex(ValueError, 'already_initialized'):
            source.initialize(self.path, self.personal, source_hash=digest('different'), at=NOW)
        self.assertEqual(self.path.read_bytes(), after_request)

    def test_explicit_baseline_preserves_imports_pause_and_old_image_without_new_get(self):
        imported = {'receiptId': digest('old-import'), 'date': '2026-09-06',
                    'searches': 11, 'posts': 9, 'sourceHash': digest('old-source')}
        record = {'receipt': imported, 'budgetBefore': {'searches': 7, 'posts': 2},
                  'budgetAfter': {'searches': 18, 'posts': 11}}
        analysis = source.usage.empty_state()
        analysis['sourceImports'][imported['receiptId']] = record
        pause = {'reason': 'access_denied', 'host': source.HOSTS[0], 'at': '2026-09-06T10:00:00Z',
                 'retryAt': '2026-09-06T12:00:00Z', 'httpStatus': 403}
        self.personal['paused'] = pause
        original = copy.deepcopy((self.personal, analysis))
        old_image = {'receiptId': digest('old-image'), 'date': '2026-09-06',
                     'images': 1, 'sourceHash': digest('private-evidence')}
        self.initialize(source_imports=analysis['sourceImports'], historical_images=[old_image],
                        cooldowns={source.HOSTS[0]: '2026-09-06T13:00:00Z'})
        state = source.load_state(self.path, required=True)
        source.validate_legacy(state, self.personal, analysis)
        self.assertEqual((self.personal, analysis), original)
        self.assertEqual(state['receipts'], {})
        self.assertEqual(state['baseline']['sourceImports'], analysis['sourceImports'])
        report = source.usage_counts(state, 'run', NOW - dt.timedelta(days=1))
        self.assertEqual(report['day']['personal'], {'searches': 18, 'posts': 11, 'images': 0})
        self.assertEqual(report['historicalImages'], 1)
        self.assertEqual(report['remaining']['images'], 4)
        with self.shared() as ledger:
            with self.assertRaisesRegex(source.SourceFailure, 'source_paused'):
                ledger.check('posts')
        with self.shared('official') as ledger:
            ledger.check('posts')
        self.assertEqual(state['cooldowns'][source.HOSTS[0]], '2026-09-06T13:00:00Z')

    def test_baseline_import_mismatch_never_adds_or_repairs_legacy_counts(self):
        imported = {'receiptId': digest('old-import'), 'date': '2026-09-06',
                    'searches': 11, 'posts': 9, 'sourceHash': digest('old-source')}
        records = {imported['receiptId']: {'receipt': imported,
                   'budgetBefore': {'searches': 18, 'posts': 11},
                   'budgetAfter': {'searches': 29, 'posts': 20}}}
        with self.assertRaises(ValueError):
            source.baseline_state(self.personal, source_hash=digest('old'), at=NOW, source_imports=records)
        state = source.load_state(self.path)
        analysis = source.usage.empty_state()
        analysis['sourceImports'] = records
        with self.assertRaisesRegex(ValueError, 'source_usage_budget_mismatch'):
            source.validate_legacy(state, self.personal, analysis)
        self.assertEqual(self.personal['budgets']['2026-09-06'], {'searches': 18, 'posts': 11})

    def test_approved_imports_append_exact_records_without_changing_baseline_native_or_safety(self):
        image = {'receiptId': digest('past-image'), 'date': '2026-09-06',
                 'images': 1, 'sourceHash': digest('past-image-proof')}
        analysis = self.canonical_import_baseline(historical_images=[image])
        with self.shared('official') as ledger:
            self.reserve(ledger, 'posts')
        with self.shared('personal', personal_path=self.personal_path) as ledger:
            self.reserve(ledger, 'posts', 2)
        with self.shared() as ledger:
            self.reserve(ledger, 'images')
            pause = {'reason': 'access_denied', 'host': 'pbs.twimg.com',
                     'at': source.usage._stamp(self.clock),
                     'retryAt': source.usage._stamp(self.clock + dt.timedelta(hours=2)), 'httpStatus': 403}
            ledger.set_cooldown('pbs.twimg.com', self.clock + dt.timedelta(hours=2), paused=pause)
        self.personal['paused'] = pause
        self.save_personal()
        state = source.load_state(self.path)
        receipts = [imported_source('old-day', day='2026-09-06'), imported_source('today')]
        after_personal, after_analysis = prepare_imports(self.personal, analysis, receipts)
        before = copy.deepcopy((state, self.personal, analysis, receipts, after_personal, after_analysis))
        result = source.apply_source_imports(
            state, receipts, personal_before=self.personal, personal_after=after_personal,
            analysis_before=analysis, analysis_after=after_analysis)
        self.assertEqual((state, self.personal, analysis, receipts, after_personal, after_analysis), before)
        self.assertIsNot(result, state)
        self.assertEqual({key: value for key, value in result.items() if key != 'sourceImports'}, state)
        self.assertEqual(result['sourceImports'], {
            receipt['receiptId']: after_analysis['sourceImports'][receipt['receiptId']] for receipt in receipts})
        self.assertEqual(len(result['baseline']['sourceImports']), 1)
        self.assertEqual(result['baseline']['personalBudgets']['2026-09-06'], {'searches': 18, 'posts': 11})
        self.assertEqual(source.legacy_budgets(result), after_personal['budgets'])
        counts = source.usage_counts(result, 'run-1', self.clock)
        self.assertEqual(counts['run'], source.usage_counts(state, 'run-1', self.clock)['run'])
        self.assertEqual(counts['issued'], source.usage_counts(state, 'run-1', self.clock)['issued'])
        self.assertEqual(counts['day']['personal'], {'searches': 2, 'posts': 2, 'images': 0})
        self.assertEqual(counts['day']['schedule']['images'], 1)
        old_day = source.usage_counts(result, 'different', NOW - dt.timedelta(days=1))
        self.assertEqual(old_day['day']['personal'], {'searches': 20, 'posts': 12, 'images': 0})
        self.assertEqual(old_day['historicalImages'], 1)
        source.atomic_json(self.path, result)
        source.validate_legacy(source.load_state(self.path), after_personal, after_analysis)
        result['sourceImports'][receipts[0]['receiptId']]['receipt']['searches'] = 999
        self.assertEqual(after_analysis['sourceImports'][receipts[0]['receiptId']]['receipt']['searches'], 2)

    def test_import_replay_and_initial_migration_replay_preserve_new_native_receipts(self):
        analysis = self.canonical_import_baseline()
        original_personal = copy.deepcopy(self.personal)
        state = source.load_state(self.path)
        receipt = imported_source('once')
        after_personal, after_analysis = prepare_imports(self.personal, analysis, [receipt])
        result = source.apply_source_imports(
            state, [receipt], personal_before=self.personal, personal_after=after_personal,
            analysis_before=analysis, analysis_after=after_analysis)
        self.personal = after_personal
        self.save_personal()
        source.atomic_json(self.path, result)
        with self.shared('personal', personal_path=self.personal_path) as ledger:
            self.reserve(ledger, 'posts', 7)
        current = source.load_state(self.path)
        replay_personal, replay_analysis = prepare_imports(self.personal, after_analysis, [receipt, receipt])
        replay = source.apply_source_imports(
            current, [receipt, receipt], personal_before=self.personal, personal_after=replay_personal,
            analysis_before=after_analysis, analysis_after=replay_analysis)
        self.assertEqual(replay, current)
        self.assertEqual(source.legacy_budgets(replay)['2026-09-07'], {'searches': 2, 'posts': 2})
        before = self.path.read_bytes()
        initialized = source.initialize(
            self.path, original_personal, source_hash=digest('approved-canonical'),
            at=NOW, source_imports=analysis['sourceImports'])
        self.assertEqual(initialized, current)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(replay['sourceImports']), 1)
        self.assertEqual(replay['baseline'], state['baseline'])

    def test_optional_import_field_keeps_legacy_schema_and_existing_receipt_replay_unchanged(self):
        analysis = self.canonical_import_baseline()
        state = source.load_state(self.path)
        old = next(iter(analysis['sourceImports'].values()))['receipt']
        for receipts in ([], [old]):
            with self.subTest(receipts=receipts):
                result = source.apply_source_imports(
                    state, receipts, personal_before=self.personal, personal_after=self.personal,
                    analysis_before=analysis, analysis_after=analysis)
                self.assertEqual(result, state)
                self.assertNotIn('sourceImports', result)
                self.assertIsNot(result, state)
        explicit_empty = {**copy.deepcopy(state), 'sourceImports': {}}
        source.validate_state(explicit_empty)
        source.validate_legacy(explicit_empty, self.personal, analysis)

    def test_same_phase_ai_usage_import_is_unchanged_and_not_reclassified_as_source(self):
        analysis = self.canonical_import_baseline()
        state = source.load_state(self.path)
        receipt = imported_source('same-phase')
        after_personal, after_analysis = prepare_imports(self.personal, analysis, [receipt])
        source.usage.apply_import(after_analysis, {
            'receiptId': digest('new-ai'), 'sourceHash': digest('new-ai-proof'),
            'date': '2026-09-07', 'counts': {'requests': 1},
            'modelBreakdown': [{'model': 'gpt-5.6-luna', 'kind': 'image',
                               'component': 'schedule', 'count': 1}]})
        before = copy.deepcopy(after_analysis)
        result = source.apply_source_imports(
            state, [receipt], personal_before=self.personal, personal_after=after_personal,
            analysis_before=analysis, analysis_after=after_analysis)
        self.assertEqual(after_analysis, before)
        self.assertEqual(len(result['sourceImports']), 1)
        self.assertEqual(result['receipts'], {})
        self.assertEqual(source.usage_counts(result, 'run-1', NOW)['run']['schedule'],
                         {'searches': 0, 'posts': 0, 'images': 0})
        self.assertEqual(source.usage.usage_counts(after_analysis, 'run-1', NOW)['day'], 1)

    def test_late_history_can_exhaust_day_without_retroactively_erasing_spent_native_requests(self):
        analysis = self.canonical_import_baseline()
        with self.shared('personal', personal_path=self.personal_path) as ledger:
            self.reserve(ledger, 'posts')
        with self.shared() as ledger:
            self.reserve(ledger, 'images')
        state = source.load_state(self.path)
        receipt = imported_source('late-full-day', searches=60, posts=30)
        after_personal, after_analysis = prepare_imports(self.personal, analysis, [receipt])
        result = source.apply_source_imports(
            state, [receipt], personal_before=self.personal, personal_after=after_personal,
            analysis_before=analysis, analysis_after=after_analysis)
        source.validate_state(result)
        self.assertEqual(result['receipts'], state['receipts'])
        self.assertEqual(source.usage_counts(result, 'run-1', NOW)['day']['personal'],
                         {'searches': 60, 'posts': 31, 'images': 0})
        source.atomic_json(self.path, result)
        self.personal = after_personal
        self.save_personal()
        for component in ('personal', 'schedule'):
            with self.shared(component, run_id='different', personal_path=self.personal_path) as ledger:
                for kind in ('searches', 'posts'):
                    with self.assertRaisesRegex(source.SourceFailure, 'source_budget_exhausted'):
                        ledger.check(kind)
        with self.shared('official', run_id='different', personal_path=self.personal_path) as ledger:
            ledger.check('searches', 2)
            ledger.check('posts', 20)
        self.clock += dt.timedelta(days=1)
        with self.shared('schedule', run_id='tomorrow', personal_path=self.personal_path) as ledger:
            ledger.check('images', 4)
            self.assertEqual(ledger.report()['day']['personal'], {'searches': 0, 'posts': 0, 'images': 0})

    def test_reconciliation_rejects_unapproved_records_changed_proofs_and_unexplained_budgets(self):
        analysis = self.canonical_import_baseline()
        state = source.load_state(self.path)
        receipt = imported_source('approved')
        old_id = next(iter(analysis['sourceImports']))
        for case in ('unapproved', 'removed', 'changed-old', 'record-before', 'budget', 'before-budget'):
            after_personal, after_analysis = prepare_imports(self.personal, analysis, [receipt])
            before_personal = copy.deepcopy(self.personal)
            if case == 'unapproved':
                source.usage.apply_source_import(after_analysis, imported_source('not-approved'), after_personal)
            elif case == 'removed':
                del after_analysis['sourceImports'][old_id]
            elif case == 'changed-old':
                after_analysis['sourceImports'][old_id]['receipt']['sourceHash'] = digest('changed-old-proof')
            elif case == 'record-before':
                record = after_analysis['sourceImports'][receipt['receiptId']]
                record['budgetBefore']['posts'] += 1
                record['budgetAfter']['posts'] += 1
            elif case == 'budget':
                after_personal['budgets']['2026-09-07']['posts'] += 1
            else:
                before_personal['budgets']['2026-09-06']['posts'] += 1
            original = copy.deepcopy((state, before_personal, after_personal, analysis, after_analysis))
            with self.subTest(case=case), self.assertRaisesRegex(
                    ValueError, 'source_usage_import_mismatch|source_usage_budget_mismatch'):
                source.apply_source_imports(
                    state, [receipt], personal_before=before_personal, personal_after=after_personal,
                    analysis_before=analysis, analysis_after=after_analysis)
            self.assertEqual((state, before_personal, after_personal, analysis, after_analysis), original)

    def test_reconciliation_rejects_replacement_duplicate_provenance_images_and_raw(self):
        analysis = self.canonical_import_baseline()
        state = source.load_state(self.path)
        old = next(iter(analysis['sourceImports'].values()))['receipt']
        cases = [
            {**old, 'posts': old['posts'] + 1},
            {**old, 'receiptId': digest('same-proof-another-id')},
            {**imported_source('image-disguise'), 'images': 1},
            {**imported_source('raw'), 'raw': 'PRIVATE_RAW_SENTINEL'},
            {**imported_source('boolean'), 'posts': True},
        ]
        for receipt in cases:
            original = copy.deepcopy((state, self.personal, analysis, receipt))
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                source.apply_source_imports(
                    state, [receipt], personal_before=self.personal, personal_after=self.personal,
                    analysis_before=analysis, analysis_after=analysis)
            self.assertEqual((state, self.personal, analysis, receipt), original)

    def test_appended_import_schema_rejects_overlap_duplicate_provenance_and_counter_tampering(self):
        analysis = self.canonical_import_baseline()
        state = source.load_state(self.path)
        receipt = imported_source('appended', day='2026-09-06')
        after_personal, after_analysis = prepare_imports(self.personal, analysis, [receipt])
        result = source.apply_source_imports(
            state, [receipt], personal_before=self.personal, personal_after=after_personal,
            analysis_before=analysis, analysis_after=after_analysis)
        old_id = next(iter(analysis['sourceImports']))
        for case in ('overlap', 'provenance', 'raw', 'budget-before', 'budget-after', 'not-map'):
            invalid = copy.deepcopy(result)
            if case == 'overlap':
                invalid['sourceImports'][old_id] = copy.deepcopy(analysis['sourceImports'][old_id])
            elif case == 'provenance':
                record = copy.deepcopy(analysis['sourceImports'][old_id])
                record['receipt']['receiptId'] = digest('duplicate-proof-id')
                invalid['sourceImports'][record['receipt']['receiptId']] = record
            elif case == 'raw':
                invalid['sourceImports'][receipt['receiptId']]['raw'] = 'PRIVATE_RAW_SENTINEL'
            elif case == 'not-map':
                invalid['sourceImports'] = []
            else:
                record = invalid['sourceImports'][receipt['receiptId']]
                delta = -1 if case == 'budget-before' else 1
                record['budgetBefore']['posts'] += delta
                record['budgetAfter']['posts'] += delta
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, 'invalid_source_usage'):
                source.validate_state(invalid)

    def test_searches_share_five_with_official_two_and_personal_schedule_three(self):
        with self.shared('official') as ledger:
            for index in range(2):
                self.reserve(ledger, 'searches', index)
            with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                self.reserve(ledger, 'searches', 3)
        with self.shared('personal', personal_path=self.personal_path) as ledger:
            for index in range(2):
                self.reserve(ledger, 'searches', index + 10)
        with self.shared() as ledger:
            self.reserve(ledger, 'searches', 20)
            report = ledger.report()
            self.assertEqual(sum(row['searches'] for row in report['run'].values()), 5)
            with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                self.reserve(ledger, 'searches', 21)
        with self.shared('personal', personal_path=self.personal_path) as ledger:
            with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                self.reserve(ledger, 'searches', 99)
        source.validate_legacy(source.load_state(self.path), self.personal)

    def test_all_individual_gets_twenty_official_not_artificially_reduced(self):
        with self.shared('official') as ledger:
            for index in range(20):
                self.reserve(ledger, 'posts', index)
            self.assertEqual(ledger.report()['remaining']['posts'], 0)
        with self.shared() as ledger:
            for kind in ('posts', 'images'):
                with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                    ledger.check(kind)
        with self.shared('personal') as ledger:
            with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                ledger.check('posts')

    def test_personal_three_schedule_one_post_four_images_and_shared_twenty(self):
        with self.shared('official') as ledger:
            for index in range(12):
                self.reserve(ledger, 'posts', index)
        with self.shared('personal', personal_path=self.personal_path) as ledger:
            for index in range(3):
                self.reserve(ledger, 'posts', index + 20)
            with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                ledger.check('posts')
        with self.shared() as ledger:
            self.reserve(ledger, 'posts', 30)
            ledger.check('images', count=4)
            for index in range(4):
                self.reserve(ledger, 'images', index)
            self.assertEqual(ledger.report()['remaining']['images'], 0)
            self.assertEqual(ledger.report()['run']['schedule'], {'searches': 0, 'posts': 1, 'images': 4})
            with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                ledger.check('posts')
        self.assertEqual(len(source.load_state(self.path)['receipts']), 20)

    def test_personal_schedule_day_sixty_thirty_does_not_cap_official(self):
        self.personal['budgets']['2026-09-07'] = {'searches': 59, 'posts': 28}
        self.initialize()
        with self.shared() as ledger:
            self.reserve(ledger, 'searches')
            self.reserve(ledger, 'posts')
            self.reserve(ledger, 'images')
        with self.shared('personal', run_id='different') as ledger:
            for kind in ('searches', 'posts'):
                with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                    ledger.check(kind)
        with self.shared('official', run_id='different') as ledger:
            self.reserve(ledger, 'searches', 99)
            self.reserve(ledger, 'posts', 99)
        self.assertEqual(source.load_state(self.path)['baseline']['personalBudgets'], self.personal['budgets'])

    def test_official_can_exceed_thirty_daily_posts_across_bounded_runs(self):
        for run in ('first', 'second'):
            with self.shared('official', run_id=run) as ledger:
                for index in range(20):
                    self.reserve(ledger, 'posts', index)
        state = source.load_state(self.path)
        source.validate_state(state)
        report = source.usage_counts(state, 'third', self.clock, 'official')
        self.assertEqual(report['day']['official']['posts'], 40)
        self.assertEqual(report['remaining']['posts'], 20)

    def test_eight_images_actual_jst_day_and_fail_closed_midnight_issue(self):
        for run in ('first', 'second'):
            with self.shared(run_id=run) as ledger:
                for index in range(4):
                    self.reserve(ledger, 'images', index)
        with self.shared(run_id='third') as ledger:
            with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                ledger.check('images')
        self.clock = NOW.replace(hour=14, minute=59, second=59)
        with self.shared('official', run_id='midnight') as ledger:
            key = self.reserve(ledger, 'posts', complete=False)
            self.clock += dt.timedelta(seconds=2)
            with self.assertRaisesRegex(source.SourceFailure, 'source_interrupted'):
                ledger.issued(key)
        with self.shared(run_id='next-day') as ledger:
            self.reserve(ledger, 'images')
            self.assertEqual(ledger.report()['day']['schedule']['images'], 1)
            self.assertEqual(ledger.report()['date'], '2026-09-08')

    def test_spacing_rechecks_actual_day_after_sleep_and_uses_twelve_not_ai_sixty(self):
        self.personal['budgets']['2026-09-08'] = {'searches': 60, 'posts': 30}
        self.initialize()
        self.clock = NOW.replace(hour=14, minute=59, second=59)
        with self.shared('official') as ledger:
            self.reserve(ledger, 'searches')
        with self.shared() as ledger:
            with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                self.reserve(ledger, 'searches', 2)
        self.assertEqual(self.sleeps, [12])
        self.assertEqual(len(source.load_state(self.path)['receipts']), 1)

    def test_same_run_id_and_media_variants_are_not_requested_twice_after_restart(self):
        with self.shared('personal', personal_path=self.personal_path) as ledger:
            self.reserve(ledger, 'posts', 1, complete=False)
        with self.shared() as ledger:
            with self.assertRaisesRegex(source.SourceFailure, 'already_requested'):
                ledger.reserve('posts', 'https://x.com/other/status/2000000000000000001')
            self.reserve(ledger, 'images')
        with self.shared() as ledger:
            with self.assertRaisesRegex(source.SourceFailure, 'already_requested'):
                ledger.reserve('images', 'https://pbs.twimg.com/media/synthetic1?format=jpg&name=large')
        state = source.load_state(self.path)
        self.assertEqual(len(state['receipts']), 2)
        self.assertNotIn('https:', self.path.read_text())
        self.assertEqual(source.usage_counts(state, 'run-1', self.clock)['issued']['personal']['posts'], 0)

    def test_legacy_reservation_write_gap_fails_closed_never_repairs_or_refunds(self):
        original = self.personal_path.read_bytes()
        with self.shared('personal', personal_path=self.personal_path) as ledger:
            ledger.reserve('posts', url('posts'))
            with self.assertRaisesRegex(ValueError, 'source_usage_budget_mismatch'):
                ledger.check('posts')
        with self.assertRaisesRegex(ValueError, 'source_usage_budget_mismatch'):
            with self.shared('schedule', personal_path=self.personal_path):
                self.fail('must not repair')
        self.assertEqual(self.personal_path.read_bytes(), original)
        self.assertEqual(len(source.load_state(self.path)['receipts']), 1)

    def test_failed_save_poisons_instance_and_no_issue_or_success_fallback(self):
        with self.shared() as ledger:
            with mock.patch.object(source.os, 'replace', side_effect=OSError('synthetic disk error')):
                with self.assertRaisesRegex(ValueError, 'source_usage_save_failed'):
                    ledger.reserve('images', url('images'))
            with self.assertRaisesRegex(source.SourceFailure, 'source_usage_save_failed'):
                ledger.check('images')
        self.assertEqual(source.load_state(self.path)['receipts'], {})
        self.assertEqual(list(self.folder.glob('*.partial')), [])

    def test_longest_image_host_cooldown_survives_children_without_get(self):
        later = NOW + dt.timedelta(hours=2)
        with self.shared() as ledger:
            ledger.set_cooldown('pbs.twimg.com', later)
            ledger.set_cooldown('pbs.twimg.com', NOW + dt.timedelta(minutes=5))
        with self.shared(run_id='next') as ledger:
            with self.assertRaisesRegex(source.SourceFailure, 'host_cooldown'):
                self.reserve(ledger, 'images')
            self.assertEqual(ledger.state['cooldowns']['pbs.twimg.com'], source.usage._stamp(later))
            self.assertEqual(ledger.state['receipts'], {})

    def test_preflight_is_pure_and_requires_full_image_capacity(self):
        with self.shared() as ledger:
            before = self.path.read_bytes()
            ledger.check('images', 4)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.sleeps, [])
            with self.assertRaisesRegex(source.SourceFailure, 'budget_exhausted'):
                ledger.check('images', 5)
            for count in (True, 0, -1, 1.5):
                with self.assertRaises(ValueError):
                    ledger.check('images', count)

    def test_opt_in_cache_is_shared_by_components_bounded_and_does_not_refetch_evictions(self):
        with source.TransientSourceCache('cached-run') as cache:
            with self.shared('official', run_id='cached-run', cache=cache) as ledger:
                for index in range(4):
                    receipt = self.reserve(ledger, 'posts', index)
                    ledger.remember(receipt, b'{"text":"synthetic raw ' + str(index).encode() + b'"}')
                self.assertEqual(cache.counts()['posts'], 3)
                self.assertIsNone(ledger.cached('posts', url('posts', 0)))
                with self.assertRaisesRegex(source.SourceFailure, 'source_already_requested'):
                    ledger.reserve('posts', url('posts', 0))
            before = self.path.read_bytes()
            with self.shared('schedule', run_id='cached-run', cache=cache) as ledger:
                cached = ledger.cached('posts', 'https://x.com/example/status/2000000000000000001')
                self.assertEqual(json.loads(cached['body']), {'text': 'synthetic raw 1'})
                self.assertIn('fetchedAt', cached)
                self.assertEqual(ledger.report()['run']['schedule']['posts'], 0)
                self.assertEqual(self.path.read_bytes(), before)
            self.assertNotIn('synthetic raw', self.path.read_text())
            retained = next(iter(cache._items['posts'].values()))['body']
        self.assertEqual(cache.counts()['posts'], 0)
        self.assertEqual(retained, bytes(len(retained)))

    def test_cache_is_opt_in_and_failed_or_other_run_receipts_cannot_supply_raw(self):
        with self.shared() as ledger:
            receipt = self.reserve(ledger, 'posts')
            self.assertFalse(ledger.remember(receipt, b'synthetic raw'))
            self.assertIsNone(ledger.cached('posts', url('posts')))
        with source.TransientSourceCache('next') as cache:
            with self.shared(run_id='next', cache=cache) as ledger:
                with self.assertRaisesRegex(source.SourceFailure, 'source_interrupted'):
                    ledger.remember(receipt, b'synthetic raw')
                pending = self.reserve(ledger, 'posts', complete=False)
                with self.assertRaisesRegex(source.SourceFailure, 'source_interrupted'):
                    ledger.remember(pending, b'synthetic raw')
            with self.assertRaisesRegex(ValueError, 'source_cache_run_mismatch'):
                self.shared(run_id='different', cache=cache)
            self.assertEqual(cache.counts()['posts'], 0)

    def test_image_cache_is_one_post_four_images_twelve_mib_and_clears_on_failure(self):
        with source.TransientSourceCache('images') as cache:
            post_id = '2000000000000000001'
            self.assertTrue(cache.put('images', digest('one'), b'a' * (8 * 1024 * 1024),
                                      content_type='image/jpeg', post_id=post_id))
            self.assertTrue(cache.put('images', digest('two'), b'b' * (4 * 1024 * 1024),
                                      content_type='image/png', post_id=post_id))
            self.assertFalse(cache.put('images', digest('three'), b'c', post_id=post_id))
            self.assertEqual(cache.counts()['imageBytes'], 12 * 1024 * 1024)
            previous = cache._items['images'][digest('one')]['body']
            self.assertTrue(cache.put('images', digest('other-post'), b'd',
                                      post_id='2000000000000000002'))
            self.assertEqual(previous, bytes(len(previous)))
            self.assertEqual(cache.counts()['images'], 1)
            for index in range(3):
                self.assertTrue(cache.put('images', digest(str(index)), b'one',
                                          post_id='2000000000000000002'))
            self.assertFalse(cache.put('images', digest('fifth'), b'one',
                                       post_id='2000000000000000002'))
            with self.assertRaisesRegex(RuntimeError, 'offline failure'):
                with self.shared(run_id='images', cache=cache):
                    raise RuntimeError('offline failure')
            self.assertEqual(cache.counts(), {'searches': 0, 'posts': 0, 'images': 0, 'imageBytes': 0})

    def test_unknown_fields_and_inconsistent_dates_are_rejected(self):
        with self.shared() as ledger:
            self.reserve(ledger, 'images')
        original = source.load_state(self.path)
        invalid = copy.deepcopy(original)
        invalid['raw'] = 'PRIVATE_SENTINEL'
        with self.assertRaisesRegex(ValueError, 'invalid_source_usage'):
            source.validate_state(invalid)
        invalid = copy.deepcopy(original)
        item = next(iter(invalid['receipts'].values()))
        item['date'] = '2026-09-06'
        with self.assertRaisesRegex(ValueError, 'invalid_source_usage'):
            source.validate_state(invalid)
        self.path.write_text('{"schemaVersion":1,"schemaVersion":1}', encoding='utf-8')
        with self.assertRaises(ValueError):
            source.load_state(self.path)

    def test_state_validation_detects_tampered_run_cap_and_pause_cooldown_rollback(self):
        with self.shared() as ledger:
            self.reserve(ledger, 'posts')
        invalid = source.load_state(self.path)
        record = copy.deepcopy(next(iter(invalid['receipts'].values())))
        record['requestHash'] = digest('different-post')
        key = digest(record['runId'] + ':' + record['kind'] + ':' + record['requestHash'])
        invalid['receipts'][key] = record
        with self.assertRaisesRegex(ValueError, 'invalid_source_usage'):
            source.validate_state(invalid)
        self.personal['paused'] = {
            'reason': 'access_denied', 'host': 'pbs.twimg.com', 'at': source.usage._stamp(NOW),
            'retryAt': source.usage._stamp(NOW + dt.timedelta(hours=2))}
        self.initialize()
        invalid = source.load_state(self.path)
        invalid['cooldowns']['pbs.twimg.com'] = source.usage._stamp(NOW)
        with self.assertRaisesRegex(ValueError, 'invalid_source_usage'):
            source.validate_state(invalid)

    def test_local_sequence_lock_is_real_and_cloud_lease_is_not_modified(self):
        code = (
            "import importlib.util,sys,datetime as dt\n"
            "s=importlib.util.spec_from_file_location('child',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
            "try:\n"
            " with m.SharedSource(sys.argv[2],run_id='other',component='official',clock=lambda:dt.datetime.now(dt.timezone.utc),sleep=lambda _:None):pass\n"
            "except m.SourceFailure as e:\n"
            " print(e.reason);sys.exit(8)\n")
        with self.shared():
            result = subprocess.run([sys.executable, '-c', code, str(TOOLS / 'source-state.py'), str(self.path)],
                                    capture_output=True, text=True, timeout=15, cwd=TOOLS.parent,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertEqual(result.stdout.strip(), 'source_usage_locked')
        self.assertEqual(set(path.name for path in self.folder.iterdir()),
                         {'source-usage.json', 'source-usage.json.lock', 'personal.json'})


if __name__ == '__main__':
    unittest.main()
