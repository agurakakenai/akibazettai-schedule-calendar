"""Bounded half-month producer; raw v3 evidence is encrypted outside the checkout.

Legacy invocation remains available. --cycle enables the finite active-person
cohort; --resume continues only its saved names/periods, not a new search chain.
--evidence-cache must name external _private-evidence/cache.bin and use the
SCHEDULE_EVIDENCE_KEY environment variable. --max-runtime-seconds defaults to
1110. Legacy max-searches/max-posts and one-request ceilings do not cap a cycle;
shared host spacing, monthly AI budget, registry guards and the deadline do.
The JSON report adds continuation (chainId/names/periods/cursor/ready/reason/
nextAt), nextStage, nextEligible, reasons and unresolvedCount. Only ready=true
authorizes an immediate finite continuation; daily roots may discover missing
sources, while cached pending work resumes without source GETs.
diagnostics[] links failures to name/postId/postUrl, optional imageIndex, failedAt,
host/httpStatus/retryAt and stage/nextStage/reason. Reading failures persist in
private readings.failure until processing succeeds; raw/image URLs are excluded.
"""
import argparse
import copy
from contextlib import ExitStack
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
members = facts.member_registry()
source_safety = official.source_module()
Failure = official.FetchFailure


class RegistryFailure(Failure):
    pass


def check_registry(guard, name=None):
    if guard is not None:
        try:
            return guard.check(name)
        except ValueError as exc:
            raise RegistryFailure(str(exc)) from None


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


def matching_binding(bindings, name, uid, handle, *, registry=None):
    for owner, bound in (bindings or {}).items():
        if registry is not None:
            member = members.lookup(registry, owner)
            owner = member['canonicalName'] if member else owner
        same_id = bound['authorId'] == uid
        same_handle = bound['authorScreenName'].casefold() == handle.casefold()
        if (owner == name and not (same_id and same_handle)
                or owner != name and (same_id or same_handle)):
            return False
    return True


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


def discovery_text(entry):
    """Yahoo's displayTextBody may be a string or a structured text fragment list."""
    def flatten(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return ''.join(flatten(item) for item in value)
        if isinstance(value, dict):
            return ''.join(flatten(value[key]) for key in ('text', 'body', 'displayTextBody')
                           if key in value)
        return ''
    return flatten(entry.get('displayTextBody', entry.get('text', entry.get('body', ''))))


def discover(document, targets, now, bindings=None, *, registry=None, other_bindings=(),
             candidate_since=None):
    """Bound publication dates separately from the real discovery/receipt clock."""
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
    handles = {target['handle'].casefold(): target for target in targets.values()}
    candidates, conflicts = {}, set()
    start = facts.candidate_start(now) if candidate_since is None else candidate_since
    evidence_hash = facts.digest(document.encode('utf-8'))
    for entry in entries[:200]:
        handle = entry.get('screenName')
        if not isinstance(handle, str) or handle.casefold() not in handles:
            continue
        tid, uid = official.post_id(entry.get('id')), author_id(entry.get('userId'))
        epoch = entry.get('createdAt')
        if (not tid or not uid or uid == official.AUTHOR_ID or type(epoch) not in (int, str)
                or not re.fullmatch(r'\d{10}', str(epoch))):
            continue
        if any(entry.get(key) for key in (
                'retweetedStatus', 'retweeted_status', 'isRetweet')):
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
            target = handles[handle.casefold()]
            if not all(matching_binding(mapping, target['name'], uid, handle, registry=registry)
                       for mapping in (bindings, *other_bindings)):
                continue
            candidate = {'id': tid, 'url': facts.public_url(handle, tid), 'name': target['name'],
                         'authorId': uid, 'authorScreenName': handle,
                         'searchCreatedAt': facts.stamp(when), 'discoveredAt': facts.stamp(now),
                         'discoveryHash': evidence_hash,
                         'lastAttemptAt': None, 'nextAttemptAt': None,
                         'priority': 0 if re.search(r'予定|お給仕|前半|後半|シフト',
                                                   discovery_text(entry)) else 1}
            if entry.get('isReply') is True:
                candidate['replyCandidate'] = True
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


def validate_post(candidate, payload, target, now, binding=None, *, payload_hash=None, parent_payload=None):
    """Verify only the original author's top-level media, without daily-date gates."""
    facts.validate_candidate(candidate)
    tid, uid, handle = candidate['id'], candidate['authorId'], candidate['authorScreenName']
    if not isinstance(payload, dict) or not official.matching_id(payload, tid):
        raise ValueError('response_id_mismatch')
    author = payload.get('user')
    if (not isinstance(author, dict) or not matching_author(author, uid)
            or author.get('screen_name') != handle or uid == official.AUTHOR_ID
            or candidate['name'] != target['name'] or handle.casefold() != target['handle'].casefold()
            or binding and (binding['authorId'] != uid
                            or binding['authorScreenName'].casefold() != handle.casefold())):
        raise ValueError('author_mismatch')
    created = official.timestamp(payload.get('created_at'))
    if (created > now or abs((created - facts.timestamp(candidate['searchCreatedAt'])).total_seconds()) >= 1
            or abs((official.snowflake_time(tid) - created).total_seconds()) >= 2):
        raise ValueError('timestamp_mismatch')
    if payload.get('retweeted_status') or payload.get('retweeted_tweet'):
        raise ValueError('quoted_or_reply')
    reply = {}
    parent_ids = [payload[key] for key in ('in_reply_to_status_id_str', 'in_reply_to_status_id')
                  if payload.get(key) is not None]
    parent_authors = [payload[key] for key in ('in_reply_to_user_id_str', 'in_reply_to_user_id')
                      if payload.get(key) is not None]
    if parent_ids or parent_authors:
        if (not parent_ids or not parent_authors
                or any(official.post_id(value) != official.post_id(parent_ids[0]) for value in parent_ids)
                or not official.post_id(parent_ids[0])
                or any(author_id(value) != uid for value in parent_authors)):
            raise ValueError('reply_author_mismatch')
        parent_id = official.post_id(parent_ids[0])
        parent = parent_payload or payload.get('parent')
        if (not isinstance(parent, dict) or not official.matching_id(parent, parent_id)
                or not isinstance(parent.get('user'), dict)
                or not matching_author(parent['user'], uid)
                or parent['user'].get('screen_name') != handle or int(parent_id) >= int(tid)):
            raise ValueError('reply_parent_unverified')
        reply = {'replyToId': parent_id, 'replyToAuthorId': uid}
    text = payload.get('text')
    if not isinstance(text, str) or len(text.encode('utf-8')) > azure.MAX_INPUT_BYTES:
        raise ValueError('invalid_post_text')
    if re.search(r'^\s*RT\s+@', text):
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
    source.update(reply)
    facts.validate_source(source)
    return source, text, urls


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure('redirect_refused', status=code)


class SourceClient:
    def __init__(self, source, *, clock, opener=None, http_state=None, registry_guard=None,
                 chunk_images=False):
        if source is None:
            raise ValueError('shared_source_state_required')
        self.source, self.clock = source, clock
        self.http_state = http_state
        self.registry_guard, self.registry_name = registry_guard, None
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.seen = set()
        self.images = {}
        self.requests = {'searches': 0, 'posts': 0, 'images': 0}
        self.chunk_images = chunk_images

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
        def guard():
            if self.registry_guard is not None:
                if self.registry_name is None:
                    raise RegistryFailure('unknown_member')
                check_registry(self.registry_guard, self.registry_name)
        guard()
        if source_safety.already_requested(self.source.state, self.source.run_id, kind, url):
            cached = self.source.cached(kind, url)
            if cached is not None:
                return bytearray(cached['body']), cached['contentType']
            raise Failure('source_already_issued')
        if kind not in ('searches', 'posts', 'images') or url in self.seen:
            raise Failure('source_already_issued')
        if self.http_state:
            host = urllib.parse.urlsplit(url).hostname
            until = official.load_transport(self.http_state).get(host)
            if until is not None:
                self.source.set_cooldown(host, official.timestamp(until))
        self.source.check(kind)
        guard()
        receipt = self.source.reserve(kind, url)
        self.seen.add(url)
        finished = False
        raw = None
        http_status = None
        try:
            guard()
            self.source.issued(receipt)
            guard()
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
                if kind != 'images' and content_type in source_safety.TransientSourceCache.CONTENT_TYPES:
                    self.source.remember(receipt, raw, content_type=content_type)
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
        remaining = (azure.MAX_IMAGE_BYTES if self.chunk_images else
                     azure.MAX_POST_BYTES - sum(len(image['bytes']) for image in self.images.values()))
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
            azure.image_facts([image] if self.chunk_images else [*self.images.values(), image])
        except BaseException:
            raw[:] = b'\0' * len(raw)
            raise
        self.images[url] = image
        return image


def refresh_coverage(state, reasons, now, manual, *, registry=None, terminal_confirmed=False,
                     periods=None):
    periods = facts.target_periods(now) if periods is None else periods
    merged = facts.effective_schedule(manual, state, registry=registry)
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
            previously_unavailable = row['handle'] is None
            row['handle'] = reason['handle']
            row['confirmedIds'] = sorted({item['id'] for item in state['schedules']
                                          if item['name'] == name and item['period']['from'] == start}, key=int)
            row['candidateIds'] = [item['id'] for item in state['pending'] if item['name'] == name]
            if reason['handle'] is None:
                row['reason'] = reason['reason']
            elif previously_unavailable:
                row['reason'] = 'post_unverified' if row['candidateIds'] else 'not_searched'
            if reason['handle'] is not None and row['confirmedIds'] and row['reason'] not in {
                    facts.TIMING_STORAGE_LIMIT_REASON, *facts.CAPACITY_HOLD_REASONS}:
                row['reason'] = 'valid_schedule'
            if terminal_confirmed and row['confirmedIds']:
                row['nextCheckAt'] = None
            elif row['lastSearchedAt'] is not None:
                interval = 6 if empty_day and not row['confirmedIds'] else 24
                row['nextCheckAt'] = facts.stamp(facts.timestamp(row['lastSearchedAt'])
                                                + dt.timedelta(hours=interval))
    return periods


TRANSIENT_REASONS = {'azure_timeout', 'azure_network_error', 'azure_interrupted', 'azure_rate_limited'}


def retry_reason(record, analyzer):
    retry = record.get('retry', {})
    if retry.get('attempts', 0) >= 3:
        return None
    reason = retry.get('lastReason')
    usage = getattr(getattr(analyzer, 'usage', None), 'state', None)
    receipt = next((item for item in (usage or {}).get('receipts', {}).values()
                    if item['requestHash'] == record.get('requestHash')), None) if isinstance(usage, dict) else None
    reason = reason or (receipt or {}).get('reason')
    if reason == 'azure_http_error' and ((receipt or {}).get('httpStatus') or 0) >= 500:
        return reason
    return reason if reason in TRANSIENT_REASONS else None


def terminal_source(record, analyzer):
    return record['status'] in ('valid', 'negative', 'issued') or (
        record['status'] == 'failed' and retry_reason(record, analyzer) is None)


def enqueue(state, candidates, now, analyzer=None):
    known = {item['id'] for item in state['pending']}
    dropped = set()
    for candidate in sorted(candidates, key=lambda item: (item['priority'], -int(item['id']))):
        if candidate['id'] in known:
            continue
        if facts.candidate_key(candidate) in state['candidateHistory']:
            continue
        if any(record['source']['id'] == candidate['id'] and terminal_source(record, analyzer)
               for record in state['sources'].values()):
            continue
        people = [item for item in state['pending'] if item['name'] == candidate['name']]
        count = len(people)
        if count >= 6:
            rank = lambda item: (item['priority'], -int(item['id']))
            worst = max(people, key=rank)
            if rank(candidate) < rank(worst):
                state['candidateHistory'][facts.candidate_key(worst)] = {
                    'candidate': dict(worst), 'reason': 'queue_limit', 'checkedAt': facts.stamp(now)}
                state['pending'].remove(worst)
                count -= 1
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


def restore_transient_candidates(state, analyzer, now):
    known = {item['id'] for item in state['pending']}
    for record in state['sources'].values():
        if record['status'] != 'failed' or retry_reason(record, analyzer) is None:
            continue
        source = record['source']
        if source['id'] in known or len(state['pending']) >= 240:
            continue
        if sum(item['name'] == source['name'] for item in state['pending']) >= 6:
            continue
        due = record.get('retry', {}).get('notBefore') or facts.stamp(
            facts.timestamp(record['checkedAt']) + dt.timedelta(hours=1))
        state['pending'].append({
            'id': source['id'], 'url': source['url'], 'name': source['name'],
            'authorId': source['authorId'], 'authorScreenName': source['authorScreenName'],
            'searchCreatedAt': source['createdAt'], 'discoveredAt': source['observedAt'],
            'discoveryHash': source['discoveryHash'], 'priority': 0,
            'lastAttemptAt': record['checkedAt'], 'nextAttemptAt': due,
            'attempts': record.get('retry', {}).get('attempts', 1)})
        known.add(source['id'])


def _set_reason(state, name, periods, reason):
    for start, _ in periods:
        if name in state['coverage'].get(start, {}):
            state['coverage'][start][name]['reason'] = reason


def waiting_candidates(state, targets, now, source=None):
    last_attempt = {}
    for item in state['pending']:
        name = item['name']
        last_attempt[name] = max(last_attempt.get(name, ''), item['lastAttemptAt'] or '')
    for record in state['sources'].values():
        name = record['source']['name']
        last_attempt[name] = max(last_attempt.get(name, ''), record['checkedAt'])
    for record in state['candidateHistory'].values():
        name = record['candidate']['name']
        last_attempt[name] = max(last_attempt.get(name, ''), record['checkedAt'])
    period = facts.discovery_periods(now)[0]
    opened = facts.publication_start(period)
    confirmed = {item['name'] for item in state['schedules'] if item['period']['from'] == period[0]}
    return sorted((item for item in state['pending'] if item['name'] in targets
                   and not (source is not None and source_safety.paused_for(source.state, 'images', now)
                            and any(record['source']['id'] == item['id'] and record['source']['media']
                                    for record in state['sources'].values()))
                   and not (source is not None and source_safety.already_requested(
                       source.state, source.run_id, 'posts',
                       f'https://{POST_HOST}/tweet-result?id={item["id"]}&lang=ja&token=a')
                       and source.cached('posts',
                           f'https://{POST_HOST}/tweet-result?id={item["id"]}&lang=ja&token=a') is None)
                   and (item['nextAttemptAt'] is None or facts.timestamp(item['nextAttemptAt']) <= now)),
                  key=lambda item: (item['name'] in confirmed,
                                    facts.timestamp(item['searchCreatedAt']).astimezone(facts.JST).date() < opened,
                                    item['priority'], last_attempt[item['name']], item['lastAttemptAt'] or '',
                                    item['discoveredAt'], -int(item['id'])))


def target_population(state, schedule, insights, accounts, existing_bindings=None, *, registry=None):
    bindings = dict(existing_bindings or {})
    if registry is not None:
        targets, reasons = facts.population(
            schedule, insights, accounts, existing_bindings, registry=registry,
            other_bindings=(state['identityBindings'],))
        bindings = {}
        for mapping in (existing_bindings or {}, state['identityBindings']):
            for name, bound in mapping.items():
                member = members.lookup(registry, name)
                bindings[member['canonicalName'] if member else name] = bound
        return targets, reasons, bindings
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


def completion_report(state, status, counts, now, *, registry=None, existing_bindings=None,
                      registry_error=None):
    code = (3 if status in ('paused', 'unavailable') else
            2 if status in ('partial', 'budget-exhausted') else 0)
    report = {'completed': True, 'component': 'schedule', 'collectionStatus': status,
            'status': status, 'exitCode': code, 'complete': False,
            'requests': {kind: counts[kind] for kind in ('searches', 'posts', 'images')},
            'analysisRequests': counts.get('analysis', 0),
            'scheduleCount': len(state['schedules']), 'pendingCount': len(state['pending']),
            'finishedAt': facts.stamp(now)}
    if registry is not None:
        report['registry'] = members.registry_report(
            registry, existing_bindings or {}, state['identityBindings'])
        report['registry']['unresolvedNames'] = [
            row['name'] for row in registry['unresolvedNames'] if row['resolvedMemberId'] is None]
        if registry_error is not None:
            report['registry']['stopReason'] = registry_error
    return report, code


def update_check_time(state, now, *, successful=False):
    checked = max([facts.timestamp(facts.stamp(now)), *(
        facts.timestamp(state[field]) for field in ('checkedAt', 'lastSuccessAt')
        if state[field] is not None)])
    state['checkedAt'] = facts.stamp(checked)
    if successful:
        state['lastSuccessAt'] = state['checkedAt']


def collect(state, schedule, insights, accounts, client, source, analyzer, *,
            clock, save, max_searches=1, max_posts=1, max_images=4, existing_bindings=None,
            registry=None, registry_guard=None):
    """At most one search/post/AI, with first-pass search and pending-post fairness."""
    if source is None or analyzer is None:
        raise ValueError('shared_schedule_accounting_required')
    if (type(max_searches) is not int or max_searches not in (0, 1)
            or type(max_posts) is not int or max_posts not in (0, 1)
            or type(max_images) is not int or not 0 <= max_images <= 4):
        raise ValueError('invalid_schedule_limits')
    facts.validate_state(state)
    if registry_guard is not None:
        if registry is None:
            registry = registry_guard.registry
        client.registry_guard = analyzer.registry_guard = registry_guard
    now = clock()
    prior_checked = state['checkedAt']
    update_check_time(state, now)
    targets, reasons, bindings = target_population(
        state, schedule, insights, accounts, existing_bindings, registry=registry)
    binding_maps = (existing_bindings or {}, state['identityBindings'])
    manual = (schedule or {}).get('schedule') or {}
    periods = refresh_coverage(state, reasons, now, manual, registry=registry)
    counts = {'searches': 0, 'posts': 0, 'images': 0, 'analysis': 0}
    outcome, target, verified = 'no-results', None, None
    selected = None
    payload_received = False
    image_hashes = None
    registry_error = None
    search_target, search_before, search_succeeded = None, None, False

    def guard(name=None):
        check_registry(registry_guard, name)
        client.registry_name = name

    try:
        guard()
        analyzer.check()
        restore_transient_candidates(state, analyzer, now)
        # Expired metadata is accounted for in coverage, never in valid revisions.
        retained = []
        for item in state['pending']:
            if item['name'] not in targets:
                retained.append(item)
            elif facts.timestamp(item['searchCreatedAt']).astimezone(facts.JST).date() < facts.candidate_start(now):
                _set_reason(state, item['name'], periods, 'stale_candidate')
            else:
                if (item['authorScreenName'].casefold() != targets[item['name']]['handle'].casefold()
                        or not all(matching_binding(
                            mapping, item['name'], item['authorId'], item['authorScreenName'], registry=registry)
                            for mapping in (bindings, *binding_maps))):
                    state['candidateHistory'][facts.candidate_key(item)] = {
                        'candidate': dict(item), 'reason': 'account_identity_mismatch',
                        'checkedAt': facts.stamp(now)}
                    _set_reason(state, item['name'], periods, 'account_identity_mismatch')
                else:
                    retained.append(item)
        state['pending'] = retained
        image_held = {record['source']['name'] for record in state['sources'].values()
                      if record['source']['media']
                      and any(item['id'] == record['source']['id'] for item in state['pending'])}
        image_held = image_held if source_safety.paused_for(source.state, 'images', clock()) else set()
        for name in image_held:
            _set_reason(state, name, periods, 'image_host_paused')
        waiting = waiting_candidates(state, targets, now, source)
        due = []
        search_attempts = {}
        for receipt in source.state.get('receipts', {}).values():
            if receipt['kind'] == 'searches':
                key = receipt['requestHash']
                search_attempts[key] = max(search_attempts.get(key, ''), receipt.get('reservedAt', ''))
        for name, possible in targets.items():
            url = personal.account_search_url(possible['handle'])
            request_hash, _ = source_safety.request_identity('searches', url)
            if (source_safety.already_requested(source.state, source.run_id, 'searches', url)
                    and source.cached('searches', url) is None):
                continue
            priorities = []
            for index, period in enumerate(facts.discovery_periods(now)):
                row = state['coverage'][period[0]][name]
                searched = row['lastSearchedAt']
                first = not facts.searched_in_period(row, period)
                if first or row['nextCheckAt'] is None or facts.timestamp(row['nextCheckAt']) <= now:
                    priorities.append((bool(row['confirmedIds']), not first, index,
                                       max(searched or '', search_attempts.get(request_hash, ''))))
            if priorities:
                due.append((min(priorities), name, possible))
        search_due = due
        if search_due and max_searches and not source_safety.paused_for(source.state, 'searches', clock()):
            target = min(search_due)[2]
            search_target = target
            guard(target['name'])
            if not source_safety.already_requested(
                    source.state, source.run_id, 'searches', personal.account_search_url(target['handle'])):
                source.check('searches')
            guard(target['name'])
            search_before = {
                start: (state['coverage'][start][target['name']]['lastSearchedAt'],
                        state['coverage'][start][target['name']]['nextCheckAt'])
                for start, _ in periods}
            counts['searches'] += 1
            document = client.search(target['handle'])
            candidates, truncated = discover(
                document, {target['name']: target}, clock(), bindings,
                registry=registry, other_bindings=binding_maps)
            del document
            search_succeeded = True
            for start, _ in periods:
                row = state['coverage'][start][target['name']]
                row['lastSearchedAt'] = facts.stamp(clock())
                row['nextCheckAt'] = facts.stamp(clock() + dt.timedelta(hours=24))
            _set_reason(state, target['name'], periods, 'candidate_limit' if truncated
                        else 'post_unverified' if candidates else 'no_candidates')
            dropped = enqueue(state, candidates, clock(), analyzer)
            guard(target['name'])
            waiting = waiting_candidates(state, targets, clock(), source)
            selected = waiting[0] if waiting else None
            save()
        elif waiting:
            selected = waiting[0]
        elif due:
            outcome = 'budget-exhausted'
        else:
            outcome = 'no-new'
        if selected is None and image_held:
            outcome = 'partial' if counts['searches'] else 'paused'
        if selected is not None and source_safety.paused_for(source.state, 'posts', clock()):
            _set_reason(state, selected['name'], periods, 'paused')
            outcome = 'partial' if counts['searches'] else 'paused'
            selected = None
        if selected is not None and max_posts:
            target = targets[selected['name']]
            guard(target['name'])
            analyzer.check()
            if not source_safety.already_requested(
                    source.state, source.run_id, 'posts',
                    f'https://{POST_HOST}/tweet-result?id={selected["id"]}&lang=ja&token=a'):
                source.check('posts')
            guard(target['name'])
            if selected.get('attempts', 0) >= 3:
                state['candidateHistory'][facts.candidate_key(selected)] = {
                    'candidate': dict(selected), 'reason': 'retry_exhausted', 'checkedAt': facts.stamp(clock())}
                state['pending'].remove(selected)
                _set_reason(state, target['name'], periods, 'retry_exhausted')
                selected = None
                raise Failure('retry_exhausted')
            selected['lastAttemptAt'] = facts.stamp(clock())
            selected['attempts'] = selected.get('attempts', 0) + 1
            save()
            counts['posts'] += 1
            payload, payload_hash = client.post(selected['id'])
            guard(target['name'])
            payload_received = True
            verified, text, urls = validate_post(
                selected, payload, target, clock(), bindings.get(target['name']),
                payload_hash=payload_hash)
            del payload
            key = facts.source_key(verified)
            previous = state['sources'].get(key)
            same_input = any(terminal_source(record, analyzer)
                             and record['source']['authorId'] == verified['authorId']
                             and record['source']['bodyHash'] == verified['bodyHash']
                             and record['source']['media'] == verified['media']
                             for record in state['sources'].values())
            if previous and terminal_source(previous, analyzer) or same_input:
                state['pending'].remove(selected)
                _set_reason(state, target['name'], periods, 'known_source')
                outcome = 'no-new'
            else:
                analyzer.request_attempt = max(selected['attempts'] - 1,
                                               (previous or {}).get('retry', {}).get('attempts', 0))
                facts.bind_identity(state, verified)
                facts.record_source(state, verified, 'pending', 'not_issued', clock())
                save()
                if urls:
                    source.check('images', len(urls))
                azure.check_caption_capacity(verified, text)
                analyzer.check()
                if len(urls) > max_images:
                    raise Failure('source_budget_exhausted')
                images = []
                try:
                    for url in urls:
                        guard(target['name'])
                        analyzer.check()
                        counts['images'] += 1
                        image = client.image(url)
                        images.append(image)
                        azure.image_facts(images)
                    image_hashes = [item['sha256'] for item in azure.image_facts(images)]
                    identical = any(terminal_source(record, analyzer)
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
                        guard(target['name'])
                        schedules, analysis = analyzer.analyze(verified, text, images, periods, on_issued)
                        if schedules:
                            try:
                                changed = facts.apply_revision(state, schedules, verified, analysis)
                            except facts.timing().WorkTimingLimitError:
                                outcome = 'partial'
                                facts.record_source(state, verified, 'failed', facts.TIMING_STORAGE_LIMIT_REASON,
                                                    clock(), analysis['requestHash'], image_hashes)
                                _set_reason(state, target['name'], periods, facts.TIMING_STORAGE_LIMIT_REASON)
                            else:
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
    except azure.capacity.CapacityHold as exc:
        outcome = 'partial'
        if target is not None:
            _set_reason(state, target['name'], periods, exc.reason)
        if verified is not None:
            record = state['sources'].get(facts.source_key(verified), {})
            facts.record_source(state, verified, 'failed', exc.reason, clock(),
                                record.get('requestHash'), image_hashes)
        if selected is not None and selected in state['pending']:
            state['pending'].remove(selected)
    except (RegistryFailure, azure.RegistryFailure) as exc:
        outcome, registry_error = 'paused', exc.reason
        if target is not None:
            _set_reason(state, target['name'], periods, 'paused')
        if verified is not None:
            record = state['sources'].get(facts.source_key(verified))
            if record and record['status'] == 'issued':
                facts.record_source(state, verified, 'failed', 'analysis_failed',
                                    clock(), record['requestHash'])
    except Exception as exc:
        reason = getattr(exc, 'reason', str(exc))
        if 'budget' in reason:
            outcome, coverage_reason = 'budget-exhausted', 'budget_wait'
        elif any(word in reason for word in ('paused', 'cooldown', 'backoff', 'auth_stopped')):
            image_stop = (verified is not None and verified['media']
                          and source_safety.paused_for(source.state, 'images', clock()))
            performed = client.requests if isinstance(getattr(client, 'requests', None), dict) else counts
            outcome = 'partial' if performed['searches'] else 'paused'
            coverage_reason = 'image_host_paused' if image_stop else 'paused'
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
                attempts = selected.get('attempts', 1) if selected else 1
                transient = reason in TRANSIENT_REASONS or (
                    reason == 'azure_http_error' and (getattr(exc, 'status', None) or 0) >= 500)
                record = state['sources'][key]
                previous_requests = list(record.get('retry', {}).get('previousRequests', []))
                if record['requestHash'] and record['requestHash'] not in previous_requests:
                    previous_requests.append(record['requestHash'])
                record['retry'] = {
                    'attempts': attempts, 'notBefore': facts.stamp(clock() + dt.timedelta(hours=1)),
                    'lastReason': reason, 'previousRequests': previous_requests}
                record['reason'] = 'transient_retry' if transient and attempts < 3 else (
                    'retry_exhausted' if transient else 'permanent_failure')
                _set_reason(state, target['name'], periods, record['reason'])
                if selected in state['pending']:
                    if transient and attempts < 3:
                        selected['nextAttemptAt'] = record['retry']['notBefore']
                    else:
                        state['pending'].remove(selected)
            elif record and isinstance(exc, ValueError):
                facts.record_source(state, verified, 'failed', 'source_failed', clock())
                if selected in state['pending']:
                    state['pending'].remove(selected)
    finally:
        client.close()
    if search_before is not None and not search_succeeded:
        for start, previous in search_before.items():
            row = state['coverage'][start][search_target['name']]
            row['lastSearchedAt'], row['nextCheckAt'] = previous
    state['lastRun'] = {'status': outcome}
    refresh_coverage(state, reasons, clock(), manual, registry=registry)
    if source_safety.paused_for(source.state, 'images', clock()):
        pending_ids = {item['id'] for item in state['pending']}
        for record in state['sources'].values():
            if record['source']['id'] in pending_ids and record['source']['media']:
                _set_reason(state, record['source']['name'], periods, 'image_host_paused')
    if registry_error is not None and target is not None:
        _set_reason(state, target['name'], periods, 'paused')
    if isinstance(getattr(client, 'requests', None), dict):
        counts.update(client.requests)
    if any(counts.values()):
        update_check_time(state, clock(), successful=outcome in ('ok', 'no-new', 'no-results'))
    else:
        state['checkedAt'] = prior_checked
    facts.validate_state(state)
    save()
    return completion_report(state, outcome, counts, clock(), registry=registry,
                             existing_bindings=existing_bindings, registry_error=registry_error)


def _combined_reading(results):
    """Combine disjoint packs locally, without inventing rows or resolving conflicts."""
    periods, complete = {}, bool(results)
    for result in results:
        complete &= result['complete'] and result['classification'] != 'uncertain'
        for period in result['periods'] or []:
            key = tuple(period[field] for field in ('month', 'half', 'printedYear', 'textYear'))
            if key not in periods:
                periods[key] = copy.deepcopy(period)
                continue
            previous = periods[key]
            previous['imageIndexes'] = sorted(set(previous['imageIndexes'] + period['imageIndexes']))
            if previous['days'] is None or period['days'] is None:
                complete = False
                previous['days'] = previous['days'] or period['days']
                continue
            known = {row['day']: row for row in previous['days'] if row['day'] is not None}
            for row in period['days']:
                old = known.get(row['day'])
                if old is None:
                    previous['days'].append(copy.deepcopy(row))
                    known[row['day']] = row
                elif any(old.get(field) != row.get(field) for field in (
                        'weekday', 'shifts', 'qualifier', 'explicitStart', 'explicitEnd', 'operation')):
                    complete = False
    return {'classification': 'schedule' if periods else 'non_schedule' if complete else 'uncertain',
            'complete': bool(complete), 'periods': list(periods.values())}


def _reading_pack(text, views, spec):
    mapping = {view['variantId']: view for view in views}
    selected = [mapping[key] for key in spec['ids']]
    # Repacking only measures the saved pack; its original part numbers bind the wire hash.
    pack = azure.reading.pack_attachments(text, selected)[0]
    if len(pack['images']) != len(selected):
        raise ValueError('invalid_saved_reading_pack')
    pack.update(part=spec['part'], parts=spec['parts'])
    return pack


def read_evidence(state, source, text, images, analyzer, periods, cache, clock, save,
                  on_issued, *, allowed=lambda: True):
    """Resume the finite original/detail plan; every raw result stays in encrypted cache."""
    key = facts.source_key(source)
    value = cache.get(key)
    if value is None:
        raise ValueError('evidence_cache_missing')
    progress = state['readings'][key]
    bundle = value['reading']
    variants, seen = value['variants'], value['seen']
    changed = False

    def persist():
        cache.put(key, source, text, images, reading=bundle, variants=variants, seen=seen)
        progress['attemptedVariants'] = list(seen)
        progress['stage'] = bundle['stage']
        state['readings'][key] = progress
        save()

    def plan(stage, previous=None):
        packs = azure.reading_packs(source, text, images, stage=stage, previous=previous, seen=seen,
                                    full_context=stage == 'detail')
        existing = {view['variantId'] for view in variants}
        for pack in packs:
            for view in pack['images']:
                if view['kind'] != 'original' and view['variantId'] not in existing:
                    variants.append(view)
                    existing.add(view['variantId'])
        bundle['packs'] = [{'ids': [view['variantId'] for view in pack['images']],
                            'part': pack['part'], 'parts': pack['parts']} for pack in packs]
        bundle['stage'] = stage
        persist()

    if bundle['stage'] == 'fetch':
        plan('original')
    if bundle['stage'] in ('held', 'done'):
        return False
    while bundle['stage'] in ('original', 'detail'):
        stage = bundle['stage']
        for spec in bundle['packs']:
            if not allowed():
                raise Failure('time_limit')
            pack = _reading_pack(text, [*azure.reading.original_views(images), *variants], spec)
            messages, prepared = azure.prepare_reading_request(source, text, images, pack)
            messages.clear()
            request = prepared['requestHash']
            if request in bundle['issued']:
                continue

            def issued(request_hash):
                if request_hash != request:
                    raise ValueError('reading_request_changed')
                bundle['issued'].append(request)
                seen.extend(view['variantId'] for view in pack['images'] if view['variantId'] not in seen)
                persist()
                on_issued(request)

            try:
                schedules, proof, result = analyzer.analyze_reading(
                    source, text, images, periods, issued, pack=pack)
            except azure.AnalysisFailure as exc:
                if exc.reason == 'azure_invalid_output' and request in bundle['issued']:
                    bundle['results'].append({'stage': stage, 'requestHash': request,
                                              'result': analyzer.last_reading, 'analysis': None})
                    persist()
                    continue
                raise
            bundle['results'].append({'stage': stage, 'requestHash': request,
                                      'result': result, 'analysis': proof})
            persist()
            # Earlier chunks remain partial; the final chunk is applied after local aggregation.
            if spec is not bundle['packs'][-1] and schedules:
                changed |= facts.apply_reading_revision(state, schedules, source, proof)
                save()
        results = [row for row in bundle['results'] if row['stage'] == stage]
        valid = [row for row in results if row['analysis'] is not None]
        all_received = len(valid) == len(bundle['packs']) and bool(valid)
        combined = _combined_reading([row['result'] for row in valid])
        if not all_received:
            combined['complete'] = False
        if stage == 'detail':
            ids = {identity for spec in bundle['packs'] for identity in spec['ids']}
            submitted = [view for view in variants if view['variantId'] in ids]
            known_days = {(period['month'], row['day']) for previous in bundle['results']
                          if previous['stage'] == 'original' and previous['analysis'] is not None
                          for period in previous['result']['periods'] or []
                          for row in period['days'] or [] if row['day'] is not None}
            detail_days = {(period['month'], row['day']) for period in combined['periods'] or []
                           for row in period['days'] or [] if row['day'] is not None}
            if (not known_days <= detail_days or any(not azure.reading.covers_original(submitted, index)
                                                    for index in range(len(images)))):
                combined['complete'] = False
        schedules, complete = azure.normalize_reading(
            combined, source, text, len(images), periods,
            image_metadata=[azure.probe_image(image['bytes'], image['mime']) for image in images])
        if valid and all_received:
            proof = copy.deepcopy(valid[-1]['analysis'])
            if len(valid) > 1:
                proof['readingBatch'] = [row['analysis'] for row in valid]
            if schedules:
                # Reuse the last receipt only for the same deterministic aggregate.
                changed |= facts.apply_reading_revision(state, schedules, source, proof)
                save()
        elif valid:
            for row in valid:
                partial, _ = azure.normalize_reading(
                    row['result'], source, text, len(images), periods,
                    image_metadata=row['analysis']['images'])
                for table in partial:
                    table['reading']['complete'] = False
                if partial:
                    changed |= facts.apply_reading_revision(state, partial, source, row['analysis'])
                    save()
        if ('replyToId' in source and all_received and schedules
                and not azure.reading.needs_reread(combined, text)):
            bundle['stage'], progress['reason'] = 'done', 'reading_partial'
        elif complete and schedules:
            bundle['stage'], progress['reason'] = 'done', 'valid_schedule'
        elif complete and combined['periods']:
            bundle['stage'], progress['reason'] = 'done', 'reading_outside_period'
        elif stage == 'original' and images:
            # Image-backed empty negatives are not terminal, even without caption keywords.
            previous = {**combined, 'classification': 'uncertain', 'complete': False}
            plan('detail', previous)
            continue
        elif not images and all_received and all(
                row['result']['classification'] == 'non_schedule' for row in valid):
            bundle['stage'], progress['reason'] = 'done', 'not_schedule'
        else:
            bundle['stage'] = 'held'
            progress['reason'] = 'reading_partial' if schedules or any(
                table['id'] == source['id'] for table in state.get('partialSchedules', [])) else 'reading_uncertain'
        progress['nextAt'] = None
        persist()
    return changed


def _cycle_failure(exc, now):
    reason = getattr(exc, 'reason', str(exc) if isinstance(exc, ValueError) else None)
    retry = getattr(exc, 'retry_at', None)
    if reason in ('time_limit', 'azure_deadline'):
        return 'time_limit', now + dt.timedelta(minutes=1), True
    if reason and 'budget' in reason:
        month = (now.astimezone(facts.JST).date().replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return 'budget_wait', retry or dt.datetime.combine(month, dt.time(), facts.JST), True
    if reason and any(word in reason for word in ('cache', 'evidence_')):
        return ('image_cache_expired' if 'expired' in reason else 'image_cache_invalid'), None, False
    if reason and any(word in reason for word in ('author', 'reply_', 'identity')):
        return 'identity_unknown', None, False
    if reason and any(word in reason for word in ('cooldown', 'paused', 'backoff', 'auth_stopped')):
        return 'paused', retry or now + dt.timedelta(hours=1), False
    if reason is not None or isinstance(exc, OSError):
        return 'reading_pending', retry or now + dt.timedelta(hours=6), False
    raise exc


def _cycle_service_day(value):
    try:
        return facts.day(value)
    except ValueError:
        raise ValueError('invalid_schedule_service_date') from None


def collect_cycle(state, schedule, source, analyzer, client_factory, *, clock, save, registry=None,
                  existing_bindings=None, registry_guard=None, cache=None, runtime_seconds=1110,
                  resume=False, cycle_id=None, service_date=None):
    """Resume saved cohort/periods across dates; clock still governs all paid I/O.

    service_date optionally fixes a delayed new cycle's work date. Resume always
    uses the saved cohort, not this argument. collection.serviceDate records that
    original JST date; optional cloud mode is schedule/both. Legacy collections
    without either field remain resumable.
    Neither field substitutes for the accounting, deadline, host or cache clock.
    """
    if type(runtime_seconds) is not int or not 1 <= runtime_seconds <= 3600:
        raise ValueError('invalid_schedule_runtime')
    facts.validate_state(state)
    started = clock()
    service_day = (_cycle_service_day(service_date) if service_date is not None
                   else started.astimezone(facts.JST).date())
    update_check_time(state, started)
    allowed = lambda: (clock() - started).total_seconds() < runtime_seconds
    targets, reasons, bindings = target_population(
        state, schedule, {}, [], existing_bindings, registry=registry)
    manual = schedule.get('schedule', {})
    current = state.get('collection')
    if resume and current is None:
        raise ValueError('schedule_resume_missing')
    if resume and cycle_id is not None and current['chainId'] != cycle_id:
        raise ValueError('schedule_resume_mismatch')
    if not resume:
        discovery = facts.discovery_periods(service_day)
        identity = cycle_id or source.run_id
        if not re.fullmatch(r'[1-9][0-9]{0,19}-[1-9][0-9]{0,19}', identity):
            identity = f'{int(started.timestamp())}-{int(facts.digest(identity.encode())[:12], 16) or 1}'
        names = ({member['canonicalName'] for member in registry['members'] if member['membership'] == 'active'}
                 if registry is not None else set(reasons))
        current = {'chainId': identity, 'names': sorted(names), 'periods': [list(p) for p in discovery],
                   'reason': 'processing', 'ready': False, 'cursor': 0, 'nextAt': None,
                   'serviceDate': service_day.isoformat()}
        state['collection'] = current
    else:
        discovery = [tuple(period) for period in current['periods']]
        targets = {name: target for name, target in targets.items() if name in current['names']}
        reasons = {name: reason for name, reason in reasons.items() if name in current['names']}
    # A full-month table also covers the original following half. Reconstruct that
    # extraction window from saved periods, never from the resumed accounting date.
    anchor = min(facts.day(start) for start, _ in discovery)
    periods = sorted(set(facts.target_periods(anchor)) | set(discovery))
    refresh_coverage(state, reasons, started, manual, registry=registry,
                     terminal_confirmed=True, periods=periods)
    candidate_since = facts.candidate_start(anchor)
    current.setdefault('cursor', 0)
    current.setdefault('nextAt', None)
    initial_cursor = current['cursor']
    state.setdefault('readings', {})
    stale = {item['id'] for item in state['pending'] if item['name'] in targets
             and facts.timestamp(item['searchCreatedAt']).astimezone(facts.JST).date()
             < candidate_since}
    state['pending'] = [item for item in state['pending'] if item['id'] not in stale]
    for progress in state['readings'].values():
        if progress['sourceId'] in stale:
            progress.update(stage='held', reason='stale_candidate', nextAt=None)
    counts = {'searches': 0, 'posts': 0, 'images': 0, 'analysis': 0}
    stop, changed, errors = False, False, []
    failures = {}
    old_deadline = getattr(analyzer.usage, 'deadline', None)
    analyzer.usage.deadline = lambda: allowed() and (old_deadline is None or old_deadline())

    def confirmed(name):
        return all(state['coverage'][start][name]['confirmedIds'] for start, _ in discovery)

    def guard(client, name):
        check_registry(registry_guard, name)
        client.registry_name = name
        if not allowed():
            raise Failure('time_limit')

    try:
        if cache is None:
            for name in current['names']:
                if name in targets and not confirmed(name):
                    _set_reason(state, name, discovery, 'image_cache_unconfigured')
            errors.append('image_cache_unconfigured')
        else:
            for position, name in enumerate(current['names']):
                reply_waiting = any(item['name'] == name and item.get('replyCandidate')
                                    for item in state['pending'])
                if name not in targets or confirmed(name) and not reply_waiting:
                    current['cursor'] = max(current['cursor'], position + 1)
                    continue
                client = client_factory()
                client.chunk_images = True
                selected, progress = None, None
                failure_stage, failure_host, failure_image = 'source', None, None
                try:
                    guard(client, name)
                    pending = [item for item in state['pending'] if item['name'] == name]
                    if not pending and position >= current['cursor']:
                        due = any(not state['coverage'][start][name]['confirmedIds'] and (
                            state['coverage'][start][name]['nextCheckAt'] is None
                            or facts.timestamp(state['coverage'][start][name]['nextCheckAt']) <= clock())
                                  for start, _ in discovery)
                        if due:
                            source.check('searches')
                            counts['searches'] += 1
                            document = client.search(targets[name]['handle'])
                            candidates, truncated = discover(
                                document, {name: targets[name]}, clock(), bindings, registry=registry,
                                other_bindings=(state['identityBindings'], existing_bindings or {}),
                                candidate_since=candidate_since)
                            del document
                            dropped = enqueue(state, candidates, clock(), analyzer)
                            for start, _ in discovery:
                                row = state['coverage'][start][name]
                                if not row['confirmedIds']:
                                    row.update(lastSearchedAt=facts.stamp(clock()),
                                               nextCheckAt=facts.stamp(clock() + dt.timedelta(hours=6)),
                                               reason='queue_limit' if name in dropped else 'candidate_limit' if truncated else
                                               'post_unverified' if candidates else 'source_not_found')
                            save()
                            pending = [item for item in state['pending'] if item['name'] == name]
                    for selected in sorted(pending, key=lambda item: (item['priority'], -int(item['id']))):
                        progress = None
                        failure_stage, failure_host, failure_image = 'source', POST_HOST, None
                        guard(client, name)
                        if confirmed(name) and not selected.get('replyCandidate'):
                            continue
                        if selected['nextAttemptAt'] and facts.timestamp(selected['nextAttemptAt']) > clock():
                            continue
                        known = [(key, record) for key, record in state['readings'].items()
                                 if record['sourceId'] == selected['id']]
                        if not known:
                            cached_keys = cache.find_source(selected['id'], selected['authorId'])
                            if len(cached_keys) > 1:
                                raise ValueError('evidence_cache_ambiguous')
                            if cached_keys:
                                key = cached_keys[0]
                                failure_stage, failure_host = 'cache', None
                                recovered = cache.get(key)
                                progress = {'sourceId': selected['id'], 'cacheKey': key,
                                            'stage': recovered['reading']['stage'], 'reason': 'reading_pending',
                                            'nextAt': None, 'attemptedVariants': recovered['seen']}
                                if progress['stage'] in ('done', 'held'):
                                    # Replay existing results offline, not the already issued requests.
                                    progress['stage'] = (recovered['reading']['results'][-1]['stage']
                                                         if recovered['reading']['results'] else 'original')
                                    recovered['reading']['stage'] = progress['stage']
                                    cache.put(key, recovered['source'], recovered['text'], recovered['images'],
                                              reading=recovered['reading'])
                                state['readings'][key] = progress
                                known = [(key, progress)]
                        progress = known[0][1] if known else None
                        if progress and (progress['stage'] in ('done', 'held')
                                         or progress['nextAt'] and facts.timestamp(progress['nextAt']) > clock()):
                            continue
                        if known:
                            key = known[0][0]
                            failure_stage, failure_host = 'cache', None
                            value = cache.get(key)
                            if value is None:
                                raise ValueError('evidence_cache_missing')
                            verified, text, images, bundle = (
                                value['source'], value['text'], value['images'], value['reading'])
                            facts.validate_source(verified)
                            if (verified['name'] != name or verified['id'] != selected['id']
                                    or verified['authorId'] != selected['authorId']
                                    or verified['authorScreenName'].casefold() != targets[name]['handle'].casefold()
                                    or not matching_binding(bindings, name, verified['authorId'],
                                                            verified['authorScreenName'], registry=registry)):
                                raise ValueError('author_mismatch')
                            facts.bind_identity(state, verified)
                            if key not in state['sources']:
                                facts.record_source(state, verified, 'pending', 'reading_pending', clock())
                            save()
                        else:
                            analyzer.usage.check()
                            source.check('posts')
                            counts['posts'] += 1
                            payload, payload_hash = client.post(selected['id'])
                            parent = None
                            parent_id = payload.get('in_reply_to_status_id_str', payload.get('in_reply_to_status_id'))
                            if parent_id is not None and payload.get('parent') is None:
                                reply_author = payload.get('in_reply_to_user_id_str', payload.get('in_reply_to_user_id'))
                                if (not official.post_id(parent_id) or author_id(reply_author) != selected['authorId']
                                        or not matching_author(payload.get('user', {}), selected['authorId'])):
                                    raise ValueError('reply_parent_unverified')
                                source.check('posts')
                                counts['posts'] += 1
                                parent, _ = client.post(str(parent_id))
                            verified, text, urls = validate_post(
                                selected, payload, targets[name], clock(), bindings.get(name),
                                payload_hash=payload_hash, parent_payload=parent)
                            del payload, parent
                            key = facts.source_key(verified)
                            images = []
                            bundle = {'stage': 'fetch', 'urls': urls, 'issued': [], 'results': [], 'packs': []}
                            cache.put(key, verified, text, images, reading=bundle)
                            facts.bind_identity(state, verified)
                            facts.record_source(state, verified, 'pending', 'not_issued', clock())
                            progress = {'sourceId': verified['id'], 'cacheKey': key, 'stage': 'fetch',
                                        'reason': 'reading_pending', 'nextAt': None, 'attemptedVariants': []}
                            state['readings'][key] = progress
                            save()
                        if bundle['stage'] == 'fetch':
                            for url in bundle['urls'][len(images):]:
                                failure_stage, failure_host, failure_image = 'fetch', PHOTO_HOST, len(images)
                                guard(client, name)
                                source.check('images')
                                counts['images'] += 1
                                images.append(client.image(url))
                                cache.put(key, verified, text, images, reading=bundle)
                            if len(images) != len(verified['media']):
                                raise ValueError('schedule_images_incomplete')

                        def issued(request):
                            counts['analysis'] += 1
                            facts.record_source(state, verified, 'issued', 'reading_pending', clock(),
                                                request, [facts.digest(bytes(image['bytes'])) for image in images])
                            save()

                        failure_stage, failure_host, failure_image = progress['stage'], None, None
                        changed |= read_evidence(state, verified, text, images, analyzer, periods, cache,
                                                 clock, save, issued, allowed=allowed)
                        progress.pop('failure', None)
                        for period in discovery:
                            reason = progress['reason']
                            if reason == 'valid_schedule' and not any(
                                    table['name'] == name and table['period']['from'] == period[0]
                                    for table in state['schedules']):
                                reason = 'reading_outside_period'
                            _set_reason(state, name, [period], reason)
                        if progress['stage'] == 'done':
                            state['pending'] = [item for item in state['pending'] if item['id'] != selected['id']]
                            if progress['reason'] in ('reading_outside_period', 'not_schedule'):
                                facts.record_source(state, verified, 'negative',
                                                    'outside_period' if progress['reason'] == 'reading_outside_period'
                                                    else 'not_schedule', clock(),
                                                    image_hashes=[facts.digest(bytes(image['bytes'])) for image in images])
                        refresh_coverage(state, reasons, clock(), manual, registry=registry,
                                         terminal_confirmed=True, periods=periods)
                        save()
                        client.close()
                    current = state['collection']
                    current['cursor'] = max(current['cursor'], position + 1)
                except Exception as exc:
                    if progress is not None and failure_host is None and failure_stage != 'cache':
                        failure_stage = progress['stage']
                    reason, next_at, stop = _cycle_failure(exc, clock())
                    if (isinstance(exc, ValueError) and reason == 'reading_pending'
                            and progress is not None and progress['stage'] != 'fetch'):
                        reason, next_at = 'reading_uncertain', None
                        progress['stage'] = 'held'
                    if reason == 'reading_pending' and progress is not None and progress['stage'] == 'fetch':
                        reason = 'image_fetch_failed'
                    elif reason == 'reading_pending' and progress is None:
                        reason = 'source_failed'
                    errors.append(reason)
                    _set_reason(state, name, discovery, reason)
                    if selected is not None:
                        selected = next((item for item in state['pending'] if item['id'] == selected['id']), selected)
                        selected['nextAttemptAt'] = facts.stamp(next_at) if next_at else None
                    if progress is not None:
                        progress.update(reason=reason, nextAt=facts.stamp(next_at) if next_at else None)
                        if reason in ('image_cache_expired', 'image_cache_invalid', 'identity_unknown'):
                            progress['stage'] = 'held'
                    else:
                        for start, _ in discovery:
                            state['coverage'][start][name]['nextCheckAt'] = facts.stamp(next_at) if next_at else None
                    if selected is not None:
                        status = getattr(exc, 'status', None)
                        failure = {
                            'name': name, 'postId': selected['id'], 'postUrl': selected['url'],
                            'imageIndex': failure_image, 'failedAt': facts.stamp(clock()),
                            'host': failure_host,
                            'httpStatus': status if type(status) is int and 100 <= status <= 599 else None,
                            'retryAt': facts.stamp(next_at) if next_at else None,
                            'stage': failure_stage,
                            'nextStage': progress['stage'] if progress is not None else
                            'held' if reason in ('image_cache_expired', 'image_cache_invalid') else 'source',
                            'reason': reason}
                        failures[selected['id']] = failure
                        if progress is not None:
                            progress['failure'] = failure
                    if stop:
                        break
                    current = state['collection']
                    current['cursor'] = max(current['cursor'], position + 1)
                finally:
                    client.close()
                    save()
    finally:
        analyzer.usage.deadline = old_deadline
    current = state['collection']
    pending_ids = {item['id'] for item in state['pending'] if item['name'] in current['names']
                   and item['name'] in targets and (not confirmed(item['name']) or item.get('replyCandidate'))}
    pending_progress = [record for record in state['readings'].values()
                        if record['stage'] not in ('done', 'held') and record['sourceId'] in pending_ids]
    unresolved = [name for name in current['names'] if name in reasons
                  and (name not in targets or not confirmed(name))]
    next_times = [row['nextAt'] for row in pending_progress if row['nextAt']]
    next_times += [state['coverage'][start][name]['nextCheckAt'] for name in current['names'] if name in targets
                   for start, _ in discovery if not state['coverage'][start][name]['confirmedIds']
                   and state['coverage'][start][name]['nextCheckAt']
                   and not any(item['name'] == name for item in state['pending'])]
    remaining = bool(pending_progress or current['cursor'] < len(current['names']))
    progressed = any(counts.values()) or changed or current['cursor'] > initial_cursor
    current.update(ready=bool('time_limit' in errors and remaining and progressed),
                   reason='time_limit' if 'time_limit' in errors else
                   'waiting' if errors or remaining or unresolved else 'complete',
                   nextAt=min(next_times) if next_times else None)
    outcome = ('budget-exhausted' if 'budget_wait' in errors else 'partial' if errors or remaining or unresolved
               else 'ok' if changed else 'no-new')
    state['lastRun'] = {'status': outcome}
    if any(counts.values()):
        update_check_time(state, clock(), successful=changed)
    facts.validate_state(state)
    save()
    report, code = completion_report(state, outcome, counts, clock(), registry=registry,
                                     existing_bindings=existing_bindings)
    report.update(continuation=copy.deepcopy(current),
                  nextStage='complete' if current['reason'] == 'complete' else 'resume' if remaining else 'waiting',
                  nextEligible=current['nextAt'], reasons=sorted(set(errors)), unresolvedCount=len(unresolved))
    for record in state['readings'].values():
        failure = record.get('failure')
        if failure and failure['name'] in current['names']:
            failures.setdefault(failure['postId'], failure)
    report['diagnostics'] = copy.deepcopy(list(failures.values()))
    return report, code


def replay_saved(state, *, document, payload_bytes, images, result, schedule, insights,
                 accounts, post_id, now, receipt_id, allowed_periods=None, existing_bindings=None,
                 contract_version=facts.VERSION, parent_payload_bytes=None, pack=None):
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
    parent = None
    if parent_payload_bytes is not None:
        if not isinstance(parent_payload_bytes, bytes) or len(parent_payload_bytes) > MAX_DOCUMENT_BYTES:
            raise ValueError('invalid_saved_payload')
        parent = azure.transport.strict_json(parent_payload_bytes)
    source, text, urls = validate_post(selected, payload, targets[selected['name']], now,
                                      bindings.get(selected['name']),
                                      payload_hash=facts.digest(payload_bytes), parent_payload=parent)
    if len(images) != len(urls):
        raise ValueError('schedule_images_incomplete')
    parsed, proof = azure.saved_result(source, text, images, result, now=now, receipt_id=receipt_id,
                                      allowed_periods=allowed_periods or facts.target_periods(now),
                                      contract_version=contract_version, pack=pack)
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
    apply = (facts.apply_reading_revision if value['analysis']['contract'] == facts.READING_VERSION
             else facts.apply_revision)
    apply(facts.empty_state(), value['schedules'], value['source'], value['analysis'])
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
    parser.add_argument('--catch-up', action='store_true', help='share the bounded recovery run budget')
    parser.add_argument('--cycle', action='store_true', help='finite v3 person/half cohort with encrypted resume')
    parser.add_argument('--resume', action='store_true', help='resume the saved finite cohort; requires --cycle')
    parser.add_argument('--cycle-id', help='stable finite cohort ID; does not replace the accounting run ID')
    parser.add_argument('--service-date', help='new cycle work date YYYY-MM-DD; resume keeps its saved scope')
    parser.add_argument('--evidence-cache', type=Path, help='outside-checkout _private-evidence/cache.bin')
    parser.add_argument('--max-runtime-seconds', type=int, default=1110)
    parser.add_argument('--max-searches', type=int, choices=(0, 1), default=1)
    parser.add_argument('--max-posts', type=int, choices=(0, 1), default=1)
    parser.add_argument('--max-images', type=int, choices=range(5), default=4)
    parser.add_argument('--schedule', type=Path, default=ROOT / 'data' / 'schedule.js')
    parser.add_argument('--insights', type=Path, default=ROOT / 'data' / 'store-insights.js')
    parser.add_argument('--accounts', type=Path, default=ROOT / 'tools' / 'data' / 'accounts.csv')
    parser.add_argument('--members', type=Path, default=ROOT / 'data' / 'members.json')
    parser.add_argument('--node', type=Path)
    parser.add_argument('--publish', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    return parser


def run(args, *, clock=official.utc_now, sleep=time.sleep, environment=None):
    cycle = getattr(args, 'cycle', False)
    service_date = getattr(args, 'service_date', None)
    if service_date is not None:
        if not cycle:
            raise ValueError('schedule_service_date_requires_cycle')
        _cycle_service_day(service_date)
    if getattr(args, 'resume', False) and not cycle:
        raise ValueError('schedule_resume_requires_cycle')
    if args.source_run_id is not None and args.source_run_id != args.analysis_run_id:
        raise ValueError('schedule_run_id_mismatch')
    writes = [path.resolve() for path in (args.snapshot, args.source_state, args.http_state,
                                         args.ai_state, args.publish, args.report) if path]
    inputs = {path.resolve() for path in (
        args.schedule, args.insights, args.accounts, args.personal_state, args.members) if path}
    if (len(set(writes)) != len(writes) or set(writes) & inputs
            or any(path.suffix != '.json' for path in writes)
            or args.snapshot.resolve().parent == (ROOT / 'data').resolve()):
        raise ValueError('unsafe_storage_paths')
    source_module = _module('source-state.py', 'half_month_source_usage')
    usage_module = _module('analysis-state.py', 'half_month_ai_usage')

    def defer(state, status):
        check_registry(registry_guard)
        _, reasons, _ = target_population(
            state, {}, {}, [], personal_state['identityBindings'], registry=registry)
        refresh_coverage(state, reasons, clock(), {}, registry=registry, terminal_confirmed=cycle)
        state['lastRun'] = {'status': status}
        facts.validate_state(state)
        official.atomic_json(args.snapshot, state)
        report, code = completion_report(state, status, {
            'searches': 0, 'posts': 0, 'images': 0, 'analysis': 0}, clock(),
            registry=registry, existing_bindings=personal_state['identityBindings'])
        report['exitCode'] = code
        if args.publish and not args.dry_run:
            official.atomic_json(args.publish, facts.public_state(state))
        if args.report:
            official.atomic_json(args.report, report)
        return report, code

    with ExitStack() as stack:
        stack.enter_context(official.ProcessLock(args.snapshot.with_suffix('.lock')))
        state = facts.read_state(args.snapshot)
        personal_state = personal.read_state(args.personal_state)
        registry_guard = members.RegistryGuard(args.members, bindings=lambda: (
            personal.read_state(args.personal_state)['identityBindings'], state['identityBindings']))
        registry = registry_guard.registry
        if args.analysis_limit == 0:
            source_module.validate_legacy(
                source_module.load_state(args.source_state, required=True),
                personal_state,
                usage_module.load_state(args.ai_state, required=True))
            return defer(state, 'budget-exhausted')
        current_name = [None]

        def guarded_sleep(seconds):
            check_registry(registry_guard, current_name[0])
            if cycle and (clock() - cycle_started).total_seconds() + seconds >= args.max_runtime_seconds:
                raise Failure('time_limit')
            sleep(seconds)
            check_registry(registry_guard, current_name[0])

        cycle_started = clock()
        source = stack.enter_context(source_module.SharedSource(
            args.source_state, run_id=args.analysis_run_id, component='schedule',
            clock=clock, sleep=guarded_sleep, personal_path=args.personal_state, catch_up=args.catch_up))
        usage = stack.enter_context(usage_module.SharedUsage(
            args.ai_state, run_id=args.analysis_run_id, component='schedule', clock=clock,
            sleep=guarded_sleep, request_limit=None if cycle else 1,
            run_limit=None if cycle else usage_module.CATCHUP_RUN_LIMIT if args.catch_up else usage_module.RUN_LIMIT))
        if not cycle and all(source_safety.paused_for(source.state, kind, clock()) for kind in ('searches', 'posts')):
            return defer(state, 'paused')
        try:
            if not cycle:
                usage.check()
        except usage_module.UsageFailure as exc:
            if exc.reason == 'azure_budget_exhausted':
                return defer(state, 'budget-exhausted')
            if exc.reason in ('azure_auth_stopped', 'azure_backoff'):
                return defer(state, 'paused')
            raise
        if not target_population(state, {}, {}, [], personal_state['identityBindings'],
                                 registry=registry)[0]:
            return defer(state, 'no-new')
        if not cycle and not (args.max_searches or args.max_posts):
            return defer(state, 'no-new')
        schedule = personal.read_js(args.schedule, 'SCHEDULE_DATA', args.node)
        analyzer = azure.AzureAnalyzer(
            usage, os.environ if environment is None else environment, clock=clock,
            registry_guard=registry_guard)
        client = SourceClient(source, clock=clock, http_state=args.http_state, registry_guard=registry_guard)

        def save():
            current_name[0] = client.registry_name
            facts.validate_state(state)
            official.atomic_json(args.snapshot, state)

        if cycle:
            cache = None
            environment = os.environ if environment is None else environment
            if args.evidence_cache is not None:
                cache = _module('schedule-evidence-cache.py', 'half_month_private_cache').EvidenceCache(
                    args.evidence_cache, environment.get('SCHEDULE_EVIDENCE_KEY'), clock=clock)
            def cycle_client():
                current = SourceClient(source, clock=clock, http_state=args.http_state,
                                       registry_guard=registry_guard, chunk_images=True)
                # The existing guarded-sleep callback reads this client through save().
                nonlocal client
                client = current
                return current
            report, code = collect_cycle(
                state, schedule, source, analyzer, cycle_client, clock=clock, save=save,
                registry=registry, registry_guard=registry_guard,
                existing_bindings=personal_state['identityBindings'], cache=cache,
                runtime_seconds=args.max_runtime_seconds, resume=args.resume, cycle_id=args.cycle_id,
                service_date=service_date)
        else:
            report, code = collect(state, schedule, {}, [], client, source, analyzer,
                                   clock=clock, save=save, max_searches=args.max_searches,
                                   max_posts=args.max_posts, max_images=args.max_images,
                                   existing_bindings=personal_state['identityBindings'],
                                   registry=registry, registry_guard=registry_guard)
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
    except Exception as exc:
        # Exception messages can contain transport bodies or local storage paths.
        safe = {'missing_evidence_cache_key', 'invalid_evidence_cache_key',
                'evidence_cache_authentication_failed', 'evidence_cache_capacity',
                'evidence_cache_expired', 'evidence_public_path_refused', 'evidence_symlink_refused',
                'schedule_resume_missing', 'schedule_resume_mismatch', 'schedule_resume_requires_cycle',
                'invalid_schedule_service_date', 'schedule_service_date_requires_cycle',
                'invalid_schedule_runtime'}
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in safe else 'schedule_infrastructure_failed'
        print(json.dumps({'completed': False, 'component': 'schedule', 'status': 'unavailable',
                          'collectionStatus': 'unavailable', 'exitCode': 1,
                          'reason': reason}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
