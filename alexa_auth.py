"""Amazon にログインして refresh_token を取る（alexa-cookie2 の登録処理の移植）。

alexa-cookie-cli はログイン画面をプロキシで中継して認可コードを抜き取るが、ここでは
プロキシを使わない。ログイン用の URL を普通のブラウザで開いてもらい、ログイン後に
表示される「ページが見つかりません」の URL（/ap/maplanding?...authorization_code=...）を
貼り付けてもらう。

  1. start_login(): 端末 ID と PKCE を作り、ログイン用の URL を返す
  2. finish_login(): 貼り付けられた URL から認可コードを取り出し、
     POST https://api.amazon.com/auth/register で端末を登録して refresh_token を受け取る

日本のアカウントでも、ログインと登録は amazon.com で行う（amazon.co.jp で行うと
認可コードが付かない）。動かなくなったら alexa-cookie2（https://github.com/Apollon77/alexa-cookie）
の lib/proxy.js（ログイン URL）と alexa-cookie.js の handleTokenRegistration を見比べる。
"""

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

BASE = "amazon.com"
APP_VERSION = "2.2.651540.0"
USER_AGENT = f"AmazonWebView/Amazon Alexa/{APP_VERSION}/iOS/18.3.1/iPhone"
DEVICE_TYPE = "A2IVLV5VM2W81"
APP_NAME = "alexa_speak"
TIMEOUT = 30


class AuthError(Exception):
    pass


@dataclass
class PendingLogin:
    device_id: str
    code_verifier: str
    url: str


@dataclass
class LoginResult:
    refresh_token: str
    account_pool: str  # 例: AmazonJP
    customer_name: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start_login(language: str = "ja_JP") -> PendingLogin:
    # 端末 ID は「16 バイトの乱数の大文字 16 進表記 + "#" + 端末種別」をさらに 16 進にしたもの
    serial = secrets.token_hex(16).upper()
    device_id = f"{serial}#{DEVICE_TYPE}".encode().hex()
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    params = {
        "openid.return_to": f"https://www.{BASE}/ap/maplanding",
        "openid.assoc_handle": "amzn_dp_project_dee_ios",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "pageId": "amzn_dp_project_dee_ios",
        "accountStatusPolicy": "P1",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.mode": "checkid_setup",
        "openid.ns.oa2": f"http://www.{BASE}/ap/ext/oauth/2",
        "openid.oa2.client_id": f"device:{device_id}",
        "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
        "openid.oa2.response_type": "code",
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.pape.max_auth_age": "0",
        "openid.oa2.scope": "device_auth_access",
        "openid.oa2.code_challenge_method": "S256",
        "openid.oa2.code_challenge": challenge,
        "language": language,
    }
    url = f"https://www.{BASE}/ap/signin?" + urllib.parse.urlencode(params)
    return PendingLogin(device_id=device_id, code_verifier=verifier, url=url)


def extract_code(pasted: str) -> str:
    query = urllib.parse.urlsplit(pasted.strip()).query
    code = urllib.parse.parse_qs(query).get("openid.oa2.authorization_code", [""])[0]
    if not code:
        raise AuthError(
            "URL に認可コード（openid.oa2.authorization_code）が無い。"
            "ログインが終わったあとに表示されるページの URL をそのまま貼り付ける"
        )
    return code


def finish_login(pending: PendingLogin, pasted_url: str) -> LoginResult:
    code = extract_code(pasted_url)
    # frc は alexa-cookie2 でも乱数で作っている値（313 バイトを base64）
    frc = base64.b64encode(secrets.token_bytes(313)).decode()
    body = {
        "requested_extensions": ["device_info", "customer_info"],
        "cookies": {"website_cookies": [], "domain": f".{BASE}"},
        "registration_data": {
            "domain": "Device",
            "app_version": APP_VERSION,
            "device_type": DEVICE_TYPE,
            "device_name": f"%FIRST_NAME%'s%DUPE_STRATEGY_1ST%{APP_NAME}",
            "os_version": "18.3.1",
            "device_serial": secrets.token_hex(16),
            "device_model": "iPhone",
            "app_name": APP_NAME,
            "software_version": "1",
        },
        "auth_data": {
            "client_id": pending.device_id,
            "authorization_code": code,
            "code_verifier": pending.code_verifier,
            "code_algorithm": "SHA-256",
            "client_domain": "DeviceLegacy",
        },
        "user_context_map": {"frc": frc},
        "requested_token_type": ["bearer", "mac_dms", "website_cookies"],
    }
    req = urllib.request.Request(
        f"https://api.{BASE}/auth/register",
        data=json.dumps(body).encode(),
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja-JP",
            "Accept-Charset": "utf-8",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-amzn-identity-auth-domain": f"api.{BASE}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            status, raw = res.status, res.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise AuthError(f"Amazon に接続できなかった: {e}") from e

    try:
        success = json.loads(raw)["response"]["success"]
        token = success["tokens"]["bearer"]["refresh_token"]
    except (ValueError, KeyError, TypeError):
        detail = raw.decode("utf-8", "replace")[:300]
        raise AuthError(
            f"端末の登録に失敗した（HTTP {status}）。認可コードは数分で使えなくなるので、"
            f"ログインからやり直す: {detail}"
        )
    customer = success.get("extensions", {}).get("customer_info", {})
    return LoginResult(
        refresh_token=token,
        account_pool=customer.get("account_pool", ""),
        customer_name=customer.get("given_name") or customer.get("name", ""),
    )
