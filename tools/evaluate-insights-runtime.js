"use strict";
// Invoked by evaluate-insights.py. Counterfactuals alter only in-memory call paths.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const api = require(path.join(root, "app.js"));
function load(file, key) {
  const context = { window: {} };
  vm.runInNewContext(fs.readFileSync(path.join(root, "data", file), "utf8"), context);
  return JSON.parse(JSON.stringify(context.window[key]));
}
const shipped = load("store-insights.js", "STORE_INSIGHTS");
const schedule = load("schedule.js", "SCHEDULE_DATA");
const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
const call = "applySameDayEvidence(insights, forecastOutlook(insights, key, shift, last), key, shift)";
assert.equal(source.split(call).length, 2, "counterfactual must replace exactly one call");
const context = { module: { exports: {} } };
vm.runInNewContext(source.replace(call, "forecastOutlook(insights, key, shift, last)"), context);
const before = context.module.exports;
const key = ids => [...ids].sort().join(",");
const results = {};
function score(label, truth, left, right) {
  const row = results[label] ??= { n: 0, before: 0, after: 0, wins: 0, losses: 0 };
  const a = truth === left, b = truth === right;
  row.n++; row.before += a; row.after += b; row.wins += b && !a; row.losses += a && !b;
}
function outlook(which, ins, c, members, full) {
  let o = which.getStoreOutlook({
    insights: ins, dateKey: c.date, shift: c.shift, lastActualDate: api.addDays(c.date, -1)
  });
  if (full) {
    o = api.applyHomeStaff(ins, o, members, schedule.homeStore, schedule.kitchenStaff);
    o = api.applyPostedTilt(ins, o, c.shift, members);
  }
  return o;
}
function compare(label, ins, c, members, full, truth) {
  score(label, truth,
    key(api.expectedOpenStores(ins, c.shift, outlook(before, ins, c, members, full), members)),
    key(api.expectedOpenStores(ins, c.shift, outlook(api, ins, c, members, full), members)));
}
const roster = new Set(schedule.roster);
const frozenDates = new Set();
for (const date of Object.keys(shipped.actual).sort()) {
  const truth = shipped.actual[date]?.夜;
  const rec = shipped.actualRoster[date]?.夜;
  if (!truth || !rec || !shipped.actual[date]?.昼 ||
      !shipped.actual[api.addDays(date, -1)]?.夜) continue;
  // Deliberately preserve the old, non-deduplicated current-roster pool as a separate control.
  const members = Object.values(rec.stores).flat().filter(m => roster.has(m));
  if (!members.length) continue;
  frozenDates.add(date);
  const ins = JSON.parse(JSON.stringify(shipped));
  delete ins.actual[date].夜;
  compare("frozenShippedLegacy", ins, { date, shift: "夜" }, members, false, key(truth));
}
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const emitted = [], oracleFiltered = [];
for (const { case: c, ins } of payload.snapshots) {
  for (const name of ["actual", "actualRoster", "actualWithoutRoster"]) {
    assert.equal(ins[name][c.date]?.[c.shift], undefined, `${name}: target leaked`);
    assert.ok(Object.keys(ins[name]).every(d => d <= c.date), `${name}: future leaked`);
  }
  const members = schedule.roster.filter(m => c.listed.includes(m));
  const truth = key(Object.keys(c.stores));
  if (c.shift === "夜" && frozenDates.has(c.date)) {
    const legacyMembers = Object.values(c.stores).flat().filter(m => roster.has(m));
    compare("causalSameLegacyPool", ins, c, legacyMembers, false, truth);
  }
  if (c.shift === "夜" && c.date >= "2026-03-07" && ins.actual[c.date]?.昼 &&
      ins.actual[api.addDays(c.date, -1)]?.夜) {
    compare("causalProductionPoolAndCorrections", ins, c, members, true, truth);
  }
  const o = outlook(api, ins, c, members, true);
  const stores = api.expectedOpenStores(ins, c.shift, o, members);
  const move = api.earlierShiftPlaces(ins, c.date, c.shift);
  const options = { insights: ins, members, shift: c.shift, outlook: o,
    kitchenStaff: schedule.kitchenStaff };
  const assignment = api.getShiftAssignment({ ...options, movedFrom: move });
  if (c.shift === "夜" && c.date >= "2026-02-16" && ins.actualRoster[c.date]?.昼 &&
      Object.keys(c.stores).length >= 2 && assignment) {
    const base = api.getShiftAssignment(options);
    for (const name of members) {
      if (!move?.has(name) || schedule.kitchenStaff.includes(name)) continue;
      const actual = Object.keys(c.stores).find(s => c.stores[s].includes(name));
      score("causalMovingFloor", actual, base.byMaid.get(name)?.storeId,
        assignment.byMaid.get(name)?.storeId);
    }
  }
  if (!members.length || !stores.length) continue;
  const traineeStores = Object.keys(c.stores).filter(s => c.stores[s].some(m => c.trainees.includes(m)));
  const usual = ins.typicalHeadcount[c.shift];
  const pick = fn => [...stores].sort((x, y) => fn(y) - fn(x) ||
    shipped.stores.findIndex(s => s.id === x) - shipped.stores.findIndex(s => s.id === y))[0];
  const capacities = api.storeCapacities(ins, c.shift, stores, members.length);
  const probabilityCounts = Object.fromEntries(stores.map(s => [s, 0]));
  for (const name of members) {
    const probabilities = api.storeProbabilities(ins, name, c.shift, stores);
    for (const sid of stores) probabilityCounts[sid] += probabilities[sid] ?? 0;
  }
  const choice = {
    date: c.date, noTrainees: !traineeStores.length,
    random: stores.filter(s => traineeStores.includes(s)).length / stores.length,
    fixed: traineeStores.includes(stores.includes("s1") ? "s1" : stores[0]),
    integerCapacity: traineeStores.includes(pick(s => usual[s] - capacities[s])),
    probabilityGap: traineeStores.includes(pick(s => usual[s] - probabilityCounts[s])),
    byStore: traineeStores.includes(pick(s => ins.traineeOutlook[c.shift]?.byStore?.[s] ?? 0))
  };
  if (c.trainees.length && Object.keys(c.stores).length >= 2) oracleFiltered.push(choice);
  if (api.expectedTrainees(ins, c.shift, stores.length, false) > 0) emitted.push(choice);
}
function summarize(rows) {
  return {
    n: rows.length, noTrainees: rows.filter(r => r.noTrainees).length,
    methods: Object.fromEntries(["random", "fixed", "integerCapacity", "probabilityGap", "byStore"]
      .map(k => [k, rows.reduce((n, r) => n + Number(r[k]), 0)]))
  };
}
results.snapshots = payload.snapshots.length;
results.traineeOracleFiltered = summarize(oracleFiltered);
results.traineeProductionDisplay = summarize(emitted);
console.log(JSON.stringify(results));
