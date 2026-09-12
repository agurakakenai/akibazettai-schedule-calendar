"""Durable source reservations. Initialization is an explicit data-only migration.

The cloud owner holds the publication lease; this lock serializes local children.
A reservation is spent even if no HTTP was issued. Only hashes, counters and
transport metadata belong here, never source text, images or request URLs.
"""
import copy
from collections import OrderedDict
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import urllib.parse
import uuid


SPEC = importlib.util.spec_from_file_location('source_usage_validation',
                                             Path(__file__).with_name('analysis-state.py'))
usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage)
KINDS = ('searches', 'posts', 'images')
COMPONENTS = ('official', 'personal', 'schedule')
HOSTS = ('search.yahoo.co.jp', 'cdn.syndication.twimg.com', 'pbs.twimg.com')
HOST_KIND = dict(zip(HOSTS, KINDS))
JST = usage.JST
DAY_INDIVIDUAL_LIMIT = 40


class SourceFailure(Exception):
    def __init__(self, reason, status=None, retry_at=None):
        super().__init__(reason)
        self.reason, self.status, self.retry_at = reason, status, retry_at

    def facts(self):
        result = {'reason': self.reason}
        if self.status is not None:
            result['httpStatus'] = self.status
        if self.retry_at is not None:
            result['retryAt'] = self.retry_at
        return result


class TransientSourceCache:
    """One process/run, opt-in; share this object across sequential components.

    The orchestrator owns its context and must not add this cache on top of an
    already-full official raw analysis buffer. No disk/IPC serialization is
    provided. Uncached/evicted reservations still cannot be requested again.
    """
    POST_LIMIT = 3
    SEARCH_LIMIT = 5
    BODY_LIMIT = 8 * 1024 * 1024
    IMAGE_LIMIT = 12 * 1024 * 1024
    CONTENT_TYPES = (None, 'application/json', 'text/html', 'image/jpeg', 'image/png')

    def __init__(self, run_id):
        usage._token(run_id)
        self.run_id = run_id
        self._items = {kind: OrderedDict() for kind in KINDS}
        self._image_post = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.clear()

    def _drop(self, kind, key):
        entry = self._items[kind].pop(key, None)
        if entry is not None:
            entry['body'][:] = b'\0' * len(entry['body'])

    def clear(self):
        for kind, items in self._items.items():
            for key in list(items):
                self._drop(kind, key)
        self._image_post = None

    def get(self, kind, key):
        entry = self._items[kind].get(key)
        if entry is None:
            return None
        self._items[kind].move_to_end(key)
        return {'body': bytes(entry['body']), 'contentType': entry['contentType']}

    def put(self, kind, key, body, *, content_type=None, post_id=None):
        if (kind not in KINDS or not isinstance(body, (bytes, bytearray))
                or content_type not in self.CONTENT_TYPES):
            raise ValueError('invalid_source_cache_entry')
        usage._hash(key)
        if not body or len(body) > self.BODY_LIMIT:
            return False
        items = self._items[kind]
        if kind == 'images':
            if not isinstance(post_id, str) or not re.fullmatch(r'[1-9][0-9]{9,24}', post_id):
                raise ValueError('invalid_source_cache_post')
            if self._image_post != post_id:
                for previous in list(items):
                    self._drop(kind, previous)
                self._image_post = post_id
            total = sum(len(item['body']) for previous, item in items.items() if previous != key)
            if total + len(body) > self.IMAGE_LIMIT or key not in items and len(items) >= 4:
                return False
        else:
            maximum = self.POST_LIMIT if kind == 'posts' else self.SEARCH_LIMIT
            if key not in items and len(items) >= maximum:
                self._drop(kind, next(iter(items)))
        self._drop(kind, key)
        items[key] = {'body': bytearray(body), 'contentType': content_type}
        return True

    def counts(self):
        return {**{kind: len(items) for kind, items in self._items.items()},
                'imageBytes': sum(len(item['body']) for item in self._items['images'].values())}


def _zero():
    return dict.fromkeys(KINDS, 0)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _budgets(value):
    if not isinstance(value, dict):
        raise ValueError
    for day, counts in value.items():
        usage._day(day)
        usage._source_budget(counts)


def _cooldowns(value):
    if not isinstance(value, dict):
        raise ValueError
    for host, until in value.items():
        if host not in HOSTS:
            raise ValueError
        usage._time(until)


def _pause(value):
    if value is None:
        return
    usage._keys(value, ('reason', 'host', 'at', 'retryAt',
                        *(('httpStatus',) if 'httpStatus' in value else ())))
    if (not isinstance(value['reason'], str)
            or not re.fullmatch(r'[a-z_]{1,80}', value['reason'])
            or value['host'] not in HOSTS):
        raise ValueError
    usage._time(value['at'])
    usage._time(value['retryAt'])
    if 'httpStatus' in value and (type(value['httpStatus']) is not int
                                  or not 100 <= value['httpStatus'] <= 599):
        raise ValueError


def _imports(value):
    state = usage.empty_state()
    state['sourceImports'] = value
    usage.validate_state(state)


def _images(value):
    if not isinstance(value, list):
        raise ValueError
    seen, sources = set(), set()
    for image in value:
        usage._keys(image, ('receiptId', 'date', 'images', 'sourceHash'))
        usage._hash(image['receiptId'])
        usage._hash(image['sourceHash'])
        usage._day(image['date'])
        usage._count(image['images'])
        source = (image['date'], image['sourceHash'])
        if image['receiptId'] in seen or source in sources:
            raise ValueError
        seen.add(image['receiptId'])
        sources.add(source)


def validate_state(state):
    """Strict public-Git-safe metadata schema; reservations remain spent."""
    try:
        usage._keys(state, ('schemaVersion', 'baseline', 'receipts',
                            'nextRequests', 'cooldowns', 'paused',
                            *(('sourceImports',) if isinstance(state, dict) and 'sourceImports' in state else ())))
        if type(state['schemaVersion']) is not int or state['schemaVersion'] != 1:
            raise ValueError
        baseline = state['baseline']
        usage._keys(baseline, ('at', 'sourceHash', 'personalBudgets', 'sourceImports',
                               'historicalImages', 'paused', 'cooldowns', 'lastRequests'))
        usage._time(baseline['at'])
        usage._hash(baseline['sourceHash'])
        _budgets(baseline['personalBudgets'])
        _imports(baseline['sourceImports'])
        _imports(state.get('sourceImports', {}))
        _imports(_all_source_imports(state))
        _images(baseline['historicalImages'])
        _pause(baseline['paused'])
        _cooldowns(baseline['cooldowns'])
        _cooldowns(baseline['lastRequests'])
        _pause(state['paused'])
        _cooldowns(state['cooldowns'])
        _cooldowns(state['nextRequests'])
        if baseline['paused'] is not None and state['paused'] is None:
            raise ValueError
        for pause in (baseline['paused'], state['paused']):
            if pause is not None and (
                    pause['host'] not in state['cooldowns']
                    or usage._time(state['cooldowns'][pause['host']]) < usage._time(pause['retryAt'])):
                raise ValueError
        if any(usage._time(stamp) > usage._time(baseline['at'])
               for stamp in baseline['lastRequests'].values()):
            raise ValueError
        if any(item['date'] > usage._time(baseline['at']).astimezone(JST).date().isoformat()
               for item in baseline['historicalImages']):
            raise ValueError
        for host, until in baseline['cooldowns'].items():
            if (host not in state['cooldowns']
                    or usage._time(state['cooldowns'][host]) < usage._time(until)):
                raise ValueError
        for record in baseline['sourceImports'].values():
            budget = baseline['personalBudgets'].get(record['receipt']['date'], _zero())
            if any(budget[kind] < record['budgetAfter'][kind] for kind in ('searches', 'posts')):
                raise ValueError
        if not isinstance(state['receipts'], dict):
            raise ValueError
        minimums = {host: usage._time(at) + dt.timedelta(seconds=12)
                    for host, at in baseline['lastRequests'].items()}
        for key, item in state['receipts'].items():
            usage._hash(key)
            usage._keys(item, ('runId', 'component', 'kind', 'requestHash', 'host', 'date',
                                'reservedAt', 'issuedAt', 'completedAt', 'status',
                                'httpStatus', 'contentHash'))
            usage._token(item['runId'])
            if item['component'] not in COMPONENTS or item['kind'] not in KINDS:
                raise ValueError
            if item['host'] not in HOSTS or HOST_KIND[item['host']] != item['kind']:
                raise ValueError
            usage._hash(item['requestHash'])
            if key != _digest(item['runId'] + ':' + item['kind'] + ':' + item['requestHash']):
                raise ValueError
            usage._day(item['date'])
            reserved = usage._time(item['reservedAt'])
            issued = usage._time(item['issuedAt']) if item['issuedAt'] is not None else None
            completed = usage._time(item['completedAt']) if item['completedAt'] is not None else None
            if (reserved < usage._time(baseline['at'])
                    or item['date'] != reserved.astimezone(JST).date().isoformat()
                    or issued is not None and (issued < reserved
                        or issued.astimezone(JST).date() != reserved.astimezone(JST).date())
                    or completed is not None and completed < (issued or reserved)):
                raise ValueError
            if item['status'] not in ('reserved', 'issued', 'ok', 'failed'):
                raise ValueError
            if ((item['status'] == 'reserved') != (issued is None and completed is None)
                    or item['status'] == 'issued' and (issued is None or completed is not None)
                    or item['status'] in ('ok', 'failed') and completed is None
                    or item['status'] == 'ok' and issued is None):
                raise ValueError
            if item['httpStatus'] is not None and (
                    type(item['httpStatus']) is not int or not 100 <= item['httpStatus'] <= 599):
                raise ValueError
            if item['contentHash'] is not None:
                usage._hash(item['contentHash'])
                if item['status'] != 'ok':
                    raise ValueError
            spacing = 2 if item['component'] == 'official' else 12
            minimum = (issued or reserved) + dt.timedelta(seconds=spacing)
            minimums[item['host']] = max(minimums.get(item['host'], minimum), minimum)
        for host, minimum in minimums.items():
            if host not in state['nextRequests'] or usage._time(state['nextRequests'][host]) < minimum:
                raise ValueError
        totals = legacy_budgets(state)
        for record in state.get('sourceImports', {}).values():
            day = record['receipt']['date']
            original = baseline['personalBudgets'].get(day, _zero())
            if any(record['budgetBefore'][kind] < original[kind]
                   or record['budgetAfter'][kind] > totals[day][kind] for kind in ('searches', 'posts')):
                raise ValueError
        _validate_limits(state)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError('invalid_source_usage') from None


def _validate_limits(state):
    runs, days = {}, {}
    for receipt in state['receipts'].values():
        run = runs.setdefault(receipt['runId'], {name: _zero() for name in COMPONENTS})
        run[receipt['component']][receipt['kind']] += 1
        if receipt['component'] != 'official':
            daily = days.setdefault(receipt['date'], {name: _zero() for name in ('personal', 'schedule')})
            daily[receipt['component']][receipt['kind']] += 1
    for run in runs.values():
        if (run['official']['searches'] > 2
                or sum(row['searches'] for row in run.values()) > 17
                or run['personal']['searches'] + run['schedule']['searches'] > 15
                or run['schedule']['searches'] > 1
                or sum(row['posts'] + row['images'] for row in run.values()) > 20
                or run['personal']['posts'] > 14 or run['personal']['images'] > 0
                or run['schedule']['posts'] > 1 or run['schedule']['images'] > 4):
            raise ValueError
    for day, daily in days.items():
        # Late approved history cannot invalidate already-spent native requests.
        # check() uses the full imported totals to block any further reservation.
        baseline = state['baseline']['personalBudgets'].get(day, _zero())
        images = sum(item['images'] for item in state['baseline']['historicalImages']
                     if item['date'] == day)
        searches = sum(row['searches'] for row in daily.values())
        individual = sum(row['posts'] + row['images'] for row in daily.values())
        if (searches and searches + baseline['searches'] > 60
                or individual and individual + baseline['posts'] + images > DAY_INDIVIDUAL_LIMIT
                or daily['schedule']['images'] and daily['schedule']['images'] + images > 8):
            raise ValueError


def _all_source_imports(state):
    baseline, additions = state['baseline']['sourceImports'], state.get('sourceImports', {})
    if baseline.keys() & additions.keys():
        raise ValueError('duplicate_source_usage_import')
    return {**baseline, **additions}


def _imported_budgets(state):
    result = copy.deepcopy(state['baseline']['personalBudgets'])
    for record in state.get('sourceImports', {}).values():
        receipt = record['receipt']
        counts = result.setdefault(receipt['date'], {'searches': 0, 'posts': 0})
        for kind in ('searches', 'posts'):
            counts[kind] += receipt[kind]
    return result


def legacy_budgets(state):
    result = _imported_budgets(state)
    for item in state['receipts'].values():
        if item['component'] == 'personal' and item['kind'] in ('searches', 'posts'):
            counts = result.setdefault(item['date'], {'searches': 0, 'posts': 0})
            counts[item['kind']] += 1
    return result


def validate_legacy(state, personal_state, analysis_state=None):
    """Require baseline + approved additions + native personal counters exactly."""
    validate_state(state)
    try:
        _budgets(personal_state['budgets'])
        if personal_state['budgets'] != legacy_budgets(state):
            raise ValueError
        _pause(personal_state['paused'])
        if personal_state['paused'] is not None and state['paused'] != personal_state['paused']:
            raise ValueError
        if analysis_state is not None:
            usage.validate_state(analysis_state)
            if analysis_state.get('sourceImports', {}) != _all_source_imports(state):
                raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError('source_usage_budget_mismatch') from None


def apply_source_imports(state, receipts, *, personal_before, personal_after,
                         analysis_before, analysis_after):
    """Reconcile only approved legacy source deltas; return a new data-only state.

    The caller owns approval, lease/CAS and atomic publication of all three files.
    Optional top-level sourceImports holds the exact added AI sourceImport records;
    the original migration baseline, all native receipts and safety state stay
    immutable. New AI usage imports never authorize anything in this function.
    """
    validate_legacy(state, personal_before, analysis_before)
    usage.validate_state(analysis_before)
    usage.validate_state(analysis_after)
    if not isinstance(receipts, list) or len(receipts) > 10:
        raise ValueError('invalid_source_usage_import')
    expected_analysis = copy.deepcopy(analysis_before)
    expected_personal = {'budgets': copy.deepcopy(personal_before['budgets'])}
    for receipt in receipts:
        usage.apply_source_import(expected_analysis, receipt, expected_personal)
    expected_imports = expected_analysis.get('sourceImports', {})
    if expected_imports != analysis_after.get('sourceImports', {}):
        raise ValueError('source_usage_import_mismatch')
    try:
        _budgets(personal_after['budgets'])
        if expected_personal['budgets'] != personal_after['budgets']:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError('source_usage_budget_mismatch') from None
    result = copy.deepcopy(state)
    existing = _all_source_imports(state)
    for key, record in expected_imports.items():
        if key not in existing:
            result.setdefault('sourceImports', {})[key] = copy.deepcopy(record)
    validate_legacy(result, personal_after, analysis_after)
    return result


def baseline_state(personal_state, *, source_hash, at, source_imports=None,
                   historical_images=(), cooldowns=None):
    """Build an explicit approved migration, without mutating any legacy state.

    source_imports are the already-applied AI ledger sourceImports records.
    historical_images contain receiptId/date/images/sourceHash evidence records;
    these are old GETs, not new reservations or personal post deltas.
    """
    baseline = {
        'at': usage._stamp(at), 'sourceHash': source_hash,
        'personalBudgets': copy.deepcopy(personal_state['budgets']),
        'sourceImports': copy.deepcopy(source_imports if source_imports is not None else {}),
        'historicalImages': copy.deepcopy(list(historical_images)),
        'paused': copy.deepcopy(personal_state['paused']),
        'cooldowns': copy.deepcopy(cooldowns if cooldowns is not None else {}),
        'lastRequests': copy.deepcopy(personal_state.get('lastRequests', {})),
    }
    state = {'schemaVersion': 1, 'baseline': baseline, 'receipts': {},
             'nextRequests': {host: usage._stamp(usage._time(stamp) + dt.timedelta(seconds=12))
                              for host, stamp in baseline['lastRequests'].items()},
             'cooldowns': copy.deepcopy(baseline['cooldowns']), 'paused': copy.deepcopy(baseline['paused'])}
    if state['paused']:
        pause = state['paused']
        previous = state['cooldowns'].get(pause['host'], pause['retryAt'])
        state['cooldowns'][pause['host']] = usage._stamp(max(
            usage._time(previous), usage._time(pause['retryAt'])))
    validate_legacy(state, personal_state)
    return state


def _read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('invalid_source_usage')
            result[key] = value
        return result

    def invalid(_):
        raise ValueError('invalid_source_usage')

    with Path(path).open(encoding='utf-8') as handle:
        return json.load(handle, object_pairs_hook=pairs, parse_constant=invalid)


def load_state(path, required=False):
    try:
        state = _read_json(path)
    except FileNotFoundError:
        if required:
            raise ValueError('missing_source_usage') from None
        return None
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_source_usage') from None
    validate_state(state)
    return state


def atomic_json(path, state):
    validate_state(state)
    path = Path(path)
    staging = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with staging.open('x', encoding='utf-8', newline='\n') as handle:
            json.dump(state, handle, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        staging.unlink(missing_ok=True)


def initialize(path, personal_state, **baseline_options):
    """Caller owns migration approval/lease. Never overwrite an existing ledger."""
    state = baseline_state(personal_state, **baseline_options)
    with _Lock(Path(path)):
        if Path(path).exists():
            previous = load_state(path, required=True)
            if previous['baseline'] != state['baseline']:
                raise ValueError('source_usage_already_initialized')
            return previous
        else:
            atomic_json(path, state)
    return state


def usage_counts(state, run_id, now, component='schedule', *, catch_up=False):
    validate_state(state)
    usage._token(run_id)
    if component not in COMPONENTS:
        raise ValueError('invalid_source_configuration')
    day = usage._now(now).astimezone(JST).date().isoformat()
    run = {name: _zero() for name in COMPONENTS}
    daily = {name: _zero() for name in COMPONENTS}
    daily['personal'].update(_imported_budgets(state).get(day, {}))
    historical = sum(item['images'] for item in state['baseline']['historicalImages']
                     if item['date'] == day)
    for item in state['receipts'].values():
        if item['runId'] == run_id:
            run[item['component']][item['kind']] += 1
        if item['date'] == day:
            daily[item['component']][item['kind']] += 1
    shared_searches = sum(run[name]['searches'] for name in ('personal', 'schedule'))
    all_searches = sum(item['searches'] for item in run.values())
    individual = sum(item['posts'] + item['images'] for item in run.values())
    day_searches = sum(daily[name]['searches'] for name in ('personal', 'schedule'))
    day_individual = historical + sum(daily[name]['posts'] + daily[name]['images']
                                      for name in ('personal', 'schedule'))
    search_left = [(17 if catch_up else 5) - all_searches]
    individual_left = [20 - individual]
    if component == 'official':
        search_left.append(2 - run['official']['searches'])
    else:
        search_left.extend(((15 if catch_up else 3) - shared_searches, 60 - day_searches))
        individual_left.append(DAY_INDIVIDUAL_LIMIT - day_individual)
    posts_left, images_left = list(individual_left), list(individual_left)
    if component == 'personal':
        posts_left.append((14 if catch_up else 3) - run['personal']['posts'])
        images_left.append(0)
    elif component == 'schedule':
        search_left.append(1 - run['schedule']['searches'])
        posts_left.append(1 - run['schedule']['posts'])
        images_left.extend((4 - run['schedule']['images'],
                            8 - daily['schedule']['images'] - historical))
    else:
        images_left.append(0)
    return {'runId': run_id, 'date': day, 'component': component,
            'run': run, 'day': daily, 'historicalImages': historical,
            'issued': {name: {kind: sum(
                item['runId'] == run_id and item['component'] == name
                and item['kind'] == kind and item['issuedAt'] is not None
                for item in state['receipts'].values()) for kind in KINDS} for name in COMPONENTS},
            'remaining': {'searches': max(0, min(search_left)),
                          'posts': max(0, min(posts_left)), 'images': max(0, min(images_left))}}


def request_identity(kind, url):
    """Canonicalize only for accounting. Each HTTP client still owns its allowlist."""
    if kind not in KINDS or not isinstance(url, str):
        raise ValueError('invalid_source_request')
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port or parsed.fragment):
        raise ValueError('invalid_source_request')
    if kind == 'posts':
        if parsed.hostname == HOSTS[1] and parsed.path == '/tweet-result':
            ids = urllib.parse.parse_qs(parsed.query).get('id', [])
        elif parsed.hostname in ('x.com', 'twitter.com'):
            match = re.fullmatch(r'/[A-Za-z0-9_]{1,15}/status/([1-9][0-9]{9,24})', parsed.path)
            ids = [match[1]] if match and not parsed.query else []
        else:
            ids = []
        if len(ids) != 1 or not re.fullmatch(r'[1-9][0-9]{9,24}', ids[0]):
            raise ValueError('invalid_source_request')
        identity, host = ids[0], HOSTS[1]
    elif kind == 'images':
        if parsed.hostname != HOSTS[2] or not re.fullmatch(
                r'/media/[A-Za-z0-9_-]+(?:\.(?:jpg|jpeg|png))?', parsed.path):
            raise ValueError('invalid_source_request')
        identity = re.sub(r'\.(?:jpg|jpeg|png)$', '', parsed.path)
        host = HOSTS[2]
    else:
        if parsed.hostname != HOSTS[0] or parsed.path != '/realtime/search':
            raise ValueError('invalid_source_request')
        identity, host = url, HOSTS[0]
    return _digest(identity), host


class _Lock:
    def __init__(self, path):
        self.path, self.handle = path, None

    def __enter__(self):
        handle = self.path.with_name(self.path.name + '.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if not handle.tell():
                    handle.write(b'\0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SourceFailure('source_usage_locked') from None
        self.handle = handle
        return self

    def __exit__(self, *args):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


class SharedSource:
    failure_type = SourceFailure

    def __init__(self, path, *, run_id, component, clock, sleep, personal_path=None, cache=None,
                 catch_up=False):
        usage._token(run_id)
        if component not in COMPONENTS:
            raise ValueError('invalid_source_configuration')
        if cache is not None and cache.run_id != run_id:
            raise ValueError('source_cache_run_mismatch')
        self.path, self.run_id, self.component = Path(path), run_id, component
        self.clock, self.sleep = clock, sleep
        self.personal_path = Path(personal_path) if personal_path is not None else None
        self.cache = cache
        if type(catch_up) is not bool:
            raise ValueError('invalid_source_configuration')
        self.catch_up = catch_up
        self.state, self._lock, self._poisoned = None, None, False
        self._owned = set()

    def __enter__(self):
        if self._lock is not None:
            raise ValueError('source_usage_already_open')
        if not self.path.is_file():
            raise ValueError('missing_source_usage')
        self._lock = _Lock(self.path)
        try:
            self._lock.__enter__()
            self.state = load_state(self.path, required=True)
            self._legacy()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *args):
        if args and args[0] is not None and self.cache is not None:
            self.cache.clear()
        if self._lock is not None:
            self._lock.__exit__(*args)
            self._lock = None
        self._owned.clear()

    def _require(self):
        if self._lock is None:
            raise ValueError('source_usage_not_open')
        if self._poisoned:
            raise SourceFailure('source_usage_save_failed')

    def _legacy(self):
        if self.personal_path is not None:
            validate_legacy(self.state, _read_json(self.personal_path))

    def _save(self):
        self._require()
        try:
            atomic_json(self.path, self.state)
        except (OSError, ValueError):
            self._poisoned = True
            raise ValueError('source_usage_save_failed') from None

    def report(self):
        self._require()
        return usage_counts(self.state, self.run_id, self.clock(), self.component, catch_up=self.catch_up)

    counts = report

    def cached(self, kind, url):
        """Return an opt-in, successful same-run body; no receipt or quota mutation."""
        self._require()
        self._legacy()
        key, _ = request_identity(kind, url)
        receipt_id = _digest(self.run_id + ':' + kind + ':' + key)
        item = self.state['receipts'].get(receipt_id)
        if self.cache is None or item is None or item['status'] != 'ok':
            return None
        entry = self.cache.get(kind, key)
        if entry is not None:
            entry['fetchedAt'] = item['completedAt']
        return entry

    def remember(self, receipt_id, body, *, content_type=None, post_id=None):
        """Cache an acquired response only after its durable successful receipt."""
        self._require()
        item = self.state['receipts'].get(receipt_id)
        if (receipt_id not in self._owned or item is None
                or item['runId'] != self.run_id or item['status'] != 'ok'):
            raise SourceFailure('source_interrupted')
        if self.cache is None:
            return False
        return self.cache.put(item['kind'], item['requestHash'], body,
                              content_type=content_type, post_id=post_id)

    def check(self, kind, count=1):
        self._require()
        if kind not in KINDS or type(count) is not int or count < 1:
            raise ValueError('invalid_source_request')
        self._legacy()
        if self.component != 'official' and self.state['paused'] is not None:
            raise SourceFailure('source_paused')
        if self.report()['remaining'][kind] < count:
            raise SourceFailure('source_budget_exhausted')

    def reserve(self, kind, url):
        self._require()
        request_hash, host = request_identity(kind, url)
        receipt_id = _digest(self.run_id + ':' + kind + ':' + request_hash)
        if receipt_id in self.state['receipts']:
            raise SourceFailure('source_already_requested')
        self.check(kind)
        now = usage._now(self.clock())
        cooldown = self.state['cooldowns'].get(host)
        if cooldown is not None and usage._time(cooldown) > now:
            raise SourceFailure('source_host_cooldown', retry_at=cooldown)
        until = self.state['nextRequests'].get(host)
        if self.component != 'official':
            prior = [usage._time(item['issuedAt'] or item['reservedAt'])
                     for item in self.state['receipts'].values() if item['host'] == host]
            if prior:
                minimum = max(prior) + dt.timedelta(seconds=12)
                until = usage._stamp(max(minimum, usage._time(until))) if until else usage._stamp(minimum)
        if until is not None and usage._time(until) > now:
            self.sleep((usage._time(until) - now).total_seconds())
            self.check(kind)
            now = usage._now(self.clock())
            if usage._time(until) > now:
                raise SourceFailure('source_host_cooldown', retry_at=until)
        self.check(kind)
        now = usage._now(self.clock())
        self.state['receipts'][receipt_id] = {
            'runId': self.run_id, 'component': self.component, 'kind': kind,
            'requestHash': request_hash, 'host': host, 'date': now.astimezone(JST).date().isoformat(),
            'reservedAt': usage._stamp(now), 'issuedAt': None, 'completedAt': None,
            'status': 'reserved', 'httpStatus': None, 'contentHash': None}
        spacing = 2 if self.component == 'official' else 12
        self.state['nextRequests'][host] = usage._stamp(now + dt.timedelta(seconds=spacing))
        self._save()
        self._owned.add(receipt_id)
        return receipt_id

    def issued(self, receipt_id):
        self._require()
        if receipt_id not in self._owned:
            raise SourceFailure('source_interrupted')
        item = self.state['receipts'][receipt_id]
        now = usage._now(self.clock())
        if (item['status'] != 'reserved' or now < usage._time(item['reservedAt'])
                or now.astimezone(JST).date().isoformat() != item['date']):
            raise SourceFailure('source_interrupted')
        if self.component != 'official' and self.state['paused'] is not None:
            raise SourceFailure('source_paused')
        until = self.state['cooldowns'].get(item['host'])
        if until is not None and usage._time(until) > now:
            raise SourceFailure('source_host_cooldown', retry_at=until)
        item['issuedAt'], item['status'] = usage._stamp(now), 'issued'
        spacing = 2 if self.component == 'official' else 12
        self.state['nextRequests'][item['host']] = usage._stamp(now + dt.timedelta(seconds=spacing))
        self._save()
        after = usage._now(self.clock())
        if after < now or after.astimezone(JST).date().isoformat() != item['date']:
            raise SourceFailure('source_interrupted')

    def finish(self, receipt_id, status='ok', *, http_status=None, content_hash=None):
        self._require()
        if (receipt_id not in self._owned or status not in ('ok', 'failed')
                or status == 'ok' and self.state['receipts'][receipt_id]['issuedAt'] is None):
            raise SourceFailure('source_interrupted')
        item = self.state['receipts'][receipt_id]
        if item['completedAt'] is not None:
            raise SourceFailure('source_already_requested')
        item.update(status=status, httpStatus=http_status, contentHash=content_hash,
                    completedAt=usage._stamp(max(usage._now(self.clock()),
                        usage._time(item['issuedAt'] or item['reservedAt']))))
        self._save()

    def set_cooldown(self, host, until, *, paused=None):
        self._require()
        if host not in HOSTS:
            raise ValueError('invalid_source_host')
        until = usage._now(until)
        if host in self.state['cooldowns']:
            until = max(until, usage._time(self.state['cooldowns'][host]))
        self.state['cooldowns'][host] = usage._stamp(until)
        if paused is not None:
            _pause(paused)
            self.state['paused'] = copy.deepcopy(paused)
        self._save()
