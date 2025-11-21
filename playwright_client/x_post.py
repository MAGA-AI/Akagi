#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
x_post.py (Auth header fix)
- OAuth 2.0 Authorization Code + PKCE（ユーザーコンテキスト）
- 初回はブラウザで認可、ローカルHTTPサーバでcode受け取り→トークン交換
- トークン保存/自動リフレッシュ
- 引数のテキストを POST /2/tweets で投稿

必要環境変数:
  X_CLIENT_ID, X_REDIRECT_URI
任意:
  X_SCOPES, X_TOKEN_FILE
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Dict, Tuple

CLIENT_ID = os.environ.get("X_CLIENT_ID") or "d1FOWThFRHk0R0ZLUHJ0TVlsbUM6MTpjaQ"
REDIRECT_URI = os.environ.get("X_REDIRECT_URI") or "http://127.0.0.1:9876/callback"
SCOPES = os.environ.get("X_SCOPES") or "tweet.write tweet.read users.read offline.access"
TOKEN_FILE = os.environ.get("X_TOKEN_FILE") or "./x_tokens.json"

AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
TWEET_URL = "https://api.twitter.com/2/tweets"

# --- 画像アップロード & 画像付き投稿 ---

import requests

def upload_media(filepath: str) -> str | None:
    """
    v1.1 の upload endpoint を使って画像をアップロードし、media_id_string を返す。
    """
    token = ensure_access_token()
    url = "https://upload.twitter.com/1.1/media/upload.json"
    with open(filepath, "rb") as f:
        files = {"media": f}
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.post(url, headers=headers, files=files, timeout=30)
    if resp.ok:
        return resp.json().get("media_id_string")
    print("[upload_media] failed:", resp.status_code, resp.text[:300])
    return None

def post_tweet_with_img(text: str, image_paths: list[str]) -> tuple[int, dict]:
    """
    画像を複数添付してツイート（最大4枚）。
    """
    mids = []
    for p in image_paths[:4]:
        mid = upload_media(p)
        if mid:
            mids.append(mid)
    token = ensure_access_token()
    url = "https://api.twitter.com/2/tweets"
    payload = {"text": text}
    if mids:
        payload["media"] = {"media_ids": mids}
    resp = requests.post(url, headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"},
                         json=payload, timeout=30)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"error": resp.text[:300]}


def urlopen_json(req: urllib.request.Request) -> Tuple[int, Dict]:
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        try:
            return e.code, (json.loads(body) if body else {})
        except Exception:
            return e.code, {"error": body}
    except Exception as e:
        return 0, {"error": str(e)}

def b64url_no_pad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def now() -> int:
    return int(time.time())

def load_tokens() -> Dict:
    if not os.path.exists(TOKEN_FILE):
        return {}
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_tokens(tokens: Dict) -> None:
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)

@dataclass
class PKCE:
    verifier: str
    challenge: str
    state: str

def make_pkce() -> PKCE:
    verifier = b64url_no_pad(secrets.token_bytes(64))[:64]
    challenge = b64url_no_pad(hashlib.sha256(verifier.encode()).digest())
    state = b64url_no_pad(secrets.token_bytes(16))
    return PKCE(verifier, challenge, state)

class Handler(http.server.BaseHTTPRequestHandler):
    received: Dict[str, str] = {}
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = q.get("code", [None])[0]
        state = q.get("state", [""])[0]
        self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.end_headers()
        if code:
            Handler.received = {"code": code, "state": state}
            self.wfile.write(b"<h2>Authorization complete.</h2><p>You can close this tab.</p>")
        else:
            self.wfile.write(b"<h2>No code found.</h2>")
    def log_message(self, *a, **k): pass

def wait_code(port: int, timeout_sec: int = 300) -> Dict[str, str]:
    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    srv.socket.settimeout(1.0)
    start = now()
    try:
        while now() - start < timeout_sec:
            srv.handle_request()
            if Handler.received.get("code"):
                return Handler.received
        raise TimeoutError("Timed out waiting for authorization code.")
    finally:
        try: srv.server_close()
        except: pass

def build_auth_url(client_id: str, redirect_uri: str, scopes: str, code_challenge: str, state: str) -> str:
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes.split()),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(q)

def _auth_header_basic_with_client_id_only(client_id: str) -> str:
    # client_secretを発行していないPKCEアプリ向け: "client_id:" をBase64
    raw = (client_id + ":").encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")

def exchange_code_for_tokens(client_id: str, code: str, redirect_uri: str, code_verifier: str) -> Dict:
    payload = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _auth_header_basic_with_client_id_only(client_id),
    }
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(payload).encode(), headers=headers, method="POST")
    status, data = urlopen_json(req)
    if status == 200 and "access_token" in data:
        data["expires_at"] = now() + int(data.get("expires_in", 0))
    return data

def refresh_tokens(client_id: str, refresh_token: str) -> Dict:
    payload = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _auth_header_basic_with_client_id_only(client_id),
    }
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(payload).encode(), headers=headers, method="POST")
    status, data = urlopen_json(req)
    if status == 200 and "access_token" in data:
        data["expires_at"] = now() + int(data.get("expires_in", 0))
    return data

def ensure_access_token() -> str:
    if not CLIENT_ID:
        sys.exit("ERROR: X_CLIENT_ID が未設定です。")
    tokens = load_tokens()
    if not tokens.get("access_token"):
        pkce = make_pkce()
        auth_url = build_auth_url(CLIENT_ID, REDIRECT_URI, SCOPES, pkce.challenge, pkce.state)
        print("\nOpen this URL if browser doesn't open automatically:\n", auth_url, "\n")
        try: webbrowser.open(auth_url)
        except: pass
        port = urllib.parse.urlparse(REDIRECT_URI).port or 9876
        received = wait_code(port)
        if "code" not in received:
            sys.exit("ERROR: 認可コードの受け取りに失敗しました。")
        data = exchange_code_for_tokens(CLIENT_ID, received["code"], REDIRECT_URI, pkce.verifier)
        if "access_token" not in data:
            sys.exit(f"ERROR: トークン交換に失敗: {data}")
        save_tokens(data)
        return data["access_token"]

    if tokens.get("expires_at") and now() < int(tokens["expires_at"]) - 60:
        return tokens["access_token"]

    if not tokens.get("refresh_token"):
        os.remove(TOKEN_FILE)
        return ensure_access_token()

    new_tokens = refresh_tokens(CLIENT_ID, tokens["refresh_token"])
    if "access_token" in new_tokens:
        if "refresh_token" not in new_tokens and "refresh_token" in tokens:
            new_tokens["refresh_token"] = tokens["refresh_token"]
        save_tokens(new_tokens)
        return new_tokens["access_token"]

    # refresh失敗 → 再認可
    os.remove(TOKEN_FILE)
    return ensure_access_token()

def post_tweet(text: str) -> Tuple[int, Dict]:
    token = ensure_access_token()
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        TWEET_URL, data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    return urlopen_json(req)

def main():
    if len(sys.argv) < 2:
        print('使い方: python3 x_post.py "投稿テキスト"'); sys.exit(1)
    status, data = post_tweet(sys.argv[1])
    print("HTTP", status); print(json.dumps(data, ensure_ascii=False, indent=2))
    if status == 201 and "data" in data:
        print("✅ 投稿成功:", data["data"].get("id"))

if __name__ == "__main__":
    main()
