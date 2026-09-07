"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const api = require("../app.js");
const { test } = require("node:test");
const { emptyHalfMonthSchedules } = require("./fixtures/half-month-schedules.js");
const context = { window: {} };
vm.createContext(context);
for (const file of ["schedule.js", "store-insights.js"]) {
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "data", file), "utf8"), context);
}
const insights = context.window.STORE_INSIGHTS;
const schedule = context.window.SCHEDULE_DATA;
const before = JSON.stringify(insights);
const halfDays = [
  ["2026-09-02", "夜"], ["2026-09-05", "昼"], ["2026-09-07", "昼"],
  ["2026-09-10", "夜"], ["2026-09-12", "昼"], ["2026-09-14", "昼"]
];
const copyOf = (value) => JSON.parse(JSON.stringify(value));
function halfSource(name = "いと", overrides = {}) {
  const createdAt = "2026-09-06T11:57:53Z";
  const id = (((BigInt(Date.parse(createdAt)) - 1288834974657n) << 22n) + 1n).toString();
  const handle = insights.maidTendency[name]?.x ?? "half_fixture";
  return {
    id, url: `https://x.com/${handle}/status/${id}`, name, authorId: "123456789",
    authorScreenName: handle, createdAt, observedAt: "2026-09-06T12:00:00Z",
    sourceKind: "half-month-schedule",
    period: { from: "2026-09-01", to: "2026-09-15", printedYear: null, yearBasis: "post-context" },
    days: halfDays.map(([date, shift]) => ({ date, shifts: [shift] })),
    ...overrides
  };
}
const halfFeed = (sources = [halfSource()]) => ({
  schemaVersion: 1, complete: false, checkedAt: "2026-09-06T12:00:00Z",
  lastSuccessAt: "2026-09-06T12:00:00Z", schedules: sources, lastRun: { status: "ok" }
});

function timingSource(post, sourceKind) {
  return { ...Object.fromEntries(["id", "url", "name", "authorId", "authorScreenName", "createdAt"]
    .map(field => [field, post[field]])), sourceKind };
}

function timingFact(source, changes = {}) {
  return { serviceDate: "2026-09-07", shift: "昼", boundary: "end", status: "set",
    qualifier: "long", explicitTime: null, source, ...changes };
}

test("work timing strictly separates source words, exact clocks and work boundaries", () => {
  const source = timingSource(halfSource(), "half-month-schedule");
  for (const [shift, boundary, explicitTime, qualifier, label] of [
    ["昼", "end", "16:00", null, "短め"], ["昼", "end", "18:00", null, "ながめ"],
    ["夜", "start", "16:00", null, "早め"], ["夜", "start", "18:00", null, "おそめ"],
    ["昼", "end", null, "short", "短め"], ["昼", "end", null, "long", "ながめ"],
    ["夜", "start", null, "early", "早め"], ["夜", "start", null, "late", "おそめ"],
    ["昼", "start", "16:00", null, null], ["夜", "end", "18:00", null, null],
    ["昼", "end", "15:00", null, null], ["昼", "end", "17:00", null, null],
    ["昼", "end", "19:00", null, null], ["夜", "start", "17:00", null, null],
    ["夜", "start", "19:00", null, null]
  ]) {
    const fact = timingFact(source, { shift, boundary, explicitTime, qualifier });
    const channel = { schemaVersion: 1, facts: [fact] };
    assert.equal(api.validateWorkTiming(channel), channel);
    assert.equal(api.workTimingLabel(fact), label);
    if (label) {
      assert.match(api.workTimingDescription(fact), explicitTime === null ? /数値記載なし/ : /予定表に時刻記載/);
    }
  }
  assert.equal(api.workTimingLabel(timingFact(source, { qualifier: "long", explicitTime: "16:00" })), null);
  for (const status of ["withdrawn", "conflict"]) {
    assert.equal(api.workTimingLabel(timingFact(source, { status, qualifier: null })), null);
  }
  const valid = { schemaVersion: 1, facts: [timingFact(source)] };
  for (const mutate of [
    x => { x.raw = "private"; }, x => { x.schemaVersion = "1"; }, x => { x.facts = null; },
    x => { delete x.facts[0].explicitTime; }, x => { x.facts[0].evidenceLineIds = [1]; },
    x => { x.facts[0].serviceDate = "2026-09-31"; }, x => { x.facts[0].shift = "unspecified"; },
    x => { x.facts[0].boundary = "break"; }, x => { x.facts[0].status = "pending"; },
    x => { x.facts[0].qualifier = null; }, x => { x.facts[0].qualifier = "early"; },
    x => { x.facts[0].qualifier = "__proto__"; }, x => { x.facts[0].explicitTime = "24:00"; },
    x => { x.facts[0].source.url = "javascript:alert(1)"; }, x => { x.facts[0].source.body = "private"; },
    x => { x.facts[0].source.id = 12345; }, x => { x.facts[0].source.createdAt = "2026-09-05T00:00:00Z"; },
    x => { x.facts.push(copyOf(x.facts[0])); }
  ]) {
    const bad = copyOf(valid);
    mutate(bad);
    assert.throws(() => api.validateWorkTiming(bad), mutate.toString());
  }
  assert.throws(() => api.validateWorkTiming(valid, { owner: { ...halfSource(), authorId: "888" } }));
  assert.throws(() => api.validateWorkTiming(valid, { date: "2026-09-08" }));
  assert.throws(() => api.validateWorkTiming(valid, { days: { "2026-09-07": ["夜"] } }));
  assert.throws(() => api.validateWorkTiming(valid, { sourceKind: "personal-work-post" }));
});

test("timing is display-only for curated and planned entries without inventing links or attendance", () => {
  const table = halfSource();
  table.workTiming = { schemaVersion: 1, facts: [
    timingFact(timingSource(table, "half-month-schedule")),
    timingFact(timingSource(table, "half-month-schedule"),
      { serviceDate: "2026-09-02", shift: "夜", boundary: "start", qualifier: "early" })
  ] };
  assert.equal(api.validateHalfMonthSchedules(halfFeed([table]), { roster: schedule.roster, insights }).schedules[0], table);
  const plain = api.buildEffectiveSchedule(schedule.schedule, halfFeed());
  const effective = api.buildEffectiveSchedule(schedule.schedule, halfFeed([table]));
  for (const [dateKey, shift, label] of [["2026-09-02", "夜", "早め"], ["2026-09-07", "昼", "ながめ"]]) {
    const options = { insights, observations: null, personal: null, dateKey, shift, roster: schedule.roster };
    const before = api.resolveShiftRoster({ ...options, schedule: plain });
    const after = api.resolveShiftRoster({ ...options, schedule: effective });
    assert.deepEqual(after.entries.map(entry => entry.name), before.entries.map(entry => entry.name));
    assert.equal(api.workTimingLabel(after.entries.find(entry => entry.name === "いと").workTimingNote), label);
    assert.equal(after.observed.byMaid.size, before.observed.byMaid.size);
    assert.equal(after.personal.byMaid.size, before.personal.byMaid.size);
  }
  const before = JSON.stringify([table, effective, insights]);
  const post = { ...table, date: "2026-09-06", events: [], links: [] };
  delete post.period;
  delete post.days;
  delete post.sourceKind;
  post.workTiming = { schemaVersion: 1, facts: [
    timingFact(timingSource(post, "personal-work-post"), { serviceDate: post.date })
  ] };
  const personal = { schemaVersion: 1, complete: false, posts: [post],
    checkedAt: post.observedAt, lastSuccessAt: post.observedAt, lastRun: { status: "ok" } };
  assert.equal(api.validatePersonalShifts(personal), personal);
  const result = api.resolveShiftRoster({ insights: null, observations: null, personal,
    dateKey: post.date, shift: "昼", roster: schedule.roster, schedule: {} });
  assert.deepEqual(result.entries, []);
  assert.equal(api.dayHasPersonStoreEvidence(null, null, post.date, personal), false);
  assert.equal(api.personalPostLink({ personal, insights: null, observations: null, dateKey: post.date, shift: "昼", name: post.name }), null);
  assert.equal(JSON.stringify([table, effective, insights]), before);
});

test("timing precedence retains unmentioned facts and never revives explicitly blocked old labels", () => {
  const table = halfSource("あむ");
  table.workTiming = { schemaVersion: 1, facts: [timingFact(timingSource(table, "half-month-schedule"))] };
  const effective = api.buildEffectiveSchedule({}, halfFeed([table]));
  const makePost = (hour, changes = {}) => {
    const createdAt = `2026-09-07T${String(hour).padStart(2, "0")}:00:00Z`;
    const id = (((BigInt(Date.parse(createdAt)) - 1288834974657n) << 22n) + 1n).toString();
    return { id, url: `https://x.com/${table.authorScreenName}/status/${id}`, name: table.name,
      authorId: table.authorId, authorScreenName: table.authorScreenName, createdAt, observedAt: createdAt,
      date: "2026-09-07", events: [], links: [{ scope: "昼", status: "work" }], ...changes };
  };
  const short = makePost(1);
  short.workTiming = { schemaVersion: 1, facts: [
    timingFact(timingSource(short, "personal-work-post"), { qualifier: null, explicitTime: "16:00" })
  ] };
  const resolve = (posts, changes = {}) => api.resolveWorkTiming({
    personal: { posts }, schedule: effective, insights, dateKey: "2026-09-07", shift: "昼", name: "あむ", ...changes
  });
  const unrelated = makePost(2);
  assert.equal(api.workTimingLabel(resolve([unrelated, short])), "短め");
  assert.equal(api.workTimingLabel(resolve([makePost(3, { workTiming: { schemaVersion: 1, facts: [] } }), short])), "短め");
  const changedTime = makePost(3);
  changedTime.workTiming = { schemaVersion: 1, facts: [
    timingFact(timingSource(changedTime, "personal-work-post"), { qualifier: null, explicitTime: "17:00" })
  ] };
  assert.equal(resolve([changedTime, short]), null);
  for (const status of ["withdrawn", "conflict"]) {
    const cancelled = makePost(3);
    cancelled.workTiming = { schemaVersion: 1, facts: [
      timingFact(timingSource(cancelled, "personal-work-post"), { status, qualifier: null })
    ] };
    assert.equal(resolve([cancelled, short, makePost(4)]), null);
  }
  assert.equal(resolve([short, makePost(3, { events: [{ shift: "昼", kind: "absence", excerpt: "お休み" }] }),
    makePost(4, { events: [{ shift: "昼", kind: "return", excerpt: "復帰" }] })]), null);
  assert.equal(resolve([short, makePost(3, { links: [{ scope: "昼", status: "withdrawn" }] })]), null);
  const future = makePost(4, { date: "2026-09-08" });
  future.workTiming = { schemaVersion: 1, facts: [
    timingFact(timingSource(future, "personal-work-post"), { serviceDate: future.date, status: "withdrawn", qualifier: null })
  ] };
  assert.equal(api.workTimingLabel(resolve([short, future])), "短め");
  assert.equal(api.workTimingLabel(resolve([makePost(4, { events: [{ shift: "昼", kind: "late", time: "16:00", excerpt: "遅れ" }] })])), "ながめ");
  assert.equal(resolve([short], { shift: "夜" }), null);
});

test("value-specific timing denials do not cancel unrelated labels or revive older labels", () => {
  const table = halfSource("あむ", { days: [{ date: "2026-09-07", shifts: ["夜"] }] });
  const base = timingFact(timingSource(table, "half-month-schedule"),
    { shift: "夜", boundary: "start", qualifier: "late" });
  const earlyDenial = timingFact(base.source,
    { shift: "夜", boundary: "start", status: "excluded", qualifier: "early" });
  const timeDenial = { ...earlyDenial, qualifier: null, explicitTime: "18:00" };
  table.workTiming = { schemaVersion: 1, facts: [base, earlyDenial, timeDenial] };
  const feed = halfFeed([table]);
  assert.equal(api.validateHalfMonthSchedules(feed), feed);
  const resolve = (facts, posts = []) => {
    const schedule = api.buildEffectiveSchedule({}, halfFeed([{ ...table, workTiming: { schemaVersion: 1, facts } }]));
    return api.resolveWorkTiming({ personal: { posts }, schedule, insights, dateKey: "2026-09-07", shift: "夜", name: "あむ" });
  };
  assert.equal(api.workTimingLabel(resolve([base, earlyDenial])), "おそめ");
  assert.equal(resolve([base, timeDenial]), null, "a denied clock matches the label's house meaning without inventing a source clock");
  assert.equal(resolve([base, timeDenial, earlyDenial]), null);
  assert.equal(resolve([{ ...base, qualifier: null, explicitTime: "18:00" },
    { ...earlyDenial, qualifier: "late" }]), null);
  const post = (hour, changes) => {
    const createdAt = `2026-09-07T0${hour}:00:00Z`;
    const id = (((BigInt(Date.parse(createdAt)) - 1288834974657n) << 22n) + 1n).toString();
    const value = { id, url: `https://x.com/${table.authorScreenName}/status/${id}`,
      name: table.name, authorId: table.authorId, authorScreenName: table.authorScreenName,
      date: "2026-09-07", createdAt, observedAt: createdAt, events: [], links: [] };
    value.workTiming = { schemaVersion: 1, facts: [
      timingFact(timingSource(value, "personal-work-post"),
        { shift: "夜", boundary: "start", status: "excluded", qualifier: "early", ...changes })
    ] };
    return value;
  };
  const notEarly = post(1, {});
  const notLate = post(2, { qualifier: "late" });
  const unrelatedLater = post(3, {});
  assert.equal(api.workTimingLabel(resolve([base], [notEarly])), "おそめ");
  assert.equal(resolve([base], [notLate, notEarly, unrelatedLater]), null);
  assert.equal(api.workTimingLabel(resolve([base], [notLate, notEarly, unrelatedLater,
    post(4, { status: "set", qualifier: "early" })])), "早め");
  for (const facts of [
    [{ ...earlyDenial, explicitTime: "16:00" }],
    [{ ...earlyDenial, qualifier: null, explicitTime: null }],
    [earlyDenial, copyOf(earlyDenial)]
  ]) {
    assert.throws(() => api.validateWorkTiming({ schemaVersion: 1, facts }));
  }
});

test("half-month feed strictly validates the public-only contract and calendar", () => {
  const snapshot = halfFeed();
  assert.equal(api.validateHalfMonthSchedules(snapshot, { roster: schedule.roster, insights }), snapshot);
  const empty = emptyHalfMonthSchedules();
  assert.equal(api.validateHalfMonthSchedules(empty), empty);
  assert.deepEqual(empty, { schemaVersion: 1, complete: false, checkedAt: null, lastSuccessAt: null,
    schedules: [], lastRun: { status: "never" } }, "the public unit fixture starts never/empty");
  const anotherEmpty = emptyHalfMonthSchedules();
  assert.notEqual(empty, anotherEmpty);
  assert.notEqual(empty.schedules, anotherEmpty.schedules);
  assert.notEqual(empty.lastRun, anotherEmpty.lastRun);
  for (const status of ["never", "ok", "partial", "unavailable", "no-new", "no-results",
    "paused", "budget-exhausted", "outside-window"]) {
    assert.ok(api.validateHalfMonthSchedules({ ...snapshot, lastRun: { status } }));
  }
  const invalid = [
    x => { x.extra = {}; }, x => { delete x.complete; }, x => { x.complete = true; },
    x => { x.schemaVersion = "1"; }, x => { x.lastRun.raw = "not public"; },
    x => { x.lastRun.status = "success"; }, x => { x.schedules = {}; },
    x => { x.checkedAt = "2026-09-06T21:00:00+09:00"; },
    x => { x.checkedAt = "2026-02-30T12:00:00Z"; },
    x => { x.lastSuccessAt = "2026-09-07T00:00:00Z"; },
    x => { x.schedules[0].id = Number(x.schedules[0].id); },
    x => { x.schedules[0].id = `0${x.schedules[0].id}`; },
    x => { x.schedules[0].id = "9".repeat(26); },
    x => { x.schedules[0].authorId = "0"; },
    x => { x.schedules[0].authorId = 123456789; },
    x => { x.schedules[0].authorScreenName = "someone/else"; },
    x => { x.schedules[0].url += "?tracking=1"; },
    x => { x.schedules[0].url = "javascript:alert(1)"; },
    x => { x.schedules[0].url = x.schedules[0].url.replace("https:", "http:"); },
    x => { x.schedules[0].name = "unknown"; }, x => { x.schedules[0].name += "\n"; },
    x => { x.schedules[0].name = null; },
    x => { x.schedules[0].createdAt = "2026-09-06T11:58:00Z"; },
    x => { x.schedules[0].createdAt = "2026-09-06T11:57:53"; },
    x => { x.schedules[0].observedAt = "2026-09-06T11:00:00Z"; },
    x => { x.schedules[0].sourceKind = "personal"; },
    x => { x.schedules[0].raw = "private"; },
    x => { x.schedules[0].imageUrl = "https://pbs.twimg.com/media/fixture.jpg"; },
    x => { x.schedules[0].analysisCache = {}; },
    x => { x.schedules[0].excerpt = "invented"; },
    x => { x.schedules[0].period.from = "2026-09-02"; },
    x => { x.schedules[0].period.to = "2026-09-14"; },
    x => { x.schedules[0].period.to = "2026-09-31"; },
    x => { x.schedules[0].period.from = "2026-02-30"; },
    x => { x.schedules[0].period.printedYear = 2026; },
    x => { x.schedules[0].period.yearBasis = "inferred"; },
    x => { x.schedules[0].period = { from: "2026-07-01", to: "2026-07-15", printedYear: null, yearBasis: "post-context" }; },
    x => { x.schedules[0].period.yearBasis = "printed"; },
    x => { x.schedules[0].period.lineId = "fabricated"; },
    x => { x.schedules[0].days = null; },
    x => { x.schedules[0].days = []; },
    x => { x.schedules[0].days[0].date = "2026-09-16"; },
    x => { x.schedules[0].days[0].date = "2026-09-01T00:00:00Z"; },
    x => { x.schedules[0].days.push(x.schedules[0].days[0]); },
    x => { x.schedules[0].days[0].shifts = []; },
    x => { x.schedules[0].days[0].shifts = ["昼", "昼"]; },
    x => { x.schedules[0].days[0].shifts = ["昼", "夜", "昼"]; },
    x => { x.schedules[0].days[0].shifts = ["長め昼"]; },
    x => { x.schedules[0].days[0].shifts = "昼"; },
    x => { x.schedules[0].days[0].storeId = "s1"; },
    x => { x.schedules.push(copyOf(x.schedules[0])); }
  ];
  for (const mutate of invalid) {
    const bad = copyOf(snapshot);
    mutate(bad);
    assert.throws(() => api.validateHalfMonthSchedules(bad, { roster: schedule.roster, insights }), mutate.toString());
  }
  const printed = copyOf(snapshot);
  printed.schedules[0].period = { from: "2024-02-16", to: "2024-02-29", printedYear: 2024, yearBasis: "printed" };
  printed.schedules[0].days = [{ date: "2024-02-29", shifts: ["昼", "夜"] }];
  assert.ok(api.validateHalfMonthSchedules(printed));
  printed.schedules[0].period.printedYear = 2023;
  assert.throws(() => api.validateHalfMonthSchedules(printed));
  printed.schedules[0].period = { from: "2023-02-16", to: "2023-02-29", printedYear: 2023, yearBasis: "printed" };
  assert.throws(() => api.validateHalfMonthSchedules(printed), "non-leap dates never roll forward");
  assert.deepEqual(api.halfMonthPeriod("2026-12-31"), { from: "2026-12-16", to: "2026-12-31" });
  assert.deepEqual(api.halfMonthPeriod("2024-02-29"), { from: "2024-02-16", to: "2024-02-29" });
  assert.equal(api.halfMonthPeriod("2023-02-29"), null);
  assert.equal(api.halfMonthPeriod("0000-01-01"), null);
  const fullMonth = copyOf(snapshot);
  fullMonth.schedules.push(halfSource("いと", {
    period: { from: "2026-09-16", to: "2026-09-30", printedYear: null, yearBasis: "text" },
    days: [{ date: "2026-09-30", shifts: ["昼", "夜"] }]
  }));
  assert.ok(api.validateHalfMonthSchedules(fullMonth), "one original post can cover two disjoint half-months");
  const separatelyObserved = copyOf(fullMonth);
  separatelyObserved.checkedAt = separatelyObserved.schedules[1].observedAt = "2026-09-07T00:00:00Z";
  assert.ok(api.validateHalfMonthSchedules(separatelyObserved),
    "separately verified halves retain their own observation times, not a new post identity");
  fullMonth.schedules[1].authorId = "987654321";
  assert.throws(() => api.validateHalfMonthSchedules(fullMonth), "split periods cannot rebind the author");
  assert.throws(() => api.validateHalfMonthSchedules(snapshot, {
    previous: halfFeed([halfSource("いと", { authorId: "987654321" })])
  }));
  assert.throws(() => api.validateHalfMonthSchedules(snapshot, {
    personal: { posts: [halfSource("いと", { authorId: "987654321" })] }
  }));
  assert.throws(() => api.validateHalfMonthSchedules(snapshot, {
    insights: { maidTendency: { "いと": { x: "different" } } }
  }));
  for (const [createdAt, from, to] of [
    ["2026-12-31T15:05:00Z", "2026-12-16", "2026-12-31"],
    ["2026-12-31T14:55:00Z", "2027-01-01", "2027-01-15"],
    ["2026-09-15T03:00:00Z", "2026-09-16", "2026-09-30"]
  ]) {
    const id = ((BigInt(Date.parse(createdAt)) - 1288834974657n) << 22n).toString();
    const source = halfSource("いと", { createdAt, observedAt: createdAt, id,
      url: `https://x.com/${halfSource().authorScreenName}/status/${id}`,
      period: { from, to, printedYear: null, yearBasis: "post-context" },
      days: [{ date: from, shifts: ["昼"] }] });
    assert.ok(api.validateHalfMonthSchedules({ ...halfFeed([source]), checkedAt: createdAt, lastSuccessAt: createdAt }));
  }
});

test("effective plans are an immutable manual/half union, never event or attendance evidence", () => {
  const snapshot = halfFeed();
  const manual = { "2026-09-07": {
    "昼": [{ name: "いと", featured: true, eventLabel: "手動の記念日" }, { name: "あむ" }],
    "夜": [{ name: "いと" }]
  }};
  const beforeInputs = JSON.stringify([manual, snapshot, schedule, insights]);
  const effective = api.buildEffectiveSchedule(manual, snapshot);
  assert.ok(Object.isFrozen(effective));
  assert.ok(Object.isFrozen(effective["2026-09-07"]["昼"]));
  const ito = effective["2026-09-07"]["昼"].find(entry => entry.name === "いと");
  assert.equal(effective["2026-09-07"]["昼"].filter(entry => entry.name === "いと").length, 1);
  assert.equal(ito.featured, true);
  assert.equal(ito.eventLabel, "手動の記念日");
  assert.equal(ito.halfMonthSources[0].sourceKind, "half-month-schedule");
  assert.ok(Object.isFrozen(ito.halfMonthSources[0].period));
  assert.throws(() => { ito.name = "changed"; });
  assert.equal(effective["2026-09-07"]["夜"][0].name, "いと", "opposite manual shift is never deleted");
  assert.equal(effective["2026-09-07"]["夜"][0].halfMonthSources, undefined, "no counterpart provenance");
  assert.equal(effective["2026-09-10"]["夜"][0].featured, undefined);
  assert.deepEqual(api.dayEvents({ ...schedule, schedule: manual }, insights, "2026-09-07")
    .map(event => event.name), ["いと"], "only manual event metadata is used");
  const pureHalf = api.buildEffectiveSchedule({}, snapshot);
  assert.deepEqual(Object.entries(pureHalf).flatMap(([date, day]) => Object.keys(day).map(shift => [date, shift])), halfDays);
  assert.equal(api.dayHasPersonStoreEvidence(insights, null, "2026-09-07"), false);
  assert.equal(api.personalPostLink({ personal: { posts: [] }, insights, observations: null,
    dateKey: "2026-09-07", shift: "昼", name: "いと" }), null, "a half source never becomes a same-day link");
  const replaced = halfFeed([halfSource("いと", { days: [{ date: "2026-09-08", shifts: ["昼"] }] })]);
  const changed = api.buildEffectiveSchedule(manual, replaced);
  assert.equal(changed["2026-09-10"], undefined, "producer's current winner replaces only automatic facts");
  assert.equal(changed["2026-09-07"]["夜"][0].name, "いと");
  assert.equal(JSON.stringify([manual, snapshot, schedule, insights]), beforeInputs);
});

test("half union preserves curated, official, personal absence/return and kitchen semantics", () => {
  const publicObserved = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "data", "observed-shifts.json"), "utf8"));
  const publicPersonal = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "data", "personal-shifts.json"), "utf8"));
  const inputBefore = JSON.stringify([schedule, insights, publicObserved, publicPersonal]);
  const effective = api.buildEffectiveSchedule(schedule.schedule, halfFeed());
  const resolve = (dateKey, shift, planned = effective, personal = publicPersonal) => api.resolveShiftRoster({
    insights, observations: publicObserved, personal, dateKey, shift, schedule: planned,
    roster: schedule.roster, nameCorrections: schedule.observationNameCorrections,
    personalEventAdditions: schedule.personalEventAdditions
  });
  for (const [date, shift, store] of [["2026-09-02", "昼", "s2"], ["2026-09-02", "夜", "s1"], ["2026-09-05", "昼", "s1"]]) {
    const old = resolve(date, shift, schedule.schedule);
    const current = resolve(date, shift);
    assert.deepEqual(current.entries.map(({ halfMonthSources, ...entry }) => entry), old.entries,
      `${date} ${shift}: automatic union does not change the existing roster/count`);
    const entry = current.entries.find(entry => entry.name === "いと");
    assert.ok(entry, `${date} ${shift} retains Ito from its own evidence`);
    assert.equal(current.assignment?.byMaid.get("いと")?.storeId ?? current.observed.byMaid.get("いと")?.storeIds[0], store);
    assert.equal(entry.halfMonthSources?.length ?? 0, date === "2026-09-02" && shift === "昼" ? 0 : 1);
  }
  for (const [date, shift] of halfDays) {
    assert.equal(effective[date][shift].filter(entry => entry.name === "いと").length, 1);
    const result = resolve(date, shift);
    assert.equal(result.entries.filter(entry => entry.name === "いと").length, 1);
  }
  const key = "2026-09-07";
  const post = { id: "2097000000000000001", url: "https://x.com/half_fixture/status/2097000000000000001",
    name: "いと", authorId: "123456789", authorScreenName: "half_fixture", date: key,
    createdAt: `${key}T02:00:00Z`, observedAt: `${key}T03:00:00Z`,
    events: [{ kind: "absence", shift: "昼", excerpt: "synthetic cancellation" }] };
  const cancelled = { posts: [post] };
  assert.equal(resolve(key, "昼", effective, cancelled).entries.some(entry => entry.name === "いと"), false);
  const returnPost = { ...post, id: "2097000000000000002", createdAt: `${key}T04:00:00Z`,
    events: [{ kind: "return", shift: "昼", excerpt: "synthetic return" }] };
  const returned = resolve(key, "昼", effective, { posts: [post, returnPost] });
  assert.equal(returned.entries.filter(entry => entry.name === "いと").length, 1);
  assert.equal(returned.entries.find(entry => entry.name === "いと").halfMonthSources.length, 1);
  const kitchens = schedule.kitchenStaff;
  assert.equal(kitchens.length, 5);
  const allCooks = halfFeed(kitchens.map((name, index) => {
    const original = halfSource(name);
    const id = (BigInt(original.id) + BigInt(index)).toString();
    return { ...original, id, authorId: String(100 + index),
      url: `https://x.com/${original.authorScreenName}/status/${id}`,
      days: [{ date: key, shifts: ["昼"] }] };
  }));
  api.validateHalfMonthSchedules(allCooks, { roster: schedule.roster, insights });
  const cookPlans = api.buildEffectiveSchedule({}, allCooks);
  const outlook = api.getStoreOutlook({ insights, dateKey: key, shift: "昼" });
  const cookOutlook = api.applyHomeStaff(insights, outlook, cookPlans[key]["昼"].map(entry => entry.name),
    schedule.homeStore, kitchens);
  assert.deepEqual(cookOutlook.entries, api.applyHomeStaff(insights, outlook, [], schedule.homeStore, kitchens).entries,
  "all five kitchen members remain excluded from floor headcounts");
  assert.match(cookOutlook.summary, /キッチンにゃんこ5人.*この数に入れていません/);
  assert.equal(JSON.stringify([schedule, insights, publicObserved, publicPersonal]), inputBefore);
});
const makePost = (id, storeId, names) => ({
  id, url: `https://x.com/akibazettai/status/${id}`,
  authorId: "822429861218131969", authorScreenName: "akibazettai",
  createdAt: "2026-09-05T03:00:00Z", observedAt: "2026-09-05T05:00:00Z",
  date: "2026-09-05", shift: "昼", storeId, names
});
const fixture = {
  schemaVersion: 1, complete: false, checkedAt: "2026-09-05T05:00:00Z",
  lastSuccessAt: "2026-09-05T05:00:00Z", pending: [],
  lastRun: { status: "partial" },
  posts: [
    makePost("2096000000000000001", "s2", ["あむ", "まこと", "もな"]),
    makePost("2096000000000000002", "s2", ["あむ"]),
    makePost("2096000000000000003", "s4", ["まこと"])
  ]
};
assert.equal(api.validateObservations(fixture), fixture);
for (const mutate of [
  x => { x.complete = true; },
  x => { x.posts[0].authorId = "other"; },
  x => { x.posts[0].authorScreenName = "other"; },
  x => { x.posts[0].id = 2096000000000000001; },
  x => { x.posts[0].url = "https://x.com/other/status/2096000000000000001"; },
  x => { x.posts.push(x.posts[0]); },
  x => { x.posts[0].createdAt = "2026-09-05T12:00:00"; },
  x => { x.posts[0].date = "2026-02-30"; },
  x => { x.posts[0].names = []; },
  x => { x.lastRun.status = "unknown-success"; }
]) {
  const copy = JSON.parse(JSON.stringify(fixture));
  mutate(copy);
  assert.throws(() => api.validateObservations(copy));
}
const observed = api.observedShift(fixture, insights, "2026-09-05", "昼");
const sparseInsights = { ...insights, maidTendency: { ...insights.maidTendency, "未記録の在籍者": null } };
assert.deepEqual(api.observedShift(fixture, sparseInsights, "2026-09-05", "昼"), observed,
  "nullable tendencies must not break observed names or approved aliases");
assert.deepEqual([...observed.byStore.keys()], ["s2", "s4"]);
assert.equal(observed.byStore.has("s1"), false, "no assertion that an unobserved shop is closed");
assert.deepEqual([...observed.byMaid.keys()], ["あむ", "まこっちゃん", "もな"]);
assert.deepEqual(observed.byMaid.get("まこっちゃん").storeIds, ["s2", "s4"], "preserve multi-store observations");
assert.equal(observed.byStore.get("s2").get("あむ").length, 2, "duplicate appearances keep both sources, one person");
assert.equal(observed.byMaid.has("もなか"), false, "do not invent an identity correction");
assert.equal(api.observedShift(fixture, insights, "2026-09-05", "夜").posts.length, 0);
assert.equal(api.observedShift(fixture, insights, "2026-09-06", "昼").posts.length, 0);
{
  const first = makePost("2096000000000000010", "s1", ["まこと", "あむ"]);
  const second = {
    ...makePost("2096000000000000011", "s2", ["あむ"]),
    notices: [{ name: "みりあ", kind: "late", excerpt: "あとから" }],
    editTweetIds: [first.id, "2096000000000000011"]
  };
  const latest = { ...makePost("2096000000000000012", "s4", ["みりあ"]),
    editTweetIds: [first.id, second.id, "2096000000000000012"] };
  const snapshot = { ...fixture, posts: [first, second] };
  const raw = JSON.stringify(snapshot);
  assert.equal(api.validateObservations(snapshot), snapshot);
  assert.deepEqual(api.activeObservationPosts(snapshot).map(post => post.id), [second.id]);
  const active = api.observedShift(snapshot, insights, first.date, first.shift);
  assert.deepEqual([...active.byMaid.keys()], ["あむ"]);
  assert.deepEqual(active.byMaid.get("あむ").storeIds, ["s2"]);
  assert.equal(active.notices.get("みりあ").storeId, "s2");
  assert.deepEqual(active.posts.map(post => post.id), [second.id]);
  assert.equal(JSON.stringify(snapshot), raw, "edit filtering leaves all raw posts and history intact");
  assert.deepEqual(api.activeObservationPosts({ posts: [latest, first, second] }).map(post => post.id), [latest.id]);
  const partialLatest = { ...latest, editTweetIds: [second.id, latest.id] };
  const partialChain = { ...fixture, posts: [first, second, partialLatest] };
  const partialRaw = JSON.stringify(partialChain);
  assert.equal(api.validateObservations(partialChain), partialChain);
  assert.deepEqual(api.activeObservationPosts(partialChain).map(post => post.id), [latest.id],
    "adding B-to-C must preserve the earlier verified A-to-B replacement");
  const partialRoster = api.observedShift(partialChain, insights, first.date, first.shift);
  assert.deepEqual([...partialRoster.byMaid.keys()], ["みりあ"], "old people must not reappear");
  assert.equal(partialRoster.notices.size, 0, "the replaced late notice must not reappear");
  assert.deepEqual(partialRoster.posts.map(post => post.id), [latest.id]);
  assert.equal(JSON.stringify(partialChain), partialRaw);
  assert.deepEqual(api.activeObservationPosts({ posts: [first, partialLatest] }).map(post => post.id), [first.id, latest.id],
    "a missing intermediary cannot supply an unverified A-to-B relationship");
  assert.deepEqual(api.activeObservationPosts({ posts: [first, second, { ...partialLatest, date: "2026-09-06" }] })
    .map(post => post.id), [second.id, latest.id], "a different-day successor cannot remove B or revive A");
  assert.deepEqual(api.activeObservationPosts({ posts: [first, latest] }).map(post => post.id), [latest.id],
    "a fetched terminal version can replace an ancestor even when intermediate IDs were not fetched");
  assert.deepEqual(api.activeObservationPosts({ ...snapshot, pending: [{ id: latest.id }] }).map(post => post.id), [second.id],
    "an unfetched latest ID does not remove the last available version");
  const changedHeader = { ...second, shift: "夜" };
  assert.equal(api.observedShift({ posts: [first, changedHeader] }, insights, first.date, first.shift).posts.length, 0);
  assert.deepEqual(api.observedShift({ posts: [first, changedHeader] }, insights, first.date, "夜")
    .posts.map(post => post.id), [second.id], "same-day edits apply before filtering shift");
  const otherDay = { ...second, date: "2026-09-06" };
  assert.deepEqual(api.activeObservationPosts({ posts: [first, otherDay] }).map(post => post.id), [first.id, second.id],
    "a different service day cannot supersede the stored source's facts");
  assert.equal(api.dayHasPersonStoreEvidence(insights, { posts: [first, changedHeader] }, first.date), true,
    "the retained history gate does not collapse back into guessed stores");
  for (const chain of [null, {}, second.id, [], [second.id, second.id], [Number(second.id)],
    ["123", second.id], ["0123456789", second.id], ["１２３４５６７８９０", second.id],
    ["1".repeat(26), second.id], [first.id, "2096000000000000009", second.id],
    [first.id], [second.id, latest.id]]) {
    assert.throws(() => api.validateObservations({ ...fixture, posts: [{ ...second, editTweetIds: chain }] }));
  }
  const stale = { ...first, editTweetIds: [first.id, latest.id] };
  assert.deepEqual(api.activeObservationPosts({ posts: [stale] }).map(post => post.id), [first.id]);
  const foreign = { ...second, authorId: "123", authorScreenName: "other" };
  assert.throws(() => api.validateObservations({ ...fixture, posts: [first, foreign] }));
  assert.ok(api.activeObservationPosts({ posts: [first, foreign] }).includes(first),
    "unverified third-party metadata cannot suppress official facts");
  assert.ok(api.activeObservationPosts({ posts: [first, second, { ...partialLatest, authorId: "123" }] }).includes(second),
    "an unverified later author cannot suppress the last verified version");
  const quoted = { ...first, quoted_status: { edit_control: { edit_tweet_ids: [first.id, second.id] } } };
  assert.deepEqual(api.activeObservationPosts({ posts: [quoted] }).map(post => post.id), [first.id]);
  const cyclic = [
    { ...first, editTweetIds: [second.id, first.id] },
    { ...second, editTweetIds: [first.id, second.id] }
  ];
  assert.throws(() => api.validateObservations({ ...fixture, posts: cyclic }),
    "reverse intermediate IDs are invalid even when each chain ends at its current ID");
  assert.deepEqual(api.activeObservationPosts({ posts: [cyclic[0]] }).map(post => post.id), [first.id],
    "an invalid descending claim cannot erase the only available facts");
  const twentyOneIds = Array.from({ length: 21 }, (_, index) => (2096800000000000000n + BigInt(index)).toString());
  const bounded = { ...makePost(twentyOneIds[19], "s1", ["あむ"]), editTweetIds: twentyOneIds.slice(0, 20) };
  assert.ok(api.validateObservations({ ...fixture, posts: [bounded] }));
  const oversized = { ...makePost(twentyOneIds[20], "s1", ["あむ"]), editTweetIds: twentyOneIds };
  assert.throws(() => api.validateObservations({ ...fixture, posts: [oversized] }));
}
const traineeMetadata = { traineePeriods: { byName: { "見習い例": { from: "2026-08-01", to: "2026-10-29" } } } };
assert.equal(api.observedTrainee(traineeMetadata, "見習い例", "2026-09-05"), true);
assert.equal(api.observedTrainee(traineeMetadata, "見習い例", "2026-07-31"), false);
assert.equal(api.observedTrainee(traineeMetadata, "見習い例", "2026-10-30"), false);
assert.equal(api.observedTrainee(traineeMetadata, "未掲載の別人", "2026-09-05"), null);
assert.equal(api.observedTrainee({}, "見習い例", "2026-09-05"), null);
traineeMetadata.unlistedMaids = { "未掲載の非見習い": { status: "active" } };
assert.equal(api.observedTrainee(traineeMetadata, "未掲載の非見習い", "2026-09-05"), null);
const promotionBoundary = { traineePeriods: { byName: { "昇格例": { from: "2026-08-01", to: "2026-09-04" } } } };
assert.equal(api.observedTrainee(promotionBoundary, "昇格例", "2026-09-04"), true);
assert.equal(api.observedTrainee(promotionBoundary, "昇格例", "2026-09-05"), false);
assert.notEqual(api.observedTrainee(insights, "つぽみ", "2026-09-05"), true, "an unknown spelling is not evidence of trainee status");

const planned = [{ name: "あむ" }, { name: "こい" }];
const merged = api.observationEntries(planned, observed, schedule.roster);
assert.equal(merged.length, 4);
assert.equal(merged.find(x => x.name === "こい").observed, undefined, "a planned person remains unmatched");
assert.equal(merged.find(x => x.name === "あむ").observed, true);
assert.deepEqual(planned, [{ name: "あむ" }, { name: "こい" }], "do not mutate public plans");
const baseOutlook = api.getStoreOutlook({ insights, dateKey: "2026-09-05", shift: "昼" });
const assignment = api.getShiftAssignment({ insights, members: ["あむ", "こい"], shift: "昼", outlook: baseOutlook });
for (const [name, recorded, stores] of [
  ["あむ", true, ["s2"]], ["こい", false, []], ["まこっちゃん", true, ["s2", "s4"]], ["もな", true, ["s2"]]
]) {
  const result = api.maidItinerary({
    schedule: { "2026-09-05": { "昼": planned } }, name, dates: ["2026-09-05"], shifts: ["昼"],
    resolve: () => ({ outlook: baseOutlook, assignment, observed }), kitchenStaff: schedule.kitchenStaff
  });
  assert.equal(result.stops.length, 1);
  assert.equal(result.stops[0].recorded, recorded, name);
  assert.deepEqual(result.stops[0].storeIds, stores);
  if (recorded) {
    assert.equal(result.stops[0].openRate, null);
    assert.ok(result.stops[0].sourcePosts.length > 0);
    assert.equal(result.stops[0].kitchen, false, "observed kitchen staff have a documented shop");
  } else assert.equal(result.guesses, 1);
}
assert.ok(!assignment.recorded, "partial evidence never upgrades the entire assignment");
const curated = JSON.parse(JSON.stringify(insights));
curated.actualRoster["2026-09-05"] = { "昼": { stores: { s1: ["あむ"] }, trainees: [] } };
assert.equal(api.observedShift(fixture, curated, "2026-09-05", "昼").posts.length, 0, "manual roster wins");
assert.equal(JSON.stringify(insights), before, "observations must not train or mutate historical tables");
assert.deepEqual(api.getStoreOutlook({ insights, dateKey: "2026-09-05", shift: "昼" }), baseOutlook);

const storesOnly = {
  stores: insights.stores,
  actual: { "2026-09-05": { "昼": ["s1"] } },
  actualWithoutRoster: { "2026-09-05": { "昼": ["s1"] } }
};
assert.equal(api.dayHasPersonStoreEvidence(storesOnly, null, "2026-09-05"), false);
assert.equal(api.dayHasPersonStoreEvidence(storesOnly, { ...fixture, posts: [] }, "2026-09-05"), false);
assert.equal(api.dayHasPersonStoreEvidence(insights, fixture, "2026-09-05"), true);
assert.equal(api.dayHasPersonStoreEvidence(insights, fixture, "2026-09-06"), false);
for (const shift of ["昼", "夜"]) {
  const onePerson = { ...fixture, posts: [{ ...fixture.posts[0], shift, names: ["あむ"] }] };
  assert.equal(api.dayHasPersonStoreEvidence(storesOnly, onePerson, "2026-09-05"), true,
    "one person on either shift switches the whole day");
  const recordedPerson = {
    ...storesOnly, actualRoster: { "2026-09-05": { [shift]: { stores: { s2: ["あむ"] } } } }
  };
  assert.equal(api.dayHasPersonStoreEvidence(recordedPerson, null, "2026-09-05"), true);
}
assert.equal(api.dayHasPersonStoreEvidence({
  ...storesOnly, actualRoster: { "2026-09-05": { "昼": { stores: { s1: [] } } } }
}, null, "2026-09-05"), false, "an empty roster is not person/store evidence");
const pendingChange = { ...fixture, pending: [{ id: "2096000000000099999" }] };
assert.equal(api.dayHasPersonStoreEvidence(storesOnly, pendingChange, "2026-09-05"), true,
  "unresolved change candidates do not erase retained person/store evidence");
assert.equal(api.dayHasPersonStoreEvidence(storesOnly, { ...pendingChange, posts: [] }, "2026-09-05"), false,
  "a pending candidate alone cannot confirm a placement");

const orderingInput = [
  { name: "あらた", trainee: true }, { name: "わたげ", trainee: true },
  { name: "もち" }, { name: "あむ" }, { name: "あるか", trainee: true },
  { name: "ゆめ" }, { name: "未分類" }, { name: "ちゆ" }, { name: "ぴあの" }, { name: "にゃな" }
];
const orderingBefore = JSON.stringify(orderingInput);
const ordered = api.orderRosterEntries(orderingInput,
  ["ちゆ", "あむ", "にゃな", "ぴあの", "ゆめ", "もち", "あらた"], ["あらた"],
  { "ゆめ": "にゃな", "もち": "ぴあの" });
assert.deepEqual(ordered.map(entry => entry.name),
  ["ちゆ", "あむ", "ゆめ", "にゃな", "もち", "ぴあの", "あるか", "わたげ", "未分類", "あらた"]);
assert.equal(ordered.find(entry => entry.name === "もち").trainee, undefined);
assert.equal(ordered.find(entry => entry.name === "未分類").trainee, undefined);
assert.equal(JSON.stringify(orderingInput), orderingBefore, "display ordering does not alter source order or roles");
assert.deepEqual(api.orderRosterEntries([{ name: "新しい方" }, { name: "あむ" }],
  ["あむ", "新しい方"], [], { "新しい方": "既に卒業した方" }).map(entry => entry.name),
  ["あむ", "新しい方"], "missing debut anchors retain the saved order");
assert.deepEqual(api.orderRosterEntries(
  ["いと", "ぴあの", "ゆめ", "ちぇる", "もち", "にゃな"].map(name => ({ name })),
  schedule.roster, schedule.kitchenStaff, schedule.normalOrderBefore).map(entry => entry.name),
  ["ゆめ", "ちぇる", "いと", "にゃな", "もち", "ぴあの"], "confirmed unpublished normals join the first-service rank");

const correctionPost = makePost("2096074325120237794", "s1", ["つぽみ"]);
const correctionFixture = { ...fixture, posts: [correctionPost] };
const correctionRules = schedule.observationNameCorrections;
assert.equal(correctionRules["2096074325120237794"]["つぽみ"].name, "つぼみ");
const corrected = api.observedShift(correctionFixture, insights, "2026-09-05", "昼", correctionRules);
assert.deepEqual([...corrected.byStore.get("s1").keys()], ["つぼみ"]);
const reconciled = api.observationEntries([{ name: "つぼみ" }], corrected, schedule.roster);
assert.equal(reconciled.length, 1);
assert.equal(reconciled[0].observed, true, "the confirmed person must not remain in unmatched plans");
assert.deepEqual(correctionPost.names, ["つぽみ"], "keep the original observed spelling");
assert.equal(corrected.byMaid.get("つぼみ").sources[0].url, correctionPost.url);
const correctedPlan = api.maidItinerary({
  schedule: { "2026-09-05": { "昼": [{ name: "つぼみ" }] } }, name: "つぼみ",
  dates: ["2026-09-05"], shifts: ["昼"],
  resolve: () => ({ outlook: baseOutlook, assignment: null, observed: corrected })
});
assert.equal(correctedPlan.stops[0].recorded, true);
assert.equal(correctedPlan.stops[0].storeId, "s1");
assert.equal(correctedPlan.stops[0].sourcePosts[0].id, correctionPost.id);
assert.equal(correctedPlan.stops[0].nameCorrections[0].rawName, "つぽみ");
assert.match(correctedPlan.stops[0].nameCorrections[0].reason, /利用者確認/);
const otherPost = { ...correctionPost, id: "2096074325120237795", url: "https://x.com/akibazettai/status/2096074325120237795" };
const untouched = api.observedShift({ ...fixture, posts: [otherPost] }, insights, "2026-09-05", "昼", correctionRules);
assert.deepEqual([...untouched.byMaid.keys()], ["つぽみ"], "the same spelling on a different post must stay unchanged");
const otherDay = { ...otherPost, date: "2026-09-06" };
assert.deepEqual([...api.observedShift({ ...fixture, posts: [otherDay] }, insights, otherDay.date, "昼", correctionRules).byMaid.keys()],
  ["つぽみ"], "another date/post is not corrected");
const otherNames = { ...correctionPost, names: ["みずれ", "みひん", "つぼみ", "constructor", "toString"] };
assert.deepEqual([...api.observedShift({ ...fixture, posts: [otherNames] }, insights, "2026-09-05", "昼", correctionRules).byMaid.keys()],
  ["みずれ", "みひん", "つぼみ", "constructor", "toString"], "only explicitly owned raw-name keys may be corrected");
console.log("Observed shifts valid: partial coverage, source identity, per-person evidence, aliases, and curated precedence.");

const personalPost = (id, name, events, time = "2026-09-06T00:00:02+09:00", author = "amu_zettai") => ({
  id, name, url: `https://x.com/${author}/status/${id}`,
  authorId: "1180156105181159424", authorScreenName: author,
  createdAt: time, observedAt: "2026-09-06T13:00:00+09:00", date: "2026-09-06", events
});
const announcement = (kind, storeId, extra = {}) => ({
  shift: "夜", kind, excerpt: `${kind}の明示案内`, ...(storeId ? { storeId } : {}), ...extra
});
const personalFixture = {
  schemaVersion: 1, complete: false, checkedAt: "2026-09-06T13:00:00+09:00",
  lastSuccessAt: "2026-09-06T13:00:00+09:00", lastRun: { status: "ok" },
  posts: [
    personalPost("2096252018260062487", "あむ", [
      announcement("placement", "s1", { shift: "昼" }), announcement("placement", "s2")
    ]),
    { ...personalPost("2096253883677044837", "ららこ", [
      announcement("placement", "s1", { shift: "昼", excerpt: "1号店➡️2号店 12-22。2→1だと勘違いしていた" })
    ], "2026-09-06T00:07:27+09:00", "rarako_zettai"), authorId: "2065375500131028992" }
  ]
};
assert.equal(api.validatePersonalShifts(personalFixture), personalFixture);
for (const mutate of [
  x => { x.complete = true; }, x => { delete x.posts; }, x => { x.checkedAt = "today"; },
  x => { x.posts[0].id = 2096252018260062487; }, x => { x.posts.push(x.posts[0]); },
  x => { x.posts[0].url = "javascript:alert(1)"; }, x => { x.posts[0].authorId = "amu"; },
  x => { x.posts[0].authorScreenName = "other"; }, x => { x.posts[0].name = ""; },
  x => { x.posts[0].date = "2026-02-30"; }, x => { x.posts[0].createdAt = "2026-09-06T00:00:02"; },
  x => { x.posts[0].events = []; }, x => { x.posts[0].events[0].shift = "終日"; },
  x => { x.posts[0].events[0].kind = "closed"; }, x => { delete x.posts[0].events[0].storeId; },
  x => { x.posts[0].events[0].kind = "absence"; },
  x => { x.posts[0].events[0].storeId = "s5"; }, x => { x.posts[0].events[0].time = "25:00"; },
  x => { x.posts[0].events[0].excerpt = ""; },
  x => { x.posts[0].events[0].excerpt = " "; },
  x => { x.posts[0].events[0].excerpt = "昼\n夜"; },
  x => { x.posts[0].events[0].excerpt = "x".repeat(161); }
]) {
  const copy = JSON.parse(JSON.stringify(personalFixture));
  mutate(copy);
  assert.throws(() => api.validatePersonalShifts(copy));
}
for (const status of ["paused", "budget-exhausted", "outside-window", "unavailable", "partial"]) {
  assert.ok(api.validatePersonalShifts({ ...personalFixture, lastRun: { status } }));
}
const dailyPlans = { "2026-09-06": {
  "昼": [{ name: "あむ" }, { name: "ららこ" }, { name: "みりあ" }],
  "夜": [{ name: "あむ" }, { name: "ららこ" }, { name: "ちゆ" }]
} };
const officialToday = { ...fixture, posts: [{
  ...makePost("2096400000000000001", "s1", ["あむ", "ららこ"]),
  date: "2026-09-06", createdAt: "2026-09-06T03:00:00Z"
}] };
const options = { insights, observations: officialToday, personal: personalFixture,
  dateKey: "2026-09-06", schedule: dailyPlans, roster: schedule.roster };
const unchanged = JSON.stringify([dailyPlans, personalFixture, officialToday, insights]);
{
  const first = { ...officialToday.posts[0], shift: "夜", names: ["あむ"] };
  const latest = { ...first, id: "2096400000000000002",
    url: "https://x.com/akibazettai/status/2096400000000000002", storeId: "s2",
    editTweetIds: [first.id, "2096400000000000002"] };
  const result = api.resolveShiftRoster({ ...options, shift: "夜", observations: { ...officialToday, posts: [first, latest] } });
  const amu = result.entries.find(entry => entry.name === "あむ");
  assert.equal(amu.observed, true);
  assert.equal(amu.personalNotice.conflict, false, "the edited-out store is not a current conflict");
  assert.equal(amu.personalNotice.placementSource.url, personalFixture.posts[0].url);
}
const lunch = api.resolveShiftRoster({ ...options, shift: "昼" });
const night = api.resolveShiftRoster({ ...options, shift: "夜" });
assert.equal(lunch.entries.filter(entry => entry.name === "あむ").length, 1);
assert.equal(lunch.entries.find(entry => entry.name === "あむ").observed, true);
assert.equal(lunch.entries.find(entry => entry.name === "あむ").personalPlacement, false);
assert.equal(night.entries.find(entry => entry.name === "あむ").personalNotice.storeId, "s2");
assert.equal(night.entries.find(entry => entry.name === "あむ").personalPlacement, true);
assert.equal(night.entries.find(entry => entry.name === "ららこ").personalNotice, undefined,
  "UI does not infer an evening store from arrows, 12-22, or quoted misunderstandings");
assert.equal(api.dayHasPersonStoreEvidence(insights, null, options.dateKey, personalFixture), true);
const planOf = (name, resolved) => api.maidItinerary({
  name, dates: [options.dateKey], shifts: ["夜"], schedule: dailyPlans,
  resolve: () => ({ ...resolved, confirmedOnly: true })
});
const nightPlan = planOf("あむ", night);
assert.equal(nightPlan.stops[0].recorded, false);
assert.equal(nightPlan.stops[0].observed, false);
assert.equal(nightPlan.stops[0].personal, true);
assert.equal(nightPlan.stops[0].state, "announced");
assert.equal(nightPlan.stops[0].openRate, null);
assert.equal(nightPlan.guesses, 0, "an announcement is neither an actual nor a statistical guess");
assert.equal(planOf("ららこ", night).stops[0].storeId, null);

const additions = {
  "2096253883677044837": {
    name: "ららこ", authorId: "2065375500131028992", authorScreenName: "rarako_zettai", date: "2026-09-06",
    events: [{ shift: "夜", kind: "placement", storeId: "s2", excerpt: "1号店➡️2号店 / お昼1号店" }],
    reason: "User-reviewed placement"
  }
};
const scoped = (personal = personalFixture, personalEventAdditions = additions) =>
  api.resolveShiftRoster({ ...options, personal, personalEventAdditions, shift: "夜" });
const amended = scoped();
assert.equal(amended.entries.find(entry => entry.name === "ららこ").personalNotice.storeId, "s2");
assert.equal(planOf("ららこ", amended).stops[0].storeId, "s2");
assert.equal(planOf("ららこ", amended).stops[0].recorded, false);
assert.equal(api.personalShift(personalFixture, insights, options.dateKey, "夜").byMaid.has("ららこ"), false);
assert.equal(api.personalShift(personalFixture, insights, options.dateKey, "夜", null, additions).byMaid.get("ららこ").storeId, "s2");
for (const field of ["name", "authorId", "authorScreenName", "date"]) {
  const wrong = structuredClone(additions);
  wrong["2096253883677044837"][field] += "wrong";
  assert.equal(scoped(personalFixture, wrong).personal.byMaid.has("ららこ"), false, field);
}
assert.equal(scoped(personalFixture, Object.create(additions)).personal.byMaid.has("ららこ"), false);
const anotherId = structuredClone(personalFixture);
anotherId.posts[1].id = "2096253883677044838";
anotherId.posts[1].url = "https://x.com/rarako_zettai/status/2096253883677044838";
assert.equal(scoped(anotherId).personal.byMaid.has("ららこ"), false);
const alreadyScoped = structuredClone(personalFixture);
alreadyScoped.posts[1].events.push(announcement("uncertain"));
assert.equal(scoped(alreadyScoped).personal.byMaid.get("ららこ").storeId, null,
  "an existing event in that shift prevents an approved addition");
const rarakoChange = (kind, storeId) => ({
  ...personalFixture.posts[1], id: "2096500000000000099",
  url: "https://x.com/rarako_zettai/status/2096500000000000099",
  createdAt: "2026-09-06T14:00:00+09:00", events: [announcement(kind, storeId)]
});
assert.ok(!scoped({ ...personalFixture, posts: [...personalFixture.posts, rarakoChange("absence")] })
  .entries.some(entry => entry.name === "ららこ"));
assert.equal(scoped({ ...personalFixture, posts: [...personalFixture.posts, rarakoChange("placement", "s4")] })
  .personal.byMaid.get("ららこ").storeId, "s4");
const gateFixture = { ...personalFixture, posts: [{
  ...personalFixture.posts[1], events: [announcement("uncertain", undefined, { shift: "昼" })]
}] };
assert.equal(api.dayHasPersonStoreEvidence(insights, null, options.dateKey, gateFixture), false);
assert.equal(api.dayHasPersonStoreEvidence(insights, null, options.dateKey, gateFixture, additions), true);
assert.equal(JSON.stringify([dailyPlans, personalFixture, officialToday, insights]), unchanged,
  "approved view-only additions never modify the raw snapshot, source, schedule or insights");

const lateOfficial = {
  ...officialToday.posts[0], id: "2096436633973526890",
  url: "https://x.com/akibazettai/status/2096436633973526890",
  storeId: "s4", names: ["るるか", "ちぇる", "まこと"],
  notices: [{ name: "みりあ", kind: "late", excerpt: "みりあちゃんもあとから来るにゃんね" }],
  lastCheckedAt: "2026-09-06T04:15:00.123456Z",
  revisions: [{
    date: options.dateKey, shift: "昼", storeId: "s4", names: ["るるか"],
    notices: [], observedAt: "2026-09-06T03:15:00Z"
  }]
};
const officialNotices = { ...officialToday, posts: [lateOfficial] };
const officialBefore = JSON.stringify(officialNotices);
assert.equal(api.validateObservations(officialNotices), officialNotices);
const resolveOfficialLate = (posts) => api.resolveShiftRoster({
  ...options, observations: { ...officialNotices, posts }, personal: null, shift: "昼"
});
const officialLate = resolveOfficialLate([lateOfficial]);
const miria = officialLate.entries.find(entry => entry.name === "みりあ");
assert.equal(miria.officialPlacement, true);
assert.equal(miria.officialNotice.storeId, "s4");
assert.equal(miria.officialNotice.time, null);
assert.equal(miria.observed, undefined);
assert.equal(officialLate.observed.byMaid.has("みりあ"), false);
assert.ok(officialLate.observed.byMaid.has("まこっちゃん"));
const miriaPlan = api.maidItinerary({ name: "みりあ", dates: [options.dateKey], shifts: ["昼"],
  schedule: dailyPlans, resolve: () => ({ ...officialLate, confirmedOnly: true }) });
assert.equal(miriaPlan.stops[0].recorded, false);
assert.equal(miriaPlan.stops[0].personal, false);
assert.equal(miriaPlan.stops[0].state, "announced");
assert.equal(miriaPlan.stops[0].openRate, null);
assert.equal(miriaPlan.stops[0].sourcePosts[0], lateOfficial);
assert.equal(miriaPlan.guesses, 0);
const samePostLate = { ...lateOfficial, names: [...lateOfficial.names, "みりあ"] };
const specificallyLate = resolveOfficialLate([samePostLate]);
assert.equal(specificallyLate.entries.find(entry => entry.name === "みりあ").officialPlacement, true);
assert.equal(specificallyLate.observed.byMaid.has("みりあ"), false,
  "an explicit late notice is not presence even when the same post also lists the name");
assert.equal(samePostLate.names.length, 4, "the original roster is not rewritten");
const arrival = { ...lateOfficial, id: "2096500000000000088", createdAt: "2026-09-06T05:00:00Z",
  names: ["みりあ"], notices: [] };
const arrived = resolveOfficialLate([arrival, lateOfficial]);
assert.ok(arrived.observed.byMaid.has("みりあ"));
assert.equal(arrived.entries.find(entry => entry.name === "みりあ").officialNotice, undefined);
assert.ok(api.validateObservations({ ...officialNotices, posts: [{ ...lateOfficial, names: [] }] }));
assert.equal(api.dayHasPersonStoreEvidence(insights, { posts: [{ ...lateOfficial, names: [] }] }, options.dateKey), true);
for (const mutate of [
  x => { x.notices[0].kind = "placement"; },
  x => { x.notices[0].excerpt = ""; },
  x => { x.notices[0].excerpt = "x\nx"; },
  x => { x.notices[0].excerpt = "x\u2028x"; },
  x => { x.notices[0].excerpt = "x".repeat(161); },
  x => { x.notices[0].time = "25:00"; },
  x => { x.notices[0].time = null; },
  x => { x.notices[0].observedAt = null; },
  x => { x.notices[0].observedAt = "2026-09-05T03:00:00Z"; },
  x => { x.lastCheckedAt = "now"; },
  x => { x.revisions[0].observedAt = null; }
]) {
  const post = structuredClone(lateOfficial);
  mutate(post);
  assert.throws(() => api.validateObservations({ ...officialNotices, posts: [post] }));
}
assert.equal(JSON.stringify(officialNotices), officialBefore, "official raw names/notices/history remain unchanged");
const correctedLate = { ...makePost("2096074325120237794", "s1", []),
  notices: [{ name: "つぽみ", kind: "late", excerpt: "あとから" }] };
assert.equal(api.observedShift({ posts: [correctedLate] }, insights, "2026-09-05", "昼",
  schedule.observationNameCorrections).notices.has("つぼみ"), true);
assert.equal(api.observedShift({ posts: [{ ...correctedLate, id: "2096074325120237795" }] }, insights,
  "2026-09-05", "昼", schedule.observationNameCorrections).notices.has("つぽみ"), true);

{
  const update = (number, kind, storeId, extra = {}) => personalPost(
    `209670000000000000${number}`, "あむ", [announcement(kind, storeId, extra)], "2026-09-06T14:00:00+09:00");
  const sourceOf = (...posts) => api.resolveShiftRoster({
    ...options, observations: null, shift: "夜",
    personal: { ...personalFixture, posts: [personalFixture.posts[0], ...posts] }
  }).personal.byMaid.get("あむ").placementSource;
  const moved = update(1, "placement", "s4");
  const lateWithoutStore = update(2, "late", undefined, { time: "18:30" });
  const lateWithStore = update(3, "late", "s2", { time: "19:00" });
  const uncertain = update(4, "uncertain");
  const absent = update(5, "absence");
  const returnWithoutStore = update(6, "return");
  const returnedToStore = update(7, "return", "s1");
  assert.equal(sourceOf(), personalFixture.posts[0]);
  assert.equal(sourceOf(moved), moved, "a later correction owns the current store's source");
  assert.equal(sourceOf(lateWithoutStore, moved), moved, "storeless late does not replace placement evidence");
  assert.equal(sourceOf(uncertain, moved), moved, "uncertain text does not replace placement evidence");
  assert.equal(sourceOf(lateWithStore, moved), lateWithStore, "explicit-store late is placement evidence");
  assert.equal(sourceOf(moved, absent), null);
  assert.equal(sourceOf(absent, returnedToStore), returnedToStore);
  assert.equal(sourceOf(absent, returnWithoutStore), null, "storeless return must not resurrect an old source");
  assert.equal(sourceOf(absent, update(6, "late", "s2")), null, "late cannot revive a cancelled placement");
  assert.equal(sourceOf(absent, update(6, "placement", "s2")), null, "placement alone cannot revive cancellation");
  assert.equal(lunch.entries.find(entry => entry.name === "あむ").personalNotice.placementSource.url,
    personalFixture.posts[0].url, "matching official evidence keeps the supporting personal post");
  assert.equal(amended.personal.byMaid.get("ららこ").placementSource.url, personalFixture.posts[1].url,
    "the scoped evening addition links to the reviewed original post");
  const conflict = api.resolveShiftRoster({
    ...options, shift: "夜", observations: { ...officialToday, posts: [{ ...officialToday.posts[0], shift: "夜", storeId: "s4" }] }
  });
  assert.equal(conflict.personal.byMaid.get("あむ").placementSource, null);
  assert.equal(JSON.stringify([dailyPlans, personalFixture, officialToday, insights]), unchanged);
}

const withEvents = (...posts) => ({ ...personalFixture, posts: [personalFixture.posts[0], ...posts] });
const cancellation = personalPost("2096500000000000001", "あむ", [announcement("absence")], "2026-09-06T13:00:00+09:00");
const restored = personalPost("2096500000000000002", "あむ", [announcement("return")], "2026-09-06T14:00:00+09:00");
const officialNight = { ...officialToday, posts: [{ ...officialToday.posts[0], shift: "夜", names: ["あむ"], storeId: "s2" }] };
const resolveNight = (personal, observations = officialNight, customInsights = insights) =>
  api.resolveShiftRoster({ ...options, insights: customInsights, personal, observations, shift: "夜" });
const cancelled = resolveNight(withEvents(cancellation));
assert.ok(!cancelled.entries.some(entry => entry.name === "あむ"));
assert.equal(cancelled.observed.byMaid.has("あむ"), false);
assert.equal(cancelled.personal.byMaid.get("あむ").history.length, 2);
assert.equal(planOf("あむ", cancelled).stops.length, 0);
assert.equal(planOf("あむ", cancelled).changes.length, 1, "cancellation source history stays discoverable");
assert.equal(api.dayHasPersonStoreEvidence(insights, null, options.dateKey, withEvents(cancellation)), true,
  "cancellation cannot turn the day gate back into store guessing");
const returned = resolveNight(withEvents(restored, cancellation));
assert.equal(returned.entries.find(entry => entry.name === "あむ").personalPlacement, false);
assert.equal(planOf("あむ", returned).stops[0].storeId, null, "return without a store must not restore an earlier shop");
const returnedStore = resolveNight(withEvents(cancellation, {
  ...restored, events: [announcement("return", "s4")]
}));
assert.equal(planOf("あむ", returnedStore).stops[0].storeId, "s4");
assert.equal(planOf("あむ", returnedStore).stops[0].recorded, false);

const late = personalPost("2096500000000000003", "あむ", [announcement("late")], "2026-09-06T15:00:00+09:00");
const lateOnly = resolveNight(withEvents(late));
assert.equal(lateOnly.entries.find(entry => entry.name === "あむ").observed, true, "late does not erase a collected person");
assert.equal(lateOnly.personal.byMaid.get("あむ").late.time, null);
assert.equal(resolveNight(withEvents(cancellation, late)).entries.some(entry => entry.name === "あむ"), false,
  "late is not an explicit return after a cancellation");
assert.equal(resolveNight(withEvents(cancellation, {
  ...late, events: [announcement("placement", "s4")]
})).entries.some(entry => entry.name === "あむ"), false, "a new placement alone is not an explicit return");
const timedLate = resolveNight(withEvents({ ...late, events: [announcement("late", null, { time: "18:30" })] }));
assert.equal(timedLate.personal.byMaid.get("あむ").late.time, "18:30");
const explicitStoreLate = {
  ...personalFixture, posts: [{ ...late,
    events: [announcement("late", "s2", { time: "18:30", excerpt: "夜は2号店、18:30からです" })]
  }]
};
const noOfficialPeople = { ...fixture, posts: [] };
assert.equal(api.validatePersonalShifts(explicitStoreLate), explicitStoreLate);
assert.equal(api.dayHasPersonStoreEvidence(insights, noOfficialPeople, options.dateKey, explicitStoreLate), true,
  "a late notice with an explicit shop is retained day-level placement evidence");
const placedLate = resolveNight(explicitStoreLate, noOfficialPeople);
assert.equal(placedLate.entries.find(entry => entry.name === "あむ").personalPlacement, true);
assert.equal(placedLate.personal.byMaid.get("あむ").storeId, "s2");
assert.equal(placedLate.personal.byMaid.get("あむ").late.time, "18:30");
assert.equal(planOf("あむ", placedLate).stops[0].storeId, "s2");
assert.equal(planOf("あむ", placedLate).stops[0].recorded, false);
assert.equal(planOf("あむ", placedLate).stops[0].openRate, null);
const cancelledStoreLate = { ...explicitStoreLate, posts: [cancellation, ...explicitStoreLate.posts] };
const stillCancelled = resolveNight(cancelledStoreLate, noOfficialPeople);
assert.equal(stillCancelled.entries.some(entry => entry.name === "あむ"), false,
  "a late notice with a shop must not revive a cancelled person");
assert.equal(stillCancelled.personal.byMaid.get("あむ").storeId, null);
assert.equal(stillCancelled.personal.byMaid.get("あむ").absent, true);
assert.equal(api.dayHasPersonStoreEvidence(insights, noOfficialPeople, options.dateKey, cancelledStoreLate), true,
  "the day gate reads the retained explicit shop even when its person is cancelled");
const noStoreLate = { ...personalFixture, posts: [late] };
assert.equal(api.dayHasPersonStoreEvidence(insights, noOfficialPeople, options.dateKey, noStoreLate), false,
  "a late notice without a shop is not day-level placement evidence");
assert.equal(planOf("あむ", resolveNight(noStoreLate, noOfficialPeople)).stops[0].storeId, null,
  "without prior placement or an explicit shop, a late notice cannot invent one");
const uncertain = personalPost("2096500000000000004", "あむ", [announcement("uncertain")], "2026-09-06T16:00:00+09:00");
assert.equal(resolveNight(withEvents(uncertain)).entries.find(entry => entry.name === "あむ").observed, true,
  "uncertain text does not cancel an earlier explicit placement");
const pendingOnly = resolveNight({ ...personalFixture, posts: [uncertain] }, { ...fixture, posts: [] });
assert.equal(planOf("あむ", pendingOnly).stops[0].storeId, null);
assert.equal(planOf("あむ", pendingOnly).stops[0].recorded, false);
assert.equal(api.dayHasPersonStoreEvidence(insights, null, options.dateKey, { ...personalFixture, posts: [uncertain] }), false);

const contradiction = resolveNight(personalFixture, {
  ...officialNight, posts: [{ ...officialNight.posts[0], storeId: "s4" }]
});
assert.equal(contradiction.personal.byMaid.get("あむ").conflict, false);
assert.equal(contradiction.personal.byMaid.get("あむ").superseded, true);
assert.equal(contradiction.observed.byMaid.has("あむ"), true);
assert.equal(contradiction.entries.filter(entry => entry.name === "あむ").length, 1);
assert.equal(planOf("あむ", contradiction).stops[0].storeId, "s4");
assert.equal(planOf("あむ", contradiction).stops[0].recorded, true);
assert.equal(contradiction.observed.posts.length, 1, "conflicting original collection source remains intact");
{
  const correction = personalPost("2096500000000000077", "あむ",
    [announcement("placement", "s4")], "2026-09-06T14:00:00+09:00");
  const updated = resolveNight(withEvents(correction));
  assert.equal(updated.personal.byMaid.get("あむ").conflict, false);
  assert.equal(updated.observed.byMaid.has("あむ"), false, "a new placement is guidance, not a new observation");
  assert.equal(planOf("あむ", updated).stops[0].storeId, "s4");
  assert.equal(planOf("あむ", updated).stops[0].observed, false);
  assert.equal(updated.entries.filter(entry => entry.name === "あむ").length, 1);
  assert.equal(updated.personal.byMaid.get("あむ").placementSource, correction);
  assert.equal(api.resolveShiftRoster({ ...options, personal: withEvents(correction), shift: "昼" })
    .observed.byMaid.get("あむ").storeIds[0], "s1", "night-only correction never clears lunch");
  const miriaMove = personalPost("2096500000000000078", "みりあ",
    [announcement("placement", "s2", { shift: "昼" })], "2026-09-06T14:00:00+09:00");
  const movedNotice = api.resolveShiftRoster({
    ...options, observations: officialNotices, personal: { ...personalFixture, posts: [miriaMove] }, shift: "昼"
  });
  assert.equal(movedNotice.entries.find(entry => entry.name === "みりあ").officialNotice, undefined);
  assert.equal(movedNotice.entries.find(entry => entry.name === "みりあ").personalNotice.storeId, "s2");
  const observedLater = { ...lateOfficial,
    notices: [{ ...lateOfficial.notices[0], observedAt: "2026-09-06T05:54:14.290Z" }] };
  assert.equal(api.validateObservations({ ...officialNotices, posts: [observedLater] }).posts[0].observedAt,
    lateOfficial.observedAt, "notice acquisition does not rewrite roster observation");
  const timedOfficial = { ...lateOfficial, createdAt: "2026-09-06T04:00:00Z",
    notices: [{ ...lateOfficial.notices[0], time: "15:00" }] };
  const timedPersonal = personalPost("2096400000000000098", "みりあ",
    [announcement("late", "s4", { shift: "昼", time: "14:30" })], "2026-09-06T03:00:00Z");
  const sameStore = api.resolveShiftRoster({
    ...options, observations: { ...officialNotices, posts: [timedOfficial] },
    personal: { ...personalFixture, posts: [timedPersonal] }, shift: "昼"
  });
  assert.equal(sameStore.entries.find(entry => entry.name === "みりあ").officialNotice.time, "15:00");
  assert.equal(sameStore.personal.byMaid.get("みりあ").late, null,
    "a later official time supersedes an old personal time even when the shop is unchanged");
  const laterStoreless = personalPost("2096500000000000098", "みりあ",
    [announcement("late", null, { shift: "昼", time: "16:00" })], "2026-09-06T05:00:00Z");
  const laterTime = api.resolveShiftRoster({
    ...options, observations: { ...officialNotices, posts: [timedOfficial] },
    personal: { ...personalFixture, posts: [timedPersonal, laterStoreless] }, shift: "昼"
  });
  const latestNotice = laterTime.entries.find(entry => entry.name === "みりあ").officialNotice;
  assert.equal(latestNotice.storeId, "s4", "storeless time correction preserves explicit earlier shop evidence");
  assert.equal(latestNotice.time, "16:00");
  assert.equal(latestNotice.arrivalSource, laterStoreless);
  assert.equal(timedPersonal.events[0].time, "14:30");
  assert.equal(timedOfficial.notices[0].time, "15:00", "source history retains both original announcements");
  const unclearAfter = personalPost("2096500000000000099", "みりあ",
    [announcement("uncertain", null, { shift: "昼" })], "2026-09-06T05:30:00Z");
  const retainedTime = api.resolveShiftRoster({
    ...options, observations: { ...officialNotices, posts: [timedOfficial] },
    personal: { ...personalFixture, posts: [timedPersonal, unclearAfter] }, shift: "昼"
  });
  assert.equal(retainedTime.personal.byMaid.get("みりあ").late, null,
    "later uncertainty cannot resurrect a superseded personal arrival time");
  assert.equal(retainedTime.entries.find(entry => entry.name === "みりあ").officialNotice.time, "15:00");
  assert.equal(retainedTime.entries.find(entry => entry.name === "みりあ").officialNotice.uncertain, true);
  const definiteAfter = { ...laterStoreless, id: "2096500000000000100",
    url: "https://x.com/amu_zettai/status/2096500000000000100", createdAt: "2026-09-06T05:45:00Z" };
  const resolvedAgain = api.resolveShiftRoster({
    ...options, observations: { ...officialNotices, posts: [timedOfficial] },
    personal: { ...personalFixture, posts: [timedPersonal, unclearAfter, definiteAfter] }, shift: "昼"
  });
  assert.equal(resolvedAgain.entries.find(entry => entry.name === "みりあ").officialNotice.uncertain, false);
  assert.equal(resolvedAgain.entries.find(entry => entry.name === "みりあ").officialNotice.time, "16:00");
  const placedAfter = { ...definiteAfter, events: [announcement("placement", "s4", { shift: "昼" })] };
  for (const observations of [{ ...officialNotices, posts: [timedOfficial] }, null]) {
    const placed = api.resolveShiftRoster({
      ...options, observations, personal: { ...personalFixture, posts: [timedPersonal, placedAfter] }, shift: "昼"
    });
    assert.equal(placed.personal.byMaid.get("みりあ").late, null,
      "a later definite placement replaces old lateness instead of reviving its time");
    assert.equal(placed.personal.byMaid.get("みりあ").storeId, "s4");
    assert.equal(placed.personal.byMaid.get("みりあ").history[0].event.time, "14:30");
  }
}
const laterCollection = resolveNight(withEvents(cancellation), {
  ...officialNight, posts: [{ ...officialNight.posts[0], createdAt: "2026-09-06T15:00:00+09:00" }]
});
assert.equal(laterCollection.personal.byMaid.get("あむ").conflict, true,
  "a newer collection post is not an implicit return");
const multiple = resolveNight(withEvents({
  ...personalFixture.posts[0], id: "2096252018260062488",
  url: "https://x.com/amu_zettai/status/2096252018260062488"
}));
assert.equal(multiple.personal.byMaid.get("あむ").sources.length, 2);
assert.equal(multiple.entries.filter(entry => entry.name === "あむ").length, 1);
const aliasPersonal = { ...personalFixture, posts: [personalPost("2096500000000000005", "まこと", [announcement("placement", "s4")])] };
assert.ok(resolveNight(aliasPersonal).entries.some(entry => entry.name === "まこっちゃん"));
assert.equal(aliasPersonal.posts[0].name, "まこと", "display aliases do not alter source identity");
const pastRoster = { ...insights, actualRoster: { ...insights.actualRoster, [options.dateKey]: {
  "夜": { stores: { s1: ["あむ"] }, trainees: [] }
} } };
const past = resolveNight(withEvents(cancellation), officialNight, pastRoster);
assert.deepEqual(past.entries.map(entry => entry.name), ["あむ"]);
assert.equal(past.personal.posts.length, 0, "curated roster wins and excluded past planned people are not revived");
assert.equal(past.assignment.recorded, true);
assert.equal(JSON.stringify([dailyPlans, personalFixture, officialToday, insights]), unchanged,
  "personal resolution must not mutate schedules, official posts, curated data or personal history");
console.log("Personal announcements valid: real examples, schema, history, conflicts, cancellation/return, late, pending, aliases and non-actual evidence.");
const personalFile = path.join(__dirname, "..", "data", "personal-shifts.json");
if (fs.existsSync(personalFile)) {
  api.validatePersonalShifts(JSON.parse(fs.readFileSync(personalFile, "utf8")));
  console.log("Published personal snapshot conforms to the shared schema.");
}

const linkDay = "2026-09-06";
const linkInsights = { ...insights, actualRoster: {}, maidTendency: {
  ...insights.maidTendency, "あお": { x: "ao_example" }, "しろ": { x: "shiro_example" },
  "すみ": { x: "sumi_example" }
} };
const linkPost = (number, name, links, events = [], hour = 9) => {
  const author = linkInsights.maidTendency[name]?.x ?? `${name === "みらい" ? "mirai" : "other"}_example`;
  const id = `${2096900000000000000n + BigInt(number)}`;
  return { id, name, authorId: "123456789", authorScreenName: author,
    url: `https://x.com/${author}/status/${id}`, date: linkDay,
    createdAt: `${linkDay}T${String(hour).padStart(2, "0")}:00:00+09:00`,
    observedAt: "2026-09-07T02:00:00+09:00", events,
    ...(links !== undefined ? { links } : {}) };
};
const work = (scope = "unspecified") => [{ scope, status: "work" }];
const linkSnapshot = (posts) => ({ ...personalFixture, posts });
const findLink = (posts, name = "あお", shift = "昼", extra = {}) => api.personalPostLink({
  personal: linkSnapshot(posts), insights: linkInsights, dateKey: linkDay, name, shift, ...extra
});

test("personal links validate the optional bounded contract without changing legacy events", () => {
  const valid = linkPost(1, "あお", work());
  assert.equal(api.validatePersonalShifts(linkSnapshot([valid])).posts[0], valid);
  for (const links of [[], null, {}, "work", [null], [{ scope: "昼" }],
    [{ scope: "昼", status: "work", url: valid.url }],
    [{ scope: "unknown", status: "work" }], [{ scope: "昼", status: "pending" }],
    [{ scope: "unspecified", status: "withdrawn" }], [{ scope: "unspecified", status: "conflict" }],
    [...work("昼"), ...work("昼")], [...work("昼"), ...work("夜"), ...work(), ...work("昼")]]) {
    assert.throws(() => api.validatePersonalShifts(linkSnapshot([{ ...valid, links }])), JSON.stringify(links));
  }
  const authoritativeEmpty = { ...valid, links: [], events: [announcement("placement", "s1", { shift: "昼" })] };
  assert.equal(api.validatePersonalShifts(linkSnapshot([authoritativeEmpty])).posts[0], authoritativeEmpty);
  assert.equal(findLink([authoritativeEmpty]), null, "present empty links never derive work from events");
  assert.equal(findLink([{ ...valid, links: undefined }]), null, "present undefined links are not legacy");
  const legacy = linkPost(2, "あお", undefined, [announcement("placement", "s1", { shift: "昼" })]);
  assert.equal(findLink([legacy]), legacy);
  assert.equal(findLink([legacy], "あお", "夜"), null, "legacy never infers unspecified scope");
  for (const kind of ["late", "return"]) {
    const post = linkPost(3, "しろ", undefined, [announcement(kind, null, { shift: "夜" })]);
    assert.equal(findLink([post], "しろ", "夜"), post);
    assert.equal(findLink([post], "しろ", "昼"), null);
  }
  assert.equal(findLink([linkPost(4, "あお", undefined, [announcement("uncertain", null, { shift: "昼" })])]), null);
});

test("links alone cannot create people, stores, attendance or the confirmed-only gate", () => {
  const posts = ["あお", "しろ", "すみ"].map((name, index) => linkPost(index + 10, name, work()));
  const personal = linkSnapshot(posts);
  const official = { ...fixture, posts: ["昼", "夜"].map((shift, index) => ({
    ...makePost(`209690000000000002${index}`, "s2", ["あお"]),
    date: linkDay, shift, createdAt: `${linkDay}T12:00:00+09:00`,
    notices: [{ name: "しろ", kind: "late", excerpt: "あとから" }]
  })) };
  const base = { insights: linkInsights, dateKey: linkDay, schedule: {}, roster: [], personal };
  const raw = JSON.stringify([personal, official, linkInsights]);
  for (const shift of ["昼", "夜"]) {
    const empty = api.resolveShiftRoster({ ...base, shift });
    assert.equal(empty.entries.length, 0);
    assert.equal(empty.personal.posts.length, 0);
    assert.equal(empty.personal.byMaid.size, 0);
    const displayed = api.resolveShiftRoster({ ...base, shift, observations: official });
    assert.deepEqual(displayed.entries.map(entry => entry.name).sort(), ["あお", "しろ"]);
    assert.deepEqual(displayed, api.resolveShiftRoster({ ...base, shift, observations: official, personal: null }),
      "adding link-only posts cannot alter independent roster evidence");
    for (const name of ["あお", "しろ"]) {
      assert.equal(findLink(posts, name, shift, { observations: official }), posts.find(post => post.name === name));
      const plan = api.maidItinerary({ name, dates: [linkDay], shifts: [shift], schedule: {},
        resolve: () => ({ ...displayed, confirmedOnly: true }) });
      assert.equal(plan.stops.length, 1, "official names and notices include people absent from the schedule");
      assert.ok(plan.stops[0].sourcePosts.every(post => post.authorScreenName === "akibazettai"));
      assert.equal(plan.stops[0].personal, false);
    }
  }
  assert.equal(api.dayHasPersonStoreEvidence(linkInsights, null, linkDay, personal), false);
  const scheduled = { ...base, shift: "夜", schedule: { [linkDay]: { "夜": [{ name: "すみ" }] } } };
  assert.deepEqual(api.resolveShiftRoster(scheduled),
    api.resolveShiftRoster({ ...scheduled, personal: null }), "a link does not turn a scheduled person into confirmed attendance");
  assert.equal(JSON.stringify([personal, official, linkInsights]), raw);
});

test("scope-specific withdrawals and conflicts retain the other shift and original chronology", () => {
  for (const name of ["あお", "しろ", "すみ"]) {
    const first = linkPost(30, name, work(), [], 8);
    const night = linkPost(31, name, work("夜"), [], 10);
    const withdrawal = linkPost(32, name, [{ scope: "昼", status: "withdrawn" }], [], 12);
    const pending = linkPost(33, name, [], [announcement("uncertain", null, { shift: "夜" })], 14);
    const posts = [withdrawal, night, pending, { ...first, observedAt: "2026-09-09T10:00:00+09:00" }];
    assert.equal(findLink(posts, name, "昼"), null);
    assert.equal(findLink(posts, name, "夜"), night);
    const newerUnspecified = linkPost(34, name, work(), [], 15);
    assert.equal(findLink([...posts, newerUnspecified], name, "昼"), null,
      "unknown scope is not an explicit reversal of a day cancellation");
    assert.equal(findLink([...posts, newerUnspecified], name, "夜"), newerUnspecified);
    const conflict = linkPost(35, name, [{ scope: "夜", status: "conflict" }], [], 16);
    assert.equal(findLink([...posts, conflict], name, "夜"), null);
    const restored = linkPost(36, name, work("昼"), [], 17);
    assert.equal(findLink([...posts, conflict, restored], name, "昼"), restored);
    assert.equal(findLink([...posts, conflict, restored], name, "夜"), null);
    const tie = linkPost(37, name, work("昼"), [], 17);
    assert.equal(findLink([tie, restored], name, "昼"), tie, "same timestamp uses original numeric ID order");
    const absence = linkPost(38, name, [], [announcement("absence", null, { shift: "夜" })], 18);
    const late = linkPost(39, name, undefined, [announcement("late")], 19);
    assert.equal(findLink([night, absence, late], name, "夜"), null,
      "absence events remain cancellation facts even with authoritative empty links");
    const returned = linkPost(40, name, undefined, [announcement("return")], 20);
    assert.equal(findLink([night, absence, returned], name, "夜"), returned);
    const unlinkedReturn = { ...returned, links: [] };
    const laterWork = linkPost(41, name, work(), [], 21);
    assert.equal(findLink([night, conflict, unlinkedReturn], name, "夜"), null,
      "an authoritative empty return does not derive its own link");
    assert.equal(findLink([night, conflict, unlinkedReturn, laterWork], name, "夜"), laterWork,
      "an explicit return resolves that scope before a later verified unknown-scope post");
  }
});

test("storeless work selects its own post while keeping placement and contradiction safety separate", () => {
  const old = linkPost(50, "あお", undefined, [announcement("placement", "s1", { shift: "昼" })], 8);
  const newer = linkPost(51, "あお", work("昼"), [], 13);
  const late = linkPost(52, "あお", undefined, [announcement("late", null, { shift: "昼", time: "16:00" })], 14);
  const pending = linkPost(53, "あお", [], [announcement("uncertain", null, { shift: "昼" })], 15);
  const personal = linkSnapshot([pending, late, newer, old]);
  const resolved = api.resolveShiftRoster({ insights: linkInsights, personal, dateKey: linkDay, shift: "昼",
    schedule: {}, roster: [] });
  assert.equal(resolved.personal.byMaid.get("あお").placementSource, old);
  assert.equal(findLink(personal.posts), late);
  for (const status of ["partial", "paused", "budget-exhausted", "no-results"]) {
    assert.equal(findLink([], "あお", "昼", { personal: { ...personal, lastRun: { status },
      pending: [{ status: "pending" }], resolved: [{ status: "refusal" }, { status: "no_event" }] } }), late);
  }
  const officialPost = { ...makePost("2096900000000000054", "s2", ["あお"]),
    date: linkDay, createdAt: `${linkDay}T12:00:00+09:00` };
  const observations = { ...fixture, posts: [officialPost] };
  assert.equal(findLink([old], "あお", "昼", { observations }), null, "a superseded wrong-store post is not current");
  assert.equal(findLink([old, newer], "あお", "昼", { observations }), newer, "later storeless work is not wrong-store evidence");
  assert.equal(findLink([old, pending], "あお", "昼", { observations }), null, "pending cannot restore a stale link");
  const correction = linkPost(55, "あお", [], [announcement("placement", "s3", { shift: "昼" })], 13);
  assert.equal(findLink([old, correction]), null, "a confirmed correction without a verified new link clears contradictory old content");
  const impossible = linkPost(56, "あお", work("昼"), [
    announcement("placement", "s1", { shift: "昼" }), announcement("placement", "s2", { shift: "昼" })
  ], 14);
  assert.equal(findLink([old, impossible]), null, "same-post explicit conflicting stores hold only that side");
});

test("own-day identity, aliases and the reviewed post-specific addition are display-only", () => {
  const post = linkPost(60, "あお", work());
  for (const extra of [{ date: "2026-09-07" }, { createdAt: "2026-09-05T14:59:59Z" },
    { createdAt: "2026-09-06T15:00:00Z" }, { authorScreenName: "wrong" }, { authorId: "" },
    { url: "https://example.com/post" }, { authorScreenName: "wrong", url: `https://x.com/wrong/status/${post.id}` }]) {
    assert.equal(findLink([{ ...post, ...extra }]), null, JSON.stringify(extra));
  }
  const midnight = { ...post, createdAt: "2026-09-05T15:00:00Z" };
  assert.equal(findLink([midnight]), midnight, "same day means JST, not UTC");
  const alias = { ...linkPost(61, "まこっちゃん", work()), name: "まこと" };
  const raw = JSON.stringify([alias, personalFixture, additions, insights]);
  assert.equal(findLink([alias], "まこっちゃん"), alias);
  assert.equal(findLink([alias], "まこと"), alias);
  const miria = linkPost(62, "みりあ", work());
  assert.equal(findLink([miria], "みらい"), null);
  assert.equal(findLink([miria], "みりあ"), miria);
  const reviewed = { personal: personalFixture, insights, personalEventAdditions: additions };
  assert.equal(findLink([], "ららこ", "夜", reviewed).url, personalFixture.posts[1].url);
  assert.equal(findLink([], "ららこ", "夜", { ...reviewed, personalEventAdditions: {} }), null);
  const other = structuredClone(personalFixture);
  other.posts[1].id = "2096253883677044838";
  other.posts[1].url = `https://x.com/rarako_zettai/status/${other.posts[1].id}`;
  assert.equal(findLink([], "ららこ", "夜", { ...reviewed, personal: other }), null);
  assert.equal(JSON.stringify([alias, personalFixture, additions, insights]), raw);
});

test("reviewed night link survives partial v5 links without changing raw data or overriding withdrawals", () => {
  const post = { ...structuredClone(personalFixture.posts[1]), links: work("昼") };
  const personal = linkSnapshot([post]);
  const raw = JSON.stringify([personal, additions]);
  const view = () => api.personalPostsForView(personal, additions)[0];
  const resolve = (snapshot = personal, rules = additions) => findLink([], "ららこ", "夜", {
    personal: snapshot, insights, personalEventAdditions: rules
  });
  assert.equal(post.events.some(event => event.shift === "夜"), false);
  assert.deepEqual(view().links, [...work("昼"), ...work("夜")]);
  assert.equal(resolve()?.url, post.url, "new day-only analysis must retain the user-reviewed night link");
  assert.equal(view().events.filter(event => event.shift === "夜").length, 1);
  const projected = view();
  assert.equal(api.personalPostsForView(linkSnapshot([projected]), additions)[0], projected,
    "view-only projection is idempotent");
  const emptyLinks = linkSnapshot([{ ...post, links: [] }]);
  assert.deepEqual(api.personalPostsForView(emptyLinks, additions)[0].links, work("夜"),
    "only the independently reviewed scope is restored, never unreviewed raw-event work links");
  for (const status of ["withdrawn", "conflict"]) {
    const explicit = { ...post, links: [...work("昼"), { scope: "夜", status }] };
    const snapshot = linkSnapshot([explicit]);
    assert.deepEqual(api.personalPostsForView(snapshot, additions)[0].links, explicit.links);
    assert.equal(resolve(snapshot), null, `explicit night ${status} wins over the reviewed addition`);
    const later = { ...post, id: "2097500000000000001",
      url: "https://x.com/rarako_zettai/status/2097500000000000001",
      createdAt: "2026-09-06T18:00:00+09:00", events: [], links: [{ scope: "夜", status }] };
    assert.equal(resolve(linkSnapshot([later, { ...post, observedAt: "2026-09-08T01:00:00+09:00" }])), null,
      "later confirmed scope facts win over an older post re-analysis");
  }
  const absence = { ...post, id: "2097500000000000002",
    url: "https://x.com/rarako_zettai/status/2097500000000000002",
    createdAt: "2026-09-06T19:00:00+09:00", events: [announcement("absence")], links: [] };
  assert.equal(resolve(linkSnapshot([post, absence])), null, "later legacy cancellation facts still win");
  const dayWithdrawal = linkSnapshot([{ ...post, links: [{ scope: "昼", status: "withdrawn" }] }]);
  assert.equal(resolve(dayWithdrawal)?.url, post.url, "day withdrawal cannot remove the reviewed night scope");
  for (const field of ["name", "authorId", "authorScreenName", "date"]) {
    const mismatched = structuredClone(additions);
    mismatched[post.id][field] += "wrong";
    assert.equal(api.personalPostsForView(personal, mismatched)[0], post);
    assert.equal(resolve(personal, mismatched), null, `the reviewed ${field} binding must match exactly`);
  }
  const other = { ...post, id: "2096253883677044838",
    url: "https://x.com/rarako_zettai/status/2096253883677044838" };
  assert.equal(resolve(linkSnapshot([other])), null, "the correction is never generalized to another post");
  assert.equal(resolve(personal, Object.create(additions)), null, "inherited rules are not approved bindings");
  const uncertainRule = structuredClone(additions);
  uncertainRule[post.id].events = [announcement("uncertain")];
  assert.deepEqual(api.personalPostsForView(personal, uncertainRule)[0].links, work("昼"));
  assert.equal(resolve(personal, uncertainRule), null, "an uncertain addition is never a verified work link");
  const existingNight = linkSnapshot([{ ...post, events: [...post.events, announcement("absence")] }]);
  assert.equal(api.personalPostsForView(existingNight, additions)[0], existingNight.posts[0]);
  assert.equal(resolve(existingNight), null, "a raw night event prevents inserting the reviewed night addition");
  assert.equal(JSON.stringify([personal, additions]), raw, "neither source events/links nor the approved rule are rewritten");
});
