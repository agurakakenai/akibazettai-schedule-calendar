"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadWindowGlobal(file, key) {
  const context = { window: {} };
  vm.runInNewContext(fs.readFileSync(file, "utf8"), context, { filename: file });
  // vm の別レルムで作られた配列は deepEqual が参照比較で落ちるため、素の JSON に戻す。
  return JSON.parse(JSON.stringify(context.window[key]));
}

const dataDir = path.join(__dirname, "..", "data");
const insightsPath = path.join(dataDir, "store-insights.js");
const schedulePath = path.join(dataDir, "schedule.js");

assert.ok(fs.existsSync(insightsPath), "data/store-insights.js must exist");

const insights = loadWindowGlobal(insightsPath, "STORE_INSIGHTS");
const schedule = loadWindowGlobal(schedulePath, "SCHEDULE_DATA");

assert.ok(insights, "STORE_INSIGHTS must be defined");

const storeIdList = ["s1", "s2", "s3", "s4"];
const storeIds = new Set(storeIdList);
const weekdayKeys = ["0", "1", "2", "3", "4", "5", "6"];

function isRate(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function assertRate(value, label) {
  assert.ok(isRate(value), `${label} must be a number between 0 and 1, got ${value}`);
}

function assertStoreRates(table, label) {
  assert.ok(table && typeof table === "object", `${label} must be an object`);
  assert.deepEqual(
    Object.keys(table).sort(),
    [...storeIdList],
    `${label} must be keyed by s1 through s4`
  );
  for (const id of storeIdList) {
    assertRate(table[id], `${label}.${id}`);
  }
}

function assertDistribution(table, label, allowedStates, tolerance = 0.011) {
  assert.ok(table && typeof table === "object", `${label} must be an object`);
  assert.ok(Object.keys(table).length > 0, `${label} must not be empty`);
  let total = 0;
  for (const [key, value] of Object.entries(table)) {
    assert.ok(allowedStates.has(key), `${label} has unknown state "${key}"`);
    assertRate(value, `${label}.${key}`);
    total += value;
  }
  assert.ok(
    Math.abs(total - 1) <= tolerance,
    `${label} must sum to 1 (±${tolerance}), got ${total}`
  );
}

// --- 店舗 ---------------------------------------------------------------
assert.ok(Array.isArray(insights.stores), "stores must be an array");
assert.equal(insights.stores.length, 4, "stores must contain exactly 4 shops");
assert.deepEqual(
  insights.stores.map((store) => store.id),
  [...storeIdList],
  "store ids must be s1 through s4 in order"
);
for (const store of insights.stores) {
  assert.ok(
    typeof store.short === "string" && store.short.trim(),
    `${store.id} needs a non-empty short label`
  );
  assert.ok(
    typeof store.name === "string" && store.name.trim(),
    `${store.id} needs a non-empty name`
  );
}

// --- シフトと曜日の起点 -------------------------------------------------
assert.deepEqual(insights.shifts, ["昼", "夜"], "shifts must be 昼 and 夜");
const shiftNames = insights.shifts;
assert.equal(
  insights.weekdayOrigin,
  "sunday",
  'weekdayOrigin must be "sunday" so app.js can use Date#getDay() directly'
);

// --- 営業率 -------------------------------------------------------------
assert.deepEqual(
  Object.keys(insights.baseOpenRate).sort(),
  [...shiftNames].sort(),
  "baseOpenRate must be keyed by shift"
);
for (const shift of shiftNames) {
  assertStoreRates(insights.baseOpenRate[shift], `baseOpenRate.${shift}`);
}

assert.deepEqual(
  Object.keys(insights.weekdayOpenRate).sort(),
  [...shiftNames].sort(),
  "weekdayOpenRate must be keyed by shift first"
);
for (const shift of shiftNames) {
  const byWeekday = insights.weekdayOpenRate[shift];
  assert.deepEqual(
    Object.keys(byWeekday).sort(),
    [...weekdayKeys],
    `weekdayOpenRate.${shift} must be keyed by '0' through '6'`
  );
  for (const weekday of weekdayKeys) {
    assertStoreRates(byWeekday[weekday], `weekdayOpenRate.${shift}.${weekday}`);
  }
}

for (const shift of shiftNames) {
  const headcount = insights.typicalHeadcount[shift];
  assert.ok(
    headcount && typeof headcount === "object",
    `typicalHeadcount.${shift} must be an object`
  );
  for (const id of storeIdList) {
    assert.ok(
      typeof headcount[id] === "number" && headcount[id] > 0,
      `typicalHeadcount.${shift}.${id} must be a positive number`
    );
  }
}

// --- 営業パターン -------------------------------------------------------
const patternStates = new Set(["dayOnly", "nightOnly", "allDay", "closed"]);
for (const id of storeIdList) {
  const pattern = insights.shiftPattern[id];
  assert.ok(pattern, `shiftPattern.${id} is required`);
  assertDistribution(
    Object.fromEntries(Object.entries(pattern).filter(([key]) => patternStates.has(key))),
    `shiftPattern.${id}`,
    patternStates
  );
  assertRate(pattern.partialShare, `shiftPattern.${id}.partialShare`);
  assert.ok(
    Number.isInteger(pattern.openDays) && pattern.openDays >= 0,
    `shiftPattern.${id}.openDays must be a non-negative integer`
  );

  const split = insights.shiftSplitGivenOpen[id];
  assert.ok(split, `shiftSplitGivenOpen.${id} is required`);
  assertDistribution(
    { dayOnly: split.dayOnly, nightOnly: split.nightOnly, allDay: split.allDay },
    `shiftSplitGivenOpen.${id}`,
    new Set(["dayOnly", "nightOnly", "allDay"])
  );
  assert.ok(
    Number.isInteger(split.n) && split.n > 0,
    `shiftSplitGivenOpen.${id}.n must be a positive integer`
  );
}

for (const shift of shiftNames) {
  const counts = insights.openCountPerShift[shift];
  assert.ok(counts && typeof counts === "object", `openCountPerShift.${shift} must be an object`);
  for (const [key, value] of Object.entries(counts)) {
    assert.match(key, /^[0-4]$/, `openCountPerShift.${shift} has unexpected key "${key}"`);
    assert.ok(
      Number.isInteger(value) && value >= 0,
      `openCountPerShift.${shift}.${key} must be a non-negative integer`
    );
  }
}

// --- ローテーション -----------------------------------------------------
const groupStates = new Set(["s2", "s3", "none", "both"]);
const openStates = new Set(["open", "closed"]);

function assertTransitions(table, label, states) {
  assert.ok(table && typeof table === "object", `${label} must be an object`);
  for (const [from, distribution] of Object.entries(table)) {
    assert.ok(states.has(from), `${label} has unknown source state "${from}"`);
    assertDistribution(distribution, `${label}.${from}`, states);
  }
}

assertTransitions(insights.rotation.nextDayByDay, "rotation.nextDayByDay", groupStates);
assertTransitions(insights.rotation.nextDayByDayS4, "rotation.nextDayByDayS4", openStates);
assertTransitions(insights.rotation.sameDay, "rotation.sameDay", groupStates);
assertTransitions(insights.rotation.sameDayS4, "rotation.sameDayS4", openStates);
for (const shift of shiftNames) {
  assertTransitions(insights.rotation.nextDay[shift], `rotation.nextDay.${shift}`, groupStates);
  assertTransitions(insights.rotation.nextDayS4[shift], `rotation.nextDayS4.${shift}`, openStates);
}

// 「同じ日に2号店と3号店が入れ替わることはない」という UI の説明の根拠。
assert.ok(
  (insights.rotation.sameDay.s2?.s3 ?? 0) <= 0.05,
  "sameDay.s2 must show that 昼2号店 rarely flips to 夜3号店"
);
assert.ok(
  (insights.rotation.sameDay.s3?.s2 ?? 0) <= 0.05,
  "sameDay.s3 must show that 昼3号店 rarely flips to 夜2号店"
);

// --- 精度 ---------------------------------------------------------------
const accuracy = insights.accuracy;
assert.ok(accuracy && typeof accuracy === "object", "accuracy is required");
for (const key of ["maidStoreGivenOpen", "maidStoreTop1", "maidStoreTop2"]) {
  assertRate(accuracy[key], `accuracy.${key}`);
}
assert.ok(
  accuracy.maidStoreTop2 >= accuracy.maidStoreTop1,
  "accuracy.maidStoreTop2 must not be worse than maidStoreTop1"
);
for (const bucket of [...shiftNames, "日"]) {
  const measured = accuracy.nextDayByShift[bucket];
  assert.ok(measured, `accuracy.nextDayByShift.${bucket} is required`);
  assertRate(measured.group, `accuracy.nextDayByShift.${bucket}.group`);
  assertRate(measured.s4, `accuracy.nextDayByShift.${bucket}.s4`);
  assert.ok(
    Number.isInteger(measured.n) && measured.n > 0,
    `accuracy.nextDayByShift.${bucket}.n must be a positive integer`
  );
}
assertRate(
  accuracy.nextDayByShift["日"].groupBaseline,
  "accuracy.nextDayByShift.日.groupBaseline"
);

// --- 実績 ---------------------------------------------------------------
const actualEntries = Object.entries(insights.actual);
assert.ok(actualEntries.length > 0, "actual must contain at least one date");
for (const [date, record] of actualEntries) {
  assert.match(date, /^\d{4}-\d{2}-\d{2}$/, `invalid date format in actual: ${date}`);
  assert.equal(
    new Date(`${date}T00:00:00Z`).toISOString().slice(0, 10),
    date,
    `invalid calendar date in actual: ${date}`
  );
  assert.ok(Object.keys(record).length > 0, `actual.${date} must record at least one shift`);
  for (const [shift, ids] of Object.entries(record)) {
    assert.ok(shiftNames.includes(shift), `actual.${date} has unknown shift "${shift}"`);
    assert.ok(Array.isArray(ids), `actual.${date}.${shift} must be an array`);
    assert.equal(new Set(ids).size, ids.length, `actual.${date}.${shift} repeats a store`);
    for (const id of ids) {
      assert.ok(storeIds.has(id), `actual.${date}.${shift} has unknown store id "${id}"`);
    }
  }
}

// --- メイド別の傾向 -----------------------------------------------------
const tendencyNames = Object.keys(insights.maidTendency);
assert.deepEqual(
  [...tendencyNames].sort(),
  [...schedule.roster].sort(),
  "maidTendency keys must match data/schedule.js roster exactly"
);

const handlePattern = /^[A-Za-z0-9_]{1,15}$/;
let withTendency = 0;

for (const name of tendencyNames) {
  const tendency = insights.maidTendency[name];
  if (tendency === null) {
    continue;
  }
  withTendency += 1;

  assertStoreRates(tendency.pickRate, `maidTendency.${name}.pickRate`);
  assertStoreRates(tendency.share, `maidTendency.${name}.share`);
  assertDistribution(tendency.share, `maidTendency.${name}.share`, storeIds);

  for (const shift of shiftNames) {
    assertStoreRates(
      tendency.pickRateByShift[shift],
      `maidTendency.${name}.pickRateByShift.${shift}`
    );
    assertStoreRates(
      tendency.shareByShift[shift],
      `maidTendency.${name}.shareByShift.${shift}`
    );
    assertDistribution(
      tendency.shareByShift[shift],
      `maidTendency.${name}.shareByShift.${shift}`,
      storeIds
    );
    const samples = tendency.sampleByShift[shift];
    assert.ok(samples, `maidTendency.${name}.sampleByShift.${shift} is required`);
    for (const id of storeIdList) {
      assert.ok(
        Number.isInteger(samples[id]) && samples[id] >= 0,
        `maidTendency.${name}.sampleByShift.${shift}.${id} must be a non-negative integer`
      );
    }
  }

  assert.ok(Array.isArray(tendency.likely), `maidTendency.${name}.likely must be an array`);
  assert.equal(tendency.likely.length, 2, `maidTendency.${name}.likely must list 2 stores`);
  assert.equal(
    new Set(tendency.likely).size,
    2,
    `maidTendency.${name}.likely must not repeat a store`
  );
  for (const id of tendency.likely) {
    assert.ok(storeIds.has(id), `maidTendency.${name}.likely has unknown store id "${id}"`);
  }
  assert.equal(
    tendency.share[tendency.likely[0]],
    Math.max(...storeIdList.map((id) => tendency.share[id])),
    `maidTendency.${name}.likely[0] must be the highest-share store`
  );
  assert.ok(
    tendency.share[tendency.likely[0]] >= tendency.share[tendency.likely[1]],
    `maidTendency.${name}.likely must be ordered by share`
  );

  assert.ok(storeIds.has(tendency.home), `maidTendency.${name}.home must be a known store id`);
  assert.ok(
    Number.isInteger(tendency.workShifts) && tendency.workShifts > 0,
    `maidTendency.${name}.workShifts must be a positive integer`
  );
  assertRate(tendency.nightShare, `maidTendency.${name}.nightShare`);
  if (tendency.x !== null && tendency.x !== undefined) {
    assert.match(tendency.x, handlePattern, `maidTendency.${name}.x must look like an X handle`);
  }
}

// --- サイト未掲載のメンバー ---------------------------------------------
const unlisted = insights.unlistedMaids ?? {};
const knownStatuses = new Set(["本人確認済み", "公式サイト", "卒業済み"]);
const memberStatuses = new Set(["active", "graduated"]);

assert.match(
  insights.shiftDataFrom,
  /^\d{4}-\d{2}-\d{2}$/,
  "shiftDataFrom is required so the UI can say how far back firstSeen can reach"
);

for (const [name, info] of Object.entries(unlisted)) {
  assert.ok(
    !schedule.roster.includes(name),
    `unlistedMaids must not repeat a rostered maid: ${name}`
  );
  assert.ok(
    memberStatuses.has(info.status),
    `unlistedMaids.${name}.status must be "active" or "graduated", got ${info.status}`
  );
  assert.ok(
    Number.isInteger(info.recentShifts) && info.recentShifts > 0,
    `unlistedMaids.${name}.recentShifts must be a positive integer`
  );
  assert.ok(
    Number.isInteger(info.recentShifts31) && info.recentShifts31 >= 0,
    `unlistedMaids.${name}.recentShifts31 must be a non-negative integer`
  );
  assert.ok(
    info.recentShifts31 <= info.recentShifts,
    `unlistedMaids.${name}.recentShifts31 must not exceed recentShifts`
  );
  for (const flag of ["promoted", "likelyNew", "hasPublicAccount"]) {
    assert.equal(
      typeof info[flag],
      "boolean",
      `unlistedMaids.${name}.${flag} must be a boolean`
    );
  }
  assert.ok(
    Array.isArray(info.otherAccounts),
    `unlistedMaids.${name}.otherAccounts must be an array`
  );
  for (const account of info.otherAccounts) {
    assert.equal(typeof account, "string", `unlistedMaids.${name}.otherAccounts must hold strings`);
  }

  assertStoreRates(info.pickRate, `unlistedMaids.${name}.pickRate`);
  assertDistribution(info.share, `unlistedMaids.${name}.share`, storeIds);
  assert.ok(storeIds.has(info.home), `unlistedMaids.${name}.home must be a known store id`);
  assert.equal(info.likely.length, 2, `unlistedMaids.${name}.likely must list 2 stores`);

  assert.ok(
    info.x === null || typeof info.x === "string",
    `unlistedMaids.${name}.x must be a string or null`
  );
  if (info.x !== null) {
    assert.match(info.x, handlePattern, `unlistedMaids.${name}.x must look like an X handle`);
    assert.ok(
      knownStatuses.has(info.xStatus),
      `unlistedMaids.${name}.xStatus must be one of ${[...knownStatuses].join(" / ")}`
    );
  } else {
    assert.equal(info.xStatus, null, `unlistedMaids.${name}.xStatus must be null without an account`);
    assert.equal(
      info.hasPublicAccount,
      false,
      `unlistedMaids.${name} cannot have a public account without a handle`
    );
  }

  for (const key of ["firstSeen", "streakStart"]) {
    assert.match(
      info[key],
      /^\d{4}-\d{2}-\d{2}$/,
      `unlistedMaids.${name}.${key} must be a date`
    );
    assert.ok(
      info[key] >= insights.shiftDataFrom,
      `unlistedMaids.${name}.${key} cannot predate shiftDataFrom`
    );
  }
  assert.ok(
    Number.isInteger(info.daysSinceLast) && info.daysSinceLast >= 0,
    `unlistedMaids.${name}.daysSinceLast must be a non-negative integer`
  );
  assert.ok(
    info.xTweets === null || (Number.isInteger(info.xTweets) && info.xTweets >= 0),
    `unlistedMaids.${name}.xTweets must be a non-negative integer or null`
  );

  // 休眠アカウントは同名の別人のことがあるので、リンクの裏づけには使わない。
  if (info.hasPublicAccount) {
    assert.ok(info.x, `unlistedMaids.${name} claims a public account without a handle`);
    assert.ok(
      Number.isInteger(info.xTweets) && info.xTweets >= 20,
      `unlistedMaids.${name} has only ${info.xTweets} posts, which is too dormant to trust as hers`
    );
  }

  // promoted と likelyNew はリンクや「新人かも」の表示条件そのものなので、裏づけを必須にする。
  if (info.promoted) {
    assert.ok(info.hasPublicAccount && info.x, `unlistedMaids.${name} is promoted without an account`);
    assert.equal(info.status, "active", `unlistedMaids.${name} is promoted but not active`);
    assert.ok(
      info.recentShifts31 > 0,
      `unlistedMaids.${name} is promoted but has no shift in the last month`
    );
  }
  if (info.likelyNew) {
    assert.ok(info.promoted, `unlistedMaids.${name} is likelyNew without being promoted`);
    assert.equal(
      info.otherAccounts.length,
      0,
      `unlistedMaids.${name} has an older account, so it must not be called new`
    );
  }
}

const activeCount = Object.values(unlisted).filter((info) => info.status === "active").length;
const linkableCount = Object.values(unlisted).filter(
  (info) => info.status === "active" && info.hasPublicAccount && info.x
).length;

// --- カレンダーに出ない人がどれだけいるか -------------------------------
const coverage = insights.rosterCoverage;
assert.ok(
  coverage && typeof coverage === "object",
  "rosterCoverage is required so the UI can warn that trainees are invisible in advance"
);
for (const key of ["unlistedShare", "shiftsWithUnlisted"]) {
  assertRate(coverage[key], `rosterCoverage.${key}`);
}
assert.ok(
  typeof coverage.unlistedPerShift === "number" && coverage.unlistedPerShift >= 0,
  "rosterCoverage.unlistedPerShift must be a non-negative number"
);
for (const key of ["shiftCells", "totalMaids", "rostered", "unlisted"]) {
  assert.ok(
    Number.isInteger(coverage[key]) && coverage[key] >= 0,
    `rosterCoverage.${key} must be a non-negative integer`
  );
}
assert.equal(
  coverage.rostered + coverage.unlisted,
  coverage.totalMaids,
  "rosterCoverage.rostered + unlisted must account for every maid seen"
);
const distributionTotal = Object.entries(coverage.distribution).reduce(
  (sum, [count, cells]) => {
    assert.match(count, /^\d+$/, `rosterCoverage.distribution has a non-numeric key "${count}"`);
    assert.ok(
      Number.isInteger(cells) && cells >= 0,
      `rosterCoverage.distribution.${count} must be a non-negative integer`
    );
    return sum + cells;
  },
  0
);
assert.equal(
  distributionTotal,
  coverage.shiftCells,
  "rosterCoverage.distribution must cover exactly shiftCells shifts"
);
const shiftsWithout = coverage.distribution["0"] ?? 0;
assert.ok(
  Math.abs((1 - shiftsWithout / coverage.shiftCells) - coverage.shiftsWithUnlisted) <= 0.01,
  "rosterCoverage.shiftsWithUnlisted must agree with the distribution"
);
for (const key of ["from", "to"]) {
  assert.match(coverage[key], /^\d{4}-\d{2}-\d{2}$/, `rosterCoverage.${key} must be a date`);
}

console.log(
  `Store insights valid: ${insights.stores.length} stores, ` +
    `${actualEntries.length} recorded dates through ${Object.keys(insights.actual).sort().at(-1)}, ` +
    `${withTendency}/${tendencyNames.length} rostered maids with a store tendency, ` +
    `${activeCount}/${Object.keys(unlisted).length} unlisted members still active (${linkableCount} linkable), ` +
    `${toPercent(coverage.unlistedShare)} of shift slots invisible in advance.`
);

function toPercent(rate) {
  return `${Math.round(rate * 100)}%`;
}
