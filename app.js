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
      dateFrom: `${datePrefix}-01`,
      dateTo: `${datePrefix}-${pad(daysInMonth[month - 1])}`
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

  function tokyoToday(now = new Date()) {
    return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Tokyo" }).format(now);
  }

  function monthCells(year, monthIndex) {
    const first = new Date(year, monthIndex, 1);
    const count = new Date(year, monthIndex + 1, 0).getDate();
    const length = Math.ceil((first.getDay() + count) / 7) * 7;
    return Array.from({ length }, (_, index) => {
      const day = index - first.getDay() + 1;
      return day >= 1 && day <= count ? new Date(year, monthIndex, day) : null;
    });
  }

  function displayAliases(insights) {
    if (insights?.memberRegistry) return insights.memberRegistry.aliases();
    // Standalone legacy consumers may omit this context; browser bootstrap never does.
    return new Map(
      Object.entries(insights?.maidTendency ?? {})
        .filter(([, entry]) => entry?.alias)
        .map(([name, entry]) => [entry.alias, name])
    );
  }

  function dayEvents(data, insights, key) {
    const aliases = displayAliases(insights);
    const events = new Map();
    for (const shift of SHIFT_NAMES) {
      for (const entry of data.schedule?.[key]?.[shift] ?? []) {
        if (!entry.featured) continue;
        const name = aliases.get(entry.name) ?? entry.name;
        if (!events.has(name)) events.set(name, { name, labels: [], shifts: [] });
        const event = events.get(name);
        if (entry.eventLabel && !event.labels.includes(entry.eventLabel)) {
          event.labels.push(entry.eventLabel);
        }
        if (!event.shifts.includes(shift)) event.shifts.push(shift);
      }
    }
    return [...events.values()];
  }

  const DEFAULT_EVENT_IMAGE = {
    src: "assets/events/flower.svg",
    alt: "イベントを飾るオリジナルの花の仮イラスト"
  };

  function eventImageFor(data, key, name) {
    return data.eventImages?.events?.[key]?.[name]
      ?? data.eventImages?.maids?.[name]
      ?? data.eventImages?.fallback
      ?? DEFAULT_EVENT_IMAGE;
  }

  const EMPTY_OBSERVATIONS = {
    schemaVersion: 1, complete: false, checkedAt: null, lastSuccessAt: null,
    posts: [], pending: [], lastRun: { status: "never" }
  };
  const EMPTY_PERSONAL_SHIFTS = {
    schemaVersion: 1, complete: false, checkedAt: null, lastSuccessAt: null,
    posts: [], lastRun: { status: "never" }
  };
  const EMPTY_HALF_MONTH_SCHEDULES = {
    schemaVersion: 1, complete: false, checkedAt: null, lastSuccessAt: null,
    schedules: [], lastRun: { status: "never" }
  };

  function validDateKey(key) {
    return typeof key === "string" && /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(key) &&
      Number(key.slice(0, 4)) > 0 &&
      Number.isFinite(Date.parse(`${key}T00:00:00Z`)) &&
      new Date(`${key}T00:00:00Z`).toISOString().slice(0, 10) === key;
  }

  function validateMemberRegistry(value) {
    const fields = (object, keys) => object && typeof object === "object" && !Array.isArray(object) &&
      Object.keys(object).length === keys.length && keys.every((key) => Object.hasOwn(object, key));
    const invalid = () => { throw new Error("メンバー名簿の形式・本人情報が不正です"); };
    const nameOK = (name) => typeof name === "string" && name.trim() === name &&
      /^[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}$/.test(name);
    const reserved = new Set(["about", "account", "accounts", "compose", "download", "explore", "hashtag",
      "help", "home", "i", "intent", "jobs", "login", "logout", "messages", "notifications", "privacy",
      "search", "settings", "share", "signup", "tos", "widgets", "akibazettai"]);
    if (!fields(value, ["schemaVersion", "members", "unresolvedNames"]) || value.schemaVersion !== 1 ||
        !Array.isArray(value.members) || !Array.isArray(value.unresolvedNames)) invalid();
    const ids = new Map();
    const names = new Set();
    const handles = new Set();
    for (const member of value.members) {
      if (!fields(member, ["memberId", "canonicalName", "displayName", "aliases", "role", "membership",
        "collection", "inactiveFrom", "xProfileUrl", "homeStore", "homeStoreSource", "officialListing", "orderBefore"]) ||
          typeof member.memberId !== "string" || member.memberId.length !== 34 ||
          !/^m-[0-9a-f]{32}$/.test(member.memberId) || ids.has(member.memberId) ||
          !nameOK(member.canonicalName) || !nameOK(member.displayName) ||
          !Array.isArray(member.aliases) || Array.from(member.aliases).some((name) => !nameOK(name)) ||
          new Set(member.aliases).size !== member.aliases.length || member.aliases.includes(member.canonicalName) ||
          !["normal", "kitchen", "trainee", "unknown"].includes(member.role) ||
          !["active", "inactive", "unconfirmed"].includes(member.membership) ||
          !["enabled", "paused", "review"].includes(member.collection) ||
          (member.membership !== "active" && member.collection === "enabled") ||
          (member.inactiveFrom !== null && (!validDateKey(member.inactiveFrom) || member.membership !== "inactive")) ||
          !["listed", "unlisted", "unknown"].includes(member.officialListing) ||
          (member.homeStore === null ? member.homeStoreSource !== null
            : !["s1", "s2", "s3", "s4"].includes(member.homeStore) ||
              !["official", "legacy-reviewed", "user-provided"].includes(member.homeStoreSource))) invalid();
      for (const name of new Set([member.canonicalName, member.displayName, ...member.aliases])) {
        if (names.has(name)) invalid();
        names.add(name);
      }
      if (member.xProfileUrl !== null) {
        const match = typeof member.xProfileUrl === "string" &&
          /^https:\/\/x\.com\/([a-z0-9_]{1,15})$/.exec(member.xProfileUrl);
        if (!match || match[0] !== member.xProfileUrl || reserved.has(match[1]) || handles.has(match[1])) invalid();
        handles.add(match[1]);
      }
      ids.set(member.memberId, member);
    }
    for (const member of value.members) {
      if (member.orderBefore !== null && (typeof member.orderBefore !== "string" ||
          member.role !== "normal" || ids.get(member.orderBefore)?.role !== "normal")) invalid();
      const seen = new Set();
      let current = member;
      while (current.orderBefore !== null) {
        if (seen.has(current.memberId)) invalid();
        seen.add(current.memberId);
        current = ids.get(current.orderBefore);
        if (!current) invalid();
      }
    }
    for (const name of value.unresolvedNames) {
      if (!nameOK(name) || names.has(name)) invalid();
      names.add(name);
    }
    return value;
  }

  function createMemberRegistryAdapter(value) {
    validateMemberRegistry(value);
    const members = Object.freeze(value.members.map((member) =>
      Object.freeze({ ...member, aliases: Object.freeze([...member.aliases]) })));
    const byId = new Map(members.map((member) => [member.memberId, member]));
    const byName = new Map(members.flatMap((member) =>
      [...new Set([member.canonicalName, member.displayName, ...member.aliases])].map((name) => [name, member])));
    const ordered = [];
    const append = (member) => {
      members.filter((candidate) => candidate.orderBefore === member.memberId).forEach(append);
      ordered.push(member);
    };
    members.filter((member) => member.orderBefore === null).forEach(append);
    const names = (select) => Object.freeze(ordered.filter(select).map((member) => member.canonicalName));
    return Object.freeze({
      members,
      activeNames: names((member) => member.membership === "active"),
      knownNames: names(() => true),
      kitchenStaff: names((member) => member.role === "kitchen"),
      unresolvedNames: Object.freeze([...value.unresolvedNames]),
      homeStore: Object.freeze(Object.fromEntries(members.filter((member) => member.homeStore)
        .map((member) => [member.canonicalName, member.homeStore]))),
      displayNames: Object.freeze(Object.fromEntries([...byName].map(([name, member]) => [name, member.displayName]))),
      normalOrderBefore: Object.freeze(Object.fromEntries(members.filter((member) => member.orderBefore)
        .map((member) => [member.canonicalName, byId.get(member.orderBefore).canonicalName]))),
      member: (name) => byName.get(name) ?? null,
      canonicalName: (name) => byName.get(name)?.canonicalName ?? name,
      profileUrl: (name) => byName.get(name)?.xProfileUrl ?? null,
      aliases: () => new Map([...byName].map(([name, member]) => [name, member.canonicalName]))
    });
  }

  function profileHandleFor(insights, name) {
    return insights?.memberRegistry
      ? insights.memberRegistry.profileUrl(name)?.split("/").at(-1) ?? null
      : maidTendencyFor(insights, name)?.x ?? null;
  }

  function maidTendencyFor(insights, name) {
    return Object.hasOwn(insights?.maidTendency ?? {}, name) ? insights.maidTendency[name] : null;
  }

  function memberPlanReview(registry, name, key, today = tokyoToday()) {
    const member = registry?.member(name);
    return member && key >= today && member.inactiveFrom === null &&
      member.membership !== "active"
      ? "在籍・収集状態は要確認です。適用日が未確認のため、保存済み予定を表示しています。" : null;
  }

  function memberFilterNames(registry, { schedule, insights, observations, personal, dateFrom, dateTo,
    nameCorrections = {} } = {}) {
    const evidence = new Set();
    const add = (name) => evidence.add(registry.canonicalName(name));
    for (const [key, day] of Object.entries(schedule ?? {})) {
      if (isDateKeyInRange(key, dateFrom, dateTo)) Object.values(day).flat().forEach((entry) => add(entry.name));
    }
    for (const [key, day] of Object.entries(insights?.actualRoster ?? {})) {
      if (isDateKeyInRange(key, dateFrom, dateTo)) Object.values(day).forEach((record) =>
        Object.values(record.stores ?? {}).flat().forEach(add));
    }
    for (const post of observations?.posts ?? []) {
      if (isDateKeyInRange(post.date, dateFrom, dateTo)) {
        [...post.names, ...(post.notices ?? []).map((notice) => notice.name)]
          .forEach((name) => add(nameCorrections[post.id]?.[name]?.name ?? name));
      }
    }
    for (const post of personal?.posts ?? []) {
      if (isDateKeyInRange(post.date, dateFrom, dateTo)) add(post.name);
    }
    return [...registry.activeNames, ...[...registry.knownNames, ...registry.unresolvedNames]
      .filter((name) => !registry.activeNames.includes(name) && evidence.has(name))];
  }

  function halfMonthPeriod(key) {
    if (!validDateKey(key)) return null;
    const prefix = key.slice(0, 7);
    if (Number(key.slice(8)) <= 15) return { from: `${prefix}-01`, to: `${prefix}-15` };
    const nextMonth = new Date(`${prefix}-01T00:00:00Z`);
    nextMonth.setUTCMonth(nextMonth.getUTCMonth() + 1);
    nextMonth.setUTCDate(0);
    return { from: `${prefix}-16`, to: nextMonth.toISOString().slice(0, 10) };
  }

  const WORK_TIMING_TERMS = {
    short: { shift: "昼", boundary: "end", time: "16:00", label: "短め" },
    long: { shift: "昼", boundary: "end", time: "18:00", label: "ながめ" },
    early: { shift: "夜", boundary: "start", time: "16:00", label: "早め" },
    late: { shift: "夜", boundary: "start", time: "18:00", label: "おそめ" }
  };

  function validateWorkTiming(value, { owner, date, days, sourceKind } = {}) {
    const fields = (object, keys) => object && typeof object === "object" && !Array.isArray(object) &&
      Object.keys(object).length === keys.length && keys.every((key) => Object.hasOwn(object, key));
    const invalid = () => { throw new Error("勤務時間の根拠が不正です"); };
    if (!fields(value, ["schemaVersion", "facts"]) || value.schemaVersion !== 1 ||
        !Array.isArray(value.facts) || value.facts.length > 512) invalid();
    const seen = new Set();
    for (const fact of value.facts) {
      if (!fields(fact, ["serviceDate", "shift", "boundary", "status", "qualifier", "explicitTime", "source"]) ||
          !validDateKey(fact.serviceDate) || !["昼", "夜"].includes(fact.shift) ||
          !["start", "end"].includes(fact.boundary) || !["set", "excluded", "withdrawn", "conflict"].includes(fact.status) ||
          (fact.qualifier !== null && (typeof fact.qualifier !== "string" || !Object.hasOwn(WORK_TIMING_TERMS, fact.qualifier))) ||
          (fact.explicitTime !== null && (typeof fact.explicitTime !== "string" ||
            !/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(fact.explicitTime)))) invalid();
      const key = JSON.stringify([fact.serviceDate, fact.shift, fact.boundary,
        ...(fact.status === "excluded" ? ["excluded", fact.qualifier, fact.explicitTime] : [])]);
      if (seen.has(key) || (date !== undefined && fact.serviceDate !== date) ||
          (days && !days[fact.serviceDate]?.includes(fact.shift))) invalid();
      seen.add(key);
      if (fact.status === "set" || fact.status === "excluded") {
        if (fact.qualifier === null && fact.explicitTime === null) invalid();
        if (fact.status === "excluded" && fact.qualifier !== null && fact.explicitTime !== null) invalid();
        if (fact.qualifier !== null && (WORK_TIMING_TERMS[fact.qualifier].shift !== fact.shift ||
            WORK_TIMING_TERMS[fact.qualifier].boundary !== fact.boundary)) invalid();
      } else if (fact.qualifier !== null || fact.explicitTime !== null) invalid();
      const source = fact.source;
      if (!fields(source, ["id", "url", "name", "authorId", "authorScreenName", "createdAt", "sourceKind"]) ||
          typeof source.id !== "string" || !/^[1-9][0-9]{9,24}$/.test(source.id) ||
          typeof source.authorId !== "string" || !/^[1-9][0-9]{0,24}$/.test(source.authorId) ||
          typeof source.authorScreenName !== "string" || !/^[A-Za-z0-9_]{1,15}$/.test(source.authorScreenName) ||
          source.url !== `https://x.com/${source.authorScreenName}/status/${source.id}` ||
          typeof source.name !== "string" || !/^[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}$/.test(source.name) ||
          !["personal-work-post", "half-month-schedule"].includes(source.sourceKind) ||
          typeof source.createdAt !== "string" ||
          !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$/.test(source.createdAt) ||
          !validDateKey(source.createdAt.slice(0, 10)) || !Number.isFinite(Date.parse(source.createdAt)) ||
          Math.abs(Number((BigInt(source.id) >> 22n) + 1288834974657n) - Date.parse(source.createdAt)) >= 2000 ||
          (sourceKind && source.sourceKind !== sourceKind)) invalid();
      if (owner) {
        if (["name", "authorId", "authorScreenName"].some((field) => owner[field] !== source[field])) invalid();
        if (source.sourceKind === "personal-work-post") {
          if (["id", "url", "createdAt"].some((field) => owner[field] !== source[field])) invalid();
        } else if (comparePosts(source, owner) > 0) invalid();
      }
    }
    return value;
  }

  function workTimingLabel(fact) {
    if (!fact || fact.status !== "set") return null;
    const word = WORK_TIMING_TERMS[fact.qualifier]?.label;
    const byTime = Object.values(WORK_TIMING_TERMS).find((term) =>
      term.shift === fact.shift && term.boundary === fact.boundary && term.time === fact.explicitTime)?.label;
    return word && byTime && word !== byTime ? null : word ?? byTime ?? null;
  }

  function workTimingTargetMatches(exclusion, fact) {
    if (!fact || fact.status !== "set") return false;
    const term = fact.qualifier ?? Object.keys(WORK_TIMING_TERMS).find((word) => {
      const value = WORK_TIMING_TERMS[word];
      return value.shift === fact.shift && value.boundary === fact.boundary && value.time === fact.explicitTime;
    });
    return exclusion.qualifier !== null ? exclusion.qualifier === term
      : exclusion.explicitTime === (fact.explicitTime ?? WORK_TIMING_TERMS[term]?.time);
  }

  function workTimingDescription(fact) {
    const label = workTimingLabel(fact);
    if (!label) return "";
    if (fact.explicitTime !== null) {
      const origin = fact.source.sourceKind === "half-month-schedule" ? "半月予定表" : "本人投稿";
      return `${label}・${fact.explicitTime}${fact.boundary === "start" ? "入り" : "まで"}（${origin}に時刻記載）`;
    }
    const term = Object.values(WORK_TIMING_TERMS).find((value) => value.label === label);
    return `${label}（明示語に基づく補足。店舗の呼称では${term.shift}${term.time}${term.boundary === "start" ? "入り" : "まで"}）`;
  }

  function workTimingPresentation(value, key, shift) {
    return { schemaVersion: 1, facts: value.facts
      .filter((fact) => (!key || fact.serviceDate === key) && (!shift || fact.shift === shift))
      .map((fact) => ({
        serviceDate: fact.serviceDate, shift: fact.shift, boundary: fact.boundary, status: fact.status,
        qualifier: fact.qualifier, explicitTime: fact.explicitTime,
        source: Object.fromEntries(["id", "url", "name", "authorId", "authorScreenName", "createdAt", "sourceKind"]
          .map((field) => [field, fact.source[field]]))
      })).sort((a, b) => JSON.stringify([a.serviceDate, a.shift, a.boundary, a.status, a.qualifier, a.explicitTime])
        .localeCompare(JSON.stringify([b.serviceDate, b.shift, b.boundary, b.status, b.qualifier, b.explicitTime])))
    };
  }

  function resolveWorkTiming({ personal, schedule, insights, dateKey, shift, name, personalEventAdditions }) {
    const aliases = displayAliases(insights);
    const boundary = shift === "昼" ? "end" : "start";
    const claims = [...(schedule?.[dateKey]?.[shift]?.find((entry) => entry.name === name)?.workTiming?.facts ?? [])];
    for (const post of personalPostsForView(personal, personalEventAdditions)) {
      if (post.date !== dateKey || (aliases.get(post.name) ?? post.name) !== name) continue;
      claims.push(...(post.workTiming?.facts ?? []));
      // Explicit work withdrawal blocks old hours too; a later return without
      // new hours cannot resurrect the old half-month timing.
      if (post.events.some((event) => event.shift === shift && event.kind === "absence") ||
          post.links?.some((link) => link.scope === shift && ["withdrawn", "conflict"].includes(link.status))) {
        claims.push({
          serviceDate: dateKey, shift, boundary, status: "withdrawn", qualifier: null, explicitTime: null,
          source: { ...post, sourceKind: "personal-work-post" }
        });
      }
    }
    const priority = (fact) => fact.source.sourceKind === "personal-work-post" ? 1 : 0;
    const candidates = claims.filter((fact) => fact.serviceDate === dateKey && fact.shift === shift && fact.boundary === boundary)
      .sort((left, right) => priority(left) - priority(right) || comparePosts(left.source, right.source) ||
        Number(left.status !== "set") - Number(right.status !== "set"));
    let current = null;
    for (const fact of candidates) {
      if (fact.status === "excluded") {
        if (workTimingTargetMatches(fact, current)) current = null;
      } else {
        current = fact;
      }
    }
    return workTimingLabel(current) ? current : null;
  }

  function attachWorkTiming(entries, options) {
    return entries.map((entry) => {
      const workTimingNote = resolveWorkTiming({ ...options, name: entry.name });
      if (!workTimingNote) return entry;
      return { ...entry, workTimingNote };
    });
  }

  function validateHalfMonthSchedules(value, { roster, insights, previous, personal, registry = insights?.memberRegistry } = {}) {
    const fields = (object, keys) => object && typeof object === "object" && !Array.isArray(object) &&
      Object.keys(object).length === keys.length && keys.every((key) => Object.hasOwn(object, key));
    const utcTime = (time) => typeof time === "string" &&
      /^[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$/.test(time) &&
      validDateKey(time.slice(0, 10)) && Number.isFinite(Date.parse(time));
    const statuses = ["never", "ok", "partial", "unavailable", "no-new", "no-results",
      "paused", "budget-exhausted", "outside-window"];
    if (!fields(value, ["schemaVersion", "complete", "checkedAt", "lastSuccessAt", "schedules", "lastRun"]) ||
        value.schemaVersion !== 1 || value.complete !== false || !Array.isArray(value.schedules) ||
        !fields(value.lastRun, ["status"]) || !statuses.includes(value.lastRun.status) ||
        [value.checkedAt, value.lastSuccessAt].some((time) => time !== null && !utcTime(time)) ||
        (value.lastSuccessAt && (!value.checkedAt || Date.parse(value.lastSuccessAt) > Date.parse(value.checkedAt)))) {
      throw new Error("半月予定データの形式が対応していません");
    }
    const winners = new Set();
    const posts = new Map();
    const identities = new Map();
    const handles = new Map();
    const authors = new Map();
    const known = [...(previous?.schedules ?? []), ...(personal?.posts ?? [])];
    for (const source of value.schedules) {
      if (!fields(source, ["id", "url", "name", "authorId", "authorScreenName", "createdAt",
        "observedAt", "sourceKind", "period", "days", ...(Object.hasOwn(source, "workTiming") ? ["workTiming"] : [])]) ||
          typeof source.id !== "string" || !/^[1-9][0-9]{9,24}$/.test(source.id) ||
          typeof source.authorId !== "string" || !/^[1-9][0-9]{0,24}$/.test(source.authorId) ||
          typeof source.authorScreenName !== "string" || !/^[A-Za-z0-9_]{1,15}$/.test(source.authorScreenName) ||
          source.url !== `https://x.com/${source.authorScreenName}/status/${source.id}` ||
          typeof source.name !== "string" || !source.name || source.name.trim() !== source.name ||
          /[\u0000-\u001f\u007f\u2028\u2029]/.test(source.name) ||
          (registry ? !registry.member(source.name) : roster && !roster.includes(source.name)) ||
          source.sourceKind !== "half-month-schedule" ||
          !utcTime(source.createdAt) || !utcTime(source.observedAt) ||
          Date.parse(source.observedAt) < Date.parse(source.createdAt) ||
          Math.abs(Number((BigInt(source.id) >> 22n) + 1288834974657n) - Date.parse(source.createdAt)) >= 2000) {
        throw new Error("半月予定の投稿・本人確認情報が不正です");
      }
      const handle = source.authorScreenName.toLowerCase();
      const canonical = registry ? registry.canonicalName(source.name) : source.name;
      const canonicalOf = (name) => registry ? registry.canonicalName(name) : name;
      // Published sources keep their original author binding; the current profile is not historical authority.
      const expectedHandle = registry ? null : maidTendencyFor(insights, source.name)?.x;
      const identity = `${source.authorId}|${handle}`;
      if ((expectedHandle && expectedHandle.toLowerCase() !== handle) ||
          (identities.has(canonical) && identities.get(canonical) !== identity) ||
          (handles.has(handle) && handles.get(handle) !== canonical) ||
          (authors.has(source.authorId) && authors.get(source.authorId) !== canonical) ||
          known.some((post) => canonicalOf(post.name) === canonical
            ? post.authorId !== source.authorId || post.authorScreenName.toLowerCase() !== handle
            : registry && (post.authorId === source.authorId || post.authorScreenName.toLowerCase() === handle))) {
        throw new Error("半月予定の本人情報が保存済み情報と一致しません");
      }
      identities.set(canonical, identity);
      handles.set(handle, canonical);
      authors.set(source.authorId, canonical);
      const period = source.period;
      const boundary = halfMonthPeriod(period?.from);
      if (!fields(period, ["from", "to", "printedYear", "yearBasis"]) || !boundary ||
          period.from !== boundary.from || period.to !== boundary.to ||
          !["printed", "text", "post-context"].includes(period.yearBasis) ||
          (period.yearBasis === "printed"
            ? !Number.isInteger(period.printedYear) || period.printedYear !== Number(period.from.slice(0, 4))
            : period.printedYear !== null)) {
        throw new Error("半月予定の対象期間・年の根拠が不正です");
      }
      const postDate = new Date(Date.parse(source.createdAt) + 9 * 3600000).toISOString().slice(0, 10);
      const monthNumber = (key) => Number(key.slice(0, 4)) * 12 + Number(key.slice(5, 7));
      if (period.yearBasis === "post-context" && Math.abs(monthNumber(postDate) - monthNumber(period.from)) > 1) {
        throw new Error("半月予定の対象年が投稿時期と一致しません");
      }
      const winner = `${canonical}|${period.from}`;
      const versions = posts.get(source.id) ?? [];
      if (winners.has(winner) || versions.length >= 2 || versions.some((other) =>
        other.name !== source.name || other.authorId !== source.authorId ||
        other.authorScreenName !== source.authorScreenName || other.createdAt !== source.createdAt ||
        (other.period.from <= period.to && period.from <= other.period.to))) {
        throw new Error("半月予定の人物・期間・投稿が重複しています");
      }
      winners.add(winner);
      posts.set(source.id, [...versions, source]);
      const dates = new Set();
      if (!Array.isArray(source.days) || source.days.length < 1 || source.days.length > 16) {
        throw new Error("半月予定の勤務日が不正です");
      }
      for (const day of source.days) {
        if (!fields(day, ["date", "shifts"]) || !validDateKey(day.date) ||
            day.date < period.from || day.date > period.to || dates.has(day.date) ||
            !Array.isArray(day.shifts) || day.shifts.length < 1 || day.shifts.length > 2 ||
            day.shifts.some((shift) => !["昼", "夜"].includes(shift)) ||
            new Set(day.shifts).size !== day.shifts.length) {
          throw new Error("半月予定の勤務日・昼夜が不正です");
        }
        dates.add(day.date);
      }
      if (Object.hasOwn(source, "workTiming")) {
        validateWorkTiming(source.workTiming, { owner: source, sourceKind: "half-month-schedule",
          days: Object.fromEntries(source.days.map((day) => [day.date, day.shifts])) });
      }
    }
    return value;
  }

  function halfMonthProvenance(source) {
    return Object.freeze({
      id: source.id, url: source.url, name: source.name,
      authorId: source.authorId, authorScreenName: source.authorScreenName,
      createdAt: source.createdAt, sourceKind: source.sourceKind,
      period: Object.freeze({
        from: source.period.from, to: source.period.to,
        printedYear: source.period.printedYear, yearBasis: source.period.yearBasis
      })
    });
  }

  function buildEffectiveSchedule(manual, snapshot = EMPTY_HALF_MONTH_SCHEDULES,
    { registry, today = tokyoToday(), includeInactivePlans = false } = {}) {
    const canonical = (name) => registry ? registry.canonicalName(name) : name;
    const included = (name, key) => includeInactivePlans || !registry?.member(name)?.inactiveFrom ||
      key < registry.member(name).inactiveFrom;
    const projection = (entry, key) => {
      const review = memberPlanReview(registry, entry.name, key, today);
      return { ...entry, name: canonical(entry.name), ...(review ? { memberReview: review } : {}) };
    };
    const result = {};
    for (const [key, day] of Object.entries(manual ?? {})) {
      result[key] = Object.fromEntries(Object.entries(day).map(([shift, entries]) =>
        [shift, entries.filter((entry) => included(entry.name, key)).map((entry) => projection(entry, key))]));
    }
    const sources = snapshot.schedules.slice().sort((a, b) =>
      `${a.name}|${a.period.from}`.localeCompare(`${b.name}|${b.period.from}`));
    for (const source of sources) {
      const provenance = halfMonthProvenance(source);
      for (const day of source.days.slice().sort((a, b) => a.date.localeCompare(b.date))) {
        if (!included(source.name, day.date)) continue;
        for (const shift of ["昼", "夜"].filter((item) => day.shifts.includes(item))) {
          const entries = (result[day.date] ??= {})[shift] ??= [];
          let entry = entries.find((item) => item.name === canonical(source.name));
          if (!entry) {
            entry = projection({ name: source.name }, day.date);
            entries.push(entry);
          }
          entry.halfMonthSources = [...(entry.halfMonthSources ?? []), provenance];
          const timing = source.workTiming && workTimingPresentation(source.workTiming, day.date, shift);
          if (timing?.facts.length) {
            entry.workTiming = Object.freeze({ schemaVersion: 1, facts: Object.freeze(timing.facts.map((fact) =>
              Object.freeze({ ...fact, source: Object.freeze(fact.source) }))) });
          }
        }
      }
    }
    for (const day of Object.values(result)) {
      for (const entries of Object.values(day)) {
        for (const entry of entries) {
          if (entry.halfMonthSources) Object.freeze(entry.halfMonthSources);
          Object.freeze(entry);
        }
        Object.freeze(entries);
      }
      Object.freeze(day);
    }
    return Object.freeze(result);
  }

  function validPersonalLinks(links) {
    return Array.isArray(links) && links.length <= 3 &&
      links.every((link) => link && Object.keys(link).length === 2 &&
        Object.hasOwn(link, "scope") && Object.hasOwn(link, "status") &&
        ["昼", "夜", "unspecified"].includes(link.scope) &&
        ["work", "withdrawn", "conflict"].includes(link.status) &&
        (link.scope !== "unspecified" || link.status === "work")) &&
      new Set(links.map((link) => link.scope)).size === links.length;
  }

  function validatePersonalShifts(value) {
    const statuses = ["never", "ok", "partial", "unavailable", "no-new", "no-results",
      "paused", "budget-exhausted", "outside-window"];
    const isoTime = (time) => typeof time === "string" &&
      /(?:Z|[+-]\d{2}:\d{2})$/.test(time) && Number.isFinite(Date.parse(time));
    if (!value || value.schemaVersion !== 1 || value.complete !== false ||
        !Array.isArray(value.posts) || !statuses.includes(value.lastRun?.status) ||
        [value.checkedAt, value.lastSuccessAt].some((time) => time !== null && !isoTime(time))) {
      throw new Error("本人案内データの形式が対応していません");
    }
    const ids = new Set();
    for (const post of value.posts) {
      if (!post || typeof post.id !== "string" || !/^\d{10,25}$/.test(post.id) || ids.has(post.id) ||
          typeof post.authorId !== "string" || !/^\d{1,25}$/.test(post.authorId) ||
          typeof post.authorScreenName !== "string" || !/^[A-Za-z0-9_]{1,15}$/.test(post.authorScreenName) ||
          post.url !== `https://x.com/${post.authorScreenName}/status/${post.id}` ||
          typeof post.name !== "string" || !post.name.trim() ||
          !isoTime(post.createdAt) || !isoTime(post.observedAt) ||
          !/^\d{4}-\d{2}-\d{2}$/.test(post.date) || !Number.isFinite(Date.parse(`${post.date}T00:00:00Z`)) ||
          new Date(`${post.date}T00:00:00Z`).toISOString().slice(0, 10) !== post.date ||
          (Object.hasOwn(post, "links") && !validPersonalLinks(post.links)) ||
          !Array.isArray(post.events) || (!post.events.length && !post.links?.length && !post.workTiming?.facts?.length) ||
          post.events.some((event) => !event || !["昼", "夜"].includes(event.shift) ||
            !["placement", "absence", "late", "return", "uncertain"].includes(event.kind) ||
            (event.kind === "placement" && !event.storeId) ||
            (event.kind === "absence" && event.storeId !== undefined) ||
            (event.storeId !== undefined && !["s1", "s2", "s3", "s4"].includes(event.storeId)) ||
            (event.time !== undefined && !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(event.time)) ||
            typeof event.excerpt !== "string" || !event.excerpt.trim() || [...event.excerpt].length > 160 ||
            /[\r\n\u2028\u2029]/.test(event.excerpt))) {
        throw new Error("本人ポストの検証情報が不正です");
      }
      if (Object.hasOwn(post, "workTiming")) {
        validateWorkTiming(post.workTiming, { owner: post, date: post.date, sourceKind: "personal-work-post" });
      }
      ids.add(post.id);
    }
    return value;
  }

  function validEditTweetIds(post) {
    const ids = post.editTweetIds;
    return Array.isArray(ids) && ids.length > 0 && ids.length <= 20 &&
      ids.every((id) => typeof id === "string" && /^[1-9][0-9]{9,24}$/.test(id)) &&
      new Set(ids).size === ids.length &&
      ids.every((id, index) => index === 0 || BigInt(ids[index - 1]) < BigInt(id)) &&
      ids.at(-1) === post.id;
  }

  function validateObservations(value) {
    if (!value || value.schemaVersion !== 1 || value.complete !== false ||
        !Array.isArray(value.posts) || !Array.isArray(value.pending) ||
        !["never", "ok", "partial", "unavailable", "no-new", "no-results"].includes(value.lastRun?.status)) {
      throw new Error("自動収集データの形式が対応していません");
    }
    const ids = new Set();
    const isoTime = (time) => typeof time === "string" &&
      /(?:Z|[+-]\d{2}:\d{2})$/.test(time) && Number.isFinite(Date.parse(time));
    const validContent = (post) => post &&
      /^\d{4}-\d{2}-\d{2}$/.test(post.date) && Number.isFinite(Date.parse(`${post.date}T00:00:00Z`)) &&
      new Date(`${post.date}T00:00:00Z`).toISOString().slice(0, 10) === post.date &&
      ["昼", "夜"].includes(post.shift) && ["s1", "s2", "s3", "s4"].includes(post.storeId) &&
      Array.isArray(post.names) && post.names.every((name) => typeof name === "string" && name.trim()) &&
      (post.notices === undefined || (Array.isArray(post.notices) && post.notices.every((notice) =>
        notice && typeof notice.name === "string" && notice.name.trim() && notice.kind === "late" &&
        typeof notice.excerpt === "string" && notice.excerpt.trim() &&
        [...notice.excerpt].length <= 160 && !/[\r\n\u2028\u2029]/.test(notice.excerpt) &&
        (notice.observedAt === undefined || isoTime(notice.observedAt)) &&
        (notice.time === undefined || /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(notice.time))))) &&
      (post.names.length > 0 || post.notices?.length > 0);
    for (const time of [value.checkedAt, value.lastSuccessAt]) {
      if (time !== null && !isoTime(time)) throw new Error("自動収集時刻が不正です");
    }
    for (const post of value.posts) {
      if (!post || typeof post.id !== "string" || !/^\d{10,25}$/.test(post.id) ||
          ids.has(post.id) || post.url !== `https://x.com/akibazettai/status/${post.id}` ||
          post.authorId !== "822429861218131969" || post.authorScreenName !== "akibazettai" ||
          !isoTime(post.createdAt) || !isoTime(post.observedAt) ||
          !validContent(post) ||
          (post.notices ?? []).some((notice) => notice.observedAt !== undefined &&
            Date.parse(notice.observedAt) < Date.parse(post.createdAt)) ||
          (post.editTweetIds !== undefined && !validEditTweetIds(post)) ||
          (post.lastCheckedAt !== undefined && !isoTime(post.lastCheckedAt)) ||
          (post.revisions !== undefined && (!Array.isArray(post.revisions) ||
            post.revisions.some((revision) => !validContent(revision) || !isoTime(revision.observedAt))))) {
        throw new Error("自動収集した投稿の検証情報が不正です");
      }
      ids.add(post.id);
    }
    return value;
  }

  function activeObservationPosts(observations) {
    const posts = observations?.posts ?? [];
    const byId = new Map(posts.map((post) => [post.id, post]));
    const verifiedChains = posts.filter((post) => {
      if (post.editTweetIds === undefined) return false;
      try {
        validateObservations({ ...EMPTY_OBSERVATIONS, posts: [post] });
        return true;
      } catch {
        return false;
      }
    });
    const earlierIds = (post) => post.editTweetIds.slice(0, -1).filter((id) => byId.get(id)?.date === post.date);
    // A verified replacement remains evidence after its successor is superseded.
    const superseded = new Set(verifiedChains.flatMap(earlierIds));
    return posts.filter((post) => !superseded.has(post.id));
  }

  function observedShift(observations, insights, key, shift, nameCorrections = {}) {
    const result = { posts: [], byStore: new Map(), byMaid: new Map(), notices: new Map() };
    // A curated person roster takes precedence; observations are not a second
    // training source and never mutate actual/actualRoster/rotation.
    if (insights?.actualRoster?.[key]?.[shift]) return result;
    const aliases = displayAliases(insights);
    for (const post of activeObservationPosts(observations)) {
      if (post.date !== key || post.shift !== shift) continue;
      result.posts.push(post);
      if (!result.byStore.has(post.storeId)) result.byStore.set(post.storeId, new Map());
      const store = result.byStore.get(post.storeId);
      for (const rawName of post.names) {
        const postCorrections = nameCorrections?.[post.id];
        const correction = postCorrections && Object.hasOwn(postCorrections, rawName)
          ? postCorrections[rawName] : null;
        const matchedName = correction?.name ?? rawName;
        const name = aliases.get(matchedName) ?? matchedName;
        if (!store.has(name)) store.set(name, []);
        if (!store.get(name).some((source) => source.id === post.id)) store.get(name).push(post);
        if (!result.byMaid.has(name)) result.byMaid.set(name, {
          storeIds: [], sources: [], trainee: observedTrainee(insights, name, key),
          nameCorrections: []
        });
        const person = result.byMaid.get(name);
        if (!person.storeIds.includes(post.storeId)) person.storeIds.push(post.storeId);
        if (!person.sources.some((source) => source.id === post.id)) person.sources.push(post);
        if (correction && !person.nameCorrections.some((entry) => entry.postId === post.id && entry.rawName === rawName)) {
          person.nameCorrections.push({ postId: post.id, rawName, name, reason: correction.reason });
        }
      }
      for (const notice of post.notices ?? []) {
        const postCorrections = nameCorrections?.[post.id];
        const correction = postCorrections && Object.hasOwn(postCorrections, notice.name)
          ? postCorrections[notice.name] : null;
        const matchedName = correction?.name ?? notice.name;
        const name = aliases.get(matchedName) ?? matchedName;
        const previous = result.notices.get(name);
        if (!previous || comparePosts(previous.sources[0], post) < 0) {
          result.notices.set(name, {
            name, storeId: post.storeId, time: notice.time ?? null, excerpt: notice.excerpt,
            sources: [post], trainee: observedTrainee(insights, name, key), conflict: false,
            nameCorrections: correction
              ? [{ postId: post.id, rawName: notice.name, name, reason: correction.reason }] : []
          });
        }
      }
    }
    for (const [name, notice] of result.notices) {
      const recorded = result.byMaid.get(name);
      if (!recorded) continue;
      if (recorded.sources.some((post) => comparePosts(post, notice.sources[0]) > 0)) {
        result.notices.delete(name);
      } else {
        result.byMaid.delete(name);
        for (const people of result.byStore.values()) people.delete(name);
      }
    }
    return result;
  }

  function comparePosts(left, right) {
    return Date.parse(left.createdAt) - Date.parse(right.createdAt) ||
      left.id.length - right.id.length || left.id.localeCompare(right.id);
  }

  function personalPostsForView(snapshot, additions = {}) {
    return (snapshot?.posts ?? []).map((post) => {
      if (!additions || !/^\d{10,25}$/.test(post.id) || !Object.hasOwn(additions, post.id)) return post;
      const rule = additions[post.id];
      if (!rule || !["name", "authorId", "authorScreenName", "date"].every((field) => rule[field] === post[field]) ||
          !Array.isArray(rule.events)) return post;
      const shifts = new Set(post.events.map((event) => event.shift));
      const extra = rule.events.filter((event) => {
        if (!event || shifts.has(event.shift)) return false;
        shifts.add(event.shift);
        return true;
      });
      if (!extra.length) return post;
      const amended = { ...post, events: [...post.events, ...extra] };
      if (Object.hasOwn(post, "links") && validPersonalLinks(post.links)) {
        // Preserve reviewed view-only scopes when a partial analysis supplies only the other shift.
        const supplied = new Set(post.links.map((link) => link.scope));
        amended.links = [...post.links, ...extra
          .filter((event) => ["placement", "late", "return"].includes(event.kind) && !supplied.has(event.shift))
          .map((event) => ({ scope: event.shift, status: "work" }))];
      }
      try {
        validatePersonalShifts({ ...EMPTY_PERSONAL_SHIFTS, posts: [amended] });
        return amended;
      } catch {
        return post;
      }
    });
  }

  function personalPostLink({ personal, insights, observations, dateKey, shift, name,
    nameCorrections, personalEventAdditions }) {
    if (!SHIFT_NAMES.includes(shift)) return null;
    const aliases = displayAliases(insights);
    const canonical = aliases.get(name) ?? name;
    const account = profileHandleFor(insights, canonical);
    const posts = personalPostsForView(personal, personalEventAdditions).filter((post) => {
      if (post.date !== dateKey || (aliases.get(post.name) ?? post.name) !== canonical) return false;
      try {
        validatePersonalShifts({ ...EMPTY_PERSONAL_SHIFTS, posts: [post] });
        return tokyoToday(new Date(post.createdAt)) === dateKey &&
          (insights?.memberRegistry ? Boolean(insights.memberRegistry.member(post.name))
            : !account || post.authorScreenName.toLowerCase() === account.toLowerCase());
      } catch {
        return false;
      }
    });
    if (!posts.length) return null;
    // Curated placement wins the roster, but does not replace same-day link history.
    const official = observedShift(observations, { ...insights, actualRoster: null },
      dateKey, shift, nameCorrections);
    const notice = official.notices.get(canonical);
    const history = [
      ...posts.map((post) => ({ post })),
      ...(official.byMaid.get(canonical)?.sources ?? []).map((post) => ({ post, official: true })),
      ...(notice?.sources ?? []).map((post) => ({ post, official: true, time: notice.time }))
    ].sort((left, right) => comparePosts(left.post, right.post));
    let source = null;
    let absent = false;
    let held = false;
    const confirmedEvents = (post) => post.events.filter((event) =>
      event.shift === shift && event.kind !== "uncertain");
    const contradicts = (events, storeId, time) => events.some((event) =>
      (storeId && event.storeId && event.storeId !== storeId) ||
      (time && event.time && event.time !== time));
    for (const item of history) {
      const { post } = item;
      if (item.official) {
        if (source && contradicts(confirmedEvents(source), post.storeId, item.time)) source = null;
        continue;
      }
      const events = confirmedEvents(post);
      const cancelled = events.some((event) => event.kind === "absence");
      const returned = events.some((event) => event.kind === "return");
      const conflicting = (cancelled && events.some((event) => event.kind !== "absence")) ||
        new Set(events.map((event) => event.storeId).filter(Boolean)).size > 1;
      if (cancelled || returned) {
        absent = cancelled;
        source = null;
        if (returned && !cancelled) held = false;
      }
      if (source && events.some((event) =>
        contradicts(confirmedEvents(source), event.storeId, event.time))) source = null;
      const links = Object.hasOwn(post, "links") ? post.links : events.map((event) => ({
        scope: event.shift, status: event.kind === "absence" ? "withdrawn" : "work"
      }));
      const link = links.find((link) => link.scope === shift) ??
        links.find((link) => link.scope === "unspecified");
      if (conflicting || link?.status === "withdrawn" || link?.status === "conflict") {
        held = true;
        source = null;
      } else if (link?.status === "work" && !absent && !(held && link.scope === "unspecified")) {
        held = false;
        source = post;
      }
    }
    return source;
  }

  function rosterPostLink({ schedule, ...options }) {
    const post = personalPostLink(options);
    if (post) return { post, kind: "personal" };
    const { insights, dateKey, shift, name } = options;
    if (!SHIFT_NAMES.includes(shift)) return null;
    const aliases = displayAliases(insights);
    const canonical = aliases.get(name) ?? name;
    const account = profileHandleFor(insights, canonical);
    // Only use provenance attached to this exact person/date/shift, not a period-wide lookup.
    const source = schedule?.[dateKey]?.[shift]?.find((entry) => entry.name === canonical)
      ?.halfMonthSources?.find((source) => (aliases.get(source.name) ?? source.name) === canonical &&
        source.sourceKind === "half-month-schedule" &&
        source.period?.from <= dateKey && dateKey <= source.period?.to &&
        typeof source.id === "string" && /^[1-9][0-9]{9,24}$/.test(source.id) &&
        typeof source.authorScreenName === "string" && /^[A-Za-z0-9_]{1,15}$/.test(source.authorScreenName) &&
        source.url === `https://x.com/${source.authorScreenName}/status/${source.id}` &&
        (insights?.memberRegistry ? Boolean(insights.memberRegistry.member(source.name)) &&
          typeof source.authorId === "string" && /^[1-9][0-9]{0,24}$/.test(source.authorId)
          : !account || source.authorScreenName.toLowerCase() === account.toLowerCase()));
    return source ? { post: source, kind: "half-month-schedule" } : null;
  }

  function observedTrainee(insights, name, key) {
    const period = insights?.traineePeriods?.byName?.[name];
    if (!period) return null;
    return key >= period.from && key <= period.to;
  }

  function dayHasPersonStoreEvidence(insights, observations, key, personal, personalEventAdditions = {}) {
    if (SHIFT_NAMES.some((shift) => recordedAssignment(insights, key, shift))) return true;
    const stores = new Set(storesOf(insights).map((store) => store.id));
    // Read retained source history, not filtered/current placements: a later
    // cancellation must not send the remaining people back into guessed shops.
    return (observations?.posts ?? []).some((post) =>
      post.date === key && SHIFT_NAMES.includes(post.shift) &&
      stores.has(post.storeId) && (post.names.length > 0 || post.notices?.length > 0)) ||
      personalPostsForView(personal, personalEventAdditions).some((post) => post.date === key && post.events.some((event) =>
        SHIFT_NAMES.includes(event.shift) && ["placement", "return", "late"].includes(event.kind) && stores.has(event.storeId)));
  }

  function personalShift(snapshot, insights, key, shift, observed, personalEventAdditions = {}) {
    const result = { posts: [], byMaid: new Map() };
    if (insights?.actualRoster?.[key]?.[shift]) return result;
    const aliases = displayAliases(insights);
    const posts = personalPostsForView(snapshot, personalEventAdditions).filter((post) =>
      post.date === key && post.events.some((event) => event.shift === shift))
      .sort(comparePosts);
    for (const post of posts) {
      result.posts.push(post);
      const name = aliases.get(post.name) ?? post.name;
      if (!result.byMaid.has(name)) result.byMaid.set(name, {
        name, sources: [], history: [], storeId: null, placementSource: null, absent: false, returned: false,
        late: null, lateSource: null, uncertain: false, conflict: false, suppressObserved: false, resetAt: null,
        superseded: false,
        trainee: observedTrainee(insights, name, key)
      });
      const person = result.byMaid.get(name);
      person.sources.push(post);
      for (const event of post.events.filter((event) => event.shift === shift)) {
        person.history.push({ post, event });
        person.uncertain = event.kind === "uncertain";
        if (person.uncertain) continue;
        if (event.kind === "late") {
          person.late = { time: event.time ?? null };
          person.lateSource = post;
          if (!person.absent && event.storeId) {
            person.storeId = event.storeId;
            person.placementSource = post;
          }
        } else if (event.kind === "absence") {
          person.absent = true;
          person.storeId = null;
          person.placementSource = null;
          person.returned = false;
          person.late = null;
          person.lateSource = null;
          person.resetAt = Date.parse(post.createdAt);
        } else if (event.kind === "return") {
          person.absent = false;
          person.returned = true;
          person.storeId = event.storeId ?? null;
          person.placementSource = event.storeId ? post : null;
          person.late = null;
          person.lateSource = null;
          person.resetAt = Date.parse(post.createdAt);
        } else if (!person.absent) {
          person.storeId = event.storeId;
          person.placementSource = post;
          person.late = null;
          person.lateSource = null;
        }
      }
    }
    for (const person of result.byMaid.values()) {
      const official = observed?.byMaid.get(person.name);
      if (!official) continue;
      // An explicit cancellation/return supersedes earlier announcements, but a
      // newer contradictory collection post is not an implicit return.
      const relevant = official.sources.filter((post) =>
        person.resetAt === null || Date.parse(post.createdAt) > person.resetAt);
      if (!relevant.length) {
        person.suppressObserved = true;
      } else if (person.absent) {
        person.conflict = true;
        person.absent = false;
        person.storeId = null;
        person.placementSource = null;
        person.suppressObserved = true;
      } else if (person.storeId && relevant.some((post) => post.storeId !== person.storeId)) {
        const latest = relevant.reduce((left, right) => comparePosts(left, right) >= 0 ? left : right);
        if (person.placementSource && comparePosts(person.placementSource, latest) > 0) {
          person.suppressObserved = true;
        } else {
          // A later collection supports the observed shop, not an older personal link.
          person.storeId = null;
          person.placementSource = null;
          person.superseded = !person.lateSource || comparePosts(person.lateSource, latest) <= 0;
          if (person.superseded) person.late = null;
        }
      }
    }
    return result;
  }

  function resolveShiftRoster({ insights, observations, personal, dateKey, shift, schedule, sourceSchedule = schedule,
    roster, nameCorrections, personalEventAdditions }) {
    const timingOptions = { insights, personal, dateKey, shift, schedule: sourceSchedule, personalEventAdditions };
    const recorded = recordedRoster({ insights, dateKey, shift, schedule: sourceSchedule, roster });
    if (recorded) return {
      ...recorded, entries: attachWorkTiming(recorded.entries.map(({ memberReview, ...entry }) => entry), timingOptions),
      observed: observedShift(null, insights, dateKey, shift),
      personal: { posts: [], byMaid: new Map() }
    };
    const observed = observedShift(observations, insights, dateKey, shift, nameCorrections);
    const notices = personalShift(personal, insights, dateKey, shift, observed, personalEventAdditions);
    for (const person of notices.byMaid.values()) {
      if (person.absent || person.suppressObserved) {
        observed.byMaid.delete(person.name);
        for (const people of observed.byStore.values()) people.delete(person.name);
      } else if (person.resetAt !== null && observed.byMaid.has(person.name)) {
        const official = observed.byMaid.get(person.name);
        official.sources = official.sources.filter((post) => Date.parse(post.createdAt) > person.resetAt);
        official.storeIds = [...new Set(official.sources.map((post) => post.storeId))];
        for (const [id, people] of observed.byStore) {
          const sources = official.sources.filter((post) => post.storeId === id);
          if (sources.length) people.set(person.name, sources);
          else people.delete(person.name);
        }
      }
    }
    const entries = new Map(observationEntries(schedule?.[dateKey]?.[shift] ?? [], observed, roster)
      .map((entry) => [entry.name, entry]));
    for (const person of notices.byMaid.values()) {
      if (person.absent) {
        entries.delete(person.name);
        continue;
      }
      if (!entries.has(person.name) && !person.storeId && !person.returned && !person.conflict) continue;
      const entry = entries.get(person.name) ?? { name: person.name };
      entries.set(person.name, {
        ...entry, personalNotice: person.superseded ? null : person,
        personalPlacement: Boolean(person.storeId && !entry.observed),
        trainee: person.trainee
      });
    }
    for (const [name, official] of observed.notices) {
      const person = notices.byMaid.get(name);
      if (person?.resetAt !== null && person?.resetAt !== undefined &&
          Date.parse(official.sources[0].createdAt) <= person.resetAt) continue;
      if (person?.placementSource && comparePosts(person.placementSource, official.sources[0]) > 0) continue;
      const conflict = official.conflict || Boolean(person?.absent || person?.conflict ||
        (person?.storeId && person.storeId !== official.storeId && !person.placementSource));
      const latestPersonal = person?.history.at(-1)?.post;
      const superseded = latestPersonal && comparePosts(latestPersonal, official.sources[0]) <= 0;
      if (!conflict && person?.lateSource && comparePosts(person.lateSource, official.sources[0]) <= 0) {
        person.late = null;
        person.lateSource = null;
      }
      const newerLate = person?.late && person?.lateSource &&
        comparePosts(person.lateSource, official.sources[0]) > 0;
      const guidance = {
        ...official,
        uncertain: Boolean(person?.uncertain && latestPersonal && !superseded),
        ...(!conflict && newerLate ? { time: person.late.time, arrivalSource: person.lateSource } : {})
      };
      if (!conflict && person && (superseded || (person.storeId && person.storeId !== official.storeId))) {
        person.storeId = null;
        person.placementSource = null;
        person.superseded = Boolean(superseded);
        if (person.superseded) {
          person.late = null;
          person.lateSource = null;
        }
      }
      if (conflict && person) {
        person.absent = false;
        person.conflict = true;
        person.storeId = null;
        person.placementSource = null;
      }
      const entry = entries.get(name) ?? { name };
      entries.set(name, {
        ...entry, officialNotice: { ...guidance, conflict, storeId: conflict ? null : official.storeId },
        personalNotice: person?.superseded ? null : entry.personalNotice,
        officialPlacement: !conflict, personalPlacement: false,
        trainee: entry.trainee ?? official.trainee
      });
    }
    return { assignment: null, observed, personal: notices, entries: attachWorkTiming([...entries.values()]
      .map((entry) => {
        if (!entry.observed && !entry.personalPlacement && !entry.officialPlacement) return entry;
        const { memberReview, ...confirmed } = entry;
        return confirmed;
      }), timingOptions) };
  }

  function observationEntries(planned, observed, roster) {
    const entries = new Map(planned.map((entry) => [entry.name, { ...entry }]));
    for (const name of observed.byMaid.keys()) {
      entries.set(name, {
        ...(entries.get(name) ?? {}), name, observed: true,
        nameCorrections: observed.byMaid.get(name).nameCorrections
      });
    }
    const rank = new Map(roster.map((name, index) => [name, index]));
    return [...entries.values()].sort((a, b) =>
      (rank.get(a.name) ?? Infinity) - (rank.get(b.name) ?? Infinity));
  }

  function orderRosterEntries(entries, roster, kitchen, normalOrderBefore = {}) {
    const cooks = kitchen instanceof Set ? kitchen : new Set(kitchen ?? []);
    const displayOrder = [...roster];
    for (const [name, before] of Object.entries(normalOrderBefore)) {
      // An anchor can leave the current roster; retain the saved rank in that case.
      if (!displayOrder.includes(name) || !displayOrder.includes(before) || name === before ||
          cooks.has(name) || cooks.has(before)) continue;
      displayOrder.splice(displayOrder.indexOf(name), 1);
      displayOrder.splice(displayOrder.indexOf(before), 0, name);
    }
    const rank = new Map(displayOrder.map((name, index) => [name, index]));
    const category = (entry) => cooks.has(entry.name) ? 3
      : entry.trainee === true ? 1 : rank.has(entry.name) ? 0 : 2;
    return [...entries].sort((left, right) => {
      const group = category(left) - category(right);
      if (group) return group;
      if (category(left) === 0 || category(left) === 3) {
        const official = (rank.get(left.name) ?? roster.length) - (rank.get(right.name) ?? roster.length);
        if (official) return official;
      }
      return left.name.localeCompare(right.name, "ja");
    });
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

  // 2026-09-01 の制度変更（上旬・下旬をまとめて事前公開）直後は、予定が未確認の
  // メイドさんがいて予定表が薄い。移行が落ち着けば不要になる注意書きなので、
  // 変更日から一定期間だけ出して自動で消えるようにする。
  const SCHEDULE_NOTE_DAYS = 60;

  function scheduleSystemNote(insights, todayKey, windowDays = SCHEDULE_NOTE_DAYS) {
    const changedAt = insights?.scheduleSystemChangedAt;
    if (!changedAt || !todayKey || todayKey < changedAt) {
      return null;
    }
    if (todayKey > addDays(changedAt, windowDays)) {
      return null;
    }
    const [year, month] = changedAt.split("-");
    return (
      `お給仕予定は${year}年${Number(month)}月から、上旬・下旬をまとめて公開する方式になりました。` +
      "予定が未確認のメイドさんは、この表に出ていないことがあります。"
    );
  }
  // 予定表が未確認の在籍者。揃うまで、画面の顔ぶれは実際より薄い。
  // 人数から店舗数を決めているので、開く店も少なめに出ることがある。
  //
  // 「何人ぶん薄いか」は windowShifts で割って出す。この分母はデータに入って
  // いるので推測にならない。入っていないときは書かない。推測した分母で割った
  // 数字は、測った数字の顔をしてしまう。
  function schedulePendingNote(insights, snapshot = EMPTY_HALF_MONTH_SCHEDULES,
    { dateFrom, dateTo, registry = insights?.memberRegistry, manual } = {}) {
    const pending = insights?.schedulePending;
    const legacyNames = registry ? registry.activeNames : pending?.pending;
    if (!Array.isArray(legacyNames) || legacyNames.length === 0) {
      return null;
    }
    const periods = [];
    if (validDateKey(dateFrom) && validDateKey(dateTo) && dateFrom <= dateTo) {
      let period = halfMonthPeriod(dateFrom);
      while (period && period.from <= dateTo) {
        periods.push(period);
        period = halfMonthPeriod(addDays(period.to, 1));
      }
    }
    const missing = new Map(legacyNames.map((name) => [name, periods.filter((period) =>
      !snapshot.schedules.some((source) => (registry ? registry.canonicalName(source.name) : source.name) === name &&
        source.period.from === period.from && source.period.to === period.to))]));
    const names = legacyNames.filter((name) => !periods.length || missing.get(name).length);
    if (!names.length) return null;
    const partlyConfirmed = names.filter((name) => periods.length > missing.get(name).length).length;
    const counts = pending?.recentShifts ?? {};
    const manualNames = new Set(Object.entries(manual ?? {}).filter(([key]) =>
      isDateKeyInRange(key, dateFrom, dateTo)).flatMap(([, day]) =>
      Object.values(day).flat().map((entry) => registry ? registry.canonicalName(entry.name) : entry.name)));
    const busiest = [...names].sort((a, b) => (counts[b] ?? 0) - (counts[a] ?? 0));
    const named = busiest
      .map((name) => (counts[name] > 0 ? `${name}（最近${counts[name]}回）` : name) +
        (registry && manualNames.has(name) ? "［手入力の予定あり・半月全体は未確認］" : "") +
        (partlyConfirmed && periods.length > missing.get(name).length
          ? `［${missing.get(name).map((period) => `${period.from}〜${period.to}`).join("・")} 未確認］` : ""))
      .join("・");
    const total = registry ? registry.activeNames.length : pending?.rostered;
    const window = pending?.windowShifts;
    const worked = names.reduce((sum, name) => sum + (counts[name] ?? 0), 0);
    const perShift = !partlyConfirmed && window > 0 && worked > 0
      ? `同じペースなら1シフトあたり${(worked / window).toFixed(1)}人ぶんです。`
      : "";
    const head = `${total > 0 ? `${registry ? "現在の" : ""}在籍${total}名のうち` : ""}${names.length}名の${periods.length ? "対象期間の" : ""}予定が未確認です` +
      (partlyConfirmed ? `（うち${partlyConfirmed}名は一部の半月を確認済み）` : "");
    return {
      short: head,
      long:
        `${head}：${named}。` +
        `未確認の期間があるため、顔ぶれは実際より少なめになることがあります。${perShift}` +
        "人数から開く店の数を決めているぶん、店も少なめに出ることがあります。" +
        (registry ? "手入力は記載された日・昼夜の根拠で、自動取得した半月予定表とは区別しています。" : "") +
        "未確認は未投稿・未提出・欠勤を意味しません。対象の半月すべてを確認できれば、この注意書きは消えます"
    };
  }

  // 記録が無いシフトは「休み」ではなく「情報なし」なので null を返す。
  function openStoresOn(insights, key, shift) {
    const ids = insights?.actual?.[key]?.[shift];
    return Array.isArray(ids) ? new Set(ids) : null;
  }

  // 昼夜をまとめた「その日開いていた店」。日単位の見方はシフト別より当たる。
  function openStoresOnDay(insights, key) {
    const record = insights?.actual?.[key];
    if (!record) {
      return null;
    }
    const ids = new Set();
    let seen = false;
    for (const value of Object.values(record)) {
      if (Array.isArray(value)) {
        seen = true;
        value.forEach((id) => ids.add(id));
      }
    }
    return seen ? ids : null;
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

  // 予測モードでは、同じ店に入りそうな人がまとまって見えるように並べ替える。
  function sortByAssignedStore({ insights, entries, assignment }) {
    if (!assignment) {
      return [...entries];
    }
    const order = new Map(storesOf(insights).map((store, index) => [store.id, index]));
    return entries
      .map((entry, index) => ({ entry, index }))
      .sort((a, b) => {
        const left = assignment.byMaid.get(a.entry.name);
        const right = assignment.byMaid.get(b.entry.name);
        const leftRank = left ? order.get(left.storeId) ?? Infinity : Infinity;
        const rightRank = right ? order.get(right.storeId) ?? Infinity : Infinity;
        return leftRank - rightRank || a.index - b.index;
      })
      .map(({ entry }) => entry);
  }

  // 店舗ごとにまとめて返す。店は店舗の並び順、店の中はサイト掲載順（roster 順）のまま。
  // 一本のリストに混ぜると、掲載順が飛ぶせいで並びが崩れて見える。
  function groupByAssignedStore({ insights, entries, assignment }) {
    if (!assignment) {
      return [{ storeId: null, entries: [...entries] }];
    }
    const groups = new Map(storesOf(insights).map((store) => [store.id, []]));
    const strays = [];
    entries.forEach((entry) => {
      const placed = assignment.byMaid.get(entry.name);
      if (placed && groups.has(placed.storeId)) {
        groups.get(placed.storeId).push(entry);
      } else {
        strays.push(entry);
      }
    });
    const ordered = [...groups]
      .filter(([, members]) => members.length > 0)
      .map(([storeId, members]) => ({ storeId, entries: members }));
    if (strays.length > 0) {
      ordered.push({ storeId: null, entries: strays });
    }
    return ordered;
  }

  // 予定表に出ない見習いにゃんこ。名前は分からないので人数だけ出す。
  //
  // データ側は「1店あたり何人」で持っている。開く店が増えるほど見習いも増える
  // ためで、店舗数が決まってから掛ける。
  //
  // 店ごとの内訳（`traineeOutlook[shift].byStore`）も持っているが、店ごとには
  // 配らない。平均人数は存在確率ではなく、店を断定する根拠にはならない。
  // 歴史的な比較は将来の事前予定表での評価ではないため、独立した見出しに置く。
  //
  // 以前ここに「1号店0.35 / 2号店0.35 / 3号店0.32と横並びなので選ぶ根拠がない」と
  // 書いていたが、測り直すと横並びではなかった（1号店がいちばん多く、4号店がいちばん
  // 少ない）。理由が違っていただけで、配らない結論は変わらない。
  //
  // 記録のある日には出さない。誰がいたか分かっているところに推測を混ぜない。
  function expectedTrainees(insights, shift, storeCount, recorded) {
    const rate = insights?.traineeOutlook?.[shift]?.perStore;
    if (recorded || !(rate > 0) || !(storeCount > 0)) {
      return 0;
    }
    return Math.round(rate * storeCount);
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

  // その店がふだん何人態勢なのか。4号店だけ2人少ないので、チップの補足に出す。
  function storeSizeNote(insights, shift, storeId) {
    const profile = insights?.headcountProfile?.[shift]?.[storeId];
    if (!profile) {
      return null;
    }
    const spread = profile.p25 === profile.p75
      ? `${profile.p25}人`
      : `${profile.p25}〜${profile.p75}人`;
    return [
      `${storeShort(insights, storeId)}の${shift}はふだん${profile.mode}人態勢` +
        `（${spread}が中心、${toPercent(profile.modeShare ?? 0)}が${profile.mode}人。` +
        `実績${profile.shifts}シフト）`,
      unlistedNote(insights, storeId)
    ]
      .filter(Boolean)
      .join("。");
  }

  // Coverage measures unlisted members, not the separate trainee classification.
  function unlistedNote(insights, storeId) {
    const coverage = insights?.rosterCoverage;
    const store = coverage?.byStore?.[storeId];
    if (!store || typeof store.cellsWithUnlistedRate !== "number") {
      return null;
    }
    const withAny = store.cellsWithUnlistedRate;
    if (withAny <= 0) {
      return null;
    }
    const months = monthsBetween(coverage.from, coverage.to);
    const period = months ? `直近${months}か月` : "この集計期間";
    return (
      `予定表の掲載対象外の方（見習いとは限りません）の集計です。${period}では、この店の` +
      `${toPercent(withAny)}の店舗枠に1人以上いました。予定が未確認の人数とは別です`
    );
  }

  function monthsBetween(from, to) {
    if (!from || !to) {
      return null;
    }
    const days = (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000;
    return Number.isFinite(days) ? Math.max(1, Math.round(days / 30)) : null;
  }

  function actualOutlook(insights, key, shift) {
    const open = openStoresOn(insights, key, shift);
    if (!open) {
      return null;
    }
    // 店舗だけ分かっていて顔ぶれの記録が無い日がある（メイドさんの当日投稿など）。
    // 営業したことは確かなので実績として扱うが、誰がいたかは分からないと断る。
    const storesOnly = Boolean(insights.actualWithoutRoster?.[key]?.[shift]);
    const summary = open.size > 0
      ? `${shift}は${joinStoreNames(insights, open)}が営業していました（公式Xの投稿で確認できた実績）。`
      : `${shift}に営業した店舗の記録がありません。`;
    return {
      basis: "actual",
      badge: "実績",
      badgeClass: "is-actual",
      openStores: [...open],
      summary: storesOnly
        ? `${summary}この日は営業した店舗だけが分かっていて、誰がいたかの記録はありません。下の顔ぶれは予定表からの割り振りです。`
        : summary,
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

  // 前日の同じシフトに記録があれば、そこから翌日を見る。実績は昼だけ・夜だけ
  // 入ることがあるので、「実績の最終日の翌日」で判定すると、記録の飛んだ側が
  // 前日を引けずに見込みを出せなくなる。前日そのものを見れば飛んでいても動く。
  // ただし記録より前の日には使わない。過ぎた日の抜けは予測ではなく「記録が無い」
  // だけなので、曜日傾向側で「休みとは限りません」と断るほうが正しい。
  function forecastOutlook(insights, key, shift, lastActualDate) {
    if (lastActualDate && key < lastActualDate) {
      return null;
    }
    const previousDate = addDays(key, -1);
    const previous = openStoresOn(insights, previousDate, shift);
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
    const parts = [`前日（${previousDate}）の${shift}の実績から見た、翌日の${shift}の見込みです。`];

    if (leader) {
      parts.push(`2・3号店では${leader.store.short}が${toPercent(leader.rate)}で最有力です。`);
    }
    if (typeof shiftAccuracy.group === "number") {
      parts.push(
        `ただし${shift}まで分けた2・3号店の的中は実測${toPercent(shiftAccuracy.group)}で、当てずっぽうと同じくらいです。`
      );
    }
    if (typeof dayAccuracy.group === "number") {
      // 日単位のローテーション表は、日単位の問いに対してはシフト別より当たる（51.4% 対 38〜43%）。
      // チップの数値はシフト別のままにして、ここでは「その日どちらが開くか」だけを補足する。
      const previousDay = openStoresOnDay(insights, previousDate);
      const dayNext = previousDay
        ? insights.rotation?.nextDayByDay?.[groupStateOf(previousDay)] ?? {}
        : {};
      const dayRates = {
        s2: (dayNext.s2 ?? 0) + (dayNext.both ?? 0),
        s3: (dayNext.s3 ?? 0) + (dayNext.both ?? 0)
      };
      const dayLeader = dayRates.s2 >= dayRates.s3 ? "s2" : "s3";
      const dayLead = dayRates[dayLeader] > 0
        ? `昼夜をまとめると${storeShort(insights, dayLeader)}が${toPercent(dayRates[dayLeader])}で、この見方の的中は`
        : "昼夜をまとめて「どちらが開くか」なら的中は";
      parts.push(
        `${dayLead}実測${toPercent(dayAccuracy.group)}` +
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
      badge: "翌日見込み",
      badgeClass: "is-forecast",
      previousStores: [...previous],
      summary: parts.join(""),
      entries
    };
  }

  // その日の記録が片方だけのとき、欠けている側は「取りこぼした」のか
  // 「まだ来ていない」のか。記録の最終日で、記録済みより後のシフトを訊かれて
  // いるなら後者で、「記録がありません」と書くと誤解させる。
  function isShiftStillToCome(insights, key, shift, lastActualDate) {
    if (!lastActualDate || key !== lastActualDate) {
      return false;
    }
    const shifts = insights?.shifts ?? [];
    const asked = shifts.indexOf(shift);
    const latestRecorded = shifts.reduce(
      (latest, candidate, index) =>
        insights.actual?.[key]?.[candidate] ? index : latest,
      -1
    );
    return asked > latestRecorded && latestRecorded >= 0;
  }

  // 曜日傾向そのままのときだけ言える一文。顔ぶれで配分を寄せたあとは、
  // 表示している数字がもう「曜日の営業率」ではないので、この文は外す。
  const RAW_WEEKDAY_CLAIM = "予測ではありません。";

  function tendencyOutlook(insights, key, shift, lastActualDate) {
    const bucket = weekdayBucket(insights, key);
    const rates = insights.weekdayOpenRate?.[shift]?.[bucket];
    if (!rates) {
      return null;
    }
    const hasPartialRecord =
      Boolean(insights.actual?.[key]) && !isShiftStillToCome(insights, key, shift, lastActualDate);
    const isPast = Boolean(lastActualDate) && key <= lastActualDate;
    const weekdayName = WEEKDAY_LABELS[weekdayIndex(key)];
    // 前日の実績があれば forecastOutlook が拾っているので、ここに来た時点で
    // 前日は分かっていない。曜日の平均しか無いことを言っておく。
    const noYesterday = !openStoresOn(insights, addDays(key, -1), shift)
      ? "前日の実績が手元にないので、"
      : "";
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
        `${lead}${noYesterday}${weekdayName}曜日の${shift}の、過去1年の営業率です。` +
        `${RAW_WEEKDAY_CLAIM}2日以上先は当てになりません。`,
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

  // 同じ日のもう片方のシフトに実績があるとき、そこから見る。
  // 昼に2号店が開いていれば夜も2号店、という結びつきが強く、
  // 3号店に入れ替わることはほぼ無い（rotation.sameDay.s2.s3 は 0 件）。
  //
  // 2・3号店だけに使い、1・4号店は曜日傾向のままにしている。実測で、
  // 全店に使うと4号店の F1 が 51.2% -> 46.3% と落ちるが、2・3号店に限れば
  // 46.7% とほぼ保ったまま、2号店が 7.8% -> 47.4% に上がる。
  // 夜の店舗の組み合わせを丸ごと当てる的中は 23.8% -> 30.4%（n=349, p=0.015）。
  const SAME_DAY_STORES = ["s2", "s3"];

  function sameDayOutlook(insights, key, shift, lastActualDate) {
    const other = otherShiftOf(insights, shift);
    if (!other) {
      return null;
    }
    // この表は「昼の実績 → 夜の見込み」の向きに作られている。逆に当てると
    // 別の分布になる。実測（記録176日）では夜が2号店だった日の昼は2号店が58%
    // だが、この表を流用すると40%と出る。夜だけ記録のある日は稀なので、
    // 向きが合わないときは何もせず曜日傾向に落とす。
    const [fromShift, toShift] = insights.shifts ?? [];
    if (shift !== toShift || other !== fromShift) {
      return null;
    }
    const known = openStoresOn(insights, key, other);
    const bucket = weekdayBucket(insights, key);
    const weekday = insights.weekdayOpenRate?.[shift]?.[bucket];
    if (!known || !weekday) {
      return null;
    }
    const transitions = insights.rotation?.sameDay?.[groupStateOf(known)];
    if (!transitions) {
      return null;
    }
    const rates = {
      s2: (transitions.s2 ?? 0) + (transitions.both ?? 0),
      s3: (transitions.s3 ?? 0) + (transitions.both ?? 0)
    };
    const entries = storesOf(insights).map((store) => {
      const rate = SAME_DAY_STORES.includes(store.id)
        ? rates[store.id] ?? 0
        : weekday[store.id] ?? 0;
      return {
        store,
        state: stateForRate(rate),
        rate,
        text: toPercent(rate),
        srText: `${store.short}が${shift}に営業する見込みは${toPercent(rate)}`
      };
    });
    const rivals = entries.filter((entry) => SAME_DAY_STORES.includes(entry.store.id));
    const leader = rivals.reduce((best, entry) => (entry.rate > best.rate ? entry : best));
    // 過ぎた日にこれが出るのは、その日のもう片方の記録だけが欠けているとき。
    // 見込みを実績と読まれると「休みだった」ことになってしまうので、先に断る。
    // 記録の最終日で欠けている側は、記録し損ねたのではなくまだ来ていないので除く。
    const isPast =
      Boolean(lastActualDate) &&
      key <= lastActualDate &&
      !isShiftStillToCome(insights, key, shift, lastActualDate);
    const lead = isPast
      ? `この日の${shift}の記録だけが手元にありません（休みとは限りません）。`
      : "";
    return {
      basis: "sameDay",
      badge: "同日の実績",
      badgeClass: "is-forecast",
      knownShift: other,
      knownStores: [...known],
      summary:
        `${lead}同じ日の${other}に${joinStoreNames(insights, known)}が営業していた実績から見た、` +
        `${shift}の見込みです。2・3号店は同じ日のうちに入れ替わることがほとんど無いため、` +
        `${leader.store.short}が${toPercent(leader.rate)}で最有力です。` +
        `1号店と4号店は${WEEKDAY_LABELS[weekdayIndex(key)]}曜日の営業率です。`,
      entries
    };
  }

  // 2番手の店は、予定表の顔ぶれから読めます。その店を本拠とする人が何人載っているか
  // で、2号店は27%→54%、3号店は23%→48%と動きます。実測で2番手を当てる的中が
  // 34.4% → 42.2%（372シフト、87勝58敗、p=0.020）。
  //
  // 4号店は動きません（配属者0人で37%、4人で30%）。当て方の問題ではなく読めないので、
  // 動かないことをそのまま出します。
  const SECOND_STORE_MIN_SAMPLE = 20;

  // キッチンにゃんこは数から外す。公式サイトには配属が載っているが、実測では
  // その店に入る率がフロアより 13.3 ポイント低く（62.6% 対 75.9%、p=0.025）、
  // どこにでも入っている。data/store-insights.js の表も外して作ってあるので、
  // ここで数え方を合わせないと引く場所がずれる。
  function homeStaffCounts(members, homeStore, kitchenStaff) {
    const kitchen = kitchenStaff instanceof Set
      ? kitchenStaff
      : new Set(kitchenStaff ?? []);
    const counts = new Map();
    for (const name of members ?? []) {
      if (kitchen.has(name)) {
        continue;
      }
      const home = Object.hasOwn(homeStore ?? {}, name) ? homeStore[name] : null;
      if (home) {
        counts.set(home, (counts.get(home) ?? 0) + 1);
      }
    }
    return counts;
  }

  // 表は「配属者が n 人」で引く。上限より多い人数は上限のバケットに寄せる。
  function secondStoreRate(insights, storeId, count) {
    const table = insights?.secondStoreByHome?.[storeId];
    if (!table) {
      return null;
    }
    const buckets = Object.keys(table)
      .map(Number)
      .filter((value) => Number.isFinite(value))
      .sort((a, b) => a - b);
    if (buckets.length === 0) {
      return null;
    }
    const capped = Math.min(Math.max(count, buckets[0]), buckets[buckets.length - 1]);
    // 実際に無いバケット（3号店の0人など）は、いちばん近い下の段に寄せる。
    const bucket = [...buckets].reverse().find((value) => value <= capped) ?? buckets[0];
    const entry = table[String(bucket)];
    if (!entry || !(entry.n >= SECOND_STORE_MIN_SAMPLE) || typeof entry.rate !== "number") {
      return null;
    }
    return entry.rate;
  }

  // 見込みの日だけ、1号店以外の3店を顔ぶれから引き直す。実績の日には触らない。
  //
  // 置き換えではなく、その店の平均からのずれ（尤度比）で元の見込みを更新する。
  // 置き換えると、同日ルールが出した「昼が2号店なら夜の3号店は1%」のような
  // 強い手がかりまで捨ててしまう。この形なら、表が平均どおりの店は動かない
  // ので、4号店が読めないことも特別扱いせずそのまま出る。
  function applyHomeStaff(insights, outlook, members, homeStore, kitchenStaff) {
    if (!outlook || outlook.basis === "actual" || !insights?.secondStoreByHome) {
      return outlook;
    }
    const counts = homeStaffCounts(members, homeStore, kitchenStaff);
    const ids = Object.keys(insights.secondStoreByHome);
    const shifted = new Map();
    for (const id of ids) {
      const rate = secondStoreRate(insights, id, counts.get(id) ?? 0);
      const average = averageSecondStoreRate(insights, id);
      // 引けない店は「動かさない」（係数1）。ここで全体を諦めると、
      // 標本の薄いバケットを1つ引いただけで読み取りが丸ごと黙って止まる。
      shifted.set(id, rate === null || !(average > 0) ? 1 : rate / average);
    }
    if ([...shifted.values()].every((factor) => factor === 1)) {
      return outlook;
    }
    const certain = new Set(outlook.certainStores ?? []);
    const movable = outlook.entries.filter(
      (entry) => shifted.has(entry.store.id) && !certain.has(entry.store.id)
    );
    const before = movable.reduce((sum, entry) => sum + (entry.rate ?? 0), 0);
    const weighted = movable.reduce(
      (sum, entry) => sum + (entry.rate ?? 0) * shifted.get(entry.store.id),
      0
    );
    if (!(before > 0) || !(weighted > 0)) {
      return outlook;
    }
    // 開く店の数の見込みは変えず、3店のあいだの配分だけ動かす。
    const scale = before / weighted;
    const entries = outlook.entries.map((entry) => {
      if (!shifted.has(entry.store.id) || certain.has(entry.store.id)) {
        return entry;
      }
      const rate = Math.min(1, (entry.rate ?? 0) * shifted.get(entry.store.id) * scale);
      return {
        ...entry,
        state: stateForRate(rate),
        rate,
        text: toPercent(rate),
        // 読み上げ文の言い回しは元の見込みのものを保ち、割合だけ差し替える。
        srText: (entry.srText ?? "").includes(entry.text)
          ? entry.srText.replace(entry.text, toPercent(rate))
          : `${entry.store.short}が営業する見込みは${toPercent(rate)}`
      };
    });
    const named = ids
      .map((id) => `${storeShort(insights, id)}に${counts.get(id) ?? 0}人`)
      .join("・");
    // 読み手が画面の顔ぶれを数えると合わないので、いる日だけ理由を断る。
    // キッチンにゃんこは配属店に入る率がフロアより13.3pt低く、どこにでも入る。
    const kitchen = kitchenStaff instanceof Set ? kitchenStaff : new Set(kitchenStaff ?? []);
    const cooksHere = (members ?? []).filter((name) => kitchen.has(name)).length;
    const cookNote = cooksHere > 0
      ? `キッチンにゃんこ${cooksHere}人は、配属と実際が合わないためこの数に入れていません。`
      : "";
    // 振れ幅は表から出す。直書きすると集計をやり直したときに古くなるし、
    // 古くなったことに誰も気づかない（「在籍35名」と同じ轍）。
    const spreads = ids
      .map((id) => ({ id, spread: secondStoreSpread(insights, id) }))
      .filter((row) => row.spread);
    const widest = spreads.filter((row) => row.spread.width >= 0.15);
    const flattest = spreads.length
      ? spreads.reduce((a, b) => (a.spread.width <= b.spread.width ? a : b))
      : null;
    const movesText = widest
      .map(
        (row) =>
          `${storeShort(insights, row.id)}は${toPercent(row.spread.low)}〜` +
          `${toPercent(row.spread.high)}`
      )
      .join("、");
    const flatText =
      flattest && flattest.spread.width < 0.15
        ? `${storeShort(insights, flattest.id)}は配属者が何人でも` +
          `${toPercent(flattest.spread.low)}〜${toPercent(flattest.spread.high)}に収まり、` +
          `この見方では読めません。`
        : "";
    return {
      ...outlook,
      basis: `${outlook.basis}+home`,
      entries,
      // 曜日傾向を土台にした場合、表示している数字はもう「曜日の営業率そのもの」
      // ではないので、そう言い切っている一文を外す。残すと本文の中で矛盾する。
      summary:
        `${outlook.summary.replace(RAW_WEEKDAY_CLAIM, "")}` +
        `この予定表の配属は${named}で、そのぶん配分を寄せています。` +
        cookNote +
        (movesText ? `配属者の人数で${movesText}と動きます。` : "") +
        flatText
    };
  }

  // その店が2番手になる率の、いちばん低いバケットと高いバケット。
  function secondStoreSpread(insights, storeId) {
    const table = insights?.secondStoreByHome?.[storeId];
    if (!table) {
      return null;
    }
    const rates = Object.values(table)
      .filter((entry) => typeof entry?.rate === "number" && entry.n >= SECOND_STORE_MIN_SAMPLE)
      .map((entry) => entry.rate);
    if (rates.length < 2) {
      return null;
    }
    const low = Math.min(...rates);
    const high = Math.max(...rates);
    return { low, high, width: high - low };
  }

  // その店が2番手になる平均の率。バケットごとの件数で重み付けする。
  function averageSecondStoreRate(insights, storeId) {
    const table = insights?.secondStoreByHome?.[storeId];
    if (!table) {
      return null;
    }
    let weight = 0;
    let total = 0;
    for (const entry of Object.values(table)) {
      if (typeof entry?.rate === "number" && entry.n > 0) {
        weight += entry.n;
        total += entry.rate * entry.n;
      }
    }
    return weight > 0 ? total / weight : null;
  }

  function otherShiftOf(insights, shift) {
    const shifts = insights?.shifts ?? [];
    return shifts.find((candidate) => candidate !== shift) ?? null;
  }

  // 実績 → 翌日見込み → 同日の実績 → 曜日傾向 の順に、確かなものから採用する。
  // 前日の同じシフト（2つ前）と、同じ日のもう片方（直前）は別のことを言う。
  //
  // 2つ前は同じシフトどうしの比較で、日をまたぐ入れ替わりを見ている。直前は
  // 同じ日の中の結びつきで、昼に2号店なら夜に3号店へ移ることはほぼ無い
  // （rotation.sameDay.s2.s3 は 0 件）という鋭い規則を持っている。
  //
  // Combine log odds relative to the base rate. Historical comparisons and their
  // time-separated counterparts are documented in README; fixed shipped tables
  // must not be scored as if they were trained before each evaluation date.
  function applySameDayEvidence(insights, outlook, key, shift) {
    if (outlook?.basis !== "forecast") {
      return outlook;
    }
    const [fromShift, toShift] = insights?.shifts ?? [];
    if (shift !== toShift) {
      return outlook;
    }
    const known = openStoresOn(insights, key, fromShift);
    const base = insights?.baseOpenRate?.[shift];
    if (!known || !base) {
      return outlook;
    }
    const transitions = insights.rotation?.sameDay?.[groupStateOf(known)];
    if (!transitions) {
      return outlook;
    }
    const sameDay = {
      s2: (transitions.s2 ?? 0) + (transitions.both ?? 0),
      s3: (transitions.s3 ?? 0) + (transitions.both ?? 0)
    };
    const odds = (value) => {
      const p = Math.min(Math.max(value, 0.005), 0.995);
      return Math.log(p / (1 - p));
    };
    const entries = outlook.entries.map((entry) => {
      const evidence = SAME_DAY_STORES.includes(entry.store.id) ? sameDay[entry.store.id] : null;
      if (evidence == null || typeof entry.rate !== "number" || !(base[entry.store.id] > 0)) {
        return entry;
      }
      const shifted = odds(entry.rate) + odds(evidence) - odds(base[entry.store.id]);
      const rate = 1 / (1 + Math.exp(-shifted));
      return {
        ...entry,
        state: stateForRate(rate),
        rate,
        text: toPercent(rate),
        srText: (entry.srText ?? "").includes(entry.text)
          ? entry.srText.replace(entry.text, toPercent(rate))
          : `${entry.store.short}が営業する見込みは${toPercent(rate)}`
      };
    });
    return {
      ...outlook,
      entries,
      sameDayShift: fromShift,
      summary:
        `${outlook.summary}この日の${fromShift}の実績（${joinStoreNames(insights, known) || "2・3号店とも休み"}）も` +
        `踏まえています。同じ日のうちに2号店と3号店が入れ替わることはほぼありません。`
    };
  }

  function getStoreOutlook({ insights, dateKey: key, shift, lastActualDate }) {
    if (!insights || storesOf(insights).length === 0) {
      return null;
    }
    const last = lastActualDate === undefined ? lastActualDateOf(insights) : lastActualDate;
    return (
      actualOutlook(insights, key, shift) ??
      applySameDayEvidence(insights, forecastOutlook(insights, key, shift, last), key, shift) ??
      sameDayOutlook(insights, key, shift, last) ??
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

  // 所属店の出どころ。公式サイトに載っている人はサイトの配属、まだ載っていない人
  // （unpostedMaids）はお店からの案内による。どちらも `homeStore` に入っているので、
  // ここで分けないとツールチップが「公式サイトの配属です」と嘘をつく。
  function homeStoreSourceOf(homeStore, unpostedMaids, name) {
    if (!Object.hasOwn(homeStore ?? {}, name) || !homeStore[name]) {
      return null;
    }
    const unposted = unpostedMaids instanceof Set ? unpostedMaids : new Set(unpostedMaids ?? []);
    return unposted.has(name) ? "shop" : "site";
  }

  const HOME_STORE_SOURCE_LABEL = {
    site: "公式サイトの配属",
    shop: "お店の案内による所属",
    registered: "登録時の申告"
  };

  // 生誕祭・周年・卒業の主役は、その日かならず自分の所属店に立つ。
  // 所属店は homeStore を優先し、載っていない人だけ
  // 「その店が開いている日にいちばん入っている店」からの推定で補う。
  function eventStorePins({ insights, entries, homeStore, unpostedMaids }) {
    const pins = new Map();
    for (const entry of entries ?? []) {
      if (!entry?.featured) {
        continue;
      }
      const tendency = maidTendencyFor(insights, entry.name);
      const declared = Object.hasOwn(homeStore ?? {}, entry.name) ? homeStore[entry.name] : null;
      const home = insights?.memberRegistry ? declared : declared ?? tendency?.home;
      if (!home) {
        continue;
      }
      pins.set(entry.name, {
        storeId: home,
        label: entry.eventLabel ?? "記念日",
        source: insights?.memberRegistry
          ? { official: "site", "legacy-reviewed": "shop", "user-provided": "registered" }[insights.memberRegistry.member(entry.name)?.homeStoreSource]
          : homeStoreSourceOf(homeStore, unpostedMaids, entry.name) ?? "record",
        pickRate: tendency?.pickRate?.[home] ?? null
      });
    }
    return pins;
  }

  // 主役がいる店は開いていることが確定するので、見込みを実績側に寄せる。
  function applyEventCertainty(insights, outlook, pins) {
    if (!outlook || !pins || pins.size === 0 || outlook.basis === "actual") {
      return outlook;
    }
    const hosts = new Map();
    for (const [name, pin] of pins) {
      if (!hosts.has(pin.storeId)) {
        hosts.set(pin.storeId, []);
      }
      hosts.get(pin.storeId).push({ name, label: pin.label });
    }

    const reason = [...hosts.entries()]
      .map(([storeId, people]) =>
        `${storeShort(insights, storeId)}は${people.map((p) => `${p.name}さんの${p.label}`).join("・")}` +
        "があるため営業します"
      )
      .join("。");

    return {
      ...outlook,
      certainStores: [...hosts.keys()],
      summary: `${reason}。${outlook.summary}`,
      entries: outlook.entries.map((entry) =>
        hosts.has(entry.store.id)
          ? {
            ...entry,
            state: "open",
            rate: 1,
            text: "営業",
            srText: `${entry.store.short}は営業（記念日の主役がいるため確定）`
          }
          : entry
      )
    };
  }
  // その日そのシフトに出る顔ぶれを、開いていそうな店舗へ振り分ける。
  // 1人ずつ独立に「いそうな店」を出すと、営業率の高い1号店に全員が寄ってしまうため、
  // 店ごとの標準人数を定員として奪い合わせる。
  function storeCapacities(insights, shift, storeIds, poolSize) {
    const headcount = insights.typicalHeadcount?.[shift] ?? {};
    const weights = storeIds.map((id) => Math.max(headcount[id] ?? 1, 0.1));
    const total = weights.reduce((sum, value) => sum + value, 0);
    const exact = weights.map((weight) => (weight / total) * poolSize);
    const capacity = exact.map((value) => Math.floor(value));
    let remaining = poolSize - capacity.reduce((sum, value) => sum + value, 0);

    // 端数は取りこぼしの大きい店から配る。同点は店舗の並び順で決める。
    const order = exact
      .map((value, index) => [index, value - Math.floor(value)])
      .sort((a, b) => b[1] - a[1] || a[0] - b[0]);
    for (let step = 0; remaining > 0; step += 1, remaining -= 1) {
      capacity[order[step % order.length][0]] += 1;
    }
    return Object.fromEntries(storeIds.map((id, index) => [id, capacity[index]]));
  }

  // 同じ日の早いシフトの記録から、遅いシフトの行き先を読み直す。
  //
  // The shipped move table uses a 365-day window. It is multiplied without
  // smoothing, with a tendency fallback when all products are zero.
  //
  // 人ごとの表があればそれを使う。無ければ全体の表。人ごとは n が小さい組み
  // 合わせがあるので（あむさんの昼s4は6回）、n も返して読み手側で判断できる形。
  function sameDayMoveOdds(insights, name, fromStoreId) {
    const perMaid = maidTendencyFor(insights, name)?.sameDayMove?.[fromStoreId];
    if (perMaid?.to && perMaid.n > 0) {
      return { to: perMaid.to, n: perMaid.n, source: "maid" };
    }
    const overall = insights?.sameDayMaidMove?.[fromStoreId];
    if (overall?.to && overall.n > 0) {
      return { to: overall.to, n: overall.n, source: "all" };
    }
    return null;
  }

  // この移り先の表は「早いシフト → 遅いシフト」の向きに作ってある。
  // 逆に当てると別の分布になるので、遅いシフトを組むときにしか使わない。
  function isLaterShift(insights, shift) {
    const shifts = insights?.shifts;
    return Array.isArray(shifts) && shifts.indexOf(shift) === shifts.length - 1;
  }

  function affinityFor(insights, name, shift, storeIds, movedFrom) {
    const tendency = maidTendencyFor(insights, name);
    const uniform = 1 / storeIds.length;
    if (!tendency) {
      return { scores: Object.fromEntries(storeIds.map((id) => [id, uniform])), known: false };
    }
    const { pickRate } = tendencyTables(tendency, shift);
    // 昼にどこにいたか分かっているなら、その人の傾向に移り先の確率を掛ける。
    // 掛けるのは、傾向を捨てずに向きだけ足したいから。置き換えると
    // 「夜はみんな1号店」になって、その人の癖が消える。
    const move = movedFrom ? sameDayMoveOdds(insights, name, movedFrom)?.to : null;
    const raw = storeIds.map((id) => {
      const base = Math.max(pickRate[id] ?? 0, 0);
      return move ? base * (move[id] ?? 0) : base;
    });
    const total = raw.reduce((sum, value) => sum + value, 0);
    if (total <= 0) {
      // 掛けた結果が全滅することがある（その店に移った記録が1件も無い場合）。
      // そのときは傾向だけに戻す。移り先が読めないことと、行かないことは違う。
      return move
        ? affinityFor(insights, name, shift, storeIds, null)
        : { scores: Object.fromEntries(storeIds.map((id) => [id, uniform])), known: false };
    }
    return {
      scores: Object.fromEntries(storeIds.map((id, index) => [id, raw[index] / total])),
      known: true
    };
  }

  // キッチンにゃんこは料理担当なので、実測でも各店1人ずつが基本（1店1人が77.5%、平均1.05人）。
  // 禁止ではなく減点にする。実際に13.7%は2人一緒に入っている。
  // スコアは候補店で合計1に正規化した値なので、減点もその尺度で置く。
  const KITCHEN_SPREAD_PENALTY = 0.5;

  function assignShiftStores({ insights, members, shift, storeIds, pins, kitchenStaff, movedFrom }) {
    if (insights?.memberRegistry && Array.isArray(members)) {
      members = members.filter((name) => maidTendencyFor(insights, name) || pins?.has(name));
    }
    if (!insights || !Array.isArray(members) || members.length === 0 || storeIds.length === 0) {
      return null;
    }
    const kitchen = kitchenStaff instanceof Set ? kitchenStaff : new Set(kitchenStaff ?? []);
    const pinned = new Map(
      [...(pins ?? new Map())].filter(
        ([name, pin]) => members.includes(name) && storeIds.includes(pin.storeId)
      )
    );

    if (storeIds.length === 1) {
      const only = storeIds[0];
      return {
        storeIds,
        capacity: { [only]: members.length },
        byMaid: new Map(
          members.map((name) => [
            name,
            {
              storeId: only,
              score: 1,
              runnerUpId: null,
              runnerUpScore: 0,
              known: false,
              full: false,
              pin: pinned.get(name) ?? null
            }
          ])
        )
      };
    }

    const capacity = storeCapacities(insights, shift, storeIds, members.length);
    const remaining = { ...capacity };
    const kitchenPlaced = Object.fromEntries(storeIds.map((id) => [id, 0]));
    const byMaid = new Map();

    // 主役は動かせないので先に席を取る。定員を超えるなら定員のほうを広げる。
    for (const [name, pin] of pinned) {
      remaining[pin.storeId] = (remaining[pin.storeId] ?? 0) - 1;
      capacity[pin.storeId] = Math.max(capacity[pin.storeId] ?? 0, 1);
      if (kitchen.has(name)) {
        kitchenPlaced[pin.storeId] += 1;
      }
      byMaid.set(name, {
        storeId: pin.storeId,
        score: 1,
        runnerUpId: null,
        runnerUpScore: 0,
        known: true,
        full: false,
        pin
      });
    }

    const ranked = members
      .map((name, index) => ({ name, index }))
      .filter(({ name }) => !pinned.has(name))
      .map(({ name, index }) => {
        const cameFrom = movedFrom?.get?.(name) ?? movedFrom?.[name] ?? null;
        const { scores, known } = affinityFor(insights, name, shift, storeIds, cameFrom);
        const order = [...storeIds].sort(
          (a, b) => scores[b] - scores[a] || storeIds.indexOf(a) - storeIds.indexOf(b)
        );
        return {
          name,
          index,
          scores,
          known,
          isKitchen: kitchen.has(name),
          // 迷いの少ない人から先に決める。後回しにすると定員が埋まって押し出される。
          regret: scores[order[0]] - scores[order[1]]
        };
      });

    ranked.sort(
      (a, b) =>
        // キッチンにゃんこは各店1人ずつという制約が強いので先に席を決める。
        // 後回しにすると定員が埋まって、同じ店に押し込まれてしまう。
        Number(b.isKitchen) - Number(a.isKitchen) ||
        b.regret - a.regret ||
        a.index - b.index
    );

    for (const member of ranked) {
      const effective = (id) =>
        member.scores[id] -
        (member.isKitchen ? KITCHEN_SPREAD_PENALTY * (kitchenPlaced[id] ?? 0) : 0);
      const order = [...storeIds].sort(
        (a, b) => effective(b) - effective(a) || storeIds.indexOf(a) - storeIds.indexOf(b)
      );
      const target = order.find((id) => remaining[id] > 0) ?? order[0];
      remaining[target] = Math.max((remaining[target] ?? 0) - 1, 0);
      if (member.isKitchen) {
        kitchenPlaced[target] += 1;
      }
      const runnerUp = order.find((id) => id !== target) ?? null;
      byMaid.set(member.name, {
        storeId: target,
        score: member.scores[target],
        runnerUpId: runnerUp,
        runnerUpScore: runnerUp ? member.scores[runnerUp] : 0,
        known: member.known,
        // 本人の一番人気ではなく、定員の都合で押し出された場合。
        full: target !== order[0]
      , pin: null });
    }

    // 実際に配った人数を定員として持ち直す（主役ぶんで増えていることがある）。
    const placed = Object.fromEntries(storeIds.map((id) => [id, 0]));
    for (const entry of byMaid.values()) {
      placed[entry.storeId] += 1;
    }
    return { storeIds, capacity: placed, byMaid };
  }

  // その日出る人数から、いくつの店が開くかを決める。
  // 閾値は実測の多数決（openCountByHeadcount）。typicalHeadcount の累積だと
  // 1号店が6.1人あるため6人が1店になってしまうが、実測では6人は2店が多数派
  // （昼61% / 夜73%）。表を使うと的中は昼88.9% / 夜93.2%（累積は84.4% / 91.8%）。
  function capacityCountFor(insights, shift, orderedIds, poolSize) {
    if (!(poolSize > 0)) {
      return 1;
    }
    const thresholds = insights.openCountByHeadcount?.[shift];
    if (Array.isArray(thresholds) && thresholds.length > 0) {
      const index = thresholds.findIndex((limit) => poolSize <= limit);
      return Math.min(index === -1 ? thresholds.length + 1 : index + 1, orderedIds.length);
    }
    // 表が無いデータでも動くよう、標準人数の累積で代用する。
    const headcount = insights.typicalHeadcount?.[shift] ?? {};
    let seats = 0;
    for (let index = 0; index < orderedIds.length; index += 1) {
      seats += headcount[orderedIds[index]] ?? 0;
      if (seats >= poolSize) {
        return index + 1;
      }
    }
    return orderedIds.length;
  }

  // その日の顔ぶれに、どの店の配属者が多いか。多い店ほど開く見込みが上がる。
  // 「この人たちが出るならこの店が開く」という見方を、公式サイトの配属だけで表す。
  // 実測で、店舗の組み合わせを丸ごと当てる的中が 42.9% -> 47.7%（n=709, p<0.05）。
  // 人数ではなく割合で見るので、予定表の提出が揃っていなくても比が変わらない。
  const POSTED_WEIGHT = 0.6;

  function postedTilt(insights, shift, members) {
    const baseline = insights?.homeStaffShare?.[shift];
    if (!baseline || !Array.isArray(members) || members.length === 0) {
      return null;
    }
    const counts = new Map();
    let known = 0;
    for (const name of members) {
      const posted = maidTendencyFor(insights, name)?.posted;
      if (!posted) {
        continue;
      }
      counts.set(posted, (counts.get(posted) ?? 0) + 1);
      known += 1;
    }
    if (known === 0) {
      return null;
    }
    const tilt = new Map();
    for (const store of storesOf(insights)) {
      const share = (counts.get(store.id) ?? 0) / known;
      const expected = baseline[store.id] ?? 0;
      // 0 を避けつつ、いない店は下げ、多い店は上げる。
      tilt.set(store.id, POSTED_WEIGHT * Math.log(Math.max(share, 0.02) / Math.max(expected, 0.02)));
    }
    return tilt;
  }

  // 予定表の顔ぶれを画面の数字にも反映させる。
  //
  // ここを分けていたのが誤りだった。以前は expectedOpenStores が順位づけのときだけ
  // この傾きを足していて、画面に出る割合には入っていなかった。結果として
  // 「48%と出した店が空で、39%と出した店に4人いる」が起きる。読み手には理由がない。
  //
  // どちらが正しいかは実測できる（過去1年521シフト）。顔ぶれを入れた順位のほうが
  // 実際に開いた店をよく当てる（1.36店 対 1.32店、食い違った104件で45勝25敗
  // p=0.023）。つまり直すべきは順位づけではなく、古いままだった表示のほうだった。
  //
  // 対数オッズで足すのは、順位づけがそうしていたから。ここを別の式にすると
  // 「表示は動いたが選ぶ店は変わらない」という、いま直している状態に戻る。
  function applyPostedTilt(insights, outlook, shift, members) {
    const tilt = postedTilt(insights, shift, members);
    if (!outlook || !tilt || outlook.basis === "actual") {
      return outlook;
    }
    const certain = new Set(outlook.certainStores ?? []);
    const entries = outlook.entries.map((entry) => {
      const move = tilt.get(entry.store.id) ?? 0;
      if (certain.has(entry.store.id) || move === 0 || typeof entry.rate !== "number") {
        return entry;
      }
      const clamped = Math.min(Math.max(entry.rate, 0.005), 0.995);
      const odds = Math.log(clamped / (1 - clamped)) + move;
      const rate = 1 / (1 + Math.exp(-odds));
      return {
        ...entry,
        state: stateForRate(rate),
        rate,
        text: toPercent(rate),
        // 読み上げ文の言い回しは元の見込みのものを保ち、割合だけ差し替える。
        srText: (entry.srText ?? "").includes(entry.text)
          ? entry.srText.replace(entry.text, toPercent(rate))
          : `${entry.store.short}が営業する見込みは${toPercent(rate)}`
      };
    });
    return { ...outlook, entries };
  }

  // 開いている店が分からない日は、その日出る人数から店舗数を決める。
  // 第4引数は人数でも顔ぶれの配列でもよい（数えるのは人数だけ）。
  //
  // 順位は画面に出している割合そのもので付ける。別の物差しで選ぶと、読み手には
  // 「低いほうが選ばれた」としか見えない。顔ぶれの効果は applyPostedTilt が
  // 割合そのものに入れてあるので、ここで二重に足してはいけない。
  function expectedOpenStores(insights, shift, outlook, pool) {
    if (!outlook) {
      return [];
    }
    if (outlook.basis === "actual") {
      return outlook.openStores ?? [];
    }
    const poolSize = Array.isArray(pool) ? pool.length : pool ?? 0;
    const certain = outlook.certainStores ?? [];
    const ordered = [
      ...certain,
      ...[...outlook.entries]
        .sort((a, b) => (b.rate ?? 0) - (a.rate ?? 0))
        .map((entry) => entry.store.id)
        .filter((id) => !certain.includes(id))
    ];
    const target = Math.min(
      ordered.length,
      Math.max(1, certain.length, capacityCountFor(insights, shift, ordered, poolSize))
    );
    const chosen = ordered.slice(0, target);
    // 店舗の並び順に戻して、割り振り結果が安定するようにする。
    return storesOf(insights)
      .map((store) => store.id)
      .filter((id) => chosen.includes(id));
  }

  // 上位k で切る形そのものは妥当（確率で切ると、どの閾値でも現行より悪くなる）。
  // ただし境目が僅差のときは、選んだ側に根拠がない。実測でも、最後に選んだ店と
  // 最初に落とした店の差が10ポイント未満のときは、落とした店のほうがよく開いて
  // いる（42% 対 38%、519シフト）。顔ぶれは片方にしか出せないので、もう一方も
  // 同じくらいあり得ることを書いておく。
  //
  // 比べるのは表示している確率どうし。読み手が「52%と42%なのになぜ」と思う
  // ところを説明するのが目的なので、内部の順位付けではなく画面の数字で見る。
  const NEAR_MISS_MARGIN = 0.05;

  function nearMissStores(insights, shift, outlook, pool) {
    if (!outlook || outlook.basis === "actual") {
      return [];
    }
    const chosen = new Set(expectedOpenStores(insights, shift, outlook, pool));
    if (chosen.size === 0 || chosen.size === outlook.entries.length) {
      return [];
    }
    const rateOf = (id) => outlook.entries.find((entry) => entry.store.id === id)?.rate ?? 0;
    const lowestChosen = Math.min(...[...chosen].map(rateOf));
    return outlook.entries
      .filter((entry) => !chosen.has(entry.store.id))
      .filter((entry) => (entry.rate ?? 0) + NEAR_MISS_MARGIN >= lowestChosen)
      .sort((a, b) => (b.rate ?? 0) - (a.rate ?? 0))
      .map((entry) => entry.store.id);
  }

  // 同じシフトに2店が揃った割合。記録から数える（180日ぶんなので軽い）。
  // 「52%と42%」を足して94%と読まれないよう、実際には揃わないことを示すのに使う。
  // データごとに覚える。組だけで覚えると、別のデータで呼んだときに前の答えを返す。
  const coOpenMemo = new WeakMap();

  function coOpenRate(insights, a, b) {
    if (!insights || typeof insights !== "object") {
      return null;
    }
    if (!coOpenMemo.has(insights)) {
      coOpenMemo.set(insights, new Map());
    }
    const perData = coOpenMemo.get(insights);
    const key = [a, b].sort().join("|");
    if (perData.has(key)) {
      return perData.get(key);
    }
    let shifts = 0;
    let together = 0;
    for (const day of Object.values(insights.actual ?? {})) {
      for (const open of Object.values(day)) {
        if (!Array.isArray(open)) {
          continue;
        }
        shifts += 1;
        if (open.includes(a) && open.includes(b)) {
          together += 1;
        }
      }
    }
    const result = shifts > 0 ? { rate: together / shifts, shifts } : null;
    perData.set(key, result);
    return result;
  }

  // 僅差で落とした店について、読み手に何が起きているかを説明する一文。
  function nearMissNote(insights, shift, outlook, pool) {
    const missed = nearMissStores(insights, shift, outlook, pool);
    if (missed.length === 0) {
      return null;
    }
    const chosen = expectedOpenStores(insights, shift, outlook, pool);
    const rateOf = (id) => outlook.entries.find((entry) => entry.store.id === id)?.rate ?? 0;
    const rival = [...chosen].sort((a, b) => rateOf(a) - rateOf(b))[0];
    const names = missed
      .map((id) => `${storeShort(insights, id)}（${toPercent(rateOf(id))}）`)
      .join("・");
    const parts = [
      `${names}も${storeShort(insights, rival)}（${toPercent(rateOf(rival))}）と僅差で、` +
        `どちらになるかはまだ決まっていません`
    ];
    const co = coOpenRate(insights, rival, missed[0]);
    if (co && co.shifts > 0) {
      parts.push(
        `${storeShort(insights, rival)}と${storeShort(insights, missed[0])}が同じシフトに揃ったのは` +
          `記録${co.shifts}シフト中${toPercent(co.rate)}なので、確率を足さないでください`
      );
    }
    parts.push("顔ぶれの割り振りは、どちらか一方にしか出せないので片側だけに出しています");
    return parts.join("。") + "。";
  }

  // Observed rotations do not establish when the shop makes its decisions.
  function sameDayDecisionNote(insights, outlook) {
    if (!outlook || outlook.basis === "actual") {
      return null;
    }
    return (
      "何店開くかも、どの店が開くかも、公開予定と過去の記録からの推測です。" +
      "お店の決定時刻や未公開の予定は分かりません。公式発表をご確認ください。"
    );
  }

  function getShiftAssignment({ insights, members, shift, outlook, pins, kitchenStaff, movedFrom }) {
    const storeIds = expectedOpenStores(insights, shift, outlook, members ?? 0);
    if (storeIds.length === 0) {
      return null;
    }
    const assignment = assignShiftStores({
      insights,
      members,
      shift,
      storeIds,
      pins,
      kitchenStaff,
      movedFrom
    });
    return assignment ? { ...assignment, basis: outlook.basis } : null;
  }

  // 同じ日の早いシフトの記録から「誰がどこにいたか」を取る。
  // 記録が無ければ null。推測した昼の店を渡すと、誤差が二重に乗る。
  function earlierShiftPlaces(insights, dateKey, shift) {
    if (!isLaterShift(insights, shift)) {
      return null;
    }
    const earlier = insights.shifts[insights.shifts.indexOf(shift) - 1];
    const record = recordedAssignment(insights, dateKey, earlier);
    if (!record) {
      return null;
    }
    return new Map([...record.byMaid].map(([name, placed]) => [name, placed.storeId]));
  }

  // 候補店の中で pickRate を合計1に正規化する。「この人はこの店」と断定せず、
  // どの店にもいる可能性があることを示す。実測で roster 全員が4店舗すべてに入っている。
  function storeProbabilities(insights, name, shift, storeIds) {
    if (!Array.isArray(storeIds) || storeIds.length === 0) {
      return {};
    }
    const tendency = maidTendencyFor(insights, name);
    const pickRate = tendency ? tendencyTables(tendency, shift).pickRate : {};
    // 0 の店を完全に消さないよう下限を置く。低くても行かないわけではない。
    const raw = storeIds.map((id) => Math.max(pickRate[id] ?? 0, 1e-6));
    const total = raw.reduce((sum, value) => sum + value, 0);
    return Object.fromEntries(storeIds.map((id, index) => [id, raw[index] / total]));
  }

  // 割り振り結果を、そのメイドさんの行に出すチップに変換する。
  // 表示した確率が実測とどれだけ離れているか。真ん中は5ポイント以内に収まるが、
  // 両端は自信過剰で、「94%」と出しても実測は76%ほどしか当たらない。
  // 帯そのものはデータ側の測定（accuracy.calibration）から引く。
  const CALIBRATION_DRIFT = 0.1;
  // 標本の少ないバケットは実測値自体が揺れるので、注記の根拠にしない。
  const CALIBRATION_MIN_SAMPLE = 100;
  // この測定は2店舗以上開いたシフトだけを対象にしている（データ側の scope）。
  // 1店舗の日は「その店にいる」が定義上100%になり、バケットを不当に良く見せる
  // ため除外されている。だから候補が1つのときは、この数字を引けない。
  const CALIBRATION_SCOPE_MIN_STORES = { twoOrMoreOpen: 2 };
  const CALIBRATION_MIN_STORES = 2;

  function calibrationNote(insights, rate, storeCount) {
    const calibration = insights?.accuracy?.calibration;
    const buckets = calibration?.buckets;
    if (!Array.isArray(buckets) || typeof rate !== "number") {
      return null;
    }
    const minStores =
      calibration.minOpenStores ??
      CALIBRATION_SCOPE_MIN_STORES[calibration.scope] ??
      CALIBRATION_MIN_STORES;
    if (typeof storeCount === "number" && storeCount < minStores) {
      return null;
    }
    const bucket = buckets.find(
      (candidate) => rate >= candidate.from && (rate < candidate.to || candidate.to >= 1)
    );
    if (!bucket || !(bucket.n >= CALIBRATION_MIN_SAMPLE) || typeof bucket.actual !== "number") {
      return null;
    }
    const drift = bucket.actual - rate;
    if (Math.abs(drift) < CALIBRATION_DRIFT) {
      return null;
    }
    const band = bucket.to >= 1
      ? `${toPercent(bucket.from)}以上`
      : `${toPercent(bucket.from)}〜${toPercent(bucket.to - 0.01)}`;
    // 較正は開いた店が分かっている日で測っている。開く店の予測を外すぶんは
    // 含まれていないので、実際にはこれよりさらに下がる。
    const measured = "開いた店が分かっている日で測った値なので、開く店の予測を外すぶんは含みません";
    return drift < 0
      ? `ただしこのくらい高い数字は自信過剰で、${band}と出したときに実際に当たったのは${toPercent(bucket.actual)}です（${measured}）`
      : `ただしこのくらい低い数字は控えめすぎて、${band}と出した店にも実際は${toPercent(bucket.actual)}の割合で入っています（${measured}）`;
  }

  // 「昼の店が夜も開いているのに、なぜ別の店にいるのか」への答え。
  // Explain the historical move table without claiming this evening is settled.
  function sameDayMoveNote(insights, name, fromStoreId, toStoreId) {
    const odds = sameDayMoveOdds(insights, name, fromStoreId);
    const rate = odds?.to?.[toStoreId];
    if (!odds || typeof rate !== "number") {
      return null;
    }
    const from = storeShort(insights, fromStoreId);
    const to = storeShort(insights, toStoreId);
    const who = odds.source === "maid" ? "この方は" : "全体では";
    const stay = odds.to[fromStoreId] ?? 0;
    // 同じ店に残るほうが少ないことを併記する。ここを書かないと、
    // 「開いているのになぜ」が残ったままになる。
    const staying = fromStoreId === toStoreId
      ? ""
      : `${from}に残るのは${toPercent(stay)}です。`;
    return (
      `昼は${from}にいた記録があります。${who}昼に${from}だったとき、` +
      `夜は${to}が${toPercent(rate)}でした（${odds.n}件）。${staying}` +
      "移動表の過去実績であり、この夜の確定情報ではありません"
    );
  }

  // 記録のある日は、予定表からの割り振りではなく実際の顔ぶれを出す。
  // 戻り値は assignShiftStores と同じ形にして、並べ替え・グループ化・人ごとの
  // 一覧がそのまま動くようにする。別の形にすると2つの画面が食い違う。
  function recordedAssignment(insights, dateKey, shift) {
    const record = insights?.actualRoster?.[dateKey]?.[shift];
    if (!record?.stores) {
      return null;
    }
    const storeIds = storesOf(insights)
      .map((store) => store.id)
      .filter((id) => Array.isArray(record.stores[id]) && record.stores[id].length > 0);
    if (storeIds.length === 0) {
      return null;
    }
    // trainees キーが無い日は「まだ判定していない」。全員が昇格済みという意味では
    // ないので、誰にも印を付けない。?? [] で潰すと、その区別が黙って消える。
    const judged = Array.isArray(record.trainees);
    const aliases = insights?.memberRegistry ? displayAliases(insights) : new Map();
    const canonical = (name) => aliases.get(name) ?? name;
    const trainees = new Set(judged ? record.trainees.map(canonical) : []);
    const byMaid = new Map();
    const capacity = {};
    for (const id of storeIds) {
      capacity[id] = record.stores[id].length;
      for (const rawName of record.stores[id]) {
        const name = canonical(rawName);
        byMaid.set(name, {
          storeId: id,
          score: 1,
          runnerUpId: null,
          runnerUpScore: 0,
          known: true,
          full: false,
          pin: null,
          trainee: judged ? trainees.has(name) : null
        });
      }
    }
    return { storeIds, capacity, byMaid, recorded: true, traineesJudged: judged };
  }

  // 記録の顔ぶれを、画面のほかの場所と同じ並びに直す。公式の掲載順に合わせ、
  // 予定表に載らない方（見習いなど）はその後ろに、記録の並びのまま置く。
  //
  // 記録がある日は、その日の顔ぶれを記録がすべて決める。予定表に名前があっても
  // 記録に無ければ出さない（実際にお休みだったのを「未定」と書くことになる）。
  function recordedRoster({ insights, dateKey, shift, schedule, roster }) {
    const assignment = recordedAssignment(insights, dateKey, shift);
    if (!assignment) {
      return null;
    }
    const posted = new Map(
      (schedule?.[dateKey]?.[shift] ?? []).map((entry) => [entry.name, entry])
    );
    const order = new Map((roster ?? []).map((name, index) => [name, index]));
    const entries = [...assignment.byMaid.keys()]
      .map((name, index) => ({
        name,
        listed: order.has(name),
        rank: order.has(name) ? order.get(name) : (roster?.length ?? 0) + index
      }))
      .sort((a, b) => a.rank - b.rank)
      .map(({ name, listed }) => ({
        ...(posted.get(name) ?? {}),
        name,
        listed,
        trainee: assignment.byMaid.get(name).trainee
      }));
    return { assignment, entries };
  }

  function getMaidStoreOutlook({ insights, name, shift, outlook, assignment, unpostedMaids }) {
    const placedInRecord = assignment?.recorded ? assignment.byMaid.get(name) : null;
    if (placedInRecord) {
      const store = storesOf(insights).find((c) => c.id === placedInRecord.storeId);
      if (!store) {
        return null;
      }
      // 記録のある日に確率を語らない。この人がこの店にいたことは分かっている。
      return {
        basis: "actual",
        storeId: store.id,
        rate: 1,
        label: compactStoreLabel(store),
        percent: "実績",
        alternative: null,
        title: `${shift}は${store.short}にいた記録があります`,
        srText: `${shift}は${store.short}にいた記録があります`
      };
    }
    const tendency = maidTendencyFor(insights, name);
    if (!tendency || !assignment) {
      return null;
    }
    const placed = assignment.byMaid.get(name);
    if (!placed) {
      return null;
    }
    const stores = storesOf(insights);
    const shortOf = (id) => storeShort(insights, id);

    if (placed.pin) {
      const store = stores.find((candidate) => candidate.id === placed.storeId);
      if (!store) {
        return null;
      }
      const strength = typeof placed.pin.pickRate === "number"
        ? `${store.short}が開いた${shift}の${toPercent(placed.pin.pickRate)}をこの店で過ごしています`
        : null;
      const detail = strength ? `（${strength}）` : "";
      const declared = HOME_STORE_SOURCE_LABEL[placed.pin.source];
      const source = declared
        ? `所属店は${declared}です${detail}`
        : `所属店は公式の配属が分からないため、出勤実績から推定したものです${detail}`;
      return {
        basis: "event",
        storeId: store.id,
        rate: 1,
        label: compactStoreLabel(store),
        percent: "確定",
        alternative: null,
        title: [`${placed.pin.label}の主役なので、所属店の${store.short}にいます`, source].join("。"),
        srText: `${placed.pin.label}の主役なので所属店の${store.short}にいます`
      };
    }

    const probabilities = storeProbabilities(insights, name, shift, assignment.storeIds);
    // 並びは割り振り結果でまとめているので、チップも同じ店を先頭に出す。
    // ただし断定はせず、その店にいる確率と、次に可能性の高い店を並べる。
    const top = stores.find((candidate) => candidate.id === placed.storeId);
    if (!top) {
      return null;
    }
    const runnerUpId = assignment.storeIds
      .filter((id) => id !== top.id)
      .sort((a, b) => probabilities[b] - probabilities[a])[0] ?? null;
    const second = runnerUpId ? stores.find((candidate) => candidate.id === runnerUpId) : null;
    const { scope } = tendencyTables(tendency, shift);
    // 個人の傾向だけ集計期間が短い。店舗側の数字と混同されないよう明記する。
    const days = insights.tendencyWindow?.days;
    const period = days ? `直近${days}日` : null;
    const scopeNote = [
      scope === shift ? `${shift}の実績` : "昼夜あわせた実績（このシフトは件数が少ないため）",
      period
    ]
      .filter(Boolean)
      .join("・");
    const everyStore = [...assignment.storeIds]
      .sort((a, b) => probabilities[b] - probabilities[a])
      .map((id) => `${shortOf(id)} ${toPercent(probabilities[id])}`)
      .join(" / ");
    // 所属店。確率は実績が主で配属は弱い事前分布なので、実績の多い人ほど配属から
    // 離れる。そのずれを読み手が確かめられるように出す。出どころは2種類あり、
    // 公式サイトに載っていない人はお店からの案内によるので、そう書き分ける。
    const postedSource = tendency?.posted
      ? (new Set(unpostedMaids ?? []).has(name) ? "shop" : "site")
      : null;
    const postedNote = postedSource
      ? `${HOME_STORE_SOURCE_LABEL[postedSource]}は${shortOf(tendency.posted)}`
      : null;
    // 候補が1つなら、正規化の結果この店が100%になる。それはこの人の話ではなく
    // 「開く店が1つと見込んだ」という話なので、不確かさの在り処を書いておく。
    const soleStoreNote = assignment.storeIds.length === 1
      ? `この${shift}は${top.short}だけが開く見込みなので、その見込みが当たればこの店です`
      : null;
    // 在籍者数は roster が増減するので数えて出す。文言に焼き込むと黙って古くなる。
    const rosterSize = Object.keys(insights.maidTendency ?? {}).length;

    return {
      basis: "probability",
      storeId: top.id,
      rate: probabilities[top.id],
      label: compactStoreLabel(top),
      percent: toPercent(probabilities[top.id]),
      alternative: second
        ? `${compactStoreLabel(second)} ${toPercent(probabilities[second.id])}`
        : null,
      title: [
        `この${shift}に開きそうな店にいる確率：${everyStore}（${scopeNote}）`,
        postedNote,
        soleStoreNote,
        calibrationNote(insights, probabilities[top.id], assignment.storeIds.length),
        `どの店にも入る可能性があります。実際、在籍${rosterSize}名は全員が4店舗すべてに入った実績があります`,
        "いちばん高い店だけを見ると、たまに入る店を取りこぼします"
      ]
        .filter(Boolean)
        .join("。"),
      srText: second
        ? `${shift}は${top.short}が${toPercent(probabilities[top.id])}、次に${second.short}が${toPercent(probabilities[second.id])}`
        : `${shift}は${top.short}が${toPercent(probabilities[top.id])}`
    };
  }

  // その人の行き先をどれだけ当てられたか。直近180日の各シフトを、その手前
  // 120日だけを見た条件付きの実測値。実際の開店集合を知る個人独立argmaxで、
  // 本番の店舗予測・整数定員・移動補正を含む精度ではない。
  //
  // 全体値（accuracy.maidStoreGivenOpen）とは測り方が違うので、混ぜない。
  // 測れていない人（記録が少ない人）には、全体値で埋めずに測れていないと書く。
  function maidAccuracy(insights, name) {
    const measured = maidTendencyFor(insights, name)?.accuracy;
    if (!measured || typeof measured.rate !== "number" || !(measured.n > 0)) {
      return null;
    }
    return { rate: measured.rate, n: measured.n };
  }

  // p^n is an illustration assuming independent trials with the same p,
  // not an observed accuracy for the displayed itinerary.
  function itineraryConfidence(insights, guesses, name) {
    const measured = maidAccuracy(insights, name);
    if (!measured || !(guesses > 0)) {
      return null;
    }
    return {
      perStop: measured.rate,
      samples: measured.n,
      guesses,
      allRight: Math.pow(measured.rate, guesses)
    };
  }

  // 低い数字が出る人もいる。当たらないのはその人のせいではないので、
  // 主語をこちら側に置く。「この人は読めない」ではなく「私たちが当てられない」。
  function maidAccuracyNote(insights, name, isKitchen) {
    const measured = maidAccuracy(insights, name);
    if (!measured) {
      return "この方は記録が少なく、行き先をどれだけ当てられるかは測れていません";
    }
    const why = isKitchen
      ? "キッチンにゃんこは配属と関係なく4店を回るので、とくに当てにくくなります。"
      : "";
    return (
      `${why}この方の行き先は、過去${measured.n}件を試して` +
      `${toPercent(measured.rate)}当てられました（実際の開店集合を知る条件付き・個人別の評価）。` +
      "この画面の店舗予測や定員調整を含む精度ではありません"
    );
  }

  // 人ごとの一覧を組み立てる。店ごとの画面と同じ割り振りを引くので、両者は食い違わない。
  //
  // この軸には固有の危険がある。1件あたりの的中はその人ごとに 39〜81% と幅があり、
  // 実際の開店集合を知る条件付きの精度を、そのまま本番精度とは呼べない。
  // 同じ人の予測を縦に並べると、その外れが一覧で見える。店ごとの画面では
  // 1行ずつ独立に見えて気づかなかったものが表に出るので、件数を数えて
  // 「全部当たることはまずない」と先に言う。
  function maidItinerary({ schedule, name, dates, shifts, resolve, kitchenStaff }) {
    const cooks = kitchenStaff instanceof Set ? kitchenStaff : new Set(kitchenStaff ?? []);
    const stops = [];
    const changes = [];
    for (const key of dates ?? []) {
      for (const shift of shifts ?? []) {
        const { outlook, assignment, observed, personal, entries, confirmedOnly } = resolve(key, shift) ?? {};
        const resolvedEntry = entries?.find((entry) => entry.name === name);
        const notice = assignment?.recorded ? null : personal?.byMaid.get(name);
        const officialNotice = assignment?.recorded ? null : resolvedEntry?.officialNotice;
        if (notice?.absent) {
          changes.push({ dateKey: key, shift, personalNotice: notice });
          continue;
        }
        const observation = assignment?.recorded ? null : observed?.byMaid.get(name);
        // 記録のある日は、その日の顔ぶれを記録が決める。予定表を見ると、
        // お休みだった方に行を作り、記録にしかいない方を落とすことになる。
        const roll = assignment?.recorded
          ? (assignment.byMaid.has(name) ? { name } : null)
          : (schedule?.[key]?.[shift] ?? []).find((member) => member.name === name)
            ?? (observation || officialNotice || notice?.storeId || notice?.returned || notice?.conflict ? { name } : null);
        if (!roll) {
          if (notice) changes.push({ dateKey: key, shift, personalNotice: notice });
          continue;
        }
        const placed = notice || officialNotice || (confirmedOnly && !assignment?.recorded)
          ? null : assignment?.byMaid?.get(name) ?? null;
        // キッチンにゃんこの行き先は、見込みの日には店ごとの画面でも名乗らない。
        // ここで名乗ると、同じ人・同じ日で2つの画面が違うことを言う。
        const loose =
          placed && !assignment.recorded && !observation && !placed.pin && cooks.has(name);
        const storeId = observation?.storeIds[0] ?? officialNotice?.storeId ?? notice?.storeId ?? (loose ? null : placed?.storeId ?? null);
        const row = storeId
          ? outlook?.entries?.find((candidate) => candidate.store.id === storeId) ?? null
          : null;
        // 決まっているのは、記録のある日と、記念日の主役だけ。
        //
        // 店の state だけを見てはいけない。記念日は主役の所属店を「営業」で確定
        // させるので、その店に割り振られた他の人まで確定に見えてしまう。実際に
        // 未来の日付で「3号店にいた記録があります」と出ていた。店が開くことと、
        // その人がそこにいることは別の話。
        //
        // Store-only records do not establish a person's placement.
        const recorded = Boolean((assignment?.recorded && placed) || observation);
        const announced = Boolean((officialNotice?.storeId || notice?.storeId) && !observation);
        const host = !observation && !notice && !officialNotice && Boolean(placed?.pin);
        const settled = recorded || host || announced;
        const forecast = !settled && !confirmedOnly && !notice && !officialNotice && Boolean(placed);
        stops.push({
          dateKey: key,
          shift,
          storeId,
          kitchen: Boolean(loose),
          // 店ごとの画面と同じ三段階を使う。別の線を引くと画面が食い違う。
          // 確定でないなら、店が100%でも「見込み」として出す。
          state: announced ? "announced" : recorded || host ? "open" : forecast && row ? stateForRate(row.rate ?? 0) : null,
          openRate: recorded || notice || officialNotice || confirmedOnly ? null : row?.rate ?? null,
          recorded,
          observed: Boolean(observation),
          personal: announced && !officialNotice,
          personalNotice: notice,
          officialNotice,
          confirmedOnly: Boolean(confirmedOnly),
          forecast,
          storeIds: observation?.storeIds ?? (announced ? [storeId] : []),
          sourcePosts: observation?.sources ?? officialNotice?.sources ?? [],
          ...(resolvedEntry?.workTimingNote ? { workTimingNote: resolvedEntry.workTimingNote } : {}),
          ...(resolvedEntry?.memberReview ? { memberReview: resolvedEntry.memberReview } : {}),
          halfMonthSources: resolvedEntry?.halfMonthSources ??
            schedule?.[key]?.[shift]?.find((entry) => entry.name === name)?.halfMonthSources ?? [],
          nameCorrections: observation?.nameCorrections ?? officialNotice?.nameCorrections ?? [],
          host,
          settled,
          trainee: observation ? observation.trainee : officialNotice?.trainee ?? notice?.trainee ?? placed?.trainee ?? null,
          eventLabel: roll.eventLabel
            ?? schedule?.[key]?.[shift]?.find((entry) => entry.name === name)?.eventLabel
            ?? null
        });
      }
    }
    return { name, stops, changes, guesses: stops.filter((stop) => stop.forecast).length };
  }

  // 開く店の数は実測の表（openCountByHeadcount）から決めている。表は
  // 「何人を超えたら1店増えるか」の区切りなので、言える最大は要素数+1になる。
  // いま3要素目が無いのは、記録に4店のシフトが一度も無いからで、
  // 「4店にはならない」と測ったわけではない。
  //
  // だから上限に当たったときは「3店が最有力」ではなく「これ以上は数えられない」
  // と書く。人数がいくら増えても答えが動かなくなる場所なので、そこだけは断る。
  function openCountCeilingNote(insights, shift, storeCount) {
    const table = insights?.openCountByHeadcount?.[shift];
    const stores = storesOf(insights).length;
    if (!Array.isArray(table) || table.length === 0 || !(storeCount > 0)) {
      return null;
    }
    const ceiling = table.length + 1;
    // 上限が全店なら、それ以上は存在しないので断ることが無い。
    if (storeCount !== ceiling || !(stores > ceiling)) {
      return null;
    }
    const seen = insights?.openCountPerShift?.[shift];
    const counted = seen
      ? Object.values(seen).reduce((sum, value) => sum + value, 0)
      : 0;
    const above = seen
      ? Object.entries(seen)
        .filter(([count]) => Number(count) > ceiling)
        .reduce((sum, [, value]) => sum + value, 0)
      : 0;
    const evidence = counted > 0
      ? `${ceiling}店より多いシフトは${shift}${counted}件の記録に${above}件しかありません`
      : `${ceiling}店より多いシフトの記録がありません`;
    return (
      `。この${shift}は${ceiling}店で、いまの表で言える上限です。` +
      `${evidence}。${ceiling}店がいちばんありそう、という意味ではなく、` +
      `これより多いかどうかを数える材料がありません`
    );
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      addDays,
      applyEventCertainty,
      applyHomeStaff,
      applyPostedTilt,
      applySameDayEvidence,
      assignShiftStores,
      calibrationNote,
      coOpenRate,
      dateKey,
      dayEvents,
      earlierShiftPlaces,
      eventStorePins,
      eventImageFor,
      expectedOpenStores,
      expectedTrainees,
      getDateGridColumn,
      getMaidStoreOutlook,
      getShiftAssignment,
      getStoreOutlook,
      getTokyoDateDefaults,
      getVisibleMonthDates,
      groupByAssignedStore,
      isDateKeyInRange,
      itineraryConfidence,
      lastActualDateOf,
      maidAccuracy,
      maidAccuracyNote,
      maidItinerary,
      monthCells,
      observedShift,
      activeObservationPosts,
      observedTrainee,
      personalShift,
      personalPostsForView,
      personalPostLink,
      rosterPostLink,
      resolveShiftRoster,
      validatePersonalShifts,
      validateHalfMonthSchedules,
      validateMemberRegistry,
      createMemberRegistryAdapter,
      memberFilterNames,
      validateWorkTiming,
      workTimingLabel,
      workTimingDescription,
      resolveWorkTiming,
      buildEffectiveSchedule,
      halfMonthPeriod,
      dayHasPersonStoreEvidence,
      observationEntries,
      orderRosterEntries,
      nearMissNote,
      nearMissStores,
      openCountCeilingNote,
      openStoresOn,
      openStoresOnDay,
      recordedAssignment,
      recordedRoster,
      sameDayDecisionNote,
      sameDayMoveNote,
      sameDayMoveOdds,
      schedulePendingNote,
      scheduleSystemNote,
      sortByAssignedStore,
      storeCapacities,
      storeProbabilities,
      storeSizeNote,
      tokyoToday,
      validateObservations,
      weekdayBucket,
      weekdayIndex
    };
  }

  if (typeof window === "undefined" || typeof document === "undefined") {
    return;
  }

  let registry;
  try {
    registry = createMemberRegistryAdapter(window.MEMBER_REGISTRY);
  } catch {
    const note = document.createElement("p");
    note.textContent = "メンバー名簿を読み込めません。再読み込みして確認してください。古い名簿への切替は行いません。";
    note.setAttribute("role", "alert");
    document.querySelector("#maid-checkboxes").replaceChildren();
    document.querySelector("#calendar").replaceChildren(note);
    document.querySelector("#result-summary").textContent = "名簿を確認できないため表示を停止しています。";
    return;
  }
  const data = {
    ...window.SCHEDULE_DATA,
    roster: registry.activeNames,
    kitchenStaff: registry.kitchenStaff,
    displayNames: registry.displayNames,
    homeStore: registry.homeStore,
    normalOrderBefore: registry.normalOrderBefore,
    unpostedMaids: registry.members.filter((member) => member.officialListing === "unlisted").map((member) => member.canonicalName)
  };
  const displayName = (name) => Object.hasOwn(data.displayNames ?? {}, name) ? data.displayNames[name] : name;
  const displayText = (text) => {
    const names = Object.keys(data.displayNames).filter((name) => data.displayNames[name] !== name)
      .sort((a, b) => b.length - a.length);
    return names.length ? text.replace(new RegExp(names.join("|"), "g"), displayName) : text;
  };
  const shifts = ["昼", "夜"];
  const TRAINEE_PLACEHOLDER = "見習い";

  // 見習いの人数は入店で動くので、データ側は半減期30日で見ている（人数の180日
  // とは別）。落ち着いた量ではなく来月には変わるので、割合そのものは書かない。
  //
  // Historical comparisons do not establish impossibility or future accuracy.
  function traineeGuessNote(insights, shift, storeCount) {
    const where = storeCount > 1
      ? `${storeCount}店開けば、そのどこかに`
      : "この店に";
    const lead = `予定表に出ない見習いにゃんこです。${where}いそうですが、どなたかは分かりません（${shift}の実績から）`;
    if (storeCount < 2) {
      return lead;
    }
    return `${lead}。人数だけ見込んでいて、店は決めていません。` +
      "表示した店自体も予測なので、別の店や見習いがいない場合もあります";
  }
  const kitchenStaff = new Set(data.kitchenStaff ?? []);
  let filterNames = [...data.roster];
  let rosterNames = new Set(filterNames);
  // 公式サイト未掲載の人。所属は分かるが出どころがサイトではないので書き分ける。
  const unpostedMaids = new Set(data.unpostedMaids ?? []);
  const weekdays = ["日", "月", "火", "水", "木", "金", "土"];
  const shiftDetails = {
    "昼": { icon: "☀", className: "shift-day" },
    "夜": { icon: "☾", className: "shift-night" }
  };

  const insights = { ...window.STORE_INSIGHTS, memberRegistry: registry };
  let observations = validateObservations(window.OBSERVED_SHIFTS ?? EMPTY_OBSERVATIONS);
  let observationLoading = false;
  let personalShifts = EMPTY_PERSONAL_SHIFTS;
  let personalLoading = false;
  try {
    personalShifts = validatePersonalShifts(window.PERSONAL_SHIFTS ?? EMPTY_PERSONAL_SHIFTS);
  } catch (error) {
    console.error("Personal snapshot load failed", error);
  }
  let halfMonthSchedules = EMPTY_HALF_MONTH_SCHEDULES;
  let halfMonthLoading = false;
  try {
    halfMonthSchedules = validateHalfMonthSchedules(window.HALF_MONTH_SCHEDULES ?? EMPTY_HALF_MONTH_SCHEDULES,
      { registry, insights, personal: personalShifts });
  } catch (error) {
    console.error("Half-month snapshot load failed", error);
  }
  let effectiveSchedule = buildEffectiveSchedule(data.schedule, halfMonthSchedules, { registry });
  // Retain provenance for independently evidenced rows, even when plan-only projection is excluded.
  let sourceSchedule = buildEffectiveSchedule(data.schedule, halfMonthSchedules, { registry, includeInactivePlans: true });
  const storeList = storesOf(insights);
  const lastActualKey = lastActualDateOf(insights);
  const hasInsights = Boolean(insights) && storeList.length > 0;

  function getShiftOutlook(key, shift) {
    const entries = effectiveSchedule[key]?.[shift] ?? [];
    const pins = eventStorePins({
      insights,
      entries,
      homeStore: data.homeStore,
      unpostedMaids
    });
    const outlook = getStoreOutlook({
      insights,
      dateKey: key,
      shift,
      lastActualDate: lastActualKey
    });
    // 顔ぶれの配属から2番手を読み直す。絞り込みに動かされないよう全員で数える。
    // ただしキッチンにゃんこは配属と実際が合わないので、そこでは数えない。
    const withHome = applyHomeStaff(
      insights,
      outlook,
      entries.map((entry) => entry.name),
      data.homeStore,
      data.kitchenStaff
    );
    // 公式配属の顔ぶれも割合そのものに入れる。ここを入れずに順位づけだけで使うと、
    // 画面の数字と選ばれた店が食い違う。
    const withPosted = applyPostedTilt(
      insights,
      withHome,
      shift,
      entries.map((entry) => entry.name)
    );
    return { outlook: applyEventCertainty(insights, withPosted, pins), pins };
  }

  function createStoreOutlook(outlook, shift, members) {
    const wrapper = document.createElement("div");
    wrapper.className = "store-outlook";
    // 僅差で顔ぶれを出せなかった店があれば、そのことも本文に書く。
    const missed = nearMissNote(insights, shift, outlook, members ?? 0);
    const sameDay = sameDayDecisionNote(insights, outlook);
    const opening = new Set(
      outlook.basis === "actual"
        ? outlook.openStores ?? []
        : expectedOpenStores(insights, shift, outlook, members ?? 0)
    );
    // 記録の日は数え直す必要がない。実際に何店開いたか分かっている。
    const ceiling = outlook.basis === "actual"
      ? null
      : openCountCeilingNote(insights, shift, opening.size);
    wrapper.title = [outlook.summary, sameDay, missed, ceiling]
      .filter(Boolean)
      .join("")
      // 各文は「。」始まりで書いてあるが、前の文が「。」で終わることもある。
      .replace(/。+/g, "。");

    const nearMiss = new Set(nearMissStores(insights, shift, outlook, members ?? 0));

    const description = document.createElement("p");
    description.className = "visually-hidden";
    description.textContent =
      `${shift}の店舗（${outlook.badge}）：` +
      `${outlook.entries.map((entry) => entry.srText).join("、")}。${wrapper.title}`;

    const list = document.createElement("ul");
    list.className = "store-chips";
    list.setAttribute("aria-hidden", "true");

    // 表に出すのは、開くと見込んだ店だけ。確率は出さない。
    // どの店を開けるかは当日決まるので、数字を並べても読み手は選べない。
    // 4店ぶんの割合・根拠・僅差の説明は、上の読み上げ用テキストと title に残す。

    outlook.entries
      .filter((entry) => opening.has(entry.store.id) || nearMiss.has(entry.store.id))
      .forEach((entry) => {
      const chip = document.createElement("li");
      chip.className = `store-chip is-${entry.state}`;
      chip.dataset.store = entry.store.id;
      const size = storeSizeNote(insights, shift, entry.store.id);
      // 僅差で顔ぶれを出せなかった店は、無視したわけではないと分かるようにする。
      if (nearMiss.has(entry.store.id)) {
        chip.classList.add("is-near-miss");
        chip.title = [size, "僅差で選ばれなかった店です。顔ぶれは片方にしか出せません"]
          .filter(Boolean)
          .join("。");
      } else if (size) {
        chip.title = size;
      }

      const name = document.createElement("span");
      name.className = "store-chip-name";
      name.textContent = compactStoreLabel(entry.store);
      chip.append(name);
      // 割合は表に出さないが、HTML には残す。根拠を確かめたいときの手がかり。
      const value = document.createElement("span");
      value.className = "store-chip-rate";
      value.textContent = entry.text;
      chip.append(value);
      list.append(chip);
    });

    wrapper.append(description, list);
    // 上限に当たった日は、そのことを目に見える形でも言う。ツールチップだけだと
    // 「3店が最有力」としか読めない。読まれ方が変わる情報は表に出す。
    if (ceiling) {
      const capped = document.createElement("p");
      capped.className = "store-outlook-capped";
      capped.setAttribute("aria-hidden", "true");
      capped.textContent = `${opening.size}店までしか数えられません`;
      wrapper.append(capped);
    }
    return wrapper;
  }

  function createStatusBadge(outlook) {
    const badge = document.createElement("span");
    badge.className = `store-status-badge ${outlook.badgeClass}`;
    badge.textContent = outlook.badge;
    badge.setAttribute("aria-hidden", "true");
    return badge;
  }

  function createMaidStoreChip(chipData, showStore = true) {
    const chip = document.createElement("span");
    chip.className = "maid-store-chip";
    chip.dataset.store = chipData.storeId;
    chip.setAttribute("aria-hidden", "true");

    if (showStore) {
      const label = document.createElement("span");
      label.textContent = chipData.label;
      chip.append(label);
    }
    // 月グリッドはセルが狭く、割合まで出すと1件が2行になるので CSS で出し分ける。
    const percent = document.createElement("span");
    percent.className = showStore ? "maid-store-chip-rate" : "maid-store-chip-only";
    percent.textContent = chipData.percent;
    chip.append(percent);

    // 上位2店で実測97%をカバーする。狭いセルでは隠し、ツールチップに全店を出す。
    if (chipData.alternative) {
      const alternative = document.createElement("span");
      alternative.className = "maid-store-chip-alt";
      alternative.textContent = chipData.alternative;
      chip.append(alternative);
    }
    return chip;
  }

  const VIEW_MODES = {
    calendar: {
      label: "月間カレンダー",
      help: "イベント画像は日付単位。日付をタップすると、昼・夜のお給仕詳細が開きます。"
    },
    forecast: {
      label: "予測",
      help: "開いていそうな店舗と、その日出るメンバーの割り振りを出します。"
    },
    roster: {
      label: "誰いるか",
      help: "店舗の予測を隠して、誰がお給仕に出るかだけを見ます。"
    },
    maid: {
      label: "この人はどこ",
      help: "メイドさんごとのお給仕一覧です。記録・イベントの主役・店舗の推測を区別します。"
    }
  };
  const VIEW_MODE_KEY = "akibazettai:view-mode:v2";

  function readStoredMode() {
    try {
      const stored = window.localStorage?.getItem(VIEW_MODE_KEY);
      return stored && VIEW_MODES[stored] ? stored : null;
    } catch {
      return null;
    }
  }

  function storeMode(mode) {
    try {
      window.localStorage?.setItem(VIEW_MODE_KEY, mode);
    } catch {
      // プライベートモードなどで保存できなくても表示には影響しない。
    }
  }

  const defaults = getTokyoDateDefaults();
  const state = {
    visibleMonth: new Date(defaults.year, defaults.month - 1, 1),
    selectedMaids: new Set([...registry.knownNames, ...registry.unresolvedNames]),
    dateFrom: defaults.dateFrom,
    dateTo: defaults.dateTo,
    customRange: false,
    selectedDate: null,
    hideKitchen: false,
    viewMode: readStoredMode() ?? "calendar"
  };

  const elements = {
    calendar: document.querySelector("#calendar"),
    monthTitle: document.querySelector("#month-title"),
    monthNumber: document.querySelector("#month-number"),
    monthYear: document.querySelector("#month-year"),
    resultSummary: document.querySelector("#result-summary"),
    lastUpdated: document.querySelector("#last-updated"),
    modeHelp: document.querySelector("#mode-help"),
    modeInputs: [...document.querySelectorAll('input[name="view-mode"]')],
    maidCheckboxes: document.querySelector("#maid-checkboxes"),
    maidFilterDetails: document.querySelector("#maid-filter-details"),
    maidFilterSummary: document.querySelector("#maid-filter-summary"),
    dateFrom: document.querySelector("#date-from"),
    dateTo: document.querySelector("#date-to"),
    previousMonth: document.querySelector("#previous-month"),
    nextMonth: document.querySelector("#next-month"),
    currentMonth: document.querySelector("#current-month"),
    dayDialog: document.querySelector("#day-dialog"),
    dialogTitle: document.querySelector("#day-dialog-title"),
    dialogEvents: document.querySelector("#day-dialog-events"),
    dialogContent: document.querySelector("#day-dialog-content"),
    closeDialog: document.querySelector("#close-day-dialog"),
    selectAll: document.querySelector("#select-all"),
    clearAll: document.querySelector("#clear-all"),
    hideKitchen: document.querySelector("#hide-kitchen"),
    resetFilters: document.querySelector("#reset-filters")
  };

  function isInDateRange(key) {
    return isDateKeyInRange(key, state.dateFrom, state.dateTo);
  }

  function isVisibleMaid(name) {
    if (state.hideKitchen && kitchenStaff.has(name)) {
      return false;
    }
    // 記録には、予定表に載らない方（見習いなど）も出てくる。その方たちには
    // チェックボックスが無いので、絞り込みを始めた時点で隠す。残したままだと
    // 「1人だけ選んだのに他の名前が出る」ことになり、絞り込みが効いて見えない。
    if (!rosterNames.has(name)) {
      return filterNames.every((name) => state.selectedMaids.has(name));
    }
    return state.selectedMaids.has(name);
  }

  // その日そのシフトの顔ぶれ。記録があればそれを、無ければ予定表を使う。
  function shiftRoster(key, shift) {
    return resolveShiftRoster({
      insights, observations, personal: personalShifts, dateKey: key, shift,
      schedule: effectiveSchedule, sourceSchedule, roster: registry.knownNames, nameCorrections: data.observationNameCorrections,
      personalEventAdditions: data.personalEventAdditions
    });
  }

  function filteredEntries(key, shift) {
    return shiftRoster(key, shift).entries.filter((entry) => isVisibleMaid(entry.name));
  }

  function observationTime(value) {
    return new Intl.DateTimeFormat("ja-JP", {
      timeZone: "Asia/Tokyo", month: "numeric", day: "numeric",
      hour: "2-digit", minute: "2-digit", hour12: false
    }).format(new Date(value));
  }

  function createObservationLink(post, label) {
    const link = document.createElement("a");
    link.href = post.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.className = "observation-source";
    link.textContent = label ?? `公式投稿 ${observationTime(post.createdAt)}`;
    link.title = `@${post.authorScreenName} / ${post.id}`;
    return link;
  }

  function createRosterPostLabel(name, key, shift) {
    const source = rosterPostLink({
      schedule: sourceSchedule, personal: personalShifts, insights, observations,
      dateKey: key, shift, name, nameCorrections: data.observationNameCorrections,
      personalEventAdditions: data.personalEventAdditions
    });
    const link = document.createElement(source ? "a" : "span");
    if (source) {
      link.href = source.post.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.dataset.sourceKind = source.kind;
      link.dataset.name = name;
      link.dataset.date = key;
      link.dataset.shift = shift;
      link.title = `${displayName(name)}：${source.kind === "personal"
        ? "本人の当日投稿を開く" : "予定表の投稿を開く（当日の出勤確認ではありません）"}`;
      link.setAttribute("aria-label", link.title);
    }
    return link;
  }

  function personalNoticeLabel(person) {
    if (person.conflict) return "案内が不一致・保留";
    if (person.absent) return "取消";
    const pending = person.uncertain ? "・保留" : "";
    if (person.late) return "お給仕予定" + pending;
    if (person.returned && !person.storeId) return "復帰・未発表" + pending;
    if (person.storeId) return "お給仕予定" + pending;
    return "保留";
  }

  function officialNoticeLabel(notice) {
    if (notice.conflict) return "案内が不一致・保留";
    return "お給仕予定" + (notice.uncertain ? "・保留" : "");
  }

  function appendWorkTiming(target, fact) {
    const label = workTimingLabel(fact);
    if (!label) return;
    const note = document.createElement("span");
    note.className = "entry-update work-timing-note";
    note.textContent = label;
    note.dataset.boundary = fact.boundary;
    note.dataset.sourceKind = fact.source.sourceKind;
    note.setAttribute("aria-hidden", "true");
    target.append(note);
  }

  function createChangeNotice(person, prefix = "") {
    const change = document.createElement("p");
    change.className = "shift-change";
    change.dataset.name = person.name;
    change.textContent = `${prefix}${displayName(person.name)}：${personalNoticeLabel(person)}`;
    return change;
  }

  const officialEmbeds = new Map();
  let officialWidgetScript;

  function loadOfficialWidgets() {
    if (!officialWidgetScript) {
      officialWidgetScript = new Promise((resolve, reject) => {
        if (window.twttr?.widgets?.createTweet) {
          resolve(window.twttr.widgets);
          return;
        }
        const script = document.createElement("script");
        script.src = "https://platform.twitter.com/widgets.js";
        script.async = true;
        const timer = window.setTimeout(() => reject(new Error("widget timeout")), 10000);
        const ready = () => {
          window.clearTimeout(timer);
          if (window.twttr?.widgets?.createTweet) resolve(window.twttr.widgets);
          else reject(new Error("widget unavailable"));
        };
        script.onload = () => {
          if (window.twttr?.widgets?.createTweet) ready();
          else if (typeof window.twttr?.ready === "function") window.twttr.ready(ready);
          else ready();
        };
        script.onerror = () => {
          window.clearTimeout(timer);
          reject(new Error("widget blocked"));
        };
        document.head.append(script);
      });
    }
    return officialWidgetScript;
  }

  function createOfficialPost(post, key, shift, store) {
    const label = `${store.short} ${observationTime(post.createdAt)}`;
    const existing = officialEmbeds.get(post.id);
    if (existing) {
      // A cached widget can move between the store view and the hidden dialog.
      const previousSection = existing.node.closest?.(".shift-section");
      if (previousSection) delete previousSection.dataset.renderKey;
      existing.link.textContent = label;
      existing.link.setAttribute("aria-label", `${label}の集合ポストを開く`);
      existing.link.dataset.focusKey = `${key}|${shift}|${store.id}|${post.id}`;
      return existing.node;
    }
    const node = document.createElement("div");
    node.className = "official-post";
    node.dataset.postId = post.id;
    const link = createObservationLink(post, label);
    link.setAttribute("aria-label", `${label}の集合ポストを開く`);
    link.dataset.focusKey = `${key}|${shift}|${store.id}|${post.id}`;
    node.append(link);
    const target = document.createElement("div");
    target.className = "official-embed";
    target.setAttribute("inert", "");
    target.setAttribute("aria-hidden", "true");
    node.append(target);
    officialEmbeds.set(post.id, { node, link, target, started: false });
    return node;
  }

  async function embedOfficialPost(post) {
    if (!/^\d{10,25}$/.test(post.id) || post.authorScreenName !== "akibazettai" ||
        post.authorId !== "822429861218131969" ||
        post.url !== `https://x.com/${post.authorScreenName}/status/${post.id}`) return;
    const embed = officialEmbeds.get(post.id);
    if (!embed || embed.started) return;
    embed.started = true;
    embed.node.dataset.embedState = "loading";
    try {
      const widgets = await loadOfficialWidgets();
      const result = await widgets.createTweet(post.id, embed.target, {
        theme: "light", dnt: true, width: Math.max(250, Math.min(550, embed.target.clientWidth || 300))
      });
      if (!result) throw new Error("post unavailable");
      embed.target.querySelectorAll("iframe").forEach((frame) => { frame.tabIndex = -1; });
      embed.node.dataset.embedState = "ready";
    } catch {
      embed.node.dataset.embedState = "unavailable";
      const message = document.createElement("p");
      message.className = "embed-fallback";
      message.textContent = "埋め込みを読み込めません。元ポストから確認できます。";
      embed.node.append(message);
    }
  }

  function createRosterEntry(entry, key, shift, {
    evidence, storeId, note = ""
  } = {}) {
    const item = document.createElement("li");
    item.className = "maid-entry";
    item.dataset.name = entry.name;
    item.dataset.evidence = evidence;
    if (storeId) item.dataset.store = storeId;
    const name = createRosterPostLabel(entry.name, key, shift);
    name.className = "maid-name";
    name.textContent = displayName(entry.name);
    name.dataset.name = entry.name;
    name.dataset.focusKey = `${key}|${shift}|${entry.name}`;
    if (!name.href) {
      name.tabIndex = -1;
    }
    item.append(name);
    const timingDescription = workTimingDescription(entry.workTimingNote);
    appendWorkTiming(item, entry.workTimingNote);
    const descriptions = note ? [note] : [];
    if (entry.memberReview) {
      const review = document.createElement("span");
      review.className = "entry-update";
      review.textContent = "要確認";
      item.append(review);
      descriptions.push(entry.memberReview);
    }
    if (timingDescription) {
      descriptions.push(timingDescription);
      if (name.href) {
        name.title += `。${timingDescription}`;
        name.setAttribute("aria-label", name.title);
      }
    }
    if (kitchenStaff.has(entry.name)) {
      item.classList.add("is-kitchen");
      descriptions.push("キッチンにゃんこ");
    }
    if (entry.trainee === true) {
      item.classList.add("is-trainee");
      const mark = document.createElement("span");
      mark.className = "maid-trainee";
      mark.textContent = "🔰";
      mark.setAttribute("aria-hidden", "true");
      item.append(mark);
      descriptions.push("見習いにゃんこ");
    }
    if (entry.featured) {
      item.classList.add("is-featured");
      descriptions.push(`${entry.eventLabel}の主役（公開予定）`);
    }
    if (entry.personalNotice && !entry.officialNotice) {
      const person = entry.personalNotice;
      if (person.conflict || (person.returned && !person.storeId) ||
          (!entry.observed && person.uncertain)) {
        const update = document.createElement("span");
        update.className = "entry-update";
        update.textContent = personalNoticeLabel(person);
        item.append(update);
      }
      descriptions.push(personalNoticeLabel(person));
    }
    if (entry.officialNotice) {
      if (entry.officialNotice.conflict || entry.officialNotice.uncertain) {
        const update = document.createElement("span");
        update.className = "entry-update";
        update.textContent = officialNoticeLabel(entry.officialNotice);
        item.append(update);
      }
      descriptions.push(officialNoticeLabel(entry.officialNotice));
    }
    if (descriptions.length) {
      item.title = displayText(`${entry.name}：${descriptions.join(" / ")}`);
      item.setAttribute("aria-label", displayText(`${entry.name}（${descriptions.join("・")}）`));
    }
    return item;
  }

  function appendRosterGroup(target, store, members, renderEntry) {
    if (!members.length) return;
    if (store) {
      const heading = document.createElement("p");
      heading.className = "maid-group-label";
      heading.dataset.store = store.id;
      heading.textContent = store.short;
      target.append(heading);
    }
    const list = document.createElement("ul");
    list.className = "maid-list";
    orderRosterEntries(members, registry.knownNames, kitchenStaff, data.normalOrderBefore)
      .forEach((entry) => list.append(renderEntry(entry)));
    target.append(list);
  }

  function createRecordedRoster({ type, groups, posts = [] }, key, shift) {
    const visiblePosts = posts.filter((post) => {
      const current = observedShift({ posts: [post] }, insights, key, shift, data.observationNameCorrections);
      return [...current.byMaid.keys(), ...current.notices.keys()].some(isVisibleMaid);
    });
    const block = document.createElement("section");
    block.className = groups.some(({ entries }) => entries.some((entry) => isVisibleMaid(entry.name)))
      ? "recorded-roster" : "roster-sources";
    block.dataset.recordType = type;
    block.setAttribute("aria-label", `${key} ${shift}の${type === "recorded" ? "収録記録" : "店舗ごとの案内"}`);
    const details = document.createElement("details");
    details.className = "observation-details";
    details.dataset.stateKey = `${key}|${shift}|observation-details`;
    const summary = document.createElement("summary");
    summary.textContent = "集合ポスト";
    summary.dataset.focusKey = `${key}|${shift}|observation-summary`;
    details.append(summary);
    for (const { store, entries } of groups) {
      const shown = entries.filter((entry) => isVisibleMaid(entry.name));
      if (!shown.length) continue;
      appendRosterGroup(block, store, shown, (entry) => createRosterEntry(entry, key, shift, {
        evidence: entry.officialPlacement ? "official-announced" : entry.personalPlacement ? "personal" : type === "recorded" ? "recorded" : "observed",
        storeId: store.id,
        note: entry.personalPlacement || entry.officialPlacement ? `${store.short}のお給仕予定` : type !== "recorded"
          ? `${store.short}のお給仕投稿で確認`
          : `${shift}は${store.short}にいた記録があります`
      }));
    }
    for (const store of storeList) {
      const sources = visiblePosts.filter((post) => post.storeId === store.id);
      if (!sources.length) continue;
      const sourceList = document.createElement("div");
      sourceList.className = "observation-sources";
      for (const source of sources) {
        sourceList.append(createOfficialPost(source, key, shift, store));
      }
      details.append(sourceList);
    }
    if (visiblePosts.length) {
      details.addEventListener("toggle", () => {
        if (details.open) visiblePosts.forEach(embedOfficialPost);
      });
      block.append(details);
    }
    return block;
  }

  function createUnmatchedNames(entries, key, shift) {
    const block = document.createElement("section");
    block.className = "unmatched-roster";
    block.setAttribute("aria-label", `${key} ${shift}の未発表`);
    const heading = document.createElement("h5");
    heading.className = "unmatched-heading";
    heading.textContent = "未発表";
    block.append(heading);
    appendRosterGroup(block, null, entries, (entry) => createRosterEntry({
      ...entry, trainee: observedTrainee(insights, entry.name, key)
    }, key, shift, {
      evidence: entry.personalNotice?.conflict || entry.officialNotice?.conflict ? "pending" : "scheduled",
      note: entry.personalNotice?.conflict || entry.officialNotice?.conflict
        ? "案内が一致しないため、店舗は保留です。"
        : "公開予定。店舗を確認できる人物ごとの根拠はまだありません。"
    }));
    return block;
  }

  function appendEmptyShift(section, hasEntries) {
    const empty = document.createElement("p");
    section.classList.add("is-empty");
    empty.className = "empty-shift";
    if (hasEntries) {
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
  }

  async function refreshObservedData() {
    if (observationLoading) return;
    observationLoading = true;
    try {
      const response = await window.fetch("data/observed-shifts.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = validateObservations(await response.json());
      const changed = JSON.stringify(next) !== JSON.stringify(observations);
      observations = next;
      if (changed) rerenderSourceUpdate();
    } catch (error) {
      console.error("Observation snapshot load failed", error);
    } finally {
      observationLoading = false;
    }
  }

  function sourceFacts(key, shift) {
    const timing = (value) => workTimingPresentation(value, key, shift);
    return {
      posts: activeObservationPosts(observations).filter((post) => (!key || post.date === key) && (!shift || post.shift === shift))
        .map(({ id, url, date, shift, createdAt, names, notices, storeId }) => ({ id, url, date, shift, createdAt, names, notices, storeId })),
      personal: personalPostsForView(personalShifts, data.personalEventAdditions)
        .filter((post) => (!key || post.date === key) && (!shift ||
          post.events.some((event) => event.shift === shift) ||
          post.links?.some((link) => link.scope === shift || link.scope === "unspecified") ||
          post.workTiming?.facts.some((fact) => fact.shift === shift)))
        .map(({ id, url, date, createdAt, name, authorId, authorScreenName, events, links, workTiming }) => ({
          id, url, date, createdAt, name, authorId, authorScreenName,
          events: events.filter((event) => !shift || event.shift === shift),
          ...(links !== undefined ? { links: links.filter((link) =>
            !shift || link.scope === shift || link.scope === "unspecified") } : {}),
          ...(workTiming !== undefined ? { workTiming: timing(workTiming) } : {})
        })),
      halfMonth: halfMonthSchedules.schedules
        .map((source) => ({
          ...halfMonthProvenance(source),
          ...(source.workTiming !== undefined ? { workTiming: timing(source.workTiming) } : {}),
          days: source.days.filter((day) => (!key || day.date === key) &&
            (!shift || day.shifts.includes(shift))).map((day) => ({
            date: day.date, shifts: day.shifts.filter((item) => !shift || item === shift).slice().sort()
          })).sort((a, b) => a.date.localeCompare(b.date))
        }))
        .filter((source) => !key || source.days.length)
        .sort((a, b) => `${a.name}|${a.period.from}`.localeCompare(`${b.name}|${b.period.from}`))
    };
  }

  let lastSourcePresentation = JSON.stringify(sourceFacts());

  function rerenderSourceUpdate() {
    const presentation = JSON.stringify(sourceFacts());
    if (lastSourcePresentation === presentation) return;
    lastSourcePresentation = presentation;
    syncMaidFilterNames();
    if (elements.dayDialog.open) {
      observationsChangedInDialog = true;
      openDayDialog(state.selectedDate, dialogOrigin, true);
    } else {
      const focused = document.activeElement;
      const focusedDate = focused?.dataset?.date;
      const focusedKey = focused?.dataset?.focusKey;
      const scroll = [elements.calendar.scrollTop, elements.calendar.scrollLeft];
      const pageScroll = [window.scrollX, window.scrollY];
      renderCalendar();
      const target = focusedKey
        ? [...elements.calendar.querySelectorAll("[data-focus-key]")].find((item) => item.dataset.focusKey === focusedKey)
        : focusedDate ? dayButtons.get(focusedDate) : null;
      (target ?? (focusedKey ? elements.calendar : null))?.focus({ preventScroll: true });
      [elements.calendar.scrollTop, elements.calendar.scrollLeft] = scroll;
      if (typeof window.scrollTo === "function") window.scrollTo(...pageScroll);
    }
  }

  async function refreshPersonalData() {
    if (personalLoading) return;
    personalLoading = true;
    try {
      const response = await window.fetch("data/personal-shifts.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = validatePersonalShifts(await response.json());
      const changed = JSON.stringify(next) !== JSON.stringify(personalShifts);
      personalShifts = next;
      if (changed) rerenderSourceUpdate();
    } catch (error) {
      console.error("Personal snapshot load failed", error);
    } finally {
      personalLoading = false;
    }
  }

  async function refreshHalfMonthData() {
    if (halfMonthLoading) return;
    halfMonthLoading = true;
    try {
      const response = await window.fetch("data/half-month-schedules.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = validateHalfMonthSchedules(await response.json(),
        { registry, insights, previous: halfMonthSchedules, personal: personalShifts });
      halfMonthSchedules = next;
      effectiveSchedule = buildEffectiveSchedule(data.schedule, halfMonthSchedules, { registry });
      sourceSchedule = buildEffectiveSchedule(data.schedule, halfMonthSchedules, { registry, includeInactivePlans: true });
      rerenderSourceUpdate();
    } catch (error) {
      console.error("Half-month snapshot load failed", error);
    } finally {
      halfMonthLoading = false;
    }
  }

  function createShiftSection(key, date, shift, showForecast = state.viewMode === "forecast", confirmedOnly = false) {
    const section = document.createElement("section");
    section.className = `shift-section ${shiftDetails[shift].className}`;
    section.setAttribute("aria-label", `${shift}のお給仕`);

    const roster = shiftRoster(key, shift);
    confirmedOnly = confirmedOnly || dayHasPersonStoreEvidence(insights, observations, key, personalShifts, data.personalEventAdditions) ||
      roster.personal.posts.length > 0;
    const partial = roster.observed.posts.length > 0;
    const forecasting = showForecast && !confirmedOnly;
    const { outlook, pins } = forecasting && !partial && !roster.assignment?.recorded
      ? getShiftOutlook(key, shift)
      : { outlook: null, pins: new Map() };
    const title = document.createElement("h4");
    title.className = "shift-title";
    const icon = document.createElement("span");
    icon.className = "shift-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = shiftDetails[shift].icon;
    const label = document.createElement("span");
    label.textContent = shift;
    title.append(icon, label);
    if (outlook && !partial) {
      title.append(createStatusBadge(outlook));
    }
    section.append(title);

    if (partial || roster.assignment?.recorded || confirmedOnly) {
      const type = roster.assignment?.recorded ? "recorded"
        : roster.entries.some((entry) => entry.personalPlacement) ? (partial ? "mixed" : "personal") : "observed";
      const groups = storeList.map((store) => ({
        store,
        entries: roster.assignment?.recorded
          ? roster.entries.filter((entry) => roster.assignment.byMaid.get(entry.name)?.storeId === store.id)
          : roster.entries.filter((entry) => roster.observed.byStore.get(store.id)?.has(entry.name) ||
            (entry.personalPlacement && entry.personalNotice.storeId === store.id) ||
            (entry.officialPlacement && entry.officialNotice.storeId === store.id))
            .map((entry) => ({ ...entry, trainee: roster.observed.byMaid.get(entry.name)?.trainee ?? entry.trainee }))
      }));
      const hasConfirmed = groups.some(({ entries }) => entries.some((entry) => isVisibleMaid(entry.name)));
      if (hasConfirmed || roster.observed.posts.length) {
        section.append(createRecordedRoster({ type, groups, posts: roster.observed.posts }, key, shift));
      }
      const unmatched = roster.entries.filter((entry) => !entry.observed && !entry.personalPlacement && !entry.officialPlacement && isVisibleMaid(entry.name));
      const hasUnmatched = !roster.assignment?.recorded && unmatched.length > 0;
      if (hasUnmatched) section.append(createUnmatchedNames(unmatched, key, shift));
      for (const person of roster.personal.byMaid.values()) {
        if (isVisibleMaid(person.name) && (person.absent || !roster.entries.some((entry) => entry.name === person.name))) {
          section.append(createChangeNotice(person));
        }
      }
      if (!hasConfirmed && !hasUnmatched && !roster.personal.posts.length) appendEmptyShift(section, roster.entries.length > 0);
      return section;
    }
    // 同じ日の昼に誰がどこにいたか。記録があるときだけ、夜の割り振りに使う。
    const movedFrom = earlierShiftPlaces(insights, key, shift);
    // 見込みの計算には予定表の顔ぶれを使う。記録のある日は使われないが、
    // 記録の顔ぶれを入れると「答えを見て予測する」ことになる。
    const postedNames = (effectiveSchedule[key]?.[shift] ?? []).map((entry) => entry.name);
    if (outlook) {
      section.append(createStoreOutlook(outlook, shift, postedNames));
    }

    const allEntries = roster.entries;
    const visible = allEntries.filter((entry) => !entry.observed && isVisibleMaid(entry.name));
    // 割り振りは絞り込みに影響されないよう、その日そのシフトの全員で計算する。
    // 記録のある日は割り振らない。誰がどこにいたかは分かっている。
    const assignment = roster.assignment
      ?? (outlook
        ? getShiftAssignment({
          insights,
          members: postedNames,
          shift,
          outlook,
          pins,
          kitchenStaff,
          // 同じ日の昼に誰がどこにいたか記録があれば、夜の割り振りに使う。
          movedFrom
        })
        : null);
    const evidence = document.createElement("p");
    evidence.className = "shift-evidence";
    evidence.textContent = roster.assignment?.recorded
      ? "人物・店舗とも投稿の記録。掲載のない方の不在を確定するものではありません。"
      : partial
        ? "以下は投稿と未照合の予定です。店舗は推測で、不在・出勤確認ではありません。"
      : outlook?.basis === "actual"
        ? "店舗のみ実績 ／ 人物は公開予定・行き先は推測です。"
        : !(effectiveSchedule[key]?.[shift]?.length > 0)
          ? "お給仕の公開予定・人物の記録は未確認です。休業や不在の確定ではありません。"
        : "公開予定 ／ 店舗の割り振りは推測です。イベント主役は別表示。";
    evidence.classList.add("visually-hidden");
    section.append(evidence);
    // 記録の並びは掲載順で確定しているので、割り振り順に組み直すのは見込みの日だけ。
    const ordered = assignment && !assignment.recorded
      ? sortByAssignedStore({ insights, entries: visible, assignment })
      : visible;
    // キッチンにゃんこは、見込みの日には店の下に置かない。
    //
    // どの店にいるかを当てられない（実測でフロア65.4%に対し58.7%）うえに、
    // 当てる必要もない。開いた店の枠にキッチンが載っているのは90%で、
    // 1店なら1人・2店なら2人と各店に1人ずついるので、「どの店にいるか」は
    // 「どの店が開くか」とほとんど同じことしか言っていない。
    //
    // 見習いと違って、誰が出るかは予定表に載っているので名前は出せる。
    //
    // ただし記念日の主役は所属店に確定する。そこは分かっているので外さない。
    const guessingStores = Boolean(assignment) && !assignment.recorded;
    const looseCook = (entry) =>
      guessingStores &&
      kitchenStaff.has(entry.name) &&
      !assignment.byMaid.get(entry.name)?.pin;
    const entries = ordered.filter((entry) => !looseCook(entry));
    const cooks = ordered.filter(looseCook);
    // 予定表に出ない見習いにゃんこ。誰なのかも、どの店かも分からないので、
    // 店ごとのグループには入れず、シフトの末尾にまとめて置く。
    const guessedTrainees = !partial && isVisibleMaid(TRAINEE_PLACEHOLDER)
      ? expectedTrainees(
        insights,
        shift,
        assignment?.storeIds?.length ?? 0,
        Boolean(assignment?.recorded)
      )
      : 0;

    function createMaidEntry(entry, groupStoreId, hideStore = false) {
      const item = createRosterEntry(entry, key, shift, {
        evidence: "scheduled", storeId: groupStoreId
      });
      const isKitchen = kitchenStaff.has(entry.name);
      const chipData = getMaidStoreOutlook({
        insights,
        name: entry.name,
        shift,
        outlook,
        assignment,
        unpostedMaids
      });
      const timingDescription = workTimingDescription(entry.workTimingNote);
      const titles = [entry.memberReview, timingDescription].filter(Boolean);
      const descriptions = [...titles];

      if (isKitchen) {
        titles.push("キッチンにゃんこ");
        descriptions.push("キッチンにゃんこ");
      }

      // 見習いにゃんこ。判定できた日だけ印を付ける。trainee が null の日は
      // 「まだ判定していない」で、「昇格済み」ではない。そこを混ぜない。
      if (entry.trainee === true) {
        titles.push("見習いにゃんこ");
        descriptions.push("見習いにゃんこ");
      }

      if (entry.featured) {
        titles.unshift(entry.eventLabel);
        descriptions.unshift(`${entry.eventLabel}の主役`);
      }

      item.dataset.evidence = assignment?.recorded ? "recorded"
        : chipData?.basis === "event" ? "event" : "scheduled";

      // 昼の記録を使って夜を組んだ人には、そのことと移り先の実績を書く。
      // 「所属店が開いているのになぜ別の店」は、読み手が実際に持った疑問。
      // 夜も記録がある日は書かない。どこにいたか分かっているので確率の出番がない。
      const cameFrom = assignment?.recorded ? null : movedFrom?.get(entry.name);
      const placedAt = assignment?.byMaid?.get(entry.name)?.storeId;
      if (cameFrom && placedAt) {
        const move = sameDayMoveNote(insights, entry.name, cameFrom, placedAt);
        if (move) {
          item.classList.add("is-moved");
          titles.push(move);
          descriptions.push(move);
        }
      }

      if (chipData && !hideStore) {
        // 見出しがその店を名乗っているなら、チップは割合だけになる。割合は表に
        // 出さない方針なので、そこでは何も足さない。数字はこの行の title に残る。
        const showStore = groupStoreId !== chipData.storeId;
        if (showStore) {
          item.append(createMaidStoreChip(chipData, true));
        }
        titles.push(chipData.title);
        descriptions.push(chipData.srText);
      }

      if (titles.length > 0) {
        item.title = displayText(`${entry.name}：${titles.join(" / ")}`);
        item.setAttribute("aria-label", displayText(`${entry.name}（${descriptions.join("・")}）`));
      }
      return item;
    }

    function appendKitchen(target, members, storeIds) {
      if (members.length === 0) {
        return;
      }
      const storeCount = storeIds.length;
      // 誰が出るかは分かっている。分からないのは、どの店にいるか。
      const note =
        `キッチンにゃんこです。${storeCount > 1 ? `開く${storeCount}店に1人ずつ入るのがふだんの形で、` : ""}` +
        "どの店かは配属とも関係なく決まるため、店ごとの一覧には入れていません";
      const heading = document.createElement("p");
      // .maid-group-label は「かならず店を名乗る」約束なので、そこには入れない。
      heading.className = "maid-kitchen-label";
      const label = document.createElement("span");
      label.textContent = "キッチン";
      const count = document.createElement("span");
      count.className = "maid-group-count";
      count.textContent = `${members.length}人`;
      heading.append(label, count);
      // 見習いと同じ理由で、開く店を並べる。位置を答えにしない。
      const where = candidateStoresLabel(insights, storeIds);
      if (where) {
        const span = document.createElement("span");
        span.className = "maid-group-where";
        span.textContent = where;
        heading.append(span);
      }
      heading.title = note;
      const list = document.createElement("ul");
      // 店ごとの一覧（.maid-list）は「見出しと1対1」で店を名乗る約束なので混ぜない。
      list.className = "maid-kitchen-list";
      orderRosterEntries(members, registry.knownNames, kitchenStaff, data.normalOrderBefore).forEach((entry) => {
        // 店を名乗らないと決めた以上、チップで店を出しては辻褄が合わない。
        const item = createMaidEntry(entry, null, true);
        const detail = [note, workTimingDescription(entry.workTimingNote)].filter(Boolean).join("。");
        item.setAttribute(
          "aria-label",
          displayText(`${entry.name}（${detail}）`)
        );
        item.title = displayText(`${entry.name}：${detail}`);
        list.append(item);
      });
      target.append(heading, list);
    }

    if (entries.length > 0 || cooks.length > 0) {
      const groups = groupByAssignedStore({ insights, entries, assignment });

      groups.forEach(({ storeId, entries: members }) => {
        const store = storeId ? storeList.find((candidate) => candidate.id === storeId) : null;
        appendRosterGroup(section, store, members, (entry) => createMaidEntry(entry, storeId));
      });

      appendTraineeGuesses(section, shift, guessedTrainees, assignment?.storeIds ?? []);
      appendKitchen(section, cooks, assignment?.storeIds ?? []);
      return section;
    }

    if (guessedTrainees > 0) {
      appendTraineeGuesses(section, shift, guessedTrainees, assignment?.storeIds ?? []);
      return section;
    }

    appendEmptyShift(section, allEntries.length > 0);
    return section;
  }

  // 予定表に出ない見習いにゃんこ。店ごとのグループには入れない。
  // どの店にいるか読めないのに見出しの下に置くと、その店にいると読まれる。
  // The groups express different uncertainty, not a ranking of confidence.
  //
  // 見出しを必ず付ける。クラスを分けるだけでは足りない。見出しが無いと、直前の店の
  // <ul> にそのまま続いて見え、店は s1→s4 の順に並ぶので、いつも番号のいちばん
  // 大きい店にぶら下がって読める。「どの店か分からない」と言うつもりが「4号店に
  // いる」と読める形になっていた。
  // 「どこかにいる」は当たり前で、知りたいのは「どこにいそうか」。
  //
  // Store averages are not calibrated probabilities of this shift's placement.
  //
  // 出せるのは、測った結果そのもの。ただし**こちら側の話として書く**。
  // 「どれも同じくらい」は店についての主張で、それは嘘になる（byStore は昼で
  // 0.243〜0.503 と倍以上ひらいている）。当てられないのは私たちのほうで、
  // 店が横並びだからではない。同じ理由で、人の的中率も「その人が読めない」では
  // なく「私たちが当てられない」と書いている。
  function candidateStoresLabel(insights, storeIds) {
    const stores = storesOf(insights).filter((store) => storeIds.includes(store.id));
    if (stores.length === 0) {
      return null;
    }
    const names = stores.map(compactStoreLabel);
    if (names.length === 1) {
      return `${names[0]}が営業する予測の場合・店舗未定`;
    }
    return `${names.join("・")}が営業する予測の場合・店舗は特定できません`;
  }

  function appendTraineeGuesses(section, shift, count, storeIds) {
    if (!(count > 0)) {
      return;
    }
    const note = traineeGuessNote(insights, shift, storeIds.length);
    const heading = document.createElement("p");
    // .maid-group-label は「かならず店を名乗る」約束なので、そこには入れない。
    heading.className = "maid-trainee-label";
    const label = document.createElement("span");
    label.textContent = "見習い";
    const tally = document.createElement("span");
    tally.className = "maid-group-count";
    // 予定表から数えた人数ではなく、実績から見込んだ数。「ほど」で言い分ける。
    tally.textContent = `${count}人ほど`;
    heading.append(label, tally);
    const where = candidateStoresLabel(insights, storeIds);
    if (where) {
      const span = document.createElement("span");
      span.className = "maid-group-where";
      span.textContent = where;
      heading.append(span);
    }
    heading.title = note;
    const list = document.createElement("ul");
    // 店ごとの一覧（.maid-list）とは別のクラスにする。あちらは「見出しと1対1」を
    // 保つ約束があり、店を名乗らないこの一覧を混ぜるとその約束が崩れる。
    list.className = "maid-trainee-list";
    for (let index = 0; index < count; index += 1) {
      const item = document.createElement("li");
      item.className = "maid-entry is-trainee is-trainee-guess";
      const nameLabel = document.createElement("span");
      nameLabel.className = "maid-name";
      nameLabel.textContent = TRAINEE_PLACEHOLDER;
      const mark = document.createElement("span");
      mark.className = "maid-trainee";
      mark.textContent = "🔰";
      mark.setAttribute("aria-hidden", "true");
      item.append(nameLabel, mark);
      item.title = note;
      item.setAttribute("aria-label", note);
      list.append(item);
    }
    section.append(heading, list);
  }

  function createDayCell(date, isFirstRenderedDate) {
    const key = dateKey(date);
    const day = document.createElement("article");
    day.className = "calendar-day is-detail-day";
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

  const dayButtons = new Map();
  let dialogOrigin = null;
  let previousOverflow = "";
  let observationsChangedInDialog = false;

  function createEventArt(event, key) {
    const figure = document.createElement("span");
    figure.className = "event-art";
    figure.dataset.maid = event.name;
    const image = document.createElement("img");
    const configured = eventImageFor(data, key, event.name);
    image.className = "event-image";
    image.alt = displayText(configured.alt);
    image.width = 160;
    image.height = 160;
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    let fallback = false;
    image.addEventListener("error", () => {
      console.warn(`Event image unavailable: ${event.name} (${key})`);
      if (!fallback && configured.src !== DEFAULT_EVENT_IMAGE.src) {
        fallback = true;
        image.src = DEFAULT_EVENT_IMAGE.src;
        image.alt = DEFAULT_EVENT_IMAGE.alt;
      } else {
        image.hidden = true;
        figure.classList.add("image-unavailable");
      }
    });
    image.src = configured.src;
    const name = document.createElement("span");
    name.className = "event-art-name";
    name.textContent = displayName(event.name);
    name.dataset.name = event.name;
    const label = document.createElement("span");
    label.className = "event-art-label";
    label.textContent = event.labels.join("・");
    figure.append(image, name, label);
    return figure;
  }

  function closeDayDialog() {
    if (elements.dayDialog.open) {
      elements.dayDialog.close();
      finishDayDialog();
    }
  }

  function finishDayDialog() {
    if (!dialogOrigin || elements.dayDialog.open) return;
    document.body.style.overflow = previousOverflow;
    const origin = dialogOrigin;
    dialogOrigin = null;
    if (observationsChangedInDialog) {
      observationsChangedInDialog = false;
      renderCalendar();
      dayButtons.get(origin.dataset.date)?.focus({ preventScroll: true });
    } else if (origin.isConnected) {
      origin.focus({ preventScroll: true });
    }
  }

  function openDayDialog(key, origin, preserveInteraction = false) {
    if (!isInDateRange(key)) {
      elements.resultSummary.textContent = "選択した期間の外です。表示条件で期間を変更してください。";
      return;
    }
    effectiveSchedule = buildEffectiveSchedule(data.schedule, halfMonthSchedules, { registry });
    sourceSchedule = buildEffectiveSchedule(data.schedule, halfMonthSchedules, { registry, includeInactivePlans: true });
    const focused = preserveInteraction ? document.activeElement : null;
    const focusedId = focused?.id;
    const focusedKey = focused?.dataset?.focusKey;
    const scroll = elements.dialogContent.scrollTop;
    const expanded = preserveInteraction
      ? new Set([...elements.dialogContent.querySelectorAll("details[data-state-key]")]
        .filter((details) => details.open).map((details) => details.dataset.stateKey))
      : new Set();
    const date = new Date(`${key}T00:00:00`);
    state.selectedDate = key;
    for (const [dayKey, button] of dayButtons) {
      button.classList.remove("is-selected");
      if (dayKey === key) button.classList.add("is-selected");
    }
    elements.dialogTitle.textContent =
      `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日（${weekdays[date.getDay()]}）`;
    const events = dayEvents({ ...data, schedule: effectiveSchedule }, insights, key).filter((event) => isVisibleMaid(event.name));
    elements.dialogEvents.textContent = events.length > 0
      ? events.map((event) => `${displayName(event.name)} ${event.labels.join("・")}`).join(" ／ ")
      : "";
    const confirmedOnly = dayHasPersonStoreEvidence(insights, observations, key, personalShifts, data.personalEventAdditions);
    shifts.forEach((shift, index) => {
      const renderKey = JSON.stringify({
        key, shift, confirmedOnly, today: tokyoToday(), selected: [...state.selectedMaids], kitchen: state.hideKitchen,
        schedule: effectiveSchedule[key]?.[shift], recorded: insights.actualRoster?.[key]?.[shift],
        stores: insights.actual?.[key]?.[shift], displayNames: data.displayNames,
        ...sourceFacts(key, shift)
      });
      const previous = elements.dialogContent.children[index];
      if (previous?.dataset.renderKey === renderKey) return;
      const section = createShiftSection(key, date, shift, true, confirmedOnly);
      section.id = index === 0 ? "dialog-day" : "dialog-night";
      section.tabIndex = -1;
      section.dataset.renderKey = renderKey;
      if (previous) elements.dialogContent.replaceChild(section, previous);
      else elements.dialogContent.append(section);
    });
    elements.dialogContent.scrollTop = 0;
    if (!elements.dayDialog.open) {
      dialogOrigin = origin;
      previousOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      elements.dayDialog.showModal();
    }
    if (preserveInteraction) {
      for (const details of elements.dialogContent.querySelectorAll("details[data-state-key]")) {
        details.open = expanded.has(details.dataset.stateKey);
      }
      const target = focused?.isConnected ? focused
        : focusedId ? document.getElementById(focusedId)
        : focusedKey ? [...elements.dialogContent.querySelectorAll("[data-focus-key]")]
          .find((element) => element.dataset.focusKey === focusedKey) : null;
      (target && elements.dayDialog.contains(target) ? target : elements.dialogContent)
        .focus({ preventScroll: true });
      elements.dialogContent.scrollTop = scroll;
    } else {
      elements.closeDialog.focus({ preventScroll: true });
    }
  }

  function renderMonthGrid(year, monthIndex) {
    const grid = document.createElement("div");
    grid.className = "calendar-grid";
    grid.setAttribute("role", "table");
    grid.setAttribute("aria-labelledby", "month-title");
    const header = document.createElement("div");
    header.className = "calendar-row";
    header.setAttribute("role", "row");
    const english = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    weekdays.forEach((label, index) => {
      const cell = document.createElement("div");
      cell.className = "weekday";
      cell.setAttribute("role", "columnheader");
      cell.setAttribute("aria-label", `${label}曜日`);
      cell.textContent = english[index];
      header.append(cell);
    });
    grid.append(header);
    dayButtons.clear();
    let row;
    let eventCount = 0;
    let visibleCount = 0;
    monthCells(year, monthIndex).forEach((date, index) => {
      if (index % 7 === 0) {
        row = document.createElement("div");
        row.className = "calendar-row";
        row.setAttribute("role", "row");
        grid.append(row);
      }
      const cell = document.createElement("div");
      cell.className = "month-cell";
      cell.setAttribute("role", "cell");
      if (!date) {
        cell.classList.add("is-blank");
        row.append(cell);
        return;
      }
      const key = dateKey(date);
      const inRange = isInDateRange(key);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "day-button";
      button.dataset.date = key;
      button.disabled = !inRange;
      button.setAttribute("aria-haspopup", "dialog");
      const number = document.createElement("span");
      number.className = "day-number";
      number.textContent = String(date.getDate());
      button.append(number);
      if (date.getDay() === 0) cell.classList.add("is-sunday");
      if (date.getDay() === 6) cell.classList.add("is-saturday");
      if (key === tokyoToday()) {
        button.classList.add("is-today");
        button.setAttribute("aria-current", "date");
      }
      if (key === state.selectedDate) button.classList.add("is-selected");
      const events = inRange
        ? dayEvents({ ...data, schedule: effectiveSchedule }, insights, key).filter((event) => isVisibleMaid(event.name))
        : [];
      if (inRange) visibleCount += 1;
      if (events.length > 0) {
        eventCount += 1;
        const art = document.createElement("span");
        art.className = "day-events";
        if (events.length > 1) art.classList.add("has-multiple");
        events.forEach((event) => art.append(createEventArt(event, key)));
        button.append(art);
      } else if (inRange) {
        const hasMembers = shifts.some((shift) => filteredEntries(key, shift).length > 0);
        const hint = document.createElement("span");
        hint.className = hasMembers ? "day-hint has-schedule" : "day-hint";
        const hasInformation = shifts.some((shift) => shiftRoster(key, shift).entries.length > 0);
        const hasObserved = shifts.some((shift) =>
          observedShift(observations, insights, key, shift, data.observationNameCorrections).posts.length > 0);
        const hasPersonal = shifts.some((shift) =>
          [...shiftRoster(key, shift).personal.byMaid.keys()].some(isVisibleMaid));
        hint.textContent = hasMembers ? "お給仕" : hasPersonal ? "変更あり" : hasInformation ? "該当なし" : "未確認";
        if (hasObserved) hint.classList.add("is-observed");
        button.append(hint);
      }
      const eventLabel = events.map((event) => `${displayName(event.name)} ${event.labels.join("・")}`).join("、");
      button.setAttribute("aria-label",
        `${year}年${monthIndex + 1}月${date.getDate()}日（${weekdays[date.getDay()]}）` +
        (inRange ? ` ${eventLabel} 昼と夜のお給仕詳細を開く` : " 表示期間外"));
      button.addEventListener("click", () => openDayDialog(key, button));
      dayButtons.set(key, button);
      cell.append(button);
      row.append(cell);
    });
    elements.resultSummary.textContent =
      `${visibleCount}日を表示・イベント ${eventCount}日` +
      (visibleMaidCount() !== filterNames.length ? `・${visibleMaidCount()}名で絞り込み中` : "") +
      (state.customRange ? "（指定期間のみ開けます）" : "");
    elements.calendar.replaceChildren(grid);
  }

  function renderCalendar() {
    effectiveSchedule = buildEffectiveSchedule(data.schedule, halfMonthSchedules, { registry });
    sourceSchedule = buildEffectiveSchedule(data.schedule, halfMonthSchedules, { registry, includeInactivePlans: true });
    syncMaidFilterNames();
    closeDayDialog();
    const year = state.visibleMonth.getFullYear();
    const monthIndex = state.visibleMonth.getMonth();
    elements.monthTitle.textContent = `${year}年${monthIndex + 1}月`;
    elements.monthYear.textContent = String(year);
    elements.monthNumber.textContent = String(monthIndex + 1);
    if (state.viewMode === "calendar") {
      renderMonthGrid(year, monthIndex);
      return;
    }
    if (state.viewMode === "maid") {
      renderMaidView();
      return;
    }
    const firstDay = new Date(year, monthIndex, 1);
    const visibleDates = getVisibleMonthDates(
      year,
      monthIndex,
      state.dateFrom,
      state.dateTo
    );
    const grid = document.createElement("div");
    grid.className = "detail-grid";
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

    const displayedCount = visibleDates.reduce((total, date) => {
      const key = dateKey(date);
      // 記録のある日は記録の顔ぶれを数える。予定表を数えると、画面に出ている
      // 見習いにゃんこが抜け、お休みだった方が入って、表示と合わなくなる。
      return total + shifts.reduce(
        (shiftTotal, shift) => shiftTotal + filteredEntries(key, shift).length,
        0
      );
    }, 0);

    elements.monthTitle.textContent = `${year}年${monthIndex + 1}月`;
    elements.resultSummary.textContent =
      `${visibleMaidCount()}名を選択中・${displayedCount}件のお給仕を表示`;
    elements.calendar.replaceChildren(grid);
  }


  // 人 → 日付 → 店 の軸。店ごとの画面と同じ割り振りを引くので、両者は食い違わない。
  function renderMaidView() {
    const year = state.visibleMonth.getFullYear();
    const monthIndex = state.visibleMonth.getMonth();
    const dates = getVisibleMonthDates(year, monthIndex, state.dateFrom, state.dateTo).map(dateKey);
    const wrapper = document.createElement("div");
    wrapper.className = "maid-view";

    const shiftCache = new Map();
    const resolve = (key, shift) => {
      const id = `${key}|${shift}`;
      if (!shiftCache.has(id)) {
        const roster = shiftRoster(key, shift);
        const confirmedOnly = dayHasPersonStoreEvidence(insights, observations, key, personalShifts, data.personalEventAdditions) ||
          roster.personal.posts.length > 0;
        const { outlook, pins } = confirmedOnly ? { outlook: null, pins: new Map() } : getShiftOutlook(key, shift);
        // 記録のある日は記録の割り振りを使う。カレンダーと同じものを引かないと、
        // 同じ人・同じ日で別の店を出してしまう。
        const members = (effectiveSchedule[key]?.[shift] ?? []).map((entry) => entry.name);
        shiftCache.set(id, {
          outlook,
          observed: roster.observed,
          personal: roster.personal,
          entries: roster.entries,
          confirmedOnly,
          members: [...new Set([...roster.entries.map((entry) => entry.name), ...roster.personal.byMaid.keys()])],
          assignment: roster.assignment
            ?? (outlook
              ? getShiftAssignment({
                insights,
                members,
                shift,
                outlook,
                pins,
                kitchenStaff,
                movedFrom: earlierShiftPlaces(insights, key, shift)
              })
              : null)
        });
      }
      return shiftCache.get(id);
    };

    // 記録に出てくる方は roster に載っていないことがある（見習いなど）。
    // カレンダーに出しているなら、この画面からも引けないと辻褄が合わない。
    const seen = new Set(data.roster);
    const extra = [];
    for (const key of dates) {
      for (const shift of shifts) {
        for (const name of resolve(key, shift).members) {
          if (!seen.has(name)) {
            seen.add(name);
            extra.push(name);
          }
        }
      }
    }
    const shown = [...data.roster, ...extra].filter((name) => isVisibleMaid(name));
    let stopCount = 0;

    shown.forEach((name) => {
      const plan = maidItinerary({
        schedule: effectiveSchedule,
        name,
        dates,
        shifts,
        resolve,
        kitchenStaff
      });
      if (plan.stops.length === 0 && plan.changes.length === 0) {
        return;
      }
      stopCount += plan.stops.length;
      wrapper.append(createMaidPlan(plan));
    });

    if (wrapper.childElementCount === 0) {
      const empty = document.createElement("p");
      empty.className = "calendar-empty";
      empty.setAttribute("aria-live", "polite");
      empty.textContent = "この期間に予定のあるメイドさんがいません。";
      wrapper.append(empty);
    }

    elements.monthTitle.textContent = `${year}年${monthIndex + 1}月`;
    elements.resultSummary.textContent =
      `${visibleMaidCount()}名を選択中・${stopCount}件のお給仕を表示`;
    elements.calendar.replaceChildren(wrapper);
  }

  function createMaidPlan(plan) {
    const block = document.createElement("section");
    block.className = "maid-plan";
    block.dataset.name = plan.name;
    block.setAttribute("aria-label", `${displayName(plan.name)}のお給仕`);

    const heading = document.createElement("h3");
    heading.className = "maid-plan-name";
    const profileUrl = registry.profileUrl(plan.name);
    const nameLabel = document.createElement(profileUrl ? "a" : "span");
    nameLabel.className = "maid-name";
    nameLabel.textContent = displayName(plan.name);
    nameLabel.dataset.name = plan.name;
    nameLabel.dataset.focusKey = `${plan.name}|profile`;
    if (profileUrl) {
      nameLabel.href = profileUrl;
      nameLabel.target = "_blank";
      nameLabel.rel = "noopener noreferrer";
      nameLabel.title = `${displayName(plan.name)}のXを開く`;
    }
    const count = document.createElement("span");
    count.className = "maid-plan-count";
    count.textContent = plan.stops.length ? `${plan.stops.length}件` : "変更案内";
    heading.append(nameLabel, count);
    if (kitchenStaff.has(plan.name)) {
      const cook = document.createElement("span");
      cook.className = "maid-plan-cook";
      cook.textContent = "キッチン";
      cook.title = "キッチンにゃんこは配属と関係なく4店を回ります";
      heading.append(cook);
    }
    block.append(heading);

    const note = document.createElement("p");
    note.className = "maid-plan-note";
    note.textContent = maidPlanCaveat(plan);
    block.append(note);

    const list = document.createElement("ol");
    list.className = "maid-plan-stops";
    plan.stops.forEach((stop) => list.append(createMaidStop(stop, plan.name)));
    block.append(list);
    for (const change of plan.changes) {
      block.append(createChangeNotice(change.personalNotice, `${change.dateKey} ${change.shift} `));
    }
    return block;
  }

  function createMaidStop(stop, name) {
    const item = document.createElement("li");
    item.className = `maid-plan-stop is-${stop.state ?? "unknown"}`;
    item.dataset.date = stop.dateKey;
    item.dataset.shift = stop.shift;
    const when = createRosterPostLabel(name, stop.dateKey, stop.shift);
    when.className = "maid-plan-when";
    when.dataset.focusKey = `${stop.dateKey}|${stop.shift}|${name}|when`;
    const [, month, date] = stop.dateKey.split("-").map(Number);
    // 曜日はカレンダーと同じ書き方で添える。「9/3」だけでは何曜日か分からない。
    const weekday = weekdays[new Date(`${stop.dateKey}T00:00:00`).getDay()];
    when.textContent = `${month}/${date}(${weekday}) ${stop.shift}`;
    if (when.href) {
      when.setAttribute("aria-label", `${when.textContent} ${when.title}`);
    }
    const where = document.createElement("span");
    where.className = "maid-plan-where";
    where.dataset.store = stop.storeId ?? "";
    where.textContent = stop.observed
      ? stop.storeIds.map((id) => storeShort(insights, id)).join("・")
      : stop.storeId ? storeShort(insights, stop.storeId) : "未発表";
    item.append(when, where);
    appendWorkTiming(item, stop.workTimingNote);
    const evidence = document.createElement("span");
    evidence.className = "maid-plan-evidence";
    evidence.textContent = stop.observed ? "記録" : stop.recorded ? "実績"
      : stop.officialNotice ? officialNoticeLabel(stop.officialNotice)
      : stop.personal ? personalNoticeLabel(stop.personalNotice) : stop.personalNotice?.conflict ? "案内が不一致・保留"
        : stop.personalNotice ? personalNoticeLabel(stop.personalNotice)
        : stop.host ? "イベント主役の予定" : stop.confirmedOnly ? "公開予定・未発表"
          : stop.forecast ? "未照合の予定・店舗推測" : "公開予定・店舗未確認";
    item.dataset.evidence = stop.observed ? "observed" : stop.recorded ? "recorded"
      : stop.officialNotice ? (stop.officialNotice.conflict ? "pending" : "official-announced")
      : stop.personal ? "personal" : stop.personalNotice?.conflict ? "pending" : stop.host ? "event" : "scheduled";
    item.append(evidence);
    if (stop.memberReview) {
      const review = document.createElement("span");
      review.className = "entry-update";
      review.textContent = "要確認";
      item.append(review);
    }
    for (const post of stop.sourcePosts) {
      const link = createObservationLink(post);
      link.dataset.focusKey = `${stop.dateKey}|${stop.shift}|${name}|official|${post.id}`;
      item.append(link);
    }
    // 見習いにゃんこ。判定できた日だけ印を付ける。null は「まだ判定していない」。
    if (stop.trainee === true) {
      const mark = document.createElement("span");
      mark.className = "maid-trainee";
      mark.textContent = "🔰";
      mark.setAttribute("aria-hidden", "true");
      item.append(mark);
    }
    // 割合は表に出さないが、店ごとの画面と同じく HTML には残す。
    // 順位付けには予定表の顔ぶれも入っているので、この数字だけでは置いた理由に
    // ならない。根拠を確かめたいときの手がかりとしてだけ持たせる。
    if (typeof stop.openRate === "number") {
      const rate = document.createElement("span");
      rate.className = "maid-plan-rate";
      rate.textContent = toPercent(stop.openRate);
      item.append(rate);
    }
    if (stop.eventLabel) {
      const event = document.createElement("span");
      event.className = "maid-plan-event";
      event.textContent = stop.eventLabel;
      item.append(event);
    }
    const timingDescription = workTimingDescription(stop.workTimingNote);
    const explanation = stopExplanation(stop) + (timingDescription ? `${timingDescription}。` : "") + (stop.memberReview ?? "");
    if (when.href && timingDescription) {
      when.title += `。${timingDescription}`;
      when.setAttribute("aria-label", `${when.textContent} ${when.title}`);
    }
    item.title = explanation;
    // 読み上げは aria-label に一本化する。同じ文を隠し要素にも置くと二度読まれる。
    item.setAttribute("aria-label", `${when.textContent}は${explanation}`);
    return item;
  }

  // 縦に並べると外れが見える軸なので、先に「全部は当たりません」と言っておく。
  function maidPlanCaveat(plan) {
    const recorded = plan.stops.filter((stop) => stop.recorded).length;
    const hosts = plan.stops.filter((stop) => !stop.recorded && stop.host).length;
    const observed = plan.stops.filter((stop) => stop.observed).length;
    const personal = plan.stops.filter((stop) => stop.personal || stop.officialNotice?.storeId).length;
    const pending = plan.stops.filter((stop) => !stop.settled && !stop.forecast).length;
    const parts = [];
    if (recorded > 0) parts.push(`${recorded}件は人物の実績`);
    if (hosts > 0) parts.push(`${hosts}件はイベント主役の予定`);
    if (observed > 0) parts.push(`実績のうち${observed}件は自動収集した投稿による記録です。未照合の予定は不在の証拠ではありません`);
    if (personal > 0) parts.push(`${personal}件はお給仕予定`);
    if (pending > 0) parts.push(`${pending}件は未発表・保留`);
    if (plan.changes.length > 0) parts.push("取消などの変更あり");
    if (plan.guesses > 0) {
      parts.push(
        `${recorded + hosts > 0 ? "残り" : ""}${plan.guesses}件の行き先は推測です`
      );
    }
    // 的中はその人ごとに実測してある。全体値で代用すると、当たりにくい方を
    // 実際より当たるように、当たりやすい方を実際より当たらないように書くことになる。
    const confidence = itineraryConfidence(insights, plan.guesses, plan.name);
    const cook = kitchenStaff.has(plan.name)
      ? "キッチンにゃんこは配属と関係なく4店を回るので、とくに当てにくくなります。"
      : "";
    if (confidence) {
      // 同じ割合を「1件あたり」と「実測」で2回書かない。実測のほうに寄せて、
      // そこから積み上げた結果を続ける。
      const measured =
        `${cook}この方の行き先は過去${confidence.samples}件を試して` +
        `${toPercent(confidence.perStop)}当てられています（実際の開店集合を知る条件付き・個人別の評価）`;
      parts.push(
        plan.guesses === 1
          ? measured
          : `${measured}。各回が独立で同じ確率と仮定した計算例では、` +
            `${plan.guesses}件すべて当たるのは${toPercent(confidence.allRight)}です`
      );
      parts.push("この一覧全体や、店舗予測・定員調整を含む本番の精度ではありません");
    } else if (plan.guesses > 0) {
      parts.push(maidAccuracyNote(insights, plan.name, kitchenStaff.has(plan.name)));
    }
    return `${parts.join("。")}。`;
  }

  // 見込みの日に、その店の営業率だけを数字で出さない。順位付けには予定表の顔ぶれも
  // 入っているので、「4号店が開くのは15%」と書くと、置いた理由と食い違って見える。
  // 表に出せるのは「開くと見た店のひとつ」までで、確からしさは強弱で伝える。
  function stopExplanation(stop) {
    const trainee = stop.trainee === true ? "見習いにゃんことして" : "";
    if (stop.officialNotice) {
      return `${stop.storeId ? `${storeShort(insights, stop.storeId)}の` : ""}${officialNoticeLabel(stop.officialNotice)}。`;
    }
    if (stop.personalNotice && !stop.observed && !stop.recorded) {
      return `${stop.storeId ? `${storeShort(insights, stop.storeId)}の` : ""}お給仕予定。` +
        (personalNoticeLabel(stop.personalNotice) === "お給仕予定" ? "" : `${personalNoticeLabel(stop.personalNotice)}。`);
    }
    if (!stop.storeId) {
      return stop.kitchen
        ? "キッチンにゃんこは開く店に1人ずつ入りますが、どの店かは配属とも関係なく決まるので、ここでは言えません。"
        : "どこへ立つかは、まだ何も言えません。";
    }
    const where = storeShort(insights, stop.storeId);
    if (stop.observed) {
      return `${stop.storeIds.map((id) => storeShort(insights, id)).join("・")}の公式のお給仕投稿で確認しています。` +
        "確認できた投稿の範囲の情報で、この時間帯の全員・全店舗を網羅するものではありません。";
    }
    // 過ぎた日と、これからの日を混ぜない。記録があるのは過ぎた日だけ。
    if (stop.recorded) {
      return `${trainee}${where}にいた記録があります。`;
    }
    if (stop.host) {
      return `${stop.eventLabel}の主役なので、所属店の${where}にいます。`;
    }
    const strength =
      stop.state === "unlikely"
        ? `${where}は開くと見た店のひとつですが、確からしさは低めです`
        : `${where}は開くと見た店です`;
    return `${strength}。公開予定からの割り振りで、この方がいた記録ではありません。変わることがあります。`;
  }

  function setViewMode(mode) {
    if (!VIEW_MODES[mode] || (mode === "forecast" && !hasInsights)) {
      return;
    }
    state.viewMode = mode;
    storeMode(mode);
    syncViewMode();
    renderCalendar();
  }

  function syncViewMode() {
    elements.modeInputs.forEach((input) => {
      input.checked = input.value === state.viewMode;
      input.disabled = input.value === "forecast" && !hasInsights;
    });
    if (elements.modeHelp) {
      elements.modeHelp.textContent = VIEW_MODES[state.viewMode].help;
    }
  }

  function visibleMaidCount() {
    return filterNames.filter((name) => isVisibleMaid(name)).length;
  }

  function updateMaidFilterSummary() {
    elements.maidFilterSummary.textContent =
      `${visibleMaidCount()}/${filterNames.length}名を表示`;
  }

  function syncMaidFilterNames() {
    const dates = getVisibleMonthDates(state.visibleMonth.getFullYear(), state.visibleMonth.getMonth(),
      state.dateFrom, state.dateTo).map(dateKey);
    const next = dates.length ? memberFilterNames(registry, {
      schedule: effectiveSchedule, insights, observations, personal: personalShifts,
      dateFrom: dates[0], dateTo: dates.at(-1), nameCorrections: data.observationNameCorrections
    }) : [...registry.activeNames];
    if (JSON.stringify(next) === JSON.stringify(filterNames)) return;
    const all = filterNames.length > 0 && filterNames.every((name) => state.selectedMaids.has(name));
    if (all) next.forEach((name) => state.selectedMaids.add(name));
    filterNames = next;
    rosterNames = new Set(next);
    renderMaidFilters();
  }

  function renderMaidFilters() {
    const fragment = document.createDocumentFragment();

    filterNames.forEach((name, index) => {
      const label = document.createElement("div");
      label.className = "checkbox-label";
      label.dataset.name = name;
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

      const text = document.createElement("label");
      text.setAttribute("for", checkbox.id);
      text.textContent = displayName(name);
      label.append(checkbox, text);

      if (kitchenStaff.has(name)) {
        label.classList.add("is-kitchen");
        checkbox.disabled = state.hideKitchen;
        if (state.hideKitchen) {
          label.classList.add("is-muted");
        }
        const badge = document.createElement("span");
        badge.className = "kitchen-badge";
        badge.textContent = "🍳";
        badge.title = `${displayName(name)}はキッチンにゃんこです`;
        badge.setAttribute("role", "img");
        badge.setAttribute("aria-label", "キッチンにゃんこ");
        label.append(badge);
      }

      const member = registry.member(name);
      if (!member || member.membership !== "active") {
        const history = document.createElement("span");
        history.className = "entry-update";
        history.textContent = "履歴";
        history.title = member ? "現在の在籍一覧ではなく、この期間の記録・予定に掲載されています。" : "本人情報は未確認です。保存済みの名前で履歴を表示しています。";
        label.append(history);
      }
      const profileUrl = registry.profileUrl(name);
      if (profileUrl) {
        const profile = document.createElement("a");
        profile.href = profileUrl;
        profile.target = "_blank";
        profile.rel = "noopener noreferrer";
        profile.textContent = "X";
        profile.setAttribute("aria-label", `${displayName(name)}のXを開く`);
        label.append(profile);
      }

      fragment.append(label);
    });
    elements.maidCheckboxes.replaceChildren(fragment);
    updateMaidFilterSummary();
  }

  function setAllMaids(selected) {
    state.selectedMaids = selected ? new Set(filterNames) : new Set();
    renderMaidFilters();
    renderCalendar();
  }

  function setVisibleMonth(offset) {
    state.visibleMonth = new Date(
      state.visibleMonth.getFullYear(),
      state.visibleMonth.getMonth() + offset,
      1
    );
    if (!state.customRange) syncMonthRange();
    renderCalendar();
  }

  function syncMonthRange() {
    const year = state.visibleMonth.getFullYear();
    const month = state.visibleMonth.getMonth();
    state.dateFrom = dateKey(new Date(year, month, 1));
    state.dateTo = dateKey(new Date(year, month + 1, 0));
    elements.dateFrom.value = state.dateFrom;
    elements.dateTo.value = state.dateTo;
  }

  function syncDateRange(changedField) {
    state.customRange = true;
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
    state.selectedMaids = new Set([...registry.knownNames, ...registry.unresolvedNames]);
    state.dateFrom = resetDefaults.dateFrom;
    state.dateTo = resetDefaults.dateTo;
    state.customRange = false;
    state.selectedDate = null;
    state.hideKitchen = false;
    elements.hideKitchen.checked = false;
    state.viewMode = "calendar";
    storeMode(state.viewMode);
    elements.dateFrom.value = state.dateFrom;
    elements.dateTo.value = state.dateTo;
    syncViewMode();
    renderMaidFilters();
    renderCalendar();
  }

  elements.modeInputs.forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) {
        setViewMode(input.value);
      }
    });
  });

  elements.previousMonth.addEventListener("click", () => setVisibleMonth(-1));
  elements.nextMonth.addEventListener("click", () => setVisibleMonth(1));
  elements.currentMonth.addEventListener("click", () => {
    const today = getTokyoDateDefaults();
    state.visibleMonth = new Date(today.year, today.month - 1, 1);
    if (!state.customRange) syncMonthRange();
    renderCalendar();
  });
  elements.closeDialog.addEventListener("click", closeDayDialog);
  elements.dayDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeDayDialog();
  });
  elements.dayDialog.addEventListener("keydown", (event) => {
    if (!elements.dayDialog.open || event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) return;
    const focusable = [...elements.dayDialog.querySelectorAll("a[href], button, input, select, textarea, summary, [tabindex]")]
      .filter((element) => element.tabIndex >= 0 && !element.disabled &&
        !element.closest("[inert]") && element.getClientRects().length > 0);
    const first = focusable[0] ?? elements.closeDialog;
    const last = focusable.at(-1) ?? first;
    if (!focusable.includes(document.activeElement) ||
        document.activeElement === (event.shiftKey ? first : last)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus({ preventScroll: true });
    }
  });
  elements.dayDialog.addEventListener("click", (event) => {
    if (event.target === elements.dayDialog) closeDayDialog();
  });
  elements.dayDialog.addEventListener("close", finishDayDialog);
  elements.selectAll.addEventListener("click", () => setAllMaids(true));
  elements.clearAll.addEventListener("click", () => setAllMaids(false));
  elements.hideKitchen.addEventListener("change", () => {
    state.hideKitchen = elements.hideKitchen.checked;
    renderMaidFilters();
    renderCalendar();
  });
  elements.resetFilters.addEventListener("click", resetFilters);
  elements.dateFrom.addEventListener("change", () => syncDateRange("from"));
  elements.dateTo.addEventListener("change", () => syncDateRange("to"));

  elements.dateFrom.value = state.dateFrom;
  elements.dateTo.value = state.dateTo;
  elements.lastUpdated.textContent = `最終更新：${data.lastUpdated.replace(/\s*JST\b/g, "")}`;
  elements.maidFilterDetails.open =
    !window.matchMedia("(max-width: 45rem)").matches;
  syncViewMode();
  renderMaidFilters();
  renderCalendar();
  if (!window.OBSERVED_SHIFTS) {
    refreshObservedData();
    window.setInterval(() => {
      if (!document.hidden && !elements.dayDialog.open) refreshObservedData();
    }, 60000);
  }
  if (!window.PERSONAL_SHIFTS && typeof window.fetch === "function") {
    refreshPersonalData();
    window.setInterval(() => {
      if (!document.hidden && !elements.dayDialog.open) refreshPersonalData();
    }, 60000);
  }
  if (!window.HALF_MONTH_SCHEDULES && typeof window.fetch === "function") {
    refreshHalfMonthData();
    window.setInterval(() => {
      if (!document.hidden && !elements.dayDialog.open) refreshHalfMonthData();
    }, 60000);
  }
})();
