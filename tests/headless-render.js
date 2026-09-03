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

const knownBadges = new Set(["実績", "翌日見込み", "同日の実績", "曜日傾向"]);
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

    // 表に出るのは、開くと見込んだ店だけ（と僅差で落とした店）。4店ぶん並べても
    // どの店を開けるかは当日決まるので、読み手は選べない。割合は HTML に残る。
    const chips = withClass(section, "store-chip");
    assert.ok(chips.length > 0, "a shift must show at least one shop");
    assert.ok(
      chips.length <= insights.stores.length,
      "a shift cannot show more shops than exist"
    );
    const shownIds = chips.map((chip) => chip.dataset.store);
    assert.deepEqual(
      shownIds,
      [...insights.stores].map((store) => store.id).filter((id) => shownIds.includes(id)),
      "chips must stay in store order"
    );

    const isRecord = badges[0].textContent === "実績";
    chips.forEach((chip) => {
      const storeId = chip.dataset.store;
      assert.ok(storeIds.has(storeId), `unknown store id "${storeId}"`);
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

    // 表から消えた店の情報は、読み上げ用テキストに残っていること。
    const described = withClass(section, "visually-hidden")[0]?.textContent ?? "";
    for (const store of insights.stores) {
      assert.ok(
        described.includes(store.short),
        `${store.short} must still be described even when it has no chip`
      );
    }

    // 主役は自分の所属店に割り振られ、確率ではなく確定として出る。
    for (const entry of schedule.schedule[cellKey]?.[shift] ?? []) {
      if (!entry.featured) {
        continue;
      }
      const row = withClass(section, "maid-entry").find(
        (item) => withClass(item, "maid-name")[0].textContent === entry.name
      );
      assert.ok(row, `${entry.name} must be rendered on ${cellKey} ${shift}`);
      // 店は見出しが名乗る。見出しと同じ店ならチップは出さない（繰り返しになる）。
      const lists = withClass(section, "maid-list");
      const labels = withClass(section, "maid-group-label");
      const index = lists.findIndex((list) => withClass(list, "maid-entry").includes(row));
      assert.ok(index >= 0, `${entry.name} must sit in one of the shop groups`);
      assert.equal(
        labels[index].dataset.store,
        schedule.homeStore[entry.name],
        `${entry.name} must be placed at her own store on ${cellKey} ${shift}`
      );
      assert.ok(
        row.title.includes(entry.eventLabel),
        "the tooltip must say the event is what fixes her store"
      );
      assert.ok(
        row.title.includes("確定") || row.title.includes("所属店"),
        "the tooltip must say the event is what makes it certain"
      );
    }
  });
}

// --- メイドさんの行 -----------------------------------------------------
const roster = new Set(schedule.roster);
// 記録には、予定表に載らない方（見習いなど）も出てくる。名簿だけで照合すると
// 記録を出した日に落ちるので、記録に名前がある人も知っている人として扱う。
const recordedNames = new Set(
  Object.values(insights.actualRoster ?? {}).flatMap((day) =>
    Object.values(day).flatMap((entry) => Object.values(entry.stores).flat())
  )
);
const maidEntries = withClass(calendar, "maid-entry");
// 見込みの見習いにゃんこは名前を持たない。実在の顔ぶれを数えるときは外す。
const realMaidEntries = maidEntries.filter(
  (entry) => !entry.classList.contains("is-trainee-guess")
);
assert.ok(maidEntries.length > 0, "the default range must render some maids");

for (const entry of maidEntries) {
  const names = withClass(entry, "maid-name");
  assert.equal(names.length, 1, "each maid entry must render exactly one name");
  // 見習いにゃんこの見込みは、誰なのかが分からないので名前を名乗らない。
  if (entry.classList.contains("is-trainee-guess")) {
    continue;
  }
  assert.ok(
    roster.has(names[0].textContent) || recordedNames.has(names[0].textContent),
    `unknown maid "${names[0].textContent}"`
  );

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
// 割り振り先は見出しが名乗る（チップは見出しと違う店のときだけ出る）。
let splitSections = 0;
let forcedSections = 0;
for (const cell of dayCells) {
  const [, year, month, day] = /(\d+)年(\d+)月(\d+)日/.exec(cell.getAttribute("aria-label"));
  const cellKey = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  withClass(cell, "shift-section").forEach((section, shiftIndex) => {
    const shift = insights.shifts[shiftIndex];
    const assigned = withClass(section, "maid-group-label").map((label) => label.dataset.store);
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

const allAssigned = withClass(calendar, "maid-group-label").map((label) => label.dataset.store);
assert.ok(
  new Set(allAssigned).size >= 2,
  "the calendar must not send every maid to the same store"
);

// 開く店舗の数は日によって変わる。最頻値で固定すると3店舗の日も1店舗の日も出せない。
const shopCounts = new Set();
for (const cell of dayCells) {
  withClass(cell, "shift-section").forEach((section) => {
    const assigned = withClass(section, "maid-group-label").map((label) => label.dataset.store);
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

      // 見出しがその店を名乗っているので、チップは繰り返さない。
      assert.equal(
        withClass(member, "maid-store-chip").length,
        0,
        `${name} sits under ${storeId}, so her row must not repeat the shop`
      );
    }
  });
}
assert.ok(groupedSections > 0, "some shifts must render grouped line-ups");

// --- 表示モードの切り替え -----------------------------------------------
assert.ok(
  withClass(calendar, "store-chip").length > 0,
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
// 見込みの見習いにゃんこは予測なので、予測を隠すモードでは出ない。
// 実在のメイドさんの顔ぶれが変わっていないことだけを見る。
assert.equal(
  withClass(calendar, "maid-entry").filter((entry) => entry.classList.contains("is-trainee-guess")).length,
  0,
  "the roster mode must not guess at trainees it cannot name"
);
assert.equal(
  withClass(calendar, "maid-entry").length,
  realMaidEntries.length,
  "switching modes must not change who is on the calendar"
);

// 素の並びは data/schedule.js の公式順に戻る。
const rosterOrder = new Map(schedule.roster.map((name, index) => [name, index]));
for (const cell of withClass(calendar, "calendar-day")) {
  for (const section of withClass(cell, "shift-section")) {
    const order = withClass(section, "maid-entry").map(
      (entry) => rosterOrder.get(withClass(entry, "maid-name")[0].textContent) ?? Infinity
    );
    assert.deepEqual(
      order,
      [...order].sort((a, b) => a - b),
      "the roster mode must keep the official roster order"
    );
  }
}

selectViewMode("forecast");
assert.ok(
  withClass(calendar, "store-chip").length > 0,
  "switching back must restore the store outlook"
);

// --- 人 → 日付 → 店 のモード -------------------------------------------
// この画面は店ごとの画面と同じ割り振りを引く約束になっている。別々に計算すると
// 「カレンダーでは2号店、人の一覧では3号店」という食い違いが黙って出るので、
// 二つの画面から同じ (人・日付・シフト) を拾って突き合わせる。
const placementsByView = new Map();
for (const cell of withClass(calendar, "calendar-day")) {
  const [, , month, day] = /(\d+)年(\d+)月(\d+)日/.exec(cell.getAttribute("aria-label"));
  const stopKey = `${Number(month)}/${Number(day)}`;
  withClass(cell, "shift-section").forEach((section, shiftIndex) => {
    const shift = insights.shifts[shiftIndex];
    // 割り振り先は見出しが名乗る。チップは見出しと違う店のときしか出ない。
    // キッチンの一覧は店を名乗らないので、そこに入った時点で見出しを手放す。
    let current = null;
    for (const node of walk(section)) {
      if (!node.classList) {
        continue;
      }
      if (node.classList.contains("maid-group-label")) {
        current = node.dataset.store || null;
      } else if (
        node.classList.contains("maid-kitchen-list") ||
        node.classList.contains("maid-trainee-list")
      ) {
        current = null;
      } else if (node.classList.contains("maid-name") && current) {
        placementsByView.set(`${node.textContent}|${stopKey} ${shift}`, current);
      }
    }
  });
}
assert.ok(placementsByView.size > 0, "the forecast mode must place maids in shops");

selectViewMode("maid");
assert.equal(
  withClass(calendar, "calendar-day").length,
  0,
  "the maid mode replaces the calendar grid"
);

const plans = withClass(calendar, "maid-plan");
assert.ok(plans.length > 0, "the maid mode must list some maids");
assert.ok(
  plans.length <= schedule.roster.length,
  "the maid mode cannot list more maids than the roster holds"
);

const stopStates = new Set();
const planNames = [];
let agreed = 0;
let accuracyShown = 0;
let unmeasuredShown = 0;
// 記録のある日と、これからの日。既定期間は丸ごと未来なので、ここで通るのは
// 未来の側だけ。記録の側は下の「過ぎた日とこれからの日を混ぜない」で見る。
const lastRecorded = Object.keys(insights.actual).sort().at(-1);
let guessedAhead = 0;
for (const plan of plans) {
  const heading = withClass(plan, "maid-name");
  assert.equal(heading.length, 1, "each plan must name exactly one maid");
  const name = heading[0].textContent;
  assert.ok(
    roster.has(name) || recordedNames.has(name),
    `unknown maid "${name}" in the maid mode`
  );
  planNames.push(name);

  const stops = withClass(plan, "maid-plan-stop");
  assert.ok(stops.length > 0, "a plan with no shifts must not be rendered at all");

  // 件数を先に言っておかないと、外れが縦に積み上がったときに裏切りに見える。
  const notes = withClass(plan, "maid-plan-note");
  assert.equal(notes.length, 1, "each plan must carry exactly one caveat");
  assert.match(notes[0].textContent, /\d+件/, "the caveat must count the days it is guessing at");

  // 的中はその人の実測。全体値で代用したり、母数を落としたりしていないこと。
  const measured = insights.maidTendency?.[name]?.accuracy;
  const caveat = notes[0].textContent;
  if (measured && plan !== undefined) {
    const stopsHere = withClass(plan, "maid-plan-stop");
    const open = stopsHere.filter((s) => !s.classList.contains("is-open")).length;
    if (open > 0) {
      assert.ok(
        caveat.includes(`${measured.n}件を試して`),
        `${name}: the caveat must say how many tries the rate is based on`
      );
      assert.ok(
        caveat.includes(`${Math.round(measured.rate * 100)}%`),
        `${name}: the caveat must quote her own measured rate`
      );
      // 主語はこちら側に置く。低い数字が出る方もいるので「当てられ」と書く。
      assert.ok(
        caveat.includes("当てられて"),
        `${name}: the sentence must put the guessing on us, not on her`
      );
      if (schedule.kitchenStaff.includes(name)) {
        assert.ok(caveat.includes("キッチン"), `${name}: a kitchen maid must get the reason too`);
      }
      accuracyShown += 1;
    }
  } else if (!measured) {
    assert.ok(
      caveat.includes("測れていません"),
      `${name}: without a measured rate the caveat must say so, not borrow one`
    );
    assert.doesNotMatch(
      caveat,
      /\d+%当てられ/,
      `${name}: an unmeasured maid must not be handed someone else's figure`
    );
    unmeasuredShown += 1;
  }

  const counted = withClass(plan, "maid-plan-count");
  assert.equal(counted.length, 1, "each plan must show its own shift count");
  assert.equal(counted[0].textContent, `${stops.length}件`, "the count must match the list");

  // 日付は行ごとに書く。片シフトだけの日が83%なので、日ごとに束ねても
  // ほとんどの行は1件のままで、入れ子が増えるだけになる。
  assert.deepEqual(
    stops.map((stop) => stop.dataset.date),
    [...stops.map((stop) => stop.dataset.date)].sort(),
    "a plan must read in date order"
  );
  assert.equal(
    withClass(plan, "maid-plan-stops").length,
    1,
    "a plan must keep one flat list of shifts"
  );

  for (const stop of stops) {
    const label = withClass(stop, "maid-plan-when")[0].textContent;
    const [, month, day] = stop.dataset.date.split("-").map(Number);
    const shift = insights.shifts.find((candidate) => label.endsWith(candidate));
    assert.ok(shift, `a stop must name a real shift, got "${label}"`);
    // 日付は行に書く。曜日も添える（「9/3」だけでは何曜日か分からない）。
    assert.ok(
      label.startsWith(`${month}/${day}(`),
      `a stop must lead with its own date and weekday, got "${label}"`
    );
    const when = `${month}/${day} ${shift}`;
    const where = withClass(stop, "maid-plan-where")[0];
    const storeId = where.dataset.store;
    assert.ok(
      storeId === "" || storeIds.has(storeId),
      `the maid mode named an unknown shop "${storeId}"`
    );
    // 確度の低い日を空欄にすると「出ない日」と読まれる。かならず何か書く。
    assert.ok(where.textContent.length > 0, "a stop must never render an empty shop");
    if (storeId === "") {
      assert.equal(where.textContent, "未定", "an unplaced stop must say so in words");
    }
    // 色だけで確度を伝えないよう、状態はクラスで分け、読み上げ文も添える。
    const state = ["open", "likely", "unlikely", "unknown"].find((candidate) =>
      stop.classList.contains(`is-${candidate}`)
    );
    assert.ok(state, `a stop carries no state class: ${stop.classList.value}`);
    stopStates.add(state);
    assert.ok(stop.title, "a stop must explain itself in a tooltip");
    // 過ぎた日とこれからの日を混ぜない。記念日は主役の所属店を「営業」で確定
    // させるので、店の状態だけを見ていると、同じ店に置かれた他の人まで確定に
    // 見える。実際に未来の日付で「◯号店にいた記録があります」と出ていた。
    if (stop.dataset.date > lastRecorded) {
      assert.doesNotMatch(
        stop.title,
        /記録が/,
        `${name} ${when}: this day has not happened yet, so there is no record of it`
      );
      assert.ok(
        !stop.classList.contains("is-open") || stop.title.includes("主役"),
        `${name} ${when}: only the host of an anniversary may read as settled before the day`
      );
      guessedAhead += 1;
    }
    // 置いた理由には予定表の顔ぶれも入っている。営業率だけを表に出すと
    // 「15%の店になぜ置いたのか」と読まれるので、本文では数字を名乗らない。
    assert.doesNotMatch(
      stop.title,
      /\d+%/,
      "a stop must not quote a bare opening rate it did not decide on"
    );
    const kept = withClass(stop, "maid-plan-rate");
    assert.ok(kept.length <= 1, "a stop must not carry more than one hidden rate");
    if (kept.length === 1) {
      assert.match(kept[0].textContent, /^\d+%$/, "the hidden rate must stay readable in the markup");
    }
    // 読み上げは aria-label だけ。同じ文を隠し要素にも置くと二度読まれる。
    const spoken = stop.getAttribute("aria-label");
    assert.ok(spoken, "a stop must expose its explanation to screen readers");
    assert.ok(spoken.startsWith(label), "the spoken text must say which day it is talking about");
    assert.ok(spoken.endsWith(stop.title), "the spoken text and the tooltip must not drift apart");
    assert.equal(
      withClass(stop, "visually-hidden").length,
      0,
      "the aria-label already carries the explanation; a hidden copy would be read twice"
    );

    const seen = placementsByView.get(`${name}|${when}`);
    if (seen !== undefined) {
      agreed += 1;
      assert.equal(
        storeId,
        seen,
        `${name} ${when}: the maid mode says ${storeId} but the calendar says ${seen}`
      );
    }
  }
}
assert.ok(agreed > 0, "the two views must overlap enough to be compared at all");
assert.ok(
  guessedAhead > 0,
  "no stop falls after the last record, so the wording for days ahead is untested"
);
assert.ok(
  accuracyShown > 0,
  "no plan quoted a measured accuracy, so that wording is untested"
);
assert.ok(
  stopStates.size >= 2,
  `every stop looks equally certain (${[...stopStates].join(",")}), so the reader cannot tell them apart`
);

// 並びは公式の掲載順のまま。モードを変えても顔ぶれの順序は変わらない。
// 記録にしかいない方は名簿に位置が無いので、最後にまとめて並ぶ。
const planOrder = planNames.map((name) => rosterOrder.get(name) ?? Infinity);
assert.deepEqual(
  planOrder,
  [...planOrder].sort((a, b) => a - b),
  "the maid mode must keep the official roster order"
);

selectViewMode("forecast");
assert.equal(
  withClass(calendar, "maid-plan").length,
  0,
  "switching back must clear the per-maid list"
);
assert.ok(
  withClass(calendar, "calendar-day").length > 0,
  "switching back must restore the calendar grid"
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
const assignedBeforeHiding = withClass(calendar, "maid-group-label").map(
  (label) => label.dataset.store
);

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

// 隠しても割り振りは動かない。残った人の店は同じまま。見出しが名乗る。
const stillShown = withClass(calendar, "maid-group-label").map((label) => label.dataset.store);
assert.ok(
  stillShown.every((store) => assignedBeforeHiding.includes(store)),
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
  // 記録の無い側は、同じ日のもう片方の実績から見込むか、曜日傾向に落ちる。
  // どちらでもよいが、「実績」を名乗ってはいけない（＝休みと読まれてはいけない）。
  const fallback = cell.badges.filter((badge) => badge !== "実績");
  assert.equal(
    fallback.length,
    1,
    `${partialDate}'s missing shift must fall back instead of being called closed`
  );
  assert.ok(
    knownBadges.has(fallback[0]),
    `${partialDate}'s fallback badge must be one we know, got "${fallback[0]}"`
  );
}

// --- 人ごとの画面で、過ぎた日とこれからの日を混ぜない -------------------
// 上の既定期間は丸ごと未来なので、「記録があります」の側が一度も通らない。
// いまの期間は記録の最終日をまたいでいるので、両側を同じ画面から読める。
selectViewMode("maid");
let citedRecord = 0;
let guessedHere = 0;
for (const plan of withClass(calendar, "maid-plan")) {
  const who = withClass(plan, "maid-name")[0].textContent;
  for (const stop of withClass(plan, "maid-plan-stop")) {
    const on = stop.dataset.date;
    if (on > lastActual) {
      // 記念日は主役の所属店を「営業」で確定させる。店の状態だけを見ていると、
      // 同じ店に置かれた他の人まで確定に見え、未来の日付に「◯号店にいた記録が
      // あります」と出る。店が開くことと、その人がそこにいることは別の話。
      assert.doesNotMatch(
        stop.title,
        /記録が/,
        `${who} ${on}: this day has not happened yet, so there is no record of it`
      );
      guessedHere += 1;
    } else if (insights.actual[on]) {
      assert.match(
        stop.title,
        /記録が|主役/,
        `${who} ${on}: this day is on the record, so do not call it a guess`
      );
      if (stop.title.includes("記録が")) {
        citedRecord += 1;
      }
    }
  }
}
assert.ok(citedRecord > 0, "no stop cites a record, so the wording for days gone by is untested");
assert.ok(guessedHere > 0, "no stop falls after the last record, so the other wording is untested");


// 予測モードに戻して、その日そのシフトに誰が出ていたかを画面から読む。
selectViewMode("forecast");
const recordedCells = withClass(calendar, "calendar-day").filter((cell) =>
  withClass(cell, "store-status-badge").some((badge) => badge.textContent === "実績")
);
assert.ok(recordedCells.length > 0, "the recorded window must render some recorded days");

let checkedRecordedShifts = 0;
let traineesShown = 0;
for (const cell of recordedCells) {
  const [, year, month, day] = /(\d+)年(\d+)月(\d+)日/.exec(cell.getAttribute("aria-label"));
  const cellKey = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  withClass(cell, "shift-section").forEach((section, shiftIndex) => {
    const shift = insights.shifts[shiftIndex];
    const record = insights.actualRoster?.[cellKey]?.[shift];
    if (!record) {
      return;
    }
    checkedRecordedShifts += 1;
    const shown = withClass(section, "maid-entry").map(
      (entry) => withClass(entry, "maid-name")[0].textContent
    );
    const inRecord = Object.values(record.stores).flat();
    assert.deepEqual(
      [...shown].sort(),
      [...inRecord].sort(),
      `${cellKey} ${shift}: the calendar must show the maids the record names, no more and no fewer`
    );

    // 予定表に名前があってもお休みだった方は出さない。出すと「未定」と書くことになる。
    const posted = (schedule.schedule[cellKey]?.[shift] ?? []).map((entry) => entry.name);
    for (const name of posted) {
      if (!inRecord.includes(name)) {
        assert.ok(
          !shown.includes(name),
          `${cellKey} ${shift}: ${name} was on the rota but not in the record, so she must not be shown`
        );
      }
    }

    // 割り振り先も記録どおり。見出しがその店を名乗る。
    let group = null;
    for (const node of walk(section)) {
      if (!node.classList) {
        continue;
      }
      if (node.classList.contains("maid-group-label")) {
        group = node.dataset.store || null;
      } else if (node.classList.contains("maid-name") && group) {
        assert.ok(
          record.stores[group]?.includes(node.textContent),
          `${cellKey} ${shift}: ${node.textContent} is under ${group}, which the record does not say`
        );
      }
    }

    // 見習いにゃんこの印。判定した日だけ、判定された人にだけ付く。
    const judged = Array.isArray(record.trainees);
    for (const entry of withClass(section, "maid-entry")) {
      const name = withClass(entry, "maid-name")[0].textContent;
      const marked = withClass(entry, "maid-trainee");
      const expected = judged && record.trainees.includes(name);
      assert.equal(
        marked.length,
        expected ? 1 : 0,
        `${cellKey} ${shift}: ${name} ${expected ? "is" : "is not"} a trainee in the record`
      );
      if (marked.length === 1) {
        traineesShown += 1;
        assert.equal(marked[0].textContent, "🔰", "the trainee mark must be the badge emoji");
        assert.equal(
          marked[0].getAttribute("aria-hidden"),
          "true",
          "the mark is decorative; the entry's own label carries the words"
        );
        // 色や絵文字だけに頼らない。読み上げにも枠線にも出す。
        assert.ok(
          entry.classList.contains("is-trainee"),
          "a trainee entry must be marked in the class list too, not only by the emoji"
        );
        assert.ok(
          (entry.getAttribute("aria-label") ?? "").includes("見習いにゃんこ"),
          "a trainee must be described in words for screen readers"
        );
        assert.ok(
          (entry.title ?? "").includes("見習いにゃんこ"),
          "a trainee must be named in the tooltip too"
        );
      }
    }

    // 記録の日に確率は出さない。誰がどこにいたかは分かっている。
    for (const entry of withClass(section, "maid-entry")) {
      assert.doesNotMatch(
        entry.title ?? "",
        /\d+%/,
        `${cellKey} ${shift}: a recorded shift must not quote odds for a maid`
      );
    }
  });
}
assert.ok(checkedRecordedShifts > 0, "at least one recorded shift must have been checked");
assert.ok(traineesShown > 0, "the recorded window must include a trainee, or the mark is untested");

// --- 見込みのシフトに、名前の分からない見習いにゃんこを出す -------------
// 人数は「1店あたり × 開く店の数」を丸めた値。データ側の割合は半減期30日で
// 動くので、期待値は表から引き直す（数字を書くと集計のたびに落ちる）。
let guessedShifts = 0;
let guessedTotal = 0;
let headedTrainees = 0;
for (const cell of withClass(calendar, "calendar-day")) {
  const [, year, month, day] = /(\d+)年(\d+)月(\d+)日/.exec(cell.getAttribute("aria-label"));
  const cellKey = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  withClass(cell, "shift-section").forEach((section, shiftIndex) => {
    const shift = insights.shifts[shiftIndex];
    const guesses = withClass(section, "maid-entry").filter((entry) =>
      entry.classList.contains("is-trainee-guess")
    );
    const recorded = Boolean(insights.actualRoster?.[cellKey]?.[shift]);
    if (recorded) {
      // 誰がいたか分かっている日に推測を混ぜない。
      assert.equal(
        guesses.length,
        0,
        `${cellKey} ${shift} is recorded, so it must not guess at trainees`
      );
      return;
    }
    const shops = new Set(withClass(section, "maid-group-label").map((l) => l.dataset.store));
    if (shops.size === 0) {
      return;
    }
    const rate = insights.traineeOutlook?.[shift]?.perStore ?? 0;
    assert.equal(
      guesses.length,
      Math.round(rate * shops.size),
      `${cellKey} ${shift} opens ${shops.size} shop(s), so the trainee count must follow the table`
    );
    guessedShifts += 1;
    guessedTotal += guesses.length;
    for (const guess of guesses) {
      // 名前は出さない。誰なのかが分からないため。
      assert.equal(
        withClass(guess, "maid-name")[0].textContent,
        "見習い",
        "a guessed trainee must not borrow a real maid's name"
      );
      assert.equal(withClass(guess, "maid-trainee").length, 1, "a guess must carry the 🔰");
      assert.equal(
        withClass(guess, "maid-trainee")[0].getAttribute("aria-hidden"),
        "true",
        "the mark is decorative; the row's own label carries the words"
      );
      const spoken = guess.getAttribute("aria-label") ?? "";
      assert.ok(spoken.includes("見習いにゃんこ"), "a guess must say what it is in words");
      assert.ok(
        spoken.includes("分かりません"),
        "a guess must admit it cannot name the maid"
      );
      assert.equal(guess.title, spoken, "the tooltip and the spoken text must not drift apart");
      // 半減期30日で動く値なので、文言に割合を焼き込まない。
      assert.doesNotMatch(spoken, /\d+%|0\.\d+/, "a guess must not quote a rate that moves monthly");
    }
    // どの店にいるかは読めない。店の見出しの下に置くと、その店にいると読まれる。
    for (const list of withClass(section, "maid-list")) {
      assert.equal(
        withClass(list, "maid-entry").filter((e) => e.classList.contains("is-trainee-guess")).length,
        0,
        `${cellKey} ${shift}: a guessed trainee must not sit under a shop heading`
      );
    }
    // クラスを分けるだけでは足りない。見出しが無いと直前の店の <ul> にそのまま
    // 続いて見え、店は s1→s4 の順に並ぶので、いつも番号のいちばん大きい店に
    // ぶら下がって読める。実測では見習いがいちばん少ないのがその4号店なので、
    // 黙っていると実測と正反対のことを言うことになる。
    if (guesses.length > 0) {
      const lists = withClass(section, "maid-trainee-list");
      assert.equal(lists.length, 1, `${cellKey} ${shift}: the trainees must form one list`);
      const nodes = section.children;
      const before = nodes[nodes.indexOf(lists[0]) - 1];
      assert.ok(
        before && before.classList?.contains("maid-trainee-label"),
        `${cellKey} ${shift}: the trainee list must be introduced by its own heading, ` +
          `otherwise it reads as part of the shop above it (got "${before?.classList?.value}")`
      );
      // 見出しは店を名乗らない。名乗ったら、この一覧を分けた意味が消える。
      assert.ok(!before.dataset.store, "the trainee heading must not claim a shop");
      assert.ok(
        before.textContent.includes(`${guesses.length}人`),
        `${cellKey} ${shift}: the trainee heading must count them`
      );
      // 予定表から数えた人数ではないので、キッチンと同じ書き方にはしない。
      assert.ok(
        before.textContent.includes("ほど"),
        "the trainee count is an estimate, so it must not read like a headcount"
      );
      assert.ok(
        (before.title ?? "").includes("分かりません"),
        "the trainee heading must say the maid cannot be named"
      );
      // 店ごとの見出しの「かならず店を名乗る」約束を壊していないこと。
      assert.equal(
        withClass(section, "maid-group-label").length,
        withClass(section, "maid-list").length,
        "the trainee heading must not be counted as a shop group"
      );
      headedTrainees += 1;
    }
  });
}
assert.ok(guessedShifts > 0, "some forecast shift must have been checked for trainees");
assert.ok(guessedTotal > 0, "the forecast window must guess at least one trainee, or this is untested");
assert.ok(headedTrainees > 0, "no trainee list was checked for its heading");

// --- 表の上限に当たったら、そう断る -------------------------------------
// 上限は insights.openCountByHeadcount の要素数+1。3店と出したのは
// 「3店がいちばんありそう」だからではなく、4店を数える材料が無いから。
let cappedShifts = 0;
for (const cell of withClass(calendar, "calendar-day")) {
  const [, year, month, day] = /(\d+)年(\d+)月(\d+)日/.exec(cell.getAttribute("aria-label"));
  const cellKey = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  withClass(cell, "shift-section").forEach((section, shiftIndex) => {
    const shift = insights.shifts[shiftIndex];
    const shops = new Set(withClass(section, "maid-group-label").map((l) => l.dataset.store));
    if (shops.size === 0) {
      return;
    }
    const ceiling = insights.openCountByHeadcount[shift].length + 1;
    const capped = withClass(section, "store-outlook-capped");
    const recorded = Boolean(insights.actualRoster?.[cellKey]?.[shift]);
    // 記録の日は数え直す必要がない。実際に何店開いたか分かっている。
    const expected = !recorded && shops.size === ceiling && ceiling < insights.stores.length;
    assert.equal(
      capped.length,
      expected ? 1 : 0,
      `${cellKey} ${shift}: ${shops.size} shop(s) against a ceiling of ${ceiling}`
    );
    if (expected) {
      cappedShifts += 1;
      assert.ok(capped[0].textContent.includes(`${ceiling}店`), "the note must say where the ceiling is");
      assert.equal(
        capped[0].getAttribute("aria-hidden"),
        "true",
        "the visible note is a summary; the spoken text carries the reason"
      );
      const spoken = withClass(section, "visually-hidden")[0]?.textContent ?? "";
      assert.ok(
        spoken.includes("いちばんありそう、という意味ではなく"),
        "screen readers must get the reason, not just the count"
      );
    }
  });
}
assert.ok(cappedShifts > 0, "some shift must hit the ceiling, or this is untested");

// --- キッチンにゃんこは、見込みの日には店を名乗らない ---------------------
// 開いた店の枠に90%載っていて各店1人ずつなので、「どの店にいるか」は
// 「どの店が開くか」とほとんど同じことしか言わない。当てられもしない。
{
  const cooks = new Set(schedule.kitchenStaff);
  let looseCooks = 0;
  let recordedCooks = 0;
  let ordersChecked = 0;
  for (const cell of withClass(calendar, "calendar-day")) {
    const [, year, month, day] = /(\d+)年(\d+)月(\d+)日/.exec(cell.getAttribute("aria-label"));
    const key = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    withClass(cell, "shift-section").forEach((section, shiftIndex) => {
      const shift = insights.shifts[shiftIndex];
      const recorded = Boolean(insights.actualRoster?.[key]?.[shift]);
      const inShops = new Set(
        withClass(section, "maid-list").flatMap((list) =>
          withClass(list, "maid-entry").map((e) => withClass(e, "maid-name")[0].textContent)
        )
      );
      const inKitchen = withClass(section, "maid-kitchen-list").flatMap((list) =>
        withClass(list, "maid-entry").map((e) => withClass(e, "maid-name")[0].textContent)
      );
      for (const name of inKitchen) {
        assert.ok(cooks.has(name), `${name} is not kitchen staff but sits in the kitchen list`);
        assert.ok(!recorded, `${key} ${shift} is recorded, so ${name}'s shop is known`);
        looseCooks += 1;
      }
      // 店を名乗らないと決めた以上、チップで店を出しては辻褄が合わない。
      for (const list of withClass(section, "maid-kitchen-list")) {
        assert.equal(
          withClass(list, "maid-store-chip").length,
          0,
          `${key} ${shift}: the kitchen list must not name a shop on a chip either`
        );
      }
      for (const name of inShops) {
        if (!cooks.has(name)) {
          continue;
        }
        // 記録の日と、記念日の主役だけは店の下に出てよい。どちらも分かっている。
        const row = withClass(section, "maid-entry").find(
          (e) => withClass(e, "maid-name")[0].textContent === name
        );
        const featured = row.classList.contains("is-featured");
        assert.ok(
          recorded || featured,
          `${key} ${shift}: ${name} is kitchen staff on a guessed shift, so no shop should be claimed`
        );
        if (recorded) {
          recordedCooks += 1;
        }
      }
      if (inKitchen.length > 0) {
        const heading = withClass(section, "maid-kitchen-label");
        assert.equal(heading.length, 1, "the kitchen list must carry one heading");
        assert.ok(heading[0].textContent.includes(`${inKitchen.length}人`), "the heading must count them");
        assert.ok(heading[0].title.includes("配属とも関係なく"), "the heading must say why no shop is named");
        // 見出しが店を名乗らないことが、店ごとの見出しの約束を壊していないこと。
        assert.equal(
          withClass(section, "maid-group-label").length,
          withClass(section, "maid-list").length,
          "the kitchen heading must not be counted as a shop group"
        );
        // 並びは 店ごと → 見習い → キッチン。
        const order = section.children.map((node) => node.classList?.value ?? "");
        const kitchenAt = order.findIndex((c) => c.includes("maid-kitchen-list"));
        const traineeAt = order.findIndex((c) => c.includes("maid-trainee-list"));
        const lastShopAt = order.map((c, i) => (c.includes("maid-list") ? i : -1)).pop();
        assert.ok(kitchenAt > lastShopAt, "the kitchen list must come after the shop groups");
        if (traineeAt >= 0) {
          assert.ok(traineeAt < kitchenAt, "trainees must be listed above the kitchen staff");
          ordersChecked += 1;
        }
      }
    });
  }
  assert.ok(looseCooks > 0, "no kitchen maid was pulled out of the shops, so this is untested");
  assert.ok(recordedCooks > 0, "a recorded shift must still place its kitchen staff");
  assert.ok(
    ordersChecked > 0,
    "no shift carried both a trainee and a kitchen list, so their order is untested"
  );
  // 記念日の主役が所属店に残ることは、既定の期間を見ている上のブロックが確かめている
  // （あらたさんがキッチンかつ主役で、店ごとの見出しの下にいることを要求している）。
  // この期間には主役のキッチンにゃんこがいないので、ここでは件数を求めない。
}

// 未提出の注意書きは、カレンダーの下に1行だけ出す。シフトごとに繰り返さない。
{
  const line = elementById("schedule-pending-note");
  const pending = insights.schedulePending?.pending ?? [];
  if (pending.length > 0) {
    assert.equal(line.hidden, false, "with names outstanding the line must be shown");
    assert.ok(line.textContent.includes(`${pending.length}名`), "the line must count them");
    assert.ok(line.title, "the line must carry the detail in a tooltip");
    assert.ok(
      (line.getAttribute("aria-label") ?? "").includes("実際より少なめ"),
      "screen readers must get the same warning as the tooltip"
    );
    for (const name of pending) {
      assert.ok(line.title.includes(name), `${name} must be named in the detail`);
    }
  } else {
    assert.equal(line.hidden, true, "with nobody outstanding the line must stay hidden");
  }
}

// 判定していない日には印を付けない。「全員が昇格済み」ではなく「分からない」ため。
const unjudgedDay = Object.keys(insights.actualRoster ?? {}).find((key) =>
  insights.shifts.some((s) => insights.actualRoster[key][s] && !insights.actualRoster[key][s].trainees)
);
assert.ok(unjudgedDay, "the record must reach past the window where trainees are judged");
assert.ok(
  unjudgedDay < Object.keys(insights.actualRoster).sort().at(-1),
  "the unjudged days must be the older ones"
);

console.log(
  `Headless render valid: ${dayCells.length} day cells, ${shiftSections.length} shift sections, ` +
    `${maidEntries.length} maid entries sorted by assigned store, ` +
    `${viewModeValues.length} view modes with ${plans.length} per-maid plans agreeing with the calendar, ` +
    "and no explanation block below the calendar."
);
