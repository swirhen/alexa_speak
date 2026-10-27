"""Amazon Echo に文章を読み上げさせる小さな API サーバー。

Alexa の Web API は alexa_client.py で直接呼ぶ（alexa-remote-control の移植）。

GET  /health            死活確認（認証なし）
GET  /devices           端末一覧
POST /speak             {"text": "...", "device": "端末名（省略時は管理ページで決めた既定の端末）", "chime": true}
GET  /admin             API の ID を管理するページ（管理者の Basic 認証）

/devices と /speak は、管理ページで発行した ID とパスワードの Basic 認証が必要。
ID は clients.json にパスワードのハッシュで保存する。管理者は ALEXA_SPEAK_ADMIN に
"id:password" の形で書く（空なら管理ページは使えない）。
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from alexa_auth import AuthError, LoginResult, PendingLogin, finish_login, start_login
from alexa_client import AlexaClient, AlexaError

HOST = os.environ.get("ALEXA_SPEAK_HOST", "127.0.0.1")
PORT = int(os.environ.get("ALEXA_SPEAK_PORT", "8003"))
SETTINGS_FILE = Path(os.environ.get("ALEXA_SPEAK_SETTINGS_FILE", Path(__file__).with_name("settings.json")))
# 読み上げの前に鳴らす音（Alexa の効果音ライブラリの soundbank:// URL）。空ならチャイムなし
CHIME = os.environ.get(
    "ALEXA_SPEAK_CHIME", "soundbank://soundlibrary/home/amzn_sfx_doorbell_chime_01"
).strip()
CLIENTS_FILE = Path(os.environ.get("ALEXA_SPEAK_CLIENTS_FILE", Path(__file__).with_name("clients.json")))
TOKEN_FILE = Path(os.environ.get("ALEXA_SPEAK_TOKEN_FILE", Path(__file__).with_name("alexa_token.json")))
# ログイン用 URL を出してから貼り付けを受け付けるまでの時間
LOGIN_PENDING_TTL = 30 * 60
ADMIN_PAGE = Path(__file__).with_name("admin.html")
MAX_TEXT = 300
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

# 認証の失敗は 15 分で接続元ごとに 5 回、全体で 30 回まで
FAIL_WINDOW = 15 * 60
FAIL_LIMIT_PER_IP = 5
FAIL_LIMIT_TOTAL = 30
# 読み上げは ID ごとに 1 分 10 回まで
SPEAK_WINDOW = 60
SPEAK_LIMIT = 10


def _parse_pair(value: str) -> tuple[str, str] | None:
    cid, sep, password = value.strip().partition(":")
    return (cid, password) if cid and sep and password else None


ADMIN = _parse_pair(os.environ.get("ALEXA_SPEAK_ADMIN", ""))

class TokenStore:
    """refresh_token を alexa_token.json で持つ。無ければ環境変数 REFRESH_TOKEN を使う。

    管理ページからログインし直したときに、サービス自身が書き換えられるようにするため、
    /etc/default ではなくリポジトリ直下（Git 管理外、権限 600）に置く。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            token = os.environ.get("REFRESH_TOKEN", "").strip()
            self.data = {"refresh_token": token, "source": "env" if token else "", "updated": None,
                         "account_pool": "", "customer_name": ""}

    @property
    def token(self) -> str:
        return self.data.get("refresh_token", "")

    def save(self, result: LoginResult) -> None:
        self.data = {"refresh_token": result.refresh_token, "source": "admin", "updated": _now(),
                     "account_pool": result.account_pool, "customer_name": result.customer_name}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def info(self) -> dict:
        return {k: v for k, v in self.data.items() if k != "refresh_token"} | {"has_token": bool(self.token)}


class SettingsStore:
    """管理ページで変える読み上げの設定を settings.json で持つ。

    default_device: device を省いたときの端末名（無ければ環境変数 ALEXA_DEFAULT_DEVICE）
    speak_volume:   読み上げるときだけ使う音量 1〜100。0 なら音量を変えない
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data = {"default_device": os.environ.get("ALEXA_DEFAULT_DEVICE", ""), "speak_volume": 0}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    @property
    def default_device(self) -> str:
        return self.data["default_device"]

    @property
    def speak_volume(self) -> int:
        return self.data["speak_volume"]

    def save(self, default_device: str, speak_volume: int) -> None:
        with self.lock:
            self.data = {"default_device": default_device, "speak_volume": speak_volume, "updated": _now()}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)


def _new_alexa_client(token: str) -> AlexaClient:
    return AlexaClient(
        refresh_token=token,
        amazon=os.environ.get("AMAZON", "amazon.co.jp"),
        alexa=os.environ.get("ALEXA", "alexa.amazon.co.jp"),
        locale=os.environ.get("TTS_LOCALE", "ja-JP"),
    )

# 失敗・読み上げの記録はメモリ上だけ（再起動で消える）
_rate_lock = threading.Lock()
_fails: dict[str, deque] = defaultdict(deque)
_speaks: dict[str, deque] = defaultdict(deque)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hash(password: str, salt: str) -> str:
    # パスワードはこちらで発行する長い乱数なので、ソルト付き SHA-256 で足りる
    return hashlib.sha256((salt + password).encode()).hexdigest()


class ClientStore:
    """API の ID とパスワード（ハッシュ）を clients.json で持つ。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        if path.exists():
            self.clients = json.loads(path.read_text(encoding="utf-8"))["clients"]
        else:
            # 初回だけ、以前の ALEXA_SPEAK_CLIENTS（id:password のカンマ区切り）を取り込む
            self.clients = {}
            for item in os.environ.get("ALEXA_SPEAK_CLIENTS", "").split(","):
                pair = _parse_pair(item)
                if pair and CLIENT_ID_RE.match(pair[0]):
                    self._set_password(pair[0], pair[1])
            self._save()

    def _set_password(self, cid: str, password: str) -> None:
        salt = secrets.token_hex(16)
        entry = self.clients.setdefault(cid, {"created": _now(), "last_used": None})
        entry["salt"] = salt
        entry["hash"] = _hash(password, salt)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"clients": self.clients}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def verify(self, cid: str, password: str) -> bool:
        with self.lock:
            entry = self.clients.get(cid)
            if not entry or not hmac.compare_digest(_hash(password, entry["salt"]), entry["hash"]):
                return False
            entry["last_used"] = _now()
            self._save()
            return True

    def list(self) -> list[dict]:
        with self.lock:
            return [
                {"id": cid, "created": e["created"], "last_used": e["last_used"]}
                for cid, e in sorted(self.clients.items())
            ]

    def issue(self, cid: str, new: bool) -> str | None:
        """new=True は追加（既にあれば None）、False は再発行（無ければ None）。"""
        with self.lock:
            if (cid in self.clients) == new:
                return None
            password = secrets.token_hex(24)
            self._set_password(cid, password)
            self._save()
            return password

    def delete(self, cid: str) -> bool:
        with self.lock:
            if self.clients.pop(cid, None) is None:
                return False
            self._save()
            return True


STORE = ClientStore(CLIENTS_FILE)
TOKENS = TokenStore(TOKEN_FILE)
SETTINGS = SettingsStore(SETTINGS_FILE)
ALEXA = _new_alexa_client(TOKENS.token)

# 管理ページからの Amazon ログインの途中経過（同時に進めるのは 1 件だけ）
_login_lock = threading.Lock()
_pending_login: tuple[PendingLogin, float] | None = None
_last_alexa_error = ""


def _record_alexa_error(e: Exception) -> None:
    """管理ページに「最後に失敗した理由」として出す。"""
    global _last_alexa_error
    _last_alexa_error = f"{_now()} {e}"


def _prune(q: deque, window: int, now: float) -> None:
    while q and q[0] <= now - window:
        q.popleft()


def to_speech(text: str, chime: bool) -> str:
    """チャイムを付けるときは SSML にして、文章の前に効果音を入れる。"""
    if not (chime and CHIME):
        return text
    return f'<speak><audio src="{html.escape(CHIME)}"/>{html.escape(text, quote=False)}</speak>'


class Handler(BaseHTTPRequestHandler):
    client_id = "-"

    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _remote_ip(self) -> str:
        # Apache 経由のときは、Apache が末尾に足した接続元を使う
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[-1].strip()
        return self.client_address[0]

    def _authenticate(self, admin: bool = False) -> bool:
        """認証に失敗したときは応答まで送って False を返す。"""
        if admin and ADMIN is None:
            self._send(404, {"ok": False, "error": "not found"})
            return False
        ip = self._remote_ip()
        now = time.time()
        with _rate_lock:
            _prune(_fails[ip], FAIL_WINDOW, now)
            _prune(_fails["*"], FAIL_WINDOW, now)
            if len(_fails[ip]) >= FAIL_LIMIT_PER_IP or len(_fails["*"]) >= FAIL_LIMIT_TOTAL:
                self._send(429, {"ok": False, "error": "too many failed attempts"})
                return False

        cid, password = "", ""
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                cid, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                pass
        if admin:
            ok = hmac.compare_digest(cid.encode(), ADMIN[0].encode()) and hmac.compare_digest(
                password.encode(), ADMIN[1].encode()
            )
        else:
            ok = STORE.verify(cid, password)
        if ok:
            self.client_id = f"admin:{cid}" if admin else cid
            return True

        if header:
            with _rate_lock:
                _fails[ip].append(now)
                _fails["*"].append(now)
        realm = "alexa_speak admin" if admin else "alexa_speak"
        self._send(401, {"ok": False, "error": "unauthorized"},
                   {"WWW-Authenticate": f'Basic realm="{realm}"'})
        return False

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            body = None
        if not isinstance(body, dict):
            self._send(400, {"ok": False, "error": "invalid json"})
            return None
        return body

    # ---- 管理ページ ----

    def _admin(self, method: str, path: str) -> None:
        if not self._authenticate(admin=True):
            return
        if path == "/admin" and method == "GET":
            data = ADMIN_PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/admin/api/clients" and method == "GET":
            self._send(200, {"ok": True, "clients": STORE.list()})
            return

        if path == "/admin/api/alexa" and method == "GET":
            self._send(200, {"ok": True, **TOKENS.info(), "last_error": _last_alexa_error,
                             "login_pending": _pending_login is not None})
            return

        # 変更の操作は、ほかのサイトのフォームから送れないよう専用ヘッダーを必須にする
        if self.headers.get("X-Alexa-Speak-Admin") != "1":
            self._send(403, {"ok": False, "error": "missing X-Alexa-Speak-Admin header"})
            return
        if path.startswith("/admin/api/alexa/") and method == "POST":
            self._admin_alexa(path)
            return
        if path in ("/admin/api/settings", "/admin/api/devices", "/admin/api/speak-test"):
            self._admin_settings(method, path)
            return
        m = re.fullmatch(r"/admin/api/clients(?:/([^/]+)(/reset)?)?", path)
        if not m:
            self._send(404, {"ok": False, "error": "not found"})
            return
        cid, reset = (unquote(m.group(1)) if m.group(1) else None), bool(m.group(2))

        if method == "POST" and cid is None:
            body = self._read_json()
            if body is None:
                return
            cid = str(body.get("id", "")).strip()
            if not CLIENT_ID_RE.match(cid):
                self._send(400, {"ok": False, "error": "ID は英数字と _ . - で 32 文字まで"})
                return
            password = STORE.issue(cid, new=True)
            if password is None:
                self._send(409, {"ok": False, "error": "その ID は既にある"})
                return
            print(f"admin {self.client_id} created client {cid}", flush=True)
            self._send(200, {"ok": True, "id": cid, "password": password})
        elif method == "POST" and cid and reset:
            password = STORE.issue(cid, new=False)
            if password is None:
                self._send(404, {"ok": False, "error": "その ID は無い"})
                return
            print(f"admin {self.client_id} reset client {cid}", flush=True)
            self._send(200, {"ok": True, "id": cid, "password": password})
        elif method == "DELETE" and cid and not reset:
            if not STORE.delete(cid):
                self._send(404, {"ok": False, "error": "その ID は無い"})
                return
            print(f"admin {self.client_id} deleted client {cid}", flush=True)
            self._send(200, {"ok": True})
        else:
            self._send(405, {"ok": False, "error": "method not allowed"})

    def _admin_settings(self, method: str, path: str) -> None:
        if path == "/admin/api/settings" and method == "GET":
            self._send(200, {"ok": True, "default_device": SETTINGS.default_device,
                             "speak_volume": SETTINGS.speak_volume})
        elif path == "/admin/api/devices" and method == "GET":
            try:
                devices = ALEXA.list_devices()
            except AlexaError as e:
                _record_alexa_error(e)
                self._send(502, {"ok": False, "error": str(e)})
                return
            self._send(200, {"ok": True, "devices": devices})
        elif path == "/admin/api/settings" and method == "POST":
            body = self._read_json()
            if body is None:
                return
            device = str(body.get("default_device", "")).strip()
            volume = body.get("speak_volume", 0)
            if not device:
                self._send(400, {"ok": False, "error": "既定の端末を選ぶ"})
                return
            if not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= 100:
                self._send(400, {"ok": False, "error": "音量は 0〜100 の整数（0 は変えない）"})
                return
            SETTINGS.save(device, volume)
            print(f"admin {self.client_id} saved settings device={device} volume={volume}", flush=True)
            self._send(200, {"ok": True, "default_device": device, "speak_volume": volume})
        elif path == "/admin/api/speak-test" and method == "POST":
            # 今の設定（既定の端末・音量・チャイム）で読み上げる
            try:
                ALEXA.speak(SETTINGS.default_device,
                            to_speech("アレクサ スピークのテストです。この音量で読み上げます。", True),
                            SETTINGS.speak_volume)
            except AlexaError as e:
                _record_alexa_error(e)
                self._send(502, {"ok": False, "error": str(e)})
                return
            print(f"admin {self.client_id} speak test device={SETTINGS.default_device}", flush=True)
            self._send(200, {"ok": True})
        else:
            self._send(405, {"ok": False, "error": "method not allowed"})

    def _admin_alexa(self, path: str) -> None:
        global _pending_login, _last_alexa_error
        if path == "/admin/api/alexa/check":
            try:
                devices = ALEXA.check()
            except AlexaError as e:
                _record_alexa_error(e)
                self._send(502, {"ok": False, "error": str(e)})
                return
            _last_alexa_error = ""
            self._send(200, {"ok": True, "devices": [d["name"] for d in devices]})
        elif path == "/admin/api/alexa/login/start":
            pending = start_login()
            with _login_lock:
                _pending_login = (pending, time.time())
            print(f"admin {self.client_id} started amazon login", flush=True)
            self._send(200, {"ok": True, "url": pending.url})
        elif path == "/admin/api/alexa/login/finish":
            body = self._read_json()
            if body is None:
                return
            with _login_lock:
                current = _pending_login
            if current is None or time.time() - current[1] > LOGIN_PENDING_TTL:
                self._send(409, {"ok": False, "error": "ログインを開始していないか、時間が経ちすぎた。「Amazon にログイン」からやり直す"})
                return
            try:
                result = finish_login(current[0], str(body.get("url", "")))
            except AuthError as e:
                self._send(400, {"ok": False, "error": str(e)})
                return
            # 保存する前に、新しいトークンで実際にログインと端末一覧の取得ができるか確かめる
            trial = _new_alexa_client(result.refresh_token)
            try:
                devices = trial.check()
            except AlexaError as e:
                self._send(502, {"ok": False, "error": f"取得したトークンでログインできなかった"
                                 f"（アカウントの国: {result.account_pool or '不明'}）: {e}"})
                return
            TOKENS.save(result)
            ALEXA.set_refresh_token(result.refresh_token)
            with _login_lock:
                _pending_login = None
            _last_alexa_error = ""
            print(f"admin {self.client_id} updated amazon token ({result.account_pool})", flush=True)
            self._send(200, {"ok": True, "account_pool": result.account_pool,
                             "customer_name": result.customer_name, "devices": [d["name"] for d in devices]})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    # ---- API ----

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, {"ok": True})
            return
        if path == "/admin" or path.startswith("/admin/"):
            self._admin("GET", path)
            return
        if path != "/devices":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._authenticate():
            return
        try:
            devices = ALEXA.list_devices()
        except AlexaError as e:
            _record_alexa_error(e)
            self._send(502, {"ok": False, "error": str(e)})
            return
        self._send(200, {"ok": True, "devices": devices})

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/admin/"):
            self._admin("DELETE", path)
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/admin/"):
            self._admin("POST", path)
            return
        if path != "/speak":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._authenticate():
            return
        body = self._read_json()
        if body is None:
            return

        text = " ".join(str(body.get("text", "")).split())
        device = str(body.get("device") or SETTINGS.default_device)
        chime = body.get("chime", True)
        if not isinstance(chime, bool):
            self._send(400, {"ok": False, "error": "chime must be true or false"})
            return
        if not text:
            self._send(400, {"ok": False, "error": "text is required"})
            return
        if not device:
            self._send(400, {"ok": False, "error": "device is required"})
            return
        if len(text) > MAX_TEXT:
            self._send(400, {"ok": False, "error": f"text is too long (max {MAX_TEXT})"})
            return

        now = time.time()
        with _rate_lock:
            q = _speaks[self.client_id]
            _prune(q, SPEAK_WINDOW, now)
            if len(q) >= SPEAK_LIMIT:
                self._send(429, {"ok": False, "error": f"too many requests (max {SPEAK_LIMIT}/min)"})
                return
            q.append(now)

        print(f"speak client={self.client_id} device={device} chime={chime} text={text}", flush=True)
        started = time.time()
        try:
            ALEXA.speak(device, to_speech(text, chime), SETTINGS.speak_volume)
        except AlexaError as e:
            _record_alexa_error(e)
            print(f"speak failed client={self.client_id}: {e}", flush=True)
            self._send(502, {"ok": False, "error": str(e)})
            return
        print(f"speak done client={self.client_id} {time.time() - started:.2f}s", flush=True)
        self._send(200, {"ok": True})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self._remote_ip()} {self.client_id} {fmt % args}", flush=True)


if __name__ == "__main__":
    if ADMIN is None:
        print("ALEXA_SPEAK_ADMIN が空なので、管理ページは使えない", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
