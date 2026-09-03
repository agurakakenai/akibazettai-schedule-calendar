"""index.html の刻印が、コミットされた中身と合っているかを確かめます。

    python tools/check-stamps.py           # HEAD を見る
    python tools/check-stamps.py origin/main

`tests/validate-schedule.js` も同じ照合をしますが、あちらは**作業ディレクトリ**を
見ます。ワークツリーを複数人（複数セッション）で共有していると、これが問題になります。

    誰かの未コミットの app.js が置いてある
      -> build-insights.py はその中身に対して刻印を書く
      -> 手元では validate-schedule.js が通る
      -> コミットには刻印だけが入り、app.js は入らない
      -> main で落ちる

**検査は「このリポジトリが整合しているか」と「このリポジトリ＋誰かの未コミット作業が
整合しているか」を区別できません。** 実際に一度やりました。作業ディレクトリを見ずに
コミットだけで判定すれば、その取り違えは起きません。

刻印の計算は `build-insights.py` の `stamp_assets()` と同じ規則です。

    - CRLF は LF に揃える（手元は CRLF、CI と GitHub Pages は LF）
    - `"generatedAt":` の行は数えない（回すたびに変わるため）
"""
import hashlib
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def blob(rev, path):
    """コミットの中身を生バイトで取り出す。

    テキストとして読むと、シェルや Python の改行変換が挟まって値が変わる。
    実際にそれで「不一致」を出して、危うく偽の警報を上げるところだった。
    """
    out = subprocess.run(['git', '-C', ROOT, 'show', f'{rev}:{path}'],
                         capture_output=True)
    if out.returncode != 0:
        return None
    return out.stdout


def digest(raw):
    raw = raw.replace(b'\r\n', b'\n')
    raw = re.sub(rb'^\s*"generatedAt":.*$', b'', raw, flags=re.M)
    return hashlib.sha256(raw).hexdigest()[:10]


def main():
    rev = sys.argv[1] if len(sys.argv) > 1 else 'HEAD'
    index = blob(rev, 'index.html')
    if index is None:
        print(f'{rev} に index.html がありません')
        return 2

    marks = re.findall(r'(?:src|href)="([^"?]+)\?v=([0-9a-f]+)"',
                       index.decode('utf-8', 'replace'))
    if not marks:
        print(f'{rev} の index.html に刻印がありません')
        return 2

    print(f'{rev} の刻印を、同じコミットの中身と照合します')
    bad = []
    for path, want in marks:
        raw = blob(rev, path)
        if raw is None:
            bad.append(path)
            print(f'  {path:<26} そのコミットにありません')
            continue
        got = digest(raw)
        if want == got:
            print(f'  {path:<26} 一致')
        else:
            bad.append(path)
            print(f'  {path:<26} 不一致  刻印 {want} / 中身 {got}')

    if not bad:
        print('すべて一致しています。')
        return 0

    print()
    print('刻印と中身が食い違っています。よくある原因は、')
    print('誰かの未コミットの変更に対して build-insights.py を回したことです。')
    print('コミットしてから `python tools/build-insights.py` を回し直してください。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
