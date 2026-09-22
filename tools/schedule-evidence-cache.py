"""Private AES-256-GCM cache; no plaintext file, public metadata or network I/O.

Use an access-controlled directory named _private-evidence OUTSIDE any checkout.
Only cache.bin is writable. Windows directory ACLs remain the runner's duty;
POSIX new directories/files use 0700/0600. Never publish this directory.
get() returns None only for an absent key; expiry/authentication/key errors raise.
put(..., variants=..., seen=...) preserves exact reread bytes across runs and does
not extend the original seven-day expiry. Restore may discard expired payloads,
retaining encrypted expiry/identity markers so get/find_source cannot refetch them.
No automatic eviction or silent reset.
"""
import base64
import binascii
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile


MAX_BYTES = 64 * 1024 * 1024
TTL = dt.timedelta(days=7)
AAD = b'akibazettai-schedule-evidence-v1'
REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    'evidence_reading', Path(__file__).with_name('schedule-reading.py'))
reading = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reading)


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('invalid_evidence_key')
    return value


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def checked_path(path, *, output=False):
    """Reject traversal, symlinks/junctions and non-allowlisted private outputs."""
    path = Path(path)
    if ('..' in path.parts or (os.name == 'nt' and (
            path.drive.startswith('\\\\') or any(':' in part for part in path.parts[1:])))):
        raise ValueError('invalid_evidence_path')
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        if part.exists() or part.is_symlink():
            if _linked(part):
                raise ValueError('evidence_symlink_refused')
    if output:
        if path.name != 'cache.bin' or path.parent.name != '_private-evidence':
            raise ValueError('invalid_evidence_output')
        if path == REPO_ROOT or REPO_ROOT in path.parents:
            raise ValueError('evidence_public_path_refused')
        if any((parent / '.git').exists() for parent in path.parents):
            raise ValueError('evidence_public_path_refused')
    if path.exists() and not path.is_file():
        raise ValueError('invalid_evidence_file')
    return path


def read_bounded(path, *, limit=MAX_BYTES, output=False):
    path = checked_path(path, output=output)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('evidence_cache_capacity')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('evidence_cache_capacity')
    return raw


def atomic_write(path, blob):
    """Only encrypted, bounded cache bytes may be passed here."""
    if not isinstance(blob, bytes) or not 28 <= len(blob) <= MAX_BYTES:
        raise ValueError('evidence_cache_capacity')
    path = checked_path(path, output=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    checked_path(path, output=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.cache-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(blob)
            target.flush()
            os.fsync(target.fileno())
        checked_path(path, output=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise ValueError('invalid_evidence_expiry') from None


def _now(clock):
    value = clock()
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('invalid_evidence_clock')
    return value


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('invalid_evidence_record') from None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('invalid_evidence_record')
        result[key] = value
    return result


def _cipher(key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if key is None or key == '':
        raise ValueError('missing_evidence_cache_key')
    try:
        raw = base64.b64decode(key, validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise ValueError('invalid_evidence_cache_key') from None
    if len(raw) != 32:
        raise ValueError('invalid_evidence_cache_key')
    return AESGCM(raw)


def _encode_images(images):
    if not isinstance(images, list):
        raise ValueError('invalid_evidence_images')
    encoded, size = [], 0
    for image in images:
        if (not isinstance(image, dict) or not isinstance(image.get('bytes'), (bytes, bytearray))
                or not image['bytes'] or image.get('mime') not in ('image/png', 'image/jpeg')):
            raise ValueError('invalid_evidence_image')
        if len(image['bytes']) > 8 * 1024 * 1024:
            raise ValueError('evidence_image_capacity')
        size += 4 * ((len(image['bytes']) + 2) // 3)
        if size + 28 > MAX_BYTES:
            raise ValueError('evidence_cache_capacity')
        encoded.append({**image, 'bytes': base64.b64encode(image['bytes']).decode('ascii'),
                        'sha256': hashlib.sha256(image['bytes']).hexdigest()})
    return encoded


def _decode_images(images):
    if not isinstance(images, list):
        raise ValueError('invalid_evidence_images')
    decoded = []
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get('bytes'), str):
            raise ValueError('invalid_evidence_image')
        if len(image['bytes']) > 4 * ((8 * 1024 * 1024 + 2) // 3):
            raise ValueError('evidence_image_capacity')
        try:
            raw = base64.b64decode(image['bytes'], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('invalid_evidence_image') from None
        if hashlib.sha256(raw).hexdigest() != image.get('sha256'):
            raise ValueError('evidence_image_hash_mismatch')
        result = {key: value for key, value in image.items() if key != 'sha256'}
        result['bytes'] = raw
        decoded.append(result)
    _encode_images(decoded)
    return decoded


def _validate_variants(variants, images, seen):
    if not isinstance(seen, list):
        raise ValueError('invalid_evidence_seen')
    originals = reading.original_views(images)
    ids = set()
    for variant in variants:
        index = variant.get('originalIndex')
        if type(index) is not int or not 0 <= index < len(images):
            raise ValueError('invalid_evidence_variant')
        if (variant.get('originalHash') != hashlib.sha256(images[index]['bytes']).hexdigest()
                or variant.get('variantHash') != hashlib.sha256(variant['bytes']).hexdigest()):
            raise ValueError('evidence_variant_hash_mismatch')
        reading._metadata(variant)
        if variant['originalSize'] != originals[index]['originalSize']:
            raise ValueError('invalid_evidence_variant')
        with reading._open_image(variant):
            pass
        identity = _digest(variant.get('variantId'))
        if identity in ids:
            raise ValueError('duplicate_evidence_variant')
        ids.add(identity)
    for identity in seen:
        _digest(identity)
    if len(set(seen)) != len(seen) or not set(seen) <= ids | {view['variantId'] for view in originals}:
        raise ValueError('invalid_evidence_seen')


def decode_blob(blob, key, *, clock):
    """Authenticate and validate without writing; used by the local restore CLI."""
    cipher = _cipher(key)
    if not isinstance(blob, bytes) or not 28 <= len(blob) <= MAX_BYTES:
        raise ValueError('invalid_evidence_cache')
    from cryptography.exceptions import InvalidTag
    try:
        records = json.loads(cipher.decrypt(blob[:12], blob[12:], AAD), object_pairs_hook=_object)
    except (InvalidTag, ValueError, UnicodeError, RecursionError):
        raise ValueError('evidence_cache_authentication_failed') from None
    if not isinstance(records, dict):
        raise ValueError('invalid_evidence_cache')
    now = _now(clock)
    for key, record in records.items():
        _digest(key)
        if not isinstance(record, dict) or set(record) != {'expiresAt', 'value'}:
            raise ValueError('invalid_evidence_record')
        expiry = _timestamp(record['expiresAt'])
        if expiry > now + TTL:
            raise ValueError('invalid_evidence_expiry')
        value = record['value']
        if (not isinstance(value, dict)
                or not {'source', 'text', 'images', 'reading'} <= set(value)
                or set(value) - {'source', 'text', 'images', 'reading', 'variants', 'seen'}
                or not isinstance(value['source'], dict) or not isinstance(value['text'], str)
                or value['reading'] is not None and not isinstance(value['reading'], dict)):
            raise ValueError('invalid_evidence_record')
        images = _decode_images(value['images'])
        variants = _decode_images(value.get('variants', []))
        _validate_variants(variants, images, value.get('seen', []))
        if len(value['text'].encode('utf-8')) > reading.MAX_INPUT_BYTES:
            raise ValueError('reading_text_limit')
    _json(records)
    return records


def _encode_blob(records, cipher):
    raw = _json(records)
    if len(raw) + 28 > MAX_BYTES:
        raise ValueError('evidence_cache_capacity')
    nonce = os.urandom(12)
    return nonce + cipher.encrypt(nonce, raw, AAD)


class EvidenceCache:
    def __init__(self, path, key, *, clock):
        self.cipher, self.path, self.clock = _cipher(key), checked_path(path, output=True), clock
        _now(clock)
        self.records = {}
        if self.path.exists():
            self.records = decode_blob(read_bounded(self.path, output=True), key, clock=clock)

    def get(self, key):
        _digest(key)
        value = self.records.get(key)
        if value is None:
            return None
        if _timestamp(value['expiresAt']) <= _now(self.clock):
            raise ValueError('evidence_cache_expired')
        result = copy.deepcopy(value['value'])
        result['images'] = _decode_images(result['images'])
        result['variants'] = _decode_images(result.get('variants', []))
        result.setdefault('seen', [])
        _validate_variants(result['variants'], result['images'], result['seen'])
        return result

    def find_source(self, source_id, author_id):
        """Private recovery index when cache commit preceded the metadata snapshot."""
        return [key for key, record in self.records.items()
                if record['value']['source'].get('id') == source_id
                and record['value']['source'].get('authorId') == author_id]

    def put(self, key, source, text, images, *, reading=None, variants=None, seen=None):
        """Omitted variants/seen retain prior views; [] explicitly replaces them."""
        _digest(key)
        now = _now(self.clock)
        old = self.records.get(key)
        if old and _timestamp(old['expiresAt']) <= now:
            raise ValueError('evidence_cache_expired')
        if (not isinstance(source, dict) or not isinstance(text, str)
                or reading is not None and not isinstance(reading, dict)):
            raise ValueError('invalid_evidence_record')
        if len(text.encode('utf-8')) > 6000:
            raise ValueError('reading_text_limit')
        previous = old['value'] if old else {}
        encoded = _encode_images(images)
        encoded_variants = (copy.deepcopy(previous.get('variants', [])) if variants is None
                            else _encode_images(variants))
        seen = copy.deepcopy(previous.get('seen', [])) if seen is None else list(seen)
        _validate_variants(_decode_images(encoded_variants), _decode_images(encoded), seen)
        candidate = copy.deepcopy(self.records)
        candidate[key] = {
            'expiresAt': old['expiresAt'] if old else (now + TTL).isoformat(),
            'value': {'source': source, 'text': text, 'images': encoded, 'reading': reading,
                      'variants': encoded_variants, 'seen': seen}}
        self._save(candidate)
        self.records = copy.deepcopy(candidate)

    def _save(self, records):
        atomic_write(self.path, _encode_blob(records, self.cipher))

    def save(self):
        self._save(self.records)
