"use strict";

const assert = require("node:assert/strict");
const {
  dateKey,
  getDateGridColumn,
  getVisibleMonthDates,
  isDateKeyInRange
} = require("../app.js");

function visibleKeys(year, monthIndex, dateFrom, dateTo) {
  return getVisibleMonthDates(year, monthIndex, dateFrom, dateTo).map(dateKey);
}

assert.equal(
  isDateKeyInRange("2026-09-15", "2026-09-01", "2026-09-15"),
  true,
  "the end date must remain visible"
);
assert.equal(
  isDateKeyInRange("2026-09-16", "2026-09-01", "2026-09-15"),
  false,
  "the day after the end date must be excluded"
);

const firstHalf = visibleKeys(2026, 8, "2026-09-01", "2026-09-15");
assert.equal(firstHalf.length, 15, "the 1-15 range must render exactly 15 dates");
assert.deepEqual(
  firstHalf,
  Array.from({ length: 15 }, (_, index) => `2026-09-${String(index + 1).padStart(2, "0")}`)
);
assert.ok(!firstHalf.includes("2026-09-16"), "September 16 must not be rendered");

assert.deepEqual(
  visibleKeys(2026, 8, "2026-09-05", "2026-09-08"),
  ["2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08"],
  "an arbitrary contiguous range must render without surrounding dates"
);

assert.deepEqual(
  visibleKeys(2026, 9, "2026-09-05", "2026-09-08"),
  [],
  "a visible month outside the selected range must have no date cards"
);

const firstSeptemberDate = getVisibleMonthDates(
  2026,
  8,
  "2026-09-01",
  "2026-09-15"
)[0];
assert.equal(dateKey(firstSeptemberDate), "2026-09-01");
assert.equal(getDateGridColumn(firstSeptemberDate), 3, "September 1, 2026 must align to Tuesday");

const arbitraryRangeStart = getVisibleMonthDates(
  2026,
  8,
  "2026-09-05",
  "2026-09-08"
)[0];
assert.equal(getDateGridColumn(arbitraryRangeStart), 7, "September 5, 2026 must align to Saturday");

console.log("Range rendering valid: boundaries, empty month, and weekday alignment.");
