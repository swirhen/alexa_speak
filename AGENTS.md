# AI開発エージェント向け運用ルール

## プロジェクト概要

- Alexa の Web API（非公式）を HTTP API でくるみ、Echo に文章を読み上げさせる。Alexa とのやりとりは `alexa_client.py` で、alexa-remote-control（bash スクリプト）から必要な部分だけを Python に移植したもの。
- 使い方・設定・本番の導入手順は `README.md` を参照。
- リポジトリは公開している。README の「AI 向け：使い方」は、ほかの AI がそれだけ読んで API を使えるように書いている。API の仕様（パス・項目・制限・応答）を変えたら、必ずこの節も直す。

## 主なファイル

- `server.py`: API サーバー（標準ライブラリのみ）。API の ID は `ClientStore` が `clients.json` で管理する。
- `alexa_auth.py`: Amazon へのログイン（alexa-cookie2 の端末登録の移植）。ログイン URL を作り、貼り付けられた `/ap/maplanding` の URL から認可コードを取り出して `/auth/register` で refresh_token を受け取る。プロキシは使わない。
- `alexa_client.py`: Alexa の Web API のクライアント（標準ライブラリのみ）。refresh_token → cookie の交換、csrf・customerId の取得、端末一覧、`Alexa.Speak` の送信。cookie と端末一覧はメモリ上に持つ。
- `settings.json`: 管理ページで変える読み上げの設定（既定の端末・読み上げの音量）。Git 管理外。`server.py` の `SettingsStore` が読み書きする。
- `admin.html`: ID の管理ページ（`/admin`、管理者の Basic 認証）。変更の操作は `X-Alexa-Speak-Admin: 1` ヘッダーが必須（ほかのサイトからの CSRF 対策）。
- `alexa-speak.service`: systemd 定義。日本向けの環境変数（`AMAZON`・`ALEXA`・`TTS_LOCALE`）もここで指定する。
- `alexa-speak.env.example`: `/etc/default/alexa-speak` の雛形。
- `mcp_server/server.py`: `/speak` を呼ぶ MCP サーバー（複数の AI から接続する共通のリモート MCP サーバー、Streamable HTTP、ツールは `speak` だけ）。URL `/mcp/<名前>/<乱数>` の名前と乱数（管理ページで発行する `kind: mcp` の ID）をそのまま `/speak` の Basic 認証に使う。秘密情報は持たない。

## 注意

- `REFRESH_TOKEN`・`ALEXA_SPEAK_ADMIN`・`ALEXA_SPEAK_PASSWORD` の値、MCP コネクタの URL と `clients.json`・`alexa_token.json` は表示・コミットしない。
- MCP サーバーは URL に含まれる乱数だけで守っている。公開するツールを増やすときは、URL を知った人に何ができるようになるかを考えること。
- API は `https://swirhen.tv/alexa_speak/` で外部に公開している。`/health` 以外は必ず Basic 認証を通すこと（認証なしの読み上げ口を作らない）。
- Alexa の Web API は非公式で、予告なく変わる。読み上げや端末一覧が急に失敗するようになったら、まず upstream の alexa-remote-control（https://github.com/thorsten-gehrig/alexa-remote-control）の最新版の `log_in` / `get_devlist` / `check_status` / `run_cmd` と `alexa_client.py` を見比べる。管理ページからの Amazon ログインが失敗するようになったら、alexa-cookie2（https://github.com/Apollon77/alexa-cookie）の `lib/proxy.js` と `alexa-cookie.js` の `handleTokenRegistration` を `alexa_auth.py` と見比べる。

## 検証

- 変更後は `python -m py_compile server.py alexa_client.py alexa_auth.py mcp_server/server.py` を実行する。
