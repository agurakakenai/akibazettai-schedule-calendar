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
