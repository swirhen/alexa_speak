"""alexa-remote-control を HTTP 経由で呼ぶための小さな API サーバー。

GET  /health            死活確認
GET  /devices           端末一覧（alexa_remote_control.sh -a）
POST /speak             {"text": "...", "device": "端末名（省略時は ALEXA_DEFAULT_DEVICE）"}
"""

import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = os.environ.get("ALEXA_SCRIPT", "/home/swirhen/alexa-remote-control/alexa_remote_control.sh")
HOST = os.environ.get("ALEXA_SPEAK_HOST", "127.0.0.1")
PORT = int(os.environ.get("ALEXA_SPEAK_PORT", "8003"))
DEFAULT_DEVICE = os.environ.get("ALEXA_DEFAULT_DEVICE", "")
MAX_TEXT = 300
TIMEOUT = 90

# スクリプトは cookie・端末一覧を共有ファイルに書くので、同時に実行しない
_lock = threading.Lock()


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
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True})
        elif self.path == "/devices":
            r = run_script("-a")
            if r.returncode != 0:
                self._send(502, {"ok": False, "stdout": r.stdout, "stderr": r.stderr})
                return
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            self._send(200, {"ok": True, "output": lines})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/speak":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
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
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
