"""Synthetic v3 state and projections; no source requests or live model calls."""
import copy
import datetime as dt
import types
import unittest
from unittest import mock

import test_half_month_schedules as base
import test_half_month_saved as saved_fixture


facts = base.facts
PROMPT = 'synthetic reading contract'
SCHEMA = {'type': 'object'}


def reading_table(*, complete=False, suffix=2, rows=None):
    source, tables, analysis = base.normalized(base.CREATED + dt.timedelta(minutes=suffix), suffix)
    table = tables[0]
    table['days'] = copy.deepcopy(rows if rows is not None else [{'date': '2026-09-08', 'shifts': []}])
    table.pop('workTiming', None)
    table['reading'] = {
        'contract': facts.READING_VERSION, 'complete': complete,
        'days': {row['date']: {
            'weekday': '月火水木金土日'[facts.day(row['date']).weekday()],
            'qualifier': None, 'hours': {}, 'shiftStatus': 'stated' if row['shifts'] else 'unstated',
            'evidence': {'imageIndex': 0, 'box': [0.1, 0.2, 0.8, 0.9],
                         'imageHash': analysis['images'][0]['sha256']},
            'transcriptionHash': facts.digest(['synthetic', row['date']]),
        } for row in table['days']},
    }
    analysis.update(contract=facts.READING_VERSION, promptHash=facts.digest(PROMPT.encode()),
                    schemaHash=facts.digest(SCHEMA))
    return source, [table], analysis


class ReadingStateTests(base.Offline):
    def setUp(self):
        super().setUp()
        original = facts.load_module

        def load(filename, name):
            if name == 'half_month_reading_contract':
                return types.SimpleNamespace(contract_parts=lambda version: (PROMPT, SCHEMA, None))
            return original(filename, name)

        self.patch = mock.patch.object(facts, 'load_module', side_effect=load)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def apply(self, state, fixture):
        source, tables, analysis = fixture
        return facts.apply_reading_revision(state, tables, source, analysis)

    def test_partial_is_durable_public_and_non_destructive(self):
        state = facts.empty_state()
        source, tables, analysis = base.normalized()
        facts.apply_revision(state, tables, source, analysis)
        confirmed = copy.deepcopy(state['schedules'])
        fixture = reading_table()
        self.assertTrue(self.apply(state, fixture))
        self.assertEqual(state['schedules'], confirmed)
        self.assertEqual(state['partialSchedules'], fixture[1])
        self.assertEqual(len(state['receipts']), 2)
        self.assertFalse(self.apply(state, fixture))
        facts.validate_state(state)
        public = facts.public_state(state)
        facts.validate_state(public, private=False)
        self.assertEqual(public['partialSchedules'], fixture[1])
        for private in ('revisions', 'readings', 'sources', 'bodyHash', 'transcription"', 'rawText'):
            self.assertNotIn(private, str(public))
        manual = {'2026-09-08': {'夜': [{'name': 'あむ', 'note': 'manual'}]}}
        before = copy.deepcopy(manual)
        result = facts.effective_schedule(manual, public)
        self.assertEqual(result['2026-09-08']['夜'], manual['2026-09-08']['夜'])
        self.assertEqual(result['2026-09-08']['unassigned'][0]['name'], 'あむ')
        self.assertEqual(set(result['2026-09-08']), {'夜', 'unassigned'})
        self.assertEqual(manual, before)

    def test_complete_date_only_is_confirmed_without_invented_shift(self):
        state = facts.empty_state()
        fixture = reading_table(complete=True)
        self.apply(state, fixture)
        self.assertEqual(state['schedules'], fixture[1])
        self.assertNotIn('partialSchedules', state)
        self.assertEqual(set(facts.effective_schedule({}, state)['2026-09-08']), {'unassigned'})

    def test_partial_same_post_can_be_completed_and_replayed(self):
        state = facts.empty_state()
        partial = reading_table()
        self.apply(state, partial)
        complete = copy.deepcopy(partial)
        complete[1][0]['reading']['complete'] = True
        complete[2]['receiptId'] = facts.digest('completion')
        self.apply(state, complete)
        self.assertEqual(state['schedules'], complete[1])
        self.assertEqual(state['partialSchedules'], [])
        self.assertEqual(len(state['revisions']), 2)
        self.assertFalse(self.apply(state, partial))

    def test_detail_reading_accumulates_dates_without_downgrading_stated_shift(self):
        state = facts.empty_state()
        initial = reading_table(rows=[{'date': '2026-09-05', 'shifts': ['昼']}])
        self.apply(state, initial)
        detail = reading_table(rows=[
            {'date': '2026-09-05', 'shifts': []}, {'date': '2026-09-08', 'shifts': []}])
        detail[2].update(receiptId=facts.digest('detail'),
                         analyzedAt=facts.stamp(base.NOW + dt.timedelta(minutes=3)))
        self.apply(state, detail)
        self.assertEqual(state['partialSchedules'][0]['days'], [
            {'date': '2026-09-05', 'shifts': ['昼']}, {'date': '2026-09-08', 'shifts': []}])
        reordered = copy.deepcopy(state)
        reordered['revisions'] = dict(reversed(list(state['revisions'].items())))
        facts.validate_state(reordered)
        self.assertEqual(facts.public_state(reordered), facts.public_state(state))

    def test_full_month_two_halves_are_representable(self):
        state = facts.empty_state()
        source, tables, analysis = reading_table(complete=True)
        second = copy.deepcopy(tables[0])
        second['period'].update({'from': '2026-09-16', 'to': '2026-09-30'})
        second['days'][0]['date'] = '2026-09-20'
        fact = second['reading']['days'].pop('2026-09-08')
        fact['weekday'] = '日'
        second['reading']['days']['2026-09-20'] = fact
        tables.append(second)
        facts.apply_reading_revision(state, tables, source, analysis)
        self.assertEqual(len(state['schedules']), 2)
        self.assertEqual(len(state['receipts'][analysis['receiptId']]), 2)

    def test_partial_is_never_admitted_as_legacy_or_unbacked_public_state(self):
        source, tables, analysis = reading_table()
        analysis['contract'] = facts.VERSION
        analysis['promptHash'] = facts.digest(base.azure.PROMPT.encode())
        analysis['schemaHash'] = facts.digest(base.azure.SCHEMA)
        with self.assertRaisesRegex(ValueError, 'invalid_schedule_contract'):
            facts.apply_revision(facts.empty_state(), tables, source, analysis)
        state = facts.empty_state()
        state['partialSchedules'] = tables
        with self.assertRaisesRegex(ValueError, 'schedule_partial_missing'):
            facts.validate_state(state)
        state['schedules'] = tables
        with self.assertRaisesRegex(ValueError, 'invalid_confirmed_schedule'):
            facts.validate_state(state)

    def test_public_partial_post_identity_is_consistent_across_halves(self):
        state = facts.empty_state()
        fixture = reading_table()
        self.apply(state, fixture)
        public = facts.public_state(state)
        second = copy.deepcopy(public['partialSchedules'][0])
        second['name'] = 'べつ'
        public['partialSchedules'].append(second)
        with self.assertRaisesRegex(ValueError, 'inconsistent_schedule_post'):
            facts.validate_state(public, private=False)

    def test_unsafe_fields_and_unbound_images_rejected_atomically(self):
        for change in ('raw', 'transcription', 'image', 'status', 'hour', 'weekday', 'unknown'):
            fixture = reading_table()
            table = fixture[1][0]
            fact = table['reading']['days']['2026-09-08']
            if change == 'raw':
                table['text'] = 'must never persist'
            elif change == 'transcription':
                fact['transcription'] = 'must never persist'
            elif change == 'image':
                fact['evidence']['imageHash'] = 'f' * 64
            elif change == 'status':
                fact['shiftStatus'] = 'stated'
            elif change == 'hour':
                fact['hours'] = {'start': {'time': '12:00', 'basis': 'qualifier-rule-v1'}}
            elif change == 'weekday':
                fact['weekday'] = '月'
            else:
                table['reading']['unexpected'] = True
            state = facts.empty_state()
            before = copy.deepcopy(state)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.apply(state, fixture)
            self.assertEqual(state, before)

    def test_qualifier_rules_do_not_invent_unmapped_hours(self):
        for qualifier, shift, start, end in (
                ('long', '昼', '12:00', '18:00'), ('early', '夜', '16:00', '22:00'),
                ('late', '夜', '18:00', '22:00')):
            _, tables, _ = reading_table(rows=[{'date': '2026-09-08', 'shifts': [shift]}])
            fact = tables[0]['reading']['days']['2026-09-08']
            fact.update(qualifier=qualifier, hours={
                'start': {'time': start, 'basis': 'qualifier-rule-v1'},
                'end': {'time': end, 'basis': 'qualifier-rule-v1'}})
            facts.validate_schedule(tables[0])
            fact['hours']['start'] = {'time': '13:00', 'basis': 'explicit'}
            facts.validate_schedule(tables[0])
        fact['qualifier'] = 'long'
        with self.assertRaisesRegex(ValueError, 'invalid_schedule_hours'):
            facts.validate_schedule(tables[0])
        fact['hours'] = {}
        facts.validate_schedule(tables[0])

    def test_reading_progress_bounds_and_private_projection(self):
        state = facts.empty_state()
        state['readings'] = {'a' * 64: {
            'sourceId': base.post_id(), 'cacheKey': 'b' * 64, 'stage': 'fetch',
            'reason': 'image_fetch_failed', 'nextAt': None,
            'attemptedVariants': [facts.digest(number) for number in range(256)]}}
        state['collection'] = {'chainId': '1-2', 'names': ['あむ'],
                               'periods': [['2026-09-01', '2026-09-15']],
                               'reason': 'time_limit', 'ready': False,
                               'nextAt': facts.stamp(base.NOW), 'cursor': 1}
        state['collectionSlots'] = [facts.stamp(base.NOW)]
        facts.validate_state(state)
        public = facts.public_state(state)
        self.assertFalse({'readings', 'collection', 'collectionSlots'} & set(public))
        for mode in ('schedule', 'both'):
            state['collection'].update(serviceDate='2026-09-07', mode=mode)
            facts.validate_state(state)
            self.assertNotIn('collection', facts.public_state(state))
        original = copy.deepcopy(state['collection'])
        for patch in ({'serviceDate': '2026-09-31'}, {'serviceDate': '2026-9-7'},
                      {'serviceDate': None}, {'mode': 'personal'}, {'mode': None},
                      {'periods': []}, {'extra': 'rejected'}):
            state['collection'] = {**original, **patch}
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                facts.validate_state(state)
        state['collection'] = original
        state['readings']['a' * 64]['attemptedVariants'].append(facts.digest(256))
        with self.assertRaisesRegex(ValueError, 'invalid_reading_variants'):
            facts.validate_state(state)

    def test_reply_to_old_parent_cannot_replace_or_cancel_newer_table(self):
        for operation in ('replace', 'cancel'):
            state = facts.empty_state()
            older = reading_table(complete=True, suffix=1, rows=[{'date': '2026-09-05', 'shifts': ['昼']}])
            newer = reading_table(complete=True, suffix=2, rows=[{'date': '2026-09-05', 'shifts': ['昼', '夜']}])
            self.apply(state, older)
            self.apply(state, newer)
            reply = reading_table(suffix=3, rows=[{'date': '2026-09-05', 'shifts': ['昼']}])
            parent = {'replyToId': older[0]['id'], 'replyToAuthorId': older[0]['authorId']}
            reply[0].update(parent)
            reply[1][0].update(parent, sourceKind='own-reply')
            reply[1][0]['reading']['days']['2026-09-05']['operation'] = operation
            self.apply(state, reply)
            effective = facts.effective_schedule({}, state)
            for shift in ('昼', '夜'):
                self.assertEqual(effective['2026-09-05'][shift][0]['name'], 'あむ')
                self.assertEqual(effective['2026-09-05'][shift][0]['scheduleSources'][0]['id'], newer[0]['id'])

    def test_own_reply_amends_automatic_rows_but_never_manual_rows(self):
        for operation in ('add', 'replace', 'cancel'):
            state = facts.empty_state()
            source, tables, analysis = base.normalized()
            facts.apply_revision(state, tables, source, analysis)
            reply = reading_table(suffix=3, rows=[
                {'date': '2026-09-05', 'shifts': [] if operation == 'cancel' else ['夜']}])
            reply[0].update(replyToId=source['id'], replyToAuthorId=source['authorId'])
            reply[1][0].update(sourceKind='own-reply', replyToId=source['id'],
                               replyToAuthorId=source['authorId'])
            fact = reply[1][0]['reading']['days']['2026-09-05']
            fact.update(operation=operation, shiftStatus='stated')
            self.apply(state, reply)
            manual = {'2026-09-05': {'昼': [{'name': 'あむ', 'note': 'manual'}]}}
            projected = facts.effective_schedule(manual, state)
            self.assertEqual(projected['2026-09-05']['昼'][0]['note'], 'manual')
            automatic = facts.effective_schedule({}, state)
            self.assertEqual(bool(automatic.get('2026-09-05', {}).get('昼')), operation == 'add')
            self.assertEqual(bool(automatic.get('2026-09-05', {}).get('夜')), operation != 'cancel')
            self.assertEqual(state['schedules'], tables)

    def test_own_reply_requires_paired_same_author_and_explicit_operation(self):
        source, tables, _ = reading_table()
        source['replyToId'] = base.post_id()
        with self.assertRaisesRegex(ValueError, 'invalid_schedule_reply'):
            facts.validate_source(source)
        source['replyToAuthorId'] = '123'
        with self.assertRaisesRegex(ValueError, 'invalid_schedule_reply'):
            facts.validate_source(source)
        table = tables[0]
        table.update(sourceKind='own-reply', replyToId=base.post_id(),
                     replyToAuthorId=source['authorId'])
        with self.assertRaisesRegex(ValueError, 'invalid_schedule_fields'):
            facts.validate_schedule(table)

    def test_own_reply_cancellation_can_target_only_one_shift(self):
        state = facts.empty_state()
        original = reading_table(complete=True, suffix=1, rows=[
            {'date': '2026-09-05', 'shifts': ['昼', '夜']}])
        self.apply(state, original)
        source = original[0]
        reply = reading_table(suffix=3, rows=[{'date': '2026-09-05', 'shifts': ['夜']}])
        reply[0].update(replyToId=source['id'], replyToAuthorId=source['authorId'])
        reply[1][0].update(sourceKind='own-reply', replyToId=source['id'],
                           replyToAuthorId=source['authorId'])
        reply[1][0]['reading']['days']['2026-09-05']['operation'] = 'cancel'
        self.apply(state, reply)
        self.assertEqual(set(facts.effective_schedule({}, state)['2026-09-05']), {'昼'})

    def test_saved_partial_import_is_accounted_and_does_not_claim_success(self):
        fixture = saved_fixture.SavedHalfMonthTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        entry = fixture.entry()
        source, tables, analysis = reading_table(suffix=1)
        entry['amendment'].update(source=source, schedules=tables, analysis=analysis)
        entry['amendment']['proof'].update(issuedAt=analysis['analyzedAt'],
                                          searchCreatedAt=source['createdAt'])
        # Both fixture modules use independent canonical imports.
        with mock.patch.object(saved_fixture.facts, 'load_module', side_effect=facts.load_module):
            after = fixture.apply(None, [entry], now=base.NOW + dt.timedelta(hours=1))
            self.assertEqual(after['schedules'], [])
            self.assertEqual(after['partialSchedules'], tables)
            self.assertEqual(after['lastRun'], {'status': 'partial'})
            self.assertIsNone(after['lastSuccessAt'])
            saved_fixture.saved.validate_accounting(after, fixture.usage, saved_fixture.facts)
            self.assertEqual(fixture.apply(after, [entry], now=base.NOW + dt.timedelta(hours=1)), after)

    def test_timing_core_preserves_v3_and_skips_date_only(self):
        timing = facts.load_module('half-month-timing.py', 'reading_state_timing_test')
        source, tables, analysis = reading_table(complete=True, rows=[
            {'date': '2026-09-05', 'shifts': ['昼']}, {'date': '2026-09-08', 'shifts': []}])
        authorization = {
            'operation': 'work-timing-only', 'updateChannels': ['workTiming'],
            'expectedSubjectHash': '1' * 64, 'expectedCoreHash': facts.core_hash(tables),
            'expectedTimingHash': facts.timing_hash(tables), 'basisRevisionKeys': ['2' * 64],
            'previous': {'contractVersion': facts.READING_VERSION, 'contractHash': '3' * 64,
                         'analysisReceiptId': analysis['receiptId'], 'kind': 'native'},
            'targetScopes': [{'name': source['name'], 'serviceDate': '2026-09-05',
                              'shift': '昼', 'boundary': 'end'}],
        }
        self.assertEqual(timing.core_copy(tables), tables)
        self.assertEqual(len(timing.slots_for(tables, authorization)), 1)
        tables[0]['reading']['complete'] = False
        with self.assertRaisesRegex(ValueError, 'timing_partial_schedule'):
            timing.slots_for(tables, authorization)

    def test_saved_timing_amendment_retains_complete_v3_reading_and_unassigned_day(self):
        import test_half_month_timing as timing_fixture
        harness = timing_fixture.TimingOnlyTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        with mock.patch.object(saved_fixture.facts, 'load_module', side_effect=facts.load_module), \
                mock.patch.object(timing_fixture.facts, 'load_module', side_effect=facts.load_module):
            entry = harness.fx.entry()
            source, tables, analysis = reading_table(complete=True, suffix=1, rows=[
                {'date': '2026-09-05', 'shifts': ['昼']}, {'date': '2026-09-08', 'shifts': []}])
            entry['amendment'].update(source=source, schedules=tables, analysis=analysis)
            entry['amendment']['proof'].update(issuedAt=analysis['analyzedAt'],
                                              searchCreatedAt=source['createdAt'])
            harness.state = harness.fx.apply(None, [entry], now=timing_fixture.NOW)
            harness.configure()
            packet = harness.packet()
            updated, _ = harness.apply(packet)
            self.assertEqual(updated['schedules'][0]['reading'], tables[0]['reading'])
            self.assertEqual(updated['schedules'][0]['days'], tables[0]['days'])
            self.assertTrue(updated['schedules'][0]['workTiming']['facts'])
            self.assertIn('unassigned', facts.effective_schedule({}, updated)['2026-09-08'])


if __name__ == '__main__':
    unittest.main()
