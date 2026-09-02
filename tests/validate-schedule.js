"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const schedulePath = path.join(__dirname, "..", "data", "schedule.js");
const source = fs.readFileSync(schedulePath, "utf8");
const context = { window: {} };
vm.runInNewContext(source, context, { filename: schedulePath });

const data = context.window.SCHEDULE_DATA;
const validShifts = new Set(["昼", "夜"]);
const roster = new Set(data.roster);
const rosterOrder = new Map(data.roster.map((name, index) => [name, index]));
const featuredEntries = [];

assert.ok(data, "SCHEDULE_DATA must be defined");
assert.equal(roster.size, data.roster.length, "roster contains duplicate names");

const kitchenStaff = [...(data.kitchenStaff ?? [])];
assert.ok(Array.isArray(data.kitchenStaff ?? []), "kitchenStaff must be an array");
assert.equal(
  new Set(kitchenStaff).size,
  kitchenStaff.length,
  "kitchenStaff contains duplicate names"
);
for (const name of kitchenStaff) {
  assert.ok(roster.has(name), `unknown kitchen staff "${name}"`);
}
assert.deepEqual(
  [...kitchenStaff].sort((a, b) => rosterOrder.get(a) - rosterOrder.get(b)),
  kitchenStaff,
  "kitchenStaff must follow official roster order"
);
assert.deepEqual(
  kitchenStaff,
  ["まこっちゃん", "あらた", "うる", "みりん", "けだま"],
  "kitchenStaff does not match the maids without a maid uniform on the official site"
);

// 所属店舗。roster と過不足なく揃っていないと、記念日の主役を置く店が
// 静かに推定へ落ちるので、そこを見張る。公式サイトにまだ載っていない人も、
// お店からの案内で配属は分かるため、ここには全員そろっていなければならない。
const homeStore = data.homeStore ?? {};
const unposted = data.unpostedMaids ?? [];
for (const name of unposted) {
  assert.ok(roster.has(name), `unpostedMaids has "${name}", who is not on the roster`);
}
assert.deepEqual(
  Object.keys(homeStore),
  [...data.roster],
  "homeStore must list every rostered maid, in the same order as the roster"
);

// 昇格日。見習いだったころの出勤を「事前に分かっていた人数」に数えると、
// 店舗数の閾値が上にずれる（実測で夜の3店舗判定が13名から15名に動く）。
const promotedAt = data.promotedAt ?? {};
for (const [name, date] of Object.entries(promotedAt)) {
  assert.ok(roster.has(name), `promotedAt has "${name}", who is not on the roster`);
  assert.match(date, /^\d{4}-\d{2}-\d{2}$/, `${name}'s promotion date must be a date`);
  assert.ok(
    date <= data.defaultDateTo,
    `${name} cannot be promoted after the calendar ends`
  );
}
// 公式サイトに載っていない人は、載っていないこと自体が最近の昇格を示すので、
// いつ昇格したかが要る。載っている人は昔から在籍しているので要らない。
for (const name of unposted) {
  assert.ok(
    promotedAt[name],
    `${name} is not on the site yet, so promotedAt must say when she stopped being a trainee`
  );
}
const storeIds = new Set(["s1", "s2", "s3", "s4"]);
for (const [name, store] of Object.entries(homeStore)) {
  assert.ok(storeIds.has(store), `${name} is posted to an unknown store "${store}"`);
}
// 公式サイトは4店とも人を抱えている。1店に寄っていたら転記を間違えている。
const posted = new Map([...storeIds].map((id) => [id, 0]));
for (const store of Object.values(homeStore)) {
  posted.set(store, posted.get(store) + 1);
}
for (const [store, count] of posted) {
  assert.ok(count > 0, `no one is posted to ${store}`);
}

for (const [date, day] of Object.entries(data.schedule)) {
  assert.match(date, /^\d{4}-\d{2}-\d{2}$/, `invalid date format: ${date}`);
  const parsed = new Date(`${date}T00:00:00Z`);
  assert.equal(
    parsed.toISOString().slice(0, 10),
    date,
    `invalid calendar date: ${date}`
  );

  for (const shift of Object.keys(day)) {
    assert.ok(validShifts.has(shift), `invalid shift "${shift}" on ${date}`);
  }

  for (const shift of validShifts) {
    assert.ok(Array.isArray(day[shift]), `${date} ${shift} must be an array`);
    const names = new Set();
    let previousRosterIndex = -1;

    for (const entry of day[shift]) {
      assert.ok(roster.has(entry.name), `unknown maid "${entry.name}" on ${date} ${shift}`);
      assert.ok(!names.has(entry.name), `duplicate maid "${entry.name}" on ${date} ${shift}`);
      assert.ok(
        rosterOrder.get(entry.name) > previousRosterIndex,
        `${date} ${shift} must follow official roster order`
      );
      names.add(entry.name);
      previousRosterIndex = rosterOrder.get(entry.name);

      if (entry.featured) {
        assert.equal(typeof entry.eventLabel, "string", `featured entry needs an event label`);
        assert.ok(entry.eventLabel.trim(), `featured entry needs a non-empty event label`);
        featuredEntries.push(`${date}|${shift}|${entry.name}|${entry.eventLabel}`);
      } else {
        assert.equal(
          entry.eventLabel,
          undefined,
          `non-featured entry has an event label on ${date} ${shift}`
        );
      }
    }
  }
}

assert.deepEqual(
  featuredEntries,
  [
    "2026-09-01|夜|もなか|1周年",
    "2026-09-03|昼|ちま|生誕",
    "2026-09-08|昼|あらた|7周年",
    "2026-09-08|夜|あらた|7周年",
    "2026-09-13|夜|あくび|生誕"
  ],
  "featured event entries do not match the confirmed schedule"
);

assert.ok(
  data.schedule["2026-09-01"]["夜"].some((entry) => entry.name === "こえび"),
  "Sep 1 night must include こえび's corrected shift"
);

function shiftsFor(name) {
  return Object.entries(data.schedule).flatMap(([date, day]) =>
    [...validShifts]
      .filter((shift) => day[shift].some((entry) => entry.name === name))
      .map((shift) => `${date}|${shift}`)
  );
}

assert.deepEqual(
  shiftsFor("あめる"),
  [
    "2026-09-02|昼",
    "2026-09-02|夜",
    "2026-09-04|昼",
    "2026-09-04|夜",
    "2026-09-05|夜",
    "2026-09-06|夜",
    "2026-09-08|夜",
    "2026-09-09|昼",
    "2026-09-11|夜",
    "2026-09-12|夜",
    "2026-09-13|昼",
    "2026-09-13|夜",
    "2026-09-14|夜"
  ],
  "あめる's shifts do not match the verified schedule and corrections"
);

assert.deepEqual(
  shiftsFor("ちさと"),
  [
    "2026-09-02|昼",
    "2026-09-05|昼",
    "2026-09-08|昼",
    "2026-09-09|夜",
    "2026-09-10|夜",
    "2026-09-15|昼"
  ],
  "ちさと's shifts do not match the verified schedule"
);

assert.deepEqual(
  shiftsFor("けだま"),
  [
    "2026-09-01|昼",
    "2026-09-02|昼",
    "2026-09-05|夜",
    "2026-09-07|昼",
    "2026-09-08|昼",
    "2026-09-10|昼",
    "2026-09-12|昼",
    "2026-09-14|昼"
  ],
  "けだま's shifts do not match the schedule published in her X profile"
);

assert.deepEqual(
  shiftsFor("ひかり"),
  [
    "2026-09-02|昼",
    "2026-09-02|夜",
    "2026-09-03|夜",
    "2026-09-08|昼",
    "2026-09-11|夜",
    "2026-09-12|昼",
    "2026-09-14|夜",
    "2026-09-15|夜"
  ],
  "ひかり's shifts do not match her published 9月前半 post"
);

// --- キャッシュ避け -----------------------------------------------------
// GitHub Pages は max-age=600 を返すので、読み込みにハッシュが付いていないと、
// データを更新してもブラウザーは古いファイルを使い続ける。実際に9月1日と2日の
// 実績を反映したあと、画面には予測が出たままだった。
// ハッシュは tools/build-insights.py が中身から付け直す。
{
  const indexPath = path.join(__dirname, "..", "index.html");
  const html = fs.readFileSync(indexPath, "utf8");
  const refs = [...html.matchAll(/(?:href|src)="([^"]+\.(?:js|css))(\?v=([0-9a-f]+))?"/g)];
  assert.ok(refs.length >= 4, "index.html must load the stylesheet, both data files and the app");

  for (const [, file, , stamp] of refs) {
    assert.ok(stamp, `${file} is loaded without a version, so a stale copy can survive a deploy`);
    const onDisk = path.join(__dirname, "..", ...file.split("/"));
    assert.ok(fs.existsSync(onDisk), `index.html loads ${file}, which is not in the repository`);
    const digest = crypto.createHash("sha256").update(fs.readFileSync(onDisk)).digest("hex");
    assert.equal(
      stamp,
      digest.slice(0, stamp.length),
      `${file} has changed since index.html was stamped; run tools/build-insights.py`
    );
  }
}

console.log(
  `Schedule valid: ${data.roster.length} rostered maids ` +
    `(${kitchenStaff.length} kitchen) in official site order, ` +
    `posted across ${[...posted].filter(([, count]) => count > 0).length} stores, ` +
    `${Object.keys(data.schedule).length} scheduled dates, ${featuredEntries.length} featured shifts.`
);
