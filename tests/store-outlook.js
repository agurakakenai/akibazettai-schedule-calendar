"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const {
  addDays,
  getMaidStoreOutlook,
  getStoreOutlook,
  lastActualDateOf,
  openStoresOn,
  weekdayBucket,
  weekdayIndex
} = require("../app.js");

function loadWindowGlobal(file, key) {
  const context = { window: {} };
  vm.runInNewContext(fs.readFileSync(file, "utf8"), context, { filename: file });
  return JSON.parse(JSON.stringify(context.window[key]));
}

const dataDir = path.join(__dirname, "..", "data");
const insights = loadWindowGlobal(path.join(dataDir, "store-insights.js"), "STORE_INSIGHTS");
const schedule = loadWindowGlobal(path.join(dataDir, "schedule.js"), "SCHEDULE_DATA");

const lastActual = lastActualDateOf(insights);
const outlookFor = (dateKey, shift) =>
  getStoreOutlook({ insights, dateKey, shift, lastActualDate: lastActual });
const openIdsOf = (outlook) =>
  outlook.entries.filter((entry) => entry.state === "open").map((entry) => entry.store.id);

assert.equal(lastActual, "2026-08-30", "the fixture must still end on 2026-08-30");

// 1. 実績のある日は actual の中身がそのまま返る。
for (const shift of insights.shifts) {
  const outlook = outlookFor(lastActual, shift);
  assert.equal(outlook.basis, "actual", `${lastActual} ${shift} must resolve to a record`);
  assert.deepEqual(
    [...outlook.openStores].sort(),
    [...insights.actual[lastActual][shift]].sort(),
    `${lastActual} ${shift} must report exactly the recorded stores`
  );
  assert.deepEqual(
    openIdsOf(outlook).sort(),
    [...insights.actual[lastActual][shift]].sort(),
    `${lastActual} ${shift} chips must mark exactly the recorded stores as open`
  );
  for (const entry of outlook.entries) {
    assert.equal(entry.rate, null, "a record must not carry a probability");
    assert.ok(["open", "closed"].includes(entry.state), "a record is only open or closed");
  }
}

// 2. 実績最終日の翌日はローテーションを使った見込みになる。
const forecastDate = addDays(lastActual, 1);
assert.equal(forecastDate, "2026-08-31", "addDays must roll to the next calendar day");
for (const shift of insights.shifts) {
  const outlook = outlookFor(forecastDate, shift);
  assert.equal(outlook.basis, "forecast", `${forecastDate} ${shift} must be a forecast`);

  const previous = insights.actual[lastActual][shift];
  const groupState = previous.includes("s2")
    ? (previous.includes("s3") ? "both" : "s2")
    : previous.includes("s3")
      ? "s3"
      : "none";
  const transitions = insights.rotation.nextDay[shift][groupState];
  const s4Transitions = insights.rotation.nextDayS4[shift][previous.includes("s4") ? "open" : "closed"];

  const rateOf = (id) => outlook.entries.find((entry) => entry.store.id === id).rate;
  assert.equal(
    rateOf("s2"),
    (transitions.s2 ?? 0) + (transitions.both ?? 0),
    `${shift} s2 forecast must come from rotation.nextDay`
  );
  assert.equal(
    rateOf("s3"),
    (transitions.s3 ?? 0) + (transitions.both ?? 0),
    `${shift} s3 forecast must come from rotation.nextDay`
  );
  assert.equal(rateOf("s4"), s4Transitions.open ?? 0, `${shift} s4 forecast must come from rotation.nextDayS4`);
  assert.equal(
    rateOf("s1"),
    insights.baseOpenRate[shift].s1,
    `${shift} s1 must fall back to the base open rate`
  );
  for (const entry of outlook.entries) {
    assert.ok(
      Number.isFinite(entry.rate) && entry.rate >= 0 && entry.rate <= 1,
      `${shift} ${entry.store.id} forecast must be a real probability, got ${entry.rate}`
    );
  }
  assert.ok(
    outlook.summary.includes(lastActual),
    "the forecast summary must name the day it is based on"
  );
}

// 3. さらに先の日は曜日傾向になる。
const farFuture = addDays(lastActual, 30);
for (const shift of insights.shifts) {
  const outlook = outlookFor(farFuture, shift);
  assert.equal(outlook.basis, "tendency", `${farFuture} ${shift} must fall back to a weekday tendency`);
  assert.ok(
    outlook.summary.includes("2日以上先は当てになりません"),
    "a weekday tendency must say it is not a forecast"
  );
}

// 4. 昼と夜で記録の有無が違う日。片方しか記録が無くても「休み」と断定しない。
const partialDates = Object.keys(insights.actual).filter(
  (date) => Object.keys(insights.actual[date]).length < insights.shifts.length
);
assert.ok(partialDates.length > 0, "the fixture must contain at least one single-shift day");
assert.ok(
  partialDates.includes("2026-08-29"),
  "2026-08-29 must still be one of the single-shift days"
);

for (const date of partialDates) {
  for (const shift of insights.shifts) {
    const recorded = insights.actual[date][shift];
    const outlook = outlookFor(date, shift);
    if (recorded) {
      assert.equal(outlook.basis, "actual", `${date} ${shift} has a record and must use it`);
    } else {
      assert.equal(
        outlook.basis,
        "tendency",
        `${date} ${shift} has no record, so it must not be treated as a record`
      );
      assert.equal(
        openStoresOn(insights, date, shift),
        null,
        `${date} ${shift} must report "no record" rather than an empty set`
      );
      assert.ok(
        outlook.entries.every((entry) => entry.state !== "closed"),
        `${date} ${shift} must not claim any store was closed`
      );
      assert.ok(
        outlook.summary.includes("休みとは限りません"),
        `${date} ${shift} must say the missing record is not a closure`
      );
    }
  }
}

// 5. 曜日インデックスは日曜=0。日曜の値が weekdayOpenRate[shift]['0'] と一致する。
assert.equal(insights.weekdayOrigin, "sunday", "this test assumes a Sunday-based weekday index");
const sunday = "2026-09-06";
assert.equal(weekdayIndex(sunday), 0, "2026-09-06 must be a Sunday");
assert.equal(weekdayBucket(insights, sunday), "0", "a Sunday must map to bucket '0'");
for (const shift of insights.shifts) {
  const outlook = outlookFor(sunday, shift);
  assert.equal(outlook.basis, "tendency", "2026-09-06 is far enough ahead to be a tendency");
  assert.equal(outlook.weekdayBucket, "0", "the Sunday outlook must read bucket '0'");
  for (const entry of outlook.entries) {
    assert.equal(
      entry.rate,
      insights.weekdayOpenRate[shift]["0"][entry.store.id],
      `${shift} ${entry.store.id} must read weekdayOpenRate['${shift}']['0']`
    );
  }
}
// 月曜が '1' であることも確認し、起点がずれていないことを押さえる。
assert.equal(weekdayBucket(insights, "2026-09-07"), "1", "2026-09-07 (Monday) must map to bucket '1'");
assert.equal(weekdayBucket(insights, "2026-09-12"), "6", "2026-09-12 (Saturday) must map to bucket '6'");

// 6. 欠損キーは 0 として扱い、undefined が計算に混ざらない。
assert.equal(
  insights.rotation.sameDay.s2.s3,
  undefined,
  "the fixture must still have no 昼2号店 → 夜3号店 transition"
);
const missingTransition = {
  ...insights,
  actual: { "2026-08-30": { 昼: ["s1", "s3"], 夜: ["s1", "s3"] } },
  rotation: {
    ...insights.rotation,
    // s3 と both が欠けた分布を渡しても NaN にならないこと。
    nextDay: { ...insights.rotation.nextDay, 昼: { s3: { none: 1 } } }
  }
};
const degraded = getStoreOutlook({
  insights: missingTransition,
  dateKey: "2026-08-31",
  shift: "昼",
  lastActualDate: "2026-08-30"
});
assert.equal(degraded.basis, "forecast", "a sparse rotation must still produce a forecast");
for (const entry of degraded.entries) {
  assert.ok(
    Number.isFinite(entry.rate),
    `${entry.store.id} must stay a number when a transition key is missing, got ${entry.rate}`
  );
  assert.doesNotMatch(entry.text, /NaN/, `${entry.store.id} must not render NaN`);
}
assert.equal(
  degraded.entries.find((entry) => entry.store.id === "s3").rate,
  0,
  "a missing transition key must count as 0"
);

// --- メイドさんの店舗 ---------------------------------------------------
const maidWithRecord = getMaidStoreOutlook({
  insights,
  name: schedule.roster[0],
  shift: "昼",
  outlook: outlookFor(lastActual, "昼")
});
assert.equal(maidWithRecord.basis, "pickRate", "a recorded day must rank the stores that were open");
assert.ok(
  insights.actual[lastActual]["昼"].includes(maidWithRecord.storeId),
  "a recorded day must only suggest a store that was actually open"
);

const maidWithoutRecord = getMaidStoreOutlook({
  insights,
  name: schedule.roster[0],
  shift: "昼",
  outlook: outlookFor(farFuture, "昼")
});
assert.equal(maidWithoutRecord.basis, "share", "an unrecorded day must fall back to the overall share");
assert.equal(
  maidWithoutRecord.storeId,
  insights.maidTendency[schedule.roster[0]].likely[0],
  "the suggested store must match the highest-share store"
);

assert.equal(
  getMaidStoreOutlook({ insights, name: "存在しないメイド", shift: "昼", outlook: null }),
  null,
  "an unknown maid must not produce a chip"
);
assert.equal(
  getMaidStoreOutlook({
    insights: { ...insights, maidTendency: { ...insights.maidTendency, テスト: null } },
    name: "テスト",
    shift: "昼",
    outlook: null
  }),
  null,
  "a null tendency must not produce a chip"
);

// 夜の分母が小さい人は、昼夜あわせた値に切り替えて 0% / 100% への振れを避ける。
const smallNightSample = Object.keys(insights.maidTendency).find((name) => {
  const samples = insights.maidTendency[name]?.sampleByShift?.["夜"];
  return samples && Object.values(samples).reduce((sum, value) => sum + value, 0) < 20;
});
if (smallNightSample) {
  const chip = getMaidStoreOutlook({
    insights,
    name: smallNightSample,
    shift: "夜",
    outlook: outlookFor(farFuture, "夜")
  });
  assert.ok(
    chip.title.includes("昼夜あわせた実績"),
    `${smallNightSample} has too few 夜 samples, so the chip must say it used the combined figures`
  );
  assert.equal(
    chip.rate,
    insights.maidTendency[smallNightSample].share[chip.storeId],
    "the combined share must be used verbatim"
  );
}

// 傾向データが無くても落ちないこと。
assert.equal(
  getStoreOutlook({ insights: null, dateKey: "2026-09-01", shift: "昼" }),
  null,
  "a missing STORE_INSIGHTS must simply disable the feature"
);
assert.equal(
  getStoreOutlook({ insights: { stores: [] }, dateKey: "2026-09-01", shift: "昼" }),
  null,
  "an empty store list must simply disable the feature"
);

console.log(
  "Store outlook valid: records, next-day forecast, weekday tendency, " +
    `${partialDates.length} single-shift days, Sunday-based weekday index, and sparse-rotation fallbacks.`
);
