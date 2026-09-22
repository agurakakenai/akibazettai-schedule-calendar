"""Offline monetary boundaries on the actual shared reservation/HTTP path."""
import copy
import contextlib
import datetime as dt
from decimal import Decimal
import io
import json
import os
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
        temporary = tempfile.TemporaryDirectory(dir=base.TOOLS / 'tests', prefix='budget-test-')
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

    def shared(self, run='one', component='personal', **kwargs):
        return ledger.SharedUsage(self.path, run_id=run, component=component,
                                  clock=lambda: self.now, sleep=self.sleep, **kwargs)

    def finite(self, **kwargs):
        return self.shared(request_limit=None, run_limit=None, **kwargs)

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

    def test_official_cli_finite_batches_share_run_without_a_three_request_terminal(self):
        import test_official_azure as fixture
        official, model = fixture.official, fixture.azure
        self.now = fixture.NOW
        snapshot = self.path.parent / 'official.json'
        curated = self.path.parent / 'curated.csv'
        curated.write_text('tweet_id,maid\n', encoding='utf-8')
        envelope = self.envelope()
        envelope['choices'][0]['message']['content'] = json.dumps(fixture.decision())
        self.reply(envelope)
        def segment(offset, limit=3, target=snapshot):
            posts = {str(int(fixture.TID) + index): fixture.payload(tid=str(int(fixture.TID) + index))
                     for index in range(offset, offset + 3)}
            args = official.argument_parser().parse_args([
                '--once', '--catch-up', '--analysis-backend', 'azure',
                '--ai-state', str(self.path), '--analysis-run-id', '12345-1',
                '--analysis-limit', str(limit), '--max-posts', '3',
                '--date-from', fixture.DAY.isoformat(), '--date-to', fixture.DAY.isoformat(),
                '--snapshot', str(target)])
            with mock.patch.dict(os.environ, fixture.ENV), \
                    mock.patch.object(official, 'analysis_module', return_value=model), \
                    mock.patch.object(model, 'load_names', return_value=fixture.NAMES), \
                    mock.patch.object(model.transport.urllib.request, 'build_opener', return_value=self.opener), \
                    contextlib.redirect_stdout(io.StringIO()):
                return official.run(args, curated=curated, client=fixture.Source(posts),
                                    clock=lambda: self.now, sleep=self.sleep)
        self.assertEqual(segment(0), 0)
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(segment(3), 0)
        self.assertEqual(self.opener.open.call_count, 6)
        receipts = ledger.load_state(self.path)['receipts']
        self.assertEqual(len(receipts), 6)
        self.assertEqual({row['runId'] for row in receipts.values()}, {'12345-1'})
        self.assertTrue(all(row['money']['chargedMicroJPY'] is not None for row in receipts.values()))
        self.assertEqual(segment(3), 0)
        self.assertEqual(self.opener.open.call_count, 6)
        self.assertEqual(segment(6, 0, self.path.parent / 'disabled.json'), 2)
        self.assertEqual(self.opener.open.call_count, 6)

    def test_finite_queue_exceeds_forty_with_cached_non_events_and_707_hold(self):
        self.opening(707_000_000, count=40)
        expected_cost = 0
        for component, count in (('personal', 41), ('official', 5), ('schedule', 5)):
            with self.finite(component=component) as usage:
                for index in range(count):
                    usage.check()
                    key = self.issue(usage, f'{component}-{index}')
                    envelope = self.envelope(cached=80 if index % 2 else 0)
                    self.reply(envelope)
                    self.assertEqual(self.client(usage).structured(self.messages, {}, **self.arguments), {'ok': True})
                    usage.finish(key, 'no_event' if index % 2 else 'events')
                    charge = usage.state['receipts'][ledger._receipt_id(component, key)]['money']
                    expected_cost += charge['chargedMicroJPY']
                    before = copy.deepcopy(usage.state)
                    with self.assertRaisesRegex(ledger.UsageFailure, 'azure_already_analyzed'):
                        usage.reserve(key, base.IDENTITY, request=self.request)
                    self.assertEqual(usage.state, before)
                usage.check()
        state = ledger.load_state(self.path)
        self.assertIsNone(ledger.CATCHUP_RUN_LIMIT)
        self.assertEqual(ledger.usage_counts(state, 'one', self.now, None),
                         {'run': 51, 'day': 91, 'remaining': None})
        self.assertIsNone(ledger.remaining(state, 'one', self.now, run_limit=None))
        self.assertEqual(self.opener.open.call_count, 51)
        receipts = list(state['receipts'].values())
        times = sorted(ledger._time(item['issuedAt']) for item in receipts)
        self.assertTrue(all((after - before).total_seconds() >= 60 for before, after in zip(times, times[1:])))
        balance = money.balance(state, self.now)
        self.assertEqual(balance['openingProvisionalMicroJPY'], 707_000_000)
        self.assertEqual(balance['tokenPricedMicroJPY'], expected_cost)
        self.assertEqual(balance['reservedMicroJPY'], 0)
        self.assertEqual(balance['availableMicroJPY'], 293_000_000 - expected_cost)

    def test_finite_unmigrated_and_unknown_costs_fail_before_http_or_sleep(self):
        for migrated in (False, True):
            with self.subTest(migrated=migrated):
                state = ledger.empty_state()
                if migrated:
                    state['money'] = money.empty()
                    ledger.apply_import(state, base.historical(date='2026-09-13'))
                ledger.atomic_json(self.path, state)
                with self.finite() as usage, mock.patch.object(usage, 'sleep') as sleep:
                    self.assertEqual(ledger.remaining(usage.state, 'one', self.now, None), 0)
                    with self.assertRaisesRegex(ledger.UsageFailure, 'azure_budget_exhausted'):
                        usage.check()
                    with self.assertRaisesRegex(ledger.UsageFailure, 'azure_budget_exhausted'):
                        self.issue(usage)
                    self.assertEqual(usage.state, state)
                    sleep.assert_not_called()
                self.opener.open.assert_not_called()

    def test_finite_exact_budget_reservation_reconciles_without_count_allowance(self):
        reservation = money.reservation(base.IDENTITY, self.request)['reservedMicroJPY']
        self.opening(money.LIMIT - reservation)
        with self.finite() as usage:
            self.assertIsNone(ledger.remaining(usage.state, 'one', self.now, None))
            key = self.issue(usage)
            self.assertEqual(ledger.remaining(usage.state, 'one', self.now, None), 0)
            self.reply()
            self.client(usage).structured(self.messages, {}, **self.arguments)
            usage.finish(key, 'no_event')
            self.assertIsNone(ledger.remaining(usage.state, 'one', self.now, None))
            with self.assertRaisesRegex(ledger.UsageFailure, 'azure_budget_exhausted'):
                self.issue(usage, 'does-not-fit')
            self.assertEqual(len(usage.state['receipts']), 1)
        self.opener.open.assert_called_once()
        self.setUp_ledger()
        self.opening(money.LIMIT - reservation + 1)
        self.opener.reset_mock()
        with self.finite() as usage:
            with self.assertRaisesRegex(ledger.UsageFailure, 'azure_budget_exhausted'):
                self.issue(usage)
            self.assertEqual(usage.state['receipts'], {})
        self.opener.open.assert_not_called()

    def test_finite_707_hold_rejects_image_reservation_larger_than_remaining_money(self):
        self.opening(707_000_000)
        image = azure.request_budget([{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}
        ]}], {}, **self.arguments)
        self.assertGreater(money.reservation(base.IDENTITY, image)['reservedMicroJPY'], 293_000_000)
        with self.finite() as usage:
            with self.assertRaisesRegex(ledger.UsageFailure, 'azure_budget_exhausted'):
                usage.reserve(base.digest('image'), base.IDENTITY, request=image)
            self.assertEqual(usage.state['receipts'], {})
            self.assertEqual(money.balance(usage.state, self.now)['openingProvisionalMicroJPY'], 707_000_000)
        self.opener.open.assert_not_called()

    def test_finite_queue_still_obeys_backoff_auth_deadline_and_request_validation(self):
        for status, reason in ((429, 'azure_rate_limited'), (401, 'azure_auth_stopped'), (403, 'azure_auth_stopped')):
            with self.subTest(status=status):
                self.setUp_ledger()
                with self.finite() as usage:
                    key = self.issue(usage, str(status))
                    with self.assertRaisesRegex(ledger.UsageFailure, reason):
                        usage.http_failure(status, '600')
                    usage.finish(key, reason)
                with self.finite(run='next') as usage:
                    before = copy.deepcopy(usage.state)
                    for operation in (usage.check, lambda: self.issue(usage, 'blocked')):
                        with self.assertRaisesRegex(ledger.UsageFailure,
                                                    'azure_backoff' if status == 429 else reason):
                            operation()
                    self.assertEqual(usage.state, before)
        self.setUp_ledger()
        with self.finite(deadline=lambda: False) as usage:
            with self.assertRaisesRegex(ledger.UsageFailure, 'azure_deadline'):
                self.issue(usage)
        with self.finite() as usage:
            for request in (None, {**self.request, 'inputCeiling': money.INPUT_MAX + 1},
                            {**self.request, 'outputCeiling': money.OUTPUT_MAX + 1}):
                with self.assertRaisesRegex(ledger.UsageFailure, 'azure_input_limit'):
                    usage.reserve(base.digest('oversized'), base.IDENTITY, request=request)
            self.assertEqual(usage.state['receipts'], {})
        self.opener.open.assert_not_called()

    def test_finite_charge_uses_issue_day_and_month_not_reservation_or_completion(self):
        for day in (13, 30):
            with self.subTest(day=day):
                self.setUp_ledger()
                self.now = dt.datetime(2026, 9, day, 14, 59, 50, tzinfo=dt.timezone.utc)
                reserved_at = self.now
                with self.finite() as usage:
                    key = base.digest('boundary')
                    usage.reserve(key, base.IDENTITY, request=self.request)
                    self.now += dt.timedelta(seconds=20)
                    usage.issued(key)
                    issued_at = self.now
                    self.reply()

                    def delayed_response(*args):
                        self.now += dt.timedelta(days=1)
                        return json.dumps(self.envelope()).encode()

                    self.opener.open.return_value.read.side_effect = delayed_response
                    self.client(usage).structured(self.messages, {}, **self.arguments)
                    usage.finish(key, 'no_event')
                    receipt = next(iter(usage.state['receipts'].values()))
                    self.assertEqual(receipt['date'], issued_at.astimezone(ledger.JST).date().isoformat())
                    self.assertEqual(ledger.usage_counts(usage.state, 'one', reserved_at, None)['day'], 0)
                    self.assertEqual(ledger.usage_counts(usage.state, 'one', issued_at, None)['day'], 1)
                    self.assertEqual(ledger.usage_counts(usage.state, 'one', self.now, None)['day'], 0)
                    if day == 30:
                        self.assertEqual(money.balance(usage.state, reserved_at)['tokenPricedMicroJPY'], 0)
                        self.assertEqual(money.balance(usage.state, issued_at)['tokenPricedMicroJPY'],
                                         receipt['money']['chargedMicroJPY'])

    def test_finite_month_rollover_rechecks_money_at_issue_and_final_http_gate(self):
        for gate in ('issue', 'http'):
            with self.subTest(gate=gate):
                self.setUp_ledger()
                self.now = dt.datetime(2026, 9, 30, 14, 59, 50, tzinfo=dt.timezone.utc)
                with self.finite() as usage:
                    key = base.digest('new-month-blocked')
                    usage.reserve(key, base.IDENTITY, request=self.request)
                    if gate == 'http':
                        usage.issued(key)
                    usage.state['money']['billing']['highWater']['2026-10-01'] = money.LIMIT
                    usage._save()
                    self.now += dt.timedelta(seconds=20)
                    with self.assertRaisesRegex(ledger.UsageFailure, 'azure_budget_exhausted'):
                        if gate == 'issue':
                            usage.issued(key)
                        else:
                            usage.authorize_http(base.IDENTITY, self.request)
                    self.assertEqual(ledger.remaining(usage.state, 'one', self.now, None), 0)
                    self.assertEqual(len(usage.state['receipts']), 1)
                self.opener.open.assert_not_called()

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

    def test_cost_query_checks_cli_principal_tenant_subscription_before_http(self):
        client = '11111111-1111-4111-8111-111111111111'
        tenant = '22222222-2222-4222-8222-222222222222'
        subscription = money.RESOURCE_ID.split('/')[2]
        environment = {'AZURE_COST_CLIENT_ID': client, 'AZURE_COST_TENANT_ID': tenant,
                       'AZURE_COST_SUBSCRIPTION_ID': subscription,
                       'GH_TOKEN': 'not-forwarded', 'AZURE_OPENAI_API_KEY': 'not-forwarded',
                       'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'not-forwarded'}
        account = {'id': subscription, 'tenantId': tenant,
                   'user': {'name': client, 'type': 'servicePrincipal'}}
        credential = {'subscription': subscription, 'tenant': tenant, 'accessToken': 'offline-credential'}
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = 200
        response.read.return_value = b'{}'
        body = money.query_body(NOW)
        for changed in ('none', 'configuration', 'scope', 'id', 'tenantId', 'name', 'type',
                        'token-subscription', 'token-tenant', 'malformed', 'rate-limited'):
            with self.subTest(changed=changed):
                env, info, token = copy.deepcopy((environment, account, credential))
                if changed == 'configuration':
                    env.pop('AZURE_COST_CLIENT_ID')
                elif changed == 'scope':
                    env['AZURE_COST_SUBSCRIPTION_ID'] = client
                elif changed in ('id', 'tenantId'):
                    info[changed] = client
                elif changed in ('name', 'type'):
                    info['user'][changed] = 'other'
                elif changed.startswith('token-'):
                    token[changed[6:]] = client
                elif changed == 'malformed':
                    info['user']['name'] = None
                results = [mock.Mock(returncode=0, stdout=json.dumps(item)) for item in (info, token)]
                with mock.patch.object(money.subprocess, 'run', side_effect=results) as cli, \
                        mock.patch.object(money.urllib.request, 'build_opener') as opener, \
                        mock.patch.object(money, 'query_body', return_value=body), \
                        mock.patch.object(money, 'parse_billing', return_value={'checked': True}), \
                        mock.patch.object(money.sys, 'stderr', new_callable=io.StringIO) as errors:
                    now = dt.datetime.now(dt.timezone.utc)
                    opener.return_value.open.return_value = response
                    if changed in ('none', 'rate-limited'):
                        if changed == 'none':
                            self.assertEqual(money.query_azure(env, now), {'checked': True})
                        else:
                            headers = {'Retry-After': '30',
                                       'x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after': '720',
                                       'x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after': '60',
                                       'Authorization': 'never-log-this'}
                            opener.return_value.open.side_effect = money.urllib.error.HTTPError(
                                'https://management.azure.com/', 429, 'limited', headers, io.BytesIO(b'private'))
                            with self.assertRaises(money.BillingFailure) as failure:
                                money.query_azure(env, now)
                            self.assertEqual(failure.exception.reason, 'rate_limited')
                            self.assertEqual(failure.exception.retry_at, money.stamp(now + dt.timedelta(seconds=720)))
                            self.assertIn('tenant-retry-after', errors.getvalue())
                            self.assertIn('clienttype-retry-after', errors.getvalue())
                            self.assertNotIn('never-log-this', errors.getvalue())
                            self.assertNotIn('private', errors.getvalue())
                        self.assertEqual(cli.call_count, 2)
                        self.assertEqual(cli.call_args_list[0].args[0][1:3], ['account', 'show'])
                        opener.return_value.open.assert_called_once()
                        self.assertIn(money.RESOURCE_ID.split('/providers/')[0],
                                      opener.return_value.open.call_args.args[0].full_url)
                    else:
                        reason = {'configuration': 'auth_not_configured', 'scope': 'scope_mismatch'}.get(
                            changed, 'auth_failed')
                        with self.assertRaisesRegex(money.BillingFailure, reason):
                            money.query_azure(env, now)
                        opener.assert_not_called()
                        self.assertEqual(cli.call_count, 0 if changed in ('configuration', 'scope')
                                         else 2 if changed.startswith('token-') else 1)
                    for call in cli.call_args_list:
                        self.assertTrue(set(call.kwargs['env']).isdisjoint(
                            {'GH_TOKEN', 'AZURE_OPENAI_API_KEY', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN'}))

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
