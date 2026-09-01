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
    return (
      `${storeShort(insights, storeId)}の${shift}はふだん${profile.mode}人態勢` +
      `（${spread}が中心、${toPercent(profile.modeShare ?? 0)}が${profile.mode}人。` +
      `実績${profile.shifts}シフト）`
    );
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
      // 日単位のローテーション表は、日単位の問いに対してはシフト別より当たる（51.4% 対 38〜43%）。
      // チップの数値はシフト別のままにして、ここでは「その日どちらが開くか」だけを補足する。
      const previousDay = openStoresOnDay(insights, lastActualDate);
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

  // 生誕祭・周年・卒業の主役は、その日かならず自分の所属店に立つ。
  // 所属店は公式の配属ではなく「その店が開いている日にいちばん入っている店」からの推定。
  function eventStorePins({ insights, entries }) {
    const pins = new Map();
    for (const entry of entries ?? []) {
      if (!entry?.featured) {
        continue;
      }
      const tendency = insights?.maidTendency?.[entry.name];
      const home = tendency?.home;
      if (!home) {
        continue;
      }
      pins.set(entry.name, {
        storeId: home,
        label: entry.eventLabel ?? "記念日",
        pickRate: tendency.pickRate?.[home] ?? null
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

  function affinityFor(insights, name, shift, storeIds) {
    const tendency = insights.maidTendency?.[name];
    const uniform = 1 / storeIds.length;
    if (!tendency) {
      return { scores: Object.fromEntries(storeIds.map((id) => [id, uniform])), known: false };
    }
    const { pickRate } = tendencyTables(tendency, shift);
    const raw = storeIds.map((id) => Math.max(pickRate[id] ?? 0, 0));
    const total = raw.reduce((sum, value) => sum + value, 0);
    if (total <= 0) {
      return { scores: Object.fromEntries(storeIds.map((id) => [id, uniform])), known: false };
    }
    return {
      scores: Object.fromEntries(storeIds.map((id, index) => [id, raw[index] / total])),
      known: true
    };
  }

  function assignShiftStores({ insights, members, shift, storeIds, pins }) {
    if (!insights || !Array.isArray(members) || members.length === 0 || storeIds.length === 0) {
      return null;
    }
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
    const byMaid = new Map();

    // 主役は動かせないので先に席を取る。定員を超えるなら定員のほうを広げる。
    for (const [name, pin] of pinned) {
      remaining[pin.storeId] = (remaining[pin.storeId] ?? 0) - 1;
      capacity[pin.storeId] = Math.max(capacity[pin.storeId] ?? 0, 1);
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
        const { scores, known } = affinityFor(insights, name, shift, storeIds);
        const order = [...storeIds].sort(
          (a, b) => scores[b] - scores[a] || storeIds.indexOf(a) - storeIds.indexOf(b)
        );
        return {
          name,
          index,
          scores,
          order,
          known,
          // 迷いの少ない人から先に決める。後回しにすると定員が埋まって押し出される。
          regret: scores[order[0]] - scores[order[1]]
        };
      });

    ranked.sort((a, b) => b.regret - a.regret || a.index - b.index);

    for (const member of ranked) {
      const target = member.order.find((id) => remaining[id] > 0) ?? member.order[0];
      remaining[target] = Math.max((remaining[target] ?? 0) - 1, 0);
      const runnerUp = member.order.find((id) => id !== target) ?? null;
      byMaid.set(member.name, {
        storeId: target,
        score: member.scores[target],
        runnerUpId: runnerUp,
        runnerUpScore: runnerUp ? member.scores[runnerUp] : 0,
        known: member.known,
        // 本人の一番人気ではなく、定員の都合で押し出された場合。
        full: target !== member.order[0],
        pin: null
      });
    }

    // 実際に配った人数を定員として持ち直す（主役ぶんで増えていることがある）。
    const placed = Object.fromEntries(storeIds.map((id) => [id, 0]));
    for (const entry of byMaid.values()) {
      placed[entry.storeId] += 1;
    }
    return { storeIds, capacity: placed, byMaid };
  }

  // その日出る人数が標準人数を超えるなら、その人数を収めるだけの店が開いているはず。
  // 実測では、この定員方式だけで店舗数の的中が昼85.5% / 夜85.8%（1店舗の日も3店舗の日も拾える）。
  // ローテーション確率は1号店が高すぎてほぼ常に「2店」になり、少人数の日を潰すので使わない。
  function capacityCountFor(insights, shift, orderedIds, poolSize) {
    if (!(poolSize > 0)) {
      return 1;
    }
    const headcount = insights.typicalHeadcount?.[shift] ?? {};
    // typicalHeadcount は見習い込みの人数。カレンダーに出るのは在籍だけなので、
    // その差（2割ほど）が「見習いが何人来るか分からない」ぶんの緩衝として働く。
    // ここにさらに見習いぶんを足すと過大評価になり、実測でも精度が落ちる（84.4%→82.5%）。
    const needed = poolSize;
    let seats = 0;
    for (let index = 0; index < orderedIds.length; index += 1) {
      seats += headcount[orderedIds[index]] ?? 0;
      if (seats >= needed) {
        return index + 1;
      }
    }
    return orderedIds.length;
  }

  // 開いている店が分からない日は、その日出る人数から店舗数を決める。
  function expectedOpenStores(insights, shift, outlook, poolSize) {
    if (!outlook) {
      return [];
    }
    if (outlook.basis === "actual") {
      return outlook.openStores ?? [];
    }
    const certain = outlook.certainStores ?? [];
    const ordered = [
      ...certain,
      ...[...outlook.entries]
        .sort((a, b) => b.rate - a.rate)
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

  function getShiftAssignment({ insights, members, shift, outlook, pins }) {
    const storeIds = expectedOpenStores(insights, shift, outlook, members?.length ?? 0);
    if (storeIds.length === 0) {
      return null;
    }
    const assignment = assignShiftStores({ insights, members, shift, storeIds, pins });
    return assignment ? { ...assignment, basis: outlook.basis } : null;
  }

  // 割り振り結果を、そのメイドさんの行に出すチップに変換する。
  function getMaidStoreOutlook({ insights, name, shift, outlook, assignment }) {
    const tendency = insights?.maidTendency?.[name];
    if (!tendency || !assignment) {
      return null;
    }
    const placed = assignment.byMaid.get(name);
    if (!placed) {
      return null;
    }
    const stores = storesOf(insights);
    const store = stores.find((candidate) => candidate.id === placed.storeId);
    if (!store) {
      return null;
    }

    const { scope } = tendencyTables(tendency, shift);
    const scopeNote = scope === shift
      ? `${shift}の実績`
      : "昼夜あわせた実績（このシフトは件数が少ないため）";

    if (placed.pin) {
      const strength = typeof placed.pin.pickRate === "number"
        ? `${store.short}が開いた${shift}の${toPercent(placed.pin.pickRate)}をこの店で過ごしています`
        : null;
      return {
        basis: "event",
        storeId: store.id,
        rate: 1,
        label: compactStoreLabel(store),
        percent: "確定",
        title: [
          `${placed.pin.label}の主役なので、所属店の${store.short}にいます`,
          `所属店は公式の配属ではなく、出勤実績から推定したものです${strength ? `（${strength}）` : ""}`
        ].join("。"),
        srText: `${placed.pin.label}の主役なので所属店の${store.short}にいます`
      };
    }

    const roomFor = assignment.storeIds
      .map((id) => `${storeShort(insights, id)}${assignment.capacity[id]}人`)
      .join(" / ");
    const known = outlook?.basis === "actual"
      ? `この${shift}に開いていた${assignment.storeIds.length}店`
      : `この${shift}に開きそうな${assignment.storeIds.length}店`;
    const percent = toPercent(placed.score);
    const detail = [
      `${known}へ、この${shift}に出る${assignment.byMaid.size}名を店ごとの標準人数（${roomFor}）で割り振った結果です`,
      `${name}さんは${store.short}が${percent}（${scopeNote}）`,
      placed.runnerUpId
        ? `次点は${storeShort(insights, placed.runnerUpId)}が${toPercent(placed.runnerUpScore)}`
        : null,
      placed.full ? "本人の傾向では別の店が上でしたが、そちらの定員が先に埋まりました" : null,
      outlook?.basis === "actual" ? null : "開いている店舗自体が見込みなので、外れることがあります"
    ];

    return {
      basis: "assignment",
      storeId: store.id,
      rate: placed.score,
      label: compactStoreLabel(store),
      percent,
      title: detail.filter(Boolean).join("。"),
      srText: `${shift}の割り振りでは${store.short}、${percent}`
    };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      addDays,
      applyEventCertainty,
      assignShiftStores,
      dateKey,
      eventStorePins,
      expectedOpenStores,
      getDateGridColumn,
      getMaidStoreOutlook,
      getShiftAssignment,
      getStoreOutlook,
      getTokyoDateDefaults,
      getVisibleMonthDates,
      isDateKeyInRange,
      lastActualDateOf,
      openStoresOn,
      openStoresOnDay,
      sortByAssignedStore,
      storeCapacities,
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
    const entries = data.schedule[key]?.[shift] ?? [];
    const pins = eventStorePins({ insights, entries });
    const outlook = getStoreOutlook({
      insights,
      dateKey: key,
      shift,
      lastActualDate: lastActualKey
    });
    return { outlook: applyEventCertainty(insights, outlook, pins), pins };
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
      const size = storeSizeNote(insights, shift, entry.store.id);
      if (size) {
        chip.title = size;
      }

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

  const VIEW_MODES = {
    forecast: {
      label: "予測",
      help: "開いていそうな店舗と、その日出るメンバーの割り振りを出します。"
    },
    roster: {
      label: "誰いるか",
      help: "店舗の予測を隠して、誰がお給仕に出るかだけを見ます。"
    }
  };
  const VIEW_MODE_KEY = "akibazettai:view-mode";

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
    selectedMaids: new Set(data.roster),
    dateFrom: defaults.dateFrom,
    dateTo: defaults.dateTo,
    viewMode: hasInsights ? readStoredMode() ?? "forecast" : "roster"
  };

  const elements = {
    calendar: document.querySelector("#calendar"),
    monthTitle: document.querySelector("#month-title"),
    resultSummary: document.querySelector("#result-summary"),
    lastUpdated: document.querySelector("#last-updated"),
    insightNotes: document.querySelector("#insight-notes"),
    modeHelp: document.querySelector("#mode-help"),
    modeInputs: [...document.querySelectorAll('input[name="view-mode"]')],
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

    const forecasting = state.viewMode === "forecast";
    const { outlook, pins } = forecasting
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
    if (outlook) {
      title.append(createStatusBadge(outlook));
    }
    section.append(title);

    if (outlook) {
      section.append(createStoreOutlook(outlook, shift));
    }

    const allEntries = data.schedule[key]?.[shift] ?? [];
    const visible = filteredEntries(key, shift);
    // 割り振りは絞り込みに影響されないよう、その日そのシフトの全員で計算する。
    const assignment = outlook
      ? getShiftAssignment({
        insights,
        members: allEntries.map((entry) => entry.name),
        shift,
        outlook,
        pins
      })
      : null;
    const entries = assignment
      ? sortByAssignedStore({ insights, entries: visible, assignment })
      : visible;

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
        const chipData = getMaidStoreOutlook({
          insights,
          name: entry.name,
          shift,
          outlook,
          assignment
        });
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

    lines.push(
      "メイドさんのチップは、その日そのシフトに出る顔ぶれを、店ごとの標準人数を定員として割り振った結果です。" +
        "1人ずつ独立に「いそうな店」を出すと、ほぼ毎日開いている1号店に全員が寄ってしまうため、定員を奪い合わせています。"
    );
    lines.push(
      "生誕祭・周年・卒業の主役だけは確実で、かならず自分の所属店に立ちます。" +
        "その店はその日営業することも確定するので、チップは割合ではなく「確定」と出しています。" +
        "所属店は公式の配属ではなく、その店が開いた日にいちばん入っている店から推定したものです。"
    );

    const s1Day = insights.baseOpenRate?.["昼"]?.s1;
    const s1Night = insights.baseOpenRate?.["夜"]?.s1;
    if (typeof s1Day === "number" && typeof s1Night === "number") {
      lines.push(
        `1号店もほぼ毎日開いていますが、毎日ではありません（昼${toPercent(s1Day)} / 夜${toPercent(s1Night)}）。` +
          "夜だけ休みの日がときどきあります。"
      );
    }
    if (givenOpen !== null || top1 !== null) {
      const parts = [];
      if (givenOpen !== null) {
        parts.push(`開いている店舗が分かっていれば${toPercent(givenOpen)}`);
      }
      if (top1 !== null) {
        parts.push(`分からなければ${toPercent(top1)}`);
      }
      lines.push(`個々のメイドさんの店舗を1店に絞って当たったのは、${parts.join("、")}でした。`);
    }
    if (top2 !== null) {
      lines.push(
        `候補を2店舗まで広げると${toPercent(top2)}が当たります。チップに出しているのは1店だけなので、` +
          "次点はチップにカーソルを合わせると読めます。"
      );
    }

    const split = insights.shiftSplitGivenOpen;
    const counts = insights.openCountPerShift;
    if (counts) {
      const shareOf = (shift, wanted) => {
        const table = counts[shift] ?? {};
        const total = Object.values(table).reduce((sum, value) => sum + value, 0);
        return total > 0 ? (table[String(wanted)] ?? 0) / total : 0;
      };
      const three = shifts
        .map((shift) => `${shift}${toPercent(shareOf(shift, 3))}`)
        .join(" / ");
      const one = shifts
        .map((shift) => `${shift}${toPercent(shareOf(shift, 1))}`)
        .join(" / ");
      lines.push(
        `開く店舗の数は日によって違います。3店舗開く日は${three}、1店舗だけの日は${one}で、` +
          "夜のほうが閉まりやすい傾向です。"
      );
    }

    const profile = insights.headcountProfile;
    if (profile) {
      const described = shifts
        .map((shift) => {
          const parts = storeList
            .filter((store) => profile[shift]?.[store.id])
            .map((store) => `${store.short}${profile[shift][store.id].mode}人`);
          return parts.length > 0 ? `${shift}は${parts.join("・")}` : null;
        })
        .filter(Boolean);
      const sized = storeList.filter((store) => profile["昼"]?.[store.id]);
      if (described.length > 0 && sized.length > 0) {
        const smallest = sized.reduce((least, store) =>
          profile["昼"][store.id].mode < profile["昼"][least.id].mode ? store : least
        );
        const smallestProfile = profile["昼"][smallest.id];
        lines.push(
          `店の規模も違います。ふだんの人数は${described.join("、")}で、` +
            `${smallest.short}だけ${smallestProfile.mode}人態勢（昼の${toPercent(smallestProfile.modeShare ?? 0)}）と小さめです。` +
            "各店のチップにカーソルを合わせると、その店の人数の幅が読めます。"
        );
      }
    }

    const headcounts = insights.rosterHeadcountByOpenCount;    if (headcounts) {
      const described = shifts
        .map((shift) => {
          const table = headcounts[shift] ?? {};
          const parts = Object.keys(table)
            .sort()
            .map((count) => `${count}店舗なら${table[count].mean}人`);
          return parts.length > 0 ? `${shift}は${parts.join("・")}` : null;
        })
        .filter(Boolean);
      if (described.length > 0) {
        lines.push(
          `いくつ開くかは、その日カレンダーに出る人数から見積もっています（${described.join("、")}）。` +
            "ローテーションの確率からは3店舗の日を当てられなかったためです。"
        );
      }
    }    if (split) {
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

  function createCoverageNote() {
    const coverage = insights.rosterCoverage;
    if (!coverage || typeof coverage.unlistedPerShift !== "number") {
      return null;
    }

    const note = document.createElement("p");
    note.className = "insight-headline";
    const perShift = String(coverage.unlistedPerShift);
    const shiftShare = toPercent(coverage.shiftsWithUnlisted ?? 0);
    const parts = [
      "カレンダーに出るのは在籍メイドさんだけです。",
      "見習いにゃんこがいつお給仕に出るかは当日のお給仕投稿まで分からず、事前に知る手段がありません。",
      `そのため、ここに出ている人数は実際より少なく、開いている店1つあたり平均${perShift}人ぶん足りません（${shiftShare}のシフトで発生）。`
    ];

    if (typeof coverage.unlistedShare === "number" && coverage.shiftCells) {
      parts.push(
        `${coverage.from}〜${coverage.to}の${coverage.shiftCells}シフトで、` +
          `のべ人数の${toPercent(coverage.unlistedShare)}が未掲載でした。`
      );
    }

    const byStore = coverage.byStore;
    if (byStore) {
      const described = storeList
        .filter((store) => byStore[store.id])
        .map((store) => `${store.short}${byStore[store.id].unlistedPerShift}人`)
        .join(" / ");
      if (described) {
        const quietest = storeList
          .filter((store) => byStore[store.id])
          .reduce((least, store) =>
            byStore[store.id].unlistedPerShift < byStore[least.id].unlistedPerShift ? store : least
          );
        parts.push(
          `店ごとの見習いの人数は${described}で、${quietest.short}だけ少なめです` +
            `（見習いがいないシフトが${toPercent(byStore[quietest.id].shiftsWithoutUnlisted ?? 0)}）。`
        );
      }
    }

    note.textContent = parts.join("");
    note.title =
      `未掲載の人数の内訳：` +
      Object.entries(coverage.distribution ?? {})
        .map(([count, days]) => `${count}人が${days}シフト`)
        .join("、");
    return note;
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
        `事前には分からない人たち（${active.length}名）：` +
        "公式サイトの在籍一覧には無いものの、最近のお給仕投稿に出ている方です。" +
        "この方々が出るかどうかは当日まで分からないため、カレンダー本体には表示していません。" +
        "予定としては使えません。公開アカウントを確認できた方だけXへのリンクを付けています。";

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
    if (container.children.length > 1) {
      // 一度作れば中身は変わらない。モードによる出し入れだけ行う。
      container.hidden = state.viewMode !== "forecast";
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
        badge: "翌日見込み",
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
    const coverage = createCoverageNote();
    container.append(
      ...(coverage ? [coverage] : []),
      legend,
      accuracy,
      ...(unlisted ? [unlisted] : []),
      source
    );
    container.hidden = state.viewMode !== "forecast";
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
    renderInsightNotes();
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
    state.viewMode = hasInsights ? "forecast" : "roster";
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
  syncViewMode();
  renderMaidFilters();
  renderCalendar();
})();
