"""Automatic roster updates, recorded on collector-state and applied only at publication.

Graduations come only from the member's own announcement: her own post (a reply to
herself is fine; replies to others and reposts are not) that names her own last service
day explicitly. The text model (gpt-6-luna, low) must classify the post as her own
graduation AND the date it returns must be one of the dates found mechanically in the
body. Undated announcements, other people's graduations and graduation events are not
used. Results live in roster-updates.json on collector-state (post ID, URL, body hash);
main is never committed. data/members.json stays authoritative for manual decisions:
a manual graduation wins, and `member-registry.py reject-auto-graduation` rejects one.

Commands:
  run      collector child: search, verify and record (shared source/AI ledgers)
  publish  apply the recorded updates to data/members.json, members.js and asset stamps
"""
import argparse
import calendar
import copy
import datetime as dt
import hashlib
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


ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc
JST = dt.timezone(dt.timedelta(hours=9))
FILE = 'roster-updates.json'
SEARCH_HOST = 'search.yahoo.co.jp'
POST_HOST = 'cdn.syndication.twimg.com'
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
    return {'schemaVersion': 1, 'graduations': [], 'changes': [], 'scan': {}, 'posts': {}, 'lastRun': None}


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


def validate_change(row):
    keys(row, ('at', 'runId', 'kind', 'memberId', 'name', 'lastServiceDate', 'effectiveFrom',
               'evidenceUrl', 'postId'))
    timestamp(row['at'])
    require(RUN_ID.fullmatch(row['runId'] or '') is not None and row['kind'] == 'graduation')
    require(isinstance(row['memberId'], str) and members.MEMBER_ID.fullmatch(row['memberId']))
    members.valid_name(row['name'])
    last = dt.date.fromisoformat(day_text(row['lastServiceDate']))
    require(day_text(row['effectiveFrom']) == (last + dt.timedelta(days=1)).isoformat())
    require(members.STATUS_URL.fullmatch(row['evidenceUrl'] or '') is not None
            and POST_ID.fullmatch(row['postId'] or '') is not None)


def validate_state(value):
    try:
        keys(value, ('schemaVersion', 'graduations', 'changes', 'scan', 'posts', 'lastRun'))
        require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1)
        require(isinstance(value['graduations'], list) and isinstance(value['changes'], list))
        seen = set()
        for row in value['graduations']:
            validate_graduation(row)
            require(row['postId'] not in seen)
            seen.add(row['postId'])
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
    graduations = state['graduations'] if state else []
    return members.apply_automation(base, graduations)


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
        source_factory=None, usage_factory=None, analyzer_factory=None, opener=None):
    environment = os.environ if environment is None else environment
    require(RUN_ID.fullmatch(args.run_id or '') is not None, 'invalid_roster_run_id')
    started = clock()
    loaded = load_state(args.state)
    state = loaded[0] if loaded else empty_state()
    base = members.load_registry(args.members)
    bindings = read_bindings(args.personal_state, args.half_month_state)
    report = {'component': 'roster', 'status': 'no-new', 'reason': None, 'changes': [],
              'requests': {'searches': 0, 'posts': 0, 'analysis': 0}, 'scanned': 0, 'candidates': 0}
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
    if report:
        lines.append(f"\nGraduation scan: status={report['status']}; reason={report['reason'] or 'none'}; "
                     f"members searched={report['scanned']}; candidates={report['candidates']}; "
                     f"search/post/AI={report['requests']['searches']}/{report['requests']['posts']}/"
                     f"{report['requests']['analysis']}.\n")
        for row in report['changes']:
            lines.append(f"\n- New: {row['name']} graduates after {row['lastServiceDate']}; removed from the "
                         f"roster and targets from {row['effectiveFrom']}; evidence <{row['evidenceUrl']}>.\n")
    for row in applied:
        effective = (dt.date.fromisoformat(row['lastServiceDate']) + dt.timedelta(days=1)).isoformat()
        lines.append(f"\n- Applied: {row['name']} last service {row['lastServiceDate']}, off the roster from "
                     f"{effective}; <{row['evidenceUrl']}>.\n")
    for row in skipped:
        lines.append(f"\n- Kept manual roster for {row['name']} ({row['skipReason']}); automatic evidence "
                     f"<{row['evidenceUrl']}>.\n")
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
