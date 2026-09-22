import datetime as dt
import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    'collection_slots', Path(__file__).resolve().parents[1] / 'collection-slots.py')
slots = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(slots)


class CollectionSlotsTests(unittest.TestCase):
    def test_personal_four_slots_have_distinct_cron_identity(self):
        self.assertEqual({(hour + 9) % 24 for hour in slots.PERSONAL_SCHEDULES.values()}, {1, 7, 10, 12})
        for expression, hour in slots.PERSONAL_SCHEDULES.items():
            created = dt.datetime(2026, 9, 22, hour, 12, tzinfo=slots.UTC)
            route = slots.event_slot(expression, created.isoformat())
            expected = created.astimezone(slots.JST)
            self.assertEqual(route['kind'], 'personal')
            self.assertEqual(route['serviceDate'], expected.date().isoformat())
            self.assertTrue(route['shouldCollect'])

    def test_official_twelve_thirty_is_not_half_month_eleven_thirty(self):
        official = slots.event_slot(slots.OFFICIAL_SCHEDULE, '2026-10-01T03:40:00Z')
        half = slots.event_slot('30 2 1,13 * *', '2026-10-01T03:40:00Z')
        self.assertEqual(official['kind'], 'official')
        self.assertEqual(half['kind'], 'half-month')
        self.assertNotEqual(official['slotId'], half['slotId'])

    def test_half_month_start_and_thirteenth_cycle(self):
        blocked = slots.event_slot('30 15 * * *', '2026-09-29T15:35:00Z')
        self.assertFalse(blocked['shouldCollect'])
        first = slots.event_slot('30 15 * * *', '2026-09-30T15:35:00Z')
        self.assertTrue(first['shouldCollect'])
        self.assertEqual(first['serviceDate'], '2026-10-01')
        self.assertEqual(first['period'], {'from': '2026-10-01', 'to': '2026-10-15'})
        second = slots.event_slot('30 15 * * *', '2026-10-12T15:35:00Z')
        self.assertEqual(second['period'], {'from': '2026-10-16', 'to': '2026-10-31'})

    def test_extra_morning_uses_actual_jst_month_boundary(self):
        for utc, expected in [
            ('2027-02-28T21:35:00Z', True),
            ('2028-02-28T21:35:00Z', False),
            ('2028-02-29T21:35:00Z', True),
            ('2026-10-30T21:35:00Z', False),
            ('2026-10-31T21:35:00Z', True),
            ('2026-11-30T21:35:00Z', True),
            ('2026-12-31T21:35:00Z', True),
            ('2026-10-12T21:35:00Z', True),
        ]:
            with self.subTest(utc=utc):
                self.assertEqual(slots.event_slot('30 21 12,28-31 * *', utc)['shouldCollect'], expected)

    def test_delayed_job_and_repeated_event_keep_same_slot_and_service_day(self):
        first = slots.event_slot('0 3 * * *', '2026-09-23T03:02:00Z')
        delayed = slots.event_slot('0 3 * * *', '2026-09-23T16:05:00Z')
        self.assertEqual(first, delayed)
        self.assertEqual(delayed['serviceDate'], '2026-09-23')
        self.assertEqual(slots.event_slot('0 3 * * *', '2026-09-23T03:02:00Z'), first)

    def test_unknown_timezone_or_unmatched_monthly_event_is_rejected(self):
        with self.assertRaises(ValueError):
            slots.event_slot('0 3 * * *', '2026-09-23T03:02:00')
        with self.assertRaises(ValueError):
            slots.event_slot('30 2 1,13 * *', '2026-10-20T02:35:00Z')
        with self.assertRaises(ValueError):
            slots.event_slot('30 3-6,8-11 * * * ', '2026-10-20T03:35:00Z')


if __name__ == '__main__':
    unittest.main()
