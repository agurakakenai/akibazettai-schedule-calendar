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
import functools
import html
from html.parser import HTMLParser
import http.client
import importlib.util
import io
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


@functools.lru_cache(maxsize=1)
def analysis_module():
    spec = importlib.util.spec_from_file_location('official_notice_analysis',
                                                 ROOT / 'tools' / 'official-azure.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def source_module():
    spec = importlib.util.spec_from_file_location('shared_source_accounting',
                                                 ROOT / 'tools' / 'source-state.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_call(source, method, *args, **kwargs):
    try:
        return getattr(source, method)(*args, **kwargs)
    except source.failure_type as exc:
        retry = timestamp(exc.retry_at) if exc.retry_at else None
        raise FetchFailure(exc.reason, exc.status, retry) from None


def analysis_context():
    return SimpleNamespace(
        ROOT=ROOT, IMPORTER=IMPORTER, STORE_IDS=STORE_IDS, timestamp=timestamp,
        snowflake_time=snowflake_time, post_id=post_id, iso=iso,
        validate_post=validate_post)


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
    def __init__(self, response, source=None, receipt=None):
        self.response = response
        self.source, self.receipt, self.finished = source, receipt, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self.response.close()
        finally:
            if self.source is not None and not self.finished:
                source_call(self.source, 'finish', self.receipt, 'failed')

    def read(self):
        body = self.response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise FetchFailure('response_too_large')
        if self.source is not None and not self.finished:
            source_call(self.source, 'finish', self.receipt, http_status=200)
            self.finished = True
            source_call(self.source, 'remember', self.receipt, body)
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
        self.requests = {'searches': 0, 'posts': 0}
        self.shared_source = None
        self.importer = load_importer()
        # Reuse fetch unchanged, but capture HTTP safety metadata before it parses
        # the response. Do not replace urllib's process-global urlopen.
        self.importer.urllib = SimpleNamespace(
            request=SimpleNamespace(Request=urllib.request.Request, urlopen=self.open),
            error=urllib.error)

    def begin_run(self):
        self.blocked.clear()
        self.requests = {'searches': 0, 'posts': 0}

    def open(self, request, timeout=45):
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        is_post = (parsed.scheme == 'https' and host == POST_HOST
                   and parsed.path == '/tweet-result'
                   and not parsed.username and not parsed.port)
        if url not in SEARCH_URLS and not is_post:
            raise FetchFailure('route_refused')
        if self.shared_source is not None:
            cached = source_call(self.shared_source, 'cached', 'posts' if is_post else 'searches', url)
            if cached is not None:
                return io.BytesIO(cached['body'])
        now = self.clock()
        retry_at = self.cooldowns.get(host)
        if host in self.blocked or (retry_at is not None and now < retry_at):
            raise FetchFailure('host_rate_limited', retry_at=retry_at)
        if host in self.last_request:
            self.sleep(max(0, 2 - (self.monotonic() - self.last_request[host])))
        self.last_request[host] = self.monotonic()
        receipt = None
        if self.shared_source is not None:
            receipt = source_call(self.shared_source, 'reserve', 'posts' if is_post else 'searches', url)
            source_call(self.shared_source, 'issued', receipt)
            now = self.clock()
        try:
            # Use an ordinary unauthenticated GET, including when the reused
            # importer supplies browser-like headers. No cookies or identity spoof.
            self.requests['posts' if is_post else 'searches'] += 1
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
                if self.shared_source is not None:
                    source_call(self.shared_source, 'set_cooldown', host, self.cooldowns[host])
            exc.close()
            if receipt is not None:
                source_call(self.shared_source, 'finish', receipt, 'failed', http_status=exc.code)
            raise FetchFailure('http_error', exc.code, retry_at) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            if receipt is not None:
                source_call(self.shared_source, 'finish', receipt, 'failed')
            raise FetchFailure('network_error') from None
        except FetchFailure as exc:
            if receipt is not None:
                source_call(self.shared_source, 'finish', receipt, 'failed', http_status=exc.status)
            raise
        try:
            if response.getcode() != 200:
                raise FetchFailure('unexpected_http_status', response.getcode())
            check_http_metadata(response.headers, self.clock())
            return LimitedResponse(response, self.shared_source, receipt)
        except BaseException:
            response.close()
            if receipt is not None:
                source_call(self.shared_source, 'finish', receipt, 'failed')
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
            if host not in ('search.yahoo.co.jp', POST_HOST, 'pbs.twimg.com'):
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
            if 'notices' in post:
                analysis_module().validate_notices(
                    post['notices'], analysis_context(), timestamp(post['createdAt']))
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
        if 'officialAnalysis' in state:
            analysis_module().validate_state(state['officialAnalysis'], analysis_context())
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
            if host not in ('search.yahoo.co.jp', POST_HOST, 'pbs.twimg.com'):
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
            facts = lambda item: {key: value for key, value in item.items()
                                  if key not in ('observedAt', 'notices')}
            if facts(existing) != facts(post):
                raise ValueError('observation_conflict')
            if post.get('notices') and not existing.get('notices'):
                existing['notices'] = copy.deepcopy(post['notices'])
            elif post.get('notices') and existing.get('notices'):
                if existing['notices'] != post['notices']:
                    previous_at = max(timestamp(item.get('observedAt', existing['observedAt']))
                                      for item in existing['notices'])
                    published_at = max(timestamp(item.get('observedAt', post['observedAt']))
                                       for item in post['notices'])
                    if published_at == previous_at:
                        notice_facts = lambda items: sorted(
                            ({key: value for key, value in item.items() if key != 'observedAt'}
                             for item in items), key=lambda item: item['name'])
                        if notice_facts(existing['notices']) != notice_facts(post['notices']):
                            raise ValueError('observation_conflict')
                        by_name = {item['name']: item for item in post['notices']}
                        for item in existing['notices']:
                            if 'observedAt' not in item and 'observedAt' in by_name[item['name']]:
                                item['observedAt'] = by_name[item['name']]['observedAt']
                    if published_at > previous_at:
                        existing['notices'] = copy.deepcopy(post['notices'])
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
        if ((item['id'] in posts or item['id'] in resolved)
                and not item['reason'].startswith('azure_')):
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


def apply_saved_notice(state, amendment):
    """Apply a trusted, data-only same-ID amendment without touching roster facts."""
    analysis = analysis_module()
    analysis.require(amendment, ('schemaVersion', 'id', 'source', 'notices'))
    source = amendment['source']
    analysis.require(source, ('url', 'authorId', 'authorScreenName', 'createdAt',
                              'fetchedAt', 'bodyHash'), ('analyzedAt', 'analysisReceiptHash'))
    tid = amendment['id']
    if (type(amendment['schemaVersion']) is not int or amendment['schemaVersion'] != 1
            or not isinstance(tid, str) or not post_id(tid)
            or not isinstance(source['bodyHash'], str) or not analysis.HEX.fullmatch(source['bodyHash'])
            or source['url'] != canonical(tid) or source['authorId'] != AUTHOR_ID
            or source['authorScreenName'] != AUTHOR):
        raise ValueError('invalid_official_amendment')
    created, fetched = timestamp(source['createdAt']), timestamp(source['fetchedAt'])
    if fetched < created or abs((snowflake_time(tid) - created).total_seconds()) >= 2:
        raise ValueError('invalid_official_amendment')
    if ('analyzedAt' in source) != ('analysisReceiptHash' in source):
        raise ValueError('invalid_official_amendment')
    analyzed_at = source.get('analyzedAt', source['fetchedAt'])
    if (timestamp(analyzed_at) < fetched
            or ('analysisReceiptHash' in source and (
                not isinstance(source['analysisReceiptHash'], str)
                or not analysis.HEX.fullmatch(source['analysisReceiptHash'])))):
        raise ValueError('invalid_official_amendment')
    result = copy.deepcopy(state)
    post = next((item for item in result['posts'] if item['id'] == tid), None)
    if post is None or any(post[field] != source[field] for field in (
            'url', 'authorId', 'authorScreenName', 'createdAt')):
        raise ValueError('official_amendment_target_mismatch')
    analysis.validate_notices(amendment['notices'], analysis_context(), created)
    if not amendment['notices'] or any('observedAt' not in notice or timestamp(notice['observedAt']) != fetched
                                       for notice in amendment['notices']):
        raise ValueError('invalid_official_amendment')
    private = result.setdefault('officialAnalysis', analysis.empty_state())
    analysis.validate_state(private, analysis_context())
    receipt = analysis.digest(analysis.canonical_json(amendment))
    if receipt in private['receipts']:
        return result
    if any(timestamp(notice.get('observedAt', post['observedAt'])) > fetched
           for notice in post.get('notices', [])):
        raise ValueError('stale_official_amendment')
    analysis.replace_notices(result, post, amendment['notices'], analyzed_at,
                             receipt, analysis_context())
    private['receipts'][receipt] = {'id': tid, 'at': source['fetchedAt'], 'bodyHash': source['bodyHash']}
    if 'analyzedAt' in source:
        private['receipts'][receipt].update(
            analyzedAt=analyzed_at, analysisReceiptHash=source['analysisReceiptHash'])
    private['queue'].pop(tid, None)
    result['pending'] = [item for item in result['pending']
                         if item['id'] != tid or not item['reason'].startswith('azure_')]
    return result


def analysis_buffer_path(value):
    path = value.resolve()
    if (value.is_symlink() or value.parent.is_symlink()
            or path.parent.parent != ROOT.resolve()
            or not re.fullmatch(r'\.cc-work-[0-9a-f]{16}', path.parent.name)
            or path.suffix != '.json' or not path.parent.is_dir()):
        raise ValueError('invalid_analysis_buffer_path')
    return path


def buffer_state_hash(state):
    analysis = analysis_module()
    return analysis.digest(analysis.canonical_json({
        key: state[key] for key in ('posts', 'lastRun', 'checkedAt')}))


def make_analysis_buffer(state, run_id, version_hash, items, now):
    value = {'schemaVersion': 1, 'runId': run_id,
             'createdAt': now.astimezone(UTC).isoformat().replace('+00:00', 'Z'),
             'versionHash': version_hash, 'stateHash': buffer_state_hash(state),
             'items': copy.deepcopy(items)}
    validate_analysis_buffer(value, state, run_id, now, version_hash)
    return value


def validate_analysis_buffer(value, state, run_id, now, version_hash=None):
    analysis = analysis_module()
    analysis.require(value, ('schemaVersion', 'runId', 'createdAt', 'versionHash', 'stateHash', 'items'))
    if (type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or not isinstance(value['runId'], str) or value['runId'] != run_id
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', run_id)
            or not isinstance(value['versionHash'], str) or not analysis.HEX.fullmatch(value['versionHash'])
            or (version_hash is not None and value['versionHash'] != version_hash)
            or value['stateHash'] != buffer_state_hash(state)
            or not isinstance(value['items'], list) or len(value['items']) > 3):
        raise ValueError('invalid_analysis_buffer')
    created = timestamp(value['createdAt'])
    if created > now:
        raise ValueError('invalid_analysis_buffer')
    posts = {post['id']: post for post in state['posts']}
    queued = state.get('officialAnalysis', {}).get('queue', {})
    seen = set()
    for entry in value['items']:
        analysis.require(entry, ('id', 'fetchedAt', 'bodyHash', 'payload'))
        tid = entry['id']
        if (not isinstance(tid, str) or tid not in posts or tid in seen or tid not in queued
                or not isinstance(entry['bodyHash'], str) or not analysis.HEX.fullmatch(entry['bodyHash'])
                or entry['bodyHash'] != queued[tid]['bodyHash']
                or timestamp(entry['fetchedAt']) != timestamp(queued[tid]['fetchedAt'])
                or timestamp(entry['fetchedAt']) > created):
            raise ValueError('invalid_analysis_buffer')
        post = posts[tid]
        day = dt.date.fromisoformat(post['date'])
        try:
            verified = validate_post(tid, entry['payload'], day, day, now)
            if (verified is None or any(verified[key] != post[key] for key in (
                    'id', 'url', 'authorId', 'authorScreenName', 'createdAt', 'date', 'shift', 'storeId'))
                    or timestamp(entry['fetchedAt']) < timestamp(post['createdAt'])
                    or analysis.digest(entry['payload']['text']) != entry['bodyHash']
                    or not analysis.edit_metadata_supported(entry['payload'], tid)):
                raise ValueError('invalid_analysis_buffer')
            analysis.source_lines(entry['payload']['text'])
        except (FetchFailure, analysis.AnalysisFailure):
            raise ValueError('invalid_analysis_buffer') from None
        seen.add(tid)


def load_analysis_buffer(path, state, run_id, now):
    if not path.is_file() or path.stat().st_size > 3 * MAX_BODY + 65536:
        raise ValueError('invalid_analysis_buffer')
    value = analysis_module().transport.strict_json(path.read_text(encoding='utf-8'))
    validate_analysis_buffer(value, state, run_id, now)
    return value


def replay_analysis_buffer(state, value, analyzer, clock=utc_now):
    """Consume this run's bounded in-memory sources; retain the first source report."""
    validate_analysis_buffer(value, state, analyzer.usage.run_id, clock(), analyzer.version)
    analysis = analysis_module()
    updated = copy.deepcopy(state)
    initial = copy.deepcopy(state['lastRun'])
    analyzer.bind(updated)
    posts = {post['id']: post for post in updated['posts']}
    pending = {item['id']: item for item in updated['pending']}
    attempted, failures = 0, []
    for entry in value['items']:
        tid = entry['id']
        if not analyzer.can_fetch(tid):
            continue
        attempted += 1
        try:
            notices, reason, key = analyzer.parse(entry['payload'], posts[tid], entry['fetchedAt'])
            if reason == 'notices':
                analysis.replace_notices(updated, posts[tid], notices, iso(clock()), key, analysis_context())
            pending.pop(tid, None)
        except analysis.AnalysisFailure as exc:
            previous = pending.get(tid, {})
            pending[tid] = {
                'id': tid, 'url': canonical(tid), **exc.facts(),
                'firstSeenAt': previous.get('firstSeenAt', entry['fetchedAt']),
                'lastAttemptAt': iso(clock()), 'attempts': previous.get('attempts', 0) + 1}
            failures.append({'id': tid, **exc.facts()})
    updated['pending'] = sorted(pending.values(), key=lambda item: int(item['id']))
    updated['lastRun'] = initial
    status = initial['status']
    code = {'partial': 2, 'unavailable': 3}.get(status, 0)
    report = {
        'schemaVersion': 1, **copy.deepcopy(initial), 'component': 'official', 'exitCode': code,
        'checkedAt': updated['checkedAt'], 'lastSuccessAt': updated['lastSuccessAt'],
        'requests': {'searches': 0, 'posts': 0}, 'attemptedCount': 0, 'fetchedCount': 0,
        'newPostCount': 0, 'newNameCount': 0, 'newFacts': [], 'pending': updated['pending'],
        'analysisReplayCount': attempted, 'analysisReplayFailures': failures, 'initialRun': initial,
        'analysisDeferredCount': len(analyzer.state['queue']),
        'analysisPendingCount': sum(item['reason'].startswith('azure_') for item in updated['pending']),
    }
    return updated, report, code


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


def collect(state, known, client, start, end, max_posts, clock=utc_now, on_limit=None,
            analyzer=None, saved_payloads=None, source_fetched_at=None, buffered_payloads=None):
    checked = clock()
    next_state = copy.deepcopy(state)
    analysis = analysis_module() if analyzer is not None else None
    staged_analysis = analyzer is not None and buffered_payloads is not None and saved_payloads is None
    analysis_jobs = []
    if analyzer is not None:
        analyzer.bind(next_state)
    present = {post['id'] for post in state['posts']}
    resolved = {item['id']: copy.deepcopy(item) for item in state.get('resolved', [])}
    pending = {item['id']: copy.deepcopy(item) for item in next_state['pending']
               if item['id'] not in present | known | resolved.keys()
               or item['reason'].startswith('azure_')}
    sources, candidates, failures, rejected = [], set(), [], []
    search_attempted = 0
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

    if saved_payloads is not None:
        if analyzer is None or not isinstance(saved_payloads, dict) or not source_fetched_at:
            raise ValueError('invalid_saved_analysis')
        acquired = timestamp(source_fetched_at)
        if acquired > checked:
            raise ValueError('future_source_fetched_at')
        if any(not isinstance(tid, str) or not post_id(tid)
               or tid not in present | known | resolved.keys() for tid in saved_payloads):
            raise ValueError('saved_post_not_known')
        candidates.update(saved_payloads)
    else:
        client.begin_run()
    for url in (() if saved_payloads is not None else SEARCH_URLS):
        host = urllib.parse.urlsplit(url).hostname
        called = False
        try:
            if limited(host):
                raise FetchFailure('host_rate_limited', retry_at=cooldowns.get(host))
            search_attempted += 1
            called = True
            ids = client.search(url)
            candidates.update(ids)
            sources.append({'url': url, 'status': 'ok', 'candidateCount': len(ids)})
        except FetchFailure as exc:
            if called and (exc.reason in ('host_rate_limited', 'route_refused')
                           or exc.reason.startswith('source_')):
                search_attempted -= 1
            remember_limit(host, exc)
            sources.append({'url': url, 'status': 'failed', **exc.facts()})
    discovered_count = len(candidates)
    if saved_payloads is None:
        candidates.update(pending)
    skipped_curated = skipped_observed = skipped_resolved = 0
    eligible = []
    for tid in sorted(candidates, key=int, reverse=True):
        queued = False
        if analyzer is not None and tid in analyzer.state['queue']:
            queued = analyzer.prefetch_capacity() > 0 if staged_analysis else analyzer.can_fetch(tid)
        saved = saved_payloads is not None and tid in saved_payloads
        recover_roster = False
        if analyzer is not None and not (queued or saved):
            cached = [entry for entry in analyzer.state['cache'].values() if entry['postId'] == tid]
            if cached and tid not in present:
                latest = max(enumerate(cached), key=lambda pair: (timestamp(pair[1]['at']), pair[0]))[1]
                recover_roster = latest['reason'] in ('notices', 'no_event')
                if not recover_roster:
                    continue
        if (tid in pending and pending[tid]['reason'].startswith('azure_')
                and not (queued or saved or recover_roster)):
            continue
        try:
            created = snowflake_time(tid)
            if created - dt.timedelta(seconds=2) > checked:
                rejected.append({'id': tid, 'reason': 'future_candidate'})
                continue
            # Metadata is authoritative within the existing <2s snowflake tolerance.
            margin = dt.timedelta(seconds=2)
            if not (service_day(created + margin) >= start
                    and service_day(created - margin) <= end):
                rejected.append({'id': tid, 'reason': 'outside_date_range'})
                continue
        except (ValueError, OSError, OverflowError):
            rejected.append({'id': tid, 'reason': 'invalid_post_id'})
            continue
        if saved or queued:
            eligible.append(tid)
        elif tid in known:
            skipped_curated += 1
        elif tid in present:
            skipped_observed += 1
        elif tid in resolved:
            skipped_resolved += 1
        else:
            eligible.append(tid)
    # New roster facts precede supplemental known-ID work; retry failures within each group.
    eligible.sort(key=lambda tid: (
        tid in present | known | resolved.keys(),
        tid not in pending, pending.get(tid, {}).get('lastAttemptAt') or '', -int(tid)))
    attempted = fetched = handled = post_issued = 0
    known_prefetch_slots, known_prefetch_attempts, preceding_jobs = None, 0, 0
    host_stopped = limited(POST_HOST)
    for index, tid in enumerate(eligible):
        previous = pending.get(tid, {})
        item = {'id': tid, 'url': canonical(tid),
                'firstSeenAt': previous.get('firstSeenAt', iso(checked)),
                'lastAttemptAt': previous.get('lastAttemptAt'),
                'attempts': previous.get('attempts', 0)}
        known_queue = (analyzer is not None and tid in analyzer.state['queue']
                       and tid in present | known | resolved.keys())
        if saved_payloads is None and known_queue:
            if staged_analysis:
                if known_prefetch_slots is None:
                    preceding_jobs = len(analysis_jobs)
                    known_prefetch_slots = min(
                        3, max(0, analyzer.prefetch_capacity() - preceding_jobs), max_posts - attempted)
                if (known_prefetch_attempts >= known_prefetch_slots
                        or known_prefetch_attempts >= analyzer.prefetch_capacity() - preceding_jobs):
                    continue
            elif not analyzer.can_fetch(tid):
                continue
        host_stopped = host_stopped or limited(POST_HOST)
        if (attempted if saved_payloads is None else index) >= max_posts or host_stopped:
            if known_queue:
                continue
            item['reason'] = 'host_rate_limited' if host_stopped else 'post_limit'
            if host_stopped and POST_HOST in cooldowns:
                item['retryAt'] = iso(cooldowns[POST_HOST])
            elif previous.get('retryAt'):
                item['retryAt'] = previous['retryAt']
            pending[tid] = item
            continue
        if saved_payloads is None:
            attempted += 1
            if staged_analysis and known_queue:
                known_prefetch_attempts += 1
        item['lastAttemptAt'] = iso(clock())
        item['attempts'] += 1
        try:
            if saved_payloads is None:
                post_issued += 1
                value = client.fetch_post(tid)
                fetched += 1
                acquired_at = clock().astimezone(UTC).isoformat().replace('+00:00', 'Z')
            else:
                value = saved_payloads[tid]
                acquired_at = source_fetched_at
            post = validate_post(tid, value, start, end, clock())
            handled += 1
            pending.pop(tid, None)
            if post is None:
                if tid in present and analyzer is not None:
                    raise FetchFailure('azure_header_changed')
                rejected.append({'id': tid, 'reason': 'not_shift_post'})
                resolved[tid] = {'id': tid, 'url': canonical(tid),
                                 'reason': 'not_shift_post', 'resolvedAt': iso(clock())}
            else:
                existing = next((item for item in next_state['posts'] if item['id'] == tid), None)
                if existing is not None:
                    if any(existing[key] != post[key] for key in (
                            'id', 'url', 'authorId', 'authorScreenName', 'createdAt',
                            'date', 'shift', 'storeId')):
                        raise FetchFailure('saved_metadata_conflict')
                    post = existing
                else:
                    new_posts.append(post)
                    if analyzer is not None:
                        next_state['posts'].append(post)
                if analyzer is not None:
                    try:
                        if staged_analysis:
                            notices, reason, key = analyzer.parse(value, post, acquired_at, allow_request=False)
                        else:
                            notices, reason, key = analyzer.parse(value, post, acquired_at)
                        if reason == 'notices':
                            analysis.replace_notices(next_state, post, notices, iso(clock()),
                                                     key, analysis_context())
                    except analysis.AnalysisFailure as exc:
                        item.update(exc.facts())
                        pending[tid] = item
                        if (staged_analysis and tid in analyzer.state['queue']
                                and len(analysis_jobs) < min(3, analyzer.prefetch_capacity())):
                            analysis_jobs.append((tid, value, post, acquired_at, item))
                        else:
                            failures.append({'id': tid, 'url': canonical(tid), **exc.facts()})
                        if (not staged_analysis and buffered_payloads is not None and tid in analyzer.state['queue']
                                and len(buffered_payloads) < 3
                                and not any(entry['id'] == tid for entry in buffered_payloads)):
                            buffered_payloads.append({
                                'id': tid, 'fetchedAt': acquired_at,
                                'bodyHash': analysis.digest(value['text']), 'payload': copy.deepcopy(value)})
        except FetchFailure as exc:
            if saved_payloads is None and (exc.reason in ('host_rate_limited', 'route_refused')
                                           or exc.reason.startswith('source_')):
                post_issued -= 1
            if analyzer is not None and tid in analyzer.state['queue']:
                analyzer.state['queue'].pop(tid, None)
                exc = FetchFailure('azure_saved_body_required', exc.status, exc.retry_at)
            remember_limit(POST_HOST, exc)
            item.update(exc.facts())
            pending[tid] = item
            failures.append({'id': tid, 'url': canonical(tid), **exc.facts()})
            if exc.status in (403, 429) or exc.reason == 'host_rate_limited':
                host_stopped = True
    for tid, value, post, acquired_at, item in analysis_jobs:
        try:
            notices, reason, key = analyzer.parse(value, post, acquired_at)
            if reason == 'notices':
                analysis.replace_notices(next_state, post, notices, iso(clock()), key, analysis_context())
            pending.pop(tid, None)
        except analysis.AnalysisFailure as exc:
            item.update(exc.facts())
            pending[tid] = item
            failures.append({'id': tid, 'url': canonical(tid), **exc.facts()})
            if tid in analyzer.state['queue']:
                buffered_payloads.append({
                    'id': tid, 'fetchedAt': acquired_at,
                    'bodyHash': analysis.digest(value['text']), 'payload': copy.deepcopy(value)})
    source_count = sum(source['status'] == 'ok' for source in sources)
    deferred = max(0, len(eligible) - (attempted if saved_payloads is None else handled + len(failures)))
    if source_count == 0 and handled == 0 and saved_payloads is None:
        status = 'unavailable'
    elif ((saved_payloads is None and source_count != len(SEARCH_URLS))
          or failures or deferred or pending):
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
    existing_ids = {post['id'] for post in next_state['posts']}
    next_state['posts'].extend(post for post in new_posts if post['id'] not in existing_ids)
    next_state['posts'].sort(key=lambda post: (post['createdAt'], int(post['id'])))
    next_state['pending'] = sorted(pending.values(), key=lambda item: int(item['id']))
    next_state['resolved'] = sorted(resolved.values(), key=lambda item: int(item['id']))
    next_state['cooldowns'] = {host: iso(until) for host, until in cooldowns.items()
                               if until > clock()}
    requests = {'searches': search_attempted, 'posts': post_issued}
    measured = getattr(client, 'requests', None) if saved_payloads is None else None
    if (isinstance(measured, dict) and set(measured) == set(requests)
            and all(type(value) is int and value >= 0 for value in measured.values())):
        requests = dict(measured)
    next_state['lastRun'] = {
        'status': status, 'dateFrom': start.isoformat(), 'dateTo': end.isoformat(),
        'dateBasis': 'JST service day, 05:00 boundary',
        'finishedAt': finished, 'sourceCount': source_count,
        'sourcePageLimit': len(SEARCH_URLS), 'sources': sources,
        'discoveredCount': discovered_count, 'eligibleCount': len(eligible),
        'attemptedCount': attempted, 'fetchedCount': fetched,
        'requests': requests,
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
              'pending': next_state['pending'],
              'component': 'official', 'exitCode': {'partial': 2, 'unavailable': 3}.get(status, 0)}
    if analyzer is not None:
        report['analysisDeferredCount'] = len(analyzer.state['queue'])
        report['analysisPendingCount'] = sum(
            item['reason'].startswith('azure_') for item in next_state['pending'])
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
    parser.add_argument('--analysis-backend', choices=('rules', 'azure'), default='rules')
    parser.add_argument('--ai-state', type=Path, help='existing private shared AI usage ledger')
    parser.add_argument('--analysis-run-id', help='shared official/personal run identifier')
    parser.add_argument('--source-state', type=Path, help='existing shared source reservation ledger')
    parser.add_argument('--source-run-id', help='shared official/personal/schedule source run identity')
    parser.add_argument('--analysis-limit', type=int, default=3, help='AI request allocation, 0..3')
    parser.add_argument('--catch-up', action='store_true', help='share the bounded recovery run budget')
    parser.add_argument('--analyze-saved', type=Path, help='known post ID to saved source payload JSON')
    parser.add_argument('--source-fetched-at', help='actual UTC raw acquisition time; required with saved input')
    buffer_mode = parser.add_mutually_exclusive_group()
    buffer_mode.add_argument('--analysis-buffer', type=Path,
                             help='same-run .cc-work buffer for at most three unissued sources')
    buffer_mode.add_argument('--replay-buffer', type=Path,
                             help='consume this run buffer without constructing a source client')
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
    source_path = args.source_state.resolve() if args.source_state else None
    if bool(source_path) != bool(args.source_run_id) or source_path and args.watch:
        raise ValueError('invalid_source_configuration')
    if publish:
        paths.add(publish)
    if not 0 <= args.analysis_limit <= 3:
        raise ValueError('invalid_analysis_limit')
    if args.analysis_backend == 'azure' and (not args.ai_state or not args.analysis_run_id):
        raise ValueError('missing_analysis_configuration')
    if args.analysis_backend == 'azure' and publish == snapshot:
        raise ValueError('private_public_path_overlap')
    if ((args.analysis_buffer or args.replay_buffer)
            and (args.analysis_backend != 'azure' or args.watch or args.dry_run or args.analyze_saved)):
        raise ValueError('invalid_analysis_buffer_mode')
    buffer_output = analysis_buffer_path(args.analysis_buffer) if args.analysis_buffer else None
    replay_path = analysis_buffer_path(args.replay_buffer) if args.replay_buffer else None
    if buffer_output and buffer_output.exists():
        raise ValueError('analysis_buffer_exists')
    if args.analyze_saved and (args.analysis_backend != 'azure' or not args.source_fetched_at
                               or args.watch):
        raise ValueError('invalid_saved_analysis')
    if args.source_fetched_at and not args.analyze_saved:
        raise ValueError('invalid_saved_analysis')
    saved_payloads = None
    if args.analyze_saved:
        saved_payloads = analysis_module().transport.strict_json(
            args.analyze_saved.read_text(encoding='utf-8-sig'))
        if not isinstance(saved_payloads, dict):
            raise ValueError('invalid_saved_analysis')
        timestamp(args.source_fetched_at)
    transport_paths = {path.with_suffix('.http-state.json') for path in paths}
    if paths & transport_paths or any(path.name.endswith('.http-state.json') for path in paths):
        raise ValueError('overlapping_storage_paths')
    if args.ai_state:
        ledger_path = args.ai_state.resolve()
        if (ledger_path in paths | transport_paths
                or ledger_path == curated.resolve()
                or any(ledger_path == path.with_suffix('.lock') for path in paths)):
            raise ValueError('overlapping_storage_paths')
    if args.analyze_saved and args.analyze_saved.resolve() in paths | transport_paths:
        raise ValueError('overlapping_storage_paths')
    if source_path:
        protected_source = paths | transport_paths | {curated.resolve()}
        protected_source.update(path.with_suffix('.lock') for path in paths)
        protected_source.update(path.resolve() for path in (
            args.ai_state, args.analyze_saved, buffer_output, replay_path) if path is not None)
        if source_path.suffix != '.json' or source_path in protected_source:
            raise ValueError('overlapping_storage_paths')
    for buffer_path in (buffer_output, replay_path):
        if buffer_path and (buffer_path in paths | transport_paths
                            or args.ai_state and buffer_path == args.ai_state.resolve()):
            raise ValueError('overlapping_storage_paths')
    if args.report:
        report_path = args.report.resolve()
        protected = {snapshot.resolve(), snapshot.with_suffix('.lock').resolve(),
                     curated.resolve()}
        protected.update(transport_paths)
        if args.ai_state:
            protected.update((args.ai_state.resolve(), Path(str(args.ai_state.resolve()) + '.lock')))
        if source_path:
            protected.update((source_path, Path(str(source_path) + '.lock')))
        if args.analyze_saved:
            protected.add(args.analyze_saved.resolve())
        protected.update(path for path in (buffer_output, replay_path) if path is not None)
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
        shared_source = None
        if source_path:
            shared_source = locks.enter_context(source_module().SharedSource(
                source_path, run_id=args.source_run_id, component='official', clock=clock, sleep=sleep,
                catch_up=args.catch_up))
        if saved_payloads is None and replay_path is None:
            client = client or PublicClient(clock=clock, sleep=sleep)
            client.shared_source = shared_source
        while True:
            state = load_snapshot(snapshot)
            if publish and publish != snapshot and publish.exists():
                state = merge_snapshots(state, load_snapshot(publish))
            replay_value = load_analysis_buffer(replay_path, state, args.analysis_run_id, clock()) if replay_path else None
            buffered_payloads = [] if buffer_output is not None else None
            limits = dict(state.get('cooldowns', {}))
            for path in transport_paths:
                for host, until in load_transport(path).items():
                    if host not in limits or timestamp(until) > timestamp(limits[host]):
                        limits[host] = until
            if shared_source is not None:
                for host, until in shared_source.state['cooldowns'].items():
                    if host not in limits or timestamp(until) > timestamp(limits[host]):
                        limits[host] = until
                for host, until in limits.items():
                    source_call(shared_source, 'set_cooldown', host, timestamp(until))

            def persist_limits(host=None, until=None):
                if host is not None:
                    value = iso(until)
                    if host not in limits or timestamp(value) > timestamp(limits[host]):
                        limits[host] = value
                    if shared_source is not None:
                        source_call(shared_source, 'set_cooldown', host, until)
                payload = {'schemaVersion': 1, 'cooldowns': dict(limits)}
                for path in sorted(transport_paths):
                    atomic_json(path, payload)

            # Preflight durable transport storage before any request. Observation
            # dry-runs still have network side effects, so limits must survive them.
            persist_limits()
            state['cooldowns'] = dict(limits)
            known = curated_ids(curated)
            start, end = date_range(args, clock())
            with ExitStack() as analysis_lock:
                analyzer = None
                if args.analysis_backend == 'azure':
                    analysis = analysis_module()
                    usage = analysis_lock.enter_context(analysis.ledger.SharedUsage(
                        args.ai_state, run_id=args.analysis_run_id, component='official',
                        clock=clock, sleep=sleep, request_limit=args.analysis_limit,
                        run_limit=analysis.ledger.CATCHUP_RUN_LIMIT if args.catch_up else analysis.ledger.RUN_LIMIT))
                    def save_analysis(partial):
                        checkpoint = copy.deepcopy(state if args.dry_run else partial)
                        checkpoint['officialAnalysis'] = copy.deepcopy(partial['officialAnalysis'])
                        pending_by_id = {item['id']: item for item in checkpoint['pending']}
                        latest = {}
                        for entry in partial['officialAnalysis']['cache'].values():
                            previous = latest.get(entry['postId'])
                            if previous is None or timestamp(entry['at']) >= timestamp(previous['at']):
                                latest[entry['postId']] = entry
                        for tid, entry in latest.items():
                            if entry['reason'] not in ('notices', 'no_event'):
                                pending_by_id[tid] = {
                                    'id': tid, 'url': canonical(tid), 'reason': entry['reason'],
                                    'firstSeenAt': entry['at'], 'lastAttemptAt': entry['at'], 'attempts': 1}
                        for tid, entry in partial['officialAnalysis']['queue'].items():
                            pending_by_id[tid] = {
                                'id': tid, 'url': canonical(tid), 'reason': entry['reason'],
                                'firstSeenAt': entry['fetchedAt'], 'lastAttemptAt': None, 'attempts': 0}
                        checkpoint['pending'] = list(pending_by_id.values())
                        atomic_json(snapshot, checkpoint)
                    analyzer = analysis.AzureAnalyzer(state, analysis_context(), os.environ, usage,
                                                       clock=clock, save=save_analysis)
                if replay_value is not None:
                    validate_analysis_buffer(replay_value, state, args.analysis_run_id, clock(), analyzer.version)
                    replay_path.unlink()
                    updated, report, code = replay_analysis_buffer(state, replay_value, analyzer, clock)
                else:
                    updated, report, code = collect(
                        state, known, client, start, end, args.max_posts, clock, on_limit=persist_limits,
                        analyzer=analyzer, saved_payloads=saved_payloads,
                        source_fetched_at=args.source_fetched_at, buffered_payloads=buffered_payloads)
                if analyzer is not None:
                    report['analysisBackend'] = 'azure'
                    report['analysisRequests'] = usage.used
            if shared_source is not None:
                report['sourceUsage'] = shared_source.report()
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
                        atomic_json(publish, {key: value for key, value in updated.items()
                                              if key != 'officialAnalysis'})
                        report['published'] = True
                    except OSError:
                        report['collectionStatus'] = report['status']
                        report.update(status='unavailable', reason='publication_failed')
                        code = 4
            if buffer_output is not None:
                atomic_json(buffer_output, make_analysis_buffer(
                    updated, args.analysis_run_id, analyzer.version, buffered_payloads, clock()))
            report['exitCode'] = code
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
    if not 0 <= args.analysis_limit <= 3:
        parser.error('--analysis-limit must be 0..3')
    try:
        date_range(args, utc_now())
        return run(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, FetchFailure, source_module().SourceFailure,
            analysis_module().ledger.UsageFailure) as exc:
        reason = 'local_io_error' if isinstance(exc, OSError) else str(exc)
        if not re.fullmatch(r'[a-z_]+', reason):
            reason = 'invalid_local_data'
        print(json.dumps({'status': 'unavailable', 'reason': reason, 'exitCode': 4}))
        return 4


if __name__ == '__main__':
    sys.exit(main())
