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

    // UI は「何人態勢の店か」をチップの補足に出すので、分布まで要る。
    const profile = insights.headcountProfile[shift][id];
    assert.ok(profile, `headcountProfile.${shift}.${id} is required`);
    for (const key of ["median", "mode", "min", "max", "p25", "p75", "shifts"]) {
      assert.ok(
        Number.isInteger(profile[key]) && profile[key] > 0,
        `headcountProfile.${shift}.${id}.${key} must be a positive integer`
      );
    }
    assertRate(profile.modeShare, `headcountProfile.${shift}.${id}.modeShare`);
    assert.ok(
      profile.min <= profile.p25 && profile.p25 <= profile.p75 && profile.p75 <= profile.max,
      `headcountProfile.${shift}.${id} quartiles must be ordered within min..max`
    );
    assert.ok(
      profile.min <= profile.median && profile.median <= profile.max,
      `headcountProfile.${shift}.${id}.median must sit inside min..max`
    );
    assert.ok(
      profile.min <= profile.mean && profile.mean <= profile.max,
      `headcountProfile.${shift}.${id}.mean must sit inside min..max`
    );
    assert.equal(
      profile.mean,
      headcount[id],
      `headcountProfile.${shift}.${id}.mean must match typicalHeadcount`
    );

    const distribution = profile.distribution;
    assert.ok(distribution, `headcountProfile.${shift}.${id}.distribution is required`);
    const observed = Object.keys(distribution).map(Number);
    assert.equal(
      Object.values(distribution).reduce((sum, value) => sum + value, 0),
      profile.shifts,
      `headcountProfile.${shift}.${id}.distribution must cover every recorded shift`
    );
    assert.equal(Math.min(...observed), profile.min, `${shift}.${id} min must match the distribution`);
    assert.equal(Math.max(...observed), profile.max, `${shift}.${id} max must match the distribution`);
    const busiest = observed.reduce((top, size) =>
      distribution[String(size)] > distribution[String(top)] ? size : top
    );
    assert.equal(profile.mode, busiest, `${shift}.${id}.mode must be the commonest headcount`);
    assert.ok(
      Math.abs(distribution[String(profile.mode)] / profile.shifts - profile.modeShare) <= 0.01,
      `${shift}.${id}.modeShare must match the distribution`
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

  // UI は「その日出る人数」で開店店舗数を決めるので、その根拠となる実測が要る。
  const headcounts = insights.rosterHeadcountByOpenCount[shift];
  assert.ok(headcounts, `rosterHeadcountByOpenCount.${shift} is required`);
  const openCounts = Object.keys(headcounts).map(Number).sort((a, b) => a - b);
  assert.ok(openCounts.length >= 2, `${shift} must record more than one open-shop count`);
  let previousMean = 0;
  for (const count of openCounts) {
    const bucket = headcounts[String(count)];
    assert.ok(
      typeof bucket.mean === "number" && bucket.mean > 0,
      `rosterHeadcountByOpenCount.${shift}.${count}.mean must be positive`
    );
    assert.ok(
      Number.isInteger(bucket.n) && bucket.n > 0,
      `rosterHeadcountByOpenCount.${shift}.${count}.n must be a positive integer`
    );
    assert.ok(
      bucket.mean > previousMean,
      `${shift}: opening more shops must mean more maids, ${count} shops gave ${bucket.mean}`
    );
    previousMean = bucket.mean;
  }

  // 人数から店舗数を引く閾値。単調増加で、店舗数を超えない。
  const thresholds = insights.openCountByHeadcount[shift];
  assert.ok(Array.isArray(thresholds), `openCountByHeadcount.${shift} must be an array`);
  assert.ok(
    thresholds.length >= 1 && thresholds.length < storeIdList.length,
    `openCountByHeadcount.${shift} must hold fewer boundaries than there are shops`
  );
  let previousLimit = 0;
  for (const limit of thresholds) {
    assert.ok(
      Number.isInteger(limit) && limit > previousLimit,
      `openCountByHeadcount.${shift} must rise, got ${thresholds.join(",")}`
    );
    previousLimit = limit;
  }
  // 実測の平均人数と矛盾しないこと（1店舗の平均は最初の境界以下に収まる）。
  assert.ok(
    headcounts["1"].mean <= thresholds[0],
    `${shift}: a one-shop shift averages ${headcounts["1"].mean}, above the boundary ${thresholds[0]}`
  );
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
  // 公式サイトの配属。予定表の homeStore と食い違うと、ツールチップが嘘をつく。
  // 公式サイトにまだ載っていない人（unpostedMaids）は、roster にいても配属を持たない。
  if (schedule.roster.includes(name)) {
    assert.equal(
      tendency.posted ?? null,
      schedule.homeStore[name] ?? null,
      `maidTendency.${name}.posted must match the posting in data/schedule.js`
    );
  } else {
    assert.equal(
      tendency.posted ?? null,
      null,
      `${name} is not on the official site, so she must not claim a posting`
    );
  }
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

// 個人の傾向だけ集計期間が短い。店舗側の期間と取り違えないよう別のキーで持つ。
for (const [key, table] of [["sampleWindow", insights.sampleWindow], ["tendencyWindow", insights.tendencyWindow]]) {
  assert.ok(table, `${key} is required`);
  for (const field of ["from", "to"]) {
    assert.match(table[field], /^\d{4}-\d{2}-\d{2}$/, `${key}.${field} must be a date`);
  }
  assert.ok(table.from <= table.to, `${key} must not run backwards`);
  assert.ok(
    Number.isInteger(table.days) && table.days > 0,
    `${key}.days must be a positive integer`
  );
}
assert.ok(
  insights.tendencyWindow.days <= insights.sampleWindow.days,
  "the per-maid window must not be longer than the shop window"
);
assert.ok(
  insights.tendencyWindow.to === insights.sampleWindow.to,
  "both windows must end on the same day"
);

// 予定の公開方式が変わった日。UI の一時的な注意書きの期限に使う。
assert.match(
  insights.scheduleSystemChangedAt,
  /^\d{4}-\d{2}-\d{2}$/,
  "scheduleSystemChangedAt must be a date"
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

// 「カレンダーの顔ぶれ＋何人か」を店ごとに言うための実測。
assert.ok(coverage.byStore, "rosterCoverage.byStore is required");
for (const id of storeIdList) {
  const store = coverage.byStore[id];
  assert.ok(store, `rosterCoverage.byStore.${id} is required`);
  for (const key of ["shifts", "rostered", "unlisted"]) {
    assert.ok(
      Number.isInteger(store[key]) && store[key] >= 0,
      `rosterCoverage.byStore.${id}.${key} must be a non-negative integer`
    );
  }
  assertRate(store.unlistedShare, `rosterCoverage.byStore.${id}.unlistedShare`);
  assertRate(store.shiftsWithoutUnlisted, `rosterCoverage.byStore.${id}.shiftsWithoutUnlisted`);
  assert.ok(
    typeof store.unlistedPerShift === "number" && store.unlistedPerShift >= 0,
    `rosterCoverage.byStore.${id}.unlistedPerShift must be a non-negative number`
  );
  if (store.shifts > 0) {
    assert.ok(
      Math.abs(store.unlisted / store.shifts - store.unlistedPerShift) <= 0.01,
      `rosterCoverage.byStore.${id}.unlistedPerShift must match unlisted / shifts`
    );
  }
}
assert.equal(
  storeIdList.reduce((sum, id) => sum + coverage.byStore[id].shifts, 0),
  coverage.shiftCells,
  "the per-store shift counts must add up to shiftCells"
);
assert.equal(
  storeIdList.reduce((sum, id) => sum + coverage.byStore[id].unlisted, 0),
  coverage.unlisted,
  "the per-store unlisted counts must add up to the total"
);

// 確率チップの較正。端は当てにならないので、UI が「この帯は過剰」と断れるようにする。
{
  const cal = insights.accuracy?.calibration;
  assert.ok(cal, "accuracy.calibration must exist");
  assert.match(cal.from, /^\d{4}-\d{2}-\d{2}$/, "calibration must say when it starts");
  assert.match(cal.to, /^\d{4}-\d{2}-\d{2}$/, "calibration must say when it ends");
  assert.ok(cal.from < cal.to, "the calibration window must move forwards");
  assert.ok(cal.n > 1000, `calibration needs a real sample, got ${cal.n}`);
  assert.ok(cal.brier > 0 && cal.brier < 0.25, `Brier must be sane, got ${cal.brier}`);
  // 適用範囲を明記させる。1店舗しか開かない日は測っていないので、
  // チップが候補1店で 100% と出すときの根拠には使えない。
  assert.equal(
    cal.scope,
    "twoOrMoreOpen",
    "calibration must say it only covers shifts where two or more shops opened"
  );

  assert.ok(Array.isArray(cal.buckets) && cal.buckets.length >= 5, "buckets must be usable");
  let previous = -1;
  let sampled = 0;
  for (const bucket of cal.buckets) {
    assert.ok(bucket.from > previous, "buckets must climb without overlapping");
    previous = bucket.from;
    assert.ok(bucket.to > bucket.from, "each bucket must span a range");
    assert.ok(bucket.n >= 100, `a bucket needs enough samples to quote, got ${bucket.n}`);
    assert.ok(
      bucket.said >= bucket.from && bucket.said <= bucket.to,
      `${bucket.from}-${bucket.to} must say a figure inside its own range, got ${bucket.said}`
    );
    assert.ok(bucket.actual >= 0 && bucket.actual <= 1, "actual must be a rate");
    sampled += bucket.n;
  }
  assert.ok(sampled <= cal.n, "buckets cannot hold more than was measured");

  // 端が過剰であること自体を固定する。崩れたら注記を出す意味が変わる。
  const top = cal.buckets.at(-1);
  assert.ok(top.from >= 0.8, "the last bucket must cover the confident end");
  assert.ok(
    top.actual < top.said - 0.05,
    `the confident end must still overstate itself, said ${top.said} got ${top.actual}`
  );
  // 真ん中は当たること。ここまで外れていたら確率で見せる意味が無い。
  const middle = cal.buckets.filter((bucket) => bucket.from >= 0.2 && bucket.to <= 0.8);
  assert.ok(middle.length >= 4, "the middle must be measured");
  for (const bucket of middle) {
    assert.ok(
      Math.abs(bucket.actual - bucket.said) < 0.1,
      `${bucket.from}-${bucket.to} must land within ten points, ` +
        `said ${bucket.said} got ${bucket.actual}`
    );
  }
}

// 店舗数の閾値は roster の人数で測ってあること。予定表に載るのは roster の人だけなので、
// 見習いやサイト未掲載の人まで数えた人数で測ると、母数が違うぶん店舗数を読み違える。
{
  const byOpen = insights.rosterHeadcountByOpenCount;
  assert.ok(byOpen, "rosterHeadcountByOpenCount must exist");
  for (const shift of insights.shifts) {
    const table = byOpen[shift];
    assert.ok(table, `${shift} must have headcounts per shop count`);
    const counts = Object.keys(table).map(Number).sort((a, b) => a - b);
    assert.ok(counts.length >= 2, `${shift} must cover more than one shop count`);
    // 店舗が増えれば人数も増える。逆転していたら測り方が壊れている。
    for (let index = 1; index < counts.length; index += 1) {
      assert.ok(
        table[String(counts[index])].mean > table[String(counts[index - 1])].mean,
        `${shift}: ${counts[index]}店 must need more people than ${counts[index - 1]}店`
      );
    }
    // 1店舗の平均人数は、最初の閾値を超えない。超えていたら別の母数で測っている。
    const thresholds = insights.openCountByHeadcount[shift];
    assert.ok(
      table[String(counts[0])].mean <= thresholds[0] + 1,
      `${shift}: ${counts[0]}店の平均 ${table[String(counts[0])].mean} 人は ` +
        `閾値 ${thresholds[0]} と釣り合わない（母数が違う疑い）`
    );
  }
}

// 予定表の顔ぶれから相方店舗を読む表。
// 主張は「2・3号店は配属者が増えると開きやすくなる、4号店は動かない」で、
// walk-forward で 34.4% -> 42.2%（87勝58敗 p=0.020）だった。
// 差が消えたら、それは主張が成り立たなくなったということなので気づきたい。
{
  const table = insights.secondStoreByHome;
  assert.ok(table, "secondStoreByHome is missing");
  const spread = {};
  for (const id of ["s2", "s3", "s4"]) {
    const rows = table[id];
    assert.ok(rows && Object.keys(rows).length >= 3, `${id}: too few rows to read`);
    const rates = [];
    for (const [count, row] of Object.entries(rows)) {
      assert.ok(/^[0-4]$/.test(count), `${id}: unexpected head count ${count}`);
      assert.ok(row.rate >= 0 && row.rate <= 1, `${id}: rate ${row.rate} out of range`);
      assert.ok(row.n >= 20, `${id}: ${count} maids rests on only ${row.n} shifts`);
      rates.push(row.rate);
    }
    spread[id] = Math.max(...rates) - Math.min(...rates);
  }
  // 2・3号店は動き、4号店は動かない。この差が主張そのもの。
  for (const id of ["s2", "s3"]) {
    assert.ok(
      spread[id] >= 0.15,
      `${id}: home staff should sway the odds, but the spread is only ` +
        `${Math.round(spread[id] * 100)} points`
    );
  }
  assert.ok(
    spread.s4 < spread.s2 && spread.s4 < spread.s3,
    `s4 is meant to be the one home staff cannot predict, but its spread ` +
      `${Math.round(spread.s4 * 100)} points is not the smallest`
  );
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
