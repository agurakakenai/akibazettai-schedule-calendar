"""Offline: python -m unittest discover -s tools/tests -p test_cloud_collection.py"""
import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('cloud_collection', ROOT / 'tools' / 'cloud-collection.py')
cloud = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloud)
collector = cloud.load_collector()
NOW = dt.datetime(2026, 9, 5, 20, tzinfo=dt.timezone.utc)
CREATED = '2026-09-05T09:17:05Z'
TID = '2096165714604486679'
TOKEN = 'github_pat_OFFLINE_SENTINEL_NEVER_LOG_OR_STORE'


def make_id(created):
    milliseconds = int(collector.timestamp(created).timestamp() * 1000)
    return str((milliseconds - 1288834974657) << 22)


OTHER = make_id('2026-09-05T09:18:00Z')
THIRD = make_id('2026-09-05T09:19:00Z')


def payload(tid=TID):
    return {
        'id_str': tid,
        'user': {'id_str': collector.AUTHOR_ID, 'screen_name': collector.AUTHOR},
        'created_at': collector.iso(collector.snowflake_time(tid)),
        'text': '【アキバ絶対領域】\nよるにゃんこ\n\nあむ\nこい\n⊂(´ω´⊂)))',
    }


def fact(tid=TID):
    return collector.validate_post(
        tid, payload(tid), dt.date(2026, 9, 4), dt.date(2026, 9, 5), NOW)


def pending(tid=TID):
    return {'id': tid, 'url': collector.canonical(tid), 'reason': 'network_error',
            'firstSeenAt': CREATED, 'lastAttemptAt': CREATED, 'attempts': 1}


class OfflineClient:
    def __init__(self, ids=(TID,), posts=None, search_failure=None, on_http=None):
        self.ids = list(ids)
        self.posts = posts if posts is not None else {tid: payload(tid) for tid in ids}
        self.search_failure = search_failure
        self.on_http = on_http or (lambda: None)
        self.searches = []
        self.requests = []

    def begin_run(self):
        pass

    def search(self, url):
        self.on_http()
        self.searches.append(url)
        if self.search_failure:
            raise self.search_failure
        return self.ids

    def fetch_post(self, tid):
        self.on_http()
        self.requests.append(tid)
        value = self.posts[tid]
        if isinstance(value, Exception):
            raise value
        return value


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.base = ROOT / ('.cc-test-' + uuid.uuid4().hex[:12])
        self.base.mkdir()
        self.addCleanup(cloud.remove_tree, self.base)
        self.root = self.base / 'checkout'
        self.root.mkdir()
        self.remote = self.base / 'remote.git'
        self.git(self.base, 'init', '--quiet', '--bare', str(self.remote))
        self.git(self.root, 'init', '--quiet', '-b', 'main')
        self.output = self.root / 'data' / cloud.SNAPSHOT
        collector.atomic_json(self.output, collector.empty_snapshot())
        (self.root / 'tools' / 'data').mkdir(parents=True)
        (self.root / 'tools' / 'data' / 'shifts.csv').write_text('tweet_id\n', encoding='utf-8')
        (self.root / 'trusted.py').write_text('main checkout only\n', encoding='utf-8')
        self.git(self.root, 'add', '.')
        self.git(self.root, 'commit', '--quiet', '-m', 'Offline main fixture')
        self.git(self.root, 'push', '--quiet', str(self.remote), 'HEAD:refs/heads/main')
        self.git(self.remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.source = self.git(self.root, 'rev-parse', 'HEAD').decode().strip()
        self.event_path = self.root / 'event.json'
        self.event_path.write_text(json.dumps({
            'repository': {'full_name': cloud.REPOSITORY, 'fork': False},
            'ref': cloud.MAIN_REF, 'deleted': False,
        }), encoding='utf-8')
        self.environment = {
            **os.environ,
            'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': cloud.REPOSITORY,
            'GITHUB_SERVER_URL': 'https://github.com',
            'GITHUB_REF': cloud.MAIN_REF, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_WORKFLOW_REF': cloud.REPOSITORY + '/.github/workflows/deploy-pages.yml@refs/heads/main',
            'GITHUB_HEAD_REF': '', 'GITHUB_BASE_REF': '',
            'GITHUB_RUN_ID': '12345', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_EVENT_PATH': str(self.event_path), 'GH_TOKEN': TOKEN,
        }
        self.environment.pop('GITHUB_OUTPUT', None)
        self.args = argparse.Namespace(
            mode='collect', output=Path('data') / cloud.SNAPSHOT,
            recovery_dir=Path('recovery'))
        patch = mock.patch.object(cloud, 'REMOTE', str(self.remote))
        patch.start()
        self.addCleanup(patch.stop)
        self.network = mock.patch('urllib.request.OpenerDirector.open',
                                  side_effect=AssertionError('Live HTTP is prohibited'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def git(self, cwd, *args):
        result = cloud.child_process(
            ['git', '-c', 'user.name=offline fixture',
             '-c', 'user.email=offline@example.invalid',
             '-c', 'commit.gpgSign=false', '-c', 'core.autocrlf=false',
             '-c', 'core.longpaths=true',
             '-c', 'core.hooksPath=' + str(self.base / 'disabled-hooks'),
             '-c', 'init.templateDir=', *args], cwd=cwd,
            environment=cloud.safe_environment(os.environ))
        if result.returncode:
            self.fail('Offline fixture git failed: ' + result.stderr.decode(errors='replace'))
        return result.stdout

    def run_cloud(self, client=None):
        client = client or OfflineClient()

        def offline_collect(root, state, report, environment):
            self.assertEqual(root, self.root)
            args = collector.argument_parser().parse_args([
                '--once', '--days', '2', '--max-posts', '20',
                '--snapshot', str(state / cloud.SNAPSHOT), '--report', str(report)])
            with contextlib.redirect_stdout(io.StringIO()):
                return collector.run(
                    args, curated=self.root / 'tools' / 'data' / 'shifts.csv',
                    client=client, clock=lambda: NOW)

        with mock.patch.object(cloud, 'invoke_collector', side_effect=offline_collect):
            return cloud.orchestrate(
                self.args, root=self.root, environment=self.environment, collector=collector)

    def remote_json(self, name, revision=cloud.REF):
        raw = self.git(self.remote, 'show', revision + ':' + name)
        return json.loads(raw), raw

    def remote_names(self, revision=cloud.REF):
        return set(self.git(self.remote, 'ls-tree', '-r', '--name-only', revision)
                   .decode().splitlines())

    def bare_commit(self, files):
        workspace = self.base / ('state-' + uuid.uuid4().hex[:12])
        workspace.mkdir()
        self.git(workspace, 'init', '--quiet')
        refs = self.git(self.remote, 'for-each-ref', '--format=%(refname)', cloud.REF)
        if refs:
            self.git(workspace, 'fetch', '--quiet', str(self.remote), cloud.REF)
            self.git(workspace, 'checkout', '--quiet', '-b', cloud.BRANCH, 'FETCH_HEAD')
        else:
            self.git(workspace, 'checkout', '--quiet', '--orphan', cloud.BRANCH)
        for name, value in files.items():
            path = workspace / name
            if value is None:
                path.unlink(missing_ok=True)
            else:
                collector.atomic_json(path, value)
        self.git(workspace, 'add', '--all')
        self.git(workspace, 'commit', '--quiet', '-m', 'Offline state fixture')
        self.git(workspace, 'push', '--quiet', str(self.remote), 'HEAD:' + cloud.REF)

    def lease(self, run_id='12345'):
        return {'schemaVersion': 1, 'managedBy': cloud.MANAGER,
                'leaseId': uuid.uuid4().hex,
                'repository': cloud.REPOSITORY, 'runId': run_id, 'runAttempt': '1',
                'createdAt': '2000-01-01T00:00:00Z', 'sourceCodeSHA': self.source}

    def seed_branch(self, state=None, limits=None, lease=None):
        files = {
            cloud.SNAPSHOT: state if state is not None else collector.empty_snapshot(),
            cloud.HTTP_STATE: {'schemaVersion': 1, 'cooldowns': limits or {}},
            cloud.OWNER_FILE: cloud.STATE_OWNER,
        }
        if lease:
            files[cloud.LEASE] = lease
        self.bare_commit(files)

    def assert_read_only_failure(self, reason):
        references = self.git(self.remote, 'show-ref')
        output = self.output.read_bytes()
        commands = []
        original = cloud.child_process

        def record(argv, **kwargs):
            commands.append(argv)
            return original(argv, **kwargs)

        with mock.patch.object(cloud, 'child_process', side_effect=record), \
                mock.patch.object(cloud, 'invoke_collector') as collect:
            with self.assertRaisesRegex(cloud.CloudError, '^' + reason + '$'):
                cloud.orchestrate(self.args, root=self.root,
                                  environment=self.environment, collector=collector)
            collect.assert_not_called()
        self.assertFalse(any('push' in command or 'commit' in command for command in commands))
        self.assertEqual(self.git(self.remote, 'show-ref'), references)
        self.assertEqual(self.output.read_bytes(), output)

    def test_first_collection_has_remote_lease_before_http_and_data_only_history(self):
        (self.root / 'unrelated.py').write_text('do not include me', encoding='utf-8')
        self.git(self.root, 'add', 'unrelated.py')
        index_before = (self.root / '.git' / 'index').read_bytes()
        checks = []

        def check_lease():
            lease, _ = self.remote_json(cloud.LEASE)
            self.assertEqual(lease['runId'], '12345')
            self.assertEqual(lease['sourceCodeSHA'], self.source)
            self.assertEqual(self.remote_names(), cloud.FILES)
            checks.append(True)

        result = self.run_cloud(OfflineClient(on_http=check_lease))
        self.assertEqual(result['collectionCode'], 0)
        self.assertEqual(result['collectionStatus'], 'ok')
        self.assertEqual(result['persistenceStatus'], 'saved')
        self.assertEqual(result['sourceCodeSHA'], self.source)
        self.assertEqual(result['stateSource'], 'seed')
        self.assertTrue(checks)
        self.assertEqual(self.remote_names(), {cloud.SNAPSHOT, cloud.HTTP_STATE, cloud.OWNER_FILE})
        history = self.git(self.remote, 'rev-list', cloud.REF).decode().splitlines()
        self.assertEqual(len(history), 2)
        for revision in history:
            self.assertLessEqual(self.remote_names(revision), cloud.FILES)
            self.assertEqual(self.remote_json(cloud.OWNER_FILE, revision)[0], cloud.STATE_OWNER)
        self.assertNotIn(self.source, history)
        self.assertEqual((self.root / '.git' / 'index').read_bytes(), index_before)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD').decode().strip(), self.source)
        self.assertEqual(self.remote_json(cloud.SNAPSHOT)[0]['posts'], [fact()])
        self.assertEqual(self.output.read_bytes(), self.remote_json(cloud.SNAPSHOT)[1])
        self.assertFalse((self.output.parent / cloud.OWNER_FILE).exists())
        self.assertFalse((self.root / 'recovery' / cloud.OWNER_FILE).exists())
        self.assertFalse(list(self.root.glob('.cc-work-*')))

    def test_restore_latest_branch_not_old_main_seed_and_no_collection(self):
        self.run_cloud()
        collector.atomic_json(self.output, collector.empty_snapshot())
        self.args.mode = 'restore'
        with mock.patch.object(cloud, 'invoke_collector', side_effect=AssertionError('no HTTP')):
            result = cloud.orchestrate(self.args, root=self.root, environment={}, collector=collector)
        self.assertEqual(result['stateSource'], 'branch')
        self.assertEqual(json.loads(self.output.read_bytes())['posts'], [fact()])
        self.assertEqual(self.output.read_bytes(), self.remote_json(cloud.SNAPSHOT)[1])
        self.assertEqual(self.output.with_suffix('.http-state.json').read_bytes(),
                         self.remote_json(cloud.HTTP_STATE)[1])

    def test_missing_branch_restore_preserves_existing_snapshot_bytes(self):
        real_seed = (ROOT / 'data' / cloud.SNAPSHOT).read_bytes()
        self.output.write_bytes(real_seed)
        self.args.mode = 'restore'
        result = self.run_cloud()
        self.assertEqual(self.output.read_bytes(), real_seed)
        self.assertEqual(result['stateCommit'], '')
        self.assertEqual(result['stateSource'], 'seed')
        self.assertEqual(self.git(self.remote, 'for-each-ref', '--format=%(refname)', cloud.REF), b'')
        self.assertFalse((self.root / 'recovery').exists())

    def test_seed_restore_has_no_ten_post_ceiling(self):
        source = ROOT / 'data' / cloud.SNAPSHOT
        state = json.loads(source.read_bytes())
        state['posts'] = [fact(str(int(TID) + index)) for index in range(11)]
        seeded = json.dumps(state, ensure_ascii=False).encode('utf-8')
        read_bytes = Path.read_bytes
        with mock.patch.object(Path, 'read_bytes',
                               lambda path: seeded if path == source else read_bytes(path)):
            self.test_missing_branch_restore_preserves_existing_snapshot_bytes()

    def test_missing_seed_fails_instead_of_creating_empty_facts(self):
        self.output.unlink()
        self.args.mode = 'restore'
        with self.assertRaisesRegex(cloud.CloudError, 'missing_or_unsafe_state'):
            self.run_cloud()
        self.assertFalse(self.output.exists())

    def test_append_and_deduplicate_across_runs(self):
        self.run_cloud()
        second = OfflineClient(ids=(TID, OTHER))
        result = self.run_cloud(second)
        self.assertEqual(second.requests, [OTHER])
        self.assertEqual(result['stateSource'], 'branch')
        self.assertEqual({p['id'] for p in self.remote_json(cloud.SNAPSHOT)[0]['posts']}, {TID, OTHER})
        third = OfflineClient(ids=(TID, OTHER))
        result = self.run_cloud(third)
        self.assertEqual(result['collectionStatus'], 'no-new')
        self.assertEqual(third.requests, [])

    def test_resolved_wins_over_old_mirror_pending_and_failures_are_retained(self):
        state = collector.empty_snapshot()
        state['posts'] = [fact()]
        state['resolved'] = [{'id': OTHER, 'url': collector.canonical(OTHER),
                              'reason': 'not_shift_post', 'resolvedAt': CREATED}]
        state['pending'] = [pending(THIRD)]
        self.seed_branch(state)
        mirror = collector.empty_snapshot()
        mirror['pending'] = [pending(OTHER)]
        collector.atomic_json(self.output, mirror)
        client = OfflineClient(ids=(TID, OTHER, THIRD), posts={
            THIRD: collector.FetchFailure('network_error')})
        result = self.run_cloud(client)
        saved = self.remote_json(cloud.SNAPSHOT)[0]
        self.assertEqual(result['collectionCode'], 2)
        self.assertEqual(result['collectionStatus'], 'partial')
        self.assertEqual(client.requests, [THIRD])
        self.assertEqual(saved['posts'], [fact()])
        self.assertEqual([p['id'] for p in saved['pending']], [THIRD])
        self.assertEqual(saved['pending'][0]['attempts'], 2)
        self.assertEqual(saved['resolved'], state['resolved'])
        self.assertNotIn(cloud.LEASE, self.remote_names())

    def test_partial_new_facts_and_pending_both_reach_output_and_recovery(self):
        result = self.run_cloud(OfflineClient(ids=(TID, OTHER), posts={
            TID: payload(), OTHER: collector.FetchFailure('network_error')}))
        self.assertEqual(result['collectionStatus'], 'partial')
        saved, raw = self.remote_json(cloud.SNAPSHOT)
        self.assertEqual(saved['posts'], [fact()])
        self.assertEqual([item['id'] for item in saved['pending']], [OTHER])
        self.assertEqual(self.output.read_bytes(), raw)
        self.assertEqual((self.root / 'recovery' / cloud.SNAPSHOT).read_bytes(), raw)
        self.assertEqual((self.root / 'recovery' / cloud.HTTP_STATE).read_bytes(),
                         self.remote_json(cloud.HTTP_STATE)[1])

    def test_cooldowns_merge_sidecar_and_mirror_without_http(self):
        state = collector.empty_snapshot()
        state['posts'] = [fact()]
        state['pending'] = [pending(OTHER)]
        state['cooldowns'] = {'search.yahoo.co.jp': '2026-09-06T20:00:00Z'}
        self.seed_branch(state, {collector.POST_HOST: '2026-09-06T22:00:00Z'})
        collector.atomic_json(self.output.with_suffix('.http-state.json'), {
            'schemaVersion': 1, 'cooldowns': {'search.yahoo.co.jp': '2026-09-06T23:00:00Z'}})
        client = OfflineClient(on_http=lambda: self.fail('Cooldown must prevent every HTTP request'))
        result = self.run_cloud(client)
        saved = self.remote_json(cloud.SNAPSHOT)[0]
        self.assertEqual(result['collectionCode'], 3)
        self.assertEqual(result['collectionStatus'], 'unavailable')
        self.assertEqual(saved['posts'], [fact()])
        self.assertEqual(saved['pending'][0]['id'], OTHER)
        self.assertEqual(saved['cooldowns'], {
            'search.yahoo.co.jp': '2026-09-06T23:00:00Z',
            collector.POST_HOST: '2026-09-06T22:00:00Z'})
        self.assertEqual(saved['cooldowns'], self.remote_json(cloud.HTTP_STATE)[0]['cooldowns'])
        self.assertNotIn(cloud.LEASE, self.remote_names())

    def assert_throttled(self, status):
        client = OfflineClient(search_failure=collector.FetchFailure(
            'http_error', status=status, retry_at=NOW + dt.timedelta(hours=2)))
        result = self.run_cloud(client)
        self.assertEqual(result['collectionCode'], 3)
        self.assertEqual(len(client.searches), 1)
        self.assertEqual(self.remote_json(cloud.HTTP_STATE)[0]['cooldowns'],
                         {'search.yahoo.co.jp': '2026-09-05T22:00:00Z'})
        self.assertNotIn(cloud.LEASE, self.remote_names())

    def test_403_saves_cooldown_without_retrying_same_host(self):
        self.assert_throttled(403)

    def test_429_saves_cooldown_without_retrying_same_host(self):
        self.assert_throttled(429)

    def test_failed_seed_push_prevents_collection_and_has_short_error(self):
        original = cloud.child_process

        def fail_push(argv, **kwargs):
            if 'push' in argv:
                return subprocess.CompletedProcess(argv, 1, b'', TOKEN.encode())
            return original(argv, **kwargs)

        with mock.patch.object(cloud, 'child_process', side_effect=fail_push), \
                mock.patch.object(cloud, 'invoke_collector') as collect:
            with self.assertRaisesRegex(cloud.CloudError, '^lease_push_failed$'):
                cloud.orchestrate(self.args, root=self.root,
                                  environment=self.environment, collector=collector)
            collect.assert_not_called()

    def test_failed_final_push_keeps_lease_and_exact_recovery_then_blocks_new_process(self):
        original = cloud.child_process
        pushes = []

        def fail_final(argv, **kwargs):
            if 'push' in argv:
                pushes.append(argv)
                if len(pushes) == 2:
                    return subprocess.CompletedProcess(argv, 1, b'', TOKEN.encode())
            return original(argv, **kwargs)

        with mock.patch.object(cloud, 'child_process', side_effect=fail_final):
            with self.assertRaisesRegex(cloud.CloudError, '^state_push_failed$'):
                self.run_cloud()
        self.assertEqual(self.remote_names(), cloud.FILES)
        self.assertEqual(self.remote_json(cloud.SNAPSHOT)[0]['posts'], [])
        recovery = self.root / 'recovery' / cloud.SNAPSHOT
        self.assertEqual(json.loads(recovery.read_bytes())['posts'], [fact()])
        self.assertEqual(json.loads(recovery.read_bytes())['lastRun']['status'], 'ok')
        script = (
            'import importlib.util,pathlib,sys;'
            's=importlib.util.spec_from_file_location("cloud",sys.argv[1]);'
            'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
            'm.REMOTE=sys.argv[2];original=m.orchestrate;'
            'm.orchestrate=lambda args: original(args,root=pathlib.Path(sys.argv[3]));'
            'm.invoke_collector=lambda *a: (_ for _ in ()).throw(AssertionError("HTTP forbidden"));'
            'sys.exit(m.main(["--mode","collect","--output","data/observed-shifts.json",'
            '"--recovery-dir","recovery"]))')
        process = cloud.child_process(
            [sys.executable, '-B', '-c', script, str(ROOT / 'tools' / 'cloud-collection.py'),
             str(self.remote), str(self.root)],
            cwd=self.root, environment=self.environment)
        self.assertEqual(process.returncode, 1)
        result = json.loads(process.stdout)
        self.assertEqual(result['reason'], 'unresolved_lease')
        self.assertIn('lease', result['recoveryInstructions'])
        self.assertNotIn(TOKEN.encode(), process.stdout + process.stderr)
        self.assertEqual(self.remote_names(), cloud.FILES)

    def test_collector_local_failure_preserves_http_only_sidecar_and_blocks_retry(self):
        def fail_local(root, state, report, environment):
            collector.atomic_json(state / cloud.HTTP_STATE, {
                'schemaVersion': 1, 'cooldowns': {'search.yahoo.co.jp': '2026-09-06T20:00:00Z'}})
            return 4

        with mock.patch.object(cloud, 'invoke_collector', side_effect=fail_local):
            with self.assertRaisesRegex(cloud.CloudError, 'collector_local_failure'):
                cloud.orchestrate(self.args, root=self.root,
                                  environment=self.environment, collector=collector)
        self.assertEqual(self.remote_names(), cloud.FILES)
        recovery, _ = cloud.validate_transport(self.root / 'recovery' / cloud.HTTP_STATE, collector)
        self.assertEqual(recovery['cooldowns'], {'search.yahoo.co.jp': '2026-09-06T20:00:00Z'})
        with self.assertRaisesRegex(cloud.CloudError, 'unresolved_lease'):
            self.run_cloud()

    def test_invalid_saved_result_is_not_pushed_or_mislabelled_as_seed_recovery(self):
        def poison(root, state, report, environment):
            saved = collector.empty_snapshot()
            saved['text'] = 'full original text'
            collector.atomic_json(state / cloud.SNAPSHOT, saved)
            collector.atomic_json(state / cloud.HTTP_STATE, {
                'schemaVersion': 1, 'cooldowns': {'search.yahoo.co.jp': '2026-09-06T20:00:00Z'}})
            return 3

        with mock.patch.object(cloud, 'invoke_collector', side_effect=poison):
            with self.assertRaisesRegex(cloud.CloudError, 'recovery_state_invalid'):
                cloud.orchestrate(self.args, root=self.root,
                                  environment=self.environment, collector=collector)
        self.assertEqual(self.remote_names(), cloud.FILES)
        self.assertFalse((self.root / 'recovery' / cloud.SNAPSHOT).exists())
        limits = json.loads((self.root / 'recovery' / cloud.HTTP_STATE).read_bytes())
        self.assertEqual(limits['cooldowns'], {'search.yahoo.co.jp': '2026-09-06T20:00:00Z'})

    def test_existing_owned_lease_never_expires_even_same_run(self):
        self.seed_branch(lease=self.lease())
        for mode in ('restore', 'collect'):
            with self.subTest(mode=mode):
                self.args.mode = mode
                with self.assertRaisesRegex(cloud.CloudError, 'unresolved_lease'):
                    self.run_cloud()

    def test_unowned_lease_is_rejected(self):
        lease = self.lease()
        lease['managedBy'] = 'somebody-else'
        self.seed_branch(lease=lease)
        with self.assertRaisesRegex(cloud.CloudError, 'unowned_lease'):
            self.run_cloud()

    def test_two_writers_compare_and_swap_without_force_or_retry(self):
        self.seed_branch()
        repositories = []
        leases = []
        for index in range(2):
            path = self.base / ('racer-' + str(index))
            path.mkdir()
            repository = cloud.StateRepository(path, self.environment)
            repository.initialize(self.root, writing=True)
            lease = self.lease()
            collector.atomic_json(path / cloud.LEASE, lease)
            leases.append(lease)
            repositories.append(repository)
        first, second = repositories
        commands = []
        original = cloud.child_process

        def record(argv, **kwargs):
            commands.append(argv)
            return original(argv, **kwargs)

        with mock.patch.object(cloud, 'child_process', side_effect=record):
            winning_sha = first.persist(collector, leased=True)
            with self.assertRaisesRegex(cloud.CloudError, 'lease_push_failed'):
                second.persist(collector, leased=True)
        self.assertEqual(self.remote_json(cloud.LEASE)[0], leases[0])
        self.assertNotEqual(leases[0]['leaseId'], leases[1]['leaseId'])
        self.assertEqual(self.git(self.remote, 'rev-parse', cloud.REF).decode().strip(), winning_sha)
        self.assertEqual(sum('push' in command for command in commands), 2)
        for command in commands:
            self.assertFalse(any('force' in part for part in command))
            self.assertNotIn('reset', command)
        for repository in repositories:
            config = (repository.path / '.git' / 'config').read_text()
            self.assertNotIn('credential', config)
            self.assertNotIn(TOKEN, config)
            self.assertNotIn('remote', config)

    def test_fork_pr_feature_wrong_repo_and_untrusted_workflow_ref_rejected_before_git(self):
        variations = (
            {'GITHUB_REPOSITORY': 'someone/fork'},
            {'GITHUB_EVENT_NAME': 'pull_request'},
            {'GITHUB_EVENT_NAME': 'pull_request_target'},
            {'GITHUB_REF': 'refs/heads/feature'},
            {'GITHUB_HEAD_REF': 'feature'},
            {'GITHUB_BASE_REF': 'main'},
            {'GITHUB_WORKFLOW_REF': cloud.REPOSITORY + '/.github/workflows/deploy.yml@refs/heads/feature'},
            {'GITHUB_WORKFLOW_REF': 'other/repo/.github/workflows/deploy.yml@refs/heads/main'},
            {'GITHUB_SERVER_URL': 'https://example.invalid'},
            {'GITHUB_ACTIONS': 'false'},
            {'GH_TOKEN': ''},
        )
        for overrides in variations:
            with self.subTest(overrides=overrides), \
                    mock.patch.object(cloud.StateRepository, 'initialize') as initialize:
                with self.assertRaises(cloud.CloudError):
                    cloud.orchestrate(self.args, root=self.root,
                                      environment={**self.environment, **overrides}, collector=collector)
                initialize.assert_not_called()

    def test_trusted_events_require_same_nonfork_repository_and_main_push(self):
        for event_name in ('push', 'workflow_dispatch', 'schedule'):
            cloud.trusted_context({**self.environment, 'GITHUB_EVENT_NAME': event_name})
        for event in (
                {'repository': {'full_name': cloud.REPOSITORY, 'fork': True}},
                {'repository': {'full_name': 'someone/fork', 'fork': False}},
                {'repository': {'full_name': cloud.REPOSITORY, 'fork': False},
                 'ref': cloud.MAIN_REF, 'deleted': True},
                {'repository': {'full_name': cloud.REPOSITORY, 'fork': False},
                 'ref': 'refs/heads/feature', 'deleted': False}):
            self.event_path.write_text(json.dumps(event), encoding='utf-8')
            with self.assertRaisesRegex(cloud.CloudError, 'untrusted_event'):
                cloud.trusted_context({**self.environment, 'GITHUB_EVENT_NAME': 'push'})

    def test_checkout_must_match_latest_main_not_event_sha(self):
        self.environment['GITHUB_SHA'] = '0' * 40
        self.run_cloud()
        (self.root / 'another.py').write_text('not pushed to main', encoding='utf-8')
        self.git(self.root, 'add', 'another.py')
        self.git(self.root, 'commit', '--quiet', '-m', 'Offline feature code')
        with self.assertRaisesRegex(cloud.CloudError, 'checkout_not_latest_main'):
            self.run_cloud()

    def test_unexpected_code_is_rejected_before_checkout_or_http(self):
        self.seed_branch()
        self.bare_commit({'evil.py': {'doNotRun': True}})
        self.assert_read_only_failure('unexpected_state_files')

    def test_existing_data_branch_without_permanent_owner_is_never_adopted(self):
        self.seed_branch()
        self.bare_commit({cloud.OWNER_FILE: None})
        for mode in ('restore', 'collect'):
            with self.subTest(mode=mode):
                self.args.mode = mode
                self.assert_read_only_failure('missing_state_owner')
        self.assertNotIn(cloud.OWNER_FILE, self.remote_names())

    def test_managed_lease_does_not_replace_permanent_ownership(self):
        self.seed_branch(lease=self.lease())
        self.bare_commit({cloud.OWNER_FILE: None})
        self.assert_read_only_failure('missing_state_owner')
        self.assertIn(cloud.LEASE, self.remote_names())

    def test_wrong_owner_schema_repository_branch_or_extra_marker_field_is_rejected(self):
        self.seed_branch()
        for field, value in (
                ('owner', 'another-owner'), ('managedBy', 'another-tool'),
                ('repository', 'someone/fork'), ('branch', 'main'),
                ('schemaVersion', 2), ('schemaVersion', True), ('extra', 'not-owned')):
            with self.subTest(field=field, value=value):
                marker = {**cloud.STATE_OWNER, field: value}
                self.bare_commit({cloud.OWNER_FILE: marker})
                self.assert_read_only_failure('unowned_state_branch')
                self.assertEqual(self.remote_json(cloud.OWNER_FILE)[0], marker)

    def test_target_ref_rejects_main_master_feature_and_inconsistent_configuration(self):
        for branch, ref in (
                ('main', 'refs/heads/main'),
                ('master', 'refs/heads/master'),
                ('feature', 'refs/heads/feature'),
                ('collector-state', 'refs/heads/main'),
                ('main', 'refs/heads/collector-state'),
                ('collector-state', 'refs/tags/collector-state')):
            with self.subTest(branch=branch, ref=ref), \
                    mock.patch.object(cloud, 'BRANCH', branch), \
                    mock.patch.object(cloud, 'REF', ref):
                self.assert_read_only_failure('unsafe_state_target')

    def test_state_branch_cannot_be_remote_default_even_with_correct_marker(self):
        self.seed_branch()
        self.git(self.remote, 'symbolic-ref', 'HEAD', cloud.REF)
        for mode in ('collect', 'restore'):
            with self.subTest(mode=mode):
                self.args.mode = mode
                self.assert_read_only_failure('state_is_default_branch')
        self.assertEqual(self.git(self.remote, 'symbolic-ref', 'HEAD').decode().strip(), cloud.REF)

    def test_unknown_default_branch_stops_instead_of_assuming_main(self):
        self.git(self.remote, 'symbolic-ref', 'HEAD', 'refs/heads/missing')
        self.assert_read_only_failure('remote_default_unknown')

    def test_state_ref_aliasing_main_code_commit_is_rejected(self):
        self.git(self.remote, 'update-ref', cloud.REF, self.source)
        self.assert_read_only_failure('state_aliases_code_branch')
        self.assertEqual(self.git(self.remote, 'rev-parse', cloud.REF).decode().strip(), self.source)

    def test_owner_marker_is_revalidated_before_each_persist(self):
        self.seed_branch()
        workspace = self.base / 'owner-check'
        workspace.mkdir()
        repo = cloud.StateRepository(workspace, self.environment)
        repo.initialize(self.root, writing=True)
        collector.atomic_json(workspace / cloud.LEASE, self.lease())
        collector.atomic_json(workspace / cloud.OWNER_FILE, {
            **cloud.STATE_OWNER, 'owner': 'changed'})
        with mock.patch.object(repo, 'git') as git:
            with self.assertRaisesRegex(cloud.CloudError, 'unowned_state_branch'):
                repo.persist(collector, leased=True)
            git.assert_not_called()

    def test_secret_paths_fulltext_and_unknown_operational_keys_rejected_locally(self):
        mutations = (
            lambda state: state.update(privateKey='-----BEGIN PRIVATE KEY-----'),
            lambda state: state['lastRun'].update(snapshotPath=r'C:\Users\private\state.json'),
            lambda state: state['lastRun'].update(processId=123),
            lambda state: state['posts'][0].update(full_text='original post text'),
            lambda state: state['posts'][0].update(names=['アキバ絶対領域よるにゃんこ']),
            lambda state: state['posts'][0].update(names=['S-1-5-21-123']),
            lambda state: state['lastRun'].update(reason=TOKEN),
            lambda state: state['lastRun'].update(sources=[{
                'url': 'https://example.invalid/private', 'status': 'ok'}]),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                state = collector.empty_snapshot()
                state['posts'] = [fact()]
                mutation(state)
                collector.atomic_json(self.output, state)
                with self.assertRaises((cloud.CloudError, ValueError)):
                    self.run_cloud()
        self.assertEqual(self.git(self.remote, 'for-each-ref', '--format=%(refname)', cloud.REF), b'')

    def test_poisoned_remote_canonical_and_host_rejected(self):
        state = collector.empty_snapshot()
        state['posts'] = [fact()]
        state['posts'][0]['text'] = 'Do not publish or execute'
        self.seed_branch(state)
        with self.assertRaises(cloud.CloudError):
            self.run_cloud()
        self.bare_commit({cloud.SNAPSHOT: collector.empty_snapshot(),
                          cloud.HTTP_STATE: {'schemaVersion': 1, 'cooldowns': {
                              'example.invalid': '2026-09-06T00:00:00Z'}}})
        with self.assertRaises(cloud.CloudError):
            self.run_cloud()

    def test_output_and_recovery_cannot_overwrite_code_or_escape_checkout(self):
        for output, recovery in (
                (Path('tools') / 'code.json', Path('recovery')),
                (Path('data') / cloud.SNAPSHOT, Path('tools')),
                (Path('data') / cloud.SNAPSHOT, self.base / 'outside'),
                (Path('data') / cloud.SNAPSHOT, Path('.git'))):
            args = argparse.Namespace(mode='collect', output=output, recovery_dir=recovery)
            with self.subTest(output=output, recovery=recovery):
                with self.assertRaises((cloud.CloudError, ValueError)):
                    cloud.orchestrate(args, root=self.root,
                                      environment=self.environment, collector=collector)

    def test_credentials_never_in_arguments_config_files_or_logs_and_traces_deleted(self):
        self.environment.update(
            GIT_CURL_VERBOSE='0', GIT_TRACE='1', GIT_TRACE_CURL='1',
            GIT_TRACE2_EVENT=str(self.root / 'trace.json'), GH_DEBUG='api',
            GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='credential.helper',
            GIT_CONFIG_VALUE_0='malicious helper', GIT_INDEX_FILE='wrong-index',
            GCM_TRACE='1', GITHUB_TOKEN=TOKEN)
        original = cloud.child_process
        captured = []

        def record(argv, **kwargs):
            environment = kwargs['environment']
            if argv[0] == 'git':
                captured.append(argv)
                self.assertEqual(environment['GH_TOKEN'], TOKEN)
                for key in ('GIT_CURL_VERBOSE', 'GIT_TRACE', 'GIT_TRACE_CURL', 'GIT_TRACE2_EVENT',
                            'GH_DEBUG', 'GIT_CONFIG_COUNT', 'GIT_INDEX_FILE', 'GCM_TRACE'):
                    self.assertNotIn(key, environment)
                self.assertEqual(environment['GIT_TERMINAL_PROMPT'], '0')
                self.assertEqual(environment['GCM_INTERACTIVE'], 'Never')
                self.assertEqual(environment['GH_PROMPT_DISABLED'], '1')
                self.assertIn('credential.helper=!gh auth git-credential', argv)
                self.assertNotIn(TOKEN, ' '.join(argv))
            return original(argv, **kwargs)

        with mock.patch.object(cloud, 'child_process', side_effect=record), \
                contextlib.redirect_stdout(io.StringIO()) as logged:
            result = self.run_cloud()
            cloud.emit(result, {})
        self.assertTrue(captured)
        self.assertNotIn(TOKEN, logged.getvalue())
        self.assertNotIn(str(self.root), logged.getvalue())
        self.assertNotIn('processId', logged.getvalue())
        self.assertFalse((self.root / 'trace.json').exists())
        for name in self.remote_names():
            self.assertNotIn(TOKEN.encode(), self.remote_json(name)[1])
        self.assertNotIn(TOKEN, (self.remote / 'config').read_text())

    def test_real_collector_subprocess_contract_is_headless_and_report_is_not_logged(self):
        completed = subprocess.CompletedProcess([], 2, b'{"processId":123,"path":"private"}', TOKEN.encode())
        with mock.patch.object(cloud, 'child_process', return_value=completed) as child:
            code = cloud.invoke_collector(
                self.root, self.root / 'state', self.root / 'report.json', self.environment)
        self.assertEqual(code, 2)
        argv = child.call_args.args[0]
        self.assertEqual(argv[1:3], ['-I', '-B'])
        self.assertIn(str(self.root / 'tools' / 'collect-shifts.py'), argv)
        self.assertEqual(argv[4:9], ['--once', '--days', '2', '--max-posts', '20'])
        self.assertNotIn('--publish', argv)
        environment = child.call_args.kwargs['environment']
        self.assertNotIn('GH_TOKEN', environment)
        self.assertNotIn('GITHUB_TOKEN', environment)
        with mock.patch.object(cloud.subprocess, 'run', return_value=completed) as run:
            cloud.child_process(['git', 'version'], cwd=self.root, environment={})
        self.assertEqual(run.call_args.kwargs['creationflags'],
                         subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertIs(run.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_github_output_safe_summary_and_cli_failure_redaction(self):
        result = self.run_cloud()
        output = self.root / 'github-output.txt'
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            cloud.emit(result, {'GITHUB_OUTPUT': str(output)})
        self.assertEqual(json.loads(stream.getvalue()), result)
        self.assertIn('collectionStatus=ok\n', output.read_text())
        self.assertIn('stateCommit=' + result['stateCommit'] + '\n', output.read_text())
        self.assertNotIn('processId', output.read_text())
        with mock.patch.object(cloud, 'orchestrate', side_effect=OSError(TOKEN)), \
                mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}), \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            code = cloud.main(['--mode', 'collect'])
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, stream.getvalue() + output.read_text())
        self.assertEqual(json.loads(stream.getvalue())['reason'], 'local_or_validation_failure')


if __name__ == '__main__':
    unittest.main()
