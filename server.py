"""Amazon Echo に文章を読み上げさせる小さな API サーバー。

Alexa の Web API は alexa_client.py で直接呼ぶ（alexa-remote-control の移植）。

GET  /health            死活確認（認証なし）
GET  /devices           端末一覧
POST /speak             {"text": "...", "device": "端末名（省略時は ALEXA_DEFAULT_DEVICE）", "chime": true}
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
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from alexa_client import AlexaClient, AlexaError

HOST = os.environ.get("ALEXA_SPEAK_HOST", "127.0.0.1")
PORT = int(os.environ.get("ALEXA_SPEAK_PORT", "8003"))
DEFAULT_DEVICE = os.environ.get("ALEXA_DEFAULT_DEVICE", "")
# 読み上げの前に鳴らす音（Alexa の効果音ライブラリの soundbank:// URL）。空ならチャイムなし
CHIME = os.environ.get(
    "ALEXA_SPEAK_CHIME", "soundbank://soundlibrary/home/amzn_sfx_doorbell_chime_01"
).strip()
CLIENTS_FILE = Path(os.environ.get("ALEXA_SPEAK_CLIENTS_FILE", Path(__file__).with_name("clients.json")))
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

if not os.environ.get("REFRESH_TOKEN", "").strip():
    sys.exit("環境変数 REFRESH_TOKEN が空")
ALEXA = AlexaClient(
    refresh_token=os.environ["REFRESH_TOKEN"].strip(),
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

        # 変更の操作は、ほかのサイトのフォームから送れないよう専用ヘッダーを必須にする
        if self.headers.get("X-Alexa-Speak-Admin") != "1":
            self._send(403, {"ok": False, "error": "missing X-Alexa-Speak-Admin header"})
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
        device = str(body.get("device") or DEFAULT_DEVICE)
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
            ALEXA.speak(device, to_speech(text, chime))
        except AlexaError as e:
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
