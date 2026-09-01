# アキバ絶対領域 お給仕カレンダー

アキバ絶対領域の公開情報を、昼・夜に分けた月間カレンダーで閲覧するための静的サイトです。メイド・日付で絞り込めます。

公開サイト: <https://agurakakenai.github.io/akibazettai-schedule-calendar/>

## ローカルプレビュー

依存パッケージやビルドは不要です。リポジトリのルートで静的サーバーを起動します。

```powershell
python -m http.server 4173
```

ブラウザーで <http://localhost:4173> を開いてください。

データ検証:

```powershell
node tests/validate-schedule.js
```

## お給仕情報の更新

表示データは [`data/schedule.js`](data/schedule.js) だけに集約しています。描画処理は [`app.js`](app.js) に分離されているため、通常の予定更新で HTML や JavaScript のロジックを変更する必要はありません。

1. `lastUpdated` を更新します。
2. 必要なら `defaultDateFrom` と `defaultDateTo` を更新します。
3. `schedule` の日付に、`昼` と `夜` の配列を追加・編集します。
4. 記念日・生誕の主役には `featured: true` と、読み上げ・ツールチップ用の `eventLabel` を指定します。
5. `node tests/validate-schedule.js` を実行します。

予定が未確認の日はキーを追加しません。フィルターには `roster` の全員が公式順で表示されるため、予定がないメイドも削除しないでください。

## デプロイ

`main` への push または手動実行で、[GitHub Pages ワークフロー](.github/workflows/deploy-pages.yml)がデータを検証して静的サイトを公開します。HTML、CSS、JavaScript、予定データはリポジトリのプロジェクトパスで動作する相対パスを使用しています。
