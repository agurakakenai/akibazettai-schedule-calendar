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
const rosterOrder = new Map(data.roster.map((name, index) => [name, index]));
const featuredEntries = [];

assert.ok(data, "SCHEDULE_DATA must be defined");
assert.equal(roster.size, data.roster.length, "roster contains duplicate names");

const kitchenStaff = [...(data.kitchenStaff ?? [])];
assert.ok(Array.isArray(data.kitchenStaff ?? []), "kitchenStaff must be an array");
assert.equal(
  new Set(kitchenStaff).size,
  kitchenStaff.length,
  "kitchenStaff contains duplicate names"
);
for (const name of kitchenStaff) {
  assert.ok(roster.has(name), `unknown kitchen staff "${name}"`);
}
assert.deepEqual(
  [...kitchenStaff].sort((a, b) => rosterOrder.get(a) - rosterOrder.get(b)),
  kitchenStaff,
  "kitchenStaff must follow official roster order"
);
assert.deepEqual(
  kitchenStaff,
  ["まこっちゃん", "あらた", "うる", "みりん", "けだま"],
  "kitchenStaff does not match the maids without a maid uniform on the official site"
);

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
    let previousRosterIndex = -1;

    for (const entry of day[shift]) {
      assert.ok(roster.has(entry.name), `unknown maid "${entry.name}" on ${date} ${shift}`);
      assert.ok(!names.has(entry.name), `duplicate maid "${entry.name}" on ${date} ${shift}`);
      assert.ok(
        rosterOrder.get(entry.name) > previousRosterIndex,
        `${date} ${shift} must follow official roster order`
      );
      names.add(entry.name);
      previousRosterIndex = rosterOrder.get(entry.name);

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

function shiftsFor(name) {
  return Object.entries(data.schedule).flatMap(([date, day]) =>
    [...validShifts]
      .filter((shift) => day[shift].some((entry) => entry.name === name))
      .map((shift) => `${date}|${shift}`)
  );
}

assert.deepEqual(
  shiftsFor("あめる"),
  [
    "2026-09-02|昼",
    "2026-09-02|夜",
    "2026-09-04|昼",
    "2026-09-04|夜",
    "2026-09-05|夜",
    "2026-09-06|夜",
    "2026-09-08|夜",
    "2026-09-09|昼",
    "2026-09-11|夜",
    "2026-09-12|夜",
    "2026-09-13|昼",
    "2026-09-13|夜",
    "2026-09-14|夜"
  ],
  "あめる's shifts do not match the verified schedule and corrections"
);

assert.deepEqual(
  shiftsFor("ちさと"),
  [
    "2026-09-02|昼",
    "2026-09-05|昼",
    "2026-09-08|昼",
    "2026-09-09|夜",
    "2026-09-10|夜",
    "2026-09-15|昼"
  ],
  "ちさと's shifts do not match the verified schedule"
);

assert.deepEqual(
  shiftsFor("けだま"),
  [
    "2026-09-01|昼",
    "2026-09-02|昼",
    "2026-09-05|夜",
    "2026-09-07|昼",
    "2026-09-08|昼",
    "2026-09-10|昼",
    "2026-09-12|昼",
    "2026-09-14|昼"
  ],
  "けだま's shifts do not match the schedule published in her X profile"
);

assert.deepEqual(
  shiftsFor("ひかり"),
  [
    "2026-09-02|昼",
    "2026-09-02|夜",
    "2026-09-03|夜",
    "2026-09-08|昼",
    "2026-09-11|夜",
    "2026-09-12|昼",
    "2026-09-14|夜",
    "2026-09-15|夜"
  ],
  "ひかり's shifts do not match her published 9月前半 post"
);

console.log(
  `Schedule valid: ${data.roster.length} rostered maids ` +
    `(${kitchenStaff.length} kitchen), ` +
    `${Object.keys(data.schedule).length} scheduled dates, ${featuredEntries.length} featured shifts.`
);
