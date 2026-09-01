# アキバ絶対領域 お給仕カレンダー

アキバ絶対領域の公開情報を、昼・夜に分けた月間カレンダーで閲覧するための静的サイトです。メイド・日付で絞り込めます。

このリポジトリは **private のまま運用**します。公開先も Azure Static Web Apps の GitHub ログインと `schedule-reader` ロールで制限し、ログインしただけの GitHub ユーザーには表示しません。

## ローカルプレビュー

依存パッケージやビルドは不要です。リポジトリのルートで静的サーバーを起動します。

```powershell
python -m http.server 4173
```

ブラウザーで <http://localhost:4173> を開いてください。ローカルサーバーでは Azure のアクセス制御は動作しません。

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

## Azure Static Web Apps の準備

1. Azure Portal で **Static Web App** リソースを作成します。ソースは GitHub、リポジトリは private の `agurakakenai/akibazettai-schedule-calendar`、デプロイ方法は後述の手動ワークフローを使います。
2. Azure Static Web Apps リソースの **Manage deployment token** からデプロイトークンを取得します。
3. GitHub リポジトリの **Settings → Secrets and variables → Actions** で、Repository secret `AZURE_STATIC_WEB_APPS_API_TOKEN_SCHEDULE_CALENDAR` を作成し、デプロイトークンを保存します。
4. GitHub の **Actions → Deploy to Azure Static Web Apps → Run workflow** を実行します。ワークフローは `workflow_dispatch` 専用なので、Azure の準備前や通常の push では実行・失敗しません。

サイトのルート、CSS、JavaScript、データは [`staticwebapp.config.json`](staticwebapp.config.json) により `schedule-reader` ロールで保護されます。`/.auth/*` と `unauthorized.html` は、ログイン・ログアウトと権限エラー表示のため保護対象から除外しています。

## 閲覧者を招待

初回デプロイ後、Azure Portal で対象の Static Web App を開きます。

1. **Settings → Role management → Invite** を開きます。
2. Identity provider に **GitHub**、GitHub user details に **`agurakakenai`**、Role に **`schedule-reader`** を指定します。
3. 招待リンクを対象ユーザーに渡し、GitHub でログインして招待を受諾してもらいます。

`authenticated` ロールには閲覧権限を与えていません。招待された `agurakakenai` が `schedule-reader` を取得した場合だけカレンダーを表示できます。未ログイン時は GitHub ログインへ、ログイン済みでロールがない場合は `unauthorized.html` へ案内されます。
