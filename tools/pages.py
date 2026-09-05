"""Allowlisted Pages artifacts and a one-shot, unseeded public HTTP probe.

stage --output NEW_DIRECTORY --revision FULL_SHA
probe --report NEW_JSON_FILE

Destinations must be new paths inside this checkout, outside source directories.
Probe exit codes: 0=verified new facts, 2=insufficient/partial, 3=unavailable,
4=local or invalid-data error. No-new/no-results are deliberately not successes.
Version hashes cover exact published bytes (excluding version.json itself), not
the frontend's newline-normalized cache-busting hashes.
"""
import argparse
import contextlib
import datetime as dt
from functools import lru_cache
import hashlib
from html.parser import HTMLParser
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time
import urllib.error
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    'index.html', 'app.js', 'styles.css', 'data/schedule.js',
    'data/store-insights.js', 'data/observed-shifts.json',
)
POST_FIELDS = (
    'id', 'url', 'authorId', 'authorScreenName', 'createdAt', 'date',
    'shift', 'storeId', 'names', 'observedAt',
)
PUBLIC_COUNTS = ('sourceCount', 'newPostCount', 'newNameCount')
PROBE_COUNTS = PUBLIC_COUNTS + (
    'discoveredCount', 'eligibleCount', 'attemptedCount', 'fetchedCount',
    'skippedCuratedCount', 'deferredCount', 'pendingCount',
)
PROTECTED_DIRS = {'.git', '.github', 'tools', 'data', 'assets', 'node_modules'}
NAME_RE = re.compile(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}\Z')
SVG_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*\.svg\Z')
SAFE_REASONS = {
    'http_error', 'host_rate_limited', 'network_error', 'route_refused',
    'redirect_refused', 'unexpected_http_status', 'invalid_http_metadata',
    'future_http_date', 'stale_http_cache', 'response_too_large',
    'invalid_search_response', 'unrecognized_search_page', 'invalid_post_json',
    'response_id_mismatch', 'author_mismatch', 'invalid_created_at',
    'future_post', 'outside_date_range', 'id_timestamp_mismatch',
    'invalid_post_id', 'missing_post_text', 'missing_store_header',
    'parse_failed', 'parsed_metadata_mismatch',
}


class PagesError(ValueError):
    """A fixed, public-safe failure code, never an exception's local details."""


@contextlib.contextmanager
def _no_bytecode():
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        yield
    finally:
        sys.dont_write_bytecode = previous


@lru_cache(maxsize=1)
def load_collector():
    with _no_bytecode():
        source = _source_file(ROOT, 'tools/collect-shifts.py')
        _source_file(ROOT, 'tools/add-shifts.py')
        spec = importlib.util.spec_from_file_location(
            'pages_collect_shifts', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
            + '\n').encode('utf-8')


def _timestamp(value, collector, nullable=False):
    if value is None and nullable:
        return None
    return collector.iso(collector.timestamp(value))


def _date(value):
    if (not isinstance(value, str)
            or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)):
        raise ValueError
    return dt.date.fromisoformat(value).isoformat()


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def public_projection(state, *, collector=None):
    """Whitelist a snapshot dict; reject invalid facts, never repair names.

    For files, use load_public_snapshot(), which also runs load_snapshot's
    private-state validation. Unknown fields never cross the public boundary.
    """
    collector = collector or load_collector()
    try:
        if (not isinstance(state, dict) or type(state['schemaVersion']) is not int
                or state['schemaVersion'] != 1 or state['complete'] is not False
                or not isinstance(state['posts'], list)
                or not isinstance(state['pending'], list)
                or not isinstance(state['lastRun'], dict)):
            raise ValueError
        result = {
            'schemaVersion': 1, 'complete': False,
            'checkedAt': _timestamp(state['checkedAt'], collector, nullable=True),
            'lastSuccessAt': _timestamp(state['lastSuccessAt'], collector, nullable=True),
            'posts': [], 'pending': [],
        }
        ids = set()
        for post in state['posts']:
            if not isinstance(post, dict):
                raise ValueError
            item = {field: post[field] for field in POST_FIELDS}
            tid = item['id']
            if (not isinstance(tid, str) or not collector.post_id(tid) or tid in ids
                    or item['authorId'] != collector.AUTHOR_ID
                    or item['authorScreenName'] != collector.AUTHOR
                    or item['url'] != collector.canonical(tid)
                    or item['storeId'] not in collector.STORE_IDS.values()
                    or item['shift'] not in ('昼', '夜')):
                raise ValueError
            created = collector.timestamp(item['createdAt'])
            observed = collector.timestamp(item['observedAt'])
            if (_date(item['date']) != collector.service_day(created).isoformat()
                    or abs((collector.snowflake_time(tid) - created).total_seconds()) >= 2
                    or observed < created):
                raise ValueError
            names = item['names']
            if (not isinstance(names, list) or not names
                    or any(not isinstance(name, str) or not NAME_RE.fullmatch(name)
                           or name.startswith('http') or 'にゃんこ' in name
                           or (name not in collector.IMPORTER.CONFIRMED_NAMES
                               and re.search(r'(にゃん(ね|こ)?|です|ます|だよ|でした)$', name))
                           for name in names)
                    or len(set(names)) != len(names)):
                raise ValueError
            item['createdAt'] = collector.iso(created)
            item['observedAt'] = collector.iso(observed)
            item['names'] = list(names)
            ids.add(tid)
            result['posts'].append(item)
        run = state['lastRun']
        if not isinstance(run['status'], str) or run['status'] not in collector.STATUSES:
            raise ValueError
        start, end = run.get('dateFrom'), run.get('dateTo')
        if start is not None or end is not None:
            start, end = _date(start), _date(end)
            if start > end:
                raise ValueError
        public_run = {'status': run['status'], 'dateFrom': start, 'dateTo': end}
        for field in PUBLIC_COUNTS:
            if field in run:
                public_run[field] = _count(run[field])
        if (public_run.get('sourceCount', 0) > len(collector.SEARCH_URLS)
                or public_run.get('newPostCount', 0) > len(result['posts'])
                or public_run.get('newNameCount', 0)
                > sum(len(post['names']) for post in result['posts'])):
            raise ValueError
        result['lastRun'] = public_run
        return result
    except (KeyError, ValueError, TypeError, OverflowError, OSError, AttributeError):
        raise PagesError('invalid_public_snapshot') from None


def load_public_snapshot(path, *, collector=None):
    collector = collector or load_collector()
    try:
        return public_projection(collector.load_snapshot(Path(path)), collector=collector)
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        raise PagesError('invalid_public_snapshot') from None


def _plain_path(path):
    """Reject symlinks, Windows junctions/reparse points, and special files."""
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
                or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
            raise PagesError('unsafe_path')
    return path


def _source_file(root, name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in ('.', '..') for part in relative.parts):
        raise PagesError('unsafe_path')
    path = _plain_path(root.joinpath(*relative.parts))
    if not path.is_file() or not path.is_relative_to(root):
        raise PagesError('missing_public_file')
    return path


def _destination(root, path, *, directory):
    root = _plain_path(root)
    raw = Path(path)
    parts = raw.parts[1:] if raw.anchor else raw.parts
    if any(re.search(r'[<>:"|?*\x00-\x1f]', part)
           or part.rstrip(' .') != part
           or re.fullmatch(r'(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', part)
           for part in parts):
        raise PagesError('unsafe_output')
    path = _plain_path(path)
    if path == root or not path.is_relative_to(root):
        raise PagesError('unsafe_output')
    relative = path.relative_to(root)
    if (relative.parts[0].casefold() in PROTECTED_DIRS
            or '.git' in (part.casefold() for part in relative.parts)):
        raise PagesError('unsafe_output')
    for parent in path.parents:
        if parent == root:
            break
        if (parent / '.git').exists():
            raise PagesError('unsafe_output')
    if path.exists() or not path.parent.is_dir():
        raise PagesError('output_must_be_new')
    if not directory and path.suffix.lower() != '.json':
        raise PagesError('json_report_required')
    return path


def _asset_reference(value, files, *, external_link=False):
    if (not isinstance(value, str) or '\\' in value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise PagesError('unsafe_asset_reference')
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if external_link and parsed.scheme in ('https', 'http') and parsed.netloc:
            return
        raise PagesError('unsafe_asset_reference')
    if not parsed.path:
        if external_link and parsed.fragment:
            return
        raise PagesError('unsafe_asset_reference')
    name = unquote(parsed.path)
    if (name.startswith('/') or '\\' in name or '%' in name or ':' in name
            or any(part in ('', '.', '..') for part in name.split('/'))
            or any(ord(char) <= 32 or ord(char) == 127 for char in name)):
        raise PagesError('unsafe_asset_reference')
    if name not in files:
        raise PagesError('missing_referenced_asset')


class _IndexAssets(HTMLParser):
    def __init__(self, files):
        super().__init__(convert_charrefs=True)
        self.files = files

    def handle_starttag(self, tag, attrs):
        if tag == 'base':
            raise PagesError('unsafe_asset_reference')
        for name, value in attrs:
            if name in ('src', 'href', 'poster', 'data', 'action'):
                _asset_reference(value, self.files, external_link=tag == 'a' and name == 'href')
            elif name == 'srcset':
                if not value:
                    raise PagesError('unsafe_asset_reference')
                for candidate in value.split(','):
                    parts = candidate.strip().split()
                    if not parts:
                        raise PagesError('unsafe_asset_reference')
                    _asset_reference(parts[0], self.files)

    handle_startendtag = handle_starttag


def _check_references(files):
    parser = _IndexAssets(files)
    parser.feed(files['index.html'].decode('utf-8-sig'))
    parser.close()
    for name in ('index.html', 'styles.css', 'app.js', 'data/schedule.js',
                 'data/store-insights.js'):
        text = files[name].decode('utf-8-sig')
        for match in re.finditer(r"""assets/events/[^"'\s`<>\\)]+""", text):
            _asset_reference(match.group(), files)
        if name in ('index.html', 'styles.css'):
            for match in re.finditer(r'url\(\s*([^)]+?)\s*\)', text, re.IGNORECASE):
                _asset_reference(match.group(1).strip('"\''), files)
            for match in re.finditer(r"""@import\s+["']([^"']+)["']""", text, re.IGNORECASE):
                _asset_reference(match.group(1), files)


def _artifact_files(output):
    result = set()
    for directory, dirs, files in os.walk(output, followlinks=False):
        for name in (*dirs, *files):
            _plain_path(Path(directory) / name)
        for name in files:
            result.add((Path(directory) / name).relative_to(output).as_posix())
    return result


def stage(output, revision, *, root=ROOT, clock=None):
    """Return version.json's manifest after writing an exclusively new artifact."""
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', revision):
        raise PagesError('full_revision_required')
    root = _plain_path(root)
    output = _destination(root, output, directory=True)
    files = {}
    for name in PUBLIC_FILES:
        source = _source_file(root, name)
        if name == 'data/observed-shifts.json':
            observation = load_public_snapshot(source)
            files[name] = json_bytes(observation)
        else:
            files[name] = source.read_bytes()
    events = _plain_path(root / 'assets' / 'events')
    if not events.is_dir():
        raise PagesError('missing_public_file')
    for source in sorted(events.glob('*.svg')):
        if not SVG_RE.fullmatch(source.name):
            raise PagesError('unsafe_asset_name')
        name = 'assets/events/' + source.name
        files[name] = _source_file(root, name).read_bytes()
    _check_references(files)
    collector = load_collector()
    manifest = {
        'revision': revision.lower(), 'sourceSha': revision.lower(),
        'buildtime': collector.iso((clock or collector.utc_now)()),
        'observation': {'checkedAt': observation['checkedAt']},
        'files': {name: {'rawSHA256': hashlib.sha256(body).hexdigest()}
                  for name, body in sorted(files.items())},
    }
    files['version.json'] = json_bytes(manifest)
    output.mkdir()
    for name, body in sorted(files.items()):
        destination = output.joinpath(*PurePosixPath(name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _plain_path(destination)
        with destination.open('xb') as target:
            target.write(body)
    if (_artifact_files(output) != set(files)
            or any(output.joinpath(*PurePosixPath(name).parts).read_bytes() != body
                   for name, body in files.items())):
        raise PagesError('artifact_verification_failed')
    return manifest


class _HTTPTrace:
    def __init__(self, opener, collector):
        self.opener = opener
        self.collector = collector
        self.counts = {'search': {'getCount': 0, 'statuses': []},
                       'syndication': {'getCount': 0, 'statuses': []}}
        self.successful_posts = set()
        self.successful_sources = set()

    def open(self, request, timeout=45):
        url = request.full_url
        kind = 'search' if url in self.collector.SEARCH_URLS else 'syndication'
        if request.get_method() != 'GET':
            raise self.collector.FetchFailure('route_refused')
        evidence = self.counts[kind]
        evidence['getCount'] += 1
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if type(exc.code) is int and 100 <= exc.code <= 599:
                evidence['statuses'].append(exc.code)
            raise
        code = response.getcode()
        if type(code) is int and 100 <= code <= 599:
            evidence['statuses'].append(code)
        if code == 200:
            if kind == 'search':
                self.successful_sources.add(url)
            else:
                tid = parse_qs(urlsplit(url).query).get('id', [None])[0]
                if self.collector.post_id(tid):
                    self.successful_posts.add(tid)
        return response


def _safe_errors(report, collector):
    errors = []
    for entry in [*report.get('sources', []), *report.get('failures', [])]:
        if not isinstance(entry, dict) or 'reason' not in entry:
            continue
        reason = entry['reason']
        safe = {'reason': reason if isinstance(reason, str) and reason in SAFE_REASONS
                else 'collection_failed'}
        code = entry.get('httpStatus')
        if type(code) is int and 100 <= code <= 599:
            safe['httpStatus'] = code
        if entry.get('retryAt') is not None:
            try:
                safe['retryAt'] = _timestamp(entry['retryAt'], collector)
            except (TypeError, ValueError, OverflowError):
                pass
        if safe not in errors:
            errors.append(safe)
    return errors


def _probe_report(raw, code, trace, collector):
    state = {
        'schemaVersion': 1, 'complete': False,
        'checkedAt': raw['checkedAt'], 'lastSuccessAt': raw['lastSuccessAt'],
        'posts': raw['newFacts'], 'pending': [], 'lastRun': raw,
    }
    public = public_projection(state, collector=collector)
    counts = {field: _count(raw[field]) for field in PROBE_COUNTS}
    facts = [post for post in public['posts'] if post['id'] in trace.successful_posts]
    consistent = (counts['newPostCount'] == len(facts) == len(public['posts'])
                  and counts['newNameCount'] == sum(len(post['names']) for post in facts))
    report = {
        **counts, 'status': public['lastRun']['status'],
        'newPostCount': len(facts),
        'newNameCount': sum(len(post['names']) for post in facts),
        'verifiedPostCount': len(facts), 'newFacts': facts,
        'http': trace.counts, 'errors': _safe_errors(raw, collector),
    }
    success = (code == 0 and report['status'] == 'ok' and consistent
               and counts['sourceCount'] == 2 and len(trace.successful_sources) == 2
               and len(facts) > 0 and trace.counts['syndication']['getCount'] > 0
               and not report['errors'] and counts['pendingCount'] == 0
               and counts['deferredCount'] == 0)
    if success:
        report.update(reason='verified_new_facts', exitCode=0)
    elif not consistent:
        report.update(status='unavailable', reason='unverified_new_facts', exitCode=4)
    elif report['status'] == 'unavailable':
        report.update(reason='collection_unavailable', exitCode=3)
    elif (report['status'] == 'partial' or counts['sourceCount'] != 2
          or report['errors'] or counts['pendingCount'] or counts['deferredCount']):
        report.update(reason='incomplete_collection', exitCode=2)
    else:
        report.update(reason='no_verified_new_facts', exitCode=2)
    return report


def probe(report_path, *, root=ROOT, clock=None, sleep=time.sleep):
    """Run the real collector transport once; tests replace only its HTTP opener."""
    root = _plain_path(root)
    destination = _destination(root, report_path, directory=False)
    collector = load_collector()
    clock = clock or collector.utc_now
    report = {'status': 'unavailable', 'reason': 'local_io_error', 'exitCode': 4,
              'newFacts': [], 'sourceCount': 0, 'newPostCount': 0,
              'verifiedPostCount': 0,
              'http': {'search': {'getCount': 0, 'statuses': []},
                       'syndication': {'getCount': 0, 'statuses': []}}}
    with destination.open('xb') as target:
        try:
            curated = _source_file(root, 'tools/data/shifts.csv')
            # Keep all ephemeral state in this checkout, never the OS temp area.
            with tempfile.TemporaryDirectory(prefix='.pages-probe-', dir=root) as directory:
                snapshot = Path(directory) / 'observed-shifts.json'
                args = collector.argument_parser().parse_args([
                    '--dry-run', '--days', '2', '--max-posts', '20', '--once',
                    '--snapshot', str(snapshot),
                ])
                captured = io.StringIO()
                with _no_bytecode(), contextlib.redirect_stdout(captured), \
                        contextlib.redirect_stderr(io.StringIO()):
                    client = collector.PublicClient(clock=clock, sleep=sleep)
                    trace = _HTTPTrace(client.opener, collector)
                    client.opener = trace
                    report['http'] = trace.counts
                    code = collector.run(args, curated=curated, client=client,
                                         clock=clock, sleep=sleep)
                raw = json.loads(captured.getvalue())
                report = _probe_report(raw, code, trace, collector)
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            report.update(status='unavailable', reason='invalid_probe_result', exitCode=4)
        except OSError:
            report.update(status='unavailable', reason='local_io_error', exitCode=4)
        except Exception:
            report.update(status='unavailable', reason='probe_failed', exitCode=4)
        target.write(json_bytes(report))
    return report


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    staging = subparsers.add_parser('stage', help='write a new, allowlisted Pages artifact')
    staging.add_argument('--output', type=Path, required=True)
    staging.add_argument('--revision', required=True, help='40-hex source commit SHA')
    probing = subparsers.add_parser('probe', help='one unseeded public HTTP dry-run')
    probing.add_argument('--report', type=Path, required=True, help='new JSON report inside checkout')
    return parser


def main(argv=None):
    args = argument_parser().parse_args(argv)
    try:
        if args.command == 'stage':
            result = stage(args.output, args.revision)
            code = 0
        else:
            result = probe(args.report)
            code = result['exitCode']
    except PagesError as exc:
        result, code = {'status': 'unavailable', 'reason': str(exc), 'exitCode': 4}, 4
    except (OSError, UnicodeError, ValueError):
        result, code = {'status': 'unavailable', 'reason': 'invalid_local_data', 'exitCode': 4}, 4
    except Exception:
        result, code = {'status': 'unavailable', 'reason': 'pages_failed', 'exitCode': 4}, 4
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
