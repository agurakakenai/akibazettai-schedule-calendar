"use strict";

// Optional real-browser regression, using Node's built-in CDP transport.
// Uses a disposable profile and never connects to a user's browser session.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const output = process.argv[2];
const publicOrigin = process.env.PUBLIC_PREVIEW_URL?.replace(/\/$/, "");
if (publicOrigin) {
  assert.equal(publicOrigin, "https://agurakakenai.github.io/akibazettai-schedule-calendar",
    "the public smoke test is restricted to this site's approved URL");
}
const executable = process.env.CHROME_PATH || [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
].find((file) => fs.existsSync(file));
assert.ok(executable, "Set CHROME_PATH to a Chromium executable");
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  let observationResponse = null;
  let observationFailure = false;
  let holdObservation = false;
  const heldObservationResponses = [];
  let personalResponse = {
    schemaVersion: 1, complete: false, checkedAt: null, lastSuccessAt: null,
    posts: [], lastRun: { status: "never" }
  };
  let personalFailure = 0;
  let holdPersonal = false;
  const heldPersonalResponses = [];
  const profile = fs.mkdtempSync(path.join(path.relative(process.cwd(), root), ".calendar-headless-"));
  const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
  const server = http.createServer((req, res) => {
    const pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
    if (pathname === "/data/personal-shifts.json") {
      if (holdPersonal) {
        heldPersonalResponses.push(res);
        return;
      }
      res.setHeader("Content-Type", "application/json");
      res.writeHead(personalFailure || 200);
      res.end(JSON.stringify(personalResponse));
      return;
    }
    if (pathname === "/data/observed-shifts.json" && (observationResponse || observationFailure)) {
      if (holdObservation) {
        heldObservationResponses.push(res);
        return;
      }
      res.setHeader("Content-Type", "application/json");
      res.writeHead(observationFailure ? 503 : 200);
      res.end(JSON.stringify(observationResponse));
      return;
    }
    const file = path.resolve(root, `.${pathname === "/" ? "/index.html" : pathname}`);
    if (!file.startsWith(`${root}${path.sep}`) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.writeHead(404).end();
      return;
    }
    res.setHeader("Content-Type", mime[path.extname(file)] || "application/octet-stream");
    res.end(fs.readFileSync(file));
  });
  if (!publicOrigin) await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = publicOrigin || `http://127.0.0.1:${server.address().port}`;
  const browser = spawn(executable, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--disable-component-update", "--disable-sync",
    "--disable-extensions", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
    `--user-data-dir=${path.resolve(profile)}`, "about:blank"
  ], { windowsHide: true, stdio: "ignore" });
  let ws;
  try {
    const portFile = path.join(profile, "DevToolsActivePort");
    for (let n = 0; !fs.existsSync(portFile) && n < 100; n++) await delay(100);
    assert.ok(fs.existsSync(portFile), "headless Chromium must start");
    const port = fs.readFileSync(portFile, "utf8").split("\n")[0];
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    ws = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
    let id = 0;
    const pending = new Map();
    const exceptions = [];
    let widgetRequests = 0;
    let widgetBlocked = false;
    const mockWidget = `
      window.__mockTweetCalls = {};
      window.__mockTweetLoads = {};
      window.twttr = {widgets: {createTweet: async (id, target, options) => {
        window.__mockTweetCalls[id] = (window.__mockTweetCalls[id] || 0) + 1;
        window.__mockTweetOptions = options;
        const frame = document.createElement("iframe");
        frame.dataset.postId = id;
        frame.title = "Official post widget mock";
        frame.style.minWidth = "550px";
        frame.height = "170";
        frame.srcdoc = "<p>Official widget fixture</p><button>Widget action</button>";
        frame.addEventListener("load", () => {
          window.__mockTweetLoads[id] = (window.__mockTweetLoads[id] || 0) + 1;
          frame.contentDocument.querySelector("button").addEventListener("click", () => {
            window.__mockWidgetClicks = (window.__mockWidgetClicks || 0) + 1;
          });
        });
        target.append(frame);
        return frame;
      }}};
      const widgets = window.twttr.widgets;
      delete window.twttr.widgets;
      window.twttr.ready = callback => setTimeout(() => {
        window.twttr.widgets = widgets;
        callback(window.twttr);
      }, 0);
    `;
    ws.onmessage = ({ data }) => {
      const message = JSON.parse(data);
      if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
      if (message.method === "Fetch.requestPaused") {
        const { requestId, request } = message.params;
        const widget = request.url === "https://platform.twitter.com/widgets.js";
        if (widget) widgetRequests++;
        const method = widget && !widgetBlocked ? "Fetch.fulfillRequest" : "Fetch.failRequest";
        const params = widget && !widgetBlocked
          ? { requestId, responseCode: 200, responseHeaders: [{ name: "Content-Type", value: "text/javascript" }],
            body: Buffer.from(mockWidget).toString("base64") }
          : { requestId, errorReason: "BlockedByClient" };
        ws.send(JSON.stringify({ id: ++id, method, params }));
      }
      if (!message.id) return;
      const request = pending.get(message.id);
      if (!request) return;
      pending.delete(message.id);
      clearTimeout(request.timer);
      if (message.error) request.reject(new Error(JSON.stringify(message.error)));
      else request.resolve(message.result);
    };
    const call = (method, params = {}) => new Promise((resolve, reject) => {
      const number = ++id;
      const timer = setTimeout(() => {
        pending.delete(number);
        reject(new Error(`CDP timeout: ${method}`));
      }, 15000);
      pending.set(number, { resolve, reject, timer });
      ws.send(JSON.stringify({ id: number, method, params }));
    });
    const evaluate = async (expression) => {
      const result = await call("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const wait = async (expression) => {
      for (let n = 0; n < 100; n++) {
        if (await evaluate(expression)) return;
        await delay(50);
      }
      assert.fail(`Browser condition timed out: ${expression}; exceptions=${JSON.stringify(exceptions)}; focus=${await evaluate('document.activeElement.outerHTML.slice(0, 400)')}`);
    };
    const click = (selector) => evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
    const scrollShift = (shift) => evaluate(`(() => {
      const section = document.querySelector("#dialog-" + ${JSON.stringify(shift)});
      const content = document.querySelector("#day-dialog-content");
      content.scrollTop = ${JSON.stringify(shift)} === "day" ? 0
        : content.scrollTop + section.getBoundingClientRect().top - content.getBoundingClientRect().top;
      section.focus({preventScroll:true});
    })()`);
    const key = async (name, code, virtualKey, modifiers = 0) => {
      for (const type of ["keyDown", "keyUp"]) {
        await call("Input.dispatchKeyEvent", {
          type, key: name, code, windowsVirtualKeyCode: virtualKey, modifiers,
          ...(type === "keyDown" && name === "Enter" ? { text: "\r" } : {}),
          ...(type === "keyDown" && name === " " ? { text: " " } : {})
        });
      }
    };
    const capture = async (name, full = false) => {
      if (!output) return;
      fs.mkdirSync(output, { recursive: true });
      const params = { format: "png", captureBeyondViewport: full };
      if (full) {
        const metrics = await call("Page.getLayoutMetrics");
        params.clip = { x: 0, y: 0, width: metrics.cssContentSize.width, height: metrics.cssContentSize.height, scale: 1 };
      }
      const image = await call("Page.captureScreenshot", params);
      fs.writeFileSync(path.join(output, `${name}.png`), Buffer.from(image.data, "base64"));
    };
    const preference = (value) => call("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value }]
    });
    const palette = (selectors) => evaluate(`(${JSON.stringify(selectors)}).map(selector => {
      const style = getComputedStyle(document.querySelector(selector));
      return [selector, style.backgroundColor, style.color, style.colorScheme, style.borderColor, style.accentColor];
    })`);
    await call("Page.enable");
    await call("Runtime.enable");
    await call("Network.enable");
    await call("Fetch.enable", { patterns: publicOrigin
      ? [{ urlPattern: "https://platform.twitter.com/*" }, { urlPattern: "https://x.com/*" },
        { urlPattern: "https://*.twimg.com/*" }, { urlPattern: "https://twitter.com/*" }]
      : [{ urlPattern: "https://*" }, { urlPattern: "http://*.com/*" }, { urlPattern: "http://*.jp/*" }] });
    await call("Emulation.setFocusEmulationEnabled", { enabled: true });
    await call("Emulation.setTimezoneOverride", { timezoneId: "America/Los_Angeles" });
    await call("Page.addScriptToEvaluateOnNewDocument", { source: `
      const OriginalDate = Date;
      window.Date = class extends OriginalDate {
        constructor(...args) { super(...(args.length ? args : ["2026-09-05T15:30:00Z"])); }
        static now() { return new OriginalDate("2026-09-05T15:30:00Z").valueOf(); }
      };
    ` });
    await call("Emulation.setDeviceMetricsOverride", { width: 1280, height: 960, deviceScaleFactor: 1, mobile: false });
    await preference("light");
    await call("Page.navigate", { url: `${origin}/${publicOrigin ? "?smoke=" + Date.now() : ""}` });
    await wait('document.querySelectorAll(".day-button").length === 30');
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await wait('document.querySelector("#personal-status").dataset.loaded === "true"');
    assert.equal(widgetRequests, 0, "no widget request before an explicit disclosure open");
    assert.equal(await evaluate('document.querySelectorAll("script[src=\\"https://platform.twitter.com/widgets.js\\"]").length'), 0);
    await wait('[...document.querySelectorAll(".event-image")].every(img => img.complete && img.naturalWidth > 0)');
    const lightSelectors = ["html", "body", ".calendar-card", ".day-button", "#date-from", "#date-to"];
    const lightPalette = await palette(lightSelectors);
    assert.equal(lightPalette[1][1], "rgb(255, 251, 235)", "the page background is the light cream");
    assert.ok(lightPalette.every(row => row[3] === "light only" || row[3] === "only light"),
      "forms, native date controls and scrollbars inherit only light");
    await preference("dark");
    await call("Page.navigate", { url: `${origin}/?scoutTheme=dark${publicOrigin ? "&smoke=" + Date.now() : ""}` });
    await wait('document.querySelectorAll(".day-button").length === 30');
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await wait('[...document.querySelectorAll(".event-image")].every(img => img.complete && img.naturalWidth > 0)');
    assert.ok(await evaluate('matchMedia("(prefers-color-scheme: dark)").matches'));
    assert.deepEqual(await palette(lightSelectors), lightPalette, "dark OS preference and legacy URL cannot change the palette");
    await capture("calendar-dark-preference", true);
    await call("Emulation.setAutoDarkModeOverride", { enabled: true });
    assert.deepEqual(await palette(lightSelectors), lightPalette);
    await capture("calendar-auto-dark", true);
    await call("Emulation.setAutoDarkModeOverride", { enabled: false });
    assert.equal(await evaluate('document.querySelector("#date-from").value'), "2026-09-01");
    assert.equal(await evaluate('document.querySelector("#date-to").value'), "2026-09-30");
    assert.equal(await evaluate('document.querySelector(".is-today").dataset.date'), "2026-09-06", "today must be JST, not browser timezone");
    assert.equal(await evaluate('document.querySelectorAll("#calendar .shift-section").length'), 0, "no day/night roster in the month grid");
    assert.equal(await evaluate('document.querySelectorAll(".day-events").length'), 4, "four actual event dates");
    assert.equal(await evaluate('document.querySelectorAll(\'[data-date="2026-09-08"] .event-art\').length'), 1, "same host across shifts appears once");
    const columns = () => evaluate('getComputedStyle(document.querySelector(".calendar-grid")).gridTemplateColumns.split(" ").length');
    const noOverflow = () => evaluate('document.documentElement.scrollWidth <= innerWidth');
    assert.equal(await columns(), 7);
    assert.ok(await noOverflow());
    await capture("calendar-desktop", true);

    await evaluate('document.querySelector(\'[data-date="2026-09-08"]\').focus()');
    await key("Enter", "Enter", 13);
    await wait('document.querySelector("#day-dialog").open');
    assert.equal(await evaluate('document.activeElement.id'), "close-day-dialog");
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .shift-section").length'), 2);
    assert.equal(await evaluate('document.body.style.overflow'), "hidden");
    assert.ok(await evaluate('[...document.querySelectorAll("#day-dialog .is-featured")].some(item => item.title.includes("7周年"))'));
    const popupSelectors = ["#day-dialog", ".dialog-header", ".dialog-content", ".shift-day", ".shift-night", "#close-day-dialog", "#date-from"];
    const darkPopup = await palette(popupSelectors);
    await preference("light");
    assert.deepEqual(await palette(popupSelectors), darkPopup, "popup and inputs stay light under either OS preference");
    await preference("dark");
    await capture("popup-dark-preference");
    await capture("popup-desktop");
    for (let n = 0; n < 30; n++) {
      await key("Tab", "Tab", 9, n < 15 ? 0 : 8);
      assert.ok(await evaluate('document.querySelector("#day-dialog").contains(document.activeElement)'),
        "Tab and Shift+Tab must remain inside the modal");
    }
    await key("Escape", "Escape", 27);
    await wait('!document.querySelector("#day-dialog").open');
    assert.equal(await evaluate('document.activeElement.dataset.date'), "2026-09-08");
    assert.equal(await evaluate('document.body.style.overflow'), "");
    assert.ok(await evaluate('document.activeElement.classList.contains("is-selected")'));
    await key(" ", "Space", 32);
    await wait('document.querySelector("#day-dialog").open');
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');

    // The same markup and incumbent store colors apply to both record sources.
    const storeColors = {
      s1: "rgb(177, 31, 75)", s2: "rgb(112, 64, 184)",
      s3: "rgb(23, 121, 92)", s4: "rgb(31, 111, 168)"
    };
    const checkedColors = new Set();
    for (const date of ["2026-09-03", "2026-09-04", "2026-09-05"]) {
      await click(`[data-date="${date}"]`);
      await wait('document.querySelector("#day-dialog").open');
      const curated = await evaluate(`Boolean(window.STORE_INSIGHTS.actualRoster[${JSON.stringify(date)}])`);
      const liveObserved = await evaluate('document.querySelectorAll("#day-dialog [data-evidence=observed]").length');
      if (!curated && process.env.REQUIRE_OBSERVATIONS === "1") {
        assert.ok(liveObserved > 0, "live observation snapshot must reach the popup");
        assert.ok(await evaluate('document.querySelectorAll("#day-dialog [data-evidence=observed] .maid-trainee").length > 0'),
          "eligible observed trainees need a beginner mark");
      }
      if (curated || liveObserved > 0) {
        const type = curated ? "recorded" : "observed";
        assert.ok(await evaluate(`[".shift-day", ".shift-night"].every(selector =>
          document.querySelectorAll("#day-dialog "+selector+" [data-evidence=${type}]").length > 0)`));
        assert.equal(await evaluate('document.querySelectorAll("#day-dialog .shift-evidence").length'), 0);
        assert.equal(await evaluate('document.querySelectorAll("#day-dialog .store-outlook").length'), 0);
        assert.equal(await evaluate('document.querySelectorAll("#day-dialog .store-status-badge, #day-dialog .entry-evidence, #day-dialog .maid-store-chip").length'), 0);
        assert.equal(await evaluate(`document.querySelectorAll("#day-dialog .recorded-roster[data-record-type=${type}]").length`), 2);
        assert.ok(await evaluate('[...document.querySelectorAll("#day-dialog .maid-list")].every(list => list.children.length > 0)'));
        assert.ok(await evaluate('[...document.querySelectorAll("#day-dialog .observation-details")].every(details => !details.open)'));
        assert.ok(await evaluate('[...document.querySelectorAll("#day-dialog .observation-details summary")].every(summary => summary.textContent === "集合ポスト")'));
        assert.doesNotMatch(await evaluate('document.querySelector("#day-dialog").textContent'), /JST|原表記|利用者確認/);
        assert.ok(await evaluate('[...document.querySelectorAll("#day-dialog .unmatched-roster")].every(block => block.querySelectorAll(".maid-name").length > 0)'));
        if (curated || date === "2026-09-04") assert.equal(await evaluate('document.querySelectorAll("#day-dialog .unmatched-heading").length'), 0,
          "do not render an empty unconfirmed-plan block when all scheduled names were observed");
        assert.doesNotMatch(await evaluate('document.querySelector("#day-dialog").textContent'),
          /部分観測|公式確認|投稿実績|[1-4]号店[：:\s]*未確認|0人/);
        const names = await evaluate(`(async () => {
          const date = ${JSON.stringify(date)};
          const snapshot = await (await fetch("data/observed-shifts.json")).json();
          const byId = new Map(snapshot.posts.map(post => [post.id, post]));
          const superseded = new Set(snapshot.posts.flatMap(post =>
            (post.editTweetIds?.slice(0, -1) ?? []).filter(id => byId.get(id)?.date === post.date)));
          const currentPosts = snapshot.posts.filter(post => !superseded.has(post.id));
          const insights = window.STORE_INSIGHTS;
          const aliases = new Map(Object.entries(insights.maidTendency)
            .filter(([, entry]) => entry?.alias).map(([name, entry]) => [entry.alias, name]));
          const displayName = (post, rawName) => {
            const rules = window.SCHEDULE_DATA.observationNameCorrections?.[post.id];
            const corrected = rules && Object.hasOwn(rules, rawName) ? rules[rawName].name : rawName;
            return aliases.get(corrected) ?? corrected;
          };
          return ["昼", "夜"].map((shift, index) => {
            const section = document.querySelector(index === 0 ? "#dialog-day" : "#dialog-night");
            const record = insights.actualRoster[date]?.[shift];
            const expected = record
              ? Object.entries(record.stores).flatMap(([store, people]) => people.map(name => store+"|"+name))
              : [...new Set(currentPosts.filter(post => post.date === date && post.shift === shift)
                .flatMap(post => post.names.map(name => post.storeId+"|"+displayName(post, name))))];
            const shown = [...section.querySelectorAll(".recorded-roster .maid-entry")]
              .map(row => row.dataset.store+"|"+row.dataset.name);
            const sourceLinks = [...section.querySelectorAll(".observation-details a")].map(a => a.href);
            const expectedLinks = record ? [] : currentPosts
              .filter(post => post.date === date && post.shift === shift).map(post => post.url);
            const details = section.querySelector(".observation-details");
            const marked = [...section.querySelectorAll(".recorded-roster .maid-entry.is-trainee")]
              .map(row => row.dataset.name);
            const expectedMarked = shown.map(value => value.split("|")[1]).filter(name => record
              ? record.trainees?.includes(name)
              : insights.traineePeriods?.byName?.[name]?.from <= date && date <= insights.traineePeriods.byName[name].to);
            const displayOrder = [...window.SCHEDULE_DATA.roster];
            for (const [name, before] of Object.entries(window.SCHEDULE_DATA.normalOrderBefore ?? {})) {
              if (!displayOrder.includes(name) || !displayOrder.includes(before)) continue;
              displayOrder.splice(displayOrder.indexOf(name), 1);
              displayOrder.splice(displayOrder.indexOf(before), 0, name);
            }
            const rank = new Map(displayOrder.map((name, index) => [name, index]));
            const kitchen = new Set(window.SCHEDULE_DATA.kitchenStaff);
            const category = name => kitchen.has(name) ? 3 : expectedMarked.includes(name) ? 1
              : rank.has(name) ? 0 : 2;
            const orders = [...section.querySelectorAll(".recorded-roster .maid-list")].map(list => {
              const actual = [...list.querySelectorAll(".maid-name")].map(node => node.dataset.name);
              const expected = [...actual].sort((a, b) => category(a) - category(b) ||
                ((category(a) === 0 || category(a) === 3) ? (rank.get(a) ?? rank.size) - (rank.get(b) ?? rank.size) : 0) ||
                a.localeCompare(b, "ja"));
              return {actual, expected};
            });
            return { shown: shown.sort(), expected: expected.sort(), sources: sourceLinks.sort(),
              expectedSources: expectedLinks.sort(), details: details?.textContent ?? "",
              marked: marked.sort(), expectedMarked: expectedMarked.sort(), orders };
          });
        })()`);
        for (const [index, result] of names.entries()) {
          assert.deepEqual(result.shown, result.expected, `${date} ${index}: person/store facts must match their own source`);
          assert.deepEqual(result.sources, result.expectedSources, `${date} ${index}: no invented or cross-shift source URL`);
          assert.deepEqual(result.marked, result.expectedMarked, `${date} ${index}: trainees use only documented metadata`);
          for (const order of result.orders) assert.deepEqual(order.actual, order.expected,
            `${date} ${index}: store members use first-service rank, trainees and kitchen`);
          if (curated) assert.equal(result.details, "", "no empty disclosure or technical curated-source prose");
        }
        if (date === "2026-09-05" && liveObserved > 0) {
          assert.equal(await evaluate('[...document.querySelectorAll("#dialog-day .recorded-roster .maid-entry")].filter(row => row.dataset.store === "s1" && row.querySelector(".maid-name").textContent === "つぼみ").length'), 1);
          assert.equal(await evaluate('[...document.querySelectorAll("#dialog-day .unmatched-roster .maid-name")].filter(node => node.textContent === "つぼみ").length'), 0);
          assert.equal(await evaluate('[...document.querySelectorAll("#dialog-day .recorded-roster .maid-name")].filter(node => node.textContent === "つぽみ").length'), 0);
          assert.doesNotMatch(await evaluate('document.querySelector("#dialog-day").textContent'), /原表記|利用者確認/);
          assert.ok(await evaluate('[...document.querySelectorAll("#dialog-day [title], #dialog-day [aria-label]")].every(node => !/原表記|利用者確認/.test((node.title || "") + (node.getAttribute("aria-label") || "")))'));
          assert.ok(await evaluate('(async () => (await (await fetch("data/observed-shifts.json")).json()).posts.find(post => post.id === "2096074325120237794").names.includes("つぽみ"))()'),
            "the displayed correction must not rewrite the published raw observations");
        }
        const headings = await evaluate('[...document.querySelectorAll("#day-dialog .maid-group-label")].map(heading => ({store:heading.dataset.store, color:getComputedStyle(heading).borderBottomColor}))');
        for (const heading of headings) {
          assert.equal(heading.color, storeColors[heading.store], "a shared store heading uses the original store token, not orange");
          checkedColors.add(heading.store);
        }
        for (const width of [1280, 390, 320]) {
          await call("Emulation.setDeviceMetricsOverride", {
            width, height: width === 1280 ? 960 : 844, deviceScaleFactor: 1, mobile: width !== 1280
          });
          await scrollShift("day");
          await evaluate('document.querySelector("#close-day-dialog").focus({preventScroll:true})');
          assert.ok(await noOverflow(), `${date} page fits ${width}px`);
          assert.ok(await evaluate('document.querySelector("#day-dialog-content").scrollWidth <= document.querySelector("#day-dialog-content").clientWidth'),
            `${date} popup content fits ${width}px`);
          assert.ok(await evaluate('[...document.querySelectorAll("#day-dialog .maid-entry")].every(row => row.getBoundingClientRect().right <= document.querySelector("#day-dialog-content").getBoundingClientRect().right)'));
          await capture(`popup-unified-${date}-${width === 1280 ? "desktop" : width === 390 ? "mobile" : "mobile-320"}`);
          await evaluate('document.querySelectorAll("#day-dialog .observation-details").forEach(details => { details.open = true; })');
          if (!curated) {
            await wait('[...document.querySelectorAll("#day-dialog .official-post")].every(node => node.dataset.embedState === "ready")');
            assert.equal(widgetRequests, 1, "one widgets.js load for the document");
            assert.equal(await evaluate('document.querySelectorAll("script[src=\\"https://platform.twitter.com/widgets.js\\"]").length'), 1);
            assert.ok(await evaluate('Object.values(window.__mockTweetCalls).every(count => count === 1)'),
              "one createTweet call per official post, including reopened details");
            assert.ok(await evaluate('window.__mockTweetOptions.theme === "light" && window.__mockTweetOptions.dnt === true'));
          }
          assert.ok(await evaluate('document.querySelector("#day-dialog-content").scrollWidth <= document.querySelector("#day-dialog-content").clientWidth'));
          if (width === 1280) await capture(`popup-unified-${date}-details`);
          await evaluate('document.querySelectorAll("#day-dialog .observation-details").forEach(details => { details.open = false; })');
        }
        await call("Emulation.setDeviceMetricsOverride", { width: 1280, height: 960, deviceScaleFactor: 1, mobile: false });
      }
      await click("#close-day-dialog");
      await wait('!document.querySelector("#day-dialog").open');
    }

    if (process.env.REQUIRE_OBSERVATIONS === "1") assert.deepEqual([...checkedColors].sort(), Object.keys(storeColors),
      "the curated/observed samples exercise all four original store colors");
    await click("#clear-all");
    await evaluate(`(() => { const selected = [...document.querySelectorAll("#maid-checkboxes input")].find(input => input.value === "つぼみ");
      selected.checked = true; selected.dispatchEvent(new Event("change")); })()`);
    await click('[data-date="2026-09-05"]');
    assert.deepEqual(await evaluate('[...document.querySelectorAll("#dialog-day .recorded-roster .maid-name")].map(node => node.textContent)'), ["つぼみ"]);
    assert.equal(await evaluate('document.querySelectorAll("#dialog-day .unmatched-roster").length'), 0);
    assert.ok(await evaluate('[...document.querySelectorAll("#dialog-day .observation-details a")].some(link => link.href.endsWith("/2096074325120237794"))'));
    await capture("popup-tsubomi-confirmed-filter");
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    await evaluate(`(() => { const mode = document.querySelector('input[name="view-mode"][value="maid"]');
      mode.checked = true; mode.dispatchEvent(new Event("change")); })()`);
    const correctedStop = await evaluate(`(() => {
      const rows = [...document.querySelectorAll('.maid-plan-stop[data-date="2026-09-05"]')]
        .filter(row => row.querySelector(".maid-plan-when").textContent.endsWith(" 昼"));
      return rows.map(row => ({evidence:row.dataset.evidence, store:row.querySelector(".maid-plan-where").dataset.store,
        source:[...row.querySelectorAll("a")].some(link => link.href.endsWith("/2096074325120237794")), title:row.title}));
    })()`);
    assert.equal(correctedStop.length, 1);
    assert.equal(correctedStop[0].evidence, "observed");
    assert.equal(correctedStop[0].store, "s1");
    assert.equal(correctedStop[0].source, true);
    assert.doesNotMatch(correctedStop[0].title, /原表記|利用者確認/);
    await click("#reset-filters");
    if (publicOrigin) {
      const stored = await evaluate('(async () => (await (await fetch("data/observed-shifts.json", {cache:"no-store"})).json()).posts.length)()');
      assert.ok(stored >= 10, "production must retain the ten verified observations");
      assert.equal(exceptions.length, 0, JSON.stringify(exceptions));
      console.log(`Public Pages smoke passed: ${publicOrigin}; ${stored} observations, unified store colors, 1280/390/320, light-only and modal controls.`);
      return;
    }

    // Curated records have no fabricated disclosure, and keep focused names on refresh.
    observationResponse = JSON.parse(fs.readFileSync(path.join(root, "data", "observed-shifts.json"), "utf8"));
    holdObservation = true;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "false"');
    await click('[data-date="2026-09-03"]');
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .observation-details").length'), 0);
    await evaluate('document.querySelector("#dialog-day .maid-name").focus({preventScroll:true})');
    const curatedDetailsBefore = await evaluate('({focus:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop})');
    observationResponse.checkedAt = "2026-09-05T15:00:01Z";
    holdObservation = false;
    assert.ok(heldObservationResponses.length > 0);
    for (const res of heldObservationResponses.splice(0)) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(observationResponse));
    }
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    assert.deepEqual(await evaluate('({focus:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop})'),
      curatedDetailsBefore, "curated name focus survives async observation refresh");
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');

    // A person record on the opposite shift switches the entire popup, but
    // actual store evidence alone still uses the existing unconfirmed view.
    const originalRecordedDay = await evaluate('window.STORE_INSIGHTS.actualRoster["2026-09-03"]');
    await evaluate(`delete window.STORE_INSIGHTS.actualRoster["2026-09-03"]["昼"];
      window.STORE_INSIGHTS.actualWithoutRoster["2026-09-03"] = {"昼": [...window.STORE_INSIGHTS.actual["2026-09-03"]["昼"]]};`);
    await click('[data-date="2026-09-03"]');
    assert.ok(await evaluate('document.querySelector("#dialog-day .unmatched-roster").textContent.includes("未発表")'));
    assert.equal(await evaluate('document.querySelectorAll("#dialog-day .store-outlook, #dialog-day .maid-group-label").length'), 0);
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .shift-day [data-evidence=recorded]").length'), 0);
    assert.equal(await evaluate('[...document.querySelectorAll("#day-dialog .shift-day .maid-entry")].some(row => /にいた記録/.test(row.title))'), false);
    assert.ok(await evaluate('document.querySelectorAll("#day-dialog .shift-night [data-evidence=recorded]").length > 0'));
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    await evaluate('delete window.STORE_INSIGHTS.actualRoster["2026-09-03"]["夜"]');
    await click('[data-date="2026-09-03"]');
    assert.ok(await evaluate('document.querySelector("#dialog-day .shift-evidence").textContent.includes("店舗のみ実績")'));
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .unmatched-roster, #day-dialog [data-evidence=recorded]").length'), 0);
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    await evaluate(`window.STORE_INSIGHTS.actualRoster["2026-09-03"] = ${JSON.stringify(originalRecordedDay)}`);

    for (const width of [390, 320]) {
      await call("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: true });
      await evaluate("window.scrollTo(0, 0)");
      assert.equal(await columns(), 7);
      assert.ok(await noOverflow(), `calendar fits ${width}px`);
      await capture(`calendar-mobile-${width}`, true);
      await click('[data-date="2026-09-08"]');
      await wait('document.querySelector("#day-dialog").open');
      assert.ok(await noOverflow());
      assert.ok(await evaluate('document.querySelector("#day-dialog-content").scrollHeight > document.querySelector("#day-dialog-content").clientHeight'));
      assert.equal(await evaluate('document.querySelectorAll(".dialog-shift-nav, #jump-day, #jump-night").length'), 0);
      await scrollShift("night");
      assert.equal(await evaluate('document.activeElement.id'), "dialog-night");
      assert.ok(await evaluate('document.querySelector("#day-dialog-content").scrollTop > 0'));
      assert.ok(await evaluate('document.querySelector("#close-day-dialog").getBoundingClientRect().top >= 0'));
      assert.ok(await evaluate('document.querySelector("#day-dialog .shift-night").getBoundingClientRect().bottom <= innerHeight'));
      await capture(`popup-night-mobile-${width}`);
      await scrollShift("day");
      assert.equal(await evaluate('document.activeElement.id'), "dialog-day");
      assert.equal(await evaluate('document.querySelector("#day-dialog-content").scrollTop'), 0);
      await capture(`popup-mobile-${width}`);
      // Browser-level pointer event, confined to the isolated headless page.
      await call("Input.dispatchMouseEvent", { type: "mousePressed", x: 2, y: 2, button: "left", clickCount: 1 });
      await call("Input.dispatchMouseEvent", { type: "mouseReleased", x: 2, y: 2, button: "left", clickCount: 1 });
      await wait('!document.querySelector("#day-dialog").open');
    }

    // Multiple hosts and an image failure must keep both names.
    await evaluate(`window.SCHEDULE_DATA.schedule["2026-09-08"]["夜"].push({name:"ちま",featured:true,eventLabel:"test event"});
      window.SCHEDULE_DATA.eventImages.events["2026-09-08"] = {"ちま": {src:"missing-image.svg",alt:"test missing image"}};`);
    await click("#current-month");
    await wait('document.querySelectorAll(\'[data-date="2026-09-08"] .event-art\').length === 2');
    await wait('[...document.querySelectorAll(\'[data-date="2026-09-08"] img\')].every(img => img.complete && img.naturalWidth > 0)');
    assert.ok(await noOverflow());
    assert.ok(await evaluate('document.querySelector(\'[data-date="2026-09-08"]\').textContent.includes("ちま")'));

    await evaluate(`document.querySelector("#date-from").value="2026-09-05";
      document.querySelector("#date-to").value="2026-09-07";
      document.querySelector("#date-from").dispatchEvent(new Event("change"));`);
    assert.equal(await evaluate('document.querySelectorAll(".day-button:not(:disabled)").length'), 3);
    assert.equal(await evaluate('document.querySelectorAll(".event-art").length'), 0);
    await click('[data-date="2026-09-08"]');
    assert.equal(await evaluate('document.querySelector("#day-dialog").open'), false);
    await click("#next-month");
    assert.equal(await evaluate('document.querySelectorAll(".day-button:not(:disabled)").length'), 0);
    assert.equal(await evaluate('document.querySelector("#date-from").value'), "2026-09-05", "explicit range survives month navigation");
    await click("#reset-filters");
    await click("#next-month");
    assert.equal(await evaluate('document.querySelector("#date-from").value'), "2026-10-01");
    assert.equal(await evaluate('document.querySelectorAll(".day-button").length'), 31);
    await click("#current-month");
    await evaluate(`document.querySelector("#clear-all").click();
      const checkbox=[...document.querySelectorAll("#maid-checkboxes input")].find(input=>input.value==="あむ");
      checkbox.checked=true; checkbox.dispatchEvent(new Event("change"));`);
    await click('[data-date="2026-09-08"]');
    assert.ok(await evaluate('[...document.querySelectorAll("#day-dialog .maid-name")].every(node=>node.textContent==="あむ")'));
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');

    // Offline partial-observation integration: one shop/person is not a full shift.
    observationResponse = {
      schemaVersion: 1, complete: false, checkedAt: "2026-09-05T15:00:00Z",
      lastSuccessAt: "2026-09-05T15:00:00Z", pending: [{ id: "2096000000000000002" }],
      lastRun: { status: "partial", dateFrom: "2026-09-04", dateTo: "2026-09-05" },
      posts: [{
        id: "2096000000000000001", url: "https://x.com/akibazettai/status/2096000000000000001",
        authorId: "822429861218131969", authorScreenName: "akibazettai",
        createdAt: "2026-09-05T03:00:00Z", observedAt: "2026-09-05T15:00:00Z",
        date: "2026-09-05", shift: "昼", storeId: "s2", names: ["あむ", "もな"]
      }]
    };
    await click("#reset-filters");
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true" && document.querySelector("#observation-status").textContent.includes("一部失敗")');
    await click('[data-date="2026-09-05"]');
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .shift-day [data-evidence=observed]").length'), 2);
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .shift-day .maid-group-label").length'), 1);
    assert.equal(await evaluate('document.querySelector("#day-dialog .shift-day .maid-group-label").dataset.store'), "s2");
    assert.doesNotMatch(await evaluate('document.querySelector("#day-dialog .recorded-roster").textContent'),
      /[1-4]号店[：:\s]*未確認|休業|休み|0人/, "do not display missing shops as unknown, closed or empty");
    assert.deepEqual(await evaluate('[...document.querySelectorAll("#day-dialog .shift-day [data-evidence=observed]")].map(el => el.textContent)'), ["あむ", "もな"]);
    assert.ok(await evaluate('document.querySelector("#day-dialog .observation-details").open === false'));
    assert.ok(await evaluate('!document.querySelector("#day-dialog").textContent.includes("部分観測")'));
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .shift-night [data-evidence=observed]").length'), 0);
    assert.ok(await evaluate('document.querySelector("#day-dialog .shift-day").textContent.includes("未発表")'));
    assert.ok(await evaluate('document.querySelector("#dialog-night .unmatched-roster").textContent.includes("未発表")'));
    assert.ok(await evaluate('document.querySelectorAll("#day-dialog .shift-day [data-evidence=scheduled]").length > 0'));
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .shift-day [data-evidence=recorded]").length'), 0);
    const assertDailyUnknown = async (observedByShift) => {
      const results = await evaluate(`["昼", "夜"].map((shift, index) => {
        const section = document.querySelector(index ? "#dialog-night" : "#dialog-day");
        const confirmed = ${JSON.stringify(observedByShift)}[index];
        const planned = window.SCHEDULE_DATA.schedule["2026-09-05"]?.[shift] ?? [];
        const expected = planned.map(entry => entry.name).filter(name => !confirmed.includes(name)).sort();
        const frames = [...section.querySelectorAll(".unmatched-roster")];
        const names = frames.flatMap(frame => [...frame.querySelectorAll(".maid-name")].map(node => node.dataset.name));
        const styles = frames.map(frame => {
          const style = getComputedStyle(frame);
          return {width:style.borderTopWidth,style:style.borderTopStyle,color:style.borderTopColor,
            neutral:getComputedStyle(document.documentElement).getPropertyValue("--cp-border-strong").trim(),
            padding:style.paddingTop};
        });
        return {expected, names:names.sort(), frames:frames.length, styles,
          placed:[...section.querySelectorAll(".recorded-roster .maid-name")].map(node => node.dataset.name).sort(),
          guesses:section.querySelectorAll(".store-outlook, .store-status-badge, .maid-store-chip, .is-trainee-guess").length,
          unknownStores:frames.flatMap(frame => [...frame.querySelectorAll("[data-store]")]).length};
      })`);
      for (const [index, result] of results.entries()) {
        assert.deepEqual(result.names, result.expected, "preserve this shift's planned names without confirmed duplicates");
        assert.deepEqual(result.placed, [...observedByShift[index]].sort());
        assert.equal(result.frames, result.expected.length ? 1 : 0, "one nonempty unknown frame per shift");
        assert.equal(result.guesses, 0, "no forecast stores, odds or unnamed trainees once any person is confirmed");
        assert.equal(result.unknownStores, 0, "unknown people do not inherit a store");
        for (const style of result.styles) {
          assert.equal(style.width, "1px");
          assert.equal(style.style, "solid");
          assert.equal(style.color, "rgb(145, 145, 145)", "neutral border, not one of the four store colors");
          assert.ok(parseFloat(style.padding) >= 12);
        }
      }
    };
    await assertDailyUnknown([["あむ", "もな"], []]);
    assert.ok(await noOverflow());
    await capture("popup-partial-observation-fixture");
    const captureDailyFrame = async (name) => {
      for (const width of [1280, 390, 320]) {
        await call("Emulation.setDeviceMetricsOverride", {
          width, height: width === 1280 ? 960 : 844, deviceScaleFactor: 1, mobile: width !== 1280
        });
        await scrollShift("day");
        await evaluate('document.querySelector("#close-day-dialog").focus({preventScroll:true})');
        assert.ok(await noOverflow());
        assert.ok(await evaluate('document.querySelector("#day-dialog-content").scrollWidth <= document.querySelector("#day-dialog-content").clientWidth'));
        await capture(`${name}-${width}`);
      }
    };
    await captureDailyFrame("popup-daily-day-only");
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    const dayOnly = JSON.parse(JSON.stringify(observationResponse));
    observationResponse.posts[0].shift = "夜";
    observationResponse.posts[0].names = ["あむ"];
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await click('[data-date="2026-09-05"]');
    await assertDailyUnknown([[], ["あむ"]]);
    await captureDailyFrame("popup-daily-night-only");
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    observationResponse = dayOnly;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await click("#clear-all");
    await evaluate(`(() => { const selected = [...document.querySelectorAll("#maid-checkboxes input")]
      .find(input => input.value === "かなた"); selected.checked = true; selected.dispatchEvent(new Event("change")); })()`);
    await click('[data-date="2026-09-05"]');
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .recorded-roster, #day-dialog .store-outlook, #day-dialog .maid-group-label").length'), 0,
      "hiding the sole confirmed group must not bring guesses back");
    assert.ok(await evaluate('document.querySelectorAll("#day-dialog .unmatched-roster .maid-name").length > 0'));
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    await click("#clear-all");
    await click('[data-date="2026-09-05"]');
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .recorded-roster, #day-dialog .unmatched-roster, #day-dialog .maid-name").length'), 0,
      "an empty filter must not leave an empty frame or source disclosure");
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    await click("#reset-filters");
    holdObservation = true;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "false"');
    await click('[data-date="2026-09-05"]');
    await scrollShift("night");
    await evaluate('document.querySelector("#dialog-night .maid-name").focus({preventScroll:true})');
    const activeBeforeRefresh = await evaluate('({focus:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop,title:document.querySelector("#day-dialog-title").textContent})');
    observationResponse.checkedAt = "2026-09-05T15:01:00Z";
    holdObservation = false;
    assert.ok(heldObservationResponses.length > 0);
    for (const res of heldObservationResponses.splice(0)) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(observationResponse));
    }
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    assert.deepEqual(await evaluate('({focus:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop,title:document.querySelector("#day-dialog-title").textContent})'),
      activeBeforeRefresh, "async snapshot arrival must preserve selected date, scroll and focused person");
    assert.ok(await evaluate('document.querySelector("#day-dialog").open'));
    assert.equal(await evaluate('document.body.style.overflow'), "hidden");
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    assert.equal(await evaluate('document.activeElement.dataset.date'), "2026-09-05");
    assert.equal(await evaluate('document.body.style.overflow'), "");
    holdObservation = true;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "false"');
    await click('[data-date="2026-09-05"]');
    await click("#dialog-day .observation-details summary");
    await evaluate('document.querySelector("#dialog-day .observation-details summary").focus({preventScroll:true}); document.querySelector("#day-dialog-content").scrollTop=80');
    const detailsBefore = await evaluate('({open:document.querySelector("#dialog-day .observation-details").open,focus:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop})');
    observationResponse.checkedAt = "2026-09-05T15:02:00Z";
    holdObservation = false;
    for (const res of heldObservationResponses.splice(0)) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(observationResponse));
    }
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    assert.deepEqual(await evaluate('({open:document.querySelector("#dialog-day .observation-details").open,focus:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop})'),
      detailsBefore, "expanded update information and its focused summary must survive metadata-only refresh");
    assert.equal(await evaluate('document.activeElement.tagName'), "SUMMARY");
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    await evaluate(`const mode = document.querySelector('input[name="view-mode"][value="maid"]');
      mode.checked = true; mode.dispatchEvent(new Event("change"));`);
    assert.ok(await evaluate('document.querySelectorAll(".maid-plan-stop[data-date=\\"2026-09-05\\"][data-evidence=observed]").length === 2'));
    assert.ok(await evaluate('document.querySelectorAll(".maid-plan-stop[data-date=\\"2026-09-05\\"][data-evidence=scheduled]").length > 0'));
    observationFailure = true;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").textContent.includes("HTTP 503")');
    assert.equal(await evaluate('document.querySelectorAll(".maid-plan-stop[data-date=\\"2026-09-05\\"][data-evidence=observed]").length'), 2,
      "failed loading must retain the previous observation snapshot");
    observationFailure = false;
    observationResponse.lastRun.status = "no-new";
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").textContent.includes("新規追加なし")');
    assert.equal(await evaluate('document.querySelectorAll(".maid-plan-stop[data-date=\\"2026-09-05\\"][data-evidence=observed]").length'), 2);
    observationResponse.posts[0].date = "2026-09-01";
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    assert.equal(await evaluate('document.querySelectorAll(".maid-plan-stop[data-date=\\"2026-09-01\\"][data-evidence=observed]").length'), 0,
      "a curated roster must take precedence");

    // Personal same-day notices: offline-only, separate readiness and source identity.
    observationResponse = JSON.parse(fs.readFileSync(path.join(root, "data", "observed-shifts.json"), "utf8"));
    observationResponse.posts = observationResponse.posts.filter(post => post.date !== "2026-09-06" || post.shift === "昼");
    const personalSeed = {
      schemaVersion: 1, complete: false, checkedAt: "2026-09-06T13:00:00+09:00",
      lastSuccessAt: "2026-09-06T13:00:00+09:00", lastRun: { status: "ok" }, posts: [
        { id: "2096252018260062487", url: "https://x.com/amu_zettai/status/2096252018260062487",
          name: "あむ", authorId: "1180156105181159424", authorScreenName: "amu_zettai",
          createdAt: "2026-09-06T00:00:02+09:00", observedAt: "2026-09-06T13:00:00+09:00",
          date: "2026-09-06", events: [
            { shift: "昼", kind: "placement", storeId: "s1", excerpt: "昼は1号店" },
            { shift: "夜", kind: "placement", storeId: "s2", excerpt: "夜は2号店" }
          ] },
        { id: "2096253883677044837", url: "https://x.com/rarako_zettai/status/2096253883677044837",
          name: "ららこ", authorId: "2065375500131028992", authorScreenName: "rarako_zettai",
          createdAt: "2026-09-06T00:07:27+09:00", observedAt: "2026-09-06T13:00:00+09:00",
          date: "2026-09-06", events: [
            { shift: "昼", kind: "placement", storeId: "s1", excerpt: "1号店➡️2号店 12-22。2→1だと勘違いしていた" }
          ] }
      ]
    };
    personalResponse = JSON.parse(JSON.stringify(personalSeed));
    const setMode = (value) => evaluate(`(() => {
      const input = document.querySelector('input[name="view-mode"][value="${value}"]');
      input.checked=true; input.dispatchEvent(new Event("change"));
    })()`);
    await click("#reset-filters");
    await setMode("calendar");
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await click("#refresh-personal");
    await wait('document.querySelector("#personal-status").dataset.loaded === "true" && document.querySelector("#personal-status").dataset.error === "false"');
    const sourceBaseline = await evaluate('JSON.stringify([window.SCHEDULE_DATA.schedule,window.STORE_INSIGHTS.actual,window.STORE_INSIGHTS.actualRoster])');
    await click('[data-date="2026-09-06"]');
    const todayNames = await evaluate(`["#dialog-day","#dialog-night"].map(selector => {
      const section=document.querySelector(selector);
      return [...section.querySelectorAll(".maid-entry")].map(row => ({
        name:row.dataset.name,store:row.dataset.store || null,
        evidence:row.dataset.evidence,title:row.title,
        href:row.querySelector(".maid-name").getAttribute("href"),
        rel:row.querySelector(".maid-name").getAttribute("rel")
      }));
    })`);
    const amuNight = todayNames[1].filter(row => row.name === "あむ");
    assert.equal(amuNight.length, 1);
    assert.equal(amuNight[0].store, "s2");
    assert.equal(amuNight[0].evidence, "personal");
    assert.equal(amuNight[0].href, personalSeed.posts[0].url);
    assert.equal(amuNight[0].rel, "noopener noreferrer");
    assert.match(amuNight[0].title, /お給仕予定/);
    assert.doesNotMatch(amuNight[0].title, /本人|実績/);
    assert.equal(todayNames[1].find(row => row.name === "ららこ").store, "s2");
    assert.equal(todayNames[1].find(row => row.name === "ららこ").href, personalSeed.posts[1].url);
    assert.ok(todayNames.every(rows => new Set(rows.map(row => row.name)).size === rows.length));
    if (observationResponse.posts.some(post => post.date === "2026-09-06" && post.shift === "昼")) {
      assert.equal(todayNames[0].filter(row => row.name === "あむ").length, 1);
      assert.equal(todayNames[0].find(row => row.name === "あむ").evidence, "observed");
    }
    assert.equal(await evaluate('document.querySelectorAll("#dialog-night .observation-details").length'), 0,
      "a personal-only placement must not acquire a collection-source heading");
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .personal-details, #day-dialog .personal-source-link").length'), 0);
    assert.doesNotMatch(await evaluate('document.querySelector("#day-dialog").textContent'), /本人ポスト|本人案内|本人の当日案内/);
    await captureDailyFrame("popup-personal-2026-09-06");
    await scrollShift("night");
    await capture("popup-personal-night-mobile-320");

    holdPersonal = true;
    await click("#close-day-dialog");
    await click("#refresh-personal");
    await wait('document.querySelector("#personal-status").dataset.loaded === "false"');
    assert.equal(await evaluate('document.querySelector("#observation-status").dataset.loaded'), "true",
      "personal loading must not reset official readiness");
    await click('[data-date="2026-09-06"]');
    await scrollShift("night");
    await evaluate('document.querySelector("#dialog-night .maid-name[data-name=\\"あむ\\"]").focus({preventScroll:true})');
    const interactionBefore = await evaluate('({key:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop})');
    personalResponse.checkedAt = "2026-09-06T13:01:00+09:00";
    holdPersonal = false;
    assert.ok(heldPersonalResponses.length > 0);
    for (const res of heldPersonalResponses.splice(0)) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(personalResponse));
    }
    await wait('document.querySelector("#personal-status").dataset.loaded === "true"');
    assert.deepEqual(await evaluate('({key:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop})'),
      interactionBefore, "personal refresh must preserve focused person and scroll");
    const originalOfficial = observationResponse;
    const collectionPost = (shift, storeId, names, id) => ({
      id, url: `https://x.com/akibazettai/status/${id}`, authorId: "822429861218131969",
      authorScreenName: "akibazettai", createdAt: "2026-09-06T03:00:00Z",
      observedAt: "2026-09-06T04:00:00Z", date: "2026-09-06", shift, storeId, names
    });
    observationResponse = { ...originalOfficial, posts: [
      ...originalOfficial.posts.filter(post => post.date !== "2026-09-06"),
      collectionPost("昼", "s1", ["あむ", "ららこ"], "2096550000000000001"),
      collectionPost("夜", "s4", ["あむ"], "2096550000000000002")
    ] };
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    assert.equal(await evaluate('document.querySelectorAll("#dialog-day [data-evidence=observed]").length'), 2);
    assert.equal(await evaluate('document.querySelectorAll("#dialog-day [data-evidence=personal]").length'), 0,
      "matching personal and collection sources share one person row");
    assert.equal(await evaluate('document.querySelector("#dialog-day .maid-name[data-name=\\"あむ\\"]").getAttribute("href")'), personalSeed.posts[0].url);
    assert.equal(await evaluate('document.querySelectorAll("#dialog-night [data-evidence=pending]").length'), 0);
    assert.equal(await evaluate('document.querySelectorAll("#dialog-night .maid-entry[data-name=\\"あむ\\"][data-evidence=personal], #dialog-night [data-evidence=observed]").length'), 1,
      "the later collection supersedes an earlier personal placement");
    assert.equal(await evaluate('document.querySelector("#dialog-night .maid-entry[data-name=\\"あむ\\"]").dataset.store'), "s4");
    assert.equal(await evaluate('document.activeElement.dataset.focusKey'), interactionBefore.key,
      "official refresh also preserves the focused person");
    observationResponse = originalOfficial;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    assert.equal(await evaluate('document.querySelectorAll("#dialog-night [data-evidence=personal]").length'), 2);
    await click("#close-day-dialog");
    for (const code of [404, 503]) {
      personalFailure = code;
      await click("#refresh-personal");
      await wait(`document.querySelector("#personal-status").textContent.includes("HTTP ${code}")`);
      await click('[data-date="2026-09-06"]');
      assert.equal(await evaluate('document.querySelectorAll("#dialog-night [data-evidence=personal]").length'), 2);
      assert.deepEqual(await evaluate('[...document.querySelectorAll("#dialog-day [data-evidence=observed] .maid-name")].map(row=>row.dataset.name).sort()'),
        todayNames[0].filter(row => row.evidence === "observed").map(row => row.name).sort());
      await click("#close-day-dialog");
    }
    personalFailure = 0;
    personalResponse = { ...personalSeed, complete: true };
    await click("#refresh-personal");
    await wait('document.querySelector("#personal-status").dataset.error === "true" && !document.querySelector("#personal-status").textContent.includes("HTTP")');
    await click('[data-date="2026-09-06"]');
    assert.equal(await evaluate('document.querySelectorAll("#dialog-night [data-evidence=personal]").length'), 2);
    await click("#close-day-dialog");
    personalResponse = JSON.parse(JSON.stringify(personalSeed));
    personalResponse.lastRun.status = "budget-exhausted";
    await click("#refresh-personal");
    await wait('document.querySelector("#personal-status").textContent.includes("取得予算待ち")');
    await setMode("maid");
    const personalStops = await evaluate('[...document.querySelectorAll(".maid-plan-stop[data-date=\\"2026-09-06\\"][data-evidence=personal]")].map(row => ({text:row.textContent,title:row.title,rate:row.querySelector(".maid-plan-rate")?.textContent}))');
    assert.ok(personalStops.some(row => row.text.includes("2号店") && row.text.includes("お給仕予定")));
    assert.ok(personalStops.every(row => !row.rate && row.title.includes("お給仕予定") && !/本人|にいた記録|勤務実績/.test(row.title)));
    await evaluate(`document.querySelector("#date-from").value="2026-09-07";
      document.querySelector("#date-to").value="2026-09-08";
      document.querySelector("#date-from").dispatchEvent(new Event("change"));`);
    assert.equal(await evaluate('document.querySelectorAll(".maid-plan-stop[data-date=\\"2026-09-06\\"]").length'), 0);
    await click("#reset-filters");
    await setMode("calendar");

    const personalChange = (kind, suffix, storeId) => ({
      ...personalSeed.posts[0], id: `209660000000000000${suffix}`,
      url: `https://x.com/amu_zettai/status/209660000000000000${suffix}`,
      createdAt: `2026-09-06T${15 + suffix}:00:00+09:00`,
      events: [{ shift: "夜", kind, ...(storeId ? { storeId } : {}), excerpt: `${kind}の明示変更` }]
    });
    personalResponse.posts.push(personalChange("absence", 1));
    await click("#refresh-personal");
    await wait('document.querySelector("#personal-status").dataset.loaded === "true"');
    await click('[data-date="2026-09-06"]');
    assert.equal(await evaluate('[...document.querySelectorAll("#dialog-night .maid-name")].some(row => row.textContent === "あむ")'), false);
    assert.ok(await evaluate('[...document.querySelectorAll("#dialog-night .shift-change")].some(node => node.textContent.includes("あむ：取消"))'));
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .store-outlook, #day-dialog .is-trainee-guess").length'), 0);
    await click("#close-day-dialog");
    personalResponse.posts.push(personalChange("return", 2));
    await click("#refresh-personal");
    await wait('document.querySelector("#personal-status").dataset.loaded === "true"');
    await click('[data-date="2026-09-06"]');
    assert.ok(await evaluate('[...document.querySelectorAll("#dialog-night .unmatched-roster .maid-name")].some(row => row.textContent === "あむ")'));
    assert.equal(await evaluate('document.querySelectorAll("#dialog-night .maid-entry[data-name=\\"あむ\\"][data-evidence=personal]").length'), 0);
    await click("#close-day-dialog");
    personalResponse.posts.push(personalChange("return", 3, "s4"));
    await click("#refresh-personal");
    await wait('document.querySelector("#personal-status").dataset.loaded === "true"');
    await click('[data-date="2026-09-06"]');
    assert.equal(await evaluate('document.querySelector("#dialog-night .maid-entry[data-name=\\"あむ\\"][data-evidence=personal]").dataset.store'), "s4");
    assert.equal(await evaluate('document.querySelector("#dialog-night .maid-name[data-name=\\"あむ\\"]").getAttribute("href")'),
      personalChange("return", 3, "s4").url, "the latest explicit return owns the quiet name link");
    await click("#close-day-dialog");
    assert.equal(await evaluate('JSON.stringify([window.SCHEDULE_DATA.schedule,window.STORE_INSIGHTS.actual,window.STORE_INSIGHTS.actualRoster])'), sourceBaseline);

    // Three-post runtime fixture; the committed offline seed can still contain two.
    observationResponse = originalOfficial;
    personalResponse = JSON.parse(JSON.stringify(personalSeed));
    personalResponse.posts.push({
      ...personalSeed.posts[0], id: "2096600000000000044",
      url: "https://x.com/fixture_ameru/status/2096600000000000044",
      name: "あめる", authorId: "1000000000000000044", authorScreenName: "fixture_ameru",
      events: [{ shift: "夜", kind: "placement", storeId: "s4", excerpt: "夜は4号店" }]
    });
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await click("#refresh-personal");
    await wait('document.querySelector("#personal-status").dataset.loaded === "true"');
    await click('[data-date="2026-09-06"]');
    for (const [name, store] of [["あむ", "s2"], ["ららこ", "s2"], ["あめる", "s4"]]) {
      assert.deepEqual(await evaluate(`(() => {
        const rows = [...document.querySelectorAll("#dialog-night .maid-entry")]
          .filter(row => row.dataset.name === ${JSON.stringify(name)});
        return rows.map(row => [row.dataset.store, row.dataset.evidence]);
      })()`), [[store, "personal"]]);
    }
    await captureDailyFrame("popup-three-posts-2026-09-06");
    await click("#close-day-dialog");

    // Optional official late notices use current facts, never the revision's roster.
    const officialLate = {
      ...collectionPost("昼", "s4", ["るるか", "ちぇる", "まこと"], "2096436633973526890"),
      notices: [{ name: "みりあ", kind: "late", excerpt: "みりあちゃんもあとから来るにゃんね" }],
      lastCheckedAt: "2026-09-06T04:15:00.123456Z",
      revisions: [{ date: "2026-09-06", shift: "昼", storeId: "s4", names: ["るるか"],
        notices: [], observedAt: "2026-09-06T03:15:00Z" }]
    };
    observationResponse = { ...originalOfficial, posts: [
      ...originalOfficial.posts.filter(post => post.date !== "2026-09-06"), officialLate
    ] };
    personalResponse = JSON.parse(JSON.stringify(personalSeed));
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await click("#refresh-personal");
    await wait('document.querySelector("#personal-status").dataset.loaded === "true"');
    await click('[data-date="2026-09-06"]');
    assert.ok(await evaluate(`(() => {
      const row = document.querySelector('#dialog-day .maid-entry[data-name="みりあ"]');
      return row.dataset.store === "s4" && row.dataset.evidence === "official-announced"
        && row.textContent.includes("あとから") && !/実績|確認/.test(row.title);
    })()`));
    assert.equal(await evaluate('document.querySelectorAll("#dialog-day .unmatched-roster [data-name=\\"みりあ\\"]").length'), 0);
    assert.ok(await evaluate(`(() => {
      const row = document.querySelector('#dialog-day .maid-entry[data-name="まこっちゃん"]');
      return row.classList.contains("is-kitchen") && row.textContent.includes("まこと")
        && !/まこっちゃん/.test(row.textContent + row.title + row.getAttribute("aria-label"));
    })()`));
    await captureDailyFrame("popup-refined-2026-09-06");
    await click("#close-day-dialog");
    await click("#clear-all");
    await evaluate(`(() => {
      const input = [...document.querySelectorAll("#maid-checkboxes input")].find(node => node.value === "みりあ");
      input.checked = true; input.dispatchEvent(new Event("change"));
    })()`);
    await click('[data-date="2026-09-06"]');
    assert.equal(await evaluate('document.querySelectorAll("#dialog-day .maid-entry").length'), 1);
    assert.equal(await evaluate('document.querySelector("#dialog-day .observation-source").href'), officialLate.url);
    await click("#dialog-day .observation-details summary");
    await wait('document.querySelector("#dialog-day .official-post").dataset.embedState === "ready"');
    await wait('window.__mockTweetLoads["2096436633973526890"] > 0');
    for (const width of [1280, 390, 320]) {
      await call("Emulation.setDeviceMetricsOverride", {
        width, height: width === 1280 ? 960 : 844, deviceScaleFactor: 1, mobile: width !== 1280
      });
      assert.ok(await noOverflow());
      assert.ok(await evaluate('document.querySelector("#day-dialog-content").scrollWidth <= document.querySelector("#day-dialog-content").clientWidth'),
        `official iframe fits ${width}px`);
      assert.ok(await evaluate(`(() => {
        const post = document.querySelector("#dialog-day .official-post");
        const mount = post.querySelector(".official-embed");
        const frame = post.querySelector("iframe");
        const link = post.querySelector(".observation-source");
        const bounds = post.getBoundingClientRect(), rect = frame.getBoundingClientRect();
        return post.querySelectorAll("a").length === 1 && mount.inert &&
          mount.getAttribute("aria-hidden") === "true" && frame.tabIndex === -1 &&
          getComputedStyle(frame).pointerEvents === "none" &&
          rect.left >= bounds.left && rect.right <= bounds.right &&
          document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) === link;
      })()`), `the original link alone covers the inert widget within ${width}px bounds`);
      await capture(`popup-official-late-widget-${width}`);
    }
    const sourceLabel = await evaluate('document.querySelector("#dialog-day .observation-source").getAttribute("aria-label")');
    const accessible = await call("Accessibility.getFullAXTree");
    assert.equal(accessible.nodes.filter(node => node.role?.value === "link" && node.name?.value === sourceLabel).length, 1,
      "assistive technology gets one original-post link, not a second embedded control surface");
    assert.ok(!accessible.nodes.some(node => !node.ignored && node.name?.value === "Widget action"));
    const sourcePoint = await evaluate(`(() => {
      const post = document.querySelector("#dialog-day .official-post");
      const link = post.querySelector(".observation-source");
      window.__sourceActivations = [];
      link.addEventListener("click", event => {
        event.preventDefault();
        window.__sourceActivations.push({href:link.href, trusted:event.isTrusted});
      });
      const rect = post.querySelector("iframe").getBoundingClientRect();
      return {x:rect.left + rect.width / 2, y:rect.top + rect.height / 2};
    })()`);
    for (const type of ["mousePressed", "mouseReleased"]) {
      await call("Input.dispatchMouseEvent", { type, ...sourcePoint, button: "left", clickCount: 1 });
    }
    assert.equal(await evaluate('document.activeElement.classList.contains("observation-source")'), true);
    await key("Enter", "Enter", 13);
    assert.deepEqual(await evaluate('window.__sourceActivations'), [
      { href: officialLate.url, trusted: true }, { href: officialLate.url, trusted: true }
    ], "pointer and Enter activate the single original-post link without external HTTP in the test");
    assert.equal(await evaluate('window.__mockWidgetClicks || 0'), 0);
    await capture("popup-official-display-surface-320");
    await evaluate('document.querySelector("#dialog-day .observation-details summary").focus({preventScroll:true})');
    await key("Tab", "Tab", 9);
    assert.equal(await evaluate('document.activeElement.classList.contains("observation-source")'), true);
    await key("Tab", "Tab", 9);
    assert.equal(await evaluate('document.activeElement.id'), "close-day-dialog",
      "Tab skips the widget and wraps to the modal's close control");
    await key("Tab", "Tab", 9, 8);
    assert.equal(await evaluate('document.activeElement.classList.contains("observation-source")'), true,
      "Shift+Tab wraps to the single source link, never the iframe");
    await evaluate(`window.__retainedFrame = document.querySelector("#dialog-day iframe");
      window.__retainedFrameWindow = window.__retainedFrame.contentWindow;
      window.__retainedSection = document.querySelector("#dialog-day");
      document.querySelector("#day-dialog-content").style.maxHeight = "300px";
      document.querySelector("#dialog-day .observation-details summary").focus({preventScroll:true});
      document.querySelector("#day-dialog-content").scrollTop = 70;`);
    const widgetInteraction = await evaluate(`({
      focus:document.activeElement.dataset.focusKey, scroll:document.querySelector("#day-dialog-content").scrollTop,
      loads:window.__mockTweetLoads["2096436633973526890"]
    })`);
    assert.ok(widgetInteraction.scroll > 0);
    officialLate.lastCheckedAt = "2026-09-06T04:30:00Z";
    officialLate.revisions.push({ ...officialLate.revisions[0], observedAt: "2026-09-06T04:20:00Z" });
    observationResponse.checkedAt = "2026-09-06T04:30:00Z";
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    assert.ok(await evaluate(`document.querySelector("#dialog-day") === window.__retainedSection &&
      document.querySelector("#dialog-day iframe") === window.__retainedFrame &&
      window.__retainedFrame.contentWindow === window.__retainedFrameWindow &&
      document.querySelector("#dialog-day .observation-details").open`));
    assert.deepEqual(await evaluate(`({
      focus:document.activeElement.dataset.focusKey, scroll:document.querySelector("#day-dialog-content").scrollTop,
      loads:window.__mockTweetLoads["2096436633973526890"]
    })`), widgetInteraction, "metadata refresh retains iframe, browsing context, disclosure, focus and scroll");
    assert.equal(await evaluate('window.__mockTweetCalls["2096436633973526890"]'), 1);
    await evaluate('document.querySelector("#day-dialog-content").style.maxHeight = ""');
    await evaluate('document.querySelector("#dialog-day .observation-source").focus({preventScroll:true})');
    await evaluate('document.querySelector("#dialog-day iframe").focus()');
    assert.equal(await evaluate('document.activeElement.classList.contains("observation-source")'), true,
      "even a programmatic iframe focus request cannot leave the parent modal");
    await key("Escape", "Escape", 27);
    await wait('!document.querySelector("#day-dialog").open');
    assert.equal(await evaluate('document.activeElement.dataset.date'), "2026-09-06");
    await click('[data-date="2026-09-06"]');
    assert.ok(await evaluate('document.querySelector("#dialog-day iframe") === window.__retainedFrame'));
    await click("#close-day-dialog");
    await setMode("forecast");
    await evaluate(`window.__legacyFrame = document.querySelector("#calendar .official-post iframe");
      window.__legacyWindow = window.__legacyFrame.contentWindow; true;`);
    observationResponse.checkedAt = "2026-09-06T04:31:00Z";
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    assert.ok(await evaluate('document.querySelector("#calendar .official-post iframe") === window.__legacyFrame && window.__legacyFrame.contentWindow === window.__legacyWindow'),
      "metadata refresh also preserves embedded browsing contexts in the store view");
    await setMode("calendar");
    await click('[data-date="2026-09-06"]');
    assert.equal(await evaluate('document.querySelector("#dialog-day .observation-source").href'), officialLate.url,
      "returning from the store view restores the cached source into the dialog");
    assert.equal(await evaluate('document.querySelectorAll("#dialog-day iframe").length'), 1);
    assert.equal(await evaluate('window.__mockTweetCalls["2096436633973526890"]'), 1);
    await click("#close-day-dialog");
    await setMode("maid");
    assert.equal(await evaluate('document.querySelector(".maid-plan-stop[data-date=\\"2026-09-06\\"]").dataset.evidence'), "official-announced");
    assert.doesNotMatch(await evaluate('document.querySelector(".maid-plan-stop[data-date=\\"2026-09-06\\"]").title'), /実績|確認/);
    await click("#reset-filters");
    await setMode("calendar");
    const oldVersion = { ...officialLate, editTweetIds: [officialLate.id] };
    const middleVersion = {
      ...officialLate, id: "2096436633973526891",
      url: "https://x.com/akibazettai/status/2096436633973526891",
      createdAt: "2026-09-06T04:30:00Z", observedAt: "2026-09-06T04:35:00Z",
      lastCheckedAt: "2026-09-06T04:35:00Z",
      names: ["ちぇる", "みりあ"], notices: [],
      editTweetIds: [oldVersion.id, "2096436633973526891"]
    };
    const latestVersion = {
      ...officialLate, id: "2096436633973526892",
      url: "https://x.com/akibazettai/status/2096436633973526892",
      createdAt: "2026-09-06T04:40:00Z", observedAt: "2026-09-06T04:45:00Z",
      lastCheckedAt: "2026-09-06T04:45:00Z",
      names: ["みりあ"], notices: [],
      editTweetIds: [middleVersion.id, "2096436633973526892"]
    };
    observationResponse.posts = [...originalOfficial.posts.filter(post => post.date !== "2026-09-06"), oldVersion];
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await click('[data-date="2026-09-06"]');
    await evaluate('document.querySelector("#dialog-day .observation-details").open = true');
    await wait('document.querySelector("#dialog-day .official-post").dataset.embedState === "ready"');
    assert.equal(await evaluate('document.querySelector("#dialog-day iframe").dataset.postId'), oldVersion.id);
    observationResponse.posts.push(middleVersion);
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await wait('document.querySelector("#dialog-day .official-post").dataset.embedState === "ready"');
    assert.deepEqual(await evaluate('[...document.querySelectorAll("#dialog-day .official-post")].map(node => node.dataset.postId)'), [middleVersion.id]);
    assert.deepEqual(await evaluate('[...document.querySelectorAll("#dialog-day iframe")].map(node => node.dataset.postId)'), [middleVersion.id]);
    const versionSnapshot = { ...observationResponse, posts: [...observationResponse.posts, latestVersion] };
    observationResponse = versionSnapshot;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await wait('document.querySelector("#dialog-day .official-post").dataset.embedState === "ready"');
    assert.deepEqual(await evaluate('[...document.querySelectorAll("#dialog-day .official-post")].map(node => node.dataset.postId)'), [latestVersion.id]);
    assert.deepEqual(await evaluate('[...document.querySelectorAll("#dialog-day iframe")].map(node => node.dataset.postId)'), [latestVersion.id],
      "partial B-to-C metadata must not restore A or B's old widget");
    assert.equal(await evaluate('document.querySelector("#dialog-day .observation-source").href'), latestVersion.url);
    assert.equal(await evaluate('document.querySelector("#dialog-day .maid-entry[data-name=\\"みりあ\\"]").dataset.evidence'), "observed");
    assert.deepEqual(await evaluate('[...document.querySelectorAll("#dialog-day .maid-entry[data-evidence=observed]")].map(node => node.dataset.name)'), ["みりあ"],
      "old versions' people remain excluded from the active roster");
    assert.doesNotMatch(await evaluate('document.querySelector("#dialog-day .maid-entry[data-name=\\"みりあ\\"]").textContent'), /あとから/);
    assert.deepEqual(await evaluate('(async () => (await (await fetch("data/observed-shifts.json")).json()).posts.filter(post => post.date === "2026-09-06").map(post => post.id))()'),
      [oldVersion.id, middleVersion.id, latestVersion.id], "the published raw history still contains all three versions");
    observationResponse = { ...versionSnapshot, posts: [{ ...oldVersion, editTweetIds: latestVersion.editTweetIds }] };
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").textContent.includes("読込に失敗")');
    assert.equal(await evaluate('document.querySelector("#dialog-day .observation-source").href'), latestVersion.url,
      "a stale current-ID chain cannot overwrite the last validated presentation");
    observationResponse = versionSnapshot;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true" && !document.querySelector("#observation-status").textContent.includes("読込に失敗")');
    await click("#close-day-dialog");
    const unavailablePost = { ...officialLate, id: "2096436633973526893",
      url: "https://x.com/akibazettai/status/2096436633973526893" };
    observationResponse.posts = [...originalOfficial.posts.filter(post => post.date !== "2026-09-06"), unavailablePost];
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await evaluate('window.twttr.widgets.createTweet = async () => { throw new Error("mock factory unavailable"); }');
    await click('[data-date="2026-09-06"]');
    await click("#dialog-day .observation-details summary");
    await wait('document.querySelector("#dialog-day .official-post").dataset.embedState === "unavailable"');
    assert.equal(await evaluate('document.querySelector("#dialog-day .observation-source").href'), unavailablePost.url);
    assert.equal(await evaluate('document.querySelectorAll("#dialog-day iframe").length'), 0);
    await click("#close-day-dialog");
    observationResponse.posts = [...originalOfficial.posts.filter(post => post.date !== "2026-09-06"), officialLate];
    const requestsBeforeBlocked = widgetRequests;
    widgetBlocked = true;
    await call("Page.navigate", { url: `${origin}/` });
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    await wait('document.querySelector("#personal-status").dataset.loaded === "true"');
    await click('[data-date="2026-09-06"]');
    assert.equal(widgetRequests, requestsBeforeBlocked, "a fresh document still loads widgets lazily");
    await click("#dialog-day .observation-details summary");
    await wait('document.querySelector("#dialog-day .official-post").dataset.embedState === "unavailable"');
    assert.equal(widgetRequests, requestsBeforeBlocked + 1);
    assert.equal(await evaluate('document.querySelector("#dialog-day .observation-source").href'), officialLate.url);
    assert.equal(await evaluate('document.querySelectorAll("#dialog-day iframe").length'), 0);
    assert.equal(await evaluate('document.querySelector("#dialog-day .maid-entry[data-name=\\"みりあ\\"]").dataset.evidence'), "official-announced");
    await click("#dialog-day .observation-details summary");
    await click("#dialog-day .observation-details summary");
    await capture("popup-official-widget-blocked");
    assert.equal(widgetRequests, requestsBeforeBlocked + 1, "blocked script does not start duplicate requests");
    await click("#close-day-dialog");

    await call("Page.navigate", { url: `${origin}/unauthorized.html?scoutTheme=dark` });
    await wait('document.title.startsWith("閲覧権限")');
    await wait('document.querySelector("link[rel=stylesheet]").sheet !== null');
    const deniedDark = await palette(["html", "body"]);
    await preference("light");
    assert.deepEqual(await palette(["html", "body"]), deniedDark);
    assert.equal(deniedDark[1][1], "rgb(255, 251, 235)");
    assert.ok(await evaluate('getComputedStyle(document.body).forcedColorAdjust !== "none"'),
      "do not disable accessibility forced colors");
    assert.equal(exceptions.length, 0, JSON.stringify(exceptions));
    console.log("Browser calendar passed: 1280/390/320, events, modal, focus, keys, scroll, evidence, range, filters, fallback, light-only under dark preference and Auto Dark.");
    if (output) console.log(`Screenshots: ${path.resolve(output)}`);
  } finally {
    if (ws) ws.close();
    browser.kill();
    if (server.listening) await new Promise((resolve) => server.close(resolve));
    await delay(1000);
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
