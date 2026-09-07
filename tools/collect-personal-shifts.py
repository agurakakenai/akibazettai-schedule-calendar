"""Optional personal announcements, isolated from official observations.

JST calendar dates start at 00:00, not the official collector's 05:00.
--dry-run suppresses publication only: private facts/ledgers, spent budgets and
access-denial pauses remain durable. A saved pause requires manual review; this
CLI deliberately has no automatic resume or budget-reset option.
Exit: 0 normal/outside-window, 2 partial/budget-exhausted, 3 HTTP unavailable/paused,
4 infrastructure/locking/invalid input (the cloud lease must remain fail-closed).
"""
import argparse
from contextlib import ExitStack
import copy
import csv
import datetime as dt
import email.utils
import html
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('personal_official_helpers',
                                              ROOT / 'tools' / 'collect-shifts.py')
official = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(official)
AZURE_SPEC = importlib.util.spec_from_file_location('personal_azure', ROOT / 'tools' / 'personal-azure.py')
azure = importlib.util.module_from_spec(AZURE_SPEC)
AZURE_SPEC.loader.exec_module(azure)
timing = work_timing = azure.timing
SEARCH_SPEC = importlib.util.spec_from_file_location('personal_yahoo', ROOT / 'tools' / 'yahoo-search.py')
yahoo = importlib.util.module_from_spec(SEARCH_SPEC)
SEARCH_SPEC.loader.exec_module(yahoo)
REGISTRY_SPEC = importlib.util.spec_from_file_location(
    'personal_member_registry', ROOT / 'tools' / 'member-registry.py')
members = importlib.util.module_from_spec(REGISTRY_SPEC)
REGISTRY_SPEC.loader.exec_module(members)
UTC, JST = official.UTC, official.JST
Failure = official.FetchFailure


class RegistryFailure(Failure):
    pass


class InfrastructureFailure(RuntimeError):
    pass


SEARCH_HOST = 'search.yahoo.co.jp'
POST_HOST = official.POST_HOST
DAILY_LIMITS = {'searches': 60, 'posts': 30}
PILOT_BUDGETS = {'2026-09-06': {'searches': 7, 'posts': 2}}
MAX_BODY = 4_000_000
STATUSES = {'never', 'ok', 'partial', 'unavailable', 'no-new', 'no-results',
            'paused', 'budget-exhausted', 'outside-window'}
KINDS = {'placement', 'absence', 'late', 'return', 'uncertain'}
PUBLIC_FIELDS = {'schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt', 'posts', 'lastRun'}
PRIVATE_FIELDS = {'pending', 'resolved', 'budgets', 'paused', 'identityBindings',
                  'originalTargets', 'lastRequests'}
OPTIONAL_PRIVATE_FIELDS = {'azureAnalysis', 'coverage', 'searchHistory', 'savedPersonalImports'}
SCOPES = ('昼', '夜', 'unspecified')
LINK_STATUSES = ('work', 'withdrawn', 'conflict')
TARGET_ORIGINS = {'original', 'scheduled', 'official_names', 'official_notice', 'personal', 'curated',
                  'registry'}
DENIAL = re.compile(
    r'captcha|access denied|アクセス.{0,12}(?:拒否|制限)|ロボットではない|'
    r'unusual traffic|verify you are human|permission denied|forbidden|unauthorized|'
    r'could not authenticate|authentication required|login required|'
    r'rate limit|too many requests|ログイン.{0,8}(?:必要|してください)', re.I)
DATE_WORD = re.compile(r'(?<!\d)(\d{1,2})(?:月|/)(\d{1,2})(?:日)?(?!\d)')
NODE_FALLBACK = Path(
    r'C:\Users\manab\.copilot\session-state\5ed7cdc5-1b7a-4598-ac55-b2b55e2ed467'
    r'\files\tools\node-v22.11.0-win-x64\node.exe')


def public_url(handle, tid):
    return f'https://x.com/{handle}/status/{tid}'


def same_identity(binding, author_id, handle):
    return (binding['authorId'] == author_id
            and binding['authorScreenName'].casefold() == handle.casefold())


def calendar_day(value):
    return value.astimezone(JST).date()


def stamp(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def empty_state():
    return {'schemaVersion': 1, 'complete': False, 'checkedAt': None,
            'lastSuccessAt': None, 'posts': [], 'pending': [], 'resolved': [],
            'budgets': {}, 'paused': None, 'identityBindings': {},
            'originalTargets': {}, 'lastRequests': {},
            'lastRun': {'status': 'never'}}


def public_state(state):
    result = {key: copy.deepcopy(state[key]) for key in (
        'schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt', 'posts', 'lastRun')}
    if 'failures' in result['lastRun']:
        result['lastRun']['failures'] = [
            {'id': item['id'], 'reason': 'analysis_pending'}
            if item['reason'].startswith('azure_') and 'id' in item else item
            for item in result['lastRun']['failures']]
    return result


def empty_snapshot():
    return public_state(empty_state())


def load_snapshot(path):
    """Validate a full private state, or the six-field public projection.

    A partial private state must not masquerade as public data: presence of any
    private field requires the complete private schema.
    """
    if not path.exists():
        return empty_snapshot()
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError
        private = bool((PRIVATE_FIELDS | OPTIONAL_PRIVATE_FIELDS).intersection(value))
    except (ValueError, TypeError):
        raise ValueError('invalid_personal_state') from None
    return read_state(path, private=private)


def require_keys(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= set(value)
            or set(value) - set(required) - set(optional)):
        raise ValueError('invalid_personal_state')


def validate_failure(value):
    if 'reason' in value and not re.fullmatch('[a-z_]+', value['reason']):
        raise ValueError('invalid_personal_state')
    if 'httpStatus' in value and (type(value['httpStatus']) is not int
                                  or not 100 <= value['httpStatus'] <= 599):
        raise ValueError('invalid_personal_state')
    if 'retryAt' in value:
        official.timestamp(value['retryAt'])


def validate_last_run(value):
    counts = {'sourceCount', 'targetCount', 'activeTargetCount', 'attemptedCount',
              'newPostCount', 'newEventCount', 'skippedResolvedCount',
              'pendingCount', 'deferredCount'}
    require_keys(value, ('status',), counts | {
        'date', 'dateBasis', 'source', 'sources', 'requests', 'failures', 'finishedAt', 'complete'})
    for key in counts & value.keys():
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError
    if 'date' in value:
        dt.date.fromisoformat(value['date'])
    if 'dateBasis' in value and value['dateBasis'] != 'JST calendar date, 00:00 boundary':
        raise ValueError
    if 'source' in value and value['source'] != 'manually_reviewed_public_http_pilot':
        raise ValueError
    if 'complete' in value and value['complete'] is not False:
        raise ValueError
    if 'finishedAt' in value:
        official.timestamp(value['finishedAt'])
    if 'requests' in value:
        require_keys(value['requests'], ('searches', 'posts'))
        if any(type(count) is not int or count < 0 for count in value['requests'].values()):
            raise ValueError
    for key in ('sources', 'failures'):
        if not isinstance(value.get(key, []), list):
            raise ValueError
    for source in value.get('sources', []):
        require_keys(source, ('url', 'status'), ('candidateCount', 'reason', 'httpStatus', 'retryAt'))
        if (source['status'] not in ('ok', 'failed')
                or not valid_search_url(source['url'])
                or ('candidateCount' in source and
                    (type(source['candidateCount']) is not int or source['candidateCount'] < 0))):
            raise ValueError
        validate_failure(source)
    for failure in value.get('failures', []):
        require_keys(failure, ('reason',), ('id', 'httpStatus', 'retryAt'))
        if 'id' in failure and (not isinstance(failure['id'], str)
                                or not official.post_id(failure['id'])):
            raise ValueError
        validate_failure(failure)


def valid_post(post):
    require_keys(post, ('id', 'url', 'name', 'authorId', 'authorScreenName',
                       'createdAt', 'observedAt', 'date', 'events'), ('links', 'workTiming'))
    if 'workTiming' in post:
        work_timing.validate(post['workTiming'], owner=post, date=post['date'],
                             source_kind='personal-work-post')
    tid = official.post_id(post['id'])
    handle = post['authorScreenName']
    if (not isinstance(post['id'], str) or not tid
            or not re.fullmatch(r'[A-Za-z0-9_]{1,15}', handle)
            or not isinstance(post['name'], str)
            or not re.fullmatch(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', post['name'])
            or not isinstance(post['authorId'], str)
            or not official.post_id(post['authorId'])
            or post['authorId'] == official.AUTHOR_ID
            or post['url'] != public_url(handle, tid)
            or calendar_day(official.timestamp(post['createdAt'])).isoformat() != post['date']
            or not isinstance(post['events'], list)
            or not post['events'] and not post.get('links')
            and not post.get('workTiming', {}).get('facts')):
        raise ValueError('invalid_personal_post')
    created, observed = official.timestamp(post['createdAt']), official.timestamp(post['observedAt'])
    if observed < created or abs((official.snowflake_time(tid) - created).total_seconds()) >= 2:
        raise ValueError('invalid_personal_post')
    for event in post['events']:
        valid_event(event)
    if 'links' in post:
        valid_links(post['links'])


def valid_link(link):
    require_keys(link, ('scope', 'status'))
    if (link['scope'] not in SCOPES or link['status'] not in LINK_STATUSES
            or link['scope'] == 'unspecified' and link['status'] != 'work'):
        raise ValueError('invalid_personal_link')


def valid_links(links):
    if not isinstance(links, list) or len(links) > 3:
        raise ValueError('invalid_personal_link')
    seen = set()
    for link in links:
        valid_link(link)
        if link['scope'] in seen:
            raise ValueError('invalid_personal_link')
        seen.add(link['scope'])


def legacy_links(post):
    links = []
    for scope in ('昼', '夜'):
        events = [event for event in post['events']
                  if event['shift'] == scope and event['kind'] != 'uncertain']
        if not events:
            continue
        kinds = {event['kind'] for event in events}
        stores = {event['storeId'] for event in events if 'storeId' in event}
        conflict = len(stores) > 1 or 'absence' in kinds and len(kinds) > 1
        links.append({'scope': scope, 'status':
                      'conflict' if conflict else 'withdrawn' if 'absence' in kinds else 'work'})
    return links


def valid_event(event):
    if (not isinstance(event, dict)
                or not {'shift', 'kind', 'excerpt'} <= set(event)
                or set(event) - {'shift', 'kind', 'storeId', 'time', 'excerpt'}
                or event['shift'] not in ('昼', '夜') or event['kind'] not in KINDS
                or not isinstance(event['excerpt'], str)
                or not 1 <= len(event['excerpt']) <= 160
                or not event['excerpt'].strip()
                or re.search(r'[\r\n\u2028\u2029]', event['excerpt'])
                or ('storeId' in event and event['storeId'] not in ('s1', 's2', 's3', 's4'))
                or (event['kind'] == 'placement' and 'storeId' not in event)
                or (event['kind'] == 'absence' and 'storeId' in event)
                or ('time' in event and not re.fullmatch(
                    r'(?:[01]\d|2[0-3]):[0-5]\d', event['time']))):
        raise ValueError('invalid_personal_event')


def azure_context():
    return SimpleNamespace(
        official=official, require_keys=require_keys, valid_event=valid_event,
        valid_post=valid_post, valid_link=valid_link, valid_links=valid_links,
        validate_failure=validate_failure, stamp=stamp,
        calendar_day=calendar_day)


def read_state(path, private=True):
    if not path.exists():
        return empty_state()
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        require_keys(value, PUBLIC_FIELDS | PRIVATE_FIELDS if private else PUBLIC_FIELDS,
                     OPTIONAL_PRIVATE_FIELDS if private else ())
        if (type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
                or value['complete'] is not False or value['lastRun']['status'] not in STATUSES
                or not isinstance(value['posts'], list)):
            raise ValueError
        validate_last_run(value['lastRun'])
        for key in ('checkedAt', 'lastSuccessAt'):
            if value[key] is not None:
                official.timestamp(value[key])
        ids = set()
        for post in value['posts']:
            valid_post(post)
            if post['id'] in ids:
                raise ValueError
            ids.add(post['id'])
        if private:
            if 'azureAnalysis' in value:
                azure.validate_state(value['azureAnalysis'], azure_context())
            validate_collection_coverage(value)
            if 'savedPersonalImports' in value:
                spec = importlib.util.spec_from_file_location(
                    'personal_saved_validation', ROOT / 'tools' / 'personal-saved.py')
                saved = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(saved)
                saved.validate_revisions(value, azure_context())
            for key in ('pending', 'resolved', 'budgets', 'paused', 'identityBindings',
                        'originalTargets', 'lastRequests'):
                if key not in value:
                    raise ValueError
            if not isinstance(value['budgets'], dict):
                raise ValueError
            for day, budget in value['budgets'].items():
                if dt.date.fromisoformat(day).isoformat() != day:
                    raise ValueError
                require_keys(budget, ('searches', 'posts'))
                if set(budget) != {'searches', 'posts'} or any(
                        type(count) is not int or count < 0 for count in budget.values()):
                    raise ValueError
            for key in ('pending', 'resolved'):
                seen = set()
                if not isinstance(value[key], list):
                    raise ValueError
                for item in value[key]:
                    if key == 'pending':
                        require_keys(item, (
                            'id', 'url', 'name', 'authorId', 'authorScreenName', 'date',
                            'searchCreatedAt', 'reason', 'firstSeenAt', 'lastAttemptAt', 'attempts'),
                            ('httpStatus', 'retryAt', 'metadataSource', 'sourceCreatedAt'))
                    else:
                        require_keys(item, ('id', 'url', 'name', 'date', 'reason', 'resolvedAt'))
                    if (not isinstance(item['id'], str) or not official.post_id(item['id'])
                            or item['id'] in seen
                            or not isinstance(item['name'], str) or not item['name']
                            or not re.fullmatch('[a-z_]+', item['reason'])):
                        raise ValueError
                    dt.date.fromisoformat(item['date'])
                    if key == 'pending':
                        handle = item['authorScreenName']
                        if (not re.fullmatch(r'[A-Za-z0-9_]{1,15}', handle)
                                or not isinstance(item['authorId'], str)
                                or not official.post_id(item['authorId'])
                                or item['authorId'] == official.AUTHOR_ID
                                or item['url'] != public_url(handle, item['id'])
                                or type(item['attempts']) is not int or item['attempts'] < 0):
                            raise ValueError
                        source = item.get('metadataSource', 'search')
                        if source not in ('search', 'saved_post', 'saved_binding'):
                            raise ValueError
                        if item['searchCreatedAt'] is None:
                            if source == 'search':
                                raise ValueError
                        elif calendar_day(official.timestamp(item['searchCreatedAt'])).isoformat() != item['date']:
                            raise ValueError
                        if 'sourceCreatedAt' in item:
                            if (source != 'saved_post' or calendar_day(official.timestamp(
                                    item['sourceCreatedAt'])).isoformat() != item['date']):
                                raise ValueError
                        official.timestamp(item['firstSeenAt'])
                        if item['lastAttemptAt'] is not None:
                            official.timestamp(item['lastAttemptAt'])
                        validate_failure(item)
                    else:
                        official.timestamp(item['resolvedAt'])
                        if not re.fullmatch(
                                r'https://x\.com/[A-Za-z0-9_]{1,15}/status/' + item['id'],
                                item['url']):
                            raise ValueError
                    seen.add(item['id'])
            for mapping in ('identityBindings', 'originalTargets', 'lastRequests'):
                if not isinstance(value[mapping], dict):
                    raise ValueError
            for item in value['identityBindings'].values():
                require_keys(item, ('authorId', 'authorScreenName', 'verifiedAt'))
                if (not official.post_id(item['authorId'])
                        or not isinstance(item['authorId'], str)
                        or not re.fullmatch(r'[A-Za-z0-9_]{1,15}', item['authorScreenName'])):
                    raise ValueError
                official.timestamp(item['verifiedAt'])
            for day, targets in value['originalTargets'].items():
                if dt.date.fromisoformat(day).isoformat() != day:
                    raise ValueError
                if not isinstance(targets, dict):
                    raise ValueError
                for name, target in targets.items():
                    require_keys(target, ('name', 'handle', 'shifts'))
                    if (target['name'] != name or not isinstance(name, str)
                            or not re.fullmatch(r'[A-Za-z0-9_]{1,15}', target['handle'])
                            or not isinstance(target['shifts'], list) or not target['shifts']
                            or any(shift not in ('昼', '夜') for shift in target['shifts'])
                            or len(set(target['shifts'])) != len(target['shifts'])):
                        raise ValueError
            if value['paused'] is not None:
                require_keys(value['paused'], ('reason', 'host', 'at', 'retryAt'), ('httpStatus',))
                if (not isinstance(value['paused'], dict)
                        or value['paused']['host'] not in (SEARCH_HOST, POST_HOST, 'pbs.twimg.com')):
                    raise ValueError
                official.timestamp(value['paused']['at'])
                validate_failure(value['paused'])
            for host, at in value['lastRequests'].items():
                if host not in (SEARCH_HOST, POST_HOST, 'pbs.twimg.com'):
                    raise ValueError
                official.timestamp(at)
        return value
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('invalid_personal_state') from None


def merge_seed(state, seed, *, registry=None):
    have = {post['id'] for post in state['posts']}
    resolved = {item['id'] for item in state['resolved']}
    removed_by_analysis = (
        {item['id'] for item in state['resolved'] if item['reason'] == 'no_event'}
        & {post['id'] for post in state.get('azureAnalysis', {}).get('history', [])})
    for post in seed['posts']:
        if post['id'] in removed_by_analysis:
            continue
        binding = state['identityBindings'].get(post['name'])
        identity = {'authorId': post['authorId'],
                    'authorScreenName': post['authorScreenName']}
        if binding and not same_identity(binding, identity['authorId'], identity['authorScreenName']):
            raise ValueError('seed_identity_conflict')
        if post['id'] not in have:
            state['posts'].append(copy.deepcopy(post))
            have.add(post['id'])
        state['identityBindings'].setdefault(post['name'], {
            **identity, 'verifiedAt': post['observedAt']})
        if post['id'] not in resolved:
            state['resolved'].append({
                'id': post['id'], 'name': post['name'], 'url': post['url'],
                'date': post['date'], 'reason': 'seed_confirmed',
                'resolvedAt': post['observedAt']})
            resolved.add(post['id'])
    eligible_names = None
    if registry is not None:
        eligible, _ = members.collection_population(registry, state['identityBindings'])
        eligible_names = {name for member_id in eligible
                          for name in members.names_of(members.lookup(registry, member_id))}
    state['pending'] = [item for item in state['pending']
                        if item['id'] not in resolved or item['reason'].startswith('azure_')
                        or eligible_names is not None and item['name'] not in eligible_names]
    for day, floor in PILOT_BUDGETS.items():
        budget = state['budgets'].setdefault(day, {'searches': 0, 'posts': 0})
        for kind, count in floor.items():
            budget[kind] = max(budget[kind], count)
    if state['checkedAt'] is None:
        state['checkedAt'] = seed['checkedAt']
    if state['lastSuccessAt'] is None:
        state['lastSuccessAt'] = seed['lastSuccessAt']


def read_js(path, key, node=None):
    text = path.read_text(encoding='utf-8-sig')
    prefix = re.search(r'window\.' + re.escape(key) + r'\s*=\s*', text)
    if not prefix:
        raise ValueError('missing_input_assignment')
    try:
        return json.loads(text[prefix.end():].strip().removesuffix(';'))
    except ValueError:
        pass
    executable = str(node or shutil.which('node') or NODE_FALLBACK)
    script = """
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const context=vm.createContext({window:Object.create(null)},
 {codeGeneration:{strings:false,wasm:false}});
vm.runInContext(input.text,context,{timeout:750});
process.stdout.write(vm.runInContext('JSON.stringify(window.'+input.key+')',
 context,{timeout:750}));
"""
    options = {}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
        options['startupinfo'] = subprocess.STARTUPINFO()
        options['startupinfo'].dwFlags |= subprocess.STARTF_USESHOWWINDOW
        options['startupinfo'].wShowWindow = 0
    try:
        result = subprocess.run(
            [executable, '-e', script], input=json.dumps({'key': key, 'text': text}),
            capture_output=True, encoding='utf-8', timeout=8, check=True,
            env={key: value for key, value in os.environ.items()
                 if not key.upper().startswith('AZURE_OPENAI_')}, **options)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError('unreadable_schedule_data') from None


def validate_collection_coverage(state):
    for field in ('coverage', 'searchHistory'):
        days = state.get(field, {})
        if not isinstance(days, dict):
            raise ValueError('invalid_personal_coverage')
        for day, rows in days.items():
            if dt.date.fromisoformat(day).isoformat() != day or not isinstance(rows, dict):
                raise ValueError('invalid_personal_coverage')
            for name, row in rows.items():
                if not isinstance(name, str) or not re.fullmatch(
                        r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', name):
                    raise ValueError('invalid_personal_coverage')
                if field == 'searchHistory':
                    require_keys(row, ('handle', 'attemptedAt'))
                    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', row['handle']):
                        raise ValueError('invalid_personal_coverage')
                    if calendar_day(official.timestamp(row['attemptedAt'])).isoformat() != day:
                        raise ValueError('invalid_personal_coverage')
                    continue
                require_keys(row, ('name', 'handle', 'shifts', 'origins', 'reason',
                                   'postIds', 'linkScopes', 'searchedAt'))
                if (row['name'] != name
                        or row['handle'] is not None and (
                            not isinstance(row['handle'], str)
                            or not re.fullmatch(r'[A-Za-z0-9_]{1,15}', row['handle']))
                        or not isinstance(row['shifts'], list)
                        or any(shift not in ('昼', '夜') for shift in row['shifts'])
                        or len(set(row['shifts'])) != len(row['shifts'])
                        or not isinstance(row['origins'], list)
                        or any(origin not in TARGET_ORIGINS for origin in row['origins'])
                        or len(set(row['origins'])) != len(row['origins'])
                        or not isinstance(row['reason'], str)
                        or not re.fullmatch(r'[a-z_]+', row['reason'])
                        or not isinstance(row['postIds'], list)
                        or any(not isinstance(tid, str) or not official.post_id(tid)
                               for tid in row['postIds'])
                        or len(set(row['postIds'])) != len(row['postIds'])
                        or not isinstance(row['linkScopes'], list)
                        or len(row['linkScopes']) > 2):
                    raise ValueError('invalid_personal_coverage')
                seen = set()
                for link in row['linkScopes']:
                    require_keys(link, ('scope', 'id'))
                    if (link['scope'] not in row['shifts'] or link['scope'] in seen
                            or link['id'] not in row['postIds']):
                        raise ValueError('invalid_personal_coverage')
                    seen.add(link['scope'])
                if row['searchedAt'] is not None:
                    if calendar_day(official.timestamp(row['searchedAt'])).isoformat() != day:
                        raise ValueError('invalid_personal_coverage')


def name_aliases(insights):
    return {entry['alias']: name for name, entry in ((insights or {}).get('maidTendency') or {}).items()
            if isinstance(entry, dict) and entry.get('alias')}


def select_targets(schedule, insights, accounts, date, state, observations=None, *,
                   registry=None, binding_maps=()):
    insights = insights or {}
    aliases = name_aliases(insights) if registry is None else members.display_projection(registry)['aliases']
    if registry is not None:
        registry_targets, registry_coverage = members.collection_population(
            registry, state['identityBindings'], *binding_maps)
        registry_by_name = {row['name']: row for row in registry_coverage.values()}
        eligible_by_name = {row['name']: row for row in registry_targets.values()}
    canonical = lambda name: aliases.get(name, name)
    by_name, by_handle = {}, {}
    for row in accounts:
        name = canonical(row['name'])
        by_name.setdefault(name, []).append(row)
        by_handle.setdefault(row['handle'].casefold(), set()).add(name)
    day = date.isoformat()
    population = {}

    def include(name, shift, origin):
        name = canonical(name)
        person = population.setdefault(name, {'shifts': set(), 'origins': set()})
        if shift in ('昼', '夜'):
            person['shifts'].add(shift)
        person['origins'].add(origin)

    if registry is not None:
        for member in registry['members']:
            include(member['canonicalName'], None, 'registry')
        for row in registry['unresolvedNames']:
            if row['resolvedMemberId'] is None:
                include(row['name'], None, 'registry')
    for name, target in state['originalTargets'].get(day, {}).items():
        for shift in target['shifts']:
            include(name, shift, 'original')
    for shift in ('昼', '夜'):
        for person in schedule.get('schedule', {}).get(day, {}).get(shift, []):
            include(person['name'], shift, 'scheduled')
        recorded = ((insights.get('actualRoster') or {}).get(day) or {}).get(shift) or {}
        for names in (recorded.get('stores') or {}).values():
            for name in names:
                include(name, shift, 'curated')
    official_posts = (observations or {}).get('posts', [])
    official_days = {post['id']: post['date'] for post in official_posts}
    superseded = {tid for post in official_posts
                  for tid in post.get('editTweetIds', [])[:-1]
                  if official_days.get(tid) == post['date']}
    for post in official_posts:
        if post['date'] != day or post['id'] in superseded:
            continue
        corrections = schedule.get('observationNameCorrections', {}).get(post['id'], {})
        for name in post['names']:
            include(corrections.get(name, {}).get('name', name), post['shift'], 'official_names')
        for notice in post.get('notices', []):
            name = notice['name']
            include(corrections.get(name, {}).get('name', name), post['shift'], 'official_notice')
    for post in state['posts']:
        if post['date'] != day:
            continue
        # Link-only evidence never introduces a person or an unstated shift.
        for event in post['events']:
            include(post['name'], event['shift'], 'personal')
        rule = schedule.get('personalEventAdditions', {}).get(post['id'], {})
        if rule and all(rule.get(key) == post[key] for key in (
                'name', 'authorId', 'authorScreenName', 'date')):
            for event in rule.get('events', []):
                include(post['name'], event['shift'], 'personal')

    eligible, coverage = {}, {}
    for name in sorted(population):
        rows = by_name.get(name, [])
        handle, reason = None, 'account_unknown'
        if registry is not None:
            managed = registry_by_name.get(name)
            if managed:
                handle, reason = managed['handle'], managed['reason']
            if name in eligible_by_name:
                reason = 'not_searched'
        elif len(rows) > 1:
            reason = 'account_ambiguous'
        elif rows:
            row = rows[0]
            candidate = row['handle']
            if (row['source'] in ('公式サイト', '本人確認済み')
                    and re.fullmatch(r'[A-Za-z0-9_]{1,15}', candidate)):
                if len(by_handle[candidate.casefold()]) != 1:
                    reason = 'account_ambiguous'
                elif (((insights.get('maidTendency') or {}).get(name) or {}).get('x') != candidate):
                    reason = 'account_identity_mismatch'
                else:
                    bound = state['identityBindings'].get(name)
                    if bound and bound['authorScreenName'] != candidate:
                        reason = 'account_identity_mismatch'
                    else:
                        handle, reason = candidate, 'not_searched'
        shifts = [shift for shift in ('昼', '夜') if shift in population[name]['shifts']]
        if registry is not None:
            if name in eligible_by_name:
                eligible[name] = {**eligible_by_name[name], 'shifts': shifts,
                                  'registeredAt': members.lookup(registry, name)['registeredAt'],
                                  'aliases': sorted(members.names_of(members.lookup(registry, name)))}
        elif handle and not shifts:
            reason = 'shift_unknown'
        elif handle:
            eligible[name] = {'name': name, 'handle': handle, 'shifts': shifts}
        coverage[name] = {
            'name': name, 'handle': handle, 'shifts': shifts,
            'origins': sorted(population[name]['origins']), 'reason': reason,
            'postIds': [], 'linkScopes': [], 'searchedAt': None}
    if day not in state['originalTargets']:
        state['originalTargets'][day] = {
            name: {'name': target['name'], 'handle': target['handle'], 'shifts': [
                shift for shift in target['shifts']
                if any(canonical(person['name']) == name for person in
                       schedule.get('schedule', {}).get(day, {}).get(shift, []))]}
            for name, target in eligible.items() if 'scheduled' in population[name]['origins']}
    state.setdefault('coverage', {})[day] = coverage
    return eligible


def active_targets(targets, date, now, *, scheduled=False):
    if calendar_day(now) != date:
        return {}
    local = now.astimezone(JST).timetz().replace(tzinfo=None)
    if scheduled and local > dt.time(18):
        return {}
    return {name: target for name, target in targets.items()
            if local <= dt.time(13 if target['shifts'] == ['昼'] else 19, 30)}


def search_urls(date):
    queries = (f'{date.month}月{date.day}日 号店',
               '今日 お休み 絶対領域', f'{date.month}/{date.day} 号店')
    return tuple('https://' + SEARCH_HOST + '/realtime/search?'
                 + urllib.parse.urlencode({'p': query, 'ei': 'UTF-8'}) for query in queries)


def account_search_url(handle):
    if not isinstance(handle, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,15}', handle):
        raise ValueError('invalid_search_account')
    return 'https://' + SEARCH_HOST + '/realtime/search?' + urllib.parse.urlencode(
        {'p': 'id:' + handle, 'ei': 'UTF-8'})


def valid_search_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        if set(query) != {'p', 'ei'} or query['ei'] != ['UTF-8'] or len(query['p']) != 1:
            return False
        text = query['p'][0]
        if re.fullmatch(r'id:[A-Za-z0-9_]{1,15}', text):
            return url == account_search_url(text[3:])
        if not (text == '今日 お休み 絶対領域' or re.fullmatch(
                r'(?:[1-9]|1[0-2])(?:月(?:[1-9]|[12]\d|3[01])日|/(?:[1-9]|[12]\d|3[01])) 号店', text)):
            return False
        return url == 'https://' + SEARCH_HOST + '/realtime/search?' + urllib.parse.urlencode(
            {'p': text, 'ei': 'UTF-8'})
    except ValueError:
        return False


def target_searches(targets, date, state, now, maximum, *, scheduled=False):
    active = active_targets(targets, date, now, scheduled=scheduled)
    history = {}
    for day, rows in state.get('searchHistory', {}).items():
        if day > date.isoformat():
            continue
        for name, row in rows.items():
            target = next((target for target in active.values()
                           if name == target['name'] or name in target.get('aliases', ())), None)
            if target and row['handle'].casefold() == target['handle'].casefold():
                canonical = target['name']
                history[canonical] = max(history.get(canonical, ''), row['attemptedAt'])

    def priority(target):
        return history.get(target['name'], ''), target.get('registeredAt', ''), target['name']

    if any('memberId' in target for target in active.values()):
        fresh = sorted((target for target in active.values()
                        if target['name'] not in history), key=priority)
        corrections = sorted((target for target in active.values() if target not in fresh), key=priority)
        local = now.astimezone(JST)
        minutes = local.hour * 60 + local.minute

        def age(target):
            at = history.get(target['name']) or target.get('registeredAt')
            return official.timestamp(at) if at else dt.datetime.min.replace(tzinfo=UTC)

        oldest = min(age(target) for target in active.values())
        near = [target for target in sorted(active.values(), key=priority) if target['shifts']
                and target_deadline(target, scheduled=scheduled) - minutes <= 90
                and age(target) <= oldest + dt.timedelta(minutes=90)
                and (target['name'] not in history
                     or official.timestamp(history[target['name']]).astimezone(JST)
                     < local.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(
                         minutes=target_deadline(target, scheduled=scheduled) - 90))]
        # One discovery slot is part of, never additional to, the existing cap.
        selected = near[:1] if maximum else []
        if fresh and len(selected) < maximum and not any(target in fresh for target in selected):
            selected.append(fresh[0])
        selected.extend(target for target in corrections if target not in selected)
        return [(target['name'], account_search_url(target['handle'])) for target in selected[:maximum]]
    return [(target['name'], account_search_url(target['handle']))
            for target in sorted(active.values(), key=lambda target: (
                target['shifts'] != ['昼'], *priority(target)))[:maximum]]


def target_deadline(target, *, scheduled=False):
    return 13 * 60 + 30 if target['shifts'] == ['昼'] else (
        18 * 60 if scheduled else 19 * 60 + 30)


def target_for_name(targets, name):
    if name in targets:
        return targets[name]
    for target in targets.values():
        if name in target.get('aliases', ()):
            return {**target, 'name': name}
    return None


def identity_bindings(registry, *binding_maps):
    bindings = {}
    for mapping in binding_maps:
        for name, bound in mapping.items():
            member = members.lookup(registry, name)
            for alias in members.names_of(member) if member else (name,):
                bindings[alias] = bound
    return bindings


def search_page(document):
    try:
        return yahoo.search_page(document)
    except ValueError:
        raise Failure('invalid_search_response') from None


def discover(document, targets, date, now, bindings):
    page = search_page(document)
    error = page.get('searchError')
    if DENIAL.search(json.dumps(error, ensure_ascii=False)):
        raise Failure('access_denied', status=200)
    if error is None:
        error = {}
    if not isinstance(error, dict):
        raise Failure('invalid_search_response')
    if error.get('errorType') not in (None, '', 'zeromatch'):
        raise Failure('search_error')
    try:
        entries = yahoo.candidate_entries(page)
    except ValueError:
        raise Failure('invalid_search_response') from None
    handles = {target['handle'].casefold(): target for target in targets.values()}
    candidates, conflicts = {}, set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise Failure('invalid_search_response')
        handle = entry.get('screenName')
        if not isinstance(handle, str) or handle.casefold() not in handles:
            continue
        tid, uid = official.post_id(entry.get('id')), official.post_id(entry.get('userId'))
        epoch = entry.get('createdAt')
        if (not tid or not uid or uid == official.AUTHOR_ID
                or isinstance(epoch, bool) or not isinstance(epoch, (str, int))
                or not re.fullmatch(r'\d{10}', str(epoch))):
            continue
        try:
            when = dt.datetime.fromtimestamp(int(epoch), UTC)
            url = urllib.parse.urlsplit(official.unescape_urls(entry.get('url', '')))
            if (url.scheme != 'https' or url.hostname not in ('x.com', 'twitter.com')
                    or url.username or url.port
                    or url.path != f'/{handle}/status/{tid}'
                    or calendar_day(when) != date or when > now
                    or abs((official.snowflake_time(tid) - when).total_seconds()) >= 2):
                continue
        except (ValueError, OSError, OverflowError, TypeError):
            continue
        target = handles[handle.casefold()]
        bound = bindings.get(target['name'])
        if bound and bound['authorId'] != uid:
            continue
        candidate = {
            'id': tid, 'url': public_url(handle, tid), 'name': target['name'],
            'authorId': uid, 'authorScreenName': handle, 'date': date.isoformat(),
            'searchCreatedAt': stamp(when)}
        if tid in candidates and candidates[tid] != candidate:
            conflicts.add(tid)
        candidates[tid] = candidate
    return [item for tid, item in candidates.items() if tid not in conflicts]


def date_context(fragment):
    words = [match.group(0) for match in DATE_WORD.finditer(fragment)]
    words.extend(re.findall(r'今日|本日|昨日|明日|明後日|あした|あす|きのう', fragment))
    return ' '.join(words)


def announcement_clauses(text):
    # A conjunction ends the predicate's store/time/shift scope. Do not carry an
    # earlier store into a later predicate, or a later store into an earlier one.
    text = re.sub(r'(です|ます|でした|ました)が', r'\1、', text)
    text = re.sub(r'けれども?|けど|一方|そして', '、', text)
    fragments, sentences = [], []
    for sentence, body in enumerate(re.split(r'[。！？!?]+', text)):
        clauses = re.split(r'[\n、,;；]+', body)
        fragments.extend(clauses)
        sentences.extend([sentence] * len(clauses))
    negation = re.compile(r'ではなく|じゃなく|でなく|ではありません|'
                          r'ではなかった|じゃなかった|ではない|じゃない')
    for index, raw in enumerate(fragments):
        fragment = raw.strip()
        if (not re.search(r'昼|夜|昨日|明日|明後日|あした|あす|きのう', fragment)
                and not DATE_WORD.search(fragment)
                and re.search(r'復帰|戻(?:ります|りました)|出勤します|お給仕します', fragment)):
            previous = index - 1
            while previous >= 0 and not fragments[previous].strip():
                previous -= 1
            if previous >= 0 and re.search(r'お休み|おやすみ|欠勤', fragments[previous]):
                fragments[previous] = date_context(fragments[previous])
        correction = negation.search(fragment)
        if correction is None:
            continue
        if not fragment[:correction.start()].strip():
            # "昼1号店、ではなく昼2号店" retracts the preceding clause.
            previous = index - 1
            while previous >= 0 and not fragments[previous].strip():
                previous -= 1
            if previous >= 0:
                fragments[previous] = date_context(fragments[previous])
        context = date_context(fragment[:correction.start()])
        replacement = fragment[correction.end():] if correction[0] in (
            'ではなく', 'じゃなく', 'でなく') else ''
        fragments[index] = context + ' ' + replacement
    return list(zip(fragments, sentences))


def third_party_subject(text, name, roster):
    if (re.search(r'(?:ちゃん|さん)\s*(?:は|が|も|の)', text)
            or re.search(r'@\w+|(?:友達|友人|彼女|相方|店舗|お店|スタッフ)\s*(?:が|は|の)', text)
            or any(other != name and re.search(
                re.escape(other) + r'\s*(?:は|が|も)', text) for other in roster)):
        return True
    without_dates = DATE_WORD.sub(' ', text)
    own_subjects = {name, '私', 'わたし', '自分', '僕', 'ぼく', 'あたし',
                    '今日', '本日', '昼', 'お昼', '夜', '今夜', '魔法', '体調',
                    '出勤', 'お給仕', '時間', '予定'}
    for match in re.finditer(r'([ぁ-んァ-ヶ一-龠ーA-Za-z0-9_]+?)\s*(?:は|が)', without_dates):
        subject = match[1]
        if subject.endswith(('です', 'ます', 'でした', 'ました')):
            continue
        if subject in ('で', 'じゃ') or re.search(r'(?:[1-4]号店|お休み|欠勤|遅刻)で$', subject):
            continue
        subject = re.sub(r'^(?:今日|本日)(?:の)?(?=.)', '', subject)
        if subject not in own_subjects:
            return True
    return False


def parse_events(text, created, date, shifts, name='', roster=()):
    if calendar_day(created) != date:
        return [], 'outside_date'
    text = unicodedata.normalize('NFKC', text)
    if (re.search(r'(^|\n)\s*(?:RT\s+@|@\w+|>|引用|転載)', text)
            or re.search(r'[「」『』“”"]', text)):
        return [], 'quoted_text'
    if re.search(r'(?:予定|つもり)(?:でした|だった)|'
                 r'(?:お休み|おやすみ|欠勤|遅刻)(?:する)?(?:でした|だった)|'
                 r'撤回|取り消|取消|訂正|(?:^|[\n、。])\s*違(?:います|いました)', text):
        return [], 'unresolved_shift'
    if (re.search(r'お休み|おやすみ|欠勤|遅刻', text)
            and re.search(r'予定|つもり|思って', text)):
        return [], 'unresolved_shift'
    if third_party_subject(text, name, roster):
        return [], 'unresolved_shift'
    if re.search(r'勘違い|間違い', text):
        for fragment in re.split(r'[\n。！？!?、]+', text):
            if re.search(r'勘違い|間違い', fragment) and not re.fullmatch(
                    r'\s*[1-4](?:号店)?\s*[→➡️]+\s*[1-4](?:号店)?'
                    r'\s*だと勘違いしていた\s*', fragment):
                return [], 'unresolved_shift'
    fragments = announcement_clauses(text)
    scoped = False
    saw_scope = False
    unresolved = False
    events = []
    inherited_shifts = []
    previous_sentence = None
    for fragment, sentence in fragments:
        if sentence != previous_sentence:
            inherited_shifts = []
            previous_sentence = sentence
        fragment = re.sub(r'https?://\S+', '', fragment).strip()
        if not fragment:
            continue
        dates = DATE_WORD.findall(fragment)
        if dates:
            inherited_shifts = []
            scoped = all((int(month), int(day)) == (date.month, date.day)
                         for month, day in dates)
            if not scoped:
                continue
        if re.search(r'今日|本日', fragment):
            scoped = True
        if re.search(r'昨日|明日|明後日|あした|あす|きのう', fragment):
            scoped = False
            continue
        saw_scope |= scoped
        if not scoped:
            continue
        if (re.search(r'休憩|昼休み|お昼休み|[「『”"]|勘違い|間違|訂正|'
                      r'ではな[くい]|じゃな[くい]|ではありません|'
                      r'休みません|休まない|遅れません|復帰しません|戻りません|'
                      r'(?:欠勤|遅刻|出勤|お給仕|お休み)しません|'
                      r'聞きました|とのこと|らしい|だそう|によると|引用', fragment)
                or re.search(r'(?:ちゃん|さん)(?:は|が|も|の)', fragment)
                or re.search(r'@\w+|(?:友達|友人|彼女|相方|店舗|お店|スタッフ)(?:が|は|の)', fragment)
                or any(other != name and re.search(re.escape(other) + r'(?:は|が|も)',
                                                   fragment) for other in roster)):
            unresolved = True
            continue
        mentioned = [shift for shift in ('昼', '夜') if shift in fragment]
        if mentioned:
            inherited_shifts = mentioned
        stated = [shift for shift in (shifts or mentioned) if shift in mentioned]
        ambiguous = re.search(
            r'人間の姿になれませんでした|魔法がうまくかかりませんでした|'
            r'かもしれ(?:ません|ない)|かも|未定|わからない|分からない|行けるか|なら', fragment)
        absence = re.search(
            r'お休み(?:します|です|いただきます|させていただきます|になりました)?|'
            r'おやすみ(?:します|です)|休み(?:ます|です)|欠勤(?:します|です)?|'
            r'お給仕できません|出勤できません|行けなくなりました', fragment)
        late = re.search(r'遅刻(?:します|です)?|遅れ(?:ます|そう|て)|遅くなります', fragment)
        returned = re.search(r'復帰(?:します|しました|です)|戻(?:ります|りました)|'
                             r'出勤再開|お給仕再開', fragment)
        if absence and returned:
            unresolved = True
            continue
        kind_match = next(((kind, match) for kind, match in (
            ('uncertain', ambiguous), ('late', late), ('absence', absence),
            ('return', returned)) if match), None)
        if kind_match:
            kind, match = kind_match
            if len(mentioned) > 1 and not (
                    kind == 'absence' and re.search(
                        r'昼(?:も|と)?夜|夜(?:も|と)?昼|終日|全日|一日', fragment)):
                unresolved = True
                continue
            event_shifts = stated
            if kind == 'absence' and not mentioned:
                if inherited_shifts:
                    event_shifts = [shift for shift in (shifts or inherited_shifts)
                                    if shift in inherited_shifts]
                elif re.search(r'今日|本日|終日|全日|一日', fragment) or DATE_WORD.search(fragment):
                    event_shifts = list(shifts)
            if not event_shifts:
                unresolved = True
                continue
            for shift in event_shifts:
                event = {'shift': shift, 'kind': kind, 'excerpt': match.group(0)[:160]}
                if kind in ('late', 'return'):
                    stores = set(re.findall(r'([1-4])号店', fragment))
                    if len(stores) == 1:
                        event['storeId'] = 's' + stores.pop()
                    explicit = re.search(
                        r'(?<!\d)([01]?\d|2[0-3])(?::([0-5]\d)|時(?:([0-5]?\d)分)?)(?!\d)',
                        fragment)
                    if explicit:
                        event['time'] = f'{int(explicit[1]):02d}:{int(explicit[2] or explicit[3] or 0):02d}'
                events.append(event)
            continue
        if re.search(r'だった|でした|いません|行きません|行かない|出ません|出ない|違います',
                     fragment):
            unresolved = True
            continue
        matches = list(re.finditer(r'([1-4])号店\s*(昼|夜)|'
                                   r'(?:お)?(昼|夜)\s*(?:は|の|に)?\s*([1-4])号店',
                                   fragment))
        for match in matches:
            store, shift = (match[1], match[2]) if match[1] else (match[4], match[3])
            if not shifts or shift in shifts:
                events.append({'shift': shift, 'kind': 'placement',
                               'storeId': 's' + store, 'excerpt': match.group(0)[:160]})
    unique = []
    for shift in shifts or ('昼', '夜'):
        selected = [event for event in events if event['shift'] == shift]
        stores = {event['storeId'] for event in selected if event['kind'] == 'placement'}
        kinds = {event['kind'] for event in selected}
        if len(stores) > 1 or ('absence' in kinds and kinds & {'placement', 'late', 'return'}):
            selected = [{'shift': shift, 'kind': 'uncertain',
                         'excerpt': ' / '.join(event['excerpt'] for event in selected)[:160]}]
        for event in selected:
            event['excerpt'] = ' '.join(event['excerpt'].split())[:160]
            if event not in unique:
                unique.append(event)
    return unique, ('events' if unique else 'unresolved_shift' if unresolved
                    else 'no_event' if saw_scope else 'explicit_date_required')


def validate_post(candidate, payload, target, now, binding=None, roster=(), analyzer=None):
    tid, uid = candidate['id'], candidate['authorId']
    if not isinstance(payload, dict) or not official.matching_id(payload, tid):
        raise Failure('response_id_mismatch')
    author = payload.get('user')
    if (not isinstance(author, dict) or not official.matching_id(author, uid)
            or author.get('screen_name') != candidate['authorScreenName']
            or uid == official.AUTHOR_ID
            or candidate['name'] != target['name']
            or candidate['authorScreenName'].casefold() != target['handle'].casefold()
            or candidate['url'] != public_url(candidate['authorScreenName'], tid)
            or (binding and not same_identity(binding, uid, target['handle']))):
        raise Failure('author_mismatch')
    try:
        created = official.timestamp(payload.get('created_at'))
        metadata_source = candidate.get('metadataSource', 'search')
        if metadata_source not in ('search', 'saved_post', 'saved_binding'):
            raise Failure('invalid_saved_metadata')
        searched = (official.timestamp(candidate['searchCreatedAt'])
                    if candidate.get('searchCreatedAt') is not None else None)
        if searched is None and (metadata_source == 'search' or binding is None):
            raise Failure('saved_post_identity_required')
        date = dt.date.fromisoformat(candidate['date'])
        if (created > now or calendar_day(created) != date
                or searched is not None and abs((created - searched).total_seconds()) >= 1
                or 'sourceCreatedAt' in candidate and created != official.timestamp(candidate['sourceCreatedAt'])
                or abs((official.snowflake_time(tid) - created).total_seconds()) >= 2):
            raise Failure('timestamp_mismatch')
    except (ValueError, TypeError, OverflowError, OSError):
        raise Failure('invalid_created_at') from None
    if any(payload.get(key) for key in (
            'quoted_tweet', 'quoted_status', 'retweeted_status',
            'in_reply_to_status_id_str', 'in_reply_to_status_id', 'in_reply_to_user_id_str',
            'in_reply_to_screen_name')):
        return None, 'quoted_or_reply'
    text = payload.get('text')
    if not isinstance(text, str):
        raise Failure('missing_post_text')
    if analyzer is None:
        events, reason = parse_events(text, created, date, target['shifts'], target['name'], roster)
        links = None
        timing_facts = []
    else:
        events, links, timing_facts, reason = analyzer.parse_with_timing(
            text, created, date, target['shifts'], target['name'],
            post_id=tid, author_id=uid)
    if not events and not links and not timing_facts:
        return None, reason
    post = {
        'id': tid, 'url': candidate['url'], 'name': target['name'],
        'authorId': uid, 'authorScreenName': candidate['authorScreenName'], 'createdAt': stamp(created),
        'observedAt': stamp(now), 'date': date.isoformat(), 'events': events,
    }
    if links is not None:
        post['links'] = links
    if timing_facts:
        post['workTiming'] = work_timing.bind(timing_facts, post, 'personal-work-post')
    valid_post(post)
    return post, reason


class DurableHttp:
    def __init__(self, state, snapshot, http_state, date, targets, max_searches, max_posts,
                 clock=official.utc_now, sleep=time.sleep, *, scheduled=False, shared_source=None,
                 registry_guard=None, bindings=None):
        self.state, self.snapshot, self.http_state = state, snapshot, http_state
        self.date, self.targets = date, targets
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.caps = {'searches': max_searches, 'posts': max_posts}
        self.used = {'searches': 0, 'posts': 0}
        self.cooldowns = official.load_transport(http_state)
        self.post_target = None
        self.scheduled = scheduled
        self.shared_source = shared_source
        self.registry_guard = registry_guard
        self.bindings = bindings or (lambda: self.state['identityBindings'])

    def save(self):
        official.atomic_json(self.snapshot, self.state)

    def save_http(self):
        latest = official.load_transport(self.http_state)
        for host, until in latest.items():
            if host not in self.cooldowns or official.timestamp(until) > official.timestamp(self.cooldowns[host]):
                self.cooldowns[host] = until
        if self.shared_source is not None:
            for host, until in self.shared_source.state['cooldowns'].items():
                if host not in self.cooldowns or official.timestamp(until) > official.timestamp(self.cooldowns[host]):
                    self.cooldowns[host] = until
            for host, until in self.cooldowns.items():
                official.source_call(self.shared_source, 'set_cooldown', host, official.timestamp(until))
        official.atomic_json(self.http_state, {'schemaVersion': 1, 'cooldowns': self.cooldowns})

    def preflight(self):
        self.save()
        self.save_http()

    def check_window(self, target_name=None):
        self.check_registry(target_name)
        active = active_targets(self.targets, self.date, self.clock(), scheduled=self.scheduled)
        if not active or (target_name is not None and target_for_name(active, target_name) is None):
            raise Failure('outside_window')

    def check_registry(self, target_name=None):
        if self.registry_guard is not None:
            try:
                self.registry_guard.check(target_name)
            except ValueError as exc:
                raise RegistryFailure(str(exc)) from None

    def analysis_allowed(self):
        self.check_registry(self.post_target)
        active = active_targets(self.targets, self.date, self.clock(), scheduled=self.scheduled)
        return bool(active) and (self.post_target is None or target_for_name(active, self.post_target) is not None)

    def reserve(self, host, kind, target_name=None, url=None):
        if self.state['paused']:
            raise Failure('paused')
        target_name = target_name or (self.post_target if kind == 'posts' else None)
        self.check_window(target_name)
        self.cooldowns = official.load_transport(self.http_state)
        if host in self.cooldowns and official.timestamp(self.cooldowns[host]) > self.clock():
            raise Failure('shared_host_cooldown', retry_at=official.timestamp(self.cooldowns[host]))
        if self.shared_source is None:
            budget = self.state['budgets'].setdefault(self.date.isoformat(), {'searches': 0, 'posts': 0})
        else:
            budget = self.state['budgets'].get(self.date.isoformat(), {'searches': 0, 'posts': 0})
        if self.used[kind] >= self.caps[kind] or budget[kind] >= DAILY_LIMITS[kind]:
            raise Failure('budget_exhausted')
        previous = official.timestamp(self.state['lastRequests'][host]) if host in self.state['lastRequests'] else self.started
        self.sleep(max(0, 12 - (self.clock() - previous).total_seconds()))
        self.check_window(target_name)
        # Reservations are durable before any network side effect, including dry runs.
        self.save_http()
        if host in self.cooldowns and official.timestamp(self.cooldowns[host]) > self.clock():
            raise Failure('shared_host_cooldown', retry_at=official.timestamp(self.cooldowns[host]))
        receipt = None
        if self.shared_source is not None:
            self.check_window(target_name)
            receipt = official.source_call(self.shared_source, 'reserve', kind, url)
        budget = self.state['budgets'].setdefault(self.date.isoformat(), {'searches': 0, 'posts': 0})
        budget[kind] += 1
        self.used[kind] += 1
        self.state['lastRequests'][host] = stamp(self.clock())
        self.save()
        self.check_window(target_name)
        if receipt is not None:
            official.source_call(self.shared_source, 'issued', receipt)
            self.check_window(target_name)
        return receipt

    def deny(self, host, reason, status=None, retry_after=None):
        now = self.clock()
        until = now + dt.timedelta(hours=1)
        if retry_after:
            try:
                until = now + dt.timedelta(seconds=max(0, int(retry_after)))
            except (ValueError, OverflowError):
                try:
                    until = official.timestamp(
                        email.utils.parsedate_to_datetime(retry_after).isoformat())
                except (ValueError, TypeError, OverflowError):
                    pass
        prior = official.load_transport(self.http_state).get(host)
        if prior:
            until = max(until, official.timestamp(prior))
        until = max(now, until)
        self.cooldowns[host] = stamp(until)
        self.state['paused'] = {'reason': reason, 'host': host, 'at': stamp(now),
                                'retryAt': stamp(until)}
        if status is not None:
            self.state['paused']['httpStatus'] = status
        if self.shared_source is not None:
            official.source_call(self.shared_source, 'set_cooldown', host, until, paused=self.state['paused'])
        errors = []
        for save in (self.save, self.save_http):
            try:
                save()
            except OSError as exc:
                errors.append(exc)
        if errors:
            raise InfrastructureFailure('transport_state_save_failed') from None
        raise Failure(reason, status, until)


class PersonalClient(official.PublicClient):
    def __init__(self, durable):
        super().__init__(clock=durable.clock, sleep=durable.sleep)
        self.durable = durable
        self.urls = {*search_urls(durable.date),
                     *(account_search_url(target['handle']) for target in durable.targets.values())}

    def open(self, request, timeout=35):
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        if url in self.urls:
            kind = 'searches'
        elif (parsed.scheme == 'https' and parsed.hostname == POST_HOST
              and not parsed.username and not parsed.port and not parsed.fragment
              and parsed.path == '/tweet-result'
              and re.fullmatch(r'id=[1-9][0-9]{9,24}&lang=ja&token=a', parsed.query)):
            kind = 'posts'
        else:
            raise Failure('route_refused')
        host = parsed.hostname
        target_name = None
        if kind == 'searches':
            target_name = next((name for name, target in self.durable.targets.items()
                                if account_search_url(target['handle']) == url), None)
        else:
            target_name = self.durable.post_target
            if target_name is None:
                tid = urllib.parse.parse_qs(parsed.query)['id'][0]
                target_name = next((item['name'] for item in self.durable.state['pending']
                                    if item['id'] == tid), None)
            if target_name is None:
                raise Failure('missing_post_target')
        if self.durable.shared_source is not None:
            self.durable.check_window(target_name)
            cached = official.source_call(self.durable.shared_source, 'cached', kind, url)
            if cached is not None:
                return io.BytesIO(cached['body'])
        receipt = self.durable.reserve(host, kind, target_name=target_name, url=url)
        finished = False
        response_status = None
        try:
            self.durable.check_window(target_name)
            with self.opener.open(urllib.request.Request(url), timeout=min(timeout, 35)) as response:
                status = response.getcode()
                response_status = status
                headers = response.headers
                if status in (401, 403, 429):
                    self.durable.deny(host, 'access_denied', status, headers.get('Retry-After'))
                if status != 200:
                    raise Failure('unexpected_http_status', status)
                official.check_http_metadata(headers, self.clock())
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    raise Failure('response_too_large')
                text = body.decode('utf-8', 'strict')
                title = re.search(r'<title[^>]*>(.*?)</title>', text, re.I | re.S)
                challenge = bool(title and DENIAL.search(html.unescape(title[1])))
                if challenge:
                    self.durable.deny(host, 'access_denied', status, headers.get('Retry-After'))
                if kind == 'searches':
                    if '__NEXT_DATA__' not in text and DENIAL.search(text):
                        challenge = True
                    if '__NEXT_DATA__' in text:
                        error = search_page(text).get('searchError')
                        challenge |= bool(DENIAL.search(json.dumps(error, ensure_ascii=False)))
                else:
                    try:
                        error = json.loads(text)
                        if isinstance(error, dict):
                            challenge |= any(DENIAL.search(str(error.get(key, '')))
                                             for key in ('error', 'errors', 'message', 'detail'))
                    except ValueError:
                        challenge |= bool(DENIAL.search(text))
                if challenge:
                    self.durable.deny(host, 'access_denied', status, headers.get('Retry-After'))
                if receipt is not None:
                    official.source_call(self.durable.shared_source, 'finish', receipt, http_status=status)
                    finished = True
                    official.source_call(self.durable.shared_source, 'remember', receipt, body)
                return io.BytesIO(body)
        except urllib.error.HTTPError as exc:
            status, retry = exc.code, exc.headers.get('Retry-After') if exc.headers else None
            response_status = status
            exc.close()
            if status in (401, 403, 429):
                self.durable.deny(host, 'access_denied', status, retry)
            raise Failure('http_error', status) from None
        except Failure as exc:
            if exc.reason == 'redirect_refused':
                self.durable.deny(host, 'redirect_refused', exc.status)
            raise
        except (OSError, http.client.HTTPException, UnicodeError):
            raise Failure('network_error') from None
        finally:
            if receipt is not None and not finished:
                official.source_call(self.durable.shared_source, 'finish', receipt, 'failed',
                                     http_status=response_status)

    def search(self, url, targets, date, now, bindings):
        with self.open(urllib.request.Request(url)) as response:
            try:
                return discover(response.read().decode('utf-8'), targets, date, now, bindings)
            except Failure as exc:
                if exc.reason == 'access_denied':
                    self.durable.deny(SEARCH_HOST, 'access_denied', exc.status)
                raise

    def fetch_post(self, tid):
        try:
            return self.importer.fetch(tid)
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            raise Failure('invalid_post_json') from None


def saved_candidates(state, payloads, date):
    """Only explicit, already known IDs; payload metadata cannot create a target."""
    known = {item['id']: item for item in state['resolved']}
    known.update({item['id']: item for item in state['posts']})
    known.update({item['id']: item for item in state['pending']})
    candidates = []
    for tid in payloads:
        item = known.get(tid)
        if item is None or item['date'] != date.isoformat():
            raise ValueError('unknown_saved_post')
        binding = state['identityBindings'].get(item['name'])
        if item.get('searchCreatedAt') is not None:
            candidates.append(copy.deepcopy(item))
        elif binding:
            candidate = {
                'id': tid, 'url': item['url'], 'name': item['name'], 'date': item['date'],
                'authorId': binding['authorId'], 'authorScreenName': binding['authorScreenName'],
                'searchCreatedAt': None,
                'metadataSource': 'saved_post' if 'createdAt' in item else 'saved_binding'}
            if 'createdAt' in item:
                candidate['sourceCreatedAt'] = item['createdAt']
            candidates.append(candidate)
        else:
            raise ValueError('saved_post_identity_required')
    return candidates


def update_coverage(state, targets, date, now, *, scheduled=False, search_limit=None):
    day = date.isoformat()
    rows = state.get('coverage', {}).get(day, {})
    history = state.get('searchHistory', {}).get(day, {})
    active = active_targets(targets, date, now, scheduled=scheduled)
    for name, row in rows.items():
        target = targets.get(name, row)
        aliases = target.get('aliases', [name])
        posts = sorted((post for post in state['posts'] if post['date'] == day
                        and post['name'] in aliases
                        and (not target['handle']
                             or post['authorScreenName'].casefold() == target['handle'].casefold())),
                       key=lambda post: (official.timestamp(post['createdAt']), int(post['id'])))
        row['postIds'] = [post['id'] for post in posts]
        if name not in targets:
            continue
        prior_search = history.get(name, {})
        row['searchedAt'] = (prior_search.get('attemptedAt')
                             if prior_search.get('handle', '').casefold() == target['handle'].casefold() else None)
        scopes = {}
        for post in posts:
            links = post.get('links')
            if links is None:
                links = legacy_links(post)
            for link in links:
                affected = target['shifts'] if link['scope'] == 'unspecified' else [link['scope']]
                for scope in affected:
                    if scope not in target['shifts']:
                        continue
                    previous = scopes.get(scope)
                    # Unscoped work cannot establish a return from an explicit cancellation.
                    if link['scope'] == 'unspecified' and previous and previous['status'] != 'work':
                        continue
                    scopes[scope] = {'id': post['id'], 'status': link['status']}
            for event in post['events']:
                if event['kind'] == 'absence':
                    scopes[event['shift']] = {'id': post['id'], 'status': 'withdrawn'}
        row['linkScopes'] = [{'scope': scope, 'id': value['id']}
                             for scope, value in sorted(scopes.items()) if value['status'] == 'work']
        pending = [item for item in state['pending'] if item['name'] in aliases and item['date'] == day]
        resolved = [item for item in state['resolved'] if item['name'] in aliases and item['date'] == day]
        failed_source = next((source for source in state['lastRun'].get('sources', [])
                              if source['url'] == account_search_url(target['handle'])
                              and source['status'] == 'failed'), None)
        authority_failure = next((failure['reason'] for failure in state['lastRun'].get('failures', [])
                                  if failure['reason'] in ('registry_changed', 'registry_unavailable')), None)
        pending_reason = None
        if pending:
            reason = pending[-1]['reason']
            pending_reason = {'discovered': 'metadata_only',
                              'azure_saved_body_required': 'body_unavailable',
                              'missing_post_text': 'body_unavailable'}.get(reason, reason)
        if authority_failure:
            row['reason'] = authority_failure
        elif 'registry' in row['origins'] and pending_reason:
            row['reason'] = pending_reason
        elif 'registry' in row['origins'] and failed_source:
            row['reason'] = failed_source['reason']
        elif row['linkScopes']:
            row['reason'] = 'verified_post_available'
        elif any(value['status'] == 'conflict' for value in scopes.values()):
            row['reason'] = 'conflicting_guidance'
        elif any(value['status'] == 'withdrawn' for value in scopes.values()):
            row['reason'] = 'withdrawn'
        elif any(post['events'] for post in posts):
            row['reason'] = 'observed_events'
        elif any(post.get('links') for post in posts):
            row['reason'] = 'verified_annotation_available'
        elif any(post.get('workTiming', {}).get('facts') for post in posts):
            row['reason'] = 'observed_timing'
        elif pending:
            row['reason'] = pending_reason
        elif failed_source:
            row['reason'] = failed_source['reason']
        elif state['paused']:
            row['reason'] = 'paused'
        elif name not in active:
            row['reason'] = 'outside_window'
        elif resolved:
            row['reason'] = 'analyzed_no_link'
        elif ('registry' in row['origins'] and search_limit is not None and not row['searchedAt']
              and state['lastRun'].get('requests', {}).get('searches', 0) >= search_limit):
            row['reason'] = 'search_budget_deferred'
        else:
            row['reason'] = 'no_candidate_in_checked_pages' if row['searchedAt'] else 'not_searched'
    return copy.deepcopy(rows)


def collect(state, durable, client, targets, date, max_searches, max_posts,
            clock=official.utc_now, roster=(), analyzer=None, saved_payloads=None):
    state['checkedAt'] = stamp(clock())
    sources, failures, new_posts = [], [], []
    resolved = {item['id'] for item in state['resolved']} | {post['id'] for post in state['posts']}
    pending = {item['id']: item for item in state['pending']
               if item['id'] not in resolved or item['reason'].startswith('azure_')
               or target_for_name(targets, item['name']) is None}
    active = active_targets(targets, date, clock(), scheduled=durable.scheduled)
    if saved_payloads is not None:
        active = targets
        for candidate in saved_candidates(state, saved_payloads, date):
            if target_for_name(targets, candidate['name']) is None:
                continue
            pending.setdefault(candidate['id'], {
                **candidate, 'reason': 'azure_saved_body_required',
                'firstSeenAt': stamp(clock()), 'lastAttemptAt': None, 'attempts': 0})
    skipped = 0
    early_status = 'paused' if state['paused'] else 'outside-window' if not active else None
    searches = (target_searches(targets, date, state, clock(), max_searches, scheduled=durable.scheduled)
                if not early_status and saved_payloads is None else ())
    for name, url in searches:
        before = durable.used['searches']
        try:
            active_now = active_targets(targets, date, clock(), scheduled=durable.scheduled)
            if name not in active_now:
                continue
            durable.check_registry(name)
            candidates = client.search(url, {name: active_now[name]},
                                       date, clock(), durable.bindings())
            durable.check_registry(name)
            sources.append({'url': url, 'status': 'ok', 'candidateCount': len(candidates)})
            for candidate in candidates:
                if (candidate['name'] != name
                        or candidate['authorScreenName'].casefold() != targets[name]['handle'].casefold()):
                    continue
                tid = candidate['id']
                if tid in resolved:
                    skipped += 1
                    continue
                if tid not in pending:
                    pending[tid] = {**candidate, 'reason': 'discovered',
                                    'firstSeenAt': stamp(clock()), 'lastAttemptAt': None,
                                    'attempts': 0}
            state['pending'] = list(pending.values())
            durable.save()
        except Failure as exc:
            sources.append({'url': url, 'status': 'failed', **exc.facts()})
            failures.append(exc.facts())
            if isinstance(exc, RegistryFailure) or exc.reason in ('budget_exhausted', 'outside_window', 'shared_host_cooldown',
                              'source_budget_exhausted', 'source_paused', 'source_host_cooldown') or state['paused']:
                break
        finally:
            if durable.used['searches'] > before:
                state.setdefault('searchHistory', {}).setdefault(date.isoformat(), {})[name] = {
                    'handle': targets[name]['handle'], 'attemptedAt': state['lastRequests'][SEARCH_HOST]}
                durable.save()
    attempted = deferred = 0
    grouped, positions, previous_attempt = {}, {}, {}
    for item in pending.values():
        grouped.setdefault(item['name'], []).append(item)
    for name, items in grouped.items():
        for index, item in enumerate(sorted(items, key=lambda item: -int(item['id']))):
            positions[item['id']] = index
        previous_attempt[name] = max(
            [item['lastAttemptAt'] for item in items if item.get('lastAttemptAt')]
            + [item['resolvedAt'] for item in state['resolved']
               if item['name'] == name] + [''])

    def priority(item):
        target = target_for_name(targets, item['name']) or {'shifts': []}
        deadline = target_deadline(target, scheduled=durable.scheduled)
        return deadline, positions[item['id']], previous_attempt[item['name']], -int(item['id'])

    for item in sorted(pending.values(), key=priority):
        if item['date'] != date.isoformat():
            continue
        if saved_payloads is not None and item['id'] not in saved_payloads:
            continue
        target = target_for_name(targets if saved_payloads is not None else active_targets(
            targets, date, clock(), scheduled=durable.scheduled), item['name'])
        if target is None or target['handle'].casefold() != item['authorScreenName'].casefold():
            continue
        try:
            durable.check_registry(item['name'])
        except Failure as exc:
            failures.append({'id': item['id'], **exc.facts()})
            continue
        binding = durable.bindings().get(item['name'])
        if binding and not same_identity(binding, item['authorId'], item['authorScreenName']):
            item['reason'] = 'author_mismatch'
            failures.append({'id': item['id'], 'reason': item['reason']})
            continue
        if saved_payloads is None and item['reason'].startswith('azure_'):
            failures.append({'id': item['id'], 'reason': item['reason']})
            deferred += 1
            continue
        if attempted >= max_posts or state['paused']:
            item['reason'] = 'paused' if state['paused'] else 'post_limit'
            if saved_payloads is not None:
                item['reason'] = 'azure_saved_' + item['reason']
            deferred += 1
            continue
        if (saved_payloads is None and analyzer is not None
                and callable(getattr(analyzer, 'check_capacity', None))):
            durable.post_target = item['name']
            try:
                analyzer.check_capacity()
            except azure.AnalysisFailure as exc:
                if not isinstance(exc, azure.RegistryFailure):
                    item['reason'] = 'analysis_capacity_deferred'
                failures.append({'id': item['id'], **exc.facts()})
                deferred += 1
                continue
            finally:
                durable.post_target = None
        unchanged = copy.deepcopy(item)
        attempted += 1
        item['lastAttemptAt'] = stamp(clock())
        item['attempts'] += 1
        state['pending'] = list(pending.values())
        durable.save()
        try:
            durable.post_target = item['name']
            value = saved_payloads[item['id']] if saved_payloads is not None else client.fetch_post(item['id'])
            if analyzer is not None:
                if saved_payloads is None:
                    durable.check_window(item['name'])
                item['reason'] = 'azure_saved_body_required'
                state['pending'] = list(pending.values())
                durable.save()
            post, reason = validate_post(
                item, value, target, clock(), durable.bindings().get(item['name']), roster, analyzer)
            durable.check_registry(item['name'])
            if analyzer is not None and reason == 'quoted_or_reply':
                raise azure.AnalysisFailure('azure_ungrounded')
            previous = next((entry for entry in state['posts'] if entry['id'] == item['id']), None)
            if analyzer is not None and previous and reason == 'no_event':
                raise azure.AnalysisFailure('azure_no_event_conflict')
            if (analyzer is not None and previous and post
                    and ('workTiming' in previous or 'workTiming' in post)):
                old_timing, update = previous.get('workTiming'), post.get('workTiming')
                if old_timing is not None and not (update or {}).get('facts'):
                    post['workTiming'] = copy.deepcopy(old_timing)
                else:
                    try:
                        merged = work_timing.merge(old_timing, update)
                    except work_timing.WorkTimingLimitError:
                        raise azure.AnalysisFailure('azure_work_timing_storage_limit') from None
                    if old_timing is not None and (
                            {work_timing.fact_key(fact): fact for fact in merged['facts']}
                            == {work_timing.fact_key(fact): fact for fact in old_timing['facts']}):
                        merged = copy.deepcopy(old_timing)
                    post['workTiming'] = merged
            state['identityBindings'][item['name']] = {
                'authorId': item['authorId'], 'authorScreenName': item['authorScreenName'],
                'verifiedAt': stamp(clock())}
            state['resolved'] = [entry for entry in state['resolved'] if entry['id'] != item['id']]
            state['resolved'].append({
                'id': item['id'], 'url': item['url'], 'name': item['name'], 'date': item['date'],
                'reason': reason, 'resolvedAt': stamp(clock())})
            pending.pop(item['id'])
            if analyzer is not None and previous:
                if post:
                    supplied_shifts = {event['shift'] for event in post['events']}
                    post['events'].extend(copy.deepcopy(event) for event in previous['events']
                                          if event['shift'] not in supplied_shifts)
                    post['events'].sort(key=lambda event: ('昼', '夜').index(event['shift']))
                    if 'links' in previous or 'links' in post:
                        supplied_scopes = {link['scope'] for link in post.get('links', [])}
                        retained = previous.get('links', legacy_links(previous))
                        post.setdefault('links', []).extend(copy.deepcopy(link) for link in retained
                                                            if link['scope'] not in supplied_scopes)
                if (post and previous['events'] == post['events']
                        and previous.get('links') == post.get('links')
                        and previous.get('workTiming') == post.get('workTiming')):
                    post = previous
                else:
                    analyzer.state['history'].append(copy.deepcopy(previous))
                state['posts'] = [entry for entry in state['posts'] if entry['id'] != item['id']]
            if post:
                state['posts'].append(post)
                if previous != post:
                    new_posts.append(post)
                if any(event['kind'] == 'uncertain' for event in post['events']):
                    failures.append({'id': item['id'], 'reason': 'uncertain_guidance'})
            elif reason in ('unresolved_shift', 'explicit_date_required'):
                failures.append({'id': item['id'], 'reason': reason})
        except (Failure, azure.AnalysisFailure) as exc:
            facts = exc.facts()
            if isinstance(exc, (RegistryFailure, azure.RegistryFailure)):
                item.clear()
                item.update(unchanged)
            elif saved_payloads is not None and not facts['reason'].startswith('azure_'):
                facts['reason'] = 'azure_saved_' + facts['reason']
                item.update(facts)
            else:
                item.update(facts)
            failures.append({'id': item['id'], **facts})
            if exc.reason in ('budget_exhausted', 'shared_host_cooldown', 'outside_window'):
                deferred += 1
        finally:
            durable.post_target = None
        state['pending'] = list(pending.values())
        durable.save()
    codes = {failure['reason'] for failure in failures}
    if state['paused'] or codes & {'shared_host_cooldown', 'source_paused', 'source_host_cooldown'}:
        status = 'paused'
    elif early_status:
        status = early_status
    elif codes & {'budget_exhausted', 'azure_budget_exhausted', 'source_budget_exhausted'}:
        status = 'budget-exhausted'
    elif codes and codes <= {'azure_work_timing_storage_limit', 'azure_capacity_hold',
                            'azure_capacity_profile_stale'}:
        status = 'partial'
    elif failures:
        status = 'partial' if any(source['status'] == 'ok' for source in sources) or new_posts else 'unavailable'
    elif deferred:
        status = 'partial'
    elif new_posts:
        status = 'ok'
    elif skipped:
        status = 'no-new'
    else:
        status = 'no-results'
    if status in ('ok', 'no-new', 'no-results'):
        state['lastSuccessAt'] = stamp(clock())
    state['posts'].sort(key=lambda post: (post['createdAt'], int(post['id'])))
    state['pending'] = list(pending.values())
    state['lastRun'] = {
        'status': status, 'date': date.isoformat(), 'dateBasis': 'JST calendar date, 00:00 boundary',
        'sourceCount': sum(source['status'] == 'ok' for source in sources),
        'sources': sources, 'targetCount': len(targets), 'activeTargetCount': len(active),
        'attemptedCount': attempted, 'requests': dict(durable.used),
        'newPostCount': len(new_posts), 'newEventCount': sum(len(post['events']) for post in new_posts),
        'skippedResolvedCount': skipped, 'pendingCount': len(pending), 'deferredCount': deferred,
        'failures': failures, 'finishedAt': stamp(clock()), 'complete': False}
    coverage = update_coverage(state, targets, date, clock(), scheduled=durable.scheduled,
                               search_limit=max_searches)
    durable.save()
    return {'component': 'personal', **state['lastRun'], 'budgets': state['budgets'],
            'paused': state['paused'], 'coverage': coverage}, (3 if status in ('paused', 'unavailable')
                                      else 2 if status in ('partial', 'budget-exhausted') else 0)


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='one bounded run (default)')
    parser.add_argument('--snapshot', type=Path, required=True, help='private durable canonical state')
    parser.add_argument('--http-state', type=Path, required=True, help='shared official HTTP sidecar')
    parser.add_argument('--publish', type=Path, help='optional public fact-only JSON mirror')
    parser.add_argument('--report', type=Path, help='fact-only component report')
    parser.add_argument('--seed', type=Path, default=ROOT / 'data' / 'personal-shifts.json')
    parser.add_argument('--schedule', type=Path, default=ROOT / 'data' / 'schedule.js')
    parser.add_argument('--members', type=Path, default=ROOT / 'data' / 'members.json')
    parser.add_argument('--half-month-snapshot', type=Path, help='validated effective half-month public feed')
    parser.add_argument('--insights', type=Path, default=ROOT / 'data' / 'store-insights.js')
    parser.add_argument('--accounts', type=Path, default=ROOT / 'tools' / 'data' / 'accounts.csv')
    parser.add_argument('--observations', type=Path, default=ROOT / 'data' / 'observed-shifts.json',
                        help='validated official names/notices for the same-run target population')
    parser.add_argument('--node', type=Path, help='existing Node executable for the local schedule JS')
    parser.add_argument('--date', help='JST calendar date; only today may make requests')
    parser.add_argument('--max-searches', type=int, default=2, help='maximum 0..3 pages/run')
    parser.add_argument('--max-posts', type=int, default=3, help='maximum 0..3 new individual GETs/run')
    parser.add_argument('--dry-run', action='store_true', help='persist private safety/facts, do not publish')
    parser.add_argument('--analysis-backend', choices=('rules', 'azure'), default='rules')
    parser.add_argument('--ai-state', type=Path, help='existing shared AI usage ledger')
    parser.add_argument('--analysis-run-id', help='shared official/personal run identity')
    parser.add_argument('--source-state', type=Path, help='existing shared source reservation ledger')
    parser.add_argument('--source-run-id', help='shared official/personal/schedule source run identity')
    parser.add_argument('--analysis-limit', type=int, default=3, help='personal AI allocation, 1..3')
    parser.add_argument('--scheduled', action='store_true', help='also stop personal requests after 18:00 JST')
    parser.add_argument('--analyze-saved', type=Path,
                        help='Azure only: known ID -> saved payload JSON; no Yahoo/X requests')
    return parser


def run(args, clock=official.utc_now, sleep=time.sleep, client_factory=PersonalClient,
        analyzer_factory=azure.AzureAnalyzer, environment=None):
    snapshot, http_state = args.snapshot.resolve(), args.http_state.resolve()
    publish = args.publish.resolve() if args.publish else None
    report_path = args.report.resolve() if args.report else None
    writes = [path for path in (snapshot, http_state, publish, report_path) if path]
    source_path = args.source_state.resolve() if args.source_state else None
    if bool(source_path) != bool(args.source_run_id):
        raise ValueError('invalid_source_configuration')
    if source_path:
        writes.append(source_path)
    if args.ai_state:
        writes.append(args.ai_state.resolve())
        if args.analysis_backend != 'azure' or not args.analysis_run_id:
            raise ValueError('shared_analysis_configuration_required')
    elif args.analysis_run_id:
        raise ValueError('shared_analysis_state_required')
    if args.scheduled and (args.analyze_saved or (args.analysis_backend == 'azure' and not args.ai_state)):
        raise ValueError('invalid_scheduled_analysis_configuration')
    inputs = {path.resolve() for path in (
        args.schedule, args.insights, args.accounts, args.observations, args.members)}
    if args.half_month_snapshot:
        inputs.add(args.half_month_snapshot.resolve())
    if args.analyze_saved:
        if args.analysis_backend != 'azure':
            raise ValueError('saved_analysis_requires_azure')
        inputs.add(args.analyze_saved.resolve())
    if (len(set(writes)) != len(writes) or any(path.suffix != '.json' for path in writes)
            or any(path in inputs for path in writes)
            or snapshot == args.seed.resolve() or http_state == args.seed.resolve()
            or not http_state.name.endswith('.http-state.json')
            or snapshot == ROOT / 'data' / 'personal-shifts.json'
            or any(path.name in ('observed-shifts.json', 'schedule.js', 'store-insights.js')
                   for path in (snapshot, publish, report_path) if path)):
        raise ValueError('unsafe_storage_paths')
    official_lock = http_state.with_name(
        http_state.name.removesuffix('.http-state.json') + '.lock')
    lock_paths = {snapshot.with_suffix('.lock'), http_state.with_suffix('.lock'), official_lock}
    if publish:
        lock_paths.add(publish.with_suffix('.lock'))
    if set(writes) & lock_paths:
        raise ValueError('unsafe_storage_paths')
    with ExitStack() as locks:
        for path in sorted(lock_paths, key=lambda item: str(item).casefold()):
            locks.enter_context(official.ProcessLock(path))
        registry = members.load_registry(args.members)
        state = read_state(snapshot)
        merge_seed(state, read_state(args.seed, private=False), registry=registry)
        shared_source = None
        if source_path:
            source_usage = official.source_module()
            try:
                shared_source = locks.enter_context(source_usage.SharedSource(
                    source_path, run_id=args.source_run_id, component='personal',
                    clock=clock, sleep=sleep, personal_path=snapshot))
            except source_usage.SourceFailure as exc:
                raise InfrastructureFailure(exc.reason) from None
        date = dt.date.fromisoformat(args.date) if args.date else calendar_day(clock())
        schedule = read_js(args.schedule, 'SCHEDULE_DATA', args.node)
        half_bindings = {}
        if args.half_month_snapshot:
            spec = importlib.util.spec_from_file_location(
                'personal_half_month_schedule', ROOT / 'tools' / 'half-month-schedules.py')
            half_month = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(half_month)
            feed = azure.strict_json(args.half_month_snapshot.read_text(encoding='utf-8-sig'))
            half_bindings = copy.deepcopy(feed.get('identityBindings', {}))
            feed = half_month.public_state(feed)
            schedule = {**schedule, 'schedule': half_month.effective_schedule(
                schedule.get('schedule', {}), feed, registry=registry)}
        def current_half_bindings():
            if not args.half_month_snapshot:
                return {}
            current = azure.strict_json(args.half_month_snapshot.read_text(encoding='utf-8-sig'))
            half_month.public_state(current)
            return current.get('identityBindings', {})

        guard = members.RegistryGuard(
            args.members, bindings=lambda: (state['identityBindings'], current_half_bindings()))
        if guard.check() != registry:
            raise ValueError('registry_changed')
        insights = read_js(args.insights, 'STORE_INSIGHTS', args.node) if args.insights.exists() else None
        with args.accounts.open(encoding='utf-8-sig', newline='') as source:
            accounts = list(csv.DictReader(source))
        observations = official.load_snapshot(args.observations)
        targets = select_targets(schedule, insights, accounts, date, state, observations,
                                 registry=registry, binding_maps=(half_bindings,))
        durable = DurableHttp(state, snapshot, http_state, date, targets,
                              args.max_searches, args.max_posts, clock, sleep, scheduled=args.scheduled,
                              shared_source=shared_source, registry_guard=guard,
                              bindings=lambda: identity_bindings(
                                  registry, current_half_bindings(), state['identityBindings']))
        durable.preflight()
        analyzer = None
        if args.analysis_backend == 'azure':
            options = {'registry_guard': guard,
                       'review_names': {name for target in targets.values() for name in target['aliases']},
                       'deadline': None if args.analyze_saved else durable.analysis_allowed}
            if args.ai_state:
                spec = importlib.util.spec_from_file_location(
                    'personal_shared_usage', ROOT / 'tools' / 'analysis-state.py')
                usage_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(usage_module)
                try:
                    options['usage'] = locks.enter_context(usage_module.SharedUsage(
                        args.ai_state, run_id=args.analysis_run_id, component='personal',
                        clock=clock, sleep=sleep, request_limit=args.analysis_limit,
                        deadline=None if args.analyze_saved else durable.analysis_allowed))
                except usage_module.UsageFailure as exc:
                    raise InfrastructureFailure(exc.reason) from None
            analyzer = analyzer_factory(state, durable.save, azure_context(),
                                        os.environ if environment is None else environment,
                                        clock=clock, sleep=sleep, **options)
            durable.save()
        payloads = None
        if args.analyze_saved:
            if args.analyze_saved.stat().st_size > MAX_BODY:
                raise ValueError('saved_posts_too_large')
            payloads = azure.strict_json(args.analyze_saved.read_text(encoding='utf-8-sig'))
            if (not isinstance(payloads, dict) or not payloads
                    or any(not official.post_id(tid) or not isinstance(value, dict)
                           for tid, value in payloads.items())):
                raise ValueError('invalid_saved_posts')
        client = client_factory(durable) if payloads is None else None
        report, code = collect(state, durable, client, targets, date,
                               args.max_searches, args.max_posts, clock,
                               members.display_projection(registry)['knownNames'],
                               analyzer, payloads)
        report.update(dryRun=args.dry_run, published=False, exitCode=code)
        if shared_source is not None:
            report['sourceUsage'] = shared_source.report()
        if publish and not args.dry_run:
            official.atomic_json(publish, public_state(state))
            report['published'] = True
        official.write_report(report, report_path)
        return code


def main(argv=None):
    parser = argument_parser()
    try:
        args = parser.parse_args(argv)
        if (not 0 <= args.max_searches <= 3 or not 0 <= args.max_posts <= 3
                or not 1 <= args.analysis_limit <= 3):
            parser.error('--max-searches/--max-posts must be 0..3; --analysis-limit must be 1..3')
    except SystemExit as exc:
        if exc.code != 2:
            raise
        print(json.dumps({'component': 'personal', 'status': 'unavailable',
                          'reason': 'invalid_cli_arguments', 'exitCode': 4}))
        return 4
    try:
        return run(args)
    except (OSError, ValueError, KeyError, TypeError, OverflowError, InfrastructureFailure) as exc:
        reason = str(exc) if isinstance(exc, (ValueError, InfrastructureFailure)) else 'infrastructure_error'
        if not re.fullmatch(r'[a-z_]+', reason):
            reason = 'invalid_local_data'
        print(json.dumps({'component': 'personal', 'status': 'unavailable',
                          'reason': reason, 'exitCode': 4}))
        return 4


if __name__ == '__main__':
    sys.exit(main())
