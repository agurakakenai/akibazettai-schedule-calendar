"""Private, locked AI accounting shared by official, personal and schedule collectors.

Reservations are spent even without an issue/completion marker. Nothing retries a
receipt. Imports are explicit approved historical totals, never inferred from an
absent ledger or from a collector's legacy cache.
"""
import copy
import datetime as dt
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import uuid


RUN_LIMIT, DAY_LIMIT, SPACING_SECONDS = 3, 30, 60
JST = dt.timezone(dt.timedelta(hours=9))
HEX = re.compile(r'[0-9a-f]{64}\Z')
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z')
REASONS = {
    'events', 'links', 'work_timing', 'no_event', 'schedule', 'not_schedule',
    'azure_pending', 'azure_invalid_output', 'azure_refused',
    'azure_timeout', 'azure_network_error', 'azure_http_error', 'azure_rate_limited',
    'azure_auth_stopped', 'azure_interrupted', 'azure_input_limit', 'azure_ungrounded',
    'azure_model_mismatch', 'azure_deadline', 'azure_budget_exhausted', 'azure_backoff',
}
SUCCESS_REASONS = {'events', 'links', 'work_timing', 'no_event', 'schedule', 'not_schedule'}


class UsageFailure(Exception):
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


def _keys(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('invalid_ai_usage')


def _hash(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError('invalid_ai_usage')


def _token(value):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError('invalid_ai_usage')


def _day(value):
    if not isinstance(value, str) or dt.date.fromisoformat(value).isoformat() != value:
        raise ValueError('invalid_ai_usage')


def _now(value):
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('invalid_ai_usage_clock')
    return value.astimezone(dt.timezone.utc)


def _stamp(value):
    return _now(value).isoformat().replace('+00:00', 'Z')


def _time(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('invalid_ai_usage')
    return _now(dt.datetime.fromisoformat(value[:-1] + '+00:00'))


def _count(value):
    if type(value) is not int or value < 1:
        raise ValueError('invalid_ai_usage')


def _identity(value):
    _keys(value, ('provider', 'endpointHash', 'deployment', 'model', 'modelVersion'))
    if value['provider'] != 'azure_openai':
        raise ValueError('invalid_ai_usage')
    _hash(value['endpointHash'])
    for field in ('deployment', 'model', 'modelVersion'):
        _token(value[field])


def _private_identity(value):
    _keys(value, ('provider', 'endpoint', 'deployment', 'model', 'modelVersion'))
    endpoint = value['endpoint']
    if (not isinstance(endpoint, str)
            or not re.fullmatch(r'https://[a-z0-9-]+\.openai\.azure\.com', endpoint)):
        raise ValueError('invalid_ai_usage')
    result = {key: child for key, child in value.items() if key != 'endpoint'}
    result['endpointHash'] = hashlib.sha256(endpoint.encode()).hexdigest()
    _identity(result)
    return result


def _receipt_id(component, key):
    return hashlib.sha256((component + ':' + key).encode()).hexdigest()


def empty_state():
    return {'schemaVersion': 1, 'receipts': {}, 'imports': {}, 'sourceImports': {},
            'nextRequestAt': None, 'retryAt': None, 'paused': None}


def _source_budget(value):
    _keys(value, ('searches', 'posts'))
    if any(type(count) is not int or count < 0 for count in value.values()):
        raise ValueError('invalid_ai_usage')


def _validate_source_import(receipt):
    _keys(receipt, ('receiptId', 'date', 'searches', 'posts', 'sourceHash'))
    _hash(receipt['receiptId'])
    _hash(receipt['sourceHash'])
    _day(receipt['date'])
    _source_budget({key: receipt[key] for key in ('searches', 'posts')})
    if (receipt['searches'] > 60 or receipt['posts'] > 30
            or receipt['searches'] + receipt['posts'] == 0):
        raise ValueError('invalid_ai_usage')


def _validate_import(receipt):
    _keys(receipt, ('receiptId', 'date', 'counts', 'modelBreakdown', 'sourceHash'))
    _hash(receipt['receiptId'])
    _hash(receipt['sourceHash'])
    _day(receipt['date'])
    _keys(receipt['counts'], ('requests',))
    _count(receipt['counts']['requests'])
    if not isinstance(receipt['modelBreakdown'], list) or not receipt['modelBreakdown']:
        raise ValueError('invalid_ai_usage')
    total, seen = 0, set()
    for item in receipt['modelBreakdown']:
        if not isinstance(item, dict):
            raise ValueError('invalid_ai_usage')
        optional = ('deployment', 'modelVersion', 'component')
        _keys(item, ('model', 'kind', 'count', *(field for field in optional if field in item)))
        _token(item['model'])
        if not isinstance(item['kind'], str) or item['kind'] not in ('image', 'text', 'other'):
            raise ValueError('invalid_ai_usage')
        for field in ('deployment', 'modelVersion'):
            if field in item:
                _token(item[field])
        if 'component' in item and item['component'] not in ('official', 'personal', 'schedule', 'external'):
            raise ValueError('invalid_ai_usage')
        _count(item['count'])
        identity = (item['model'], item['kind'], *(item.get(field) for field in optional))
        if identity in seen:
            raise ValueError('invalid_ai_usage')
        seen.add(identity)
        total += item['count']
    if total != receipt['counts']['requests']:
        raise ValueError('invalid_ai_usage')


def validate_state(value):
    """Validate a data-only private ledger; reject unknown fields at every level."""
    try:
        fields = set(empty_state())
        if isinstance(value, dict) and 'sourceImports' not in value:
            fields.remove('sourceImports')
        _keys(value, fields)
        if type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
            raise ValueError
        if not isinstance(value['receipts'], dict) or not isinstance(value['imports'], dict):
            raise ValueError
        for name in ('nextRequestAt', 'retryAt'):
            if value[name] is not None:
                _time(value[name])
        if value['paused'] is not None:
            _keys(value['paused'], ('reason', 'httpStatus', 'at'))
            if (value['paused']['reason'] != 'azure_auth_stopped'
                    or type(value['paused']['httpStatus']) is not int
                    or value['paused']['httpStatus'] not in (401, 403)):
                raise ValueError
            _time(value['paused']['at'])
        minimum_next = None
        for key, receipt in value['receipts'].items():
            _hash(key)
            _keys(receipt, ('runId', 'component', 'requestHash', 'identity', 'date',
                            'reservedAt', 'issuedAt', 'completedAt', 'reason',
                            'httpStatus', 'retryAt'))
            _token(receipt['runId'])
            if receipt['component'] not in ('official', 'personal', 'schedule'):
                raise ValueError
            _hash(receipt['requestHash'])
            if key != _receipt_id(receipt['component'], receipt['requestHash']):
                raise ValueError
            _identity(receipt['identity'])
            _day(receipt['date'])
            reserved = _time(receipt['reservedAt'])
            issued = _time(receipt['issuedAt']) if receipt['issuedAt'] is not None else None
            completed = _time(receipt['completedAt']) if receipt['completedAt'] is not None else None
            if (issued is not None and issued < reserved
                    or completed is not None and completed < (issued or reserved)):
                raise ValueError
            if receipt['date'] != (issued or reserved).astimezone(JST).date().isoformat():
                raise ValueError
            minimum = (issued or reserved) + dt.timedelta(seconds=SPACING_SECONDS)
            minimum_next = max(minimum_next, minimum) if minimum_next is not None else minimum
            if not isinstance(receipt['reason'], str) or receipt['reason'] not in REASONS:
                raise ValueError
            if completed is None and receipt['reason'] != 'azure_interrupted':
                raise ValueError
            if receipt['reason'] in SUCCESS_REASONS and issued is None:
                raise ValueError
            status = receipt['httpStatus']
            if status is not None and (type(status) is not int or not 100 <= status <= 599):
                raise ValueError
            if receipt['retryAt'] is not None:
                _time(receipt['retryAt'])
            if receipt['reason'] == 'azure_auth_stopped':
                if status not in (401, 403) or value['paused'] is None:
                    raise ValueError
            if receipt['reason'] == 'azure_rate_limited':
                if (status != 429 or receipt['retryAt'] is None or value['retryAt'] is None
                        or _time(value['retryAt']) < _time(receipt['retryAt'])):
                    raise ValueError
        if minimum_next is not None:
            if value['nextRequestAt'] is None or _time(value['nextRequestAt']) < minimum_next:
                raise ValueError
        provenance = set()
        for key, receipt in value['imports'].items():
            _validate_import(receipt)
            if key != receipt['receiptId']:
                raise ValueError
            source = (receipt['date'], receipt['sourceHash'])
            if source in provenance:
                raise ValueError
            provenance.add(source)
        source_imports = value.get('sourceImports', {})
        if not isinstance(source_imports, dict):
            raise ValueError
        provenance = set()
        for key, record in source_imports.items():
            _keys(record, ('receipt', 'budgetBefore', 'budgetAfter'))
            receipt = record['receipt']
            _validate_source_import(receipt)
            if key != receipt['receiptId']:
                raise ValueError
            for field in ('budgetBefore', 'budgetAfter'):
                _source_budget(record[field])
            if any(record['budgetAfter'][kind] != record['budgetBefore'][kind] + receipt[kind]
                   for kind in ('searches', 'posts')):
                raise ValueError
            source = (receipt['date'], receipt['sourceHash'])
            if source in provenance:
                raise ValueError
            provenance.add(source)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError('invalid_ai_usage') from None


def load_state(path, required=False):
    """Return None for an optional absent ledger, never an authoritative zero."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('invalid_ai_usage')
            result[key] = value
        return result

    def invalid(_):
        raise ValueError('invalid_ai_usage')

    try:
        with Path(path).open(encoding='utf-8') as handle:
            state = json.load(handle, object_pairs_hook=pairs, parse_constant=invalid)
    except FileNotFoundError:
        if required:
            raise ValueError('missing_ai_usage') from None
        return None
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_ai_usage') from None
    validate_state(state)
    return state


def atomic_json(path, state):
    """Atomic validated replacement. Callers changing a ledger must hold its lock."""
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


def _counts(state, run_id, now):
    day = _now(now).astimezone(JST).date().isoformat()
    run = sum(item['runId'] == run_id for item in state['receipts'].values())
    daily = (sum(item['date'] == day for item in state['receipts'].values())
             + sum(item['counts']['requests'] for item in state['imports'].values()
                   if item['date'] == day))
    return {'run': run, 'day': daily, 'remaining': max(0, min(RUN_LIMIT - run, DAY_LIMIT - daily))}


def usage_counts(state, run_id, now):
    """Return actual-JST-day and combined-run counts for fair child scheduling."""
    validate_state(state)
    _token(run_id)
    return _counts(state, run_id, now)


def remaining(state, run_id, now):
    return usage_counts(state, run_id, now)['remaining']


def apply_import(state, receipt):
    """Apply an approved historical receipt in place, idempotently; return state.

    Approval/provenance verification belongs to the caller. A changed receipt ID
    cannot re-import the same source hash on the same date. Persist under the lock.
    """
    validate_state(state)
    try:
        _validate_import(receipt)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('invalid_ai_usage_import') from None
    previous = state['imports'].get(receipt['receiptId'])
    if previous is not None:
        if previous != receipt:
            raise ValueError('conflicting_ai_usage_import')
        return state
    if any(item['sourceHash'] == receipt['sourceHash'] and item['date'] == receipt['date']
           for item in state['imports'].values()):
        raise ValueError('duplicate_ai_usage_provenance')
    state['imports'][receipt['receiptId']] = copy.deepcopy(receipt)
    return state


def apply_source_import(state, receipt, personal_state):
    """Apply one approved source-budget delta in place and return the AI ledger.

    This never consumes AI quota. The caller must persist both states together
    under its state transaction. Recorded before/after counters detect incomplete
    publication or rollback rather than silently skipping an unapplied delta.
    """
    validate_state(state)
    try:
        _validate_source_import(receipt)
        budgets = personal_state['budgets']
        if not isinstance(budgets, dict):
            raise ValueError
        for day, counts in budgets.items():
            _day(day)
            _source_budget(counts)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('invalid_source_usage_import') from None
    records = state.get('sourceImports', {})
    for record in records.values():
        counts = budgets.get(record['receipt']['date'], {'searches': 0, 'posts': 0})
        if any(counts[kind] < record['budgetAfter'][kind] for kind in ('searches', 'posts')):
            raise ValueError('source_usage_budget_mismatch')
    previous = records.get(receipt['receiptId'])
    if previous is not None:
        if previous['receipt'] != receipt:
            raise ValueError('conflicting_source_usage_import')
        return state
    if any(record['receipt']['date'] == receipt['date']
           and record['receipt']['sourceHash'] == receipt['sourceHash'] for record in records.values()):
        raise ValueError('duplicate_source_usage_provenance')
    before = copy.deepcopy(budgets.get(receipt['date'], {'searches': 0, 'posts': 0}))
    after = {kind: before[kind] + receipt[kind] for kind in ('searches', 'posts')}
    state.setdefault('sourceImports', {})[receipt['receiptId']] = {
        'receipt': copy.deepcopy(receipt), 'budgetBefore': before, 'budgetAfter': copy.deepcopy(after)}
    budgets[receipt['date']] = after
    return state


class SharedUsage:
    """Hold one cross-process file lock for the entire child, including its HTTP.

    Production requires an existing validated file. ``create=True`` is only for
    explicit initialization/migration. ``deadline()`` returns True when allowed.
    The lock fails closed rather than polling/retrying a concurrent child.
    """
    failure_type = UsageFailure

    def __init__(self, path, *, run_id, component, clock, sleep,
                 request_limit=RUN_LIMIT, deadline=None, create=False):
        _token(run_id)
        if (component not in ('official', 'personal', 'schedule') or type(request_limit) is not int
                or not 0 <= request_limit <= RUN_LIMIT
                or deadline is not None and not callable(deadline)):
            raise ValueError('invalid_ai_usage_configuration')
        self.path, self.run_id, self.component = Path(path), run_id, component
        self.clock, self.sleep = clock, sleep
        self.request_limit = min(request_limit, 1) if component == 'schedule' else request_limit
        self.deadline, self.create = deadline, create
        self.state, self._lock = None, None
        self._owned, self._active = set(), None
        self._http = (None, None)

    def __enter__(self):
        if self._lock is not None:
            raise ValueError('ai_usage_already_open')
        if not self.path.parent.is_dir():
            if not self.create:
                raise ValueError('missing_ai_usage')
            self.path.parent.mkdir(parents=True)
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
            raise UsageFailure('azure_usage_locked') from None
        self._lock = handle
        try:
            self.state = load_state(self.path, required=not self.create)
            if self.state is None:
                self.state = empty_state()
                self._save()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._lock is not None:
            handle, self._lock = self._lock, None
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
        self._owned.clear()
        self._active = None

    def _require_open(self):
        if self._lock is None:
            raise ValueError('ai_usage_not_open')

    @property
    def used(self):
        self._require_open()
        return sum(item['runId'] == self.run_id and item['component'] == self.component
                   for item in self.state['receipts'].values())

    def _save(self):
        self._require_open()
        atomic_json(self.path, self.state)

    def _allowed(self):
        if self.deadline is not None and self.deadline() is not True:
            raise UsageFailure('azure_deadline')
        if self.state['paused']:
            raise UsageFailure('azure_auth_stopped', self.state['paused']['httpStatus'])

    def check(self):
        """Non-reserving preflight before source GETs; never save, wait or spend."""
        self._require_open()
        self._allowed()
        now = _now(self.clock())
        if self.used >= self.request_limit or not _counts(self.state, self.run_id, now)['remaining']:
            raise UsageFailure('azure_budget_exhausted')
        retry = self.state['retryAt']
        if retry is not None and _time(retry) > now:
            raise UsageFailure('azure_backoff', retry_at=retry)

    def reserve(self, key, identity):
        self._require_open()
        _hash(key)
        private_identity = _private_identity(identity)
        receipt_id = _receipt_id(self.component, key)
        if receipt_id in self.state['receipts']:
            previous = self.state['receipts'][receipt_id]
            reason = previous['reason'] if previous['reason'] not in SUCCESS_REASONS else 'azure_already_analyzed'
            raise UsageFailure(reason, previous['httpStatus'], previous['retryAt'])
        self._allowed()
        if self._active is not None:
            raise UsageFailure('azure_interrupted')
        now = _now(self.clock())
        if self.used >= self.request_limit or not _counts(self.state, self.run_id, now)['remaining']:
            raise UsageFailure('azure_budget_exhausted')
        retry = self.state['retryAt']
        if retry is not None and _time(retry) > now:
            raise UsageFailure('azure_backoff', retry_at=retry)
        until = self.state['nextRequestAt']
        if until is not None and _time(until) > now:
            self.sleep((_time(until) - now).total_seconds())
            self._allowed()
            now = _now(self.clock())
            if _time(until) > now:
                raise UsageFailure('azure_backoff', retry_at=until)
        self._allowed()
        now = _now(self.clock())
        if self.used >= self.request_limit or not _counts(self.state, self.run_id, now)['remaining']:
            raise UsageFailure('azure_budget_exhausted')
        self.state['receipts'][receipt_id] = {
            'runId': self.run_id, 'component': self.component, 'requestHash': key,
            'identity': private_identity, 'date': now.astimezone(JST).date().isoformat(),
            'reservedAt': _stamp(now), 'issuedAt': None, 'completedAt': None,
            'reason': 'azure_interrupted', 'httpStatus': None, 'retryAt': None,
        }
        self.state['nextRequestAt'] = _stamp(now + dt.timedelta(seconds=SPACING_SECONDS))
        self._save()
        self._owned.add(receipt_id)
        self._active, self._http = receipt_id, (None, None)

    def issued(self, key):
        self._require_open()
        _hash(key)
        receipt_id = _receipt_id(self.component, key)
        if receipt_id not in self._owned or receipt_id != self._active:
            raise UsageFailure('azure_interrupted')
        receipt = self.state['receipts'][receipt_id]
        if receipt['issuedAt'] is not None or receipt['completedAt'] is not None:
            raise UsageFailure('azure_interrupted')
        self._allowed()
        now = _now(self.clock())
        if self.state['retryAt'] is not None and _time(self.state['retryAt']) > now:
            raise UsageFailure('azure_backoff', retry_at=self.state['retryAt'])
        if now < _time(receipt['reservedAt']):
            raise UsageFailure('azure_backoff', retry_at=receipt['reservedAt'])
        day = now.astimezone(JST).date().isoformat()
        if day != receipt['date'] and _counts(self.state, self.run_id, now)['day'] >= DAY_LIMIT:
            raise UsageFailure('azure_budget_exhausted')
        receipt['date'], receipt['issuedAt'] = day, _stamp(now)
        self.state['nextRequestAt'] = _stamp(now + dt.timedelta(seconds=SPACING_SECONDS))
        self._save()
        self._allowed()
        after_save = _now(self.clock())
        if after_save < now or after_save.astimezone(JST).date().isoformat() != day:
            raise UsageFailure('azure_interrupted')

    def finish(self, key, reason):
        self._require_open()
        _hash(key)
        if not isinstance(reason, str) or reason not in REASONS:
            raise ValueError('invalid_ai_usage_reason')
        receipt_id = _receipt_id(self.component, key)
        if receipt_id not in self._owned:
            raise UsageFailure('azure_interrupted')
        receipt = self.state['receipts'][receipt_id]
        if receipt['completedAt'] is not None:
            if receipt['reason'] == reason:
                return
            raise UsageFailure('azure_interrupted')
        if reason in SUCCESS_REASONS and receipt['issuedAt'] is None:
            raise UsageFailure('azure_interrupted')
        receipt['reason'] = reason
        receipt['completedAt'] = _stamp(max(_now(self.clock()), _time(receipt['issuedAt'] or receipt['reservedAt'])))
        receipt['httpStatus'], receipt['retryAt'] = self._http
        self._save()
        self._active = None

    def http_failure(self, status, retry_after):
        self._require_open()
        if type(status) is not int or not 100 <= status <= 599:
            raise ValueError('invalid_ai_http_status')
        now, retry = _now(self.clock()), None
        if status in (401, 403):
            self.state['paused'] = {'reason': 'azure_auth_stopped', 'httpStatus': status, 'at': _stamp(now)}
            reason = 'azure_auth_stopped'
        elif status == 429:
            until = now + dt.timedelta(minutes=5)
            if retry_after:
                try:
                    parsed = now + dt.timedelta(seconds=int(retry_after))
                except (ValueError, TypeError, OverflowError):
                    try:
                        parsed = _now(email.utils.parsedate_to_datetime(retry_after))
                    except (ValueError, TypeError, OverflowError, AttributeError):
                        parsed = until
                until = max(until, parsed)
            if self.state['retryAt'] is not None:
                until = max(until, _time(self.state['retryAt']))
            retry = self.state['retryAt'] = _stamp(until)
            reason = 'azure_rate_limited'
        else:
            reason = 'azure_http_error'
        self._http = (status, retry)
        self._save()
        raise UsageFailure(reason, status, retry)
