"use strict";

function emptyHalfMonthSchedules() {
  return {
    schemaVersion: 1, complete: false, checkedAt: null, lastSuccessAt: null,
    schedules: [], lastRun: { status: "never" }
  };
}

module.exports = { emptyHalfMonthSchedules };
