"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {
  dateKey, monthCells, dayEvents, eventImageFor, tokyoToday,
  getStoreOutlook, getShiftAssignment, maidItinerary, getMaidStoreOutlook
} = require("../app.js");
const context = { window: {} };
vm.createContext(context);
for (const file of ["schedule.js", "store-insights.js"]) {
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "data", file), "utf8"), context);
}
const data = context.window.SCHEDULE_DATA;
const insights = context.window.STORE_INSIGHTS;

for (const file of ["index.html", "unauthorized.html"]) {
  const html = fs.readFileSync(path.join(__dirname, "..", file), "utf8");
  assert.match(html, /<meta name="color-scheme" content="only light">/);
  assert.doesNotMatch(html, /prefers-color-scheme|scoutTheme|color-scheme:\s*dark/);
}
const css = fs.readFileSync(path.join(__dirname, "..", "styles.css"), "utf8");
assert.match(css, /color-scheme:\s*only light/);
assert.doesNotMatch(css, /data-theme="dark"|prefers-color-scheme|forced-color-adjust:\s*none/);

for (const [year, monthIndex, days, start] of [
  [2026, 8, 30, 2], [2028, 1, 29, 2], [2026, 1, 28, 0], [2026, 0, 31, 4],
  [2026, 11, 31, 2], [2027, 0, 31, 5], [2100, 1, 28, 1], [2000, 1, 29, 2]
]) {
  const cells = monthCells(year, monthIndex);
  assert.equal(cells.length % 7, 0);
  assert.equal(cells.filter(Boolean).length, days);
  assert.ok(cells.slice(0, start).every((cell) => cell === null));
  assert.equal(cells[start].getDate(), 1);
  assert.equal(cells.findLast(Boolean).getDate(), days);
  assert.equal(cells[start].getDay(), start);
  assert.equal(new Set(cells.filter(Boolean).map(dateKey)).size, days);
}
assert.equal(tokyoToday(new Date("2026-12-31T15:00:00Z")), "2027-01-01");
assert.equal(dayEvents(data, insights, "2026-09-08").length, 1, "one host in both shifts is one decoration");
assert.equal(dayEvents(data, insights, "2026-09-01")[0].shifts.join(), "夜", "a night event must not create a day shift");
assert.equal(dayEvents(data, insights, "2026-09-16").length, 0, "no fabricated event from an anniversary date");

const fixture = {
  schedule: { "2026-09-08": {
    "昼": [{ name: "まこと", featured: true, eventLabel: "周年" }, { name: "あむ" }],
    "夜": [{ name: "まこっちゃん", featured: true, eventLabel: "周年" }, { name: "ちま", featured: true, eventLabel: "生誕" }]
  } },
  eventImages: {
    fallback: { src: "fallback.svg", alt: "fallback" },
    maids: { "ちま": { src: "maid.svg", alt: "maid" } },
    events: { "2026-09-08": { "ちま": { src: "event.svg", alt: "event" } } }
  }
};
const events = dayEvents(fixture, insights, "2026-09-08");
const sparseInsights = { ...insights, maidTendency: { ...insights.maidTendency, "未記録の在籍者": null } };
assert.deepEqual(dayEvents(fixture, sparseInsights, "2026-09-08"), events,
  "a roster member without tendency records must not break event grouping");
assert.equal(events.length, 2, "multiple hosts survive alias normalization and shift deduplication");
assert.equal(events[0].name, "まこっちゃん");
assert.equal(events[0].shifts.length, 2);
assert.equal(eventImageFor(fixture, "2026-09-08", "ちま").src, "event.svg");
assert.equal(eventImageFor(fixture, "2026-09-09", "ちま").src, "maid.svg");
assert.equal(eventImageFor(fixture, "2026-09-08", "あむ").src, "fallback.svg");
assert.ok(eventImageFor({}, "2026-09-08", "あむ").src.endsWith("flower.svg"));

const images = [data.eventImages.fallback, ...Object.values(data.eventImages.maids),
  ...Object.values(data.eventImages.events).flatMap(Object.values)];
for (const image of images) {
  assert.ok(image.alt.trim(), "every static image needs meaningful alt text");
  const file = path.resolve(__dirname, "..", image.src);
  assert.ok(file.startsWith(path.resolve(__dirname, "..") + path.sep));
  assert.ok(fs.existsSync(file), `${image.src} must ship with the site`);
  assert.match(fs.readFileSync(file, "utf8"), /<svg\b/, "placeholder artwork is a local SVG");
}
for (const [key, day] of Object.entries(data.schedule)) {
  for (const event of dayEvents(data, insights, key)) {
    assert.ok(data.eventImages.maids[event.name], `configure a distinct placeholder for ${event.name}`);
    assert.ok(Object.values(day).flat().some((entry) => entry.name === event.name && entry.featured));
  }
}

// R2: a valid stores-only input with a non-empty scheduled pool.
const key = "2026-09-03", shift = "昼";
const storesOnly = JSON.parse(JSON.stringify(insights));
delete storesOnly.actualRoster[key][shift];
storesOnly.actualWithoutRoster[key] = { [shift]: [...storesOnly.actual[key][shift]] };
const members = data.schedule[key][shift].map((entry) => entry.name);
const outlook = getStoreOutlook({ insights: storesOnly, dateKey: key, shift });
const assignment = getShiftAssignment({ insights: storesOnly, members, shift, outlook });
assert.equal(outlook.basis, "actual");
assert.ok(!assignment.recorded);
for (const name of members) {
  const plan = maidItinerary({
    schedule: data.schedule, name, dates: [key], shifts: [shift],
    resolve: () => ({ outlook, assignment })
  });
  assert.equal(plan.stops[0].recorded, false, `${name}: store evidence is not person evidence`);
  assert.equal(plan.stops[0].settled, false);
  assert.equal(plan.guesses, 1);
  const chip = getMaidStoreOutlook({ insights: storesOnly, name, shift, outlook, assignment });
  assert.notEqual(chip?.basis, "actual");
  assert.doesNotMatch(chip?.title ?? "", /にいた記録/);
}
console.log("Month calendar valid: month boundaries, event grouping, static art, and stores-only person evidence.");
