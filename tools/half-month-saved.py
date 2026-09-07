"""Bounded, already-accounted half-month evidence imports; never opens clients."""
import copy
import datetime as dt


PROOF_HASHES = ('usageReceiptId', 'usageSourceHash', 'sourceManifestHash',
                'analysisResultHash', 'analysisReceiptHash')


def validate_timing_accounting(usage, proof, analysis, half_month):
    """New-purpose accounting is explicit: never fall back between native and imported usage."""
    half_month.load_module('analysis-state.py', 'half_timing_accounting').validate_state(usage)
    half_month.require_keys(proof, (*PROOF_HASHES, 'issuedAt', 'searchCreatedAt', 'accountingKind'))
    for field in PROOF_HASHES:
        half_month.valid_hash(proof[field])
    for field in ('issuedAt', 'searchCreatedAt'):
        half_month.timestamp(proof[field])
    _require(analysis['contract'] == half_month.TIMING_VERSION, 'saved_half_month_accounting_kind')
    _require(proof['analysisResultHash'] == analysis['resultHash']
             and proof['analysisReceiptHash'] == half_month.digest(analysis),
             'saved_half_month_result_attestation')
    attestation = {
        'contract': half_month.TIMING_VERSION, 'requestHash': analysis['requestHash'],
        'resultHash': analysis['resultHash'], 'semanticResultHash': analysis['timingOnly']['semanticResultHash'],
        'analysisHash': half_month.digest(analysis), 'consumed': 1}
    native_id = half_month.digest(('schedule:' + analysis['requestHash']).encode('utf-8'))
    if proof['accountingKind'] == 'native':
        receipt = usage['receipts'].get(proof['usageReceiptId'])
        _require(proof['usageReceiptId'] == analysis['receiptId'] == native_id
                 and isinstance(receipt, dict), 'saved_half_month_native_missing')
        _require(receipt['component'] == 'schedule' and receipt['requestHash'] == analysis['requestHash']
                 and receipt['issuedAt'] == proof['issuedAt'] and receipt['completedAt'] is not None
                 and receipt['reason'] in ('events', 'no_event', 'azure_pending')
                 and receipt['httpStatus'] is None and receipt['retryAt'] is None,
                 'saved_half_month_native_phase')
        _require(half_month.timestamp(receipt['issuedAt']) <= half_month.timestamp(analysis['analyzedAt'])
                 <= half_month.timestamp(receipt['completedAt']), 'saved_half_month_native_chronology')
        _require(all(receipt['identity'][key] == value for key, value in (
            ('model', half_month.MODEL), ('deployment', half_month.MODEL),
            ('modelVersion', half_month.MODEL_VERSION))), 'half_month_usage_model_mismatch')
        _require(proof['usageSourceHash'] == half_month.digest(receipt)
                 and proof['sourceManifestHash'] == analysis['timingOnly']['sourceHash'],
                 'saved_half_month_native_source_hash')
    elif proof['accountingKind'] == 'imported':
        _require(native_id not in usage['receipts'] and analysis['receiptId'] not in usage['receipts'],
                 'saved_half_month_timing_usage_reused')
        _require(_import_capacity(usage, proof, analysis, half_month) == 1, 'saved_half_month_usage_model')
        receipt = usage['imports'][proof['usageReceiptId']]
    else:
        raise ValueError('saved_half_month_accounting_kind')
    _require(receipt.get('resultAttestation') == attestation, 'saved_half_month_result_attestation')
    return 1


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
        if analysis['contract'] == half_month.TIMING_VERSION:
            revision = state['revisions'][state['receipts'][proof['receiptId']][0]]
            source = revision.get('source', state['sources'][revision['sourceKey']]['source'])
            capacity = validate_timing_accounting(usage, {
                **{field: proof[field] for field in (*PROOF_HASHES, 'issuedAt', 'accountingKind')},
                'searchCreatedAt': source['createdAt']}, analysis, half_month)
        else:
            capacity = _import_capacity(usage, proof, analysis, half_month)
        key = (proof['usageReceiptId'], bool(analysis['images']))
        used = attributed.setdefault(key, set())
        used.add(proof['receiptId'])
        _require(0 < len(used) <= capacity, 'saved_half_month_usage_overallocated')
    imported = {proof['receiptId'] for proof in state.get('savedImports', {}).values()}
    timing_imports = {revision['timingAmendment']['importId']
                      for revision in state['revisions'].values() if 'timingAmendment' in revision}
    for import_id in timing_imports:
        proof = state['savedImports'][import_id]
        _require(sum(item['usageReceiptId'] == proof['usageReceiptId']
                     for item in state['savedImports'].values()) == 1,
                 'saved_half_month_timing_usage_reused')
        _require(proof.get('accountingKind') == 'native'
                 or proof['usageReceiptId'] not in (usage or {}).get('receipts', {}),
                 'saved_half_month_timing_usage_reused')
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


def validate_reanalysis_basis(state, authorization, source, images, half_month, *,
                              expected_apply_subject_hash=None):
    """Shared pre-issue/apply checks against the authoritative predecessor, not caller-supplied core."""
    half_month.validate_state(state)
    half_month.validate_timing_authorization(authorization, with_import=False)
    half_month.validate_source(source)
    expected_subject = authorization['expectedSubjectHash']
    if expected_apply_subject_hash is not None:
        half_month.valid_hash(expected_apply_subject_hash)
        expected_subject = expected_apply_subject_hash
    _require(expected_subject == subject_hash(state, half_month),
             'saved_half_month_subject_changed')
    prior = authorization['previous']
    selected = half_month.select_revisions(state)
    current_keys = {key for key, revision in selected if any(
        table['id'] == revision['schedule']['id']
        and table['period']['from'] == revision['schedule']['period']['from']
        and table['name'] == revision['schedule']['name'] for table in state['schedules'])}
    prior_keys = state['receipts'].get(prior['analysisReceiptId'], [])
    _require(bool(prior_keys) and set(prior_keys) <= current_keys, 'saved_half_month_timing_parent_stale')
    _require(authorization['basisRevisionKeys'] == half_month.projection_basis(state, prior['analysisReceiptId']),
             'saved_half_month_timing_basis_stale')
    analysis = _analysis_for_receipt(state, prior['analysisReceiptId'])
    _require(prior['contractVersion'] == analysis['contract']
             and prior['contractHash'] == half_month.contract_hash(analysis),
             'saved_half_month_timing_contract_lineage')
    imported = state.get('savedImports', {})
    if prior.get('kind') == 'native':
        _require(all(item['receiptId'] != prior['analysisReceiptId'] for item in imported.values()),
                 'saved_half_month_timing_native_lineage')
    else:
        _require(imported.get(prior['importId'], {}).get('receiptId') == prior['analysisReceiptId'],
                 'saved_half_month_timing_import_lineage')
    previous_rows = [state['revisions'][key]['schedule'] for key in prior_keys]
    for key in prior_keys:
        revision = state['revisions'][key]
        previous_source = revision.get('source', state['sources'][revision['sourceKey']]['source'])
        _require(all(source[field] == previous_source[field] for field in (
            'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt',
            'bodyHash', 'media', 'editTweetIds')), 'schedule_timing_source_changed')
    _require([item['sha256'] for item in images] == [item['sha256'] for item in analysis['images']],
             'schedule_timing_source_changed')
    current = [row for row in state['schedules'] if any(
        half_month._pair(row) == half_month._pair(previous) for previous in previous_rows)]
    _require(half_month.core_hash(current) == authorization['expectedCoreHash'],
             'saved_half_month_timing_core_changed')
    _require(half_month.timing_hash(current) == authorization['expectedTimingHash'],
             'saved_half_month_timing_stale_hash')
    allowed = half_month.validate_timing_authorization(authorization, with_import=False)
    for name, date, shift, _ in allowed:
        _require(any(row['name'] == name and any(
            item['date'] == date and shift in item['shifts'] for item in row['days']) for row in previous_rows),
            'schedule_timing_scope_changed')
    return copy.deepcopy(previous_rows)


def _registry_source(state, source, analysis, registry, personal_bindings, half_month):
    members = half_month.member_registry()
    maps = (personal_bindings, state['identityBindings'])
    members.binding_index(registry, maps)
    member = members.lookup(registry, source['name'])
    source_owner = member['memberId'] if member else source['name']
    identities = {}
    for mapping in maps:
        for name, bound in mapping.items():
            owner = members.lookup(registry, name)
            owner = owner['memberId'] if owner else name
            identity = (bound['authorId'], bound['authorScreenName'].casefold())
            _require(owner not in identities or identities[owner] == identity,
                     'saved_half_month_binding_mismatch')
            identities[owner] = identity
            same_author = identity[0] == source['authorId']
            same_handle = identity[1] == source['authorScreenName'].casefold()
            if owner == source_owner:
                _require(same_author and same_handle, 'saved_half_month_binding_mismatch')
            else:
                _require(not same_author and not same_handle, 'saved_half_month_binding_ambiguous')
    key = half_month.source_key(source)
    admitted = False
    for revision in state['revisions'].values():
        if revision['sourceKey'] != key:
            continue
        previous = revision.get('source', state['sources'][key]['source'])
        if (all(previous[field] == source[field] for field in (
                'id', 'url', 'name', 'authorId', 'authorScreenName', 'createdAt'))
                and [image['sha256'] for image in revision['analysis']['images']]
                == [image['sha256'] for image in analysis['images']]):
            admitted = True
            break
    if not admitted:
        targets, _ = members.collection_population(registry, *maps)
        target = targets.get(member['memberId']) if member else None
        _require(target is not None and target['name'] == source['name']
                 and target['handle'].casefold() == source['authorScreenName'].casefold(),
                 'saved_half_month_account_mismatch')


def apply_amendments(state, entries, usage, half_month, *, schedule, insights, accounts,
                     personal_state, now, approved_selections=None, approved_selection_apply=None,
                     registry=None):
    _require(isinstance(entries, list) and len(entries) <= 1)
    if registry is not None:
        half_month.member_registry().validate_registry(registry)
    if state is not None:
        half_month.validate_state(state)
        validate_accounting(state, usage, half_month)
    result = copy.deepcopy(state) if state is not None else half_month.empty_state()
    for entry in entries:
        separate_apply_cas = isinstance(entry, dict) and 'expectedApplySubjectHash' in entry
        half_month.require_keys(entry, ('expectedSubjectHash', 'amendment',
                                       *(('expectedApplySubjectHash',) if separate_apply_cas else ())))
        half_month.valid_hash(entry['expectedSubjectHash'])
        if separate_apply_cas:
            half_month.valid_hash(entry['expectedApplySubjectHash'])
        amendment = entry['amendment']
        timing_only = isinstance(amendment, dict) and 'operation' in amendment
        selected_mode = isinstance(amendment, dict) and 'selectionProof' in amendment
        _require(not separate_apply_cas or selected_mode, 'timing_selection_apply_subject_requires_selection')
        half_month.require_keys(amendment, ('source', 'schedules', 'analysis', 'proof',
                                           *(half_month.TIMING_AMENDMENT_FIELDS if timing_only else ()),
                                           *(('selectionProof',) if selected_mode else ())))
        source, analysis, proof = (amendment[key] for key in ('source', 'analysis', 'proof'))
        half_month.validate_source(source)
        half_month.validate_analysis(analysis)
        _require(analysis['contract'] != half_month.TIMING_VERSION or timing_only,
                 'schedule_timing_authorization_required')
        timing_contract = analysis['contract'] == half_month.TIMING_VERSION
        _require(not selected_mode or timing_contract, 'timing_selection_contract')
        half_month.require_keys(proof, (*PROOF_HASHES, 'issuedAt', 'searchCreatedAt',
                                       *(('accountingKind',) if timing_contract else ())))
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
        if registry is not None:
            _registry_source(result, source, analysis, registry, personal_state['identityBindings'], half_month)
        else:
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
        capacity = (validate_timing_accounting(usage, proof, analysis, half_month) if timing_contract
                    else _import_capacity(usage, proof, analysis, half_month))
        _require(capacity > 0,
                 'saved_half_month_usage_model')
        selection = amendment.get('selectionProof')
        _require(not selected_mode or isinstance(selection, dict), 'timing_selection_contract')
        timing_product = None
        if timing_contract:
            timing_product = half_month.load_module('half-month-timing.py', 'saved_timing_selection')
            timing_product.validate_selection_record(
                analysis, {**{field: amendment[field] for field in half_month.TIMING_AMENDMENT_FIELDS},
                           'expectedSubjectHash': entry['expectedSubjectHash']},
                source, amendment['schedules'], selection)
        key = half_month.digest(amendment)
        if key in result.get('savedImports', {}):
            _require(result['savedImports'][key].get('expectedApplySubjectHash')
                     == entry.get('expectedApplySubjectHash'), 'saved_half_month_replay_changed')
            if timing_only:
                prior_authorizations = [revision.get('timingAmendment') for revision in result['revisions'].values()
                                        if revision.get('timingAmendment', {}).get('importId') == key]
                _require(bool(prior_authorizations) and all(
                    authorization['expectedSubjectHash'] == entry['expectedSubjectHash']
                    for authorization in prior_authorizations), 'saved_half_month_replay_changed')
            continue
        _require(entry.get('expectedApplySubjectHash', entry['expectedSubjectHash']) == subject_hash(state, half_month),
                 'saved_half_month_subject_changed')
        if selected_mode:
            # The trusted dispatcher supplies approved manifests separately from untrusted amendment data.
            _require(isinstance(approved_selections, dict) and len(approved_selections) == 1,
                     'timing_selection_approval_missing')
            approved = approved_selections.get(selection['approvalManifestHash'])
            timing_product.validate_approved_selection(approved)
            _require(approved == {field: selection[field] for field in timing_product.SELECTION_APPROVAL_FIELDS},
                     'timing_selection_approval_changed')
            _require(isinstance(approved_selection_apply, dict), 'timing_selection_apply_approval_required')
            timing_product.validate_selection_apply_approval(approved_selection_apply, entry)
        authorization = None
        if timing_only:
            _require(proof['searchCreatedAt'] == source['createdAt'],
                     'saved_half_month_timing_search_mismatch')
            authorization = {field: copy.deepcopy(amendment[field])
                             for field in half_month.TIMING_AMENDMENT_FIELDS}
            authorization.update(expectedSubjectHash=entry['expectedSubjectHash'], importId=key)
            half_month.validate_timing_authorization(authorization)
            validate_reanalysis_basis(result, {field: value for field, value in authorization.items()
                                              if field != 'importId'}, source, analysis['images'], half_month,
                                      expected_apply_subject_hash=entry.get('expectedApplySubjectHash'))
            if selected_mode:
                timing_product._authorize(
                    result, {field: value for field, value in authorization.items() if field != 'importId'},
                    source, analysis['images'], usage,
                    expected_apply_subject_hash=entry.get('expectedApplySubjectHash'))
            prior = authorization['previous']
            selected = half_month.select_revisions(result)
            current_keys = {revision_key for revision_key, revision in selected
                            if any(table['id'] == revision['schedule']['id']
                                   and table['period']['from'] == revision['schedule']['period']['from']
                                   and table['name'] == revision['schedule']['name']
                                   for table in result['schedules'])}
            prior_keys = result['receipts'].get(prior['analysisReceiptId'], [])
            _require(bool(prior_keys) and set(prior_keys) <= current_keys,
                     'saved_half_month_timing_parent_stale')
            _require(amendment['basisRevisionKeys'] == half_month.projection_basis(
                result, prior['analysisReceiptId']), 'saved_half_month_timing_basis_stale')
            _require(analysis['receiptId'] not in result['receipts'],
                     'saved_half_month_timing_receipt_reused')
            _require(all(item['usageReceiptId'] != proof['usageReceiptId']
                         for item in result.get('savedImports', {}).values()),
                     'saved_half_month_timing_usage_reused')
            current = [table for table in result['schedules'] if table['id'] == source['id']
                       and any(table['period']['from'] == item['period']['from']
                               for item in amendment['schedules'])]
            _require(half_month.core_hash(current) == amendment['expectedCoreHash'],
                     'saved_half_month_timing_core_changed')
            _require(half_month.timing_hash(current) == amendment['expectedTimingHash'],
                     'saved_half_month_timing_stale_hash')
        imported = {
            **{field: proof[field] for field in PROOF_HASHES},
            **({'accountingKind': proof['accountingKind']} if timing_contract else {}),
            **({'selectionProof': copy.deepcopy(selection)} if selected_mode else {}),
            **({'applyApproval': copy.deepcopy(approved_selection_apply)} if selected_mode else {}),
            **({'expectedApplySubjectHash': entry['expectedApplySubjectHash']} if separate_apply_cas else {}),
            'receiptId': analysis['receiptId'], 'requestHash': analysis['requestHash'],
            'issuedAt': proof['issuedAt'], 'importedAt': half_month.stamp(now),
        }
        working = copy.deepcopy(result)
        half_month.apply_revision(working, amendment['schedules'], source, analysis,
                                  timing_amendment=authorization, saved_import=(key, imported))
        if timing_only and not any(table.get('workTiming', {}).get('facts')
                                   for table in amendment['schedules']):
            continue
        if selected_mode:
            working['lastRun'] = {'status': 'partial'}
            timing_product.validate_selection_delta(
                result, working, analysis, source, amendment['schedules'], selection, reported=True)
            result = working
            continue
        result = working
        result['checkedAt'] = half_month.stamp(now)
        pending = analysis.get('timingOnly', {}).get('pendingSlotIds', [])
        if not pending:
            result['lastSuccessAt'] = half_month.stamp(now)
        result['lastRun'] = {'status': 'partial' if pending else 'ok'}
    half_month.validate_state(result)
    validate_accounting(result, usage, half_month)
    return result
