"""Automatic roster updates, recorded on collector-state and applied only at publication.

Graduations come only from the member's own announcement: her own post (a reply to
herself is fine; replies to others and reposts are not) that names her own last service
day explicitly. The text model (gpt-6-luna, low) must classify the post as her own
graduation AND the date it returns must be one of the dates found mechanically in the
body. Undated announcements, other people's graduations and graduation events are not
used. Results live in roster-updates.json on collector-state (post ID, URL, body hash);
main is never committed. data/members.json stays authoritative for manual decisions:
a manual graduation wins, and `member-registry.py reject-auto-graduation` rejects one.

New members come from the official staff page (https://akibazettai.com/staff/), read once a
day with check-roster.py's parser. A name is added only when it newly appears there (or was
never known at all) and nobody currently holds it; a name reused from an earlier generation
becomes a separate member with a new ID, never a reactivation. Her store comes from the
page's store tab and her X account from her introduction page when listed there; without
one she is added but not collected. Disappearing from the page never removes anyone. A
failed or structurally changed page changes nothing and is reported for the alert.
A manual registration wins, and `member-registry.py reject-auto-addition` rejects one.

Commands:
  run      collector child: search, verify and record (shared source/AI ledgers)
  publish  apply the recorded updates to data/members.json, members.js and asset stamps
"""
import argparse
import calendar
import copy
import datetime as dt
import hashlib
import html as html_module
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc
JST = dt.timezone(dt.timedelta(hours=9))
FILE = 'roster-updates.json'
SEARCH_HOST = 'search.yahoo.co.jp'
POST_HOST = 'cdn.syndication.twimg.com'
STAFF_URL = 'https://akibazettai.com/staff/'
STAFF_DETAIL = re.compile(r'https://akibazettai\.com/staff/[A-Za-z0-9_-]{1,80}/\Z')
MAX_STAFF_BYTES = 2_000_000
MIN_STAFF_ENTRIES = 10
MAX_ADDITIONS = 5
# An unannounced departure needs this many consecutive healthy staff-page checks (one per
# JST day) without her, and no activity of hers since she first went missing.
MISSING_CHECKS = 3
SERVICE_SOURCES = ('official', 'curated', 'personal', 'none')
HIATUS_WORDS = ('休止', 'お休み', 'おやすみ', '休養', '休業', '休み期間')
HIATUS_RECHECK_DAYS = 30
HIATUS_LOOKBACK_DAYS = 60
OFFICIAL_AUTHOR_ID = '822429861218131969'
CONTRACT = 'roster-graduation-v1'
MAX_SEARCHES, MAX_POSTS, MAX_ANALYSIS = 6, 3, 2
RUNS_PER_DAY = 3
RUNTIME_SECONDS = 420
LOOKBACK_DAYS = 120
MAX_BODY = 4_000_000
MAX_INPUT_BYTES = 6000
MAX_OUTPUT_TOKENS = 400
HISTORY_LIMIT, POSTS_LIMIT = 1000, 2000
HEX = re.compile(r'[0-9a-f]{64}\Z')
POST_ID = re.compile(r'[1-9][0-9]{9,24}\Z')
HANDLE = re.compile(r'[A-Za-z0-9_]{1,15}\Z')
RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z')
REASON = re.compile(r'[a-z_]{1,64}\Z')
STATUSES = {'ok', 'no-new', 'partial', 'unavailable'}
KEYWORD = '卒業'
WEEKDAYS = '月火水木金土日'
DATE_PATTERNS = (
    # 2026年10月27日 / 10月27日 / 10/27 / 16日（金）
    re.compile(r'(?:(?P<year>20\d{2})\s*年\s*)?(?<![0-9０-９])(?P<month>[0-9０-９]{1,2})\s*月\s*'
               r'(?P<day>[0-9０-９]{1,2})\s*日'),
    re.compile(r'(?<![0-9０-９/／.])(?P<month>[0-9０-９]{1,2})\s*[/／]\s*(?P<day>[0-9０-９]{1,2})(?![0-9０-９/／])'),
    re.compile(r'(?<![0-9０-９月/／])(?P<day>[0-9０-９]{1,2})\s*日\s*(?:[（(]\s*(?P<weekday>[月火水木金土日])\s*[)）])?'),
)
PROMPT = """You check one Japanese social-media post by a maid of the café Akiba Zettai Ryoiki.
Decide whether the AUTHOR herself announces HER OWN graduation (leaving the café) and, if so,
the last day she works there exactly as written in the post.
kind:
- self_graduation: the author announces that she herself graduates / leaves.
- other_graduation: the post is about someone else's graduation.
- event_or_plan: a graduation event, ceremony or plan that is not her own leaving, or a
  hypothetical or past reference.
- unrelated: anything else.
lastServiceDate: the month and day of her final working day ONLY when that date is written
in the post (for example 10月27日, 10/27, or 16日（金） for her final shift). Return null when no
date is written, when only relative words such as 明日 or 本日 are used, or when unsure.
Never guess a date. evidenceLineIds: the line ids that state the graduation and the date.
"""
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['kind', 'lastServiceDate', 'evidenceLineIds'],
    'properties': {
        'kind': {'type': 'string', 'enum': ['self_graduation', 'other_graduation', 'event_or_plan', 'unrelated']},
        'lastServiceDate': {'anyOf': [
            {'type': 'null'},
            {'type': 'object', 'additionalProperties': False, 'required': ['month', 'day'],
             'properties': {'month': {'type': 'integer'}, 'day': {'type': 'integer'}}}]},
        'evidenceLineIds': {'type': 'array', 'items': {'type': 'integer'}},
    },
}


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


members = _module('roster_member_registry', 'member-registry.py')
yahoo = _module('roster_yahoo', 'yahoo-search.py')
ledger = _module('roster_analysis_state', 'analysis-state.py')
source_state = _module('roster_source_state', 'source-state.py')
site = _module('roster_check_roster', 'check-roster.py')


class RosterError(ValueError):
    pass


class Stop(Exception):
    """End this run early; recorded state stays valid."""

    def __init__(self, reason, status='partial'):
        super().__init__(reason)
        self.reason, self.status = reason, status


def require(condition, reason='invalid_roster_updates'):
    if not condition:
        raise RosterError(reason)


def digest(value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def stamp(value):
    require(isinstance(value, dt.datetime) and value.utcoffset() is not None, 'timezone_required')
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value),
            'invalid_roster_timestamp')
    return dt.datetime.fromisoformat(value[:-1] + '+00:00')


def day_text(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value), 'invalid_roster_date')
    try:
        require(dt.date.fromisoformat(value).isoformat() == value, 'invalid_roster_date')
    except ValueError:
        raise RosterError('invalid_roster_date') from None
    return value


def keys(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional))


def empty_state():
    return {'schemaVersion': 1, 'graduations': [], 'additions': [], 'departures': [], 'absences': {},
            'changes': [], 'scan': {}, 'posts': {}, 'staff': None, 'lastRun': None}


def status_url(handle, tid):
    require(HANDLE.fullmatch(handle or '') is not None and POST_ID.fullmatch(tid or '') is not None,
            'invalid_status_url')
    return f'https://x.com/{handle}/status/{tid}'


def validate_graduation(row):
    keys(row, ('memberId', 'name', 'lastServiceDate', 'evidenceUrl', 'postId', 'authorId',
               'bodySha256', 'postCreatedAt', 'detectedAt', 'runId'))
    require(isinstance(row['memberId'], str) and members.MEMBER_ID.fullmatch(row['memberId']))
    members.valid_name(row['name'])
    day_text(row['lastServiceDate'])
    require(POST_ID.fullmatch(row['postId'] or '') is not None and POST_ID.fullmatch(row['authorId'] or '') is not None)
    match = members.STATUS_URL.fullmatch(row['evidenceUrl'] or '')
    require(match is not None and match[2] == row['postId'] and match[1].lower() != members.OFFICIAL_HANDLE)
    require(HEX.fullmatch(row['bodySha256'] or '') is not None)
    timestamp(row['postCreatedAt'])
    timestamp(row['detectedAt'])
    require(RUN_ID.fullmatch(row['runId'] or '') is not None)


def profile_or_none(value):
    require(value is None or members.normalize_profile_url(value) == value)


def validate_addition(row):
    keys(row, ('memberId', 'name', 'homeStore', 'xProfileUrl', 'detailUrl', 'pageSha256', 'detectedAt', 'runId'))
    require(isinstance(row['memberId'], str) and members.MEMBER_ID.fullmatch(row['memberId']))
    members.valid_name(row['name'])
    require(row['homeStore'] in ('s1', 's2', 's3', 's4'))
    profile_or_none(row['xProfileUrl'])
    require(isinstance(row['detailUrl'], str) and STAFF_DETAIL.fullmatch(row['detailUrl']))
    require(HEX.fullmatch(row['pageSha256'] or '') is not None)
    timestamp(row['detectedAt'])
    require(RUN_ID.fullmatch(row['runId'] or '') is not None)


def validate_departure(row):
    keys(row, ('memberId', 'name', 'lastServiceDate', 'lastServiceSource', 'offRosterFrom', 'missingSince',
               'checks', 'xProfileUrl', 'detectedAt', 'runId', 'reinstatedAt', 'reinstateUrl'))
    require(isinstance(row['memberId'], str) and members.MEMBER_ID.fullmatch(row['memberId']))
    members.valid_name(row['name'])
    require(day_text(row['lastServiceDate']) < day_text(row['offRosterFrom']))
    require(row['lastServiceSource'] in SERVICE_SOURCES)
    require(day_text(row['missingSince']) <= row['offRosterFrom'])
    require(type(row['checks']) is int and MISSING_CHECKS <= row['checks'] <= 9999)
    profile_or_none(row['xProfileUrl'])
    timestamp(row['detectedAt'])
    require(RUN_ID.fullmatch(row['runId'] or '') is not None)
    require((row['reinstatedAt'] is None) == (row['reinstateUrl'] is None))
    if row['reinstatedAt'] is not None:
        timestamp(row['reinstatedAt'])
        require(STAFF_DETAIL.fullmatch(row['reinstateUrl']) is not None)


def validate_absence(row):
    keys(row, ('missingSince', 'checks', 'lastCheckDate', 'hiatusUrl', 'hiatusCheckedAt'))
    day_text(row['missingSince'])
    require(type(row['checks']) is int and 1 <= row['checks'] <= 9999)
    require(day_text(row['lastCheckDate']) >= row['missingSince'])
    require(row['hiatusUrl'] is None or members.STATUS_URL.fullmatch(row['hiatusUrl']) is not None)
    require(row['hiatusCheckedAt'] is None or timestamp(row['hiatusCheckedAt']))


def validate_change(row):
    require(isinstance(row, dict) and row.get('kind') in ('graduation', 'addition', 'departure', 'reinstatement'))
    if row['kind'] == 'departure':
        keys(row, ('at', 'runId', 'kind', 'memberId', 'name', 'lastServiceDate', 'effectiveFrom', 'evidenceUrl'))
        require(day_text(row['lastServiceDate']) < day_text(row['effectiveFrom']) and row['evidenceUrl'] == STAFF_URL)
    elif row['kind'] == 'reinstatement':
        keys(row, ('at', 'runId', 'kind', 'memberId', 'name', 'evidenceUrl'))
        require(isinstance(row['evidenceUrl'], str) and STAFF_DETAIL.fullmatch(row['evidenceUrl']))
    elif row['kind'] == 'addition':
        keys(row, ('at', 'runId', 'kind', 'memberId', 'name', 'homeStore', 'xProfileUrl', 'evidenceUrl'))
        require(row['homeStore'] in ('s1', 's2', 's3', 's4'))
        profile_or_none(row['xProfileUrl'])
        require(isinstance(row['evidenceUrl'], str) and STAFF_DETAIL.fullmatch(row['evidenceUrl']))
    else:
        keys(row, ('at', 'runId', 'kind', 'memberId', 'name', 'lastServiceDate', 'effectiveFrom',
                   'evidenceUrl', 'postId'))
        last = dt.date.fromisoformat(day_text(row['lastServiceDate']))
        require(day_text(row['effectiveFrom']) == (last + dt.timedelta(days=1)).isoformat())
        require(members.STATUS_URL.fullmatch(row['evidenceUrl'] or '') is not None
                and POST_ID.fullmatch(row['postId'] or '') is not None)
    timestamp(row['at'])
    require(RUN_ID.fullmatch(row['runId'] or '') is not None)
    require(isinstance(row['memberId'], str) and members.MEMBER_ID.fullmatch(row['memberId']))
    members.valid_name(row['name'])


def validate_state(value):
    try:
        # additions/staff arrived after the first graduation-only saves; both stay optional.
        keys(value, ('schemaVersion', 'graduations', 'changes', 'scan', 'posts', 'lastRun'),
             ('additions', 'staff', 'departures', 'absences'))
        require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1)
        require(isinstance(value['graduations'], list) and isinstance(value['changes'], list))
        seen = set()
        for row in value['graduations']:
            validate_graduation(row)
            require(row['postId'] not in seen)
            seen.add(row['postId'])
        require(isinstance(value.get('additions', []), list))
        added = set()
        for row in value.get('additions', []):
            validate_addition(row)
            require(row['memberId'] not in added)
            added.add(row['memberId'])
        staff = value.get('staff')
        require(isinstance(value.get('departures', []), list) and isinstance(value.get('absences', {}), dict))
        departed = set()
        for row in value.get('departures', []):
            validate_departure(row)
            require(row['memberId'] not in departed or row['reinstatedAt'] is not None)
            if row['reinstatedAt'] is None:
                departed.add(row['memberId'])
        for member_id, row in value.get('absences', {}).items():
            require(members.MEMBER_ID.fullmatch(member_id) is not None)
            validate_absence(row)
        if staff is not None:
            keys(staff, ('checkedAt', 'pageSha256', 'names'))
            timestamp(staff['checkedAt'])
            require(HEX.fullmatch(staff['pageSha256'] or '') is not None)
            require(isinstance(staff['names'], list) and len(staff['names']) <= 500
                    and len(set(staff['names'])) == len(staff['names']))
            for name in staff['names']:
                members.valid_name(name)
        require(len(value['changes']) <= HISTORY_LIMIT)
        for row in value['changes']:
            validate_change(row)
        require(isinstance(value['scan'], dict))
        for member_id, row in value['scan'].items():
            require(members.MEMBER_ID.fullmatch(member_id) is not None)
            keys(row, ('searchedAt',))
            timestamp(row['searchedAt'])
        require(isinstance(value['posts'], dict) and len(value['posts']) <= POSTS_LIMIT)
        for tid, row in value['posts'].items():
            require(POST_ID.fullmatch(tid) is not None)
            keys(row, ('memberId', 'checkedAt', 'reason', 'bodySha256'))
            require(members.MEMBER_ID.fullmatch(row['memberId'] or '') is not None)
            timestamp(row['checkedAt'])
            require(REASON.fullmatch(row['reason'] or '') is not None)
            require(row['bodySha256'] is None or HEX.fullmatch(row['bodySha256']) is not None)
        run = value['lastRun']
        if run is not None:
            keys(run, ('at', 'date', 'runs', 'status', 'reason'))
            timestamp(run['at'])
            day_text(run['date'])
            require(type(run['runs']) is int and 1 <= run['runs'] <= 99 and run['status'] in STATUSES)
            require(run['reason'] is None or REASON.fullmatch(run['reason']) is not None)
    except (KeyError, TypeError, ValueError, members.RegistryError) as exc:
        if isinstance(exc, RosterError) and str(exc) == 'invalid_roster_updates':
            raise
        raise RosterError('invalid_roster_updates') from None
    return value


def strict_json(raw):
    return members.strict_json(raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw)


def load_state(path, required=False):
    path = Path(path)
    if not path.exists():
        require(not required, 'missing_roster_updates')
        return None
    raw = members.read_bytes(path)
    return validate_state(strict_json(raw)), raw


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def save_state(path, value):
    validate_state(value)
    members.atomic_write(Path(path), json_bytes(value))


def effective_registry(base, state):
    """Committed registry with the recorded automatic updates applied (manual wins)."""
    if not state:
        return members.apply_automation(base)
    return members.apply_automation(base, state['graduations'], state.get('additions', []),
                                    state.get('departures', []))


def due(state, now):
    run = (state or {}).get('lastRun')
    today = now.astimezone(JST).date().isoformat()
    if run is None or run['date'] != today:
        return True
    return run['status'] in ('partial', 'unavailable') and run['runs'] < RUNS_PER_DAY


# --- mechanical date checks -------------------------------------------------------------

def _digits(value):
    return int(value.translate(str.maketrans('０１２３４５６７８９', '0123456789')))


def explicit_dates(text, created):
    """Calendar dates written in the text, resolved against the post's JST date.

    Month/day without a year resolve to the nearest occurrence from 14 days before the post.
    A day alone (16日（金）) resolves to the next such day within 45 days; a written weekday
    must match. Relative words such as 明日 are never dates here.
    """
    posted = created.astimezone(JST).date()
    found = set()
    for index, pattern in enumerate(DATE_PATTERNS):
        for match in pattern.finditer(text):
            groups = match.groupdict()
            try:
                day = _digits(groups['day'])
                if index == 2:
                    candidates = [posted + dt.timedelta(days=offset) for offset in range(-14, 46)]
                    candidates = [value for value in candidates if value.day == day]
                    weekday = groups.get('weekday')
                    if weekday:
                        candidates = [value for value in candidates if WEEKDAYS[value.weekday()] == weekday]
                    future = [value for value in candidates if value >= posted]
                    if future:
                        found.add(future[0])
                    continue
                month = _digits(groups['month'])
                years = [_digits(groups['year'])] if groups.get('year') else [posted.year - 1, posted.year,
                                                                                posted.year + 1]
                options = []
                for year in years:
                    if 1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]:
                        options.append(dt.date(year, month, day))
                options = [value for value in options
                           if posted - dt.timedelta(days=14) <= value <= posted + dt.timedelta(days=200)]
                if options:
                    found.add(min(options, key=lambda value: abs((value - posted).days)))
            except (TypeError, ValueError):
                continue
    return found


def hint_text(entry):
    text = entry.get('displayText')
    if not isinstance(text, str):
        return ''
    return text.replace('\tSTART\t', '').replace('\tEND\t', '')


# --- HTTP through the shared source ledger ----------------------------------------------

def search_url(handle):
    require(HANDLE.fullmatch(handle or '') is not None, 'invalid_search_account')
    return 'https://' + SEARCH_HOST + '/realtime/search?' + urllib.parse.urlencode(
        {'p': f'id:{handle} {KEYWORD}', 'ei': 'UTF-8'})


def post_url(tid):
    require(POST_ID.fullmatch(tid or '') is not None, 'invalid_post_id')
    return f'https://{POST_HOST}/tweet-result?id={tid}&lang=ja&token=a'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Stop('redirect_refused', 'unavailable')


class SourceClient:
    """One GET per durable receipt; a denial stops this run and cools the host down."""

    def __init__(self, source, *, clock, opener=None):
        self.source, self.clock = source, clock
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.requests = {'searches': 0, 'posts': 0}

    def call(self, method, *args, **kwargs):
        try:
            return getattr(self.source, method)(*args, **kwargs)
        except self.source.failure_type as exc:
            # Another component's host stop is already alerted there; this scan just waits.
            raise Stop(exc.reason if REASON.fullmatch(str(exc.reason)) else 'source_failure') from None

    def get(self, kind, url, limit):
        if self.requests[kind] >= limit:
            raise Stop('source_budget')
        host = urllib.parse.urlsplit(url).hostname
        self.call('check', kind)
        receipt = self.call('reserve', kind, url)
        self.requests[kind] += 1
        finished = False
        status = None
        try:
            self.call('issued', receipt)
            with self.opener.open(urllib.request.Request(url), timeout=35) as response:
                status = response.getcode()
                if status != 200:
                    raise Stop('unexpected_http_status')
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    raise Stop('response_too_large')
            self.call('finish', receipt, http_status=status, content_hash=digest(body))
            finished = True
            return body
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403, 429):
                self.call('finish', receipt, 'failed', http_status=status)
                finished = True
                self.call('set_cooldown', host, self.clock() + dt.timedelta(hours=1))
                raise Stop('access_denied', 'unavailable') from None
            raise Stop('http_error') from None
        except (OSError, http.client.HTTPException):
            raise Stop('network_error') from None
        finally:
            if not finished:
                try:
                    self.source.finish(receipt, 'failed', http_status=status)
                except self.source.failure_type:
                    pass


# --- text analysis through the shared AI ledger -----------------------------------------

def numbered_lines(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()][:64]
    return [{'id': index + 1, 'text': line[:400]} for index, line in enumerate(lines)]


class InvalidOutput(Exception):
    reason = 'azure_invalid_output'


class GraduationAnalyzer:
    """One text request per post body, ever; receipts are never retried."""

    def __init__(self, usage, environment, *, transport=None, opener=None):
        transport = transport or _module('roster_azure_transport', 'azure-openai.py')
        self.transport, self.usage = transport, usage
        self.client = transport.AzureOpenAI(environment, on_http_failure=usage.http_failure,
                                            usage=usage, profile_name='text', opener=opener)
        self.version = digest(json.dumps([CONTRACT, PROMPT, SCHEMA, self.client.identity['model'],
                                          self.client.identity['modelVersion']], sort_keys=True))
        self.requests = 0

    def check(self):
        self.usage.check()

    def assess(self, *, name, text, created, post_id, author_id):
        lines = numbered_lines(text)
        payload = json.dumps({'author': name, 'postedAtJst': created.astimezone(JST).date().isoformat(),
                              'lines': lines}, ensure_ascii=False)
        if len(payload.encode('utf-8')) > MAX_INPUT_BYTES:
            return 'ai_input_limit', None
        messages = [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': payload}]
        key = digest(json.dumps([self.version, post_id, author_id, digest(text)]))
        budget = self.client.request_budget(messages, SCHEMA, name='roster_graduation',
                                            max_completion_tokens=MAX_OUTPUT_TOKENS)
        try:
            self.usage.reserve(key, self.client.identity, request=budget)
        except self.usage.failure_type as exc:
            if exc.reason in ('azure_budget_exhausted', 'azure_backoff', 'azure_auth_stopped', 'azure_deadline',
                              'azure_usage_locked'):
                raise Stop('ai_' + exc.reason.removeprefix('azure_'))
            return 'ai_' + exc.reason.removeprefix('azure_'), None
        self.requests += 1
        reasons = ledger.REASONS
        try:
            self.usage.issued(key)
            result = self.client.structured(messages, SCHEMA, name='roster_graduation',
                                            max_completion_tokens=MAX_OUTPUT_TOKENS)
            parsed = parse_result(result, lines)
        except (self.transport.AzureFailure, self.usage.failure_type, InvalidOutput) as exc:
            reason = exc.reason if exc.reason in reasons else 'azure_invalid_output'
            try:
                self.usage.finish(key, reason)
            except self.usage.failure_type:
                pass
            return 'ai_' + reason.removeprefix('azure_'), None
        self.usage.finish(key, 'events' if parsed['kind'] == 'self_graduation' else 'no_event')
        return 'analyzed', parsed


def parse_result(result, lines):
    if not isinstance(result, dict) or set(result) != {'kind', 'lastServiceDate', 'evidenceLineIds'}:
        raise InvalidOutput()
    if result['kind'] not in ('self_graduation', 'other_graduation', 'event_or_plan', 'unrelated'):
        raise InvalidOutput()
    date = result['lastServiceDate']
    if date is not None:
        if (not isinstance(date, dict) or set(date) != {'month', 'day'}
                or any(type(date[key]) is not int for key in ('month', 'day'))):
            raise InvalidOutput()
    ids = result['evidenceLineIds']
    known = {line['id'] for line in lines}
    if not isinstance(ids, list) or any(type(item) is not int or item not in known for item in ids):
        raise InvalidOutput()
    return {'kind': result['kind'], 'date': None if date is None else (date['month'], date['day']),
            'lines': [line['text'] for line in lines if line['id'] in ids]}


# --- detection -------------------------------------------------------------------------

def adopt(parsed, text, created):
    """The model's answer counts only when its date is written in the post itself."""
    if parsed is None or parsed['kind'] != 'self_graduation':
        return None, 'not_self_graduation' if parsed else 'not_analyzed'
    if parsed['date'] is None:
        return None, 'no_explicit_date'
    dates = explicit_dates(text, created)
    evidence = '\n'.join(parsed['lines'])
    matches = sorted(value for value in dates if (value.month, value.day) == parsed['date'])
    if not matches:
        return None, 'date_not_in_text'
    if KEYWORD not in evidence and KEYWORD not in text:
        return None, 'no_keyword'
    return matches[0], 'adopted'


def candidate_entries(document, handle, binding, now):
    page = yahoo.search_page(document)
    error = page.get('searchError') or {}
    if not isinstance(error, dict) or error.get('errorType') not in (None, '', 'zeromatch'):
        raise Stop('search_error')
    result = []
    for entry in yahoo.candidate_entries(page):
        if not isinstance(entry, dict) or not isinstance(entry.get('screenName'), str):
            continue
        if entry['screenName'].casefold() != handle.casefold():
            continue
        tid, uid = str(entry.get('id', '')), str(entry.get('userId', ''))
        if not POST_ID.fullmatch(tid) or not POST_ID.fullmatch(uid) or uid == OFFICIAL_AUTHOR_ID:
            continue
        if binding and binding['authorId'] != uid:
            continue
        mentions = entry.get('replyMentions') or []
        if not isinstance(mentions, list) or any(
                not isinstance(item, str) or item.casefold() != handle.casefold() for item in mentions):
            continue
        epoch = entry.get('createdAt')
        if isinstance(epoch, bool) or not re.fullmatch(r'\d{10}', str(epoch)):
            continue
        created = dt.datetime.fromtimestamp(int(epoch), UTC)
        if not now - dt.timedelta(days=LOOKBACK_DAYS) <= created <= now:
            continue
        text = hint_text(entry)
        if KEYWORD not in text or not explicit_dates(text, created):
            continue
        result.append({'id': tid, 'userId': uid, 'createdAt': created})
    return sorted(result, key=lambda row: row['createdAt'], reverse=True)


def verified_text(payload, candidate, handle):
    """Top-level text of her own post; reposts and replies to others are refused."""
    if not isinstance(payload, dict):
        return None, 'invalid_post_json'
    supplied = [payload[key] for key in ('id_str', 'id') if key in payload]
    if not supplied or any(str(item) != candidate['id'] for item in supplied):
        return None, 'response_id_mismatch'
    user = payload.get('user')
    if (not isinstance(user, dict) or str(user.get('id_str')) != candidate['userId']
            or not isinstance(user.get('screen_name'), str) or user['screen_name'].casefold() != handle.casefold()):
        return None, 'author_mismatch'
    if payload.get('retweeted_status') or payload.get('retweeted_tweet'):
        return None, 'repost'
    reply = payload.get('in_reply_to_status_id_str')
    if reply is not None:
        parent = payload.get('parent') if isinstance(payload.get('parent'), dict) else {}
        parent_user = parent.get('user') if isinstance(parent.get('user'), dict) else {}
        to_user = payload.get('in_reply_to_user_id_str') or parent_user.get('id_str')
        if str(to_user) != candidate['userId']:
            return None, 'reply_to_other'
    try:
        created = dt.datetime.fromisoformat(str(payload.get('created_at')).replace('Z', '+00:00'))
    except ValueError:
        return None, 'invalid_created_at'
    if created.utcoffset() is None or abs((created - candidate['createdAt']).total_seconds()) >= 2:
        return None, 'timestamp_mismatch'
    text = payload.get('text')
    if not isinstance(text, str) or KEYWORD not in text:
        return None, 'no_keyword'
    return text, 'ok'


def remember_post(state, tid, member_id, reason, now, body_hash=None):
    state['posts'][tid] = {'memberId': member_id, 'checkedAt': stamp(now), 'reason': reason,
                           'bodySha256': body_hash}
    if len(state['posts']) > POSTS_LIMIT:
        for old in sorted(state['posts'], key=lambda key: state['posts'][key]['checkedAt'])[
                :len(state['posts']) - POSTS_LIMIT]:
            del state['posts'][old]


def scan_targets(registry, state, now):
    rows = []
    for member in registry['members']:
        if (not members.active_on(member, now) or member['xProfileUrl'] is None
                or members.rejected_graduation(registry, member['memberId'], None)
                or any(row['memberId'] == member['memberId'] for row in state['graduations'])):
            continue
        searched = state['scan'].get(member['memberId'], {}).get('searchedAt', '')
        rows.append((searched, member))
    return [member for _, member in sorted(rows, key=lambda row: row[0])]


def collect_graduations(state, base, bindings, client, analyzer, *, now, run_id, clock, deadline):
    """Search a few members a day; record self-announced last service days."""
    registry, _, _ = effective_registry(base, state)
    changes, counts = [], {'scanned': 0, 'candidates': 0}
    queue = []
    for member in scan_targets(registry, state, now):
        if client.requests['searches'] >= MAX_SEARCHES or clock() >= deadline:
            break
        handle = members.profile_handle(member['xProfileUrl'])
        binding = bindings.get(member['canonicalName'])
        document = client.get('searches', search_url(handle), MAX_SEARCHES)
        state['scan'][member['memberId']] = {'searchedAt': stamp(clock())}
        counts['scanned'] += 1
        try:
            entries = candidate_entries(document.decode('utf-8', 'strict'), handle, binding, now)
        except (UnicodeError, ValueError):
            raise Stop('invalid_search_response')
        for entry in entries:
            if entry['id'] in state['posts'] or members.rejected_graduation(
                    registry, member['memberId'], entry['id']):
                continue
            queue.append((member, handle, entry))
    counts['candidates'] = len(queue)
    for member, handle, entry in queue:
        if client.requests['posts'] >= MAX_POSTS or clock() >= deadline:
            break
        if analyzer is None:
            raise Stop('ai_unavailable')
        if analyzer.requests >= MAX_ANALYSIS:
            break
        analyzer.check_or_stop()
        body = client.get('posts', post_url(entry['id']), MAX_POSTS)
        try:
            payload = json.loads(body.decode('utf-8', 'strict'))
        except (UnicodeError, ValueError, RecursionError):
            remember_post(state, entry['id'], member['memberId'], 'invalid_post_json', clock())
            continue
        text, reason = verified_text(payload, entry, handle)
        if text is None:
            remember_post(state, entry['id'], member['memberId'], reason, clock())
            continue
        body_hash = digest(text)
        if not explicit_dates(text, entry['createdAt']):
            remember_post(state, entry['id'], member['memberId'], 'no_explicit_date', clock(), body_hash)
            continue
        reason, parsed = analyzer.assess(name=member['canonicalName'], text=text, created=entry['createdAt'],
                                         post_id=entry['id'], author_id=entry['userId'])
        if reason != 'analyzed':
            remember_post(state, entry['id'], member['memberId'], reason, clock(), body_hash)
            continue
        last, reason = adopt(parsed, text, entry['createdAt'])
        remember_post(state, entry['id'], member['memberId'], reason, clock(), body_hash)
        if last is None or any(row['memberId'] == member['memberId'] for row in state['graduations']):
            continue
        detected = clock()
        url = status_url(handle, entry['id'])
        state['graduations'].append({
            'memberId': member['memberId'], 'name': member['canonicalName'],
            'lastServiceDate': last.isoformat(), 'evidenceUrl': url, 'postId': entry['id'],
            'authorId': entry['userId'], 'bodySha256': body_hash, 'postCreatedAt': stamp(entry['createdAt']),
            'detectedAt': stamp(detected), 'runId': run_id})
        change = {'at': stamp(detected), 'runId': run_id, 'kind': 'graduation', 'memberId': member['memberId'],
                  'name': member['canonicalName'], 'lastServiceDate': last.isoformat(),
                  'effectiveFrom': (last + dt.timedelta(days=1)).isoformat(), 'evidenceUrl': url,
                  'postId': entry['id']}
        state['changes'].append(change)
        del state['changes'][:-HISTORY_LIMIT]
        changes.append(change)
    return changes, counts


class StaffUnavailable(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class StaffClient:
    """Plain GETs to the official site only: the staff list and introduction pages."""

    def __init__(self, *, opener=None, sleep=time.sleep):
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.sleep = sleep
        self.requests = 0

    def get(self, url):
        require(url == STAFF_URL or STAFF_DETAIL.fullmatch(url) is not None, 'staff_route_refused')
        if self.requests:
            self.sleep(2)
        self.requests += 1
        try:
            with self.opener.open(urllib.request.Request(url), timeout=45) as response:
                if response.getcode() != 200:
                    raise StaffUnavailable('staff_page_unavailable')
                body = response.read(MAX_STAFF_BYTES + 1)
        except (urllib.error.URLError, OSError, http.client.HTTPException, Stop):
            raise StaffUnavailable('staff_page_unavailable') from None
        if len(body) > MAX_STAFF_BYTES:
            raise StaffUnavailable('staff_page_structure_changed')
        return body.decode('utf-8', 'replace')


def introduction_profile(document, name):
    """Her X profile from her own introduction page, only when the page is hers."""
    heading = re.search(r'<div id="staffDetail"[^>]*>\s*<h3>([^<]{1,40})</h3>', document)
    if heading is None or html_module.unescape(heading.group(1)).strip() != name:
        return None
    match = re.search(r'<em>\s*(?:Twitter|X|Ｘ)\s*</em>\s*<p>\s*<a href="(https://(?:twitter|x)\.com/'
                      r'[A-Za-z0-9_]{1,15}/?)"', document)
    if match is None:
        return None
    try:
        return members.normalize_profile_url(match.group(1))
    except members.RegistryError:
        return None


def check_staff(state, base, client, *, now, run_id, clock):
    """Record names newly listed on the official staff page as new members."""
    registry, _, _ = effective_registry(base, state)
    document = client.get(STAFF_URL)
    if 'id="maidList"' not in document:
        raise StaffUnavailable('staff_page_structure_changed')
    entries = site.parse_site_entries(document)
    previous = state.get('staff')
    try:
        for entry in entries:
            members.valid_name(entry['name'])
    except members.RegistryError:
        raise StaffUnavailable('staff_page_structure_changed') from None
    if len(entries) < MIN_STAFF_ENTRIES or previous and len(entries) < len(previous['names']) // 2:
        raise StaffUnavailable('staff_page_structure_changed')
    listed_before = set(previous['names']) if previous else None
    known = {name for member in registry['members'] for name in members.names_of(member)}
    known |= {row['name'] for row in registry['unresolvedNames']}
    handles = {members.profile_handle(m['xProfileUrl']) for m in registry['members'] if m['xProfileUrl']}
    handles |= set(registry['reservedHandles'])
    handles |= {members.profile_handle(account['profileUrl'])
                for row in registry['unresolvedNames'] for account in row['legacyAccounts']}
    changes, notes = [], []
    pages = {}
    departed = {row['memberId']: row for row in state.get('departures', []) if row['reinstatedAt'] is None}
    for entry in entries:
        name = entry['name']
        if any(name in members.names_of(m) and not members.retired_before(m, now) for m in registry['members']):
            continue
        # A name still listed since the previous check is the same person (for example a
        # graduate the page has not removed yet); only a newly listed or never-known name is new.
        if name in known and (listed_before is None or name in listed_before):
            continue
        detail = entry['href'] if isinstance(entry['href'], str) and STAFF_DETAIL.fullmatch(entry['href']) else None
        returning = [departed[m['memberId']] for m in registry['members']
                     if m['memberId'] in departed and name in members.names_of(m)]
        if returning:
            # Listed again after an inferred departure: the same X account means the same person.
            if detail is None:
                raise StaffUnavailable('staff_page_structure_changed')
            pages[detail] = pages.get(detail) or client.get(detail)
            listed_profile = introduction_profile(pages[detail], name)
            same = next((row for row in returning if row['xProfileUrl'] and row['xProfileUrl'] == listed_profile),
                        None)
            if same is not None:
                at = clock()
                same.update(reinstatedAt=stamp(at), reinstateUrl=detail)
                change = {'at': stamp(at), 'runId': run_id, 'kind': 'reinstatement', 'memberId': same['memberId'],
                          'name': name, 'evidenceUrl': detail}
                state['changes'].append(change)
                del state['changes'][:-HISTORY_LIMIT]
                changes.append(change)
                state.get('absences', {}).pop(same['memberId'], None)
                continue
            if listed_profile is None:
                notes.append({'name': name, 'status': 'relisted_unverified'})
                continue
        if members.rejected_addition(registry, name) or len(changes) >= MAX_ADDITIONS:
            continue
        if detail is None:
            raise StaffUnavailable('staff_page_structure_changed')
        page = pages.get(detail) or client.get(detail)
        profile = introduction_profile(page, name)
        if profile is not None and members.profile_handle(profile) in handles:
            profile = None
        detected = clock()
        member_id = 'm-' + uuid.uuid5(uuid.NAMESPACE_URL,
                                      f'akibazettai:official-site:{name}:{stamp(detected)}').hex
        addition = {'memberId': member_id, 'name': name, 'homeStore': entry['store'], 'xProfileUrl': profile,
                    'detailUrl': detail, 'pageSha256': digest(page), 'detectedAt': stamp(detected),
                    'runId': run_id}
        state['additions'].append(addition)
        change = {'at': stamp(detected), 'runId': run_id, 'kind': 'addition', 'memberId': member_id, 'name': name,
                  'homeStore': entry['store'], 'xProfileUrl': profile, 'evidenceUrl': detail}
        state['changes'].append(change)
        del state['changes'][:-HISTORY_LIMIT]
        changes.append(change)
        if profile is not None:
            handles.add(members.profile_handle(profile))
        known.add(name)
    track_absences(state, registry, {entry['name'] for entry in entries}, previous, now)
    state['staff'] = {'checkedAt': stamp(clock()), 'pageSha256': digest(document),
                      'names': [entry['name'] for entry in entries]}
    return changes, len(entries), notes


def track_absences(state, registry, listed, previous, now):
    """Count healthy daily checks on which a once-listed current member is missing."""
    today = now.astimezone(JST).date().isoformat()
    absences = state.setdefault('absences', {})
    listed_before = set(previous['names']) if previous else set()
    current = set()
    for member in registry['members']:
        names = members.names_of(member)
        if not members.active_on(member, now) or names & listed:
            continue
        if member['officialListing'] != 'listed' and not names & listed_before:
            continue  # never on the page: nothing has disappeared
        current.add(member['memberId'])
        row = absences.get(member['memberId']) or {
            'missingSince': today, 'checks': 0, 'lastCheckDate': today, 'hiatusUrl': None, 'hiatusCheckedAt': None}
        if row['checks'] == 0 or row['lastCheckDate'] != today:
            row['checks'] += 1
            row['lastCheckDate'] = today
        absences[member['memberId']] = row
    for member_id in list(absences):
        if member_id not in current:
            del absences[member_id]  # listed again, departed or no longer current: start over


def read_window_value(path, key):
    """A `window.KEY = ...` data file; JSON first, then node in an isolated context."""
    text = Path(path).read_text(encoding='utf-8-sig')
    prefix = re.search(r'window\.' + re.escape(key) + r'\s*=\s*', text)
    if not prefix:
        raise ValueError('missing_input_assignment')
    try:
        return json.loads(text[prefix.end():].strip().removesuffix(';'))
    except ValueError:
        pass
    import shutil
    import subprocess
    script = ("const fs=require('node:fs'),vm=require('node:vm');const input=JSON.parse(fs.readFileSync(0,'utf8'));"
              "const context=vm.createContext({window:Object.create(null)},{codeGeneration:{strings:false,wasm:false}});"
              "vm.runInContext(input.text,context,{timeout:750});"
              "process.stdout.write(vm.runInContext('JSON.stringify(window.'+input.key+')',context,{timeout:750}));")
    node = shutil.which('node')
    if node is None:
        raise ValueError('node_unavailable')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run([node, '-e', script], input=json.dumps({'key': key, 'text': text}),
                            capture_output=True, encoding='utf-8', timeout=8, check=True,
                            env={k: v for k, v in os.environ.items() if not k.upper().startswith('AZURE_OPENAI_')},
                            **options)
    return json.loads(result.stdout)


WORK_KINDS = ('placement', 'late', 'return')


def load_activity(*, observed=None, curated=None, personal=None, half_month=None, schedule=None):
    """Dated mentions per name: confirmed work (official, curated, her own work posts) and
    anything else that shows she is still scheduled (plans, absence notices)."""
    rows, unreadable = [], []

    def add(name, day, kind, confirmed):
        if isinstance(name, str) and isinstance(day, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            rows.append({'name': name, 'date': day, 'kind': kind, 'confirmed': confirmed})

    def source(label, path, read):
        if path is None or not Path(path).is_file():
            return
        try:
            read(path)
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError, AttributeError):
            unreadable.append(label)
        except Exception:  # node or subprocess failures stay contained as unreadable input
            unreadable.append(label)

    def read_observed(path):
        for post in strict_json(members.read_bytes(path)).get('posts', []):
            for name in post.get('names', []):
                add(name, post.get('date'), 'official', True)
            for notice in post.get('notices', []) or []:
                add(notice.get('name'), post.get('date'), 'official', False)

    def read_curated(path):
        import csv
        with Path(path).open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                add(row.get('maid'), row.get('date'), 'curated', True)

    def read_personal(path):
        for post in strict_json(members.read_bytes(path)).get('posts', []):
            work = (any(event.get('kind') in WORK_KINDS for event in post.get('events', []) or [])
                    or any(link.get('status') == 'work' for link in post.get('links', []) or []))
            add(post.get('name'), post.get('date'), 'personal', work)

    def read_half_month(path):
        value = strict_json(members.read_bytes(path))
        for item in [*value.get('schedules', []), *value.get('partialSchedules', [])]:
            for day in item.get('days', []):
                add(item.get('name'), day.get('date'), 'plan', False)

    def read_schedule(path):
        for day, shifts in (read_window_value(path, 'SCHEDULE_DATA').get('schedule') or {}).items():
            for entries in shifts.values():
                for entry in entries:
                    add(entry.get('name'), day, 'plan', False)

    source('observed', observed, read_observed)
    source('curated', curated, read_curated)
    source('personal', personal, read_personal)
    source('half_month', half_month, read_half_month)
    source('schedule', schedule, read_schedule)
    return {'rows': rows, 'unreadable': unreadable}


def hiatus_notice(client, member, since):
    """Her own or the official account's rest/hiatus post since shortly before she went missing."""
    name = member['canonicalName']
    queries = []
    handle = members.profile_handle(member['xProfileUrl']) if member['xProfileUrl'] else None
    if handle:
        queries += [(handle, f'id:{handle} 休止'), (handle, f'id:{handle} お休み')]
    queries.append((members.OFFICIAL_HANDLE, f'id:{members.OFFICIAL_HANDLE} {name} 休止'))
    for author, query in queries:
        url = 'https://' + SEARCH_HOST + '/realtime/search?' + urllib.parse.urlencode({'p': query, 'ei': 'UTF-8'})
        document = client.get('searches', url, MAX_SEARCHES + len(queries))
        try:
            page = yahoo.search_page(document.decode('utf-8', 'strict'))
            found = yahoo.candidate_entries(page)
        except (UnicodeError, ValueError):
            raise Stop('invalid_search_response')
        for entry in found:
            if not isinstance(entry, dict) or str(entry.get('screenName', '')).casefold() != author.casefold():
                continue
            tid, epoch = str(entry.get('id', '')), str(entry.get('createdAt', ''))
            if not POST_ID.fullmatch(tid) or not re.fullmatch(r'\d{10}', epoch):
                continue
            if dt.datetime.fromtimestamp(int(epoch), UTC) < since:
                continue
            text = hint_text(entry)
            if any(word in text for word in HIATUS_WORDS) and (author != members.OFFICIAL_HANDLE or name in text):
                return status_url(str(entry['screenName']), tid)
    return None


def evaluate_departures(state, base, client, activity, *, now, run_id, clock):
    """Adopt a departure for a member missing from the page with nothing showing her since."""
    registry, _, _ = effective_registry(base, state)
    today = now.astimezone(JST).date()
    by_id = {member['memberId']: member for member in registry['members']}
    changes, rows = [], []
    for member_id, absence in sorted(state.get('absences', {}).items(), key=lambda item: item[1]['missingSince']):
        member = by_id.get(member_id)
        if member is None or not members.active_on(member, now):
            continue
        names = members.names_of(member)
        mine = [row for row in activity['rows'] if row['name'] in names]
        later = sorted(row['date'] for row in mine if row['date'] >= absence['missingSince'])
        row = {'name': member['canonicalName'], 'checks': absence['checks'], 'missingSince': absence['missingSince'],
               'lastActivity': later[-1] if later else None, 'status': 'missing'}
        rows.append(row)
        if absence['checks'] < MISSING_CHECKS:
            continue
        if later:
            row['status'] = 'active_after_missing'
            continue
        if activity['unreadable']:
            row['status'] = 'activity_unreadable'
            continue
        if members.rejected_departure(registry, member_id):
            row['status'] = 'rejected_by_override'
            continue
        recent = absence['hiatusCheckedAt'] and timestamp(absence['hiatusCheckedAt']) > now - dt.timedelta(
            days=HIATUS_RECHECK_DAYS)
        if not recent:
            since = dt.datetime.combine(dt.date.fromisoformat(absence['missingSince']), dt.time(), JST) \
                - dt.timedelta(days=HIATUS_LOOKBACK_DAYS)
            absence['hiatusUrl'] = hiatus_notice(client, member, since)
            absence['hiatusCheckedAt'] = stamp(clock())
        if absence['hiatusUrl']:
            row['status'] = 'hiatus_notice'
            continue
        confirmed = sorted((row_['date'], row_['kind']) for row_ in mine
                           if row_['confirmed'] and row_['date'] < today.isoformat())
        last, kind = confirmed[-1] if confirmed else ((today - dt.timedelta(days=1)).isoformat(), 'none')
        detected = clock()
        state.setdefault('departures', []).append({
            'memberId': member_id, 'name': member['canonicalName'], 'lastServiceDate': last,
            'lastServiceSource': kind, 'offRosterFrom': today.isoformat(), 'missingSince': absence['missingSince'],
            'checks': absence['checks'], 'xProfileUrl': member['xProfileUrl'], 'detectedAt': stamp(detected),
            'runId': run_id, 'reinstatedAt': None, 'reinstateUrl': None})
        change = {'at': stamp(detected), 'runId': run_id, 'kind': 'departure', 'memberId': member_id,
                  'name': member['canonicalName'], 'lastServiceDate': last, 'effectiveFrom': today.isoformat(),
                  'evidenceUrl': STAFF_URL}
        state['changes'].append(change)
        del state['changes'][:-HISTORY_LIMIT]
        changes.append(change)
        row['status'] = 'departed'
        state['absences'].pop(member_id, None)
    return changes, rows


class AnalyzerGate:
    """Adds the non-reserving ledger preflight used before any post GET."""

    def __init__(self, analyzer):
        self.analyzer = analyzer

    @property
    def requests(self):
        return self.analyzer.requests

    def check_or_stop(self):
        try:
            self.analyzer.check()
        except self.analyzer.usage.failure_type as exc:
            raise Stop('ai_' + str(exc.reason).removeprefix('azure_')) from None

    def assess(self, **kwargs):
        return self.analyzer.assess(**kwargs)


def read_bindings(*paths):
    bindings = {}
    for path in paths:
        if path is None or not Path(path).is_file():
            continue
        value = strict_json(members.read_bytes(path))
        for name, bound in (value.get('identityBindings') or {}).items():
            if isinstance(bound, dict) and POST_ID.fullmatch(str(bound.get('authorId', ''))):
                bindings.setdefault(name, {'authorId': str(bound['authorId'])})
    return bindings


def run(args, *, clock=lambda: dt.datetime.now(UTC), sleep=time.sleep, environment=None,
        source_factory=None, usage_factory=None, analyzer_factory=None, opener=None, staff_opener=None,
        staff_check=True):
    environment = os.environ if environment is None else environment
    require(RUN_ID.fullmatch(args.run_id or '') is not None, 'invalid_roster_run_id')
    started = clock()
    loaded = load_state(args.state)
    state = loaded[0] if loaded else empty_state()
    state.setdefault('additions', [])
    state.setdefault('staff', None)
    state.setdefault('departures', [])
    state.setdefault('absences', {})
    base = members.load_registry(args.members)
    bindings = read_bindings(args.personal_state, args.half_month_state)
    activity = load_activity(observed=getattr(args, 'observed', None), curated=getattr(args, 'curated', None),
                             personal=args.personal_state, half_month=args.half_month_state,
                             schedule=getattr(args, 'schedule', None))
    report = {'component': 'roster', 'status': 'no-new', 'reason': None, 'changes': [],
              'requests': {'searches': 0, 'posts': 0, 'analysis': 0}, 'scanned': 0, 'candidates': 0,
              'staff': {'status': 'ok', 'reason': None, 'listed': 0, 'requests': 0}, 'missing': [],
              'notes': []}
    # The official staff page first: a failure there changes nothing and is alerted, while
    # the graduation scan still runs.
    staff = StaffClient(opener=staff_opener, sleep=sleep)
    try:
        if staff_check:
            _, report['staff']['listed'], report['notes'] = check_staff(
                state, base, staff, now=started, run_id=args.run_id, clock=clock)
        else:
            report['staff']['status'] = 'skipped'
    except StaffUnavailable as exc:
        report['staff'].update(status='unavailable', reason=exc.reason)
    report['staff']['requests'] = staff.requests
    source_factory = source_factory or (lambda: source_state.SharedSource(
        args.source_state, run_id=args.run_id, component='roster', clock=clock, sleep=sleep))
    usage_factory = usage_factory or (lambda: ledger.SharedUsage(
        args.ai_state, run_id=args.run_id, component='personal', clock=clock, sleep=sleep,
        request_limit=MAX_ANALYSIS, run_limit=ledger.RUN_LIMIT))
    deadline = started + dt.timedelta(seconds=RUNTIME_SECONDS)
    client = None
    try:
        with source_factory() as source, usage_factory() as usage:
            client = SourceClient(source, clock=clock, opener=opener)
            analyzer = None
            factory = analyzer_factory or (lambda usage, environment: GraduationAnalyzer(usage, environment))
            try:
                analyzer = AnalyzerGate(factory(usage, environment))
            except ValueError:
                analyzer = None
            try:
                _, report['missing'] = evaluate_departures(state, base, client, activity, now=started,
                                                           run_id=args.run_id, clock=clock)
                changes, counts = collect_graduations(state, base, bindings, client, analyzer, now=started,
                                                      run_id=args.run_id, clock=clock, deadline=deadline)
                report.update(changes=changes, **counts)
                report['status'] = 'ok' if changes else 'no-new'
            except Stop as stop:
                report.update(status=stop.status, reason=stop.reason)
            finally:
                report['requests'].update(client.requests)
                if analyzer is not None:
                    report['requests']['analysis'] = analyzer.requests
                report['changes'] = [row for row in state['changes'] if row['runId'] == args.run_id]
    except (source_state.SourceFailure, ledger.UsageFailure) as exc:
        report.update(status='unavailable', reason=str(exc.reason) if REASON.fullmatch(str(exc.reason))
                      else 'ledger_unavailable')
    report['changes'] = [row for row in state['changes'] if row['runId'] == args.run_id]
    if report['status'] == 'no-new' and report['changes']:
        report['status'] = 'ok'
    if report['staff']['status'] == 'unavailable' and report['status'] != 'unavailable':
        report.update(status='unavailable', reason=report['staff']['reason'])
    previous = state['lastRun']
    today = started.astimezone(JST).date().isoformat()
    state['lastRun'] = {'at': stamp(clock()), 'date': today,
                        'runs': (previous['runs'] + 1 if previous and previous['date'] == today else 1),
                        'status': report['status'], 'reason': report['reason']}
    save_state(args.state, state)
    report['exitCode'] = {'partial': 2, 'unavailable': 3}.get(report['status'], 0)
    if args.report:
        members.atomic_write(Path(args.report), json_bytes(report))
    return report


# --- publication ------------------------------------------------------------------------

def publish(state_path, root, *, stamp_assets=True, base_bytes=None):
    """Write the effective registry into a checkout's data/. Never commits anything."""
    root = Path(root)
    path = root / 'data' / 'members.json'
    raw = base_bytes if base_bytes is not None else members.read_bytes(path)
    base = members.validate_registry(members.strict_json(raw.decode('utf-8-sig')))
    loaded = load_state(state_path)
    if loaded is None:
        return {'applied': [], 'skipped': [], 'changed': False}
    effective, applied, skipped = effective_registry(base, loaded[0])
    changed = effective != base
    if changed or base_bytes is not None:
        members.atomic_write(path, members.json_bytes(effective))
        members.atomic_write(path.with_suffix('.js'), members.javascript_bytes(effective))
        if stamp_assets:
            members.stamp_registry_assets(path)
    return {'applied': applied, 'skipped': skipped, 'changed': changed}


def summary_lines(applied, skipped, report=None):
    lines = ['## Automatic roster updates (collector-state, not committed to main)\n']
    for row in applied:
        if row['kind'] == 'addition':
            account = (f"X {row['xProfileUrl']}" if row['xProfileUrl']
                       else 'no X account on her introduction page, so she is not a collection target yet')
            lines.append(f"\n- Applied: new member {row['name']} ({row['homeStore']}) from the official staff page "
                         f"<{row['detailUrl']}>; {account}"
                         + (f"; account kept for review ({row['accountDropped']})" if 'accountDropped' in row else '')
                         + '.\n')
            continue
        if row['kind'] == 'departure':
            lines.append(f"\n- Applied: {row['name']} left the official staff page ({row['checks']} checks from "
                         f"{row['missingSince']}) with no activity since; off the roster from {row['offRosterFrom']}; "
                         f"last confirmed service {row['lastServiceDate']} ({row['lastServiceSource']}).\n")
            continue
        effective = (dt.date.fromisoformat(row['lastServiceDate']) + dt.timedelta(days=1)).isoformat()
        lines.append(f"\n- Applied: {row['name']} last service {row['lastServiceDate']}, off the roster from "
                     f"{effective}; <{row['evidenceUrl']}>.\n")
    for row in skipped:
        evidence = row.get('evidenceUrl') or row.get('detailUrl') or STAFF_URL
        lines.append(f"\n- Kept manual roster for {row['name']} ({row['skipReason']}); automatic evidence "
                     f"<{evidence}>.\n")
    return lines


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    collect = sub.add_parser('run')
    collect.add_argument('--state', type=Path, required=True)
    collect.add_argument('--members', type=Path, required=True, help='committed (base) members.json')
    collect.add_argument('--personal-state', type=Path)
    collect.add_argument('--half-month-state', type=Path)
    collect.add_argument('--source-state', type=Path, required=True)
    collect.add_argument('--ai-state', type=Path, required=True)
    collect.add_argument('--run-id', required=True)
    collect.add_argument('--report', type=Path)
    collect.add_argument('--observed', type=Path, help='official observed-shifts state')
    collect.add_argument('--curated', type=Path, help='tools/data/shifts.csv')
    collect.add_argument('--schedule', type=Path, help='data/schedule.js')
    apply = sub.add_parser('publish')
    apply.add_argument('--state', type=Path, required=True)
    apply.add_argument('--root', type=Path, default=ROOT)
    return parser


def main(argv=None):
    args = argument_parser().parse_args(argv)
    try:
        if args.command == 'run':
            report = run(args)
            print(json.dumps({key: report[key] for key in ('status', 'reason', 'exitCode')}))
            return report['exitCode']
        result = publish(args.state, args.root)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8', newline='\n') as target:
                target.writelines(summary_lines(result['applied'], result['skipped']))
        print(json.dumps({'applied': len(result['applied']), 'skipped': len(result['skipped']),
                          'changed': result['changed']}))
        return 0
    except (RosterError, members.RegistryError, OSError) as exc:
        reason = str(exc) if REASON.fullmatch(str(exc)) else 'roster_local_failure'
        print(json.dumps({'reason': reason, 'exceptionClass': type(exc).__name__}))
        return 4


if __name__ == '__main__':
    sys.exit(main())
