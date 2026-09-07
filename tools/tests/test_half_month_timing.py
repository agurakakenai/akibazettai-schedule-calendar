"""Offline-only same-source timing contract, authorization, usage and immutable-core tests."""
import copy
import datetime as dt
import json
from unittest import mock
import unittest

import test_half_month_schedules as base
import test_half_month_saved as saved_fixture


timing = base.collector._module('half-month-timing.py', 'test_half_month_timing_product')
facts, azure = timing.facts, timing.azure
NOW = base.NOW + dt.timedelta(minutes=1)
IDENTITY = {'provider': 'azure_openai', 'endpoint': 'https://offline.openai.azure.com',
            'model': facts.MODEL, 'modelVersion': facts.MODEL_VERSION, 'deployment': facts.MODEL}


class TimingOnlyTests(base.Offline):
    def setUp(self):
        super().setUp()
        self.fx = saved_fixture.SavedHalfMonthTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.state = self.fx.legacy_state()
        self.usage = self.fx.usage
        self.text = base.payload()['text']
        self.images = [{'bytes': base.png(), 'mime': 'image/png'}]
        self.selection_approvals = {}
        self.configure()

    def configure(self):
        revision_key, revision = facts.select_revisions(self.state)[0]
        receipt_id = revision['analysis']['receiptId']
        import_id = next(key for key, value in self.state['savedImports'].items()
                         if value['receiptId'] == receipt_id)
        self.source = copy.deepcopy(self.state['revisions'][revision_key]['source'])
        self.approval = {
            'operation': 'work-timing-only', 'updateChannels': ['workTiming'],
            'expectedSubjectHash': facts.digest(self.state),
            'expectedCoreHash': facts.core_hash(self.state['schedules']),
            'expectedTimingHash': facts.timing_hash(self.state['schedules']),
            'previous': {'contractVersion': revision['analysis']['contract'],
                         'contractHash': facts.contract_hash(revision['analysis']),
                         'importId': import_id, 'analysisReceiptId': receipt_id},
            'basisRevisionKeys': facts.projection_basis(self.state, receipt_id),
            'targetScopes': [
                {'name': table['name'], 'serviceDate': row['date'], 'shift': shift,
                 'boundary': 'end' if shift == '昼' else 'start'}
                for table in self.state['schedules'] for row in table['days'] for shift in row['shifts']],
        }

    def seed_source(self, *, caption=None, photos=1, raw=None):
        self.source, self.text, _ = base.source(photos=photos)
        if caption is not None:
            self.text = caption
            self.source['bodyHash'] = facts.digest(caption.encode())
        self.images = [{'bytes': base.png(width=9 + index), 'mime': 'image/png'} for index in range(photos)]
        raw = base.result(contract_version=facts.LEGACY_VERSION) if raw is None else raw
        tables, analysis = azure.saved_result(
            self.source, self.text, self.images, raw, now=base.NOW,
            receipt_id=facts.digest(b'original-explicit-v1-input'), contract_version=facts.LEGACY_VERSION)
        entry = self.fx.entry()
        entry['amendment'].update(source=self.source, schedules=tables, analysis=analysis)
        self.state = self.fx.apply(None, [entry])
        self.configure()

    def prepared(self):
        return timing.prepare_request(self.state, self.approval, self.source, self.text, self.images, self.usage)

    def response(self, by_scope=None):
        slots = self.prepared()['analysis']['timingOnly']['slots']
        by_scope = {('2026-09-05', '昼'): [{'kind': 'long', 'time': None}],
                    ('2026-09-12', '昼'): [{'kind': 'long', 'time': None}]} if by_scope is None else by_scope
        return {'slots': [{'slotId': slot['slotId'],
                           'workTiming': copy.deepcopy(by_scope.get((slot['serviceDate'], slot['shift']), []))}
                          for slot in slots]}

    def packet(self, result=None, label='timing-only'):
        return timing.saved_result(
            self.state, self.approval, self.source, self.text, self.images,
            self.response() if result is None else result, self.usage, now=NOW,
            receipt_id=facts.digest(label.encode()))

    def accounted_proof(self, packet, label='timing-only'):
        receipt_id, source_hash = facts.digest(('usage:' + label).encode()), facts.digest(('source:' + label).encode())
        receipt = {'receiptId': receipt_id, 'sourceHash': source_hash, 'date': '2026-09-07',
                   'counts': {'requests': 1}, 'modelBreakdown': [{
                       'model': facts.MODEL, 'deployment': facts.MODEL, 'modelVersion': facts.MODEL_VERSION,
                       'component': 'schedule', 'kind': 'image', 'count': 1}],
                   'resultAttestation': timing.result_attestation(packet)}
        timing.ledger.apply_import(self.usage, receipt)
        self.fx.usage = self.usage
        return timing.imported_proof(
            packet, self.usage, receipt_id=receipt_id, issued_at=packet['analysis']['analyzedAt'],
            source_manifest_hash=facts.digest(b'new-authorized-source-manifest'))

    def entry(self, packet, label='timing-only'):
        proof = self.accounted_proof(packet, label)
        if packet['status'] == 'partial':
            return self.selected_entry(packet, proof, self.selection_for(packet))
        return timing.to_amendment(packet, proof, self.usage)

    def selection_for(self, packet, expected_facts=None):
        slots = {(slot['serviceDate'], slot['shift']): slot['slotId']
                 for slot in packet['analysis']['timingOnly']['slots']}
        if expected_facts is None:
            expected_facts = [fact for row in packet['schedules'] for fact in row.get('workTiming', {}).get('facts', [])]
        manifest = {
            'schemaVersion': 1, 'kind': 'half-month-timing-selection-approval-v1', 'stage': 'selection-only',
            'mode': 'confirmed-set-only', 'originPacketHash': facts.digest(packet),
            'independentGoldHash': facts.digest(b'independent-synthetic-selection-gold'),
            'selected': sorted([{'slotId': slots[(fact['serviceDate'], fact['shift'])], 'factHash': facts.digest(fact)}
                                for fact in expected_facts], key=lambda item: item['slotId'])}
        manifest['untouchedSlotIds'] = sorted(set(slots.values()) - {item['slotId'] for item in manifest['selected']})
        return {'document': manifest, 'approvalManifestHash': timing.selection_manifest_hash(manifest)}

    def selected_entry(self, packet, proof, approved):
        self.selection_approvals[approved['approvalManifestHash']] = copy.deepcopy(approved)
        return timing.to_selected_amendment(packet, proof, approved, self.state, self.usage)

    def apply_entry(self, state, entry):
        selection = entry['amendment'].get('selectionProof')
        approvals = ({selection['approvalManifestHash']: self.selection_approvals[selection['approvalManifestHash']]}
                     if selection is not None else None)
        return self.fx.apply_timing(state, entry, approved_selections=approvals,
                                    approved_selection_apply=self.final_approval(entry) if selection is not None else None)

    def final_approval(self, entry):
        return {'schemaVersion': 1, 'kind': 'half-month-timing-selection-apply-v1', 'stage': 'apply-exact',
                'selectionApprovalHash': entry['amendment']['selectionProof']['approvalManifestHash'],
                'entryHash': facts.digest(entry)}

    def apply(self, packet, label='timing-only'):
        entry = self.entry(packet, label)
        self.fx.usage = self.usage
        return self.apply_entry(self.state, entry), entry

    def analyzer(self, result):
        usage = mock.Mock(component='schedule', state=self.usage)
        client = mock.Mock(identity=IDENTITY)
        client.structured.return_value = result
        analyzer = timing.AzureAnalyzer(usage, clock=lambda: NOW, client=client)
        return analyzer, usage, client

    def native_packet(self, result=None):
        path = self.work_dir() / 'native-usage.json'
        timing.ledger.atomic_json(path, self.usage)
        client = mock.Mock(identity=IDENTITY)
        client.structured.return_value = self.response() if result is None else result
        with timing.ledger.SharedUsage(path, run_id='native-timing', component='schedule', clock=lambda: NOW,
                                       sleep=lambda _: self.fail('no wait')) as usage:
            packet = timing.AzureAnalyzer(usage, clock=lambda: NOW, client=client).analyze(
                self.state, self.approval, self.source, self.text, self.images, mock.Mock())
            self.usage = copy.deepcopy(usage.state)
        self.fx.usage = self.usage
        return packet, timing.native_proof(packet, self.usage)

    def partial_pattern(self):
        response = self.response()
        for row in response['slots']:
            if not row['workTiming']:
                row['workTiming'] = None
        return response

    def independent_expected_sets(self):
        return facts.timing().bind([{
            'serviceDate': date, 'shift': '昼', 'boundary': 'end', 'status': 'set',
            'qualifier': 'long', 'explicitTime': None}
            for date in ('2026-09-05', '2026-09-12')], self.source, 'half-month-schedule')['facts']

    def unchecked_history(self, entry):
        """Assemble synthetic private records to exercise validation without the public apply gate."""
        amendment = entry['amendment']
        key = facts.digest(amendment)
        state = copy.deepcopy(self.state)
        authorization = {field: copy.deepcopy(amendment[field]) for field in facts.TIMING_AMENDMENT_FIELDS}
        authorization.update(importId=key, expectedSubjectHash=entry['expectedSubjectHash'])
        analysis = amendment['analysis']
        imported = {field: amendment['proof'][field] for field in (*timing.saved.PROOF_HASHES, 'issuedAt')}
        imported.update(accountingKind=amendment['proof']['accountingKind'], receiptId=analysis['receiptId'],
                        requestHash=analysis['requestHash'], importedAt=facts.stamp(NOW))
        if 'selectionProof' in amendment:
            imported['selectionProof'] = copy.deepcopy(amendment['selectionProof'])
            imported['applyApproval'] = self.final_approval(entry)
        keys = []
        for table in amendment['schedules']:
            revision = {'schedule': copy.deepcopy(table), 'source': copy.deepcopy(amendment['source']),
                        'sourceKey': facts.source_key(amendment['source']), 'analysis': copy.deepcopy(analysis),
                        'timingAmendment': copy.deepcopy(authorization)}
            revision_key = facts.digest(revision)
            state['revisions'][revision_key] = revision
            keys.append(revision_key)
        state['savedImports'][key] = imported
        state['receipts'][analysis['receiptId']] = keys
        return state

    def test_selected_partial_preserves_full_origin_and_exact_unselected_state_with_both_accounting_kinds(self):
        historical = self.fx.entry()
        original_source = historical['amendment']['source']
        old_facts = [
            {'serviceDate': '2026-09-02', 'shift': '夜', 'boundary': 'start', 'status': 'set',
             'qualifier': 'late', 'explicitTime': '18:00'},
            {'serviceDate': '2026-09-07', 'shift': '昼', 'boundary': 'end', 'status': 'excluded',
             'qualifier': 'short', 'explicitTime': None},
            {'serviceDate': '2026-09-10', 'shift': '夜', 'boundary': 'start', 'status': 'conflict',
             'qualifier': None, 'explicitTime': None},
            {'serviceDate': '2026-09-14', 'shift': '昼', 'boundary': 'end', 'status': 'set',
             'qualifier': None, 'explicitTime': '17:00'},
            {'serviceDate': '2026-09-05', 'shift': '昼', 'boundary': 'end', 'status': 'set',
             'qualifier': 'short', 'explicitTime': '16:00'}]
        historical['amendment']['schedules'][0]['workTiming'] = facts.timing().bind(
            old_facts, original_source, 'half-month-schedule')
        self.state = self.fx.apply(None, [historical])
        self.configure()
        baseline = copy.deepcopy(self.usage)
        expected_facts = self.independent_expected_sets()
        exact = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        for kind in ('native', 'imported'):
            self.usage = copy.deepcopy(baseline)
            with self.subTest(kind=kind):
                if kind == 'native':
                    packet, proof = self.native_packet(self.partial_pattern())
                else:
                    packet = self.packet(self.partial_pattern())
                    proof = self.accounted_proof(packet)
                approved = self.selection_for(packet, expected_facts)
                frozen = exact((packet, proof, approved, self.state, self.usage))
                entry = self.selected_entry(packet, proof, approved)
                selection = entry['amendment']['selectionProof']
                self.assertEqual(len(entry['amendment']['targetScopes']), 6)
                self.assertEqual(len(selection['document']['selected']), 2)
                self.assertEqual(selection['document']['untouchedSlotIds'], packet['pendingSlotIds'])
                self.assertEqual(entry['amendment']['analysis'], packet['analysis'])
                self.assertNotIn('selectionProof', packet['analysis'])
                self.assertEqual(timing.result_attestation(packet)['semanticResultHash'],
                                 packet['analysis']['timingOnly']['semanticResultHash'])
                after = self.apply_entry(self.state, entry)
                self.assertEqual(exact((packet, proof, approved, self.state, self.usage)), frozen)
                self.assertEqual(after['lastRun'], {'status': 'partial'})
                mutable = {'schedules', 'revisions', 'receipts', 'savedImports', 'lastRun'}
                for field in set(self.state) - mutable:
                    self.assertEqual(exact(after[field]), exact(self.state[field]), field)
                for field in ('revisions', 'receipts', 'savedImports'):
                    for key, value in self.state[field].items():
                        self.assertEqual(exact(after[field][key]), exact(value))
                self.assertEqual(exact(timing.core_copy(after['schedules'])),
                                 exact(timing.core_copy(self.state['schedules'])))
                notes = after['schedules'][0]['workTiming']['facts']
                selected_dates = {'2026-09-05', '2026-09-12'}
                self.assertEqual([fact for fact in notes if fact['serviceDate'] not in selected_dates],
                                 [fact for fact in historical['amendment']['schedules'][0]['workTiming']['facts']
                                  if fact['serviceDate'] not in selected_dates])
                self.assertEqual([fact for fact in notes if fact['serviceDate'] in selected_dates], expected_facts)
                self.assertEqual(self.fx.apply_timing(after, entry), after)
                timing.saved.validate_accounting(after, self.usage, facts)
                with self.assertRaisesRegex(ValueError, 'timing_response_pending'):
                    timing.require_complete(packet, proof, self.usage)

    def test_partial_adoption_requires_selection_at_public_apply_and_private_history_layers(self):
        packet, proof = self.native_packet(self.partial_pattern())
        approved = self.selection_for(packet, self.independent_expected_sets())
        entry = self.selected_entry(packet, proof, approved)
        before = copy.deepcopy((self.state, self.usage))
        with self.assertRaisesRegex(ValueError, 'timing_selection_authorization_required'):
            timing.to_amendment(packet, proof, self.usage)
        with self.assertRaisesRegex(ValueError, 'timing_selection_approval_missing'):
            self.fx.apply_timing(self.state, entry)
        ordinary = copy.deepcopy(entry)
        del ordinary['amendment']['selectionProof']
        with self.assertRaisesRegex(ValueError, 'timing_selection_authorization_required'):
            self.fx.apply_timing(self.state, ordinary)
        forged = self.unchecked_history(ordinary)
        with self.assertRaisesRegex(ValueError, 'timing_selection_authorization_required'):
            facts.select_revisions(forged)
        with self.assertRaisesRegex(ValueError, 'timing_selection_authorization_required'):
            facts.validate_state(forged)
        self.assertEqual((self.state, self.usage), before)

    def test_selection_document_and_final_exact_apply_approval_are_two_distinct_stages(self):
        packet, proof = self.native_packet(self.partial_pattern())
        approved = self.selection_for(packet, self.independent_expected_sets())
        document = copy.deepcopy(approved['document'])
        self.assertEqual(document['stage'], 'selection-only')
        self.assertEqual(document['untouchedSlotIds'], packet['pendingSlotIds'])
        self.assertEqual(approved['approvalManifestHash'], facts.digest(document))
        self.assertNotIn('approvalManifestHash', document)
        entry = self.selected_entry(packet, proof, approved)
        final = self.final_approval(entry)
        approvals = {approved['approvalManifestHash']: approved}
        original = copy.deepcopy((packet, proof, approved, entry, self.state, self.usage))
        with self.assertRaisesRegex(ValueError, 'timing_selection_apply_approval_required'):
            self.fx.apply_timing(self.state, entry, approved_selections=approvals)
        with self.assertRaises(ValueError):
            self.fx.apply_timing(self.state, entry, approved_selections=approvals,
                                 approved_selection_apply=document)
        self.assertEqual(final['entryHash'], facts.digest(entry))
        self.assertEqual(final['selectionApprovalHash'], facts.digest(document))
        self.assertNotEqual(final['entryHash'], approved['approvalManifestHash'])
        self.assertNotEqual(facts.digest(final), approved['approvalManifestHash'])
        self.assertNotIn('applyApproval', entry['amendment'])
        self.assertNotIn('entryHash', document)
        after = self.fx.apply_timing(self.state, entry, approved_selections=approvals, approved_selection_apply=final)
        self.assertEqual((packet, proof, approved, entry, self.state, self.usage), original)
        audit = after['savedImports'][facts.digest(entry['amendment'])]
        self.assertEqual(audit['selectionProof']['document'], document)
        self.assertEqual(audit['applyApproval'], final)
        self.assertEqual(after['sources'], self.state['sources'])
        self.assertEqual(self.fx.apply_timing(after, entry), after)
        with self.assertRaisesRegex(ValueError, 'timing_response_pending'):
            timing.require_complete(packet, proof, self.usage)

    def test_full_trial_apply_manifest_and_self_reference_cannot_authorize_selection(self):
        packet, proof = self.native_packet(self.partial_pattern())
        approved = self.selection_for(packet, self.independent_expected_sets())
        entry = self.selected_entry(packet, proof, approved)
        old_trial = {'schemaVersion': 1, 'kind': 'timing1', 'requests': [],
                     'status': 'STOPPED_NO_RETRY_USAGE_MUST_SETTLE', 'policy': {'apply_allowed': False}}
        apply_manifest = {'schemaVersion': 1, 'kind': 'final-apply-manifest', 'entries': [entry]}
        self_reference = {**copy.deepcopy(approved['document']), 'approvalManifestHash': approved['approvalManifestHash']}
        final_reference = {**copy.deepcopy(approved['document']), 'entryHash': facts.digest(entry)}
        wrong_stage = {**copy.deepcopy(approved['document']), 'stage': 'apply-exact'}
        documents = (old_trial, apply_manifest, self.final_approval(entry), copy.deepcopy(approved),
                     self_reference, final_reference, wrong_stage)
        before = copy.deepcopy((self.state, self.usage, packet))
        for document in documents:
            wrapper = {'approvalManifestHash': facts.digest(document), 'document': document}
            with self.subTest(fields=sorted(document)):
                with self.assertRaises(ValueError):
                    timing.selection_manifest_hash(document)
                with self.assertRaises(ValueError):
                    timing.to_selected_amendment(packet, proof, wrapper, self.state, self.usage)
                changed = copy.deepcopy(entry)
                changed['amendment']['selectionProof'] = wrapper
                with self.assertRaises(ValueError):
                    self.fx.apply_timing(
                        self.state, changed, approved_selections={wrapper['approvalManifestHash']: wrapper},
                        approved_selection_apply=self.final_approval(changed))
                # Even rehashed private history cannot turn another document kind into this authorization.
                with self.assertRaises(ValueError):
                    facts.select_revisions(self.unchecked_history(changed))
        self.assertEqual((self.state, self.usage, packet), before)

    def test_selection_document_explicitly_binds_untouched_scope_even_after_rehash(self):
        packet, proof = self.native_packet(self.partial_pattern())
        approved = self.selection_for(packet, self.independent_expected_sets())
        for change in ('omission', 'overlap', 'unknown', 'duplicate'):
            altered = copy.deepcopy(approved)
            document = altered['document']
            if change == 'omission':
                document['untouchedSlotIds'].pop()
            elif change == 'overlap':
                document['untouchedSlotIds'][0] = document['selected'][0]['slotId']
                document['untouchedSlotIds'].sort()
            elif change == 'unknown':
                document['untouchedSlotIds'][0] = 's0000000000'
                document['untouchedSlotIds'].sort()
            else:
                document['untouchedSlotIds'].append(document['untouchedSlotIds'][-1])
            altered['approvalManifestHash'] = facts.digest(document)
            with self.subTest(change=change), self.assertRaises(ValueError):
                timing.to_selected_amendment(packet, proof, altered, self.state, self.usage)

    def test_final_apply_approval_is_exact_and_retained_outside_the_amendment_hash(self):
        packet, proof = self.native_packet(self.partial_pattern())
        approved = self.selection_for(packet, self.independent_expected_sets())
        entry = self.selected_entry(packet, proof, approved)
        final = self.final_approval(entry)
        approvals = {approved['approvalManifestHash']: approved}
        before = copy.deepcopy((self.state, self.usage))
        for change in ('amendmentOnlyHash', 'selectionHash', 'stage', 'selfReference'):
            wrong = copy.deepcopy(final)
            if change == 'amendmentOnlyHash':
                wrong['entryHash'] = facts.digest(entry['amendment'])
            elif change == 'selectionHash':
                wrong['selectionApprovalHash'] = facts.digest(final)
            elif change == 'stage':
                wrong['stage'] = 'selection-only'
            else:
                wrong['applyManifestHash'] = facts.digest(final)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.fx.apply_timing(self.state, entry, approved_selections=approvals, approved_selection_apply=wrong)
        self.assertEqual((self.state, self.usage), before)
        after = self.apply_entry(self.state, entry)
        key = facts.digest(entry['amendment'])
        for change in ('missing', 'wrongEntry', 'wrongDocument'):
            forged = copy.deepcopy(after)
            if change == 'missing':
                del forged['savedImports'][key]['applyApproval']
            elif change == 'wrongEntry':
                forged['savedImports'][key]['applyApproval']['entryHash'] = 'f' * 64
            else:
                forged['savedImports'][key]['applyApproval'] = copy.deepcopy(approved['document'])
            with self.subTest(change=change), self.assertRaises(ValueError):
                facts.validate_state(forged)

    def test_selected_manifest_rejects_wrong_independent_expectations_and_untrusted_replacement(self):
        expected = self.independent_expected_sets()
        packet, proof = self.native_packet(self.partial_pattern())
        approved = self.selection_for(packet, expected)
        entry = self.selected_entry(packet, proof, approved)
        before = copy.deepcopy((self.state, self.usage))
        for change in ('short', 'inventedClock', 'wrongSource', 'partialSelection', 'pending', 'unknown',
                       'duplicate', 'extraField', 'originHash', 'goldHash', 'approvalHash'):
            altered = copy.deepcopy(approved)
            document = altered['document']
            if change in ('short', 'inventedClock', 'wrongSource'):
                wrong = copy.deepcopy(expected)
                if change == 'short':
                    wrong[0]['qualifier'] = 'short'
                elif change == 'inventedClock':
                    wrong[0]['explicitTime'] = '18:00'
                else:
                    wrong[0]['source']['authorId'] = '123'
                altered = self.selection_for(packet, wrong)
            elif change == 'partialSelection':
                document['selected'].pop()
            elif change in ('pending', 'unknown'):
                document['selected'][0]['slotId'] = packet['pendingSlotIds'][0] if change == 'pending' else 's0000000000'
                document['selected'].sort(key=lambda item: item['slotId'])
            elif change == 'duplicate':
                document['selected'].append(copy.deepcopy(document['selected'][-1]))
            elif change == 'extraField':
                document['selected'][0]['boundary'] = 'start'
            elif change == 'originHash':
                document['originPacketHash'] = 'f' * 64
            elif change == 'goldHash':
                document['independentGoldHash'] = 'f' * 64
            else:
                altered['approvalManifestHash'] = 'f' * 64
            if change in ('partialSelection', 'pending', 'unknown', 'originHash'):
                document['untouchedSlotIds'] = sorted(
                    {item['slotId'] for item in packet['analysis']['timingOnly']['slots']}
                    - {item['slotId'] for item in document['selected']})
                altered['approvalManifestHash'] = timing.selection_manifest_hash(document)
            with self.subTest(change=change), self.assertRaises(ValueError):
                timing.to_selected_amendment(packet, proof, altered, self.state, self.usage)
        # Even a self-consistent replacement manifest is not the separately pinned dispatcher approval.
        replacement = copy.deepcopy(approved)
        replacement['document']['independentGoldHash'] = 'f' * 64
        replacement['approvalManifestHash'] = timing.selection_manifest_hash(replacement['document'])
        changed = timing.to_selected_amendment(packet, proof, replacement, self.state, self.usage)
        with self.assertRaises(ValueError):
            self.fx.apply_timing(self.state, changed, approved_selections={approved['approvalManifestHash']: approved})
        with self.assertRaisesRegex(ValueError, 'timing_selection_manifest_changed'):
            wrong_approval = copy.deepcopy(approved)
            wrong_approval['document']['independentGoldHash'] = 'f' * 64
            self.fx.apply_timing(self.state, entry,
                                 approved_selections={approved['approvalManifestHash']: wrong_approval})
        self.assertEqual((self.state, self.usage), before)

    def test_selection_cannot_hide_third_claim_nonsets_or_pending_origin(self):
        baseline = copy.deepcopy(self.usage)
        expected = self.independent_expected_sets()
        for kind in ('thirdSet', 'excluded', 'withdrawn', 'conflict', 'pending'):
            self.usage = copy.deepcopy(baseline)
            response = self.partial_pattern()
            if kind == 'pending':
                response = {'slots': None}
            else:
                note_kind = {'thirdSet': 'long', 'excluded': 'not-long',
                             'withdrawn': 'withdrawn', 'conflict': 'conflict'}[kind]
                response['slots'][2]['workTiming'] = [{'kind': note_kind, 'time': None}]
            packet = self.packet(response)
            proof = self.accounted_proof(packet)
            approved = self.selection_for(packet, expected)
            before = copy.deepcopy((packet, self.state, self.usage))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                timing.to_selected_amendment(packet, proof, approved, self.state, self.usage)
            self.assertEqual((packet, self.state, self.usage), before)

    def test_selected_packet_source_core_attestation_and_subject_tampering_remain_rejected(self):
        packet, proof = self.native_packet(self.partial_pattern())
        approved = self.selection_for(packet, self.independent_expected_sets())
        original = copy.deepcopy((self.state, self.usage))
        for change in ('status', 'pending', 'clock', 'source', 'core', 'semanticHash',
                       'resultHash', 'receipt', 'subject', 'basis', 'image', 'accounting'):
            altered, auth, accounting = copy.deepcopy((packet, approved, proof))
            state, usage = copy.deepcopy((self.state, self.usage))
            if change == 'status':
                altered['status'] = 'ok'
            elif change == 'pending':
                altered['pendingSlotIds'] = []
                altered['analysis']['timingOnly']['pendingSlotIds'] = []
                altered['status'] = 'ok'
            elif change == 'clock':
                altered['schedules'][0]['workTiming']['facts'][0]['explicitTime'] = '16:00'
            elif change == 'source':
                altered['source']['bodyHash'] = 'f' * 64
            elif change == 'core':
                altered['schedules'][0]['days'].reverse()
            elif change == 'semanticHash':
                altered['analysis']['timingOnly']['semanticResultHash'] = 'f' * 64
            elif change in ('resultHash', 'receipt'):
                altered['analysis']['resultHash' if change == 'resultHash' else 'receiptId'] = 'f' * 64
            elif change == 'subject':
                state['checkedAt'] = facts.stamp(NOW)
            elif change == 'basis':
                altered['authorization']['basisRevisionKeys'] = ['f' * 64]
            elif change == 'image':
                altered['analysis']['images'][0]['sha256'] = 'f' * 64
            else:
                accounting['usageReceiptId'] = 'f' * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                timing.to_selected_amendment(altered, accounting, auth, state, usage)
        self.assertEqual((self.state, self.usage), original)

    def test_private_selection_audit_is_hash_bound_and_records_untouched_scopes(self):
        packet, proof = self.native_packet(self.partial_pattern())
        entry = self.selected_entry(packet, proof, self.selection_for(packet, self.independent_expected_sets()))
        for change in ('untouched', 'factHash', 'manifest', 'origin', 'extra', 'drop'):
            changed = copy.deepcopy(entry)
            selection = changed['amendment']['selectionProof']
            if change == 'untouched':
                selection['document']['untouchedSlotIds'].pop()
            elif change == 'factHash':
                selection['document']['selected'][0]['factHash'] = 'f' * 64
            elif change == 'manifest':
                selection['approvalManifestHash'] = 'f' * 64
            elif change == 'origin':
                selection['document']['originPacketHash'] = 'f' * 64
            elif change == 'extra':
                selection['fullTrialAccepted'] = True
            else:
                del changed['amendment']['selectionProof']
            forged = self.unchecked_history(changed)
            with self.subTest(change=change), self.assertRaises(ValueError):
                facts.select_revisions(forged)
        after = self.apply_entry(self.state, entry)
        audit = after['savedImports'][facts.digest(entry['amendment'])]
        self.assertEqual(audit['selectionProof']['document']['untouchedSlotIds'], packet['pendingSlotIds'])
        facts.validate_state(after)

    def test_selected_delta_guard_rejects_every_unrelated_mutation(self):
        packet, proof = self.native_packet(self.partial_pattern())
        entry = self.selected_entry(packet, proof, self.selection_for(packet, self.independent_expected_sets()))
        after = self.apply_entry(self.state, entry)
        for change in ('sourceIndex', 'lastSuccess', 'checkedAt', 'history', 'core', 'unselected', 'status', 'identity'):
            changed = copy.deepcopy(after)
            if change == 'sourceIndex':
                next(iter(changed['sources'].values()))['requestHash'] = packet['analysis']['requestHash']
            elif change in ('lastSuccess', 'checkedAt'):
                changed['lastSuccessAt' if change == 'lastSuccess' else 'checkedAt'] = facts.stamp(NOW)
            elif change == 'history':
                next(iter(changed['revisions'].values()))['analysis']['resultHash'] = 'f' * 64
            elif change == 'core':
                changed['schedules'][0]['days'].reverse()
            elif change == 'unselected':
                claim = copy.deepcopy(changed['schedules'][0]['workTiming']['facts'][0])
                claim['serviceDate'] = '2026-09-07'
                changed['schedules'][0]['workTiming']['facts'].append(claim)
            elif change == 'status':
                changed['lastRun'] = {'status': 'ok'}
            else:
                next(iter(changed['identityBindings'].values()))['verifiedAt'] = facts.stamp(NOW)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'timing_selection_'):
                timing.validate_selection_delta(self.state, changed, packet['analysis'], packet['source'],
                                                packet['schedules'], entry['amendment']['selectionProof'],
                                                reported=True)

    def test_selected_partial_keeps_two_periods_and_all_64_read_bounds(self):
        raw = base.maximum_timing_result()
        for period in raw['periods']:
            for row in period['days']:
                del row['workTiming']
        self.seed_source(photos=4, raw=raw)
        slots = self.prepared()['analysis']['timingOnly']['slots']
        selected_slot = slots[-1]
        qualifier = 'long' if selected_slot['shift'] == '昼' else 'early'
        expected = facts.timing().bind([{
            'serviceDate': selected_slot['serviceDate'], 'shift': selected_slot['shift'],
            'boundary': 'end' if selected_slot['shift'] == '昼' else 'start', 'status': 'set',
            'qualifier': qualifier, 'explicitTime': None}], self.source, 'half-month-schedule')['facts']
        response = {'slots': [{'slotId': slot['slotId'], 'workTiming': (
            [{'kind': qualifier, 'time': None}] if slot['slotId'] == selected_slot['slotId'] else None)}
            for slot in slots]}
        packet = self.packet(response)
        proof = self.accounted_proof(packet)
        entry = self.selected_entry(packet, proof, self.selection_for(packet, expected))
        after = self.apply_entry(self.state, entry)
        self.assertEqual(len(entry['amendment']['targetScopes']), 64)
        self.assertEqual(len(entry['amendment']['selectionProof']['document']['untouchedSlotIds']), 63)
        self.assertEqual(after['schedules'][0], self.state['schedules'][0])
        self.assertEqual(after['schedules'][1]['workTiming']['facts'], expected)
        self.assertEqual(len(after['revisions']) - len(self.state['revisions']), 2)
        self.assertEqual(after['sources'], self.state['sources'])
        self.assertEqual(self.fx.apply_timing(after, entry), after)

    def test_selected_other_period_preserves_existing_timing_after_sorted_json_roundtrip(self):
        raw = base.maximum_timing_result()
        for period in raw['periods']:
            for row in period['days']:
                del row['workTiming']
        self.seed_source(photos=4, raw=raw)
        slots = self.prepared()['analysis']['timingOnly']['slots']
        existing = {slot['slotId']: 'long' if slot['shift'] == '昼' else 'early'
                    for slot in (slots[0], slots[-1])}
        initial = {'slots': [{'slotId': slot['slotId'], 'workTiming': (
            [{'kind': existing[slot['slotId']], 'time': None}] if slot['slotId'] in existing else [])} for slot in slots]}
        baseline, _ = self.apply(self.packet(initial, label='existing-other-period'), label='existing-other-period')
        baseline_usage = copy.deepcopy(self.usage)
        exact = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        for sorted_roundtrip in (False, True):
            with self.subTest(sorted_roundtrip=sorted_roundtrip):
                self.state = (json.loads(json.dumps(baseline, sort_keys=True, ensure_ascii=False))
                              if sorted_roundtrip else copy.deepcopy(baseline))
                self.usage = copy.deepcopy(baseline_usage)
                self.configure()
                untouched = copy.deepcopy(self.state['schedules'][0])
                self.assertTrue(untouched['workTiming']['facts'])
                if sorted_roundtrip:
                    self.assertEqual(list(untouched['workTiming']), ['facts', 'schemaVersion'])
                current_slots = self.prepared()['analysis']['timingOnly']['slots']
                selected = current_slots[-1]
                kind = 'short' if selected['shift'] == '昼' else 'late'
                response = {'slots': [{'slotId': slot['slotId'], 'workTiming': (
                    [{'kind': kind, 'time': None}] if slot['slotId'] == selected['slotId'] else None)}
                    for slot in current_slots]}
                packet = self.packet(response, label='sorted-selection-' + str(sorted_roundtrip))
                after, entry = self.apply(packet, label='sorted-selection-' + str(sorted_roundtrip))
                self.assertEqual(exact(after['schedules'][0]), exact(untouched))
                self.assertEqual(after['sources'], self.state['sources'])
                self.assertEqual(after['lastSuccessAt'], self.state['lastSuccessAt'])
                self.assertEqual(after['lastRun'], {'status': 'partial'})
                facts.validate_state(after)
                facts.validate_state(json.loads(json.dumps(after, sort_keys=True)))
                self.assertEqual(self.fx.apply_timing(after, entry), after)

    def test_saved_complete_empty_period_can_be_reanalyzed_complete_or_selected_after_json_roundtrip(self):
        raw = base.maximum_timing_result()
        for period in raw['periods']:
            period['days'] = period['days'][:1]
            period['days'][0]['shifts'] = period['days'][0]['shifts'][:1]
            del period['days'][0]['workTiming']
        self.seed_source(photos=4, raw=raw)
        slots = self.prepared()['analysis']['timingOnly']['slots']
        first = {'slots': [{'slotId': slot['slotId'], 'workTiming': (
            [{'kind': 'long', 'time': None}] if index == 0 else [])} for index, slot in enumerate(slots)]}
        baseline, first_entry = self.apply(self.packet(first, label='complete-with-empty'), label='complete-with-empty')
        baseline_usage = copy.deepcopy(self.usage)
        self.assertEqual(baseline['schedules'][1]['workTiming'], {'schemaVersion': 1, 'facts': []})
        self.assertNotIn('workTiming', first_entry['amendment']['schedules'][1])
        exact = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        frozen_baseline = exact(baseline)
        for sorted_roundtrip in (False, True):
            for selected in (False, True):
                with self.subTest(sorted_roundtrip=sorted_roundtrip, selected=selected):
                    self.state = (json.loads(json.dumps(baseline, sort_keys=True, ensure_ascii=False))
                                  if sorted_roundtrip else copy.deepcopy(baseline))
                    self.usage = copy.deepcopy(baseline_usage)
                    self.configure()
                    before = copy.deepcopy(self.state)
                    slots = self.prepared()['analysis']['timingOnly']['slots']
                    response = {'slots': [{'slotId': slot['slotId'], 'workTiming': (
                        [{'kind': 'long', 'time': None}] if index == 1 else None if selected else [])}
                        for index, slot in enumerate(slots)]}
                    label = 'next-' + str(sorted_roundtrip) + '-' + str(selected)
                    packet = self.packet(response, label=label)
                    entry = self.entry(packet, label=label)
                    accounted = copy.deepcopy(self.usage)
                    after = self.apply_entry(self.state, entry)
                    self.assertEqual(self.state, before)
                    self.assertEqual(self.usage, accounted)
                    self.assertEqual(timing.core_copy(after['schedules']), timing.core_copy(before['schedules']))
                    self.assertEqual(after['schedules'][0]['workTiming'], before['schedules'][0]['workTiming'])
                    self.assertEqual(after['schedules'][1]['workTiming'], packet['schedules'][1]['workTiming'])
                    for field in ('revisions', 'receipts', 'savedImports'):
                        for key, value in before[field].items():
                            self.assertEqual(exact(after[field][key]), exact(value))
                    basis = facts._project([facts._effective_revision(before, key)
                                            for key in packet['authorization']['basisRevisionKeys']])
                    basis_rows = [basis[facts._pair(row)] for row in before['schedules']]
                    self.assertEqual(facts.timing_hash(basis_rows), packet['authorization']['expectedTimingHash'])
                    if selected:
                        self.assertEqual(exact(after['schedules'][0]), exact(before['schedules'][0]))
                        self.assertEqual(after['sources'], before['sources'])
                        self.assertEqual(after['lastRun'], {'status': 'partial'})
                    facts.validate_state(after)
                    facts.validate_state(json.loads(json.dumps(after, sort_keys=True)))
                    self.assertEqual(self.fx.apply_timing(after, entry), after)
        self.assertEqual(exact(baseline), frozen_baseline)

    def test_selection_does_not_replace_complete_acceptance_or_allow_stale_rebase(self):
        complete, complete_proof = self.native_packet()
        with self.assertRaisesRegex(ValueError, 'timing_selection_requires_partial_sets'):
            timing.to_selected_amendment(complete, complete_proof, self.selection_for(complete),
                                         self.state, self.usage)
        ordinary = timing.to_amendment(complete, complete_proof, self.usage)
        self.assertNotIn('selectionProof', ordinary['amendment'])
        timing.require_complete(complete, complete_proof, self.usage)
        self.state = self.fx.apply_timing(self.state, ordinary)
        self.configure()
        partial = self.packet(self.partial_pattern(), label='next-partial')
        proof = self.accounted_proof(partial, label='next-partial')
        approved = self.selection_for(partial, self.independent_expected_sets())
        entry = self.selected_entry(partial, proof, approved)
        after = self.apply_entry(self.state, entry)
        with self.assertRaisesRegex(ValueError, 'subject_changed'):
            timing.to_selected_amendment(partial, proof, approved, after, self.usage)
        self.assertEqual(self.fx.apply_timing(after, entry), after)

    def test_distinct_contract_copies_core_and_never_reuses_failed_calendar_output(self):
        failed = base.timing_result({5: [base.work_note()], 12: [base.work_note()]})
        failed['periods'][0]['days'][0]['weekday'] = '木'
        frozen = json.dumps(failed, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, 'schedule_calendar_unresolved'):
            azure.normalize_result(failed, self.source, self.text, 1)
        with self.assertRaises(ValueError):
            self.packet(failed)
        self.assertEqual(json.dumps(failed, ensure_ascii=False), frozen)
        packet = self.packet()
        self.assertEqual(packet['analysis']['contract'], 'half-month-timing-v1')
        self.assertEqual(packet['status'], 'ok')
        self.assertEqual(json.dumps(timing.core_copy(packet['schedules']), ensure_ascii=False),
                         json.dumps(timing.core_copy(self.state['schedules']), ensure_ascii=False))
        self.assertEqual([(fact['serviceDate'], fact['qualifier'], fact['explicitTime'])
                          for fact in packet['schedules'][0]['workTiming']['facts']],
                         [('2026-09-05', 'long', None), ('2026-09-12', 'long', None)])
        after, entry = self.apply(packet)
        self.assertEqual(len(after['revisions']), 2)
        self.assertTrue(all(after['revisions'][key] == value for key, value in self.state['revisions'].items()))
        self.assertEqual(self.fx.apply_timing(after, entry), after)
        facts.validate_state(after)
        timing.saved.validate_accounting(after, self.usage, facts)
        self.assertEqual(json.dumps(failed, ensure_ascii=False), frozen)

    def test_prompt_has_no_old_timing_answers_and_slot_ids_are_opaque_not_positions(self):
        prepared = self.prepared()
        context = json.loads(prepared['messages'][1]['content'][0]['text'])
        self.assertNotIn('workTiming', context)
        self.assertNotIn('qualifier', context)
        self.assertNotIn('expectedTimingHash', context)
        self.assertEqual(len(context['slots']), 6)
        self.assertEqual(context['slotFields'], ['slotId', 'serviceDate', 'shift'])
        self.assertTrue(all(timing.SLOT_ID.fullmatch(row[0]) for row in context['slots']))
        self.assertNotIn('2026-09-05', timing.PROMPT)
        self.assertNotIn('2026-09-12', timing.PROMPT)
        self.assertIn('NOT a printed row number, image position', timing.PROMPT)
        self.assertEqual(set(prepared['schema']['properties']), {'slots'})
        self.assertEqual(set(timing.NOTE_SCHEMA['properties']), {'kind', 'time'})

    def test_canonical_day_shift_order_and_observation_metadata_are_not_normalized_again(self):
        original = self.fx.entry()
        original['amendment']['schedules'][0]['days'].reverse()
        original['amendment']['schedules'][0]['days'][0]['shifts'] = ['夜', '昼']
        self.state = self.fx.apply(None, [original])
        self.state['schedules'][0] = dict(reversed(list(self.state['schedules'][0].items())))
        facts.validate_state(self.state)
        self.configure()
        before = json.dumps(timing.core_copy(self.state['schedules']), ensure_ascii=False)
        packet = self.packet(self.response({('2026-09-14', '昼'): [{'kind': 'long', 'time': None}]}))
        self.assertEqual(json.dumps(timing.core_copy(packet['schedules']), ensure_ascii=False), before)
        after, _ = self.apply(packet)
        self.assertEqual(json.dumps(timing.core_copy(after['schedules']), ensure_ascii=False), before)
        self.assertEqual(after['schedules'][0]['observedAt'], self.source['observedAt'])

    def test_authorization_and_source_changes_fail_before_reserve_or_model(self):
        response = self.response()
        for change in ('subject', 'core', 'timing', 'basis', 'import', 'receipt', 'contract', 'contractHash',
                       'author', 'body', 'image', 'source', 'observation', 'scopeDate', 'scopeShift',
                       'boundary', 'channel'):
            state, approval, source, text, images = (
                copy.deepcopy(self.state), copy.deepcopy(self.approval), copy.deepcopy(self.source),
                self.text, copy.deepcopy(self.images))
            if change in ('subject', 'core', 'timing'):
                approval[{'subject': 'expectedSubjectHash', 'core': 'expectedCoreHash',
                          'timing': 'expectedTimingHash'}[change]] = 'f' * 64
            elif change == 'basis':
                approval['basisRevisionKeys'] = ['f' * 64]
            elif change in ('import', 'receipt', 'contractHash'):
                approval['previous'][{'import': 'importId', 'receipt': 'analysisReceiptId',
                                      'contractHash': 'contractHash'}[change]] = 'f' * 64
            elif change == 'contract':
                approval['previous']['contractVersion'] = facts.VERSION
            elif change == 'author':
                source['authorId'] = '12345'
            elif change == 'body':
                text += ' changed'
                source['bodyHash'] = facts.digest(text.encode())
            elif change == 'image':
                images[0]['bytes'] = base.png(width=10)
            elif change == 'source':
                source = base.source(suffix=2)[0]
            elif change == 'observation':
                source['observedAt'] = facts.stamp(NOW)
            elif change.startswith('scope'):
                approval['targetScopes'][0]['serviceDate' if change == 'scopeDate' else 'shift'] = (
                    '2026-09-03' if change == 'scopeDate' else '昼')
            elif change == 'boundary':
                approval['targetScopes'][0]['boundary'] = 'end'
            else:
                approval['updateChannels'] = ['days']
            analyzer, usage, client = self.analyzer(response)
            issued = mock.Mock()
            with self.subTest(change=change), self.assertRaises(ValueError):
                analyzer.analyze(state, approval, source, text, images, issued)
            usage.reserve.assert_not_called()
            usage.issued.assert_not_called()
            issued.assert_not_called()
            client.structured.assert_not_called()

    def test_full_slot_coverage_and_typed_bounds_reject_unknown_missing_duplicate_and_core_fields(self):
        for change in ('unknown', 'duplicate', 'missing', 'empty', 'date', 'shift', 'boundary', 'wrongShift', 'third'):
            value = self.response()
            if change == 'unknown':
                value['slots'][0]['slotId'] = 's0000000000'
            elif change == 'duplicate':
                value['slots'][1] = copy.deepcopy(value['slots'][0])
            elif change == 'missing':
                value['slots'].pop()
            elif change == 'empty':
                value['slots'] = []
            elif change in ('date', 'shift'):
                value['slots'][0][change] = 'not allowed'
            elif change == 'boundary':
                value['slots'][1]['workTiming'][0]['boundary'] = 'start'
            elif change == 'wrongShift':
                value['slots'][0]['workTiming'] = [{'kind': 'long', 'time': None}]
            else:
                value['slots'][1]['workTiming'] *= 3
            before = copy.deepcopy(self.state)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.packet(value)
            self.assertEqual(self.state, before)

    def test_authorization_requires_all_six_core_slots_before_issue_and_at_apply(self):
        packet = self.packet()
        entry = self.entry(packet)
        original = copy.deepcopy(self.state)
        for change in ('one', 'omitted', 'extra'):
            approval = copy.deepcopy(self.approval)
            if change == 'one':
                approval['targetScopes'] = approval['targetScopes'][:1]
            elif change == 'omitted':
                approval['targetScopes'].pop()
            else:
                approval['targetScopes'].append({
                    'name': self.source['name'], 'serviceDate': '2026-09-03', 'shift': '昼', 'boundary': 'end'})
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, 'timing_scope_mismatch'):
                    timing.slots_for(timing.core_copy(self.state['schedules']), approval)
                analyzer, usage, client = self.analyzer(self.response())
                issued = mock.Mock()
                with self.assertRaises(ValueError):
                    analyzer.analyze(self.state, approval, self.source, self.text, self.images, issued)
                usage.reserve.assert_not_called()
                usage.issued.assert_not_called()
                issued.assert_not_called()
                client.structured.assert_not_called()
                forged = copy.deepcopy(packet)
                forged['authorization'] = approval
                forged['analysis']['timingOnly']['authorizationHash'] = facts.digest(approval)
                with self.assertRaisesRegex(ValueError, 'timing_scope_mismatch'):
                    timing._validate_packet(forged)
                changed = copy.deepcopy(entry)
                changed['amendment']['targetScopes'] = approval['targetScopes']
                with self.assertRaises(ValueError):
                    self.fx.apply_timing(self.state, changed)
                self.assertEqual(self.state, original)
        self.assertEqual(len(timing.slots_for(timing.core_copy(self.state['schedules']), self.approval)), 6)

    def test_semantic_result_fingerprint_rejects_metadata_claim_and_raw_hash_changes(self):
        raw = self.response()
        raw['slots'][0]['workTiming'] = None
        packet = self.packet(raw)
        entry = self.entry(packet)
        proof = entry['amendment']['proof']
        timing.validate_packet(packet, proof, self.usage)
        frozen = copy.deepcopy((packet, self.state, self.usage, raw))
        for change in ('promote', 'pending', 'status', 'claims', 'rawHash', 'semanticHash'):
            bad = copy.deepcopy(packet)
            if change == 'promote':
                bad['pendingSlotIds'] = []
                bad['analysis']['timingOnly']['pendingSlotIds'] = []
                bad['status'] = 'ok'
            elif change == 'pending':
                bad['pendingSlotIds'] = []
            elif change == 'status':
                bad['status'] = 'ok'
            elif change == 'claims':
                bad['schedules'][0]['workTiming']['facts'][0]['qualifier'] = 'short'
            elif change == 'rawHash':
                bad['analysis']['resultHash'] = 'f' * 64
            else:
                bad['analysis']['timingOnly']['semanticResultHash'] = 'f' * 64
            with self.subTest(change=change):
                for validate in (timing.validate_packet, timing.require_complete, timing.to_amendment):
                    with self.assertRaises(ValueError):
                        validate(bad, proof, self.usage)
                changed = copy.deepcopy(entry)
                changed['amendment'].update(analysis=bad['analysis'], schedules=bad['schedules'])
                if change in ('pending', 'status'):
                    # Packet-only presentation fields are never an accepted amendment channel.
                    changed['amendment'][change] = bad['status'] if change == 'status' else []
                with self.assertRaises(ValueError):
                    self.fx.apply_timing(self.state, changed)
        self.assertEqual((packet, self.state, self.usage, raw), frozen)
        after = self.apply_entry(self.state, entry)
        self.assertEqual(after['lastRun']['status'], 'partial')
        self.assertEqual(after['schedules'][0]['days'], self.state['schedules'][0]['days'])

    def test_rehashing_forged_semantics_cannot_replace_native_result_attestation(self):
        raw = self.response()
        raw['slots'][0]['workTiming'] = None
        packet, proof = self.native_packet(raw)
        entry = self.selected_entry(packet, proof, self.selection_for(packet))
        original = copy.deepcopy((self.state, self.usage))
        for change in ('promote', 'claims', 'rawHash'):
            bad, changed_proof = copy.deepcopy((packet, proof))
            if change == 'promote':
                bad['pendingSlotIds'] = []
                bad['analysis']['timingOnly']['pendingSlotIds'] = []
                bad['status'] = 'ok'
            elif change == 'claims':
                bad['schedules'][0]['workTiming']['facts'][0]['qualifier'] = 'short'
            else:
                bad['analysis']['resultHash'] = 'f' * 64
            bad['analysis']['timingOnly']['semanticResultHash'] = timing.semantic_result_hash(
                bad['analysis'], bad['schedules'])
            changed_proof['analysisResultHash'] = bad['analysis']['resultHash']
            changed_proof['analysisReceiptHash'] = facts.digest(bad['analysis'])
            timing._validate_packet(bad)
            with self.subTest(change=change):
                for validate in (timing.validate_packet, timing.require_complete, timing.to_amendment):
                    with self.assertRaisesRegex(ValueError, 'result_attestation'):
                        validate(bad, changed_proof, self.usage)
                changed = copy.deepcopy(entry)
                changed['amendment'].update(analysis=bad['analysis'], schedules=bad['schedules'], proof=changed_proof)
                with self.assertRaisesRegex(ValueError, 'result_attestation'):
                    self.fx.apply_timing(self.state, changed)
        self.assertEqual((self.state, self.usage), original)
        after = self.apply_entry(self.state, entry)
        self.assertEqual(after['lastRun']['status'], 'partial')
        self.assertEqual(self.fx.apply_timing(after, entry), after)
        with self.assertRaisesRegex(ValueError, 'timing_response_pending'):
            timing.require_complete(packet, proof, self.usage)

    def test_native_proof_rejects_wrong_receipt_phase_identity_consumption_and_unknown_import(self):
        packet, proof = self.native_packet()
        entry = timing.to_amendment(packet, proof, self.usage)
        original = copy.deepcopy(self.state)
        for change in ('receipt', 'reserved', 'unfinished', 'failed', 'model', 'version', 'deployment',
                       'request', 'attestation', 'consumed', 'wholeLedgerHash', 'unknownImport', 'missingKind'):
            usage, changed = copy.deepcopy((self.usage, entry))
            bound = changed['amendment']['proof']
            receipt = usage['receipts'][packet['analysis']['receiptId']]
            if change == 'receipt':
                bound['usageReceiptId'] = 'f' * 64
            elif change in ('reserved', 'unfinished', 'failed'):
                receipt.pop('resultAttestation')
                receipt['completedAt'] = None if change != 'failed' else receipt['completedAt']
                receipt['reason'] = 'azure_invalid_output' if change == 'failed' else 'azure_interrupted'
                if change == 'reserved':
                    receipt['issuedAt'] = None
            elif change in ('model', 'version', 'deployment'):
                receipt['identity']['modelVersion' if change == 'version' else change] = 'wrong-model'
            elif change == 'request':
                receipt['requestHash'] = 'f' * 64
            elif change == 'attestation':
                receipt['resultAttestation']['semanticResultHash'] = 'f' * 64
            elif change == 'consumed':
                receipt['resultAttestation']['consumed'] = 2
            elif change == 'wholeLedgerHash':
                bound['usageSourceHash'] = facts.digest(usage)
            elif change == 'unknownImport':
                bound['accountingKind'] = 'imported'
                bound['usageReceiptId'] = 'f' * 64
            else:
                del bound['accountingKind']
            if change in ('reserved', 'unfinished', 'failed', 'model', 'version', 'deployment',
                          'request', 'attestation', 'consumed'):
                bound['usageSourceHash'] = facts.digest(receipt)
            before = copy.deepcopy(usage)
            self.fx.usage = usage
            with self.subTest(change=change):
                for validate in (timing.validate_packet, timing.require_complete, timing.to_amendment):
                    with self.assertRaises(ValueError):
                        validate(packet, bound, usage)
                with self.assertRaises(ValueError):
                    self.fx.apply_timing(self.state, changed)
                self.assertEqual(usage, before)
                self.assertEqual(self.state, original)
        self.fx.usage = self.usage

    def test_external_import_cannot_double_count_native_result_and_native_chain_remains_usable(self):
        packet, proof = self.native_packet()
        before = copy.deepcopy(self.usage)
        with self.assertRaisesRegex(ValueError, 'duplicate_ai_usage_result'):
            self.entry(packet, 'external-duplicate-of-native')
        self.assertEqual(self.usage, before)
        entry = timing.to_amendment(packet, proof, self.usage)
        self.state = self.fx.apply_timing(self.state, entry)
        self.configure()
        prepared = self.prepared()
        self.assertEqual(len(prepared['analysis']['timingOnly']['slots']), 6)
        self.assertEqual(self.approval['previous']['analysisReceiptId'], packet['analysis']['receiptId'])

    def test_pending_is_explicit_non_destructive_and_not_fixed_live_complete(self):
        initial_usage = copy.deepcopy(self.usage)
        pending = self.packet({'slots': None})
        pending_entry = self.entry(pending)
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(len(pending['pendingSlotIds']), 6)
        with self.assertRaisesRegex(ValueError, 'timing_response_pending'):
            timing.require_complete(pending, pending_entry['amendment']['proof'], self.usage)
        after = self.fx.apply_timing(self.state, pending_entry)
        self.assertEqual(after, self.state)
        self.usage = copy.deepcopy(initial_usage)
        empty = self.packet(self.response({}), label='empty')
        empty_entry = self.entry(empty, 'empty')
        self.assertEqual(empty['status'], 'no-new')
        self.assertIs(timing.require_complete(empty, empty_entry['amendment']['proof'], self.usage), empty)
        self.usage = copy.deepcopy(initial_usage)
        value = self.response()
        value['slots'][0]['workTiming'] = None
        partial = self.packet(value, label='partial')
        self.assertEqual(partial['status'], 'partial')
        after, entry = self.apply(partial, label='partial')
        self.assertEqual(after['lastRun']['status'], 'partial')
        self.assertEqual(after['lastSuccessAt'], self.state['lastSuccessAt'])
        self.assertEqual(timing.core_copy(after['schedules']), timing.core_copy(self.state['schedules']))
        with self.assertRaises(ValueError):
            timing.require_complete(partial, entry['amendment']['proof'], self.usage)

    def test_timing_contract_cannot_be_attached_to_initial_or_broad_update_or_changed_core(self):
        packet = self.packet()
        with self.assertRaisesRegex(ValueError, 'authorization_required'):
            facts.apply_revision(facts.empty_state(), packet['schedules'], self.source, packet['analysis'])
        entry = self.entry(packet)
        broad = copy.deepcopy(entry)
        for key in facts.TIMING_AMENDMENT_FIELDS:
            del broad['amendment'][key]
        with self.assertRaisesRegex(ValueError, 'authorization_required'):
            self.fx.apply_timing(self.state, broad)
        for change in ('core', 'binding', 'scope', 'pending'):
            bad = copy.deepcopy(entry)
            if change == 'core':
                bad['amendment']['schedules'][0]['days'].reverse()
            elif change == 'binding':
                bad['amendment']['analysis']['timingOnly']['sourceHash'] = 'f' * 64
            elif change == 'scope':
                bad['amendment']['analysis']['timingOnly']['slots'][0]['serviceDate'] = '2026-09-03'
            else:
                info = bad['amendment']['analysis']['timingOnly']
                info['pendingSlotIds'] = [info['slots'][1]['slotId']]
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.fx.apply_timing(self.state, bad)

    def test_targeted_denial_keeps_late_and_old_exclusions_across_new_same_post_revision(self):
        first, _ = self.apply(self.packet(self.response({
            ('2026-09-10', '夜'): [{'kind': 'late', 'time': '18:00'}]})))
        self.state = first
        self.configure()
        second = self.packet(self.response({
            ('2026-09-10', '夜'): [{'kind': 'not-early', 'time': None},
                                {'kind': 'not-time', 'time': '16:00'}]}), label='denial')
        after, _ = self.apply(second, label='denial')
        notes = after['schedules'][0]['workTiming']['facts']
        self.assertEqual([(note['status'], note['qualifier'], note['explicitTime']) for note in notes],
                         [('set', 'late', '18:00'), ('excluded', 'early', None),
                          ('excluded', None, '16:00')])
        self.assertEqual(len(facts.select_revisions(after)), 1)
        self.assertEqual(len(after['revisions']), 3)

    def test_capacity_profile_and_large_input_hold_before_reserve(self):
        for stale in (False, True):
            state = copy.deepcopy(self.state)
            source, text = copy.deepcopy(self.source), self.text
            if not stale:
                # A different caption must fail source verification, not be granted canonical core reuse.
                text = 'x' * 6000
                source['bodyHash'] = facts.digest(text.encode())
            analyzer, usage, client = self.analyzer(self.response())
            patch = {'promptHash': 'f' * 64} if stale else {}
            with mock.patch.dict(timing.azure.capacity.HALF_MONTH_TIMING, patch), self.assertRaises(ValueError):
                analyzer.analyze(state, self.approval, source, text, self.images, mock.Mock())
            usage.reserve.assert_not_called()
            client.structured.assert_not_called()

    def test_known_6000_byte_same_source_is_capacity_held_with_zero_reservation(self):
        self.seed_source(caption='x' * 6000, photos=4)
        prepared = timing.build_request(
            self.state, self.approval, self.source, self.text, self.images, self.usage)
        self.assertEqual(prepared['core'], timing.core_copy(self.state['schedules']))
        analyzer, usage, client = self.analyzer({'slots': None})
        before = copy.deepcopy(self.state)
        with self.assertRaisesRegex(timing.azure.capacity.CapacityHold, '^azure_capacity_hold$'):
            analyzer.analyze(self.state, self.approval, self.source, self.text, self.images, mock.Mock())
        usage.reserve.assert_not_called()
        usage.issued.assert_not_called()
        client.structured.assert_not_called()
        self.assertEqual(self.state, before)

    def test_changed_image_order_and_missing_canonical_usage_reject_before_issue(self):
        self.seed_source(photos=2)
        response = self.response()
        analyzer, usage, client = self.analyzer(response)
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            analyzer.analyze(self.state, self.approval, self.source, self.text,
                             list(reversed(self.images)), mock.Mock())
        usage.reserve.assert_not_called()
        client.structured.assert_not_called()
        usage.state = timing.ledger.empty_state()
        with self.assertRaisesRegex(ValueError, 'usage_missing'):
            analyzer.analyze(self.state, self.approval, self.source, self.text, self.images, mock.Mock())
        usage.reserve.assert_not_called()
        client.structured.assert_not_called()

    def test_maximum_64_slots_64_claims_and_two_period_order_survive_import(self):
        raw = base.maximum_timing_result()
        for period in raw['periods']:
            for row in period['days']:
                del row['workTiming']
        self.seed_source(photos=4, raw=raw)
        prepared = self.prepared()
        slots = prepared['analysis']['timingOnly']['slots']
        self.assertEqual(len(slots), 64)
        self.assertEqual(timing.wire_payload(prepared['messages'], prepared['schema'])['max_completion_tokens'], 3584)
        value = {'slots': [{'slotId': slot['slotId'],
                            'workTiming': [{'kind': 'not-time', 'time': '18:00'}]} for slot in slots]}
        packet = self.packet(value)
        self.assertEqual(sum(len(table['workTiming']['facts']) for table in packet['schedules']), 64)
        self.assertEqual(sum(len(table['days']) for table in packet['schedules']), 32)
        self.assertLess(len(json.dumps({'choices': [{'message': {
            'content': json.dumps(value, indent=2)}}]}).encode()), azure.transport.MAX_RESPONSE_BYTES)
        after, _ = self.apply(packet)
        after['revisions'] = dict(reversed(list(after['revisions'].items())))
        facts.validate_state(after)
        self.assertEqual(timing.core_copy(after['schedules']), timing.core_copy(self.state['schedules']))
        value['slots'][0]['workTiming'].append({'kind': 'not-time', 'time': '17:00'})
        with self.assertRaisesRegex(ValueError, 'note_limit'):
            self.packet(value)

    def test_pending_slot_keeps_its_previous_claim_while_other_slot_updates(self):
        self.state, _ = self.apply(self.packet())
        self.configure()
        value = self.response({('2026-09-10', '夜'): [{'kind': 'early', 'time': '16:00'}]})
        slots = self.prepared()['analysis']['timingOnly']['slots']
        pending_id = next(slot['slotId'] for slot in slots if slot['serviceDate'] == '2026-09-05')
        next(row for row in value['slots'] if row['slotId'] == pending_id)['workTiming'] = None
        packet = self.packet(value, label='partial-keep')
        after, _ = self.apply(packet, label='partial-keep')
        notes = {fact['serviceDate']: fact for fact in after['schedules'][0]['workTiming']['facts']}
        self.assertEqual(notes['2026-09-05']['qualifier'], 'long')
        self.assertEqual(notes['2026-09-12']['qualifier'], 'long')
        self.assertEqual(notes['2026-09-10']['qualifier'], 'early')
        self.assertEqual(after['lastRun']['status'], 'partial')

    def test_old_timing_values_are_not_sent_back_as_model_answers(self):
        original = self.fx.entry()
        source = original['amendment']['source']
        original['amendment']['schedules'][0]['workTiming'] = facts.timing().bind([{
            'serviceDate': '2026-09-05', 'shift': '昼', 'boundary': 'end', 'status': 'set',
            'qualifier': None, 'explicitTime': '23:47'}], source, 'half-month-schedule')
        self.state = self.fx.apply(None, [original])
        self.configure()
        context = self.prepared()['messages'][1]['content'][0]['text']
        self.assertNotIn('23:47', context)
        self.assertNotIn('workTiming', context)

    def test_invalid_model_output_is_accounted_as_invalid_output_and_not_applied(self):
        analyzer, usage, client = self.analyzer({'periods': []})
        before = copy.deepcopy(self.state)
        with self.assertRaisesRegex(azure.AnalysisFailure, 'azure_invalid_output'):
            analyzer.analyze(self.state, self.approval, self.source, self.text, self.images, mock.Mock())
        usage.reserve.assert_called_once()
        usage.finish.assert_called_once_with(usage.reserve.call_args.args[0], 'azure_invalid_output')
        self.assertEqual(self.state, before)

    def test_actual_shared_usage_with_mock_http_then_native_apply_without_import_or_charge(self):
        folder = self.work_dir()
        path = folder / 'timing-usage.json'
        timing.ledger.atomic_json(path, self.usage)
        raw = self.response()
        http = mock.MagicMock()
        http.getcode.return_value = 200
        http.read.return_value = json.dumps({'model': facts.MODEL, 'choices': [{
            'finish_reason': 'stop', 'message': {'content': json.dumps(raw)}}]}).encode()
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = http
        initial_imports = copy.deepcopy(self.usage['imports'])
        with timing.ledger.SharedUsage(
                path, run_id='timing-only-1', component='schedule', clock=lambda: NOW,
                sleep=lambda _: self.fail('no waiting permitted in offline test')) as usage:
            client = azure.transport.AzureOpenAI({
                'AZURE_OPENAI_ENDPOINT': IDENTITY['endpoint'], 'AZURE_OPENAI_API_KEY': 'offline-test-key'},
                on_http_failure=usage.http_failure, opener=opener)
            analyzer = timing.AzureAnalyzer(usage, clock=lambda: NOW, client=client)
            issued = mock.Mock()
            packet = analyzer.analyze(self.state, self.approval, self.source, self.text, self.images, issued)
            opener.open.assert_called_once()
            self.assertEqual(facts.digest(opener.open.call_args.args[0].data), packet['analysis']['requestHash'])
            issued.assert_called_once_with(packet['analysis']['requestHash'])
            native = usage.state['receipts'][packet['analysis']['receiptId']]
            self.assertEqual(native['reason'], 'events')
            self.assertEqual(native['resultAttestation'], timing.result_attestation(packet))
            self.assertEqual(timing.ledger.usage_counts(usage.state, 'timing-only-1', NOW)['run'], 1)
            self.assertEqual(self.usage['receipts'], {})
            proof = timing.native_proof(packet, usage.state)
            timing.require_complete(packet, proof, usage.state)
            entry = timing.to_amendment(packet, proof, usage.state)
            self.fx.usage = usage.state
            before = copy.deepcopy(usage.state)
            after = self.fx.apply_timing(self.state, entry)
            self.assertEqual(self.fx.apply_timing(after, entry), after)
            self.assertEqual(usage.state, before)
            self.assertEqual(usage.state['imports'], initial_imports)
            self.assertEqual(len(usage.state['receipts']), 1)
            self.assertEqual(after['savedImports'][facts.digest(entry['amendment'])]['accountingKind'], 'native')
            # Unrelated accounting growth must not invalidate this stable native receipt proof.
            unrelated = copy.deepcopy(next(iter(initial_imports.values())))
            unrelated.update(receiptId='1' * 64, sourceHash='2' * 64)
            timing.ledger.apply_import(usage.state, unrelated)
            timing.validate_packet(packet, proof, usage.state)
            self.assertEqual(self.fx.apply_timing(after, entry), after)
            usage._save()
        self.usage = timing.ledger.load_state(path, required=True)
        facts.validate_state(after)
        timing.saved.validate_accounting(after, self.usage, facts)
        self.assertEqual(len(after['revisions']), 2)

    def test_state_cas_change_before_issue_or_after_response_is_not_rebased(self):
        for phase in ('before', 'after'):
            state = copy.deepcopy(self.state)
            analyzer, usage, client = self.analyzer(self.response())
            def mutate(*_):
                state['checkedAt'] = facts.stamp(NOW)
            issued = mock.Mock(side_effect=mutate if phase == 'before' else None)
            if phase == 'after':
                def result(*args, **kwargs):
                    mutate()
                    return self.response()
                client.structured.side_effect = result
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, 'subject_changed'):
                analyzer.analyze(state, self.approval, self.source, self.text, self.images, issued)
            usage.reserve.assert_called_once()
            usage.finish.assert_called_once()
            if phase == 'before':
                usage.issued.assert_not_called()
                client.structured.assert_not_called()
            self.assertEqual(state['schedules'], self.state['schedules'])


if __name__ == '__main__':
    unittest.main()
