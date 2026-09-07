"""Bounded, already-accounted half-month evidence imports; never opens clients."""
import copy
import datetime as dt


PROOF_HASHES = ('usageReceiptId', 'usageSourceHash', 'sourceManifestHash',
                'analysisResultHash', 'analysisReceiptHash')


def _require(condition, reason='invalid_saved_half_month'):
    if not condition:
        raise ValueError(reason)


def subject_hash(state, half_month):
    return half_month.digest(state)


def _analysis_for_receipt(state, receipt_id):
    keys = state['receipts'].get(receipt_id, [])
    _require(bool(keys), 'missing_half_month_revision')
    analyses = [state['revisions'][key]['analysis'] for key in keys]
    _require(all(item == analyses[0] for item in analyses), 'half_month_analysis_conflict')
    return analyses[0]


def _import_capacity(usage, proof, analysis, half_month):
    receipt = (usage or {}).get('imports', {}).get(proof['usageReceiptId'])
    _require(isinstance(receipt, dict) and receipt['sourceHash'] == proof['usageSourceHash'],
             'saved_half_month_usage_missing')
    issued = half_month.timestamp(proof['issuedAt'])
    _require(receipt['date'] == issued.astimezone(half_month.JST).date().isoformat()
             and issued <= half_month.timestamp(analysis['analyzedAt']),
             'saved_half_month_usage_date')
    kind = 'image' if analysis['images'] else 'text'
    return sum(item['count'] for item in receipt['modelBreakdown']
               if item.get('model') == half_month.MODEL
               and item.get('deployment') == half_month.MODEL
               and item.get('modelVersion') == half_month.MODEL_VERSION
               and item.get('component') in ('schedule', 'external')
               and item.get('kind') == kind)


def validate_accounting(state, usage, half_month):
    attributed = {}
    for proof in state.get('savedImports', {}).values():
        analysis = _analysis_for_receipt(state, proof['receiptId'])
        _require(analysis['requestHash'] == proof['requestHash'], 'saved_half_month_request_mismatch')
        capacity = _import_capacity(usage, proof, analysis, half_month)
        key = (proof['usageReceiptId'], bool(analysis['images']))
        used = attributed.setdefault(key, set())
        used.add(proof['receiptId'])
        _require(0 < len(used) <= capacity, 'saved_half_month_usage_overallocated')
    imported = {proof['receiptId'] for proof in state.get('savedImports', {}).values()}
    for receipt_id in state['receipts']:
        if receipt_id in imported:
            continue
        analysis = _analysis_for_receipt(state, receipt_id)
        receipt = (usage or {}).get('receipts', {}).get(receipt_id)
        _require(isinstance(receipt, dict) and receipt['component'] == 'schedule'
                 and receipt['requestHash'] == analysis['requestHash']
                 and receipt['issuedAt'] is not None and receipt['completedAt'] is not None
                 and receipt['reason'] in ('events', 'schedule'), 'half_month_usage_missing')
        identity = receipt['identity']
        _require(identity['model'] == half_month.MODEL
                 and identity['modelVersion'] == half_month.MODEL_VERSION
                 and identity['deployment'] == half_month.MODEL,
                 'half_month_usage_model_mismatch')


def apply_amendments(state, entries, usage, half_month, *, schedule, insights, accounts,
                     personal_state, now):
    _require(isinstance(entries, list) and len(entries) <= 1)
    if state is not None:
        half_month.validate_state(state)
        validate_accounting(state, usage, half_month)
    result = copy.deepcopy(state) if state is not None else half_month.empty_state()
    for entry in entries:
        half_month.require_keys(entry, ('expectedSubjectHash', 'amendment'))
        half_month.valid_hash(entry['expectedSubjectHash'])
        amendment = entry['amendment']
        half_month.require_keys(amendment, ('source', 'schedules', 'analysis', 'proof'))
        source, analysis, proof = (amendment[key] for key in ('source', 'analysis', 'proof'))
        half_month.validate_source(source)
        half_month.validate_analysis(analysis)
        half_month.require_keys(proof, (*PROOF_HASHES, 'issuedAt', 'searchCreatedAt'))
        for field in PROOF_HASHES:
            half_month.valid_hash(proof[field])
        issued, searched = (half_month.timestamp(proof[field]) for field in ('issuedAt', 'searchCreatedAt'))
        _require(abs((searched - half_month.timestamp(source['createdAt'])).total_seconds()) < 1,
                 'saved_half_month_search_mismatch')
        _require(half_month.timestamp(source['observedAt']) <= issued
                 <= half_month.timestamp(analysis['analyzedAt']) <= now.astimezone(dt.timezone.utc),
                 'saved_half_month_chronology')
        _require(len(source['media']) == len(analysis['images']), 'saved_half_month_partial_images')
        _require(isinstance(amendment['schedules'], list) and 1 <= len(amendment['schedules']) <= 2)
        for table in amendment['schedules']:
            half_month.validate_schedule(table)
            _require(all(table[key] == source[key] for key in (
                'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt', 'observedAt')),
                'saved_half_month_source_mismatch')
        bindings = copy.deepcopy(personal_state['identityBindings'])
        for name, bound in result['identityBindings'].items():
            previous = bindings.get(name)
            _require(previous is None or all(previous[key] == bound[key] for key in (
                'authorId', 'authorScreenName')), 'saved_half_month_binding_mismatch')
            bindings[name] = bound
        targets, _ = half_month.population(schedule, insights, accounts, bindings)
        target = targets.get(source['name'])
        _require(target is not None and target['handle'] == source['authorScreenName'],
                 'saved_half_month_account_mismatch')
        for name, bound in bindings.items():
            if name == source['name']:
                _require(all(source[key] == bound[key] for key in (
                    'authorId', 'authorScreenName')), 'saved_half_month_binding_mismatch')
            else:
                _require(source['authorId'] != bound['authorId']
                         and source['authorScreenName'].casefold() != bound['authorScreenName'].casefold(),
                         'saved_half_month_binding_ambiguous')
        _require(_import_capacity(usage, proof, analysis, half_month) > 0,
                 'saved_half_month_usage_model')
        key = half_month.digest(amendment)
        if key in result.get('savedImports', {}):
            continue
        _require(entry['expectedSubjectHash'] == subject_hash(state, half_month),
                 'saved_half_month_subject_changed')
        half_month.apply_revision(result, amendment['schedules'], source, analysis)
        result.setdefault('savedImports', {})[key] = {
            **{field: proof[field] for field in PROOF_HASHES},
            'receiptId': analysis['receiptId'], 'requestHash': analysis['requestHash'],
            'issuedAt': proof['issuedAt'], 'importedAt': half_month.stamp(now),
        }
        result['checkedAt'] = half_month.stamp(now)
        result['lastSuccessAt'] = half_month.stamp(now)
        result['lastRun'] = {'status': 'ok'}
    half_month.validate_state(result)
    validate_accounting(result, usage, half_month)
    return result
