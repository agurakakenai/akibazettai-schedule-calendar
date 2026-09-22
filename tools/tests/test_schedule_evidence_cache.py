"""Private-cache regressions with generated evidence and ephemeral test keys only."""
import base64
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import test_schedule_reading as fixture


cache = fixture.load('evidence_cache_test', 'schedule-evidence-cache.py')
restore = fixture.load('evidence_restore_test', 'restore-schedule-cache.py')
reading = fixture.reading
NOW = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc)
KEY = base64.b64encode(bytes(range(32))).decode('ascii')
IDENTITY = hashlib.sha256(b'synthetic-source').hexdigest()


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='schedule-cache-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / '_private-evidence' / 'cache.bin'
        self.archive = self.root / 'schedule-evidence.zip'
        self.now = NOW
        self.images = [fixture.image()]
        self.source = {'url': 'https://example.invalid/PRIVATE-URL', 'verified': True}
        self.text = 'PRIVATE-BODY-SYNTHETIC'
        self.cache = cache.EvidenceCache(self.path, KEY, clock=lambda: self.now)
        patch = mock.patch('socket.socket', side_effect=AssertionError('network forbidden'))
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, **kwargs):
        self.cache.put(IDENTITY, self.source, self.text, self.images, **kwargs)

    def load(self, key=KEY):
        return cache.EvidenceCache(self.path, key, clock=lambda: self.now)

    def zip(self, blob=None, *, name='cache.bin', mode=stat.S_IFREG | 0o600):
        if blob is None:
            blob = self.path.read_bytes()
        with zipfile.ZipFile(self.archive, 'w', zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, blob)
        return self.archive

    def encrypted(self, records):
        nonce = bytes(range(12))
        return nonce + self.cache.cipher.encrypt(nonce, json.dumps(records).encode(), cache.AAD)

    def test_missing_is_only_none_and_key_validation_never_logs_key(self):
        self.assertIsNone(self.cache.get('0' * 64))
        for key in (None, '', ' ', 'NOT-BASE64', base64.b64encode(b'short').decode(), 42):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'evidence_cache_key'):
                self.load(key)
        for identity in ('', 'x' * 64, None, 12):
            with self.assertRaisesRegex(ValueError, 'invalid_evidence_key'):
                self.cache.get(identity)

    def test_cross_run_originals_and_exact_variants_restore_reuse(self):
        first = reading.initial_reading(self.text, self.images)
        partial = fixture.result([.7, .8, 1, 1])
        variants = reading.views(self.images, partial)
        self.put(reading=partial, variants=variants)
        blob = self.path.read_bytes()
        for private in (b'PRIVATE-BODY', b'PRIVATE-URL', self.images[0]['bytes'], variants[0]['bytes']):
            self.assertNotIn(private, blob)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['cache.bin'])
        target = self.root / 'second-run' / '_private-evidence' / 'cache.bin'
        summary = restore.restore(self.zip(), target=target, key=KEY, clock=lambda: self.now)
        self.assertEqual(set(summary), {'records', 'bytes', 'expiredRecords', 'status'})
        self.assertEqual((summary['status'], summary['expiredRecords']), ('restored', 0))
        second = cache.EvidenceCache(target, KEY, clock=lambda: self.now)
        value = second.get(IDENTITY)
        self.assertEqual(value['source'], self.source)
        self.assertEqual(value['images'], self.images)
        self.assertEqual(value['variants'], variants)
        packs = reading.pack_attachments(value['text'], value['variants'])
        self.assertEqual(packs[0]['images'], variants)
        self.assertEqual(variants, reading.views(value['images'], value['reading']))
        self.assertEqual(variants[0]['originalHash'], first[0]['images'][0]['variantHash'])
        second.put(IDENTITY, value['source'], value['text'], value['images'],
                   reading=value['reading'], seen=[v['variantId'] for v in value['variants']])
        third = cache.EvidenceCache(target, KEY, clock=lambda: self.now).get(IDENTITY)
        self.assertEqual(third['variants'], variants)
        self.assertEqual(reading.reread(third['text'], third['images'], third['reading'],
                                       seen=third['seen']), [])

    def test_seven_day_expiry_no_sliding_renewal(self):
        self.put()
        expiry = self.cache.records[IDENTITY]['expiresAt']
        self.now += dt.timedelta(days=6, hours=23, minutes=59)
        self.put()
        self.assertEqual(self.cache.records[IDENTITY]['expiresAt'], expiry)
        self.assertIsNotNone(self.load().get(IDENTITY))
        self.now = NOW + cache.TTL
        with self.assertRaisesRegex(ValueError, 'evidence_cache_expired'):
            self.load().get(IDENTITY)
        with self.assertRaisesRegex(ValueError, 'evidence_cache_expired'):
            self.put()
        summary = restore.restore(self.zip(), target=self.path, key=KEY, clock=lambda: self.now)
        self.assertEqual((summary['records'], summary['expiredRecords'], summary['status']), (0, 1, 'all_expired'))
        restored = self.load()
        self.assertEqual(restored.records[IDENTITY]['expiresAt'], expiry)
        self.assertEqual(restored.records[IDENTITY]['value']['images'], [])
        with self.assertRaisesRegex(ValueError, 'evidence_cache_expired'):
            restored.get(IDENTITY)
        with self.assertRaisesRegex(ValueError, 'evidence_cache_expired'):
            restored.put(IDENTITY, self.source, self.text, self.images)
        self.now += dt.timedelta(days=1)
        markers = copy.deepcopy(restored.records)
        summary = restore.restore(self.zip(), target=self.path, key=KEY, clock=lambda: self.now)
        self.assertEqual(summary['status'], 'all_expired')
        self.assertEqual(self.load().records, markers)

    def test_restore_mixed_expiry_boundary_preserves_live_variants_and_expired_reason(self):
        self.source.update(id='synthetic-expired-id', authorId='synthetic-author')
        self.put()
        old_expiry = self.cache.records[IDENTITY]['expiresAt']
        self.now += dt.timedelta(days=1)
        fresh_key = 'f' * 64
        variants = reading.views(self.images, fixture.result([0, 0, .2, .2]))
        self.cache.put(fresh_key, {'id': 'synthetic-live-id', 'authorId': 'synthetic-author'},
                       'SYNTHETIC-LIVE', self.images, variants=variants,
                       seen=[variants[0]['variantId']])
        fresh_record = copy.deepcopy(self.cache.records[fresh_key])
        original = self.path.read_bytes()
        self.zip()
        target = self.root / 'restored' / '_private-evidence' / 'cache.bin'
        self.now = NOW + cache.TTL - dt.timedelta(microseconds=1)
        before = restore.restore(self.archive, target=target, key=KEY, clock=lambda: self.now)
        self.assertEqual(before, {'records': 2, 'bytes': len(original), 'expiredRecords': 0, 'status': 'restored'})
        self.assertEqual(target.read_bytes(), original)

        self.now += dt.timedelta(microseconds=1)
        summary = restore.restore(self.archive, target=target, key=KEY, clock=lambda: self.now)
        self.assertEqual(summary, {'records': 1, 'expiredRecords': 1, 'bytes': target.stat().st_size,
                                   'status': 'partially_expired'})
        restored = cache.EvidenceCache(target, KEY, clock=lambda: self.now)
        self.assertEqual(restored.records[fresh_key], fresh_record)
        self.assertEqual(restored.get(fresh_key)['variants'], variants)
        self.assertEqual(restored.get(fresh_key)['seen'], [variants[0]['variantId']])
        self.assertEqual(restored.records[IDENTITY], {'expiresAt': old_expiry, 'value': {
            'source': {'id': 'synthetic-expired-id', 'authorId': 'synthetic-author'},
            'text': '', 'images': [], 'reading': None, 'variants': [], 'seen': []}})
        self.assertEqual(restored.find_source('synthetic-expired-id', 'synthetic-author'), [IDENTITY])
        with self.assertRaisesRegex(ValueError, 'evidence_cache_expired'):
            restored.get(IDENTITY)
        with self.assertRaisesRegex(ValueError, 'evidence_cache_expired'):
            restored.put(IDENTITY, self.source, self.text, self.images)
        self.assertIsNone(restored.get('0' * 64))
        self.assertNotIn(self.text, cache._json(restored.records).decode())
        self.assertNotIn(self.source['url'], cache._json(restored.records).decode())

        self.zip(target.read_bytes())
        again = restore.restore(self.archive, target=target, key=KEY, clock=lambda: self.now)
        self.assertEqual(again['expiredRecords'], 1)
        self.assertEqual(cache.EvidenceCache(target, KEY, clock=lambda: self.now).records,
                         restored.records)
        self.now += dt.timedelta(days=1)
        summary = restore.restore(self.archive, target=target, key=KEY, clock=lambda: self.now)
        self.assertEqual((summary['status'], summary['records'], summary['expiredRecords']), ('all_expired', 0, 2))
        expired_cache = cache.EvidenceCache(target, KEY, clock=lambda: self.now)
        for identity, expected in ((IDENTITY, old_expiry), (fresh_key, fresh_record['expiresAt'])):
            self.assertEqual(expired_cache.records[identity]['expiresAt'], expected)
            self.assertEqual(expired_cache.records[identity]['value']['images'], [])
            self.assertEqual(expired_cache.records[identity]['value']['variants'], [])
            with self.assertRaisesRegex(ValueError, 'evidence_cache_expired'):
                expired_cache.get(identity)

    def test_mixed_restore_authentication_validation_and_atomic_failure_are_failclosed(self):
        self.put()
        self.now += dt.timedelta(days=1)
        self.cache.put('f' * 64, self.source, 'live', self.images)
        self.now = NOW + cache.TTL
        before = self.path.read_bytes()
        records = copy.deepcopy(self.cache.records)
        variants = [
            (before[:-1] + bytes([before[-1] ^ 1]), KEY, 'authentication_failed'),
            (before, base64.b64encode(b'z' * 32).decode(), 'authentication_failed')]
        records[IDENTITY]['value']['images'][0]['sha256'] = '0' * 64
        variants.append((self.encrypted(records), KEY, 'image_hash_mismatch'))
        for blob, key, reason in variants:
            self.zip(blob)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                restore.restore(self.archive, target=self.path, key=key, clock=lambda: self.now)
            self.assertEqual(self.path.read_bytes(), before)
        self.zip(before)
        with mock.patch.object(restore.cache.os, 'replace', side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError):
                restore.restore(self.archive, target=self.path, key=KEY, clock=lambda: self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([path.name for path in self.path.parent.iterdir()], ['cache.bin'])

    def test_all_expired_still_authenticates_and_validates_before_replacement(self):
        self.put()
        self.now += cache.TTL
        before = self.path.read_bytes()
        records = copy.deepcopy(self.cache.records)
        records[IDENTITY]['value']['images'][0]['sha256'] = '0' * 64
        for blob, key, reason in (
                (before, base64.b64encode(b'z' * 32).decode(), 'authentication_failed'),
                (before[:-1] + bytes([before[-1] ^ 1]), KEY, 'authentication_failed'),
                (self.encrypted(records), KEY, 'image_hash_mismatch')):
            self.zip(blob)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                restore.restore(self.archive, target=self.path, key=key, clock=lambda: self.now)
            self.assertEqual(self.path.read_bytes(), before)
        self.zip(before)
        with mock.patch.object(restore.cache.os, 'replace', side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError):
                restore.restore(self.archive, target=self.path, key=KEY, clock=lambda: self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([path.name for path in self.path.parent.iterdir()], ['cache.bin'])

    def test_wrong_key_and_ciphertext_nonce_truncation_corruption(self):
        self.put()
        with self.assertRaisesRegex(ValueError, 'authentication_failed'):
            self.load(base64.b64encode(b'x' * 32).decode())
        original = self.path.read_bytes()
        for changed in (b'', original[:20], original[:-1],
                        bytes([original[0] ^ 1]) + original[1:],
                        original[:-1] + bytes([original[-1] ^ 1])):
            self.path.write_bytes(changed)
            with self.assertRaises(ValueError):
                self.load()

    def test_authenticated_record_hash_shape_dates_and_variant_validation(self):
        variants = reading.views(self.images, fixture.result([0, 0, .2, .2]))
        self.put(variants=variants)
        good = copy.deepcopy(self.cache.records)
        changes = [
            lambda r: r[IDENTITY].update(expiresAt='bad'),
            lambda r: r[IDENTITY].update(expiresAt=NOW.replace(tzinfo=None).isoformat()),
            lambda r: r[IDENTITY].update(expiresAt=(NOW + cache.TTL + dt.timedelta(seconds=1)).isoformat()),
            lambda r: r[IDENTITY]['value']['images'][0].update(sha256='0' * 64),
            lambda r: r[IDENTITY]['value']['images'][0].update(bytes='!'),
            lambda r: r[IDENTITY]['value']['variants'][0].update(originalHash='0' * 64),
            lambda r: r[IDENTITY]['value']['variants'][0].update(pixelBox=[0, 0, 120, 100]),
            lambda r: r[IDENTITY]['value'].update(seen=['0' * 64]),
            lambda r: r[IDENTITY]['value'].update(unexpected='no'),
        ]
        for change in changes:
            records = copy.deepcopy(good)
            change(records)
            self.path.write_bytes(self.encrypted(records))
            with self.assertRaises(ValueError):
                self.load()

    def test_duplicate_variants_and_seen_are_not_silently_accepted(self):
        variants = reading.views(self.images)
        for kwargs in ({'variants': variants * 2},
                       {'variants': variants, 'seen': [variants[0]['variantId']] * 2},
                       {'variants': variants, 'seen': ['0' * 64]}):
            with self.assertRaises(ValueError):
                self.put(**kwargs)
        self.assertFalse(self.path.exists())

    def test_failed_save_is_transactional_unique_temp_removed(self):
        self.put()
        before, records = self.path.read_bytes(), copy.deepcopy(self.cache.records)
        with mock.patch.object(cache.os, 'replace', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.cache.put('1' * 64, self.source, self.text, self.images)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.cache.records, records)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['cache.bin'])
        self.put()
        self.assertNotEqual(self.path.read_bytes(), before)

    def test_capacity_exact_boundary_and_one_over_rollback(self):
        self.put()
        records = copy.deepcopy(self.cache.records)
        before = self.path.read_bytes()
        size = len(cache._json(records)) + 28
        with mock.patch.object(cache, 'MAX_BYTES', size):
            self.cache.save()
            self.assertEqual(self.path.stat().st_size, size)
        stable = self.path.read_bytes()
        with mock.patch.object(cache, 'MAX_BYTES', size - 1):
            with self.assertRaisesRegex(ValueError, 'evidence_cache_capacity'):
                self.cache.save()
        self.assertEqual(self.path.read_bytes(), stable)
        self.assertEqual(self.cache.records, records)
        self.assertNotEqual(before, stable)

    def test_real_64mib_read_limit_and_image_limit(self):
        self.path.parent.mkdir()
        with self.path.open('wb') as target:
            target.truncate(cache.MAX_BYTES + 1)
        with self.assertRaisesRegex(ValueError, 'evidence_cache_capacity'):
            self.load()
        with self.assertRaisesRegex(ValueError, 'evidence_image_capacity'):
            self.cache.put(IDENTITY, {}, '', [{'bytes': b'x' * (8 * 1024 * 1024 + 1),
                                             'mime': 'image/png'}])

    def test_real_64mib_encrypted_write_boundary(self):
        self.put()
        record = self.cache.records[IDENTITY]['value']
        record['source'] = {'syntheticPadding': ''}
        size = len(cache._json(self.cache.records)) + 28
        record['source']['syntheticPadding'] = 'x' * (cache.MAX_BYTES - size)
        self.cache.save()
        self.assertEqual(self.path.stat().st_size, 64 * 1024 * 1024)
        record['source']['syntheticPadding'] += 'x'
        with self.assertRaisesRegex(ValueError, 'evidence_cache_capacity'):
            self.cache.save()
        self.assertEqual(self.path.stat().st_size, 64 * 1024 * 1024)

    def test_private_output_allowlist_public_checkout_and_traversal(self):
        for path in (self.root / 'cache.bin', self.root / '_private-evidence' / 'plain.json',
                     self.root / '..' / '_private-evidence' / 'cache.bin',
                     cache.REPO_ROOT / '_private-evidence' / 'cache.bin'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                cache.EvidenceCache(path, KEY, clock=lambda: self.now)
        other_repo = self.root / 'checkout'
        other_repo.mkdir()
        (other_repo / '.git').write_text('gitdir: synthetic')
        with self.assertRaisesRegex(ValueError, 'public_path_refused'):
            cache.EvidenceCache(other_repo / '_private-evidence' / 'cache.bin',
                                KEY, clock=lambda: self.now)

    def test_symlink_and_parent_link_refused(self):
        self.put()
        linked = self.root / 'linked'
        try:
            linked.symlink_to(self.path.parent, target_is_directory=True)
        except OSError:
            # Some Windows runners deny link creation; exercise the same lstat
            # branch with the Windows reparse bit, without changing privileges.
            fake = mock.Mock(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            with mock.patch.object(Path, 'lstat', return_value=fake):
                self.assertTrue(cache._linked(self.path.parent))
            with mock.patch.object(cache, '_linked', return_value=True):
                with self.assertRaisesRegex(ValueError, 'symlink_refused'):
                    cache.checked_path(self.path, output=True)
            return
        with self.assertRaisesRegex(ValueError, 'symlink_refused'):
            cache.checked_path(linked / 'cache.bin')
        self.zip()
        linked_archive = self.root / 'alias'
        linked_archive.symlink_to(self.archive)
        with self.assertRaisesRegex(ValueError, 'symlink_refused'):
            restore.restore(linked_archive, target=self.path, key=KEY, clock=lambda: self.now)
        self.path.unlink()
        self.path.symlink_to(self.archive)
        with self.assertRaisesRegex(ValueError, 'symlink_refused'):
            self.load()

    def test_restore_rejects_traversal_links_duplicates_other_members(self):
        self.put()
        before = self.path.read_bytes()
        for name, mode in [('../cache.bin', stat.S_IFREG), ('/cache.bin', stat.S_IFREG),
                           ('folder/cache.bin', stat.S_IFREG), ('folder\\cache.bin', stat.S_IFREG),
                           ('C:\\cache.bin', stat.S_IFREG), ('cache.bin', stat.S_IFLNK),
                           ('cache.bin', stat.S_IFDIR), ('other.bin', stat.S_IFREG)]:
            self.zip(name=name, mode=mode)
            with self.subTest(name=name, mode=mode), self.assertRaises(ValueError):
                restore.restore(self.archive, target=self.path, key=KEY, clock=lambda: self.now)
            self.assertEqual(self.path.read_bytes(), before)
        with zipfile.ZipFile(self.zip(), 'a') as archive:
            archive.writestr('extra.txt', 'synthetic')
        with self.assertRaisesRegex(ValueError, 'invalid_evidence_artifact'):
            restore.restore(self.archive, target=self.path, key=KEY, clock=lambda: self.now)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.zip(), 'a') as archive:
                archive.writestr('cache.bin', before)
        with self.assertRaisesRegex(ValueError, 'invalid_evidence_artifact'):
            restore.restore(self.archive, target=self.path, key=KEY, clock=lambda: self.now)

    def test_restore_corrupt_crc_zip_bomb_and_oversized_archive(self):
        self.put()
        before = self.path.read_bytes()
        self.archive.write_bytes(b'not zip')
        with self.assertRaisesRegex(ValueError, 'invalid_evidence_artifact'):
            restore.restore(self.archive, target=self.path, key=KEY, clock=lambda: self.now)
        self.zip()
        raw = bytearray(self.archive.read_bytes())
        start = raw.index(before)
        raw[start] ^= 1
        self.archive.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'invalid_evidence_artifact'):
            restore.restore(self.archive, target=self.path, key=KEY, clock=lambda: self.now)
        # Lower the uncompressed limit for a compressible member; archive limits
        # and the real 64MiB encrypted-file bound are tested separately.
        self.zip(b'x' * 1024)
        with mock.patch.object(restore.cache, 'MAX_BYTES', 1023):
            with self.assertRaisesRegex(ValueError, 'invalid_evidence_artifact'):
                restore.restore(self.archive, target=self.path, key=KEY, clock=lambda: self.now)
        with self.archive.open('wb') as target:
            target.truncate(restore.MAX_ARCHIVE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, 'evidence_cache_capacity'):
            restore.restore(self.archive, target=self.path, key=KEY, clock=lambda: self.now)
        self.assertEqual(self.path.read_bytes(), before)

    def test_restore_wrong_key_and_authenticated_expiry_preserve_target(self):
        self.put()
        self.zip()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'authentication_failed'):
            restore.restore(self.archive, target=self.path,
                            key=base64.b64encode(b'z' * 32).decode(), clock=lambda: self.now)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_headless_success_and_sanitized_failure(self):
        self.now = dt.datetime.now(dt.timezone.utc)
        self.put()
        self.zip()
        target = self.root / 'cli' / '_private-evidence'
        command = [sys.executable, '-B', '-X', 'utf8', str(fixture.TOOLS / 'restore-schedule-cache.py'),
                   '--archive', str(self.archive), '--cache-dir', str(target)]
        environment = {**os.environ, 'SCHEDULE_EVIDENCE_KEY': KEY}
        options = {'capture_output': True, 'text': True, 'env': environment,
                   'creationflags': subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0}
        good = subprocess.run(command, **options)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertTrue((target / 'cache.bin').is_file())
        mixed = copy.deepcopy(self.cache.records)
        mixed['f' * 64] = copy.deepcopy(mixed[IDENTITY])
        mixed['f' * 64]['expiresAt'] = (self.now - dt.timedelta(seconds=1)).isoformat()
        self.zip(self.encrypted(mixed))
        pruned = subprocess.run(command, **options)
        self.assertEqual(pruned.returncode, 0, pruned.stderr)
        self.assertIn('1 records, 1 expired payloads discarded', pruned.stdout)
        self.assertNotIn(KEY, pruned.stdout + pruned.stderr)
        self.assertNotIn(self.text, pruned.stdout + pruned.stderr)
        for key in ('', 'INVALID-SECRET-DO-NOT-PRINT'):
            environment['SCHEDULE_EVIDENCE_KEY'] = key
            failed = subprocess.run(command, **options)
            self.assertEqual(failed.returncode, 1)
            self.assertIn('evidence_cache_key', failed.stderr)
            self.assertNotIn('INVALID-SECRET', failed.stderr + failed.stdout)
            self.assertNotIn(self.text, failed.stderr + failed.stdout)
        environment['SCHEDULE_EVIDENCE_KEY'] = KEY
        mixed[IDENTITY]['expiresAt'] = mixed['f' * 64]['expiresAt']
        self.zip(self.encrypted(mixed))
        expired = subprocess.run(command, **options)
        self.assertEqual(expired.returncode, 0, expired.stderr)
        self.assertIn('status=all_expired, 0 records, 2 expired payloads discarded', expired.stdout)
        tombstones = cache.EvidenceCache(target / 'cache.bin', KEY, clock=lambda: self.now)
        self.assertEqual(set(tombstones.records), {IDENTITY, 'f' * 64})
        with self.assertRaisesRegex(ValueError, 'evidence_cache_expired'):
            tombstones.get(IDENTITY)
        missing = subprocess.run(command[:5], **options)
        self.assertNotEqual(missing.returncode, 0)

    def test_old_stash_shape_remains_readable(self):
        self.put()
        records = copy.deepcopy(self.cache.records)
        records[IDENTITY]['value'].pop('variants')
        records[IDENTITY]['value'].pop('seen')
        self.path.write_bytes(self.encrypted(records))
        value = self.load().get(IDENTITY)
        self.assertEqual(value['images'], self.images)
        self.assertEqual(value['variants'], [])
        self.assertEqual(value['seen'], [])


if __name__ == '__main__':
    unittest.main()
