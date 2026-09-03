"""お給仕投稿の URL から、その日の顔ぶれを `shifts.csv` に取り込みます。

    python tools/add-shifts.py https://x.com/akibazettai/status/2095077027061899578 ...
    python tools/add-shifts.py --dry-run <URL> ...     # 書かずに見るだけ

X のタイムラインは取得できません。`syndication.twitter.com` の
timeline-profile は「新人にゃんこ」「お知らせ」だけを100件返す固定の抜粋で、
お給仕投稿を含みません。Wayback にも日々の投稿は残りません。

一方、`cdn.syndication.twimg.com/tweet-result?id=<ID>` は**ID さえ分かれば
ログイン無しで1件読めます**。だから URL さえもらえれば取り込めます。
この道具はその手順をまとめたものです。

投稿の形はこうなっています。

    【アキバ絶対領域+e】
    ひるにゃんこ🐈🎮

    あむ
    さぴ
    こまち

見出しの店名には表記ゆれがあります（`A.D.1912` / `A.D.1912`、`+e` / `＋e`、
`アキバ絶対 A.D.1912` など）。空白と全角を潰してから照合します。

名前は本文の行から取ります。名簿に無い名前もそのまま入れます。
見習いにゃんこは名簿に載らないので、弾くとその人が消えます。
"""
import argparse
import csv
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHIFTS = os.path.join(HERE, 'data', 'shifts.csv')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

STORES = [
    ('a.d.2045', '4号店 A.D.2045'),
    ('2045', '4号店 A.D.2045'),
    ('4号店', '4号店 A.D.2045'),
    ('a.d.1912', '2号店 A.D.1912'),
    ('1912', '2号店 A.D.1912'),
    ('2号店', '2号店 A.D.1912'),
    ('+e', '3号店 +e'),
    ('3号店', '3号店 +e'),
]
SHIFT_WORDS = [('ひる', 'ひる'), ('昼', 'ひる'), ('ヒル', 'ひる'),
               ('よる', 'よる'), ('夜', 'よる'), ('ヨル', 'よる')]


def norm(s):
    """全角と空白のゆれを潰す。見出しの店名は表記が揺れる。"""
    return unicodedata.normalize('NFKC', s).replace(' ', '').lower()


def tweet_id(arg):
    m = re.search(r'/status/(\d+)', arg) or re.fullmatch(r'\s*(\d{10,25})\s*', arg)
    return m.group(1) if m else None


def fetch(tid):
    url = ('https://cdn.syndication.twimg.com/tweet-result'
           '?id=%s&lang=ja&token=a' % tid)
    req = urllib.request.Request(url, headers={'User-Agent': UA,
                                               'Accept-Language': 'ja,en;q=0.8'})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def parse(text, created_at):
    """投稿本文から (日付, 店, シフト, [名前]) を取り出す。読めなければ None。"""
    head = text[:120]
    n = norm(head)
    if 'にゃんこ' not in n:
        return None

    shift = None
    for word, value in SHIFT_WORDS:
        if norm(word) + 'にゃんこ' in n:
            shift = value
            break
    if not shift:
        return None

    store = '1号店 アキバ絶対領域'
    for needle, full in STORES:
        if norm(needle) in n:
            store = full
            break

    # 名前は見出しの下の空行から始まり、次の空行までひと固まりで並ぶ。
    #
    #     【アキバ絶対領域】
    #     よるにゃんこ🐈🍓
    #                      <- 空行
    #     あむ
    #     きらり🎉          <- 主役には絵文字が付く
    #                      <- 空行
    #     きらりちゃんの周年を…   <- ここから下は本文
    #
    # 行ごとの見た目だけで拾うと、この本文まで名前になる。実際に
    # 「えいちゃん卒業にゃん」が名前として入った。固まりで取れば起きない。
    lines = [ln.strip() for ln in text.split('\n')]
    blocks, current = [], []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    def looks_like_name(line):
        m = re.match(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9]{1,12}', line)
        if not m:
            return None
        name = m.group(0)
        rest = line[len(name):]
        # 名前の後ろは絵文字だけ。文字が続く行は本文。
        if rest and re.search(r'[ぁ-んァ-ヶ一-龠ーａ-ｚA-Za-z0-9、。！？!?･・]', rest):
            return None
        # お店の言い回しで終わる行は本文。「浴衣コスデーにゃんね」「待ってるにゃんね」
        # のように、名前と同じ見た目で1行に収まるものがある。
        if re.search(r'(にゃん(ね|こ)?|です|ます|だよ|でした)$', name):
            return None
        return name

    names = []
    for block in blocks:
        if any(b.startswith(('#', 'http', '【', '※', '⊂')) or 'にゃんこ' in b
               for b in block):
            continue
        got = [looks_like_name(b) for b in block]
        # ひとつでも名前でない行が混ざる固まりは、名前の固まりではない。
        if not got or any(g is None for g in got):
            continue
        for g in got:
            if g not in names:
                names.append(g)
    if not names:
        return None

    # created_at は UTC。日本時間の日付にする。
    import datetime
    try:
        dt = datetime.datetime.strptime(created_at, '%a %b %d %H:%M:%S %z %Y')
    except (ValueError, TypeError):
        try:
            dt = datetime.datetime.fromisoformat(
                str(created_at).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
    jst = dt.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    # 夜の投稿が日をまたぐことはないが、深夜0〜4時のものは前日の夜として扱う。
    if jst.hour < 5:
        jst -= datetime.timedelta(days=1)
    return jst.strftime('%Y-%m-%d'), store, shift, names


def load_known():
    """記録にある名前と、その回数。新しい名前を見分けるのに使う。"""
    counts = {}
    try:
        with open(SHIFTS, encoding='utf-8-sig', newline='') as fh:
            for row in csv.DictReader(fh):
                name = row.get('maid')
                if name:
                    counts[name] = counts.get(name, 0) + 1
    except OSError:
        pass
    debuts = set()
    try:
        with open(os.path.join(HERE, 'data', 'debuts.csv'),
                  encoding='utf-8-sig', newline='') as fh:
            for row in csv.DictReader(fh):
                name = (row.get('name') or row.get('maid') or '').strip()
                if name:
                    debuts.add(name)
    except OSError:
        pass
    return counts, debuts


def resembles(name, counts, floor=5):
    """定着した名前のうち、1文字違いか頭が一致するもの。

    お店の投稿には打ち間違いがあります。実際に `もな`（もなか）、`みずれ`（みぞれ）、
    `みひん`（みりん）、`ましろ`（まひろ）などが記録に入っていました。1回きり
    しか出てこない名前は、たいてい打ち間違いです。

    ただし**直せません**。見習いにゃんこは名簿に載らない新しい名前で来るので、
    打ち間違いと見分けが付かないためです。`あるか` さんは初日の見習いでしたが、
    `るるか` と1文字違いなのでこの検査に引っかかります。**言うだけにします。**
    """
    out = []
    for other, n in counts.items():
        if other == name or n < floor:
            continue
        if other.startswith(name) or name.startswith(other):
            out.append((other, n))
        elif len(other) == len(name) and sum(
                a != b for a, b in zip(name, other)) == 1:
            out.append((other, n))
    return sorted(out, key=lambda x: -x[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('urls', nargs='+', help='お給仕投稿の URL か tweet ID')
    ap.add_argument('--dry-run', action='store_true', help='書かずに見るだけ')
    args = ap.parse_args()

    with open(SHIFTS, encoding='utf-8-sig', newline='') as fh:
        rows = list(csv.DictReader(fh))
    fields = list(rows[0].keys()) if rows else ['date', 'store', 'shift',
                                                'maid', 'tweet_id']
    have = {r['tweet_id'] for r in rows if r.get('tweet_id')}
    counts, debuts = load_known()

    added, skipped, fresh = [], [], []
    for arg in args.urls:
        tid = tweet_id(arg)
        if not tid:
            skipped.append((arg, 'URL から ID を読めません'))
            continue
        if tid in have:
            skipped.append((tid, 'すでに入っています'))
            continue
        try:
            data = fetch(tid)
        except urllib.error.HTTPError as exc:
            skipped.append((tid, 'HTTP %s' % exc.code))
            continue
        except Exception as exc:                      # noqa: BLE001
            skipped.append((tid, str(exc)[:60]))
            continue
        text = data.get('text') or ''
        got = parse(text, data.get('created_at'))
        if not got:
            skipped.append((tid, 'お給仕投稿として読めません: %s'
                            % text[:40].replace('\n', ' ')))
            continue
        date, store, shift, names = got
        print('%s %s %s  %d名  %s' % (date, store, shift, len(names),
                                      ' '.join(names)))
        for name in names:
            added.append({'date': date, 'store': store, 'shift': shift,
                          'maid': name, 'tweet_id': tid})
            if name not in counts:
                fresh.append((name, date, resembles(name, counts),
                              name in debuts))

    print()
    for name, date, like, has_debut in fresh:
        if like:
            print('  ★ %s (%s) は記録に無い名前です。%s に似ています（%s）'
                  % (name, date,
                     ' / '.join('%s %d回' % (n, c) for n, c in like[:3]),
                     '新人告知あり' if has_debut else '新人告知なし'))
            print('     打ち間違いなら直してください。'
                  '見習いにゃんこなら debuts.csv に初日を足してください。')
        else:
            print('  ・ %s (%s) は記録に無い名前です。似た名前はありません。'
                  % (name, date))
    for tid, why in skipped:
        print('  飛ばした %s: %s' % (tid, why))
    if not added:
        print('入れるものがありません。')
        return 1
    print('%d 行を追加します。' % len(added))
    if args.dry_run:
        print('--dry-run なので書きません。')
        return 0

    rows.extend(added)
    # 日付・店・シフト・名前で並べ直す。読むときに探しやすい。
    rows.sort(key=lambda r: (r['date'], r['store'], r['shift'], r['maid']))
    with open(SHIFTS, 'w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=fields, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    print('書きました -> tools/data/shifts.csv')
    print('つぎに `python tools/build-insights.py` を回してください。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
