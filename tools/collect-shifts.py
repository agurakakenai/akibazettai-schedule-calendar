"""Collect independently observed public shift facts; never change curated data.

Only two Yahoo realtime keyword pages and the existing public post fetcher are
used. Default dates are the last two JST service days (a day starts at 05:00).
lastSuccessAt means a completed run with both searches and all selected posts
successfully handled, including no-new/no-results; partial runs do not advance it.
Optional --refresh-known shares the request cap and selects today's unchecked
legacy posts first, then the current shift, within the JST service day. It
reserves a 30-minute recheck interval even for dry runs.
Footer notices are plans, not roster attendance; revisions are extraction
updates, not evidence that X marked a post as edited.
Verified root edit_control can enqueue one bounded follow-up GET per run.
Unknown metadata, unparseable latest versions and cross-service-day chains
remain pending; no unfetched ID supersedes a saved post.
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


class StaleEditResponse(FetchFailure):
    def __init__(self, edit_ids):
        super().__init__('stale_edit_response')
        self.latest_id = edit_ids[-1]
        self.edit_ids = tuple(edit_ids)


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


def checked_edit_ids(ids):
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_POSTS
            or any(not isinstance(tid, str) or not post_id(tid) for tid in ids)
            or any(int(first) >= int(second) for first, second in zip(ids, ids[1:]))):
        raise ValueError('invalid_edit_chain')
    return ids


def same_edit_service_day(ids, created):
    day = service_day(created)
    return all(service_day(snowflake_time(tid)) == day for tid in ids)


def validate_edit_tweet_ids(post, known_posts=None):
    if 'editTweetIds' in post:
        ids = checked_edit_ids(post['editTweetIds'])
        if ids[-1] != post['id']:
            raise ValueError('invalid_edit_chain')
        if not same_edit_service_day(ids, timestamp(post['createdAt'])):
            raise ValueError('edit_date_mismatch')
        if known_posts is not None:
            # The existing two-second clock tolerance must not move a saved source across 05:00.
            day = service_day(timestamp(post['createdAt']))
            for tid in ids:
                source = known_posts.get(tid)
                if source is not None and service_day(timestamp(source['createdAt'])) != day:
                    raise ValueError('edit_date_mismatch')


def response_edit_ids(tid, value, now):
    stale = value.get('isStaleEdit', False)
    if not isinstance(stale, bool) or ('isEdited' in value and not isinstance(value['isEdited'], bool)):
        raise FetchFailure('invalid_edit_metadata')
    if 'edit_control' not in value:
        if value.get('isEdited'):
            raise FetchFailure('invalid_edit_metadata')
        if stale:
            raise FetchFailure('stale_edit_response')
        return None
    control = value['edit_control']
    try:
        if not isinstance(control, dict) or not isinstance(control.get('edit_tweet_ids'), list):
            raise ValueError
        ids = [post_id(item) for item in control['edit_tweet_ids']]
        checked_edit_ids(ids)
        if tid not in ids or any(snowflake_time(item) > now for item in ids):
            raise ValueError
        if not same_edit_service_day(ids, timestamp(value['created_at'])):
            raise FetchFailure('edit_date_mismatch')
    except (ValueError, OverflowError, OSError):
        raise FetchFailure('invalid_edit_metadata') from None
    if ids[-1] != tid:
        raise StaleEditResponse(ids)
    if stale:
        raise FetchFailure('invalid_edit_metadata')
    return ids


def superseded_ids(posts):
    posts = list(posts)
    known_posts = {post['id']: post for post in posts}
    result = set()
    for post in posts:
        try:
            validate_edit_tweet_ids(post, known_posts)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        result.update(post.get('editTweetIds', [])[:-1])
    return result


def current_notice_count(posts):
    posts = list(posts)
    superseded = superseded_ids(posts)
    return sum(len(post.get('notices', [])) for post in posts if post['id'] not in superseded)


def merge_edit_ids(first, second):
    a, b = first.get('editTweetIds'), second.get('editTweetIds')
    if a is None or b is None:
        return copy.deepcopy(a if b is None else b)
    if set(a) <= set(b):
        return copy.deepcopy(b)
    if set(b) <= set(a):
        return copy.deepcopy(a)
    raise ValueError('observation_conflict')


def notice_name_evidence():
    counts, debuts = IMPORTER.load_known()
    evidence = {name: name for name in set(counts) | debuts | set(IMPORTER.CONFIRMED_NAMES)}
    try:
        source = (ROOT / 'data' / 'schedule.js').read_text(encoding='utf-8-sig')
        roster = re.search(r'\broster:\s*\[(.*?)\]', source, re.S)
        if roster:
            evidence.update({name: name for name in re.findall(r'"([^"]+)"', roster[1])})
        source = (ROOT / 'data' / 'store-insights.js').read_text(encoding='utf-8-sig')
        insights = json.loads(source.split('window.STORE_INSIGHTS =', 1)[1].strip().removesuffix(';'))
        for name, item in insights.get('maidTendency', {}).items():
            evidence.setdefault(name, name)
            alias = item.get('alias')
            if isinstance(alias, str) and alias:
                if alias in evidence and evidence[alias] is None:
                    continue
                previous = evidence.get(alias)
                evidence[alias] = name if previous in (None, alias, name) else None
    except (OSError, ValueError, IndexError, AttributeError):
        # Missing supplementary evidence cannot authorize a guessed name.
        pass
    return {name: canonical_name for name, canonical_name in evidence.items()
            if canonical_name and re.fullmatch(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', name)}


def parse_notices(text, created, date, shift, store_id, names, evidence=None):
    """Extract only named future arrivals from a separate official footer."""
    if not re.search(r'あとから|後から|遅れて', text):
        return []
    blocks = re.split(r'\n\s*\n', text.replace('\r\n', '\n'))
    roster_blocks = []
    for index, block in enumerate(blocks):
        for line in block.splitlines():
            name = re.match(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', line.strip())
            if name and name[0] in names:
                roster_blocks.append(index)
                break
    if not roster_blocks:
        return []
    footer = '\n'.join(blocks[max(roster_blocks) + 1:])
    if (not footer or re.search(
            r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f「」『』“”"?？>]|昨日|明日|明後日|あした|きのう|来週|'
            r'ではな|じゃな|ない|ません|中止|訂正|撤回|勘違い|間違い|'
            r'によると|と聞|らしい|とのこと|友達|友人|引用|転載|RT\s*@', footer)):
        return []
    if '今日' in footer and created.astimezone(JST).date().isoformat() != date:
        return []
    target = dt.date.fromisoformat(date)
    for month, day in re.findall(r'(?<!\d)(\d{1,2})(?:月|/)(\d{1,2})(?:日)?', footer):
        if (int(month), int(day)) != (target.month, target.day):
            return []
    normalized = IMPORTER.norm(footer)
    explicit_stores = {'s' + number for number in re.findall(r'([1-9])号店', normalized)}
    explicit_stores.update(STORE_IDS[label] for needle, label in IMPORTER.STORES
                           if IMPORTER.norm(needle) in normalized)
    if explicit_stores - {store_id}:
        return []
    if ((shift == '昼' and re.search(r'夜|よる|ヨル', footer))
            or (shift == '夜' and re.search(r'昼|ひる|ヒル', footer))):
        return []
    evidence = notice_name_evidence() if evidence is None else evidence
    evidence = evidence if isinstance(evidence, dict) else {name: name for name in evidence}
    known = [name for name, canonical_name in evidence.items() if canonical_name]
    if not known:
        return []
    roster_names = {evidence.get(name, name) for name in names}
    pattern = re.compile(
        r'^(?:[1-4]号店(?:の|に|へ)?\s*)?(?P<name>'
        + '|'.join(re.escape(name) for name in sorted(known, key=len, reverse=True))
        + r')(?:ちゃん)?(?:も|は|が)?\s*'
        r'(?:(?P<time>[0-9０-９]{1,2}(?:[:：][0-9０-９]{2}|時(?:[0-9０-９]{1,2}分)?))'
        r'\s*(?:から|に)?\s*)?'
        r'(?:(?:あとから|後から)(?:来る|くる|来ます|きます|合流)|遅れて(?:合流|来る|くる|来ます))'
        r'(?:にゃんね|にゃん|します|です|予定です)?[^\wぁ-んァ-ヶ一-龠ー]*$')
    result, ambiguous = {}, set()
    for clause in re.split(r'[\n。！!、]+', footer.replace('⊂(´ω´⊂)))', '')):
        clause = clause.strip()
        match = pattern.fullmatch(clause)
        if not match:
            continue
        name = match['name']
        if evidence[name] in roster_names:
            continue
        notice = {'name': name, 'kind': 'late', 'excerpt': ' '.join(clause.split())[:160]}
        if match['time']:
            raw = IMPORTER.norm(match['time'])
            when = re.fullmatch(r'(\d{1,2})(?::(\d{2})|時(?:(\d{1,2})分)?)', raw)
            if not when or int(when[1]) > 23 or int(when[2] or when[3] or 0) > 59:
                continue
            notice['time'] = f'{int(when[1]):02d}:{int(when[2] or when[3] or 0):02d}'
        if name in result and result[name].get('time') != notice.get('time'):
            ambiguous.add(name)
        result.setdefault(name, notice)
    return [notice for name, notice in result.items() if name not in ambiguous]


def roster_text_without_arrival_footer(text):
    blocks = re.split(r'\n\s*\n', text.replace('\r\n', '\n'))
    kept = [block for index, block in enumerate(blocks)
            if index < 2 or not re.search(r'あとから|後から|遅れて', block)]
    # Short prose such as "みりあちゃんも遅れて合流" otherwise fits the
    # importer's lexical name shape. Only separate footer paragraphs are removed.
    return text if len(kept) == len(blocks) else '\n\n'.join(kept)


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
    edit_ids = response_edit_ids(tid, value, now)
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
    parsed = IMPORTER.parse(roster_text_without_arrival_footer(text), value['created_at'])
    if parsed is None:
        raise FetchFailure('parse_failed')
    date, store, shift, names = parsed
    if date != service_day(created).isoformat() or store not in STORE_IDS:
        raise FetchFailure('parsed_metadata_mismatch')
    post = {
        'id': tid, 'url': canonical(tid), 'authorId': AUTHOR_ID,
        'authorScreenName': AUTHOR, 'createdAt': iso(created), 'date': date,
        'shift': {'ひる': '昼', 'よる': '夜'}[shift],
        'storeId': STORE_IDS[store], 'names': names, 'observedAt': iso(now),
    }
    if edit_ids is not None:
        post['editTweetIds'] = edit_ids
    if not any(value.get(key) for key in (
            'quoted_tweet', 'quoted_status', 'in_reply_to_status_id_str', 'in_reply_to_status_id')):
        notices = parse_notices(text, created, date, post['shift'], post['storeId'], names)
        if notices:
            post['notices'] = notices
    return post


def extracted_facts(post):
    return {**{key: copy.deepcopy(post[key]) for key in ('date', 'shift', 'storeId', 'names')},
            'notices': copy.deepcopy(post.get('notices', []))}


def version_facts(post):
    return {**extracted_facts(post), 'observedAt': post['observedAt']}


def version_key(version):
    return (timestamp(version['observedAt']),
            json.dumps(extracted_facts(version), ensure_ascii=False, sort_keys=True))


def last_checked(post):
    return timestamp(post.get('lastCheckedAt', post['observedAt']))


def checked_iso(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def validate_notices(notices):
    if not isinstance(notices, list):
        raise ValueError
    seen = set()
    for item in notices:
        if (not isinstance(item, dict)
                or set(item) - {'name', 'kind', 'excerpt', 'time'}
                or not {'name', 'kind', 'excerpt'} <= set(item)
                or not isinstance(item['name'], str)
                or not re.fullmatch(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', item['name'])
                or item['name'] in seen or item['kind'] != 'late'
                or not isinstance(item['excerpt'], str) or not item['excerpt'].strip()
                or len(item['excerpt']) > 160
                or re.search(r'[\x00-\x1f\x7f\u2028\u2029]', item['excerpt'])
                or ('time' in item and (not isinstance(item['time'], str)
                    or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', item['time'])))):
            raise ValueError
        seen.add(item['name'])


def validate_extraction_history(post):
    validate_edit_tweet_ids(post)
    validate_notices(post.get('notices', []))
    observed = timestamp(post['observedAt'])
    created = timestamp(post['createdAt'])
    if observed < created or last_checked(post) < observed:
        raise ValueError
    revisions = post.get('revisions', [])
    if not isinstance(revisions, list):
        raise ValueError
    previous = None
    for revision in revisions:
        if (not isinstance(revision, dict)
                or set(revision) != {'date', 'shift', 'storeId', 'names', 'notices', 'observedAt'}
                or revision['date'] != post['date']
                or revision['shift'] not in ('昼', '夜')
                or revision['storeId'] not in STORE_IDS.values()
                or not isinstance(revision['names'], list) or not revision['names']
                or any(not isinstance(name, str) or not name for name in revision['names'])):
            raise ValueError
        when = timestamp(revision['observedAt'])
        if when < created or when >= observed or (previous is not None and when <= previous):
            raise ValueError
        validate_notices(revision['notices'])
        previous = when


def refresh_candidates(state, start, end, now, limit):
    day = service_day(now)
    shift = '昼' if 5 <= now.astimezone(JST).hour < 17 else '夜'
    attempts = {item['id']: timestamp(item['lastAttemptAt'])
                for item in state['pending']
                if item['reason'].startswith('refresh_') and item.get('lastAttemptAt')}
    excluded = superseded_ids(state['posts']) | {
        item['editSourceId'] for item in state['pending'] if item.get('editSourceId')}
    if not start <= day <= end:
        return []
    posts = [post for post in state['posts']
             if post['id'] not in excluded
             and post['date'] == day.isoformat()
             and ('lastCheckedAt' not in post or post['shift'] == shift)
             and now - max(last_checked(post), attempts.get(post['id'], last_checked(post)))
             >= dt.timedelta(minutes=30)]
    return sorted(posts, key=lambda post: (
        'lastCheckedAt' in post,
        max(last_checked(post), attempts.get(post['id'], last_checked(post))),
        int(post['id'])))[:limit]


def refreshed_post(previous, current, checked):
    for key in ('id', 'url', 'authorId', 'authorScreenName', 'date'):
        if previous[key] != current[key]:
            raise FetchFailure('refresh_metadata_mismatch')
    if timestamp(previous['createdAt']) != timestamp(current['createdAt']):
        raise FetchFailure('refresh_metadata_mismatch')
    if 'editTweetIds' in previous and 'editTweetIds' not in current:
        raise FetchFailure('edit_metadata_missing')
    if ('editTweetIds' in previous and 'editTweetIds' in current
            and not set(previous['editTweetIds']) <= set(current['editTweetIds'])):
        raise FetchFailure('edit_chain_conflict')
    changed = extracted_facts(previous) != extracted_facts(current)
    if changed:
        result = copy.deepcopy(current)
        result['revisions'] = [*copy.deepcopy(previous.get('revisions', [])), version_facts(previous)]
    else:
        result = copy.deepcopy(previous)
    edit_ids = merge_edit_ids(previous, current)
    if edit_ids is not None:
        result['editTweetIds'] = edit_ids
    result['lastCheckedAt'] = checked_iso(checked)
    return result, changed


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
        for field in ('refreshedCount', 'updatedPostCount', 'noticeCount'):
            if field in state['lastRun'] and (
                    type(state['lastRun'][field]) is not int or state['lastRun'][field] < 0):
                raise ValueError
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
            validate_extraction_history(post)
            ids.add(tid)
        known_posts = {post['id']: post for post in state['posts']}
        for post in state['posts']:
            validate_edit_tweet_ids(post, known_posts)
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
            if 'editSourceId' in item and (
                    not isinstance(item['editSourceId'], str)
                    or not post_id(item['editSourceId'])
                    or int(item['editSourceId']) >= int(tid)):
                raise ValueError
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


def merge_post_versions(first, second):
    for key in ('id', 'url', 'authorId', 'authorScreenName', 'date'):
        if first[key] != second[key]:
            raise ValueError('observation_conflict')
    if timestamp(first['createdAt']) != timestamp(second['createdAt']):
        raise ValueError('observation_conflict')
    current_equal = extracted_facts(first) == extracted_facts(second)
    older, newer = sorted((first, second), key=lambda post: timestamp(post['observedAt']))
    if not current_equal:
        old_version = version_key(older)
        if (old_version not in {version_key(item) for item in newer.get('revisions', [])}
                or last_checked(older) > last_checked(newer)):
            raise ValueError('observation_conflict')
    merged = copy.deepcopy(newer if not current_equal or newer.get('revisions') else first)
    edit_ids = merge_edit_ids(first, second)
    if edit_ids is not None:
        merged['editTweetIds'] = edit_ids
    if current_equal and not merged.get('revisions'):
        merged['observedAt'] = min((first['observedAt'], second['observedAt']), key=timestamp)
    history = {}
    at_time = {}
    for item in [*first.get('revisions', []), *second.get('revisions', [])]:
        key = version_key(item)
        if key[0] in at_time and at_time[key[0]] != key:
            raise ValueError('observation_conflict')
        at_time[key[0]] = key
        history[key] = copy.deepcopy(item)
    if history:
        merged['revisions'] = [history[key] for key in sorted(history)]
    if 'lastCheckedAt' in first or 'lastCheckedAt' in second:
        checked = max(last_checked(first), last_checked(second)) if current_equal else last_checked(newer)
        merged['lastCheckedAt'] = checked_iso(checked)
    validate_extraction_history(merged)
    return merged


def merge_snapshots(primary, published):
    """Merge extraction lineages, never union names from different versions."""
    known_posts = {post['id']: post for post in [*primary['posts'], *published['posts']]}
    for state in (primary, published):
        for post in state['posts']:
            validate_edit_tweet_ids(post, known_posts)
    result = copy.deepcopy(primary)
    posts = {post['id']: post for post in result['posts']}
    for post in published['posts']:
        existing = posts.get(post['id'])
        if existing is not None:
            posts[post['id']] = merge_post_versions(existing, post)
        else:
            posts[post['id']] = copy.deepcopy(post)
    result['posts'] = list(posts.values())
    superseded = superseded_ids(result['posts'])
    resolved = {}
    for item in [*primary.get('resolved', []), *published.get('resolved', [])]:
        if item['id'] not in posts:
            previous = resolved.get(item['id'])
            if previous is None or timestamp(item['resolvedAt']) > timestamp(previous['resolvedAt']):
                resolved[item['id']] = copy.deepcopy(item)
    pending = {}
    for item in [*primary['pending'], *published['pending']]:
        post = posts.get(item['id'])
        refresh_pending = (post is not None and item['reason'].startswith('refresh_')
                           and item.get('lastAttemptAt')
                           and timestamp(item['lastAttemptAt']) > last_checked(post))
        edit_pending = (bool(item.get('editSourceId')) and item['id'] not in superseded
                        and not (post and item['editSourceId'] in post.get('editTweetIds', [])[:-1]))
        if edit_pending:
            # Even a newer general non-shift verdict has no proof about this edit source.
            resolved.pop(item['id'], None)
        if ((post is not None and not refresh_pending and not edit_pending)
                or item['id'] in resolved or item['id'] in superseded):
            continue
        previous = pending.get(item['id'])
        when = timestamp(item.get('lastAttemptAt') or item['firstSeenAt'])
        if previous is None or when >= timestamp(previous.get('lastAttemptAt') or previous['firstSeenAt']):
            pending[item['id']] = copy.deepcopy(item)
        sources = [entry['editSourceId'] for entry in (previous, item)
                   if entry and entry.get('editSourceId')]
        if sources:
            pending[item['id']]['editSourceId'] = min(sources, key=int)
    result['pending'] = list(pending.values())
    result['resolved'] = list(resolved.values())
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


def collect(state, known, client, start, end, max_posts, clock=utc_now, on_limit=None,
            refresh_known=0, on_refresh_attempt=None):
    if type(refresh_known) is not int or not 0 <= refresh_known <= 3:
        raise ValueError('invalid_refresh_limit')
    checked = clock()
    next_state = copy.deepcopy(state)
    original_posts = {post['id']: post for post in state['posts']}
    present = set(original_posts)
    superseded = superseded_ids(state['posts'])
    resolved = {item['id']: copy.deepcopy(item) for item in state.get('resolved', [])}
    pending = {item['id']: copy.deepcopy(item) for item in state['pending']
               if item['id'] not in superseded and (
                   item['id'] not in present | known | resolved.keys()
                   or item.get('editSourceId')
                   or (item['id'] in present and item['reason'].startswith('refresh_')))}
    for item in pending.values():
        if item.get('editSourceId'):
            resolved.pop(item['id'], None)
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
    waiting_sources = {item['editSourceId'] for item in pending.values() if item.get('editSourceId')}
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
        if tid in superseded:
            skipped_resolved += 1
        elif tid in waiting_sources:
            continue
        elif tid in known:
            skipped_curated += 1
        elif pending.get(tid, {}).get('editSourceId'):
            eligible.append(tid)
        elif tid in present:
            skipped_observed += 1
        elif tid in resolved:
            skipped_resolved += 1
        else:
            eligible.append(tid)
    # Retry older failed attempts first; no cursor can silently skip a failure.
    eligible.sort(key=lambda tid: (
        tid in present, tid not in pending,
        pending.get(tid, {}).get('lastAttemptAt') or '', -int(tid)))
    attempted = fetched = handled = refreshed = updated = known_attempted = 0
    initial_deferred = refresh_deferred = 0
    replacements = {}
    attempted_ids = set()
    edit_queue = []
    followed = False
    host_stopped = limited(POST_HOST)

    def working_posts():
        return [replacements.get(post['id'], post) for post in state['posts']] + new_posts

    def working_state():
        return {'posts': working_posts(), 'pending': list(pending.values())}

    def pending_record(tid):
        previous = pending.get(tid, {})
        item = {
            'id': tid, 'url': canonical(tid),
            'firstSeenAt': previous.get('firstSeenAt', original_posts.get(tid, {}).get('observedAt', iso(checked))),
            'lastAttemptAt': previous.get('lastAttemptAt'),
            'attempts': previous.get('attempts', 0)}
        if previous.get('editSourceId'):
            item['editSourceId'] = previous['editSourceId']
        return item

    def queue_latest(source_id, exc):
        if not isinstance(exc, StaleEditResponse) or source_id in superseded_ids(working_posts()):
            return
        tid = exc.latest_id
        item = pending_record(tid)
        item['editSourceId'] = min((source_id, item.get('editSourceId', source_id)), key=int)
        item['reason'] = 'refresh_edit_latest_unverified' if tid in present else 'edit_latest_unverified'
        if tid in known:
            item['reason'] = 'edit_latest_curated'
        pending[tid] = item
        resolved.pop(tid, None)
        if tid not in edit_queue:
            edit_queue.append(tid)

    def ready_known(tid):
        if tid in known or not start <= service_day(snowflake_time(tid)) <= end:
            return False
        if tid not in present:
            return True
        return (known_attempted < refresh_known and tid in {
            post['id'] for post in refresh_candidates(working_state(), start, end, clock(), len(present))})

    def fetch_one(tid):
        nonlocal attempted, fetched, handled, refreshed, updated, known_attempted, host_stopped
        previous = original_posts.get(tid)
        item = pending_record(tid)
        item['lastAttemptAt'] = checked_iso(clock())
        item['attempts'] += 1
        if previous is not None and on_refresh_attempt is not None:
            on_refresh_attempt(tid, item['lastAttemptAt'])
        attempted += 1
        attempted_ids.add(tid)
        if previous is not None:
            known_attempted += 1
        try:
            value = client.fetch_post(tid)
            fetched += 1
            post = validate_post(tid, value, start, end, clock())
            if post is not None:
                try:
                    validate_edit_tweet_ids(post, {item['id']: item for item in working_posts()})
                except ValueError:
                    raise FetchFailure('edit_date_mismatch') from None
            if item.get('editSourceId'):
                source_id = item['editSourceId']
                if post is None:
                    raise FetchFailure('edit_latest_unparsed')
                if source_id not in post.get('editTweetIds', [])[:-1]:
                    raise FetchFailure('edit_chain_unconfirmed')
                if post['date'] != service_day(snowflake_time(source_id)).isoformat():
                    raise FetchFailure('edit_date_mismatch')
            if post is None:
                if previous is not None:
                    raise FetchFailure('not_shift_post')
                rejected.append({'id': tid, 'reason': 'not_shift_post'})
                resolved[tid] = {'id': tid, 'url': canonical(tid),
                                 'reason': 'not_shift_post', 'resolvedAt': iso(clock())}
            elif previous is not None:
                replacement, changed = refreshed_post(previous, post, clock())
                replacements[tid] = replacement
                refreshed += 1
                updated += int(changed)
            else:
                if refresh_known or post.get('editTweetIds'):
                    post['lastCheckedAt'] = checked_iso(clock())
                new_posts.append(post)
            handled += 1
            pending.pop(tid, None)
        except FetchFailure as exc:
            if isinstance(exc, StaleEditResponse) and item.get('editSourceId'):
                if item['editSourceId'] not in exc.edit_ids:
                    exc = FetchFailure('edit_chain_unconfirmed')
                elif service_day(snowflake_time(item['editSourceId'])) != service_day(snowflake_time(tid)):
                    exc = FetchFailure('edit_date_mismatch')
            remember_limit(POST_HOST, exc)
            item.update(exc.facts())
            if previous is not None:
                item['reason'] = 'refresh_' + exc.reason
            pending[tid] = item
            failures.append({'id': tid, 'url': canonical(tid), **exc.facts()})
            queue_latest(tid, exc)
            if exc.status in (403, 429) or exc.reason == 'host_rate_limited':
                host_stopped = True

    for tid in eligible:
        if tid in superseded_ids(working_posts()):
            pending.pop(tid, None)
            skipped_resolved += 1
            continue
        if tid in {item.get('editSourceId') for item in pending.values()}:
            continue
        if attempted >= max_posts or host_stopped or not ready_known(tid):
            item = pending_record(tid)
            item['reason'] = 'host_rate_limited' if host_stopped else 'post_limit'
            if tid in present:
                item['reason'] = 'refresh_' + item['reason']
            if host_stopped and POST_HOST in cooldowns:
                item['retryAt'] = iso(cooldowns[POST_HOST])
            elif pending.get(tid, {}).get('retryAt'):
                item['retryAt'] = pending[tid]['retryAt']
            pending[tid] = item
            initial_deferred += 1
            continue
        fetch_one(tid)

    def follow_latest():
        nonlocal followed
        if followed or host_stopped or attempted >= max_posts:
            return
        for tid in edit_queue:
            if (tid in attempted_ids or tid not in pending
                    or tid in superseded_ids(working_posts()) or not ready_known(tid)):
                continue
            followed = True
            fetch_one(tid)
            break

    follow_latest()
    refresh_limit = min(refresh_known - known_attempted, max_posts - attempted)
    selections = [
        post for post in refresh_candidates(working_state(), start, end, clock(), len(present))
        if post['id'] not in known
    ][:refresh_limit] if refresh_limit else []
    for index, previous in enumerate(selections):
        if attempted >= max_posts or known_attempted >= refresh_known:
            break
        if host_stopped or limited(POST_HOST):
            refresh_deferred += len(selections) - index
            break
        # A watch run may cross the service-day/shift boundary while fetching new posts.
        tid = previous['id']
        if tid in attempted_ids or not ready_known(tid):
            continue
        fetch_one(tid)
        follow_latest()
    superseded = superseded_ids(working_posts())
    pending = {tid: item for tid, item in pending.items() if tid not in superseded}
    failures = [item for item in failures
                if not (item['id'] in superseded and item['reason'] == 'stale_edit_response')]
    source_count = sum(source['status'] == 'ok' for source in sources)
    deferred = initial_deferred + refresh_deferred + sum(
        tid in pending and tid not in attempted_ids and tid not in eligible for tid in edit_queue)
    if source_count == 0 and handled == 0:
        status = 'unavailable'
    elif source_count != len(SEARCH_URLS) or failures or deferred or pending:
        status = 'partial'
    elif new_posts or updated:
        status = 'ok'
    elif skipped_curated or skipped_observed or skipped_resolved or handled:
        status = 'no-new'
    else:
        status = 'no-results'
    finished = iso(clock())
    next_state['checkedAt'] = iso(checked)
    if status in ('ok', 'no-new', 'no-results'):
        next_state['lastSuccessAt'] = finished
    next_state['posts'] = [replacements.get(post['id'], post) for post in next_state['posts']]
    next_state['posts'].extend(new_posts)
    next_state['posts'].sort(key=lambda post: (post['createdAt'], int(post['id'])))
    next_state['pending'] = sorted(pending.values(), key=lambda item: int(item['id']))
    next_state['resolved'] = sorted(resolved.values(), key=lambda item: int(item['id']))
    next_state['cooldowns'] = {host: iso(until) for host, until in cooldowns.items()
                               if until > clock()}

    def outside_requested_range(tid):
        try:
            return not start <= service_day(snowflake_time(tid)) <= end
        except (ValueError, OverflowError, OSError):
            return True

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
        'pendingOutsideRangeCount': sum(outside_requested_range(tid) for tid in pending),
        'maxPosts': max_posts, 'failures': failures, 'rejected': rejected,
        'complete': False,
        'lastSuccessMeaning': 'Both searches and every selected post handled without failure or deferral',
    }
    notice_count = current_notice_count(next_state['posts'])
    if refresh_known or notice_count or refreshed or superseded:
        next_state['lastRun'].update(
            refreshedCount=refreshed, updatedPostCount=updated, noticeCount=notice_count)
    report = {'schemaVersion': 1, 'checkedAt': next_state['checkedAt'],
              'lastSuccessAt': next_state['lastSuccessAt'],
              **next_state['lastRun'], 'newFacts': new_posts,
              'pending': next_state['pending']}
    return next_state, report, {'partial': 2, 'unavailable': 3}.get(status, 0)


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='leave facts unchanged; persist HTTP cooldowns and known-post request reservations')
    parser.add_argument('--days', type=int, default=2,
                        help='inclusive JST 05:00 service days (default: 2)')
    parser.add_argument('--date-from', help='inclusive service date YYYY-MM-DD')
    parser.add_argument('--date-to', help='inclusive service date YYYY-MM-DD')
    parser.add_argument('--max-posts', type=int, default=20,
                        help='individual request cap, 1..20 (default: 20)')
    parser.add_argument('--refresh-known', type=int, default=0,
                        help="recheck up to 0..3 posts: today's unchecked legacy first, "
                             'then current shift; at least 30 minutes apart')
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
            refresh_safety = copy.deepcopy(state)

            def reserve_refresh(tid, at):
                pending = {item['id']: item for item in refresh_safety['pending']}
                previous = pending.get(tid, {})
                pending[tid] = {
                    'id': tid, 'url': canonical(tid), 'reason': 'refresh_requested',
                    'firstSeenAt': previous.get('firstSeenAt', at),
                    'lastAttemptAt': at, 'attempts': previous.get('attempts', 0) + 1}
                if previous.get('editSourceId'):
                    pending[tid]['editSourceId'] = previous['editSourceId']
                refresh_safety['pending'] = list(pending.values())
                atomic_json(snapshot, refresh_safety)

            updated, report, code = collect(
                state, known, client, start, end, args.max_posts, clock, on_limit=persist_limits,
                refresh_known=getattr(args, 'refresh_known', 0), on_refresh_attempt=reserve_refresh)
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
    if not 0 <= args.refresh_known <= 3:
        parser.error('--refresh-known must be 0..3')
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
