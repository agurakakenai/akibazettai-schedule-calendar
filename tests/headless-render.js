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

// index.html に実際にある id と radio を読み、シムがそれ以外を黙って作らないようにする。
const markup = fs.readFileSync(path.join(repo, "index.html"), "utf8");
const declaredIds = new Set([...markup.matchAll(/\sid="([\w-]+)"/g)].map((match) => match[1]));
const viewModeValues = [...markup.matchAll(/<input[^>]*name="view-mode"[^>]*>/g)].map((tag) => {
  const value = /value="([\w-]+)"/.exec(tag[0]);
  assert.ok(value, `a view-mode radio in index.html has no value: ${tag[0]}`);
  return value[1];
});
assert.ok(viewModeValues.length >= 2, "index.html must offer at least two view modes");

const byId = new Map();
function elementById(id) {
  if (!byId.has(id)) {
    byId.set(id, makeElement("div"));
  }
  return byId.get(id);
}

const viewModeInputs = viewModeValues.map((value) => {
  const input = makeElement("input");
  input.value = value;
  input.attributes.name = "view-mode";
  return input;
});

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
    assert.ok(
      declaredIds.has(match[1]),
      `app.js looks up #${match[1]}, which does not exist in index.html`
    );
    return elementById(match[1]);
  },
  querySelectorAll: (selector) => {
    assert.equal(
      String(selector).trim(),
      'input[name="view-mode"]',
      `the shim does not implement "${selector}"`
    );
    return viewModeInputs;
  },
  addEventListener: (type, fn) => listeners.push({ element: "document", type, fn }),
  documentElement: makeElement("html"),
  body: makeElement("body")
};

const windowShim = {
  addEventListener: (type, fn) => listeners.push({ element: "window", type, fn }),
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  localStorage: {
    _store: new Map(),
    getItem(key) {
      return this._store.has(key) ? this._store.get(key) : null;
    },
    setItem(key, value) {
      this._store.set(key, String(value));
    },
    removeItem(key) {
      this._store.delete(key);
    }
  },
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

function selectViewMode(mode) {
  const input = viewModeInputs.find((candidate) => candidate.value === mode);
  assert.ok(input, `index.html has no "${mode}" view mode`);
  viewModeInputs.forEach((candidate) => {
    candidate.checked = candidate === input;
  });
  const matched = listeners.filter((entry) => entry.element === input && entry.type === "change");
  assert.ok(matched.length > 0, `the "${mode}" radio has no change listener`);
  matched.forEach((entry) => entry.fn({ type: "change", target: input }));
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

const knownBadges = new Set(["実績", "翌日見込み", "曜日傾向"]);
const storeIds = new Set(insights.stores.map((store) => store.id));

// 記念日の主役はかならず所属店に立つので、その店だけは見込みの日でも「営業」で確定する。
function hostingStores(dateKey, shift) {
  const entries = schedule.schedule[dateKey]?.[shift] ?? [];
  return new Set(
    entries
      .filter((entry) => entry.featured)
      .map((entry) => insights.maidTendency[entry.name]?.home)
      .filter(Boolean)
  );
}

for (const cell of dayCells) {
  const label = cell.getAttribute("aria-label");
  const [, year, month, day] = /(\d+)年(\d+)月(\d+)日/.exec(label);
  const cellKey = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  const sections = withClass(cell, "shift-section");
  assert.equal(sections.length, insights.shifts.length, `${cellKey} must render both shifts`);

  sections.forEach((section, shiftIndex) => {
    const shift = insights.shifts[shiftIndex];
    const hosts = hostingStores(cellKey, shift);
    const badges = withClass(section, "store-status-badge");
    assert.equal(badges.length, 1, "each shift section must carry exactly one badge");
    assert.ok(knownBadges.has(badges[0].textContent), `unexpected badge "${badges[0].textContent}"`);
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

    const isRecord = badges[0].textContent === "実績";
    chips.forEach((chip, index) => {
      const storeId = insights.stores[index].id;
      assert.equal(
        chip.dataset.store,
        storeId,
        "chips must stay in store order so position identifies the store"
      );
      assert.ok(storeIds.has(chip.dataset.store), `unknown store id "${chip.dataset.store}"`);
      assert.ok(
        chip.title && chip.title.includes(`${insights.headcountProfile[shift][storeId].mode}人態勢`),
        `${storeId} must explain how many staff it usually runs`
      );

      const state = chip.className.split(/\s+/).find((name) => name.startsWith("is-")) ?? "";
      assert.ok(
        /^is-(open|closed|likely|unlikely)$/.test(state),
        `chip must carry a state class, got "${chip.className}"`
      );
      assert.ok(
        /^\d号(営業|休み|\d+%)$/.test(chip.textContent),
        `chip text must be a store label plus a state or a rate, got "${chip.textContent}"`
      );

      const settled = state === "is-open" || state === "is-closed";
      if (isRecord) {
        assert.ok(settled, `${cellKey} ${shift} is recorded, so ${storeId} must be settled`);
      } else if (hosts.has(storeId)) {
        assert.equal(
          state,
          "is-open",
          `${cellKey} ${shift} hosts an event at ${storeId}, so it must be certain to be open`
        );
      } else {
        assert.ok(
          !settled,
          `${cellKey} ${shift} is not recorded, so ${storeId} must stay a probability`
        );
      }
    });

    // 主役は自分の所属店に割り振られ、確率ではなく確定として出る。
    for (const entry of schedule.schedule[cellKey]?.[shift] ?? []) {
      if (!entry.featured) {
        continue;
      }
      const row = withClass(section, "maid-entry").find(
        (item) => withClass(item, "maid-name")[0].textContent === entry.name
      );
      assert.ok(row, `${entry.name} must be rendered on ${cellKey} ${shift}`);
      const chip = withClass(row, "maid-store-chip")[0];
      assert.ok(chip, `${entry.name} hosts ${entry.eventLabel}, so her store must be shown`);
      assert.equal(
        chip.dataset.store,
        insights.maidTendency[entry.name].home,
        `${entry.name} must be placed at her own store on ${cellKey} ${shift}`
      );
      assert.ok(
        row.title.includes(entry.eventLabel),
        "the tooltip must say the event is what fixes her store"
      );
    }
  });
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

// 1人ずつ独立に決めると全員が1号店になるので、複数店に割れることを確かめる。
// ただし少人数のシフトは1店で収まるのが正しいので、標準人数を超えた場合だけ2店以上を要求する。
let splitSections = 0;
let forcedSections = 0;
for (const cell of dayCells) {
  const [, year, month, day] = /(\d+)年(\d+)月(\d+)日/.exec(cell.getAttribute("aria-label"));
  const cellKey = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  withClass(cell, "shift-section").forEach((section, shiftIndex) => {
    const shift = insights.shifts[shiftIndex];
    const assigned = withClass(section, "maid-store-chip").map((chip) => chip.dataset.store);
    if (assigned.length === 0) {
      return;
    }
    if (new Set(assigned).size > 1) {
      splitSections += 1;
    }
    const biggestShop = Math.max(...Object.values(insights.typicalHeadcount[shift]));
    const scheduled = (schedule.schedule[cellKey]?.[shift] ?? []).length;
    if (scheduled > biggestShop) {
      forcedSections += 1;
      assert.ok(
        new Set(assigned).size > 1,
        `${cellKey} ${shift} has ${scheduled} maids, more than any single shop holds, so it must open more than one`
      );
    }
  });
}
assert.ok(forcedSections > 0, "the schedule must contain a shift too big for a single shop");
assert.ok(splitSections > 0, "some shifts must spread across stores");

const allAssigned = withClass(calendar, "maid-store-chip").map((chip) => chip.dataset.store);
assert.ok(
  new Set(allAssigned).size >= 2,
  "the calendar must not send every maid to the same store"
);

// 開く店舗の数は日によって変わる。最頻値で固定すると3店舗の日も1店舗の日も出せない。
const shopCounts = new Set();
for (const cell of dayCells) {
  withClass(cell, "shift-section").forEach((section) => {
    const assigned = withClass(section, "maid-store-chip").map((chip) => chip.dataset.store);
    if (assigned.length > 0) {
      shopCounts.add(new Set(assigned).size);
    }
  });
}
assert.ok(
  shopCounts.size >= 2,
  `the calendar must not use the same shop count everywhere, got ${[...shopCounts].join(",")}`
);
for (const count of shopCounts) {
  assert.ok(
    (insights.openCountPerShift["昼"][String(count)] ?? 0) +
      (insights.openCountPerShift["夜"][String(count)] ?? 0) > 0,
    `${count} shops open at once has never actually happened`
  );
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

// 見習いは事前に分からない、という注意が数値つきで出ていること。
const coverageNote = withClass(notes, "insight-headline");
assert.equal(coverageNote.length, 1, "the notes must carry the roster-coverage warning");
const coverageText = coverageNote[0].textContent;
assert.ok(
  coverageText.includes("事前に知る手段がありません"),
  "the warning must say trainees cannot be known in advance"
);
assert.ok(
  coverageText.includes(String(insights.rosterCoverage.unlistedPerShift)),
  "the warning must quote the measured per-shift gap rather than a hardcoded number"
);
assert.ok(
  coverageText.includes(
    `${Math.round(insights.rosterCoverage.shiftsWithUnlisted * 100)}%`
  ),
  "the warning must quote the measured share of affected shifts"
);
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

// 予測モードでは、同じ店に入りそうな人がまとまって並ぶ。
const storeOrder = new Map(insights.stores.map((store, index) => [store.id, index]));
for (const section of shiftSections) {
  const assigned = withClass(section, "maid-entry")
    .map((entry) => withClass(entry, "maid-store-chip")[0]?.dataset.store)
    .filter(Boolean)
    .map((id) => storeOrder.get(id));
  const sorted = [...assigned].sort((a, b) => a - b);
  assert.deepEqual(
    assigned,
    sorted,
    "maids must be listed in store order so each shop's line-up reads together"
  );
}

// --- 表示モードの切り替え -----------------------------------------------
assert.equal(
  withClass(calendar, "store-chip").length,
  shiftSections.length * insights.stores.length,
  "the forecast mode must show the store outlook"
);

selectViewMode("roster");
assert.equal(
  withClass(calendar, "store-chip").length,
  0,
  "the roster mode must not show any store outlook"
);
assert.equal(
  withClass(calendar, "store-status-badge").length,
  0,
  "the roster mode must not show the basis badges"
);
assert.equal(
  withClass(calendar, "maid-store-chip").length,
  0,
  "the roster mode must not guess where each maid will be"
);
assert.equal(
  elementById("insight-notes").hidden,
  true,
  "the forecast notes are meaningless in the roster mode"
);
assert.equal(
  withClass(calendar, "maid-entry").length,
  maidEntries.length,
  "switching modes must not change who is on the calendar"
);

// 素の並びは data/schedule.js の公式順に戻る。
const rosterOrder = new Map(schedule.roster.map((name, index) => [name, index]));
for (const cell of withClass(calendar, "calendar-day")) {
  for (const section of withClass(cell, "shift-section")) {
    const order = withClass(section, "maid-entry").map((entry) =>
      rosterOrder.get(withClass(entry, "maid-name")[0].textContent)
    );
    assert.deepEqual(
      order,
      [...order].sort((a, b) => a - b),
      "the roster mode must keep the official roster order"
    );
  }
}

selectViewMode("forecast");
assert.equal(
  withClass(calendar, "store-chip").length,
  shiftSections.length * insights.stores.length,
  "switching back must restore the store outlook"
);
assert.equal(
  elementById("insight-notes").hidden,
  false,
  "switching back must restore the notes"
);

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
assert.ok(rendered.has("翌日見込み"), "the day after the last record must render a forecast");
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
  ["翌日見込み", "翌日見込み"],
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
    `${maidEntries.length} maid entries sorted by assigned store, both view modes, ` +
    `${activeNames.length} unlisted members listed as active ` +
    `(${graduatedNames.length} graduated kept separate).`
);
