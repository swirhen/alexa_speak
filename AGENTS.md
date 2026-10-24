# AI開発エージェント向け運用ルール

## プロジェクト概要

- alexa-remote-control（bash スクリプト）を HTTP API でくるみ、Echo に文章を読み上げさせる。
- 使い方・設定・本番の導入手順は `README.md` を参照。

## 主なファイル

- `server.py`: API サーバー（標準ライブラリのみ）。スクリプトは cookie などを共有ファイルに書くため、ロックで直列に実行する。API の ID は `ClientStore` が `clients.json` で管理する。
- `admin.html`: ID の管理ページ（`/admin`、管理者の Basic 認証）。変更の操作は `X-Alexa-Speak-Admin: 1` ヘッダーが必須（ほかのサイトからの CSRF 対策）。
- `alexa-speak.service`: systemd 定義。日本向けの環境変数と cookie の保存先もここで指定する。
- `alexa-speak.env.example`: `/etc/default/alexa-speak` の雛形。

## 注意

- `REFRESH_TOKEN`・`ALEXA_SPEAK_ADMIN` の値と `clients.json` は表示・コミットしない。
- API は `https://swirhen.tv/alexa_speak/` で外部に公開している。`/health` 以外は必ず Basic 認証を通すこと（認証なしの読み上げ口を作らない）。
- alexa-remote-control は `speak:` の文章を引用符なしの `echo` に通すため、`server.py` で glob 文字（`*?[]`）を取り除いている。
- スクリプトは失敗しても終了コード 0 を返すことがあるので、成否は出力の中身でも判定している。
- 本番の `/home/swirhen/alexa-remote-control` は upstream のまま使い、改変しない。

## 検証

- 変更後は `python -m py_compile server.py` を実行する。
