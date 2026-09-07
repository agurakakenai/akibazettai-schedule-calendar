"""Source0/inference0 cloud integration, using only local fake Git remotes."""
import contextlib
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load('half_cloud_existing_fixture', Path(__file__).with_name('test_cloud_collection.py'))
fixture = load('half_cloud_schedule_fixture', Path(__file__).with_name('test_half_month_schedules.py'))
pages = load('half_cloud_pages', TOOLS / 'pages.py')
cloud, collector = legacy.cloud, legacy.collector
NOW = fixture.NOW
RUN_ID = '12345-1'
IDENTITY = {'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
            'deployment': 'gpt-5.6-luna', 'model': 'gpt-5.6-luna', 'modelVersion': '2026-07-09'}
RAW = 'HALF_CLOUD_PRIVATE_RAW_SENTINEL'


class HalfMonthCloudTests(unittest.TestCase):
    def setUp(self):
        # Composition deliberately avoids rediscovering/running the old 105 tests.
        self.fx = legacy.CloudTests(methodName='runTest')
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.personal = cloud.load_personal_collector()
        self.ai = cloud.load_analysis_state()
        self.sources = cloud.load_source_state()
        self.half = cloud.load_half_month_state()
        self.saved = cloud.load_half_month_saved()
        self.official_analysis = collector.analysis_module()
        self.now = NOW
        self.private = self.personal.empty_state()
        self.private['budgets'] = {'2026-09-06': {'searches': 18, 'posts': 11}}
        self.private['lastRequests'] = {
            self.personal.SEARCH_HOST: '2026-09-06T02:00:00Z',
            self.personal.POST_HOST: '2026-09-06T02:01:00Z'}
        self.transport = {'schemaVersion': 1, 'cooldowns': {
            'pbs.twimg.com': '2026-09-06T05:00:00Z'}}
        self.usage = self.ai.empty_state()
        self.ai.apply_import(self.usage, {
            'receiptId': cloud.data_hash('old-ai'), 'sourceHash': cloud.data_hash('old-ai-evidence'),
            'date': '2026-09-06', 'counts': {'requests': 24},
            'modelBreakdown': [{'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 24}]})
        source_import = {
            'receiptId': cloud.data_hash('old-source'), 'sourceHash': cloud.data_hash('old-source-evidence'),
            'date': '2026-09-06', 'searches': 11, 'posts': 9}
        self.usage['sourceImports'][source_import['receiptId']] = {
            'receipt': source_import, 'budgetBefore': {'searches': 7, 'posts': 2},
            'budgetAfter': {'searches': 18, 'posts': 11}}
        self.old_images = [{
            'receiptId': cloud.data_hash('old-image'), 'date': '2026-09-06', 'images': 1,
            'sourceHash': cloud.data_hash('old-image-evidence')}]
        self.source_state = self.baseline()
        self.half_state = self.half.empty_state()
        self.child_calls = []

    def baseline(self):
        return self.sources.baseline_state(
            self.private, source_hash=cloud.source_baseline_hash(self.private, self.usage, self.transport),
            at=NOW, source_imports=self.usage['sourceImports'],
            historical_images=self.old_images, cooldowns=self.transport['cooldowns'])

    def seed(self, *, half=True, source=True):
        files = {cloud.SNAPSHOT: collector.empty_snapshot(), cloud.HTTP_STATE: self.transport,
                 cloud.PERSONAL: self.private, cloud.AI_USAGE: self.usage,
                 cloud.OWNER_FILE: cloud.STATE_OWNER}
        if half:
            files[cloud.HALF_MONTH] = self.half_state
        if source:
            files[cloud.SOURCE_USAGE] = self.source_state
        self.fx.bare_commit(files)

    def manifest(self, *, migrate=False, entries=(), imports=()):
        result = {'schemaVersion': 1, 'expectedMainSHA': self.fx.source,
                  'expectedStateSHA': self.fx.git(self.fx.remote, 'rev-parse', cloud.REF).decode().strip(),
                  'officialAmendments': [], 'sourceReceipts': [],
                  'usageImports': copy.deepcopy(list(imports)), 'halfMonthAmendments': copy.deepcopy(list(entries))}
        if migrate:
            result['sourceMigration'] = {
                'expectedPersonalBudgetsHash': cloud.data_hash(self.private['budgets']),
                'sourceHash': cloud.source_baseline_hash(self.private, self.usage, self.transport),
                'historicalImages': copy.deepcopy(self.old_images)}
        return result

    def facts_entry(self, state=None):
        source, schedules, analysis = fixture.normalized()
        return {'expectedSubjectHash': self.saved.subject_hash(state, self.half), 'amendment': {
            'source': source, 'schedules': schedules, 'analysis': analysis,
            'proof': {'usageReceiptId': 'a' * 64, 'usageSourceHash': 'b' * 64,
                      'sourceManifestHash': 'c' * 64, 'analysisResultHash': 'd' * 64,
                      'analysisReceiptHash': 'e' * 64, 'issuedAt': analysis['analyzedAt'],
                      'searchCreatedAt': source['createdAt']}}}

    def schedule_import(self):
        return {'receiptId': 'a' * 64, 'sourceHash': 'b' * 64, 'date': '2026-09-07',
                'counts': {'requests': 1}, 'modelBreakdown': [{
                    'model': 'gpt-5.6-luna', 'deployment': 'gpt-5.6-luna',
                    'modelVersion': '2026-07-09', 'component': 'schedule', 'kind': 'image', 'count': 1}]}

    def schedule_inputs(self):
        (self.fx.root / 'data' / 'schedule.js').write_text(
            'window.SCHEDULE_DATA=' + json.dumps(fixture.SCHEDULE) + ';', encoding='utf-8')
        (self.fx.root / 'data' / 'store-insights.js').write_text('window.STORE_INSIGHTS={};', encoding='utf-8')
        (self.fx.root / 'tools' / 'data' / 'accounts.csv').write_text(
            'name,handle,source\nあむ,amu_zettai,公式サイト\n', encoding='utf-8')

    def invoke(self):
        return cloud.orchestrate(self.fx.args, root=self.fx.root,
                                 environment=self.fx.environment, collector=collector, personal=self.personal)

    def without_children(self, action):
        with contextlib.ExitStack() as stack:
            children = [stack.enter_context(mock.patch.object(cloud, name,
                        side_effect=AssertionError('No source/Azure child allowed'))) for name in (
                            'invoke_collector', 'invoke_personal_collector', 'invoke_half_month_collector')]
            stack.enter_context(mock.patch.object(collector, 'utc_now', side_effect=lambda: self.now))
            result = action()
            for child in children:
                child.assert_not_called()
            return result

    def apply(self, manifest):
        self.fx.saved_mode(manifest)
        return self.without_children(self.invoke)

    def before_lease_failure(self, pattern):
        references = self.fx.git(self.fx.remote, 'show-ref')
        output = self.fx.output.read_bytes()
        with mock.patch.object(cloud.StateRepository, 'persist',
                               side_effect=AssertionError('Invalid input must not acquire lease')) as persist:
            with self.assertRaisesRegex((cloud.CloudError, ValueError), pattern):
                self.without_children(self.invoke)
            persist.assert_not_called()
        self.assertEqual(self.fx.git(self.fx.remote, 'show-ref'), references)
        self.assertEqual(self.fx.output.read_bytes(), output)
        self.assertNotIn(cloud.LEASE, self.fx.remote_names())

    def enable(self, *, half=True):
        self.fx.args.mode = 'daily-guidance'
        self.fx.environment.update(
            GITHUB_EVENT_NAME='schedule', DAILY_GUIDANCE_ENABLED='true',
            HALF_MONTH_SCHEDULE_ENABLED='true' if half else 'false')
        event = json.loads(self.fx.event_path.read_bytes())
        event['schedule'] = cloud.DAILY_SCHEDULE
        self.fx.event_path.write_text(json.dumps(event), encoding='utf-8')

    def test_flag_false_restores_legacy_without_creating_half_or_zero_source(self):
        self.seed(half=False, source=False)
        originals = {name: self.fx.remote_json(name)[1]
                     for name in (cloud.SNAPSHOT, cloud.PERSONAL, cloud.AI_USAGE, cloud.HTTP_STATE)}
        head = self.fx.git(self.fx.remote, 'rev-parse', cloud.REF)
        self.fx.args.mode = 'restore'
        self.fx.environment['HALF_MONTH_SCHEDULE_ENABLED'] = 'false'
        self.without_children(self.invoke)
        for name, raw in originals.items():
            self.assertEqual((self.fx.output.parent / name).read_bytes(), raw)
        self.assertFalse((self.fx.output.parent / cloud.HALF_MONTH).exists())
        self.assertFalse((self.fx.output.parent / cloud.SOURCE_USAGE).exists())
        self.assertEqual(self.fx.git(self.fx.remote, 'rev-parse', cloud.REF), head)

    def test_enabled_missing_half_state_stops_before_lease_or_child(self):
        self.seed(half=False)
        self.enable()
        self.before_lease_failure('missing_half_month_state')

    def test_enabled_missing_source_ledger_stops_before_lease_or_child(self):
        self.seed(source=False)
        self.enable()
        self.before_lease_failure('missing_half_month_source_usage')

    def test_migration_only_apply_is_leased_data_only_and_replay_preserves_nonzero_baseline(self):
        self.seed(half=False, source=False)
        old_personal, old_personal_raw = self.fx.remote_json(cloud.PERSONAL)
        old_usage, old_usage_raw = self.fx.remote_json(cloud.AI_USAGE)
        marker = self.fx.remote_json(cloud.OWNER_FILE)[1]
        manifest = self.manifest(migrate=True)
        phases = []
        original = cloud.StateRepository.persist

        def persist(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            phases.append(kwargs['leased'])
            self.assertEqual(self.fx.remote_json(cloud.OWNER_FILE)[1], marker)
            if kwargs['leased']:
                lease = self.fx.remote_json(cloud.LEASE)[0]
                self.assertEqual(lease['sourceCodeSHA'], self.fx.source)
                self.assertEqual(lease['runId'], '12345')
                self.assertNotIn(cloud.SOURCE_USAGE, self.fx.remote_names())
            return result

        with mock.patch.object(cloud.StateRepository, 'persist', new=persist):
            result = self.apply(manifest)
        self.assertEqual(phases, [True, False])
        self.assertEqual(result['collectionStatus'], 'applied-saved')
        self.assertNotIn(cloud.LEASE, self.fx.remote_names())
        half, half_raw = self.fx.remote_json(cloud.HALF_MONTH)
        source, source_raw = self.fx.remote_json(cloud.SOURCE_USAGE)
        self.assertEqual(half['schedules'], [])
        self.assertEqual(source['receipts'], {})
        self.assertEqual(source['baseline']['personalBudgets'], {'2026-09-06': {'searches': 18, 'posts': 11}})
        self.assertEqual(source['baseline']['sourceImports'], old_usage['sourceImports'])
        self.assertEqual(source['baseline']['historicalImages'], self.old_images)
        cloud.validate_half_month_links(half, source, old_usage, old_personal)
        self.assertEqual(self.fx.remote_json(cloud.PERSONAL)[1], old_personal_raw)
        self.assertEqual(self.fx.remote_json(cloud.AI_USAGE)[1], old_usage_raw)
        self.now += dt.timedelta(hours=1)
        manifest['expectedStateSHA'] = self.fx.git(self.fx.remote, 'rev-parse', cloud.REF).decode().strip()
        self.apply(manifest)
        self.assertEqual(self.fx.remote_json(cloud.HALF_MONTH)[1], half_raw)
        self.assertEqual(self.fx.remote_json(cloud.SOURCE_USAGE)[1], source_raw)
        self.assertEqual(self.fx.remote_json(cloud.PERSONAL)[1], old_personal_raw)
        self.assertEqual(self.fx.remote_json(cloud.AI_USAGE)[1], old_usage_raw)

    def test_migration_hash_mismatches_and_raw_are_rejected_before_lease(self):
        self.seed(half=False, source=False)
        for field in ('sourceHash', 'expectedPersonalBudgetsHash', 'raw'):
            manifest = self.manifest(migrate=True)
            if field == 'raw':
                manifest['sourceMigration']['raw'] = RAW
            else:
                manifest['sourceMigration'][field] = 'f' * 64
            self.fx.saved_mode(manifest)
            with self.subTest(field=field):
                self.before_lease_failure('source_migration_baseline_changed|unsafe_state|invalid_saved_manifest')

    def test_approved_source_delta_after_migration_is_atomic_and_keeps_baseline_replay(self):
        self.seed()
        original_source = copy.deepcopy(self.source_state)
        half_raw = self.fx.remote_json(cloud.HALF_MONTH)[1]
        owner_raw = self.fx.remote_json(cloud.OWNER_FILE)[1]
        manifest = self.manifest()
        manifest['sourceReceipts'] = [{
            'receiptId': cloud.data_hash('later-source-receipt'),
            'sourceHash': cloud.data_hash('later-source-evidence'),
            'date': '2026-09-06', 'searches': 1, 'posts': 1,
        }]
        result = self.apply(manifest)
        self.assertEqual(result['collectionStatus'], 'applied-saved')
        personal, personal_raw = self.fx.remote_json(cloud.PERSONAL)
        usage, usage_raw = self.fx.remote_json(cloud.AI_USAGE)
        sources, source_raw = self.fx.remote_json(cloud.SOURCE_USAGE)
        self.assertEqual(personal['budgets']['2026-09-06'], {'searches': 19, 'posts': 12})
        self.assertEqual(len(usage['sourceImports']), 2)
        self.assertEqual(sources['baseline'], original_source['baseline'])
        for field in ('receipts', 'nextRequests', 'cooldowns', 'paused'):
            self.assertEqual(sources[field], original_source[field])
        self.sources.validate_legacy(sources, personal, usage)
        self.assertEqual(self.sources.legacy_budgets(sources), personal['budgets'])
        self.assertEqual(self.fx.remote_json(cloud.HALF_MONTH)[1], half_raw)
        self.assertEqual(self.fx.remote_json(cloud.OWNER_FILE)[1], owner_raw)
        self.assertNotIn(cloud.LEASE, self.fx.remote_names())

        manifest['expectedStateSHA'] = self.fx.git(self.fx.remote, 'rev-parse', cloud.REF).decode().strip()
        self.apply(manifest)
        self.assertEqual(self.fx.remote_json(cloud.PERSONAL)[1], personal_raw)
        self.assertEqual(self.fx.remote_json(cloud.AI_USAGE)[1], usage_raw)
        self.assertEqual(self.fx.remote_json(cloud.SOURCE_USAGE)[1], source_raw)
        self.apply(self.manifest(migrate=True))
        self.assertEqual(self.fx.remote_json(cloud.SOURCE_USAGE)[1], source_raw)

    def test_source_reconciliation_does_not_authorize_new_same_phase_ai_import(self):
        self.seed()
        self.schedule_inputs()
        manifest = self.manifest(entries=[self.facts_entry(self.half_state)],
                                 imports=[self.schedule_import()])
        manifest['sourceReceipts'] = [{
            'receiptId': cloud.data_hash('source-and-ai-together'),
            'sourceHash': cloud.data_hash('source-and-ai-evidence'),
            'date': '2026-09-06', 'searches': 1, 'posts': 0,
        }]
        self.fx.saved_mode(manifest)
        self.before_lease_failure('saved_manifest_rejected')

    def test_legacy_counter_divergence_is_rejected_before_lease_on_collect_and_restore(self):
        self.private['budgets']['2026-09-06']['posts'] += 1
        self.seed()
        for mode in ('restore', 'collect'):
            self.fx.args.mode = mode
            with self.subTest(mode=mode):
                self.before_lease_failure('source_usage_budget_mismatch')

    def test_unknown_raw_canonical_half_state_is_rejected_before_lease(self):
        self.half_state['raw'] = RAW
        self.seed()
        self.fx.args.mode = 'restore'
        self.before_lease_failure('invalid_schedule_state|invalid_schedule_fields|unsafe_state|private_state')
        del self.half_state['raw']
        self.source_state['raw'] = RAW
        self.fx.bare_commit({cloud.HALF_MONTH: self.half_state, cloud.SOURCE_USAGE: self.source_state})
        self.before_lease_failure('invalid_source_usage|unsafe_state|private_state')

    def test_data_only_capsule_preserves_pause_and_longest_http_cooldown(self):
        pause = {'reason': 'access_denied', 'host': self.personal.SEARCH_HOST,
                 'at': '2026-09-06T02:00:00Z', 'retryAt': '2026-09-07T02:00:00Z',
                 'httpStatus': 403}
        self.private['paused'] = pause
        self.transport['cooldowns'][self.personal.SEARCH_HOST] = '2026-09-07T03:00:00Z'
        self.seed(half=False, source=False)
        before = copy.deepcopy((self.private, self.usage, self.transport))
        half, source = cloud.prepare_half_month_saved(
            self.manifest(migrate=True), None, None, self.private, self.usage, self.transport,
            root=self.fx.root, personal_collector=self.personal, now=NOW)
        self.assertEqual((self.private, self.usage, self.transport), before)
        self.assertEqual(source['paused'], pause)
        self.assertEqual(source['cooldowns'][self.personal.SEARCH_HOST], '2026-09-07T03:00:00Z')
        self.assertEqual(source['cooldowns']['pbs.twimg.com'], self.transport['cooldowns']['pbs.twimg.com'])
        self.assertEqual(source['baseline']['personalBudgets'], self.private['budgets'])
        cloud.validate_half_month_links(half, source, self.usage, self.private)

    def test_stale_manifest_and_existing_lease_do_not_rebase_or_overwrite(self):
        self.seed(half=False, source=False)
        manifest = self.manifest(migrate=True)
        self.fx.bare_commit({cloud.HTTP_STATE: {'schemaVersion': 1, 'cooldowns': {}}})
        self.fx.saved_mode(manifest)
        self.before_lease_failure('saved_manifest_stale')
        manifest['expectedStateSHA'] = self.fx.git(self.fx.remote, 'rev-parse', cloud.REF).decode().strip()
        self.fx.bare_commit({cloud.LEASE: self.fx.lease('67890')})
        manifest['expectedStateSHA'] = self.fx.git(self.fx.remote, 'rev-parse', cloud.REF).decode().strip()
        self.fx.saved_mode(manifest)
        before = self.fx.git(self.fx.remote, 'show-ref')
        with self.assertRaisesRegex(cloud.CloudError, 'unresolved_lease'):
            self.without_children(self.invoke)
        self.assertEqual(self.fx.git(self.fx.remote, 'show-ref'), before)

    def test_migration_cas_conflict_preserves_other_writer_and_exact_recovery(self):
        self.seed(half=False, source=False)
        manifest = self.manifest(migrate=True)
        original = cloud.StateRepository.persist
        winning = {}

        def persist(repo, *args, **kwargs):
            if not kwargs['leased']:
                changed = copy.deepcopy(self.private)
                changed['budgets']['2026-09-07'] = {'searches': 1, 'posts': 0}
                self.fx.bare_commit({cloud.PERSONAL: changed})
                winning['raw'] = self.fx.remote_json(cloud.PERSONAL)[1]
            return original(repo, *args, **kwargs)

        with mock.patch.object(cloud.StateRepository, 'persist', new=persist):
            with self.assertRaisesRegex(cloud.CloudError, 'state_push_failed'):
                self.apply(manifest)
        self.assertEqual(self.fx.remote_json(cloud.PERSONAL)[1], winning['raw'])
        self.assertIn(cloud.LEASE, self.fx.remote_names())
        self.assertNotIn(cloud.SOURCE_USAGE, self.fx.remote_names())
        recovery = self.fx.root / 'recovery'
        source = self.sources.load_state(recovery / cloud.SOURCE_USAGE, required=True)
        self.assertEqual(source['baseline']['personalBudgets'], self.private['budgets'])
        self.assertEqual(source['receipts'], {})

    def test_usage_import_and_saved_facts_need_two_canonical_applies(self):
        self.seed(half=False, source=False)
        self.schedule_inputs()
        entry = self.facts_entry()
        combined = self.manifest(migrate=True, entries=[entry], imports=[self.schedule_import()])
        self.fx.saved_mode(combined)
        self.before_lease_failure('saved_manifest_rejected|saved_half_month_usage_missing')
        usage_manifest = self.manifest(imports=[self.schedule_import()])
        self.apply(usage_manifest)
        self.assertNotIn(cloud.HALF_MONTH, self.fx.remote_names())
        accounted_raw = self.fx.remote_json(cloud.AI_USAGE)[1]
        self.usage = self.fx.remote_json(cloud.AI_USAGE)[0]
        facts_manifest = self.manifest(migrate=True, entries=[entry])
        self.apply(facts_manifest)
        half = self.fx.remote_json(cloud.HALF_MONTH)[0]
        self.assertEqual(len(half['schedules']), 1)
        self.assertEqual(len(half['schedules'][0]['days']), 6)
        self.assertEqual(len(half['savedImports']), 1)
        self.assertEqual(self.fx.remote_json(cloud.AI_USAGE)[1], accounted_raw)
        half_raw = self.fx.remote_json(cloud.HALF_MONTH)[1]
        source_raw = self.fx.remote_json(cloud.SOURCE_USAGE)[1]
        facts_manifest['expectedStateSHA'] = self.fx.git(self.fx.remote, 'rev-parse', cloud.REF).decode().strip()
        self.apply(facts_manifest)
        self.assertEqual(self.fx.remote_json(cloud.HALF_MONTH)[1], half_raw)
        self.assertEqual(self.fx.remote_json(cloud.SOURCE_USAGE)[1], source_raw)
        self.assertEqual(self.fx.remote_json(cloud.AI_USAGE)[1], accounted_raw)

    def test_selected_manifest_requires_exact_separate_approval_documents(self):
        self.seed()
        approval_hash = 'f' * 64
        entry = self.facts_entry()
        entry['amendment']['selectionProof'] = {'approvalManifestHash': approval_hash}
        manifest = self.manifest(entries=[entry])
        for approvals in (None, [], {'g' * 64: {}}, {approval_hash: []}, {},
                          {'e' * 64: {}}, {approval_hash: {}, 'e' * 64: {}}):
            with self.subTest(approvals=approvals):
                invalid = copy.deepcopy(manifest)
                invalid['halfMonthSelections'] = approvals
                self.fx.saved_mode(invalid)
                self.before_lease_failure(
                    'invalid_half_month_selections|half_month_selection_approval_mismatch')
        separate_document = {'originPacketHash': 'a' * 64}
        manifest['halfMonthSelections'] = {approval_hash: separate_document}
        self.fx.saved_mode(manifest)
        self.before_lease_failure('half_month_selection_apply_approval_mismatch')
        manifest['halfMonthSelectionApply'] = {}
        self.fx.saved_mode(manifest)
        parsed = cloud.read_saved_manifest(self.fx.environment)
        self.assertEqual(cloud.saved_half_month_selections(parsed),
                         {approval_hash: separate_document})
        orphaned = copy.deepcopy(manifest)
        del orphaned['halfMonthAmendments'][0]['amendment']['selectionProof']
        self.fx.saved_mode(orphaned)
        self.before_lease_failure('half_month_selection_approval_mismatch')

    def test_selected_partial_cloud_apply_requires_both_approvals_and_restores_public_subset(self):
        timing_fixture = load('half_cloud_selected_fixture', Path(__file__).with_name('test_half_month_timing.py'))
        helper = timing_fixture.TimingOnlyTests(methodName='runTest')
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        packet = helper.packet(helper.partial_pattern())
        original_packet = copy.deepcopy(packet)
        proof = helper.accounted_proof(packet)
        approved = helper.selection_for(packet, helper.independent_expected_sets())
        entry = helper.selected_entry(packet, proof, approved)
        self.half_state = copy.deepcopy(helper.state)
        for receipt in helper.usage['imports'].values():
            self.ai.apply_import(self.usage, receipt)
        self.source_state = self.baseline()
        self.now = NOW + dt.timedelta(hours=1)
        self.seed()
        self.schedule_inputs()
        manifest = self.manifest(entries=[entry])
        manifest['halfMonthSelections'] = {approved['approvalManifestHash']: approved['document']}
        self.fx.saved_mode(manifest)
        self.before_lease_failure('half_month_selection_apply_approval_mismatch')
        manifest['halfMonthSelectionApply'] = helper.final_approval(entry)
        for field in ('entryHash', 'selectionApprovalHash', 'stage'):
            invalid = copy.deepcopy(manifest)
            invalid['halfMonthSelectionApply'][field] = 'wrong'
            self.fx.saved_mode(invalid)
            self.before_lease_failure('saved_manifest_rejected')
        invalid = copy.deepcopy(manifest)
        invalid['halfMonthSelections'][approved['approvalManifestHash']]['untouchedSlotIds'] = []
        self.fx.saved_mode(invalid)
        self.before_lease_failure('saved_manifest_rejected')
        untouched = {name: self.fx.remote_json(name)[1] for name in (
            cloud.SNAPSHOT, cloud.HTTP_STATE, cloud.PERSONAL, cloud.AI_USAGE, cloud.SOURCE_USAGE,
            cloud.OWNER_FILE)}
        self.apply(manifest)
        self.half_state, selected_bytes = self.fx.remote_json(cloud.HALF_MONTH)
        self.assertEqual(self.half_state, helper.apply_entry(helper.state, entry))
        self.assertEqual(packet, original_packet)
        self.assertEqual(packet['status'], 'partial')
        self.assertEqual(len(packet['analysis']['timingOnly']['pendingSlotIds']), 4)
        for name, expected in untouched.items():
            self.assertEqual(self.fx.remote_json(name)[1], expected)
        manifest['expectedStateSHA'] = self.fx.git(self.fx.remote, 'rev-parse', cloud.REF).decode().strip()
        self.apply(manifest)
        self.assertEqual(self.fx.remote_json(cloud.HALF_MONTH)[1], selected_bytes)
        self.assert_restored_public_site(seed=False)

    def test_canonical_restore_is_byte_exact_and_pages_exposes_only_normalized_feed(self):
        self.ai.apply_import(self.usage, self.schedule_import())
        self.source_state = self.baseline()
        self.half_state = self.saved.apply_amendments(
            None, [self.facts_entry()], self.usage, self.half, schedule=fixture.SCHEDULE,
            insights={}, accounts=fixture.ACCOUNTS, personal_state=self.private, now=NOW)
        self.assert_restored_public_site()

    def test_work_timing_revision_survives_private_restore_and_public_javascript(self):
        saved_fixture = load('half_cloud_timing_fixture', Path(__file__).with_name('test_half_month_saved.py'))
        helper = saved_fixture.SavedHalfMonthTests(methodName='runTest')
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        old = helper.legacy_state()
        self.half_state = helper.apply_timing(old, helper.timing_entry(old))
        self.now = NOW + dt.timedelta(hours=1)
        for receipt in helper.usage['imports'].values():
            self.ai.apply_import(self.usage, receipt)
        self.source_state = self.baseline()
        self.assertEqual(len(self.half_state['revisions']), 2)
        self.assert_restored_public_site()

    def test_bound_timing_only_contract_survives_restore_without_public_core_reinterpretation(self):
        timing_fixture = load('half_cloud_bound_timing_fixture', Path(__file__).with_name('test_half_month_timing.py'))
        helper = timing_fixture.TimingOnlyTests(methodName='runTest')
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        self.half_state, _ = helper.apply(helper.packet())
        self.now = NOW + dt.timedelta(hours=1)
        for receipt in helper.usage['imports'].values():
            self.ai.apply_import(self.usage, receipt)
        self.source_state = self.baseline()
        self.assertEqual(len(self.half_state['revisions']), 2)
        self.assertEqual({row['analysis']['contract'] for row in self.half_state['revisions'].values()},
                         {'half-month-schedule-v1', 'half-month-timing-v1'})
        self.assert_restored_public_site()

    def test_native_timing_attestation_restores_without_duplicate_usage_import(self):
        timing_fixture = load('half_cloud_native_timing_fixture', Path(__file__).with_name('test_half_month_timing.py'))
        helper = timing_fixture.TimingOnlyTests(methodName='runTest')
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        packet, proof = helper.native_packet()
        entry = timing_fixture.timing.to_amendment(packet, proof, helper.usage)
        self.half_state = helper.fx.apply_timing(helper.state, entry)
        self.now = NOW + dt.timedelta(hours=1)
        for receipt in helper.usage['imports'].values():
            self.ai.apply_import(self.usage, receipt)
        self.usage['receipts'].update(copy.deepcopy(helper.usage['receipts']))
        self.usage['nextRequestAt'] = helper.usage['nextRequestAt']
        self.source_state = self.baseline()
        self.assertEqual(len(self.usage['receipts']), 1)
        self.assertEqual(self.half_state['savedImports'][self.half.digest(entry['amendment'])]['accountingKind'], 'native')
        self.assert_restored_public_site()

    def assert_restored_public_site(self, *, seed=True):
        if seed:
            self.seed()
        self.fx.bare_commit({
            cloud.SNAPSHOT: json.loads((TOOLS.parent / 'data' / cloud.SNAPSHOT).read_bytes())})
        expected = {name: self.fx.remote_json(name)[1] for name in (
            cloud.SNAPSHOT, cloud.HTTP_STATE, cloud.HALF_MONTH, cloud.SOURCE_USAGE,
            cloud.AI_USAGE, cloud.PERSONAL)}
        references = self.fx.git(self.fx.remote, 'show-ref')
        self.fx.args.mode = 'restore'
        self.fx.environment['HALF_MONTH_SCHEDULE_ENABLED'] = 'false'
        self.without_children(self.invoke)
        for name, raw in expected.items():
            self.assertEqual((self.fx.output.parent / name).read_bytes(), raw)
        # Production validates the restored private checkout before Pages projection.
        for name in (*pages.PUBLIC_FILES, 'unauthorized.html'):
            if not name.endswith('.json'):
                shutil.copyfile(TOOLS.parent / name, self.fx.root / name)
        shutil.copyfile(TOOLS.parent / 'data' / 'members.json', self.fx.root / 'data' / 'members.json')
        for name in ('tests', 'tools/data', 'assets/events'):
            shutil.copytree(TOOLS.parent / name, self.fx.root / name, dirs_exist_ok=True)
        node = shutil.which('node') or str(self.personal.NODE_FALLBACK)
        result = cloud.child_process(
            [node, '--test', *('tests/' + name + '.js' for name in (
                'validate-schedule', 'date-defaults', 'range-rendering', 'month-calendar',
                'observed-shifts', 'validate-insights', 'store-outlook', 'headless-render'))],
            cwd=self.fx.root, environment=cloud.safe_environment(os.environ))
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode('utf-8'))
        (self.fx.root / 'data' / 'raw-sentinel.json').write_text(RAW, encoding='utf-8')
        output = self.fx.root / 'site'
        manifest = pages.stage(output, self.fx.source, root=self.fx.root, clock=lambda: NOW)
        public = json.loads((output / 'data' / cloud.HALF_MONTH).read_bytes())
        self.assertEqual(public, self.half.public_state(self.half_state))
        self.assertIn('data/' + cloud.HALF_MONTH, manifest['files'])
        self.assertEqual(set(manifest['files']), set(pages.PUBLIC_FILES) | {
            'assets/events/' + path.name for path in (TOOLS.parent / 'assets' / 'events').glob('*.svg')})
        for name in (cloud.SOURCE_USAGE, cloud.AI_USAGE, 'raw-sentinel.json'):
            self.assertFalse((output / 'data' / name).exists())
        for forbidden in ('savedImports', 'revisions', 'timingAmendment', 'timingOnly', 'authorizationHash',
                          'semanticResultHash', 'resultAttestation', 'accountingKind',
                          'selectionProof', 'approvalManifestHash', 'untouchedSlotIds', 'applyApproval',
                          'coreHash', 'slotId', 'pendingSlotIds', 'requestHash', 'bodyHash', 'payloadHash', RAW):
            self.assertNotIn(forbidden, json.dumps(public))
        script = """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const api = require('./app.js');
const value = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
assert.equal(api.validateHalfMonthSchedules(value), value);
const effective = api.buildEffectiveSchedule({}, value);
for (const source of value.schedules) {
  for (const fact of source.workTiming?.facts ?? []) {
    const note = api.resolveWorkTiming({schedule:effective, personal:null, insights:null,
      dateKey:fact.serviceDate, shift:fact.shift, name:source.name});
    assert.equal(api.workTimingLabel(note), api.workTimingLabel(fact));
  }
}
"""
        checked = cloud.child_process(
            [node, '-e', script, str(output / 'data' / cloud.HALF_MONTH)],
            cwd=self.fx.root, environment=cloud.safe_environment(os.environ))
        self.assertEqual(checked.returncode, 0, (checked.stdout + checked.stderr).decode('utf-8'))
        for name, raw in expected.items():
            self.assertEqual((self.fx.output.parent / name).read_bytes(), raw)
            self.assertEqual(self.fx.remote_json(name)[1], raw)
        self.assertEqual(self.fx.git(self.fx.remote, 'show-ref'), references)

    def source_url(self, component, kind, index):
        if kind == 'searches':
            if component == 'official':
                return collector.SEARCH_URLS[index]
            return self.personal.account_search_url(component + str(index))
        if kind == 'images':
            return f'https://pbs.twimg.com/media/OFFLINE_{index}.jpg'
        offset = {'official': 0, 'personal': 100, 'schedule': 200}[component]
        tid = str(int(legacy.make_id('2026-09-07T02:00:00Z')) + offset + index)
        return f'https://cdn.syndication.twimg.com/tweet-result?id={tid}&lang=ja&token=a'

    def scheduled_run(self, *, personal_ai=1, half_ai=1, buffer_count=0,
                      half_enabled=True, when=None, official_posts=17):
        self.now = when or dt.datetime(2026, 9, 7, 12, 30, tzinfo=cloud.JST)
        self.enable(half=half_enabled)
        phases, issued_ai, source_counts, allocations, buffer_sizes = [], [], [], [], []
        buffer_paths = []

        def sleep(seconds):
            self.now += dt.timedelta(seconds=seconds)

        def source_requests(component, state, environment, counts):
            with self.sources.SharedSource(
                    state / cloud.SOURCE_USAGE, run_id=environment['CLOUD_COLLECTION_RUN_ID'],
                    component=component, clock=lambda: self.now, sleep=sleep,
                    personal_path=state / cloud.PERSONAL) as ledger:
                for kind, count in counts.items():
                    for index in range(count):
                        key = ledger.reserve(kind, self.source_url(component, kind, index))
                        if component == 'personal':
                            private = self.personal.read_state(state / cloud.PERSONAL)
                            day = ledger.state['receipts'][key]['date']
                            private['budgets'].setdefault(day, {'searches': 0, 'posts': 0})[kind] += 1
                            host = ledger.state['receipts'][key]['host']
                            private['lastRequests'][host] = collector.iso(self.now)
                            collector.atomic_json(state / cloud.PERSONAL, private)
                        ledger.issued(key)
                        ledger.finish(key)
                        source_counts.append((component, kind))

        def ai_requests(component, state, environment, phase, count):
            count_done = 0
            limit = 1 if component == 'schedule' else int(environment['CLOUD_COLLECTION_ANALYSIS_LIMIT'])
            allocations.append((component, phase, limit))
            with self.ai.SharedUsage(
                    state / cloud.AI_USAGE, run_id=environment['CLOUD_COLLECTION_RUN_ID'],
                    component=component, clock=lambda: self.now, sleep=sleep, request_limit=limit) as usage:
                for index in range(count):
                    key = cloud.data_hash(['half-cloud-ai', component, phase, index])
                    try:
                        usage.reserve(key, IDENTITY)
                    except self.ai.UsageFailure as exc:
                        self.assertEqual(exc.reason, 'azure_budget_exhausted')
                        break
                    usage.issued(key)
                    usage.finish(key, 'not_schedule' if component == 'schedule' else 'no_event')
                    issued_ai.append(component)
                    count_done += 1
            return count_done

        def invoke(component, state, report, environment):
            phase = environment.get('CLOUD_COLLECTION_BUFFER_MODE', '') if component == 'official' else 'source'
            phases.append((component, phase))
            self.assertEqual(environment['CLOUD_COLLECTION_SOURCE_ENABLED'], 'true')
            self.assertEqual(environment['CLOUD_COLLECTION_RUN_ID'], RUN_ID)
            if component != 'official':
                path = cloud.analysis_buffer_path(self.fx.root, state)
                if path.exists():
                    buffer_sizes.append(len(json.loads(path.read_bytes())['items']))
                    self.assertLessEqual(buffer_sizes[-1], 2)
            if component == 'official':
                snapshot, _ = cloud.validate_snapshot(state / cloud.SNAPSHOT, collector)
                if phase == 'replay':
                    requests = {'searches': 0, 'posts': 0}
                    path = cloud.analysis_buffer_path(self.fx.root, state)
                    buffered = json.loads(path.read_bytes())
                    resolved = ai_requests(component, state, environment, phase, len(buffered['items']))
                    for item in buffered['items'][:resolved]:
                        snapshot['officialAnalysis']['queue'].pop(item['id'])
                        snapshot['pending'] = [entry for entry in snapshot['pending'] if entry['id'] != item['id']]
                    path.unlink()
                else:
                    requests = {'searches': 2, 'posts': official_posts}
                    source_requests(component, state, environment, requests)
                    ai_requests(component, state, environment, phase, 1)
                    if buffer_count:
                        analysis = collector.analysis_module()
                        snapshot['officialAnalysis'] = analysis.empty_state()
                        entries = []
                        for index in range(buffer_count):
                            tid = str(int(legacy.make_id('2026-09-07T02:00:00Z')) + index)
                            payload = {**legacy.payload(tid), 'rawOnly': RAW}
                            post = collector.validate_post(tid, payload, dt.date(2026, 9, 7),
                                                           dt.date(2026, 9, 7), self.now)
                            snapshot['posts'].append(post)
                            stamp = collector.iso(self.now)
                            body_hash = analysis.digest(payload['text'])
                            snapshot['officialAnalysis']['queue'][tid] = {
                                'createdAt': post['createdAt'], 'fetchedAt': stamp,
                                'bodyHash': body_hash, 'reason': 'azure_budget_exhausted'}
                            snapshot['pending'].append({
                                'id': tid, 'url': collector.canonical(tid), 'reason': 'azure_budget_exhausted',
                                'firstSeenAt': stamp, 'lastAttemptAt': stamp, 'attempts': 1})
                            entries.append({'id': tid, 'fetchedAt': stamp, 'bodyHash': body_hash, 'payload': payload})
                status = 'partial' if snapshot['pending'] else 'no-new'
                snapshot['checkedAt'] = collector.iso(self.now)
                snapshot['lastRun'] = {
                    'status': status, 'dateFrom': '2026-09-06', 'dateTo': '2026-09-07',
                    'requests': requests, 'sourceCount': 0 if phase == 'replay' else 2,
                    'newPostCount': 0 if phase == 'replay' else buffer_count,
                    'newNameCount': 0 if phase == 'replay' else buffer_count * 2,
                    'pendingCount': len(snapshot['pending']), 'failures': []}
                collector.atomic_json(state / cloud.SNAPSHOT, snapshot)
                if phase == 'write':
                    path = cloud.analysis_buffer_path(self.fx.root, state)
                    buffered = collector.make_analysis_buffer(
                        snapshot, environment['CLOUD_COLLECTION_RUN_ID'], 'f' * 64,
                        entries if buffer_count else [], self.now)
                    collector.atomic_json(path, buffered)
                    buffer_paths.append(path)
            elif component == 'personal':
                if half_enabled:
                    self.assertEqual(environment['CLOUD_COLLECTION_PERSONAL_SEARCHES'], '2')
                requests = {'searches': int(environment.get('CLOUD_COLLECTION_PERSONAL_SEARCHES', '3')),
                            'posts': 1 if personal_ai else 0}
                source_requests(component, state, environment, requests)
                ai_requests(component, state, environment, phase, personal_ai)
                snapshot = self.personal.read_state(state / cloud.PERSONAL)
                status = 'no-new' if personal_ai else 'no-results'
                snapshot['lastRun'] = {'status': status, 'requests': requests}
                collector.atomic_json(state / cloud.PERSONAL, snapshot)
            else:
                requests = {'searches': 1, 'posts': 1, 'images': 1}
                source_requests(component, state, environment, requests)
                count = ai_requests(component, state, environment, phase, half_ai)
                requests['analysis'] = count
                status = 'no-results'
                snapshot = self.half.read_state(state / cloud.HALF_MONTH)
                snapshot['lastRun'] = {'status': status}
                collector.atomic_json(state / cloud.HALF_MONTH, snapshot)
            code = 2 if status == 'partial' else 0
            collector.atomic_json(report, {
                'component': component, 'status': status, 'exitCode': code, 'requests': requests})
            return code

        with mock.patch.object(collector, 'ROOT', self.fx.root), \
                mock.patch.object(collector, 'analysis_module', return_value=self.official_analysis), \
                mock.patch.object(collector, 'utc_now', side_effect=lambda: self.now), \
                mock.patch.object(cloud, 'invoke_collector', side_effect=lambda r, s, p, e: invoke('official', s, p, e)), \
                mock.patch.object(cloud, 'invoke_personal_collector', side_effect=lambda r, s, p, e: invoke('personal', s, p, e)), \
                mock.patch.object(cloud, 'invoke_half_month_collector', side_effect=lambda r, s, p, e: invoke('schedule', s, p, e)):
            result = self.invoke()
        for path in buffer_paths:
            self.assertFalse(path.exists())
        self.assertFalse(list(self.fx.root.glob('.cc-work-*')))
        self.assertNotIn(cloud.ANALYSIS_BUFFER, self.fx.remote_names())
        for path in (self.fx.root / 'recovery').iterdir():
            self.assertNotIn(RAW.encode(), path.read_bytes())
        for name in self.fx.remote_names():
            self.assertNotIn(RAW.encode(), self.fx.remote_json(name)[1])
        return result, phases, issued_ai, source_counts, allocations, buffer_sizes

    def test_scheduled_three_components_share_actual_ledgers_three_thirty_and_source_caps(self):
        self.ai.apply_import(self.usage, {
            'receiptId': cloud.data_hash('today-27'), 'sourceHash': cloud.data_hash('today-evidence'),
            'date': '2026-09-07', 'counts': {'requests': 27},
            'modelBreakdown': [{'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 27}]})
        self.source_state = self.baseline()
        self.seed()
        result, phases, ai, sources, allocations, _ = self.scheduled_run()
        self.assertEqual([component for component, _ in phases], ['official', 'personal', 'schedule'])
        self.assertEqual(ai, ['official', 'personal', 'schedule'])
        self.assertEqual([limit for _, _, limit in allocations], [1, 1, 1])
        self.assertEqual(sum(kind == 'searches' for _, kind in sources), 5)
        self.assertEqual(sum(kind in ('posts', 'images') for _, kind in sources), 20)
        self.assertEqual(sum(component == 'schedule' and kind == 'images' for component, kind in sources), 1)
        usage = self.fx.remote_json(cloud.AI_USAGE)[0]
        self.assertEqual(self.ai.usage_counts(usage, RUN_ID, self.now), {'run': 3, 'day': 30, 'remaining': 0})
        for component in ('official', 'personal', 'schedule'):
            with self.ai.SharedUsage(self.fx.output.parent / cloud.AI_USAGE, run_id='next-run',
                                     component=component, clock=lambda: self.now, sleep=lambda _: None) as shared:
                with self.assertRaisesRegex(self.ai.UsageFailure, 'azure_budget_exhausted'):
                    shared.check()
        source = self.fx.remote_json(cloud.SOURCE_USAGE)[0]
        personal = self.fx.remote_json(cloud.PERSONAL)[0]
        cloud.validate_half_month_links(self.fx.remote_json(cloud.HALF_MONTH)[0], source, usage, personal)
        self.assertEqual(personal['budgets']['2026-09-06'], {'searches': 18, 'posts': 11})
        self.assertEqual(personal['budgets']['2026-09-07'], {'searches': 2, 'posts': 1})
        self.assertEqual(source['baseline']['personalBudgets'], self.private['budgets'])
        self.assertEqual(usage['imports'], self.usage['imports'])
        self.assertEqual(usage['sourceImports'], self.usage['sourceImports'])
        self.assertEqual(result['persistenceStatus'], 'saved')

    def test_unused_personal_ai_returns_to_official_source_zero_resume_after_half(self):
        self.seed()
        result, phases, ai, sources, _, sizes = self.scheduled_run(personal_ai=0, buffer_count=3)
        self.assertEqual(phases, [('official', 'write'), ('personal', 'source'),
                                  ('schedule', 'source'), ('official', 'replay')])
        self.assertEqual(ai, ['official', 'schedule', 'official'])
        self.assertEqual(sizes, [2, 2])
        self.assertEqual(sum(component == 'official' and kind == 'posts' for component, kind in sources), 17)
        official = self.fx.remote_json(cloud.SNAPSHOT)[0]
        self.assertEqual(official['lastRun']['requests'], {'searches': 2, 'posts': 17})
        self.assertEqual(len(official['posts']), 3)
        self.assertEqual(len(official['pending']), 2)
        self.assertEqual(result['officialCollectionStatus'], 'partial')

    def test_1830_never_starts_personal_child_and_preserves_its_old_budgets(self):
        self.seed()
        result, phases, ai, sources, _, _ = self.scheduled_run(
            when=dt.datetime(2026, 9, 7, 18, 30, tzinfo=cloud.JST), official_posts=16)
        self.assertEqual([component for component, _ in phases], ['official', 'schedule'])
        self.assertEqual(ai, ['official', 'schedule'])
        self.assertFalse(any(component == 'personal' for component, _ in sources))
        private = self.fx.remote_json(cloud.PERSONAL)[0]
        self.assertEqual(private['budgets'], self.private['budgets'])
        self.assertEqual(private['posts'], self.private['posts'])
        self.assertEqual(result['personalCollectionStatus'], 'outside-window')

    def test_turning_half_flag_off_accepts_existing_schedule_receipt_and_keeps_source_accounting(self):
        path = self.fx.base / 'prior-ai.json'
        self.ai.atomic_json(path, self.usage)
        with self.ai.SharedUsage(path, run_id='earlier-run', component='schedule',
                                 clock=lambda: NOW, sleep=lambda _: None) as shared:
            key = cloud.data_hash('earlier-schedule')
            shared.reserve(key, IDENTITY)
            shared.issued(key)
            shared.finish(key, 'not_schedule')
        self.usage = self.ai.load_state(path)
        self.seed()
        _, phases, _, sources, _, _ = self.scheduled_run(half_enabled=False, official_posts=17)
        self.assertEqual([component for component, _ in phases], ['official', 'personal'])
        self.assertFalse(any(component == 'schedule' for component, _ in sources))
        usage = self.fx.remote_json(cloud.AI_USAGE)[0]
        self.assertTrue(any(receipt['component'] == 'schedule' for receipt in usage['receipts'].values()))
        self.assertIsInstance(cloud.official_allocation(
            self.fx.output.parent / cloud.AI_USAGE, 'future-run', self.now, True, True), int)
        self.sources.validate_legacy(self.fx.remote_json(cloud.SOURCE_USAGE)[0],
                                     self.fx.remote_json(cloud.PERSONAL)[0], usage)

    def test_child_flags_keep_shared_source_and_feed_after_half_flag_is_disabled(self):
        state = self.fx.root / '.cc-work-0123456789abcdef' / 'collected'
        state.mkdir(parents=True)
        env = {**self.fx.environment, 'HALF_MONTH_SCHEDULE_ENABLED': 'false',
               'CLOUD_COLLECTION_SOURCE_ENABLED': 'true', 'CLOUD_COLLECTION_RUN_ID': RUN_ID,
               'CLOUD_COLLECTION_HALF_MONTH_PRESENT': 'true',
               'CLOUD_COLLECTION_PERSONAL_SEARCHES': '0'}
        calls = []

        def child(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b'', b'')

        with mock.patch.object(cloud, 'child_process', side_effect=child):
            cloud.invoke_collector(self.fx.root, state, self.fx.base / 'official-report.json', env)
            cloud.invoke_personal_collector(self.fx.root, state, self.fx.base / 'personal-report.json', env)
        for argv in calls:
            self.assertEqual(argv[argv.index('--source-state') + 1], str(state / cloud.SOURCE_USAGE))
            self.assertEqual(argv[argv.index('--source-run-id') + 1], RUN_ID)
        self.assertEqual(calls[1][calls[1].index('--half-month-snapshot') + 1], str(state / cloud.HALF_MONTH))
        self.assertEqual(calls[1][calls[1].index('--max-searches') + 1], '0')


if __name__ == '__main__':
    unittest.main()
