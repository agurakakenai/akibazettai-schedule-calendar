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
  openStoresOnDay,
  sortByAssignedStore,
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
const eventShiftsPreview = eventShifts;
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
      eventStorePins({ insights, entries: eventShift.entries })
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

  // 実データでも分かれること。まこっちゃんとあらたの同店率は実測14%。
  for (const [date, shift] of [["2026-09-01", "夜"], ["2026-09-10", "昼"]]) {
    const entries = schedule.schedule[date][shift];
    const members = entries.map((entry) => entry.name);
    const cooksToday = members.filter((name) => kitchen.has(name));
    if (cooksToday.length < 2) {
      continue;
    }
    const pins = eventStorePins({ insights, entries });
    const outlook = applyEventCertainty(insights, outlookFor(date, shift), pins);
    const assigned = getShiftAssignment({
      insights,
      members,
      shift,
      outlook,
      pins,
      kitchenStaff: kitchen
    });
    if (assigned.storeIds.length < 2) {
      continue;
    }
    assert.equal(
      new Set(cooksToday.map((name) => assigned.byMaid.get(name).storeId)).size,
      cooksToday.length,
      `${date} ${shift}: ${cooksToday.join(" and ")} must each get their own shop`
    );
  }
}

console.log(
  "Store outlook valid: records, next-day forecast, weekday tendency, " +
    `${partialDates.length} single-shift days, Sunday-based weekday index, sparse-rotation fallbacks, ` +
    `headcount-driven shop counts (1/2/3), capacity-based assignment, ` +
    `and ${eventShifts.length} event shifts pinned to the host's own store.`
);
