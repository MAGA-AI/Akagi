import threading
import traceback
import queue
import time
import re
from pathlib import Path
from dataclasses import dataclass
from collections import deque
from typing import Any
from playwright.sync_api import Page
from playwright.sync_api import (
    sync_playwright, Playwright, Browser, Page, WebSocket,
    TimeoutError as PWTimeout,
)

from email.header import Header
from email.utils import formatdate, make_msgid, formataddr
from .bridge import MajsoulBridge
from .logger import logger
from akagi.hooks import register_page
import os
from datetime import datetime
import requests
import os, json, ssl, smtplib
from email.mime.text import MIMEText
from typing import Optional, Dict, Any, Tuple, List
import logging
import pyautogui
import subprocess
from .slack_listener import start_socket_mode_in_thread, stop_event, logout_event, pop_logout_ctx, pop_stop_ctx, post_in_thread, pop_login_item_nowait, normalize_slack_text, pop_authcode_item_nowait

notify_log = logging.getLogger("akagi.notify")
AKAGI_DEBUG_NOTIFY        = os.getenv("AKAGI_DEBUG_NOTIFY", "0") == "1"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("")
LINE_USER_ID              = os.getenv("LINE_USER_ID", "")

SLACK_BOT_TOKEN  = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "")

# 段位しきい値（≦なら開始）
AKAGI_MAX_RANK_ID_4P = os.getenv("AKAGI_MAX_RANK_ID_4P", "10403")  # 例: "10401"（四麻 雀豪1 など）
AKAGI_MAX_RANK_ID_3P = os.getenv("AKAGI_MAX_RANK_ID_3P")  # 例: "20302"（三麻 雀傑2 など）

LEVEL_ID_4_TO_NAME = {
    10101: "初心1",
    10102: "初心2",
    10103: "初心3",
    10201: "雀士1",
    10202: "雀士2",
    10203: "雀士3",
    10301: "雀傑1",
    10302: "雀傑2",
    10303: "雀傑3",
    10401: "雀豪1",
    10402: "雀豪2",
    10403: "雀豪3",
    10501: "雀聖1",
    10502: "雀聖2",
    10503: "雀聖3",
    10601: "魂天",
    # 必要に応じて追加
}


_PROOF_DIR = Path("logs/click_proof"); _PROOF_DIR.mkdir(parents=True, exist_ok=True)

# === X (OAuth 2.0 / Authorization Code + PKCE) ===
import base64, hashlib, http.server, urllib.parse, urllib.request, webbrowser, secrets

X_CLIENT_ID   = os.getenv("X_CLIENT_ID", "Q2p6WmFLOC10UHVScXJfWG5TZ246MTpjaQ")
X_REDIRECT_URI= os.getenv("X_REDIRECT_URI", "http://127.0.0.1:8000/callback")
X_SCOPES      = os.getenv("X_SCOPES", "tweet.write tweet.read users.read offline.access")
X_TOKEN_FILE  = os.getenv("X_TOKEN_FILE", "./x_tokens.json")
X_TWEET_ENABLE= os.getenv("X_TWEET_ENABLE", "1") == "1"

AUTH_URL  = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
TWEET_URL = "https://api.twitter.com/2/tweets"

# === Post to X only when 10 NEW games accumulated ===
NEW10_BUFFER_FILE = Path().cwd() / "last10_buffer.json"

def _load_new10_buffer() -> list[tuple[int, int]]:
    try:
        data = json.loads(NEW10_BUFFER_FILE.read_text(encoding="utf-8"))
        items = data.get("results", [])
        return [(int(r), int(d)) for r, d in items if r is not None and d is not None]
    except Exception:
        return []

def _save_new10_buffer(items: list[tuple[int, int]]) -> None:
    try:
        payload = {"results": [[int(r), int(d)] for r, d in items]}
        NEW10_BUFFER_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        notify_log.warning(f"[X] failed to save buffer: {e}")

def _compose_new10_x_post(items: list[tuple[int, int]]) -> str:
    total = sum(d for (_, d) in items) if items else 0
    # Emoji by total
    if total >= 200:
        emoji = "🥳"
    elif 50 <= total < 200:
        emoji = "😊"
    elif -50 <= total < 50:
        emoji = "😐"
    elif -200 <= total < -50:
        emoji = "😣"
    else:
        emoji = "💀"
    lines = [f"{emoji}", f"合計：{total:+}", "====", f"直近10戦の成績："]
    for r, d in items:
        lines.append(f"{r}位({d:+})")
    return "\n".join(lines)

def _append_and_maybe_post_new10(rank: int, delta: int, *, interactive_auth: bool = True) -> bool:
    """Append a single game's (rank, delta) and post to X only when NEW 10 items are ready.
    If multiple batches (20, 30, ...) exist, it will post one batch per call until fewer than 10 remain.
    On post failure, it will keep the batch for retry on next call.
    Returns True if at least one post succeeded in this call.
    """
    buf = _load_new10_buffer()
    buf.append((int(rank), int(delta)))
    posted_any = False

    while len(buf) >= 10:
        batch = buf[:10]
        msg = _compose_new10_x_post(batch)
        ok = send_x_post_api(message=msg, interactive_auth=interactive_auth)
        if ok:
            # remove posted batch and continue if more remain
            buf = buf[10:]
            posted_any = True
        else:
            # keep batch for retry next time
            break

    _save_new10_buffer(buf)
    return posted_any
LAST10_RESULTS: deque[tuple[int, int]] = deque(maxlen=10)

def _compose_last10_x_post() -> str:
    """
    Build X post text like the following:
    🥳
    合計：+250
    ====
    直近10戦の成績：
    1位(+30)
    ...
    """
    items = list(LAST10_RESULTS)
    n = len(items)
    total = sum(d for (_, d) in items) if n else 0

    # Emoji by total
    if total >= 200:
        emoji = "🥳"
    elif 50 <= total < 200:
        emoji = "😊"
    elif -50 <= total < 50:
        emoji = "😐"
    elif -200 <= total < -50:
        emoji = "😣"
    else:
        emoji = "💀"

    lines = [f"{emoji}", f"合計：{total:+}", "====", f"直近{n if n < 10 else 10}戦の成績："]
    for r, d in items:
        lines.append(f"{r}位({d:+})")
    return "\n".join(lines)

def post_last10_to_x(interactive_auth: bool = False) -> bool:
    msg = _compose_last10_x_post()
    return send_x_post_api(message=msg, interactive_auth=interactive_auth)


def _b64url_no_pad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def _now() -> int:
    return int(time.time())

def _load_tokens() -> dict:
    if not os.path.exists(X_TOKEN_FILE):
        return {}
    with open(X_TOKEN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_tokens(tokens: dict) -> None:
    with open(X_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)

@dataclass
class _PKCE:
    verifier: str
    challenge: str
    state: str

def _make_pkce() -> _PKCE:
    verifier  = _b64url_no_pad(secrets.token_bytes(64))[:64]
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode()).digest())
    state     = _b64url_no_pad(secrets.token_bytes(16))
    return _PKCE(verifier, challenge, state)

class _AuthHandler(http.server.BaseHTTPRequestHandler):
    received: dict[str, str] = {}
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = q.get("code", [None])[0]
        state = q.get("state", [""])[0]
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.end_headers()
        if code:
            _AuthHandler.received = {"code": code, "state": state}
            self.wfile.write(b"<h2>Authorization complete.</h2><p>You can close this tab.</p>")
        else:
            self.wfile.write(b"<h2>No code found.</h2>")
    def log_message(self, *a, **k): pass

def _wait_code(port: int, timeout_sec: int = 300) -> dict:
    srv = http.server.HTTPServer(("127.0.0.1", port), _AuthHandler)
    srv.socket.settimeout(1.0)
    start = _now()
    try:
        while _now() - start < timeout_sec:
            srv.handle_request()
            if _AuthHandler.received.get("code"):
                return _AuthHandler.received
        raise TimeoutError("Timed out waiting for authorization code.")
    finally:
        try: srv.server_close()
        except: pass

def _build_auth_url(client_id: str, redirect_uri: str, scopes: str, code_challenge: str, state: str) -> str:
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes.split()),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return "https://twitter.com/i/oauth2/authorize?" + urllib.parse.urlencode(q)

def _auth_header_basic_with_client_id_only(client_id: str) -> str:
    # client_secretを発行していないPKCEアプリ向け: "client_id:" をBase64
    raw = (client_id + ":").encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")

def _urlopen_json(req: urllib.request.Request) -> tuple[int, dict]:
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

def _exchange_code_for_tokens(client_id: str, code: str, redirect_uri: str, code_verifier: str) -> dict:
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
    status, data = _urlopen_json(req)
    if status == 200 and "access_token" in data:
        data["expires_at"] = _now() + int(data.get("expires_in", 0))
    return data

def _refresh_tokens(client_id: str, refresh_token: str) -> dict:
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
    status, data = _urlopen_json(req)
    if status == 200 and "access_token" in data:
        data["expires_at"] = _now() + int(data.get("expires_in", 0))
    return data

def _ensure_x_access_token(interactive: bool = False) -> str | None:
    """
    初回はブラウザで認可 → ローカルHTTPでcode受領 → トークン保存。
    既に保存済みなら自動リフレッシュ。
    interactive=False のとき、未認可なら None を返す（メインループをブロックしないため）。
    """
    if not X_CLIENT_ID:
        notify_log.error("X_CLIENT_ID 未設定"); return None

    tokens = _load_tokens()
    if tokens.get("access_token"):
        if tokens.get("expires_at") and _now() < int(tokens["expires_at"]) - 60:
            return tokens["access_token"]
        if tokens.get("refresh_token"):
            new_tokens = _refresh_tokens(X_CLIENT_ID, tokens["refresh_token"])
            if "access_token" in new_tokens:
                if "refresh_token" not in new_tokens and "refresh_token" in tokens:
                    new_tokens["refresh_token"] = tokens["refresh_token"]
                _save_tokens(new_tokens)
                return new_tokens["access_token"]
            notify_log.warning("[X] refresh failed, need re-auth")

    if not interactive:
        # 非対話モードではブロックしない
        return None

    # 対話モード：認可フロー開始
    pkce = _make_pkce()
    auth_url = _build_auth_url(X_CLIENT_ID, X_REDIRECT_URI, X_SCOPES, pkce.challenge, pkce.state)
    notify_log.info(f"[X] Open auth URL: {auth_url}")
    try: webbrowser.open(auth_url)
    except: pass
    port = urllib.parse.urlparse(X_REDIRECT_URI).port or 9876
    received = _wait_code(port)
    if "code" not in received:
        notify_log.error("[X] 認可コード受領失敗"); return None
    data = _exchange_code_for_tokens(X_CLIENT_ID, received["code"], X_REDIRECT_URI, pkce.verifier)
    if "access_token" not in data:
        notify_log.error(f"[X] トークン交換失敗: {data}"); return None
    _save_tokens(data)
    return data["access_token"]

def send_x_post_api(message: str,
                    max_retries: int = 3,
                    timeout_sec: int = 10,
                    interactive_auth: bool = False) -> bool:
    """
    X API v2 でツイート。トークンは PKCE フローで自動取得/更新。
    初回未認可の場合、interactive_auth=True ならブラウザで認可を開始。
    """
    if not X_TWEET_ENABLE:
        notify_log.info("[X] tweet disabled by X_TWEET_ENABLE=0")
        return True

    token = _ensure_x_access_token(interactive=interactive_auth)
    if not token:
        notify_log.error("[X] access_token unavailable (未認可 or 設定不備)")
        return False

    url = TWEET_URL
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {"text": message if len(message) <= 280 else (message[:279] + "…")}

    if AKAGI_DEBUG_NOTIFY:
        notify_log.info(f"[X] post -> payload={json.dumps(payload, ensure_ascii=False)[:300]}")
    else:
        notify_log.info("[X] post -> payload=(masked)")

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout_sec)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "1"))
                notify_log.warning(f"[X] 429 Too Many Requests, retry after {retry_after}s (attempt {attempt}/{max_retries})")
                time.sleep(retry_after)
                continue

            data = {}
            try: data = resp.json()
            except Exception: pass

            if 200 <= resp.status_code < 300 and data.get("data", {}).get("id"):
                notify_log.info(f"[X] sent OK (id={data['data']['id']})")
                return True

            snippet = resp.text[:500]
            if resp.status_code in (400, 401, 403):
                notify_log.error(f"[X] auth/perm error {resp.status_code}: {snippet}")
                # 認可が消えていたら次回対話で認可できるように token ファイルを消すオプションもあり
                return False

            notify_log.error(f"[X] API error: {resp.status_code} {snippet}")
        except requests.Timeout:
            notify_log.error("[X] timeout")
        except Exception as e:
            notify_log.exception(f"[X] exception: {e}")

        sleep_sec = 2 ** attempt
        notify_log.warning(f"[X] retrying in {sleep_sec}s (attempt {attempt}/{max_retries})")
        time.sleep(sleep_sec)

    return False

# --- (任意) 画像付きポスト: v1.1 media/upload → v2/tweets ---
def _x_upload_media(filepath: str) -> str | None:
    token = _ensure_x_access_token()
    if not token: return None
    url = "https://upload.twitter.com/1.1/media/upload.json"
    with open(filepath, "rb") as f:
        files = {"media": f}
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.post(url, headers=headers, files=files, timeout=30)
    if resp.ok:
        return resp.json().get("media_id_string")
    notify_log.error(f"[X] media upload failed: {resp.status_code} {resp.text[:200]}")
    return None

def send_x_post_with_images(message: str, image_paths: list[str]) -> bool:
    token = _ensure_x_access_token()
    if not token: return False
    mids = []
    for p in image_paths[:4]:
        mid = _x_upload_media(p)
        if mid: mids.append(mid)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"text": message}
    if mids: payload["media"] = {"media_ids": mids}
    resp = requests.post(TWEET_URL, headers=headers, json=payload, timeout=30)
    ok = (200 <= resp.status_code < 300)
    if ok:
        notify_log.info("[X] sent with images OK")
    else:
        notify_log.error(f"[X] image tweet error {resp.status_code}: {resp.text[:300]}")
    return ok


def wait_for_account_ready(page: Page, timeout_ms: int = 180_000, poll_ms: int = 500) -> bool:
    """
    GameMgr/NetAgent 初期化と account_id>0（=ログイン完了）まで待つ。
    進捗をログ出力。True=準備OK, False=タイムアウト。
    """
    end = time.time() + (timeout_ms / 1000.0)
    js_probe = r"""
    () => {
      const id = (globalThis.GameMgr && GameMgr.Inst && typeof GameMgr.Inst.account_id !== 'undefined')
          ? GameMgr.Inst.account_id : -1;
      const hasAcc = !!(GameMgr && GameMgr.Inst && GameMgr.Inst.account_data);
      const hasNet = !!(globalThis.app && app.NetAgent && app.NetAgent.sendReq2Lobby);
      // 参照できる環境により名前が違うことがあるので広めに見る
      const inHall = !!(GameMgr?.Inst?.in_hall || GameMgr?.Inst?.in_lobby || GameMgr?.Inst?.lobby);
      return { id, hasAcc, hasNet, inHall };
    }
    """
    last_print = 0.0
    while time.time() < end:
        try:
            st = page.evaluate(js_probe)
            # 1秒に1回くらい進捗ログ
            now = time.time()
            if now - last_print > 1.0:
                notify_log.info(f"[READY] id={st.get('id')} hasAcc={st.get('hasAcc')} hasNet={st.get('hasNet')} inHall={st.get('inHall')}")
                last_print = now
            if isinstance(st, dict) and (int(st.get("id", -1)) > 0):
                notify_log.info("[READY] account_id > 0 を確認。段位取得を開始します。")
                return True
        except Exception:
            pass
        page.wait_for_timeout(poll_ms)
    notify_log.warning("[READY] タイムアウト：account_id が正になりませんでした（未ログイン/未入場）")
    return False

def allow_auto_start_by_rank(page: Page) -> bool:
    # 準備ができるまで待つ（未ログインなら False）
    if not wait_for_account_ready(page, timeout_ms=180_000):
        notify_log.warning("[gate] account 未準備（account_id<=0）。ログイン/入場完了後に再試行してください。")
        return False

    max4 = _as_int(AKAGI_MAX_RANK_ID_4P)
    max3 = _as_int(AKAGI_MAX_RANK_ID_3P)

    info = fetch_current_rank_ids(page)
    if info is None or (info.get("level_id_4") is None and info.get("level_id_3") is None):
        if max4 is None and max3 is None:
            notify_log.info("[gate] rank not detected, but no thresholds configured -> allow")
            return True
        notify_log.warning("[gate] rank not detected (after ready) and thresholds configured -> deny")
        return False

    cur4 = info.get("level_id_4"); cur3 = info.get("level_id_3")
    acc = info.get("account_id");  nick = info.get("nickname")
    notify_log.info(f"[gate] account={acc} nick={nick} cur4={cur4} cur3={cur3} max4={max4} max3={max3}")

    if max4 is not None:
        if cur4 is None:
            notify_log.warning("[gate] 4p threshold set but current 4p rank unknown -> deny")
            return False
        allow = (cur4 <= max4)
        notify_log.info(f"[gate] 4p check: {cur4} <= {max4} -> {allow}")
        return allow

    if max3 is not None:
        if cur3 is None:
            notify_log.warning("[gate] 3p threshold set but current 3p rank unknown -> deny")
            return False
        allow = (cur3 <= max3)
        notify_log.info(f"[gate] 3p check: {cur3} <= {max3} -> {allow}")
        return allow

    notify_log.info("[gate] no thresholds -> allow")
    return True


def _as_int(v):
    if v is None: return None
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return None

# ★ 追加: 現在段位ID取得（四麻 level.id / 三麻 level3.id）
def fetch_current_rank_ids(page: Page) -> Optional[dict]:
    """
    段位IDを堅牢に取得。詳細デバッグ情報付き。
    返り値: {
        "account_id": int|None, "nickname": str|None,
        "level_id_4": int|None, "level_id_3": int|None,
        "_source": "local" | "fetchInfo_account" | "fetchInfo_list" | "fetchAccountInfo",
        "debug": [str, ...]  # 取得経路ログ
    }
    """
    js = r"""
    () => new Promise((resolve) => {
      const debug = [];
      function j(o){ try{return JSON.stringify(o)}catch(_){return String(o)}}
      function keys(o){ try{return Object.keys(o||{}).join(',')}catch(_){return ''} }

      function num(v){
        if (typeof v === "number") return v;
        if (v && typeof v.toNumber === "function") { try { return v.toNumber(); } catch(_){} }
        if (typeof v === "string" && v.trim() !== "") { const n = Number(v); if (!Number.isNaN(n)) return n; }
        return null;
      }
      function pickInt(v){ const n = num(v); return (n == null ? null : Math.trunc(n)); }

      const myId = (globalThis.GameMgr && GameMgr.Inst && GameMgr.Inst.account_id) ? GameMgr.Inst.account_id : null;
      debug.push("myId=" + myId);
      if (!myId) { debug.push("no myId -> abort"); resolve({debug}); return; }

      // ---- 1) local (GameMgr.Inst.account_data)
      try{
        const acc = GameMgr?.Inst?.account_data || null;
        debug.push("local: has_acc=" + !!acc);
        if (acc){
          const level  = acc.level  || {};
          const level3 = acc.level3 || {};
          const out = {
            account_id: pickInt(acc.account_id ?? myId),
            nickname: (acc.nickname ?? acc.nick ?? null),
            level_id_4: pickInt(level.id),
            level_id_3: pickInt(level3.id),
            _source: "local",
            debug
          };
          debug.push("local ids: 4=" + out.level_id_4 + " 3=" + out.level_id_3);
          if (out.level_id_4 != null || out.level_id_3 != null) { resolve(out); return; }
        }
      }catch(e){ debug.push("local err: "+e); }

      // ---- 2) fetchInfo({})
      try{
        app.NetAgent.sendReq2Lobby("Lobby", "fetchInfo", {}, function(err, resp){
          if (err){ debug.push("fetchInfo{} err"); step3(); return; }
          debug.push("fetchInfo{} keys=" + keys(resp));
          if (resp && resp.error != null) debug.push("fetchInfo{} error=" + j(resp.error));
          const a = resp && (resp.account || resp.info || resp.player || null);
          if (a){
            const level  = a.level  || {};
            const level3 = a.level3 || {};
            const out = {
              account_id: pickInt(a.account_id ?? a.id ?? myId),
              nickname: (a.nickname ?? a.nick ?? null),
              level_id_4: pickInt(level.id),
              level_id_3: pickInt(level3.id),
              _source: "fetchInfo_account",
              debug
            };
            debug.push("fetchInfo{} ids: 4=" + out.level_id_4 + " 3=" + out.level_id_3);
            if (out.level_id_4 != null || out.level_id_3 != null) { resolve(out); return; }
          }else{
            debug.push("fetchInfo{} no account field");
          }
          step3(); // continue
        });
      }catch(e){ debug.push("fetchInfo{} throw:"+e); step3(); }

      // ---- 3) fetchInfo({account_id_list:[myId]})  ※配列系の形状を総当り
      function step3(){
        try{
          app.NetAgent.sendReq2Lobby("Lobby", "fetchInfo", { account_id_list: [myId] }, function(err, resp){
            if (err){ debug.push("fetchInfo[list] err"); step4(); return; }
            debug.push("fetchInfo[list] keys=" + keys(resp));
            if (resp && resp.error != null) debug.push("fetchInfo[list] error=" + j(resp.error));
            const candidates = resp && (resp.infos || resp.accounts || resp.players || resp.account_info || []);
            const arr = Array.isArray(candidates) ? candidates : [candidates];
            let picked = null;
            for (const cand of arr){
              if (!cand) continue;
              const a = cand.account || cand; // 中に account を抱える形とフラットの両方に対応
              const id = pickInt(a && (a.account_id ?? a.id));
              if (id != null && id === pickInt(myId)){ picked = a; break; }
            }
            if (picked){
              const level  = picked.level  || {};
              const level3 = picked.level3 || {};
              const out = {
                account_id: pickInt(picked.account_id ?? picked.id ?? myId),
                nickname: (picked.nickname ?? picked.nick ?? null),
                level_id_4: pickInt(level.id),
                level_id_3: pickInt(level3.id),
                _source: "fetchInfo_list",
                debug
              };
              debug.push("fetchInfo[list] ids: 4=" + out.level_id_4 + " 3=" + out.level_id_3);
              if (out.level_id_4 != null || out.level_id_3 != null) { resolve(out); return; }
            }else{
              debug.push("fetchInfo[list] no matched account");
            }
            step4();
          });
        }catch(e){ debug.push("fetchInfo[list] throw:"+e); step4(); }
      }

      // ---- 4) fetchAccountInfo({account_id: myId})
      function step4(){
        try{
          app.NetAgent.sendReq2Lobby("Lobby", "fetchAccountInfo", { account_id: myId }, function(err, resp){
            if (err){ debug.push("fetchAccountInfo err"); finish(); return; }
            debug.push("fetchAccountInfo keys=" + keys(resp));
            if (resp && resp.error != null) debug.push("fetchAccountInfo error=" + j(resp.error));
            const a = resp && (resp.account || resp.info || resp.player || resp) || null;
            if (a){
              const level  = a.level  || {};
              const level3 = a.level3 || {};
              const out = {
                account_id: pickInt(a.account_id ?? a.id ?? myId),
                nickname: (a.nickname ?? a.nick ?? null),
                level_id_4: pickInt(level.id),
                level_id_3: pickInt(level3.id),
                _source: "fetchAccountInfo",
                debug
              };
              debug.push("fetchAccountInfo ids: 4=" + out.level_id_4 + " 3=" + out.level_id_3);
              resolve(out); return;
            }
            finish();
          });
        }catch(e){ debug.push("fetchAccountInfo throw:"+e); finish(); }
      }

      function finish(){
        debug.push("all paths failed");
        resolve({ debug });
      }
    })
    """
    try:
        data = page.evaluate(js)
        # JS 側で debug を常に返すので、ここで状況を可視化
        dbg = (data or {}).get("debug") if isinstance(data, dict) else None
        if isinstance(dbg, list):
            for line in dbg:
                notify_log.info(f"[RANK.debug] {line}")

        if not data or not isinstance(data, dict):
            notify_log.warning("[RANK] JS returned non-dict or null")
            return None

        # 正規化
        data["account_id"] = _as_int(data.get("account_id"))
        data["level_id_4"] = _as_int(data.get("level_id_4"))
        data["level_id_3"] = _as_int(data.get("level_id_3"))

        notify_log.info(f"[RANK] ids fetched: 4p={data.get('level_id_4')} 3p={data.get('level_id_3')} source={data.get('_source')}")
        return data if (data.get("level_id_4") is not None or data.get("level_id_3") is not None) else data  # 取れなかった場合も debug のため返す
    except Exception:
        notify_log.exception("[RANK] fetch_current_rank_ids failed")
        return None



def _peek_both_scores(page):
    js = r"""
    () => new Promise((resolve) => {
      app.NetAgent.sendReq2Lobby("Lobby", "fetchInfo", {}, (err, info) => {
        if (err || !info || !info.account) { resolve(null); return; }
        const l4 = info.account.level?.score ?? null;
        const l3 = info.account.level3?.score ?? null;
        resolve({ level_score_4p: l4, level_score_3p: l3 });
      });
    })
    """
    try:
        return page.evaluate(js)
    except Exception:
        return None

def fetch_rank_score_with_retry(page: Page, is_sanma: bool, retries: int = 3, wait_ms: int = 300) -> Optional[int]:
    """
    fetchInfo を叩いて Account.level / level3 の score を取得。
    取りこぼしや反映遅延に備えてリトライ。
    """
    js = r"""
    (isSanma) => new Promise((resolve) => {
      app.NetAgent.sendReq2Lobby("Lobby", "fetchInfo", {},
        function(err, info){
          if (err || !info || !info.account) { resolve(null); return; }
          const lv = isSanma ? info.account.level3 : info.account.level;
          const v = (lv && typeof lv.score === 'number') ? lv.score : null;
          resolve(v);
        });
    })
    """
    for _ in range(retries):
        try:
            v = page.evaluate(js, is_sanma)
            if isinstance(v, (int, float)):
                return int(v)
        except Exception:
            pass
        page.wait_for_timeout(wait_ms)
    return None


def fetch_my_latest_result(page) -> Optional[dict]:
    # ...（docstringはそのまま）

    js = r"""
    () => new Promise((resolve) => {
    function clone(x){ try{ return JSON.parse(JSON.stringify(x)); }catch(_){ return null; } }
    // Long/文字列 -> number 正規化
    function num(v){
        if (typeof v === "number") return v;
        if (v && typeof v.toNumber === "function") { // protobuf Long
        try { return v.toNumber(); } catch(_) {}
        }
        if (typeof v === "string" && v.trim() !== "") {
        const n = Number(v);
        if (!Number.isNaN(n)) return n;
        }
        return null;
    }

    const myId = GameMgr?.Inst?.account_id;
    if (!myId) { resolve(null); return; }

    app.NetAgent.sendReq2Lobby("Lobby", "fetchGameRecordList",
        { start: 0, count: 1, type: 2 },
        function(err, list){
        if (err || !list || !list.record_list || list.record_list.length === 0) {
            resolve(null); return;
        }
        const uuid = list.record_list[0].uuid;

        app.NetAgent.sendReq2Lobby("Lobby", "fetchGameRecord",
            { game_uuid: uuid, client_version_string: GameMgr.Inst.getClientVersion() },
            function(err2, rec){
            if (err2 || !rec) { resolve(null); return; }

            const head = clone(rec.head) || {};
            const accounts = Array.isArray(head.accounts) ? head.accounts : [];
            const result = head.result || {};
            const players = Array.isArray(result.players) ? result.players : [];

            // 自席 seat
            let mySeat = null, myAcc = null;
            for (let i = 0; i < accounts.length; i++) {
                const a = accounts[i];
                if (!a) continue;
                if (a.account_id === myId) { mySeat = (a.seat ?? i); myAcc = a; break; }
            }
            if (mySeat === null) { resolve(null); return; }

            // 順位
            let rank = null;
            if (players.length > 0) {
                const sorted = clone(players).sort((a,b)=>( (num(b?.total_point ?? b?.totalScore ?? b?.score) ?? 0) - (num(a?.total_point ?? a?.totalScore ?? a?.score) ?? 0) ));
                rank = 1 + sorted.findIndex(p => p && p.seat === mySeat);
                if (rank <= 0) rank = null;
            }

            const isSanma = (accounts.length === 3);

            // スコア/増減の抽出
            const pickScore = (p) => {
                if (!p) return null;
                return num(p.total_point ?? p.totalScore ?? p.score);
            };
            const pickDelta = (p) => {
                if (!p) return null;
                return num(p.grading_score ?? p.rating_score ?? p.delta);
            };

            let myTotal = null, myDelta = null;
            for (const p of players) {
                if (p && p.seat === mySeat) {
                myTotal = pickScore(p);
                myDelta = pickDelta(p);
                break;
                }
            }

            // 牌譜の「対局者」から開始時段位ポイントを取得（四麻: level.score / 三麻: level3.score）
            const gradingBefore = isSanma
                ? num(myAcc?.level3?.score)
                : num(myAcc?.level?.score);

            // after は before + delta（どちらか欠けたら null）
            const gradingAfter = (gradingBefore != null && myDelta != null)
                ? (gradingBefore + myDelta)
                : null;

            resolve({
                uuid,
                rank,
                score: myTotal,
                grading_delta: myDelta,
                grading_after: gradingAfter,   // ← ここが埋まる
                grading_before: gradingBefore, // ← デバッグ用に返すと便利
                is_sanma: !!isSanma
            });
            });
        });
    })
    """

    try:
        data = page.evaluate(js)
        notify_log.info(f"[API] data={str(data)}")
        if not data or not isinstance(data, dict):
            return None

        return data
    except Exception:
        return None

def _mask(s: str, show: int = 6) -> str:
    if not s: return ""
    return s[:show] + "..." if len(s) > show else "***"


def send_line_message_api(message: str) -> bool:
    """LINE Messaging API で push 通知を送る（詳細ログ付き）"""
    if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID):
        notify_log.error("[LINE] token or user_id missing")
        return False
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": message}],
    }

    # ログ（トークンは伏せる）
    if AKAGI_DEBUG_NOTIFY:
        notify_log.info(f"[LINE] push -> user={_mask(LINE_USER_ID, 6)} payload={json.dumps(payload, ensure_ascii=False)}")
    else:
        notify_log.info(f"[LINE] push -> user={_mask(LINE_USER_ID, 6)}")

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        ok = (resp.status_code == 200)
        if ok:
            notify_log.info("[LINE] sent OK (200)")
        else:
            notify_log.error(f"[LINE] API error: {resp.status_code} {resp.text[:500]}")
        return ok
    except requests.Timeout:
        notify_log.error("[LINE] timeout")
        return False
    except Exception as e:
        notify_log.exception(f"[LINE] exception: {e}")
        return False
    
def send_slack_message_api(
    message: str,
    channel_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
    max_retries: int = 3,
    timeout_sec: int = 10,
) -> bool:
    """
    Slack の chat.postMessage で通知を送る（簡易リトライ付き、429対応）。
    依存: requests
    """
    token = SLACK_BOT_TOKEN
    channel = channel_id or SLACK_CHANNEL_ID

    if not token or not channel:
        notify_log.error("[Slack] token or channel_id missing")
        return False

    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload: Dict[str, Any] = {"channel": channel, "text": message}
    if thread_ts:
        payload["thread_ts"] = thread_ts  # スレッド返信したいとき

    # ログ（トークンは伏せる）
    if AKAGI_DEBUG_NOTIFY:
        notify_log.info(f"[Slack] post -> ch={_mask(channel, 6)} payload={json.dumps(payload, ensure_ascii=False)}")
    else:
        notify_log.info(f"[Slack] post -> ch={_mask(channel, 6)}")

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout_sec)
            if resp.status_code == 429:
                # レート制限: Retry-After 秒待つ
                retry_after = int(resp.headers.get("Retry-After", "1"))
                notify_log.warning(f"[Slack] 429 Too Many Requests, retry after {retry_after}s (attempt {attempt}/{max_retries})")
                time.sleep(retry_after)
                continue

            data = {}
            try:
                data = resp.json()
            except Exception:
                pass

            if resp.ok and data.get("ok"):
                notify_log.info(f"[Slack] sent OK (ts={data.get('ts')})")
                return True

            # それ以外は内容をログ出し
            snippet = resp.text[:500]
            notify_log.error(f"[Slack] API error: {resp.status_code} {snippet}")
        except requests.Timeout:
            notify_log.error("[Slack] timeout")
        except Exception as e:
            notify_log.exception(f"[Slack] exception: {e}")

        # 次の試行まで指数バックオフ
        sleep_sec = 2 ** attempt
        notify_log.warning(f"[Slack] retrying in {sleep_sec}s (attempt {attempt}/{max_retries})")
        time.sleep(sleep_sec)

    return False


def try_extract_end_result_from_text_frame(payload: str) -> Tuple[Optional[int], Optional[int]]:
    """WS文字列(JSON想定)から (rank, point) を抽出。結果をログ。"""
    try:
        data = json.loads(payload)
    except Exception:
        notify_log.debug("[extract:text] not json")
        return (None, None)

    rank = None; point = None
    rank_keys  = ["rank", "place", "final_rank", "result_rank"]
    point_keys = ["point", "points", "finalPoint", "grade_score", "rating_score", "delta"]

    def walk(obj):
        nonlocal rank, point
        if isinstance(obj, dict):
            t = str(obj.get("type") or obj.get("event") or "").lower()
            if "end" in t or "result" in t:
                for k, v in obj.items():
                    lk = str(k).lower()
                    if rank is None and any(rk in lk for rk in rank_keys):
                        try:
                            r = int(v)
                            if 1 <= r <= 4: rank = r
                        except: pass
                    if point is None and any(pk in lk for pk in point_keys):
                        try:
                            point = int(v)
                        except:
                            try: point = int(float(v))
                            except: pass
            for v in obj.values():
                if rank is not None and point is not None: break
                walk(v)
        elif isinstance(obj, list):
            for it in obj:
                if rank is not None and point is not None: break
                walk(it)

    walk(data)
    notify_log.info(f"[extract:text] rank={rank} point={point}")
    return (rank, point)


def try_extract_end_result_from_parsed_msg(m: dict) -> Tuple[Optional[int], Optional[int]]:
    """bridge.parse() の1要素(dict想定)から (rank, point) を抽出。結果をログ。"""
    if not isinstance(m, dict):
        notify_log.debug("[extract:parsed] not dict")
        return (None, None)
    rank = None; point = None
    for k in ["rank", "place", "final_rank", "result_rank"]:
        if k in m:
            try:
                r = int(m[k])
                if 1 <= r <= 4: rank = r; break
            except: pass
    for k in ["point", "points", "finalPoint", "grade_score", "rating_score", "delta"]:
        if k in m:
            try:
                point = int(m[k])
            except:
                try: point = int(float(m[k]))
                except: pass
            break
    notify_log.info(f"[extract:parsed] rank={rank} point={point}")
    return (rank, point)


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

def _snap(page: Page, label: str) -> str:
    """素のスクショ保存"""
    path = _PROOF_DIR / f"{_ts()}_{label}.png"
    page.screenshot(path=str(path))
    logger.info(f"[Proof] screenshot: {path}")
    return str(path)

def _snap_with_marker(page: Page, x: int, y: int, label: str) -> str:
    """
    クリック位置に一時的なマーカー(div)を重ねてスクショ → 即削除。
    これで“どこを押したか”が画像で一目瞭然。
    """
    page.evaluate("""
        ([x, y]) => {
          const id = "__akagi_click_marker__";
          let el = document.getElementById(id);
          if (!el) {
            el = document.createElement('div');
            el.id = id;
            el.style.position = 'fixed';
            el.style.zIndex = 999999;
            el.style.width = '18px';
            el.style.height = '18px';
            el.style.borderRadius = '50%';
            el.style.border = '2px solid red';
            el.style.background = 'rgba(255,0,0,0.25)';
            el.style.pointerEvents = 'none';
            document.body.appendChild(el);
          }
          el.style.left = (x - 9) + 'px';
          el.style.top  = (y - 9) + 'px';
        }
    """, [x, y])
    page.wait_for_timeout(50)
    path = _snap(page, label)
    # 後片付け
    page.evaluate("""
        () => {
          const el = document.getElementById('__akagi_click_marker__');
          if (el && el.parentNode) el.parentNode.removeChild(el);
        }
    """)
    return path


# =========================
# Post-game helpers (module-level)
# =========================

class PostGameGuard:
    """直近アクティビティ時刻を管理して、一定時間“静止”している時だけ後片付けを許可"""
    def __init__(self) -> None:
        self._last_activity = time.time()

    def bump(self) -> None:
        self._last_activity = time.time()

    def idle_for(self, sec: float) -> bool:
        return (time.time() - self._last_activity) >= sec


# =========================
# PlaywrightController
# =========================

# フロー管理（bridge は既存実装に準拠）
activated_flows: list[str] = []  # store all flow.id ([-1] is the recently opened)
majsoul_bridges: dict[WebSocket, MajsoulBridge] = {}  # store all flow.id -> MajsoulBridge
mjai_messages: queue.Queue[dict] = queue.Queue()  # store all messages


class PlaywrightController:
    """
    A controller for a Playwright browser instance that runs in the main thread.
    It manages a single page, processes commands from a queue, monitors WebSockets,
    and handles clicking based on a normalized 16x9 grid.
    """

    def __init__(self, url: str, width: int = 1600, height: int = 900) -> None:
        """
        Initializes the controller.
        Args:
            url (str): The fixed URL the browser page will navigate to.
        """
        self.url = url
        self.width = width
        self.height = height
        self.command_queue: queue.Queue[dict] = queue.Queue()
        self.running = False

        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.page: Page | None = None

        self.bridge_lock = threading.Lock()
        self._postgame_guard = PostGameGuard()
        self._ended = False  # ← 終局フラグ（WS/解析で True）
        self._started = False  # ← 追加：次の対戦が始まったか
        self._last_end_rank: Optional[int] = None
        self._last_end_point: Optional[int] = None
        self._auto_started_once = False
    # -------------- WebSocket -------------

    def _on_web_socket(self, ws: WebSocket) -> None:
        """Callback for new WebSocket connections."""
        global majsoul_bridges
        logger.info(f"[WebSocket] Connection opened: {ws.url}")

        # Create and store a bridge for this new WebSocket flow
        majsoul_bridges[ws] = MajsoulBridge()

        # Set up listeners for messages and closure on this specific WebSocket instance
        ws.on("framesent", lambda payload: self._on_frame(ws, payload, from_client=True))
        ws.on("framereceived", lambda payload: self._on_frame(ws, payload, from_client=False))
        ws.on("close", lambda: self._on_socket_close(ws))

    def _on_frame(self, ws: WebSocket, payload: str | bytes, from_client: bool) -> None:
        """Callback for WebSocket messages."""
        global mjai_messages, majsoul_bridges

        # アクティビティ更新（ゲームが動いている）
        self._postgame_guard.bump()

        bridge = majsoul_bridges.get(ws)
        if not bridge:
            logger.error(f"[WebSocket] Message from untracked WebSocket: {ws.url}")
            return

        # 文字列フレームに 'end_game' が含まれていれば即フラグ
        try:
            if isinstance(payload, str):
                if ('"type":"end_game"' in payload) or ("'type': 'end_game'" in payload):
                    self._ended = True
                    r, p = try_extract_end_result_from_text_frame(payload)
                    if r is not None: self._last_end_rank = r
                    if p is not None: self._last_end_point = p
                    notify_log.info(f"[ws:text] end_game detected rank={self._last_end_rank} point={self._last_end_point}")
                if ('"type":"start_game"' in payload) or ("'type': 'start_game'" in payload):
                    self._started = True
                    notify_log.info("[ws:text] start_game detected")
        except Exception:
            pass

        try:
            with self.bridge_lock:
                msgs = bridge.parse(payload)
            if msgs:
                for m in msgs:
                    try:
                        if isinstance(m, dict):
                            t = m.get("type")
                            if t == "end_game":
                                self._ended = True
                                r, p = try_extract_end_result_from_parsed_msg(m)
                                if r is not None: self._last_end_rank = r
                                if p is not None: self._last_end_point = p
                                notify_log.info(f"[ws:parsed] end_game detected rank={self._last_end_rank} point={self._last_end_point}")
                            elif t == "start_game":
                                self._started = True
                                notify_log.info("[ws:parsed] start_game detected")
                        mjai_messages.put(m)
                    except Exception:
                        pass
        except Exception:
            logger.error(f"[WebSocket] Error during message parsing: {traceback.format_exc()}")

    def _on_socket_close(self, ws: WebSocket) -> None:
        """Callback for WebSocket closures."""
        global majsoul_bridges
        if ws in majsoul_bridges:
            logger.info(f"[WebSocket] Connection closed: {ws.url}")
            del majsoul_bridges[ws]
        else:
            logger.warning(f"[WebSocket] Untracked WebSocket connection closed: {ws.url}")

    # -------------- Coordinates -------------

    def _get_clickxy(self, x: float, y: float) -> tuple[float | None, float | None]:
        """
        Converts normalized grid coordinates (0-16 for x, 0-9 for y)
        to pixel coordinates based on the current viewport size.
        """
        if not self.page:
            logger.error("Page is not available to get click coordinates.")
            return (None, None)

        viewport_size = self.page.viewport_size
        if not viewport_size:
            logger.error("Could not get viewport size.")
            return (None, None)

        viewport_width = viewport_size["width"]
        viewport_height = viewport_size["height"]

        target_aspect_ratio = 16 / 9
        viewport_aspect_ratio = viewport_width / viewport_height

        rect_width = viewport_width
        rect_height = viewport_height
        offset_x = 0
        offset_y = 0

        # Determine the dimensions of the 16:9 inscribed rectangle
        if viewport_aspect_ratio > target_aspect_ratio:
            # Viewport is wider than 16:9 (letterboxed)
            rect_width = int(viewport_height * target_aspect_ratio)
            offset_x = (viewport_width - rect_width) / 2
        else:
            # Viewport is taller than 16:9 (pillarboxed)
            rect_height = int(viewport_width / target_aspect_ratio)
            offset_y = (viewport_height - rect_height) / 2

        # Normalize grid coordinates (0-16 for x, 0-9 for y)
        if not (0 <= x <= 16 and 0 <= y <= 9):
            logger.warning(f"Click coordinates ({x}, {y}) are outside the 0-16, 0-9 grid.")
            return (None, None)

        # Calculate the absolute pixel coordinates
        click_x = offset_x + (x / 16) * rect_width
        click_y = offset_y + (y / 9) * rect_height
        return (click_x, click_y)

    def _move_mouse(self, click_x: float, click_y: float) -> None:
        """Moves the mouse to the specified pixel coordinates."""
        if not self.page:
            logger.error("Page is not available to move mouse.")
            return
        try:
            logger.info(f"Moving mouse to pixel ({click_x:.2f}, {click_y:.2f})")
            self.page.mouse.move(click_x, click_y)
        except Exception as e:
            logger.error(f"Failed to move mouse: {e}")

    def _click(self, click_x: float, click_y: float) -> None:
        """Clicks at the specified pixel coordinates."""
        if not self.page:
            logger.error("Page is not available to click.")
            return
        try:
            logger.info(f"Clicking at pixel ({click_x:.2f}, {click_y:.2f})")
            self.page.mouse.click(click_x, click_y)
        except Exception as e:
            logger.error(f"Failed to perform click: {e}")
    
    def _wait_started(self, timeout_sec: float = 30.0) -> bool:
        end = time.time() + timeout_sec
        while time.time() < end:
            if self._started:
                return True
            if self.page:
                self.page.wait_for_timeout(200)
        return False


    # -------------- Main loop -------------

    def _process_commands(self) -> None:
        """The main loop to process commands from the queue."""
        while True:
            # --- High-priority Slack "LOGOUT" trigger (runs regardless of UI state) ---
            try:
                if logout_event.is_set():
                    ctx = pop_logout_ctx()  # 受付時に保存したスレッド文脈
                    logger.info("[logout] Slack trigger detected. Running perform_logout()")
                    try:
                        perform_logout(self)
                        if ctx:
                            post_in_thread(ctx.channel, ctx.thread_ts, "✅ ログアウトが完了しました。", attach_latest=True)
                    except Exception as e:
                        logger.error(f"[logout] perform_logout failed: {e}")
                        if ctx:
                            post_in_thread(ctx.channel, ctx.thread_ts, f"❌ ログアウト失敗: {e}")
                    finally:
                        # prevent repeated executions unless a new message arrives
                        logout_event.clear()


                item = pop_login_item_nowait()
                if item:
                    try:
                        perform_login(self, item.text)
                        post_in_thread(item.channel, item.thread_ts, "✅ ログイン処理が完了しました。", attach_latest=True)
                    except Exception as e:
                        logger.error(f"[login] perform_login failed: {e}")
                        post_in_thread(item.channel, item.thread_ts, f"❌ ログイン処理に失敗しました: {e}")
                    finally:
                        if self.page:
                            self.page.wait_for_timeout(200)
                    continue  # このtickの他処理はスキップ

                # 3) 認証コード（新規）
                ac = pop_authcode_item_nowait()
                if ac:
                    try:
                        perform_authcode(self, ac.text)
                        self.page.wait_for_timeout(8_000)
                        _snap(self.page, "ログイン完了")
                        post_in_thread(ac.channel, ac.thread_ts, "✅ 認証コードの入力とログイン操作が完了しました。", attach_latest=True)

                        # 自動再開シーケンス
                        snap_before_start(self)
                        run_auto_start_sequence(self.page)
                    except Exception as e:
                        post_in_thread(ac.channel, ac.thread_ts, f"❌ 認証コード処理に失敗しました: {e}")
                    finally:
                        if self.page:
                            self.page.wait_for_timeout(200)
                    continue

            except Exception as e:
                logger.warning(f"[logout] pre-check error: {e}")

            try:
                command_data = self.command_queue.get_nowait()
                command = command_data.get("command")

                if command == "click":
                    point = command_data.get("point")
                    if point and len(point) == 2:
                        click_x, click_y = self._get_clickxy(point[0], point[1])
                        if click_x is None or click_y is None:
                            logger.error(f"Invalid click coordinates: {point}")
                            continue
                        self._move_mouse(click_x, click_y)
                        if self.page:
                            self.page.wait_for_timeout(100)
                        logger.info(f"Clicking at normalized grid point {point} -> pixel ({click_x:.2f}, {click_y:.2f})")
                        self._click(click_x, click_y)
                    else:
                        logger.error(f"Invalid 'click' command data: {command_data}")

                elif command == "delay":
                    delay = command_data.get("delay", 0)
                    if isinstance(delay, (int, float)) and delay >= 0:
                        logger.info(f"Delaying for {delay} seconds.")
                        if self.page:
                            self.page.wait_for_timeout(int(delay * 1000))
                    else:
                        logger.error(f"Invalid 'delay' command data: {command_data}")

                elif command == "stop":
                    # Clear queue and exit loop
                    while not self.command_queue.empty():
                        self.command_queue.get_nowait()
                    break

                else:
                    logger.warning(f"Unknown command received: {command}")

            except queue.Empty:
                if self.page:
                    self.page.wait_for_timeout(20)
                    try:
                        # 終局フラグが立っており、直近2秒アイドルなら後片付け実行
                        if self._ended and self._postgame_guard.idle_for(2.0):
                            logger.info("[PostGame] handling post-game flow...")
                            info = fetch_my_latest_result(self.page)
                            if info:
                                rank  = _as_int(info.get("rank"))
                                score = _as_int(info.get("score"))
                                delta = _as_int(info.get("grading_delta"))
                                total_score = _as_int(info.get("grading_after"))

                                # 追加: WS 由来ポイントの保険
                                if score is None and self._last_end_point is not None:
                                    score = _as_int(self._last_end_point)
                                
                                # ★ 順位に応じて補正を追加
                                bonus = 0
                                if rank == 1:
                                    bonus = 10000
                                elif rank == 2:
                                    bonus = 20000
                                elif rank == 3:
                                    bonus = 30000
                                elif rank == 4:
                                    bonus = 40000

                                # delta の表示形式を調整 (+付き)
                                delta_txt = "不明"
                                if delta is not None:
                                    delta_txt = f"{delta:+}"

                                disp_score = None if score is None else (score + bonus)
                                rank_txt  = f"{rank}位" if rank is not None else "不明"
                                score_txt = f"{disp_score:,}" if disp_score is not None else "不明"

                                # 送信用メッセージ
                                body = (
                                    f"結果順位: {rank_txt}\n"
                                    f"最終スコア: {score_txt}\n"
                                    f"加算ポイント: {delta_txt}\n"
                                    f"現在のポイント: {total_score}\n"
                                )
                                peek = _peek_both_scores(self.page)
                                notify_log.info(f"[peek] level_score_4p={peek and peek.get('level_score_4p')} "
                                                f"level_score_3p={peek and peek.get('level_score_3p')} "
                                                f"is_sanma_guess={info.get('is_sanma')}")
                                send_slack_message_api(message=body)

                                try:
                                    # 新規10戦が溜まったらだけ投稿
                                    if rank is not None and delta is not None:
                                        _append_and_maybe_post_new10(int(rank), int(delta), interactive_auth=True)
                                except Exception as e:
                                    notify_log.warning(f"[X] append/post error: {e}")
                                
                                # （本文 body や info は既存のまま）
                                rank_info = fetch_current_rank_ids(self.page) or {}
                                cur4 = rank_info.get("level_id_4")
                                
                            else:
                                # 取れなかった場合でも通知（必要なければ省略可）
                                body = (
                                    "雀魂 終局\n"
                                    "結果情報の取得に失敗しました（API応答なし）\n"
                                    f"時刻: {time.strftime('%Y/%m/%d %H:%M:%S')}"
                                )
                            

                            self._started = False
                            self._ended = False

                            self.page.wait_for_timeout(10_000)
                            self.page.wait_for_timeout(3_000)  # 少し待機
                            try:
                                self.page.reload()
                                self.page.wait_for_timeout(15_000)  # 少し待機
                                self.page.reload()
                                self.page.wait_for_timeout(16_000)
                            except Exception as e:
                                logger.error(f"[Recovery] reload failed: {e}")


                            # ★ ここでゲート判定 → 許可時のみ開始
                            try:
                                if allow_auto_start_by_rank(self.page) and not stop_event.is_set():
                                    run_auto_start_sequence(self.page)
                                else:
                                    # 対局終了フラグが立っていても必ずこちらに入る
                                    notify_log.warning("[auto-start] skipped by rank gate (post-game or forced stop)")
                                    body = (
                                        "🎉代行完了🎉\n"
                                        f"目標の段位に到達しました。\n代打ちを終了します。\n"
                                        f"時刻: {time.strftime('%Y/%m/%d %H:%M')}"
                                    )
                                    send_slack_message_api(message=body)
                                    send_line_message_api(message=body)

                                    # 代行終了スナップショット（元のまま）
                                    self.page.wait_for_timeout(7_000)
                                    _ensure_viewport(self.page, need_w=310, need_h=70)
                                    self.page.mouse.click(300, 60)
                                    self.page.wait_for_timeout(1_000)
                                    _snap(self.page, "代行終了後")
                                    self.page.wait_for_timeout(2_000)
                                    _ensure_viewport(self.page, need_w=1210, need_h=60)
                                    self.page.mouse.click(1200, 50)
                                    self.page.wait_for_timeout(1_000)

                                    ctx = pop_stop_ctx()
                                    if ctx:
                                        post_in_thread(ctx.channel, ctx.thread_ts, "🧹 終局後の後片付けが完了しました。代打ちを終了します。")
                                    if stop_event.is_set():
                                        stop_event.clear()

                            except Exception as e:
                                logger.error(f"[gate] error: {e}")

                            self._postgame_guard.bump()
                        
                    except Exception as e:
                        logger.error(f"[post-game] error: {e}")
                continue

    # -------------- Lifecycle -------------

    def start(self) -> None:
        """
        Starts the Playwright instance, opens the browser, and begins
        the command processing loop.
        """
        logger.info("Controller Starting...")
        self.running = True


        try:
            with sync_playwright() as p:
                self.playwright = p
                self.browser = self.playwright.chromium.launch_persistent_context(
                    user_data_dir=Path().cwd() / "playwright_data",
                    headless=False,                      # アプリウィンドウで表示
                    viewport=None,                       # ← window-size を優先させる
                    ignore_default_args=['--enable-automation'],
                    args=[
                        f'--app={"https://game.mahjongsoul.com"}',      # ← ここがポイント（例: https://game.mahjongsoul.com）
                        f'--window-size={self.width},{self.height}',
                        '--no-first-run',
                        '--no-default-browser-check',
                        '--noerrdialogs',
                        # 必要なら: '--start-fullscreen',  # さらに広く表示したいとき
                        # 必要なら: '--kiosk',             # 完全全画面（Escで解除不可。用途に注意）
                    ],
                    chromium_sandbox=True,
                )

                pages: list[Page] = self.browser.pages
                if not pages:
                    logger.error("No pages found in the browser context.")
                    return
                if len(pages) > 1:
                    for page in pages[1:]:
                        logger.info(f"Closing extra page: {page.url}")
                        page.close()

                self.page = pages[0]
                self.page.on("websocket", self._on_web_socket)

                logger.info(f"Navigating to {self.url}...")
                register_page(self.page)  # hooks: 外部オート等が必要な場合の受け渡し
                self.page.goto(self.url)

                # --- 起動直後の自動“対戦開始”（一度だけ） ---
                try:
                    if not self._auto_started_once and not self._started:
                        # ロビーUIが整うまで少し待つ（必要に応じて調整）
                        self.page.wait_for_timeout(3000)
                        # ★ ゲート判定してから開始
                        if allow_auto_start_by_rank(self.page):
                            # 代行開始前スナップショット
                            snap_before_start(self)
                            run_auto_start_sequence(self.page)
                            self._auto_started_once = True
                            # # アプリ起動時にSlack受信を開始
                            start_socket_mode_in_thread()
                        else:
                            notify_log.warning("[auto-start] skipped by rank gate (boot)")
                except Exception as e:
                    logger.error(f"[auto-start] failed: {e}")
# -------------------------------------------

                # メインループ開始
                self._process_commands()

        except Exception as e:
            logger.error(f"A critical error occurred during Playwright startup or operation: {e}")
        finally:
            logger.info("Shutting down...")
            self.running = False
            logger.info("Controller Stopped.")

    def stop(self) -> None:
        """Signals the controller to stop and cleans up resources."""
        if self.running:
            logger.info("Sending stop signal...")
            self.command_queue.put({"command": "stop"})
        else:
            logger.info("Controller already stopped.")

    # -------------- Public API -------------

    def click(self, x: float, y: float) -> None:
        """
        Queue a click command on normalized grid (0..16, 0..9).
        """
        if self.running:
            self.command_queue.put({"command": "click", "point": [x, y]})
        else:
            logger.warning("Controller is not running. Cannot queue click command.")

def _ensure_viewport(page: Page, need_w: int, need_h: int) -> None:
    """クリック座標がビューポート外なら、その場で広げる（落下防止）。"""
    vp = page.viewport_size or {"width": 1600, "height": 900}
    cur_w, cur_h = vp["width"], vp["height"]
    new_w = max(cur_w, need_w)
    new_h = max(cur_h, need_h)
    if new_w != cur_w or new_h != cur_h:
        page.set_viewport_size({"width": new_w, "height": new_h})

def run_fixed_postgame_sequence(page: Page) -> None:
    """
    終局 → 10秒 → (1456,929) → 5秒 → (1456,929) → 5秒 → (1223,937) → 5秒 → (666,775)
    それぞれの押下をスクショで証跡化。最後に start_game を WS で検証。
    """
    # 事前スクショ
    # _snap(page, "before_sequence")

    # 20秒待機
    page.wait_for_timeout(30_000)

    # 1回目 確認
    _ensure_viewport(page, need_w=1500+10, need_h=870+10)
    _snap_with_marker(page, 1500, 870, "end1_marker")
    page.mouse.click(1500, 870)
    page.wait_for_timeout(5_000)
    # _snap(page, "after_tap1")
    # page.wait_for_timeout(5_000)

    # 2回目 確認
    _ensure_viewport(page, need_w=1500+10, need_h=870+10)
    _snap_with_marker(page, 1500, 870, "end2_marker")
    page.mouse.click(1500, 870)
    page.wait_for_timeout(5_000)
    # _snap(page, "after_tap2")
    # page.wait_for_timeout(5_000)

    # 3回目 確認
    _ensure_viewport(page, need_w=1300+10, need_h=300+10)
    _snap_with_marker(page, 1300, 300, "start1_ranked")
    page.mouse.click(1300, 300)
    page.wait_for_timeout(5_000)
    # _snap(page, "after_tap3")
    # page.wait_for_timeout(5_000)
    # もう一局
    _ensure_viewport(page, need_w=1300+10, need_h=850+10)
    _snap_with_marker(page, 1300, 850, "end3_marker")
    page.mouse.click(1300, 850)
    page.wait_for_timeout(5_000)
    # _snap(page, "after_tap3")
    # page.wait_for_timeout(5_000)

    # 最後のクリック
    _ensure_viewport(page, need_w=666+10, need_h=700+10)
    _snap_with_marker(page, 666, 700, "end4_marker")
    page.mouse.click(666, 700)
    # page.wait_for_timeout(5_000)
    # _snap(page, "after_tap4")

    # ここで「対戦開始」を WS で検証（start_game フラグ）
    logger.info("[Proof] waiting for start_game via WS...")

def run_auto_start_sequence(page: Page) -> None:
    """
    「対戦開始」導線。
    - 段位戦 -> （level_id_4 により 銅/銀/金/玉の間） -> 四人東 or 四人南
    条件:
      10101,10102,10103 → 銅の間 → 四人東
      10201,10202,10203 → 銀の間 → 四人東
      10301,10302,10303 → 金の間 → 四人南
      それ以上 → 玉の間 → 四人南
      不明 → 金の間(既定) → 四人南
    """
    logger.info("[auto-start] begin")

    # 段位戦
    _ensure_viewport(page, need_w=900+10, need_h=180+10)
    page.mouse.click(900, 180)
    page.wait_for_timeout(2_000)

    # --- アカウントから level_id_4 を取得して分岐 ---
    room_type = "bronze"  # 既定: 金の間
    try:
        rank_info = fetch_current_rank_ids(page) or {}
        level_id_4 = _as_int(rank_info.get("level_id_4"))
        notify_log.info(f"[auto-start] detected level_id_4={level_id_4}")

        if level_id_4 in (10101, 10102, 10103):
            room_type = "bronze"
            notify_log.info(f"[auto-start] -> 銅の間を選択")
        elif level_id_4 in (10201, 10202, 10203):
            room_type = "silver"
            notify_log.info(f"[auto-start] -> 銀の間を選択")
        elif level_id_4 in (10301, 10302, 10303):
            room_type = "gold"
            notify_log.info(f"[auto-start] -> 金の間を選択")
        elif level_id_4 is not None and level_id_4 >= 10401:
            room_type = "jade"
            notify_log.info(f"[auto-start] -> 玉の間を選択")
        else:
            notify_log.info(f"[auto-start] 未定義/その他 -> 金の間(既定)を選択")
    except Exception as e:
        notify_log.warning(f"[auto-start] level_id_4 の取得に失敗: {e} -> 金の間(既定)を選択")

    # 銅 / 銀 / 金 / 玉 のクリック
    if room_type == "bronze":
        _ensure_viewport(page, need_w=900+10, need_h=300+10)
        page.mouse.click(900, 300)   # 銅の間
    elif room_type == "silver":
        _ensure_viewport(page, need_w=900+10, need_h=400+10)
        page.mouse.click(900, 400)   # 銀の間
    elif room_type == "gold":
        _ensure_viewport(page, need_w=900+10, need_h=500+10)
        page.mouse.click(900, 500)   # 金の間
    elif room_type == "jade":
        _ensure_viewport(page, need_w=900+10, need_h=600+10)
        page.mouse.click(900, 600)   # 玉の間
    page.wait_for_timeout(2_000)

    # --- 四人南 or 四人東 ---
    if room_type in ("bronze", "silver"):
        _ensure_viewport(page, need_w=900+10, need_h=300+10)
        page.mouse.click(900, 300)   # 四人東
        notify_log.info("[auto-start] -> 四人東を選択")
    else:
        _ensure_viewport(page, need_w=900+10, need_h=400+10)
        page.mouse.click(900, 400)   # 四人南
        notify_log.info("[auto-start] -> 四人南を選択")

    page.wait_for_timeout(2_000)
    logger.info("[auto-start] done")

def snap_before_start(self):
    # 代行開始前スナップショット
    self.page.wait_for_timeout(7_000)
    _ensure_viewport(self.page, need_w=300+10, need_h=60+10)
    self.page.mouse.click(300, 60)
    self.page.wait_for_timeout(2_000)
    _snap(self.page, "代行開始前")
    self.page.wait_for_timeout(2_000)
    _ensure_viewport(self.page, need_w=1200+10, need_h=50+10)
    self.page.mouse.click(1200, 50)
    self.page.wait_for_timeout(2_000)

def perform_logout(self):
    # ログアウト
    _ensure_viewport(self.page, need_w=1210+10, need_h=50+10)
    self.page.mouse.click(1210, 50)
    self.page.wait_for_timeout(2_000)

    _ensure_viewport(self.page, need_w=300+10, need_h=580+10)
    self.page.mouse.click(300, 580)
    self.page.wait_for_timeout(2_000)

    _ensure_viewport(self.page, need_w=500+10, need_h=450+10)
    self.page.mouse.click(500, 450)
    self.page.wait_for_timeout(2_000)

    time.sleep(3.00)
    _snap(self.page, "ログアウト完了")

def perform_login(self, text: str):
    # リロード
    self.page.reload()
    self.page.wait_for_timeout(3_000)  # 少し待機

    # ログイン
    self.page.wait_for_timeout(6_000)
    _ensure_viewport(self.page, need_w=1000+10, need_h=330+10)
    self.page.mouse.click(1000, 330)

    # メールアドレス
    self.page.wait_for_timeout(2_000)
    _ensure_viewport(self.page, need_w=500+10, need_h=270+10)
    self.page.mouse.click(500, 270)
    time.sleep(0.05)

    for _ in range(30):
        pyautogui.press("backspace")
        time.sleep(0.05)

    self.page.wait_for_timeout(1_000)
    text = normalize_slack_text(text)

    self.page.wait_for_selector('input[placeholder="メールアドレス"]')
    self.page.fill('input[placeholder="メールアドレス"]', text)

    # コードを受け取る
    self.page.wait_for_timeout(2_000)
    _ensure_viewport(self.page, need_w=650+10, need_h=360+10)
    self.page.mouse.click(650, 360)

    time.sleep(1.0)
    _snap(self.page, "認証コード送信")

    # OK
    self.page.wait_for_timeout(2_000)
    _ensure_viewport(self.page, need_w=650+10, need_h=460+10)
    self.page.mouse.click(650, 460)

def perform_authcode(self, text: str):
    # 念のため正規化（mrkdwn剥がし）
    try:
        text = normalize_slack_text(text)  # あれば利用、無ければ pass
    except NameError:
        pass

    # 認証コードフィールドにフォーカス
    self.page.wait_for_timeout(2_000)
    _ensure_viewport(self.page, need_w=350+10, need_h=360+10)
    self.page.mouse.click(350, 360)

    # クリップボードへコピー → ペースト
    # subprocess.run("pbcopy", text=True, input=text)  # macOS
    # time.sleep(0.2)
    # pyautogui.hotkey("command", "v")

    self.page.wait_for_timeout(1_000)
    self.page.wait_for_selector('input[placeholder="認証コード"]')
    self.page.fill('input[placeholder="認証コード"]', text)

    # 「ログイン」ボタンへ
    self.page.wait_for_timeout(2_000)
    _ensure_viewport(self.page, need_w=470+10, need_h=490+10)
    self.page.mouse.click(470, 490)
