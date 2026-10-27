"""Alexa の Web API（非公式）を呼ぶクライアント。読み上げに必要な部分だけを実装する。

alexa-remote-control（https://github.com/thorsten-gehrig/alexa-remote-control）の
処理を移植したもの。Amazon 側の仕様が変わって動かなくなったら、まずそちらの
最新の実装（log_in / get_devlist / check_status / run_cmd）と見比べる。

  1. refresh_token を cookie に交換する（POST https://api.<amazon>/ap/exchangetoken/cookies）
  2. /api/language などを呼んで csrf cookie を受け取る
  3. /api/users/me で customerId を取る
  4. /api/devices-v2/device で端末一覧を取る
  5. /api/behaviors/preview に Alexa.Speak のシーケンスを送る

cookie と端末一覧はメモリ上に持ち、再起動すると取り直す。
"""

import http.cookiejar
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:1.0) bash-script/1.0"
# cookie は 1 日で取り直す（alexa-remote-control の COOKIE_LIFETIME と同じ）
COOKIE_LIFETIME = 24 * 60 * 60
DEVICE_CACHE = 10 * 60
TIMEOUT = 30
# 読み上げに使える端末の種類（Echo 系とマルチルームのグループ）。管理ページの既定の端末の候補にする
SPEAKER_FAMILIES = {"ECHO", "KNIGHT", "ROOK", "WHA"}


class AlexaError(Exception):
    pass


class AlexaClient:
    def __init__(self, refresh_token: str, amazon: str, alexa: str, locale: str) -> None:
        self.refresh_token = refresh_token
        self.amazon = amazon  # 例: amazon.co.jp
        self.alexa = alexa  # 例: alexa.amazon.co.jp
        self.locale = locale  # 例: ja-JP
        self.lock = threading.Lock()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.customer_id = ""
        self.logged_in_at = 0.0
        self.devices: list[dict] = []
        self.devices_at = 0.0

    # ---- HTTP ----

    def _open(self, req: urllib.request.Request) -> tuple[int, bytes]:
        try:
            with self.opener.open(req, timeout=TIMEOUT) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, TimeoutError) as e:
            raise AlexaError(f"Amazon に接続できなかった: {e}") from e

    def _csrf(self) -> str:
        for c in self.jar:
            if c.name == "csrf" and c.domain.endswith(self.amazon):
                return c.value
        return ""

    def _api(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        headers = {
            "User-Agent": USER_AGENT,
            "DNT": "1",
            "Referer": f"https://alexa.{self.amazon}/spa/index.html",
            "Origin": f"https://alexa.{self.amazon}",
            "csrf": self._csrf(),
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"https://{self.alexa}{path}", data=data, headers=headers, method=method)
        return self._open(req)

    # ---- ログイン ----

    def _add_cookie(self, domain: str, c: dict) -> None:
        self.jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=c["Name"], value=c["Value"], port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
            path=c.get("Path") or "/", path_specified=True,
            secure=str(c.get("Secure")).lower() == "true",
            # 有効期限は Amazon の値を使わず、COOKIE_LIFETIME で取り直す
            expires=None, discard=True, comment=None, comment_url=None, rest={}, rfc2109=False,
        ))

    def set_refresh_token(self, refresh_token: str) -> None:
        """トークンを差し替え、次の呼び出しでログインし直させる。"""
        with self.lock:
            self.refresh_token = refresh_token
            self.customer_id = ""
            self.jar.clear()
            self.devices, self.devices_at = [], 0.0

    def check(self) -> list[dict]:
        """ログインし直してから端末一覧を取る（トークンが使えるかの確認用）。"""
        with self.lock:
            self._ensure_login(force=True)
            return self._summarize(self._load_devices(refresh=True))

    def _login(self) -> None:
        self.jar.clear()
        self.devices, self.devices_at = [], 0.0
        self.customer_id = ""
        if not self.refresh_token:
            raise AlexaError("Amazon にログインしていない。管理ページの「Amazon にログイン」から設定する")

        form = urllib.parse.urlencode({
            "app_name": "Amazon Alexa",
            "requested_token_type": "auth_cookies",
            "domain": f"www.{self.amazon}",
            "source_token_type": "refresh_token",
            "source_token": self.refresh_token,
        }).encode()
        req = urllib.request.Request(
            f"https://api.{self.amazon}/ap/exchangetoken/cookies", data=form,
            headers={"x-amzn-identity-auth-domain": f"api.{self.amazon}", "User-Agent": USER_AGENT},
        )
        status, raw = self._open(req)
        try:
            cookies = json.loads(raw)["response"]["tokens"]["cookies"]
        except (ValueError, KeyError, TypeError):
            raise AlexaError(f"refresh_token を cookie に交換できなかった（HTTP {status}）。トークンが失効した可能性がある")
        for domain, items in cookies.items():
            for c in items:
                self._add_cookie(domain, c)
        if not any(c.name.startswith("at-") and c.domain.endswith(self.amazon) for c in self.jar):
            raise AlexaError("交換した cookie に認証用の at- cookie が無い。REFRESH_TOKEN の国（AMAZON）を確認する")

        # csrf cookie は、どれかの API を呼ぶと付く
        for path in ("/api/language", "/templates/oobe/d-device-pick.handlebars", "/api/devices-v2/device?cached=false"):
            self._api("GET", path)
            if self._csrf():
                break
        else:
            raise AlexaError("csrf cookie を取得できなかった")

        status, raw = self._api("GET", "/api/users/me")
        try:
            self.customer_id = json.loads(raw)["id"]
        except (ValueError, KeyError, TypeError):
            raise AlexaError(f"ログインしたユーザーの情報を取得できなかった（HTTP {status}）")
        self.logged_in_at = time.time()

    def _ensure_login(self, force: bool = False) -> None:
        if force or not self.customer_id or time.time() - self.logged_in_at > COOKIE_LIFETIME:
            self._login()

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        """ログインしてから呼ぶ。401/403 のときは 1 回だけログインし直す。"""
        self._ensure_login()
        status, raw = self._api(method, path, body)
        if status in (401, 403):
            self._ensure_login(force=True)
            status, raw = self._api(method, path, body)
        return status, raw

    # ---- 端末 ----

    def _load_devices(self, refresh: bool) -> list[dict]:
        if refresh or not self.devices or time.time() - self.devices_at > DEVICE_CACHE:
            status, raw = self._call("GET", "/api/devices-v2/device?cached=false")
            try:
                self.devices = json.loads(raw)["devices"]
            except (ValueError, KeyError, TypeError):
                raise AlexaError(f"端末一覧を取得できなかった（HTTP {status}）")
            self.devices_at = time.time()
        return self.devices

    @staticmethod
    def _summarize(devices: list[dict]) -> list[dict]:
        return [
            {"name": d.get("accountName"), "family": d.get("deviceFamily"), "online": d.get("online"),
             "speaker": d.get("deviceFamily") in SPEAKER_FAMILIES}
            for d in devices
        ]

    def list_devices(self) -> list[dict]:
        with self.lock:
            return self._summarize(self._load_devices(refresh=True))

    def _targets(self, name: str) -> list[tuple[str, str]]:
        """端末名から、命令を送る (deviceType, serialNumber) の一覧を返す。"""
        devices = self._load_devices(refresh=False)
        found = next((d for d in devices if d.get("accountName") == name), None)
        if found is None:
            # 名前を変えた直後などのために、一覧を取り直してもう一度探す
            devices = self._load_devices(refresh=True)
            found = next((d for d in devices if d.get("accountName") == name), None)
        if found is None:
            raise AlexaError(f"端末「{name}」が見つからない")
        if found.get("deviceFamily") != "WHA":
            return [(found["deviceType"], found["serialNumber"])]
        # マルチルームのグループは、メンバーの端末それぞれに同時に送る
        by_serial = {d.get("serialNumber"): d for d in devices}
        members = [by_serial[s] for s in found.get("clusterMembers", []) if s in by_serial]
        if not members:
            raise AlexaError(f"グループ「{name}」にメンバーの端末が無い")
        return [(m["deviceType"], m["serialNumber"]) for m in members]

    # ---- 読み上げ ----

    def _node(self, command: str, device_type: str, serial: str, **payload: str) -> dict:
        return {
            "@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode",
            "type": command,
            "operationPayload": {
                "deviceType": device_type,
                "deviceSerialNumber": serial,
                "customerId": self.customer_id,
                "locale": self.locale,
                **payload,
            },
        }

    def _current_volumes(self) -> dict[str, int]:
        """端末ごと（シリアル番号）の今の音量。取れなければ空。"""
        status, raw = self._call("GET", "/api/devices/deviceType/dsn/audio/v1/allDeviceVolumes")
        try:
            return {v["dsn"]: int(v["speakerVolume"]) for v in json.loads(raw)["volumes"]}
        except (ValueError, KeyError, TypeError):
            return {}

    def speak(self, device: str, text: str, volume: int = 0) -> None:
        """text は普通の文章か、<speak> で始まる SSML。

        volume が 1〜100 なら、その音量にしてから読み上げ、終わったら元の音量に戻す
        （alexa-remote-control の SPEAKVOL と同じ）。0 なら音量は変えない。
        """
        parallel = "com.amazon.alexa.behaviors.model.ParallelNode"
        with self.lock:
            self._ensure_login()
            targets = self._targets(device)
            speak_nodes = [self._node("Alexa.Speak", t, s, textToSpeak=text) for t, s in targets]
            pre, post = [], []
            if volume:
                current = self._current_volumes()
                for t, s in targets:
                    # 今の音量が取れないときは戻し先が分からないので、その端末の音量は変えない
                    if s in current and current[s] != volume:
                        pre.append(self._node("Alexa.DeviceControls.Volume", t, s, value=str(volume)))
                        post.append(self._node("Alexa.DeviceControls.Volume", t, s, value=str(current[s])))
            if pre:
                # 音量を変える → 読み上げる → 元に戻す、を順番に（それぞれの中は全端末同時に）
                start = {
                    "@type": "com.amazon.alexa.behaviors.model.SerialNode",
                    "nodesToExecute": [
                        {"@type": parallel, "nodesToExecute": pre},
                        {"@type": parallel, "nodesToExecute": speak_nodes},
                        {"@type": parallel, "nodesToExecute": post},
                    ],
                }
            else:
                start = {"@type": parallel, "nodesToExecute": speak_nodes}
            sequence = {"@type": "com.amazon.alexa.behaviors.model.Sequence", "startNode": start}
            status, raw = self._call("POST", "/api/behaviors/preview", {
                "behaviorId": "PREVIEW",
                "sequenceJson": json.dumps(sequence, ensure_ascii=False),
                "status": "ENABLED",
            })
        if not 200 <= status < 300:
            detail = raw.decode("utf-8", "replace")[:200]
            raise AlexaError(f"読み上げの命令が受け付けられなかった（HTTP {status}）: {detail}")
