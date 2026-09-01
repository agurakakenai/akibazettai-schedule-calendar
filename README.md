# アキバ絶対領域 お給仕カレンダー

アキバ絶対領域の公開情報を、昼・夜に分けた月間カレンダーで閲覧するための静的サイトです。メイド・日付で絞り込めます。

**このリポジトリは非公開（private）です。** 一般公開していた GitHub Pages は停止済みで、閲覧はローカルプレビューで行います。

## ローカルプレビュー

依存パッケージやビルドは不要です。[`preview.cmd`](preview.cmd) をダブルクリックし、ブラウザーで <http://localhost:4173> を開いてください。

コマンドで起動する場合は、リポジトリのルートで次を実行します。

```powershell
py -m http.server 4173 --bind 127.0.0.1
```

`--bind 127.0.0.1` を付けているため、同じネットワークの他の端末からは見えません。

> Windows で `python` がマイクロソフトストアのスタブになっていることがあります。その場合は `py` を使ってください。

データ検証:

```powershell
node tests/validate-schedule.js
node tests/date-defaults.js
node tests/range-rendering.js
```

## お給仕情報の更新

表示データは [`data/schedule.js`](data/schedule.js) だけに集約しています。描画処理は [`app.js`](app.js) に分離されているため、通常の予定更新で HTML や JavaScript のロジックを変更する必要はありません。

1. `lastUpdated` を更新します。
2. `schedule` の日付に、`昼` と `夜` の配列を追加・編集します。
3. 記念日・生誕の主役には `featured: true` と、読み上げ・ツールチップ用の `eventLabel` を指定します。
4. メイド服を着ないキッチンにゃんこは `kitchenStaff` に入れます。カレンダーとフィルターに 🍳 が付きます。
5. `node tests/validate-schedule.js` を実行します。

各シフトの並び順は `roster` の公式順と一致させてください（検証スクリプトが確認します）。

初期表示月と日付範囲は、アクセス時の日本時間から自動計算されます。開始日は当日、終了日は当日が1〜15日なら15日、16日以降なら同じ月の末日です。

予定が未確認の日はキーを追加しません。フィルターには `roster` の全員が公式順で表示されるため、予定がないメイドも削除しないでください。

絞り込みで誰も残らなかったシフトは `-`、そもそも公開情報を確認できていないシフトは「確認情報なし」と表示します。

## 公開状態

リポジトリは非公開です。無料プランの GitHub Pages は公開リポジトリでしか使えないため、Pages は無効化してあります。

`main` への push では [ワークフロー](.github/workflows/deploy-pages.yml)の検証ジョブだけが動き、サイトは公開されません。再び一般公開する場合だけ、リポジトリを公開に戻したうえで Actions タブからワークフローを手動実行してください。

HTML、CSS、JavaScript、予定データはリポジトリのプロジェクトパスで動作する相対パスを使用しています。

### 自分だけが見られる形で公開したい場合

| 方法 | リポジトリ | サイト | 費用 |
|---|---|---|---|
| GitHub Pages（無料プラン） | 公開が必須 | 誰でも閲覧可 | 無料 |
| GitHub Pages（Pro） | 非公開にできる | **URL を知っていれば誰でも閲覧可** | 月 $4 |
| Azure Static Web Apps | 非公開のまま | **許可した本人だけ** | 無料枠 |
| Cloudflare Tunnel + Access | 非公開のまま | 許可した本人だけ | 無料（独自ドメインが必要） |

GitHub Pages は Pro にしてもサイト自体は公開されます。サイトを本人だけに限定できるのは GitHub Enterprise Cloud だけです。

[`staticwebapp.config.json`](staticwebapp.config.json) には Azure Static Web Apps 用の設定を用意済みです。全ページを `schedule-reader` ロールに限定し、未認証のアクセスは GitHub ログインへリダイレクト、ロールがないアカウントには [`unauthorized.html`](unauthorized.html) を返します。

#### Azure Static Web Apps で非公開のまま閲覧できるようにする手順

1. [Azure Portal](https://portal.azure.com/) で「静的 Web アプリ」を作成し、プランは **Free** を選びます。
2. デプロイソースに GitHub を選び、このリポジトリと `main` ブランチを指定します。
3. ビルドのプリセットは「カスタム」、アプリの場所は `/`、API の場所は空、出力先は `/` にします（ビルド不要のため）。
4. 作成すると `.github/workflows/azure-static-web-apps-*.yml` が自動で追加され、以降は push でデプロイされます。
5. リソースの **設定 → ロールの管理 → 招待** を開きます。
   - 承認プロバイダー: **GitHub**
   - 招待するユーザー: 自分の GitHub **ユーザー名**（Entra ID を選んだ場合はメールアドレス）
   - ロール: `schedule-reader`
   - 有効期限: 最大 168 時間（7日）
6. 生成された招待リンクを自分で開き、GitHub でサインインします。**リンクを開いて初めてロールが付与されます。**

これで、サイトの URL にアクセスできるのはロールを付与したアカウントだけになります。ロールのないアカウントがサインインしても [`unauthorized.html`](unauthorized.html) が返るだけです。リポジトリは非公開のままで構いません。

補足:

- 事前構成済みのログイン方法は **GitHub と Microsoft Entra ID** で、Free プランでも追加設定なしに使えます。Google などを足す場合は Standard プランが必要です。
- 招待によるロール付与は全プランで使えます。Standard 限定なのは「関数でロールを自動割り当てする」機能だけです。
- Free プランの上限は帯域 100 GB/月・アプリサイズ 250 MB・カスタムドメイン 2 件で、このサイトの規模なら十分です。
- アクセスを取り消したいときは、同じ「ロールの管理」からユーザーを削除します。
