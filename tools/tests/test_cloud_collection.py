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


def saved_personal_fixture(module):
    """Synthetic source attestations, not new observations or model acceptances."""
    saved = cloud.load_personal_saved()
    state = module.empty_state()
    fetched = '2026-09-06T04:00:00Z'
    state.update(checkedAt=fetched, lastSuccessAt=fetched,
                 lastRun={'status': 'partial', 'date': '2026-09-06'})
    state['budgets'] = {'2026-09-06': {'searches': 11, 'posts': 5}}
    state['lastRequests'] = {module.POST_HOST: fetched}
    ledger = cloud.load_analysis_state()
    usage = ledger.empty_state()
    ledger.apply_import(usage, {
        'receiptId': 'a' * 64, 'sourceHash': 'b' * 64, 'date': '2026-09-07',
        'counts': {'requests': 3},
        'modelBreakdown': [{
            'model': 'gpt-5.6-luna', 'deployment': 'gpt-5.6-luna',
            'modelVersion': '2026-07-09', 'kind': 'text', 'component': 'personal', 'count': 3,
        }],
    })
    amendments = []
    for index, (name, handle, author) in enumerate((
            ('あむ', 'amu_zettai', '1180156105181159424'),
            ('ららこ', 'rarako_zettai', '2065375500131028992'),
            ('あめる', 'ameru_zettai', '1822822097141575680'))):
        created = f'2026-09-05T15:0{index + 1}:00Z'
        tid = make_id(created)
        metadata = {
            'name': name, 'authorId': author, 'authorScreenName': handle,
            'date': '2026-09-06', 'url': module.public_url(handle, tid),
        }
        source = {
            **metadata, 'createdAt': created, 'fetchedAt': fetched,
            'analyzedAt': f'2026-09-06T16:0{index}:00Z',
            'provenance': 'search' if index == 0 else 'direct',
            'bodyHash': saved.data_hash(['body', tid]),
            'sourceHash': saved.data_hash(['source', tid]),
            'sourceManifestHash': 'c' * 64, 'analysisResultHash': 'd' * 64,
            'analysisReceiptHash': saved.data_hash(['analysis', tid]), 'contractHash': 'e' * 64,
            'contractVersion': saved.LEGACY_CONTRACT, 'model': saved.MODEL,
            'deployment': saved.MODEL, 'modelVersion': saved.MODEL_VERSION,
            'usageReceiptId': 'a' * 64, 'usageSourceHash': 'b' * 64,
        }
        events = [{'shift': '昼', 'kind': 'placement', 'storeId': 's1', 'excerpt': '1号店昼'}]
        if index == 0:
            source['searchCreatedAt'] = created
            state['pending'].append({
                'id': tid, **metadata, 'searchCreatedAt': created,
                'reason': 'network_error', 'firstSeenAt': fetched,
                'lastAttemptAt': fetched, 'attempts': 1,
            })
        else:
            state['identityBindings'][name] = {
                'authorId': author, 'authorScreenName': handle, 'verifiedAt': fetched,
            }
            if index == 1:
                state['resolved'].append({
                    'id': tid, **{key: metadata[key] for key in ('name', 'url', 'date')},
                    'reason': 'no_event', 'resolvedAt': fetched,
                })
            else:
                state['posts'].append({
                    'id': tid, **metadata, 'createdAt': created,
                    'observedAt': fetched, 'events': copy.deepcopy(events),
                })
        amendments.append({
            'schemaVersion': 1, 'id': tid, 'source': source, 'events': events,
            'links': [{'scope': '昼', 'status': 'work'}],
        })
    entries = [{'expectedSubjectHash': saved.subject_hash(state, item['id']), 'amendment': item}
               for item in amendments]
    return state, usage, entries


def saved_personal_pending_pair(module, second_identity):
    state, usage, entries = saved_personal_fixture(module)
    entries = entries[:2]
    second = entries[1]['amendment']
    source = second['source']
    del state['identityBindings'][source['name']]
    state['resolved'] = []
    source.update(second_identity)
    source.update(provenance='search', searchCreatedAt=source['createdAt'],
                  url=module.public_url(source['authorScreenName'], second['id']))
    state['pending'].append({
        'id': second['id'], **{field: source[field] for field in (
            'url', 'name', 'authorId', 'authorScreenName', 'date', 'searchCreatedAt')},
        'reason': 'network_error', 'firstSeenAt': source['fetchedAt'],
        'lastAttemptAt': source['fetchedAt'], 'attempts': 1,
    })
    saved = cloud.load_personal_saved()
    for entry in entries:
        entry['expectedSubjectHash'] = saved.subject_hash(state, entry['amendment']['id'])
    return state, usage, entries


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

    def saved_mode(self, manifest):
        self.personal_mode('apply-saved')
        text = json.dumps(manifest, ensure_ascii=False)
        self.environment['APPLY_SAVED_MANIFEST'] = text
        event = json.loads(self.event_path.read_bytes())
        event['inputs']['saved_manifest'] = text
        self.event_path.write_text(json.dumps(event), encoding='utf-8')

    def saved_manifest(self):
        return {
            'schemaVersion': 1, 'expectedMainSHA': self.source,
            'expectedStateSHA': self.git(self.remote, 'rev-parse', cloud.REF).decode().strip(),
            'officialAmendments': [], 'sourceReceipts': [],
            'usageImports': [{
                'receiptId': 'a' * 64, 'date': '2026-09-07', 'counts': {'requests': 4},
                'modelBreakdown': [{
                    'model': 'gpt-5.6-luna', 'kind': 'image', 'count': 1,
                    'deployment': 'gpt-5.6-luna-compare', 'modelVersion': '2026-07-09',
                    'component': 'external',
                }, {
                    'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 3,
                    'deployment': 'gpt-5.6-luna', 'modelVersion': '2026-07-09',
                    'component': 'personal',
                }],
                'sourceHash': 'b' * 64,
            }],
        }

    def empty_analysis_buffer(self, state, environment):
        if environment.get('CLOUD_COLLECTION_BUFFER_MODE') == 'write':
            snapshot, _ = cloud.validate_snapshot(state / cloud.SNAPSHOT, collector)
            value = collector.make_analysis_buffer(
                snapshot, environment['CLOUD_COLLECTION_RUN_ID'], 'f' * 64, [], collector.utc_now())
            collector.atomic_json(state.parent / cloud.ANALYSIS_BUFFER, value)

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
            self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL, cloud.AI_USAGE})
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
        message = self.git(self.remote, 'log', '-1', '--format=%B', cloud.REF).decode()
        self.assertIn('Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>', message)
        author = self.git(self.remote, 'log', '-1', '--format=%an <%ae>', cloud.REF).decode().strip()
        self.assertEqual(author, 'github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>')
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
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL, cloud.AI_USAGE})
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
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL, cloud.AI_USAGE})

    def test_collector_local_failure_preserves_http_only_sidecar_and_blocks_retry(self):
        def fail_local(root, state, report, environment):
            collector.atomic_json(state / cloud.HTTP_STATE, {
                'schemaVersion': 1, 'cooldowns': {'search.yahoo.co.jp': '2026-09-06T20:00:00Z'}})
            return 4

        with mock.patch.object(cloud, 'invoke_collector', side_effect=fail_local):
            with self.assertRaisesRegex(cloud.CloudError, 'collector_local_failure'):
                cloud.orchestrate(self.args, root=self.root,
                                  environment=self.environment, collector=collector)
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL, cloud.AI_USAGE})
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
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.PERSONAL, cloud.AI_USAGE})
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
            self.assertEqual(self.remote_names(), cloud.FILES - {cloud.AI_USAGE})
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
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.LEASE, cloud.AI_USAGE})
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
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.AI_USAGE})
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
        self.assertEqual(self.remote_names(), cloud.FILES - {cloud.AI_USAGE})
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
        self.assertEqual(args.observations, self.root / 'collected' / cloud.SNAPSHOT)
        self.assertEqual((args.max_searches, args.max_posts), (3, 3))
        self.assertIsNone(args.publish)
        self.assertNotIn('GH_TOKEN', child.call_args.kwargs['environment'])
        self.assertNotIn('GITHUB_TOKEN', child.call_args.kwargs['environment'])
        self.assertEqual(logged.getvalue(), '')

    def test_both_personal_child_observes_same_run_validated_official_facts(self):
        module, _ = self.personal_seed()
        self.seed_branch()
        self.personal_mode('both')
        observations = []

        def personal_child(root, state, report, environment):
            current, _ = cloud.validate_snapshot(state / cloud.SNAPSHOT, collector)
            observations.extend(current['posts'])
            self.assertEqual(current['posts'], [fact()])
            self.assertEqual(json.loads(self.output.read_bytes())['posts'], [])
            private = module.read_state(state / cloud.PERSONAL)
            private['lastRun'] = {'status': 'no-new'}
            collector.atomic_json(state / cloud.PERSONAL, private)
            collector.atomic_json(report, {'component': 'personal', 'status': 'no-new', 'exitCode': 0})
            return 0

        with mock.patch.object(cloud, 'invoke_personal_collector', side_effect=personal_child):
            self.run_cloud()
        self.assertEqual(observations, [fact()])

    def test_personal_link_only_cloud_contract_and_private_field_boundaries(self):
        module, seed = self.personal_seed()
        private = module.empty_state()
        module.merge_seed(private, seed)
        private['posts'][0].update(events=[], links=[{'scope': 'unspecified', 'status': 'work'}])
        private.update(coverage={}, searchHistory={}, savedPersonalImports={})
        path = self.root / 'links.json'
        collector.atomic_json(path, private)
        cloud.validate_personal(path, module)
        public = module.public_state(private)
        collector.atomic_json(path, public)
        cloud.validate_personal(path, module, private=False)
        self.assertEqual(public['posts'][0]['events'], [])
        self.assertEqual(public['posts'][0]['links'], [{'scope': 'unspecified', 'status': 'work'}])
        for field in ('coverage', 'searchHistory', 'savedPersonalImports'):
            rejected = copy.deepcopy(public)
            rejected[field] = {}
            collector.atomic_json(path, rejected)
            with self.assertRaises((cloud.CloudError, ValueError)):
                cloud.validate_personal(path, module, private=False)
        for links in (
                None, {}, [], [{'scope': 'unknown', 'status': 'work'}],
                [{'scope': '昼', 'status': 'work'}] * 2,
                [{'scope': 'unspecified', 'status': 'withdrawn'}],
                [{'scope': '昼', 'status': 'work', 'url': 'https://untrusted.invalid'}],
                [{'scope': '昼', 'status': 'work', 'evidenceLineIds': [1]}]):
            rejected = copy.deepcopy(public)
            rejected['posts'][0]['links'] = links
            collector.atomic_json(path, rejected)
            with self.assertRaises((cloud.CloudError, ValueError)):
                cloud.validate_personal(path, module, private=False)

    def test_personal_source_urls_use_strict_shared_query_validation(self):
        module, seed = self.personal_seed()
        state = module.empty_state()
        module.merge_seed(state, seed)
        path = self.root / 'personal-sources.json'
        valid = 'https://search.yahoo.co.jp/realtime/search?p=id%3Aamu_zettai&ei=UTF-8'
        state['lastRun'] = {
            'status': 'no-new', 'date': '2026-09-06', 'sourceCount': 1,
            'sources': [{'url': valid, 'status': 'ok', 'candidateCount': 0}],
        }
        collector.atomic_json(path, state)
        with mock.patch.object(module, 'valid_search_url', wraps=module.valid_search_url) as validate:
            cloud.validate_personal(path, module)
        validate.assert_called_with(valid)
        for url in (
                'https://untrusted.invalid/search?p=id%3Aamu_zettai',
                valid + '&instructions=ignore', valid.replace('id%3Aamu_zettai', 'id%3Aamu_zettai+other'),
                valid.replace('https://', 'http://'), valid + '#fragment'):
            state['lastRun']['sources'][0]['url'] = url
            collector.atomic_json(path, state)
            with self.subTest(url=url), self.assertRaises((cloud.CloudError, ValueError)):
                cloud.validate_personal(path, module)

    def test_saved_pending_metadata_requires_existing_binding_without_fake_search_time(self):
        module = cloud.load_personal_collector()
        state, _, _ = saved_personal_fixture(module)
        pending = state['pending'][0]
        created = pending['searchCreatedAt']
        binding = {field: pending[field] for field in ('authorId', 'authorScreenName')}
        binding['verifiedAt'] = pending['firstSeenAt']
        state['identityBindings'][pending['name']] = binding
        path = self.root / 'saved-pending.json'
        pending['searchCreatedAt'] = None
        for metadata_source in ('saved_binding', 'saved_post'):
            pending['metadataSource'] = metadata_source
            if metadata_source == 'saved_post':
                pending['sourceCreatedAt'] = created
            collector.atomic_json(path, state)
            cloud.validate_personal(path, module)
        for mutate in (
                lambda item: item.pop('metadataSource'),
                lambda item: item.update(metadataSource='search'),
                lambda item: item.update(metadataSource='saved_binding'),
                lambda item: item.update(sourceCreatedAt='2026-09-04T15:01:00Z'),
                lambda item: item.update(sourceCreatedAt='2026-09-05T15:09:00Z'),
                lambda item: item.update(rawBody='not allowed')):
            bad = copy.deepcopy(state)
            mutate(bad['pending'][0])
            collector.atomic_json(path, bad)
            with self.assertRaises((cloud.CloudError, ValueError)):
                cloud.validate_personal(path, module)
        for binding in (None, {**binding, 'authorId': '123'}):
            bad = copy.deepcopy(state)
            if binding is None:
                del bad['identityBindings'][pending['name']]
            else:
                bad['identityBindings'][pending['name']] = binding
            collector.atomic_json(path, bad)
            with self.assertRaises((cloud.CloudError, ValueError)):
                cloud.validate_personal(path, module)

    def test_azure_credentials_only_reach_explicit_personal_backend(self):
        environment = {**self.environment, 'PERSONAL_ANALYSIS_BACKEND': 'azure',
                       'CLOUD_COLLECTION_SHARED': 'true', 'CLOUD_COLLECTION_RUN_ID': '12345-1',
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

    def test_daily_guidance_requires_exact_schedule_activation_and_imported_ledger(self):
        self.personal_seed()
        self.args.mode = 'daily-guidance'
        self.environment['GITHUB_EVENT_NAME'] = 'schedule'
        event = json.loads(self.event_path.read_bytes())
        event['schedule'] = cloud.DAILY_SCHEDULE
        self.event_path.write_text(json.dumps(event), encoding='utf-8')
        self.assert_read_only_failure('daily_guidance_not_enabled')
        self.environment['DAILY_GUIDANCE_ENABLED'] = 'true'
        event['schedule'] += ' '
        self.event_path.write_text(json.dumps(event), encoding='utf-8')
        self.assert_read_only_failure('unknown_collection_schedule')
        event['schedule'] = cloud.DAILY_SCHEDULE
        self.event_path.write_text(json.dumps(event), encoding='utf-8')
        self.assert_read_only_failure('missing_ai_usage')
        self.args.mode = 'restore'
        self.assert_read_only_failure('missing_ai_usage')

    def test_shared_ledger_restore_is_byte_exact_and_never_seeded_from_checkout(self):
        usage = cloud.load_analysis_state()
        self.seed_branch()
        state = usage.empty_state()
        usage.apply_import(state, {
            'receiptId': 'a' * 64, 'date': '2026-09-06', 'counts': {'requests': 24},
            'modelBreakdown': [{'model': 'nano', 'kind': 'text', 'count': 18},
                               {'model': 'mini', 'kind': 'text', 'count': 6}],
            'sourceHash': 'b' * 64,
        })
        usage.apply_import(state, {
            'receiptId': 'c' * 64, 'date': '2026-09-07', 'counts': {'requests': 4},
            'modelBreakdown': [{'model': 'gpt-5.6-luna', 'kind': 'image', 'count': 1},
                               {'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 3}],
            'sourceHash': 'd' * 64,
        })
        self.bare_commit({cloud.AI_USAGE: state})
        self.args.mode = 'restore'
        self.environment['DAILY_GUIDANCE_ENABLED'] = 'true'
        self.run_cloud()
        restored, raw = cloud.validate_ai_usage(self.output.parent / cloud.AI_USAGE)
        self.assertEqual(raw, self.remote_json(cloud.AI_USAGE)[1])
        self.assertEqual(usage.remaining(restored, 'new-run',
                                       dt.datetime(2026, 9, 7, 3, tzinfo=dt.timezone.utc)), 3)
        self.assertEqual(usage.usage_counts(restored, 'new-run',
                                          dt.datetime(2026, 9, 7, 3, tzinfo=dt.timezone.utc))['day'], 4)
        self.environment['DAILY_GUIDANCE_ENABLED'] = 'false'
        self.bare_commit({cloud.AI_USAGE: None})
        self.run_cloud()
        self.assertFalse((self.output.parent / cloud.AI_USAGE).exists())

    def test_shared_child_arguments_zero_source_remainder_and_scheduled_guard(self):
        environment = {**self.environment, 'PERSONAL_ANALYSIS_BACKEND': 'azure',
                       'CLOUD_COLLECTION_SHARED': 'true', 'CLOUD_COLLECTION_RUN_ID': '12345-1',
                       'CLOUD_COLLECTION_ANALYSIS_LIMIT': '2', 'CLOUD_COLLECTION_SCHEDULED': 'true',
                       'CLOUD_COLLECTION_PERSONAL_POSTS': '0',
                       'CLOUD_COLLECTION_OFFICIAL_AZURE': 'true',
                       'AZURE_OPENAI_API_KEY': 'OFFLINE_AZURE_SENTINEL',
                       'APPLY_SAVED_MANIFEST': '{"must":"never reach children"}'}
        completed = subprocess.CompletedProcess([], 0, b'', b'')
        with mock.patch.object(cloud, 'child_process', return_value=completed) as child:
            cloud.invoke_personal_collector(self.root, self.root, self.root / 'report.json', environment)
            args = cloud.load_personal_collector().argument_parser().parse_args(child.call_args.args[0][4:])
            self.assertEqual(args.ai_state, self.root / cloud.AI_USAGE)
            self.assertEqual((args.analysis_run_id, args.analysis_limit, args.max_posts),
                             ('12345-1', 2, 0))
            self.assertTrue(args.scheduled)
            self.assertNotIn('APPLY_SAVED_MANIFEST', child.call_args.kwargs['environment'])
            cloud.invoke_collector(self.root, self.root, self.root / 'report.json', environment)
            args = collector.argument_parser().parse_args(child.call_args.args[0][4:])
            self.assertEqual(args.analysis_backend, 'azure')
            self.assertEqual(args.analysis_limit, 2)
            self.assertEqual(child.call_args.kwargs['environment']['AZURE_OPENAI_API_KEY'],
                             'OFFLINE_AZURE_SENTINEL')
            environment['CLOUD_COLLECTION_ANALYSIS_LIMIT'] = '0'
            cloud.invoke_collector(self.root, self.root, self.root / 'report.json', environment)
            args = collector.argument_parser().parse_args(child.call_args.args[0][4:])
            self.assertEqual(args.analysis_limit, 0)
            with self.assertRaisesRegex(cloud.CloudError, 'invalid_analysis_allocation'):
                cloud.invoke_personal_collector(self.root, self.root, self.root / 'report.json', environment)
        for hour, minute, scheduled, expected in (
                (18, 0, True, True), (18, 1, True, False), (18, 30, True, False),
                (19, 30, True, False), (20, 30, True, False), (19, 30, False, True)):
            with self.subTest(hour=hour, minute=minute, scheduled=scheduled):
                self.assertEqual(cloud.personal_window_open(
                    dt.datetime(2026, 9, 7, hour, minute, tzinfo=cloud.JST), scheduled=scheduled), expected)

    def test_stale_official_status_without_completion_attestation_retains_lease(self):
        state = collector.empty_snapshot()
        state['lastRun'] = {'status': 'ok', 'dateFrom': '2026-09-05', 'dateTo': '2026-09-06'}
        self.seed_branch(state)
        with mock.patch.object(cloud, 'invoke_collector', return_value=0), \
                self.assertRaisesRegex(cloud.CloudError, 'official_report_missing'):
            cloud.orchestrate(self.args, root=self.root, environment=self.environment, collector=collector)
        self.assertIn(cloud.LEASE, self.remote_names())

    def test_saved_import_is_explicit_data_only_idempotent_and_uses_normal_lease(self):
        module, seed = self.personal_seed()
        private = module.empty_state()
        module.merge_seed(private, seed)
        self.seed_branch()
        self.bare_commit({cloud.PERSONAL: private})
        before = self.remote_json(cloud.PERSONAL)[0]
        manifest = self.saved_manifest()
        manifest['sourceReceipts'] = [{
            'receiptId': 'c' * 64, 'date': '2026-09-06', 'searches': 8, 'posts': 6,
            'sourceHash': 'd' * 64,
        }]
        self.saved_mode(manifest)
        with mock.patch.object(cloud, 'invoke_collector', side_effect=AssertionError('No source')), \
                mock.patch.object(cloud, 'invoke_personal_collector', side_effect=AssertionError('No AI')):
            result = cloud.orchestrate(
                self.args, root=self.root, environment=self.environment, collector=collector, personal=module)
        self.assertEqual(result['collectionStatus'], 'applied-saved')
        self.assertNotIn(cloud.LEASE, self.remote_names())
        before['budgets']['2026-09-06']['searches'] += 8
        before['budgets']['2026-09-06']['posts'] += 6
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0], before)
        state, raw = self.remote_json(cloud.AI_USAGE)
        self.assertEqual(state['imports']['a' * 64], manifest['usageImports'][0])
        self.assertEqual(state['sourceImports']['c' * 64]['receipt'], manifest['sourceReceipts'][0])
        self.assertEqual((self.root / 'recovery' / cloud.AI_USAGE).read_bytes(), raw)
        manifest['expectedStateSHA'] = result['stateCommit']
        self.saved_mode(manifest)
        cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                          collector=collector, personal=module)
        self.assertEqual(self.remote_json(cloud.AI_USAGE)[0], state)
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0], before)
        rolled_back = copy.deepcopy(before)
        rolled_back['budgets']['2026-09-06']['searches'] -= 1
        self.bare_commit({cloud.PERSONAL: rolled_back})
        self.args.mode = 'restore'
        self.assert_read_only_failure('source_usage_budget_mismatch')

    def test_saved_stale_or_raw_manifest_refuses_before_lease_or_collector(self):
        self.seed_branch()
        manifest = self.saved_manifest()
        for field, value, reason in (
                ('expectedMainSHA', 'f' * 40, 'saved_manifest_stale'),
                ('expectedStateSHA', 'e' * 40, 'saved_manifest_stale'),
                ('rawBody', 'must not be accepted', 'unsafe_state'),
                ('officialAmendments', [None] * 4, 'unsafe_state')):
            with self.subTest(field=field):
                bad = copy.deepcopy(manifest)
                bad[field] = value
                self.saved_mode(bad)
                self.assert_read_only_failure(reason)
        self.saved_mode(manifest)
        self.environment['APPLY_SAVED_MANIFEST'] = self.environment['APPLY_SAVED_MANIFEST'].replace(
            '"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1')
        self.assert_read_only_failure('unsafe_state')

    def test_synthetic_saved_notice_preserves_roster_and_distinct_observation_times(self):
        module, seed = self.personal_seed()
        private = module.empty_state()
        module.merge_seed(private, seed)
        snapshot = collector.empty_snapshot()
        snapshot['posts'] = [fact()]
        snapshot['officialAnalysis'] = collector.analysis_module().empty_state()
        snapshot['officialAnalysis']['cache']['f' * 64] = {
            'postId': TID, 'bodyHash': 'a' * 64, 'versionHash': 'c' * 64,
            'createdAt': snapshot['posts'][0]['createdAt'], 'fetchedAt': '2026-09-06T04:00:00Z',
            'at': '2026-09-06T04:00:00Z', 'reason': 'no_event', 'notices': [],
        }
        self.seed_branch(snapshot)
        self.bare_commit({cloud.PERSONAL: private})
        manifest = self.saved_manifest()
        source = {field: snapshot['posts'][0][field] for field in (
            'url', 'authorId', 'authorScreenName', 'createdAt')}
        source.update(fetchedAt='2026-09-06T05:54:14.290Z', bodyHash='e' * 64,
                      analyzedAt='2026-09-07T03:00:00Z', analysisReceiptHash='d' * 64)
        amendment = {
            'schemaVersion': 1, 'id': TID, 'source': source,
            'notices': [{'name': 'るるか', 'kind': 'late', 'excerpt': 'るるかちゃんもあとから',
                         'observedAt': source['fetchedAt']}],
        }
        manifest['officialAmendments'] = [{
            'expectedPostHash': cloud.data_hash(snapshot['posts'][0]), 'amendment': amendment,
        }]
        for change, reason in (
                (lambda item: item.update(expectedPostHash='f' * 64), 'saved_post_hash_mismatch'),
                (lambda item: item['amendment'].update(rawBody='not allowed'), 'saved_manifest_rejected'),
                (lambda item: item['amendment']['source'].update(
                    url='https://untrusted.invalid/input'), 'saved_manifest_rejected')):
            rejected = copy.deepcopy(manifest)
            change(rejected['officialAmendments'][0])
            self.saved_mode(rejected)
            self.assert_read_only_failure(reason)
        self.saved_mode(manifest)
        with mock.patch.object(cloud, 'invoke_collector', side_effect=AssertionError('No source or AI')), \
                mock.patch.object(cloud, 'invoke_personal_collector', side_effect=AssertionError('No source or AI')):
            result = cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                                       collector=collector, personal=module)
        saved = self.remote_json(cloud.SNAPSHOT)[0]
        self.assertEqual(saved['posts'][0]['names'], snapshot['posts'][0]['names'])
        self.assertEqual(saved['posts'][0]['observedAt'], snapshot['posts'][0]['observedAt'])
        self.assertEqual(saved['posts'][0]['notices'], amendment['notices'])
        self.assertEqual(saved['officialAnalysis']['cache'], snapshot['officialAnalysis']['cache'])
        self.assertEqual(len(saved['officialAnalysis']['history']), 1)
        receipt = saved['officialAnalysis']['receipts'][cloud.data_hash(amendment)]
        self.assertEqual(receipt['analyzedAt'], source['analyzedAt'])
        self.assertEqual(receipt['at'], source['fetchedAt'])
        pages_spec = importlib.util.spec_from_file_location('saved_pages', ROOT / 'tools' / 'pages.py')
        pages = importlib.util.module_from_spec(pages_spec)
        pages_spec.loader.exec_module(pages)
        public = pages.load_public_snapshot(self.output)
        self.assertEqual(public['posts'][0]['notices'], amendment['notices'])
        self.assertNotIn('officialAnalysis', public)
        manifest['expectedStateSHA'] = result['stateCommit']
        self.saved_mode(manifest)
        cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                          collector=collector, personal=module)
        self.assertEqual(self.remote_json(cloud.SNAPSHOT)[0], saved)

    def test_saved_personal_delta_is_leased_accounted_once_and_publicly_minimized(self):
        module = cloud.load_personal_collector()
        private, usage, entries = saved_personal_fixture(module)
        snapshot = collector.empty_snapshot()
        snapshot['posts'] = [fact()]
        self.seed_branch(snapshot)
        self.bare_commit({cloud.PERSONAL: private, cloud.AI_USAGE: usage})
        manifest = self.saved_manifest()
        manifest.update(usageImports=[], personalAmendments=entries)
        self.saved_mode(manifest)
        with mock.patch.object(cloud, 'invoke_collector', side_effect=AssertionError('No source')), \
                mock.patch.object(cloud, 'invoke_personal_collector', side_effect=AssertionError('No AI')):
            result = cloud.orchestrate(
                self.args, root=self.root, environment=self.environment, collector=collector, personal=module)
        self.assertEqual(result['collectionStatus'], 'applied-saved')
        self.assertNotIn(cloud.LEASE, self.remote_names())
        after = self.remote_json(cloud.PERSONAL)[0]
        self.assertEqual(len(after['posts']), 3)
        self.assertEqual(len(after['savedPersonalImports']), 3)
        self.assertEqual(after['pending'], [])
        self.assertEqual(after['resolved'], [])
        self.assertEqual(after['budgets'], private['budgets'])
        self.assertEqual(self.remote_json(cloud.AI_USAGE)[0], usage)
        self.assertEqual(self.remote_json(cloud.SNAPSHOT)[0], snapshot)
        pages_spec = importlib.util.spec_from_file_location('personal_saved_pages', ROOT / 'tools' / 'pages.py')
        pages = importlib.util.module_from_spec(pages_spec)
        pages_spec.loader.exec_module(pages)
        public = pages.load_public_personal_snapshot(self.output.parent / cloud.PERSONAL)
        self.assertEqual(public['posts'], after['posts'])
        for field in ('savedPersonalImports', 'coverage', 'sourceHash', 'analysisReceiptHash',
                      'usageReceiptId', 'bodyHash', 'contractVersion', 'identityBindings'):
            self.assertNotIn(field, json.dumps(public))
        manifest['expectedStateSHA'] = result['stateCommit']
        self.saved_mode(manifest)
        cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                          collector=collector, personal=module)
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0], after)
        self.assertEqual(self.remote_json(cloud.AI_USAGE)[0], usage)

    def test_saved_personal_rejections_happen_before_lease(self):
        module = cloud.load_personal_collector()
        private, usage, entries = saved_personal_fixture(module)
        self.seed_branch()
        self.bare_commit({cloud.PERSONAL: private, cloud.AI_USAGE: usage})
        manifest = self.saved_manifest()
        manifest.update(usageImports=[], personalAmendments=entries)
        mutations = (
            lambda items: items[0].update(expectedSubjectHash='f' * 64),
            lambda items: items[0]['amendment'].update(id=THIRD),
            lambda items: items[0]['amendment'].update(rawBody='not allowed'),
            lambda items: items[0]['amendment']['source'].update(bodyLines=[]),
            lambda items: items[0]['amendment']['events'][0].update(evidenceLineIds=[1]),
            lambda items: items[0]['amendment']['source'].update(url='https://untrusted.invalid/input'),
            lambda items: items[1]['amendment']['source'].update(authorId='123'),
            lambda items: items[0]['amendment']['source'].update(usageSourceHash='f' * 64),
            lambda items: items[0]['amendment']['source'].update(analyzedAt='2026-09-06T05:00:00Z'),
        )
        for change in mutations:
            rejected = copy.deepcopy(manifest)
            change(rejected['personalAmendments'])
            self.saved_mode(rejected)
            self.assert_read_only_failure('saved_manifest_rejected')
        for field in ('expectedMainSHA', 'expectedStateSHA'):
            rejected = copy.deepcopy(manifest)
            rejected[field] = 'f' * 40
            self.saved_mode(rejected)
            self.assert_read_only_failure('saved_manifest_stale')
        rejected = copy.deepcopy(manifest)
        rejected['personalAmendments'] *= 2
        self.saved_mode(rejected)
        self.assert_read_only_failure('unsafe_state')
        bad_binding = copy.deepcopy(private)
        del bad_binding['identityBindings']['ららこ']
        self.bare_commit({cloud.PERSONAL: bad_binding})
        manifest['expectedStateSHA'] = self.git(self.remote, 'rev-parse', cloud.REF).decode().strip()
        manifest['personalAmendments'][1]['expectedSubjectHash'] = cloud.load_personal_saved().subject_hash(
            bad_binding, entries[1]['amendment']['id'])
        self.saved_mode(manifest)
        self.assert_read_only_failure('saved_manifest_rejected')

    def test_saved_personal_batch_binding_conflict_is_rejected_before_lease(self):
        module = cloud.load_personal_collector()
        private, usage, entries = saved_personal_pending_pair(module, {'name': 'あむ'})
        self.seed_branch()
        self.bare_commit({cloud.PERSONAL: private, cloud.AI_USAGE: usage})
        manifest = self.saved_manifest()
        manifest.update(usageImports=[], personalAmendments=entries)
        self.saved_mode(manifest)
        self.assert_read_only_failure('saved_manifest_rejected')
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0], private)
        self.assertEqual(self.remote_json(cloud.AI_USAGE)[0], usage)
        self.assertNotIn(cloud.LEASE, self.remote_names())

    def test_saved_personal_cas_conflict_never_overwrites_other_writer(self):
        module = cloud.load_personal_collector()
        private, usage, entries = saved_personal_fixture(module)
        self.seed_branch()
        self.bare_commit({cloud.PERSONAL: private, cloud.AI_USAGE: usage})
        manifest = self.saved_manifest()
        manifest.update(usageImports=[], personalAmendments=entries)
        self.saved_mode(manifest)
        original = cloud.child_process
        pushes = []

        def race(argv, **kwargs):
            if 'push' in argv:
                pushes.append(argv)
                if len(pushes) == 2:
                    concurrent = copy.deepcopy(private)
                    concurrent['budgets']['2026-09-06']['posts'] += 1
                    self.bare_commit({cloud.PERSONAL: concurrent})
            return original(argv, **kwargs)

        with mock.patch.object(cloud, 'child_process', side_effect=race), \
                self.assertRaisesRegex(cloud.CloudError, 'state_push_failed'):
            cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                              collector=collector, personal=module)
        remote = self.remote_json(cloud.PERSONAL)[0]
        self.assertNotIn('savedPersonalImports', remote)
        self.assertEqual(remote['budgets']['2026-09-06']['posts'], 6)
        self.assertIn(cloud.LEASE, self.remote_names())
        self.assertTrue(all('--force' not in argument for argv in pushes for argument in argv))

    def test_daily_children_share_three_ai_requests_and_official_source_remainder(self):
        module, seed = self.personal_seed()
        private = module.empty_state()
        module.merge_seed(private, seed)
        self.seed_branch()
        ledger = cloud.load_analysis_state()
        imported = ledger.empty_state()
        ledger.apply_import(imported, {
            'receiptId': 'a' * 64, 'date': '2026-09-07', 'counts': {'requests': 27},
            'modelBreakdown': [{'model': 'gpt-5.6-luna', 'kind': 'text', 'count': 27}],
            'sourceHash': 'b' * 64,
        })
        self.bare_commit({cloud.PERSONAL: private, cloud.AI_USAGE: imported})
        self.args.mode = 'daily-guidance'
        self.environment.update(GITHUB_EVENT_NAME='schedule', DAILY_GUIDANCE_ENABLED='true')
        event = json.loads(self.event_path.read_bytes())
        event['schedule'] = cloud.DAILY_SCHEDULE
        self.event_path.write_text(json.dumps(event), encoding='utf-8')
        now = [dt.datetime(2026, 9, 7, 12, 30, tzinfo=cloud.JST)]
        calls = []
        identity = {'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
                    'deployment': 'gpt-5.6-luna', 'model': 'gpt-5.6-luna', 'modelVersion': '2026-07-09'}

        def sleep(seconds):
            now[0] += dt.timedelta(seconds=seconds)

        def invoke(component, state, report, environment):
            with ledger.SharedUsage(
                    state / cloud.AI_USAGE, run_id=environment['CLOUD_COLLECTION_RUN_ID'],
                    component=component, clock=lambda: now[0], sleep=sleep,
                    request_limit=int(environment['CLOUD_COLLECTION_ANALYSIS_LIMIT'])) as usage:
                for index in range(3):
                    key = cloud.data_hash([component, index])
                    try:
                        usage.reserve(key, identity)
                    except ledger.UsageFailure:
                        break
                    usage.issued(key)
                    usage.finish(key, 'no_event')
                    calls.append(component)
            if component == 'official':
                self.assertEqual(environment['CLOUD_COLLECTION_ANALYSIS_LIMIT'], '2')
                snapshot, _ = cloud.validate_snapshot(state / cloud.SNAPSHOT, collector)
                snapshot['lastRun'] = {
                    'status': 'no-new', 'dateFrom': '2026-09-06', 'dateTo': '2026-09-07',
                    'requests': {'searches': 2, 'posts': 19},
                }
                collector.atomic_json(state / cloud.SNAPSHOT, snapshot)
                self.empty_analysis_buffer(state, environment)
                code = 0
            else:
                self.assertEqual(environment['CLOUD_COLLECTION_PERSONAL_POSTS'], '1')
                snapshot = module.read_state(state / cloud.PERSONAL)
                self.assertEqual(snapshot['posts'], private['posts'])
                snapshot['lastRun'] = {'status': 'partial', 'requests': {'searches': 3, 'posts': 1}}
                collector.atomic_json(state / cloud.PERSONAL, snapshot)
                code = 2
            collector.atomic_json(report, {'component': component, 'status': snapshot['lastRun']['status'],
                                           'exitCode': code, 'requests': snapshot['lastRun']['requests']})
            return code

        with mock.patch.object(collector, 'utc_now', side_effect=lambda: now[0]), \
                mock.patch.object(cloud, 'invoke_collector', side_effect=lambda r, s, p, e: invoke('official', s, p, e)), \
                mock.patch.object(cloud, 'invoke_personal_collector', side_effect=lambda r, s, p, e: invoke('personal', s, p, e)):
            result = cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                                       collector=collector, personal=module)
        self.assertEqual(calls, ['official', 'official', 'personal'])
        self.assertEqual(result['collectionStatus'], 'partial')
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0]['posts'], private['posts'])
        saved = self.remote_json(cloud.AI_USAGE)[0]
        self.assertEqual(ledger.remaining(saved, '12345-1', now[0]), 0)
        self.assertEqual(ledger.usage_counts(saved, '12345-1', now[0])['day'], 30)
        path = self.output.parent / cloud.AI_USAGE
        self.assertEqual(cloud.official_allocation(
            path, '12346-1', now[0] + dt.timedelta(days=1), True, True), 1)
        self.assertEqual(cloud.official_allocation(
            path, '12346-1', now[0].replace(hour=18, minute=30), True, True), 0)
        self.assertEqual(cloud.official_allocation(
            path, '12346-1', now[0].replace(hour=18, minute=30) + dt.timedelta(days=1),
            True, True), 3)

    def test_waiting_past_scheduled_cutoff_never_starts_personal_child(self):
        module, seed = self.personal_seed()
        private = module.empty_state()
        module.merge_seed(private, seed)
        self.seed_branch()
        ledger = cloud.load_analysis_state()
        state = ledger.empty_state()
        ledger.apply_import(state, self.saved_manifest()['usageImports'][0])
        self.bare_commit({cloud.PERSONAL: private, cloud.AI_USAGE: state})
        self.args.mode = 'daily-guidance'
        self.environment.update(GITHUB_EVENT_NAME='schedule', DAILY_GUIDANCE_ENABLED='true')
        event = json.loads(self.event_path.read_bytes())
        event['schedule'] = cloud.DAILY_SCHEDULE
        self.event_path.write_text(json.dumps(event), encoding='utf-8')
        now = [dt.datetime(2026, 9, 7, 17, 30, tzinfo=cloud.JST)]

        def official(root, saved, report, environment):
            now[0] = now[0].replace(hour=18, minute=30)
            snapshot, _ = cloud.validate_snapshot(saved / cloud.SNAPSHOT, collector)
            snapshot['lastRun'] = {
                'status': 'no-new', 'dateFrom': '2026-09-06', 'dateTo': '2026-09-07',
            }
            collector.atomic_json(saved / cloud.SNAPSHOT, snapshot)
            self.empty_analysis_buffer(saved, environment)
            collector.atomic_json(report, {
                'component': 'official', 'status': 'no-new', 'exitCode': 0,
                'requests': {'searches': 2, 'posts': 20},
            })
            return 0

        with mock.patch.object(collector, 'utc_now', side_effect=lambda: now[0]), \
                mock.patch.object(cloud, 'invoke_collector', side_effect=official), \
                mock.patch.object(cloud, 'invoke_personal_collector') as personal:
            result = cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                                       collector=collector, personal=module)
        personal.assert_not_called()
        self.assertEqual(result['personalCollectionStatus'], 'outside-window')
        saved = self.remote_json(cloud.PERSONAL)[0]
        self.assertEqual(saved['lastRun']['status'], 'outside-window')
        for field in ('posts', 'pending', 'budgets', 'paused', 'originalTargets', 'lastSuccessAt'):
            self.assertEqual(saved[field], private[field])
        self.assertEqual(self.remote_json(cloud.AI_USAGE)[0], state)

    def test_shared_reservation_survives_child_failure_in_exact_private_recovery(self):
        self.seed_branch()
        ledger = cloud.load_analysis_state()
        initial = ledger.empty_state()
        ledger.apply_import(initial, self.saved_manifest()['usageImports'][0])
        self.bare_commit({cloud.AI_USAGE: initial})
        now = dt.datetime(2026, 9, 7, 12, tzinfo=cloud.JST)
        reserved = []

        def interrupted(root, saved, report, environment):
            with ledger.SharedUsage(
                    saved / cloud.AI_USAGE, run_id=environment['CLOUD_COLLECTION_RUN_ID'],
                    component='official', clock=lambda: now, sleep=lambda _: None) as usage:
                usage.reserve('f' * 64, {
                    'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
                    'deployment': 'gpt-5.6-luna', 'model': 'gpt-5.6-luna', 'modelVersion': '2026-07-09',
                })
            reserved.append((saved / cloud.AI_USAGE).read_bytes())
            return 4

        with mock.patch.object(cloud, 'invoke_collector', side_effect=interrupted), \
                self.assertRaisesRegex(cloud.CloudError, 'collector_local_failure'):
            cloud.orchestrate(self.args, root=self.root, environment=self.environment, collector=collector)
        self.assertIn(cloud.LEASE, self.remote_names())
        self.assertEqual(self.remote_json(cloud.AI_USAGE)[0], initial)
        path = self.root / 'recovery' / cloud.AI_USAGE
        recovered, raw = cloud.validate_ai_usage(path)
        self.assertEqual(raw, reserved[0])
        self.assertEqual(ledger.usage_counts(recovered, '12345-1', now)['run'], 1)
        self.assert_read_only_failure('unresolved_lease')

    def test_saved_import_preserves_legacy_backoff_pause_and_requires_usage_coverage(self):
        module, seed = self.personal_seed()
        private = module.empty_state()
        module.merge_seed(private, seed)
        legacy = module.azure.empty_state()
        legacy['budgets'] = {'2026-09-07': 4}
        legacy['nextRequestAt'] = '2026-09-07T05:00:00Z'
        legacy['paused'] = {'reason': 'azure_auth_stopped', 'httpStatus': 403,
                            'at': '2026-09-07T03:00:00Z'}
        private['azureAnalysis'] = legacy
        self.seed_branch()
        manifest = self.saved_manifest()
        now = dt.datetime(2026, 9, 7, 3, tzinfo=dt.timezone.utc)
        with mock.patch.object(collector, 'utc_now', return_value=now):
            _, personal_result, usage = cloud.prepare_saved(
                manifest, collector.empty_snapshot(), private, None, collector, module)
        self.assertEqual(personal_result, private)
        self.assertEqual(usage['paused'], legacy['paused'])
        self.assertEqual(usage['nextRequestAt'], legacy['nextRequestAt'])
        self.assertEqual(usage['retryAt'], legacy['nextRequestAt'])
        manifest['usageImports'][0]['counts']['requests'] = 3
        manifest['usageImports'][0]['modelBreakdown'][1]['count'] = 2
        with self.assertRaisesRegex(cloud.CloudError, 'incomplete_legacy_usage_import'):
            cloud.prepare_saved(manifest, collector.empty_snapshot(), private, None, collector, module)

    def test_manual_azure_requires_reconciled_shared_ledger_even_with_activation_off(self):
        module, seed = self.personal_seed()
        private = module.empty_state()
        module.merge_seed(private, seed)
        self.seed_branch()
        self.bare_commit({cloud.PERSONAL: private})
        self.environment.update(PERSONAL_ANALYSIS_BACKEND='azure', DAILY_GUIDANCE_ENABLED='false')
        for mode in ('personal', 'both'):
            self.personal_mode(mode)
            self.assert_read_only_failure('missing_ai_usage')
        ledger = cloud.load_analysis_state()
        usage = ledger.empty_state()
        self.bare_commit({cloud.AI_USAGE: usage})
        self.assert_read_only_failure('missing_initial_usage_import')
        ledger.apply_import(usage, self.saved_manifest()['usageImports'][0])
        private['azureAnalysis'] = module.azure.empty_state()
        private['azureAnalysis']['budgets'] = {'2026-09-07': 5}
        self.bare_commit({cloud.AI_USAGE: usage, cloud.PERSONAL: private})
        self.assert_read_only_failure('incomplete_legacy_usage_import')
        private['azureAnalysis']['budgets']['2026-09-07'] = 4
        private['azureAnalysis']['paused'] = {
            'reason': 'azure_auth_stopped', 'httpStatus': 403, 'at': '2026-09-07T03:00:00Z',
        }
        self.bare_commit({cloud.PERSONAL: private})
        self.assert_read_only_failure('unreconciled_ai_usage')
        self.args.mode = 'restore'
        result = cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                                   collector=collector, personal=module)
        self.assertEqual(result['persistenceStatus'], 'restored')
        self.assertEqual(self.remote_json(cloud.PERSONAL)[0], private)

    def test_personal_azure_child_cannot_fall_back_to_private_zero_budget(self):
        environment = {**self.environment, 'PERSONAL_ANALYSIS_BACKEND': 'azure'}
        with mock.patch.object(cloud, 'child_process') as child, \
                self.assertRaisesRegex(cloud.CloudError, 'missing_ai_usage'):
            cloud.invoke_personal_collector(self.root, self.root, self.root / 'report.json', environment)
        child.assert_not_called()

    def test_zero_official_allocation_keeps_scarce_deadline_slot_for_personal(self):
        ledger = cloud.load_analysis_state()
        path = self.root / cloud.AI_USAGE
        for used, hour, minute, personal_active, expected in (
                (29, 13, 29, True, 0), (29, 14, 0, True, 1),
                (28, 13, 29, True, 1), (30, 12, 30, True, 0),
                (29, 13, 29, False, 1)):
            with self.subTest(used=used, hour=hour, minute=minute, personal_active=personal_active):
                state = ledger.empty_state()
                ledger.apply_import(state, {
                    'receiptId': 'a' * 64, 'date': '2026-09-07', 'counts': {'requests': used},
                    'modelBreakdown': [{'model': 'gpt-5.6-luna', 'kind': 'text', 'count': used}],
                    'sourceHash': 'b' * 64,
                })
                collector.atomic_json(path, state)
                now = dt.datetime(2026, 9, 7, hour, minute, tzinfo=cloud.JST)
                self.assertEqual(cloud.official_allocation(
                    path, '12345-1', now, personal_active, True), expected)

    def test_resume_aggregation_keeps_source_counters_and_source_failures(self):
        initial = collector.empty_snapshot()
        initial.update(checkedAt=collector.iso(NOW), posts=[fact(TID), fact(OTHER), fact(THIRD)])
        initial['pending'] = [{**pending(THIRD), 'reason': 'azure_budget_exhausted'}]
        initial['lastRun'] = {
            'status': 'partial', 'dateFrom': '2026-09-04', 'dateTo': '2026-09-05',
            'finishedAt': collector.iso(NOW), 'requests': {'searches': 2, 'posts': 3},
            'sourceCount': 2, 'attemptedCount': 3, 'fetchedCount': 3,
            'newPostCount': 3, 'newNameCount': 6, 'deferredCount': 0, 'pendingCount': 1,
            'sources': [{'url': url, 'status': 'ok', 'candidateCount': 3}
                        for url in collector.SEARCH_URLS],
            'failures': [{'id': THIRD, 'url': collector.canonical(THIRD),
                          'reason': 'azure_budget_exhausted'}],
        }
        resumed = copy.deepcopy(initial)
        resumed['pending'] = []
        resumed['lastRun'] = {
            'status': 'no-new', 'dateFrom': '2026-09-04', 'dateTo': '2026-09-05',
            'finishedAt': collector.iso(NOW + dt.timedelta(minutes=2)),
            'requests': {'searches': 0, 'posts': 0}, 'sourceCount': 0,
            'newPostCount': 0, 'newNameCount': 0, 'failures': [],
        }
        result = cloud.aggregate_official_resume(
            initial, resumed, {THIRD}, initial['lastRun']['requests'], collector)
        self.assertEqual(result['lastRun']['status'], 'ok')
        for field in ('requests', 'sourceCount', 'attemptedCount', 'fetchedCount',
                      'newPostCount', 'newNameCount', 'sources'):
            self.assertEqual(result['lastRun'][field], initial['lastRun'][field])
        self.assertEqual(result['lastRun']['failures'], [])
        self.assertEqual(result['checkedAt'], initial['checkedAt'])
        self.assertEqual(result['posts'], initial['posts'])
        initial['lastRun']['sourceCount'] = 1
        initial['lastRun']['sources'][0] = {
            'url': collector.SEARCH_URLS[0], 'status': 'failed', 'reason': 'network_error',
        }
        result = cloud.aggregate_official_resume(
            initial, resumed, {THIRD}, initial['lastRun']['requests'], collector)
        self.assertEqual(result['lastRun']['status'], 'partial')
        self.assertEqual(result['lastRun']['sources'], initial['lastRun']['sources'])
        self.assertEqual(result['lastSuccessAt'], initial['lastSuccessAt'])

    def buffered_guidance(self, *, personal_attempts=0, near_deadline=False,
                          search_failure=False, corrupt_buffer=False, fail_replay=False,
                          replay_refusal=False, known_queue=False):
        module, seed = self.personal_seed()
        private = module.empty_state()
        module.merge_seed(private, seed)
        self.seed_branch()
        analysis = collector.analysis_module()
        usage = analysis.ledger.empty_state()
        analysis.ledger.apply_import(usage, self.saved_manifest()['usageImports'][0])
        self.bare_commit({cloud.PERSONAL: private, cloud.AI_USAGE: usage})
        self.args.mode = 'daily-guidance'
        self.environment.update(
            GITHUB_EVENT_NAME='schedule', DAILY_GUIDANCE_ENABLED='true',
            AZURE_OPENAI_ENDPOINT='https://offline.openai.azure.com',
            AZURE_OPENAI_API_KEY='OFFLINE_TRANSIENT_AZURE_KEY')
        event = json.loads(self.event_path.read_bytes())
        event['schedule'] = cloud.DAILY_SCHEDULE
        self.event_path.write_text(json.dumps(event), encoding='utf-8')
        now = [dt.datetime(2026, 9, 7, 13 if near_deadline else 12,
                           29 if near_deadline else 30, tzinfo=cloud.JST)]
        ids = [make_id(f'2026-09-07T02:0{minute}:00Z') for minute in range(3)]
        posts = {tid: {**payload(tid), 'text': payload(tid)['text'] + '\n\nみりあちゃんもあとから来るにゃんね',
                       'rawOnly': 'RAW-TRANSIENT-ONLY'} for tid in ids}
        if known_queue:
            initial = collector.empty_snapshot()
            initial['officialAnalysis'] = analysis.empty_state()
            previous = now[0] - dt.timedelta(minutes=10)
            for tid in ids:
                saved = collector.validate_post(
                    tid, posts[tid], dt.date(2026, 9, 7), dt.date(2026, 9, 7), previous)
                initial['posts'].append(saved)
                initial['officialAnalysis']['queue'][tid] = {
                    'createdAt': saved['createdAt'], 'fetchedAt': collector.iso(previous),
                    'bodyHash': analysis.digest(posts[tid]['text']), 'reason': 'azure_budget_exhausted'}
            self.bare_commit({cloud.SNAPSHOT: initial})
        source = OfflineClient(ids, posts)
        search = source.search

        def search_once(url):
            if search_failure and not source.searches:
                source.searches.append(url)
                raise collector.FetchFailure('network_error')
            return search(url)

        source.search = search_once
        trace, phases, buffers = [], [], []
        real_analyzer, real_child = analysis.AzureAnalyzer, cloud.child_process
        outer = self

        def sleep(seconds):
            now[0] += dt.timedelta(seconds=seconds)

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def getcode(self):
                return 200

            def read(self, limit):
                result = {'decision': 'notices', 'date': '2026-09-07', 'notices': [{
                    'name': 'みりあ', 'kind': 'late', 'time': None, 'evidenceLineIds': [1, 2, 8],
                }]}
                message = ({'refusal': 'offline refusal', 'content': None}
                           if replay_refusal and phases[-1][0] == 'replay' else {'content': json.dumps(result)})
                return json.dumps({'model': analysis.transport.MODEL_NAME, 'choices': [{
                    'finish_reason': 'stop', 'message': message,
                }]}).encode('utf-8')[:limit]

        class Opener:
            def open(self, request, timeout):
                trace.append('official')
                return Response()

        def analyzer(state, context, environment, shared, **kwargs):
            return real_analyzer(
                state, context, self.environment, shared, **kwargs,
                names=('あむ', 'こい', 'みりあ'), opener=Opener())

        def child(argv, **kwargs):
            if len(argv) < 4 or argv[0] != sys.executable:
                return real_child(argv, **kwargs)
            name = Path(argv[3]).name
            if name == 'collect-shifts.py':
                args = collector.argument_parser().parse_args(argv[4:])
                phase = 'replay' if args.replay_buffer else 'source'
                phases.append((phase, args.analysis_limit, args.analysis_run_id))
                outer.assertNotIn('GH_TOKEN', kwargs['environment'])
                outer.assertEqual(kwargs['environment']['AZURE_OPENAI_API_KEY'], 'OFFLINE_TRANSIENT_AZURE_KEY')
                if args.replay_buffer:
                    outer.assertEqual(len(source.requests), 3)
                    if fail_replay:
                        return subprocess.CompletedProcess(argv, 4, b'', b'')
                with contextlib.redirect_stdout(io.StringIO()):
                    code = collector.run(
                        args, curated=self.root / 'tools' / 'data' / 'shifts.csv',
                        client=None if args.replay_buffer else source, clock=lambda: now[0], sleep=sleep)
                if args.analysis_buffer:
                    buffers.append(args.analysis_buffer)
                    value = json.loads(args.analysis_buffer.read_bytes())
                    outer.assertLessEqual(len(value['items']), 3)
                    outer.assertIn('RAW-TRANSIENT-ONLY', args.analysis_buffer.read_text(encoding='utf-8'))
                    if corrupt_buffer:
                        value['items'] = value['items'][:1] * 4
                        collector.atomic_json(args.analysis_buffer, value)
                return subprocess.CompletedProcess(argv, code, b'', b'')
            if name == 'collect-personal-shifts.py':
                args = module.argument_parser().parse_args(argv[4:])
                state = module.read_state(args.snapshot)
                day = now[0].astimezone(cloud.JST).date().isoformat()
                budget = state['budgets'].setdefault(day, {'searches': 0, 'posts': 0})
                budget['searches'] += 3
                budget['posts'] += personal_attempts
                sleep(3)
                with analysis.ledger.SharedUsage(
                        args.ai_state, run_id=args.analysis_run_id, component='personal',
                        clock=lambda: now[0], sleep=sleep, request_limit=args.analysis_limit) as shared:
                    for index in range(personal_attempts):
                        key = cloud.data_hash(['personal-buffer-test', index])
                        shared.reserve(key, {
                            'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
                            'deployment': 'gpt-5.6-luna', 'model': 'gpt-5.6-luna', 'modelVersion': '2026-07-09',
                        })
                        shared.issued(key)
                        shared.finish(key, 'no_event')
                        trace.append('personal')
                state['lastRun'] = {
                    'status': 'no-new' if personal_attempts else 'no-results', 'date': day,
                    'sourceCount': 3, 'requests': {'searches': 3, 'posts': personal_attempts},
                }
                collector.atomic_json(args.snapshot, state)
                collector.atomic_json(args.report, {
                    'component': 'personal', 'status': state['lastRun']['status'], 'exitCode': 0,
                })
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            return real_child(argv, **kwargs)

        with mock.patch.object(collector, 'ROOT', self.root), \
                mock.patch.object(collector, 'utc_now', side_effect=lambda: now[0]), \
                mock.patch.object(collector, 'analysis_module', return_value=analysis), \
                mock.patch.object(collector, 'PublicClient', side_effect=AssertionError('No new source client')), \
                mock.patch.object(analysis, 'AzureAnalyzer', side_effect=analyzer), \
                mock.patch.object(cloud, 'child_process', side_effect=child):
            if corrupt_buffer or fail_replay:
                reason = 'official_analysis_buffer_invalid' if corrupt_buffer else 'official_resume_local_failure'
                with self.assertRaisesRegex(cloud.CloudError, reason):
                    cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                                      collector=collector, personal=module)
                result = None
            else:
                result = cloud.orchestrate(self.args, root=self.root, environment=self.environment,
                                           collector=collector, personal=module)
        self.assertEqual((len(source.searches), len(source.requests)), (2, 3))
        self.assertTrue(all(not path.exists() for path in buffers))
        self.assertFalse(list(self.root.glob('.cc-work-*')))
        self.assertNotIn(cloud.ANALYSIS_BUFFER, self.remote_names())
        for path in (self.root / 'recovery').iterdir():
            self.assertNotEqual(path.name, cloud.ANALYSIS_BUFFER)
            self.assertNotIn(b'RAW-TRANSIENT-ONLY', path.read_bytes())
            self.assertNotIn(b'OFFLINE_TRANSIENT_AZURE_KEY', path.read_bytes())
        saved = self.remote_json(cloud.SNAPSHOT)[0]
        ledger = self.remote_json(cloud.AI_USAGE)[0]
        return result, saved, ledger, trace, phases, now[0]

    def test_personal_no_candidates_returns_third_slot_without_another_source_get(self):
        result, state, usage, trace, phases, now = self.buffered_guidance()
        self.assertEqual(trace, ['official', 'official', 'official'])
        self.assertEqual([(phase, limit) for phase, limit, _ in phases], [('source', 2), ('replay', 3)])
        self.assertEqual(len({run for _, _, run in phases}), 1)
        self.assertEqual(result['officialCollectionStatus'], 'ok')
        self.assertEqual(state['lastRun']['requests'], {'searches': 2, 'posts': 3})
        self.assertEqual((state['lastRun']['newPostCount'], state['lastRun']['newNameCount']), (3, 6))
        self.assertEqual(state['pending'], [])
        self.assertTrue(all(len(post['notices']) == 1 for post in state['posts']))
        replayed = state['posts'][0]
        cached = next(entry for entry in state['officialAnalysis']['cache'].values()
                      if entry['postId'] == replayed['id'])
        self.assertGreater(collector.timestamp(cached['at']),
                           collector.timestamp(replayed['notices'][0]['observedAt']))
        spec = importlib.util.spec_from_file_location('buffer_pages', ROOT / 'tools' / 'pages.py')
        pages = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pages)
        public = pages.load_public_snapshot(self.output)
        self.assertEqual(public['posts'], state['posts'])
        self.assertNotIn('RAW-TRANSIENT-ONLY', json.dumps(public))
        self.assertNotIn('officialAnalysis', public)
        self.assertEqual(cloud.load_analysis_state().remaining(usage, '12345-1', now), 0)
        self.assertNotIn(cloud.LEASE, self.remote_names())

    def test_shared_slots_stay_fair_before_one_source_free_official_replay(self):
        result, state, usage, trace, phases, now = self.buffered_guidance(
            personal_attempts=1, near_deadline=True)
        self.assertEqual(trace, ['official', 'personal', 'official'])
        self.assertEqual([(phase, limit) for phase, limit, _ in phases], [('source', 1), ('replay', 2)])
        self.assertEqual(result['officialCollectionStatus'], 'partial')
        self.assertEqual(len(state['pending']), 1)
        self.assertEqual(cloud.load_analysis_state().remaining(usage, '12345-1', now), 0)
        self.assertEqual(state['lastRun']['requests'], {'searches': 2, 'posts': 3})

    def test_known_queue_only_uses_returned_personal_slot_without_replay_get(self):
        result, state, usage, trace, phases, now = self.buffered_guidance(known_queue=True)
        self.assertEqual(trace, ['official'] * 3)
        self.assertEqual([(phase, limit) for phase, limit, _ in phases], [('source', 2), ('replay', 3)])
        self.assertEqual(result['officialCollectionStatus'], 'no-new')
        self.assertEqual(state['lastRun']['requests'], {'searches': 2, 'posts': 3})
        self.assertEqual((state['lastRun']['newPostCount'], state['lastRun']['newNameCount']), (0, 0))
        self.assertEqual(state['pending'], [])
        self.assertTrue(all(len(post['notices']) == 1 for post in state['posts']))
        self.assertEqual(cloud.load_analysis_state().remaining(usage, '12345-1', now), 0)

    def test_official_replay_does_not_clear_initial_search_failure(self):
        result, state, _, trace, phases, _ = self.buffered_guidance(search_failure=True)
        self.assertEqual(trace, ['official'] * 3)
        self.assertEqual(len(phases), 2)
        self.assertEqual(result['officialCollectionStatus'], 'partial')
        self.assertEqual(state['lastRun']['sourceCount'], 1)
        self.assertEqual(state['lastRun']['sources'][0]['reason'], 'network_error')
        self.assertEqual(state['lastRun']['requests'], {'searches': 2, 'posts': 3})
        self.assertIsNone(state['lastSuccessAt'])

    def test_invalid_or_failed_transient_buffer_is_never_recovered_or_published(self):
        self.buffered_guidance(corrupt_buffer=True)
        self.assertIn(cloud.LEASE, self.remote_names())

    def test_failed_official_replay_keeps_lease_without_retaining_raw_buffer(self):
        self.buffered_guidance(fail_replay=True)
        self.assertIn(cloud.LEASE, self.remote_names())

    def test_official_replay_refusal_remains_partial_without_dropping_source_facts(self):
        result, state, usage, trace, _, now = self.buffered_guidance(replay_refusal=True)
        self.assertEqual(trace, ['official'] * 3)
        self.assertEqual(result['officialCollectionStatus'], 'partial')
        self.assertEqual(state['pending'][0]['reason'], 'azure_refused')
        self.assertEqual(state['lastRun']['failures'][0]['reason'], 'azure_refused')
        self.assertEqual((state['lastRun']['newPostCount'], state['lastRun']['newNameCount']), (3, 6))
        self.assertEqual(state['lastRun']['requests'], {'searches': 2, 'posts': 3})
        self.assertEqual(cloud.load_analysis_state().remaining(usage, '12345-1', now), 0)

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


class PersonalSavedTests(unittest.TestCase):
    def setUp(self):
        self.personal = cloud.load_personal_collector()
        self.saved = cloud.load_personal_saved()
        self.state, self.usage, self.entries = saved_personal_fixture(self.personal)

    def apply(self, state=None, entries=None, usage=None):
        return self.saved.apply_amendments(
            self.state if state is None else state,
            self.entries if entries is None else entries,
            self.usage if usage is None else usage, self.personal)

    def test_batch_proposed_bindings_reject_name_author_and_handle_conflicts(self):
        first = self.entries[0]['amendment']['source']
        variants = (
            ({'name': first['name']}, 'saved_personal_identity_mismatch'),
            ({'name': first['name'], 'authorId': first['authorId']},
             'saved_personal_identity_mismatch'),
            ({'name': first['name'], 'authorScreenName': first['authorScreenName']},
             'saved_personal_identity_mismatch'),
            ({'authorId': first['authorId']}, 'saved_personal_binding_collision'),
            ({'authorScreenName': first['authorScreenName'].upper()}, 'saved_personal_binding_collision'),
        )
        for identity, reason in variants:
            with self.subTest(identity=identity):
                state, usage, entries = saved_personal_pending_pair(self.personal, identity)
                before, before_usage = copy.deepcopy(state), copy.deepcopy(usage)
                first_result = self.apply(state=state, entries=[entries[0]], usage=usage)
                self.apply(state=state, entries=[entries[1]], usage=usage)
                for ordered in (entries, list(reversed(entries))):
                    with self.assertRaisesRegex(ValueError, reason):
                        self.apply(state=state, entries=ordered, usage=usage)
                next_entry = copy.deepcopy(entries[1])
                next_entry['expectedSubjectHash'] = self.saved.subject_hash(
                    first_result, next_entry['amendment']['id'])
                with self.assertRaisesRegex(ValueError, reason):
                    self.apply(state=first_result, entries=[next_entry], usage=usage)
                self.assertEqual(state, before)
                self.assertEqual(usage, before_usage)

    def test_batch_same_identity_can_add_two_known_posts_and_merge_as_seed(self):
        first = self.entries[0]['amendment']['source']
        identity = {field: first[field] for field in ('name', 'authorId', 'authorScreenName')}
        state, usage, entries = saved_personal_pending_pair(self.personal, identity)
        after = self.apply(state=state, entries=entries, usage=usage)
        self.assertEqual(len(after['posts']), len(state['posts']) + 2)
        self.assertEqual(after['identityBindings'][first['name']], {
            'authorId': first['authorId'], 'authorScreenName': first['authorScreenName'],
            'verifiedAt': first['fetchedAt'],
        })
        self.assertEqual(self.apply(state=after, entries=entries, usage=usage), after)
        seeded = self.personal.empty_state()
        self.personal.merge_seed(seeded, self.personal.public_state(after))
        self.assertEqual(seeded['posts'], after['posts'])
        self.assertEqual(seeded['identityBindings'][first['name']]['authorId'], first['authorId'])

    def test_exact_bounded_delta_preserves_unrelated_facts_history_and_budgets(self):
        original = copy.deepcopy(self.state)
        kept = copy.deepcopy(self.state['posts'][0])
        kept['id'] = make_id('2026-09-05T15:04:00Z')
        kept['url'] = self.personal.public_url(kept['authorScreenName'], kept['id'])
        kept['createdAt'] = '2026-09-05T15:04:00Z'
        kept['events'] = [{'shift': '夜', 'kind': 'absence', 'excerpt': 'お休みします'}]
        self.state['posts'].append(kept)
        unrelated = copy.deepcopy(self.state['pending'][0])
        unrelated['id'] = make_id('2026-09-05T15:05:00Z')
        unrelated['url'] = self.personal.public_url(unrelated['authorScreenName'], unrelated['id'])
        unrelated['searchCreatedAt'] = '2026-09-05T15:05:00Z'
        self.state['pending'].append(unrelated)
        self.state.update(coverage={}, searchHistory={})
        self.state['paused'] = {
            'reason': 'http_error', 'host': self.personal.POST_HOST,
            'at': '2026-09-07T04:00:00Z', 'retryAt': '2026-09-08T04:00:00Z', 'httpStatus': 429,
        }
        before, usage = copy.deepcopy(self.state), copy.deepcopy(self.usage)
        after = self.apply()
        self.assertEqual(after['posts'][1], kept)
        self.assertEqual(after['pending'], [unrelated])
        for field in ('budgets', 'paused', 'lastRequests', 'lastRun', 'checkedAt', 'lastSuccessAt',
                      'originalTargets', 'coverage', 'searchHistory'):
            self.assertEqual(after[field], before[field])
        for name, binding in original['identityBindings'].items():
            self.assertEqual(after['identityBindings'][name], binding)
        self.assertEqual(after['identityBindings']['あむ'], {
            'authorId': self.entries[0]['amendment']['source']['authorId'],
            'authorScreenName': 'amu_zettai', 'verifiedAt': '2026-09-06T04:00:00Z',
        })
        self.assertEqual(self.usage, usage)
        self.assertEqual(self.state, before)

    def test_replay_is_noop_even_if_a_newer_post_or_current_facts_changed(self):
        after = self.apply()
        after['posts'][0]['events'] = [{'shift': '昼', 'kind': 'absence', 'excerpt': 'お休み'}]
        before = copy.deepcopy(after)
        self.assertEqual(self.apply(state=after), before)
        refreshed = copy.deepcopy(self.entries)
        for entry in refreshed:
            entry['expectedSubjectHash'] = self.saved.subject_hash(after, entry['amendment']['id'])
        self.assertEqual(self.apply(state=after, entries=refreshed), before)
        refreshed[0]['expectedSubjectHash'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'saved_personal_receipt_conflict'):
            self.apply(state=after, entries=refreshed)
        altered = copy.deepcopy(self.entries)
        altered[0]['amendment']['events'][0]['storeId'] = 's4'
        with self.assertRaisesRegex(ValueError, 'saved_personal_receipt_conflict'):
            self.apply(state=after, entries=altered)

    def test_known_resolved_requires_binding_and_never_invents_search_metadata(self):
        for change in (
                lambda: self.state['identityBindings'].pop('ららこ'),
                lambda: self.entries[1]['amendment']['source'].update(
                    provenance='search', searchCreatedAt=self.entries[1]['amendment']['source']['createdAt']),
                lambda: self.entries[0]['amendment']['source'].update(provenance='direct'),
                lambda: self.state['identityBindings'].update({
                    '別人': {'authorId': '1180156105181159424', 'authorScreenName': 'other',
                             'verifiedAt': '2026-09-06T04:00:00Z'}})):
            with self.subTest(change=change):
                self.state, self.usage, self.entries = saved_personal_fixture(self.personal)
                change()
                for entry in self.entries:
                    entry['expectedSubjectHash'] = self.saved.subject_hash(
                        self.state, entry['amendment']['id'])
                with self.assertRaises(ValueError):
                    self.apply()

    def test_pending_source_identity_and_subject_hash_are_not_optional(self):
        for field, value in (('name', '別人'), ('authorId', '123'), ('authorScreenName', 'other'),
                             ('date', '2026-09-05'), ('createdAt', '2026-09-05T15:08:00Z'),
                             ('searchCreatedAt', '2026-09-05T15:01:00.100Z'),
                             ('fetchedAt', '2026-09-05T15:00:00Z')):
            with self.subTest(field=field):
                bad = copy.deepcopy(self.entries)
                bad[0]['amendment']['source'][field] = value
                with self.assertRaises(ValueError):
                    self.apply(entries=bad)
        self.state['pending'][0]['attempts'] += 1
        with self.assertRaisesRegex(ValueError, 'saved_personal_subject_mismatch'):
            self.apply()

    def test_saved_pending_uses_direct_provenance_and_never_creates_a_binding(self):
        for metadata_source in ('saved_binding', 'saved_post'):
            with self.subTest(metadata_source=metadata_source):
                state, usage, entries = saved_personal_fixture(self.personal)
                pending = state['pending'][0]
                source = entries[0]['amendment']['source']
                if metadata_source == 'saved_post':
                    pending['sourceCreatedAt'] = source['createdAt']
                pending.update(searchCreatedAt=None, metadataSource=metadata_source)
                binding = {field: source[field] for field in ('authorId', 'authorScreenName')}
                binding['verifiedAt'] = '2026-09-06T05:00:00Z'
                state['identityBindings'][source['name']] = binding
                entries[0]['expectedSubjectHash'] = self.saved.subject_hash(state, pending['id'])
                with self.assertRaisesRegex(ValueError, 'saved_personal_search_mismatch'):
                    self.apply(state=state, entries=entries, usage=usage)
                source['provenance'] = 'direct'
                del source['searchCreatedAt']
                after = self.apply(state=state, entries=entries, usage=usage)
                self.assertEqual(after['identityBindings'][source['name']], binding)
                del state['identityBindings'][source['name']]
                entries[0]['expectedSubjectHash'] = self.saved.subject_hash(state, pending['id'])
                with self.assertRaisesRegex(ValueError, 'missing_saved_personal_binding'):
                    self.apply(state=state, entries=entries, usage=usage)

    def test_all_proof_hashes_contract_and_accounted_model_identity_are_required(self):
        for field in self.saved.SOURCE_FIELDS:
            bad = copy.deepcopy(self.entries)
            del bad[0]['amendment']['source'][field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                self.apply(entries=bad)
        for field, value in (('model', 'gpt-5.4-mini'), ('deployment', 'other'),
                             ('modelVersion', 'latest'), ('contractVersion', 'unrecorded-contract'),
                             ('analysisReceiptHash', 'not-a-hash'), ('usageReceiptId', 'f' * 64),
                             ('usageSourceHash', 'f' * 64), ('sourceHash', None)):
            bad = copy.deepcopy(self.entries)
            bad[0]['amendment']['source'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.apply(entries=bad)

    def test_aggregate_usage_is_not_recreated_or_overallocated(self):
        for field, value in (('kind', 'image'), ('component', 'official'),
                             ('modelVersion', 'other'), ('deployment', 'other'),
                             ('count', 2)):
            usage = copy.deepcopy(self.usage)
            usage['imports']['a' * 64]['modelBreakdown'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.apply(usage=usage)
        manifest = {'usageImports': [self.usage['imports']['a' * 64]],
                    'sourceReceipts': [], 'officialAmendments': [], 'personalAmendments': self.entries}
        with self.assertRaises(ValueError):
            cloud.prepare_saved(manifest, collector.empty_snapshot(), self.state,
                                cloud.load_analysis_state().empty_state(), collector, self.personal)
        self.assertEqual(self.usage['receipts'], {})

    def test_v4_optional_links_can_only_restate_accepted_explicit_event_scopes(self):
        for links in (
                [{'scope': 'unspecified', 'status': 'work'}],
                [{'scope': '夜', 'status': 'work'}],
                [{'scope': '昼', 'status': 'withdrawn'}],
                [{'scope': '昼', 'status': 'conflict'}]):
            entries = copy.deepcopy(self.entries)
            entries[0]['amendment']['links'] = links
            with self.subTest(links=links), self.assertRaises(ValueError):
                self.apply(entries=entries)
        entries = copy.deepcopy(self.entries)
        entries[0]['amendment']['events'] = []
        with self.assertRaises(ValueError):
            self.apply(entries=entries)
        entries = copy.deepcopy(self.entries)
        for entry in entries:
            del entry['amendment']['links']
        self.assertTrue(all('links' not in post for post in self.apply(entries=entries)['posts']))

    def test_synthetic_v5_link_only_attestation_makes_no_placement_or_attendance(self):
        entries = copy.deepcopy(self.entries)
        amendment = entries[0]['amendment']
        amendment['source']['contractVersion'] = self.saved.LINK_CONTRACT
        amendment.update(events=[], links=[{'scope': 'unspecified', 'status': 'work'}])
        after = self.apply(entries=entries)
        post = next(item for item in after['posts'] if item['id'] == amendment['id'])
        self.assertEqual(post['events'], [])
        self.assertEqual(post['links'], [{'scope': 'unspecified', 'status': 'work'}])

    def test_private_receipts_reject_raw_unknown_fields_forgery_and_unbounded_data(self):
        after = self.apply()
        self.saved.validate_imports(after['savedPersonalImports'], self.personal.azure_context())
        receipt_id = next(iter(after['savedPersonalImports']))
        for level, field in (('entry', 'raw'), ('amendment', 'text'), ('source', 'bodyLines'),
                             ('source', 'cache'), ('source', 'endpoint'), ('event', 'evidenceLineIds')):
            receipts = copy.deepcopy(after['savedPersonalImports'])
            entry = receipts[receipt_id]
            target = (entry if level == 'entry' else entry['amendment'] if level == 'amendment'
                      else entry['amendment']['source'] if level == 'source'
                      else entry['amendment']['events'][0])
            target[field] = 'not allowed'
            with self.subTest(level=level, field=field), self.assertRaises(ValueError):
                self.saved.validate_imports(receipts, self.personal)
        receipts = copy.deepcopy(after['savedPersonalImports'])
        receipts['f' * 64] = receipts.pop(receipt_id)
        with self.assertRaises(ValueError):
            self.saved.validate_imports(receipts, self.personal)
        with self.assertRaises(ValueError):
            self.apply(entries=self.entries * 2)
        with self.assertRaises(ValueError):
            self.apply(entries=[self.entries[0], self.entries[0]])
        with self.assertRaises(ValueError):
            self.saved.validate_imports({str(index): {} for index in range(1001)}, self.personal)


if __name__ == '__main__':
    unittest.main()
