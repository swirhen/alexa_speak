# Alexa Speak

[alexa-remote-control](https://github.com/thorsten-gehrig/alexa-remote-control) を HTTP から呼び、Echo に任意の文章を読み上げさせる API です。リマインダーなどを AI で作った文章で読み上げさせるための土台です。

```
呼び出し元 ──HTTP──▶ server.py (127.0.0.1:8003) ──▶ alexa_remote_control.sh ──▶ Alexa
```

## API

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/health` | 死活確認 |
| GET | `/devices` | 端末一覧（`alexa_remote_control.sh -a` の出力） |
| POST | `/speak` | `{"text": "読み上げる文章", "device": "端末名"}`。`device` を省くと `ALEXA_DEFAULT_DEVICE` を使う。文章は 300 文字まで |

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"text":"テストです。聞こえますか？"}' http://127.0.0.1:8003/speak
```

## 設定

`alexa-speak.env.example` を `/etc/default/alexa-speak` にコピーし（root・600）、値を入れます。

| キー | 内容 |
| --- | --- |
| `REFRESH_TOKEN` | alexa-cookie-cli で取得する `Atnr|...` のトークン |
| `ALEXA_DEFAULT_DEVICE` | `device` を省いたときの端末名（`/devices` で表示される名前） |
| `SPEAKVOL` | 読み上げ時の音量（0 は変更しない） |

日本向けの設定（`AMAZON=amazon.co.jp` など）と cookie の保存先は `alexa-speak.service` に書いています。

### REFRESH_TOKEN の取得

[alexa-cookie-cli](https://github.com/adn77/alexa-cookie-cli/releases) を手元の PC で実行し、表示された `http://127.0.0.1:8080` で Amazon にログインします。

```
alexa-cookie-cli --amazonPage amazon.co.jp --baseAmazonPage amazon.co.jp --acceptLanguage ja-JP
```

パスワード変更などで失効したら取り直して `/etc/default/alexa-speak` を書き換え、サービスを再起動します。

## 本番

- 配置先: `/home/swirhen/alexa_speak`（`prod-checker`）
- 本体: `/home/swirhen/alexa-remote-control`（upstream をクローンしたもの。改変しない）
- systemd: `alexa-speak.service`。追加の Python パッケージは不要（システムの `python3` で動く）

### 初期導入

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

### 更新のリリース

```bash
cd /home/swirhen/alexa_speak && git pull --ff-only && sudo systemctl restart alexa-speak.service
```

`alexa-speak.service` を変えたときは、`install` と `daemon-reload` もやり直します。
