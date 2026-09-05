"""Standard-library regression tests. All fault injection stays in memory."""
import contextlib
import datetime as dt
from decimal import Decimal
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load("readme_checker", "check-readme.py")
evaluation = load("insights_evaluation", "evaluate-insights.py")
builder = evaluation.load_builder()


class ReadmeClaims(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = checker.load_readme()
        cls.data = checker.load_insights()
        counts = cls.data["openCountPerShift"]["昼"]
        cls.lunch_n = sum(counts.values())
        cls.above = sum(n for k, n in counts.items() if int(k) > 3)
        cls.lunch_claim = f"昼{cls.lunch_n}件の記録に{cls.above}件"
        cls.wrong_lunch_claim = f"昼{cls.lunch_n + 1}件の記録に{cls.above}件"
        cls.roster_n = cls.data["schedulePending"]["rostered"]

    def test_current_document(self):
        self.assertEqual(checker.check(self.text, self.data), [])
        self.assertEqual(self.run_main(self.text)[0], 0)

    def run_main(self, text):
        output = io.StringIO()
        with (mock.patch.object(checker, "load_readme", return_value=text),
              mock.patch.object(checker, "load_insights", return_value=self.data),
              contextlib.redirect_stdout(output)):
            code = checker.main()
        return code, output.getvalue()

    def test_main_rejects_wrong_roster_count_with_whitespace(self):
        for space in (" ", "\t", "\u3000", "\n"):
            with self.subTest(space=repr(space)):
                changed = self.text.replace(
                    f"在籍{self.roster_n}名",
                    f"在籍{space}{self.roster_n - 2}{space}名", 1)
                self.assertNotEqual(changed, self.text)
                code, output = self.run_main(changed)
                self.assertEqual(code, 1)
                self.assertIn("現在の在籍人数", output)

    def test_main_accepts_correct_roster_count_with_whitespace(self):
        for prefix in ("在籍", "roster は"):
            with self.subTest(prefix=prefix):
                changed = self.text.replace(f"在籍{self.roster_n}名",
                                            f"{prefix} {self.roster_n} 名", 1)
                self.assertEqual(self.run_main(changed)[0], 0)

    def test_main_ignores_same_named_target_under_historical_heading(self):
        title = "### 開く店の数が表の上限に当たったら、そう断ります"
        for claim in (self.lunch_claim, self.wrong_lunch_claim):
            with self.subTest(claim=claim):
                changed = self.text + f"\n## 過去測定: 旧表\n{title}\n{claim}。\n"
                self.assertEqual(self.run_main(changed)[0], 0)

    def test_main_does_not_accept_target_only_inside_history(self):
        title = "### 開く店の数が表の上限に当たったら、そう断ります"
        changed = self.text.replace(title, "### 対象外の説明", 1)
        self.assertNotEqual(changed, self.text)
        changed += f"\n## 過去測定: 旧表\n{title}\n{self.lunch_claim}。\n"
        code, output = self.run_main(changed)
        self.assertEqual(code, 1)
        self.assertIn("0件", output)

    def test_main_still_rejects_duplicate_current_target(self):
        title = "### 開く店の数が表の上限に当たったら、そう断ります"
        changed = self.text + f"\n## 現在の追記\n{title}\n{self.lunch_claim}。\n"
        self.assertEqual(self.run_main(changed)[0], 1)

    def test_wrong_lunch_body_cannot_borrow_historical_360(self):
        changed = self.text.replace(self.lunch_claim, self.wrong_lunch_claim, 1)
        self.assertNotEqual(changed, self.text)
        changed += f"\n## 過去測定: 対照\n{self.lunch_claim}という記録でした。\n"
        self.assertTrue(checker.check(changed, self.data))

    def test_all_lunch_numbers_changed_control(self):
        self.assertTrue(checker.check(
            self.text.replace(str(self.lunch_n), str(self.lunch_n + 1)), self.data))

    def test_arbitrary_roster_errors_not_only_neighbors(self):
        for n in (self.roster_n - 2, self.roster_n - 1, self.roster_n + 1, self.roster_n * 10):
            with self.subTest(n=n):
                changed = self.text.replace(f"在籍{self.roster_n}名", f"在籍{n}名", 1)
                self.assertNotEqual(changed, self.text)
                self.assertTrue(checker.check(changed, self.data))

    def test_new_conflicting_current_claim_fails(self):
        self.assertTrue(checker.check(
            self.text + f"\n## 現在の補足\n在籍{self.roster_n - 2}名。\n", self.data))

    def test_explicit_historical_roster_is_not_current(self):
        self.assertEqual(checker.check(
            self.text + f"\n## 過去測定: 名簿\n在籍{self.roster_n - 2}名。\n", self.data), [])

    def test_missing_and_duplicated_sections_fail(self):
        title = "#### 未掲載者の集計単位"
        for text in (self.text.replace(title, "#### 別の節"), self.text + "\n" + title + "\n"):
            with self.subTest():
                self.assertTrue(checker.check(text, self.data))

    def test_every_coverage_column_is_checked(self):
        scope = checker.section(self.text, "未掲載者の集計単位")
        original = next(line for line in scope.splitlines() if line.startswith("| 全店ユニーク |"))
        self.assert_row_mutations_fail(original, start=2)

    def test_every_trainee_column_is_checked(self):
        scope = checker.section(self.text, "店舗枠の見習い率")
        original = next(line for line in scope.splitlines() if line.startswith("| 1号店 |"))
        self.assert_row_mutations_fail(original, start=2)

    def test_every_unlisted_store_column_is_scoped_and_checked(self):
        scope = checker.section(self.text, "店舗ごとの未掲載率")
        for sid in ("s1", "s2", "s3", "s4"):
            original = next(line for line in scope.splitlines()
                            if line.startswith(f"| {sid[1:]}号店 |"))
            self.assert_row_mutations_fail(original, start=2)

    def test_every_move_count_and_rate_is_checked_in_its_section(self):
        move = self.data["sameDayMaidMoveSummary"]
        n, moved = move["personPairs"], move["movedPersonPairs"]
        percent = round(100 * moved / n, 1) if n else 0
        original = f"昼夜とも出た{n}人回のうち{moved}人回（{percent}%）が別の店"
        self.assertIn(original, self.text)
        for values in ((n + 1, moved, percent), (n, moved + 1, percent), (n, moved, percent + 1)):
            with self.subTest(values=values):
                wrong = f"昼夜とも出た{values[0]}人回のうち{values[1]}人回（{values[2]}%）が別の店"
                changed = self.text.replace(original, wrong, 1)
                changed += f"\n## 過去測定: 移動\n{original}\n"
                self.assertTrue(checker.check(changed, self.data))

    def assert_row_mutations_fail(self, original, start):
        fields = original.split("|")
        for index in range(start, len(fields) - 1):
            with self.subTest(column=index):
                changed_fields = list(fields)
                value = fields[index].strip()
                if value == "date-shift":
                    value = "date-shift-store"
                else:
                    percent = "%" if value.endswith("%") else ""
                    value = str(Decimal(value.rstrip("%")) + 1) + percent
                changed_fields[index] = " " + value + " "
                changed = self.text.replace(original, "|".join(changed_fields))
                self.assertTrue(checker.check(changed, self.data))

    def test_duplicate_correct_claim_does_not_hide_wrong_claim(self):
        changed = self.text.replace(self.lunch_claim,
                                   self.wrong_lunch_claim + "。" + self.lunch_claim, 1)
        self.assertTrue(checker.check(changed, self.data))

    def test_code_fence_cannot_supply_a_missing_claim(self):
        changed = self.text.replace(self.lunch_claim, "該当する記録", 1)
        changed = changed.replace("### 開く店の数が表の上限に当たったら、そう断ります",
            "### 開く店の数が表の上限に当たったら、そう断ります\n```\n" + self.lunch_claim + "\n```", 1)
        self.assertTrue(checker.check(changed, self.data))

    def test_nested_history_does_not_swallow_current_section(self):
        changed = self.text + (
            f"\n## 過去測定: 名簿\n在籍{self.roster_n - 2}名。\n"
            f"### 過去測定: 補足\n在籍{self.roster_n - 1}名。\n"
            f"## 現在の追記\n在籍{self.roster_n - 3}名。\n")
        self.assertTrue(checker.check(changed, self.data))


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
        expected = checker.load_insights()
        actual = dict(self.insights)
        expected.pop("generatedAt")
        actual.pop("generatedAt")
        self.assertEqual(actual, expected)

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
