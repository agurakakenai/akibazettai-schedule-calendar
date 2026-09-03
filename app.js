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
  // 予定表をまだ出していない在籍者。揃うまで、画面の顔ぶれは実際より薄い。
  // 人数から店舗数を決めているので、開く店も少なめに出ることがある。
  //
  // 「何人ぶん薄いか」は windowShifts で割って出す。この分母はデータに入って
  // いるので推測にならない。入っていないときは書かない。推測した分母で割った
  // 数字は、測った数字の顔をしてしまう。
  function schedulePendingNote(insights) {
    const pending = insights?.schedulePending;
    const names = pending?.pending;
    if (!Array.isArray(names) || names.length === 0) {
      return null;
    }
    const counts = pending.recentShifts ?? {};
    const busiest = [...names].sort((a, b) => (counts[b] ?? 0) - (counts[a] ?? 0));
    const named = busiest
      .map((name) => (counts[name] > 0 ? `${name}（最近${counts[name]}回）` : name))
      .join("・");
    const total = pending.rostered;
    const window = pending.windowShifts;
    const worked = names.reduce((sum, name) => sum + (counts[name] ?? 0), 0);
    const perShift = window > 0 && worked > 0
      ? `同じペースなら1シフトあたり${(worked / window).toFixed(1)}人ぶんです。`
      : "";
    const head = `${total > 0 ? `在籍${total}名のうち` : ""}${names.length}名が、まだ予定を出していません`;
    return {
      short: head,
      long:
        `${head}：${named}。` +
        `この方たちは表に出ていないので、顔ぶれは実際より少なめです。${perShift}` +
        "人数から開く店の数を決めているぶん、店も少なめに出ることがあります。" +
        "提出が揃えばこの注意書きは消えます"
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
  // ためで、店舗数が決まってから掛ける。店ごとに配らないのは、どの店にいるかが
  // 読めないから。実測でも1号店0.35 / 2号店0.35 / 3号店0.32と横並びで、
  // どこかを選ぶ根拠がない。だから店の見出しの下ではなく、シフトの末尾に置く。
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
      traineeNote(insights, storeId)
    ]
      .filter(Boolean)
      .join("。");
  }

  // 見習いにゃんこは予定表に出ないので、カレンダーの人数より実際は多い。
  // 平均を小数で見せても伝わらないので、「何割の枠にいたか」で言う。
  // 実測の分布は 0人29% / 1人58% / 2人11% で、ほぼ0か1。
  function traineeNote(insights, storeId) {
    const coverage = insights?.rosterCoverage;
    const store = coverage?.byStore?.[storeId];
    if (!store || typeof store.shiftsWithoutUnlisted !== "number") {
      return null;
    }
    const withAny = 1 - store.shiftsWithoutUnlisted;
    if (withAny <= 0) {
      return null;
    }
    const months = monthsBetween(coverage.from, coverage.to);
    const period = months ? `直近${months}か月` : "この集計期間";
    return (
      `予定表に出ない見習いにゃんこがいます。${period}では、この店の` +
      `${toPercent(withAny)}の枠に1人以上いました（多くはちょうど1人）`
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
      const home = homeStore?.[name];
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
  function getStoreOutlook({ insights, dateKey: key, shift, lastActualDate }) {
    if (!insights || storesOf(insights).length === 0) {
      return null;
    }
    const last = lastActualDate === undefined ? lastActualDateOf(insights) : lastActualDate;
    return (
      actualOutlook(insights, key, shift) ??
      forecastOutlook(insights, key, shift, last) ??
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
    if (!homeStore?.[name]) {
      return null;
    }
    const unposted = unpostedMaids instanceof Set ? unpostedMaids : new Set(unpostedMaids ?? []);
    return unposted.has(name) ? "shop" : "site";
  }

  const HOME_STORE_SOURCE_LABEL = {
    site: "公式サイトの配属",
    shop: "お店の案内による所属"
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
      const tendency = insights?.maidTendency?.[entry.name];
      const declared = homeStore?.[entry.name];
      const home = declared ?? tendency?.home;
      if (!home) {
        continue;
      }
      pins.set(entry.name, {
        storeId: home,
        label: entry.eventLabel ?? "記念日",
        source: homeStoreSourceOf(homeStore, unpostedMaids, entry.name) ?? "record",
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
  // 通しで働いた1238人のうち68.7%が昼と別の店にいる。昼の店が夜も開いていても
  // 56.4%が移るので、「開いているから残る」ではない。移り先には向きがあり、
  // 夜はどの店からも1号店に吸われる（昼s4→夜s1 が84%）。
  //
  // 人ごとの表があればそれを使う。無ければ全体の表。人ごとは n が小さい組み
  // 合わせがあるので（あむさんの昼s4は6回）、n も返して読み手側で判断できる形。
  function sameDayMoveOdds(insights, name, fromStoreId) {
    const perMaid = insights?.maidTendency?.[name]?.sameDayMove?.[fromStoreId];
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
    const tendency = insights.maidTendency?.[name];
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

  // どの店を開けるかは当日決まる。1号店は93%が通しで日単位に動くが、2〜4号店は
  // 片シフトだけの営業が主で、同じ日でも昼と夜で別の店を開けている（通しで出た人の
  // 68.6%が昼夜で別の店）。だから「予測を外した」のではなく「まだ決まっていない」。
  //
  // 一方、何店開くかはお店が事前に人を組んでいるぶん読める（人数から87.5%）。
  // 断定してよいのは店の数で、どの店かではない。
  function sameDayDecisionNote(insights, outlook) {
    if (!outlook || outlook.basis === "actual") {
      return null;
    }
    return (
      "何店開くかは、その日に出る人数から読めます（お店が先に人を組むため）。" +
      "ただし、どの店を開けるかは当日決まります。ここから先は過去の並びから見た可能性で、" +
      "まだ誰も知りません。"
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
    const tendency = insights?.maidTendency?.[name];
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
  // 通しで働いた方の68.7%が昼と別の店にいて、昼の店が夜も開いていた場合でも
  // 56.4%が移る。読み手が不思議に思う場所なので、移り先を出したときは理由も書く。
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
      `${from}が夜も開いているかどうかとは別に、移ることのほうが多くなっています`
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
    const trainees = new Set(judged ? record.trainees : []);
    const byMaid = new Map();
    const capacity = {};
    for (const id of storeIds) {
      capacity[id] = record.stores[id].length;
      for (const name of record.stores[id]) {
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
  // 120日だけを見て答え、実際と突き合わせた実測値（walk-forward）。1店しか
  // 開かなかったシフトは定義上かならず当たるので除いてある。
  //
  // 全体値（accuracy.maidStoreGivenOpen）とは測り方が違うので、混ぜない。
  // 測れていない人（記録が少ない人）には、全体値で埋めずに測れていないと書く。
  function maidAccuracy(insights, name) {
    const measured = insights?.maidTendency?.[name]?.accuracy;
    if (!measured || typeof measured.rate !== "number" || !(measured.n > 0)) {
      return null;
    }
    return { rate: measured.rate, n: measured.n };
  }

  // 「n件すべて当たる」確率。並べると外れが見えることを、数で言うために使う。
  // 実測のある人はその人の値で、無い人は何も言わない。
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
      `${toPercent(measured.rate)}当てられました`
    );
  }

  // 人ごとの一覧を組み立てる。店ごとの画面と同じ割り振りを引くので、両者は食い違わない。
  //
  // この軸には固有の危険がある。1件あたりの的中はその人ごとに 39〜81% と幅があり、
  // 店の側も当日決まるので、実際にその人がその店にいる確率はもっと低い。
  // 同じ人の予測を縦に並べると、その外れが一覧で見える。店ごとの画面では
  // 1行ずつ独立に見えて気づかなかったものが表に出るので、件数を数えて
  // 「全部当たることはまずない」と先に言う。
  function maidItinerary({ schedule, name, dates, shifts, resolve, kitchenStaff }) {
    const cooks = kitchenStaff instanceof Set ? kitchenStaff : new Set(kitchenStaff ?? []);
    const stops = [];
    for (const key of dates ?? []) {
      for (const shift of shifts ?? []) {
        const { outlook, assignment } = resolve(key, shift) ?? {};
        // 記録のある日は、その日の顔ぶれを記録が決める。予定表を見ると、
        // お休みだった方に行を作り、記録にしかいない方を落とすことになる。
        const roll = assignment?.recorded
          ? (assignment.byMaid.has(name) ? { name } : null)
          : (schedule?.[key]?.[shift] ?? []).find((member) => member.name === name);
        if (!roll) {
          continue;
        }
        const placed = assignment?.byMaid?.get(name) ?? null;
        // キッチンにゃんこの行き先は、見込みの日には店ごとの画面でも名乗らない。
        // ここで名乗ると、同じ人・同じ日で2つの画面が違うことを言う。
        const loose =
          placed && !assignment.recorded && !placed.pin && cooks.has(name);
        const storeId = loose ? null : placed?.storeId ?? null;
        const row = storeId
          ? outlook?.entries?.find((candidate) => candidate.store.id === storeId) ?? null
          : null;
        stops.push({
          dateKey: key,
          shift,
          storeId,
          kitchen: Boolean(loose),
          // 店ごとの画面と同じ三段階を使う。open は実績か記念日で確定、
          // likely は5割以上、unlikely はそれ未満。別の線を引くと画面が食い違う。
          state: row?.state ?? null,
          openRate: row?.rate ?? null,
          settled: row?.state === "open",
          trainee: placed?.trainee ?? null,
          eventLabel: roll.eventLabel ?? null
        });
      }
    }
    return { name, stops, guesses: stops.filter((stop) => !stop.settled).length };
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
      assignShiftStores,
      calibrationNote,
      coOpenRate,
      dateKey,
      earlierShiftPlaces,
      eventStorePins,
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
      weekdayBucket,
      weekdayIndex
    };
  }

  if (typeof window === "undefined" || typeof document === "undefined") {
    return;
  }

  const data = window.SCHEDULE_DATA;
  const shifts = ["昼", "夜"];
  const TRAINEE_PLACEHOLDER = "見習い";

  // 見習いの人数は入店で動くので、データ側は半減期30日で見ている（人数の180日
  // とは別）。落ち着いた量ではなく来月には変わるので、画面に割合そのものを
  // 書かない。「いそう」までにして、根拠は開く店の数で言う。
  function traineeGuessNote(shift, storeCount) {
    const where = storeCount > 1
      ? `${storeCount}店開けば、そのどこかに`
      : "この店に";
    return `予定表に出ない見習いにゃんこです。${where}いそうですが、どなたかは分かりません（${shift}の実績から）`;
  }
  const kitchenStaff = new Set(data.kitchenStaff ?? []);
  const rosterNames = new Set(data.roster ?? []);
  // 公式サイト未掲載の人。所属は分かるが出どころがサイトではないので書き分ける。
  const unpostedMaids = new Set(data.unpostedMaids ?? []);
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
      help: "メイドさんごとに、その月どこに立ちそうかを並べます。1件ずつは3回に1回ほどしか当たりません。"
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
    schedulePendingNote: document.querySelector("#schedule-pending-note"),
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
    // 記録には、予定表に載らない方（見習いなど）も出てくる。その方たちには
    // チェックボックスが無いので、絞り込みを始めた時点で隠す。残したままだと
    // 「1人だけ選んだのに他の名前が出る」ことになり、絞り込みが効いて見えない。
    if (!rosterNames.has(name)) {
      return state.selectedMaids.size === data.roster.length;
    }
    return state.selectedMaids.has(name);
  }

  // その日そのシフトの顔ぶれ。記録があればそれを、無ければ予定表を使う。
  function shiftRoster(key, shift) {
    const recorded = recordedRoster({
      insights,
      dateKey: key,
      shift,
      schedule: data.schedule,
      roster: data.roster
    });
    return recorded ?? { assignment: null, entries: data.schedule[key]?.[shift] ?? [] };
  }

  function filteredEntries(key, shift) {
    return shiftRoster(key, shift).entries.filter((entry) => isVisibleMaid(entry.name));
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

    const roster = shiftRoster(key, shift);
    // 同じ日の昼に誰がどこにいたか。記録があるときだけ、夜の割り振りに使う。
    const movedFrom = earlierShiftPlaces(insights, key, shift);
    // 見込みの計算には予定表の顔ぶれを使う。記録のある日は使われないが、
    // 記録の顔ぶれを入れると「答えを見て予測する」ことになる。
    const postedNames = (data.schedule[key]?.[shift] ?? []).map((entry) => entry.name);
    if (outlook) {
      section.append(createStoreOutlook(outlook, shift, postedNames));
    }

    const allEntries = roster.entries;
    const visible = allEntries.filter((entry) => isVisibleMaid(entry.name));
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
    const guessedTrainees = isVisibleMaid(TRAINEE_PLACEHOLDER)
      ? expectedTrainees(
        insights,
        shift,
        assignment?.storeIds?.length ?? 0,
        Boolean(assignment?.recorded)
      )
      : 0;

    function createMaidEntry(entry, groupStoreId, hideStore = false) {
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
        assignment,
        unpostedMaids
      });
      const titles = [];
      const descriptions = [];

      if (isKitchen) {
        item.classList.add("is-kitchen");
        titles.push("キッチンにゃんこ");
        descriptions.push("キッチンにゃんこ");
      }

      // 見習いにゃんこ。判定できた日だけ印を付ける。trainee が null の日は
      // 「まだ判定していない」で、「昇格済み」ではない。そこを混ぜない。
      if (entry.trainee === true) {
        item.classList.add("is-trainee");
        const mark = document.createElement("span");
        mark.className = "maid-trainee";
        mark.textContent = "🔰";
        mark.setAttribute("aria-hidden", "true");
        item.append(mark);
        titles.push("見習いにゃんこ");
        descriptions.push("見習いにゃんこ");
      }

      if (entry.featured) {
        item.classList.add("is-featured");
        titles.unshift(entry.eventLabel);
        descriptions.unshift(`${entry.eventLabel}の主役`);
      }

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
        item.title = `${entry.name}：${titles.join(" / ")}`;
        item.setAttribute("aria-label", `${entry.name}（${descriptions.join("・")}）`);
      }
      return item;
    }

    function appendKitchen(target, members, storeCount) {
      if (members.length === 0) {
        return;
      }
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
      heading.title = note;
      const list = document.createElement("ul");
      // 店ごとの一覧（.maid-list）は「見出しと1対1」で店を名乗る約束なので混ぜない。
      list.className = "maid-kitchen-list";
      members.forEach((entry) => {
        // 店を名乗らないと決めた以上、チップで店を出しては辻褄が合わない。
        const item = createMaidEntry(entry, null, true);
        item.setAttribute(
          "aria-label",
          `${entry.name}（${note}）`
        );
        item.title = `${entry.name}：${note}`;
        list.append(item);
      });
      target.append(heading, list);
    }

    if (entries.length > 0 || cooks.length > 0) {
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

      appendTraineeGuesses(section, shift, guessedTrainees, assignment?.storeIds?.length ?? 0);
      appendKitchen(section, cooks, assignment?.storeIds?.length ?? 0);
      return section;
    }

    if (guessedTrainees > 0) {
      appendTraineeGuesses(section, shift, guessedTrainees, assignment?.storeIds?.length ?? 0);
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

  // 予定表に出ない見習いにゃんこ。店ごとのグループには入れない。
  // どの店にいるか読めないのに見出しの下に置くと、その店にいると読まれる。
  // 順番は、店ごとの一覧 → 見習い → キッチン。上ほど確かなことを言っている。
  // 見習いは「誰か分からないが、いる」、キッチンは「誰か分かるが、どこか分からない」。
  function appendTraineeGuesses(section, shift, count, storeCount) {
    if (!(count > 0)) {
      return;
    }
    const note = traineeGuessNote(shift, storeCount);
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
    section.append(list);
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
    if (state.viewMode === "maid") {
      renderMaidView();
      return;
    }
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
        const { outlook, pins } = getShiftOutlook(key, shift);
        const roster = shiftRoster(key, shift);
        // 記録のある日は記録の割り振りを使う。カレンダーと同じものを引かないと、
        // 同じ人・同じ日で別の店を出してしまう。
        const members = (data.schedule[key]?.[shift] ?? []).map((entry) => entry.name);
        shiftCache.set(id, {
          outlook,
          members: roster.entries.map((entry) => entry.name),
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
        schedule: data.schedule,
        name,
        dates,
        shifts,
        resolve,
        kitchenStaff
      });
      if (plan.stops.length === 0) {
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
    block.setAttribute("aria-label", `${plan.name}のお給仕`);

    const heading = document.createElement("h3");
    heading.className = "maid-plan-name";
    const account = insights?.maidTendency?.[plan.name]?.x;
    const nameLabel = document.createElement(account ? "a" : "span");
    nameLabel.className = "maid-name";
    nameLabel.textContent = plan.name;
    if (account) {
      nameLabel.href = `https://x.com/${account}`;
      nameLabel.target = "_blank";
      nameLabel.rel = "noopener noreferrer";
      nameLabel.title = `${plan.name}のXを開く`;
    }
    const count = document.createElement("span");
    count.className = "maid-plan-count";
    count.textContent = `${plan.stops.length}件`;
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
    plan.stops.forEach((stop) => list.append(createMaidStop(stop)));
    block.append(list);
    return block;
  }

  function createMaidStop(stop) {
    const item = document.createElement("li");
    item.className = `maid-plan-stop is-${stop.state ?? "unknown"}`;
    item.dataset.date = stop.dateKey;
    const when = document.createElement("span");
    when.className = "maid-plan-when";
    const [, month, date] = stop.dateKey.split("-").map(Number);
    // 曜日はカレンダーと同じ書き方で添える。「9/3」だけでは何曜日か分からない。
    const weekday = weekdays[new Date(`${stop.dateKey}T00:00:00`).getDay()];
    when.textContent = `${month}/${date}(${weekday}) ${stop.shift}`;
    const where = document.createElement("span");
    where.className = "maid-plan-where";
    where.dataset.store = stop.storeId ?? "";
    where.textContent = stop.storeId ? storeShort(insights, stop.storeId) : "未定";
    item.append(when, where);
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
    const explanation = stopExplanation(stop);
    item.title = explanation;
    // 読み上げは aria-label に一本化する。同じ文を隠し要素にも置くと二度読まれる。
    item.setAttribute("aria-label", `${when.textContent}は${explanation}`);
    return item;
  }

  // 縦に並べると外れが見える軸なので、先に「全部は当たりません」と言っておく。
  function maidPlanCaveat(plan) {
    const settled = plan.stops.length - plan.guesses;
    const parts = [];
    if (settled > 0) {
      parts.push(`${settled}件は記録か記念日で確定`);
    }
    if (plan.guesses > 0) {
      parts.push(
        `${settled > 0 ? "残り" : ""}${plan.guesses}件はどの店を開けるかが当日決まります`
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
        `${toPercent(confidence.perStop)}当てられています`;
      parts.push(
        plan.guesses === 1
          ? measured
          : `${measured}が、${plan.guesses}件すべて当たるのは${toPercent(confidence.allRight)}です`
      );
    } else {
      parts.push(maidAccuracyNote(insights, plan.name, kitchenStaff.has(plan.name)));
    }
    return parts.length > 0 ? `${parts.join("。")}。` : "この期間のお給仕はすべて確定しています。";
  }

  // 見込みの日に、その店の営業率だけを数字で出さない。順位付けには予定表の顔ぶれも
  // 入っているので、「4号店が開くのは15%」と書くと、置いた理由と食い違って見える。
  // 表に出せるのは「開くと見た店のひとつ」までで、確からしさは強弱で伝える。
  function stopExplanation(stop) {
    const trainee = stop.trainee === true ? "見習いにゃんことして" : "";
    if (!stop.storeId) {
      return stop.kitchen
        ? "キッチンにゃんこは開く店に1人ずつ入りますが、どの店かは配属とも関係なく決まるので、ここでは言えません。"
        : "どこへ立つかは、まだ何も言えません。";
    }
    const where = storeShort(insights, stop.storeId);
    if (stop.settled) {
      return stop.eventLabel
        ? `${stop.eventLabel}の主役なので、所属店の${where}にいます。`
        : `${trainee}${where}にいた記録があります。`;
    }
    const strength =
      stop.state === "unlikely"
        ? `${where}は開くと見た店のひとつですが、確からしさは低めです`
        : `${where}は開くと見た店です`;
    return `${strength}。どの店を開けるかは当日決まるので、変わることがあります。`;
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
  // 制度変更の説明は「出していない人がいることがある」までしか言えない。
  // 実際に誰が出していないかは分かっているので、分かるほうを出す。
  // 提出が揃えば pending が空になり、この一文は自分で消える。
  const pendingNote = schedulePendingNote(insights);
  if (pendingNote && elements.schedulePendingNote) {
    elements.schedulePendingNote.textContent = pendingNote.short;
    elements.schedulePendingNote.title = pendingNote.long;
    elements.schedulePendingNote.setAttribute("aria-label", pendingNote.long);
    elements.schedulePendingNote.hidden = false;
  }
  elements.maidFilterDetails.open =
    !window.matchMedia("(max-width: 45rem)").matches;
  syncViewMode();
  renderMaidFilters();
  renderCalendar();
})();
