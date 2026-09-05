"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const api = require("../app.js");
const context = { window: {} };
vm.createContext(context);
for (const file of ["schedule.js", "store-insights.js"]) {
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "data", file), "utf8"), context);
}
const insights = context.window.STORE_INSIGHTS;
const schedule = context.window.SCHEDULE_DATA;
const before = JSON.stringify(insights);
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
console.log("Observed shifts valid: partial coverage, source identity, per-person evidence, aliases, and curated precedence.");
