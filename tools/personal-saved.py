"""Bounded, offline attestations for already-accounted personal analyses.

An entry is {expectedSubjectHash, amendment}; the amendment contains schemaVersion,
id, source, events and optional links. source carries confirmed post metadata,
provenance (search/direct), optional searchCreatedAt, fetchedAt/analyzedAt,
bodyHash/sourceHash/sourceManifestHash/analysisResultHash/analysisReceiptHash/
contractHash, contractVersion, model/deployment/modelVersion, and the existing
aggregate usageReceiptId/usageSourceHash. sourceHash hashes the saved source bytes;
the other hashes identify the caller-reviewed source manifest, accepted analysis
artifact, individual analysis record, and contract. They are not usage receipts.

Like saved official notices, this is an explicit reviewed attestation boundary:
the caller verifies original bytes and accepted results offline. This module
cannot reconstruct or revalidate a body from its hash, and never fetches sources,
executes analysis, copies raw/evidence/cache, or reserves usage.

Before projecting unselected comparison channels to null, reviewed candidate
builders must call validate_selection_core(post, raw, text, shifts, personal)
with the original response/body and collector module. This pure v8 check returns
the grounded channels only after every nonempty raw event/link has the same date,
scope and semantic core as the existing post (including effective legacy links).
The caller must separately bind source/request/response/core hashes and approve
the exact timing subset; null projection is not proof that raw core is compatible.
apply_amendments cannot reconstruct those originals from opaque attestation hashes.

savedPersonalImports is a private map keyed by SHA256 of the amendment's canonical
JSON. validate_imports(value, personal) raises ValueError on invalid data. personal
may be the collector module or its azure_context() (official and valid_post).
subject_hash(state, id) identifies all canonical subject facts and their binding.
apply_amendments(state, entries, usage, personal) returns a new state; usage is the
pre-existing canonical ledger, not a ledger with this run's imports added.

v8 work-timing-only amendments additionally require previous
{contractVersion, contractHash, importId, analysisReceiptId}, expectedCoreHash,
expectedTimingHash, targetScopes [{serviceDate, name, shift, boundary}] and
updateChannels ["workTiming"]. analysisReceiptId identifies the predecessor's
analysisReceiptHash. events/links are required nullable comparison inputs; they
never replace the original core. workTiming is a nonempty source-bound versioned
channel. Null/empty updates must settle usage separately, not enter this path.
Multiple distinct exclusions use the same single logical targetScopes entry;
their distinct fact keys retain the set value and other excluded targets.
CONTRACT_HASH from personal-azure.py is the required new contractHash.
Immutable savedPersonalImports entries form the revision history and current
revision is the unique chain leaf, never whichever analyzedAt sorts last.
Native or legacy subjects lacking a prior saved import are deliberately refused.
Normal v8 imports can create a known post for the first time, but cannot replace
an existing post; that requires the authorized work-timing-only revision path.

Explicit manual-saved-post imports may admit a previously unknown past post only
with an empty-subject CAS, settled usage, search attestation, and registry/binding
and dated-work validation. Ordinary imports retain their known-subject requirement.
"""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import re


SPEC = importlib.util.spec_from_file_location('saved_personal_timing', Path(__file__).with_name('work-timing.py'))
timing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(timing)
AZURE_SPEC = importlib.util.spec_from_file_location(
    'saved_personal_contract', Path(__file__).with_name('personal-azure.py'))
azure_contract = importlib.util.module_from_spec(AZURE_SPEC)
AZURE_SPEC.loader.exec_module(azure_contract)

HEX = re.compile(r'[0-9a-f]{64}\Z')
METADATA = ('url', 'name', 'authorId', 'authorScreenName', 'date', 'createdAt')
HASHES = ('bodyHash', 'sourceHash', 'sourceManifestHash', 'analysisResultHash',
          'analysisReceiptHash', 'contractHash', 'usageReceiptId', 'usageSourceHash')
SOURCE_FIELDS = (*METADATA, *HASHES, 'fetchedAt', 'analyzedAt', 'provenance',
                 'contractVersion', 'model', 'deployment', 'modelVersion')
MAX_IMPORTS = 1000
MODEL = 'gpt-5.6-luna'
MODEL_VERSION = '2026-07-09'
LEGACY_CONTRACT = 'personal-line-ids-v4'
PREVIOUS_LINK_CONTRACT = 'personal-line-ids-v5'
NULLABLE_LINK_CONTRACT = 'personal-line-ids-v6'
LINK_CONTRACT = 'personal-line-ids-v7'
TIMING_CONTRACT = 'personal-line-ids-v8'
TIMING_CONTRACT_HASH = azure_contract.CONTRACT_HASH
PREVIOUS_TIMING_CONTRACT_HASH = 'ec45c49aa773f90cd1d38a049af4904470692ecd824f5e0e20d3a685ad8e92c6'
TIMING_OPERATION = 'work-timing-only'
MANUAL_IMPORT_OPERATION = 'manual-saved-post'
TIMING_UPDATE_FIELDS = ('operation', 'previous', 'expectedCoreHash', 'expectedTimingHash',
                        'targetScopes', 'updateChannels')


def _require(condition, reason='invalid_saved_personal_import'):
    if not condition:
        raise ValueError(reason)


def _keys(value, required, optional=()):
    _require(isinstance(value, dict) and set(required) <= set(value)
             and set(value) <= set(required) | set(optional))


def data_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _hash(value):
    _require(isinstance(value, str) and HEX.fullmatch(value))


def post_from_amendment(amendment):
    source = amendment['source']
    post = {'id': amendment['id'], **{field: source[field] for field in METADATA},
            'observedAt': source['fetchedAt'], 'events': copy.deepcopy(amendment['events'] or [])}
    if amendment.get('links') is not None:
        post['links'] = copy.deepcopy(amendment['links'])
    if amendment.get('workTiming') is not None:
        post['workTiming'] = copy.deepcopy(amendment['workTiming'])
    return post


def _validate_entry(entry, personal):
    _keys(entry, ('expectedSubjectHash', 'amendment'))
    _hash(entry['expectedSubjectHash'])
    amendment = entry['amendment']
    is_timing = amendment.get('operation') == TIMING_OPERATION if isinstance(amendment, dict) else False
    is_manual = amendment.get('operation') == MANUAL_IMPORT_OPERATION if isinstance(amendment, dict) else False
    _keys(amendment, ('schemaVersion', 'id', 'source', 'events'),
          ('links', 'workTiming', *TIMING_UPDATE_FIELDS) if is_timing
          else ('links', 'workTiming', 'operation') if is_manual else ('links', 'workTiming'))
    _require(type(amendment['schemaVersion']) is int and amendment['schemaVersion'] == 1)
    source = amendment['source']
    _keys(source, SOURCE_FIELDS, ('searchCreatedAt',))
    for field in HASHES:
        _hash(source[field])
    _require(source['model'] == source['deployment'] == MODEL
             and source['modelVersion'] == MODEL_VERSION)
    _require(source['contractVersion'] in (
        LEGACY_CONTRACT, PREVIOUS_LINK_CONTRACT, NULLABLE_LINK_CONTRACT, LINK_CONTRACT, TIMING_CONTRACT))
    if source['contractVersion'] == TIMING_CONTRACT:
        _require('links' in amendment and 'workTiming' in amendment)
        _require(source['contractHash'] in (TIMING_CONTRACT_HASH, PREVIOUS_TIMING_CONTRACT_HASH),
                 'saved_personal_contract_mismatch')
    else:
        _require('workTiming' not in amendment and not is_timing)
        _require('links' not in amendment or isinstance(amendment['links'], list))
    _require(source['provenance'] in ('search', 'direct')
             and ('searchCreatedAt' in source) == (source['provenance'] == 'search'))
    official = personal.official
    created = official.timestamp(source['createdAt'])
    fetched = official.timestamp(source['fetchedAt'])
    analyzed = official.timestamp(source['analyzedAt'])
    _require(created <= fetched <= analyzed)
    if is_manual:
        _require(source['contractVersion'] == TIMING_CONTRACT
                 and source['provenance'] == 'search'
                 and source['date'] < analyzed.astimezone(official.JST).date().isoformat(),
                 'invalid_manual_saved_personal_scope')
    if source['provenance'] == 'search':
        searched = official.timestamp(source['searchCreatedAt'])
        _require(abs((searched - created).total_seconds()) < 2
                 and searched.astimezone(official.JST).date().isoformat() == source['date'])
    _require(is_timing and amendment['events'] is None
             or isinstance(amendment['events'], list) and len(amendment['events']) <= 8)
    if is_timing:
        _keys(amendment, ('schemaVersion', 'id', 'source', 'events', 'links', 'workTiming',
                          *TIMING_UPDATE_FIELDS))
        _require(amendment['updateChannels'] == ['workTiming'])
        _keys(amendment['previous'], ('contractVersion', 'contractHash', 'importId', 'analysisReceiptId'))
        _require(amendment['previous']['contractVersion'] in (
            LEGACY_CONTRACT, PREVIOUS_LINK_CONTRACT, NULLABLE_LINK_CONTRACT, LINK_CONTRACT, TIMING_CONTRACT))
        for field in ('contractHash', 'importId', 'analysisReceiptId'):
            _hash(amendment['previous'][field])
        for field in ('expectedCoreHash', 'expectedTimingHash'):
            _hash(amendment[field])
        _require(amendment['workTiming'] is not None
                 and isinstance(amendment['workTiming'], dict)
                 and bool(amendment['workTiming'].get('facts')), 'saved_personal_empty_timing')
        _target_scopes(amendment)
    if source['contractVersion'] == LEGACY_CONTRACT:
        # Saved v4 acceptances do not attest new link-only/unknown-scope meaning.
        _require(amendment['events'])
    post = post_from_amendment(amendment)
    personal.valid_post(post)
    if 'workTiming' in post:
        timing.validate(post['workTiming'], owner=post, date=post['date'], source_kind='personal-work-post')
    _require(source['authorScreenName'].casefold() != official.AUTHOR.casefold())
    seen = set()
    for event in post['events']:
        _require(not re.search(r'[\x00-\x1f\x7f]|https?://|www\.', event['excerpt'], re.I))
        identity = data_hash(event)
        _require(identity not in seen)
        seen.add(identity)
    if 'links' in post and source['contractVersion'] == LEGACY_CONTRACT:
        _require(isinstance(post['links'], list) and len(post['links']) <= 3)
        seen = set()
        for link in post['links']:
            _keys(link, ('scope', 'status'))
            _require(link['scope'] in ('昼', '夜') and link['scope'] not in seen)
            seen.add(link['scope'])
            kinds = {event['kind'] for event in post['events'] if event['shift'] == link['scope']}
            statuses = {
                'withdrawn' if kind == 'absence' else
                'conflict' if kind == 'uncertain' else 'work' for kind in kinds
            }
            _require(statuses == {link['status']} and link['status'] in ('work', 'withdrawn'))
    return post


def validate_imports(value, personal):
    _require(isinstance(value, dict) and len(value) <= MAX_IMPORTS)
    ids, analysis, children = set(), set(), {}
    for receipt_id, entry in value.items():
        _hash(receipt_id)
        _validate_entry(entry, personal)
        amendment = entry['amendment']
        _require(receipt_id == data_hash(amendment))
        identity = amendment['id']
        receipt = amendment['source']['analysisReceiptHash']
        _require(receipt not in analysis)
        if amendment.get('operation') == TIMING_OPERATION:
            parent_id = amendment['previous']['importId']
            _require(parent_id in value and parent_id not in children and parent_id != receipt_id,
                     'saved_personal_predecessor_mismatch')
            children[parent_id] = receipt_id
        else:
            _require(identity not in ids)
            ids.add(identity)
        analysis.add(receipt)
    current, visited = {}, set()
    for receipt_id, entry in value.items():
        if entry['amendment'].get('operation') == TIMING_OPERATION:
            continue
        post = post_from_amendment(entry['amendment'])
        while True:
            _require(receipt_id not in visited, 'saved_personal_predecessor_mismatch')
            visited.add(receipt_id)
            current[post['id']] = (receipt_id, post)
            child = children.get(receipt_id)
            if child is None:
                break
            amendment = value[child]['amendment']
            post = _timing_revision(post, amendment, value[receipt_id], receipt_id, personal)
            receipt_id = child
    _require(visited == set(value), 'saved_personal_predecessor_mismatch')
    return current


def core_hash(post):
    return data_hash({field: value for field, value in post.items() if field != 'workTiming'})


def timing_hash(post):
    return data_hash(post.get('workTiming'))


def validate_selection_core(post, raw, text, shifts, personal):
    """Validate original v8 core before a reviewed timing-only projection; no adoption."""
    personal.valid_post(post)
    normalized = azure_contract.grounded_assessment_v8(
        raw, text, dt.date.fromisoformat(post['date']), shifts, personal)
    reason = 'saved_personal_selection_core_mismatch'
    for field, scope_key in (('events', 'shift'), ('links', 'scope')):
        for claim in raw[field] or []:
            _require(claim['serviceDate'] == post['date']
                     and (claim[scope_key] in shifts
                          or field == 'links' and claim[scope_key] == 'unspecified'), reason)
    fields = ('shift', 'kind', 'storeId', 'time')
    existing_events = {tuple(event.get(key) for key in fields) for event in post['events']}
    _require(all(tuple(event.get(key) for key in fields) in existing_events
                 for event in normalized[0]), reason)
    existing_links = post.get('links', personal.legacy_links(post))
    _require(all(link in existing_links for link in normalized[1]), reason)
    return normalized


def _target_scopes(amendment):
    scopes = amendment['targetScopes']
    _require(isinstance(scopes, list) and 1 <= len(scopes) <= timing.MAX_FACTS)
    seen = set()
    for scope in scopes:
        _keys(scope, ('serviceDate', 'name', 'shift', 'boundary'))
        _require(scope['name'] == amendment['source']['name']
                 and scope['serviceDate'] == amendment['source']['date']
                 and scope['shift'] in ('昼', '夜') and scope['boundary'] in ('start', 'end'))
        key = timing.scope(scope)
        _require(key not in seen)
        seen.add(key)
    return seen


def _timing_revision(post, amendment, predecessor, predecessor_id, personal):
    previous, prior = amendment['previous'], predecessor['amendment']
    old_source, source = prior['source'], amendment['source']
    _require(previous == {
        'contractVersion': old_source['contractVersion'], 'contractHash': old_source['contractHash'],
        'importId': predecessor_id, 'analysisReceiptId': old_source['analysisReceiptHash'],
    } and prior['id'] == amendment['id'] == post['id'], 'saved_personal_predecessor_mismatch')
    _require(all(source[field] == old_source[field] for field in (*METADATA, 'bodyHash', 'sourceHash',
                                                                  'fetchedAt')),
             'saved_personal_source_mismatch')
    _require(personal.official.timestamp(source['analyzedAt'])
             >= personal.official.timestamp(old_source['analyzedAt']), 'saved_personal_predecessor_mismatch')
    _require(amendment['expectedCoreHash'] == core_hash(post), 'saved_personal_core_mismatch')
    _require(amendment['expectedTimingHash'] == timing_hash(post), 'saved_personal_timing_mismatch')
    for field, scope_key in (('events', 'shift'), ('links', 'scope')):
        supplied = amendment[field]
        if supplied:
            scopes = {item[scope_key] for item in supplied}
            merged = copy.deepcopy(supplied)
            merged.extend(copy.deepcopy(item) for item in post.get(field, []) if item[scope_key] not in scopes)
            # Order is not evidence; preserve the existing canonical order when equal.
            _require(sorted(merged, key=data_hash) == sorted(post.get(field, []), key=data_hash),
                     'saved_personal_core_mismatch')
    scopes = _target_scopes(amendment)
    update = amendment['workTiming']
    timing.validate(update, owner=post, date=post['date'], source_kind='personal-work-post')
    _require(all(timing.scope(fact) in scopes for fact in update['facts']), 'saved_personal_scope_mismatch')
    result = copy.deepcopy(post)
    result['workTiming'] = timing.merge(post.get('workTiming'), update)
    personal.valid_post(result)
    return result


def validate_revisions(state, personal):
    """Bind saved current revisions, retaining proof if a later native analysis exists."""
    imports = state.get('savedPersonalImports', {})
    current = validate_imports(imports, personal)
    for tid, (receipt_id, post) in current.items():
        if imports[receipt_id]['amendment'].get('operation') != TIMING_OPERATION:
            continue
        active = [item for item in state['posts'] if item['id'] == tid]
        history = state.get('azureAnalysis', {}).get('history', [])
        _require(active == [post] or post in history, 'saved_personal_revision_mismatch')
    return current


def subject_facts(state, tid):
    facts = {field: [copy.deepcopy(item) for item in state[field] if item['id'] == tid]
             for field in ('posts', 'pending', 'resolved')}
    names = {item['name'] for values in facts.values() for item in values}
    facts['identityBindings'] = {
        name: copy.deepcopy(state['identityBindings'][name])
        for name in sorted(names) if name in state['identityBindings']
    }
    imports = {key: copy.deepcopy(entry) for key, entry in state.get('savedPersonalImports', {}).items()
               if entry['amendment']['id'] == tid}
    history = [copy.deepcopy(post) for post in state.get('azureAnalysis', {}).get('history', [])
               if post['id'] == tid]
    if imports:
        facts['savedPersonalImports'] = imports
    if history:
        facts['analysisHistory'] = history
    return facts


def subject_hash(state, tid):
    return data_hash(subject_facts(state, tid))


def validate_accounting(imports, usage, personal):
    """Bind individual saved analysis attestations to existing aggregate usage."""
    validate_imports(imports, personal)
    _require(not imports or isinstance(usage, dict), 'missing_saved_personal_usage')
    groups = {}
    for entry in imports.values():
        source = entry['amendment']['source']
        receipt_id = source['usageReceiptId']
        receipt = usage.get('imports', {}).get(receipt_id)
        _require(isinstance(receipt, dict) and receipt.get('receiptId') == receipt_id
                 and receipt.get('sourceHash') == source['usageSourceHash'],
                 'saved_personal_usage_mismatch')
        date = personal.official.timestamp(source['analyzedAt']).astimezone(
            personal.official.JST).date().isoformat()
        _require(receipt.get('date') == date, 'saved_personal_usage_mismatch')
        count = sum(item['count'] for item in receipt.get('modelBreakdown', [])
                    if all(item.get(key) == expected for key, expected in (
                        ('model', MODEL), ('deployment', MODEL), ('modelVersion', MODEL_VERSION),
                        ('kind', 'text'), ('component', 'personal'))))
        groups[receipt_id] = groups.get(receipt_id, 0) + 1
        _require(groups[receipt_id] <= count, 'saved_personal_usage_exhausted')


def _check_binding(source, bindings):
    binding = bindings.get(source['name'])
    if binding is not None:
        _require(all(binding[field] == source[field] for field in ('authorId', 'authorScreenName')),
                 'saved_personal_identity_mismatch')
    for name, other in bindings.items():
        _require(name == source['name'] or (
            other['authorId'] != source['authorId']
            and other['authorScreenName'].casefold() != source['authorScreenName'].casefold()),
            'saved_personal_binding_collision')
    return binding


def _check_subject(state, entry, personal, *, registry=None, binding_maps=(), daily_targets=None):
    amendment, source = entry['amendment'], entry['amendment']['source']
    tid = amendment['id']
    facts = subject_facts(state, tid)
    items = [item for field in ('posts', 'pending', 'resolved') for item in facts[field]]
    if amendment.get('operation') == MANUAL_IMPORT_OPERATION:
        _require(not items, 'manual_saved_personal_subject_exists')
        _require(subject_hash(state, tid) == entry['expectedSubjectHash'],
                 'saved_personal_subject_mismatch')
        _require(registry is not None, 'manual_saved_personal_registry_required')
        incoming = {source['name']: {
            'authorId': source['authorId'], 'authorScreenName': source['authorScreenName'],
            'verifiedAt': source['fetchedAt']}}
        targets, _ = personal.members.collection_population(
            registry, state['identityBindings'], *binding_maps, incoming)
        member = personal.members.lookup(registry, source['name'])
        target = targets.get(member['memberId']) if member else None
        _require(target is not None and target['name'] == source['name']
                 and target['handle'].casefold() == source['authorScreenName'].casefold(),
                 'saved_personal_identity_mismatch')
        work = (daily_targets or {}).get(source['date'], {}).get(source['name'])
        shifts = work.get('shifts', []) if work else []
        _require(shifts and all(event['shift'] in shifts for event in amendment['events'])
                 and all(link['scope'] == 'unspecified' or link['scope'] in shifts
                         for link in amendment.get('links') or [])
                 and all(fact['shift'] in shifts for fact in (amendment.get('workTiming') or {}).get('facts', [])),
                 'manual_saved_personal_work_scope_required')
        return
    _require(items, 'unknown_saved_personal_post')
    _require(all(len(facts[field]) <= 1 for field in ('posts', 'pending', 'resolved')))
    _require(subject_hash(state, tid) == entry['expectedSubjectHash'],
             'saved_personal_subject_mismatch')
    for item in items:
        for field in ('url', 'name', 'date', 'authorId', 'authorScreenName'):
            _require(field not in item or item[field] == source[field],
                     'saved_personal_identity_mismatch')
        if 'createdAt' in item:
            _require(personal.official.timestamp(item['createdAt'])
                     == personal.official.timestamp(source['createdAt']))
    pending = facts['pending']
    if pending and pending[0]['searchCreatedAt'] is not None:
        _require(source['provenance'] == 'search'
                 and source['searchCreatedAt'] == pending[0]['searchCreatedAt'],
                 'saved_personal_search_mismatch')
    else:
        _require(source['provenance'] == 'direct', 'saved_personal_search_mismatch')
        if pending:
            _require(pending[0].get('metadataSource') in ('saved_post', 'saved_binding'))
    if pending and 'sourceCreatedAt' in pending[0]:
        _require(pending[0].get('metadataSource') == 'saved_post'
                 and personal.official.timestamp(pending[0]['sourceCreatedAt'])
                 == personal.official.timestamp(source['createdAt']),
                 'saved_personal_search_mismatch')
    binding = _check_binding(source, state['identityBindings'])
    if binding is None:
        _require(pending and pending[0]['searchCreatedAt'] is not None,
                 'missing_saved_personal_binding')


def _assert_delta(before, after, entries):
    ids = {entry['amendment']['id'] for entry in entries}
    names = {entry['amendment']['source']['name'] for entry in entries}
    fields = {'posts', 'pending', 'resolved', 'identityBindings', 'savedPersonalImports'}
    _require({key: value for key, value in before.items() if key not in fields}
             == {key: value for key, value in after.items() if key not in fields},
             'saved_personal_unrelated_state_changed')
    for field in ('posts', 'pending', 'resolved'):
        _require([item for item in before[field] if item['id'] not in ids]
                 == [item for item in after[field] if item['id'] not in ids],
                 'saved_personal_unrelated_state_changed')
    for name, binding in before['identityBindings'].items():
        _require(after['identityBindings'].get(name) == binding)
    _require(set(after['identityBindings']) - set(before['identityBindings']) <= names)
    for key, receipt in before.get('savedPersonalImports', {}).items():
        _require(after.get('savedPersonalImports', {}).get(key) == receipt)


def apply_amendments(state, entries, usage, personal, *, registry=None, binding_maps=(), daily_targets=None):
    _require(isinstance(entries, list) and len(entries) <= 3)
    previous = state.get('savedPersonalImports', {})
    validate_accounting(previous, usage, personal)
    current = validate_revisions(state, personal)
    prepared, ids = [], set()
    receipts = copy.deepcopy(previous)
    proposed_bindings = copy.deepcopy(state['identityBindings'])
    for entry in entries:
        post = _validate_entry(entry, personal)
        tid = post['id']
        _require(tid not in ids, 'duplicate_saved_personal_post')
        ids.add(tid)
        receipt_id = data_hash(entry['amendment'])
        if receipt_id in previous:
            recorded = previous[receipt_id]
            if entry['amendment'].get('operation') == TIMING_OPERATION:
                _require(recorded == entry, 'saved_personal_receipt_conflict')
                continue
            _require(recorded['amendment'] == entry['amendment']
                     and entry['expectedSubjectHash'] in (
                         recorded['expectedSubjectHash'], subject_hash(state, tid)),
                     'saved_personal_receipt_conflict')
            continue
        if entry['amendment'].get('operation') == TIMING_OPERATION:
            _require(tid in current, 'saved_personal_predecessor_required')
            predecessor_id, predecessor_post = current[tid]
            _require(entry['amendment']['previous']['importId'] == predecessor_id,
                     'saved_personal_predecessor_mismatch')
            _require(subject_hash(state, tid) == entry['expectedSubjectHash'],
                     'saved_personal_subject_mismatch')
            existing = [item for item in state['posts'] if item['id'] == tid]
            _require(len(existing) == 1 and existing[0] == predecessor_post,
                     'saved_personal_core_mismatch')
            _require(_check_binding(entry['amendment']['source'], proposed_bindings) is not None,
                     'missing_saved_personal_binding')
            post = _timing_revision(existing[0], entry['amendment'], previous[predecessor_id],
                                    predecessor_id, personal)
            receipts[receipt_id] = copy.deepcopy(entry)
            prepared.append((entry, post))
            continue
        _require(not any(item['amendment']['id'] == tid for item in previous.values()),
                 'saved_personal_receipt_conflict')
        _require(entry['amendment']['source']['contractVersion'] != TIMING_CONTRACT
                 or not any(item['id'] == tid for item in state['posts']),
                 'saved_personal_timing_operation_required')
        _check_subject(state, entry, personal, registry=registry, binding_maps=binding_maps,
                       daily_targets=daily_targets)
        source = entry['amendment']['source']
        if _check_binding(source, proposed_bindings) is None:
            proposed_bindings[source['name']] = {
                'authorId': source['authorId'], 'authorScreenName': source['authorScreenName'],
                'verifiedAt': source['fetchedAt'],
            }
        receipts[receipt_id] = copy.deepcopy(entry)
        prepared.append((entry, post))
    validate_accounting(receipts, usage, personal)
    result = copy.deepcopy(state)
    for entry, post in prepared:
        tid = post['id']
        existing = next((index for index, item in enumerate(result['posts']) if item['id'] == tid), None)
        if existing is None:
            result['posts'].append(post)
        else:
            result['posts'][existing] = post
        if entry['amendment'].get('operation') != TIMING_OPERATION:
            for field in ('pending', 'resolved'):
                result[field] = [item for item in result[field] if item['id'] != tid]
    if prepared:
        result['identityBindings'] = proposed_bindings
        result['savedPersonalImports'] = receipts
    _assert_delta(state, result, entries)
    return result


def apply_link_reviews(state, entries, usage, personal):
    """Review only a native, link-only undated-work false positive; retain its evidence."""
    _require(isinstance(entries, list) and len(entries) <= 1, 'invalid_personal_link_review')
    result = copy.deepcopy(state)
    for entry in entries:
        _keys(entry, ('id', 'expectedSubjectHash', 'bodyHash', 'sourceHash'))
        _require(personal.official.post_id(entry['id']), 'invalid_personal_link_review')
        for field in ('expectedSubjectHash', 'bodyHash', 'sourceHash'):
            _hash(entry[field])
        tid = entry['id']
        resolved = [item for item in result['resolved'] if item['id'] == tid]
        if len(resolved) == 1 and resolved[0].get('linkReview') == entry:
            continue
        _require(subject_hash(result, tid) == entry['expectedSubjectHash'],
                 'saved_personal_subject_mismatch')
        posts = [post for post in result['posts'] if post['id'] == tid]
        _require(len(posts) == len(resolved) == 1 and resolved[0]['reason'] == 'links'
                 and not any(item['id'] == tid for item in result['pending'])
                 and not any(item['amendment']['id'] == tid
                             for item in result.get('savedPersonalImports', {}).values()),
                 'personal_link_review_requires_native_link')
        post = posts[0]
        _require(post['events'] == [] and not post.get('workTiming')
                 and post.get('links') == [{'scope': 'unspecified', 'status': 'work'}],
                 'personal_link_review_requires_native_link')
        analysis = result.get('azureAnalysis', {})
        cache_keys = {key for key, cached in analysis.get('cache', {}).items()
                      if cached['postId'] == tid and cached['bodyHash'] == entry['bodyHash']
                      and cached['reason'] == 'links' and cached['events'] == []
                      and cached.get('links') == post['links'] and not cached.get('workTiming')}
        _require(any(receipt['requestHash'] in cache_keys and receipt['component'] == 'personal'
                     and receipt.get('issuedAt') and receipt.get('reason') == 'links'
                     for receipt in (usage or {}).get('receipts', {}).values()),
                 'personal_link_review_source_mismatch')
        _require(_check_binding(post, result['identityBindings']) is not None,
                 'missing_saved_personal_binding')
        if post not in analysis['history']:
            analysis['history'].append(copy.deepcopy(post))
        result['posts'] = [item for item in result['posts'] if item['id'] != tid]
        resolved[0].update(reason='reviewed_undated_work', linkReview=copy.deepcopy(entry),
                           resolvedAt=personal.official.iso(personal.official.utc_now()))
        row = result.get('coverage', {}).get(post['date'], {}).get(post['name'])
        if row is not None:
            row['postIds'] = [value for value in row['postIds'] if value != tid]
            row['linkScopes'] = [value for value in row['linkScopes'] if value['id'] != tid]
            if not row['postIds']:
                row['reason'] = 'reviewed_undated_work'
    return result
