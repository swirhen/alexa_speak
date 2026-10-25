"""alexa_speak の /speak を呼ぶ MCP サーバー（Streamable HTTP）。

複数の AI（claude.ai のカスタムコネクタなど）から接続する共通の MCP サーバーで、公開するツールは speak だけ。
/speak はすべて共通の ID（ai-web）で呼ぶ。

環境変数
  ALEXA_SPEAK_ID / ALEXA_SPEAK_PASSWORD  /speak の Basic 認証（必須）
  ALEXA_SPEAK_MCP_PATH_TOKEN             URL に含める乱数（必須）。
                                         エンドポイントは /mcp/<トークン> になる
  ALEXA_SPEAK_URL                        /speak の URL（省略時はサーバー内の API）
  ALEXA_SPEAK_MCP_PORT                   待ち受けポート（省略時は 8004）

カスタムコネクタなどは Basic 認証のヘッダーを付けられないので、コネクタの URL に
推測できないトークンを含めて守る。トークンを知らない接続は 404 になる。
"""

import asyncio
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

SPEAK_URL = os.environ.get("ALEXA_SPEAK_URL", "http://127.0.0.1:8003/speak")
PORT = int(os.environ.get("ALEXA_SPEAK_MCP_PORT", "8004"))
MAX_TEXT = 300
TIMEOUT = 120


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"環境変数 {name} が空")
    return value


CLIENT_ID = _require("ALEXA_SPEAK_ID")
PASSWORD = _require("ALEXA_SPEAK_PASSWORD")
PATH_TOKEN = _require("ALEXA_SPEAK_MCP_PATH_TOKEN")
if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", PATH_TOKEN):
    sys.exit("ALEXA_SPEAK_MCP_PATH_TOKEN は英数字と _ - で 32 文字以上にする")

_AUTH = "Basic " + base64.b64encode(f"{CLIENT_ID}:{PASSWORD}".encode()).decode()

server = MCPServer(
    name="alexa-speak",
    instructions=(
        "持ち主の家の Amazon Echo に文章を読み上げさせる。実際に家の中で声が出るので、"
        "ユーザーに頼まれたときだけ使う。"
    ),
)


def _post(text: str, chime: bool) -> tuple[int, dict]:
    body = json.dumps({"text": text, "chime": chime}).encode("utf-8")
    req = urllib.request.Request(
        SPEAK_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": _AUTH},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


# 応答コードごとに、呼び出し元の AI が次にどうすべきかを返す
_ERRORS = {
    400: "文章が不正（空・長すぎるなど）。直して送り直してよい",
    401: "読み上げ API の認証に失敗した。送り直さず、持ち主に MCP サーバーの設定を確認してもらう",
    429: "回数制限に当たった（1 分 10 回まで）。繰り返さず、しばらく待つ",
    502: "Alexa 側で失敗した（ログイン情報の失効など）。送り直さず、持ち主に伝える",
    504: "Alexa の応答がタイムアウトした。1 回だけ待ってから送り直してよい",
}


@server.tool(
    title="Echo で読み上げる",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    ),
)
async def speak(
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
    text = text.strip()
    if not text:
        raise ToolError("文章が空")
    if len(text) > MAX_TEXT:
        raise ToolError(f"文章が長すぎる（{len(text)} 文字。{MAX_TEXT} 文字まで）。短くして送り直してよい")
    try:
        status, data = await asyncio.to_thread(_post, text, chime)
    except (urllib.error.URLError, TimeoutError) as e:
        raise ToolError(f"読み上げ API に接続できなかった（{e}）。送り直さず、持ち主に伝える") from e
    if status == 200 and data.get("ok"):
        return "読み上げを依頼した。数秒〜十数秒で Echo から聞こえる"
    hint = _ERRORS.get(status, "予期しない応答。送り直さず、持ち主に伝える")
    detail = data.get("error") or ""
    raise ToolError(f"HTTP {status}{'（' + detail + '）' if detail else ''}: {hint}")


if __name__ == "__main__":
    server.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=PORT,
        streamable_http_path=f"/mcp/{PATH_TOKEN}",
        stateless_http=True,
        json_response=True,
    )
