"""Offline only: python -m unittest discover -s tools/tests -p test_pages.py."""
import contextlib
import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('pages', ROOT / 'tools' / 'pages.py')
pages = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pages)
collector = pages.load_collector()
NOW = dt.datetime(2026, 9, 5, 18, 50, tzinfo=collector.UTC)
CREATED = '2026-09-05T09:17:05Z'
TID = '2096165714604486679'
SHA = '0123456789abcdef0123456789abcdef01234567'
SECRET = r'C:\Users\private-person\secrets\cookie.txt'


def make_id(created):
    delta = collector.timestamp(created) - dt.datetime(1970, 1, 1, tzinfo=collector.UTC)
    milliseconds = delta.days * 86400000 + delta.seconds * 1000
    return str((milliseconds - 1288834974657) << 22)


OTHER = make_id('2026-09-05T08:00:00Z')
CURATED = make_id('2026-09-05T07:00:00Z')


def fact(tid=TID, created=CREATED):
    return {
        'id': tid, 'url': collector.canonical(tid),
        'authorId': collector.AUTHOR_ID, 'authorScreenName': collector.AUTHOR,
        'createdAt': created, 'date': collector.service_day(collector.timestamp(created)).isoformat(),
        'shift': '夜', 'storeId': 's1', 'names': ['あむ', 'あずにゃん'],
        'observedAt': collector.iso(NOW),
    }


def snapshot():
    value = collector.empty_snapshot()
    value.update(checkedAt=collector.iso(NOW), lastSuccessAt=collector.iso(NOW),
                 posts=[fact()])
    value['lastRun'] = {
        'status': 'ok', 'dateFrom': '2026-09-05', 'dateTo': '2026-09-06',
        'sourceCount': 2, 'newPostCount': 1, 'newNameCount': 2,
    }
    return value


def payload(tid=TID, created=CREATED, text=None):
    return {
        'id_str': tid, 'user': {'id_str': collector.AUTHOR_ID, 'screen_name': collector.AUTHOR},
        'created_at': created,
        'text': text if text is not None else '【アキバ絶対領域】\nよるにゃんこ\n\nあむ\nあずにゃん\n⊂(´ω´⊂)))',
        'private_path': SECRET,
    }


class FakeResponse:
    def __init__(self, body, code=200, headers=None):
        self.body = body
        self.code = code
        self.headers = headers or {}
        self.closed = False

    def getcode(self):
        return self.code

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def close(self):
        self.closed = True


class FakeOpener:
    """Only HTTP is replaced: discovery, parser, validation and locks stay real."""
    def __init__(self, searches=None, posts=None):
        self.searches = searches if searches is not None else [[TID], [TID]]
        self.posts = posts if posts is not None else {TID: payload()}
        self.calls = []

    def open(self, request, timeout=45):
        url = request.full_url
        self.calls.append(request)
        if url in collector.SEARCH_URLS:
            response = self.searches[list(collector.SEARCH_URLS).index(url)]
            if isinstance(response, list):
                body = ('<p>検索結果はありません</p>' if not response else ''.join(
                    f'<a href="{collector.canonical(tid)}">post</a>' for tid in response))
                return FakeResponse(body.encode('utf-8'))
        else:
            tid = parse_qs(urlsplit(url).query)['id'][0]
            response = self.posts[tid]
            if isinstance(response, dict):
                return FakeResponse(json.dumps(response, ensure_ascii=False).encode('utf-8'))
        if isinstance(response, Exception):
            raise response
        return response


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='.pages-test-', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = self.work / 'repo'
        self.root.mkdir()
        self.write('index.html', (
            '<!doctype html>\r\n<link rel="stylesheet" href="styles.css?v=oldhash">\r\n'
            '<script src="data/schedule.js?v=oldhash"></script>\r\n'
            '<script src="data/store-insights.js?v=oldhash"></script>\r\n'
            '<script src="app.js?v=oldhash"></script>\r\n'
            '<a href="#calendar">skip</a><a href="https://x.com/akibazettai">source</a>\r\n'
        ).encode())
        self.write('app.js', b'const src = "assets/events/flower.svg";\r\n')
        self.write('styles.css', b'body { color: #123; }\r\n')
        self.write('data/schedule.js', b'window.SCHEDULE_DATA = {};\r\n')
        self.write('data/store-insights.js', b'window.STORE_INSIGHTS = {};\r\n')
        self.write('data/observed-shifts.json', pages.json_bytes(snapshot()))
        self.write('assets/events/flower.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>\r\n')
        self.write('tools/data/shifts.csv', ('tweet_id,maid\n' + CURATED + ',あむ\n').encode('utf-8'))

    def write(self, name, body):
        path = self.root.joinpath(*name.split('/'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def tree_bytes(self, root=None):
        root = root or self.root
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob('*') if path.is_file()}

    def stage(self, output=None, revision=SHA):
        return pages.stage(output or self.root / '_site', revision,
                           root=self.root, clock=lambda: NOW)

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Symlink creation unavailable: ' + type(exc).__name__)


class ProjectionTests(WorkspaceTests):
    def test_projection_drops_unknown_private_metadata_and_unverified_ids(self):
        state = snapshot()
        state.update(
            snapshotPath=SECRET, processId=123, config={'cookie': SECRET},
            lease={'token': SECRET}, cooldowns={collector.POST_HOST: collector.iso(NOW)},
            rawText=SECRET,
            resolved=[{'id': OTHER, 'url': collector.canonical(OTHER),
                       'reason': 'not_shift_post', 'resolvedAt': collector.iso(NOW),
                       'private': SECRET}],
            pending=[{'id': CURATED, 'url': collector.canonical(CURATED),
                      'reason': 'parse_failed', 'firstSeenAt': collector.iso(NOW),
                      'lastAttemptAt': collector.iso(NOW), 'attempts': 1, 'private': SECRET}],
        )
        state['posts'][0].update(text=SECRET, rawSource=SECRET, cookie=SECRET,
                                 futureMetadata={'fullText': SECRET})
        state['lastRun'].update(
            failures=[{'id': CURATED, 'path': SECRET}], rejected=[{'id': OTHER}],
            snapshotPath=SECRET, processId=123, pendingCount=1, fetchedCount=1,
            finishedAt=SECRET, sourcePageLimit=2, lease=SECRET,
        )
        path = self.write('data/observed-shifts.json', pages.json_bytes(state))
        before = path.read_bytes()
        result = pages.load_public_snapshot(path)
        self.assertEqual(set(result), {'schemaVersion', 'complete', 'checkedAt',
                                      'lastSuccessAt', 'posts', 'pending', 'lastRun'})
        self.assertEqual(set(result['lastRun']), {
            'status', 'dateFrom', 'dateTo', 'sourceCount', 'newPostCount', 'newNameCount'})
        self.assertEqual(set(result['posts'][0]), set(pages.POST_FIELDS))
        self.assertEqual(result['pending'], [])
        serialized = json.dumps(result, ensure_ascii=False)
        for private in (SECRET, 'private-person', OTHER, CURATED, 'cookie',
                        'lease', 'processId', 'fullText', 'pendingCount'):
            self.assertNotIn(private, serialized)
        self.assertEqual(path.read_bytes(), before)
        result['posts'][0]['names'].append('るる')
        self.assertEqual(state['posts'][0]['names'], ['あむ', 'あずにゃん'])

    def test_collector_snapshot_validation_is_also_used(self):
        state = snapshot()
        state['cooldowns'] = {'private.invalid': collector.iso(NOW)}
        path = self.write('data/observed-shifts.json', pages.json_bytes(state))
        with self.assertRaisesRegex(pages.PagesError, 'invalid_public_snapshot'):
            pages.load_public_snapshot(path)

    def test_name_shape_rejects_paths_body_markup_and_wrong_types(self):
        for name in (SECRET, '/home/runner/secret', '../secret', '..\\secret', 'a.txt',
                     'S-1-5-21-123', 'a\nb', 'あむ こい', 'https://example.com',
                     '<script>', 'あむ🎉', '待ってるにゃんね', 'httpsecret',
                     'あ' * 13, '', 123, None, {}, ['あむ']):
            with self.subTest(name=name):
                state = snapshot()
                state['posts'][0]['names'] = [name]
                with self.assertRaisesRegex(pages.PagesError, 'invalid_public_snapshot'):
                    pages.public_projection(state)
        for names in ('あむ', [], ['あむ', 'あむ']):
            with self.subTest(names=names):
                state = snapshot()
                state['posts'][0]['names'] = names
                with self.assertRaises(pages.PagesError):
                    pages.public_projection(state)

    def test_name_shape_keeps_producer_names_without_alias_correction(self):
        state = snapshot()
        state['posts'][0]['names'] = ['もな', 'あずにゃん', 'A9', 'ａｂ', '猫', 'メイドー']
        state['lastRun']['newNameCount'] = 6
        self.assertEqual(pages.public_projection(state)['posts'][0]['names'],
                         state['posts'][0]['names'])

    def test_fact_types_identity_and_dates_fail_closed(self):
        changes = {
            'id': [int(TID), float(TID), True, '1', OTHER],
            'url': [SECRET, collector.canonical(OTHER), collector.canonical(TID) + '?secret=1'],
            'authorId': [int(collector.AUTHOR_ID), '1', {}],
            'authorScreenName': ['other', None],
            'createdAt': [SECRET, '2026-09-04T09:17:05Z', '2026-09-05T09:17:05', 0],
            'observedAt': [SECRET, '2026-09-04T09:17:05Z'],
            'date': ['2026-09-04', '2026-09-31', '20260905', 20260905],
            'shift': ['ひる', None, {}],
            'storeId': ['s5', 1, None],
        }
        for field, values in changes.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    state = snapshot()
                    state['posts'][0][field] = value
                    with self.assertRaisesRegex(pages.PagesError, 'invalid_public_snapshot'):
                        pages.public_projection(state)
        state = snapshot()
        state['posts'].append(copy.deepcopy(state['posts'][0]))
        with self.assertRaises(pages.PagesError):
            pages.public_projection(state)
        for field in pages.POST_FIELDS:
            state = snapshot()
            del state['posts'][0][field]
            with self.subTest(missing=field), self.assertRaises(pages.PagesError):
                pages.public_projection(state)

    def test_metadata_is_typed_and_canonicalized(self):
        cases = (
            ('schemaVersion', True), ('schemaVersion', '1'), ('complete', 0),
            ('complete', True), ('checkedAt', SECRET), ('lastSuccessAt', 42),
            ('posts', {}), ('pending', None), ('lastRun', []),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                state = snapshot()
                state[field] = value
                with self.assertRaises(pages.PagesError):
                    pages.public_projection(state)
        for changes in (
            {'status': SECRET}, {'status': []}, {'dateFrom': SECRET},
            {'dateFrom': None}, {'dateTo': '2026-09-04'},
            {'sourceCount': 3}, {'sourceCount': True}, {'newPostCount': 2},
            {'newNameCount': 3}, {'newNameCount': '2'}, {'newPostCount': -1},
        ):
            with self.subTest(changes=changes):
                state = snapshot()
                state['lastRun'].update(changes)
                with self.assertRaises(pages.PagesError):
                    pages.public_projection(state)
        state = snapshot()
        state['checkedAt'] = NOW.astimezone(collector.JST).isoformat()
        self.assertEqual(pages.public_projection(state)['checkedAt'], collector.iso(NOW))
        self.assertEqual(pages.public_projection(collector.empty_snapshot())['posts'], [])

    def test_service_day_uses_five_am_boundary(self):
        created = '2026-09-05T19:00:00Z'
        state = snapshot()
        state['posts'][0] = fact(make_id(created), created)
        state['posts'][0]['observedAt'] = '2026-09-05T20:00:00Z'
        result = pages.public_projection(state)
        self.assertEqual(result['posts'][0]['date'], '2026-09-05')


class StageTests(WorkspaceTests):
    def test_allowlist_excludes_backend_and_same_named_private_files(self):
        private_names = (
            'README.md', '.git', 'staticwebapp.config.json', 'config.json', 'logs/run.log',
            'data/observed-shifts.http-state.json', 'data/observed-shifts.lock',
            'data/rawsource.json', 'http-state.json', 'lock', 'rawsource.txt',
            'tools/collect-shifts.py', 'tools/tests/test_secret.py',
            'backend/index.html', 'backend/app.js', 'backend/styles.css',
            'backend/data/schedule.js', 'backend/data/observed-shifts.json',
            'assets/events/private.txt', 'assets/events/private.json',
            'assets/events/nested/private.svg',
        )
        for name in private_names:
            self.write(name, SECRET.encode())
        self.write('assets/events/second.svg', b'<svg/>\n')
        state = snapshot()
        state['secret'] = SECRET
        state['posts'][0]['text'] = SECRET
        self.write('data/observed-shifts.json', pages.json_bytes(state))
        before = self.tree_bytes()
        manifest = self.stage()
        expected = set(pages.PUBLIC_FILES) | {
            'assets/events/flower.svg', 'assets/events/second.svg', 'version.json'}
        artifact = self.tree_bytes(self.root / '_site')
        self.assertEqual(set(artifact), expected)
        for body in artifact.values():
            self.assertNotIn(b'private-person', body)
        after = self.tree_bytes()
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(set(after) - set(before), {'_site/' + name for name in expected})
        self.assertEqual(set(manifest['files']), expected - {'version.json'})

    def test_raw_hashes_preserve_crlf_and_do_not_rewrite_index_vhash(self):
        manifest = self.stage(revision=SHA.upper())
        self.assertEqual(set(manifest), {'revision', 'sourceSha', 'buildtime', 'observation', 'files'})
        self.assertEqual(manifest['revision'], SHA)
        self.assertEqual(manifest['sourceSha'], SHA)
        self.assertEqual(manifest['buildtime'], collector.iso(NOW))
        self.assertEqual(manifest['observation'], {'checkedAt': collector.iso(NOW)})
        self.assertEqual(json.loads((self.root / '_site' / 'version.json').read_text('utf-8')),
                         manifest)
        for name, entry in manifest['files'].items():
            body = self.root.joinpath('_site', *name.split('/')).read_bytes()
            self.assertEqual(set(entry), {'rawSHA256'})
            self.assertEqual(entry['rawSHA256'], hashlib.sha256(body).hexdigest())
            if name != 'data/observed-shifts.json':
                self.assertEqual(body, self.root.joinpath(*name.split('/')).read_bytes())
        body = (self.root / 'app.js').read_bytes()
        self.assertNotEqual(manifest['files']['app.js']['rawSHA256'],
                            hashlib.sha256(body.replace(b'\r\n', b'\n')).hexdigest())
        self.assertIn(b'v=oldhash', (self.root / '_site' / 'index.html').read_bytes())

    def test_existing_output_including_empty_directory_is_untouched(self):
        for name, is_dir in (('existing', True), ('existing.json', False), ('empty', True)):
            target = self.root / name
            if is_dir:
                target.mkdir()
                if name != 'empty':
                    (target / 'owned.txt').write_text('keep', encoding='utf-8')
            else:
                target.write_text('keep', encoding='utf-8')
            before = self.tree_bytes()
            with self.subTest(name=name), self.assertRaisesRegex(pages.PagesError, 'output_must_be_new'):
                self.stage(target)
            self.assertEqual(self.tree_bytes(), before)

    def test_unsafe_output_root_ancestor_external_and_source_dirs(self):
        for target in (
            self.root, self.work, self.work / 'external', self.root / 'data' / 'artifact',
            self.root / 'tools' / 'artifact', self.root / '.git' / 'artifact',
            self.root / 'assets' / 'artifact',
        ):
            with self.subTest(target=target), self.assertRaises(pages.PagesError):
                self.stage(target)
        nested = self.root / 'nested-repo'
        nested.mkdir()
        (nested / '.git').write_text('gitdir: ignored', encoding='utf-8')
        with self.assertRaisesRegex(pages.PagesError, 'unsafe_output'):
            self.stage(nested / 'artifact')

    def test_output_rejects_windows_aliases_devices_and_alternate_streams(self):
        for name in ('DATA/artifact', 'TOOLS/artifact', '.GIT/artifact',
                     'report:secret.json', 'artifact.', 'artifact ', 'NUL.json', 'CON'):
            with self.subTest(name=name), self.assertRaises(pages.PagesError):
                self.stage(self.root.joinpath(*name.split('/')))

    def test_revision_requires_exact_full_sha_before_any_write(self):
        for revision in ('a' * 39, 'a' * 41, 'z' * 40, SHA + '\n', SECRET, None):
            with self.subTest(revision=revision), self.assertRaisesRegex(
                    pages.PagesError, 'full_revision_required'):
                self.stage(revision=revision)
        self.assertFalse((self.root / '_site').exists())

    def test_missing_allowlisted_file_and_missing_image_fail_before_creation(self):
        path = self.root / 'data' / 'store-insights.js'
        body = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(pages.PagesError, 'missing_public_file'):
            self.stage()
        path.write_bytes(body)
        (self.root / 'assets' / 'events' / 'flower.svg').unlink()
        with self.assertRaisesRegex(pages.PagesError, 'missing_referenced_asset'):
            self.stage()
        self.assertFalse((self.root / '_site').exists())

    def test_index_references_are_checked_not_copied_as_additional_permissions(self):
        for fragment in (
            '<script src="private.js"></script>', '<img src="../secret.txt">',
            '<img src="data/../app.js">', '<img src="%2e%2e/private.txt">',
            '<img src="/app.js">', '<script src="//evil.invalid/a.js"></script>',
            '<script src="https://evil.invalid/a.js"></script>',
            '<img src="C:\\private\\file.svg">', '<base href="https://evil.invalid/">',
            '<img srcset="assets/events/flower.svg 1x, missing.svg 2x">',
            '<img srcset="assets/events/flower.svg 1x,">',
            '<style>body{background:url(secret.txt)}</style>',
        ):
            with self.subTest(fragment=fragment):
                self.write('index.html', fragment.encode())
                self.write('private.js', b'private')
                with self.assertRaises(pages.PagesError):
                    self.stage()
                self.assertFalse((self.root / '_site').exists())

    def test_schedule_image_and_css_references_must_exist(self):
        for name, body in (
            ('data/schedule.js', b'const image = "assets/events/missing.svg";'),
            ('styles.css', b'body{background:url("missing.svg")}'),
        ):
            with self.subTest(name=name):
                path = self.root.joinpath(*name.split('/'))
                original = path.read_bytes()
                path.write_bytes(body)
                with self.assertRaisesRegex(pages.PagesError, 'missing_referenced_asset'):
                    self.stage()
                path.write_bytes(original)

    def test_source_symlink_even_inside_root_is_rejected(self):
        path = self.root / 'app.js'
        path.unlink()
        self.symlink(path, self.root / 'styles.css')
        with self.assertRaisesRegex(pages.PagesError, 'unsafe_path'):
            self.stage()
        self.assertFalse((self.root / '_site').exists())

    def test_directory_symlink_escape_and_output_symlink_are_rejected(self):
        outside = self.work / 'outside'
        outside.mkdir()
        (outside / 'private.svg').write_text(SECRET, encoding='utf-8')
        source = self.root / 'assets' / 'events' / 'private.svg'
        self.symlink(source, outside / 'private.svg')
        with self.assertRaisesRegex(pages.PagesError, 'unsafe_path'):
            self.stage()
        source.unlink()
        link = self.root / 'linked'
        self.symlink(link, outside, directory=True)
        with self.assertRaisesRegex(pages.PagesError, 'unsafe_path'):
            self.stage(link / 'artifact')
        self.assertFalse((outside / 'artifact').exists())

    def test_source_and_destination_link_metadata_rejected_without_link_privileges(self):
        original = Path.lstat
        for source, is_reparse in (
            (self.root / 'app.js', False),
            (self.root / 'assets', True),
            (self.root / 'data', True),
            (self.root / 'linked', True),
        ):
            with self.subTest(source=source, reparse=is_reparse):
                def linked(path, *args, **kwargs):
                    if path == source:
                        return SimpleNamespace(
                            st_mode=stat.S_IFDIR if is_reparse else stat.S_IFLNK,
                            st_file_attributes=0x400 if is_reparse else 0)
                    return original(path, *args, **kwargs)
                target = source / 'artifact' if source.name == 'linked' else self.root / '_site'
                with mock.patch.object(Path, 'lstat', linked):
                    with self.assertRaisesRegex(pages.PagesError, 'unsafe_path'):
                        self.stage(target)
        self.assertFalse((self.root / '_site').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_real_windows_junction_escape_is_rejected(self):
        import _winapi
        outside = self.work / 'junction-target'
        outside.mkdir()
        link = self.root / 'junction'
        _winapi.CreateJunction(str(outside), str(link))
        with self.assertRaisesRegex(pages.PagesError, 'unsafe_path'):
            self.stage(link / 'artifact')
        self.assertEqual(list(outside.iterdir()), [])

    def test_artifact_fileset_is_verified_after_copy(self):
        original = pages._artifact_files

        def contaminate(output):
            (output / 'private.txt').write_text(SECRET, encoding='utf-8')
            return original(output)

        with mock.patch.object(pages, '_artifact_files', side_effect=contaminate):
            with self.assertRaisesRegex(pages.PagesError, 'artifact_verification_failed'):
                self.stage()

    def test_publication_bytes_not_private_snapshot_are_hashed(self):
        state = snapshot()
        state['privatePath'] = SECRET
        source = self.write('data/observed-shifts.json', pages.json_bytes(state))
        manifest = self.stage()
        published = self.root / '_site' / 'data' / 'observed-shifts.json'
        self.assertNotEqual(source.read_bytes(), published.read_bytes())
        self.assertEqual(manifest['files']['data/observed-shifts.json']['rawSHA256'],
                         hashlib.sha256(published.read_bytes()).hexdigest())

    def test_current_frontend_stages_with_actual_snapshot_and_image_references(self):
        output = self.work / 'current-site'
        source_names = [*pages.PUBLIC_FILES,
                        *(path.relative_to(ROOT).as_posix()
                          for path in (ROOT / 'assets' / 'events').glob('*.svg'))]
        before = {name: ROOT.joinpath(*name.split('/')).read_bytes() for name in source_names}
        manifest = pages.stage(output, SHA, clock=lambda: NOW)
        self.assertEqual(set(manifest['files']), set(source_names))
        self.assertEqual(set(self.tree_bytes(output)), set(source_names) | {'version.json'})
        self.assertEqual({name: ROOT.joinpath(*name.split('/')).read_bytes()
                          for name in source_names}, before)


class ProbeTests(WorkspaceTests):
    def probe(self, opener=None, **kwargs):
        opener = opener or FakeOpener()
        with mock.patch.object(collector.urllib.request, 'build_opener', return_value=opener):
            result = pages.probe(self.root / 'probe.json', root=self.root,
                                 clock=lambda: NOW, sleep=lambda _: None, **kwargs)
        persisted = json.loads((self.root / 'probe.json').read_text('utf-8'))
        self.assertEqual(persisted, result)
        return result, opener

    def assert_private_absent(self, result):
        text = json.dumps(result, ensure_ascii=False)
        for value in (SECRET, 'private-person', str(self.root), str(self.work), 'snapshotPath',
                      'processId', 'transportStatePaths', 'publishPath', 'full_text',
                      'rawText', 'cookie.txt'):
            self.assertNotIn(value, text)

    def test_real_collector_dryrun_starts_empty_and_keeps_curated_skip(self):
        state = snapshot()
        state['posts'] = [fact()]
        self.write('data/observed-shifts.json', pages.json_bytes(state))
        self.write('data/observed-shifts.http-state.json', b'not even valid JSON')
        self.write('data/observed-shifts.lock', b'private lock')
        before = self.tree_bytes()
        opener = FakeOpener(searches=[[TID, CURATED], [TID]])
        original_collect = collector.collect
        original_run = collector.run
        observed = {}

        def inspect_collect(state, known, client, start, end, maximum, *args, **kwargs):
            observed.update(state=copy.deepcopy(state), known=known, start=start,
                            end=end, maximum=maximum)
            return original_collect(state, known, client, start, end, maximum, *args, **kwargs)

        def inspect_run(args, **kwargs):
            self.assertTrue(sys.dont_write_bytecode)
            self.assertTrue(args.dry_run)
            self.assertTrue(args.once)
            self.assertFalse(args.watch)
            self.assertEqual((args.days, args.max_posts), (2, 20))
            self.assertIsNone(args.report)
            self.assertIsNone(args.publish)
            self.assertFalse(args.snapshot.exists())
            self.assertEqual(list(args.snapshot.parent.iterdir()), [])
            self.assertTrue(args.snapshot.parent.is_relative_to(self.root))
            return original_run(args, **kwargs)

        with mock.patch.object(collector, 'collect', side_effect=inspect_collect), \
                mock.patch.object(collector, 'run', side_effect=inspect_run):
            result, _ = self.probe(opener)
        self.assertEqual(result['exitCode'], 0)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['sourceCount'], 2)
        self.assertEqual(result['newPostCount'], 1)
        self.assertEqual(result['verifiedPostCount'], 1)
        self.assertEqual(result['newFacts'], [fact()])
        self.assertEqual(result['skippedCuratedCount'], 1)
        self.assertEqual(observed['state']['posts'], [])
        self.assertEqual(observed['state']['pending'], [])
        self.assertEqual(observed['state']['resolved'], [])
        self.assertEqual(observed['state']['cooldowns'], {})
        self.assertEqual(observed['known'], {CURATED})
        self.assertEqual((observed['start'], observed['end'], observed['maximum']),
                         (dt.date(2026, 9, 4), dt.date(2026, 9, 5), 20))
        self.assertEqual(result['http'], {
            'search': {'getCount': 2, 'statuses': [200, 200]},
            'syndication': {'getCount': 1, 'statuses': [200]},
        })
        self.assertEqual(len(opener.calls), 3)
        for request in opener.calls:
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.header_items(), [])
        after = self.tree_bytes()
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertEqual(set(after) - set(before), {'probe.json'})
        self.assertFalse(list(self.root.glob('.pages-probe-*')))
        self.assert_private_absent(result)

    def test_no_new_curated_only_is_not_success_and_no_individual_get(self):
        result, opener = self.probe(FakeOpener(searches=[[CURATED], [CURATED]]))
        self.assertEqual(result['status'], 'no-new')
        self.assertEqual(result['exitCode'], 2)
        self.assertEqual(result['newFacts'], [])
        self.assertEqual(result['http']['syndication']['getCount'], 0)
        self.assertEqual(len(opener.calls), 2)

    def test_no_results_is_not_success(self):
        result, _ = self.probe(FakeOpener(searches=[[], []]))
        self.assertEqual(result['status'], 'no-results')
        self.assertEqual(result['exitCode'], 2)
        self.assertEqual(result['verifiedPostCount'], 0)

    def test_individual_get_without_duty_is_not_success(self):
        result, _ = self.probe(FakeOpener(posts={TID: payload(text='新人にゃんこのお知らせ')}))
        self.assertEqual(result['status'], 'no-new')
        self.assertEqual(result['exitCode'], 2)
        self.assertEqual(result['http']['syndication']['statuses'], [200])
        self.assertEqual(result['newFacts'], [])
        self.assertNotIn(TID, json.dumps(result))

    def test_individual_author_id_date_duty_and_name_validation_required(self):
        invalid = []
        for change in (
            {'user': {'id_str': '123456789012345678', 'screen_name': collector.AUTHOR}},
            {'user': {'id_str': collector.AUTHOR_ID, 'screen_name': 'other'}},
            {'id_str': OTHER}, {'created_at': '2026-09-04T09:17:05Z'},
            {'text': 'よるにゃんこ\n\nあむ\n'},
            {'text': '【アキバ絶対領域】\nよるにゃんこ\n\n' + SECRET},
        ):
            value = payload()
            value.update(change)
            invalid.append(value)
        for index, value in enumerate(invalid):
            with self.subTest(index=index):
                result, _ = self.probe(FakeOpener(posts={TID: value}))
                self.assertNotEqual(result['exitCode'], 0)
                self.assertEqual(result['newFacts'], [])
                self.assertEqual(result['verifiedPostCount'], 0)
                self.assertNotIn(TID, json.dumps(result))
                self.assert_private_absent(result)
                (self.root / 'probe.json').unlink()

    def test_discovery_only_claim_of_new_facts_cannot_succeed_or_leak_id(self):
        original_write = collector.write_report

        def forge(report, destination):
            report.update(status='ok', newFacts=[fact()], newPostCount=1, newNameCount=2)
            original_write(report, destination)

        with mock.patch.object(collector, 'write_report', side_effect=forge):
            result, _ = self.probe(FakeOpener(searches=[[], []]))
        self.assertEqual(result['exitCode'], 4)
        self.assertEqual(result['reason'], 'unverified_new_facts')
        self.assertEqual(result['newFacts'], [])
        self.assertNotIn(TID, json.dumps(result))

    def test_both_sources_required_even_with_verified_new_fact(self):
        failure = urllib.error.URLError(SECRET)
        result, _ = self.probe(FakeOpener(searches=[[TID], failure]))
        self.assertEqual(result['sourceCount'], 1)
        self.assertEqual(result['newPostCount'], 1)
        self.assertEqual(result['verifiedPostCount'], 1)
        self.assertEqual(result['exitCode'], 2)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['errors'], [{'reason': 'network_error'}])
        self.assert_private_absent(result)

    def test_search_403_and_429_stop_same_host_without_retry(self):
        for code in (403, 429):
            with self.subTest(code=code):
                failure = urllib.error.HTTPError(
                    collector.SEARCH_URLS[0], code, SECRET, {'Retry-After': '120'}, io.BytesIO())
                result, opener = self.probe(FakeOpener(searches=[failure, [TID]]))
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(result['http']['search'], {'getCount': 1, 'statuses': [code]})
                self.assertEqual(result['http']['syndication']['getCount'], 0)
                self.assertEqual(result['sourceCount'], 0)
                self.assertEqual(result['exitCode'], 3)
                self.assertEqual(result['newFacts'], [])
                self.assertIn({'reason': 'http_error', 'httpStatus': code,
                               'retryAt': '2026-09-05T18:52:00Z'}, result['errors'])
                self.assertIn({'reason': 'host_rate_limited', 'retryAt': '2026-09-05T18:52:00Z'},
                              result['errors'])
                self.assert_private_absent(result)
                (self.root / 'probe.json').unlink()

    def test_syndication_403_and_429_stop_remaining_posts(self):
        for code in (403, 429):
            with self.subTest(code=code):
                failure = urllib.error.HTTPError(
                    'https://' + collector.POST_HOST, code, SECRET, {}, io.BytesIO())
                result, opener = self.probe(FakeOpener(
                    searches=[[TID, OTHER], [TID]],
                    posts={TID: failure, OTHER: payload(OTHER, '2026-09-05T08:00:00Z')}))
                self.assertEqual(len(opener.calls), 3)
                self.assertEqual(result['http']['syndication'], {'getCount': 1, 'statuses': [code]})
                self.assertEqual(result['sourceCount'], 2)
                self.assertEqual(result['attemptedCount'], 1)
                self.assertEqual(result['deferredCount'], 1)
                self.assertEqual(result['newFacts'], [])
                self.assertEqual(result['exitCode'], 2)
                self.assertIn({'reason': 'http_error', 'httpStatus': code,
                               'retryAt': '2026-09-05T19:50:00Z'}, result['errors'])
                self.assertNotIn(TID, json.dumps(result))
                self.assertNotIn(OTHER, json.dumps(result))
                self.assert_private_absent(result)
                (self.root / 'probe.json').unlink()

    def test_stale_http_200_is_not_evidence_of_verified_fact(self):
        response = FakeResponse(json.dumps(payload()).encode(), headers={'Age': '99999'})
        result, _ = self.probe(FakeOpener(posts={TID: response}))
        self.assertEqual(result['http']['syndication']['statuses'], [200])
        self.assertEqual(result['newFacts'], [])
        self.assertNotEqual(result['exitCode'], 0)
        self.assertEqual(result['errors'], [{'reason': 'stale_http_cache'}])

    def test_post_cap_is_twenty_no_unbounded_retry(self):
        ids = [make_id(f'2026-09-05T09:{minute:02d}:00Z') for minute in range(21)]
        posts = {tid: payload(tid, f'2026-09-05T09:{minute:02d}:00Z')
                 for minute, tid in enumerate(ids)}
        result, opener = self.probe(FakeOpener(searches=[ids, []], posts=posts))
        self.assertEqual(result['http']['syndication']['getCount'], 20)
        self.assertEqual(len(opener.calls), 22)
        self.assertEqual(result['newPostCount'], 20)
        self.assertEqual(result['deferredCount'], 1)
        self.assertEqual(result['exitCode'], 2)

    def test_raw_cli_stdout_and_unknown_error_details_are_never_printed(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        original_write = collector.write_report

        def add_private(report, destination):
            report.update(rawText=SECRET, secret=SECRET, lease=SECRET)
            report['failures'] = [{'reason': SECRET, 'id': OTHER, 'url': SECRET,
                                   'retryAt': SECRET, 'httpStatus': SECRET}]
            original_write(report, destination)

        with mock.patch.object(collector, 'write_report', side_effect=add_private), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result, _ = self.probe()
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(result['errors'], [{'reason': 'collection_failed'}])
        self.assert_private_absent(result)

    def test_safe_report_on_local_failure_and_no_real_http(self):
        with mock.patch.object(collector, 'curated_ids', side_effect=OSError(SECRET)):
            result, opener = self.probe()
        self.assertEqual(result['exitCode'], 4)
        self.assertEqual(result['reason'], 'local_io_error')
        self.assertEqual(opener.calls, [])
        self.assert_private_absent(result)
        self.assertFalse(list(self.root.glob('.pages-probe-*')))

    def test_unexpected_collector_exception_is_a_safe_nonzero_report(self):
        with mock.patch.object(collector, 'run', side_effect=RuntimeError(SECRET)):
            result, _ = self.probe()
        self.assertEqual(result['exitCode'], 4)
        self.assertEqual(result['reason'], 'probe_failed')
        self.assert_private_absent(result)
        self.assertFalse(list(self.root.glob('.pages-probe-*')))

    def test_report_destination_preflight_prevents_network_and_overwrite(self):
        existing = self.root / 'existing.json'
        existing.write_text('keep', encoding='utf-8')
        opener = FakeOpener()
        with mock.patch.object(collector.urllib.request, 'build_opener', return_value=opener):
            for path in (existing, self.root / 'data' / 'public.json',
                         self.work / 'outside.json', self.root / 'bad.txt'):
                with self.subTest(path=path), self.assertRaises(pages.PagesError):
                    pages.probe(path, root=self.root, clock=lambda: NOW, sleep=lambda _: None)
        self.assertEqual(existing.read_text('utf-8'), 'keep')
        self.assertEqual(opener.calls, [])

    def test_cli_emits_only_sanitized_json_and_propagates_exit(self):
        for status, code in (('ok', 0), ('no-new', 2), ('unavailable', 3)):
            result = {'status': status, 'exitCode': code, 'newFacts': []}
            stdout = io.StringIO()
            with self.subTest(status=status), \
                    mock.patch.object(pages, 'probe', return_value=result) as run, \
                    contextlib.redirect_stdout(stdout):
                returned = pages.main(['probe', '--report', str(self.root / 'probe.json')])
            self.assertEqual(returned, code)
            self.assertEqual(json.loads(stdout.getvalue()), result)
            run.assert_called_once()
        stdout = io.StringIO()
        with mock.patch.object(pages, 'stage', side_effect=OSError(SECRET)), \
                contextlib.redirect_stdout(stdout):
            code = pages.main(['stage', '--output', str(self.root / 'artifact'), '--revision', SHA])
        self.assertEqual(code, 4)
        self.assertNotIn('private-person', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
