"""2019年からの記録を取り出します。README の的中率を検算するときに使います。

    python tools/load-archive.py            # 何が入っているかを見る
    python tools/load-archive.py --merge shifts-full.csv

`tools/data/shifts.csv` は直近400日ぶんです。サイトを作るにはそれで足りますが、
**精度の測り直しには訓練365日＋評価365日が要る**ので、それだけでは README に
書いた的中率を検算できません。

そこで `tools/data/shifts-archive.csv.zip` に 2019-04-14 〜 2025-07-25 を
入れてあります（26,448行、178KB）。`shifts.csv` と合わせると 34,457 行、
2019-04-14 〜 いま、になります。日付は重なりも欠けもありません。

**この記録はもう取り直せません。**

    X のタイムライン   新人告知100件の固定セットで、お給仕投稿を含まない
    Wayback Machine   日々の投稿は保存されない
    tweet-result      ID が分かれば1件ずつ読めるが、その ID の一覧が要る

なので消さないでください。圧縮しているのは大きさのためで、中身は `shifts.csv` と
まったく同じ列・同じ表記です。

なお、取り込むときに本文から名前として拾われていたものを外してあります
（`あとから` / `巫女コスデー` / `でした` / `新人にゃんこ` / `りかにゃん`）。
`あずにゃん` は残しています。原文が読点区切りの一覧で、そこに名前として
並んでいるためです。**「にゃん」で終わる形だけで落とすと、この方が消えます。**
"""
import argparse
import csv
import io
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(HERE, 'data', 'shifts-archive.csv.zip')
SHIFTS = os.path.join(HERE, 'data', 'shifts.csv')


def read_archive():
    with zipfile.ZipFile(ARCHIVE) as z:
        raw = z.read('shifts-archive.csv').decode('utf-8-sig')
    return list(csv.DictReader(io.StringIO(raw)))


def read_shifts():
    with open(SHIFTS, encoding='utf-8-sig', newline='') as fh:
        return list(csv.DictReader(fh))


def describe(label, rows):
    if not rows:
        print('  %-14s 空です' % label)
        return
    dates = sorted({r['date'] for r in rows})
    print('  %-14s %6d 行 / %4d 日 / %s 〜 %s'
          % (label, len(rows), len(dates), dates[0], dates[-1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--merge', metavar='OUT',
                    help='shifts.csv と合わせて書き出す先')
    args = ap.parse_args()

    old = read_archive()
    now = read_shifts()
    describe('archive', old)
    describe('shifts.csv', now)

    key = lambda r: (r['date'], r['store'], r['shift'], r['maid'])
    overlap = {key(r) for r in old} & {key(r) for r in now}
    print()
    print('  重なり        %d 行' % len(overlap))

    if not args.merge:
        merged = len(old) + len(now) - len(overlap)
        print('  合わせると    %d 行' % merged)
        print()
        print('書き出すには --merge <ファイル名> を付けてください。')
        return 0

    seen = set()
    rows = []
    for r in old + now:
        k = key(r)
        if k in seen:
            continue
        seen.add(k)
        rows.append(r)
    rows.sort(key=lambda r: (r['date'], r['store'], r['shift'], r['maid']))

    out = args.merge if os.path.isabs(args.merge) else os.path.join(
        os.getcwd(), args.merge)
    with open(out, 'w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(now[0].keys()),
                           quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    describe('書き出し', rows)
    print('  -> %s' % out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
