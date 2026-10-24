"""alexa-remote-control を HTTP 経由で呼ぶための小さな API サーバー。

GET  /health            死活確認（認証なし）
GET  /devices           端末一覧（alexa_remote_control.sh -a）
POST /speak             {"text": "...", "device": "端末名（省略時は ALEXA_DEFAULT_DEVICE）"}

/health 以外は Basic 認証が必要。ID とパスワードは ALEXA_SPEAK_CLIENTS に
"id1:password1,id2:password2" の形で書く（呼び出し元の AI ごとに分ける）。
"""

import base64
import hmac
import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

SCRIPT = os.environ.get("ALEXA_SCRIPT", "/home/swirhen/alexa-remote-control/alexa_remote_control.sh")
HOST = os.environ.get("ALEXA_SPEAK_HOST", "127.0.0.1")
PORT = int(os.environ.get("ALEXA_SPEAK_PORT", "8003"))
DEFAULT_DEVICE = os.environ.get("ALEXA_DEFAULT_DEVICE", "")
MAX_TEXT = 300
TIMEOUT = 90

# 認証の失敗は 15 分で接続元ごとに 5 回、全体で 30 回まで
FAIL_WINDOW = 15 * 60
FAIL_LIMIT_PER_IP = 5
FAIL_LIMIT_TOTAL = 30
# 読み上げは ID ごとに 1 分 10 回まで
SPEAK_WINDOW = 60
SPEAK_LIMIT = 10


def load_clients() -> dict[str, str]:
    clients = {}
    for item in os.environ.get("ALEXA_SPEAK_CLIENTS", "").split(","):
        cid, sep, password = item.strip().partition(":")
        if cid and sep and password:
            clients[cid] = password
    return clients


CLIENTS = load_clients()

# スクリプトは cookie・端末一覧を共有ファイルに書くので、同時に実行しない
_lock = threading.Lock()
# 失敗・読み上げの記録はメモリ上だけ（再起動で消える）
_rate_lock = threading.Lock()
_fails: dict[str, deque] = defaultdict(deque)
_speaks: dict[str, deque] = defaultdict(deque)


def _prune(q: deque, window: int, now: float) -> None:
    while q and q[0] <= now - window:
        q.popleft()


def run_script(*args: str) -> subprocess.CompletedProcess:
    with _lock:
        return subprocess.run(
            [SCRIPT, *args], capture_output=True, text=True, timeout=TIMEOUT
        )


def sanitize(text: str) -> str:
    # スクリプト内で引用符なしの echo を通るため、glob として展開される文字を除く
    text = re.sub(r"[*?\[\]]", "", text)
    return " ".join(text.split())


class Handler(BaseHTTPRequestHandler):
    client_id = "-"

    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
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

    def _authenticate(self) -> bool:
        """認証に失敗したときは応答まで送って False を返す。"""
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
        expected = CLIENTS.get(cid)
        if expected is not None and hmac.compare_digest(password.encode(), expected.encode()):
            self.client_id = cid
            return True

        if header:
            with _rate_lock:
                _fails[ip].append(now)
                _fails["*"].append(now)
        self._send(401, {"ok": False, "error": "unauthorized"},
                   {"WWW-Authenticate": 'Basic realm="alexa_speak"'})
        return False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, {"ok": True})
            return
        if path != "/devices":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._authenticate():
            return
        r = run_script("-a")
        if r.returncode != 0:
            self._send(502, {"ok": False, "stdout": r.stdout, "stderr": r.stderr})
            return
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        self._send(200, {"ok": True, "output": lines})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/speak":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._authenticate():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(body, dict):
            self._send(400, {"ok": False, "error": "invalid json"})
            return

        text = sanitize(str(body.get("text", "")))
        device = str(body.get("device") or DEFAULT_DEVICE)
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

        print(f"speak client={self.client_id} device={device} text={text}", flush=True)
        try:
            r = run_script("-d", device, "-e", f"speak:{text}")
        except subprocess.TimeoutExpired:
            self._send(504, {"ok": False, "error": "timeout"})
            return
        # スクリプトは失敗しても終了コード 0 を返すことがあるので出力も返す
        ok = r.returncode == 0 and not any(
            s in r.stdout for s in ("ERROR", "device login process")
        )
        self._send(200 if ok else 502, {"ok": ok, "stdout": r.stdout, "stderr": r.stderr})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self._remote_ip()} {self.client_id} {fmt % args}", flush=True)


if __name__ == "__main__":
    if not CLIENTS:
        print("ALEXA_SPEAK_CLIENTS が空なので、/health 以外はすべて 401 になる", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
