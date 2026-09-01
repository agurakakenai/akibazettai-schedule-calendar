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

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      dateKey,
      getDateGridColumn,
      getTokyoDateDefaults,
      getVisibleMonthDates,
      isDateKeyInRange
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

  function createShiftSection(key, shift) {
    const section = document.createElement("section");
    section.className = `shift-section ${shiftDetails[shift].className}`;
    section.setAttribute("aria-label", `${shift}のお給仕`);

    const title = document.createElement("h4");
    title.className = "shift-title";
    const icon = document.createElement("span");
    icon.className = "shift-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = shiftDetails[shift].icon;
    const label = document.createElement("span");
    label.textContent = shift;
    title.append(icon, label);
    section.append(title);

    const allEntries = data.schedule[key]?.[shift] ?? [];
    const entries = filteredEntries(key, shift);

    if (entries.length > 0) {
      const list = document.createElement("ul");
      list.className = "maid-list";

      entries.forEach((entry) => {
        const item = document.createElement("li");
        item.className = "maid-entry";
        item.textContent = entry.name;
        const isKitchen = kitchenStaff.has(entry.name);

        if (isKitchen) {
          item.classList.add("is-kitchen");
        }

        if (entry.featured) {
          item.classList.add("is-featured");
          item.title = isKitchen
            ? `${entry.name}：${entry.eventLabel} / キッチンにゃんこ`
            : `${entry.name}：${entry.eventLabel}`;
          item.setAttribute(
            "aria-label",
            isKitchen
              ? `${entry.name}（${entry.eventLabel}の主役・キッチンにゃんこ）`
              : `${entry.name}（${entry.eventLabel}の主役）`
          );
        } else if (isKitchen) {
          item.title = `${entry.name}：キッチンにゃんこ`;
          item.setAttribute("aria-label", `${entry.name}（キッチンにゃんこ）`);
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
    shifts.forEach((shift) => day.append(createShiftSection(key, shift)));
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
  renderMaidFilters();
  renderCalendar();
})();
