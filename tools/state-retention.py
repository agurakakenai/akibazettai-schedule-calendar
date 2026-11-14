"""Bounded retention for the collector-state files, applied before each state commit.

Only records that no longer protect money, idempotency or publication are compacted:

- ai-usage.json: settled receipts of closed JST months (before the UTC month-boundary day) move to
  retired[month] (request count, charged micro-JPY, per-component counts and 16-hex receipt-ID
  prefixes that still reject a second issue). Unsettled, usage_missing, reserved and interrupted
  receipts, monthly opening evidence and receipts referenced by half-month or personal reviews stay.
- source-usage.json: completed receipts older than SOURCE_DAYS move to retired[day][component][kind];
  403/429/401/3xx stop evidence and half-month image-recovery evidence stay.
- personal-shifts.json: azureAnalysis cache entries of posts older than the personal cache window
  (except pending, reviewed, history and saved-import posts).
- half-month-schedules.json: candidateHistory/sources/coverage of posts or periods older than one
  half-month before the oldest date any search can still accept, unless still referenced.
"""
import datetime as dt
import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JST = dt.timezone(dt.timedelta(hours=9))
SOURCE_DAYS = 14
PERSONAL_CACHE_DAYS = 14  # personal-azure.CACHE_RETENTION_DAYS
SIZE_WARNING_RATIO = 0.6
TWITTER_EPOCH_MS = 1288834974657


def _module(file, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _today(now):
    return now.astimezone(JST).date()


def _post_day(tid):
    milliseconds = (int(tid) >> 22) + TWITTER_EPOCH_MS
    return dt.datetime.fromtimestamp(milliseconds / 1000, dt.timezone.utc).astimezone(JST).date()


def _stamp_day(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(JST).date()


def ai_cutoff(now):
    """Receipts dated before this JST day are in a closed month and outside the UTC boundary day."""
    first = _today(now).replace(day=1)
    return (first - dt.timedelta(days=1)).isoformat()


def ai_references(half_month, personal):
    """AI receipt IDs that other state files still validate against."""
    keep = set()
    if half_month:
        keep |= set(half_month.get('receipts', {}))
        for revision in half_month.get('revisions', {}).values():
            analysis = revision.get('analysis', {})
            keep.add(analysis.get('receiptId'))
            keep |= {part.get('receiptId') for part in analysis.get('readingBatch', [])}
        keep |= {proof.get('usageReceiptId') for proof in half_month.get('savedImports', {}).values()}
    if personal:
        reviewed = {item['id'] for item in personal.get('resolved', [])
                    if 'linkReview' in item or 'placementReview' in item}
        for key, entry in personal.get('azureAnalysis', {}).get('cache', {}).items():
            if entry['postId'] in reviewed:
                keep.add(hashlib.sha256(('personal:' + key).encode()).hexdigest())
    keep.discard(None)
    return keep


def prune_ai(usage, now, *, keep=frozenset(), keep_runs=frozenset()):
    ledger = _module('analysis-state.py', 'retention_analysis_state')
    cutoff = ai_cutoff(now)
    opening = {key.split(':', 1)[1] for month in usage.get('money', {}).get('opening', {}).values()
               for key in month['records'] if key.startswith('native:')}
    retire = [key for key, receipt in usage['receipts'].items()
              if receipt['date'] < cutoff and (receipt.get('money') or {}).get('usageStatus') == 'settled'
              and receipt['completedAt'] is not None and key not in opening and key not in keep
              and not (receipt['component'] == 'schedule' and receipt['runId'] in keep_runs)]
    if not retire:
        return 0
    retired = usage.setdefault('retired', {})
    for key in retire:
        receipt = usage['receipts'].pop(key)
        month = retired.setdefault(receipt['date'][:7], {
            'requests': 0, 'chargedMicroJPY': 0, 'components': {}, 'receiptIds': []})
        month['requests'] += 1
        month['chargedMicroJPY'] += receipt['money']['chargedMicroJPY']
        month['components'][receipt['component']] = month['components'].get(receipt['component'], 0) + 1
        month['receiptIds'].append(key[:ledger.RETIRED_ID_LENGTH])
    for month in retired.values():
        month['receiptIds'] = sorted(set(month['receiptIds']))
    ledger.validate_state(usage)
    return len(retire)


def _stop_evidence(receipt):
    status = receipt['httpStatus']
    return receipt['status'] == 'failed' and status is not None and (status in (401, 403, 429)
                                                                     or 300 <= status < 400)


def recovery_hashes(half_month):
    """Post request hashes of half-month readings that a cache recovery may still verify."""
    if not half_month:
        return set()
    source = _module('source-state.py', 'retention_source_state')
    return {source.request_identity(
        'posts', f'https://cdn.syndication.twimg.com/tweet-result?id={row["sourceId"]}&lang=ja&token=a')[0]
        for row in half_month.get('readings', {}).values()}


def prune_source(state, now, *, keep_hashes=frozenset()):
    """Return (retired count, run IDs whose schedule receipts were kept as recovery evidence)."""
    source = _module('source-state.py', 'retention_source_state')
    cutoff = (_today(now) - dt.timedelta(days=SOURCE_DAYS)).isoformat()
    retire, runs = [], set()
    for key, receipt in state['receipts'].items():
        if receipt['date'] >= cutoff or receipt['completedAt'] is None or _stop_evidence(receipt):
            continue
        if receipt['component'] == 'schedule' and receipt['kind'] == 'posts' and receipt['requestHash'] in keep_hashes:
            runs.add(receipt['runId'])
            continue
        retire.append(key)
    if retire:
        retired = state.setdefault('retired', {})
        for key in retire:
            receipt = state['receipts'].pop(key)
            counts = retired.setdefault(receipt['date'], {}).setdefault(receipt['component'], {})
            counts[receipt['kind']] = counts.get(receipt['kind'], 0) + 1
        source.validate_state(state)
    return len(retire), runs


def prune_personal(state, now):
    analysis = state.get('azureAnalysis')
    if not analysis:
        return 0
    oldest = _today(now) - dt.timedelta(days=PERSONAL_CACHE_DAYS)
    protected = ({item['id'] for item in state['pending']} | set(analysis['review'])
                 | {post['id'] for post in analysis['history']}
                 | {item['id'] for item in state['resolved'] if 'linkReview' in item or 'placementReview' in item}
                 | {item['amendment']['id'] for item in state.get('savedPersonalImports', {}).values()})
    retire = [key for key, entry in analysis['cache'].items()
              if entry['postId'] not in protected and _post_day(entry['postId']) < oldest]
    for key in retire:
        del analysis['cache'][key]
    return len(retire)


def prune_half_month(state, now, usage):
    facts = _module('half-month-schedules.py', 'retention_half_month')
    start = facts.candidate_start(now)
    cutoff = facts.day(facts.half_period(start - dt.timedelta(days=1))[0])
    open_requests = {item['requestHash'] for item in (usage or {}).get('receipts', {}).values()
                     if item['completedAt'] is None
                     or (item.get('money') or {}).get('usageStatus') not in (None, 'settled')}
    keep_keys = ({revision['sourceKey'] for revision in state['revisions'].values() if 'sourceKey' in revision}
                 | {row['cacheKey'] for row in state['readings'].values()})
    keep_ids = ({item['id'] for item in state['pending']}
                | {row['sourceId'] for row in state['readings'].values()}
                | {item['id'] for field in ('schedules', 'partialSchedules') for item in state.get(field, [])})
    removed = 0
    for key, record in list(state['sources'].items()):
        if (_stamp_day(record['source']['createdAt']) < cutoff and key not in keep_keys
                and record['source']['id'] not in keep_ids and record.get('requestHash') not in open_requests):
            del state['sources'][key]
            removed += 1
    for key, item in list(state['candidateHistory'].items()):
        if _stamp_day(item['candidate']['searchCreatedAt']) < cutoff and item['candidate']['id'] not in keep_ids:
            del state['candidateHistory'][key]
            removed += 1
    active = {period[0] for period in state.get('collection', {}).get('periods', [])}
    for period in list(state['coverage']):
        if facts.day(facts.half_period(facts.day(period))[1]) < cutoff and period not in active:
            del state['coverage'][period]
            removed += 1
    facts.validate_state(state, private=True)
    return removed


def size_warnings(sizes, limit):
    return sorted(name for name, size in sizes.items() if size > limit * SIZE_WARNING_RATIO)
