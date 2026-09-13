"""Offline monetary boundaries on the actual shared reservation/HTTP path."""
import copy
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_analysis_state as base
import test_azure_openai as wire


ledger = base.usage
money = ledger.costs
azure = wire.azure
NOW = dt.datetime(2026, 9, 13, 2, tzinfo=dt.timezone.utc)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'ai-usage.json'
        self.now = NOW
        self.state = ledger.empty_state()
        self.state['money'] = money.empty()
        ledger.atomic_json(self.path, self.state)
        self.messages = [{'role': 'user', 'content': 'saved source'}]
        self.arguments = {'name': 'offline', 'max_completion_tokens': 100}
        self.request = azure.request_budget(self.messages, {}, **self.arguments)
        self.opener = mock.Mock()
        self.no_network = mock.patch('urllib.request.OpenerDirector.open',
                                     side_effect=AssertionError('live HTTP forbidden'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def sleep(self, seconds):
        self.now += dt.timedelta(seconds=seconds)

    def shared(self, run='one', component='personal'):
        return ledger.SharedUsage(self.path, run_id=run, component=component,
                                  clock=lambda: self.now, sleep=self.sleep)

    def opening(self, amount, count=1):
        old = base.historical(date='2026-09-13', count=count)
        state = ledger.load_state(self.path)
        ledger.apply_import(state, old)
        state['money']['opening']['2026-09'] = {
            'amountMicroJPY': amount, 'basisHash': base.digest('approved-observation'),
            'records': {'import:' + old['receiptId']: money.digest(old)},
            'kind': 'provisional-azure-actual', 'throughDate': '2026-09-12',
            'observedAt': '2026-09-13T01:57:31Z'}
        ledger.atomic_json(self.path, state)

    def client(self, usage):
        return azure.AzureOpenAI(wire.ENV, on_http_failure=usage.http_failure,
                                 usage=usage, opener=self.opener)

    def envelope(self, prompt=200, cached=80, written=40, output=20):
        return {'model': 'gpt-5.6-luna-2026-07-09',
                'usage': {'prompt_tokens': prompt, 'completion_tokens': output,
                          'total_tokens': prompt + output,
                          'prompt_tokens_details': {'cached_tokens': cached, 'cache_write_tokens': written}},
                'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}

    def reply(self, envelope=None):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = 200
        response.read.return_value = json.dumps(envelope or self.envelope()).encode()
        self.opener.open.return_value = response

    def issue(self, usage, key='request'):
        key = base.digest(key)
        usage.reserve(key, base.IDENTITY, request=self.request)
        usage.issued(key)
        return key

    def test_exact_1000_allowed_one_micro_over_stops_before_http(self):
        reservation = money.reservation(base.IDENTITY, self.request)['reservedMicroJPY']
        for excess in (0, 1):
            with self.subTest(excess=excess):
                state = ledger.empty_state()
                state['money'] = money.empty()
                ledger.atomic_json(self.path, state)
                self.opening(money.LIMIT - reservation + excess)
                self.opener.reset_mock()
                with self.shared() as usage:
                    if excess:
                        with self.assertRaisesRegex(ledger.UsageFailure, 'azure_budget_exhausted'):
                            self.issue(usage)
                        self.opener.open.assert_not_called()
                        self.assertEqual(usage.state['receipts'], {})
                    else:
                        self.reply()
                        key = self.issue(usage)
                        self.assertEqual(money.balance(usage.state, self.now)['availableMicroJPY'], 0)
                        self.assertEqual(self.client(usage).structured(self.messages, {}, **self.arguments), {'ok': True})
                        usage.finish(key, 'no_event')
                        self.opener.open.assert_called_once()

    def test_final_gate_rejects_changed_payload_and_second_http(self):
        with self.shared() as usage:
            self.issue(usage)
            with self.assertRaisesRegex(azure.AzureFailure, 'azure_interrupted'):
                self.client(usage).structured([{'role': 'user', 'content': 'different'}], {}, **self.arguments)
            self.opener.open.assert_not_called()
            self.reply()
            client = self.client(usage)
            client.structured(self.messages, {}, **self.arguments)
            with self.assertRaisesRegex(azure.AzureFailure, 'azure_interrupted'):
                client.structured(self.messages, {}, **self.arguments)
            self.opener.open.assert_called_once()

    def test_final_http_gate_observes_budget_changes_and_unknown_prices(self):
        reservation = money.reservation(base.IDENTITY, self.request)['reservedMicroJPY']
        self.opening(money.LIMIT - reservation)
        with self.shared() as usage:
            self.issue(usage)
            usage.state['money']['opening']['2026-09']['amountMicroJPY'] += 1
            usage._save()
            with self.assertRaisesRegex(azure.AzureFailure, 'azure_budget_exhausted'):
                self.client(usage).structured(self.messages, {}, **self.arguments)
            self.opener.open.assert_not_called()
        self.setUp_ledger()
        with self.shared() as usage:
            with self.assertRaisesRegex(ledger.UsageFailure, 'azure_input_limit'):
                usage.reserve(base.digest('unknown-model'), {**base.IDENTITY, 'modelVersion': 'unknown'},
                              request=self.request)
            self.assertEqual(usage.state['receipts'], {})

    def test_unknown_usage_timeout_and_refusal_keep_or_settle_real_cost(self):
        for case in ('timeout', 'usage-missing', 'refusal'):
            with self.subTest(case=case):
                self.setUp_ledger()
                self.opener.reset_mock()
                self.opener.open.side_effect = None
                with self.shared() as usage:
                    key = self.issue(usage)
                    original = money.balance(usage.state, self.now)['reservedMicroJPY']
                    envelope = self.envelope()
                    if case == 'timeout':
                        self.opener.open.side_effect = TimeoutError()
                    elif case == 'usage-missing':
                        del envelope['usage']['prompt_tokens_details']['cache_write_tokens']
                    else:
                        envelope['choices'][0]['message']['refusal'] = 'refused'
                    self.reply(envelope)
                    if case == 'usage-missing':
                        self.assertEqual(self.client(usage).structured(self.messages, {}, **self.arguments), {'ok': True})
                        usage.finish(key, 'no_event')
                    else:
                        with self.assertRaises(azure.AzureFailure) as caught:
                            self.client(usage).structured(self.messages, {}, **self.arguments)
                        usage.finish(key, caught.exception.reason)
                    value = next(iter(usage.state['receipts'].values()))['money']
                    if case == 'refusal':
                        self.assertIsNotNone(value['chargedMicroJPY'])
                    else:
                        self.assertIsNone(value['chargedMicroJPY'])
                        self.assertEqual(value['reservedMicroJPY'], original)
                        if case == 'usage-missing':
                            self.assertEqual(value['usageStatus'], 'usage_missing')
                            self.assertEqual(money.balance(usage.state, self.now)['unsettledUsageRecords'], 1)
                self.assertEqual(len(ledger.load_state(self.path)['receipts']), 1)

    def setUp_ledger(self):
        state = ledger.empty_state()
        state['money'] = money.empty()
        ledger.atomic_json(self.path, state)

    def test_images_use_model_input_max_text_uses_wire_and_actual_output_cap(self):
        self.assertLess(self.request['inputCeiling'], 2000)
        image = azure.request_budget([{'role': 'user', 'content': [
            {'type': 'text', 'text': 'saved schedule'},
            *[{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}} for _ in range(4)]
        ]}], {}, name='half_month_schedule', max_completion_tokens=3840)
        self.assertEqual(image['inputCeiling'], 922000)
        self.assertEqual(image['outputCeiling'], 3840)
        with self.shared(component='schedule') as usage:
            usage.reserve(base.digest('images'), base.IDENTITY, request=image)
            actual = next(iter(usage.state['receipts'].values()))['money']
            self.assertEqual(actual['reservedMicroJPY'],
                             money.ceiling('gpt-5.6-luna', '2026-07-09', 922000, 3840))
            self.assertGreater(actual['reservedMicroJPY'], 100_000_000)
        with self.assertRaises(azure.AzureFailure):
            azure.request_budget([{'role': 'user', 'content': [{'type': 'input_audio'}]}], {}, **self.arguments)

    def test_up_to_four_cache_write_prefixes_are_reserved_without_assuming_disjoint_tokens(self):
        charge = money.reservation(base.IDENTITY, self.request)
        self.assertEqual(charge['reservedMicroJPY'], money.cost(
            charge['model'], charge['modelVersion'], charge['inputCeiling'], 0,
            charge['inputCeiling'] * 4, charge['outputCeiling']))
        settled = money.settle(charge, self.envelope(prompt=200, cached=80, written=800))
        self.assertEqual(settled['usage']['cachedWrite'], 800)
        self.assertLessEqual(settled['chargedMicroJPY'], settled['reservedMicroJPY'])
        with self.assertRaises(money.UsageInconsistent):
            money.settle(charge, self.envelope(prompt=200, cached=80, written=801))

    def test_usage_above_reservation_stops_further_issuance_even_if_optional_usage_is_missing(self):
        with self.shared() as usage:
            key = self.issue(usage)
            response = self.envelope(prompt=self.request['inputCeiling'] + 1)
            del response['usage']['prompt_tokens_details']['cache_write_tokens']
            self.reply(response)
            with self.assertRaisesRegex(azure.AzureFailure, 'azure_invalid_output'):
                self.client(usage).structured(self.messages, {}, **self.arguments)
            usage.finish(key, 'azure_invalid_output')
            self.assertEqual(money.balance(usage.state, self.now)['reason'], 'monthly_usage_inconsistent')
            with self.assertRaisesRegex(ledger.UsageFailure, 'azure_budget_exhausted'):
                usage.check()

    def test_legacy_unknown_does_not_become_zero_and_import_stays_idempotent(self):
        state = ledger.load_state(self.path)
        old = base.historical(date='2026-09-13', count=40)
        ledger.apply_import(state, old)
        self.assertEqual(money.balance(state, NOW)['reason'], 'monthly_cost_unknown')
        before = copy.deepcopy(state)
        ledger.apply_import(state, old)
        self.assertEqual(state, before)
        self.opening(7_931_426, count=40)
        with self.shared() as usage:
            self.issue(usage)
            self.assertEqual(ledger.usage_counts(usage.state, 'one', NOW)['remaining'], 2)
            self.assertEqual(ledger.usage_counts(usage.state, 'one', NOW)['day'], 41)
        self.assertEqual(ledger.load_state(self.path)['imports'][old['receiptId']]['counts']['requests'], 40)

    def test_month_crossing_rechecks_and_moves_unissued_reservation(self):
        self.now = dt.datetime(2026, 9, 30, 14, 59, 50, tzinfo=dt.timezone.utc)
        with self.shared() as usage:
            key = base.digest('month-end')
            usage.reserve(key, base.IDENTITY, request=self.request)
            self.now += dt.timedelta(seconds=20)
            usage.issued(key)
            receipt = next(iter(usage.state['receipts'].values()))
            self.assertEqual(receipt['date'], '2026-10-01')
            self.assertEqual(money.balance(usage.state, self.now)['month'], '2026-10')
            self.reply()
            self.client(usage).structured(self.messages, {}, **self.arguments)
            usage.finish(key, 'no_event')
            self.assertEqual(money.balance(usage.state, NOW)['reservedMicroJPY'], 0)

    def test_parallel_reservation_uses_the_existing_lock(self):
        with self.shared() as first:
            first.reserve(base.digest('first'), base.IDENTITY, request=self.request)
            with self.assertRaisesRegex(ledger.UsageFailure, 'azure_usage_locked'):
                with self.shared(run='two'):
                    self.fail('second writer entered')

    def observation(self, values):
        body = money.query_body(self.now)
        columns = ['PreTaxCost', 'UsageDate', 'ResourceId', 'Currency']
        response = {'properties': {'nextLink': None, 'columns': [{'name': name} for name in columns],
                                   'rows': [[Decimal(str(amount)), int(day.replace('-', '')), money.RESOURCE_ID, 'JPY']
                                            for day, amount in values.items()]}}
        return money.parse_billing(response, body, self.now)

    def test_daily_previous_days_zero_delay_failure_and_no_double_counting(self):
        self.opening(7_931_426)
        with self.shared() as usage:
            query = mock.Mock(return_value=self.observation({'2026-09-12': '7.9314259668'}))
            self.assertTrue(money.sync(usage, {}, query=query))
            self.assertFalse(money.sync(usage, {}, query=query))
            query.assert_called_once()
            self.assertEqual(money.balance(usage.state, NOW)['reconciledMicroJPY'], 7_931_426)
            self.now += dt.timedelta(days=1)
            query.return_value = self.observation({'2026-09-12': 0, '2026-09-13': 2})
            money.sync(usage, {}, query=query)
            self.assertEqual(money.balance(usage.state, self.now)['reconciledMicroJPY'], 9_931_426)
            previous = copy.deepcopy(usage.state['money']['billing']['lastSuccess'])
            self.now += dt.timedelta(days=1)
            query.side_effect = money.BillingFailure('query_failed')
            money.sync(usage, {}, query=query)
            self.assertEqual(usage.state['money']['billing']['lastSuccess'], previous)
            self.assertEqual(money.balance(usage.state, self.now)['azureFailure'], 'query_failed')
            self.assertEqual(money.balance(usage.state, self.now)['reconciledMicroJPY'], 9_931_426)

    def test_billing_refuses_scope_currency_future_and_preserves_retry_after(self):
        body = money.query_body(NOW)
        self.assertTrue(body['timePeriod']['to'].startswith('2026-09-12'))
        for field, bad in (('ResourceId', '/other'), ('Currency', 'USD'), ('UsageDate', 20260913)):
            columns = ['PreTaxCost', 'UsageDate', 'ResourceId', 'Currency']
            row = dict(PreTaxCost=Decimal(0), UsageDate=20260912, ResourceId=money.RESOURCE_ID, Currency='JPY')
            row[field] = bad
            with self.assertRaises(money.BillingFailure):
                money.parse_billing({'properties': {'columns': [{'name': key} for key in columns],
                                                    'rows': [[row[key] for key in columns]]}}, body, NOW)
        with self.shared() as usage:
            query = mock.Mock(side_effect=money.BillingFailure('rate_limited', '2026-09-16T00:00:00Z'))
            money.sync(usage, {}, query=query)
            self.now += dt.timedelta(days=1)
            self.assertFalse(money.sync(usage, {}, query=query))
            query.assert_called_once()
        first_day = dt.datetime(2026, 9, 30, 15, 1, tzinfo=dt.timezone.utc)
        with self.assertRaisesRegex(money.BillingFailure, 'no_closed_usage_day'):
            money.query_body(first_day)
        body = money.query_body(dt.datetime(2026, 10, 1, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(body['timePeriod']['from'], '2026-09-30T00:00:00Z')
        self.assertEqual(body['timePeriod']['to'], '2026-09-30T23:59:59Z')

    def test_utc_boundary_does_not_drop_jst_first_nine_hours_or_charge_them_twice(self):
        self.now = dt.datetime(2026, 9, 30, 14, tzinfo=dt.timezone.utc)
        with self.shared() as usage:
            first = self.issue(usage, 'september')
            self.reply()
            self.client(usage).structured(self.messages, {}, **self.arguments)
            usage.finish(first, 'no_event')
            first_amount = next(iter(usage.state['receipts'].values()))['money']['chargedMicroJPY']
            self.now += dt.timedelta(hours=2)
            second = self.issue(usage, 'october')
            self.client(usage).structured(self.messages, {}, **self.arguments)
            usage.finish(second, 'no_event')
            self.now = dt.datetime(2026, 10, 1, 1, tzinfo=dt.timezone.utc)
            usage.state['money']['billing']['highWater']['2026-09-30'] = first_amount * 2
            usage._save()
            value = money.balance(usage.state, self.now)
            self.assertEqual(value['tokenPricedMicroJPY'], first_amount)
            self.assertEqual(value['reconciledMicroJPY'], first_amount)
            self.assertEqual(value['utcBoundaryHoldMicroJPY'], 0)
            usage.state['money']['billing']['highWater']['2026-09-30'] += 100
            self.assertEqual(money.balance(usage.state, self.now)['utcBoundaryHoldMicroJPY'], 100)


if __name__ == '__main__':
    unittest.main()
