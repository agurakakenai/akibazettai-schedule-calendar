"use strict";

const assert = require("node:assert/strict");
const { getTokyoDateDefaults } = require("../app.js");

const cases = [
  {
    name: "Tokyo 1st after UTC is still in the prior month",
    now: "2026-08-31T15:00:00.000Z",
    expected: {
      year: 2026,
      month: 9,
      dateFrom: "2026-09-01",
      dateTo: "2026-09-30"
    }
  },
  {
    name: "15th still shows the entire month",
    now: "2026-09-15T14:59:59.999Z",
    expected: {
      year: 2026,
      month: 9,
      dateFrom: "2026-09-01",
      dateTo: "2026-09-30"
    }
  },
  {
    name: "16th still starts on the 1st",
    now: "2026-09-15T15:00:00.000Z",
    expected: {
      year: 2026,
      month: 9,
      dateFrom: "2026-09-01",
      dateTo: "2026-09-30"
    }
  },
  {
    name: "month end remains a full-month range",
    now: "2026-09-30T14:59:59.999Z",
    expected: {
      year: 2026,
      month: 9,
      dateFrom: "2026-09-01",
      dateTo: "2026-09-30"
    }
  },
  {
    name: "31-day month uses the 31st",
    now: "2026-01-20T03:00:00.000Z",
    expected: {
      year: 2026,
      month: 1,
      dateFrom: "2026-01-01",
      dateTo: "2026-01-31"
    }
  },
  {
    name: "non-leap February ends on the 28th",
    now: "2027-02-20T03:00:00.000Z",
    expected: {
      year: 2027,
      month: 2,
      dateFrom: "2027-02-01",
      dateTo: "2027-02-28"
    }
  },
  {
    name: "leap-year February ends on the 29th",
    now: "2028-02-20T03:00:00.000Z",
    expected: {
      year: 2028,
      month: 2,
      dateFrom: "2028-02-01",
      dateTo: "2028-02-29"
    }
  }
];

for (const testCase of cases) {
  assert.deepEqual(
    getTokyoDateDefaults(new Date(testCase.now)),
    testCase.expected,
    testCase.name
  );
}

console.log(`Date defaults valid: ${cases.length} Tokyo boundary cases.`);
