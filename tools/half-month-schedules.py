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
READING_VERSION = 'half-month-reading-v3'
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
    'not_schedule', 'budget_wait', 'paused', 'image_host_paused', 'valid_schedule', 'queue_limit',
    'candidate_limit', 'outside_period', 'source_failed', 'analysis_failed',
    'known_source', 'search_failed', 'not_due', 'stale_candidate', TIMING_STORAGE_LIMIT_REASON,
    'transient_retry', 'retry_exhausted', 'permanent_failure',
    *CAPACITY_HOLD_REASONS,
    'image_cache_unconfigured', 'image_cache_expired', 'reading_pending', 'reading_uncertain',
    'reading_partial', 'reading_outside_period', 'time_limit', 'image_fetch_failed',
    'source_not_found', 'identity_unknown',
    'image_cache_invalid',
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
_REGISTRY_MODULE = None


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


def member_registry():
    global _REGISTRY_MODULE
    if _REGISTRY_MODULE is None:
        _REGISTRY_MODULE = load_module('member-registry.py', 'half_month_member_registry')
    return _REGISTRY_MODULE


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


def discovery_periods(now):
    date = now.astimezone(JST).date() if isinstance(now, dt.datetime) else now
    current, following = target_periods(date)
    return [following, current] if 13 <= date.day <= 15 else [current]


def publication_start(period):
    start = day(period[0])
    return start.replace(day=1 if start.day == 1 else 13)


def searched_in_period(row, period):
    searched = row.get('lastSearchedAt')
    return searched is not None and timestamp(searched).astimezone(JST).date() >= publication_start(period)


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
    optional = {'workTiming', 'reading', 'replyToId', 'replyToAuthorId'}
    require_keys(value, SCHEDULE_FIELDS | (set(value) & optional if isinstance(value, dict) else set()))
    identity(value['name'], value['authorScreenName'], value['authorId'])
    identifier(value['id'])
    if value['url'] != public_url(value['authorScreenName'], value['id']):
        raise ValueError('invalid_schedule_url')
    created = validate_publication(value['id'], value['createdAt'], value['observedAt'])
    if value['sourceKind'] not in ('half-month-schedule', 'own-reply'):
        raise ValueError('invalid_schedule_source')
    validate_reply(value)
    if (value['sourceKind'] == 'own-reply') != ('replyToId' in value):
        raise ValueError('invalid_schedule_reply')
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
                or (not shifts and 'reading' not in value) or len(shifts) > 2
                or any(shift not in ('昼', '夜') for shift in shifts)
                or len(set(shifts)) != len(shifts)):
            raise ValueError('invalid_schedule_day')
        seen.add(when)
    if 'reading' in value:
        validate_reading(value)
    elif value['sourceKind'] == 'own-reply':
        raise ValueError('invalid_schedule_reply')
    if 'workTiming' in value:
        timing().validate(value['workTiming'], owner=value,
                          days={row['date']: row['shifts'] for row in days},
                          source_kind='half-month-schedule')
    return value


def validate_reply(value):
    present = set(value) & {'replyToId', 'replyToAuthorId'}
    if present:
        if len(present) != 2:
            raise ValueError('invalid_schedule_reply')
        post_identifier(value['replyToId'])
        identifier(value['replyToAuthorId'])
        if value['replyToAuthorId'] != value['authorId'] or int(value['replyToId']) >= int(value['id']):
            raise ValueError('invalid_schedule_reply')


def validate_reading(schedule):
    reading = schedule['reading']
    require_keys(reading, ('contract', 'complete', 'days'))
    if (reading['contract'] != READING_VERSION or type(reading['complete']) is not bool
            or not isinstance(reading['days'], dict)
            or set(reading['days']) != {row['date'] for row in schedule['days']}):
        raise ValueError('invalid_schedule_reading')
    reply = schedule['sourceKind'] == 'own-reply'
    if reply and reading['complete']:
        raise ValueError('schedule_reply_requires_amendment')
    for row in schedule['days']:
        fact = reading['days'][row['date']]
        require_keys(fact, ('weekday', 'qualifier', 'hours', 'evidence', 'transcriptionHash',
                           'shiftStatus', *(('operation',) if reply else ())))
        valid_hash(fact['transcriptionHash'])
        if fact['weekday'] not in (None, *'月火水木金土日'):
            raise ValueError('invalid_schedule_weekday')
        if fact['weekday'] is not None and fact['weekday'] != '月火水木金土日'[day(row['date']).weekday()]:
            raise ValueError('invalid_schedule_weekday')
        if fact['qualifier'] not in (None, 'long', 'early', 'late', 'all_day'):
            raise ValueError('invalid_schedule_qualifier')
        if fact['shiftStatus'] not in ('stated', 'unstated', 'unreadable'):
            raise ValueError('invalid_schedule_shift_status')
        cancellation = reply and fact['operation'] == 'cancel'
        if reply and fact['operation'] not in ('add', 'replace', 'cancel'):
            raise ValueError('invalid_schedule_operation')
        if ((not cancellation and bool(row['shifts']) != (fact['shiftStatus'] == 'stated'))
                or cancellation and (fact['shiftStatus'] == 'unreadable'
                                     or row['shifts'] and fact['shiftStatus'] != 'stated')
                or cancellation and (fact['hours'] or fact['qualifier'] is not None)):
            raise ValueError('invalid_schedule_shift_status')
        hours = fact['hours']
        if not isinstance(hours, dict) or set(hours) - {'start', 'end'}:
            raise ValueError('invalid_schedule_hours')
        rules = {('long', ('昼',)): ('12:00', '18:00'),
                 ('early', ('夜',)): ('16:00', '22:00'),
                 ('late', ('夜',)): ('18:00', '22:00')}
        expected = rules.get((fact['qualifier'], tuple(row['shifts'])))
        for key, hour in hours.items():
            require_keys(hour, ('time', 'basis'))
            if (hour['basis'] not in ('explicit', 'qualifier-rule-v1')
                    or not isinstance(hour['time'], str)
                    or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', hour['time'])
                    or hour['basis'] == 'qualifier-rule-v1'
                    and (expected is None or hour['time'] != expected[key == 'end'])):
                raise ValueError('invalid_schedule_hours')
        if fact['qualifier'] == 'all_day' and set(row['shifts']) != {'昼', '夜'}:
            raise ValueError('invalid_schedule_qualifier')
        region = fact['evidence']
        require_keys(region, ('imageIndex', 'box', 'imageHash'))
        index, box = region['imageIndex'], region['box']
        if index is not None and (type(index) is not int or not 0 <= index < 4):
            raise ValueError('invalid_schedule_region')
        if region['imageHash'] is not None:
            valid_hash(region['imageHash'])
        if (index is None) != (region['imageHash'] is None):
            raise ValueError('invalid_schedule_region')
        if box is not None and (index is None or not isinstance(box, list) or len(box) != 4
                or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in box)
                or box[0] >= box[2] or box[1] >= box[3]):
            raise ValueError('invalid_schedule_region')


def validate_candidate(value):
    require_keys(value, ('id', 'url', 'name', 'authorId', 'authorScreenName',
                         'searchCreatedAt', 'discoveredAt', 'discoveryHash', 'priority',
                         'lastAttemptAt', 'nextAttemptAt',
                         *(tuple(sorted(set(value) & {'attempts', 'replyCandidate'}))
                           if isinstance(value, dict) else ())))
    if 'replyCandidate' in value and type(value['replyCandidate']) is not bool:
        raise ValueError('invalid_schedule_candidate')
    if 'attempts' in value and (type(value['attempts']) is not int or not 0 <= value['attempts'] <= 3):
        raise ValueError('invalid_schedule_candidate_attempts')
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
                         'discoveryHash', 'media',
                         *(sorted(set(value) & {'replyToId', 'replyToAuthorId'})
                           if isinstance(value, dict) else ())))
    validate_reply(value)
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
    return digest({key: source[key] for key in (
        'authorId', 'id', 'editTweetIds', 'bodyHash', 'media', 'replyToId', 'replyToAuthorId')
        if key in source})


def validate_analysis(value):
    global _CONTRACT_FINGERPRINTS
    timing_only = isinstance(value, dict) and value.get('contract') == TIMING_VERSION
    require_keys(value, ('contract', 'model', 'modelVersion', 'promptHash', 'schemaHash',
                         'contextHash', 'requestHash', 'resultHash', 'receiptId', 'analyzedAt', 'images',
                         *(('timingOnly',) if timing_only else ()),
                         *(('readingBatch',) if isinstance(value, dict) and 'readingBatch' in value else ())))
    if (value['contract'] not in (LEGACY_VERSION, VERSION, TIMING_VERSION, READING_VERSION)
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
    if value['contract'] == READING_VERSION:
        contract = load_module('schedule-azure.py', 'half_month_reading_contract')
        prompt, schema, _ = contract.contract_parts(READING_VERSION)
        if (value['promptHash'], value['schemaHash']) != (digest(prompt.encode('utf-8')), digest(schema)):
            raise ValueError('invalid_schedule_contract_hash')
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
    if value['contract'] != READING_VERSION and (
            total_bytes > 12 * 1024 * 1024 or total_pixels > 40_000_000):
        raise ValueError('invalid_schedule_images')
    if 'readingBatch' in value:
        batch = value['readingBatch']
        if (value['contract'] != READING_VERSION or not isinstance(batch, list)
                or not 2 <= len(batch) <= 256):
            raise ValueError('invalid_reading_batch')
        seen = set()
        for item in batch:
            if not isinstance(item, dict) or 'readingBatch' in item:
                raise ValueError('invalid_reading_batch')
            validate_analysis(item)
            if (item['contract'] != READING_VERSION or item['images'] != value['images']
                    or item['requestHash'] in seen):
                raise ValueError('invalid_reading_batch')
            seen.add(item['requestHash'])
        if batch[-1] != {key: item for key, item in value.items() if key != 'readingBatch'}:
            raise ValueError('invalid_reading_batch')


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
    return digest([{key: copy.deepcopy(row[key]) for key in sorted(
        (SCHEDULE_FIELDS - {'observedAt'}) | (set(row) & {'reading', 'replyToId', 'replyToAuthorId'}))}
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
    if previous['contractVersion'] not in (LEGACY_VERSION, VERSION, TIMING_VERSION, READING_VERSION):
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


def is_partial(schedule):
    return schedule.get('reading', {}).get('complete') is False


def partial_projection(state):
    rows = {}
    confirmed = {(*_pair(row), row['id']) for row in state['schedules']}
    for revision in sorted(state['revisions'].values(), key=lambda revision: (
            timestamp(revision['analysis']['analyzedAt']),
            timestamp(revision['schedule']['observedAt']), digest(revision))):
        row = revision['schedule']
        key = (*_pair(row), row['id'])
        if is_partial(row) and key not in confirmed:
            old = rows.get(key)
            merged = copy.deepcopy(row)
            if old is not None:
                days = {item['date']: copy.deepcopy(item) for item in old['days']}
                evidence = copy.deepcopy(old['reading']['days'])
                for item in row['days']:
                    previous = days.get(item['date'])
                    if (row['sourceKind'] != 'own-reply' and previous is not None
                            and previous['shifts'] and not item['shifts']):
                        continue
                    days[item['date']] = copy.deepcopy(item)
                    evidence[item['date']] = copy.deepcopy(row['reading']['days'][item['date']])
                merged['days'] = sorted(days.values(), key=lambda item: item['date'])
                merged['reading']['days'] = evidence
            rows[key] = merged
    return sorted(copy.deepcopy(list(rows.values())),
                  key=lambda row: (*_post_order(row), *_pair(row)))


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
        if is_partial(row):
            if 'timingAmendment' in revision:
                raise ValueError('schedule_partial_timing_amendment')
            continue
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
                entry = {'expectedSubjectHash': auth['expectedSubjectHash'], 'amendment': amendment}
                if 'expectedApplySubjectHash' in imported:
                    entry['expectedApplySubjectHash'] = imported['expectedApplySubjectHash']
                timing_product.validate_selection_apply_approval(
                    imported.get('applyApproval'), entry)
    return selected


def validate_state(value, private=True):
    fields = PUBLIC_FIELDS | PRIVATE_FIELDS if private else PUBLIC_FIELDS
    if private and isinstance(value, dict) and 'savedImports' not in value:
        fields = fields - {'savedImports'}
    optional = {'partialSchedules'} | ({'readings', 'collectionSlots', 'collection'} if private else set())
    require_keys(value, fields | (set(value) & optional if isinstance(value, dict) else set()))
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
        if is_partial(schedule):
            raise ValueError('invalid_confirmed_schedule')
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
    partials = value.get('partialSchedules', [])
    if not isinstance(partials, list) or len(partials) > 240:
        raise ValueError('invalid_partial_schedules')
    partial_keys = {(*_pair(schedule), schedule['id']) for schedule in value['schedules']}
    for schedule in partials:
        validate_schedule(schedule)
        key = (*_pair(schedule), schedule['id'])
        if not is_partial(schedule) or key in partial_keys:
            raise ValueError('invalid_partial_schedule')
        partial_keys.add(key)
        siblings = by_post.setdefault(schedule['id'], [])
        if len(siblings) >= 2 or any(any(previous[field] != schedule[field] for field in (
                'name', 'url', 'authorId', 'authorScreenName', 'createdAt')) for previous in siblings):
            raise ValueError('inconsistent_schedule_post')
        siblings.append(schedule)
    if not private:
        return value
    validate_collection_state(value)
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
        require_keys(record, ('source', 'status', 'reason', 'checkedAt', 'requestHash', 'imageHashes',
                              *(('retry',) if 'retry' in record else ())))
        if 'retry' in record:
            retry = record['retry']
            require_keys(retry, ('attempts', 'notBefore', 'lastReason', 'previousRequests'))
            if type(retry['attempts']) is not int or not 1 <= retry['attempts'] <= 3:
                raise ValueError('invalid_schedule_retry')
            timestamp(retry['notBefore'])
            if not isinstance(retry['lastReason'], str) or not re.fullmatch(r'[a-z_]+', retry['lastReason']):
                raise ValueError('invalid_schedule_retry')
            if not isinstance(retry['previousRequests'], list) or len(retry['previousRequests']) > 3:
                raise ValueError('invalid_schedule_retry')
            for request in retry['previousRequests']:
                valid_hash(request)
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
        if any(source.get(field) != schedule.get(field) for field in ('replyToId', 'replyToAuthorId')):
            raise ValueError('schedule_source_mismatch')
        validate_analysis(revision['analysis'])
        if 'reading' in schedule:
            if (revision['analysis']['contract'] != READING_VERSION
                    and not (revision['analysis']['contract'] == TIMING_VERSION
                             and 'timingAmendment' in revision)):
                raise ValueError('invalid_schedule_contract')
            for fact in schedule['reading']['days'].values():
                evidence = fact['evidence']
                index = evidence['imageIndex']
                if index is not None and (index >= len(revision['analysis']['images'])
                        or evidence['imageHash'] != revision['analysis']['images'][index]['sha256']):
                    raise ValueError('schedule_reading_image_mismatch')
        elif revision['analysis']['contract'] == READING_VERSION:
            raise ValueError('invalid_schedule_reading')
        if revision['analysis']['contract'] == TIMING_VERSION and 'timingAmendment' not in revision:
            raise ValueError('schedule_timing_authorization_required')
        if 'workTiming' in schedule:
            if revision['analysis']['contract'] not in (VERSION, TIMING_VERSION, READING_VERSION) or any(
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
    if partials != partial_projection(value):
        raise ValueError('schedule_partial_missing_or_stale')
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
                                 and 'applyApproval' in imported else ()),
                               *(('expectedApplySubjectHash',) if isinstance(imported, dict)
                                 and 'expectedApplySubjectHash' in imported else ())))
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
        if 'expectedApplySubjectHash' in imported:
            valid_hash(imported['expectedApplySubjectHash'])
            if 'selectionProof' not in imported:
                raise ValueError('timing_selection_apply_subject_requires_selection')
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


def validate_collection_state(value):
    if 'collection' in value:
        collection = value['collection']
        require_keys(collection, {'chainId', 'names', 'periods', 'reason', 'ready'}
                     | (set(collection) & {'nextAt', 'cursor', 'serviceDate', 'mode'}
                        if isinstance(collection, dict) else set()))
        if (not isinstance(collection['chainId'], str)
                or not re.fullmatch(r'[1-9][0-9]{0,19}-[1-9][0-9]{0,19}', collection['chainId'])
                or not isinstance(collection['names'], list) or len(collection['names']) > 500
                or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in collection['names'])
                or len(set(collection['names'])) != len(collection['names'])
                or type(collection['ready']) is not bool
                or collection['reason'] not in ('processing', 'time_limit', 'complete', 'waiting')):
            raise ValueError('invalid_schedule_collection')
        periods = collection['periods']
        if not isinstance(periods, list) or not 1 <= len(periods) <= 4:
            raise ValueError('invalid_collection_period')
        for period in periods:
            if not isinstance(period, list) or len(period) != 2 or tuple(period) != half_period(day(period[0])):
                raise ValueError('invalid_collection_period')
        if len({tuple(period) for period in periods}) != len(periods):
            raise ValueError('invalid_collection_period')
        if 'serviceDate' in collection:
            day(collection['serviceDate'])
        if 'mode' in collection and collection['mode'] not in ('schedule', 'both'):
            raise ValueError('invalid_schedule_collection_mode')
        if collection.get('nextAt') is not None:
            timestamp(collection['nextAt'])
        if 'cursor' in collection and (type(collection['cursor']) is not int
                or not 0 <= collection['cursor'] <= len(collection['names'])):
            raise ValueError('invalid_collection_cursor')
    if 'collectionSlots' in value:
        slots = value['collectionSlots']
        if not isinstance(slots, list) or len(slots) > 100:
            raise ValueError('invalid_collection_slots')
        for slot in slots:
            timestamp(slot)
    if 'readings' in value:
        if not isinstance(value['readings'], dict) or len(value['readings']) > 240:
            raise ValueError('invalid_readings')
        for key, record in value['readings'].items():
            valid_hash(key)
            require_keys(record, {'sourceId', 'cacheKey', 'stage', 'reason', 'nextAt', 'attemptedVariants'}
                         | ({'failure'} if isinstance(record, dict) and 'failure' in record else set()))
            post_identifier(record['sourceId'])
            valid_hash(record['cacheKey'])
            if record['stage'] not in ('fetch', 'original', 'detail', 'done', 'held'):
                raise ValueError('invalid_reading_stage')
            if record['reason'] not in REASONS:
                raise ValueError('invalid_reading_reason')
            if record['nextAt'] is not None:
                timestamp(record['nextAt'])
            variants = record['attemptedVariants']
            if not isinstance(variants, list) or len(variants) > 256:
                raise ValueError('invalid_reading_variants')
            for variant in variants:
                valid_hash(variant)
            if len(set(variants)) != len(variants):
                raise ValueError('invalid_reading_variants')
            if 'failure' in record:
                failure = record['failure']
                require_keys(failure, ('name', 'postId', 'postUrl', 'imageIndex', 'failedAt',
                                       'host', 'httpStatus', 'retryAt', 'stage', 'nextStage', 'reason'))
                stages = ('source', 'cache', 'fetch', 'original', 'detail', 'held', 'done')
                if (not isinstance(failure['name'], str) or not NAME.fullmatch(failure['name'])
                        or failure['postId'] != record['sourceId']
                        or not isinstance(failure['postUrl'], str)
                        or not re.fullmatch(r'https://x\.com/[A-Za-z0-9_]{1,15}/status/' +
                                            re.escape(record['sourceId']), failure['postUrl'])
                        or failure['host'] not in (None, 'pbs.twimg.com', 'cdn.syndication.twimg.com',
                                                   'search.yahoo.co.jp')
                        or failure['stage'] not in stages or failure['nextStage'] not in stages
                        or failure['reason'] not in REASONS
                        or failure['imageIndex'] is not None and (type(failure['imageIndex']) is not int
                                                                 or not 0 <= failure['imageIndex'] < 4)
                        or failure['httpStatus'] is not None and (type(failure['httpStatus']) is not int
                                                                 or not 100 <= failure['httpStatus'] <= 599)):
                    raise ValueError('invalid_reading_failure')
                timestamp(failure['failedAt'])
                if failure['retryAt'] is not None:
                    timestamp(failure['retryAt'])


def read_state(path, private=True):
    # A missing authoritative state is not an empty migration.
    transport = load_module('azure-openai.py', 'schedule_json')
    value = transport.strict_json(Path(path).read_bytes())
    return validate_state(value, private)


def public_state(state):
    if not isinstance(state, dict):
        raise ValueError('invalid_schedule_state')
    validate_state(state, private=bool(PRIVATE_FIELDS & set(state)))
    return copy.deepcopy({key: state[key] for key in PUBLIC_FIELDS | (set(state) & {'partialSchedules'})})


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
    retry = state['sources'].get(key, {}).get('retry')
    state['sources'][key] = {'source': copy.deepcopy(previous_source if previous_source is not None else source),
                             'status': status, 'reason': reason,
                             'checkedAt': stamp(now), 'requestHash': request_hash,
                             'imageHashes': list(image_hashes)}
    if retry is not None:
        state['sources'][key]['retry'] = copy.deepcopy(retry)
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
        if is_partial(schedule) and analysis['contract'] != READING_VERSION:
            raise ValueError('invalid_schedule_contract')
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
        key = record_source(working, source, 'valid',
                            'reading_partial' if any(is_partial(row) for row in schedules) else 'valid_schedule',
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
        if selected_mode and state.get('savedImports', {}).get(saved_import[0], {}).get(
                'expectedApplySubjectHash') != saved_import[1].get('expectedApplySubjectHash'):
            raise ValueError('schedule_receipt_conflict')
        old = [state['revisions'][revision_key] for revision_key in previous]
        if len(old) != len(revisions) or any(
                before['schedule'] != after['schedule'] or before['sourceKey'] != after['sourceKey']
                or before['analysis'] != after['analysis']
                or before.get('timingAmendment') != after.get('timingAmendment')
                or before.get('source', state['sources'][before['sourceKey']]['source']) != after['source']
                for before, after in zip(old, revisions)):
            raise ValueError('schedule_receipt_conflict')
        return False
    if selected_mode and 'expectedApplySubjectHash' in saved_import[1]:
        valid_hash(saved_import[1]['expectedApplySubjectHash'])
        if saved_import[1]['expectedApplySubjectHash'] != digest(state):
            raise ValueError('timing_selection_apply_subject_changed')
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
    if 'partialSchedules' in state or any(is_partial(row) for row in schedules):
        working['partialSchedules'] = partial_projection(working)
    changed = (working['schedules'] != state['schedules']
               or working.get('partialSchedules', []) != state.get('partialSchedules', []))
    validate_state(working)
    if selected_mode:
        load_module('half-month-timing.py', 'half_month_selected_delta').validate_selection_delta(
            state, working, analysis, source, schedules, saved_import[1]['selectionProof'])
    state.clear()
    state.update(working)
    return changed


def apply_reading_revision(state, schedules, source, analysis, *, saved_import=None):
    """Persist complete or partial v3 evidence atomically, without replacing confirmed tables."""
    if analysis.get('contract') != READING_VERSION:
        raise ValueError('invalid_schedule_contract')
    return apply_revision(state, schedules, source, analysis, saved_import=saved_import)


def population(schedule, insights, accounts, bindings=None, *, registry=None, other_bindings=()):
    """Registry targets are independent of historical feeds and legacy metadata."""
    if registry is not None:
        members = member_registry()
        targets, coverage = members.collection_population(registry, bindings or {}, *other_bindings)
        reasons = {}
        for row in coverage.values():
            reason = row['reason']
            eligible = reason == 'eligible_not_collected'
            reasons[row['name']] = {
                'handle': row['handle'] if eligible else None,
                'reason': ('not_searched' if eligible else reason if reason in REASONS else 'paused')}
        for row in registry['unresolvedNames']:
            if row['resolvedMemberId'] is None:
                reasons[row['name']] = {'handle': None, 'reason': 'account_unknown'}
        return {target['name']: target for target in targets.values()}, reasons
    # Explicit legacy callers retain their original input contract.
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


def effective_schedule(manual_schedule_dict, feed, *, registry=None):
    """Date -> 昼/夜/unassigned -> [{name, scheduleSources, ...}]; manual attributes win."""
    validate_state(feed, private=bool(PRIVATE_FIELDS & set(feed)))
    result = copy.deepcopy(manual_schedule_dict or {})
    automatic = {}
    confirmed = {_pair(row): row for row in feed['schedules']}
    for schedule in [*feed['schedules'], *sorted(feed.get('partialSchedules', []), key=_post_order)]:
        current = confirmed.get(_pair(schedule))
        if (schedule['sourceKind'] == 'own-reply' and current is not None
                and _post_order(schedule) <= _post_order(current)):
            continue
        source = {key: copy.deepcopy(schedule[key]) for key in (
            'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt',
            'observedAt', 'sourceKind', 'period', 'replyToId', 'replyToAuthorId') if key in schedule}
        for item in schedule['days']:
            operation = schedule.get('reading', {}).get('days', {}).get(item['date'], {}).get('operation')
            if operation in ('replace', 'cancel'):
                matched = False
                for shift, rows in automatic.get(item['date'], {}).items():
                    if operation == 'cancel' and item['shifts'] and shift not in item['shifts']:
                        continue
                    for row in list(rows):
                        if row['name'] != schedule['name']:
                            continue
                        bound = row['scheduleSources']
                        retained = [proof for proof in bound if proof['id'] != schedule['replyToId']
                                    and proof.get('replyToId') != schedule['replyToId']]
                        if retained != bound:
                            matched = True
                            if retained:
                                row['scheduleSources'] = retained
                            else:
                                rows.remove(row)
                if operation == 'replace' and not matched:
                    continue
            if operation == 'cancel':
                continue
            reviewed_shifts = {
                shift for shift, rows in (manual_schedule_dict or {}).get(item['date'], {}).items()
                if any(row['name'] == schedule['name'] and any(
                    review.get('id') == schedule['id']
                    and review.get('confirmation', {}).get('method') == 'source-confirmed'
                    for review in row.get('halfMonthSources', [])) for row in rows)}
            for shift in item['shifts'] or ['unassigned']:
                if shift != 'unassigned' and reviewed_shifts and shift not in reviewed_shifts:
                    continue
                rows = automatic.setdefault(item['date'], {}).setdefault(shift, [])
                person = next((row for row in rows if row['name'] == schedule['name']), None)
                if person is None:
                    person = {'name': schedule['name']}
                    rows.append(person)
                sources = person.setdefault('scheduleSources', [])
                if source not in sources:
                    sources.append(copy.deepcopy(source))
    for date, shifts in automatic.items():
        for shift, rows in shifts.items():
            for row in rows:
                destination = result.setdefault(date, {}).setdefault(shift, [])
                person = next((person for person in destination if person['name'] == row['name']), None)
                if person is None:
                    destination.append(copy.deepcopy(row))
                else:
                    sources = person.setdefault('scheduleSources', [])
                    sources.extend(copy.deepcopy(source) for source in row['scheduleSources'] if source not in sources)
    if registry is not None:
        members = member_registry()
        members.validate_registry(registry)
        for date, shifts in result.items():
            for shift, rows in shifts.items():
                retained = []
                for person in rows:
                    member = members.lookup(registry, person['name'])
                    policy = members.plan_policy(member, date) if member else 'retained'
                    if policy == 'excluded_from_plan_view':
                        continue
                    if policy == 'retained_requires_review':
                        person['planPolicy'] = policy
                    retained.append(person)
                shifts[shift] = retained
    return result
