"""Data-only collector-state orchestration for trusted GitHub Actions main runs.

CLI: --mode restore|collect|personal|both --output data/observed-shifts.json
collect remains official-only; personal/both require an explicit manual event.
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
personal-shifts.json is optional on legacy branches. Restore uses a checked main
seed when absent; only manual personal/both first adds it to the branch.
Recovery: inspect the failed run's recovery JSON files and shared cooldowns, commit
them to collector-state without force and remove lease.json in that same commit,
preserving state-owner.json, then run again. Reports and the private scratch
repository are never artifacts.
"""
import argparse
import copy
import datetime as dt
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
LEASE = 'lease.json'
OWNER_FILE = 'state-owner.json'
FILES = {SNAPSHOT, HTTP_STATE, PERSONAL, OWNER_FILE, LEASE}
MANAGER = 'cloud-collection/v1'
STATE_OWNER = {
    'schemaVersion': 1, 'owner': 'agurakakenai', 'managedBy': MANAGER,
    'repository': REPOSITORY, 'branch': 'collector-state',
}
MAX_JSON_BYTES = 16 * 1024 * 1024
SHA_RE = re.compile(r'[0-9a-f]{40}\Z')
RECOVERY = (
    '失敗runの公式JSON・共有HTTPstate・本人JSON（導入済みの場合）が揃っていることを'
    '検査し、cooldownと本人pause/budgetを確認してください。'
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


def safe_environment(environment, *, credentials=False):
    result = {
        key: value for key, value in environment.items()
        if not key.upper().startswith(('GIT_', 'GCM_', 'GH_DEBUG'))
        and key.upper() not in ('GH_HOST', 'GH_FORCE_TTY', 'GITHUB_TOKEN')
    }
    if not credentials:
        result.pop('GH_TOKEN', None)
        result.pop('GITHUB_OUTPUT', None)
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
    keys(value, ('search.yahoo.co.jp', collector.POST_HOST))
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


def validate_notices(notices):
    require(isinstance(notices, list))
    for notice in notices:
        keys(notice, ('name', 'kind', 'excerpt', 'time'), ('name', 'kind', 'excerpt'))
        require(isinstance(notice['name'], str)
                and re.fullmatch(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', notice['name'])
                and notice['kind'] == 'late'
                and isinstance(notice['excerpt'], str) and 1 <= len(notice['excerpt']) <= 160
                and '\r' not in notice['excerpt'] and '\n' not in notice['excerpt'])
        if 'time' in notice:
            require(isinstance(notice['time'], str)
                    and re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', notice['time']))


def validate_official_names(names):
    require(isinstance(names, list) and names
            and all(isinstance(name, str)
                    and re.fullmatch(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', name) for name in names)
            and len(names) == len(set(names)))


def validate_edit_tweet_ids(post, collector):
    if 'editTweetIds' not in post:
        return
    ids = post['editTweetIds']
    require(isinstance(ids, list) and 1 <= len(ids) <= collector.MAX_POSTS
            and all(isinstance(tid, str) and collector.post_id(tid) for tid in ids),
            'invalid_edit_chain')
    require(len(ids) == len(set(ids)) and ids[-1] == post['id']
            and all(int(first) < int(second) for first, second in zip(ids, ids[1:])),
            'invalid_edit_chain')
    try:
        day = collector.service_day(collector.timestamp(post['createdAt']))
        same_day = all(collector.service_day(collector.snowflake_time(tid)) == day for tid in ids)
    except (ValueError, TypeError, OverflowError, OSError):
        raise CloudError('edit_date_mismatch') from None
    require(same_day, 'edit_date_mismatch')


def validate_snapshot(path, collector):
    state, raw = read_json(path)
    keys(state, ('schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt',
                 'posts', 'pending', 'resolved', 'cooldowns', 'lastRun'),
         ('schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt',
          'posts', 'pending', 'lastRun'))
    # Reuse the collector's author/id/url/date checks and resolved-first merge.
    collector.load_snapshot(path)
    for post in state['posts']:
        fields = ('id', 'url', 'authorId', 'authorScreenName', 'createdAt',
                  'date', 'shift', 'storeId', 'names', 'observedAt')
        keys(post, (*fields, 'notices', 'lastCheckedAt', 'revisions', 'editTweetIds'), fields)
        validate_edit_tweet_ids(post, collector)
        validate_official_names(post['names'])
        # Edit metadata never authorizes substituting another version's creation time.
        require(abs((collector.snowflake_time(post['id'])
                     - collector.timestamp(post['createdAt'])).total_seconds()) < 2)
        validate_notices(post.get('notices', []))
        if 'lastCheckedAt' in post:
            collector.timestamp(post['lastCheckedAt'])
        require(isinstance(post.get('revisions', []), list))
        for revision in post.get('revisions', []):
            fields = ('date', 'shift', 'storeId', 'names', 'notices', 'observedAt')
            keys(revision, fields, fields)
            require(revision['date'] == collector.service_day(
                collector.timestamp(post['createdAt'])).isoformat()
                and revision['shift'] in ('昼', '夜') and revision['storeId'] in collector.STORE_IDS.values())
            validate_official_names(revision['names'])
            validate_notices(revision['notices'])
            collector.timestamp(revision['observedAt'])
    for pending in state['pending']:
        keys(pending, ('id', 'url', 'reason', 'firstSeenAt', 'lastAttemptAt',
                       'attempts', 'httpStatus', 'retryAt', 'editSourceId'),
             ('id', 'url', 'reason', 'firstSeenAt', 'lastAttemptAt', 'attempts'))
        integer(pending['attempts'])
        if 'editSourceId' in pending:
            source = pending['editSourceId']
            require(isinstance(source, str) and collector.post_id(source)
                    and int(source) < int(pending['id']), 'invalid_edit_source')
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
        'deferredCount', 'pendingCount', 'pendingOutsideRangeCount', 'maxPosts',
        'refreshedCount', 'updatedPostCount', 'noticeCount')
    keys(run, (*counts, 'status', 'dateFrom', 'dateTo', 'dateBasis', 'finishedAt',
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


def validate_personal(path, personal=None, *, private=True):
    state, raw = read_json(path)
    personal = personal or load_personal_collector()
    personal.read_state(path, private=private)
    public_fields = ('schemaVersion', 'complete', 'checkedAt', 'lastSuccessAt', 'posts', 'lastRun')
    private_fields = ('pending', 'resolved', 'budgets', 'paused', 'identityBindings',
                      'originalTargets', 'lastRequests')
    fields = (*public_fields, *private_fields) if private else public_fields
    keys(state, fields, fields)
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
        keys(post, fields, fields)
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
                and source['url'] in personal.search_urls(dt.date.fromisoformat(run['date'])))
        if 'candidateCount' in source:
            integer(source['candidateCount'])
        validate_failure(source, official)
    for failure in run.get('failures', []):
        keys(failure, ('id', 'reason', 'httpStatus', 'retryAt'), ('reason',))
        if 'id' in failure:
            require(isinstance(failure['id'], str) and official.post_id(failure['id']))
        validate_failure(failure, official)
    if private:
        for item in state['pending']:
            fields = ('id', 'url', 'name', 'authorId', 'authorScreenName', 'date',
                      'searchCreatedAt', 'reason', 'firstSeenAt', 'lastAttemptAt', 'attempts')
            keys(item, (*fields, 'httpStatus', 'retryAt'), fields)
            identity(item)
            require(personal.calendar_day(official.timestamp(item['searchCreatedAt'])).isoformat()
                    == item['date'])
            require(abs((official.snowflake_time(item['id'])
                         - official.timestamp(item['searchCreatedAt'])).total_seconds()) < 2)
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
    if mode not in ('personal', 'both'):
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
        if has_personal:
            validate_personal(self.path / PERSONAL, personal)
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
        if leased:
            self.git('add', '--', LEASE, reason='state_stage_failed')
        else:
            self.git('rm', '--quiet', '--cached', '--ignore-unmatch', '--', LEASE,
                     reason='state_stage_failed')
        message = ('Record collection lease' if leased else 'Save collection state') + (
            '\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>')
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
            SNAPSHOT, HTTP_STATE, PERSONAL},
                'unsafe_recovery_path')
    return output, recovery


def copy_pair(source, destination, collector, *, include_personal=False, personal=None):
    _, canonical = validate_snapshot(source / SNAPSHOT, collector)
    _, transport = validate_transport(source / HTTP_STATE, collector)
    extra = validate_personal(source / PERSONAL, personal)[1] if include_personal else None
    atomic_bytes(destination / SNAPSHOT, canonical)
    atomic_bytes(destination / HTTP_STATE, transport)
    if extra is not None:
        atomic_bytes(destination / PERSONAL, extra)


def save_recovery(source, destination, collector, *, include_personal=False, personal=None):
    invalid = False
    validators = [(SNAPSHOT, validate_snapshot), (HTTP_STATE, validate_transport)]
    if include_personal:
        validators.append((PERSONAL, lambda path, _: validate_personal(path, personal)))
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
    try:
        process = child_process(
            [sys.executable, '-I', '-B', str(root / 'tools' / 'collect-shifts.py'),
             '--once', '--days', '2', '--max-posts', '20',
             '--refresh-known', '3',
             '--snapshot', str(state / SNAPSHOT), '--report', str(report)],
            cwd=root, environment=safe_environment(environment), timeout=1200)
    except (OSError, subprocess.SubprocessError):
        raise CloudError('collector_process_failed') from None
    # The report/stdout/stderr may contain local paths and process IDs.
    return process.returncode


def invoke_personal_collector(root, state, report, environment):
    try:
        process = child_process(
            [sys.executable, '-I', '-B', str(root / 'tools' / 'collect-personal-shifts.py'),
             '--once', '--snapshot', str(state / PERSONAL),
             '--http-state', str(state / HTTP_STATE),
             '--seed', str(state.parent / 'personal-seed.json'),
             '--max-searches', '3', '--max-posts', '3', '--report', str(report)],
            cwd=root, environment=safe_environment(environment), timeout=600)
    except (OSError, subprocess.SubprocessError):
        raise CloudError('personal_process_failed') from None
    return process.returncode


def combined_status(official, personal):
    incomplete = {'partial', 'unavailable', 'paused', 'budget-exhausted'}
    if official in incomplete or personal in incomplete:
        return 'unavailable' if official in incomplete and personal in incomplete else 'partial'
    return 'ok' if 'ok' in (official, personal) else official


def validate_personal_completion(path, status, code):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_JSON_BYTES,
            'personal_report_missing')
    try:
        report = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=no_duplicate_keys)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CloudError('personal_report_invalid') from None
    # Only inspect a completion attestation. Never copy or log the report, which
    # is permitted to contain internal paths and other operational diagnostics.
    require(isinstance(report, dict) and report.get('component') == 'personal'
            and report.get('status') == status and type(report.get('exitCode')) is int
            and report['exitCode'] == code, 'personal_report_mismatch')


def orchestrate(args, *, root=ROOT, environment=None, collector=None, personal=None):
    environment = dict(os.environ if environment is None else environment)
    require(args.mode in ('restore', 'collect', 'personal', 'both'), 'invalid_collection_mode')
    writing = args.mode != 'restore'
    collect_official = args.mode in ('collect', 'both')
    collect_personal = args.mode in ('personal', 'both')
    if writing:
        trusted_context(environment)
        require_manual_personal(args.mode, environment)
    output, recovery = checked_paths(root, args.output, args.recovery_dir)
    collector = collector or load_collector()
    work = root / ('.cc-work-' + uuid.uuid4().hex[:16])
    work.mkdir()
    state_dir = work / 'state'
    state_dir.mkdir()
    repo = StateRepository(state_dir, environment)
    try:
        source = repo.initialize(root, writing=writing)
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
        has_personal = (state_dir / PERSONAL).exists()
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
        if collect_personal:
            require(personal_state is not None, 'missing_personal_seed')
            if not has_personal:
                # Do not extend the remote format during an official cron run.
                # The first explicit manual run from merged main owns migration.
                atomic_bytes(state_dir / PERSONAL, personal_raw)
                has_personal = True
        bundle = {'include_personal': has_personal, 'personal': personal}
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
        }
        if args.mode == 'restore':
            # Byte-exact state handoff, and no seed replacement with an empty file.
            copy_pair(state_dir, output.parent, collector, **bundle)
            if personal_state is not None and not has_personal:
                atomic_bytes(personal_seed, personal_raw)
            return result

        if repo.head and output.exists():
            mirror, _ = validate_snapshot(output, collector)
            canonical = collector.merge_snapshots(canonical, mirror)
        # Keep the longest recorded host cooldown, even if the canonical facts
        # predate an HTTP-only save. The collector consumes the same sidecar.
        limits, _ = validate_transport(state_dir / HTTP_STATE, collector)
        local_sidecar = output.with_suffix('.http-state.json')
        if local_sidecar.exists():
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
        try:
            if collect_official:
                code = invoke_collector(root, collected, work / 'official-report.json', environment)
                require(code in (0, 2, 3), 'collector_local_failure')
                canonical, _ = validate_snapshot(collected / SNAPSHOT, collector)
                status = canonical['lastRun']['status']
                require(status != 'never' and code == {'partial': 2, 'unavailable': 3}.get(status, 0),
                        'collector_status_mismatch')
                result.update(officialCollectionStatus=status, officialCollectionCode=code)
            if collect_personal:
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
        finally:
            # Capture the actual saved bytes, not an in-memory report/projection.
            # Invalid data is never copied; retain a valid HTTP-only save so its
            # cooldowns can still be inspected during manual recovery.
            save_recovery(collected, recovery, collector, **bundle)
        if args.mode == 'both':
            status = combined_status(
                result['officialCollectionStatus'], result['personalCollectionStatus'])
            code = {'partial': 2, 'unavailable': 3}.get(status, 0)
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
    parser.add_argument('--mode', choices=('restore', 'collect', 'personal', 'both'), required=True)
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
