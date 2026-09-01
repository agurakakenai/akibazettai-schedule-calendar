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


// 予測モードでは、店舗ごとに見出しを付けてまとめる。
// 一本のリストに混ぜると掲載順が飛ぶので、並びが崩れて見える。
const rosterIndex = new Map(schedule.roster.map((name, index) => [name, index]));
const storeOrder = new Map(insights.stores.map((store, index) => [store.id, index]));
let groupedSections = 0;

for (const section of shiftSections) {
  const labels = withClass(section, "maid-group-label");
  const lists = withClass(section, "maid-list");
  if (withClass(section, "maid-entry").length === 0) {
    continue;
  }
  groupedSections += 1;
  assert.equal(labels.length, lists.length, "every group must have a heading");

  let previousRank = -1;
  labels.forEach((label, index) => {
    const storeId = label.dataset.store;
    assert.ok(storeIds.has(storeId), `unknown store heading "${storeId}"`);
    const rank = storeOrder.get(storeId);
    assert.ok(rank > previousRank, "shop headings must follow the shop order");
    previousRank = rank;

    const members = withClass(lists[index], "maid-entry");
    assert.ok(members.length > 0, "a shop heading must have maids under it");
    assert.ok(
      label.textContent.includes(`${members.length}人`),
      "the heading must count the maids under it"
    );

    let previousRoster = -1;
    for (const member of members) {
      const name = withClass(member, "maid-name")[0].textContent;
      const position = rosterIndex.get(name);
      assert.ok(
        position > previousRoster,
        `${storeId}: ${name} breaks the official roster order`
      );
      previousRoster = position;

      const chip = withClass(member, "maid-store-chip")[0];
      assert.equal(
        chip.dataset.store,
        storeId,
        `${name} sits under ${storeId} so her chip must agree`
      );
    }
  });
}
assert.ok(groupedSections > 0, "some shifts must render grouped line-ups");

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

// カレンダーの下に解説ブロックは出さない。根拠は README にまとめてある。
assert.equal(
  walk(elementById("calendar")).filter((node) =>
    node.classList && (node.classList.contains("insight-notes") || node.classList.contains("unlisted-maids"))
  ).length,
  0,
  "the long explanation block must stay out of the page"
);

// --- キッチンにゃんこを隠す ---------------------------------------------
const kitchenNames = new Set(schedule.kitchenStaff);
const kitchenBefore = withClass(calendar, "maid-entry").filter((entry) =>
  kitchenNames.has(withClass(entry, "maid-name")[0].textContent)
).length;
assert.ok(kitchenBefore > 0, "the default range must show some kitchen staff");

const chipsBeforeHiding = withClass(calendar, "store-chip").length;
const assignedBeforeHiding = withClass(calendar, "maid-store-chip").map((chip) => chip.dataset.store);

elementById("hide-kitchen").checked = true;
dispatch("hide-kitchen", "change");

assert.equal(
  withClass(calendar, "maid-entry").filter((entry) =>
    kitchenNames.has(withClass(entry, "maid-name")[0].textContent)
  ).length,
  0,
  "checking the box must hide every kitchen maid"
);
assert.equal(
  withClass(calendar, "maid-entry").length,
  maidEntries.length - kitchenBefore,
  "only the kitchen maids may disappear"
);
assert.equal(
  withClass(calendar, "store-chip").length,
  chipsBeforeHiding,
  "hiding the kitchen must not change the store outlook"
);

// 隠しても割り振りは動かない。残った人の店は同じまま。
const stillShown = withClass(calendar, "maid-entry").map((entry) => ({
  name: withClass(entry, "maid-name")[0].textContent,
  store: withClass(entry, "maid-store-chip")[0]?.dataset.store
}));
assert.ok(
  stillShown.every((entry) => assignedBeforeHiding.includes(entry.store)),
  "the remaining maids must keep the shops they were already assigned to"
);

// フィルター側でもキッチンは操作できなくなる。
const mutedLabels = withClass(elementById("maid-checkboxes"), "is-muted");
assert.equal(mutedLabels.length, kitchenNames.size, "every kitchen row must be greyed out");

elementById("hide-kitchen").checked = false;
dispatch("hide-kitchen", "change");
assert.equal(
  withClass(calendar, "maid-entry").length,
  maidEntries.length,
  "unchecking the box must bring the kitchen maids back"
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
// 実績は日ごとに伸びるので、月を決め打ちしない。記録の最終日を含む月まで動かす。
const lastActual = Object.keys(insights.actual).sort().at(-1);
const shiftDate = (key, days) => {
  const date = new Date(`${key}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
};
const monthOf = (key) => key.slice(0, 7);

elementById("date-from").value = shiftDate(lastActual, -5);
dispatch("date-from", "change");
elementById("date-to").value = shiftDate(lastActual, 2);
dispatch("date-to", "change");

// カレンダーは schedule.js の initialMonth から始まる。記録の月まで前後させる。
const monthsBetween = (from, to) => {
  const [fy, fm] = from.split("-").map(Number);
  const [ty, tm] = to.split("-").map(Number);
  return (ty - fy) * 12 + (tm - fm);
};
let steps = monthsBetween(monthOf(schedule.initialMonth), monthOf(lastActual));
while (steps < 0) {
  dispatch("previous-month", "click");
  steps += 1;
}
while (steps > 0) {
  dispatch("next-month", "click");
  steps -= 1;
}

const recordMonthBadges = withClass(calendar, "calendar-day").map((cell) => ({
  day: withClass(cell, "day-number")[0].textContent,
  badges: withClass(cell, "store-status-badge").map((badge) => badge.textContent)
}));
const rendered = new Set(recordMonthBadges.flatMap((cell) => cell.badges));
assert.ok(rendered.has("実績"), "the recorded window must render 実績 badges");

const dayLabel = (key) => String(Number(key.slice(-2)));
const lastActualCell = recordMonthBadges.find((cell) => cell.day === dayLabel(lastActual));
assert.ok(lastActualCell, `${lastActual} must be visible in the selected range`);
// 最新の記録日は片シフトだけのことがある（その日の夜がまだ来ていない等）。
const recordedShifts = Object.keys(insights.actual[lastActual]).length;
assert.equal(
  lastActualCell.badges.filter((badge) => badge === "実績").length,
  recordedShifts,
  `${lastActual} has ${recordedShifts} recorded shift(s), so that many 実績 badges are correct`
);

const forecastKey = shiftDate(lastActual, 1);
if (monthOf(forecastKey) === monthOf(lastActual)) {
  const forecastCell = recordMonthBadges.find((cell) => cell.day === dayLabel(forecastKey));
  assert.ok(forecastCell, `${forecastKey} must be visible in the selected range`);
  // 見込みは前日の同じシフトの記録から出す。前日が片シフトだけなら、
  // もう片方は前日を引けないので曜日傾向に落ちる。
  assert.equal(
    forecastCell.badges.filter((badge) => badge === "翌日見込み").length,
    recordedShifts,
    `${forecastKey} must forecast exactly the ${recordedShifts} shift(s) recorded the day before`
  );
}

// 片方のシフトしか記録が無い日は、記録のある側だけが 実績 になる。
const partialDate = Object.keys(insights.actual).find(
  (date) =>
    monthOf(date) === monthOf(lastActual) &&
    date >= shiftDate(lastActual, -5) &&
    Object.keys(insights.actual[date]).length === 1
);
if (partialDate) {
  const cell = recordMonthBadges.find((entry) => entry.day === dayLabel(partialDate));
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
    "and no explanation block below the calendar."
);
