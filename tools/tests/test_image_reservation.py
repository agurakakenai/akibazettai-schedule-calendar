"""Image requests reserve from their actual images; the basis is stored and re-verified."""
import base64
import copy
import io
import unittest

import test_ai_budget as fixture


money, ledger, azure, base = fixture.money, fixture.ledger, fixture.azure, fixture.base


def image_uri(width, height, fmt='JPEG'):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), 'white').save(buffer, format=fmt)
    return f'data:image/{fmt.lower()};base64,' + base64.b64encode(buffer.getvalue()).decode()


def image_messages(*sizes, text='saved schedule text', fmt='JPEG'):
    return [{'role': 'user', 'content': [
        {'type': 'text', 'text': text},
        *[{'type': 'image_url', 'image_url': {'url': image_uri(w, h, fmt), 'detail': 'high'}}
          for w, h in sizes]]}]


class ImageReservationTests(unittest.TestCase):
    setUp = fixture.BudgetTests.setUp
    sleep = fixture.BudgetTests.sleep
    shared = fixture.BudgetTests.shared

    def test_formula_covers_tile_and_patch_schemes(self):
        self.assertEqual(money.image_token_bound(512, 512), max(170 + 85, (256 * 162 + 99) // 100))
        self.assertEqual(money.image_token_bound(1536, 2048), max(170 * 12 + 85, (1536 * 162 + 99) // 100))
        self.assertEqual(money.image_token_bound(4096, 4096), 170 * 16 + 85)
        for value in (0, -1, True, 1.5, 70000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money.image_token_bound(value, 100)

    def test_actual_images_give_a_small_audited_reservation(self):
        for fmt in ('JPEG', 'PNG'):
            with self.subTest(fmt=fmt):
                request = azure.request_budget(image_messages(*[(1536, 2048)] * 4, fmt=fmt), {},
                                               name='half_month_reading', max_completion_tokens=8192)
                basis = request['imageBasis']
                self.assertEqual([(row['width'], row['height']) for row in basis['images']], [(1536, 2048)] * 4)
                self.assertEqual((basis['safetyFactor'], basis['fixedTokens']), (2, 2048))
                self.assertEqual(request['inputCeiling'], basis['textBound'] + 2 * sum(
                    row['tokens'] for row in basis['images']) + 2048)
                charge = money.reservation(base.IDENTITY, request)
                self.assertLess(charge['reservedMicroJPY'], 20_000_000)
                self.assertGreater(money.ceiling('gpt-5.6-luna', '2026-07-09', 922000, 8192), 350_000_000)
                money.validate_charge(charge)

    def test_text_bound_excludes_image_bytes_but_counts_the_text(self):
        small = azure.request_budget(image_messages((900, 1200)), {}, name='n', max_completion_tokens=100)
        large = azure.request_budget(image_messages((900, 1200), text='x' * 5000), {}, name='n',
                                     max_completion_tokens=100)
        self.assertGreaterEqual(large['imageBasis']['textBound'] - small['imageBasis']['textBound'],
                                5000 - len('saved schedule text'))
        self.assertLess(small['imageBasis']['textBound'], 1500)

    def test_unreadable_images_and_text_requests_keep_existing_ceilings(self):
        legacy = azure.request_budget([{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]}], {},
            name='n', max_completion_tokens=100)
        self.assertEqual((legacy['inputCeiling'], 'imageBasis' in legacy), (money.INPUT_MAX, False))
        remote = azure.request_budget([{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/a.jpg'}}]}], {},
            name='n', max_completion_tokens=100)
        self.assertEqual(remote['inputCeiling'], money.INPUT_MAX)
        text = azure.request_budget([{'role': 'user', 'content': 'plain'}], {}, name='n', max_completion_tokens=100)
        self.assertNotIn('imageBasis', text)
        self.assertLess(text['inputCeiling'], 2000)

    def test_tampered_basis_or_ceiling_is_rejected(self):
        request = azure.request_budget(image_messages((1536, 2048)), {}, name='n', max_completion_tokens=100)
        for change in (
                lambda value: value.update(inputCeiling=value['inputCeiling'] - 1),
                lambda value: value['imageBasis'].update(safetyFactor=1),
                lambda value: value['imageBasis'].update(fixedTokens=0),
                lambda value: value['imageBasis']['images'][0].update(tokens=1),
                lambda value: value['imageBasis']['images'][0].update(width=10),
                lambda value: value['imageBasis'].update(extra=True),
                lambda value: value.update(imageInput=False)):
            tampered = copy.deepcopy(request)
            change(tampered)
            with self.subTest(change=change), self.assertRaises(ValueError):
                money.validate_request(tampered)

    def test_reserved_receipt_keeps_the_basis_and_settles_or_flags_inconsistency(self):
        request = azure.request_budget(image_messages(*[(1536, 2048)] * 4), {}, name='n',
                                       max_completion_tokens=8192)
        key = base.digest('image-reading')
        with self.shared(component='schedule') as usage:
            usage.reserve(key, base.IDENTITY, request=request)
            receipt = next(iter(usage.state['receipts'].values()))
            self.assertEqual(receipt['money']['imageBasis'], request['imageBasis'])
            ledger.validate_state(usage.state)
            charge = receipt['money']
        settled = money.settle(charge, {'model': 'gpt-5.6-luna-2026-07-09', 'usage': {
            'prompt_tokens': 9000, 'completion_tokens': 300, 'total_tokens': 9300}})
        self.assertEqual(settled['usageStatus'], 'settled')
        self.assertLess(settled['chargedMicroJPY'], charge['reservedMicroJPY'])
        with self.assertRaises(money.UsageInconsistent):
            money.settle(charge, {'model': 'gpt-5.6-luna-2026-07-09', 'usage': {
                'prompt_tokens': charge['inputCeiling'] + 1, 'completion_tokens': 1,
                'total_tokens': charge['inputCeiling'] + 2}})


if __name__ == '__main__':
    unittest.main()
