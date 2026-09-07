"""Mock-only stable-purpose/calendar/image regressions; no real model result."""
import copy
import datetime as dt
import io
import json
from unittest import mock
import unittest

import test_half_month_schedules as base


facts, azure = base.facts, base.azure


class CalendarTests(base.Offline):
    def normalize(self, value, *, created=base.CREATED, text=None, count=1):
        verified, body, _ = base.source(created=created, photos=count)
        return azure.normalize_result(value, verified, body if text is None else text, count)

    def test_independent_core_gold_mock(self):
        schedules = self.normalize(base.result())
        self.assertEqual(schedules[0]['period'], {'from': '2026-09-01', 'to': '2026-09-15',
                                                 'printedYear': None, 'yearBasis': 'post-context'})
        self.assertEqual([(row['date'], row['shifts']) for row in schedules[0]['days']],
                         [('2026-09-02', ['夜']), ('2026-09-05', ['昼']), ('2026-09-07', ['昼']),
                          ('2026-09-10', ['夜']), ('2026-09-12', ['昼']), ('2026-09-14', ['昼'])])

    def test_late_and_fifteenth_advance_post(self):
        for created in (dt.datetime(2026, 9, 2, tzinfo=facts.UTC), dt.datetime(2026, 9, 15, tzinfo=facts.UTC)):
            with self.subTest(created=created):
                result = self.normalize(base.result([(16, '水', ['夜'])], half='second'), created=created)
                self.assertEqual(result[0]['period']['from'], '2026-09-16')

    def test_next_month_and_year_crossing(self):
        created = dt.datetime(2026, 12, 28, tzinfo=facts.UTC)
        schedules = self.normalize(base.result([(2, '土', ['昼'])], month=1), created=created)
        self.assertEqual(schedules[0]['days'][0]['date'], '2027-01-02')
        schedules = self.normalize(base.result([(25, '金', ['昼'])], month=12, half='second'),
                                   created=dt.datetime(2027, 1, 2, tzinfo=facts.UTC))
        self.assertEqual(schedules[0]['days'][0]['date'], '2026-12-25')

    def test_post_context_cannot_echo_distant_target(self):
        with self.assertRaisesRegex(ValueError, 'calendar_unresolved'):
            self.normalize(base.result(month=3))

    def test_printed_vs_text_year(self):
        verified = self.normalize(base.result(printed=2026))[0]
        self.assertEqual(verified['period']['printedYear'], 2026)
        text = '2026年9月前半 2日 夜'
        verified = self.normalize(base.result([(2, '水', ['夜'])], text_year=2026, images=False),
                                  text=text, count=0)[0]
        self.assertEqual(verified['period']['yearBasis'], 'text')
        self.assertIsNone(verified['period']['printedYear'])
        with self.assertRaises(ValueError):
            self.normalize(base.result(text_year=2027, printed=2026), text='2027年9月')

    def test_leap_month_weekday_and_bad_calendar(self):
        created = dt.datetime(2024, 2, 20, tzinfo=facts.UTC)
        valid = self.normalize(base.result([(29, '木', ['昼'])], month=2, half='second'), created=created)
        self.assertEqual(valid[0]['period']['to'], '2024-02-29')
        for created in (dt.datetime(2026, 2, 20, tzinfo=facts.UTC), dt.datetime(2025, 2, 20, tzinfo=facts.UTC)):
            with self.subTest(created=created), self.assertRaises(ValueError):
                self.normalize(base.result([(29, None, ['昼'])], month=2, half='second'), created=created)
        with self.assertRaises(ValueError):
            self.normalize(base.result([(2, '月', ['夜'])]))

    def test_pending_non_schedule_and_malformed(self):
        self.assertEqual(self.normalize({'periods': []}), [])
        malformed = [{'periods': None}, {}, {'periods': {}, 'decision': 'ok'}]
        for field, value in [('month', None), ('half', None), ('days', None),
                             ('days', []), ('month', True), ('half', 'unknown'), ('imageIndexes', [1])]:
            item = base.result()
            item['periods'][0][field] = value
            malformed.append(item)
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.normalize(value)

    def test_duplicate_days_shifts_references_missing_unknown(self):
        for rows in ([(2, '水', ['夜']), (2, '水', ['昼'])],
                     [(2, '水', ['昼', '昼'])], [(2, None, None)], [(None, None, ['昼'])],
                     [(2, '', ['昼'])], [(2, '水', ['長め昼'])], [(16, None, ['夜'])]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.normalize(base.result(rows))
        value = base.result()
        value['periods'][0]['imageIndexes'] = [0, 0]
        with self.assertRaises(ValueError):
            self.normalize(value)
        value = base.result()
        value['periods'][0]['days'][0]['storeId'] = 's1'
        with self.assertRaises(ValueError):
            self.normalize(value)

    def test_full_month_and_overlap(self):
        value = base.result([(2, '水', ['夜']), (20, '日', ['昼', '夜'])], half='full')
        self.assertEqual(len(self.normalize(value)), 2)
        value['periods'].append(copy.deepcopy(value['periods'][0]))
        with self.assertRaises(ValueError):
            self.normalize(value)

    def test_outside_target_never_shifts_year(self):
        verified, text, _ = base.source()
        self.assertEqual(azure.normalize_result(base.result(), verified, text, 1,
                                                [('2027-09-01', '2027-09-15')]), [])

    def test_complete_full_month_projects_current_half_after_validation(self):
        verified, text, _ = base.source()
        now = dt.datetime(2026, 9, 20, tzinfo=facts.UTC)
        full = base.result([(2, '水', ['夜']), (20, '日', ['昼'])], half='full')
        accepted = azure.normalize_result(full, verified, text, 1, facts.target_periods(now))
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]['period']['from'], '2026-09-16')
        self.assertEqual(accepted[0]['days'], [{'date': '2026-09-20', 'shifts': ['昼']}])
        state = facts.empty_state()
        schedules, analysis = azure.saved_result(
            verified, text, [{'bytes': base.png(), 'mime': 'image/png'}], full, now=now,
            receipt_id=facts.digest(b'projected-full-month'), allowed_periods=facts.target_periods(now))
        facts.apply_revision(state, schedules, verified, analysis)
        self.assertEqual(next(iter(state['sources'].values()))['status'], 'valid')

    def test_expired_invalid_or_partial_sibling_is_not_filtered_away(self):
        verified, text, _ = base.source()
        allowed = facts.target_periods(dt.datetime(2026, 9, 20, tzinfo=facts.UTC))
        for day in ({'day': 2, 'weekday': '月', 'shifts': ['夜']},
                    {'day': 2, 'weekday': '水', 'shifts': None},
                    {'day': None, 'weekday': None, 'shifts': ['夜']},
                    {'day': 2, 'weekday': '水', 'shifts': ['夜', '夜']}):
            full = base.result([(20, '日', ['昼'])], half='full')
            full['periods'][0]['days'].insert(0, day)
            with self.subTest(day=day), self.assertRaises(ValueError):
                azure.normalize_result(full, verified, text, 1, allowed)
        valid_first = base.result([(2, '水', ['夜'])])['periods'][0]
        valid_second = base.result([(20, '日', ['昼'])], half='second')['periods'][0]
        for bad in ({**valid_first, 'days': None}, {**valid_first, 'month': None},
                    {**valid_first, 'days': [*valid_first['days'], *valid_first['days']]}):
            for periods in ([bad, valid_second], [valid_second, bad]):
                with self.subTest(periods=periods), self.assertRaises(ValueError):
                    azure.normalize_result({'periods': periods}, verified, text, 1, allowed)


class ImageTests(base.Offline):
    def test_png_and_saved_medium_header(self):
        self.assertEqual(azure.probe_image(base.png(), 'image/png')['width'], 9)
        verified, text, _ = base.source(mime='jpg')
        image = {'bytes': base.jpeg(), 'mime': 'image/jpeg'}
        messages, proof = azure.prepare_request(verified, text, [image])
        context = json.loads(messages[1]['content'][0]['text'])
        self.assertEqual(context['images'][0]['originalWidth'], 1536)
        self.assertEqual(context['images'][0]['width'], 900)
        self.assertEqual(proof['images'][0]['height'], 1200)
        self.assertEqual(messages[1]['content'][1]['image_url']['detail'], 'high')
        self.assertNotIn('pbs.twimg.com', json.dumps(messages))
        self.assertNotIn('gold', json.dumps(context))
        self.assertNotIn('lineIds', json.dumps(context))

    def test_bad_signature_type_dimensions_truncated_and_size(self):
        for raw, mime in [(b'', 'image/png'), (b'<html>challenge</html>', 'image/jpeg'),
                          (base.png(), 'image/jpeg'), (base.png(), 'image/webp'),
                          (base.png()[:-3], 'image/png'), (base.jpeg()[:-2], 'image/jpeg'),
                          (base.png(width=8193), 'image/png'), (base.jpeg(6000, 4000), 'image/jpeg'),
                          (b'x' * (azure.MAX_IMAGE_BYTES + 1), 'image/png')]:
            with self.subTest(mime=mime, length=len(raw)), self.assertRaises(ValueError):
                azure.probe_image(raw, mime)
        damaged = bytearray(base.png())
        damaged[20] ^= 1
        with self.assertRaises(ValueError):
            azure.probe_image(damaged, 'image/png')

    def test_all_images_and_total_pixel_limit(self):
        verified, text, _ = base.source(photos=2)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            azure.prepare_request(verified, text, [{'bytes': base.png(), 'mime': 'image/png'}])
        images = [{'bytes': base.jpeg(4500, 4000), 'mime': 'image/jpeg'}] * 3
        with self.assertRaisesRegex(ValueError, 'post_limit'):
            azure.image_facts(images)
        with self.assertRaises(ValueError):
            azure.image_facts([{'bytes': base.png(), 'mime': 'image/png'}] * 5)

    def test_input_and_serialized_request_limits(self):
        verified, text, _ = base.source()
        with self.assertRaises(ValueError):
            azure.prepare_request(verified, '昼' * 2001, [])
        with mock.patch.object(azure, 'MAX_REQUEST_BYTES', 100), self.assertRaises(ValueError):
            azure.prepare_request(verified, text, [{'bytes': base.png(), 'mime': 'image/png'}])


class TransportTests(base.Offline):
    def analyzer(self, result=None):
        usage = mock.Mock()
        client = mock.Mock()
        client.identity = {'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
                           'deployment': facts.MODEL, 'model': facts.MODEL, 'modelVersion': facts.MODEL_VERSION}
        client.structured.return_value = base.result() if result is None else result
        return azure.AzureAnalyzer(usage, clock=lambda: base.NOW, client=client), usage, client

    def test_shared_reservation_one_request_and_fingerprint(self):
        analyzer, usage, client = self.analyzer()
        verified, text, _ = base.source()
        image = {'bytes': base.png(), 'mime': 'image/png'}
        issued = mock.Mock()
        schedules, proof = analyzer.analyze(verified, text, [image], facts.target_periods(base.NOW), issued)
        self.assertEqual(len(schedules[0]['days']), 6)
        usage.reserve.assert_called_once()
        usage.issued.assert_called_once_with(proof['requestHash'])
        usage.finish.assert_called_once_with(proof['requestHash'], 'events')
        issued.assert_called_once_with(proof['requestHash'])
        self.assertEqual(client.structured.call_args.kwargs['max_completion_tokens'], 1200)
        with self.assertRaises(azure.AnalysisFailure):
            analyzer.analyze(verified, text, [image], facts.target_periods(base.NOW), issued)
        self.assertEqual(client.structured.call_count, 1)

    def test_failure_consumed_without_retry(self):
        analyzer, usage, client = self.analyzer()
        client.structured.side_effect = azure.AnalysisFailure('azure_timeout')
        verified, text, _ = base.source()
        with self.assertRaises(azure.AnalysisFailure):
            analyzer.analyze(verified, text, [{'bytes': base.png(), 'mime': 'image/png'}],
                             facts.target_periods(base.NOW), lambda _: None)
        self.assertEqual(usage.finish.call_args.args[1], 'azure_timeout')
        self.assertEqual(client.structured.call_count, 1)

    def test_no_unsafe_unaccounted_analyzer(self):
        with self.assertRaises(ValueError):
            azure.AzureAnalyzer(None, clock=lambda: base.NOW, client=mock.Mock())

    def test_actual_transport_wire_and_model_rejection_are_mocked(self):
        for output_model, refusal, finish, succeeds in [
                ('gpt-5.6-luna-2026-07-09', None, 'stop', True),
                ('gpt-5.6-luna-compare', None, 'stop', False),
                ('gpt-5.6-luna', 'no', 'stop', False),
                ('gpt-5.6-luna', None, 'length', False)]:
            envelope = {'model': output_model, 'choices': [{'finish_reason': finish,
                          'message': {'content': json.dumps(base.result()), 'refusal': refusal}}]}
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.getcode.return_value = 200
            response.read.return_value = json.dumps(envelope).encode()
            opener = mock.Mock()
            opener.open.return_value = response
            client = azure.transport.AzureOpenAI(
                {'AZURE_OPENAI_ENDPOINT': 'https://offline.openai.azure.com',
                 'AZURE_OPENAI_API_KEY': 'SYNTHETIC_ONLY'}, on_http_failure=mock.Mock(), opener=opener)
            verified, text, _ = base.source()
            messages, proof = azure.prepare_request(verified, text, [{'bytes': base.png(), 'mime': 'image/png'}])
            with self.subTest(model=output_model, refusal=refusal, finish=finish):
                if succeeds:
                    client.structured(messages, azure.SCHEMA, name='half_month_schedule', max_completion_tokens=1200)
                    request = opener.open.call_args.args[0]
                    self.assertEqual(facts.digest(request.data), proof['requestHash'])
                    self.assertEqual(json.loads(request.data)['reasoning_effort'], 'none')
                else:
                    with self.assertRaises(azure.AnalysisFailure):
                        client.structured(messages, azure.SCHEMA, name='half_month_schedule', max_completion_tokens=1200)


if __name__ == '__main__':
    unittest.main()
