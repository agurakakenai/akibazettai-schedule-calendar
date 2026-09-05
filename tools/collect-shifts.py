"""Collect independently observed public shift facts; never change curated data.

Only two Yahoo realtime keyword pages and the existing public post fetcher are
used. Default dates are the last two JST service days (a day starts at 05:00).
lastSuccessAt means a completed run with both searches and all selected posts
successfully handled, including no-new/no-results; partial runs do not advance it.
Exit codes: 0=ok/no-new/no-results, 2=partial, 3=unavailable, 4=local/lock error.
"""
import argparse
from contextlib import ExitStack
import copy
import csv
import datetime as dt
import email.utils
import html
from html.parser import HTMLParser
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'data' / 'observed-shifts.json'
CURATED = ROOT / 'tools' / 'data' / 'shifts.csv'
UTC = dt.timezone.utc
JST = dt.timezone(dt.timedelta(hours=9))
AUTHOR_ID = '822429861218131969'
AUTHOR = 'akibazettai'
QUERIES = ('アキバ絶対領域', 'ひるにゃんこ')
SEARCH_URLS = tuple(
    'https://search.yahoo.co.jp/realtime/search?'
    + urllib.parse.urlencode({'p': query, 'ei': 'UTF-8'})
    for query in QUERIES)
POST_HOST = 'cdn.syndication.twimg.com'
ID_RE = re.compile(r'[1-9][0-9]{9,24}\Z')
URL_RE = re.compile(
    r'https?://(?:www\.)?(?:x\.com|twitter\.com)/'
    r'akibazettai/status/([1-9][0-9]{9,24})(?=$|[/?#\s"\'<>&\\])',
    re.IGNORECASE)
MAX_POSTS = 20
MAX_BODY = 8 * 1024 * 1024
MAX_CACHE_SECONDS = 3600
STORE_IDS = {'1号店 アキバ絶対領域': 's1', '2号店 A.D.1912': 's2',
             '3号店 +e': 's3', '4号店 A.D.2045': 's4'}
STATUSES = {'never', 'ok', 'partial', 'unavailable', 'no-new', 'no-results'}


def load_importer():
    spec = importlib.util.spec_from_file_location(
        'observed_shift_importer', ROOT / 'tools' / 'add-shifts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IMPORTER = load_importer()


def utc_now():
    return dt.datetime.now(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('invalid_timestamp')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        result = dt.datetime.strptime(value, '%a %b %d %H:%M:%S %z %Y')
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timezone_required')
    return result.astimezone(UTC)


def service_day(value):
    return (value.astimezone(JST) - dt.timedelta(hours=5)).date()


def date_range(args, now):
    end = dt.date.fromisoformat(args.date_to) if args.date_to else service_day(now)
    start = (dt.date.fromisoformat(args.date_from) if args.date_from
             else end - dt.timedelta(days=args.days - 1))
    if start > end:
        raise ValueError('invalid_date_range')
    return start, end


def post_id(value):
    # Never accept floats, even when their rounded decimal happens to look valid.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if ID_RE.fullmatch(value) else None


def canonical(tid):
    return 'https://x.com/akibazettai/status/' + tid


def snowflake_time(tid):
    return dt.datetime(1970, 1, 1, tzinfo=UTC) + dt.timedelta(
        milliseconds=(int(tid) >> 22) + 1288834974657)


def unescape_urls(value):
    for _ in range(3):
        decoded = html.unescape(value).replace('\\/', '/')
        decoded = re.sub(r'\\u([0-9a-fA-F]{4})',
                         lambda match: chr(int(match.group(1), 16)), decoded)
        if decoded == value:
            break
        value = decoded
    return value


def urls_in(value):
    return {match.group(1) for match in URL_RE.finditer(unescape_urls(value))}


def quoted_key(key):
    return bool(re.search(r'quote|quoted|retweeted|retweet', key, re.IGNORECASE))


def metadata_author_allowed(value):
    for key in ('user', 'author', 'userInfo'):
        user = value.get(key)
        if not isinstance(user, dict):
            continue
        handle = user.get('screen_name', user.get('screenName'))
        uid = user.get('id_str', user.get('id'))
        if handle is not None and handle != AUTHOR:
            return False
        if uid is not None and str(uid) != AUTHOR_ID:
            return False
    return True


def structured_urls(value):
    found = set()
    if isinstance(value, dict):
        if not metadata_author_allowed(value):
            return found
        for key, child in value.items():
            if not quoted_key(key) and key not in (
                    'text', 'full_text', 'fullText', 'description', 'quotedText'):
                found.update(structured_urls(child))
    elif isinstance(value, list):
        for child in value:
            found.update(structured_urls(child))
    elif isinstance(value, str):
        found.update(urls_in(value))
    return found


class DiscoveryParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.stack = []
        self.next_data = None
        self.script = False
        self.structured = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        blocked = (any(item[1] for item in self.stack)
                   or tag == 'blockquote'
                   or quoted_key(attributes.get('class', ''))
                   or quoted_key(attributes.get('data-testid', '')))
        if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img',
                       'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
            self.stack.append((tag, blocked))
        if tag == 'script':
            self.script = True
            if attributes.get('id') == '__NEXT_DATA__' and not blocked:
                self.next_data = []
        if not blocked and not self.script and tag == 'a':
            self.ids.update(urls_in(attributes.get('href', '')))

    def handle_endtag(self, tag):
        if tag == 'script':
            self.script = False
            if self.next_data is not None:
                try:
                    raw = ''.join(self.next_data)
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = json.loads(html.unescape(raw))
                    self.ids.update(structured_urls(value))
                    self.structured = True
                except (ValueError, RecursionError):
                    pass
                self.next_data = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.next_data is not None:
            self.next_data.append(data)


def discover(document):
    parser = DiscoveryParser()
    parser.feed(document)
    parser.close()
    # A challenge/login/error document must not be reported as a zero-result search.
    recognized = (parser.structured or bool(parser.ids)
                  or '検索結果はありません' in document
                  or '検索結果がありません' in document
                  or '一致するポストは見つかりませんでした' in document
                  or '一致するツイートは見つかりませんでした' in document)
    if not recognized:
        raise FetchFailure('unrecognized_search_page')
    return sorted(parser.ids, key=int, reverse=True)


class FetchFailure(Exception):
    def __init__(self, reason, status=None, retry_at=None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.retry_at = retry_at

    def facts(self):
        result = {'reason': self.reason}
        if self.status is not None:
            result['httpStatus'] = self.status
        if self.retry_at is not None:
            result['retryAt'] = iso(self.retry_at)
        return result


def check_http_metadata(headers, now):
    headers = {key.lower(): value for key, value in headers.items()}
    try:
        age = int(headers.get('age', '0'))
        if age < 0:
            raise ValueError
        date = (email.utils.parsedate_to_datetime(headers['date'])
                if 'date' in headers else None)
        if date is not None and (date.tzinfo is None or date.utcoffset() is None):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise FetchFailure('invalid_http_metadata')
    if date is not None:
        if date > now + dt.timedelta(minutes=5):
            raise FetchFailure('future_http_date')
        age = max(age, (now - date).total_seconds())
    limit = MAX_CACHE_SECONDS
    match = re.search(r'(?:^|,)\s*(?:s-maxage|max-age)\s*=\s*"?(\d+)',
                      headers.get('cache-control', ''), re.IGNORECASE)
    if match:
        # Permit transport/clock granularity, not stale-while-revalidate snapshots.
        limit = min(limit, int(match.group(1)) + 60)
    if age > limit or re.search(r'(?:^|,)\s*11[01]\b', headers.get('warning', '')):
        raise FetchFailure('stale_http_cache')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FetchFailure('redirect_refused', status=code)


class LimitedResponse:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.response.close()

    def read(self):
        body = self.response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise FetchFailure('response_too_large')
        return body


class PublicClient:
    def __init__(self, clock=utc_now, sleep=time.sleep, monotonic=time.monotonic):
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        self.cooldowns = {}
        self.blocked = set()
        self.last_request = {}
        self.importer = load_importer()
        # Reuse fetch unchanged, but capture HTTP safety metadata before it parses
        # the response. Do not replace urllib's process-global urlopen.
        self.importer.urllib = SimpleNamespace(
            request=SimpleNamespace(Request=urllib.request.Request, urlopen=self.open),
            error=urllib.error)

    def begin_run(self):
        self.blocked.clear()

    def open(self, request, timeout=45):
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        is_post = (parsed.scheme == 'https' and host == POST_HOST
                   and parsed.path == '/tweet-result'
                   and not parsed.username and not parsed.port)
        if url not in SEARCH_URLS and not is_post:
            raise FetchFailure('route_refused')
        now = self.clock()
        retry_at = self.cooldowns.get(host)
        if host in self.blocked or (retry_at is not None and now < retry_at):
            raise FetchFailure('host_rate_limited', retry_at=retry_at)
        if host in self.last_request:
            self.sleep(max(0, 2 - (self.monotonic() - self.last_request[host])))
        self.last_request[host] = self.monotonic()
        try:
            # Use an ordinary unauthenticated GET, including when the reused
            # importer supplies browser-like headers. No cookies or identity spoof.
            response = self.opener.open(urllib.request.Request(url), timeout=timeout)
        except urllib.error.HTTPError as exc:
            retry_at = None
            if exc.code in (403, 429):
                retry_at = now + dt.timedelta(hours=1)
                raw = exc.headers.get('Retry-After') if exc.headers else None
                if raw:
                    try:
                        retry_at = now + dt.timedelta(seconds=max(0, int(raw)))
                    except (ValueError, OverflowError):
                        try:
                            retry_at = email.utils.parsedate_to_datetime(raw).astimezone(UTC)
                        except (ValueError, TypeError, OverflowError):
                            pass
                self.cooldowns[host] = max(now, retry_at)
                self.blocked.add(host)
            exc.close()
            raise FetchFailure('http_error', exc.code, retry_at) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise FetchFailure('network_error') from None
        try:
            if response.getcode() != 200:
                raise FetchFailure('unexpected_http_status', response.getcode())
            check_http_metadata(response.headers, self.clock())
            return LimitedResponse(response)
        except BaseException:
            response.close()
            raise

    def search(self, url):
        request = urllib.request.Request(url)
        try:
            with self.open(request) as response:
                return discover(response.read().decode('utf-8', 'strict'))
        except (UnicodeError, ValueError, RecursionError):
            raise FetchFailure('invalid_search_response') from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise FetchFailure('network_error') from None

    def fetch_post(self, tid):
        try:
            return self.importer.fetch(tid)
        except (ValueError, UnicodeError, RecursionError):
            raise FetchFailure('invalid_post_json') from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise FetchFailure('network_error') from None


def matching_id(value, expected):
    supplied = [value[key] for key in ('id_str', 'id') if key in value]
    return bool(supplied) and all(post_id(item) == expected for item in supplied)


def validate_post(tid, value, start, end, now):
    if not isinstance(value, dict) or not matching_id(value, tid):
        raise FetchFailure('response_id_mismatch')
    author = value.get('user')
    if (not isinstance(author, dict) or not matching_id(author, AUTHOR_ID)
            or author.get('screen_name') != AUTHOR):
        raise FetchFailure('author_mismatch')
    try:
        created = timestamp(value.get('created_at'))
    except (ValueError, TypeError, OverflowError):
        raise FetchFailure('invalid_created_at') from None
    if created > now:
        raise FetchFailure('future_post')
    if not start <= service_day(created) <= end:
        raise FetchFailure('outside_date_range')
    try:
        if abs((snowflake_time(tid) - created).total_seconds()) >= 2:
            raise FetchFailure('id_timestamp_mismatch')
    except (ValueError, OverflowError, OSError):
        raise FetchFailure('invalid_post_id') from None
    # Only the outer post's text and outer author are ever inspected.
    text = value.get('text')
    if not isinstance(text, str):
        raise FetchFailure('missing_post_text')
    head = IMPORTER.norm(text[:120])
    if not any(IMPORTER.norm(word) + 'にゃんこ' in head
               for word, _ in IMPORTER.SHIFT_WORDS):
        return None
    if 'アキバ絶対' not in head:
        raise FetchFailure('missing_store_header')
    parsed = IMPORTER.parse(text, value['created_at'])
    if parsed is None:
        raise FetchFailure('parse_failed')
    date, store, shift, names = parsed
    if date != service_day(created).isoformat() or store not in STORE_IDS:
        raise FetchFailure('parsed_metadata_mismatch')
    return {
        'id': tid, 'url': canonical(tid), 'authorId': AUTHOR_ID,
        'authorScreenName': AUTHOR, 'createdAt': iso(created), 'date': date,
        'shift': {'ひる': '昼', 'よる': '夜'}[shift],
        'storeId': STORE_IDS[store], 'names': names, 'observedAt': iso(now),
    }


def empty_snapshot():
    return {'schemaVersion': 1, 'complete': False, 'checkedAt': None,
            'lastSuccessAt': None, 'posts': [], 'pending': [], 'resolved': [],
            'lastRun': {'status': 'never', 'dateFrom': None, 'dateTo': None}}


def load_snapshot(path):
    if not path.exists():
        return empty_snapshot()
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if (type(state['schemaVersion']) is not int or state['schemaVersion'] != 1
                or state['complete'] is not False
                or not isinstance(state['posts'], list)
                or not isinstance(state['pending'], list)
                or state['lastRun']['status'] not in STATUSES):
            raise ValueError
        for field in ('checkedAt', 'lastSuccessAt'):
            if state[field] is not None:
                timestamp(state[field])
        cooldowns = state.get('cooldowns', {})
        if not isinstance(cooldowns, dict):
            raise ValueError
        for host, until in cooldowns.items():
            if host not in ('search.yahoo.co.jp', POST_HOST):
                raise ValueError
            timestamp(until)
        ids = set()
        for post in state['posts']:
            tid = post_id(post['id'])
            if (not isinstance(post['id'], str) or not tid or tid in ids
                    or post['url'] != canonical(tid)
                    or post['authorId'] != AUTHOR_ID
                    or post['authorScreenName'] != AUTHOR
                    or post['storeId'] not in STORE_IDS.values()
                    or post['shift'] not in ('昼', '夜')
                    or not isinstance(post['names'], list) or not post['names']
                    or any(not isinstance(name, str) or not name for name in post['names'])
                    or service_day(timestamp(post['createdAt'])).isoformat() != post['date']):
                raise ValueError
            timestamp(post['observedAt'])
            ids.add(tid)
        pending_ids = set()
        for item in state['pending']:
            tid = post_id(item['id'])
            if (not isinstance(item['id'], str) or not tid or tid in pending_ids
                    or item['url'] != canonical(tid)
                    or not re.fullmatch(r'[a-z_]+', item['reason'])):
                raise ValueError
            timestamp(item['firstSeenAt'])
            if item.get('lastAttemptAt') is not None:
                timestamp(item['lastAttemptAt'])
            pending_ids.add(tid)
        resolved_ids = set()
        if not isinstance(state.get('resolved', []), list):
            raise ValueError
        for item in state.get('resolved', []):
            tid = post_id(item['id'])
            if (not isinstance(item['id'], str) or not tid or tid in resolved_ids
                    or item['url'] != canonical(tid) or item['reason'] != 'not_shift_post'):
                raise ValueError
            timestamp(item['resolvedAt'])
            resolved_ids.add(tid)
        return state
    except (KeyError, ValueError, TypeError, OverflowError):
        raise ValueError('invalid_snapshot') from None


def curated_ids(path):
    with path.open(encoding='utf-8-sig', newline='') as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or 'tweet_id' not in reader.fieldnames:
            raise ValueError('invalid_curated_header')
        return {tid for row in reader if (tid := post_id(row.get('tweet_id')))}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with scratch.open('x', encoding='utf-8', newline='\n') as target:
            json.dump(value, target, ensure_ascii=False, sort_keys=True, indent=2)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(scratch, path)
    finally:
        if scratch.exists():
            scratch.unlink()

def load_transport(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if value['schemaVersion'] != 1 or not isinstance(value['cooldowns'], dict):
            raise ValueError
        for host, until in value['cooldowns'].items():
            if host not in ('search.yahoo.co.jp', POST_HOST):
                raise ValueError
            timestamp(until)
        return value['cooldowns']
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('invalid_transport_state') from None


def merge_snapshots(primary, published):
    """Import verified facts from a mirror without replacing existing facts."""
    result = copy.deepcopy(primary)
    posts = {post['id']: post for post in result['posts']}
    for post in published['posts']:
        existing = posts.get(post['id'])
        if existing is not None:
            facts = lambda item: {key: value for key, value in item.items() if key != 'observedAt'}
            if facts(existing) != facts(post):
                raise ValueError('observation_conflict')
        else:
            posts[post['id']] = copy.deepcopy(post)
    result['posts'] = list(posts.values())
    resolved = {}
    for item in [*primary.get('resolved', []), *published.get('resolved', [])]:
        if item['id'] not in posts:
            previous = resolved.get(item['id'])
            if previous is None or timestamp(item['resolvedAt']) > timestamp(previous['resolvedAt']):
                resolved[item['id']] = copy.deepcopy(item)
    result['resolved'] = list(resolved.values())
    pending = {}
    for item in [*primary['pending'], *published['pending']]:
        if item['id'] in posts or item['id'] in resolved:
            continue
        previous = pending.get(item['id'])
        when = timestamp(item.get('lastAttemptAt') or item['firstSeenAt'])
        if previous is None or when >= timestamp(previous.get('lastAttemptAt') or previous['firstSeenAt']):
            pending[item['id']] = copy.deepcopy(item)
    result['pending'] = list(pending.values())
    limits = {}
    for state in (primary, published):
        for host, until in state.get('cooldowns', {}).items():
            if host not in limits or timestamp(until) > timestamp(limits[host]):
                limits[host] = until
        for field in ('checkedAt', 'lastSuccessAt'):
            value = state[field]
            if value is not None and (result[field] is None or timestamp(value) > timestamp(result[field])):
                result[field] = value
    result['cooldowns'] = limits
    return result


class ProcessLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b'\0')
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise ValueError('collector_locked') from None
        return self

    def __exit__(self, *args):
        if self.handle is not None:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def collect(state, known, client, start, end, max_posts, clock=utc_now, on_limit=None):
    checked = clock()
    next_state = copy.deepcopy(state)
    present = {post['id'] for post in state['posts']}
    resolved = {item['id']: copy.deepcopy(item) for item in state.get('resolved', [])}
    pending = {item['id']: copy.deepcopy(item) for item in state['pending']
               if item['id'] not in present | known | resolved.keys()}
    sources, candidates, failures, rejected = [], set(), [], []
    new_posts = []
    cooldowns = {host: timestamp(until) for host, until in state.get('cooldowns', {}).items()}
    blocked_hosts = set()

    def limited(host):
        return host in blocked_hosts or cooldowns.get(host, checked) > clock()

    def remember_limit(host, exc):
        if exc.status in (403, 429) or exc.reason == 'host_rate_limited':
            blocked_hosts.add(host)
            until = exc.retry_at or clock() + dt.timedelta(hours=1)
            cooldowns[host] = max(cooldowns.get(host, until), until)
            if on_limit is not None:
                on_limit(host, cooldowns[host])

    client.begin_run()
    for url in SEARCH_URLS:
        host = urllib.parse.urlsplit(url).hostname
        try:
            if limited(host):
                raise FetchFailure('host_rate_limited', retry_at=cooldowns.get(host))
            ids = client.search(url)
            candidates.update(ids)
            sources.append({'url': url, 'status': 'ok', 'candidateCount': len(ids)})
        except FetchFailure as exc:
            remember_limit(host, exc)
            sources.append({'url': url, 'status': 'failed', **exc.facts()})
    discovered_count = len(candidates)
    candidates.update(pending)
    skipped_curated = skipped_observed = skipped_resolved = 0
    eligible = []
    for tid in sorted(candidates, key=int, reverse=True):
        try:
            created = snowflake_time(tid)
            if created > checked:
                rejected.append({'id': tid, 'reason': 'future_candidate'})
                continue
            if not start <= service_day(created) <= end:
                rejected.append({'id': tid, 'reason': 'outside_date_range'})
                continue
        except (ValueError, OSError, OverflowError):
            rejected.append({'id': tid, 'reason': 'invalid_post_id'})
            continue
        if tid in known:
            skipped_curated += 1
        elif tid in present:
            skipped_observed += 1
        elif tid in resolved:
            skipped_resolved += 1
        else:
            eligible.append(tid)
    # Retry older failed attempts first; no cursor can silently skip a failure.
    eligible.sort(key=lambda tid: (
        tid not in pending, pending.get(tid, {}).get('lastAttemptAt') or '', -int(tid)))
    attempted = fetched = handled = 0
    host_stopped = limited(POST_HOST)
    for index, tid in enumerate(eligible):
        previous = pending.get(tid, {})
        item = {'id': tid, 'url': canonical(tid),
                'firstSeenAt': previous.get('firstSeenAt', iso(checked)),
                'lastAttemptAt': previous.get('lastAttemptAt'),
                'attempts': previous.get('attempts', 0)}
        if index >= max_posts or host_stopped:
            item['reason'] = 'host_rate_limited' if host_stopped else 'post_limit'
            if host_stopped and POST_HOST in cooldowns:
                item['retryAt'] = iso(cooldowns[POST_HOST])
            elif previous.get('retryAt'):
                item['retryAt'] = previous['retryAt']
            pending[tid] = item
            continue
        attempted += 1
        item['lastAttemptAt'] = iso(clock())
        item['attempts'] += 1
        try:
            value = client.fetch_post(tid)
            fetched += 1
            post = validate_post(tid, value, start, end, clock())
            handled += 1
            pending.pop(tid, None)
            if post is None:
                rejected.append({'id': tid, 'reason': 'not_shift_post'})
                resolved[tid] = {'id': tid, 'url': canonical(tid),
                                 'reason': 'not_shift_post', 'resolvedAt': iso(clock())}
            else:
                new_posts.append(post)
        except FetchFailure as exc:
            remember_limit(POST_HOST, exc)
            item.update(exc.facts())
            pending[tid] = item
            failures.append({'id': tid, 'url': canonical(tid), **exc.facts()})
            if exc.status in (403, 429) or exc.reason == 'host_rate_limited':
                host_stopped = True
    source_count = sum(source['status'] == 'ok' for source in sources)
    deferred = len(eligible) - attempted
    if source_count == 0 and handled == 0:
        status = 'unavailable'
    elif source_count != len(SEARCH_URLS) or failures or deferred or pending:
        status = 'partial'
    elif new_posts:
        status = 'ok'
    elif skipped_curated or skipped_observed or skipped_resolved or handled:
        status = 'no-new'
    else:
        status = 'no-results'
    finished = iso(clock())
    next_state['checkedAt'] = iso(checked)
    if status in ('ok', 'no-new', 'no-results'):
        next_state['lastSuccessAt'] = finished
    next_state['posts'].extend(new_posts)
    next_state['posts'].sort(key=lambda post: (post['createdAt'], int(post['id'])))
    next_state['pending'] = sorted(pending.values(), key=lambda item: int(item['id']))
    next_state['resolved'] = sorted(resolved.values(), key=lambda item: int(item['id']))
    next_state['cooldowns'] = {host: iso(until) for host, until in cooldowns.items()
                               if until > clock()}
    next_state['lastRun'] = {
        'status': status, 'dateFrom': start.isoformat(), 'dateTo': end.isoformat(),
        'dateBasis': 'JST service day, 05:00 boundary',
        'finishedAt': finished, 'sourceCount': source_count,
        'sourcePageLimit': len(SEARCH_URLS), 'sources': sources,
        'discoveredCount': discovered_count, 'eligibleCount': len(eligible),
        'attemptedCount': attempted, 'fetchedCount': fetched,
        'newPostCount': len(new_posts),
        'newNameCount': sum(len(post['names']) for post in new_posts),
        'skippedCuratedCount': skipped_curated, 'skippedObservedCount': skipped_observed,
        'skippedResolvedCount': skipped_resolved,
        'deferredCount': deferred, 'pendingCount': len(pending),
        'pendingOutsideRangeCount': sum(tid not in eligible for tid in pending),
        'maxPosts': max_posts, 'failures': failures, 'rejected': rejected,
        'complete': False,
        'lastSuccessMeaning': 'Both searches and every selected post handled without failure or deferral',
    }
    report = {'schemaVersion': 1, 'checkedAt': next_state['checkedAt'],
              'lastSuccessAt': next_state['lastSuccessAt'],
              **next_state['lastRun'], 'newFacts': new_posts,
              'pending': next_state['pending']}
    return next_state, report, {'partial': 2, 'unavailable': 3}.get(status, 0)


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='leave observation facts unchanged; persist HTTP cooldowns even in dry-run')
    parser.add_argument('--days', type=int, default=2,
                        help='inclusive JST 05:00 service days (default: 2)')
    parser.add_argument('--date-from', help='inclusive service date YYYY-MM-DD')
    parser.add_argument('--date-to', help='inclusive service date YYYY-MM-DD')
    parser.add_argument('--max-posts', type=int, default=20,
                        help='individual request cap, 1..20 (default: 20)')
    parser.add_argument('--report', type=Path, help='fact-only JSON report; absolute paths allowed')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--watch', action='store_true',
                      help='repeat while holding the same process lock for the entire lifetime')
    mode.add_argument('--once', action='store_true', help='one run (default; useful for scheduled tasks)')
    parser.add_argument('--interval', type=int, default=3600,
                        help='watch interval in seconds, at least 60 (default: 3600)')
    parser.add_argument('--snapshot', type=Path,
                        help='canonical durable JSON path (default: data/observed-shifts.json)')
    parser.add_argument('--publish', type=Path,
                        help='atomically mirror the canonical snapshot to this frontend JSON path')
    return parser


def write_report(report, destination):
    if destination is not None:
        atomic_json(destination, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


def run(args, snapshot=SNAPSHOT, curated=CURATED, client=None,
        clock=utc_now, sleep=time.sleep):
    snapshot = (args.snapshot or snapshot).resolve()
    publish = args.publish.resolve() if args.publish else None
    if snapshot.suffix.lower() != '.json' or (publish and publish.suffix.lower() != '.json'):
        raise ValueError('json_snapshot_required')
    paths = {snapshot}
    if publish:
        paths.add(publish)
    transport_paths = {path.with_suffix('.http-state.json') for path in paths}
    if paths & transport_paths or any(path.name.endswith('.http-state.json') for path in paths):
        raise ValueError('overlapping_storage_paths')
    if args.report:
        report_path = args.report.resolve()
        protected = {snapshot.resolve(), snapshot.with_suffix('.lock').resolve(),
                     curated.resolve()}
        protected.update(transport_paths)
        if publish:
            protected.update((publish, publish.with_suffix('.lock')))
        # Reports may be in external artifact folders, but may not overwrite
        # other project data/code or become a second frontend snapshot.
        protected_dirs = (ROOT / 'data', ROOT / 'tools' / 'data')
        if (report_path in protected or report_path.suffix.lower() != '.json'
                or any(path in report_path.parents for path in protected_dirs)):
            raise ValueError('unsafe_report_path')
    with ExitStack() as locks:
        for path in sorted(paths, key=lambda item: str(item).casefold()):
            locks.enter_context(ProcessLock(path.with_suffix('.lock')))
        client = client or PublicClient(clock=clock, sleep=sleep)
        while True:
            state = load_snapshot(snapshot)
            if publish and publish != snapshot and publish.exists():
                state = merge_snapshots(state, load_snapshot(publish))
            limits = dict(state.get('cooldowns', {}))
            for path in transport_paths:
                for host, until in load_transport(path).items():
                    if host not in limits or timestamp(until) > timestamp(limits[host]):
                        limits[host] = until

            def persist_limits(host=None, until=None):
                if host is not None:
                    value = iso(until)
                    if host not in limits or timestamp(value) > timestamp(limits[host]):
                        limits[host] = value
                payload = {'schemaVersion': 1, 'cooldowns': dict(limits)}
                for path in sorted(transport_paths):
                    atomic_json(path, payload)

            # Preflight durable transport storage before any request. Observation
            # dry-runs still have network side effects, so limits must survive them.
            persist_limits()
            state['cooldowns'] = dict(limits)
            known = curated_ids(curated)
            start, end = date_range(args, clock())
            updated, report, code = collect(
                state, known, client, start, end, args.max_posts, clock, on_limit=persist_limits)
            report['dryRun'] = args.dry_run
            report['saved'] = not args.dry_run
            report['processId'] = os.getpid()
            report['watch'] = args.watch
            report['intervalSeconds'] = args.interval if args.watch else None
            report['snapshotPath'] = str(snapshot)
            report['publishPath'] = str(publish) if publish else None
            report['transportStatePaths'] = [str(path) for path in sorted(transport_paths)]
            report['published'] = False
            if not args.dry_run:
                atomic_json(snapshot, updated)
                report['published'] = publish == snapshot
                if publish and publish != snapshot:
                    try:
                        atomic_json(publish, updated)
                        report['published'] = True
                    except OSError:
                        report['collectionStatus'] = report['status']
                        report.update(status='unavailable', reason='publication_failed')
                        code = 4
            write_report(report, args.report)
            if not args.watch:
                return code
            sleep(args.interval)


def main(argv=None):
    parser = argument_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 366 or not 1 <= args.max_posts <= MAX_POSTS:
        parser.error('--days must be 1..366 and --max-posts must be 1..20')
    if args.interval < 60:
        parser.error('--interval must be at least 60 seconds')
    try:
        date_range(args, utc_now())
        return run(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'local_io_error'
        if not re.fullmatch(r'[a-z_]+', reason):
            reason = 'invalid_local_data'
        print(json.dumps({'status': 'unavailable', 'reason': reason, 'exitCode': 4}))
        return 4


if __name__ == '__main__':
    sys.exit(main())
