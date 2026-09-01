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
  lastActualDateOf,
  openStoresOn,
  storeCapacities,
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

// 開店店舗が分からない日は、そのシフトでいちばん多い開店店舗数ぶんだけ候補にする。
const modalOpenCount = Number(
  Object.entries(insights.openCountPerShift["昼"])
    .filter(([count]) => Number(count) > 0)
    .sort((a, b) => b[1] - a[1])[0][0]
);
assert.equal(
  expectedOpenStores(insights, "昼", futureOutlook).length,
  modalOpenCount,
  "an unknown day must consider the most common number of open stores"
);
assert.ok(
  expectedOpenStores(insights, "昼", futureOutlook).includes("s1"),
  "1号店 is open almost every day, so it must always be a candidate"
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

// チップは割り振りの結果を読む。
const chip = getMaidStoreOutlook({
  insights,
  name: dayPool[0],
  shift: "昼",
  outlook: futureOutlook,
  assignment
});
assert.equal(chip.basis, "assignment", "the chip must reflect the assignment, not a standalone guess");
assert.equal(
  chip.storeId,
  assignment.byMaid.get(dayPool[0]).storeId,
  "the chip must name the store the maid was assigned to"
);
assert.ok(chip.title.includes("割り振"), "the chip must explain that it is an assignment");
assert.ok(chip.title.includes("標準人数"), "the chip must say what the capacities came from");

assert.equal(
  getMaidStoreOutlook({ insights, name: dayPool[0], shift: "昼", outlook: futureOutlook }),
  null,
  "without an assignment there is nothing honest to show"
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
assert.ok(eventShifts.length > 0, "the fixture must contain at least one event shift");

for (const { date, shift, entries } of eventShifts) {
  const pins = eventStorePins({ insights, entries });
  const hosts = entries.filter((entry) => entry.featured);
  assert.equal(pins.size, hosts.length, `${date} ${shift} must pin every featured maid`);

  for (const host of hosts) {
    const pin = pins.get(host.name);
    assert.equal(
      pin.storeId,
      insights.maidTendency[host.name].home,
      `${host.name} must be pinned to her own store`
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
      insights.maidTendency[host.name].home,
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
      hostChip.title.includes("推定"),
      "the chip must admit the home store is inferred from shifts, not an official posting"
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

console.log(
  "Store outlook valid: records, next-day forecast, weekday tendency, " +
    `${partialDates.length} single-shift days, Sunday-based weekday index, sparse-rotation fallbacks, ` +
    `capacity-based assignment, and ${eventShifts.length} event shifts pinned to the host's own store.`
);
