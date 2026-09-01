"use strict";

/* 最小の DOM シムでカレンダーを実際に描画し、ランタイムエラーと描画結果を検証する。
   既存のテストは純関数しか通らないため、IIFE が走ったあとの DOM はここでしか確認できない。
   依存パッケージは無し。第1引数で別のチェックアウトも指定できる。 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const repo = process.argv[2] || path.join(__dirname, "..");
const listeners = [];

function makeClassList() {
  const set = new Set();
  return {
    _set: set,
    add: (...names) => names.forEach((name) => name && set.add(name)),
    remove: (...names) => names.forEach((name) => set.delete(name)),
    contains: (name) => set.has(name),
    get value() {
      return [...set].join(" ");
    }
  };
}

function makeElement(tagName = "div") {
  const element = {
    tagName: String(tagName).toUpperCase(),
    children: [],
    attributes: {},
    dataset: {},
    style: {},
    _text: "",
    hidden: false,
    checked: false,
    value: ""
  };
  element.classList = makeClassList();
  element.appendChild = (child) => {
    if (child && child._isFragment) {
      child.children.forEach((node) => element.children.push(node));
      child.children = [];
    } else if (child) {
      element.children.push(child);
    }
    return child;
  };
  element.append = (...nodes) => nodes.forEach((node) => element.appendChild(node));
  element.replaceChildren = (...nodes) => {
    element.children = [];
    element.append(...nodes);
  };
  element.setAttribute = (key, value) => {
    element.attributes[key] = String(value);
  };
  element.getAttribute = (key) => (key in element.attributes ? element.attributes[key] : null);
  element.addEventListener = (type, fn) => listeners.push({ element, type, fn });
  element.querySelector = () => null;
  element.querySelectorAll = () => [];
  Object.defineProperty(element, "textContent", {
    get() {
      return element._text || element.children.map((child) => child.textContent ?? "").join("");
    },
    set(value) {
      element._text = String(value ?? "");
      element.children = [];
    }
  });
  Object.defineProperty(element, "className", {
    get() {
      return element.classList.value;
    },
    set(value) {
      element.classList._set.clear();
      String(value ?? "")
        .split(/\s+/)
        .filter(Boolean)
        .forEach((name) => element.classList._set.add(name));
    }
  });
  return element;
}

// app.js は querySelector("#id") で要素を引くので、id は必ず同じインスタンスに解決する。
const byId = new Map();
function elementById(id) {
  if (!byId.has(id)) {
    byId.set(id, makeElement("div"));
  }
  return byId.get(id);
}

const documentShim = {
  createElement: makeElement,
  createDocumentFragment: () => {
    const fragment = makeElement("#fragment");
    fragment._isFragment = true;
    return fragment;
  },
  getElementById: elementById,
  querySelector: (selector) => {
    const match = /^#([\w-]+)$/.exec(String(selector).trim());
    assert.ok(match, `the shim only resolves id selectors, got "${selector}"`);
    return elementById(match[1]);
  },
  querySelectorAll: () => [],
  addEventListener: (type, fn) => listeners.push({ element: "document", type, fn }),
  documentElement: makeElement("html"),
  body: makeElement("body")
};

const windowShim = {
  addEventListener: (type, fn) => listeners.push({ element: "window", type, fn }),
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  location: { hash: "", search: "" }
};

const sandbox = {
  window: windowShim,
  document: documentShim,
  console,
  Intl,
  Date,
  Math,
  JSON,
  Set,
  Map,
  Array,
  Object,
  String,
  Number,
  Boolean,
  navigator: { language: "ja-JP" }
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

function run(relativePath) {
  const file = path.join(repo, relativePath);
  vm.runInContext(fs.readFileSync(file, "utf8"), sandbox, { filename: relativePath });
}

run("data/schedule.js");
run("data/store-insights.js");
run("app.js");

const insights = sandbox.window.STORE_INSIGHTS;
const schedule = sandbox.window.SCHEDULE_DATA;

function dispatch(id, type) {
  const target = elementById(id);
  const matched = listeners.filter((entry) => entry.element === target && entry.type === type);
  assert.ok(matched.length > 0, `#${id} has no "${type}" listener`);
  matched.forEach((entry) => entry.fn({ type, target }));
}

function walk(node, out = []) {
  if (!node || !node.children) {
    return out;
  }
  out.push(node);
  node.children.forEach((child) => walk(child, out));
  return out;
}

const withClass = (root, name) =>
  walk(root).filter((node) => node.classList && node.classList.contains(name));

const calendar = elementById("calendar");

// --- カレンダー本体 -----------------------------------------------------
const dayCells = withClass(calendar, "calendar-day");
assert.ok(dayCells.length > 0, "the calendar must render day cells");
assert.equal(
  withClass(calendar, "weekday").length,
  7,
  "the calendar must render a 7-column weekday header"
);

const shiftSections = withClass(calendar, "shift-section");
assert.equal(
  shiftSections.length,
  dayCells.length * insights.shifts.length,
  `each day must render ${insights.shifts.length} shift sections`
);

const knownBadges = new Set(["実績", "見込み（翌日）", "曜日傾向"]);
const storeIds = new Set(insights.stores.map((store) => store.id));

for (const section of shiftSections) {
  const badges = withClass(section, "store-status-badge");
  assert.equal(badges.length, 1, "each shift section must carry exactly one badge");
  assert.ok(
    knownBadges.has(badges[0].textContent),
    `unexpected badge "${badges[0].textContent}"`
  );
  assert.equal(
    badges[0].getAttribute("aria-hidden"),
    "true",
    "the badge is decorative; the spoken summary carries the same information"
  );

  const outlooks = withClass(section, "store-outlook");
  assert.equal(outlooks.length, 1, "each shift section must carry one store outlook");
  assert.ok(outlooks[0].title, "the outlook must expose its reasoning as a tooltip");

  const spoken = withClass(outlooks[0], "visually-hidden");
  assert.equal(spoken.length, 1, "the outlook must expose exactly one spoken summary");
  assert.ok(
    spoken[0].textContent.includes(badges[0].textContent),
    "the spoken summary must state which basis was used"
  );

  const chipLists = withClass(section, "store-chips");
  assert.equal(chipLists.length, 1, "each shift section must carry one chip list");
  assert.equal(
    chipLists[0].getAttribute("aria-hidden"),
    "true",
    "chips duplicate the spoken summary, so they must be hidden from assistive tech"
  );

  const chips = withClass(section, "store-chip");
  assert.equal(chips.length, insights.stores.length, "every store must get a chip");
  chips.forEach((chip, index) => {
    assert.equal(
      chip.dataset.store,
      insights.stores[index].id,
      "chips must stay in store order so position identifies the store"
    );
    assert.ok(storeIds.has(chip.dataset.store), `unknown store id "${chip.dataset.store}"`);
    assert.ok(
      /^is-(open|closed|likely|unlikely)$/.test(
        chip.className.split(/\s+/).find((name) => name.startsWith("is-")) ?? ""
      ),
      `chip must carry a state class, got "${chip.className}"`
    );
    const text = chip.textContent;
    assert.ok(
      /^\d号(営業|休み|\d+%)$/.test(text),
      `chip text must be a store label plus a state or a rate, got "${text}"`
    );
  });

  // 実績のシフトは営業/休み、それ以外は割合。混在しない。
  const states = chips.map((chip) => (chip.classList.contains("is-open") || chip.classList.contains("is-closed")));
  const isRecord = badges[0].textContent === "実績";
  assert.ok(
    states.every((isRecordState) => isRecordState === isRecord),
    "a shift must not mix recorded states with probabilities"
  );
}

// --- メイドさんの行 -----------------------------------------------------
const roster = new Set(schedule.roster);
const maidEntries = withClass(calendar, "maid-entry");
assert.ok(maidEntries.length > 0, "the default range must render some maids");

for (const entry of maidEntries) {
  const names = withClass(entry, "maid-name");
  assert.equal(names.length, 1, "each maid entry must render exactly one name");
  assert.ok(roster.has(names[0].textContent), `unknown maid "${names[0].textContent}"`);

  const chips = withClass(entry, "maid-store-chip");
  assert.ok(chips.length <= 1, "a maid entry must not render more than one store chip");
  if (chips.length === 1) {
    assert.ok(storeIds.has(chips[0].dataset.store), "the maid chip must name a known store");
    assert.equal(
      chips[0].getAttribute("aria-hidden"),
      "true",
      "the maid chip is covered by the entry's aria-label"
    );
    assert.ok(entry.title, "a maid entry with a chip must explain it in a tooltip");
    assert.ok(
      entry.getAttribute("aria-label"),
      "a maid entry with a chip must expose the same information to screen readers"
    );
    assert.ok(
      entry.getAttribute("aria-label").startsWith(names[0].textContent),
      "the aria-label must lead with the maid's name"
    );
  }
}

const linkedNames = withClass(calendar, "maid-name").filter((node) => node.tagName === "A");
assert.ok(linkedNames.length > 0, "maids with a known X account must link to it");
for (const link of linkedNames) {
  assert.match(link.href, /^https:\/\/x\.com\/[A-Za-z0-9_]{1,15}$/, "malformed X link");
  assert.equal(link.rel, "noopener noreferrer", "external links must not leak the opener");
}

const featured = maidEntries.filter((entry) => entry.classList.contains("is-featured"));
assert.ok(featured.length > 0, "the September range still contains featured shifts");
for (const entry of featured) {
  assert.ok(
    entry.getAttribute("aria-label").includes("主役"),
    "a featured maid must keep her event description"
  );
}

// --- 凡例と注記 ---------------------------------------------------------
const notes = elementById("insight-notes");
assert.equal(notes.hidden, false, "the notes must be revealed once the data loads");
assert.equal(withClass(notes, "insight-legend").length, 1, "the notes must carry one legend");
assert.equal(
  withClass(notes, "insight-legend")[0].children.length,
  knownBadges.size,
  "the legend must explain every badge"
);

const notesText = walk(notes)
  .map((node) => node._text || "")
  .join("\n");
for (const badge of knownBadges) {
  assert.ok(notesText.includes(badge), `the legend must mention the "${badge}" badge`);
}
assert.ok(
  notesText.includes("2日以上先"),
  "the notes must say that anything past tomorrow is not a forecast"
);
assert.ok(
  notesText.includes("@akibazettai"),
  "the notes must credit where the data came from"
);

const unlistedLists = withClass(notes, "unlisted-maids");
assert.ok(unlistedLists.length > 0, "the notes must list the members missing from the roster");

const activeList = unlistedLists[0];
const activeEntries = activeList.children;
const activeNames = activeEntries.map((item) => withClass(item, "unlisted-name")[0].textContent);
const graduatedNames = Object.entries(insights.unlistedMaids)
  .filter(([, info]) => info.status === "graduated")
  .map(([name]) => name);
const promotedNames = Object.entries(insights.unlistedMaids)
  .filter(([, info]) => info.promoted)
  .map(([name]) => name);

for (const name of promotedNames) {
  assert.ok(activeNames.includes(name), `${name} is promoted, so she belongs in the active list`);
  const item = activeEntries[activeNames.indexOf(name)];
  const link = withClass(item, "unlisted-name")[0];
  assert.equal(link.tagName, "A", `${name} has a confirmed account, so her name must link to it`);
  assert.equal(link.href, `https://x.com/${insights.unlistedMaids[name].x}`, "wrong X link");
}

for (const name of graduatedNames) {
  assert.ok(
    !activeNames.includes(name),
    `${name} has graduated and must not be listed as currently working`
  );
}

if (graduatedNames.length > 0) {
  assert.equal(unlistedLists.length, 2, "graduated members must be listed separately");
  const graduatedEntries = unlistedLists[1].children;
  assert.equal(
    graduatedEntries.length,
    graduatedNames.length,
    "every graduated member must appear in the second list"
  );
  for (const item of graduatedEntries) {
    assert.ok(item.classList.contains("is-graduated"), "graduated entries must be marked");
    assert.equal(
      withClass(item, "unlisted-name")[0].tagName,
      "SPAN",
      "a graduated member must not be linked as if she were still working"
    );
  }
}

// 「新人かも」と言い切ってよいのは likelyNew だけ。
for (const [name, info] of Object.entries(insights.unlistedMaids)) {
  if (!activeNames.includes(name)) {
    continue;
  }
  const item = activeEntries[activeNames.indexOf(name)];
  const detail = withClass(item, "unlisted-detail")[0].textContent;
  assert.equal(
    detail.includes("新人"),
    Boolean(info.likelyNew),
    `${name} must only be called new when likelyNew is set`
  );
}

// --- 絞り込みを変えても店舗の見込みは残る -------------------------------
const beforeChips = withClass(calendar, "store-chip").length;
dispatch("clear-all", "click");
assert.equal(
  withClass(calendar, "maid-entry").length,
  0,
  "clearing the maid filter must hide every maid"
);
assert.equal(
  withClass(calendar, "store-chip").length,
  beforeChips,
  "the store outlook must not depend on the maid filter"
);

dispatch("select-all", "click");
assert.equal(
  withClass(calendar, "maid-entry").length,
  maidEntries.length,
  "re-selecting every maid must restore the original entries"
);

// --- 実績・見込みの月へ移動する -----------------------------------------
const lastActual = Object.keys(insights.actual).sort().at(-1);
elementById("date-from").value = "2026-08-26";
dispatch("date-from", "change");
elementById("date-to").value = "2026-09-02";
dispatch("date-to", "change");
dispatch("previous-month", "click");

const augustBadges = withClass(calendar, "calendar-day").map((cell) => ({
  day: withClass(cell, "day-number")[0].textContent,
  badges: withClass(cell, "store-status-badge").map((badge) => badge.textContent)
}));
const rendered = new Set(augustBadges.flatMap((cell) => cell.badges));
assert.ok(rendered.has("実績"), "the recorded window must render 実績 badges");
assert.ok(rendered.has("見込み（翌日）"), "the day after the last record must render a forecast");
assert.ok(rendered.has("曜日傾向"), "a shift with no record must fall back to the weekday tendency");

const lastActualDay = String(Number(lastActual.slice(-2)));
const forecastDay = String(Number(lastActual.slice(-2)) + 1);
assert.deepEqual(
  augustBadges.find((cell) => cell.day === lastActualDay).badges,
  ["実績", "実績"],
  `${lastActual} is fully recorded, so both shifts must show 実績`
);
assert.deepEqual(
  augustBadges.find((cell) => cell.day === forecastDay).badges,
  ["見込み（翌日）", "見込み（翌日）"],
  "the day after the last record must be a forecast on both shifts"
);

// 片方のシフトしか記録が無い日は、記録のある側だけが 実績 になる。
const partialDate = Object.keys(insights.actual).find(
  (date) => date.startsWith("2026-08-2") && Object.keys(insights.actual[date]).length === 1
);
if (partialDate) {
  const cell = augustBadges.find((entry) => entry.day === String(Number(partialDate.slice(-2))));
  assert.ok(cell, `${partialDate} must be visible in the selected range`);
  assert.equal(
    cell.badges.filter((badge) => badge === "実績").length,
    1,
    `${partialDate} has one recorded shift, so exactly one 実績 badge is correct`
  );
  assert.equal(
    cell.badges.filter((badge) => badge === "曜日傾向").length,
    1,
    `${partialDate}'s missing shift must fall back instead of being called closed`
  );
}

console.log(
  `Headless render valid: ${dayCells.length} day cells, ${shiftSections.length} shift sections, ` +
    `${maidEntries.length} maid entries, ${activeNames.length} unlisted members listed as active ` +
    `(${graduatedNames.length} graduated kept separate).`
);
