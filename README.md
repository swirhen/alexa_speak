# Alexa Speak

自宅の Amazon Echo に、任意の文章を読み上げさせる HTTP API です。AI がリマインダーや通知を声で伝えるために使うことを想定しています。

- Alexa の Web API（非公式）を直接呼びます。[alexa-remote-control](https://github.com/thorsten-gehrig/alexa-remote-control) と [alexa-cookie2](https://github.com/Apollon77/alexa-cookie) の処理を Python に移植したもので、本体は標準ライブラリだけで動きます。
- 呼び出し元（AI など）ごとに ID とパスワードを発行できます。
- リモート MCP サーバーも付いていて、claude.ai のカスタムコネクタなどからツールとして使えます。
- 管理ページから、Amazon へのログイン、既定の端末と読み上げの音量の設定、ID と MCP コネクタの発行ができます。

```
呼び出し元（AI など）──HTTPS + Basic 認証──▶ リバースプロキシ ──▶ server.py ──▶ Alexa の Web API ──▶ Echo
AI のコネクタ ──HTTPS（URL に鍵）──▶ リバースプロキシ ──▶ mcp_server/server.py ──▶ server.py
```

> 日本の Amazon アカウント（amazon.co.jp）で作り、動かしています。ほかの国のアカウントでは確かめていません。

## AI 向け：使い方

この節だけ読めば使えるように書いています。URL・ID・パスワードは、持ち主から教えてもらってください。以下では、持ち主から教わった URL を `<ベース URL>`（例: `https://example.com/alexa_speak`）と書きます。

### 何が起きるか

`POST <ベース URL>/speak` に文章を送ると、**持ち主の家の Echo がその場で声に出して読み上げます**。家にいる人に実際に聞こえるので、頼まれたとき・決められた用途のときだけ送ってください。動作確認のための連投や、同じ内容の繰り返しは避けてください。

### 認証

Basic 認証です。ID とパスワードは持ち主が呼び出し元ごとに発行して、別の経路で渡します。

- 環境変数（以下の例では `ALEXA_SPEAK_URL`（ベース URL）・`ALEXA_SPEAK_ID`・`ALEXA_SPEAK_PASSWORD`）や秘密情報の保管場所から読み、コードやログ・会話・コミットに書き出さないでください。
- 持っていない場合は、推測や総当たりをせず、持ち主に尋ねてください（失敗が続くと接続元ごと締め出されます）。

### 送り方

```bash
curl -s -u "$ALEXA_SPEAK_ID:$ALEXA_SPEAK_PASSWORD" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"text":"15時から歯医者の予約です。そろそろ出かける準備をしましょう。"}' \
  "$ALEXA_SPEAK_URL/speak"
```

```python
import os, requests

r = requests.post(
    os.environ["ALEXA_SPEAK_URL"] + "/speak",
    auth=(os.environ["ALEXA_SPEAK_ID"], os.environ["ALEXA_SPEAK_PASSWORD"]),
    json={"text": "15時から歯医者の予約です。"},
    timeout=120,
)
print(r.status_code, r.json()["ok"])
```

| 項目 | 内容 |
| --- | --- |
| `text`（必須） | 読み上げる文章。300 文字まで |
| `device`（省略可） | 読み上げる端末名。通常は省略する（持ち主の決めた既定の端末で読み上げる）。端末名は `GET <ベース URL>/devices` で分かる |
| `chime`（省略可） | 読み上げの前にチャイムを鳴らすか。既定は `true`。続けて何通も送るときの 2 通目以降など、鳴らさないほうが自然なときだけ `false` にする |

応答は JSON で、`ok` が `true` なら読み上げを依頼できています。読み上げが始まるまで数秒〜十数秒かかることがあるので、タイムアウトは長め（60 秒以上）にしてください。

### 文章の書き方

画面ではなく耳で聞く文章です。

- 日本語の話し言葉で、短い文に区切る。全体で 1〜3 文、長くても 300 文字まで。
- 記号・URL・絵文字・Markdown・括弧書き・箇条書きは使わない（そのまま読まれるか、変な読み方になる）。
- 時刻は「15時30分」、日付は「10月25日」のように書く。「15:30」「10/25」は避ける。
- 読み間違えやすい固有名詞や略語は、ひらがな・カタカナにする。

### 応答とエラーへの対応

| 応答 | 意味 | 対応 |
| --- | --- | --- |
| 200 `{"ok": true}` | 読み上げを依頼できた | — |
| 400 | 文章が空・長すぎる、JSON が不正 | 直して送り直す |
| 401 | ID かパスワードが違う | 送り直さず、持ち主に伝える |
| 429 | 回数制限（読み上げは ID ごとに 1 分 10 回まで。認証の失敗が続いたときも） | しばらく待つ。繰り返さない |
| 502 | Alexa 側の失敗（ログイン情報の失効、端末名の間違いなど。`error` に理由が入る） | 送り直さず、持ち主に伝える |

### MCP（リモート MCP サーバー）

リモート MCP サーバーを登録できる AI（claude.ai のカスタムコネクタなど）は、持ち主から渡された URL（`<MCP の URL>/mcp/<名前>/<乱数>`）を登録すると、`speak` ツール（引数は `text` と `chime`）で読み上げられます。URL は AI ごとに別々で、認証の設定は不要です（URL に含まれる乱数が鍵の代わり）。ツールの使い方は、上の `/speak` と同じです。

## 構成

| ファイル | 内容 |
| --- | --- |
| `server.py` | API サーバー本体（標準ライブラリのみ）。読み上げ API・管理ページ・ID の管理 |
| `alexa_client.py` | Alexa の Web API のクライアント（refresh_token からのログイン、端末一覧、読み上げ、音量） |
| `alexa_auth.py` | Amazon へのログイン（refresh_token の取得） |
| `admin.html` | 管理ページ |
| `alexa-speak.service` | systemd の定義の例 |
| `alexa-speak.env.example` | 設定ファイルの雛形 |
| `mcp_server/` | リモート MCP サーバー（任意。公式 SDK `mcp` を使うので venv が必要） |

実行中に、リポジトリの直下に次のファイルが作られます。どれも Git 管理外で、秘密情報を含むものは権限 600 で書かれます。

| ファイル | 内容 |
| --- | --- |
| `alexa_token.json` | Amazon の refresh_token（管理ページからログインすると作られる） |
| `clients.json` | API の ID と MCP コネクタ（パスワードはソルト付きハッシュ） |
| `settings.json` | 既定の端末と読み上げの音量 |

## 必要なもの

- Linux などの常時動いているサーバー（Python 3.10 以上。例では systemd で動かす）
- HTTPS で公開できるリバースプロキシ（Apache・nginx など）。Basic 認証や URL に鍵を載せるので、**HTTP で公開しない**
- Echo を登録している Amazon アカウント

## 導入

以下では、配置先を `/opt/alexa_speak`、公開する URL を `https://example.com/alexa_speak/`（API と管理ページ）と `https://example.com/alexa_speak_mcp/`（MCP サーバー）として書きます。自分の環境に合わせて読み替えてください。

### 1. 配置と設定ファイル

```bash
sudo git clone https://github.com/swirhen/alexa_speak.git /opt/alexa_speak
sudo chown -R <動かすユーザー>: /opt/alexa_speak
sudo install -m 0600 -o root -g root /opt/alexa_speak/alexa-speak.env.example /etc/default/alexa-speak
sudoedit /etc/default/alexa-speak   # ALEXA_SPEAK_ADMIN を入れる（ほかは後から管理ページで設定できる）
```

設定項目は「[設定](#設定)」を参照。最低限、管理ページの管理者（`ALEXA_SPEAK_ADMIN`）を入れます。

### 2. systemd に登録する

`alexa-speak.service` は例です。`User`・`Group`・`WorkingDirectory`・`ExecStart` のパスを、自分の環境に合わせて書き換えてから登録します。日本向けの設定（`AMAZON=amazon.co.jp` など）もこのファイルにあります。

```bash
sudo install -m 0644 /opt/alexa_speak/alexa-speak.service /etc/systemd/system/   # 書き換えてから
sudo systemctl daemon-reload
sudo systemctl enable --now alexa-speak.service
curl -s http://127.0.0.1:8003/health   # {"ok": true}
```

API は `127.0.0.1:8003` で待ち受けます（`ALEXA_SPEAK_PORT` で変えられる）。

### 3. リバースプロキシで公開する

`/alexa_speak/` を `http://127.0.0.1:8003/` に中継します。MCP サーバーを使うなら、`/alexa_speak_mcp/` を `http://127.0.0.1:8004/` に中継します。

- `X-Forwarded-For` を付けてください。認証の失敗の回数制限を、接続元ごとに数えるのに使います（最後の値を接続元とみなす）。
- MCP サーバーには、`Host` ヘッダーを**書き換えて**（中継先の `127.0.0.1:8004` にして）渡してください。元の `Host` のまま渡すと、SDK の DNS リバインディング対策で拒否されます。Apache の `ProxyPass` と nginx の `proxy_pass` は、既定でそうなります。

Apache（`mod_proxy` と `mod_proxy_http` が必要。`X-Forwarded-For` は自動で付く）:

```apache
ProxyPass        /alexa_speak/ http://127.0.0.1:8003/
ProxyPassReverse /alexa_speak/ http://127.0.0.1:8003/
ProxyPass        /alexa_speak_mcp/ http://127.0.0.1:8004/
ProxyPassReverse /alexa_speak_mcp/ http://127.0.0.1:8004/
```

nginx:

```nginx
location /alexa_speak/ {
    proxy_pass http://127.0.0.1:8003/;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
location /alexa_speak_mcp/ {
    proxy_pass http://127.0.0.1:8004/;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

MCP サーバーを API と別の場所（別のパスやドメイン）で公開するときは、設定ファイルの `ALEXA_SPEAK_MCP_PUBLIC_URL` にその URL を書きます。管理ページに表示するコネクタの URL に使います。

### 4. 管理ページで Amazon にログインする

`https://example.com/alexa_speak/admin` を開き、`ALEXA_SPEAK_ADMIN` の ID とパスワードで入ります。「Amazon のログイン」からログインし（「[Amazon へのログイン](#amazon-へのログイン)」を参照）、「読み上げの設定」で既定の端末を選んで保存します。「今の設定でテスト読み上げ」で声が出れば完了です。

### 5. MCP サーバー（任意）

```bash
cd /opt/alexa_speak/mcp_server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo install -m 0644 alexa-speak-mcp.service /etc/systemd/system/   # パスとユーザーを書き換えてから
sudo systemctl daemon-reload
sudo systemctl enable --now alexa-speak-mcp.service
```

MCP サーバーは `127.0.0.1:8004` で待ち受けます（`ALEXA_SPEAK_MCP_PORT` で変えられる）。API サーバーの場所が `http://127.0.0.1:8003` でなければ、`ALEXA_SPEAK_BASE_URL` で指定します。MCP サーバー自身は秘密情報を持たないので、設定ファイルは要りません。

### 更新

```bash
cd /opt/alexa_speak && git pull --ff-only
sudo systemctl restart alexa-speak.service alexa-speak-mcp.service
```

systemd の定義を変えたときは `install` と `daemon-reload` を、`mcp_server/requirements.txt` を変えたときは `mcp_server/.venv/bin/pip install -r mcp_server/requirements.txt` もやり直します。

## 管理ページ

`<ベース URL>/admin` を開き、`ALEXA_SPEAK_ADMIN` の ID とパスワードで入ります（ブラウザの Basic 認証）。

### Amazon へのログイン

初回と、トークンが使えなくなったとき（Amazon のパスワードを変えた、端末登録を解除した、など）にだけ必要です。cookie は refresh_token から毎日自動で作り直すので、cookie の期限切れでやり直す必要はありません。

1. 「Amazon にログイン」を押し、表示されたリンクから Amazon のログイン画面を開く（普段のブラウザで、スマホでもよい）。
2. Echo を登録しているアカウントでログインする。2 段階認証も Amazon の画面でそのまま済ませる。
3. ログインが終わると「ページが見つかりません」と出る。そのときのアドレスバーの URL（`https://www.amazon.com/ap/maplanding?...openid.oa2.authorization_code=...`）をまるごとコピーし、管理ページに貼って「登録」を押す。認可コードは数分で使えなくなるので、すぐに貼る。
4. サーバーが Amazon に端末として登録して refresh_token を受け取り、そのトークンでログインと端末一覧の取得ができることを確かめてから保存し、すぐに使い始める。登録した「アカウントの国」が表示される。

- ログインと端末登録は、日本のアカウントでも amazon.com で行う（amazon.co.jp で行うと認可コードが付かない）。
- ログイン ID（メールアドレス・電話番号）がほかの国のアカウントと同じだと、そちらのアカウントとして扱われる。Echo を登録している国のアカウントだけで使っている ID でログインし、表示された「アカウントの国」を確かめる。
- ログインするたびに、Amazon のアカウントに端末（`alexa_speak`）が 1 つ登録される。この端末は Amazon の「コンテンツと端末」の Web ページには出てこない。不要になった古いトークンは、そのトークンでアクセストークンを取り（`POST https://api.amazon.com/auth/token`）、`POST https://api.amazon.com/auth/deregister`（`Authorization: Bearer <アクセストークン>`、本文 `{"deregister_all_existing_accounts": false}`）で解除できる。解除後は cookie への交換が `InvalidToken` になる。
- 管理ページを使わずに、ほかの方法で取った refresh_token（`Atnr|...`）を設定ファイルの `REFRESH_TOKEN` に書いてもよい（`alexa_token.json` が無いときに使われる）。

### 読み上げの設定

- 既定の端末：API で `device` を省いたときに読み上げる端末。Echo 系の端末とマルチルームのグループから選ぶ。
- 読み上げの音量（0〜100）：読み上げるときだけこの音量にして、終わったら元の音量に戻す（音量を変える → 読み上げる → 戻す、を 1 つの命令として送る）。0 なら音量を変えない。今の音量が取れなかった端末は、音量を変えずに読み上げる。

保存すると、再起動なしですぐ使われます。「今の設定でテスト読み上げ」で、保存した設定のまま読み上げを試せます。

### MCP コネクタ

MCP につなぐ AI ごとにコネクタを追加します。追加すると、その AI 専用の URL（`<MCP の公開 URL>/mcp/<名前>/<乱数>`）を 1 回だけ表示するので、AI のコネクタの設定に登録します（claude.ai なら「設定 → コネクタ → カスタムコネクタを追加」）。URL の作り直しと削除もでき、最終使用日時も見られます。

### API の ID

MCP を使わずに `/speak` を直接呼ぶ呼び出し元ごとに、ID とパスワードを発行します。パスワードはサーバーが乱数で作り、発行した画面でだけ表示します（あとから見ることはできないので、忘れたら再発行する）。

## API

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/health` | 死活確認（認証なし） |
| GET | `/devices` | 端末一覧。`{"ok": true, "devices": [{"name": "Echo Hub", "family": "KNIGHT", "online": true, "speaker": true}, ...]}` |
| POST | `/speak` | `{"text": "読み上げる文章", "device": "端末名", "chime": true}`。`device` を省くと既定の端末を使う。既定の音量が決めてあれば、その音量で読み上げて元に戻す。`chime` が `true`（既定）なら、文章を SSML で包んで前に `ALEXA_SPEAK_CHIME` の音を入れる。文章は 300 文字まで |
| GET | `/whoami` | Basic 認証の ID と種類（MCP サーバーが使う） |
| GET | `/admin` | 管理ページ（管理者の Basic 認証） |

- `/devices`・`/speak`・`/whoami` は Basic 認証が必要。
- 認証の失敗は、同じ接続元で 15 分に 5 回、全体で 15 分に 30 回を超えると 429 になる。読み上げは ID ごとに 1 分 10 回まで。記録はメモリ上だけで、再起動すると消える。
- 読み上げた内容は、呼び出し元の ID と一緒に標準出力のログに残る（systemd なら `journalctl -u alexa-speak`）。
- 応答の一覧は「[応答とエラーへの対応](#応答とエラーへの対応)」を参照。

## 設定

`/etc/default/alexa-speak`（雛形は `alexa-speak.env.example`）に書きます。変えたらサービスを再起動します。

| キー | 内容 |
| --- | --- |
| `ALEXA_SPEAK_ADMIN` | 管理ページの管理者。`id:password` の形で書く。空なら管理ページは使えない |
| `ALEXA_SPEAK_MCP_PUBLIC_URL` | MCP サーバーを公開している URL（例: `https://example.com/alexa_speak_mcp`）。管理ページに表示するコネクタの URL に使う。空なら、管理ページと同じ階層の `../alexa_speak_mcp` とみなす |
| `ALEXA_SPEAK_CHIME` | 読み上げの前に鳴らす音。Alexa の効果音ライブラリ（[ASK Sound Library](https://developer.amazon.com/en-US/docs/alexa/custom-skills/ask-soundlibrary.html)）の `soundbank://` URL。省略時はドアチャイム（`soundbank://soundlibrary/home/amzn_sfx_doorbell_chime_01`）、空にするとチャイムなし |
| `REFRESH_TOKEN` | Amazon の `Atnr|...` のトークン。通常は空でよい（管理ページからログインすると `alexa_token.json` に保存され、そちらが優先される） |
| `ALEXA_DEFAULT_DEVICE` | `device` を省いたときの端末名の初期値。管理ページで保存すると `settings.json` が優先される |

systemd の定義（`alexa-speak.service`）で指定しているもの:

| キー | 内容 |
| --- | --- |
| `AMAZON` / `ALEXA` / `TTS_LOCALE` | Amazon のドメイン・Alexa の Web API のホスト・読み上げの言語（既定は `amazon.co.jp` / `alexa.amazon.co.jp` / `ja-JP`） |
| `ALEXA_SPEAK_PORT` | 待ち受けポート（既定 8003。`127.0.0.1` だけで待ち受ける） |

## セキュリティ上の注意

- 必ず HTTPS で公開してください。Basic 認証のパスワードや MCP コネクタの URL がそのまま流れます。
- MCP コネクタは URL に含まれる乱数だけで守っています。URL を知っていれば誰でも読み上げられるので、AI に登録する以外には使わず、漏れたら管理ページで作り直してください。リバースプロキシのアクセスログには URL ごと残ります（MCP サーバー自身のログには、コネクタの名前しか残しません）。
- 管理者のパスワードは長いものにしてください。管理ページは Amazon のログイン情報と、すべての ID を扱えます。

## 動かなくなったとき

Alexa の Web API は非公式で、予告なく変わります。

- 読み上げや端末一覧が急に失敗するようになったら、[alexa-remote-control](https://github.com/thorsten-gehrig/alexa-remote-control) の最新版の `log_in` / `get_devlist` / `check_status` / `run_cmd` と `alexa_client.py` を見比べてください。
- 管理ページからの Amazon ログインが失敗するようになったら、[alexa-cookie2](https://github.com/Apollon77/alexa-cookie) の `lib/proxy.js`（ログイン URL）と `alexa-cookie.js` の `handleTokenRegistration` を `alexa_auth.py` と見比べてください。
