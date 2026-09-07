import importlib.util
from pathlib import Path
import unittest


TOOLS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location("capacity_test_" + name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capacity = load("request-capacity")
personal = load("personal-azure")
half = load("schedule-azure")


class RequestCapacityTests(unittest.TestCase):
    def test_timing_only_profile_is_separate_and_pins_dynamic_slot_schema(self):
        timing = load("half-month-timing")
        self.assertEqual(timing.MAX_OUTPUT_TOKENS, 3584)
        self.assertEqual(capacity.HALF_MONTH_TIMING["output"], 3584)
        self.assertEqual((half.MAX_OUTPUT_TOKENS, capacity.HALF_MONTH["output"]), (3840, 3840))
        self.assertEqual((personal.MAX_OUTPUT_TOKENS, capacity.PERSONAL["output"]), (2304, 2304))
        self.assertEqual(half.LEGACY_MAX_OUTPUT_TOKENS, 1200)
        old_personal, old_half = dict(capacity.PERSONAL), dict(capacity.HALF_MONTH)
        slots = [{"slotId": "s123456789a", "serviceDate": "2026-09-05", "shift": "昼"}]
        schema = timing.request_schema(slots)
        report = capacity.half_month_timing('{"body":"source","slots":[]}', timing.PROMPT,
                                            schema, timing.MAX_OUTPUT_TOKENS)
        self.assertLess(report["textReservationBound"], 10000)
        self.assertFalse(report["imageTokensIncluded"])
        self.assertFalse(report["serviceTpmGuaranteed"])
        self.assertEqual(capacity.PERSONAL, old_personal)
        self.assertEqual(capacity.HALF_MONTH, old_half)
        for prompt, changed_schema, output in (
                (timing.PROMPT + " changed", schema, timing.MAX_OUTPUT_TOKENS),
                (timing.PROMPT, {**schema, "extra": True}, timing.MAX_OUTPUT_TOKENS),
                (timing.PROMPT, schema, 9999),
                (timing.PROMPT, schema, 3840),
                (timing.PROMPT, schema, 3200),
                (timing.PROMPT, timing.request_schema([]), timing.MAX_OUTPUT_TOKENS)):
            with self.assertRaisesRegex(capacity.CapacityHold, "profile_stale"):
                capacity.half_month_timing("source", prompt, changed_schema, output)
        with self.assertRaisesRegex(capacity.CapacityHold, "^azure_capacity_hold$"):
            capacity.half_month_timing("x" * 6000, timing.PROMPT, schema, timing.MAX_OUTPUT_TOKENS)

    def test_new_contract_profiles_are_pinned_and_small_inputs_fit(self):
        payload = {"bodyLines": [{"id": 1, "text": "9/6 16:00"}], "author": "fixture"}
        report = capacity.personal(payload, personal.PROMPT, personal.SCHEMA, personal.MAX_OUTPUT_TOKENS)
        self.assertLess(report["textReservationBound"], 10000)
        self.assertFalse(report["serviceTpmGuaranteed"])
        self.assertFalse(report["imageTokensIncluded"])
        image_request = capacity.half_month('{"body":"9/6","images":[]}', half.PROMPT, half.SCHEMA, half.MAX_OUTPUT_TOKENS)
        self.assertLess(image_request["textReservationBound"], 10000)
        self.assertEqual(half.LEGACY_MAX_OUTPUT_TOKENS, 1200)
        self.assertEqual(personal.transport.MAX_RESPONSE_BYTES, 24000)

    def test_large_escaped_text_is_held_without_truncation(self):
        lines = [{"id": index + 1, "text": "x" * 46 + "\n"} for index in range(127)]
        lines.append({"id": 128, "text": "x" * 31})
        before = [dict(line) for line in lines]
        self.assertEqual(sum(len(line["text"]) for line in lines), 6000)
        with self.assertRaisesRegex(capacity.CapacityHold, "^azure_capacity_hold$"):
            capacity.personal({"bodyLines": lines}, personal.PROMPT, personal.SCHEMA, personal.MAX_OUTPUT_TOKENS)
        self.assertEqual(lines, before)
        with self.assertRaisesRegex(capacity.CapacityHold, "^azure_capacity_hold$"):
            capacity.half_month("x" * 6000, half.PROMPT, half.SCHEMA, half.MAX_OUTPUT_TOKENS)

    def test_changed_contract_or_unbounded_structure_fails_closed(self):
        payload = {"bodyLines": [{"id": 1, "text": "source"}]}
        for prompt, schema, output in (
                (personal.PROMPT + " changed", personal.SCHEMA, personal.MAX_OUTPUT_TOKENS),
                (personal.PROMPT, {**personal.SCHEMA, "extra": True}, personal.MAX_OUTPUT_TOKENS),
                (personal.PROMPT, personal.SCHEMA, 9999)):
            with self.assertRaisesRegex(capacity.CapacityHold, "profile_stale"):
                capacity.personal(payload, prompt, schema, output)
        for lines in ([], [{"id": True, "text": "source"}], [{"id": 2, "text": "source"}],
                      [{"id": 1, "text": "source", "extra": "not in model template"}]):
            with self.assertRaises(capacity.CapacityHold):
                capacity.personal({"bodyLines": lines}, personal.PROMPT, personal.SCHEMA, personal.MAX_OUTPUT_TOKENS)


if __name__ == "__main__":
    unittest.main()
