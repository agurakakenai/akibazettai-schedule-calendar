import copy
import datetime as dt
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("shared_work_timing", Path(__file__).parents[1] / "work-timing.py")
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)


def source(hour=10):
    created = f"2026-09-06T{hour:02d}:00:00Z"
    milliseconds = int(dt.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000)
    tid = str(((milliseconds - 1288834974657) << 22) + 1)
    return {"id": tid, "url": f"https://x.com/timing_fixture/status/{tid}",
            "name": "fixture", "authorId": "12345", "authorScreenName": "timing_fixture",
            "createdAt": created}


def fact(**changes):
    return {"serviceDate": "2026-09-07", "shift": timing.DAY, "boundary": "end",
            "status": "set", "qualifier": "long", "explicitTime": None, **changes}


class WorkTimingTests(unittest.TestCase):
    def test_exact_source_fields_and_word_without_invented_clock(self):
        post = source()
        value = timing.bind([fact()], post, "half-month-schedule")
        self.assertIs(timing.validate(value, owner=post, days={"2026-09-07": [timing.DAY]}), value)
        self.assertIsNone(value["facts"][0]["explicitTime"])
        for mutate in (
                lambda x: x.update(raw="private"),
                lambda x: x["facts"][0].update(evidence="private"),
                lambda x: x["facts"][0]["source"].update(bodyHash="private"),
                lambda x: x["facts"][0]["source"].update(url="javascript:alert(1)"),
                lambda x: x["facts"][0]["source"].update(id="123"),
                lambda x: x["facts"][0]["source"].update(createdAt="2026-09-06T11:00:00Z")):
            bad = copy.deepcopy(value)
            mutate(bad)
            with self.assertRaises(ValueError):
                timing.validate(bad)

    def test_roles_null_and_nonmapping_times_are_not_lateness(self):
        for qualifier, (shift, boundary) in timing.QUALIFIERS.items():
            self.assertEqual(timing.validate_fact(fact(qualifier=qualifier, shift=shift, boundary=boundary))["qualifier"],
                             qualifier)
        self.assertEqual(timing.validate_fact(fact(qualifier=None, explicitTime="17:00"))["explicitTime"], "17:00")
        for bad in (fact(boundary="start"), fact(shift=timing.NIGHT),
                    fact(qualifier=None), fact(explicitTime="24:00"),
                    fact(boundary="break"), fact(status="pending"),
                    fact(status="withdrawn"), fact(qualifier=[])):
            with self.assertRaises(ValueError):
                timing.validate_fact(bad)
        for status in ("withdrawn", "conflict"):
            self.assertEqual(timing.validate_fact(fact(status=status, qualifier=None))["status"], status)

    def test_missing_channel_and_other_side_preserve_original_provenance(self):
        old = timing.bind([fact(), fact(shift=timing.NIGHT, boundary="start", qualifier="early")],
                          source(), "half-month-schedule")
        before = copy.deepcopy(old)
        for update in (None, {"schemaVersion": 1, "facts": []}):
            self.assertEqual(timing.merge(old, update), old)
        changed = timing.bind([fact(qualifier=None, explicitTime="17:00")], source(11), "half-month-schedule")
        merged = timing.merge(old, changed)
        day = next(item for item in merged["facts"] if item["shift"] == timing.DAY)
        night = next(item for item in merged["facts"] if item["shift"] == timing.NIGHT)
        self.assertEqual(day["explicitTime"], "17:00")
        self.assertEqual(day["source"]["id"], source(11)["id"])
        self.assertEqual(night["source"]["id"], source()["id"])
        self.assertEqual(old, before)
        self.assertEqual(timing.merge(merged, old), merged)
        filtered = timing.merge(old, changed, days={"2026-09-07": [timing.DAY]})
        self.assertEqual(len(filtered["facts"]), 1)

    def test_explicit_withdrawal_persists_and_owner_scope_cannot_change(self):
        post = source()
        old = timing.bind([fact()], post, "personal-work-post")
        withdrawal = timing.bind([fact(status="withdrawn", qualifier=None)], post, "personal-work-post")
        result = timing.merge(old, withdrawal)
        self.assertEqual(timing.merge(result, None), result)
        self.assertEqual(result["facts"][0]["status"], "withdrawn")
        for kwargs in ({"owner": source(11)}, {"date": "2026-09-08"},
                       {"days": {"2026-09-07": [timing.NIGHT]}}, {"source_kind": "half-month-schedule"}):
            with self.assertRaises(ValueError):
                timing.validate(result, **kwargs)
        duplicate = copy.deepcopy(result)
        duplicate["facts"] *= 2
        with self.assertRaises(ValueError):
            timing.validate(duplicate)

    def test_compact_denials_preserve_their_target_and_other_facts(self):
        date = "2026-09-07"
        excluded = timing.expand_compact({"shift": timing.NIGHT, "kind": "not-early", "time": None}, date)
        self.assertEqual(excluded["status"], "excluded")
        self.assertEqual(excluded["qualifier"], "early")
        self.assertIsNone(excluded["explicitTime"])
        clock = timing.expand_compact({"shift": timing.NIGHT, "kind": "not-time", "time": "18:00"}, date)
        self.assertEqual(clock["status"], "excluded")
        self.assertEqual(clock["explicitTime"], "18:00")
        original = timing.bind([fact(shift=timing.NIGHT, boundary="start", qualifier="late")],
                               source(), "half-month-schedule")
        denial = timing.bind([excluded, clock], source(11), "half-month-schedule")
        combined = timing.merge(original, denial)
        self.assertEqual(len(combined["facts"]), 3)
        self.assertEqual(combined["facts"][0]["qualifier"], "late")
        self.assertEqual(timing.merge(combined, None), combined)
        self.assertEqual(len({timing.scope(item) for item in combined["facts"]}), 1)
        for invalid in (
                {"shift": timing.NIGHT, "kind": "not-time", "time": None},
                {"shift": timing.NIGHT, "kind": "not-early", "time": "16:00"},
                {"shift": timing.DAY, "kind": "not-early", "time": None},
                {"shift": timing.NIGHT, "kind": "normal", "time": None}):
            with self.assertRaises(ValueError):
                timing.expand_compact(invalid, date)

    def test_aggregate_limit_never_discards_old_values_or_exclusions(self):
        facts = [fact(shift=timing.NIGHT, boundary="start", qualifier="late")]
        facts.extend(fact(shift=timing.NIGHT, boundary="start", status="excluded", qualifier=None,
                          explicitTime=f"{minute // 60:02d}:{minute % 60:02d}")
                     for minute in range(timing.MAX_FACTS - 1))
        previous = timing.bind(facts, source(), "personal-work-post")
        addition = timing.bind([fact(shift=timing.NIGHT, boundary="start", status="excluded",
                                     qualifier=None, explicitTime="23:59")], source(), "personal-work-post")
        before = copy.deepcopy(previous)
        with self.assertRaisesRegex(timing.WorkTimingLimitError, "work_timing_storage_limit"):
            timing.merge(previous, addition)
        self.assertEqual(previous, before)

    def test_same_post_affirmation_replaces_only_matching_previous_denials(self):
        post = source()
        early = fact(shift=timing.NIGHT, boundary="start", status="excluded", qualifier="early")
        late = fact(shift=timing.NIGHT, boundary="start", status="excluded", qualifier="late")
        before = timing.bind([early, late], post, "personal-work-post")
        original = copy.deepcopy(before)
        affirmative = timing.bind([fact(shift=timing.NIGHT, boundary="start", qualifier=None,
                                        explicitTime="18:00")], post, "personal-work-post")
        updated = timing.merge(before, affirmative)
        self.assertEqual(len(updated["facts"]), 2)
        self.assertTrue(any(row["status"] == "excluded" and row["qualifier"] == "early" for row in updated["facts"]))
        self.assertFalse(any(row["status"] == "excluded" and row["qualifier"] == "late" for row in updated["facts"]))
        self.assertEqual(before, original)
        co_emitted = timing.bind([*[{key: row[key] for key in timing.FACT_FIELDS}
                                    for row in affirmative["facts"]], late], post, "personal-work-post")
        self.assertTrue(any(row["status"] == "excluded" and row["qualifier"] == "late"
                            for row in timing.merge(before, co_emitted)["facts"]))
        other = timing.bind([fact(shift=timing.NIGHT, boundary="start", qualifier="late")],
                            source(11), "half-month-schedule")
        retained = timing.merge(timing.bind([late], post, "half-month-schedule"), other)
        self.assertEqual(len(retained["facts"]), 2, "different original sources keep their provenance")


if __name__ == "__main__":
    unittest.main()
