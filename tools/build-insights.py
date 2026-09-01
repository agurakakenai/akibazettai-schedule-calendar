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

WINDOW_DAYS = 365      # 傾向を出す対象期間
HISTORY_DAYS = 180     # カレンダーに実績として持たせる期間
SHRINK = 5.0           # シフト別の比率を全体値へ寄せる強さ（サンプル不足対策）


def load_csv(name):
    with open(os.path.join(DATA, name), encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


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

    base, typical = {}, {}
    for sh in SHIFTS:
        cnt = collections.Counter()
        sizes = collections.defaultdict(list)
        for d in dates:
            for sid, maids in cell.get((d, sh), {}).items():
                cnt[sid] += 1
                sizes[sid].append(len(maids))
        base[sh] = {sid: rate(cnt[sid], nd) for sid in IDS}
        typical[sh] = {sid: (round(sum(sizes[sid]) / len(sizes[sid]), 1) if sizes[sid] else 0) for sid in IDS}

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
    worked = collections.defaultdict(set)
    at = collections.defaultdict(lambda: collections.defaultdict(set))
    open_for = collections.defaultdict(set)
    worked_sh = collections.defaultdict(collections.Counter)
    for (d, sh), stores in cell.items():
        if d < cut or d in events:
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
    accounts, account_status = {}, {}
    if os.path.exists(os.path.join(DATA, 'accounts.csv')):
        for a in load_csv('accounts.csv'):
            if a.get('handle'):
                accounts[a['name']] = a['handle']
                account_status[a['name']] = a.get('source') or ''

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
            'recentShifts31': recent_count31.get(m, 0),
            # サイト未掲載だが公開の *_zettai アカウントがあり直近1か月にお給仕あり
            'promoted': bool(accounts.get(m)) and account_status.get(m) == '本人確認済み'
                        and recent_count31.get(m, 0) > 0,
        }
    unlisted = dict(sorted(unlisted.items(), key=lambda kv: (-kv[1]['promoted'], -kv[1]['recentShifts'])))

    for nm, t in tendency.items():
        if t is not None:
            t['x'] = accounts.get(nm) or accounts.get(t['alias'] or nm)

    hist_from = (last_d - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    actual = {}
    for d in all_dates:
        if d < hist_from:
            continue
        entry = {sh: sorted(open_at(d, sh)) for sh in SHIFTS if (d, sh) in cell}
        if entry:
            actual[d] = entry

    return {
        'generatedAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'historyRange': {'from': all_dates[0], 'to': last},
        'sampleWindow': {'from': cut, 'to': last, 'days': nd},
        'stores': [{'id': i, 'short': s, 'name': n} for i, _, s, n in STORES],
        'shifts': SHIFTS,
        'weekdayOrigin': 'sunday',
        'baseOpenRate': base,
        'typicalHeadcount': typical,
        'weekdayOpenRate': weekday,
        'shiftPattern': shift_pattern,
        'openCountPerShift': open_count,
        'shiftSplitGivenOpen': shift_split,
        'rotation': {
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
        'maidTendency': tendency,
        'unlistedMaids': unlisted,
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
