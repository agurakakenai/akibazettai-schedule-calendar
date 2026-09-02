"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const {
  addDays,
  applyEventCertainty,
  applyHomeStaff,
  applyPostedTilt,
  assignShiftStores,
  calibrationNote,
  coOpenRate,
  eventStorePins,  expectedOpenStores,
  getMaidStoreOutlook,
  getShiftAssignment,
  getStoreOutlook,
  groupByAssignedStore,
  itineraryConfidence,
  lastActualDateOf,
  maidItinerary,
  nearMissNote,
  nearMissStores,
  openStoresOn,
  openStoresOnDay,
  sameDayDecisionNote,
  scheduleSystemNote,
  sortByAssignedStore,
  spreadNote,
  spreadStanding,
  storeCapacities,
  storeProbabilities,
  storeSizeNote,
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
      // 記録が無い側は、前日の同じシフトが分かっていれば見込み、同じ日のもう片方が
      // 分かっていれば同日の実績から、どちらも無ければ曜日傾向になる。
      // どれであっても「実績」を名乗ってはいけない。
      assert.ok(
        ["forecast", "sameDay", "tendency"].includes(outlook.basis),
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
      // 過ぎた日の抜けは「記録が無いだけ」と断る。ただし記録の最終日そのものは、
      // 欠けている側がまだ来ていないシフトなので、そう書くと誤解させる。
      if (date < lastActual) {
        assert.ok(
          outlook.summary.includes("休みとは限りません"),
          `${date} ${shift} must say the missing record is not a closure`
        );
      }
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
// 「全員が1号店」と書くと、僅差の1人が入れ替わっただけで落ちる。実際に
// 2026-09-02 の実績を足した時点で、ねむりさんが s4 35.5% / s1 34.5% と逆転した。
// 見たいのは「傾向だけだと1つの店に潰れる」ことなので、割合で言う。
const shareWinners = schedule.roster.map((name) => {
  const share = insights.maidTendency[name].share;
  return ["s1", "s2", "s3", "s4"].reduce((top, id) => (share[id] > share[top] ? id : top), "s1");
});
const towardS1 = shareWinners.filter((id) => id === "s1").length;
assert.ok(
  towardS1 >= shareWinners.length * 0.9,
  "share alone points almost every maid at one store, which is why the assignment exists; " +
    `got ${towardS1}/${shareWinners.length} at s1`
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
// 在籍者数は roster が増えれば変わる。文言に焼き込むと黙って古くなるので、
// 表示が実際の人数と一致していることを見る。
assert.ok(
  chip.title.includes(`在籍${schedule.roster.length}名`),
  `the chip must quote the roster it actually has (${schedule.roster.length})`
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

  // README は「いちばん低い人でも 0.018」と書いている。配属を事前分布に入れた影響で
  // 配属外の店の pickRate は下がるので、書いた値と実データがずれていないか見張る。
  {
    const lowest = Math.min(
      ...schedule.roster.flatMap((name) =>
        insights.stores.map((store) => insights.maidTendency[name].pickRate[store.id])
      )
    );
    assert.ok(lowest > 0, "no maid may read as never having worked a shop");
    assert.ok(
      Math.abs(lowest - 0.018) < 0.002,
      `README says the lowest pickRate is 0.018; it is now ${lowest.toFixed(3)}`
    );
  }

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

// 記念日が過去になった日は確率の検査から外れる。全部が過去になると、この節が
// 何も見ないまま通ってしまうので、見込みの日が残っていることを最後に確かめる。
let forecastEventShifts = 0;
let pastEventShifts = 0;

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
  // 記念日が過ぎると、その日は見込みではなく実績になる。実績には確率が無いので
  // 「確定させる」対象でもない（2026-09-01 のもなかさん周年で実際に起きた）。
  // 主役の立ち位置はここまでで見ているので、確率の話だけを飛ばす。
  if (base.basis === "actual") {
    pastEventShifts += 1;
    continue;
  }
  forecastEventShifts += 1;
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

assert.ok(
  forecastEventShifts > 0,
  `at least one event shift must still be a forecast, or this section checks nothing ` +
    `(${pastEventShifts} of ${eventShifts.length} have already happened)`
);

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
  assert.equal(pinned.source, "site", "a pin from the site must say so");

  const fallback = eventStorePins({ insights, entries: host }).get("える");
  assert.equal(fallback.storeId, guessed, "without a posting we fall back on the tendency");
  assert.equal(fallback.source, "record", "a guessed pin must not claim to be a posting");

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

  // 所属はすべて宣言されているので、記念日の主役は推定に落ちない。
  // ただし出どころは2種類ある。公式サイトに載っていない人はお店の案内による。
  const unposted = new Set(schedule.unpostedMaids ?? []);
  for (const [date, day] of Object.entries(schedule.schedule)) {
    for (const [shift, members] of Object.entries(day)) {
      const pins = eventStorePins({
        insights,
        entries: members,
        homeStore: schedule.homeStore,
        unpostedMaids: unposted
      });
      for (const [name, pin] of pins) {
        assert.equal(
          pin.source,
          unposted.has(name) ? "shop" : "site",
          `${date} ${shift}: ${name}'s posting must name where it came from`
        );
        assert.equal(pin.storeId, schedule.homeStore[name], `${name} must stand at her own shop`);
      }
    }
  }

  // 公式サイトに載っていない人には「公式サイトの配属」と書かない。
  assert.ok(unposted.size > 0, "the fixture must contain a maid the site has not listed yet");
  const shopSourced = eventStorePins({
    insights,
    entries: [{ name: [...unposted][0], featured: true, eventLabel: "生誕" }],
    homeStore: schedule.homeStore,
    unpostedMaids: unposted
  }).get([...unposted][0]);
  assert.equal(shopSourced.source, "shop", "a maid the site has not listed is posted by the shop");
}

// 開店店舗数は人数から決まる。最頻値で固定すると3店舗の日も1店舗の日も出せない。
// 境界は実測（openCountByHeadcount）なので、そこから期待値を作る。直書きすると
// 集計をやり直すたびに落ちるし、落ちても「壊れた」のか「実測が動いた」のか分からない。
{
  const tendency = outlookFor(farFuture, "昼");
  const countFor = (poolSize) => expectedOpenStores(insights, "昼", tendency, poolSize).length;
  const limits = insights.openCountByHeadcount["昼"];
  assert.ok(Array.isArray(limits) && limits.length >= 2, "the 昼 thresholds must span three counts");

  // 境界のすぐ下と、そのひとつ上で、店舗数が1つ増えること。
  limits.forEach((limit, index) => {
    assert.equal(countFor(limit), index + 1, `${limit} maids stay at ${index + 1} shop(s)`);
    assert.equal(countFor(limit + 1), index + 2, `${limit + 1} maids need ${index + 2} shops`);
  });
  // 1店舗も3店舗も出せること。どちらかが出ないなら人数を使う意味がない。
  assert.equal(countFor(1), 1, "a tiny line-up opens one shop");
  assert.ok(countFor(20) >= 3, "a large line-up opens three shops");

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
  // 過ぎた記念日は実績になり applyEventCertainty が素通しするので、見込みの日を選ぶ。
  const eventShift = eventShiftsPreview.find(
    (candidate) => outlookFor(candidate.date, candidate.shift).basis !== "actual"
  );
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
  //
  // 顔ぶれを効かせるのは applyPostedTilt の役目で、割合そのものを動かす。
  // expectedOpenStores は動いたあとの割合を素直に上から採るだけ。
  const future = outlookFor(farFuture, "昼");
  const tiltedFor = (lineUp) => applyPostedTilt(insights, future, "昼", lineUp);
  const roster = Object.keys(insights.maidTendency).filter((name) => insights.maidTendency[name]);
  const postedTo = (id) => roster.filter((name) => insights.maidTendency[name].posted === id);
  for (const id of insights.stores.map((store) => store.id)) {
    const lineUp = postedTo(id);
    if (lineUp.length === 0) {
      continue;
    }
    assert.equal(
      expectedOpenStores(insights, "昼", tiltedFor(lineUp), lineUp).length,
      expectedOpenStores(insights, "昼", future, lineUp.length).length,
      `a line-up posted to ${id} must open as many shops as its headcount alone would`
    );
  }

  // 2号店配属だけを並べたら、2号店が選ばれること。1号店配属だけなら選ばれないこと。
  const s2Only = postedTo("s2").slice(0, 9);
  const s1Only = postedTo("s1").slice(0, 9);
  assert.ok(s2Only.length >= 4 && s1Only.length >= 4, "the roster must cover both shops");
  const withS2 = expectedOpenStores(insights, "昼", tiltedFor(s2Only), s2Only);
  const withS1 = expectedOpenStores(insights, "昼", tiltedFor(s1Only), s1Only);
  assert.ok(
    withS2.includes("s2"),
    `a line-up posted to 2号店 must open it, got ${withS2.join("+")}`
  );
  assert.ok(
    !withS1.includes("s2"),
    `a line-up with nobody posted to 2号店 must not open it, got ${withS1.join("+")}`
  );

  // 画面に出す割合と、選ばれた店が食い違わないこと。
  // 以前は順位づけだけが顔ぶれを見ていて、「48%の店が空で39%の店に4人」が出ていた。
  // 読み手には理由が見えないので、選ぶ物差しは表示している数字そのものにする。
  for (const lineUp of [s2Only, s1Only, postedTo("s3").slice(0, 9), postedTo("s4").slice(0, 9)]) {
    if (lineUp.length === 0) {
      continue;
    }
    const shown = tiltedFor(lineUp);
    const chosen = new Set(expectedOpenStores(insights, "昼", shown, lineUp));
    const rateOf = (id) => shown.entries.find((entry) => entry.store.id === id)?.rate ?? 0;
    const lowestChosen = Math.min(...[...chosen].map(rateOf));
    for (const entry of shown.entries) {
      if (chosen.has(entry.store.id)) {
        continue;
      }
      assert.ok(
        entry.rate <= lowestChosen,
        `${entry.store.id} shows ${entry.text} but was left out while a lower shop was chosen`
      );
    }
  }

  // 名前が分からない（配属が引けない）ときは、人数だけのときと同じ結果に戻る。
  const unknown = ["だれか", "べつのだれか", "みっつめ"];
  assert.deepEqual(
    expectedOpenStores(insights, "昼", tiltedFor(unknown), unknown),
    expectedOpenStores(insights, "昼", future, unknown.length),
    "unknown names must fall back to the headcount-only ordering"
  );
  assert.equal(
    applyPostedTilt(insights, future, "昼", unknown),
    future,
    "with nobody recognisable, the numbers must be left alone"
  );
}

// 同じ日のもう片方に実績があれば、曜日傾向ではなくそこから見る。
// 2号店と3号店は同じ日のうちに入れ替わらないので、これがいちばん効く。
{
  const partial = Object.keys(insights.actual).filter(
    (date) => Object.keys(insights.actual[date]).length === 1
  );
  assert.ok(partial.length > 0, "the fixture must contain a single-shift day");

  let seen = 0;
  for (const date of partial) {
    const recorded = insights.shifts.find((shift) => insights.actual[date][shift]);
    const missing = insights.shifts.find((shift) => shift !== recorded);
    const outlook = outlookFor(date, missing);
    if (outlook.basis !== "sameDay") {
      continue;   // 前日の実績があればそちらが優先される
    }
    seen += 1;
    assert.deepEqual(
      [...outlook.knownStores].sort(),
      [...insights.actual[date][recorded]].sort(),
      `${date} must quote the shift it actually knows`
    );
    assert.equal(outlook.knownShift, recorded, "it must name which shift it read");

    // 昼に2号店が開いていたなら、夜の3号店はほぼ無い。逆も同じ。
    const known = new Set(outlook.knownStores);
    const rateOf = (id) => outlook.entries.find((entry) => entry.store.id === id).rate;
    if (known.has("s2") && !known.has("s3")) {
      assert.ok(
        rateOf("s3") < rateOf("s2"),
        `${date} ${missing}: 2号店が開いていた日に3号店を上に置いてはいけない`
      );
      assert.ok(rateOf("s3") <= 0.05, `${date} ${missing}: 3号店はほぼ無いはず`);
    }
    if (known.has("s3") && !known.has("s2")) {
      assert.ok(
        rateOf("s2") < rateOf("s3"),
        `${date} ${missing}: 3号店が開いていた日に2号店を上に置いてはいけない`
      );
    }
    // 1号店と4号店は曜日傾向のまま。全店に同日ルールを当てると4号店が悪化する。
    const weekday = insights.weekdayOpenRate[missing][weekdayBucket(insights, date)];
    for (const id of ["s1", "s4"]) {
      assert.equal(rateOf(id), weekday[id], `${date} ${missing}: ${id} は曜日傾向のまま`);
    }
    // 過ぎた日に出るときは、記録が無いだけで休みではないと断る。
    // 記録の最終日は別で、欠けている側はまだ来ていないシフトなので断らない。
    if (date < lastActual) {
      assert.ok(
        outlook.summary.includes("休みとは限りません"),
        `${date} ${missing} must say the missing record is not a closure`
      );
    }
    if (date === lastActual) {
      assert.ok(
        !outlook.summary.includes("休みとは限りません"),
        `${date} ${missing} has not happened yet, so it is not a missing record`
      );
    }
  }

  // 記録の最終日で欠けているシフトは、どの経路に落ちても「記録がありません」と
  // 言ってはいけない。まだ来ていないだけで、取りこぼしたわけではない。
  for (const shift of insights.shifts) {
    if (insights.actual[lastActual][shift]) {
      continue;
    }
    const outlook = outlookFor(lastActual, shift);
    assert.ok(
      !outlook.summary.includes("記録だけが手元にありません"),
      `${lastActual} ${shift} is still to come, whatever ${outlook.basis} it falls back to`
    );
  }

  // 逆に、過ぎた日で片方だけ欠けているなら、どの経路でも必ず断る。
  for (const date of Object.keys(insights.actual)) {
    if (date >= lastActual || Object.keys(insights.actual[date]).length !== 1) {
      continue;
    }
    const missing = insights.shifts.find((shift) => !insights.actual[date][shift]);
    assert.ok(
      outlookFor(date, missing).summary.includes("休みとは限りません"),
      `${date} ${missing} is a real gap, so it must not read as a closure`
    );
  }
  assert.ok(seen > 0, "the fixture must exercise the same-day path at least once");

  // rotation.sameDay は「昼の実績 → 夜の見込み」の向きに作られた表で、
  // 逆向きの分布とは別物。実測（記録176日）では夜が2号店だった日の昼は
  // 2号店58%だが、この表を流用すると40%になる。逆向きには使わない。
  const nightOnly = Object.keys(insights.actual).filter(
    (date) => insights.actual[date][insights.shifts[1]] && !insights.actual[date][insights.shifts[0]]
  );
  for (const date of nightOnly) {
    const outlook = outlookFor(date, insights.shifts[0]);
    assert.notEqual(
      outlook.basis,
      "sameDay",
      `${date}: the same-day table only runs ${insights.shifts[0]} to ${insights.shifts[1]}`
    );
  }

  // 昼だけ記録のある日は、逆に必ず使う（前日の実績が無いかぎり）。
  const dayOnly = Object.keys(insights.actual).filter(
    (date) => insights.actual[date][insights.shifts[0]] && !insights.actual[date][insights.shifts[1]]
  );
  const usable = dayOnly.filter(
    (date) => !openStoresOn(insights, addDays(date, -1), insights.shifts[1])
  );
  for (const date of usable) {
    assert.equal(
      outlookFor(date, insights.shifts[1]).basis,
      "sameDay",
      `${date}: the ${insights.shifts[0]} record must beat the weekday tendency`
    );
  }
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
// 顔ぶれの記録が届くと、その日は openings.csv から shifts.csv に移り、ここの検体は
// 消える（2026-09-02 が実際にそうなった）。検体の有無で検査が消えないよう、
// 合成した1日で仕組みを固定し、実データがあるときは追加で確かめる。
{
  const probeDate = "2026-08-31";
  const probe = JSON.parse(JSON.stringify(insights));
  probe.actual[probeDate] = { 昼: ["s1"] };
  probe.actualWithoutRoster = { [probeDate]: { 昼: ["s1"] } };
  const probed = getStoreOutlook({
    insights: probe,
    dateKey: probeDate,
    shift: "昼",
    lastActualDate: lastActualDateOf(probe)
  });
  assert.equal(probed.basis, "actual", "a stores-only day is recorded, even without a line-up");
  assert.deepEqual(
    [...probed.openStores].sort(),
    ["s1"],
    "a stores-only day must report exactly the stores we were told about"
  );
  assert.ok(
    probed.summary.includes("誰がいたかの記録はありません"),
    "a stores-only day must admit the line-up is unknown"
  );

  const storesOnly = insights.actualWithoutRoster ?? {};
  for (const date of Object.keys(storesOnly)) {
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

// 端の確率は自信過剰なので、そこに落ちたときだけツールチップで断る。
// 帯そのものは data 側の測定（accuracy.calibration）から引き、閾値は app.js に持たない。
{
  const buckets = [
    { from: 0, to: 0.1, n: 526, actual: 0.226 },
    { from: 0.2, to: 0.3, n: 1130, actual: 0.243 },
    { from: 0.4, to: 0.5, n: 2081, actual: 0.448 },
    { from: 0.6, to: 0.7, n: 1595, actual: 0.693 },
    { from: 0.9, to: 1, n: 401, actual: 0.738 }
  ];
  const measured = { accuracy: { calibration: { buckets } } };

  // 真ん中はずれが小さいので何も言わない。
  for (const rate of [0.25, 0.45, 0.65]) {
    assert.equal(
      calibrationNote(measured, rate),
      null,
      `${rate} sits in the range that holds up, so it needs no caveat`
    );
  }

  // 高すぎる側。実測を添えて「4回に1回外す」ことが読めるようにする。
  const high = calibrationNote(measured, 0.97);
  assert.ok(high, "a figure in the nineties must carry a caveat");
  assert.ok(high.includes("自信過剰"), "the caveat must say the figure is too confident");
  assert.ok(high.includes("74%"), "the caveat must quote what actually happened");
  assert.ok(high.includes("90%以上"), "the caveat must name the band it is talking about");

  // 低すぎる側。「行かない」と読まれないようにする。
  const low = calibrationNote(measured, 0.03);
  assert.ok(low, "a single-digit figure must carry a caveat");
  assert.ok(low.includes("23%"), "the caveat must say how often it actually happens");

  // 標本の少ないバケットは実測が揺れるので、根拠にしない。
  const thin = { accuracy: { calibration: { buckets: [{ from: 0.9, to: 1, n: 12, actual: 0.4 }] } } };
  assert.equal(calibrationNote(thin, 0.95), null, "12 samples cannot support a caveat");

  // 測定が無ければ黙る。データを消しても壊れない。
  assert.equal(calibrationNote({}, 0.97), null, "no measurement, no claim");
  assert.equal(calibrationNote(insights, null), null, "no rate, no claim");
  assert.equal(
    calibrationNote({ accuracy: { calibration: { buckets: [] } } }, 0.97),
    null,
    "an empty table must not throw"
  );

  // この測定は2店舗以上開いたシフトだけを見ている。候補が1つの日は
  // 「その店にいる」が定義上100%になるため、データ側で除外されている。
  // そこにこの数字を当てると、測っていない母数の値を引くことになる。
  assert.equal(
    calibrationNote(measured, 1, 1),
    null,
    "a shift with one candidate is outside what the calibration measured"
  );
  assert.ok(calibrationNote(measured, 0.97, 2), "two candidates are inside the measurement");

  // 対象範囲がデータ側で変わったら、それに従う。
  const stricter = {
    accuracy: { calibration: { buckets, minOpenStores: 3 } }
  };
  assert.equal(
    calibrationNote(stricter, 0.97, 2),
    null,
    "the data decides how many shops the measurement needs, not app.js"
  );

  // データ側は範囲を scope で書いている。そちらでも同じ判定になること。
  const scoped = { accuracy: { calibration: { buckets, scope: "twoOrMoreOpen" } } };
  assert.equal(calibrationNote(scoped, 1, 1), null, "scope must exclude single-shop shifts");
  assert.ok(calibrationNote(scoped, 0.97, 2), "scope must allow two-shop shifts");
  assert.equal(
    insights.accuracy.calibration.scope,
    "twoOrMoreOpen",
    "the shipped data must say what the calibration covers"
  );

  // 測っているのは「開いた店が分かっている日の、人の配置」だけ。
  // 開く店の予測を外すぶんは含まれないので、そこも断る。
  assert.ok(
    calibrationNote(scoped, 0.97, 2).includes("開く店の予測を外すぶんは含みません"),
    "the caveat must not be read as covering the shop guess as well"
  );

  // 実データに測定が入ったら、チップの本文にも出ること。
  if (insights.accuracy?.calibration?.buckets?.length > 0) {
    const noted = schedule.roster
      .map((name) =>
        getMaidStoreOutlook({ insights, name, shift: "昼", outlook: futureOutlook, assignment })
      )
      .filter((chip) => chip && calibrationNote(insights, chip.rate, assignment.storeIds.length));
    for (const chip of noted) {
      assert.ok(
        chip.title.includes("自信過剰") || chip.title.includes("控えめすぎ"),
        "a chip whose figure lands in an unreliable band must say so in its tooltip"
      );
    }
  }

  // 候補が1つの日は、100% の理由を構造で説明する。較正の数字は引かない。
  {
    const sole = getShiftAssignment({
      insights,
      members: schedule.roster.slice(0, 4),
      shift: "昼",
      outlook: {
        ...futureOutlook,
        entries: futureOutlook.entries.map((entry) => ({
          ...entry,
          rate: entry.store.id === "s1" ? 0.99 : 0.001
        }))
      }
    });
    assert.equal(sole.storeIds.length, 1, "four maids must fit in a single shop");
    const chip = getMaidStoreOutlook({
      insights,
      name: schedule.roster[0],
      shift: "昼",
      outlook: futureOutlook,
      assignment: sole
    });
    assert.equal(chip.percent, "100%", "with one shop open the probability is one by construction");
    assert.ok(
      chip.title.includes("だけが開く見込み"),
      "the tooltip must say the 100% comes from the shop guess, not from her record"
    );
    assert.ok(
      !chip.title.includes("自信過剰"),
      "the calibration figure excludes single-shop shifts, so it must not be quoted here"
    );
  }
}

// 見習いにゃんこは予定表に出ないので、カレンダーの人数より実際は多い。
// 平均（0.83人など）では伝わらないので、「何割の枠にいたか」で言う。
{
  const coverage = insights.rosterCoverage;
  assert.ok(coverage?.byStore, "rosterCoverage must carry a per-store breakdown");

  for (const store of insights.stores) {
    const note = storeSizeNote(insights, "昼", store.id);
    const measured = coverage.byStore[store.id];
    assert.ok(note.includes("見習いにゃんこ"), `${store.id}: the note must mention trainees`);
    assert.ok(
      note.includes(`${Math.round((1 - measured.shiftsWithoutUnlisted) * 100)}%の枠`),
      `${store.id}: the share must come from rosterCoverage, not from prose`
    );
  }

  // 4号店だけ見習いが少ない。そこが埋もれていないこと。
  const withAny = (id) => 1 - coverage.byStore[id].shiftsWithoutUnlisted;
  assert.ok(
    withAny("s4") < Math.min(withAny("s1"), withAny("s2"), withAny("s3")),
    "4号店 must read as the shop that most often has no trainee"
  );

  // 集計期間も出す。店舗側の数字と個人側で期間が違うので、混同させない。
  assert.ok(
    storeSizeNote(insights, "昼", "s1").includes("か月"),
    "the note must say how long the trainee figure was measured over"
  );

  // 測定が無ければ何も言わない。人数の話だけ残る。
  const bare = {
    stores: insights.stores,
    headcountProfile: insights.headcountProfile
  };
  const withoutCoverage = storeSizeNote(bare, "昼", "s1");
  assert.ok(withoutCoverage, "the headcount half must survive on its own");
  assert.ok(
    !withoutCoverage.includes("見習い"),
    "without a measurement there is nothing to say about trainees"
  );
  assert.equal(storeSizeNote(bare, "昼", "知らない店"), null, "an unknown shop has no note");
}

// 2番手の店は、予定表の顔ぶれの配属から読み直す。置き換えではなく尤度比で更新
// するので、同日ルールのような強い手がかりは残り、読めない店は動かない。
{
  const table = insights.secondStoreByHome;
  assert.ok(table, "secondStoreByHome must exist");
  assert.ok(!table.s1, "the first shop is not the second shop");

  const homeStore = schedule.homeStore;
  const base = outlookFor(farFuture, "昼");
  const rateOf = (outlook, id) => outlook.entries.find((e) => e.store.id === id).rate;
  const lineUp = (counts) =>
    Object.entries(counts).flatMap(([id, n]) =>
      schedule.roster.filter((name) => homeStore[name] === id).slice(0, n)
    );

  // 配属者が多い店は上がり、少ない店は下がる。
  const manyS2 = applyHomeStaff(insights, base, lineUp({ s2: 4, s3: 1, s4: 1 }), homeStore);
  const manyS3 = applyHomeStaff(insights, base, lineUp({ s2: 1, s3: 4, s4: 1 }), homeStore);
  assert.ok(
    rateOf(manyS2, "s2") > rateOf(manyS3, "s2"),
    "four maids posted to 2号店 must lift it above a line-up with one"
  );
  assert.ok(
    rateOf(manyS3, "s3") > rateOf(manyS2, "s3"),
    "the same must hold for 3号店"
  );

  // 4号店は読めない。配属者0人と4人でほとんど差が出ないこと。
  const noS4 = applyHomeStaff(insights, base, lineUp({ s2: 1, s3: 1, s4: 0 }), homeStore);
  const allS4 = applyHomeStaff(insights, base, lineUp({ s2: 1, s3: 1, s4: 4 }), homeStore);
  const s4Swing = Math.abs(rateOf(allS4, "s4") - rateOf(noS4, "s4"));
  const s2Swing = Math.abs(rateOf(manyS2, "s2") - rateOf(manyS3, "s2"));
  assert.ok(
    s4Swing < s2Swing,
    `4号店 must move less than 2号店 (${s4Swing.toFixed(3)} vs ${s2Swing.toFixed(3)})`
  );

  // 1号店は触らない。開く店の数の見込みも動かさない。
  assert.equal(rateOf(manyS2, "s1"), rateOf(base, "s1"), "the first shop is left alone");  const sumOf = (outlook) =>
    ["s2", "s3", "s4"].reduce((total, id) => total + rateOf(outlook, id), 0);
  assert.ok(
    Math.abs(sumOf(manyS2) - sumOf(base)) < 0.001,
    "shifting the balance must not change how many shops are expected to open"
  );

  // 文言に出る割合は表から出ていること。直書きだと集計をやり直したときに
  // 古くなり、しかも古くなったことに誰も気づかない。表の値を1つずつ
  // 文言と突き合わせて、どれかが欠けていたら落とす。
  {
    const summary = manyS2.summary;
    // 出す/出さないの境目は app.js の SECOND_STORE_MIN_SAMPLE と同じでなければ
    // ならない。data 側が緩いと、薄いバケットを app が弾いた拍子に「配属を読む」
    // 処理ごと無効になる。文言から実際に使われている閾値を読み取って照合する。
    const MIN_BUCKET = 20;
    // data 側が MIN_BUCKET より薄いバケットを出していたら、そこを引いた瞬間に
    // app が null を返し、配属の読み取りが丸ごと止まる。実際に n=12 のバケットで
    // そうなった。データ側の足切りが app と同じであることを直接見る。
    for (const [id, rows] of Object.entries(insights.secondStoreByHome)) {
      for (const [count, row] of Object.entries(rows)) {
        assert.ok(
          row.n >= MIN_BUCKET,
          `${id}: the ${count}-maid bucket rests on ${row.n} shifts, below the ` +
            `${MIN_BUCKET} app.js requires. app will read null and stop reading ` +
            "the rota at all, for every shop, without saying so."
        );
      }
    }
    const pct = (rate) => `${Math.round(rate * 100)}%`;
    for (const [id, rows] of Object.entries(insights.secondStoreByHome)) {
      const rates = Object.values(rows)
        .filter((entry) => typeof entry?.rate === "number" && entry.n >= MIN_BUCKET)
        .map((entry) => entry.rate);
      if (rates.length < 2) {
        continue;
      }
      const low = Math.min(...rates);
      const high = Math.max(...rates);
      assert.ok(
        summary.includes(pct(low)) && summary.includes(pct(high)),
        `${id}: the tooltip must quote ${pct(low)}〜${pct(high)} from the table, ` +
          `not a figure typed in by hand. Got: ${summary}`
      );
    }
    // 読めない店を「読めません」と名指ししていること。
    const flattest = Object.entries(insights.secondStoreByHome)
      .map(([id, rows]) => {
        const rates = Object.values(rows)
          .filter((entry) => typeof entry?.rate === "number" && entry.n >= MIN_BUCKET)
          .map((entry) => entry.rate);
        return { id, width: rates.length >= 2 ? Math.max(...rates) - Math.min(...rates) : 1 };
      })
      .reduce((a, b) => (a.width <= b.width ? a : b));
    const short = insights.stores.find((store) => store.id === flattest.id).short;
    assert.ok(
      summary.includes(`${short}は配属者が何人でも`),
      `${short} moves least, so the tooltip must be the one saying it cannot be read`
    );
  }

  // キッチンにゃんこは配属の数に入れない。data 側の表も外して作ってあるので、
  // ここで数え方がずれると、引くバケットがずれたまま誰も気づかない。
  // キッチンだけを足しても結果が動かないことで、外れていることを見る。
  {
    const kitchen = [...(schedule.kitchenStaff ?? [])];
    assert.ok(kitchen.length > 0, "there should be cooks to exclude");
    const cook = kitchen.find((name) => schedule.homeStore?.[name]);
    assert.ok(cook, "at least one cook must have a posted shop to test with");
    const plain = lineUp({ s2: 1, s3: 1, s4: 1 });
    const withCook = [...plain, cook];
    const a = applyHomeStaff(insights, base, plain, homeStore, schedule.kitchenStaff);
    const b = applyHomeStaff(insights, base, withCook, homeStore, schedule.kitchenStaff);
    for (const id of ["s2", "s3", "s4"]) {
      assert.equal(
        rateOf(b, id),
        rateOf(a, id),
        `adding ${cook}, a cook posted to ${schedule.homeStore[cook]}, must not move ${id}: ` +
          "the table was built without cooks, so counting them here reads the wrong bucket"
      );
    }
    // フロアの方を足せば動く（検査が素通りしていないことの確認）
    const floor = schedule.roster.find(
      (name) => schedule.homeStore?.[name] === "s2" && !kitchen.includes(name)
    );
    assert.ok(floor, "expected a floor maid posted to 2号店");
    const c = applyHomeStaff(insights, base, [...plain, floor], homeStore, schedule.kitchenStaff);
    assert.ok(
      rateOf(c, "s2") > rateOf(a, "s2"),
      "adding a floor maid posted to 2号店 must lift it, or the test proves nothing"
    );
  }

  // 実績の日は書き換えない。
  const recorded = outlookFor(lastActual, insights.shifts.find((s) => insights.actual[lastActual][s]));  assert.equal(
    applyHomeStaff(insights, recorded, lineUp({ s2: 4 }), homeStore),
    recorded,
    "a recorded shift is not a guess to be adjusted"
  );

  // 同日ルールが出した「ほぼ無い」は、顔ぶれで持ち上げない。
  const nearZero = {
    ...base,
    basis: "sameDay",
    entries: base.entries.map((entry) => ({
      ...entry,
      rate: entry.store.id === "s3" ? 0.01 : entry.rate,
      text: entry.store.id === "s3" ? "1%" : entry.text
    }))
  };
  const kept = applyHomeStaff(insights, nearZero, lineUp({ s2: 1, s3: 4, s4: 1 }), homeStore);
  assert.ok(
    rateOf(kept, "s3") < 0.1,
    `a shop the same-day rule ruled out must stay ruled out, got ${rateOf(kept, "s3")}`
  );

  // 測定が無ければ何もしない。
  assert.equal(
    applyHomeStaff({ ...insights, secondStoreByHome: undefined }, base, lineUp({ s2: 4 }), homeStore),
    base,
    "without the table there is nothing to apply"
  );
  assert.equal(applyHomeStaff(insights, null, [], homeStore), null, "no outlook, no work");

  // 配属が引けない顔ぶれでも落ちない（全員0人扱い）。
  const strangers = applyHomeStaff(insights, base, ["知らない人", "べつのだれか"], homeStore);
  assert.ok(strangers.entries.every((entry) => entry.rate >= 0 && entry.rate <= 1), "rates stay sane");

  // 標本の薄いバケットを1つ引いただけで、読み取りが丸ごと止まってはいけない。
  // 止まっても画面は何も言わないので、黙って効かなくなるのがいちばん困る。
  {
    const thin = JSON.parse(JSON.stringify(insights));
    thin.secondStoreByHome.s3 = { "0": { rate: 0.2, n: 5 } };
    const lots = lineUp({ s2: 1, s3: 4, s4: 1 });
    const readable = applyHomeStaff(insights, base, lots, homeStore);
    const partial = applyHomeStaff(thin, base, lots, homeStore);
    assert.notEqual(partial, base, "one unreadable shop must not switch the whole thing off");

    // 引けない店は、自分の配属者数では動かない。4人いても持ち上がらないこと。
    assert.ok(
      rateOf(partial, "s3") < rateOf(readable, "s3"),
      "a shop we cannot read must not be lifted by its own count"
    );
    assert.ok(
      Math.abs(rateOf(partial, "s3") - rateOf(base, "s3")) <
        Math.abs(rateOf(readable, "s3") - rateOf(base, "s3")),
      "it must stay near where it was, moving only with the renormalisation"
    );
    // 引ける店は動く。
    assert.notEqual(
      rateOf(partial, "s2"),
      rateOf(base, "s2"),
      "the shops we can read must still move"
    );

    // どの店も引けないなら、何もしないで返す。
    const allThin = JSON.parse(JSON.stringify(insights));
    for (const id of Object.keys(allThin.secondStoreByHome)) {
      allThin.secondStoreByHome[id] = { "0": { rate: 0.2, n: 1 } };
    }
    assert.equal(
      applyHomeStaff(allThin, base, lineUp({ s2: 4 }), homeStore),
      base,
      "nothing readable, nothing to say"
    );
  }

  // キッチンにゃんこは配属と実際が合わないので数から外す。画面の顔ぶれを
  // 数えた読み手が混乱しないよう、いる日だけその旨を書く。
  {
    const cooks = schedule.kitchenStaff;
    assert.ok(cooks.length > 0, "the fixture must have kitchen staff");
    const cook = cooks[0];
    const floor = schedule.roster.find(
      (name) => !cooks.includes(name) && schedule.homeStore[name] === schedule.homeStore[cook]
    );
    assert.ok(floor, "the fixture must have a floor maid posted to the same shop");

    // キッチンを足しても数字は動かない。動くのは注記だけ。data 側も同じ数え方を
    // しているので、ここがずれると引くバケットがずれて黙って別の答えになる。
    const withoutCook = applyHomeStaff(insights, base, [floor], homeStore, cooks);
    const withCook = applyHomeStaff(insights, base, [floor, cook], homeStore, cooks);
    assert.deepEqual(
      withCook.entries.map((entry) => entry.rate),
      withoutCook.entries.map((entry) => entry.rate),
      "adding a cook must not change the figures"
    );

    // 同じ配属のフロアを足すと動く。つまり「数えていない」のはキッチンだけ。
    const floor2 = schedule.roster.find(
      (name) =>
        name !== floor && !cooks.includes(name) && schedule.homeStore[name] === schedule.homeStore[cook]
    );
    if (floor2) {
      assert.notDeepEqual(
        applyHomeStaff(insights, base, [floor, floor2], homeStore, cooks).entries.map((e) => e.rate),
        withoutCook.entries.map((entry) => entry.rate),
        "a second floor maid at the same shop must move the figures"
      );
    }

    assert.ok(
      withCook.summary.includes("キッチンにゃんこ1人"),
      "a shift with a cook must say she was not counted"
    );
    assert.ok(
      !withoutCook.summary.includes("キッチン"),
      "a shift without cooks must not mention them"
    );
  }


  {
    // 前日の実績がある日は、その日付を名指しする。
    const afterRecord = outlookFor(addDays(lastActual, 1), insights.shifts[0]);
    assert.equal(afterRecord.basis, "forecast", "the day after a record must forecast from it");
    assert.ok(
      afterRecord.summary.includes(lastActual),
      "a forecast must name the day it read"
    );

    // 前日が分からない日は、そう書く。曜日の平均しか無いことが読めるように。
    const far = outlookFor(farFuture, insights.shifts[0]);
    assert.equal(far.basis, "tendency", "a day with no yesterday falls back on the weekday");
    assert.ok(
      far.summary.includes("前日の実績が手元にない"),
      "the weekday fallback must say why it is only a weekday average"
    );
    assert.ok(
      !far.summary.includes(lastActual),
      "the weekday fallback must not look like it read a record"
    );

    // 曜日傾向そのままなら「予測ではありません」と言える。
    assert.ok(far.summary.includes("予測ではありません"), "an untouched weekday rate is not a guess");

    // 顔ぶれで配分を寄せたら、その一文は外す。表示している数字がもう
    // 曜日の営業率そのものではないので、残すと本文の中で矛盾する。
    const adjusted = applyHomeStaff(insights, far, lineUp({ s2: 4, s3: 1, s4: 1 }), homeStore);
    assert.notEqual(adjusted, far, "the fixture must actually exercise the adjustment");
    assert.ok(
      !adjusted.summary.includes("予測ではありません"),
      "a figure moved by the line-up must not still claim to be no guess at all"
    );
    assert.ok(
      adjusted.summary.includes("配分を寄せています"),
      "the adjusted summary must say what moved it"
    );
  }
}

// 境目が僅差のとき、選ばなかった店も同じくらいあり得る。上位k で切る形自体は
// 妥当（確率で切るとどの閾値でも悪化する）が、境目に根拠がないことは言う。
{
  const base = outlookFor(farFuture, "昼");
  const rateOf = (o, id) => o.entries.find((e) => e.store.id === id).rate;

  // 2位と3位を僅差にすると、3位が僅差として出る。
  const close = {
    ...base,
    entries: base.entries.map((entry) => {
      const rates = { s1: 0.98, s2: 0.5, s3: 0.47, s4: 0.1 };
      const rate = rates[entry.store.id];
      return { ...entry, rate, text: `${Math.round(rate * 100)}%` };
    })
  };
  const chosen = expectedOpenStores(insights, "昼", close, 8);
  assert.deepEqual(chosen, ["s1", "s2"], "eight maids open the two likeliest shops");
  assert.deepEqual(
    nearMissStores(insights, "昼", close, 8),
    ["s3"],
    "a shop three points behind the last pick is a near miss"
  );

  // 大差なら僅差ではない。
  const clear = {
    ...base,
    entries: base.entries.map((entry) => {
      const rates = { s1: 0.98, s2: 0.6, s3: 0.15, s4: 0.1 };
      const rate = rates[entry.store.id];
      return { ...entry, rate, text: `${Math.round(rate * 100)}%` };
    })
  };
  assert.deepEqual(
    nearMissStores(insights, "昼", clear, 8),
    [],
    "a shop far behind was not a close call"
  );

  // 実績の日には出さない。決まっているので僅差もない。
  const recordedShift = insights.shifts.find((s) => insights.actual[lastActual][s]);
  assert.deepEqual(
    nearMissStores(insights, recordedShift, outlookFor(lastActual, recordedShift), 8),
    [],
    "a recorded shift has no near miss"
  );

  // 本文は、僅差であること・確率を足せないこと・片方にしか出せないことを言う。
  const note = nearMissNote(insights, "昼", close, 8);
  assert.ok(note, "a near miss must be explained");
  assert.ok(note.includes("僅差"), "the note must say the call was close");
  assert.ok(
    note.includes("まだ決まっていません"),
    "a close call is not a bad guess; the shop has not been chosen yet"
  );
  assert.ok(note.includes("足さないでください"), "the note must warn against adding the two up");
  assert.ok(
    note.includes("一方にしか出せない"),
    "the note must say why only one shop got a line-up"
  );  assert.equal(nearMissNote(insights, "昼", clear, 8), null, "no near miss, no note");

  // 同時開店の割合は記録から数える。足し算できない理由の裏づけ。
  const co = coOpenRate(insights, "s2", "s3");
  assert.ok(co && co.shifts > 0, "the co-open rate must come from the records");
  assert.ok(co.rate < 0.1, `2号店と3号店が揃うのは稀なはず、実測 ${co.rate}`);
  assert.deepEqual(coOpenRate(insights, "s3", "s2"), co, "the pair is unordered");
  assert.equal(coOpenRate({}, "s2", "s3"), null, "no records, no rate");
}

// どの店を開けるかは当日決まる。何店開くかは事前に読める。この2つを混ぜない。
{
  const guess = outlookFor(farFuture, "昼");
  const note = sameDayDecisionNote(insights, guess);
  assert.ok(note, "a guess must say the shop has not been chosen yet");
  assert.ok(note.includes("当日決まります"), "the note must say when the shop is decided");
  assert.ok(
    note.includes("何店開くか"),
    "the note must separate the count, which is knowable, from the shops, which are not"
  );

  // 実績の日は決まっている。言うことがない。
  const recordedShift = insights.shifts.find((s) => insights.actual[lastActual][s]);
  assert.equal(
    sameDayDecisionNote(insights, outlookFor(lastActual, recordedShift)),
    null,
    "a recorded shift was decided long ago"
  );
  assert.equal(sameDayDecisionNote(insights, null), null, "no outlook, no note");
}

// --- 人ごとの一覧 -------------------------------------------------------
{
  // 「うるちゃんは日によって変わります」と言えるかどうかは spread で決まる。
  // 在籍が実際に3つの帯に割れていないと、その言い分けは飾りになる。
  const bands = schedule.roster
    .map((name) => spreadStanding(insights, name))
    .filter(Boolean);
  assert.equal(bands.length, schedule.roster.length, "every maid must have a measured spread");
  assert.deepEqual(
    [...new Set(bands.map((standing) => standing.band))].sort(),
    ["mixed", "roving", "settled"],
    "spread must separate the roster into three bands"
  );
  // 区切りはデータ側にある。ここに数字を書くと、集計をやり直した日に画面だけが古くなる。
  const cuts = insights.spreadBands;
  assert.ok(cuts.settled > cuts.mixed, "the published bands must be ordered");
  for (const standing of bands) {
    const expected =
      standing.spread >= cuts.settled ? "settled" : standing.spread >= cuts.mixed ? "mixed" : "roving";
    assert.equal(
      standing.band,
      expected,
      `spread ${standing.spread} must follow the published bands, not a threshold typed into app.js`
    );
  }
  assert.equal(
    spreadStanding({ maidTendency: { だれか: { spread: 0.5 } } }, "だれか"),
    null,
    "without published bands there is nothing to say"
  );
  // 順位は散らばりが小さい人ほど後ろ。1番が最も行き先の決まっている人。
  const ranked = [...bands].sort((a, b) => a.rank - b.rank);
  assert.deepEqual(
    ranked.map((standing) => standing.spread),
    [...ranked.map((standing) => standing.spread)].sort((a, b) => b - a),
    "rank 1 must be the maid whose destination is most settled"
  );

  // キッチンにゃんこは配属と関係なく4店を回る。この2つの事実が離れたら落とす。
  const kitchen = schedule.kitchenStaff.map((name) => spreadStanding(insights, name));
  assert.ok(kitchen.length > 0 && kitchen.every(Boolean), "the kitchen staff must have spreads");
  assert.ok(
    kitchen.every((standing) => standing.band !== "settled"),
    "the kitchen staff move between all four shops, so none of them can read as settled"
  );
  const cook = schedule.kitchenStaff.find(
    (name) => spreadStanding(insights, name).band === "roving"
  );
  assert.ok(cook, "at least one kitchen maid must land in the roving band");
  assert.ok(
    spreadNote(insights, cook, true).includes("キッチン"),
    "a roving kitchen maid must be told apart from a roving floor maid"
  );
  assert.equal(spreadStanding(insights, "いない人"), null, "an unknown maid has no standing");
  assert.equal(spreadNote(insights, "いない人", false), null, "an unknown maid gets no sentence");

  // 一覧は店ごとの画面と同じ割り振りを引く。resolve が返したものをそのまま並べる。
  const day = lastActual;
  const shift = insights.shifts.find((s) => insights.actual[day]?.[s]);
  const name = (schedule.schedule[day]?.[shift] ?? [])[0]?.name;
  assert.ok(name, "the last recorded day must have someone on the rota");
  const resolve = (key, s) => {
    const outlook = outlookFor(key, s);
    const members = (schedule.schedule[key]?.[s] ?? []).map((entry) => entry.name);
    return {
      outlook,
      assignment: getShiftAssignment({
        insights,
        members,
        shift: s,
        outlook,
        pins: eventStorePins({
          insights,
          entries: schedule.schedule[key]?.[s] ?? [],
          homeStore: schedule.homeStore,
          unpostedMaids: new Set(schedule.unpostedMaids ?? [])
        }),
        kitchenStaff: new Set(schedule.kitchenStaff)
      })
    };
  };
  const plan = maidItinerary({
    schedule: schedule.schedule,
    name,
    dates: [day],
    shifts: [shift],
    resolve
  });
  assert.equal(plan.stops.length, 1, "the maid must appear once on that shift");
  assert.equal(plan.stops[0].dateKey, day, "the stop must keep its date");
  // 実績の日は確定。当日決まる件数に数えてはいけない。
  assert.equal(plan.stops[0].state, "open", "a recorded shift is settled, not a guess");
  assert.equal(plan.guesses, 0, "a recorded shift must not count as still open");
  assert.ok(
    [...resolve(day, shift).assignment.byMaid.keys()].includes(name),
    "the assignment the plan read must be the same one the calendar reads"
  );
  assert.equal(
    plan.stops[0].storeId,
    resolve(day, shift).assignment.byMaid.get(name).storeId,
    "the per-maid list must not compute its own placement"
  );

  // 出番のない日は行を作らない。空行は「休み」と読まれてしまう。
  const absent = maidItinerary({
    schedule: schedule.schedule,
    name: "いない人",
    dates: [day],
    shifts: [shift],
    resolve
  });
  assert.equal(absent.stops.length, 0, "a maid who is not on the rota gets no rows");

  // 縦に並べると外れが積み上がる。件数が増えるほど「全部当たる」は下がる。
  const perStop = insights.accuracy.maidStoreGivenOpen;
  assert.ok(perStop > 0 && perStop < 1, "the per-stop hit rate must be a real probability");
  const one = itineraryConfidence(insights, 1);
  const many = itineraryConfidence(insights, 10);
  assert.equal(one.allRight, perStop, "one open day is just the per-stop rate");
  assert.ok(
    many.allRight < one.allRight / 10,
    "ten open days in a row must read as far less certain than one"
  );
  assert.equal(itineraryConfidence(insights, 0), null, "a settled month has nothing to warn about");
  assert.equal(itineraryConfidence({}, 5), null, "without a measured rate, say nothing");
}

console.log(
  "Store outlook valid: records, next-day forecast, weekday tendency, " +
    `${partialDates.length} single-shift days, Sunday-based weekday index, sparse-rotation fallbacks, ` +
    `headcount-driven shop counts (1/2/3), capacity-based assignment, store-by-store grouping, ` +
    `per-maid itineraries banded by spread, ` +
    `and ${eventShifts.length} event shifts pinned to the host's own store.`
);
