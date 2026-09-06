"""Offline: python -m unittest discover -s tools/tests -p test_cloud_collection.py"""
import argparse
import contextlib
import copy
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


class OfflinePersonalClient:
    def __init__(self, module, durable, candidates, *, denial=None, post_failure=False,
                 on_http=None, posts=None):
        self.module, self.durable, self.candidates = module, durable, candidates
        self.denial, self.post_failure = denial, post_failure
        self.on_http = on_http or (lambda: None)
        self.posts = posts or {}
        self.calls = []

    def search(self, url, targets, date, now, bindings):
        self.durable.reserve(self.module.SEARCH_HOST, 'searches')
        self.on_http()
        self.calls.append(('search', url))
        if self.denial:
            self.durable.deny(self.module.SEARCH_HOST, 'http_error', status=self.denial)
            raise self.module.Failure('http_error', status=self.denial)
        return copy.deepcopy(self.candidates)

    def fetch_post(self, tid):
        self.durable.reserve(self.module.POST_HOST, 'posts')
        self.on_http()
        self.calls.append(('post', tid))
        if self.post_failure:
            raise self.module.Failure('network_error')
        if tid in self.posts:
            return copy.deepcopy(self.posts[tid])
        raise AssertionError('Seed IDs must not be fetched again')


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

    def personal_seed(self):
        module = cloud.load_personal_collector()
        seed, _ = cloud.validate_personal(
            ROOT / 'tools' / 'tests' / 'fixtures' / 'personal-pilot.json', module, private=False)
        collector.atomic_json(self.output.parent / cloud.PERSONAL, seed)
        return module, seed

    def personal_mode(self, mode):
        self.args.mode = mode
        event = json.loads(self.event_path.read_bytes())
        event['inputs'] = {'mode': mode}
        self.event_path.write_text(json.dumps(event), encoding='utf-8')

    def run_personal_cloud(self, module, *, official_client=None, denial=None,
                           candidates=None, post_failure=False, on_http=None, when=None, posts=None):
        calls = []

        def invoke(root, state, report, environment):
            self.assertEqual(root, self.root)
            now = when or dt.datetime(2026, 9, 6, 3, tzinfo=dt.timezone.utc)
            date = now.astimezone(module.JST).date()
            current = module.read_state(state / cloud.PERSONAL)
            targets = {
                'あむ': {'name': 'あむ', 'handle': 'amu_zettai', 'shifts': ['昼', '夜']},
                'ららこ': {'name': 'ららこ', 'handle': 'rarako_zettai', 'shifts': ['昼']},
            }
            current['originalTargets'].setdefault(date.isoformat(), copy.deepcopy(targets))
            durable = module.DurableHttp(
                current, state / cloud.PERSONAL, state / cloud.HTTP_STATE, date,
                targets, 3, 3, clock=lambda: now, sleep=lambda _: None)
            durable.preflight()
            items = candidates if candidates is not None else [{
                **{key: post[key] for key in (
                    'id', 'url', 'name', 'authorId', 'authorScreenName', 'date')},
                'searchCreatedAt': post['createdAt'],
            } for post in current['posts']]
            client = OfflinePersonalClient(module, durable, items, denial=denial,
                                           post_failure=post_failure, on_http=on_http, posts=posts)
            component, code = module.collect(
                current, durable, client, targets, date, 3, 3, clock=lambda: now,
                roster=['あむ', 'ららこ'])
            calls.extend(client.calls)
            component['exitCode'] = code
            with contextlib.redirect_stdout(io.StringIO()):
                module.official.write_report(component, report)
            return code

        with mock.patch.object(cloud, 'invoke_personal_collector', side_effect=invoke):
            result = self.run_cloud(official_client)
        return result, calls

    def test_first_collection_has_remote_lease_before_http_and_data_only_history(self):
        (self.root / 'unrelated.py').write_text('do not include me', encoding='utf-8')
        self.git(self.root, 'add', 'unrelated.py')
        index_before = (self.root / '.git' / 'index').read_bytes()
        checks = []

        def check_lease():
            lease, _ = self.remote_json(cloud.LEASE)
            self.assertEqual(lease['runId'], '12345')
            self.assertEqual(lease['sourceCodeSHA'], self.source)
            self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL})
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
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL})
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
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL})

    def test_collector_local_failure_preserves_http_only_sidecar_and_blocks_retry(self):
        def fail_local(root, state, report, environment):
            collector.atomic_json(state / cloud.HTTP_STATE, {
                'schemaVersion': 1, 'cooldowns': {'search.yahoo.co.jp': '2026-09-06T20:00:00Z'}})
            return 4

        with mock.patch.object(cloud, 'invoke_collector', side_effect=fail_local):
            with self.assertRaisesRegex(cloud.CloudError, 'collector_local_failure'):
                cloud.orchestrate(self.args, root=self.root,
                                  environment=self.environment, collector=collector)
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL})
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
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL})
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

    def test_legacy_restore_uses_private_personal_seed_without_migrating_remote(self):
        module, seed = self.personal_seed()
        official = collector.empty_snapshot()
        official['posts'] = [fact()]
        self.seed_branch(official)
        before = self.git(self.remote, 'rev-parse', cloud.REF)
        self.args.mode = 'restore'
        result = self.run_cloud()
        restored = module.read_state(self.output.parent / cloud.PERSONAL)
        self.assertEqual(restored['posts'], seed['posts'])
        self.assertEqual(restored['budgets'], {'2026-09-06': {'searches': 7, 'posts': 2}})
        self.assertEqual(len(restored['resolved']), 2)
        self.assertEqual(set(restored['identityBindings']), {'あむ', 'ららこ'})
        self.assertEqual(result['personalStateSource'], 'seed')
        self.assertEqual(result['personalCollectionCode'], -1)
        self.assertEqual(self.output.read_bytes(), self.remote_json(cloud.SNAPSHOT)[1])
        self.assertEqual(self.git(self.remote, 'rev-parse', cloud.REF), before)
        self.assertNotIn(cloud.PERSONAL, self.remote_names())

    def test_existing_official_cron_does_not_migrate_or_run_personal_collection(self):
        self.personal_seed()
        self.environment['GITHUB_EVENT_NAME'] = 'schedule'
        with mock.patch.object(cloud, 'invoke_personal_collector') as invoke:
            result = self.run_cloud()
            invoke.assert_not_called()
        self.assertEqual(result['collectionMode'], 'collect')
        self.assertEqual(result['officialCollectionStatus'], 'ok')
        self.assertNotIn(cloud.PERSONAL, self.remote_names())
        self.assertIn('budgets', json.loads((self.output.parent / cloud.PERSONAL).read_bytes()))

    def test_manual_personal_migration_leases_seed_budgets_before_http_and_never_refetches_seed(self):
        module, seed = self.personal_seed()
        official = collector.empty_snapshot()
        official['posts'] = [fact()]
        self.seed_branch(official)
        self.personal_mode('personal')
        checks = []

        def before_http():
            self.assertEqual(self.remote_names(), cloud.FILES)
            saved = self.remote_json(cloud.PERSONAL)[0]
            self.assertEqual(saved['posts'], seed['posts'])
            self.assertEqual(saved['budgets'], {'2026-09-06': {'searches': 7, 'posts': 2}})
            checks.append(True)

        with mock.patch.object(cloud, 'invoke_collector', side_effect=AssertionError('official HTTP forbidden')):
            result, calls = self.run_personal_cloud(module, on_http=before_http)
        self.assertTrue(checks)
        self.assertEqual(result['officialCollectionCode'], -1)
        self.assertEqual(result['personalCollectionStatus'], 'no-new')
        self.assertFalse(any(kind == 'post' for kind, _ in calls))
        saved = self.remote_json(cloud.PERSONAL)[0]
        self.assertEqual(saved['budgets']['2026-09-06'], {'searches': 7 + len(calls), 'posts': 2})
        self.assertEqual(saved['posts'], seed['posts'])
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.LEASE})
        self.assertEqual(self.remote_json(cloud.SNAPSHOT)[0]['posts'], official['posts'])
        for name in (cloud.SNAPSHOT, cloud.HTTP_STATE, cloud.PERSONAL):
            self.assertEqual((self.output.parent / name).read_bytes(), self.remote_json(name)[1])
            self.assertEqual((self.root / 'recovery' / name).read_bytes(), self.remote_json(name)[1])
        self.assertFalse((self.root / 'recovery' / cloud.OWNER_FILE).exists())

    def test_public_seed_explicit_usage_survives_first_migration_save_and_restore(self):
        module, seed = self.personal_seed()
        self.assertEqual(set(seed), {
            'schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt', 'posts', 'lastRun'})
        self.assertEqual(len(seed['posts']), 2)
        expected = {'2026-09-06': {'searches': 7, 'posts': 2}}
        self.seed_branch()
        self.personal_mode('personal')
        result, calls = self.run_personal_cloud(
            module, when=dt.datetime(2026, 9, 6, 11, tzinfo=dt.timezone.utc))
        self.assertEqual(calls, [])
        self.assertEqual(result['personalCollectionStatus'], 'outside-window')
        state, saved = self.remote_json(cloud.PERSONAL)
        self.assertEqual(state['budgets'], expected)
        self.assertEqual(state['posts'], seed['posts'])
        self.assertEqual(len(state['resolved']), 2)
        self.assertNotIn(cloud.LEASE, self.remote_names())
        self.assertEqual((self.root / 'recovery' / cloud.PERSONAL).read_bytes(), saved)
        commit = self.git(self.remote, 'rev-parse', cloud.REF)
        # A fresh latest-main checkout has only the public seed, not the private
        # budget ledger. Restoring must use the saved ledger, not infer usage.
        collector.atomic_json(self.output.parent / cloud.PERSONAL, seed)
        self.args.mode = 'restore'
        with mock.patch.object(cloud, 'invoke_personal_collector') as invoke:
            restored = self.run_cloud()
            invoke.assert_not_called()
        self.assertEqual(restored['personalStateSource'], 'branch')
        self.assertEqual(module.read_state(self.output.parent / cloud.PERSONAL)['budgets'], expected)
        self.assertEqual((self.output.parent / cloud.PERSONAL).read_bytes(), saved)
        self.assertEqual(self.git(self.remote, 'rev-parse', cloud.REF), commit)

    def assert_both_with_personal_denial(self, status):
        module, _ = self.personal_seed()
        self.personal_mode('both')
        order = []
        result, calls = self.run_personal_cloud(
            module, official_client=OfflineClient(on_http=lambda: order.append('official')),
            denial=status, on_http=lambda: order.append('personal'))
        self.assertEqual(order, ['official', 'official', 'official', 'personal'])
        self.assertEqual(result['officialCollectionStatus'], 'ok')
        self.assertEqual(result['personalCollectionStatus'], 'paused')
        self.assertEqual(result['collectionStatus'], 'partial')
        self.assertEqual(result['persistenceStatus'], 'saved')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.remote_json(cloud.SNAPSHOT)[0]['posts'], [fact()])
        person = self.remote_json(cloud.PERSONAL)[0]
        self.assertEqual(person['paused']['httpStatus'], status)
        self.assertEqual(person['budgets']['2026-09-06'], {'searches': 8, 'posts': 2})
        self.assertIn(module.SEARCH_HOST, self.remote_json(cloud.HTTP_STATE)[0]['cooldowns'])
        self.assertNotIn(cloud.LEASE, self.remote_names())
        with mock.patch.object(cloud, 'orchestrate', return_value=result), \
                mock.patch.dict(os.environ, {'GITHUB_OUTPUT': ''}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.main(['--mode', 'both']), 0)

    def test_both_official_success_and_personal_403_pause_publish_saved_facts(self):
        self.assert_both_with_personal_denial(403)

    def test_both_official_success_and_personal_429_pause_publish_saved_facts(self):
        self.assert_both_with_personal_denial(429)

    def test_both_hands_new_official_and_personal_facts_to_the_same_run_without_replacing_history(self):
        module, seed = self.personal_seed()
        self.personal_mode('both')
        created = '2026-09-06T02:00:00Z'
        tid = make_id(created)
        candidate = {
            'id': tid, 'url': module.public_url('amu_zettai', tid), 'name': 'あむ',
            'authorId': seed['posts'][0]['authorId'], 'authorScreenName': 'amu_zettai',
            'date': '2026-09-06', 'searchCreatedAt': created,
        }
        payload = {
            'id_str': tid, 'created_at': created, 'text': '9月6日\n夜3号店',
            'user': {'id_str': candidate['authorId'], 'screen_name': 'amu_zettai'},
        }
        result, calls = self.run_personal_cloud(module, candidates=[candidate], posts={tid: payload})
        self.assertEqual(result['officialCollectionStatus'], 'ok')
        self.assertEqual(result['personalCollectionStatus'], 'ok')
        self.assertEqual([item for item in calls if item[0] == 'post'], [('post', tid)])
        self.assertEqual(json.loads(self.output.read_bytes())['posts'], [fact()])
        state = json.loads((self.output.parent / cloud.PERSONAL).read_bytes())
        self.assertEqual(state['posts'][:2], seed['posts'])
        self.assertEqual(state['posts'][-1]['events'][0]['storeId'], 's3')
        self.assertEqual(len(state['posts']), 3)
        self.assertEqual((self.output.parent / cloud.PERSONAL).read_bytes(),
                         self.remote_json(cloud.PERSONAL)[1])

    def test_official_cooldown_is_shared_with_personal_without_another_http(self):
        module, _ = self.personal_seed()
        self.personal_mode('both')
        result, calls = self.run_personal_cloud(module, official_client=OfflineClient(
            search_failure=collector.FetchFailure(
                'http_error', status=429,
                retry_at=dt.datetime(2026, 9, 6, 4, tzinfo=dt.timezone.utc))))
        self.assertEqual(calls, [])
        self.assertEqual(result['officialCollectionStatus'], 'unavailable')
        self.assertEqual(result['personalCollectionStatus'], 'paused')
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0]['budgets']['2026-09-06'],
                         {'searches': 7, 'posts': 2})
        self.assertNotIn(cloud.LEASE, self.remote_names())

    def test_personal_cooldown_and_private_state_are_preserved_by_next_official_only_run(self):
        module, _ = self.personal_seed()
        self.personal_mode('personal')
        self.run_personal_cloud(module, denial=429)
        raw = self.remote_json(cloud.PERSONAL)[1]
        self.args.mode = 'collect'
        with mock.patch.object(cloud, 'invoke_personal_collector') as personal_call:
            result = self.run_cloud(OfflineClient(
                on_http=lambda: self.fail('Shared personal cooldown must block official HTTP')))
            personal_call.assert_not_called()
        self.assertEqual(result['officialCollectionStatus'], 'unavailable')
        self.assertEqual(result['personalCollectionCode'], -1)
        self.assertEqual(self.remote_json(cloud.PERSONAL)[1], raw)
        self.assertEqual((self.output.parent / cloud.PERSONAL).read_bytes(), raw)

    def test_existing_personal_pause_and_source_history_survive_old_seed_and_expired_cooldown(self):
        module, seed = self.personal_seed()
        state = module.empty_state()
        module.merge_seed(state, seed)
        state['budgets']['2026-09-06']['searches'] = 23
        state['paused'] = {
            'reason': 'http_error', 'host': module.SEARCH_HOST, 'httpStatus': 403,
            'at': '2026-09-05T03:00:00Z', 'retryAt': '2026-09-05T04:00:00Z'}
        self.seed_branch()
        self.bare_commit({cloud.PERSONAL: state})
        self.personal_mode('both')
        result, calls = self.run_personal_cloud(module)
        self.assertEqual(calls, [])
        saved = self.remote_json(cloud.PERSONAL)[0]
        for key in ('paused', 'budgets', 'posts', 'resolved', 'identityBindings'):
            self.assertEqual(saved[key], state[key])
        self.assertEqual(result['officialCollectionStatus'], 'ok')
        self.assertEqual(result['personalCollectionStatus'], 'paused')
        self.args.mode = 'restore'
        collector.atomic_json(self.output.parent / cloud.PERSONAL, seed)
        restored = self.run_cloud()
        self.assertEqual(restored['personalStateSource'], 'branch')
        self.assertEqual((self.output.parent / cloud.PERSONAL).read_bytes(),
                         self.remote_json(cloud.PERSONAL)[1])

    def test_personal_daily_budget_exhaustion_does_not_block_official_success(self):
        module, seed = self.personal_seed()
        state = module.empty_state()
        module.merge_seed(state, seed)
        state['budgets']['2026-09-06'] = {'searches': 60, 'posts': 30}
        self.seed_branch()
        self.bare_commit({cloud.PERSONAL: state})
        self.personal_mode('both')
        result, calls = self.run_personal_cloud(module)
        self.assertEqual(calls, [])
        self.assertEqual(result['personalCollectionStatus'], 'budget-exhausted')
        self.assertEqual(result['personalCollectionCode'], 2)
        self.assertEqual(result['officialCollectionStatus'], 'ok')
        self.assertNotIn(cloud.LEASE, self.remote_names())

    def test_personal_outside_window_does_not_block_official_success_or_spend_budget(self):
        module, _ = self.personal_seed()
        self.personal_mode('both')
        result, calls = self.run_personal_cloud(
            module, when=dt.datetime(2026, 9, 6, 11, tzinfo=dt.timezone.utc))
        self.assertEqual(calls, [])
        self.assertEqual(result['personalCollectionStatus'], 'outside-window')
        self.assertEqual(result['personalCollectionCode'], 0)
        self.assertEqual(result['officialCollectionStatus'], 'ok')
        self.assertEqual(result['collectionStatus'], 'ok')
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0]['budgets']['2026-09-06'],
                         {'searches': 7, 'posts': 2})

    def test_personal_failure_keeps_pending_resolved_budgets_and_original_targets(self):
        module, seed = self.personal_seed()
        self.personal_mode('personal')
        created = '2026-09-06T02:00:00Z'
        candidate = {
            'id': make_id(created), 'url': module.public_url('amu_zettai', make_id(created)),
            'name': 'あむ', 'authorId': seed['posts'][0]['authorId'],
            'authorScreenName': 'amu_zettai', 'date': '2026-09-06',
            'searchCreatedAt': created,
        }
        result, calls = self.run_personal_cloud(module, candidates=[candidate], post_failure=True)
        self.assertEqual(result['personalCollectionStatus'], 'partial')
        self.assertEqual([item for item in calls if item[0] == 'post'], [('post', candidate['id'])])
        state = self.remote_json(cloud.PERSONAL)[0]
        self.assertEqual(state['pending'][0]['id'], candidate['id'])
        self.assertEqual(state['pending'][0]['attempts'], 1)
        self.assertEqual(len(state['resolved']), 2)
        self.assertEqual(state['posts'], seed['posts'])
        self.assertEqual(state['budgets']['2026-09-06']['posts'], 3)
        self.assertEqual(state['originalTargets']['2026-09-06']['あむ']['shifts'], ['昼', '夜'])
        result, calls = self.run_personal_cloud(module, candidates=[candidate], post_failure=True)
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0]['pending'][0]['attempts'], 2)
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0]['budgets']['2026-09-06']['posts'], 4)

    def test_personal_infra_failure_leaves_lease_and_recovers_official_success(self):
        module, _ = self.personal_seed()
        self.personal_mode('both')

        def failed(root, state, report, environment):
            person = module.read_state(state / cloud.PERSONAL)
            person['budgets']['2026-09-06']['searches'] += 1
            collector.atomic_json(state / cloud.PERSONAL, person)
            collector.atomic_json(state / cloud.HTTP_STATE, {
                'schemaVersion': 1, 'cooldowns': {module.SEARCH_HOST: '2026-09-07T03:00:00Z'}})
            return 4

        with mock.patch.object(cloud, 'invoke_personal_collector', side_effect=failed):
            with self.assertRaisesRegex(cloud.CloudError, 'personal_local_failure'):
                self.run_cloud()
        self.assertEqual(self.remote_names(), cloud.FILES)
        self.assertEqual(self.remote_json(cloud.SNAPSHOT)[0]['posts'], [])
        recovery = self.root / 'recovery'
        self.assertEqual(json.loads((recovery / cloud.SNAPSHOT).read_bytes())['posts'], [fact()])
        self.assertEqual(json.loads((recovery / cloud.PERSONAL).read_bytes())['budgets']['2026-09-06']['searches'], 8)
        self.assertIn(module.SEARCH_HOST, json.loads((recovery / cloud.HTTP_STATE).read_bytes())['cooldowns'])
        self.assert_read_only_failure('unresolved_lease')

    def test_stale_network_status_without_completion_report_is_an_infra_failure(self):
        module, seed = self.personal_seed()
        state = module.empty_state()
        module.merge_seed(state, seed)
        state['lastRun'] = {'status': 'unavailable'}
        self.seed_branch()
        self.bare_commit({cloud.PERSONAL: state})
        self.personal_mode('personal')
        with mock.patch.object(cloud, 'invoke_personal_collector', return_value=3):
            with self.assertRaisesRegex(cloud.CloudError, 'personal_report_missing'):
                self.run_cloud()
        self.assertIn(cloud.LEASE, self.remote_names())

    def test_personal_final_push_failure_keeps_seed_lease_and_exact_budget_recovery(self):
        module, _ = self.personal_seed()
        self.personal_mode('personal')
        original = cloud.child_process
        pushes = []

        def fail_final(argv, **kwargs):
            if 'push' in argv:
                pushes.append(argv)
                if len(pushes) == 2:
                    return subprocess.CompletedProcess(argv, 1, b'', TOKEN.encode())
            return original(argv, **kwargs)

        with mock.patch.object(cloud, 'child_process', side_effect=fail_final):
            with self.assertRaisesRegex(cloud.CloudError, 'state_push_failed'):
                self.run_personal_cloud(module)
        self.assertEqual(self.remote_names(), cloud.FILES)
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0]['budgets']['2026-09-06']['searches'], 7)
        recovery = module.read_state(self.root / 'recovery' / cloud.PERSONAL)
        self.assertGreater(recovery['budgets']['2026-09-06']['searches'], 7)
        self.assert_read_only_failure('unresolved_lease')

    def test_personal_requires_explicit_manual_input_and_is_not_scheduled_yet(self):
        self.personal_seed()
        self.personal_mode('personal')
        self.environment['GITHUB_EVENT_NAME'] = 'schedule'
        self.assert_read_only_failure('personal_requires_manual_run')
        self.environment['GITHUB_EVENT_NAME'] = 'workflow_dispatch'
        self.args.mode = 'both'
        self.assert_read_only_failure('personal_requires_explicit_input')
        self.environment['GITHUB_REF'] = 'refs/heads/feature'
        self.assert_read_only_failure('untrusted_context')

    def test_personal_missing_seed_refuses_migration_without_http_or_empty_replacement(self):
        self.personal_mode('personal')
        self.assert_read_only_failure('missing_personal_seed')
        self.assertEqual(self.git(self.remote, 'for-each-ref', '--format=%(refname)', cloud.REF), b'')

    def test_private_personal_unknown_text_process_path_and_host_are_rejected(self):
        module, seed = self.personal_seed()
        state = module.empty_state()
        module.merge_seed(state, seed)
        for change, reason in (
                (lambda value: value.update(full_text='original text'), 'unsafe_state'),
                (lambda value: value['posts'][0].update(processId=123), 'unsafe_state'),
                (lambda value: value['posts'][0]['events'][0].update(
                    excerpt=r'C:\Users\private\credentials'), 'private_state_rejected'),
                (lambda value: value['originalTargets'].update(
                    {'2026-09-06': {'あむ': {
                        'name': 'あむ', 'handle': 'amu_zettai', 'shifts': ['昼'], 'text': 'original'}}}),
                 'unsafe_state')):
            with self.subTest(reason=reason):
                value = copy.deepcopy(state)
                change(value)
                collector.atomic_json(self.root / 'unsafe-personal.json', value)
                with self.assertRaisesRegex((cloud.CloudError, ValueError), reason + '|invalid_personal_state'):
                    cloud.validate_personal(self.root / 'unsafe-personal.json', module)

    def test_personal_absence_has_no_store_and_optional_event_values_are_never_null(self):
        module, seed = self.personal_seed()
        value = copy.deepcopy(seed)
        value['posts'][0]['events'] = [{'shift': '昼', 'kind': 'absence', 'excerpt': 'お休みします'}]
        path = self.root / 'absence-seed.json'
        collector.atomic_json(path, value)
        cloud.validate_personal(path, module, private=False)
        for field, invalid in (('storeId', 's1'), ('storeId', None), ('time', None)):
            with self.subTest(field=field, invalid=invalid):
                candidate = copy.deepcopy(value)
                candidate['posts'][0]['events'][0][field] = invalid
                collector.atomic_json(path, candidate)
                with self.assertRaises((cloud.CloudError, ValueError)):
                    cloud.validate_personal(path, module, private=False)

    def test_personal_child_cli_uses_only_shared_sidecar_and_no_credentials_or_publication(self):
        module = cloud.load_personal_collector()
        completed = subprocess.CompletedProcess([], 3, TOKEN.encode(), TOKEN.encode())
        with mock.patch.object(cloud, 'child_process', return_value=completed) as child, \
                contextlib.redirect_stdout(io.StringIO()) as logged:
            self.assertEqual(cloud.invoke_personal_collector(
                self.root, self.root / 'collected', self.root / 'report.json', self.environment), 3)
        argv = child.call_args.args[0]
        args = module.argument_parser().parse_args(argv[4:])
        self.assertEqual(args.http_state.name, cloud.HTTP_STATE)
        self.assertEqual(args.snapshot.name, cloud.PERSONAL)
        self.assertEqual((args.max_searches, args.max_posts), (3, 3))
        self.assertIsNone(args.publish)
        self.assertNotIn('GH_TOKEN', child.call_args.kwargs['environment'])
        self.assertNotIn('GITHUB_TOKEN', child.call_args.kwargs['environment'])
        self.assertEqual(logged.getvalue(), '')

    def test_azure_credentials_only_reach_explicit_personal_backend(self):
        environment = {**self.environment, 'PERSONAL_ANALYSIS_BACKEND': 'azure',
                       'AZURE_OPENAI_API_KEY': 'OFFLINE_AZURE_SENTINEL',
                       'AZURE_OPENAI_ENDPOINT': 'https://offline.openai.azure.com/',
                       'AZURE_OPENAI_DEPLOYMENT': 'gpt-5.6-luna',
                       'AZURE_OPENAI_UNEXPECTED': 'not-forwarded'}
        completed = subprocess.CompletedProcess([], 0, b'', b'')
        with mock.patch.object(cloud, 'child_process', return_value=completed) as child:
            cloud.invoke_personal_collector(self.root, self.root, self.root / 'report.json', environment)
            argv = child.call_args.args[0]
            self.assertEqual(argv[argv.index('--analysis-backend') + 1], 'azure')
            self.assertEqual(child.call_args.kwargs['environment']['AZURE_OPENAI_API_KEY'],
                             'OFFLINE_AZURE_SENTINEL')
            self.assertEqual(child.call_args.kwargs['environment']['AZURE_OPENAI_DEPLOYMENT'],
                             'gpt-5.6-luna')
            self.assertNotIn('GH_TOKEN', child.call_args.kwargs['environment'])
            self.assertNotIn('AZURE_OPENAI_UNEXPECTED', child.call_args.kwargs['environment'])
            cloud.invoke_collector(self.root, self.root, self.root / 'report.json', environment)
            self.assertFalse(any(key.startswith('AZURE_OPENAI_')
                                 for key in child.call_args.kwargs['environment']))
        for credentials in (False, True):
            self.assertNotIn('AZURE_OPENAI_API_KEY',
                             cloud.safe_environment(environment, credentials=credentials))

    def test_private_azure_state_survives_restore_and_never_enters_public_projection(self):
        module, seed = self.personal_seed()
        state = module.empty_state()
        module.merge_seed(state, seed)
        ai = module.azure.empty_state()
        ai['budgets'] = {'2026-09-06': 3}
        ai['nextRequestAt'] = '2026-09-06T05:00:00Z'
        ai['paused'] = {'reason': 'azure_auth_stopped', 'httpStatus': 401,
                        'at': '2026-09-06T04:00:00Z'}
        ai['review'] = {seed['posts'][0]['id']: 'azure_saved_body_required'}
        ai['history'] = [copy.deepcopy(seed['posts'][0])]
        ai['cache']['a' * 64] = {
            'postId': seed['posts'][0]['id'], 'bodyHash': 'b' * 64, 'versionHash': 'c' * 64,
            'at': '2026-09-06T04:00:00Z', 'reason': 'azure_refused', 'events': []}
        state['azureAnalysis'] = ai
        self.seed_branch()
        self.bare_commit({cloud.PERSONAL: state})
        before = self.remote_json(cloud.PERSONAL)[1]
        self.args.mode = 'restore'
        with mock.patch.object(cloud, 'invoke_personal_collector') as personal_run, \
                mock.patch.object(cloud, 'invoke_collector') as official_run:
            self.run_cloud()
            personal_run.assert_not_called()
            official_run.assert_not_called()
        path = self.output.parent / cloud.PERSONAL
        self.assertEqual(path.read_bytes(), before)
        restored, _ = cloud.validate_personal(path, module)
        self.assertEqual(restored['azureAnalysis'], ai)
        spec = importlib.util.spec_from_file_location('azure_pages_test', ROOT / 'tools' / 'pages.py')
        pages = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pages)
        public = pages.load_public_personal_snapshot(path)
        self.assertEqual(public['posts'], pages.personal_projection(seed)['posts'])
        for field in ('azureAnalysis', 'bodyHash', 'versionHash', 'azure_auth_stopped', 'azure_refused'):
            self.assertNotIn(field, json.dumps(public))
        for field in ('apiKey', 'endpoint', 'text'):
            bad = copy.deepcopy(restored)
            bad['azureAnalysis'][field] = 'must-not-persist'
            collector.atomic_json(self.root / 'bad-azure.json', bad)
            with self.assertRaises((ValueError, cloud.CloudError)):
                cloud.validate_personal(self.root / 'bad-azure.json', module)

    def test_personal_entrypoint_respects_the_official_canonical_lock_before_any_work(self):
        module, seed = self.personal_seed()
        workspace = self.base / 'shared-lock'
        workspace.mkdir()
        private = module.empty_state()
        module.merge_seed(private, seed)
        collector.atomic_json(workspace / cloud.PERSONAL, private)
        collector.atomic_json(workspace / cloud.HTTP_STATE, {'schemaVersion': 1, 'cooldowns': {}})
        before = (workspace / cloud.PERSONAL).read_bytes()
        args = module.argument_parser().parse_args([
            '--snapshot', str(workspace / cloud.PERSONAL),
            '--http-state', str(workspace / cloud.HTTP_STATE),
            '--seed', str(self.output.parent / cloud.PERSONAL)])
        with collector.ProcessLock((workspace / cloud.SNAPSHOT).with_suffix('.lock')), \
                mock.patch.object(module, 'read_js', side_effect=AssertionError('Must stop at lock')), \
                self.assertRaisesRegex(ValueError, 'collector_locked'):
            module.run(args, client_factory=lambda _: self.fail('No HTTP client before shared lock'))
        self.assertEqual((workspace / cloud.PERSONAL).read_bytes(), before)

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
