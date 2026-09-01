"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const schedulePath = path.join(__dirname, "..", "data", "schedule.js");
const source = fs.readFileSync(schedulePath, "utf8");
const context = { window: {} };
vm.runInNewContext(source, context, { filename: schedulePath });

const data = context.window.SCHEDULE_DATA;
const validShifts = new Set(["昼", "夜"]);
const roster = new Set(data.roster);
const featuredEntries = [];

assert.ok(data, "SCHEDULE_DATA must be defined");
assert.equal(roster.size, data.roster.length, "roster contains duplicate names");

for (const [date, day] of Object.entries(data.schedule)) {
  assert.match(date, /^\d{4}-\d{2}-\d{2}$/, `invalid date format: ${date}`);
  const parsed = new Date(`${date}T00:00:00Z`);
  assert.equal(
    parsed.toISOString().slice(0, 10),
    date,
    `invalid calendar date: ${date}`
  );

  for (const shift of Object.keys(day)) {
    assert.ok(validShifts.has(shift), `invalid shift "${shift}" on ${date}`);
  }

  for (const shift of validShifts) {
    assert.ok(Array.isArray(day[shift]), `${date} ${shift} must be an array`);
    const names = new Set();

    for (const entry of day[shift]) {
      assert.ok(roster.has(entry.name), `unknown maid "${entry.name}" on ${date} ${shift}`);
      assert.ok(!names.has(entry.name), `duplicate maid "${entry.name}" on ${date} ${shift}`);
      names.add(entry.name);

      if (entry.featured) {
        assert.equal(typeof entry.eventLabel, "string", `featured entry needs an event label`);
        assert.ok(entry.eventLabel.trim(), `featured entry needs a non-empty event label`);
        featuredEntries.push(`${date}|${shift}|${entry.name}|${entry.eventLabel}`);
      } else {
        assert.equal(
          entry.eventLabel,
          undefined,
          `non-featured entry has an event label on ${date} ${shift}`
        );
      }
    }
  }
}

assert.deepEqual(
  featuredEntries,
  [
    "2026-09-01|夜|もなか|1周年",
    "2026-09-03|昼|ちま|生誕",
    "2026-09-08|昼|あらた|7周年",
    "2026-09-08|夜|あらた|7周年",
    "2026-09-13|夜|あくび|生誕"
  ],
  "featured event entries do not match the confirmed schedule"
);

assert.ok(
  data.schedule["2026-09-01"]["夜"].some((entry) => entry.name === "こえび"),
  "Sep 1 night must include こえび's corrected shift"
);

console.log(
  `Schedule valid: ${data.roster.length} rostered maids, ` +
    `${Object.keys(data.schedule).length} scheduled dates, ${featuredEntries.length} featured shifts.`
);
