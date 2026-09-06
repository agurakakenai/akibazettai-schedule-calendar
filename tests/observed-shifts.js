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
