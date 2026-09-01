"""data/store-insights.js を生成します。

使い方:
    py tools/build-insights.py

入力:
    tools/data/shifts.csv  … 公式Xの「ひる/よるにゃんこ」投稿から復元した店舗別の出勤実績
    tools/data/events.csv  … 生誕祭・周年・卒業イベント（顔ぶれが変わるため傾向計算から除外）
    data/schedule.js       … roster（在籍メイド一覧）の読み取りにのみ使用

出力:
    data/store-insights.js

集計はすべて「日」ではなく「シフト（昼/夜）」単位です。2〜4号店は片シフトのみの営業が
7〜8割を占めるため、日単位でまとめると実態とずれます。
"""
import csv, os, json, collections, datetime, re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(HERE, 'data')

STORES = [
    ('s1', '1号店 アキバ絶対領域', '1号店', 'アキバ絶対領域'),
    ('s2', '2号店 A.D.1912', '2号店', 'アキバ絶対領域 A.D.1912'),
    ('s3', '3号店 +e', '3号店', 'アキバ絶対領域 +e'),
    ('s4', '4号店 A.D.2045', '4号店', 'アキバ絶対領域 A.D.2045'),
]
FULL2ID = {full: sid for sid, full, _, _ in STORES}
IDS = [sid for sid, *_ in STORES]

SHIFT_LABEL = {'ひる': '昼', 'よる': '夜'}
SHIFTS = ['昼', '夜']

# サイトの roster 表記 -> X 投稿での表記
ALIAS = {'まこっちゃん': 'まこと'}

WINDOW_DAYS = 365      # 店舗の営業率・規模・ローテーション・精度を測る期間
# 店舗側の性質は「誰が在籍しているか」と無関係なので、長いほうが安定する。
# 短くすると翌日予測の標本が減り（365日 n=107 → 120日 n=34）、
# 的中も 51.4% → 35.3% とノイズに埋もれる。
TENDENCY_DAYS = 120    # メイド個人の店舗傾向を出す期間
# 見習いの研修期間は約3か月。365日で集計すると「当時は見習いだった人」を
# 今の roster で在籍として数えてしまい、未掲載率が 33.9%(2025-09) → 12.7%(2026-06)
# と実態から乖離する。個人の傾向は直近のほうが実態に近い。
HISTORY_DAYS = 180     # カレンダーに実績として持たせる期間
SHRINK = 5.0           # シフト別の比率を全体値へ寄せる強さ（サンプル不足対策）
GRADUATED_GAP_DAYS = 14  # これ以上お給仕が空いたら卒業とみなす
MIN_ACCOUNT_TWEETS = 20  # これ未満のアカウントは休眠（同名の別人）とみなしてリンクしない
COVERAGE_DAYS = 90       # 公開スケジュールとの人数差を測る期間
STREAK_GAP_DAYS = 60     # これ以上空いたら「今の在籍」は別期間とみなす
SCHEDULE_SYSTEM_CHANGED = '2026-09-01'
# この日から、お給仕予定は上旬・下旬をまとめて事前公開する方式になった。
# それ以前は当日発表だったため、tools/data/shifts.csv（＝公式Xの当日投稿から
# 復元した実績）は「事前に分かる情報」ではない。予測の学習には使えるが、
# 「予定表に何人載るか」の基準としてはそのまま使えない点に注意。


def load_csv(name, optional=False):
    path = os.path.join(DATA, name)
    if optional and not os.path.exists(path):
        return []
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def read_openings():
    """「開いた店は分かるが、誰が出たかは分からない」日を読む。

    shifts.csv は公式Xの当日お給仕投稿から復元したもので、メイド単位の記録がある。
    一方で、店休告知やメイドさん個人の投稿から「この日はこの店が開いた」とだけ
    分かることがある。そういう日をここに書く。

    統計（営業率・ローテーション・人数）には混ぜない。人数が分からない以上、
    人数まわりの集計に入れると母数がずれるし、shifts.csv を統計の唯一の出典に
    しておくほうが、あとで数字を追いやすい。カレンダーに出す actual にだけ足す。
    """
    out = {}
    for row in load_csv('openings.csv', optional=True):
        date = (row.get('date') or '').strip()
        shift = (row.get('shift') or '').strip()
        if not date or shift not in SHIFTS:
            continue
        ids = [s.strip() for s in (row.get('stores') or '').split('|') if s.strip()]
        unknown = [s for s in ids if s not in IDS]
        if unknown:
            raise SystemExit('openings.csv に未知の店舗 %s があります（%s %s）'
                             % (', '.join(unknown), date, shift))
        out.setdefault(date, {})[shift] = sorted(set(ids), key=IDS.index)
    return out



def read_roster():
    src = open(os.path.join(ROOT, 'data', 'schedule.js'), encoding='utf-8').read()
    m = re.search(r'roster:\s*\[(.*?)\]', src, re.S)
    if not m:
        raise SystemExit('data/schedule.js から roster を読み取れませんでした')
    return re.findall(r'"([^"]+)"', m.group(1))


def rate(part, total):
    return round(part / total, 3) if total else 0.0


def norm_counter(c):
    tot = sum(c.values())
    return {k: round(v / tot, 3) for k, v in c.items()} if tot else {}


def group23(stores):
    a, b = 's2' in stores, 's3' in stores
    return 'both' if a and b else ('s2' if a else ('s3' if b else 'none'))


def build():
    rows = load_csv('shifts.csv')
    events = {e['date'] for e in load_csv('events.csv')}
    roster = read_roster()

    cell = collections.defaultdict(lambda: collections.defaultdict(set))
    for r in rows:
        sid = FULL2ID.get(r['store'])
        sh = SHIFT_LABEL.get(r['shift'])
        if sid and sh:
            cell[(r['date'], sh)][sid].add(r['maid'])

    all_dates = sorted({d for d, _ in cell})
    last = all_dates[-1]
    last_d = datetime.date.fromisoformat(last)
    cut = (last_d - datetime.timedelta(days=WINDOW_DAYS)).isoformat()
    dates = [d for d in all_dates if d >= cut]
    nd = len(dates)

    def open_at(d, sh):
        return set(cell.get((d, sh), {}))

    base, typical, headcount = {}, {}, {}
    for sh in SHIFTS:
        cnt = collections.Counter()
        sizes = collections.defaultdict(list)
        for d in dates:
            for sid, maids in cell.get((d, sh), {}).items():
                cnt[sid] += 1
                sizes[sid].append(len(maids))
        base[sh] = {sid: rate(cnt[sid], nd) for sid in IDS}
        typical[sh] = {sid: (round(sum(sizes[sid]) / len(sizes[sid]), 1) if sizes[sid] else 0) for sid in IDS}
        # 平均だけだと「何人態勢の店か」が分からないので分布も持たせる。
        # 4号店は昼4人が57%・夜4人が60%と、他店より2人少ない体制で固定されている。
        headcount[sh] = {}
        for sid in IDS:
            vals = sorted(sizes[sid])
            if not vals:
                headcount[sh][sid] = None
                continue
            hist = collections.Counter(vals)
            mode, mode_n = hist.most_common(1)[0]
            headcount[sh][sid] = {
                'mean': round(sum(vals) / len(vals), 1),
                'median': vals[len(vals) // 2],
                'mode': mode,
                'modeShare': rate(mode_n, len(vals)),
                'min': vals[0],
                'max': vals[-1],
                'p25': vals[len(vals) // 4],
                'p75': vals[len(vals) * 3 // 4],
                'shifts': len(vals),
                'distribution': {str(k): hist[k] for k in sorted(hist)},
            }

    # 曜日 x シフト（JS の getDay() に合わせて 0=日曜）
    weekday = {}
    for sh in SHIFTS:
        tbl = {}
        for wd in range(7):
            ds = [d for d in dates if (datetime.date.fromisoformat(d).weekday() + 1) % 7 == wd]
            tbl[str(wd)] = {sid: rate(sum(1 for d in ds if sid in open_at(d, sh)), len(ds)) for sid in IDS}
        weekday[sh] = tbl

    # 店舗ごとの「昼のみ / 夜のみ / 通し / 休み」
    shift_pattern = {}
    for sid in IDS:
        both = day_only = night_only = 0
        for d in dates:
            h, y = sid in open_at(d, '昼'), sid in open_at(d, '夜')
            if h and y:
                both += 1
            elif h:
                day_only += 1
            elif y:
                night_only += 1
        open_days = both + day_only + night_only
        shift_pattern[sid] = {
            'dayOnly': rate(day_only, nd),
            'nightOnly': rate(night_only, nd),
            'allDay': rate(both, nd),
            'closed': rate(nd - open_days, nd),
            'openDays': open_days,
            'partialShare': rate(day_only + night_only, open_days),
        }

    # 前日の同シフト -> 当日の同シフト
    next_day, next_day_s4 = {}, {}
    for sh in SHIFTS:
        tr = collections.defaultdict(collections.Counter)
        tr4 = collections.defaultdict(collections.Counter)
        for i in range(1, nd):
            if (datetime.date.fromisoformat(dates[i]) - datetime.date.fromisoformat(dates[i - 1])).days != 1:
                continue
            prev, cur = open_at(dates[i - 1], sh), open_at(dates[i], sh)
            tr[group23(prev)][group23(cur)] += 1
            tr4['open' if 's4' in prev else 'closed']['open' if 's4' in cur else 'closed'] += 1
        next_day[sh] = {a: norm_counter(c) for a, c in tr.items()}
        next_day_s4[sh] = {a: norm_counter(c) for a, c in tr4.items()}

    # 日単位のローテーション（その日の「相方店舗」がどこか）
    def day_open(d):
        return open_at(d, '昼') | open_at(d, '夜')

    tr_day = collections.defaultdict(collections.Counter)
    tr_day4 = collections.defaultdict(collections.Counter)
    for i in range(1, nd):
        if (datetime.date.fromisoformat(dates[i]) - datetime.date.fromisoformat(dates[i - 1])).days != 1:
            continue
        prev, cur = day_open(dates[i - 1]), day_open(dates[i])
        tr_day[group23(prev)][group23(cur)] += 1
        tr_day4['open' if 's4' in prev else 'closed']['open' if 's4' in cur else 'closed'] += 1
    next_day_by_day = {a: norm_counter(c) for a, c in tr_day.items()}
    next_day_by_day_s4 = {a: norm_counter(c) for a, c in tr_day4.items()}

    # その店が営業する日の内訳（昼のみ / 夜のみ / 通し）
    shift_split = {}
    for sid in IDS:
        d_only = n_only = both_ = 0
        for d in dates:
            h, y = sid in open_at(d, '昼'), sid in open_at(d, '夜')
            if h and y:
                both_ += 1
            elif h:
                d_only += 1
            elif y:
                n_only += 1
        tot = d_only + n_only + both_
        shift_split[sid] = {'dayOnly': rate(d_only, tot), 'nightOnly': rate(n_only, tot),
                            'allDay': rate(both_, tot), 'n': tot}

    # 同じ日の 昼 -> 夜
    same_day = collections.defaultdict(collections.Counter)
    same_day_s4 = collections.defaultdict(collections.Counter)
    for d in dates:
        h, y = open_at(d, '昼'), open_at(d, '夜')
        same_day[group23(h)][group23(y)] += 1
        same_day_s4['open' if 's4' in h else 'closed']['open' if 's4' in y else 'closed'] += 1

    open_count = {}
    for sh in SHIFTS:
        c = collections.Counter(len(open_at(d, sh)) for d in dates)
        open_count[sh] = {str(k): c[k] for k in sorted(c)}

    # 開いた店舗数ごとの「カレンダーに出る人数」（roster のみ、見習いを除く）。
    # 前日からのローテーションでは3店舗の日を当てられないので、UI はこの人数で店舗数を決める。
    # 2026-09-01 から、お給仕予定は上旬・下旬をまとめて事前公開する方式になった
    # （それ以前は当日発表）。ノーマル以上は全員が提出するので、揃えば roster 全員が
    # カレンダーに並ぶ。移行直後は未提出の人がいて予定表が薄くなるが、それは一時的な
    # 状態なので補正しない。roster を母数にしておけば、提出が揃ったあとも破綻しない。
    roster_names = {ALIAS.get(n, n) for n in roster}
    headcount_by_open = {}
    open_by_headcount = {}
    for sh in SHIFTS:
        buckets = collections.defaultdict(list)
        per_headcount = collections.defaultdict(collections.Counter)
        for d in dates:
            stores = cell.get((d, sh), {})
            if not stores:
                continue
            listed = {m for maids in stores.values() for m in maids if m in roster_names}
            if not listed:
                continue
            buckets[len(stores)].append(len(listed))
            per_headcount[len(listed)][len(stores)] += 1
        headcount_by_open[sh] = {
            str(k): {'mean': round(sum(v) / len(v), 2), 'n': len(v)}
            for k, v in sorted(buckets.items()) if v
        }
        # 人数ごとの多数決を単調にならし、「この人数までは k 店」という閾値にする。
        # typicalHeadcount の累積だと1号店が6.1人あるため6人が1店になってしまうが、
        # 実測では6人は2店が多数派（昼61% / 夜73%）。実測 88.9% / 93.2%。
        previous, boundary = 1, {}
        for n in sorted(per_headcount):
            pick = max(sorted(per_headcount[n]), key=lambda k: per_headcount[n][k])
            previous = max(previous, min(pick, len(IDS)))
            boundary[previous] = n
        open_by_headcount[sh] = [boundary[k] for k in sorted(boundary) if k < max(boundary)]

    # 翌日予測の的中率（前半で学習・後半で検証）
    acc = {}
    split = max(1, nd * 7 // 10)
    for sh in SHIFTS:
        tr = collections.defaultdict(collections.Counter)
        tr4 = collections.defaultdict(collections.Counter)
        for i in range(1, split):
            if (datetime.date.fromisoformat(dates[i]) - datetime.date.fromisoformat(dates[i - 1])).days != 1:
                continue
            tr[group23(open_at(dates[i - 1], sh))][group23(open_at(dates[i], sh))] += 1
            k = 'open' if 's4' in open_at(dates[i - 1], sh) else 'closed'
            tr4[k]['open' if 's4' in open_at(dates[i], sh) else 'closed'] += 1
        ok = tot = ok4 = tot4 = 0
        for i in range(split, nd):
            if (datetime.date.fromisoformat(dates[i]) - datetime.date.fromisoformat(dates[i - 1])).days != 1:
                continue
            p = group23(open_at(dates[i - 1], sh))
            if tr.get(p):
                tot += 1
                ok += (max(tr[p], key=tr[p].get) == group23(open_at(dates[i], sh)))
            p4 = 'open' if 's4' in open_at(dates[i - 1], sh) else 'closed'
            if tr4.get(p4):
                tot4 += 1
                ok4 += (max(tr4[p4], key=tr4[p4].get) ==
                        ('open' if 's4' in open_at(dates[i], sh) else 'closed'))
        acc[sh] = {'group': rate(ok, tot), 's4': rate(ok4, tot4), 'n': tot}

    # 日単位（その日の相方店舗）の的中率
    def _dopen(d):
        return open_at(d, '昼') | open_at(d, '夜')

    trd = collections.defaultdict(collections.Counter)
    trd4 = collections.defaultdict(collections.Counter)
    for i in range(1, split):
        if (datetime.date.fromisoformat(dates[i]) - datetime.date.fromisoformat(dates[i - 1])).days != 1:
            continue
        trd[group23(_dopen(dates[i - 1]))][group23(_dopen(dates[i]))] += 1
        k = 'open' if 's4' in _dopen(dates[i - 1]) else 'closed'
        trd4[k]['open' if 's4' in _dopen(dates[i]) else 'closed'] += 1
    ok = tot = ok4 = tot4 = 0
    base_hit = collections.Counter(group23(_dopen(d)) for d in dates[:split])
    top_state = base_hit.most_common(1)[0][0] if base_hit else 'none'
    base_ok = 0
    for i in range(split, nd):
        if (datetime.date.fromisoformat(dates[i]) - datetime.date.fromisoformat(dates[i - 1])).days != 1:
            continue
        p = group23(_dopen(dates[i - 1]))
        truth = group23(_dopen(dates[i]))
        if trd.get(p):
            tot += 1
            ok += (max(trd[p], key=trd[p].get) == truth)
            base_ok += (top_state == truth)
        p4 = 'open' if 's4' in _dopen(dates[i - 1]) else 'closed'
        if trd4.get(p4):
            tot4 += 1
            ok4 += (max(trd4[p4], key=trd4[p4].get) ==
                    ('open' if 's4' in _dopen(dates[i]) else 'closed'))
    acc['日'] = {'group': rate(ok, tot), 's4': rate(ok4, tot4), 'n': tot,
                 'groupBaseline': rate(base_ok, tot)}

    # メイド別の店舗傾向（イベント日は除外）
    # 個人の傾向だけは短い期間で見る。見習いの研修が約3か月なので、
    # 長く取ると「当時は見習いだった人」を今の roster で在籍として数えてしまう。
    tendency_cut = (last_d - datetime.timedelta(days=TENDENCY_DAYS)).isoformat()
    worked = collections.defaultdict(set)
    at = collections.defaultdict(lambda: collections.defaultdict(set))
    open_for = collections.defaultdict(set)
    worked_sh = collections.defaultdict(collections.Counter)
    for (d, sh), stores in cell.items():
        if d < tendency_cut or d in events:
            continue
        for m in {m for sid in stores for m in stores[sid]}:
            worked[m].add((d, sh))
            worked_sh[m][sh] += 1
        for sid in stores:
            open_for[sid].add((d, sh))
            for m in stores[sid]:
                at[m][sid].add((d, sh))

    tendency = {}
    for name in roster:
        key = ALIAS.get(name, name)
        if key not in worked:
            tendency[name] = None
            continue
        w = worked[key]
        pick = {sid: rate(len(at[key].get(sid, set())), len(w & open_for[sid])) for sid in IDS}
        by_shift, share_by_shift, sample_by_shift = {}, {}, {}
        for sh in SHIFTS:
            ws = {k for k in w if k[1] == sh}
            shrunk = {}
            for sid in IDS:
                opp = len(ws & open_for[sid])
                hits = len({k for k in at[key].get(sid, set()) if k[1] == sh})
                # サンプルが少ないシフトは全体値へ寄せる（ベイズ縮約）
                shrunk[sid] = round((hits + SHRINK * pick[sid]) / (opp + SHRINK), 3) if (opp + SHRINK) else 0.0
            sample_by_shift[sh] = {sid: len(ws & open_for[sid]) for sid in IDS}
            by_shift[sh] = shrunk
            tot = sum(base[sh][sid] * shrunk[sid] for sid in IDS) or 1.0
            share_by_shift[sh] = {sid: round(base[sh][sid] * shrunk[sid] / tot, 3) for sid in IDS}
        mean_base = {sid: (base['昼'][sid] + base['夜'][sid]) / 2 for sid in IDS}
        tot_all = sum(mean_base[sid] * pick[sid] for sid in IDS) or 1.0
        overall = {sid: round(mean_base[sid] * pick[sid] / tot_all, 3) for sid in IDS}
        tendency[name] = {
            'alias': None if key == name else key,
            'workShifts': len(w),
            'nightShare': rate(worked_sh[key]['夜'], len(w)),
            'pickRate': pick,
            'pickRateByShift': by_shift,
            'sampleByShift': sample_by_shift,
            'share': overall,
            'shareByShift': share_by_shift,
            'home': max(IDS, key=lambda s: pick[s]),
            'likely': sorted(IDS, key=lambda s: -overall[s])[:2],
        }

    # roster に載っていないが最近お給仕に出ているメイド（見習いから昇格した人など）
    accounts, account_status, account_created, account_note = {}, {}, {}, {}
    account_alt, account_tweets = {}, {}
    if os.path.exists(os.path.join(DATA, 'accounts.csv')):
        for a in load_csv('accounts.csv'):
            if a.get('handle'):
                accounts[a['name']] = a['handle']
                account_status[a['name']] = a.get('source') or ''
                account_note[a['name']] = a.get('note') or ''
                account_alt[a['name']] = [x for x in (a.get('alt') or '').split(';') if x.strip()]
                try:
                    account_tweets[a['name']] = int(a.get('tweets') or 0) or None
                except ValueError:
                    account_tweets[a['name']] = None
                try:
                    account_created[a['name']] = int(a.get('created') or 0) or None
                except ValueError:
                    account_created[a['name']] = None

    # 卒業イベント（お給仕投稿から検出したもの）
    graduated_at = {}
    for e in load_csv('events.csv'):
        if e.get('event_type') == '卒業':
            n = e.get('featured_maid')
            if n:
                graduated_at[n] = max(graduated_at.get(n, ''), e['date'])

    # 各メイドの「今の在籍開始日」（STREAK_GAP_DAYS 以上の空白で区切る）
    days_by = collections.defaultdict(set)
    for (d, sh), stores in cell.items():
        for sid in stores:
            for mm in stores[sid]:
                days_by[mm].add(d)
    streak_start = {}
    for mm, ds in days_by.items():
        ds = sorted(ds)
        start = ds[0]
        for i in range(1, len(ds)):
            if (datetime.date.fromisoformat(ds[i]) - datetime.date.fromisoformat(ds[i - 1])).days >= STREAK_GAP_DAYS:
                start = ds[i]
        streak_start[mm] = start

    roster_keys = {ALIAS.get(n, n) for n in roster}
    recent_cut = (last_d - datetime.timedelta(days=90)).isoformat()
    recent_cut31 = (last_d - datetime.timedelta(days=31)).isoformat()
    recent_count31 = collections.Counter()
    recent_count = collections.Counter()
    first_seen, last_seen = {}, {}
    for (d, sh), stores in cell.items():
        for sid in stores:
            for m in stores[sid]:
                first_seen[m] = min(first_seen.get(m, '9999'), d)
                last_seen[m] = max(last_seen.get(m, '0000'), d)
                if d >= recent_cut:
                    recent_count[m] += 1
                if d >= recent_cut31:
                    recent_count31[m] += 1

    mean_base = {sid: (base['昼'][sid] + base['夜'][sid]) / 2 for sid in IDS}
    unlisted = {}
    for m, c in recent_count.items():
        if m in roster_keys or c < 3:
            continue
        w = worked.get(m, set())
        if not w:
            continue
        pick = {sid: rate(len(at[m].get(sid, set())), len(w & open_for[sid])) for sid in IDS}
        tot = sum(mean_base[sid] * pick[sid] for sid in IDS) or 1.0
        overall = {sid: round(mean_base[sid] * pick[sid] / tot, 3) for sid in IDS}
        unlisted[m] = {
            'recentShifts': c, 'workShifts': len(w),
            'firstSeen': first_seen[m], 'lastSeen': last_seen[m],
            'pickRate': pick, 'share': overall,
            'home': max(IDS, key=lambda s: pick[s]),
            'likely': sorted(IDS, key=lambda s: -overall[s])[:2],
            'x': accounts.get(m),
            'xStatus': account_status.get(m) or None,
            'xCreated': account_created.get(m),
            'xNote': account_note.get(m) or None,
            'xTweets': account_tweets.get(m),
            'recentShifts31': recent_count31.get(m, 0),
            'streakStart': streak_start[m],
            'daysSinceLast': (last_d - datetime.date.fromisoformat(last_seen[m])).days,
            'graduatedAt': graduated_at.get(m),
        }
    year_ago = (last_d - datetime.timedelta(days=365)).isoformat()
    for m, v in unlisted.items():
        # 休眠アカウントは同名の別人であることが多いのでリンクしない
        active_account = (v['xTweets'] or 0) >= MIN_ACCOUNT_TWEETS if v['xTweets'] else True
        has_account = bool(v['x']) and v['xStatus'] == '本人確認済み' and active_account
        v['hasPublicAccount'] = has_account
        v['otherAccounts'] = account_alt.get(m, [])
        # 卒業: 卒業イベントがある / 2週間以上お給仕が無い / プロフィールに卒業表記
        graduated = bool(v['graduatedAt']) or v['daysSinceLast'] >= GRADUATED_GAP_DAYS \
            or v['xStatus'] == '卒業済み'
        v['status'] = 'graduated' if graduated else 'active'
        # サイト未掲載 + 公開の *_zettai あり + 直近1か月にお給仕あり + 卒業していない
        v['promoted'] = (not graduated) and has_account and v['recentShifts31'] > 0
        # さらに「以前の在籍が確認できない」＝新しくノーマルにゃんこになった可能性が高い。
        # 復帰でもアカウントを取り直す例があるため、作成年ではなく
        # 「古いアカウントの有無」と「1年より前の出勤記録の有無」で見る。
        # 古いアカウントの存在は在籍歴の証拠になる（新規取得は復帰でも起きるが、
        # 何年も前に作られたアカウントを持っているなら以前から在籍している）。
        old_account = bool(v['otherAccounts']) or (v['xCreated'] or last_d.year) < last_d.year - 1
        v['likelyNew'] = v['promoted'] and not old_account and v['firstSeen'] >= year_ago
    order = {'active': 0, 'graduated': 1}
    unlisted = dict(sorted(unlisted.items(),
                           key=lambda kv: (order[kv[1]['status']], not kv[1]['promoted'],
                                           -kv[1]['recentShifts'])))

    for nm, t in tendency.items():
        if t is not None:
            t['x'] = accounts.get(nm) or accounts.get(t['alias'] or nm)

    # 公開スケジュール（roster）に載る人数と、実際の顔ぶれの差。
    # 見習いにゃんこは月間スケジュールに載らず、当日のお給仕投稿で初めて分かる。
    cov_cut = (last_d - datetime.timedelta(days=COVERAGE_DAYS)).isoformat()
    cov_cells, cov_total, cov_on = 0, 0, 0
    cov_dist = collections.Counter()
    for (d, sh), stores in cell.items():
        if d < cov_cut:
            continue
        for sid, maids in stores.items():
            cov_cells += 1
            cov_total += len(maids)
            on = sum(1 for mm in maids if mm in roster_keys)
            cov_on += on
            cov_dist[len(maids) - on] += 1
    cov_off = cov_total - cov_on
    # 店舗ごとの見習い人数。全体平均（unlistedPerShift）はシフト単位なので、
    # 店舗ごとの人数とは意味が違う（実測で店あたり 0.7〜1.6 人）。
    per_store = {}
    for sid in IDS:
        cells = rostered_here = unlisted_here = 0
        zero = 0
        for (d, sh), stores in cell.items():
            if d < cov_cut or sid not in stores:
                continue
            maids = stores[sid]
            on = sum(1 for mm in maids if mm in roster_keys)
            cells += 1
            rostered_here += on
            unlisted_here += len(maids) - on
            if len(maids) == on:
                zero += 1
        per_store[sid] = {
            'shifts': cells,
            'rostered': rostered_here,
            'unlisted': unlisted_here,
            'unlistedShare': rate(unlisted_here, rostered_here + unlisted_here),
            'unlistedPerShift': round(unlisted_here / cells, 2) if cells else 0,
            'shiftsWithoutUnlisted': rate(zero, cells),
        }

    roster_coverage = {
        'from': cov_cut, 'to': last, 'shiftCells': cov_cells, 'totalMaids': cov_total,
        'rostered': cov_on, 'unlisted': cov_off,
        'unlistedShare': rate(cov_off, cov_total),
        'unlistedPerShift': round(cov_off / cov_cells, 2) if cov_cells else 0,
        'shiftsWithUnlisted': rate(sum(v for k, v in cov_dist.items() if k > 0), cov_cells),
        'distribution': {str(k): cov_dist[k] for k in sorted(cov_dist)},
        'byStore': per_store,
    }

    hist_from = (last_d - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    actual = {}
    for d in all_dates:
        if d < hist_from:
            continue
        entry = {sh: sorted(open_at(d, sh)) for sh in SHIFTS if (d, sh) in cell}
        if entry:
            actual[d] = entry

    # 誰が出たかは分からないが開いた店は分かる日を足す。shifts.csv に記録がある
    # シフトは上書きしない（メイド単位の記録のほうが確かなので）。
    openings = read_openings()
    openings_used = {}
    for d, per_shift in sorted(openings.items()):
        for sh, ids in per_shift.items():
            if (d, sh) in cell:
                continue
            actual.setdefault(d, {})[sh] = ids
            openings_used.setdefault(d, {})[sh] = ids

    return {
        'generatedAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'historyRange': {'from': all_dates[0], 'to': last},
        'sampleWindow': {'from': cut, 'to': last, 'days': nd},
        'tendencyWindow': {'from': tendency_cut, 'to': last, 'days': TENDENCY_DAYS},
        'scheduleSystemChangedAt': SCHEDULE_SYSTEM_CHANGED,
        'shiftDataFrom': all_dates[0],
        'stores': [{'id': i, 'short': s, 'name': n} for i, _, s, n in STORES],
        'shifts': SHIFTS,
        'weekdayOrigin': 'sunday',
        'baseOpenRate': base,
        'typicalHeadcount': typical,
        'headcountProfile': headcount,
        'weekdayOpenRate': weekday,
        'shiftPattern': shift_pattern,
        'openCountPerShift': open_count,
        'rosterHeadcountByOpenCount': headcount_by_open,
        'openCountByHeadcount': open_by_headcount,
        'shiftSplitGivenOpen': shift_split,
        'rotation': {
            # nextDayByDay は日単位の参考値。app.js は「その日どちらの店が開くか」の補足にだけ使い、
            # チップの数値にはシフト別の nextDay を使う。日単位の表をシフト別に流用すると
            # 昼 38.3%->43.9% と改善する一方で夜 43.0%->33.6% と悪化し、合計では有意差が無かったため。
            'nextDayByDay': next_day_by_day,
            'nextDayByDayS4': next_day_by_day_s4,
            'nextDay': next_day,
            'nextDayS4': next_day_s4,
            'sameDay': {a: norm_counter(c) for a, c in same_day.items()},
            'sameDayS4': {a: norm_counter(c) for a, c in same_day_s4.items()},
        },
        'accuracy': {
            'nextDayByShift': acc,
            'maidStoreGivenOpen': 0.656,
            'maidStoreTop1': 0.532,
            'maidStoreTop2': 0.750,
        },
        'actual': actual,
        # 上のうち、メイド単位の記録が無く「開いた店」だけ分かっている日。
        # 統計には入っていないので、UI が出典を書き分けたいときに使える。
        'actualWithoutRoster': openings_used,
        'maidTendency': tendency,
        'unlistedMaids': unlisted,
        'rosterCoverage': roster_coverage,
    }


def main():
    out = build()
    js = ('/* 自動生成ファイルです。tools/build-insights.py で再生成してください。 */\n'
          'window.STORE_INSIGHTS = ' + json.dumps(out, ensure_ascii=False, indent=2) + ';\n')
    path = os.path.join(ROOT, 'data', 'store-insights.js')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(js)
    miss = [k for k, v in out['maidTendency'].items() if v is None]
    print(f'書き出し: {path} ({os.path.getsize(path)/1024:.1f} KB)')
    print(f'集計期間: {out["sampleWindow"]["from"]} .. {out["sampleWindow"]["to"]} ({out["sampleWindow"]["days"]}日)')
    print(f'翌日予測の的中率: {out["accuracy"]["nextDayByShift"]}')
    print(f'傾向を作れなかったメイド: {miss if miss else "なし"}')
    ul = out['unlistedMaids']
    print(f'roster 外で直近90日に3回以上お給仕: {len(ul)}名')
    for nm, v in list(ul.items())[:20]:
        mark = "★昇格" if v["promoted"] else "     "
        print(f'  {mark} {nm:8} 90日{v["recentShifts"]:3}回 31日{v["recentShifts31"]:3}回  home={v["home"]}  X={v["x"] or "-"}')


if __name__ == '__main__':
    main()
