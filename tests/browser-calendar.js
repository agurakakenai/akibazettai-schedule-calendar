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
  const profile = fs.mkdtempSync(path.join(path.relative(process.cwd(), root), ".calendar-headless-"));
  const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
  const server = http.createServer((req, res) => {
    const pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
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
    ws.onmessage = ({ data }) => {
      const message = JSON.parse(data);
      if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
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
    await call("Network.setBlockedURLs", { urls: publicOrigin
      ? ["https://x.com/*", "https://twitter.com/*", "https://cdn.syndication.twimg.com/*", "https://search.yahoo.co.jp/*"]
      : ["https://*", "http://*.com/*", "http://*.jp/*"] });
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
        assert.ok(await evaluate('[...document.querySelectorAll("#day-dialog .unmatched-roster")].every(block => block.querySelectorAll(".maid-name").length > 0)'));
        if (curated || date === "2026-09-04") assert.equal(await evaluate('document.querySelectorAll("#day-dialog .unmatched-heading").length'), 0,
          "do not render an empty unconfirmed-plan block when all scheduled names were observed");
        assert.doesNotMatch(await evaluate('document.querySelector("#day-dialog").textContent'),
          /部分観測|公式確認|投稿実績|[1-4]号店[：:\s]*未確認|0人/);
        const names = await evaluate(`(async () => {
          const date = ${JSON.stringify(date)};
          const snapshot = await (await fetch("data/observed-shifts.json")).json();
          const insights = window.STORE_INSIGHTS;
          const aliases = new Map(Object.entries(insights.maidTendency)
            .filter(([, entry]) => entry?.alias).map(([name, entry]) => [entry.alias, name]));
          return ["昼", "夜"].map((shift, index) => {
            const section = document.querySelector(index === 0 ? "#dialog-day" : "#dialog-night");
            const record = insights.actualRoster[date]?.[shift];
            const expected = record
              ? Object.entries(record.stores).flatMap(([store, people]) => people.map(name => store+"|"+name))
              : [...new Set(snapshot.posts.filter(post => post.date === date && post.shift === shift)
                .flatMap(post => post.names.map(name => post.storeId+"|"+(aliases.get(name) ?? name))))];
            const shown = [...section.querySelectorAll(".recorded-roster .maid-entry")]
              .map(row => row.dataset.store+"|"+row.querySelector(".maid-name").textContent);
            const sourceLinks = [...section.querySelectorAll(".observation-details a")].map(a => a.href);
            const expectedLinks = record ? [] : snapshot.posts
              .filter(post => post.date === date && post.shift === shift).map(post => post.url);
            const details = section.querySelector(".observation-details");
            const marked = [...section.querySelectorAll(".recorded-roster .maid-entry.is-trainee")]
              .map(row => row.querySelector(".maid-name").textContent);
            const expectedMarked = shown.map(value => value.split("|")[1]).filter(name => record
              ? record.trainees?.includes(name)
              : insights.traineePeriods?.byName?.[name]?.from <= date && date <= insights.traineePeriods.byName[name].to);
            return { shown: shown.sort(), expected: expected.sort(), sources: sourceLinks.sort(),
              expectedSources: expectedLinks.sort(), details: details.textContent,
              marked: marked.sort(), expectedMarked: expectedMarked.sort() };
          });
        })()`);
        for (const [index, result] of names.entries()) {
          assert.deepEqual(result.shown, result.expected, `${date} ${index}: person/store facts must match their own source`);
          assert.deepEqual(result.sources, result.expectedSources, `${date} ${index}: no invented or cross-shift source URL`);
          assert.deepEqual(result.marked, result.expectedMarked, `${date} ${index}: trainees use only documented metadata`);
          if (curated) assert.ok(result.details.includes(`${date} ${index === 0 ? "昼" : "夜"}`) &&
            result.details.includes("data/store-insights.js・actualRoster"));
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
          await click("#jump-day");
          await evaluate('document.querySelector("#close-day-dialog").focus({preventScroll:true})');
          assert.ok(await noOverflow(), `${date} page fits ${width}px`);
          assert.ok(await evaluate('document.querySelector("#day-dialog-content").scrollWidth <= document.querySelector("#day-dialog-content").clientWidth'),
            `${date} popup content fits ${width}px`);
          assert.ok(await evaluate('[...document.querySelectorAll("#day-dialog .maid-entry")].every(row => row.getBoundingClientRect().right <= document.querySelector("#day-dialog-content").getBoundingClientRect().right)'));
          await capture(`popup-unified-${date}-${width === 1280 ? "desktop" : width === 390 ? "mobile" : "mobile-320"}`);
          await evaluate('document.querySelectorAll("#day-dialog .observation-details").forEach(details => { details.open = true; })');
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
    if (publicOrigin) {
      const stored = await evaluate('(async () => (await (await fetch("data/observed-shifts.json", {cache:"no-store"})).json()).posts.length)()');
      assert.ok(stored >= 10, "production must retain the ten verified observations");
      assert.equal(exceptions.length, 0, JSON.stringify(exceptions));
      console.log(`Public Pages smoke passed: ${publicOrigin}; ${stored} observations, unified store colors, 1280/390/320, light-only and modal controls.`);
      return;
    }

    // Curated source details use the same stable disclosure/focus keys as observations.
    observationResponse = JSON.parse(fs.readFileSync(path.join(root, "data", "observed-shifts.json"), "utf8"));
    holdObservation = true;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "false"');
    await click('[data-date="2026-09-03"]');
    await click("#dialog-day .observation-details summary");
    await evaluate('document.querySelector("#dialog-day .observation-details summary").focus({preventScroll:true})');
    const curatedDetailsBefore = await evaluate('({open:document.querySelector("#dialog-day .observation-details").open,focus:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop})');
    observationResponse.checkedAt = "2026-09-05T15:00:01Z";
    holdObservation = false;
    assert.ok(heldObservationResponses.length > 0);
    for (const res of heldObservationResponses.splice(0)) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(observationResponse));
    }
    await wait('document.querySelector("#observation-status").dataset.loaded === "true"');
    assert.deepEqual(await evaluate('({open:document.querySelector("#dialog-day .observation-details").open,focus:document.activeElement.dataset.focusKey,scroll:document.querySelector("#day-dialog-content").scrollTop})'),
      curatedDetailsBefore, "curated disclosures and summary focus survive async observation refresh");
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');

    // Actual store evidence alone must never become recorded person evidence.
    await evaluate(`delete window.STORE_INSIGHTS.actualRoster["2026-09-03"]["昼"];
      window.STORE_INSIGHTS.actualWithoutRoster["2026-09-03"] = {"昼": [...window.STORE_INSIGHTS.actual["2026-09-03"]["昼"]]};`);
    await click('[data-date="2026-09-03"]');
    assert.ok(await evaluate('document.querySelector("#day-dialog .shift-day .shift-evidence").textContent.includes("店舗のみ実績")'));
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .shift-day [data-evidence=recorded]").length'), 0);
    assert.equal(await evaluate('[...document.querySelectorAll("#day-dialog .shift-day .maid-entry")].some(row => /にいた記録/.test(row.title))'), false);
    assert.ok(await evaluate('document.querySelectorAll("#day-dialog .shift-night [data-evidence=recorded]").length > 0'));
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');

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
      assert.ok(await evaluate('document.querySelector("#jump-night").getBoundingClientRect().bottom < innerHeight'));
      await click("#jump-night");
      assert.equal(await evaluate('document.activeElement.id'), "dialog-night");
      assert.ok(await evaluate('document.querySelector("#day-dialog-content").scrollTop > 0'));
      assert.ok(await evaluate('document.querySelector("#close-day-dialog").getBoundingClientRect().top >= 0'));
      assert.ok(await evaluate('document.querySelector("#day-dialog .shift-night").getBoundingClientRect().bottom <= innerHeight'));
      await capture(`popup-night-mobile-${width}`);
      await click("#jump-day");
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
    assert.ok(await evaluate('document.querySelector("#day-dialog .shift-day").textContent.includes("予定（未確認）")'));
    assert.ok(await evaluate('document.querySelectorAll("#day-dialog .shift-day [data-evidence=scheduled]").length > 0'));
    assert.equal(await evaluate('document.querySelectorAll("#day-dialog .shift-day [data-evidence=recorded]").length'), 0);
    assert.ok(await noOverflow());
    await capture("popup-partial-observation-fixture");
    await click("#close-day-dialog");
    await wait('!document.querySelector("#day-dialog").open');
    holdObservation = true;
    await click("#refresh-observations");
    await wait('document.querySelector("#observation-status").dataset.loaded === "false"');
    await click('[data-date="2026-09-05"]');
    await click("#jump-night");
    await evaluate('document.querySelector("#dialog-night .maid-name[href]").focus({preventScroll:true})');
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
