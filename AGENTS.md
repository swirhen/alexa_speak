# AI開発エージェント向け運用ルール

## プロジェクト概要

- Alexa の Web API（非公式）を HTTP API でくるみ、Echo に文章を読み上げさせる。Alexa とのやりとりは `alexa_client.py` で、alexa-remote-control（bash スクリプト）から必要な部分だけを Python に移植したもの。
- 使い方・設定・導入手順は `README.md` を参照。README は任意のサーバーで構築する前提で書いている（URL は `https://example.com/...`、配置先は `/opt/alexa_speak` などの例）。うちの環境に固有の値は README に書かず、この AGENTS.md の「うちの環境」に書く。
- リポジトリは公開している。README の「AI 向け：使い方」は、ほかの AI がそれだけ読んで API を使えるように書いている。API の仕様（パス・項目・制限・応答）を変えたら、必ずこの節も直す。

## 主なファイル

- `server.py`: API サーバー（標準ライブラリのみ）。API の ID と MCP コネクタは `ClientStore` が `clients.json` で、読み上げの設定は `SettingsStore` が `settings.json` で、Amazon のトークンは `TokenStore` が `alexa_token.json` で管理する。
- `alexa_auth.py`: Amazon へのログイン（alexa-cookie2 の端末登録の移植）。ログイン URL を作り、貼り付けられた `/ap/maplanding` の URL から認可コードを取り出して `/auth/register` で refresh_token を受け取る。プロキシは使わない。
- `alexa_client.py`: Alexa の Web API のクライアント（標準ライブラリのみ）。refresh_token → cookie の交換、csrf・customerId の取得、端末一覧、`Alexa.Speak` の送信、読み上げ時の音量の切り替え。cookie と端末一覧はメモリ上に持つ。
- `admin.html`: 管理ページ（`/admin`、管理者の Basic 認証）。変更の操作は `X-Alexa-Speak-Admin: 1` ヘッダーが必須（ほかのサイトからの CSRF 対策）。
- `alexa-speak.service` / `mcp_server/alexa-speak-mcp.service`: systemd 定義。README では「例」として扱っている（下の「うちの環境」の値がそのまま入っている）。
- `alexa-speak.env.example`: `/etc/default/alexa-speak` の雛形。
- `mcp_server/server.py`: `/speak` を呼ぶ MCP サーバー（複数の AI から接続する共通のリモート MCP サーバー、Streamable HTTP、ツールは `speak` だけ）。URL `/mcp/<名前>/<乱数>` の名前と乱数（管理ページで発行する `kind: mcp` の ID）をそのまま `/speak` の Basic 認証に使う。秘密情報は持たない。

## 注意

- `REFRESH_TOKEN`・`ALEXA_SPEAK_ADMIN` の値、MCP コネクタの URL、`clients.json`・`alexa_token.json` の中身は表示・コミットしない。
- MCP サーバーは URL に含まれる乱数だけで守っている。公開するツールを増やすときは、URL を知った人に何ができるようになるかを考えること。MCP サーバーのログに URL（乱数）を出さないこと（uvicorn のアクセスログは止めてある）。
- API は外部に公開している。`/health` 以外は必ず Basic 認証を通すこと（認証なしの読み上げ口を作らない）。
- Alexa の Web API は非公式で、予告なく変わる。読み上げや端末一覧が急に失敗するようになったら、まず upstream の alexa-remote-control（https://github.com/thorsten-gehrig/alexa-remote-control）の最新版の `log_in` / `get_devlist` / `check_status` / `run_cmd` と `alexa_client.py` を見比べる。管理ページからの Amazon ログインが失敗するようになったら、alexa-cookie2（https://github.com/Apollon77/alexa-cookie）の `lib/proxy.js` と `alexa-cookie.js` の `handleTokenRegistration` を `alexa_auth.py` と見比べる。

## うちの環境

- サーバー: `prod-checker`（SSH の Host 名）。配置先は `/home/swirhen/alexa_speak`（README の `/opt/alexa_speak` に当たる）。動かすユーザーは `swirhen`。
- 公開 URL: API と管理ページは `https://swirhen.tv/alexa_speak/`、MCP サーバーは `https://swirhen.tv/alexa_speak_mcp/`（管理ページの隣のパスなので、`ALEXA_SPEAK_MCP_PUBLIC_URL` は空のまま）。
- リバースプロキシ: Apache。設定は dotfile リポジトリの `apache2/sites-available/000-default.conf` にあり、本番の `/etc/apache2/sites-available/000-default.conf` はそこへのシンボリックリンク。dotfile を `git pull` してから `sudo apachectl configtest && sudo systemctl reload apache2` で反映する。
- systemd: `alexa-speak.service`（システムの `python3`）と `alexa-speak-mcp.service`（`mcp_server/.venv`）。リポジトリの定義ファイルをそのまま `install` している。
- 設定ファイル: `/etc/default/alexa-speak`（root・600）。同じ内容のものを手元の `alexa_speak/alexa-speak.env`（Git 管理外）にも置いていて、書き換えたら本番に送ってから再起動する。
- Amazon: 日本のアカウント。トークンは管理ページから取得した `alexa_token.json` を使っている（設定ファイルの `REFRESH_TOKEN` は空）。

### 更新のリリース

```bash
cd /home/swirhen/alexa_speak && git pull --ff-only && sudo systemctl restart alexa-speak.service alexa-speak-mcp.service
```

systemd の定義を変えたときは、`sudo install -m 0644 alexa-speak.service mcp_server/alexa-speak-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload` もやり直す。

## 検証

- 変更後は `python -m py_compile server.py alexa_client.py alexa_auth.py mcp_server/server.py` を実行する。
