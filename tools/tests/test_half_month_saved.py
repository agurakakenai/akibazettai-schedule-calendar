"""Synthetic, network-forbidden saved schedule accounting and delta tests."""
import copy
import datetime as dt
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


saved = load('saved_half_test', ROOT / 'tools' / 'half-month-saved.py')
fixture = load('saved_half_fixture', Path(__file__).with_name('test_half_month_schedules.py'))
facts = fixture.facts
ledger = load('saved_half_usage', ROOT / 'tools' / 'analysis-state.py')


class SavedHalfMonthTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch('urllib.request.OpenerDirector.open',
                           side_effect=AssertionError('live network forbidden'))
        patch.start()
        self.addCleanup(patch.stop)
        self.usage = ledger.empty_state()
        ledger.apply_import(self.usage, {
            'receiptId': 'a' * 64, 'sourceHash': 'b' * 64,
            'date': '2026-09-07', 'counts': {'requests': 1},
            'modelBreakdown': [{
                'model': facts.MODEL, 'deployment': facts.MODEL,
                'modelVersion': facts.MODEL_VERSION, 'component': 'schedule',
                'kind': 'image', 'count': 1,
            }],
        })
        self.personal = {'identityBindings': {}}

    def entry(self, state=None, suffix=1):
        source, schedules, analysis = fixture.normalized(suffix=suffix)
        return {'expectedSubjectHash': saved.subject_hash(state, facts), 'amendment': {
            'source': source, 'schedules': schedules, 'analysis': analysis,
            'proof': {
                'usageReceiptId': 'a' * 64, 'usageSourceHash': 'b' * 64,
                'sourceManifestHash': 'c' * 64, 'analysisResultHash': 'd' * 64,
                'analysisReceiptHash': 'e' * 64,
                'issuedAt': analysis['analyzedAt'], 'searchCreatedAt': source['createdAt'],
            },
        }}

    def apply(self, state, entries, **changes):
        options = {
            'schedule': fixture.SCHEDULE, 'insights': {}, 'accounts': fixture.ACCOUNTS,
            'personal_state': self.personal, 'now': fixture.NOW,
        }
        options.update(changes)
        return saved.apply_amendments(state, entries, self.usage, facts, **options)

    def test_first_source_binds_real_metadata_and_replay_is_noop_without_double_usage(self):
        entry = self.entry()
        before = copy.deepcopy(self.usage)
        state = self.apply(None, [entry])
        self.assertEqual(len(state['schedules']), 1)
        self.assertEqual(len(state['schedules'][0]['days']), 6)
        self.assertEqual(len(state['savedImports']), 1)
        self.assertEqual(state['identityBindings'][fixture.TARGET['name']]['authorId'], fixture.AUTHOR)
        replay = self.apply(state, [entry], now=fixture.NOW + dt.timedelta(hours=1))
        self.assertEqual(replay, state)
        self.assertEqual(self.usage, before)
        public = facts.public_state(state)
        for key in ('savedImports', 'sources', 'revisions', 'bodyHash', 'payloadHash'):
            self.assertNotIn(key, repr(public))

    def test_compare_deployment_missing_usage_and_wrong_issue_day_are_rejected(self):
        entry = self.entry()
        original = copy.deepcopy(self.usage)
        changes = [
            ('deployment', 'gpt-5.6-luna-compare'), ('model', 'gpt-5.4-nano'),
            ('modelVersion', '2026-01-01'), ('kind', 'text'), ('component', 'personal'),
        ]
        for field, value in changes:
            self.usage = copy.deepcopy(original)
            self.usage['imports']['a' * 64]['modelBreakdown'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.apply(None, [entry])
        self.usage = copy.deepcopy(original)
        self.usage['imports']['a' * 64]['date'] = '2026-09-06'
        with self.assertRaisesRegex(ValueError, 'usage_date'):
            self.apply(None, [entry])
        self.usage = ledger.empty_state()
        with self.assertRaisesRegex(ValueError, 'usage_missing'):
            self.apply(None, [entry])

    def test_one_import_request_cannot_fund_two_different_analyses(self):
        state = self.apply(None, [self.entry()])
        before = copy.deepcopy(state)
        with self.assertRaisesRegex(ValueError, 'overallocated'):
            self.apply(state, [self.entry(state, suffix=2)])
        self.assertEqual(state, before)

    def test_binding_roster_and_unknown_account_fail_before_mutation(self):
        entry = self.entry()
        bound = {'identityBindings': {fixture.TARGET['name']: {
            'authorId': '999', 'authorScreenName': fixture.TARGET['handle'],
        }}}
        for options in ({'personal_state': bound}, {'accounts': []},
                        {'schedule': {'roster': [], 'schedule': {}}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.apply(None, [entry], **options)
        wrong = copy.deepcopy(entry)
        wrong['amendment']['schedules'][0]['name'] = 'いと'
        with self.assertRaisesRegex(ValueError, 'source_mismatch'):
            self.apply(None, [wrong])

    def test_bad_subject_partial_images_search_time_and_raw_are_rejected(self):
        for change in ('subject', 'images', 'search', 'raw'):
            entry = self.entry()
            if change == 'subject':
                entry['expectedSubjectHash'] = 'f' * 64
            elif change == 'images':
                entry['amendment']['analysis']['images'] = []
            elif change == 'search':
                entry['amendment']['proof']['searchCreatedAt'] = facts.stamp(fixture.CREATED + dt.timedelta(seconds=2))
            else:
                entry['amendment']['source']['text'] = 'PRIVATE_RAW_SENTINEL'
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.apply(None, [entry])
        with self.assertRaises(ValueError):
            self.apply(None, [self.entry(), self.entry()])

    def test_native_receipt_is_referenced_not_imported_again(self):
        source, schedules, analysis = fixture.normalized()
        identity = {'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
                    'model': facts.MODEL, 'modelVersion': facts.MODEL_VERSION, 'deployment': facts.MODEL}
        with tempfile.TemporaryDirectory(prefix='.half-saved-', dir=ROOT) as temporary:
            path = Path(temporary) / 'usage.json'
            ledger.atomic_json(path, ledger.empty_state())
            with ledger.SharedUsage(path, run_id='native-1', component='schedule',
                                    clock=lambda: fixture.NOW, sleep=lambda _: None) as usage:
                usage.reserve(analysis['requestHash'], identity)
                usage.issued(analysis['requestHash'])
                usage.finish(analysis['requestHash'], 'events')
                analysis['receiptId'] = next(iter(usage.state['receipts']))
                state = facts.empty_state()
                facts.apply_revision(state, schedules, source, analysis)
                saved.validate_accounting(state, usage.state, facts)
                self.assertEqual(usage.state['imports'], {})
                self.assertEqual(ledger.usage_counts(usage.state, 'native-1', fixture.NOW)['day'], 1)
                with self.assertRaisesRegex(ValueError, 'usage_missing'):
                    saved.validate_accounting(state, ledger.empty_state(), facts)


if __name__ == '__main__':
    unittest.main()
