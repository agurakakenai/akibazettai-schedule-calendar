"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const api = require("../app.js");
const { emptyHalfMonthSchedules } = require("./fixtures/half-month-schedules.js");
const context = { window: {} };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "..", "data", "members.js"), "utf8"), context);
const publicRegistry = JSON.parse(JSON.stringify(context.window.MEMBER_REGISTRY));
const copy = value => JSON.parse(JSON.stringify(value));
const newMember = (changes = {}) => ({
  memberId: "m-00000000000000000000000000000001", canonicalName: "新しい人", displayName: "新しい人",
  aliases: [], role: "normal", membership: "active", collection: "enabled", inactiveFrom: null,
  xProfileUrl: "https://x.com/new_member", homeStore: null, homeStoreSource: null,
  officialListing: "unknown", orderBefore: null, ...changes
});
const registryWith = (...members) => ({ schemaVersion: 1, members, unresolvedNames: [] });
const halfSource = (member, changes = {}) => {
  const createdAt = "2026-09-01T01:00:00Z";
  const id = (((BigInt(Date.parse(createdAt)) - 1288834974657n) << 22n) + 1n).toString();
  const authorScreenName = member.xProfileUrl.split("/").at(-1);
  return {
    id, url: `https://x.com/${authorScreenName}/status/${id}`, name: member.canonicalName,
    authorId: "123456789", authorScreenName, createdAt, observedAt: "2026-09-01T02:00:00Z",
    sourceKind: "half-month-schedule",
    period: { from: "2026-09-01", to: "2026-09-15", printedYear: 2026, yearBasis: "printed" },
    days: [{ date: "2026-09-06", shifts: ["昼"] }, { date: "2026-09-07", shifts: ["夜"] }],
    ...changes
  };
};
const halfFeed = source => ({ ...emptyHalfMonthSchedules(), schedules: [source] });
const personalPost = (member, changes = {}) => {
  const createdAt = "2026-09-07T01:00:00Z";
  const id = (((BigInt(Date.parse(createdAt)) - 1288834974657n) << 22n) + 1n).toString();
  const authorScreenName = member.xProfileUrl.split("/").at(-1);
  return {
    id, url: `https://x.com/${authorScreenName}/status/${id}`, name: member.canonicalName,
    authorId: "123456789", authorScreenName, createdAt, observedAt: "2026-09-07T02:00:00Z",
    date: "2026-09-07", events: [], links: [{ scope: "夜", status: "work" }], ...changes
  };
};
const personalFeed = post => ({
  schemaVersion: 1, complete: false, checkedAt: null, lastSuccessAt: null, posts: post ? [post] : [],
  lastRun: { status: "never" }
});

test("the public registry is strict, immutable and separates all identities from current membership", () => {
  assert.equal(api.validateMemberRegistry(publicRegistry), publicRegistry);
  const registry = api.createMemberRegistryAdapter(publicRegistry);
  assert.equal(registry.activeNames.length, 40);
  assert.equal(registry.kitchenStaff.length, 5);
  assert.equal(registry.activeNames.filter(name => !registry.kitchenStaff.includes(name)).length, 35);
  assert.equal(registry.unresolvedNames.length, 11);
  assert.ok(!registry.activeNames.includes("わたげ"));
  assert.equal(registry.canonicalName("まこと"), "まこっちゃん");
  assert.equal(registry.displayNames["まこっちゃん"], "まこと");
  const raw = registryWith(newMember());
  const adapted = api.createMemberRegistryAdapter(raw);
  raw.members[0].displayName = "別の人";
  assert.equal(adapted.member("新しい人").displayName, "新しい人");
  assert.throws(() => adapted.activeNames.push("別の人"), TypeError);
  adapted.aliases().clear();
  assert.equal(adapted.canonicalName("新しい人"), "新しい人");
});

test("registry ordering honors transitive anchors even when an anchor is inactive", () => {
  const last = newMember({ memberId: "m-00000000000000000000000000000003", canonicalName: "最後", displayName: "最後",
    xProfileUrl: "https://x.com/last" });
  const first = newMember({ orderBefore: "m-00000000000000000000000000000002" });
  const middle = newMember({ memberId: first.orderBefore, canonicalName: "途中", displayName: "途中",
    xProfileUrl: "https://x.com/middle", membership: "inactive", collection: "paused", orderBefore: last.memberId });
  const registry = api.createMemberRegistryAdapter(registryWith(last, first, middle));
  assert.deepEqual(registry.knownNames, ["新しい人", "途中", "最後"]);
  assert.deepEqual(registry.activeNames, ["新しい人", "最後"]);
});

test("malformed registry never becomes a partial or legacy roster", () => {
  const invalid = [
    value => { value.schemaVersion = "1"; },
    value => { value.extra = true; },
    value => { delete value.unresolvedNames; },
    value => { value.members[0].promotedAt = "2026-09-01"; },
    value => { delete value.members[0].homeStore; },
    value => { value.members[0].memberId += "\n"; },
    value => { value.members[0].canonicalName += "\n"; },
    value => { value.members[0].displayName = "<script>"; },
    value => { value.members[0].aliases = ["旧名", "旧名"]; },
    value => { value.members[0].aliases = new Array(1); },
    value => { value.members[0].aliases = ["新しい人"]; },
    value => { value.members[0].membership = "retired"; },
    value => { value.members[0].membership = "inactive"; },
    value => { value.members[0].collection = null; },
    value => { value.members[0].role = "admin"; },
    value => { value.members[0].inactiveFrom = "2026-09-07"; },
    value => { Object.assign(value.members[0], { membership: "inactive", collection: "paused", inactiveFrom: "2026-02-30" }); },
    value => { value.members[0].xProfileUrl = "https://twitter.com/new_member"; },
    value => { value.members[0].xProfileUrl = "https://x.com/New_Member"; },
    value => { value.members[0].xProfileUrl = "https://x.com/home"; },
    value => { value.members[0].xProfileUrl = "https://x.com/new_member/status/123"; },
    value => { value.members[0].xProfileUrl += "?s=20"; },
    value => { value.members[0].xProfileUrl += "\n"; },
    value => { value.members[0].homeStore = "s1"; },
    value => { value.members[0].homeStoreSource = "official"; },
    value => { value.members[0].officialListing = true; },
    value => { value.members[0].orderBefore = value.members[0].memberId; },
    value => { value.members[0].orderBefore = "m-000000000000000000000000000000ff"; },
    value => { value.unresolvedNames = ["新しい人"]; },
    value => { value.unresolvedNames = ["未解決", "未解決"]; },
    value => { value.members.push(copy(value.members[0])); },
    value => { value.members.push(newMember({ memberId: "m-00000000000000000000000000000002", canonicalName: "別人", displayName: "別人" })); }
  ];
  for (const mutate of invalid) {
    const value = registryWith(newMember());
    mutate(value);
    assert.throws(() => api.createMemberRegistryAdapter(value), /名簿/, String(mutate));
  }
  for (const value of [null, undefined, {}, [], { ...publicRegistry, members: null }]) {
    assert.throws(() => api.createMemberRegistryAdapter(value), /名簿/);
  }
  const first = newMember({ orderBefore: "m-00000000000000000000000000000002" });
  const second = newMember({ memberId: first.orderBefore, canonicalName: "別人", displayName: "別人",
    xProfileUrl: "https://x.com/another", orderBefore: first.memberId });
  assert.throws(() => api.createMemberRegistryAdapter(registryWith(first, second)), /名簿/);
});

test("name plus X registration supplies identity and a warning but no attendance or store", () => {
  const registry = api.createMemberRegistryAdapter(registryWith(newMember()));
  const insights = { memberRegistry: registry };
  const manual = {};
  const schedule = api.buildEffectiveSchedule(manual, undefined, { registry });
  assert.deepEqual(schedule, {});
  assert.equal(registry.profileUrl("新しい人"), "https://x.com/new_member");
  assert.deepEqual(registry.homeStore, {});
  assert.equal(Object.hasOwn(registry.member("新しい人"), "promotedAt"), false);
  assert.deepEqual(api.memberFilterNames(registry, { schedule }), ["新しい人"]);
  const note = api.schedulePendingNote(insights, undefined, { dateFrom: "2026-09-01", dateTo: "2026-09-15" });
  assert.match(note.short, /1名/);
  assert.match(note.long, /新しい人/);
  assert.match(note.long, /未投稿・未提出・欠勤を意味しません/);
  assert.deepEqual(api.resolveShiftRoster({ insights, schedule, roster: registry.knownNames,
    dateKey: "2026-09-07", shift: "夜" }).entries, []);
  assert.equal(api.assignShiftStores({ insights, members: registry.activeNames, shift: "夜", storeIds: ["s1"] }), null);
  assert.equal(api.eventStorePins({ insights: { ...insights, maidTendency: { 新しい人: { home: "s4" } } },
    entries: [{ name: "新しい人", featured: true }], homeStore: registry.homeStore }).size, 0);
  const latin = api.createMemberRegistryAdapter(registryWith(newMember({ canonicalName: "constructor", displayName: "constructor" })));
  const latinInsights = { memberRegistry: latin, maidTendency: {} };
  assert.equal(api.assignShiftStores({ insights: latinInsights, members: latin.activeNames, shift: "昼", storeIds: ["s1"] }), null);
  assert.equal(api.eventStorePins({ insights: latinInsights, entries: [{ name: "constructor", featured: true }],
    homeStore: latin.homeStore }).size, 0);
});

test("paused and retired half-month history validates by known identity and saved author bindings", () => {
  for (const membership of ["active", "inactive", "unconfirmed"]) {
    const member = newMember({ membership, collection: "paused" });
    const registry = api.createMemberRegistryAdapter(registryWith(member));
    const source = halfSource(member);
    const feed = halfFeed(source);
    const before = JSON.stringify(feed);
    assert.equal(api.validateHalfMonthSchedules(feed, { registry, roster: [] }), feed);
    assert.equal(JSON.stringify(feed), before);
    const prior = copy(source);
    prior.authorId = "987654321";
    assert.throws(() => api.validateHalfMonthSchedules(feed, { registry, previous: halfFeed(prior) }), /本人情報/);
    const other = { ...personalPost(member), name: "未解決" };
    assert.throws(() => api.validateHalfMonthSchedules(feed, { registry, personal: personalFeed(other) }), /本人情報/);
    const wrong = copy(source);
    wrong.authorScreenName = "unregistered";
    wrong.url = `https://x.com/${wrong.authorScreenName}/status/${wrong.id}`;
    assert.throws(() => api.validateHalfMonthSchedules(halfFeed(wrong), { registry, previous: feed }), /本人情報/);
  }
  const registry = api.createMemberRegistryAdapter({ ...registryWith(), unresolvedNames: ["新しい人"] });
  assert.throws(() => api.validateHalfMonthSchedules(halfFeed(halfSource(newMember())), { registry }), /本人確認/);
});

test("display aliases identify sources without rewriting names, provenance or timing facts", () => {
  const member = newMember({ displayName: "新表示", aliases: ["新表示", "旧表示"] });
  const registry = api.createMemberRegistryAdapter(registryWith(member));
  const source = halfSource(member, { name: "旧表示" });
  const feed = halfFeed(source);
  const insights = { memberRegistry: registry, maidTendency: { 新しい人: { x: "stale_handle", alias: "無関係" } } };
  api.validateHalfMonthSchedules(feed, { registry, insights, personal: personalFeed(personalPost(member)) });
  const before = JSON.stringify(feed);
  const schedule = api.buildEffectiveSchedule({ "2026-09-07": { 夜: [{ name: "新表示", featured: true }] } }, feed, { registry });
  assert.equal(schedule["2026-09-07"].夜.length, 1);
  assert.equal(schedule["2026-09-07"].夜[0].name, member.canonicalName);
  assert.equal(schedule["2026-09-07"].夜[0].halfMonthSources[0].name, "旧表示");
  assert.equal(JSON.stringify(feed), before);
  const options = { schedule, insights, dateKey: "2026-09-07", shift: "夜", name: "新表示" };
  assert.equal(api.rosterPostLink(options).post.url, source.url);
  const own = personalPost(member, { name: "旧表示" });
  assert.equal(api.rosterPostLink({ ...options, personal: personalFeed(own) }).post.url, own.url);
  assert.equal(api.rosterPostLink({ ...options, shift: "昼" }), null);
  assert.equal(api.rosterPostLink({ ...options, dateKey: "2026-09-08" }), null);
  const duplicate = copy(source);
  duplicate.name = "新しい人";
  assert.throws(() => api.validateHalfMonthSchedules({ ...feed, schedules: [source, duplicate] }, { registry }), /重複/);
});

test("current profile changes cannot invalidate saved historical bindings or source links", () => {
  const original = newMember();
  const feed = halfFeed(halfSource(original));
  const personal = personalFeed(personalPost(original));
  const before = JSON.stringify([feed, personal]);
  for (const xProfileUrl of [null, "https://x.com/reviewed_later"]) {
    const registry = api.createMemberRegistryAdapter(registryWith(newMember({
      membership: "inactive", collection: "review", xProfileUrl
    })));
    const insights = { memberRegistry: registry };
    assert.equal(api.validateHalfMonthSchedules(feed, { registry, previous: feed, personal }), feed);
    assert.equal(api.validateHalfMonthSchedules(feed, { registry }), feed, "first load is also a published historical snapshot");
    const schedule = api.buildEffectiveSchedule({}, feed, { registry });
    const options = { schedule, insights, name: original.canonicalName, dateKey: "2026-09-07", shift: "夜" };
    assert.equal(api.rosterPostLink(options).post.url, feed.schedules[0].url);
    assert.equal(api.rosterPostLink({ ...options, personal }).post.url, personal.posts[0].url);
    assert.equal(registry.profileUrl(original.canonicalName), xProfileUrl, "only profile navigation follows current registry metadata");
    assert.equal(JSON.stringify([feed, personal]), before);
  }
});

test("inactiveFrom cuts plan-only projection, not raw sources, actual history or later explicit evidence", () => {
  const member = newMember({ membership: "inactive", collection: "paused", inactiveFrom: "2026-09-07", role: "kitchen" });
  const registry = api.createMemberRegistryAdapter(registryWith(member));
  const feed = halfFeed(halfSource(member));
  feed.schedules[0].workTiming = { schemaVersion: 1, facts: [{
    serviceDate: "2026-09-07", shift: "夜", boundary: "start", status: "set", qualifier: "late", explicitTime: null,
    source: Object.fromEntries(["id", "url", "name", "authorId", "authorScreenName", "createdAt", "sourceKind"]
      .map(field => [field, feed.schedules[0][field]]))
  }] };
  api.validateHalfMonthSchedules(feed, { registry });
  const manual = { "2026-09-06": { 昼: [{ name: member.canonicalName }] }, "2026-09-07": { 夜: [{ name: member.canonicalName }] } };
  const before = JSON.stringify([manual, feed]);
  const schedule = api.buildEffectiveSchedule(manual, feed, { registry });
  const sourceSchedule = api.buildEffectiveSchedule(manual, feed, { registry, includeInactivePlans: true });
  assert.equal(schedule["2026-09-06"].昼.length, 1);
  assert.equal(schedule["2026-09-07"].夜.length, 0);
  assert.equal(JSON.stringify([manual, feed]), before);
  const insights = { memberRegistry: registry, stores: [{ id: "s2" }],
    actualRoster: { "2026-09-07": { 夜: { stores: { s2: [member.canonicalName] }, trainees: [] } } } };
  const options = { insights, schedule, sourceSchedule, roster: registry.knownNames, dateKey: "2026-09-07", shift: "夜" };
  const recorded = api.resolveShiftRoster(options);
  assert.equal(recorded.entries[0].name, member.canonicalName);
  assert.equal(recorded.entries[0].halfMonthSources[0].url, feed.schedules[0].url);
  assert.equal(api.workTimingLabel(recorded.entries[0].workTimingNote), "おそめ");
  assert.equal(recorded.entries[0].memberReview, undefined);
  assert.deepEqual(api.memberFilterNames(registry, { schedule, insights, dateFrom: "2026-09-07", dateTo: "2026-09-07" }), [member.canonicalName]);
  assert.deepEqual(api.memberFilterNames(registry, { schedule, dateFrom: "2026-09-07", dateTo: "2026-09-07" }), []);
  const personal = personalFeed(personalPost(member, { events: [{ shift: "夜", kind: "placement", storeId: "s2", excerpt: "勤務" }] }));
  const placed = api.resolveShiftRoster({ ...options, insights: { ...insights, actualRoster: {} }, personal });
  assert.equal(placed.entries[0].personalPlacement, true);
  assert.equal(placed.entries[0].name, member.canonicalName);
});

test("unknown inactive dates retain future plans with review, and kitchen history remains filterable", () => {
  const member = newMember({ membership: "inactive", collection: "paused", role: "kitchen" });
  const registry = api.createMemberRegistryAdapter(registryWith(member));
  const feed = halfFeed(halfSource(member));
  const schedule = api.buildEffectiveSchedule({}, feed, { registry, today: "2026-09-07" });
  assert.equal(schedule["2026-09-06"].昼[0].memberReview, undefined);
  assert.match(schedule["2026-09-07"].夜[0].memberReview, /適用日が未確認/);
  assert.deepEqual(registry.activeNames, []);
  assert.deepEqual(registry.kitchenStaff, [member.canonicalName]);
  assert.deepEqual(api.memberFilterNames(registry, { schedule, dateFrom: "2026-09-07", dateTo: "2026-09-07" }), [member.canonicalName]);
  assert.deepEqual(api.memberFilterNames(registry, { schedule, dateFrom: "2026-10-01", dateTo: "2026-10-31" }), []);
});

test("pending warning distinguishes manual positive days from automatic half-month coverage", () => {
  const member = newMember();
  const registry = api.createMemberRegistryAdapter(registryWith(member));
  const insights = { memberRegistry: registry, schedulePending: { pending: [], rostered: 40 } };
  const scope = { dateFrom: "2026-09-01", dateTo: "2026-09-30",
    manual: { "2026-09-06": { 昼: [{ name: member.canonicalName }] } } };
  const before = JSON.stringify([insights.schedulePending, scope.manual]);
  const unconfirmed = api.schedulePendingNote(insights, undefined, scope);
  assert.match(unconfirmed.long, /手入力の予定あり・半月全体は未確認/);
  assert.match(unconfirmed.short, /在籍1名のうち1名/);
  const feed = halfFeed(halfSource(member));
  const partly = api.schedulePendingNote(insights, feed, scope);
  assert.match(partly.short, /一部の半月を確認済み/);
  assert.equal(api.schedulePendingNote(insights, feed, { ...scope, dateTo: "2026-09-15" }), null);
  assert.ok(api.schedulePendingNote(insights, feed, { ...scope, dateFrom: "2026-10-01", dateTo: "2026-10-15" }));
  assert.equal(JSON.stringify([insights.schedulePending, scope.manual]), before);
});

test("registered timing-only posts still cannot create attendance, shops or core links", () => {
  const member = newMember();
  const registry = api.createMemberRegistryAdapter(registryWith(member));
  const insights = { memberRegistry: registry, stores: [{ id: "s1" }] };
  const post = personalPost(member, { links: [] });
  post.workTiming = { schemaVersion: 1, facts: [{
    serviceDate: post.date, shift: "夜", boundary: "start", status: "set", qualifier: "late", explicitTime: null,
    source: { ...Object.fromEntries(["id", "url", "name", "authorId", "authorScreenName", "createdAt"]
      .map(field => [field, post[field]])), sourceKind: "personal-work-post" }
  }] };
  const personal = personalFeed(post);
  api.validatePersonalShifts(personal);
  const options = { insights, personal, dateKey: post.date, shift: "夜", schedule: {}, roster: registry.knownNames, name: member.canonicalName };
  assert.deepEqual(api.resolveShiftRoster(options).entries, []);
  assert.equal(api.dayHasPersonStoreEvidence(insights, null, post.date, personal), false);
  assert.equal(api.rosterPostLink(options), null);
});
