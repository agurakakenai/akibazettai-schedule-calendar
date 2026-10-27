"""Image ALT text as author-written schedule evidence; synthetic posts, mocked Luna, no network."""
import copy
import datetime as dt
import json
import unittest
from unittest import mock

import test_half_month_discovery as discovery
import test_half_month_schedules as base


collector, facts, azure = base.collector, base.facts, base.azure
reading = discovery.reading
at = discovery.at
SECOND_HALF = '2026-10-16'
BODY = '10月後半お給仕予定⬇️ #synthetic'
LINES = ['17日土ひる12-17', '20日火よる17-22', '21日水ひる 12-17', '23日金よる17-22',
         '25日日ひる12-17', '27日火よる17-22', '28日水ひる12-17', '30日金よる17-22']
ALT = 'あむ10月後半お給仕情報\n\n' + '\n'.join(LINES) + '\n\nまってるね'
WEEKDAYS = {17: '土', 20: '火', 21: '水', 23: '金', 25: '日', 27: '火', 28: '水', 30: '金', 31: '土'}


def alt_row(line, *, index=None):
    day = int(line.split('日', 1)[0])
    shifts = ('昼',) if 'ひる' in line else ('夜',)
    return reading.row(day, shifts, weekday=WEEKDAYS.get(day, '月'), transcription=line, index=index)


def alt_result(lines=LINES, *, complete=True, index=None, extra=()):
    return reading.result(*[alt_row(line, index=index) for line in lines], *extra,
                          month=10, half='second', indexes=() if index is None else (index,),
                          complete=complete)


class AltCycleTests(discovery.DiscoveryBase):
    def setUp(self):
        super().setUp()
        self.wires = []
        self.respond_with([])
        self.now = at(17)

    def alt_post(self, alt=ALT, *, created=None, suffix=70, body=BODY):
        entry = self.post(created or at(16, 21), suffix, body)
        if alt is not None:
            self.payloads[entry['id']]['mediaDetails'][0]['ext_alt_text'] = alt
        return entry

    def context(self, wire):
        return json.loads(wire[1]['content'][0]['text'])

    def images_attached(self, wire):
        return [item for item in wire[1]['content'] if item['type'] == 'image_url']

    def source_of(self, entry):
        return next(record for record in self.state['sources'].values()
                    if record['source']['id'] == entry['id'])

    def second_half(self):
        return [row for row in self.state['schedules'] if row['period']['from'] == SECOND_HALF]

    def test_alt_with_whole_schedule_is_one_text_only_request_without_image_download(self):
        entry = self.alt_post()
        self.documents = {'amu_zettai': discovery.base.document(entry)}
        self.responses = [alt_result()]
        report, _ = self.run_cycle(cycle_id='4001-1')
        self.assertEqual((len(self.wires), report['analysisRequests'], self.usage.calls), (1, 1, 1))
        self.assertEqual(self.images_attached(self.wires[0]), [])
        self.assertEqual([kind for kind, _ in self.calls if kind == 'image'], [], 'no image download')
        self.assertEqual(report['requests']['images'], 0)
        context = self.context(self.wires[0])
        self.assertEqual(context['body'], BODY + '\n\n[画像1のALT]\n' + ALT)
        self.assertEqual((context['imageMap'], context['images']), ([], []))
        self.assertIn('altText', context)
        self.assertIn('attachments', context)
        [table] = self.second_half()
        self.assertEqual([day['date'][-2:] for day in table['days']], [line[:2] for line in LINES])
        self.assertEqual([day['shifts'] for day in table['days']],
                         [['昼'] if 'ひる' in line else ['夜'] for line in LINES])
        self.assertTrue(table['reading']['complete'])
        self.assertTrue(all(fact['evidence']['imageIndex'] is None
                            for fact in table['reading']['days'].values()))
        record = self.source_of(entry)
        self.assertEqual(record['imageHashes'], [])
        self.assertEqual(record['source']['bodyHash'], facts.digest(BODY.encode('utf-8')))
        self.assertEqual(record['source']['authorTextHash'],
                         facts.digest((BODY + '\n\n[画像1のALT]\n' + ALT).encode('utf-8')))
        progress = next(iter(self.state['readings'].values()))
        self.assertEqual((progress['stage'], progress['reason']), ('done', 'valid_schedule'))
        self.assertTrue(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])
        state_text = json.dumps(self.state, ensure_ascii=False)
        for private in ('お給仕情報', LINES[0], 'まってるね'):
            self.assertNotIn(private, state_text)
        facts.validate_state(self.state)
        # A finished ALT reading is not re-read or re-charged by a later run.
        self.now += dt.timedelta(days=1)
        self.reopen()
        self.calls.clear()
        report, _ = self.run_cycle(resume=True, cycle_id='4001-1')
        self.assertEqual((report['analysisRequests'], len(self.wires), self.usage.calls), (0, 1, 1))
        self.assertNotIn(('post', entry['id']), self.calls)

    def test_title_only_alt_reads_the_images_with_alt_as_context(self):
        entry = self.alt_post('あむ10月後半お給仕情報')
        self.documents = {'amu_zettai': discovery.base.document(entry)}
        self.responses = [reading.result(reading.row(20, weekday='火', transcription='20日 昼'),
                                         month=10, half='second')]
        self.run_cycle(cycle_id='4002-1')
        self.assertEqual(len(self.wires), 1)
        self.assertEqual(len(self.images_attached(self.wires[0])), 1)
        self.assertEqual([kind for kind, _ in self.calls if kind == 'image'], ['image'])
        self.assertIn('[画像1のALT]\nあむ10月後半お給仕情報', self.context(self.wires[0])['body'])
        self.assertEqual([day['date'] for day in self.second_half()[0]['days']], ['2026-10-20'])
        self.assertEqual(len(self.source_of(entry)['imageHashes']), 1)
        facts.validate_state(self.state)

    def test_alt_without_any_row_falls_back_to_the_unchanged_image_reading(self):
        entry = self.alt_post()
        self.documents = {'amu_zettai': discovery.base.document(entry)}
        self.responses = [{'classification': 'uncertain', 'complete': False, 'periods': []},
                          reading.result(reading.row(20, weekday='火', transcription='20日 昼'),
                                         month=10, half='second')]
        report, _ = self.run_cycle(cycle_id='4003-1')
        self.assertEqual((len(self.wires), self.usage.calls, report['analysisRequests']), (2, 2, 2))
        self.assertEqual(self.images_attached(self.wires[0]), [])
        self.assertEqual(len(self.images_attached(self.wires[1])), 1)
        self.assertEqual([kind for kind, _ in self.calls if kind == 'image'], ['image'])
        self.assertEqual(len(set(self.usage.requests)), 2, 'no duplicate paid request')
        self.assertEqual([day['date'] for day in self.second_half()[0]['days']], ['2026-10-20'])
        facts.validate_state(self.state)

    def test_without_alt_the_post_is_read_exactly_as_before(self):
        for alt in (None, '', '  \n '):
            with self.subTest(alt=alt):
                self.setUp()
                entry = self.alt_post(alt)
                self.documents = {'amu_zettai': discovery.base.document(entry)}
                self.responses = [reading.result(reading.row(20, weekday='火', transcription='20日 昼'),
                                                 month=10, half='second')]
                self.run_cycle(cycle_id='4004-1')
                context = self.context(self.wires[0])
                self.assertEqual(context['body'], BODY)
                self.assertNotIn('altText', context)
                self.assertNotIn('attachments', context)
                self.assertEqual(len(self.images_attached(self.wires[0])), 1)
                record = self.source_of(entry)
                self.assertNotIn('authorTextHash', record['source'])
                self.assertEqual(facts.source_key(record['source']), facts.digest(
                    {key: record['source'][key] for key in (
                        'authorId', 'id', 'editTweetIds', 'bodyHash', 'media')}))

    def test_fabricated_rows_are_dropped_and_never_saved(self):
        entry = self.alt_post()
        self.documents = {'amu_zettai': discovery.base.document(entry)}
        # 19日 is in neither the body nor the ALT text; citing image 0 cannot hide that,
        # because a text-only request supplied no image.
        fabricated = [reading.row(19, ('昼',), weekday='月', transcription='19日月ひる12-17', index=None),
                      reading.row(22, ('夜',), weekday='木', transcription='22日木よる17-22', index=0)]
        self.responses = [alt_result(extra=fabricated)]
        self.run_cycle(cycle_id='4005-1')
        self.assertEqual(len(self.wires), 1)
        [table] = self.second_half() or self.state['partialSchedules']
        dates = [day['date'] for day in table['days']]
        self.assertNotIn('2026-10-19', dates)
        self.assertNotIn('2026-10-22', dates)
        self.assertEqual(len(dates), 8)
        self.assertFalse(table['reading']['complete'])
        progress = next(iter(self.state['readings'].values()))
        self.assertEqual((progress['stage'], progress['reason']), ('held', 'reading_partial'))
        self.assertEqual([kind for kind, _ in self.calls if kind == 'image'], [])
        facts.validate_state(self.state)

    def test_image_reading_drops_a_body_row_absent_from_body_and_alt(self):
        entry = self.alt_post('あむ10月後半お給仕情報')
        self.documents = {'amu_zettai': discovery.base.document(entry)}
        self.responses = [reading.result(
            reading.row(20, weekday='火', transcription='20日 昼'),
            reading.row(24, weekday='土', transcription='24日 昼', index=None),
            month=10, half='second')]
        self.run_cycle(cycle_id='4006-1')
        rows = self.second_half() or self.state['partialSchedules']
        self.assertEqual([day['date'] for day in rows[0]['days']], ['2026-10-20'])
        self.assertFalse(rows[0]['reading']['complete'])

    def test_an_issued_alt_request_is_never_reissued_after_a_failure(self):
        entry = self.alt_post()
        self.documents = {'amu_zettai': discovery.base.document(entry)}

        def timeout(messages, *args, **kwargs):
            self.wires.append(copy.deepcopy(messages))
            raise azure.AnalysisFailure('azure_timeout')
        self.model.structured.side_effect = timeout
        report, _ = self.run_cycle(cycle_id='4008-1')
        self.assertEqual((len(self.wires), self.usage.calls), (1, 1))
        progress = next(iter(self.state['readings'].values()))
        self.assertEqual(progress['stage'], 'original')
        self.assertEqual([kind for kind, _ in self.calls if kind == 'image'], [])
        # Next run: the issued body+ALT request is not repeated; the images are read instead.
        self.now = at(17, 17)
        self.reopen()
        self.respond_with([reading.result(reading.row(20, weekday='火', transcription='20日 昼'),
                                           month=10, half='second')])
        self.run_cycle(resume=True, cycle_id='4008-1')
        self.assertEqual((len(self.wires), self.usage.calls, len(set(self.usage.requests))), (2, 2, 2))
        self.assertEqual(len(self.images_attached(self.wires[1])), 1)
        self.assertEqual([day['date'] for day in self.second_half()[0]['days']], ['2026-10-20'])
        facts.validate_state(self.state)

    def respond_with(self, responses):
        self.responses = list(responses)

        def respond(messages, *args, **kwargs):
            self.wires.append(copy.deepcopy(messages))
            return self.responses.pop(0)
        self.model.structured.side_effect = respond

    def test_edited_alt_is_a_new_input_that_is_read_again(self):
        first = self.alt_post()
        self.documents = {'amu_zettai': discovery.base.document(first)}
        self.responses = [alt_result(LINES[:4], complete=False)]
        self.run_cycle(cycle_id='4007-1')
        self.assertFalse(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])
        # X edits keep the body but publish a new post id that lists the earlier one.
        edited_alt = ALT.replace('30日金よる17-22', '31日土よる17-22')
        edited = self.alt_post(edited_alt, created=at(17, 1), suffix=71)
        self.payloads[edited['id']]['edit_control'] = {'edit_tweet_ids': [first['id'], edited['id']]}
        self.now = at(17, 17)
        self.documents = {'amu_zettai': discovery.base.document(edited, first)}
        lines = [*LINES[:-1], '31日土よる17-22']
        self.responses = [alt_result(lines)]
        self.reopen()
        self.run_cycle(resume=True, cycle_id='4007-1')
        self.assertEqual(len(self.wires), 2)
        self.assertEqual(self.images_attached(self.wires[1]), [])
        self.assertIn('31日土よる17-22', self.context(self.wires[1])['body'])
        old, new = self.source_of(first)['source'], self.source_of(edited)['source']
        self.assertEqual(old['bodyHash'], new['bodyHash'])
        self.assertNotEqual(old['authorTextHash'], new['authorTextHash'])
        [table] = self.second_half()
        self.assertEqual(table['id'], edited['id'])
        self.assertIn('2026-10-31', [day['date'] for day in table['days']])
        self.assertNotIn('2026-10-30', [day['date'] for day in table['days']])
        facts.validate_state(self.state)


class AltSourceTests(unittest.TestCase):
    def verify(self, alt, *, body=BODY):
        created = at(16, 21)
        entry = base.entry(created=created, suffix=80)
        candidate = collector.discover(base.document(entry), {base.TARGET['name']: base.TARGET},
                                       at(17), {}, candidate_since=dt.date(2026, 10, 1))[0][0]
        payload = base.payload(created=created, suffix=80)
        payload['text'] = body
        if alt is not None:
            payload['mediaDetails'][0]['ext_alt_text'] = alt
        return collector.validate_post(candidate, payload, base.TARGET, at(17))

    def test_alt_is_part_of_the_source_fingerprint(self):
        plain, plain_text, _ = self.verify(None)
        with_alt, text, _ = self.verify(ALT)
        edited, edited_text, _ = self.verify(ALT + '!')
        self.assertEqual(plain_text, BODY)
        self.assertEqual(text, BODY + '\n\n[画像1のALT]\n' + ALT)
        self.assertEqual({plain['bodyHash'], with_alt['bodyHash'], edited['bodyHash']},
                         {facts.digest(BODY.encode('utf-8'))})
        keys = {facts.source_key(item) for item in (plain, with_alt, edited)}
        self.assertEqual(len(keys), 3, 'an ALT edit is an input change like a body edit')
        self.assertEqual(azure.alt_text(with_alt, text), '\n\n[画像1のALT]\n' + ALT)
        self.assertEqual(azure.alt_text(plain, plain_text), '')
        self.assertTrue(collector.alt_schedule_text(with_alt, text))
        self.assertFalse(collector.alt_schedule_text(plain, plain_text))
        title, title_text, _ = self.verify('あむ10月後半お給仕情報')
        self.assertFalse(collector.alt_schedule_text(title, title_text))
        # The request text is checked against the body+ALT hash, not a caller-supplied text.
        azure._validate_request_text(with_alt, text)
        with self.assertRaisesRegex(ValueError, 'schedule_body_mismatch'):
            azure._validate_request_text(with_alt, BODY)
        with self.assertRaisesRegex(ValueError, 'schedule_body_mismatch'):
            azure._validate_request_text(with_alt, text.replace('17日', '18日'))

    def test_body_text_that_mimics_an_alt_label_is_split_by_the_verified_hash(self):
        body = BODY + '\n\n[画像1のALT]\n1日 2日'
        source, text, _ = self.verify('タイトルだけ', body=body)
        self.assertEqual(azure.alt_text(source, text), '\n\n[画像1のALT]\nタイトルだけ')
        self.assertFalse(collector.alt_schedule_text(source, text))

    def test_alt_and_body_over_the_input_budget_read_the_body_and_images_only(self):
        body = 'あ' * 1980  # 5,940 UTF-8 bytes: within the body limit on its own
        source, text, urls = self.verify(ALT, body=body)
        self.assertLessEqual(len(body.encode('utf-8')), azure.MAX_INPUT_BYTES)
        self.assertGreater(len(azure.author_text(body, [ALT]).encode('utf-8')), azure.MAX_INPUT_BYTES)
        self.assertEqual(text, body)
        self.assertNotIn('authorTextHash', source)
        self.assertEqual(len(urls), 1)
        # A maximal ALT text with an ordinary body stays within the budget and is read.
        source, text, _ = self.verify('い' * collector.MAX_ALT_CHARS, body=BODY)
        self.assertLessEqual(len(text.encode('utf-8')), azure.MAX_INPUT_BYTES)
        self.assertIn('authorTextHash', source)
        with self.assertRaisesRegex(ValueError, 'invalid_post_text'):
            self.verify(None, body='あ' * 2001)

    def test_invalid_alt_metadata_is_refused(self):
        for alt in ('い' * (collector.MAX_ALT_CHARS + 1), 7, ['17日'], {'text': '17日'}):
            with self.subTest(alt=str(alt)[:10]):
                with self.assertRaisesRegex(ValueError, 'invalid_photo_metadata'):
                    self.verify(alt)

    def test_text_only_pack_requires_an_alt_bearing_image_post_and_no_attachment(self):
        source, text, _ = self.verify(ALT)
        [pack] = azure.reading_packs(source, text, [], stage='text')
        self.assertEqual((pack['textOnly'], pack['images'], pack['imageMap']), (True, [], []))
        messages, proof = azure.prepare_reading_request(source, text, [], pack)
        self.assertEqual(proof['images'], [])
        self.assertEqual([item['type'] for item in messages[1]['content']], ['text'])
        plain, plain_text, _ = self.verify(None)
        with self.assertRaisesRegex(ValueError, 'invalid_reading_pack'):
            azure.reading_packs(plain, plain_text, [], stage='text')
        image = [{'bytes': base.png(), 'mime': 'image/png'}]
        with self.assertRaisesRegex(ValueError, 'invalid_reading_pack'):
            azure.prepare_reading_request(source, text, image, pack)
        with self.assertRaisesRegex(ValueError, 'schedule_images_incomplete'):
            azure.prepare_reading_request(source, text, [], {**pack, 'textOnly': False})
        with self.assertRaisesRegex(ValueError, 'invalid_reading_pack'):
            azure.prepare_reading_request(source, text, [], {**pack, 'textOnly': 1})

    def test_text_reading_grounds_every_row_in_body_or_alt(self):
        source, text, _ = self.verify(ALT)
        value = alt_result(index=0, extra=[
            reading.row(22, ('夜',), weekday='木', transcription='22日木よる17-22', index=0)])
        value['periods'][0]['printedYear'] = 2026
        schedules, complete = azure.normalize_text_reading(value, source, text)
        self.assertFalse(complete)
        [table] = schedules
        self.assertEqual(len(table['days']), 8)
        self.assertEqual(table['period']['printedYear'], None)
        self.assertTrue(all(fact['evidence'] == {'imageIndex': None, 'box': None, 'imageHash': None}
                            for fact in table['reading']['days'].values()))
        schedules, complete = azure.normalize_text_reading(alt_result(), source, text)
        self.assertTrue(complete)
        plain, plain_text, _ = self.verify(None)
        with self.assertRaisesRegex(ValueError, 'invalid_reading_pack'):
            azure.normalize_text_reading(alt_result(), plain, plain_text)
    def test_timing_reanalysis_of_an_alt_reading_is_refused_before_any_reservation(self):
        timing = collector._module('half-month-timing.py', 'alt_timing')
        source, _, _ = self.verify(ALT)
        patches = [mock.patch.object(timing.facts, 'validate_state'),
                   mock.patch.object(timing.ledger, 'validate_state'),
                   mock.patch.object(timing.saved, 'validate_accounting'),
                   mock.patch.object(timing.saved, 'validate_reanalysis_basis',
                                     side_effect=AssertionError('checked first'))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with self.assertRaisesRegex(ValueError, 'timing_original_images_required'):
            timing._authorize({}, {}, source, [], {})


if __name__ == '__main__':
    unittest.main()
