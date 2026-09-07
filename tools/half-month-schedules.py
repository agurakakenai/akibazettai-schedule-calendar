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
VERSION = 'half-month-schedule-v1'
MODEL, MODEL_VERSION = 'gpt-5.6-luna', '2026-07-09'
HEX = re.compile(r'[a-f0-9]{64}\Z')
ID = re.compile(r'[1-9][0-9]{0,24}\Z')
HANDLE = re.compile(r'[A-Za-z0-9_]{1,15}\Z')
NAME = re.compile(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}\Z')
STATUSES = {'never', 'ok', 'partial', 'unavailable', 'no-new', 'no-results',
            'paused', 'budget-exhausted', 'outside-window'}
SOURCE_STATUSES = {'pending', 'issued', 'valid', 'negative', 'failed'}
REASONS = {
    'not_searched', 'account_unknown', 'account_ambiguous', 'account_identity_mismatch',
    'no_candidates', 'post_unverified', 'not_issued', 'period_unknown', 'schedule_pending',
    'not_schedule', 'budget_wait', 'paused', 'valid_schedule', 'queue_limit',
    'candidate_limit', 'outside_period', 'source_failed', 'analysis_failed',
    'known_source', 'search_failed', 'not_due', 'stale_candidate',
}
PUBLIC_FIELDS = {'schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt', 'schedules', 'lastRun'}
PRIVATE_FIELDS = {'identityBindings', 'revisions', 'sources', 'pending', 'coverage', 'receipts',
                  'candidateHistory', 'savedImports'}
SAVED_IMPORT_HASH_FIELDS = ('receiptId', 'requestHash', 'usageReceiptId', 'usageSourceHash',
                           'sourceManifestHash', 'analysisResultHash', 'analysisReceiptHash')
SCHEDULE_FIELDS = {'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt',
                   'observedAt', 'sourceKind', 'period', 'days'}


def load_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    require_keys(value, SCHEDULE_FIELDS)
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
    require_keys(value, ('contract', 'model', 'modelVersion', 'promptHash', 'schemaHash',
                         'contextHash', 'requestHash', 'resultHash', 'receiptId', 'analyzedAt', 'images'))
    if (value['contract'], value['model'], value['modelVersion']) != (VERSION, MODEL, MODEL_VERSION):
        raise ValueError('invalid_schedule_contract')
    for field in ('promptHash', 'schemaHash', 'contextHash', 'requestHash', 'resultHash', 'receiptId'):
        valid_hash(value[field])
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
    winners = {}
    for key, revision in value['revisions'].items():
        require_keys(revision, ('schedule', 'sourceKey', 'analysis',
                               *(('source',) if isinstance(revision, dict) and 'source' in revision else ())))
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
        if len(source['media']) != len(revision['analysis']['images']):
            raise ValueError('schedule_images_incomplete')
        bound = value['identityBindings'].get(schedule['name'])
        if not bound or any(bound[field] != source[field] for field in ('authorId', 'authorScreenName')):
            raise ValueError('schedule_binding_mismatch')
        if timestamp(revision['analysis']['analyzedAt']) < timestamp(source['observedAt']):
            raise ValueError('schedule_analysis_chronology')
        if key not in value['receipts'].get(revision['analysis']['receiptId'], []):
            raise ValueError('schedule_receipt_missing')
        pair = (schedule['name'], schedule['period']['from'])
        previous = winners.get(pair)
        order = lambda row: (timestamp(row['createdAt']), int(row['id']))
        if previous is None or order(schedule) > order(previous):
            winners[pair] = schedule
        elif order(schedule) == order(previous) and schedule['days'] != previous['days']:
            raise ValueError('schedule_same_revision_conflict')
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
        require_keys(imported, (*SAVED_IMPORT_HASH_FIELDS, 'issuedAt', 'importedAt'))
        for field in SAVED_IMPORT_HASH_FIELDS:
            valid_hash(imported[field])
        if timestamp(imported['importedAt']) < timestamp(imported['issuedAt']):
            raise ValueError('invalid_schedule_saved_import_chronology')
        linked = value['receipts'].get(imported['receiptId'])
        if not linked:
            raise ValueError('schedule_saved_import_receipt_missing')
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


def apply_revision(state, schedules, source, analysis):
    """Atomically retain all revisions; only a newer complete same-half input wins."""
    validate_state(state)
    validate_source(source)
    validate_analysis(analysis)
    if not isinstance(schedules, list) or not 1 <= len(schedules) <= 2:
        raise ValueError('schedule_pending')
    for schedule in schedules:
        validate_schedule(schedule)
    if len({(s['name'], s['period']['from']) for s in schedules}) != len(schedules):
        raise ValueError('duplicate_schedule_period')
    working = copy.deepcopy(state)
    bind_identity(working, source)
    key = record_source(working, source, 'valid', 'valid_schedule',
                        timestamp(analysis['analyzedAt']), analysis['requestHash'],
                        [image['sha256'] for image in analysis['images']])
    revisions = [{'schedule': copy.deepcopy(schedule), 'sourceKey': key, 'source': copy.deepcopy(source),
                  'analysis': copy.deepcopy(analysis)} for schedule in schedules]
    revision_keys = [digest(revision) for revision in revisions]
    receipt = analysis['receiptId']
    previous = state['receipts'].get(receipt)
    if previous is not None:
        old = [state['revisions'][revision_key] for revision_key in previous]
        if len(old) != len(revisions) or any(
                before['schedule'] != after['schedule'] or before['sourceKey'] != after['sourceKey']
                or before['analysis'] != after['analysis']
                or before.get('source', state['sources'][before['sourceKey']]['source']) != after['source']
                for before, after in zip(old, revisions)):
            raise ValueError('schedule_receipt_conflict')
        return False
    for revision_key, revision in zip(revision_keys, revisions):
        working['revisions'][revision_key] = revision
    working['receipts'][receipt] = revision_keys
    changed = False
    for schedule in schedules:
        old = next((item for item in working['schedules'] if item['name'] == schedule['name']
                    and item['period']['from'] == schedule['period']['from']), None)
        order = lambda item: (timestamp(item['createdAt']), int(item['id']))
        if old is None or order(schedule) > order(old):
            if old is not None:
                working['schedules'].remove(old)
            working['schedules'].append(copy.deepcopy(schedule))
            changed = True
        elif order(schedule) == order(old) and schedule['days'] != old['days']:
            raise ValueError('schedule_same_revision_conflict')
    working['schedules'].sort(key=lambda item: (item['period']['from'], item['name']))
    validate_state(working)
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
