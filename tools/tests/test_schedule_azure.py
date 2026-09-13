"""Mock-only stable-purpose/calendar/image regressions; no real model result."""
import copy
import datetime as dt
import io
import json
from unittest import mock
import unittest

import test_half_month_schedules as base


facts, azure = base.facts, base.azure


def http_envelope(value, *, indent=None, ensure_ascii=False):
    options = {'ensure_ascii': ensure_ascii, 'indent': indent}
    if indent is None:
        options['separators'] = (',', ':')
    return json.dumps({
        'id': 'chatcmpl-SYNTHETIC-CAPACITY-NOT-A-LIVE-RESULT', 'object': 'chat.completion',
        'created': int(base.NOW.timestamp()), 'model': facts.MODEL + '-' + facts.MODEL_VERSION,
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': json.dumps(value, **options),
                                 'refusal': None}}],
        'usage': {'prompt_tokens': 6000, 'completion_tokens': 6000, 'total_tokens': 12000},
    }, **options).encode('utf-8')


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

    def test_synthetic_six_core_gold_word_only_long_is_two_facets_not_invented_hours(self):
        value = base.timing_result({5: [base.work_note()], 12: [base.work_note()]})
        schedules = self.normalize(value)
        self.assertEqual(schedules[0]['days'], self.normalize(base.result())[0]['days'])
        self.assertEqual([(note['serviceDate'], note['shift'], note['boundary'],
                          note['qualifier'], note['explicitTime'])
                          for note in schedules[0]['workTiming']['facts']],
                         [('2026-09-05', '昼', 'end', 'long', None),
                          ('2026-09-12', '昼', 'end', 'long', None)])

    def test_all_four_explicit_words_and_nonmapped_display_clocks(self):
        for shift, boundary, qualifier, clock in [
                ('昼', 'end', 'short', '16:00'), ('昼', 'end', 'long', '18:00'),
                ('夜', 'start', 'early', '16:00'), ('夜', 'start', 'late', '18:00'),
                ('昼', 'end', None, '15:00'), ('昼', 'end', None, '17:00'),
                ('昼', 'end', None, '19:00'), ('夜', 'start', None, '17:00')]:
            value = base.result([(5, '土', [shift])])
            value['periods'][0]['days'][0]['workTiming'] = [
                base.work_note(shift, boundary, qualifier, clock)]
            with self.subTest(shift=shift, boundary=boundary, clock=clock):
                note = self.normalize(value)[0]['workTiming']['facts'][0]
                self.assertEqual((note['qualifier'], note['explicitTime']), (qualifier, clock))

    def test_numeric_only_never_invents_a_source_word(self):
        for shift, boundary, clock in [('昼', 'end', '16:00'), ('昼', 'end', '18:00'),
                                        ('夜', 'start', '16:00'), ('夜', 'start', '18:00')]:
            value = base.result([(5, '土', [shift])])
            value['periods'][0]['days'][0]['workTiming'] = [
                {'shift': shift, 'kind': 'time', 'time': clock}]
            fact = self.normalize(value)[0]['workTiming']['facts'][0]
            self.assertEqual((fact['shift'], fact['boundary'], fact['qualifier'], fact['explicitTime']),
                             (shift, boundary, None, clock))

    def test_compact_wire_requests_only_display_boundaries_and_keeps_words_clockless(self):
        self.assertEqual(azure.TIMING_SCHEMA, facts.timing().COMPACT_SCHEMA)
        self.assertEqual(set(azure.TIMING_SCHEMA['required']), {'shift', 'kind', 'time'})
        self.assertEqual(azure.DAY_SCHEMA['properties']['workTiming']['maxItems'], 2)
        self.assertIn('Do NOT extract daytime starts, nighttime ends', azure.PROMPT)
        for shift, kind in [('昼', 'short'), ('昼', 'long'), ('夜', 'early'), ('夜', 'late')]:
            value = base.result([(5, '土', [shift])])
            value['periods'][0]['days'][0]['workTiming'] = [{'shift': shift, 'kind': kind, 'time': None}]
            fact = self.normalize(value)[0]['workTiming']['facts'][0]
            self.assertEqual(fact['qualifier'], kind)
            self.assertIsNone(fact['explicitTime'])
        for shift, boundary in [('昼', 'start'), ('夜', 'end')]:
            value = base.result([(5, '土', [shift])])
            value['periods'][0]['days'][0]['workTiming'] = [
                {'shift': shift, 'kind': 'time', 'time': '16:00', 'boundary': boundary}]
            with self.assertRaises(ValueError):
                self.normalize(value)

    def test_targeted_denials_keep_exact_word_or_clock_target(self):
        for shift, kind, word, clock in [
                ('昼', 'not-short', 'short', None), ('昼', 'not-long', 'long', None),
                ('夜', 'not-early', 'early', None), ('夜', 'not-late', 'late', None),
                ('昼', 'not-time', None, '17:00')]:
            value = base.result([(5, '土', [shift])])
            value['periods'][0]['days'][0]['workTiming'] = [{'shift': shift, 'kind': kind, 'time': clock}]
            fact = self.normalize(value)[0]['workTiming']['facts'][0]
            self.assertEqual((fact['status'], fact['qualifier'], fact['explicitTime']),
                             ('excluded', word, clock))
        for note in [
                {'shift': '昼', 'kind': 'not-long', 'time': '18:00'},
                {'shift': '昼', 'kind': 'not-time', 'time': None},
                {'shift': '昼', 'kind': 'not-early', 'time': None},
                {'shift': '昼', 'kind': 'not-usual', 'time': None}]:
            value = base.timing_result({5: [note]})
            with self.subTest(note=note), self.assertRaises(ValueError):
                self.normalize(value)

    def test_same_shift_distinct_targets_are_valid_but_duplicate_fact_keys_are_not(self):
        def note(kind, clock=None):
            return {'shift': '夜', 'kind': kind, 'time': clock}
        for notes in [
                [note('late', '18:00'), note('not-early')],
                [note('not-early'), note('not-late')],
                [note('not-time', '16:00'), note('not-time', '17:00')]]:
            value = base.result([(5, '土', ['夜'])])
            value['periods'][0]['days'][0]['workTiming'] = notes
            parsed = self.normalize(value)[0]['workTiming']['facts']
            self.assertEqual(len(parsed), 2)
            self.assertEqual(len({facts.timing().scope(fact) for fact in parsed}), 1)
            self.assertEqual(len({facts.timing().fact_key(fact) for fact in parsed}), 2)
        for notes in [
                [note('not-early'), note('not-early')],
                [note('not-time', '16:00'), note('not-time', '16:00')],
                [note('late'), note('time', '18:00')],
                [note('withdrawn'), note('conflict')]]:
            value = base.result([(5, '土', ['夜'])])
            value['periods'][0]['days'][0]['workTiming'] = notes
            with self.subTest(notes=notes), self.assertRaisesRegex(ValueError, 'timing_shift'):
                self.normalize(value)

    def test_normal_usual_no_info_is_not_a_withdrawal_or_inferred_hour_contract(self):
        self.assertIn('Normal/usual/no-info alone does NOT withdraw a boundary or imply any hour.', azure.PROMPT)
        self.assertIn('followed by "not early" must retain late', azure.PROMPT)
        for body in ('通常です', 'いつもどおり', 'normal', 'usual', 'no-info'):
            for channel in (None, []):
                value = base.result([(5, '土', ['昼'])], images=False)
                value['periods'][0]['days'][0]['workTiming'] = channel
                parsed = self.normalize(value, text=body, count=0)[0]
                self.assertEqual(parsed['days'], [{'date': '2026-09-05', 'shifts': ['昼']}])
                self.assertNotIn('workTiming', parsed)
            value['periods'][0]['days'][0]['workTiming'] = [
                {'shift': '昼', 'kind': 'time', 'time': '18:00'}]
            with self.assertRaisesRegex(ValueError, 'clock_ungrounded'):
                self.normalize(value, text=body, count=0)
        for kind in ('normal', 'usual', 'no-info'):
            value = base.timing_result({5: [{'shift': '昼', 'kind': kind, 'time': None}]})
            with self.assertRaises(ValueError):
                self.normalize(value)

    def test_required_nullable_timing_independent_of_confirmed_core(self):
        value = base.result()
        value['periods'][0]['days'][0]['workTiming'] = None
        schedules = self.normalize(value)
        self.assertEqual(len(schedules[0]['days']), 6)
        self.assertNotIn('workTiming', schedules[0])
        for field in ('workTiming', 'shifts'):
            invalid = copy.deepcopy(value)
            del invalid['periods'][0]['days'][0][field]
            with self.assertRaises(ValueError):
                self.normalize(invalid)
        for word in ('お昼寝', '休憩', 'オーラス', '投稿は18:00', '翌日だけのお知らせ'):
            self.assertNotIn('workTiming', self.normalize(base.result(), text=word)[0])

    def test_timing_row_image_scope_boundary_status_and_unknown_are_strict(self):
        for change in ('shift', 'boundary', 'status', 'image', 'emptyimage', 'duplicateimage',
                       'day', 'name', 'store', 'clock', 'unknown', 'empty', 'duplicate', 'notarray',
                       'kind', 'missing', 'three', 'withdrawnClock'):
            value = base.timing_result({5: [base.work_note()]})
            row = value['periods'][0]['days'][1]
            note = row['workTiming'][0]
            if change == 'shift':
                note['shift'] = '夜'
            elif change == 'boundary':
                note['boundary'] = 'start'
            elif change == 'status':
                note['status'] = 'withdrawn'
            elif change == 'image':
                note['imageIndexes'] = [1]
            elif change == 'emptyimage':
                value['periods'][0]['imageIndexes'] = []
            elif change == 'duplicateimage':
                note['imageIndexes'] = [0, 0]
            elif change == 'day':
                note['serviceDate'] = '2026-09-06'
            elif change in ('name', 'store', 'unknown'):
                note[change] = 'not allowed'
            elif change == 'clock':
                note['time'] = '24:00'
            elif change == 'empty':
                note['kind'] = 'time'
            elif change == 'duplicate':
                row['workTiming'].append(copy.deepcopy(note))
            elif change == 'kind':
                note['kind'] = 'early'
            elif change == 'missing':
                del note['time']
            elif change == 'three':
                row['shifts'] = ['昼', '夜']
                row['workTiming'] += [
                    {'shift': '夜', 'kind': 'early', 'time': None},
                    {'shift': '夜', 'kind': 'withdrawn', 'time': None}]
            elif change == 'withdrawnClock':
                note.update(kind='withdrawn', time='16:00')
            else:
                row['workTiming'] = {}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.normalize(value)

    def test_text_clocks_are_in_body_not_source_posting_time(self):
        value = base.result([(5, '土', ['昼'])], images=False)
        value['periods'][0]['days'][0]['workTiming'] = [
            base.work_note(qualifier='short', explicit_time='16:00', images=False)]
        for text in ('5日 昼16時まで', '5日 昼16:00まで', '5日 昼１６時まで', '5日 昼11〜16'):
            self.assertEqual(self.normalize(value, text=text, count=0)[0]['workTiming']['facts'][0]
                             ['explicitTime'], '16:00')
        with self.assertRaisesRegex(ValueError, 'clock_ungrounded'):
            self.normalize(value, text='5日 昼のみ', count=0)
        value['periods'][0]['imageIndexes'] = [0]
        self.assertEqual(self.normalize(value, text='5日 昼16時まで', count=1)[0]
                         ['workTiming']['facts'][0]['explicitTime'], '16:00')

    def test_legacy_wire_is_explicit_and_does_not_mutate_v1_raw(self):
        value = base.result(contract_version=facts.LEGACY_VERSION)
        before = copy.deepcopy(value)
        source, body, _ = base.source()
        with self.assertRaises(ValueError):
            azure.normalize_result(value, source, body, 1)
        schedules = azure.normalize_result(value, source, body, 1, contract_version=facts.LEGACY_VERSION)
        self.assertEqual(value, before)
        self.assertNotIn('workTiming', schedules[0])
        with self.assertRaises(ValueError):
            azure.normalize_result(base.result(), source, body, 1, contract_version=facts.LEGACY_VERSION)
        messages, proof = azure.prepare_request(source, body, [{'bytes': base.png(), 'mime': 'image/png'}],
                                               contract_version=facts.LEGACY_VERSION)
        wire = azure.wire_payload(messages, facts.LEGACY_VERSION)
        self.assertEqual(proof['contract'], facts.LEGACY_VERSION)
        self.assertEqual(wire['max_completion_tokens'], 1200)
        self.assertEqual(proof['requestHash'], facts.digest(json.dumps(wire).encode('utf-8')))
        self.assertNotIn('workTiming', repr(azure.LEGACY_SCHEMA))

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

    def test_valid_other_half_timing_does_not_reject_target_half_and_is_not_persisted(self):
        verified, text, _ = base.source()
        value = base.result([(5, '土', ['昼']), (20, '日', ['夜'])], half='full')
        value['periods'][0]['days'][0]['workTiming'] = [base.work_note()]
        value['periods'][0]['days'][1]['workTiming'] = [
            base.work_note('夜', 'start', 'late', '18:00')]
        allowed = [('2026-09-01', '2026-09-15')]
        parsed = azure.normalize_result(value, verified, text, 1, allowed)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['days'], [{'date': '2026-09-05', 'shifts': ['昼']}])
        self.assertEqual([note['serviceDate'] for note in parsed[0]['workTiming']['facts']],
                         ['2026-09-05'])
        value['periods'][0]['days'][0]['workTiming'] = None
        parsed = azure.normalize_result(value, verified, text, 1, allowed)
        self.assertNotIn('workTiming', parsed[0])
        value['periods'][0]['days'][1]['workTiming'][0]['time'] = 'not-a-clock'
        with self.assertRaises(ValueError):
            azure.normalize_result(value, verified, text, 1, allowed)

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


class CapacityTests(base.Offline):
    def inputs(self, caption=None, photos=4):
        source, body, _ = base.source(photos=photos)
        if caption is not None:
            body = caption
            source['bodyHash'] = facts.digest(body.encode('utf-8'))
        images = [{'bytes': base.png(), 'mime': 'image/png'} for _ in range(photos)]
        return source, body, images

    def test_large_caption_is_held_before_reserve_issue_or_model_client(self):
        source, caption, images = self.inputs('x' * 6000)
        usage, client, issued = mock.Mock(), mock.Mock(), mock.Mock()
        analyzer = azure.AzureAnalyzer(usage, clock=lambda: base.NOW, client=client)
        with self.assertRaisesRegex(azure.capacity.CapacityHold, '^azure_capacity_hold$'):
            analyzer.analyze(source, caption, images, facts.target_periods(base.NOW), issued)
        usage.reserve.assert_not_called()
        usage.issued.assert_not_called()
        usage.finish.assert_not_called()
        issued.assert_not_called()
        client.structured.assert_not_called()
        self.assertEqual(analyzer.used, 0)
        with self.assertRaisesRegex(azure.capacity.CapacityHold, '^azure_capacity_hold$'):
            azure.check_caption_capacity(source, caption)

    def test_full_model_visible_metadata_is_checked_after_caption_lower_bound(self):
        source, caption, images = self.inputs('')
        messages, _ = azure.build_request(source, caption, images)
        report = azure.capacity.half_month(
            messages[1]['content'][0]['text'], azure.PROMPT, azure.SCHEMA, azure.MAX_OUTPUT_TOKENS)
        caption = 'x' * (azure.capacity.LIMIT - report['textReservationBound'] + 1)
        source['bodyHash'] = facts.digest(caption.encode('utf-8'))
        lower_bound = azure.check_caption_capacity(source, caption)
        self.assertLessEqual(lower_bound['textReservationBound'], azure.capacity.LIMIT)
        with self.assertRaisesRegex(azure.capacity.CapacityHold, '^azure_capacity_hold$'):
            azure.prepare_request(source, caption, images)

    def test_small_known_fixture_admitted_without_claiming_vision_or_service_tpm(self):
        source, caption, images = self.inputs()
        diagnostic = azure.build_request(source, caption, images)
        prepared = azure.prepare_request(source, caption, images)
        self.assertEqual(prepared, diagnostic)
        report = azure.capacity.half_month(
            prepared[0][1]['content'][0]['text'], azure.PROMPT, azure.SCHEMA, azure.MAX_OUTPUT_TOKENS)
        self.assertLessEqual(report['textReservationBound'], 10000)
        self.assertFalse(report['imageTokensIncluded'])
        self.assertFalse(report['serviceTpmGuaranteed'])
        self.assertEqual(azure.MAX_INPUT_BYTES, 6000)
        self.assertEqual(azure.MAX_OUTPUT_TOKENS, 3840)

    def test_stale_profile_fails_closed_before_issue_and_diagnostic_construction_is_pure(self):
        source, caption, images = self.inputs()
        usage, client, issued = mock.Mock(), mock.Mock(), mock.Mock()
        analyzer = azure.AzureAnalyzer(usage, clock=lambda: base.NOW, client=client)
        unchanged = azure.build_request(source, caption, images)
        with mock.patch.dict(azure.capacity.HALF_MONTH, {'schemaHash': 'f' * 64}):
            self.assertEqual(azure.build_request(source, caption, images), unchanged)
            with self.assertRaisesRegex(azure.capacity.CapacityHold, '^azure_capacity_profile_stale$'):
                analyzer.analyze(source, caption, images, facts.target_periods(base.NOW), issued)
        usage.reserve.assert_not_called()
        usage.issued.assert_not_called()
        usage.finish.assert_not_called()
        issued.assert_not_called()
        client.structured.assert_not_called()

    def test_legacy_v1_remains_byte_exact_and_is_not_subject_to_new_admission_policy(self):
        source, caption, images = self.inputs('x' * 6000)
        expected = azure.build_request(source, caption, images, contract_version=facts.LEGACY_VERSION)
        with mock.patch.object(azure.capacity, 'half_month', side_effect=AssertionError('v1 policy changed')):
            actual = azure.prepare_request(source, caption, images, contract_version=facts.LEGACY_VERSION)
            self.assertIsNone(azure.check_caption_capacity(source, caption, contract_version=facts.LEGACY_VERSION))
        self.assertEqual(actual, expected)
        wire = azure.wire_payload(actual[0], facts.LEGACY_VERSION)
        self.assertEqual(wire['max_completion_tokens'], 1200)
        self.assertEqual(facts.digest(json.dumps(wire).encode('utf-8')), actual[1]['requestHash'])

    def test_held_request_still_has_a_data_only_diagnostic_representation(self):
        source, caption, images = self.inputs('x' * 6000)
        with mock.patch.object(azure.transport, 'AzureOpenAI', side_effect=AssertionError('no client')):
            messages, proof = azure.build_request(source, caption, images)
        self.assertEqual(json.loads(messages[1]['content'][0]['text'])['body'], caption)
        self.assertEqual(len(proof['images']), 4)
        self.assertEqual(facts.digest(json.dumps(azure.wire_payload(messages)).encode('utf-8')),
                         proof['requestHash'])
        with self.assertRaises(azure.capacity.CapacityHold):
            azure.prepare_request(source, caption, images)

    def test_maximum_32_rows_64_notes_keeps_core_and_filters_only_after_full_validation(self):
        value = base.maximum_timing_result()
        verified, text, _ = base.source(photos=4)
        parsed = azure.normalize_result(value, verified, text, 4)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(sum(len(table['days']) for table in parsed), 32)
        self.assertEqual(sum(len(table['workTiming']['facts']) for table in parsed), 64)
        self.assertTrue(all(set(note) == set(facts.timing().FACT_FIELDS) | {'source'}
                            for table in parsed for note in table['workTiming']['facts']))
        legacy = copy.deepcopy(value)
        for period in legacy['periods']:
            for row in period['days']:
                del row['workTiming']
        old_core = azure.normalize_result(legacy, verified, text, 4, contract_version=facts.LEGACY_VERSION)
        self.assertEqual(facts.core_hash(parsed), facts.core_hash(old_core))
        self.assertEqual(azure.MAX_INPUT_BYTES, 6000)
        self.assertEqual(azure.SCHEMA['properties']['periods']['maxItems'], 2)
        self.assertEqual(azure.PERIOD_SCHEMA['properties']['imageIndexes']['maxItems'], 4)

        allowed = [('2026-10-16', '2026-10-31')]
        projected = azure.normalize_result(value, verified, text, 4, allowed)
        self.assertEqual(len(projected), 1)
        self.assertEqual(len(projected[0]['days']), 16)
        self.assertEqual(len(projected[0]['workTiming']['facts']), 32)
        self.assertTrue(all(note['serviceDate'].startswith('2026-10-')
                            for note in projected[0]['workTiming']['facts']))
        value['periods'][0]['days'][0]['workTiming'][0]['time'] = 'not-a-clock'
        with self.assertRaises(ValueError):
            azure.normalize_result(value, verified, text, 4, allowed)

    def test_maximum_compact_and_indented_http_envelopes_fit_unchanged_transport_limit(self):
        verified, text, _ = base.source(photos=4)
        images = [{'bytes': base.png(width=width), 'mime': 'image/png'} for width in range(9, 13)]
        messages, _ = azure.prepare_request(verified, text, images)
        self.assertEqual(azure.transport.MAX_RESPONSE_BYTES, 24000)
        for excluded in (False, True):
            value = base.maximum_timing_result(excluded=excluded)
            parsed = azure.normalize_result(value, verified, text, 4)
            self.assertEqual(sum(len(table['workTiming']['facts']) for table in parsed), 64)
            for indent, ensure_ascii in ((None, False), (None, True), (2, False), (2, True)):
                raw = http_envelope(value, indent=indent, ensure_ascii=ensure_ascii)
                with self.subTest(excluded=excluded, indent=indent, ensure_ascii=ensure_ascii, bytes=len(raw)):
                    self.assertLess(len(raw), azure.transport.MAX_RESPONSE_BYTES)
                    response = mock.MagicMock()
                    response.__enter__.return_value = response
                    response.getcode.return_value = 200
                    response.read.return_value = raw
                    opener = mock.Mock()
                    opener.open.return_value = response
                    client = azure.transport.AzureOpenAI(
                        {'AZURE_OPENAI_ENDPOINT': 'https://offline.openai.azure.com',
                         'AZURE_OPENAI_API_KEY': 'SYNTHETIC_ONLY'},
                        on_http_failure=mock.Mock(), opener=opener, usage=mock.Mock())
                    returned = client.structured(
                        messages, azure.SCHEMA, name='half_month_schedule',
                        max_completion_tokens=azure.MAX_OUTPUT_TOKENS)
                    self.assertEqual(returned, value)
                    opener.open.assert_called_once()
                    response.read.assert_called_once_with(azure.transport.MAX_RESPONSE_BYTES + 1)


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
    def test_registry_guard_blocks_ai_before_reserve_and_after_each_issuance_boundary(self):
        verified, text, _ = base.source()
        for boundary in ('before', 'reserve', 'on_issued', 'issued'):
            with self.subTest(boundary=boundary):
                path = self.registry_file()
                guard = base.collector.members.RegistryGuard(path, bindings=({},))
                usage, client, issued = mock.Mock(), mock.Mock(), mock.Mock()
                def change(*unused, **unused_keywords):
                    registry = base.collector.members.load_registry(path)
                    registry['members'][0]['collection'] = 'paused'
                    path.write_bytes(base.collector.members.json_bytes(registry))
                analyzer = azure.AzureAnalyzer(
                    usage, clock=lambda: base.NOW, client=client, registry_guard=guard)
                if boundary == 'before':
                    change()
                elif boundary == 'on_issued':
                    issued.side_effect = change
                else:
                    getattr(usage, boundary).side_effect = change
                with self.assertRaisesRegex(azure.RegistryFailure, 'registry_changed'):
                    analyzer.analyze(verified, text, [{'bytes': base.png(), 'mime': 'image/png'}],
                                     facts.target_periods(base.NOW), issued)
                client.structured.assert_not_called()
                self.assertEqual(analyzer.used, 0)
                if boundary == 'before':
                    usage.reserve.assert_not_called()
                    usage.finish.assert_not_called()
                else:
                    usage.finish.assert_called_once()

    def test_registry_guard_rejects_inactive_member_without_ai_reservation(self):
        registry = base.registry_fixture()
        registry['members'][0].update(membership='inactive', collection='paused')
        guard = base.collector.members.RegistryGuard(self.registry_file(registry), bindings=({},))
        usage, client = mock.Mock(), mock.Mock()
        analyzer = azure.AzureAnalyzer(usage, clock=lambda: base.NOW, client=client, registry_guard=guard)
        source, text, _ = base.source()
        with self.assertRaisesRegex(azure.RegistryFailure, 'membership_inactive'):
            analyzer.analyze(source, text, [{'bytes': base.png(), 'mime': 'image/png'}],
                             facts.target_periods(base.NOW), lambda _: None)
        usage.reserve.assert_not_called()
        client.structured.assert_not_called()

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
        self.assertEqual(client.structured.call_args.kwargs['max_completion_tokens'], azure.MAX_OUTPUT_TOKENS)
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
                 'AZURE_OPENAI_API_KEY': 'SYNTHETIC_ONLY'}, on_http_failure=mock.Mock(), opener=opener,
                usage=mock.Mock())
            verified, text, _ = base.source()
            messages, proof = azure.prepare_request(verified, text, [{'bytes': base.png(), 'mime': 'image/png'}])
            with self.subTest(model=output_model, refusal=refusal, finish=finish):
                if succeeds:
                    client.structured(messages, azure.SCHEMA, name='half_month_schedule',
                                      max_completion_tokens=azure.MAX_OUTPUT_TOKENS)
                    request = opener.open.call_args.args[0]
                    self.assertEqual(facts.digest(request.data), proof['requestHash'])
                    self.assertEqual(json.loads(request.data)['reasoning_effort'], 'none')
                else:
                    with self.assertRaises(azure.AnalysisFailure):
                        client.structured(messages, azure.SCHEMA, name='half_month_schedule', max_completion_tokens=1200)


if __name__ == '__main__':
    unittest.main()
