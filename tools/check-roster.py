"""公式サイトのメイドさん紹介と `data/schedule.js` の名簿を突き合わせます。

    python tools/check-roster.py

なぜ要るか。`roster` は手で保つ一覧なので、お店の側が動いたときに気づく手立てが
ありませんでした。実際に、まひろさんが公式サイトの2号店に載っているのに `roster`
から抜けたままで、87件の記録を持ちながら予測に一度も出ていませんでした。

README には「集計期間に記録が無いので追加できない」と書いてありました。書いた時点
では正しかったのだと思いますが、その後この方が戻ってきても、README を読み直す人が
いなかったので記述だけが残りました。

**覚えておく形にすると、事実が動いたときに嘘になります。**
確かめる形にすれば、走らせるたびに今の答えが出ます。

ネットワーク取得はテストには入れていません。向こうが落ちているときに
こちらが落ちるべきではないためです。解析は保存した入力で検査します。
名簿をいじるときと、時々流してください。

公式サイトの作り:

    <li class="shopNum02">
      <a href="..."><figure><img alt="まひろ"/></figure><p>まひろ</p></a>
    </li>

`shopNum01`〜`04` が `s1`〜`s4` に対応し、掲載順が `roster` の並びです。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
URL = 'https://akibazettai.com/staff/'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


def fetch(url=URL):
    req = urllib.request.Request(url, headers={'User-Agent': UA,
                                               'Accept-Language': 'ja,en;q=0.8'})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode('utf-8', 'replace')


class RosterParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.seen = set()
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == 'li':
            classes = (dict(attrs).get('class') or '').split()
            store = next((m.group(1) for value in classes
                          if (m := re.fullmatch(r'shopNum(\d+)', value))), None)
            self.items.append({'store': 's%d' % int(store) if store else None,
                               'text': None, 'name': None})
        elif tag == 'p' and self.items:
            item = self.items[-1]
            if item['name'] is None:
                item['text'] = []

    def handle_data(self, data):
        if self.items and self.items[-1]['text'] is not None:
            self.items[-1]['text'].append(data)

    def handle_endtag(self, tag):
        if not self.items:
            return
        item = self.items[-1]
        if tag == 'p' and item['text'] is not None:
            item['name'] = ''.join(item['text']).strip() or None
            item['text'] = None
        elif tag == 'li':
            self.items.pop()
            name, store = item['name'], item['store']
            if name and store and name not in self.seen:
                self.seen.add(name)
                self.out.append((name, store))


def parse_site(html):
    """掲載順のまま [(名前, 店), ...] を返す。同じ人が複数タブに出ても最初だけ。"""
    parser = RosterParser()
    parser.feed(html)
    parser.close()
    return parser.out


def read_schedule():
    src = open(os.path.join(ROOT, 'data', 'schedule.js'), encoding='utf-8').read()

    def block(key, open_c, close_c):
        m = re.search(r'%s:\s*\%s(.*?)\n  \%s' % (key, open_c, close_c), src, re.S)
        return m.group(1) if m else ''

    roster = re.findall(r'"([^"]+)"', block('roster', '[', ']'))
    home = dict(re.findall(r'"([^"]+)":\s*"(s\d)"', block('homeStore', '{', '}')))
    m = re.search(r'unpostedMaids:\s*\[(.*?)\]', src, re.S)
    unposted = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    return roster, home, unposted


def main():
    try:
        html = fetch()
    except (urllib.error.URLError, OSError) as exc:
        print('公式サイトを取得できませんでした: %s' % exc)
        print('（向こうの都合なので、これ自体は名簿の問題ではありません）')
        return 2

    site = parse_site(html)
    if not site:
        print('掲載を読み取れませんでした。ページの作りが変わったかもしれません。')
        return 2

    roster, home, unposted = read_schedule()
    site_names = [n for n, _ in site]
    site_store = dict(site)
    problems = []

    print('公式サイト %d名 / roster %d名（うち未掲載の申告 %d名）'
          % (len(site), len(roster), len(unposted)))
    print()

    missing = [n for n in site_names if n not in roster]
    if missing:
        problems.append('site_only')
        print('■ サイトに載っているのに roster にいません（予測に出ません）')
        for n in missing:
            print('    %-8s %s' % (n, site_store[n]))
        print()

    extra = [n for n in roster if n not in site_names]
    surprise = [n for n in extra if n not in unposted]
    if surprise:
        problems.append('roster_only')
        print('■ roster にいるのにサイトに載っていません')
        print('  （unpostedMaids に入れるか、卒業なら roster から外してください）')
        for n in surprise:
            print('    %-8s roster では %s' % (n, home.get(n, '?')))
        print()

    gone = [n for n in unposted if n in site_names]
    if gone:
        problems.append('now_listed')
        print('■ unpostedMaids の方がサイトに載りました')
        print('  （配属を照合して unpostedMaids から消してください）')
        for n in gone:
            mark = '一致' if home.get(n) == site_store[n] else \
                   'roster %s ≠ サイト %s' % (home.get(n, '?'), site_store[n])
            print('    %-8s %s' % (n, mark))
        print()

    moved = [(n, home[n], site_store[n]) for n in roster
             if n in site_store and n in home and home[n] != site_store[n]]
    if moved:
        problems.append('moved')
        print('■ 配属が変わっています')
        for n, a, b in moved:
            print('    %-8s %s -> %s' % (n, a, b))
        print()

    both = [n for n in site_names if n in roster]
    ordered = [n for n in roster if n in site_names]
    if both != ordered:
        problems.append('order')
        print('■ 並びがサイトと違います')
        for i, (a, b) in enumerate(zip(both, ordered)):
            if a != b:
                print('    %d番目  サイト %s / roster %s' % (i + 1, a, b))
                break
        print()

    if not problems:
        print('差分はありません。')
        return 0

    print('直したら `python tools/build-insights.py` を流し直してください。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
