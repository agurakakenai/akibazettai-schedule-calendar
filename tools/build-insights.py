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

# 「予定表に何人載ると何店開くか」だけは、古い記録を軽くして数える。
# 直近1年で1シフトあたりの平均店舗数が 1.64(2025-09) → 2.04(2026-08) と
# 動いており、365日を平等に数えると古い規模に引っぱられる。
#
# 窓を 90 日に縮めるのは危うい。walk-forward では 88.6% → 90.8% (p=0.027) と
# 良くなるが、いまのデータで作ると昼の境目が「6名で1店だった3日」だけに
# 支えられた [6,12] になる。365日で見れば6名は2店が23日ある。
# 重みなら境目の標本を捨てずに済む（半減期180日で境目の重み 11.7）。
#
#   walk-forward (2025-12-06 以降 519 シフト)
#     重みなし365日  88.6%
#     半減期 120日   90.4%  (10勝1敗 p=0.012)
#     半減期 180日   90.2%  ( 9勝1敗 p=0.022)  ← 採用
#     半減期 365日   89.8%  ( 7勝1敗 p=0.070)
#
# 180 を選んだのは、有意なものの中で境目を支える標本がいちばん厚いから。
# 120 との的中の差 0.2pt は誤差。いまのデータでの閾値はどちらも [5,12] で
# 重みなしと変わらないので、この変更で表示は動かない。
HEADCOUNT_HALF_LIFE = 180
HISTORY_DAYS = 180     # カレンダーに実績として持たせる期間
SHRINK = 5.0           # シフト別の比率を全体値へ寄せる強さ（サンプル不足対策）
GRADUATED_GAP_DAYS = 14  # これ以上お給仕が空いたら卒業とみなす
MIN_ACCOUNT_TWEETS = 20  # これ未満のアカウントは休眠（同名の別人）とみなしてリンクしない
COVERAGE_DAYS = 90       # 公開スケジュールとの人数差を測る期間
STREAK_GAP_DAYS = 60     # これ以上空いたら「今の在籍」は別期間とみなす
TRAINING_DAYS = 80       # 見習いの期間。研修は約3か月と聞いており、
# 昇格日が分かっている4名の実測はデビューから 68 / 75 / 82 / 91 日（平均79日）だった。
# 分かっていない人はこの日数で埋める（data/schedule.js の promotedAt が優先）。
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


def read_debuts():
    """新人にゃんこの告知から取れた「お給仕を始めた日」。

    公式Xが「新人にゃんこの『◯◯』ちゃん」と紹介するので、その日付を初日とする。
    記録の空白を初日と読み違える streak_start と違い、お店が言った日そのもの。
    告知が無い人は記録上の初出で代用する（呼び出し側の責任）。
    """
    out = {}
    for row in load_csv('debuts.csv', optional=True):
        name = ALIAS.get(row.get('name'), row.get('name'))
        date = row.get('date')
        if name and date and (name not in out or date < out[name]):
            out[name] = date
    return out


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


def read_home_store():
    """公式サイトに載っている配属店（data/schedule.js の homeStore）。

    お給仕の実績が薄い人ほど、この配属が効く。実測では、その期間に記録が
    まったく無い人の店を当てる的中が 66.2% -> 73.1%、1〜5回の人で 51.4% -> 55.1%。
    41回以上ある人は +0.1pt しか動かない（自分の実績のほうが強いので当然）。
    見習いから上がったばかりの人が roster に入ったとき、初日から使える。
    """
    path = os.path.join(ROOT, 'data', 'schedule.js')
    src = open(path, encoding='utf-8').read()
    m = re.search(r'homeStore:\s*\{(.*?)\n  \}', src, re.S)
    if not m:
        return {}
    out = {}
    for name, sid in re.findall(r'"([^"]+)":\s*"([^"]+)"', m.group(1)):
        if sid in IDS:
            out[name] = sid
    return out


def rate(part, total):
    return round(part / total, 3) if total else 0.0


# 公式配属を pickRate の事前分布として使う強さ。「何回ぶんの実績に相当するか」で、
# 2.0 は「公式サイトの配属は2回の出勤ぶんの重み」という意味になる。
# 実測で 0.5〜2.0 が有意（+0.3〜0.6pt, p<0.05）、3.0 以上は有意でなくなる。
POSTED_PRIOR = 2.0
POSTED_HIT = 0.60    # 配属店にいる割合の目安
POSTED_MISS = 0.15   # 配属先でない店にいる割合の目安

# チップの確率が言ったとおりに当たるかを測る設定
CALIBRATION_REFIT_DAYS = 28   # この日数ごとに pickRate を作り直して遡る
CALIBRATION_MIN_TRAIN = 60    # 訓練に使うシフトがこれ未満なら測らない
CALIBRATION_MIN_SAMPLE = 100  # この件数未満のバケットは出さない（ぶれが大きい）

# secondStoreByHome のバケットをいくつ以上の実績で出すか。
# app.js の SECOND_STORE_MIN_SAMPLE と同じ値でなければならない。こちらが緩いと、
# app 側が薄いバケットを弾いた拍子に「配属を読む」処理ごと無効になる（実際に
# n=12 のバケットを出して、そうなった）。テストで両者の一致を見ている。
SECOND_STORE_MIN_SAMPLE = 20

# 見習いにゃんこの見分け方。お店のやり方に合わせている。
# 見習いのうちは X のアカウントを持たず、できたら昇格。初日から
# TRAINEE_MAX_DAYS 経ってもアカウントが無い人は、昇格せずにいなくなったと見る。
TRAINEE_MAX_DAYS = 90
# 判定するのは直近このぶんだけ。これより前まで遡ると、卒業したメイドさんが
# 「アカウントが無い（消えた）人」として、いつまでも見習いに数えられてしまう。
TRAINEE_WINDOW_DAYS = 120

def pick_rate(hits, chances, posted, sid):
    """その店が開いていた日のうち、その人がその店にいた割合。

    記録が薄いうちは公式サイトの配属に寄せ、記録が増えるほど実績が勝つ。
    posted が無い（公式サイトに載っていない）人は、従来どおり実績だけで出す。
    """
    if not posted:
        return rate(hits, chances)
    prior = POSTED_HIT if sid == posted else POSTED_MISS
    return round((hits + POSTED_PRIOR * prior) / (chances + POSTED_PRIOR), 3)


def norm_counter(c):
    tot = sum(c.values())
    return {k: round(v / tot, 3) for k, v in c.items()} if tot else {}


def group23(stores):
    a, b = 's2' in stores, 's3' in stores
    return 'both' if a and b else ('s2' if a else ('s3' if b else 'none'))


def measure_calibration(cell, last_d, roster_keys, posted_by_key):
    """チップに出す確率が、言ったとおりの割合で当たるかを測る。

    やっていることは tendency の計算と同じ手順を、過去に遡って繰り返すだけ。
    その日より前の TENDENCY_DAYS 日だけを使って pickRate を出し、
    その日に実際に開いていた店の中で正規化して、当たったかどうかを数える。
    未来を見ないよう、訓練期間は必ず評価日より前で打ち切る。

    端の確率が当てにならないことが分かっているので（90%と言って実測74%）、
    UI 側がその帯を判定できるように、この表をデータに載せる。
    """
    cells = sorted(cell.keys())
    if not cells:
        return None
    first_d = datetime.date.fromisoformat(cells[0][0])
    # 訓練に TENDENCY_DAYS 日ぶん要るので、その先から評価する
    eval_from = (first_d + datetime.timedelta(days=TENDENCY_DAYS)).isoformat()
    buckets = [{'from': i / 10, 'to': (i + 1) / 10, 'hit': 0, 'n': 0, 'said': 0.0}
               for i in range(10)]
    brier_sum = 0.0
    brier_n = 0
    tables = None
    built_for = None

    for date, sh in cells:
        if date < eval_from:
            continue
        block = (datetime.date.fromisoformat(date).toordinal() // CALIBRATION_REFIT_DAYS)
        if block != built_for:
            lo = (datetime.date.fromisoformat(date)
                  - datetime.timedelta(days=TENDENCY_DAYS)).isoformat()
            tables = calibration_tables(cell, lo, date, posted_by_key)
            built_for = block
        if not tables:
            continue
        stores = cell[(date, sh)]
        open_ids = [sid for sid in IDS if sid in stores]
        if len(open_ids) < 2:
            continue
        for sid in open_ids:
            for maid in stores[sid]:
                if maid not in roster_keys:
                    continue
                row = tables[sh].get(maid)
                if not row:
                    continue
                vals = {o: max(row[o], 1e-6) for o in open_ids}
                total = sum(vals.values())
                for o in open_ids:
                    p = vals[o] / total
                    y = 1 if o == sid else 0
                    brier_sum += (p - y) ** 2
                    brier_n += 1
                    b = buckets[min(int(p * 10), 9)]
                    b['n'] += 1
                    b['hit'] += y
                    b['said'] += p

    if brier_n == 0:
        return None
    out = []
    for b in buckets:
        if b['n'] < CALIBRATION_MIN_SAMPLE:
            continue
        out.append({
            'from': round(b['from'], 1), 'to': round(b['to'], 1), 'n': b['n'],
            'said': round(b['said'] / b['n'], 3),
            'actual': round(b['hit'] / b['n'], 3),
        })
    if not out:
        return None
    return {
        'from': eval_from, 'to': last_d.isoformat(),
        # 1店舗しか開かない日は、その人がその店にいるのが定義上100%になり、
        # バケットを不当に良く見せるので測っていない。チップが候補1店で 100% と
        # 出すときの不確かさは「店舗予測が当たるか」であって、ここの数字ではない。
        'scope': 'twoOrMoreOpen',
        'brier': round(brier_sum / brier_n, 4),
        'n': brier_n,
        'buckets': out,
    }


def calibration_tables(cell, lo, hi, posted_by_key):
    """lo 以上 hi 未満のシフトだけで、シフト別の pickRate を作る。"""
    at = collections.defaultdict(lambda: collections.Counter())
    opp = collections.defaultdict(lambda: collections.Counter())
    at_sh = {sh: collections.defaultdict(collections.Counter) for sh in SHIFTS}
    opp_sh = {sh: collections.defaultdict(collections.Counter) for sh in SHIFTS}
    seen = 0
    for (date, sh), stores in cell.items():
        if not (lo <= date < hi):
            continue
        seen += 1
        open_ids = [sid for sid in IDS if sid in stores]
        for sid in open_ids:
            for maid in stores[sid]:
                at[maid][sid] += 1
                at_sh[sh][maid][sid] += 1
                for other in open_ids:
                    opp[maid][other] += 1
                    opp_sh[sh][maid][other] += 1
    if seen < CALIBRATION_MIN_TRAIN:
        return None
    overall = {}
    for maid in at:
        posted = posted_by_key.get(maid)
        overall[maid] = {sid: pick_rate(at[maid][sid], opp[maid][sid], posted, sid)
                         for sid in IDS}
    out = {}
    for sh in SHIFTS:
        table = {}
        for maid, base_row in overall.items():
            table[maid] = {
                sid: (at_sh[sh][maid][sid] + SHRINK * base_row[sid])
                     / (opp_sh[sh][maid][sid] + SHRINK)
                for sid in IDS
            }
        out[sh] = table
    return out


def read_promoted_at():
    """見習いを終えてノーマルになった日（data/schedule.js の promotedAt）。

    その日より前は見習いなので、予定表には載らなかった。「予定表に何人載るか」で
    店舗数を決めている以上、過去の人数を数えるときも昇格前の出勤は数えてはいけない。
    数えると、事前に分かっていた人数が実際より多く見え、閾値が上にずれる。
    """
    path = os.path.join(ROOT, 'data', 'schedule.js')
    src = open(path, encoding='utf-8').read()
    m = re.search(r'promotedAt:\s*\{(.*?)\n  \}', src, re.S)
    if not m:
        return {}
    out = {}
    for name, date in re.findall(r'"([^"]+)":\s*"(\d{4}-\d{2}-\d{2})"', m.group(1)):
        out[ALIAS.get(name, name)] = date
    return out


def debut_dates(cell):
    """各メイドの「いまの在籍が始まった日」。

    同じ名前は世代をまたいで使い回される（記録上106件）。卒業した方のアカウントは
    消えるので、名前だけでは同一人物か分からない。STREAK_GAP_DAYS 以上あいたら
    別の人（あるいは別の在籍期間）とみなして区切る。

    復帰はほとんど無く、確認できているのは1件だけ（もなか、2025-09-01デビュー。
    この区切りでも 2025-09-02 と出る）。

    ただし「記録が無い」は「働いていない」ではない。記録そのものが途切れている
    期間の直後は、誰もが初出に見える。信用できるかどうかは trusted_debut() で
    別に判定すること。
    """
    days_by = collections.defaultdict(set)
    for (d, sh), stores in cell.items():
        for sid in stores:
            for mm in stores[sid]:
                days_by[mm].add(d)
    out = {}
    for mm, ds in days_by.items():
        ds = sorted(ds)
        start = ds[0]
        for i in range(1, len(ds)):
            gap = (datetime.date.fromisoformat(ds[i])
                   - datetime.date.fromisoformat(ds[i - 1])).days
            if gap >= STREAK_GAP_DAYS:
                start = ds[i]
        out[mm] = start
    return out


def trusted_debut(start, recorded):
    """その日をデビューと呼んでよいか。

    「STREAK_GAP_DAYS 日あいたから別の在籍」と判定するには、その手前の
    STREAK_GAP_DAYS 日ぶんが実際に記録されている必要がある。記録が
    無いだけの期間を「不在」と読むと、記録が再開した日が全員のデビューになる。

    実例: 手元の全期間データは 2023-09-30 から 2024-05-03 まで 216 日ぶん
    記録が無い。この区切りをそのまま使うと 2024-05 に 64 人が「デビュー」する。
    ひかりは実際には 2017 年 2 月からいる方である。

    リポジトリの shifts.csv は直近ぶんだけなので、内部に空白は無いが左端がある。
    そこに張りついている人も同じ理由で信用できない。
    """
    s = datetime.date.fromisoformat(start)
    window = [s - datetime.timedelta(days=k) for k in range(1, STREAK_GAP_DAYS + 1)]
    have = sum(1 for d in window if d.isoformat() in recorded)
    return have >= STREAK_GAP_DAYS // 2


def read_kitchen_staff():
    """メイド服を着ないキッチンにゃんこ（data/schedule.js の kitchenStaff）。

    公式サイトには配属店が載っているが、実測すると**その店に入る率が
    フロアの方より 13.3 ポイント低い**（62.6% 対 75.9%、並べ替え検定 p=0.025）。
    店ごとの散らばりも小さく（0.120 対 0.194）、どこにでも入っている。
    うる（配属3号店）とみりん（配属1号店）は、実績の最多がどちらも4号店。

    したがって「この人が予定表にいるからこの店が開く」の根拠には使えない。
    """
    path = os.path.join(ROOT, 'data', 'schedule.js')
    src = open(path, encoding='utf-8').read()
    m = re.search(r'kitchenStaff:\s*\[(.*?)\]', src, re.S)
    if not m:
        return set()
    return {ALIAS.get(n, n) for n in re.findall(r'"([^"]+)"', m.group(1))}


def read_milestones():
    """お店の公式な周年表（tools/data/milestones.csv）。

    誕生日と周年（＝初お給仕日）が全員ぶん載っている。お店の掲示をユーザーが
    渡してくれたもの。`debut` が初お給仕日、`returned` があればそれが
    「戻ってきてからの周年」で、いまの在籍はそちらから数える。

    これが来るまでは shifts.csv から streak_start で推定していた。答え合わせ
    すると、記録期間の中にデビューした15名では誤差の中央値 +3日（絶対値の
    平均3.6日、にゃなは0日）で推定は妥当だった。ただし記録より前からいる
    21名は当てられない（ひかりは3076日ずれた）。そこは推定の限界であって、
    いまは日付そのものが分かるので推定しない。
    """
    path = os.path.join(ROOT, 'tools', 'data', 'milestones.csv')
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding='utf-8-sig', newline='') as fh:
        for row in csv.DictReader(fh):
            name = ALIAS.get(row['name'], row['name'])
            out[name] = {
                'birthday': row.get('birthday') or None,
                # 復帰した方は、いまの在籍の開始日を使う（あらた・もなか）
                'debut': row.get('returned') or row.get('debut') or None,
                'firstDebut': row.get('debut') or None,
                'returned': row.get('returned') or None,
            }
    return out


def promotion_dates(cell, roster, known):
    """各メイドが見習いを終えた日。

    見習いは予定表に載らない。店舗数はその日の予定表に何人載るかで決めるので、
    過去の人数を数えるときも昇格前の出勤は数えてはいけない。数えると
    「事前に分かっていた人数」が実際より多く見え、閾値が上にずれる。

    優先順位:
      1. data/schedule.js の promotedAt（昇格そのものが分かっている4名）
      2. 公式の周年表の初お給仕日 + TRAINING_DAYS
      3. 周年表に無い方は shifts.csv からの推定 + TRAINING_DAYS

    実測できている4名はデビューから 68 / 75 / 82 / 91 日で昇格しており、
    平均 79 日だった。

    推定に頼る場合は trusted_debut を通す。記録の空白の直後は誰もが初出に
    見えるので、そこを在籍の開始と読むと、実際には何年も前からいる方の出勤が
    まるごと母数から外れる。公式の周年表があればこの判定は要らない。
    日付そのものが分かるので、記録開始より前ならとっくに昇格済みだと分かる。
    """
    debut = debut_dates(cell)
    milestones = read_milestones()
    recorded = {d for d, _ in cell}
    first = min(recorded) if recorded else None
    out = {}
    for name in roster:
        key = ALIAS.get(name, name)
        official = (milestones.get(key) or {}).get('debut')
        if official:
            promoted = (datetime.date.fromisoformat(official)
                        + datetime.timedelta(days=TRAINING_DAYS)).isoformat()
            # 記録が始まる前に昇格済みなら、母数から外す理由がない
            if first and promoted <= first:
                continue
            out[key] = promoted
            continue
        start = debut.get(key)
        if start and trusted_debut(start, recorded):
            out[key] = (datetime.date.fromisoformat(start)
                        + datetime.timedelta(days=TRAINING_DAYS)).isoformat()
    out.update(known)
    return out

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
    promoted_at = promotion_dates(cell, roster, read_promoted_at())

    def listed_on(d, stores):
        """その日の予定表に載っていたはずの人。昇格前の見習いは載らない。"""
        return {m for maids in stores.values() for m in maids
                if m in roster_names and d >= promoted_at.get(m, '')}

    headcount_by_open = {}
    open_by_headcount = {}
    for sh in SHIFTS:
        buckets = collections.defaultdict(list)
        per_headcount = collections.defaultdict(collections.Counter)
        for d in dates:
            stores = cell.get((d, sh), {})
            if not stores:
                continue
            listed = listed_on(d, stores)
            if not listed:
                continue
            age = (last_d - datetime.date.fromisoformat(d)).days
            buckets[len(stores)].append(len(listed))
            per_headcount[len(listed)][len(stores)] += 0.5 ** (age / HEADCOUNT_HALF_LIFE)
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
    home_store = read_home_store()
    for name in roster:
        key = ALIAS.get(name, name)
        if key not in worked:
            tendency[name] = None
            continue
        w = worked[key]
        posted = home_store.get(name)
        pick = {sid: pick_rate(len(at[key].get(sid, set())), len(w & open_for[sid]), posted, sid)
                for sid in IDS}
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
        # pickRate を4店で正規化したときの、均等（各25%）からの離れ具合。
        pick_tot = sum(pick.values()) or 1.0
        norm_pick = {sid: pick[sid] / pick_tot for sid in IDS}
        tendency[name] = {
            'alias': None if key == name else key,
            'workShifts': len(w),
            'nightShare': rate(worked_sh[key]['夜'], len(w)),
            'pickRate': pick,
            'pickRateByShift': by_shift,
            'sampleByShift': sample_by_shift,
            'share': overall,
            # その人の行き先がどれだけ偏っているか。0 なら4店を均等に回る。
            # 「人ごとの画面」で、その人の予測がどれだけ当てになるかを示すのに使う。
            # walk-forward の実測的中との相関は r=+0.837（50名）で、
            #   0.28 -> 82%（ちさと） / 0.14 -> 66%（すくい） / 0.07 -> 46%（みりん）
            # 最大 pickRate（r=+0.683）より説明力が高い。一箇所に強いことより、
            # 他店に行かないことのほうが効く。
            'spread': round(sum(abs(v - 0.25) for v in norm_pick.values()) / 2, 3),            'shareByShift': share_by_shift,
            'posted': posted,
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
    streak_start = debut_dates(cell)

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
        # 同じ名前は世代をまたいで使い回され（記録上106件）、卒業したメイドさんの
        # アカウントは消える。したがって「同じ名前の古いアカウント」は別人のもので、
        # この人の在籍歴の証拠にはならない。いま動いているアカウント自体が
        # 何年も前に作られているなら、それは本人が長くいる証拠になる。
        old_account = (v['xCreated'] or last_d.year) < last_d.year - 1
        # 出勤記録も同じ理由で「初出日」ではなく「今の在籍の開始」で見る。
        # 初出日は同名の別人のものかもしれない。
        v['likelyNew'] = v['promoted'] and not old_account and v['streakStart'] >= year_ago
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
            on = sum(1 for mm in maids if mm in roster_keys and d >= promoted_at.get(mm, ''))
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
            on = sum(1 for mm in maids if mm in roster_keys and d >= promoted_at.get(mm, ''))
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

    # 配属先の人が「その顔ぶれの何割を占めるか」の平年値。
    # その日の顔ぶれに2号店配属の人が多ければ、2号店が開く見込みが上がる、という
    # 使い方をする（app.js の expectedOpenStores）。実測で、店舗の組み合わせを
    # 丸ごと当てる的中が 42.9% -> 47.7%（McNemar p<0.05, n=709）。
    #
    # 人数ではなく割合にしてあるのは、予定表の提出が揃っていない時期でも比が
    # 変わらないため。実測でも、顔ぶれの3割を伏せた条件で 37.5% -> 40.3% と
    # 改善幅こそ縮むが悪化しない。人数で見る版は欠けに弱かった。
    #
    # 母数を「配属の分かる人」に揃えているのは、予定表に載るのが
    # ノーマル以上（＝公式サイトに配属が載っている人）だけだから。
    posted_by_key = {ALIAS.get(n, n): sid for n, sid in home_store.items()}
    home_staff_share = {}
    for sh in SHIFTS:
        acc_share = {sid: [] for sid in IDS}
        for (d, sh2), stores in cell.items():
            if sh2 != sh or d < cut:
                continue
            known = [m for sid in stores for m in stores[sid] if posted_by_key.get(m)]
            if not known:
                continue
            for sid in IDS:
                acc_share[sid].append(
                    sum(1 for m in known if posted_by_key[m] == sid) / len(known))
        home_staff_share[sh] = {
            sid: round(sum(v) / len(v), 4) if v else round(1 / len(IDS), 4)
            for sid, v in acc_share.items()
        }

    # 予定表の顔ぶれから相方店舗を読む。
    #
    # 1号店はほぼ毎シフト開くので、問題は「2番目がどれか」に尽きる。前日から
    # 当てると 33.5% -> 46.2% になるが、前日の実測が要る。実測が2日前までしか
    # 無いと 32.7% とベースラインに戻るので、記録が途切れると即座に効かなくなる。
    # X が読めない今、予定表のある15日のうち前日が分かるのは1日だけである。
    #
    # 予定表の配属者数なら実測が要らない。予定表は月初にまとめて手に入るので
    # 15日ぶん全部に効く。単独で 41.1%（前日方式 46.2% に近い）、前日と併せて
    # 50.9% だった。
    #
    # 出しているのは「その店を本拠とする人が n 人載っているとき、その店が
    # 相方だった割合」。キッチンにゃんこは数から外す。公式サイトには配属が
    # 載っているが、実測ではその店に入る率がフロアより 13.3pt 低く
    # （62.6% 対 75.9%、p=0.025）、どこにでも入っている。「この人がいるから
    # この店が開く」の根拠にならない。外すと 3号店の振れ幅が 21% -> 67% に
    # 広がる（うる・みりんの配属が 3号店・1号店なのに実績最多が4号店）。
    #
    # 4号店は外しても動かない。キッチン5名に4号店配属が一人もいないので、
    # そもそも4号店の数に影響しない。4号店が読めない理由は別にある。
    second_by_home = {}
    kitchen = read_kitchen_staff()
    for sid in IDS[1:]:
        tally = collections.defaultdict(lambda: [0, 0])
        for (d, _sh), stores in cell.items():
            if d < cut:
                continue
            others = [x for x in IDS[1:] if x in stores]
            if 's1' not in stores or len(others) != 1:
                continue
            listed = [m for x in stores for m in stores[x] if m not in kitchen]
            k = min(sum(1 for m in listed if posted_by_key.get(m) == sid), 4)
            tally[k][1] += 1
            tally[k][0] += 1 if others[0] == sid else 0
        second_by_home[sid] = {
            str(k): {'rate': round(v[0] / v[1], 3), 'n': v[1]}
            for k, v in sorted(tally.items()) if v[1] >= SECOND_STORE_MIN_SAMPLE
        }

    hist_from = (last_d - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    # 見習いにゃんこの見分け方は、お店のやり方に合わせる。
    #
    #   - 見習いのうちは X のアカウントを持たない。アカウントができたら昇格。
    #   - 見つけた初日から TRAINEE_MAX_DAYS 経ってもアカウントが無い人は、
    #     昇格せずにいなくなったと見る（ずっと見習いのままにはしない）。
    #   - 判定するのは直近 TRAINEE_WINDOW_DAYS だけ。それより前まで遡ると、
    #     もう卒業したメイドさんを見習いとして数え続けることになる。
    #
    # 初日は新人にゃんこの告知（debuts.csv）を優先し、無ければ記録上の初出を使う。
    trainee_from = (last_d - datetime.timedelta(days=TRAINEE_WINDOW_DAYS)).isoformat()
    debuts = read_debuts()
    first_shift = {}
    for (d0, _sh0), stores0 in cell.items():
        for maids0 in stores0.values():
            for m0 in maids0:
                if m0 not in first_shift or d0 < first_shift[m0]:
                    first_shift[m0] = d0

    def is_trainee(name, on_date):
        if accounts.get(name):
            return False
        started = debuts.get(name) or first_shift.get(name)
        if not started:
            return False
        age = (datetime.date.fromisoformat(on_date)
               - datetime.date.fromisoformat(started)).days
        return 0 <= age < TRAINEE_MAX_DAYS

    actual = {}
    actual_roster = {}
    for d in all_dates:
        if d < hist_from:
            continue
        entry = {sh: sorted(open_at(d, sh)) for sh in SHIFTS if (d, sh) in cell}
        if entry:
            actual[d] = entry
        # 誰がどこにいたかまで、記録があるぶんはそのまま渡す。カレンダーが
        # 「実績」と書く日に予定表からの割り振りを出すと、名前だけが推測のまま残る。
        names = {}
        for sh in SHIFTS:
            if (d, sh) not in cell:
                continue
            per_store = {sid: sorted(cell[(d, sh)][sid])
                         for sid in IDS if cell[(d, sh)].get(sid)}
            if not per_store:
                continue
            shift_entry = {'stores': per_store}
            # 窓の外は「見習いかどうか分からない」であって「全員ノーマル」ではない。
            # キーごと落として、印を付けない日だと分かるようにする。
            if d >= trainee_from:
                shift_entry['trainees'] = sorted({
                    m for maids in per_store.values() for m in maids
                    if is_trainee(m, d)
                })
            names[sh] = shift_entry
        if names:
            actual_roster[d] = names

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
        'homeStaffShare': home_staff_share,
        # spread をどこで区切ると、人ごとの画面の一言が実態と合うか。
        # 実測（walk-forward で 20 シフト以上ある 37 名）から決めた。
        #
        #   区切り        人数  行き先を当てられる  いちばん多い店の割合
        #   0.30 以上      8名        76%              74%
        #   0.20〜0.30     9名        72%              53%
        #   0.20 未満     20名        59%              43%
        #
        # 0.30/0.15 では中位が 17 名と重くなり、的中も 68% と上位に寄る。
        # 0.35/0.20 では上位が 5 名しか残らない。0.30/0.20 がいちばん素直に割れる。
        'spreadBands': {'settled': 0.30, 'mixed': 0.20},
        'secondStoreByHome': second_by_home,
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
            'calibration': measure_calibration(
                cell, last_d, {ALIAS.get(n, n) for n in roster}, posted_by_key),
        },
        'actual': actual,
        # 記録のある日の「誰がどこにいたか」。カレンダーが実績と書く日は、
        # 予定表からの割り振りではなくこちらを出す。trainees は、その日は
        # 見習いにゃんこだった人（公式サイトの在籍一覧に載らない人）。
        'actualRoster': actual_roster,
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
