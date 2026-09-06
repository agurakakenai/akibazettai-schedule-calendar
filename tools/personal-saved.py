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

savedPersonalImports is a private map keyed by SHA256 of the amendment's canonical
JSON. validate_imports(value, personal) raises ValueError on invalid data. personal
may be the collector module or its azure_context() (official and valid_post).
subject_hash(state, id) identifies all canonical subject facts and their binding.
apply_amendments(state, entries, usage, personal) returns a new state; usage is the
pre-existing canonical ledger, not a ledger with this run's imports added.
"""
import copy
import hashlib
import json
import re


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
            'observedAt': source['fetchedAt'], 'events': copy.deepcopy(amendment['events'])}
    if 'links' in amendment:
        post['links'] = copy.deepcopy(amendment['links'])
    return post


def _validate_entry(entry, personal):
    _keys(entry, ('expectedSubjectHash', 'amendment'))
    _hash(entry['expectedSubjectHash'])
    amendment = entry['amendment']
    _keys(amendment, ('schemaVersion', 'id', 'source', 'events'), ('links',))
    _require(type(amendment['schemaVersion']) is int and amendment['schemaVersion'] == 1)
    source = amendment['source']
    _keys(source, SOURCE_FIELDS, ('searchCreatedAt',))
    for field in HASHES:
        _hash(source[field])
    _require(source['model'] == source['deployment'] == MODEL
             and source['modelVersion'] == MODEL_VERSION)
    _require(source['contractVersion'] in (
        LEGACY_CONTRACT, PREVIOUS_LINK_CONTRACT, NULLABLE_LINK_CONTRACT, LINK_CONTRACT))
    _require(source['provenance'] in ('search', 'direct')
             and ('searchCreatedAt' in source) == (source['provenance'] == 'search'))
    official = personal.official
    created = official.timestamp(source['createdAt'])
    fetched = official.timestamp(source['fetchedAt'])
    analyzed = official.timestamp(source['analyzedAt'])
    _require(created <= fetched <= analyzed)
    if source['provenance'] == 'search':
        searched = official.timestamp(source['searchCreatedAt'])
        _require(abs((searched - created).total_seconds()) < 2
                 and searched.astimezone(official.JST).date().isoformat() == source['date'])
    _require(isinstance(amendment['events'], list) and len(amendment['events']) <= 8)
    if source['contractVersion'] == LEGACY_CONTRACT:
        # Saved v4 acceptances do not attest new link-only/unknown-scope meaning.
        _require(amendment['events'])
    post = post_from_amendment(amendment)
    personal.valid_post(post)
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
    ids, analysis = set(), set()
    for receipt_id, entry in value.items():
        _hash(receipt_id)
        _validate_entry(entry, personal)
        amendment = entry['amendment']
        _require(receipt_id == data_hash(amendment))
        identity = amendment['id']
        receipt = amendment['source']['analysisReceiptHash']
        _require(identity not in ids and receipt not in analysis)
        ids.add(identity)
        analysis.add(receipt)


def subject_facts(state, tid):
    facts = {field: [copy.deepcopy(item) for item in state[field] if item['id'] == tid]
             for field in ('posts', 'pending', 'resolved')}
    names = {item['name'] for values in facts.values() for item in values}
    facts['identityBindings'] = {
        name: copy.deepcopy(state['identityBindings'][name])
        for name in sorted(names) if name in state['identityBindings']
    }
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


def _check_subject(state, entry, personal):
    amendment, source = entry['amendment'], entry['amendment']['source']
    tid = amendment['id']
    facts = subject_facts(state, tid)
    items = [item for field in ('posts', 'pending', 'resolved') for item in facts[field]]
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


def apply_amendments(state, entries, usage, personal):
    _require(isinstance(entries, list) and len(entries) <= 3)
    previous = state.get('savedPersonalImports', {})
    validate_accounting(previous, usage, personal)
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
            _require(recorded['amendment'] == entry['amendment']
                     and entry['expectedSubjectHash'] in (
                         recorded['expectedSubjectHash'], subject_hash(state, tid)),
                     'saved_personal_receipt_conflict')
            continue
        _require(not any(item['amendment']['id'] == tid for item in previous.values()),
                 'saved_personal_receipt_conflict')
        _check_subject(state, entry, personal)
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
    for _, post in prepared:
        tid = post['id']
        existing = next((index for index, item in enumerate(result['posts']) if item['id'] == tid), None)
        if existing is None:
            result['posts'].append(post)
        else:
            result['posts'][existing] = post
        for field in ('pending', 'resolved'):
            result[field] = [item for item in result[field] if item['id'] != tid]
    if prepared:
        result['identityBindings'] = proposed_bindings
        result['savedPersonalImports'] = receipts
    _assert_delta(state, result, entries)
    return result
