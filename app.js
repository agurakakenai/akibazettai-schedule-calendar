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

  // 2026-09-01 の制度変更（上旬・下旬をまとめて事前公開）直後は、まだ提出していない
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
      "まだ提出していないメイドさんは、この表に出ていないことがあります。"
    );
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
  // 所属店は公式サイトの配属（homeStore）を優先し、載っていない人だけ
  // 「その店が開いている日にいちばん入っている店」からの推定で補う。
  function eventStorePins({ insights, entries, homeStore }) {
    const pins = new Map();
    for (const entry of entries ?? []) {
      if (!entry?.featured) {
        continue;
      }
      const tendency = insights?.maidTendency?.[entry.name];
      const official = homeStore?.[entry.name];
      const home = official ?? tendency?.home;
      if (!home) {
        continue;
      }
      pins.set(entry.name, {
        storeId: home,
        label: entry.eventLabel ?? "記念日",
        official: Boolean(official),
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

  // キッチンにゃんこは料理担当なので、実測でも各店1人ずつが基本（1店1人が77.5%、平均1.05人）。
  // 禁止ではなく減点にする。実際に13.7%は2人一緒に入っている。
  // スコアは候補店で合計1に正規化した値なので、減点もその尺度で置く。
  const KITCHEN_SPREAD_PENALTY = 0.5;

  function assignShiftStores({ insights, members, shift, storeIds, pins, kitchenStaff }) {
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
        const { scores, known } = affinityFor(insights, name, shift, storeIds);
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
      const posted = insights.maidTendency?.[name]?.posted;
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

  // 開いている店が分からない日は、その日出る人数から店舗数を決める。
  // 第4引数は人数でも顔ぶれの配列でもよい。配列なら誰が出るかも順位付けに使う。
  function expectedOpenStores(insights, shift, outlook, pool) {
    if (!outlook) {
      return [];
    }
    if (outlook.basis === "actual") {
      return outlook.openStores ?? [];
    }
    const members = Array.isArray(pool) ? pool : null;
    const poolSize = members ? members.length : pool ?? 0;
    const certain = outlook.certainStores ?? [];
    const tilt = postedTilt(insights, shift, members);
    const rank = (entry) => {
      const rate = Math.min(Math.max(entry.rate ?? 0, 0.005), 0.995);
      const odds = Math.log(rate / (1 - rate));
      return odds + (tilt?.get(entry.store.id) ?? 0);
    };
    const ordered = [
      ...certain,
      ...[...outlook.entries]
        .sort((a, b) => rank(b) - rank(a))
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

  function getShiftAssignment({ insights, members, shift, outlook, pins, kitchenStaff }) {
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
      kitchenStaff
    });
    return assignment ? { ...assignment, basis: outlook.basis } : null;
  }

  // 候補店の中で pickRate を合計1に正規化する。「この人はこの店」と断定せず、
  // どの店にもいる可能性があることを示す。実測で35名全員が4店舗すべてに入っている。
  function storeProbabilities(insights, name, shift, storeIds) {
    if (!Array.isArray(storeIds) || storeIds.length === 0) {
      return {};
    }
    const tendency = insights?.maidTendency?.[name];
    const pickRate = tendency ? tendencyTables(tendency, shift).pickRate : {};
    // 0 の店を完全に消さないよう下限を置く。低くても行かないわけではない。
    const raw = storeIds.map((id) => Math.max(pickRate[id] ?? 0, 1e-6));
    const total = raw.reduce((sum, value) => sum + value, 0);
    return Object.fromEntries(storeIds.map((id, index) => [id, raw[index] / total]));
  }

  // 割り振り結果を、そのメイドさんの行に出すチップに変換する。
  // 表示した確率が実測とどれだけ離れているか。真ん中は5ポイント以内に収まるが、
  // 両端は自信過剰で、「97%」と出しても実測は74%ほどしか当たらない。
  // 帯そのものはデータ側の測定（accuracy.calibration）から引く。
  const CALIBRATION_DRIFT = 0.1;
  // 標本の少ないバケットは実測値自体が揺れるので、注記の根拠にしない。
  const CALIBRATION_MIN_SAMPLE = 100;

  function calibrationNote(insights, rate) {
    const buckets = insights?.accuracy?.calibration?.buckets;
    if (!Array.isArray(buckets) || typeof rate !== "number") {
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
    return drift < 0
      ? `ただしこのくらい高い数字は自信過剰で、${band}と出したときに実際に当たったのは${toPercent(bucket.actual)}です`
      : `ただしこのくらい低い数字は控えめすぎて、${band}と出した店にも実際は${toPercent(bucket.actual)}の割合で入っています`;
  }

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
    const shortOf = (id) => storeShort(insights, id);

    if (placed.pin) {
      const store = stores.find((candidate) => candidate.id === placed.storeId);
      if (!store) {
        return null;
      }
      const strength = typeof placed.pin.pickRate === "number"
        ? `${store.short}が開いた${shift}の${toPercent(placed.pin.pickRate)}をこの店で過ごしています`
        : null;
      const source = placed.pin.official
        ? `所属店は公式サイトの配属です${strength ? `（${strength}）` : ""}`
        : `所属店は公式の配属が分からないため、出勤実績から推定したものです${strength ? `（${strength}）` : ""}`;
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
    // 公式サイトの配属。確率は実績が主で配属は弱い事前分布なので、
    // 実績の多い人ほど配属から離れる。そのずれを読み手が確かめられるように出す。
    const postedNote = tendency?.posted
      ? `公式サイトの配属は${shortOf(tendency.posted)}`
      : null;

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
        calibrationNote(insights, probabilities[top.id]),
        "どの店にも入る可能性があります。実際、在籍35名は全員が4店舗すべてに入った実績があります",
        "いちばん高い店だけを見ると、たまに入る店を取りこぼします"
      ]
        .filter(Boolean)
        .join("。"),
      srText: second
        ? `${shift}は${top.short}が${toPercent(probabilities[top.id])}、次に${second.short}が${toPercent(probabilities[second.id])}`
        : `${shift}は${top.short}が${toPercent(probabilities[top.id])}`
    };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      addDays,
      applyEventCertainty,
      assignShiftStores,
      calibrationNote,
      dateKey,
      eventStorePins,
      expectedOpenStores,
      getDateGridColumn,
      getMaidStoreOutlook,
      getShiftAssignment,
      getStoreOutlook,
      getTokyoDateDefaults,
      getVisibleMonthDates,
      groupByAssignedStore,
      isDateKeyInRange,
      lastActualDateOf,
      openStoresOn,
      openStoresOnDay,
      scheduleSystemNote,
      sortByAssignedStore,
      storeCapacities,
      storeProbabilities,
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
    const pins = eventStorePins({ insights, entries, homeStore: data.homeStore });
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
    hideKitchen: false,
    viewMode: hasInsights ? readStoredMode() ?? "forecast" : "roster"
  };

  const elements = {
    calendar: document.querySelector("#calendar"),
    monthTitle: document.querySelector("#month-title"),
    resultSummary: document.querySelector("#result-summary"),
    lastUpdated: document.querySelector("#last-updated"),
    scheduleSystemNote: document.querySelector("#schedule-system-note"),
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
    return state.selectedMaids.has(name);
  }

  function filteredEntries(key, shift) {
    const entries = data.schedule[key]?.[shift] ?? [];
    return entries.filter((entry) => isVisibleMaid(entry.name));
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
        pins,
        kitchenStaff
      })
      : null;
    const entries = assignment
      ? sortByAssignedStore({ insights, entries: visible, assignment })
      : visible;

    function createMaidEntry(entry, groupStoreId) {
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
        // 見出しで店が分かっているときは、チップは割合だけにして繰り返さない。
        item.append(createMaidStoreChip(chipData, groupStoreId !== chipData.storeId));
        titles.push(chipData.title);
        descriptions.push(chipData.srText);
      }

      if (titles.length > 0) {
        item.title = `${entry.name}：${titles.join(" / ")}`;
        item.setAttribute("aria-label", `${entry.name}（${descriptions.join("・")}）`);
      }
      return item;
    }

    if (entries.length > 0) {
      const groups = groupByAssignedStore({ insights, entries, assignment });

      groups.forEach(({ storeId, entries: members }) => {
        const store = storeId ? storeList.find((candidate) => candidate.id === storeId) : null;
        if (store) {
          const heading = document.createElement("p");
          heading.className = "maid-group-label";
          heading.dataset.store = store.id;
          const name = document.createElement("span");
          name.textContent = store.short;
          const count = document.createElement("span");
          count.className = "maid-group-count";
          count.textContent = `${members.length}人`;
          heading.append(name, count);
          heading.title = storeSizeNote(insights, shift, store.id) ?? "";
          section.append(heading);
        }

        const list = document.createElement("ul");
        list.className = "maid-list";
        members.forEach((entry) => list.append(createMaidEntry(entry, storeId)));
        section.append(list);
      });

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
          shiftTotal + day[shift].filter((entry) => isVisibleMaid(entry.name)).length,
        0
      );
    }, 0);

    elements.monthTitle.textContent = `${year}年${monthIndex + 1}月`;
    elements.resultSummary.textContent =
      `${visibleMaidCount()}名を選択中・${displayedCount}件のお給仕を表示`;
    elements.calendar.replaceChildren(grid);
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
    return data.roster.filter((name) => isVisibleMaid(name)).length;
  }

  function updateMaidFilterSummary() {
    elements.maidFilterSummary.textContent =
      `${visibleMaidCount()}/${data.roster.length}名を表示`;
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
        checkbox.disabled = state.hideKitchen;
        if (state.hideKitchen) {
          label.classList.add("is-muted");
        }
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
    state.hideKitchen = false;
    elements.hideKitchen.checked = false;
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
  elements.lastUpdated.textContent = `最終更新：${data.lastUpdated}`;
  const systemNote = scheduleSystemNote(insights, defaults.dateFrom);
  if (systemNote && elements.scheduleSystemNote) {
    elements.scheduleSystemNote.textContent = systemNote;
    elements.scheduleSystemNote.hidden = false;
  }
  elements.maidFilterDetails.open =
    !window.matchMedia("(max-width: 45rem)").matches;
  syncViewMode();
  renderMaidFilters();
  renderCalendar();
})();
