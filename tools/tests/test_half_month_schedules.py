"""Synthetic fixtures only. Never contact X/Yahoo/Azure or copy private originals."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import unittest
from unittest import mock
import uuid
import zlib


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('half_month_test_collector',
                                             TOOLS / 'collect-half-month-schedules.py')
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
facts, azure = collector.facts, collector.azure
NOW = dt.datetime(2026, 9, 7, 1, tzinfo=facts.UTC)
CREATED = NOW - dt.timedelta(days=1)
TARGET = {'name': 'あむ', 'handle': 'amu_zettai'}
AUTHOR = '2080944098043977777'
ACCOUNTS = [{'name': TARGET['name'], 'handle': TARGET['handle'], 'source': '公式サイト'}]
SCHEDULE = {'roster': ['あむ'], 'schedule': {}, 'kitchenStaff': ['あむ']}


def post_id(created=CREATED, suffix=1):
    epoch = int(created.timestamp() * 1000)
    return str(((epoch - 1288834974657) << 22) + suffix)


def png(width=9, height=12):
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack(
            '>I', zlib.crc32(kind + payload) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\0' * (width * 3)) * min(height, 16)))
            + chunk(b'IEND', b''))


def jpeg(width=900, height=1200):
    # Structurally sufficient for the intentionally header-only stdlib probe.
    return (b'\xff\xd8\xff\xc0\x00\x0b\x08' + struct.pack('>HH', height, width)
            + b'\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00'
            + b'\x01\x02\x03\xff\xd9')


def entry(created=CREATED, suffix=1, name=TARGET['name'], handle=TARGET['handle']):
    tid = post_id(created, suffix)
    return {'id': tid, 'userId': AUTHOR, 'screenName': handle, 'createdAt': str(int(created.timestamp())),
            'url': f'https://x.com/{handle}/status/{tid}?s=20', 'text': '9月前半の予定'}


def document(*entries, best=None):
    page = {'timeline': {'entry': list(entries)}, 'searchError': {}}
    if best is not None:
        page['bestTweet'] = best
    value = {'props': {'pageProps': {'pageData': page}}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(value) + '</script>'


def payload(created=CREATED, suffix=1, photos=1, mime='png'):
    tid = post_id(created, suffix)
    result = {'id_str': tid, 'created_at': facts.stamp(created), 'text': '9月前半のお給仕予定。長め昼も昼です。',
              'user': {'id_str': AUTHOR, 'screen_name': TARGET['handle']},
              'edit_control': {'edit_tweet_ids': [tid]}, 'photos': [], 'mediaDetails': []}
    for index in range(photos):
        url = f'https://pbs.twimg.com/media/SYNTHETIC_{suffix}_{index}.{mime}'
        result['photos'].append({'url': url, 'width': 900, 'height': 1200})
        result['mediaDetails'].append({'type': 'photo', 'media_url_https': url,
                                      'expanded_url': f'https://x.com/{TARGET["handle"]}/status/{tid}/photo/{index + 1}',
                                      'original_info': {'width': 1536, 'height': 2048}})
    return result


def source(created=CREATED, suffix=1, photos=1, mime='png'):
    observed = created + dt.timedelta(days=1)
    candidate = collector.discover(document(entry(created, suffix)), {TARGET['name']: TARGET},
                                   observed, {})[0][0]
    return collector.validate_post(candidate, payload(created, suffix, photos, mime),
                                   TARGET, observed)


def result(days=None, *, month=9, half='first', printed=None, text_year=None, images=True):
    if days is None:
        days = [(2, '水', ['夜']), (5, '土', ['昼']), (7, '月', ['昼']),
                (10, '木', ['夜']), (12, '土', ['昼']), (14, '月', ['昼'])]
    return {'periods': [{'month': month, 'half': half, 'printedYear': printed, 'textYear': text_year,
                         'imageIndexes': [0] if images else [],
                         'days': [{'day': number, 'weekday': weekday, 'shifts': shifts}
                                  for number, weekday, shifts in days]}]}


def normalized(created=CREATED, suffix=1, value=None):
    verified, text, _ = source(created, suffix)
    images = [{'bytes': png(), 'mime': 'image/png'}]
    return verified, *azure.saved_result(verified, text, images, result() if value is None else value,
                                         now=facts.timestamp(verified['observedAt']),
                                         receipt_id=facts.digest(str(suffix).encode()),
                                         allowed_periods=None)


class Offline(unittest.TestCase):
    def setUp(self):
        guard = mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('live_source_forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def work_dir(self):
        path = TOOLS / 'tests' / ('.half-month-test-' + uuid.uuid4().hex)
        path.mkdir()
        self.addCleanup(shutil.rmtree, path)
        return path


class StateTests(Offline):
    def test_empty_and_missing_are_distinct(self):
        state = facts.empty_state()
        self.assertEqual(facts.validate_state(state), state)
        self.assertEqual(set(facts.public_state(state)), facts.PUBLIC_FIELDS)
        with self.assertRaises(FileNotFoundError):
            facts.read_state(self.work_dir() / 'absent.json')

    def test_public_projection_accepts_restored_private_and_public_seed(self):
        private = facts.empty_state()
        public = facts.public_state(private)
        self.assertIs(facts.validate_state(private, private=True), private)
        self.assertIs(facts.validate_state(public, private=False), public)
        self.assertEqual(facts.public_state(public), public)
        self.assertEqual(set(facts.public_state(public)), facts.PUBLIC_FIELDS)
        for value in (private, public):
            projection = facts.public_state(value)
            projection['lastRun']['status'] = 'paused'
            self.assertEqual(value['lastRun']['status'], 'never')
            invalid = {**copy.deepcopy(value), 'raw': 'not permitted'}
            with self.assertRaises(ValueError):
                facts.public_state(invalid)
        with self.assertRaises(ValueError):
            facts.validate_state(private, private=False)
        with self.assertRaises(ValueError):
            facts.validate_state(public, private=True)
        for invalid in (None, [], 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                facts.public_state(invalid)
        folder = self.work_dir()
        for name, value, private_mode in [('private.json', private, True), ('seed.json', public, False)]:
            path = folder / name
            path.write_text(json.dumps(value), encoding='utf-8')
            self.assertEqual(facts.read_state(path, private=private_mode), value)

    def test_strict_unknown_fields_and_duplicate_json(self):
        for field in ('body', 'raw', 'imageUrl', 'analysisCache'):
            state = facts.empty_state()
            state[field] = 'not allowed'
            with self.subTest(field=field), self.assertRaises(ValueError):
                facts.validate_state(state)
        path = self.work_dir() / 'state.json'
        path.write_text('{"schemaVersion":1,"schemaVersion":1}', encoding='utf-8')
        with self.assertRaises(ValueError):
            facts.read_state(path)

    def test_revision_union_and_private_projection(self):
        state = facts.empty_state()
        verified, schedules, analysis = normalized()
        self.assertTrue(facts.apply_revision(state, schedules, verified, analysis))
        self.assertFalse(facts.apply_revision(state, schedules, verified, analysis))
        manual = {'2026-09-02': {'昼': [{'name': 'あむ', 'featured': True}],
                                  '夜': [{'name': 'あむ', 'eventLabel': 'manual'}]}}
        before = copy.deepcopy(manual)
        merged = facts.effective_schedule(manual, facts.public_state(state))
        self.assertEqual(manual, before)
        self.assertTrue(merged['2026-09-02']['昼'][0]['featured'])
        self.assertNotIn('scheduleSources', merged['2026-09-02']['昼'][0])
        self.assertEqual(merged['2026-09-02']['夜'][0]['eventLabel'], 'manual')
        self.assertEqual(merged['2026-09-02']['夜'][0]['scheduleSources'][0]['id'], verified['id'])
        self.assertEqual(sum(len(row['shifts']) for row in state['schedules'][0]['days']), 6)
        public = json.dumps(facts.public_state(state))
        for prohibited in ('bodyHash', 'payloadHash', 'requestHash', 'data:image', 'media', 'receiptId', 'revisions'):
            self.assertNotIn(prohibited, public)

    def test_new_complete_replaces_only_auto_old_arrival_cannot_win(self):
        state = facts.empty_state()
        old_source, old, old_analysis = normalized()
        newer_source, newer, newer_analysis = normalized(CREATED + dt.timedelta(hours=1), 2,
                                                         result([(7, '月', ['昼'])]))
        facts.apply_revision(state, newer, newer_source, newer_analysis)
        self.assertFalse(facts.apply_revision(state, old, old_source, old_analysis))
        self.assertEqual(state['schedules'], newer)
        self.assertEqual(len(state['revisions']), 2)
        preserved = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            facts.apply_revision(state, [], old_source, old_analysis)
        self.assertEqual(state, preserved)

    def test_same_post_two_halves_and_same_half_duplicate_rejected(self):
        full = result([(2, '水', ['夜']), (20, '日', ['昼'])], half='full')
        verified, schedules, analysis = normalized(value=full)
        self.assertEqual(len(schedules), 2)
        self.assertEqual(schedules[0]['id'], schedules[1]['id'])
        state = facts.empty_state()
        facts.apply_revision(state, schedules, verified, analysis)
        self.assertEqual(len(state['schedules']), 2)
        state['schedules'].append(copy.deepcopy(schedules[0]))
        with self.assertRaises(ValueError):
            facts.validate_state(state)

    def test_public_post_identity_and_two_period_cap(self):
        _, schedules, _ = normalized()
        public = facts.public_state(facts.empty_state())
        public['schedules'] = schedules
        other = copy.deepcopy(schedules[0])
        other.update(name='別人', authorId='2080944098043977888', authorScreenName='other')
        other['url'] = facts.public_url('other', other['id'])
        with self.assertRaisesRegex(ValueError, 'inconsistent_schedule_post'):
            facts.validate_state({**public, 'schedules': [*schedules, other]}, private=False)
        for start, end, date in [('2026-09-16', '2026-09-30', '2026-09-20'),
                                  ('2026-10-01', '2026-10-15', '2026-10-02')]:
            other = copy.deepcopy(schedules[0])
            other['period'].update({'from': start, 'to': end})
            other['days'] = [{'date': date, 'shifts': ['昼']}]
            public['schedules'].append(other)
            if len(public['schedules']) == 2:
                facts.validate_state(public, private=False)
                other['observedAt'] = facts.stamp(NOW + dt.timedelta(seconds=1))
                facts.validate_state(public, private=False)
                other['observedAt'] = schedules[0]['observedAt']
                other['createdAt'] = facts.stamp(CREATED + dt.timedelta(milliseconds=500))
                with self.assertRaisesRegex(ValueError, 'inconsistent_schedule_post'):
                    facts.validate_state(public, private=False)
                other['createdAt'] = schedules[0]['createdAt']
                other['observedAt'] = schedules[0]['observedAt']
        with self.assertRaisesRegex(ValueError, 'inconsistent_schedule_post'):
            facts.validate_state(public, private=False)
        for invalid in ('0', '0123456789123456789'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                facts.identifier(invalid)
        self.assertEqual(facts.identifier('12345'), '12345')
        with self.assertRaises(ValueError):
            facts.post_identifier('12345')

    def test_revision_bad_hash_and_receipt_conflict(self):
        state = facts.empty_state()
        verified, schedules, analysis = normalized()
        facts.apply_revision(state, schedules, verified, analysis)
        changed = copy.deepcopy(schedules)
        changed[0]['days'].pop()
        with self.assertRaisesRegex(ValueError, 'receipt_conflict'):
            facts.apply_revision(state, changed, verified, analysis)
        state['revisions'][next(iter(state['revisions']))]['schedule']['days'].pop()
        with self.assertRaises(ValueError):
            facts.validate_state(state)

    def test_revisions_keep_individual_observation_and_payload_provenance(self):
        first_source, first, first_analysis = normalized()
        state = facts.empty_state()
        facts.apply_revision(state, first, first_source, first_analysis)
        first_key = state['receipts'][first_analysis['receiptId']][0]
        first_revision = copy.deepcopy(state['revisions'][first_key])
        later = NOW + dt.timedelta(seconds=1)
        second_source = copy.deepcopy(first_source)
        second_source.update(observedAt=facts.stamp(later), payloadHash=facts.digest(b'later-payload'),
                             discoveryHash=facts.digest(b'later-discovery'))
        second = copy.deepcopy(first[0])
        second.update(observedAt=facts.stamp(later),
                      period={'from': '2026-09-16', 'to': '2026-09-30',
                              'printedYear': None, 'yearBasis': 'post-context'},
                      days=[{'date': '2026-09-20', 'shifts': ['昼']}])
        second_analysis = copy.deepcopy(first_analysis)
        second_analysis.update(receiptId=facts.digest(b'separate-approved-receipt'),
                               analyzedAt=facts.stamp(later), resultHash=facts.digest(b'second-half-result'))
        self.assertEqual(facts.source_key(first_source), facts.source_key(second_source))
        self.assertTrue(facts.apply_revision(state, [second], second_source, second_analysis))
        facts.validate_state(state)
        second_key = state['receipts'][second_analysis['receiptId']][0]
        self.assertEqual(state['revisions'][first_key], first_revision)
        self.assertEqual(state['revisions'][second_key]['source'], second_source)
        self.assertEqual(len(state['sources']), 1)
        self.assertEqual(next(iter(state['sources'].values()))['source'], first_source)
        self.assertEqual([item['observedAt'] for item in facts.public_state(state)['schedules']],
                         [facts.stamp(NOW), facts.stamp(later)])
        before = copy.deepcopy(state)
        self.assertFalse(facts.apply_revision(state, first, first_source, first_analysis))
        self.assertFalse(facts.apply_revision(state, [second], second_source, second_analysis))
        self.assertEqual(state, before)
        path = self.work_dir() / 'revisions.json'
        path.write_text(json.dumps(state), encoding='utf-8')
        self.assertEqual(facts.read_state(path), state)
        invalid = copy.deepcopy(state)
        altered = invalid['revisions'].pop(second_key)
        altered['source']['observedAt'] = facts.stamp(NOW)
        altered_key = facts.digest(altered)
        invalid['revisions'][altered_key] = altered
        invalid['receipts'][second_analysis['receiptId']] = [altered_key]
        with self.assertRaisesRegex(ValueError, 'schedule_source_mismatch'):
            facts.validate_state(invalid)

    def test_legacy_revision_hashes_and_receipts_survive_new_observations(self):
        source, schedules, analysis = normalized()
        state = facts.empty_state()
        facts.apply_revision(state, schedules, source, analysis)
        current = state['receipts'][analysis['receiptId']][0]
        legacy = state['revisions'].pop(current)
        del legacy['source']
        legacy_key = facts.digest(legacy)
        state['revisions'][legacy_key] = legacy
        state['receipts'][analysis['receiptId']] = [legacy_key]
        path = self.work_dir() / 'legacy.json'
        path.write_text(json.dumps(state), encoding='utf-8')
        restored = facts.read_state(path)
        unchanged = copy.deepcopy(restored)
        self.assertFalse(facts.apply_revision(restored, schedules, source, analysis))
        self.assertEqual(restored, unchanged)
        fresh_source = copy.deepcopy(source)
        fresh_source.update(observedAt=facts.stamp(NOW + dt.timedelta(seconds=1)),
                            payloadHash=facts.digest(b'new-payload'))
        facts.record_source(restored, fresh_source, 'valid', 'valid_schedule',
                            NOW + dt.timedelta(seconds=1), analysis['requestHash'])
        facts.validate_state(restored)
        self.assertEqual(restored['revisions'][legacy_key], legacy)
        self.assertEqual(restored['receipts'][analysis['receiptId']], [legacy_key])
        self.assertEqual(restored['sources'][facts.source_key(source)]['source'], source)
        followup = copy.deepcopy(schedules[0])
        followup.update(observedAt=fresh_source['observedAt'],
                        period={'from': '2026-09-16', 'to': '2026-09-30',
                                'printedYear': None, 'yearBasis': 'post-context'},
                        days=[{'date': '2026-09-20', 'shifts': ['昼']}])
        followup_analysis = {**analysis, 'receiptId': facts.digest(b'legacy-followup'),
                             'analyzedAt': fresh_source['observedAt']}
        self.assertTrue(facts.apply_revision(restored, [followup], fresh_source, followup_analysis))
        self.assertEqual(restored['revisions'][legacy_key], legacy)
        self.assertEqual(restored['receipts'][analysis['receiptId']], [legacy_key])
        self.assertEqual(restored['sources'][facts.source_key(source)]['source'], source)

    def test_feed_check_timestamp_cannot_precede_last_success(self):
        for checked in (None, facts.stamp(NOW - dt.timedelta(seconds=1))):
            state = facts.empty_state()
            state.update(checkedAt=checked, lastSuccessAt=facts.stamp(NOW))
            with self.subTest(checked=checked), self.assertRaisesRegex(ValueError, 'check_chronology'):
                facts.validate_state(state)

    def test_saved_import_attribution_is_strict_linked_and_private(self):
        state = facts.empty_state()
        self.assertEqual(state['savedImports'], {})
        legacy = copy.deepcopy(state)
        del legacy['savedImports']
        facts.validate_state(legacy, private=True)
        self.assertNotIn('savedImports', facts.public_state(legacy))
        verified, schedules, analysis = normalized()
        facts.apply_revision(state, schedules, verified, analysis)
        self.assertEqual(state['savedImports'], {})
        amendment_hash = facts.digest(b'offline-amendment')
        imported = {field: facts.digest(field.encode()) for field in facts.SAVED_IMPORT_HASH_FIELDS}
        imported.update(receiptId=analysis['receiptId'], requestHash=analysis['requestHash'],
                        issuedAt=facts.stamp(NOW), importedAt=facts.stamp(NOW + dt.timedelta(seconds=1)))
        state['savedImports'][amendment_hash] = imported
        facts.validate_state(state)
        public = facts.public_state(state)
        self.assertNotIn('savedImports', public)
        self.assertNotIn('usageReceiptId', json.dumps(public))
        with self.assertRaises(ValueError):
            facts.validate_state({**public, 'savedImports': {}}, private=False)
        for field, bad_value in [('requestHash', facts.digest(b'wrong-request')),
                                 ('receiptId', facts.digest(b'missing-receipt')),
                                 ('usageReceiptId', 'not-a-hash'), ('usageSourceHash', None),
                                 ('issuedAt', '2026-09-07T01:00:00+00:00'),
                                 ('importedAt', facts.stamp(NOW - dt.timedelta(seconds=1))),
                                 ('raw', 'not allowed')]:
            invalid = copy.deepcopy(state)
            invalid['savedImports'][amendment_hash][field] = bad_value
            with self.subTest(field=field), self.assertRaises(ValueError):
                facts.validate_state(invalid)
        for field in imported:
            invalid = copy.deepcopy(state)
            del invalid['savedImports'][amendment_hash][field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                facts.validate_state(invalid)
        invalid = copy.deepcopy(state)
        invalid['savedImports']['bad-key'] = invalid['savedImports'].pop(amendment_hash)
        with self.assertRaises(ValueError):
            facts.validate_state(invalid)
        self.assertFalse(facts.apply_revision(state, schedules, verified, analysis))
        self.assertEqual(state['savedImports'][amendment_hash], imported)

    def test_population_includes_kitchen_unknown_rank_and_absent_insights(self):
        targets, reasons = facts.population(SCHEDULE, {'promotion_dates': {'あむ': '2026-09-10'}},
                                            ACCOUNTS, {})
        self.assertEqual(targets, {'あむ': TARGET})
        self.assertEqual(reasons['あむ']['reason'], 'not_searched')
        insights = {'maidTendency': {'あむ': {'rank': 'unknown'}}}
        self.assertEqual(facts.population(SCHEDULE, insights, ACCOUNTS)[0], targets)
        insights['maidTendency']['あむ']['x'] = 'wrong'
        self.assertFalse(facts.population(SCHEDULE, insights, ACCOUNTS)[0])

    def test_population_alias_duplicate_and_unknown_account(self):
        schedule = {'roster': ['あむ', '別人', '未知']}
        accounts = [*ACCOUNTS, {'name': '別人', 'handle': TARGET['handle'], 'source': '本人確認済み'}]
        targets, reasons = facts.population(schedule, {}, accounts)
        self.assertFalse(targets)
        self.assertEqual(reasons['あむ']['reason'], 'account_ambiguous')
        self.assertEqual(reasons['未知']['reason'], 'account_unknown')
        alias = {'maidTendency': {'あむ': {'alias': 'あむむ'}}}
        accounts = [{**ACCOUNTS[0], 'name': 'あむむ'}]
        self.assertIn('あむ', facts.population(SCHEDULE, alias, accounts)[0])

    def test_identity_binding_never_overwrites(self):
        state = facts.empty_state()
        verified, _, _ = source()
        facts.bind_identity(state, verified)
        changed = copy.deepcopy(verified)
        changed['authorId'] = '2080944098043977888'
        with self.assertRaises(ValueError):
            facts.bind_identity(state, changed)

    def test_public_calendar_types_and_boundaries(self):
        _, schedules, _ = normalized()
        for path, value in [('complete', True), ('period', {'from': '2026-09-02', 'to': '2026-09-15',
                                                         'printedYear': None, 'yearBasis': 'post-context'})]:
            bad = copy.deepcopy(schedules[0])
            if path == 'complete':
                bad['complete'] = value
            else:
                bad[path] = value
            with self.assertRaises(ValueError):
                facts.validate_schedule(bad)
        bad = copy.deepcopy(schedules[0])
        bad['days'][0]['shifts'] = ['昼', '昼']
        with self.assertRaises(ValueError):
            facts.validate_schedule(bad)

    def test_period_windows(self):
        self.assertEqual(facts.target_periods(dt.date(2026, 12, 20)),
                         [('2026-12-16', '2026-12-31'), ('2027-01-01', '2027-01-15')])
        self.assertEqual(facts.half_period(dt.date(2024, 2, 20)), ('2024-02-16', '2024-02-29'))
        self.assertEqual(facts.candidate_start(NOW), dt.date(2026, 8, 16))


if __name__ == '__main__':
    unittest.main()
