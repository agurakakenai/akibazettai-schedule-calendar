"""Synthetic v3 adapter regressions. All model and network calls are mocked."""
import copy
import datetime as dt
import io
import json
import struct
import unittest
import zlib
from unittest import mock

import test_half_month_schedules as base


azure, facts = base.azure, base.facts


def fixture(*, photos=1, text='9月前半のお給仕予定', reply=False):
    source, _, _ = base.source(photos=photos)
    source['bodyHash'] = facts.digest(text.encode('utf-8'))
    if reply:
        source.update(replyToId=base.post_id(base.CREATED - dt.timedelta(days=1)),
                      replyToAuthorId=source['authorId'])
    return source, text, [{'bytes': base.png(), 'mime': 'image/png'} for _ in range(photos)]


def row(day=5, shifts=('昼',), *, weekday='土', transcription='5日 昼', qualifier=None,
        start=None, end=None, index=0, box=None, operation=None):
    return {'day': day, 'weekday': weekday, 'shifts': None if shifts is None else list(shifts),
            'workTiming': [], 'transcription': transcription, 'qualifier': qualifier,
            'explicitStart': start, 'explicitEnd': end,
            'evidence': {'imageIndex': index, 'box': box}, 'operation': operation}


def result(*rows, complete=True, classification='schedule', month=9, half='first',
           printed=None, text_year=None, indexes=(0,)):
    return {'classification': classification, 'complete': complete,
            'periods': [{'month': month, 'half': half, 'printedYear': printed,
                         'textYear': text_year, 'imageIndexes': list(indexes),
                         'days': list(rows or (row(),))}]}


class NormalizationTests(base.Offline):
    def normalize(self, value, context=None, allowed=None):
        source, text, images = context or fixture()
        metadata = [azure.probe_image(image['bytes'], image['mime']) for image in images]
        return azure.normalize_reading(value, source, text, len(images), allowed,
                                       image_metadata=metadata)

    def test_date_only_and_unreadable_rows_are_distinct(self):
        schedules, complete = self.normalize(result(
            row(shifts=(), transcription='5日'),
            row(7, None, weekday='月', transcription='7日 判読不能')))
        self.assertFalse(complete)
        self.assertEqual([item['shifts'] for item in schedules[0]['days']], [[], []])
        details = schedules[0]['reading']['days']
        self.assertEqual(details['2026-09-05']['shiftStatus'], 'unstated')
        self.assertEqual(details['2026-09-07']['shiftStatus'], 'unreadable')
        self.assertFalse(schedules[0]['reading']['complete'])
        schedule, complete = self.normalize(result(row(shifts=(), transcription='5日')))
        self.assertTrue(complete)
        self.assertEqual(schedule[0]['days'][0]['shifts'], [])

    def test_unknown_day_and_unknown_period_keep_readable_partial(self):
        for unknown in (row(None, None, weekday=None, transcription='?'),
                        row(None, (), weekday=None, transcription='')):
            schedules, complete = self.normalize(result(row(), unknown))
            self.assertFalse(complete)
            self.assertEqual(len(schedules[0]['days']), 1)
        value = result()
        value['periods'].append({**copy.deepcopy(value['periods'][0]), 'month': None})
        schedules, complete = self.normalize(value)
        self.assertFalse(complete)
        self.assertFalse(schedules[0]['reading']['complete'])

    def test_hours_rules_explicit_precedence_and_no_customary_clock(self):
        cases = [
            (row(qualifier='long', transcription='5日 ながめ昼'), ['昼'], '12:00', '18:00'),
            (row(shifts=('夜',), qualifier='early', transcription='5日 はやめ夜'), ['夜'], '16:00', '22:00'),
            (row(shifts=(), qualifier='late', transcription='5日 おそめ'), ['夜'], '18:00', '22:00'),
            (row(shifts=('夜',), qualifier='long', transcription='5日 長め夜'), ['夜'], None, None),
            (row(shifts=(), qualifier='all_day', transcription='5日 オーラス'), ['昼', '夜'], None, None),
            (row(qualifier='all_day', transcription='5日 おーらす'), ['昼', '夜'], None, None),
            (row(qualifier='long', transcription='5日 長め昼 13:00〜17:00',
                 start='13:00', end='17:00'), ['昼'], '13:00', '17:00'),
            (row(shifts=(), transcription='5日 13:00〜17:00', start='13:00', end='17:00'),
             [], '13:00', '17:00'),
        ]
        for item, shifts, start, end in cases:
            with self.subTest(item=item):
                schedules, complete = self.normalize(result(item))
                self.assertTrue(complete)
                schedule = schedules[0]
                self.assertEqual(schedule['days'][0]['shifts'], shifts)
                hours = schedule['reading']['days']['2026-09-05']['hours']
                for key, time in [('start', start), ('end', end)]:
                    if time is None:
                        self.assertNotIn(key, hours)
                    else:
                        self.assertEqual(hours[key], {
                            'time': time, 'basis': 'explicit' if item['explicit' + key.title()]
                            else 'qualifier-rule-v1'})
                self.assertNotIn('workTiming', schedule)

    def test_unknown_shift_never_inherits_rule_hours(self):
        schedules, complete = self.normalize(result(
            row(shifts=None, qualifier='late', transcription='5日 おそめ')))
        self.assertFalse(complete)
        self.assertEqual(schedules[0]['days'][0]['shifts'], [])
        self.assertEqual(schedules[0]['reading']['days']['2026-09-05']['hours'], {})

    def test_explicit_clocks_and_qualifiers_must_be_in_row_transcription(self):
        for item in [row(start='13:00'), row(qualifier='long'),
                     row(end='24:00', transcription='5日 24:00'),
                     row(start='13:00', transcription='5日 113:00')]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.normalize(result(item))
        item = row(transcription='5日 昼13時〜17時', start='13:00', end='17:00')
        self.assertTrue(self.normalize(result(item))[1])

    def test_body_evidence_is_grounded_and_hash_only_is_public(self):
        context = fixture(photos=0, text='9月前半 5日 長め昼 13:00〜17:00')
        item = row(index=None, qualifier='long', start='13:00', end='17:00',
                   transcription='5日 長め昼 13:00〜17:00')
        schedules, _ = self.normalize(result(item, indexes=()), context)
        detail = schedules[0]['reading']['days']['2026-09-05']
        self.assertEqual(detail['evidence'], {'imageIndex': None, 'box': None, 'imageHash': None})
        self.assertEqual(detail['transcriptionHash'], facts.digest(item['transcription'].encode()))
        self.assertNotIn(item['transcription'], json.dumps(schedules, ensure_ascii=False))
        item['transcription'] = '5日 昼 15:00'
        with self.assertRaisesRegex(ValueError, 'transcription'):
            self.normalize(result(item, indexes=()), context)

    def test_full_month_year_weekdays_and_filter_after_validation(self):
        context = fixture(text='2026年9月予定')
        value = result(row(), row(20, (), weekday='日', transcription='20日'),
                       half='full', text_year=2026)
        schedules, complete = self.normalize(value, context)
        self.assertTrue(complete)
        self.assertEqual([item['period']['from'] for item in schedules], ['2026-09-01', '2026-09-16'])
        self.assertTrue(self.normalize(value, context, {('2026-09-01', '2026-09-15')})[1])
        value['periods'][0]['days'][1]['weekday'] = '月'
        with self.assertRaisesRegex(ValueError, 'calendar'):
            self.normalize(value, context, {('2026-09-01', '2026-09-15')})
        value['periods'][0]['days'][1]['weekday'] = '日'
        value['periods'][0]['printedYear'] = 2027
        with self.assertRaisesRegex(ValueError, 'year_conflict'):
            self.normalize(value, context)

    def test_year_rollover_and_distant_target_do_not_guess(self):
        source, _, _ = base.source(created=dt.datetime(2026, 12, 28, tzinfo=facts.UTC))
        text = '1月前半予定'
        source['bodyHash'] = facts.digest(text.encode())
        context = source, text, [{'bytes': base.png(), 'mime': 'image/png'}]
        schedules, _ = self.normalize(result(row(5, weekday='火'), month=1), context)
        self.assertEqual(schedules[0]['days'][0]['date'], '2027-01-05')
        self.assertEqual(self.normalize(result(row(5, weekday='火'), month=1), context,
                                        {('2028-01-01', '2028-01-15')})[0], [])

    def test_multiimage_evidence_keeps_original_hash_and_reference(self):
        context = fixture(photos=2)
        item = row(index=1, box=[.1, .2, .8, .9])
        schedules, _ = self.normalize(result(item, indexes=(0, 1)), context)
        evidence = schedules[0]['reading']['days']['2026-09-05']['evidence']
        self.assertEqual(evidence, {'imageIndex': 1, 'box': [.1, .2, .8, .9],
                                   'imageHash': facts.digest(base.png())})
        with self.assertRaisesRegex(ValueError, 'reading_image'):
            self.normalize(result(item, indexes=(0,)), context)
        source, text, _ = context
        with self.assertRaisesRegex(ValueError, 'metadata_required'):
            azure.normalize_reading(result(item, indexes=(0, 1)), source, text, 2)

    def test_work_timing_remains_compact_and_independent(self):
        item = row(transcription='5日 長め昼 17:00まで', qualifier='long', end='17:00')
        item['workTiming'] = [{'shift': '昼', 'kind': 'long', 'time': '17:00'}]
        schedules, _ = self.normalize(result(item))
        self.assertEqual(schedules[0]['workTiming']['facts'][0]['explicitTime'], '17:00')
        self.assertEqual(schedules[0]['reading']['days']['2026-09-05']['hours']['end'],
                         {'time': '17:00', 'basis': 'explicit'})
        item['workTiming'][0]['time'] = '18:00'
        with self.assertRaisesRegex(ValueError, 'clock_ungrounded'):
            self.normalize(result(item))
        item['workTiming'] = None
        self.assertTrue(self.normalize(result(item))[1])

    def test_own_reply_operations_are_explicit_and_always_partial(self):
        for operation, word, shifts in [('add', '追加', ('昼',)), ('replace', '変更', ('夜',)),
                                        ('cancel', 'キャンセル', ())]:
            context = fixture(photos=0, text=f'9月前半 5日 {word}', reply=True)
            item = row(shifts=shifts, index=None, operation=operation, transcription=f'5日 {word}')
            schedules, complete = self.normalize(result(item, indexes=()), context)
            self.assertFalse(complete)
            self.assertEqual(schedules[0]['sourceKind'], 'own-reply')
            self.assertEqual(schedules[0]['replyToAuthorId'], context[0]['authorId'])
            self.assertEqual(schedules[0]['reading']['days']['2026-09-05']['operation'], operation)
            facts.validate_schedule(schedules[0])
        context = fixture(reply=True)
        for operation, text in [(None, '5日 昼'), ('replace', '5日 昼'), ('add', '5日 追加なし'),
                                ('replace', '5日 変更しません'), ('cancel', '5日 no cancellation')]:
            with self.subTest(operation=operation, text=text), self.assertRaises(ValueError):
                self.normalize(result(row(operation=operation, transcription=text)), context)

    def test_reply_identity_and_nonreply_operation_fail_closed(self):
        context = fixture(reply=True)
        context[0]['replyToAuthorId'] = '123456789'
        with self.assertRaises(ValueError):
            self.normalize(result(row(operation='add', transcription='5日 追加')), context)
        del context[0]['replyToAuthorId']
        with self.assertRaises(ValueError):
            self.normalize(result(), context)
        with self.assertRaises(ValueError):
            self.normalize(result(row(operation='replace', transcription='5日 変更')))

    def test_non_schedule_uncertain_and_caption_conflict(self):
        for classification, complete, text, expected in [
                ('non_schedule', True, '自撮り', True),
                ('non_schedule', True, '9月の予定', False),
                ('uncertain', True, '自撮り', False),
                ('schedule', True, '予定', False)]:
            value = {'classification': classification, 'complete': complete, 'periods': []}
            self.assertEqual(self.normalize(value, fixture(text=text)), ([], expected))
        with self.assertRaises(ValueError):
            self.normalize(result(classification='non_schedule'))


class RequestTests(base.Offline):
    def analyzer(self, value):
        usage, client = mock.Mock(), mock.Mock()
        client.structured.return_value = value
        return azure.AzureAnalyzer(usage, clock=lambda: base.NOW, client=client), usage, client

    def test_readable_originals_are_one_identification_and_extraction_call(self):
        source, text, images = fixture(photos=4)
        packs = azure.reading_packs(source, text, images)
        self.assertEqual(len(packs), 1)
        self.assertEqual(len(packs[0]['images']), 4)
        value = result(indexes=(0, 1, 2, 3))
        analyzer, usage, client = self.analyzer(value)
        sequence = []
        usage.reserve.side_effect = lambda *a, **k: sequence.append('reserve')
        usage.issued.side_effect = lambda *a: sequence.append('issue')
        output, proof, raw = analyzer.analyze_reading(
            source, text, images, None, lambda key: sequence.append('durable'), pack=packs[0])
        self.assertEqual(sequence, ['reserve', 'durable', 'issue'])
        self.assertTrue(output[0]['reading']['complete'])
        self.assertIs(raw, value)
        self.assertIs(analyzer.last_reading, value)
        self.assertEqual(proof['resultHash'], facts.digest(value))
        client.structured.assert_called_once()
        usage.reserve.assert_called_once()
        self.assertEqual(usage.finish.call_args.args[1], 'events')
        self.assertEqual(client.structured.call_args.args[1], azure.READING_SCHEMA)
        self.assertEqual(client.structured.call_args.kwargs['max_completion_tokens'], 8192)

    def test_request_hash_is_exact_deterministic_transport_payload(self):
        source, text, images = fixture(photos=2)
        pack = azure.reading_packs(source, text, images)[0]
        messages, proof = azure.prepare_reading_request(source, text, images, pack)
        repeated = azure.prepare_reading_request(source, text, images, copy.deepcopy(pack))
        self.assertEqual((messages, proof), repeated)
        payload = azure.transport.request_payload(messages, azure.READING_SCHEMA,
                                                  name='half_month_schedule', max_completion_tokens=8192)
        wire = json.dumps(payload).encode('utf-8')
        self.assertEqual(proof['requestHash'], facts.digest(wire))
        self.assertLessEqual(len(wire), pack['wireBytes'])
        self.assertLessEqual(pack['wireBytes'], azure.MAX_REQUEST_BYTES)
        self.assertEqual(payload['model'], 'gpt-5.6-luna')
        self.assertEqual(len(messages[1]['content']), 3)
        context = json.loads(messages[1]['content'][0]['text'])
        self.assertEqual(context['body'], text)
        self.assertEqual(context['imageMap'], pack['imageMap'])

    def test_original_pixel_and_byte_aggregate_limits_split_real_synthetic_images(self):
        from PIL import Image
        source, text, _ = fixture(photos=3)
        with Image.new('RGB', (4000, 3500)) as image:
            stream = io.BytesIO()
            image.save(stream, format='PNG')
        images = [{'bytes': stream.getvalue(), 'mime': 'image/png'} for _ in range(3)]
        packs = azure.reading_packs(source, text, images)
        self.assertEqual([pack['parts'] for pack in packs], [2, 2])
        self.assertEqual([len(pack['images']) for pack in packs], [2, 1])
        for pack in packs:
            self.assertLessEqual(pack['pixels'], 40_000_000)
            self.assertLessEqual(pack['attachmentBytes'], 12 * 1024 * 1024)
            messages, proof = azure.prepare_reading_request(source, text, images, pack)
            self.assertEqual(len(proof['images']), 3)
            self.assertLessEqual(len(json.dumps(azure.wire_payload(messages, azure.READING_VERSION)).encode()),
                                 pack['wireBytes'])
        source, text, _ = fixture(photos=2)
        payload = b'ruSt' + b'x' * (7 * 1024 * 1024)
        chunk = struct.pack('>I', len(payload) - 4) + payload + struct.pack(
            '>I', zlib.crc32(payload) & 0xffffffff)
        raw = base.png()[:-12] + chunk + base.png()[-12:]
        images = [{'bytes': raw, 'mime': 'image/png'} for _ in range(2)]
        packs = azure.reading_packs(source, text, images)
        self.assertEqual([len(pack['images']) for pack in packs], [1, 1])
        self.assertTrue(all(pack['wireBytes'] <= 17 * 1024 * 1024 for pack in packs))

    def test_four_zooms_never_append_originals_and_map_original_coordinates(self):
        source, text, images = fixture(photos=2)
        value = result(row(index=1, box=[.4, .4, .8, .8]), complete=False, indexes=(1,))
        packs = azure.reading_packs(source, text, images, stage='detail', previous=value)
        self.assertGreater(len(packs), 1)
        ids = []
        for pack in packs:
            self.assertLessEqual(len(pack['images']), 4)
            self.assertTrue(all(image['kind'] == 'crop' for image in pack['images']))
            messages, _ = azure.prepare_reading_request(source, text, images, pack)
            self.assertEqual(len(messages[1]['content']), 1 + len(pack['images']))
            ids.extend(image['variantId'] for image in pack['images'])
        self.assertEqual(azure.reading_packs(source, text, images, stage='detail', previous=value, seen=ids), [])
        pack = next(pack for pack in packs if any(i['originalIndex'] == 1 for i in pack['images']))
        analyzer, _, _ = self.analyzer(value)
        schedules, _, raw = analyzer.analyze_reading(source, text, images, None, lambda key: None, pack=pack)
        detail = schedules[0]['reading']['days']['2026-09-05']
        self.assertEqual(detail['evidence']['imageIndex'], 1)
        self.assertEqual(detail['evidence']['box'], [.4, .4, .8, .8])
        self.assertEqual(raw, value)

    def test_partitions_remain_partial_and_missing_saved_pack_is_ambiguous(self):
        source, text, images = fixture(photos=2)
        with mock.patch.object(azure.reading, 'MAX_POST_PIXELS', 108):
            packs = azure.reading_packs(source, text, images)
            self.assertEqual(len(packs), 2)
            with self.assertRaisesRegex(ValueError, 'reading_pack_required'):
                azure.saved_result(source, text, images, result(), now=base.NOW,
                                   receipt_id='a' * 64, contract_version=azure.READING_VERSION)
        analyzer, _, client = self.analyzer(None)
        for index, pack in enumerate(packs):
            value = result(row(index=index), indexes=(index,))
            client.structured.return_value = value
            schedules, proof, raw = analyzer.analyze_reading(
                source, text, images, None, lambda key: None, pack=pack)
            self.assertFalse(schedules[0]['reading']['complete'])
            self.assertTrue(raw['complete'])
            replay, replay_proof = azure.saved_result(
                source, text, images, value, now=base.NOW, receipt_id=proof['receiptId'],
                contract_version=azure.READING_VERSION, pack=pack)
            self.assertEqual((replay, replay_proof), (schedules, proof))
        self.assertEqual(client.structured.call_count, 2)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'budget_exhausted'):
            analyzer.check()

    def test_saved_single_original_replays_without_live_client(self):
        source, text, images = fixture()
        value = result()
        before = copy.deepcopy(value)
        schedules, proof = azure.saved_result(source, text, images, value, now=base.NOW,
                                              receipt_id='b' * 64, contract_version=azure.READING_VERSION)
        self.assertTrue(schedules[0]['reading']['complete'])
        self.assertEqual(value, before)
        facts.validate_analysis(proof)

    def test_actual_partial_adapter_output_applies_without_raw_canonical_storage(self):
        source, text, images = fixture()
        value = result(row(shifts=None, transcription='5日 読めないシフト'), complete=False)
        schedules, proof = azure.saved_result(source, text, images, value, now=base.NOW,
                                              receipt_id='c' * 64, contract_version=azure.READING_VERSION)
        state = facts.empty_state()
        facts.apply_reading_revision(state, schedules, source, proof)
        facts.validate_state(state)
        self.assertEqual(state['schedules'], [])
        self.assertEqual(state['partialSchedules'], schedules)
        encoded = json.dumps(state, ensure_ascii=False)
        self.assertNotIn('5日 読めないシフト', encoded)
        self.assertNotIn('"classification"', encoded)
        self.assertNotIn('"transcription"', encoded)
        self.assertIn('"transcriptionHash"', encoded)

    def test_actual_reply_adapter_output_applies_as_partial_operation(self):
        source, text, images = fixture(photos=0, text='9月前半 5日 追加', reply=True)
        value = result(row(index=None, operation='add', transcription='5日 追加'), indexes=())
        schedules, proof = azure.saved_result(source, text, images, value, now=base.NOW,
                                              receipt_id='d' * 64, contract_version=azure.READING_VERSION)
        state = facts.empty_state()
        facts.apply_reading_revision(state, schedules, source, proof)
        facts.validate_state(state)
        self.assertEqual(state['schedules'], [])
        self.assertEqual(state['partialSchedules'][0]['reading']['days']['2026-09-05']['operation'], 'add')

    def test_text_only_reading_and_negative_each_use_one_request(self):
        for value, text in [(result(row(index=None), indexes=()), '9月前半 5日 昼'),
                            ({'periods': [], 'classification': 'non_schedule', 'complete': True}, '自撮り')]:
            source, text, images = fixture(photos=0, text=text)
            pack = azure.reading_packs(source, text, images)[0]
            self.assertEqual(pack['images'], [])
            analyzer, usage, client = self.analyzer(value)
            schedules, proof, raw = analyzer.analyze_reading(
                source, text, images, None, lambda key: None, pack=pack)
            client.structured.assert_called_once()
            self.assertEqual(usage.finish.call_args.args[1], 'events' if schedules else 'no_event')
            self.assertEqual(proof['images'], [])
            self.assertEqual(raw, value)

    def test_interruption_consumes_reservation_and_preserves_durable_hash(self):
        source, text, images = fixture()
        pack = azure.reading_packs(source, text, images)[0]
        analyzer, usage, client = self.analyzer(result())
        hashes = []
        def stop_after_persist(key):
            hashes.append(key)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            analyzer.analyze_reading(source, text, images, None, stop_after_persist, pack=pack)
        self.assertEqual(hashes, [azure.prepare_reading_request(source, text, images, pack)[1]['requestHash']])
        usage.reserve.assert_called_once()
        usage.issued.assert_not_called()
        client.structured.assert_not_called()

    def test_invalid_result_retained_privately_without_retry(self):
        source, text, images = fixture()
        analyzer, usage, client = self.analyzer({'private': 'unreadable raw response'})
        pack = azure.reading_packs(source, text, images)[0]
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            analyzer.analyze_reading(source, text, images, None, lambda key: None, pack=pack)
        self.assertEqual(analyzer.last_reading, {'private': 'unreadable raw response'})
        client.structured.assert_called_once()
        self.assertEqual(usage.finish.call_args.args[1], 'azure_invalid_output')

    def test_runtime_registry_and_reservation_stop_before_http(self):
        source, text, images = fixture()
        pack = azure.reading_packs(source, text, images)[0]
        for boundary in ('check', 'reserve', 'durable', 'issued'):
            analyzer, usage, client = self.analyzer(result())
            issued = mock.Mock()
            error = azure.AnalysisFailure('azure_deadline')
            if boundary == 'durable':
                issued.side_effect = error
            else:
                getattr(usage, boundary).side_effect = error
            with self.subTest(boundary=boundary), self.assertRaises(azure.AnalysisFailure):
                analyzer.analyze_reading(source, text, images, None, issued, pack=pack)
            client.structured.assert_not_called()
            self.assertIsNone(analyzer.last_reading)
            if boundary in ('check', 'reserve'):
                issued.assert_not_called()
        analyzer, usage, client = self.analyzer(result())
        analyzer.registry_guard = mock.Mock()
        analyzer.registry_guard.check.side_effect = ValueError('registry_changed')
        with self.assertRaises(azure.RegistryFailure):
            analyzer.analyze_reading(source, text, images, None, mock.Mock(), pack=pack)
        usage.reserve.assert_not_called()
        client.structured.assert_not_called()

    def test_bad_views_counts_original_binding_and_wire_limit_rejected(self):
        source, text, images = fixture()
        pack = azure.reading_packs(source, text, images)[0]
        for change in ('hash', 'map', 'index', 'size', 'count', 'text', 'parts', 'bytes'):
            bad = copy.deepcopy(pack)
            if change == 'hash':
                bad['images'][0]['variantHash'] = 'c' * 64
            elif change == 'map':
                bad['imageMap'] = []
            elif change == 'index':
                bad['images'][0]['originalIndex'] = 1
            elif change == 'size':
                bad['images'][0]['originalSize'] = [8, 12]
            elif change == 'count':
                bad['images'] *= 5
            elif change == 'text':
                bad['text'] += ' changed'
            elif change == 'parts':
                bad['parts'] = 0
            else:
                bad['images'][0]['bytes'] = b'x' * (azure.MAX_IMAGE_BYTES + 1)
            with self.subTest(change=change), self.assertRaises(ValueError):
                azure.prepare_reading_request(source, text, images, bad)
        with mock.patch.object(azure, 'MAX_REQUEST_BYTES', 100):
            with self.assertRaisesRegex(ValueError, 'azure_input_limit'):
                azure.prepare_reading_request(source, text, images, pack)
        with self.assertRaises(ValueError):
            azure.reading_packs(source, text, images, stage='fallback-model')

    def test_response_cannot_cite_an_original_missing_from_this_pack(self):
        source, text, images = fixture(photos=2)
        with mock.patch.object(azure.reading, 'MAX_POST_PIXELS', 108):
            pack = azure.reading_packs(source, text, images)[0]
        analyzer, _, _ = self.analyzer(result(row(index=1), indexes=(1,)))
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            analyzer.analyze_reading(source, text, images, None, lambda key: None, pack=pack)


if __name__ == '__main__':
    unittest.main()
