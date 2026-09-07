"""Bounded original-post/photo half-month producer; raw inputs are transient only."""
import argparse
from contextlib import ExitStack
import csv
import datetime as dt
import email.utils
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def _module(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


facts = _module('half-month-schedules.py', 'half_month_facts')
azure = _module('schedule-azure.py', 'half_month_azure')
official = _module('collect-shifts.py', 'half_month_official')
personal = _module('collect-personal-shifts.py', 'half_month_personal')
yahoo = _module('yahoo-search.py', 'half_month_yahoo')
Failure = official.FetchFailure
MAX_DOCUMENT_BYTES = 4_000_000
TIMEOUT = 35
PHOTO_HOST = 'pbs.twimg.com'
POST_HOST = 'cdn.syndication.twimg.com'
SEARCH_HOST = 'search.yahoo.co.jp'


def author_id(value):
    if type(value) not in (str, int):
        return None
    try:
        return facts.identifier(str(value))
    except ValueError:
        return None


def matching_author(value, expected):
    supplied = [value[key] for key in ('id_str', 'id') if key in value]
    return bool(supplied) and all(author_id(item) == expected for item in supplied)


def media_url(url):
    """Return (asset, MIME, variant), rejecting all non-photo routes/parameters."""
    if not isinstance(url, str) or re.search(r'[\x00-\x20\x7f]', url):
        raise ValueError('image_url_refused')
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != PHOTO_HOST or parsed.fragment
                or parsed.username or parsed.password or parsed.port is not None
                or '%' in parsed.path or '\\' in url):
            raise ValueError
        match = re.fullmatch(r'/media/([A-Za-z0-9_-]{1,128})(?:\.(jpg|jpeg|png))?(?::(orig|large|medium|small|thumb))?',
                             parsed.path)
        if not match:
            raise ValueError
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        if any(len(items) != 1 for items in query.values()) or set(query) - {'format', 'name'}:
            raise ValueError
        extension = match[2]
        format_ = query.get('format', [extension])[0]
        variant = query.get('name', [match[3] or 'orig'])[0]
        if (format_ not in ('jpg', 'jpeg', 'png') or extension and 'format' in query
                or match[3] and query or not extension and set(query) != {'format', 'name'}
                or variant not in ('orig', 'large', 'medium', 'small', 'thumb', '4096x4096')):
            raise ValueError
        return match[1], 'image/png' if format_ == 'png' else 'image/jpeg', variant
    except (ValueError, TypeError):
        raise ValueError('image_url_refused') from None


def selected_image_url(url):
    asset, mime, _ = media_url(url)
    return f'https://{PHOTO_HOST}/media/{asset}?format={"png" if mime == "image/png" else "jpg"}&name=medium'


def discover(document, targets, now, bindings=None):
    """Yahoo bestTweet + timeline, but a half-period-specific publication window."""
    if not isinstance(document, str) or len(document.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise ValueError('invalid_search_response')
    page = yahoo.search_page(document)
    error = page.get('searchError')
    if personal.DENIAL.search(json.dumps(error, ensure_ascii=False)):
        raise Failure('access_denied', status=200)
    if error is not None and (not isinstance(error, dict) or
                             error.get('errorType') not in (None, '', 'zeromatch')):
        raise ValueError('invalid_search_response')
    entries = yahoo.candidate_entries(page)
    handles = {target['handle']: target for target in targets.values()}
    candidates, conflicts = {}, set()
    start = facts.candidate_start(now)
    evidence_hash = facts.digest(document.encode('utf-8'))
    for entry in entries[:200]:
        handle = entry.get('screenName')
        if not isinstance(handle, str) or handle not in handles:
            continue
        tid, uid = official.post_id(entry.get('id')), author_id(entry.get('userId'))
        epoch = entry.get('createdAt')
        if (not tid or not uid or uid == official.AUTHOR_ID or type(epoch) not in (int, str)
                or not re.fullmatch(r'\d{10}', str(epoch))):
            continue
        if any(entry.get(key) for key in (
                'retweetedStatus', 'retweeted_status', 'quotedTweet', 'quoted_tweet',
                'inReplyToStatusId', 'in_reply_to_status_id', 'isRetweet', 'isQuote', 'isReply')):
            continue
        try:
            when = dt.datetime.fromtimestamp(int(epoch), facts.UTC)
            parsed = urllib.parse.urlsplit(official.unescape_urls(entry.get('url', '')))
            if (parsed.scheme != 'https' or parsed.hostname not in ('x.com', 'twitter.com')
                    or parsed.username or parsed.password or parsed.port is not None or parsed.fragment
                    or parsed.path != f'/{handle}/status/{tid}'
                    or not start <= when.astimezone(facts.JST).date() or when > now
                    or abs((official.snowflake_time(tid) - when).total_seconds()) >= 2):
                continue
            target = handles[handle]
            bound = (bindings or {}).get(target['name'])
            if bound and (bound['authorId'] != uid or bound['authorScreenName'] != handle):
                continue
            candidate = {'id': tid, 'url': facts.public_url(handle, tid), 'name': target['name'],
                         'authorId': uid, 'authorScreenName': handle,
                         'searchCreatedAt': facts.stamp(when), 'discoveredAt': facts.stamp(now),
                         'discoveryHash': evidence_hash,
                         'lastAttemptAt': None, 'nextAttemptAt': None,
                         'priority': 0 if re.search(r'予定|お給仕|前半|後半|シフト',
                                                   str(entry.get('text', entry.get('body', '')))) else 1}
            facts.validate_candidate(candidate)
            if tid in candidates:
                if any(candidates[tid][key] != candidate[key] for key in (
                        'name', 'authorId', 'authorScreenName', 'url', 'searchCreatedAt')):
                    conflicts.add(tid)
                candidate['priority'] = min(candidate['priority'], candidates[tid]['priority'])
            candidates[tid] = candidate
        except (ValueError, TypeError, OverflowError, OSError):
            continue
    return [item for tid, item in candidates.items() if tid not in conflicts], len(entries) > 200


def validate_post(candidate, payload, target, now, binding=None, *, payload_hash=None):
    """Verify only the original author's top-level media, without daily-date gates."""
    facts.validate_candidate(candidate)
    tid, uid, handle = candidate['id'], candidate['authorId'], candidate['authorScreenName']
    if not isinstance(payload, dict) or not official.matching_id(payload, tid):
        raise ValueError('response_id_mismatch')
    author = payload.get('user')
    if (not isinstance(author, dict) or not matching_author(author, uid)
            or author.get('screen_name') != handle or uid == official.AUTHOR_ID
            or candidate['name'] != target['name'] or handle != target['handle']
            or binding and (binding['authorId'] != uid or binding['authorScreenName'] != handle)):
        raise ValueError('author_mismatch')
    created = official.timestamp(payload.get('created_at'))
    if (created > now or abs((created - facts.timestamp(candidate['searchCreatedAt'])).total_seconds()) >= 1
            or abs((official.snowflake_time(tid) - created).total_seconds()) >= 2):
        raise ValueError('timestamp_mismatch')
    if any(payload.get(key) for key in (
            'quoted_tweet', 'quoted_status', 'quoted_status_id', 'quoted_status_id_str',
            'is_quote_status', 'retweeted_status', 'retweeted_tweet', 'in_reply_to_status_id_str',
            'in_reply_to_status_id', 'in_reply_to_user_id_str', 'in_reply_to_user_id',
            'in_reply_to_screen_name')):
        raise ValueError('quoted_or_reply')
    text = payload.get('text')
    if not isinstance(text, str) or len(text.encode('utf-8')) > azure.MAX_INPUT_BYTES:
        raise ValueError('invalid_post_text')
    if re.search(r'(^|\n)\s*(RT\s+@|引用|転載|>)', text):
        raise ValueError('quoted_or_reply')
    edit_control = payload.get('edit_control', {})
    if not isinstance(edit_control, dict):
        raise ValueError('invalid_edit_metadata')
    edits = edit_control.get('edit_tweet_ids', [tid])
    if (not isinstance(edits, list) or not 1 <= len(edits) <= 8
            or any(not isinstance(item, str) or not official.post_id(item) for item in edits)
            or len(set(edits)) != len(edits) or edits != sorted(edits, key=int) or edits[-1] != tid):
        raise ValueError('obsolete_or_invalid_edit')
    photos, details = payload.get('photos', []), payload.get('mediaDetails', [])
    if (not isinstance(photos, list) or not isinstance(details, list)
            or len(photos) > 4 or len(details) > 4):
        raise ValueError('invalid_photo_metadata')
    if payload.get('video') or any(not isinstance(item, dict) or item.get('type') != 'photo'
                                   for item in details):
        raise ValueError('non_photo_media')
    if len(photos) != len(details):
        raise ValueError('incomplete_photo_metadata')
    media, urls, seen = [], [], set()
    for index, (photo, detail) in enumerate(zip(photos, details), 1):
        if not isinstance(photo, dict):
            raise ValueError('invalid_photo_metadata')
        url = photo.get('url')
        asset, mime, _ = media_url(url)
        other_asset, other_mime, _ = media_url(detail.get('media_url_https'))
        if asset != other_asset or mime != other_mime or asset in seen:
            raise ValueError('photo_metadata_mismatch')
        seen.add(asset)
        expanded = detail.get('expanded_url')
        if expanded is not None:
            parsed = urllib.parse.urlsplit(expanded)
            if (parsed.scheme != 'https' or parsed.netloc not in ('x.com', 'twitter.com')
                    or parsed.path != f'/{handle}/status/{tid}/photo/{index}'
                    or parsed.query or parsed.fragment):
                raise ValueError('photo_owner_mismatch')
        for field in ('source_status_id_str', 'source_status_id'):
            if detail.get(field) is not None and official.post_id(detail[field]) != tid:
                raise ValueError('photo_owner_mismatch')
        for field in ('source_user_id_str', 'source_user_id'):
            if detail.get(field) is not None and author_id(detail[field]) != uid:
                raise ValueError('photo_owner_mismatch')
        original = detail.get('original_info')
        if not isinstance(original, dict):
            raise ValueError('invalid_photo_dimensions')
        width, height = original.get('width'), original.get('height')
        if (type(width) is not int or type(height) is not int or min(width, height) < 1
                or max(width, height) > 65535):
            raise ValueError('invalid_photo_dimensions')
        # The photo's dimensions describe a variant, not necessarily original_info.
        if any(type(photo.get(key)) is not int or photo[key] <= 0 for key in ('width', 'height')):
            raise ValueError('invalid_photo_dimensions')
        fetch_url = selected_image_url(url)
        urls.append(fetch_url)
        media.append({'urlHash': facts.digest(fetch_url.encode()),
                      'originalWidth': width, 'originalHeight': height})
    source = {key: candidate[key] for key in ('id', 'url', 'name', 'authorId', 'authorScreenName')}
    source.update(createdAt=facts.stamp(created), observedAt=facts.stamp(now),
                  editTweetIds=edits, bodyHash=facts.digest(text.encode('utf-8')),
                  payloadHash=payload_hash or facts.digest(payload),
                  discoveryHash=candidate['discoveryHash'], media=media)
    facts.validate_source(source)
    return source, text, urls


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure('redirect_refused', status=code)


class SourceClient:
    def __init__(self, source, *, clock, opener=None, http_state=None):
        if source is None:
            raise ValueError('shared_source_state_required')
        self.source, self.clock = source, clock
        self.http_state = http_state
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.seen = set()
        self.images = {}
        self.requests = {'searches': 0, 'posts': 0, 'images': 0}

    def close(self):
        for image in self.images.values():
            if isinstance(image['bytes'], bytearray):
                image['bytes'][:] = b'\0' * len(image['bytes'])
        self.images.clear()

    def _deny(self, host, status, retry_after, *, reason='access_denied'):
        now = self.clock()
        until = now + dt.timedelta(hours=1)
        if retry_after:
            try:
                parsed = now + dt.timedelta(seconds=max(0, int(retry_after)))
            except (ValueError, TypeError, OverflowError):
                try:
                    parsed = email.utils.parsedate_to_datetime(retry_after).astimezone(facts.UTC)
                except (ValueError, TypeError, OverflowError, AttributeError):
                    parsed = until
            until = max(until, parsed)
        cooldowns = official.load_transport(self.http_state) if self.http_state else {}
        for known_host, deadline in self.source.state['cooldowns'].items():
            if known_host not in cooldowns or official.timestamp(deadline) > official.timestamp(cooldowns[known_host]):
                cooldowns[known_host] = deadline
        if host in cooldowns:
            until = max(until, official.timestamp(cooldowns[host]))
        self.source.set_cooldown(host, until, paused={
            'reason': reason, 'host': host, 'at': facts.stamp(now),
            'retryAt': facts.stamp(until), 'httpStatus': status})
        if self.http_state:
            cooldowns[host] = facts.stamp(until)
            official.atomic_json(self.http_state, {'schemaVersion': 1, 'cooldowns': cooldowns})
        raise Failure('shared_host_cooldown', status=status, retry_at=until)

    def _get(self, kind, url, maximum):
        if (kind == 'searches' and not personal.valid_search_url(url)
                or kind == 'posts' and not re.fullmatch(
                    r'https://cdn\.syndication\.twimg\.com/tweet-result\?id=[0-9]{15,22}&lang=ja&token=a', url)):
            raise ValueError('route_refused')
        if kind == 'images':
            media_url(url)
        if kind not in ('searches', 'posts', 'images') or url in self.seen:
            raise Failure('source_already_issued')
        if self.http_state:
            host = urllib.parse.urlsplit(url).hostname
            until = official.load_transport(self.http_state).get(host)
            if until is not None:
                self.source.set_cooldown(host, official.timestamp(until))
        self.source.check(kind)
        receipt = self.source.reserve(kind, url)
        self.seen.add(url)
        finished = False
        raw = None
        http_status = None
        try:
            self.source.issued(receipt)
            self.requests[kind] += 1
            with self.opener.open(urllib.request.Request(url), timeout=TIMEOUT) as response:
                status = response.getcode()
                http_status = status
                if status in (401, 403, 429):
                    self._deny(urllib.parse.urlsplit(url).hostname, status, response.headers.get('Retry-After'))
                if 300 <= status < 400:
                    self._deny(urllib.parse.urlsplit(url).hostname, status, None, reason='redirect_refused')
                if status != 200:
                    raise Failure('unexpected_http_status', status=status)
                # Immutable photo variants can legitimately have a long CDN Age.
                if kind != 'images':
                    official.check_http_metadata(response.headers, self.clock())
                length = response.headers.get('Content-Length')
                if length is not None and (not re.fullmatch(r'\d+', str(length)) or int(length) > maximum):
                    raise Failure('response_too_large')
                encoding = response.headers.get('Content-Encoding', 'identity').lower()
                if encoding != 'identity':
                    raise Failure('response_encoding_refused')
                raw = bytearray()
                while True:
                    part = response.read(min(65536, maximum + 1 - len(raw)))
                    if not part:
                        break
                    raw.extend(part)
                    if len(raw) > maximum:
                        raise Failure('response_too_large')
                if not raw:
                    raise Failure('empty_response')
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                self.source.finish(receipt, http_status=status)
                finished = True
                return raw, content_type
        except urllib.error.HTTPError as exc:
            status, retry = exc.code, exc.headers.get('Retry-After') if exc.headers else None
            http_status = status
            exc.close()
            if status in (401, 403, 429):
                self._deny(urllib.parse.urlsplit(url).hostname, status, retry)
            if 300 <= status < 400:
                self._deny(urllib.parse.urlsplit(url).hostname, status, None, reason='redirect_refused')
            raise Failure('http_error', status=status) from None
        except Failure as exc:
            if exc.status is not None:
                http_status = exc.status
            if exc.reason == 'redirect_refused':
                self._deny(urllib.parse.urlsplit(url).hostname, exc.status or 302, None,
                           reason='redirect_refused')
            raise
        except (OSError, http.client.HTTPException):
            raise Failure('network_error') from None
        finally:
            if not finished:
                if raw is not None:
                    raw[:] = b'\0' * len(raw)
                self.source.finish(receipt, 'failed', http_status=http_status)

    def search(self, handle):
        raw, _ = self._get('searches', personal.account_search_url(handle), MAX_DOCUMENT_BYTES)
        return raw.decode('utf-8', 'strict')

    def post(self, tid):
        facts.identifier(tid)
        raw, _ = self._get('posts', f'https://{POST_HOST}/tweet-result?id={tid}&lang=ja&token=a',
                           MAX_DOCUMENT_BYTES)
        return azure.transport.strict_json(raw), facts.digest(bytes(raw))

    def image(self, url):
        if url in self.images:
            return self.images[url]
        remaining = azure.MAX_POST_BYTES - sum(len(image['bytes']) for image in self.images.values())
        if remaining <= 0:
            raise ValueError('image_post_limit')
        raw, mime = self._get('images', url, min(azure.MAX_IMAGE_BYTES, remaining))
        expected = media_url(url)[1]
        if mime != expected:
            raw[:] = b'\0' * len(raw)
            raise ValueError('image_content_type')
        try:
            azure.probe_image(raw, mime)
        except BaseException:
            raw[:] = b'\0' * len(raw)
            raise
        image = {'bytes': raw, 'mime': mime}
        try:
            azure.image_facts([*self.images.values(), image])
        except BaseException:
            raw[:] = b'\0' * len(raw)
            raise
        self.images[url] = image
        return image


def refresh_coverage(state, reasons, now, manual):
    periods = facts.target_periods(now)
    merged = facts.effective_schedule(manual, state)
    for start, end in periods:
        people = state['coverage'].setdefault(start, {})
        date, empty_day = facts.day(start), False
        while date <= facts.day(end):
            rows = merged.get(date.isoformat(), {})
            empty_day |= not any(rows.get(shift) for shift in ('昼', '夜'))
            date += dt.timedelta(days=1)
        for name, reason in reasons.items():
            row = people.setdefault(name, {
                'name': name, 'handle': reason['handle'], 'to': end,
                'lastSearchedAt': None, 'nextCheckAt': None,
                'candidateIds': [], 'confirmedIds': [], 'reason': reason['reason']})
            row['handle'] = reason['handle']
            row['confirmedIds'] = sorted({item['id'] for item in state['schedules']
                                          if item['name'] == name and item['period']['from'] == start}, key=int)
            row['candidateIds'] = [item['id'] for item in state['pending'] if item['name'] == name]
            if reason['handle'] is None:
                row['reason'] = reason['reason']
            elif row['confirmedIds']:
                row['reason'] = 'valid_schedule'
            if row['lastSearchedAt'] is not None:
                interval = 6 if empty_day and not row['confirmedIds'] else 24
                row['nextCheckAt'] = facts.stamp(facts.timestamp(row['lastSearchedAt'])
                                                + dt.timedelta(hours=interval))
    return periods


def enqueue(state, candidates, now):
    known = {item['id'] for item in state['pending']}
    dropped = set()
    for candidate in sorted(candidates, key=lambda item: (item['priority'], -int(item['id']))):
        if candidate['id'] in known:
            continue
        if facts.candidate_key(candidate) in state['candidateHistory']:
            continue
        if any(record['source']['id'] == candidate['id'] and record['status'] != 'pending'
               for record in state['sources'].values()):
            continue
        count = sum(item['name'] == candidate['name'] for item in state['pending'])
        if len(state['pending']) >= 240 or count >= 6:
            dropped.add(candidate['name'])
            continue
        state['pending'].append(candidate)
        known.add(candidate['id'])
    for people in state['coverage'].values():
        for name in dropped:
            if name in people:
                people[name]['reason'] = 'queue_limit'
                people[name]['nextCheckAt'] = facts.stamp(now + dt.timedelta(hours=6))
    return dropped


def _set_reason(state, name, periods, reason):
    for start, _ in periods:
        if name in state['coverage'].get(start, {}):
            state['coverage'][start][name]['reason'] = reason


def target_population(state, schedule, insights, accounts, existing_bindings=None):
    bindings = dict(existing_bindings or {})
    conflicts = set()
    for name, bound in state['identityBindings'].items():
        if name in bindings and any(bound[field] != bindings[name][field]
                                    for field in ('authorId', 'authorScreenName')):
            conflicts.add(name)
        else:
            bindings[name] = bound
    targets, reasons = facts.population(schedule, insights, accounts, bindings)
    for name in conflicts:
        targets.pop(name, None)
        if name in reasons:
            reasons[name] = {'handle': None, 'reason': 'account_identity_mismatch'}
    return targets, reasons, bindings


def completion_report(state, status, counts, now):
    code = (3 if status in ('paused', 'unavailable') else
            2 if status in ('partial', 'budget-exhausted') else 0)
    return {'completed': True, 'component': 'schedule', 'collectionStatus': status,
            'status': status, 'exitCode': code, 'complete': False,
            'requests': {kind: counts[kind] for kind in ('searches', 'posts', 'images')},
            'analysisRequests': counts.get('analysis', 0),
            'scheduleCount': len(state['schedules']), 'pendingCount': len(state['pending']),
            'finishedAt': facts.stamp(now)}, code


def update_check_time(state, now, *, successful=False):
    checked = max([facts.timestamp(facts.stamp(now)), *(
        facts.timestamp(state[field]) for field in ('checkedAt', 'lastSuccessAt')
        if state[field] is not None)])
    state['checkedAt'] = facts.stamp(checked)
    if successful:
        state['lastSuccessAt'] = state['checkedAt']


def collect(state, schedule, insights, accounts, client, source, analyzer, *,
            clock, save, max_searches=1, max_posts=1, max_images=4, existing_bindings=None):
    """One person, at most one search/post/AI; authoritative ledgers are mandatory."""
    if source is None or analyzer is None:
        raise ValueError('shared_schedule_accounting_required')
    if (type(max_searches) is not int or max_searches not in (0, 1)
            or type(max_posts) is not int or max_posts not in (0, 1)
            or type(max_images) is not int or not 0 <= max_images <= 4):
        raise ValueError('invalid_schedule_limits')
    facts.validate_state(state)
    now = clock()
    update_check_time(state, now)
    targets, reasons, bindings = target_population(state, schedule, insights, accounts, existing_bindings)
    periods = refresh_coverage(state, reasons, now, schedule.get('schedule', {}))
    counts = {'searches': 0, 'posts': 0, 'images': 0, 'analysis': 0}
    outcome, target, verified = 'no-results', None, None
    selected = None
    payload_received = False
    try:
        analyzer.check()
        # Expired metadata is accounted for in coverage, never in valid revisions.
        retained = []
        for item in state['pending']:
            if facts.timestamp(item['searchCreatedAt']).astimezone(facts.JST).date() < facts.candidate_start(now):
                _set_reason(state, item['name'], periods, 'stale_candidate')
            elif item['name'] in targets and item['authorScreenName'] == targets[item['name']]['handle']:
                bound = bindings.get(item['name'])
                if bound and bound['authorId'] != item['authorId']:
                    state['candidateHistory'][facts.candidate_key(item)] = {
                        'candidate': dict(item), 'reason': 'account_identity_mismatch',
                        'checkedAt': facts.stamp(now)}
                    _set_reason(state, item['name'], periods, 'account_identity_mismatch')
                else:
                    retained.append(item)
        state['pending'] = retained
        waiting = sorted((item for item in state['pending'] if item['nextAttemptAt'] is None
                          or facts.timestamp(item['nextAttemptAt']) <= now),
                         key=lambda item: (item['lastAttemptAt'] or '', item['discoveredAt'],
                                           item['priority'], -int(item['id'])))
        if waiting:
            selected = waiting[0]
            target = targets[selected['name']]
        else:
            due = []
            for name, possible in targets.items():
                rows = [state['coverage'][start][name] for start, _ in periods]
                if any(row['nextCheckAt'] is None or facts.timestamp(row['nextCheckAt']) <= now for row in rows):
                    due.append((min(row['lastSearchedAt'] or '' for row in rows), name, possible))
            if due and max_searches:
                target = min(due)[2]
                source.check('searches')
                for start, _ in periods:
                    row = state['coverage'][start][target['name']]
                    row['lastSearchedAt'] = facts.stamp(clock())
                    row['nextCheckAt'] = facts.stamp(clock() + dt.timedelta(hours=24))
                save()
                counts['searches'] += 1
                document = client.search(target['handle'])
                candidates, truncated = discover(document, {target['name']: target},
                                                  clock(), bindings)
                del document
                _set_reason(state, target['name'], periods, 'candidate_limit' if truncated
                            else 'post_unverified' if candidates else 'no_candidates')
                enqueue(state, candidates, clock())
                waiting = sorted((item for item in state['pending'] if item['name'] == target['name']),
                                 key=lambda item: (item['lastAttemptAt'] or '', item['discoveredAt'],
                                                   item['priority'], -int(item['id'])))
                waiting = [item for item in waiting if item['nextAttemptAt'] is None
                           or facts.timestamp(item['nextAttemptAt']) <= clock()]
                selected = waiting[0] if waiting else None
                save()
            elif due:
                outcome = 'budget-exhausted'
            else:
                outcome = 'no-new'
        if selected is not None and max_posts:
            analyzer.check()
            source.check('posts')
            selected['lastAttemptAt'] = facts.stamp(clock())
            save()
            counts['posts'] += 1
            payload, payload_hash = client.post(selected['id'])
            payload_received = True
            verified, text, urls = validate_post(
                selected, payload, target, clock(), bindings.get(target['name']),
                payload_hash=payload_hash)
            del payload
            key = facts.source_key(verified)
            previous = state['sources'].get(key)
            same_input = any(record['status'] != 'pending'
                             and record['source']['authorId'] == verified['authorId']
                             and record['source']['bodyHash'] == verified['bodyHash']
                             and record['source']['media'] == verified['media']
                             for record in state['sources'].values())
            if previous and previous['status'] != 'pending' or same_input:
                state['pending'].remove(selected)
                _set_reason(state, target['name'], periods, 'known_source')
                outcome = 'no-new'
            else:
                facts.bind_identity(state, verified)
                facts.record_source(state, verified, 'pending', 'not_issued', clock())
                save()
                analyzer.check()
                if len(urls) > max_images:
                    raise Failure('source_budget_exhausted')
                if urls:
                    source.check('images', len(urls))
                images = []
                try:
                    for url in urls:
                        analyzer.check()
                        counts['images'] += 1
                        image = client.image(url)
                        images.append(image)
                        azure.image_facts(images)
                    image_hashes = [item['sha256'] for item in azure.image_facts(images)]
                    identical = any(record['status'] != 'pending'
                                    and record['source']['authorId'] == verified['authorId']
                                    and record['source']['bodyHash'] == verified['bodyHash']
                                    and record['imageHashes'] == image_hashes
                                    for record in state['sources'].values())

                    def on_issued(request_hash):
                        counts['analysis'] += 1
                        facts.record_source(state, verified, 'issued', 'not_issued', clock(),
                                            request_hash, image_hashes)
                        save()

                    if identical:
                        facts.record_source(state, verified, 'negative', 'known_source', clock(),
                                            image_hashes=image_hashes)
                        outcome = 'no-new'
                        _set_reason(state, target['name'], periods, 'known_source')
                    else:
                        schedules, analysis = analyzer.analyze(verified, text, images, periods, on_issued)
                        if schedules:
                            changed = facts.apply_revision(state, schedules, verified, analysis)
                            outcome = 'ok' if changed else 'no-new'
                            _set_reason(state, target['name'], periods, 'valid_schedule')
                        else:
                            facts.record_source(state, verified, 'negative', 'not_schedule',
                                                clock(), analysis['requestHash'], image_hashes)
                            outcome = 'no-results'
                            _set_reason(state, target['name'], periods, 'not_schedule')
                    state['pending'].remove(selected)
                finally:
                    images.clear()
                    text = None
                    client.close()
        elif selected is not None:
            outcome = 'budget-exhausted'
            _set_reason(state, selected['name'], periods, 'budget_wait')
    except Exception as exc:
        reason = getattr(exc, 'reason', str(exc))
        if 'budget' in reason:
            outcome, coverage_reason = 'budget-exhausted', 'budget_wait'
        elif any(word in reason for word in ('paused', 'cooldown', 'backoff', 'auth_stopped')):
            outcome, coverage_reason = 'paused', 'paused'
        else:
            outcome = 'partial' if counts['searches'] else 'unavailable'
            coverage_reason = 'analysis_failed' if counts['analysis'] else 'source_failed'
        if target is not None:
            _set_reason(state, target['name'], periods, coverage_reason)
        if selected is not None and selected in state['pending']:
            if isinstance(exc, ValueError) and payload_received and verified is None:
                state['candidateHistory'][facts.candidate_key(selected)] = {
                    'candidate': dict(selected), 'reason': 'source_failed', 'checkedAt': facts.stamp(clock())}
                state['pending'].remove(selected)
            else:
                wait = dt.timedelta(minutes=15) if coverage_reason == 'budget_wait' else dt.timedelta(hours=6)
                selected['nextAttemptAt'] = facts.stamp(clock() + wait)
        if verified is not None:
            key = facts.source_key(verified)
            record = state['sources'].get(key)
            if record and record['status'] == 'issued':
                facts.record_source(state, verified, 'failed', 'analysis_failed',
                                    clock(), record['requestHash'])
                if selected in state['pending']:
                    state['pending'].remove(selected)
            elif record and isinstance(exc, ValueError):
                facts.record_source(state, verified, 'failed', 'source_failed', clock())
                if selected in state['pending']:
                    state['pending'].remove(selected)
    finally:
        client.close()
    state['lastRun'] = {'status': outcome}
    refresh_coverage(state, reasons, clock(), schedule.get('schedule', {}))
    update_check_time(state, clock(), successful=outcome in ('ok', 'no-new', 'no-results'))
    facts.validate_state(state)
    save()
    if isinstance(getattr(client, 'requests', None), dict):
        counts.update(client.requests)
    return completion_report(state, outcome, counts, clock())


def replay_saved(state, *, document, payload_bytes, images, result, schedule, insights,
                 accounts, post_id, now, receipt_id, allowed_periods=None, existing_bindings=None):
    """Offline bootstrap from verified roster/account + actual discovery and raw hashes.

    This produces validation material, not authorization: the caller must bind
    these hashes to a separately approved stable-model canonical usage receipt.
    """
    facts.validate_state(state)
    targets, _, bindings = target_population(state, schedule, insights, accounts, existing_bindings)
    candidates, _ = discover(document, targets, now, bindings)
    selected = next((item for item in candidates if item['id'] == post_id), None)
    if selected is None:
        raise ValueError('saved_discovery_required')
    if not isinstance(payload_bytes, bytes) or len(payload_bytes) > MAX_DOCUMENT_BYTES:
        raise ValueError('invalid_saved_payload')
    payload = azure.transport.strict_json(payload_bytes)
    source, text, urls = validate_post(selected, payload, targets[selected['name']], now,
                                      bindings.get(selected['name']),
                                      payload_hash=facts.digest(payload_bytes))
    if len(images) != len(urls):
        raise ValueError('schedule_images_incomplete')
    parsed, proof = azure.saved_result(source, text, images, result, now=now, receipt_id=receipt_id,
                                      allowed_periods=allowed_periods or facts.target_periods(now))
    target = targets[selected['name']]
    account = next(row for row in accounts if row['handle'] == target['handle'])
    subject = {'name': target['name'], 'handle': target['handle'], 'accountSource': account['source'],
               'accountHash': facts.digest(account), 'rosterHash': facts.digest(schedule['roster'])}
    return {'subject': subject, 'subjectHash': facts.digest(subject), 'source': source,
            'analysis': proof, 'schedules': parsed}


def validate_saved_packet(value, existing_bindings=None):
    """Validate a data-only packet, NOT its external approval/canonical AI receipt."""
    facts.require_keys(value, ('subject', 'subjectHash', 'source', 'analysis', 'schedules'))
    subject = value['subject']
    facts.require_keys(subject, ('name', 'handle', 'accountSource', 'accountHash', 'rosterHash'))
    facts.identity(subject['name'], subject['handle'])
    if subject['accountSource'] not in ('公式サイト', '本人確認済み'):
        raise ValueError('saved_account_source_required')
    facts.valid_hash(subject['accountHash'])
    facts.valid_hash(subject['rosterHash'])
    if value['subjectHash'] != facts.digest(subject):
        raise ValueError('saved_subject_hash_mismatch')
    facts.validate_source(value['source'])
    if (subject['name'] != value['source']['name']
            or subject['handle'] != value['source']['authorScreenName']):
        raise ValueError('saved_subject_mismatch')
    bound = (existing_bindings or {}).get(subject['name'])
    if bound and any(bound[field] != value['source'][field] for field in ('authorId', 'authorScreenName')):
        raise ValueError('saved_binding_mismatch')
    facts.apply_revision(facts.empty_state(), value['schedules'], value['source'], value['analysis'])
    return value


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--source-state', type=Path, required=True)
    parser.add_argument('--source-run-id')
    parser.add_argument('--personal-state', '--personal-snapshot', dest='personal_state', type=Path, required=True)
    parser.add_argument('--http-state', type=Path, required=True)
    parser.add_argument('--ai-state', type=Path, required=True)
    parser.add_argument('--analysis-run-id', required=True)
    parser.add_argument('--analysis-limit', type=int, choices=(0, 1), default=1)
    parser.add_argument('--max-searches', type=int, choices=(0, 1), default=1)
    parser.add_argument('--max-posts', type=int, choices=(0, 1), default=1)
    parser.add_argument('--max-images', type=int, choices=range(5), default=4)
    parser.add_argument('--schedule', type=Path, default=ROOT / 'data' / 'schedule.js')
    parser.add_argument('--insights', type=Path, default=ROOT / 'data' / 'store-insights.js')
    parser.add_argument('--accounts', type=Path, default=ROOT / 'tools' / 'data' / 'accounts.csv')
    parser.add_argument('--node', type=Path)
    parser.add_argument('--publish', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    return parser


def run(args, *, clock=official.utc_now, sleep=time.sleep, environment=None):
    if args.source_run_id is not None and args.source_run_id != args.analysis_run_id:
        raise ValueError('schedule_run_id_mismatch')
    writes = [path.resolve() for path in (args.snapshot, args.source_state, args.http_state,
                                         args.ai_state, args.publish, args.report) if path]
    inputs = {path.resolve() for path in (args.schedule, args.insights, args.accounts, args.personal_state) if path}
    if (len(set(writes)) != len(writes) or set(writes) & inputs
            or any(path.suffix != '.json' for path in writes)
            or args.snapshot.resolve().parent == (ROOT / 'data').resolve()):
        raise ValueError('unsafe_storage_paths')
    source_module = _module('source-state.py', 'half_month_source_usage')
    usage_module = _module('analysis-state.py', 'half_month_ai_usage')

    def defer(state, status):
        state['lastRun'] = {'status': status}
        update_check_time(state, clock())
        facts.validate_state(state)
        official.atomic_json(args.snapshot, state)
        report, code = completion_report(state, status, {
            'searches': 0, 'posts': 0, 'images': 0, 'analysis': 0}, clock())
        report['exitCode'] = code
        if args.publish and not args.dry_run:
            official.atomic_json(args.publish, facts.public_state(state))
        if args.report:
            official.atomic_json(args.report, report)
        return report, code

    if args.analysis_limit == 0:
        with official.ProcessLock(args.snapshot.with_suffix('.lock')):
            state = facts.read_state(args.snapshot)
            source_module.validate_legacy(
                source_module.load_state(args.source_state, required=True),
                personal.read_state(args.personal_state),
                usage_module.load_state(args.ai_state, required=True))
            return defer(state, 'budget-exhausted')
    with ExitStack() as stack:
        stack.enter_context(official.ProcessLock(args.snapshot.with_suffix('.lock')))
        state = facts.read_state(args.snapshot)
        source = stack.enter_context(source_module.SharedSource(
            args.source_state, run_id=args.analysis_run_id, component='schedule',
            clock=clock, sleep=sleep, personal_path=args.personal_state))
        usage = stack.enter_context(usage_module.SharedUsage(
            args.ai_state, run_id=args.analysis_run_id, component='schedule', clock=clock,
            sleep=sleep, request_limit=1))
        try:
            usage.check()
        except usage_module.UsageFailure as exc:
            if exc.reason == 'azure_budget_exhausted':
                return defer(state, 'budget-exhausted')
            if exc.reason in ('azure_auth_stopped', 'azure_backoff'):
                return defer(state, 'paused')
            raise
        personal_state = personal.read_state(args.personal_state)
        schedule = personal.read_js(args.schedule, 'SCHEDULE_DATA', args.node)
        insights = personal.read_js(args.insights, 'STORE_INSIGHTS', args.node)
        with args.accounts.open(encoding='utf-8-sig', newline='') as stream:
            accounts = list(csv.DictReader(stream))
        analyzer = azure.AzureAnalyzer(usage, os.environ if environment is None else environment, clock=clock)
        client = SourceClient(source, clock=clock, http_state=args.http_state)

        def save():
            facts.validate_state(state)
            official.atomic_json(args.snapshot, state)

        report, code = collect(state, schedule, insights, accounts, client, source, analyzer,
                               clock=clock, save=save, max_searches=args.max_searches,
                               max_posts=args.max_posts, max_images=args.max_images,
                               existing_bindings=personal_state['identityBindings'])
        report['exitCode'] = code
        if args.publish and not args.dry_run:
            official.atomic_json(args.publish, facts.public_state(state))
        if args.report:
            official.atomic_json(args.report, report)
        return report, code


def main(argv=None):
    try:
        report, code = run(argument_parser().parse_args(argv))
        print(json.dumps(report, ensure_ascii=False))
        return code
    except Exception:
        # Exception messages can contain transport bodies or local storage paths.
        print(json.dumps({'completed': False, 'component': 'schedule', 'status': 'unavailable',
                          'collectionStatus': 'unavailable', 'exitCode': 1,
                          'reason': 'schedule_infrastructure_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
