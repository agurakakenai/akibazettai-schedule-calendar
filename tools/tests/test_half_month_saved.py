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

    def legacy_state(self):
        entry = self.entry()
        source, schedules, analysis = fixture.normalized(contract_version=facts.LEGACY_VERSION)
        entry['amendment'].update(source=source, schedules=schedules, analysis=analysis)
        return self.apply(None, [entry])

    def timing_entry(self, state, *, label='timing-one', notes=None):
        source, schedules, analysis = fixture.normalized(value=fixture.timing_result(
            {5: [fixture.work_note()], 12: [fixture.work_note()]} if notes is None else notes))
        analysis.update(receiptId=facts.digest(label.encode()),
                        analyzedAt=facts.stamp(fixture.NOW + dt.timedelta(minutes=1)))
        receipt = facts.digest(('usage-' + label).encode())
        source_hash = facts.digest(('usage-source-' + label).encode())
        if receipt not in self.usage['imports']:
            ledger.apply_import(self.usage, {
                'receiptId': receipt, 'sourceHash': source_hash,
                'date': '2026-09-07', 'counts': {'requests': 1},
                'modelBreakdown': [{
                    'model': facts.MODEL, 'deployment': facts.MODEL, 'modelVersion': facts.MODEL_VERSION,
                    'component': 'schedule', 'kind': 'image', 'count': 1,
                }],
            })
        previous_key, previous = next((key, revision) for key, revision in facts.select_revisions(state)
                                      if revision['schedule']['id'] == source['id'])
        del previous_key
        previous_import = next((key for key, imported in state['savedImports'].items()
                                if imported['receiptId'] == previous['analysis']['receiptId']), None)
        target_scopes = {}
        for day, values in ({5: [fixture.work_note()], 12: [fixture.work_note()]}
                            if notes is None else notes).items():
            for note in values:
                target = {'name': source['name'], 'serviceDate': '2026-09-' + str(day).zfill(2),
                          'shift': note['shift'], 'boundary': 'end' if note['shift'] == '昼' else 'start'}
                target_scopes[tuple(target.values())] = target
        entry = self.entry(state)
        amendment = entry['amendment']
        amendment.update(
            source=source, schedules=schedules, analysis=analysis, operation='work-timing-only',
            previous={'contractVersion': previous['analysis']['contract'],
                      'contractHash': facts.contract_hash(previous['analysis']),
                      **({'importId': previous_import} if previous_import else {'kind': 'native'}),
                      'analysisReceiptId': previous['analysis']['receiptId']},
            expectedCoreHash=facts.core_hash(state['schedules']),
            expectedTimingHash=facts.timing_hash(state['schedules']),
            basisRevisionKeys=facts.projection_basis(state, previous['analysis']['receiptId']),
            targetScopes=list(target_scopes.values()),
            updateChannels=['workTiming'])
        if not amendment['targetScopes']:
            amendment['targetScopes'] = [
                {'name': source['name'], 'serviceDate': '2026-09-05', 'shift': '昼', 'boundary': 'end'}]
        amendment['proof'].update(usageReceiptId=receipt, usageSourceHash=source_hash,
                                  issuedAt=analysis['analyzedAt'])
        return entry

    def apply_timing(self, state, entry, *, approved_selections=None, approved_selection_apply=None):
        return self.apply(state, [entry], now=fixture.NOW + dt.timedelta(hours=1),
                          approved_selections=approved_selections, approved_selection_apply=approved_selection_apply)

    def test_v1_to_v2_explicit_timing_amendment_keeps_core_and_all_history(self):
        before = self.legacy_state()
        entry = self.timing_entry(before)
        usage_before = copy.deepcopy(self.usage)
        after = self.apply_timing(before, entry)
        self.assertEqual(after['schedules'][0]['days'], before['schedules'][0]['days'])
        self.assertEqual(facts.core_hash(after['schedules']), facts.core_hash(before['schedules']))
        self.assertEqual([(fact['serviceDate'], fact['qualifier'], fact['explicitTime'])
                          for fact in after['schedules'][0]['workTiming']['facts']],
                         [('2026-09-05', 'long', None), ('2026-09-12', 'long', None)])
        for field in ('revisions', 'receipts', 'savedImports'):
            self.assertEqual(len(after[field]), len(before[field]) + 1)
            self.assertTrue(all(after[field][key] == value for key, value in before[field].items()))
        self.assertEqual(before['schedules'][0].get('workTiming'), None)
        self.assertEqual(self.apply_timing(after, entry), after)
        self.assertEqual(self.usage, usage_before)
        facts.validate_state(after)
        saved.validate_accounting(after, self.usage, facts)
        reordered = copy.deepcopy(after)
        reordered['revisions'] = dict(reversed(list(reordered['revisions'].items())))
        facts.validate_state(reordered)
        self.assertEqual(facts.public_state(reordered), facts.public_state(after))
        for forbidden in ('timingAmendment', 'expectedSubjectHash', 'importId', 'receiptId', 'bodyHash'):
            self.assertNotIn(forbidden, repr(facts.public_state(after)))

    def test_timing_revision_chain_keeps_unmentioned_and_explicit_retraction(self):
        before = self.legacy_state()
        first = self.apply_timing(before, self.timing_entry(before))
        second_entry = self.timing_entry(first, label='timing-two', notes={
            5: [fixture.work_note(qualifier=None, status='withdrawn')]})
        second = self.apply_timing(first, second_entry)
        facts_ = second['schedules'][0]['workTiming']['facts']
        self.assertEqual([(fact['serviceDate'], fact['status']) for fact in facts_],
                         [('2026-09-05', 'withdrawn'), ('2026-09-12', 'set')])
        third = self.apply_timing(second, self.timing_entry(second, label='timing-three', notes={
            12: [fixture.work_note(qualifier=None, explicit_time='17:00')]}))
        self.assertEqual([(fact['serviceDate'], fact['status'], fact['explicitTime'])
                          for fact in third['schedules'][0]['workTiming']['facts']],
                         [('2026-09-05', 'withdrawn', None), ('2026-09-12', 'set', '17:00')])
        self.assertEqual(len(third['revisions']), 4)
        facts.validate_state(third)

    def test_same_post_distinct_exclusions_use_one_target_scope_and_do_not_withdraw_late(self):
        before = self.legacy_state()
        first = self.apply_timing(before, self.timing_entry(before, notes={
            10: [fixture.work_note('夜', 'start', 'late', '18:00')]}))
        entry = self.timing_entry(first, label='targeted-denials', notes={
            10: [fixture.work_note('夜', 'start', 'early', status='excluded'),
                 fixture.work_note('夜', 'start', None, '16:00', status='excluded')]})
        self.assertEqual(entry['amendment']['targetScopes'], [{
            'name': fixture.TARGET['name'], 'serviceDate': '2026-09-10',
            'shift': '夜', 'boundary': 'start'}])
        second = self.apply_timing(first, entry)
        notes = second['schedules'][0]['workTiming']['facts']
        self.assertEqual([(fact['status'], fact['qualifier'], fact['explicitTime']) for fact in notes],
                         [('set', 'late', '18:00'), ('excluded', 'early', None),
                          ('excluded', None, '16:00')])
        self.assertEqual(len({facts.timing().scope(fact) for fact in notes}), 1)
        self.assertEqual(len({facts.timing().fact_key(fact) for fact in notes}), 3)
        self.assertTrue(all(second['revisions'][key] == value for key, value in first['revisions'].items()))
        self.assertEqual(self.apply_timing(second, entry), second)
        facts.validate_state(second)
        saved.validate_accounting(second, self.usage, facts)

    def test_saved_storage_overflow_rejects_without_mutating_facts_or_accounted_usage(self):
        original = self.entry()
        source = original['amendment']['source']
        original['amendment']['schedules'][0]['workTiming'] = fixture.full_timing_channel(source)
        state = self.apply(None, [original])
        entry = self.timing_entry(state, label='overflow', notes={
            5: [fixture.work_note(qualifier=None, explicit_time='20:00', status='excluded')]})
        before, usage = copy.deepcopy(state), copy.deepcopy(self.usage)
        with self.assertRaisesRegex(facts.timing().WorkTimingLimitError, 'work_timing_storage_limit'):
            self.apply_timing(state, entry)
        self.assertEqual(state, before)
        self.assertEqual(self.usage, usage)
        self.assertEqual(len(state['schedules'][0]['workTiming']['facts']), 512)

    def late_older_entry(self, state):
        source, schedules, analysis = fixture.normalized(
            fixture.CREATED - dt.timedelta(hours=1), suffix=9,
            value=fixture.timing_result({
                5: [fixture.work_note(qualifier='short', explicit_time='16:00')],
                7: [fixture.work_note()]}))
        receipt, source_hash = facts.digest(b'late-old-usage'), facts.digest(b'late-old-usage-source')
        ledger.apply_import(self.usage, {
            'receiptId': receipt, 'sourceHash': source_hash, 'date': '2026-09-07',
            'counts': {'requests': 1}, 'modelBreakdown': [{
                'model': facts.MODEL, 'deployment': facts.MODEL, 'modelVersion': facts.MODEL_VERSION,
                'component': 'schedule', 'kind': 'image', 'count': 1}]})
        entry = self.entry(state)
        entry['amendment'].update(source=source, schedules=schedules, analysis=analysis)
        entry['amendment']['proof'].update(
            usageReceiptId=receipt, usageSourceHash=source_hash,
            issuedAt=analysis['analyzedAt'], searchCreatedAt=source['createdAt'])
        return entry

    def test_late_older_post_does_not_rebase_historical_timing_approval(self):
        before = self.legacy_state()
        entry = self.timing_entry(before)
        amended = self.apply_timing(before, entry)
        frozen = copy.deepcopy(amended)
        older_entry = self.late_older_entry(amended)
        older = older_entry['amendment']
        standalone = facts.empty_state()
        facts.apply_revision(standalone, older['schedules'], older['source'], older['analysis'])
        facts.validate_state(standalone)

        current = self.apply_timing(amended, older_entry)
        facts.validate_state(current)
        saved.validate_accounting(current, self.usage, facts)
        self.assertEqual(current['schedules'][0]['id'], amended['schedules'][0]['id'])
        self.assertEqual(current['schedules'][0]['days'], amended['schedules'][0]['days'])
        notes = {note['serviceDate']: note for note in current['schedules'][0]['workTiming']['facts']}
        self.assertEqual(set(notes), {'2026-09-05', '2026-09-07', '2026-09-12'})
        self.assertEqual((notes['2026-09-05']['qualifier'], notes['2026-09-05']['explicitTime']),
                         ('long', None))
        self.assertEqual(notes['2026-09-05']['source']['id'], amended['schedules'][0]['id'])
        self.assertEqual(notes['2026-09-07']['source']['id'], older['source']['id'])
        for field in ('revisions', 'receipts', 'savedImports'):
            self.assertTrue(all(current[field][key] == value for key, value in frozen[field].items()))
        self.assertEqual(amended, frozen)
        self.assertEqual(self.apply_timing(current, entry), current)

        followup = self.timing_entry(current, label='after-late-discovery', notes={
            12: [fixture.work_note(qualifier=None, explicit_time='17:00')]})
        older_key = current['receipts'][older['analysis']['receiptId']][0]
        self.assertIn(older_key, followup['amendment']['basisRevisionKeys'])
        final = self.apply_timing(current, followup)
        facts.validate_state(final)
        self.assertTrue(all(final['revisions'][key] == value for key, value in frozen['revisions'].items()))
        notes = {note['serviceDate']: note for note in final['schedules'][0]['workTiming']['facts']}
        self.assertEqual(notes['2026-09-07']['source']['id'], older['source']['id'])
        self.assertEqual(notes['2026-09-12']['explicitTime'], '17:00')

    def test_stale_or_forged_projection_basis_is_rejected(self):
        before = self.legacy_state()
        amended = self.apply_timing(before, self.timing_entry(before))
        prepared = self.timing_entry(amended, label='stale-basis')
        older_entry = self.late_older_entry(amended)
        current = self.apply_timing(amended, older_entry)
        older_key = current['receipts'][older_entry['amendment']['analysis']['receiptId']][0]
        prepared['expectedSubjectHash'] = saved.subject_hash(current, facts)
        prepared['amendment']['expectedTimingHash'] = facts.timing_hash(current['schedules'])
        snapshot = copy.deepcopy(current)
        with self.assertRaisesRegex(ValueError, 'timing_basis_stale'):
            self.apply_timing(current, prepared)
        self.assertEqual(current, snapshot)

        ready = self.timing_entry(current, label='valid-basis')
        for basis in ([], [None], ['f' * 64], [older_key, older_key],
                      list(reversed(ready['amendment']['basisRevisionKeys']))):
            invalid = copy.deepcopy(ready)
            invalid['amendment']['basisRevisionKeys'] = basis
            with self.subTest(basis=basis), self.assertRaises(ValueError):
                self.apply_timing(current, invalid)
            self.assertEqual(current, snapshot)

        amendment_key = next(key for key, revision in current['revisions'].items()
                             if 'timingAmendment' in revision)
        forged = copy.deepcopy(current)
        revision = forged['revisions'].pop(amendment_key)
        revision['timingAmendment']['basisRevisionKeys'] = sorted(
            [*revision['timingAmendment']['basisRevisionKeys'], older_key])
        replacement = facts.digest(revision)
        forged['revisions'][replacement] = revision
        receipt = revision['analysis']['receiptId']
        forged['receipts'][receipt] = [replacement if key == amendment_key else key
                                       for key in forged['receipts'][receipt]]
        with self.assertRaisesRegex(ValueError, 'timing_amendment_hash'):
            facts.validate_state(forged)

    def test_empty_timing_has_usage_but_no_amendment_or_core_mutation(self):
        before = self.legacy_state()
        entry = self.timing_entry(before, notes={})
        usage = copy.deepcopy(self.usage)
        self.assertEqual(self.apply_timing(before, entry), before)
        self.assertEqual(self.usage, usage)
        self.assertEqual(len(usage['imports']), 2)

    def test_timing_stale_branch_and_receipt_reuse_are_rejected_atomically(self):
        before = self.legacy_state()
        first_entry = self.timing_entry(before)
        after = self.apply_timing(before, first_entry)
        stale = self.timing_entry(before, label='branch')
        stale['expectedSubjectHash'] = saved.subject_hash(after, facts)
        usage_before = copy.deepcopy(self.usage)
        with self.assertRaisesRegex(ValueError, 'parent_stale'):
            self.apply_timing(after, stale)
        replay = copy.deepcopy(first_entry)
        replay['expectedSubjectHash'] = facts.digest(b'not-the-original-manifest')
        with self.assertRaisesRegex(ValueError, 'replay_changed'):
            self.apply_timing(after, replay)
        for change in ('analysisReceipt', 'usageReceipt'):
            invalid = self.timing_entry(before, label='reuse')
            if change == 'analysisReceipt':
                invalid['amendment']['analysis']['receiptId'] = next(iter(before['receipts']))
            else:
                invalid['amendment']['proof'].update(usageReceiptId='a' * 64, usageSourceHash='b' * 64)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'reused'):
                self.apply_timing(before, invalid)
        self.assertEqual(before['schedules'][0].get('workTiming'), None)
        self.assertTrue(all(self.usage['imports'][key] == value
                            for key, value in usage_before['imports'].items()))

    def test_timing_authorization_abuse_cannot_modify_old_canonical_or_usage(self):
        before = self.legacy_state()
        entry = self.timing_entry(before)
        for change in ('subject', 'coreHash', 'timingHash', 'contract', 'contractHash',
                       'import', 'analysisRef', 'body', 'image', 'media', 'days', 'shift',
                       'period', 'name', 'scopeDate', 'scopeShift', 'scopeBoundary',
                       'scopeName', 'scopeUnknown', 'channel', 'operation', 'newPrompt', 'newSchema'):
            invalid = copy.deepcopy(entry)
            amendment = invalid['amendment']
            if change == 'subject':
                invalid['expectedSubjectHash'] = 'f' * 64
            elif change in ('coreHash', 'timingHash'):
                amendment['expected' + change[0].upper() + change[1:]] = 'f' * 64
            elif change == 'contract':
                amendment['previous']['contractVersion'] = facts.VERSION
            elif change == 'contractHash':
                amendment['previous']['contractHash'] = 'f' * 64
            elif change == 'import':
                amendment['previous']['importId'] = 'f' * 64
            elif change == 'analysisRef':
                amendment['previous']['analysisReceiptId'] = 'f' * 64
            elif change == 'body':
                amendment['source']['bodyHash'] = 'f' * 64
            elif change == 'image':
                amendment['analysis']['images'][0]['sha256'] = 'f' * 64
            elif change == 'media':
                amendment['source']['media'][0]['urlHash'] = 'f' * 64
            elif change == 'days':
                amendment['schedules'][0]['days'].pop()
            elif change == 'shift':
                amendment['schedules'][0]['days'][0]['shifts'] = ['昼']
            elif change == 'period':
                amendment['schedules'][0]['period']['yearBasis'] = 'text'
            elif change == 'name':
                amendment['source']['name'] = '別人'
            elif change.startswith('scope'):
                field, value = {
                    'scopeDate': ('serviceDate', '2026-09-06'), 'scopeShift': ('shift', '夜'),
                    'scopeBoundary': ('boundary', 'start'), 'scopeName': ('name', '別人'),
                    'scopeUnknown': ('store', 's1'),
                }[change]
                amendment['targetScopes'][0][field] = value
            elif change == 'channel':
                amendment['updateChannels'] = ['workTiming', 'days']
            elif change in ('newPrompt', 'newSchema'):
                amendment['analysis']['promptHash' if change == 'newPrompt' else 'schemaHash'] = 'f' * 64
            else:
                amendment['operation'] = 'replace'
            snapshot, usage = copy.deepcopy(before), copy.deepcopy(self.usage)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.apply_timing(before, invalid)
            self.assertEqual(before, snapshot)
            self.assertEqual(self.usage, usage)

    def test_validate_and_apply_reject_same_lineage_fork(self):
        before = self.legacy_state()
        first = self.apply_timing(before, self.timing_entry(before))
        second = self.apply_timing(before, self.timing_entry(before, label='branch', notes={
            5: [fixture.work_note(qualifier=None, explicit_time='17:00')]}))
        forged = copy.deepcopy(first)
        for field in ('revisions', 'receipts', 'savedImports'):
            forged[field].update(second[field])
        with self.assertRaisesRegex(ValueError, 'timing_branch'):
            facts.validate_state(forged)

    def test_same_ordered_image_hashes_are_required(self):
        source, text, _ = fixture.source(photos=2)
        images = [{'bytes': fixture.png(width=width), 'mime': 'image/png'} for width in (9, 10)]
        schedules, analysis = fixture.azure.saved_result(
            source, text, images, fixture.result(contract_version=facts.LEGACY_VERSION),
            now=fixture.NOW, receipt_id=facts.digest(b'old-two-images'),
            contract_version=facts.LEGACY_VERSION)
        first = self.entry()
        first['amendment'].update(source=source, schedules=schedules, analysis=analysis)
        before = self.apply(None, [first])
        update = self.timing_entry(before)
        tables, analysis = fixture.azure.saved_result(
            source, text, images, fixture.timing_result({5: [fixture.work_note()], 12: [fixture.work_note()]}),
            now=fixture.NOW + dt.timedelta(minutes=1), receipt_id=update['amendment']['analysis']['receiptId'])
        update['amendment'].update(source=source, schedules=tables, analysis=analysis)
        accepted = self.apply_timing(before, update)
        self.assertEqual(len(accepted['revisions']), 2)
        invalid = copy.deepcopy(update)
        invalid['amendment']['analysis']['images'].reverse()
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            self.apply_timing(before, invalid)
        self.assertEqual(len(before['revisions']), 1)

    def test_timing_cannot_amend_an_older_post_behind_newer_original_post(self):
        before = self.legacy_state()
        entry = self.timing_entry(before)
        source, schedules, analysis = fixture.normalized(
            fixture.CREATED + dt.timedelta(minutes=10), suffix=9)
        # A separately accounted newer complete schedule remains the current core.
        newer_usage = facts.digest(b'newer-usage')
        ledger.apply_import(self.usage, {
            'receiptId': newer_usage, 'sourceHash': '9' * 64, 'date': '2026-09-07',
            'counts': {'requests': 1}, 'modelBreakdown': [{
                'model': facts.MODEL, 'deployment': facts.MODEL, 'modelVersion': facts.MODEL_VERSION,
                'component': 'schedule', 'kind': 'image', 'count': 1}]})
        newer = self.entry(before)
        newer['amendment'].update(source=source, schedules=schedules, analysis=analysis)
        newer['amendment']['proof'].update(
            usageReceiptId=newer_usage, usageSourceHash='9' * 64, issuedAt=analysis['analyzedAt'],
            searchCreatedAt=source['createdAt'])
        current = self.apply(before, [newer], now=fixture.NOW + dt.timedelta(hours=1))
        entry['expectedSubjectHash'] = saved.subject_hash(current, facts)
        with self.assertRaisesRegex(ValueError, 'parent_stale'):
            self.apply_timing(current, entry)
        self.assertEqual(current['schedules'][0]['id'], source['id'])

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
                self.usage = copy.deepcopy(usage.state)
                amendment = self.timing_entry(state)
                self.assertEqual(amendment['amendment']['previous']['kind'], 'native')
                self.assertNotIn('importId', amendment['amendment']['previous'])
                amended = self.apply_timing(state, amendment)
                self.assertEqual(len(amended['revisions']), 2)
                self.assertEqual(len(amended['schedules'][0]['workTiming']['facts']), 2)
                saved.validate_accounting(amended, self.usage, facts)
                invalid = copy.deepcopy(amendment)
                invalid['amendment']['previous'].pop('kind')
                invalid['amendment']['previous']['importId'] = None
                with self.assertRaises(ValueError):
                    self.apply_timing(state, invalid)


if __name__ == '__main__':
    unittest.main()
