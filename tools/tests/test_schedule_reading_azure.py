"""Synthetic v3 adapter regressions. All model and network calls are mocked."""
import copy
import datetime as dt
import io
import json
from pathlib import Path
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


def row(day=5, shifts=('昼',), *, weekday='土', transcription=None, qualifier=None,
        start=None, end=None, index=0, box=None, operation=None):
    if transcription is None:
        # A row's transcription is its own printed line: the date and its 昼/夜 words.
        transcription = f'{day}日 ' + (''.join(shifts) if shifts else 'にゃ?')
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
            row(7, None, weekday='月', transcription='7日 にゃ?')))
        self.assertFalse(complete)
        self.assertEqual([item['shifts'] for item in schedules[0]['days']], [[], []])
        details = schedules[0]['reading']['days']
        self.assertEqual(details['2026-09-05']['shiftStatus'], 'unstated')
        self.assertEqual(details['2026-09-07']['shiftStatus'], 'unreadable')
        self.assertFalse(schedules[0]['reading']['complete'])
        # A date whose own cell is only "unreadable" is no evidence of work and is not saved.
        schedules, complete = self.normalize(result(
            row(shifts=(), transcription='5日'), row(7, None, weekday='月', transcription='7日 判読不能')))
        self.assertEqual(([item['date'] for item in schedules[0]['days']], complete), (['2026-09-05'], False))
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
             ['昼'], '13:00', '17:00'),
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

    def test_printed_clocks_supply_missing_shifts_and_words_win_conflicts(self):
        cases = [
            # (shifts, qualifier, transcription, start, end) -> (shifts, shiftStatus, qualifier)
            (((), None, '5日 12:00〜17:00', '12:00', '17:00'), (['昼'], 'clock-derived', None)),
            (((), None, '5日 12:00〜18:00', '12:00', '18:00'), (['昼'], 'clock-derived', None)),
            (((), None, '5日 16:00〜22:00', '16:00', '22:00'), (['夜'], 'clock-derived', None)),
            (((), None, '5日 17:30〜22:00', '17:30', '22:00'), (['夜'], 'clock-derived', None)),
            (((), None, '5日 12:00〜22:00', '12:00', '22:00'), (['昼', '夜'], 'clock-derived', None)),
            # Clocks that match neither store window do not invent a shift.
            (((), None, '5日 18:00〜20:00', '18:00', '20:00'), ([], 'unstated', None)),
            # One printed bound is not a pair.
            (((), None, '5日 17:00', None, '17:00'), ([], 'unstated', None)),
            # Words win over contradicting clocks and the contradiction stays recorded.
            ((('昼',), None, '5日 昼 17:00〜22:00', '17:00', '22:00'), (['昼'], 'stated-conflict', None)),
            ((('昼', '夜'), 'all_day', '5日 おーらす 12-17 12:00〜17:00', '12:00', '17:00'),
             (['昼', '夜'], 'stated-conflict', 'all_day')),
            ((('夜',), None, '5日 夜 17:00〜22:00', '17:00', '22:00'), (['夜'], 'stated', None)),
            # A qualifier word without clocks keeps its existing rule and never gains clock shifts.
            (((), 'late', '5日 おそめ', None, None), (['夜'], 'stated', 'late')),
            # Neither words nor clocks: no shift.
            (((), None, '5日', None, None), ([], 'unstated', None)),
        ]
        for (shifts, qualifier, text, start, end), (expected, status, kept) in cases:
            with self.subTest(text=text):
                schedules, _ = self.normalize(result(row(
                    shifts=shifts, qualifier=qualifier, transcription=text, start=start, end=end)))
                fact = schedules[0]['reading']['days']['2026-09-05']
                self.assertEqual(sorted(schedules[0]['days'][0]['shifts']), sorted(expected))
                self.assertEqual((fact['shiftStatus'], fact['qualifier']), (status, kept))
                facts.validate_reading(schedules[0])

    def test_wordless_all_day_is_stored_only_when_printed_clocks_span_both_shifts(self):
        schedules, complete = self.normalize(result(row(
            shifts=('昼', '夜'), qualifier='all_day', transcription='5日 12:00〜22:00',
            start='12:00', end='22:00')))
        fact = schedules[0]['reading']['days']['2026-09-05']
        self.assertTrue(complete)
        self.assertEqual(schedules[0]['days'][0]['shifts'], ['昼', '夜'])
        # Without a printed 昼/夜 word the shifts come from the printed clocks (PR84 rule).
        self.assertEqual((fact['qualifier'], fact['shiftStatus']), (None, 'clock-derived'))
        for item in [row(shifts=('昼', '夜'), qualifier='all_day', transcription='5日 12:00〜17:00',
                         start='12:00', end='17:00'),
                     row(shifts=('昼', '夜'), qualifier='all_day', transcription='5日 昼夜')]:
            with self.subTest(item=item):
                self.assertEqual(self.normalize(result(item)), ([], False))

    def test_public_reading_rejects_forged_clock_shift_status(self):
        schedules, _ = self.normalize(result(row(
            shifts=(), transcription='5日 17:00〜22:00', start='17:00', end='22:00')))
        schedule = schedules[0]
        facts.validate_reading(schedule)
        fact = schedule['reading']['days']['2026-09-05']
        for forge in [lambda: schedule['days'][0].update(shifts=['昼']),
                      lambda: fact.update(hours={}),
                      lambda: fact.update(shiftStatus='stated-conflict'),
                      lambda: fact.update(qualifier='late')]:
            with self.subTest(forge=forge):
                copy_schedule = copy.deepcopy(schedule)
                saved = (schedule['days'][0]['shifts'], copy.deepcopy(fact))
                forge()
                with self.assertRaises(ValueError):
                    facts.validate_reading(schedule)
                schedule['days'][0]['shifts'] = saved[0]
                fact.clear()
                fact.update(saved[1])
                self.assertEqual(schedule, copy_schedule)

    def test_reading_and_public_clock_rules_agree(self):
        clocks = [f'{hour:02d}:{minute:02d}' for hour in range(10, 24) for minute in (0, 30)]
        for start in clocks:
            for end in clocks:
                self.assertEqual(azure.reading.clock_shifts(start, end), facts.clock_shifts(start, end))

    def test_explicit_clocks_and_qualifiers_must_be_in_row_transcription(self):
        # Unprinted clocks are removed from the row; an unprinted qualifier or an invalid
        # clock drops the row and leaves the table partial. Nothing ungrounded is saved.
        for item, saved in [(row(start='13:00'), True), (row(qualifier='long'), False),
                            (row(end='24:00', transcription='5日 24:00'), False),
                            (row(start='13:00', transcription='5日 113:00'), True)]:
            with self.subTest(item=item):
                schedules, complete = self.normalize(result(item))
                if not saved:
                    self.assertEqual((schedules, complete), ([], False))
                    continue
                detail = schedules[0]['reading']['days']['2026-09-05']
                self.assertEqual((detail['hours'], detail['qualifier']), ({}, None))
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
        # A body row whose line is not in the author's text is dropped alone, never saved.
        self.assertEqual(self.normalize(result(item, indexes=()), context), ([], False))
        grounded = row(7, weekday='月', index=None, transcription='7日 昼')
        context = fixture(photos=0, text='9月前半 7日 昼')
        schedules, complete = self.normalize(result(grounded, item, indexes=()), context)
        self.assertFalse(complete)
        self.assertEqual([day['date'] for day in schedules[0]['days']], ['2026-09-07'])

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
        # An unprinted timing clock is dropped alone; the printed row itself is kept.
        item['workTiming'][0]['time'] = '18:00'
        schedules, _ = self.normalize(result(item))
        self.assertNotIn('workTiming', schedules[0])
        self.assertEqual(schedules[0]['reading']['days']['2026-09-05']['hours']['end'],
                         {'time': '17:00', 'basis': 'explicit'})
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


class TranscriptionGroundingTests(base.Offline):
    """Rows follow their own printed line; one bad row never rejects or contaminates a table."""
    normalize = NormalizationTests.normalize

    def dates(self, value, context=None):
        schedules, complete = self.normalize(value, context)
        return [item['date'] for schedule in schedules for item in schedule['days']], complete

    def test_row_shifts_and_clocks_come_from_its_transcription(self):
        schedules, _ = self.normalize(result(row(shifts=('夜',), transcription='5日 昼')))
        self.assertEqual(schedules[0]['days'][0]['shifts'], ['昼'])
        # A model 昼 without a printed word is not evidence; printed clocks decide (PR84 rule).
        schedules, _ = self.normalize(result(row(shifts=('昼',), transcription='5日 17-22')))
        detail = schedules[0]['reading']['days']['2026-09-05']
        self.assertEqual(schedules[0]['days'][0]['shifts'], ['夜'])
        self.assertEqual((detail['shiftStatus'], detail['hours']['start'], detail['hours']['end']),
                         ('clock-derived', {'time': '17:00', 'basis': 'explicit'},
                          {'time': '22:00', 'basis': 'explicit'}))
        # Neither a word nor a clock: still no shift.
        schedules, _ = self.normalize(result(row(shifts=('昼',), transcription='5日 おきゅ')))
        self.assertEqual(schedules[0]['days'][0]['shifts'], [])

    def test_decimal_half_hour_is_an_explicit_clock(self):
        schedules, _ = self.normalize(result(row(
            shifts=('夜',), qualifier='late', transcription='5日 おそめよる 17.5-22')))
        hours = schedules[0]['reading']['days']['2026-09-05']['hours']
        self.assertEqual((hours['start'], hours['end']), ({'time': '17:30', 'basis': 'explicit'},
                                                          {'time': '22:00', 'basis': 'explicit'}))

    def test_row_whose_transcription_names_another_date_is_dropped_alone(self):
        self.assertEqual(self.dates(result(row(), row(7, weekday='月', transcription='8日 昼'))),
                         (['2026-09-05'], False))
        self.assertEqual(self.dates(result(row(), row(7, weekday='月', transcription=''))),
                         (['2026-09-05'], False))

    def test_printed_weekday_majority_drops_only_the_contradicting_row(self):
        value = result(row(), row(7, weekday='月', transcription='7日(月) 昼'),
                       row(8, weekday='火', transcription='8日(火) 昼'),
                       row(9, weekday='水', transcription='9日(月) 昼'))
        self.assertEqual(self.dates(value), (['2026-09-05', '2026-09-07', '2026-09-08'], False))
        # Without a majority the calendar stays unresolved, as before.
        with self.assertRaisesRegex(ValueError, 'calendar'):
            self.normalize(result(row(transcription='5日(日) 昼', weekday='日')))

    def test_unprinted_body_year_is_ignored_not_fatal(self):
        self.assertEqual(self.dates(result(row(), text_year=2025)), (['2026-09-05'], True))
        context = fixture(text='2025年9月前半のお給仕予定')
        self.assertEqual(self.dates(result(row(weekday=None), text_year=2025), context),
                         (['2025-09-05'], True))

    def test_missing_or_contradicted_half_is_decided_by_day_and_partial(self):
        late = row(20, weekday='日', transcription='20日 昼')
        self.assertEqual(self.dates(result(row(), late, half=None)),
                         (['2026-09-05', '2026-09-20'], False))
        self.assertEqual(self.dates(result(row(), copy.deepcopy(late), half='first')),
                         (['2026-09-05'], False))

    def test_invalid_or_foreign_timing_items_are_dropped_alone(self):
        item = row(transcription='5日 昼 12-17', start='12:00', end='17:00')
        item['workTiming'] = [{'shift': '夜', 'kind': 'time', 'time': '17:00'},
                              {'shift': '昼', 'kind': 'time', 'time': None}]
        schedules, _ = self.normalize(result(item))
        self.assertNotIn('workTiming', schedules[0])
        self.assertEqual(schedules[0]['days'][0]['shifts'], ['昼'])


class SavedReadingBenchmarkTests(base.Offline):
    """API0 re-normalization of five real-shaped readings (synthetic wording, local truth)."""
    FIXTURE = Path(__file__).with_name('fixtures') / 'half_month_reading_benchmark.json'

    def score(self):
        posts = json.loads(self.FIXTURE.read_text(encoding='utf-8'))['posts']
        shift_names = {'day': ['昼'], 'night': ['夜'], 'all': ['昼', '夜']}
        results = {}
        for post in posts:
            source, text, images = fixture(photos=post['photos'])
            value = {'classification': post['classification'], 'complete': post['complete'], 'periods': [
                {'month': period['month'], 'half': period['half'], 'printedYear': None, 'textYear': None,
                 'imageIndexes': period['imageIndexes'], 'days': [
                     {**{key: item[key] for key in ('day', 'weekday', 'shifts', 'transcription', 'qualifier',
                                                    'explicitStart', 'explicitEnd')},
                      'workTiming': [], 'operation': None,
                      'evidence': {'imageIndex': item['imageIndex'], 'box': None}}
                     for item in period['days']]} for period in post['periods']]}
            metadata = [azure.probe_image(image['bytes'], image['mime']) for image in images]
            try:
                schedules, _ = azure.normalize_reading(value, source, text, len(images), None,
                                                       image_metadata=metadata)
            except ValueError as exc:
                results[post['post']] = {'rejected': str(exc), 'full': 0, 'outside': []}
                continue
            saved = {}
            for schedule in schedules:
                facts.validate_reading(schedule)
                for item in schedule['days']:
                    hours = schedule['reading']['days'][item['date']]['hours']
                    clock = tuple(hours[key]['time'] if hours.get(key, {}).get('basis') == 'explicit' else None
                                  for key in ('start', 'end'))
                    saved[int(item['date'][-2:])] = (sorted(item['shifts']), clock)
            full = 0
            for day, shift, start, end in post['truth']:
                if day not in saved:
                    continue
                shifts, clock = saved[day]
                accepted = [sorted(shift_names[shift])] + [
                    sorted(sum((shift_names[name] for name in names), [])) for d, names in post['alternatives']
                    if d == day]
                full += shifts in accepted and clock == (start, end)
            truth_days = {day for day, *_ in post['truth']}
            results[post['post']] = {'rejected': None, 'full': full, 'outside': sorted(set(saved) - truth_days)}
        return results

    def test_saved_reading_rules_recover_clock_backed_days_without_new_inference(self):
        results = self.score()
        self.assertEqual(sum(item['full'] for item in results.values()), 31)
        self.assertEqual([item['outside'] for item in results.values()], [[]] * 5)
        # An all-day word absent from its own row no longer rejects the whole post: only
        # those rows are dropped, the remaining printed dates are kept (still never fabricated).
        self.assertEqual({post: item['rejected'] for post, item in results.items() if item['rejected']}, {})
        # P5 is the clock-only table that the previous rule rejected outright (21/51 before).
        self.assertEqual(results['P5']['full'], 10)
        self.assertEqual(sum(item['full'] for post, item in results.items() if post != 'P5'), 21)


if __name__ == '__main__':
    unittest.main()
