"""Offline member administration; no collection, network, inference or statistics build.

members.json is authoritative; members.js is a deterministic public projection.
Existing schedules, statistics and author bindings are never rewritten here.
Commands: migrate, add-normal, set-account, set-status, set-display-name,
          check, export, report. Run with --help for arguments.
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
import sys
import tempfile
import urllib.parse
import uuid


ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc
JST = dt.timezone(dt.timedelta(hours=9))
NAME = re.compile(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}\Z')
HANDLE = re.compile(r'[A-Za-z0-9_]{1,15}\Z')
MEMBER_ID = re.compile(r'm-[0-9a-f]{32}\Z')
AUTHOR_ID = re.compile(r'[1-9][0-9]{0,24}\Z')
TRUSTS = {'official', 'legacy-verified', 'user-provided'}
RESERVED_ROUTES = {
    'about', 'account', 'accounts', 'compose', 'download', 'explore', 'hashtag',
    'help', 'home', 'i', 'intent', 'jobs', 'login', 'logout', 'messages',
    'notifications', 'privacy', 'search', 'settings', 'share', 'signup', 'tos',
    'widgets', 'akibazettai',
}
ROOT_FIELDS = {'schemaVersion', 'legacySnapshot', 'members', 'unresolvedNames', 'reservedHandles'}
STATUS_FIELDS = {'membership', 'collection', 'inactiveFrom', 'statusRecordedAt'}
MEMBER_FIELDS = {
    'memberId', 'canonicalName', 'displayName', 'aliases', 'role', 'xProfileUrl',
    'accountTrust', 'registeredAt', 'registrationSource', 'homeStore',
    'homeStoreSource', 'officialListing', 'orderBefore', 'statusHistory',
} | STATUS_FIELDS
PUBLIC_MEMBER_FIELDS = (
    'memberId', 'canonicalName', 'displayName', 'aliases', 'role', 'membership',
    'collection', 'inactiveFrom', 'xProfileUrl', 'homeStore', 'homeStoreSource',
    'officialListing', 'orderBefore',
)
MAX_BYTES = 4 * 1024 * 1024


class RegistryError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise RegistryError(reason)


def fields(value, expected, reason='invalid_registry_fields'):
    require(isinstance(value, dict) and set(value) == set(expected), reason)


def choice(value, allowed, reason):
    require(isinstance(value, str) and value in allowed, reason)


def valid_name(value):
    require(isinstance(value, str) and NAME.fullmatch(value), 'invalid_member_name')
    return value


def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value),
        'invalid_registry_timestamp')
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise RegistryError('invalid_registry_timestamp') from None


def date_key(value):
    try:
        require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value),
                'invalid_registry_date')
        require(dt.date.fromisoformat(value).isoformat() == value, 'invalid_registry_date')
    except ValueError:
        raise RegistryError('invalid_registry_date') from None
    return value


def stamp(now):
    require(isinstance(now, dt.datetime) and now.utcoffset() is not None, 'timezone_required')
    return now.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def normalize_profile_url(value):
    """Profile-only normalization, not the collectors' post/accounting URL parser."""
    require(isinstance(value, str) and not re.search(r'[\s\\\x00-\x1f\x7f]', value),
            'invalid_profile_url')
    try:
        parsed = urllib.parse.urlsplit(value)
        require(parsed.scheme == 'https' and parsed.hostname in ('x.com', 'twitter.com')
                and parsed.netloc.lower() == parsed.hostname
                and not parsed.fragment and '#' not in value, 'invalid_profile_url')
        match = re.fullmatch(r'/([A-Za-z0-9_]{1,15})/?', parsed.path)
        require(match is not None, 'profile_url_required')
        handle = match[1].lower()
        require(handle not in RESERVED_ROUTES, 'profile_url_required')
        if '?' in value:
            query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
            require(bool(query) and set(query) <= {'s', 't'}
                    and all(len(items) == 1 for items in query.values()), 'invalid_profile_query')
            require('s' not in query or query['s'][0] in ('09', '19', '20', '21', '46'),
                    'invalid_profile_query')
            require('t' not in query or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', query['t'][0]),
                    'invalid_profile_query')
        return 'https://x.com/' + handle
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, RegistryError):
            raise
        raise RegistryError('invalid_profile_url') from None


def profile_handle(url):
    return normalize_profile_url(url).rsplit('/', 1)[1]


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def strict_json(text):
    def invalid_constant(_):
        raise RegistryError('invalid_json')
    try:
        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise RegistryError('invalid_json') from None


def read_bytes(path):
    path = Path(path)
    require(not path.is_symlink() and path.stat().st_size <= MAX_BYTES, 'unsafe_registry_input')
    raw = path.read_bytes()
    require(len(raw) <= MAX_BYTES, 'unsafe_registry_input')
    return raw


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def names_of(member):
    return {member['canonicalName'], member['displayName'], *member['aliases']}


def status_of(member):
    return {key: member[key] for key in STATUS_FIELDS}


def validate_status(status):
    fields(status, STATUS_FIELDS)
    choice(status['membership'], {'active', 'inactive', 'unconfirmed'}, 'invalid_membership')
    choice(status['collection'], {'enabled', 'paused', 'review'}, 'invalid_collection_status')
    require(status['membership'] == 'active' or status['collection'] != 'enabled',
            'nonactive_collection_enabled')
    if status['inactiveFrom'] is not None:
        date_key(status['inactiveFrom'])
        require(status['membership'] == 'inactive', 'inactive_date_requires_inactive')
    timestamp(status['statusRecordedAt'])


def validate_registry(value):
    fields(value, ROOT_FIELDS)
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1,
            'unsupported_registry_version')
    baseline = value['legacySnapshot']
    if baseline is not None:
        fields(baseline, {'revision', 'registeredAt'})
        require(isinstance(baseline['revision'], str)
                and re.fullmatch(r'[0-9a-f]{40}', baseline['revision']), 'invalid_legacy_revision')
        timestamp(baseline['registeredAt'])
    for key in ('members', 'unresolvedNames', 'reservedHandles'):
        require(isinstance(value[key], list), 'invalid_registry_list')
    ids, names, handles = {}, {}, {}
    for member in value['members']:
        fields(member, MEMBER_FIELDS)
        mid = member['memberId']
        require(isinstance(mid, str) and MEMBER_ID.fullmatch(mid) and mid not in ids,
                'duplicate_or_invalid_member_id')
        ids[mid] = member
        valid_name(member['canonicalName'])
        valid_name(member['displayName'])
        require(isinstance(member['aliases'], list), 'invalid_member_aliases')
        for alias in member['aliases']:
            valid_name(alias)
        require(len(member['aliases']) == len(set(member['aliases']))
                and member['canonicalName'] not in member['aliases'], 'invalid_member_aliases')
        for name in names_of(member):
            require(name not in names, 'member_name_collision')
            names[name] = mid
        choice(member['role'], {'normal', 'kitchen', 'trainee', 'unknown'}, 'invalid_member_role')
        choice(member['registrationSource'], {'legacy-roster', 'user-provided'},
               'invalid_registration_source')
        registered = timestamp(member['registeredAt'])
        validate_status(status_of(member))
        require(isinstance(member['statusHistory'], list), 'invalid_status_history')
        previous_time = registered
        for old in [*member['statusHistory'], status_of(member)]:
            validate_status(old)
            when = timestamp(old['statusRecordedAt'])
            require(when >= previous_time, 'status_history_time_reversed')
            previous_time = when
        url, trust = member['xProfileUrl'], member['accountTrust']
        if url is None:
            require(trust is None, 'trust_without_profile')
        else:
            require(normalize_profile_url(url) == url, 'noncanonical_profile_url')
            choice(trust, TRUSTS, 'untrusted_profile')
            handle = profile_handle(url)
            require(handle not in handles, 'member_handle_collision')
            handles[handle] = mid
        home = member['homeStore']
        if home is None:
            require(member['homeStoreSource'] is None, 'home_source_without_store')
        else:
            choice(home, {'s1', 's2', 's3', 's4'}, 'invalid_home_store')
            choice(member['homeStoreSource'], {'official', 'legacy-reviewed', 'user-provided'},
                   'invalid_home_source')
        choice(member['officialListing'], {'listed', 'unlisted', 'unknown'}, 'invalid_listing_status')
    for member in value['members']:
        anchor = member['orderBefore']
        if anchor is not None:
            require(isinstance(anchor, str) and anchor in ids and anchor != member['memberId']
                    and member['role'] == ids[anchor]['role'] == 'normal', 'invalid_order_anchor')
        seen, current = set(), member
        while current['orderBefore'] is not None:
            require(current['memberId'] not in seen, 'cyclic_member_order')
            seen.add(current['memberId'])
            next_id = current['orderBefore']
            require(isinstance(next_id, str) and next_id in ids, 'invalid_order_anchor')
            current = ids[next_id]
    unresolved_names, legacy_handles = set(), set()
    for row in value['unresolvedNames']:
        fields(row, {'name', 'legacyAccounts', 'resolvedMemberId'})
        name = valid_name(row['name'])
        require(name not in unresolved_names, 'duplicate_unresolved_name')
        unresolved_names.add(name)
        require(isinstance(row['legacyAccounts'], list), 'invalid_legacy_accounts')
        for account in row['legacyAccounts']:
            fields(account, {'profileUrl', 'status'})
            require(normalize_profile_url(account['profileUrl']) == account['profileUrl'],
                    'noncanonical_profile_url')
            choice(account['status'], {'legacy-verified', 'legacy-retired-account', 'unconfirmed'},
                   'invalid_legacy_account_status')
            handle = profile_handle(account['profileUrl'])
            require(handle not in handles and handle not in legacy_handles, 'legacy_handle_collision')
            legacy_handles.add(handle)
        resolved = row['resolvedMemberId']
        if resolved is None:
            require(name not in names, 'unresolved_name_requires_explicit_resolution')
        else:
            require(isinstance(resolved, str) and resolved in ids
                    and ids[resolved]['canonicalName'] == name and not row['legacyAccounts'],
                    'legacy_identity_review_required')
    reserved = set()
    for handle in value['reservedHandles']:
        require(isinstance(handle, str) and HANDLE.fullmatch(handle) and handle == handle.lower(),
                'invalid_reserved_handle')
        require(handle not in handles and handle not in reserved, 'reserved_handle_collision')
        reserved.add(handle)
    return value


def load_registry(path):
    return validate_registry(strict_json(read_bytes(path).decode('utf-8-sig')))


def empty_registry():
    return {'schemaVersion': 1, 'legacySnapshot': None, 'members': [],
            'unresolvedNames': [], 'reservedHandles': []}


def lookup(registry, selector):
    require(isinstance(selector, str), 'invalid_member_selector')
    matches = [m for m in registry['members']
               if selector == m['memberId'] or selector in names_of(m)]
    require(len(matches) <= 1, 'ambiguous_member')
    return copy.deepcopy(matches[0]) if matches else None


def known_identities(registry):
    validate_registry(registry)
    return {m['memberId']: copy.deepcopy(m) for m in registry['members']}


def new_member(name, url, now, *, role='normal', member_id=None, source='user-provided'):
    valid_name(name)
    at = stamp(now)
    return {
        'memberId': 'm-' + uuid.uuid4().hex if member_id is None else member_id, 'canonicalName': name,
        'displayName': name, 'aliases': [], 'role': role,
        'membership': 'active', 'collection': 'enabled', 'inactiveFrom': None,
        'registeredAt': at, 'registrationSource': source, 'statusRecordedAt': at,
        'statusHistory': [], 'xProfileUrl': normalize_profile_url(url) if url is not None else None,
        'accountTrust': 'user-provided' if url is not None else None,
        'homeStore': None, 'homeStoreSource': None, 'officialListing': 'unknown',
        'orderBefore': None,
    }


def validate_transition(before, after):
    validate_registry(before)
    validate_registry(after)
    require(before['legacySnapshot'] == after['legacySnapshot'], 'legacy_snapshot_immutable')
    require(set(before['reservedHandles']) <= set(after['reservedHandles']), 'reserved_handle_removed')
    new_by_id = known_identities(after)
    for previous in before['members']:
        current = new_by_id.get(previous['memberId'])
        require(current is not None, 'historical_identity_removed')
        for key in ('canonicalName', 'registeredAt', 'registrationSource', 'role'):
            require(previous[key] == current[key], 'immutable_member_field')
        require(names_of(previous) <= names_of(current), 'historical_name_removed')
        if previous['xProfileUrl'] is not None:
            require(previous['xProfileUrl'] == current['xProfileUrl']
                    and previous['accountTrust'] == current['accountTrust'],
                    'account_change_requires_review')
        old_history = [*previous['statusHistory'], status_of(previous)]
        new_history = [*current['statusHistory'], status_of(current)]
        require(new_history[:len(old_history)] == old_history, 'status_history_not_preserved')
    remaining = {row['name']: row for row in after['unresolvedNames']}
    for old in before['unresolvedNames']:
        row = remaining.get(old['name'])
        require(row is not None and row['legacyAccounts'] == old['legacyAccounts'],
                'legacy_identity_removed')
        require(old['resolvedMemberId'] is None or old['resolvedMemberId'] == row['resolvedMemberId'],
                'legacy_identity_reassigned')
    return after


def add_member(registry, name, url, now, *, role='normal'):
    validate_registry(registry)
    require(lookup(registry, name) is None, 'member_already_exists_use_set_account')
    result = copy.deepcopy(registry)
    member = new_member(name, url, now, role=role)
    for row in result['unresolvedNames']:
        if row['name'] == name:
            require(not row['legacyAccounts'], 'legacy_identity_review_required')
            row['resolvedMemberId'] = member['memberId']
    position = len(result['members'])
    if role == 'normal':
        position = next((index for index, existing in enumerate(result['members'])
                         if existing['role'] == 'kitchen'), position)
    result['members'].insert(position, member)
    return validate_transition(registry, result)


def update_member(registry, selector, *, now, url=None, display_name=None,
                  membership=None, collection=None, inactive_from=None, clear_inactive_from=False):
    validate_registry(registry)
    found = lookup(registry, selector)
    require(found is not None, 'unknown_member')
    result = copy.deepcopy(registry)
    member = next(m for m in result['members'] if m['memberId'] == found['memberId'])
    if url is not None:
        normalized = normalize_profile_url(url)
        require(member['xProfileUrl'] in (None, normalized), 'account_change_requires_review')
        member['xProfileUrl'] = normalized
        if member['accountTrust'] is None:
            member['accountTrust'] = 'user-provided'
    if display_name is not None:
        valid_name(display_name)
        old_display = member['displayName']
        if old_display != member['canonicalName'] and old_display not in member['aliases']:
            member['aliases'].append(old_display)
        member['displayName'] = display_name
    old_status = status_of(member)
    if membership is not None:
        member['membership'] = membership
        if membership != 'active' and collection is None:
            member['collection'] = 'paused'
    if collection is not None:
        member['collection'] = collection
    require(not (clear_inactive_from and inactive_from is not None), 'conflicting_inactive_date')
    if clear_inactive_from:
        member['inactiveFrom'] = None
    elif inactive_from is not None:
        member['inactiveFrom'] = date_key(inactive_from)
    if status_of(member) != old_status:
        member['statusHistory'].append(old_status)
        member['statusRecordedAt'] = stamp(now)
    return validate_transition(registry, result)


def binding_index(registry, binding_maps):
    """Compare both collectors without rebinding or requiring a first binding."""
    records, by_handle, by_author, conflicts = {}, {}, {}, set()
    for member in registry['members']:
        if member['xProfileUrl'] is not None:
            by_handle.setdefault(profile_handle(member['xProfileUrl']), set()).add(member['memberId'])
    for mapping in binding_maps:
        require(isinstance(mapping, dict), 'invalid_identity_bindings')
        for name, bound in mapping.items():
            valid_name(name)
            fields(bound, {'authorId', 'authorScreenName', 'verifiedAt'}, 'invalid_identity_binding')
            require(isinstance(bound['authorId'], str) and AUTHOR_ID.fullmatch(bound['authorId'])
                    and isinstance(bound['authorScreenName'], str)
                    and HANDLE.fullmatch(bound['authorScreenName']), 'invalid_identity_binding')
            timestamp(bound['verifiedAt'])
            member = lookup(registry, name)
            owner = member['memberId'] if member else 'unresolved:' + name
            identity = (bound['authorId'], bound['authorScreenName'].lower())
            if owner in records and records[owner] != identity:
                conflicts.add(owner)
            records[owner] = identity
            by_author.setdefault(identity[0], set()).add(owner)
            by_handle.setdefault(identity[1], set()).add(owner)
    for group in [*by_author.values(), *by_handle.values()]:
        if len(group) > 1:
            conflicts.update(group)
    return records, conflicts


def collection_population(registry, *binding_maps):
    validate_registry(registry)
    bindings, conflicts = binding_index(registry, binding_maps)
    targets, coverage = {}, {}
    for member in registry['members']:
        mid = member['memberId']
        handle = profile_handle(member['xProfileUrl']) if member['xProfileUrl'] else None
        bound = bindings.get(mid)
        identity = ('account_identity_mismatch' if mid in conflicts or
                    bound and handle is not None and bound[1] != handle else
                    'author_verified' if bound else
                    'trusted_handle_unbound' if handle and binding_maps else
                    'binding_not_loaded' if handle else 'account_unknown')
        if member['membership'] != 'active':
            reason = 'membership_' + member['membership']
        elif member['collection'] != 'enabled':
            reason = 'collection_' + member['collection']
        elif identity == 'account_identity_mismatch':
            reason = identity
        elif handle is None:
            reason = 'account_unknown'
        else:
            reason = 'eligible_not_collected'
            targets[mid] = {'memberId': mid, 'name': member['canonicalName'], 'handle': handle}
        coverage[mid] = {
            'name': member['canonicalName'], 'membership': member['membership'],
            'collection': member['collection'], 'handle': handle,
            'identity': identity, 'reason': reason,
        }
    return targets, coverage


class RegistryGuard:
    """Recheck local authority immediately before I/O, including after any wait."""

    def __init__(self, path, *, bindings=()):
        self.path = Path(path)
        self.bindings = bindings
        self.registry, self.revision = self._read()
        self._seen_bindings, _ = binding_index(self.registry, self._binding_maps())

    def _read(self):
        try:
            raw = read_bytes(self.path)
            return (validate_registry(strict_json(raw.decode('utf-8-sig'))),
                    hashlib.sha256(raw).hexdigest())
        except (OSError, UnicodeError):
            raise RegistryError('registry_unavailable') from None

    def _binding_maps(self):
        maps = self.bindings() if callable(self.bindings) else self.bindings
        require(isinstance(maps, (list, tuple)), 'invalid_identity_bindings')
        return maps

    def check(self, name=None):
        current, revision = self._read()
        require(revision == self.revision, 'registry_changed')
        maps = self._binding_maps()
        records, _ = binding_index(current, maps)
        require(all(records.get(owner) == identity for owner, identity in self._seen_bindings.items()),
                'account_identity_mismatch')
        self._seen_bindings.update(records)
        if name is None:
            return current
        member = lookup(current, name)
        require(member is not None, 'unknown_member')
        targets, coverage = collection_population(current, *maps)
        mid = member['memberId']
        require(mid in targets, coverage[mid]['reason'])
        return targets[mid]


def public_projection(registry):
    validate_registry(registry)
    return {'schemaVersion': 1, 'members': [
        {key: copy.deepcopy(member[key]) for key in PUBLIC_MEMBER_FIELDS}
        for member in registry['members']],
        'unresolvedNames': [row['name'] for row in registry['unresolvedNames']
                            if row['resolvedMemberId'] is None]}


def javascript_bytes(registry):
    payload = json_bytes(public_projection(registry)).decode('utf-8').rstrip()
    return ('// Generated by tools/member-registry.py; do not edit.\n'
            'window.MEMBER_REGISTRY = ' + payload + ';\n').encode('utf-8')


def display_projection(registry):
    validate_registry(registry)
    active = [m for m in registry['members'] if m['membership'] == 'active']
    return {
        'roster': [m['canonicalName'] for m in active],
        'knownNames': [m['canonicalName'] for m in registry['members']],
        'kitchenStaff': [m['canonicalName'] for m in registry['members'] if m['role'] == 'kitchen'],
        'displayNames': {m['canonicalName']: m['displayName'] for m in registry['members']},
        'aliases': {alias: m['canonicalName'] for m in registry['members'] for alias in names_of(m)},
        'homeStore': {m['canonicalName']: m['homeStore'] for m in registry['members'] if m['homeStore']},
        'normalOrderBefore': {
            m['canonicalName']: lookup(registry, m['orderBefore'])['canonicalName']
            for m in registry['members'] if m['orderBefore']},
    }


def plan_policy(member, day):
    date_key(day)
    if member['membership'] != 'inactive':
        return 'retained'
    if member['inactiveFrom'] is None:
        return 'retained_requires_review'
    return 'excluded_from_plan_view' if day >= member['inactiveFrom'] else 'retained'


def plan_impact(registry, selector, plans, today):
    """Plans are name/date/shift references only, never actual attendance or raw posts."""
    date_key(today)
    member = lookup(registry, selector)
    require(member is not None, 'unknown_member')
    if plans is None:
        return {'evaluated': False, 'reason': 'plan_inputs_not_supplied', 'factsDeleted': 0}
    require(isinstance(plans, list), 'invalid_plan_references')
    results, seen = [], set()
    for row in plans:
        fields(row, {'name', 'date', 'shift'}, 'invalid_plan_reference')
        valid_name(row['name'])
        date_key(row['date'])
        choice(row['shift'], {'昼', '夜'}, 'invalid_plan_shift')
        key = (row['date'], row['shift'])
        if row['name'] in names_of(member) and row['date'] >= today and key not in seen:
            seen.add(key)
            results.append({'date': row['date'], 'shift': row['shift'],
                            'policy': plan_policy(member, row['date'])})
    return {'evaluated': True, 'factsDeleted': 0,
            'futurePlans': sorted(results, key=lambda row: (row['date'], row['shift'])),
            'retainedCount': sum(row['policy'] != 'excluded_from_plan_view' for row in results),
            'reviewCount': sum(row['policy'] == 'retained_requires_review' for row in results),
            'excludedFromPlanViewCount': sum(row['policy'] == 'excluded_from_plan_view' for row in results)}


def registry_report(registry, *binding_maps):
    targets, coverage = collection_population(registry, *binding_maps)
    active = [m for m in registry['members'] if m['membership'] == 'active']
    return {
        'activeCount': len(active),
        'activeKitchenCount': sum(m['role'] == 'kitchen' for m in active),
        'activeFloorCount': sum(m['role'] in ('normal', 'trainee') for m in active),
        'activeUnknownRoleCount': sum(m['role'] == 'unknown' for m in active),
        'knownIdentityCount': len(registry['members']),
        'unresolvedNameCount': sum(row['resolvedMemberId'] is None for row in registry['unresolvedNames']),
        'eligibleCount': len(targets), 'collectionEvaluated': False, 'coverage': coverage,
    }


def legacy_fields(path):
    """Read only the saved JSON-valued metadata blocks, never execute schedule JS."""
    source = read_bytes(path).decode('utf-8-sig')
    decoder = json.JSONDecoder(object_pairs_hook=unique_pairs)
    result = {}
    for key in ('roster', 'kitchenStaff', 'displayNames', 'normalOrderBefore',
                'homeStore', 'unpostedMaids'):
        matches = list(re.finditer(r'^\s*' + key + r':\s*', source, re.M))
        require(len(matches) == 1, 'unsupported_legacy_metadata')
        try:
            result[key] = decoder.raw_decode(source[matches[0].end():])[0]
        except (json.JSONDecodeError, RecursionError):
            raise RegistryError('unsupported_legacy_metadata') from None
    return result


def read_legacy_insights(path):
    source = read_bytes(path).decode('utf-8-sig')
    match = re.search(r'^window\.STORE_INSIGHTS\s*=\s*', source, re.M)
    require(match is not None, 'unsupported_legacy_insights')
    result = strict_json(source[match.end():].strip().removesuffix(';'))
    require(isinstance(result, dict) and isinstance(result.get('maidTendency'), dict),
            'unsupported_legacy_insights')
    return result


def migrate_legacy(schedule, accounts, insights, now, revision):
    fields(schedule, {'roster', 'kitchenStaff', 'displayNames', 'normalOrderBefore',
                      'homeStore', 'unpostedMaids'}, 'unsupported_legacy_metadata')
    require(isinstance(accounts, list) and isinstance(insights, dict)
            and isinstance(insights.get('maidTendency', {}), dict), 'invalid_legacy_inputs')
    roster = schedule['roster']
    require(isinstance(roster, list) and roster and all(isinstance(n, str) for n in roster)
            and len(roster) == len(set(roster)), 'invalid_legacy_roster')
    for key in ('kitchenStaff', 'unpostedMaids'):
        require(isinstance(schedule[key], list) and all(isinstance(n, str) for n in schedule[key])
                and len(schedule[key]) == len(set(schedule[key]))
                and set(schedule[key]) <= set(roster), 'invalid_legacy_roster')
    for key in ('displayNames', 'normalOrderBefore', 'homeStore'):
        require(isinstance(schedule[key], dict) and set(schedule[key]) <= set(roster),
                'invalid_legacy_roster')
    rows = {}
    for row in accounts:
        require(isinstance(row, dict), 'invalid_legacy_account')
        name = valid_name(row.get('name'))
        require(name not in rows, 'duplicate_legacy_account')
        rows[name] = row
    result = empty_registry()
    result['legacySnapshot'] = {'revision': revision, 'registeredAt': stamp(now)}
    by_name = {}
    for name in roster:
        row = rows.get(name, {})
        handle = row.get('handle')
        require(isinstance(handle, str) and HANDLE.fullmatch(handle)
                and row.get('source') in ('公式サイト', '本人確認済み'), 'legacy_account_requires_review')
        mid = 'm-' + uuid.uuid5(uuid.NAMESPACE_URL, 'akibazettai:legacy-roster:' + name).hex
        member = new_member(name, 'https://x.com/' + handle, now, member_id=mid,
                            role='kitchen' if name in schedule['kitchenStaff'] else 'normal',
                            source='legacy-roster')
        member['accountTrust'] = 'official' if row['source'] == '公式サイト' else 'legacy-verified'
        member['displayName'] = schedule['displayNames'].get(name, name)
        tendency = insights.get('maidTendency', {}).get(name)
        require(tendency is None or isinstance(tendency, dict), 'invalid_legacy_tendency')
        if tendency and tendency.get('x'):
            require(profile_handle('https://x.com/' + tendency['x']) == handle.lower(),
                    'legacy_account_identity_mismatch')
        alias = tendency.get('alias') if tendency else None
        if alias and alias != name:
            member['aliases'] = [valid_name(alias)]
        if member['displayName'] != name and member['displayName'] not in member['aliases']:
            member['aliases'].append(member['displayName'])
        member['homeStore'] = schedule['homeStore'].get(name)
        member['officialListing'] = 'unlisted' if name in schedule['unpostedMaids'] else 'listed'
        if member['homeStore']:
            member['homeStoreSource'] = 'legacy-reviewed' if member['officialListing'] == 'unlisted' else 'official'
        result['members'].append(member)
        by_name[name] = member
    for name, before in schedule['normalOrderBefore'].items():
        require(isinstance(before, str) and before in by_name, 'invalid_legacy_order')
        by_name[name]['orderBefore'] = by_name[before]['memberId']
    for name, row in rows.items():
        if name not in by_name:
            accounts_for_name = []
            if row.get('handle'):
                accounts_for_name.append({
                    'profileUrl': normalize_profile_url('https://x.com/' + row['handle']),
                    'status': ('legacy-retired-account' if row.get('source') == '卒業済み'
                               else 'legacy-verified' if row.get('source') in ('公式サイト', '本人確認済み')
                               else 'unconfirmed')})
            result['unresolvedNames'].append({
                'name': name, 'legacyAccounts': accounts_for_name, 'resolvedMemberId': None})
        # Explicit handles in old notes are exclusions for review, never discovered accounts.
        note = row.get('note') or ''
        require(isinstance(note, str), 'invalid_legacy_note')
        for handle in re.findall(r'@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])', note):
            if handle.lower() not in result['reservedHandles']:
                result['reservedHandles'].append(handle.lower())
    result['reservedHandles'].sort()
    return validate_registry(result)


def atomic_write(path, raw):
    require(not path.is_symlink(), 'unsafe_registry_output')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.members-', suffix='.tmp',
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_registry(path, registry, expected_hash):
    """Serialize writers; interruption between outputs is detected by check/stage."""
    path = Path(path).absolute()
    require(path.name == 'members.json' and path.parent.is_dir() and not path.is_symlink(),
            'unsafe_registry_output')
    output = path.with_suffix('.js')
    require(not output.is_symlink(), 'unsafe_registry_output')
    validate_registry(registry)
    source, script = json_bytes(registry), javascript_bytes(registry)
    require(len(source) <= MAX_BYTES and len(script) <= MAX_BYTES, 'registry_too_large')
    lock = path.with_suffix('.lock')
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RegistryError('registry_locked') from None
    try:
        os.close(descriptor)
        existing = read_bytes(path) if path.exists() else None
        actual_hash = hashlib.sha256(existing).hexdigest() if existing is not None else None
        require(actual_hash == expected_hash, 'registry_changed')
        if existing is not None:
            validate_transition(strict_json(existing.decode('utf-8-sig')), registry)
        else:
            require(not output.exists(), 'orphan_projection_requires_review')
        if existing != source:
            atomic_write(path, source)
        try:
            if not output.exists() or output.read_bytes() != script:
                atomic_write(output, script)
        except (OSError, RegistryError):
            raise RegistryError('registry_saved_projection_incomplete') from None
    finally:
        lock.unlink()


def check_projection(path, registry):
    # Git may check text out as CRLF on Windows; generation itself always uses LF.
    output = Path(path).with_suffix('.js')
    require(output.is_file() and not output.is_symlink(), 'generated_projection_missing_or_unsafe')
    require(output.read_bytes().replace(b'\r\n', b'\n') == javascript_bytes(registry),
            'generated_projection_mismatch')


def registry_project_root(path):
    path = Path(path).resolve()
    root = path.parent.parent
    return root if path.parent.name == 'data' and (root / 'index.html').is_file() else None


def stamp_registry_assets(path):
    root = registry_project_root(path)
    if root is None:
        return False
    spec = importlib.util.spec_from_file_location(
        'member_registry_asset_stamps', Path(__file__).with_name('build-insights.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.stamp_assets(root=root)


def local_plan_references(path):
    root = registry_project_root(path)
    if root is None:
        return None
    spec = importlib.util.spec_from_file_location(
        'member_registry_schedule_reader', Path(__file__).with_name('collect-personal-shifts.py'))
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    schedule = loader.read_js(root / 'data' / 'schedule.js', 'SCHEDULE_DATA')
    require(isinstance(schedule, dict) and isinstance(schedule.get('schedule'), dict),
            'invalid_local_schedule')
    refs = []
    for day, shifts in schedule['schedule'].items():
        date_key(day)
        require(isinstance(shifts, dict), 'invalid_local_schedule')
        for shift, people in shifts.items():
            choice(shift, {'昼', '夜'}, 'invalid_plan_shift')
            require(isinstance(people, list), 'invalid_local_schedule')
            for person in people:
                require(isinstance(person, dict), 'invalid_local_schedule')
                refs.append({'name': valid_name(person.get('name')), 'date': day, 'shift': shift})
    spec = importlib.util.spec_from_file_location(
        'member_registry_half_reader', Path(__file__).with_name('half-month-schedules.py'))
    half = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(half)
    feed = half.public_state(strict_json(read_bytes(root / 'data' / 'half-month-schedules.json').decode('utf-8-sig')))
    for item in feed['schedules']:
        for day in item['days']:
            refs.extend({'name': item['name'], 'date': day['date'], 'shift': shift} for shift in day['shifts'])
    return refs


def argument_parser():
    parser = argparse.ArgumentParser(description='Offline member registry; no collection or inference')
    parser.add_argument('--registry', type=Path, default=ROOT / 'data' / 'members.json')
    parser.add_argument('--bindings', type=Path, action='append', default=[],
                        help='optional identityBindings mapping or saved collector JSON; read only')
    parser.add_argument('--baseline', type=Path, help='previous registry for append-only history validation')
    sub = parser.add_subparsers(dest='command', required=True)
    migrate = sub.add_parser('migrate', help='one-time import of saved roster metadata only')
    migrate.add_argument('--schedule', type=Path, default=ROOT / 'data' / 'schedule.js')
    migrate.add_argument('--accounts', type=Path, default=ROOT / 'tools' / 'data' / 'accounts.csv')
    migrate.add_argument('--insights', type=Path, default=ROOT / 'data' / 'store-insights.js')
    migrate.add_argument('--revision', required=True)
    add = sub.add_parser('add-normal', help='register a normal; unknown account is allowed')
    add.add_argument('--name', required=True)
    add.add_argument('--x')
    account = sub.add_parser('set-account', help='fill a missing profile; never replace a known identity')
    account.add_argument('--member', required=True)
    account.add_argument('--x', required=True)
    display = sub.add_parser('set-display-name')
    display.add_argument('--member', required=True)
    display.add_argument('--name', required=True)
    status = sub.add_parser('set-status')
    status.add_argument('--member', required=True)
    status.add_argument('--membership', choices=('active', 'inactive', 'unconfirmed'))
    status.add_argument('--collection', choices=('enabled', 'paused', 'review'))
    dates = status.add_mutually_exclusive_group()
    dates.add_argument('--inactive-from')
    dates.add_argument('--clear-inactive-from', action='store_true')
    status.add_argument('--plans', type=Path, help='JSON array of name/date/shift plan references; read only')
    for command in ('check', 'export', 'report'):
        sub.add_parser(command)
    return parser


def main(argv=None, *, clock=lambda: dt.datetime.now(UTC)):
    args = argument_parser().parse_args(argv)
    try:
        if args.command == 'migrate':
            require(not args.registry.exists(), 'registry_already_exists')
            with args.accounts.open(encoding='utf-8-sig', newline='') as stream:
                accounts = list(csv.DictReader(stream))
            registry = migrate_legacy(legacy_fields(args.schedule), accounts,
                                      read_legacy_insights(args.insights), clock(), args.revision)
            original_hash = None
        else:
            original = read_bytes(args.registry)
            original_hash = hashlib.sha256(original).hexdigest()
            registry = validate_registry(strict_json(original.decode('utf-8-sig')))
        binding_maps = []
        for path in args.bindings:
            value = strict_json(read_bytes(path).decode('utf-8-sig'))
            require(isinstance(value, dict), 'invalid_identity_bindings')
            binding_maps.append(value.get('identityBindings', value))
        affected = None
        if args.command == 'add-normal':
            registry = add_member(registry, args.name, args.x, clock())
            affected = args.name
        elif args.command == 'set-account':
            registry = update_member(registry, args.member, now=clock(), url=args.x)
            affected = args.member
        elif args.command == 'set-display-name':
            registry = update_member(registry, args.member, now=clock(), display_name=args.name)
            affected = args.member
        elif args.command == 'set-status':
            require(args.membership is not None or args.collection is not None
                    or args.inactive_from is not None or args.clear_inactive_from,
                    'status_change_required')
            registry = update_member(registry, args.member, now=clock(), membership=args.membership,
                                     collection=args.collection, inactive_from=args.inactive_from,
                                     clear_inactive_from=args.clear_inactive_from)
            affected = args.member
        if args.baseline:
            validate_transition(load_registry(args.baseline), registry)
        report = registry_report(registry, *binding_maps)
        if affected and args.command in ('add-normal', 'set-account'):
            member = lookup(registry, affected)
            require(report['coverage'][member['memberId']]['identity'] != 'account_identity_mismatch',
                    'account_identity_mismatch')
        impact = None
        if args.command == 'set-status':
            plans = (strict_json(read_bytes(args.plans).decode('utf-8-sig')) if args.plans
                     else local_plan_references(args.registry))
            impact = plan_impact(registry, args.member, plans, clock().astimezone(JST).date().isoformat())
        if args.command == 'check':
            check_projection(args.registry, registry)
        elif args.command != 'report':
            save_registry(args.registry, registry, original_hash)
            try:
                stamp_registry_assets(args.registry)
            except (OSError, UnicodeError):
                raise RegistryError('registry_saved_asset_stamps_incomplete') from None
        result = {'command': args.command, 'published': False, 'scope': 'offline_registry',
                  'baselineChecked': args.baseline is not None,
                  'sourceRequests': 0, 'analysisRequests': 0, **report}
        if impact is not None:
            result['planImpact'] = impact
            result['planScope'] = 'explicit_references' if args.plans else 'local_saved_inputs'
            result['message'] = (
                '既存の予定・実績・出典は削除していません。退在籍日不明では将来予定は照合要です。'
                '確認済みのinactiveFromだけがplan-only表示の打切りに使われます。'
                + ('' if impact['evaluated'] else '将来予定の件数は入力未指定のため未確認です。'))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (RegistryError, OSError, UnicodeError) as exc:
        reason = str(exc) if isinstance(exc, RegistryError) else 'registry_io_failed'
        error = {'error': reason, 'published': False}
        if reason == 'registry_saved_projection_incomplete':
            error.update(registrySaved=True, projectionComplete=False, recoveryCommands=['check', 'export'],
                         message='members.jsonは保存済みですが、members.jsの生成が完了していません。'
                                 '追加操作は繰り返さず、同じregistryにcheckとexportを実行してください。')
        elif reason == 'registry_saved_asset_stamps_incomplete':
            error.update(registrySaved=True, recoveryCommands=['export'],
                         message='名簿は保存済みですが、index.htmlの刻印更新が完了していません。'
                                 '同じregistryにexportを実行してください。')
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
