"""Retention never re-enables a paid request and never changes the monthly accounting."""
import copy
import datetime as dt
import importlib.util
from pathlib import Path
import unittest

import test_ai_budget as budget


ledger = budget.ledger
money = budget.money
base = budget.base
SPEC = importlib.util.spec_from_file_location('retention_under_test', base.TOOLS / 'state-retention.py')
retention = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retention)
SOURCE_SPEC = importlib.util.spec_from_file_location('retention_source_under_test', base.TOOLS / 'source-state.py')
source = importlib.util.module_from_spec(SOURCE_SPEC)
SOURCE_SPEC.loader.exec_module(source)
OCTOBER = dt.datetime(2026, 10, 10, 2, tzinfo=dt.timezone.utc)
DECEMBER = dt.datetime(2026, 12, 2, 2, tzinfo=dt.timezone.utc)


class AiRetentionTests(unittest.TestCase):
    setUp = budget.BudgetTests.setUp
    sleep = budget.BudgetTests.sleep
    shared = budget.BudgetTests.shared
    finite = budget.BudgetTests.finite
    client = budget.BudgetTests.client
    envelope = budget.BudgetTests.envelope
    reply = budget.BudgetTests.reply
    issue = budget.BudgetTests.issue

    def settled(self, usage, name, envelope=None):
        key = self.issue(usage, name)
        self.reply(envelope or self.envelope())
        self.client(usage).structured(self.messages, {}, **self.arguments)
        usage.finish(key, 'no_event')
        return key

    def test_closed_month_receipts_compact_but_still_reject_a_second_issue(self):
        self.now = OCTOBER
        missing = self.envelope()
        del missing['usage']
        with self.finite(component='personal') as usage:
            paid = self.settled(usage, 'paid')
            held = self.settled(usage, 'held', missing)
        state = ledger.load_state(self.path)
        held_id = ledger._receipt_id('personal', held)
        self.assertEqual(state['receipts'][held_id]['money']['usageStatus'], 'usage_missing')
        charged = state['receipts'][ledger._receipt_id('personal', paid)]['money']['chargedMicroJPY']
        self.now = DECEMBER
        before = money.balance(state, self.now)
        hold = copy.deepcopy(state['receipts'][held_id])
        self.assertEqual(retention.prune_ai(state, self.now), 1)
        self.assertEqual(state['retired'], {'2026-10': {
            'requests': 1, 'chargedMicroJPY': charged, 'components': {'personal': 1},
            'receiptIds': [ledger._receipt_id('personal', paid)[:16]]}})
        self.assertEqual(state['receipts'], {held_id: hold})
        self.assertEqual(money.balance(state, self.now), before)
        ledger.atomic_json(self.path, state)
        with self.finite(component='personal') as usage:
            snapshot = copy.deepcopy(usage.state)
            with self.assertRaisesRegex(ledger.UsageFailure, 'azure_already_analyzed'):
                usage.reserve(paid, base.IDENTITY, request=self.request)
            self.assertEqual(usage.state, snapshot)
        self.assertEqual(self.opener.open.call_count, 2)
        # A live receipt can never coexist with its retired prefix.
        broken = ledger.load_state(self.path)
        broken['receipts'][ledger._receipt_id('personal', paid)] = copy.deepcopy(hold)
        with self.assertRaisesRegex(ValueError, 'invalid_ai_usage'):
            ledger.validate_state(broken)

    def test_current_month_boundary_day_and_referenced_receipts_stay(self):
        self.now = dt.datetime(2026, 11, 30, 14, tzinfo=dt.timezone.utc)  # 2026-11-30 23:00 JST
        with self.finite(component='schedule') as usage:
            boundary = self.settled(usage, 'boundary')
        state = ledger.load_state(self.path)
        self.now = dt.datetime(2026, 12, 1, 2, tzinfo=dt.timezone.utc)
        self.assertEqual(retention.prune_ai(state, self.now), 0)
        self.now = dt.datetime(2027, 1, 2, 2, tzinfo=dt.timezone.utc)
        referenced = ledger._receipt_id('schedule', boundary)
        self.assertEqual(retention.prune_ai(state, self.now, keep={referenced}), 0)
        self.assertEqual(retention.prune_ai(state, self.now), 1)


class SourceRetentionTests(unittest.TestCase):
    def test_old_receipts_keep_personal_budgets_and_stop_evidence(self):
        baseline = {'budgets': {}, 'paused': None, 'lastRequests': {}}
        state = source.baseline_state(baseline, source_hash='a' * 64, at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
        def receipt(run, kind, host, day, status='ok', http=200, component='personal'):
            at = f'{day}T01:00:00Z'
            request = base.digest(run + kind)
            state['receipts'][source._digest(run + ':' + kind + ':' + request)] = {
                'runId': run, 'component': component, 'kind': kind, 'requestHash': request, 'host': host,
                'date': day, 'reservedAt': at, 'issuedAt': at, 'completedAt': at, 'status': status,
                'httpStatus': http, 'contentHash': None}
            state['nextRequests'][host] = f'{day}T02:00:00Z'
        receipt('r1', 'searches', 'search.yahoo.co.jp', '2026-10-01')
        receipt('r2', 'posts', 'cdn.syndication.twimg.com', '2026-10-01')
        receipt('r3', 'images', 'pbs.twimg.com', '2026-10-01', 'failed', 403, 'schedule')
        receipt('r4', 'posts', 'cdn.syndication.twimg.com', '2026-11-20')
        state['cooldowns']['pbs.twimg.com'] = '2026-10-01T02:00:00Z'
        source.validate_state(state)
        budgets = source.legacy_budgets(state)
        pauses = source.host_pauses(state)
        count, _ = retention.prune_source(state, dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc))
        self.assertEqual(count, 2)
        self.assertEqual(state['retired'], {'2026-10-01': {'personal': {'searches': 1, 'posts': 1}}})
        self.assertEqual(source.legacy_budgets(state), budgets)
        self.assertEqual(source.host_pauses(state), pauses)
        self.assertEqual({item['runId'] for item in state['receipts'].values()}, {'r3', 'r4'})


if __name__ == '__main__':
    unittest.main()
