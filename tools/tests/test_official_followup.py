import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    'official_followup', Path(__file__).resolve().parents[1] / 'official-followup.py')
followup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(followup)
DAY = '2026-09-23'
STORES = ('s1', 's2', 's3', 's4')


def roster(shift='昼', names=None):
    return [{'id': str(2100000000000000000 + index + (10 if shift == '夜' else 0)),
             'createdAt': '2026-09-23T04:00:00Z', 'date': DAY, 'shift': shift,
             'storeId': store, 'names': names or ['あむ']}
            for index, store in enumerate(STORES)]


class OfficialFollowupTests(unittest.TestCase):
    def prepare(self, posts, shifts=('昼', '夜'), checked=(), personal=None, **options):
        targets = {'みりあ': {'name': 'みりあ', 'handle': 'miria_zettai', 'shifts': list(shifts)}}
        return followup.prepare(targets, {'posts': posts}, personal or {'posts': []}, DAY,
                                required_stores=STORES, slot_id='official:2026-09-23T13:30+09:00',
                                checked=checked, **options)

    def test_missing_roster_is_not_evidence_of_absence(self):
        result = self.prepare(roster()[:3])
        self.assertEqual(result['checks'], [])
        self.assertEqual(result['scopes']['昼']['missingStores'], ['s4'])
        self.assertEqual(result['meaning'], 'correction_check_only_not_absence')

    def test_complete_daytime_only_creates_daytime_check_not_night_absence(self):
        result = self.prepare(roster())
        self.assertEqual(len(result['checks']), 1)
        check = result['checks'][0]
        self.assertEqual(check['shifts'], ['昼'])
        self.assertEqual(check['target']['shifts'], ['昼', '夜'])
        self.assertNotIn('events', check)
        self.assertEqual(self.prepare(roster(), checked=[check['key']])['checks'], [])

    def test_late_addition_and_explicit_cancellation_are_already_resolved(self):
        for kind in ('late', 'absent'):
            posts = roster()
            posts[0]['notices'] = [{'name': 'みりあ', 'kind': kind}]
            self.assertEqual(self.prepare(posts)['checks'], [])

    def test_old_personal_work_does_not_prove_no_subsequent_cancellation(self):
        post = {'id': '2099999999999999999', 'name': 'みりあ', 'date': DAY,
                'createdAt': '2026-09-23T00:00:00Z',
                'events': [{'shift': '昼', 'kind': 'placement'}]}
        self.assertEqual(len(self.prepare(roster(), personal={'posts': [post]})['checks']), 1)
        post['events'][0]['kind'] = 'absence'
        self.assertEqual(self.prepare(roster(), personal={'posts': [post]})['checks'], [])

    def test_date_only_requires_both_complete_scopes_before_check(self):
        self.assertEqual(self.prepare(roster(), shifts=[])['checks'], [])
        result = self.prepare(roster() + roster('夜'), shifts=[])
        self.assertEqual(result['checks'][0]['shifts'], ['昼', '夜'])
        self.assertEqual(result['checks'][0]['target']['shifts'], [])

    def test_reply_alone_does_not_stand_in_for_a_missing_store_roster(self):
        posts = roster()
        posts[3]['replyTo'] = posts[0]['id']
        self.assertEqual(self.prepare(posts)['checks'], [])

    def test_new_full_roster_omission_is_a_check_not_an_inferred_cancellation(self):
        posts = roster()
        older = {**posts[0], 'id': '2099999999999999999',
                 'createdAt': '2026-09-23T03:00:00Z', 'names': ['みりあ']}
        result = self.prepare([older, *posts])
        self.assertEqual(len(result['checks']), 1)
        self.assertNotIn('events', result['checks'][0])

    def test_missing_store_cannot_be_assumed_closed(self):
        with self.assertRaises(ValueError):
            followup.prepare({}, {'posts': []}, {'posts': []}, DAY,
                             required_stores=['s1', 's2', 's3'], slot_id='official-slot')


if __name__ == '__main__':
    unittest.main()
