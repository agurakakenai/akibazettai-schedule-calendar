(() => {
  "use strict";

  const data = window.SCHEDULE_DATA;
  const shifts = ["昼", "夜"];
  const weekdays = ["日", "月", "火", "水", "木", "金", "土"];
  const shiftDetails = {
    "昼": { icon: "☀", className: "shift-day" },
    "夜": { icon: "☾", className: "shift-night" }
  };
  const [initialYear, initialMonth] = data.initialMonth.split("-").map(Number);
  const state = {
    visibleMonth: new Date(initialYear, initialMonth - 1, 1),
    selectedMaids: new Set(data.roster),
    dateFrom: data.defaultDateFrom,
    dateTo: data.defaultDateTo
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

  function dateKey(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function isInDateRange(key) {
    const isAfterStart = !state.dateFrom || key >= state.dateFrom;
    const isBeforeEnd = !state.dateTo || key <= state.dateTo;
    return isAfterStart && isBeforeEnd;
  }

  function filteredEntries(key, shift) {
    const entries = data.schedule[key]?.[shift] ?? [];
    return entries.filter((entry) => state.selectedMaids.has(entry.name));
  }

  function createShiftSection(key, shift, isInRange) {
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
    const entries = isInRange ? filteredEntries(key, shift) : [];

    if (entries.length > 0) {
      const list = document.createElement("ul");
      list.className = "maid-list";

      entries.forEach((entry) => {
        const item = document.createElement("li");
        item.className = "maid-entry";
        item.textContent = entry.name;

        if (entry.featured) {
          item.classList.add("is-featured");
          item.title = `${entry.name}：${entry.eventLabel}`;
          item.setAttribute("aria-label", `${entry.name}（${entry.eventLabel}の主役）`);
        }

        list.append(item);
      });

      section.append(list);
      return section;
    }

    const empty = document.createElement("p");
    section.classList.add("is-empty");
    empty.className = "empty-shift";
    if (!isInRange) {
      empty.textContent = "期間外";
    } else if (allEntries.length > 0) {
      empty.textContent = "該当なし";
    } else {
      empty.textContent = "確認情報なし";
    }
    section.append(empty);
    return section;
  }

  function createDayCell(date, visibleMonthIndex) {
    const key = dateKey(date);
    const isCurrentMonth = date.getMonth() === visibleMonthIndex;
    const inRange = isInDateRange(key);
    const day = document.createElement("article");
    day.className = "calendar-day";
    day.setAttribute("role", "gridcell");
    day.setAttribute(
      "aria-label",
      `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
    );

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

    if (!isCurrentMonth) {
      day.classList.add("is-outside-month");
      const outsideLabel = document.createElement("span");
      outsideLabel.className = "outside-month-label";
      outsideLabel.textContent = `${date.getMonth() + 1}月`;
      heading.append(outsideLabel);
      day.append(heading);
      return day;
    }

    if (!inRange) {
      day.classList.add("is-outside-range");
    }

    day.append(heading);
    shifts.forEach((shift) => day.append(createShiftSection(key, shift, inRange)));
    return day;
  }

  function renderCalendar() {
    const year = state.visibleMonth.getFullYear();
    const monthIndex = state.visibleMonth.getMonth();
    const firstDay = new Date(year, monthIndex, 1);
    const daysInMonth = new Date(year, monthIndex + 1, 0).getDate();
    const cellCount = Math.ceil((firstDay.getDay() + daysInMonth) / 7) * 7;
    const firstCellDate = new Date(year, monthIndex, 1 - firstDay.getDay());
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

    for (let weekStart = 0; weekStart < cellCount; weekStart += 7) {
      const row = document.createElement("div");
      row.className = "calendar-row";
      row.setAttribute("role", "row");

      for (let offset = 0; offset < 7; offset += 1) {
        const date = new Date(firstCellDate);
        date.setDate(firstCellDate.getDate() + weekStart + offset);
        row.append(createDayCell(date, monthIndex));
      }

      grid.append(row);
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
      fragment.append(label);
    });
    elements.maidCheckboxes.replaceChildren(fragment);
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
    state.visibleMonth = new Date(initialYear, initialMonth - 1, 1);
    state.selectedMaids = new Set(data.roster);
    state.dateFrom = data.defaultDateFrom;
    state.dateTo = data.defaultDateTo;
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
