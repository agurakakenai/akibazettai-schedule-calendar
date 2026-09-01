@echo off
rem お給仕カレンダーをこの PC だけで表示します。ビルドや依存パッケージは不要です。
cd /d "%~dp0"
set "PORT=4173"

set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY where python3 >nul 2>&1 && set "PY=python3"
if not defined PY where python >nul 2>&1 && set "PY=python"

if not defined PY (
  echo Python が見つかりませんでした。
  echo https://www.python.org/downloads/ からインストールしてください。
  pause
  exit /b 1
)

echo お給仕カレンダー ローカルプレビュー
echo.
echo   http://localhost:%PORT%/ をブラウザーで開いてください。
echo   停止するには、このウィンドウで Ctrl+C を押してください。
echo.
rem 127.0.0.1 に固定しているため、同じネットワークの他の端末からは見えません。
%PY% -m http.server %PORT% --bind 127.0.0.1
