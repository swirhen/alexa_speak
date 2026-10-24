# Alexa Speak

[alexa-remote-control](https://github.com/thorsten-gehrig/alexa-remote-control) を HTTP から呼び、家の Amazon Echo に任意の文章を読み上げさせる API です。AI がリマインダーや通知を声で伝えるために使います。

```
呼び出し元（AI など）──HTTPS + Basic 認証──▶ Apache (https://swirhen.tv/alexa_speak/)
  ──▶ server.py (127.0.0.1:8003) ──▶ alexa_remote_control.sh ──▶ Alexa
```

## AI 向け：使い方

この節だけ読めば使えるように書いています。

### 何が起きるか

`POST https://swirhen.tv/alexa_speak/speak` に文章を送ると、**持ち主の家の Echo がその場で声に出して読み上げます**。家にいる人に実際に聞こえるので、頼まれたとき・決められた用途のときだけ送ってください。動作確認のための連投や、同じ内容の繰り返しは避けてください。

### 認証

Basic 認証です。ID とパスワードは持ち主が呼び出し元ごとに発行して、別の経路で渡します。

- 環境変数（例：`ALEXA_SPEAK_ID` / `ALEXA_SPEAK_PASSWORD`）や秘密情報の保管場所から読み、コードやログ・会話・コミットに書き出さないでください。
- 持っていない場合は、推測や総当たりをせず、持ち主に尋ねてください（失敗が続くと接続元ごと締め出されます）。

### 送り方

```bash
curl -s -u "$ALEXA_SPEAK_ID:$ALEXA_SPEAK_PASSWORD" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"text":"15時から歯医者の予約です。そろそろ出かける準備をしましょう。"}' \
  https://swirhen.tv/alexa_speak/speak
```

```python
import os, requests

r = requests.post(
    "https://swirhen.tv/alexa_speak/speak",
    auth=(os.environ["ALEXA_SPEAK_ID"], os.environ["ALEXA_SPEAK_PASSWORD"]),
    json={"text": "15時から歯医者の予約です。"},
    timeout=120,
)
print(r.status_code, r.json()["ok"])
```

| 項目 | 内容 |
| --- | --- |
| `text`（必須） | 読み上げる文章。300 文字まで |
| `device`（省略可） | 読み上げる端末名。通常は省略する（持ち主の決めた既定の端末で読み上げる）。端末名は `GET /devices` で分かる |

応答は JSON で、`ok` が `true` なら読み上げを依頼できています。読み上げが始まるまで数秒〜十数秒かかることがあるので、タイムアウトは長め（60 秒以上）にしてください。

### 文章の書き方

画面ではなく耳で聞く文章です。

- 日本語の話し言葉で、短い文に区切る。全体で 1〜3 文、長くても 300 文字まで。
- 記号・URL・絵文字・Markdown・括弧書き・箇条書きは使わない（そのまま読まれるか、変な読み方になる）。
- 時刻は「15時30分」、日付は「10月25日」のように書く。「15:30」「10/25」は避ける。
- 読み間違えやすい固有名詞や略語は、ひらがな・カタカナにする。
- `* ? [ ]` は送っても取り除かれる。

### 応答とエラーへの対応

| 応答 | 意味 | 対応 |
| --- | --- | --- |
| 200 `{"ok": true}` | 読み上げを依頼できた | — |
| 400 | 文章が空・長すぎる、JSON が不正 | 直して送り直す |
| 401 | ID かパスワードが違う | 送り直さず、持ち主に伝える |
| 429 | 回数制限（読み上げは ID ごとに 1 分 10 回まで。認証の失敗が続いたときも） | しばらく待つ。繰り返さない |
| 502 | Alexa 側の失敗（ログイン情報の失効など） | 送り直さず、持ち主に伝える |
| 504 | タイムアウト | 1 回だけ待ってから送り直してよい |

## 運用者向け

### API

URL は `https://swirhen.tv/alexa_speak/<パス>`（サーバー内からは `http://127.0.0.1:8003/<パス>`）。

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/health` | 死活確認（認証なし） |
| GET | `/admin` | ID の管理ページ（管理者でログイン） |
| GET | `/devices` | 端末一覧（`alexa_remote_control.sh -a` の出力） |
| POST | `/speak` | `{"text": "読み上げる文章", "device": "端末名"}`。`device` を省くと `ALEXA_DEFAULT_DEVICE` を使う。文章は 300 文字まで |

```bash
curl -s -u <ID>:<パスワード> -X POST -H 'Content-Type: application/json' \
  -d '{"text":"テストです。聞こえますか？"}' https://swirhen.tv/alexa_speak/speak
```

- `/devices` と `/speak` は Basic 認証が必要。ID とパスワードは管理ページで呼び出し元ごとに発行する。
- 認証の失敗は、同じ接続元で 15 分に 5 回、全体で 15 分に 30 回を超えると 429 になる。読み上げは ID ごとに 1 分 10 回まで。記録はメモリ上だけで、再起動すると消える。
- 読み上げた内容は、呼び出し元の ID と一緒に journal に残る（`journalctl -u alexa-speak`）。

応答の一覧は「AI 向け：使い方」を参照。

### 設定

`alexa-speak.env.example` を `/etc/default/alexa-speak` にコピーし（root・600）、値を入れます。

| キー | 内容 |
| --- | --- |
| `REFRESH_TOKEN` | alexa-cookie-cli で取得する `Atnr|...` のトークン |
| `ALEXA_DEFAULT_DEVICE` | `device` を省いたときの端末名（`/devices` で表示される名前） |
| `SPEAKVOL` | 読み上げ時の音量（0 は変更しない） |
| `ALEXA_SPEAK_ADMIN` | 管理ページの管理者。`id:password` の形で書く。空なら管理ページは使えない |

#### API の ID の管理

`https://swirhen.tv/alexa_speak/admin` を開き、`ALEXA_SPEAK_ADMIN` の ID とパスワードでログインする。ID の追加・パスワードの再発行・削除ができ、最終使用日時も見られる。

- パスワードはサーバーが乱数で作り、発行した画面でだけ表示する（保存するのはソルト付きのハッシュだけなので、あとから見ることはできない）。忘れたら再発行する。
- ID は `clients.json`（リポジトリ直下、Git 管理外、権限 600）に保存する。
- `clients.json` が無い状態で起動すると、`ALEXA_SPEAK_CLIENTS`（`id:password` のカンマ区切り。以前の設定方法）の ID を取り込む。

日本向けの設定（`AMAZON=amazon.co.jp` など）と cookie の保存先は `alexa-speak.service` に書いています。

#### REFRESH_TOKEN の取得

[alexa-cookie-cli](https://github.com/adn77/alexa-cookie-cli) を手元の PC で実行し、表示された `http://127.0.0.1:8080` で Amazon にログインします。「cookie が取れた」旨のページが出たら成功で、ターミナルに `refreshToken: Atnr|...` が出ます（その後も CPU を使い続けるので Ctrl+C で止める）。

```
alexa-cookie-cli -p amazon.co.jp -b amazon.com -a ja_JP -L ja-JP -B 127.0.0.1
```

- `-b`（ログイン窓口）は日本のアカウントでも **amazon.com** にする。`amazon.co.jp` にすると、ログイン後の `/ap/maplanding` に認可コードが付かず「有効なページではない」と出て失敗する。`-p`（Alexa を使う国）は `amazon.co.jp`。
- ログイン ID（メールアドレス・電話番号）が US などほかの国のアカウントと同じだと、そちらのアカウントとして扱われる。JP アカウントだけで使っている ID でログインする。
- `-B 127.0.0.1` を付けないと、ログイン用のプロキシが LAN に公開される。

v5.0.5 の Windows 版 exe は起動時に `V8 rejected the bytecode cache` で落ちるため、ソースを Node.js で動かす。alexa-cookie2 は 2段階認証での 404 が直った 5.0.6 以上にする。

```powershell
git clone --depth 1 --branch v5.0.5 https://github.com/adn77/alexa-cookie-cli.git $env:LOCALAPPDATA\alexa-cookie-cli
cd $env:LOCALAPPDATA\alexa-cookie-cli; npm ci --omit=dev; npm install alexa-cookie2@5.0.6 --omit=dev --no-save
# PATH の通ったフォルダに alexa-cookie-cli.cmd を置く
# 中身: @node "%LOCALAPPDATA%\alexa-cookie-cli\alexa-cookie-cli.js" %*
```

パスワード変更などで失効したら取り直して `/etc/default/alexa-speak` を書き換え、サービスを再起動します。

### 本番

- 配置先: `/home/swirhen/alexa_speak`（`prod-checker`）
- 本体: `/home/swirhen/alexa-remote-control`（upstream をクローンしたもの。改変しない）
- systemd: `alexa-speak.service`。追加の Python パッケージは不要（システムの `python3` で動く）
- 公開 URL: `https://swirhen.tv/alexa_speak/`（Apache が `127.0.0.1:8003` へ中継）。Apache 設定は dotfile リポジトリの `apache2/sites-available/000-default.conf`

#### 初期導入

```bash
cd /home/swirhen
git clone https://github.com/thorsten-gehrig/alexa-remote-control.git
git clone git@github.com:swirhen/alexa_speak.git
mkdir -p ~/.cache/alexa-remote-control && chmod 700 ~/.cache/alexa-remote-control
cd alexa_speak
sudo install -m 0600 -o root -g root alexa-speak.env.example /etc/default/alexa-speak   # 値を入れる
sudo install -m 0644 alexa-speak.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alexa-speak.service
```

#### 更新のリリース

```bash
cd /home/swirhen/alexa_speak && git pull --ff-only && sudo systemctl restart alexa-speak.service
```

`alexa-speak.service` を変えたときは、`install` と `daemon-reload` もやり直します。
