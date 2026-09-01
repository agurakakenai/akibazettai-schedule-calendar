(() => {
  "use strict";

  function getTokyoDateDefaults(now = new Date()) {
    const parts = new Intl.DateTimeFormat("en-US-u-ca-gregory-nu-latn", {
      timeZone: "Asia/Tokyo",
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).formatToParts(now);
    const dateParts = Object.fromEntries(
      parts
        .filter(({ type }) => ["year", "month", "day"].includes(type))
        .map(({ type, value }) => [type, Number(value)])
    );
    const { year, month, day } = dateParts;
    const isLeapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const daysInMonth = [31, isLeapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    const pad = (value) => String(value).padStart(2, "0");
    const datePrefix = `${year}-${pad(month)}`;

    return {
      year,
      month,
      dateFrom: `${datePrefix}-${pad(day)}`,
      dateTo: `${datePrefix}-${pad(day <= 15 ? 15 : daysInMonth[month - 1])}`
    };
  }

  function dateKey(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function isDateKeyInRange(key, dateFrom, dateTo) {
    const isAfterStart = !dateFrom || key >= dateFrom;
    const isBeforeEnd = !dateTo || key <= dateTo;
    return isAfterStart && isBeforeEnd;
  }

  function getVisibleMonthDates(year, monthIndex, dateFrom, dateTo) {
    const daysInMonth = new Date(year, monthIndex + 1, 0).getDate();
    return Array.from({ length: daysInMonth }, (_, index) => new Date(year, monthIndex, index + 1))
      .filter((date) => isDateKeyInRange(dateKey(date), dateFrom, dateTo));
  }

  function getDateGridColumn(date) {
    return date.getDay() + 1;
  }

  const SHIFT_NAMES = ["昼", "夜"];
  const WEEKDAY_LABELS = ["日", "月", "火", "水", "木", "金", "土"];
  // シフト別の分母が小さいと 0% / 100% に振れるため、合計サンプルが足りるときだけ使う。
  const SHIFT_SAMPLE_MIN = 20;

  function addDays(key, amount) {
    const date = new Date(`${key}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + amount);
    return date.toISOString().slice(0, 10);
  }

  // 日付キーだけで曜日を決めるので、閲覧端末のタイムゾーンに左右されない。
  function weekdayIndex(key) {
    return new Date(`${key}T00:00:00Z`).getUTCDay();
  }

  // weekdayOrigin が "sunday" なら Date#getDay() と同じ並び、それ以外は Python の weekday()。
  function weekdayBucket(insights, key) {
    const index = weekdayIndex(key);
    return String(insights?.weekdayOrigin === "sunday" ? index : (index + 6) % 7);
  }

  function lastActualDateOf(insights) {
    const keys = Object.keys(insights?.actual ?? {});
    return keys.length > 0 ? keys.sort().at(-1) : null;
  }

  // 記録が無いシフトは「休み」ではなく「情報なし」なので null を返す。
  function openStoresOn(insights, key, shift) {
    const ids = insights?.actual?.[key]?.[shift];
    return Array.isArray(ids) ? new Set(ids) : null;
  }

  function groupStateOf(open) {
    const hasS2 = open.has("s2");
    const hasS3 = open.has("s3");
    return hasS2 && hasS3 ? "both" : hasS2 ? "s2" : hasS3 ? "s3" : "none";
  }

  function toPercent(rate) {
    return `${Math.round(rate * 100)}%`;
  }

  function compactStoreLabel(store) {
    return store.short.replace(/号店$/, "号");
  }

  function stateForRate(rate) {
    return rate >= 0.5 ? "likely" : "unlikely";
  }

  function storesOf(insights) {
    return Array.isArray(insights?.stores) ? insights.stores : [];
  }

  function storeShort(insights, id) {
    return storesOf(insights).find((store) => store.id === id)?.short ?? id;
  }

  function joinStoreNames(insights, ids) {
    return storesOf(insights)
      .filter((store) => ids.has(store.id))
      .map((store) => store.short)
      .join("・");
  }

  function actualOutlook(insights, key, shift) {
    const open = openStoresOn(insights, key, shift);
    if (!open) {
      return null;
    }
    return {
      basis: "actual",
      badge: "実績",
      badgeClass: "is-actual",
      openStores: [...open],
      summary: open.size > 0
        ? `${shift}は${joinStoreNames(insights, open)}が営業していました（公式Xの投稿で確認できた実績）。`
        : `${shift}に営業した店舗の記録がありません。`,
      entries: storesOf(insights).map((store) => {
        const isOpen = open.has(store.id);
        return {
          store,
          state: isOpen ? "open" : "closed",
          rate: null,
          text: isOpen ? "営業" : "休み",
          srText: `${store.short}は${isOpen ? "営業" : "休み"}`
        };
      })
    };
  }

  function forecastOutlook(insights, key, shift, lastActualDate) {
    if (!lastActualDate || key !== addDays(lastActualDate, 1)) {
      return null;
    }
    const previous = openStoresOn(insights, lastActualDate, shift);
    const baseRate = insights.baseOpenRate?.[shift];
    if (!previous || !baseRate) {
      return null;
    }
    // 欠損した遷移先（例: rotation.sameDay.s2.s3）は 0 件なので 0 として扱う。
    const groupNext = insights.rotation?.nextDay?.[shift]?.[groupStateOf(previous)] ?? {};
    const s4Next =
      insights.rotation?.nextDayS4?.[shift]?.[previous.has("s4") ? "open" : "closed"] ?? {};
    const rates = {
      s1: baseRate.s1 ?? 0,
      s2: (groupNext.s2 ?? 0) + (groupNext.both ?? 0),
      s3: (groupNext.s3 ?? 0) + (groupNext.both ?? 0),
      s4: s4Next.open ?? 0
    };
    const entries = storesOf(insights).map((store) => {
      const rate = rates[store.id] ?? baseRate[store.id] ?? 0;
      return {
        store,
        state: stateForRate(rate),
        rate,
        text: toPercent(rate),
        srText: `${store.short}が営業する見込みは${toPercent(rate)}`
      };
    });

    const shiftAccuracy = insights.accuracy?.nextDayByShift?.[shift] ?? {};
    const dayAccuracy = insights.accuracy?.nextDayByShift?.["日"] ?? {};
    const rivals = entries.filter((entry) => ["s2", "s3"].includes(entry.store.id));
    const leader = rivals.length > 0
      ? rivals.reduce((best, entry) => (entry.rate > best.rate ? entry : best))
      : null;
    const parts = [`前日（${lastActualDate}）の${shift}の実績から見た、翌日の${shift}の見込みです。`];

    if (leader) {
      parts.push(`2・3号店では${leader.store.short}が${toPercent(leader.rate)}で最有力です。`);
    }
    if (typeof shiftAccuracy.group === "number") {
      parts.push(
        `ただし${shift}まで分けた2・3号店の的中は実測${toPercent(shiftAccuracy.group)}で、当てずっぽうと同じくらいです。`
      );
    }
    if (typeof dayAccuracy.group === "number") {
      parts.push(
        `昼夜をまとめて「どちらが開くか」なら実測${toPercent(dayAccuracy.group)}` +
          (typeof dayAccuracy.groupBaseline === "number"
            ? `（当てずっぽうは${toPercent(dayAccuracy.groupBaseline)}）`
            : "") +
          "です。"
      );
    }
    if (typeof shiftAccuracy.s4 === "number") {
      parts.push(`4号店の${shift}は実測${toPercent(shiftAccuracy.s4)}当たります。`);
    }

    return {
      basis: "forecast",
      badge: "見込み（翌日）",
      badgeClass: "is-forecast",
      previousStores: [...previous],
      summary: parts.join(""),
      entries
    };
  }

  function tendencyOutlook(insights, key, shift, lastActualDate) {
    const bucket = weekdayBucket(insights, key);
    const rates = insights.weekdayOpenRate?.[shift]?.[bucket];
    if (!rates) {
      return null;
    }
    const hasPartialRecord = Boolean(insights.actual?.[key]);
    const isPast = Boolean(lastActualDate) && key <= lastActualDate;
    const weekdayName = WEEKDAY_LABELS[weekdayIndex(key)];
    const lead = hasPartialRecord
      ? `この日の${shift}の記録だけが手元にありません（休みとは限りません）。`
      : isPast
        ? "この日の実績は手元にありません。"
        : "";
    return {
      basis: "tendency",
      badge: "曜日傾向",
      badgeClass: "is-tendency",
      weekdayBucket: bucket,
      summary:
        `${lead}${weekdayName}曜日の${shift}の、過去1年の営業率です。` +
        "予測ではありません。2日以上先は当てになりません。",
      entries: storesOf(insights).map((store) => {
        const rate = rates[store.id] ?? 0;
        return {
          store,
          state: stateForRate(rate),
          rate,
          text: toPercent(rate),
          srText: `${store.short}の${weekdayName}曜日${shift}の営業率は${toPercent(rate)}`
        };
      })
    };
  }

  // 実績 → 翌日見込み → 曜日傾向 の順に、確かなものから採用する。
  function getStoreOutlook({ insights, dateKey: key, shift, lastActualDate }) {
    if (!insights || storesOf(insights).length === 0) {
      return null;
    }
    const last = lastActualDate === undefined ? lastActualDateOf(insights) : lastActualDate;
    return (
      actualOutlook(insights, key, shift) ??
      forecastOutlook(insights, key, shift, last) ??
      tendencyOutlook(insights, key, shift, last)
    );
  }

  function tendencyTables(tendency, shift) {
    const samples = tendency.sampleByShift?.[shift] ?? null;
    const total = samples
      ? Object.values(samples).reduce((sum, value) => sum + value, 0)
      : 0;
    const pickByShift = tendency.pickRateByShift?.[shift];
    const shareByShift = tendency.shareByShift?.[shift];

    if (total >= SHIFT_SAMPLE_MIN && pickByShift && shareByShift) {
      return { pickRate: pickByShift, share: shareByShift, scope: shift, samples: total };
    }
    return {
      pickRate: tendency.pickRate ?? {},
      share: tendency.share ?? {},
      scope: "overall",
      samples: total
    };
  }

  // その日そのシフトで、そのメイドさんがいそうな店舗を1つ返す。
  function getMaidStoreOutlook({ insights, name, shift, outlook }) {
    const tendency = insights?.maidTendency?.[name];
    if (!tendency) {
      return null;
    }
    const stores = storesOf(insights);
    const { pickRate, share, scope } = tendencyTables(tendency, shift);
    const scopeNote = scope === shift
      ? `${shift}の実績`
      : "昼夜あわせた実績（このシフトは件数が少ないため）";
    const openIds = outlook?.basis === "actual" ? outlook.openStores ?? [] : [];

    if (openIds.length > 0) {
      const best = openIds.reduce(
        (top, id) => ((pickRate[id] ?? 0) > (pickRate[top] ?? 0) ? id : top),
        openIds[0]
      );
      const store = stores.find((candidate) => candidate.id === best);
      if (!store || !(pickRate[best] > 0)) {
        return null;
      }
      const percent = toPercent(pickRate[best]);
      return {
        basis: "pickRate",
        storeId: store.id,
        rate: pickRate[best],
        label: compactStoreLabel(store),
        percent,
        title: `${store.short}が開いている${shift}は${percent}の割合でこの店に入っています（${scopeNote}）`,
        srText: `この${shift}に開いていた店舗のうち、よく入るのは${store.short}、${percent}`
      };
    }

    const ranking = stores
      .map((store) => [store.id, share[store.id] ?? 0])
      .filter(([, value]) => value > 0)
      .sort((a, b) => b[1] - a[1]);
    const ranked = ranking.length > 0
      ? ranking.map(([id]) => id)
      : (Array.isArray(tendency.likely) ? tendency.likely : []);
    const topId = ranked[0];
    const store = stores.find((candidate) => candidate.id === topId);
    if (!store || !(share[topId] > 0)) {
      return null;
    }
    const percent = toPercent(share[topId]);
    const pair = ranked
      .slice(0, 2)
      .map((id) => `${storeShort(insights, id)} ${toPercent(share[id] ?? 0)}`)
      .join(" / ");
    const coverage = insights.accuracy?.maidStoreTop2;
    return {
      basis: "share",
      storeId: store.id,
      rate: share[topId],
      label: compactStoreLabel(store),
      percent,
      title:
        `${shift}に出勤するときにいそうな店舗：${pair}（${scopeNote}）` +
        (typeof coverage === "number" ? `。この2店舗で実測${toPercent(coverage)}をカバーします` : ""),
      srText: `${shift}にいそうな店舗は${store.short}、${percent}`
    };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      addDays,
      dateKey,
      getDateGridColumn,
      getMaidStoreOutlook,
      getStoreOutlook,
      getTokyoDateDefaults,
      getVisibleMonthDates,
      isDateKeyInRange,
      lastActualDateOf,
      openStoresOn,
      weekdayBucket,
      weekdayIndex
    };
  }

  if (typeof window === "undefined" || typeof document === "undefined") {
    return;
  }

  const data = window.SCHEDULE_DATA;
  const shifts = ["昼", "夜"];
  const kitchenStaff = new Set(data.kitchenStaff ?? []);
  const weekdays = ["日", "月", "火", "水", "木", "金", "土"];
  const shiftDetails = {
    "昼": { icon: "☀", className: "shift-day" },
    "夜": { icon: "☾", className: "shift-night" }
  };

  const insights = window.STORE_INSIGHTS ?? null;
  const storeList = storesOf(insights);
  const lastActualKey = lastActualDateOf(insights);
  const hasInsights = Boolean(insights) && storeList.length > 0;

  function getShiftOutlook(key, shift) {
    return getStoreOutlook({
      insights,
      dateKey: key,
      shift,
      lastActualDate: lastActualKey
    });
  }

  function createStoreOutlook(outlook, shift) {
    const wrapper = document.createElement("div");
    wrapper.className = "store-outlook";
    wrapper.title = outlook.summary;

    const description = document.createElement("p");
    description.className = "visually-hidden";
    description.textContent =
      `${shift}の店舗（${outlook.badge}）：` +
      `${outlook.entries.map((entry) => entry.srText).join("、")}。${outlook.summary}`;

    const list = document.createElement("ul");
    list.className = "store-chips";
    list.setAttribute("aria-hidden", "true");

    outlook.entries.forEach((entry) => {
      const chip = document.createElement("li");
      chip.className = `store-chip is-${entry.state}`;
      chip.dataset.store = entry.store.id;

      const name = document.createElement("span");
      name.className = "store-chip-name";
      name.textContent = compactStoreLabel(entry.store);
      const value = document.createElement("span");
      value.className = "store-chip-rate";
      value.textContent = entry.text;
      chip.append(name, value);
      list.append(chip);
    });

    wrapper.append(description, list);
    return wrapper;
  }

  function createStatusBadge(outlook) {
    const badge = document.createElement("span");
    badge.className = `store-status-badge ${outlook.badgeClass}`;
    badge.textContent = outlook.badge;
    badge.setAttribute("aria-hidden", "true");
    return badge;
  }

  function createMaidStoreChip(chipData) {
    const chip = document.createElement("span");
    chip.className = "maid-store-chip";
    chip.dataset.store = chipData.storeId;
    chip.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.textContent = chipData.label;
    // 月グリッドはセルが狭く、割合まで出すと1件が2行になるので CSS で出し分ける。
    const percent = document.createElement("span");
    percent.className = "maid-store-chip-rate";
    percent.textContent = chipData.percent;

    chip.append(label, percent);
    return chip;
  }

  const defaults = getTokyoDateDefaults();
  const state = {
    visibleMonth: new Date(defaults.year, defaults.month - 1, 1),
    selectedMaids: new Set(data.roster),
    dateFrom: defaults.dateFrom,
    dateTo: defaults.dateTo
  };

  const elements = {
    calendar: document.querySelector("#calendar"),
    monthTitle: document.querySelector("#month-title"),
    resultSummary: document.querySelector("#result-summary"),
    lastUpdated: document.querySelector("#last-updated"),
    insightNotes: document.querySelector("#insight-notes"),
    maidCheckboxes: document.querySelector("#maid-checkboxes"),
    maidFilterDetails: document.querySelector("#maid-filter-details"),
    maidFilterSummary: document.querySelector("#maid-filter-summary"),
    dateFrom: document.querySelector("#date-from"),
    dateTo: document.querySelector("#date-to"),
    previousMonth: document.querySelector("#previous-month"),
    nextMonth: document.querySelector("#next-month"),
    selectAll: document.querySelector("#select-all"),
    clearAll: document.querySelector("#clear-all"),
    resetFilters: document.querySelector("#reset-filters")
  };

  function isInDateRange(key) {
    return isDateKeyInRange(key, state.dateFrom, state.dateTo);
  }

  function filteredEntries(key, shift) {
    const entries = data.schedule[key]?.[shift] ?? [];
    return entries.filter((entry) => state.selectedMaids.has(entry.name));
  }

  function createShiftSection(key, date, shift) {
    const section = document.createElement("section");
    section.className = `shift-section ${shiftDetails[shift].className}`;
    section.setAttribute("aria-label", `${shift}のお給仕`);

    const outlook = getShiftOutlook(key, shift);
    const title = document.createElement("h4");
    title.className = "shift-title";
    const icon = document.createElement("span");
    icon.className = "shift-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = shiftDetails[shift].icon;
    const label = document.createElement("span");
    label.textContent = shift;
    title.append(icon, label);
    if (outlook) {
      title.append(createStatusBadge(outlook));
    }
    section.append(title);

    if (outlook) {
      section.append(createStoreOutlook(outlook, shift));
    }

    const allEntries = data.schedule[key]?.[shift] ?? [];
    const entries = filteredEntries(key, shift);

    if (entries.length > 0) {
      const list = document.createElement("ul");
      list.className = "maid-list";

      entries.forEach((entry) => {
        const item = document.createElement("li");
        item.className = "maid-entry";
        const account = insights?.maidTendency?.[entry.name]?.x;
        const nameLabel = document.createElement(account ? "a" : "span");
        nameLabel.className = "maid-name";
        nameLabel.textContent = entry.name;
        if (account) {
          nameLabel.href = `https://x.com/${account}`;
          nameLabel.target = "_blank";
          nameLabel.rel = "noopener noreferrer";
          nameLabel.title = `${entry.name}のXを開く`;
        }
        item.append(nameLabel);
        const isKitchen = kitchenStaff.has(entry.name);
        const chipData = getMaidStoreOutlook({ insights, name: entry.name, shift, outlook });
        const titles = [];
        const descriptions = [];

        if (isKitchen) {
          item.classList.add("is-kitchen");
          titles.push("キッチンにゃんこ");
          descriptions.push("キッチンにゃんこ");
        }

        if (entry.featured) {
          item.classList.add("is-featured");
          titles.unshift(entry.eventLabel);
          descriptions.unshift(`${entry.eventLabel}の主役`);
        }

        if (chipData) {
          item.append(createMaidStoreChip(chipData));
          titles.push(chipData.title);
          descriptions.push(chipData.srText);
        }

        if (titles.length > 0) {
          item.title = `${entry.name}：${titles.join(" / ")}`;
          item.setAttribute("aria-label", `${entry.name}（${descriptions.join("・")}）`);
        }

        list.append(item);
      });

      section.append(list);
      return section;
    }

    const empty = document.createElement("p");
    section.classList.add("is-empty");
    empty.className = "empty-shift";
    if (allEntries.length > 0) {
      const mark = document.createElement("span");
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = "-";
      const description = document.createElement("span");
      description.className = "visually-hidden";
      description.textContent = "該当なし";
      empty.append(mark, description);
    } else {
      empty.textContent = "確認情報なし";
    }
    section.append(empty);
    return section;
  }

  function createDayCell(date, isFirstRenderedDate) {
    const key = dateKey(date);
    const day = document.createElement("article");
    day.className = "calendar-day";
    day.setAttribute("role", "gridcell");
    day.setAttribute(
      "aria-label",
      `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
    );

    if (isFirstRenderedDate) {
      day.style.gridColumnStart = String(getDateGridColumn(date));
    }

    if (date.getDay() === 0) {
      day.classList.add("is-sunday");
    }

    const heading = document.createElement("div");
    heading.className = "day-heading";
    const dateLabel = document.createElement("div");
    dateLabel.className = "day-date";
    const number = document.createElement("span");
    number.className = "day-number";
    number.textContent = String(date.getDate());
    const weekday = document.createElement("span");
    weekday.className = "day-weekday";
    weekday.textContent = `${weekdays[date.getDay()]}曜日`;
    dateLabel.append(number, weekday);
    heading.append(dateLabel);

    day.append(heading);
    shifts.forEach((shift) => day.append(createShiftSection(key, date, shift)));
    return day;
  }

  function renderCalendar() {
    const year = state.visibleMonth.getFullYear();
    const monthIndex = state.visibleMonth.getMonth();
    const firstDay = new Date(year, monthIndex, 1);
    const visibleDates = getVisibleMonthDates(
      year,
      monthIndex,
      state.dateFrom,
      state.dateTo
    );
    const grid = document.createElement("div");
    grid.className = "calendar-grid";
    grid.setAttribute("role", "grid");
    grid.setAttribute("aria-labelledby", "month-title");

    const headerRow = document.createElement("div");
    headerRow.className = "calendar-row";
    headerRow.setAttribute("role", "row");
    weekdays.forEach((weekday) => {
      const header = document.createElement("div");
      header.className = "weekday";
      header.setAttribute("role", "columnheader");
      header.textContent = weekday;
      headerRow.append(header);
    });
    grid.append(headerRow);

    if (visibleDates.length === 0) {
      const row = document.createElement("div");
      row.className = "calendar-row";
      row.setAttribute("role", "row");
      const empty = document.createElement("p");
      empty.className = "calendar-empty";
      empty.setAttribute("role", "gridcell");
      empty.setAttribute("aria-live", "polite");
      empty.textContent = "この月には、選択した期間の日付がありません。";
      row.append(empty);
      grid.append(row);
    } else {
      let currentWeek = -1;
      let row;

      visibleDates.forEach((date, index) => {
        const week = Math.floor((firstDay.getDay() + date.getDate() - 1) / 7);
        if (week !== currentWeek) {
          row = document.createElement("div");
          row.className = "calendar-row";
          row.setAttribute("role", "row");
          grid.append(row);
          currentWeek = week;
        }
        row.append(createDayCell(date, index === 0));
      });
    }

    const displayedCount = Object.entries(data.schedule).reduce((total, [key, day]) => {
      if (!key.startsWith(`${year}-${String(monthIndex + 1).padStart(2, "0")}`)) {
        return total;
      }
      if (!isInDateRange(key)) {
        return total;
      }
      return total + shifts.reduce(
        (shiftTotal, shift) =>
          shiftTotal + day[shift].filter((entry) => state.selectedMaids.has(entry.name)).length,
        0
      );
    }, 0);

    elements.monthTitle.textContent = `${year}年${monthIndex + 1}月`;
    elements.resultSummary.textContent =
      `${state.selectedMaids.size}名を選択中・${displayedCount}件のお給仕を表示`;
    elements.calendar.replaceChildren(grid);
  }

  function accuracyValue(...keys) {
    let node = insights?.accuracy;
    for (const key of keys) {
      if (!node || typeof node !== "object") {
        return null;
      }
      node = node[key];
    }
    return typeof node === "number" ? node : null;
  }

  function shopName(id) {
    return storeShort(insights, id);
  }

  function buildAccuracyLines() {
    const lines = [];
    const dayGroup = accuracyValue("nextDayByShift", "日", "group");
    const dayBaseline = accuracyValue("nextDayByShift", "日", "groupBaseline");
    const dayGroupParts = [];

    if (dayGroup !== null) {
      dayGroupParts.push(
        `翌日について「2号店と3号店のどちらが開くか」は、昼夜をまとめると実測${toPercent(dayGroup)}です`
      );
      if (dayBaseline !== null) {
        dayGroupParts.push(`（当てずっぽうは${toPercent(dayBaseline)}）`);
      }
      lines.push(`${dayGroupParts.join("")}。`);
    }

    const shiftGroups = shifts
      .map((shift) => [shift, accuracyValue("nextDayByShift", shift, "group")])
      .filter(([, value]) => value !== null);
    if (shiftGroups.length > 0) {
      lines.push(
        `昼夜まで分けると2・3号店は${shiftGroups.map(([shift, value]) => `${shift}${toPercent(value)}`).join(" / ")}で、` +
          "当てずっぽうとほとんど変わりません。昼夜の内訳は参考程度に見てください。"
      );
    }

    const shiftS4 = shifts
      .map((shift) => [shift, accuracyValue("nextDayByShift", shift, "s4")])
      .filter(([, value]) => value !== null);
    if (shiftS4.length > 0) {
      lines.push(
        `4号店の翌日は${shiftS4.map(([shift, value]) => `${shift}${toPercent(value)}`).join(" / ")}で、こちらは多少あてになります。`
      );
    }

    lines.push("2日以上先は曜日ごとの平均を出しているだけで、その日の予想には使えません。");

    const givenOpen = accuracyValue("maidStoreGivenOpen");
    const top1 = accuracyValue("maidStoreTop1");
    const top2 = accuracyValue("maidStoreTop2");
    if (givenOpen !== null || top1 !== null) {
      const parts = [];
      if (givenOpen !== null) {
        parts.push(`開いている店舗が分かっていれば${toPercent(givenOpen)}`);
      }
      if (top1 !== null) {
        parts.push(`分からなければ${toPercent(top1)}`);
      }
      lines.push(`メイドさんの店舗を1店に絞って当たったのは、${parts.join("、")}でした。`);
    }
    if (top2 !== null) {
      lines.push(
        `候補を2店舗まで広げると${toPercent(top2)}が当たります。チップには最有力の1店だけを出しているので、外れたらもう1店を疑ってください。`
      );
    }

    const split = insights.shiftSplitGivenOpen;
    if (split) {
      const described = storeList
        .filter((store) => split[store.id])
        .map((store) => {
          const value = split[store.id];
          return (
            `${store.short}は通し${toPercent(value.allDay ?? 0)}・` +
            `昼のみ${toPercent(value.dayOnly ?? 0)}・夜のみ${toPercent(value.nightOnly ?? 0)}`
          );
        });
      if (described.length > 0) {
        lines.push(`営業する日の内訳は、${described.join("、")}です。`);
      }
    }

    const sameDay = insights.rotation?.sameDay;
    if (sameDay?.s2) {
      const swap = sameDay.s2.s3 ?? 0;
      lines.push(
        `昼が2号店だった日に夜が3号店へ入れ替わった割合は${toPercent(swap)}です（夜は両方休みが${toPercent(sameDay.s2.none ?? 0)}、` +
          `2号店の続きが${toPercent(sameDay.s2.s2 ?? 0)}）。入れ替わるのは同じ日の中ではなく翌日です。`
      );
    }

    lines.push("生誕祭・周年・卒業の日は主役のメイドさんが顔ぶれを選べるため、傾向の計算から除いています。");
    return lines;
  }

  function createUnlistedEntry(name, info) {
    const linkable = Boolean(info.hasPublicAccount && info.x && info.status !== "graduated");
    const item = document.createElement("li");
    if (!linkable) {
      item.classList.add("is-unverified");
    }
    if (info.status === "graduated") {
      item.classList.add("is-graduated");
    }

    const label = document.createElement(linkable ? "a" : "span");
    label.className = "unlisted-name";
    label.textContent = name;
    if (linkable) {
      label.href = `https://x.com/${info.x}`;
      label.target = "_blank";
      label.rel = "noopener noreferrer";
    }

    const recent = (info.recentShifts31 ?? 0) > 0
      ? `直近1か月${info.recentShifts31}回`
      : `直近90日で${info.recentShifts ?? 0}回`;
    const home = info.home ? `よく${shopName(info.home)}` : null;
    const facts = [recent, home];

    if (info.status === "graduated") {
      facts.push(info.graduatedAt ? `卒業 ${info.graduatedAt}` : "卒業・離脱");
    } else if (info.likelyNew) {
      facts.push("新人かも");
    }

    const detail = document.createElement("span");
    detail.className = "unlisted-detail";
    detail.textContent = facts.filter(Boolean).join("・");

    // 判定の根拠をそのまま出しておく。断定できるだけの材料はない。
    const clamped = info.streakStart && insights.shiftDataFrom
      && info.streakStart <= insights.shiftDataFrom;
    const evidence = [
      `${name}：${facts.filter(Boolean).join(" / ")}`,
      info.streakStart
        ? `このデータ（${insights.shiftDataFrom ?? "?"}以降）では ${info.streakStart} からお給仕を確認` +
          (clamped ? "。それ以前は分かりません" : "")
        : null,
      typeof info.daysSinceLast === "number" ? `最後のお給仕から${info.daysSinceLast}日` : null,
      info.otherAccounts?.length > 0
        ? `以前のアカウント ${info.otherAccounts.join("・")} が見つかったので、新人とは判断していません`
        : info.likelyNew
          ? "以前のアカウントも1年より前のお給仕も見つからないため、新しくノーマルにゃんこになった可能性があります"
          : null,
      info.xNote ?? null,
      info.status === "graduated"
        ? "卒業・離脱と判断したため、在籍中の一覧からは外しています"
        : linkable
          ? "Xを開きます"
          : info.xStatus === "公式サイト"
            ? "公式サイトには在籍しています"
            : "公開アカウントを確認できていません"
    ];
    item.title = evidence.filter(Boolean).join(" / ");

    item.append(label, detail);
    return item;
  }

  function createUnlistedMaids() {
    const unlisted = insights.unlistedMaids ?? {};
    const byRecency = (a, b) => {
      const left = unlisted[a] ?? {};
      const right = unlisted[b] ?? {};
      return (
        Number(Boolean(right.promoted)) - Number(Boolean(left.promoted)) ||
        (right.recentShifts31 ?? 0) - (left.recentShifts31 ?? 0) ||
        (right.recentShifts ?? 0) - (left.recentShifts ?? 0)
      );
    };
    const names = Object.keys(unlisted);
    const active = names.filter((name) => unlisted[name]?.status !== "graduated").sort(byRecency);
    const graduated = names.filter((name) => unlisted[name]?.status === "graduated").sort(byRecency);

    if (active.length === 0 && graduated.length === 0) {
      return null;
    }

    const section = document.createElement("div");

    if (active.length > 0) {
      const heading = document.createElement("p");
      heading.className = "insight-subhead";
      heading.textContent =
        `このカレンダーに載っていないメンバー（${active.length}名）：` +
        "公式サイトの在籍一覧には無いものの、最近のお給仕投稿に出ている方です。" +
        "公開アカウントを確認できた方だけXへのリンクを付けています。カレンダー本体には表示していません。";

      const list = document.createElement("ul");
      list.className = "unlisted-maids";
      active.forEach((name) => list.append(createUnlistedEntry(name, unlisted[name] ?? {})));
      section.append(heading, list);
    }

    if (graduated.length > 0) {
      const heading = document.createElement("p");
      heading.className = "insight-subhead";
      heading.textContent =
        `卒業・離脱と判断した方（${graduated.length}名）：` +
        "卒業イベント、2週間以上お給仕が無いこと、Xの卒業表記のいずれかで判断しています。" +
        "上の一覧とは分けています。";

      const list = document.createElement("ul");
      list.className = "unlisted-maids";
      graduated.forEach((name) => list.append(createUnlistedEntry(name, unlisted[name] ?? {})));
      section.append(heading, list);
    }

    return section;
  }

  function renderInsightNotes() {
    const container = elements.insightNotes;
    if (!container || !hasInsights) {
      return;
    }

    const legend = document.createElement("ul");
    legend.className = "insight-legend";
    [
      {
        badge: "実績",
        badgeClass: "is-actual",
        text: "公式Xの投稿で確認できた、その日そのシフトの営業店舗です。"
      },
      {
        badge: "見込み（翌日）",
        badgeClass: "is-forecast",
        text: "実績の最終日の翌日だけ、前日からのローテーションで見積もっています。"
      },
      {
        badge: "曜日傾向",
        badgeClass: "is-tendency",
        text: "曜日ごとの過去1年の営業率です。その日の予測ではありません。"
      }
    ].forEach(({ badge, badgeClass, text }) => {
      const item = document.createElement("li");
      const mark = document.createElement("span");
      mark.className = `store-status-badge ${badgeClass}`;
      mark.textContent = badge;
      const description = document.createElement("span");
      description.className = "legend-text";
      description.textContent = text;
      item.append(mark, description);
      legend.append(item);
    });

    const accuracy = document.createElement("ul");
    accuracy.className = "insight-accuracy";
    buildAccuracyLines().forEach((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      accuracy.append(item);
    });

    const source = document.createElement("p");
    source.className = "insight-source";
    const sample = insights.sampleWindow ?? {};
    const history = insights.historyRange ?? {};
    const range = sample.from && sample.to
      ? `${sample.from}〜${sample.to}`
      : `${history.from ?? "?"}〜${history.to ?? "?"}`;
    source.textContent =
      "出どころ：公式X @akibazettai の「ひるにゃんこ / よるにゃんこ」投稿を Wayback Machine 経由で復元した出勤実績。" +
      `集計期間 ${range}` +
      (typeof sample.days === "number" ? `（${sample.days}日）` : "") +
      `。データ生成 ${insights.generatedAt ?? "不明"}。`;

    const unlisted = createUnlistedMaids();
    container.append(legend, accuracy, ...(unlisted ? [unlisted] : []), source);
    container.hidden = false;
  }

  function updateMaidFilterSummary() {
    elements.maidFilterSummary.textContent =
      `${state.selectedMaids.size}/${data.roster.length}名を表示`;
  }

  function renderMaidFilters() {
    const fragment = document.createDocumentFragment();

    data.roster.forEach((name, index) => {
      const label = document.createElement("label");
      label.className = "checkbox-label";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.id = `maid-${index}`;
      checkbox.value = name;
      checkbox.checked = state.selectedMaids.has(name);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          state.selectedMaids.add(name);
        } else {
          state.selectedMaids.delete(name);
        }
        updateMaidFilterSummary();
        renderCalendar();
      });

      const text = document.createElement("span");
      text.textContent = name;
      label.append(checkbox, text);

      if (kitchenStaff.has(name)) {
        label.classList.add("is-kitchen");
        const badge = document.createElement("span");
        badge.className = "kitchen-badge";
        badge.textContent = "🍳";
        badge.title = `${name}はキッチンにゃんこです`;
        badge.setAttribute("role", "img");
        badge.setAttribute("aria-label", "キッチンにゃんこ");
        label.append(badge);
      }

      fragment.append(label);
    });
    elements.maidCheckboxes.replaceChildren(fragment);
    updateMaidFilterSummary();
  }

  function setAllMaids(selected) {
    state.selectedMaids = selected ? new Set(data.roster) : new Set();
    renderMaidFilters();
    renderCalendar();
  }

  function setVisibleMonth(offset) {
    state.visibleMonth = new Date(
      state.visibleMonth.getFullYear(),
      state.visibleMonth.getMonth() + offset,
      1
    );
    renderCalendar();
  }

  function syncDateRange(changedField) {
    state.dateFrom = elements.dateFrom.value;
    state.dateTo = elements.dateTo.value;

    if (state.dateFrom && state.dateTo && state.dateFrom > state.dateTo) {
      if (changedField === "from") {
        state.dateTo = state.dateFrom;
        elements.dateTo.value = state.dateTo;
      } else {
        state.dateFrom = state.dateTo;
        elements.dateFrom.value = state.dateFrom;
      }
    }

    renderCalendar();
  }

  function resetFilters() {
    const resetDefaults = getTokyoDateDefaults();
    state.visibleMonth = new Date(resetDefaults.year, resetDefaults.month - 1, 1);
    state.selectedMaids = new Set(data.roster);
    state.dateFrom = resetDefaults.dateFrom;
    state.dateTo = resetDefaults.dateTo;
    elements.dateFrom.value = state.dateFrom;
    elements.dateTo.value = state.dateTo;
    renderMaidFilters();
    renderCalendar();
  }

  elements.previousMonth.addEventListener("click", () => setVisibleMonth(-1));
  elements.nextMonth.addEventListener("click", () => setVisibleMonth(1));
  elements.selectAll.addEventListener("click", () => setAllMaids(true));
  elements.clearAll.addEventListener("click", () => setAllMaids(false));
  elements.resetFilters.addEventListener("click", resetFilters);
  elements.dateFrom.addEventListener("change", () => syncDateRange("from"));
  elements.dateTo.addEventListener("change", () => syncDateRange("to"));

  elements.dateFrom.value = state.dateFrom;
  elements.dateTo.value = state.dateTo;
  elements.lastUpdated.textContent = `最終更新：${data.lastUpdated}`;
  elements.maidFilterDetails.open =
    !window.matchMedia("(max-width: 45rem)").matches;
  renderInsightNotes();
  renderMaidFilters();
  renderCalendar();
})();
