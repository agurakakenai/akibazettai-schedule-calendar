"""Data-only collector-state orchestration for trusted GitHub Actions main runs.

CLI: --mode restore|collect|personal|both|daily-guidance|apply-saved
collect remains official-only; daily-guidance requires the activation flag and
the exact existing scheduled event. apply-saved requires an explicit main input.
Optional: --recovery-dir .cloud-collection-recovery (not a Pages artifact).
Collect needs contents:write and GH_TOKEN supplied from the existing GITHUB_TOKEN;
restore needs contents:read. Checkout latest main with persist-credentials:false,
and serialize ALL production runs in the same Pages concurrency group with
cancel-in-progress:false. Only code from that checkout is ever executed.

Exit 0 means the canonical handoff is durable (including collector codes 2/3).
Exit 1 stops deployment. JSON stdout/GITHUB_OUTPUT expose collectionStatus,
collectionCode, stateCommit, sourceCodeSHA, stateSource, persistenceStatus.
officialCollectionStatus/Code and personalCollectionStatus/Code describe each
component separately. Paused/budget/unavailable components still publish saved
facts; storage/process/completion-attestation failures retain the remote lease.
Never automatically expire or clear a lease, including on a rerun of the same job.
The permanent state-owner.json marker is mandatory on every existing state
branch. Missing/mismatched markers are never adopted automatically. Only the
fixed collector-state ref is allowed, and it must not be the remote default.
personal-shifts.json and ai-usage.json are optional on legacy branches. Restore
uses a checked personal seed when absent, but never invents a shared usage ledger.
Activation requires an approved, explicitly imported usage ledger.
Recovery: inspect the failed run's recovery JSON files and shared cooldowns, commit
them to collector-state without force and remove lease.json in that same commit,
preserving state-owner.json, then run again. Reports and the private scratch
repository are never artifacts.
"""
import argparse
import copy
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'agurakakenai/akibazettai-schedule-calendar'
REMOTE = 'https://github.com/' + REPOSITORY + '.git'
BRANCH = 'collector-state'
REF = 'refs/heads/' + BRANCH
MAIN_REF = 'refs/heads/main'
SNAPSHOT = 'observed-shifts.json'
HTTP_STATE = 'observed-shifts.http-state.json'
PERSONAL = 'personal-shifts.json'
AI_USAGE = 'ai-usage.json'
HALF_MONTH = 'half-month-schedules.json'
SOURCE_USAGE = 'source-usage.json'
ANALYSIS_BUFFER = 'official-analysis-buffer.json'
LEASE = 'lease.json'
OWNER_FILE = 'state-owner.json'
FILES = {SNAPSHOT, HTTP_STATE, PERSONAL, AI_USAGE, HALF_MONTH, SOURCE_USAGE, OWNER_FILE, LEASE}
DAILY_SCHEDULE = '30 3-6,8-11 * * *'
JST = dt.timezone(dt.timedelta(hours=9))
MANAGER = 'cloud-collection/v1'
STATE_OWNER = {
    'schemaVersion': 1, 'owner': 'agurakakenai', 'managedBy': MANAGER,
    'repository': REPOSITORY, 'branch': 'collector-state',
}
MAX_JSON_BYTES = 16 * 1024 * 1024
SHA_RE = re.compile(r'[0-9a-f]{40}\Z')
RECOVERY = (
    '失敗runの公式JSON・共有HTTPstate・本人JSON・共有AI台帳（導入済みの場合）が揃っていることを'
    '検査し、cooldownと本人pause/budget・AI予約を確認してください。'
    '恒久markerを維持し、collector-stateへ非force commitでstate JSON一式を戻して'
    '同じcommitでlease.jsonを除去後、'
    '次runを実行してください。leaseは自動失効しません。')


class CloudError(Exception):
    pass


def require(condition, reason='unsafe_state'):
    if not condition:
        raise CloudError(reason)


def load_collector():
    spec = importlib.util.spec_from_file_location(
        'cloud_observation_collector', ROOT / 'tools' / 'collect-shifts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_personal_collector():
    spec = importlib.util.spec_from_file_location(
        'cloud_personal_collector', ROOT / 'tools' / 'collect-personal-shifts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_analysis_state():
    spec = importlib.util.spec_from_file_location(
        'cloud_analysis_state', ROOT / 'tools' / 'analysis-state.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_personal_saved():
    spec = importlib.util.spec_from_file_location(
        'cloud_personal_saved', ROOT / 'tools' / 'personal-saved.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_half_month_state():
    spec = importlib.util.spec_from_file_location(
        'cloud_half_month_state', ROOT / 'tools' / 'half-month-schedules.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_half_month_saved():
    spec = importlib.util.spec_from_file_location(
        'cloud_half_month_saved', ROOT / 'tools' / 'half-month-saved.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_source_state():
    spec = importlib.util.spec_from_file_location(
        'cloud_source_state', ROOT / 'tools' / 'source-state.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_environment(environment, *, credentials=False, azure=False):
    result = {
        key: value for key, value in environment.items()
        if not key.upper().startswith(('GIT_', 'GCM_', 'GH_DEBUG'))
        and key.upper() not in ('GH_HOST', 'GH_FORCE_TTY', 'GITHUB_TOKEN')
        and not key.upper().startswith('AZURE_OPENAI_')
        and key.upper() not in ('PERSONAL_ANALYSIS_BACKEND', 'APPLY_SAVED_MANIFEST')
        and not key.upper().startswith('CLOUD_COLLECTION_')
    }
    if not credentials:
        result.pop('GH_TOKEN', None)
        result.pop('GITHUB_OUTPUT', None)
    if azure:
        for key in ('AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_DEPLOYMENT'):
            if key in environment:
                result[key] = environment[key]
    result.update(
        GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never', GH_PROMPT_DISABLED='1',
        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GH_HOST='github.com')
    return result


def child_process(argv, *, cwd, environment, timeout=180):
    return subprocess.run(
        argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def no_duplicate_keys(pairs):
    value = {}
    for key, child in pairs:
        require(key not in value)
        value[key] = child
    return value


def read_json(path):
    require(path.is_file() and not path.is_symlink(), 'missing_or_unsafe_state')
    require(path.stat().st_size <= MAX_JSON_BYTES, 'state_too_large')
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=no_duplicate_keys,
                           parse_constant=lambda _: require(False))
    except (UnicodeError, ValueError, RecursionError):
        raise CloudError('invalid_json') from None
    scan_private(value)
    return value, raw


def scan_private(value):
    if isinstance(value, dict):
        for key, child in value.items():
            scan_private(key)
            scan_private(child)
    elif isinstance(value, list):
        for child in value:
            scan_private(child)
    elif isinstance(value, str):
        require(not re.search(
            r'[\x00-\x1f\x7f]|\b[A-Za-z]:[\\/]|\\\\|/(?:home|Users|tmp|proc)/'
            r'|S-\d-\d+(?:-\d+){2,}|-----BEGIN\b'
            r'|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+', value, re.I),
            'private_state_rejected')


def keys(value, allowed, required=()):
    require(isinstance(value, dict) and set(value) <= set(allowed)
            and set(required) <= set(value))


def integer(value, minimum=0, maximum=10**9):
    require(type(value) is int and minimum <= value <= maximum)


def validate_limits(value, collector):
    keys(value, ('search.yahoo.co.jp', collector.POST_HOST, 'pbs.twimg.com'))
    for until in value.values():
        collector.timestamp(until)


def validate_failure(value, collector):
    if 'reason' in value:
        require(isinstance(value['reason'], str)
                and re.fullmatch(r'[a-z_]{1,64}', value['reason']))
    if 'httpStatus' in value:
        integer(value['httpStatus'], 100, 599)
    if 'retryAt' in value:
        collector.timestamp(value['retryAt'])


def validate_snapshot(path, collector):
    state, raw = read_json(path)
    keys(state, ('schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt',
                 'posts', 'pending', 'resolved', 'cooldowns', 'lastRun', 'officialAnalysis'),
         ('schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt',
          'posts', 'pending', 'lastRun'))
    # Reuse the collector's author/id/url/date checks and resolved-first merge.
    collector.load_snapshot(path)
    for post in state['posts']:
        fields = ('id', 'url', 'authorId', 'authorScreenName', 'createdAt',
                  'date', 'shift', 'storeId', 'names', 'observedAt')
        keys(post, (*fields, 'notices'), fields)
        if 'notices' in post:
            collector.analysis_module().validate_notices(
                post['notices'], collector.analysis_context(), collector.timestamp(post['createdAt']))
        require(all(re.fullmatch(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', name)
                    for name in post['names']))
        require(len(post['names']) == len(set(post['names'])))
        require(abs((collector.snowflake_time(post['id'])
                     - collector.timestamp(post['createdAt'])).total_seconds()) < 2)
    for pending in state['pending']:
        keys(pending, ('id', 'url', 'reason', 'firstSeenAt', 'lastAttemptAt',
                       'attempts', 'httpStatus', 'retryAt'),
             ('id', 'url', 'reason', 'firstSeenAt', 'lastAttemptAt', 'attempts'))
        integer(pending['attempts'])
        validate_failure(pending, collector)
    for resolved in state.get('resolved', []):
        keys(resolved, ('id', 'url', 'reason', 'resolvedAt'),
             ('id', 'url', 'reason', 'resolvedAt'))
    validate_limits(state.get('cooldowns', {}), collector)
    run = state['lastRun']
    counts = (
        'sourceCount', 'sourcePageLimit', 'discoveredCount', 'eligibleCount',
        'attemptedCount', 'fetchedCount', 'newPostCount', 'newNameCount',
        'skippedCuratedCount', 'skippedObservedCount', 'skippedResolvedCount',
        'deferredCount', 'pendingCount', 'pendingOutsideRangeCount', 'maxPosts')
    keys(run, (*counts, 'status', 'dateFrom', 'dateTo', 'dateBasis', 'finishedAt', 'requests',
               'sources', 'failures', 'rejected', 'complete', 'lastSuccessMeaning'),
         ('status', 'dateFrom', 'dateTo'))
    for field in counts:
        if field in run:
            integer(run[field])
    for field in ('dateFrom', 'dateTo'):
        if run[field] is not None:
            require(dt.date.fromisoformat(run[field]).isoformat() == run[field])
    if 'finishedAt' in run:
        collector.timestamp(run['finishedAt'])
    if 'requests' in run:
        keys(run['requests'], ('searches', 'posts'), ('searches', 'posts'))
        integer(run['requests']['searches'], 0, 2)
        integer(run['requests']['posts'], 0, 20)
    if 'complete' in run:
        require(run['complete'] is False)
    for field, expected in (
            ('dateBasis', 'JST service day, 05:00 boundary'),
            ('lastSuccessMeaning',
             'Both searches and every selected post handled without failure or deferral')):
        if field in run:
            require(run[field] == expected)
    for field in ('sources', 'failures', 'rejected'):
        require(isinstance(run.get(field, []), list))
    for source in run.get('sources', []):
        keys(source, ('url', 'status', 'candidateCount', 'reason', 'httpStatus', 'retryAt'),
             ('url', 'status'))
        require(source['url'] in collector.SEARCH_URLS and source['status'] in ('ok', 'failed'))
        if 'candidateCount' in source:
            integer(source['candidateCount'])
        validate_failure(source, collector)
    for field in ('failures', 'rejected'):
        for failure in run.get(field, []):
            allowed = ('id', 'reason') if field == 'rejected' else (
                'id', 'url', 'reason', 'httpStatus', 'retryAt')
            keys(failure, allowed, ('id', 'reason'))
            require(isinstance(failure['id'], str) and collector.post_id(failure['id']))
            if 'url' in failure:
                require(failure['url'] == collector.canonical(failure['id']))
            validate_failure(failure, collector)
    return state, raw


def validate_transport(path, collector):
    state, raw = read_json(path)
    keys(state, ('schemaVersion', 'cooldowns'), ('schemaVersion', 'cooldowns'))
    require(type(state['schemaVersion']) is int and state['schemaVersion'] == 1)
    validate_limits(state['cooldowns'], collector)
    return state, raw


def validate_ai_usage(path):
    state, raw = read_json(path)
    load_analysis_state().validate_state(state)
    return state, raw


def validate_half_month(path):
    state, raw = read_json(path)
    scan_private(state)
    load_half_month_state().validate_state(state, private=True)
    return state, raw


def validate_source_usage(path):
    state, raw = read_json(path)
    scan_private(state)
    load_source_state().validate_state(state)
    return state, raw


def validate_half_month_links(half_month, source_usage, usage, personal):
    if half_month is not None:
        require(source_usage is not None and usage is not None, 'missing_half_month_accounting')
        load_half_month_saved().validate_accounting(half_month, usage, load_half_month_state())
    if source_usage is not None:
        require(personal is not None, 'missing_source_personal_baseline')
        load_source_state().validate_legacy(source_usage, personal, usage)


def validate_usage_links(usage, personal, personal_collector=None):
    if personal and personal.get('savedPersonalImports'):
        load_personal_saved().validate_accounting(
            personal['savedPersonalImports'], usage, personal_collector or load_personal_collector())
    for record in (usage or {}).get('sourceImports', {}).values():
        require(personal is not None, 'source_usage_budget_mismatch')
        budget = personal['budgets'].get(record['receipt']['date'], {'searches': 0, 'posts': 0})
        require(all(budget[kind] >= record['budgetAfter'][kind] for kind in ('searches', 'posts')),
                'source_usage_budget_mismatch')


def validate_legacy_budget(usage, personal):
    ledger = load_analysis_state()
    for day, count in personal.get('azureAnalysis', {}).get('budgets', {}).items():
        when = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(12), tzinfo=JST)
        require(ledger.usage_counts(usage, 'saved-import', when)['day'] >= count,
                'incomplete_legacy_usage_import')


def validate_reconciled_usage(usage, personal, collector):
    if personal is None:
        return
    validate_legacy_budget(usage, personal)
    legacy = personal.get('azureAnalysis', {})
    require(not legacy.get('paused') or usage['paused'] is not None, 'unreconciled_ai_usage')
    now = collector.utc_now()
    for field in ('nextRequestAt', 'retryAt'):
        until = legacy.get(field)
        if until is not None and collector.timestamp(until) > now:
            require(usage['retryAt'] is not None
                    and collector.timestamp(usage['retryAt']) >= collector.timestamp(until),
                    'unreconciled_ai_usage')


def validate_personal(path, personal=None, *, private=True):
    state, raw = read_json(path)
    personal = personal or load_personal_collector()
    personal.read_state(path, private=private)
    public_fields = ('schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt', 'posts', 'lastRun')
    private_fields = ('pending', 'resolved', 'budgets', 'paused', 'identityBindings',
                      'originalTargets', 'lastRequests')
    fields = (*public_fields, *private_fields) if private else public_fields
    keys(state, (*fields, 'azureAnalysis', 'coverage', 'searchHistory', 'savedPersonalImports')
         if private else fields, fields)
    official = personal.official

    def name(value):
        require(isinstance(value, str)
                and re.fullmatch(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', value))

    def identity(value):
        name(value['name'])
        require(isinstance(value['authorId'], str) and official.post_id(value['authorId'])
                and value['authorId'] != official.AUTHOR_ID)
        require(isinstance(value['authorScreenName'], str)
                and re.fullmatch(r'[A-Za-z0-9_]{1,15}', value['authorScreenName']))
        require(value['url'] == personal.public_url(value['authorScreenName'], value['id']))

    for post in state['posts']:
        fields = ('id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt',
                  'observedAt', 'date', 'events')
        keys(post, (*fields, 'links'), fields)
        identity(post)
        for event in post['events']:
            require(event['kind'] != 'absence' or 'storeId' not in event,
                    'invalid_personal_absence')
        require(abs((official.snowflake_time(post['id'])
                     - official.timestamp(post['createdAt'])).total_seconds()) < 2)
    run = state['lastRun']
    counts = ('sourceCount', 'targetCount', 'activeTargetCount', 'attemptedCount',
              'newPostCount', 'newEventCount', 'skippedResolvedCount', 'pendingCount', 'deferredCount')
    keys(run, (*counts, 'status', 'date', 'dateBasis', 'source', 'sources', 'requests',
               'failures', 'finishedAt', 'complete'), ('status',))
    for field in counts:
        if field in run:
            integer(run[field])
    if 'date' in run:
        require(dt.date.fromisoformat(run['date']).isoformat() == run['date'])
    for field, expected in (
            ('dateBasis', 'JST calendar date, 00:00 boundary'),
            ('source', 'manually_reviewed_public_http_pilot'), ('complete', False)):
        if field in run:
            require(run[field] is False if expected is False else run[field] == expected)
    if 'finishedAt' in run:
        official.timestamp(run['finishedAt'])
    if 'requests' in run:
        keys(run['requests'], ('searches', 'posts'), ('searches', 'posts'))
        for count in run['requests'].values():
            integer(count)
    for field in ('sources', 'failures'):
        require(isinstance(run.get(field, []), list))
    for source in run.get('sources', []):
        keys(source, ('url', 'status', 'candidateCount', 'reason', 'httpStatus', 'retryAt'),
             ('url', 'status'))
        require(source['status'] in ('ok', 'failed')
                and personal.valid_search_url(source['url']))
        if 'candidateCount' in source:
            integer(source['candidateCount'])
        validate_failure(source, official)
    for failure in run.get('failures', []):
        keys(failure, ('id', 'reason', 'httpStatus', 'retryAt'), ('reason',))
        if 'id' in failure:
            require(isinstance(failure['id'], str) and official.post_id(failure['id']))
        validate_failure(failure, official)
    if private:
        if 'savedPersonalImports' in state:
            load_personal_saved().validate_imports(state['savedPersonalImports'], personal)
        for item in state['pending']:
            fields = ('id', 'url', 'name', 'authorId', 'authorScreenName', 'date',
                      'searchCreatedAt', 'reason', 'firstSeenAt', 'lastAttemptAt', 'attempts')
            keys(item, (*fields, 'httpStatus', 'retryAt', 'metadataSource', 'sourceCreatedAt'), fields)
            identity(item)
            metadata_source = item.get('metadataSource', 'search')
            require(metadata_source in ('search', 'saved_post', 'saved_binding'))
            if item['searchCreatedAt'] is None:
                require(metadata_source in ('saved_post', 'saved_binding'))
                binding = state['identityBindings'].get(item['name'])
                require(binding is not None and all(binding[field] == item[field]
                        for field in ('authorId', 'authorScreenName')))
            else:
                searched = official.timestamp(item['searchCreatedAt'])
                require(personal.calendar_day(searched).isoformat() == item['date'])
                require(abs((official.snowflake_time(item['id']) - searched).total_seconds()) < 2)
            if 'sourceCreatedAt' in item:
                require(metadata_source == 'saved_post')
                created = official.timestamp(item['sourceCreatedAt'])
                require(personal.calendar_day(created).isoformat() == item['date'])
                require(abs((official.snowflake_time(item['id']) - created).total_seconds()) < 2)
            official.timestamp(item['firstSeenAt'])
            if item['lastAttemptAt'] is not None:
                official.timestamp(item['lastAttemptAt'])
            integer(item['attempts'])
            validate_failure(item, official)
        for item in state['resolved']:
            fields = ('id', 'url', 'name', 'date', 'reason', 'resolvedAt')
            keys(item, fields, fields)
            name(item['name'])
            require(re.fullmatch(r'https://x\.com/[A-Za-z0-9_]{1,15}/status/' + item['id'],
                                 item['url']))
            require(dt.date.fromisoformat(item['date']).isoformat() == item['date'])
            require(personal.calendar_day(official.snowflake_time(item['id'])).isoformat() == item['date'])
            official.timestamp(item['resolvedAt'])
        for person, binding in state['identityBindings'].items():
            name(person)
            keys(binding, ('authorId', 'authorScreenName', 'verifiedAt'),
                 ('authorId', 'authorScreenName', 'verifiedAt'))
            require(binding['authorId'] != official.AUTHOR_ID)
            official.timestamp(binding['verifiedAt'])
        for day, targets in state['originalTargets'].items():
            require(dt.date.fromisoformat(day).isoformat() == day and isinstance(targets, dict))
            for person, target in targets.items():
                name(person)
                keys(target, ('name', 'handle', 'shifts'), ('name', 'handle', 'shifts'))
                require(target['name'] == person and re.fullmatch(r'[A-Za-z0-9_]{1,15}', target['handle'])
                        and isinstance(target['shifts'], list) and target['shifts']
                        and set(target['shifts']) <= {'昼', '夜'}
                        and len(set(target['shifts'])) == len(target['shifts']))
        if state['paused'] is not None:
            keys(state['paused'], ('reason', 'host', 'at', 'retryAt', 'httpStatus'),
                 ('reason', 'host', 'at', 'retryAt'))
            validate_failure(state['paused'], official)
    return state, raw


def validate_state_target():
    require(REPOSITORY == 'agurakakenai/akibazettai-schedule-calendar'
            and BRANCH == 'collector-state' and REF == 'refs/heads/collector-state'
            and MAIN_REF == 'refs/heads/main', 'unsafe_state_target')


def validate_owner(path):
    marker, _ = read_json(path)
    require(isinstance(marker, dict) and marker == STATE_OWNER
            and type(marker.get('schemaVersion')) is int, 'unowned_state_branch')


def validate_lease(path, collector):
    lease, _ = read_json(path)
    fields = ('schemaVersion', 'managedBy', 'repository', 'leaseId', 'runId', 'runAttempt',
              'createdAt', 'sourceCodeSHA')
    keys(lease, fields, fields)
    require(type(lease['schemaVersion']) is int and lease['schemaVersion'] == 1
            and lease['managedBy'] == MANAGER and lease['repository'] == REPOSITORY,
            'unowned_lease')
    for field in ('runId', 'runAttempt'):
        require(isinstance(lease[field], str)
                and re.fullmatch(r'[1-9][0-9]{0,19}', lease[field]), 'unowned_lease')
    require(isinstance(lease['sourceCodeSHA'], str)
            and SHA_RE.fullmatch(lease['sourceCodeSHA']), 'unowned_lease')
    require(isinstance(lease['leaseId'], str)
            and re.fullmatch(r'[0-9a-f]{32}', lease['leaseId']), 'unowned_lease')
    collector.timestamp(lease['createdAt'])


def trusted_context(environment):
    require(environment.get('GITHUB_ACTIONS') == 'true'
            and environment.get('GITHUB_REPOSITORY') == REPOSITORY
            and environment.get('GITHUB_SERVER_URL') == 'https://github.com'
            and environment.get('GITHUB_REF') == MAIN_REF
            and environment.get('GITHUB_EVENT_NAME') in ('push', 'schedule', 'workflow_dispatch')
            and not environment.get('GITHUB_HEAD_REF')
            and not environment.get('GITHUB_BASE_REF'), 'untrusted_context')
    require(re.fullmatch(re.escape(REPOSITORY)
                         + r'/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml@refs/heads/main',
                         environment.get('GITHUB_WORKFLOW_REF', '')), 'untrusted_workflow')
    require(bool(environment.get('GH_TOKEN')), 'missing_gh_token')
    for name in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        require(re.fullmatch(r'[1-9][0-9]{0,19}', environment.get(name, '')),
                'invalid_run_identity')
    try:
        event = json.loads(Path(environment['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        repository = event['repository']
        require(repository['full_name'] == REPOSITORY and repository['fork'] is False
                and not event.get('pull_request'), 'untrusted_event')
        if environment['GITHUB_EVENT_NAME'] == 'push':
            require(event.get('ref') == MAIN_REF and event.get('deleted') is False,
                    'untrusted_event')
    except (KeyError, TypeError, ValueError, OSError):
        raise CloudError('untrusted_event') from None


def require_manual_personal(mode, environment):
    if mode == 'daily-guidance':
        require(environment.get('DAILY_GUIDANCE_ENABLED', 'false') == 'true'
                and environment.get('GITHUB_EVENT_NAME') == 'schedule',
                'daily_guidance_not_enabled')
        try:
            event = json.loads(Path(environment['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
            require(event.get('schedule') == DAILY_SCHEDULE, 'unknown_collection_schedule')
        except (KeyError, TypeError, ValueError, OSError):
            raise CloudError('untrusted_event') from None
        return
    if mode not in ('personal', 'both', 'apply-saved'):
        return
    require(environment.get('GITHUB_EVENT_NAME') == 'workflow_dispatch',
            'personal_requires_manual_run')
    try:
        event = json.loads(Path(environment['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        require(event.get('inputs', {}).get('mode') == mode, 'personal_requires_explicit_input')
    except (KeyError, TypeError, ValueError, OSError):
        raise CloudError('untrusted_event') from None


def atomic_bytes(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with scratch.open('xb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)


def remove_tree(path):
    def writable_retry(function, target, error):
        if not isinstance(error[1], PermissionError):
            raise error[1]
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        function(target)
    shutil.rmtree(path, onerror=writable_retry)


class StateRepository:
    """A disposable repository with no remote/config credentials or root index."""

    def __init__(self, path, environment):
        self.path = path
        self.environment = safe_environment(environment, credentials=True)
        self.head = ''

    def git(self, *args, reason, cwd=None, allowed=(0,)):
        argv = [
            'git', '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
            '-c', 'credential.interactive=false', '-c', 'core.hooksPath=' + str(self.path / 'no-hooks'),
            '-c', 'init.templateDir=', '-c', 'core.autocrlf=false', '-c', 'core.longpaths=true',
            '-c', 'core.attributesFile=' + os.devnull, '-c', 'commit.gpgSign=false',
            '-c', 'user.name=github-actions[bot]',
            '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            '-c', 'http.followRedirects=false', *args]
        try:
            result = child_process(argv, cwd=cwd or self.path, environment=self.environment)
        except (OSError, subprocess.SubprocessError):
            raise CloudError(reason) from None
        require(result.returncode in allowed, reason)
        return result.stdout

    def initialize(self, root, *, writing):
        validate_state_target()
        self.git('init', '--quiet', reason='state_init_failed')
        source = self.git('rev-parse', 'HEAD', cwd=root, reason='source_revision_failed').decode().strip()
        require(SHA_RE.fullmatch(source), 'source_revision_failed')
        refs = self.git('ls-remote', '--symref', REMOTE, 'HEAD', MAIN_REF, REF,
                        reason='state_lookup_failed')
        advertised = {}
        default_ref = None
        for line in refs.decode('ascii').splitlines():
            oid, name = line.split('\t')
            if oid.startswith('ref: '):
                require(name == 'HEAD' and default_ref is None
                        and oid.startswith('ref: refs/heads/'), 'invalid_remote_refs')
                default_ref = oid.removeprefix('ref: ')
                continue
            require(SHA_RE.fullmatch(oid) and name in ('HEAD', MAIN_REF, REF)
                    and name not in advertised, 'invalid_remote_refs')
            advertised[name] = oid
        require(default_ref is not None and 'HEAD' in advertised, 'remote_default_unknown')
        require(default_ref != REF, 'state_is_default_branch')
        if REF in advertised:
            require(advertised[REF] not in (advertised['HEAD'], advertised.get(MAIN_REF)),
                    'state_aliases_code_branch')
        if writing:
            require(advertised.get(MAIN_REF) == source, 'checkout_not_latest_main')
        if REF in advertised:
            self.git('fetch', '--quiet', '--depth=1', '--no-tags', '--no-recurse-submodules',
                     REMOTE, REF, reason='state_fetch_failed')
            self.head = self.git('rev-parse', 'FETCH_HEAD', reason='state_revision_failed').decode().strip()
            entries = self.git('ls-tree', '-r', '-z', 'FETCH_HEAD', reason='state_tree_failed')
            found = set()
            for entry in entries.split(b'\0'):
                if not entry:
                    continue
                metadata, name = entry.split(b'\t')
                mode, kind, _ = metadata.split(b' ')
                require(mode == b'100644' and kind == b'blob' and name.decode('utf-8') in FILES,
                        'unexpected_state_files')
                found.add(name.decode('utf-8'))
            require({SNAPSHOT, HTTP_STATE} <= found, 'incomplete_state_branch')
            require(OWNER_FILE in found, 'missing_state_owner')
            # Only a verified flat data tree may be checked out. No branch code,
            # attributes, executable files, symlinks, hooks, or submodules.
            self.git('checkout', '--quiet', '-b', BRANCH, 'FETCH_HEAD',
                     reason='state_checkout_failed')
            validate_owner(self.path / OWNER_FILE)
        else:
            self.git('checkout', '--quiet', '--orphan', BRANCH, reason='state_orphan_failed')
            atomic_bytes(self.path / OWNER_FILE,
                         (json.dumps(STATE_OWNER, sort_keys=True, indent=2) + '\n').encode('utf-8'))
        return source

    def persist(self, collector, *, leased, personal=None):
        validate_state_target()
        validate_owner(self.path / OWNER_FILE)
        validate_snapshot(self.path / SNAPSHOT, collector)
        validate_transport(self.path / HTTP_STATE, collector)
        has_personal = (self.path / PERSONAL).exists()
        personal_state = None
        if has_personal:
            personal_state, _ = validate_personal(self.path / PERSONAL, personal)
        has_ai = (self.path / AI_USAGE).exists()
        usage = None
        if has_ai:
            usage, _ = validate_ai_usage(self.path / AI_USAGE)
        validate_usage_links(usage, personal_state, personal)
        extra_states = {}
        for name, validate in ((HALF_MONTH, validate_half_month), (SOURCE_USAGE, validate_source_usage)):
            if (self.path / name).exists():
                extra_states[name] = validate(self.path / name)[0]
        validate_half_month_links(extra_states.get(HALF_MONTH), extra_states.get(SOURCE_USAGE),
                                  usage, personal_state)
        if leased:
            validate_lease(self.path / LEASE, collector)
        else:
            require(not (self.path / LEASE).exists(), 'lease_not_removed')
        require({item.name for item in self.path.iterdir()} <= FILES | {'.git'},
                'unexpected_state_files')
        # The isolated index has never seen ROOT; explicit paths are still used.
        self.git('add', '--', SNAPSHOT, HTTP_STATE, OWNER_FILE, reason='state_stage_failed')
        if has_personal:
            self.git('add', '--', PERSONAL, reason='state_stage_failed')
        if has_ai:
            self.git('add', '--', AI_USAGE, reason='state_stage_failed')
        for name in (HALF_MONTH, SOURCE_USAGE):
            if (self.path / name).exists():
                self.git('add', '--', name, reason='state_stage_failed')
        if leased:
            self.git('add', '--', LEASE, reason='state_stage_failed')
        else:
            self.git('rm', '--quiet', '--cached', '--ignore-unmatch', '--', LEASE,
                     reason='state_stage_failed')
        message = ('Record collection lease' if leased else 'Save collection state') + (
            '\n\nCo-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>')
        self.git('commit', '--quiet', '-m', message, reason='state_commit_failed')
        # A concurrent lease or state update rejects this ordinary fast-forward
        # push. Never fetch/rebase/retry over it, force, or use force-with-lease.
        self.git('push', '--quiet', REMOTE, 'HEAD:' + REF,
                 reason='lease_push_failed' if leased else 'state_push_failed')
        self.head = self.git('rev-parse', 'HEAD', reason='state_revision_failed').decode().strip()
        require(SHA_RE.fullmatch(self.head), 'state_revision_failed')
        return self.head


def checked_paths(root, output, recovery):
    root = root.resolve()
    output = output if output.is_absolute() else root / output
    require(output.resolve() == root / 'data' / SNAPSHOT and not output.is_symlink()
            and not output.parent.is_symlink(), 'unsafe_output_path')
    recovery = recovery if recovery.is_absolute() else root / recovery
    relative = recovery.resolve().relative_to(root)
    require(relative.parts and relative.parts[0] not in (
        '.git', '.github', 'tools', 'data', 'assets'), 'unsafe_recovery_path')
    for path in (recovery, *recovery.parents):
        if path == root:
            break
        require(not path.is_symlink(), 'unsafe_recovery_path')
    if recovery.exists():
        require(recovery.is_dir() and {p.name for p in recovery.iterdir()} <= {
            SNAPSHOT, HTTP_STATE, PERSONAL, AI_USAGE, HALF_MONTH, SOURCE_USAGE},
                'unsafe_recovery_path')
    return output, recovery


def copy_pair(source, destination, collector, *, include_personal=False, personal=None,
              include_ai=False, include_half_month=False, include_source=False):
    _, canonical = validate_snapshot(source / SNAPSHOT, collector)
    _, transport = validate_transport(source / HTTP_STATE, collector)
    personal_state, extra = validate_personal(source / PERSONAL, personal) if include_personal else (None, None)
    usage, ai = validate_ai_usage(source / AI_USAGE) if include_ai else (None, None)
    validate_usage_links(usage, personal_state, personal)
    extra_states = {}
    half_month_state = source_state = None
    if include_half_month:
        half_month_state, extra_states[HALF_MONTH] = validate_half_month(source / HALF_MONTH)
    if include_source:
        source_state, extra_states[SOURCE_USAGE] = validate_source_usage(source / SOURCE_USAGE)
    validate_half_month_links(half_month_state, source_state, usage, personal_state)
    atomic_bytes(destination / SNAPSHOT, canonical)
    atomic_bytes(destination / HTTP_STATE, transport)
    if extra is not None:
        atomic_bytes(destination / PERSONAL, extra)
    if ai is not None:
        atomic_bytes(destination / AI_USAGE, ai)
    else:
        (destination / AI_USAGE).unlink(missing_ok=True)
    for name, raw in extra_states.items():
        atomic_bytes(destination / name, raw)


def save_recovery(source, destination, collector, *, include_personal=False, personal=None,
                  include_ai=False, include_half_month=False, include_source=False):
    invalid = False
    validators = [(SNAPSHOT, validate_snapshot), (HTTP_STATE, validate_transport)]
    if include_personal:
        validators.append((PERSONAL, lambda path, _: validate_personal(path, personal)))
    if include_ai:
        def checked_usage(path, _):
            usage, raw = validate_ai_usage(path)
            personal_state = validate_personal(source / PERSONAL, personal)[0] if include_personal else None
            validate_usage_links(usage, personal_state)
            return usage, raw
        validators.append((AI_USAGE, checked_usage))
    if include_half_month:
        validators.append((HALF_MONTH, lambda path, _: validate_half_month(path)))
    if include_source:
        validators.append((SOURCE_USAGE, lambda path, _: validate_source_usage(path)))
    for name, validate in validators:
        try:
            _, raw = validate(source / name, collector)
        except (CloudError, ValueError, TypeError, OSError):
            # Never label the pre-HTTP seed as the failed run's saved result.
            (destination / name).unlink(missing_ok=True)
            invalid = True
        else:
            atomic_bytes(destination / name, raw)
    require(not invalid, 'recovery_state_invalid')


def invoke_collector(root, state, report, environment):
    azure = environment.get('CLOUD_COLLECTION_OFFICIAL_AZURE') == 'true'
    argv = [sys.executable, '-I', '-B', str(root / 'tools' / 'collect-shifts.py'),
            '--once', '--days', '2', '--max-posts', '20',
            '--snapshot', str(state / SNAPSHOT), '--report', str(report)]
    argv.extend(source_arguments(state, environment))
    if azure:
        argv.extend(['--analysis-backend', 'azure',
                     *analysis_arguments(state, environment, allow_zero=True)])
        buffer_mode = environment.get('CLOUD_COLLECTION_BUFFER_MODE', '')
        require(buffer_mode in ('', 'write', 'replay'), 'invalid_analysis_buffer_mode')
        if buffer_mode:
            path = analysis_buffer_path(root, state)
            argv.extend(['--analysis-buffer' if buffer_mode == 'write' else '--replay-buffer',
                         str(path)])
    try:
        process = child_process(
            argv, cwd=root, environment=safe_environment(environment, azure=azure), timeout=1200)
    except (OSError, subprocess.SubprocessError):
        raise CloudError('collector_process_failed') from None
    # The report/stdout/stderr may contain local paths and process IDs.
    return process.returncode


def invoke_personal_collector(root, state, report, environment):
    backend = environment.get('PERSONAL_ANALYSIS_BACKEND', 'rules')
    require(backend in ('rules', 'azure'), 'invalid_analysis_backend')
    max_posts = environment.get('CLOUD_COLLECTION_PERSONAL_POSTS', '3')
    require(max_posts in ('0', '1', '2', '3'), 'invalid_source_limit')
    max_searches = environment.get('CLOUD_COLLECTION_PERSONAL_SEARCHES', '3')
    require(max_searches in ('0', '1', '2', '3'), 'invalid_source_limit')
    argv = [sys.executable, '-I', '-B', str(root / 'tools' / 'collect-personal-shifts.py'),
            '--once', '--snapshot', str(state / PERSONAL),
            '--observations', str(state / SNAPSHOT),
            '--http-state', str(state / HTTP_STATE),
            '--seed', str(state.parent / 'personal-seed.json'),
            '--analysis-backend', backend,
            '--max-searches', max_searches, '--max-posts', max_posts, '--report', str(report)]
    argv.extend(source_arguments(state, environment))
    if environment.get('CLOUD_COLLECTION_HALF_MONTH_PRESENT') == 'true':
        argv.extend(['--half-month-snapshot', str(state / HALF_MONTH)])
    if environment.get('CLOUD_COLLECTION_SCHEDULED') == 'true':
        argv.append('--scheduled')
    if backend == 'azure':
        require(environment.get('CLOUD_COLLECTION_SHARED') == 'true', 'missing_ai_usage')
        argv.extend(analysis_arguments(state, environment))
    try:
        process = child_process(
            argv,
            cwd=root, environment=safe_environment(environment, azure=backend == 'azure'), timeout=600)
    except (OSError, subprocess.SubprocessError):
        raise CloudError('personal_process_failed') from None
    return process.returncode


def source_arguments(state, environment):
    if environment.get('CLOUD_COLLECTION_SOURCE_ENABLED') != 'true':
        return []
    run_id = environment.get('CLOUD_COLLECTION_RUN_ID', '')
    require(bool(re.fullmatch(r'[1-9][0-9]{0,19}-[1-9][0-9]{0,19}', run_id)),
            'invalid_source_run_id')
    return ['--source-state', str(state / SOURCE_USAGE), '--source-run-id', run_id]


def invoke_half_month_collector(root, state, report, environment):
    require(environment.get('CLOUD_COLLECTION_SOURCE_ENABLED') == 'true'
            and environment.get('CLOUD_COLLECTION_SHARED') == 'true',
            'missing_half_month_accounting')
    argv = [sys.executable, '-I', '-B', str(root / 'tools' / 'collect-half-month-schedules.py'),
            '--once', '--snapshot', str(state / HALF_MONTH),
            '--personal-snapshot', str(state / PERSONAL),
            '--http-state', str(state / HTTP_STATE),
            *source_arguments(state, environment),
            '--ai-state', str(state / AI_USAGE),
            '--analysis-run-id', environment['CLOUD_COLLECTION_RUN_ID'],
            '--analysis-limit', '1', '--max-searches', '1', '--max-posts', '1',
            '--max-images', '4', '--report', str(report)]
    try:
        process = child_process(
            argv, cwd=root, environment=safe_environment(environment, azure=True), timeout=900)
    except (OSError, subprocess.SubprocessError):
        raise CloudError('half_month_process_failed') from None
    return process.returncode


def analysis_arguments(state, environment, *, allow_zero=False):
    run_id = environment.get('CLOUD_COLLECTION_RUN_ID', '')
    limit = environment.get('CLOUD_COLLECTION_ANALYSIS_LIMIT', '3')
    require(bool(re.fullmatch(r'[1-9][0-9]{0,19}-[1-9][0-9]{0,19}', run_id))
            and limit in (('0', '1', '2', '3') if allow_zero else ('1', '2', '3')),
            'invalid_analysis_allocation')
    return ['--ai-state', str(state / AI_USAGE), '--analysis-run-id', run_id,
            '--analysis-limit', limit]


def personal_window_open(now, *, scheduled):
    local = now.astimezone(JST)
    cutoff = dt.time(18) if scheduled else dt.time(19, 30)
    return local.time().replace(tzinfo=None) <= cutoff


def analysis_buffer_path(root, state):
    work = state.parent
    path = work / ANALYSIS_BUFFER
    require(work.parent.resolve() == root.resolve()
            and re.fullmatch(r'\.cc-work-[0-9a-f]{16}', work.name)
            and not work.is_symlink() and not path.is_symlink(),
            'unsafe_analysis_buffer_path')
    return path


def load_official_buffer(root, state, snapshot, run_id, collector):
    path = analysis_buffer_path(root, state)
    require(path.is_file(), 'official_analysis_buffer_missing')
    try:
        value = collector.load_analysis_buffer(path, snapshot, run_id, collector.utc_now())
        require(isinstance(value, dict) and isinstance(value.get('items'), list)
                and len(value['items']) <= 3, 'official_analysis_buffer_invalid')
        return value
    except (ValueError, KeyError, TypeError, OSError):
        raise CloudError('official_analysis_buffer_invalid') from None


def official_allocation(path, run_id, now, personal_active, scheduled):
    state, _ = validate_ai_usage(path)
    remaining = load_analysis_state().remaining(state, run_id, now)
    used = sum(receipt['runId'] == run_id and receipt['component'] == 'official'
               for receipt in state['receipts'].values())
    if remaining == 0:
        return min(3, used)
    if not personal_active or not personal_window_open(now, scheduled=scheduled):
        return min(3, used + remaining)
    previous = {}
    for receipt in state['receipts'].values():
        if receipt['runId'] == run_id or receipt['component'] not in ('official', 'personal'):
            continue
        run = previous.setdefault(receipt['runId'], {'official': 0, 'personal': 0, 'at': ''})
        run[receipt['component']] += 1
        run['at'] = max(run['at'], receipt['reservedAt'])
    extra = sorted((run for run in previous.values()
                    if run['official'] and run['personal'] and run['official'] != run['personal']),
                   key=lambda run: run['at'])
    prefer_personal = bool(extra and extra[-1]['official'] > extra[-1]['personal'])
    local = now.astimezone(JST).replace(tzinfo=None)
    cutoffs = (dt.time(13, 30), dt.time(18) if scheduled else dt.time(19, 30))
    near_deadline = any(dt.timedelta(0) <= dt.datetime.combine(local.date(), cutoff) - local
                        <= dt.timedelta(minutes=3) for cutoff in cutoffs)
    if remaining == 1:
        extra = 0 if prefer_personal or near_deadline else 1
    elif remaining == 2:
        extra = 1
    else:
        extra = 1 if prefer_personal or near_deadline else 2
    return min(3, used + extra)


def personal_deadline_near(now, *, scheduled):
    local = now.astimezone(JST).replace(tzinfo=None)
    cutoffs = (dt.time(13, 30), dt.time(18) if scheduled else dt.time(19, 30))
    return any(dt.timedelta(0) <= dt.datetime.combine(local.date(), cutoff) - local
               <= dt.timedelta(minutes=3) for cutoff in cutoffs)


def half_month_official_allocation(path, run_id, now, *, personal_active, scheduled):
    if personal_active and personal_deadline_near(now, scheduled=scheduled):
        return official_allocation(path, run_id, now, True, scheduled)
    usage, _ = validate_ai_usage(path)
    remaining = load_analysis_state().remaining(usage, run_id, now)
    prior = {'official': '', 'schedule': ''}
    for receipt in usage['receipts'].values():
        if receipt['runId'] != run_id and receipt['component'] in prior:
            component = receipt['component']
            prior[component] = max(prior[component], receipt['reservedAt'])
    if personal_active and personal_window_open(now, scheduled=scheduled):
        remaining = max(0, remaining - 1)
    if remaining >= 2:
        return remaining - 1
    if remaining == 1:
        return int(prior['official'] <= prior['schedule'])
    return 0


def half_month_personal_allocation(path, run_id, now, *, scheduled):
    usage, _ = validate_ai_usage(path)
    remaining = load_analysis_state().remaining(usage, run_id, now)
    if personal_deadline_near(now, scheduled=scheduled):
        return 3
    return max(1, remaining - 1)


def aggregate_official_resume(initial, resumed, resumed_ids, requests, collector):
    """Keep the initial source result while completing only buffered analysis."""
    before = {post['id']: {key: value for key, value in post.items() if key != 'notices'}
              for post in initial['posts']}
    after = {post['id']: {key: value for key, value in post.items() if key != 'notices'}
             for post in resumed['posts']}
    require(before == after, 'official_resume_changed_facts')
    require(resumed.get('resolved', []) == initial.get('resolved', []),
            'official_resume_changed_facts')
    pending = {item['id']: item for item in resumed['pending']}
    for item in initial['pending']:
        if item['id'] not in resumed_ids:
            require(pending.get(item['id']) == item, 'official_resume_changed_pending')
    result = copy.deepcopy(resumed)
    run = copy.deepcopy(initial['lastRun'])
    failures = []
    for failure in [*run.get('failures', []), *resumed['lastRun'].get('failures', [])]:
        if (failure.get('id') in resumed_ids and failure.get('reason', '').startswith('azure_')
                and pending.get(failure['id'], {}).get('reason') != failure['reason']):
            continue
        if failure not in failures:
            failures.append(copy.deepcopy(failure))
    run['failures'] = failures
    run['requests'] = dict(requests)
    run['pendingCount'] = len(result['pending'])
    run['finishedAt'] = resumed['lastRun'].get('finishedAt', collector.iso(collector.utc_now()))
    source_incomplete = (
        any(item['status'] != 'ok' for item in run.get('sources', []))
        or ('sourceCount' in run and run['sourceCount'] != len(collector.SEARCH_URLS)))
    if run['status'] == 'unavailable':
        status = 'unavailable'
    elif source_incomplete or failures or run.get('deferredCount', 0) or result['pending']:
        status = 'partial'
    elif run.get('newPostCount', 0):
        status = 'ok'
    else:
        status = run['status'] if run['status'] in ('ok', 'no-new', 'no-results') else 'no-new'
    run['status'] = status
    result['lastRun'] = run
    result['checkedAt'] = initial['checkedAt']
    result['lastSuccessAt'] = (run['finishedAt'] if status in ('ok', 'no-new', 'no-results')
                              else initial['lastSuccessAt'])
    return result


def data_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def read_saved_manifest(environment):
    text = environment.get('APPLY_SAVED_MANIFEST', '')
    require(isinstance(text, str) and 0 < len(text.encode('utf-8')) <= 32768,
            'invalid_saved_manifest')
    try:
        value = json.loads(text, object_pairs_hook=no_duplicate_keys,
                           parse_constant=lambda _: require(False, 'invalid_saved_manifest'))
        event = json.loads(Path(environment['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        require(event.get('inputs', {}).get('saved_manifest') == text, 'saved_manifest_input_mismatch')
        scan_private(value)
        fields = ('schemaVersion', 'expectedMainSHA', 'expectedStateSHA',
                  'officialAmendments', 'usageImports', 'sourceReceipts')
        keys(value, (*fields, 'personalAmendments', 'halfMonthAmendments', 'sourceMigration'), fields)
        require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1)
        for field in ('expectedMainSHA', 'expectedStateSHA'):
            require(isinstance(value[field], str) and SHA_RE.fullmatch(value[field]))
        for field, maximum in (('officialAmendments', 3), ('usageImports', 10), ('sourceReceipts', 10)):
            require(isinstance(value[field], list) and len(value[field]) <= maximum)
        require(isinstance(value.get('personalAmendments', []), list)
                and len(value.get('personalAmendments', [])) <= 3)
        require(isinstance(value.get('halfMonthAmendments', []), list)
                and len(value.get('halfMonthAmendments', [])) <= 1)
        if 'sourceMigration' in value:
            migration = value['sourceMigration']
            keys(migration, ('expectedPersonalBudgetsHash', 'sourceHash', 'historicalImages'),
                 ('expectedPersonalBudgetsHash', 'sourceHash', 'historicalImages'))
            for field in ('expectedPersonalBudgetsHash', 'sourceHash'):
                require(isinstance(migration[field], str)
                        and re.fullmatch(r'[a-f0-9]{64}', migration[field]))
            require(isinstance(migration['historicalImages'], list)
                    and len(migration['historicalImages']) <= 10)
        require(any(value.get(field) for field in (
            'officialAmendments', 'usageImports', 'sourceReceipts', 'personalAmendments',
            'halfMonthAmendments', 'sourceMigration')))
        for entry in value['officialAmendments']:
            keys(entry, ('expectedPostHash', 'amendment'), ('expectedPostHash', 'amendment'))
            require(isinstance(entry['expectedPostHash'], str)
                    and re.fullmatch(r'[0-9a-f]{64}', entry['expectedPostHash']))
        for receipt in value['usageImports']:
            require(isinstance(receipt, dict) and isinstance(receipt.get('counts'), dict))
            integer(receipt['counts'].get('requests'), 1, 1000)
            require(isinstance(receipt.get('modelBreakdown'), list)
                    and 1 <= len(receipt['modelBreakdown']) <= 8)
        for receipt in value['sourceReceipts']:
            require(isinstance(receipt, dict))
            integer(receipt.get('searches'), 0, 60)
            integer(receipt.get('posts'), 0, 30)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise CloudError('invalid_saved_manifest') from None
    return value


def prepare_saved(manifest, canonical, personal_state, usage, collector, personal):
    """Validate and prepare the complete bounded delta without I/O or clients."""
    ledger = load_analysis_state()
    require(usage is not None or bool(manifest['usageImports']), 'missing_initial_usage_import')
    official_result = copy.deepcopy(canonical)
    personal_result = copy.deepcopy(personal_state)
    usage_result = copy.deepcopy(usage) if usage is not None else ledger.empty_state()
    for receipt in manifest['usageImports']:
        ledger.apply_import(usage_result, receipt)
    legacy = personal_result.get('azureAnalysis', {})
    validate_legacy_budget(usage_result, personal_result)
    now = collector.utc_now()
    for field in ('nextRequestAt', 'retryAt'):
        value = legacy.get(field)
        if value is None:
            continue
        # Legacy nextRequestAt may represent 429 backoff, not ordinary spacing.
        targets = ('nextRequestAt', 'retryAt') if (
            field == 'retryAt' or collector.timestamp(value) > now) else ('nextRequestAt',)
        for target in targets:
            previous = usage_result.get(target)
            if previous is None or collector.timestamp(value) > collector.timestamp(previous):
                usage_result[target] = value
    if legacy.get('paused') is not None and usage_result.get('paused') is None:
        usage_result['paused'] = copy.deepcopy(legacy['paused'])
    for receipt in manifest['sourceReceipts']:
        ledger.apply_source_import(usage_result, receipt, personal_result)
    # Only the already-accounted canonical ledger can authorize saved analyses.
    # Any independently authorized source-budget delta above remains untouched.
    personal_result = load_personal_saved().apply_amendments(
        personal_result, manifest.get('personalAmendments', []), usage, personal)
    ids = set()
    for entry in manifest['officialAmendments']:
        amendment = entry['amendment']
        require(isinstance(amendment, dict) and isinstance(amendment.get('id'), str),
                'invalid_saved_amendment')
        require(isinstance(amendment.get('source'), dict)
                and {'analyzedAt', 'analysisReceiptHash'} <= set(amendment['source']),
                'missing_saved_analysis_receipt')
        tid = amendment['id']
        require(tid not in ids, 'duplicate_saved_amendment')
        ids.add(tid)
        post = next((post for post in official_result['posts'] if post['id'] == tid), None)
        require(post is not None, 'unknown_saved_post')
        receipt_id = data_hash(amendment)
        receipts = official_result.get('officialAnalysis', {}).get('receipts', {})
        require(data_hash(post) == entry['expectedPostHash'] or receipt_id in receipts,
                'saved_post_hash_mismatch')
        official_result = collector.apply_saved_notice(official_result, amendment)
    ledger.validate_state(usage_result)
    # A no-op replay is permitted; none of these updates means "delete facts".
    require(len(official_result['posts']) == len(canonical['posts']), 'saved_facts_changed')
    for before, after in zip(canonical['posts'], official_result['posts']):
        require({key: value for key, value in before.items() if key != 'notices'}
                == {key: value for key, value in after.items() if key != 'notices'}, 'saved_facts_changed')
    return official_result, personal_result, usage_result


def source_baseline_hash(personal, usage, transport):
    return data_hash({'personal': personal, 'sourceImports': usage.get('sourceImports', {}),
                      'cooldowns': transport['cooldowns']})


def prepare_half_month_saved(manifest, half_state, source_state, personal, usage, transport,
                            *, root=ROOT, personal_collector=None, now=None, accounting_usage=None):
    reconciled_usage = usage if accounting_usage is None else accounting_usage
    entries = manifest.get('halfMonthAmendments', [])
    migration = manifest.get('sourceMigration')
    if not entries and migration is None:
        validate_half_month_links(half_state, source_state, reconciled_usage, personal)
        return copy.deepcopy(half_state), copy.deepcopy(source_state)
    require(usage is not None, 'missing_half_month_accounting')
    now = now or load_collector().utc_now()
    ledger = load_source_state()
    sources = copy.deepcopy(source_state)
    if migration is not None:
        if sources is None:
            require(migration['expectedPersonalBudgetsHash'] == data_hash(personal['budgets'])
                    and migration['sourceHash'] == source_baseline_hash(personal, reconciled_usage, transport),
                    'source_migration_baseline_changed')
            sources = ledger.baseline_state(
                personal, source_hash=migration['sourceHash'], at=now,
                source_imports=reconciled_usage.get('sourceImports', {}),
                historical_images=migration['historicalImages'], cooldowns=transport['cooldowns'])
        else:
            baseline = sources['baseline']
            require(baseline['sourceHash'] == migration['sourceHash']
                    and data_hash(baseline['personalBudgets']) == migration['expectedPersonalBudgetsHash']
                    and baseline['historicalImages'] == migration['historicalImages'],
                    'source_migration_already_exists')
    require(sources is not None, 'missing_half_month_source_usage')
    module = load_half_month_state()
    if entries:
        personal_collector = personal_collector or load_personal_collector()
        schedule = personal_collector.read_js(root / 'data' / 'schedule.js', 'SCHEDULE_DATA')
        insights = personal_collector.read_js(root / 'data' / 'store-insights.js', 'STORE_INSIGHTS')
        with (root / 'tools' / 'data' / 'accounts.csv').open(encoding='utf-8-sig', newline='') as stream:
            accounts = list(csv.DictReader(stream))
        half_state = load_half_month_saved().apply_amendments(
            half_state, entries, usage, module, schedule=schedule, insights=insights,
            accounts=accounts, personal_state=personal, now=now)
    elif half_state is None:
        half_state = module.empty_state()
    validate_half_month_links(half_state, sources, reconciled_usage, personal)
    return half_state, sources


def combined_status(official, personal):
    incomplete = {'partial', 'unavailable', 'paused', 'budget-exhausted'}
    if official in incomplete or personal in incomplete:
        return 'unavailable' if official in incomplete and personal in incomplete else 'partial'
    return 'ok' if 'ok' in (official, personal) else official


def validate_completion(path, status, code, component):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_JSON_BYTES,
            component + '_report_missing')
    try:
        report = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=no_duplicate_keys)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CloudError(component + '_report_invalid') from None
    # Only inspect a completion attestation. Never copy or log the report, which
    # is permitted to contain internal paths and other operational diagnostics.
    require(isinstance(report, dict) and report.get('component') == component
            and report.get('status') == status and type(report.get('exitCode')) is int
            and report['exitCode'] == code, component + '_report_mismatch')
    return report


def validate_personal_completion(path, status, code):
    return validate_completion(path, status, code, 'personal')


def orchestrate(args, *, root=ROOT, environment=None, collector=None, personal=None):
    environment = dict(os.environ if environment is None else environment)
    require(args.mode in ('restore', 'collect', 'personal', 'both', 'daily-guidance', 'apply-saved'),
            'invalid_collection_mode')
    environment = {key: value for key, value in environment.items()
                   if not key.startswith('CLOUD_COLLECTION_')}
    enabled = environment.get('DAILY_GUIDANCE_ENABLED', 'false') == 'true'
    half_enabled = environment.get('HALF_MONTH_SCHEDULE_ENABLED', 'false') == 'true'
    scheduled = args.mode == 'daily-guidance'
    applying = args.mode == 'apply-saved'
    writing = args.mode != 'restore'
    collect_official = args.mode in ('collect', 'both', 'daily-guidance')
    collect_personal = args.mode in ('personal', 'both', 'daily-guidance')
    collect_half_month = half_enabled and args.mode in ('both', 'daily-guidance')
    require(not collect_half_month or enabled, 'half_month_requires_daily_guidance')
    personal_backend = environment.get('PERSONAL_ANALYSIS_BACKEND', 'rules')
    if collect_personal:
        require(personal_backend in ('rules', 'azure'), 'invalid_analysis_backend')
    authoritative_usage = enabled or (collect_personal and personal_backend == 'azure')
    if writing:
        trusted_context(environment)
        require_manual_personal(args.mode, environment)
    manifest = read_saved_manifest(environment) if applying else None
    output, recovery = checked_paths(root, args.output, args.recovery_dir)
    collector = collector or load_collector()
    work = root / ('.cc-work-' + uuid.uuid4().hex[:16])
    work.mkdir()
    state_dir = work / 'state'
    state_dir.mkdir()
    repo = StateRepository(state_dir, environment)
    try:
        source = repo.initialize(root, writing=writing)
        if applying:
            require(source == manifest['expectedMainSHA'] and repo.head == manifest['expectedStateSHA'],
                    'saved_manifest_stale')
        if repo.head:
            canonical, _ = validate_snapshot(state_dir / SNAPSHOT, collector)
            validate_transport(state_dir / HTTP_STATE, collector)
            if (state_dir / LEASE).exists():
                validate_lease(state_dir / LEASE, collector)
                raise CloudError('unresolved_lease')
        else:
            canonical, raw = validate_snapshot(output, collector)
            atomic_bytes(state_dir / SNAPSHOT, raw)
            sidecar = output.with_suffix('.http-state.json')
            if sidecar.exists():
                _, raw = validate_transport(sidecar, collector)
                atomic_bytes(state_dir / HTTP_STATE, raw)
            else:
                collector.atomic_json(state_dir / HTTP_STATE, {
                    'schemaVersion': 1, 'cooldowns': canonical.get('cooldowns', {})})
        has_ai = (state_dir / AI_USAGE).exists()
        usage_state = None
        if has_ai:
            usage_state, _ = validate_ai_usage(state_dir / AI_USAGE)
        require(not authoritative_usage or applying or has_ai, 'missing_ai_usage')
        require(not authoritative_usage or applying or bool(usage_state['imports']),
                'missing_initial_usage_import')
        has_personal = (state_dir / PERSONAL).exists()
        has_half_month = (state_dir / HALF_MONTH).exists()
        has_source = (state_dir / SOURCE_USAGE).exists()
        half_month_state = source_usage_state = None
        if has_half_month:
            half_month_state, _ = validate_half_month(state_dir / HALF_MONTH)
        if has_source:
            source_usage_state, _ = validate_source_usage(state_dir / SOURCE_USAGE)
        require(not has_half_month or has_source, 'missing_half_month_source_usage')
        require(not half_enabled or applying or (has_half_month and has_source),
                'missing_half_month_state')
        personal_seed = output.parent / PERSONAL
        personal_state = None
        personal_source = 'absent'
        if has_personal or personal_seed.exists():
            personal = personal or load_personal_collector()
            if has_personal:
                personal_state, personal_raw = validate_personal(state_dir / PERSONAL, personal)
            else:
                seed, _ = read_json(personal_seed)
                if 'pending' in seed:
                    personal_state, personal_raw = validate_personal(personal_seed, personal)
                else:
                    seed, _ = validate_personal(personal_seed, personal, private=False)
                    personal_state = personal.empty_state()
                    personal.merge_seed(personal_state, seed)
                    personal_state['lastRun'] = copy.deepcopy(seed['lastRun'])
                    seed_path = work / PERSONAL
                    collector.atomic_json(seed_path, personal_state)
                    personal_state, personal_raw = validate_personal(seed_path, personal)
            personal_source = 'branch' if has_personal else 'seed'
        if authoritative_usage and not applying:
            validate_reconciled_usage(usage_state, personal_state, collector)
        validate_half_month_links(half_month_state, source_usage_state, usage_state, personal_state)
        if collect_personal:
            require(personal_state is not None, 'missing_personal_seed')
            if not has_personal:
                # Do not extend the remote format during an official cron run.
                # The first explicit manual run from merged main owns migration.
                atomic_bytes(state_dir / PERSONAL, personal_raw)
                has_personal = True
        bundle = {'include_personal': has_personal, 'personal': personal, 'include_ai': has_ai,
                  'include_half_month': has_half_month, 'include_source': has_source}
        result = {
            'sourceCodeSHA': source, 'stateCommit': repo.head,
            'stateSource': 'branch' if repo.head else 'seed',
            'collectionStatus': canonical['lastRun']['status'],
            'collectionCode': -1, 'persistenceStatus': 'restored',
            'collectionMode': args.mode,
            'officialCollectionStatus': canonical['lastRun']['status'],
            'officialCollectionCode': -1,
            'personalCollectionStatus': personal_state['lastRun']['status'] if personal_state else 'never',
            'personalCollectionCode': -1, 'personalStateSource': personal_source,
            'halfMonthCollectionStatus': half_month_state['lastRun']['status'] if half_month_state else 'never',
            'halfMonthCollectionCode': -1,
        }
        if args.mode == 'restore':
            # Byte-exact state handoff, and no seed replacement with an empty file.
            copy_pair(state_dir, output.parent, collector, **bundle)
            if personal_state is not None and not has_personal:
                atomic_bytes(personal_seed, personal_raw)
            return result

        if applying:
            require(has_personal, 'missing_personal_state')
            # Validate the entire delta against current state before obtaining a lease.
            try:
                prepared = prepare_saved(manifest, canonical, personal_state, usage_state,
                                         collector, personal)
                reconciled_source = source_usage_state
                if source_usage_state is not None and manifest['sourceReceipts']:
                    reconciled_source = load_source_state().apply_source_imports(
                        source_usage_state, manifest['sourceReceipts'],
                        personal_before=personal_state, personal_after=prepared[1],
                        analysis_before=usage_state, analysis_after=prepared[2])
                transport, _ = validate_transport(state_dir / HTTP_STATE, collector)
                prepared_half_month, prepared_source = prepare_half_month_saved(
                    manifest, half_month_state, reconciled_source, prepared[1],
                    usage_state, transport, root=root, personal_collector=personal,
                    now=collector.utc_now(), accounting_usage=prepared[2])
            except (ValueError, TypeError, KeyError, OverflowError):
                raise CloudError('saved_manifest_rejected') from None
        if repo.head and output.exists() and not applying:
            mirror, _ = validate_snapshot(output, collector)
            canonical = collector.merge_snapshots(canonical, mirror)
        # Keep the longest recorded host cooldown, even if the canonical facts
        # predate an HTTP-only save. The collector consumes the same sidecar.
        limits, _ = validate_transport(state_dir / HTTP_STATE, collector)
        local_sidecar = output.with_suffix('.http-state.json')
        if local_sidecar.exists() and not applying:
            local, _ = validate_transport(local_sidecar, collector)
            for host, until in local['cooldowns'].items():
                previous = limits['cooldowns'].get(host)
                if previous is None or collector.timestamp(until) > collector.timestamp(previous):
                    limits['cooldowns'][host] = until
        for host, until in canonical.get('cooldowns', {}).items():
            previous = limits['cooldowns'].get(host)
            if previous is None or collector.timestamp(until) > collector.timestamp(previous):
                limits['cooldowns'][host] = until
        canonical['cooldowns'] = dict(limits['cooldowns'])
        collector.atomic_json(state_dir / SNAPSHOT, canonical)
        collector.atomic_json(state_dir / HTTP_STATE, limits)
        lease = {
            'schemaVersion': 1, 'managedBy': MANAGER, 'repository': REPOSITORY,
            # Distinguish simultaneous invocations even within the same Actions
            # run/attempt/second, so their lease commits cannot be identical.
            'leaseId': uuid.uuid4().hex,
            'runId': environment['GITHUB_RUN_ID'],
            'runAttempt': environment['GITHUB_RUN_ATTEMPT'],
            'createdAt': collector.iso(collector.utc_now()), 'sourceCodeSHA': source,
        }
        collector.atomic_json(state_dir / LEASE, lease)
        copy_pair(state_dir, recovery, collector, **bundle)
        if not has_personal:
            (recovery / PERSONAL).unlink(missing_ok=True)
        repo.persist(collector, leased=True, personal=personal)
        collected = work / 'collected'
        copy_pair(state_dir, collected, collector, **bundle)
        buffered = None
        initial_official = initial_requests = None
        try:
            if applying:
                canonical, personal_state, usage = prepared
                collector.atomic_json(collected / SNAPSHOT, canonical)
                collector.atomic_json(collected / PERSONAL, personal_state)
                collector.atomic_json(collected / AI_USAGE, usage)
                bundle['include_ai'] = True
                if prepared_half_month is not None:
                    collector.atomic_json(collected / HALF_MONTH, prepared_half_month)
                    bundle['include_half_month'] = True
                if prepared_source is not None:
                    collector.atomic_json(collected / SOURCE_USAGE, prepared_source)
                    bundle['include_source'] = True
            environment.update(
                CLOUD_COLLECTION_SHARED='true' if has_ai else 'false',
                CLOUD_COLLECTION_RUN_ID=environment['GITHUB_RUN_ID'] + '-' + environment['GITHUB_RUN_ATTEMPT'],
                CLOUD_COLLECTION_SCHEDULED='true' if scheduled else 'false',
                CLOUD_COLLECTION_OFFICIAL_AZURE='true' if enabled else 'false',
                CLOUD_COLLECTION_SOURCE_ENABLED='true' if has_source else 'false',
                CLOUD_COLLECTION_HALF_MONTH_PRESENT='true' if has_half_month else 'false')
            if enabled:
                environment['PERSONAL_ANALYSIS_BACKEND'] = 'azure'
            if enabled and collect_official:
                source_paused = has_source and source_usage_state.get('paused') is not None
                personal_active = collect_personal and not personal_state.get('paused') and not source_paused
                if collect_half_month and not half_month_state.get('paused') and not source_paused:
                    allocation = half_month_official_allocation(
                        collected / AI_USAGE, environment['CLOUD_COLLECTION_RUN_ID'],
                        collector.utc_now(), personal_active=personal_active, scheduled=scheduled)
                else:
                    allocation = official_allocation(
                        collected / AI_USAGE, environment['CLOUD_COLLECTION_RUN_ID'],
                        collector.utc_now(), personal_active, scheduled)
                environment['CLOUD_COLLECTION_ANALYSIS_LIMIT'] = str(allocation)
                if collect_personal:
                    environment['CLOUD_COLLECTION_BUFFER_MODE'] = 'write'
            if collect_official:
                code = invoke_collector(root, collected, work / 'official-report.json', environment)
                require(code in (0, 2, 3), 'collector_local_failure')
                canonical, _ = validate_snapshot(collected / SNAPSHOT, collector)
                status = canonical['lastRun']['status']
                require(status != 'never' and code == {'partial': 2, 'unavailable': 3}.get(status, 0),
                        'collector_status_mismatch')
                report = validate_completion(work / 'official-report.json', status, code, 'official')
                requests = report.get('requests')
                require(isinstance(requests, dict), 'official_source_report_missing')
                integer(requests.get('searches'), 0, 2)
                integer(requests.get('posts'), 0, 20)
                environment['CLOUD_COLLECTION_PERSONAL_POSTS'] = str(min(3, 20 - requests['posts']))
                result.update(officialCollectionStatus=status, officialCollectionCode=code)
                if environment.get('CLOUD_COLLECTION_BUFFER_MODE') == 'write':
                    buffered = load_official_buffer(
                        root, collected, canonical, environment['CLOUD_COLLECTION_RUN_ID'], collector)
                    if collect_half_month and len(buffered['items']) > 2:
                        buffered = collector.make_analysis_buffer(
                            canonical, buffered['runId'], buffered['versionHash'],
                            buffered['items'][:2], collector.utc_now())
                        collector.atomic_json(analysis_buffer_path(root, collected), buffered)
                    initial_official, initial_requests = copy.deepcopy(canonical), dict(requests)
            if collect_personal and scheduled and not personal_window_open(
                    collector.utc_now(), scheduled=True):
                # Do not even construct the personal child after its scheduled cutoff.
                now = collector.utc_now()
                personal_state['checkedAt'] = collector.iso(now)
                personal_state['lastRun'] = {
                    'status': 'outside-window', 'date': now.astimezone(JST).date().isoformat(),
                    'finishedAt': collector.iso(now), 'complete': False,
                    'requests': {'searches': 0, 'posts': 0},
                    'pendingCount': len(personal_state['pending']),
                    'deferredCount': len(personal_state['pending']),
                }
                collector.atomic_json(collected / PERSONAL, personal_state)
                result.update(personalCollectionStatus='outside-window', personalCollectionCode=0)
                collect_personal = False
            if collect_personal:
                if (collect_half_month and not half_month_state.get('paused')
                        and not personal_deadline_near(collector.utc_now(), scheduled=scheduled)):
                    environment['CLOUD_COLLECTION_ANALYSIS_LIMIT'] = str(
                        half_month_personal_allocation(
                            collected / AI_USAGE, environment['CLOUD_COLLECTION_RUN_ID'],
                            collector.utc_now(), scheduled=scheduled))
                    environment['CLOUD_COLLECTION_PERSONAL_SEARCHES'] = '2'
                else:
                    environment['CLOUD_COLLECTION_ANALYSIS_LIMIT'] = '3'
                collector.atomic_json(work / 'personal-seed.json', personal.public_state(personal_state))
                code = invoke_personal_collector(
                    root, collected, work / 'personal-report.json', environment)
                require(code in (0, 2, 3), 'personal_local_failure')
                personal_state, _ = validate_personal(collected / PERSONAL, personal)
                status = personal_state['lastRun']['status']
                require(status in ('ok', 'partial', 'unavailable', 'no-new', 'no-results',
                                   'paused', 'budget-exhausted', 'outside-window'),
                        'personal_status_mismatch')
                require(code == (3 if status in ('unavailable', 'paused') else
                                 2 if status in ('partial', 'budget-exhausted') else 0),
                        'personal_status_mismatch')
                validate_personal_completion(work / 'personal-report.json', status, code)
                result.update(personalCollectionStatus=status, personalCollectionCode=code)
            if collect_half_month:
                code = invoke_half_month_collector(
                    root, collected, work / 'half-month-report.json', environment)
                require(code in (0, 2, 3), 'half_month_local_failure')
                half_month_state, _ = validate_half_month(collected / HALF_MONTH)
                status = half_month_state['lastRun']['status']
                require(status in ('ok', 'partial', 'unavailable', 'no-new', 'no-results',
                                   'paused', 'budget-exhausted', 'outside-window'),
                        'half_month_status_mismatch')
                require(code == (3 if status in ('unavailable', 'paused') else
                                 2 if status in ('partial', 'budget-exhausted') else 0),
                        'half_month_status_mismatch')
                report = validate_completion(
                    work / 'half-month-report.json', status, code, 'schedule')
                keys(report.get('requests'), ('searches', 'posts', 'images', 'analysis'),
                     ('searches', 'posts', 'images'))
                for kind, maximum in (('searches', 1), ('posts', 1), ('images', 4)):
                    integer(report['requests'][kind], 0, maximum)
                if 'analysis' in report['requests']:
                    integer(report['requests']['analysis'], 0, 1)
                validate_source_usage(collected / SOURCE_USAGE)
                result.update(halfMonthCollectionStatus=status, halfMonthCollectionCode=code)
            if buffered and buffered['items']:
                usage, _ = validate_ai_usage(collected / AI_USAGE)
                now = collector.utc_now()
                run_id = environment['CLOUD_COLLECTION_RUN_ID']
                remaining = load_analysis_state().remaining(usage, run_id, now)
                used = sum(item['runId'] == run_id and item['component'] == 'official'
                           for item in usage['receipts'].values())
                if (remaining and used < 3 and usage['paused'] is None
                        and (usage['retryAt'] is None or collector.timestamp(usage['retryAt']) <= now)):
                    environment['CLOUD_COLLECTION_ANALYSIS_LIMIT'] = str(min(3, used + remaining))
                    environment['CLOUD_COLLECTION_BUFFER_MODE'] = 'replay'
                    personal_before = (collected / PERSONAL).read_bytes()
                    code = invoke_collector(root, collected, work / 'official-resume-report.json', environment)
                    require(code in (0, 2, 3), 'official_resume_local_failure')
                    resumed, _ = validate_snapshot(collected / SNAPSHOT, collector)
                    require(resumed['lastRun']['status'] != 'never'
                            and code == {'partial': 2, 'unavailable': 3}.get(resumed['lastRun']['status'], 0),
                            'official_resume_status_mismatch')
                    report = validate_completion(
                        work / 'official-resume-report.json', resumed['lastRun']['status'], code, 'official')
                    keys(report.get('requests'), ('searches', 'posts'), ('searches', 'posts'))
                    for count in report['requests'].values():
                        require(type(count) is int and count == 0, 'official_resume_used_source')
                    require((collected / PERSONAL).read_bytes() == personal_before,
                            'official_resume_changed_personal')
                    resumed_ids = {item['id'] for item in buffered['items']}
                    failures = report.get('analysisReplayFailures', [])
                    require(isinstance(failures, list) and len(failures) <= 3,
                            'official_resume_report_mismatch')
                    for failure in failures:
                        keys(failure, ('id', 'reason', 'httpStatus', 'retryAt'), ('id', 'reason'))
                        require(failure['id'] in resumed_ids, 'official_resume_report_mismatch')
                        validate_failure(failure, collector)
                    resumed['lastRun']['failures'] = [*resumed['lastRun'].get('failures', []), *failures]
                    resumed['lastRun']['finishedAt'] = collector.iso(collector.utc_now())
                    canonical = aggregate_official_resume(
                        initial_official, resumed, resumed_ids, initial_requests, collector)
                    collector.atomic_json(collected / SNAPSHOT, canonical)
                    validate_snapshot(collected / SNAPSHOT, collector)
                    status = canonical['lastRun']['status']
                    result.update(officialCollectionStatus=status,
                                  officialCollectionCode={'partial': 2, 'unavailable': 3}.get(status, 0))
        finally:
            # Capture the actual saved bytes, not an in-memory report/projection.
            # Invalid data is never copied; retain a valid HTTP-only save so its
            # cooldowns can still be inspected during manual recovery.
            try:
                save_recovery(collected, recovery, collector, **bundle)
            finally:
                (work / ANALYSIS_BUFFER).unlink(missing_ok=True)
        if args.mode in ('both', 'daily-guidance'):
            status = combined_status(
                result['officialCollectionStatus'], result['personalCollectionStatus'])
            if collect_half_month:
                status = combined_status(status, result['halfMonthCollectionStatus'])
            code = {'partial': 2, 'unavailable': 3}.get(status, 0)
        elif applying:
            status, code = 'applied-saved', 0
        elif collect_personal:
            status, code = result['personalCollectionStatus'], result['personalCollectionCode']
        else:
            status, code = result['officialCollectionStatus'], result['officialCollectionCode']
        copy_pair(collected, state_dir, collector, **bundle)
        (state_dir / LEASE).unlink()
        result['stateCommit'] = repo.persist(collector, leased=False, personal=personal)
        copy_pair(state_dir, output.parent, collector, **bundle)
        if personal_state is not None and not has_personal:
            atomic_bytes(personal_seed, personal_raw)
        result.update(collectionStatus=status, collectionCode=code, persistenceStatus='saved')
        return result
    finally:
        remove_tree(work)


def emit(result, environment):
    output = environment.get('GITHUB_OUTPUT')
    if output:
        # Only fixed names and single-line values; no report/paths/token payloads.
        allowed = ('sourceCodeSHA', 'stateCommit', 'stateSource', 'collectionStatus',
                   'collectionCode', 'persistenceStatus', 'collectionMode',
                   'officialCollectionStatus', 'officialCollectionCode',
                   'personalCollectionStatus', 'personalCollectionCode', 'personalStateSource',
                   'halfMonthCollectionStatus', 'halfMonthCollectionCode',
                   'reason')
        lines = []
        for key in allowed:
            if key in result:
                value = str(result[key])
                require(re.fullmatch(r'[A-Za-z0-9_-]*', value), 'unsafe_action_output')
                lines.append(key + '=' + value + '\n')
        with Path(output).open('a', encoding='utf-8', newline='\n') as target:
            target.writelines(lines)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=(
        'restore', 'collect', 'personal', 'both', 'daily-guidance', 'apply-saved'), required=True)
    parser.add_argument('--output', type=Path, default=Path('data') / SNAPSHOT)
    parser.add_argument('--recovery-dir', type=Path, default=Path('.cloud-collection-recovery'))
    args = parser.parse_args(argv)
    try:
        result = orchestrate(args)
        emit(result, os.environ)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        reason = str(exc) if isinstance(exc, CloudError) else 'local_or_validation_failure'
        if not re.fullmatch(r'[a-z_]+', reason):
            reason = 'local_or_validation_failure'
        result = {'collectionStatus': 'failed', 'persistenceStatus': 'failed',
                  'reason': reason, 'recoveryInstructions': RECOVERY}
        try:
            emit(result, os.environ)
        except (OSError, ValueError, CloudError):
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 1


if __name__ == '__main__':
    sys.exit(main())
