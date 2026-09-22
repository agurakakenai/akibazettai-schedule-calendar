import copy
import datetime as dt
import unittest

import test_ai_budget as fixture


money, ledger = fixture.money, fixture.ledger


class MonthlyOverrideTests(unittest.TestCase):
    setUp = fixture.BudgetTests.setUp
    sleep = fixture.BudgetTests.sleep
    shared = fixture.BudgetTests.shared
    opening = fixture.BudgetTests.opening
    reply = fixture.BudgetTests.reply
    envelope = fixture.BudgetTests.envelope
    client = fixture.BudgetTests.client
    issue = fixture.BudgetTests.issue

    def test_approval_and_month_boundaries_use_jst_without_carryover(self):
        for value, expected in (
            ('2026-09-22T23:47:59.999999+00:00', 1_000_000_000),
            ('2026-09-22T23:48:00+00:00', 1_500_000_000),
            ('2026-09-30T14:59:59.999999+00:00', 1_500_000_000),
            ('2026-09-30T15:00:00+00:00', 1_000_000_000),
            ('2026-10-31T15:00:00+00:00', 1_000_000_000),
        ):
            with self.subTest(value=value):
                self.assertEqual(money.effective_limit(dt.datetime.fromisoformat(value)), expected)
        self.assertEqual(money.empty()['limitMicroJPY'], 1_000_000_000)

    def test_extra_five_hundred_preserves_all_receipts_and_707_yen_hold(self):
        self.now = dt.datetime(2026, 9, 23, 9, tzinfo=money.JST)
        self.request = {'payloadHash': 'a' * 64, 'inputCeiling': 922_000,
                        'outputCeiling': 3840, 'imageInput': True}
        with self.shared() as usage:
            for key in ('unsettled-one', 'unsettled-two'):
                issued = self.issue(usage, key)
                usage.finish(issued, 'azure_timeout')
            before = copy.deepcopy(usage.state)
            previous = money.balance(usage.state, dt.datetime(2026, 9, 23, 8, 47, tzinfo=money.JST))
            current = money.balance(usage.state, self.now)
            self.assertEqual(current['reservedMicroJPY'], 707_289_032)
            self.assertEqual(current['availableMicroJPY'] - previous['availableMicroJPY'], 500_000_000)
            self.assertEqual(current['unsettledUsageRecords'], 2)
            for field in ('reconciledMicroJPY', 'tokenPricedMicroJPY', 'reservedMicroJPY',
                          'unknownRecords', 'inconsistentUsageRecords'):
                self.assertEqual(previous[field], current[field])
            october = money.balance(usage.state, dt.datetime(2026, 10, 1, tzinfo=money.JST))
            self.assertEqual(october['limitMicroJPY'], 1_000_000_000)
            self.assertEqual(october['availableMicroJPY'], 1_000_000_000)
            self.assertEqual(usage.state, before)
            money.validate(usage.state)

    def test_september_exact_1500_admission_settlement_and_one_micro_rejection(self):
        self.now = dt.datetime(2026, 9, 23, 9, tzinfo=money.JST)
        reservation = money.reservation(fixture.base.IDENTITY, self.request)['reservedMicroJPY']
        for excess in (0, 1):
            with self.subTest(excess=excess):
                state = ledger.empty_state()
                state['money'] = money.empty()
                ledger.atomic_json(self.path, state)
                self.opening(1_500_000_000 - reservation + excess)
                self.opener.reset_mock()
                with self.shared() as usage:
                    if excess:
                        with self.assertRaisesRegex(ledger.UsageFailure, 'azure_budget_exhausted'):
                            self.issue(usage)
                        self.assertEqual(usage.state['receipts'], {})
                        self.opener.open.assert_not_called()
                    else:
                        self.reply()
                        key = self.issue(usage)
                        self.assertEqual(money.balance(usage.state, self.now)['availableMicroJPY'], 0)
                        self.assertEqual(self.client(usage).structured(
                            self.messages, {}, **self.arguments), {'ok': True})
                        usage.finish(key, 'no_event')
                        result = money.balance(usage.state, self.now)
                        self.assertEqual(result['limitMicroJPY'], 1_500_000_000)
                        self.assertEqual(result['reservedMicroJPY'], 0)
                        self.assertGreater(result['availableMicroJPY'], 0)
                        self.assertEqual(usage.state['money']['limitMicroJPY'], 1_000_000_000)


if __name__ == '__main__':
    unittest.main()
