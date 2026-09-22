"""Restore an already-downloaded private artifact, locally; never fetch sources.

CLI: --archive <private-path>/schedule-evidence.zip
     --cache-dir <outside-checkout>/_private-evidence
Key: SCHEDULE_EVIDENCE_KEY (strict base64 of 32 bytes), never an argument.
ZIP input allowlist: exactly one regular cache.bin, no paths/links/encryption.
Output allowlist: cache.bin only. Authenticate/validate expiry before replacement.
Authenticated archives retain live records unchanged and discard expired payloads. Only
encrypted key/expiry/source-ID markers remain, preserving expired vs missing on
resume without retaining URL/body/images or renewing TTL. All-expired archives
restore these markers with explicit status, not an empty cold start. Malformed
or authentication failures never produce partial output.
The former no-argument GitHub download is intentionally removed: the parent
must approve/download the private artifact and pass its local path explicitly.
"""
import argparse
import datetime as dt
import importlib.util
import io
import os
from pathlib import Path
import stat
import sys
import zipfile
import zlib


SPEC = importlib.util.spec_from_file_location(
    'restore_evidence_cache', Path(__file__).with_name('schedule-evidence-cache.py'))
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)
MAX_ARCHIVE_BYTES = cache.MAX_BYTES + 1024 * 1024


def restore(archive_path=None, *, target=None, key=None, clock=None):
    """Return {records, bytes, expiredRecords, status}; no raw evidence in summary."""
    if archive_path is None or target is None:
        raise ValueError('evidence_restore_paths_required')
    path = cache.checked_path(archive_path)
    if path.name != 'schedule-evidence.zip':
        raise ValueError('invalid_evidence_archive_name')
    target = cache.checked_path(target, output=True)
    key = os.environ.get('SCHEDULE_EVIDENCE_KEY') if key is None else key
    cipher = cache._cipher(key)
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    raw = cache.read_bounded(path, limit=MAX_ARCHIVE_BYTES)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != 'cache.bin':
                raise ValueError('invalid_evidence_artifact')
            info = entries[0]
            mode = info.external_attr >> 16
            if (info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or info.flag_bits & 1 or info.external_attr & 0x10
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or not 28 <= info.file_size <= cache.MAX_BYTES):
                raise ValueError('invalid_evidence_artifact')
            with archive.open(info) as stream:
                blob = stream.read(cache.MAX_BYTES + 1)
            if len(blob) != info.file_size or len(blob) > cache.MAX_BYTES:
                raise ValueError('evidence_cache_capacity')
    except (zipfile.BadZipFile, EOFError, NotImplementedError, zlib.error):
        raise ValueError('invalid_evidence_artifact') from None
    now = cache._now(clock)
    records = cache.decode_blob(blob, key, clock=lambda: now)
    expired = {identity for identity, record in records.items()
               if cache._timestamp(record['expiresAt']) <= now}
    if expired:
        for identity in expired:
            record = records[identity]
            source = record['value']['source']
            records[identity] = {
                'expiresAt': record['expiresAt'],
                'value': {'source': {field: source[field] for field in ('id', 'authorId') if field in source},
                          'text': '', 'images': [], 'reading': None, 'variants': [], 'seen': []}}
        blob = cache._encode_blob(records, cipher)
    cache.atomic_write(target, blob)
    return {'records': len(records) - len(expired), 'bytes': len(blob),
            'expiredRecords': len(expired),
            'status': ('all_expired' if len(expired) == len(records) else 'partially_expired')
            if expired else 'restored'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--cache-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = restore(args.archive, target=args.cache_dir / 'cache.bin')
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print('evidence_restore_io_failed', file=sys.stderr)
        return 1
    discarded = f", {result['expiredRecords']} expired payloads discarded" if result.get('expiredRecords') else ''
    print(f"Encrypted schedule evidence restored (status={result['status']}, "
          f"{result['records']} records{discarded}).")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
