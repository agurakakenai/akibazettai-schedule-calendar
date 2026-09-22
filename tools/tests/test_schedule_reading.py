"""Synthetic rasters only; no HTTP, AI, GUI or private source fixtures."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from PIL import Image, ImageDraw


TOOLS = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reading = load('local_reading_test', 'schedule-reading.py')


def image(size=(120, 100), *, mime='image/png', color='white'):
    with Image.new('RGB', size, color) as raster:
        draw = ImageDraw.Draw(raster)
        draw.text((size[0] - 48, size[1] - 14), '5 12-18', fill='black')
        output = io.BytesIO()
        raster.save(output, format='PNG' if mime == 'image/png' else 'JPEG')
    return {'bytes': output.getvalue(), 'mime': mime}


def result(*boxes, index=0, classification='uncertain', complete=False):
    return {'classification': classification, 'complete': complete,
            'periods': [{'month': 9, 'half': 'first', 'days': [
                {'day': 5, 'shifts': [], 'evidence': {'imageIndex': index, 'box': box}}
                for box in boxes]}] if boxes else []}


class LocalReadingTests(unittest.TestCase):
    def setUp(self):
        self.network = mock.patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_initial_body_all_four_once_and_whitelisted_mapping(self):
        images = [dict(image(), url='PRIVATE-URL') for _ in range(4)]
        packs = reading.initial_reading('synthetic body', images)
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0]['text'], 'synthetic body')
        self.assertEqual([entry['originalIndex'] for entry in packs[0]['imageMap']], list(range(4)))
        self.assertEqual([entry['bytes'] for entry in packs[0]['images']],
                         [entry['bytes'] for entry in images])
        self.assertNotIn('PRIVATE-URL', repr(packs))
        self.assertEqual(len({v['variantId'] for v in packs[0]['images']}), 4)

    def test_five_attachments_chunk_no_drop_and_repeat_body(self):
        packs = reading.initial_reading('same context', [image()] * 5)
        self.assertEqual([len(pack['images']) for pack in packs], [4, 1])
        self.assertEqual([(pack['part'], pack['parts']) for pack in packs], [(1, 2), (2, 2)])
        self.assertTrue(all(pack['text'] == 'same context' for pack in packs))
        self.assertEqual([m['originalIndex'] for p in packs for m in p['imageMap']], list(range(5)))

    def test_same_run_padded_crop_precise_mapping_and_no_metadata(self):
        original = image((101, 99))
        before = copy.deepcopy(original)
        initial = reading.initial_reading('schedule', [original])
        packs = reading.reread('schedule', [original], result([.7, .8, 1, 1]))
        crop = packs[0]['images'][0]
        self.assertEqual(original, before)
        self.assertEqual(crop['originalHash'], initial[0]['images'][0]['variantHash'])
        self.assertEqual(crop['pixelBox'], [66, 75, 101, 99])
        self.assertEqual(crop['box'], [66 / 101, 75 / 99, 1, 1])
        self.assertEqual(crop['variantHash'], hashlib.sha256(crop['bytes']).hexdigest())
        with Image.open(io.BytesIO(crop['bytes'])) as raster:
            self.assertEqual(raster.size, (105, 72))
            self.assertNotIn('exif', raster.info)
        self.assertEqual(reading.reread('schedule', [original], result([.7, .8, 1, 1]),
                                       seen=[crop['variantId']]), [])

    def test_unknown_regions_overlap_cover_edges_and_deduplicate(self):
        variants = reading.views([image()])
        self.assertEqual(len(variants), 4)
        boxes = [v['box'] for v in variants]
        self.assertEqual(boxes[0][:2], [0, 0])
        self.assertEqual(boxes[-1][2:], [1, 1])
        self.assertGreater(boxes[0][2], boxes[1][0])
        self.assertGreater(boxes[0][3], boxes[2][1])
        self.assertEqual(variants, reading.views([image()]))
        self.assertEqual(len(reading.pack_attachments('', variants + variants)[0]['images']), 4)

    def test_no_first_four_region_truncation_or_duplicate_crops(self):
        areas = [[i / 10, 0, i / 10 + .05, .1] for i in range(6)]
        variants = reading.views([image()], result(*areas, areas[0]))
        self.assertEqual(len(variants), 6)
        self.assertEqual(len({v['variantId'] for v in variants}), 6)

    def test_known_and_unknown_images_keep_original_indexes(self):
        variants = reading.views([image(), image()], result([.1, .1, .2, .2], index=1))
        self.assertEqual([v['originalIndex'] for v in variants], [0, 0, 0, 0, 1])

    def test_unknown_row_on_partially_located_image_still_gets_tiles(self):
        variants = reading.views([image()], result([.1, .1, .2, .2], None))
        self.assertEqual(len(variants), 5)
        self.assertEqual([variant['box'] for variant in variants[1:]],
                         [variant['box'] for variant in reading.views([image()])])

    def test_classification_partial_and_caption_conflicts(self):
        for value, text in [(result(), ''), (result(complete=True), ''),
                            (result(classification='non_schedule', complete=True), '9月の予定'),
                            (result(classification='schedule', complete=True), '')]:
            self.assertTrue(reading.needs_reread(value, text))
        complete = result([0, 0, 1, 1], classification='schedule', complete=True)
        self.assertFalse(reading.needs_reread(complete))
        for field in ('day', 'shifts'):
            partial = copy.deepcopy(complete)
            partial['periods'][0]['days'][0][field] = None
            self.assertTrue(reading.needs_reread(partial))
        negative = result(classification='non_schedule', complete=True)
        self.assertFalse(reading.needs_reread(negative, 'portrait'))
        self.assertTrue(reading.needs_reread(negative, 'portrait', caption_schedule=True))
        self.assertEqual(reading.reread('', [image()], complete), [])

    def test_invalid_regions_indexes_and_nonfinite_values(self):
        for box in ([0, 0, 0, 1], [1, 0, 0, 1], [0, 0, float('nan'), 1],
                    [0, 0, float('inf'), 1], [False, 0, 1, 1], [-.1, 0, 1, 1], [0, 1]):
            with self.subTest(box=box), self.assertRaisesRegex(ValueError, 'invalid_reading_region'):
                reading.views([image()], result(box))
        for index in (-1, 1, True, None):
            with self.subTest(index=index), self.assertRaises(ValueError):
                reading.views([image()], result([0, 0, 1, 1], index=index))

    def test_bad_mime_corrupt_image_and_dimensions(self):
        for value in ({'bytes': b'no', 'mime': 'image/jpeg'},
                      {**image(), 'mime': 'image/jpeg'},
                      {'bytes': b'', 'mime': 'image/png'},
                      image((8193, 1)), image((5000, 4001))):
            with self.assertRaises(ValueError):
                reading.original_views([value])

    def test_text_only_and_utf8_limit(self):
        pack = reading.initial_reading('', [])[0]
        self.assertEqual(pack['images'], [])
        reading.initial_reading('予' * 2000, [])
        with self.assertRaisesRegex(ValueError, 'reading_text_limit'):
            reading.initial_reading('予' * 2001, [])

    def test_pixel_pack_boundary_exact_and_over(self):
        # Full decoding of two 20M-pixel synthetic rasters verifies the actual cap.
        originals = reading.original_views([image((5000, 4000))] * 2 + [image((1, 1))])
        packs = reading.pack_attachments('', originals)
        self.assertEqual([p['pixels'] for p in packs], [40_000_000, 1])

    def test_byte_pack_boundary_exact_and_over(self):
        # Legal PNG ancillary data, not a header-only fake or a real source image.
        import struct
        import zlib

        def padded(size):
            value = image((1, 1))
            raw = value['bytes']
            data = b'x' * (size - len(raw) - 12)
            kind = b'tEXt'
            chunk = struct.pack('>I', len(data)) + kind + data
            chunk += struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
            return {**value, 'bytes': raw[:-12] + chunk + raw[-12:]}

        images = [padded(8 * 1024 * 1024), padded(4 * 1024 * 1024), image((1, 1))]
        packs = reading.initial_reading('', images)
        self.assertEqual([len(p['images']) for p in packs], [2, 1])
        self.assertEqual(packs[0]['attachmentBytes'], reading.MAX_POST_BYTES)
        with self.assertRaisesRegex(ValueError, 'image_byte_limit'):
            reading.initial_reading('', [padded(reading.MAX_IMAGE_BYTES + 1)])

    def test_wire_size_exact_boundary_includes_escaped_body_and_map(self):
        import base64
        original = reading.original_views([image()])[0]
        text = '"\\日本語'
        pack = reading.pack_attachments(text, [original], request_overhead_bytes=0)[0]
        content = [{'type': 'text', 'text': json.dumps(
            {'body': text, 'imageMap': pack['imageMap']}, ensure_ascii=False)},
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/png;base64,' + base64.b64encode(original['bytes']).decode(),
                'detail': 'high'}}]
        self.assertEqual(pack['wireBytes'], len(json.dumps(content).encode('utf-8')))
        overhead = reading.MAX_REQUEST_BYTES - pack['wireBytes']
        self.assertEqual(reading.pack_attachments(
            text, [original], request_overhead_bytes=overhead)[0]['wireBytes'],
            reading.MAX_REQUEST_BYTES)
        with self.assertRaisesRegex(ValueError, 'reading_request_limit'):
            reading.pack_attachments(text, [original], request_overhead_bytes=overhead + 1)
        packs = reading.pack_attachments(
            text, reading.original_views([image()] * 2), request_overhead_bytes=overhead)
        self.assertEqual([len(p['images']) for p in packs], [1, 1])

    def test_mapping_tampering_refused(self):
        variant = reading.views([image()])[0]
        for field, value in [('originalHash', 'PRIVATE-URL'), ('box', [0, 0, 1, 1]),
                             ('pixelBox', [0, 0, 120, 100]), ('variantId', '0' * 64),
                             ('variantHash', '0' * 64), ('originalIndex', True)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                reading.pack_attachments('', [{**variant, field: value}])

    def test_conflicting_duplicate_and_invalid_seen_are_errors(self):
        variant = reading.views([image()])[0]
        raw = image(mime='image/jpeg', color='red')['bytes']
        changed = {**variant, 'bytes': raw, 'variantHash': hashlib.sha256(raw).hexdigest()}
        with self.assertRaisesRegex(ValueError, 'reading_variant_conflict'):
            reading.pack_attachments('', [variant, changed])
        with self.assertRaisesRegex(ValueError, 'invalid_reading_seen'):
            reading.views([image()], seen='not-an-id-list')

    def test_coverage_is_union_not_disjoint_bounding_box(self):
        self.assertTrue(reading.covers_original(reading.views([image()]), 0))
        corners = reading.views([image()], result([0, 0, .1, .1], [.9, .9, 1, 1]))
        self.assertFalse(reading.covers_original(corners, 0))
        self.assertFalse(reading.covers_original(corners, 1))


if __name__ == '__main__':
    unittest.main()
