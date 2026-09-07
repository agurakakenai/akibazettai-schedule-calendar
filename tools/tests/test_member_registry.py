"""Standalone offline tests. Invented people/accounts appear only in test fixtures."""
import contextlib
import copy
import csv
import datetime as dt
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('member_registry', TOOLS / 'member-registry.py')
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)
NOW = dt.datetime(2026, 9, 7, 7, 30, tzinfo=registry.UTC)
LATER = NOW + dt.timedelta(hours=1)
REVISION = 'aa962924549141a47ba5f206b580298052be2ee0'
NAME = '試験新人'
URL = 'https://x.com/fixture_member'
SECOND_NAME = '試験厨房'
SECOND_URL = 'https://x.com/fixture_kitchen'


def fixture(account=URL):
    return registry.add_member(registry.empty_registry(), NAME, account, NOW)


def binding(author='12345', handle='fixture_member'):
    return {'authorId': author, 'authorScreenName': handle, 'verifiedAt': registry.stamp(NOW)}


class ProfileTests(unittest.TestCase):
    def test_normalizes_only_known_profile_forms(self):
        for source in (
                URL, 'https://twitter.com/Fixture_Member/', 'HTTPS://X.COM/FIXTURE_MEMBER',
                URL + '?s=21', URL + '/?s=20&t=fixture_share-token',
                URL + '?t=fixture_token'):
            with self.subTest(source=source):
                self.assertEqual(registry.normalize_profile_url(source), URL)

    def test_rejects_search_status_arbitrary_urls_and_reserved_routes(self):
        for source in (
                'http://x.com/fixture_member', '@fixture_member', 'fixture_member',
                'https://x.com.evil.test/fixture_member', 'https://example.test/fixture_member',
                'https://www.x.com/fixture_member', 'https://x.com:443/fixture_member',
                'https://user@x.com/fixture_member', 'https://x.com/search?q=fixture_member',
                URL + '/status/12345', URL + '/photo/1', URL + '/following',
                'https://x.com/i', 'https://x.com/home', 'https://x.com/AKIBAZETTAI',
                'https://x.com/fixture%5fmember', 'https://x.com/fixture-member',
                'https://x.com/' + 'a' * 16, URL + '#', URL + '#profile',
                URL + '?', URL + '?s=20&s=21', URL + '?unknown=1',
                URL + '?s=999', URL + '?t=a/b', URL + '?s=20&',
                ' ' + URL, URL + '\n', 'https://x.com\\@evil.test/member',
                None, 42, [], {}):
            with self.subTest(source=source), self.assertRaises(registry.RegistryError):
                registry.normalize_profile_url(source)


class RegistryTests(unittest.TestCase):
    def test_new_person_needs_no_statistics_schedule_store_or_promotion_date(self):
        value = fixture()
        member = value['members'][0]
        self.assertEqual(member['registeredAt'], registry.stamp(NOW))
        self.assertIsNone(member['homeStore'])
        self.assertIsNone(member['inactiveFrom'])
        self.assertNotIn('promotedAt', member)
        self.assertNotIn('joinedAt', member)
        targets, coverage = registry.collection_population(value, {})
        self.assertEqual(len(targets), 1)
        self.assertEqual(coverage[member['memberId']]['identity'], 'trusted_handle_unbound')
        self.assertEqual(coverage[member['memberId']]['reason'], 'eligible_not_collected')
        self.assertNotIn('shifts', next(iter(targets.values())))
        self.assertNotIn('schedule', registry.public_projection(value))
        self.assertFalse(registry.registry_report(value)['collectionEvaluated'])

    def test_missing_binding_input_is_not_reported_as_verified_or_empty_history(self):
        value = fixture()
        mid = value['members'][0]['memberId']
        self.assertEqual(registry.collection_population(value)[1][mid]['identity'], 'binding_not_loaded')
        self.assertEqual(registry.collection_population(value, {})[1][mid]['identity'],
                         'trusted_handle_unbound')
        self.assertEqual(registry.collection_population(value, {NAME: binding()})[1][mid]['identity'],
                         'author_verified')

    def test_unknown_account_can_be_filled_without_duplicate_identity(self):
        value = fixture(None)
        mid = value['members'][0]['memberId']
        self.assertEqual(registry.collection_population(value, {})[0], {})
        self.assertEqual(registry.collection_population(value, {})[1][mid]['reason'], 'account_unknown')
        changed = registry.update_member(value, NAME, now=LATER, url=URL)
        self.assertEqual(changed['members'][0]['memberId'], mid)
        self.assertEqual(changed['members'][0]['registeredAt'], registry.stamp(NOW))
        self.assertEqual(len(registry.collection_population(changed, {})[0]), 1)
        self.assertIsNone(value['members'][0]['xProfileUrl'])

    def test_add_existing_name_requires_explicit_account_update(self):
        with self.assertRaisesRegex(registry.RegistryError, 'member_already_exists'):
            registry.add_member(fixture(None), NAME, URL, LATER)

    def test_invalid_empty_or_nonstring_url_is_not_silently_an_unknown_account(self):
        for url in ('', False, 0, [], {}):
            with self.subTest(url=url), self.assertRaises(registry.RegistryError):
                fixture(url)

    def test_display_changes_preserve_canonical_id_and_all_previous_names(self):
        value = fixture()
        original = copy.deepcopy(value)
        second = registry.update_member(value, NAME, now=LATER, display_name='試験表示')
        third = registry.update_member(second, '試験表示', now=LATER, display_name='試験改名')
        member = third['members'][0]
        self.assertEqual(member['memberId'], value['members'][0]['memberId'])
        self.assertEqual(member['canonicalName'], NAME)
        self.assertIn('試験表示', member['aliases'])
        for selector in (NAME, '試験表示', '試験改名', member['memberId']):
            self.assertEqual(registry.lookup(third, selector), member)
        self.assertEqual(value, original)
        self.assertEqual(len(registry.collection_population(third, {'試験表示': binding()})[0]), 1)

    def test_collision_checks_include_inactive_people_and_aliases(self):
        value = registry.update_member(fixture(), NAME, now=LATER, display_name='試験表示',
                                       membership='inactive')
        for name, url in ((NAME, SECOND_URL), ('試験表示', SECOND_URL),
                          (SECOND_NAME, 'https://twitter.com/FIXTURE_MEMBER')):
            with self.subTest(name=name, url=url), self.assertRaises(registry.RegistryError):
                registry.add_member(value, name, url, LATER)

    def test_different_existing_handle_is_never_silently_rebound(self):
        value = fixture()
        with self.assertRaisesRegex(registry.RegistryError, 'account_change_requires_review'):
            registry.update_member(value, NAME, now=LATER, url=SECOND_URL)
        same = registry.update_member(value, NAME, now=LATER,
                                      url='https://twitter.com/FIXTURE_MEMBER/')
        self.assertEqual(same, value)

    def test_pause_and_inactive_do_not_remove_history_or_profile(self):
        value = fixture()
        paused = registry.update_member(value, NAME, now=LATER, collection='paused')
        inactive = registry.update_member(paused, NAME, now=LATER, membership='inactive')
        mid = value['members'][0]['memberId']
        self.assertFalse(registry.collection_population(paused, {})[0])
        self.assertFalse(registry.collection_population(inactive, {})[0])
        self.assertEqual(registry.known_identities(inactive)[mid]['xProfileUrl'], URL)
        self.assertIsNone(inactive['members'][0]['inactiveFrom'])
        self.assertEqual(inactive['members'][0]['statusHistory'],
                         [registry.status_of(value['members'][0]), registry.status_of(paused['members'][0])])
        self.assertIn(NAME, registry.display_projection(inactive)['knownNames'])
        self.assertNotIn(NAME, registry.display_projection(inactive)['roster'])

    def test_explicit_reactivation_preserves_history_and_does_not_guess_dates(self):
        value = registry.update_member(fixture(), NAME, now=LATER, membership='inactive',
                                       inactive_from='2026-09-10')
        with self.assertRaisesRegex(registry.RegistryError, 'inactive_date_requires_inactive'):
            registry.update_member(value, NAME, now=LATER, membership='active', collection='enabled')
        resumed = registry.update_member(value, NAME, now=LATER, membership='active',
                                         collection='enabled', clear_inactive_from=True)
        self.assertEqual(len(registry.collection_population(resumed, {})[0]), 1)
        self.assertEqual(len(resumed['members'][0]['statusHistory']), 2)
        self.assertEqual(resumed['members'][0]['registeredAt'], registry.stamp(NOW))

    def test_unknown_and_review_are_not_current_collectable_members(self):
        unknown = registry.update_member(fixture(), NAME, now=LATER, membership='unconfirmed')
        review = registry.update_member(fixture(), NAME, now=LATER, collection='review')
        self.assertFalse(registry.collection_population(unknown, {})[0])
        self.assertFalse(registry.collection_population(review, {})[0])
        with self.assertRaisesRegex(registry.RegistryError, 'nonactive_collection_enabled'):
            registry.update_member(unknown, NAME, now=LATER, collection='enabled')

    def test_no_history_deletion_rekeying_or_role_reclassification(self):
        before = fixture()
        for change in ('delete', 'canonical', 'registered', 'role'):
            after = copy.deepcopy(before)
            if change == 'delete':
                after['members'] = []
            elif change == 'canonical':
                after['members'][0]['canonicalName'] = '試験別名'
            elif change == 'registered':
                after['members'][0]['registeredAt'] = registry.stamp(NOW - dt.timedelta(days=1))
            else:
                after['members'][0]['role'] = 'kitchen'
            with self.subTest(change=change), self.assertRaises(registry.RegistryError):
                registry.validate_transition(before, after)

    def test_status_history_is_append_only_and_timestamps_are_not_backdated(self):
        before = fixture()
        after = registry.update_member(before, NAME, now=LATER, collection='paused')
        after['members'][0]['statusHistory'] = []
        with self.assertRaisesRegex(registry.RegistryError, 'status_history_not_preserved'):
            registry.validate_transition(before, after)
        with self.assertRaisesRegex(registry.RegistryError, 'status_history_time_reversed'):
            registry.update_member(before, NAME, now=NOW - dt.timedelta(seconds=1), collection='paused')

    def test_binding_conflicts_from_either_collector_are_not_ignored(self):
        value = fixture()
        original = copy.deepcopy(value)
        for maps in (({NAME: binding()}, {NAME: binding('67890')}),
                     ({NAME: binding()}, {NAME: binding(handle='other_handle')}),
                     ({NAME: binding(handle='other_handle')}, {})):
            targets, coverage = registry.collection_population(value, *maps)
            self.assertFalse(targets)
            self.assertEqual(coverage[value['members'][0]['memberId']]['identity'],
                             'account_identity_mismatch')
        self.assertEqual(value, original)
        for maps in (({}, {NAME: binding()}), ({NAME: binding()}, {})):
            self.assertEqual(len(registry.collection_population(value, *maps)[0]), 1)

    def test_cross_person_author_or_handle_reuse_rejects_both_including_unregistered_owner(self):
        value = registry.add_member(fixture(), SECOND_NAME, SECOND_URL, NOW, role='kitchen')
        for other in (binding(handle='fixture_kitchen'), binding('67890')):
            targets, _ = registry.collection_population(value, {NAME: binding(), SECOND_NAME: other})
            self.assertFalse(targets)
        targets, _ = registry.collection_population(fixture(),
                                                   {NAME: binding(), '試験旧名': binding()})
        self.assertFalse(targets)

    def test_unbound_new_person_cannot_claim_another_names_bound_handle(self):
        value = fixture()
        for maps in (({'試験旧名': binding()}, {}), ({}, {'試験旧名': binding()})):
            targets, coverage = registry.collection_population(value, *maps)
            self.assertFalse(targets)
            self.assertEqual(coverage[value['members'][0]['memberId']]['identity'],
                             'account_identity_mismatch')

    def test_binding_names_and_identity_types_are_strict(self):
        for field, bad in (('authorId', 12345), ('authorId', True), ('authorId', '0'),
                           ('authorScreenName', 'not/a/handle'), ('verifiedAt', '2026-09-07')):
            item = binding()
            item[field] = bad
            with self.subTest(field=field), self.assertRaises(registry.RegistryError):
                registry.collection_population(fixture(), {NAME: item})
        with self.assertRaises(registry.RegistryError):
            registry.collection_population(fixture(), {NAME: {**binding(), 'body': 'forbidden'}})

    def test_projection_is_deterministic_and_keeps_inactive_identity_without_operational_history(self):
        value = registry.update_member(fixture(), NAME, now=LATER, membership='inactive')
        before = copy.deepcopy(value)
        self.assertEqual(registry.javascript_bytes(value), registry.javascript_bytes(copy.deepcopy(value)))
        raw = registry.javascript_bytes(value).decode()
        self.assertTrue(raw.startswith('// Generated by tools/member-registry.py; do not edit.\n'))
        projected = registry.strict_json(raw.split('window.MEMBER_REGISTRY = ', 1)[1].strip()[:-1])
        self.assertEqual(projected['members'][0]['canonicalName'], NAME)
        self.assertNotIn('statusHistory', projected['members'][0])
        self.assertNotIn('registeredAt', projected['members'][0])
        self.assertEqual(value, before)

    def test_inactive_kitchen_keeps_historical_role_but_leaves_active_counts(self):
        value = registry.add_member(fixture(), SECOND_NAME, SECOND_URL, NOW, role='kitchen')
        before = registry.registry_report(value)
        value = registry.update_member(value, SECOND_NAME, now=LATER, membership='inactive')
        after = registry.registry_report(value)
        self.assertEqual((before['activeCount'], before['activeKitchenCount']), (2, 1))
        self.assertEqual((after['activeCount'], after['activeKitchenCount'], after['activeFloorCount']),
                         (1, 0, 1))
        self.assertIn(SECOND_NAME, registry.display_projection(value)['kitchenStaff'])
        self.assertNotIn(SECOND_NAME, registry.display_projection(value)['roster'])

    def test_schema_rejects_unknown_fields_and_untrusted_account_metadata(self):
        for place in ('root', 'member', 'status'):
            value = fixture()
            if place == 'root':
                value['privatePath'] = 'forbidden'
            elif place == 'member':
                value['members'][0]['rawBody'] = 'forbidden'
            else:
                value['members'][0]['statusHistory'] = [
                    {**registry.status_of(value['members'][0]), 'modelResponse': 'forbidden'}]
            with self.subTest(place=place), self.assertRaises(registry.RegistryError):
                registry.validate_registry(value)
        value = fixture()
        value['members'][0]['accountTrust'] = 'guessed'
        with self.assertRaisesRegex(registry.RegistryError, 'untrusted_profile'):
            registry.validate_registry(value)

    def test_dates_timezone_and_json_are_strict(self):
        for value in ('2026-02-30', '2026-9-7', None, 20260907):
            with self.subTest(value=value), self.assertRaises(registry.RegistryError):
                registry.date_key(value)
        with self.assertRaisesRegex(registry.RegistryError, 'timezone_required'):
            registry.new_member(NAME, URL, NOW.replace(tzinfo=None))
        for text in ('{"schemaVersion":1,"schemaVersion":1}', '{"a":NaN}', '{"a":Infinity}', '{'):
            with self.subTest(text=text), self.assertRaises(registry.RegistryError):
                registry.strict_json(text)

    def test_orders_reject_cycles_and_kitchen_anchors(self):
        value = registry.add_member(fixture(), SECOND_NAME, SECOND_URL, NOW)
        a, b = value['members']
        a['orderBefore'], b['orderBefore'] = b['memberId'], a['memberId']
        with self.assertRaisesRegex(registry.RegistryError, 'cyclic_member_order'):
            registry.validate_registry(value)
        b['orderBefore'] = None
        b['role'] = 'kitchen'
        with self.assertRaisesRegex(registry.RegistryError, 'invalid_order_anchor'):
            registry.validate_registry(value)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.plans = json.loads((TOOLS / 'tests' / 'fixtures' / 'member-registry-plans.json').read_text(
            encoding='utf-8'))

    def test_paused_unknown_retirement_and_confirmed_date_have_different_effects(self):
        original = copy.deepcopy(self.plans)
        active = fixture()
        paused = registry.update_member(active, NAME, now=LATER, collection='paused')
        unknown = registry.update_member(active, NAME, now=LATER, membership='inactive')
        known = registry.update_member(unknown, NAME, now=LATER, inactive_from='2026-09-10')
        for value, retained, review, excluded in (
                (active, 4, 0, 0), (paused, 4, 0, 0), (unknown, 4, 4, 0), (known, 2, 0, 2)):
            impact = registry.plan_impact(value, NAME, self.plans, '2026-09-07')
            self.assertEqual((impact['retainedCount'], impact['reviewCount'],
                              impact['excludedFromPlanViewCount']), (retained, review, excluded))
            self.assertEqual(impact['factsDeleted'], 0)
        self.assertEqual(self.plans, original)
        self.assertIsNone(unknown['members'][0]['inactiveFrom'])

    def test_no_plan_input_is_unknown_not_zero_remaining(self):
        impact = registry.plan_impact(fixture(), NAME, None, '2026-09-07')
        self.assertFalse(impact['evaluated'])
        self.assertNotIn('retainedCount', impact)
        self.assertEqual(impact['reason'], 'plan_inputs_not_supplied')

    def test_plan_references_deduplicate_aliases_without_touching_sources(self):
        value = registry.update_member(fixture(), NAME, now=LATER, display_name='試験表示')
        plans = [{'name': name, 'date': '2026-09-07', 'shift': '昼'} for name in (NAME, '試験表示')]
        self.assertEqual(registry.plan_impact(value, NAME, plans, '2026-09-07')['retainedCount'], 1)
        with self.assertRaisesRegex(registry.RegistryError, 'invalid_plan_reference'):
            registry.plan_impact(value, NAME, [{**plans[0], 'actual': True}], '2026-09-07')


class MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schedule = registry.legacy_fields(TOOLS.parent / 'data' / 'schedule.js')
        with (TOOLS / 'data' / 'accounts.csv').open(encoding='utf-8-sig', newline='') as stream:
            cls.accounts = list(csv.DictReader(stream))
        cls.insights = registry.read_legacy_insights(TOOLS.parent / 'data' / 'store-insights.js')

    def migrate(self, insights=None):
        return registry.migrate_legacy(self.schedule, self.accounts,
                                       self.insights if insights is None else insights, NOW, REVISION)

    def test_saved_40_and_kitchen5_not_all51_accounts_are_active(self):
        value = self.migrate()
        report = registry.registry_report(value, {})
        self.assertEqual((report['activeCount'], report['activeKitchenCount'],
                          report['activeFloorCount'], report['eligibleCount'],
                          report['unresolvedNameCount']), (40, 5, 35, 40, 11))
        self.assertEqual(registry.display_projection(value)['roster'], self.schedule['roster'])
        self.assertEqual(registry.display_projection(value)['kitchenStaff'], self.schedule['kitchenStaff'])
        self.assertEqual(registry.display_projection(value)['normalOrderBefore'],
                         self.schedule['normalOrderBefore'])
        self.assertEqual(registry.lookup(value, 'まこと')['canonicalName'], 'まこっちゃん')
        self.assertIsNone(registry.lookup(value, 'みらい'))
        self.assertIsNotNone(registry.lookup(value, 'みりあ'))
        self.assertIsNone(registry.lookup(value, 'ひじり'))
        self.assertNotIn('promotedAt', registry.json_bytes(value).decode())

    def test_migration_is_deterministic_and_no_stats_or_null_stats_do_not_gate_it(self):
        self.assertEqual(self.migrate(), self.migrate())
        for tendencies in ({}, {name: None for name in self.schedule['roster']}):
            value = self.migrate({'maidTendency': tendencies})
            self.assertEqual(len(registry.collection_population(value, {})[0]), 40)
            self.assertEqual(registry.lookup(value, 'まこと')['canonicalName'], 'まこっちゃん')
        changed = copy.deepcopy(self.insights)
        changed['maidTendency']['あむ']['x'] = 'fixture_wrong'
        with self.assertRaisesRegex(registry.RegistryError, 'legacy_account_identity_mismatch'):
            self.migrate(changed)

    def test_legacy_accounts_and_notes_are_quarantined_not_attached_to_current_names(self):
        value = self.migrate()
        hijiri = next(row for row in value['unresolvedNames'] if row['name'] == 'ひじり')
        self.assertEqual(hijiri['legacyAccounts'][0]['status'], 'legacy-retired-account')
        self.assertIsNone(hijiri['resolvedMemberId'])
        self.assertEqual(set(value['reservedHandles']), {'piano_zettai', 'mochi2_zettai', 'noa_zettai'})
        for name, url in (('ひじり', 'https://x.com/fixture_new'),
                          ('いろは', 'https://x.com/iroha_zettai'),
                          ('試験旧世代', 'https://x.com/piano_zettai'),
                          ('試験旧世代', 'https://x.com/mochi2_zettai'),
                          ('試験旧世代', 'https://x.com/noa_zettai')):
            with self.subTest(name=name), self.assertRaises(registry.RegistryError):
                registry.add_member(value, name, url, LATER)
        serialized = registry.json_bytes(value).decode()
        for field in ('tweets', '"note"', 'xCreated', 'streakStart', 'modelResponse', 'body'):
            self.assertNotIn(field, serialized)

    def test_user_can_register_a_missing_account_name_without_reviving_other_csv_names(self):
        value = self.migrate()
        after = registry.add_member(value, 'みらい', 'https://x.com/fixture_mirai', LATER)
        self.assertEqual(registry.registry_report(after)['activeCount'], 41)
        self.assertEqual(registry.registry_report(after)['unresolvedNameCount'], 10)
        unresolved = next(row for row in after['unresolvedNames'] if row['name'] == 'みらい')
        self.assertEqual(unresolved['resolvedMemberId'], registry.lookup(after, 'みらい')['memberId'])
        self.assertIsNone(registry.lookup(after, 'ひじり'))
        self.assertIsNone(registry.lookup(value, 'みらい'))
        ordered = registry.display_projection(after)['roster']
        self.assertEqual(ordered[-5:], self.schedule['kitchenStaff'])
        self.assertEqual(ordered[-6], 'みらい')

    def test_checked_in_registry_extends_legacy_baseline_and_script_matches(self):
        path = TOOLS.parent / 'data' / 'members.json'
        saved = registry.load_registry(path)
        migrated = registry.migrate_legacy(self.schedule, self.accounts, self.insights,
                                           registry.timestamp(saved['legacySnapshot']['registeredAt']), REVISION)
        registry.validate_transition(migrated, saved)
        registry.check_projection(path, saved)

    def test_baseline_allows_later_admin_only_changes_without_updating_legacy_inputs(self):
        baseline = self.migrate()
        after = registry.add_member(baseline, NAME, URL, LATER)
        after = registry.update_member(after, 'ひかり', now=LATER, membership='inactive')
        after = registry.update_member(after, 'あむ', now=LATER, display_name='試験表示')
        registry.validate_transition(baseline, after)
        self.assertEqual(len(after['members']), 41)
        self.assertEqual(registry.lookup(after, '試験表示')['canonicalName'], 'あむ')
        self.assertEqual(registry.registry_report(after)['activeCount'], 40)


class StorageAndCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'members.json'
        self.value = fixture()
        registry.save_registry(self.path, self.value, None)

    def digest(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registry.main(['--registry', str(self.path), *arguments], clock=lambda: LATER)
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_stale_writer_cannot_overwrite_a_newer_registry(self):
        old_hash = self.digest()
        new = registry.update_member(self.value, NAME, now=LATER, collection='paused')
        registry.save_registry(self.path, new, old_hash)
        saved = self.path.read_bytes()
        with self.assertRaisesRegex(registry.RegistryError, 'registry_changed'):
            registry.save_registry(self.path, self.value, old_hash)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertFalse(self.path.with_suffix('.lock').exists())

    def test_lock_and_unsafe_paths_fail_without_touching_existing_files(self):
        lock = self.path.with_suffix('.lock')
        lock.write_text('another writer', encoding='ascii')
        with self.assertRaisesRegex(registry.RegistryError, 'registry_locked'):
            registry.save_registry(self.path, self.value, self.digest())
        self.assertEqual(lock.read_text(), 'another writer')
        for filename in ('schedule.js', 'store-insights.js', 'accounts.csv', 'other.json'):
            with self.assertRaisesRegex(registry.RegistryError, 'unsafe_registry_output'):
                registry.save_registry(self.root / filename, self.value, None)
            self.assertFalse((self.root / filename).exists())

    def test_projection_mismatch_is_an_error_and_export_repairs_without_touching_other_files(self):
        unrelated = self.root / 'index.html'
        unrelated.write_text('unchanged', encoding='ascii')
        self.path.with_suffix('.js').write_text('stale', encoding='ascii')
        code, error = self.cli('check')
        self.assertEqual((code, error['error']), (2, 'generated_projection_mismatch'))
        code, result = self.cli('export')
        self.assertEqual(code, 0)
        self.assertFalse(result['published'])
        registry.check_projection(self.path, self.value)
        self.assertEqual(unrelated.read_text(), 'unchanged')

    def test_projection_check_accepts_git_crlf_checkout_without_writing(self):
        script = self.path.with_suffix('.js')
        windows = script.read_bytes().replace(b'\n', b'\r\n')
        script.write_bytes(windows)
        code, _ = self.cli('check')
        self.assertEqual(code, 0)
        self.assertEqual(script.read_bytes(), windows)

    def test_interrupted_projection_write_is_explicit_and_recoverable(self):
        changed = registry.update_member(self.value, NAME, now=LATER, collection='paused')
        actual_write = registry.atomic_write

        def fail_script(path, raw):
            if path.suffix == '.js':
                raise OSError('simulated disk failure')
            actual_write(path, raw)

        with mock.patch.object(registry, 'atomic_write', side_effect=fail_script):
            with self.assertRaisesRegex(registry.RegistryError, 'registry_saved_projection_incomplete'):
                registry.save_registry(self.path, changed, self.digest())
        self.assertFalse(self.path.with_suffix('.lock').exists())
        with self.assertRaisesRegex(registry.RegistryError, 'generated_projection_mismatch'):
            registry.check_projection(self.path, registry.load_registry(self.path))
        code, _ = self.cli('export')
        self.assertEqual(code, 0)
        self.assertEqual(list(self.root.glob('.members-*.tmp')), [])

    def test_cli_projection_failure_discloses_saved_json_and_recovery_commands(self):
        actual_write = registry.atomic_write

        def fail_script(path, raw):
            if path.suffix == '.js':
                raise PermissionError('simulated locked output')
            actual_write(path, raw)

        with mock.patch.object(registry, 'atomic_write', side_effect=fail_script):
            code, result = self.cli('add-normal', '--name', SECOND_NAME, '--x', SECOND_URL)
        self.assertEqual((code, result['error']), (2, 'registry_saved_projection_incomplete'))
        self.assertTrue(result['registrySaved'])
        self.assertFalse(result['projectionComplete'])
        self.assertEqual(result['recoveryCommands'], ['check', 'export'])
        self.assertIsNotNone(registry.lookup(registry.load_registry(self.path), SECOND_NAME))
        code, _ = self.cli('export')
        self.assertEqual(code, 0)

    def test_report_and_check_are_read_only_and_do_not_imply_collection(self):
        before = {path: path.read_bytes() for path in self.root.iterdir()}
        for command in ('report', 'check'):
            code, result = self.cli(command)
            self.assertEqual(code, 0)
            self.assertEqual(result['sourceRequests'], 0)
            self.assertEqual(result['analysisRequests'], 0)
            self.assertFalse(result['collectionEvaluated'])
            self.assertFalse(result['published'])
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.iterdir()})

    def test_status_cli_discloses_retained_future_plans_and_unknown_date(self):
        plans = TOOLS / 'tests' / 'fixtures' / 'member-registry-plans.json'
        code, result = self.cli('set-status', '--member', NAME, '--membership', 'inactive',
                                '--plans', str(plans))
        self.assertEqual(code, 0)
        self.assertEqual(result['planImpact']['reviewCount'], 4)
        self.assertEqual(result['planImpact']['retainedCount'], 4)
        self.assertIsNone(registry.load_registry(self.path)['members'][0]['inactiveFrom'])
        code, result = self.cli('set-status', '--member', NAME, '--inactive-from', '2026-09-10',
                                '--plans', str(plans))
        self.assertEqual(code, 0)
        self.assertEqual(result['planImpact']['excludedFromPlanViewCount'], 2)
        self.assertEqual(result['planImpact']['factsDeleted'], 0)
        self.assertIn('inactiveFrom', result['message'])

    def test_status_cli_without_plan_input_does_not_claim_no_future_plans(self):
        code, result = self.cli('set-status', '--member', NAME, '--collection', 'paused')
        self.assertEqual(code, 0)
        self.assertFalse(result['planImpact']['evaluated'])
        self.assertIn('未確認', result['message'])

    def test_bad_plan_input_rejects_before_status_is_written(self):
        plans = self.root / 'bad-plans.json'
        plans.write_text('{}', encoding='ascii')
        before = self.path.read_bytes()
        code, _ = self.cli('set-status', '--member', NAME, '--membership', 'inactive', '--plans', str(plans))
        self.assertEqual(code, 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_supplied_binding_conflict_is_rejected_before_account_save(self):
        empty = fixture(None)
        other = self.root / 'other'
        other.mkdir()
        path = other / 'members.json'
        registry.save_registry(path, empty, None)
        bindings = self.root / 'bindings.json'
        bindings.write_bytes(registry.json_bytes({'identityBindings': {NAME: binding(handle='fixture_other')}}))
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = registry.main(['--registry', str(path), '--bindings', str(bindings),
                                  'set-account', '--member', NAME, '--x', URL], clock=lambda: LATER)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['error'], 'account_identity_mismatch')
        self.assertIsNone(registry.load_registry(path)['members'][0]['xProfileUrl'])

    def test_baseline_detects_removing_an_inactive_historical_identity(self):
        baseline = self.root / 'before.json'
        baseline.write_bytes(self.path.read_bytes())
        self.path.write_bytes(registry.json_bytes(registry.empty_registry()))
        self.path.with_suffix('.js').write_bytes(registry.javascript_bytes(registry.empty_registry()))
        code, result = self.cli('--baseline', str(baseline), 'check')
        self.assertEqual((code, result['error']), (2, 'historical_identity_removed'))

    def test_baseline_accepts_multiple_legitimate_status_changes_and_a_full_cycle(self):
        baseline = self.root / 'before.json'
        baseline.write_bytes(self.path.read_bytes())
        for args in (('--collection', 'paused'), ('--membership', 'inactive'),
                     ('--membership', 'active', '--collection', 'enabled')):
            code, _ = self.cli('set-status', '--member', NAME, *args)
            self.assertEqual(code, 0)
        code, result = self.cli('--baseline', str(baseline), 'check')
        self.assertEqual(code, 0)
        self.assertTrue(result['baselineChecked'])
        self.assertEqual(len(registry.load_registry(self.path)['members'][0]['statusHistory']), 3)

    def test_corrupt_or_missing_authoritative_registry_never_becomes_empty_success(self):
        self.path.write_text('{"schemaVersion":1,"schemaVersion":1}', encoding='ascii')
        code, result = self.cli('report')
        self.assertEqual((code, result['error']), (2, 'duplicate_json_key'))
        self.path.unlink()
        code, result = self.cli('report')
        self.assertEqual((code, result['error']), (2, 'registry_io_failed'))


if __name__ == '__main__':
    unittest.main()
