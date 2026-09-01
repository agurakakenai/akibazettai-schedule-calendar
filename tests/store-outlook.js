"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const {
  addDays,
  applyEventCertainty,
  assignShiftStores,
  eventStorePins,
  expectedOpenStores,
  getMaidStoreOutlook,
  getShiftAssignment,
  getStoreOutlook,
  groupByAssignedStore,
  lastActualDateOf,
  openStoresOn,
  openStoresOnDay,
  scheduleSystemNote,
  sortByAssignedStore,
  storeCapacities,
  storeProbabilities,
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

// 実績はこの先も日ごとに伸びるので、最終日を決め打ちしない。
// 見張りたいのは「actual のいちばん新しい日を拾えているか」であって、日付そのものではない。
{
  const recorded = Object.keys(insights.actual).sort();
  assert.ok(recorded.length > 0, "the fixture must contain some records");
  assert.match(lastActual, /^\d{4}-\d{2}-\d{2}$/, "the last recorded date must be a date");
  assert.equal(lastActual, recorded[recorded.length - 1], "the last record must be the newest one");
  assert.ok(
    insights.shifts.some((shift) => Array.isArray(insights.actual[lastActual][shift])),
    "the newest record must carry at least one shift"
  );
}

// 1. 実績のある日は actual の中身がそのまま返る。
// 最新の記録日は片シフトだけのことがある（その日の夜がまだ来ていない等）ので、
// 記録のあるシフトだけを見る。
const recordedShifts = insights.shifts.filter((shift) =>
  Array.isArray(insights.actual[lastActual][shift])
);
assert.ok(recordedShifts.length > 0, "the newest record must carry at least one shift");
for (const shift of recordedShifts) {
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
assert.notEqual(forecastDate, lastActual, "addDays must roll to the next calendar day");
assert.ok(forecastDate > lastActual, "the next day must sort after the last record");
for (const shift of recordedShifts) {
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

  // チップはシフト別の表、説明文は日単位の表。両方が実際に使われていること。
  const previousDay = openStoresOnDay(insights, lastActual);
  const dayNext = insights.rotation.nextDayByDay[
    previousDay.has("s2")
      ? (previousDay.has("s3") ? "both" : "s2")
      : previousDay.has("s3") ? "s3" : "none"
  ];
  const dayRates = {
    s2: (dayNext.s2 ?? 0) + (dayNext.both ?? 0),
    s3: (dayNext.s3 ?? 0) + (dayNext.both ?? 0)
  };
  const dayLeader = dayRates.s2 >= dayRates.s3 ? "s2" : "s3";
  const dayLeaderShort = insights.stores.find((store) => store.id === dayLeader).short;
  assert.ok(
    outlook.summary.includes(`昼夜をまとめると${dayLeaderShort}`),
    "the summary must use the day-level rotation table, which is the accurate one for that question"
  );
  assert.ok(
    outlook.summary.includes(`${Math.round(dayRates[dayLeader] * 100)}%`),
    "the day-level figure must come from rotation.nextDayByDay"
  );
  assert.notEqual(
    rateOf(dayLeader),
    dayRates[dayLeader],
    "the chips must still use the per-shift table, not the day-level one"
  );
}

// 昼夜をまとめた開店店舗は、両シフトの和集合になる。
{
  const union = openStoresOnDay(insights, lastActual);
  const expected = new Set(insights.shifts.flatMap((shift) => insights.actual[lastActual][shift] ?? []));
  assert.deepEqual([...union].sort(), [...expected].sort(), "the day view must merge both shifts");
  assert.equal(
    openStoresOnDay(insights, "1999-01-01"),
    null,
    "a day with no record has no day-level open set"
  );
  const partial = Object.keys(insights.actual).find(
    (date) => Object.keys(insights.actual[date]).length < insights.shifts.length
  );
  assert.ok(
    openStoresOnDay(insights, partial).size > 0,
    "a single-shift day still has a day-level open set"
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
      // 記録が無い側は、前日の同じシフトが分かっていれば見込み、分からなければ
      // 曜日傾向になる。どちらにせよ「実績」を名乗ってはいけない。
      assert.ok(
        ["forecast", "tendency"].includes(outlook.basis),
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
// ふだんの傾向だけで1人ずつ決めると、ほぼ毎日開いている1号店に全員が寄ってしまう。
// 割り振りはそれを避けるためのものなので、まず「寄ってしまう」ことを確認しておく。
const shareWinners = new Set(
  schedule.roster.map((name) => {
    const share = insights.maidTendency[name].share;
    return ["s1", "s2", "s3", "s4"].reduce((top, id) => (share[id] > share[top] ? id : top), "s1");
  })
);
assert.deepEqual(
  [...shareWinners],
  ["s1"],
  "share alone points every maid at the same store, which is why the assignment exists"
);

const dayPool = schedule.schedule["2026-09-03"]["昼"].map((entry) => entry.name);
assert.ok(dayPool.length >= 8, "this fixture day should be busy enough to split across stores");

const futureOutlook = outlookFor("2026-09-03", "昼");
const assignment = getShiftAssignment({
  insights,
  members: dayPool,
  shift: "昼",
  outlook: futureOutlook
});

assert.ok(assignment, "a shift with a pool and an outlook must produce an assignment");
assert.equal(assignment.byMaid.size, dayPool.length, "every member must be placed somewhere");
assert.ok(assignment.storeIds.length >= 2, "the assignment must consider more than one store");
assert.ok(
  new Set([...assignment.byMaid.values()].map((placed) => placed.storeId)).size >= 2,
  "the assignment must not funnel the whole pool into one store"
);

// 定員はプール人数ちょうどで、標準人数の比に従う。
const capacityTotal = Object.values(assignment.capacity).reduce((sum, value) => sum + value, 0);
assert.equal(capacityTotal, dayPool.length, "capacities must add up to the pool size");
for (const storeId of assignment.storeIds) {
  const placed = [...assignment.byMaid.values()].filter((entry) => entry.storeId === storeId).length;
  assert.equal(
    placed,
    assignment.capacity[storeId],
    `${storeId} must receive exactly its capacity`
  );
}
const headcount = insights.typicalHeadcount["昼"];
const biggest = assignment.storeIds.reduce((top, id) => (headcount[id] > headcount[top] ? id : top));
assert.equal(
  assignment.capacity[biggest],
  Math.max(...assignment.storeIds.map((id) => assignment.capacity[id])),
  "the store with the largest typical headcount must get the largest share"
);

// 割り振りは並び順に依存しない。絞り込みで並びが変わっても結果が動かないこと。
const reversed = getShiftAssignment({
  insights,
  members: [...dayPool].reverse(),
  shift: "昼",
  outlook: futureOutlook
});
for (const name of dayPool) {
  assert.equal(
    reversed.byMaid.get(name).storeId,
    assignment.byMaid.get(name).storeId,
    `${name} must land in the same store regardless of the input order`
  );
}

// 実績のある日は、実際に開いていた店にだけ割り振る。
const recordedPool = schedule.schedule["2026-09-03"]["昼"].map((entry) => entry.name);
const recordedOutlook = outlookFor(lastActual, "昼");
const recordedAssignment = getShiftAssignment({
  insights,
  members: recordedPool,
  shift: "昼",
  outlook: recordedOutlook
});
assert.deepEqual(
  [...recordedAssignment.storeIds].sort(),
  [...insights.actual[lastActual]["昼"]].sort(),
  "a recorded shift must only place maids in the stores that were open"
);

// 開店店舗が分からない日は、その日出る人数から店舗数を決める。
assert.equal(
  expectedOpenStores(insights, "昼", futureOutlook, dayPool.length).length,
  expectedOpenStores(insights, "昼", futureOutlook, dayPool.length).length,
  "the candidate set must be stable for the same line-up"
);
assert.ok(
  expectedOpenStores(insights, "昼", futureOutlook, dayPool.length).includes("s1"),
  "1号店 is open almost every day, so it must always be a candidate"
);
assert.ok(
  expectedOpenStores(insights, "昼", futureOutlook, 4).length <
    expectedOpenStores(insights, "昼", futureOutlook, 14).length,
  "a bigger line-up must open more shops than a small one"
);

// 定員の丸めは合計を崩さない。
for (const poolSize of [1, 2, 3, 7, 13, 20]) {
  const capacity = storeCapacities(insights, "昼", ["s1", "s2", "s4"], poolSize);
  assert.equal(
    Object.values(capacity).reduce((sum, value) => sum + value, 0),
    poolSize,
    `capacities for a pool of ${poolSize} must add up exactly`
  );
  assert.ok(
    Object.values(capacity).every((value) => value >= 0),
    "capacities must never go negative"
  );
}

assert.equal(
  assignShiftStores({ insights, members: [], shift: "昼", storeIds: ["s1"] }),
  null,
  "an empty pool must not produce an assignment"
);
assert.equal(
  assignShiftStores({ insights, members: dayPool, shift: "昼", storeIds: [] }),
  null,
  "no candidate stores means no assignment"
);

// チップは割り振りではなく確率を出す。「この人はこの店に行かない」と読めてはいけない。
const chip = getMaidStoreOutlook({
  insights,
  name: dayPool[0],
  shift: "昼",
  outlook: futureOutlook,
  assignment
});
assert.equal(chip.basis, "probability", "the chip must state a probability, not a verdict");
assert.equal(
  chip.storeId,
  assignment.byMaid.get(dayPool[0]).storeId,
  "the chip must lead with the shop the list is grouped by"
);
assert.ok(chip.title.includes("確率"), "the chip must say the figure is a probability");
assert.ok(
  chip.title.includes("4店舗すべて"),
  "the chip must say every maid has worked every shop"
);
assert.ok(chip.alternative, "the chip must offer a runner-up, which covers 97% together");
assert.ok(
  chip.title.includes(`直近${insights.tendencyWindow.days}日`),
  "the chip must say the per-maid figures use the shorter window"
);
for (const storeId of assignment.storeIds) {
  assert.ok(
    chip.title.includes(insights.stores.find((store) => store.id === storeId).short),
    `the chip must list ${storeId} so it never reads as "she never goes there"`
  );
}

// 公式サイトの配属も出す。確率は実績が主なので、配属と食い違う人がいる。
{
  const posted = insights.maidTendency[dayPool[0]].posted;
  assert.ok(posted, "a rostered maid must carry her posting");
  assert.ok(
    chip.title.includes(
      `公式サイトの配属は${insights.stores.find((store) => store.id === posted).short}`
    ),
    "the chip must name the shop the site posts her to"
  );

  // 配属と、いちばん高い確率の店が食い違う人が実際にいる。そこを隠さない。
  const drifted = schedule.roster.filter((name) => {
    const tendency = insights.maidTendency[name];
    return tendency?.posted && tendency.home !== tendency.posted;
  });
  assert.ok(
    drifted.length > 0,
    "some maids turn up most often somewhere other than their posting, which is why we show both"
  );

  // 公式サイトに載っていない人には配属が無いので、その一文を出さない。
  const outsider = Object.keys(insights.maidTendency).find(
    (name) => !schedule.roster.includes(name)
  );
  if (outsider) {
    const outsiderAssignment = assignShiftStores({
      insights,
      members: [outsider],
      shift: "昼",
      storeIds: assignment.storeIds
    });
    const outsiderChip = getMaidStoreOutlook({
      insights,
      name: outsider,
      shift: "昼",
      outlook: futureOutlook,
      assignment: outsiderAssignment
    });
    assert.ok(
      !outsiderChip.title.includes("公式サイトの配属"),
      `${outsider} is not on the official site, so her chip must not invent a posting`
    );
  }
}

// 確率は候補店で合計1になり、どの店も 0 にはならない。
{
  const stores = insights.stores.map((store) => store.id);
  for (const storeIds of [stores, ["s1", "s4"], ["s3"]]) {
    for (const name of schedule.roster) {
      const probabilities = storeProbabilities(insights, name, "昼", storeIds);
      const total = Object.values(probabilities).reduce((sum, value) => sum + value, 0);
      assert.ok(
        Math.abs(total - 1) <= 0.001,
        `${name}: probabilities over ${storeIds.join("+")} must sum to 1, got ${total}`
      );
      for (const id of storeIds) {
        assert.ok(
          probabilities[id] > 0,
          `${name} must never be given a flat 0% for ${id}; every maid has worked every shop`
        );
      }
    }
  }

  assert.deepEqual(
    storeProbabilities(insights, schedule.roster[0], "昼", ["s2"]),
    { s2: 1 },
    "a single candidate shop takes the whole probability"
  );
  assert.deepEqual(storeProbabilities(insights, schedule.roster[0], "昼", []), {}, "no shops, no probabilities");

  // 4号店にあまり入らない人でも 0 にはならない、というのが今回の眼目。
  const rare = schedule.roster
    .map((name) => [name, insights.maidTendency[name].pickRate.s4])
    .sort((a, b) => a[1] - b[1])[0];
  const rareProbabilities = storeProbabilities(insights, rare[0], "昼", ["s1", "s4"]);
  assert.ok(
    rareProbabilities.s4 > 0,
    `${rare[0]} works 4号店 least often (${rare[1]}), and must still get a non-zero chance`
  );
  assert.ok(
    rareProbabilities.s1 + rareProbabilities.s4 > rareProbabilities.s1,
    "two shops together must always beat the single most likely one"
  );

  // 傾向がまったく無くても NaN にならず、均等割りになる。
  const blank = storeProbabilities(
    { ...insights, maidTendency: { 無名: { pickRate: { s1: 0, s2: 0, s3: 0, s4: 0 } } } },
    "無名",
    "昼",
    ["s1", "s2"]
  );
  assert.ok(
    Math.abs(blank.s1 - 0.5) <= 0.001 && Math.abs(blank.s2 - 0.5) <= 0.001,
    "a maid with no history must be split evenly rather than produce NaN"
  );
  const unknown = storeProbabilities(insights, "存在しないメイド", "昼", ["s1", "s2"]);
  assert.ok(
    Object.values(unknown).every((value) => Number.isFinite(value)),
    "an unknown maid must not produce NaN"
  );
}

assert.equal(
  getMaidStoreOutlook({ insights, name: dayPool[0], shift: "昼", outlook: futureOutlook }),
  null,
  "without an assignment there is nothing to place"
);
assert.equal(
  getMaidStoreOutlook({ insights, name: "存在しないメイド", shift: "昼", outlook: null, assignment }),
  null,
  "an unknown maid must not produce a chip"
);
assert.equal(
  getMaidStoreOutlook({
    insights: { ...insights, maidTendency: { ...insights.maidTendency, テスト: null } },
    name: "テスト",
    shift: "昼",
    outlook: futureOutlook,
    assignment
  }),
  null,
  "a null tendency must not produce a chip"
);

// 傾向が無い人が混ざっても、全員が必ずどこかに入る。
const withStranger = getShiftAssignment({
  insights,
  members: [...dayPool, "傾向のない人"],
  shift: "昼",
  outlook: futureOutlook
});
assert.equal(
  withStranger.byMaid.size,
  dayPool.length + 1,
  "a maid without a tendency must still be placed"
);
assert.equal(
  withStranger.byMaid.get("傾向のない人").known,
  false,
  "a maid without a tendency must be marked as a guess"
);

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

// --- 記念日の主役は自分の所属店に立つ -----------------------------------
const eventShifts = [];
for (const [date, day] of Object.entries(schedule.schedule)) {
  for (const shift of insights.shifts) {
    const entries = day[shift] ?? [];
    if (entries.some((entry) => entry.featured)) {
      eventShifts.push({ date, shift, entries });
    }
  }
}
const eventShiftsPreview = eventShifts;
assert.ok(eventShifts.length > 0, "the fixture must contain at least one event shift");

for (const { date, shift, entries } of eventShifts) {
  // アプリと同じく公式サイトの配属を渡す。推定側の経路は下の単体テストで見る。
  const pins = eventStorePins({ insights, entries, homeStore: schedule.homeStore });
  const hosts = entries.filter((entry) => entry.featured);
  assert.equal(pins.size, hosts.length, `${date} ${shift} must pin every featured maid`);

  for (const host of hosts) {
    const pin = pins.get(host.name);
    assert.equal(
      pin.storeId,
      schedule.homeStore[host.name],
      `${host.name} must be pinned to the shop the site posts her to`
    );
    assert.equal(pin.label, host.eventLabel, "the pin must carry the event label");
  }

  const base = outlookFor(date, shift);
  const withEvent = applyEventCertainty(insights, base, pins);
  const pinnedStores = new Set([...pins.values()].map((pin) => pin.storeId));

  for (const storeId of pinnedStores) {
    const entry = withEvent.entries.find((candidate) => candidate.store.id === storeId);
    assert.equal(entry.state, "open", `${date} ${shift} ${storeId} must be certain to be open`);
    assert.equal(entry.rate, 1, "a store hosting an event is not a probability");
    assert.equal(entry.text, "営業", "a hosting store must read as open, not as a percentage");
  }
  assert.ok(
    withEvent.summary.includes(hosts[0].eventLabel),
    "the summary must say why the store is certain to be open"
  );
  assert.ok(
    expectedOpenStores(insights, shift, withEvent).includes([...pinnedStores][0]),
    "a hosting store must always be a candidate for the assignment"
  );

  const members = entries.map((entry) => entry.name);
  const eventAssignment = getShiftAssignment({
    insights,
    members,
    shift,
    outlook: withEvent,
    pins
  });

  for (const host of hosts) {
    const placed = eventAssignment.byMaid.get(host.name);
    assert.equal(
      placed.storeId,
      schedule.homeStore[host.name],
      `${host.name} must be assigned to her own store on ${date} ${shift}`
    );
    assert.equal(placed.score, 1, "a pinned placement is certain, not a guess");
    assert.ok(placed.pin, "a pinned placement must record why it is certain");

    const hostChip = getMaidStoreOutlook({
      insights,
      name: host.name,
      shift,
      outlook: withEvent,
      assignment: eventAssignment
    });
    assert.equal(hostChip.basis, "event", "the host's chip must say it comes from the event");
    assert.equal(hostChip.percent, "確定", "the host's chip must not show a probability");
    assert.ok(
      hostChip.title.includes(host.eventLabel),
      "the host's chip must name the event that fixes her store"
    );
    assert.ok(
      hostChip.title.includes("公式サイトの配属"),
      "the chip must credit the site for the home store, now that we read it from there"
    );
  }

  assert.equal(
    eventAssignment.byMaid.size,
    members.length,
    "pinning the host must not drop anyone else from the assignment"
  );
}

// 実績のある日は記録が最優先で、イベントで上書きしない。
const recordedShift = outlookFor(lastActual, "昼");
assert.equal(
  applyEventCertainty(
    insights,
    recordedShift,
    new Map([["だれか", { storeId: "s4", label: "生誕", pickRate: 1 }]])
  ),
  recordedShift,
  "a recorded shift already knows what happened, so an event must not rewrite it"
);
assert.equal(
  applyEventCertainty(insights, futureOutlook, new Map()),
  futureOutlook,
  "a shift with no event must be left untouched"
);
assert.equal(
  eventStorePins({ insights, entries: [{ name: "あむ", featured: false }] }).size,
  0,
  "only featured maids are pinned"
);
assert.equal(
  eventStorePins({ insights, entries: [{ name: "知らない人", featured: true, eventLabel: "生誕" }] }).size,
  0,
  "a maid with no tendency has no known store to pin her to"
);

// 記念日の主役は所属店に立つ。所属は公式サイトの配属が正で、推定はその代役。
{
  const host = [{ name: "える", featured: true, eventLabel: "生誕" }];
  const official = schedule.homeStore["える"];
  const guessed = insights.maidTendency["える"].home;
  assert.notEqual(official, guessed, "える is the case where the site and our guess disagree");

  const pinned = eventStorePins({ insights, entries: host, homeStore: schedule.homeStore }).get("える");
  assert.equal(pinned.storeId, official, "the site's posting wins over our guess");
  assert.equal(pinned.official, true, "a pin from the site must say so");

  const fallback = eventStorePins({ insights, entries: host }).get("える");
  assert.equal(fallback.storeId, guessed, "without a posting we fall back on the tendency");
  assert.equal(fallback.official, false, "a guessed pin must not claim to be official");

  // 予定表に配属が無い人でも落ちない。
  const partial = eventStorePins({ insights, entries: host, homeStore: {} }).get("える");
  assert.equal(partial.storeId, guessed, "an empty posting table falls back too");

  // ツールチップは出どころを言い分ける。公式なら公式、無ければ推定と断る。
  const chipFor = (pin) => {
    const outlook = applyEventCertainty(insights, outlookFor(farFuture, "昼"), new Map([["える", pin]]));
    const assignment = getShiftAssignment({
      insights,
      members: ["える"],
      shift: "昼",
      outlook,
      pins: new Map([["える", pin]])
    });
    return getMaidStoreOutlook({ insights, name: "える", shift: "昼", outlook, assignment });
  };
  assert.ok(chipFor(pinned).title.includes("公式サイトの配属"), "an official pin must credit the site");
  assert.ok(chipFor(fallback).title.includes("推定"), "a guessed pin must still admit it is a guess");

  // 公式の配属は全員ぶんあるので、記念日の主役はすべて公式で置ける。
  for (const [date, day] of Object.entries(schedule.schedule)) {
    for (const [shift, members] of Object.entries(day)) {
      const pins = eventStorePins({ insights, entries: members, homeStore: schedule.homeStore });
      for (const [name, pin] of pins) {
        assert.equal(pin.official, true, `${date} ${shift}: ${name} must be pinned from the site`);
        assert.equal(pin.storeId, schedule.homeStore[name], `${name} must stand at her own shop`);
      }
    }
  }
}

// 開店店舗数は人数から決まる。最頻値で固定すると3店舗の日も1店舗の日も出せない。
{
  const tendency = outlookFor(farFuture, "昼");
  const countFor = (poolSize) => expectedOpenStores(insights, "昼", tendency, poolSize).length;

  for (const poolSize of [3, 4, 5]) {
    assert.equal(countFor(poolSize), 1, `${poolSize} maids fit in a single shop`);
  }
  for (const poolSize of [6, 8, 10, 13]) {
    assert.equal(countFor(poolSize), 2, `${poolSize} maids need two shops`);
  }
  for (const poolSize of [15, 18]) {
    assert.equal(countFor(poolSize), 3, `${poolSize} maids need three shops`);
  }

  // 6人は2店が多数派（昼61% / 夜73%）。標準人数の累積だけだと1店に潰れてしまう。
  for (const shift of insights.shifts) {
    const outlook = outlookFor(farFuture, shift);
    assert.equal(
      expectedOpenStores(insights, shift, outlook, 5).length,
      1,
      `${shift}: five maids usually mean one shop`
    );
    assert.equal(
      expectedOpenStores(insights, shift, outlook, 6).length,
      2,
      `${shift}: six maids usually mean two shops`
    );
  }

  // 人数が増えれば店舗数は減らない。
  let previous = 0;
  for (let poolSize = 0; poolSize <= 30; poolSize += 1) {
    const count = countFor(poolSize);
    assert.ok(count >= previous, `a bigger line-up must not open fewer shops (${poolSize})`);
    assert.ok(
      count >= 1 && count <= insights.stores.length,
      `an extreme line-up of ${poolSize} must still pick 1..${insights.stores.length} shops`
    );
    previous = count;
  }

  // イベント開催店は人数に関係なく必ず候補に残る。
  const eventShift = eventShiftsPreview[0];
  if (eventShift) {
    const pinned = applyEventCertainty(
      insights,
      outlookFor(eventShift.date, eventShift.shift),
      eventStorePins({ insights, entries: eventShift.entries, homeStore: schedule.homeStore })
    );
    const chosen = expectedOpenStores(insights, eventShift.shift, pinned, 1);
    for (const storeId of pinned.certainStores) {
      assert.ok(chosen.includes(storeId), "a hosting shop must survive even a tiny line-up");
    }
  }
}

// 実データでも、全シフトが同じ店舗数になってはいけない。
{
  const seen = { 昼: new Set(), 夜: new Set() };
  for (const [date, day] of Object.entries(schedule.schedule)) {
    for (const shift of insights.shifts) {
      const entries = day[shift] ?? [];
      const outlook = applyEventCertainty(
        insights,
        outlookFor(date, shift),
        eventStorePins({ insights, entries })
      );
      seen[shift].add(expectedOpenStores(insights, shift, outlook, entries.length).length);
    }
  }
  const combined = new Set([...seen["昼"], ...seen["夜"]]);
  assert.ok(
    combined.size >= 2,
    `the schedule must produce a mix of shop counts, got ${[...combined].join(",")}`
  );
  const recorded = new Set(
    Object.keys(insights.openCountPerShift["昼"])
      .concat(Object.keys(insights.openCountPerShift["夜"]))
      .map(Number)
      .filter((count) => count > 0)
  );
  for (const count of combined) {
    assert.ok(recorded.has(count), `${count} shops has never actually happened`);
  }
}

// キッチンにゃんこは料理担当なので、同じ店に固まらないよう散らす。
{
  const kitchen = new Set(schedule.kitchenStaff);
  assert.ok(kitchen.size >= 2, "the roster must list at least two kitchen staff");

  // 定員の都合と区別できるよう、両方が同じ店を強く好む状況を作って検証する。
  const cooks = ["料理A", "料理B"];
  const helpers = ["補助A", "補助B"];
  const tendencyFor = (pick) => ({
    pickRate: pick,
    share: pick,
    home: "s1",
    likely: ["s1", "s2"]
  });
  // 1号店を広くしておかないと定員が先に埋まり、減点の有無に関わらず割れてしまう。
  const synthetic = {
    ...insights,
    typicalHeadcount: {
      昼: { s1: 9, s2: 3, s3: 3, s4: 3 },
      夜: { s1: 9, s2: 3, s3: 3, s4: 3 }
    },
    maidTendency: {
      ...insights.maidTendency,
      [cooks[0]]: tendencyFor({ s1: 0.9, s2: 0.1, s3: 0, s4: 0 }),
      [cooks[1]]: tendencyFor({ s1: 0.6, s2: 0.4, s3: 0, s4: 0 }),
      [helpers[0]]: tendencyFor({ s1: 0.9, s2: 0.1, s3: 0, s4: 0 }),
      [helpers[1]]: tendencyFor({ s1: 0.6, s2: 0.4, s3: 0, s4: 0 })
    }
  };
  const twoStores = ["s1", "s2"];
  const call = (members, kitchenStaff) =>
    assignShiftStores({ insights: synthetic, members, shift: "昼", storeIds: twoStores, kitchenStaff });

  const pool = [...cooks, ...helpers];
  const roomy = call(pool, undefined);
  assert.equal(
    new Set(cooks.map((name) => roomy.byMaid.get(name).storeId)).size,
    1,
    "without the rule both cooks follow their preference into the same shop"
  );

  const spread = call(pool, new Set(cooks));
  assert.equal(
    new Set(cooks.map((name) => spread.byMaid.get(name).storeId)).size,
    2,
    "cooks must be spread even when they both prefer the same shop"
  );

  // 減点はキッチンにゃんこのスコアにだけ効く。料理担当がいなければ結果は変わらない。
  assert.deepEqual(
    [...call(helpers, new Set(cooks)).byMaid].map(([name, placed]) => [name, placed.storeId]),
    [...call(helpers, undefined).byMaid].map(([name, placed]) => [name, placed.storeId]),
    "a shift with no cook must be assigned exactly as before"
  );

  // 店より多いキッチンにゃんこが出ても落ちない。
  const crowded = assignShiftStores({
    insights,
    members: [...kitchen],
    shift: "昼",
    storeIds: twoStores,
    kitchenStaff: kitchen
  });
  assert.equal(crowded.byMaid.size, kitchen.size, "every cook must still get a shop");
  for (const placed of crowded.byMaid.values()) {
    assert.ok(twoStores.includes(placed.storeId), "a cook must land in one of the open shops");
  }

  // 1店しか開かない日は散らしようがない。落ちずに全員そこへ入る。
  const single = assignShiftStores({
    insights,
    members: [...kitchen],
    shift: "昼",
    storeIds: ["s1"],
    kitchenStaff: kitchen
  });
  assert.equal(
    new Set([...single.byMaid.values()].map((placed) => placed.storeId)).size,
    1,
    "with one shop open everyone shares it, cooks included"
  );

  // 記念日の主役が料理担当でも、所属店から動かさない。
  const cook = [...kitchen][0];
  const home = insights.maidTendency[cook].home;
  const other = insights.stores.map((store) => store.id).find((id) => id !== home);
  const pinnedCook = assignShiftStores({
    insights,
    members: [cook, ...[...kitchen].slice(1, 2), ...schedule.roster.filter((n) => !kitchen.has(n)).slice(0, 6)],
    shift: "昼",
    storeIds: [home, other],
    pins: new Map([[cook, { storeId: home, label: "生誕", pickRate: 1 }]]),
    kitchenStaff: kitchen
  });
  assert.equal(
    pinnedCook.byMaid.get(cook).storeId,
    home,
    "a pinned cook stays at her own shop even though cooks are spread out"
  );

  // 実データでも、キッチンにゃんこはたいてい別の店に分かれること。
  // 「必ず分かれる」とは書かない。実測でも 13.7% は2人一緒に入っているし、
  // 2人とも同じ店を強く好むシフトまで無理に離すのは、減点ではなく禁止になる。
  {
    let apart = 0;
    let together = 0;
    for (const [date, day] of Object.entries(schedule.schedule)) {
      for (const shift of insights.shifts) {
        const entries = day[shift] ?? [];
        const members = entries.map((entry) => entry.name);
        const cooksToday = members.filter((name) => kitchen.has(name));
        if (cooksToday.length < 2) {
          continue;
        }
        const pins = eventStorePins({ insights, entries, homeStore: schedule.homeStore });
        const outlook = applyEventCertainty(insights, outlookFor(date, shift), pins);
        const assigned = getShiftAssignment({
          insights,
          members,
          shift,
          outlook,
          pins,
          kitchenStaff: kitchen
        });
        if (!assigned || assigned.storeIds.length < 2) {
          continue;
        }
        const shops = new Set(cooksToday.map((name) => assigned.byMaid.get(name).storeId));
        if (shops.size === cooksToday.length) {
          apart += 1;
        } else {
          together += 1;
        }
      }
    }
    const total = apart + together;
    assert.ok(total >= 3, "the rota must contain a few shifts with two cooks to judge this");
    // 実測は「1店1人」が77.5%。多数が分かれていれば減点は効いている。
    assert.ok(
      apart / total >= 0.6,
      `cooks must usually land in different shops, got ${apart}/${total}`
    );
  }
}

// 顔ぶれの配属で、開きそうな店の順位が変わること。
{
  const shares = insights.homeStaffShare;
  assert.ok(shares, "homeStaffShare must exist");
  for (const shift of insights.shifts) {
    const table = shares[shift];
    assert.ok(table, `homeStaffShare must cover ${shift}`);
    const total = Object.values(table).reduce((sum, value) => sum + value, 0);
    assert.ok(
      Math.abs(total - 1) < 0.01,
      `${shift} shares must add up to 1, got ${total}`
    );
    for (const store of insights.stores) {
      assert.ok(table[store.id] > 0, `${shift} ${store.id} must have a baseline share`);
    }
  }

  // 第4引数は人数でも顔ぶれでもよい。顔ぶれは「どの店か」だけを動かし、
  // 「いくつ開くか」は人数から決まったままにする。ここが崩れると、
  // 顔ぶれの偏りで店舗数まで動いてしまう。
  const future = outlookFor(farFuture, "昼");
  const roster = Object.keys(insights.maidTendency).filter((name) => insights.maidTendency[name]);
  const postedTo = (id) => roster.filter((name) => insights.maidTendency[name].posted === id);
  for (const id of insights.stores.map((store) => store.id)) {
    const lineUp = postedTo(id);
    if (lineUp.length === 0) {
      continue;
    }
    assert.equal(
      expectedOpenStores(insights, "昼", future, lineUp).length,
      expectedOpenStores(insights, "昼", future, lineUp.length).length,
      `a line-up posted to ${id} must open as many shops as its headcount alone would`
    );
  }

  // 2号店配属だけを並べたら、2号店が選ばれること。1号店配属だけなら選ばれないこと。
  const s2Only = postedTo("s2").slice(0, 9);
  const s1Only = postedTo("s1").slice(0, 9);
  assert.ok(s2Only.length >= 4 && s1Only.length >= 4, "the roster must cover both shops");
  const withS2 = expectedOpenStores(insights, "昼", future, s2Only);
  const withS1 = expectedOpenStores(insights, "昼", future, s1Only);
  assert.ok(
    withS2.includes("s2"),
    `a line-up posted to 2号店 must open it, got ${withS2.join("+")}`
  );
  assert.ok(
    !withS1.includes("s2"),
    `a line-up with nobody posted to 2号店 must not open it, got ${withS1.join("+")}`
  );

  // 名前が分からない（配属が引けない）ときは、人数だけのときと同じ結果に戻る。
  const unknown = ["だれか", "べつのだれか", "みっつめ"];
  assert.deepEqual(
    expectedOpenStores(insights, "昼", future, unknown),
    expectedOpenStores(insights, "昼", future, unknown.length),
    "unknown names must fall back to the headcount-only ordering"
  );
}

// 制度変更（上旬・下旬をまとめて事前公開）直後だけ出す注意書き。移行が落ち着けば自動で消える。
{
  const changedAt = insights.scheduleSystemChangedAt;
  assert.match(changedAt, /^\d{4}-\d{2}-\d{2}$/, "scheduleSystemChangedAt must be a date");

  assert.equal(
    scheduleSystemNote(insights, addDays(changedAt, -1)),
    null,
    "nothing to say before the system changed"
  );
  const onTheDay = scheduleSystemNote(insights, changedAt);
  assert.ok(onTheDay, "the note must show on the day the system changed");
  assert.ok(
    onTheDay.includes("提出していない"),
    "the note must explain why some maids are missing"
  );
  assert.ok(onTheDay.includes("9月"), "the note must name the month it changed");

  assert.ok(scheduleSystemNote(insights, addDays(changedAt, 30)), "still relevant a month later");
  assert.equal(
    scheduleSystemNote(insights, addDays(changedAt, 61)),
    null,
    "the note must retire itself once the transition has settled"
  );
  assert.equal(
    scheduleSystemNote({ ...insights, scheduleSystemChangedAt: undefined }, changedAt),
    null,
    "no note without a recorded change"
  );
  assert.equal(scheduleSystemNote(insights, null), null, "no note without a date to compare");
}

// 店舗ごとにまとめる。店は店舗の並び順、店の中はサイト掲載順（渡した順）のまま。
{
  const roster = schedule.roster.slice(0, 8);
  const entries = roster.map((name) => ({ name }));
  const byMaid = new Map([
    [roster[0], { storeId: "s4" }],
    [roster[1], { storeId: "s1" }],
    [roster[2], { storeId: "s4" }],
    [roster[3], { storeId: "s2" }],
    [roster[4], { storeId: "s1" }],
    [roster[5], { storeId: "s2" }],
    [roster[6], { storeId: "s1" }],
    [roster[7], { storeId: "s4" }]
  ]);
  const grouped = groupByAssignedStore({ insights, entries, assignment: { byMaid } });

  assert.deepEqual(
    grouped.map((group) => group.storeId),
    ["s1", "s2", "s4"],
    "groups must come out in shop order, skipping the shops nobody works"
  );
  assert.deepEqual(
    grouped.map((group) => group.entries.length),
    [3, 2, 3],
    "every maid must land in exactly one group"
  );
  for (const group of grouped) {
    const positions = group.entries.map((entry) => roster.indexOf(entry.name));
    assert.deepEqual(
      positions,
      [...positions].sort((a, b) => a - b),
      `${group.storeId}: the line-up must keep the official roster order`
    );
  }

  // 誰いるかモードでは割り振りがないので、一本のリストのまま返す。
  const flat = groupByAssignedStore({ insights, entries, assignment: null });
  assert.equal(flat.length, 1, "without an assignment there is nothing to group by");
  assert.equal(flat[0].storeId, null, "an ungrouped list must not claim a store");
  assert.deepEqual(
    flat[0].entries.map((entry) => entry.name),
    roster,
    "an ungrouped list must keep the order it was given"
  );

  // 割り振り漏れが出ても落とさない。最後にまとめて出す。
  const partial = groupByAssignedStore({
    insights,
    entries,
    assignment: { byMaid: new Map([[roster[1], { storeId: "s1" }]]) }
  });
  assert.deepEqual(
    partial.map((group) => group.storeId),
    ["s1", null],
    "maids without a store must still be listed, after the shops"
  );
  assert.equal(partial[1].entries.length, roster.length - 1, "nobody may be dropped");
}

// 店舗だけ分かっていて顔ぶれの記録が無い日は、実績として扱いつつ、その旨を断る。
{
  const storesOnly = insights.actualWithoutRoster ?? {};
  const dates = Object.keys(storesOnly);
  assert.ok(dates.length > 0, "the fixture must contain a stores-only record to exercise this");

  for (const date of dates) {
    for (const [shift, stores] of Object.entries(storesOnly[date])) {
      // 顔ぶれが無いだけで営業したことは確かなので、見込みには落とさない。
      const outlook = outlookFor(date, shift);
      assert.equal(outlook.basis, "actual", `${date} ${shift} is recorded, even without a line-up`);
      assert.deepEqual(
        [...outlook.openStores].sort(),
        [...stores].sort(),
        `${date} ${shift} must report exactly the stores we were told about`
      );
      assert.ok(
        outlook.summary.includes("誰がいたかの記録はありません"),
        `${date} ${shift} must admit the line-up is unknown`
      );
    }
  }

  // 顔ぶれまで分かっている日には、その断り書きを付けない。
  const fullyRecorded = Object.keys(insights.actual).find(
    (date) => !storesOnly[date] && insights.actual[date]["昼"]
  );
  assert.ok(fullyRecorded, "the fixture must contain a normally recorded day");
  assert.ok(
    !outlookFor(fullyRecorded, "昼").summary.includes("誰がいたかの記録はありません"),
    `${fullyRecorded} has a line-up, so it must not carry the caveat`
  );
}

console.log(
  "Store outlook valid: records, next-day forecast, weekday tendency, " +
    `${partialDates.length} single-shift days, Sunday-based weekday index, sparse-rotation fallbacks, ` +
    `headcount-driven shop counts (1/2/3), capacity-based assignment, store-by-store grouping, ` +
    `and ${eventShifts.length} event shifts pinned to the host's own store.`
);
