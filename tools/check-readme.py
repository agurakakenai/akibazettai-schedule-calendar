"""README に書いた数字が、いま出荷している値と合っているかを見ます。

    python tools/check-readme.py

**今日、書いてある数字と実際が食い違うのを5回見つけました。** 原因はどれも同じで、
書いた時点では正しく、その後データが増えたのに文章を読み直さなかったことです。

    「集計期間に1件もお給仕記録が無い」   まひろさんは87件あった
    「食い違うのは4名」                実際は5名
    「roster 39名のうち7名」           40名のうち8名
    「3店より多いシフトは0件」           2026-09-03 に1件できた
    「1シフトあたり0.87人」             いまは0.79人

**検算するときも間違えました。** 全787シフトで数えて「昼360件は古い」と読みかけ
ましたが、360件は365日窓の値で正しいものでした。**その数字がどの窓で作られたかを
先に確かめないと、検算のほうが嘘をつきます。**

だからこの道具は、自分で数え直しません。**`data/store-insights.js` に出荷されて
いる値だけ**を読み、README の対応する数字と比べます。生成側と読み手側が同じ値を
見ていることを確かめる、それだけです。

数字が動いたら落ちるので、**README を直すか、ここの期待値を直すか**を選ぶことに
なります。どちらにしても、一度は目を通すことになります。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_insights():
    raw = open(os.path.join(ROOT, 'data', 'store-insights.js'),
               encoding='utf-8').read()
    return json.loads(raw[raw.index('{'):raw.rindex('}') + 1])


def load_readme():
    """README を読む。ただし、この道具自身の説明にある古い数字は数えない。

    `check-readme.py` の使い方を README に書いたら、その説明に例として並べた
    古い数字（「roster は39名」「0.87人」）を、この道具が拾って落ちた。
    **道具が、自分の説明文を読んで落ちた。**

    「昔はこう書いてあった」を残せなくなると、なぜ直したのかが分からなくなる。
    そこで、その表だけを外す。コードブロックや `引用` を丸ごと外す形も試したが、
    **本文で使っている数字まで消える**ので、範囲を絞るほうが正しかった。
    """
    text = open(os.path.join(ROOT, 'README.md'), encoding='utf-8').read()
    return re.sub(r'\| 書いてあったこと \| 実際 \|.*?(?=\n\n)', '', text,
                  flags=re.S)


def pct(value):
    """0.675 -> '67.5' と '68'。README は両方の書き方をする。"""
    return ['%g' % round(value * 100, 1), '%d' % round(value * 100)]


def build_claims(d):
    """(見出し, README に出ているはずの文字列, どこから来た値か) の一覧。"""
    out = []

    def add(label, needles, source):
        if isinstance(needles, str):
            needles = [needles]
        out.append((label, [n for n in needles if n], source, False))

    counts = d.get('openCountPerShift') or {}
    for shift, table in counts.items():
        total = sum(table.values())
        above = sum(v for k, v in table.items() if int(k) > 3)
        # README が例に出しているのは昼だけ。夜の件数は本文に書いていない。
        if shift != '昼':
            continue
        add('%sのシフト数' % shift, '%s%d件' % (shift, total),
            'openCountPerShift')
        add('3店より多い%sの件数' % shift, '記録に%d件' % above,
            'openCountPerShift')

    move = d.get('sameDayMaidMove') or {}
    if move:
        total = sum(v['n'] for v in move.values())
        stayed = sum(v['n'] * v['to'].get(sid, 0) for sid, v in move.items())
        add('昼夜とも出た人数', '%d人' % total, 'sameDayMaidMove')
        add('昼と別の店にいた割合',
            ['%s%%' % s for s in pct(1 - stayed / total)],
            'sameDayMaidMove')

    cov = d.get('rosterCoverage') or {}
    if cov:
        add('見習いの集計期間', '%s〜%s' % (cov['from'], cov['to']),
            'rosterCoverage')
        add('見習いの集計シフト数', '%dシフト' % cov['shiftCells'],
            'rosterCoverage')
        # 数字だけを探すと、店ごとの正しい値（0.78 / 0.85）まで「古い」と
        # 誤検出する。**近所の数字を禁じる形は一度作って取り下げた。**
        # 前後の言葉ごと照合すれば、同じ 0.79 でも役割が区別できる。
        per = cov['unlistedPerShift']
        # 同じ値が2か所に出るので、それぞれ別に要求する。片方だけ直しても
        # もう片方で通ってしまう形を、実際に一度作った。
        add('1シフトあたりの見習い', '1シフトあたり平均%g人' % per,
            'rosterCoverage')
        add('見習いぶんの断り', '見習いぶん%g人' % per, 'rosterCoverage')
        for sid, label in (('s1', '1号店'), ('s2', '2号店'),
                           ('s3', '3号店'), ('s4', '4号店')):
            store = (cov.get('byStore') or {}).get(sid)
            if store:
                # README は 1.00 を「1.00人」とも「1人」とも書く。両方を許す。
                value = store['unlistedPerShift']
                add('%sの見習い' % label,
                    ['%s%.2f人' % (label, value), '%s%g人' % (label, value)],
                    'rosterCoverage.byStore')

    roster = d.get('schedulePending') or {}
    if roster.get('rostered'):
        n = roster['rostered']
        # 書き方が何通りかあるので、どれか1つでも出ていればよい。
        # ただし**古い人数がどこかに残っていたら落とす**。候補を並べるだけだと、
        # 「在籍40名」を「在籍39名」に書き換えても `roster 40名` のほうで
        # 通ってしまった。実際にそれで一度すり抜けた。
        add('在籍人数', ['在籍%d名' % n, 'roster %d名' % n, '%d名で' % n],
            'schedulePending')
        for wrong in (n - 1, n + 1):
            out.append(('在籍人数（%d名は古い）' % wrong,
                        ['在籍%d名' % wrong, 'roster %d名' % wrong],
                        'schedulePending', True))

    return out


def main():
    d = load_insights()
    text = load_readme()
    # 空白と全角のゆれを潰してから探す。README は「1号店 0.78人」と書くことがある。
    flat = re.sub(r'[\s　]', '', text)

    bad = []
    print('出荷している値と README を突き合わせます')
    print()
    for label, needles, source, forbidden in build_claims(d):
        if not needles:
            continue
        found = [n for n in needles if re.sub(r'[\s　]', '', n) in flat]
        if forbidden:
            # 出てはいけないもの（古い数字）。
            if found:
                bad.append((label, found, source))
                print('  %-26s %-22s ★ 古い数字が残っています'
                      % (label, found[0]))
            continue
        if found:
            print('  %-26s %-22s 見つかりました' % (label, found[0]))
        else:
            bad.append((label, needles, source))
            print('  %-26s %-22s ★ README に見当たりません'
                  % (label, ' / '.join(needles)))

    print()
    if not bad:
        print('すべて README に出ています。')
        return 0

    print('%d 件が見つかりませんでした。' % len(bad))
    print()
    print('どちらかです。')
    print('  1. データが動いたのに README を直していない -> README を直す')
    print('  2. その数字を README に書かなくなった       -> ここの一覧から外す')
    print()
    print('**数え直して確かめようとしないでください。** その数字がどの窓で')
    print('作られたかを取り違えると、検算のほうが嘘をつきます。')
    print('出典は %s です。' % ' / '.join(sorted({b[2] for b in bad})))
    return 1


if __name__ == '__main__':
    sys.exit(main())
