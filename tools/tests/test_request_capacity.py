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
