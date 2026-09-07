"""Strict half-month facts, identity checks and non-destructive schedule union."""
import calendar
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import re


UTC = dt.timezone.utc
JST = dt.timezone(dt.timedelta(hours=9))
LEGACY_VERSION = 'half-month-schedule-v1'
VERSION = 'half-month-schedule-v2'
TIMING_VERSION = 'half-month-timing-v1'
MODEL, MODEL_VERSION = 'gpt-5.6-luna', '2026-07-09'
HEX = re.compile(r'[a-f0-9]{64}\Z')
ID = re.compile(r'[1-9][0-9]{0,24}\Z')
HANDLE = re.compile(r'[A-Za-z0-9_]{1,15}\Z')
NAME = re.compile(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}\Z')
STATUSES = {'never', 'ok', 'partial', 'unavailable', 'no-new', 'no-results',
            'paused', 'budget-exhausted', 'outside-window'}
SOURCE_STATUSES = {'pending', 'issued', 'valid', 'negative', 'failed'}
TIMING_STORAGE_LIMIT_REASON = 'work_timing_storage_limit'
CAPACITY_HOLD_REASONS = {'azure_capacity_hold', 'azure_capacity_profile_stale'}
REASONS = {
    'not_searched', 'account_unknown', 'account_ambiguous', 'account_identity_mismatch',
    'no_candidates', 'post_unverified', 'not_issued', 'period_unknown', 'schedule_pending',
    'not_schedule', 'budget_wait', 'paused', 'valid_schedule', 'queue_limit',
    'candidate_limit', 'outside_period', 'source_failed', 'analysis_failed',
    'known_source', 'search_failed', 'not_due', 'stale_candidate', TIMING_STORAGE_LIMIT_REASON,
    *CAPACITY_HOLD_REASONS,
}
PUBLIC_FIELDS = {'schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt', 'schedules', 'lastRun'}
PRIVATE_FIELDS = {'identityBindings', 'revisions', 'sources', 'pending', 'coverage', 'receipts',
                  'candidateHistory', 'savedImports'}
SAVED_IMPORT_HASH_FIELDS = ('receiptId', 'requestHash', 'usageReceiptId', 'usageSourceHash',
                           'sourceManifestHash', 'analysisResultHash', 'analysisReceiptHash')
SCHEDULE_FIELDS = {'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt',
                   'observedAt', 'sourceKind', 'period', 'days'}
_CONTRACT_FINGERPRINTS = None
_TIMING_MODULE = None


def load_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def timing():
    global _TIMING_MODULE
    if _TIMING_MODULE is None:
        _TIMING_MODULE = load_module('work-timing.py', 'half_month_work_timing')
    return _TIMING_MODULE


def require_keys(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('invalid_schedule_fields')


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def valid_hash(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError('invalid_schedule_hash')
    return value


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value):
        raise ValueError('invalid_schedule_timestamp')
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def stamp(value):
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ValueError('invalid_schedule_clock')
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('invalid_schedule_date')
    return dt.date.fromisoformat(value)


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('invalid_schedule_id')
    return value


def post_identifier(value):
    identifier(value)
    if len(value) < 10:
        raise ValueError('invalid_schedule_id')
    return value


def validate_publication(tid, created, observed):
    post_identifier(tid)
    posted, checked = timestamp(created), timestamp(observed)
    try:
        snowflake = dt.datetime(1970, 1, 1, tzinfo=UTC) + dt.timedelta(
            milliseconds=(int(tid) >> 22) + 1288834974657)
    except OverflowError:
        raise ValueError('invalid_schedule_chronology') from None
    if checked < posted or abs((posted - snowflake).total_seconds()) >= 2:
        raise ValueError('invalid_schedule_chronology')
    return posted


def identity(name, handle, uid=None):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('invalid_schedule_name')
    if not isinstance(handle, str) or not HANDLE.fullmatch(handle):
        raise ValueError('invalid_schedule_handle')
    if uid is not None:
        identifier(uid)


def public_url(handle, tid):
    identity('test', handle)
    post_identifier(tid)
    return f'https://x.com/{handle}/status/{tid}'


def half_period(date):
    first = date.replace(day=1 if date.day <= 15 else 16)
    last = date.replace(day=15 if date.day <= 15 else calendar.monthrange(date.year, date.month)[1])
    return first.isoformat(), last.isoformat()


def target_periods(now):
    date = now.astimezone(JST).date() if isinstance(now, dt.datetime) else now
    current = half_period(date)
    following = half_period(day(current[1]) + dt.timedelta(days=1))
    return [current, following]


def candidate_start(now):
    current = target_periods(now)[0]
    return day(half_period(day(current[0]) - dt.timedelta(days=1))[0])


def validate_period(value):
    require_keys(value, ('from', 'to', 'printedYear', 'yearBasis'))
    start, end = day(value['from']), day(value['to'])
    if half_period(start) != (value['from'], value['to']):
        raise ValueError('invalid_half_period')
    year, basis = value['printedYear'], value['yearBasis']
    if (basis not in ('printed', 'text', 'post-context')
            or year is not None and (type(year) is not int or year != start.year)
            or (basis == 'printed') != (year is not None)):
        raise ValueError('invalid_year_basis')
    return start, end


def validate_schedule(value):
    require_keys(value, SCHEDULE_FIELDS | ({'workTiming'} if isinstance(value, dict)
                                         and 'workTiming' in value else set()))
    identity(value['name'], value['authorScreenName'], value['authorId'])
    identifier(value['id'])
    if value['url'] != public_url(value['authorScreenName'], value['id']):
        raise ValueError('invalid_schedule_url')
    created = validate_publication(value['id'], value['createdAt'], value['observedAt'])
    if value['sourceKind'] != 'half-month-schedule':
        raise ValueError('invalid_schedule_source')
    start, end = validate_period(value['period'])
    if value['period']['yearBasis'] == 'post-context':
        local = created.astimezone(JST)
        if abs((start.year * 12 + start.month) - (local.year * 12 + local.month)) > 1:
            raise ValueError('invalid_schedule_post_context')
    days = value['days']
    if not isinstance(days, list) or not 1 <= len(days) <= 16:
        raise ValueError('invalid_schedule_days')
    seen = set()
    for entry in days:
        require_keys(entry, ('date', 'shifts'))
        when = day(entry['date'])
        shifts = entry['shifts']
        if (not start <= when <= end or when in seen or not isinstance(shifts, list)
                or not shifts or len(shifts) > 2
                or any(shift not in ('昼', '夜') for shift in shifts)
                or len(set(shifts)) != len(shifts)):
            raise ValueError('invalid_schedule_day')
        seen.add(when)
    if 'workTiming' in value:
        timing().validate(value['workTiming'], owner=value,
                          days={row['date']: row['shifts'] for row in days},
                          source_kind='half-month-schedule')
    return value


def validate_candidate(value):
    require_keys(value, ('id', 'url', 'name', 'authorId', 'authorScreenName',
                         'searchCreatedAt', 'discoveredAt', 'discoveryHash', 'priority',
                         'lastAttemptAt', 'nextAttemptAt'))
    identity(value['name'], value['authorScreenName'], value['authorId'])
    if value['url'] != public_url(value['authorScreenName'], value['id']):
        raise ValueError('invalid_schedule_candidate')
    validate_publication(value['id'], value['searchCreatedAt'], value['discoveredAt'])
    valid_hash(value['discoveryHash'])
    if type(value['priority']) is not int or value['priority'] not in (0, 1):
        raise ValueError('invalid_schedule_candidate')
    for field in ('lastAttemptAt', 'nextAttemptAt'):
        if value[field] is not None:
            timestamp(value[field])


def candidate_key(value):
    validate_candidate(value)
    return digest({key: value[key] for key in ('id', 'authorId', 'authorScreenName', 'searchCreatedAt')})


def validate_source(value):
    require_keys(value, ('id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt',
                         'observedAt', 'editTweetIds', 'bodyHash', 'payloadHash',
                         'discoveryHash', 'media'))
    identity(value['name'], value['authorScreenName'], value['authorId'])
    if value['url'] != public_url(value['authorScreenName'], value['id']):
        raise ValueError('invalid_schedule_source')
    validate_publication(value['id'], value['createdAt'], value['observedAt'])
    for field in ('bodyHash', 'payloadHash', 'discoveryHash'):
        valid_hash(value[field])
    edits = value['editTweetIds']
    if (not isinstance(edits, list) or not 1 <= len(edits) <= 8
            or edits[-1] != value['id'] or len(set(edits)) != len(edits)):
        raise ValueError('invalid_schedule_edits')
    for tid in edits:
        post_identifier(tid)
    if edits != sorted(edits, key=int):
        raise ValueError('invalid_schedule_edits')
    if not isinstance(value['media'], list) or len(value['media']) > 4:
        raise ValueError('invalid_schedule_media')
    seen = set()
    for media in value['media']:
        require_keys(media, ('urlHash', 'originalWidth', 'originalHeight'))
        valid_hash(media['urlHash'])
        if media['urlHash'] in seen:
            raise ValueError('duplicate_schedule_media')
        seen.add(media['urlHash'])
        if any(type(media[key]) is not int or not 1 <= media[key] <= 65535
               for key in ('originalWidth', 'originalHeight')):
            raise ValueError('invalid_schedule_media')
    return value


def source_key(source):
    validate_source(source)
    # Purpose/model changes must not re-enable an already attempted input.
    return digest({key: source[key] for key in ('authorId', 'id', 'editTweetIds', 'bodyHash', 'media')})


def validate_analysis(value):
    global _CONTRACT_FINGERPRINTS
    timing_only = isinstance(value, dict) and value.get('contract') == TIMING_VERSION
    require_keys(value, ('contract', 'model', 'modelVersion', 'promptHash', 'schemaHash',
                         'contextHash', 'requestHash', 'resultHash', 'receiptId', 'analyzedAt', 'images',
                         *(('timingOnly',) if timing_only else ())))
    if (value['contract'] not in (LEGACY_VERSION, VERSION, TIMING_VERSION)
            or (value['model'], value['modelVersion']) != (MODEL, MODEL_VERSION)):
        raise ValueError('invalid_schedule_contract')
    for field in ('promptHash', 'schemaHash', 'contextHash', 'requestHash', 'resultHash', 'receiptId'):
        valid_hash(value[field])
    if value['contract'] == VERSION:
        if _CONTRACT_FINGERPRINTS is None:
            contract = load_module('schedule-azure.py', 'half_month_current_contract')
            _CONTRACT_FINGERPRINTS = digest(contract.PROMPT.encode('utf-8')), digest(contract.SCHEMA)
        if (value['promptHash'], value['schemaHash']) != _CONTRACT_FINGERPRINTS:
            raise ValueError('invalid_schedule_contract_hash')
    if timing_only:
        load_module('half-month-timing.py', 'half_month_timing_contract').validate_analysis_binding(value)
    timestamp(value['analyzedAt'])
    if not isinstance(value['images'], list) or len(value['images']) > 4:
        raise ValueError('invalid_schedule_images')
    total_bytes = total_pixels = 0
    for image in value['images']:
        require_keys(image, ('sha256', 'mime', 'bytes', 'width', 'height'))
        valid_hash(image['sha256'])
        if (image['mime'] not in ('image/jpeg', 'image/png')
                or type(image['bytes']) is not int or not 1 <= image['bytes'] <= 8 * 1024 * 1024
                or any(type(image[key]) is not int or not 1 <= image[key] <= 8192
                       for key in ('width', 'height'))
                or image['width'] * image['height'] > 20_000_000):
            raise ValueError('invalid_schedule_image')
        total_bytes += image['bytes']
        total_pixels += image['width'] * image['height']
    if total_bytes > 12 * 1024 * 1024 or total_pixels > 40_000_000:
        raise ValueError('invalid_schedule_images')


def empty_state():
    return {'schemaVersion': 1, 'complete': False, 'checkedAt': None, 'lastSuccessAt': None,
            'schedules': [], 'lastRun': {'status': 'never'}, 'identityBindings': {},
            'revisions': {}, 'sources': {}, 'pending': [], 'coverage': {}, 'receipts': {},
            'candidateHistory': {}, 'savedImports': {}}


def contract_hash(analysis):
    validate_analysis(analysis)
    return digest({key: analysis[key] for key in (
        'contract', 'model', 'modelVersion', 'promptHash', 'schemaHash')})


def core_hash(schedules):
    return digest([{key: copy.deepcopy(row[key]) for key in sorted(SCHEDULE_FIELDS - {'observedAt'})}
                   for row in sorted(schedules, key=lambda item: (item['name'], item['period']['from']))])


def timing_hash(schedules):
    return digest([{'name': row['name'], 'from': row['period']['from'],
                    'workTiming': row.get('workTiming')}
                   for row in sorted(schedules, key=lambda item: (item['name'], item['period']['from']))])


TIMING_AMENDMENT_FIELDS = ('operation', 'previous', 'expectedCoreHash', 'expectedTimingHash',
                          'targetScopes', 'updateChannels', 'basisRevisionKeys')


def validate_timing_authorization(value, *, with_import=True):
    require_keys(value, (*TIMING_AMENDMENT_FIELDS, 'expectedSubjectHash',
                         *(('importId',) if with_import else ())))
    if value['operation'] != 'work-timing-only' or value['updateChannels'] != ['workTiming']:
        raise ValueError('invalid_schedule_timing_operation')
    for field in ('expectedSubjectHash', 'expectedCoreHash', 'expectedTimingHash',
                  *(('importId',) if with_import else ())):
        valid_hash(value[field])
    basis = value['basisRevisionKeys']
    if not isinstance(basis, list) or not 1 <= len(basis) <= 4096:
        raise ValueError('invalid_schedule_timing_basis')
    for key in basis:
        valid_hash(key)
    if basis != sorted(set(basis)):
        raise ValueError('invalid_schedule_timing_basis')
    previous = value['previous']
    native = isinstance(previous, dict) and previous.get('kind') == 'native'
    require_keys(previous, ('contractVersion', 'contractHash', 'analysisReceiptId',
                            'kind' if native else 'importId'))
    if previous['contractVersion'] not in (LEGACY_VERSION, VERSION, TIMING_VERSION):
        raise ValueError('invalid_schedule_previous_contract')
    for field in ('contractHash', 'analysisReceiptId', *(('importId',) if not native else ())):
        valid_hash(previous[field])
    scopes = value['targetScopes']
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 128:
        raise ValueError('invalid_schedule_target_scopes')
    seen = set()
    for item in scopes:
        require_keys(item, ('name', 'serviceDate', 'shift', 'boundary'))
        identity(item['name'], 'scope')
        day(item['serviceDate'])
        if item['shift'] not in ('昼', '夜') or item['boundary'] not in ('start', 'end'):
            raise ValueError('invalid_schedule_target_scopes')
        key = (item['name'], item['serviceDate'], item['shift'], item['boundary'])
        if key in seen:
            raise ValueError('invalid_schedule_target_scopes')
        seen.add(key)
    return seen


def _post_order(schedule):
    return timestamp(schedule['createdAt']), int(schedule['id'])


def _pair(schedule):
    return schedule['name'], schedule['period']['from']


def _project(selected):
    result = {}
    for revision in sorted(selected, key=lambda item: (
            *_post_order(item['schedule']), *_pair(item['schedule']))):
        row = copy.deepcopy(revision['schedule'])
        old = result.get(_pair(row))
        if 'workTiming' in row or old is not None and 'workTiming' in old:
            row['workTiming'] = timing().merge(
                old.get('workTiming') if old else None, row.get('workTiming'),
                days={entry['date']: entry['shifts'] for entry in row['days']})
        result[_pair(row)] = row
    return result


def projection_basis(state, receipt_id):
    """Freeze the exact selected revisions contributing to a predecessor projection."""
    prior_keys = state['receipts'].get(receipt_id, [])
    if not prior_keys or any(key not in state['revisions'] for key in prior_keys):
        raise ValueError('schedule_timing_parent_missing')
    targets = {_pair(state['revisions'][key]['schedule']):
               _post_order(state['revisions'][key]['schedule']) for key in prior_keys}
    return sorted(key for key, revision in select_revisions(state)
                  if _pair(revision['schedule']) in targets
                  and _post_order(revision['schedule']) <= targets[_pair(revision['schedule'])])


def _extend_revision_timing(state, channel, revision):
    update = revision['schedule'].get('workTiming')
    selected_update = 'selectionProof' in state.get('savedImports', {}).get(
        revision['timingAmendment']['importId'], {})
    # Replay historical empty-channel creation, while keeping selected no-info scopes untouched.
    if selected_update and channel is None and update is None:
        return None
    return timing().merge(channel, update)


def _effective_revision(state, key):
    revisions, chain, visited = state['revisions'], [], set()
    current = key
    while True:
        if current not in revisions or current in visited:
            raise ValueError('invalid_schedule_timing_basis')
        visited.add(current)
        ancestor = revisions[current]
        chain.append(ancestor)
        auth = ancestor.get('timingAmendment')
        if auth is None:
            break
        parents = [parent for parent in state['receipts'].get(auth['previous']['analysisReceiptId'], [])
                   if parent in revisions
                   and _pair(revisions[parent]['schedule']) == _pair(revisions[key]['schedule'])]
        if len(parents) != 1:
            raise ValueError('invalid_schedule_timing_basis')
        current = parents[0]
    effective = copy.deepcopy(revisions[key])
    channel = copy.deepcopy(chain[-1]['schedule'].get('workTiming'))
    for ancestor in reversed(chain[:-1]):
        channel = _extend_revision_timing(state, channel, ancestor)
    if channel is not None:
        effective['schedule']['workTiming'] = channel
    return effective


def select_revisions(state):
    """Resolve explicit same-post lineage, then original publication order only."""
    revisions = state['revisions']
    groups, children, authorizations = {}, {}, {}
    for key, revision in revisions.items():
        row = revision['schedule']
        groups.setdefault((*_pair(row), row['id']), []).append(key)
        auth = revision.get('timingAmendment')
        if auth is None:
            continue
        allowed = validate_timing_authorization(auth)
        imported = state.get('savedImports', {}).get(auth['importId'])
        previous = auth['previous']
        native = previous.get('kind') == 'native'
        prior_import = state.get('savedImports', {}).get(previous.get('importId'))
        if (not isinstance(imported, dict)
                or imported['receiptId'] != revision['analysis']['receiptId']
                or imported['receiptId'] == previous['analysisReceiptId']):
            raise ValueError('schedule_timing_import_lineage')
        if native:
            if any(item['receiptId'] == previous['analysisReceiptId']
                   for item in state.get('savedImports', {}).values()):
                raise ValueError('schedule_timing_native_lineage')
        elif (not isinstance(prior_import, dict)
              or prior_import['receiptId'] != previous['analysisReceiptId']
              or imported['usageReceiptId'] == prior_import['usageReceiptId']):
            raise ValueError('schedule_timing_import_lineage')
        prior_keys = state['receipts'].get(previous['analysisReceiptId'], [])
        parents = [parent for parent in prior_keys if parent in revisions
                   and _pair(revisions[parent]['schedule']) == _pair(row)]
        if len(parents) != 1:
            raise ValueError('schedule_timing_parent_missing')
        parent_key = parents[0]
        parent = revisions[parent_key]
        prior_analysis = parent['analysis']
        if (row['id'] != parent['schedule']['id']
                or previous['contractVersion'] != prior_analysis['contract']
                or previous['contractHash'] != contract_hash(prior_analysis)
                or revision['analysis']['contract'] not in (VERSION, TIMING_VERSION)
                or timestamp(revision['analysis']['analyzedAt']) < timestamp(prior_analysis['analyzedAt'])):
            raise ValueError('schedule_timing_contract_lineage')
        before_source = parent.get('source', state['sources'][parent['sourceKey']]['source'])
        source = revision.get('source', state['sources'][revision['sourceKey']]['source'])
        if (any(source[field] != before_source[field] for field in (
                'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt',
                'bodyHash', 'media', 'editTweetIds'))
                or [image['sha256'] for image in revision['analysis']['images']]
                != [image['sha256'] for image in prior_analysis['images']]):
            raise ValueError('schedule_timing_source_changed')
        if revision['analysis']['contract'] == TIMING_VERSION and source != before_source:
            raise ValueError('schedule_timing_source_changed')
        if core_hash([row]) != core_hash([parent['schedule']]):
            raise ValueError('schedule_timing_core_changed')
        if parent_key in children and children[parent_key] != key:
            raise ValueError('schedule_timing_branch')
        children[parent_key] = key
        for fact in row.get('workTiming', {}).get('facts', []):
            if (row['name'], *timing().scope(fact)) not in allowed:
                raise ValueError('schedule_timing_scope_changed')
        authorizations.setdefault(auth['importId'], []).append(key)
    selected = []
    for keys in groups.values():
        roots = [key for key in keys if 'timingAmendment' not in revisions[key]]
        if not roots:
            raise ValueError('schedule_timing_cycle')
        root = min(roots, key=lambda key: (timestamp(revisions[key]['schedule']['observedAt']), key))
        for key in roots:
            before, after = revisions[root]['schedule'], revisions[key]['schedule']
            if core_hash([before]) != core_hash([after]) or timing_hash([before]) != timing_hash([after]):
                raise ValueError('schedule_same_revision_conflict')
        amended_roots = [key for key in roots if key in children]
        if len(amended_roots) > 1:
            raise ValueError('schedule_timing_branch')
        current = amended_roots[0] if amended_roots else root
        visited = set(roots)
        channel = revisions[current]['schedule'].get('workTiming')
        while current in children:
            current = children[current]
            if current in visited:
                raise ValueError('schedule_timing_cycle')
            visited.add(current)
            channel = _extend_revision_timing(state, channel, revisions[current])
        if visited != set(keys):
            raise ValueError('schedule_timing_branch')
        effective = copy.deepcopy(revisions[current])
        if channel is not None:
            effective['schedule']['workTiming'] = channel
        selected.append((current, effective))
    for import_id, keys in authorizations.items():
        auth = revisions[keys[0]]['timingAmendment']
        if any(revisions[key]['timingAmendment'] != auth for key in keys):
            raise ValueError('schedule_timing_authorization_conflict')
        previous_keys = state['receipts'][auth['previous']['analysisReceiptId']]
        prior_rows = [revisions[key]['schedule'] for key in previous_keys]
        new_rows = [revisions[key]['schedule'] for key in keys]
        imported = state['savedImports'][import_id]
        receipt_keys = state['receipts'][imported['receiptId']]
        if set(receipt_keys) != set(keys):
            raise ValueError('schedule_timing_receipt_lineage')
        first = revisions[receipt_keys[0]]
        source = first.get('source', state['sources'][first['sourceKey']]['source'])
        timing_contract = first['analysis']['contract'] == TIMING_VERSION
        if timing_contract and imported.get('accountingKind') not in ('native', 'imported'):
            raise ValueError('invalid_schedule_saved_accounting')
        proof_fields = ('usageReceiptId', 'usageSourceHash', 'sourceManifestHash',
                        'analysisResultHash', 'analysisReceiptHash', 'issuedAt',
                        *(('accountingKind',) if timing_contract else ()))
        amendment = {field: copy.deepcopy(auth[field]) for field in TIMING_AMENDMENT_FIELDS}
        amendment.update(source=source, schedules=[revisions[key]['schedule'] for key in receipt_keys],
                         analysis=first['analysis'],
                         proof={**{field: imported[field] for field in proof_fields},
                                'searchCreatedAt': source['createdAt']})
        if 'selectionProof' in imported:
            if not timing_contract or not isinstance(imported['selectionProof'], dict):
                raise ValueError('timing_selection_contract')
            amendment['selectionProof'] = imported['selectionProof']
        if digest(amendment) != import_id:
            raise ValueError('schedule_timing_amendment_hash')
        if core_hash(prior_rows) != auth['expectedCoreHash'] or core_hash(new_rows) != auth['expectedCoreHash']:
            raise ValueError('schedule_timing_core_changed')
        allowed = validate_timing_authorization(auth)
        for name, date, shift, _ in allowed:
            if not any(row['name'] == name and any(
                    entry['date'] == date and shift in entry['shifts'] for entry in row['days'])
                       for row in prior_rows):
                raise ValueError('schedule_timing_scope_changed')
        basis = auth['basisRevisionKeys']
        targets = {_pair(row): _post_order(row) for row in prior_rows}
        if not set(previous_keys) <= set(basis) or any(key not in revisions for key in basis):
            raise ValueError('invalid_schedule_timing_basis')
        groups_seen = set()
        for key in basis:
            row = revisions[key]['schedule']
            group = (*_pair(row), row['id'])
            if (_pair(row) not in targets or _post_order(row) > targets[_pair(row)]
                    or group in groups_seen or key in keys):
                raise ValueError('invalid_schedule_timing_basis')
            groups_seen.add(group)
        # Later discoveries participate in today's projection, never in a historical approval hash.
        projection = _project([_effective_revision(state, key) for key in basis])
        target_rows = [projection[_pair(row)] for row in prior_rows]
        if timing_hash(target_rows) != auth['expectedTimingHash']:
            raise ValueError('schedule_timing_stale_hash')
        if first['analysis']['contract'] == TIMING_VERSION:
            timing_product = load_module('half-month-timing.py', 'half_month_timing_binding')
            timing_product.validate_bound_result(
                first['analysis'], auth, source, prior_rows,
                [revisions[key]['schedule'] for key in receipt_keys])
            timing_product.validate_selection_record(
                first['analysis'], auth, source, amendment['schedules'], imported.get('selectionProof'))
            if 'selectionProof' in imported:
                timing_product.validate_selection_apply_approval(
                    imported.get('applyApproval'),
                    {'expectedSubjectHash': auth['expectedSubjectHash'], 'amendment': amendment})
    return selected


def validate_state(value, private=True):
    fields = PUBLIC_FIELDS | PRIVATE_FIELDS if private else PUBLIC_FIELDS
    if private and isinstance(value, dict) and 'savedImports' not in value:
        fields = fields - {'savedImports'}
    require_keys(value, fields)
    if type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or value['complete'] is not False:
        raise ValueError('invalid_schedule_state')
    for key in ('checkedAt', 'lastSuccessAt'):
        if value[key] is not None:
            timestamp(value[key])
    if value['lastSuccessAt'] is not None and (value['checkedAt'] is None or
            timestamp(value['lastSuccessAt']) > timestamp(value['checkedAt'])):
        raise ValueError('invalid_schedule_check_chronology')
    require_keys(value['lastRun'], ('status',))
    if not isinstance(value['lastRun']['status'], str) or value['lastRun']['status'] not in STATUSES:
        raise ValueError('invalid_schedule_status')
    if not isinstance(value['schedules'], list):
        raise ValueError('invalid_schedule_state')
    keys, by_post = set(), {}
    for schedule in value['schedules']:
        validate_schedule(schedule)
        key = (schedule['name'], schedule['period']['from'])
        if key in keys:
            raise ValueError('duplicate_current_schedule')
        keys.add(key)
        siblings = by_post.setdefault(schedule['id'], [])
        # Observation times belong to each half, not to the original post identity.
        if len(siblings) >= 2 or any(any(previous[field] != schedule[field] for field in (
                'name', 'url', 'authorId', 'authorScreenName', 'createdAt')) for previous in siblings):
            raise ValueError('inconsistent_schedule_post')
        siblings.append(schedule)
    if not private:
        return value
    for field in ('identityBindings', 'revisions', 'sources', 'coverage', 'receipts', 'candidateHistory'):
        if not isinstance(value[field], dict):
            raise ValueError('invalid_schedule_state')
    bound_ids, bound_handles = set(), set()
    for name, bound in value['identityBindings'].items():
        require_keys(bound, ('authorId', 'authorScreenName', 'verifiedAt'))
        identity(name, bound['authorScreenName'], bound['authorId'])
        timestamp(bound['verifiedAt'])
        if bound['authorId'] in bound_ids or bound['authorScreenName'].casefold() in bound_handles:
            raise ValueError('schedule_binding_ambiguous')
        bound_ids.add(bound['authorId'])
        bound_handles.add(bound['authorScreenName'].casefold())
    for key, record in value['sources'].items():
        require_keys(record, ('source', 'status', 'reason', 'checkedAt', 'requestHash', 'imageHashes'))
        if key != source_key(record['source']) or record['status'] not in SOURCE_STATUSES:
            raise ValueError('invalid_schedule_source_record')
        if record['reason'] not in REASONS:
            raise ValueError('invalid_schedule_reason')
        timestamp(record['checkedAt'])
        if record['requestHash'] is not None:
            valid_hash(record['requestHash'])
        if (not isinstance(record['imageHashes'], list)
                or len(record['imageHashes']) not in (0, len(record['source']['media']))):
            raise ValueError('invalid_schedule_image_hashes')
        for image_hash in record['imageHashes']:
            valid_hash(image_hash)
    for key, revision in value['revisions'].items():
        require_keys(revision, ('schedule', 'sourceKey', 'analysis',
                               *(('source',) if isinstance(revision, dict) and 'source' in revision else ()),
                               *(('timingAmendment',) if isinstance(revision, dict)
                                 and 'timingAmendment' in revision else ())))
        if key != digest(revision):
            raise ValueError('invalid_schedule_revision_hash')
        schedule = validate_schedule(revision['schedule'])
        indexed = value['sources'].get(revision['sourceKey'], {}).get('source')
        if indexed is None:
            raise ValueError('missing_schedule_source')
        source = revision['source'] if 'source' in revision else indexed
        validate_source(source)
        if source_key(source) != revision['sourceKey'] or any(
                source[field] != indexed[field] for field in (
                    'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt')):
            raise ValueError('schedule_source_identity_mismatch')
        if any(source[field] != schedule[field] for field in (
                'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt', 'observedAt')):
            raise ValueError('schedule_source_mismatch')
        validate_analysis(revision['analysis'])
        if revision['analysis']['contract'] == TIMING_VERSION and 'timingAmendment' not in revision:
            raise ValueError('schedule_timing_authorization_required')
        if 'workTiming' in schedule:
            if revision['analysis']['contract'] not in (VERSION, TIMING_VERSION) or any(
                    fact['source'] != timing().source_metadata(source, 'half-month-schedule')
                    for fact in schedule['workTiming']['facts']):
                raise ValueError('schedule_timing_source_mismatch')
        if len(source['media']) != len(revision['analysis']['images']):
            raise ValueError('schedule_images_incomplete')
        bound = value['identityBindings'].get(schedule['name'])
        if not bound or any(bound[field] != source[field] for field in ('authorId', 'authorScreenName')):
            raise ValueError('schedule_binding_mismatch')
        if timestamp(revision['analysis']['analyzedAt']) < timestamp(source['observedAt']):
            raise ValueError('schedule_analysis_chronology')
        if key not in value['receipts'].get(revision['analysis']['receiptId'], []):
            raise ValueError('schedule_receipt_missing')
    winners = _project([revision for _, revision in select_revisions(value)])
    for schedule in value['schedules']:
        if winners.get((schedule['name'], schedule['period']['from'])) != schedule:
            raise ValueError('schedule_revision_missing_or_stale')
    if len(winners) != len(value['schedules']):
        raise ValueError('schedule_current_missing')
    for receipt, keys in value['receipts'].items():
        valid_hash(receipt)
        if not isinstance(keys, list) or not 1 <= len(keys) <= 2 or len(set(keys)) != len(keys):
            raise ValueError('invalid_schedule_receipt')
        if any(key not in value['revisions'] or
               value['revisions'][key]['analysis']['receiptId'] != receipt for key in keys):
            raise ValueError('invalid_schedule_receipt')
    imports = value.get('savedImports', {})
    if not isinstance(imports, dict):
        raise ValueError('invalid_schedule_saved_import')
    for amendment_hash, imported in imports.items():
        valid_hash(amendment_hash)
        require_keys(imported, (*SAVED_IMPORT_HASH_FIELDS, 'issuedAt', 'importedAt',
                               *(('accountingKind',) if isinstance(imported, dict)
                                 and 'accountingKind' in imported else ()),
                               *(('selectionProof',) if isinstance(imported, dict)
                                 and 'selectionProof' in imported else ()),
                               *(('applyApproval',) if isinstance(imported, dict)
                                 and 'applyApproval' in imported else ())))
        for field in SAVED_IMPORT_HASH_FIELDS:
            valid_hash(imported[field])
        if timestamp(imported['importedAt']) < timestamp(imported['issuedAt']):
            raise ValueError('invalid_schedule_saved_import_chronology')
        linked = value['receipts'].get(imported['receiptId'])
        if not linked:
            raise ValueError('schedule_saved_import_receipt_missing')
        timing_contract = any(value['revisions'][key]['analysis']['contract'] == TIMING_VERSION for key in linked)
        if (timing_contract != ('accountingKind' in imported)
                or timing_contract and imported['accountingKind'] not in ('native', 'imported')):
            raise ValueError('invalid_schedule_saved_accounting')
        if 'selectionProof' in imported and not timing_contract:
            raise ValueError('timing_selection_contract')
        if ('selectionProof' in imported) != ('applyApproval' in imported):
            raise ValueError('timing_selection_apply_approval_required')
        if any(value['revisions'][key]['analysis']['requestHash'] != imported['requestHash']
               for key in linked):
            raise ValueError('schedule_saved_import_request_mismatch')
    if not isinstance(value['pending'], list) or len(value['pending']) > 240:
        raise ValueError('invalid_schedule_queue')
    ids, names = set(), {}
    for candidate in value['pending']:
        validate_candidate(candidate)
        if candidate['id'] in ids:
            raise ValueError('duplicate_schedule_candidate')
        ids.add(candidate['id'])
        name = candidate['name']
        names[name] = names.get(name, 0) + 1
        if names[name] > 6:
            raise ValueError('invalid_schedule_queue')
    for key, item in value['candidateHistory'].items():
        require_keys(item, ('candidate', 'reason', 'checkedAt'))
        if key != candidate_key(item['candidate']) or item['reason'] not in REASONS:
            raise ValueError('invalid_schedule_candidate_history')
        timestamp(item['checkedAt'])
    for start, people in value['coverage'].items():
        if not isinstance(people, dict) or half_period(day(start))[0] != start:
            raise ValueError('invalid_schedule_coverage')
        for name, row in people.items():
            require_keys(row, ('name', 'handle', 'to', 'lastSearchedAt', 'nextCheckAt',
                               'candidateIds', 'confirmedIds', 'reason'))
            if row['name'] != name or not NAME.fullmatch(name) or row['reason'] not in REASONS:
                raise ValueError('invalid_schedule_coverage')
            if row['handle'] is not None:
                identity(name, row['handle'])
            if half_period(day(start))[1] != row['to']:
                raise ValueError('invalid_schedule_coverage')
            for field in ('lastSearchedAt', 'nextCheckAt'):
                if row[field] is not None:
                    timestamp(row[field])
            for field in ('candidateIds', 'confirmedIds'):
                if not isinstance(row[field], list) or len(row[field]) > 6:
                    raise ValueError('invalid_schedule_coverage')
                for tid in row[field]:
                    identifier(tid)
                if len(set(row[field])) != len(row[field]):
                    raise ValueError('invalid_schedule_coverage')
    return value


def read_state(path, private=True):
    # A missing authoritative state is not an empty migration.
    transport = load_module('azure-openai.py', 'schedule_json')
    value = transport.strict_json(Path(path).read_bytes())
    return validate_state(value, private)


def public_state(state):
    if not isinstance(state, dict):
        raise ValueError('invalid_schedule_state')
    validate_state(state, private=bool(PRIVATE_FIELDS & set(state)))
    return copy.deepcopy({key: state[key] for key in PUBLIC_FIELDS})


def bind_identity(state, source):
    validate_source(source)
    bound = state['identityBindings'].get(source['name'])
    if bound and any(bound[field] != source[field] for field in ('authorId', 'authorScreenName')):
        raise ValueError('schedule_binding_mismatch')
    for name, previous in state['identityBindings'].items():
        if name != source['name'] and (previous['authorId'] == source['authorId'] or
                previous['authorScreenName'].casefold() == source['authorScreenName'].casefold()):
            raise ValueError('schedule_binding_ambiguous')
    if not bound:
        state['identityBindings'][source['name']] = {
            'authorId': source['authorId'], 'authorScreenName': source['authorScreenName'],
            'verifiedAt': source['observedAt']}


def record_source(state, source, status, reason, now, request_hash=None, image_hashes=None):
    validate_source(source)
    if status not in SOURCE_STATUSES or reason not in REASONS:
        raise ValueError('invalid_schedule_source_record')
    if request_hash is not None:
        valid_hash(request_hash)
    key = source_key(source)
    previous_source = state['sources'].get(key, {}).get('source')
    if previous_source is not None and any(source[field] != previous_source[field] for field in (
            'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt')):
        raise ValueError('schedule_source_identity_mismatch')
    if image_hashes is None:
        image_hashes = state['sources'].get(key, {}).get('imageHashes', [])
    # Keep the original capture for legacy revisions that reference this index.
    state['sources'][key] = {'source': copy.deepcopy(previous_source if previous_source is not None else source),
                             'status': status, 'reason': reason,
                             'checkedAt': stamp(now), 'requestHash': request_hash,
                             'imageHashes': list(image_hashes)}
    return key


def apply_revision(state, schedules, source, analysis, *, timing_amendment=None, saved_import=None):
    """Atomically retain all revisions; only a newer complete same-half input wins."""
    validate_state(state)
    validate_source(source)
    validate_analysis(analysis)
    if analysis['contract'] == TIMING_VERSION and timing_amendment is None:
        raise ValueError('schedule_timing_authorization_required')
    if not isinstance(schedules, list) or not 1 <= len(schedules) <= 2:
        raise ValueError('schedule_pending')
    for schedule in schedules:
        validate_schedule(schedule)
    if len({(s['name'], s['period']['from']) for s in schedules}) != len(schedules):
        raise ValueError('duplicate_schedule_period')
    working = copy.deepcopy(state)
    bind_identity(working, source)
    selected_mode = saved_import is not None and 'selectionProof' in saved_import[1]
    if selected_mode:
        key = source_key(source)
        validate_timing_authorization(timing_amendment)
        prior_keys = working['receipts'].get(timing_amendment['previous']['analysisReceiptId'], [])
        # The index retains the first capture; approval binds the exact predecessor capture.
        if key not in working['sources'] or not prior_keys or any(
                working['revisions'][prior].get(
                    'source', working['sources'][working['revisions'][prior]['sourceKey']]['source']) != source
                for prior in prior_keys):
            raise ValueError('timing_selection_source_changed')
    else:
        key = record_source(working, source, 'valid', 'valid_schedule',
                            timestamp(analysis['analyzedAt']), analysis['requestHash'],
                            [image['sha256'] for image in analysis['images']])
    revisions = [{'schedule': copy.deepcopy(schedule), 'sourceKey': key, 'source': copy.deepcopy(source),
                  'analysis': copy.deepcopy(analysis)} for schedule in schedules]
    if timing_amendment is not None:
        validate_timing_authorization(timing_amendment)
        for revision in revisions:
            revision['timingAmendment'] = copy.deepcopy(timing_amendment)
    revision_keys = [digest(revision) for revision in revisions]
    receipt = analysis['receiptId']
    previous = state['receipts'].get(receipt)
    if previous is not None:
        old = [state['revisions'][revision_key] for revision_key in previous]
        if len(old) != len(revisions) or any(
                before['schedule'] != after['schedule'] or before['sourceKey'] != after['sourceKey']
                or before['analysis'] != after['analysis']
                or before.get('timingAmendment') != after.get('timingAmendment')
                or before.get('source', state['sources'][before['sourceKey']]['source']) != after['source']
                for before, after in zip(old, revisions)):
            raise ValueError('schedule_receipt_conflict')
        return False
    if timing_amendment is not None and timing_amendment['basisRevisionKeys'] != projection_basis(
            state, timing_amendment['previous']['analysisReceiptId']):
        raise ValueError('schedule_timing_basis_stale')
    for revision_key, revision in zip(revision_keys, revisions):
        working['revisions'][revision_key] = revision
    working['receipts'][receipt] = revision_keys
    if saved_import is not None:
        import_id, imported = saved_import
        if import_id in working.setdefault('savedImports', {}):
            raise ValueError('schedule_saved_import_conflict')
        working['savedImports'][import_id] = copy.deepcopy(imported)
    projected = _project([revision for _, revision in select_revisions(working)])
    if analysis['contract'] == TIMING_VERSION:
        working['schedules'] = copy.deepcopy(state['schedules'])
        updated_pairs = {_pair(row) for row in schedules if row.get('workTiming', {}).get('facts')}
        for row in working['schedules']:
            if selected_mode and _pair(row) not in updated_pairs:
                continue
            update = projected[_pair(row)]
            if 'workTiming' in update:
                row['workTiming'] = copy.deepcopy(update['workTiming'])
    else:
        working['schedules'] = sorted(projected.values(), key=lambda item: (item['period']['from'], item['name']))
    changed = working['schedules'] != state['schedules']
    validate_state(working)
    if selected_mode:
        load_module('half-month-timing.py', 'half_month_selected_delta').validate_selection_delta(
            state, working, analysis, source, schedules, saved_import[1]['selectionProof'])
    state.clear()
    state.update(working)
    return changed


def population(schedule, insights, accounts, bindings=None):
    """Use roster including kitchen; rank/promotion guesses never gate real days."""
    bindings = bindings or {}
    tendencies = insights.get('maidTendency', {})
    aliases = {entry['alias']: name for name, entry in tendencies.items() if entry.get('alias')}
    canonical = lambda name: aliases.get(name, name)
    by_name, by_handle = {}, {}
    for row in accounts:
        name, handle = canonical(row['name']), row.get('handle', '')
        by_name.setdefault(name, []).append(row)
        if handle:
            by_handle.setdefault(handle.casefold(), set()).add(name)
    eligible, reasons = {}, {}
    for original in schedule['roster']:
        name = canonical(original)
        rows = by_name.get(name, [])
        reason, handle = 'account_unknown', None
        if len(rows) > 1:
            reason = 'account_ambiguous'
        elif rows:
            row = rows[0]
            possible = row.get('handle', '')
            if row.get('source') in ('公式サイト', '本人確認済み') and HANDLE.fullmatch(possible):
                known = tendencies.get(name, {}).get('x')
                bound = bindings.get(name)
                if len(by_handle[possible.casefold()]) != 1:
                    reason = 'account_ambiguous'
                elif ((known and known != possible)
                      or bound and bound['authorScreenName'] != possible):
                    reason = 'account_identity_mismatch'
                else:
                    handle, reason = possible, 'not_searched'
                    eligible[name] = {'name': name, 'handle': handle}
        reasons[name] = {'handle': handle, 'reason': reason}
    return eligible, reasons


def effective_schedule(manual_schedule_dict, feed):
    """date -> shift -> entries; manual attributes win, scheduleSources accumulate."""
    validate_state(feed, private=bool(PRIVATE_FIELDS & set(feed)))
    result = copy.deepcopy(manual_schedule_dict)
    for schedule in feed['schedules']:
        source = {key: copy.deepcopy(schedule[key]) for key in (
            'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt',
            'observedAt', 'sourceKind', 'period')}
        for item in schedule['days']:
            for shift in item['shifts']:
                rows = result.setdefault(item['date'], {}).setdefault(shift, [])
                person = next((row for row in rows if row['name'] == schedule['name']), None)
                if person is None:
                    person = {'name': schedule['name']}
                    rows.append(person)
                sources = person.setdefault('scheduleSources', [])
                if source not in sources:
                    sources.append(copy.deepcopy(source))
    return result
