"""Standard-library regression tests. All fault injection stays in memory."""
import datetime as dt
import importlib.util
import json
import re
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evaluation = load("insights_evaluation", "evaluate-insights.py")
builder = evaluation.load_builder()


def load_shipped_insights():
    raw = (ROOT / "data" / "store-insights.js").read_text(encoding="utf-8")
    return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])


class CountingUnits(unittest.TestCase):
    def setUp(self):
        self.last = dt.date(2026, 9, 3)
        self.cell = {
            ("2026-06-04", "昼"): {"s1": {"older"}},
            ("2026-06-05", "昼"): {"s1": {"listed"}},
            ("2026-09-03", "昼"): {"s1": {"listed", "adult", "trainee"},
                                    "s2": {"adult"}},
        }

    def test_multistore_person_is_deduplicated_only_overall(self):
        coverage = builder.roster_coverage(
            self.cell, lambda date, stores: {"listed"}, self.last)
        self.assertEqual(coverage["overall"]["unit"], "date-shift")
        self.assertEqual(coverage["overall"]["cells"], 2)
        self.assertEqual(coverage["overall"]["unlistedPersonAppearances"], 2)
        self.assertEqual(coverage["storeSlots"]["cells"], 3)
        self.assertEqual(coverage["storeSlots"]["unlistedPersonAppearances"], 3)
        self.assertEqual(coverage["overall"]["cellsWithUnlisted"], 1)
        self.assertEqual(coverage["storeSlots"]["cellsWithUnlisted"], 2)
        self.assertNotIn("shiftCells", coverage)
        self.assertNotIn("unlistedPerShift", coverage)

    def test_unlisted_adult_is_not_a_trainee(self):
        coverage = builder.trainee_coverage(
            self.cell, lambda name, date: name == "trainee", self.last)
        self.assertEqual(coverage["byStore"]["s1"]["cellsWithTrainees"], 1)
        self.assertEqual(coverage["byStore"]["s2"]["cellsWithTrainees"], 0)
        self.assertEqual(coverage["byStore"]["s2"]["cells"], 1)
        self.assertEqual(coverage["byStore"]["s3"]["traineesPerCell"], 0)

    def test_empty_coverage_has_finite_zeroes(self):
        coverage = builder.roster_coverage({}, lambda *args: set(), self.last)
        self.assertEqual(coverage["overall"]["unlistedPerCell"], 0)
        self.assertEqual(coverage["storeSlots"]["cellsWithUnlistedRate"], 0)

    def test_same_day_move_summary_counts_people_not_store_slots(self):
        cell = {
            ("2026-06-04", "昼"): {"s1": {"outside"}},
            ("2026-06-04", "夜"): {"s2": {"outside"}},
            ("2026-06-05", "昼"): {"s1": {"stay", "move", "lunch-only"}},
            ("2026-06-05", "夜"): {"s1": {"stay"}, "s2": {"move", "night-only"}},
        }
        all_moves, _per_maid, summary = builder.same_day_moves(cell, builder.IDS, "2026-06-05")
        self.assertEqual(summary, {"personPairs": 2, "movedPersonPairs": 1})
        self.assertEqual(sum(row["n"] for row in all_moves.values()), 2)


class TimeSeparation(unittest.TestCase):
    def test_shifts_and_openings_use_the_same_time_boundary(self):
        for lunch, night in (("ひる", "よる"), ("昼", "夜")):
            rows = [{"date": d, "shift": s} for d, s in (
                ("2026-09-02", night), ("2026-09-03", lunch),
                ("2026-09-03", night), ("2026-09-04", lunch))]
            with self.subTest(labels=(lunch, night)):
                day = evaluation.observed_rows(rows, "2026-09-03", "昼", builder.SHIFT_LABEL)
                evening = evaluation.observed_rows(rows, "2026-09-03", "夜", builder.SHIFT_LABEL)
                self.assertEqual(day, rows[:1])
                self.assertEqual(evening, rows[:2])

    def test_target_actual_roster_and_store_only_assertions(self):
        class FakeBuilder:
            SHIFT_LABEL = builder.SHIFT_LABEL
            maid_accuracy = None
            measure_calibration = None

            def load_csv(self, name, optional=False):
                return [{"date": "2026-09-03", "shift": "昼"},
                        {"date": "2026-09-03", "shift": "夜"}]

            def build(self):
                for name in ("shifts.csv", "openings.csv"):
                    self_test.assertEqual(self.load_csv(name),
                                          [{"date": "2026-09-03", "shift": "昼"}])
                return {key: {"2026-09-03": {"夜": []}}
                        if key == self.bad_key else {}
                        for key in ("actual", "actualRoster", "actualWithoutRoster")}

        self_test = self
        for key in ("actual", "actualRoster", "actualWithoutRoster"):
            fake = FakeBuilder()
            fake.bad_key = key
            with self.subTest(key=key), self.assertRaises(AssertionError):
                list(evaluation.snapshots(fake, [{"date": "2026-09-03", "shift": "夜"}]))
            self.assertEqual(len(fake.load_csv("openings.csv")), 2, "load_csv must be restored")


class ProductionRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.insights, cls.values = evaluation.capture_build(builder)
        cls.cases = evaluation.cases_from(builder, cls.values)

    def test_generation_matches_shipping_except_timestamp(self):
        expected = load_shipped_insights()
        actual = dict(self.insights)
        expected.pop("generatedAt")
        actual.pop("generatedAt")
        self.assertEqual(actual, expected)

    def test_display_rank_adjustments_have_confirmed_debut_evidence(self):
        source = (ROOT / "data" / "schedule.js").read_text(encoding="utf-8")
        block = re.search(r"normalOrderBefore:\s*\{([^}]+)\}", source).group(1)
        milestones = builder.read_milestones()
        debuts = builder.read_debuts()
        for name, before in re.findall(r'"([^"]+)":\s*"([^"]+)"', block):
            left = milestones.get(builder.ALIAS.get(name, name), {}).get("debut") or debuts.get(name)
            right = milestones.get(builder.ALIAS.get(before, before), {}).get("debut") or debuts.get(before)
            self.assertIsNotNone(left, name)
            self.assertIsNotNone(right, before)
            self.assertLess(left, right, (name, before))

    def test_review_threshold_population(self):
        result = evaluation.thresholds(self.cases)
        n = len(self.values["cell"])
        for row in result.values():
            self.assertEqual(row["n"], n)
            self.assertLessEqual(row["hits"], n)
            self.assertEqual(row["falseFour"], row["predictedFour"] - row["correctFour"])
        baseline, expanded = result["[5, 12]"], result["[5, 12, 15]"]
        self.assertEqual(expanded["hits"] - baseline["hits"], expanded["wins"] - expanded["losses"])
        self.assertTrue(all(len(c["listed"]) == len(set(c["listed"])) for c in self.cases))
        self.assertTrue(any(builder.shown(k) in c["listed"]
                            for k in builder.read_kitchen_staff() for c in self.cases))

    def test_four_store_false_positives_are_not_all_new_losses(self):
        cases = [{"listed": list(range(n)), "stores": dict.fromkeys(range(truth))}
                 for n, truth in ((16, 4), (16, 3), (16, 2), (5, 1))]
        result = evaluation.thresholds(cases)
        self.assertEqual(result["[5, 12]"]["hits"], 2)
        self.assertEqual(result["[5, 12, 15]"], {
            "n": 4, "hits": 2, "predictedFour": 3, "correctFour": 1,
            "falseFour": 2, "wins": 1, "losses": 1})

    def test_review_coverage_populations(self):
        cov = self.insights["rosterCoverage"]
        records = [(date, stores) for (date, _shift), stores in self.values["cell"].items()
                   if cov["from"] <= date <= cov["to"]]
        missing = [set().union(*stores.values()) - self.values["listed_on"](date, stores)
                   for date, stores in records]
        missing_slots = [names - self.values["listed_on"](date, stores)
                         for date, stores in records for names in stores.values()]
        self.assertEqual((cov["overall"]["cells"], cov["overall"]["unlistedPersonAppearances"],
                          cov["overall"]["cellsWithUnlisted"]),
                         (len(missing), sum(map(len, missing)), sum(map(bool, missing))))
        self.assertEqual((cov["storeSlots"]["cells"], cov["storeSlots"]["unlistedPersonAppearances"],
                          cov["storeSlots"]["cellsWithUnlisted"]),
                         (len(missing_slots), sum(map(len, missing_slots)), sum(map(bool, missing_slots))))
        for sid, row in self.insights["traineeCoverage"]["byStore"].items():
            counts = [sum(self.values["is_trainee"](builder.shown(name), date)
                          for name in stores[sid]) for date, stores in records if sid in stores]
            expected = round(sum(n > 0 for n in counts) / len(counts), 3) if counts else 0
            self.assertEqual(row["cellsWithTraineesRate"], expected)

    def test_trainee_periods_use_the_existing_predicate_and_expire(self):
        periods = self.insights["traineePeriods"]
        self.assertEqual(periods["definition"], "is_trainee")
        for name, period in periods["byName"].items():
            for date in (period["from"], period["to"]):
                self.assertTrue(self.values["is_trainee"](name, date), (name, date))
            after = (dt.date.fromisoformat(period["to"]) + dt.timedelta(days=1)).isoformat()
            self.assertFalse(self.values["is_trainee"](name, after), name)
        for date, shifts in self.insights["actualRoster"].items():
            for entry in shifts.values():
                if "trainees" not in entry:
                    continue
                for name in {name for people in entry["stores"].values() for name in people}:
                    period = periods["byName"].get(name)
                    expected = bool(period and period["from"] <= date <= period["to"])
                    self.assertEqual(name in entry["trainees"], expected, (date, name))

    def test_known_promotion_boundaries_do_not_override_the_existing_account_policy(self):
        periods = self.insights["traineePeriods"]
        for raw_name, promoted in builder.read_promoted_at().items():
            name = builder.shown(raw_name)
            prior = (dt.date.fromisoformat(promoted) - dt.timedelta(days=1)).isoformat()
            for date in (prior, promoted):
                period = periods["byName"].get(name)
                actual = bool(period and period["from"] <= date <= period["to"])
                expected = date >= periods["from"] and self.values["is_trainee"](name, date)
                self.assertEqual(actual, expected, (name, date))

    def test_legacy_move_is_explicitly_not_production(self):
        result = evaluation.legacy_move_comparison(builder, self.values["cell"])
        self.assertEqual(result["trainingDays"], 120)
        self.assertTrue(result["oracleOpenStores"])
        multi, all_nights = result["twoOrMore"], result["allNights"]
        self.assertGreater(multi["n"], 0)
        self.assertLessEqual(multi["n"], all_nights["n"])
        for method in ("tendency", "smoothed", "zeroFallback"):
            self.assertGreaterEqual(multi[method], 0)
            self.assertLessEqual(multi[method], multi["n"])
            self.assertEqual(all_nights[method] - multi[method], all_nights["n"] - multi["n"],
                             "the additional oracle single-store cases are always correct")

    def test_exact_move_summary_matches_source_pairs(self):
        summary = self.insights["sameDayMaidMoveSummary"]
        cell = self.values["cell"]
        pairs = []
        for date in sorted({d for d, _ in cell}):
            if not summary["from"] <= date <= summary["to"]:
                continue
            lunch = {m: s for s, names in cell.get((date, "昼"), {}).items() for m in names}
            night = {m: s for s, names in cell.get((date, "夜"), {}).items() for m in names}
            pairs.extend(lunch[m] != night[m] for m in lunch.keys() & night.keys())
        self.assertEqual(summary["personPairs"], len(pairs))
        self.assertEqual(summary["movedPersonPairs"], sum(pairs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
