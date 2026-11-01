"""Second-half discovery regressions for the October 2026 outage.

Old first-half pending candidates and a rebased cohort (cursor == len(names)) blocked every
second-half search. Synthetic sources only; Luna is mocked and no live network is reachable.
"""
import contextlib
import datetime as dt
import email.message
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import urllib.error

import test_azure_openai as transport_fixture
import test_cloud_collection as legacy
import test_half_month_cycle as cycle
import test_half_month_schedules as base


collector, facts, azure = base.collector, base.facts, base.azure
reading = cycle.reading_fixture
cloud = legacy.cloud
SECOND_HALF = '2026-10-16'


def at(day, hour=10):
    return dt.datetime(2026, 10, day, hour, tzinfo=facts.JST).astimezone(facts.UTC)


class DiscoveryBase(unittest.TestCase):
    setUp_cycle = cycle.CycleTests.setUp
    client_base = cycle.CycleTests.client
    run_cycle = cycle.CycleTests.run_cycle
    reopen = cycle.CycleTests.reopen

    def setUp(self):
        self.setUp_cycle()
        self.calls = []

    def client(self):
        value = self.client_base()
        for kind in ('search', 'post', 'image'):
            normal = getattr(value, kind).side_effect

            def wrapped(arg, kind=kind, normal=normal):
                self.calls.append((kind, arg))
                return normal(arg)
            getattr(value, kind).side_effect = wrapped
        return value

    def post(self, created, suffix, text, handle='amu_zettai'):
        entry = base.entry(created=created, suffix=suffix, handle=handle)
        payload = base.payload(created=created, suffix=suffix)
        payload['text'] = text
        self.payloads[entry['id']] = payload
        return entry

    def second_half_result(self):
        return reading.result(reading.row(20, weekday='火', transcription='20日 昼'), month=10, half='second')

    def first_half_held(self):
        """10/05: a first-half post read as uncertain stays held in pending."""
        self.now = at(5)
        old = self.post(at(3), 1, '10月前半のお給仕予定')
        self.documents = {'amu_zettai': base.document(old)}
        self.model.structured.return_value = {'classification': 'uncertain', 'complete': False, 'periods': []}
        self.run_cycle()
        self.assertEqual([item['id'] for item in self.state['pending']], [old['id']])
        self.assertEqual(next(iter(self.state['readings'].values()))['stage'], 'held')
        return old

    def searches(self):
        return [arg for kind, arg in self.calls if kind == 'search']


class SecondHalfSearchTests(DiscoveryBase):
    def test_old_first_half_pending_does_not_block_the_second_half_search(self):
        old = self.first_half_held()
        self.now = at(13)
        new = self.post(at(13, 9), 2, '10月後半のお給仕予定')
        self.documents = {'amu_zettai': base.document(new, old)}
        self.model.structured.return_value = self.second_half_result()
        self.reopen()
        self.calls.clear()
        self.run_cycle(cycle_id='3001-1')
        self.assertEqual(self.searches(), ['amu_zettai'])
        self.assertIn(('post', new['id']), self.calls)
        self.assertNotIn(('post', old['id']), self.calls, 'the held first-half source is not refetched')
        self.assertTrue(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])

    def test_rebased_cohort_with_full_cursor_still_searches_and_never_twice(self):
        old = self.first_half_held()
        self.now = at(13)
        self.documents = {'amu_zettai': base.document(old)}
        self.reopen()
        self.run_cycle(cycle_id='3002-1')
        cohort = self.state['collection']
        self.assertEqual((cohort['cursor'], len(cohort['names'])), (1, 1))
        self.assertEqual(cohort['periods'], [['2026-10-16', '2026-10-31'], ['2026-10-01', '2026-10-15']])
        self.assertFalse(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])
        # 10/17: the rebased chain resumes with cursor == len(names); the second half is still due.
        self.now = at(17)
        new = self.post(at(16, 21), 3, '10月後半のお給仕予定')
        self.documents = {'amu_zettai': base.document(new, old)}
        self.model.structured.return_value = self.second_half_result()
        self.reopen()
        self.calls.clear()
        report, _ = self.run_cycle(resume=True, cycle_id='3002-1')
        self.assertEqual(self.searches(), ['amu_zettai'], 'one search per person per run')
        self.assertTrue(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])
        self.assertEqual(report['continuation']['chainId'], '3002-1')
        # Within the six-hour check window a resumed run does not search again.
        self.now += dt.timedelta(hours=1)
        self.reopen()
        self.calls.clear()
        self.state['schedules'] = list(self.state['schedules'])
        self.run_cycle(resume=True, cycle_id='3002-1')
        self.assertEqual(self.searches(), [])

    def test_check_window_and_ended_first_half_never_search(self):
        self.now = at(13)
        self.documents = {'amu_zettai': base.document()}
        self.run_cycle(cycle_id='3003-1')
        self.assertEqual(self.searches(), ['amu_zettai'])
        self.now = at(13, 14)
        self.reopen()
        self.calls.clear()
        self.run_cycle(resume=True, cycle_id='3003-1')
        self.assertEqual(self.searches(), [], 'nextCheckAt is still in the future')
        # 10/17: only the open second half is checked; the ended first half adds no search.
        self.now = at(17)
        self.reopen()
        self.calls.clear()
        self.run_cycle(resume=True, cycle_id='3003-1')
        self.assertEqual(self.searches(), ['amu_zettai'])
        first = self.state['coverage']['2026-10-01']['あむ']
        self.assertLess(facts.timestamp(first['lastSearchedAt']), at(17))


class CandidateTerminationTests(DiscoveryBase):
    def test_candidates_for_ended_periods_retire_without_http_or_ai(self):
        self.now = at(13)
        self.documents = {'amu_zettai': base.document()}
        self.run_cycle(cycle_id='3004-1')
        stale_entry = self.post(dt.datetime(2026, 9, 20, 9, tzinfo=facts.JST).astimezone(facts.UTC),
                                4, '9月後半のお給仕予定')
        candidates, _ = collector.discover(base.document(stale_entry), {'あむ': base.TARGET}, self.now, {},
                                           registry=self.registry, candidate_since=dt.date(2026, 9, 16))
        self.assertEqual(collector.enqueue(self.state, candidates, self.now), set())
        self.assertEqual([item['id'] for item in self.state['pending']], [stale_entry['id']])
        # 10/17 resume of the 10/13 cohort: 9/20 can only describe ended periods.
        self.now = at(17)
        self.reopen()
        self.calls.clear()
        calls_before = self.usage.calls
        self.run_cycle(resume=True, cycle_id='3004-1')
        self.assertNotIn(('post', stale_entry['id']), self.calls)
        self.assertEqual(self.usage.calls, calls_before)
        self.assertEqual(self.state['pending'], [])
        history = [row for row in self.state['candidateHistory'].values()
                   if row['candidate']['id'] == stale_entry['id']]
        self.assertEqual([row['reason'] for row in history], ['stale_candidate'])
        # A later search that sees it again cannot re-enqueue it.
        self.documents = {'amu_zettai': base.document(stale_entry)}
        self.now = at(17, 17)
        self.reopen()
        self.run_cycle(resume=True, cycle_id='3004-1')
        self.assertEqual(self.state['pending'], [])
        self.assertNotIn(('post', stale_entry['id']), self.calls)

    @mock.patch.object(collector, 'HISTORY_RELEASE_BEFORE', at(1))
    def test_photo_owner_mismatch_is_terminal_after_one_body_get(self):
        self.now = at(17)
        entry = self.post(at(16, 21), 5, '10月後半のお給仕予定')
        self.payloads[entry['id']]['mediaDetails'][0]['expanded_url'] = (
            f'https://x.com/someone_else/status/{entry["id"]}/photo/1')
        self.documents = {'amu_zettai': base.document(entry)}
        report, _ = self.run_cycle(cycle_id='3005-1')
        self.assertEqual(self.calls.count(('post', entry['id'])), 1)
        self.assertEqual(self.usage.calls, 0)
        self.assertEqual(self.state['pending'], [])
        history = [row for row in self.state['candidateHistory'].values()
                   if row['candidate']['id'] == entry['id']]
        self.assertEqual([row['reason'] for row in history], ['permanent_failure'])
        diagnostic = next(row for row in report['diagnostics'] if row['postId'] == entry['id'])
        self.assertEqual((diagnostic['reason'], diagnostic['detail'], diagnostic['nextStage']),
                         ('permanent_failure', 'photo_owner_mismatch', 'held'))
        facts.validate_state(self.state)
        # The next due search finds the same post again, but it is never re-fetched.
        self.now = at(17, 17)
        self.reopen()
        self.run_cycle(resume=True, cycle_id='3005-1')
        self.assertEqual(self.calls.count(('post', entry['id'])), 1)


class ReadingCapacityTests(DiscoveryBase):
    """October 18-25 outage: 240 finished readings made every later save fail validation."""

    def stale_history(self):
        self.now = at(13)
        self.documents = {'amu_zettai': base.document()}
        self.run_cycle(cycle_id='3101-1')
        stale = self.post(dt.datetime(2026, 9, 20, 9, tzinfo=facts.JST).astimezone(facts.UTC),
                          4, '9月後半のお給仕予定')
        candidates, _ = collector.discover(base.document(stale), {'あむ': base.TARGET}, self.now, {},
                                           registry=self.registry, candidate_since=dt.date(2026, 9, 16))
        collector.enqueue(self.state, candidates, self.now)
        self.now = at(17)
        self.reopen()
        self.run_cycle(resume=True, cycle_id='3101-1')
        self.assertEqual(self.state['pending'], [])
        return stale

    def fill(self, source_id, count, stage):
        for index in range(count):
            key = facts.digest(f'synthetic-reading-{index}'.encode())
            self.state['readings'][key] = {'sourceId': source_id, 'cacheKey': key, 'stage': stage,
                                           'reason': 'not_schedule' if stage == 'done' else 'reading_pending',
                                           'nextAt': None, 'attemptedVariants': []}

    def test_full_reading_table_completes_saves_and_retires_finished_rows(self):
        stale = self.stale_history()
        self.fill(stale['id'], 240, 'done')
        new = self.post(at(16, 21), 6, '10月後半のお給仕予定')
        self.documents = {'amu_zettai': base.document(new)}
        self.model.structured.return_value = self.second_half_result()
        self.now = at(18)
        self.reopen()
        self.calls.clear()
        calls = self.usage.calls
        report, _ = self.run_cycle(cycle_id='3102-1')
        self.assertEqual(report['retiredReadings'], 240)
        self.assertEqual(report['readings'], 1)
        self.assertEqual(self.usage.calls, calls + 1)
        self.assertEqual(self.calls.count(('post', new['id'])), 1)
        self.assertNotIn(('post', stale['id']), self.calls)
        self.assertTrue(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])
        facts.validate_state(self.state)

    def test_retired_reading_never_refetches_or_reanalyses_its_source(self):
        self.now = at(17)
        entry = self.post(at(16, 21), 7, '10月後半のお給仕予定')
        self.documents = {'amu_zettai': base.document(entry)}
        self.model.structured.return_value = self.second_half_result()
        self.run_cycle(cycle_id='3103-1')
        self.assertEqual([row['stage'] for row in self.state['readings'].values()], ['done'])
        calls, posts = self.usage.calls, self.calls.count(('post', entry['id']))
        # The next day the finished reading is retired; the search sees the same post again.
        self.now = at(18, 12)
        self.state['coverage'][SECOND_HALF]['あむ'].update(confirmedIds=[], nextCheckAt=None)
        self.reopen()
        report, _ = self.run_cycle(cycle_id='3104-1')
        self.assertEqual((report['retiredReadings'], report['readings']), (1, 0))
        self.assertIn(('search', 'amu_zettai'), self.calls)
        self.assertEqual(self.calls.count(('post', entry['id'])), posts)
        self.assertEqual(self.usage.calls, calls)
        self.assertEqual(self.state['pending'], [])
        candidates, _ = collector.discover(base.document(entry), {'あむ': base.TARGET}, self.now, {},
                                           registry=self.registry, candidate_since=dt.date(2026, 10, 1))
        collector.enqueue(self.state, candidates, self.now, self.analyzer)
        self.assertEqual(self.state['pending'], [], 'the terminal source record blocks re-queueing')

    def test_queued_unsettled_or_unfinished_readings_are_never_retired(self):
        stale = self.stale_history()
        self.fill(stale['id'], 3, 'done')
        keys = list(self.state['readings'])
        self.state['readings'][keys[0]]['stage'] = 'original'
        queued = self.post(at(16, 21), 8, '10月後半のお給仕予定')
        self.state['readings'][keys[1]]['sourceId'] = queued['id']
        self.state['pending'].append({**collector.discover(
            base.document(queued), {'あむ': base.TARGET}, self.now, {}, registry=self.registry,
            candidate_since=dt.date(2026, 10, 1))[0][0]})
        self.assertEqual(collector.retirable_readings(self.state, self.analyzer), [keys[2]])
        open_receipt = mock.Mock(state={'receipts': {'r': {'requestHash': 'a' * 64, 'completedAt': None}}})
        self.cache.records[keys[2]] = {'value': {'reading': {'issued': ['a' * 64]}}}
        self.assertEqual(collector.retirable_readings(self.state, mock.Mock(usage=open_receipt), self.cache), [])

    def test_capacity_without_retirable_readings_waits_without_fetching(self):
        stale = self.stale_history()
        self.fill(stale['id'], facts.READINGS_LIMIT, 'fetch')
        new = self.post(at(16, 21), 9, '10月後半のお給仕予定')
        self.documents = {'amu_zettai': base.document(new)}
        self.now = at(18)
        self.reopen()
        self.calls.clear()
        calls = self.usage.calls
        report, _ = self.run_cycle(cycle_id='3105-1')
        self.assertIn('reading_capacity', report['reasons'])
        self.assertEqual((report['retiredReadings'], report['readings']), (0, facts.READINGS_LIMIT))
        self.assertNotIn(('post', new['id']), self.calls)
        self.assertEqual(self.usage.calls, calls)
        waiting = next(item for item in self.state['pending'] if item['id'] == new['id'])
        self.assertEqual(facts.timestamp(waiting['nextAttemptAt']), at(18) + dt.timedelta(hours=6))
        self.assertEqual(self.state['coverage'][SECOND_HALF]['あむ']['reason'], 'reading_capacity')
        facts.validate_state(self.state)
        self.state['readings'][facts.digest(b'one-too-many')] = dict(
            next(iter(self.state['readings'].values())), cacheKey=facts.digest(b'one-too-many'))
        with self.assertRaisesRegex(ValueError, 'invalid_readings'):
            facts.validate_state(self.state)


class QueueBoundTests(DiscoveryBase):
    """Each queue bound reached at the start of a run: the run completes, saves and reports it."""

    def queued(self, count, *, finished=True, first_suffix=20):
        self.documents = {'amu_zettai': base.document()}
        self.run_cycle(cycle_id='3200-1')
        entries = [self.post(at(14, 9) + dt.timedelta(minutes=index), first_suffix + index, '雑談')
                   for index in range(count)]
        candidates, _ = collector.discover(base.document(*entries), {'あむ': base.TARGET}, self.now, {},
                                           registry=self.registry, candidate_since=dt.date(2026, 10, 1))
        self.state['pending'].extend(candidates)
        if finished:
            for item in candidates:
                key = facts.digest(('reading-' + item['id']).encode())
                self.state['readings'][key] = {'sourceId': item['id'], 'cacheKey': key, 'stage': 'held',
                                               'reason': 'reading_uncertain', 'nextAt': None,
                                               'attemptedVariants': []}
        facts.validate_state(self.state)
        return candidates

    def run_with_new_post(self):
        self.now = at(18)
        new = self.post(at(16, 21), 90, '10月後半のお給仕予定')
        self.documents = {'amu_zettai': base.document(new)}
        self.model.structured.return_value = self.second_half_result()
        self.reopen()
        self.calls.clear()
        report, _ = self.run_cycle(cycle_id='3201-1')
        facts.validate_state(self.state)
        return new, report

    def assert_new_post_read_and_old_never_refetched(self, new, old, report):
        self.assertEqual(self.calls.count(('post', new['id'])), 1)
        self.assertFalse(any(('post', item['id']) in self.calls for item in old))
        self.assertTrue(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])
        self.assertEqual(report['capacity'], {'finishedCandidatesRetired': 1})
        retired = [row for row in self.state['candidateHistory'].values() if row['candidate']['id'] == old[0]['id']]
        self.assertEqual([row['reason'] for row in retired], ['reading_uncertain'])
        # The retired candidate cannot be queued again by a later search.
        collector.enqueue(self.state, [old[0]], self.now)
        self.assertNotIn(old[0]['id'], [item['id'] for item in self.state['pending']])

    def test_per_person_bound_full_of_finished_candidates_admits_the_new_post(self):
        self.now = at(14, 12)
        old = self.queued(facts.PENDING_PER_NAME)
        new, report = self.run_with_new_post()
        self.assert_new_post_read_and_old_never_refetched(new, old, report)

    def test_global_bound_full_of_finished_candidates_admits_the_new_post(self):
        self.now = at(14, 12)
        with mock.patch.object(facts, 'PENDING_LIMIT', 5), mock.patch.object(facts, 'PENDING_PER_NAME', 50):
            old = self.queued(5)
            new, report = self.run_with_new_post()
            self.assertLessEqual(len(self.state['pending']), 5)
        self.assert_new_post_read_and_old_never_refetched(new, old, report)

    def test_bound_full_of_unfinished_candidates_defers_visibly_without_error(self):
        self.now = at(14, 12)
        self.queued(facts.PENDING_PER_NAME, finished=False, first_suffix=40)
        older = self.post(at(13, 9), 30, '10月後半のお給仕予定')
        candidates, _ = collector.discover(base.document(older), {'あむ': base.TARGET}, self.now, {},
                                           registry=self.registry, candidate_since=dt.date(2026, 10, 1))
        capacity = {}
        self.assertEqual(collector.enqueue(self.state, candidates, self.now, None, capacity), {'あむ'})
        self.assertEqual(capacity, {'queueLimitDeferred': 1})
        self.assertEqual(len(self.state['pending']), facts.PENDING_PER_NAME)
        self.assertEqual(self.state['coverage'][SECOND_HALF]['あむ']['reason'], 'queue_limit')
        facts.validate_state(self.state)
        newer = self.post(at(14, 11), 99, '10月後半のお給仕予定')
        candidates, _ = collector.discover(base.document(newer), {'あむ': base.TARGET}, self.now, {},
                                           registry=self.registry, candidate_since=dt.date(2026, 10, 1))
        self.assertEqual(collector.enqueue(self.state, candidates, self.now, None, capacity), set())
        self.assertEqual(capacity['queueLimitReplaced'], 1)
        facts.validate_state(self.state)


class PostRecoveryTests(DiscoveryBase):
    """10/25 findings: real schedule posts stuck by defects, not by their content."""

    def test_multi_photo_post_reports_every_photo_as_photo_one(self):
        candidate = collector.discover(base.document(base.entry()), {'あむ': base.TARGET}, base.NOW)[0][0]
        payload = base.payload(photos=2)
        for detail in payload['mediaDetails']:
            detail['expanded_url'] = detail['expanded_url'].rsplit('/', 1)[0] + '/1'
        verified, _, urls = collector.validate_post(candidate, payload, base.TARGET, base.NOW)
        self.assertEqual(len(urls), 2)
        for path, error in [('someone_else/status/{tid}/photo/1', 'photo_owner_mismatch'),
                            ('amu_zettai/status/1/photo/1', 'photo_owner_mismatch'),
                            ('amu_zettai/status/{tid}/photo/3', 'photo_owner_mismatch')]:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, error):
                bad = base.payload(photos=2)
                bad['mediaDetails'][1]['expanded_url'] = 'https://x.com/' + path.format(tid=candidate['id'])
                collector.validate_post(candidate, bad, base.TARGET, base.NOW)

    def history_case(self, reason, checked, priority=0):
        self.now = at(17)
        self.documents = {'amu_zettai': base.document()}
        self.run_cycle(cycle_id='3301-1')
        entry = self.post(at(16, 21), 70, '10月後半のお給仕予定')
        candidate = collector.discover(base.document(entry), {'あむ': base.TARGET}, self.now, {},
                                       registry=self.registry, candidate_since=dt.date(2026, 10, 1))[0][0]
        candidate['priority'] = priority
        self.state['candidateHistory'][facts.candidate_key(candidate)] = {
            'candidate': candidate, 'reason': reason, 'checkedAt': facts.stamp(checked)}
        self.model.structured.return_value = self.second_half_result()
        self.now = at(17, 18)
        self.reopen()
        self.calls.clear()
        self.run_cycle(cycle_id='3302-1')
        facts.validate_state(self.state)
        return entry

    def test_defect_era_history_is_released_once_and_read(self):
        for reason in ('permanent_failure', 'queue_limit'):
            with self.subTest(reason=reason):
                self.setUp()
                entry = self.history_case(reason, at(17, 9))
                self.assertEqual(self.calls.count(('post', entry['id'])), 1)
                self.assertTrue(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])

    def test_low_priority_queue_limit_with_an_unissued_image_source_is_released(self):
        self.now = at(17)
        self.documents = {'amu_zettai': base.document()}
        self.run_cycle(cycle_id='3701-1')
        entry = self.post(at(16, 21), 71, 'お待たせしました')
        candidate = collector.discover(base.document(entry), {'あむ': base.TARGET}, self.now, {},
                                       registry=self.registry, candidate_since=dt.date(2026, 10, 1))[0][0]
        candidate['priority'] = 1
        verified, _, _ = collector.validate_post(candidate, base.payload(created=at(16, 21), suffix=71),
                                                 base.TARGET, self.now)
        facts.record_source(self.state, verified, 'pending', 'not_issued', self.now)
        self.state['candidateHistory'][facts.candidate_key(candidate)] = {
            'candidate': candidate, 'reason': 'queue_limit', 'checkedAt': facts.stamp(at(17, 9))}
        self.model.structured.return_value = self.second_half_result()
        self.now = at(17, 18)
        self.reopen()
        self.calls.clear()
        self.run_cycle(cycle_id='3702-1')
        self.assertIn(entry['id'], [item['id'] for item in self.state['pending']]
                      + [item['id'] for item in self.state['schedules']])
        facts.validate_state(self.state)

    def test_history_after_the_fix_or_low_priority_queue_limit_stays_retired(self):
        for reason, priority, release_before in (('permanent_failure', 0, at(17, 0)),
                                                 ('queue_limit', 1, collector.HISTORY_RELEASE_BEFORE),
                                                 ('not_schedule', 0, collector.HISTORY_RELEASE_BEFORE)):
            with self.subTest(reason=reason, priority=priority):
                self.setUp()
                with mock.patch.object(collector, 'HISTORY_RELEASE_BEFORE', release_before):
                    entry = self.history_case(reason, at(17, 9), priority)
                self.assertNotIn(('post', entry['id']), self.calls)

    def expired_without_record(self, *, issued):
        self.now = at(17)
        entry = self.post(at(16, 21), 80, '10月後半のお給仕予定')
        self.documents = {'amu_zettai': base.document(entry)}
        self.client_image = None
        failing = self.client
        def client():
            value = failing()
            value.image.side_effect = OSError('host paused')
            return value
        self.client = client
        self.run_cycle(cycle_id='3401-1')
        self.client = failing
        progress = next(iter(self.state['readings'].values()))
        self.assertEqual(progress['stage'], 'fetch')
        progress.update(stage='held', reason='image_cache_expired', nextAt=None)
        if issued:
            self.state['sources'][progress['cacheKey']].update(status='issued', requestHash='a' * 64)
        self.cache.records.pop(progress['cacheKey'])
        self.cache.put('f' * 64, {'id': 'other', 'authorId': 'other'}, 'other', [])
        self.model.structured.return_value = self.second_half_result()
        self.now = at(17, 12)
        self.reopen()
        self.calls.clear()
        calls = self.usage.calls
        self.run_cycle(cycle_id='3402-1')
        facts.validate_state(self.state)
        return entry, calls

    def test_pruned_cache_before_any_ai_request_is_refetched_once(self):
        entry, calls = self.expired_without_record(issued=False)
        self.assertEqual(self.calls.count(('post', entry['id'])), 1)
        self.assertEqual(self.usage.calls, calls + 1)
        self.assertTrue(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])

    def test_pruned_cache_after_an_ai_request_is_never_refetched(self):
        entry, calls = self.expired_without_record(issued=True)
        self.assertNotIn(('post', entry['id']), self.calls)
        self.assertEqual(self.usage.calls, calls)

    def test_expired_record_whose_run_only_read_other_posts_is_recovered(self):
        """Production 10/17 runs: the post was fetched in a run that read other posts."""
        self.now = at(17)
        entry = self.post(at(16, 21), 81, '10月後半のお給仕予定')
        self.documents = {'amu_zettai': base.document(entry)}
        failing = self.client
        def client():
            value = failing()
            value.image.side_effect = OSError('host paused')
            return value
        self.client = client
        self.run_cycle(cycle_id='3601-1')
        self.client = failing
        progress = next(iter(self.state['readings'].values()))
        progress.update(stage='held', reason='image_cache_expired', nextAt=None)
        source_record = self.state['sources'][progress['cacheKey']]['source']
        run = '3601-1'
        receipts = {
            # text-only request of another candidate after this post was fetched
            'text': {'component': 'schedule', 'runId': run, 'requestHash': 'b' * 64,
                     'reservedAt': facts.stamp(at(17) + dt.timedelta(minutes=5)),
                     'money': {'imageInput': False}},
            # image request reserved before this post was fetched
            'early': {'component': 'schedule', 'runId': run, 'requestHash': 'c' * 64,
                      'reservedAt': facts.stamp(facts.timestamp(source_record['observedAt']) - dt.timedelta(minutes=9)),
                      'money': {'imageInput': True}}}
        self.usage.state = {'receipts': receipts}
        self.source.state['receipts'] = {'post': {
            'component': 'schedule', 'kind': 'posts', 'runId': run, 'status': 'ok', 'httpStatus': 200,
            'issuedAt': source_record['observedAt'], 'completedAt': source_record['observedAt'],
            'requestHash': collector.source_safety.request_identity(
                'posts', f'https://{collector.POST_HOST}/tweet-result?id={entry["id"]}&lang=ja&token=a')[0]}}
        self.now = at(25)
        self.reopen()
        self.analyzer.usage.state = {'receipts': receipts}
        self.assertIsNotNone(collector.expired_unissued_receipt(
            self.state, progress, self.cache, self.source, self.analyzer, self.now))
        # An unattributed image request after the fetch could be this post's reading: refuse.
        receipts['late'] = {'component': 'schedule', 'runId': run, 'requestHash': 'd' * 64,
                            'reservedAt': facts.stamp(facts.timestamp(source_record['observedAt'])
                                                      + dt.timedelta(minutes=1)),
                            'money': {'imageInput': True}}
        self.assertIsNone(collector.expired_unissued_receipt(
            self.state, progress, self.cache, self.source, self.analyzer, self.now))
        self.assertEqual(collector.expired_unissued_receipt.refusal, 'recovery_ai_in_run')


class TextPrefilterTests(DiscoveryBase):
    def test_plausible_schedule_text(self):
        for text in ('10月後半のお給仕予定', '16日 19日 21日 全部', '18日夜にゃん', '10/16 10/18',
                     'あ、あと31日夜お給仕いる', '１６日　２０日'):
            self.assertTrue(collector.plausible_schedule_text(text), text)
        for text in ('スイートポテトつくる！', '今日は16日です', 'https://t.co/abc', ''):
            self.assertFalse(collector.plausible_schedule_text(text), text)

    def test_text_only_chatter_ends_without_ai_and_is_never_requeued(self):
        self.now = at(17)
        entry = self.post(at(16, 21), 60, 'スイートポテトつくる！')
        self.payloads[entry['id']].update(photos=[], mediaDetails=[])
        self.documents = {'amu_zettai': base.document(entry)}
        calls = self.usage.calls
        self.run_cycle(cycle_id='3501-1')
        self.assertEqual(self.calls.count(('post', entry['id'])), 1)
        self.assertEqual(self.usage.calls, calls)
        self.assertEqual(self.state['pending'], [])
        self.assertEqual([row['status'] for row in self.state['sources'].values()
                          if row['source']['id'] == entry['id']], ['negative'])
        facts.validate_state(self.state)


class PriorityTests(DiscoveryBase):
    def test_open_period_searches_and_candidates_precede_first_half_residue(self):
        self.registry['members'].append(collector.members.new_member(
            '新人', 'https://x.com/new_member', base.NOW, member_id='m-' + '2' * 32))
        self.now = at(13)
        self.documents = {'amu_zettai': base.document(), 'new_member': base.document()}
        self.run_cycle(cycle_id='3006-1')
        first = self.post(at(5), 6, '10月前半のお給仕予定')
        second = self.post(at(14), 7, '10月後半のお給仕予定')
        self.now = at(17)
        candidates, _ = collector.discover(base.document(first, second), {'あむ': base.TARGET}, self.now, {},
                                           registry=self.registry, candidate_since=dt.date(2026, 10, 1))
        collector.enqueue(self.state, candidates, self.now)
        newcomer = base.entry(created=at(16), suffix=8, handle='new_member')
        newcomer['userId'] = '2080944098043977778'
        self.documents['new_member'] = base.document(newcomer)
        self.model.structured.return_value = {'classification': 'uncertain', 'complete': False, 'periods': []}
        payload = base.payload(created=at(16), suffix=8)
        payload['user'] = {'id_str': newcomer['userId'], 'screen_name': 'new_member'}
        payload['mediaDetails'][0]['expanded_url'] = f'https://x.com/new_member/status/{newcomer["id"]}/photo/1'
        self.payloads[newcomer['id']] = payload
        self.reopen()
        self.calls.clear()
        self.run_cycle(resume=True, cycle_id='3006-1')
        order = [call for call in self.calls if call[0] in ('search', 'post')]
        # あむ has a queued second-half candidate, so only the newcomer is searched.
        self.assertEqual([arg for kind, arg in order if kind == 'search'], ['new_member'])
        self.assertLess(order.index(('search', 'new_member')), order.index(('post', second['id'])))
        self.assertIn(('post', newcomer['id']), order)
        # The first-half table can no longer describe an open half: retired without HTTP or AI.
        self.assertNotIn(('post', first['id']), order)
        self.assertNotIn(first['id'], {item['id'] for item in self.state['pending']})
        self.assertEqual({row['candidate']['id']: row['reason'] for row in self.state['candidateHistory'].values()
                          }.get(first['id']), 'stale_candidate')


def jst(month, day, hour=10):
    return dt.datetime(2026, month, day, hour, tzinfo=facts.JST).astimezone(facts.UTC)


class CandidateScopeTests(DiscoveryBase):
    """11/01 findings: old posts filled each 12-candidate queue and used the scarce image attempts."""

    GRADUATE_ID = '2080944098043977778'

    def candidates(self, *specs, name='あむ', handle='amu_zettai', uid=None):
        entries = []
        for created, suffix, priority in specs:
            entry = self.post(created, suffix, '雑談', handle=handle)
            if uid is not None:
                entry['userId'] = uid
            entries.append(entry)
        found, _ = collector.discover(base.document(*entries), {name: {'name': name, 'handle': handle}},
                                      self.now, {}, registry=self.registry, candidate_since=dt.date(2026, 9, 1))
        by_id = {item['id']: item for item in found}
        result = []
        for (_, _, priority), entry in zip(specs, entries):
            by_id[entry['id']]['priority'] = priority
            result.append(by_id[entry['id']])
        return result

    def reading(self, item, stage, reason):
        key = facts.digest(('reading-' + item['id']).encode())
        self.state.setdefault('readings', {})[key] = {
            'sourceId': item['id'], 'cacheKey': key, 'stage': stage,
            'reason': reason, 'nextAt': None, 'attemptedVariants': []}

    def pending_ids(self):
        return [item['id'] for item in self.state['pending']]

    def history(self):
        return {row['candidate']['id']: row['reason'] for row in self.state['candidateHistory'].values()}

    def test_candidates_published_before_every_open_half_are_retired_once_without_http_or_ai(self):
        self.now = at(13)
        self.documents = {'amu_zettai': base.document()}
        self.run_cycle(cycle_id='3700-1')
        self.now = at(17)
        old, fetched, issued, fresh = self.candidates((at(5), 1, 0), (at(6), 2, 0), (at(7), 3, 0), (at(14), 4, 1))
        self.state['pending'].extend([old, fetched, issued, fresh])
        self.reading(fetched, 'fetch', 'image_host_paused')
        self.reading(issued, 'held', 'reading_uncertain')
        facts.validate_state(self.state)
        self.model.structured.return_value = self.second_half_result()
        self.reopen()
        self.calls.clear()
        report, _ = self.run_cycle(resume=True, cycle_id='3700-1')
        self.assertEqual(report['scopeRetired'], 2)
        self.assertEqual((self.history().get(old['id']), self.history().get(fetched['id'])),
                         ('stale_candidate', 'stale_candidate'))
        # A candidate an AI request may belong to stays as it is.
        self.assertNotIn(issued['id'], self.history())
        self.assertIn(issued['id'], self.pending_ids())
        # Its started reading is no longer live: held, then retired like any finished reading.
        self.assertFalse([row for row in self.state['readings'].values()
                          if row['sourceId'] == fetched['id'] and row['stage'] not in ('held', 'done')])
        for item in (old, fetched, issued):
            self.assertNotIn(('post', item['id']), self.calls)
        self.assertTrue(all('SYNTHETIC_4_' in arg for kind, arg in self.calls if kind == 'image'))
        self.assertEqual(self.calls.count(('post', fresh['id'])), 1)
        self.assertEqual(self.usage.calls, 1)
        self.assertTrue(self.state['coverage'][SECOND_HALF]['あむ']['confirmedIds'])
        # Retired once: a later search cannot queue it again, and the next run retires nothing.
        collector.enqueue(self.state, [old], self.now)
        self.assertNotIn(old['id'], self.pending_ids())
        self.reopen()
        report, _ = self.run_cycle(resume=True, cycle_id='3700-1')
        self.assertEqual(report['scopeRetired'], 0)
        facts.validate_state(self.state)

    def test_full_queue_pushes_out_older_unissued_candidates_so_new_posts_are_kept(self):
        self.now = at(20)
        queue = self.candidates(*[(at(14, 9) + dt.timedelta(minutes=index), 20 + index, 0)
                                  for index in range(facts.PENDING_PER_NAME)])
        queue[3]['priority'] = 1
        self.state['pending'].extend(queue)
        self.reading(queue[0], 'original', 'reading_pending')
        facts.validate_state(self.state)
        capacity = {}
        newer = self.candidates((at(19), 90, 0))[0]
        self.assertEqual(collector.enqueue(self.state, [newer], self.now, None, capacity), set())
        # The new post is kept; an older one without a schedule keyword goes first.
        self.assertIn(newer['id'], self.pending_ids())
        self.assertNotIn(queue[3]['id'], self.pending_ids())
        self.assertEqual(self.history()[queue[3]['id']], 'queue_limit')
        newest = self.candidates((at(19, 11), 91, 0))[0]
        collector.enqueue(self.state, [newest], self.now, None, capacity)
        # Then the oldest; the candidate with an AI reading in progress is never pushed out.
        self.assertIn(newest['id'], self.pending_ids())
        self.assertNotIn(queue[1]['id'], self.pending_ids())
        self.assertIn(queue[0]['id'], self.pending_ids())
        self.assertEqual(capacity['queueLimitReplaced'], 2)
        self.assertEqual(len(self.pending_ids()), facts.PENDING_PER_NAME)
        # A post older than every unissued queued candidate is the one deferred.
        older = self.candidates((at(13, 9), 92, 0))[0]
        self.assertEqual(collector.enqueue(self.state, [older], self.now, None, capacity), {'あむ'})
        self.assertNotIn(older['id'], self.pending_ids())
        # A batch keeps the newest posts.
        batch = self.candidates(*[(at(19, 12) + dt.timedelta(minutes=index), 60 + index, 1)
                                  for index in range(facts.PENDING_PER_NAME + 2)])
        collector.enqueue(self.state, batch, self.now)
        self.assertEqual(sorted(self.pending_ids(), key=int),
                         sorted([queue[0]['id'], *[item['id'] for item in batch[3:]]], key=int))
        facts.validate_state(self.state)

    def test_posts_for_the_latest_open_half_are_read_first_newest_and_keyword_first(self):
        self.now = at(29)
        self.documents = {'amu_zettai': base.document()}
        self.run_cycle(cycle_id='3800-1')
        self.assertEqual(self.state['collection']['periods'], [['2026-11-01', '2026-11-15'], ['2026-10-16', '2026-10-31']])
        self.now = jst(11, 1)
        ending, plain, table, old = self.candidates((at(20), 1, 0), (at(29, 12), 2, 1), (at(30), 3, 0), (at(10), 4, 0))
        self.state['pending'].extend([ending, plain, table, old])
        self.model.structured.return_value = {'classification': 'uncertain', 'complete': False, 'periods': []}
        self.reopen()
        self.calls.clear()
        self.run_cycle(resume=True, cycle_id='3800-1')
        posts = [arg for kind, arg in self.calls if kind == 'post']
        self.assertEqual(posts, [table['id'], plain['id'], ending['id']])
        images = [arg for kind, arg in self.calls if kind == 'image']
        self.assertEqual(len(images), 3)
        self.assertFalse(any('SYNTHETIC_4_' in url for url in images))
        self.assertEqual(self.history()[old['id']], 'stale_candidate')

    def test_graduate_leaves_the_halves_after_her_last_day(self):
        self.registry['members'].append(collector.members.new_member(
            '卒業', 'https://x.com/graduate', base.NOW, member_id='m-' + '2' * 32))
        self.registry['members'][-1]['graduation'] = {
            'lastServiceDate': '2026-10-31', 'evidenceUrl': 'https://x.com/graduate/status/2109664298628944019'}
        self.now = at(29)
        self.documents = {'amu_zettai': base.document(), 'graduate': base.document()}
        report, _ = self.run_cycle(cycle_id='3900-1')
        self.assertIn('卒業', report['continuation']['names'])
        self.assertIn('graduate', self.searches())
        self.assertIn('卒業', self.state['coverage']['2026-10-16'])
        self.assertNotIn('卒業', self.state['coverage']['2026-11-01'])
        mine = self.candidates((at(20), 1, 0), (at(29, 9), 2, 0), name='卒業', handle='graduate', uid=self.GRADUATE_ID)
        self.state['pending'].extend(mine)
        facts.validate_state(self.state)
        self.now = jst(11, 2)
        self.reopen()
        self.calls.clear()
        report, _ = self.run_cycle(resume=True, cycle_id='3900-1')
        self.assertNotIn('graduate', self.searches())
        self.assertFalse(any(call == ('post', item['id']) for item in mine for call in self.calls))
        self.assertEqual({self.history()[item['id']] for item in mine}, {'stale_candidate'})
        self.assertFalse(any(item['name'] == '卒業' for item in self.state['pending']))
        self.assertNotIn('卒業', self.state['coverage']['2026-11-01'])
        self.assertEqual(report['unresolvedCount'], 1)
        facts.validate_state(self.state)


class MemberStatusSummaryTests(unittest.TestCase):
    def test_image_wait_and_queued_candidates_are_reported_separately(self):
        now = jst(11, 2)
        stamp = facts.stamp
        rows = {}

        def candidate(name, suffix, created, wait=None):
            return {'id': base.post_id(created, suffix), 'name': name, 'searchCreatedAt': stamp(created),
                    'nextAttemptAt': stamp(wait) if wait else None}

        def reading(item, stage, wait=None):
            key = facts.digest(item['id'].encode())
            rows[key] = {'sourceId': item['id'], 'cacheKey': key, 'stage': stage, 'reason': 'image_host_paused',
                         'nextAt': stamp(wait) if wait else None, 'attemptedVariants': []}
        later = now + dt.timedelta(minutes=40)
        pending = [candidate('いち', 1, jst(10, 29)), candidate('に', 2, jst(10, 30), later),
                   candidate('に', 3, jst(10, 30)), candidate('に', 4, jst(10, 31)), candidate('さん', 5, jst(10, 29), later),
                   candidate('よん', 6, jst(10, 29)), candidate('よん', 7, jst(10, 30)), candidate('ご', 8, jst(10, 29)),
                   candidate('ろく', 9, jst(10, 10))]
        reading(pending[1], 'fetch', later)
        reading(pending[3], 'fetch')
        reading(pending[4], 'fetch', later)
        reading(pending[7], 'held')
        coverage = {name: {'confirmedIds': ['1'] if name == 'いち' else [], 'reason': reason}
                    for name, reason in (('いち', 'valid_schedule'), ('に', 'image_host_paused'),
                                         ('さん', 'image_host_paused'), ('よん', 'post_unverified'),
                                         ('ご', 'reading_uncertain'), ('ろく', 'post_unverified'),
                                         ('なな', 'source_not_found'))}
        state = {'coverage': {'2026-11-01': coverage}, 'pending': pending, 'readings': rows}
        names = ['いち', 'に', 'さん', 'よん', 'ご', 'ろく', 'なな', '卒業']
        status = cloud.half_month_member_status(state, names, ('2026-11-01', '2026-11-15'), now, legacy.collector)
        self.assertEqual(status['states'], {'confirmed': 1, 'queued': 1, 'image_wait': 1, 'image_wait_and_queued': 1,
                                            'reading': 0, 'reading_held': 1, 'no_candidate': 2})
        self.assertEqual(status['candidates'], {'queued': 3, 'imageWait': 2, 'imageDue': 1, 'reading': 0,
                                                'readingHeld': 1, 'outOfPeriod': 1})
        by_name = {row['name']: row for row in status['members']}
        self.assertEqual((by_name['に']['state'], by_name['に']['queued'], by_name['に']['imageWait'],
                          by_name['に']['imageDue']), ('image_wait_and_queued', 1, 1, 1))
        self.assertEqual((by_name['よん']['state'], by_name['さん']['state'], by_name['ろく']['outOfPeriod']),
                         ('queued', 'image_wait', 1))
        self.assertNotIn('いち', by_name)
        self.assertNotIn('卒業', by_name)
        summary = Path(tempfile.mkdtemp(prefix='member-summary-')) / 'summary.md'
        self.addCleanup(shutil.rmtree, summary.parent, True)
        with contextlib.redirect_stdout(io.StringIO()):
            cloud.emit({'halfMonthAcquisition': {'period': '2026-11-01', 'searched': 7, 'targets': 7, 'confirmed': 1,
                                                 'pending': 9, 'scopeRetired': 4},
                        'halfMonthMembers': status}, {'GITHUB_STEP_SUMMARY': str(summary)})
        text = summary.read_text(encoding='utf-8')
        self.assertIn('confirmed=1; queued=1; image_wait=1; image_wait_and_queued=1', text)
        self.assertIn('queued, post not fetched yet=3; post fetched, images not tried yet=1; '
                      'image wait (host stop or retry time)=2', text)
        self.assertIn('- に: state=image_wait_and_queued; queued=1; imagesNotTried=1; imageWait=1', text)
        self.assertIn('Retired without HTTP or AI', text)


class AzureErrorTokenTests(unittest.TestCase):
    def test_only_a_short_code_becomes_a_token(self):
        token = azure.transport.error_code_token
        body = {'error': {'code': 'invalid_image_format', 'message': 'PRIVATE message text', 'param': 'x'}}
        self.assertEqual(token(json.dumps(body).encode()), 'azure_invalid_image_format')
        self.assertEqual(token(json.dumps({'error': {'code': 'BadRequest'}}).encode()), 'azure_badrequest')
        self.assertEqual(token(json.dumps({'error': {'type': 'invalid_request_error'}}).encode()),
                         'azure_invalid_request_error')
        for value in (b'', b'not json', json.dumps({'error': 'string'}).encode(),
                      json.dumps({'error': {'code': 'has spaces in it'}}).encode(),
                      json.dumps({'error': {'code': 'x' * 49}}).encode(),
                      json.dumps({'error': {'code': 400}}).encode(), b'{' * 5000):
            with self.subTest(value=value[:40]):
                self.assertIsNone(token(value))

    def test_transport_attaches_the_token_and_keeps_the_body_out(self):
        fixture = transport_fixture.TransportTests(methodName='runTest')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        headers = email.message.Message()
        error = urllib.error.HTTPError(
            'https://offline.openai.azure.com/openai/v1/chat/completions', 400, 'Bad Request', headers,
            io.BytesIO(json.dumps({'error': {'code': 'invalid_image_format',
                                             'message': 'PRIVATE_BODY_SENTINEL'}}).encode()))
        fixture.opener.open.side_effect = error
        with self.assertRaises(transport_fixture.azure.AzureFailure) as caught:
            fixture.client.structured([], {}, name='task', max_completion_tokens=1)
        self.assertEqual((caught.exception.reason, caught.exception.status, caught.exception.detail),
                         ('azure_http_error', 400, 'azure_invalid_image_format'))
        self.assertNotIn('PRIVATE_BODY_SENTINEL', repr(vars(caught.exception)))
        fixture.failures.assert_called_once_with(400, None)

    def test_cycle_diagnostic_keeps_the_token_only(self):
        case = DiscoveryBase(methodName='setUp')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.now = at(17)
        entry = case.post(at(16, 21), 9, '10月後半のお給仕予定')
        case.documents = {'amu_zettai': base.document(entry)}
        failure = azure.AnalysisFailure('azure_http_error', 400)
        failure.detail = 'azure_invalid_image_format'
        with mock.patch.object(collector, 'read_evidence', side_effect=failure):
            report, _ = case.run_cycle(cycle_id='3007-1')
        row = next(item for item in report['diagnostics'] if item['postId'] == entry['id'])
        self.assertEqual((row['httpStatus'], row['detail']), (400, 'azure_invalid_image_format'))
        facts.validate_state(case.state)


class CloudCohortTests(unittest.TestCase):
    def state(self, periods, *, cursor=40, ready=False, reason='waiting'):
        return {'pending': [], 'readings': {}, 'collection': {
            'chainId': '1-1', 'names': ['n%d' % index for index in range(40)], 'periods': periods,
            'cursor': cursor, 'ready': ready, 'reason': reason, 'nextAt': None, 'serviceDate': '2026-10-13'}}

    def test_old_scope_never_freezes_discovery_and_ready_time_limit_is_resumable(self):
        october = [['2026-10-16', '2026-10-31'], ['2026-10-01', '2026-10-15']]
        self.assertTrue(cloud.half_month_cohort_current(self.state(october), at(17)))
        self.assertFalse(cloud.half_month_cohort_current(self.state(october), dt.datetime(
            2026, 11, 1, 1, tzinfo=facts.JST)))
        self.assertFalse(cloud.half_month_cohort_current(self.state([['2026-10-01', '2026-10-15']]), at(13)))
        ready = self.state(october, ready=True, reason='time_limit')
        self.assertTrue(cloud.unfinished_half_month(ready, at(17)))
        self.assertFalse(cloud.unfinished_half_month(self.state(october), at(17)))
        self.assertFalse(cloud.unfinished_half_month(ready, dt.datetime(2026, 11, 1, 1, tzinfo=facts.JST)))
        self.assertTrue(cloud.unfinished_half_month(self.state(october, cursor=3)))


class StateRepositoryTests(unittest.TestCase):
    def test_disposable_repository_disables_background_maintenance(self):
        repository = cloud.StateRepository(legacy.ROOT, {})
        with mock.patch.object(cloud, 'child_process') as child:
            child.return_value = mock.Mock(returncode=0, stdout=b'')
            repository.git('status', reason='status_failed')
        argv = child.call_args.args[0]
        for setting in ('gc.auto=0', 'maintenance.auto=false', 'gc.autoDetach=false'):
            self.assertIn(setting, argv)
        self.assertLess(argv.index('maintenance.auto=false'), argv.index('status'))


if __name__ == '__main__':
    unittest.main()
