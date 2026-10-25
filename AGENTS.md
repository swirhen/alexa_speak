# AI開発エージェント向け運用ルール

## プロジェクト概要

- alexa-remote-control（bash スクリプト）を HTTP API でくるみ、Echo に文章を読み上げさせる。
- 使い方・設定・本番の導入手順は `README.md` を参照。
- リポジトリは公開している。README の「AI 向け：使い方」は、ほかの AI がそれだけ読んで API を使えるように書いている。API の仕様（パス・項目・制限・応答）を変えたら、必ずこの節も直す。

## 主なファイル

- `server.py`: API サーバー（標準ライブラリのみ）。スクリプトは cookie などを共有ファイルに書くため、ロックで直列に実行する。API の ID は `ClientStore` が `clients.json` で管理する。
- `admin.html`: ID の管理ページ（`/admin`、管理者の Basic 認証）。変更の操作は `X-Alexa-Speak-Admin: 1` ヘッダーが必須（ほかのサイトからの CSRF 対策）。
- `alexa-speak.service`: systemd 定義。日本向けの環境変数と cookie の保存先もここで指定する。
- `alexa-speak.env.example`: `/etc/default/alexa-speak` の雛形。
- `mcp_server/server.py`: `/speak` を呼ぶ MCP サーバー（複数の AI から接続する共通のリモート MCP サーバー、Streamable HTTP、ツールは `speak` だけ）。`/speak` は共通の ID `ai-web` で呼ぶ。認証情報は環境変数（`/etc/default/alexa-speak-mcp`）から読む。

## 注意

- `REFRESH_TOKEN`・`ALEXA_SPEAK_ADMIN`・`ALEXA_SPEAK_PASSWORD`・`ALEXA_SPEAK_MCP_PATH_TOKEN` の値と `clients.json` は表示・コミットしない。
- MCP サーバーは URL のトークンだけで守っている。公開するツールを増やすときは、URL を知った人に何ができるようになるかを考えること。
- API は `https://swirhen.tv/alexa_speak/` で外部に公開している。`/health` 以外は必ず Basic 認証を通すこと（認証なしの読み上げ口を作らない）。
- alexa-remote-control は `speak:` の文章を引用符なしの `echo` に通すため、`server.py` で glob 文字（`*?[]`）を取り除いている。
- スクリプトは失敗しても終了コード 0 を返すことがあるので、成否は出力の中身でも判定している。
- 本番の `/home/swirhen/alexa-remote-control` は upstream のまま使い、改変しない。

## 検証

- 変更後は `python -m py_compile server.py mcp_server/server.py` を実行する。
