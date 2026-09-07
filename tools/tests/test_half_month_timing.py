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

    def entry(self, packet, label='timing-only'):
        receipt_id, source_hash = facts.digest(('usage:' + label).encode()), facts.digest(('source:' + label).encode())
        receipt = {'receiptId': receipt_id, 'sourceHash': source_hash, 'date': '2026-09-07',
                   'counts': {'requests': 1}, 'modelBreakdown': [{
                       'model': facts.MODEL, 'deployment': facts.MODEL, 'modelVersion': facts.MODEL_VERSION,
                       'component': 'schedule', 'kind': 'image', 'count': 1}],
                   'resultAttestation': timing.result_attestation(packet)}
        timing.ledger.apply_import(self.usage, receipt)
        self.fx.usage = self.usage
        proof = timing.imported_proof(
            packet, self.usage, receipt_id=receipt_id, issued_at=packet['analysis']['analyzedAt'],
            source_manifest_hash=facts.digest(b'new-authorized-source-manifest'))
        return timing.to_amendment(packet, proof, self.usage)

    def apply(self, packet, label='timing-only'):
        entry = self.entry(packet, label)
        self.fx.usage = self.usage
        return self.fx.apply_timing(self.state, entry), entry

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
        after = self.fx.apply_timing(self.state, entry)
        self.assertEqual(after['lastRun']['status'], 'partial')
        self.assertEqual(after['schedules'][0]['days'], self.state['schedules'][0]['days'])

    def test_rehashing_forged_semantics_cannot_replace_native_result_attestation(self):
        raw = self.response()
        raw['slots'][0]['workTiming'] = None
        packet, proof = self.native_packet(raw)
        entry = timing.to_amendment(packet, proof, self.usage)
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
        after = self.fx.apply_timing(self.state, entry)
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
