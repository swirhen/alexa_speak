"""alexa_speak の /speak を呼ぶ MCP サーバー（Streamable HTTP）。

複数の AI（claude.ai のカスタムコネクタなど）から接続する共通の MCP サーバーで、公開するツールは speak だけ。

コネクタは alexa_speak の管理ページで AI ごとに発行する（clients.json の kind が "mcp" の ID）。
コネクタの URL は /mcp/<ID>/<パスワード> で、その ID とパスワードをそのまま /speak の
Basic 認証に使う。そのため、この MCP サーバー自身は秘密情報を持たない。

カスタムコネクタなどは Basic 認証のヘッダーを付けられないので、URL に推測できない
パスワードを含めて守る。知らない URL は 404 になる（alexa_speak の /whoami で確かめる）。

環境変数
  ALEXA_SPEAK_BASE_URL   alexa_speak の URL（省略時はサーバー内の http://127.0.0.1:8003）
  ALEXA_SPEAK_MCP_PORT   待ち受けポート（省略時は 8004）
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Annotated

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

BASE_URL = os.environ.get("ALEXA_SPEAK_BASE_URL", "http://127.0.0.1:8003").rstrip("/")
PORT = int(os.environ.get("ALEXA_SPEAK_MCP_PORT", "8004"))
MAX_TEXT = 300
TIMEOUT = 120
# コネクタの URL を確かめた結果を覚えておく秒数（削除・作り直しはこの時間で反映される）
VERIFY_CACHE = 60

CONNECTOR_PATH = re.compile(r"^/mcp/([A-Za-z0-9_.-]{1,32})/([0-9a-f]{64})/?$")
# 入口で付け直すヘッダー（外から来た同じ名前のものは捨てる）
HEADER_PREFIX = b"x-alexa-speak-"

server = MCPServer(
    name="alexa-speak",
    instructions=(
        "持ち主の家の Amazon Echo に文章を読み上げさせる。実際に家の中で声が出るので、"
        "ユーザーに頼まれたときだけ使う。"
    ),
)


def _request(path: str, cid: str, secret: str, forwarded_for: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()}
    if forwarded_for:
        # alexa_speak が、認証の失敗の回数制限を元の接続元ごとに数えられるようにする
        headers["X-Forwarded-For"] = forwarded_for
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


# ---- コネクタの URL の確認 ----

_verified: dict[str, tuple[int, float]] = {}


async def _verify(cid: str, secret: str, forwarded_for: str) -> int:
    """コネクタとして使える URL なら 200。そうでなければ返す HTTP ステータス。"""
    key = hashlib.sha256(f"{cid}:{secret}".encode()).hexdigest()
    cached = _verified.get(key)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        status, data = await asyncio.to_thread(_request, "/whoami", cid, secret, forwarded_for)
    except (urllib.error.URLError, TimeoutError):
        return 503
    if status == 200 and data.get("kind") == "mcp":
        result = 200
    elif status == 429:
        result = 429
    else:
        result = 404
    if result != 429:
        _verified[key] = (result, time.time() + VERIFY_CACHE)
    return result


async def _plain(send, status: int, text: str) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def connector_app(inner):
    """/mcp/<ID>/<パスワード> を確かめてから、/mcp として MCP 本体に渡す ASGI アプリ。"""

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)  # lifespan など
            return
        m = CONNECTOR_PATH.match(scope["path"])
        if not m:
            print(f"- {scope['method']} 404 (URL の形が違う)", flush=True)
            await _plain(send, 404, "not found")
            return
        cid, secret = m.groups()
        headers = [(k, v) for k, v in scope["headers"] if not k.startswith(HEADER_PREFIX)]
        forwarded_for = next((v.decode() for k, v in headers if k == b"x-forwarded-for"), "")
        status = await _verify(cid, secret, forwarded_for)
        # URL には乱数が入っているので、uvicorn のアクセスログは止めて、ここで名前だけ記録する
        print(f"{forwarded_for or '-'} connector={cid} {scope['method']} {status}", flush=True)
        if status != 200:
            await _plain(send, status, {404: "not found", 429: "too many failed attempts"}.get(status, "unavailable"))
            return
        headers += [(b"x-alexa-speak-connector", cid.encode()), (b"x-alexa-speak-secret", secret.encode())]
        await inner(dict(scope, path="/mcp", raw_path=b"/mcp", headers=headers), receive, send)

    return app


# ---- ツール ----

# 応答コードごとに、呼び出し元の AI が次にどうすべきかを返す
_ERRORS = {
    400: "文章が不正（空・長すぎるなど）。直して送り直してよい",
    401: "読み上げ API の認証に失敗した（コネクタが削除されたか、URL が作り直された）。送り直さず、持ち主に伝える",
    429: "回数制限に当たった（1 分 10 回まで）。繰り返さず、しばらく待つ",
    502: "Alexa 側で失敗した（ログイン情報の失効、端末名の間違いなど）。送り直さず、持ち主に伝える",
}


@server.tool(
    title="Echo で読み上げる",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    ),
)
async def speak(
    ctx: Context,
    text: Annotated[str, Field(description="読み上げる文章（日本語の話し言葉、300 文字まで）")],
    chime: Annotated[
        bool,
        Field(
            description="読み上げの前にチャイムを鳴らすか。続けて何通も送るときの 2 通目以降など、"
            "鳴らさないほうが自然なときだけ false にする"
        ),
    ] = True,
) -> str:
    """持ち主の家の Amazon Echo に、文章を声に出して読み上げさせる。

    実際に家の中で声が出るので、ユーザーに頼まれたとき・決められた用途のときだけ使う。
    動作確認のための連続呼び出しや、同じ内容の繰り返しはしない。

    文章は画面ではなく耳で聞くもの。日本語の話し言葉で短い文に区切り、全体で 1〜3 文、
    長くても 300 文字まで。記号・URL・絵文字・Markdown・括弧書き・箇条書きは使わない。
    時刻は「15時30分」、日付は「10月25日」のように書く。読み間違えやすい固有名詞や略語は
    ひらがな・カタカナにする。
    """
    # 入口（connector_app）で付け直したヘッダーなので、外から偽ることはできない
    headers = ctx.headers or {}
    cid, secret = headers.get("x-alexa-speak-connector", ""), headers.get("x-alexa-speak-secret", "")
    if not cid or not secret:
        raise ToolError("コネクタの情報が無い。持ち主に MCP サーバーの設定を確認してもらう")
    text = text.strip()
    if not text:
        raise ToolError("文章が空")
    if len(text) > MAX_TEXT:
        raise ToolError(f"文章が長すぎる（{len(text)} 文字。{MAX_TEXT} 文字まで）。短くして送り直してよい")
    try:
        status, data = await asyncio.to_thread(
            _request, "/speak", cid, secret, headers.get("x-forwarded-for", ""), {"text": text, "chime": chime}
        )
    except (urllib.error.URLError, TimeoutError) as e:
        raise ToolError(f"読み上げ API に接続できなかった（{e}）。送り直さず、持ち主に伝える") from e
    if status == 200 and data.get("ok"):
        return "読み上げを依頼した。数秒〜十数秒で Echo から聞こえる"
    hint = _ERRORS.get(status, "予期しない応答。送り直さず、持ち主に伝える")
    detail = data.get("error") or ""
    raise ToolError(f"HTTP {status}{'（' + detail + '）' if detail else ''}: {hint}")


if __name__ == "__main__":
    inner = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host="127.0.0.1"
    )
    uvicorn.run(connector_app(inner), host="127.0.0.1", port=PORT, access_log=False)
